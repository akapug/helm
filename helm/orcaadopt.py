#!/usr/bin/env python3
"""helm orcaadopt — SEE, TRACK and RESUME a pane the METAHARNESS launched.

THE GAP THIS CLOSES (dogfooded twice on 2026-07-24, with real cost). `helm seat
spawn|where|resume` only ever knew the MULTIMODEL families, so a claude pane
that orca launched directly answered `unknown seat '<seat>' (families:
codex, ds4pro, kimi)`. Two consequences, both paid for:

  1. The owner closed that seat's pane and asked for it back. There was no
     CLI path to give it to him — `where`/`resume` both refused the name — so it
     came back through a hand-built `orca terminal create`. Recovering a pane is
     helm's job, never the owner's.
  2. Slice 0's per-seat home worktrees (89017cf, "seat spawn: per-seat HOME
     worktrees — own the default that put 9 agents in one tree") only
     provision at helm SPAWN,
     so the three orca-launched claude seats still shared the main checkout —
     and the seat named above generated most of that day's
     shared-tree damage. "The structural fix for the whole class" reached the
     multimodel half only.

WHAT A SEAT'S IDENTITY ACTUALLY IS. helm has THREE registers, and the whole
slice is about reconciling them without pretending they are one:

  helm SEAT register  ~/.helm/_global/seats/<family>/…/spawn.json — helm-spawned
                      seats only. Owns handle/pid/room. `seat.py` is its keeper.
  helm CHAT roster    /dev/shm/helm-chat/.roster.json — EVERY seat that ever
                      joined chat, orca-launched ones included. Owns seat ->
                      session id(s). This is why helm knows an orca-launched seat at
                      all.
  orca                terminal.list — every live pane. Owns handle/pane.

PROVENANCE IS NEVER FLATTENED. A row is labelled `helm-spawned` or
`orca-adopted` (or `unowned`), because merging them is exactly the mistake that
produced a false "already landed" claim and nearly deleted correct work.

HOW A PANE IS TIED TO A SEAT. From the PROCESS, never from presentation. A
pane's title is a mutable auto-summary and its `preview` is scrollback that
scrolls away (measured: only 2 of 25 live panes still had their launch line in
view), so neither may authorise a resume or a kill — the same law seat.py
already applies to registered panes. What IS durable is the live claude
process's own environment: HELM_CHAT_NAME names the seat and ORCA_PANE_KEY names
the pane, and `terminal.resolvePane` turns that key into a current handle.

/proc DISCIPLINE. `cmdline` is world-readable; `environ` is not (it is refused
for other users' processes and even for `systemd --user`). A probe that CANNOT
LOOK must never report "absent" — an unreadable claude process makes the answer
UNKNOWN, which REFUSES a resume, rather than silently clearing it. Only the
non-secret identity keys below are ever selected out of an environment; a full
environ can carry credentials and is never returned or logged.
"""
import errno
import glob
import os
import re

from . import procid

# The only environment keys this module ever reads out of another process.
# Naming them as a closed set is the guard: a future edit that wants something
# else has to add it here, in the open, rather than widening a slice of environ.
IDENTITY_KEYS = ("HELM_CHAT_NAME", "ORCA_PANE_KEY", "ORCA_WORKTREE_ID")

HELM_SPAWNED = "helm-spawned"
ORCA_ADOPTED = "orca-adopted"
UNOWNED = "unowned"

LIVE, DEAD, UNKNOWN = "LIVE", "DEAD", "UNKNOWN"

# TRANSPORT -> RICH (seat._STATE_NAMES) — the ONE translation (#141). LIVE
# stays LIVE, deliberately NOT "RUNNING": RUNNING is pane-tail evidence (a
# turn in flight); LIVE is process evidence, and a WEDGED process is still a
# process — upgrading it made a hung seat read as actively working. DEAD's
# rich word is GONE. Absent keys degrade to UNKNOWN, never leak through.
RICH_STATE = {LIVE: "LIVE", DEAD: "GONE"}

# THE QUESTION THIS MODULE ANSWERS, carried in the answer so no reader has to
# infer it from a key name. #141 keeps the transport word untranslated on the
# consumer side ("LIVE stays LIVE — process evidence must not be upgraded to
# RUNNING's turn-in-flight claim; a wedged process is still a process"), and
# this states the same boundary from the producer side.
ATTACHED_QUESTION = "is a process holding this seat open"


# ---------------------------------------------------------------------------
# process IDENTITY — a pid is a slot the kernel recycles, not a name
# ---------------------------------------------------------------------------
#
# THE DEFECT THIS TYPE EXISTS FOR (dispatch a4051c69). Every
# addressing decision in this file used to travel as a bare `int`. A bare int
# survives the death of the thing it names: pid 101 is decided about, pid 101
# exits, the kernel hands 101 to ANOTHER agent's pane, and the send-time check
# — `is pid 101 alive?` — answers yes. It is not the same yes. Reproduced: the
# directive went to the vacated pane's handle and returned "resumed".
#
# On Linux the unforgeable birth identity is (pid, starttime) — /proc/<pid>/stat
# field 22, a boot-relative tick count the kernel stamps once and never edits.
# `sessions.live_sids` already proves liveness this way ("comm + procStart, so a
# recycled pid cannot pass"); the addressing half was the register that had not
# adopted it.
#
# WHY AN int SUBCLASS RATHER THAN A TUPLE. The identity has to cross an argv
# boundary (`resume-turn --deliver --pids`), a JSON surface (`seat where
# --json`), sets, sorts and a dozen "%d" log lines, and every one of those
# consumers is correct to treat it as the pid it is. Making it a tuple would
# mean editing each of them for a fact none of them decide. Making it an int
# that CARRIES its birth stamp means the one consumer that must decide — "is
# this the same process?" — has the evidence in its hand, and the rest are
# untouched. Flattening back to `int(x)` loses the stamp, which `authorized_
# handle` treats as a refusal rather than as permission.


class ProcIdent(int):
    """A pid PLUS the birth stamp naming WHICH incarnation of it.

    Equal to its pid for every purpose except authorization, where the whole
    pair must match a process observed at send time.
    """

    # No __slots__: int is a variable-length builtin and CPython refuses a
    # nonempty __slots__ on its subtypes.

    def __new__(cls, pid, start=None):
        self = int.__new__(cls, int(pid))
        self.start = str(start) if start is not None else None
        return self

    def __repr__(self):
        return "pid %d (birth stamp %s)" % (int(self), self.start or "unknown")


def proc_start(pid):
    """The birth stamp of `pid` — /proc/<pid>/stat field 22 — or None.

    PARSED FROM THE LAST `)`, never by splitting the line. Field 2 is `comm`,
    which is the executable basename in parentheses and MAY ITSELF CONTAIN
    SPACES AND PARENTHESES (`(claude (old))` is a legal comm). A naive split
    therefore mis-numbers every field after it, which would silently compare
    the wrong number and call two different processes the same one.

    None is honest UNKNOWN — the process exited, or /proc is not there. It
    never means "same incarnation": an identity that could not be read must
    refuse a send, not pass one.
    """
    try:
        with open(os.path.join(procid.proc_root(), str(pid), "stat"), "rb") as f:
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    cut = raw.rfind(")")
    if cut < 0:
        return None
    # After comm the fields resume at #3 (state), so starttime (#22) is [19].
    rest = raw[cut + 1:].split()
    return rest[19] if len(rest) > 19 else None


def ident_of(proc):
    """The ProcIdent for a row `claude_processes()` produced."""
    return ProcIdent(proc["pid"], proc.get("start"))


def ident_key(x):
    """(pid, start) — the comparable identity of a ProcIdent or a proc row."""
    if isinstance(x, dict):
        return x["pid"], x.get("start")
    return int(x), getattr(x, "start", None)


def ident_token(x):
    """`<pid>:<starttime>` — the wire form, for an argv that crosses a fork.

    A pid with no stamp serialises as the bare number it always was, so an
    older `--pids 103056` still parses; it simply cannot authorize a send.
    """
    pid, start = ident_key(x)
    return "%d:%s" % (pid, start) if start else "%d" % pid


def parse_ident(text):
    """A `--pids` token -> ProcIdent, or None if it is not one."""
    pid, _sep, start = str(text).strip().partition(":")
    if not pid.isdigit():
        return None
    return ProcIdent(int(pid), start or None)


# ---------------------------------------------------------------------------
# the process layer — the durable pane<->seat identity
# ---------------------------------------------------------------------------

def claude_processes():
    """([proc], unreadable) for every LIVE claude process on this host.

    A proc is {pid, start, seat, pane_key, worktree_id, resume_sid}.
    `unreadable` is the list of pids that ARE claude but whose environ could not
    be read — the honest UNKNOWN bucket, never folded into "not this seat".

    `start` is the birth stamp (see `proc_start`), and it is gathered in the
    SAME pass for the same reason the other two signals are: a pid observed
    without it is a slot number, and two observations of a slot number are not
    two observations of a process.

    Both signals are gathered in ONE /proc pass because they are complementary,
    not redundant, and that was measured rather than assumed: on this host
    one seat was identifiable ONLY by HELM_CHAT_NAME (no --resume in
    argv), while another was identifiable ONLY by `--resume <sid>` in
    argv (no HELM_CHAT_NAME in environ). Either rung alone is blind to one of
    the two seats this slice exists to recover.

    THE PER-PID WORK IS ONE PREDICATE, and this function is only the walk plus
    the two buckets. `_read_candidate` reads the WHOLE candidate inside ONE
    bracket and returns ONE of three verdicts — ROW, ABSENT, BLIND — so there is
    no place here to decide anything, and no partial row for a consumer to
    mistake for a whole one. Read its note for why the reads stopped being
    handled individually; rounds 3 through 7 are what that note is made of.

    A pid that never looked like claude is SKIPPED, not recorded: it is not a
    claude process helm failed to identify, it is not a claude process. Only a
    pid that PASSED the identity filter and then could not be read coherently
    reaches `unreadable`.

    AND `unreadable` IS NOT A FOOTNOTE. Every consumer of this pair must run
    `cannot_look()` before it emits a confident value — see that function, and
    see `CensusConsumerCensusTest`, which enumerates the consumers from the
    source so a fifth one cannot be written without declaring how it answers
    when the census is blind.
    """
    procs, unreadable, walked = [], [], set()
    for entry in glob.glob(os.path.join(procid.proc_root(), "[0-9]*", "cmdline")):
        try:
            # the path's own shape, never a fixed component index — split[2]
            # only meant "pid" while the root was literally /proc (sessions
            # cured this first; an injectable root broke the index silently)
            pid = int(os.path.basename(os.path.dirname(entry)))
        except ValueError:
            continue
        walked.add(pid)
        row, verdict = _read_candidate(pid, entry)
        if verdict is BLIND:
            unreadable.append(pid)
        elif row is not None:
            procs.append(row)
    # THE WALK IS SEEDED WITH A MUST-HIT. Every read below the walk is now
    # bracketed and every verdict is honest — and a pid that was never
    # ENUMERATED is a read that never happened, which is the same blindness one
    # level up. `glob` does not raise: an unmounted /proc, a hidepid mount, a
    # chroot with no procfs all answer [], and ([], []) reads to every consumer
    # as the confident claim "there are no claude processes on this host".
    #
    # helm's OWN pid is the seed, because it is the one entry a working walk
    # cannot miss — this code is running. Its absence proves the enumeration
    # did not happen, and proves it without needing to know WHY, which is what
    # makes it a floor rather than another special case.
    if os.getpid() not in walked:
        return [], ["the /proc walk itself did not enumerate helm's own "
                    "pid (%d), so this host was never actually scanned"
                    % os.getpid()]
    return procs, unreadable


# ---------------------------------------------------------------------------
# ONE PREDICATE — the whole candidate, one bracket, one verdict
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS AS A FUNCTION AND NOT AS A LOOP BODY. Rounds 3-7 each found
# ONE more read that had escaped the bracket: the send-time comparison
# (a4051c69), the metadata read before the trailing stamp (ad157755), `comm`
# read before `start_before` (ad2aed0f), and then the OSError exits that skipped
# without proving exit (90845108). Each finding was correct and each fix was
# correct, and the sequence could not terminate, because a per-read handler is
# completed only by enumerating reads and the next read is always outside the
# set somebody enumerated.
#
# So the reads stop being individually handled. There is ONE bracket around ONE
# object, and its verdict is one of THREE — never a fourth, and never a partial
# fourth wearing one of the three:
#
#   ROW    every field read succeeded AND both stamps are present AND equal.
#          A row is complete or it does not exist; there is no partial row for
#          a consumer to flatten.
#   ABSENT there is no claude process at this pid AND HELM IS SURE OF IT —
#          either a read failed because the process is not there (ENOENT/
#          ESRCH), or it is there and demonstrably is not claude. Those are
#          one verdict because they answer the consumer's question the same
#          way: nothing here to account for, and nothing to be unsure about.
#          The word is `absent`, not `exited`, precisely so a `bash` does not
#          get filed under a name that claims it died.
#   BLIND  anything else. A read that failed for any other reason, a stamp that
#          could not be read, a stamp that moved, a `comm` that read back as
#          something else. helm could not build the row and CANNOT SAY WHY NOT
#          in a way that rules this pid out.
#
# ABSENT AND BLIND ARE THE WHOLE POINT AND THEY ARRIVE AT THE SAME `except`.
# `errno` is the only thing that tells them apart: ENOENT/ESRCH is the kernel
# saying THE PROCESS IS NOT THERE; EACCES/EPERM is the kernel saying I WILL NOT
# LET YOU LOOK. Skipping on both — which is what a bare `except OSError:
# continue` does — makes an unreadable process indistinguishable from a dead
# one, and every consumer downstream then reads "no rival" where the truth is
# "a rival helm could not see".
#
# THE STAMP IS THE ONE READ WHOSE FAILURE IS NEVER `ABSENT`, and that is round
# 4's law kept deliberately: `proc_start` answers the IDENTITY question, and an
# identity that could not be read may never be compared for equality (None ==
# None is exactly the accident that would turn two failed reads into a matched
# pair). Once an opening stamp exists, helm has committed to describing one
# specific incarnation, and any failure to finish that description is a row it
# could not build. Costs one refusal, loudly.

ROW, ABSENT, BLIND = "ROW", "ABSENT", "BLIND"

# The errnos that PROVE there is no process at this pid. Everything else is
# blindness — the rule is keyed on what proves exit, never on a denylist of the
# failures somebody happened to think of.
_PROVES_EXIT = frozenset((errno.ENOENT, errno.ESRCH))


def _read_candidate(pid, entry):
    """(row, verdict) — the WHOLE candidate, read once inside ONE bracket.

    verdict is ROW, ABSENT or BLIND (see the note above). `row` is non-None
    only for ROW: a partial row is never returned, because that is exactly
    what the consumers downstream have four times mistaken for a whole one.
    """
    def read(path):
        """(bytes, verdict) — never (bytes, ABSENT) and never (None, ROW)."""
        try:
            with open(path, "rb") as f:
                return f.read(), ROW
        except OSError as e:
            return None, (ABSENT if e.errno in _PROVES_EXIT else BLIND)

    # identity is the same WHAT-is-this check sessions._pid_is_claude makes.
    # OUT HERE IT MAY ONLY SAY NO. It is the cheap reject-early filter that
    # keeps a host full of non-claude pids from paying two stat reads each, and
    # nothing more: its YES is about whoever held the number at this instant,
    # which need not be whoever the window below describes. Its FAILURE is not
    # a rejection either — a read that did not happen classified nothing.
    #
    # AND ITS NO USED TO BE WRONG ABOUT REAL PANES. comm alone rejected every
    # pane that exec'd `.../claude/versions/<semver>` directly, because comm is
    # that binary's BASENAME — the semver. Those panes were SKIPPED here, and a
    # skip never reaches `unreadable`, so the census reported them as absent
    # with 0 blind reads. Measured 2026-08-06: 14 rows, 0 unreadable, 2 live
    # claude panes missing, one of them a seat then declared DEAD mid-turn.
    raw, verdict = read(os.path.join(procid.proc_root(), str(pid), "comm"))
    if verdict is not ROW:
        return None, verdict
    ident = procid.is_claude(pid, raw)
    if ident is None:
        # comm is shaped like a versioned launch and the kernel would not let
        # us read the exe that would settle it. That is NOT-PERMITTED-TO-LOOK,
        # and routing it to ABSENT would be this function's own bug a second
        # time: the row would leave here as "not a claude process" — the one
        # verdict that never reaches `unreadable` and so never reaches
        # `cannot_look()`.
        return None, BLIND
    if not ident:
        return None, ABSENT        # not a claude process — not helm's to miss

    # --- the bracket opens: every field below is read INSIDE it ---------------
    start_before = proc_start(pid)

    # comm AGAIN, now under the stamps. The pre-filter above answered for
    # whoever held the pid BEFORE the bracket opened; if the number changed
    # hands in between, the stamps and the payload all belong to the successor
    # and agree with each other perfectly, so nothing else here can notice.
    # argv[0] cannot: `/tmp/bash-claude` ends in "claude".
    raw, verdict = read(os.path.join(procid.proc_root(), str(pid), "comm"))
    if verdict is not ROW:
        return None, verdict
    if procid.is_claude(pid, raw) is not True:
        # Two incompatible answers about one number. helm cannot say which
        # process it is looking at, so it says so — never the silent "not this
        # seat" a bare skip would mean. `is not True` because the tri-state's
        # UNKNOWN belongs on this side of the line too: cannot-tell inside the
        # bracket is the same blindness as disagreement.
        return None, BLIND

    raw, verdict = read(entry)
    if verdict is not ROW:
        return None, verdict
    argv = raw.decode("utf-8", "replace").split("\0")
    # The comm/exe gate above already proved this pid is claude; this line is
    # the SECOND opinion and must not be narrower than the first, or it becomes
    # the sole reason a proven-claude pane is dropped. A versioned launch has
    # argv[0] == the binary PATH, which does not end in "claude".
    # `is not False` and not a bare truth test: an UNREADABLE exe must not veto
    # a pid the gate above already identified, or this second opinion becomes
    # narrower than the first and turns into the sole reason a proven-claude
    # pane is dropped.
    if not argv or not (argv[0].endswith("claude")
                        or procid.exe_is_claude(pid) is not False):
        return None, ABSENT
    # A `--resume` VALUE THAT IS NOT SHAPED LIKE A SESSION ID IS NOT SESSION
    # EVIDENCE. `claude --resume` also takes a human session TITLE, and the
    # token after the flag was taken as a sid whatever it was. MEASURED LIVE
    # 2026-07-30, pid 407141: argv is
    #   ['claude', '--dangerously-skip-permissions', '--resume',
    #    'helm coordinator 7-22']
    # so resume_sid became the string "helm coordinator 7-22". That junk then
    # reached `turn_restart_identity` rung 2, which compared it to the real
    # compaction sid, found no match, found no OTHER candidate with an empty
    # resume_sid, and announced "every addressable process for <seat>
    # holds a different session (pid 407141/helm coo…) — the pane that
    # compacted is gone" — about the pane that was mid-turn INSIDE pid 407141.
    # ("helm coo" is that string's first 8 characters, which is how the alert
    # was traced back to here.)
    #
    # This restores the law rung 2 already states for itself: "SESSION EVIDENCE
    # IS ASYMMETRIC, deliberately: positive disproof refuses, missing evidence
    # does not." A title is MISSING evidence, and it was being spent as
    # positive disproof. None puts the process back in the `silent` bucket,
    # where a pane that never wrote a sid has always belonged.
    #
    # TWO LAYERS, NOT BELT-AND-BRACES — they own DIFFERENT input paths. Rung 2
    # applies `session_shaped` to whatever a CALLER supplies in `procs`, which
    # is the only protection when the rows did not come from this scan. This
    # one kills the junk AT THE SOURCE, so every OTHER consumer of resume_sid
    # is covered too — `_nameless_identity`'s sid match and the roster-identity
    # comparison both read the field directly and neither passes through rung
    # 2. Same predicate, deliberately one definition, applied at both ends.
    resume_sid = None
    for flag in ("--resume", "-r"):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv) and session_shaped(argv[i + 1]):
                resume_sid = argv[i + 1]
            break

    # environ is the read that is refused for a LIVE process helm may not look
    # at (another user's, a privileged one), which is why the errno split is
    # load-bearing here rather than theoretical. An unreadable environ used to
    # yield a row with `seat=None` AND an entry in `unreadable` — the one
    # partial row this collector could emit, and the thing every consumer then
    # had to remember to cross-check. It is a whole BLIND candidate now.
    raw, verdict = read(os.path.join(procid.proc_root(), str(pid), "environ"))
    if verdict is not ROW:
        return None, verdict
    vals = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        name = key.decode("ascii", "replace")
        if sep and name in IDENTITY_KEYS:
            vals[name] = value.decode("utf-8", "replace")

    start_after = proc_start(pid)
    # --- the bracket closes ---------------------------------------------------
    if start_before is None or start_before != start_after:
        # The payload is DISCARDED, not repaired and not partially kept. It
        # describes at least one process, and helm cannot say which.
        return None, BLIND
    return {"pid": pid, "start": start_before,
            "seat": vals.get("HELM_CHAT_NAME"),
            "pane_key": vals.get("ORCA_PANE_KEY"),
            "worktree_id": vals.get("ORCA_WORKTREE_ID"),
            "resume_sid": resume_sid}, ROW


_SESSION_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def session_shaped(value):
    """Could this string BE a session id? Not: is it the right one.

    The distinction is the whole point. `claude --resume` takes either a
    session id or a human session TITLE, so a resume token is not evidence
    about WHICH session a process holds unless it is shaped like an id at all.
    Comparing a title to an id with `!=` always says "different", which is a
    confident answer to a question the data cannot address.

    Deliberately a SHAPE test and nothing more. It does not ask whether the
    session exists, is live, or belongs to this seat — every one of those is a
    different question with a different owner, and answering them here would
    rebuild the collapse this function exists to prevent, one level in.

    CASE-INSENSITIVE ON PURPOSE, AND IT DISAGREES WITH `session._SID_RE` — which
    is strict — BECAUSE THE TWO ANSWER DIFFERENT QUESTIONS. `_SID_RE` is the
    MINT shape: what helm PRODUCES, where strict is right, because a helm that
    mints an uppercase id is a bug to catch rather than a form to bless. This is
    the PARSE shape for `turn_restart_identity`, and there the divergence has
    teeth, because of the FAIL DIRECTION:

      a shaped token is CONTRADICTION evidence (refuse the address);
      an unshaped token is NO evidence (fall through to rung 3).

    So a STRICT reader meeting a real uppercase id would read it as a TITLE, pass
    rung 2, and let a handoff be addressed to a pane holding a DIFFERENT session
    — silently, which is the a032ce7 incident class ("orcaadopt: the session
    join could type into ANOTHER agent's pane — it now refuses") this rung
    exists for. A
    LENIENT reader meeting an uppercase TITLE reads it as an id, calls it a
    contradiction, and REFUSES: loud, retryable, and the operator is told why.
    Wrong-refuse is the safe failure for a rung whose whole job is to not send a
    handoff into the wrong pane.

    This is NOT the policy_kind case, where two sides of ONE comparison drifted
    and a capital letter became a live bypass. Mint and parse are a
    producer/consumer pair, and a consumer of safety-critical evidence gets to be
    more liberal in what it RECOGNIZES precisely because recognizing it triggers
    the conservative branch. (A cross-family read ruled this.)
    """
    return bool(_SESSION_ID.fullmatch(str(value or "").strip()))


# ---------------------------------------------------------------------------
# THE ONE RULE, one implementation — what every census CONSUMER must do
# ---------------------------------------------------------------------------


def cannot_look(unreadable, claim):
    """The refusal a blind census forces on `claim`, or None if it can be made.

    THE RULE: a pid in `unreadable` is a LIVE claude process helm could not
    identify. Every question this module answers is of the form "which process
    is this / is there one at all", and for a pid helm could not read, the
    answer is one it cannot see. A consumer that filters `procs` and never
    looks at `unreadable` is asking "is there a READABLE rival?" and answering
    the different question "is there a rival?".

    THE COLLECTOR'S HONESTY IS NOT THE FIX; IT IS THE INPUT TO IT. Rounds 4-6
    made `claude_processes()` report the pids it could not identify. Round 7
    (dispatch 90845108) found the same fail-open one layer up in FOUR
    consumers at once — a readable candidate winning an address beside an
    unreadable rival, `resolve()` mapping ([], [pid]) to None, `proxywatch`
    mapping it to `off` — because each consumer had its own idea of what an
    empty-looking census meant. There is one idea now and this is it.

    UNKNOWN NEVER AUTHORIZES A SEND, and never collapses into another state.
    That is `_LIVENESS_STATES`' own law ("a guessed state is worse than an
    honest UNKNOWN because a watchdog acting on a guess acts wrong") and
    `seat_liveness` rung R3 has enforced it here since this file was written.
    These consumers now hold the same rung, from the same implementation.

    Refusing is not free: ONE unreadable claude on the host refuses every
    address until it is readable or gone. That is the direction this file
    always chooses — an unreachable pane fails loudly and costs one retry,
    while a mis-reached pane types into another agent's live session and
    reports success.
    """
    # A token is normally a pid, but the bucket also carries whole-scan
    # failures ("the /proc walk itself…", `resumeturn`'s scan exception), which
    # are the same fact at a coarser grain: a live claude helm could not
    # account for. Both render as themselves rather than being forced into a
    # "pid %s" the second kind is not.
    tokens = [("pid " + str(p)) if str(p).strip().isdigit() else str(p)
              for p in (unreadable or ())]
    if not tokens:
        return None
    return ("%d live claude process%s could not be identified — its /proc "
            "identity could not be read (%s) — so %s; one of them may be the "
            "process this answer is about, and a check that cannot see a case "
            "must return UNKNOWN rather than a verdict"
            % (len(tokens), "" if len(tokens) == 1 else "es",
               "; ".join(tokens), claim))


def _current_sid(row):
    """The CURRENT session id on a roster row, or None for anything else.

    Factored out so the one-seat reader (`roster_identity`) and the whole-roster
    reader (`roster_current_sids`) cannot drift apart about what "current"
    means. Two copies of this rule is how one surface starts addressing a seat
    the other would refuse, and the cost of that disagreement is a keystroke in
    somebody else's pane.
    """
    current = row.get("session") if isinstance(row, dict) else None
    return current if isinstance(current, str) and current else None


def roster_current_sids():
    """({seat: current_sid}, probe_failed) — the ADDRESSING half of the roster
    for EVERY seat, from ONE read.

    WHY ONE READ AND NOT N. A caller labelling a whole pane inventory asks this
    question once per seat, and `roster_identity` re-reads the roster each time.
    The roster is a live tmpfs file that other seats rewrite, so per-seat reads
    let it change underneath a single pass and produce a listing whose rows
    never described one coherent moment. Same law as the claim table's single
    liveness snapshot: one reading, shared by every claim in the pass.

    ONLY the current sid, never the history — `roster_identity` measures why
    history is liveness evidence and never an address, and this map exists to
    be addressed from.
    """
    from . import seats
    roster, failed = seats.roster_checked()
    if failed:
        return {}, True
    out = {}
    for seat, row in (roster or {}).items():
        sid = _current_sid(row)
        if isinstance(seat, str) and seat and sid:
            out[seat] = sid
    return out, False


def roster_identity(seat):
    """(current_sid, history_sids, probe_failed) — ONE roster read, TWO answers.

    They are different questions and their callers must never share an answer.

      LIVENESS may use the whole HISTORY. A stale sid found alive is evidence
      that something still holds the seat, and the safe direction there is to
      refuse a duplicate resume — a false LIVE costs one refusal, loudly.
      ADDRESSING may use only the CURRENT session. A stale sid found alive is
      NOT this seat's pane; it is whoever reopened that transcript. The safe
      direction inverts, because a false match here TYPES INTO ANOTHER AGENT'S
      PANE — and unlike an unresolved seat, that fails silently.

    MEASURED 2026-07-30 on the live roster, which is why this is a split and not
    a nicety: `codex` carries 7 historical sids and the integrator 8, while
    `kimi`'s live process runs on a HISTORY sid (8d2e1ff0…) rather than on its
    current one (58e6f94a…). History-as-an-address is a live minefield here, not
    a hypothetical one.

    `roster_checked` distinguishes a proven-empty roster from an unreadable one;
    a failed probe must reach the guard as UNKNOWN rather than as "this seat
    holds no sessions", which would clear a resume on no evidence at all.
    """
    from . import seats
    roster, failed = seats.roster_checked()
    if failed:
        return None, [], True
    from .seats_common import canonical_seat
    key, ambiguous = canonical_seat(seat, roster)
    if ambiguous:
        # AN AMBIGUOUS ROSTER IS A FAILED PROBE, NOT AN ABSENT SEAT.
        # Returning failed=False here hands the guard above "this seat
        # holds no sessions" — the one answer this docstring forbids,
        # because it clears a resume on no evidence at all. The
        # resolver already refused; discarding its refusal converts a
        # REFUSAL into a NEGATIVE, which is the opposite polarity.
        return None, [], True
    row = roster.get(key) if key else None
    if not isinstance(row, dict):
        return None, [], False
    current = _current_sid(row)
    sids, seen = [], set()
    for sid in [row.get("session")] + list(row.get("sessions") or []):
        if isinstance(sid, str) and sid and sid not in seen:
            seen.add(sid)
            sids.append(sid)
    return current, sids, False


def roster_sessions(seat):
    """([sid], probe_failed) — every session id helm's chat roster has seen this
    seat hold, newest-known first: the LIVENESS half of `roster_identity`.

    Addressing callers must NOT use this. Take the current sid from
    `roster_identity` instead, for the reason its docstring measures.
    """
    _current, sids, failed = roster_identity(seat)
    return sids, failed


# ---------------------------------------------------------------------------
# the duplicate-session guard — the one hazard the owner named by name
# ---------------------------------------------------------------------------

def seat_liveness(seat, procs=None, unreadable=None, named=None):
    """(state, evidence) — is anything ALREADY holding this seat open?

    state is LIVE (refuse a resume), DEAD (safe to resume), or UNKNOWN (refuse:
    the probe could not see enough to clear it).

    THREE INDEPENDENT RUNGS, because the failure that motivated this is a seat
    that had ALREADY SELF-RESUMED UNDER A NEW SESSION ID while its old
    transcript went 51 minutes cold. Transcript age is therefore NOT evidence of
    death and is deliberately not consulted anywhere in this function.

      R1  seat -> process.  A live claude process whose environ says
          HELM_CHAT_NAME=<seat>, or a process joined to this seat by session ID.
          This is the rung that catches a self-resume: it is keyed on the SEAT,
          so a brand-new session id changes nothing.
      R2  seat -> session -> process.  Any session id the chat roster has ever
          seen this seat hold, found in sessions.live_sids() — which itself
          proves liveness three ways: a pid-keyed session record (comm +
          procStart, so a recycled pid cannot pass), and an argv `--resume
          <sid>` scan for panes too old to write one. Catches a pane that never
          announced a HELM_CHAT_NAME.
      R3  cannot-look.  Any claude process whose environ was unreadable, or a
          roster that failed to parse, forces UNKNOWN.

    MEASURED against the live fleet 2026-07-24, and each rung earned its place:
        pane A           LIVE by R1 only (no --resume in argv)
        pane B           LIVE by R2 only (no HELM_CHAT_NAME in environ)
        pane C           LIVE by R2 via the pid-keyed record — it had NEITHER
                         HELM_CHAT_NAME nor --resume, so both of the signals
                         visible in /proc missed it
    An earlier draft of this docstring claimed that third pane was invisible to
    the guard. Measurement refuted that, which is exactly why the claim below is
    kept to what is actually verified rather than to what seemed likely.

    HONEST FAILURE MODES — this returns DEAD while a holder exists only when ALL
    of: no live claude process names the seat (R1 blind), AND no session id in
    the seat's chat-roster history is held by a live pid-keyed record or a
    `--resume` argv (R2 blind), AND every claude environ on the box was readable
    (R3 quiet). The concrete shape of that gap is a pane whose session helm has
    NEVER seen in its chat roster — a seat that self-resumed and then never
    re-joined chat under the new id. helm holds no evidence tying such a pane to
    the name, so `--force` is the only thing that should ever run against it.
    Nothing here consults transcript age: a pane thinking for an hour and one
    that exited an hour ago look identical by mtime, which is the specific
    mistake that produced a duplicate on one session.
    """
    if procs is None:
        procs, unreadable = claude_processes()
    unreadable = unreadable or []
    hits = named if named is not None else [p for p in procs if p.get("seat") == seat]
    if hits:
        # SAY WHICH JOIN FOUND IT. `resolve()` hands us `named` with the env
        # join and the SESSION join already merged, and this line used to call
        # every one of them "named by" — so a seat whose process carries no
        # HELM_CHAT_NAME at all read as though it declared its own name.
        # Measured on one claude seat: "seat is named by 1 live claude
        # process (pid 1214961)", and pid 1214961's environ has no such key. An
        # operator who believes the name is exported reaches for verbs that read
        # it. A session join is weaker evidence than a declaration and has to
        # look weaker.
        declared = [p for p in hits if p.get("seat")]
        joined = [p for p in hits if not p.get("seat")]
        parts = []
        if declared:
            parts.append("named by %d live claude process%s (pid %s)"
                         % (len(declared), "" if len(declared) == 1 else "es",
                            ", ".join(str(p["pid"]) for p in declared)))
        if joined:
            parts.append("joined by current session to %d live claude "
                         "process%s (pid %s)"
                         % (len(joined), "" if len(joined) == 1 else "es",
                            ", ".join(str(p["pid"]) for p in joined)))
        return LIVE, "seat is " + " and ".join(parts)
    sids, failed = roster_sessions(seat)
    if not failed and sids:
        from . import sessions
        held = sessions.live_sids()
        for sid in sids:
            if sid in held:
                return LIVE, ("session %s… (from this seat's chat-roster "
                              "history) is open in pid %d"
                              % (sid[:8], held[sid]))
    if failed:
        return UNKNOWN, ("helm's chat roster could not be read, so the seat's "
                         "session history is unknown — refusing rather than "
                         "assuming it holds nothing")
    # R3 — the rung every other consumer now copies, from the same predicate.
    blind = cannot_look(unreadable, "helm cannot clear this seat as DEAD")
    if blind:
        return UNKNOWN, blind
    return DEAD, ("no live claude process names this seat, and none of its %d "
                  "known session id%s is open"
                  % (len(sids), "" if len(sids) == 1 else "s"))


# ---------------------------------------------------------------------------
# provenance — helm's own register vs orca's panes
# ---------------------------------------------------------------------------

def helm_spawned():
    """{seat: spawn_record} for every seat helm's OWN register accounts for.

    Read through seat.py's keepers so there is one definition of "helm spawned
    this"; a seat whose register was never written simply is not here.
    """
    from . import seat
    out = {}
    for family, name in seat._minted_seats():   # yields (family, seat) pairs
        rec = seat._spawn_record(seat._instance_dir(family, name))
        if isinstance(rec, dict) and rec.get("seat") == name:
            out[name] = rec
    return out


def spawn_register_census():
    """([(name, path, record)], [(path, why)]) — every spawn register record
    helm holds, CHECKED, and every place one could be that did not read.

    THE REGISTER'S DOMAIN, NOT THE MINTED PROXIES'. `helm_spawned` walks
    `_minted_seats`, which skips a family with no `config.yaml` and an
    instance with no `config.yaml` before it looks for spawn.json. A register
    that survives its config is still helm's own record of the seat, so a
    caller that turns the register's silence into authority walks the domain
    `registered_seats` walks, through the same enumeration
    (`seat_lifecycle._register_candidates`): every `spawn.json` beside a seats
    entry and under its `instances`, whatever config sits beside it.

    EVERY STEP CHECKED. A missing path is absence, unless a link at it or
    above it dangles. Every other failure is UNREADABLE and named with its
    path: a dangling link, a directory listing that raised, a spawn.json that
    raised on a strict read, and a JSON value that is not an object. Every
    readable object is returned with the name its place gives it; the caller
    decides which fields name the seat. A caller that measures absence must
    refuse when the second list is not empty. `helm_spawned` stays the
    minted-proxy resolver for its other callers.
    """
    # `seat` is imported in this scope because the co-occurrence census in
    # tests/test_seat_facade_injection.py requires an accepted facade import
    # beside any direct impl import. It asserts nothing about import order.
    from . import home, pk, seat  # noqa: F401
    from .seat_lifecycle import _register_candidates
    records, unreadable, missing = [], [], object()

    def failed(path, exc):
        unreadable.append((path, "%s: %s" % (type(exc).__name__, exc)))

    stop = home.global_dir()

    def dangling(path, exc):
        """A missing path is absence only when no link on the way to it
        dangles. `os.listdir` and `open` follow links, so a dangling link at
        the path or at any directory above it raises FileNotFoundError, the
        same as a path never written. That is an entry that exists and cannot
        be read."""
        while path and path != stop:
            if os.path.islink(path) and not os.path.exists(path):
                failed(path, exc)
                return
            up = os.path.dirname(path)
            path = None if up == path else up

    def listing(path, exc):
        if isinstance(exc, FileNotFoundError):
            dangling(path, exc)
        else:
            failed(path, exc)

    for name, path in _register_candidates(listing):
        try:
            rec = pk.read_json(path, missing, strict=True)
        except Exception as exc:        # noqa: BLE001 — named UNREADABLE
            failed(path, exc)
            continue
        if rec is missing:
            dangling(path, FileNotFoundError("no such file: %s" % path))
        elif not isinstance(rec, dict):
            unreadable.append((path, "a JSON %s, not an object"
                               % type(rec).__name__))
        else:
            records.append((name, path, rec))
    return records, unreadable


def _session_join_handles(by_handle, procs, resolve, roster_sids=None,
                          roster_failed=False):
    """Fill `by_handle` for seats whose process carries NO HELM_CHAT_NAME.
    Returns a reason string when the roster could not be read, else None.

    `roster_sids`/`roster_failed` are INJECTABLE for the same reason
    `procs`/`unreadable` are one layer up: the roster is a live tmpfs file the
    whole fleet writes, so a caller that has already read it must be able to
    hand the SAME reading down, and a unit test must be able to describe a
    roster instead of silently joining against the owner's real one. A fixture
    that reaches the live roster passes or fails by what else is running on the
    host, which is not a test.

    THE DEFECT THIS EXISTS FOR, measured 2026-08-04 on this host. `resolve()`
    walks TWO joins — the env one, then `_session_joined` through the chat
    roster's current session id — and this listing walked only the first. So
    `seat where helm-claude` answered `orca-adopted — LIVE` with a pid, a pane
    key and a resolved handle, while `seat panes` called the same live pane
    `unowned` in the same second. An operator read the weaker surface, concluded
    a working seat was unreachable, and prescribed a relaunch for a seat that
    needed none; the owner compacted it by hand on the strength of that. Two
    joins of unequal power is one join too few, not a presentation difference.

    EVERY REFUSAL STAYS REFUSED. This calls `_session_joined` rather than
    reimplementing it, so ambiguity (one sid on two live processes) and
    contradiction (a row declaring a different seat) still address nothing —
    read that docstring before touching this, because a wrong join here does not
    fail loudly, it types into another agent's pane.

    AND ONE REFUSAL THIS RUNG ADDS. `_session_joined` reasons about ONE seat, so
    it cannot see two seats arriving at the same pane. A handle claimed by more
    than one seat is left UNOWNED rather than awarded to whichever sorted first
    — the same law one rung up: what is left after spending the evidence is
    ambiguity, and ambiguity names no pane.
    """
    if resolve is None:
        return None
    if roster_sids is None and not roster_failed:
        roster_sids, roster_failed = roster_current_sids()
    sids, failed = roster_sids or {}, roster_failed
    if failed:
        # NOT an exception and NOT a silent skip: the env join above is
        # unaffected and its labels stand, so the listing is still worth
        # returning — it is the UNOWNED rows that are now "could not tell".
        return ("the chat roster could not be read, so no pane could be "
                "matched to a seat by session")
    claimed = {}
    for seat, sid in sorted(sids.items()):
        named = [p for p in procs if p.get("seat") == seat]
        joined, _refused = _session_joined(seat, procs, sid, named)
        for p in joined:
            if not p.get("pane_key"):
                continue
            try:
                handle = (resolve(p["pane_key"]) or {}).get("handle")
            except Exception:                  # noqa: BLE001 — fail-open law
                continue
            if handle:
                claimed.setdefault(handle, set()).add(seat)
    for handle, seats in claimed.items():
        if handle in by_handle:                # the env join WINS, untouched
            continue
        if len(seats) == 1:
            by_handle[handle] = next(iter(seats))
    return None


def pane_rows(adapter=None, procs=None, unreadable=None, roster_sids=None,
              roster_failed=False):
    """([row], note) — every pane the metaharness holds, labelled by provenance.

    Each row carries the adapter's own pane fields plus `provenance` and `seat`.
    FAIL-OPEN: no metaharness, a dead daemon or a garbage reply answers ([],
    reason) — one honest line, never an exception and never a bare [] a caller
    could read as "orca is up and holds nothing".

    `procs`/`unreadable` let a caller that has already scanned /proc reuse the
    result, so an operator surface can report how many claude processes could
    NOT be identified alongside the listing — an `unowned` row is only honestly
    "no identity found" when the lookup could actually see everything.

    THAT LAST SENTENCE WAS A PROMISE THIS FUNCTION DID NOT KEEP. It accepted
    `unreadable`, and on the self-census path computed one, and then never
    referenced it again — so every UNOWNED label came back meaning "no identity
    found" whether or not helm could look. `seat._panes` was unharmed because it
    takes its OWN census and prints the unidentified pids itself; the caller
    that was NOT is `seat_identity`, which called `pane_rows()` bare and read
    the seat labels as a complete set.

    A CALLER THAT SUPPLIES THE CENSUS OWNS THE BLINDNESS — it has `unreadable`
    in its hand. A caller that does NOT has no other way to learn it, so the
    self-census path now REFUSES while blind rather than handing back labels
    that overclaim. Found by the paired blind/sighted consumer census, which
    asks what a function DOES when the census is blind rather than whether its
    source mentions the word.
    """
    from . import harness
    if adapter is None:
        adapter = harness.detect()
    if adapter is None:
        return [], harness.RECOMMENDATION
    panes = getattr(adapter, "panes", None)
    if panes is not None:                      # orca: the RPC leg, no subprocess
        rows, err = panes()
    else:                                      # herdr/other: the CLI leg
        try:
            rows, err = adapter.list(), None
        except harness.HarnessError as e:
            rows, err = [], str(e)
    if err:
        return [], err
    # KEYED BY STORAGE, LABELLED BY IDENTITY. The register's KEY is the name a
    # seat was spawned under and never changes; a durable rename records the
    # current name in `identity`. Every consumer of this map wants the name to
    # SHOW — a pane label, a title, an operator row — so it must resolve the
    # pair rather than hand back the storage key, which after a rename is a
    # name nothing else in the fleet uses.
    registered = {rec.get("handle"): (rec.get("identity") or name)
                  for name, rec in helm_spawned().items() if rec.get("handle")}
    if procs is None:
        procs, unreadable = claude_processes()
        blind = cannot_look(unreadable, "which panes belong to which seat")
        if blind:
            return [], blind
    procs, records_blind = with_record_sids(procs)
    # pane_key -> seat, from the process layer. resolvePane is the only bridge
    # from a pane key to a current handle, and it is one RPC per IDENTIFIED
    # claude process (a handful), not per pane.
    by_handle = {}
    resolve = getattr(adapter, "resolve_pane", None)
    # AGENT HANDLES: every pane some live claude process sits in, named or
    # not, so an UNOWNED pane can say whether it holds an agent or a shell.
    agents, agents_blind = set(), None
    for p in procs:
        if not p.get("pane_key") or resolve is None:
            continue
        try:
            handle = (resolve(p["pane_key"]) or {}).get("handle")
        except Exception as exc:               # noqa: BLE001 — fail-open law
            agents_blind = ("the pane key of claude pid %s could not be "
                            "resolved (%s)" % (p["pid"], type(exc).__name__))
            continue
        if handle:
            agents.add(handle)
            if p.get("seat"):
                by_handle.setdefault(handle, p["seat"])
    # SECOND JOIN — the rung `resolve()` already walks and this listing did not.
    # It runs AFTER the env join and only fills handles that join left empty, so
    # no spawned or env-labelled pane's answer can change.
    roster_blind = _session_join_handles(by_handle, procs, resolve,
                                         roster_sids=roster_sids,
                                         roster_failed=roster_failed)
    roster_blind = roster_blind or records_blind
    out = []
    for row in rows:
        handle = row.get("handle")
        if handle in registered:
            prov, seat_name = HELM_SPAWNED, registered[handle]
        elif handle in by_handle:
            prov, seat_name = ORCA_ADOPTED, by_handle[handle]
        else:
            prov, seat_name = UNOWNED, None
        labelled = dict(row, provenance=prov, seat=seat_name)
        if prov == UNOWNED:
            # True/False only when every claude pane key resolved; a pane
            # that matched none while one could not be resolved is unknown.
            labelled["holds_agent"] = (True if handle in agents
                                       else None if agents_blind or resolve is None
                                       else False)
        if prov == UNOWNED and roster_blind:
            # THE REASON TRAVELS WITH THE ANSWER. UNOWNED means "no identity
            # found", and with the roster unreadable the session join could not
            # look — so this particular UNOWNED is "could not tell", and the two
            # must never be the same value. Only unowned rows carry it: a row
            # that got a label has positive evidence and is unaffected.
            labelled["identity_partial"] = roster_blind
        out.append(labelled)
    return out, None


def with_record_sids(procs):
    """([proc], why|None) — each proc with no argv `--resume` sid gains the
    `record_sid` Claude's own pid-keyed session record names for it.

    A pane Orca opened runs a bare `claude`, so it has no `--resume <sid>`
    and no HELM_CHAT_NAME, and the session join could not see it. The record
    `<home>/sessions/<pid>.json` carries the session and the procStart of the
    process that wrote it; the sid is taken ONLY when that procStart equals
    this process's birth stamp, so a record left on a recycled pid names
    nothing. Two sessions claiming one incarnation name nothing either.
    `why` is set when the records could not be enumerated at all."""
    from . import beacons
    records = beacons.holder_records()
    if records is None:
        return procs, ("the claude session records could not be enumerated, "
                       "so a process with no --resume could not be matched "
                       "to its session")
    by_ident = {}
    for sid, (pid, start) in records.items():
        if start is not None:
            by_ident.setdefault((int(pid), str(start)), []).append(sid)
    out = []
    for p in procs:
        hits = by_ident.get((p["pid"], str(p.get("start"))), ())
        if not p.get("resume_sid") and p.get("start") and len(hits) == 1:
            p = dict(p, record_sid=hits[0])
        out.append(p)
    return out, None


def _session_joined(seat, procs, current_sid, named, rows=None):
    """([proc], refusal) — the rows tied to `seat` by SESSION, or NOTHING plus a
    reason. Never a plausible row.

    WHY THE JOIN EXISTS. A proc's `seat` is read from the process ENVIRONMENT
    (HELM_CHAT_NAME), which launch.sh exports only for seats helm SPAWNS. A seat
    the operator launched by hand carries no such var, so its row reads
    seat=None and an env-only match cannot address it at all — 8 of the 16 live
    claude processes on this host were nameless when this was measured.

    WHY IT REFUSES INSTEAD OF GUESSING. This resolves an ADDRESS, and the cost
    of a wrong address is a keystroke in another agent's session. REPRODUCED
    2026-07-30 against the first draft of this join, which matched on the whole
    roster history and checked nothing else: a pane declaring
    a codex seat's HELM_CHAT_NAME, resumed on a sid still in another seat's
    history, was folded in, supplied the handle, and `send_to_pane` injected
    /compact into it and returned "resumed" — while expect_pids named the
    correct pid the entire time. Unreachable fails loudly; mis-reachable does
    not, so this rung ladder mirrors `_nameless_identity` deliberately.

      CURRENT SESSION ONLY — `roster_identity` measures why history is liveness
      evidence and never an address.
      CONTRADICTION REFUSES — a row declaring a DIFFERENT seat is not this
      seat's pane whatever sid it carries. `_nameless_identity` already refuses
      this exact shape ("must not inject into a named seat's pane"). DIFFERENT
      is decided by `_declared_verdict` against the roster's own rename record,
      NOT by spelling: read that function before touching this line.
      AMBIGUITY REFUSES — one session claimed by two live processes names no
      pane. Dedup by pid cannot reach this: the two ARE different pids, so the
      dedup that guards the harmless case leaves the harmful one untouched.

    REFUSING RETURNS ([], why), NOT A FILTERED LIST. With two processes on one
    session, dropping the contradictory one would leave a survivor that is
    "unique" only because the disqualifying evidence was thrown away.

    `rows` IS A ROSTER THE CALLER ALREADY HOLDS, and it changes cost rather
    than answer — the same contract `resolve`'s `census` carries. It is read
    ONLY by the declared-name rung; a caller that omits it pays a roster read
    exactly when that rung fires, which is never on the common path.
    """
    if not current_sid:
        return [], None
    seen = {p["pid"] for p in named}
    hits = sorted((p for p in procs
                   if p["pid"] not in seen
                   and current_sid in (p.get("resume_sid"),
                                       p.get("record_sid"))),
                  key=lambda p: p["pid"])
    if not hits:
        return [], None
    if len(hits) > 1:
        return [], ("%d live processes hold session %s… (pid %s) — one "
                    "session cannot name two panes, so this join addresses "
                    "none of them"
                    % (len(hits), current_sid[:8],
                       ", ".join(str(p["pid"]) for p in hits)))
    p = hits[0]
    if p.get("seat"):
        return _declared_verdict(seat, p, current_sid, rows)
    return hits, None


def _declared_verdict(seat, p, current_sid, rows):
    """([proc], refusal) for the ONE rung where a launch-time record meets a
    live one — the process on `seat`'s CURRENT session that declares a
    DIFFERENT name than `seat`.

    A FIELD'S LIFECYCLE DECIDES WHAT IT CAN MEAN, and this rung read one field
    as though it had a lifecycle it does not. `p["seat"]` is HELM_CHAT_NAME out
    of `/proc/<pid>/environ`, which the kernel writes ONCE at exec and never
    rewrites: it says what the process was LAUNCHED as, and it keeps saying
    that for as long as the process lives. A roster key is RE-POINTED by
    `helm chat seat rename` while the process runs. So "environ says X, the
    roster says Y"
    is EQUALLY the signature of a rename that worked perfectly and of a process
    belonging to another seat — and the old text called it "contradictory
    evidence" and asserted that "injecting on it would type into another seat's
    session", which is a claim about a session nobody measured.

    HELM ALREADY OWNS THE DISCRIMINATOR AND THIS RUNG DID NOT ASK IT.
    `helm chat seat rename` records the old spelling on the NEW row as a rename
    alias with an expiry, and `seats_common.live_alias` is the one resolver
    that reads it — the same seam `actors._resolve` uses to admit a process
    whose own environ still spells it the old way, and the one `--seat <old>` is
    against. MEASURED, on the live host that filed task/2739: one process
    declares a name that is no longer a roster key, the roster key that
    replaced it holds that name as a live rename alias, and `live_alias`
    answers with the current key. The roster already says they are one seat;
    only this rung disagreed.

    THE ALIAS DOES NOT LOOSEN THE GUARD, AND live_alias IS WHY. Its own law is
    "an exact roster key is never an alias": the moment `codex-8` is re-admitted
    as its own roster row, it stops resolving to gtp-codex and the refusal below
    fires again — which is exactly the two-live-seats case this rung exists for.
    What is admitted is ONE seat under two spellings, never two seats.

    AND THE REFUSAL NAMES THE INPUT IT DISTRUSTS. With no live alias helm holds
    a launch-time record and a current key that disagree and NO evidence that
    separates the two readings, so it refuses and SAYS THAT — rather than
    implying the process is stale or is somebody else's. A roster it could not
    read is a THIRD answer and is reported as itself: a refusal names the input
    it checked, never the one it could not obtain.
    """
    from . import seats_common
    declared = p.get("seat")
    rows, rows_failed = _alias_rows(rows)
    if rows_failed:
        return [], ("pid %d carries --resume %s… and its environ declares a "
                    "HELM_CHAT_NAME other than %s. That name is written at "
                    "exec and never updated, so it may be this seat's "
                    "pre-rename spelling — but the ROSTER, the only record of "
                    "a rename alias, could not be read (%s), so helm cannot "
                    "tell the two apart and names no pane. This measures "
                    "nothing about the process."
                    % (p["pid"], current_sid[:8],
                       seats_common._seat_label(seat), rows_failed))
    alias_of, _until = seats_common.live_alias(declared, rows)
    if alias_of is not None \
            and str(alias_of).casefold() == str(seat).casefold():
        return [p], None            # one seat, spelled the way it was launched
    return [], ("pid %d carries --resume %s… — the session the roster records "
                "as %s's current one — and its environ declares "
                "HELM_CHAT_NAME=%s. /proc/<pid>/environ is written at exec and "
                "the kernel never rewrites it, so that name is what this "
                "process was LAUNCHED as and not a claim about what it is now: "
                "the disagreement is equally the signature of a rename helm no "
                "longer holds an alias for and of a process that belongs to "
                "another seat. The roster admits no live rename alias binding "
                "%s to %s, so helm cannot separate those two readings and "
                "names no pane. Nothing here measures whether either record is "
                "stale."
                % (p["pid"], current_sid[:8], seats_common._seat_label(seat),
                   seats_common._seat_label(declared),
                   seats_common._seat_label(declared),
                   seats_common._seat_label(seat)))


def _alias_rows(rows):
    """(rows, failure reason or None) — the roster this rung resolves a rename
    alias against.

    TRI-STATE, BECAUSE THE FAIL-OPEN READER CANNOT SAY WHICH. A caller that
    already read the roster under its own lock passes `rows` and this is a
    no-op. A caller that did not (`resolve`) gets `seats.roster_checked`, whose
    whole reason for existing is that an UNREADABLE roster and an EMPTY one are
    different facts: `seats_common.roster()` collapses both to {}, and
    resolving an alias against {} answers "no such alias" about a file nobody
    could open. The refusal would then be true in its verdict and WRONG about
    which input decided it, which is the class this split exists to end.
    """
    if rows is not None:
        return rows, None
    from . import seats
    try:
        got, failed = seats.roster_checked()
    except Exception as e:                  # noqa: BLE001 — a rung never raises
        return {}, "%s: %s" % (e.__class__.__name__, e)
    return (got or {}), ("the seat roster could not be read" if failed
                         else None)


def resolve(seat, adapter=None, census=None):
    """The `seat where` answer for a seat helm's own register does NOT know.

    Returns a dict with provenance/state/evidence, plus the pane fields when the
    pane can be resolved. A seat helm has never seen at all answers None so the
    caller can keep its existing "unknown seat" message for a genuine typo.

    `census` IS A (procs, unreadable) PAIR A CALLER ALREADY HOLDS, and it
    changes cost rather than answer. MEASURED: this call is about 362ms per
    seat and roughly 264ms of that is `claude_processes()` re-walking /proc,
    so a caller resolving N seats pays for N walks of the same host. The pane
    resolve itself is about 8ms. A health surface asking about a fleet is
    exactly that caller.

    IT IS A CACHE OF THE INPUT, NEVER OF THE VERDICT. Every rung below —
    the env match, the session join, its ambiguity and contradiction refusals
    — runs unchanged on the supplied rows, so a caller cannot buy a different
    answer by supplying a different census, only a staler one. Passing a
    census a caller took long ago is the one hazard, and it is the caller's to
    bound: the rows are a point-in-time reading of live processes and this
    function has no way to tell how old they are.
    """
    procs, unreadable = census if census is not None else claude_processes()
    # THE SAME RECORD RUNG `pane_rows` WALKS, so `seat where` and `seat panes`
    # name one set of panes: a bare `claude` Orca opened gains `record_sid`
    # only on a (pid, procStart) match claimed by exactly one sid. The send
    # path re-proves that same (pid, procStart) at send time.
    procs, _records_blind = with_record_sids(procs)
    current_sid, sids, roster_failed = roster_identity(seat)
    named = [p for p in procs if p.get("seat") == seat]
    # SESSION IS THE SECOND JOIN — and without it the OWNER'S OWN coordinating
    # seat was unreachable while its pane sat right there. A hand-launched seat
    # exports no HELM_CHAT_NAME, so its row reads seat=None and the env match
    # above finds nothing even though the row holds a live `pane_key`.
    #
    # ADDITIVE by construction: env-labelled rows are matched FIRST and keep
    # their order, so no spawned seat's resolution changes.
    #
    # `_session_joined` is where every refusal lives — read its docstring before
    # touching this, because a wrong join here does not fail loudly, it types
    # into another agent's pane.
    joined, session_refused = _session_joined(seat, procs, current_sid, named)
    named += joined
    # ANSWERING None IS A POSITIVE CLAIM — "helm has never heard of this name"
    # — and both callers print it as a typo (`_unknown_seat_reason`). The
    # finding in dispatch 90845108: with a live claude helm could not
    # identify, ([], [pid]) took this exit, so "I could not look" was printed as
    # "no such seat". One of those pids may BE this seat. The refusal contract
    # is the same one `seat_liveness` R3 holds — it just had to reach the exit
    # that runs BEFORE the state is computed.
    blind = cannot_look(unreadable, "helm cannot say it has never seen %s"
                                    % seat)
    if not named and not sids and not roster_failed and not blind:
        return None                            # genuinely unknown to helm
    state, evidence = seat_liveness(seat, procs=procs, unreadable=unreadable, named=named)
    info = {"seat": seat, "provenance": ORCA_ADOPTED, "state": state,
            "evidence": evidence, "sessions": sids,
            # STAMPED, NEVER BARE. `authorized_handle` refuses a pid with no
            # birth stamp — "a pid is a slot the kernel reuses, not an
            # identity" — so a consumer handed bare integers here can never
            # authorize a send, and the refusal lands BEFORE anything is
            # typed. ProcIdent subclasses int, so every existing reader that
            # compares or formats these is unaffected; only the authorization
            # path can tell the difference, and it is the one that needs to.
            "pids": [ProcIdent(p["pid"], p.get("start")) for p in named],
            # THE CANDIDATE SET, PUBLISHED — the pane-carrying rows this
            # resolution actually considered. `send_to_pane` re-derived its own
            # from the env label and the two silently disagreed; see the note
            # at its ambiguity guard. One definition, one place.
            "pane_pids": [ProcIdent(p["pid"], p.get("start"))
                          for p in named if p.get("pane_key")],
            # THE RICH-SHAPED SURFACE SPEAKS THE RICH VOCABULARY (#141). This
            # dict claims seat_liveness's shape, so its state must be a
            # seat._STATE_NAMES member — `seat where --json` printed a raw
            # transport LIVE and a whitelisting watcher called a posting seat
            # stalled. info["state"] above keeps the transport word on the
            # provenance surface; only this projection translates.
            # TWO QUESTIONS WERE WEARING ONE WORD, and `attached` is the one
            # that never had a name. This module answers "is a process holding
            # this seat open" — the duplicate-resume guard. `seat_liveness`
            # answers "is this agent able to work" by reading the pane. Those
            # diverge exactly where a seat has BOTH: measured 2026-08-05,
            # a codex seat read LIVE here and IDLE from seat_liveness at one instant,
            # and a reader branching on RUNNING/IDLE/CONTEXT_FULL falls to its
            # else-branch on a seat that is merely held open. Widening
            # _STATE_NAMES to admit LIVE made that answer LEGAL; it never made
            # the other answer AVAILABLE.
            #
            # ADD-AND-DEPRECATE, NOT REPOINT. `liveness` keeps its exact current
            # value because a consumer pins it (tests/test_orcaadopt.py) and
            # repointing a key's MEANING under a live reader is the silent kind
            # of break. It gains `question` instead, so the answer says which
            # question it answered rather than leaving the key name to imply it.
            #
            # THIS MODULE NEVER CALLS seat.seat_liveness, and that is load
            # bearing: seat.seat_liveness FALLS BACK to resolve() for
            # family-less seats (seat.py, _liveness_from_orcaadopt), so a call
            # in that direction would cycle on exactly the orca-adopted seats
            # the fallback exists to serve. The dependency stays one-way.
            #
            # NOTE THE COLLISION, because it is this lane's own defect one
            # level up: THIS module also defines a `seat_liveness`, and it is a
            # DIFFERENT function — (state, evidence) answering "is anything
            # ALREADY holding this seat open", against seat.py's dict answering
            # the pane question. Two functions, one name, two questions. The
            # local one is what line 985 calls; seat.py's is the one that must
            # never be called from here.
            "attached": {"seat": seat, "state": RICH_STATE.get(state, "UNKNOWN"),
                         "question": ATTACHED_QUESTION, "evidence": evidence},
            "liveness": {"seat": seat, "state": RICH_STATE.get(state, "UNKNOWN"),
                         "blocked_on": None, "evidence": evidence,
                         "question": ATTACHED_QUESTION,
                         "detail": None}}
    if session_refused:
        # REFUSE AND SAY WHY. "I could not tell" and "here is the answer" must
        # never be the same value, so the reason travels with the answer instead
        # of being dropped on the floor (the worktree_panes law in harness.py).
        info["session_refused"] = session_refused
    if blind:
        # Same law, the other blindness. A resolution taken beside a claude
        # process helm could not identify is a resolution over an incomplete
        # host, and the surface that prints `pids` and `handle` must say so —
        # they are a REPORT here, and an operator reading one has to know the
        # census behind it had a hole in it.
        info["unidentified"] = blind
    from . import harness
    if adapter is None:
        adapter = harness.detect()
    resolver = getattr(adapter, "resolve_pane", None)
    for p in named:
        if not p.get("pane_key") or resolver is None:
            continue
        try:
            pane = resolver(p["pane_key"]) or {}
        except Exception as e:                  # noqa: BLE001 — fail-open law
            info["pane_error"] = str(e)
            continue
        if pane.get("handle"):
            # THIS HANDLE IS A REPORT, NEVER AN AUTHORIZATION. The loop takes
            # the FIRST row that resolves, and "first" is an ordering, not a
            # decision — every wrong-pane defect in this file has been a caller
            # treating one as the other. It used to be published with a
            # `handle_pid` so a delivery leg could ASK whom it belonged to, and
            # `authorized_handle` reused it whenever that pid matched; a
            # cross-family read showed pid equality across two observations is
            # not identity, so the delivery leg no longer accepts this handle
            # at all and the attribution field is gone with it. What survives
            # is what `helm seat where` prints for a human.
            info["handle"] = pane["handle"]
            info["pane_key"] = p["pane_key"]
            info["worktree_id"] = p.get("worktree_id")
            break
    return info


# ---------------------------------------------------------------------------
# turn-restart — inject into a pane that is ALIVE (not a relaunch)
# ---------------------------------------------------------------------------
#
# THE POLARITY INVERTS HERE, and that is the whole reason these are separate
# from `resume` below. A relaunch-resume treats LIVE as a REFUSAL (something
# already holds the seat, so minting a second process would duplicate it). A
# turn restart after a compaction is the opposite act: the process survived the
# compaction and is merely sitting at an empty prompt, so LIVE is exactly the
# state that makes injection correct and DEAD is what makes it pointless.
#
# WHY IT NEEDED WIRING AT ALL (measured 2026-07-29, on this integrator's own
# pane): `resumeturn._registered` and `autocompact._pane_action` both call
# `seat._seat_family()` and stop at its error, while `seat._resume` falls
# through to `resolve()` above. The adoption path was built, tested and correct
# — 53 of the 59 seats in the roster carry no family name, and every one of
# them answered a compaction with "unknown seat" and then sat idle until the
# OWNER typed into its pane. Built is not wired.


def turn_restart_identity(seat, session, procs=None, unreadable=None):
    """(ident, reason) — may a compaction resume be ADDRESSED to this seat?

    `ident` is a `ProcIdent` for the single process the decision was made
    about, or None to refuse. Returning the WINNER rather than a boolean is
    what lets the delivery leg re-prove the same process later: the caller must
    never re-derive it from the seat name, because the name is exactly the
    ambiguous part.

    IT CARRIES THE BIRTH STAMP, and that is what makes "the same process" a
    checkable claim rather than a number that happens to match. The decision
    and the send happen in different processes separated by a settle delay, so
    the pid can legitimately belong to somebody else by the time it is used.

    `seat=None` is a REAL case, not a degenerate one: a pane launched with no
    HELM_CHAT_NAME at all (measured live 2026-07-29 — sid in argv, name
    nowhere). That rides `_nameless_identity` below, keyed on the session's
    OWN sid instead of a name.

    FILE READS ONLY (/proc + the tmpfs roster): this runs inside the 5-second
    SessionStart hook budget, so it must never reach the metaharness. Pane
    resolution is the CHILD's job, after the settle delay.

    It is the adopted-seat analogue of the spawn-register check, rung for rung:
    an address to send to, a session that does not CONTRADICT the compaction,
    and — only after those have been spent — a UNIQUE surviving holder.

    NARROW BEFORE DECLARING AMBIGUITY. The first draft refused as soon as two
    processes shared the seat name, and dogfooding it on this integrator's own
    pane refused immediately: a second claude process was carrying the same
    inherited HELM_CHAT_NAME for a few seconds. A seat name is inherited by
    everything a seat spawns, so a bare name collision is the NORMAL state of a
    busy seat, and a guard keyed on it would refuse resumes for precisely the
    seats doing the most work. What actually distinguishes the pane from its
    children is evidence this function already holds: only the pane carries an
    ORCA_PANE_KEY, and only the compacted process carries `--resume <sid>` for
    THIS session. Ambiguity is what is left after spending that, never the
    opening move.

    SESSION EVIDENCE IS ASYMMETRIC, deliberately: positive disproof refuses,
    missing evidence does not. A process that never wrote `--resume` in its
    argv and a roster wiped by a reboot are both ordinary, and refusing them
    would reinstate the very idleness this closes.
    """
    if procs is None:
        procs, unreadable = claude_processes()
    if not seat:
        # No name to key on is NOT "nothing to key on": a pre-fix quirk here
        # would have matched every nameless proc (None == None) and then
        # accepted one WITHOUT sid evidence through the absence-asymmetry
        # below — a guess. The nameless question needs POSITIVE evidence, so
        # it gets its own rung ladder.
        return _nameless_identity(session, procs, unreadable or [])
    # RUNG 0 — CAN HELM SEE THE HOST AT ALL. This ran only on the `not named`
    # branch, which is exactly backwards: a census with a hole in it is MORE
    # dangerous when something readable answers, because then there is a
    # plausible winner to hand the address to. The finding in
    # dispatch 90845108: "identity authorizes one readable candidate despite
    # an unreadable rival". Every rung below narrows a candidate SET, and
    # narrowing a set helm knows is incomplete is arithmetic on a lie.
    blind = cannot_look(unreadable, "no candidate for %s can be ruled out"
                                    % seat)
    if blind:
        return None, blind
    named = [p for p in procs if p.get("seat") == seat]
    if not named:
        return None, "no live claude process names seat %s" % seat

    # RUNG 1 — an address. A subagent or a `-p` one-shot has no pane.
    live = [p for p in named if p.get("pane_key")]
    if not live:
        return None, ("%d live process%s name%s %s but none carries an "
                       "ORCA_PANE_KEY — helm has no address to inject into "
                       "(metaharness: %s)"
                       % (len(named), "" if len(named) == 1 else "es",
                          "s" if len(named) == 1 else "", seat,
                          _detected_host()))

    # RUNG 2 — the session id, the one field that names WHICH process compacted.
    how = "no session evidence either way, and an absent record is not a " \
          "contradiction"
    if session:
        exact = [p for p in live if p.get("resume_sid") == session]
        if exact:
            live, how = exact, "argv --resume %s…" % session[:8]
        else:
            # A token that is not session-SHAPED is not a CONTRADICTION, and
            # reading it as one is how this rung declared a live seat gone.
            # `claude --resume` accepts a session TITLE as well as an id, so a
            # pane launched `--resume "helm coordinator 7-22"` carries a
            # resume_sid that can never equal any session id. The old test was
            # `!= session`, which is TRUE for every title ever written, so the
            # rung answered "that pane holds a DIFFERENT session" when what it
            # had was NO EVIDENCE ABOUT WHICH SESSION IT HOLDS.
            #
            # MEASURED, and the report is the proof: the integrator
            # compacted and this rung posted "every addressable process for
            # <seat> holds a different session (pid 407141/helm coo…)
            # — the pane that compacted (56a628d4…) is gone". pid 407141 was
            # the seat's OWN live pane, mid-turn; `helm coo` is the first eight
            # characters of its title. The alert names pane input as the
            # fallback, so acting on it means typing into a pane that is
            # working.
            #
            # Absent and unparseable now share one bucket because they are one
            # fact. The bucket is deliberately still NARROW — a well-formed
            # session id that differs IS a real contradiction and still lands
            # in the refusal, because widening past that would hand rung 3 more
            # candidates and rung 3's own comment records what that costs.
            silent = [p for p in live
                      if not session_shaped(p.get("resume_sid"))]
            if not silent:
                return None, ("every addressable process for %s holds a "
                               "different session (%s) — the pane that "
                               "compacted (%s…) is gone"
                               % (seat, ", ".join("pid %d/%s…"
                                                  % (p["pid"], str(p["resume_sid"])[:8])
                                                  for p in live), session[:8]))
            live = silent

    # RUNG 3 — the chat roster, for a pane too old to have written an argv sid.
    #
    # CURRENT SESSION ONLY, and this rung is where the round-1 split was NOT
    # applied. `roster_identity` measures the law — history is LIVENESS
    # evidence and never an ADDRESS — and this function returns an ADDRESS, so
    # it is bound by the addressing half. Reading the history here was the
    # SECOND path the split missed, and it was not theoretical: REPRODUCED at
    # a032ce7 (dispatch 1b4039cc) with a compaction carrying a sid that
    # is merely in this seat's history. The rung authorized it, and the handoff
    # written for that OLD session was injected into the pane running the
    # seat's CURRENT one — returning "resumed", silently, into live work.
    #
    # THE SEAT NAME IS NOT THE QUESTION. Both sessions belong to the same seat;
    # what differs is WHICH SESSION'S handoff this is, and a pane replaying a
    # retired transcript is a different agent from the one holding the seat now.
    #
    # AND `current_sid` IS THE ONLY KEY THIS RUNG TURNS. A review
    # (dispatch a4051c69, finding 2) reproduced the half-open door left here: a
    # row carrying `sessions` history and NO `session` yields current_sid=None,
    # which skipped the mismatch check below and then announced the compaction
    # as "%s's current session" — a claim the roster had never made. Absence of
    # a current sid is not permission to fall back to history; it is the
    # register saying it does not know which session this seat holds.
    #
    # AND THE REBOOT ASYMMETRY SURVIVES IT, which is the part that is easy to
    # break while fixing this. Those two cases look identical from inside the
    # rung and are opposite: an EMPTY roster (a reboot wiped tmpfs, a seat that
    # never joined chat) is no evidence at all and must still resume, while a
    # roster that HAS a row for this seat and files this sid under history has
    # answered — its own data model puts the current session in `session` and
    # everything retired in `sessions`. So the emptiness check gates the whole
    # rung and the refusal lives inside it.
    if session and how.startswith("no session"):
        current_sid, sids, failed = roster_identity(seat)
        if not failed and sids:
            if session not in sids:
                return None, ("the chat roster records %d session%s for %s "
                               "and this compaction (%s…) is not among them"
                               % (len(sids), "" if len(sids) == 1 else "s",
                                  seat, session[:8]))
            if not current_sid:
                return None, ("the chat roster holds %s… for %s only as "
                               "HISTORY — it names no current session for the "
                               "seat at all, so it cannot say this compaction "
                               "addresses the live pane; a historical sid is "
                               "liveness evidence and never an address"
                               % (session[:8], seat))
            if session != current_sid:
                return None, ("the chat roster holds %s… for %s as HISTORY, "
                               "not as its current session (%s…) — a "
                               "historical sid is liveness evidence and never "
                               "an address, so this compaction names no pane"
                               % (session[:8], seat, current_sid[:8]))
            how = "chat roster names session %s… as %s's current session" \
                % (session[:8], seat)

    # RUNG 4 — only NOW is a survivor count meaningful.
    if len(live) > 1:
        return None, ("%d addressable processes still name %s after session "
                       "evidence (pid %s) — a resume must never guess which "
                       "one compacted"
                       % (len(live), seat,
                          ", ".join(str(p["pid"]) for p in live)))
    return ident_of(live[0]), "pid %d, %s" % (live[0]["pid"], how)


def _nameless_identity(session, procs, unreadable):
    """(ident, reason) — WHICH pane holds `session`, when no seat name exists.

    WHICH QUESTION THIS ANSWERS, and which it must never be read as answering.
    `seats.own_name()` is DELIBERATELY strict: it answers "may I ACT AS seat
    X", where a name any other process supplied would be impersonation, so a
    session with no HELM_CHAT_NAME declares no seat — and NOTHING here changes
    that. This answers the different, narrower question "which pane do I type
    THIS session's OWN handoff into": the sid comes from the session's own
    SessionStart payload (self-evidence — no other seat's roster row is
    trusted), and the text being delivered is the session's own directive.
    A future nameless-pane gap is fixed by widening THIS resolver, never by
    loosening own_name().

    argv `--resume <sid>` is strictly LESS ambiguous than a seat name: a name
    is inherited by every child a seat spawns (the subagent lesson one
    function up), while `--resume <sid>` appears only in the resumed pane's
    own argv. So the candidate set is keyed on the sid, and — unlike the named
    ladder above, where an ABSENT record is not a contradiction — a nameless
    resolution requires POSITIVE argv evidence: with no name and no sid there
    is nothing tying any pane to this session, and acting anyway is a guess.

    A CHECK THAT CANNOT SEE A CASE RETURNS UNKNOWN, NEVER A VERDICT — the
    landed vcs.ancestry / proxywatch-HUNG law, and the exact failure this
    closes (the pre-fix guard told a human "helm cannot address its pane"
    without ever looking). Concretely: an unreadable environ is "cannot read
    its address", never "it has none"; zero argv hits is "no process
    evidence" (a fresh-launched pane writes no --resume), never "the pane
    does not exist"; two hits is "will not guess". Every refusal states what
    was actually seen.
    """
    if not session:
        return None, ("this compaction carries no session id, so there is "
                      "nothing to match a nameless pane against")
    # RUNG 0 — the same rung the named ladder opens with, and it must come
    # FIRST here for a second reason: an unreadable candidate produces NO row
    # at all now, so its argv is invisible too. Without this, an unreadable
    # process holding `--resume <session>` yields zero hits and the message
    # below would say "no process evidence" about a process helm simply could
    # not read. It also subsumes the old per-winner check (`if p["pid"] in
    # unreadable`), which the whole-object predicate made unreachable: a pid
    # can no longer be a row AND unreadable at once.
    blind = cannot_look(unreadable, "no pane holding %s… can be ruled out"
                                    % session[:8])
    if blind:
        return None, blind
    hits = [p for p in procs if p.get("resume_sid") == session]
    if not hits:
        return None, ("no live claude argv carries --resume %s… — a pane "
                      "launched fresh leaves no argv trace, so this is 'no "
                      "process evidence', not 'no pane exists'" % session[:8])
    if len(hits) > 1:
        return None, ("%d live claude processes all carry --resume %s… (pid "
                      "%s) — a resume must never guess which one is the pane"
                      % (len(hits), session[:8],
                         ", ".join(str(p["pid"]) for p in hits)))
    p = hits[0]
    if p.get("seat"):
        # The hook inherits its pane's environ, so a hook with NO name whose
        # sid-holder HAS one is not looking at its own pane — it is looking at
        # another seat double-opened on this session. Refuse loudly.
        return None, ("pid %d holds --resume %s… but declares "
                      "HELM_CHAT_NAME=%s — a nameless session must not inject "
                      "into a named seat's pane" % (p["pid"], session[:8],
                                                    p["seat"]))
    if not p.get("pane_key"):
        return None, ("pid %d holds --resume %s… but carries no "
                      "ORCA_PANE_KEY — helm has no address to inject into "
                      "(metaharness: %s)"
                      % (p["pid"], session[:8], _detected_host()))
    return ident_of(p), ("pid %d, argv --resume %s… (nameless pane, resolved "
                         "from this session's own sid)"
                         % (p["pid"], session[:8]))


# ---------------------------------------------------------------------------
# the ONE addressing primitive — no send in this module obtains a handle twice
# ---------------------------------------------------------------------------
#
# WHY A CHOKE POINT RATHER THAN TWO CAREFUL CALL SITES. Six wrong-pane defects
# have been found in this file across two cross-family review rounds — four at
# a032ce7's parent, two at a032ce7 itself — and every one of them was the SAME
# SHAPE wearing a different hat: a pane was selected on evidence that was
# STALE, HISTORICAL or AMBIGUOUS, and when that evidence thinned the code FELL
# THROUGH to a neighbouring pane instead of refusing. They kept recurring
# because the two addressing paths here each re-implemented survival checking,
# candidate counting and handle binding, so hardening one left the other whole.
# Closing the two newest instances would have bought a third round; this closes
# the SHAPE.
#
# FOUR RULES, and together they are what makes the unsafe form unrepresentable:
#
#   1. A SEND NEEDS AN AUTHORIZED PID. No decided process means no address —
#      never "the first pane that answers to this name".
#   2. THE OBSERVATION IS TAKEN HERE, at send time. This is the rung that kills
#      the TOCTOU hole, and the distinction is exact: RE-FILTERING the caller's
#      snapshot cannot close it, because a snapshot re-filtered is still stale
#      by construction. Only a fresh look can see the process exit.
#   3. THE HANDLE BINDS TO THE AUTHORIZED PID'S OWN pane_key. Nothing indexes
#      position [0] of a set whose membership can change underneath it.
#   4. UNKNOWN NEVER AUTHORIZES A SEND. If that fresh observation could not
#      read every claude on the host, it cannot rule out that one of them holds
#      the pane this directive is addressed to. Rule 2 says the send-time look
#      is the authoritative one; this says an authoritative look with a hole in
#      it authorizes nothing. Round 7 (dispatch 90845108) is the round
#      that had to add it, and it is the rung that covers ladders not yet
#      written — every adopted send comes through here.
#
# THE FAILURE DIRECTION IS FIXED: refuse and say why. An unreachable pane fails
# loudly and costs one retry; a mis-reached pane types into another agent's
# live session and reports success.


def _host_label(adapter):
    """The metaharness an adopted-pane refusal met, by the adapter's own name,
    or the no-host case `harness.detect` answers with None."""
    name = getattr(adapter, "name", None)
    if name:
        return str(name)
    return ("none detected (HELM_METAHARNESS=none, or no orca or herdr CLI "
            "on PATH)" if adapter is None else "unnamed adapter")


def _detected_host():
    """`_host_label` of the metaharness `harness.detect` would pick — an env
    read and a PATH lookup, no RPC, so an identity rung that must not probe a
    metaharness can still name the host its refusal met."""
    try:
        from . import harness
        return _host_label(harness.detect())
    except Exception as e:                          # noqa: BLE001 — prose only
        return "unknown (detection failed: %s)" % e.__class__.__name__


def _pane_of(proc, adapter):
    """(handle, reason) — the handle of THIS process's OWN pane. Never a list.

    A caller cannot index the wrong element of a one-element answer, which is
    the whole reason this returns a handle rather than candidates.
    """
    pid = proc["pid"]
    # THE REFUSAL NAMES THE HOST (task/3055, an owner requirement). An adopted
    # pane is addressed through the metaharness's own pane resolver, and only
    # an adapter that exposes one can turn a pane key into a handle. Under any
    # other host the answer is a refusal, and a refusal that does not say
    # WHICH host it met reads as a broken seat rather than a missing seam.
    host = _host_label(adapter)
    if not proc.get("pane_key"):
        return None, ("pid %d carries no ORCA_PANE_KEY — helm has no address "
                      "to inject into (metaharness: %s)" % (pid, host))
    resolver = getattr(adapter, "resolve_pane", None)
    if resolver is None:
        return None, ("no live pane resolves for pid %d — the %s metaharness "
                      "exposes no pane resolver, so its ORCA_PANE_KEY cannot "
                      "become a handle; refusing to inject into another"
                      % (pid, host))
    try:
        pane = resolver(proc["pane_key"]) or {}
    except Exception as e:                          # noqa: BLE001 — fail-open
        return None, "pane resolve failed for pid %d: %s" % (pid, e)
    if not pane.get("handle"):
        return None, ("no live pane resolves for pid %d — the process helm "
                      "decided about has no reachable pane; refusing to "
                      "inject into another" % pid)
    return pane["handle"], None


def authorized_handle(expect_pids, adapter):
    """(handle, proof) | (None, reason) — the ONE way to turn an addressing
    decision into a handle a send may use.

    `expect_pids` is the set of PROCESS IDENTITIES the caller's ladder
    authorized (`ProcIdent`: pid plus birth stamp). It is the whole input that
    matters — no name, no prior handle, no candidate list. This primitive never
    re-derives a holder from a seat NAME, because the name is exactly the
    ambiguous part (a seat name is inherited by every child it spawns).

    IDENTITY, NOT NUMBER. The live index is keyed on (pid, birth stamp), so
    "the authorized process exited" and "its pid was recycled by a stranger"
    are the SAME miss and take the same exit — a refusal. The earlier version
    keyed on the number alone and therefore could not tell a survivor from a
    successor; a cross-family read found that, and it is why nothing below
    compares a pid to a pid.

    AN UNSTAMPED AUTHORIZATION IS NOT ONE. A caller that hands over a bare int
    is naming a slot, and a slot cannot be re-proven. That refuses too, loudly,
    rather than degrading to the pid-only comparison this exists to end.
    """
    want = list(expect_pids or ())
    if not want:
        return None, ("this send authorized no pid, so there is no process to "
                      "bind a pane to — refusing rather than injecting into "
                      "the first pane that answers")
    blank = sorted({int(t) for t in want if getattr(t, "start", None) is None})
    if blank:
        return None, ("pid %s was authorized without a process birth stamp, so "
                      "a recycled pid could not be told from the process this "
                      "send decided about — a pid is a slot the kernel reuses, "
                      "not an identity; refusing rather than comparing numbers"
                      % ", ".join(str(x) for x in blank))
    # RULE 2 — the observation is TAKEN HERE. Anything the caller measured
    # earlier is, by the time the send happens, a claim about the past.
    procs, unreadable = claude_processes()
    # RULE 4 — UNKNOWN NEVER AUTHORIZES A SEND, and it lives HERE because this
    # is the ONE function every adopted send routes through (enforced by
    # `SendSiteCensusTest`). The ladders above hold the same rung, and this is
    # not redundancy: a ladder gates the DECISION against the census it decided
    # on, and this gates the SEND against the census taken at send time. The
    # settle delay between them is exactly long enough for a claude process to
    # become unreadable — and if it did, helm can no longer say the pane it is
    # about to type into is the one it decided about. A future identity ladder
    # written without rung 0 is covered by this one for free.
    blind = cannot_look(unreadable,
                        "the send-time census cannot rule out that one of them "
                        "holds the pane this directive is addressed to")
    if blind:
        return None, blind
    live = {ident_key(p): p for p in procs}
    slots = {p["pid"] for p in procs}
    keys = sorted({ident_key(t) for t in want})
    gone = [k for k in keys if k not in live]
    if gone:
        # A SHRINKING CANDIDATE SET IS A REFUSAL, NOT A NARROWING. Reproduced
        # at a032ce7 (dispatch 1b4039cc): the expected pid exited
        # between the caller's snapshot and this one, the surviving neighbour
        # became the only candidate, and the stale first handle was sent to it
        # — returning "resumed" into another agent's pane.
        #
        # A RECYCLED PID IS THE SAME MISS WEARING THE VICTIM'S NUMBER, and it
        # gets its own sentence because the two are indistinguishable to an
        # operator otherwise: "pid 101 is alive" is true and irrelevant.
        now = {p["pid"]: p.get("start") for p in procs}
        recycled = [k for k in gone if k[0] in slots]
        if recycled:
            return None, ("%s alive but is a DIFFERENT process from the one "
                          "this send was authorized for — a pid is a slot the "
                          "kernel recycles, so the authorized process is GONE "
                          "and its successor must never inherit a directive "
                          "addressed to it"
                          % "; ".join(
                              "pid %d was authorized at birth stamp %s and the "
                              "stamp is now %s, so it is"
                              % (pid, stamp, now.get(pid))
                              for pid, stamp in recycled))
        return None, ("pid %s exited between the identity decision and the "
                      "send (live claude processes now: %s) — a candidate that "
                      "exits must ABORT the send; a neighbouring pane must "
                      "never inherit a directive addressed to it"
                      % (", ".join(str(k[0]) for k in gone),
                         ", ".join(str(x) for x in sorted(slots)) or "none"))
    holders = [live[k] for k in keys if live[k].get("pane_key")]
    if not holders:
        return None, ("no authorized process (pid %s) carries an "
                      "ORCA_PANE_KEY — helm has no address to inject into "
                      "(metaharness: %s)"
                      % (", ".join(str(k[0]) for k in keys),
                         _host_label(adapter)))
    if len(holders) > 1:
        return None, ("%d authorized processes each carry a pane (pid %s) — a "
                      "send must never guess which one holds the session"
                      % (len(holders),
                         ", ".join(str(p["pid"]) for p in holders)))
    proc = holders[0]
    # THE HANDLE IS ALWAYS RE-RESOLVED, and the optimisation that used to skip
    # this is DELETED rather than repaired. It reused a handle published by an
    # EARLIER observation whenever that publisher's pid equalled this one's —
    # and pid equality across two observations is precisely the comparison
    # finding 1 disproved. Re-resolving costs one `resolvePane` call and
    # removes the only place a stale handle could still be spent, along with
    # the `seat` parameter whose sole use was deciding whether to trust one.
    handle, why = _pane_of(proc, adapter)
    if handle is None:
        return None, why
    return handle, ("pid %d (birth stamp %s), handle re-resolved from its own "
                    "ORCA_PANE_KEY at send time"
                    % (proc["pid"], proc.get("start")))


def send_to_sid_pane(session, text, expect_pids=None, adapter=None,
                     on_typed=None, on_submit=None, admit=None):
    """(mode, detail) — inject `text` into the NAMELESS pane holding `session`.

    The nameless twin of `send_to_pane`, and deliberately NOT routed through
    it: `send_to_pane` addresses by seat NAME (resolve() + the roster), and a
    nameless delivery must never let a name — least of all one reverse-looked-
    up from the roster, which any process can write — pick the injection
    target. The pane is re-proven from the same evidence the hook decided on:
    this session's own sid in a live argv, unique, with an address.

    `expect_pids` is the TOCTOU guard, same doctrine as `send_to_pane`: the
    settle delay is a window in which the pane can be relaunched, and a
    relaunched pane resumed on the SAME sid re-resolves cleanly — to a
    DIFFERENT pid. That pane is a different agent; a directive addressed to
    its predecessor must not reach it.

    THE COMPARISON IS ON IDENTITY, NOT ON THE NUMBER. A relaunch that lands on
    the SAME pid is the harder half of the same case, and it is not exotic: a
    settle delay is exactly long enough for the kernel to hand the number back.
    Comparing `pid in expect_pids` would wave that through, so the whole birth
    identity is compared — the same law `authorized_handle` enforces one layer
    down, applied here because this leg re-derives its own holder.
    """
    from . import harness
    if adapter is None:
        adapter = harness.detect()
    if adapter is None:
        return "manual", harness.RECOMMENDATION
    procs, unreadable = claude_processes()
    pid, why = _nameless_identity(session, procs, unreadable or [])
    if pid is None:
        return "manual", why
    if expect_pids and ident_key(pid) not in {ident_key(x) for x in expect_pids}:
        return "manual", ("the pane holding %s… is now %s, not the %s the hook "
                          "decided about (relaunched during the settle) — a "
                          "directive addressed to its predecessor must not be "
                          "injected" %
                          (session[:8], ident_token(pid),
                           ", ".join(ident_token(x)
                                     for x in sorted(expect_pids))))
    # THE SAME PRIMITIVE AS THE NAMED TWIN, and this is the MODE that makes the
    # two paths one. What legitimately differs between them is the EVIDENCE the
    # ladder above accepts — this one keys on the session's own sid and must
    # never let a roster-looked-up name pick a target. What must NOT differ is
    # the refusal contract, the send-time re-proof and the handle binding, and
    # those three were exactly what each path used to re-implement.
    handle, why = authorized_handle([pid], adapter)
    if handle is None:
        return "manual", why
    from . import resumeturn
    typed = ((lambda h, placed: on_typed(h, placed, adapter))
             if on_typed is not None else None)
    # THE AUTHORIZATION RIDES TO THE ADAPTER'S ACT DOORS. `submit` is where
    # the placement and the Enter are each prepared, proven and validated, so
    # a caller's `admit` that stopped at this function boundary would leave
    # both keystrokes unauthorized. Passed only when given, so an adapter
    # double with the older signature is untouched.
    state, proof = adapter.submit(handle, text, on_typed=typed,
                                  **({"admit": admit} if admit is not None
                                     else {}))
    if on_submit is not None:
        on_submit(adapter, handle, state, proof)
    return resumeturn._mode_for(
        state, proof, detail="nameless pane %s (%s)" % (handle, why))


def send_to_pane(seat, text, expect_pids=None, adapter=None,
                 on_typed=None, on_submit=None, operation=None, admit=None):
    """(mode, detail) — inject `text` into an adopted seat's LIVE pane.

    `expect_pids` is the TOCTOU guard. A helm-spawned seat gets this from the
    per-seat lifecycle lock plus a re-read of the spawn register; an adopted
    seat has neither, so the equivalent proof is that the process the hook
    DECIDED ABOUT is still alive and still holds the seat. A pane relaunched
    during the settle delay is a different agent, and injecting a directive
    addressed to its predecessor is the mistake this refuses.

    SURVIVAL, NOT SET EQUALITY. The expected pids must still be PRESENT, not be
    the whole set: a seat name is inherited by everything the seat spawns, so a
    subagent starting or finishing during the settle changes the set without
    changing the pane. Demanding equality would refuse the busiest seats, which
    is the same mistake `turn_restart_identity` documents one rung earlier.

    THIS FUNCTION NO LONGER PICKS A PANE. It runs the seat-name identity ladder
    (`resolve` -> `_session_joined`) and then hands the decision to
    `authorized_handle`, which owns the send-time observation and the binding.
    The split matters: the two wrong-pane defects found at a032ce7 were both in
    the selection half, and both survived review because the selection logic
    LOOKED careful — a candidate census, an ambiguity count, a second /proc
    scan. What it could not do was bind the handle to the pid it was authorized
    to address, so every time the evidence thinned it fell through to whatever
    was left. There is nothing here to get right any more.
    """
    from . import harness
    if adapter is None:
        adapter = harness.detect()
    info = resolve(seat, adapter=adapter)
    if info is None:
        return "manual", "helm has no record of seat %s" % seat
    now = set(info.get("pids") or [])
    gone = sorted(set(expect_pids or []) - now)
    if gone:
        # A DECLINED JOIN IS NOT AN EXIT. When `_session_joined` refuses, the
        # declined pids are absent from `now` — and reporting them as "exited"
        # states something false about a process that is very much alive, which
        # is the same could-not-tell/here-is-the-answer conflation one layer
        # down. The refusal already says what was actually seen.
        if info.get("session_refused"):
            return "manual", info["session_refused"]
        return "manual", ("the process holding %s is gone (pid %s exited "
                          "between the compaction and the send; live now: %s)"
                          % (seat, ", ".join(str(x) for x in gone),
                             ", ".join(str(x) for x in sorted(now)) or "none"))
    handle, proof = authorized_handle(expect_pids, adapter)
    if handle is None:
        # THE PRIMITIVE OWNS THE ADDRESS. What used to stand here was this
        # function's OWN candidate census, recomputed from the env label while
        # `resolve()` had folded in a session-joined row, and then re-filtered
        # from a SECOND /proc snapshot. Both halves failed in the same
        # direction: when the set thinned, the guard stopped firing and the
        # stale first handle went out. A census cannot be made safe by taking
        # it more carefully — the handle has to be BOUND to the authorized
        # process, which is what `authorized_handle` returns and this function
        # no longer decides. It used to hand `resolve()`'s handle down as a
        # `prior`; that handle is now a REPORT and never an authorization, so
        # there is one refusal path here instead of two near-identical ones.
        #
        # A SESSION REFUSAL STILL OUTRANKS THE ADDRESS FAILURE in the message:
        # "no pane answers this seat" reads like a dead seat, while "the
        # session evidence contradicted itself" is a LIVE pane helm declined to
        # type into, and an operator must be able to tell those apart.
        return "manual", (info.get("session_refused") or proof)
    from . import resumeturn
    typed = ((lambda h, placed: on_typed(h, placed, adapter))
             if on_typed is not None else None)
    # THE ACTUATION MAY BE REPLACED; THE AUTHORIZATION ABOVE MAY NOT. `handle`
    # is the one `authorized_handle` bound to a stamped live process, and an
    # alternate operation receives exactly that handle and nothing else — so a
    # caller with a different ACT (the vendor modal's re-derived choice) cannot
    # acquire a different PANE.
    if operation is not None:
        state, proof = operation(adapter, handle, typed)
    else:
        # THE AUTHORIZATION RIDES TO THE ADAPTER'S ACT DOORS (see the
        # nameless twin above).
        state, proof = adapter.submit(handle, text, on_typed=typed,
                                      **({"admit": admit} if admit is not None
                                         else {}))
    if on_submit is not None:
        on_submit(adapter, handle, state, proof)
    return resumeturn._mode_for(state, proof,
                                detail="orca-adopted pane %s" % handle)


# ---------------------------------------------------------------------------
# resume — guarded, from the transcript (the durable source)
# ---------------------------------------------------------------------------

def newest_session_row(seat, sids=None):
    """(row, reason) — the catalog row for the newest session this seat held.

    The TRANSCRIPT is the durable source: closing an orca pane SIGKILLs the
    agent and orca deletes its own resume record, so a resume is always a fresh
    process re-reading the transcript, never a reattach.
    """
    if sids is None:
        sids, failed = roster_sessions(seat)
        if failed:
            return None, "helm's chat roster could not be read"
    if not sids:
        return None, "no session id is recorded for %s" % seat
    from . import sessions
    known = {r["i"]: r for r in sessions.rows_for(include_synthetic=True)}
    hits = [known[s] for s in sids if s in known]
    if not hits:
        return None, ("none of %s's %d recorded session id%s has a transcript "
                      "helm can see" % (seat, len(sids),
                                        "" if len(sids) == 1 else "s"))
    return max(hits, key=lambda r: r.get("mt") or 0), None


def resume(seat, adapter=None, force=False, note=None, title=None,
           skip_permissions=False, session=None):
    """(rc, lines) — relaunch an orca-adopted seat's pane from its transcript.

    `session` PINS the transcript: the post-reboot sweep classified one
    session for this seat and resumes that one or nothing — never the
    newest other one (a FIX on the managed-seat pin: the adopted
    branch had dropped it). Without the pin the newest recorded session
    is chosen, the hand verb's historical behaviour.

    The guard runs FIRST and REFUSES on LIVE or UNKNOWN: a second pane on one
    session interleaves both panes' writes into the same transcript and each
    silently loses turns. `force` overrides, and is meant only for a pane the
    operator has personally confirmed is a zombie.

    The relaunch reuses `sessions.spawn_resume` (mint script -> adapter spawn)
    and `kick_resumed` rather than reimplementing a second launcher, and carries
    HELM_CHAT_NAME so the pane comes back AS the seat instead of as an anonymous
    pane that no longer answers to its name.
    """
    lines = []
    state, evidence = seat_liveness(seat)
    if state != DEAD and not force:
        lines.append("helm seat: refusing to resume %s — %s: %s"
                     % (seat, state, evidence))
        lines.append("  A second pane on one session interleaves both panes' "
                     "writes into the same transcript and each loses turns.")
        lines.append("  Switch to that pane, or pass --force if you have "
                     "confirmed it is a zombie.")
        return 1, lines
    if state != DEAD:
        lines.append("  --force: proceeding despite %s (%s)" % (state, evidence))
    row, reason = newest_session_row(seat, sids=[session] if session else None)
    if row is None:
        lines.append("helm seat: cannot resume %s — %s%s"
                     % (seat, reason,
                        " (the pinned session %s; a reboot relaunch resumes "
                        "THAT session or none)" % str(session)[:8]
                        if session else ""))
        return 1, lines
    from . import harness, sessions
    blocking = [w for w in sessions.resume_warnings(row)
                if w.startswith(("OVERSIZED", "REFERENCE"))]
    if blocking:
        for w in blocking:
            lines.append("helm seat: refusing to resume %s — %s" % (seat, w))
        return 1, lines
    home = sessions.credhome_for(row["i"]) if row.get("h") == "claude" else None
    if adapter is None:
        adapter = harness.detect()
    if adapter is None:
        lines.append("helm seat: " + harness.RECOMMENDATION)
        lines.append("  manual paste: "
                     + sessions.resume_command(row, home=home))
        return 1, lines
    try:
        path, handle, name = sessions.spawn_resume(
            row, title=title or seat, home=home,
            skip_permissions=skip_permissions,
            env={"HELM_CHAT_NAME": seat})
    except harness.HarnessError as e:
        lines.append("helm seat: %s resume via %s failed: %s"
                     % (seat, getattr(adapter, "name", "?"), e))
        lines.append("  manual paste: "
                     + sessions.resume_command(row, home=home))
        return 1, lines
    lines.append("helm seat: resumed %s (orca-adopted) via %s — pane %s, "
                 "session %s…" % (seat, name, handle, row["i"][:8]))
    kicked = sessions.kick_resumed(adapter, handle, note=note)
    lines.append("  kick: %s" % ("delivered — the seat has its resume brief "
                                 "(beacon re-arm rides it)"
                                 if kicked else
                                 "FAILED — the pane is up but DEAF: its "
                                 "beacon died with the old process and the "
                                 "kick was the turn it would re-arm on. "
                                 "Speak to it by hand; this resume is NOT "
                                 "complete (row #153)"))
    lines.append("  script: %s" % path)
    lines.append("  cwd   : %s" % (row.get("cwd") or "?"))
    # A restart that cannot restore the wake path must refuse to report
    # itself complete: a deaf seat reads exactly like an idle one from
    # outside, which is how a restored fleet sat unreachable for hours.
    return (0 if kicked else 1), lines


# ---------------------------------------------------------------------------
# the slice-0 coverage gap — a per-seat home for an ADOPTED pane
# ---------------------------------------------------------------------------

def adopt_home(seat, repo_root, base="main", adapter=None):
    """(path, detail) — give an orca-adopted seat the SAME private checkout a
    helm-spawned seat gets: `<repo>-wt/seats/<seat>`.

    This is the half slice 0 could not reach. It provisions through the very
    same `ensure_home_worktree` seam (so the path is identical whichever way a
    seat arrived), then reports the orca-visibility outcome instead of
    swallowing it — for helm that step currently fails for a NAMED reason the
    operator needs to see (see harness.OrcaAdapter.adopt_worktree).

    The checkout is the contract; board visibility is a nicety. This never
    touches a running seat: it creates a directory and returns. Moving a live
    seat into it is a staggered reseed the integrator runs.
    """
    from . import harness
    if adapter is None:
        adapter = harness.detect()
    ensure = getattr(adapter, "ensure_home_worktree", None) if adapter else None
    path = (ensure(seat, repo_root, base=base) if ensure
            else harness.ensure_home_worktree(seat, repo_root, base=base))
    adopt = getattr(adapter, "adopt_worktree", None) if adapter else None
    if adopt is None:
        return path, "no orca adoption seam on this metaharness (checkout is ready)"
    ok, detail = adopt(repo_root, path)
    return path, ("orca: " + detail) if ok else ("orca DID NOT adopt it — " + detail)
