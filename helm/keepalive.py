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

from . import catalog, cred

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
            out.append((pid, "keepalive"))   # never return argv: it may carry pasted secrets
    return out


def _read_oauth(cred_path):
    blob, _ = cred._read_regular(cred_path)
    val = json.loads(blob.decode("utf-8"))
    return val, val.get("claudeAiOauth") or {}


def refresh_home(home_path, early_horizon_s=60, force=False, apply=False):
    """Plan or refresh one claude home's token. DRY-RUN is the default and
    performs no network call, log write, or filesystem mutation. apply=True
    snapshots the exact pre-image BEFORE the rotating grant and refuses if that
    capture fails."""
    home_path = os.path.realpath(os.path.expanduser(home_path))
    raw_name = os.path.basename(home_path)
    name = cred._display_path(raw_name)
    account = cred.account_of(home_path)["email"]

    def rec(event):
        row = {"home": name, "account": account, **event}
        return _log(row) if apply else row

    cred_path = os.path.join(home_path, ".credentials.json")
    if not os.path.exists(cred_path):
        return rec({"action": "skip", "reason": "no credentials file"})
    pid = _live_holder_pid(home_path)
    if pid:
        return rec({"action": "skip",
                    "reason": "live holder detected — one refresher per home"})
    try:
        val, oauth = _read_oauth(cred_path)
    except (OSError, ValueError) as e:
        return rec({"action": "error",
                    "reason": "unreadable cred file (%s)" % e.__class__.__name__})
    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        return rec({"action": "needs_reauth", "reason": "no refreshToken present"})
    expires_at = oauth.get("expiresAt")
    now_ms = int(time.time() * 1000)
    due = force or expires_at is None or expires_at <= now_ms + early_horizon_s * 1000
    if not due:
        return rec({"action": "skip",
                    "reason": "not due (expires in %d min)"
                              % ((expires_at - now_ms) // 60000)})
    if not apply:
        return rec({"action": "would-refresh",
                    "reason": "due; add --apply (no network or filesystem changes)"})

    # Capture before the grant. The endpoint rotates the refresh token, so
    # taking the snapshot afterwards is already too late if storage is broken.
    # This is also the pre-rotation lineage census: cred.backup records the
    # about-to-be-rotated family live in THIS home (even on the identical-skip
    # path), so a byte-copy borrowed elsewhere can never be auto-restored
    # after this grant consumes it — no fleet turn boundary required.
    snap = cred.backup(home_path, apply=True)
    if not snap["ok"]:
        return rec({"action": "error",
                    "reason": "pre-image capture failed — refresh refused"})
    pid = _live_holder_pid(home_path)
    if pid:
        return rec({"action": "skip",
                    "reason": "live holder arrived after pre-image capture — refused"})

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
        e.close()
        return rec({"action": "needs_reauth",
                    "reason": "refresh HTTP %s — one-time re-login needed" % e.code})
    except Exception as e:
        return rec({"action": "error",
                    "reason": "refresh transport failed (%s)" % e.__class__.__name__})
    new_access = j.get("access_token")
    if not new_access:
        return rec({"action": "error", "reason": "refresh response had no access_token"})
    expires_in = j.get("expires_in") or 28800
    oauth["accessToken"] = new_access
    if j.get("refresh_token"):
        oauth["refreshToken"] = j["refresh_token"]
    oauth["expiresAt"] = int(time.time() * 1000) + expires_in * 1000
    val["claudeAiOauth"] = oauth
    try:
        cred._atomic_private(cred_path, json.dumps(val, indent=2).encode(), 0o600)
    except OSError as e:
        return rec({"action": "error",
                    "reason": "write-back failed (%s)" % e.__class__.__name__})
    return rec({"action": "refreshed",
                "rotated_refresh_token": bool(j.get("refresh_token")),
                "new_expiry_in_h": round(expires_in / 3600, 1),
                "pre_image": cred._display_path(snap.get("dest"))})


def _sweep_rows(early_h, apply):
    results = []
    homes = sorted(set(os.path.realpath(p) for p in glob.glob(f"{CLAUDE_HOMES_ROOT}/*")
                       if os.path.isdir(p)))
    for hp in homes:
        results.append(refresh_home(hp, early_horizon_s=early_h * 3600,
                                    apply=apply))
    for hp in sorted(set(os.path.realpath(p) for p in glob.glob(f"{CODEX_HOMES_ROOT}/*")
                         if os.path.isdir(p))):
        ap = os.path.join(hp, "auth.json")
        if not os.path.exists(ap):
            continue
        age_h = (time.time() - os.path.getmtime(ap)) / 3600
        if age_h > 20:
            row = {"home": cred._display_path(os.path.basename(hp)),
                   "provider": "codex", "action": "stale-risk",
                   "reason": "auth.json untouched %.0fh — run its codex CLI "
                             "(self-refreshes on launch); helm never writes codex creds" % age_h}
            results.append(_log(row) if apply else row)
    return results


def sweep(early_h=24, apply=False):
    """Dry-run scans without even creating a lock file. Applied sweeps take the
    machine-wide writer lock before any backup/network/write."""
    if not apply:
        return _sweep_rows(early_h, False)
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    with open(LOCK_PATH, "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return [{"action": "skip", "reason": "another keepalive sweep is running"}]
        return _sweep_rows(early_h, True)


def _cmd_keepalive(args):
    """keepalive [--home H] [--early N] [--apply] — roll idle claude homes' OAuth tokens
    forward before their refresh chains rot (full sweep by default; codex is
    read-only by design). The ONE credential-writing verb.
    Log: ~/.cache/helm/keepalive-log.jsonl"""
    args = list(args or [])
    home = early = None
    apply = False
    while args:
        a = args.pop(0)
        if a == "--apply":
            apply = True
        elif a == "--home" and args:
            home = args.pop(0)
        elif a == "--early" and args:
            early = args.pop(0)
        else:
            print("usage: helm keepalive [--home NAME|PATH] [--early HOURS] [--apply]",
                  file=sys.stderr)
            return 2
    try:
        early = float(early) if early is not None else 24.0
    except ValueError:
        print("helm keepalive: --early must be a number of hours", file=sys.stderr)
        return 2
    # one credential writer machine-wide: refuse to run beside a live keepalive
    # (the predecessor's copy uses a different lock file our sweep can't see).
    live = _predecessor_pids() if apply else []
    if live:
        print("helm keepalive: REFUSED — a keepalive process is already running:",
              file=sys.stderr)
        for pid, _ in live:
            print("  pid %s" % pid, file=sys.stderr)
        print("  one credential writer per machine; let it finish (or stop it) first.",
              file=sys.stderr)
        return 1
    if home:
        path = home if "/" in home else os.path.join(CLAUDE_HOMES_ROOT, home)
        results = [refresh_home(path, early_horizon_s=int(early * 3600),
                                apply=apply)]
    else:
        results = sweep(early_h=early, apply=apply)
    acted = sum(1 for r in results if r.get("action") == "refreshed")
    planned = sum(1 for r in results if r.get("action") == "would-refresh")
    print("helm keepalive (%s): %d home%s checked, %d refreshed%s" % (
        "APPLIED" if apply else "dry-run — add --apply",
        len(results), "s"[:len(results) != 1], acted,
        " (%d would refresh)" % planned if not apply else ""))
    worst = 0
    for r in results:
        print("  " + json.dumps(r))
        if r.get("action") == "error":
            worst = 1
    return worst


def cmd_keepalive(args):
    """keepalive [--home H] [--early N] [--apply] — dry-run by default."""
    try:
        return _cmd_keepalive(args)
    except Exception as e:
        print("helm keepalive: operation failed (%s)" % e.__class__.__name__,
              file=sys.stderr)
        return 1
