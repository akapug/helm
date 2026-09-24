#!/usr/bin/env python3
"""THE GUARD RECORDS ITS OWN LADDER, BECAUSE THE TRANSCRIPT IS NOT AN
INSTRUMENT AND READING IT AS ONE COST A NIGHT.

The Stop guard's rung timings existed only as hook stderr, which lands in an
agent transcript. Deriving a distribution from there is not measurement: the
same marker appears in test output, in commands typed while investigating and
in files opened to look at it, so the population is decided by a text match
rather than by provenance. Three separate re-derivations of one night's
numbers were published and withdrawn before the population was right, and the
corpus GREW between two of them, so the correction and the drift could not be
told apart in a total. A log the guard writes has none of those properties:
one writer, one grammar, and a line means an event happened.

WHAT IT RECORDS, AND WHEN IT SAYS NOTHING. A healthy ladder writes nothing at
all. Recording starts only once a run's total passes SLOW, so the ordinary
stop -- the median is a few seconds -- pays no write, and the file contains
only the runs anybody would want to read about.

WHY PER-BOUNDARY AND NOT ONE SUMMARY AT THE END. The interesting run is the
one that never reaches its end: the outer timeout kills the guard mid-rung,
and a process being killed cannot write a summary of what it was doing. Each
boundary appends and flushes, so the LAST LINE IN THE FILE NAMES THE LAST
RUNG SEEN TO FINISH -- and the ladder went quiet in the one AFTER it, which is
as close as any file can get to the question no post-hoc probe can answer at
all. A boundary is written when a rung ENDS, so the rung actually running when
the axe fell wrote nothing and this instrument must not pretend to name it.

THE GRAMMAR IS THE SIBLING'S. hookprobe.log records `<iso> EVENT pid=<n>` and
then pipe-separated key=value; anything reading one file can read the other.

AND IT SHIPS ITS READER IN THE SAME BREATH. The instrument beside this one
held 359 unread records for thirteen days while the question they answered
stayed open, so a writer without a reader is a defect this module refuses to
repeat: `read` and `findings` are the point of the file, not an accessory.
"""
import os
import time

from . import probelog
from .procage import process_age

def log_path():
    """THE PATH IS RESOLVED PER CALL AND ANCHORED ON helm's OWN HOME.

    IT WAS A MODULE CONSTANT BUILT FROM `~`, AND THAT IS HOW THIS INSTRUMENT
    CAME TO CORRUPT THE ONE POPULATION IT EXISTS TO KEEP HONEST. Every helm
    test is hermetic by setting HELM_HOME to a temp directory, and every
    sibling module reaches its files through `home.helm_home()`, so that
    isolation reaches them. This module expanded `~` directly and therefore
    could not see HELM_HOME at all — so a suite driving a Stop ladder wrote
    into the REAL log, under fixture seat names, and the file that was supposed
    to be the trustworthy alternative to a text-matched transcript filled up
    with the same class of contamination that motivated it: measured on the
    live file, 532 of 2586 records were fixtures.

    RESOLVED PER CALL, not once at import, because a constant computed at
    import time is decided by whoever imported this module first — which in a
    test run is the harness, before any fixture has set anything.

    The explicit override stays, for the one producer that needs to write a
    REAL record somewhere other than production: the live proof that a real
    Stop writes a real line."""
    from . import home
    override = os.environ.get("HELM_STOPPROBE_LOG")
    if override:
        return override
    return os.path.join(home.helm_home(), "helm", "pause-ops",
                        "stopprobe.log")

_DEFAULT_SLOW = 3.0


def threshold_from_env(raw, default):
    """A MALFORMED KNOB IS NOT A REASON TO LOSE THE REPORT.

    This module is imported by a doctor rung, and the rung's own guard cannot
    catch an exception raised at IMPORT time — a stray `HELM_STOPPROBE_SLOW=x`
    would take the whole report down before any try block existed. A value
    that is not a finite number is ignored and the default stands.

    SHARED WITH THE STREAMING THRESHOLD in seats_stop_timing, which reads a
    seconds knob under exactly the same rule. One parser, because two would
    disagree about a malformed value on the day it mattered — and the arms that
    pin "ignored, default stands" would then be true of only one of them.
    """
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return default
    return value


# A LADDER UNDER THIS IS NOT NEWS. Recording every stop would bury the tail in
# the median and pay a write on turns that are working correctly.
SLOW = threshold_from_env(os.environ.get("HELM_STOPPROBE_SLOW"),
                          _DEFAULT_SLOW)

RUNG = "STOP-RUNG"     # a boundary crossed by a run that is already slow
END = "STOP-END"       # a slow run that reached its own end
MALFORMED = "STOP-BAD"  # OUR OWN line, torn or truncated: never dropped
_ORDER = (RUNG, END)


def new_run_id():
    """A PID IS NOT A RUN. Pids are reused, so a lifetime log grouped by
    pid folds an old finished ladder together with a new unfinished one
    and the new one's incompleteness disappears. The ladder mints its own
    identity once, at the moment it starts, and every record carries it."""
    try:
        entropy = os.urandom(6).hex()
    except (OSError, NotImplementedError):
        # A HOOK MAY NOT DIE FOR WANT OF RANDOMNESS. The monotonic clock is a
        # weaker uniqueness source and it is enough here: identity only has to
        # separate ladders within one log, not resist an adversary.
        entropy = "%012x" % (int(time.monotonic() * 1e6) & 0xFFFFFFFFFFFF)
    return "%d-%s" % (os.getpid(), entropy)


def _seat(known=None):
    """This module's seat, resolved by the shared instrument seam.

    The laundering rule and the reason for it live with `probelog.seat`,
    because every instrument that writes this grammar needs the same one:
    a log file is a sink somebody later greps, so the name goes through
    home.chat_name's validation rather than a raw environ read.
    """
    return probelog.seat(known)

def _write(event, fields, log=None, seat=None, began=None):
    """Append one line. NEVER RAISES: this runs inside a hook, and an
    instrument that can take the guard down is worse than no instrument.

    AND IT NEVER WRITES WHAT ITS OWN READER WOULD REFUSE. A blank run id
    wrote successfully and then came back MALFORMED, so the writer reported
    a record it had already made unreadable — a success that is a lie about
    the file. Anything the parser requires is required here.
    """
    path = log or log_path()
    # THE LADDER'S START AGE IS MEASURED BY ITS OWNER, NOT SAMPLED HERE, and
    # the first cut of this module got that wrong in a way its own tests could
    # not see. It stamped process_age() at WRITE time and let a reader compute
    # `age - total`. Those are two clocks read at two different instants: the
    # caller freezes `total` at the rung boundary and then writes to stderr,
    # which can block, so any reporting delay lands in the subtraction and is
    # reported as time the process spent BEFORE the ladder. A five-second
    # stderr stall turned a ladder that began 0.1s into its process into one
    # that appeared to begin 5.1s in -- a fabricated pre-ladder delay in the
    # exact field added to detect pre-ladder delay. Found by a source read;
    # no arm of mine could have caught it, because every arm wrote and
    # read in the same uncontended instant.
    #
    # SO NOTHING IS SUBTRACTED ANY MORE. The owner measures the age ONCE, beside
    # the monotonic origin it belongs to, and every record carries that one
    # number. A constant cannot drift, and one /proc read per LADDER replaces
    # one per record.
    #
    # IT RIDES EVERY RECORD, NOT ONLY THE FIRST, because this instrument exists
    # for the run that never reaches its end: the axe falls mid-ladder and
    # whatever line survived is the one that has to answer. Absent when
    # unknowable, never zero -- zero is the strongest claim the field can make
    # ("the ladder is the whole life of this process") and may not stand for
    # "cannot tell".
    stamped = list(fields)
    if began is not None:
        stamped.append(("began", "%.3f" % began))
    return probelog.write(path, event, stamped,
                          required=_REQUIRED[event], known_seat=seat)


def record(run, rung, elapsed, total, log=None, slow=None, seat=None,
           began=None):
    """One rung boundary of a run that has already gone slow.

    Returns True when a line was written, False when the run is still fast --
    a caller can assert the SILENCE as easily as the record, which is what
    keeps `writes nothing on a healthy ladder` a tested property rather than
    a claim in a docstring.
    """
    if total < (SLOW if slow is None else slow):
        return False
    return _write(RUNG, (("run", run), ("rung", rung),
                         ("elapsed", "%.3f" % elapsed),
                         ("total", "%.3f" % total)),
                  log=log, seat=seat, began=began)


def record_end(run, rung, total, log=None, slow=None, seat=None,
               began=None):
    """THE LADDER REACHED ITS OWN END, and only its owner may say so.

    This is called from the lifecycle's terminal, never inferred from a rung
    that happened not to name a successor. A guard that ends one rung and
    then continues into another under the SAME run would otherwise stamp the
    ladder complete mid-flight, and a later rung that stalled would be hidden
    behind that stamp -- a record claiming exactly the completion this
    instrument exists to doubt.
    """
    if total < (SLOW if slow is None else slow):
        return False
    return _write(END, (("run", run), ("rung", rung),
                        ("total", "%.3f" % total)),
                  log=log, seat=seat, began=began)


_REQUIRED = {RUNG: ("run", "rung", "elapsed", "total"),
             END: ("run", "rung", "total")}


_NUMERIC = ("elapsed", "total", "began")


def _parse(line):
    """-> a row, a MALFORMED row, or None for a line we did not write.

    The three-outcome rule, the reserved header keys, the repeated-key
    verdict and the duration validation are `probelog.parse`; what belongs
    to this instrument is WHICH events are ours, what each one must carry,
    and which of its fields are durations.
    """
    return probelog.parse(line, _ORDER, _REQUIRED, _NUMERIC, MALFORMED)


def read(log=None, since=None, until=None):
    """-> (rows, err). `err` is a sentence when the log could not be READ, or
    when a time cut was given on the wrong clock.

    A MISSING FILE IS NOT AN EMPTY ONE. No stop has gone slow since the
    instrument landed, and the instrument was never wired, produce the same
    zero rows; only the error channel can separate them, so absence of the
    file is reported and never rendered as a clean bill. The sentence is
    THIS instrument's, because only this instrument knows what its own
    silence would have meant.
    """
    path = log or log_path()
    # THESE ROWS ARE STAMPED LOCAL, so a cut against them is local — see
    # `probelog.cut`, which refuses a zone-marked cut here rather than
    # comparing two clocks and returning a believable zero (task/2458).
    return probelog.read(
        path, _ORDER, _REQUIRED, _NUMERIC, MALFORMED,
        absent="no stop-timing log at %s — UNMEASURED, not quiet" % path,
        since=since, until=until)


def runs(rows):
    """Group rows into ladders by the identity the LADDER minted.

    A run is UNFINISHED when it has boundaries and no end. That is a fact
    about what was OBSERVED, not about what happened: the run may have been
    killed, may still be going, or may have failed to append its end.

    `last` IS THE LAST RUNG THAT COMPLETED, NOT THE ONE THAT WAS BURNING.
    A boundary is written when a rung ENDS, so the rung that was actually
    running when a ladder stopped wrote nothing and cannot be named from
    this file. Calling `last` the burning rung claimed an observation the
    instrument never made; it is the last one we SAW FINISH, and the next
    one is where the ladder went quiet.
    """
    order = []
    seen = {}
    for row in rows:
        if row["event"] == MALFORMED:
            continue
        key = row.get("run") or ("legacy", row.get("pid"), row.get("seat"))
        if key not in seen:
            seen[key] = {"run": row.get("run", ""), "pid": row.get("pid"),
                         "seat": row.get("seat"), "rungs": [],
                         "ended": False, "total": 0.0, "last": "",
                         "began_at_age": None}
            order.append(seen[key])
        run = seen[key]
        try:
            run["total"] = max(run["total"], float(row.get("total", 0.0)))
        except (TypeError, ValueError):
            pass
        # HOW OLD THE PROCESS WAS WHEN THE LADDER BEGAN, which is the field
        # that separates two runs the total cannot tell apart. A ladder whose
        # total is 10.3s inside a process 10.4s old IS the process: that is a
        # slow ladder. The same 10.3s inside a process 60s old means the
        # ladder started 50s in, and whatever the guard was doing for those
        # 50s is a different bug. READ, NEVER DERIVED: an earlier cut computed
        # it as age-minus-total from two clocks sampled at different instants,
        # so any delay between freezing the total and writing the line was
        # reported as pre-ladder time that never happened.
        if row.get("began") is not None:
            try:
                run["began_at_age"] = float(row["began"])
            except (TypeError, ValueError):
                pass
        if row["event"] == END:
            run["ended"] = True
        else:
            run["rungs"].append((row.get("rung", ""), row.get("elapsed", "")))
        run["last"] = row.get("rung", "")
    return order


def malformed(rows):
    """OUR OWN records that arrived torn. Never silently dropped."""
    return [r for r in rows if r["event"] == MALFORMED]


def overruns(rows, fitted):
    """-> {rung: (worst elapsed, how many samples beat its pin)}.

    A COST THAT LIVES IN A CONSTANT CAN BE FALSIFIED BY THE LOG THE LADDER
    ITSELF WRITES, and that is the whole reason to keep it in a constant. The
    Stop budget admits a fat-tail rung by PREDICTING what it costs: deadlines
    there are cooperative, so nothing interrupts a running rung, and the
    admission check is the only thing standing between a slow rung and its
    successors' share of the budget. A prediction that drifts therefore does
    not degrade the guard, it switches the guard off — and it does so
    silently, because a Stop hook that fails open reports a PASS either way.
    One fitted cost on this ladder stood at a twelfth of its true cost, in
    prose, until an outage was traced back to it.

    ONE-DIRECTIONAL, AND THE ASYMMETRY IS NOT A LIMITATION TO APOLOGISE FOR.
    This population is censored twice over: a ladder writes nothing until its
    total passes SLOW, and a rung that overruns its local reserve is cut at
    the reserve rather than allowed to run to its natural length. So the
    distribution here is not the rung's distribution and no percentile of it
    would mean anything. A single sample ABOVE the pin is still a true
    falsification — censoring can only ever hide cost, never invent it — so
    this answers the falsified direction and nothing else. An empty answer
    means no run in the retained window was caught exceeding its pin; it is
    not a clean bill, and a caller that renders it as one is wrong.
    """
    worst = {}
    for run in runs(rows):
        for rung, elapsed in run["rungs"]:
            pin = fitted.get(rung)
            if pin is None:
                continue
            try:
                spent = float(elapsed)
            except (TypeError, ValueError):
                continue          # a torn duration is `malformed`'s to report
            if spent <= pin:
                continue
            seen, count = worst.get(rung, (0.0, 0))
            worst[rung] = (max(seen, spent), count + 1)
    return worst


def findings(rows):
    """-> (unfinished, by_rung). Runs whose end was never observed, and the
    last rung each one reported."""
    unfinished = [r for r in runs(rows) if r["rungs"] and not r["ended"]]
    by_rung = {}
    for run in unfinished:
        by_rung[run["last"]] = by_rung.get(run["last"], 0) + 1
    return unfinished, by_rung
