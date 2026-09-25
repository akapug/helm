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
import errno
import fcntl
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time

from . import freetext, home, openflags, pk, runtime_config, seat_launch_owner

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
                           timeout=timeout, env=home.cv_env(env))
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
    """THE SEAM, never a fourth copy of this literal. `seat.paste_unset_prefix`
    exists precisely because `sessions.resume_exec` and `transcripts._native_cmd`
    each built this string inline — and that duplication is why the proxy triple
    was missing from BOTH. Those two were wired to the seam; THIS module kept its
    own copy and stayed one register short, which is the prediction in that
    function's docstring coming true one site later ("a guard that must be
    re-added per site is a guard that will be absent from the next site").

    Measured on trunk before the fix: this prefix unset all three CHILD_STAMP
    names and NONE of the three proxy names — the stamp half proving the
    mechanism worked, so nothing was broken here except coverage."""
    from . import seat
    return seat.paste_unset_prefix()


def _launch_env():
    """Environment for the one permitted launch path: strip inherited child
    identity AND the proxy triple, then force transcript persistence on.

    THE PROXY TRIPLE NEVER RIDES INTO A LAUNCHED CLAUDE SESSION, the same law
    `launch.py` states at its own exec seam. Every non-claude family here runs
    behind CLIProxyAPI but inside Claude Code's harness, so a launch from a
    PROXIED seat's environment inherits a base URL aimed at a proxy fronting
    another vendor: the session looks native, is billed native, and routes
    elsewhere, and nothing downstream reports it.

    Measured before the fix, against a synthetic proxied environment and
    asserting on the KEY SET: all three proxy names SURVIVED into the child env
    while the stamp names did not — one register scrubbed, the other not."""
    from . import seat
    env = seat.scrub_env(os.environ)
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
        fd = f.fileno()
        try:
            os.set_inheritable(fd, True)
            try:
                yield True
            finally:
                os.set_inheritable(fd, False)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def _cv_resume_spec(sid):
    """Resolve cv's printed native harness command without letting cv own it."""
    env = _launch_env()
    rc, out, err = _cv("resume", sid, env=env)
    if rc != 0:
        return None, None, None, (err.strip() or "cv exited with status %d" % rc)
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    cwd = None
    if lines and lines[0].startswith("cd "):
        try:
            change = shlex.split(lines.pop(0))
        except ValueError as e:
            return None, None, None, "cv printed an invalid cwd: %s" % e
        if len(change) != 2 or change[0] != "cd":
            return None, None, None, "cv printed an ambiguous cwd command"
        cwd = change[1]
    if len(lines) != 1:
        return None, None, None, ("cv printed %d native launch commands; "
                                  "expected exactly one" % len(lines))
    try:
        argv = shlex.split(lines[0])
    except ValueError as e:
        return None, None, None, "cv printed an invalid launch command: %s" % e
    if not argv or argv[0] == "#" or any(
            token in (";", "&&", "||", "|", "<", ">") for token in argv):
        return None, None, None, "cv printed no executable native launch command"
    return argv, home.cv_env(env), cwd, None


def seat_config_dir(env):
    """The SEAT config dir this env would launch under, or None.

    DERIVED FROM THE SEAT ROOT, not from a path shape: `home.seat_claude_roots`
    is the same enumeration the transcript plumbing uses, so a new instance
    layout is covered here the moment it is minted, and no directory outside
    the seat root can be mistaken for a seat.
    """
    cdir = (env or {}).get("CLAUDE_CONFIG_DIR")
    if not cdir:
        return None
    try:
        real = os.path.realpath(cdir)
        root = os.path.realpath(os.path.join(home.global_dir(), "seats"))
    except OSError:
        return None
    return cdir if real == root or real.startswith(root + os.sep) else None


def _cv_launch(sid):
    """Resolve through cv, then give the actual harness to Helm's owner."""
    argv, env, cwd, err = _cv_resume_spec(sid)
    if err:
        return 1, err
    # A CV-RECORDED RESUME CAN INHERIT A SEAT CONFIG DIR — measured: this path
    # PRESERVES CLAUDE_CONFIG_DIR (and home.cv_env preserves it again), so a
    # session recorded under a seat resumed under that same seat config with no
    # fatal door and no preflight between it and exec. The uses-closure had
    # this classified as not-a-seat-session, which was simply wrong.
    #
    # Routed through the SAME helper the generated asset calls, rather than
    # given a check of its own — a second implementation here would recreate
    # the divergence this lane has now produced three times.
    cdir = seat_config_dir(env)
    if cdir:
        from . import hooks
        rc, msg = hooks.preflight(cdir)
        if rc:
            return 1, ("helm session: %s — no session was started" % msg)
    rc = seat_launch_owner.exec_attached(argv, env, cwd)
    rc = 0 if rc is None else rc
    return rc, ("" if rc == 0 else "%s exited with status %d" % (argv[0], rc))


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


def _is_agent(pid, comm):
    """True / False / None — is this same-uid pid a claude agent?

    DELEGATES TO `procid`, WHICH ALREADY OWNS THIS QUESTION and already knew the
    answer this module was missing. The launcher exec's
    `~/.local/share/claude/versions/2.1.238`, so the kernel copies a VERSION
    into comm and the `comm == b"claude"` test here answered False for every
    agent on the estate — measured 2026-08-22, 22 of 22, while `helm fleet`,
    `helm who` and `helm session ls` each certified an EMPTY box.

    `helm/sessions.py` (the PLURAL module) never had this outage, and the reason
    is exactly this delegation: it routes through procid, and this module never
    did. Two files one letter apart, one of them wired to the owner of the rule.

    THE THIRD STATE IS THE PART A LOCAL COPY WOULD HAVE LOST. procid returns
    None for "cannot tell", and its narrowness is measured rather than guessed:
    reading /proc/<pid>/exe needs PTRACE_MODE_READ where comm does not, and on
    this box 450 of 870 readable-comm pids refuse the exe read. So UNKNOWN is
    reserved for a pid whose comm LOOKS versioned and whose exe was not
    permitted — the pane the wide rung exists to rescue — and anything else with
    a readable non-claude comm is a genuine False. A first draft of this cure
    kept its own predicate and collapsed that third state into False; the sweep
    for other callers of the same question is what found the owner."""
    from . import procid
    return procid.is_claude(pid, comm, PROC)


def _proc_snapshot(pid):
    """One same-uid Claude process, bracketed before later record/who reads.
    Optional environ/cwd failures keep a visible UNKNOWN-capable row."""
    base = os.path.join(PROC, str(pid))
    try:
        uid = os.stat(base).st_uid
        start = _proc_start(pid)
        if uid != os.geteuid() or not start:
            return None
        if _is_agent(pid, _proc_bytes(pid, "comm").strip()) is not True:
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
    try:
        stdin = os.readlink(os.path.join(base, "fd", "0"))
    except OSError:
        stdin = None
    if not _proc_matches(pid, start, cmdline, environ_raw, cwd):
        return None
    return {"pid": pid, "uid": uid, "start": start, "cmdline": cmdline,
            "environ": environ_raw,
            "argv": cmdline.decode("utf-8", "replace").split("\0"),
            "env": environ, "cwd": cwd, "stdin": stdin}


HEADLESS_FLAGS = ("-p", "--print")
NONPERSISTENT_FLAG = "--no-session-persistence"

# The claude CLI's short aliases, measured against the real binary 2026-07-22:
# `-r <sid>` and `-r<sid>` both resume (commander attaches the token
# remainder as the value, so `-r=<sid>` carries the LITERAL value `=<sid>` —
# rejected: "Provided value \"=<sid>\" is not a UUID"), and commander splits
# boolean shorts off a cluster before a value-taking one, so `-pr <sid>`
# resumes in print mode. Booleans commander splits off; value-takers consume
# the cluster remainder — `-dr` is a debug filter, never a resume. A short
# absent from both maps (`-n <name>`) swallows the remainder itself, so its
# cluster stays OPAQUE verbatim.
_SHORT_BOOL = {"c": "--continue", "p": "--print"}
_SHORT_OPTIONAL = {"d": "--debug", "r": "--resume", "w": "--worktree"}

# Long flags MEASURED as boolean — never consuming the next token — against
# the real CLI (2.1.218, probe 2026-07-22: `claude <flag> --version` prints
# the version iff the flag cannot swallow `--version`; a value-taker eats it
# and the short-circuit never fires). Every option outside this set and
# _MEASURED_OPTIONAL_FLAGS is presumed capable of consuming its neighbor,
# because commander hands a required option-argument the next token RAW even
# when it is flag-shaped.
# `--bg`/`--background` probed INCONCLUSIVE (they dispatch before the version
# action fires) and stay presumed value-taking — the fail-closed side.
_MEASURED_BOOL_FLAGS = frozenset((
    "--print", "--continue", NONPERSISTENT_FLAG,
    "--allow-dangerously-skip-permissions", "--ax-screen-reader", "--bare",
    "--brief", "--chrome", "--dangerously-skip-permissions",
    "--disable-slash-commands", "--exclude-dynamic-system-prompt-sections",
    "--fork-session", "--forward-subagent-text", "--ide",
    "--include-hook-events", "--include-partial-messages", "--no-chrome",
    "--replay-user-messages", "--safe-mode", "--strict-mcp-config",
    "--verbose"))

# Long flags MEASURED as OPTIONAL-value — help declares `[value]`, and each
# passes the same discriminator as the boolean set (2.1.218, probe
# 2026-07-22: `claude <flag> --version` prints the version, so the flag
# cannot swallow a flag-shaped neighbor; `claude --model --version` still
# swallows). Commander consumes an optional option-argument only when the
# next token is NOT flag-shaped (live: `claude -d --resume BAD -p hi` errors
# 'not a UUID' — the resume was parsed, not eaten as a debug filter) and
# leaves even the ``--`` terminator alone (`claude -d -- --version` runs
# `--version` as prompt prose; `--model --` swallows the terminator). A
# NON-dash neighbor it does consume: `claude -p -d hi` errors 'Input must be
# provided' — `hi` became the debug filter, not the prompt.
_MEASURED_OPTIONAL_FLAGS = frozenset((
    "--debug", "--from-pr", "--prompt-suggestions", "--remote-control",
    "--resume", "--worktree"))


def _expand_options(argv):
    """The OPTION region parsed ONCE, under one law, into (token, consumed)
    pairs every scanner shares. `consumed` marks a token sitting in the value
    slot of the unmeasured option before it: commander hands a required
    option-argument the next token RAW even when it is flag-shaped, a short
    cluster, or the ``--`` terminator itself (measured: `claude --model --
    --version` prints the version — the terminator was swallowed as the model
    name). A consumed token is OPAQUE — never expanded, never a flag, never
    poison. Expanding it minted identity the CLI never granted (`--model -cr
    <uuid>` is a model named "-cr" plus prompt prose, not a resume), defeated
    the value-position guard with fragments of the very token commander
    swallowed whole (`--model -cp --no-session-persistence`), and poisoned a
    real holder on prompt prose that merely looked like a cluster.

    One law, THREE measured token classes: a measured boolean never takes a
    value; a measured OPTIONAL-value option consumes its neighbor only when
    the neighbor is not flag-shaped (a lone ``-`` is a value; ``-x``/``--x``
    are options; the ``--`` terminator stays a terminator — all measured, see
    _MEASURED_OPTIONAL_FLAGS); everything else is presumed REQUIRED-greedy,
    the fail-closed side. Collapsing the optional class into the greedy one
    was r7's regression: `claude -d --resume <uuid> -p` REALLY resumes, yet
    the presumption read the resume as a debug filter — certifying green and
    dropping a proven holder from DOUBLE-OPEN.

    Unconsumed short clusters are rewritten to canonical long form exactly as
    commander parses them: leading boolean shorts split off one by one, then
    a value-taking short absorbs the remainder as its attached value (`-pr X`
    -> `--print --resume X`; `-rX` -> `--resume=X`; `-r=X` -> the literal
    value `=X`, invalid, poisoning downstream; `-dr` -> `--debug=r`, a debug
    filter, never a resume) — a BARE mapped short (`-d`, `-r`, `-w`) takes
    its long form's optional-value law. A cluster led by an unmapped short
    stays verbatim — its remainder belongs to THAT flag — and a BARE unmapped
    short (`-n`) is presumed to consume its neighbor like any other
    unmeasured option. Past the standard ``--`` terminator every token is
    positional prose and passes through untouched."""
    out = []
    pending = None

    def emit(token, takes):
        nonlocal pending
        out.append((token, False))
        pending = takes

    for i, arg in enumerate(argv):
        if pending == "required" or (pending == "optional"
                                     and not (len(arg) > 1 and arg[0] == "-")):
            out.append((arg, True))
            pending = None
            continue
        pending = None
        if arg == "--":
            out.extend((a, False) for a in argv[i:])
            break
        if len(arg) > 1 and arg[0] == "-" and arg[1] != "-":
            rest = arg[1:]
            while rest and rest[0] in _SHORT_BOOL:
                emit(_SHORT_BOOL[rest[0]], None)
                rest = rest[1:]
            if rest and rest[0] in _SHORT_OPTIONAL:
                long = _SHORT_OPTIONAL[rest[0]]
                if len(rest) == 1:
                    emit(long, "optional")
                else:
                    emit(long + "=" + rest[1:], None)
            elif rest:
                emit("-" + rest, "required" if len(rest) == 1 else None)
            continue
        if arg in _MEASURED_OPTIONAL_FLAGS:
            emit(arg, "optional")
            continue
        emit(arg, "required" if len(arg) > 2 and arg[:2] == "--"
             and "=" not in arg and arg not in _MEASURED_BOOL_FLAGS else None)
    return out


def _argv_flag(argv, names, prefixes=()):
    """Flag presence in the OPTION region of argv only: past the standard
    ``--`` terminator every token is positional (a boot prompt that happens to
    equal ``-p`` is prose, not a flag), so scanning stops there. Short
    clusters are expanded first, so `-pr <sid>` declares print mode exactly
    as `-p` does — the short alias of a flag is the flag.

    A token CONSUMED as the value of the option before it is evidence of
    NOTHING: `--append-system-prompt --no-session-persistence` hands the flag
    to commander as prompt text, and reading it as declared nonpersistence
    would certify green a session the CLI persists. That verdict is
    _expand_options' — both this scanner and _resume_sid walk the SAME pairs
    under the same value-position law, so the two can never read one argv two
    ways. Fail-closed here means the occurrence never counts — only a token
    no predecessor could consume (after a positional, a measured boolean, or
    an ``=``-attached form) declares the flag."""
    for token, consumed in _expand_options(argv):
        if consumed:
            continue
        if token == "--":
            return False
        if token in names or (prefixes and token.startswith(prefixes)):
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
    reads this mode TOGETHER with the row's session-identity evidence.

    Argv is one rung of the mode evidence, not the whole of it: the CLI also
    enters print mode ON ITS OWN whenever stdin is not a terminal (measured
    2026-07-22: `claude </dev/null` with no flags errors 'when using
    --print'), so _proc_claude_rows composes this scan with the process's
    proven stdin redirection (_stdin_redirected)."""
    return _argv_flag(argv, HEADLESS_FLAGS, ("-p=", "--print="))


def _stdin_redirected(target):
    """True when a /proc fd/0 readlink target PROVES stdin is not a terminal
    — the condition under which the claude CLI enters print mode with no `-p`
    at all (see _is_headless). A pipe/socket/file/`/dev/null` stdin is that
    proof; a pty/tty is a live terminal — including the pty MASTER
    `/dev/ptmx`, isatty-true and interactive however exotic as a child's
    stdin; and an absent or unreadable target proves nothing, failing toward
    interactive — the direction that refuses to certify, never the one that
    greens."""
    if not target:
        return False
    return not (target.startswith("/dev/pts/") or target.startswith("/dev/tty")
                or target in ("/dev/console", "/dev/ptmx"))


def _is_nonpersistent(argv):
    """True when argv declares `--no-session-persistence` outright — the one
    flag that states sessionless intent rather than merely an interaction
    mode. Kept as its own axis so print mode is never mistaken for it."""
    return _argv_flag(argv, (NONPERSISTENT_FLAG,))


def _own_session_identity(r):
    """A session identity that belongs to THIS process — any canonical rung:
    a procStart-bound pid record (`declared`), an unambiguous ``--resume``
    (`resume`), or ``helm who``'s fresh exact attribution (identity ``who``).

    `who` is canonical here, not a hint: _proc_claude_rows suppresses who
    attribution for child processes (both the CLAUDE_CODE_CHILD_SESSION stamp
    and who's own agent-ancestor marking), and who's session dedupe demotes
    every younger same-session process to a candidate — so a row that SURVIVES
    with identity=='who' carries the process's own exact attribution, never
    its parent's. Treating who rows as inherited hints hid a proven holder
    from live-holder/DOUBLE-OPEN arithmetic (a review's HIGH)."""
    return bool(r.get("declared") or r.get("resume")
                or r.get("identity") == "who")


def _sessionless_oneshot(r):
    """True only when the row PROVES sessionless intent: explicit
    `--no-session-persistence` IN PRINT MODE with no explicit session
    identity of its own. These rows leave every health count AND certify
    green — their missing transcript is the design working, not work at risk.
    Measured 2026-07-22, twenty minutes after this census first certified the
    fleet: the `remember` plugin ran `claude -p --output-format json
    --no-session-persistence` to compress memory, and the estate flipped to a
    memory-only FAIL advising `helm session rescue` on a process that had
    already exited. That row is excluded on its EXPLICIT nonpersistence
    evidence.

    The flag alone is NOT that proof: the CLI documents it '(only works with
    --print)', so in an interactive pane it is INERT and a real session
    persists. The print-mode conjunct is the row's `headless` bit, which
    _proc_claude_rows derives from argv OR from proven stdin redirection —
    the CLI enters print mode on its own when stdin is not a terminal — so a
    piped spawn that omitted `-p` still certifies, while an interactive pane
    mislaunched with the inert flag falls through to UNKNOWN fail-closed.

    Plain `-p` is NOT that proof. Print mode persists a transcript by default,
    so a `-p` row whose SID cannot be resolved is an UNRESOLVED session, not
    an absent one — it fails closed as UNKNOWN in certification like any other
    unresolved row (see _cmd_ls). Whether such a row enters HOLDER arithmetic
    is the separate axis _inherited_hint_worker decides.

    The line that must never move: a row with a CANONICAL session identity of
    its own — pid record, unambiguous ``--resume``, or exact who attribution
    (_own_session_identity) — is a proven holder and stays in every count no
    matter how it interacts or how soon it exits (`--no-session-persistence
    --resume X` still READS X while it lives).

    This narrows WHAT IS COUNTED, never what is allowed: a helm SEAT that
    shows up headless is still a law violation, and it stays visible in
    `session ls` tagged as such rather than being hidden."""
    return bool(r.get("nonpersistent") and r.get("headless")
                and not _own_session_identity(r))


def _inherited_hint_worker(r):
    """True for a print-mode/nonpersistent row with NO canonical session
    identity of its own (identity 'unknown' — no pid record, no unambiguous
    ``--resume``, no exact who attribution): any SID such a row still carries
    is at best inherited/echoed from the pane that spawned it, so it is never
    a SECOND holder of that session. Such rows leave HOLDER arithmetic —
    live_sids/DOUBLE-OPEN and the memory-only pane census — or an ordinary
    background print worker echoing its parent's SID would manufacture a
    false DOUBLE-OPEN against the very pane it serves.

    Exclusion is based on the ABSENCE of every canonical rung, never on the
    interaction mode alone: identity=='who' is the process's own exact
    attribution (who suppresses child processes and dedupes shared sessions —
    see _own_session_identity), so a who-attributed print row stays a holder.

    Deliberately distinct from _sessionless_oneshot: leaving holder
    arithmetic never certifies a row green. Without explicit nonpersistence a
    plain `-p` row with no resolvable SID still fails closed as UNKNOWN —
    print mode normally persists, so 'not a second holder' is a claim about
    double-open arithmetic, never proof of sessionlessness."""
    return bool((r.get("headless") or r.get("nonpersistent"))
                and not _own_session_identity(r))


def _resume_sid(argv):
    """One unambiguous full UUID from the OPTION region of argv. Short
    aliases are expanded first (`-r <sid>`, attached `-r<sid>`, and cluster
    forms like `-pr <sid>` all resume — measured against the real CLI), so a
    holder spawned through the short alias never silently vanishes from the
    census. The scan honors the standard ``--`` terminator under the same law
    as _argv_flag: past it every token is positional prose, so `claude -p --
    --resume <uuid>` carries prompt text, never a session identity — and
    post-terminator prose can never conflict away a real pre-terminator
    resume. The scan walks the same (token, consumed) pairs as _argv_flag —
    one law, never two readings of one argv — so a resume token CONSUMED as
    the value of the option before it is opaque prose: it neither mints a
    holder (`--model -cr <uuid>` names a model, and `--model --resume <uuid>`
    swallows the long flag whole — measured, the CLI parses no resume) nor
    poisons a real one (`--append-system-prompt '-pr be brief' --resume
    <sid>` keeps the proven holder). Only a MEASURED greedy predecessor
    consumes that way: `-d [filter]` is optional-value, so `claude -d
    --resume <uuid> -p` REALLY resumes (measured — `-d --resume BAD -p hi`
    errors 'not a UUID') and the holder stays attributed. `--resume` itself
    is optional-value, so only a CONSUMED neighbor is its value — a
    flag-shaped neighbor commander refuses (`--resume --print`) leaves the
    resume BARE. Within the option region the parse is FAIL-CLOSED over
    every UNCONSUMED resume occurrence, short or long: a bare/trailing
    ``--resume``/``-r``, a prefix or otherwise invalid value (`-r=X` carries
    the literal value `=X`; `-rp` resumes by TITLE "p", unresolvable from
    argv), and conflicting repeats each poison the WHOLE parse — a valid
    occurrence beside an invalid one is contradictory evidence, not a
    majority vote. A false holder is worse than falling through to another
    rung."""
    found = []
    tokens = _expand_options(argv)
    for i, (token, consumed) in enumerate(tokens):
        if consumed:
            continue
        if token == "--":
            break
        if token == "--resume":
            after = tokens[i + 1] if i + 1 < len(tokens) else None
            value = after[0] if after and after[1] else None
        elif token.startswith("--resume="):
            value = token.split("=", 1)[1]
        else:
            continue
        if not (isinstance(value, str) and _SID_RE.fullmatch(value)):
            return None
        found.append(value)
    return found[0] if len(set(found)) == 1 else None


def _safe_owned_dir(value, uid):
    return (stat.S_ISDIR(value.st_mode) and value.st_uid == uid
            and not value.st_mode & stat.S_IWOTH)


def _safe_owned_record(value, uid):
    return (stat.S_ISREG(value.st_mode) and value.st_uid == uid
            and value.st_nlink == 1
            and not value.st_mode & stat.S_IWOTH
            and value.st_size <= _SESSION_RECORD_MAX)


def _config_root(config_dir, home_dir, uid):
    """Canonical process config home from the shared provider rule.

    None means the provider key was absent; an empty string means it was present
    but invalid. ``home_dir`` is the TARGET process's HOME, never the inspector's.
    """
    env = {"HOME": home_dir}
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    root, _source = runtime_config.resolve("claude", env)
    if not root:
        return None
    try:
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
    rec, reason = read_session_presence(root, pid, uid, proc_start)
    return (rec["sessionId"] if rec else None), reason


def read_session_presence(root, pid, uid, proc_start):
    """(record, reason): Claude Code's WHOLE presence record for `pid` under
    config root `root` — the file it rewrites as the session changes state
    (`status` busy/idle/shell/waiting, `waitingFor`, `statusUpdatedAt`) — or
    (None, reason). Same bracket as the session-id read: bounded, owner-bound,
    no link followed, and bound to THIS pid generation by `procStart`, so a
    reused pid never lends a dead session's state to a live one."""
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
    try:
        flags = openflags.flags(
            os.O_RDONLY, "O_NOFOLLOW", "O_NONBLOCK", cloexec=True)
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
    return rec, "record-ok"


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
            "headless": (_is_headless(snap.get("argv") or [])
                         or _stdin_redirected(snap.get("stdin"))),
            "nonpersistent": _is_nonpersistent(snap.get("argv") or []),
        })
    return rows


_GONE_ERRNOS = {errno.ENOENT, errno.ESRCH}


def _gone(err):
    """The pid left mid-scan (genuine absence) vs a failed PROBE on a pid that
    persists (EACCES/EIO/anything else on a mandatory read)."""
    return err.errno in _GONE_ERRNOS


def _full_environ(raw):
    """The bracketed environ, decoded WHOLE, or None when the bracketed read
    failed. Exported on census rows so consumers (fleet's seat/deck/stamp
    columns) read env facts from the SAME process generation the census proved
    coherent, instead of re-opening /proc/<pid>/environ at a later moment a
    reused pid could answer."""
    if raw is None:
        return None
    return dict(kv.split("=", 1) for kv in
                raw.decode("utf-8", "replace").split("\0") if "=" in kv)


def _census_matches(pid, start, cmdline, environ=None, cwd=None):
    """Tri-state sibling of _proc_matches for the census. True = the bracket is
    proven intact; False = a read PROVED a different generation, or the pid
    left mid-recheck (ENOENT/ESRCH) — genuine absence of the bracketed
    generation; None = a mandatory recheck read FAILED (EACCES/EIO/...) while
    the pid persists — a failed PROBE the census surfaces as UNKNOWN, never as
    proven absence. (The plain _proc_matches stays bool-valued for its own
    callers; the census needs the None rung to tell probe-failure from proven
    reuse.)"""
    try:
        # Do not call _proc_start here: its legacy contract intentionally folds
        # every OSError to None. The census must preserve EACCES/EIO as UNKNOWN
        # and distinguish them from a gone pid or a proven generation change.
        live_start = _starttime_from_stat(_proc_bytes(pid, "stat"))
        if live_start is None:
            return None  # stat was read but unparsable: failed probe, not absence
        if live_start != start or _proc_bytes(pid, "cmdline") != cmdline:
            return False
        if environ is not None and _proc_bytes(pid, "environ") != environ:
            return False
        return cwd is None or os.readlink(
            os.path.join(PROC, str(pid), "cwd")) == cwd
    except OSError as e:
        return False if _gone(e) else None


def _census_snapshot(pid):
    """Tri-state sibling of _proc_snapshot for the census, fail-closed — a pass
    whose input was missing is vacuous:

      ("ok", snap)      every mandatory fact proven.
      ("absent", None)  structurally not ours (foreign uid, non-claude comm)
                        or the process left mid-scan (ENOENT/ESRCH) — genuine
                        absence, skipped.
      ("unknown", stub) comm PROVED claude, then a mandatory read failed while
                        the pid persists — the pid is KNOWN, its facts are
                        unprovable; the census surfaces it, never drops it as
                        proven absence.
      ("partial", None) a mandatory read failed BEFORE comm could prove or
                        refute claude on a pid that may be ours — the census
                        cannot certify its own completeness (census_partial).

    (The plain _proc_snapshot stays dict-or-None for its own callers, which
    tri-state would break; this sibling adds the UNKNOWN/PARTIAL rungs the
    fail-closed census needs.)"""
    base = os.path.join(PROC, str(pid))
    try:
        uid = os.stat(base).st_uid
    except OSError as e:
        return ("absent", None) if _gone(e) else ("partial", None)
    if uid != os.geteuid():
        return "absent", None  # foreign uid: structurally not ours
    try:
        start = _starttime_from_stat(_proc_bytes(pid, "stat"))
    except OSError as e:
        return ("absent", None) if _gone(e) else ("partial", None)
    if not start:
        return "partial", None  # stat read but unparsable: a failed probe
    try:
        comm = _proc_bytes(pid, "comm").strip()
    except OSError as e:
        return ("absent", None) if _gone(e) else ("partial", None)
    agent = _is_agent(pid, comm)
    if agent is None:
        # UNDECIDABLE IS NOT ABSENT. This caller already has the third state the
        # census needs — `partial` is what raises census_partial, which turns the
        # row count into a FLOOR instead of a certified estate. Collapsing it to
        # `absent` is how a census certifies a world it could not see.
        return "partial", None
    if not agent:
        return "absent", None
    try:
        cmdline = _proc_bytes(pid, "cmdline")
    except OSError as e:
        if _gone(e):
            return "absent", None
        return "unknown", {"pid": pid, "uid": uid, "start": start}
    try:
        environ_raw = _proc_bytes(pid, "environ")
        environ = _selected_environ(environ_raw)
    except OSError:
        environ_raw = environ = None
    try:
        cwd = os.readlink(os.path.join(base, "cwd"))
    except OSError:
        cwd = None
    try:
        stdin = os.readlink(os.path.join(base, "fd", "0"))
    except OSError:
        stdin = None
    match = _census_matches(pid, start, cmdline, environ_raw, cwd)
    if match is None:
        # the bracket RECHECK failed while the pid persists: comm already
        # PROVED claude, so the pid is KNOWN and its facts are unprovable —
        # UNKNOWN, never a silent drop that certifies absence
        return "unknown", {"pid": pid, "uid": uid, "start": start}
    if not match:
        return "absent", None
    return "ok", {"pid": pid, "uid": uid, "start": start, "cmdline": cmdline,
                  "environ": environ_raw,
                  "argv": cmdline.decode("utf-8", "replace").split("\0"),
                  "env": environ, "cwd": cwd, "stdin": stdin}


def _proc_claude_census():
    """Every same-uid live Claude process, PLUS the completeness of the GLOBAL
    probes the table rests on, PLUS each row's bracket export. The fail-closed
    superset of _proc_claude_rows that helm/fleet.py consumes: identical
    resolution ladder and per-row identity (procStart-bound pid record ->
    unambiguous full ``--resume`` UUID -> exact ``helm who`` attribution -> cwd
    candidate set), but it never turns a failed probe into a proven-empty row.

    ``listing_failed`` True: the /proc enumeration itself failed — zero rows
    because nothing was READ, not because nothing runs; a consumer renders
    estate-UNKNOWN, never certifies an empty estate. ``who_failed`` True: the
    who rung was never probed, so an unresolved non-child row may only look
    unresolved because its strongest remaining rung silently vanished.
    ``census_partial`` True: a mandatory per-pid probe failed BEFORE the pid's
    comm could prove or refute claude — the row count is a FLOOR ("at least
    N"), never a certified estate total. A probe that fails AFTER comm proved
    claude yields a ``probe_failed`` UNKNOWN row (pid known, facts unprovable)
    rather than vanishing. Only ENOENT/ESRCH — the process left mid-scan — is
    genuine absence.

    Each row also exports its bracket: ``start`` (the pid generation every
    fact was proven against — a consumer making LATER /proc reads must re-prove
    it before composing them in), ``cwd``, ``root`` (canonical trusted config
    root; None = config-untrusted) and ``environ`` (the bracketed environ,
    whole, so env facts never need a second unbracketed read)."""
    snapshots, unknown_stubs = [], []
    listing_failed = census_partial = False
    try:
        pids = sorted((int(p) for p in os.listdir(PROC) if p.isdigit()))
    except OSError:
        pids, listing_failed = [], True
    for pid in pids:
        status, snap = _census_snapshot(pid)
        if status == "ok":
            snapshots.append(snap)
        elif status == "unknown":
            unknown_stubs.append(snap)
        elif status == "partial":
            census_partial = True
    who_failed, who_failed_pids = False, set()
    try:
        from . import who
        who_status = {}
        scanned = who.scan(accounts=[], status=who_status)
        who_rows = {r["pid"]: r for r in scanned
                    if r.get("provider") == "anthropic"
                    and r.get("child") is False}
        who_failed = bool(who_status.get("listing_failed"))
        who_failed_pids = set(who_status.get("failed_pids") or ())
    except Exception:
        # broad on purpose: who parses external proc/transcript state, so a
        # malformed row can raise KeyError/TypeError just as plausibly as an
        # OSError. Every shape of failure degrades to who_failed, never a
        # successful negative attribution.
        who_rows, who_failed = {}, True
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
        who_row = who_rows.get(pid, {})
        who_sid = None if child else _who_holder_sid(who_row)
        # PID equality alone is not identity: who must have resolved the SAME
        # bracketed config root and cwd as this census row. Otherwise a process
        # with alternate HOME can inherit the inspector's ~/.claude transcript.
        who_context_mismatch = bool(
            who_sid and (not root or not who_row.get("home")
                         or os.path.realpath(who_row["home"])
                         != os.path.realpath(root)
                         or who_row.get("cwd") != snap["cwd"]))
        attributed = None if who_context_mismatch else who_sid
        who_probe_failed = pid in who_failed_pids
        possible = []
        if (not (child or declared or resume or attributed)
                and root and snap["cwd"]):
            key = (root, snap["cwd"])
            if key not in cwd_candidates:
                cwd_candidates[key] = _cwd_session_ids(*key)
            possible = cwd_candidates[key]
        match = _census_matches(pid, snap["start"], snap["cmdline"],
                                snap["environ"], snap["cwd"])
        if match is None:
            # the mid-loop rebracket FAILED while the pid persists (EACCES/
            # EIO): comm proved claude at snapshot time, so the row must
            # surface as a probe_failed UNKNOWN stub — a silent drop here would
            # read as proven absence, the exact class this census legislates
            # against
            unknown_stubs.append({"pid": pid, "uid": snap["uid"],
                                  "start": snap["start"]})
            continue
        if not match:
            continue
        session_id = declared or resume or attributed
        rows.append({
            "pid": pid,
            "resume": resume,
            "declared": declared,
            "declared_reason": reason,
            # exported composition context (consumed by helm/fleet.py so it can
            # CALL this census instead of re-deriving it): the bracketed cwd,
            # the canonical trusted config root (None = config-untrusted), the
            # bracket generation, and the bracketed whole environ.
            "cwd": snap["cwd"],
            "root": root,
            "start": snap["start"],
            "environ": _full_environ(snap["environ"]),
            "identity": ("declared" if declared else "resume" if resume
                         else "who" if attributed else "unknown"),
            "session": session_id,
            "possible_sessions": possible,
            "child": child,
            "ancestor_sid8": (env.get("CLAUDE_CODE_SESSION_ID") or "")[:8],
            "force": env.get(FORCE_VAR) == "1",
            "headless": (_is_headless(snap.get("argv") or [])
                         or _stdin_redirected(snap.get("stdin"))),
            "nonpersistent": _is_nonpersistent(snap.get("argv") or []),
            "probe_failed": False,
            "who_probe_failed": who_probe_failed,
            "who_context_mismatch": who_context_mismatch,
        })
    for stub in unknown_stubs:
        # comm proved claude, then a mandatory read failed while the pid
        # persisted. Surface the pid as an UNKNOWN row — but only while its
        # GENERATION still persists: a gone pid (ENOENT/ESRCH) is genuine
        # absence, while an unreadable recheck stays fail-closed UNKNOWN.
        try:
            live_start = _starttime_from_stat(
                _proc_bytes(stub["pid"], "stat"))
            # A readable-but-torn stat is still a failed probe. Only a parsed
            # different starttime proves this generation gone.
            still = live_start is None or live_start == stub["start"]
        except OSError as e:
            still = not _gone(e)
        if not still:
            continue
        rows.append({
            "pid": stub["pid"], "resume": None, "declared": None,
            "declared_reason": "probe-failed",
            "cwd": None, "root": None, "start": stub["start"],
            "environ": None, "identity": "unknown", "session": None,
            "possible_sessions": [], "child": False, "ancestor_sid8": "",
            "force": False, "headless": False, "nonpersistent": False,
            "probe_failed": True, "who_probe_failed": False,
            "who_context_mismatch": False,
        })
    rows.sort(key=lambda r: r["pid"])
    return {"rows": rows, "listing_failed": listing_failed,
            "who_failed": who_failed, "census_partial": census_partial}


def _runtime_config_snapshot(pid):
    """One bounded Claude-process bracket for the synchronous hook path.

    No process-table listing, who scan, transcript candidate search, /proc cwd,
    or stdin probe belongs here. The event already owns cwd; this snapshot owns
    only process generation, image, environment, and session-record inputs.
    """
    base = os.path.join(PROC, str(pid))
    try:
        uid = os.stat(base).st_uid
        start = _starttime_from_stat(_proc_bytes(pid, "stat"))
        comm = _proc_bytes(pid, "comm").strip()
    except OSError as e:
        return ("absent", None) if _gone(e) else ("unknown", None)
    if uid != os.geteuid() or not start:
        return "absent", None
    agent = _is_agent(pid, comm)
    if agent is not True:
        return ("absent", None) if agent is False else ("unknown", None)
    try:
        cmdline = _proc_bytes(pid, "cmdline")
        environ_raw = _proc_bytes(pid, "environ")
    except OSError as e:
        return ("absent", None) if _gone(e) else ("unknown", None)
    match = _census_matches(pid, start, cmdline, environ_raw)
    if match is not True:
        return ("absent", None) if match is False else ("unknown", None)
    return "ok", {"pid": pid, "uid": uid, "start": start,
                  "cmdline": cmdline, "environ": environ_raw,
                  "env": _selected_environ(environ_raw),
                  "argv": cmdline.decode("utf-8", "replace").split("\0")}


def runtime_config_for_session(session_id, pid, event_cwd, snapshot=None):
    """One named Claude process generation's hook context, or loud refusal.

    This is deliberately NOT an estate census. CLAUDE_PID names the one process
    whose bracket is read; its procStart-bound session record or exact --resume
    binds the hook SID. Unrelated process failures cannot poison this result.
    The hook event cwd remains the turn/cohort cwd; /proc cwd is not consulted.
    """
    if not session_id:
        return None, "session-unresolved"
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None, "pid-unreadable"
    if pid <= 0:
        return None, "pid-unreadable"
    if not isinstance(event_cwd, str) or not os.path.isabs(event_cwd):
        return None, "event-cwd-unavailable"
    status, snap = snapshot if snapshot is not None else _runtime_config_snapshot(pid)
    if status != "ok" or not isinstance(snap, dict):
        return None, ("agent-pid-unavailable" if status == "absent" else
                      "agent-runtime-unavailable")
    if snap.get("pid") != pid:
        return None, "pid-mismatch"
    env, start = snap.get("env"), snap.get("start")
    if not isinstance(env, dict) or not isinstance(start, str) \
            or not start.isdigit():
        return None, "agent-runtime-unavailable"
    declared, _reason, root = _session_record(
        pid, env.get("CLAUDE_CONFIG_DIR"), env.get("HOME"), snap.get("uid"),
        start)
    resume = _resume_sid(snap.get("argv") or [])
    identities = {sid for sid in (declared, resume) if sid}
    if len(identities) > 1:
        return None, "session-ambiguous"
    if not identities:
        return None, "session-unresolved"
    if session_id not in identities:
        return None, "session-mismatch"
    resolved, source = runtime_config.resolve("claude", env)
    if not resolved or not root or os.path.realpath(root) != resolved:
        return None, "config-unproven"
    match = _census_matches(pid, start, snap.get("cmdline"),
                            snap.get("environ"))
    if match is not True:
        return None, ("pid-generation-changed" if match is False else
                      "agent-runtime-unavailable")
    return {"pid": pid, "start": start, "session": session_id,
            "cwd": os.path.realpath(event_cwd), "harness": "claude",
            "config_home": root, "config_home_source": source}, None


def live_sids(rows=None):
    """{sid: [pid,...]} of proven live copies. The procStart-bound pid record
    wins, then canonical resume/who evidence. An inherited stamp SID is only the
    spawning ancestor and never enters this map, and neither does a print-mode
    worker with NO canonical identity of its own — a background worker echoing
    its parent's SID must not manufacture a false DOUBLE-OPEN. But headless is
    only an interaction mode: a print-mode row with a canonical pid-record /
    ``--resume`` / exact-who identity is a proven live holder and MUST enter,
    or a real overlapping holder hides from the certifier while the launch
    guard still sees it (see _inherited_hint_worker)."""
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


def held_or_unproven(sid):
    """False ONLY when the census PROVES no live claude process holds `sid`.

    This is law 1's own answer read the other way round: `open_pids` empty on
    a census read whole is what lets a resume open `sid` again, and the same
    measurement is what says a seat's recorded session no longer has a pane.
    The identity law's claim-jump asks it (seats_identity), because a roster
    row keeps its last session after that session's pane is gone: a seat
    relaunched onto a FRESH session still names its dead predecessor, and
    reading that record as a live occupant refused the new pane's own join and
    every `helm chat wait --follow` it armed. The waiter printed the refusal
    and exited before `beacons.arm` registered anything, so the beacon census
    read a live pane as DEAF and dispatch would not book work to it.

    True for everything else, which keeps that refusal standing:
      * a row whose session IS `sid`, or a candidate row that could be holding
        it (an unattributed claude whose cwd holds its transcript),
      * a census that raised, answered without its contract keys, could not
        list the process table, stopped before a pid's comm proved or refuted
        claude, or lost its `helm who` rung,
      * any row carrying a documented uncertainty marker, since a row whose
        facts could not be probed may be the holder.
    An unreadable census is not an empty one, so it never answers False. The
    contract keys and the markers are the claim ledger's pinned copies
    (seats_claims), read rather than restated so one schema pin covers both."""
    if not sid:
        return False
    try:
        from .seats_claims import _CENSUS_KEYS, _ROW_UNCERTAIN
        census = _proc_claude_census()
        if not isinstance(census, dict) \
                or any(k not in census for k in _CENSUS_KEYS):
            return True
        rows = census.get("rows")
        if not isinstance(rows, (list, tuple)) \
                or any(census.get(k) is not False for k in _CENSUS_KEYS
                       if k != "rows"):
            return True
        if any(not isinstance(r, dict) or any(r.get(k) for k in _ROW_UNCERTAIN)
               for r in rows):
            return True
        return bool(open_pids(sid, list(rows)))
    except Exception:          # noqa: BLE001 — a census that cannot answer
        return True            # proves nothing, and nothing never opens this


def memory_only_panes(rows=None, persisting=None):
    """Proven live AGENT PANES with NO transcript on disk. Two exclusions, and
    they are different in kind:

    UNKNOWN candidate rows are excluded because they are neither a persistence
    PASS nor memory-only — a check that passes on absent input reports the
    opposite of the truth.

    Print-mode workers with no canonical session identity of their OWN are
    excluded because they are not panes: an explicitly nonpersistent one-shot
    has no transcript by design, and an inherited-hint worker's transcript
    risk belongs to the pane that actually holds that session. But a
    print-mode row with a CANONICAL identity — pid record, ``--resume``, or
    exact who attribution — stays: headless is an interaction mode, and print
    mode persists unless nonpersistence was requested outright (see
    _inherited_hint_worker)."""
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
    writing a multi-MB transcript (two codex seats at 2.9MB and 2.0MB). A
    persistence surface that lies about the seats is worse than none — it is
    what the fleet uses to decide whether an agent's work is safe to lose."""
    out, complete = {}, True
    # Known bound (documented, accepted): the catalog GLOBS its roots, so a
    # missing/unreadable/offline store (e.g. a network-mounted ~/.claude-homes)
    # reads as [] — indistinguishable from legitimately empty — and `complete`
    # stays True. Accepted because transcript proof is gc's LAST tier: the
    # presence window and the FAIL-CLOSED live-process scan both precede it,
    # so a live seat with an invisible store is still kept; the worst case
    # (idle past REAP_S, no live process, store offline) loses only the
    # roster row + cursors, and the next join self-heals at EOF. A
    # mount-aware completeness probe would false-trip every host without
    # per-account stores, which is the common shape.
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
    # THE CENSUS, NOT THE LOSSY ROWS, because this surface is one of the three
    # the outage was reported through and it must be able to say "I could not
    # see everything". `_proc_claude_rows` returns a bare list: a pid whose comm
    # looks versioned and whose exe we were not PERMITTED to read is simply not
    # in it, and the headline then counts what remains and calls that the
    # estate. The census carries the same rows PLUS its completeness, and
    # `census_partial` is exactly the flag that turns a count into a FLOOR.
    # Rows the census could not probe at all are excluded from the body — that
    # is the population `_proc_claude_rows` has always rendered — so they are
    # reported in the marker instead of vanishing.
    # THE BODY COMES FROM ITS ORIGINAL SOURCE, UNCHANGED. A first draft swapped
    # it for the census rows minus `probe_failed` and asserted in its own commit
    # message that this "is the population _proc_claude_rows always rendered".
    # IT IS NOT — the headless-census arms proved it, and the way I convinced
    # myself was comparing key sets and COUNTS on a live box where the two
    # happened to agree at 22 because it held ZERO of the differing case. A
    # population containing none of the difference cannot test the difference.
    #
    # So the swap is reverted and only the FLAGS come from the census. That
    # costs a second /proc walk on a CLI nobody runs in a loop, and it buys an
    # unchanged rendered population — the smaller change I should have made
    # first.
    rows = _proc_claude_rows()
    _census = _proc_claude_census()
    _floor = bool(_census["listing_failed"] or _census["census_partial"])
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
    # (memory-only, double-open) excludes every print-mode row with no
    # CANONICAL session identity of its own (_inherited_hint_worker) — a
    # worker echoing its parent's SID is not a second holder. But
    # CERTIFICATION green is earned only by proof: an explicitly nonpersistent
    # one-shot (_sessionless_oneshot) is sessionless by request; a plain `-p`
    # row with no resolvable SID is an UNRESOLVED session — print mode
    # persists by default — and fails closed as UNKNOWN. A print-mode row
    # with a canonical pid-record/--resume/exact-who identity is a proven
    # holder that stays in every count. Excluded rows stay rendered below;
    # exclusion narrows what is COUNTED, never what the operator can SEE.
    mo = memory_only_panes(rows, persisting)
    # An inherited-hint worker's transcript alarm belongs to its HOLDER — but
    # only a holder actually PRESENT in the census can carry it. A
    # transcriptless hint row with no live holder anywhere (impossible today:
    # _proc_claude_rows only sets `session` from canonical rungs) is an
    # impossible shape, and impossible shapes fail closed as UNKNOWN rather
    # than green — nobody else is left to alarm.
    holders = {r["session"] for r in rows
               if r.get("session") and _own_session_identity(r)}
    orphan_hint = {r["pid"] for r in rows
                   if _inherited_hint_worker(r) and r.get("session")
                   and disk.get(r["pid"]) is False
                   and r["session"] not in holders}
    unknown = [r for r in rows if not _sessionless_oneshot(r)
               and (not r.get("session") or disk.get(r["pid"]) is None
                    or r["pid"] in orphan_hint)]
    live = live_sids(rows)
    dbl = {s: ps for s, ps in live.items() if len(ps) > 1}
    print("helm session ls — %d live claude panes%s"
          % (len(rows),
             " (AT LEAST — the scan could not classify every process, so this "
             "is a FLOOR, not the estate)" if _floor else ""))
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
            # an interactive pane mislaunched with the inert flag lands here:
            # the flag proves nothing outside print mode, so its session is
            # UNRESOLVED, never sessionless — say why so the operator can fix
            # the launch instead of trusting a label
            if r.get("nonpersistent") and not r.get("headless"):
                detail += ("; --no-session-persistence is inert outside "
                           "print mode")
            state = "UNKNOWN (sid unresolved%s — verify by hand)" % detail + why
        elif disk[r["pid"]] is True:
            state = "persisted" + why
        elif disk[r["pid"]] is None:
            state = "UNKNOWN (transcript census incomplete — verify by hand)" + why
        elif r["pid"] in orphan_hint:
            # the render must agree with the UNKNOWN count above: with no
            # live holder present, "risk belongs to its holder" would name
            # nobody, and certifying green on an impossible shape is the
            # dangerous direction
            state = ("UNKNOWN (inherited session hint with no live holder "
                     "in the census — verify by hand)")
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
        print("helm session resume: launch failed: " + cerr, file=sys.stderr)
        return rc
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
    """Matching lines from the EXPERT's own transcript. Missing/unreadable
    input stays distinct from a measured no-hit before the context-pack rung."""
    try:
        from . import sessions
        row = next((r for r in sessions.rows_for(include_synthetic=True)
                    if r["i"] == sid), None)
    except Exception as e:
        return {"hits": "", "unavailable": {
            "stage": "expert-transcript", "kind": "catalog",
            "message": "expert transcript catalog unavailable (%s)" % type(e).__name__}}
    path = (row or {}).get("p")
    if not path or not os.path.exists(path):
        return {"hits": "", "unavailable": {
            "stage": "expert-transcript", "kind": "missing",
            "message": "expert transcript unavailable"}}
    try:
        found = subprocess.run(["/usr/bin/grep", "-iF", "-m", str(cap), "--", q, path],
                               capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return {"hits": "", "unavailable": {
            "stage": "expert-transcript", "kind": "timeout",
            "message": "expert transcript grep timed out"}}
    except OSError as e:
        return {"hits": "", "unavailable": {
            "stage": "expert-transcript", "kind": "exec",
            "message": "expert transcript grep unavailable (%s)" % type(e).__name__}}
    if found.returncode not in (0, 1):
        return {"hits": "", "unavailable": {
            "stage": "expert-transcript", "kind": "exit",
            "message": "expert transcript grep failed (exit %s)" % found.returncode,
            "returncode": found.returncode}}
    hits = []
    for line in found.stdout.splitlines():
        i = line.lower().find(q.lower())
        hits.append("  …" + line[max(0, i - 80): i + 140].strip() + "…")
    return {"hits": "\n".join(hits[:cap])}


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
    domain = args[0]
    q, rc = freetext.tail("helm session", "ask", args[1:], "a question")
    if rc is not None:
        return rc
    q = q or ""
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
    searched = _grep_expert(sid, q)
    shown = searched["hits"]
    if shown:
        print("expert-transcript hits:\n" + shown)
    else:
        pack_q = _pack_query(domain, q)
        rc, out, _ = _cv("pack", "--limit", "3", pack_q)
        if rc == 0 and out.strip():
            state = ("expert transcript unavailable — context pack:"
                     if searched.get("unavailable")
                     else "no hit in the expert's own transcript — context pack:")
            print("(" + state + ")\n" + out.rstrip())
        elif searched.get("unavailable"):
            print("(expert transcript unavailable: %s)" %
                  searched["unavailable"]["message"])
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
    # with "no session id starts with '--help'" (dogfood).
    if any(a in ("--help", "-h") for a in args[1:]):
        print(USAGE)
        return 0
    try:
        return _VERBS[args[0]](args[1:])
    except (OSError, ValueError) as e:
        print("helm session: %s" % e, file=sys.stderr)
        return 2
