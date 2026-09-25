#!/usr/bin/env python3
"""Idle-home keepalive: refresh a claude credential home's OAuth token before its
refresh chain rots — the ONE place in helm that writes credential files.
ABSORBED from the predecessor (its server/keepalive.py), behavior-preserving,
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
  * ORCA'S CHAIN IS NOT OURS. A home that shares its refresh family with
    Orca's managed copy of the account, or is stale against it, is skipped
    (_orca_holds_the_chain): one refresher per token family, and that
    refresher is Orca.
  * THE CLIENT IDENTITY IS THE INSTALLED CLI'S, NEVER SHIPPED. The grant
    names an OAuth client id, and the request carries a user agent; both
    belong to the Claude Code CLI that minted the token, so both are read out
    of that CLI's installed executable when a grant is about to be made
    (`cli_identity`). A CLI that cannot be found or read refuses the grant
    and says why: helm never guesses an identity and carries none of its own.
  * CODEX IS READ-ONLY EVERYWHERE. The prior implementation never refresh-wrote
    codex either (one family = one writer; codex's own CLI rotates on launch).
    Full parity therefore requires NO codex write. Idle codex homes are surfaced
    as `stale-risk` for a human-run `codex login status`/launch instead.

Every action (refresh, skip, refusal, failure) is appended to
$HELM_CACHE_DIR/keepalive-log.jsonl (default ~/.cache/helm) — token VALUES never
appear anywhere.
(catalog.CACHE_DIR seeds itself once from a declared predecessor's cache, so
the predecessor's log history carries over; new writes are helm-only.)
"""
import contextlib
import fcntl
import glob
import json
import mmap
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

from . import catalog, cred, home, localnames

OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
HOME = os.path.expanduser("~")
CLAUDE_HOMES_ROOT = os.path.join(HOME, ".claude-homes")
CODEX_HOMES_ROOT = os.path.join(HOME, ".codex-homes")


def _cache_path(name):
    return os.path.join(home.env("CACHE_DIR") or catalog.CACHE_DIR, name)


def _log_path():
    return _cache_path("keepalive-log.jsonl")


def _actor():
    """WHO ran this pass: the cadence's own seat identity (the timer unit sets
    HELM_CHAT_NAME=keepalive-cron) or "hand" for a human-typed run. A log that
    cannot say whether a loop or a person did the last refresh cannot answer
    the question a stale token raises — is the loop turning? — which is the
    question the quota page and `helm doctor` now put to this file."""
    try:
        return home.chat_name() or "hand"
    except Exception:                      # noqa: BLE001 — a hostile seat name
        return "hand"                      # must not take the log write down


def _log(event):
    path = _log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "by": _actor(), **event}
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with open(path, "a") as fh:
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
    """Live keepalive processes: helm's, and a declared predecessor's (whose
    copy uses a DIFFERENT lock file, so our sweep lock can't see it — detect
    it directly and refuse to run a second credential writer beside it)."""
    me = str(os.getpid())
    names = ("helm",) + tuple(n for n in (localnames.predecessor(),) if n)
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
        if "keepalive" in cmdline and any(n in cmdline for n in names):
            out.append((pid, "keepalive"))   # never return argv: it may carry pasted secrets
    return out


# ---------------------------------------------------------------------------
# the client identity — read from the installed CLI, never shipped
# ---------------------------------------------------------------------------

#: The Claude Code native installer keeps one executable per version here,
#: and the newest by version is the one that runs. HELM_CLAUDE_CLI names an
#: executable instead (an npm install's cli.js reads the same way).
CLI_VERSIONS_DIR = os.path.join(HOME, ".local", "share", "claude", "versions")
CLI_ENV = "HELM_CLAUDE_CLI"

_UUID = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
#: The tail of the CLI's user-agent template, matched at a found anchor:
#: `}.VERSION} (<user type>, ${<entrypoint expr>??"<default>"}`.
_UA_TAIL = re.compile(rb'\}\.VERSION\} \(([a-z]{1,24}), \$\{[A-Za-z0-9_.$]{1,64}'
                      rb'\?\?"([a-z][a-z0-9-]{0,23})"\}')
#: Its head: return`<product>/${{ — the build inlines the constants object.
_UA_HEAD = re.compile(rb"return`([a-z][a-z0-9-]{0,31})/\$\{\{")
_UA_VERSION = re.compile(rb'VERSION:"(\d{1,4}\.\d{1,4}\.\d{1,6})"')
_UA_SPAN = 4096        # the inlined constants object measured ~500 bytes
_ID_SPAN = 512         # CLIENT_ID to OAUTH_FILE_SUFFIX measured ~100 bytes

_identity_cache = {}

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_identity_cache": (
        "keyed by CLI path, size and mtime; a changed binary misses"),
}


def _version_key(name):
    return tuple(int(p) if p.isdigit() else -1 for p in name.split("."))


def installed_cli():
    """(path, None) for the CLI executable to read, or (None, why)."""
    named = os.environ.get(CLI_ENV)
    if named:
        path = os.path.expanduser(named)
        return (path, None) if os.path.isfile(path) else \
            (None, "%s names %s, which is not a file" % (CLI_ENV, path))
    try:
        names = [n for n in os.listdir(CLI_VERSIONS_DIR)
                 if os.path.isfile(os.path.join(CLI_VERSIONS_DIR, n))]
    except OSError:
        names = []
    if not names:
        return None, ("no Claude Code CLI is installed under %s (set %s to "
                      "its executable)" % (CLI_VERSIONS_DIR, CLI_ENV))
    return os.path.join(CLI_VERSIONS_DIR, max(names, key=_version_key)), None


def _client_ids(mm):
    """Every distinct OAuth client id whose config object stores its tokens
    in the file with NO suffix — `.credentials.json`, the file this module
    refreshes. The CLI carries one such object per environment; the others
    name a suffix (`-local-oauth`) and are not this file's."""
    found, i = set(), 0
    while True:
        i = _key_at(mm, b'CLIENT_ID:"', i)
        if i < 0:
            return found
        i += 11
        cid = mm[i:i + 36]
        if not _UUID.match(cid) or mm[i + 36:i + 37] != b'"':
            continue
        # the suffix must belong to THIS object: no other CLIENT_ID key
        # stands between the id and it
        suf = _key_at(mm, b"OAUTH_FILE_SUFFIX:", i + 37, i + 37 + _ID_SPAN)
        if suf < 0 or 0 <= _key_at(mm, b'CLIENT_ID:"', i + 37, suf):
            continue
        if mm[suf + 18:suf + 20] == b'""':
            found.add(cid.decode("ascii"))


def _key_at(mm, key, start, end=None):
    """The next offset of `key` used as an object KEY — not the tail of a
    longer identifier (DESIGN_CLIENT_ID) — at or after `start`, else -1."""
    end = len(mm) if end is None else min(end, len(mm))
    while True:
        at = mm.find(key, start, end)
        if at <= 0:
            return at
        prev = mm[at - 1:at]
        if not (prev.isalnum() or prev in (b"_", b"$")):
            return at
        start = at + 1


def _user_agents(mm):
    """Every distinct user agent the CLI's own template would send with no
    agent SDK, client app or workload set: `<product>/<version> (<user
    type>, <default entrypoint>)`."""
    found, k = set(), 0
    while True:
        k = mm.find(b"}.VERSION} (", k)
        if k < 0:
            return found
        tail = _UA_TAIL.match(mm[k:k + 160])
        head_at = mm.rfind(b"return`", max(0, k - _UA_SPAN), k)
        k += 1
        if not tail or head_at < 0:
            continue
        head = _UA_HEAD.match(mm[head_at:head_at + 64])
        versions = set(_UA_VERSION.findall(mm[head_at:k]))
        if not head or len(versions) != 1:
            continue
        found.add("%s/%s (%s, %s)" % (
            head.group(1).decode(), versions.pop().decode(),
            tail.group(1).decode(), tail.group(2).decode()))


def cli_identity():
    """({"client_id", "user_agent", "cli"}, None), or (None, why).

    THE IDENTITY IS READ, NEVER GUESSED. Each field must be found exactly
    once in the installed executable; none found, or two that disagree, is a
    refusal that names the file, and no default stands in for it. The read is
    two byte searches over a memory map, cached per executable (path, size,
    mtime), so a sweep reads the CLI once."""
    path, why = installed_cli()
    if why:
        return None, why
    try:
        st = os.stat(path)
    except OSError as e:
        return None, "cannot stat %s (%s)" % (path, e.__class__.__name__)
    key = (path, st.st_size, st.st_mtime_ns)
    if key not in _identity_cache:
        try:
            with open(path, "rb") as fh, \
                    mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                ids, agents = _client_ids(mm), _user_agents(mm)
        except (OSError, ValueError) as e:
            return None, "cannot read %s (%s)" % (path, e.__class__.__name__)
        if len(ids) != 1:
            got = (None, "%s carries %s OAuth client id for .credentials.json; "
                   "refusing to guess one" % (path, len(ids) or "no"))
        elif len(agents) != 1:
            got = (None, "%s carries %s user-agent template; refusing to guess "
                   "one" % (path, len(agents) or "no"))
        else:
            got = ({"client_id": ids.pop(), "user_agent": agents.pop(),
                    "cli": path}, None)
        _identity_cache.clear()
        _identity_cache[key] = got
    return _identity_cache[key]


def _read_oauth(cred_path):
    blob, _ = cred._read_regular(cred_path)
    val = json.loads(blob.decode("utf-8"))
    return val, val.get("claudeAiOauth") or {}


def _orca_holds_the_chain(home_path):
    """Why this home must not be granted on because Orca's managed copy of the
    same account owns its refresh chain, else None. Two shapes, both measured
    by cred.freshness (no token byte leaves it):
      * SAME FAMILY — the home carries the refresh token Orca holds (a launch
        synced it from Orca). One refresh token, two holders: a helm grant
        rotates it and burns Orca's copy. Orca is that family's refresher; once
        Orca refreshes it the home reads CHAIN-UNPROVEN, and only the operator's
        `helm cred sync-orca --replace-own-chain` or a fresh login recovers it.
      * STALE AGAINST ORCA — Orca's copy is strictly fresher AND the home's
        own chain is provably spent (no token or refresh token, or a refresh
        lifetime already past); keepalive would present a dead token.
      * CHAIN-UNPROVEN — Orca is fresher and the home's token may be that
        rotated-away copy (lifetimes within the refresh jitter, or one
        missing): the grant might be the reuse.
    Anything else — no Orca copy, an unreadable store, a home on its own login
    chain (OWN-CHAIN, whether ahead of or behind Orca) — keeps keepalive's
    behaviour exactly as before: missing evidence is not evidence against a
    grant."""
    try:
        fr = cred.freshness(home_path)
    except Exception:
        return None
    if fr.get("same_family"):
        return ("shares its refresh-token family with Orca's managed copy of %s "
                "— Orca refreshes that chain; a helm grant would burn Orca's copy "
                "(once Orca refreshes it, this home reads CHAIN-UNPROVEN; %s)"
                % (fr["account"], cred._cure(home_path)))
    if fr.get("verdict") == cred.UNPROVEN:
        return ("cannot be told apart from a copy of Orca's managed %s whose refresh "
                "token Orca rotated away, and presenting that token risks revoking "
                "Orca's live family; %s" % (fr["account"], cred._cure(home_path)))
    if fr.get("verdict") == cred.STALE:
        return ("is stale against Orca's managed copy of %s — its own chain is "
                "spent (%s), so a grant would present a dead token; %s"
                % (fr["account"], fr.get("chain") or "unknown",
                   "`helm cred sync-orca --home %s --apply` instead"
                   % cred._display_path(os.path.basename(home_path))
                   if cred.is_credhome(home_path)
                   else "log in fresh in it: %s" % cred._login_cmd(home_path)))
    return None


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
    orca = _orca_holds_the_chain(home_path)
    if orca:
        return rec({"action": "skip", "reason": "home " + orca})
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
    # THE IDENTITY BEFORE THE CAPTURE: a grant that cannot name its client
    # makes no pre-image, no request and no write.
    ident, why = cli_identity()
    if why:
        return rec({"action": "error",
                    "reason": "refresh refused: the installed Claude Code CLI's "
                              "client identity could not be read — " + why})

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
    orca = _orca_holds_the_chain(home_path)
    if orca:
        # re-asked after the capture: a launch may have synced the home from
        # Orca between the plan and the grant
        return rec({"action": "skip",
                    "reason": "home %s (measured after pre-image capture)" % orca})

    body = json.dumps({"grant_type": "refresh_token",
                       "refresh_token": refresh_token,
                       "client_id": ident["client_id"]}).encode()
    req = urllib.request.Request(OAUTH_TOKEN_URL, data=body, method="POST",
                                 headers={"content-type": "application/json",
                                          "user-agent": ident["user_agent"]})
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


def last_outcome(home_name, tail_bytes=262144):
    """The most recent row of ANY action this log holds for one home ->
    {"ts", "action", "by", "orca"} or None. `last_refresh` answers "when did
    the loop last succeed"; this answers "what did the loop last DECIDE", which
    is what a status sentence must agree with: a home whose newest row is
    needs_reauth is one keepalive has already given up on, and a sentence that
    still names keepalive as the cure contradicts keepalive's own record. The
    reason text stays here (it can carry a path); `orca` is the one fact a
    consumer needs from it: whether the skip was Orca holding the chain."""
    path = _log_path()
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    wanted = {home_name, cred._display_path(home_name)}
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("home") not in wanted:
            continue
        action = str(row.get("action") or "")
        if action in ("would-refresh", ""):
            continue          # a dry run decided nothing about the home
        reason = str(row.get("reason") or "")
        return {"ts": row.get("ts"), "action": action,
                "by": row.get("by") or "hand",
                "orca": action == "skip" and reason.startswith("home ")
                and "orca" in reason.lower()}
    return None


def last_refresh(home_name=None, tail_bytes=262144):
    """The most recent RECORDED refresh -> {"ts", "home", "by"} or None.

    Reads only the TAIL of the append-only log: a sweep appends a row per home
    per run, so an hourly cadence makes this file grow forever and a status
    line that read all of it would get slower every day. The seek lands
    mid-line, so the partial first line is dropped rather than parsed.

    Returns three facts and no reason text. Token values were never written
    here to begin with (the module docstring's law), and a `reason` can name a
    home path, which a status sentence on a shared page should not carry.
    """
    path = _log_path()
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    wanted = {home_name, cred._display_path(home_name)} if home_name else None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("action") != "refreshed":
            continue
        if wanted and row.get("home") not in wanted:
            continue
        return {"ts": row.get("ts"), "home": row.get("home"),
                "by": row.get("by") or "hand"}
    return None


@contextlib.contextmanager
def _writer_lock():
    """The machine-wide credential-writer lock, non-blocking -> True when held.
    Every applied keepalive write takes it, sweep or --home, and the Orca
    credhome sync takes the same file (cred.orca._keepalive_lock), so the two
    never write one home at once."""
    lock_path = _cache_path("keepalive.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True


LOCK_HELD = {"action": "skip", "reason": "the credential-writer lock is held (another "
                                         "keepalive sweep, or an Orca credhome sync)"}


def sweep(early_h=24, apply=False):
    """Dry-run scans without even creating a lock file. Applied sweeps take the
    machine-wide writer lock before any backup/network/write."""
    if not apply:
        return _sweep_rows(early_h, False)
    with _writer_lock() as held:
        return _sweep_rows(early_h, True) if held else [dict(LOCK_HELD)]


# ---------------------------------------------------------------------------
# cadence — the hourly systemd user timer (external, no demons)
# ---------------------------------------------------------------------------

DEFAULT_INTERVAL_S = 3600    # an anthropic access token lives ~8h and the
                             # sweep's early horizon is 24h, so hourly is many
                             # chances per token rather than one; the pass is
                             # reads plus an idempotent grant on the few homes
                             # actually due, so cheaper buys nothing

_UNIT_SERVICE = """[Unit]
Description=helm keepalive (roll idle claude homes' tokens forward)

[Service]
Type=oneshot
WorkingDirectory=%(cwd)s
Environment=HELM_CHAT_NAME=keepalive-cron
UnsetEnvironment=CLAUDE_CODE_SESSION_ID CLAUDE_SESSION_ID CODEX_SESSION_ID
ExecStart=%(helm)s keepalive --apply
"""
# Environment + UnsetEnvironment together are repo-watch's measured env
# hygiene, and here the declared name is load-bearing twice over: it is what
# `_actor` writes into every log row, so the log can say whether the last
# refresh was the loop or a person.

_UNIT_TIMER = """[Unit]
Description=helm keepalive cadence (external, no demons)

[Timer]
OnBootSec=300
OnUnitActiveSec=%(interval)ss

[Install]
WantedBy=timers.target
"""

TIMER_NAME = "helm-keepalive.timer"


def _timer_units(interval=DEFAULT_INTERVAL_S):
    # Same law as tasksmirror._timer_units, same reason: a persistent unit must
    # never capture a DISPOSABLE WORKTREE's path — the binary is the stable
    # install, the cwd is the lane folded back to the shared checkout.
    from . import work
    helm_bin = os.path.join(HOME, ".local", "bin", "helm")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cwd = work.find_root(here) or here
    udir = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    return (os.path.join(udir, "helm-keepalive.service"),
            _UNIT_SERVICE % {"helm": helm_bin, "cwd": cwd},
            os.path.join(udir, TIMER_NAME),
            _UNIT_TIMER % {"interval": interval})


def hand_crontab():
    """Crontab lines that run this verb, or [] — never a mutation.

    A HAND-INSTALLED CADENCE IS STILL A CADENCE, and it is the one thing that
    would make this timer a SECOND credential writer on the same schedule. The
    writer lock means the overlap is safe rather than destructive, but two
    installers of one loop is exactly the hand-owned state helm is supposed to
    own — so the ensure path REPORTS the line and names it as superseded, and
    the operator removes it. helm does not edit a crontab it did not write:
    the file is the operator's, one bad rewrite takes every other job with it,
    and nothing here can prove which lines are ours to drop."""
    crontab = shutil.which("crontab")
    if not crontab:
        return []
    try:
        r = subprocess.run([crontab, "-l"], capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    return [ln.strip() for ln in (r.stdout or "").splitlines()
            if "keepalive" in ln and not ln.lstrip().startswith("#")]


def timer_installed(interval=DEFAULT_INTERVAL_S):
    """Is the unit file on disk? The same question doctor asks of the mirror
    and the stale-bot, asked the same way."""
    return os.path.exists(_timer_units(interval)[2])


def ensure_timer(interval=DEFAULT_INTERVAL_S):
    """Install/refresh and enable the hourly cadence -> (ok, detail).
    Idempotent: re-running is the refresh path (tasksmirror's shape), so a
    changed interval or a moved checkout is repaired by re-running rather than
    by an operator noticing."""
    from . import pk
    if interval < 1:
        return False, "interval must be at least 1 second"
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, ("systemctl unavailable; run `helm keepalive --apply` "
                       "from another scheduler")
    spath, service, tpath, timer = _timer_units(interval)

    def current(path):
        try:
            with open(path) as fh:
                return fh.read()
        except OSError:
            return None

    existed = os.path.exists(tpath)
    unchanged = current(spath) == service and current(tpath) == timer
    try:
        os.makedirs(os.path.dirname(spath), exist_ok=True)
        # IDEMPOTENT MEANS THE BYTES DO NOT MOVE. Re-writing identical content
        # still bumps mtime, which is the one thing an operator (or systemd's
        # own change detection) can look at to tell a verify from an install —
        # so a pass that reports "unchanged" must leave the files alone to have
        # said something true.
        if not unchanged:
            pk.atomic_write(spath, service)
            pk.atomic_write(tpath, timer)
    except OSError as e:
        return False, "unit write failed: %s" % e
    for cmd in ([systemctl, "--user", "daemon-reload"],
                [systemctl, "--user", "enable", "--now", TIMER_NAME]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return False, "%s failed: %s" % (
                " ".join(cmd), (r.stderr or r.stdout or "").strip()[:200])
    # WHAT IT FOUND, not only what it did: a second call on an unchanged tree
    # has to be legible as a no-op or an operator cannot tell a verified
    # install from a fresh one, and will keep re-typing it.
    detail = ("keepalive cadence already installed every %ds, unchanged (%s)"
              % (interval, tpath) if existed and unchanged
              else "keepalive cadence %s every %ds (%s)"
              % ("refreshed" if existed else "enabled", interval, tpath))
    for line in hand_crontab():
        detail += ("\n  a hand-installed crontab line runs this verb too and is "
                   "SUPERSEDED by the timer above — remove it with `crontab -e`: %s"
                   % line)
    return True, detail


USAGE = ("usage: helm keepalive [--home NAME|PATH] [--early HOURS] [--apply] "
         "[--ensure-timer]")


def _cmd_keepalive(args):
    """keepalive [--home H] [--early N] [--apply] [--ensure-timer] — roll idle claude
    homes' OAuth tokens forward before their refresh chains rot (full sweep by
    default; codex is read-only by design). The ONE credential-writing verb.
    --ensure-timer installs/verifies the hourly cadence and exits; without it
    this verb only runs when somebody types it, which is how six healthy
    accounts came to read reauth-needed.
    Log: $HELM_CACHE_DIR/keepalive-log.jsonl (default ~/.cache/helm)."""
    args = list(args or [])
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    home = early = None
    apply = ensure = False
    while args:
        a = args.pop(0)
        if a == "--apply":
            apply = True
        elif a == "--ensure-timer":
            ensure = True
        elif a == "--home" and args:
            home = args.pop(0)
        elif a == "--early" and args:
            early = args.pop(0)
        else:
            print(USAGE, file=sys.stderr)
            return 2
    try:
        early = float(early) if early is not None else 24.0
    except ValueError:
        print("helm keepalive: --early must be a number of hours", file=sys.stderr)
        return 2
    if ensure:
        # THE CADENCE IS INSTALLED, NEVER RUN, BY THIS FLAG. --ensure-timer is
        # about the loop's existence; mixing a grant into it would make an
        # idempotent verify a credential write. The period is not --early's to
        # set either: --early is how far ahead of expiry a token is rolled
        # forward, and the two answer different questions.
        ok, detail = ensure_timer()
        print("helm keepalive --ensure-timer: %s" % detail,
              file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
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
        if apply:
            with _writer_lock() as held:
                results = [refresh_home(path, early_horizon_s=int(early * 3600),
                                        apply=True) if held else dict(LOCK_HELD)]
        else:
            results = [refresh_home(path, early_horizon_s=int(early * 3600))]
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
    """keepalive [--home H] [--early N] [--apply] [--ensure-timer] — dry-run by default."""
    try:
        return _cmd_keepalive(args)
    except Exception as e:
        print("helm keepalive: operation failed (%s)" % e.__class__.__name__,
              file=sys.stderr)
        return 1
