#!/usr/bin/env python3
"""task/3382 item 9: the findings pass re-reads its row before each read.

THE DEFECT, MEASURED OVER 24 h on the reader's server: 19 of 137 passes read
a row that had meanwhile reached a verdict or been cancelled, and the 14 of
them the census could match spent 2,248 s of local GPU on reads nobody was
left to adjudicate. The pass looked at the row ONCE, when it took the queue
lock, and never again while the reading script worked through the files.

THE CURE, one arm or more per clause:

  * before each read the pass asks the question it asks at the lock, can the
    row still take the note (`findingspass.standing`: its status is open or
    held and it is not retired), and stops on a row that never can again: a
    verdict, a cancel, a close or a retirement;
  * a held row, a row a successor carries and a row retipped to another tip
    are read to their end and noted, or recorded, as they always were;
  * a stop keeps every read that finished, stored by reference, and leaves
    one line — `stopped: row reached <state> at <ts>` — that triage prints
    under the named row;
  * a ledger that cannot be read is UNKNOWN: the pass goes on and its note
    says so, never "no longer owed";
  * a change after the last read stops nothing: the pass writes its note as
    it does today;
  * no queue is ever lost, and a row and tip are read once: every queue
    starts its worker, the worker waits for the queue lock as every pass
    always has and reads the tip its row names at that lock, so a worker
    that lives covers one that died before its lock, and a second pass of
    that row and tip finds the first one's complete or partial read there;
  * the dispatch-ledger write path runs no findings-pass code.

THE SURFACE x STATE MATRIX. The state is one of {still pending, verdict,
cancelled, closed, held, superseded, retipped, ledger unreadable}; the check
runs before the first read or between reads. Each cell drives a FIXTURE
local-review.py that emits the real script's shapes one read at a time, each
read held on a gate file the arm opens only after the check that followed the
previous read has run, so which check saw which state is deterministic and
never a race on the box's speed.
"""
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import dispatches, eventledger, findingspass, foldckpt  # noqa: E402
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_findingspass as _fp  # noqa: E402

READER = findingspass.READER
OWED, ENDED, UNKNOWN = findingspass.OWED, findingspass.ENDED, \
    findingspass.UNKNOWN
#: The one line a complete read of the fixture's three files lands.
CLEAN_LINE = ("%s: no findings (complete, 3 reads). Not a review, not an "
              "approval." % READER)

#: A local-review.py that emits the real script's shapes (a header, one
#: section per read, the tail and its status line), each step held on a gate
#: file named in its config, and records what it did. Its config sits beside
#: it as JSON, so nothing reaches it through the environment the suite shares.
STREAM = r'''import json, os, sys, time
argv = sys.argv[1:]
with open(__file__ + ".json", encoding="utf-8") as f:
    CONF = json.load(f)
out = []


def note(word):
    with open(CONF["record"], "a", encoding="utf-8") as f:
        f.write("%s %d\n" % (word, os.getpid()))


def gate(point):
    # HELD, NOT TIMED: the arm opens each gate. The cap and the vanished
    # home only free a straggler.
    path = CONF["gates"].get(point)
    cap = time.monotonic() + 120
    while path and not os.path.exists(path) \
            and os.path.isdir(os.path.dirname(path)) \
            and time.monotonic() < cap:
        time.sleep(0.01)


def emit(text):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()
    out.append(text)


reads = CONF["reads"]
note("start")
gate("header")
emit("# Local review of abcdef012345\n\n- files in diff: %d\n" % reads)
for i in range(1, reads + 1):
    note("read-start-%d" % i)
    gate("read-%d" % i)
    head = ("\n## helm/f%d.py\n\n`prompt 1200 tok, prefill 900 tok/s, decode "
            "20.0 tok/s, 12s`\n" % i)
    if i in CONF.get("split", ()):
        # ONE FINISHED READ IN TWO WRITES, the first ending in a newline: a
        # long answer a pipe read splits looks exactly like this
        sys.stdout.write(head)
        sys.stdout.flush()
        gate("rest-%d" % i)
        emit("NONE FOUND in the rest of read %d\n" % i)
        out[-1] = head + out[-1]
    else:
        emit(head + "NONE FOUND\n")
    note("read-end-%d" % i)
gate("tail")
emit("\n---\n\nMEASURED: %d reads, 36s total wall, largest prompt 1200 "
     "tokens.\n\nNo file was truncated; every file was read whole.\n\n"
     "LOCAL-REVIEW-STATUS complete reads=%d errors=0 empty=0 cut=0 "
     "truncated=0 kept=0 rejected=0 unjudged=0 verify_rejected=0"
     % (reads, reads))
if "--out" in argv:
    with open(argv[argv.index("--out") + 1], "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
note("exit")
sys.exit(0)
'''

#: The fixture's steps in order. Every step but the tail is followed by one
#: check; the tail ends the reads and is followed by none.
STEPS = ("header", "read-1", "read-2", "read-3", "tail")
#: Where each position's change lands: before the step named here opens.
#: "before the first read" changes the row after the script started and
#: before its header, so the lock-time check has already passed and the
#: header's check is the first to see it. "between reads" changes it while
#: read 2 is in flight, after the check that followed read 1. "after the last
#: read" changes it after the check that followed read 3.
BEFORE_FIRST, BETWEEN, AFTER_LAST = "header", "read-2", "tail"


class RecheckBase(_fp.FindingsBase):
    """A FindingsBase home with a gated streaming fixture and a spy on the
    re-read, so each arm knows which check saw which state."""

    BOUND = 120
    #: How long an arm lets a pass that must be WAITING go on waiting before
    #: it reads that the pass has not moved: a pass that did not wait moves
    #: within milliseconds, so this only has to be longer than that.
    SETTLE = 0.5
    #: The worker thread's name, so a spy can tell the pass's own work from
    #: the arm's reads and writes on the test thread.
    WORKER = "findings-pass-worker"
    #: The keys an arm points at its own fixture, switch or bound, restored
    #: after the arm by name: a whole-environment patch would restore
    #: HELM_HOME and every other key to their in-arm values after tearDown
    #: put them back.
    POINTED = ("HELM_LOCAL_REVIEW_SCRIPT", "HELM_QWEN27_FINDINGS",
               "HELM_QWEN27_FINDINGS_TIMEOUT_S")

    def setUp(self):
        super().setUp()
        prior = {key: os.environ.get(key) for key in self.POINTED}

        def restore():
            for key, value in prior.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(restore)
        self.checks = []
        #: Where the pass is: None before its first check, "checks" from it.
        self.phase = None
        real = findingspass.row_now

        def spy(*args, **kwargs):
            self.phase = "checks"
            got = real(*args, **kwargs)
            self.checks.append(got)
            return got
        patcher = mock.patch.object(findingspass, "row_now", spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -------------------------------------------------------------- fixture

    def stream(self, name, split=()):
        """One cell's fixture: its script, record and gates, in their own
        directory, and the pass pointed at it. A read named in `split` is
        written in two parts, the second held on its own `rest-N` gate."""
        base = os.path.join(self.tmp, name)
        os.makedirs(base)
        gates = {step: os.path.join(base, "gate-" + step)
                 for step in STEPS + tuple("rest-%d" % i for i in split)}
        record = os.path.join(base, "record")
        script = os.path.join(base, "local-review.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(STREAM)
        with open(script + ".json", "w", encoding="utf-8") as f:
            json.dump({"record": record, "gates": gates, "reads": 3,
                       "split": list(split)}, f)
        os.environ["HELM_LOCAL_REVIEW_SCRIPT"] = script
        # a cell that fails part-way still frees its fixture (and the home
        # going away at tearDown frees it too)
        self.addCleanup(self.free, gates)
        return gates, record

    @staticmethod
    def open_gate(path):
        with open(path, "a", encoding="utf-8"):
            pass

    def free(self, gates):
        for path in gates.values():
            try:
                self.open_gate(path)
            except OSError:
                pass

    def lines(self, record):
        """The fixture's record as (word, pid) pairs, in the order written."""
        try:
            with open(record, encoding="utf-8") as f:
                return [tuple(ln.split()[:2]) for ln in f if ln.endswith("\n")]
        except FileNotFoundError:
            return []

    def words(self, record):
        return [word for word, _pid in self.lines(record)]

    def wait(self, test, what):
        deadline = time.monotonic() + self.BOUND
        while not test():
            if time.monotonic() > deadline:
                self.fail("waited %d s for %s; checks %r"
                          % (self.BOUND, what, self.checks))
            time.sleep(0.01)

    @staticmethod
    def on(_rid=None):
        """The switch on, now: the filing before it started nothing."""
        os.environ["HELM_QWEN27_FINDINGS"] = "on"

    def lock_spy(self):
        """Every queue-lock step each thread takes, in order, as (step,
        thread name): lock-asked and lock-held around the queue lock."""
        events = []
        real_lock = findingspass._lock

        def lock():
            events.append(("lock-asked", threading.current_thread().name))
            fd = real_lock()
            events.append(("lock-held", threading.current_thread().name))
            return fd
        patcher = mock.patch.object(findingspass, "_lock", lock)
        patcher.start()
        self.addCleanup(patcher.stop)
        return events

    # ----------------------------------------------------------------- run

    def drift(self):
        """THE TREE CHANGED UNDER THE PASS: from here this process cannot
        name its code (`foldckpt.policy()` answers None for the rest of the
        process, as it does once any helm/*.py changes on disk), so every
        fold it takes is a whole cold fold with no checkpoint."""
        patcher = mock.patch.dict(foldckpt._POLICY, {"drifted": True})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertIsNone(foldckpt.policy(), "the drift did not take, so "
                          "this arm would measure a process that still "
                          "names its code")

    def drive(self, name, at=None, change=None, kind="review", ref=None,
              split=(), after_end=None, hold=None, drifted=False):
        """One pass over a fresh row, run as the worker runs it, released
        one step at a time. `change` runs before the step `at`
        opens and may return an undo, which runs once the next check has
        been taken; `after_end(gates)` runs once a check has ENDED the pass.
        The step `hold` names is never opened, so the pass waits there until
        its bound. `drifted` changes the tree under the pass once its
        lock-time check has run. Returns the row id, the pass's (row, why),
        the fixture's record words, and what `change` returned."""
        gates, record = self.stream(name, split)
        tip = ref or self.side
        row = self.dispatch(ref=tip, lane="lane/" + name, kind=kind)
        rid = row["id"]
        del self.checks[:]
        result = {}
        worker = threading.Thread(
            target=lambda: result.update(out=findingspass.run(rid)),
            daemon=True, name=self.WORKER)
        worker.start()
        # the lock-time check has run once the script has started
        self.wait(lambda: "start" in self.words(record) or not worker.is_alive(),
                  "the fixture to start")
        if drifted:
            self.drift()
        changed = undo = None
        for n, step in enumerate(STEPS):
            if not worker.is_alive():
                break
            if step == at:
                changed = change(rid)
                if callable(changed):
                    undo, changed = changed, None
            if step == hold:
                break
            self.open_gate(gates[step])
            if step == "tail":
                break
            self.wait(lambda: len(self.checks) > n or not worker.is_alive(),
                      "the check after %s" % step)
            if undo is not None:
                undo()
                undo = None
            # A CHECK THAT ENDED THE PASS OPENS NO FURTHER GATE: the spy
            # records the answer before the pass acts on it, and a gate
            # opened in that window would let the fixture finish a read the
            # stop was about to prevent.
            if self.checks and self.checks[-1].answer == ENDED:
                if after_end is not None:
                    after_end(gates)
                break
        worker.join(self.BOUND)
        self.assertFalse(worker.is_alive(), "the pass never returned")
        return rid, result["out"], self.words(record), changed

    # --------------------------------------------------------------- states

    def verdict(self, rid):
        out, err = self.mark_verdict(rid, self.side, "reviewed",
                                     polarity="approve")
        self.assertIsNone(err, err)
        return "verdict", self.current(rid)["verdict_ts"]

    def cancelled(self, rid):
        out, err = dispatches.mark_cancel(rid, "the work is moot")
        self.assertIsNone(err, err)
        return "cancelled", None

    def superseded(self, rid):
        parent = self.current(rid)
        kid = self.dispatch(ref=self.side, lane=parent["lane"], kind="review",
                            supersedes=rid)
        return "superseded", self.current(kid["id"])["ts"]

    def held(self, rid):
        out, err = dispatches.mark_hold(rid, "waiting on the base")
        self.assertIsNone(err, err)
        return "held", self.current(rid)["hold_ts"]

    def closed(self, rid):
        """An administrative retirement: the writer's own event shape, which
        the reducer admits on any open row (the world the writer proves
        first is not this arm's subject)."""
        stamp = dispatches.pk.now_ts()
        self.assertTrue(eventledger.append_unlocked(dispatches.ledger_path(), {
            "v": 3, "event": "retire", "seq": self.current(rid)["seq"] + 1,
            "id": rid, "ts": stamp, "retire_reason": "repo-unreadable",
            "retire_measurement": "the fixture's repository was unreadable",
            "retire_seat": "integrator",
            "retire_proof_version": dispatches._RETIRE_PROOF_V}))
        self.assertTrue(self.current(rid).get("retired_admin"),
                        "the retire event did not take, so this arm would "
                        "measure an open row")
        return "retired_admin", stamp

    def retipped(self, rid):
        out, err = dispatches.retip(rid, self.b, reason="base moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        return "retipped", self.current(rid)["retips"][-1]["ts"]

    def unreadable(self, _rid):
        """A second hard link: the ledger door reads only a private regular
        file with ONE link, so every read is UNKNOWN until the link goes."""
        path = dispatches.ledger_path()
        twin = path + ".twin"
        os.link(path, twin)
        self.assertIsNotNone(dispatches.snapshot()[1],
                             "the linked ledger still reads, so this arm "
                             "would measure a readable one")
        return lambda: os.unlink(twin)

    #: Each ending state the pass stops on, and the row kind its writer
    #: takes: every one leaves the row unable to take a note for good.
    ENDED = (("verdict", "review"), ("cancelled", "review"),
             ("closed", "review"))
    #: Each state that can come back, read to its end as it always was. A
    #: retip is a build row's move here: the review-kind identity rule of the
    #: retip writer is not this arm's subject, and the pass reads a row by
    #: its id whatever its kind.
    COMES_BACK = (("held", "review"), ("superseded", "review"),
                  ("retipped", "build"))

    def ref_for(self, kind):
        return self.a if kind == "build" else self.side

    def stop_record(self, rid):
        record = findingspass.stop_record(rid)
        self.assertIsNotNone(record, "no stop record was written")
        return record

    def stored(self, record):
        text, problem = dispatches.read_brief_file(record["output_ref"],
                                                   record["output_bytes"])
        self.assertIsNone(problem, problem)
        return text

    def assertStopped(self, rid, got, words, changed, reads):
        """The pass stopped at the check after `reads` reads: no note on the
        ledger, the script stopped before the next read finished, the reads
        that finished stored whole, and one line naming the state and its
        time."""
        word, at = changed
        row, why = got
        self.assertIsNone(row, "a note was recorded: %r" % (row,))
        self.assertNotIn("findings_notes", self.current(rid))
        prefix = "stopped: row reached %s at " % word
        self.assertIn(prefix, why)
        if at:
            self.assertIn(prefix + at, why)
        else:
            self.assertIn("the row records no time", why)
        self.assertNotIn("read-end-%d" % (reads + 1), words)
        self.assertNotIn("exit", words, "the script ran to its end")
        record = self.stop_record(rid)
        self.assertEqual((record["state"], record["reads"]), (word, reads))
        self.assertEqual(record["reviewed_tip"], self.side)
        text = self.stored(record)
        for i in range(1, 4):
            (self.assertIn if i <= reads else self.assertNotIn)(
                "## helm/f%d.py" % i, text)
        line = "\n".join(findingspass.note_lines(self.current(rid)))
        self.assertIn(prefix, line)
        return record


class QueuedWorkersBase(RecheckBase):
    """`queue` with its detached start stood in by a thread that runs the
    worker's own `run` on the row the argv hands it, as the real worker
    does: the queue, the lock it waits on and the pass it runs are each the
    production code, in-process."""

    def setUp(self):
        super().setUp()
        #: (thread, result box) of every worker a queue started, in order.
        self.workers = []
        #: How many of the next queued workers die before they take the
        #: queue lock (an OOM, a SIGKILL, an import crash): each one starts
        #: and exits without running its pass.
        self.dying = 0
        real = findingspass.queue

        def spawn(argv, tip):
            # the worker's argv names the row and nothing else, as main's
            rid, = argv[argv.index("helm.findingspass") + 1:]
            box = {"tip": tip}
            dies = self.dying > 0
            self.dying -= dies

            def target():
                if dies:
                    box["died"] = True
                else:
                    box["out"] = findingspass.run(rid)
            thread = threading.Thread(
                target=target, daemon=True,
                name="queued-%d" % len(self.workers))
            self.workers.append((thread, box))
            thread.start()
            return mock.Mock()
        def queue(row, popen=None):
            tip = str(row.get("tip") or "") if isinstance(row, dict) else ""
            return real(row, popen=lambda argv, **_kwargs: spawn(argv, tip))
        patcher = mock.patch.object(findingspass, "queue", queue)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.join_workers)

    def join_workers(self):
        for thread, _box in self.workers:
            thread.join(self.BOUND)
            self.assertFalse(thread.is_alive(), "a queued pass never returned")

    def started(self):
        """The tip each queued worker's row named when it was queued, in
        order."""
        return [box["tip"] for _thread, box in self.workers]

    def lane_tip(self, name):
        """The lane's one commit on trunk, its message naming the branch.
        One patch under two messages is the same work at two tips (a
        rewritten car), which a review row's retip verifies by patch id in
        both directions: the two tips share their base, so each one's
        reviewed sequence is the other's whole history."""
        self.git("checkout", "-q", "-b", name, self.main)
        with open(os.path.join(self.repo, "lane.txt"), "w",
                  encoding="utf-8") as f:
            f.write("the lane's own work\n")
        self.git("add", "lane.txt")
        # ONLY the lane's file: the fixture keeps its package root staged and
        # uncommitted (`plant_helm_root`), and it must stay that way on trunk
        self.git("commit", "-q", "-m", "lane work (%s)" % name, "--",
                 "lane.txt")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        return tip


class TheCheckBeforeTheFirstReadTest(RecheckBase):
    """The row's state changes after the pass took its lock and before the
    script's first read: the check at the script's header sees it."""

    def test_each_ending_state_stops_the_pass_before_any_read(self):  # noqa: VACUOUS_ASSERTION — each cell asserts its stop record's state and read count to exact values and the stop line positively; the absent reads are read off the same fixture record, and the must-hit list after the loop proves every cell ran
        ran = []
        for state, kind in self.ENDED:
            with self.subTest(state=state):
                rid, got, words, changed = self.drive(
                    "first-" + state, BEFORE_FIRST, getattr(self, state),
                    kind=kind, ref=self.ref_for(kind))
                self.assertEqual([c.answer for c in self.checks], [ENDED])
                self.assertIn("start", words)
                self.assertStopped(rid, got, words, changed, reads=0)
                ran.append(state)
        self.assertEqual(ran, [state for state, _kind in self.ENDED])

    def test_a_row_still_pending_is_read_whole_and_noted_as_today(self):
        rid, got, words, _ = self.drive("first-pending")
        row, why = got
        self.assertIsNone(why, why)
        self.assertEqual(self.line(rid), CLEAN_LINE)
        self.assertEqual(set(c.answer for c in self.checks), {OWED})
        self.assertEqual(len(self.checks), 4, "one check before each read "
                         "and one after the last")
        self.assertIn("exit", words)
        self.assertIsNone(findingspass.stop_record(rid))

    def test_an_unreadable_ledger_is_unknown_and_the_pass_goes_on(self):  # noqa: VACUOUS_ASSERTION — the absent stop record is read after the same row's note is asserted to exact outcome, read count and reason
        rid, got, words, _ = self.drive("first-unreadable", BEFORE_FIRST,
                                        self.unreadable)
        row, why = got
        self.assertIsNone(why, why)
        self.assertEqual([c.answer for c in self.checks],
                         [UNKNOWN] + [OWED] * 3)
        note = self.notes(rid)[-1]
        self.assertEqual((note["outcome"], note["reads"]), ("complete", 3))
        self.assertIn("could not be re-read at 1 of 4 checks", note["reason"])
        self.assertIn("went on", note["reason"])
        self.assertIn(note["reason"],
                      "\n".join(findingspass.note_lines(self.current(rid))))
        self.assertIsNone(findingspass.stop_record(rid))


class TheCheckBetweenReadsTest(RecheckBase):
    """The row's state changes while read 2 is in flight: the check after it
    stops the pass before read 3, and reads 1 and 2 are kept."""

    def test_each_ending_state_stops_the_pass_and_keeps_the_finished_reads(self):  # noqa: VACUOUS_ASSERTION — each cell asserts read 2 finished and the stored output carries reads 1 and 2 before read 3 is asserted absent, and the must-hit list after the loop proves every cell ran
        ran = []
        for state, kind in self.ENDED:
            with self.subTest(state=state):
                rid, got, words, changed = self.drive(
                    "between-" + state, BETWEEN, getattr(self, state),
                    kind=kind, ref=self.ref_for(kind))
                self.assertEqual([c.answer for c in self.checks],
                                 [OWED] * 2 + [ENDED])
                self.assertIn("read-end-2", words)
                self.assertStopped(rid, got, words, changed, reads=2)
                ran.append(state)
        self.assertEqual(ran, [state for state, _kind in self.ENDED])

    def test_a_finished_read_split_across_pipe_reads_is_kept_whole(self):  # noqa: VACUOUS_ASSERTION — the absent later read sits beside the stored output asserted to carry the second half of read 2 and a stop record asserted to exact state and read count
        """The check can run on the first part of a finished read's text. The
        script is stopped only once its output has been quiet, so the rest of
        that read arrives and is kept; the quiet window is widened here so
        the box's speed decides nothing."""
        with mock.patch.object(findingspass, "QUIET_S", 3):
            rid, got, words, changed = self.drive(
                "between-split", BETWEEN, self.verdict, split=(2,),
                after_end=lambda gates: self.open_gate(gates["rest-2"]))
        self.assertEqual([c.answer for c in self.checks],
                         [OWED] * 2 + [ENDED])
        record = self.assertStopped(rid, got, words, changed, reads=2)
        self.assertIn("NONE FOUND in the rest of read 2", self.stored(record))
        self.assertIn("read-end-2", words)

    def test_a_row_still_pending_is_read_whole(self):
        rid, got, words, _ = self.drive("between-pending", BETWEEN,
                                        lambda rid: None)
        self.assertIsNone(got[1], got[1])
        self.assertEqual(self.notes(rid)[-1]["outcome"], "complete")
        self.assertIn("exit", words)

    def test_an_unreadable_ledger_is_unknown_and_the_pass_goes_on(self):  # noqa: VACUOUS_ASSERTION — the None is the pass's why channel, whose note is asserted to exact outcome, read count and reason
        rid, got, words, _ = self.drive("between-unreadable", BETWEEN,
                                        self.unreadable)
        self.assertIsNone(got[1], got[1])
        self.assertEqual([c.answer for c in self.checks],
                         [OWED] * 2 + [UNKNOWN] + [OWED])
        note = self.notes(rid)[-1]
        self.assertEqual((note["outcome"], note["reads"]), ("complete", 3))
        self.assertIn("could not be re-read at 1 of 4 checks", note["reason"])
        self.assertIn("exit", words)


class AStateThatCanComeBackIsReadToItsEndTest(RecheckBase):
    """THE RULING (task/3382 L5): the pass stops only on a row that can never
    take its note. A hold, a successor that carries the row and a retip each
    land while read 2 is in flight; every check still answers OWED, the
    script runs to its end, and the row gets what main gave it: a note on a
    held or carried row, and on a retipped one the ledger's other-tip
    refusal, named in a FINISHED record with the reads it stored."""

    def test_a_hold_mid_pass_is_read_to_its_end_and_noted(self):  # noqa: VACUOUS_ASSERTION — the absent stop record is read after the same row's note is asserted to its exact line on a row asserted HELD
        rid, got, words, changed = self.drive("back-held", BETWEEN, self.held)
        row, why = got
        self.assertIsNone(why, why)
        self.assertEqual(row["status"], "held")
        self.assertEqual([c.answer for c in self.checks], [OWED] * 4)
        self.assertIn("exit", words)
        self.assertEqual(self.line(rid), CLEAN_LINE)
        self.assertIsNone(findingspass.stop_record(rid))

    def test_a_successor_mid_pass_is_read_to_its_end_and_noted(self):  # noqa: VACUOUS_ASSERTION — the absent stop record is read after the same row's note is asserted to its exact line on a row asserted carried by the successor
        rid, got, words, changed = self.drive("back-superseded", BETWEEN,
                                              self.superseded)
        self.assertIsNone(got[1], got[1])
        current, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable, unavailable)
        self.assertIsNotNone(dispatches.carrier(current[rid], current),
                             "no successor carries the row, so this arm "
                             "would measure an uncarried one")
        self.assertEqual([c.answer for c in self.checks], [OWED] * 4)
        self.assertIn("exit", words)
        self.assertEqual(self.line(rid), CLEAN_LINE)
        self.assertIsNone(findingspass.stop_record(rid))

    def test_a_retip_mid_pass_is_read_to_its_end_and_recorded_finished(self):  # noqa: VACUOUS_ASSERTION — the absent note sits beside a FINISHED record asserted to exact kind, state, read count and tip, whose stored output is asserted to carry the last read
        rid, got, words, changed = self.drive(
            "back-retipped", BETWEEN, self.retipped, kind="build", ref=self.a)
        row, why = got
        self.assertEqual(self.current(rid)["tip"], self.b)
        self.assertEqual([c.answer for c in self.checks], [OWED] * 4)
        self.assertIn("exit", words)
        self.assertIsNone(row)
        self.assertIn(dispatches.FINDINGS_OTHER_TIP, why)
        self.assertNotIn("findings_notes", self.current(rid))
        record = self.stop_record(rid)
        self.assertEqual((record["kind"], record["state"], record["reads"],
                          record["reviewed_tip"]),
                         ("finished", "retipped", 3, self.a))
        self.assertIn("## helm/f3.py", self.stored(record))
        self.assertIn("reached retipped at %s" % changed[1], record["line"])


class AChangeAfterTheLastReadStopsNothingTest(RecheckBase):
    """After the last read there is no read left to save: the pass completes
    and writes its note as it does today."""

    def test_a_hold_after_the_last_read_still_gets_the_note(self):  # noqa: VACUOUS_ASSERTION — the absent stop record is read after the same row's note is asserted to exact outcome on a row asserted HELD
        rid, got, words, changed = self.drive("after-held", AFTER_LAST,
                                              self.held)
        row, why = got
        self.assertIsNone(why, why)
        self.assertEqual(row["status"], "held")
        self.assertEqual(self.notes(rid)[-1]["outcome"], "complete")
        self.assertEqual(set(c.answer for c in self.checks), {OWED})
        self.assertIn("exit", words)
        self.assertIsNone(findingspass.stop_record(rid))

    def test_a_verdict_after_the_last_read_keeps_the_finished_reads(self):  # noqa: VACUOUS_ASSERTION — the absent ledger note sits beside a stop record asserted to exact kind, state and read count, whose stored output is asserted to carry the last read
        """The ledger refuses the note, as today, because nobody is left to
        adjudicate it; the reads that finished are stored and named rather
        than dropped with it."""
        rid, got, words, changed = self.drive("after-verdict", AFTER_LAST,
                                              self.verdict)
        row, why = got
        self.assertIsNone(row)
        self.assertIn("a findings note lands only on a row still owed", why)
        self.assertNotIn("findings_notes", self.current(rid))
        self.assertIn("exit", words)
        record = self.stop_record(rid)
        self.assertEqual((record["kind"], record["state"], record["reads"]),
                         ("finished", "verdict", 3))
        self.assertIn("## helm/f3.py", self.stored(record))
        line = "\n".join(findingspass.note_lines(self.current(rid)))
        self.assertIn("before its note could land", line)


class TheLockTimeCheckIsMainsTest(RecheckBase):
    """The check the pass makes when it takes the queue lock is main's: a
    row open or held is read, and any other skips the pass with no script
    started and no record written, as main skipped it."""

    #: The words each ending state's skip names: the status main's line
    #: names, and the resolver's own refusal for a retired row.
    LOCK_WORDS = {"verdict": "is verdict by now: nobody is left to adjudicate",
                  "cancelled": "is cancelled by now: nobody is left to "
                               "adjudicate",
                  "closed": "was administratively retired"}

    def test_each_ending_state_before_the_lock_reads_nothing(self):  # noqa: VACUOUS_ASSERTION — each cell asserts the skip line positively; the empty fixture record is the script never starting, the absent stop record is main's shape, and the must-hit list after the loop proves every cell ran
        ran = []
        for state, kind in self.ENDED:
            with self.subTest(state=state):
                gates, record = self.stream("lock-" + state)
                row = self.dispatch(ref=self.side, lane="lane/lock-" + state,
                                    kind=kind)
                getattr(self, state)(row["id"])
                got, why = findingspass.run(row["id"])
                self.assertIsNone(got)
                self.assertIn(self.LOCK_WORDS[state], why)
                self.assertEqual(self.words(record), [],
                                 "the script was started for a row nobody "
                                 "is left to read")
                self.assertIsNone(findingspass.stop_record(row["id"]),
                                  "a skip at the lock wrote a record main "
                                  "never wrote")
                ran.append(state)
        self.assertEqual(ran, ["verdict", "cancelled", "closed"])

    def test_a_held_or_carried_row_at_the_lock_is_read_as_main_reads_it(self):  # noqa: VACUOUS_ASSERTION — each cell asserts the note to its exact line and the fixture to have run to its end, and the must-hit list after the loop proves every cell ran
        ran = []
        for state in ("held", "superseded"):
            with self.subTest(state=state):
                gates, record = self.stream("lock-read-" + state)
                self.free(gates)
                rid = self.dispatch(ref=self.side, lane="lane/lock-read-"
                                    + state, kind="review")["id"]
                getattr(self, state)(rid)
                got, why = findingspass.run(rid)
                self.assertIsNone(why, why)
                self.assertEqual(self.line(rid), CLEAN_LINE)
                self.assertIn("exit", self.words(record))
                ran.append(state)
        self.assertEqual(ran, ["held", "superseded"])

    def test_the_lock_and_every_check_share_one_owed_set(self):  # noqa: VACUOUS_ASSERTION — the lock-time skip is asserted positively by its line and the mid-read stop by its exact check answers, state and read count
        """CONDITION 3 of the reader's walk: no status can slip between the
        lock and the checks, because both derive the ending from
        `dispatches.FINDINGS_NOTE_STATES`. Narrowed to open alone, a held
        row is skipped at the lock AND stops a pass already reading."""
        with mock.patch.object(dispatches, "FINDINGS_NOTE_STATES", ("open",)):
            gates, record = self.stream("narrow-lock")
            rid = self.dispatch(ref=self.side, lane="lane/narrow-lock",
                                kind="review")["id"]
            self.held(rid)
            got, why = findingspass.run(rid)
            self.assertIsNone(got)
            self.assertIn("is held by now: nobody is left to adjudicate", why)
            self.assertEqual(self.words(record), [])
            rid, got, words, changed = self.drive("narrow-mid", BETWEEN,
                                                  self.held)
        self.assertEqual([c.answer for c in self.checks],
                         [OWED] * 2 + [ENDED])
        # a hold ends nothing on the real set, so no ending time names it
        self.assertIn("stopped: row reached held at ", got[1])
        self.assertEqual((self.stop_record(rid)["state"],
                          self.stop_record(rid)["reads"]), ("held", 2))
        self.assertNotIn("exit", words)

    def test_an_unreadable_ledger_at_the_lock_is_never_no_longer_owed(self):  # noqa: VACUOUS_ASSERTION — the reason is asserted positively to name the unavailable ledger before the absent stop word and record are read
        gates, record = self.stream("lock-unreadable")
        row = self.dispatch(ref=self.side, lane="lane/lock-unreadable",
                            kind="review")
        undo = self.unreadable(row["id"])
        try:
            got, why = findingspass.run(row["id"])
        finally:
            undo()
        self.assertIsNone(got)
        self.assertIn("dispatch ledger unavailable", why)
        self.assertNotIn("stopped", why)
        self.assertIsNone(findingspass.stop_record(row["id"]))


class TriageShowsTheStopTest(RecheckBase):
    """`helm dispatch triage <id>` on a row the pass stopped on prints the
    stop under the row, whatever state the row is in."""

    def cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = dispatches.cmd_dispatch(args)
        return rc, out.getvalue(), err.getvalue()

    def test_a_verdicted_row_prints_its_stop_line(self):
        rid, got, _words, changed = self.drive("triage-verdict", BETWEEN,
                                               self.verdict)
        rc, out, err = self.cli(["triage", rid])
        self.assertEqual(rc, 0, err)
        self.assertIn("VERDICT", out)
        self.assertIn("stopped: row reached verdict at %s" % changed[1], out)
        self.assertIn("whole output", out)


class ACheckIsCheapTest(RecheckBase):
    """F1 of the approval-tier read: each check was a whole
    `dispatches.snapshot()`, and once the helm tree changes on disk under a
    long pass every snapshot in that process is a COLD fold (95-140 s and
    about 1,460 git spawns on the live ledger), about 27 of them on the
    largest pass where main took about 2. A check now reads only what the
    ledger appended since the last answer: a line that does not name the row
    changes nothing, and costs no fold."""

    def setUp(self):
        super().setUp()
        #: The phase of every fold the WORKER took.
        self.folds = []
        real_fold = dispatches._ledger_fold

        def fold(*args, **kwargs):
            if threading.current_thread().name == self.WORKER:
                self.folds.append(self.phase)
            return real_fold(*args, **kwargs)
        real_note = dispatches.record_findings_note

        def note(*args, **kwargs):
            if threading.current_thread().name == self.WORKER:
                self.phase = "note"
            return real_note(*args, **kwargs)
        for name, spy in (("_ledger_fold", fold),
                          ("record_findings_note", note)):
            patcher = mock.patch.object(dispatches, name, spy)
            patcher.start()
            self.addCleanup(patcher.stop)

    def at_checks(self):
        """How many folds the worker took from its first check until it
        wrote its note: the checks' own cost."""
        return self.folds.count("checks")

    def test_a_drifted_process_takes_no_fold_at_a_check(self):  # noqa: VACUOUS_ASSERTION — the zero fold count sits beside the check list asserted to four exact OWED answers and the note asserted to its exact complete line
        rid, got, words, _ = self.drive("cheap-drifted", drifted=True)
        self.assertIsNone(got[1], got[1])
        self.assertEqual([c.answer for c in self.checks], [OWED] * 4)
        self.assertEqual(self.at_checks(), 0,
                         "a check took a cold fold in a drifted process")
        self.assertEqual(self.line(rid), CLEAN_LINE)
        self.assertIn("exit", words)

    def test_a_warm_process_takes_no_fold_at_a_check_nothing_changed(self):  # noqa: VACUOUS_ASSERTION — the zero fold count at the checks sits beside the lock-time fold asserted to exactly one on the same spy and four exact OWED answers
        rid, got, words, _ = self.drive("cheap-warm")
        self.assertIsNone(got[1], got[1])
        self.assertEqual([c.answer for c in self.checks], [OWED] * 4)
        self.assertEqual(self.at_checks(), 0,
                         "a check folded a ledger that named nothing new")
        self.assertEqual(self.folds.count(None), 1,
                         "the lock-time check is the one fold before the "
                         "first read")

    def test_a_line_naming_the_row_costs_one_fold_where_the_code_is_named(self):  # noqa: VACUOUS_ASSERTION — the fold count is asserted to exactly one and the stop to its exact state and read count
        rid, got, words, changed = self.drive("cheap-named", BETWEEN,
                                              self.verdict)
        self.assertEqual([c.answer for c in self.checks],
                         [OWED] * 2 + [ENDED])
        self.assertEqual(self.at_checks(), 1)
        self.assertStopped(rid, got, words, changed, reads=2)

    def test_a_line_naming_the_row_in_a_drifted_process_is_unknown(self):  # noqa: VACUOUS_ASSERTION — the absent ledger note sits beside a stop record asserted to exact kind and read count whose stored output is asserted to carry the last read
        """No fold at all: the pass goes on as main did, and the ledger's
        own refusal of the note names why the finished reads have none."""
        rid, got, words, changed = self.drive(
            "cheap-drifted-named", BETWEEN, self.verdict, drifted=True)
        row, why = got
        self.assertEqual([c.answer for c in self.checks],
                         [OWED] * 2 + [UNKNOWN] * 2)
        self.assertIn("tree changed", self.checks[2].why)
        self.assertEqual(self.at_checks(), 0,
                         "a check took a cold fold in a drifted process")
        self.assertIn("exit", words, "the pass stopped on a fold it never took")
        self.assertIsNone(row)
        self.assertIn(dispatches.FINDINGS_NOT_OWED, why)
        self.assertNotIn("findings_notes", self.current(rid))
        record = self.stop_record(rid)
        self.assertEqual((record["kind"], record["reads"]), ("finished", 3))
        self.assertIn("## helm/f3.py", self.stored(record))
        self.assertIn(dispatches.FINDINGS_NOT_OWED, "\n".join(
            findingspass.note_lines(self.current(rid))))


class ALockTimeCheckThatRaisesTest(RecheckBase):
    """F5: the lock-time check sat outside any guard, so a raise there
    crashed the worker with no note and no record. It is UNKNOWN, as the
    checks between reads are, and the pass goes on."""

    def test_a_raise_at_the_lock_is_unknown_and_the_pass_goes_on(self):  # noqa: VACUOUS_ASSERTION — the absent stop record is read after the same row's note is asserted to exact outcome and read count and its reason to name the raise
        gates, record = self.stream("lock-raise")
        self.free(gates)
        rid = self.dispatch(ref=self.side, lane="lane/lock-raise",
                            kind="review")["id"]
        real = findingspass.standing
        calls = []

        def flaky(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise RuntimeError("a row this classifier cannot read")
            return real(*args, **kwargs)
        with mock.patch.object(findingspass, "standing", flaky):
            got, why = findingspass.run(rid)
        self.assertIsNone(why, why)
        note = self.notes(rid)[-1]
        self.assertEqual((note["outcome"], note["reads"]), ("complete", 3))
        self.assertIn("could not be read when the pass took its lock",
                      note["reason"])
        self.assertIn("RuntimeError: a row this classifier cannot read",
                      note["reason"])
        self.assertIsNone(findingspass.stop_record(rid))


class ATimedOutRunCountsItsStoredReadsTest(RecheckBase):
    """F6: a run that timed out after its reads finished, on a row that
    ended meanwhile, said `ran to its end (0 read(s) finished)` while the
    output it stored holds every finished read."""

    def test_a_timed_out_run_counts_the_reads_it_stored(self):  # noqa: VACUOUS_ASSERTION — the absent words sit beside a stop record asserted to exact kind, state and read count
        os.environ[findingspass.TIMEOUT] = "10"
        rid, got, words, changed = self.drive(
            "timed-out", AFTER_LAST, self.verdict, hold=AFTER_LAST)
        row, why = got
        self.assertIsNone(row)
        self.assertNotIn("exit", words)
        record = self.stop_record(rid)
        self.assertEqual((record["kind"], record["state"], record["reads"]),
                         ("finished", "verdict", 3))
        self.assertIn("3 read(s) finished", record["line"])
        self.assertIn("bound", record["line"])
        self.assertNotIn("ran to its end", record["line"])
        self.assertIn("## helm/f3.py", self.stored(record))


class ALateRefusalOnAHeldRowIsNotBlamedOnItTest(RecheckBase):
    """A pass that ran to its end names the state that ended its row when
    the ledger refused the note. A held row takes a note, so a refusal there
    is the ledger's own and is never said to be the row's state. A retipped
    row takes a note of its own tip only
    (`ARetipBeforeTheNoteKeepsTheFinishedReadsTest`)."""

    def test_a_note_refused_on_a_held_row_is_not_said_to_be_the_hold(self):  # noqa: VACUOUS_ASSERTION — the absent stop word and record sit beside the refusal asserted positively to be the ledger's own words
        def refuse(rid, tip, fields):
            return None, ("ledger unwritable (fixture) — findings note NOT "
                          "recorded")
        with mock.patch.object(dispatches, "record_findings_note", refuse):
            rid, got, words, changed = self.drive("late-held", AFTER_LAST,
                                                  self.held)
        row, why = got
        self.assertIsNone(row)
        self.assertEqual(self.current(rid)["status"], "held")
        self.assertIn("ledger unwritable (fixture)", why)
        self.assertNotIn("reached held", why)
        self.assertIsNone(findingspass.stop_record(rid))


class ARetipBeforeTheNoteKeepsTheFinishedReadsTest(RecheckBase):
    """F12 of the approval-tier read: a pass ran to its end on tip A of a
    row retipped to B before its note. The ledger refuses a note of another
    tip than the row's, a refusal that does not carry FINDINGS_NOT_OWED, so
    the pass returned with no record at all and the reads of A it had stored
    were named nowhere. That refusal is the retip: the pass writes its
    FINISHED record naming it, with the reads it stored, whether or not its
    re-read can fold the row."""

    def late_retip(self, name, drifted):
        rid, got, words, changed = self.drive(name, AFTER_LAST, self.retipped,
                                              kind="build", ref=self.a,
                                              drifted=drifted)
        row, why = got
        self.assertIn("exit", words, "the pass did not run to its end")
        self.assertEqual(self.current(rid)["tip"], self.b)
        self.assertIsNone(row)
        self.assertNotIn("findings_notes", self.current(rid))
        record = self.stop_record(rid)
        self.assertEqual((record["kind"], record["state"], record["reads"],
                          record["reviewed_tip"]),
                         ("finished", "retipped", 3, self.a))
        self.assertIn("## helm/f3.py", self.stored(record))
        self.assertIn("before its note could land", record["line"])
        self.assertIn("names another tip", why)
        self.assertIn(record["line"], why)
        return record

    def test_a_retip_after_the_last_read_writes_the_finished_record(self):
        record = self.late_retip("late-retip", drifted=False)
        self.assertIn("reached retipped at %s" % self.current(
            record["id"])["retips"][-1]["ts"], record["line"])

    def test_a_retip_after_the_last_read_in_a_drifted_process_writes_it_too(self):  # noqa: VACUOUS_ASSERTION — the helper asserts the record's kind, state, read count and tip to exact values, and the check list here to exact answers
        self.late_retip("late-retip-drifted", drifted=True)
        self.assertEqual([c.answer for c in self.checks], [OWED] * 4)

    def test_a_note_at_the_new_tip_then_speaks_over_the_record(self):  # noqa: VACUOUS_ASSERTION — the absent stop words are read off the same lines whose first is asserted to be the complete note
        """The record is the newest word only until a note lands after it;
        which speaks is decided by the ledger's order, never the clock (the
        record and the note usually share one second here)."""
        record = self.late_retip("late-retip-then-note", drifted=False)
        got, why = findingspass.run(record["id"])
        self.assertIsNone(why, why)
        self.assertEqual(self.stop_record(record["id"])["notes_seen"], 0)
        lines = findingspass.note_lines(self.current(record["id"]))
        self.assertIn("no findings (complete, 3 reads)", lines[0])
        self.assertNotIn("stopped", "\n".join(lines))
        self.assertNotIn("before its note could land", "\n".join(lines))


class ASecondQueueWaitsForTheFirstTest(QueuedWorkersBase):
    """F4 AS THE RULING KEEPS IT: a queue is never refused, and a row and tip
    are read once. A second queue of the row and tip the first pass reads
    starts its worker; that worker waits on the queue lock the first pass
    holds, and at its lock finds the first one's complete read and spends no
    read."""

    def test_a_second_queue_waits_and_at_its_lock_reads_nothing(self):  # noqa: VACUOUS_ASSERTION — the second pass's absent row sits beside its why asserted positively, the first pass's note is asserted to its exact line, and the script starts are counted to exactly one
        gates, record = self.stream("second-queue")
        rid = self.dispatch(ref=self.side, lane="lane/second-queue",
                            kind="review")["id"]
        self.on()
        events = self.lock_spy()
        self.assertEqual(findingspass.queue(self.current(rid)), (True, None))
        self.wait(lambda: "start" in self.words(record),
                  "the first pass's script to start")
        self.assertEqual(findingspass.queue(self.current(rid)), (True, None),
                         "a queue of a row and tip a pass holds was refused")
        second = self.workers[1][0].name
        self.wait(lambda: ("lock-asked", second) in events,
                  "the second pass to ask for the queue lock")
        time.sleep(self.SETTLE)
        mine = [step for step, who in events if who == second]
        self.assertEqual(mine, ["lock-asked"], "the second pass took the "
                         "queue lock the first holds")
        self.free(gates)
        self.join_workers()
        first_row, first_why = self.workers[0][1]["out"]
        self.assertIsNone(first_why, first_why)
        self.assertEqual(self.line(rid), CLEAN_LINE)
        got, why = self.workers[1][1]["out"]
        self.assertIsNone(got)
        self.assertIn("already carries a read of %s" % self.side[:12], why)
        self.assertEqual(self.words(record).count("start"), 1,
                         "the second pass read the row again")
        self.assertEqual(self.started(), [self.side, self.side])


class TheQueueLockIsMainsTest(QueuedWorkersBase):
    """CONDITION 1 of the reader's walk: the fleet's one-at-a-time queue lock
    stays exactly what it was, so two passes of two rows never read at
    once."""

    def test_two_passes_on_two_rows_never_read_at_the_same_time(self):  # noqa: VACUOUS_ASSERTION — the absent lock-held step sits beside the lock-asked step the arm waited for on the same spy, and both notes and the script order are asserted exactly
        gates, record = self.stream("two-rows")
        first = self.dispatch(ref=self.side, lane="lane/two-rows-r",
                              kind="review")["id"]
        other = self.dispatch(ref=self.side, lane="lane/two-rows-s",
                              kind="review")["id"]
        self.on()
        events = self.lock_spy()
        self.assertEqual(findingspass.queue(self.current(first)), (True, None))
        self.wait(lambda: "start" in self.words(record),
                  "the first row's script to start")
        self.assertEqual(findingspass.queue(self.current(other)), (True, None))
        second = self.workers[1][0].name
        self.wait(lambda: ("lock-asked", second) in events,
                  "the other row's pass to ask the queue lock")
        time.sleep(self.SETTLE)
        self.assertNotIn(("lock-held", second), events,
                         "the other row's pass took the queue lock while "
                         "the first read")
        self.assertEqual(self.words(record).count("start"), 1,
                         "two scripts ran at once")
        self.free(gates)
        self.join_workers()
        for rid in (first, other):
            self.assertEqual(self.line(rid), CLEAN_LINE)
        marks = self.lines(record)
        pids = [pid for word, pid in marks if word == "start"]
        self.assertEqual(len(pids), 2, marks)
        self.assertLess(marks.index(("exit", pids[0])),
                        marks.index(("start", pids[1])),
                        "the second script started before the first ended")


class ARetipAwayAndBackLosesNoPassTest(QueuedWorkersBase):
    """A row retipped A to B and back to A while the pass at A reads ends
    with exactly one complete read at A, in both orders. Each retip queues a
    pass, and each WAITS on the queue lock the reading pass holds; at its own
    lock each reads the tip the row names then, which is A again. When the
    back retip lands before the first pass's note, that note lands at A and
    both waiters find it; when it lands after the ledger refused that note as
    B's (the F12 gap, where an earlier claim that refused a queue lost the
    pass), the first waiter to take the lock reads A and the other finds that
    read."""

    def away_and_back(self, name, late):
        """(row id, the first pass's (row, why), its record words, A, B)."""
        old, new = self.lane_tip(name + "-first"), \
            self.lane_tip(name + "-rewritten")
        self.assertNotEqual(old, new, "the fixture did not rewrite the car")
        box, back = [], []

        def retip(to, why):
            out, err = dispatches.retip(box[0], to, reason=why,
                                        repo=self.repo, notify=False)
            self.assertIsNone(err, err)

        def away(rid):
            self.on()
            box.append(rid)
            retip(new, "trunk moved")
            if not late:
                retip(old, "trunk moved back")
                back.append(old)
        real = dispatches.record_findings_note

        def note(rid, tip, fields):
            out, err = real(rid, tip, fields)
            if late and not back \
                    and dispatches.FINDINGS_OTHER_TIP in str(err or ""):
                # THE RETIP BACK LANDS AFTER THE OTHER-TIP REFUSAL AND BEFORE
                # THE FIRST PASS LETS GO OF THE QUEUE LOCK
                retip(old, "trunk moved back")
                back.append(old)
            return out, err
        with mock.patch.object(dispatches, "record_findings_note", note):
            rid, got, words, _ = self.drive(name, BETWEEN, away, ref=old)
            self.assertEqual(back, [old], "the retip back never ran")
            self.join_workers()
        self.assertEqual(self.current(rid)["tip"], old)
        self.assertEqual(self.started(), [new, old])
        self.assertEqual([(n["reviewed_tip"], n["outcome"])
                          for n in self.notes(rid)], [(old, "complete")],
                         "the row does not carry exactly one complete read "
                         "at %s" % old[:12])
        return rid, got, words, old, new

    def test_a_retip_back_while_the_pass_reads_leaves_its_note_to_the_waiter(self):  # noqa: VACUOUS_ASSERTION — the one script start is counted off the fixture record once every queued pass returned, both waiters' reasons are asserted to name the read at A, and the helper asserts the one complete note at A
        rid, got, words, old, new = self.away_and_back("back-early",
                                                       late=False)
        self.assertIsNone(got[1], got[1])
        self.assertEqual(
            self.words(os.path.join(self.tmp, "back-early", "record"))
            .count("start"), 1, "a waiter read A again")
        outs = [box["out"] for _thread, box in self.workers]
        self.assertEqual([got_q for got_q, _why in outs], [None, None], outs)
        self.assertEqual(["already carries a read of %s" % old[:12]
                          in str(why) for _got, why in outs], [True, True],
                         outs)

    def test_a_retip_back_after_the_other_tip_refusal_is_read_by_the_waiter(self):  # noqa: VACUOUS_ASSERTION — exactly one waiter's why is None and its row is asserted at A, the other's reason is asserted to name that read, and the helper asserts the one complete note at A
        rid, got, words, old, new = self.away_and_back("back-late", late=True)
        self.assertIn(dispatches.FINDINGS_OTHER_TIP, got[1])
        record = self.stop_record(rid)
        self.assertEqual((record["kind"], record["state"],
                          record["reviewed_tip"]), ("finished", "retipped",
                                                    old))
        # EITHER WAITER MAY TAKE THE LOCK FIRST: the one that does reads A,
        # and the other finds that read
        outs = [box["out"] for _thread, box in self.workers]
        read = [got_q for got_q, why in outs if why is None]
        found = [why for _got, why in outs if why is not None]
        self.assertEqual(len(read), 1, "not exactly one waiter read A: %r"
                         % (outs,))
        self.assertEqual(read[0]["tip"], old)
        self.assertEqual(len(found), 1, outs)
        self.assertIn("already carries a read of %s" % old[:12], found[0])
        self.assertEqual(
            self.words(os.path.join(self.tmp, "back-late", "record"))
            .count("start"), 2)
        self.assertIn("no findings (complete, 3 reads)",
                      findingspass.note_lines(self.current(rid))[0])


class ADeadWorkerIsCoveredByOneThatLivesTest(QueuedWorkersBase):
    """F16 of the approval-tier read: a row filed at A queues a worker, a
    retip to B queues another, and the one queued at B dies before it takes
    the queue lock (an OOM, a SIGKILL, an import crash). The worker queued
    at A must read B when it takes the lock, as main's does: a pass reads
    the tip its row names at its lock, so any worker that lives covers one
    that died and B is never left unread. When both live, the lock-time
    check of a complete or partial read at that tip keeps B to one read."""

    def queued_behind_the_lock(self, name, dying):
        """(row id, A, B, the fixture's record words): a worker queued at A
        waits on the queue lock the arm holds, the row is retipped to B,
        whose worker dies before its lock when `dying`, and the arm lets go
        of the lock once every worker has returned or is waiting."""
        old, new = self.lane_tip(name + "-first"), \
            self.lane_tip(name + "-rewritten")
        gates, record = self.stream(name)
        self.free(gates)
        rid = self.dispatch(ref=old, lane="lane/" + name,
                            kind="review")["id"]
        self.on()
        held = findingspass._lock()
        try:
            events = self.lock_spy()
            self.assertEqual(findingspass.queue(self.current(rid)),
                             (True, None))
            first = self.workers[0][0].name
            self.wait(lambda: ("lock-asked", first) in events,
                      "the worker queued at A to ask for the queue lock")
            self.dying = int(dying)
            out, err = dispatches.retip(rid, new, reason="trunk moved",
                                        repo=self.repo, notify=False)
            self.assertIsNone(err, err)
            self.assertEqual(len(self.workers), 2,
                             "the retip to B queued no worker")
            if dying:
                thread, box = self.workers[1]
                thread.join(self.BOUND)
                self.assertEqual(box.get("died"), True,
                                 "the worker queued at B did not die")
            time.sleep(self.SETTLE)
            self.assertNotIn(("lock-held", first), events,
                             "the worker queued at A took a lock the arm "
                             "holds")
        finally:
            os.close(held)
        self.join_workers()
        self.assertEqual(self.current(rid)["tip"], new)
        self.assertEqual(self.started(), [old, new])
        return rid, old, new, self.words(record)

    def assertOneReadAt(self, rid, tip, words):
        self.assertEqual([(n["reviewed_tip"], n["outcome"])
                          for n in self.notes(rid)], [(tip, "complete")],
                         "the row does not carry exactly one complete read "
                         "at %s" % tip[:12])
        self.assertEqual(words.count("start"), 1,
                         "the row was not read exactly once")

    def test_the_worker_queued_at_A_reads_B_when_B_s_worker_died(self):  # noqa: VACUOUS_ASSERTION — the None is the worker's why channel, whose row is asserted at B, and the helper asserts the one complete note at B and one script start
        rid, old, new, words = self.queued_behind_the_lock("dead-at-b",
                                                           dying=True)
        got, why = self.workers[0][1]["out"]
        self.assertIsNone(why, "the worker queued at A read nothing while "
                          "B's worker was dead: %s" % why)
        self.assertEqual(got["tip"], new)
        self.assertOneReadAt(rid, new, words)
        self.assertEqual(self.line(rid), CLEAN_LINE)

    def test_both_workers_alive_read_B_once(self):  # noqa: VACUOUS_ASSERTION — the reader among the two returns is counted to exactly one, the other's reason is asserted to name the read at B, and the helper asserts the one complete note at B and one script start
        rid, old, new, words = self.queued_behind_the_lock("alive-at-b",
                                                           dying=False)
        outs = [box["out"] for _thread, box in self.workers]
        self.assertEqual([why is None for _got, why in outs].count(True), 1,
                         outs)
        found = [why for _got, why in outs if why is not None]
        self.assertEqual(len(found), 1, outs)
        self.assertIn("already carries a read of %s" % new[:12], found[0])
        self.assertOneReadAt(rid, new, words)


class AKilledHolderLetsItsWaiterGoOnTest(RecheckBase):
    """A pass SIGKILLed while it reads frees the queue lock through the
    kernel: the pass waiting on that lock takes it, reads the row, and lands
    the note the killed one never wrote. The holder is a real
    worker process (`python3 -m helm.findingspass <row>`), because only a
    process can be killed."""

    def test_a_holder_killed_mid_read_lets_the_waiter_read_the_row(self):  # noqa: VACUOUS_ASSERTION — the None is the waiter's why channel, whose note is asserted to its exact line, and the holder to have died by SIGKILL
        gates, record = self.stream("killed-holder")
        rid = self.dispatch(ref=self.side, lane="lane/killed-holder",
                            kind="review")["id"]
        root = os.path.dirname(os.path.dirname(os.path.abspath(
            findingspass.__file__)))
        log = open(os.path.join(self.tmp, "holder.log"), "wb")
        self.addCleanup(log.close)
        holder = subprocess.Popen(
            [sys.executable, "-m", "helm.findingspass", rid],
            cwd=root, env=dict(os.environ, PYTHONPATH=root),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True)

        def reap():
            if holder.poll() is None:
                holder.kill()
                holder.wait(self.BOUND)
        self.addCleanup(reap)
        self.wait(lambda: "start" in self.words(record)
                  or holder.poll() is not None, "the holder's script to start")
        self.assertIsNone(holder.poll(), "the holder exited before it read")
        script = int(dict(self.lines(record))["start"])
        events = self.lock_spy()
        box = {}
        waiter = threading.Thread(
            target=lambda: box.update(out=findingspass.run(rid)),
            daemon=True, name="waiter")
        waiter.start()
        self.wait(lambda: ("lock-asked", "waiter") in events,
                  "the waiter to ask for the queue lock")
        time.sleep(self.SETTLE)
        self.assertEqual([s for s, who in events if who == "waiter"],
                         ["lock-asked"],
                         "the waiter moved past a lock a live holder holds")
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(self.BOUND)
        # the killed holder's script is in its own session and outlives it
        try:
            os.kill(script, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.wait(lambda: ("lock-held", "waiter") in events,
                  "the waiter to take the lock the killed holder held")
        self.free(gates)
        waiter.join(self.BOUND)
        self.assertFalse(waiter.is_alive(), "the waiter never returned")
        self.assertEqual(holder.returncode, -signal.SIGKILL)
        got, why = box["out"]
        self.assertIsNone(why, why)
        self.assertEqual(self.line(rid), CLEAN_LINE)


class APartialReadAtTheTipCountsAsReadTest(RecheckBase):
    """CONDITION 2 of the reader's walk: the at-lock skip keeps main's rule.
    A COMPLETE OR PARTIAL read by this reader at this tip is the read the
    row is owed, so a second pass at that tip reads nothing; a note that
    read nothing (not run, timed out, failed) leaves the read owed."""

    def starts(self, record):
        try:
            with open(record, encoding="utf-8") as f:
                return [ln.split()[0] for ln in f].count("start")
        except FileNotFoundError:
            return 0

    def test_a_partial_read_is_not_read_again_at_its_tip(self):
        rid = self.row(lane="lane/partial-once")["id"]
        record = os.path.join(self.tmp, "partial-record")
        self.fixture(rc=2, body=_fp.PARTIAL, record=record)
        got, why = findingspass.run(rid)
        self.assertIsNone(why, why)
        self.assertEqual([(n["reviewed_tip"], n["outcome"])
                          for n in self.notes(rid)], [(self.side, "partial")])
        self.fixture(rc=0, body=_fp.CLEAN, record=record)
        got, why = findingspass.run(rid)
        self.assertIsNone(got)
        self.assertIn("already carries a read of %s" % self.side[:12], why)
        self.assertEqual(self.starts(record), 1,
                         "a partial read at this tip was read again")

    def test_a_note_that_read_nothing_leaves_the_read_owed(self):  # noqa: VACUOUS_ASSERTION — each None is a run's why channel, and the row's notes are asserted EQUAL to exactly not-run then complete
        rid = self.row(lane="lane/not-run-then-read")["id"]
        os.environ["HELM_LOCAL_REVIEW_SCRIPT"] = os.path.join(self.tmp,
                                                              "missing.py")
        got, why = findingspass.run(rid)
        self.assertIsNone(why, why)
        record = os.path.join(self.tmp, "owed-record")
        self.fixture(rc=0, body=_fp.CLEAN, record=record)
        got, why = findingspass.run(rid)
        self.assertIsNone(why, why)
        self.assertEqual([n["outcome"] for n in self.notes(rid)],
                         ["not-run", "complete"])
        self.assertEqual(self.starts(record), 1)


class TheLedgerWritePathRunsNoFindingsPassCodeTest(RecheckBase):
    """THE RULING DELETED THE RECONCILE: no dispatch-ledger write runs any
    findings-pass code. A hold, a release, a successor's cancel and a concur
    on a successor each write through `_ledger_write`, and a profiler on the
    writing thread sees no call into helm/findingspass.py."""

    def calls(self, act):
        """(what `act` returned, the names of every findings-pass function
        called while it ran on this thread)."""
        here = findingspass.enabled.__code__.co_filename
        seen = []

        def profile(frame, event, _arg):
            if event == "call" and frame.f_code.co_filename == here:
                seen.append(frame.f_code.co_name)
        prior = sys.getprofile()
        sys.setprofile(profile)
        try:
            got = act()
        finally:
            sys.setprofile(prior)
        return got, seen

    def test_no_ledger_write_calls_into_the_findings_pass(self):  # noqa: VACUOUS_ASSERTION — the empty call list is the contract; the control before it asserts the same profiler sees a call into the module, and the must-hit list proves every write ran
        # CONTROL: the profiler sees a call into the module
        got, seen = self.calls(findingspass.enabled)
        self.assertIn("enabled", seen)
        held = self.dispatch(ref=self.side, lane="lane/write-held",
                             kind="review")["id"]
        root = self.dispatch(ref=self.side, lane="lane/write-root",
                             kind="review")["id"]
        kid = self.dispatch(ref=self.side, lane="lane/write-root",
                            kind="review", supersedes=root)["id"]
        other = self.dispatch(ref=self.side, lane="lane/write-other",
                              kind="review")["id"]
        concur = self.dispatch(ref=self.side, lane="lane/write-other",
                               kind="review", supersedes=other)["id"]
        ran = []
        for name, act in (
                ("hold", lambda: dispatches.mark_hold(held, "waiting")),
                ("release", lambda: dispatches.mark_release(held)),
                ("cancel", lambda: dispatches.mark_cancel(kid, "moot")),
                ("concur", lambda: self.mark_verdict(
                    concur, self.side, "reviewed", polarity="concur"))):
            with self.subTest(write=name):
                (out, err), seen = self.calls(act)
                self.assertIsNone(err, err)
                self.assertEqual(seen, [], "the %s write ran findings-pass "
                                 "code" % name)
                ran.append(name)
        self.assertEqual(ran, ["hold", "release", "cancel", "concur"])


class TheStandingIsOnePredicateTest(unittest.TestCase):
    """The classifier over a folded row, for the shapes no writer in this
    fixture can reach cheaply: a closed row, a status the reducer does not
    have yet, and a row this helm cannot read in full."""

    TIP = "a" * 40

    def row(self, **fields):
        return dict({"id": "ab" * 16, "status": "open", "tip": self.TIP,
                     "seq": 3}, **fields)

    def test_a_closed_row_names_its_close_and_its_time(self):
        got = findingspass.standing(self.row(status="closed",
                                             close_reason="landed",
                                             close_ts="2026-09-26T10:00:00Z"))
        self.assertEqual((got.answer, got.word, got.at),
                         (ENDED, "landed", "2026-09-26T10:00:00Z"))

    def test_an_open_row_is_owed_and_a_row_it_cannot_read_is_unknown(self):
        self.assertEqual(findingspass.standing(self.row()).answer, OWED)
        odd = self.row(status="verdict", **{
            dispatches.UNKNOWN_KINDS_FIELD: ["a-future-kind"]})
        got = findingspass.standing(odd)
        self.assertEqual(got.answer, UNKNOWN)
        self.assertIn("a-future-kind", got.why)

    def test_the_ended_set_is_every_status_outside_the_note_states(self):  # noqa: VACUOUS_ASSERTION — every loop runs over a non-empty literal tuple and each subtest asserts an exact answer, and the must-hit count after the loops proves every cell ran
        ran = 0
        for status in ("open", "held", "verdict", "cancelled", "closed",
                       "a-status-the-reducer-learns-later"):
            with self.subTest(status=status):
                want = OWED if status in dispatches.FINDINGS_NOTE_STATES \
                    else ENDED
                self.assertEqual(findingspass.standing(
                    self.row(status=status)).answer, want)
                ran += 1
        # A RETIREMENT KEEPS THE ROW'S STATUS and still ends it for good
        for status in dispatches.FINDINGS_NOTE_STATES:
            with self.subTest(retired=status):
                got = findingspass.standing(self.row(
                    status=status, retired_admin=True,
                    retire_reason=dispatches.RETIRE_REASONS[0],
                    retire_ts="2026-09-26T11:00:00Z"))
                self.assertEqual((got.answer, got.word, got.at),
                                 (ENDED, "retired_admin",
                                  "2026-09-26T11:00:00Z"))
                ran += 1
        # A ROW A SUCCESSOR CARRIES OR A RETIP MOVED is still owed: nothing
        # about either is read here
        got = findingspass.standing(self.row(status="open", tip="b" * 40,
                                             retips=({"ts": "t"},)))
        self.assertEqual(got.answer, OWED)
        self.assertEqual(ran, 8)


if __name__ == "__main__":
    unittest.main()
