#!/usr/bin/env python3
"""helm session — the SESSION SUBSTRATE interface (premise: sessions-are-the-
substrate). One context window holds one complex concept well; the
environment's real capability is MANY sessions preserved / branchable /
resumable across the whole local env, first-class.

helm WRAPS cv (clustervision), never rebuilds it (cv-is-core-helm-dep): cv owns
the mechanics (read/reshape/rehome/shrink — ls/show/doctor/prune/port/resume);
helm owns the POLICY:

  LAW 1 — never two live copies of one session. Before printing any launch
    line, scan live Claude pids; prefer Claude Code's procStart-bound pid record,
    then canonical argv/who/cwd evidence. Child-stamped panes are included even
    when no heartbeat registers. The inherited stamp SID is an ancestor, never
    the child's own session. Found or conservatively possible: close/verify
    first, never print the incantation.
  LAW 2 — prepare + print, never launch. checkpoint/port/rescue end at a
    PRINTED incantation; only `resume --launch` spawns, and only after law 1.

THE PERSISTENCE SURFACE: transcript presence is the verdict. A child stamp is
only a reason to inspect (it has historically disabled persistence); stamped
children that are writing transcripts are persisted, while unstamped panes with
no transcript are memory-only too. Unresolved SIDs remain UNKNOWN. `ls` /
`doctor` / `doctor-panes` surface all three states; printed incantations still
strip inherited stamps and bake CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1 so a
resume cannot re-enter the known trap.

EXPERTS (expert-sessions-beat-fresh-research): sessions are also the EXPERTISE
layer — over time, querying/resuming a preserved expert beats fresh research.
`experts` is a durable registry (~/.helm/_global/session-experts.json:
sid -> {domain, registered, last_refreshed, note}) so routing to an expert is
O(1). `ask <domain> <q>` is the QUERY LADDER: registry hit -> transcript search
(cv search scoped to the expert) -> else pack-digest (cv pack) -> resume-live
is always PRINT-DON'T-LAUNCH with a mandatory RE-GROUND instruction (expertise
goes stale like everything else — the expert re-verifies key facts against the
current substrate before answering).
"""
import calendar
import contextlib
import fcntl
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time

from . import home, pk

CV = "cv"
FORCE_VAR = "CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"
FORCE = FORCE_VAR + "=1"
PROC = "/proc"
_PROC_FILE_MAX = 4 * 1024 * 1024
_SESSION_RECORD_MAX = 64 * 1024
_SID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


# ---------------------------------------------------------------------------
# cv wrapper (the policy/mechanics seam — thin, timeout-bounded, clean degrade)
# ---------------------------------------------------------------------------

def _cv(*argv, timeout=120, env=None):
    """Run cv, return (rc, stdout, stderr). FileNotFoundError / timeout degrade
    to a named error string, never a traceback — helm's policy layer must stay
    up when the mechanics layer is absent."""
    try:
        p = subprocess.run([CV] + list(argv), capture_output=True, text=True,
                           timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "cv not installed — clustervision must be on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", "cv timed out after %ds" % timeout
    except OSError as e:
        return 1, "", "cv failed to launch: %s" % e


def _stamp_vars():
    from . import seat
    return seat.CHILD_STAMP_VARS


def _unset_prefix():
    return "env " + " ".join("-u " + v for v in _stamp_vars()) + " "


def _launch_env():
    """Environment for the one permitted launch path: strip inherited child
    identity and force transcript persistence on."""
    env = os.environ.copy()
    for v in _stamp_vars():
        env.pop(v, None)
    env[FORCE_VAR] = "1"
    return env


@contextlib.contextmanager
def _launch_lock(sid):
    """Non-blocking, per-session exclusion held for the attached cv/Claude
    lifetime. The under-lock process recheck closes the two-helm-launch race."""
    path = os.path.join(home.global_dir(), ".state",
                        "session-launch-%s.lock" % pk.slug(sid))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _cv_launch(sid):
    """Run cv's native launch attached to this terminal. No capture and no
    timeout: the interactive harness owns the TTY until it exits."""
    try:
        return subprocess.call([CV, "resume", sid, "--launch"],
                               env=_launch_env()), ""
    except FileNotFoundError:
        return 127, "cv not installed — clustervision must be on PATH"
    except OSError as e:
        return 1, "cv failed to launch: %s" % e


# ---------------------------------------------------------------------------
# live-pane scan (law 1 + the memory-only surface) — /proc, never ps-grep
# ---------------------------------------------------------------------------

def _who_holder_sid(row):
    """Only ``helm who``'s fresh exact attribution is identity. Historical cwd
    candidates remain possible holders; promoting a sole stale file invents a SID."""
    exact = row.get("session")
    return exact if isinstance(exact, str) and _SID_RE.fullmatch(exact) else None


def _cwd_session_ids(home_dir, cwd):
    """Every Claude sid in one cwd store, filenames only. Used solely as the
    fail-closed holder set when a live fresh process cannot be attributed to
    one of several historical sessions."""
    if not (home_dir and cwd):
        return []
    slug = cwd.replace("/", "-").replace(".", "-")
    try:
        names = os.listdir(os.path.join(home_dir, "projects", slug))
    except OSError:
        return []
    suffix = ".jsonl"
    return sorted(sid for n in names if n.endswith(suffix)
                  for sid in [n[:-len(suffix)]] if _SID_RE.fullmatch(sid))


def _starttime_from_stat(raw):
    """Field 22 from one /proc/<pid>/stat payload. ``comm`` is parenthesized
    but may itself contain spaces and ``)`` characters, so field counting starts
    after the LAST close-paren. Return the exact decimal token Claude records."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    _comm, sep, tail = raw.rpartition(b")")
    if not sep:
        return None
    fields = tail.split()
    if len(fields) <= 19:
        return None
    value = fields[19]
    return value.decode("ascii") if value.isdigit() else None


def _proc_bytes(pid, name):
    with open(os.path.join(PROC, str(pid), name), "rb") as f:
        raw = f.read(_PROC_FILE_MAX + 1)
    if len(raw) > _PROC_FILE_MAX:
        raise OSError("proc file exceeds bounded census read")
    return raw


def _proc_start(pid):
    try:
        return _starttime_from_stat(_proc_bytes(pid, "stat"))
    except OSError:
        return None


def _proc_matches(pid, start, cmdline, environ=None, cwd=None):
    """The same pid generation and process image still brackets the reads.
    starttime catches exit/PID reuse; cmdline+environ catch exec; cwd prevents
    inference from composing two working-directory moments."""
    try:
        if _proc_start(pid) != start or _proc_bytes(pid, "cmdline") != cmdline:
            return False
        if environ is not None and _proc_bytes(pid, "environ") != environ:
            return False
        return cwd is None or os.readlink(os.path.join(PROC, str(pid), "cwd")) == cwd
    except OSError:
        return False


def _selected_environ(raw):
    keys = set(_stamp_vars()) | {FORCE_VAR, "CLAUDE_CONFIG_DIR", "HOME"}
    out = {}
    for kv in raw.split(b"\0"):
        key, sep, value = kv.partition(b"=")
        if not sep:
            continue
        try:
            name = key.decode("ascii")
        except UnicodeDecodeError:
            continue
        if name not in keys:
            continue
        try:
            out[name] = value.decode("utf-8")
        except UnicodeDecodeError:
            out[name] = None
    return out


def _proc_snapshot(pid):
    """One same-uid Claude process, bracketed before later record/who reads.
    Optional environ/cwd failures keep a visible UNKNOWN-capable row."""
    base = os.path.join(PROC, str(pid))
    try:
        uid = os.stat(base).st_uid
        start = _proc_start(pid)
        if uid != os.geteuid() or not start:
            return None
        if _proc_bytes(pid, "comm").strip() != b"claude":
            return None
        cmdline = _proc_bytes(pid, "cmdline")
    except OSError:
        return None
    try:
        environ_raw = _proc_bytes(pid, "environ")
        environ = _selected_environ(environ_raw)
    except OSError:
        environ_raw = environ = None
    try:
        cwd = os.readlink(os.path.join(base, "cwd"))
    except OSError:
        cwd = None
    if not _proc_matches(pid, start, cmdline, environ_raw, cwd):
        return None
    return {"pid": pid, "uid": uid, "start": start, "cmdline": cmdline,
            "environ": environ_raw,
            "argv": cmdline.decode("utf-8", "replace").split("\0"),
            "env": environ, "cwd": cwd}


HEADLESS_FLAGS = ("-p", "--print")
NONPERSISTENT_FLAG = "--no-session-persistence"


def _argv_flag(argv, names, prefixes=()):
    """Flag presence in the OPTION region of argv only: past the standard
    ``--`` terminator every token is positional (a boot prompt that happens to
    equal ``-p`` is prose, not a flag), so scanning stops there."""
    for a in argv:
        if a == "--":
            return False
        if a in names or (prefixes and a.startswith(prefixes)):
            return True
    return False


def _is_headless(argv):
    """True when this process runs in PRINT MODE (`-p`/`--print`).

    Headless is an INTERACTION MODE, not proof the process holds no session.
    Claude Code supports `--resume` together with `--print`, and print mode
    persists a transcript unless `--no-session-persistence` is also requested —
    so `claude -p --resume <sid>` is a short-lived process operating on a
    proven resumable session, every bit a holder while it lives. Whether a row
    leaves the health arithmetic is decided by _sessionless_oneshot, which
    reads this mode TOGETHER with the row's session-identity evidence."""
    return _argv_flag(argv, HEADLESS_FLAGS, ("-p=", "--print="))


def _is_nonpersistent(argv):
    """True when argv declares `--no-session-persistence` outright — the one
    flag that states sessionless intent rather than merely an interaction
    mode. Kept as its own axis so print mode is never mistaken for it."""
    return _argv_flag(argv, (NONPERSISTENT_FLAG,))


def _sessionless_oneshot(r):
    """True only when the row PROVES sessionless intent: explicit
    `--no-session-persistence` with no explicit session identity of its own.
    These rows leave every health count AND certify green — their missing
    transcript is the design working, not work at risk. Measured 2026-07-22,
    twenty minutes after this census first certified the fleet: the `remember`
    plugin ran `claude -p --output-format json --no-session-persistence` to
    compress memory, and the estate flipped to a memory-only FAIL advising
    `helm session rescue` on a process that had already exited. That row is
    excluded on its EXPLICIT nonpersistence evidence.

    Plain `-p` is NOT that proof. Print mode persists a transcript by default,
    so a `-p` row whose SID cannot be resolved is an UNRESOLVED session, not
    an absent one — it fails closed as UNKNOWN in certification like any other
    unresolved row (see _cmd_ls). Whether such a row enters HOLDER arithmetic
    is the separate axis _inherited_hint_worker decides.

    The line that must never move: a row with an EXPLICIT session identity —
    a procStart-bound pid record or an unambiguous ``--resume`` in argv — is a
    proven holder and stays in every count no matter how it interacts or how
    soon it exits (`--no-session-persistence --resume X` still READS X while
    it lives).

    This narrows WHAT IS COUNTED, never what is allowed: a helm SEAT that
    shows up headless is still a law violation, and it stays visible in
    `session ls` tagged as such rather than being hidden."""
    return bool(r.get("nonpersistent")
                and not (r.get("declared") or r.get("resume")))


def _inherited_hint_worker(r):
    """True for a print-mode/nonpersistent row with no explicit session
    identity of its OWN: whatever SID it carries is inherited/attributed from
    the pane that spawned it, so it is never a SECOND holder of that session.
    Such rows leave HOLDER arithmetic — live_sids/DOUBLE-OPEN and the
    memory-only pane census — or an ordinary background print worker carrying
    its parent's SID would manufacture a false DOUBLE-OPEN against the very
    pane it serves.

    Deliberately distinct from _sessionless_oneshot: leaving holder
    arithmetic never certifies a row green. Without explicit nonpersistence a
    plain `-p` row with no resolvable SID still fails closed as UNKNOWN —
    print mode normally persists, so 'not a second holder' is a claim about
    double-open arithmetic, never proof of sessionlessness."""
    return bool((r.get("headless") or r.get("nonpersistent"))
                and not (r.get("declared") or r.get("resume")))


def _resume_sid(argv):
    """One unambiguous full UUID from the OPTION region of argv. The scan
    honors the standard ``--`` terminator under the same law as _argv_flag:
    past it every token is positional prose, so `claude -p -- --resume <uuid>`
    carries prompt text, never a session identity — and post-terminator prose
    can never conflict away a real pre-terminator resume. Bare/trailing
    ``--resume``, a flag consumed as its value, prefixes, and conflicting
    repeats are UNKNOWN — a false holder is worse than falling through to
    another rung."""
    found = []
    for i, arg in enumerate(argv):
        if arg == "--":
            break
        value = None
        if arg == "--resume" and i + 1 < len(argv):
            value = argv[i + 1]
        elif arg.startswith("--resume="):
            value = arg.split("=", 1)[1]
        if isinstance(value, str) and _SID_RE.fullmatch(value):
            found.append(value)
    unique = sorted(set(found))
    return unique[0] if len(unique) == 1 else None


def _safe_owned_dir(value, uid):
    return (stat.S_ISDIR(value.st_mode) and value.st_uid == uid
            and not value.st_mode & stat.S_IWOTH)


def _safe_owned_record(value, uid):
    return (stat.S_ISREG(value.st_mode) and value.st_uid == uid
            and value.st_nlink == 1
            and not value.st_mode & stat.S_IWOTH
            and value.st_size <= _SESSION_RECORD_MAX)


def _config_root(config_dir, home_dir, uid):
    """Canonical process config home. Explicit config paths must already be
    absolute; ``~`` is never expanded through the inspecting process's HOME.
    Child paths are checked separately so a missing record store does not
    suppress later cwd inference from a trusted home."""
    if config_dir:
        raw = config_dir
    else:
        base = home_dir if home_dir is not None else os.path.expanduser("~")
        if not isinstance(base, str) or not os.path.isabs(base):
            return None
        raw = os.path.join(base, ".claude")
    if not isinstance(raw, str) or "\0" in raw or not os.path.isabs(raw):
        return None
    try:
        root = os.path.realpath(raw)
        rst = os.stat(root)
    except (OSError, ValueError):
        return None
    return root if _safe_owned_dir(rst, uid) else None


def _record_unchanged(path, before):
    try:
        current = os.lstat(path)
    except OSError:
        return False
    return (_safe_owned_record(current, before.st_uid)
            and (current.st_dev, current.st_ino, current.st_mode, current.st_size,
                 current.st_mtime_ns, current.st_ctime_ns)
            == (before.st_dev, before.st_ino, before.st_mode, before.st_size,
                before.st_mtime_ns, before.st_ctime_ns))


def _read_session_record(root, pid, uid, proc_start):
    """Read one bounded, owner-bound regular record without following links.
    The opened inode and pathname must still agree after the read, so atomic
    replacement or an in-place rewrite during the census degrades to UNKNOWN."""
    sessions = os.path.join(root, "sessions")
    path = os.path.join(sessions, "%d.json" % pid)
    try:
        sst = os.lstat(sessions)
    except FileNotFoundError:
        return None, "record-missing"
    except OSError:
        return None, "record-unreadable"
    if not _safe_owned_dir(sst, uid):
        return None, "record-unsafe"
    try:
        lst = os.lstat(path)
    except FileNotFoundError:
        return None, "record-missing"
    except OSError:
        return None, "record-unreadable"
    if not _safe_owned_record(lst, uid):
        return None, "record-unsafe"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) \
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
        try:
            before = os.fstat(fd)
            if not _safe_owned_record(before, uid):
                return None, "record-unsafe"
            raw = b""
            while len(raw) <= _SESSION_RECORD_MAX:
                chunk = os.read(fd, min(8192, _SESSION_RECORD_MAX + 1 - len(raw)))
                if not chunk:
                    break
                raw += chunk
            after = os.fstat(fd)
        finally:
            os.close(fd)
    except OSError:
        return None, "record-unreadable"
    before_id = (before.st_dev, before.st_ino, before.st_mode, before.st_size,
                 before.st_mtime_ns, before.st_ctime_ns)
    after_id = (after.st_dev, after.st_ino, after.st_mode, after.st_size,
                after.st_mtime_ns, after.st_ctime_ns)
    if (not _safe_owned_record(after, uid) or len(raw) > _SESSION_RECORD_MAX
            or before_id != after_id or not _record_unchanged(path, before)):
        return None, "record-replaced"
    try:
        rec = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "record-corrupt"
    if not isinstance(rec, dict) or type(rec.get("pid")) is not int \
            or rec.get("pid") != pid:
        return None, "record-schema"
    want = rec.get("procStart")
    sid = rec.get("sessionId")
    if not isinstance(want, str) or not want.isdigit() \
            or not isinstance(sid, str) or not _SID_RE.fullmatch(sid):
        return None, "record-schema"
    if want != proc_start:
        return None, "record-stale"
    return sid, "record-ok"


def _session_record(pid, config_dir, home_dir, uid, proc_start):
    root = _config_root(config_dir, home_dir, uid)
    if not root:
        return None, "config-untrusted", None
    sid, reason = _read_session_record(root, pid, uid, proc_start)
    return sid, reason, root


def _sid_from_session_file(pid, config_dir, home_dir=None, uid=None,
                           proc_start=None):
    """Compatibility seam for tests/callers that need only the proven SID."""
    try:
        uid = os.stat(os.path.join(PROC, str(pid))).st_uid if uid is None else uid
    except OSError:
        return None
    proc_start = proc_start or _proc_start(pid)
    if not proc_start or uid != os.geteuid():
        return None
    return _session_record(pid, config_dir, home_dir, uid, proc_start)[0]


def _proc_claude_rows():
    """Every same-uid live Claude process. Resolution ladder, strongest first:
    procStart-bound pid record -> unambiguous full ``--resume`` UUID -> exact
    ``helm who`` attribution -> cwd candidate set. Every per-pid read is
    bracketed; one bad record only demotes its own rung."""
    snapshots = []
    try:
        pids = sorted((int(p) for p in os.listdir(PROC) if p.isdigit()))
    except OSError:
        pids = []
    for pid in pids:
        snap = _proc_snapshot(pid)
        if snap:
            snapshots.append(snap)
    try:
        from . import who
        who_rows = {r["pid"]: r for r in who.scan(accounts=[])
                    if r.get("provider") == "anthropic" and not r.get("child")}
    except (OSError, ValueError):
        who_rows = {}
    cwd_candidates = {}
    rows = []
    for snap in snapshots:
        pid, selected = snap["pid"], snap["env"]
        env = selected or {}
        invalid_home = (("CLAUDE_CONFIG_DIR" in env
                         and env["CLAUDE_CONFIG_DIR"] is None)
                        or (not env.get("CLAUDE_CONFIG_DIR") and "HOME" in env
                            and env["HOME"] is None))
        if selected is None:
            declared, reason, root = None, "environ-unreadable", None
        elif invalid_home:
            declared, reason, root = None, "config-untrusted", None
        else:
            declared, reason, root = _session_record(
                pid, env.get("CLAUDE_CONFIG_DIR"), env.get("HOME"),
                snap["uid"], snap["start"])
        resume = _resume_sid(snap["argv"])
        child = env.get("CLAUDE_CODE_CHILD_SESSION") == "1"
        attributed = None if child else _who_holder_sid(who_rows.get(pid, {}))
        possible = []
        if not (child or declared or resume or attributed) and root and snap["cwd"]:
            key = (root, snap["cwd"])
            if key not in cwd_candidates:
                cwd_candidates[key] = _cwd_session_ids(*key)
            possible = cwd_candidates[key]
        if not _proc_matches(pid, snap["start"], snap["cmdline"],
                             snap["environ"], snap["cwd"]):
            continue
        session_id = declared or resume or attributed
        rows.append({
            "pid": pid,
            "resume": resume,
            "declared": declared,
            "declared_reason": reason,
            "identity": ("declared" if declared else "resume" if resume
                         else "who" if attributed else "unknown"),
            "session": session_id,
            "possible_sessions": possible,
            "child": child,
            "ancestor_sid8": (env.get("CLAUDE_CODE_SESSION_ID") or "")[:8],
            "force": env.get(FORCE_VAR) == "1",
            "headless": _is_headless(snap.get("argv") or []),
            "nonpersistent": _is_nonpersistent(snap.get("argv") or []),
        })
    return rows


def live_sids(rows=None):
    """{sid: [pid,...]} of proven live copies. The procStart-bound pid record
    wins, then canonical resume/who evidence. An inherited stamp SID is only the
    spawning ancestor and never enters this map, and neither does a print-mode
    worker whose only session hint is inherited/attributed — a background
    worker carrying its parent's SID must not manufacture a false DOUBLE-OPEN.
    But headless is only an interaction mode: a print-mode row with an explicit
    ``--resume``/pid-record identity is a proven live holder and MUST enter, or
    a real overlapping holder hides from the certifier while the launch guard
    still sees it (see _inherited_hint_worker)."""
    out = {}
    for r in (rows if rows is not None else _proc_claude_rows()):
        sid = r.get("session") or r.get("resume")
        if sid and not _inherited_hint_worker(r):
            out.setdefault(sid, []).append(r["pid"])
    return out


def _sid_matches(candidate, prefix):
    candidate, prefix = (candidate or "").lower(), (prefix or "").lower()
    return bool(candidate and prefix and
                (candidate.startswith(prefix) or prefix.startswith(candidate)))


def _matching_rows(sid, rows):
    """(proven rows, unproven candidate rows) for one sid/prefix."""
    exact, possible = [], []
    for row in rows:
        if _sid_matches(row.get("session"), sid):
            exact.append(row)
        elif any(_sid_matches(candidate, sid)
                 for candidate in row.get("possible_sessions") or []):
            possible.append(row)
    return exact, possible


def open_pids(sid, rows=None):
    """Live pids holding, or conservatively capable of holding, sid. Candidate
    rows remain unproven but still block launch/resume so law 1 fails closed."""
    exact, possible = _matching_rows(
        sid, rows if rows is not None else _proc_claude_rows())
    return sorted({r["pid"] for r in exact + possible})


def memory_only_panes(rows=None, persisting=None):
    """Proven live AGENT PANES with NO transcript on disk. Two exclusions, and
    they are different in kind:

    UNKNOWN candidate rows are excluded because they are neither a persistence
    PASS nor memory-only — a check that passes on absent input reports the
    opposite of the truth.

    Print-mode workers with no session identity of their OWN are excluded
    because they are not panes: an explicitly nonpersistent one-shot has no
    transcript by design, and an inherited/attributed-SID worker's transcript
    risk belongs to the pane that actually holds that session. But a
    print-mode row with an EXPLICIT session identity stays — headless is an
    interaction mode, and print mode persists unless nonpersistence was
    requested outright (see _inherited_hint_worker)."""
    rows = rows if rows is not None else _proc_claude_rows()
    persisting = persisting if persisting is not None else _persisting_sids()
    return [r for r in rows if r.get("session") and not _inherited_hint_worker(r)
            and _sid_on_disk(r["session"], persisting) is False]


# ---------------------------------------------------------------------------
# verbs
# ---------------------------------------------------------------------------

def _resolve_sid(prefix):
    """id-prefix -> full sid via the catalog (one resolver, the <sid> prefix
    convention). None + a printed reason when ambiguous/absent."""
    from . import sessions
    all_hits = [r for r in sessions.rows_for(include_synthetic=True)
                if r["i"].startswith(prefix)]
    hits = [r for r in all_hits if r.get("h") == "claude"]
    if not hits and all_hits:
        return None, "session '%s' is not a Claude session (v1 supports Claude)" % prefix
    if not hits:
        return None, "no session id starts with '%s'" % prefix
    if len(hits) > 1:
        return None, "%d sessions match '%s'" % (len(hits), prefix)
    return hits[0]["i"], None


def _session_row(sid):
    from . import sessions
    return next((r for r in sessions.rows_for(include_synthetic=True)
                 if r["i"] == sid), None)


def _session_cwd(sid):
    row = _session_row(sid)
    cwd = os.path.expanduser((row or {}).get("cwd") or (row or {}).get("c") or "")
    if not cwd:
        raise ValueError("session %s has no recorded cwd; refusing an unsafe resume line"
                         % sid[:12])
    return cwd


def _print_incantation(sid, cred_home=None, cwd=None):
    """LAW 2's output: the pasteable resume line, FORCE baked in, child-stamp
    unset (so a paste into a stamped pane can't re-trap), optional credhome.
    Values are shell-quoted because cwd and configured home paths are data.
    Never executed here."""
    env = _unset_prefix() + FORCE + " "
    if cred_home:
        env += "CLAUDE_CONFIG_DIR=%s " % shlex.quote(cred_home)
    return "cd %s && %sclaude --resume %s" % (
        shlex.quote(cwd or _session_cwd(sid)), env, shlex.quote(sid))


class _PersistenceCensus(dict):
    def __init__(self, *args, complete=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.complete = complete


def _persisting_sids():
    """Full sids that HAVE a real transcript on disk — persistence TRUTH, not
    the env stamp (premise transcript-on-disk-is-persistence-truth-not-env: a
    child-stamped pane whose transcript is growing on disk is persisting fine,
    and the env heuristic over-flags it MEMORY-ONLY). Sourced from the session
    catalog — the canonical scanner that already follows symlinked homes
    (cto-example -> …) and never indexes the derived .flat.jsonl variant.

    PLUS the SEAT homes, which the catalog does NOT cover: a proxy seat's
    CLAUDE_CONFIG_DIR lives under ~/.helm/_global/seats/<family>[/instances/
    <seat>]/claude, so its transcripts are invisible to a ~/.claude +
    ~/.claude-homes scan. Measured 2026-07-22: the catalog held 1472 rows and
    ZERO under ~/.helm, so EVERY proxy seat was reported MEMORY-ONLY while
    writing a multi-MB transcript (codex-2 at 2.9MB, codex-3 at 2.0MB). A
    persistence surface that lies about the seats is worse than none — it is
    what the fleet uses to decide whether an agent's work is safe to lose."""
    out, complete = {}, True
    try:
        from . import transcripts
        for r in transcripts.get_catalog().get("rows", []):
            p = r.get("p")
            if p and not str(p).endswith(".flat.jsonl") and os.path.exists(p):
                out[r["i"]] = p
    except Exception:
        complete = False
    # seat homes — walk each seat's own projects store directly
    try:
        seats_root = os.path.join(home.global_dir(), "seats")
        if os.path.isdir(seats_root):
            for fam in sorted(os.listdir(seats_root)):
                fam_dir = os.path.join(seats_root, fam)
                homes = [os.path.join(fam_dir, "claude")]
                inst = os.path.join(fam_dir, "instances")
                if os.path.isdir(inst):
                    homes += [os.path.join(inst, s, "claude")
                              for s in sorted(os.listdir(inst))]
                for h in homes:
                    proj = os.path.join(h, "projects")
                    if not os.path.isdir(proj):
                        continue
                    errors = []
                    for root, _dirs, files in os.walk(
                            proj, onerror=lambda _e: errors.append(True)):
                        for fn in files:
                            if fn.endswith(".jsonl") and not fn.endswith(".flat.jsonl"):
                                out.setdefault(fn[:-len(".jsonl")],
                                               os.path.join(root, fn))
                    complete = complete and not errors
    except Exception:
        complete = False
    return _PersistenceCensus(out, complete=complete)


def _sid_on_disk(sid, persisting):
    """True persisted, False proven absent, None census incomplete/UNKNOWN."""
    path = persisting.get(sid) if sid else None
    if path and os.path.isfile(path):
        return True
    return False if getattr(persisting, "complete", True) else None


def _cmd_ls(args, certify=False):
    """Shared renderer. Certification refuses to call UNKNOWN a passing view."""
    rows = _proc_claude_rows()
    persisting = _persisting_sids()

    # Persistence is decided by the TRANSCRIPT ON DISK, for EVERY pane — a
    # top-level session with no transcript is at-risk just as much as a stamped
    # one; the stamp/force is only WHY, never the persistence verdict. (The old
    # code trusted `not child` => persisted and mislabeled genuinely-transcript-
    # less top-level panes as safe — the dangerous direction.)
    # An UNRESOLVED sid can't be checked against disk — its persistence is
    # genuinely UNKNOWN, never assert-safe NOR false-alarm memory-only. The hard
    # at-risk count is only panes we RESOLVED and found transcript-less.
    disk = {r["pid"]: _sid_on_disk(r.get("session"), persisting)
            for r in rows if r.get("session")}
    # Two exclusion axes, deliberately different in reach. HOLDER arithmetic
    # (memory-only, double-open) excludes every print-mode row with no session
    # identity of its OWN (_inherited_hint_worker) — a worker carrying its
    # parent's SID is not a second holder. But CERTIFICATION green is earned
    # only by proof: an explicitly nonpersistent one-shot
    # (_sessionless_oneshot) is sessionless by request; a plain `-p` row with
    # no resolvable SID is an UNRESOLVED session — print mode persists by
    # default — and fails closed as UNKNOWN. A print-mode row with an explicit
    # --resume/pid-record identity is a proven holder that stays in every
    # count. Excluded rows stay rendered below; exclusion narrows what is
    # COUNTED, never what the operator can SEE.
    mo = memory_only_panes(rows, persisting)
    unknown = [r for r in rows if not _sessionless_oneshot(r)
               and (not r.get("session") or disk.get(r["pid"]) is None)]
    live = live_sids(rows)
    dbl = {s: ps for s, ps in live.items() if len(ps) > 1}
    print("helm session ls — %d live claude panes" % len(rows))
    for r in sorted(rows, key=lambda x: x["pid"]):
        sid = r.get("session") or r.get("resume")
        why = (" (rescued)" if r["force"] else " (stamped)" if r["child"] else "")
        if _sessionless_oneshot(r):
            # SHOWN, never hidden. Excluding it from the health counts above is
            # about what gets COUNTED; withholding it from the operator would
            # be about what can be SEEN, and a helm SEAT running headless is a
            # law violation that has to stay visible to be caught. Only
            # explicit nonpersistence earns this label: plain print mode
            # persists by default, so a `-p` row with no resolvable SID is
            # UNRESOLVED, not sessionless, and falls through to UNKNOWN below.
            state = ("headless one-shot (--no-session-persistence; "
                     "sessionless by request)")
        elif not sid:
            reason = r.get("declared_reason")
            detail = "; pid record %s" % reason if reason else ""
            state = "UNKNOWN (sid unresolved%s — verify by hand)" % detail + why
        elif disk[r["pid"]] is True:
            state = "persisted" + why
        elif disk[r["pid"]] is None:
            state = "UNKNOWN (transcript census incomplete — verify by hand)" + why
        elif _inherited_hint_worker(r):
            # transcript absent, but this worker's only SID evidence is
            # inherited/attributed — the persistence alarm belongs to the pane
            # that actually holds the session, so this row never enters the
            # memory-only count above; the render must agree with the count
            state = ("inherited-session print worker (no identity of its own; "
                     "transcript risk belongs to its holder)")
        elif r["child"] and not r["force"]:
            state = "MEMORY-ONLY (stamped, no FORCE)"
        else:
            state = "MEMORY-ONLY (no transcript on disk)"
        if r.get("headless") and not _sessionless_oneshot(r):
            # a print-mode HOLDER — counted like any pane, tagged so the
            # operator sees the interaction mode too
            state += " [headless]"
        possible = len(r.get("possible_sessions") or [])
        res = ((" session=%s" % sid[:12]) if sid else
               (" session=? (%d cwd candidates)" % possible if possible else ""))
        print("  pid %-8d %s%s" % (r["pid"], state, res))
    if dbl:
        print("DOUBLE-OPEN (law 1 violation):")
        for s, ps in dbl.items():
            print("  sid %s open in pids %s" % (s, ps))
    if mo:
        print("⚠ %d memory-only pane(s) — a death loses them; rescue via "
              "`helm session rescue`." % len(mo))
    if unknown:
        print("? %d pane(s) have UNKNOWN sid/persistence — no safety or absence "
              "claim is certified." % len(unknown))
    return 1 if certify and (unknown or mo or dbl) else 0


def cmd_ls(args):
    """List all panes without turning the inventory itself into a health gate."""
    return _cmd_ls(args)


def cmd_doctor_panes(args):
    """The doctor leg for LIVE panes: which are memory-only right now, which
    are double-open. (cv doctor owns per-session context-window diagnosis;
    this is the persistence/liveness layer cv doesn't see.)"""
    return _cmd_ls(args, certify=True)


def _live_sid(prefix, rows):
    matches = sorted({r["session"] for r in rows
                      if r.get("session") and _sid_matches(r["session"], prefix)})
    if len(matches) == 1:
        return matches[0], None
    if matches:
        return None, "%d live sessions match '%s'" % (len(matches), prefix)
    return None, None


def _doctor_sid(sid, rows):
    """Render one already-resolved SID. A proven transcriptless live pane does
    not depend on catalog/cv mechanics that require the missing transcript."""
    kinds = []
    exact, uncertain = _matching_rows(sid, rows)
    pids = sorted({r["pid"] for r in exact + uncertain})
    disk_state = None
    if exact:
        # PERSISTENCE IS TRANSCRIPT-TRUTH, not the env stamp. The stamp is a
        # reason to inspect; only a still-present transcript is the verdict.
        disk_state = _sid_on_disk(sid, _persisting_sids())
        if disk_state is None:
            kinds.append("UNKNOWN (transcript census incomplete)")
        elif disk_state is False:
            stamped = any(p["child"] and not p["force"] for p in exact)
            kinds.append("bridged-child (memory-only)" if stamped
                         else "memory-only (no transcript on disk)")
        else:
            kinds.append("live")
    out = ""
    if disk_state is not False:
        rc, out, cerr = _cv("doctor", sid, "--json")
        if rc != 0:
            print("helm session doctor: cv doctor failed: " + (cerr or out),
                  file=sys.stderr)
            return 1
    if uncertain:
        kinds.append("UNKNOWN (sid is only a cwd candidate in pid(s) %s)"
                     % sorted(r["pid"] for r in uncertain))
    try:
        d = json.loads(out) if out.strip() else {}
    except ValueError:
        d = {}
    if d.get("compactions") or d.get("compact_boundaries"):
        kinds.append("compacted")
    if d.get("forked_from") or d.get("forkedFrom"):
        kinds.append("forked")
    if not kinds:
        kinds.append("normal")
    print("helm session doctor %s" % sid[:12])
    print("  kind: %s" % " / ".join(kinds))
    if uncertain:
        print("  holder evidence: proven pid(s) %s; UNKNOWN candidate pid(s) %s "
              "— LAW 1 blocks resume until verified" %
              (sorted(r["pid"] for r in exact),
               sorted(r["pid"] for r in uncertain)))
    elif pids:
        print("  live in pid(s): %s — LAW 1: close before any resume" % pids)
    if out.strip():
        print("  cv doctor: " + out.strip().splitlines()[0])
    elif disk_state is False:
        print("  cv doctor: skipped (no transcript on disk)")
    # Key the lane on MEMORY-ONLY, not on "bridged": a stamped child and an
    # unstamped transcript-less pane are the same emergency (context dies with
    # the process) and both must route to rescue. Keying on the narrower token
    # would send the unstamped case to checkpoint, which needs a transcript
    # that by definition is not there.
    lane = ("rescue (memory-only — harvest first)"
            if any("memory-only" in k for k in kinds)
            else "verify live pane identity before checkpoint/port"
            if any("UNKNOWN" in k for k in kinds)
            else "checkpoint/port as needed")
    print("  lane: %s" % lane)
    return 0


def cmd_doctor(args):
    """session doctor <sid> — classify a session from transcript truth plus cv
    metadata. A unique proven live SID can be diagnosed before it has a catalog
    row, which is essential for transcriptless memory-only panes."""
    if not args:
        print("usage: helm session doctor <sid-prefix>", file=sys.stderr)
        return 2
    rows = _proc_claude_rows()
    sid, err = _live_sid(args[0], rows)
    if err:
        print("helm session doctor: " + err, file=sys.stderr)
        return 1
    if not sid:
        sid, err = _resolve_sid(args[0])
    if not sid:
        print("helm session doctor: " + err, file=sys.stderr)
        return 1
    return _doctor_sid(sid, rows)


def cmd_checkpoint(args):
    """session checkpoint <sid> [--window N] — mint a NEW resumable snapshot id
    (original untouched) via cv prune (revive + --thinking default), so a
    maxed/forked session becomes branchable. Memory-only sids route to rescue."""
    if not args:
        print("usage: helm session checkpoint <sid-prefix> [--window N]",
              file=sys.stderr)
        return 2
    sid, err = _resolve_sid(args[0])
    if not sid:
        print("helm session checkpoint: " + err, file=sys.stderr)
        return 1
    window = "150000"
    if "--window" in args:
        i = args.index("--window")
        window = args[i + 1] if i + 1 < len(args) else window
    rows = _proc_claude_rows()
    exact, uncertain = _matching_rows(sid, rows)
    if uncertain:
        print("helm session checkpoint: %s has UNKNOWN live holder candidate(s) "
              "in pid(s) %s — verify identity before prune." %
              (sid[:12], sorted(r["pid"] for r in uncertain)), file=sys.stderr)
        return 1
    persisting = _persisting_sids()
    states = [_sid_on_disk(r["session"], persisting) for r in exact]
    if any(state is None for state in states):
        print("helm session checkpoint: transcript persistence is UNKNOWN — "
              "the census was incomplete; verify before prune.", file=sys.stderr)
        return 1
    if any(state is False for state in states):
        print("helm session checkpoint: %s is MEMORY-ONLY live — use "
              "`helm session rescue %s` (harvest lane), not prune." % (sid[:12], sid[:12]),
              file=sys.stderr)
        return 1
    cwd = _session_cwd(sid)  # fail closed before cv creates any artifact
    import uuid
    newid = str(uuid.uuid4())
    rc, out, cerr = _cv("prune", sid, "--window", window, "--thinking",
                        "--to", newid, "--json")
    if rc != 0:
        print("helm session checkpoint: cv prune failed: " + (cerr or out),
              file=sys.stderr)
        return 1
    try:
        report = json.loads(out)
    except ValueError:
        report = {}
    if report.get("newId") != newid:
        print("helm session checkpoint: cv prune returned success without the "
              "requested artifact id", file=sys.stderr)
        return 1
    pk.event("session-checkpoint", newid, "from %s window %s" % (sid[:12], window))
    print("checkpoint minted: %s (from %s)" % (newid, sid[:12]))
    print("resume: " + _print_incantation(newid, cwd=cwd))
    return 0


def _project_trusted(cred_home, cwd):
    cfg = pk.read_json(os.path.join(cred_home, ".claude.json"), {}) or {}
    return bool(((cfg.get("projects") or {}).get(cwd) or {})
                .get("hasTrustDialogAccepted"))


def cmd_port(args):
    """session port --cred <home> <sid> — cred-switch resume PREP: verify the
    target home's projects share / trust / settings, then PRINT the incantation
    (FORCE baked in). Never launches (law 2)."""
    home_arg = sid = None
    i = 0
    while i < len(args):
        if args[i] == "--cred" and i + 1 < len(args):
            home_arg = args[i + 1]; i += 2
        else:
            sid = args[i]; i += 1
    if not (home_arg and sid):
        print("usage: helm session port --cred <credhome> <sid-prefix>",
              file=sys.stderr)
        return 2
    sid, err = _resolve_sid(sid)
    if not sid:
        print("helm session port: " + err, file=sys.stderr)
        return 1
    from . import homes
    target = next((h for h in homes.homes_list()
                   if h.get("name") == home_arg or home_arg in (h.get("aliases") or [])
                   or h.get("path") == home_arg), None)
    if not target:
        print("helm session port: no cred home matches '%s'" % home_arg,
              file=sys.stderr)
        return 1
    if target.get("archived"):
        print("helm session port: target '%s' is archived; restore it before "
              "resume" % home_arg, file=sys.stderr)
        return 1
    if target.get("provider") not in (None, "claude"):
        print("helm session port: target '%s' is not a Claude cred home" % home_arg,
              file=sys.stderr)
        return 1
    if target.get("authed") is False:
        print("helm session port: target '%s' is not authenticated" % home_arg,
              file=sys.stderr)
        return 1
    path = target.get("path")
    if not path:
        print("helm session port: target '%s' has no path" % home_arg,
              file=sys.stderr)
        return 1
    cwd = _session_cwd(sid)
    # preflights (re-run at print time, never cached — homes mutate)
    proj = os.path.join(path, "projects")
    shared = target.get("projects_link_ok")
    if shared is None:
        shared = (os.path.islink(proj)
                  and os.path.realpath(proj) == os.path.realpath(
                      os.path.expanduser("~/.claude/projects")))
    trusted = _project_trusted(path, cwd)
    pids = open_pids(sid)
    print("helm session port %s -> %s" % (sid[:12], path))
    print("  projects: %s" % ("shared symlink (no file move)" if shared
                              else "OWNED dir — cv port --out required"))
    print("  trust: %s for %s" % ("seeded" if trusted else "MISSING", cwd))
    if pids:
        print("  LAW 1: sid is LIVE in pid(s) %s — close first; NOT printing "
              "the incantation." % pids)
        return 1
    if not shared:
        print("  prepare first: cv port %s --out %s" % (
            shlex.quote(sid), shlex.quote(proj)))
        return 1
    if not trusted:
        print("  target home has not accepted trust for this cwd; seed trust "
              "before resume.")
        return 1
    print("  incantation: " + _print_incantation(
        sid, cred_home=path, cwd=cwd))
    return 0


def cmd_rescue(args):
    """session rescue <pid|sid> — diagnose from the live census, then harvest
    before close. Transcriptless panes never get a fabricated resume line."""
    if not args:
        print("usage: helm session rescue <pid|sid-prefix>", file=sys.stderr)
        return 2
    target = args[0]
    rows = _proc_claude_rows()
    sid = None
    if target.isdigit():  # a live pid: resolve its sid from the same census
        pid = int(target)
        row = next((r for r in rows if r["pid"] == pid), None)
        if not row:
            print("helm session rescue: no live claude pid %s" % pid, file=sys.stderr)
            return 1
        sid = row.get("session") or row.get("resume")
        if not sid:
            ancestor = row["ancestor_sid8"]
            print("helm session rescue: pid %s has no resolvable own sid "
                  "(--resume absent; live attribution ambiguous). Stamp SID %s "
                  "is the spawning ancestor, "
                  "not this pane; harvest/recap it before close." %
                  (pid, ancestor or "unknown"), file=sys.stderr)
            return 1
        print("pid %d -> sid %s (child-stamped: %s, FORCED: %s)"
              % (pid, sid[:12], row["child"], row["force"]))
    else:
        sid, err = _live_sid(target, rows)
        if err:
            print("helm session rescue: " + err, file=sys.stderr)
            return 1
        if not sid:
            sid, err = _resolve_sid(target)
        if not sid:
            print("helm session rescue: " + err, file=sys.stderr)
            return 1
    rc = _doctor_sid(sid, rows)
    if rc != 0:
        return rc
    live = open_pids(sid, rows)
    print("rescue plan for %s:" % sid[:12])
    print("  1. HARVEST side channels FIRST (pane scrollback, journals, "
          "/dev/shm/helm-chat) — they die with the pane/reboot.")
    print("  2. Write the pane's SELF-RECAP (the fidelity anchor).")
    if live:
        print("  3. LAW 1: close live pid(s) %s after harvest; NOT printing "
              "an incantation while it is open." % live)
        return 1
    print("  3. Resume: " + _print_incantation(sid))
    return 0


def cmd_resume(args):
    """session resume <sid> [--launch] — cv resume + LAW 1 single-open guard.
    Prints the incantation; --launch (only here) spawns after the guard."""
    launch = "--launch" in args
    args = [a for a in args if a != "--launch"]
    if not args:
        print("usage: helm session resume <sid-prefix> [--launch]", file=sys.stderr)
        return 2
    sid, err = _resolve_sid(args[0])
    if not sid:
        print("helm session resume: " + err, file=sys.stderr)
        return 1
    if not launch:
        pids = open_pids(sid)
        if pids:
            print("helm session resume: LAW 1 — %s is LIVE in pid(s) %s. Close "
                  "first; NOT printing." % (sid[:12], pids), file=sys.stderr)
            return 1
        print(_print_incantation(sid))
        return 0
    with _launch_lock(sid) as acquired:
        if not acquired:
            print("helm session resume: LAW 1 — a launch for %s is already in "
                  "progress; NOT launching." % sid[:12], file=sys.stderr)
            return 1
        pids = open_pids(sid)  # under-lock recheck closes the check→launch race
        if pids:
            print("helm session resume: LAW 1 — %s is LIVE in pid(s) %s. Close "
                  "first; NOT launching." % (sid[:12], pids), file=sys.stderr)
            return 1
        rc, cerr = _cv_launch(sid)
    if rc != 0:
        print("helm session resume: cv failed: " + cerr, file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# experts registry + the ask ladder (the expertise layer)
# ---------------------------------------------------------------------------

def _experts_path():
    return os.path.join(home.global_dir(), "session-experts.json")


@contextlib.contextmanager
def _experts_lock():
    """Serialize registry read-modify-write across fleet seats. Atomic replace
    prevents torn files; this stable sibling lock prevents lost updates."""
    path = _experts_path() + ".lock"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _experts():
    return pk.read_json(_experts_path(), {}) or {}


def _write_experts(d):
    os.makedirs(os.path.dirname(_experts_path()), exist_ok=True)
    pk.atomic_write(_experts_path(), json.dumps(d, indent=2, ensure_ascii=False))


def cmd_experts(args):
    """session experts [--register <sid> --domain D [--note N]] [--refresh <sid>]
    — the O(1) expert routing registry. Durable, freshness-flagged."""
    if not args:
        ex = _experts()
        if not ex:
            print("no experts registered — helm session experts --register "
                  "<sid> --domain <domain>")
            return 0
        print("helm session experts (%d):" % len(ex))
        for sid, r in sorted(ex.items(), key=lambda kv: kv[1].get("domain", "")):
            age = _fresh(r.get("last_refreshed") or r.get("registered"))
            print("  %-14s %s  (%s, refreshed %s)%s" % (
                r.get("domain", "?"), sid[:12], r.get("note", "")[:40], age,
                "  STALE" if age.endswith("d") and int(age[:-1] or 0) > 14 else ""))
        return 0
    if "--register" in args:
        i = args.index("--register")
        sid = args[i + 1] if i + 1 < len(args) else None
        domain = note = None
        if "--domain" in args:
            j = args.index("--domain")
            domain = args[j + 1] if j + 1 < len(args) else None
        if "--note" in args:
            j = args.index("--note")
            note = args[j + 1] if j + 1 < len(args) else None
        if not (sid and domain):
            print("usage: helm session experts --register <sid> --domain D "
                  "[--note N]", file=sys.stderr)
            return 2
        full, err = _resolve_sid(sid)
        if not full:
            print("helm session experts: " + err, file=sys.stderr)
            return 1
        with _experts_lock():
            ex = _experts()
            now = pk.now_ts()
            ex[full] = {"domain": domain, "registered": now,
                        "last_refreshed": now, "note": note or ""}
            _write_experts(ex)
        pk.event("session-expert-register", full[:12], domain)
        print("registered %s as expert: %s" % (full[:12], domain))
        return 0
    if "--refresh" in args:
        i = args.index("--refresh")
        sid = args[i + 1] if i + 1 < len(args) else None
        with _experts_lock():
            ex = _experts()
            hits = [s for s in ex if s.startswith(sid or "")]
            if not hits:
                print("helm session experts: no expert sid starts '%s'" % sid,
                      file=sys.stderr)
                return 1
            if len(hits) > 1:
                print("helm session experts: %d expert sids start '%s' — "
                      "disambiguate" % (len(hits), sid), file=sys.stderr)
                return 1
            full = hits[0]
            ex[full]["last_refreshed"] = pk.now_ts()
            _write_experts(ex)
        print("refreshed %s (%s)" % (full[:12], ex[full]["domain"]))
        return 0
    print("usage: helm session experts [--register <sid> --domain D [--note N]] "
          "[--refresh <sid>]", file=sys.stderr)
    return 2


def _fresh(ts):
    """ISO ts -> '3d'/'5h'/'now' age for the freshness flag."""
    try:
        t = calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
        d = (time.time() - t) / 86400.0
        return "now" if d < 0.04 else ("%dh" % int(d * 24) if d < 1 else "%dd" % int(d))
    except (TypeError, ValueError):
        return "?"


def _grep_expert(sid, q, cap=3):
    """Matching lines from the EXPERT's own transcript (the scoped rung of the
    ask ladder) — the expert's answer to q in its own words, not the corpus's.
    Fail-open to '' (no transcript / no hit -> the caller falls to a context
    pack)."""
    try:
        from . import sessions
        row = next((r for r in sessions.rows_for(include_synthetic=True)
                    if r["i"] == sid), None)
        path = (row or {}).get("p")
        if not path or not os.path.exists(path):
            return ""
        out = subprocess.run(["grep", "-iF", "-m", str(cap), "--", q, path],
                             capture_output=True, text=True, timeout=15).stdout
        hits = []
        for line in out.splitlines():
            i = line.lower().find(q.lower())
            hits.append("  …" + line[max(0, i - 80): i + 140].strip() + "…")
        return "\n".join(hits[:cap])
    except Exception:
        return ""


def _pack_query(domain, q):
    """Plain token query for cv's full-text parser. Preserve Unicode words and
    spell syntax-bearing developer terms instead of dropping their meaning."""
    text = (domain + " " + q).replace("+", " plus ").replace("#", " sharp ") \
        .replace(".", " dot ")
    return " ".join(re.findall(r"\w+", text, flags=re.UNICODE))


def cmd_ask(args):
    """session ask <domain> <question...> — the QUERY LADDER over the experts
    registry: (1) registry hit (O(1) route) -> (2) transcript search scoped to
    the expert (cv search) -> (3) pack-digest (cv pack) -> resume-live is
    PRINT-DON'T-LAUNCH with a mandatory RE-GROUND instruction."""
    if len(args) < 2:
        print("usage: helm session ask <domain> <question...>", file=sys.stderr)
        return 2
    domain, q = args[0], " ".join(args[1:])
    ex = _experts()
    candidates = [(s, r) for s, r in ex.items() if r.get("domain") == domain]
    hit = max(candidates, key=lambda x: x[1].get("last_refreshed") or "") \
        if candidates else None
    if not hit:
        print("no expert for domain '%s' — register one: helm session experts "
              "--register <sid> --domain %s" % (domain, domain))
        print("falling back to a corpus search: cv search -- '%s'" % q)
        rc, out, cerr = _cv("search", "--limit", "5", "--", q)
        print(out if rc == 0 else "cv: " + (cerr or "unavailable"))
        return 0 if rc == 0 else 1
    sid, reg = hit
    print("expert for '%s': %s (refreshed %s)" % (domain, sid[:12],
                                                  _fresh(reg.get("last_refreshed"))))
    # ladder rung 2: transcript search SCOPED TO THE EXPERT's own transcript
    # (cv search has no per-session scope — grep the expert's jsonl directly,
    # the _grep_sessions pattern). Fall through to cv's context pack when the
    # expert's own transcript holds no hit.
    shown = _grep_expert(sid, q)
    if shown:
        print("expert-transcript hits:\n" + shown)
    else:
        pack_q = _pack_query(domain, q)
        rc, out, _ = _cv("pack", "--limit", "3", pack_q)
        if rc == 0 and out.strip():
            print("(no hit in the expert's own transcript — context pack:)\n"
                  + out.rstrip())
    # ladder rung 3: resume-live, PRINT-DON'T-LAUNCH + mandatory re-ground
    pids = open_pids(sid)
    print("\nresume-live (re-grounds before answering):")
    if pids:
        print("  expert is LIVE in pid(s) %s — ask in that pane; LAW 1 forbids "
              "printing a competing resume incantation." % pids)
    else:
        print("  " + _print_incantation(sid))
    print("  RE-GROUND (mandatory): before answering '%s', the expert re-verifies "
          "its key facts against the CURRENT substrate — expertise goes stale." % q)
    return 0


# ---------------------------------------------------------------------------

_VERBS = {
    "ls": cmd_ls,
    "doctor-panes": cmd_doctor_panes,
    "doctor": cmd_doctor,
    "checkpoint": cmd_checkpoint,
    "port": cmd_port,
    "rescue": cmd_rescue,
    "resume": cmd_resume,
    "experts": cmd_experts,
    "ask": cmd_ask,
}

USAGE = ("usage: helm session ls | doctor-panes | doctor <sid> | checkpoint "
         "<sid> [--window N]\n"
         "       helm session port --cred <home> <sid> | rescue <pid|sid> | "
         "resume <sid> [--launch]\n"
         "       helm session experts [--register <sid> --domain D [--note N]] "
         "[--refresh <sid>]\n"
         "       helm session ask <domain> <question...>")


def cmd_session(args):
    """session ls|doctor|checkpoint|port|rescue|resume|experts|ask — the
    session substrate: every session an asset (inventory, health, checkpoint,
    branch, port, compose), wrapping cv with helm's policy (single-open law,
    print-don't-launch, credhome preflights, the experts registry)."""
    args = list(args or [])
    if not args or args[0] not in _VERBS:
        print(USAGE, file=sys.stderr)
        return 2
    # `--help`/`-h` anywhere is a help request, never a <sid> prefix — without
    # this, `session checkpoint --help` resolves "--help" as an id and dies
    # with "no session id starts with '--help'" (helm-claude dogfood, 07-22).
    if any(a in ("--help", "-h") for a in args[1:]):
        print(USAGE)
        return 0
    try:
        return _VERBS[args[0]](args[1:])
    except (OSError, ValueError) as e:
        print("helm session: %s" % e, file=sys.stderr)
        return 2
