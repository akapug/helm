"""Installed PostToolUse pair: one terminal response, not buffered delivery.

Record keeps its deployed 10s. Whisper preparation gets an independent 2s,
then delivery gets its full 2s. Preparation does not consume a whisper latch.
The injected synchronous emitter runs under the EXISTING delivery locks:
cursor commit -> complete fd1 publication -> delivery receipt. Only after
that owner returns do we commit the published whisper, still inside delivery's
2s deadline. A failed/expired post-publication step is UNKNOWN, never repair
JSON or replay of the delivery occurrence. An uncommitted whisper can be offered
on a later event; publication plus latch commit is not atomic. Standalone
delivery and for_hook are not changed.

Input acquisition (1s), registry lookup (two bounded 1s reads), and failure-only
publication (1s) use the existing 5s shell grace, not a cut to any handler's
budget. Normal latch commit has no additional stage or deadline. The installed
shell MUST NOT append fallback JSON: after a kill it cannot know whether fd1
was already attempted. Lost runtime/output is UNKNOWN on stderr, not a promise
of visible context. Wiring belongs to hooks.
"""
import contextlib
from contextvars import ContextVar
import json
import os
import signal
import sys

from . import hookalarm, hookoutcome, hookrun, hooklatency

_CURRENT = ContextVar("helm_posttoolrun", default=None)
GRACE_STEP_S = 1.0


def current():
    return _CURRENT.get()


@contextlib.contextmanager
def _grace_deadline():
    # Only called BETWEEN run_one calls, whose own timers are disarmed. Do not
    # silently appropriate an existing timer; active-timer nesting is separate.
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise RuntimeError("an existing alarm prevents bounded finalization")
    prior = signal.signal(signal.SIGALRM, hookrun._alarm)
    try:
        signal.setitimer(signal.ITIMER_REAL, GRACE_STEP_S)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prior if prior is not None else signal.SIG_DFL)


def _record_spec():
    from . import record
    return record.deployed_spec()


def _delivery_spec():
    from . import hooks
    return next(dict(s) for s in hooks.SPECS
                if s.get("name") == "deliver" and s.get("event") == "PostToolUse"
                and not s.get("external"))


class _Event:
    def __init__(self):
        self.phase = "registry"
        self.alarms = []
        self.issues = set()
        self.candidate = None
        self.delivery_allowed = False
        self.attempted = False
        self.complete = False
        self.whisper_published = False
        self.latch_attempted = False

    def consumed(self, exc):
        """Hand a CAUGHT timeout's evidence to the observer before reporting.

        A timeout caught HERE never propagates through the enclosing latency
        spans, so they exit with the location the alarm already captured
        thrown away -- and `failure` marks them unchecked, burying a KNOWN
        timeout under the no-answer outcome. The same door `_run_one` uses:
        record where the deadline expired, and the strictest outcome."""
        if isinstance(exc, hookrun._Timeout):
            hooklatency.blocked_in(getattr(exc, "where", None))
            hooklatency.mark("timeout")

    def failure(self, why, declare=True):
        hooklatency.mark("unchecked")
        key = (self.phase, why)
        if declare:
            hookoutcome.declare(hookoutcome.UNCHECKED, why)
        if key in self.issues:
            return
        self.issues.add(key)
        # `issues` DEDUPES INSIDE ONE EVENT; THE NOISE IS ACROSS EVENTS. This
        # pair fires on EVERY tool call, so an unchanged failure printed the
        # same sentence to the owner and to the agent a dozen times a turn on
        # every project — which is how the fail-open alarm became unreadable.
        # `hookalarm` keeps the first line of each window in full, counts the
        # rest, and hands the count to the next line that speaks. The
        # machine-readable half is untouched: `hookoutcome.declare` above runs
        # whether or not this event is the one that says it out loud, so no
        # consumer of the outcome ever sees a suppressed failure as an answer.
        if self.attempted:
            # NEVER RATE-LIMITED. This is not the per-tool-call banner: it says
            # a response was already half-published and the event's outcome is
            # genuinely UNKNOWN. It is rare, it is different every time, and it
            # is the one line a reader must see on the call it happened. The
            # first cut suppressed this branch with the rest and six arms about
            # partial and zero-byte writes went red asserting an empty stderr.
            message = "[helm posttoolrun] UNKNOWN after stdout attempt: %s: %s; no repair output or replay" % (self.phase, why)
        else:
            spoke, swallowed, since = hookalarm.speak(
                "posttoolrun-%s" % self.phase)
            if not spoke:
                return
            message = ("[helm posttoolrun] %s: %s — UNCHECKED%s"
                       % (self.phase, why,
                          hookalarm.arrears(swallowed, since)))
            self.alarms.append(message)
        # Diagnostics are best-effort, not a prerequisite for sibling handlers
        # or publication through an available stdout. Do not swallow cancellation.
        try:
            sys.stderr.write(message + "\n")
        except Exception:
            pass

    def prepare(self, session):
        from . import toolwhisper
        self.candidate = toolwhisper.prepare_for_pair(session)

    def emit(self, line=None, whisper=True):
        if self.attempted:
            raise RuntimeError("terminal response already attempted")
        candidate = self.candidate if whisper and self.delivery_allowed else None
        lines = list(self.alarms)
        if candidate:
            lines.append(candidate["text"])
        if line:
            if not isinstance(line, str):
                raise TypeError("delivery context must be text")
            lines.append(line)
        if not lines:
            return
        document = {"hookSpecificOutput": {
            "hookEventName": "PostToolUse", "additionalContext": "\n\n".join(lines)}}
        if self.alarms:
            document["systemMessage"] = "\n".join(self.alarms)
        wire = (json.dumps(document, ensure_ascii=False) + "\n").encode("utf-8")
        # Serialize BEFORE marking attempted. Once any fd1 write is attempted,
        # even one reporting no bytes, no path may issue a replacement JSON.
        self.attempted = True
        offset = 0
        while offset < len(wire):
            n = os.write(1, wire[offset:])
            if n <= 0:
                raise OSError("stdout write made no progress")
            offset += n
        self.complete = True
        self.whisper_published = bool(candidate)

    def commit_published(self):
        if not self.complete or not self.whisper_published or self.latch_attempted:
            return
        from . import toolwhisper
        self.latch_attempted = True
        toolwhisper.commit_for_pair(self.candidate)

    def finish(self):
        # Called by seats_cli AFTER deliver_any returns, hence AFTER any receipt,
        # but before run_one disarms the delivery timer. Quiet delivery may have
        # left just a whisper/alarm to publish; a wholly quiet event emits none.
        if not self.attempted:
            self.emit()
        self.commit_published()

    def stage(self, phase, spec, payload):
        self.phase = phase
        outcome = {}
        try:
            rc = hookrun.run_one(spec, payload, outcome=outcome)
        except (Exception, hookrun._Timeout) as exc:
            self.failure("dispatcher failed (%s: %s)" % (type(exc).__name__, exc), declare=False)
            return {"status": hookoutcome.UNCHECKED}
        if rc:
            self.failure("handler returned rc %s" % rc, declare=False)
        elif outcome.get("status") == hookoutcome.UNCHECKED:
            # THE FALLBACK NO LONGER NAMES A CAUSE IT DID NOT MEASURE.
            # Every UNCHECKED path in `hookrun.run_one` now sets `why` — timed
            # out after Ns, raised X, exited with an uninterpretable code — so
            # reaching this string means the status arrived from somewhere
            # that did NOT say, and the only honest report is that we do not
            # know. "timed out or crashed" named two causes precisely because
            # it could not tell them apart, and an operator reading it had no
            # way to learn which: one of the three paths wrote nothing to
            # stderr at all.
            self.failure(outcome.get("why") or
                         "handler did not answer and gave no reason "
                         "(budget %gs; see stderr)" % spec["timeout"],
                         declare=False)
        return outcome

    def lookup(self, name, getter):
        self.phase = name + " registry"
        try:
            with hooklatency.stage(name + "-registry"), _grace_deadline():
                return getter()
        except (Exception, hookrun._Timeout) as exc:
            self.consumed(exc)
            self.failure("declaration unavailable (%s)" % type(exc).__name__, declare=False)
            return None


@contextlib.contextmanager
def _observe_recorder(event):
    # record.record intentionally swallows ordinary Exceptions. Observe only its
    # low-level boundary, then re-raise into that SAME catcher: owner behavior
    # and CLI dispatch remain unchanged, but the installed event learns why it
    # did not answer. Restore the module slot on every path. Other contexts call
    # the original implementation without acquiring this event's declaration.
    from . import record
    original = record._record

    def observed(payload):
        try:
            return original(payload)
        except Exception as exc:
            if current() is event:
                event.failure("recorder failed (%s: %s)" % (type(exc).__name__, exc))
            raise

    record._record = observed
    try:
        yield
    finally:
        record._record = original


def run(payload=None):
    """Run the installed pair, always fail-open (rc 0); read stdin only once."""
    with hooklatency.event_scope("composite", observer="python-runtime-entry-v1"):
        return _run(payload)


def _run(payload=None):
    event = _Event()
    token = _CURRENT.set(event)
    prior = os.environ.get("HELM_NO_TREE_WARNING")
    os.environ["HELM_NO_TREE_WARNING"] = "1"
    try:
        if payload is None:
            with hooklatency.stage("input"), _grace_deadline():
                payload = "" if sys.stdin.isatty() else sys.stdin.read()
        record_spec = event.lookup("record", _record_spec)
        if record_spec:
            try:
                with _observe_recorder(event):
                    event.stage("record", record_spec, payload)
            except (Exception, hookrun._Timeout) as exc:
                event.consumed(exc)
                event.phase = "record"
                event.failure("recorder boundary unavailable (%s)" % type(exc).__name__, declare=False)
        delivery = event.lookup("delivery", _delivery_spec)
        if delivery:
            # Existing --room skips cmd_chat's default homing before seats_cli;
            # preparation needs only session/identity, not delivery room state.
            # The prefix remains chat deliver --hook-json for the SAME CLI scope.
            prep = dict(delivery, name="whisper-prepare",
                        args=delivery["args"] + " --room main")
            event.stage("prepare", prep, payload)
            result = event.stage("delivery", delivery, payload)
            if result.get("status") == hookoutcome.SKIPPED:
                event.delivery_allowed = False
    except (Exception, hookrun._Timeout) as exc:
        event.consumed(exc)
        event.failure("pair failed (%s: %s)" % (type(exc).__name__, exc), declare=False)
    finally:
        try:
            # If a handler failed/refused/skipped BEFORE output, publish only
            # accumulated alarms. Never publish/latch an uncompleted whisper on
            # this fallback, and never attempt output again after an fd1 attempt.
            if event.alarms and not event.attempted:
                try:
                    with _grace_deadline():
                        event.emit(whisper=False)
                except (Exception, hookrun._Timeout) as exc:
                    event.consumed(exc)
                    event.failure("alarm publication failed (%s)" % type(exc).__name__, declare=False)
        finally:
            if prior is None:
                os.environ.pop("HELM_NO_TREE_WARNING", None)
            else:
                os.environ["HELM_NO_TREE_WARNING"] = prior
            _CURRENT.reset(token)
    return 0
