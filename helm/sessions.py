#!/usr/bin/env python3
"""helm sessions — every local claude + codex session, keyed to your projects.

NOT all harnesses. The catalog indexes exactly two transcript formats —
catalog._files() globs the claude and codex roots, and _row()/_session_id()
decode only those two shapes — so opencode and pi sessions, though scanned by
harnesses.all_observations() and therefore present in `helm projects`, have no
row source here. Say claude + codex when describing this verb; "all harnesses"
is true of the auto-map, not of the catalog.

Do NOT explain that scope by what helm can RESUME: helm mints pi resume
commands too (`helm pi resume --session|--continue`, pi.py). The boundary is
which transcript formats the catalog can read, nothing else.

The catalog is the row source; the registry is the lens:
sessions group under the helm-known project whose tree their cwd lives in, so
"what was I doing on that project?" is one verb. Resume stays one copy-paste away —
the exact command, not a wrapper (it's just the harness's own CLI).
"""
import contextlib
import glob
import io
import os
import shlex
import stat
import sys
import threading
import time

from . import procid
from . import registry
from . import pk


def _project_lens():
    """[(name, prefix)] longest-prefix-first, so nested repos match before
    parents. Uses the registry's full cv_scope prefix set — sibling-dir
    worktree cwds don't share the canonical root's prefix."""
    try:
        reg = registry.load()
    except (OSError, ValueError):
        reg = {"projects": {}}
    pairs = []
    for p in reg["projects"].values():
        if not p.get("path") or p.get("external"):
            continue
        prefixes = (p.get("cv_scope") or {}).get("cwd_prefixes") or [p["path"]]
        pairs += [(p["name"], pre) for pre in prefixes]
    return sorted(pairs, key=lambda t: -len(t[1]))


def _project_for(cwd, lens):
    cwd = os.path.expanduser(cwd or "")
    for name, path in lens:
        if cwd == path or cwd.startswith(path + "/"):
            return name
    return None


def rows_for(project=None, include_synthetic=False, limit=None):
    """Catalog rows via transcripts.get_catalog() — the single-flight cached
    path every other consumer (/api/catalog, /api/burn, the CLI) shares, so
    /api/sessions never re-spawns a full catalog build per GET. Same rows,
    plus cwd overrides applied. Rows are COPIED before the project annotation —
    the cache's row objects are shared and must never be mutated here."""
    from . import transcripts
    rows = transcripts.get_catalog()["rows"]
    lens = _project_lens()
    out = []
    for r in rows:
        proj = _project_for(r.get("cwd") or r.get("c"), lens)
        if project and proj != project:
            continue
        if not include_synthetic and r.get("syn"):
            continue
        out.append(dict(r, project=proj))
        if limit and len(out) >= limit:
            break
    return out


BINDINGS = os.path.expanduser("~/.helm/_global/session-creds.tsv")

# ── REQUEST SCOPE ────────────────────────────────────────────────────────
# `credhome_for` answers ONE session, and its three inputs — the live-pane map,
# the home list and the latch index — are each WHOLE-MACHINE reads that do not
# depend on the sid being asked about. A caller resolving one session pays for
# them once and nobody notices. A caller resolving a PAGE of sessions pays for
# them once PER ROW, and the /proc walk inside live_sids is pure CPU under the
# GIL, so the cost is not merely the caller's: it is the whole threaded server's
# for the duration.
#
# A scope makes the three reads ONCE and serves the same answer to every
# lookup inside it. That is not only cheaper, it is more honest: without it a
# page's first row and its last row are answered from machine states seconds
# apart, and the payload describes no instant that ever existed.
#
# THREAD-LOCAL because the server is threaded and a scope belongs to the
# request that opened it, never to its neighbours. Entering is explicit: every
# caller outside a scope keeps the unmemoized behaviour it has today.
_scope = threading.local()


@contextlib.contextmanager
def snapshot():
    """Resolve many sessions against ONE reading of the machine.

    Inside the block, `live_sids()`, `cred_homes()` and `_bindings()` are each
    computed on first use and reused. Re-entrant: a nested scope joins the
    outer one rather than taking a second reading, so a helper that opens its
    own scope stays correct when called from inside one.

    A scope is a snapshot of what can be OBSERVED, never of what is WRITTEN —
    `record_binding` updates the memoized index as it appends, so a write made
    inside a scope is visible to the rest of it."""
    if getattr(_scope, "box", None) is not None:
        yield                                  # nested: the outer reading holds
        return
    _scope.box = {}
    try:
        yield
    finally:
        _scope.box = None


def _memo(key, build):
    """`build()`, or the scope's remembered answer for `key` when one is open."""
    box = getattr(_scope, "box", None)
    if box is None:
        return build()
    if key not in box:
        box[key] = build()
    return box[key]


# The catch-all home, named once. credhome_for has to know which entry in
# cred_homes() is the fallback, and deriving that from ~ while the home LIST
# comes from elsewhere lets the two disagree silently — the fallback stops
# being recognised as one and starts winning lookups it should lose.
DEFAULT_HOME = "~/.claude"


def cred_homes():
    """Every claude credential home on this machine, DEREFERENCED and deduped.
    Read once per `snapshot()` scope; see `_read_cred_homes` for the read."""
    return _memo("cred_homes", _read_cred_homes)


def _read_cred_homes():
    """The listdir+glob+realpath sweep behind `cred_homes`.

    ~/.claude-homes carries a short alias symlink beside each real home
    (cto-example -> cto-example-invalid, owner -> owner-example-invalid, …),
    so a naive listdir double-counts every account and makes a one-account
    question look like a two-account one. realpath collapses the pair."""
    roots = [os.path.expanduser(DEFAULT_HOME)]
    homes_dir = os.path.expanduser("~/.claude-homes")
    if os.path.isdir(homes_dir):
        roots += [os.path.join(homes_dir, n) for n in sorted(os.listdir(homes_dir))]
    # proxy seats keep genuinely isolated homes (their projects/ is a REAL dir,
    # not a symlink into the shared one) — they belong in the same search
    seats = os.path.expanduser("~/.helm/_global/seats")
    for pat in ("*/claude", "*/instances/*/claude"):
        roots += sorted(glob.glob(os.path.join(seats, pat)))
    seen, out = set(), []
    for r in roots:
        real = os.path.realpath(r)
        if real not in seen and os.path.isdir(real):
            seen.add(real)
            out.append(real)
    return out


# How much a binding can be trusted, highest first. A binding may only ever be
# OVERWRITTEN BY A STRICTLY BETTER SOURCE — the first version of this index had
# no ranking, so the first guess to arrive won permanently and was consulted
# ahead of the live signal that would have corrected it. A cache that can freeze
# a guess forever and then shadow the truth is worse than no cache.
AUTHORITY = {"environ": 3, "pidrecord": 2, "session-env": 1}


def _bindings():
    """{sid: (home, source)}. Plain TSV because this is append-mostly, read-hot,
    and must stay repairable by eye. Later rows win only if better-sourced.

    Parsed once per `snapshot()` scope. The unscoped read is cheap for one
    lookup and proportional to the FILE for every lookup, which is what made
    `record_binding`'s idempotence guard — a read — cost a whole file parse per
    row on a page."""
    return _memo("bindings", _read_bindings)


def _read_bindings():
    out = {}
    try:
        with open(BINDINGS) as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 2 or not parts[0] or not parts[1]:
                    continue
                sid, home = parts[0], parts[1]
                src = parts[2] if len(parts) > 2 else "session-env"
                prev = out.get(sid)
                # STRICTLY greater wins; an equal-authority later row never
                # overwrites (same-source conflicts must not flip the answer by
                # scan/append order — first claim at a tier holds until a
                # genuinely better tier speaks)
                if prev is None or AUTHORITY.get(src, 0) > AUTHORITY.get(prev[1], 0):
                    out[sid] = (home, src)
    except OSError:
        pass
    return out


def record_binding(sid, home, source="session-env"):
    """Freeze a sid -> credhome binding. Best-effort by design: a failure here
    must never break the read that triggered it. A weaker source never
    overwrites a stronger one, and an identical row is not rewritten."""
    if not sid or not home:
        return False
    prev = _bindings().get(sid)
    if prev:
        if prev == (home, source):
            return False                      # idempotent re-record
        if AUTHORITY.get(source, 0) <= AUTHORITY.get(prev[1], 0):
            # equal authority with a DIFFERENT home is a CONFLICT, not an
            # update — refuse, keep the first claim (xrev: >= here let
            # environ-B silently overwrite environ-A)
            return False
    try:
        os.makedirs(os.path.dirname(BINDINGS), exist_ok=True)
        with open(BINDINGS, "a") as f:
            f.write("%s\t%s\t%s\n" % (sid, home, source))
    except OSError:
        return False
    # An open scope holds a PARSE of this file, and the row just appended is
    # now missing from it. Carrying the write into the memo keeps the guard
    # above honest for the rest of the scope — otherwise a second record of the
    # same sid inside one scope re-reads the pre-append world, re-appends a row
    # that is already there, and counts it as new.
    box = getattr(_scope, "box", None)
    if box is not None and "bindings" in box:
        box["bindings"][sid] = (home, source)
    return True


def proc_home(pid):
    """The home a LIVE pane is ACTUALLY using, read from its own environment.

    This is ground truth and needs no inference: CLAUDE_CONFIG_DIR is fixed at
    exec and /proc/<pid>/environ reports exactly what the process got. Its
    ABSENCE is equally decisive — it means the pane runs on the default home.
    Every other rung here is archaeology by comparison, and skipping this one is
    how a live pane whose account is stated outright got attributed by guesswork
    instead."""
    try:
        with open(os.path.join(procid.proc_root(), str(pid), "environ"), "rb") as f:
            env = f.read().decode("utf-8", "replace").split("\0")
    except OSError:
        return None
    for kv in env:
        if kv.startswith("CLAUDE_CONFIG_DIR="):
            val = kv.split("=", 1)[1]
            return os.path.realpath(val) if val else None
    return os.path.realpath(os.path.expanduser(DEFAULT_HOME))


def latch_live():
    """Bind every LIVE pane's session to its home from the AUTHORITATIVE record.

    claude itself writes <home>/sessions/<pid>.json holding {"pid","sessionId"},
    and that file sits in the real home (sessions/ is not one of the symlinked
    dirs), so it is ground truth rather than inference. It exists only while the
    pane lives, which is exactly why it must be harvested rather than queried:
    catch a session while it is running and its account is known forever; miss
    it and you are back to the decaying session-env signal. Returns the number
    of NEW bindings frozen."""
    with snapshot():
        return _latch_live()


def _latch_live():
    """latch_live's sweep, under the caller's scope. Split out so the whole
    harvest resolves against ONE reading: the guard inside `record_binding` is
    a read of the index, and re-parsing it once per session record made the
    harvest cost grow with the SQUARE of what it had already recorded."""
    n = 0
    for home in cred_homes():
        for path in glob.glob(os.path.join(home, "sessions", "*.json")):
            try:
                import json
                with pk.open_regular(path) as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            if record_binding(rec.get("sessionId"), home, "pidrecord"):
                n += 1
    # the strongest rung last so it OVERWRITES anything weaker already recorded
    for sid, pid in live_sids().items():
        h = proc_home(pid)
        if h and record_binding(sid, h, "environ"):
            n += 1
    return n


def _pid_is_claude(pid, want_start=None):
    """True only when /proc/<pid> is a LIVE claude process, and — when the
    record carries procStart — the SAME incarnation of it.

    Mere pid existence proves nothing: pids recycle, and a stale
    sessions/<pid>.json can point at whatever now wears the number (xrev repro:
    a `sleep` with a hostile CLAUDE_CONFIG_DIR in its environ was claimed as a
    live session, its unrelated home was then read as rung-0 environ TRUTH and
    latched — a silent wrong-account resume, the exact failure this lane
    exists to prevent). procid answers WHAT the pid is; procStart (stat field
    22, position 19 after the comm split) answers WHICH incarnation.

    WHAT-IT-IS IS NO LONGER COMM ALONE. comm is the exec'd binary's BASENAME,
    so a pane that execs `.../claude/versions/<semver>` directly is named for
    the semver and was rejected here — measured 2026-08-06 on a live pane whose
    own valid sessions/<pid>.json was then read as holding nothing, and whose
    seat was reported DEAD mid-turn. `procid.is_claude` adds the kernel's own
    `/proc/<pid>/exe` as a SECOND ACCEPT PATH.

    THAT IS A WIDENING AND NOT A HARDENING: is_claude ORs, so the hostile
    `sleep` of the repro above is still accepted on the comm rung it can set
    for itself. The spoof surface is UNCHANGED. This paragraph previously said
    the opposite, attached to the very repro it does not defend.

    is_claude may also answer None (cannot tell). Folded to False HERE
    DELIBERATELY: the pids reaching this function come from OUR OWN cred-home
    session records, so their exe is readable and the None case is not
    reachable by that route. The honest tri-state matters where the /proc WALK
    meets other users' pids, and it is spent there — orcaadopt maps None to
    BLIND rather than to "not a claude process"."""
    try:
        with open(os.path.join(procid.proc_root(), str(pid), "comm"), "rb") as f:
            comm_raw = f.read()
    except OSError:
        comm_raw = None
    if procid.is_claude(pid, comm_raw) is not True:
        return False
    if not want_start:
        return True
    try:
        with open(os.path.join(procid.proc_root(), str(pid), "stat"), "rb") as f:
            fields = f.read().decode("utf-8", "replace").rpartition(")")[2].split()
        return fields[19] == str(want_start)
    except (OSError, IndexError):
        return False


def live_sids():
    """{sid: pid} for every session a pane is CURRENTLY holding open.

    Walked once per `snapshot()` scope. The walk reads every cmdline in /proc
    plus every cred home's session records; it is pure CPU, so under the GIL a
    caller repeating it per row stalls every other thread in the process for as
    long as the loop runs."""
    return _memo("live_sids", _read_live_sids)


def _read_live_sids():
    """The /proc walk behind `live_sids`.

    Resume has to ask this because a session is not a file you open twice: two
    panes on one sessionId interleave their writes into the same transcript and
    each silently loses turns to the other (the double-open certify-fleet's law
    1 exists to catch). Transcript mtime cannot answer it — a pane that has been
    thinking for an hour looks identical to one that exited an hour ago — so the
    answer comes from the pid-keyed record plus a liveness check on the pid it
    names, never from the file's age."""
    import json
    out = {}
    for home in cred_homes():
        for path in glob.glob(os.path.join(home, "sessions", "*.json")):
            try:
                with pk.open_regular(path) as f:
                    rec = json.load(f)
                pid = int(rec.get("pid") or 0)
            except (OSError, ValueError, TypeError):
                continue
            if pid and rec.get("sessionId") and \
                    _pid_is_claude(pid, rec.get("procStart")):
                out[rec["sessionId"]] = pid
    # SECOND RUNG, different failure mode. The pid-keyed record is the better
    # signal but it is not universal: panes launched by a claude older than the
    # feature never write one, and those are precisely the longest-running panes
    # — the ones most likely to still be open and most overdue for a relaunch.
    # A guard whose only rung is the record is therefore blindest exactly where
    # a double-open is most likely. argv carries `--resume <sid>` for any pane
    # started that way, so it covers the gap without depending on the same file.
    for entry in glob.glob(os.path.join(procid.proc_root(), "[0-9]*", "cmdline")):
        # THE PID COMES FROM THE PATH'S OWN SHAPE, not from a fixed component
        # index: `entry.split("/")[2]` only ever meant "pid" while the root was
        # literally "/proc", so it silently addressed the wrong component the
        # moment the root became injectable.
        try:
            pid = int(os.path.basename(os.path.dirname(entry)))
        except ValueError:
            continue
        try:
            with open(entry, "rb") as f:
                argv = f.read().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        # argv[0] IS THE WEAKEST OF THE THREE and stays only as a fast accept:
        # a versioned-launch pane's argv[0] is the binary PATH, which does not
        # end in "claude", and `/tmp/bash-claude` does. The kernel's exe link
        # settles both cases.
        try:
            with open(os.path.join(procid.proc_root(), str(pid),
                                   "comm"), "rb") as cf:
                comm_raw = cf.read()
        except OSError:
            comm_raw = None
        # `is not False`, with comm in hand: a versioned-comm pane whose exe
        # read is refused is UNKNOWN, and an unknown pane must stay HELD —
        # folding it out of this map is what licensed the false DEAD verdict
        # and the duplicate resume
        if not argv or not (argv[0].endswith("claude")
                            or procid.is_claude(pid, comm_raw) is not False):
            continue
        for flag in ("--resume", "-r"):
            if flag in argv:
                i = argv.index(flag)
                if i + 1 < len(argv) and argv[i + 1] and argv[i + 1] not in out:
                    out[argv[i + 1]] = pid
                break
    return out


def credhome_for(sid, latch=True):
    """The credential home that OWNS this session, or None.

    WHY THIS IS NOT DERIVABLE FROM THE TRANSCRIPT PATH: every OAuth credhome
    symlinks projects/ -> ~/.claude/projects, so all their transcripts pile
    into ONE shared directory and the path says nothing about the account.
    (Proxy-seat homes do keep a real projects/ dir — for those the path would
    work — but one rule has to cover both.)

    session-env/<sid> is the per-home artifact that does NOT follow that
    symlink, so it carries the mapping. But it DECAYS — claude prunes it, and
    measured on this machine resolution falls from ~91% for sessions touched in
    the last 2 days to ~18% past 30 days. A signal that erodes cannot answer
    "resume ANY session on ANY cred", so every successful lookup is LATCHED
    into a helm-owned index that never prunes. Coverage then freezes at what we
    could see rather than decaying to nothing, and no new hook is needed —
    reads are frequent enough to be the capture path.

    ~/.claude is consulted LAST and only as a fallback: it accumulates entries
    for sessions that also belong to a named home, so preferring it would
    mis-attribute a named-home session to the default account."""
    # RUNG 0 — a live pane STATES its home; never infer what you can read.
    pid = live_sids().get(sid)
    if pid is not None:
        h = proc_home(pid)
        if h and os.path.isdir(h):
            if latch:
                record_binding(sid, h, "environ")
            return h
    latched = _bindings().get(sid)
    if latched and os.path.isdir(latched[0]):
        return latched[0]
    # RUNG 2 — session-env, and it is NOT EXCLUSIVE. Measured live: one sid was
    # claimed by THREE homes (a default-home pane also had entries under two
    # named homes, written a day later). Returning the first non-default hit made
    # the answer depend on listdir ORDER, and it picked a home the pane had never
    # run on. The creating home is the one whose entry is OLDEST — it was written
    # when the session began — so rank by mtime instead of by iteration order.
    default_home = os.path.realpath(os.path.expanduser(DEFAULT_HOME))
    claims = []
    for home in cred_homes():
        entry = os.path.join(home, "session-env", sid)
        try:
            claims.append((os.path.getmtime(entry), home))
        except OSError:
            continue
    if not claims:
        return None
    claims.sort()
    found = claims[0][1]
    fallback = default_home if any(h == default_home for _, h in claims) else None
    found = found or fallback
    if found and latch:
        record_binding(sid, found, "session-env")
    return found


def resume_command(row, home=None):
    """The harness's own resume invocation. claude resume is cwd-scoped, so the
    command carries the cd; codex resume is global-by-UUID (the cd is comfort).
    A paste-for-human command must be safe in a STAMPED shell: an inherited
    CLAUDE_CODE_CHILD_SESSION/SID/bridge id would make the resumed session a
    subprocess child with transcript persistence silently OFF
    (child-stamp-kills-seat-persistence) — so the line unsets the trio first.

    It also PINS CLAUDE_CONFIG_DIR to the owning home. Without the pin this
    command does not fail on the wrong account — it silently SUCCEEDS on it:
    the shared projects/ symlink means the transcript resolves from any home,
    so the session simply continues under whichever cred the calling shell
    carried. A silent re-home is worse than an error, because nothing ever
    reports it."""
    cwd = os.path.expanduser(row.get("cwd") or "") or "."
    return "cd %r && %s" % (cwd, resume_exec(row, home=home))


def trust_blocked(row, home):
    """The home, when it has NOT accepted the trust dialog for this session's
    cwd — else None.

    This is the second way a resume can look like it worked and deliver
    nothing. claude asks "do you trust the files in this folder?" on first use
    of a directory, and renders that prompt into an ALTERNATE SCREEN BUFFER,
    which the metaharness tail does not capture: the pane reads as blank and
    the process reads as alive, so every liveness signal says healthy while the
    session never loads. Checking the recorded flag turns an invisible hang
    into a refusal that names its own remedy."""
    if row.get("h") != "claude" or not home:
        return None
    cwd = os.path.expanduser(row.get("cwd") or "")
    try:
        import json
        with pk.open_regular(os.path.join(home, ".claude.json")) as f:
            proj = (json.load(f).get("projects") or {}).get(cwd)
    except (OSError, ValueError):
        return None
    if proj is not None and not proj.get("hasTrustDialogAccepted"):
        return home
    return None


def resume_exec(row, home=None, skip_permissions=False):
    """The resume invocation WITHOUT the cd prefix — the part that is safe to
    hand to `exec`.

    Kept separate because gluing `exec` onto the front of the combined
    "cd X && claude …" line silently breaks it: exec binds to `cd`, a shell
    builtin, so the exec fails and the && chain never runs. The pane opens,
    dies immediately, and the spawn still returns a handle — a resume that
    reports success and delivers nothing."""
    from . import seat
    unset = seat.paste_unset_prefix()
    if row["h"] != "claude":
        return "%scodex resume %s" % (unset, row["i"])
    if home is None:
        home = credhome_for(row["i"])
    pin = ("CLAUDE_CONFIG_DIR=%s " % home) if is_pinnable(home) else ""
    skip = " --dangerously-skip-permissions" if skip_permissions else ""
    return "%s%sclaude --resume %s%s" % (unset, pin, row["i"], skip)


def is_pinnable(home):
    """Whether CLAUDE_CONFIG_DIR may be pinned to this home.

    The DEFAULT home is the one place where pinning is wrong. ~/.claude is not
    a credential home — it is where claude keeps state when CLAUDE_CONFIG_DIR is
    UNSET, and in that mode the onboarding marker lives one level up in
    ~/.claude.json. Pin CLAUDE_CONFIG_DIR=~/.claude and claude looks for that
    marker at ~/.claude/.claude.json instead, finds a 1.2KB stub with no
    hasCompletedOnboarding, and opens the FIRST-RUN WIZARD rather than the
    session. Measured live: the pane sat on the theme picker forever, alive and
    doing nothing, having reported a successful resume.

    So for the default account, faithfully reproducing the original environment
    means pinning NOTHING. Named homes and proxy-seat homes carry their own
    onboarded .claude.json and pin correctly."""
    if not home:
        return False
    return os.path.realpath(home) != os.path.realpath(os.path.expanduser(DEFAULT_HOME))


RESUME_DIR = os.path.expanduser("~/.helm/_global/resumes")


def mint_resume_script(row, home=None, skip_permissions=False, env=None):
    """Write the resume as an executable SCRIPT and return its path.

    TOKEN LAW (borrowed intact from seat.py's resume): what crosses the
    metaharness seam is a PATH, never an expanded command line. A seat's launch
    line carries its proxy token, and a pane command is visible in adapter
    listings, window titles and logs — so the secret must stay in a file the
    adapter only ever names. Native OAuth resumes carry no token, but they go
    through the same door: one rule, no per-caller judgement about whether
    today's line happens to be safe.

    `env` EXPORTS identity vars into the resumed pane (HELM_CHAT_NAME for an
    adopted seat, so it comes back AS that seat rather than as an anonymous
    pane). It rides the script rather than the adapter because only orca's
    daemon RPC can set pane env at all — its own CLI has no env flag and
    herdr's spawn happens in a daemon helm cannot reach — so the script is the
    one vehicle that behaves identically on all three metaharness cases.
    Values are shell-quoted; NOTHING SECRET may be passed here, same as every
    other consumer of this seam."""
    os.makedirs(RESUME_DIR, exist_ok=True)
    path = os.path.join(RESUME_DIR, "%s.sh" % row["i"])
    cwd = os.path.expanduser(row.get("cwd") or "") or "."
    exports = "".join("export %s=%s\n" % (k, shlex.quote(str(v)))
                      for k, v in sorted((env or {}).items()))
    # cd on its own line, and FAIL LOUD if it cannot: resuming claude from the
    # wrong directory does not error, it forks a fresh session — so a silent
    # fallback to $PWD would look like a resume and lose the history.
    with open(path, "w") as f:
        f.write("#!/bin/sh\n"
                "# helm sessions resume — regenerated on every run;\n"
                "# edit nothing here, it is derived state.\n"
                "cd %s || { echo \"helm resume: cwd is gone: %s\" >&2; exit 1; }\n"
                "%sexec %s\n"
                % (shlex.quote(cwd), cwd, exports,
                   resume_exec(row, home=home, skip_permissions=skip_permissions)))
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


def resume_identity_env(sid):
    """Identity exports for a resumed pane — {HELM_CHAT_NAME: seat} when
    exactly ONE rostered seat owns `sid`, else None.

    D of the identity refusal set (owner-declared P0, 2026-08-02): `helm
    sessions resume --go` passed NO env, so the resumed pane inherited
    whatever HELM_CHAT_NAME a metaharness ancestor had exported — the root
    vector of the integrator-name hijack (an exported name OUTLIVES the pane
    it named; see home.chat_name). Every helm-owned launch path must SET the
    name per-seat instead of inheriting. Ambiguous (multi-row) or unknown
    sids pass nothing: a nameless pane is honest (orcaadopt: nameless
    declares no seat) and the join derives it a name; a guessed name is
    incident 1."""
    from . import seats
    hits = seats.seats_for_session(sid)
    return {"HELM_CHAT_NAME": hits[0]} if len(hits) == 1 else None


def spawn_resume(row, title=None, home=None, skip_permissions=False, env=None):
    """Actually resume the session in a pane. (path, handle, adapter) on
    success; raises harness.HarnessError when no metaharness is reachable.

    This is the hop `sessions` deliberately left out — it computed the exact
    command and handed it over to be pasted. That is a fine contract between
    two programs and a broken one for a human: the owner is GUI-first and does
    not run CLI commands, so a capability that terminates in a string is a
    capability he does not have (human-surface parity). The seat lane already
    owned the real spawn; this routes the universal catalog through it instead
    of reinventing a second launcher."""
    from . import cred, harness
    ad = harness.detect()
    if ad is None:
        raise harness.HarnessError(harness.RECOMMENDATION)
    # THE SAME ORCA SYNC `helm launch` runs, because this door execs claude
    # onto a credhome without passing through it: a resume onto a home whose
    # token Orca has rotated away reads "Not logged in" exactly like a launch.
    # Only a claude row pinned to a named credhome is synced, and a launch the
    # sync refuses (an identity that disagrees with Orca's copy, a chain it
    # cannot prove) spawns nothing, with the sync's own reason.
    if row.get("h") == "claude" and home:
        said = io.StringIO()
        ok = cred.launch_sync(home, home, prefix="[helm resume]", out=said)
        sys.stderr.write(said.getvalue())
        if not ok:
            raise harness.HarnessError(
                "%s — nothing was spawned (`helm cred sync-orca --home %s` shows "
                "both sides)" % (said.getvalue().strip() or "the Orca sync refused "
                                 "the credhome", cred._display_path(home)))
    path = mint_resume_script(row, home=home,
                              skip_permissions=skip_permissions, env=env)
    cwd = os.path.expanduser(row.get("cwd") or "") or os.path.expanduser("~")
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    title = title or ("resume-" + row["i"][:8])
    # Provider-specific recovery belongs to the adapter. Orca can retry a
    # selector miss while preserving cwd in the command; every caller then gets
    # the same narrow behavior instead of sessions owning a second fallback.
    return path, ad.spawn(path, title=title, cwd=cwd), ad.name


RESUME_KICK = (
    "This session was RESUMED via `helm sessions resume` after its pane died. "
    "Your transcript is intact through its last persisted turn — pick up exactly "
    "where it leaves off. If any recent work is missing from the transcript, it "
    "was never persisted (say so plainly rather than guessing at it). If you are "
    "a fleet seat, re-arm your inbox beacon before anything else "
    "(Monitor: helm chat wait --seat <your-seat> --follow).")


def kick_resumed(ad, handle, note=None):
    """Type the resume brief INTO the pane so the restored agent starts moving.

    A resume restores the SESSION, not the momentum: the agent sits at a prompt
    until someone speaks to it, and both live recoveries on 2026-07-22 needed a
    human-typed brief before the seat did anything (the second one sat idle for
    minutes as a mystery). A DM cannot close this gap — a freshly resumed
    session has no beacon armed yet, and a seatless session has no DM lane at
    all. Injection through the adapter is the only path that reaches every
    resumed pane, so it rides the spawn instead of anyone's memory."""
    text = RESUME_KICK + ((" CONTEXT FROM THE RESUMER: " + note) if note else "")
    try:
        ad.send(handle, text, enter=True)
        return True
    except Exception:
        # the pane may still be booting claude; the resume itself succeeded and
        # a failed kick must not unwind it — report, never raise
        return False


def resume_warnings(row):
    """Why THIS resume might not do what you expect — the catalog-level signals
    (make_cmd's provider-coupled preflight is the richer surface; this is the
    same truth the one-paste `sessions resume` path can carry for free). Order:
    a session that cannot resume at all first, then cwd caveats."""
    warn = []
    if row.get("xl"):
        mb = (row.get("z") or 0) / 1e6
        warn.append("OVERSIZED (~%.0fMB in ~%d lines): a single/few-message "
                    "session whose content likely exceeds the 200k window and "
                    "cannot compact — plain resume will fail (common for "
                    "daily-memory/summarizer sessions)." % (mb, row.get("m") or 0))
    elif row.get("syn"):
        warn.append("REFERENCE session (daily-memory summarizer) — a "
                    "read/training artifact, not a resumable work session.")
    if row["h"] == "claude":
        cwd = os.path.expanduser(row.get("cwd") or "")
        if not cwd:
            warn.append("no recorded cwd; claude resume is cwd-scoped — the "
                        "command may not resolve.")
        elif not os.path.isdir(cwd):
            warn.append("recorded cwd no longer exists: %s (resume from another "
                        "dir may fork a fresh session)." % cwd)
    return warn


def _age(mt):
    if not mt:
        return "?"
    d = (time.time() - mt) / 86400.0
    if d < 1:
        return "today"
    return "%dd" % int(d)


def cmd_sessions(args):
    """sessions [<project>] [--limit N] [--all] | sessions resume <id-prefix>"""
    if args and args[0] == "resume":
        usage = ("sessions resume <session-id-prefix> [--go] [--title T] "
                 "[--note TEXT] [--skip-permissions] [--force]")
        if len(args) < 2:
            print("usage: helm " + usage)
            return 2
        pref = args[1]
        rest = args[2:]
        flags = ("--go", "--skip-permissions", "--force")
        valued = ("--title", "--note")
        from .cli import guard_tail
        if pref in ("-h", "--help"):
            # help-FIRST still guards the tail: `resume --help --bogus` used
            # to print usage and exit 0, a soft false existence probe for
            # --bogus. Junk beats help, same as everywhere else.
            rc = guard_tail("helm sessions resume", rest, flags=flags,
                            valued=valued, usage=usage)
            if rc is not None:
                return rc
            print("helm " + usage)
            return 0
        if pref.startswith("-"):
            print("helm sessions: resume wants an <id-prefix>, got '%s' (%s)"
                  % (pref, usage), file=sys.stderr)
            return 2
        # the tail is guarded BEFORE rows_for/spawn: an unknown flag exits 2
        # pre-action (`resume <id> --go --bogus` used to spawn anyway with
        # --help pretending success), and --title/--note must carry a
        # non-flag value exactly once — a missing value used to crash AFTER
        # the pane was already spawned.
        rc = guard_tail("helm sessions resume", rest,
                        flags=flags, valued=valued, usage=usage)
        if rc is not None:
            return rc
        go = "--go" in rest
        title = rest[rest.index("--title") + 1] if "--title" in rest else None
        hits = [r for r in rows_for(include_synthetic=True)
                if r["i"].startswith(pref)]
        if not hits:
            print("helm sessions: no session id starts with '%s'" % pref)
            return 1
        if len(hits) > 1:
            print("helm sessions: %d sessions match '%s' — disambiguate:" % (len(hits), pref))
            for r in hits[:8]:
                print("  %s  (%s, %s)" % (r["i"], r["h"], r.get("u", "?")))
            return 1
        row = hits[0]
        home = credhome_for(row["i"]) if row["h"] == "claude" else None
        if not go:
            print(resume_command(row, home=home))
            # warnings ride stderr so `$(helm sessions resume <id>)` stays the pure
            # command, while an interactive caller still sees why it might not resume
            for w in resume_warnings(row):
                print("  # " + w, file=sys.stderr)
            if row["h"] == "claude" and not home:
                print("  # no owning credhome found (no session-env/%s entry) — this "
                      "resumes on the CALLING shell's account, which may not be the "
                      "one that created it" % row["i"][:8], file=sys.stderr)
            print("  # add --go to actually open it in a pane", file=sys.stderr)
            return 0
        # --go is REFUSED, not best-effort, when the session cannot resume:
        # spawning a pane that dies on arrival looks like a working resume in
        # every listing while delivering nothing.
        blocking = [w for w in resume_warnings(row) if w.startswith(("OVERSIZED", "REFERENCE"))]
        if blocking:
            for w in blocking:
                print("helm sessions: refusing --go — " + w, file=sys.stderr)
            return 1
        blocked = trust_blocked(row, home)
        if blocked and "--skip-permissions" not in rest:
            print("helm sessions: refusing --go — %s has NOT accepted the trust "
                  "dialog for %s." % (os.path.basename(blocked), row.get("cwd")),
                  file=sys.stderr)
            print("  The pane would open, render the dialog into an alternate screen "
                  "buffer the adapter cannot read, and sit there looking like a "
                  "healthy blank pane forever.", file=sys.stderr)
            print("  Re-run with --skip-permissions to pass "
                  "--dangerously-skip-permissions, or accept the dialog once by hand.",
                  file=sys.stderr)
            return 1
        held = live_sids().get(row["i"])
        if held and "--force" not in rest:
            print("helm sessions: refusing --go — session %s is ALREADY OPEN in pid %d."
                  % (row["i"][:8], held), file=sys.stderr)
            print("  A second pane on one sessionId interleaves both panes' writes into "
                  "the same transcript and each loses turns to the other.", file=sys.stderr)
            print("  Switch to that pane, or pass --force if you know it is a zombie.",
                  file=sys.stderr)
            return 1
        from . import harness
        try:
            path, handle, adapter = spawn_resume(
                row, title=title, home=home,
                skip_permissions="--skip-permissions" in rest,
                env=resume_identity_env(row["i"]))
        except harness.HarnessError as e:
            print("helm sessions: cannot spawn a pane: %s" % e, file=sys.stderr)
            print("  paste instead: " + resume_command(row, home=home), file=sys.stderr)
            return 1
        print("helm sessions: resumed %s via %s — pane %s" % (row["i"][:8], adapter, handle))
        note = rest[rest.index("--note") + 1] if "--note" in rest else None
        from . import harness as _h
        kicked = kick_resumed(_h.detect(), handle, note=note)
        print("  kick: %s" % ("delivered — the agent has its resume brief"
                              if kicked else
                              "FAILED — the pane is up but idle; speak to it or re-send by hand"))
        print("  cred: %s" % (home if is_pinnable(home)
                                else "%s (default account — deliberately UNPINNED; "
                                     "pinning it opens the first-run wizard)" % (home or "unknown")))
        print("  cwd : %s" % (row.get("cwd") or "?"))
        print("  script: %s" % path)
        for w in resume_warnings(row):
            print("  warn: " + w, file=sys.stderr)
        return 0

    # the bare-list path: ONE optional positional (the <project> filter, the
    # docstring's contract) — unknown flag-shaped args refuse instead of
    # silently listing as if they existed.
    rest = list(args)
    limit = 25
    if "--limit" in rest:
        i = rest.index("--limit")
        try:
            limit = int(rest[i + 1])
        except (IndexError, ValueError):
            print("helm sessions: --limit wants an integer", file=sys.stderr)
            return 2
        if limit < 1:
            # a negative/zero limit used to silently truncate the listing to
            # one row (rows_for's `len(out) >= limit` fires immediately) —
            # nonsense values refuse like non-integers do.
            print("helm sessions: --limit wants a positive integer",
                  file=sys.stderr)
            return 2
        del rest[i:i + 2]
    include_synthetic = "--all" in rest
    rest = [a for a in rest if a != "--all"]
    # -h/--help on an OTHERWISE-CLEAN tail is a help request, not junk:
    # `sessions --all --help` used to refuse rc 2 lying that --help is an
    # unknown arg. Junk still beats help (an unknown flag alongside --help
    # refuses — the existence probe stays honest).
    want_help = any(a in ("-h", "--help") for a in rest)
    rest = [a for a in rest if a not in ("-h", "--help")]
    junk = [a for a in rest if a.startswith("-")]
    if junk:
        print("helm sessions: unknown arg '%s' (sessions [<project>] "
              "[--limit N] [--all] | sessions resume <id-prefix>)" % junk[0],
              file=sys.stderr)
        return 2
    if len(rest) > 1:
        print("helm sessions: one <project> filter at most (got: %s)"
              % ", ".join(rest), file=sys.stderr)
        return 2
    if want_help:
        print("sessions [<project>] [--limit N] [--all] | "
              "sessions resume <id-prefix>")
        return 0
    project = rest[0] if rest else None
    rows = rows_for(project=project, include_synthetic=include_synthetic, limit=limit)
    if not rows:
        print("helm sessions: none%s." % (" for project '%s'" % project if project else ""))
        return 0
    scope = " — " + project if project else ""
    print("helm sessions (%d newest%s):" % (len(rows), scope))
    for r in rows:
        proj = r.get("project") or "-"
        print("  %-6s %-7s %-20s %-9s %s" % (
            _age(r.get("mt")), r["h"], proj[:20], r["i"][:8],
            (r.get("t") or "(untitled)")[:70]))
    print("resume any: helm sessions resume <id-prefix>")
    return 0


def _same_project(a, b):
    """True when two cwds resolve to one project: the registry derivation
    helm scopes with (which reads `<root>-wt/<lane>` as `<root>`), else the
    basename the roster records as `project`."""
    try:
        from .inject._ledger import project_for_cwd
        pa, pb = project_for_cwd(a), project_for_cwd(b)
    except Exception:
        pa = pb = None
    if pa is not None or pb is not None:
        return pa == pb
    a, b = (os.path.basename(str(c or "").rstrip(os.sep)) for c in (a, b))
    return bool(a) and a == b


def _session_dead(sid):
    """True only when `sid` is PROVEN not held by a live claude process.

    Held means either a pid-keyed session record whose pid is the SAME live
    claude incarnation (`sessions._pid_is_claude` with procStart), or a live
    argv `--resume <sid>`. Any probe that cannot answer is not proof, so it
    answers False and the caller keeps today's dedup."""
    if not sid:
        return False
    from . import beacons
    records = beacons.holder_records()
    live = beacons.live_sessions()
    if records is None or live is None:
        return False
    if sid in live:
        return False
    rec = records.get(sid)
    return not (rec and _pid_is_claude(rec[0], rec[1]))


def pane_heir(r, cwd):
    """The name a restart in the SAME Orca pane inherits, or None.

    A pane Orca opened keeps its ORCA_PANE_KEY across a claude restart, and
    join records it on the row (`pane_key`). The later session reuses that
    row's name ONLY when the row's current session is proven dead and the new
    cwd is in the same project. A live holder never loses its name: anything
    short of proof falls through to the ordinary dedup."""
    pane = os.environ.get("ORCA_PANE_KEY")
    if not pane:
        return None
    for name, row in sorted(r.items()):
        if not isinstance(row, dict) or row.get("pane_key") != pane:
            continue
        if _same_project(row.get("cwd"), cwd) \
                and _session_dead(row.get("session")):
            return name
    return None
