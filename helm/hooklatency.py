"""Best-effort in-process hook spans; never a launch census or timer owner.

One bounded, seat-tagged stream (no roster discovery), three generations. The
recorder's .1 rename is prior art, NOT the unlocated cred-cron rotation owner.
All cooperating append/rotation operations hold one exclusive nonblocking flock.
This bounds lock waiting, NOT filesystem open/write latency. No fsync/durability
claim. Observed drop counters are lower bounds: a dying writer can lose its last
counter too. Shared readers copy bounded bytes under an EXISTING lock, release
it, then parse. An absent/busy lock yields an explicitly uncertain snapshot.
"""
import contextlib
from contextvars import ContextVar
import fcntl
import json
import math
import os
import re
import stat
import sys
import time
import uuid

from . import home

MAX_BYTES = 1024 * 1024
GENERATIONS = 3
MAX_ROW = 2048

#: How many ENCODED bytes of `blocked_in` a row may carry -- bytes on the
#: wire, after JSON escaping, never Python characters. EIGHT FRAMES IS NOT A BYTE
#: BUDGET: the frame walk is bounded by COUNT, and long basenames and function
#: names push the serialized row past MAX_ROW, where `append` refuses it --
#: so a KNOWN timeout becomes a censored one, and the END row carrying the
#: drop count is refused by the same rule, taking the durable evidence of the
#: loss with it. The diagnostic is the part that yields: a truncated frame
#: still names where the budget expired, while a refused row names nothing.
MAX_BLOCKED_IN = 512
#: A CHARACTER IS NOT A BYTE EITHER (task/2462): the wire is
#: ASCII JSON, so one non-ASCII code point in a legal filename or function
#: name costs six bytes, and an astral one twelve. 512 characters of those is
#: ~3 KiB, past MAX_ROW -- the same refused row and censored timeout the
#: frame bound was written to stop. The field is fitted against the ROW
#: `append` will actually write (see `_fit_blocked_in`).
# JSON integers are unbounded in Python. Fix the wire domain before any
# float conversion: both timestamp differences and duration/range divisions
# then remain representable. This is schema validation, not a runtime budget.
MAX_INTEGER = (1 << 63) - 1
MAX_WALL_MS = MAX_INTEGER / 1_000_000
OBSERVER = "python-cli-entry-v1"
#: One stage per installed hook entry beside the PostToolUse internals; the
#: entry table is `cli._HOOK_SPANS`, held to `hooks.SPECS` by a test.
HANDLER_STAGES = ("inject", "delegation-stop", "saguide", "join", "resume-turn",
                  "stop-guard", "argv-guard", "handoff")
STAGES = frozenset(("event", "input", "record-registry", "delivery-registry",
                    "record", "prepare", "delivery", "lock-seats", "lock-proxywatch",
                    "lock-room", "lock-whisper", "lock-send") + HANDLER_STAGES)
#: EVERY HOOK EVENT helm installs, not only PostToolUse (task/3040): the Stop,
#: PreToolUse and SessionStart hooks are the ones a seat waits on, and a ledger
#: that could not name them could not show a single one of their timeouts. The
#: two `-or-` labels are provisional: one argv serves two events and the START
#: row is written before any payload is read.
EVENTS = frozenset(("PostToolUse", "PostToolUseFailure", "PostToolUse-or-Failure",
                    "UserPromptSubmit", "SubagentStart", "SubagentStop",
                    "SessionStart", "Stop", "PreToolUse", "PreCompact",
                    "SessionEnd", "PreCompact-or-SessionEnd"))
#: Each provisional label and the events it can turn out to be.
PROVISIONAL = {"PostToolUse-or-Failure": ("PostToolUse", "PostToolUseFailure"),
               "PreCompact-or-SessionEnd": ("PreCompact", "SessionEnd")}
OUTCOMES = ("completed", "acquired", "nonzero", "skipped", "unchecked", "exception",
            "failed-open", "cancelled", "timeout")
_CURRENT = ContextVar("helm_hooklatency", default=())
_DISPATCH = ContextVar("helm_hooklatency_inner_dispatch", default=False)
_ID = re.compile(r"\A[A-Za-z0-9._-]{1,128}\Z")
_KEYS_V1 = frozenset(("schema", "observer", "writer", "sequence", "observed_drops",
                      "event_id", "span_id", "parent_id", "seat", "session",
                      "event", "mode", "stage", "kind", "monotonic_ns", "time_ns",
                      "wall_ms", "startup_ms", "outcome"))
#: v2 adds `blocked_in`: where a span was executing when its budget expired.
_KEYS_V2 = _KEYS_V1 | {"blocked_in"}
#: THE KEY SET IS EXACT IN BOTH DIRECTIONS, so a new field is a NEW VERSION or
#: it invalidates the entire retained history. Adding `blocked_in` to one
#: frozen set would have rejected every row already on disk — and the reader
#: reports rejects as `invalid`, so the loss would have arrived as a counter
#: nobody reads rather than as an error. The reader accepts BOTH versions and
#: the writer emits v2; a reader that lands after its writer drops the very
#: rows the change exists to collect.
_KEYS_BY_SCHEMA = {1: _KEYS_V1, 2: _KEYS_V2}
SCHEMA = 2


def path():
    return os.path.join(home.helm_home(), "_global", ".state", "hook-latency.jsonl")


#: THE INCIDENTS OUTLIVE THE STREAM. At fleet rate the three generations above
#: hold minutes (MEASURED on the agents box: 4,362 rows spanned 0.15 h), so "how many hooks
#: timed out today" had no answer at all. A span that ENDS in one of these
#: outcomes is copied, with its START, to a second stream sized for days of
#: incidents: ~3 KB each, about 160 a day at that measurement, so
#: RETAIN_BYTES x RETAIN_GENERATIONS keeps 24 h up to ~1,300 a day. That is a
#: byte bound, not a clock, so the report prints the hours it actually holds.
RETAINED = frozenset(("timeout", "cancelled"))
RETAIN_BYTES = 2 * 1024 * 1024
RETAIN_GENERATIONS = 3


def incidents_path(dest=None):
    return (path() if dest is None else dest) + ".incidents"


def _identity(value):
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _bounded_int(value):
    return type(value) is int and 0 <= value <= MAX_INTEGER


def _clock(fn):
    try:
        value = fn()
        return value if _bounded_int(value) else None
    except Exception:
        return None


def _open_regular(name, flags, mode=0o600):
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular telemetry file")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _wire(row):
    """The exact bytes `append` writes for `row` -- the one encoding every size
    decision about a row is made against."""
    return ("\n" + json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")


def append(row):
    """True only for a full observed append; ordinary failures return False.

    No fd1/stdin, diagnostics, delivery locks, retries, or cancellation catcher.
    A short append is not a successful measurement; the reader rejects it.
    """
    return _append_to(path(), row, MAX_BYTES, GENERATIONS)


def _retain(start, end):
    """Copy one incident span, START first, to the incidents stream. Best
    effort like every append here; a failure is not counted anywhere."""
    try:
        dest = incidents_path()
        for row in (start, end):
            if row is not None:
                _append_to(dest, row, RETAIN_BYTES, RETAIN_GENERATIONS)
    except Exception:
        pass


def _append_to(dest, row, max_bytes, generations):
    lock = fd = None
    try:
        if not _valid(row):
            return False
        wire = _wire(row)
        if len(wire) > MAX_ROW:
            return False
        os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
        lock = _open_regular(dest + ".lock", os.O_CREAT | os.O_RDWR)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            size = os.stat(dest, follow_symlinks=False).st_size
        except FileNotFoundError:
            size = 0
        if size + len(wire) > max_bytes:
            for n in range(generations - 1, 0, -1):
                src = dest if n == 1 else dest + "." + str(n - 1)
                try:
                    os.replace(src, dest + "." + str(n))
                except FileNotFoundError:
                    pass
        fd = _open_regular(dest, os.O_CREAT | os.O_WRONLY | os.O_APPEND)
        return os.write(fd, wire) == len(wire)
    except Exception:
        return False
    finally:
        for handle in (fd, lock):
            if handle is not None:
                try:
                    os.close(handle)
                except OSError:
                    pass


class _Event:
    def __init__(self, mode, event, observer):
        self.id = uuid.uuid4().hex
        self.mode, self.event, self.observer = mode, event, observer
        try:
            self.seat = home.chat_name()
        except Exception:
            self.seat = None
        # Session is bound only at an already-parsed payload seam, not guessed
        # from an ancestor environment. Seat is declared, not roster-admitted.
        self.session = None
        self.sequence = self.drops = 0

    def write(self, span, kind, stamp, elapsed=None):
        self.sequence += 1
        row = dict(schema=SCHEMA, observer=self.observer, writer=self.id,
                   sequence=self.sequence, observed_drops=self.drops,
                   event_id=self.id, span_id=span.id, parent_id=span.parent,
                   seat=self.seat, session=self.session, event=self.event,
                   mode=self.mode, stage=span.stage, kind=kind,
                   monotonic_ns=stamp, time_ns=_clock(time.time_ns),
                   wall_ms=elapsed, startup_ms=None,
                   outcome=span.outcome if kind == "END" else None,
                   # WHAT THE SPAN WAS EXECUTING WHEN THE BUDGET EXPIRED, or
                   # None. The outcome says a span ENDED in timeout; this says
                   # where it was at that instant, which is the question a
                   # per-stage tally structurally cannot answer.
                   blocked_in=None)
        if kind == "END":
            row["blocked_in"] = _fit_blocked_in(span.blocked_in, row)
        try:
            written = append(row)
        except Exception:
            written = False
        if not written:
            self.drops += 1
        if kind == "START":
            span.start_row = row
        elif row["outcome"] in RETAINED:
            _retain(span.start_row, row)


class _Span:
    def __init__(self, event, stage, parent):
        self.event, self.stage, self.parent = event, stage, parent
        self.id = uuid.uuid4().hex
        self.outcome = "completed"
        self.blocked_in = None
        self.start_row = None


def active():
    return bool(_CURRENT.get())


def entry_allowed():
    return not active() and not _DISPATCH.get()


@contextlib.contextmanager
def dispatch_scope():
    # A logical run_event's inner CLI is NOT a standalone interpreter entry.
    token = _DISPATCH.set(True)
    try:
        yield
    finally:
        _DISPATCH.reset(token)


def _fit_blocked_in(where, row):
    """`where`, bounded to a payload THIS ROW can actually carry.

    THE FRAME WALK IS BOUNDED BY COUNT AND THE ROW BY BYTES, and those are
    different limits: eight frames of long basenames and function names
    serialize past MAX_ROW, `append` refuses the row, and a timeout that the
    signal handler successfully diagnosed is dropped -- along with the END row
    that would have recorded the drop. Truncating the DIAGNOSTIC keeps the
    measurement; refusing the row loses both. The marker is kept so a reader
    never mistakes a cut frame list for the whole stack.

    MEASURED ON THE WIRE, NOT IN CHARACTERS. The budget is the smaller of
    MAX_BLOCKED_IN and what is left of MAX_ROW once every OTHER field of this
    row is encoded, and each code point is charged what `json.dumps` spends on
    it (ASCII escaping is per code point, so the costs add exactly). A row
    with no room left for even the marker keeps its measurement and drops
    only the location, to None -- the reader's "location unavailable"."""
    if not isinstance(where, str):
        return where
    room = min(MAX_BLOCKED_IN,
               MAX_ROW - len(_wire(dict(row, blocked_in=""))))
    cost = [len(json.dumps(c)) - 2 for c in where]
    if sum(cost) <= room:
        return where
    budget = room - 3
    if budget < 0:
        return None
    used = cut = 0
    for n in cost:
        if used + n > budget:
            break
        used += n
        cut += 1
    return where[:cut] + "..."


def mark(outcome):
    """Strictest observed outcome; semantic skips are not service latency."""
    if outcome not in OUTCOMES:
        return
    for span in _CURRENT.get():
        if OUTCOMES.index(outcome) > OUTCOMES.index(span.outcome):
            span.outcome = outcome


def blocked_in(where):
    """Record the frame a CONSUMED timeout was interrupted in.

    `_span` reads `exc.where` off a timeout that PROPAGATES through it, which
    is the only path that ever reached it. The dispatcher's own handler catch
    consumes the timeout inside those spans: they exit normally, nothing
    re-raises, and every END row it writes says UNKNOWN while the signal
    handler had already captured the frame. The catcher hands it over here
    instead, so the producer and the consumer of this field meet on both
    paths rather than on one.

    Every ancestor records the same place, for the same reason `_span` does:
    three timeout rows naming ONE frame are one incident, where three rows
    naming three stages read as three."""
    if not isinstance(where, str) or not where:
        return
    for span in _CURRENT.get():
        span.blocked_in = where


def bind(session=None, event=None):
    spans = _CURRENT.get()
    if spans:
        owner = spans[0].event
        owner.session = _identity(session)
        if type(event) is str and event in EVENTS:
            owner.event = event


@contextlib.contextmanager
def _span(event, stage):
    parents = _CURRENT.get()
    try:
        span = _Span(event, stage, parents[-1].id if parents else None)
    except Exception:
        event.drops += 1
        yield None
        return
    start = _clock(time.monotonic_ns)
    token = _CURRENT.set(parents + (span,))
    try:
        event.write(span, "START", start)
        failure = None
        try:
            yield span
        except BaseException as exc:
            failure = exc
            # The timeout owner supplies its type marker; never install/reset
            # an alarm here. Do not consume cancellation or change its identity.
            if isinstance(exc, SystemExit):
                try:
                    code = int(exc.code or 0)
                except (TypeError, ValueError):
                    mark("unchecked")
                else:
                    mark("nonzero" if code else "completed")
            else:
                timed_out = (span.outcome == "timeout"
                             or getattr(type(exc), "_hook_latency_timeout",
                                        False))
                if timed_out:
                    # DUCK-TYPED, so the observer never imports the
                    # dispatcher that owns the alarm. Every ancestor span
                    # records the same place, which is what makes the nesting
                    # legible: three timeout rows naming ONE frame are one
                    # incident, where three rows naming three stages read as
                    # three.
                    where = getattr(exc, "where", None)
                    if isinstance(where, str) and where:
                        span.blocked_in = where
                mark("timeout" if timed_out
                     else "unchecked" if span.outcome == "failed-open"
                     else "exception" if isinstance(exc, Exception) else "cancelled")
            raise
        finally:
            end = _clock(time.monotonic_ns)
            elapsed = ((end - start) / 1_000_000
                       if start is not None and end is not None and end >= start else None)
            # If another exception is already unwinding, ordinary logging errors
            # remain drops; BaseException is never swallowed by the logger.
            try:
                event.write(span, "END", end, elapsed)
            except BaseException as logging_failure:
                if failure is not None and not isinstance(failure, Exception):
                    # Keep an already-unwinding cancellation authoritative. A
                    # second cancellation remains its cause, never a success.
                    raise failure from logging_failure
                raise
    finally:
        _CURRENT.reset(token)


@contextlib.contextmanager
def event_scope(mode, event="PostToolUse", observer=OBSERVER):
    if active():
        yield
        return
    try:
        owner = _Event(mode, event, observer)
    except Exception:
        # No durable counter is possible without an event registration. The
        # report deliberately calls this population/loss unknown, not zero.
        yield
        return
    with _span(owner, "event"):
        yield


@contextlib.contextmanager
def stage(name):
    spans = _CURRENT.get()
    if not spans or name not in STAGES:
        yield
        return
    with _span(spans[0].event, name) as span:
        yield span


def remaining_budget():
    """Seconds left on THIS PROCESS's hook alarm, or None when none is armed.

    A FACT ABOUT THE PROCESS, not about how it was launched — the same shape
    as `hookalarm._under_a_test_runner`, and chosen for the same reason: a
    handler running in-process under `hookrun.run_one` has no parameter to
    read, and threading a deadline through every intermediate signature is how
    one of them comes to drop it silently. `hookrun` arms `ITIMER_REAL` with
    the handler's budget and this reads what is left of it, so the arm and the
    read cannot drift apart: whatever budget the wrapper owns, this returns the
    remainder of exactly that.

    None means UNBOUNDED, and every caller must treat it as such: a CLI run, a
    timer, a test. Returning a number there would invent a deadline for a
    process nobody is timing.
    """
    try:
        import signal
        left, _interval = signal.getitimer(signal.ITIMER_REAL)
    except Exception:                    # noqa: BLE001 — no SIGALRM, no budget
        return None
    return left if left > 0 else None


def flock(file, flags, name, fail_open=False, deadline=None):
    """Observe the existing acquisition call, never replace its flags/deadline.

    END logging runs after acquisition, before the caller's lock body. If it
    cancels there, release the acquired lock before propagating: the caller may
    not yet have entered its normal release finally. No paths enter telemetry.

    `deadline` (a `time.monotonic()` instant) is the CALLER'S, never this
    module's — the instrument still replaces nothing. It exists because a
    blocking `LOCK_EX` inside a bounded hook budget can only end one way when
    the fleet contends: MEASURED in production on this host, five consecutive
    PostToolUse delivery timeouts whose `blocked_in` frame was this very call,
    each having waited 1.6-1.9s of a 2s budget on the proxywatch delivery-state
    lock and then been killed with `event ALLOWED and UNCHECKED` printed to the
    owner. The hook did not fail and the store was not slow; it queued.

    With a deadline the wait becomes a poll and the answer becomes a VALUE:
    True when the lock is held, False when the deadline passed first. False is
    not an error — a caller whose work is idempotent and retried on the next
    tool boundary is strictly better off skipping than being killed — so the
    span records `skipped` and the caller decides. Without one, the behaviour
    is byte-for-byte what it has always been.
    """
    acquired = False
    try:
        with stage(name) as span:
            try:
                if deadline is None:
                    fcntl.flock(file, flags)
                else:
                    acquired = _poll_flock(file, flags, deadline)
                    if not acquired:
                        if span is not None:
                            # `skipped` AND NOT A NEW WORD. The first cut wrote
                            # "busy", which is not in `OUTCOMES` — so `_valid`
                            # rejected the row and the span STARTed and never
                            # ENDed: twelve starts, six ends, and the six
                            # acquisitions this change exists to make visible
                            # were the invisible ones. A new enum value is also
                            # a bug in every consumer's else; `skipped` is the
                            # established word for work that never ran and every
                            # reader already handles it.
                            span.outcome = "skipped"
                        return False
                acquired = True
                if span is not None:
                    span.outcome = "acquired"
            except OSError:
                if fail_open and span is not None:
                    span.outcome = "failed-open"
                raise
        return True
    except BaseException:
        if acquired:
            fcntl.flock(file, fcntl.LOCK_UN)
        raise


#: The backoff for a deadline-bounded acquisition. It starts far below any
#: plausible hold and caps well inside the shortest budget helm hands a hook,
#: so a lock that frees early is taken almost immediately and a contended one
#: costs a bounded number of wakeups rather than a spin.
_POLL_FIRST_S = 0.002
_POLL_CAP_S = 0.050


def _poll_flock(file, flags, deadline):
    """True once held, False at the deadline. LOCK_NB plus a capped backoff."""
    delay = _POLL_FIRST_S
    while True:
        try:
            fcntl.flock(file, flags | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            pass
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(delay, left))
        delay = min(delay * 2, _POLL_CAP_S)


def _snapshot(dest, generations=GENERATIONS, max_bytes=MAX_BYTES):
    """Fixed paths/byte ceilings; never create even a missing lock file."""
    coverage = dict(snapshot="uncertain", missing_generations=0,
                    unreadable_generations=0, oversized_generations=0,
                    retention="bounded-generations; historical coverage unknown")
    lock = None
    blobs = []
    try:
        try:
            lock = _open_regular(dest + ".lock", os.O_RDONLY)
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            coverage["snapshot"] = "coherent-for-cooperating-writers"
        except OSError:
            if lock is not None:
                os.close(lock)
                lock = None
        for n in range(generations):
            name = dest + ("." + str(n) if n else "")
            try:
                fd = _open_regular(name, os.O_RDONLY)
                try:
                    # Capture is bounded and under the shared lock; JSON parsing
                    # happens afterwards so a slow parser does not block writers.
                    with os.fdopen(fd, "rb", closefd=False) as stream:
                        blob = stream.read(max_bytes + 1)
                    if len(blob) > max_bytes:
                        coverage["oversized_generations"] += 1
                        blob = blob[:max_bytes]
                    blobs.append(blob)
                finally:
                    os.close(fd)
            except FileNotFoundError:
                coverage["missing_generations"] += 1
            except OSError:
                coverage["unreadable_generations"] += 1
    finally:
        if lock is not None:
            os.close(lock)
    return blobs, coverage


def _valid(row):
    try:
        return _valid_row(row)
    except (TypeError, ValueError, OverflowError):
        return False


def _valid_row(row):
    if not isinstance(row, dict) or "schema" not in row \
            or type(row["schema"]) is not int:
        return False
    keys = _KEYS_BY_SCHEMA.get(row["schema"])
    if keys is None or set(row) != keys:
        return False
    if row.get("blocked_in") is not None \
            and not isinstance(row["blocked_in"], str):
        return False
    if row["observer"] not in (OBSERVER, "python-runtime-entry-v1"):
        return False
    if any(_identity(row[k]) is None for k in ("writer", "event_id", "span_id")):
        return False
    if any(row[k] is not None and _identity(row[k]) is None
           for k in ("seat", "session", "parent_id")):
        return False
    if row["writer"] != row["event_id"] or row["mode"] not in ("standalone", "composite"):
        return False
    if row["event"] not in EVENTS or row["stage"] not in STAGES or row["kind"] not in ("START", "END"):
        return False
    if any(not _bounded_int(row[k]) for k in ("sequence", "observed_drops")):
        return False
    if any(row[k] is not None and not _bounded_int(row[k])
           for k in ("monotonic_ns", "time_ns")) or row["startup_ms"] is not None:
        return False
    wall = row["wall_ms"]
    if wall is not None and (type(wall) not in (float, int) or wall < 0
                             or wall > MAX_WALL_MS or not math.isfinite(wall)):
        return False
    if row["kind"] == "START":
        return row["outcome"] is None and wall is None
    return row["outcome"] in OUTCOMES


def _rank(values, fraction):
    return sorted(values)[math.ceil(len(values) * fraction) - 1] if values else None


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def report(dest=None, since=None, until=None):
    """The latency census, optionally bounded to a time window.

    THE WINDOW IS ON AN ABSOLUTE CLOCK AND THE CUT MUST SAY SO. Rows here
    carry `time_ns`, an absolute instant, while `probelog`'s files carry local
    wall-clock text — two stores a seat reads in the same sitting, on two
    clocks, with interchangeable-looking cut strings. `probelog.cut` refuses
    the mismatch in both directions rather than comparing them and returning a
    believable zero; a refusal rides back in `refused` and the counts are
    empty, never partial (task/2458).

    A WINDOW AND A SPAN ARE DIFFERENT UNITS, and the report says which
    counters absorb the difference rather than leaving a reader to guess. The
    filter is per ROW, so:

        START inside, END outside  ->  CENSORED, the same counter this census
                                       already uses for a run killed at a cut
        END inside, START outside  ->  `orphan_ends`

    `orphan_parents` counts a row whose PARENT span is missing, and A WINDOW
    CAN PRODUCE IT — `spans` is built from the rows that SURVIVED the filter,
    so a window that drops both halves of a parent while keeping a child makes
    that child's parent unresolvable. Saying a window never produces it would
    be the stronger and wrong claim.

    SO A SHORT WINDOW MAKES THE CENSUS LOOK WORSE THAN THE FLEET IS, and none
    of that is a reading about hooks: NOTHING about real termination or
    completion may be inferred from window censoring. The window is not
    widened to whole spans and a narrow one is not refused — either would
    answer a question the operator did not ask.
    """
    from . import probelog
    lo, refused = probelog.cut(since, probelog.CLOCK_ABSOLUTE)
    if not refused:
        hi, refused = probelog.cut(until, probelog.CLOCK_ABSOLUTE)
    if refused:
        return {"refused": refused, "populations": [], "counts": {},
                "coverage": {}}
    # NO SCALING HERE. `probelog.cut` returns NANOSECONDS for this clock, by
    # integer arithmetic, precisely so a caller cannot reintroduce a float
    # multiply on the boundary it was given.
    lo_ns, hi_ns = lo, hi
    blobs, coverage = _snapshot(path() if dest is None else dest)
    kept, kept_coverage = _snapshot(incidents_path(dest), RETAIN_GENERATIONS,
                                    RETAIN_BYTES)
    kept_stamps = []
    counts = dict(malformed=0, invalid=0, duplicates=0, conflicts=0,
                  orphan_ends=0, orphan_parents=0, sequence_conflicts=0,
                  censored=0, timeout=0, cancellation=0,
                  completed=0, matched=0, starts=0, ends=0)
    spans, writers, groups = {}, {}, {}
    blocked = {}                      # where -> {event_id: {span_id: parent_id}}
    stamps = []
    sequences = {}
    bad = set()
    # THE MAIN STREAM FIRST, THEN THE INCIDENTS, so a retained row the main
    # stream still holds is recognised as the same row and read once; only the
    # rows rotation already took arrive from the second stream.
    for retained, blob in [(False, b) for b in blobs] + [(True, b) for b in kept]:
        for wire in blob.splitlines(keepends=True):
            if not wire.strip():
                continue
            try:
                if len(wire) > MAX_ROW or not wire.endswith(b"\n"):
                    raise ValueError("incomplete or oversized row")
                row = json.loads(wire, object_pairs_hook=_object)
            except (ValueError, UnicodeError, RecursionError):
                counts["malformed"] += 1
                continue
            if not _valid(row):
                counts["invalid"] += 1
                continue
            if lo_ns is not None or hi_ns is not None:
                # OUT OF WINDOW IS NOT MALFORMED and not invalid: it is a row
                # this census was not asked about. A row with NO stamp cannot
                # be placed in any window, so a window excludes it rather than
                # admitting what it cannot date.
                when = row["time_ns"]
                if when is None \
                        or (lo_ns is not None and when < lo_ns) \
                        or (hi_ns is not None and when >= hi_ns):
                    continue
            key = (row["event_id"], row["span_id"])
            if retained:
                if row["time_ns"] is not None:
                    kept_stamps.append(row["time_ns"])
                if spans.get(key, {}).get(row["kind"]) == row:
                    continue
            seq = (row["writer"], row["sequence"])
            if seq in sequences and sequences[seq] != row:
                counts["sequence_conflicts"] += 1
                bad.add(key)
                bad.add((sequences[seq]["event_id"], sequences[seq]["span_id"]))
            sequences[seq] = row
            writers[row["writer"]] = max(writers.get(row["writer"], 0), row["observed_drops"])
            if row["time_ns"] is not None and not retained:
                stamps.append(row["time_ns"])
            pair = spans.setdefault(key, {})
            prior = pair.get(row["kind"])
            if prior is not None:
                counts["duplicates" if prior == row else "conflicts"] += 1
                if prior != row:
                    bad.add(key)
            else:
                pair[row["kind"]] = row

    for key, pair in spans.items():
        start, end = pair.get("START"), pair.get("END")
        row = end or start
        where = None
        counts["starts"] += bool(start)
        counts["ends"] += bool(end)
        outcome = end["outcome"] if end else "completion-unobserved"
        groupkey = (row["seat"], row["stage"], row["mode"], row["event"], row["observer"], outcome)
        g = groups.setdefault(groupkey, dict(completed=0, matched=0, timeout=0,
                              cancellation=0, censored=0, orphan_ends=0, orphan_parents=0,
                              invalid=0, values=[], writers=set()))
        g["writers"].add(row["writer"])
        if row["parent_id"] is not None and (row["event_id"], row["parent_id"]) not in spans:
            g["orphan_parents"] += 1
            counts["orphan_parents"] += 1
        if end and outcome in ("timeout", "cancelled"):
            counter = "timeout" if outcome == "timeout" else "cancellation"
            g[counter] += 1
            counts[counter] += 1
            # WHERE, KEYED BY EVENT so nesting collapses. One timed-out hook
            # writes a timeout END for every ancestor span, all naming the
            # same frame — counting rows would report one incident three
            # times, which is exactly how the per-stage tally misleads.
            # HELD, NOT PUBLISHED: the pair has not been checked yet. Two
            # individually schema-valid rows whose fixed fields disagree are
            # counted INVALID a few lines below, and taking their location
            # here put evidence into the incident that the same pass then
            # rejected — asserted to the reader through JSON and the CLI
            # with nothing marking it as discarded.
            claimed = end.get("blocked_in")
            if outcome == "timeout" and isinstance(claimed, str) and claimed:
                where = claimed
        if key in bad:
            g["invalid"] += 1
            counts["invalid"] += 1
            continue
        if start and end:
            fixed = ("event_id", "span_id", "parent_id", "stage", "mode", "observer", "seat")
            compatible = all(start[k] == end[k] for k in fixed)
            compatible &= start["session"] is None or start["session"] == end["session"]
            compatible &= start["event"] == end["event"] or start["event"] in PROVISIONAL
            a, b = start["monotonic_ns"], end["monotonic_ns"]
            valid_clock = (a is not None and b is not None and b >= a and
                           end["wall_ms"] == (b - a) / 1_000_000)
            if not compatible or not valid_clock or end["sequence"] <= start["sequence"]:
                g["invalid"] += 1
                counts["invalid"] += 1
                continue
            g["matched"] += 1
            counts["matched"] += 1
            if outcome in ("completed", "nonzero", "acquired"):
                g["values"].append(end["wall_ms"])
                g["completed"] += 1
                counts["completed"] += 1
        elif start:
            g["censored"] += 1
            counts["censored"] += 1
        else:
            g["orphan_ends"] += 1
            counts["orphan_ends"] += 1
        # PUBLISHED ONLY BY A PAIR THAT SURVIVED. Every refusal above leaves
        # through `continue`, so a location reaches the incident exactly when
        # its row was not counted invalid. An unmatched timeout END still
        # publishes: nothing about it was found contradictory, it is counted
        # as an orphan rather than rejected, and the frame it names is a real
        # observation of where that hook was blocked.
        if where:
            blocked.setdefault(where, {}).setdefault(
                row["event_id"], {})[row["span_id"]] = row["parent_id"]
    populations = []
    for key, g in sorted(groups.items(), key=lambda item: tuple(v or "" for v in item[0])):
        values = g.pop("values")
        g["event_observed_drops_lower_bound"] = sum(writers[w] for w in g.pop("writers"))
        g["unreported_loss"] = "unknown"
        populations.append(dict(zip(("seat", "stage", "mode", "event", "observer", "outcome"), key),
                                **g, p50_ms=_rank(values, .5), p95_ms=_rank(values, .95)))
    coverage.update(observed_drops_lower_bound=sum(writers.values()),
                    unreported_loss="unknown", launch_attempts=None,
                    pre_marker_population="unobserved", startup_ms=None,
                    total_event_denominator="in-process registered spans only; unmatched launches unknown",
                    retained_time_range_hours=(max(stamps) - min(stamps)) / 3.6e12 if stamps else None,
                    incidents=dict(kept_coverage, outcomes=sorted(RETAINED),
                                   bytes_per_generation=RETAIN_BYTES,
                                   generations=RETAIN_GENERATIONS,
                                   retained_time_range_hours=(
                                       (max(kept_stamps) - min(kept_stamps)) / 3.6e12
                                       if kept_stamps else None),
                                   scope="spans that ENDED timeout or cancelled, "
                                         "with their START; a process killed from "
                                         "outside writes no END and is not here"),
                    budget_decision="not authorized by this report; 24h live rows AND adequate coverage required",
                    lock_wait="nonblocking", filesystem_wall_bound=None,
                    numeric_schema="timestamps/counters: integers 0..2**63-1; wall_ms: finite 0..(2**63-1)/1e6; reject, never clamp",
                    identity="seat: declared home.chat_name seam, not admission; session: parsed payload only",
                    quantiles="nearest-rank ceil(p*n); conditional completed/nonzero/acquired spans only; empty is null",
                    count_unit="spans, not launches; event outcomes aggregate worst observed descendants, not outer-shell exit status",
                    nested_spans="overlap; do not sum percentiles or per-population event drop lower bounds",
                    boundaries={"event": "named observer entry through return/unwind, excludes interpreter startup",
                                "record": "standalone CLI dispatch or composite run_one including timer/stream cleanup",
                                "delivery": "standalone CLI dispatch or composite run_one including receipt/latch and cleanup",
                                "prepare": "composite run_one including timer/stream cleanup",
                                "input": "composite stdin acquisition when requested",
                                "record-registry": "bounded recorder declaration lookup",
                                "delivery-registry": "bounded delivery declaration lookup",
                                "lock-seats": "seats_common flock acquisition only, excludes file open/body",
                                "lock-proxywatch": "delivery_state_guard flock acquisition only, excludes file open/body",
                                "lock-whisper": "pair latch flock acquisition only, excludes file open/body",
                                "lock-room": "room acquisition including existing bounded poll loop, excludes file open/body",
                                "lock-send": "chat node send lock acquisition including its bounded poll, excludes file open/body",
                                **{name: "standalone CLI dispatch of the %s hook entry, from the observer marker in cli.main" % name
                                   for name in HANDLER_STAGES}},
                    logger_overhead="START append/scheduling included in span wall time; END append excluded from its own span",
                    local_helm_web="unavailable: no installed hook call path to local web client established",
                    uninstrumented="other lock owners, filesystem calls, node/provider HTTP, imports before marker; no complete bottleneck attribution",
                    historical_census_complete=False)
    event_rows = [g for g in populations if g["stage"] == "event"]
    coverage["registered_events"] = sum(1 for pair in spans.values()
                                        if pair.get("START", {}).get("stage") == "event")
    coverage["matched_event_ends"] = sum(g["matched"] for g in event_rows)
    return dict(schema=1, coverage=coverage, counts=counts,
                populations=populations,
                # EVENTS, NOT ROWS — see the comment at the collection site.
                blocked_in=sorted(
                    ({"where": w, "events": len(e), "timeouts": _leaves(e)}
                     for w, e in blocked.items()),
                    key=lambda b: (-b["timeouts"], -b["events"], b["where"])))


def _leaves(events):
    """How many deadlines expired at one place: the INNERMOST timed-out spans.

    AN INCIDENT IS A DEADLINE, NOT AN EVENT (task/2462). One
    timeout writes a timeout END for its span and for every ancestor, all
    naming the same frame, so counting rows triples one incident -- which is
    why this was keyed by event. But two handlers in ONE event can each
    exhaust their own budget in the same shared frame, and an event key
    reports that as one. A span whose child also timed out here is an
    ancestor echo; a span no timed-out child names as parent is a deadline.
    Stated limit: a parent span that times out AGAIN, itself, after a child
    already timed out at the same frame is counted once, because the row
    schema records where a span was blocked and not how many times."""
    total = 0
    for spans in events.values():
        parents = set(spans.values())
        total += sum(1 for span in spans if span not in parents)
    return total


def cmd(args):
    usage = ("usage: helm hooks latency [--json] [--since T] [--until T]\n"
             "  T is an ABSOLUTE instant: an ISO-8601 timestamp with a zone "
             "(a trailing Z, or an offset). These rows record absolute "
             "instants, so an unzoned cut is REFUSED rather than guessed at.")
    argv, want_json, since, until = list(args), False, None, None
    while argv:
        head = argv.pop(0)
        if head in ("--help", "-h"):
            print(usage)
            return 0
        if head == "--json":
            want_json = True
        elif head in ("--since", "--until"):
            if not argv:
                print("%s needs a timestamp\n%s" % (head, usage),
                      file=sys.stderr)
                return 2
            value = argv.pop(0)
            if head == "--since":
                since = value
            else:
                until = value
        else:
            print(usage, file=sys.stderr)
            return 2
    result = report(since=since, until=until)
    # A REFUSED CUT IS NOT AN EMPTY CENSUS. Printing zero populations at rc 0
    # is exactly the believable zero this window machinery exists to stop.
    if result.get("refused"):
        print("helm hooks latency: REFUSED — %s" % result["refused"],
              file=sys.stderr)
        return 2
    if want_json:
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    if since or until:
        print("Window %s .. %s — a span cut by a boundary is NOT completed: "
              "START inside and END outside counts as CENSORED, END inside as "
              "an orphan end. Neither says anything about real termination."
              % (since or "(open)", until or "(open)"))
    print("Hook latency — in-process only; startup_ms unavailable; launch census unobserved")
    print("Nearest-rank ceil(p*n), conditional completed/nonzero/acquired spans; empty != zero; nested spans overlap.")
    print("seat stage mode event observer outcome completed p50_ms p95_ms timeout cancellation censored orphan invalid event_drop_lb")
    for g in result["populations"]:
        print(" ".join(str(g[k]) if g[k] is not None else "unavailable" for k in
              ("seat", "stage", "mode", "event", "observer", "outcome", "completed", "p50_ms", "p95_ms",
               "timeout", "cancellation", "censored", "orphan_ends", "invalid", "event_observed_drops_lower_bound")))
    # THE FIELD MUST REACH A SURFACE OR IT IS COMPUTED AND DISCARDED. The
    # table is per-population and this fact is per-incident, so it gets its
    # own block rather than a column that would not fit.
    if result.get("blocked_in"):
        print("Timed out IN (innermost frame first; one line per distinct "
              "place, counted by EXPIRED DEADLINE not by span row):")
        for b in result["blocked_in"]:
            print("  %d timeout%s in %d event%s  %s"
                  % (b["timeouts"], "s"[:b["timeouts"] != 1],
                     b["events"], "s"[:b["events"] != 1], b["where"]))
    elif result["counts"].get("timeout"):
        print("Timed out IN: UNKNOWN for every timeout in this window — the "
              "rows predate the blocked_in field (schema 1) or the alarm "
              "could not read its own frame.")
    held = (result["coverage"].get("incidents") or {}).get("retained_time_range_hours")
    print("Incidents (timeout and cancelled spans) are kept past rotation in a "
          "second stream; it holds %s. A byte bound, not a clock."
          % ("%.1f h" % held if held is not None else "none yet"))
    print("Counts: " + json.dumps(result["counts"], sort_keys=True))
    print("Coverage: " + json.dumps(result["coverage"], sort_keys=True))
    return 0
