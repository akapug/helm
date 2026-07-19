#!/usr/bin/env python3
"""Idle-home keepalive: refresh a claude credential home's OAuth token before its
refresh chain rots — the ONE place in helm that writes credential files.
ABSORBED from the predecessor (sesh/server/keepalive.py), behavior-preserving,
every hard-won rule intact (no live runtime consumer existed at absorption —
verified by /proc scan; a leftover predecessor sweep is still detected and
`helm keepalive` refuses to run beside it):

  * ROTATION MUST PERSIST. The token endpoint rotates the refresh_token on every
    grant; failing to write the new pair back BURNS the account's refresh
    capability permanently (learned the hard way upstream).
  * ONE LIVE REFRESHER PER HOME. A live agent process on a home refreshes its own
    credentials; a second writer is the documented upstream race (#24317). We
    refresh IDLE homes only — a live holder means SKIP, loudly, never write.
  * OWNER-ONLY FROM CREATION. The temp file is opened 0o600 before any token
    byte lands in it; write + atomic rename. Never chmod-after-write.
  * REFRESH ONLY WHEN DUE. expiresAt within the horizon (default 60s, i.e.
    expired/imminent) — or the keepalive sweep's early horizon (refresh anything
    expiring within 24h) so idle homes roll forward before they rot.
  * CODEX IS READ-ONLY EVERYWHERE. The prior implementation never refresh-wrote
    codex either (one family = one writer; codex's own CLI rotates on launch).
    Full parity therefore requires NO codex write. Idle codex homes are surfaced
    as `stale-risk` for a human-run `codex login status`/launch instead.

Every action (refresh, skip, refusal, failure) is appended to
~/.cache/helm/keepalive-log.jsonl — token VALUES never appear anywhere.
(catalog.CACHE_DIR seeds itself once from the legacy ~/.cache/sesh/, so the
predecessor's log history carries over; new writes are helm-only.)
"""
import fcntl
import glob
import json
import os
import sys
import time
import urllib.request

from . import catalog

OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_UA = "claude-cli/2.1.197 (external, cli)"
HOME = os.path.expanduser("~")
LOG_PATH = os.path.join(catalog.CACHE_DIR, "keepalive-log.jsonl")
LOCK_PATH = os.path.join(catalog.CACHE_DIR, "keepalive.lock")
CLAUDE_HOMES_ROOT = os.path.join(HOME, ".claude-homes")
CODEX_HOMES_ROOT = os.path.join(HOME, ".codex-homes")


def _log(event):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **event}
    with open(LOG_PATH, "a") as fh:
        fh.write(json.dumps(event) + "\n")
    return event


def _live_holder_pid(home_path):
    """Pid of a live agent whose CLAUDE_CONFIG_DIR resolves to this home (or a
    default-home agent with no env when home IS ~/.claude). comm-filtered: only
    the agent binary counts, not inherited children."""
    target = os.path.realpath(home_path)
    is_default = target == os.path.realpath(f"{HOME}/.claude")
    for envf in glob.glob("/proc/[0-9]*/environ"):
        pid = envf.split("/")[2]
        try:
            with open(f"/proc/{pid}/comm") as fh:
                if fh.read().strip() != "claude":
                    continue
            with open(envf, "rb") as fh:
                env = fh.read()
        except OSError:
            continue
        homes = [v.split(b"=", 1)[1].decode() for v in env.split(b"\0")
                 if v.startswith(b"CLAUDE_CONFIG_DIR=")]
        if homes:
            if any(os.path.realpath(os.path.expanduser(h)) == target for h in homes):
                return pid
        elif is_default:
            return pid
    return None


def _predecessor_pids():
    """Live predecessor keepalive processes (the sesh copy uses a DIFFERENT lock
    file, so our sweep lock can't see it — detect it directly and refuse to run
    a second credential writer beside it)."""
    me = str(os.getpid())
    out = []
    for f in glob.glob("/proc/[0-9]*/cmdline"):
        pid = f.split("/")[2]
        if pid == me:
            continue
        try:
            with open(f, "rb") as fh:
                cmdline = fh.read().decode(errors="ignore")
        except OSError:
            continue
        argv0 = cmdline.split("\0", 1)[0]
        if "python" not in os.path.basename(argv0):
            continue
        if "keepalive" in cmdline and ("sesh" in cmdline or "helm" in cmdline):
            out.append((pid, cmdline.replace("\0", " ").strip()))
    return out


def _read_oauth(cred_path):
    with open(cred_path) as fh:
        val = json.load(fh)
    return val, val.get("claudeAiOauth") or {}


def refresh_home(home_path, early_horizon_s=60, force=False):
    """Refresh one claude home's token if due. Returns a JSON-able result dict;
    every outcome is logged. NEVER touches a home with a live holder."""
    home_path = os.path.realpath(os.path.expanduser(home_path))
    name = os.path.basename(home_path)
    cred_path = os.path.join(home_path, ".credentials.json")
    if not os.path.exists(cred_path):
        return _log({"home": name, "action": "skip", "reason": "no credentials file"})

    pid = _live_holder_pid(home_path)
    if pid:
        return _log({"home": name, "action": "skip",
                     "reason": f"live holder pid {pid} — one refresher per home, the agent owns it"})

    try:
        val, oauth = _read_oauth(cred_path)
    except (OSError, ValueError) as e:
        return _log({"home": name, "action": "error", "reason": f"unreadable cred file: {e}"})
    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        return _log({"home": name, "action": "needs_reauth", "reason": "no refreshToken present"})

    expires_at = oauth.get("expiresAt")  # ms epoch
    now_ms = int(time.time() * 1000)
    due = force or expires_at is None or expires_at <= now_ms + early_horizon_s * 1000
    if not due:
        return _log({"home": name, "action": "skip",
                     "reason": f"not due (expires in {(expires_at - now_ms) // 60000} min)"})

    # ---- the refresh grant (rotation!) ----
    body = json.dumps({"grant_type": "refresh_token",
                       "refresh_token": refresh_token,
                       "client_id": OAUTH_CLIENT_ID}).encode()
    req = urllib.request.Request(OAUTH_TOKEN_URL, data=body, method="POST",
                                 headers={"content-type": "application/json",
                                          "user-agent": CLAUDE_UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            j = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        e.close()  # an HTTPError IS a response object — close its fp deterministically
        return _log({"home": name, "action": "needs_reauth",
                     "reason": f"refresh HTTP {e.code} — refresh token dead, one-time re-login needed"})
    except Exception as e:
        return _log({"home": name, "action": "error", "reason": f"refresh transport: {e}"})

    new_access = j.get("access_token")
    if not new_access:
        return _log({"home": name, "action": "error", "reason": "refresh response had no access_token"})
    expires_in = j.get("expires_in") or 28800

    # ---- persist the rotation (REQUIRED — else the family burns) ----
    oauth["accessToken"] = new_access
    if j.get("refresh_token"):
        oauth["refreshToken"] = j["refresh_token"]
    oauth["expiresAt"] = int(time.time() * 1000) + expires_in * 1000
    val["claudeAiOauth"] = oauth
    tmp = f"{cred_path}.helm-tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # owner-only FROM CREATION
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(val, fh, indent=2)
        os.replace(tmp, cred_path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return _log({"home": name, "action": "error", "reason": f"write-back failed: {e}"})
    return _log({"home": name, "action": "refreshed",
                 "rotated_refresh_token": bool(j.get("refresh_token")),
                 "new_expiry_in_h": round(expires_in / 3600, 1)})


def sweep(early_h=24):
    """The keepalive pass: every idle claude home whose token expires within
    early_h hours gets rolled forward; codex homes get a stale-risk read-only
    check. One sweep at a time machine-wide (file lock)."""
    lockp = LOCK_PATH
    os.makedirs(os.path.dirname(lockp), exist_ok=True)
    results = []
    with open(lockp, "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return [{"action": "skip", "reason": "another keepalive sweep is running"}]
        homes = sorted(set(os.path.realpath(p) for p in glob.glob(f"{CLAUDE_HOMES_ROOT}/*")
                           if os.path.isdir(p)))
        for hp in homes:
            results.append(refresh_home(hp, early_horizon_s=early_h * 3600))
        # codex: read-only stale-risk surfacing (never write; the codex CLI owns rotation)
        for hp in sorted(set(os.path.realpath(p) for p in glob.glob(f"{CODEX_HOMES_ROOT}/*")
                             if os.path.isdir(p))):
            ap = os.path.join(hp, "auth.json")
            if not os.path.exists(ap):
                continue
            age_h = (time.time() - os.path.getmtime(ap)) / 3600
            if age_h > 20:
                results.append(_log({"home": os.path.basename(hp), "provider": "codex",
                                     "action": "stale-risk",
                                     "reason": f"auth.json untouched {age_h:.0f}h — run its codex CLI "
                                               f"(self-refreshes on launch); helm never writes codex creds"}))
    return results


def cmd_keepalive(args):
    """keepalive [--home H] [--early N] — roll idle claude homes' OAuth tokens
    forward before their refresh chains rot (full sweep by default; codex is
    read-only by design). The ONE credential-writing verb.
    Log: ~/.cache/helm/keepalive-log.jsonl"""
    args = list(args or [])
    home = early = None
    while args:
        a = args.pop(0)
        if a == "--home" and args:
            home = args.pop(0)
        elif a == "--early" and args:
            early = args.pop(0)
        else:
            print("usage: helm keepalive [--home NAME|PATH] [--early HOURS]",
                  file=sys.stderr)
            return 2
    try:
        early = float(early) if early is not None else 24.0
    except ValueError:
        print("helm keepalive: --early must be a number of hours", file=sys.stderr)
        return 2
    # one credential writer machine-wide: refuse to run beside a live keepalive
    # (the predecessor's copy uses a different lock file our sweep can't see).
    live = _predecessor_pids()
    if live:
        print("helm keepalive: REFUSED — a keepalive process is already running:",
              file=sys.stderr)
        for pid, cmdline in live:
            print("  pid %s: %s" % (pid, cmdline[:160]), file=sys.stderr)
        print("  one credential writer per machine; let it finish (or stop it) first.",
              file=sys.stderr)
        return 1
    if home:
        path = home if "/" in home else os.path.join(CLAUDE_HOMES_ROOT, home)
        results = [refresh_home(path, early_horizon_s=int(early * 3600))]
    else:
        results = sweep(early_h=early)
    acted = sum(1 for r in results if r.get("action") == "refreshed")
    print("helm keepalive: %d home%s checked, %d refreshed" % (
        len(results), "s"[:len(results) != 1], acted))
    worst = 0
    for r in results:
        print("  " + json.dumps(r))
        if r.get("action") == "error":
            worst = 1
    return worst
