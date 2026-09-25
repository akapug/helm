"""helm hooks run <EVENT> — ONE interpreter spawn per hook event.

WHY THIS EXISTS (task/630). Every tool call this fleet makes paid one full
interpreter spawn PER HOOK, not per event. PostToolUse alone ran `helm record`
and `helm chat deliver` as two separate processes on all 11851 tool calls in a
measured day. The floor for a helm invocation is ~47ms here (~28ms bare python3
spawn + ~20ms `import helm.cli`) before a handler does any work at all, so the
second spawn bought nothing but the spawn. Merging N handlers into one process
recovers (N-1) floors per event, for every seat on this box — the hooks are
installed fleet-wide, so this is not a helm-only saving.

THE HANDLERS ARE NOT REIMPLEMENTED HERE. Each one is invoked through
`cli.main(argv)` — helm's own verb dispatcher, the exact entry the standalone
hook used. That is deliberate and it is the whole zero-behaviour-change
argument: this module changes WHERE a handler is called from, never WHAT runs.
A handler invoked directly on the command line still behaves identically,
because nothing about the handler moved.

THE THREE PROPERTIES THAT MAKE THIS DANGEROUS, and what holds each:

1. GUARDS MUST KEEP THEIR TEETH. hooks.spec_command's own contract is exact:
   "A gate propagates ONLY rc 2 and swallows the rest." So this dispatcher
   exits 2 if and only if a spec carrying `gate: True` returned 2. A NON-gate
   handler returning 2 must NOT block — today its `|| true` swallows it, and a
   dispatcher that took the max rc across all handlers would silently arm
   every advisory hook into a blocker. `_stronger` is written for that trap,
   not for the obvious one.

2. ONE SLOW HANDLER MUST NOT EAT ANOTHER'S BUDGET. Per-handler timeouts are
   load-bearing (2s argv-guard, 3s suite guard, 5s record and stop-guard, 10s
   inject and cred heal) and they used to be enforced by a separate `timeout`
   per process. In one process they are enforced by SIGALRM around each
   handler, armed and disarmed per call, so a handler that hangs is cut at ITS
   OWN budget and its siblings still run on theirs. PEP 475 auto-retries
   EINTR, so an alarm only breaks a blocking syscall because our handler
   RAISES — that is why `_Timeout` is raised from the signal handler rather
   than setting a flag.

3. ONE HANDLER RAISING MUST NOT SILENCE THE OTHERS. This is the composition
   failure the fleet was paying four processes to avoid: today the four fail
   independently, and in one process an exception in the first would skip the
   rest. Every handler is therefore wrapped, BaseException included, and a
   failure is recorded and stepped over rather than propagated.

FAIL-OPEN IS PRESERVED, INCLUDING ITS NOISE. A timeout or a crash allows the
event, exactly as the shell ladder does today — a guard that can wedge every
seat's turn end is worse than one that misses a row. But rc 124 was once the
silently-swallowed code, and the fix for that (a guard that never ran must not
look like a guard that ran and found nothing) is reproduced here: both paths
SAY so on stderr, in the ladder's own words.

NOT EVERY SPEC CAN COME HOME. An `external` spec is a binary helm does not
ship (fab-suite-pretooluse), so it cannot be called in-process at all and
keeps its own hook entry. `event_specs` refuses it rather than pretending.
"""

import contextlib
import io
import os
import signal
import sys


class _Timeout(BaseException):
    """Dispatcher cancellation, not an ordinary handler error. Bypass handlers'
    broad Exception catches so run_one can report the timeout as UNCHECKED.
    PEP 475 retries EINTR unless the signal handler raises, so a flag would let
    a hung syscall keep hanging."""
    _hook_latency_timeout = True
    #: Where the budget expired — see `_alarm`. Read duck-typed by
    #: hooklatency so the observer never imports the dispatcher.
    where = None


#: How many frames of the interrupted stack a timeout carries. Enough to name
#: the call that was running and who asked for it; short enough that building
#: it inside a signal handler stays a few string operations.
_WHERE_FRAMES = 8


def _where(frame):
    """The innermost frames as `file:line:function`, outermost last.

    THE SIGNAL HANDLER IS HANDED THE ANSWER. SIGALRM interrupts whatever was
    executing and passes that exact frame, so the question "what was this span
    blocked IN" is answered at the moment the budget expires. A span boundary
    can only say WHICH STAGE was innermost, which is a coarser question — and
    one a per-stage tally answers misleadingly, because one timed-out hook
    ends every ancestor span in timeout and so reads as several problems.

    DELIBERATELY CHEAP AND ALLOCATION-LIGHT, because it runs in a signal
    handler: basenames rather than paths, no linecache and therefore no file
    I/O, a bounded walk, and every failure swallowed — a diagnostic that can
    raise inside a handler would convert a timeout into a crash."""
    out = []
    try:
        while frame is not None and len(out) < _WHERE_FRAMES:
            code = frame.f_code
            out.append("%s:%d:%s" % (code.co_filename.rpartition("/")[2],
                                     frame.f_lineno, code.co_name))
            frame = frame.f_back
    except Exception:                        # noqa: BLE001 — see docstring
        pass
    return " < ".join(out) or None


def _alarm(_sig, _frm):
    exc = _Timeout()
    exc.where = _where(_frm)
    raise exc


@contextlib.contextmanager
def deadline_held():
    """Defer the handler deadline across a run that must not be split.

    `_alarm` raises wherever the interpreter is. A handler whose effect is
    commit-then-publish -- the delivery hook moves a cursor, then writes the
    row -- lost the row when the deadline landed between the two: consumed,
    never shown, and the beacon's cursor moved with it. Held here, an expiry
    is recorded and re-raised through the prior handler as the run ends, so
    the effect is whole or absent. The run must be bounded I/O, never a wait.

    THE HANDLER IS SWAPPED, NOT THE SIGNAL MASK. A mask binds one thread,
    SIGALRM is process-directed, and Python runs its handler on the main
    thread whichever thread the kernel picked, so a mask held by the main
    thread alone does not hold the handler."""
    fired, held = [], False
    alrm = getattr(signal, "SIGALRM", None)
    prior = signal.getsignal(alrm) if alrm is not None else None
    if prior is not None:               # None: installed outside Python, not ours
        try:
            signal.signal(alrm, lambda _sig, frm: fired.append(frm))
            held = True
        except ValueError:              # not the main thread: no deadline here
            pass
    try:
        yield
    finally:
        if held:
            signal.signal(alrm, prior)
            if fired and callable(prior):
                prior(alrm, fired[0])
            elif fired and prior == signal.SIG_DFL:
                signal.raise_signal(alrm)


# Registry families that failed to import on the LAST dispatch_specs() call, as
# (module, attr, exception). Exists so a caller — and an arm — can assert on the
# LOSS directly instead of scraping stderr for a phrase.
_AUTH_MISSING = []


def dispatch_specs():
    """EVERY helm hook spec on this box, composed from the modules that OWN them.

    THIS REGISTRY IS WHY THE TAX WAS INVISIBLE. Three modules declare hooks and
    nothing ever put them in one list: hooks.SPECS, cred's GUARD_SPECS
    (now empty: per-turn backup/heal retired), and record's HOOK_SPECS (every
    PostToolUse and PostToolUseFailure — the highest-traffic hook in the fleet).
    They share an INSTALLER (`hooks.install_home(specs=...)`) but never a view,
    so no surface could answer "what actually fires on PostToolUse?" — and the
    answer was two full interpreter spawns on all 11851 tool calls in a day.

    OWNERSHIP DOES NOT MOVE. Each module still declares and installs its own
    hooks; this only READS their declarations. The imports are LAZY and inside
    the function on purpose: hoisting them to module top would add cred's and
    record's import cost to every `helm` invocation, which is the exact tax
    this module exists to remove.

    ORDER IS THE CONTRACT — hooks, then cred, then record. The credential
    family remains an import authority even though its active tuple is empty;
    retired declarations never enter the dispatch registry.
    """
    from . import hooks
    del _AUTH_MISSING[:]
    out = list(hooks.SPECS)
    for mod, attr in (("cred._common", "GUARD_SPECS"), ("record", "HOOK_SPECS")):
        try:
            m = __import__("helm." + mod, fromlist=[attr])
            out.extend(getattr(m, attr))
        except Exception as e:                    # noqa: BLE001
            # AN IMPORT FAILURE IS NOT AN ABSENCE, AND IT MUST NEVER BE SILENT.
            # This block used to `pass` with the comment "a missing optional
            # module must never wedge a hook". Two things were wrong with that.
            # First, NEITHER module is optional — cred and record both ship in
            # this package, so any failure here is a real failure, never a
            # legitimate absence. Second and worse: swallowing it silently
            # ERASED A LIVE HANDLER. Before the merge, record ran as its own
            # process, so a broken record failed LOUDLY on every tool call
            # while deliver carried on. After the merge, the same breakage
            # would have made PostToolUse quietly run deliver alone with an
            # empty stderr — the recorder gone, and nothing anywhere saying so.
            # Found by a probe that forced the relative record import to raise
            # and measured event_specs("PostToolUse") == ["deliver"] with
            # stderr == "".
            #
            # FAIL-OPEN IS KEPT — the surviving handlers still run, because a
            # registry that wedges every tool call is worse than one missing a
            # family. What changes is that the loss is ANNOUNCED, in the same
            # register this module uses for a timed-out or crashed handler:
            # allowed, and never in silence.
            _AUTH_MISSING.append((mod, attr, e))
            sys.stderr.write(
                "[helm hookrun] REGISTRY FAMILY MISSING: helm.%s.%s failed to "
                "import (%s: %s) — every hook it declares is NOT RUNNING this "
                "event, and this event is proceeding without it\n"
                % (mod, attr, e.__class__.__name__, e))
    return tuple(out)


# THE ORDER THAT ACTUALLY RUNS TODAY, pinned rather than inherited.
#
# Composing three modules' tables gives DECLARATION order, and declaration
# order is NOT what fires: the live settings.json was built by three separate
# installers appending at different times, so its real order is an artifact of
# install sequence. Measured against the live file, two events differ from
# naive composition — PostToolUse runs record BEFORE deliver, and SessionStart
# originally ran both cred guards BETWEEN join and resume-turn (now retired).
# Shipping declaration order would have silently reordered the highest-traffic hook
# while claiming zero behaviour change.
#
# So the order is written down. Names not listed keep declaration order, after
# the listed ones. `tests` pins this against a real installed settings file,
# because a hand-maintained order that nothing checks is the next stale artifact.
_EVENT_ORDER = {
    "PostToolUse": ("record", "deliver"),
    "SessionStart": ("join", "resume-turn"),
    "Stop": ("stop-guard",),
}


def _ordered(event, specs):
    """`specs` in the order they fire on this box today."""
    pin = _EVENT_ORDER.get(event)
    if not pin:
        return list(specs)
    rank = {n: i for i, n in enumerate(pin)}
    # stable: listed names in pinned order first, everything else after, each
    # keeping its declaration position.
    return sorted(specs, key=lambda s: (rank.get(s.get("name"), len(rank)),))


def event_specs(event, tool_name=None, specs=None):
    """The in-process handlers for `event`, in SPECS order.

    ORDER IS THE CONTRACT: handlers run in the same order the four separate
    hook entries ran, because some write state a later one reads.

    EXTERNAL specs are excluded — helm does not ship their executable, so they
    stay as their own hook entry and this returns what CAN be merged, never a
    claim to have merged everything.
    """
    out = []
    for s in (specs if specs is not None else dispatch_specs()):
        if s.get("event") != event:
            continue
        if s.get("external"):
            continue
        m = s.get("matcher")
        # A matcher of None or "*" applies to every tool; a named matcher (e.g.
        # "Bash") applies only to that tool. When the caller does not know the
        # tool, a NAMED matcher is skipped rather than guessed — running a
        # Bash-only guard against an unknown tool would be a new behaviour.
        # A named matcher may list tools the way Claude Code reads it
        # ("Bash|Monitor"), each an exact name: string equality would silently
        # stop the guard for every tool it lists. The whole matcher still
        # resolves too, because settings_block asks with the matcher in hand.
        if m not in (None, "*"):
            if tool_name is None or tool_name not in (m, *m.split("|")):
                continue
        out.append(s)
    return tuple(_ordered(event, out))


def _stronger(worst, rc, spec):
    """Fold one handler's rc into the event's exit code.

    ONLY a gate's rc 2 escapes. Everything else — a non-gate's 2, any 1, any
    127 — is swallowed, which is precisely what `|| true` and the gate ladder
    do today. Widening this is how every advisory hook silently becomes a
    blocker, so the gate check is inside the function rather than at the call
    site where a later edit could drop it.
    """
    if spec.get("gate") and rc == 2:
        return 2
    return worst



#: WHAT A HANDLER'S SLOT IN A DISPATCH ACTUALLY SAYS. Three values, because
#: the two ways of not answering are different facts: UNCHECKED means the
#: handler was asked and did not answer (timeout, crash, an unparseable exit
#: code), SKIPPED means it never ran at all. Collapsing them into one boolean
#: loses the only thing that distinguishes "the guard is broken" from "the
#: guard was not consulted", and a corroborating record must decline on both.
from .hookoutcome import ANSWERED, SKIPPED, UNCHECKED    # noqa: F401

#: The rate limit on the timeout line below — a two-import leaf (os, time), so
#: it costs this hot path nothing to name here. It is shared with the POSIX-sh
#: `124)` arm that speaks when this process has been killed outright.
from . import hookalarm


def _count_refusal(spec, payload):
    """Append one line to the friction ledger for a refusal that escapes.

    ONLY THE REFUSAL PATH PAYS. The ledger module is imported here and nowhere
    above, so a dispatch in which nothing refused never loads it. NEVER
    raises: the rc this event returns was decided before this ran, and a
    bookkeeping failure must not reach the loop that still owes the remaining
    handlers their turn."""
    try:
        from . import friction
        friction.record_gate(spec.get("name"), payload)
    except BaseException:                         # noqa: BLE001 — see docstring
        return


def _stamp_stop_allowed(payload):
    """Record that helm's Stop dispatch ALLOWED, or nothing. NEVER raises.

    NOT A COMPLETION, AND THE NAME SAYS SO. This records that every helm Stop
    handler answered and none vetoed. A foreign plugin Stop hook outside this
    dispatcher can still veto afterwards, so the harness may never accept the
    stop at all; `helm/turnstamp.py` carries the full reasoning and the census
    treats this as corroboration that can never revert a row.

    FOUR PATHS WRITE NOTHING, each because it is not even a helm allow:
      - a REENTRY (`stop_hook_active`): the harness is re-entering a stop that
        has already been handled, so this is not a fresh dispatch.
      - a DISABLED guard (HELM_NO_STOP_GUARD): nothing measured the stop, so
        nothing may be claimed about it.
      - a FAIL-OPEN handler (timeout, crash, unreadable exit code): its 0 is
        an absence of an answer, not an allow. The caller has already
        excluded these; this function does not depend on that.
      - a payload with no session, or a seat/session pair the ROSTER does not
        reconcile: a record that cannot be bound to the incarnation that
        earned it would let a dead seat's last dispatch read as the live
        one's. `turnstamp.reconcile` is the refusing door, not this one.

    AND IT NEVER RAISES, because a bookkeeping write must not turn an allowed
    stop into a crashed hook.
    """
    try:
        import json
        from . import turnstamp
        from .seats_stop_signals import _off
        if _off("STOP_GUARD"):
            return
        d = json.loads(payload or "")
        if not isinstance(d, dict) or d.get("stop_hook_active"):
            return
        session = d.get("session_id")
        if not session:
            return
        from . import home
        seat = home.chat_name()
        if not seat:
            return
        turnstamp.record(seat, session)
    except Exception:                             # noqa: BLE001
        return

def run_one(spec, payload, argv=None, outcome=None, left=None):
    from . import hooklatency
    phase = {"record": "record", "deliver": "delivery", "whisper-prepare": "prepare"}.get(spec.get("name"))
    with hooklatency.dispatch_scope(), hooklatency.stage(phase):
        return _run_one(spec, payload, argv=argv, outcome=outcome, left=left)


def _run_one(spec, payload, argv=None, outcome=None, left=None):
    """Run ONE handler in-process on `payload`. Returns its rc, or 0 fail-open.

    `outcome`, when given, is a dict this stamps with ``status``, one of
    ANSWERED, UNCHECKED or SKIPPED. A TYPE RATHER THAN A BOOLEAN because the
    two non-answers are different facts and a caller needs to tell them
    apart: UNCHECKED means the handler was asked and did not answer (timeout,
    crash, an exit code that will not parse), while SKIPPED means it never
    ran. A fail-open 0 is indistinguishable from a real allow by rc alone,
    and a corroborating stamp written on one would record a dispatch that
    never happened — so the distinction is reported rather than inferred.

    THE RC IS NOT TOUCHED BY ANY OF THIS. A fail-open still returns 0 and an
    advisory failure is never converted into a veto; the status travels
    beside the rc, which is what lets `run_event` decline to stamp without
    changing what the harness is told.

    Isolation restored on every path: stdin, argv and cwd are saved and put
    back even when the handler raises, times out, or calls os.chdir. A handler
    that leaks one of those into its sibling is the in-process hazard that does
    not exist when each runs in its own process.

    `left` is what remains of this handler's share of the hook's budget
    measured from the WRAPPER'S start (`run_event` computes it from
    HELM_HOOK_T0). The alarm is the smaller of it and the handler's own
    budget, so interpreter startup is charged to the handler instead of being
    discovered by the outer `timeout`. None keeps the handler's own budget.
    """
    import shlex
    from . import cli, hookoutcome, hooklatency
    args = argv if argv is not None else shlex.split(spec["args"])
    name = spec.get("name") or (args[0] if args else "?")
    budget = float(spec.get("timeout") or 0)

    # CLEAR BEFORE, NOT AFTER: a declaration left by the previous handler
    # would be read as this one's outcome, which is the exact
    # failure-that-looks-like-success this channel exists to prevent.
    hookoutcome.begin()
    old_stdin, old_argv, old_cwd = sys.stdin, list(sys.argv), None
    try:
        old_cwd = os.getcwd()
    except OSError:
        old_cwd = None
    prev_handler = None
    armed = False
    try:
        # TextIOWrapper over BytesIO, NOT StringIO: StringIO has no `.buffer`,
        # and seats_cli._hook_stdin reads `sys.stdin.buffer`. With a StringIO it
        # raises AttributeError, gets caught by that function's own except, falls
        # through to the plain reader which reads `.buffer` AGAIN, is caught
        # again, and RETURNS {} — so deliver, join, delegation-stop and
        # stop-guard would every one receive an empty payload, no-op, and exit
        # 0. Four guards silently disarmed while the dispatcher reported
        # success: the exact failure-that-looks-like-success this module claims
        # to prevent. Caught by an independent reader, not by me.
        sys.stdin = io.TextIOWrapper(io.BytesIO(payload.encode("utf-8")),
                                     encoding="utf-8")
        sys.argv = ["helm"] + args
        if budget > 0 and hasattr(signal, "SIGALRM"):
            prev_handler = signal.signal(signal.SIGALRM, _alarm)
            alarm = budget if left is None else max(0.001, min(budget, left))
            signal.setitimer(signal.ITIMER_REAL, alarm)
            armed = True
        rc = cli.main(args)
        if outcome is not None:
            # THE HANDLER'S OWN ACCOUNT OF ITS rc WINS. A normal return only
            # means the call did not raise; the places that KNOW it was a
            # crash-publish, an expired ladder or a project-scope skip are
            # deep inside the handler and all of them return 0. See
            # helm/hookoutcome.py for why this arrives on a channel.
            declared, why = hookoutcome.taken()
            outcome["status"] = declared or ANSWERED
            if why:
                outcome["why"] = why
        rc = int(rc or 0)
        if rc:
            hooklatency.mark("nonzero")
        if outcome is not None and outcome.get("status") in (UNCHECKED, SKIPPED):
            hooklatency.mark(outcome["status"])
        return rc
    except _Timeout as exc:
        hooklatency.mark("timeout")
        # THE FRAME SURVIVES THE CATCH. Consuming the timeout here means the
        # spans around this call exit normally, so nothing re-raises through
        # `_span` and the frame the alarm interrupted — already captured by
        # the signal handler — would be dropped one frame before the END row
        # that exists to report it.
        hooklatency.blocked_in(getattr(exc, "where", None))
        # rc 124's shape: the guard was KILLED, it did not pass. Fail open —
        # that law does not move — but never in silence, which was the bug.
        # ONE LINE PER CLASS PER WINDOW. The sentence keeps hook, budget and
        # consequence and drops everything else; UNCHECKED stays, because a
        # silent fail-open is the worse failure. A repeat inside the window
        # prints nothing and is counted, and the next line that speaks carries
        # the arrears — `hookalarm` holds both halves and the sh ladder in
        # `hooks.spec_command` writes the same files.
        said = hookalarm.line(
            "handler-%s" % name,
            "[helm %s] TIMED OUT at %gs — this event is UNCHECKED"
            % (name, budget))
        if said:
            sys.stderr.write(said + "\n")
        if outcome is not None:
            outcome["status"] = UNCHECKED
            # THE CAUSE TRAVELS WITH THE OUTCOME, NOT ONLY TO STDERR. Every
            # caller that renders an UNCHECKED reads `why`; with it empty they
            # print a fallback naming BOTH causes because they cannot tell
            # which happened. This branch knows exactly which, and the fact was
            # already being recorded one line above under a different token —
            # so the reporting gap was never a measurement gap.
            outcome["why"] = "handler timed out after %gs" % budget
        return 0
    except SystemExit as e:                       # a handler that calls sys.exit
        # AN ANSWER, NOT A FAIL-OPEN: the handler chose this code. An
        # uninterpretable code is NOT an answer, so it fails open like a crash.
        try:
            rc = int(e.code or 0)
        except (TypeError, ValueError):
            hooklatency.mark("unchecked")
            # THE ONLY SILENT UNCHECKED PATH, AND IT WAS THE WORST ONE: a
            # handler exiting with a code helm cannot read produced no stderr
            # line at all, so the caller's fallback said "see stderr" about an
            # empty stderr. It says what it saw now, and carries it.
            #
            # STAMP FIRST, THEN SPEAK, and the order is the whole point. Both
            # of the steps below run code this module does not own: `%r` calls
            # the exit object's OWN __repr__, and a stderr write fails on a
            # closed or broken stream. Either one raising before the stamp
            # escapes into `run_event`'s dispatch loop, which has no handler
            # of its own — so EVERY LATER HANDLER IN THAT EVENT NEVER RUNS.
            # That is the failure this module exists to prevent, and the line
            # that reintroduced it was the one added to make this path speak.
            why = "handler exited with an uninterpretable code"
            if outcome is not None:
                outcome["status"] = UNCHECKED
                outcome["why"] = why
            # ONE GUARD AROUND EVERYTHING THAT RUNS FOREIGN CODE, and the
            # stamp above is a CONSTANT so it cannot need any. A first cut
            # guarded `repr(e.code)` and then degraded to
            # `type(e.code).__name__` — which reaches a METACLASS attribute
            # and can raise in its own right, so the hazard moved one frame in
            # rather than leaving. Enumerating the ways an object can refuse
            # to describe itself is the wrong shape: nothing the handler
            # supplies may run outside this block.
            try:
                detailed = "%s %r" % (why, e.code)
                if outcome is not None:
                    outcome["why"] = detailed
                sys.stderr.write("[helm %s] %s — this event is ALLOWED and "
                                 "UNCHECKED\n" % (name, detailed))
            except BaseException:                 # noqa: BLE001 — see above
                pass      # the constant stamp already carries a usable reason
            return 0
        if outcome is not None:
            outcome["status"] = ANSWERED
        if rc:
            hooklatency.mark("nonzero")
        return rc
    except BaseException as e:                    # noqa: BLE001 — see module docstring (3)
        # ONE HANDLER RAISING MUST NOT SILENCE THE OTHERS. Traceback preserved
        # on stderr so a crash still leaves the evidence it leaves today.
        import traceback
        hooklatency.mark("exception" if isinstance(e, Exception) else "cancelled")
        sys.stderr.write("[helm %s] HANDLER FAILED (%s) — this event is "
                         "ALLOWED and UNCHECKED\n" % (name, e.__class__.__name__))
        traceback.print_exc(file=sys.stderr)
        if outcome is not None:
            outcome["status"] = UNCHECKED
            # The traceback stays on stderr; the CLASS travels, so a summary
            # line can say "handler raised X" instead of guessing between a
            # timeout and a crash.
            outcome["why"] = "handler raised %s" % e.__class__.__name__
        return 0
    finally:
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            # signal.signal() returns None when the previous handler was not
            # installed FROM PYTHON, so `if prev_handler is not None` would
            # leave `_alarm` installed as the process SIGALRM handler — able to
            # raise _Timeout into code that has nothing to do with this
            # dispatcher. Restore unconditionally, defaulting to SIG_DFL.
            signal.signal(signal.SIGALRM,
                          prev_handler if prev_handler is not None else signal.SIG_DFL)
        sys.stdin = old_stdin
        sys.argv = old_argv
        if old_cwd is not None:
            try:
                os.chdir(old_cwd)
            except OSError:
                pass


def _tool_of(payload):
    """The tool name from a hook payload, or None. Never raises: a garbled
    payload must fall back to "unknown tool" and let named matchers skip,
    exactly as an unparseable event does today."""
    try:
        import json
        d = json.loads(payload or "")
        if isinstance(d, dict):
            v = d.get("tool_name")
            return v if isinstance(v, str) and v else None
    except Exception:                             # noqa: BLE001
        return None
    return None


def run_event(event, payload=None, tool_name=None, specs=None):
    """Run every in-process handler for `event`. Returns the event's exit code.

    STDIN IS READ EXACTLY ONCE, here, and replayed to each handler. Every
    handler does its own `sys.stdin.read()` — the second one in a shared
    process would otherwise get an empty string and silently do nothing, which
    is the failure mode that looks like success.
    """
    if payload is None:
        payload = "" if sys.stdin.isatty() else sys.stdin.read()
    if tool_name is None:
        # THE MATCHER'S INPUT LIVES IN THE PAYLOAD, NOT IN ARGV. The harness
        # passes no tool name on the command line, so a Bash-matched gate
        # (argv-guard) would never resolve if we only looked at flags — it
        # would silently stop running on every Bash call, which is precisely
        # the disarmed-guard failure this file is meant to prevent.
        tool_name = _tool_of(payload)
    # cli.main runs which_helm_warning() on EVERY call — two git subprocesses.
    # Per-handler that would re-pay, per event, the very cost this module
    # removes, and turn a 2-handler event into 4 extra process spawns. The
    # warning is for a human typing `helm`, not for a hook, so it is suppressed
    # for the whole dispatch and restored after.
    prior = os.environ.get("HELM_NO_TREE_WARNING")
    os.environ["HELM_NO_TREE_WARNING"] = "1"
    try:
        worst, ran, all_answered = 0, 0, True
        # THE REGISTRY IS PART OF THE DISPATCH, AND ACQUIRING IT CAN FAIL.
        # `event_specs` reads settings and the handler table; if that read
        # raises, this event ran NO handlers while returning the same rc 0 an
        # all-clear returns. Treating that as an allow-with-nothing-to-do is
        # how a dispatch that consulted nobody comes to look like a dispatch
        # everybody passed — so the rc stays 0 (an advisory failure is never
        # converted into a veto) and the aggregate is marked UNCHECKED.
        try:
            dispatch = list(event_specs(event, tool_name=tool_name,
                                        specs=specs))
        except Exception as e:                    # noqa: BLE001
            sys.stderr.write("[helm hookrun] THE HANDLER REGISTRY FAILED TO "
                             "READ (%s) — this event is ALLOWED and "
                             "UNCHECKED\n" % (e.__class__.__name__,))
            return 0
        # REGISTRY COMPLETENESS IS PART OF THE ANSWER. A family whose import
        # failed is caught INSIDE the loader, which returns the survivors — so
        # the dispatch proceeds (fail-open, deliberately) with handlers that
        # never ran and no exception for anyone to catch. An aggregate that
        # ignores this reports "every handler answered" about a set that was
        # silently shortened.
        registry_complete = not _AUTH_MISSING
        # THE HOOK'S CLOCK STARTED IN THE WRAPPER. The outer timeout is the
        # sum of these handlers' budgets, armed before this interpreter began,
        # so each handler's alarm is its share of that sum measured from the
        # wrapper's start: startup is charged to the first handler, and a
        # handler that finishes early gives no later one more than its own.
        from . import procage
        import time as _time
        before = procage.hook_elapsed()
        began = _time.monotonic()
        shares = 0.0
        for spec in dispatch:
            outcome = {}
            shares += float(spec.get("timeout") or 0)
            left = None if before is None else \
                shares - before - (_time.monotonic() - began)
            rc = run_one(spec, payload, outcome=outcome,
                         **({} if left is None else {"left": left}))
            worst = _stronger(worst, rc, spec)
            # `_stronger` stays the ONE judge of what escapes: asked from a
            # clean slate, it answers whether THIS handler's rc is a refusal.
            if _stronger(0, rc, spec):
                _count_refusal(spec, payload)
            ran += 1
            all_answered = all_answered and outcome.get("status") == ANSWERED
        # THE AGGREGATED ALLOW, AND IT IS AS FAR AS HELM CAN SEE. Every helm
        # Stop handler has now answered and none vetoed. Earlier points all
        # lie: the hook fires on refusals, one guard's allow is not the
        # event's (this loop keeps going after an rc 2), and the guard's own
        # timing/probe records fire on BLOCK, ALLOW and crash alike.
        #
        # IT IS STILL NOT ACCEPTANCE. A foreign plugin Stop hook outside this
        # dispatcher can veto after this line runs, which is why what gets
        # written here is corroboration and never a completion.
        #
        # SCOPED TO THIS FUNCTION ON PURPOSE: `run_one` is a public door other
        # callers use for a single handler, and a stamp placed there would
        # fire on one guard's allow. The aggregate is only knowable here.
        if event == "Stop" and worst == 0 and ran and all_answered \
                and registry_complete:
            _stamp_stop_allowed(payload)
        return worst
    finally:
        if prior is None:
            os.environ.pop("HELM_NO_TREE_WARNING", None)
        else:
            os.environ["HELM_NO_TREE_WARNING"] = prior


def dispatch_command(event, specs):
    """The ONE hook command that replaces every in-process handler for `event`.

    THE TIMEOUT IS THE SUM of the handlers' budgets, not the max. Each handler
    is still cut at its OWN budget inside the process (SIGALRM per call). This
    dispatcher serializes its members; separate native hook entries may run in
    parallel, so declaration order does not establish their live scheduling.
    Using the max here would let a slow-but-legal first member spend its
    sibling's budget. Combining members removes duplicate interpreter startup
    structurally; live wall-time savings require paired event measurements.

    THE GATE LADDER IS REUSED VERBATIM from hooks.spec_command by handing it a
    SYNTHETIC spec: an event whose handlers include any gate must carry the
    same rc ladder, and rewriting that text here would be a second copy of the
    fleet's only enforcement layer, free to drift from the original.
    """
    from . import hooks
    total = sum(int(sp.get("timeout") or 0) for sp in specs) or hooks.TIMEOUT_S
    gate = any(sp.get("gate") for sp in specs)
    synthetic = {"name": "dispatch-%s" % event, "event": event,
                 "args": "hooks run %s --hook-json" % event,
                 "timeout": total}
    if gate:
        synthetic["gate"] = True
    return hooks.spec_command(synthetic)


def event_matcher(specs):
    """The matcher the merged entry must carry for `specs`.

    NOT always "*". PreToolUse's only in-process handler is Bash-matched, and
    widening it to "*" would fire a helm spawn on EVERY Read, Edit and Glob —
    adding cost on a lane whose entire purpose is removing it. So a named
    matcher is preserved when every handler shares it, and only widens to "*"
    when some handler genuinely applies to all tools.
    """
    named = {sp.get("matcher") for sp in specs}
    if len(named) == 1:
        only = next(iter(named))
        if only not in (None, "*"):
            return only
    return "*"


def settings_block():
    """{event: (matcher, command, [handler names])} — the merged wiring,
    derived from the registry.

    Emitted rather than hand-written because a settings block typed by hand is
    a second source of truth for what fires, and this lane exists because there
    were already three. EXTERNAL specs are absent by construction (they are not
    in the registry), so the operator keeps their own entries — this says what
    helm can merge, never what the whole file should be.
    """
    events = []
    for spec in dispatch_specs():
        ev = spec.get("event")
        if ev and ev not in events:
            events.append(ev)
    out = {}
    for ev in events:
        # Resolve with the event's own matcher in hand: a Bash-only event needs
        # its named tool to resolve at all.
        probe = tuple(sp for sp in dispatch_specs()
                      if sp.get("event") == ev and not sp.get("external"))
        if not probe:
            continue
        matcher = event_matcher(probe)
        sp = event_specs(ev, tool_name=None if matcher == "*" else matcher)
        if not sp:
            continue
        out[ev] = (matcher, dispatch_command(ev, sp), [x["name"] for x in sp])
    return out
