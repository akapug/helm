#!/usr/bin/env python3
"""THE FIRST LEDGER READ AFTER EACH LAND IS TIMED AGAINST THE LANDED CODE, ONCE
PER HEAD, AND A READ THAT DID NOT HAPPEN IS NEVER A TIMING (task/3538).

`helm/postland.py`. Every arm reads what the recorder wrote back through the
verb's own reader (`postland.rows`) or through the verb's own output, so a
recorder that wrote nothing, wrote twice, merged two heads, or wrote a timing
for a read of other code or for a read that did not happen is red. The three
review findings the recorder answers each have their own class: THE CODE THAT
RAN (a), ONE ROW PER HEAD (b), and A READ THAT DID NOT HAPPEN (c).

THE WORLD. A temp HELM_HOME per arm, and a temp git checkout that stands in
for the running helm's own checkout (`postland._code_root`): one commit
carrying `bin/helm` and a `helm/` package. The read is `dispatches.snapshot`;
the arms that are not about the fold checkpoint replace it with a stand-in
that counts its calls and answers what the arm says, and the clock is
injected, so no arm reads wall time. The two arms about WARM and COLD read a
real ledger through the real fold (`tests.test_foldckpt.FoldCheckpointBase`).
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from helm import (autoland, dispatches, foldcheck, foldckpt, landreq,
                  postland, registry, landreq_cli)
# The module, never its TestCase: tests/test_suite_collection.py says why.
import tests.test_foldckpt as fc


def _git(cwd, *args):
    return subprocess.run(["git", "-C", cwd] + list(args),
                          capture_output=True, text=True,
                          check=True).stdout.strip()


class Clock(object):
    """An injected monotonic clock: each call answers the next reading."""

    def __init__(self, *readings):
        self.readings = list(readings)

    def __call__(self):
        return self.readings.pop(0)


class Base(unittest.TestCase):
    """A temp home, and a temp checkout standing in for the running helm's."""

    def setUp(self):
        self.tmp = os.path.realpath(
            tempfile.mkdtemp(prefix="helm-test-postland-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "home")})
        env.start()
        self.addCleanup(env.stop)
        self.code = os.path.join(self.tmp, "code")
        for sub in ("bin", "helm"):
            os.makedirs(os.path.join(self.code, sub))
        _git(self.tmp, "init", "-q", self.code)
        _git(self.code, "config", "user.name", "t")
        _git(self.code, "config", "user.email", "t@example.invalid")
        _git(self.code, "config", "commit.gpgsign", "false")
        self.write("bin/helm", "# the entry script\n")
        self.write("helm/__init__.py", "")
        self.write("helm/mod.py", "X = 1\n")
        self.head = self.commit("the landed head")
        root = mock.patch.object(postland, "_code_root", lambda: self.code)
        root.start()
        self.addCleanup(root.stop)
        # THE READ, stood in for: what it answered, and a hook that runs
        # while it runs (`during`), once.
        self.snapshots = []
        self.answer = ({}, None)
        self.during = None
        read = mock.patch.object(dispatches, "snapshot", self.fake_read)
        read.start()
        self.addCleanup(read.stop)

    def fake_read(self):
        self.snapshots.append(1)
        during, self.during = self.during, None
        if during is not None:
            during()
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def write(self, rel, text):
        with open(os.path.join(self.code, rel), "w", encoding="utf-8") as f:
            f.write(text)

    def commit(self, message):
        _git(self.code, "add", "-A")
        _git(self.code, "commit", "-q", "--allow-empty", "-m", message)
        return _git(self.code, "rev-parse", "HEAD")

    def rows(self):
        return postland.rows(dispatches.ledger_path())

    def plant(self, **row):
        """One raw line in the log, written as another recorder would."""
        path = postland.log_path(dispatches.ledger_path())
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    def assert_unmeasured(self, row, words):
        self.assertIsNotNone(row, "no row was written")
        self.assertEqual(row["status"], postland.UNMEASURED, row)
        self.assertIsNone(row["seconds"], row)
        self.assertIn(words, row["reason"])


class TheCodeThatRan(Base):
    """Finding (a): a timing is recorded only against the code that ran, and
    that code is the landed head's own tree."""

    def test_a_checkout_at_the_head_times_the_read(self):
        row = postland.record(self.head, "hand", clock=Clock(100.0, 104.25))
        self.assertEqual(self.rows(), [row])
        self.assertEqual((row["status"], row["head"], row["by"],
                          row["seconds"]),
                         (postland.MEASURED, self.head, "hand", 4.25))
        self.assertIsNone(row["reason"])
        self.assertEqual(len(self.snapshots), 1)

    def test_a_checkout_that_is_not_at_the_head_times_nothing(self):
        """A `foldcheck --apply` run before the pull, or a helm of another
        tree: the code that would read is not the code the land put there."""
        behind = self.head
        landed = self.commit("the land")
        _git(self.code, "checkout", "-q", behind)
        row = postland.record(landed, "foldcheck", clock=Clock(0.0, 9.0))
        self.assert_unmeasured(row, "not the landed head")
        self.assertEqual(self.snapshots, [], "a read of other code was timed")
        self.assertEqual(self.rows(), [row])
        # CONTROL, same world: the commit the checkout does stand at is timed.
        got = postland.record(behind, "foldcheck", clock=Clock(0.0, 9.0))
        self.assertEqual((got["status"], got["seconds"]),
                         (postland.MEASURED, 9.0))

    def test_uncommitted_code_is_not_the_heads_code(self):  # noqa: VACUOUS_ASSERTION — the control times the same change once it is committed as a head
        self.write("helm/mod.py", "X = 2\n")
        row = postland.record(self.head, "hand", clock=Clock(0.0, 1.0))
        self.assert_unmeasured(row, "uncommitted")
        self.assertEqual(self.snapshots, [])
        # CONTROL: the same change, committed, is a head of its own and timed
        landed = self.commit("the change")
        got = postland.record(landed, "hand", clock=Clock(0.0, 1.0))
        self.assertEqual(got["status"], postland.MEASURED)

    def test_an_untracked_module_is_not_the_heads_code(self):  # noqa: VACUOUS_ASSERTION — the control times the head once the module is gone
        self.write("helm/extra.py", "Y = 1\n")
        row = postland.record(self.head, "hand", clock=Clock(0.0, 1.0))
        self.assert_unmeasured(row, "uncommitted")
        self.assertIn("helm/extra.py", row["reason"])
        self.assertEqual(self.snapshots, [])
        os.remove(os.path.join(self.code, "helm", "extra.py"))
        landed = self.commit("another head")
        self.assertEqual(postland.record(landed, "hand",
                                         clock=Clock(0.0, 1.0))["status"],
                         postland.MEASURED, "control: a clean checkout")

    def test_a_process_whose_code_changed_on_disk_is_unmeasured(self):
        """Auto-land's tick imported the tree its push replaced: whatever
        its checkout says, the code it runs is the code it imported."""
        with mock.patch.object(foldckpt, "policy", return_value=None):
            row = postland.record(self.head, "autoland",
                                  clock=Clock(0.0, 3.0))
        self.assert_unmeasured(row, "changed on disk")
        landed = self.commit("another head")
        self.assertEqual(postland.record(landed, "autoland",
                                         clock=Clock(0.0, 3.0))["status"],
                         postland.MEASURED, "control: the same process")

    def test_a_checkout_that_moves_during_the_read_is_unmeasured(self):
        self.during = lambda: self.commit("a land while the read ran")
        row = postland.record(self.head, "hand", clock=Clock(0.0, 3.0))
        self.assert_unmeasured(row, "during the read")
        self.assertEqual(len(self.snapshots), 1)

    def test_an_abbreviated_head_is_recorded_whole(self):
        row = postland.record(self.head[:12], "hand", clock=Clock(0.0, 1.0))
        self.assertEqual(row["head"], self.head)
        self.assertEqual(row["status"], postland.MEASURED)


class OneRowPerHead(Base):
    """Finding (b): one row per landed head, and never one row for two."""

    def test_recording_a_head_again_adds_no_row_and_reads_nothing(self):
        first = postland.record(self.head, "hand", clock=Clock(0.0, 2.0))
        for again in (self.head, self.head[:12]):
            got = postland.record(again, "autoland", clock=Clock(0.0, 7.0))
            self.assertEqual(got, first)
        self.assertEqual(self.rows(), [first])
        self.assertEqual(len(self.snapshots), 1,
                         "a head that has its row was read again")

    def test_two_recorders_racing_on_one_head_leave_one_row(self):
        """Auto-land's new process and a hand `foldcheck --apply` each find
        no row before their reads; the second to append must find the
        first's row, under the lock, and answer it."""
        inner = []
        self.during = lambda: inner.append(postland.record(
            self.head, "autoland", clock=Clock(0.0, 1.0)))
        outer = postland.record(self.head, "foldcheck", clock=Clock(0.0, 5.0))
        self.assertEqual(len(inner), 1, "control: the race ran")
        self.assertEqual(self.rows(), [inner[0]])
        self.assertEqual(outer, inner[0],
                         "the loser must answer the row that stands")
        with open(postland.log_path(dispatches.ledger_path()),
                  encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 1)

    def test_a_row_that_names_a_head_by_a_prefix_stands_for_no_head(self):
        """A row keyed by an abbreviation, or by another full id that shares
        a prefix with this head, is not this head's row."""
        cousin = self.head[:12] + ("0" if self.head[12] != "0" else "1") * 28
        self.plant(head=self.head[:12], status=postland.MEASURED, seconds=1.0,
                   at=1e9, by="hand")
        self.plant(head=cousin, status=postland.MEASURED, seconds=2.0,
                   at=1e9, by="hand")
        row = postland.record(self.head, "hand", clock=Clock(0.0, 3.0))
        self.assertEqual(row["head"], self.head)
        self.assertEqual(row["seconds"], 3.0)
        self.assertEqual([r["head"] for r in self.rows()], [cousin, self.head])
        self.assertEqual(len(self.snapshots), 1)


class AReadThatDidNotHappen(Base):
    """Finding (c): an unavailable or failed read is UNMEASURED with its
    reason, and never counted as a timing."""

    def test_an_unavailable_ledger_is_unmeasured(self):
        self.answer = (None, "the ledger could not be read (EACCES)")
        row = postland.record(self.head, "hand", clock=Clock(0.0, 0.5))
        self.assert_unmeasured(row, "the ledger could not be read (EACCES)")
        self.assertEqual(self.rows(), [row])

    def test_a_read_that_raises_is_unmeasured(self):
        self.answer = RuntimeError("boom")
        row = postland.record(self.head, "hand", clock=Clock(0.0, 0.5))
        self.assert_unmeasured(row, "RuntimeError: boom")

    def test_the_figures_count_measured_reads_only(self):
        for n, (status, seconds, road) in enumerate((
                (postland.MEASURED, 2.0, "warm"),
                (postland.MEASURED, 51.0, "cold"),
                (postland.MEASURED, 4.0, "warm"),
                # a stray timing on an unmeasured row, and a measured row
                # with no number: neither is a timing
                (postland.UNMEASURED, 0.1, None),
                (postland.MEASURED, None, "warm"))):
            self.plant(head="%x" % n * 40, status=status, seconds=seconds,
                       road=road, at=1e9 + n, by="hand",
                       reason=None if status == postland.MEASURED else "gone")
        got = postland.summary(self.rows())
        self.assertEqual(got, {"rows": 5, "measured": 3, "unmeasured": 2,
                               "median_s": 4.0, "max_s": 51.0, "cold": 1,
                               "under_bar": 2, "bar_s": 30.0})


class TheRecorderRunsInTheLandedCheckout(Base):
    """`take`: both land paths time the read in a NEW process of the landed
    checkout's own helm, and a recorder that cannot leave a row is itself a
    row that says why."""

    def setUp(self):
        super().setUp()
        self.write("helm/postland.py", "# the recorder\n")
        self.seen = os.path.join(self.tmp, "argv.json")
        self.child("""
import json, os, sys
with open(%(seen)r, "w", encoding="utf-8") as f:
    json.dump([os.getcwd()] + sys.argv, f)
head = sys.argv[sys.argv.index("--record") + 1]
by = sys.argv[sys.argv.index("--by") + 1]
os.makedirs(os.path.dirname(%(log)r), exist_ok=True)
with open(%(log)r, "a", encoding="utf-8") as f:
    f.write(json.dumps({"head": head, "by": by, "status": "measured",
                        "seconds": 1.5, "road": "warm", "at": 1e9}) + "\\n")
""")

    def child(self, text):
        self.write("bin/helm", text % {
            "seen": self.seen,
            "log": postland.log_path(dispatches.ledger_path())})

    def test_the_landed_checkouts_own_helm_records_in_a_new_process(self):
        row = postland.take(self.code, self.head[:12], "autoland")
        with open(self.seen, encoding="utf-8") as f:
            self.assertEqual(json.load(f), [
                self.code, os.path.join(self.code, "bin", "helm"), "lr",
                "postland", "--record", self.head, "--by", "autoland",
                "--repo", self.code])
        self.assertEqual(self.rows(), [row])
        self.assertEqual((row["head"], row["by"], row["seconds"]),
                         (self.head, "autoland", 1.5))
        self.assertEqual(self.snapshots, [], "the parent timed a read")

    def test_a_head_that_has_its_row_starts_nothing(self):  # noqa: VACUOUS_ASSERTION — the first take is asserted to start the recorder under the same spy
        with mock.patch.object(postland, "_start",
                               wraps=postland._start) as run:
            first = postland.take(self.code, self.head, "autoland")
            self.assertEqual(run.call_count, 1, "control: the first starts it")
            again = postland.take(self.code, self.head, "foldcheck")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(again, first)
        self.assertEqual(len(self.rows()), 1)

    def test_a_recorder_that_does_not_finish_is_unmeasured(self):
        with mock.patch.object(postland, "_start",
                               side_effect=subprocess.TimeoutExpired(
                                   ["helm"], postland.TIMEOUT_S)):
            row = postland.take(self.code, self.head, "autoland")
        self.assert_unmeasured(row, "did not finish in %d s"
                               % postland.TIMEOUT_S)
        self.assertEqual(self.rows(), [row])

    def test_a_recorder_that_fails_is_unmeasured_in_its_own_words(self):
        self.child("import sys\nsys.stderr.write('Traceback\\n"
                   "ImportError: no module named x\\n')\nsys.exit(3)\n")
        row = postland.take(self.code, self.head, "foldcheck")
        self.assert_unmeasured(row, "exited 3: ImportError: no module named x")
        self.assertEqual(row["by"], "foldcheck")

    def test_a_checkout_without_the_recorder_is_unmeasured(self):
        """An adopter's repository with a `bin/helm` wrapper is no helm tree
        the land put this recorder in."""
        os.remove(os.path.join(self.code, "helm", "postland.py"))
        with mock.patch.object(postland, "_start") as run:
            row = postland.take(self.code, self.head, "foldcheck")
        self.assertFalse(run.called)
        self.assert_unmeasured(row, "carries no post-land recorder")

    def test_a_failure_inside_the_recorder_never_raises(self):
        with mock.patch.object(postland, "_start",
                               side_effect=ValueError("bad argv")):
            row = postland.take(self.code, self.head, "autoland")
        self.assert_unmeasured(row, "ValueError")

    def test_auto_land_takes_the_read_of_the_shared_checkout(self):
        with mock.patch.object(postland, "take") as take:
            autoland.Ops().postland(self.code, self.head)
        take.assert_called_once_with(self.code, self.head, "autoland")


class FoldcheckApplyTakesTheRead(Base):

    def foldcheck(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = landreq.cmd_lr(["foldcheck", self.head, "--repo", self.code,
                                 "--no-fetch"] + list(extra))
        return rc, out.getvalue(), err.getvalue()

    def test_apply_takes_the_read_once_the_rungs_pass_and_a_dry_run_none(self):
        order = []
        real = foldcheck.check

        def check(*args, **kw):
            order.append("check")
            return real(*args, **kw)

        with mock.patch.object(postland, "take",
                               side_effect=lambda *a: order.append(a)), \
                mock.patch.object(foldcheck, "check", check), \
                mock.patch.object(foldcheck, "ok", return_value=True), \
                mock.patch.object(landreq_cli, "_fold_proven",
                                  side_effect=lambda *a: order.append("fold")
                                  or 0):
            self.foldcheck()
            self.assertEqual(order, ["check", "fold"])
            self.foldcheck("--apply")
        self.assertEqual(order, ["check", "fold", "check",
                                 (self.code, self.head, "foldcheck"), "fold"])

    def test_a_head_the_rungs_refuse_writes_no_row_and_its_land_is_timed(self):
        """A `foldcheck --apply` of a head that has not landed (its rungs
        refuse) must not take the read: a head's first row is final, so an
        UNMEASURED row written then would stand for the real land."""
        with mock.patch.object(foldcheck, "ok", return_value=False):
            rc, _out, _err = self.foldcheck("--apply")
        self.assertEqual(rc, 1, "control: the rungs refused the head")
        self.assertEqual(self.rows(), [], "a refused head was timed")
        row = postland.record(self.head, "autoland", clock=Clock(0.0, 2.0))
        self.assertEqual(row["status"], postland.MEASURED, row)
        self.assertEqual(row["seconds"], 2.0)
        self.assertEqual(self.rows(), [row])

    def test_a_recorder_that_cannot_import_never_stops_the_fold(self):
        """Once the rungs pass, a hand `foldcheck --apply` whose recorder
        cannot even be imported says one line and still folds."""
        folded = []
        real_import = __import__

        def imp(name, globals=None, locals=None, fromlist=(), level=0):
            if level and "postland" in (fromlist or ()):
                raise ImportError("no postland in this tree")
            return real_import(name, globals, locals, fromlist, level)

        with mock.patch.object(foldcheck, "ok", return_value=True), \
                mock.patch.object(landreq_cli, "_fold_proven",
                                  side_effect=lambda *a: folded.append(a)
                                  or 0), \
                mock.patch("builtins.__import__", imp):
            rc, _out, err = self.foldcheck("--apply")
        self.assertEqual(len(folded), 1, "the fold was skipped")
        self.assertNotIn("Traceback", err)
        self.assertIn("could not run: ImportError", err)
        self.assertEqual(self.rows(), [])

    def test_a_record_that_fails_never_changes_the_folds_answer(self):
        self.write("helm/postland.py", "# the recorder\n")
        with mock.patch.object(foldcheck, "ok", return_value=True), \
                mock.patch.object(landreq_cli, "_fold_proven", return_value=0):
            with mock.patch.object(postland, "take", return_value=None):
                without = self.foldcheck("--apply")
            with mock.patch.object(postland, "_start",
                                   side_effect=ValueError("bad argv")):
                rc, out, err = self.foldcheck("--apply")
        self.assertEqual((rc, out), without[:2],
                         "a failed record changed the fold's answer")
        self.assertIn("UNMEASURED", err)
        self.assert_unmeasured(self.rows()[0], "ValueError")


class AHeldLogLockNeverStallsTheLand(Base):
    """Another PROCESS holds the log's lock through every window below."""

    def hold_the_lock(self):
        lock = postland._lock_path(dispatches.ledger_path())
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl, os, sys\n"
             "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
             "fcntl.flock(fd, fcntl.LOCK_EX)\n"
             "print('held', flush=True)\n"
             "sys.stdin.read()\n", lock],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.stdin.close)
        self.assertEqual(holder.stdout.readline().strip(), b"held")
        return holder

    def release(self, holder):
        holder.stdin.close()
        holder.wait()

    def test_take_gives_up_on_a_lock_another_process_holds(self):
        """`take` returns None and says one line instead of waiting."""
        holder = self.hold_the_lock()
        err = io.StringIO()
        with mock.patch.object(postland, "LOCK_WAIT_S", 0.2), \
                contextlib.redirect_stderr(err):
            row = postland.take(self.code, self.head, "autoland")
        self.assertIsNone(row)
        self.assertEqual(self.rows(), [])
        said = err.getvalue().strip().splitlines()
        self.assertEqual(len(said), 1, said)
        self.assertIn("stayed held", said[0])
        self.release(holder)
        self.assertIsNotNone(postland.take(self.code, self.head, "autoland"),
                             "control: a free lock records")

    def test_the_recorder_exits_distinctly_when_the_lock_stays_held(self):
        """The child's own exit code says the lock stayed held, so its
        parent can tell that from a recorder that failed."""
        holder = self.hold_the_lock()
        err = io.StringIO()
        with mock.patch.object(postland, "LOCK_WAIT_S", 0.2), \
                contextlib.redirect_stderr(err):
            rc = postland.cmd(["--record", self.head, "--by", "hand"])
        self.assertEqual(rc, postland.EXIT_LOCK_HELD)
        self.assertIn("stayed held", err.getvalue())
        self.assertEqual(self.rows(), [])
        self.release(holder)
        with contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(postland.cmd(["--record", self.head]), 0,
                             "control: a free lock records")

    def test_a_child_that_found_the_lock_held_is_not_waited_for_twice(self):
        """A child that exited EXIT_LOCK_HELD leaves no UNMEASURED row, and
        the parent takes no second lock wait of its own: a lock freed
        between the two waits must not publish a final UNMEASURED row for a
        read that completed, and a held one must not delay the land twice."""
        self.write("helm/postland.py", "# the recorder\n")
        holder = self.hold_the_lock()

        class Done(object):
            returncode = postland.EXIT_LOCK_HELD
            stdout, stderr = b"", b"helm lr postland: ... stayed held\n"

        waits = []
        real = postland._locked
        err = io.StringIO()
        with mock.patch.object(postland, "_start", return_value=Done()), \
                mock.patch.object(postland, "_locked",
                                  side_effect=lambda *a: waits.append(a)
                                  or real(*a)), \
                contextlib.redirect_stderr(err):
            row = postland.take(self.code, self.head, "autoland")
        self.assertIsNone(row)
        self.assertEqual(waits, [], "the parent waited on the lock again")
        self.release(holder)
        self.assertEqual(self.rows(), [],
                         "a lock miss was published as a final row")
        self.assertIn("lock held", err.getvalue())


class TheVerb(Base):

    def run_verb(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = landreq.cmd_lr(["postland"] + list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_the_window_prints_the_measured_figures_against_the_bar(self):
        self.assertEqual(self.run_verb()[:2],
                         (0, "helm lr postland: no post-land read recorded\n"))
        for n, (seconds, road) in enumerate(((2.0, "warm"), (51.0, "cold"),
                                             (4.0, "warm"))):
            self.plant(head="abc"[n] * 40, status=postland.MEASURED,
                       seconds=seconds, road=road, at=1e9 + n, by="hand",
                       ts="T%d" % n, load=[9, 8, 7], cpus=8, reason=None)
        self.plant(head="d" * 40, status=postland.UNMEASURED, seconds=None,
                   road=None, at=1e9 + 3, by="autoland", ts="T3",
                   reason="the read did not finish in 600 s")
        rc, out, _err = self.run_verb()
        self.assertEqual(rc, 0)
        self.assertIn("4 land(s): 3 measured, median 4.0 s, max 51.0 s, "
                      "1 cold, 2 of 3 under the 30 s bar; 1 UNMEASURED", out)
        self.assertIn("UNMEASURED  by autoland  (the read did not finish in "
                      "600 s)", out)
        rc, out, _err = self.run_verb("--json")
        got = json.loads(out)
        self.assertEqual(got["summary"], {
            "rows": 4, "measured": 3, "unmeasured": 1, "median_s": 4.0,
            "max_s": 51.0, "cold": 1, "under_bar": 2, "bar_s": 30.0})
        self.assertEqual([r["head"][0] for r in got["rows"]],
                         ["a", "b", "c", "d"])
        rc, out, _err = self.run_verb("--hours", "1")
        self.assertIn("no post-land read recorded in the last 1 h", out)

    def test_a_stray_flag_refuses(self):
        for args in (("--bogus",), ("--record",), ("--hours", "soon"),
                     ("--record", self.head, "--bogus")):
            self.assertEqual(self.run_verb(*args)[0], 2, args)

    def test_record_through_the_verb(self):
        rc, out, _err = self.run_verb("--record", self.head, "--by",
                                      "autoland", "--repo", self.code)
        self.assertEqual(rc, 0, out)
        self.assertIn(self.head[:12], out)
        self.assertEqual([(r["by"], r["status"]) for r in self.rows()],
                         [("autoland", postland.MEASURED)])

    def test_the_log_beside_the_store_is_declared(self):  # noqa: VACUOUS_ASSERTION — the log and its lock are asserted present on disk first, so an empty squatter list means the registry names them
        postland.record(self.head, "hand", clock=Clock(0.0, 1.0))
        path = postland.log_path(dispatches.ledger_path())
        self.assertTrue(os.path.exists(path))
        self.assertTrue(any(name.endswith(".postland.lock") for name in
                            os.listdir(os.path.dirname(path))))
        _rows, squat = registry.projection_survey()
        self.assertEqual([f for f in squat["home"] if "postland" in f], [])


class TheRoadTheReadTook(fc.FoldCheckpointBase):
    """WARM when the fold checkpoint restored, COLD with the miss that said
    why when the ledger was replayed: the real fold, over a real ledger."""

    def setUp(self):
        super().setUp()
        root = mock.patch.object(postland, "_code_root", lambda: self.repo)
        root.start()
        self.addCleanup(root.stop)

    def test_a_restored_read_is_warm(self):
        self.carried()
        self.settle()
        head = self.git("rev-parse", "HEAD")
        row = postland.record(head, "hand", repo=self.repo)
        self.assertEqual((row["status"], row["road"], row["why"]),
                         (postland.MEASURED, postland.WARM, None))
        self.assertIsInstance(row["seconds"], float)

    def test_a_replay_is_cold_with_the_miss_that_caused_it(self):  # noqa: VACUOUS_ASSERTION — the row's road and reason are asserted to exact values and the next read asserted restored
        self.carried()
        self.settle()
        shutil.rmtree(self.store_dir())
        head = self.git("rev-parse", "HEAD")
        row = postland.record(head, "hand", repo=self.repo)
        self.assertEqual((row["status"], row["road"]),
                         (postland.MEASURED, postland.COLD))
        self.assertIn(row["why"], (foldckpt.NO_CHECKPOINT,
                                   foldckpt.WRITTEN_BY_OTHER_CODE))
        # the read it timed wrote the checkpoint the next reader restores
        roads, _out = self.assert_equivalent()
        self.assert_restored(roads)


if __name__ == "__main__":
    unittest.main()
