#!/usr/bin/env python3
"""The credential/quota provider seam. ABSORBED from helm's predecessor
(its server/providers.py, behavior-preserving) per the dissolve-into-helm law;
rebrand only — HELM_* env preferred, with the predecessor's spelling as a
fallback where this host's local names declare one (helm/localnames.py);
cache under ~/.cache/helm/ (seeded once from the predecessor's cache when
present).

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
  CliQuotaProvider    — shells a local quota CLI (HELM_QUOTA_CLI) with --json verbs.
  NativeQuotaProvider — first-principles, stdlib-only: scans the credential homes
                        and probes the vendors' own usage endpoints directly.
                        This is the deprecation path for the external CLI.

Selection (default_provider): HELM_PROVIDER=native (or the predecessor's
spelling) forces native; anything else uses the CLI provider — unless the CLI binary is
absent, in which case native is the automatic fallback.
"""
import base64
import calendar
import concurrent.futures
import glob
import json
import math
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
from . import localnames, pk


class ProviderError(RuntimeError):
    pass


class NoQuotaProvider(ProviderError):
    """THERE IS NOTHING HERE TO MEASURE WITH — a different sentence from "the
    thing that measures failed", and the quota tab answers them differently.

    A host with no quota CLI configured, or one naming a binary that is not
    installed, measures nothing; that is an honest empty world and every
    surface stays fail-open over it, because sessions and resume do not need
    quota. A CLI that IS there and answered a non-zero rc or a page of
    non-JSON is a world helm COULD NOT READ, and the page has to say so.

    Under one exception class a reader cannot tell them apart, and a broken
    quota binary then draws as "no accounts on this machine" — an unreadable
    world reported as an empty one, at the door that decides whether there is
    a world at all. A subclass, so every `except ProviderError` still catches
    it and only a reader that wants the distinction has to name this."""


def _env(name, default=None):
    """HELM_<name> preferred; a declared predecessor's <NAME>_<name> accepted
    as fallback (the same env-transition law as helm/catalog.py)."""
    v = os.environ.get("HELM_" + name)
    if v is None:
        v = localnames.legacy_env(name)
    return default if v is None else v


class CliQuotaProvider:
    """Backend = a local quota/allocation CLI with --json verbs (configurable binary)."""

    def __init__(self, binary=None):
        # config-driven, no baked default binary: an unset HELM_QUOTA_CLI means
        # no CLI quota provider (native is the default provider anyway)
        self.binary = binary or _env("QUOTA_CLI", "")

    def _run(self, *args, timeout=45):
        if not self.binary:
            raise NoQuotaProvider("no quota CLI configured (set HELM_QUOTA_CLI)")
        try:
            p = subprocess.run([self.binary, *args], capture_output=True, text=True,
                               timeout=timeout)
        except FileNotFoundError:
            raise NoQuotaProvider(
                f"quota CLI not found: {self.binary} (set HELM_QUOTA_CLI)")
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

# THE CRED_STATE VOCABULARY, OWNED WHERE IT IS PRODUCED. Every value reaching a
# row's `cred_state` is minted in this file — `_probe_one`'s local `rows(state,
# ...)` and the refresh that updates it — so the set belongs here and nowhere
# else. A CONSUMER THAT COPIES THIS TUPLE IS NOT PINNED TO IT: a review of
# task/1644 found exactly that, a display test carrying its own copy, which
# stayed green while production truncated a state the copy did not know about.
# Import it; do not restate it.
#
# `dry` is deliberately ABSENT. It appears in creds.py prose about a seat that
# ran dry mid-work — a SEAT condition, not a credential state — and an earlier
# version of that display test listed it, a fixture richer than production
# asserting over a value nothing emits.
CRED_STATES = ("ok", "unknown", "api-error", "exhausted", "expired-token",
               "due-refresh")

# THE CHAIN VOCABULARY, BORROWED FROM cred.orca RATHER THAN REINVENTED.
# `cred.freshness` already names why a home's own chain is spent — its `chain`
# field says no-home-refresh-token / home-refresh-expired / same-family — but
# it answers about a home ORCA ALSO HOLDS A COPY OF (it measures the two
# against each other) and walks Orca's whole account store to do it. A probe
# cycle asks a narrower question about the home ALONE: can `helm keepalive`
# still grant on this home's refresh token? So the predicate lives here, over
# the oauth block `_probe_one` already has in hand, and reuses that module's
# spellings so one reader meets one vocabulary for one fact.
CHAIN_LIVE = "refresh-live"
CHAIN_UNPROVEN = "refresh-lifetime-unknown"
CHAIN_EXPIRED = "home-refresh-expired"
CHAIN_ABSENT = "no-home-refresh-token"
# The chains a keepalive grant can still be attempted on. A MISSING
# refreshTokenExpiresAt counts as refreshable, and that is a decision, not an
# oversight: keepalive never consults that field — it presents the refresh
# token and the token endpoint is the authority — and claude's credentials
# file does not always carry one. Reading "spent" out of a field that is
# simply absent would put the reauth sentence back on exactly the homes this
# split exists to rescue. The cost of reading it this way is one keepalive run
# that reports needs_reauth for a home the page called due; the cost of reading
# it the other way was six accounts telling the owner to log in again while
# every one of them was fine. (`seat_rehome` takes the OPPOSITE reading of the
# same absent field, and should: it refuses a LAUNCH, where an unproven chain
# bricks a live session's successor. Here the blast radius is a sentence.)
REFRESHABLE_CHAINS = (CHAIN_LIVE, CHAIN_UNPROVEN)


def refresh_chain(oauth, now=None):
    """Which state this claude oauth block's REFRESH token is in — presence and
    expiry only, no token byte read or returned."""
    if not (oauth or {}).get("refreshToken"):
        return CHAIN_ABSENT
    life = (oauth or {}).get("refreshTokenExpiresAt")
    if not isinstance(life, (int, float)) or isinstance(life, bool):
        return CHAIN_UNPROVEN
    if life / 1000 > (now if now is not None else time.time()):
        return CHAIN_LIVE
    return CHAIN_EXPIRED


def _ago(secs):
    """A coarse age. HOURS matter here where days did not: the cadence that
    cures a due-refresh home runs hourly, so "0d ago" would hide the only
    number that says whether the loop is turning."""
    secs = max(0, int(secs))
    if secs >= 86400:
        return "%dd" % (secs // 86400)
    if secs >= 3600:
        return "%dh" % (secs // 3600)
    return "%dm" % (secs // 60)


def _last_keepalive(home_path):
    """What helm's own keepalive log says about this home, as a clause. Never
    raises: a status sentence may not be taken down by an unreadable log."""
    try:
        from . import keepalive
        row = keepalive.last_refresh(os.path.basename(home_path or ""))
    except Exception:                      # noqa: BLE001 — a clause never raises
        return "helm's keepalive log could not be read"
    if not row:
        return ("no keepalive refresh recorded for it yet "
                "(`helm keepalive --ensure-timer` installs the cadence)")
    return "last keepalive refresh %s by %s" % (row["ts"], row["by"])


def _last_outcome(home_path):
    """keepalive's newest recorded decision for this home, or None; never
    raises, for the same reason `_last_keepalive` never does."""
    try:
        from . import keepalive
        return keepalive.last_outcome(os.path.basename(home_path or ""))
    except Exception:                      # noqa: BLE001 — a clause never raises
        return None


def _due_refresh_status(dead_s, chain, home_path):
    """The due-refresh sentence: the fact, the cure verb, and when the cure
    last ran. IT NEVER SAYS REAUTH. That word sends the owner to a login he
    does not owe: it once sent him to six of them at once, every account
    healthy, three helm homes merely overdue for a grant nothing was making."""
    return ("keepalive due (helm's token expired %s ago; its refresh chain is "
            "%s) — `helm keepalive --apply` refreshes it, no login needed; %s"
            % (_ago(dead_s), chain, _last_keepalive(home_path)))


def _claude_oauth(home_path):
    """The claudeAiOauth block of a claude home's own credentials file, or {}."""
    return ((_read_json(os.path.join(home_path or "", ".credentials.json")) or {})
            .get("claudeAiOauth") or {})


def _expires_ms(oauth):
    """A claudeAiOauth block's access-token expiresAt (epoch ms), or None
    when it is absent or not a number. A bool is not a number here: Python
    counts True as 1, which reads as a token that expired in 1970."""
    exp = oauth.get("expiresAt")
    return exp if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None


def _token_live(oauth):
    """Whether a claudeAiOauth block holds a token worth presenting: an
    access token whose expiresAt (epoch ms) is not in the past. A token
    with no expiresAt counts as live; the vendor call is its authority."""
    exp = _expires_ms(oauth)
    return bool(oauth.get("accessToken")) and not (
        exp is not None and exp / 1000 < time.time())


def _orca_refill_status(home_path):
    """The sync sentence for a credhome whose own copy holds no access token
    while Orca holds a fresher copy of the same account, or None. Never
    raises: a status sentence may not be taken down by Orca's store."""
    try:
        from .cred import orca
        fresh = orca.freshness(home_path)
        if fresh.get("verdict") != orca.STALE or not orca.is_credhome(home_path):
            return None
    except Exception:                      # noqa: BLE001 — a clause never raises
        return None
    return ("sync due (helm's copy holds no access token; Orca holds a fresher "
            "copy of this account, %s) — `helm cred sync-orca --home %s --apply` "
            "refills it, no login needed"
            % (fresh.get("chain") or "home chain spent",
               os.path.basename(home_path)))


ANTHROPIC_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# A codex row whose workspace is pooled and no pool file is proven to be this
# member (task/2981). A MEASURED unknown: the pool was read and names other
# members, not this one. Its page label and its cure (`helm codex pool
# <home>`) are not those of a row with no reading at all.
POOL_MEMBER_UNKNOWN = "pool-member-unknown"
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
        with pk.open_regular(path) as f:
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
               any MEASURED session/period gauge at >=100%. A gauge the vendor
               sent with no percent carries utilization None, which is UNREAD:
               never 0% (headroom), never 100% (exhausted). An unread primary
               has no headroom, and a reading with no measured plan gauge is
               cred_state `unknown`.
    allocate   headroom ranking, optionally shaped by operator rules from
               ~/.config/helm/allocation.json (a declared predecessor's
               ~/.config/<name>/ honored; env override HELM_ALLOCATION_RULES
               or the predecessor's spelling):
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
            legacy = localnames.legacy_path(".cache", "{}",
                                            "native-usage-history.jsonl")
            if legacy and os.path.exists(legacy):  # one-time seed: the predecessor's observations carry over
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
        """(email, human tier) from the ONE content identity reader. Quota,
        command minting, homes/list/doctor and usage attribution must agree even
        when a directory name lies."""
        from . import cred
        oa = (_read_json(os.path.join(home, ".claude.json")) or {}).get("oauthAccount") or {}
        email = cred.account_of(home)["email"]
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
        utilization = (round(percent / 100.0, 4)
                       if isinstance(percent, (int, float))
                       and not isinstance(percent, bool) else None)
        return {"label": label, "kind": kind, "utilization": utilization,
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
    def _util(gauge):
        """A gauge's MEASURED utilization, or None when it is unread.

        `_gauge` writes None for a limit the vendor sent with no percent
        (task/2935). None is a missing reading: every consumer asks this one
        question instead of doing arithmetic on the field, because a None
        compared or multiplied RAISES, and inside `_probe_one` that raise
        re-throws at `cred_state`'s `f.result()` and loses every sibling
        account's row (the task/2480 R5 class)."""
        value = (gauge or {}).get("utilization")
        return value if isinstance(value, (int, float)) \
            and not isinstance(value, bool) and math.isfinite(value) else None

    @classmethod
    def _read_state(cls, gauges):
        """(cred_state, status) for a set of gauges the vendor answered with.

        EXHAUSTED IS DECIDED BY MEASURED GAUGES ONLY: a measured plan gauge at
        100% is a wall whatever its unread siblings say, and an unread gauge
        is never a wall. A reading with NO measured plan gauge is not ok: it is
        `unknown`, and its status is outside the READ vocabulary
        (`allowed`/`blocked`), so the burn fold reads the account as unread."""
        measured = [cls._util(g) for g in gauges
                    if g.get("kind") != "overage" and cls._util(g) is not None]
        if any(u >= 0.999 for u in measured):
            return "exhausted", "blocked"
        if not measured:
            return "unknown", "unread (no plan gauge carried a utilization)"
        return "ok", "allowed"

    @classmethod
    def _headroom(cls, primary):
        util = cls._util(primary)
        return None if util is None else round(100 - util * 100, 1)

    @classmethod
    def _primary(cls, gauges):
        """The binding gauge: the account-wide session window, else the fullest gauge.

        AN UNREAD GAUGE IS NEVER THE LEAST FULL. Where no session gauge
        exists and any candidate is unread, the fullest gauge is unknowable:
        the unread one is returned, so the headroom read off it is None
        rather than the headroom of whichever gauge happened to be measured."""
        session = next((g for g in gauges
                        if g["kind"] == "session" and "-" not in g["label"]), None)
        if session or not gauges:
            return session
        unread = next((g for g in gauges if cls._util(g) is None), None)
        return unread or max(gauges, key=cls._util)

    @staticmethod
    def _best_copy(members):
        """The member whose own file carries the token most worth presenting:
        a present access token first, then one that has not expired, then
        the latest expiry. A token with no expiresAt is not expired, so it
        ranks above an expired one. Ties keep the first member, so codex
        groups and single homes are read as before."""
        def rank(a):
            oauth = _claude_oauth(a["home"]) if a["provider"] == "anthropic" else {}
            return (bool(oauth.get("accessToken")), _token_live(oauth),
                    _expires_ms(oauth) or 0)
        return max(members, key=rank)

    def _probe_one(self, acct):
        """-> (cred_state row, history row). Never raises; failure degrades the row."""

        def rows(state, status, gauges=(), tier=None, note=None, source_at=None,
                 binding=None, windows=None):
            gauges = list(gauges)
            # THE BINDING GAUGE IS THE CALLER'S WHEN IT HANDS ONE OVER.
            # `_primary` prefers the account-wide SESSION window, which is
            # right for anthropic and was exactly the codex weekly-pacing bug
            # (task/2480): a 7d window at 100% sitting under a 5h window at
            # 52% reported 48% headroom, and the fleet kept routing into a
            # wall. codex now hands the FULLEST account-wide gauge.
            primary = binding or self._primary(gauges)
            cred = {"account": acct["name"], "provider": acct["provider"], "cred_state": state,
                    "headroom_pct": self._headroom(primary),
                    "status": status,
                    "resets_at_ms": primary["reset"] * 1000 if primary and primary["reset"] else None,
                    "tier": tier or acct.get("tier"), "home": acct["home"], "source_at": source_at}
            if note:
                cred["note"] = note
            if windows:
                # EVERY WINDOW, not just the binding one. The owner could not
                # see the weekly filling because the row carried one number.
                cred["windows"] = list(windows)
            hist = {"provider": acct["provider"], "account": acct["name"], "probed_at": _iso_z(),
                    "status": status, "primary": primary["label"] if primary else None,
                    "gauges": gauges, "source_at": None}
            return cred, hist

        if acct["provider"] == "anthropic":
            oauth = _claude_oauth(acct["home"])
            token = oauth.get("accessToken")
            if not token:
                # AN EMPTY COPY ORCA CAN REFILL IS NOT A DEAD ACCOUNT, and no
                # vendor call was made, so it is not an api-error either. A
                # credhome can hold a blanked stub (accessToken "",
                # refreshToken "", expiresAt 0) while Orca holds the account's
                # live chain; "no-credentials" there reads as a lost login the
                # owner cannot act on. The cure is the sync the expired branch
                # names.
                refill = _orca_refill_status(acct["home"])
                if refill:
                    return rows("due-refresh", refill)
                return rows("api-error", "no-credentials")
            # PRESENT-BUT-EXPIRED IS ITS OWN STATE, NOT api-error (task/381).
            # Named `expired-token`, deliberately NOT `stale-token`: the prior
            # no-stale-cred-live-probe retired "stale" from cred design (a
            # snapshot-age heuristic that snuck in), and this is the opposite
            # — a HARD fact read from the token's own expiresAt, more precise
            # than "stale" and free of that word's baggage.
            # Measured 2026-08-06: four healthy Max 20x accounts read
            # "api-error" because helm's stored token was 201-401h (13-17 DAYS)
            # stale — orca does the live switching and refreshes its OWN store,
            # nothing refreshes helm's ~/.claude-homes copy. api-error reads as
            # "the account is broken"; the truth is "helm's COPY of the token
            # is dead", and the owner cannot act on the first framing. A dead
            # token also guarantees a 401, so returning here SKIPS a doomed
            # round-trip. expiresAt is epoch ms; absent -> fall through and let
            # the live call be the authority (never guess an account healthy).
            #
            # AND A DUE TOKEN IS NOT A DEAD ACCOUNT (task/2749). The paragraph
            # above is right that helm's COPY is dead and wrong about what the
            # owner owes for it. Three of six homes read "reauth-needed" while
            # every account was healthy and one `helm keepalive --apply` pass
            # rolled five of them forward to +8h: they were not spent, they
            # were OVERDUE — helm's one sanctioned credential writer was on no
            # cadence and only ran when somebody typed it. So the state splits
            # on the REFRESH chain, the thing that decides WHO can fix it: a
            # chain helm can still grant on is `due-refresh` and names
            # keepalive; a chain that is spent or absent keeps `expired-token`
            # and names the re-login.
            #
            # ONE PREDICATE SAYS "EXPIRED" HERE AND IN cred_state's MEMBER
            # LOOP. This branch once parsed expiresAt itself and let a bool
            # through (True is 1 ms past the epoch), while the loop's
            # _token_live did not: a sibling with `expiresAt: true` read ok and
            # eligible off the shared reading while its own probe said
            # expired-token. The token is present here, so not-live means its
            # expiresAt is a number in the past.
            if not _token_live(oauth):
                dead_s = time.time() - _expires_ms(oauth) / 1000
                chain = refresh_chain(oauth)
                # THE SENTENCE NEVER CONTRADICTS KEEPALIVE'S OWN LAST DECISION.
                # The chain is read from the file alone; keepalive also asked
                # the token endpoint and Orca's store. Where its newest recorded
                # outcome for this home is needs_reauth, the chain is spent
                # whatever the file's lifetime says, and the owner owes the
                # login; where it skipped because Orca holds the chain, the
                # cure is the Orca sync, not another keepalive pass.
                outcome = _last_outcome(acct["home"])
                if outcome and outcome["action"] == "needs_reauth":
                    return rows("expired-token",
                                "reauth-needed (helm's token expired %dd ago; "
                                "keepalive's last pass at %s by %s recorded "
                                "needs_reauth, so the refresh chain is spent "
                                "whatever the file says)"
                                % (int(dead_s / 86400), outcome["ts"],
                                   outcome["by"]))
                if chain in REFRESHABLE_CHAINS:
                    if outcome and outcome["orca"]:
                        return rows("due-refresh",
                                    "keepalive due (helm's token expired %s ago; "
                                    "Orca holds this home's refresh chain, so "
                                    "keepalive skips it) — `helm cred sync-orca "
                                    "--home %s --apply` refreshes it, no login "
                                    "needed; last keepalive pass %s by %s"
                                    % (_ago(dead_s),
                                       os.path.basename(acct["home"] or ""),
                                       outcome["ts"], outcome["by"]))
                    return rows("due-refresh",
                                _due_refresh_status(dead_s, chain, acct["home"]))
                return rows("expired-token",
                            "reauth-needed (helm's token expired %dd ago and %s; "
                            "orca refreshes its own store, not this one)"
                            % (int(dead_s / 86400), chain))
            try:
                data = self._get_json(ANTHROPIC_USAGE_URL, {"Authorization": "Bearer " + token})
            except urllib.error.HTTPError as e:
                e.close()  # an HTTPError IS a response object — close its fp deterministically
                return rows("api-error", "needs_reauth" if e.code in (401, 403) else f"http_{e.code}")
            except Exception:
                return rows("api-error", "network-error")
            gauges = self._anthropic_gauges(data)
            return rows(*self._read_state(gauges), gauges=gauges)

        # codex — THE POOL IS THE LIVE CREDENTIAL, NOT THE CODEX HOME (task/2480,
        # which closes task/2283 for this family). A codex CLI home is refreshed
        # only while codex is actually running in it, while the seat proxy
        # refreshes its OWN pooled copy — so a home's access token can be weeks
        # stale and match nothing the proxy serves. The vendor answers HTTP 401
        # "Could not parse your authentication token" for such a token at the
        # same instant it answers 200 for the pooled token of the SAME account,
        # so reading the home yields `needs_reauth` during a wall: true about
        # bytes nobody serves, and silent about the budget the fleet spends.
        #
        # The home stays the FALLBACK so an account that is not pooled still
        # gets probed exactly as before. Any failure -> "unknown" with a note,
        # never a provider failure.
        from . import codexbudget, codexhomes
        tokens = (_read_json(os.path.join(acct["home"], "auth.json")) or {}).get("tokens") or {}
        # A POOL THIS PASS COULD NOT READ IS THIS ACCOUNT'S UNKNOWN, AND
        # NOBODY ELSE'S (task/2480 R5). This function's contract is "never
        # raises; failure degrades the row", and `cred_state` relies on it
        # literally: it collects the probes through `concurrent.futures`, so
        # ONE escaping OSError rethrows at `f.result()` and destroys the whole
        # scorecard — every healthy Anthropic sibling's row, the history
        # append, the cache — over one unreadable directory belonging to one
        # family. Round 3's raise did exactly that. It is contained HERE, at
        # the one account the directory is about, as the `unknown` state this
        # row already has a vocabulary for.
        #
        # AND THE HOME FALLBACK IS NOT THE CURE. Falling through to the codex
        # home's own auth.json would "succeed" and print a number — the stale
        # bytes the pool exists to replace — for an account whose real
        # credential nobody could look at. An uncertain diagnostic dressed as
        # a reading is worse than the traceback it replaced.
        census = codexhomes.read_pool()
        if census.unknown:
            return rows("unknown", "pool-unread",
                        note="%s; this account's pooled credential could not "
                             "be looked at, so its budget is UNKNOWN — the "
                             "codex home's own token is NOT the one the seats "
                             "spend and is not read here" % census.error)
        # THE POOL LOOKUP MAY REFUSE, AND ITS REASON IS THE READER'S (task/2480).
        # `pool_record_for` answers (record, note): the note is set only for the
        # one case where a pooled row was FOUND by email and REJECTED because it
        # carries a different account id. Without it this row would read as an
        # ordinary not-pooled home fallback while a near-miss sat in the pool —
        # and the fallback would be the SECOND-best answer with nobody told.
        # The census read above is handed over, so one probe = one enumeration.
        #
        # THE HOME'S MEMBER HALVES GO WITH ITS ACCOUNT ID (task/2981). On a
        # Team plan the account id is the WORKSPACE, and every member carries
        # it. Joined on that id alone, every member read the first
        # member's file. The user id and the address are what name this
        # member. When the workspace is pooled and no file is proven to be
        # this member, the row is UNKNOWN. It does not read the home either:
        # the pool may hold this member under a spelling the join could not
        # prove, and the home's copy is the stale one (task/2480).
        ident_email = codexhomes._identity({"tokens": tokens})[0]
        match = codexbudget.pool_record_for(
            account_id=tokens.get("account_id"),
            email=acct.get("email") or ident_email,
            user_id=codexhomes._user_id({"tokens": tokens}), census=census)
        pooled, pool_note = match
        if match.unknown:
            return rows("unknown", POOL_MEMBER_UNKNOWN, note=pool_note)
        source = pooled or {"access_token": tokens.get("access_token"),
                            "account_id": tokens.get("account_id"),
                            "email": acct.get("email")}
        if not source.get("access_token"):
            return rows("unknown", "no-credentials",
                        note="neither the proxy pool nor auth.json has an access token"
                        if pooled is None else "the pooled record has no access token")
        row = codexbudget.probe_record(source, get_json=self._get_json)
        where = "the proxy pool (%s)" % pooled["file"] if pooled else \
            "the codex home — this account is NOT pooled (`helm codex pool <name>`)"
        if pool_note:
            where += "; " + pool_note
        if row["state"] == "unknown":
            return rows("unknown", row["status"],
                        note="%s; read from %s" % (row["note"], where))
        return rows("exhausted" if row["state"] == "exhausted" else "ok",
                    row["status"], row["gauges"],
                    tier=(row.get("plan") or "").title() or None,
                    # A SUCCESSFUL HOME READ STILL SAYS IT WAS A HOME READ. The
                    # first cut noted the source only on the failure path, so a
                    # row the pool does not cover looked identical to a pooled
                    # one — and the account the seats actually spend is the
                    # POOLED one, so "this number is not what your seats burn"
                    # is exactly the fact a reader needs.
                    note=None if pooled else "read from " + where,
                    binding=row["binding_gauge"], windows=row["windows"])

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
        reads = [self._best_copy(g) for g in members]
        # STAGGERED probes, 2 lanes max: tonight's 429s came from bursting all
        # identities at the vendor simultaneously (two predecessor lanes compounding it).
        # A ~0.7s stagger per submit keeps a full roster under any sane rate limit
        # while still overlapping network waits.
        results = [None] * len(members)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            futs = {}
            for i, g in enumerate(members):
                # THE IDENTITY IS READ THROUGH ITS BEST COPY, NOT ITS FIRST
                # HOME. When two homes carry one account and the one that
                # sorts first holds a blanked stub, probing it copied
                # no-credentials onto the sibling whose token reads the usage
                # endpoint fine. The name stays the group's first, so history
                # keeps one key per identity.
                futs[ex.submit(self._probe_one, dict(reads[i], name=g[0]["name"]))] = i
                time.sleep(0.7)
            for f in concurrent.futures.as_completed(futs):
                results[futs[f]] = f.result()
        creds, hists = [], []
        for accts, read, (cred, hist) in zip(members, reads, results):
            # `due-refresh` and `expired-token` are deliberately OUTSIDE this
            # door: both are read off the home's own file, not off a vendor
            # reply, so there is no throttle to look through and no older
            # observation that would be less wrong than the fact in hand.
            if cred["cred_state"] in ("api-error", "unknown") and self._transient(cred["status"] or ""):
                # a throttle (429/5xx) says nothing about the CRED — serve hours-old
                # truth marked stale rather than an api-error; source_at carries the age
                max_age = 6 * 3600 if "429" in (cred["status"] or "") else 900
                seen = self._last_observed(accts[0]["name"], max_age_s=max_age)
                if seen:  # throttled probe ≠ dead cred: serve the last observation, marked stale
                    gauges = seen["gauges"]
                    primary = self._primary(gauges)
                    cred.update(cred_state=self._read_state(gauges)[0],
                                headroom_pct=self._headroom(primary),
                                resets_at_ms=primary["reset"] * 1000 if primary["reset"] else None,
                                source_at=seen["probed_at"],
                                note=f"usage endpoint {cred['status']} — showing last observation")
                    hist = None  # not a new observation; don't fake a history point
            if hist is not None:
                hists.append(hist)
            for a in accts:
                # A SHARED READING NEVER VOUCHES FOR A HOME'S OWN DEAD COPY.
                # The row is per HOME, and allocate() places seats by it: a
                # stub that inherited its sibling's `ok` ranked ahead of the
                # live home and launched a "Not logged in" pane. An EXPIRED
                # copy opens the same pane (its token still 401s). So a
                # member whose own file holds no access token, or whose
                # expiresAt is past, keeps its OWN state, read by the same
                # probe (both branches return before any vendor call). Its
                # history row is dropped: the identity keeps one key, and
                # this home may carry that very name.
                own = _claude_oauth(a["home"]) if a["provider"] == "anthropic" else {}
                if (a["home"] != read["home"] and a["provider"] == "anthropic"
                        and not _token_live(own)):
                    row, _ = self._probe_one(a)
                    theirs = _claude_oauth(read["home"])
                    if theirs.get("accessToken"):
                        mine = ("has expired" if own.get("accessToken")
                                else "holds no access token")
                        via = os.path.basename(read["home"])
                        # THE NOTE CLAIMS A READ-THROUGH ONLY WHEN THERE IS
                        # ONE. _best_copy ranks a live copy above every dead
                        # one, so a read copy that is not live means no copy
                        # of the account is, and "the account itself reads
                        # due-refresh through one" would name a dead copy as
                        # the account's reading.
                        row["note"] = (
                            "this home's own copy %s; the account itself "
                            "reads %s through %s" % (mine, cred["cred_state"], via)
                            if _token_live(theirs) else
                            "this home's own copy %s, and no copy of this "
                            "account is live; the freshest, %s, reads %s"
                            % (mine, via, cred["cred_state"]))
                else:
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
                    # An unread session sample is no sample: its peak is
                    # unknown, and a None among the cycle's numbers raises
                    # in `_cost_per_window`'s max.
                    if s and s.get("reset") and self._util(s) is not None:
                        w = self._account_wide(r["gauges"], "period")
                        a["cycles"].setdefault(s["reset"], []).append(
                            (self._util(s), self._util(w),
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
          verdict      = "no-weekly" (no weekly gauge, reset or reading; an
                         unread weekly is not a weekly at 0%)
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
            if not weekly or not weekly.get("reset") or self._util(weekly) is None:
                row.update(windows_left=0.0, windows_fit=0.0, verdict="no-weekly")
            else:
                W = max(0.0, 1.0 - self._util(weekly))
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
            elif state == "due-refresh":
                # BLOCKED, BECAUSE A DEAD ACCESS TOKEN STILL 401s. The split
                # changed who must act, not whether the seat can launch on it
                # right now — routing a seat onto an unrefreshed home is the
                # "Not logged in" pane, same as before.
                # The REASON is the state, not the sentence: the status here is
                # a paragraph naming the cure verb and the last cadence run,
                # and blocked_by is a list of short reasons a ranker prints.
                blocked.append("due-refresh")
            elif state in ("api-error", "expired-token"):
                blocked.append(s.get("status") or state)
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
        Path: env override, else ~/.config/helm/, else a declared
        predecessor's file."""
        helm_p = os.path.expanduser("~/.config/helm/allocation.json")
        path = _env("ALLOCATION_RULES") or (
            helm_p if os.path.exists(helm_p)
            else localnames.legacy_path(".config", "{}", "allocation.json")
            or helm_p)
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
                if not weekly or not weekly.get("reset") or self._util(weekly) is None:
                    continue  # an unread weekly is no remaining budget to drain
                left_h = (weekly["reset"] - now) / 3600.0
                remaining = 1.0 - self._util(weekly)
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
    HELM_PROVIDER=cli + HELM_QUOTA_CLI naming the binary (required — the
    shipped tree bakes no default binary); a declared predecessor's spellings
    still work."""
    choice = (_env("PROVIDER") or "native").strip().lower()
    if choice == "cli":
        binary = _env("QUOTA_CLI", "")
        if binary and shutil.which(binary):
            return CliQuotaProvider(binary)
        # opted into a CLI that is unset or not installed: fail toward working,
        # loudly (which binary to shell is a site choice, not shipped code's)
        why = f"{binary!r} not found" if binary else "HELM_QUOTA_CLI is unset"
        print(f"helm: HELM_PROVIDER=cli but {why} — using native provider",
              file=sys.stderr)
    return NativeQuotaProvider()
