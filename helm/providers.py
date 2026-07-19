#!/usr/bin/env python3
"""The credential/quota provider seam. ABSORBED from sesh
(server/providers.py, behavior-preserving) per the dissolve-into-helm law;
rebrand only — HELM_* env preferred with legacy SESH_* fallback, cache under
~/.cache/helm/ (seeded once from the sesh cache when present).

Everything helm knows about accounts, quota windows, burn history, allocation,
and resume-command truth flows through ONE interface, so a host app (or a future
native-OAuth provider) can swap the backend without touching routes or UI.

The contract (all methods return plain JSON-able data, never token contents):
    accounts()               -> [{name, provider, home, active, tier}]
    cred_state()             -> [{account, provider, cred_state, headroom_pct, status,
                                  resets_at_ms, tier, home}]
    windows()                -> [{account, provider, windows_left, windows_fit,
                                  windows_per_week, verdict, cost_per_window, cycles, assumed}]
    history(hours)           -> [{account, provider, probed_at, gauges:[{label, utilization, reset}]}]
    allocate(model)          -> ranked [{account, eligible, headroom_pct, tier, home, blocked_by}]
                                (native also adds "why": a short human ranking reason)
    launch_cmd(account, sid, model=None) -> "shell command string"  (raises ProviderError)
    preflight(account, sid, agent)       -> {resolvable, live_holder_pid, reason, ...}

Two implementations live here:
  CliQuotaProvider    — shells a local quota CLI (tokaware) with --json verbs.
  NativeQuotaProvider — first-principles, stdlib-only: scans the credential homes
                        and probes the vendors' own usage endpoints directly.
                        This is the deprecation path for the external CLI.

Selection (default_provider): HELM_PROVIDER=native (or legacy SESH_PROVIDER)
forces native; anything else uses the CLI provider — unless the CLI binary is
absent, in which case native is the automatic fallback.
"""
import base64
import calendar
import concurrent.futures
import glob
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request


class ProviderError(RuntimeError):
    pass


def _env(name, default=None):
    """HELM_<name> preferred; legacy SESH_<name> accepted as fallback (the
    same env-transition law as helm/catalog.py)."""
    v = os.environ.get("HELM_" + name)
    if v is None:
        v = os.environ.get("SESH_" + name)
    return default if v is None else v


class CliQuotaProvider:
    """Backend = a local quota/allocation CLI with --json verbs (configurable binary)."""

    def __init__(self, binary=None):
        self.binary = binary or _env("QUOTA_CLI", "tokaware")

    def _run(self, *args, timeout=45):
        try:
            p = subprocess.run([self.binary, *args], capture_output=True, text=True,
                               timeout=timeout)
        except FileNotFoundError:
            raise ProviderError(f"quota CLI not found: {self.binary} (set HELM_QUOTA_CLI)")
        except subprocess.TimeoutExpired:
            raise ProviderError(f"{self.binary} {' '.join(args[:2])} timed out after {timeout}s")
        return p.returncode, p.stdout.strip(), p.stderr.strip()

    def _json(self, *args, timeout=45, default=None):
        rc, out, err = self._run(*args, timeout=timeout)
        if rc != 0 or not out:
            if default is not None:
                return default
            raise ProviderError(f"{self.binary} {' '.join(args)} failed: {err or rc}")
        try:
            return json.loads(out)
        except ValueError:
            raise ProviderError(f"{self.binary} {' '.join(args)} emitted non-JSON")

    def accounts(self):
        return self._json("list", "--json", default=[])

    def cred_state(self):
        return self._json("cred-state", "--json", timeout=60, default=[])

    def windows(self):
        return self._json("windows", "--json", timeout=60, default=[])

    def history(self, hours):
        rows = self._json("history", "--json", "--last", "50000", "--since", str(hours),
                          timeout=90, default=[])
        return [r for r in rows if r.get("gauges")]

    def allocate(self, model):
        return self._json("allocate", model, "--json", default=[])

    def launch_cmd(self, account, sid, model=None):
        args = ["launch-cmd", account, "--resume", sid]
        if model:
            args += ["--model", model]
        rc, out, err = self._run(*args)
        if rc != 0 or not out:
            raise ProviderError(f"launch-cmd failed: {err or out or rc}")
        return out

    def preflight(self, account, sid, agent):
        # no default: a failed safety check must be distinguishable from "all clear"
        return self._json("resume-preflight", account, sid, "--agent", agent, "--json")


# ---------------------------------------------------------------------------
# Native provider — no external CLI. Read-only by design: credentials are read
# into memory ONLY to authorize the provider's own HTTPS request to the vendor's
# own usage endpoint. No token is ever logged, returned, written, refreshed, or
# copied. Homes are never mutated. (Token refresh/rotation stays the CLIs' job:
# claude/codex refresh their own creds on launch — out of scope here, on purpose.)
# ---------------------------------------------------------------------------

ANTHROPIC_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


def _iso_z(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _epoch(iso):
    """ISO-8601 UTC string ('...Z' or '+00:00') -> unix seconds, else None."""
    try:
        return calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _jwt_email(id_token):
    """Best-effort email claim from an id_token payload (base64 decode only,
    NO verification — identity label, not authentication). Token discarded."""
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        email = claims.get("email")
        return email if isinstance(email, str) and email else None
    except Exception:
        return None


def _find_cwd(obj, depth=0):
    if depth > 3 or not isinstance(obj, dict):
        return None
    v = obj.get("cwd")
    if isinstance(v, str) and v:
        return v
    for val in obj.values():
        found = _find_cwd(val, depth + 1) if isinstance(val, dict) else None
        if found:
            return found
    return None


def _model_family(model):
    m = (model or "").strip().lower()
    return "codex" if (m.startswith("gpt") or m.startswith("o3") or m.startswith("o4")
                       or "codex" in m) else "anthropic"


class NativeQuotaProvider:
    """Stdlib-only provider over the on-disk credential homes + vendor usage APIs.

    accounts   ~/.claude-homes/*/ and ~/.codex-homes/*/ (realpath+inode dedup,
               so alias symlinks collapse), plus the default ~/.claude / ~/.codex
               as "(default-claude)"/"(default-codex)" when authed and distinct.
    cred_state one live GET per usable account against the vendor's own usage
               endpoint, in parallel; every probe cycle is appended to
               ~/.cache/helm/native-usage-history.jsonl so history charts fill
               up over time (downsampling stays the server's job).
    windows    model v1, an APPROXIMATION (every row carries assumed:true) —
               the use-it-or-lose-it math over the freshest probe + observed
               history; exact formulas documented on windows().
    headroom   matches the CLI's semantic: 100 - primary (session/5h) gauge
               utilization; resets_at_ms is that gauge's reset. exhausted =
               any session/period gauge at >=100%.
    allocate   headroom ranking, optionally shaped by operator rules from
               ~/.config/helm/allocation.json (legacy ~/.config/sesh/ honored;
               env override HELM_ALLOCATION_RULES / SESH_ALLOCATION_RULES):
               {"models": {"<model-substring>": {"prefer": [...], "avoid": [...]}},
                "drain_pin": {"enabled": true}}. No file -> pure headroom ranking.
    """

    TTL = 60  # seconds; helm web caches on top of this too
    SESSION_WINDOW_H = 5.0                             # vendor session window length
    NOMINAL_SLOTS_PER_WEEK = 7 * 24 / SESSION_WINDOW_H  # 33.6 five-hour slots fit a week
    FALLBACK_FULL_WINDOW_COST = 1 / 8.0  # documented fallback: with thin history and no
    # observed session->weekly ratio, assume one FULL 5h window burns 1/8 of the weekly
    # (conservative: real Max/Team accounts observe ~5-8 full windows per weekly budget)

    def __init__(self, history_path=None):
        self.history_path = history_path or os.path.expanduser(
            "~/.cache/helm/native-usage-history.jsonl")
        if history_path is None and not os.path.exists(self.history_path):
            legacy = os.path.expanduser("~/.cache/sesh/native-usage-history.jsonl")
            if os.path.exists(legacy):  # one-time seed: sesh's observations carry over
                try:
                    os.makedirs(os.path.dirname(self.history_path), exist_ok=True)
                    shutil.copyfile(legacy, self.history_path)
                except OSError:
                    pass
        self.claude_root = os.path.expanduser("~/.claude-homes")
        self.codex_root = os.path.expanduser("~/.codex-homes")
        self._lock = threading.Lock()
        self._cred_cache = None      # (monotonic_ts, rows)
        self._active_cache = None    # (monotonic_ts, set_of_realpaths)
        self._probe_thread = None    # start_probe_loop() daemon, once per provider

    # -- home scanning -------------------------------------------------------

    def _dedup_dirs(self, root, seen):
        out = []
        for p in sorted(glob.glob(os.path.join(root, "*/"))):
            real = os.path.realpath(p.rstrip("/"))
            try:
                st = os.stat(real)
            except OSError:
                continue
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            seen.add(key)
            out.append(real)
        return out

    def _active_homes(self):
        """realpaths named by any live process's CLAUDE_CONFIG_DIR/CODEX_HOME.
        Only environs we may read (our own user's); permission errors ignored."""
        now = time.monotonic()
        if self._active_cache and now - self._active_cache[0] < 15:
            return self._active_cache[1]
        vals = set()
        for envf in glob.glob("/proc/[0-9]*/environ"):
            try:
                with open(envf, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            for chunk in data.split(b"\0"):
                if chunk.startswith(b"CLAUDE_CONFIG_DIR=") or chunk.startswith(b"CODEX_HOME="):
                    try:
                        vals.add(os.path.realpath(chunk.split(b"=", 1)[1].decode("utf-8", "replace")))
                    except (OSError, ValueError):
                        pass
        self._active_cache = (now, vals)
        return vals

    @staticmethod
    def _anthropic_identity(home):
        """(email, human tier) from the home's .claude.json oauthAccount."""
        oa = (_read_json(os.path.join(home, ".claude.json")) or {}).get("oauthAccount") or {}
        email = oa.get("emailAddress") or None
        org = oa.get("organizationType") or ""
        rl = oa.get("organizationRateLimitTier") or oa.get("userRateLimitTier") or ""
        if org == "claude_team" or (oa.get("seatTier") or "").startswith("team"):
            tier = "Team"
        elif "max_20x" in rl:
            tier = "Max 20x"
        elif "max_5x" in rl:
            tier = "Max 5x"
        elif org == "claude_max":
            tier = "Max"
        elif org == "claude_pro" or "pro" in rl:
            tier = "Pro"
        elif org == "claude_enterprise":
            tier = "Enterprise"
        else:
            tier = None
        return email, tier

    def accounts(self):
        active = self._active_homes()
        seen, rows, names = set(), [], set()

        def add(name, provider, home, tier=None, usable=False, email=None):
            if name in names:  # two distinct homes, same identity — keep both, unambiguous
                name = f"{name}#{os.path.basename(home)}"
            names.add(name)
            rows.append({"name": name, "provider": provider, "home": home,
                         "active": home in active, "tier": tier,
                         "usable": usable, "email": email})

        for home in self._dedup_dirs(self.claude_root, seen):
            email, tier = self._anthropic_identity(home)
            add(email or os.path.basename(home), "anthropic", home, tier,
                usable=os.path.exists(os.path.join(home, ".credentials.json")), email=email)
        for home in self._dedup_dirs(self.codex_root, seen):
            auth = _read_json(os.path.join(home, "auth.json"))
            email = _jwt_email(((auth or {}).get("tokens") or {}).get("id_token") or "")
            add(os.path.basename(home), "codex", home,
                usable=bool(auth), email=email)
        for home, name, provider, authfile in (
                (os.path.expanduser("~/.claude"), "(default-claude)", "anthropic", ".credentials.json"),
                (os.path.expanduser("~/.codex"), "(default-codex)", "codex", "auth.json")):
            real = os.path.realpath(home)
            try:
                st = os.stat(real)
            except OSError:
                continue
            if (st.st_dev, st.st_ino) in seen or not os.path.exists(os.path.join(real, authfile)):
                continue  # symlinked onto a scanned home, or not authed
            seen.add((st.st_dev, st.st_ino))
            tier = email = None
            if provider == "anthropic":
                email, tier = self._anthropic_identity(real)
            add(name, provider, real, tier, usable=True, email=email)
        return rows

    # -- usage probing -------------------------------------------------------

    @staticmethod
    def _get_json(url, headers):
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)

    @staticmethod
    def _gauge(label, kind, percent, reset):
        return {"label": label, "kind": kind,
                "utilization": round((percent or 0) / 100.0, 4),
                "reset": reset, "limit": None, "remaining": None}

    @classmethod
    def _anthropic_gauges(cls, data):
        gauges = []
        for lim in data.get("limits") or []:
            kind, group = lim.get("kind") or "", lim.get("group") or ""
            pct, reset = lim.get("percent"), _epoch(lim.get("resets_at"))
            if kind == "session" or group == "session":
                gauges.append(cls._gauge("5h", "session", pct, reset))
            elif kind == "weekly_all":
                gauges.append(cls._gauge("7d", "period", pct, reset))
            elif kind == "weekly_scoped":
                if not pct and not lim.get("is_active"):
                    continue  # dormant scoped window (e.g. Fable at 0%) — noise
                scope = lim.get("scope") or {}
                name = ((scope.get("model") or {}).get("display_name")
                        or scope.get("surface") or "scoped")
                gauges.append(cls._gauge(f"7d-{str(name).lower()}", "period", pct, reset))
            else:  # future kinds flow through under their own name
                gauges.append(cls._gauge(kind or "unknown",
                                         "period" if group == "weekly" else (group or "window"),
                                         pct, reset))
        if not gauges:  # older shape fallback: top-level window objects
            for key, label, kind in (("five_hour", "5h", "session"), ("seven_day", "7d", "period")):
                w = data.get(key)
                if isinstance(w, dict):
                    gauges.append(cls._gauge(label, kind, w.get("utilization"),
                                             _epoch(w.get("resets_at"))))
            for key, w in data.items():
                if key.startswith("seven_day_") and isinstance(w, dict) and "utilization" in w:
                    gauges.append(cls._gauge("7d-" + key[10:].replace("_", "-"), "period",
                                             w.get("utilization"), _epoch(w.get("resets_at"))))
        extra = data.get("extra_usage") or {}
        if extra.get("is_enabled") and isinstance(extra.get("utilization"), (int, float)):
            gauges.append(cls._gauge("overage", "overage", extra["utilization"], None))
        return gauges

    @classmethod
    def _codex_gauges(cls, data):
        def windows(rl, suffix=""):
            for wkey, fallback in (("primary_window", "5h"), ("secondary_window", "7d")):
                w = (rl or {}).get(wkey)
                if not isinstance(w, dict):
                    continue
                secs = w.get("limit_window_seconds") or 0
                label = (f"{secs // 86400}d" if secs and secs % 86400 == 0 else
                         f"{secs // 3600}h" if secs and secs % 3600 == 0 else fallback)
                yield cls._gauge(label + suffix, "session" if secs < 86400 else "period",
                                 w.get("used_percent"), w.get("reset_at"))
        gauges = list(windows(data.get("rate_limit")))
        for extra in data.get("additional_rate_limits") or []:
            name = str(extra.get("limit_name") or extra.get("metered_feature") or "scoped").lower()
            gauges.extend(windows(extra.get("rate_limit"),
                                  "-" + re.sub(r"[^a-z0-9.]+", "-", name).strip("-")))
        return gauges

    @staticmethod
    def _primary(gauges):
        """The binding gauge: the account-wide session window, else the fullest gauge."""
        return (next((g for g in gauges if g["kind"] == "session" and "-" not in g["label"]), None)
                or (max(gauges, key=lambda g: g["utilization"]) if gauges else None))

    def _probe_one(self, acct):
        """-> (cred_state row, history row). Never raises; failure degrades the row."""

        def rows(state, status, gauges=(), tier=None, note=None, source_at=None):
            gauges = list(gauges)
            primary = self._primary(gauges)
            cred = {"account": acct["name"], "provider": acct["provider"], "cred_state": state,
                    "headroom_pct": round(100 - primary["utilization"] * 100, 1) if primary else None,
                    "status": status,
                    "resets_at_ms": primary["reset"] * 1000 if primary and primary["reset"] else None,
                    "tier": tier or acct.get("tier"), "home": acct["home"], "source_at": source_at}
            if note:
                cred["note"] = note
            hist = {"provider": acct["provider"], "account": acct["name"], "probed_at": _iso_z(),
                    "status": status, "primary": primary["label"] if primary else None,
                    "gauges": gauges, "source_at": None}
            return cred, hist

        if acct["provider"] == "anthropic":
            creds = _read_json(os.path.join(acct["home"], ".credentials.json")) or {}
            token = (creds.get("claudeAiOauth") or {}).get("accessToken")
            if not token:
                return rows("api-error", "no-credentials")
            try:
                data = self._get_json(ANTHROPIC_USAGE_URL, {"Authorization": "Bearer " + token})
            except urllib.error.HTTPError as e:
                e.close()  # an HTTPError IS a response object — close its fp deterministically
                return rows("api-error", "needs_reauth" if e.code in (401, 403) else f"http_{e.code}")
            except Exception:
                return rows("api-error", "network-error")
            gauges = self._anthropic_gauges(data)
            exhausted = any(g["utilization"] >= 0.999 for g in gauges if g["kind"] != "overage")
            return rows("exhausted" if exhausted else "ok",
                        "blocked" if exhausted else "allowed", gauges)

        # codex — endpoint verified live, but stored access tokens go stale between
        # codex runs (codex refreshes them itself on launch; we never refresh).
        # Any failure -> "unknown" with a note, never a provider failure.
        tokens = (_read_json(os.path.join(acct["home"], "auth.json")) or {}).get("tokens") or {}
        token, acct_id = tokens.get("access_token"), tokens.get("account_id")
        if not token:
            return rows("unknown", "no-credentials", note="auth.json has no access token")
        headers = {"Authorization": "Bearer " + token}
        if acct_id:
            headers["chatgpt-account-id"] = acct_id
        try:
            data = self._get_json(CODEX_USAGE_URL, headers)
        except urllib.error.HTTPError as e:
            e.close()  # an HTTPError IS a response object — close its fp deterministically
            return rows("unknown", "needs_reauth" if e.code in (401, 403) else f"http_{e.code}",
                        note="stored access token rejected — codex refreshes it on next launch")
        except Exception:
            return rows("unknown", "network-error", note="usage endpoint unreachable")
        rl = data.get("rate_limit") or {}
        exhausted = bool(rl.get("limit_reached")) or rl.get("allowed") is False
        return rows("exhausted" if exhausted else "ok",
                    "blocked" if exhausted else "allowed", self._codex_gauges(data),
                    tier=(data.get("plan_type") or "").title() or None)

    @staticmethod
    def _transient(status):
        """Throttle/outage on the usage endpoint — says nothing about the cred."""
        if status == "network-error":
            return True
        code = status[5:] if status.startswith("http_") else ""
        return code.isdigit() and (int(code) == 429 or int(code) >= 500)

    def _last_observed(self, account, max_age_s=900):
        """Most recent history row for this account with gauges, if fresh enough."""
        floor = _iso_z(time.time() - max_age_s)
        best = None
        try:
            with open(self.history_path) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if (r.get("account") == account and r.get("gauges")
                            and r.get("probed_at", "") >= floor
                            and (not best or r["probed_at"] > best["probed_at"])):
                        best = r
        except OSError:
            pass
        return best

    def cred_state(self):
        with self._lock:
            if self._cred_cache and time.monotonic() - self._cred_cache[0] < self.TTL:
                return [dict(r) for r in self._cred_cache[1]]
        # one probe per unique identity, not per home: alias logins of the same
        # account (same email) share quota — probing each home would both waste
        # calls and trip the endpoint's rate limit (observed http_429).
        groups = {}
        for a in self.accounts():
            if a["usable"]:
                groups.setdefault((a["provider"], a.get("email") or a["name"]), []).append(a)
        if not groups:
            return []
        members = list(groups.values())
        # STAGGERED probes, 2 lanes max: tonight's 429s came from bursting all
        # identities at the vendor simultaneously (two sesh lanes compounding it).
        # A ~0.7s stagger per submit keeps a full roster under any sane rate limit
        # while still overlapping network waits.
        results = [None] * len(members)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            futs = {}
            for i, g in enumerate(members):
                futs[ex.submit(self._probe_one, g[0])] = i
                time.sleep(0.7)
            for f in concurrent.futures.as_completed(futs):
                results[futs[f]] = f.result()
        creds, hists = [], []
        for accts, (cred, hist) in zip(members, results):
            if cred["cred_state"] in ("api-error", "unknown") and self._transient(cred["status"] or ""):
                # a throttle (429/5xx) says nothing about the CRED — serve hours-old
                # truth marked stale rather than an api-error; source_at carries the age
                max_age = 6 * 3600 if "429" in (cred["status"] or "") else 900
                seen = self._last_observed(accts[0]["name"], max_age_s=max_age)
                if seen:  # throttled probe ≠ dead cred: serve the last observation, marked stale
                    gauges = seen["gauges"]
                    primary = self._primary(gauges)
                    exhausted = any(g["utilization"] >= 0.999 for g in gauges if g["kind"] != "overage")
                    cred.update(cred_state="exhausted" if exhausted else "ok",
                                headroom_pct=round(100 - primary["utilization"] * 100, 1),
                                resets_at_ms=primary["reset"] * 1000 if primary["reset"] else None,
                                source_at=seen["probed_at"],
                                note=f"usage endpoint {cred['status']} — showing last observation")
                    hist = None  # not a new observation; don't fake a history point
            if hist is not None:
                hists.append(hist)
            for a in accts:
                row = dict(cred, account=a["name"], home=a["home"],
                           tier=cred["tier"] or a.get("tier"))
                creds.append(row)
        self._append_history(hists)
        with self._lock:
            self._cred_cache = (time.monotonic(), creds)
        return [dict(r) for r in creds]

    # -- history persistence ---------------------------------------------------

    def _append_history(self, rows):
        os.makedirs(os.path.dirname(self.history_path), exist_ok=True)
        with self._lock:
            with open(self.history_path, "a") as f:
                for r in rows:
                    f.write(json.dumps(r, separators=(",", ":")) + "\n")
            try:  # opportunistic prune: keep the file bounded (~30 days is plenty)
                if os.path.getsize(self.history_path) > 32 * 1024 * 1024:
                    cutoff = _iso_z(time.time() - 30 * 86400)
                    with open(self.history_path) as f:
                        keep = [l for l in f if l[l.find('"probed_at":"') + 13:][:20] >= cutoff]
                    tmp = self.history_path + ".tmp"
                    with open(tmp, "w") as f:
                        f.writelines(keep)
                    os.replace(tmp, self.history_path)
            except OSError:
                pass
        return rows

    def history(self, hours):
        if _env("PROBE_LOOP") == "1":
            self.start_probe_loop()  # lazy opt-in autostart; no-op when already running
        cutoff = _iso_z(time.time() - hours * 3600)
        rows = []
        for attempt in (0, 1):
            try:
                with open(self.history_path) as f:
                    for line in f:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        if r.get("probed_at", "") >= cutoff and r.get("gauges"):
                            rows.append(r)
            except OSError:
                pass
            if rows or attempt:
                break
            self.cred_state()  # cold start: seed the file with one live probe cycle
        return rows

    # -- probe loop ------------------------------------------------------------

    def start_probe_loop(self, interval_s=300):
        """Background re-probe: force a fresh cred_state cycle (which appends to
        the history file) every interval ±jitter (±30s at the default 300s,
        scaled down for short test intervals). Daemon thread — dies with the
        process; idempotent — a second call while running is a no-op; failures
        are one stderr line per cycle, never an exception. Deliberately NOT
        wired into the server: hosts call this themselves, or set
        HELM_PROBE_LOOP=1 to have the first history() call start it lazily."""
        with self._lock:
            t = self._probe_thread
            if t is not None and t.is_alive():
                return t
            t = threading.Thread(target=self._probe_forever, args=(float(interval_s),),
                                 name="helm-native-probe", daemon=True)
            self._probe_thread = t
        t.start()
        return t

    def _probe_forever(self, interval_s):
        jitter = min(30.0, interval_s / 4)
        while True:
            try:
                with self._lock:
                    self._cred_cache = None  # bypass TTL: this cycle must really probe
                self.cred_state()
            except Exception as e:  # the loop must outlive any single bad cycle
                print(f"helm: native probe loop: {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(max(1.0, interval_s + random.uniform(-jitter, jitter)))

    # -- windows model v1 ------------------------------------------------------

    @staticmethod
    def _account_wide(gauges, kind):
        """The account-wide gauge of a kind; scoped gauges ('7d-fable') excluded."""
        return next((g for g in gauges if g["kind"] == kind and "-" not in g["label"]), None)

    def _history_model(self, now, max_age_s=900):
        """One pass over the history file ->
        {account: {"latest": freshest row within max_age_s or None,
                   "cycles": {session_reset_epoch: [(sess_util, weekly_util, weekly_reset)]}}}"""
        floor = _iso_z(now - max_age_s)
        acc = {}
        try:
            with open(self.history_path) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if not r.get("gauges") or not r.get("account"):
                        continue
                    a = acc.setdefault(r["account"], {"latest": None, "cycles": {}})
                    if r.get("probed_at", "") >= floor and (
                            not a["latest"] or r["probed_at"] > a["latest"]["probed_at"]):
                        a["latest"] = r
                    s = self._account_wide(r["gauges"], "session")
                    if s and s.get("reset"):
                        w = self._account_wide(r["gauges"], "period")
                        a["cycles"].setdefault(s["reset"], []).append(
                            (s["utilization"],
                             w["utilization"] if w else None,
                             (w or {}).get("reset")))
        except OSError:
            pass
        return acc

    @classmethod
    def _cost_per_window(cls, cycles, now):
        """(weekly fraction one typical 5h window costs, completed cycle count).

        cost = intensity * full_window_cost, where
          intensity        = mean over COMPLETED 5h cycles (session reset <= now)
                             of the max session utilization observed in the cycle;
                             thin history (<2 completed cycles) -> 1.0 (full windows)
          full_window_cost = the account's own observed session->weekly ratio:
                             median over cycles with >=2 samples, session peak >=5%,
                             and a stable weekly reset, of (weekly delta / session
                             peak); no observable ratio -> FALLBACK_FULL_WINDOW_COST
        Both clamped to physical bounds; approximation by construction."""
        peaks, ratios = [], []
        for reset, samples in cycles.items():
            if reset > now:
                continue  # cycle still open — its peak isn't final yet
            peak = max(s[0] for s in samples)
            peaks.append(peak)
            wk = [(u, rst) for _s, u, rst in samples if u is not None and rst]
            if peak >= 0.05 and len(wk) >= 2 and len({rst for _u, rst in wk}) == 1:
                ratios.append((max(u for u, _ in wk) - min(u for u, _ in wk)) / peak)
        if ratios:
            ratios.sort()
            full = min(max(ratios[len(ratios) // 2], 1 / cls.NOMINAL_SLOTS_PER_WEEK), 1.0)
        else:
            full = cls.FALLBACK_FULL_WINDOW_COST
        intensity = sum(peaks) / len(peaks) if len(peaks) >= 2 else 1.0
        return min(max(intensity * full, 0.01), 1.0), len(peaks)

    def windows(self):
        """Use-it-or-lose-it model v1 — an APPROXIMATION, so every row carries
        assumed:true for the UI/CLI to badge. Per account with a live probe:
          W            = 1 - account-wide weekly utilization   (remaining fraction)
          slots_left   = max(0, (weekly_reset - now)/3600) / 5 (5h windows that FIT)
          cost         = _cost_per_window() (weekly fraction per typical window)
          windows_left = min(slots_left, W / cost)
          verdict      = "no-weekly" (no weekly gauge/reset)
                       | "waste-danger" (windows_left > slots_left*0.85 AND W > 0.5
                         — budget for more windows than can physically be spent)
                       | "ok"
        windows_per_week = min(7*24/5, 1/cost): week-start slots bounded by what
        the weekly budget actually funds at the observed burn rate."""
        self.cred_state()  # ensure a probe cycle exists (appends to history)
        now = time.time()
        out = []
        for account, data in sorted(self._history_model(now).items()):
            seen = data["latest"]
            if not seen:
                continue  # no live probe — no row (an invented row would lie)
            provider = seen.get("provider", "unknown")
            cost, cycle_n = self._cost_per_window(data["cycles"], now)
            row = {"account": account, "provider": provider, "assumed": True,
                   "cost_per_window": round(cost, 4), "cycles": cycle_n,
                   "windows_per_week": round(min(self.NOMINAL_SLOTS_PER_WEEK, 1 / cost), 2)}
            weekly = self._account_wide(seen["gauges"], "period")
            if not weekly or not weekly.get("reset"):
                row.update(windows_left=0.0, windows_fit=0.0, verdict="no-weekly")
            else:
                W = max(0.0, 1.0 - weekly["utilization"])
                slots_left = max(0.0, (weekly["reset"] - now) / 3600.0) / self.SESSION_WINDOW_H
                windows_left = min(slots_left, W / cost)
                row.update(windows_left=round(windows_left, 2),
                           windows_fit=round(slots_left, 2),
                           verdict=("waste-danger"
                                    if windows_left > slots_left * 0.85 and W > 0.5
                                    else "ok"))
            out.append(row)
        return out

    # -- allocation + live verbs ----------------------------------------------

    def allocate(self, model):
        family = _model_family(model)
        states = {s["account"]: s for s in self.cred_state()}
        out = []
        for a in self.accounts():
            if a["provider"] != family or not a["usable"]:
                continue
            s = states.get(a["name"], {})
            state, headroom = s.get("cred_state", "unknown"), s.get("headroom_pct")
            blocked = []
            if state == "exhausted" or (isinstance(headroom, (int, float)) and headroom <= 0):
                blocked.append("exhausted")
            elif state == "api-error":
                blocked.append(s.get("status") or "api-error")
            # codex "unknown" stays eligible: the CLI self-refreshes creds on launch
            out.append({"account": a["name"], "eligible": not blocked,
                        "headroom_pct": headroom, "tier": s.get("tier") or a.get("tier"),
                        "home": a["home"], "blocked_by": blocked, "why": "headroom"})
        out.sort(key=lambda r: (r["headroom_pct"] is None, -(r["headroom_pct"] or 0)))
        rules = self._allocation_rules()
        return self._apply_rules(out, model, rules) if rules else out

    @staticmethod
    def _allocation_rules():
        """Operator allocation rules, optional. Missing file -> {} (pure headroom
        ranking, unchanged behavior); a present-but-broken file warns on stderr.
        Path: env override, else ~/.config/helm/, else the legacy sesh file."""
        helm_p = os.path.expanduser("~/.config/helm/allocation.json")
        path = _env("ALLOCATION_RULES") or (
            helm_p if os.path.exists(helm_p)
            else os.path.expanduser("~/.config/sesh/allocation.json"))
        rules = _read_json(path)
        if rules is None and os.path.exists(path):
            print(f"helm: allocation rules unreadable, ignoring: {path}", file=sys.stderr)
        return rules if isinstance(rules, dict) else {}

    def _apply_rules(self, ranked, model, rules):
        """Rules v1 over the headroom ranking. Model rule = longest key of
        rules["models"] that substring-matches the requested model (case-insens).
        prefer: stable-move those accounts to the front, in the rule's order.
        avoid:  stable-push to the back; eligible flips to False ONLY when
                headroom is also <10 (a healthy avoided account stays usable).
        drain_pin (global): an eligible, non-avoided account whose weekly resets
        within 24h while >40% of the weekly remains ranks FIRST — use-it-or-
        lose-it inverts earliest-deadline-first: spend the expiring window,
        never flee it. Every row's "why" says which rule placed it."""
        mrule = {}
        mkeys = [k for k in (rules.get("models") or {})
                 if k and k.lower() in (model or "").lower()]
        if mkeys:
            mrule = rules["models"][max(mkeys, key=len)] or {}
        prefer = [str(x).lower() for x in mrule.get("prefer") or []]
        avoid = {str(x).lower() for x in mrule.get("avoid") or []}
        keep, back = [], []
        for r in ranked:
            if r["account"].lower() in avoid:
                hr = r.get("headroom_pct")
                if isinstance(hr, (int, float)) and hr < 10:
                    r["eligible"] = False
                    r["blocked_by"] = list(r.get("blocked_by") or []) + ["avoid-rule+headroom<10"]
                    r["why"] = "avoided by rules (headroom <10)"
                else:
                    r["why"] = "avoided by rules"
                back.append(r)
            else:
                keep.append(r)
        by_name = {r["account"].lower(): r for r in keep}
        front = [by_name[p] for p in prefer if p in by_name]
        for r in front:
            r["why"] = "preferred by rules"
        front_ids = {id(r) for r in front}
        ranked = front + [r for r in keep if id(r) not in front_ids] + back
        if (rules.get("drain_pin") or {}).get("enabled"):
            now = time.time()
            hist = self._history_model(now)
            pins = []
            for r in ranked:
                if not r["eligible"] or r["why"].startswith("avoided"):
                    continue  # never pin a blocked or operator-avoided account
                seen = (hist.get(r["account"]) or {}).get("latest")
                weekly = self._account_wide(seen["gauges"], "period") if seen else None
                if not weekly or not weekly.get("reset"):
                    continue
                left_h = (weekly["reset"] - now) / 3600.0
                remaining = 1.0 - weekly["utilization"]
                if 0 < left_h <= 24 and remaining > 0.40:
                    r["why"] = f"drain-pin: {round(remaining * 100)}% expires in {left_h:.0f}h"
                    pins.append((left_h, r))
            pins.sort(key=lambda t: t[0])  # most-expiring first: drain soonest loss
            pin_rows = [r for _, r in pins]
            pin_ids = {id(r) for r in pin_rows}
            ranked = pin_rows + [r for r in ranked if id(r) not in pin_ids]
        return ranked

    def _find(self, account):
        return next((a for a in self.accounts() if a["name"] == account), None)

    def launch_cmd(self, account, sid, model=None):
        acct = self._find(account)
        if not acct:
            raise ProviderError(f"unknown account: {account}")
        if not acct["usable"]:
            raise ProviderError(f"{account} has no credentials in {acct['home']}")
        if acct["provider"] == "codex":
            return f"CODEX_HOME={shlex.quote(acct['home'])} codex resume {shlex.quote(sid)}"
        model_part = f" --model {shlex.quote(model)}" if model else ""
        return (f"CLAUDE_CONFIG_DIR={shlex.quote(acct['home'])} "
                f"claude{model_part} --resume {shlex.quote(sid)}")

    def preflight(self, account, sid, agent):
        acct = self._find(account)
        if agent == "codex":
            hits = glob.glob(os.path.expanduser("~/.codex/sessions/**/rollout-*") + sid + "*.jsonl",
                             recursive=True)
        else:  # all claude homes symlink projects/ to the one shared store
            hits = glob.glob(os.path.expanduser("~/.claude/projects/*/") + sid + ".jsonl")
        true_cwd = None
        if hits:
            try:
                with open(hits[0]) as f:
                    for _, line in zip(range(5), f):
                        true_cwd = _find_cwd(json.loads(line) if line.strip() else {})
                        if true_cwd:
                            break
            except (OSError, ValueError):
                pass
        pid = self._live_holder(sid)
        if not hits:
            reason = (f"session {sid} not found in the shared store — resume would land in "
                      "'No conversation found' (fresh + re-brief)")
        elif pid:
            reason = f"session is OPEN in pid {pid} — resuming it elsewhere would double-open"
        else:
            reason = None
        return {"resolvable": bool(hits), "agent": agent, "account": account,
                "session_id": sid, "true_cwd": true_cwd,
                "home_path": acct["home"] if acct else None,
                "live_holder_pid": pid, "reason": reason}

    @staticmethod
    def _live_holder(sid):
        """pid of a live claude/codex process holding this session, else None.
        pgrep -af matches full cmdlines; wrapper/reader processes (pagers, greps,
        editors, this server) are filtered out."""
        try:
            p = subprocess.run(["pgrep", "-af", sid], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        own = {os.getpid(), os.getppid()}
        for line in p.stdout.splitlines():
            pid_s, _, cmd = line.partition(" ")
            if not pid_s.isdigit() or int(pid_s) in own:
                continue
            low = cmd.lower()
            argv0 = os.path.basename(low.split()[0]) if low.split() else ""
            if any(w in argv0 for w in ("pgrep", "grep", "rg", "tail", "less", "cat", "vim", "nano")):
                continue
            if "claude" in low or "codex" in low:
                return int(pid_s)
        return None


def default_provider():
    """NATIVE is the default (deprecation flip 2026-07-12): helm reads the
    providers' own usage endpoints directly. A legacy quota CLI is opt-in via
    HELM_PROVIDER=cli (+ optional HELM_QUOTA_CLI naming the binary); the
    SESH_* spellings still work."""
    choice = (_env("PROVIDER") or "native").strip().lower()
    if choice == "cli":
        binary = _env("QUOTA_CLI", "tokaware")
        if shutil.which(binary):
            return CliQuotaProvider(binary)
        # opted into a CLI that isn't installed: fail toward working, loudly
        print(f"helm: HELM_PROVIDER=cli but {binary!r} not found — using native provider",
              file=sys.stderr)
    return NativeQuotaProvider()
