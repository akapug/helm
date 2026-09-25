#!/usr/bin/env python3
"""Rung timings for the Stop guard's outer-timeout evidence — BUFFERED until
the ladder is in danger, then streamed.

WHAT THIS TRACE IS FOR, WHICH IS NARROWER THAN WHAT IT USED TO PRINT. A ladder
killed by the OUTER `timeout` writes nothing at all: the kill is a signal, not
a return, so the process that would have published a verdict is simply gone.
These lines are the only account of where that ladder was. A ladder that
FINISHES has no such hole — it publishes its verdict, and `stopprobe` has
already written every boundary to a durable single-writer log once the run
passed its SLOW mark. So a completed ladder's trace is evidence nobody reads,
and it was printed on EVERY stop: twenty-eight lines in front of the one
sentence the owner needed, on a run that was never in danger.

SO THE LINES ARE KEPT AND NOT PRINTED. Every boundary is buffered. Nothing
reaches stderr while the ladder is comfortably inside its budget; once elapsed
crosses the threshold the whole buffer is flushed in order and every later
boundary streams as it happens, so a ladder that is about to be killed has
still left its evidence — and with the timestamps it would have had.

THE THRESHOLD IS A FRACTION OF THE LADDER'S OWN RESERVE, not a round number.
`seats_stop_budget.BUDGET_S` is the deadline the ladder enforces on itself
inside an outer `timeout` that is larger still, so the useful threshold is as
LATE as it can be while leaving room to flush: earlier costs the owner lines on
runs that were always going to finish, later risks the axe landing on a full
buffer. STREAM_AFTER_FRACTION sits at 0.7 of that reserve, which leaves the
last third of the reserve plus the whole harness grace streaming.

AND THERE IS AN OFF, BECAUSE A WALL-CLOCK-KEYED WRITE INTO AN OBSERVABLE
SOMEBODY ELSE PINS BYTE-FOR-BYTE IS NONDETERMINISTIC BY CONSTRUCTION. A test
suite is exactly that observer: its arms capture stderr, a loaded node makes a
ladder slow, and a flush lands inside whichever arm holds the capture at that
instant -- including a flush from a timer armed by a ladder that ended in an
EARLIER arm, since the timer fires on wall time into the CURRENT `sys.stderr`.
`HELM_STOP_TIMING_AFTER=off` means the stream never speaks at any elapsed
time, and `0` cannot express it: `0` streams from the FIRST line. The buffer
and the end-of-ladder report are untouched -- only the stream is keyed on
time, so only the stream is what an off switch may take away.

THE CROSSING CANNOT WAIT FOR THE NEXT BOUNDARY. A rung that stalls silently for
the entire budget emits nothing, so a threshold checked only at emit points
would let exactly the run this trace exists for die with an unflushed buffer —
the hole is widest precisely where the evidence matters most. A one-shot daemon
timer fires the flush at the threshold whether or not anything is being
emitted; it is a timer rather than `signal.setitimer` because SIGALRM is
already spoken for on this path (hookrun arms it per handler) and a second
owner of one process-wide signal is a silent theft, not a feature.
"""
import os
import sys
import threading
import time

from . import projscope, stopprobe

# THE ONE KNOB, spelled the way `stopprobe.SLOW` is spelled: seconds, read
# once, and a malformed value is ignored rather than allowed to take the report
# down. 0 streams from the first line, which is the debugging posture.
STREAM_AFTER_ENV = "HELM_STOP_TIMING_AFTER"

# AND THE ONE VALUE OF IT THAT IS NOT SECONDS, spelled the way this tree
# spells a disabled knob everywhere else (`off`, case-insensitively). It is
# decided BEFORE `threshold_from_env` ever sees it, because that parser's
# whole contract is that an unparseable value means the default stands -- so a
# spelling that must MEAN something can never be routed through it.
STREAM_OFF = "off"


class _Never:
    """THE OFF STATE IS A VALUE, NOT A VERY LARGE NUMBER.

    A sentinel rather than `float("inf")` because both comparisons that
    consume the threshold keep working against an infinity and neither says
    so: `after > 0` is TRUE of infinity, so an infinite threshold still ARMS a
    timer, and a timer scheduled past the end of the process is a live object
    holding a dead ladder's buffer -- the exact thing `settle` exists to
    cancel. A sentinel cannot be compared to a clock at all, so every consumer
    has to state that it handled the off state instead of inheriting an answer
    from arithmetic."""

    def __repr__(self):
        return "NEVER"


NEVER = _Never()

# See the module docstring. A fraction rather than a literal so the threshold
# follows the budget it is derived from instead of drifting away from it.
STREAM_AFTER_FRACTION = 0.7


class _Trace:
    """THE WHOLE STREAMING STATE IN ONE CELL, MUTATED AND NEVER REBOUND.

    These five values were five module globals, and every function that moved
    one declared `global` and rebound it. That is the single write the seats
    facade cannot see. `seats.py` re-exports its siblings' names and fans an
    external `seats.X = v` out to the module that defines X through
    `__setattr__`, but `global X; X = v` inside a sibling compiles to a
    STORE_GLOBAL against this module's own `__dict__` and never reaches that
    protocol — so `seats.X` keeps the value it was imported with while the
    live value moves underneath it, and no fanout logic could tell.

    An attribute write on one object bound at import needs no `global` and
    rebinds nothing: there is a single cell, the facade has nothing to
    diverge from, and a reader who finds `_STATE` finds all of it rather than
    five names scattered down the file.
    """

    def __init__(self):
        self.after = None       # the resolved threshold, memoised
        self.buffer = []        # [(monotonic, line)] not yet printed
        self.origin = None      # when this ladder's first line was buffered
        self.streaming = False  # the threshold is crossed; print as we go
        self.timer = None       # the one-shot flush, armed at the first line


_STATE = _Trace()

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_STATE": (
        "the ladder's trace; settle() stops every timer, and the arms that "
        "read it (test_hook_wrapper) reset() it first"),
}


def stream_after():
    """Seconds of quiet before the trace speaks, or `NEVER`. Resolved ONCE.

    Not a module constant, because `seats_stop_budget` imports THIS module at
    its own module scope and BUDGET_S is defined ten lines further down: a
    module-scope read here would see a half-initialised sibling whenever the
    budget module happened to be imported first. Resolved on the first emit,
    when every module in the ladder is whole."""
    if _STATE.after is None:
        from . import seats_stop_budget
        raw = os.environ.get(STREAM_AFTER_ENV)
        if (raw or "").strip().casefold() == STREAM_OFF:
            _STATE.after = NEVER
        else:
            _STATE.after = stopprobe.threshold_from_env(
                raw, STREAM_AFTER_FRACTION * seats_stop_budget.BUDGET_S)
    return _STATE.after


_LOCK = threading.Lock()


def _write(line):
    try:
        print("[helm stop-guard timing] " + line, file=sys.stderr, flush=True)
    except Exception:
        pass


def _flush_locked():
    _STATE.streaming = True
    for _when, line in _STATE.buffer:
        _write(line)
    del _STATE.buffer[:]


def _fire():
    """The one-shot timer's flush: the ladder crossed the threshold without
    emitting anything, which is the stall this instrument exists to catch."""
    with _LOCK:
        if not _STATE.streaming:
            _flush_locked()


def settle():
    """THE LADDER IS OVER: disarm the threshold, keep everything else.

    A TIMER OUTLIVES THE THING IT MEASURES unless someone stops it. The flush
    is scheduled at the ladder's FIRST line and fires on wall time, so a ladder
    that finished in milliseconds still had a flush pending: it landed a whole
    buffer of a long-dead ladder's rung boundaries on stderr a whole threshold
    later, into whatever happened to be running then. In production that is one
    daemon thread in a process that is already exiting; in a suite — one
    process, many ladders — it is another arm's output arriving inside yours.

    THE BUFFER IS KEPT, NOT DROPPED, because disarming is not forgetting: a
    caller that emits again after settling (the expired ladder's own
    `response-fallback` rung does exactly that) still gets the emit-point
    check, which flushes immediately if the threshold has genuinely passed. Only
    the CLOCK is stopped, and only the run that ended stops it.

    `reset` remains the belt: it settles AND forgets, for the next ladder.

    THE ONE WINDOW THIS CLOSES OVER, KNOWN AND ACCEPTED: a ladder that
    finishes FAST and then blocks in publication until the outer timeout kills
    it now prints no trace at all, where the unbuffered trace printed the
    whole thing. It is narrow by construction — a ladder under `stopprobe`'s
    SLOW mark has written no durable row either, so the gap is exactly
    finished-quickly-then-hung-past-the-streaming-threshold — and it is the
    price of buffering rather than an oversight."""
    with _LOCK:
        if _STATE.timer is not None:
            _STATE.timer.cancel()
        _STATE.timer = None


def reset():
    """Forget this process's trace. Production never calls it — a hook process
    lives for one ladder — but a suite runs many arms in ONE process, and
    durable module state shared across arms is a cross-test channel: the first
    arm to cross the threshold would put every later arm into streaming mode."""
    with _LOCK:
        if _STATE.timer is not None:
            _STATE.timer.cancel()
        _STATE.timer = None
        del _STATE.buffer[:]
        _STATE.after = None
        _STATE.origin = None
        _STATE.streaming = False


def _emit(line):
    now = time.monotonic()
    with _LOCK:
        if _STATE.streaming:
            _write(line)
            return
        after = stream_after()
        # THE OFF STATE IS CHECKED AT BOTH CROSSINGS, because there are two:
        # the one-shot timer for a rung that stalls silently, and the
        # emit-point check for everything else. Disarming one leaves the other
        # speaking.
        quiet = after is NEVER
        if _STATE.origin is None:
            _STATE.origin = now
            if not quiet and after > 0:
                timer = threading.Timer(after, _fire)
                timer.daemon = True
                try:
                    timer.start()
                except RuntimeError:
                    # A process that cannot start a thread still gets its
                    # evidence from the emit-point check below; losing the
                    # timer costs the silent-stall case, never the trace.
                    timer = None
                _STATE.timer = timer
        _STATE.buffer.append((now, line))
        if not quiet and now - _STATE.origin >= after:
            _flush_locked()


def begin(name):
    """Name work outside the ladder before it can be killed."""
    started = time.monotonic()
    _emit("BEGIN %s" % name)
    return started


def done(name, started):
    _emit("DONE %s elapsed=%.3fs" % (name, time.monotonic() - started))


class RungTiming:
    """Name completed and active work without changing the guard's verdict."""

    def __init__(self):
        # A LADDER IS THE UNIT, NOT A PROCESS. The buffer, its origin and the
        # threshold timer all belong to ONE ladder: a hook process runs exactly
        # one, so this changes nothing in production — but a suite runs many in
        # one process, and a process-wide origin makes the FIRST ladder's start
        # the clock for every later one. Every arm after the first threshold
        # crossing then streams, and the accumulated buffer is flushed into
        # whichever arm happened to cross. Measured: a stop-guard arm's stderr
        # arrived carrying another test's rung boundaries.
        reset()
        self.started = time.monotonic()
        self.stage_started = self.started
        self.current = "identity"
        self.last_done = "identity"
        # ONE IDENTITY PER LADDER, minted here rather than inferred later: a
        # pid is reused, so a log grouped by pid folds a finished ladder
        # together with a new unfinished one and hides the second's silence.
        self.run_id = stopprobe.new_run_id()
        # HOW OLD THE PROCESS ALREADY WAS, measured HERE beside the monotonic
        # origin it belongs to, and never again. A ladder total says how long
        # the ladder took and nothing about when it began: total=10.3s inside a
        # process 10.4s old IS the process (a slow ladder), while the same
        # total inside a process 60s old means the ladder started fifty seconds
        # in and those fifty seconds are a different bug.
        #
        # ONE READING, NOT A SUBTRACTION. Stamping the age at WRITE time and
        # letting a reader compute age-minus-total reads two clocks at two
        # instants: `now` is frozen at the rung boundary and the write happens
        # after an _emit to stderr, which can block, so a reporting stall is
        # reported as time before the ladder that never existed. Taken here it
        # is a constant, and a constant cannot drift.
        self.start_age = stopprobe.process_age()
        # NAMED LATER, BY THE RUNG THAT RESOLVES IT. The ladder starts before
        # identity does, so this is empty for exactly as long as nobody knows.
        self.seat = None
        _emit("BEGIN identity total=0.000s")

    def done(self, name, next_name=None, complete=True):
        now = time.monotonic()
        suffix = " next=%s" % next_name if next_name else ""
        _emit("%s %s elapsed=%.3fs total=%.3fs%s" %
              ("DONE" if complete else "YIELD", name,
               now - self.stage_started, now - self.started, suffix))
        # THE DURABLE HALF. Stderr reaches a transcript, which is not an
        # instrument: the same text appears in test output and in commands
        # about it, so a population read from there is decided by a text
        # match. This writes the same boundary to a log with one writer, and
        # only once a ladder has already gone slow, so the ordinary stop pays
        # nothing. A run killed by the outer timeout leaves its last COMPLETED
        # boundary behind; the ladder went quiet in the rung after it, which
        # this file cannot name because that rung never finished.
        stopprobe.record(self.run_id, name, now - self.stage_started,
                         now - self.started, seat=self.seat,
                         began=self.start_age)
        self.stage_started = now
        self.last_done = name
        self.current = next_name
        if next_name:
            _emit("BEGIN %s total=%.3fs" %
                  (next_name, now - self.started))
            projscope.spend_or_raise("starting stop rung %s" % next_name)

    def finish(self, name=None):
        """THE LADDER IS OVER. Only the lifecycle's terminal calls this.

        Inferring the end from a rung that named no successor stamped the
        ladder complete at the first such rung, and a later rung that stalled
        was hidden behind that stamp. THE NAME DEFAULTS TO THE LAST RUNG THAT
        COMPLETED rather than to a hardcoded terminal, because which rung ends
        the ladder is the caller's business and has already changed once.

        AND THE THRESHOLD IS DISARMED HERE, at the terminal, because a run that
        reached its own end can no longer be the run this trace exists for. See
        `settle`: a pending flush that outlives its ladder speaks into whatever
        runs next.
        """
        stopprobe.record_end(self.run_id, name or self.last_done,
                             time.monotonic() - self.started, seat=self.seat,
                             began=self.start_age)
        settle()

    def begin(self, name):
        """Name an external rung, then enforce its spending door."""
        self.current = name
        started = time.monotonic()
        self.stage_started = started
        _emit("BEGIN %s" % name)
        projscope.spend_or_raise("starting stop rung %s" % name)
        return started

    def measure(self, name, fn, budget=None, fallback=None):
        stage_started = time.monotonic()
        _emit("BEGIN %s total=%.3fs" %
              (name, stage_started - self.started))
        projscope.spend_or_raise("starting stop span %s" % name)
        try:
            answer = fn() if budget is None else budget.run(
                name, fn, fallback=fallback)
        except projscope.Expired:
            raise
        except Exception:
            self._measure_finish("DONE", name, stage_started)
            raise
        kind = "YIELD" if budget and name in budget.yielded else "DONE"
        self._measure_finish(kind, name, stage_started)
        return answer

    def _measure_finish(self, kind, name, stage_started):
        now = time.monotonic()
        _emit("%s %s elapsed=%.3fs total=%.3fs" %
              (kind, name, now - stage_started, now - self.started))
        stopprobe.record(self.run_id, name, now - stage_started,
                         now - self.started, seat=self.seat,
                         began=self.start_age)
