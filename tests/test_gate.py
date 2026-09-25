#!/usr/bin/env python3
"""helm gate: a suite result is a claim only when MINTED, and a verdict either
binds one or says out loud that it did not.

Every test here plants a REAL git repo and runs a REAL child process, because
the thing under test is precisely the difference between a claim and a model of
one — mocking the run would rebuild the defect inside the test."""
import ast
import contextlib
import hashlib
import inspect
import io
import textwrap
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from helm import (dispatches, gate, gateauthority, gateimport,
                  gatechild, gatetestrecord, landreq, vcs)
from tests._gate_receipt import serial_process
from tests import _gate_supervisor
from tests import HostAdmissionAsked, HostAdmissionRefused
import tests
from tests._gate_supervisor import require_supervisor
from tests._tmphome import healthy_tmp, pin_admission

# THE REAL DOORS, taken at import: every GateBase fixture pins `_admit_suite`
# and `_scratch_preflight` to a fixture box, and the arms about the host's own
# reading, and the tripwire arms, need the unpinned functions.
_REAL_ADMIT = gate._admit_suite
_REAL_PREFLIGHT = gate._scratch_preflight

ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CHAT_DIR", "HELM_CHAT_ROOM",
            "HELM_CHAT_NAME", "HELM_CELL_BIN", "HELM_CELL_PROFILE",
            "HELM_CHAT_NODE_URL", "HELM_VERDICT_ROOM", "HELM_PROC",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            # BOTH CROSS-TREE KNOBS ARE SCRATCH. They change whether the mint
            # refuses, so a suite that inherited them from the operator's shell
            # would pass or fail by what the host happened to export — the same
            # host-coupled fixture class that split local-green from fab-red.
            "HELM_NO_TREE_WARNING", "HELM_CROSS_TREE_GATE")

# A child that prints a unittest summary and nothing else — the shape the
# parser must read, without paying 5115 tests to produce it.
def _emit(*lines):
    return ["-c", "import sys; print(%r, file=sys.stderr)" % "\n".join(lines)]


def _emit_exit(rc, *lines):
    return ["-c", "import sys; print(%r, file=sys.stderr); raise SystemExit(%d)"
            % ("\n".join(lines), rc)]


def _verdicts(order, anchors=None):
    """{id: (append index, content anchor)} — the shape _snapshot builds.

    The anchor defaults to a stable digest of the id, so a test that moves a
    row's POSITION keeps its IDENTITY (that is compaction) while a test that
    wants a DIFFERENT event passes a different anchor (that is recreation)."""
    anchors = anchors or {}
    return {rid: (pos, anchors.get(rid) or hashlib.blake2b(
        rid.encode("utf-8"), digest_size=16).hexdigest())
        for rid, pos in order.items()}


class GateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-gate-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        # ONE ENVIRONMENT-RESTORATION OWNER, AND IT RUNS LAST. Registered here,
        # before any helper touches the environment, so LIFO makes it the final
        # word: unittest runs every addCleanup AFTER tearDown, so restoring the
        # snapshot in tearDown put it BEFORE the helpers' cleanups instead and a
        # helper popped an incoming HELM_CROSS_TREE_GATE=1 that this snapshot had
        # just put back. See `_tmphome.own_env`.
        self.addCleanup(self._restore_env)
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_ROOM"] = "main"
        # THE CHAT DIR IS SCRATCH, LIKE HELM_HOME ALWAYS WAS. Popping the key
        # without setting one left every verdict's attestation posting into the
        # REAL fleet room: measured 2026-07-31, one `python3 -m unittest
        # tests.test_gate` appended 9 lines to /dev/shm/helm-chat/main.jsonl.
        # A suite that writes to the thing the fleet is reading is not a suite,
        # it is a second agent — and the noise lands on whoever is on watch.
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        # dispatches.add/send now REFUSE an identityless author (the family
        # floor is never an author) — the fixture declares one,
        # exactly like a real reviewer's pane does.
        os.environ["HELM_CHAT_NAME"] = "gate-fixture"
        # HELM_PROC keeps the census READERS on an empty fake proc tree. It
        # never reached ADMISSION, which reads the host by design, so these
        # runs were red whenever the node was full (task/1740, 25 arms
        # measured); `pin_admission` below hands the real door a fixture box.
        os.environ["HELM_PROC"] = os.path.join(self.tmp, "proc")
        os.makedirs(os.environ["HELM_PROC"])
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self._git("init", "-q", "-b", "main")
        # THIS FIXTURE IS ITS OWN PROJECT: without the pin the dispatch
        # write door refuses every row here as FOREIGN — a true refusal
        # that says nothing about what these arms test.
        from tests._tmphome import helm_tree, pin_admission, pin_dispatch_home
        self.admission_proc, self.admissions = pin_admission(
            self, proc=os.environ["HELM_PROC"])
        self._real_home_repo_id = pin_dispatch_home(self, self.repo)
        self._git("config", "user.email", "gate@test")
        self._git("config", "user.name", "gate test")
        # THIS FIXTURE REPO STANDS IN FOR A TREE THAT SHIPS HELM, which is
        # the only tree whose whole-suite command helm may assume — see
        # `helm_tree`. An adopter project declares its own instead.
        helm_tree(self, self.repo)
        # AND THE SCRATCH ROOT TOO, for the two cross-tree arms that gate
        # `self.tmp` directly with `_acquire_gate` mocked: their subject is the
        # ORDER of the refusals, so the command must resolve before the FIFO.
        helm_tree(self, self.tmp)
        with open(os.path.join(self.repo, "a.txt"), "w") as fh:
            fh.write("one\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "first")
        # THE FIXTURE STAYS ON A LANE BRANCH, and `main` stays behind it. A
        # reviewed tip that is already ON trunk is LANDED, and landreq reports
        # that instead of the review state — the land-path tests below would
        # have been asserting against a lifecycle they never reached.
        self._git("checkout", "-q", "-b", "lane/probe")
        with open(os.path.join(self.repo, "b.txt"), "w") as fh:
            fh.write("lane\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "lane work")
        self.head = self._git("rev-parse", "HEAD")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _restore_env(self):
        """The fixture's whole ENV_KEYS snapshot, put back last."""
        for key, val in self.prior.items():
            os.environ.pop(key, None)
            if val is not None:
                os.environ[key] = val

    def _git(self, *args):
        return subprocess.run(("git",) + args, cwd=self.repo, text=True,
                              capture_output=True).stdout.strip()

    def _dirty(self):
        with open(os.path.join(self.repo, "a.txt"), "a") as fh:
            fh.write("edit\n")

    def gitdir(self):
        return os.path.realpath(os.path.join(
            self.repo, self._git("rev-parse", "--git-common-dir")))

    def descendant_receipt(self, receipt_ts="2026-08-02T00:00:02Z"):
        """A real whole-suite receipt at integration B containing lane tip A."""
        reviewed = self.head
        self._git("checkout", "-q", "-b", "integration/gate", reviewed)
        with open(os.path.join(self.repo, "descendant.txt"), "w") as fh:
            fh.write("later integration tree\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "descendant gate tree")
        descendant = self._git("rev-parse", "HEAD")
        self.assertNotEqual(descendant, reviewed,
                            "fixture broken: the integration commit IS the "
                            "reviewed tip, so no arm using this receipt is "
                            "measuring descendant binding at all")
        self.assertEqual(subprocess.run(
            ("git", "merge-base", "--is-ancestor", reviewed, descendant),
            cwd=self.repo).returncode, 0,
            "fixture broken: the reviewed commit is NOT an ancestor of the "
            "integration commit, so bind's descendant rung is never reached")
        with serial_process(ran=9), \
                mock.patch.object(gate.pk, "now_ts", return_value=receipt_ts):
            row, err = gate.run(repo=self.repo)
        self._git("checkout", "-q", "lane/probe")
        self.assertIsNone(err, err)
        self.assertEqual(row["head"], descendant,
                         "fixture broken: the receipt did not bind the "
                         "INTEGRATION commit, so every arm taking this "
                         "receipt reads a binding this helper did not set up")
        self.assertEqual(self._git("rev-parse", "lane/probe"), reviewed,
                         "fixture broken: lane/probe no longer points at the "
                         "reviewed tip, so the descendant relationship the "
                         "callers rely on is not the one this built")
        self.assertEqual(self._git("rev-parse", "integration/gate"), descendant,
                         "fixture broken: integration/gate moved off the "
                         "commit the receipt binds, so the two refs no longer "
                         "describe one arrangement")
        return reviewed, descendant, row

    def train_receipt(self, picks=True, reverse=False,
                      receipt_ts="2026-08-02T00:00:02Z"):
        """Two reviewed commits embedded between unrelated train cars."""
        base = self._git("rev-parse", "main")
        first = self.head
        with open(os.path.join(self.repo, "review-two.txt"), "w") as fh:
            fh.write("reviewed second patch\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "reviewed second patch")
        second = self._git("rev-parse", "HEAD")
        reviewed = second
        self._git("checkout", "-q", "-b", "integration/train", base)
        for i in range(12):
            name = "train-before-%02d.txt" % i
            with open(os.path.join(self.repo, name), "w") as fh:
                fh.write(name + "\n")
            self._git("add", "-A")
            self._git("commit", "-qm", name)
        if picks:
            commits = [first, second]
            if reverse:
                commits.reverse()
            for commit in commits:
                self._git("cherry-pick", commit)
        for i in range(12):
            name = "train-after-%02d.txt" % i
            with open(os.path.join(self.repo, name), "w") as fh:
                fh.write(name + "\n")
            self._git("add", "-A")
            self._git("commit", "-qm", name)
        train = self._git("rev-parse", "HEAD")
        self.assertNotEqual(subprocess.run(
            ("git", "merge-base", "--is-ancestor", reviewed, train),
            cwd=self.repo).returncode, 0,
            "fixture broken: the reviewed commit is an ancestor of the train, "
            "so patch containment is never consulted. THE USUAL CAUSE IS A "
            "SECOND CALL: train_receipt is NOT IDEMPOTENT — it cuts "
            "integration/train from the same base every time, so calling it "
            "twice in one method leaves the second reviewed commit aboard the "
            "FIRST train. One call per method.")
        tree = self._git("rev-parse", "HEAD^{tree}")
        ident = gate.interpreter()
        with mock.patch.object(gate.pk, "now_ts", return_value=receipt_ts):
            row, minted, err = gate._mint_result(
                self.repo, train, tree, False, ident,
                [ident["executable"]] + list(gateauthority.SERIAL_ARGV),
                True, None, 0, 0, "Ran 9 tests in 0.2s\n\nOK\n")
        self._git("checkout", "-q", "lane/probe")
        self.assertTrue(minted, err)
        return base, reviewed, train, row

    def rekeyed_train_receipt(self, gap, receipt_ts="2026-08-02T00:00:02Z"):
        """A train that CARRIES the reviewed change but re-keys its patch-id.

        The reviewed car edits line 20 of a shared file. A train car edits
        line (20 - gap) of the SAME file and lands FIRST, so cherry-picking
        the reviewed car onto it produces a diff whose CONTEXT lines differ —
        and `git patch-id --stable` hashes context. In THIS geometry the id
        changes at gap 3 and does not at gap 4; that is measured here, not a
        general distance law. Returns (base, reviewed, train, row, picked_pid,
        original_pid) so the caller can assert on the re-keying itself and not
        merely on the message.
        """
        base = self._git("rev-parse", "main")
        shared = os.path.join(self.repo, "shared.txt")
        with open(shared, "w") as fh:
            fh.writelines("line %d\n" % i for i in range(1, 31))
        self._git("add", "-A")
        self._git("commit", "-qm", "shared file")
        base = self._git("rev-parse", "HEAD")

        def edit(idx, text):
            with open(shared) as fh:
                lines = fh.readlines()
            lines[idx - 1] = text + "\n"
            with open(shared, "w") as fh:
                fh.writelines(lines)

        edit(20, "line 20 REVIEWED")
        self._git("add", "-A")
        self._git("commit", "-qm", "reviewed edits line 20")
        reviewed = self._git("rev-parse", "HEAD")
        original_pid = self._patch_id(reviewed)

        self._git("checkout", "-q", "-b", "integration/rekey-%d" % gap, base)
        edit(20 - gap, "line %d NEIGHBOUR" % (20 - gap))
        self._git("add", "-A")
        self._git("commit", "-qm", "neighbour edits line %d" % (20 - gap))
        self._git("cherry-pick", reviewed)
        train = self._git("rev-parse", "HEAD")
        picked_pid = self._patch_id(train)

        # MUST-HIT: the fixture is only meaningful if the reviewed CONTENT is
        # actually aboard while the commit is not an ancestor. If either half
        # is false the arm below would pass for the wrong reason.
        self.assertIn("line 20 REVIEWED",
                      self._git("show", "%s:shared.txt" % train),
                      "fixture broken: the reviewed change is not in the train")
        self.assertNotEqual(subprocess.run(
            ("git", "merge-base", "--is-ancestor", reviewed, train),
            cwd=self.repo).returncode, 0,
            "fixture broken: the reviewed commit is an ancestor, so patch "
            "containment is never consulted")

        tree = self._git("rev-parse", "HEAD^{tree}")
        ident = gate.interpreter()
        with mock.patch.object(gate.pk, "now_ts", return_value=receipt_ts):
            row, minted, err = gate._mint_result(
                self.repo, train, tree, False, ident,
                [ident["executable"]] + list(gateauthority.SERIAL_ARGV),
                True, None, 0, 0, "Ran 9 tests in 0.2s\n\nOK\n")
        self._git("checkout", "-q", "lane/probe")
        self.assertTrue(minted, err)
        return base, reviewed, train, row, picked_pid, original_pid

    def _patch_id(self, commit):
        diff = subprocess.run(("git", "diff-tree", "-p", commit),
                              cwd=self.repo, capture_output=True, text=True,
                              check=True).stdout
        out = subprocess.run(("git", "patch-id", "--stable"), cwd=self.repo,
                             input=diff, capture_output=True, text=True,
                             check=True).stdout
        return out.split()[0] if out.strip() else ""

    def mint(self, *lines, **kw):
        """A custom-argv run, so the child is cheap. Custom means the recorded
        interpreter is UNKNOWN by design — the suite path is covered separately
        by test_default_run_records_the_running_interpreter.

        Cheap is not unguarded: a custom run still goes through the gate guard,
        so this mint needs the cgroup supervisor exactly as a whole-suite one
        does, and says so rather than failing where it cannot have it."""
        require_supervisor()
        row, err = gate.run(repo=self.repo,
                            argv=[sys.executable] + _emit(*lines), **kw)
        self.assertIsNone(err, err)
        return row

    def fixture_suite(self, source):
        """Commit one real discoverable test module and return its path."""
        tests = os.path.join(self.repo, "tests")
        os.makedirs(tests, exist_ok=True)
        with open(os.path.join(tests, "__init__.py"), "w") as fh:
            fh.write("")
        path = os.path.join(tests, "test_gate_receipt_fixture.py")
        with open(path, "w") as fh:
            fh.write(source)
        self._git("add", "-A")
        self._git("commit", "-qm", "gate receipt fixture")
        self.head = self._git("rev-parse", "HEAD")
        return path


class CapacityGrantFlowTest(GateBase):
    GRANT = {
        "cap": 16,
        "reason": "paneless build host, 32 host-online CPUs",
    }
    TOPOLOGY = {"cores": 32, "reason": "32 host-online CPUs"}
    ENV = {"HELM_GATE_SUITE_CAP": "16"}

    def assert_grant_flow(self, argv=None):
        topology = mock.Mock(return_value=self.TOPOLOGY)
        admit = mock.Mock(return_value=(self.GRANT, None))
        # A COPY PER ARM: the run writes into the env it is handed, and the
        # class's own dict is shared by every arm and every module that
        # imports this one (task/3039).
        env = dict(self.ENV)
        suite_env = mock.Mock(return_value=env)
        child = mock.Mock(return_value=(
            "", "Ran 1 test in 0.001s\n\nOK\n", 0, None))
        with mock.patch.object(gate, "_capacity_topology", topology), \
                mock.patch.object(gate, "_admit_suite", admit), \
                mock.patch.object(gate, "_suite_env", suite_env), \
                mock.patch.object(gate, "_queued_process", child):
            row, err = gate.run(repo=self.repo, argv=argv)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "OK")
        topology.assert_called_once_with()
        self.assertIs(admit.call_args.kwargs["topology"], self.TOPOLOGY)
        suite_env.assert_called_once_with(self.GRANT)
        self.assertIs(child.call_args.kwargs["env"], env)

    def test_one_grant_reaches_queued_suite_admission_and_environment(self):  # noqa: VACUOUS_ASSERTION — assert_grant_flow unconditionally asserts the minted row plus topology, admission, env-render, and child mock calls
        self.assert_grant_flow()

    def test_one_grant_reaches_custom_suite_scale_child_environment(self):  # noqa: VACUOUS_ASSERTION — assert_grant_flow unconditionally asserts the custom path's minted row plus exact grant-bearing calls
        self.assert_grant_flow([
            sys.executable, "-m", "unittest", "discover", "-s", "tests"])


class _Vfs(object):
    """A synthetic os.statvfs result — the same shape tests/test_scratch.py
    measures the mount plane with. Bytes comfortable by default; the inode
    axis is the argument, because that is the axis df -h cannot see."""

    def __init__(self, inodes_pct, bytes_pct=36):
        self.f_frsize = self.f_bsize = 4096
        self.f_blocks, self.f_bfree = 1000, 1000 - bytes_pct * 10
        self.f_bavail = self.f_bfree
        self.f_files = 1048576
        self.f_ffree = self.f_favail = 1048576 - inodes_pct * 10485


class ScratchPreflightTest(GateBase):
    """THE MEASURED DEFECT: a build node whose tmpfs /tmp hit
    100% of its nr_inodes cap minted three whole-suite FAILED receipts whose
    failures were Errno 28 tracebacks — claims about the node, read as claims
    about the tree. The gate now measures the mount it will mint under BEFORE
    the FIFO and refuses at the doctor's own threshold, with NO receipt."""

    _TMPFS = [("/tmp", "tmpfs", "rw,nosuid,nodev,size=46004020k,"
               "nr_inodes=1048576,inode64")]

    def _node(self, inodes_pct, bytes_pct=36):
        """This node's tmp reads `inodes_pct` / `bytes_pct` for one block.
        Through the REAL reader (scratch.usage over a synthetic statvfs and
        mount table), so the arm proves the gate composes the doctor's
        instrument rather than a number the arm handed it. That is the host
        reading the fixture's pin keeps every other arm away from, so this
        restores the unpinned door and says so to the tripwire; both reads
        under it are mocked here."""
        return (
            mock.patch.object(gate.scratch.os, "statvfs",
                              return_value=_Vfs(inodes_pct, bytes_pct)),
            mock.patch.object(gate.scratch, "mount_table",
                              return_value=ScratchPreflightTest._TMPFS),
            mock.patch.object(gate.scratch, "_ambient_tmp",
                              return_value="/tmp"),
            mock.patch.object(gate, "_scratch_preflight", _REAL_PREFLIGHT),
            HostAdmissionAsked())

    def _run(self, inodes_pct, argv=None, bytes_pct=36):
        """gate.run against a node at the given pressure, with every door
        past the preflight instrumented so the arm can say which ones opened."""
        acquire = mock.Mock(return_value=({"pid": os.getpid(), "_legacy": None,
                                           "position": "p1"}, None))
        admit = mock.Mock(return_value=(CapacityGrantFlowTest.GRANT, None))
        def child_run(repo, cmd, position, timeout, env=None, **kw):
            # THE SUITE'S HARDEST LEFTOVER, planted where the suite would:
            # a release extracted 0o555 into the child's TMPDIR.
            ro = os.path.join(env["TMPDIR"], "releases", "deadbeef")
            os.makedirs(ro)
            os.chmod(ro, 0o555)
            return "", "Ran 1 test in 0.001s\n\nOK\n", 0, None
        child = mock.Mock(side_effect=child_run)
        finish = mock.Mock(return_value=(True, None))
        with contextlib.ExitStack() as stack:
            for patch in self._node(inodes_pct, bytes_pct):
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(
                gate, "_capacity_topology",
                return_value=CapacityGrantFlowTest.TOPOLOGY))
            stack.enter_context(mock.patch.object(gate, "_acquire_gate", acquire))
            stack.enter_context(mock.patch.object(gate, "_admit_suite", admit))
            stack.enter_context(mock.patch.object(gate, "_finish_position", finish))
            stack.enter_context(mock.patch.object(gate, "_queued_process", child))
            row, err = gate.run(repo=self.repo, argv=argv)
        return row, err, {"acquire": acquire, "admit": admit, "child": child}

    def test_a_pressured_node_refuses_before_the_queue_and_mints_nothing(self):  # noqa: VACUOUS_ASSERTION — the assertIns on the refusal text are the positive control on the same (row, err) answer; the shut doors and absent ledger are its consequences
        """The incident shape: 36% bytes, 100% inodes. Every door past the
        preflight stays shut — no FIFO position, no admission, no child — and
        the ledger holds no row, because a receipt about this run would be a
        receipt about the mount."""
        row, err, doors = self._run(100)
        self.assertIsNone(row)
        self.assertIn("under pressure", err)
        self.assertIn("100% inodes", err)
        self.assertIn("36% bytes", err)
        self.assertIn("nr_inodes=1048576", err)
        self.assertIn("%d%% scratch threshold" % gate.scratch.WARN_PCT, err)
        self.assertIn("mints NO receipt", err)
        self.assertIn("helm scratch gc --apply", err)
        for name, door in doors.items():
            door.assert_not_called()
        self.assertFalse(os.path.exists(gate.receipts_path()),
                         "a refused run wrote a receipt ledger")

    def test_the_threshold_is_the_doctors_warn_line(self):  # noqa: VACUOUS_ASSERTION — the WARN_PCT-1 half admits and asserts a minted OK row; that is the control for the refusing half
        """At exactly WARN_PCT the gate refuses; one point under, it admits.
        The number is scratch.WARN_PCT read at run time, never a copy."""
        row, err, doors = self._run(gate.scratch.WARN_PCT)
        self.assertIsNone(row)
        self.assertIn("under pressure", err)
        doors["child"].assert_not_called()
        row, err, doors = self._run(gate.scratch.WARN_PCT - 1)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "OK")

    def test_bytes_pressure_refuses_on_its_own(self):
        row, err, _doors = self._run(3, bytes_pct=97)
        self.assertIsNone(row)
        self.assertIn("97% bytes", err)

    def test_a_suite_shaped_custom_command_is_refused_too(self):  # noqa: VACUOUS_ASSERTION — assertIn on the refusal text is the positive control; the unstarted child is its consequence
        row, err, doors = self._run(100, argv=[
            sys.executable, "-m", "unittest", "discover", "-s", "tests"])
        self.assertIsNone(row)
        self.assertIn("under pressure", err)
        doors["child"].assert_not_called()

    def test_a_healthy_node_admits_and_hands_the_child_a_root_the_gate_reaps(self):
        """THE CONTROL at 50%: the run proceeds through every door, the
        child's TMPDIR is a gate-owned root under the ambient tmp, and that
        root is GONE when run() returns — the effect a killed child cannot
        undo, since the reap is the gate's finally and not the child's exit —
        even though the child left a 0o555 release directory inside it."""
        row, err, doors = self._run(50, bytes_pct=50)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "OK")
        doors["acquire"].assert_called_once()
        doors["admit"].assert_called_once()
        env = doors["child"].call_args.kwargs["env"]
        root = env["TMPDIR"]
        self.assertIn("helm-gate-scratch-", os.path.basename(root))
        self.assertTrue(root.startswith(tempfile.gettempdir() + os.sep), root)
        self.assertFalse(os.path.exists(root),
                         "the gate left its child scratch root behind: %s" % root)

    def test_an_unmeasurable_tmp_is_unmeasured_not_pressure(self):  # noqa: VACUOUS_ASSERTION — reader.assert_called_once_with is the positive control: the None is the consulted reader's own answer
        """scratch.usage returns None for a path it cannot statvfs and the
        survey reads that as unmeasured; the gate takes the same posture.
        POSITIVE CONTROL: the reader was consulted, about the ambient tmp —
        the None is its answer, not a preflight that never asked."""
        reader = mock.Mock(return_value=None)
        with mock.patch.object(gate.scratch, "usage", reader), \
                mock.patch.object(gate.scratch, "_ambient_tmp",
                                  return_value="/tmp"), \
                HostAdmissionAsked():
            self.assertIsNone(_REAL_PREFLIGHT())
        reader.assert_called_once_with("/tmp")


class CapacityRefusalIsNotRedTest(GateBase):
    """task/1740: A RUN THIS NODE REFUSED IS NOT A RED RUN.

    If both exit 1, a caller reading the exit code cannot tell "the tree
    failed" from "the box was full" — the strings discriminate, the code does
    not. The three capacity refusals (the whole-suite cap, the memory-stall
    floor, a pressured tmp) exit EXIT_NOT_RUN_CAPACITY and say NOT RUN; a red
    suite keeps 1 and a green one 0, on the SAME tree. Every arm drives the
    real admission door against this fixture's box (`pin_admission`), so the
    node's own load decides nothing here — which is the other half of the row.
    """

    # cap = max(SUITE_CAP, 4 // 2) = 2 on a paneless fixture box
    TOPOLOGY = {"cores": 4, "reason": "4 host-online CPUs"}
    SUITE = ("python3", "-m", "unittest", "discover", "-s", "tests", "-t", ".")

    def plant_suite(self, pid):
        """One whole-suite process on the FIXTURE box, in the shape the census
        reads (the same shape tests/test_gate_cap.py plants)."""
        d = os.path.join(self.admission_proc, str(pid))
        os.makedirs(d)
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"".join(a.encode() + b"\0" for a in self.SUITE))
        with open(os.path.join(d, "stat"), "w") as f:
            f.write("%d (suite) S 1 %s 7\n" % (pid, " ".join(["0"] * 17)))
        with open(os.path.join(d, "cgroup"), "w") as f:
            f.write("0::/app.slice/app-fake.scope\n")

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(gate, "_capacity_topology",
                                  return_value=self.TOPOLOGY):
            rc = gate.cmd_gate(["run", "--repo", self.repo] + list(argv))
        return rc, out.getvalue(), err.getvalue()

    def assert_not_run(self, want):
        """Both surfaces of one refusal: exit 3 and NOT RUN on stderr, and
        exit 3 with `not_run: capacity` in --json; no receipt either way."""
        rc, _out, err = self.cli()
        self.assertEqual(rc, gate.EXIT_NOT_RUN_CAPACITY, err)
        self.assertIn("NOT RUN (capacity, exit 3)", err)
        self.assertIn(want, err)
        rc, out, err = self.cli("--json")
        self.assertEqual(rc, gate.EXIT_NOT_RUN_CAPACITY, err)
        body = json.loads(out)
        self.assertEqual((body["minted"], body["not_run"]), (False, "capacity"))
        self.assertIn(want, body["reason"])
        self.assertFalse(os.path.exists(gate.receipts_path()),
                         "a run the node refused minted a receipt")

    def test_a_full_node_exits_NOT_RUN_and_the_same_tree_red_1_green_0(self):  # noqa: VACUOUS_ASSERTION — the ledger absent after the refusal holds the minted FAILED then OK receipts later in this arm  # noqa: ORPHANED_MOCK — cmd_gate -> _cmd_run -> run reaches _queued_process; the minted FAILED receipt is that double's output
        self.assertEqual(gate.EXIT_NOT_RUN_CAPACITY, 3)
        self.plant_suite(101)
        self.plant_suite(102)
        self.assert_not_run("whole-suite cap is 2")
        # THE SAME TREE on the same fixture box, freed: a red suite is still a
        # red suite (a minted FAILED receipt, exit 1) and a green one exits 0,
        # so 3 is about the node and neither of them moved.
        for pid in ("101", "102"):
            shutil.rmtree(os.path.join(self.admission_proc, pid))
        red = ("", _failure_stream(1), 1, None)
        with mock.patch.object(gate, "SUITE", gateauthority.SERIAL_ARGV), \
                mock.patch.object(gate, "_queued_process", return_value=red):
            rc, out, err = self.cli()
        self.assertEqual(rc, 1, err)
        self.assertIn("FAILED", out)
        self.assertEqual([r["status"] for r in gate.receipts()[0]], ["FAILED"])
        # THE SAME RED TREE RE-GATES ONLY AS A DECLARED FLAKE (task/3039):
        # nothing changed, so the bare rerun is refused before it spends, and
        # `--again` is the declaration that admits it.
        rc, _out, err = self.cli()
        self.assertEqual(rc, 1, err)
        self.assertIn("--again", err)
        with serial_process(ran=1):
            rc, out, err = self.cli("--again")
        self.assertEqual(rc, 0, err)
        self.assertEqual([r["status"] for r in gate.receipts()[0]],
                         ["FAILED", "OK"])

    def test_the_memory_stall_floor_is_a_capacity_refusal(self):  # noqa: VACUOUS_ASSERTION — exit 3 and the named stall text are the positive controls on the same run; the full-node arm proves the same ledger fills
        d = os.path.join(self.admission_proc, "pressure")
        os.makedirs(d)
        with open(os.path.join(d, "memory"), "w") as f:
            f.write("some avg10=50.00 avg60=0.00 avg300=0.00 total=1\n"
                    "full avg10=0.00 avg60=0.00 avg300=0.00 total=1\n")
        self.assert_not_run("already stalling on memory")

    def test_a_pressured_tmp_is_a_capacity_refusal(self):  # noqa: VACUOUS_ASSERTION — exit 3 and the named tmp text are the positive controls on the same run; the full-node arm proves the same ledger fills
        with contextlib.ExitStack() as stack:
            for patch in ScratchPreflightTest._node(self, 100):
                stack.enter_context(patch)
            self.assert_not_run("under pressure")

    def test_a_refusal_that_is_not_capacity_keeps_exit_1(self):
        """CONTROL: 3 is not "any refusal". A process table admission cannot
        read is UNKNOWN, not a full node, and keeps the old exit and no
        `not_run` key."""
        pin_admission(self, proc=os.path.join(self.tmp, "no-such-proc"))
        rc, _out, err = self.cli()
        self.assertEqual(rc, 1, err)
        self.assertIn("cannot count running suites", err)
        self.assertNotIn("NOT RUN", err)
        rc, out, _err = self.cli("--json")
        self.assertEqual(rc, 1)
        self.assertNotIn("not_run", json.loads(out))

    def test_queue_trouble_on_the_refusal_keeps_it_a_capacity_refusal(self):
        self.plant_suite(101)
        self.plant_suite(102)
        with mock.patch.object(gate, "_finish_position",
                               return_value=(False, "queue ledger is gone")), \
                mock.patch.object(gate, "_capacity_topology",
                                  return_value=self.TOPOLOGY):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(row)
        self.assertIsInstance(err, gate.CapacityRefusal)
        self.assertIn("whole-suite cap is 2", err)
        self.assertIn("queue ledger is gone", err)

    def test_a_full_NODE_never_reaches_an_arm_on_a_fixture_box(self):
        """THE RED-FIRST SHAPE, kept: the host's census reports a node far past
        any cap, as the build host's did with two whole suites running. On trunk every
        GateBase mint read that census and was refused; pinned, the same mint
        is admitted on the fixture box and the host census is never asked."""
        real = gate.suite_census
        asked = []

        def full(proc_dir=None):
            asked.append(proc_dir)
            if proc_dir != gate.HOST_PROC:
                return real(proc_dir)
            return [{"pid": 990000 + i, "argv": list(self.SUITE),
                     "cwd": "/full-node/%d" % i, "kind": "suite",
                     "children": [], "position": None,
                     "owner": "pid:%d" % (990000 + i), "_ancestors": ()}
                    for i in range(64)]

        with mock.patch.object(gate, "suite_census", full), \
                serial_process(ran=1):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "OK")
        self.assertIn(self.admission_proc, asked)
        self.assertNotIn(gate.HOST_PROC, asked)

    def test_fab_spills_on_the_two_admission_phrases_not_the_tmp_one(self):  # noqa: VACUOUS_ASSERTION — the tmp refusal's absent phrase sits beside two unconditional assertRegex hits and three isinstance checks on the same refusals
        """task/1740 P3, the claim docs/VERBS.md makes about fab: `fab gate`
        spills a pre-receipt failure to another node when its output matches
        FAB_SPILL (fab-gate's spoke body, verbatim). The cap and stall
        refusals carry those words, so rewording either would silently stop
        the spill; the pressured-tmp refusal does not, and helm must not fake
        them: its exit 3 is the signal fab can key on instead."""
        FAB_SPILL = "whole-suite cap is|already stalling on memory"
        self.plant_suite(101)
        self.plant_suite(102)
        _grant, cap = _REAL_ADMIT(proc_dir=self.admission_proc,
                                  topology=self.TOPOLOGY,
                                  admissions_path=self.admissions)
        for pid in ("101", "102"):
            shutil.rmtree(os.path.join(self.admission_proc, pid))
        d = os.path.join(self.admission_proc, "pressure")
        os.makedirs(d)
        with open(os.path.join(d, "memory"), "w") as f:
            f.write("some avg10=50.00 avg60=0.00 avg300=0.00 total=1\n")
        _grant, stall = _REAL_ADMIT(proc_dir=self.admission_proc,
                                    topology=self.TOPOLOGY,
                                    admissions_path=self.admissions)
        tmp = gate._scratch_preflight(usage=lambda path: {
            "path": path, "mount": path, "fstype": "tmpfs",
            "nr_inodes": None, "bytes_pct": 36, "inodes_pct": 100})
        for err in (cap, stall, tmp):
            self.assertIsInstance(err, gate.CapacityRefusal)
        self.assertRegex(cap, FAB_SPILL)
        self.assertRegex(stall, FAB_SPILL)
        self.assertIsNone(re.search(FAB_SPILL, tmp), tmp)

    def test_a_full_host_TMP_never_reaches_an_arm_on_a_fixture_box(self):  # noqa: VACUOUS_ASSERTION — the minted OK row is the positive control for the never-asked reader, and the closing assertIn proves the same full reading refuses
        """task/1740 P3, the third door: the host's tmp reads 100% inodes, as
        a build node's did when three receipts minted FAILED. Unpinned, every
        GateBase mint read that mount and exited NOT RUN; pinned, the same mint
        reads the fixture box's tmp and the host's reader is never asked."""
        full = {"path": "/tmp", "mount": "/tmp", "fstype": "tmpfs",
                "nr_inodes": 1048576, "bytes_pct": 36, "inodes_pct": 100}
        host_reader = mock.Mock(return_value=full)
        with mock.patch.object(gate.scratch, "usage", host_reader), \
                serial_process(ran=1):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "OK")
        host_reader.assert_not_called()
        # CONTROL: the same full reading through the fixture's own reader is
        # refused, so the green above is the pin and not a blind preflight.
        self.assertIn("under pressure",
                      gate._scratch_preflight(usage=lambda path: full))

    def test_the_tmp_preflight_is_inside_the_tripwire(self):  # noqa: VACUOUS_ASSERTION — the assertRaises is the must-hit; the None is the fixture reader's healthy answer beside it
        """task/1740 P3: the scratch preflight reads THIS box's tmp mount with
        no reader passed, so it raises the same event as the census, and an
        unasked arm is refused before the mount is read. A fixture reader
        passes; so does the host reader once the arm asks."""
        host_reader = mock.Mock(return_value=None)
        with mock.patch.object(gate.scratch, "usage", host_reader):
            with self.assertRaises(HostAdmissionRefused) as caught:
                _REAL_PREFLIGHT()
            host_reader.assert_not_called()
            self.assertIn("proc=None", str(caught.exception))
            self.assertIsNone(_REAL_PREFLIGHT(usage=healthy_tmp))
            with HostAdmissionAsked():
                self.assertIsNone(_REAL_PREFLIGHT())
        host_reader.assert_called_once_with(gate.scratch._ambient_tmp())

    def test_reaching_the_real_node_cap_without_asking_is_refused(self):  # noqa: VACUOUS_ASSERTION — the None errors are admissions whose refusal the three assertRaises above prove possible  # noqa: ORPHANED_MOCK — _REAL_ADMIT is gate._admit_suite, which calls all three doubles; census.assert_called_once_with proves it
        """THE TRIPWIRE. The real door with no fixture box reaches THIS node,
        and tests/__init__.py refuses it before anything is read — by the host
        /proc, and by the host ledger beside a fixture /proc (which would
        otherwise retire the host's live rows). The fixture box and an explicit
        ask are the two ways through."""
        self.assertEqual(tests.HOST_ADMISSION_EVENT, gate.HOST_ADMISSION_EVENT)
        with self.assertRaises(HostAdmissionRefused) as caught:
            _REAL_ADMIT(topology=self.TOPOLOGY, admissions_path=self.admissions)
        self.assertIn("proc='/proc'", str(caught.exception))
        with self.assertRaises(HostAdmissionRefused):
            _REAL_ADMIT(proc_dir=self.admission_proc, topology=self.TOPOLOGY,
                        admissions_path=gate._host_admissions_path())
        with self.assertRaises(HostAdmissionRefused):
            gate._admission_release(
                "feedfacefeedface",
                admissions_path=gate._host_admissions_path())
        # CONTROLS on the same door: a fixture box passes, and so does the
        # host path once the arm asks (its reads mocked, so nothing is read).
        _grant, err = _REAL_ADMIT(proc_dir=self.admission_proc,
                                  topology=self.TOPOLOGY,
                                  admissions_path=self.admissions)
        self.assertIsNone(err, err)
        census = mock.Mock(return_value=[])
        with mock.patch.object(gate, "suite_census", census), \
                mock.patch.object(gate, "_psi_some_avg10", return_value=None), \
                mock.patch.object(gate, "_capacity_grant",
                                  return_value={"cap": 2, "reason": "fixture"}), \
                HostAdmissionAsked():
            _grant, err = _REAL_ADMIT(topology=self.TOPOLOGY,
                                      admissions_path=self.admissions)
        self.assertIsNone(err, err)
        census.assert_called_once_with(gate.HOST_PROC)


def _floor(text):
    """A protocol stream in THIS interpreter's emission format. 3.11+ keeps
    the method-suffixed headers; older floors never print the suffix, so
    feeding them a suffixed synthetic stream tests an emission that floor
    cannot produce — and double-mints the id. Identity on 3.11+."""
    if gate._NEW_HEADER_FORMAT:
        return text
    import re
    # only names the identity parser accepts — exception PROSE like
    # "URLError (urllib.error.URLError)" also repeats its last component,
    # and rewriting it would change what the negative arms feed the parser
    return re.sub(r"^((?:FAIL|ERROR): )(test[\w.]*|runTest) \(([\w.<>]+)\.\2\)",
                  r"\1\2 (\3)", text, flags=re.M)


def _failure_stream(n, diagnosis="RuntimeError"):
    blocks = []
    for i in range(n):
        blocks.append("""======================================================================
ERROR: test_%d (tests.test_many.Many.test_%d)
----------------------------------------------------------------------
Traceback (most recent call last):
  File \"/tmp/test_many.py\", line %d, in test_%d
    raise RuntimeError(%r)
RuntimeError: %s
""" % (i, i, i + 1, i, diagnosis, diagnosis))
    return _floor("\n".join(blocks) +
                  "\nRan %d tests in 0.1s\n\nFAILED (errors=%d)\n" % (n, n))


class ParseResult(GateBase):
    # task/3070: the last 16 KiB of train198's whole-suite stderr, as the
    # UNKNOWN receipt gate:d3ecc40f7dc72b6f kept it (stderr_tail, 904984 bytes
    # in all, truncated), read back from the fab node's per-run ledger. One
    # interpreter path was redacted to `~/`; no other byte differs.
    TRAIN198_TAIL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "fixtures",
                                 "gate-train198-unknown-stderr-tail.txt")
    TRAIN198_REFUSAL = ("the summary says OK but the run count is unreadable "
                        "or not its terminal footer")

    def train198_tail(self):
        with open(self.TRAIN198_TAIL, encoding="utf-8") as f:
            return f.read()

    def test_the_real_train198_tail_ends_mid_suite_and_reads_as_UNKNOWN(self):
        """The run died of SIGTERM (receipt rc 241, -15 through the gate
        supervisor) inside tests/test_seat.py, on the fork warning its
        launch-owner test prints, so no unittest footer was ever written.
        The kept tail holds no summary at all, and the parse says so."""
        tail = self.train198_tail()
        self.assertTrue(tail.endswith(
            "DeprecationWarning: This process (pid=1299936) is multi-threaded,"
            " use of fork() may lead to deadlocks in the child.\n"
            "  pid = os.fork()\n"))
        got = gate.parse_result(tail)
        self.assertEqual((got["status"], got["ran"], got["detail"]),
                         ("UNKNOWN", None, ""))
        self.assertEqual(got["unreadable_reason"],
                         "the run printed no readable summary")

    def test_a_summary_followed_by_the_real_tail_stays_UNKNOWN(self):
        """The refusal the gate printed. The whole stream is gone; the receipt
        says an OK summary sat somewhere before the kept 16 KiB. The prefix
        here is the parser's own documented shape for that (a well-formed
        unittest summary), labelled as such; everything after it is the real
        captured tail. Anchoring on the LAST well-formed Ran/OK pair and
        ignoring what follows would read this as OK, Ran 3, for a suite that
        died part-way through. The control shows the same prefix alone is a
        well-formed OK, so only the real text after it makes the UNKNOWN."""
        summary = ("-" * 70 + "\nRan 3 tests in 0.010s\n\nOK\n")
        control = gate.parse_result(summary)
        self.assertEqual((control["status"], control["ran"]), ("OK", 3))
        got = gate.parse_result(summary + self.train198_tail())
        self.assertEqual((got["status"], got["ran"], got["detail"]),
                         ("UNKNOWN", None, self.TRAIN198_REFUSAL))
        self.assertTrue(got["failures_unreadable"])

    def test_ok_with_skips(self):
        got = gate.parse_result("Ran 5115 tests in 154.773s\n\nOK (skipped=8)\n")
        self.assertEqual(got["status"], "OK")
        self.assertEqual(got["ran"], 5115)
        self.assertEqual(got["skipped"], 8)
        self.assertEqual(got["elapsed"], 154.773)

    def test_failed_keeps_the_detail(self):
        got = gate.parse_result("Ran 12 tests in 1.0s\n\nFAILED (failures=2)\n")
        self.assertEqual(got["status"], "FAILED")
        self.assertEqual(got["detail"], "failures=2")
        self.assertTrue(got["failures_unreadable"])

    def test_failure_blocks_yield_ids_and_exception_diagnoses(self):
        text = """======================================================================
ERROR: test_boom (tests.test_probe.Probe.test_boom)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/test_probe.py", line 7, in test_boom
    raise ValueError("boom")
ValueError: boom

======================================================================
FAIL: test_nope (tests.test_probe.Probe.test_nope)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/test_probe.py", line 11, in test_nope
    self.assertEqual(1, 2)
AssertionError: 1 != 2

----------------------------------------------------------------------
Ran 2 tests in 0.1s

FAILED (failures=1, errors=1)
"""
        got = gate.parse_result(_floor(text))
        self.assertFalse(got["failures_unreadable"])
        self.assertEqual(got["failures"], [
            {"kind": "ERROR", "test": "tests.test_probe.Probe.test_boom",
             "traceback": "File \"/tmp/test_probe.py\", line 7, in "
                          "test_boom | raise ValueError(\"boom\") | "
                          "ValueError: boom"},
            {"kind": "FAIL", "test": "tests.test_probe.Probe.test_nope",
             "traceback": "File \"/tmp/test_probe.py\", line 11, in "
                          "test_nope | self.assertEqual(1, 2) | "
                          "AssertionError: 1 != 2"},
        ])
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            gate._show_failures(dict(got, v=4))
        self.assertIn("ValueError: boom", stdout.getvalue())
        self.assertIn("AssertionError: 1 != 2", stdout.getvalue())

    def test_long_traceback_keeps_first_frame_and_terminal_diagnosis(self):
        frames = "\n".join(
            "  File \"/tmp/frame_%02d.py\", line %d, in call_%02d\n"
            "    invoke_%02d()" % (i, i + 1, i, i)
            for i in range(30))
        text = """======================================================================
ERROR: test_long (tests.test_probe.Probe.test_long)
----------------------------------------------------------------------
Traceback (most recent call last):
%s
ValueError: terminal diagnosis

----------------------------------------------------------------------
Ran 1 test in 0.1s

FAILED (errors=1)
""" % frames
        got = gate.parse_result(_floor(text))
        diagnostic = got["failures"][0]["traceback"]
        self.assertFalse(got["failures_unreadable"])
        self.assertLessEqual(len(diagnostic), 500)
        self.assertIn("File \"/tmp/frame_00.py\", line 1, in call_00",
                      diagnostic)
        self.assertIn("traceback truncated", diagnostic)
        self.assertIn("ValueError: terminal diagnosis", diagnostic)

    def test_long_assertion_diff_keeps_exception_ahead_of_diff_tail(self):
        diff = "\n".join("+ actual value %03d" % i for i in range(100))
        text = """======================================================================
FAIL: test_diff (tests.test_probe.Probe.test_diff)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/test_probe.py", line 8, in test_diff
    self.assertEqual(expected, actual)
AssertionError: values differ
%s

Ran 1 test in 0.1s

FAILED (failures=1)
""" % diff
        got = gate.parse_result(_floor(text))
        diagnostic = got["failures"][0]["traceback"]
        self.assertFalse(got["failures_unreadable"])
        self.assertLessEqual(len(diagnostic), 500)
        self.assertIn("traceback truncated", diagnostic)
        self.assertIn("AssertionError: values differ", diagnostic)
        self.assertNotIn("actual value 099", diagnostic)

    def test_assertion_message_after_the_exception_is_not_discarded(self):
        """THE MESSAGE IS THE DIAGNOSIS, and it lives AFTER the exception.

        unittest emits an assertEqual's caller-supplied message below the
        exception line and its diff, so a terminal segment that stops at the
        exception drops it entirely — AT ANY CAP. Measured on a real receipt
        before the cure: an arm attached a nested gate run's detail and failure
        list to its message, the diagnosis came back 405 chars (UNDER the 500
        cap, so the cap never bound), and the only sentence naming the cause
        was the discarded one. What survived was the SOURCE of the format
        string and never its rendered values.
        """
        text = """======================================================================
FAIL: test_nested (tests.test_probe.Probe.test_nested)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/test_probe.py", line 8, in test_nested
    self.assertEqual(row, "OK", "inner=%r" % inner)
AssertionError: 'FAILED' != 'OK'
- FAILED
+ OK
 : inner='NotImplementedError: setsid unavailable on this platform'

Ran 1 test in 0.1s

FAILED (failures=1)
"""
        got = gate.parse_result(_floor(text))
        diagnostic = got["failures"][0]["traceback"]
        self.assertFalse(got["failures_unreadable"])
        # MUST-HIT: the cause named only inside the message survives.
        self.assertIn("NotImplementedError", diagnostic)
        self.assertIn("setsid unavailable", diagnostic)
        # The exception still leads its own segment — the message is added
        # AFTER it, never in place of it.
        self.assertIn("AssertionError: 'FAILED' != 'OK'", diagnostic)
        self.assertLess(diagnostic.index("AssertionError"),
                        diagnostic.index("NotImplementedError"))
        # MUST-MISS: an unbounded message must NOT escape the cap. Without
        # this the arm passes against a function that simply stopped
        # truncating, which would trade one defect for a worse one.
        flood = text.replace(
            "inner='NotImplementedError: setsid unavailable on this platform'",
            "inner='%s'" % ("x" * 4000))
        flooded = gate.parse_result(_floor(flood))["failures"][0]["traceback"]
        self.assertLessEqual(len(flooded), 500)
        self.assertIn("traceback truncated", flooded)

    def test_long_diff_does_not_crowd_out_the_caller_message(self):
        """A PREFIX OF THE SPAN REBUILDS THE DEFECT ONE LAYER IN.

        Spanning the terminal to the end of the block is not enough: taking a
        PREFIX of that span reproduces the original defect one layer in. With a
        long assertEqual diff the prefix keeps the exception and diff lines
        000..012 and pushes the rendered message — the only text naming the
        cause — off the end again. An arm with a TINY diff cannot reach this:
        its message always fits, and flooding the MESSAGE rather than the
        middle exercises a different bound.

        Both ends of the terminal now survive and the diff between them is
        elided — but ONLY when the last element is a caller message, which
        unittest marks with a leading colon. Without that test the elision
        keeps the last DIFF LINE instead and breaks the standing contract
        pinned by test_long_assertion_diff_keeps_exception_ahead_of_diff_tail;
        measured, "actual value 099" came back into a case that pins its
        absence.
        """
        diff = "\n".join("+ actual value %03d" % i for i in range(100))
        body = ("======================================================================\n"
                "FAIL: test_diff (tests.test_probe.Probe.test_diff)\n"
                "----------------------------------------------------------------------\n"
                "Traceback (most recent call last):\n"
                '  File "/tmp/test_probe.py", line 8, in test_diff\n'
                "    self.assertEqual(expected, actual, msg)\n"
                "AssertionError: values differ\n" + diff + "\n")
        with_msg = (body + " : inner='UNIQUE_CAUSE_ONLY_IN_MESSAGE'\n"
                    "\nRan 1 test in 0.1s\n\nFAILED (failures=1)\n")
        got = gate.parse_result(_floor(with_msg))["failures"][0]["traceback"]
        # MUST-HIT: the cause lives only in the message, behind 100 diff lines.
        self.assertIn("UNIQUE_CAUSE_ONLY_IN_MESSAGE", got)
        self.assertIn("AssertionError: values differ", got)
        self.assertLessEqual(len(got), 500)
        self.assertNotIn("actual value 050", got)   # middle elided, not kept

        # MUST-MISS: the SAME long diff with NO caller message must still
        # honour the standing contract — the diff tail may not survive.
        no_msg = body + "\nRan 1 test in 0.1s\n\nFAILED (failures=1)\n"
        plain = gate.parse_result(_floor(no_msg))["failures"][0]["traceback"]
        self.assertIn("AssertionError: values differ", plain)
        self.assertIn("traceback truncated", plain)
        self.assertNotIn("actual value 099", plain)

    def test_real_runner_multiline_message_keeps_its_continuation(self):  # noqa: VACUOUS_ASSERTION — the flagged root is a LOOP-SCOPED emit(case), so no unconditional control can exist on it by construction; the cure is the two-sided length bound plus the unconditional control_result emit above, which runs the same code path outside the loop
        """Generated by a REAL TextTestRunner, not hand-written.

        THE FIXTURES ARE THE HARD PART HERE, not the logic. unittest renders `standardMsg + " : " + msg`, and
        where that separator lands depends on the assertion — measured here,
        three shapes in one arm:
          own line       " : nested detail follows"   (full line-by-line diff)
          mid-line       "Diff is N characters long. ... : nested detail ..."
          exception line "AssertionError: False is not true : nested detail ..."
        and every CONTINUATION line is plain prose. A leading-colon test is
        false for two of the three, which is exactly when a cause hides in a
        continuation. A hand-written fixture only ever carries the shape its
        author already imagined; generating it makes unittest the author.
        """
        msg = "nested detail follows\nunique cause only on continuation"

        def emit(case):
            buf = io.StringIO()
            unittest.TextTestRunner(stream=buf, verbosity=1).run(
                unittest.TestLoader().loadTestsFromTestCase(case))
            return gate.parse_result(_floor(buf.getvalue()))

        class _OwnLine(unittest.TestCase):
            maxDiff = None
            def test_x(self):
                self.assertEqual(["e %03d" % i for i in range(100)],
                                 ["a %03d" % i for i in range(100)], msg)

        class _MidLine(unittest.TestCase):
            def test_x(self):
                self.assertEqual("e" * 900, "a" * 900, msg)

        class _OnException(unittest.TestCase):
            def test_x(self):
                self.assertTrue(False, msg)

        class _LongInline(unittest.TestCase):
            # The separator sits ON the exception line AND the message is far
            # longer than the whole budget — the shape where budgeting the
            # first line against the message's room goes negative.
            def test_x(self):
                self.assertTrue(
                    False, "prefix " + "x" * 1200 + " UNIQUE_CAUSE_AT_END")

        class _DiffOnly(unittest.TestCase):
            maxDiff = None
            def test_x(self):
                self.assertEqual(["e %03d" % i for i in range(100)],
                                 ["a %03d" % i for i in range(100)])

        # UNCONDITIONAL CONTROL, OUTSIDE THE LOOP. Every must-hit below sits
        # inside a for, so an empty or mistyped case tuple would let this arm
        # pass having asserted nothing. This one runs whatever the tuple says.
        control_result = emit(_OwnLine)
        self.assertFalse(control_result["failures_unreadable"])
        control = control_result["failures"][0]["traceback"]
        self.assertIn("unique cause only on continuation", control)
        self.assertIn("AssertionError", control)

        shapes = (_OwnLine, _MidLine, _OnException)
        self.assertEqual(len(shapes), 3, "all three separator shapes must run")

        # THE LONG-INLINE DIMENSION, which the three shapes above cannot reach:
        # each of them has a SHORT message, so the reconstruction never has to
        # bound anything. Here the separator is on the exception line and the
        # message alone is 1200 chars, so budgeting the whole first line
        # against the message's room goes negative and skips reconstruction
        # entirely — measured, that kept the exception and dropped the cause.
        inline = emit(_LongInline)["failures"][0]["traceback"]
        self.assertGreater(len(inline), 40)
        self.assertLessEqual(len(inline), 500)
        self.assertIn("AssertionError", inline)
        self.assertIn("UNIQUE_CAUSE_AT_END", inline)
        # MUST-MISS on the RENDERING, not the containment: the separator
        # belongs to neither the kind nor the message, and a single offset
        # made the kind swallow " : " while the joiner added another.
        # This is the only shape that exercises the embedded branch.
        self.assertNotIn(" : : ", inline)

        # MUST-HIT: the cause lives only on a CONTINUATION line, in all three
        # separator shapes real unittest emits.
        for case in shapes:
            diagnostic = emit(case)["failures"][0]["traceback"]
            # BOTH BOUNDS. assertLessEqual(len(x), 500) alone is satisfied by
            # an EMPTY diagnostic, which is the vacuity the rung named here:
            # its matching positive is the assertIn below, inside this same
            # loop, so neither runs if the loop body never executes.
            self.assertGreater(len(diagnostic), 40, case.__name__)
            self.assertLessEqual(len(diagnostic), 500, case.__name__)
            self.assertIn(
                "unique cause only on continuation", diagnostic,
                "%s lost the caller-message suffix — unittest's ' : ' "
                "delimiter drifted, or the suffix reconstruction stopped "
                "firing for this placement" % case.__name__)

        # MUST-MISS: no caller message at all — the suffix logic must NOT fire
        # and invent one out of diff content.
        plain = emit(_DiffOnly)["failures"][0]["traceback"]
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: prove this block produced a
        # real diagnosis before asserting what is absent from it. Without it,
        # a _DiffOnly that emitted nothing would satisfy the assertNotIn.
        self.assertIn("AssertionError", plain)
        self.assertLessEqual(len(plain), 500)
        self.assertNotIn("unique cause only on continuation", plain)

    def test_failure_capture_keeps_every_identity_for_the_record(self):
        blocks = []
        for i in range(gate.FAILURE_CAP + 5):
            blocks.append("""======================================================================
ERROR: test_%d (tests.test_many.Many.test_%d)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/test_many.py", line %d, in test_%d
    raise RuntimeError()
RuntimeError
""" % (i, i, i + 1, i))
        text = "\n".join(blocks) + ("\nRan 25 tests in 0.1s\n\n"
                                     "FAILED (errors=25)\n")
        got = gate.parse_result(_floor(text))
        self.assertFalse(got["failures_unreadable"])
        self.assertEqual(len(got["failures"]), gate.FAILURE_CAP + 5)
        self.assertEqual(got["failures"][-1]["test"],
                         "tests.test_many.Many.test_24")
        self.assertFalse(any("truncated" in item for item in got["failures"]))

    def test_legacy_marker_says_omitted_identities_were_not_recorded(self):
        failures = [{"kind": "ERROR",
                     "test": "tests.test_many.Many.test_%d" % i,
                     "traceback": "frame"} for i in range(gate.FAILURE_CAP)]
        failures.append({"truncated": 5})
        row = {"v": 4, "failures": failures, "failures_unreadable": False}
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            gate._show_failures(row)
        shown = stdout.getvalue()
        self.assertIn("5 additional identities UNAVAILABLE/SKIPPED", shown)
        self.assertIn("their diagnostics and identities were not recorded", shown)
        self.assertNotIn("25 identities recorded", shown)

    def test_display_shows_twenty_declares_omitted_total_and_changes_no_record(self):  # noqa: VACUOUS_ASSERTION — positive output controls assert total, last shown identity and omission line before the hidden 21st identity is asserted absent
        got = gate.parse_result(_failure_stream(gate.FAILURE_CAP + 5))
        record, _chunks = gate._failure_record(got["failures"])
        row = dict(record, v=8, failures_unreadable=False)
        before = json.loads(json.dumps(row))
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            gate._show_failures(row)
        shown = stdout.getvalue()
        self.assertIn("25 recorded", shown)
        self.assertIn("tests.test_many.Many.test_19", shown)
        self.assertNotIn("tests.test_many.Many.test_20", shown)
        self.assertIn("5 more failure diagnostics omitted", shown)
        self.assertIn("25 identities recorded", shown)
        self.assertEqual(row, before)

    def test_malformed_failure_entries_render_unreadable_instead_of_vanishing(self):
        row = {"v": 4, "failures_unreadable": False, "failures": [
            {"kind": "FAIL", "test": "tests.probe.Case.test_ok",
             "traceback": "frame"},
            {"kind": "FAIL", "traceback": "missing identity"},
            "not a mapping",
        ]}
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            gate._show_failures(row)
        self.assertEqual(stdout.getvalue().count("<unreadable entry>"), 2)

    def test_log_lines_with_dotted_parens_are_not_unittest_blocks(self):
        text = ("ERROR: backend unavailable (evil.Case.test_forged)\n"
                "FAIL: retry exhausted (evil.Case.test_other)\n"
                "Ran 1 test in 0.1s\n\nFAILED (errors=1)\n")
        got = gate.parse_result(_floor(text))
        self.assertEqual(got["failures"], [])
        self.assertTrue(got["failures_unreadable"])

    def test_separator_backed_log_prose_cannot_mimic_a_test_identity(self):
        mimics = [
            "ERROR: proxywatch retry exhausted (urllib.error.URLError)",
            "ERROR: lookup (api.example.com)",
            "FAIL: import_hook (pkg.module.Loader)",
        ]
        blocks = []
        for header in mimics:
            blocks.append("=" * 70 + "\n" + header + "\n" + "-" * 70 +
                          "\nTraceback (most recent call last):\n"
                          "  File \"/tmp/log.py\", line 1, in emit\n")
        blocks.append("""======================================================================
FAIL: test_real (tests.test_probe.Probe.test_real) (value=1)
real failure description
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/test_probe.py", line 9, in test_real
    self.assertEqual(1, 2)
AssertionError: 1 != 2
""")
        text = "\n".join(blocks) + \
            "\nRan 1 test in 0.1s\n\nFAILED (failures=1)\n"
        got = gate.parse_result(_floor(text))
        self.assertFalse(got["failures_unreadable"])
        self.assertEqual(got["failures"], [{
            "kind": "FAIL",
            "test": "tests.test_probe.Probe.test_real",
            "traceback": "File \"/tmp/test_probe.py\", line 9, in "
                         "test_real | self.assertEqual(1, 2) | "
                         "AssertionError: 1 != 2",
        }])

    def test_fixture_error_cannot_be_replaced_by_dotted_exception_prose(self):
        text = """======================================================================
ERROR: URLError (urllib.error.URLError)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/log.py", line 1, in emit
======================================================================
ERROR: setUpClass (__main__.Probe)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/test_probe.py", line 4, in setUpClass
    raise RuntimeError()
RuntimeError

Ran 0 tests in 0.1s

FAILED (errors=1)
"""
        got = gate.parse_result(_floor(text))
        self.assertTrue(got["failures_unreadable"])
        self.assertEqual(got["failures"], [{
            "kind": "ERROR", "test": "__main__.Probe.setUpClass",
            "traceback": ("File \"/tmp/test_probe.py\", line 4, "
                          "in setUpClass | raise RuntimeError() | "
                          "RuntimeError"),
        }])

    def test_standard_fixture_and_loader_headers_have_canonical_ids(self):  # noqa: VACUOUS_ASSERTION — cases bind positive canonical ids before prose absences
        cases = {
            "setUpClass (__main__.Probe)": "__main__.Probe.setUpClass",
            "tearDownClass (tests.test_probe.Probe)":
                "tests.test_probe.Probe.tearDownClass",
            "setUpModule (tests.test_probe)": "tests.test_probe.setUpModule",
            "tearDownModule (test_probe)": "test_probe.tearDownModule",
            # old-format shapes are safe on EVERY floor: pre-3.11 they are
            # the real emission; 3.11+ reads them as custom ids and concats
            "test_plain (__main__.C)": "__main__.C.test_plain",
            "tests.test_bad (unittest.loader._FailedTest)":
                "unittest.loader._FailedTest.tests.test_bad",
            "test.with.dot (__main__.C)": "__main__.C.test.with.dot",
        }
        if gate._NEW_HEADER_FORMAT:
            # suffixed headers exist only from 3.11; feeding them to an
            # older parser is a shape that interpreter never emits, and
            # concat would double-append — partitioned, never pretended
            cases.update({
                "runTest (__main__.Probe.runTest)": "__main__.Probe.runTest",
                "test_local (__main__.make.<locals>.Probe.test_local)":
                    "__main__.make.<locals>.Probe.test_local",
                "tests.test_bad (unittest.loader._FailedTest.tests.test_bad)":
                    "unittest.loader._FailedTest.tests.test_bad",
                "test.with.dot (__main__.C.test.with.dot)":
                    "__main__.C.test.with.dot",
                "test.with.dot (__main__.test.with.dot.test.with.dot)":
                    "__main__.test.with.dot.test.with.dot",
            })
        else:
            # the old-format CLASS/method collision: concat mints the full id
            cases["test.with.dot (__main__.test.with.dot)"] = \
                "__main__.test.with.dot.test.with.dot"
        for header, expected in cases.items():
            with self.subTest(header=header):
                self.assertEqual(gate._failure_identity(header), expected)
        for prose in ("URLError (urllib.error.URLError)",
                      "lookup (api.example.com)",
                      "import_hook (pkg.module.Loader)"):
            with self.subTest(prose=prose):
                self.assertIsNone(gate._failure_identity(prose))

    def test_a_real_dotted_collision_emission_round_trips(self):
        """The CURRENT interpreter's own emission of the collision pair — a
        generated class whose qualname equals its dotted method name — must
        parse back to exactly case.id(). No synthetic header string can
        drift from what this floor really prints."""
        stream = io.StringIO()
        cls = type("test.with.dot", (unittest.TestCase,), {})
        setattr(cls, "test.with.dot",
                lambda self: self.fail("collision control"))
        case = cls("test.with.dot")
        unittest.TextTestRunner(stream=stream, verbosity=0).run(case)
        header = None
        for line in stream.getvalue().splitlines():
            if line.startswith("FAIL: "):
                header = line[len("FAIL: "):]
                break
        self.assertIsNotNone(header, stream.getvalue())
        self.assertEqual(gate._failure_identity(header), case.id())

    def test_log_candidate_cannot_replace_a_real_runTest_failure(self):
        text = """======================================================================
FAIL: test_retry (logs.Case.test_retry)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/log.py", line 1, in emit
======================================================================
FAIL: runTest (__main__.Probe.runTest)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/test_probe.py", line 7, in runTest
    self.fail("boom")
AssertionError: boom

Ran 1 test in 0.1s

FAILED (failures=1)
"""
        got = gate.parse_result(_floor(text))
        self.assertTrue(got["failures_unreadable"])
        self.assertEqual([item["test"] for item in got["failures"]],
                         ["__main__.Probe.runTest"])

    def test_log_candidate_cannot_replace_an_unsupported_test_object(self):
        text = """======================================================================
FAIL: test_retry (logs.Case.test_retry)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/log.py", line 1, in emit
======================================================================
FAIL: unittest.case.FunctionTestCase (boom)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/test_probe.py", line 3, in boom
    raise AssertionError("boom")
AssertionError: boom

Ran 1 test in 0.1s

FAILED (failures=1)
"""
        got = gate.parse_result(_floor(text))
        self.assertTrue(got["failures_unreadable"])
        self.assertEqual(got["failures"], [])

    def test_log_candidate_cannot_replace_an_unsupported_doctest(self):
        text = """======================================================================
FAIL: test_retry (logs.Case.test_retry)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/log.py", line 1, in emit
======================================================================
FAIL: probe ()
Doctest: probe
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/usr/lib/python/doctest.py", line 1, in runTest
    raise self.failureException(report)
DocTestFailure

Ran 1 test in 0.1s

FAILED (failures=1)
"""
        got = gate.parse_result(_floor(text))
        self.assertTrue(got["failures_unreadable"])
        self.assertEqual(got["failures"], [])
        self.assertTrue(gate._failure_protocol_like("probe ()"))

    def test_log_candidate_cannot_replace_an_unsupported_docfile(self):
        text = """======================================================================
FAIL: test_retry (logs.Case.test_retry)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/log.py", line 1, in emit
======================================================================
FAIL: /tmp/sample.txt
Doctest: sample.txt
----------------------------------------------------------------------
AssertionError: Failed doctest test for sample.txt
  File "/tmp/sample.txt", line 0

Ran 1 test in 0.1s

FAILED (failures=1)
"""
        got = gate.parse_result(_floor(text))
        self.assertTrue(got["failures_unreadable"])
        self.assertEqual(got["failures"], [])
        self.assertTrue(gate._failure_protocol_like(
            "/tmp/sample.txt", "Doctest: sample.txt"))

    def test_log_candidate_cannot_replace_a_local_TestCase_method(self):
        text = """======================================================================
FAIL: test_retry (logs.Case.test_retry)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/log.py", line 1, in emit
======================================================================
FAIL: test_local (__main__.make.<locals>.Probe.test_local)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/test_probe.py", line 7, in test_local
    self.fail("boom")
AssertionError: boom

Ran 1 test in 0.1s

FAILED (failures=1)
"""
        got = gate.parse_result(_floor(text))
        self.assertTrue(got["failures_unreadable"])
        self.assertEqual([item["test"] for item in got["failures"]],
                         ["__main__.make.<locals>.Probe.test_local"])

    def test_earlier_protocol_shaped_logs_cannot_evict_the_real_failure(self):
        fake = []
        for i in range(gate.FAILURE_CAP + 5):
            fake.append("""======================================================================
ERROR: test_%d (evil.Case.test_%d)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/fake.py", line 1, in fake
    raise RuntimeError()
RuntimeError
""" % (i, i))
        real = """======================================================================
ERROR: test_real (tests.test_probe.Probe.test_real)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/test_probe.py", line 9, in test_real
    raise ValueError("real")
ValueError: real
"""
        text = "\n".join(fake) + real + \
            "\nRan 1 test in 0.1s\n\nFAILED (errors=1)\n"
        got = gate.parse_result(_floor(text))
        self.assertTrue(got["failures_unreadable"])
        self.assertEqual([item["test"] for item in got["failures"]],
                         ["tests.test_probe.Probe.test_real"])

    def test_the_LAST_summary_footer_is_authoritative(self):
        got = gate.parse_result(
            "FAILED (failures=1)\nRan 1 test in 0.1s\n\nOK\n")
        self.assertEqual(got["status"], "OK")
        self.assertEqual(got["failures"], [])
        self.assertFalse(got["failures_unreadable"])

    def test_expected_failures_do_not_invent_FAIL_blocks(self):
        text = """======================================================================
ERROR: test_boom (tests.test_probe.Probe.test_boom)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/test_probe.py", line 7, in test_boom
    raise ValueError("boom")
ValueError: boom

Ran 2 tests in 0.1s

FAILED (errors=1, expected failures=1)
"""
        got = gate.parse_result(_floor(text))
        self.assertEqual(got["status"], "FAILED")
        self.assertFalse(got["failures_unreadable"])
        self.assertEqual([item["kind"] for item in got["failures"]], ["ERROR"])

    def test_exception_group_traceback_keeps_its_diagnosis(self):
        text = """======================================================================
ERROR: test_group (tests.test_probe.Probe.test_group)
----------------------------------------------------------------------
  + Exception Group Traceback (most recent call last):
  |   File "/tmp/test_probe.py", line 9, in test_group
  |     raise ExceptionGroup("many", [ValueError("x")])
  | ExceptionGroup: many (1 sub-exception)
  +-+---------------- 1 ----------------

Ran 1 test in 0.1s

FAILED (errors=1)
"""
        got = gate.parse_result(_floor(text))
        self.assertFalse(got["failures_unreadable"])
        self.assertEqual(
            got["failures"][0]["traceback"],
            "File \"/tmp/test_probe.py\", line 9, in test_group | "
            "raise ExceptionGroup(\"many\", [ValueError(\"x\")]) | "
            "ExceptionGroup: many (1 sub-exception)")

    def test_no_summary_is_unknown_never_ok(self):
        """A run killed mid-flight prints no verdict line. Reading that as
        anything but UNKNOWN is how a missing input becomes a pass — the exact
        shape of the GraalPy hang this module exists for."""
        for text in ("", "Traceback (most recent call last):\n  boom\n"):
            self.assertEqual(gate.parse_result(_floor(text))["status"], "UNKNOWN")

    def test_truncated_run_keeps_the_count_and_withholds_the_verdict(self):
        got = gate.parse_result("Ran 300 tests in 90.0s\n")
        self.assertEqual(got["ran"], 300)          # truthful
        self.assertEqual(got["status"], "UNKNOWN")  # not observed

    def test_malformed_elapsed_cannot_turn_an_unread_count_into_OK(self):
        got = gate.parse_result("Ran 1 test in 1..2s\n\nOK\n")
        self.assertEqual(got["status"], "UNKNOWN")
        self.assertIsNone(got["ran"])
        self.assertIn("run count is unreadable", got["detail"])
        self.assertTrue(got["failures_unreadable"])

    def test_a_later_malformed_count_invalidates_an_earlier_valid_one(self):
        got = gate.parse_result(
            "Ran 1 test in 0.1s\nRan 999 tests in 1..2s\n\nOK\n")
        self.assertEqual(got["status"], "UNKNOWN")
        self.assertIsNone(got["ran"])
        self.assertIn("terminal footer", got["detail"])

    def test_post_summary_protocol_cannot_replace_the_terminal_count(self):
        got = gate.parse_result(
            "Ran 1 test in 0.1s\n\nOK\nRan 999 tests in 9.999s\n")
        self.assertEqual(got["status"], "UNKNOWN")
        self.assertIsNone(got["ran"])
        self.assertIn("terminal footer", got["detail"])


class UnreadableNamesTheConditionThatFired(GateBase):
    """AN UNREADABLE RUN IS A QUESTION, AND THESE ARE ITS ANSWERS.

    Several independent conditions make a run unreadable, and each one asks a
    different thing of whoever reads the receipt: a summary naming no counts
    means the footer is the wrong shape; a count that disagrees with the
    blocks means the parse and the runner saw different failures; a block
    with no id or no diagnosis means the transcript is truncated or the
    emission format moved. A single boolean answers none of those, and the
    reader who most needs the answer — the one holding a receipt, with the
    reference-run text already discarded — has nothing else to consult.

    So the parse states the condition IN WORDS. These arms hold it to two
    properties: every condition states something, and no two conditions state
    the SAME thing. The second is what makes the first worth having; a
    constant sentence would satisfy an each-one-speaks arm completely.

    The markers asserted per row are the shortest fragment that DISCRIMINATES
    that condition rather than the whole rendered sentence: a sentence pins
    wording no parse supplies, which costs a red without a defect."""

    _BLOCK = ("=" * 70 + "\n"
              "%s: test_probe (tests.test_probe.Probe.test_probe)\n"
              + "-" * 70 + "\n"
              "Traceback (most recent call last):\n"
              '  File "/tmp/test_probe.py", line 7, in test_probe\n'
              '    self.fail("boom")\n'
              "AssertionError: boom\n\n")
    _HEADLESS = ("=" * 70 + "\n"
                 "FAIL: test_probe (tests.test_probe.Probe.test_probe)\n"
                 + "-" * 70 + "\n\n")
    _TAIL = ("-" * 70 + "\nRan 1 test in 0.1s\n\n%s\n")

    def _run(self, body, footer):
        return _floor(body + (self._TAIL % footer if footer else ""))

    def conditions(self):
        """(label, transcript, discriminating marker) per unreadable shape."""
        return [
            ("the footer names no counts",
             self._run(self._BLOCK % "FAIL",
                       "FAILED (unexpected successes=1)"),
             "no failures= or errors= count"),
            ("the summary counts more failures than there are blocks",
             self._run(self._BLOCK % "FAIL", "FAILED (failures=2)"),
             "protocol-shaped"),
            ("the summary and the blocks disagree on KIND",
             self._run(self._BLOCK % "ERROR", "FAILED (failures=1)"),
             "the blocks are"),
            ("a block carries no diagnosis",
             self._run(self._HEADLESS, "FAILED (failures=1)"),
             "no id or no diagnosis"),
            ("a passing summary carries failure blocks",
             self._run(self._BLOCK % "FAIL", "OK"),
             "passing summary"),
            ("the run printed no summary at all",
             self._run(self._BLOCK % "FAIL", "") + "Ran 1 test in 0.1s\n\n",
             "no readable summary"),
            ("the summary is not the terminal footer",
             self._run(self._BLOCK % "FAIL", "FAILED (failures=1)")
             + "\nhandle leak warning\n",
             "terminal footer"),
        ]

    def test_every_unreadable_condition_states_which_one_fired(self):
        rows = self.conditions()
        # POSITIVE CONTROL ON THE TABLE ITSELF: an empty or truncated table
        # would pass every assertion below over nothing at all.
        self.assertGreaterEqual(len(rows), 7, rows)
        for label, text, marker in rows:
            got = gate.parse_result(text)
            self.assertTrue(got["failures_unreadable"],
                            "%s reads as READABLE, so this row no longer "
                            "exercises the condition it names" % label)
            reason = got["unreadable_reason"]
            self.assertTrue(reason, "%s is unreadable and says nothing about "
                                    "why, which is the whole defect" % label)
            self.assertIn(marker, reason,
                          "%s states a reason that does not name it: %r"
                          % (label, reason))

    def test_a_bad_footer_outranks_a_block_reason_that_is_also_available(self):
        """task/2193 — the precedence in `unreadable_reason` is a JUDGEMENT,
        and the table above cannot see it.

        `parse_result` writes the reason as `(detail or why) if not footer_ok
        else why`, and the comment argues the footer outranks the block
        reasons: without a terminal footer the summary is not the run's own
        verdict, so a block-level reason would be describing text there is no
        cause to trust. That is the right call and nothing checked it. Across
        the table's rows exactly ONE has a bad footer, and on that row the
        block-level `why` is None — so no row carries BOTH, `detail or why`
        and `why or detail` are indistinguishable over every one of them, and
        an edit flipping the order stays green.

        AND THE TWO SENTENCES ARE MATERIALLY DIFFERENT for the shape nothing
        covers. A bad footer plus a count mismatch can say "the summary says
        FAILED but the run count is unreadable or not its terminal footer" or
        "the summary counts 2 failures and 1 protocol-shaped block parsed".
        The second is the more actionable of the two and is the one the
        design deliberately discards, which is exactly why the choice needs a
        witness rather than a comment.

        THE PAIR DIFFERS IN ONE TRAILING LINE. Both transcripts carry the
        same block/count mismatch, so the block reason is available on both;
        only the second has text after the summary. Nothing else about them
        can explain the change in what is served.

        AND THE TWO `detail`s ARE DIFFERENT VARIABLES, which is worth stating
        because it reads as a feedback edge and is not. When footer_ok is
        False, `parse_result` OVERWRITES `out["detail"]` with the footer
        sentence — while `_parse_failures` is handed the LOCAL `detail`,
        which that overwrite never touches. Two names one line apart in the
        reader's eye, and reasoning from the wrong one says the count
        mismatch is not computable on a bad-footer transcript, which would
        make the competing reason unreachable and the assertion below
        unfalsifiable. It is computable; the competitor is real; the control
        above proves it.

        MEASURED BY INSTRUMENTING THE REAL CALL THROUGH THIS CLASS'S OWN
        FIXTURE, because that is the only input whose shape the code agrees
        with: `_parse_failures` receives the FAILURE-BLOCK TEXT while
        `out["detail"]` holds the footer sentence, and the two are not equal.
        Driving the same probe with a hand-built transcript answers a
        question about the transcript instead — it hands `_parse_failures`
        the whole string — so a reconstructed input is the one reliable way
        to get this paragraph wrong, and it yields a third value that
        neither variable holds.
        """
        mismatch = self._run(self._BLOCK % "FAIL", "FAILED (failures=2)")
        both = mismatch + "\nhandle leak warning\n"
        # CONTROL FIRST, UNCONDITIONAL: the block reason has to be REACHABLE
        # on this transcript, or the assertion that the footer outranks it is
        # a claim about a precedence that never had two candidates.
        available = gate.parse_result(mismatch)["unreadable_reason"]
        self.assertIn("protocol-shaped", available,
                      "the count mismatch states no block reason, so there is "
                      "nothing for the footer to outrank and this arm proves "
                      "nothing: %r" % available)
        got = gate.parse_result(both)
        self.assertTrue(got["failures_unreadable"],
                        "both conditions present and the run reads READABLE")
        served = got["unreadable_reason"]
        self.assertIn("terminal footer", served,
                      "with BOTH conditions present the footer reason is not "
                      "the one served: %r" % served)
        self.assertNotIn(  # noqa: VACUOUS_ASSERTION — control is the same text without the trailing line, necessarily a different parse
            "protocol-shaped", served,
            "the block reason was served over the footer reason; untrusted "
            "text is being described as if it were the run's own verdict")

    def test_no_two_conditions_state_the_same_reason(self):
        """WHAT MAKES THE REASON WORTH READING. One constant sentence passes
        an each-one-speaks arm on every row; it tells a receipt reader exactly
        as much as the boolean did."""
        rows = self.conditions()
        # UNCONDITIONAL, OUTSIDE THE LOOP, AND BOUND TO A NAME. A control that
        # runs only per-row does not run at all over an emptied table, and the
        # distinctness assertion below is perfectly satisfied by zero rows.
        self.assertGreaterEqual(len(rows), 7, rows)
        first = gate.parse_result(rows[0][1])["unreadable_reason"]
        self.assertTrue(first, "the first condition states no reason at all, "
                               "so every comparison below is over absences")
        seen = {}
        for label, text, _marker in rows:
            reason = gate.parse_result(text)["unreadable_reason"]
            # THE UNCONDITIONAL POSITIVE ON THE SAME OBSERVABLE. Distinctness
            # over a table of None values holds perfectly the first time and
            # fails on the second, which reads as a wording collision rather
            # than as a field nobody populated.
            self.assertTrue(reason, "%s states no reason at all, so this arm "
                                    "is comparing absences" % label)
            self.assertNotIn(reason, seen,
                             "%s and %s render the same reason, so the "
                             "receipt cannot tell them apart: %r"
                             % (label, seen.get(reason), reason))
            seen[reason] = label
        # THE POSITIVES ON THE COLLECTION THE ABSENCES WERE MEASURED AGAINST.
        # Each assertNotIn above constrains `seen`, and `seen` is empty on the
        # first pass, so the absences alone are satisfied by a table that
        # never populated it. These say what it MUST hold: the first
        # condition's reason is in there, and there is one reason per
        # condition with none of them merged.
        self.assertIn(first, seen,
                      "the reason the first condition states is not among "
                      "the reasons collected, so this dict is not the "
                      "observable those comparisons were made against")
        self.assertEqual(len(seen), len(rows),
                         "the conditions render %d distinct reason(s) over %d "
                         "rows, so a receipt cannot tell some of them apart: "
                         "%r" % (len(seen), len(rows), seen))

    def test_a_readable_failure_states_no_reason(self):
        """THE MUST-MISS. A reason attached to a run that parsed cleanly would
        make every receipt carry one, and a field that is always present is
        read as decoration rather than as a finding."""
        got = gate.parse_result(
            self._run(self._BLOCK % "FAIL", "FAILED (failures=1)"))
        self.assertEqual(got["status"], "FAILED")
        self.assertFalse(got["failures_unreadable"])
        self.assertIsNone(got["unreadable_reason"])
        self.assertEqual([item["test"] for item in got["failures"]],
                         ["tests.test_probe.Probe.test_probe"])


class TrunkStandingTest(GateBase):
    """task/387: a GREEN gate receipt says nothing about whether the ground it
    stood on is still there.

    WHY THIS IS NOT `base_check`, since the names are one word apart and I
    nearly built the wrong thing: `_base_check` asks WHO OWNS THESE FAILURES
    (STALE_BASE / LANE_OWNED / NOT_STALE) and is inherently about failures, so
    a green receipt correctly carries none. Measured on the live
    ledger: 1484 receipts, 203 with a base check, 635 v4 receipts without —
    and every green one is in the second set. `trunk_standing` asks the other
    question, for every receipt including green ones."""

    def _row(self, head):
        return {"v": 4, "id": "deadbeefdeadbeef", "status": "OK", "head": head}

    def _standing(self, row, root=None):
        """(state, reason) off the real dict, so every arm exercises the one
        shape both surfaces read."""
        got = gate.trunk_standing(row, root or self.repo)
        return got["state"], got["reason"]

    def test_the_STATE_and_the_LABEL_bind_ONE_snapshot(self):  # noqa: VACUOUS_ASSERTION — VERIFIED: the residual flags are FIXTURE PRECONDITIONS over two local shas (assertRegex on `rich`, assertNotEqual(rich, poor), and the final assertEqual that the ref really moved), which compare local strings and so have no production call for a matching positive control. The test's property carries unconditional positive controls on the observable — assertEqual(state, vcs.PATCH_EQUIVALENT) plus assertIn("main", reason) and assertIn("CONTENT", reason) on the rendered verdict — so no state passes by the function being inert.
        """The THIRD instance in this one function of
        a single family: A LABEL THAT DOES NOT BIND WHAT WAS MEASURED. First
        the readback that proved presence-at-an-instant and was named "live";
        then the snapshot that spoke for trunk; now a verdict whose STATE was
        measured against a movable ref while its LABEL named a sha resolved in
        a separate read. Three of one shape is why the cure is structural —
        resolve ONCE and compare against the resolved sha, leaving no gap for
        a fourth instance.

        THE PROBE, deterministic: let the ref MOVE after it is resolved. The
        reported sha still contains the work; the ref no longer does. A verdict
        bound to its own label must describe the sha it names."""
        from helm import vcs
        # Put the lane's CONTENT on trunk under a different sha, so the
        # resolved snapshot is genuinely PATCH_EQUIVALENT...
        self._git("checkout", "-q", "main")
        with open(os.path.join(self.repo, "moved.txt"), "w") as fh:
            fh.write("trunk moves first\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "trunk moves under the lane")
        self._git("cherry-pick", self.head)
        rich = self._git("rev-parse", "main")
        poor = self._git("rev-parse", "main~2")     # before the lane content
        self._git("checkout", "-q", "lane/probe")
        self.assertRegex(rich, r"^[0-9a-f]{40}$")
        self.assertNotEqual(rich, poor)

        real = vcs.backend(self.repo)

        class MovingRef:
            """Resolves to `rich`, then moves the ref back to `poor` — exactly
            the window the probe opened."""
            def trunk_ref(self, root):
                return "main"

            def head_sha(self, root, ref, **kw):
                sha = real.head_sha(root, ref, **kw)
                subprocess.run(("git", "update-ref", "refs/heads/main", poor),
                               cwd=root, capture_output=True)
                return sha

            def landed_state(self, root, tip, ref, **kw):
                return real.landed_state(root, tip, ref, **kw)

        with mock.patch.object(vcs, "backend", return_value=MovingRef()):
            got = gate.trunk_standing(self._row(self.head), self.repo)
        # THE PROPERTY: the state describes the sha the verdict NAMES. Bound to
        # `rich`, the lane's content is on trunk, so PATCH_EQUIVALENT. Asking
        # the moved ref name instead would answer about `poor` and say
        # NOT_ANCESTOR while still printing rich's sha.
        self.assertEqual(got["ref_sha"], rich)
        self.assertEqual(got["state"], vcs.PATCH_EQUIVALENT,
                         "the state was measured against the moved ref, not "
                         "against the sha this verdict reports")
        # UNCONDITIONAL POSITIVE CONTROLS on the rendered verdict: it names the
        # ref and carries PATCH_EQUIVALENT's own words, so the state assertion
        # above is not passing against an empty or default reason.
        self.assertIn("main", got["reason"])
        self.assertIn("CONTENT", got["reason"])
        # POSITIVE CONTROL, unconditional: the fixture really did move the ref,
        # so the arm exercised the window rather than a repo that sat still.
        self.assertEqual(self._git("rev-parse", "main"), poor)

    def test_every_verdict_NAMES_THE_REF_AND_ITS_SHA(self):
        """`trunk_ref`'s own contract asks for this and I had not done it:
        "STALENESS IS A THIRD WRONG ANSWER THAT LOOKS RIGHT ... Naming the ref
        (and its sha) in the verdict is what makes a stale answer diagnosable
        instead of merely wrong." A verdict that says only "not on trunk"
        cannot be checked by the person reading it."""
        got = gate.trunk_standing(self._row(self.head), self.repo)
        self.assertEqual(got["ref"], "main")
        self.assertRegex(got["ref_sha"], r"^[0-9a-f]{40}$")
        # the RENDERED reason carries them too, because that is the surface a
        # human actually reads
        self.assertIn("main", got["reason"])
        self.assertIn(got["ref_sha"][:12], got["reason"])

    def test_a_STALE_ref_does_not_get_to_say_never_landed(self):
        """An exact-tip refutation, and it is the same defect fixed in
        mcpd.mint wearing different clothes: a TRUE observation
        published under a bigger claim.

        With a stale remote-tracking ref this returned NOT_ANCESTOR and the
        prose said the work "never landed" — about work that WAS on trunk.
        After nothing but a fetch, the identical call returned ANCESTOR. The
        READING was right; the CLAIM was not. `origin/main` moves only on
        fetch, so every answer here is about a SNAPSHOT."""
        from helm import vcs
        # A real remote whose main has ADVANCED past what this clone last saw.
        origin = os.path.join(self.tmp, "origin")
        subprocess.run(("git", "clone", "-q", "--bare", self.repo, origin),
                       capture_output=True)
        self._git("remote", "add", "origin", origin)
        self._git("fetch", "-q", "origin")
        self._git("checkout", "-q", "main")
        with open(os.path.join(self.repo, "landed-later.txt"), "w") as fh:
            fh.write("this reaches trunk\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "work that DOES land")
        landed = self._git("rev-parse", "HEAD")
        self._git("push", "-q", "origin", "main")   # real trunk now has it
        # ...but this clone's remote-tracking snapshot is deliberately behind:
        self._git("update-ref", "refs/remotes/origin/main", "HEAD~1")

        state, reason = self._standing(self._row(landed))
        self.assertEqual(state, vcs.NOT_ANCESTOR,
                         "fixture failed to produce the stale reading")
        # THE PROPERTY: the wrong-looking state is allowed, the OVERCLAIM is
        # not. It must not pronounce on trunk, and it must name the cure.
        self.assertNotIn("never landed", reason.replace(
            "before treating this as never landed", ""))
        self.assertIn("AS THIS REPO LAST SAW IT", reason)
        self.assertIn("git fetch", reason)

        # UNCONDITIONAL POSITIVE CONTROL, the review's own repro: after a
        # fetch and NOTHING else, the identical call flips to ANCESTOR. This
        # proves the staleness was the whole difference.
        self._git("fetch", "-q", "origin", "main")
        self._git("update-ref", "refs/remotes/origin/main",
                  self._git("rev-parse", "origin/main"))
        state, reason = self._standing(self._row(landed))
        self.assertEqual(state, vcs.ANCESTOR)
        self.assertIn("ancestry", reason)

    def test_the_JSON_surface_carries_the_SAME_projection_marked_as_derived(self):
        """The text show carried `standing` and --json returned only the
        stored row. A tool reading --json is exactly the caller that would ACT
        on a receipt, so withholding it there and printing it for the human is
        backwards. Under its own key because it is DERIVED — computed against
        this repo's trunk at read time — and merging it into the row would make
        a projection indistinguishable from recorded evidence."""
        row = self._row(self.head)
        # UNCONDITIONAL POSITIVE CONTROL on the same observable the absence
        # assertion at the end reads: the fixture row really is a populated
        # receipt, so "the row was not mutated" is a statement about something
        # rather than about an empty dict.
        self.assertEqual(row["status"], "OK")
        with mock.patch.object(gate, "by_id", return_value=(row, None)), \
             mock.patch.object(os, "getcwd", return_value=self.repo):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = gate._cmd_show(["deadbeefdeadbeef", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertIn("standing_projection", payload)
        self.assertEqual(payload["standing_projection"],
                         gate.trunk_standing(row, self.repo))
        # DERIVED, NOT STORED: the receipt's own keys are untouched, so a
        # reader can still tell evidence from projection.
        self.assertEqual(payload["head"], row["head"])
        self.assertNotIn("standing", row)

    def test_the_four_states_come_from_the_REAL_backend(self):  # noqa: VACUOUS_ASSERTION — VERIFIED, not waved: the flagged items are the three `assertNotEqual(<sha>, <sha>)` FIXTURE PRECONDITIONS, which compare two local strings and so have no production call for a matching positive control to sit on; each is immediately preceded by an unconditional `assertRegex(<sha>, ^[0-9a-f]{40}$)` so neither side can be empty. The test's actual property carries THREE unconditional positive controls on the observable under test — assertEqual(state, vcs.ANCESTOR), assertEqual(state, vcs.NOT_ANCESTOR) and assertEqual(state, vcs.PATCH_EQUIVALENT), each on a real gate.trunk_standing() call against a real repo — so no state can pass by the function being inert.
        """Driven through vcs.backend against a real git repo, not a stubbed
        return value — a hand-shaped equivalent would prove the reason strings
        exist and nothing about whether the states are reachable."""
        from helm import vcs
        # THE FIXTURE'S OWN SHAPE, MEASURED RATHER THAN ASSUMED: GateBase
        # leaves HEAD on `lane/probe` with `main` behind it, and no remote, so
        # trunk_ref resolves to the local `main`. My first cut asserted
        # ANCESTOR for self.head and got not-ancestor — self.head is the LANE
        # tip and is correctly not on trunk. The fixture hands us all three
        # live states for free; the bug was in what I expected of it.
        trunk_tip = self._git("rev-parse", "main")
        # POSITIVE first, then the fork: asserting only that two shas DIFFER
        # passes just as well when both are empty strings.
        self.assertRegex(trunk_tip, r"^[0-9a-f]{40}$")
        self.assertRegex(self.head, r"^[0-9a-f]{40}$")
        self.assertNotEqual(trunk_tip, self.head)   # the fixture really forks

        state, reason = self._standing(self._row(trunk_tip), self.repo)
        self.assertEqual(state, vcs.ANCESTOR)
        self.assertIn("ancestry", reason)

        # NOT_ANCESTOR — the orphaned-green case task/387 names: a receipt
        # attesting a tree that is not on trunk and whose content is not
        # there either.
        state, reason = self._standing(self._row(self.head), self.repo)
        self.assertEqual(state, vcs.NOT_ANCESTOR)
        # The reason names the REF IT COMPARED AGAINST, not a claim about what
        # trunk contains — see test_a_STALE_ref_does_not_get_to_say_never_landed
        # for why the stronger sentence had to go.
        self.assertIn("AS THIS REPO LAST SAW IT", reason)

        # PATCH_EQUIVALENT — the state that makes this worth wiring at all.
        # TRUNK MUST MOVE FIRST, and the assertion below is why I know: a
        # straight cherry-pick of a commit whose parent IS trunk's tip
        # reproduces the byte-identical object (same tree, same parent, same
        # metadata), so the fixture handed back ANCESTOR and proved nothing.
        # One unrelated commit on trunk forces a genuine re-sha.
        self._git("checkout", "-q", "main")
        with open(os.path.join(self.repo, "trunk-moved.txt"), "w") as fh:
            fh.write("someone else landed first\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "trunk moves under the lane")
        self._git("cherry-pick", self.head)
        self._git("checkout", "-q", "lane/probe")
        picked = self._git("rev-parse", "main")
        self.assertRegex(picked, r"^[0-9a-f]{40}$")   # positive before absence
        self.assertNotEqual(picked, self.head)
        state, reason = self._standing(self._row(self.head), self.repo)
        self.assertEqual(state, vcs.PATCH_EQUIVALENT,
                         "the same content is on trunk under a different sha")
        self.assertIn("CONTENT", reason)

    def test_a_receipt_with_no_head_is_UNKNOWN_not_clean(self):
        """The absent case must not read as the good case. A receipt we cannot
        locate is unmeasured, and unmeasured is not landed."""
        from helm import vcs
        # gate.trunk_standing DIRECTLY here rather than through self._standing:
        # the vacuity rung tracks call IDENTITY, so routing both the assertion
        # and its control through a helper gives them two different call ids
        # and the control stops covering anything. Measured — this test was
        # clear before the helper existed and reddened the moment it did.
        for head in (None, "", "   ", 17):
            with self.subTest(head=head):
                got = gate.trunk_standing({"head": head}, self.repo)
                self.assertEqual(got["state"], vcs.UNKNOWN)
                self.assertIn("no head", got["reason"])
        # UNCONDITIONAL POSITIVE CONTROL on the same call, and it must ASSERT
        # A VALUE rather than merely differ from UNKNOWN: `assertNotEqual`
        # is itself an absence assertion and proves nothing about a function
        # stuck on some third answer. self.head is the lane tip, which the
        # fixture leaves genuinely off trunk.
        got = gate.trunk_standing(self._row(self.head), self.repo)
        self.assertEqual(got["state"], vcs.NOT_ANCESTOR)
        self.assertIn("AS THIS REPO LAST SAW IT", got["reason"])

    def test_an_unreadable_repo_is_UNKNOWN_and_SAYS_it_is_unmeasured(self):
        from helm import vcs
        with mock.patch.object(vcs, "backend",
                               side_effect=OSError("no such repo")):
            state, reason = self._standing(self._row(self.head),
                                           self.repo)
        self.assertEqual(state, vcs.UNKNOWN)
        self.assertIn("unmeasured", reason)
        self.assertIn("not clean", reason)

    def test_an_UNRECOGNISED_state_announces_itself_rather_than_rendering_blank(self):
        """This fallback is not decoration — it is what caught a real bug. I
        keyed the reason table off the UPPER_SNAKE names in landed_state's
        DOCSTRING while the values are lowercase-hyphenated, so every real
        receipt printed `unrecognised state 'patch-equivalent'`. A docstring
        naming a constant is not the constant. A fifth state must be as loud."""
        fake = mock.Mock()
        fake.trunk_ref.return_value = "origin/main"
        fake.head_sha.return_value = "0" * 40
        fake.landed_state.return_value = "a-fifth-state"
        from helm import vcs
        with mock.patch.object(vcs, "backend", return_value=fake):
            state, reason = self._standing(self._row(self.head),
                                           self.repo)
        self.assertEqual(state, "a-fifth-state")
        self.assertIn("unrecognised state", reason)
        self.assertIn("needs updating", reason)

    def test_the_line_is_PRINTED_for_a_GREEN_receipt_and_is_not_the_base_line(self):
        """The whole defect is an ABSENCE on green receipts, so the arm is that
        the line appears at all — and that it is a SEPARATE line from `base`,
        which answers a different question and would be read as this one."""
        row = self._row(self.head)
        row["base_check"] = None            # green: no base check, by design
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gate._show_base_check(row)      # prints nothing for a green row
            gate._show_trunk_standing(row, self.repo)
        out = buf.getvalue()
        self.assertIn("standing", out)
        self.assertIn("ANCESTOR", out)
        # NEGATIVE CONTROL: the green row really did suppress the base line, so
        # `standing` is new information and not a rename of an existing one.
        self.assertNotIn("  base ", out)


class VersionGatedReaders(unittest.TestCase):
    """Receipt versions are interpreted only through the shared registries.

    A bare field/version comparison silently becomes a second schema registry.
    The guard walks BOTH owner and importer with no line whitelist: future
    versions must extend RECEIPT_FIELD_REGISTRY/RECEIPT_VERSIONS, never add one
    more ``row.get('v') == N`` reader that can forget v8 fields.
    """

    @staticmethod
    def _version_read(node):
        return isinstance(node, ast.Call) \
            and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "get" \
            and node.args and isinstance(node.args[0], ast.Constant) \
            and node.args[0].value == "v"

    @staticmethod
    def _named_version(node):
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        else:
            return False
        return name.endswith("_VERSION") or name.endswith("_VERSIONS")

    def test_every_minted_version_is_admitted_by_every_reader(self):  # noqa: VACUOUS_ASSERTION — assertTrue(MINTED_RECEIPT_VERSIONS) and assertEqual(..., (4, 8)) are unconditional positive controls on the same observable and run FIRST; the assertNotIn arms below read that same registry, so an empty registry reddens before any absence claim is reached
        """Trunk's `test_every_version_gated_reader_admits_the_minted_version`,
        re-expressed against the registry that replaced its subject.

        THAT ARM SCRAPED THE SOURCE and its input no longer exists. It parsed
        `_mint_result` for an inline ``{"v": <int literal>}`` and then walked
        every ``row.get("v") in (...)`` tuple in gate.py, asserting the minted
        version appeared in each. Both halves are gone by construction: the
        mint now reads MINTED_RECEIPT_VERSIONS, and the readers ask the
        registry instead of restating tuples — which is precisely the defect
        class that arm was built to detect, removed rather than policed.

        A scrape over an empty set passes silently, so the guarantee is
        restated here directly against the predicates production actually
        calls. This is strictly stronger than the AST walk: it asserts what
        the readers ANSWER, not what the source looks like."""
        from helm import gateimport

        # POSITIVE CONTROL, unconditional: if the mint set were empty every
        # loop below would be vacuous and this arm would be decoration.
        self.assertTrue(gate.MINTED_RECEIPT_VERSIONS)
        self.assertEqual(gate.MINTED_RECEIPT_VERSIONS, (4, 8))

        # Every version the gate can stamp is one every READER admits.
        for version in gate.MINTED_RECEIPT_VERSIONS + (gate.FOCUSED_VERSION,):
            self.assertTrue(gate.receipt_version_known(version),
                            "gate mints v%d and its own reader refuses it — "
                            "a receipt nothing can read" % version)
            self.assertIn(version, gate.RECEIPT_VERSIONS)

        # The generic import door admits every minted version EXCEPT the
        # focused kind, which is routed-only on purpose.
        for version in gate.MINTED_RECEIPT_VERSIONS:
            self.assertIn(version, gateimport.KNOWN_VERSIONS)
        self.assertNotIn(gate.FOCUSED_VERSION, gateimport.KNOWN_VERSIONS)

        # MUST-MISS: the withdrawn integer is admitted by NOTHING. Naming an
        # input this arm has to reject is what keeps it from measuring intent.
        self.assertNotIn(gate.WITHDRAWN_SHARD_VERSION, gate.RECEIPT_VERSIONS)
        self.assertNotIn(gate.WITHDRAWN_SHARD_VERSION,
                         gateimport.KNOWN_VERSIONS)
        self.assertFalse(
            gate.receipt_version_known(gate.WITHDRAWN_SHARD_VERSION))

    def test_the_import_door_is_derived_from_the_reader_not_restated(self):  # noqa: VACUOUS_ASSERTION — every assertion here is a positive equality on a non-empty derived value (KNOWN_VERSIONS, VERSION_KEYS[5], the focused key pair); there is no absence claim, and an empty registry fails the first assertEqual
        """The two constants that must agree cannot disagree.

        `KNOWN_VERSIONS` is the reader's set minus the focused kind. Asserting
        the DERIVATION rather than the literal tuple is what makes this arm
        survive the next version: a transcribed tuple is pinned to nothing and
        stays green while the registry moves underneath it."""
        from helm import gateimport
        self.assertEqual(
            gateimport.KNOWN_VERSIONS,
            tuple(v for v in gate.RECEIPT_VERSIONS
                  if v != gate.FOCUSED_VERSION))
        # PROVE THE DERIVATION BY MOVING ITS INPUT rather than by reading it.
        self.assertEqual(gateimport.VERSION_KEYS[5],
                         gate.receipt_version_keys(5))
        self.assertEqual(gate.receipt_version_keys(gate.FOCUSED_VERSION),
                         frozenset(("host", "focus")))

    def test_no_reader_restates_a_receipt_version_ladder(self):  # noqa: VACUOUS_ASSERTION — two unconditional positive controls on the SAME ast walk run BEFORE the emptiness claim: assertGreaterEqual(len(reads), 3) proves the parse found version comparisons at all, and the assertTrue below it proves the sanctioned named-constant form is actually present. The rung cannot credit either because `reads`/`offenders` are local accumulators rather than a production call's result, which its own docstring says it will not count
        """A reader may name a KIND; it may not restate the LADDER.

        The lane's rule was "no `row.get("v")` comparison anywhere". That is
        too strong against a trunk with three sibling KINDS: 5 has its own
        structured id payload, 6 refuses at the generic import door, and 7 is
        withdrawn — each is a genuine single-version branch that no cumulative
        registry can express. What must never come back is the LADDER restated
        as a literal — `in (2, 3, 4)` — because that is the form that silently
        forgets the newest version.

        So the rule is: compare against a named or qualified kind constant,
        never against a bare integer or a literal tuple of them."""
        import ast
        from helm import gateimport
        offenders = []
        for module in (gate, gateimport):
            tree = ast.parse(inspect.getsource(module))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Compare):
                    continue
                operands = [node.left] + list(node.comparators)
                if not any(self._version_read(part) for part in operands):
                    continue
                others = [p for p in operands if not self._version_read(p)]
                # Named and qualified *_VERSION / *_VERSIONS constants are the
                # sanctioned forms; integers and literal tuples are ladders.
                if all(self._named_version(o) for o in others):
                    continue
                offenders.append((module.__name__, node.lineno,
                                  ast.unparse(node)))
        # POSITIVE CONTROL FIRST, unconditionally, on the SAME walk: if the
        # parse found no version comparison at all the emptiness below is
        # vacuous, and asserting it AFTER the absence claim means a red
        # absence hides the fact that the instrument was blind.
        reads = [n for module in (gate, gateimport)
                 for n in ast.walk(ast.parse(inspect.getsource(module)))
                 if isinstance(n, ast.Compare)
                 and any(self._version_read(part)
                         for part in [n.left] + list(n.comparators))]
        self.assertGreaterEqual(
            len(reads), 3,
            "the AST walk found no version comparison at all — it is not "
            "looking at what it thinks it is")
        # And the sanctioned form must actually be PRESENT, or "no offenders"
        # would also be satisfied by a file that stopped checking versions.
        self.assertTrue(
            [n for n in reads
             if any(self._named_version(o)
                    for o in [n.left] + list(n.comparators))],
            "no named kind constant is compared anywhere — the kinds this "
            "arm permits have vanished, so it is guarding nothing")
        self.assertTrue(
            [n for n in reads
             if any(isinstance(o, ast.Attribute)
                    and self._named_version(o)
                    for o in [n.left] + list(n.comparators))],
            "no qualified kind constant is compared anywhere — ast.Attribute "
            "support has no production positive control")

        for source in ("row.get('v') == gate.FOCUSED_VERSION",
                       "row.get('v') in gate.RECEIPT_VERSIONS"):
            compare = ast.parse(source, mode="eval").body
            self.assertTrue(all(
                self._named_version(o) for o in
                [compare.left] + list(compare.comparators)
                if not self._version_read(o)), source)
        for source in ("row.get('v') == 8", "row.get('v') in (4, 8)"):
            compare = ast.parse(source, mode="eval").body
            self.assertFalse(all(
                self._named_version(o) for o in
                [compare.left] + list(compare.comparators)
                if not self._version_read(o)), source)

        self.assertEqual(
            offenders, [],
            "a receipt-version LADDER is restated instead of asked of the "
            "registry — this is the shape that stops binding a field on the "
            "next version bump: %r" % (offenders,))

    def test_bool_is_not_a_receipt_or_chunk_version(self):
        self.assertFalse(gate.receipt_version_known(True))
        self.assertEqual(gate.receipt_version_keys(True), frozenset())
        chunk = {"v": True, "event": gate._FAILURE_CHUNK_EVENT,
                 "failures": [{"kind": "FAIL", "test": "tests.Case.test_x"}]}
        chunk["id"] = gate._failure_chunk_id(chunk)
        self.assertIn("version True is unsupported", gate._failure_chunk_error(chunk))


class Minting(GateBase):
    def test_receipt_binds_the_real_tree_state(self):
        row = self.mint("Ran 3 tests in 0.1s", "", "OK")
        self.assertEqual(row["head"], self.head)
        self.assertEqual(row["tree"], self._git("rev-parse", "HEAD^{tree}"))
        self.assertFalse(row["dirty"])
        self.assertEqual(row["status"], "OK")
        self.assertEqual(row["ran"], 3)

    def test_lone_surrogate_label_is_unmintable_not_a_gate_crash(self):
        require_supervisor()
        row, err = gate.run(
            repo=self.repo, label="hostile-\ud800",
            argv=[sys.executable, "-c",
                  "print('Ran 1 test in 0.1s\\n\\nOK')"])
        self.assertIsNone(row)
        self.assertIn("receipt not minted", err)
        self.assertIn("not durable UTF-8 JSON", err)
        self.assertEqual(gate.eventledger.events(gate.receipts_path()), [])

    def test_whole_suite_mints_one_bounded_content_bound_timing_sibling(self):
        tree = self._git("rev-parse", "HEAD^{tree}")
        timing = {
            "state": "COMPLETE", "reason": None,
            "planned_modules": 2, "measured_modules": 2,
            "measured_tests": 3, "module_wall": 0.15,
            "process_cpu": 0.11,
            "top": [
                {"module": "tests.test_slow", "tests": 2,
                 "wall": 0.1, "process_cpu": 0.07},
                {"module": "tests.test_fast", "tests": 1,
                 "wall": 0.05, "process_cpu": 0.04},
            ],
        }
        ident = gate.interpreter()
        row, minted, err = gate._mint_result(
            self.repo, self.head, tree, False, ident,
            [ident["executable"]] + list(gateauthority.SERIAL_ARGV),
            True, None, 0, 0, "Ran 3 tests in 0.2s\n\nOK\n",
            module_timing=timing)
        self.assertTrue(minted, err)
        events = gate.eventledger.events(gate.receipts_path())
        self.assertEqual([event["event"] for event in events], [
            gate._TIMING_EVENT, "gate"])
        measured = events[0]
        self.assertEqual(measured["receipt"], row["id"])
        self.assertEqual(measured["state"], "COMPLETE")
        self.assertEqual(measured["measured_modules"], 2)
        self.assertEqual(len(measured["top"]), 2,
                         "positive control: nonempty matched modules survived")
        self.assertGreater(measured["module_wall"], 0)
        self.assertEqual(measured["unattributed_wall"], 0.05)
        self.assertEqual(measured["id"], gate._timing_id(measured))
        self.assertLessEqual(gate._event_bytes(measured),
                             gate.eventledger.MAX_EVENT_BYTES)
        rows, unavailable, skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertEqual(skipped, 0)
        self.assertEqual([stored["id"] for stored in rows], [row["id"]])

    def test_advisory_timing_append_failure_does_not_discard_the_receipt(self):
        tree = self._git("rev-parse", "HEAD^{tree}")
        timing = {
            "state": "COMPLETE", "reason": None,
            "planned_modules": 1, "measured_modules": 1,
            "measured_tests": 1, "module_wall": 0.1,
            "process_cpu": 0.05,
            "top": [{"module": "tests.test_one", "tests": 1,
                     "wall": 0.1, "process_cpu": 0.05}],
        }
        append = gate.eventledger.append_unlocked

        def fail_timing(path, event):
            if event.get("event") == gate._TIMING_EVENT:
                return False
            return append(path, event)

        ident = gate.interpreter()
        with mock.patch.object(
                gate.eventledger, "append_unlocked", side_effect=fail_timing):
            row, minted, err = gate._mint_result(
                self.repo, self.head, tree, False, ident,
                [ident["executable"]] + list(gateauthority.SERIAL_ARGV),
                True, None, 0, 0, "Ran 1 test in 0.2s\n\nOK\n",
                module_timing=timing)
        self.assertTrue(minted, err)
        events = gate.eventledger.events(gate.receipts_path())
        self.assertEqual([event["event"] for event in events], ["gate"])
        self.assertEqual(events[0]["id"], row["id"],
                         "positive control: authoritative receipt still minted")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(gate._cmd_show([row["id"]]), 0)
        self.assertIn("no module timing sibling is available", out.getvalue())
        self.assertNotIn("predates module timing", out.getvalue())
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(gate._cmd_show([row["id"], "--json"]), 0)
        shown = json.loads(out.getvalue())
        self.assertEqual(shown["module_timing"], {
            "state": "UNAVAILABLE",
            "reason": "no module timing sibling is available",
        })

    def test_incomplete_timing_is_unknown_and_cannot_present_a_top_twenty(self):
        receipt = {"id": "a" * 16, "ran": 2, "elapsed": 1.0}
        timing = {
            "state": "COMPLETE", "reason": None,
            "planned_modules": 2, "measured_modules": 1,
            "measured_tests": 1, "module_wall": 0.5,
            "process_cpu": 0.4,
            "top": [{"module": "tests.test_seen", "tests": 1,
                     "wall": 0.5, "process_cpu": 0.4}],
        }
        event = gate._module_timing_event(receipt, timing)
        self.assertEqual(event["state"], "UNKNOWN")
        self.assertIn("disagrees", event["reason"])
        self.assertEqual(event["top"], [])
        self.assertEqual(event["measured_modules"], 1,
                         "positive control: a real partial module was observed")
        self.assertGreater(event["module_wall"], 0)

    def test_complete_timing_accepts_exactly_twenty_of_twenty_one_modules(self):
        receipt = {"id": "a" * 16, "ran": 21, "elapsed": 3.0}
        timing = {
            "state": "COMPLETE", "reason": None,
            "planned_modules": 21, "measured_modules": 21,
            "measured_tests": 21, "module_wall": 2.1,
            "process_cpu": 1.05,
            "top": [{"module": "tests.test_%02d" % n, "tests": 1,
                     "wall": 0.1, "process_cpu": 0.05}
                    for n in range(gatetestrecord.TIMING_TOP)],
        }
        event = gate._module_timing_event(receipt, timing)
        self.assertEqual(event["state"], "COMPLETE")
        self.assertEqual(len(event["top"]), gatetestrecord.TIMING_TOP,
                         "positive control: the bounded ranking is populated")
        self.assertIsNone(gate._timing_event_error(event))
        underfilled = dict(event, top=event["top"][:-1])
        underfilled["id"] = gate._timing_id(underfilled)
        self.assertIn("census", gate._timing_event_error(underfilled))

    def test_complete_timing_module_wall_cannot_exceed_runner_elapsed(self):
        receipt = {"id": "a" * 16, "ran": 1, "elapsed": 2.0}
        timing = {
            "state": "COMPLETE", "reason": None,
            "planned_modules": 1, "measured_modules": 1,
            "measured_tests": 1, "module_wall": 2.0,
            "process_cpu": 1.0,
            "top": [{"module": "tests.test_one", "tests": 1,
                     "wall": 2.0, "process_cpu": 1.0}],
        }
        event = gate._module_timing_event(receipt, timing)
        self.assertEqual(event["state"], "COMPLETE")
        one_row_excess = json.loads(json.dumps(event))
        one_row_excess["top"][0]["wall"] += 0.000001
        one_row_excess["id"] = gate._timing_id(one_row_excess)
        self.assertIn("aggregates",
                      gate._timing_event_error(one_row_excess))
        within = json.loads(json.dumps(event))
        within["module_wall"] = (
            receipt["elapsed"] + gate._TIMING_RUNNER_TOLERANCE)
        within["top"][0]["wall"] = within["module_wall"]
        within["id"] = gate._timing_id(within)
        self.assertIsNone(
            gate._timing_event_error(within),
            "positive control: footer rounding at the boundary is admissible")
        beyond = json.loads(json.dumps(within))
        beyond["module_wall"] += 0.000001
        beyond["top"][0]["wall"] = beyond["module_wall"]
        beyond["unattributed_wall"] = 0.0
        beyond["id"] = gate._timing_id(beyond)
        self.assertIn("exceeds runner elapsed", gate._timing_event_error(beyond))
        for name in ("module_wall", "process_cpu"):
            with self.subTest(null_complete_scalar=name):
                malformed = dict(event, **{name: None})
                malformed["id"] = gate._timing_id(malformed)
                self.assertIn("unreadable", gate._timing_event_error(malformed))

    def test_unknown_timing_requires_finite_display_totals(self):  # noqa: VACUOUS_ASSERTION — the valid UNKNOWN baseline is asserted unconditionally before the populated case table; every case recomputes its content id and asserts the field-specific rejection
        receipt = {"id": "a" * 16, "ran": 1, "elapsed": 1.0}
        event = gate._module_timing_event(receipt, {
            "state": "UNKNOWN", "reason": "partial census",
            "planned_modules": 1, "measured_modules": 0,
            "measured_tests": 0, "module_wall": 0.0,
            "process_cpu": 0.0, "top": [],
        })
        self.assertIsNone(gate._timing_event_error(event))
        cases = (("module_wall", None), ("process_cpu", None),
                 ("module_wall", -0.001), ("process_cpu", float("inf")),
                 ("module_wall", 10 ** 400),
                 ("process_cpu", 10 ** 400))
        for name, value in cases:
            with self.subTest(display_total=name, value=value):
                malformed = dict(event, **{name: value})
                malformed["id"] = gate._timing_id(malformed)
                self.assertIn(name, gate._timing_event_error(malformed))

    def test_timing_reconciliation_rejects_unformattable_integers_totally(self):  # noqa: VACUOUS_ASSERTION — every branch starts from a valid content-bound event, injects the exact JSON integer that formerly raised OverflowError, recomputes its id, and asserts a field-level refusal
        receipt = {"id": "a" * 16, "ran": 1, "elapsed": 1.0}
        event = gate._module_timing_event(receipt, {
            "state": "COMPLETE", "reason": None,
            "planned_modules": 1, "measured_modules": 1,
            "measured_tests": 1, "module_wall": 0.5,
            "process_cpu": 0.25,
            "top": [{"module": "tests.test_one", "tests": 1,
                     "wall": 0.5, "process_cpu": 0.25}],
        })
        self.assertIsNone(gate._timing_event_error(event))
        huge = 10 ** 400
        fallback = gate._module_timing_event(receipt, {
            "state": "COMPLETE", "reason": None,
            "planned_modules": 1, "measured_modules": 1,
            "measured_tests": 1, "module_wall": huge,
            "process_cpu": 0.25,
            "top": [{"module": "tests.test_one", "tests": 1,
                     "wall": huge, "process_cpu": 0.25}],
        })
        self.assertEqual((fallback["state"], fallback["module_wall"]),
                         ("UNKNOWN", 0.0))
        for name in ("runner_elapsed", "unattributed_wall"):
            with self.subTest(total=name):
                malformed = dict(event, **{name: huge})
                malformed["id"] = gate._timing_id(malformed)
                self.assertIn(name, gate._timing_event_error(malformed))
        for name in ("wall", "process_cpu"):
            with self.subTest(top=name):
                malformed = json.loads(json.dumps(event))
                malformed["top"][0][name] = huge
                malformed["id"] = gate._timing_id(malformed)
                self.assertIn("top row", gate._timing_event_error(malformed))

    def test_timing_reconciliation_ignores_same_id_non_receipt_debris(self):
        receipt = self.mint("Ran 1 test in 0.1s", "", "OK")
        timing = gate._module_timing_event(receipt, {
            "state": "COMPLETE", "reason": None,
            "planned_modules": 1, "measured_modules": 1,
            "measured_tests": 1, "module_wall": 0.05,
            "process_cpu": 0.03,
            "top": [{"module": "tests.test_one", "tests": 1,
                     "wall": 0.05, "process_cpu": 0.03}],
        })
        self.assertEqual(timing["state"], "COMPLETE")
        self.assertIsNone(gate._timing_event_error(timing))
        malformed = dict(receipt, ran=2)
        future = dict(receipt, v=99, ran=3)
        self.assertEqual({row["event"] for row in (malformed, future)}, {"gate"})
        self.assertEqual({row["id"] for row in (receipt, malformed, future)},
                         {receipt["id"]})
        self.assertTrue(gate._id_matches(receipt))
        self.assertFalse(gate._id_matches(malformed))
        self.assertFalse(gate._id_matches(future))
        with open(gate.receipts_path(), "a", encoding="utf-8") as fh:
            for row in (timing, malformed, future):
                fh.write(json.dumps(row) + "\n")
        rows, unavailable, skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertEqual(skipped, 2,
                         "positive control: both debris rows were rejected")
        self.assertEqual([row["id"] for row in rows], [receipt["id"]])
        reconciled, err = gate._timing_for_receipt(receipt["id"])
        self.assertIsNone(err, err)
        self.assertEqual(reconciled, timing)

    def test_large_failure_record_mints_as_one_validated_multi_event_object(self):
        text = _failure_stream(130, "x" * gate._FAILURE_TEXT_CAP)
        parsed = gate.parse_result(text)
        self.assertGreater(
            len(json.dumps(parsed["failures"], ensure_ascii=False).encode("utf-8")),
            gate.eventledger.MAX_EVENT_BYTES,
            "must-hit control: the uncapped legacy field must breach 64 KiB")
        tree = self._git("rev-parse", "HEAD^{tree}")
        ident = gate.interpreter()
        with mock.patch.object(gate, "_base_check", return_value={
                "verdict": gate.NOT_STALE, "reason": "measured"}) as base:
            row, minted, err = gate._mint_result(
                self.repo, self.head, tree, False, ident,
                [ident["executable"]] + list(gateauthority.SERIAL_ARGV),
                True, None, 1, 0, text)
        self.assertTrue(minted, err)
        self.assertIsNone(err)
        self.assertEqual(row["v"], 8)
        self.assertEqual(row["failure_total"], 130)
        self.assertEqual(len(row["failures"]), gate.FAILURE_CAP)
        self.assertEqual(row["failure_diagnostics_omitted"], 110)
        projected = base.call_args.args[1]
        self.assertEqual(len(projected), gate.FAILURE_CAP + 1)
        self.assertEqual(projected[-1], {"truncated": 110})
        # AND THE ROW SAYS ITS IDENTITIES SURVIVED, which is a different fact
        # from whether they were CHECKED. The bounded projection above is a
        # cost bound; this kwarg is what lets the refusal say "recorded but
        # not read" instead of "never identified", the sentence that sent two
        # seats to node-side logs for ids the receipt already carried.
        self.assertIs(base.call_args.kwargs["chunked"], True,
                      "a chunked row must tell base-check its identities are "
                      "in the receipt")
        with open(gate.receipts_path(), "rb") as fh:
            raw = fh.readlines()
        self.assertGreater(len(raw), 1)
        self.assertTrue(all(len(line) <= gate.eventledger.MAX_EVENT_BYTES
                            for line in raw))
        events = [json.loads(line) for line in raw]
        self.assertTrue(all(event["event"] == gate._FAILURE_CHUNK_EVENT
                            for event in events[:-1]))
        self.assertEqual(events[-1]["id"], row["id"],
                         "the bindable receipt must be appended last")
        rows, unavailable, skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertEqual(skipped, 0)
        self.assertEqual([stored["id"] for stored in rows], [row["id"]])

    def test_the_cap_refusal_distinguishes_recorded_ids_from_lost_ones(self):
        """ONE VERDICT, TWO DIFFERENT TRUTHS, AND THE ROW DECIDES WHICH.

        Both rows refuse: attribution is bounded to FAILURE_CAP failures on
        purpose, so past the cap the answer is UNKNOWN and no reference work
        runs. That is not what this arm is about. It is about the SENTENCE,
        because the old one asserted a fact that is false of a v8 row — that
        the ids "were never identified" — while `_failure_identity_chunks` had
        just written every one of them into the receipt. A reader who believes
        that goes looking on another box for data already in hand.

        THE PAIR IS THE MEASUREMENT. Same failure stream, same cap, one flag
        apart: the whole-suite row chunks its identities and must say they
        were RECORDED, the focused row records none (its branch in
        `_mint_result` says why it cannot without a new version) and must
        still say they were LOST. If both sentences were the same string, one
        of them would be a lie and this arm could not tell which.

        `_base_check` is REAL here, not mocked — the sentence under test is
        its output, and mocking it would test my own fixture's prose. The
        focus dict is minimal on purpose: only its truthiness reaches the
        branch (it decides whether chunks are written), and a richer plan
        would be a fixture claiming to measure something it does not.
        """
        text = _failure_stream(130, "x" * gate._FAILURE_TEXT_CAP)
        self.assertEqual(len(gate.parse_result(text)["failures"]), 130,
                         "must-hit control: the stream really is over the cap")
        tree = self._git("rev-parse", "HEAD^{tree}")
        ident = gate.interpreter()

        whole, minted, err = gate._mint_result(
            self.repo, self.head, tree, False, ident,
            [ident["executable"]] + list(gateauthority.SERIAL_ARGV),
            True, None, 1, 0, text)
        self.assertTrue(minted, err)
        self.assertEqual(whole["v"], 8, "control: the chunked kind")
        self.assertTrue(whole["failure_chunks"])

        focused, minted, err = gate._mint_result(
            self.repo, self.head, tree, False, ident,
            [ident["executable"], "-m", "unittest"],
            True, None, 1, 0, text,
            focus={"policy": "changed", "selected": ["tests.test_gate"],
                   "universe": 1})
        self.assertTrue(minted, err)
        self.assertEqual(focused["v"], gate.FOCUSED_VERSION,
                         "control: the unchunked kind")
        self.assertNotIn("failure_chunks", focused)

        for name, row in (("whole-suite", whole), ("focused", focused)):
            with self.subTest(name=name):
                self.assertEqual(row["base_check"]["verdict"],
                                 gate.BASE_UNKNOWN,
                                 "the cost bound refuses either way")
        chunked_why = whole["base_check"]["reason"]
        lost_why = focused["base_check"]["reason"]
        self.assertNotEqual(chunked_why, lost_why,
                            "the two rows carry different facts and must not "
                            "share one sentence")
        self.assertIn("ARE recorded in this receipt's failure chunks",
                      chunked_why)
        self.assertNotIn("never identified", chunked_why)
        self.assertIn("never identified", lost_why)
        self.assertNotIn("ARE recorded", lost_why)
        for why in (chunked_why, lost_why):
            self.assertIn("bounded to %d failures" % gate.FAILURE_CAP, why,
                          "both must name the bound as the reason, so a "
                          "reader stops looking for lost data")

    def test_v8_overflow_requires_the_complete_displayed_prefix(self):  # noqa: VACUOUS_ASSERTION — the unconditional valid full-prefix assertion proves the validator seam is live; the fixed two-case table then exercises empty and partial prefixes
        failures = [{"kind": "ERROR", "test": "tests.Case.test_%d" % i,
                     "traceback": "tb"}
                    for i in range(gate.FAILURE_CAP + 1)]
        record, chunks = gate._failure_record(failures)
        chunk_map = {chunk["id"]: chunk for chunk in chunks}
        self.assertIsNone(gate._failure_record_error(record, chunk_map))
        for name, count in (("empty", 0), ("partial", gate.FAILURE_CAP - 1)):
            with self.subTest(name=name):
                edited = dict(
                    record, failures=record["failures"][:count],
                    failure_diagnostics_omitted=record["failure_total"] - count)
                self.assertIn(
                    "complete bounded prefix",
                    gate._failure_record_error(edited, chunk_map))

    def test_large_failure_record_feeds_base_check_only_the_bounded_projection(self):
        text = _failure_stream(gate.FAILURE_CAP + 5)
        tree = self._git("rev-parse", "HEAD^{tree}")
        real_backend = gate.vcs.backend
        calls = []
        def backend(repo):
            calls.append(repo)
            if len(calls) > 1:
                raise AssertionError(
                    "bounded omitted failures spawned reference work")
            return real_backend(repo)
        ident = gate.interpreter()
        with mock.patch.object(gate.vcs, "backend", side_effect=backend):
            row, minted, err = gate._mint_result(
                self.repo, self.head, tree, False, ident,
                [ident["executable"]] + list(gateauthority.SERIAL_ARGV),
                True, None, 1, 0, text)
        self.assertTrue(minted, err)
        self.assertIsNone(err)
        self.assertEqual(calls, [self.repo],
                         "only the ordinary post-run tree read may open git")
        self.assertEqual(row["failure_total"], gate.FAILURE_CAP + 5)
        self.assertEqual(row["base_check"]["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("5 failures beyond", row["base_check"]["reason"])
        # THIS ROW CHUNKS, so its refusal says the ids were RECORDED and not
        # read — it used to say "SKIPPED", which for a chunked row asserted a
        # data loss that had not happened. The bound this arm exists to pin is
        # unchanged and is asserted by `calls` above; only the sentence moved.
        self.assertIn("ARE recorded in this receipt's failure chunks",
                      row["base_check"]["reason"])
        self.assertIn("bounded to %d failures" % gate.FAILURE_CAP,
                      row["base_check"]["reason"])
        chunks = gate.eventledger.events(gate.receipts_path())[:-1]
        identities = [item for chunk in chunks for item in chunk["failures"]]
        self.assertEqual(len(identities), gate.FAILURE_CAP + 5)
        self.assertEqual(identities[-1]["test"],
                         "tests.test_many.Many.test_%d" % (gate.FAILURE_CAP + 4))

    def test_gate_show_chunk_id_names_its_parent_receipt(self):
        text = _failure_stream(gate.FAILURE_CAP + 1)
        tree = self._git("rev-parse", "HEAD^{tree}")
        row, minted, err = gate._mint_result(
            self.repo, self.head, tree, False, gate.interpreter(),
            [sys.executable, "-m", "unittest"], False, None, 1, 0, text)
        self.assertTrue(minted, err)
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            rc = gate._cmd_show([row["failure_chunks"][0]])
        self.assertEqual(rc, 1)
        shown = stderr.getvalue()
        self.assertIn("failure-identity CHUNK, not a gate receipt", shown)
        self.assertIn(row["id"], shown)
        self.assertIn("helm gate show %s" % row["id"], shown)
        self.assertNotIn("run `helm gate run`", shown)

    def test_at_most_twenty_failures_still_reach_real_base_check(self):
        text = _failure_stream(gate.FAILURE_CAP)
        tree = self._git("rev-parse", "HEAD^{tree}")
        ident = gate.interpreter()
        with mock.patch.object(gate.vcs, "backend") as backend:
            backend.return_value.trunk_ref.return_value = "main"
            backend.return_value.head_sha.return_value = None
            row, minted, err = gate._mint_result(
                self.repo, self.head, tree, False, ident,
                [ident["executable"]] + list(gateauthority.SERIAL_ARGV),
                True, None, 1, 0, text)
        self.assertTrue(minted, err)
        self.assertIsNone(err)
        self.assertEqual(row["base_check"]["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("cannot resolve trunk", row["base_check"]["reason"])
        self.assertNotIn("beyond the", row["base_check"]["reason"])

    def test_oversized_single_identity_refuses_after_chunk_rotation(self):  # noqa: VACUOUS_ASSERTION — a normal identity first proves chunking is live before the post-rotation oversized identity must raise
        ordinary = {"kind": "ERROR", "test": "tests.Case.test_ok"}
        self.assertEqual(len(gate._failure_identity_chunks([ordinary])), 1)
        oversized = {
            "kind": "ERROR",
            "test": "x" * gate.eventledger.MAX_EVENT_BYTES,
        }
        with self.assertRaisesRegex(
                ValueError, "one failure identity exceeds the ledger event limit"):
            gate._failure_identity_chunks([ordinary, oversized])

    def test_missing_or_malformed_chunk_never_admits_the_gate_row(self):
        text = _failure_stream(gate.FAILURE_CAP + 1)
        tree = self._git("rev-parse", "HEAD^{tree}")
        ident = gate.interpreter()
        with mock.patch.object(gate, "_base_check", return_value={
                "verdict": gate.NOT_STALE, "reason": "measured"}):
            row, minted, err = gate._mint_result(
                self.repo, self.head, tree, False, ident,
                [ident["executable"]] + list(gateauthority.SERIAL_ARGV),
                True, None, 1, 0, text)
        self.assertTrue(minted, err)
        with open(gate.receipts_path(), encoding="utf-8") as fh:
            events = [json.loads(line) for line in fh]
        chunk, receipt = events[0], events[-1]
        cases = {
            "missing": [receipt],
            "malformed": [dict(chunk, failures=[{"kind": "ERROR"}]), receipt],
        }
        for name, stored in cases.items():
            with self.subTest(name=name):
                with open(gate.receipts_path(), "w", encoding="utf-8") as fh:
                    for event in stored:
                        fh.write(json.dumps(event) + "\n")
                rows, unavailable, skipped = gate.receipts()
                self.assertIsNone(unavailable)
                self.assertEqual(rows, [])
                self.assertGreaterEqual(skipped, 1)
                got, why = gate.by_id(row["id"])
                self.assertIsNone(got)
                self.assertIn("IS in the ledger", why)
                self.assertIn("INCOMPLETE", why)
                self.assertIn("UNJUDGEABLE", why)
                self.assertNotIn("no minted gate receipt", why)
                self.assertNotIn("run `helm gate run`", why)

    def test_twenty_failures_keep_the_single_event_v4_shape(self):
        text = _failure_stream(gate.FAILURE_CAP)
        tree = self._git("rev-parse", "HEAD^{tree}")
        row, minted, err = gate._mint_result(
            self.repo, self.head, tree, False, gate.interpreter(),
            [sys.executable, "-c", "probe"], False, None, 1, 0, text)
        self.assertTrue(minted, err)
        self.assertIsNone(err)
        self.assertEqual(row["v"], 4)
        self.assertEqual(len(row["failures"]), gate.FAILURE_CAP)
        self.assertNotIn("failure_chunks", row)
        with open(gate.receipts_path(), encoding="utf-8") as fh:
            self.assertEqual(len(fh.readlines()), 1,
                             "must-not control: <=20 stays one event")

    def test_plausible_full_suite_chunk_refs_keep_the_main_record_bounded(self):
        failures = [{"kind": "ERROR", "test": "t" * 490 + str(i),
                     "traceback": "x" * gate._FAILURE_TEXT_CAP}
                    for i in range(12000)]
        record, chunks = gate._failure_record(failures)
        failing_files = ["tests/" + "f" * 480 + str(i) + ".py"
                         for i in range(12000)]
        shown = failing_files[:gate.FAILURE_CAP]
        shell = dict(record, v=8, event="gate", id="0" * 16,
                     base_check={"verdict": gate.NOT_STALE,
                                 "reason": "r" * gate._FAILURE_TEXT_CAP,
                                 "failing_files": shown,
                                 "failing_files_total": len(failing_files),
                                 "failing_files_omitted": len(failing_files) - len(shown)})
        self.assertGreater(len(json.dumps(failing_files).encode("utf-8")),
                           gate.eventledger.MAX_EVENT_BYTES,
                           "control: an uncapped realistic projection breaches")
        self.assertLessEqual(gate._event_bytes(shell),
                             gate.eventledger.MAX_EVENT_BYTES)
        self.assertTrue(all(gate._event_bytes(chunk)
                            <= gate.eventledger.MAX_EVENT_BYTES for chunk in chunks))

    def test_a_dirty_worktree_is_recorded_not_warned_about(self):
        self._dirty()
        self.assertTrue(self.mint("Ran 1 test in 0.1s", "", "OK")["dirty"])

    def test_the_receipt_is_on_disk_before_run_returns(self):
        row = self.mint("Ran 1 test in 0.1s", "", "OK")
        found, unavailable, _skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertIn(row["id"], [r["id"] for r in found])

    def test_default_run_records_the_running_interpreter(self):
        """THE CORE GUARANTEE. helm spawns the suite as a child of ITSELF, so
        the recorded identity cannot drift from the one that ran. Asserted
        against sys.implementation directly: a receipt that merely CONTAINS an
        interpreter-shaped string would pass a weaker check while naming the
        wrong python."""
        with serial_process():
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertTrue(row["suite"])
        self.assertEqual(row["interpreter"]["name"], sys.implementation.name)
        self.assertEqual(row["interpreter"]["language"],
                         "%d.%d.%d" % sys.version_info[:3])
        self.assertEqual(row["interpreter"]["executable"],
                         os.path.realpath(sys.executable))
        self.assertEqual(row["argv"][0], row["interpreter"]["executable"])
        self.assertIn(gate.interpreter_label(row["interpreter"]),
                      gate.evidence_line(row))

    def test_real_runTest_failure_records_its_identity(self):
        code = """import unittest

class ReceiptRunTest(unittest.TestCase):
    def runTest(self):
        self.fail("boom")

result = unittest.TextTestRunner().run(ReceiptRunTest())
raise SystemExit(not result.wasSuccessful())
"""
        with mock.patch.object(gate, "SUITE", ("-c", code)):
            require_supervisor()
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        self.assertFalse(row["failures_unreadable"])
        self.assertEqual(row["failures"][0]["test"],
                         "__main__.ReceiptRunTest.runTest")

    def test_real_local_TestCase_method_records_its_qualname(self):
        code = """import unittest

def make():
    class ReceiptLocal(unittest.TestCase):
        def test_local(self):
            self.fail("boom")
    return ReceiptLocal

suite = unittest.defaultTestLoader.loadTestsFromTestCase(make())
result = unittest.TextTestRunner().run(suite)
raise SystemExit(not result.wasSuccessful())
"""
        with mock.patch.object(gate, "SUITE", ("-c", code)):
            require_supervisor()
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        self.assertFalse(row["failures_unreadable"])
        self.assertEqual(row["failures"][0]["test"],
                         "__main__.make.<locals>.ReceiptLocal.test_local")

    def test_real_doctest_failure_is_explicitly_unreadable(self):
        code = """import doctest
import types
import unittest

probe = types.ModuleType("probe")
probe.__doc__ = ">>> 1 + 1\\n3\\n"
result = unittest.TextTestRunner().run(doctest.DocTestSuite(probe))
raise SystemExit(not result.wasSuccessful())
"""
        with mock.patch.object(gate, "SUITE", ("-c", code)):
            require_supervisor()
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        self.assertTrue(row["failures_unreadable"])
        self.assertEqual(row["failures"], [])

    def test_real_docfile_failure_is_explicitly_unreadable(self):
        path = os.path.join(self.repo, "sample.txt")
        with open(path, "w") as fh:
            fh.write(">>> 1 + 1\n3\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "doctest fixture")
        self.head = self._git("rev-parse", "HEAD")
        code = ("import doctest,unittest; "
                "r=unittest.TextTestRunner().run("
                "doctest.DocFileSuite(%r,module_relative=False)); "
                "raise SystemExit(not r.wasSuccessful())" % path)
        with mock.patch.object(gate, "SUITE", ("-c", code)):
            require_supervisor()
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        self.assertTrue(row["failures_unreadable"])
        self.assertEqual(row["failures"], [])

    def test_real_class_fixture_error_records_a_canonical_identity(self):
        self.fixture_suite("""import unittest

class ReceiptFixtureError(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raise RuntimeError("fixture boom")

    def test_never_runs(self):
        self.fail("unreachable")
""")
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        self.assertFalse(row["failures_unreadable"])
        self.assertEqual(row["failures"][0]["test"],
                         "tests.test_gate_receipt_fixture."
                         "ReceiptFixtureError.setUpClass")

    def test_real_import_error_records_the_loader_identity(self):
        self.fixture_suite("raise RuntimeError('import boom')\n")
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        self.assertFalse(row["failures_unreadable"])
        self.assertEqual(row["failures"][0]["test"],
                         "unittest.loader._FailedTest."
                         "tests.test_gate_receipt_fixture")

    def test_a_real_failing_suite_records_and_shows_the_test_identity(self):
        path = self.fixture_suite("""import unittest

class ReceiptFailure(unittest.TestCase):
    def test_known_failure(self):
        '''receipt should name this test despite its description'''
        self.assertEqual(1, 2)
""")
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        self.assertFalse(row["failures_unreadable"])
        self.assertEqual(len(row["failures"]), 1)
        failure = row["failures"][0]
        self.assertEqual(failure["test"],
                         "tests.test_gate_receipt_fixture.ReceiptFailure."
                         "test_known_failure")
        self.assertIn(os.path.basename(path), failure["traceback"])
        evidence = gate.evidence_line(row)
        self.assertNotIn("test_known_failure", evidence,
                         "the one-line evidence protocol stays counts-only")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(gate.cmd_gate(["show", row["id"]]), 0)
        shown = stdout.getvalue()
        self.assertIn(failure["test"], shown)
        self.assertIn(failure["traceback"], shown)

    def test_a_real_passing_suite_records_a_confirmed_empty_list(self):
        self.fixture_suite("""import unittest

class ReceiptPass(unittest.TestCase):
    def test_known_pass(self):
        self.assertTrue(True)
""")
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "OK")
        self.assertEqual(row["failures"], [])
        self.assertFalse(row["failures_unreadable"])

    def test_post_summary_atexit_output_cannot_forge_a_verified_count(self):
        self.fixture_suite("""import atexit
import sys
import unittest

atexit.register(lambda: print("Ran 999 tests in 9.999s", file=sys.stderr))

class ReceiptPass(unittest.TestCase):
    def test_one_real_pass(self):
        self.assertTrue(True)
""")
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["rc"], 0)
        self.assertEqual(row["status"], "UNKNOWN")
        self.assertIsNone(row["ran"])
        state, _rid, why = gate.bind(gate.evidence_line(row), self.head)
        self.assertEqual(state, "REFUSED", why)

    def test_a_mangled_failure_stream_records_unreadable_not_empty(self):
        with mock.patch.object(gate, "SUITE", tuple(_emit_exit(
                1, "ERROR: this is not a unittest identity",
                "Ran 1 test in 0.0s", "", "FAILED (errors=1)"))):
            require_supervisor()
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        self.assertEqual(row["failures"], [])
        self.assertTrue(row["failures_unreadable"])
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(gate.cmd_gate(["show", row["id"]]), 0)
        self.assertIn("failures   UNREADABLE", stdout.getvalue())

    def test_stdout_cannot_forge_unittest_failure_protocol(self):
        self.fixture_suite("""import unittest

class StdoutNoise(unittest.TestCase):
    def test_passes_while_logging(self):
        for i in range(25):
            print("=" * 70)
            print("ERROR: fake (evil.Case.test_%d)" % i)
            print("-" * 70)
            print("Traceback (most recent call last):")
            print("  File \\"/tmp/fake.py\\", line 1, in fake")
        print("Ran 25 tests in 0.1s")
        print("FAILED (errors=25)")
        self.assertTrue(True)
""")
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "OK")
        self.assertEqual(row["failures"], [])
        self.assertFalse(row["failures_unreadable"])

    def test_FAILED_summary_with_zero_exit_is_UNKNOWN(self):
        with mock.patch.object(gate, "SUITE", tuple(_emit(
                "Ran 1 test in 0.0s", "", "FAILED (failures=1)"))):
            require_supervisor()
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["rc"], 0)
        self.assertEqual(row["status"], "UNKNOWN")
        self.assertIn("sources disagree", row["detail"])
        self.assertTrue(row["failures_unreadable"])

    def test_FAILED_summary_before_timeout_is_UNKNOWN(self):
        code = ("import sys,time; "
                "print('Ran 1 test in 0.0s\\n\\nFAILED (errors=1)', "
                "file=sys.stderr, flush=True); time.sleep(30)")
        with mock.patch.object(gate, "SUITE", ("-c", code)):
            require_supervisor()
            row, err = gate.run(repo=self.repo, timeout=0.2)
        self.assertIsNone(err, err)
        self.assertIsNone(row["rc"])
        self.assertEqual(row["status"], "UNKNOWN")
        self.assertIn("timeout", row["detail"])
        self.assertTrue(row["failures_unreadable"])

    def test_inherited_color_flags_cannot_change_the_receipt_grammar(self):
        self.fixture_suite("""import unittest

class ColoredFailure(unittest.TestCase):
    def test_colored_failure(self):
        self.fail("red")
""")
        with mock.patch.dict(os.environ, {"PYTHON_COLORS": "1",
                                          "FORCE_COLOR": "1"}):
            require_supervisor()
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        self.assertFalse(row["failures_unreadable"])
        self.assertEqual(row["failures"][0]["test"],
                         "tests.test_gate_receipt_fixture.ColoredFailure."
                         "test_colored_failure")

    def test_child_emitted_ansi_is_normalized_after_environment_suppression(self):
        lines = ["=" * 70,
                 _floor("ERROR: test_red (tests.test_probe.Probe.test_red)"),
                 "-" * 70,
                 "Traceback (most recent call last):",
                 "  File \"/tmp/test_probe.py\", line 7, in test_red",
                 "    raise ValueError('red')",
                 "ValueError: red", "", "-" * 70,
                 "Ran 1 test in 0.1s", "", "FAILED (errors=1)"]
        protocol = "".join("\x1b[31m%s\x1b[0m\n" % line
                           for line in lines)
        code = ("import os,sys; os.environ['PYTHON_COLORS']='1'; "
                "sys.stderr.write(%r); raise SystemExit(1)" % protocol)
        with mock.patch.object(gate, "SUITE", ("-c", code)):
            require_supervisor()
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        self.assertFalse(row["failures_unreadable"])
        self.assertEqual(row["failures"][0]["test"],
                         "tests.test_probe.Probe.test_red")

    def test_interpreter_is_read_from_the_runtime_never_baked_in(self):
        """THE ASSERTION ABOVE IS NOT ENOUGH, and the mutation pass proved it:
        replacing `impl.name` with the literal "cpython" left every test GREEN,
        because on this box sys.implementation.name IS "cpython" and an
        expected value that equals the machine's own constant cannot see a
        constant in the code. The property that actually matters is that the
        identity is READ from the runtime — so move the runtime and require the
        receipt to follow it."""
        fake = types.SimpleNamespace(name="graalpy", version=(24, 1, 0))
        with mock.patch.object(sys, "implementation", fake), \
                mock.patch.object(sys, "version_info", (3, 12, 8, "final", 0)), \
                mock.patch.object(sys, "executable", os.path.join(self.tmp, "gp")):
            ident = gate.interpreter()
        self.assertEqual(ident["name"], "graalpy")
        self.assertEqual(ident["version"], "24.1.0")
        self.assertEqual(ident["language"], "3.12.8")
        self.assertEqual(ident["executable"], os.path.join(self.tmp, "gp"))
        self.assertEqual(gate.interpreter_label(ident), "graalpy-3.12.8")
        # and the one rename the label makes, so the two pythons this module
        # exists for read as `graalpy-3.12.8` and `CPython-3.14.4`
        self.assertEqual(gate.interpreter_label(
            {"name": "cpython", "language": "3.14.4"}), "CPython-3.14.4")

    def test_a_timeout_is_unknown_and_still_minted(self):
        require_supervisor()
        row, err = gate.run(repo=self.repo, timeout=0.4,
                            argv=[sys.executable, "-c",
                                  "import time; time.sleep(30)"])
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "UNKNOWN")
        self.assertIsNone(row["rc"])

    def test_ambiguous_prefix_refuses_rather_than_picking_one(self):
        """Two real receipts, forced to share a prefix. The earlier version of
        this branched on whether two content hashes happened to collide in
        their first four characters — which is essentially never, so the
        assertion it guarded had never once run."""
        rows = [self.mint("Ran %d tests in 0.1s" % n, "", "OK") for n in (1, 2)]
        pair = [dict(rows[0], id="abcd" + "1" * 12),
                dict(rows[1], id="abcd" + "2" * 12)]
        with mock.patch.object(gate, "receipts", return_value=(pair, None, 0)):
            row, err = gate.by_id("abcd")
            self.assertIsNone(row)
            self.assertIn("matches 2 receipts", err)
            # and the CONTROL: a prefix that names exactly one still resolves
            got, err = gate.by_id("abcd1")
            self.assertIsNone(err, err)
            self.assertEqual(got["id"], pair[0]["id"])


class CodexRound1(GateBase):
    """The three blockers a reviewer reproduced against tip 1ac749c0. Each is a way
    to reach VERIFIED without having run a green suite on the named tree."""

    def test_a_plausible_footer_and_a_nonzero_exit_is_UNKNOWN(self):
        """The runner's SUMMARY and the runner's EXIT CODE are two independent
        signals, and a receipt that reads only the first believes text over the
        process that produced it. The repro: a child printing `OK` then
        exiting 9 minted status=OK, rc=9, bind=VERIFIED."""
        require_supervisor()
        row, err = gate.run(repo=self.repo, argv=[
            sys.executable, "-c",
            "print('Ran 3 tests in 0.1s'); print(); print('OK'); "
            "raise SystemExit(9)"])
        self.assertIsNone(err, err)
        self.assertEqual(row["rc"], 9)
        self.assertEqual(row["status"], "UNKNOWN")
        self.assertIn("two sources disagree", row["detail"])
        state, _rid, why = gate.bind(gate.evidence_line(row), self.head)
        self.assertEqual(state, "REFUSED", why)

    def test_a_run_that_DIRTIES_the_tree_cannot_bind(self):
        """One tree read cannot witness a change made during the thing it
        describes. The repro: a child modified a tracked file and printed
        OK; the receipt recorded dirty=False from BEFORE the child and bound."""
        require_supervisor()
        row, err = gate.run(repo=self.repo, argv=[
            sys.executable, "-c",
            "open(%r, 'a').write('mutated\\n'); print('Ran 1 test in 0.1s'); "
            "print(); print('OK')" % os.path.join(self.repo, "a.txt")])
        self.assertIsNone(err, err)
        self.assertFalse(row["dirty"])          # before: genuinely clean
        self.assertTrue(row["dirty_after"])     # after: the run made it dirty
        state, _rid, why = gate.bind(gate.evidence_line(row), self.head)
        self.assertEqual(state, "REFUSED", why)
        self.assertIn("DIRTY", why)

    def test_a_run_that_MOVES_HEAD_cannot_bind(self):
        """The other half of the bracket: a clean tree at both ends is not the
        SAME tree at both ends."""
        require_supervisor()
        row, err = gate.run(repo=self.repo, argv=[
            sys.executable, "-c",
            "import subprocess as s; "
            "open(%r,'a').write('x\\n'); "
            "s.run(['git','add','-A'],cwd=%r); "
            "s.run(['git','commit','-qm','moved'],cwd=%r); "
            "print('Ran 1 test in 0.1s'); print(); print('OK')"
            % (os.path.join(self.repo, "a.txt"), self.repo, self.repo)])
        self.assertIsNone(err, err)
        self.assertNotEqual(row["head"], row["head_after"])
        self.assertFalse(row["dirty_after"])    # clean, but a DIFFERENT tree
        state, _rid, why = gate.bind(gate.evidence_line(row), self.head)
        self.assertEqual(state, "REFUSED", why)
        self.assertIn("MOVED", why)

    def test_a_HAND_WRITTEN_receipt_line_does_not_resolve(self):
        """The addendum: `_receipt_id` is a content hash that nothing ever
        recomputed, so one appended JSON line with an invented id, the target
        head, dirty=false, status=OK and a made-up interpreter bound VERIFIED
        without any command having run."""
        self.mint("Ran 1 test in 0.1s", "", "OK")          # create the ledger
        forged = {"v": 1, "event": "gate", "ts": "2026-07-31T00:00:00Z",
                  "id": "deadbeefcafebabe", "repo_id": self.repo,
                  "head": self.head, "tree": "f" * 40, "dirty": False,
                  "head_after": self.head, "tree_after": "f" * 40,
                  "dirty_after": False, "rc": 0, "suite": True,
                  "interpreter": {"name": "cpython", "version": "3.14.4",
                                  "language": "3.14.4", "executable": "/x"},
                  "argv": ["/x", "-m", "unittest"], "status": "OK",
                  "ran": 5115, "skipped": 8, "detail": "skipped=8"}
        with open(gate.receipts_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(forged) + "\n")
        self.assertNotEqual(forged["id"], gate._receipt_id(forged))
        row, err = gate.by_id("deadbeefcafebabe")
        self.assertIsNone(row)
        # The REFUSAL is unchanged; only the SENTENCE is honest now. This used
        # to read "no minted gate receipt deadbeefcafebabe — run `helm gate
        # run`", which is false about a row sitting in the ledger and sends the
        # reader to re-run a suite that was never the problem.
        self.assertIn("IS in the ledger", err)
        self.assertIn("does NOT recompute", err)
        self.assertIn(gate._receipt_id(forged), err)   # what helm computed
        self.assertNotIn("no minted gate receipt", err)
        state, _rid, why = gate.bind("gate:deadbeefcafebabe ok", self.head)
        self.assertEqual(state, "REFUSED", why)

    def test_a_TRULY_ABSENT_id_still_says_absent_and_advises_the_gate(self):
        """The control for the arm above: when nothing in the ledger carries
        the token, the old sentence is the RIGHT one and must survive. A change
        that made every miss say "IS in the ledger" would be worse than the
        conflation it replaced."""
        self.mint("Ran 1 test in 0.1s", "", "OK")
        row, err = gate.by_id("a" * 16)
        self.assertIsNone(row)
        self.assertIn("no minted gate receipt", err)
        # task/3039: the advice names each room's gate, because a lane room
        # refuses a bare whole suite.
        self.assertIn("`helm gate run --focus`", err)
        self.assertIn("land gate", err)
        self.assertNotIn("run `helm gate run`", err)
        self.assertNotIn("IS in the ledger", err)

    def test_self_consistent_UNKNOWN_version_receipt_never_binds(self):
        """WAS `..._reserved_v5_...`, and the rename is the finding.

        This arm was written on a trunk where 5 was RESERVED, and it pinned
        that reservation by asserting a self-consistent v5 row is dropped. 5
        shipped: it is the cached-receipt kind, the live ledger carries v5
        rows, and a reader that drops them is the defect a review filed
        against this lane. Keeping the arm as written would have re-pinned the
        bug it exists beside.

        The GUARANTEE it was built for is untouched — a row this helm cannot
        read must not bind however self-consistent it is — so it moves to a
        version that is genuinely unknown. FAR-FUTURE, NOT THE NEXT ONE: its
        sibling in tests/test_gate_import.py records that this arm named v4,
        then v5, and each time that version SHIPPED the arm silently stopped
        testing anything."""
        row = self.mint("Ran 1 test in 0.1s", "", "OK")
        unknown = dict(row, v=99)
        self.assertFalse(gate.receipt_version_known(unknown))   # MUST-MISS
        unknown["id"] = gate._receipt_id(unknown)
        with open(gate.receipts_path(), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(unknown) + "\n")
        rows, unavailable, skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertEqual(rows, [])
        self.assertEqual(skipped, 1)
        got, err = gate.by_id(unknown["id"])
        self.assertIsNone(got)
        self.assertIn("NEWER helm", err)
        self.assertNotIn("CHANGED after", err)
        # AND THE DROP REASON IS NAMED HONESTLY. This row's id recomputes
        # perfectly — it is refused for its VERSION — so the sentence must not
        # accuse the ledger of an edit and then print the same id twice.
        self.assertIn("recomputes correctly", err)
        self.assertNotIn("does NOT recompute", err)
        state, _rid, why = gate.bind(gate.evidence_line(unknown), self.head)
        self.assertEqual(state, "REFUSED", why)

    def test_a_self_consistent_v5_cached_receipt_IS_read(self):
        """The positive half, and the reviewer's own observable made runnable.

        A v5 row is the cached-receipt kind and is perfectly readable here.
        Under this lane's pre-reconciliation reader it was dropped along with
        v6 — 10 of the live ledger's rows — which is the whole finding. This
        arm is the one that goes red if a future narrowing repeats it."""
        row = self.mint("Ran 1 test in 0.1s", "", "OK")
        cached = dict(row, v=5, executed=True)
        cached["id"] = gate._receipt_id(cached)
        with open(gate.receipts_path(), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(cached) + "\n")
        rows, unavailable, skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertEqual(skipped, 0)
        self.assertEqual([r["id"] for r in rows], [cached["id"]])
        got, err = gate.by_id(cached["id"])
        self.assertIsNone(err, err)
        self.assertEqual(got["id"], cached["id"])

    def test_a_self_consistent_withdrawn_v7_receipt_is_named_not_read(self):
        """And the third state: readable-looking, but withdrawn on purpose.
        7 must never be admitted by the reader no matter how consistent it is,
        and the refusal must say WITHDRAWN rather than send the reader off to
        update helm."""
        row = self.mint("Ran 1 test in 0.1s", "", "OK")
        withdrawn = dict(row, v=gate.WITHDRAWN_SHARD_VERSION)
        self.assertFalse(gate.receipt_version_known(withdrawn))
        withdrawn["id"] = gate._receipt_id(withdrawn)
        with open(gate.receipts_path(), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(withdrawn) + "\n")
        rows, _unavailable, skipped = gate.receipts()
        self.assertEqual(rows, [])
        self.assertEqual(skipped, 1)
        got, err = gate.by_id(withdrawn["id"])
        self.assertIsNone(got)
        self.assertIn("WITHDRAWN", err)
        self.assertNotIn("NEWER helm", err)
        self.assertIn("recomputes correctly", err)
        self.assertNotIn("does NOT recompute", err)

    def test_a_receipt_from_a_NEWER_helm_says_update_not_re_run(self):
        """The live case, and the reason the sentence matters more than the
        verdict. A receipt minted by a helm whose id grammar this one does not
        know recomputes differently and is dropped — indistinguishable, before
        this change, from a receipt that does not exist. The advice mattered:
        "run `helm gate run`" mints ANOTHER receipt this helm cannot read, so
        the reader loops. Measured on 756b936006bf0e47, whose reviewer traced
        three functions to find out why an APPROVE could not bind."""
        self.mint("Ran 1 test in 0.1s", "", "OK")   # a real, resolvable row
        future = {"v": 99, "event": "gate", "ts": "2026-08-04T00:00:00Z",
                  "id": "b" * 16, "repo_id": self.repo,
                  "head": self.head, "tree": "f" * 40, "dirty": False,
                  "head_after": self.head, "tree_after": "f" * 40,
                  "dirty_after": False, "rc": 0, "suite": True,
                  "interpreter": {"name": "cpython", "version": "3.14.4",
                                  "language": "3.14.4", "executable": "/x"},
                  "argv": ["/x", "-m", "unittest"], "status": "OK",
                  "ran": 5115, "skipped": 8, "detail": "skipped=8"}
        with open(gate.receipts_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(future) + "\n")
        row, err = gate.by_id("b" * 16)
        self.assertIsNone(row)
        self.assertIn("IS in the ledger", err)
        self.assertIn("NEWER helm", err)
        self.assertIn("99", err)                   # names the version it saw
        self.assertIn("WRONG move", err)           # do not just re-run
        # POSITIVE CONTROL on the same ledger, unconditional: the honest row
        # minted above still resolves, so the ledger is readable and the
        # refusal above is about THIS row rather than a broken lookup.
        rows, unavailable, skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertEqual(skipped, 1)
        self.assertEqual(len(rows), 1)

    def test_ONE_malformed_row_does_not_wedge_the_whole_ledger(self):
        """eventledger strict mode accepts any JSON object with
        a non-empty id, and the content-id recompute then reached into it — so
        a single line carrying `"interpreter": "not-an-object"` made receipts()
        and every bind() raise AttributeError. One bad row hid every honest
        receipt behind it, which is worse than the forgery it was added to
        stop."""
        with serial_process(ran=4):
            good, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        with open(gate.receipts_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"v": 4, "id": "deadbeef",
                                 "interpreter": "not-an-object"}) + "\n")
            # A SECOND, DIFFERENT breakage on purpose. `_ident_of` cannot
            # save this one — the argv list holds non-strings, so the join
            # inside _receipt_id raises TypeError — which is what binds the
            # BROAD except rather than a per-shape one. With only the
            # interpreter row here, the coercion and the guard each covered
            # for the other and the mutation pass showed BOTH surviving.
            fh.write(json.dumps({"v": 4, "id": "beefdead", "argv": [1, 2],
                                 "interpreter": {"name": "x"}}) + "\n")
        rows, unavailable, skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertEqual(skipped, 2)
        self.assertEqual([r["id"] for r in rows], [good["id"]])
        # the honest receipt still resolves and still binds THROUGH the mess
        state, rid, why = gate.bind(gate.evidence_line(good), self.head)
        self.assertEqual(rid, good["id"])
        self.assertEqual(state, "VERIFIED", why)
        # and the malformed ids resolve to nothing rather than raising
        # and the malformed ids resolve to nothing rather than raising —
        # PRESENT AND UNJUDGEABLE, which is a different answer from absent.
        # These two are dropped for DIFFERENT reasons and get different
        # sentences: deadbeef's id disagrees, beefdead cannot be computed at
        # all (a non-string in argv makes the join raise). A resolver that
        # only handled the first would still call the second missing.
        row, err = gate.by_id("deadbeef")
        self.assertIsNone(row)
        self.assertIn("IS in the ledger", err)
        self.assertIn("does NOT recompute", err)
        row, err = gate.by_id("beefdead")
        self.assertIsNone(row)
        self.assertIn("cannot compute an id for it at all", err)
        self.assertIn("unjudgeable", err)
        for token in ("deadbeef", "beefdead"):
            _row, err = gate.by_id(token)
            self.assertNotIn("no minted gate receipt", err)

    def test_malformed_timing_id_is_not_misreported_as_a_receipt(self):
        good = self.mint("Ran 1 test in 0.1s", "", "OK")
        timing = {"v": 1, "event": gate._TIMING_EVENT,
                  "id": "d" * gate._TIMING_ID_LEN,
                  "receipt": good["id"], "state": "COMPLETE"}
        with open(gate.receipts_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(timing) + "\n")
        rows, unavailable, skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertEqual(skipped, 1,
                         "positive control: malformed timing was observed")
        self.assertEqual([row["id"] for row in rows], [good["id"]])
        row, err = gate.by_id(timing["id"])
        self.assertIsNone(row)
        self.assertIn("no minted gate receipt", err)
        self.assertNotIn("IS in the ledger", err)
        resolved, err = gate.by_id(good["id"])
        self.assertIsNone(err, err)
        self.assertEqual(resolved["id"], good["id"],
                         "honest receipt lookup survives malformed timing")

    def test_a_row_from_a_NEWER_helm_is_not_reported_as_corruption(self):
        """The cost is the reason this arm exists: four
        rows written by an unlanded lane running a newer helm were counted
        into "did not match their own content", and a seat spent THREE PROBES
        hunting ledger damage that did not exist. The rows really are unusable
        by this reader and really must be skipped — what was wrong was the
        REASON GIVEN.

        THE MIXED LEDGER IS THE WHOLE POINT. With only a future row present,
        any implementation that relabels every skip passes. So this puts a
        genuinely corrupt row NEXT TO a merely-newer one and requires the
        surface to tell them apart and to count each correctly."""
        with serial_process(ran=4):
            good, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        future = json.loads(json.dumps(good))
        future["v"] = 99                       # a version this helm cannot know
        future["id"] = "f" * 16
        corrupt = json.loads(json.dumps(good))  # KNOWN version, tampered id
        corrupt["id"] = "c" * 16
        with open(gate.receipts_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(future) + "\n")
            fh.write(json.dumps(corrupt) + "\n")

        # BOTH are dropped — that part was never in dispute.
        rows, unavailable, skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertEqual(skipped, 2)
        self.assertEqual([r["id"] for r in rows], [good["id"]])

        # ONLY the future one is a VERSION problem. If this said {99: 1, 4: 1}
        # the surface would be excusing real corruption, which is the mirror
        # failure and strictly worse than the one being fixed.
        self.assertEqual(gate.unreadable_versions(), {99: 1})

        # AND THE PREDICATE THAT EXPLAINS MUST BE THE PREDICATE THAT FILTERED.
        # Valid sibling events are consumed separately, not dropped receipts;
        # apply receipt identity only to the rows that reach that predicate.
        raw = [json.loads(line) for line
               in open(gate.receipts_path(), encoding="utf-8") if line.strip()]
        self.assertTrue(any(r.get("event") == gate._TIMING_EVENT for r in raw),
                        "positive control: the ledger contains a timing sibling")
        siblings = (gate._FAILURE_CHUNK_EVENT, gate._TIMING_EVENT)
        self.assertEqual(sum(1 for r in raw
                             if r.get("event") not in siblings
                             and not gate._id_matches(r)), skipped)

        err_text = io.StringIO()
        with contextlib.redirect_stderr(err_text):
            gate.cmd_gate(["list", "--limit", "1"])
        said = err_text.getvalue()
        self.assertIn("cannot read", said)
        self.assertIn("v99", said)
        self.assertIn("NOT corruption", said)
        # and the corrupt row STILL gets the old, correct accusation — with a
        # count of ONE, not two. The bug was conflation, not the message.
        self.assertIn("1 ledger row did not match its own content", said)

        # POLE: a ledger with NOTHING unreadable says nothing about versions.
        # Without this, "always print the version line" would pass every
        # assertion above.
        self._rewrite_receipts([good])
        self.assertEqual(gate.unreadable_versions(), {})
        err_text = io.StringIO()
        with contextlib.redirect_stderr(err_text):
            gate.cmd_gate(["list", "--limit", "1"])
        self.assertNotIn("cannot read", err_text.getvalue())
        self.assertNotIn("did not match", err_text.getvalue())

    def _rewrite_receipts(self, rows):
        with open(gate.receipts_path(), "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def test_ident_coercion_is_TYPE_checked_not_truthiness_checked(self):
        """Binds the coercion on its OWN, not through the loop's guard. The
        mutation pass caught this: reverting `_ident_of` to `or {}` left every
        test green, because the broad except in receipts() swallowed the
        AttributeError and skipped the row for a different reason than the one
        under test. Two guards covering for each other means neither is
        measured."""
        for junk in ("not-an-object", ["nor", "this"], 7, None, ""):
            self.assertEqual(gate._ident_of({"interpreter": junk}), {}, junk)
        real = {"name": "cpython", "language": "3.14.4"}
        self.assertEqual(gate._ident_of({"interpreter": real}), real)
        self.assertEqual(gate._ident_of({}), {})

    def test_an_UNREADABLE_ledger_REPORTS_rather_than_raising(self):
        """Widening receipts() to three values and leaving the
        UNAVAILABLE early exit at two made both callers raise ValueError — the
        ledger said "I cannot be read" and helm crashed instead of saying so.

        THIRD TIME IN THIS LANE THE ERROR PATH WAS THE BROKEN ONE: the
        AttributeError refusal, the raising rejection, now the arity. Each was
        a guard whose success path every test exercised and whose failure path
        none did — so this one asserts the failure path directly."""
        with mock.patch.object(gate.eventledger, "checked_events",
                               return_value=([], "EIO")):
            rows, unavailable, skipped = gate.receipts()
            self.assertEqual((rows, unavailable, skipped), ([], "EIO", 0))
            row, err = gate.by_id("abcd")           # must not raise
            self.assertIsNone(row)
            self.assertIn("EIO", err)
            state, _rid, why = gate.bind("gate:abcd x", self.head)
            self.assertEqual(state, "REFUSED", why)
            self.assertEqual(gate.cmd_gate(["list"]), 1)

    def test_an_EDITED_receipt_stops_resolving(self):
        """The same guard from the other side, and the reason it is worth
        having even though no content hash can stop a determined forger: a row
        that no longer matches what was minted is rejected rather than read."""
        row = self.mint("Ran 1 test in 0.1s", "", "OK")
        path = gate.receipts_path()
        with open(path, encoding="utf-8") as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
        lines[-1]["status"] = "OK"
        lines[-1]["ran"] = 99999                      # a number nobody ran
        with open(path, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")
        got, err = gate.by_id(row["id"])
        self.assertIsNone(got, err)

    def test_failure_diagnostics_are_bound_into_the_receipt_identity(self):
        row = self.mint("Ran 1 test in 0.1s", "", "OK")
        path = gate.receipts_path()
        with open(path, encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        lines[-1]["failures"] = [{"kind": "ERROR", "test": "invented.test",
                                  "traceback": "invented frame"}]
        with open(path, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")
        got, err = gate.by_id(row["id"])
        self.assertIsNone(got, err)
        # v3 is a version this helm KNOWS, so the row was CHANGED after
        # minting — the message must say tamper, never "re-run the gate".
        self.assertIn("IS in the ledger", err)
        self.assertIn("CHANGED", err)
        self.assertNotIn("no minted gate receipt", err)

    def test_a_red_receipt_keeps_the_child_stderr_and_a_green_one_does_not(self):
        """task/1034: nineteen reds across two weeks produced ZERO diagnosis,
        because the child's stderr was captured, mined for a summary, and then
        dropped. Nothing downstream held a copy — the fab job log carries only
        the wrapper and the fetched artifact directory only the receipt — so a
        red named a failing test and destroyed the single record of why it
        failed. The tail is kept ONLY on a not-OK row: a green suite has
        nothing to explain and this ledger is append-only forever."""
        needle = "MARKER_the_reason_this_connection_died"
        require_supervisor()
        red, err = gate.run(repo=self.repo, argv=[sys.executable] + _emit_exit(
            1, needle, "Ran 1 test in 0.1s", "", "FAILED (errors=1)"))
        self.assertIsNone(err, err)
        self.assertEqual("FAILED", red["status"], red.get("detail"))
        # The EFFECT: the bytes are in the row, not merely un-complained-about.
        self.assertIn(needle, red.get("stderr_tail") or "")
        # And the id must NOT have moved. The tail sits outside the enumerated
        # grammar in _receipt_id on purpose, so every historical v4 row keeps
        # the id it was minted with. If anyone later binds this field, THIS arm
        # reddens — which is the version-bump conversation they should be made
        # to have rather than silently invalidating the ledger.
        self.assertEqual(red["id"], gate._receipt_id(red))
        green = self.mint("Ran 1 test in 0.1s", "", "OK")
        self.assertEqual("OK", green["status"])
        self.assertNotIn("stderr_tail", green)

    def test_the_kept_stderr_is_bounded_and_keeps_the_end_not_the_start(self):
        """A suite prints twelve thousand dots BEFORE it prints the thing that
        killed it, so the front of that buffer is the least useful part of it.
        Absent input answers None rather than an empty string, because an empty
        string in the receipt would read like a measurement that came back
        blank instead of a field that was never written."""
        tail, cap = "THE_REASON_IT_DIED", gate.STDERR_TAIL_CAP
        bare = {"v": 4, "id": "0" * 16, "status": "FAILED"}
        got, meta = gate._stderr_tail("H" * cap + tail, bare)
        self.assertLessEqual(len(got.encode("utf-8")), cap)
        self.assertTrue(got.endswith(tail), got[-40:])
        # the cap actually bit — the head was dropped, not merely appended to
        self.assertLess(got.count("H"), cap)
        # and the row SAYS it was cut, so a reader can tell a complete tail
        # from a window of a much larger one
        self.assertTrue(meta["truncated"])
        self.assertEqual(len(("H" * cap + tail).encode("utf-8")),
                         meta["total_bytes"])
        whole, whole_meta = gate._stderr_tail(tail, bare)
        self.assertEqual(tail, whole)
        self.assertFalse(whole_meta["truncated"])
        for empty in ("", "  \n  ", None):
            self.assertEqual((None, None), gate._stderr_tail(empty, bare))

    def test_a_short_COMPLETE_stderr_is_kept_whole_not_dropped(self):
        """THE FLOOR JUDGES A FRAGMENT, NEVER A WHOLE STREAM. An earlier loop
        bounded on `keep >= FLOOR`, so every COMPLETE stderr under 512 bytes
        was dropped — the most useful case there is, a short traceback that
        needed no cutting at all. Measured `boom\\n` answering
        (None, None) on a tip whose whole-suite gate was about to be trusted."""
        bare = {"v": 4, "id": "0" * 16, "status": "FAILED"}
        for short in ("boom\n", "x", "RemoteDisconnected\n" * 3):
            with self.subTest(short):
                self.assertLess(len(short.encode("utf-8")),
                                gate.STDERR_TAIL_FLOOR)   # the arm's premise
                kept, meta = gate._stderr_tail(short, bare)
                self.assertEqual(short, kept)
                self.assertFalse(meta["truncated"])
                self.assertEqual(len(short.encode("utf-8")),
                                 meta["total_bytes"])
        # THE MUST-MISS on the same observable: a TRUNCATED shred under the
        # floor is still refused, because the reader cannot learn anything
        # from 40 bytes of a much larger stream.
        tight = {"v": 4, "id": "0" * 16, "status": "FAILED",
                 "argv": ["x" * (gate.eventledger.MAX_EVENT_BYTES - 400)]}
        self.assertEqual((None, None),
                         gate._stderr_tail("y" * 8192, tight))

    def test_a_row_already_at_the_ceiling_keeps_no_tail_at_all(self):
        """THE MUST-MISS for this feature, and the invariant two drafts got
        backwards. eventledger.append_unlocked DROPS an oversized payload
        rather than trimming it, so a tail that does not fit does not cost the
        tail — it costs the whole red receipt. When the bare row is already at
        the ceiling the honest answer is NO TAIL."""
        fat = {"v": 4, "id": "0" * 16, "status": "FAILED",
               "argv": ["x" * (gate.eventledger.MAX_EVENT_BYTES - 64)]}
        self.assertFalse(gate._row_fits(dict(fat, stderr_tail="y" * 4096)))
        self.assertEqual((None, None),
                         gate._stderr_tail("THE_REASON_IT_DIED" * 64, fat))
        # positive control on the SAME observable: a lean row DOES keep one
        lean = {"v": 4, "id": "0" * 16, "status": "FAILED"}
        kept, _meta = gate._stderr_tail("THE_REASON_IT_DIED" * 64, lean)
        self.assertTrue(kept.endswith("THE_REASON_IT_DIED"))

    def test_json_escaping_not_byte_length_decides_what_fits(self):
        """The SECOND finding, and the reason this budget is weighed
        rather than computed: 16,384 CONTROL bytes are 16KB raw and JSON-escape
        to 98,430 — six characters each — so an estimate built from raw byte
        length plus fixed headroom was wrong by 82KB. A cap in characters was
        wrong for emoji; a cap in raw bytes was wrong for control codes; only
        serializing the actual candidate is right for both."""
        lean = {"v": 4, "id": "0" * 16, "status": "FAILED"}
        raw = "\x01" * gate.STDERR_TAIL_CAP
        self.assertEqual(gate.STDERR_TAIL_CAP, len(raw.encode("utf-8")))
        # the must-miss: the naive byte-length reading says this is affordable
        self.assertGreater(len(json.dumps(raw).encode("utf-8")),
                           gate.eventledger.MAX_EVENT_BYTES)
        kept, meta = gate._stderr_tail(raw, lean)
        self.assertIsNotNone(kept)
        self.assertTrue(gate._row_fits(
            dict(lean, stderr_tail=kept, stderr_tail_meta=meta)))

    def test_the_LINE_THE_LEDGER_ACTUALLY_HOLDS_is_under_the_ceiling(self):
        """THE FINAL-ROW-SIZE ARM, and it deliberately reads
        THE DESTINATION rather than the candidate.

        `_row_fits` weighs a row as `_mint_result` holds it at that instant. If
        anything downstream ever adds or changes a field, that weighing goes
        stale and the receipt is lost again — a failure caught twice. Reading
        the row helm RETURNED cannot see that: it is the
        same object the weighing looked at. So this measures THE BYTES ON DISK,
        which is the only artifact whose size the ledger's rule actually
        judges.

        The first arm offered — mutating the row after
        _row_fits and asserting append rejects it — was refused on the ground
        that it would
        prove only that append rejects an oversized row, a fact about append
        and not about production's ordering. This one carries no mutation: it
        asserts that the line production wrote is within the ceiling, so it
        stays true however _mint_result is later rearranged.
        """
        require_supervisor()
        red, err = gate.run(repo=self.repo, argv=[sys.executable, "-c"] + [
            "import sys;sys.stderr.write('E'*%d);"
            "sys.stderr.write('\\nRan 1 test in 0.1s\\n\\nFAILED (errors=1)\\n');"
            "raise SystemExit(1)" % (gate.STDERR_TAIL_CAP * 4)])
        self.assertIsNone(err, err)
        self.assertEqual("FAILED", red["status"], red.get("detail"))
        self.assertIn("stderr_tail", red)      # the arm's own premise
        with open(gate.receipts_path(), "rb") as handle:
            lines = [line for line in handle.read().split(b"\n") if line.strip()]
        stored = json.loads(lines[-1])
        self.assertEqual(red["id"], stored["id"])   # the line IS this receipt
        # THE DESTINATION, not the candidate: what the ledger holds must obey
        # the rule the ledger enforces, including the newline it appends.
        self.assertLessEqual(len(lines[-1]) + 1,
                             gate.eventledger.MAX_EVENT_BYTES)
        # and the tail SURVIVED the trip rather than being dropped to fit
        self.assertTrue(stored["stderr_tail"].endswith("FAILED (errors=1)\n"))
        self.assertTrue(stored["stderr_tail_meta"]["truncated"])

    def test_a_FAT_row_reaches_the_ceiling_and_the_receipt_still_lands(self):
        """THE ARM THAT ACTUALLY EXERCISES THE WEIGHING, added because the
        mutation run proved the sibling above does NOT.

        STDERR_TAIL_CAP is 16KB and MAX_EVENT_BYTES is 64KB, so once the cap
        counts BYTES a tail can never push a NORMAL row over the ceiling —
        16KB of anything is still 16KB. That makes `_row_fits` a SECOND line of
        defence behind the cap, and it only bites when the BARE ROW is already
        near the limit. Every other arm on this feature feeds normal-sized
        rows, so defeating the weighing changes nothing they can see: measured,
        a mutant that made `_row_fits` return True unconditionally
        reddened exactly ONE of the five.

        So this one makes the ROW fat rather than the tail: argv is recorded in
        the receipt, so a huge argument puts the bare row near the ceiling
        before any diagnostic exists. Then the weighing is the only thing
        standing between the ledger and a refused append.

        THE RECEIPT OUTRANKS THE DIAGNOSTIC, and that is what is asserted: the
        row LANDS, the line on disk is within the ceiling, and the tail is
        ABSENT because there was no room for it. Read from the DESTINATION, per
        the ruling that reading the returned row cannot detect a
        downstream mutation.
        """
        fat, cap = "F" * 56000, gate.STDERR_TAIL_CAP
        require_supervisor()
        red, err = gate.run(repo=self.repo, argv=[sys.executable, "-c"] + [
            "import sys;sys.stderr.write('boom '*8000);"
            "sys.stderr.write('\\nRan 1 test in 0.1s\\n\\nFAILED (errors=1)\\n');"
            "raise SystemExit(1)", fat])
        self.assertIsNone(err, err)
        self.assertEqual("FAILED", red["status"], red.get("detail"))
        # PREMISE ONE: the BARE row really is near the ceiling, so a pass here
        # cannot be earned by the row being comfortably small.
        bare = {k: v for k, v in red.items()
                if k not in ("stderr_tail", "stderr_tail_meta")}
        bare_bytes = len(json.dumps(bare, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8"))
        self.assertGreater(bare_bytes,
                           gate.eventledger.MAX_EVENT_BYTES - cap)
        # THE RECEIPT SURVIVED. This is the property the weighing protects and
        # the one a defeated weighing destroys: keeping the full cap here puts
        # the row past the ceiling, append REFUSES it, and the red is lost.
        got, gerr = gate.by_id(red["id"])
        self.assertIsNotNone(got, gerr)
        with open(gate.receipts_path(), "rb") as handle:
            lines = [ln for ln in handle.read().split(b"\n") if ln.strip()]
        stored = json.loads(lines[-1])
        self.assertEqual(red["id"], stored["id"])
        self.assertLessEqual(len(lines[-1]) + 1,
                             gate.eventledger.MAX_EVENT_BYTES)
        # PREMISE TWO, and the discriminator: the tail was SHRUNK BELOW THE CAP
        # to make room. The cap alone cannot produce this — only the weighing
        # can — so an arm that saw a cap-sized tail here would be watching the
        # wrong layer.
        kept = len(stored["stderr_tail"].encode("utf-8"))
        self.assertLess(kept, cap)
        self.assertTrue(stored["stderr_tail_meta"]["truncated"])
        self.assertTrue(stored["stderr_tail"].endswith("FAILED (errors=1)\n"))

    def test_a_multibyte_stderr_cannot_cost_the_receipt_itself(self):
        """Measured on the first draft: 16,384 EMOJI serialize to
        65,564 bytes, over eventledger.MAX_EVENT_BYTES, and the ledger REFUSES
        an oversized payload rather than trimming — so the character cap did
        not cost the tail, it cost the entire red receipt.

        THE PAYLOAD IS GENERATED IN THE CHILD, NOT PASSED IN ARGV. The
        first version of this arm was caught failing for a reason that had
        nothing to do with the property: argv is RECORDED IN THE RECEIPT, so
        embedding 16k emoji in it pushed the bare row past the ceiling before
        any tail existed. The arm was red and would have been read as proof."""
        require_supervisor()
        red, err = gate.run(repo=self.repo, argv=[sys.executable, "-c"] + [
            "import sys;sys.stderr.write('\\U0001f600'*%d);"
            "sys.stderr.write('\\nRan 1 test in 0.1s\\n\\nFAILED (errors=1)\\n');"
            "raise SystemExit(1)" % gate.STDERR_TAIL_CAP])
        self.assertIsNone(err, err)
        # the argv did NOT carry the payload — this arm's own premise
        self.assertLess(len(json.dumps(red["argv"]).encode("utf-8")), 4096)
        # THE RECEIPT SURVIVED — the property that was lost before.
        self.assertEqual("FAILED", red["status"], red.get("detail"))
        got, gerr = gate.by_id(red["id"])
        self.assertIsNotNone(got, gerr)
        payload = json.dumps(red, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
        self.assertLess(len(payload), gate.eventledger.MAX_EVENT_BYTES)
        self.assertTrue(red["stderr_tail_meta"]["truncated"])

    def test_a_red_receipt_carrying_its_diagnostic_still_imports(self):
        """The receipt that most needs to travel is a RED one minted on a fab
        node, and a red is exactly the row that carries a tail — so a foreign
        key rejection here would have blocked precisely the receipts this
        feature exists to deliver. Measured on the first draft."""
        require_supervisor()
        red, err = gate.run(repo=self.repo, argv=[sys.executable] + _emit_exit(
            1, "boom", "Ran 1 test in 0.1s", "", "FAILED (errors=1)"))
        self.assertIsNone(err, err)
        self.assertIn("stderr_tail", red)
        from helm import gateimport
        # MUST-HIT FIRST: prove this validator can still REFUSE, so a None
        # below means accepted rather than "the check is inert".
        self.assertIsNotNone(gateimport._schema_err(dict(red, nonsense=1)))
        self.assertIsNone(gateimport._schema_err(red),
                          gateimport._schema_err(red))

    def test_original_pre_bracket_v1_receipt_keeps_its_frozen_hash(self):  # noqa: VACUOUS_ASSERTION — frozen id and round-trip are positive controls
        legacy = {
            "v": 1, "event": "gate", "ts": "2026-07-30T12:00:00Z",
            "head": "a" * 40, "tree": "b" * 40, "dirty": False,
            "repo_id": "/tmp/repo",
            "interpreter": {"name": "cpython", "language": "3.14.4",
                            "executable": "/usr/bin/python3"},
            "argv": ["/usr/bin/python3", "-m", "unittest"],
            "status": "OK", "ran": 7, "skipped": 1, "rc": 0,
            "id": "dc0cbc2edaa370ab",
        }
        self.assertEqual(gate._receipt_id(legacy), legacy["id"])
        self.assertTrue(gate.eventledger.append(gate.receipts_path(), legacy))
        back, err = gate.by_id(legacy["id"])
        self.assertIsNone(err, err)
        self.assertEqual(back["id"], legacy["id"])
        partial = dict(legacy, head_after=legacy["head"])
        with self.assertRaisesRegex(ValueError, "partial v1"):
            gate._receipt_id(partial)

    def test_v1_receipts_keep_their_FROZEN_content_identity_grammar(self):
        legacy = {
            "v": 1, "event": "gate", "ts": "2026-07-31T12:34:56Z",
            "head": "a" * 40, "tree": "b" * 40, "dirty": False,
            "head_after": "a" * 40, "tree_after": "b" * 40,
            "dirty_after": False, "repo_id": "/tmp/repo",
            "interpreter": {"name": "cpython", "language": "3.14.4",
                            "executable": "/usr/bin/python3"},
            "argv": ["/usr/bin/python3", "-m", "unittest"],
            "status": "OK", "ran": 7, "skipped": 1, "rc": 0,
            "id": "a2b8be1a4f65aa62",
        }
        self.assertEqual(gate._receipt_id(legacy), "a2b8be1a4f65aa62")
        self.assertTrue(gate.eventledger.append(gate.receipts_path(), legacy))
        back, err = gate.by_id(legacy["id"])
        self.assertIsNone(err, err)
        self.assertEqual(back["id"], legacy["id"])
        injected = dict(legacy, failures=[{
            "kind": "ERROR", "test": "invented.test", "traceback": "fake"}],
                        failures_unreadable=False)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            gate._show_failures(injected)
        self.assertIn("UNAVAILABLE", stdout.getvalue())
        self.assertNotIn("invented.test", stdout.getvalue())

    def test_hash_valid_malformed_fields_render_safely_on_one_line(self):  # noqa: VACUOUS_ASSERTION — each receipt id is the positive rendered control
        row = self.mint("Ran 1 test in 0.1s", "", "OK")
        path = gate.receipts_path()
        with open(path, encoding="utf-8") as fh:
            base = [json.loads(line) for line in fh if line.strip()][-1]
        rows = [dict(base, skipped="many"),
                dict(base, interpreter="bad"),
                dict(base, tree=7),
                dict(base, interpreter={"name": "cp\nforged",
                                        "language": "3.14.4"}),
                dict(base, status="OK\nFORGED")]
        for stored in rows:
            stored["id"] = gate._receipt_id(stored)
        with open(path, "w", encoding="utf-8") as fh:
            for stored in rows:
                fh.write(json.dumps(stored) + "\n")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(gate.cmd_gate(["list"]), 0)
            for stored in rows:
                self.assertEqual(gate.cmd_gate(["show", stored["id"]]), 0)
        shown = stdout.getvalue()
        for stored in rows:
            self.assertIn(stored["id"], shown)
            self.assertNotIn("\n", gate.evidence_line(stored))
        self.assertNotIn("(skipped=", gate.evidence_line(rows[0]))
        self.assertIn("UNKNOWN", gate.evidence_line(rows[1]))
        self.assertIn("tree=7", gate.evidence_line(rows[2]))
        self.assertIn("cp forged-3.14.4", gate.evidence_line(rows[3]))
        self.assertIn("OK FORGED", gate.evidence_line(rows[4]))
        self.assertNotIn("\nFORGED", shown)
        self.assertNotEqual(row["id"], rows[0]["id"])

    def test_a_REAL_receipt_survives_the_disk_round_trip(self):
        """THE CONTROL FOR BOTH ABOVE. Without it, a recompute that never
        matches anything would pass every forgery test by rejecting the whole
        ledger — the census-reports-zero failure, one layer down."""
        row = self.mint("Ran 7 tests in 0.3s", "", "OK")
        back, err = gate.by_id(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(back["id"], row["id"])
        self.assertEqual(back["ran"], 7)

    def test_a_receipt_with_no_post_run_read_REFUSES(self):
        """A pre-bracket receipt was never bracketed, and reading its missing
        half as 'unchanged' is the pass-whose-input-was-missing this module
        exists to refuse."""
        row = self.mint("Ran 1 test in 0.1s", "", "OK")
        stripped = dict(row)
        stripped.pop("head_after")
        with mock.patch.object(gate, "receipts",
                               return_value=([stripped], None, 0)):
            state, _rid, why = gate.bind(gate.evidence_line(row), self.head)
        self.assertEqual(state, "REFUSED")
        self.assertIn("no post-run tree read", why)


class Binding(GateBase):
    def test_no_token_is_unverified_not_refused(self):
        state, rid, why = gate.bind("whole-suite Ran 5115 OK", self.head)
        self.assertEqual(state, "UNVERIFIED")
        self.assertIsNone(rid)

    def test_a_token_naming_nothing_is_refused(self):
        state, _, why = gate.bind("gate:deadbeefdeadbeef ok", self.head)
        self.assertEqual(state, "REFUSED")
        self.assertIn("no minted gate receipt", why)

    def test_a_receipt_for_another_commit_is_refused(self):
        row = self.mint("Ran 3 tests in 0.1s", "", "OK")
        self._dirty()
        self._git("add", "-A")
        self._git("commit", "-qm", "second")
        moved = self._git("rev-parse", "HEAD")
        self.assertNotEqual(moved, self.head)
        state, _, why = gate.bind(gate.evidence_line(row), moved)
        self.assertEqual(state, "REFUSED")
        self.assertIn("re-run the gate on the reviewed tip", why)

    def test_a_dirty_receipt_is_refused(self):
        self._dirty()
        row = self.mint("Ran 3 tests in 0.1s", "", "OK")
        state, _, why = gate.bind(gate.evidence_line(row), self.head)
        self.assertEqual(state, "REFUSED")
        self.assertIn("DIRTY", why)

    def test_a_failing_receipt_is_refused(self):
        row = self.mint("Ran 3 tests in 0.1s", "", "FAILED (failures=1)")
        state, _, why = gate.bind(gate.evidence_line(row), self.head)
        self.assertEqual(state, "REFUSED")
        self.assertIn("FAILED", why)

    def test_a_custom_command_can_never_bind(self):
        """helm did not choose the interpreter, so it cannot name it, so the
        receipt does not answer the question the gate exists to answer."""
        row = self.mint("Ran 3 tests in 0.1s", "", "OK")
        self.assertIsNone(row["interpreter"])
        state, _, why = gate.bind(gate.evidence_line(row), self.head)
        self.assertEqual(state, "REFUSED")
        self.assertIn("custom command", why)

    def test_a_clean_green_suite_receipt_verifies(self):
        with serial_process(ran=9):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        state, rid, why = gate.bind(gate.evidence_line(row), self.head)
        self.assertEqual(state, "VERIFIED")
        self.assertEqual(rid, row["id"])
        self.assertIn(sys.implementation.name.replace("cpython", "CPython"), why)

    def test_a_later_receipt_that_contains_the_reviewed_tip_verifies(self):
        reviewed, descendant, row = self.descendant_receipt()
        state, rid, why = gate.bind(
            gate.evidence_line(row), reviewed, repo_id=self.gitdir(),
            reviewed_ts="2026-08-02T00:00:01Z")
        self.assertEqual(state, "VERIFIED", why)
        self.assertEqual(rid, row["id"])
        self.assertIn(reviewed[:12], why)
        self.assertIn(descendant[:12], why)

    def test_a_whole_suite_train_receipt_carries_a_cherry_picked_review(self):
        """Two reviewed patches bind inside a larger integration train.

        The reviewed SHA is not an ancestor, so only repository-derived ordered
        patch content can bind this APPROVE. Unrelated cars on both sides are
        deliberately present; the correct rule is one contiguous 2->26 run
        rather than whole-history equality.
        """
        _base, reviewed, train, row = self.train_receipt()
        verdict = vcs.backend(self.gitdir()).patch_sequence_containment(
            self.gitdir(), reviewed, train)
        self.assertEqual(verdict, (vcs.PATCH_SEQUENCE_CONTAINED, 12, 2, 26))
        state, rid, detail = gate.bind(
            gate.evidence_line(row), reviewed, repo_id=self.gitdir(),
            reviewed_ts="2026-08-02T00:00:01Z")
        self.assertEqual(state, "VERIFIED", detail)
        self.assertEqual(rid, row["id"])
        self.assertIn("patches 13..14 of 26", detail)

    def test_a_CARRIED_train_receipt_binds_though_it_PREDATES_the_review(self):
        """CONTAINMENT OUTRANKS THE TIMESTAMP THAT PROXIES FOR IT.

        This arm asserted the opposite until task/2165 and the reversal is
        deliberate. The order protects against a receipt that cannot have run
        on this work; `carriage` answering CARRIED is that same question
        answered from the REPOSITORY rather than from two clocks, and a direct
        reading outranks its own proxy.

        THE WORKFLOW MAKES THE PROXY WRONG RATHER THAN MERELY REDUNDANT: an
        integrator composes a train, gates it, and mints the reviewer's row ON
        THE GREEN, so in a train the receipt ALWAYS predates the row. The gap
        is seconds and the containment is exact, so refusing on the clock
        refuses the ordinary case.

        ITS TWO CONTROLS ARE THE ARMS BELOW and neither is decoration: the
        NOT_CARRIED sibling proves the rung still bites, and the
        unreadable-timestamp sibling proves only the ORDER yielded.
        """
        _base, reviewed, _train, row = self.train_receipt(
            receipt_ts="2026-08-02T00:00:01Z")
        state, rid, detail = gate.bind(
            gate.evidence_line(row), reviewed, repo_id=self.gitdir(),
            reviewed_ts="2026-08-02T00:00:02Z")
        self.assertEqual(state, "VERIFIED", detail)
        self.assertEqual(rid, row["id"])
        self.assertIn("patches 13..14 of 26", detail)

    def test_a_train_missing_the_reviewed_patches_refuses(self):
        _base, reviewed, _train, row = self.train_receipt(picks=False)
        state, _rid, why = gate.bind(
            gate.evidence_line(row), reviewed, repo_id=self.gitdir(),
            reviewed_ts="2026-08-02T00:00:01Z")
        self.assertEqual(state, "REFUSED")
        self.assertIn("absent or reordered", why)

    def test_a_neighbour_three_lines_away_rekeys_a_carried_car_to_ABSENT(self):
        """A false negative of the kind this message exists for. The
        reviewed change IS aboard the train — the fixture asserts its content
        is in the tree — but a neighbouring car three lines away in the same
        file moved its diff CONTEXT, and patch-id hashes context. So
        containment reports ABSENT for work that is present, and the operator
        is told his patches are missing.

        SCOPE: this pins THIS fixture's geometry (a 30-line shared file,
        reviewed edit at line 20), not a general law about distance. The gap-4
        sibling is its control — one line further out and nothing re-keys —
        which is what makes the pair a measurement rather than a claim about
        every shared file."""
        _base, reviewed, train, row, picked, original = \
            self.rekeyed_train_receipt(gap=3)
        self.assertNotEqual(picked, original,
                            "gap 3 did not re-key — the band has moved and "
                            "this arm no longer tests what it names")
        state, _start, _rn, _cn = vcs.backend(self.gitdir()) \
            .patch_sequence_containment(self.repo, reviewed, train)
        self.assertEqual(state, vcs.PATCH_SEQUENCE_ABSENT)
        bstate, _rid, why = gate.bind(
            gate.evidence_line(row), reviewed, repo_id=self.gitdir(),
            reviewed_ts="2026-08-02T00:00:01Z")
        self.assertEqual(bstate, "REFUSED")
        # the door still fails CLOSED — that is correct and is NOT the defect
        self.assertIn("re-keyed by a neighbouring change", why)
        self.assertIn("a patch-id miss never proves absence", why)

    def test_a_neighbour_four_lines_away_keeps_the_patch_id(self):
        """The control that makes the arm above a measurement rather than a
        truism: the SAME construction one line further out does not re-key,
        and containment sees the car. Together the two arms bound this
        fixture's behaviour; neither asserts what any other geometry does."""
        _base, reviewed, train, _row, picked, original = \
            self.rekeyed_train_receipt(gap=4)
        self.assertEqual(picked, original,
                         "gap 4 re-keyed — the context window is wider than "
                         "measured and the band is not 3")
        state, _start, _rn, _cn = vcs.backend(self.gitdir()) \
            .patch_sequence_containment(self.repo, reviewed, train)
        self.assertEqual(state, vcs.PATCH_SEQUENCE_CONTAINED)

    def test_the_absent_refusal_names_re_keying_as_another_cause(self):
        """A zero patch-id hit has MORE causes than absence and reordering,
        and the refusal used to name only those two. ANOTHER is a NEIGHBOURING
        change in a shared file re-keying the reviewed car's patch-id by
        moving its diff context. The list is not claimed to be exhaustive:
        patch-id equality is a SUFFICIENT test for presence and never a
        NECESSARY one. An integrator who KNEW the car was aboard read 'absent
        or reordered' and went hunting their own composition. This pins the
        added clause onto the refusal an operator actually sees, driven
        through the real bind path rather than asserted against a literal."""
        _base, reviewed, _train, row = self.train_receipt(picks=False)
        state, _rid, why = gate.bind(
            gate.evidence_line(row), reviewed, repo_id=self.gitdir(),
            reviewed_ts="2026-08-02T00:00:01Z")
        self.assertEqual(state, "REFUSED")
        # the two causes that were always named stay named
        self.assertIn("absent or reordered", why)
        # and the one that sent people hunting their own work
        self.assertIn("re-keyed by a neighbouring change", why)
        self.assertIn("a patch-id miss never proves absence", why)

    def test_a_train_reordering_the_reviewed_patches_refuses(self):
        _base, reviewed, _train, row = self.train_receipt(reverse=True)
        state, _rid, why = gate.bind(
            gate.evidence_line(row), reviewed, repo_id=self.gitdir(),
            reviewed_ts="2026-08-02T00:00:01Z")
        self.assertEqual(state, "REFUSED")
        self.assertIn("absent or reordered", why)

    def test_ambiguous_or_unreadable_patch_carry_never_authorizes(self):
        _base, reviewed, _train, row = self.train_receipt()
        real = vcs.backend(self.gitdir())
        control, _rid, control_why = gate.bind(
            gate.evidence_line(row), reviewed, repo_id=self.gitdir(),
            reviewed_ts="2026-08-02T00:00:01Z")
        self.assertEqual(control, "VERIFIED", control_why)
        for sequence_state, phrase in (
                (vcs.PATCH_SEQUENCE_AMBIGUOUS, "appears more than once"),
                (vcs.PATCH_SEQUENCE_UNKNOWN, "cannot be derived")):
            backend = mock.Mock()
            backend.text.side_effect = real.text
            backend.ancestry.return_value = vcs.NOT_ANCESTOR
            backend.patch_sequence_containment.return_value = (
                sequence_state, None, None, None)
            with self.subTest(state=sequence_state), mock.patch.object(
                    gate.vcs, "backend", return_value=backend):
                state, _rid, why = gate.bind(
                    gate.evidence_line(row), reviewed, repo_id=self.gitdir(),
                    reviewed_ts="2026-08-02T00:00:01Z")
            self.assertEqual(state, "REFUSED")
            self.assertIn(phrase, why)
            backend.patch_sequence_containment.assert_called_once_with(
                self.gitdir(), reviewed, row["head"])

    def test_descendant_binding_uses_the_dispatch_repo_not_receipt_scratch(self):
        """Historical symbol retained; an unplaceable origin is now UNKNOWN.

        The dispatch repository supplies ancestry, not receipt authority. A
        durable import binding is required before a reaped scratch receipt can
        be spent there.
        """
        reviewed, _descendant, row = self.descendant_receipt()
        scratch_gone = dict(row, repo_id=os.path.join(self.tmp, "gone-scratch.git"))
        with mock.patch.object(gate, "receipts",
                               return_value=([scratch_gone], None, 0)):
            state, rid, why = gate.bind(
                gate.evidence_line(row), reviewed, repo_id=self.gitdir(),
                reviewed_ts="2026-08-02T00:00:01Z")
        self.assertEqual((state, rid), ("REFUSED", row["id"]))
        self.assertIn("no canonical authority", why)

    def test_a_PREDATING_descendant_receipt_binds_because_it_CARRIES(self):
        """SAME FIXTURE, REVERSED CLAIM, AND THE REVERSAL IS THE CURE.

        A descendant is the STRONGEST carriage there is: ancestry alone proves
        the receipt ran on a tree containing the reviewed commit. The clock
        was asking whether the receipt could have run on this work, and
        ancestry answers that question directly, so the proxy has nothing left
        to add on this receipt.

        Its two siblings below keep the rung honest by supplying the
        populations it still polices — a predating receipt that carries
        NOTHING, and one whose carriage cannot be derived.
        """
        reviewed, _descendant, row = self.descendant_receipt(
            receipt_ts="2026-08-02T00:00:01Z")
        state, _rid, detail = gate.bind(
            gate.evidence_line(row), reviewed, repo_id=self.gitdir(),
            reviewed_ts="2026-08-02T00:00:02Z")
        self.assertEqual(state, "VERIFIED", detail)
        self.assertIn("containing reviewed", detail)

    def test_a_PREDATING_receipt_with_NO_carriage_still_refuses_on_the_clock(self):
        """THE RUNG STILL BITES, AND THIS IS WHERE THAT IS MEASURED.

        One input differs from the arm above: the receipt's history does not
        contain the reviewed work at all. Carriage answers NOT_CARRIED, the
        clock refuses, and the refusal names the order rather than the
        content — a change that DELETED the rung instead of subordinating it
        passes the arm above and fails here.
        """
        _base, reviewed, _train, row = self.train_receipt(
            picks=False, receipt_ts="2026-08-02T00:00:01Z")
        state, _rid, why = gate.bind(
            gate.evidence_line(row), reviewed, repo_id=self.gitdir(),
            reviewed_ts="2026-08-02T00:00:02Z")
        self.assertEqual(state, "REFUSED")
        self.assertIn("does not postdate", why)

    def test_a_MALFORMED_receipt_timestamp_refuses_however_well_it_carries(self):
        """ONLY THE ORDER YIELDED — A MALFORMED RECORD STILL FAILS CLOSED.

        An absent or unparseable stamp is a corrupt RECORD, not an ordering
        fact, so the two rungs that read it sit above the carriage read and a
        proven carriage may not rescue them.

        THE STAMP MUST BE IN THE LEDGER, WHICH IS THE HALF I GOT WRONG FIRST.
        `bind` resolves its receipt by TOKEN — tok = token(evidence); row =
        by_id(tok) — and the timestamp is not in the evidence string at all,
        so mutating a local copy of the row and re-rendering its evidence
        line produces BYTE-IDENTICAL text and changes nothing the code can
        see. Minting through the fixture's own receipt_ts is what puts the
        malformed stamp where the rung will read it.
        """
        _base, reviewed, _train, row = self.train_receipt(
            receipt_ts="not-a-time")
        self.assertEqual(row.get("ts"), "not-a-time",
                         "the malformed stamp must reach the LEDGER row, or "
                         "this arm measures nothing")
        state, _rid, why = gate.bind(
            gate.evidence_line(row), reviewed, repo_id=self.gitdir(),
            reviewed_ts="2026-08-02T00:00:02Z")
        self.assertEqual(state, "REFUSED")
        self.assertIn("no canonical timestamp", why)

    def test_a_MALFORMED_review_stamp_refuses_however_well_it_carries(self):
        """THE REVIEW'S OWN STAMP IS THE OTHER HALF OF THE SAME RUNG.

        Split from its sibling rather than sharing one test body, because
        `train_receipt` IS NOT IDEMPOTENT WITHIN A TEST: it cuts
        integration/train from the same base each time, so a second call in
        one method leaves the new reviewed commit an ANCESTOR of the train
        and the fixture's own non-ancestor guard fires with `0 == 0` — a
        fixture death, never a statement about bind. One call per method.
        """
        _base, reviewed, _train, good = self.train_receipt(
            receipt_ts="2026-08-02T00:00:01Z")
        state, _rid, why = gate.bind(
            gate.evidence_line(good), reviewed, repo_id=self.gitdir(),
            reviewed_ts="not-a-time")
        self.assertEqual(state, "REFUSED")
        self.assertIn("no canonical opening timestamp", why)

    def test_a_divergent_receipt_does_not_bind(self):
        reviewed = self.head
        self._git("checkout", "-q", "main")
        with open(os.path.join(self.repo, "sibling.txt"), "w") as fh:
            fh.write("divergent review\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "divergent reviewed tip")
        sibling = self._git("rev-parse", "HEAD")
        self._git("checkout", "-q", "lane/probe")
        with serial_process(ran=9), \
                mock.patch.object(
                    gate.pk, "now_ts", return_value="2026-08-02T00:00:02Z"):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["head"], reviewed)
        state, _rid, why = gate.bind(
            gate.evidence_line(row), sibling, repo_id=self.gitdir(),
            reviewed_ts="2026-08-02T00:00:01Z")
        self.assertEqual(state, "REFUSED")
        self.assertIn("does not contain", why)

    def test_unknown_descendant_ancestry_refuses(self):
        reviewed, _descendant, row = self.descendant_receipt()
        real = gate.vcs.backend(self.gitdir())
        backend = mock.Mock()
        backend.text.side_effect = real.text
        backend.ancestry.return_value = gate.vcs.UNKNOWN
        with mock.patch.object(gate.vcs, "backend", return_value=backend):
            state, _rid, why = gate.bind(
                gate.evidence_line(row), reviewed, repo_id=self.gitdir(),
                reviewed_ts="2026-08-02T00:00:01Z")
        self.assertEqual(state, "REFUSED")
        self.assertIn("cannot be proven to contain", why)

    def test_descendant_binding_needs_the_standing_repository(self):
        reviewed, _descendant, row = self.descendant_receipt()
        state, _rid, why = gate.bind(
            gate.evidence_line(row), reviewed,
            repo_id=os.path.join(self.tmp, "missing.git"),
            reviewed_ts="2026-08-02T00:00:01Z")
        self.assertEqual(state, "REFUSED")
        self.assertIn("repository is unreadable", why)

    def test_descendant_binding_needs_two_canonical_timestamps(self):
        reviewed, _descendant, row = self.descendant_receipt()
        state, _rid, why = gate.bind(
            gate.evidence_line(row), reviewed, repo_id=self.gitdir(),
            reviewed_ts="not-a-time")
        self.assertEqual(state, "REFUSED")
        self.assertIn("no canonical opening timestamp", why)
        broken = dict(row, ts="not-a-time")
        with mock.patch.object(gate, "receipts",
                               return_value=([broken], None, 0)), \
                mock.patch.object(gateimport, "repository_authorization",
                                  return_value=(True, None)):
            state, _rid, why = gate.bind(
                gate.evidence_line(row), reviewed, repo_id=self.gitdir(),
                reviewed_ts="2026-08-02T00:00:01Z")
        self.assertEqual(state, "REFUSED")
        self.assertIn("no canonical timestamp", why)


class VerdictBinding(GateBase):
    """The chokepoint: every gate claim in helm enters through mark_verdict."""

    def _dispatch(self, tip):
        os.makedirs(os.environ["HELM_HOME"], exist_ok=True)
        row, err = dispatches.add("reviewer", "a-lane", tip, kind="review",
                                  repo=self.repo, notify=False, _reason=True, new_work=True)
        self.assertIsNone(err, err)
        self.assertIsNotNone(row, "fixture dispatch was not persisted")
        return row

    def test_a_verdict_citing_another_commits_receipt_is_refused(self):
        """MUTATION-BOUND: delete the gate.bind call in mark_verdict and this
        test goes red. The verdict below is well-formed in every way the old
        code checked — right dispatch, right reviewed tip, evidence under 256
        printable characters — and is a lie only about the thing nothing used
        to read."""
        row = self.mint("Ran 3 tests in 0.1s", "", "OK")   # minted at self.head
        self._dirty()
        self._git("add", "-A")
        self._git("commit", "-qm", "second")
        moved = self._git("rev-parse", "HEAD")
        d = self._dispatch(moved)
        got, err = dispatches.mark_verdict(
            d["id"], moved, gate.evidence_line(row), "approve")
        self.assertIsNone(got)
        self.assertIn("gate evidence does not bind", err)

    def test_untokened_FIX_records_unverified_and_is_not_blocked(self):
        """A negative verdict authorizes no land, so it remains actionable
        without making the reviewer pay a whole-suite gate first."""
        d = self._dispatch(self.head)
        got, err = dispatches.mark_verdict(
            d["id"], self.head, "3 blockers", "fix")
        self.assertIsNone(err, err)
        self.assertEqual(got["polarity"], "fix")
        self.assertEqual(got["gate"], "")
        self.assertEqual(dispatches.gate_state(got), "UNVERIFIED")

    def test_a_bound_verdict_records_the_receipt_id(self):  # noqa: VACUOUS_ASSERTION — exact stored receipt id and VERIFIED projection positively control both successful writes
        with serial_process(ran=9):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        d = self._dispatch(self.head)
        got, err = dispatches.mark_verdict(
            d["id"], self.head, gate.evidence_line(row), "approve")
        self.assertIsNone(err, err)
        self.assertEqual(got["gate"], row["id"])
        self.assertEqual(dispatches.gate_state(got), "VERIFIED " + row["id"])

    def test_same_lane_movement_refuses_before_gate_bind_or_epoch(self):  # noqa: VACUOUS_ASSERTION — verified receipt and named movement positively control the intentional no-write assertions
        reviewed = self.head
        with mock.patch.object(
                dispatches.pk, "now_ts",
                return_value="2026-08-02T00:00:01Z"):
            d = self._dispatch(reviewed)
        self.assertEqual(d.get("ref_branch"), "refs/heads/lane/probe")
        with open(os.path.join(self.repo, "same-lane.txt"), "w") as fh:
            fh.write("same lane moved\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "same lane moved")
        moved = self._git("rev-parse", "HEAD")
        self.assertNotEqual(moved, reviewed)
        self.assertEqual(self._git("rev-parse", "lane/probe"), moved)
        with serial_process(ran=9), \
                mock.patch.object(
                    gate.pk, "now_ts",
                    return_value="2026-08-02T00:00:02Z"):
            receipt, receipt_err = gate.run(repo=self.repo)
        self.assertIsNone(receipt_err, receipt_err)
        state, rid, why = gate.bind(
            gate.evidence_line(receipt), reviewed, repo_id=d["repo_id"],
            reviewed_ts=d["ts"])
        self.assertEqual(state, "VERIFIED", why)
        self.assertEqual(rid, receipt["id"])
        before = dispatches.history(d["id"])
        self.assertEqual([e.get("event") for e in before], ["dispatch"])
        self.assertFalse(os.path.exists(dispatches.epoch_path()))
        with mock.patch.object(gate, "bind", wraps=gate.bind) as bind, \
                mock.patch.object(
                    dispatches, "record_gate_epoch",
                    wraps=dispatches.record_gate_epoch) as epoch:
            got, err = dispatches.mark_verdict(
                d["id"], reviewed, gate.evidence_line(receipt), "approve")
        self.assertIsNone(got)
        self.assertIn("moved under this review", err)
        bind.assert_not_called()
        epoch.assert_not_called()
        after = dispatches.history(d["id"])
        self.assertEqual(after, before)
        self.assertFalse(any(e.get("event") == "verdict" for e in after))
        self.assertFalse(os.path.exists(dispatches.epoch_path()))

    def test_a_verdict_binds_a_later_receipt_containing_its_tip(self):  # noqa: VACUOUS_ASSERTION — stored gate id plus exactly-one appended verdict are positive controls on the successful writer
        reviewed = self.head
        with mock.patch.object(
                dispatches.pk, "now_ts",
                return_value="2026-08-02T00:00:01Z"):
            d = self._dispatch(reviewed)
        _reviewed, descendant, row = self.descendant_receipt()
        before = len(dispatches.history(d["id"]))
        got, err = dispatches.mark_verdict(
            d["id"], reviewed, gate.evidence_line(row), "approve")
        self.assertIsNone(err, err)
        self.assertEqual(got["gate"], row["id"])
        self.assertNotEqual(descendant, reviewed)
        self.assertEqual(len(dispatches.history(d["id"])), before + 1)
        again, err = dispatches.mark_verdict(
            d["id"], reviewed, gate.evidence_line(row), "approve")
        self.assertIsNone(err, err)
        self.assertEqual(again["gate"], row["id"])
        self.assertEqual(len(dispatches.history(d["id"])), before + 1)

    def test_an_approve_records_against_a_cherry_picked_train_receipt(self):
        with mock.patch.object(
                dispatches.pk, "now_ts",
                return_value="2026-08-02T00:00:01Z"):
            _base, reviewed, _train, receipt = self.train_receipt()
            d = self._dispatch(reviewed)
        # train_receipt stamps its receipt at 00:00:02; the dispatch timestamp is
        # held at 00:00:01 above, so content cannot hide a predated gate.
        got, err = dispatches.mark_verdict(
            d["id"], reviewed, gate.evidence_line(receipt), "approve")
        self.assertIsNone(err, err)
        self.assertEqual(got["gate"], receipt["id"])
        self.assertEqual(got["polarity"], "approve")

    def test_a_predated_descendant_receipt_APPENDS_its_verdict(self):
        """THE VERDICT DOOR FOLLOWS THE BINDING RULE, SAME FIXTURE, REVERSED.

        This arm asserted the refusal and now asserts the append, because the
        receipt CARRIES the reviewed tip by ancestry and containment outranks
        the clock that proxies for it. The sibling below keeps the door
        closed for the population the clock still polices.
        """
        reviewed = self.head
        with mock.patch.object(
                dispatches.pk, "now_ts",
                return_value="2026-08-02T00:00:02Z"):
            d = self._dispatch(reviewed)
        _reviewed, _descendant, row = self.descendant_receipt(
            receipt_ts="2026-08-02T00:00:01Z")
        before = len(dispatches.history(d["id"]))
        got, err = dispatches.mark_verdict(
            d["id"], reviewed, gate.evidence_line(row), "approve")
        self.assertIsNone(err, err)
        self.assertIsNotNone(got)
        self.assertEqual(len(dispatches.history(d["id"])), before + 1)

    def test_a_predated_receipt_that_carries_NOTHING_appends_no_verdict(self):
        """THE MUST-MISS AT THE VERDICT DOOR, and it is the same one input.

        The receipt predates the review exactly as above and its history does
        not contain the reviewed work, so the clock refuses and the ledger
        stays untouched. An implementation that dropped the rung rather than
        subordinating it passes the arm above and fails here — and the length
        assertion is what proves nothing was written on the way to the
        refusal.
        """
        with mock.patch.object(
                dispatches.pk, "now_ts",
                return_value="2026-08-02T00:00:02Z"):
            d = self._dispatch(self.head)
        _base, reviewed, _train, row = self.train_receipt(
            picks=False, receipt_ts="2026-08-02T00:00:01Z")
        before = len(dispatches.history(d["id"]))
        got, err = dispatches.mark_verdict(
            d["id"], reviewed, gate.evidence_line(row), "approve")
        self.assertIsNone(got)
        self.assertIsNotNone(err)
        self.assertEqual(len(dispatches.history(d["id"])), before)

    def test_an_idempotent_retry_answers_in_the_SAME_shape(self):  # noqa: VACUOUS_ASSERTION — the first successful verdict and bound gate positively control the retry-shape equality
        """The first draft returned gate_state/gate_why beside `gate`, and the
        retry path — which early-returns the REPLAYED row — could not carry
        them, so a retry answered in a different shape than the original call.
        tests/test_dispatches caught it at the land gate. `gate` is the one
        recorded field and gate_state() derives the rest, so there is nothing
        left to disagree."""
        with serial_process(ran=9):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        d = self._dispatch(self.head)
        ev = gate.evidence_line(row)
        first, err = dispatches.mark_verdict(
            d["id"], self.head, ev, "approve")
        self.assertIsNone(err, err)
        again, err = dispatches.mark_verdict(
            d["id"], self.head, ev, "approve")
        self.assertIsNone(err, err)
        self.assertEqual(set(again), set(first))
        self.assertEqual(again["gate"], first["gate"])

    def test_replay_never_invents_a_binding_history_lacks(self):
        """A pre-gate verdict event carries no `gate` key, and "" is the TRUE
        reading of it. If replay defaulted to anything else, every verdict
        recorded before today would retroactively claim a run."""
        d = self._dispatch(self.head)
        self.assertTrue(dispatches.eventledger.append(
            dispatches.ledger_path(), {
                "v": 3, "event": "verdict", "seq": d["seq"] + 1,
                "id": d["id"], "ts": dispatches.pk.now_ts(),
                "reviewed_tip": self.head, "verdict_ref": "prose only",
                "polarity": "approve"}))
        current, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        replayed = current[d["id"]]
        self.assertEqual(replayed["status"], "verdict")
        self.assertEqual(replayed.get("gate"), "")


class PolicyKindCaseTest(GateBase):
    """A live bypass, reproduced THROUGH THE DOCUMENTED WRITER.

    `policy_kind` was compared after .strip() and never casefolded, on both the
    write and the read side. So a policy written as `Approval-Tier` — one
    capital, every validation passing, through the real writer — is INVISIBLE
    to a reader asking for `approval-tier`. The land gate then reads "none",
    and none PERMITS. The operator believes the tier is enforced while the gate
    sees an empty store: a silent bypass of the whole mechanism, from a
    keystroke.

    ONE PLACE IS NOT ENOUGH IN EITHER DIRECTION, which is the part worth
    keeping. Fixing only the WRITE leaves every row already on disk invisible;
    fixing only the READ lets new mixed-case rows keep arriving. A
    normalization has to be shared by the writer and the reader or it is not a
    normalization, it is a convention."""

    def _write(self, kind):
        from helm import store
        store.write_prior({
            "id": "tier-case-probe", "type": "prior", "class": "certain",
            "confidence": 1.0, "status": "live", "policy_kind": kind,
            "policy_members": ["seat:kimi"], "policy_reason": "probe",
            "statement": "probe", "source": "test", "keywords": "probe",
        })

    def test_a_MIXED_CASE_kind_is_still_found(self):
        from helm import store
        self._write("Approval-Tier")
        self.assertTrue(store.policy_declared("approval-tier"),
                        "a policy written as Approval-Tier is invisible to a "
                        "reader asking for approval-tier — the land gate then "
                        "reads 'none', and none PERMITS")

    def test_the_lookup_is_case_insensitive_from_BOTH_ends(self):
        from helm import store
        self._write("approval-tier")
        for asked in ("approval-tier", "Approval-Tier", "APPROVAL-TIER"):
            self.assertTrue(store.policy_declared(asked), asked)

    def test_a_LEGACY_mixed_case_row_ON_DISK_is_found(self):
        """THIS IS WHAT THE READ-SIDE CASEFOLD ALONE BUYS, and the mutation
        pass proved I needed it: with both sides folding, reverting EITHER left
        every test green because the other covered it — the belt-and-braces
        trap named in the same breath as the bug.

        A row written BEFORE the writer canonicalized keeps its mixed case on
        disk forever. Only the reader can save it, so this writes past the
        writer's normalization to make one."""
        from helm import store
        self._write("approval-tier")
        # rewrite the stored row's kind in place — a legacy artifact the
        # canonicalizing writer will never produce again
        import glob, os as _os
        # WHERE THE WRITER ACTUALLY PUT IT, measured rather than assumed: my
        # first version globbed adopted_memory_dir(), which resolves to the
        # OWNER'S real memory dir and not this test's scratch HELM_HOME — the
        # fixture row was never there, so the test failed for a reason that had
        # nothing to do with the property under test.
        hits = glob.glob(_os.path.join(_os.environ["HELM_HOME"], "_global",
                                       "premises", "*tier-case-probe*"))
        self.assertTrue(hits, "fixture row not on disk")
        for path in hits:
            with open(path, encoding="utf-8") as fh:
                body = fh.read()
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body.replace("policy_kind: approval-tier",
                                      "policy_kind: Approval-Tier"))
        self.assertTrue(store.policy_declared("approval-tier"),
                        "a legacy mixed-case row on disk is invisible — only "
                        "the READ-side casefold can reach it")

    def test_the_WRITER_canonicalizes_so_the_store_stops_drifting(self):
        """AND THIS IS WHAT THE WRITE SIDE ALONE BUYS. Reading tolerantly makes
        lookups work; it does not stop the store accumulating a variant per
        keystroke. The stored row must be canonical."""
        from helm import store
        self._write("Approval-Tier")
        rows = [e for e in store.load_all(include_retired=True, types=("prior",))
                if str(e.get("id")) == "tier-case-probe"]
        self.assertTrue(rows, "fixture row did not load back")
        self.assertEqual(str(rows[0].get("policy_kind")), "approval-tier",
                         "the writer stored the kind uncanonicalized")

    def test_an_UNRELATED_kind_is_still_absent(self):
        """THE CONTROL. Without it the two above pass for a version where
        policy_declared always says yes — which would 'fix' the bypass by
        removing the answer."""
        from helm import store
        self._write("Approval-Tier")
        self.assertFalse(store.policy_declared("no-such-policy-kind"))


class ApprovalTierAtLandTest(GateBase):
    """The tier was a SEND-TIME warning nothing ever read again.

    MEASURED 2026-07-31: an out-of-tier APPROVE was RECORDED on the ledger and
    the row read READY. Only one agent noticing held the land. A policy
    enforced socially is the council's single finding — helm's claims about
    itself are not checked — operating on helm's own governance."""

    def _dispatch(self, tip, recipient="reviewer"):
        os.makedirs(os.environ["HELM_HOME"], exist_ok=True)
        row, err = dispatches.add(recipient, "a-lane", tip, kind="review",
                                  repo=self.repo, notify=False, _reason=True, new_work=True)
        self.assertIsNone(err, err)
        return row

    def _land_state(self, tier):
        d = self._dispatch(self.head)
        with mock.patch.object(dispatches, "GATE_CAPS", ()):
            _got, err = dispatches.mark_verdict(d["id"], self.head, "ok",
                                                polarity="approve")
        self.assertIsNone(err, err)
        with mock.patch.object(
                dispatches, "approval_tier_for_verdict", return_value=tier):
            lr, err = landreq.get(d["id"])
        self.assertIsNone(err, err)
        return lr

    def test_an_OUTSIDE_reviewer_never_reaches_READY(self):
        lr = self._land_state(("outside", "@gemini is outside the tier"))
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertIn("approval tier does not permit", lr["ungated"])
        self.assertIn("outside the tier", lr["ungated"])

    def test_an_UNEVALUABLE_tier_never_reaches_READY(self):
        """UNKNOWN never authorizes — a tier we cannot read cannot permit a
        merge, and this is the same law the gate epoch and the gc fetch both
        settled on."""
        lr = self._land_state(("unknown", "policy p1 has malformed selector"))
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertIn("malformed selector", lr["ungated"])

    def test_NO_TIER_permits_and_IN_TIER_permits(self):
        """THE CONTROL, both halves. Without it the two refusals above pass for
        a version that never lets anything be READY — 'safe' and useless. A
        fleet with no declared tier has nothing to be outside of; that is an
        answer, not an absence."""
        self.assertEqual(self._land_state(("none", "no policy"))["state"],
                         "READY")
        self.assertEqual(self._land_state(("ok", None))["state"], "READY")

    def test_a_RAISING_tier_check_refuses_rather_than_breaking_the_board(self):
        with mock.patch.object(dispatches, "approval_tier_for_verdict",
                               side_effect=RuntimeError("boom")):
            d = self._dispatch(self.head)
            with mock.patch.object(dispatches, "GATE_CAPS", ()):
                dispatches.mark_verdict(d["id"], self.head, "ok",
                                        polarity="approve")
            lr, err = landreq.get(d["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["state"], "REVIEWED")
        # the except supplies its OWN reason; "could not be evaluated" is the
        # fallback for a None why and is never reached here. Asserting the
        # unreachable branch would have passed only if the guard had failed.
        self.assertIn("approval-tier check raised", lr["ungated"])


class LandPathEnforcement(GateBase):
    """The third blocker, and the one that decided the shape of this lane.

    Recording a verdict UNVERIFIED changes nothing about whether the work can
    land: `landreq` derived READY from APPROVE polarity alone, so a brand-new
    untokened approve came back gate=UNVERIFIED **and** land_state=READY. The
    council asked for mechanical enforcement AT THE LAND PATH; marking a claim
    unverified is not enforcing anything.

    Receipt requirements are grandfathered per row and writer capability.
    That exemption does not invent tier authority: evidence-free historical
    verdicts remain pre-tier, while current author-bound fixtures can exercise
    the receipt rule independently.
    """

    def _lr_for(self, evidence, gate_capable=True):
        """Record an approve as a GATE-CAPABLE writer (the default) or as an
        old one, then read the land state it produces."""
        d = self._dispatch(self.head)
        from tests._verdict import native_author
        with mock.patch.object(
                dispatches, "GATE_CAPS",
                (dispatches.GATE_CAP_RECEIPT,) if gate_capable else ()), \
                native_author(self):
            got, err = dispatches.mark_verdict(d["id"], self.head, evidence,
                                               polarity="approve", bind_author=True)
        self.assertIsNone(err, err)
        lr, err = landreq.get(d["id"])
        self.assertIsNone(err, err)
        return lr

    def _dispatch(self, tip):
        os.makedirs(os.environ["HELM_HOME"], exist_ok=True)
        row, err = dispatches.add("reviewer", "a-lane", tip, kind="review",
                                  repo=self.repo, notify=False, _reason=True, new_work=True)
        self.assertIsNone(err, err)
        return row

    def test_an_untokened_APPROVE_does_not_reach_READY(self):
        d = self._dispatch(self.head)
        self.assertTrue(dispatches.eventledger.append(
            dispatches.ledger_path(), {
                "v": 3, "event": "verdict", "seq": d["seq"] + 1,
                "id": d["id"], "ts": dispatches.pk.now_ts(),
                "reviewed_tip": self.head,
                "verdict_ref": "whole-suite Ran 5115 OK",
                "polarity": "approve", "gate": "",
                "gate_caps": [dispatches.GATE_CAP_RECEIPT]}))
        lr, err = landreq.get(d["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertEqual(lr["gate"], "")
        self.assertIn("PRE-TIER", lr["ungated"])
        self.assertEqual(self._requirement(d["id"]), "required")

    def test_a_faulty_VERIFIED_without_token_bind_is_refused_by_the_reader(self):
        """Inject a producer defect at bind, not at recorded tier capture.

        Normal bind cannot return VERIFIED without a token. This tests the
        consumer's independent gate check, not normal producer reachability.
        """
        with serial_process(ran=9):
            receipt, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        honest = self._lr_for(gate.evidence_line(receipt))
        self.assertEqual(honest["state"], "READY")
        self.assertEqual(honest["gate"], receipt["id"])
        capture = dispatches._record_verdict_tier
        with mock.patch.object(gate, "bind", return_value=(
                "VERIFIED", "", "injected missing token")) as binding, \
                mock.patch.object(dispatches, "_record_verdict_tier",
                                  wraps=capture) as retained:
            broken = self._lr_for("injected bind defect")
        binding.assert_called_once()
        retained.assert_called_once()
        raw = dispatches.snapshot()[0][broken["id"]]
        self.assertEqual(raw["verdict_version"], 4)
        self.assertEqual(dispatches.approval_tier_for_verdict(raw), ("none", None))
        self.assertEqual(broken["state"], "REVIEWED")
        self.assertIn("no minted gate receipt", broken["ungated"])

    def test_a_BOUND_approve_reaches_READY(self):  # noqa: VACUOUS_ASSERTION — READY plus the exact bound receipt id positively control the intentional absent ungated reason
        """The control. Without it the test above passes for a version that
        never lets anything be READY."""
        with serial_process(ran=9):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        lr = self._lr_for(gate.evidence_line(row))
        self.assertEqual(lr["state"], "READY")
        self.assertEqual(lr["gate"], row["id"])
        self.assertIsNone(lr["ungated"])

    def test_current_authority_without_receipt_capability_reaches_READY(self):
        """Receipt grandfathering alone does not supply tier authority."""
        lr = self._lr_for("whole-suite Ran 5115 OK", gate_capable=False)
        self.assertEqual(lr["state"], "READY")
        self.assertIsNone(lr["ungated"])

    def test_an_evidence_free_OLD_WRITER_is_pre_tier_not_READY(self):
        """Keep the old producer input: no author/tier evidence is invented."""
        d = self._dispatch(self.head)
        with mock.patch.object(dispatches, "GATE_CAPS", ()):
            got, err = dispatches.mark_verdict(
                d["id"], self.head, "whole-suite Ran 5115 OK",
                polarity="approve", bind_author=False)
        self.assertIsNone(err, err)
        tier, why = dispatches.approval_tier_for_verdict(got)
        self.assertEqual(tier.kind, dispatches.TIER_PRE_TIER)
        lr, err = landreq.get(d["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertIn("PRE-TIER", lr["ungated"])

    def test_ABSENCE_is_legacy_only_BEFORE_the_epoch(self):
        """The design call, and the third time in this lane the answer was
        "stop using a clock". GATE_CAPS stamps the WRITER, which answers "does
        THIS CHECKOUT have the feature?" — and a reviewer running ./bin/helm
        from a worktree that has not rebased answers NO while the fleet answers
        YES. Measured: 9 of 12 live worktrees could not stamp, and
        two verdicts written MINUTES AFTER the gate landed were grandfathered
        into READY carrying no receipt.

        The cutover is the APPEND INDEX of the earliest verdict that carries a
        recognized stamp. Before it, absence is a writer that had no gate.
        At or after it, absence is UNKNOWN — the fleet had the gate by then."""
        req = landreq.gate_requirement
        # no epoch at all: a fleet that has never run a gate-capable writer
        self.assertEqual(req({}, index=5, epoch=None), "none")
        # before the cutover — genuinely legacy
        self.assertEqual(req({}, index=3, epoch=7), "none")
        # AT and AFTER the cutover — the bypass this closes
        self.assertEqual(req({}, index=7, epoch=7), "unknown")
        self.assertEqual(req({}, index=9, epoch=7), "unknown")
        # an unknown position cannot convict: no index is not evidence
        self.assertEqual(req({}, index=None, epoch=7), "none")
        # and a PRESENT stamp is judged on its own, epoch irrelevant
        self.assertEqual(req({"gate_caps": ["receipt-v1"]}, index=1, epoch=7),
                         "required")

    def test_END_TO_END_a_stale_writer_after_the_epoch_never_reaches_READY(self):
        """THE WIRING, not the predicate. The mutation pass caught this gap:
        cutting the epoch out of project()'s call left every unit test green,
        because none of them drove a row with an ABSENT stamp through the real
        projection. That is the whole bypass — a reviewer on a stale worktree
        writes a verdict with no gate_caps and the board reads READY."""
        first = self._dispatch(self.head)
        with mock.patch.object(dispatches, "GATE_CAPS",
                               (dispatches.GATE_CAP_RECEIPT,)):
            _got, err = dispatches.mark_verdict(first["id"], self.head,
                                                "stamped round", polarity="fix")
        self.assertIsNone(err, err)          # founds the epoch

        stale = self._dispatch(self.head)
        # a writer with NO gate_caps at all — the pre-gate code path, appended
        # AFTER the epoch, which is exactly the stale-worktree reviewer
        with mock.patch.object(dispatches, "GATE_CAPS", None), \
                mock.patch.object(dispatches, "GATE_POLICY", None, create=True):
            def _no_caps(rid, tip, ev, polarity=None):
                path = dispatches.ledger_path()
                cur, unavailable = dispatches.snapshot()
                row = cur[rid]
                dispatches.eventledger.append(path, {
                    "v": 3, "event": "verdict", "seq": row["seq"] + 1,
                    "id": rid, "ts": dispatches.pk.now_ts(),
                    "reviewed_tip": tip, "verdict_ref": ev,
                    "polarity": polarity})
            _no_caps(stale["id"], self.head, "unstamped approve", "approve")

        lr, err = landreq.get(stale["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["state"], "REVIEWED")
        # This genuine historical input has two independent absences. The
        # tier refusal wins; its gate requirement must still be UNKNOWN.
        self.assertIn("PRE-TIER", lr["ungated"])
        self.assertEqual(self._requirement(stale["id"]), "unknown")
        raw = dispatches.snapshot()[0][stale["id"]]
        self.assertIn("stamped no gate_caps", landreq._unknown_gate_caps_why(raw))
        self.assertIn("INTACT", landreq._unknown_gate_caps_why(raw))

        # A current, genuinely captured verdict reaches the gate consumer.
        # Observe the epoch passed by project, not a mocked authority answer.
        current = self._lr_for("current no-capability writer", gate_capable=False)
        rows, verdicts, _unavailable = dispatches.snapshot_with_verdicts()
        epoch = dispatches.gate_epoch(rows, verdicts)
        self.assertIsInstance(epoch, int)
        with mock.patch.object(landreq, "gate_requirement",
                               wraps=landreq.gate_requirement) as requirement:
            projected, err = landreq.get(current["id"])
        self.assertIsNone(err, err)
        self.assertEqual(projected["state"], "READY")
        calls = [call for call in requirement.call_args_list
                 if call.args[0].get("id") == current["id"]]
        self.assertTrue(calls, "current authority never reached the gate check")
        self.assertTrue(any(call.kwargs.get("epoch") == epoch and
                            call.kwargs.get("index") == dispatches.verdict_index(
                                verdicts, current["id"]) for call in calls))

    def test_a_LOST_marker_is_UNKNOWN_and_never_re_derived(self):
        """The blocking finding, and its reason was measured not theoretical: replay
        SILENTLY SKIPS complete malformed rows, so one unparseable founder
        makes the epoch recompute to a LATER position and every unstamped
        verdict in between flips from UNKNOWN back to none — retroactively
        authorized by a corrupted byte, with nothing announcing it.

        So the marker is FROZEN on first stamped write and read thereafter. A
        marker that should exist and does not is EPOCH_LOST: total, loud, and
        never a fresh derivation."""
        d = dispatches
        # stamped verdicts on the ledger, no marker -> LOST, not re-derived
        current = {"a": {"gate_caps": ["receipt-v1"]}}
        with mock.patch.object(d, "_read_epoch", return_value=None):
            self.assertEqual(d.gate_epoch(current, _verdicts({"a": 5})),
                             d.EPOCH_LOST)
        # and nothing reaches READY while the boundary is unknown
        self.assertEqual(landreq.gate_requirement({}, index=1,
                                                  epoch=d.EPOCH_LOST), "unknown")
        self.assertEqual(landreq.gate_requirement({}, index=99,
                                                  epoch=d.EPOCH_LOST), "unknown")
        # a fleet that NEVER gated is still honestly legacy, not LOST
        with mock.patch.object(d, "_read_epoch", return_value=None):
            self.assertIsNone(d.gate_epoch({"a": {}}, _verdicts({"a": 5})))
        # AND AN UNREADABLE LEDGER IS LOST, NOT LEGACY. The mutation pass
        # caught this branch untested: returning None there would let an
        # unreadable ledger authorize every unstamped approve on the board —
        # a pass whose input was missing, in the one function whose job is
        # refusing exactly that.
        with mock.patch.object(d, "_read_epoch", return_value=None), \
                mock.patch.object(d, "_snapshot",
                                  return_value=({}, {}, "EIO")):
            self.assertEqual(d.gate_epoch(), d.EPOCH_LOST)

    # ------------------------------------------------------------------
    # THE REPLAYED LEDGER. Everything below drives a REAL ledger through a
    # real removal and a real recreation, because the tests these replace
    # injected a founder->position map and review named exactly that as the
    # gap: an injected map cannot recreate a row, and recreation is the bug.
    # ------------------------------------------------------------------

    def _send(self, key, message="review this"):
        """A dispatch through send(), whose id is DERIVED from
        sender/repo/operation key — so the same key yields the same id, which
        is the whole mechanism of the HIGH finding.

        `new_work=True` satisfies the work-identity requirement and does NOT
        participate in the id: with an EXPLICIT operation key the namespace is
        sender/repo_id/key alone, so recreation under the same key still
        reproduces the same id. Declaring a chain here would say these
        independent founders continue one another, which they do not."""
        os.makedirs(os.environ["HELM_HOME"], exist_ok=True)
        row, why, _sent = dispatches.send("codex-3", "a-lane", message,
                                          self.head, repo=self.repo, key=key,
                                          sign=False, new_work=True)
        self.assertIsNotNone(row, why)
        return row

    def _stamped(self, rid, evidence):
        with mock.patch.object(dispatches, "GATE_CAPS",
                               (dispatches.GATE_CAP_RECEIPT,)):
            _got, err = dispatches.mark_verdict(rid, self.head, evidence,
                                                polarity="fix")
        self.assertIsNone(err, err)

    def _unstamped(self, rid, evidence):
        """The pre-gate writer: a verdict event with NO gate_caps key at all."""
        row = dispatches.snapshot()[0][rid]
        self.assertTrue(dispatches.eventledger.append(
            dispatches.ledger_path(),
            {"v": 3, "event": "verdict", "seq": row["seq"] + 1, "id": rid,
             "ts": dispatches.pk.now_ts(), "reviewed_tip": self.head,
             "verdict_ref": evidence, "polarity": "approve"}),
            "fixture broken: the pre-gate verdict event was REFUSED by the "
            "ledger, so no arm using this helper has the approve it is "
            "measuring against — a bare failure here reads 'False is not "
            "true' and blames the code under test")

    def _marker(self):
        with open(dispatches.epoch_path(), encoding="utf-8") as fh:
            return json.load(fh)

    def _ledger(self):
        with open(dispatches.ledger_path(), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _rewrite(self, rows):
        with open(dispatches.ledger_path(), "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def _requirement(self, rid):
        """What the REAL projection decides about one row's missing stamp."""
        current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable, unavailable)
        return landreq.gate_requirement(
            current[rid], index=dispatches.verdict_index(verdicts, rid),
            epoch=dispatches.gate_epoch(current, verdicts))

    def test_a_RECREATED_row_with_the_SAME_ID_cannot_move_the_epoch(self):
        """The HIGH finding, replayed exactly as reported.

            founder verdict index 2, later unstamped approval index 5 =>
            UNKNOWN; remove founder rows while retaining marker, retry the same
            operation key (same id), verdict it at index 5 while stale is now
            index 2 => gate_epoch=5 and the stale approval flips to none.

        The marker froze the dispatch WORK-ITEM ID because that id survives
        compaction. But `send()` DERIVES that id from sender/repo/operation
        key, so it also survives DELETION: retry the same operation and the
        same id comes back, at a later position, with the marker following it
        there — and every unstamped approval the boundary stepped over is
        retroactively authorized. Fail-open, which is what makes it a blocker.

        A work-item id says WHICH LOOP. The epoch is an EVENT, so the anchor is
        over the accepted verdict event's content: a legitimately recreated row
        is a DIFFERENT event, and it does not satisfy it."""
        founder = self._send("op-FOUNDER", "founding review")
        self._stamped(founder["id"], "stamped founding round")
        stale = self._send("op-STALE", "the stale one")
        self._unstamped(stale["id"], "unstamped approve")

        marker = self._marker()
        self.assertEqual(marker["founder"], founder["id"])
        self.assertEqual(marker["index"], 2)            # the reported index 2
        _c, verdicts, _u = dispatches.snapshot_with_verdicts()
        self.assertEqual(dispatches.verdict_index(verdicts, stale["id"]), 5)
        self.assertEqual(self._requirement(stale["id"]), "unknown")

        # remove the founder's rows, RETAIN the marker
        self._rewrite([r for r in self._ledger() if r.get("id") != founder["id"]])
        _c, verdicts, _u = dispatches.snapshot_with_verdicts()
        self.assertEqual(dispatches.verdict_index(verdicts, stale["id"]), 2)

        # ...and retry the same operation key. This is a LEGITIMATE retry, not
        # a forgery: helm hands the same id back by design.
        again = self._send("op-FOUNDER", "founding review")
        self.assertEqual(again["id"], founder["id"])    # retry_same_id True
        self._stamped(again["id"], "a second stamped round")
        _c, verdicts, _u = dispatches.snapshot_with_verdicts()
        self.assertEqual(dispatches.verdict_index(verdicts, founder["id"]), 5)

        # THE ASSERTION. Before the anchor this was epoch=5 and "none".
        self.assertEqual(dispatches.gate_epoch(), dispatches.EPOCH_LOST)
        self.assertEqual(self._requirement(stale["id"]), "unknown")
        lr, err = landreq.get(stale["id"])
        self.assertIsNone(err, err)
        self.assertNotEqual(lr["state"], "READY")

    def test_the_marker_survives_a_REAL_COMPACTION_of_earlier_rows(self):
        """The control for the test above, and it must stay green or the fix is
        just "refuse everything". Removing rows BEFORE the founder moves its
        position and changes nothing about its identity, so the boundary
        follows it down rather than being stranded at a stale number.

        The version this replaces injected {founder: 5} and asserted 5 — which
        is a test of dict.get, not of compaction."""
        early = self._send("op-EARLY", "an earlier loop")
        self._unstamped(early["id"], "old unstamped approve")
        founder = self._send("op-FOUNDER", "founding review")
        self._stamped(founder["id"], "stamped founding round")
        marker = self._marker()
        self.assertEqual(marker["index"], 5)
        self.assertEqual(dispatches.gate_epoch(), 5)
        # the early loop is compacted away — three rows vanish from BEFORE the
        # founder, and the frozen 5 is now a position the founder does not hold
        self._rewrite([r for r in self._ledger() if r.get("id") != early["id"]])
        self.assertEqual(dispatches.gate_epoch(), 2)
        self.assertNotEqual(dispatches.gate_epoch(), marker["index"])

    def test_a_founder_compacted_AWAY_is_EPOCH_LOST(self):
        """We cannot place any row against a boundary we can no longer locate,
        and re-deriving one is the retroactive authorization this mechanism
        exists to prevent."""
        founder = self._send("op-FOUNDER", "founding review")
        self._stamped(founder["id"], "stamped founding round")
        stale = self._send("op-STALE", "the stale one")
        self._unstamped(stale["id"], "unstamped approve")
        self.assertEqual(dispatches.gate_epoch(), 2)
        self._rewrite([r for r in self._ledger() if r.get("id") != founder["id"]])
        self.assertEqual(dispatches.gate_epoch(), dispatches.EPOCH_LOST)
        self.assertEqual(self._requirement(stale["id"]), "unknown")
        # and an unreadable ledger under a valid marker is LOST too, never the
        # stale stored index
        with mock.patch.object(dispatches, "_snapshot",
                               return_value=({}, {}, "EIO")):
            self.assertEqual(dispatches.gate_epoch(), dispatches.EPOCH_LOST)

    def test_the_marker_is_FROZEN_and_never_overwritten(self):
        """A marker that already exists IS the answer, even when today's ledger
        would compute a different one. That is the whole point: a number no
        longer being calculated cannot be moved by corrupting its inputs."""
        d = dispatches
        os.makedirs(os.path.dirname(d.epoch_path()), exist_ok=True)
        current = {"aaaaaaaa": {"gate_caps": ["receipt-v1"]}}
        five = _verdicts({"aaaaaaaa": 5})
        self.assertIsNotNone(d.record_gate_epoch(current, five))
        self.assertEqual(d.gate_epoch(current, five), 5)
        # A LATER, EARLIER-POSITIONED founder appears. The marker does NOT move
        # to it — a marker that exists IS the answer, even when today's ledger
        # would compute a different one.
        both = {"aaaaaaaa": {"gate_caps": ["receipt-v1"]},
                "bbbbbbbb": {"gate_caps": ["receipt-v1"]}}
        pair = _verdicts({"aaaaaaaa": 5, "bbbbbbbb": 2})
        self.assertIsNone(d.record_gate_epoch(both, pair))
        self.assertEqual(d.gate_epoch(both, pair), 5)
        # and the position is DERIVED from the frozen founder, so compacting
        # the list moves the answer with it rather than stranding it
        self.assertEqual(d.gate_epoch(current, _verdicts({"aaaaaaaa": 1})), 1)

    def test_the_epoch_is_the_EARLIEST_stamped_verdict(self):
        """Not the latest, and not a timestamp. Append order is the only clock
        this needs, and the ledger is append-only so it cannot drift."""
        ep = lambda c, o: dispatches._scan_epoch(c, _verdicts(o))[0]
        self.assertEqual(ep({"a": {"gate_caps": ["receipt-v1"]},
                             "b": {},
                             "c": {"gate_caps": ["receipt-v1"]}},
                            {"a": 10, "b": 4, "c": 22}), 10)
        # a fleet with no stamped verdict has NO epoch — everything is legacy,
        # which is the true reading and not a default
        self.assertIsNone(ep({"b": {}}, {"b": 4}))

    def test_only_the_RECEIPT_capability_founds_the_epoch(self):
        """Both of these were reproduced founding an epoch in the first draft.
        A writer that never advertised the capability cannot mark the moment
        the fleet gained it — an EMPTY set and an unrelated one are honest
        writers WITHOUT the receipt capability, not evidence of it."""
        ep = lambda c, o: dispatches._scan_epoch(c, _verdicts(o))[0]
        self.assertIsNone(ep({"a": {"gate_caps": []}}, {"a": 3}))
        self.assertIsNone(ep({"a": {"gate_caps": ["other-cap"]}}, {"a": 3}))
        # ...and they do not BLOCK a real founder further along either
        self.assertEqual(ep({"a": {"gate_caps": []},
                             "b": {"gate_caps": ["receipt-v1"]}},
                            {"a": 3, "b": 9}), 9)

    def test_a_MALFORMED_founder_fails_closed_AT_ITS_OWN_POSITION(self):
        """The first draft SKIPPED a corrupt stamp, which moved the boundary
        LATER and laundered every unstamped approval between the corrupt
        founder and the next valid receipt into legacy/READY.

        Skipping past a thing you cannot read is the `.get()` collapse one
        layer up: an unreadable value treated as an absent one. We cannot know
        what that writer could do, so the boundary starts THERE."""
        ep = lambda c, o: dispatches._scan_epoch(c, _verdicts(o))[0]
        current = {"bad": {"gate_caps": "not-a-list"},
                   "good": {"gate_caps": ["receipt-v1"]}}
        # corrupt at 4, real receipt at 11 -> the boundary is 4, NOT 11
        self.assertEqual(ep(current, {"bad": 4, "good": 11}), 4)
        # so an unstamped verdict at 7 — between them — is UNKNOWN, not legacy
        self.assertEqual(landreq.gate_requirement({}, index=7, epoch=4),
                         "unknown")
        # and a corrupt stamp alone still founds, rather than leaving no epoch
        self.assertEqual(ep({"bad": {"gate_caps": 7}}, {"bad": 2}), 2)

    def test_EVERY_authoritative_marker_FIELD_is_validated(self):
        """The MED finding. The first version validated `index` and nothing else,
        so `founder: []` was accepted as a marker and `verdict_order.get([])`
        raised TypeError — out of a function whose entire contract is to answer
        "may this land?" without ever raising.

        ONE FIELD WRONG AT A TIME, so each check is separately measurable: a
        row malformed in two places would stay refused when either check is
        reverted, and neither would then be bound to anything."""
        d = dispatches
        os.makedirs(os.path.dirname(d.epoch_path()), exist_ok=True)
        good = {"v": d.EPOCH_V, "index": 2, "founder": "a" * 32,
                "anchor": "b" * 32, "ts": "2026-07-31T00:00:00Z"}
        verdicts = {"a" * 32: (2, "b" * 32)}
        current = {"a" * 32: {"gate_caps": ["receipt-v1"]}}

        def epoch_for(marker):
            with open(d.epoch_path(), "w", encoding="utf-8") as fh:
                json.dump(marker, fh)
            return d.gate_epoch(current, verdicts)

        self.assertEqual(epoch_for(good), 2)            # the control
        for field, junk in (("v", d.LEGACY_EPOCH_V), ("v", "2"), ("v", True),
                            ("v", None),
                            ("index", -1), ("index", "2"), ("index", True),
                            ("index", None),
                            ("founder", []), ("founder", {}), ("founder", 7),
                            ("founder", None), ("founder", "not-hex!"),
                            ("founder", "abc"),
                            ("anchor", []), ("anchor", None), ("anchor", 7),
                            ("anchor", "b" * 31), ("anchor", "zz" * 16),
                            ("ts", 5), ("ts", None), ("ts", "yesterday")):
            self.assertEqual(epoch_for(dict(good, **{field: junk})),
                             d.EPOCH_LOST, "%s=%r" % (field, junk))
        for missing in ("v", "index", "founder", "anchor", "ts"):
            self.assertEqual(
                epoch_for({k: v for k, v in good.items() if k != missing}),
                d.EPOCH_LOST, "missing " + missing)

    def test_a_marker_PRESENT_but_unreadable_is_LOST_never_ABSENT(self):
        """The same three states as `gate_caps`, one layer up. A marker file
        that exists and cannot be parsed is not an absent one: SOMETHING froze
        a boundary here, so falling through to a fresh derivation re-authorizes
        exactly what the unreadable marker was refusing.

        `pk.read_json` collapses missing and corrupt into its default, which is
        how the distinction was lost the first time."""
        d = dispatches
        os.makedirs(os.path.dirname(d.epoch_path()), exist_ok=True)
        current = {"a" * 32: {"gate_caps": ["receipt-v1"]}}
        verdicts = {"a" * 32: (2, "b" * 32)}
        for junk in ("{not json", "", "[]", "null", '"a string"', "7"):
            with open(d.epoch_path(), "w", encoding="utf-8") as fh:
                fh.write(junk)
            self.assertEqual(d.gate_epoch(current, verdicts), d.EPOCH_LOST, junk)
        # ABSENT is different, and is NOT the same answer: with no stamped
        # verdict anywhere there is honestly nothing to freeze
        os.remove(d.epoch_path())
        self.assertIsNone(d.gate_epoch({"a" * 32: {}},
                                       {"a" * 32: (2, "b" * 32)}))

    def test_a_boundary_that_cannot_be_PROVEN_is_never_frozen(self):
        """Freezing a founder whose event we cannot hash would recreate the
        exact state this version ends: a marker that names a founder and cannot
        say which event it is."""
        d = dispatches
        os.makedirs(os.path.dirname(d.epoch_path()), exist_ok=True)
        current = {"a" * 32: {"gate_caps": ["receipt-v1"]}}
        self.assertIsNone(d.record_gate_epoch(current, {"a" * 32: (5, None)}))
        self.assertFalse(os.path.exists(d.epoch_path()))

    def test_a_v1_marker_is_UPGRADED_and_its_founder_never_moves(self):
        """The migration, and it must not become a re-derivation. A v1 marker
        froze a founder id with no proof of which event it names. The upgrade
        carries that id over verbatim and adds only the anchor: a fresh scan
        would pick today's earliest stamped verdict, which after any compaction
        is a LATER row — the retroactive authorization this file prevents.

        Until it is upgraded the boundary is unprovable, so it reads LOST."""
        d = dispatches
        early = self._send("op-EARLY", "an earlier loop")
        self._stamped(early["id"], "the FIRST stamped round")
        self.assertEqual(d.gate_epoch(), 2)

        def rollback():                 # a v1 marker: frozen founder, no proof
            with open(d.epoch_path(), "w", encoding="utf-8") as fh:
                json.dump({"v": d.LEGACY_EPOCH_V, "index": 2,
                           "founder": early["id"],
                           "ts": "2026-07-31T00:00:00Z"}, fh)

        rollback()
        self.assertEqual(d.gate_epoch(), d.EPOCH_LOST)
        self.assertIsNotNone(d.record_gate_epoch())
        upgraded = self._marker()
        self.assertEqual(upgraded["v"], d.EPOCH_V)
        self.assertEqual(upgraded["founder"], early["id"])
        self.assertEqual(d.gate_epoch(), 2)

        # AND THE HALF THAT ACTUALLY BINDS IT. Above, a fresh re-derivation
        # would have picked `early` too, so it proves nothing about which rule
        # ran — the mutation pass caught exactly that and this second half is
        # the repair. Compact the frozen founder away and add a LATER stamped
        # verdict: re-deriving now lands on `later`, moving the boundary
        # forward and re-authorizing everything between. The upgrade must
        # refuse instead, because a founder it cannot find is one it cannot
        # prove.
        later = self._send("op-LATER", "a later loop")
        self._stamped(later["id"], "a later stamped round")
        stale = self._send("op-STALE", "the stale one")
        self._unstamped(stale["id"], "unstamped approve")
        self._rewrite([r for r in self._ledger() if r.get("id") != early["id"]])
        rollback()
        self.assertIsNone(d.record_gate_epoch())
        self.assertEqual(self._marker()["v"], d.LEGACY_EPOCH_V)   # untouched
        self.assertEqual(d.gate_epoch(), d.EPOCH_LOST)
        self.assertEqual(self._requirement(stale["id"]), "unknown")

    def test_an_UNREADABLE_marker_is_repaired_by_a_human_never_overwritten(self):
        """Overwriting a marker we cannot read is indistinguishable from
        overwriting one we can — and that is a re-derivation with the audit
        trail deleted."""
        d = dispatches
        founder = self._send("op-FOUNDER", "founding review")
        self._stamped(founder["id"], "stamped founding round")
        with open(d.epoch_path(), "w", encoding="utf-8") as fh:
            fh.write("{corrupt")
        self.assertIsNone(d.record_gate_epoch())
        with open(d.epoch_path(), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{corrupt")
        self.assertEqual(d.gate_epoch(), d.EPOCH_LOST)

    def test_the_ANCHOR_ignores_re_serialization_and_nothing_else(self):
        """The two properties the frozen founder needs, asserted directly.
        Key order and whitespace must not matter — a compaction rewrites the
        file — while any VALUE change must, including the stamp that made this
        row the founder in the first place."""
        anchor = dispatches.verdict_anchor
        event = {"v": 3, "event": "verdict", "seq": 2, "id": "a" * 32,
                 "ts": "2026-07-31T00:00:00Z", "reviewed_tip": self.head,
                 "verdict_ref": "round one", "polarity": "fix", "gate": "",
                 "gate_caps": ["receipt-v1"]}
        base = anchor(event)
        self.assertEqual(base, anchor(dict(reversed(list(event.items())))))
        self.assertEqual(base, anchor(json.loads(json.dumps(event))))
        for field, changed in (("ts", "2026-07-31T00:00:01Z"),
                               ("verdict_ref", "round two"),
                               ("polarity", "approve"), ("gate", "abc123"),
                               ("seq", 3), ("id", "b" * 32),
                               ("reviewed_tip", "c" * 40),
                               ("gate_caps", [])):
            self.assertNotEqual(base, anchor(dict(event, **{field: changed})),
                                field)
        self.assertNotEqual(base, anchor({k: v for k, v in event.items()
                                          if k != "gate_caps"}))
        self.assertIsNone(anchor("not a dict"))
        self.assertIsNone(anchor(None))

    def test_project_DEGRADES_to_EPOCH_LOST_when_the_epoch_read_THROWS(self):
        """The gate_epoch call sat OUTSIDE project()'s per-row try, so
        a marker holding `founder: []` raised TypeError and took the WHOLE
        board with it — every land loop gone, where one refused row was the
        designed worst case.

        Bound to a THROWN epoch rather than to a malformed marker on purpose:
        gate_epoch validates its own marker now, so a marker-shaped test would
        be caught by the validator and this wall would never be measured."""
        first = self._send("op-FOUNDER", "founding review")
        self._stamped(first["id"], "stamped founding round")
        stale = self._send("op-STALE", "the stale one")
        self._unstamped(stale["id"], "unstamped approve")
        current = self._lr_for("current authority", gate_capable=False)
        with mock.patch.object(dispatches, "gate_epoch",
                               side_effect=RuntimeError("marker exploded")), \
                mock.patch.object(landreq, "gate_requirement",
                                  wraps=landreq.gate_requirement) as requirement:
            out, unavailable = landreq.project()
        self.assertIsNone(unavailable, unavailable)
        self.assertIn(stale["id"], out)             # the board SURVIVED
        self.assertEqual(out[stale["id"]]["state"], "REVIEWED")
        self.assertIn("PRE-TIER", out[stale["id"]]["ungated"])
        # Historical absence stays nonauthorizing, independently of the epoch.
        raw = dispatches.snapshot()[0][stale["id"]]
        self.assertEqual(landreq.gate_requirement(
            raw, index=10, epoch=dispatches.EPOCH_LOST), "unknown")
        # Current recorded authority reaches the actual gate predicate. The
        # projection must pass LOST, not silently replace the exception by None.
        calls = [call for call in requirement.call_args_list
                 if call.args[0].get("id") == current["id"]]
        self.assertTrue(calls, "current authority never reached the gate check")
        self.assertTrue(any(call.kwargs.get("epoch") == dispatches.EPOCH_LOST
                            for call in calls))
        self.assertEqual(out[current["id"]]["state"], "READY")

    def test_the_requirement_reads_NAMED_CAPABILITIES_in_three_states(self):
        """MUTATION-BOUND against every design this went through, because each
        one shipped a defect the next one found.

        A wall-time boundary could not say whether a receipt was OBTAINABLE —
        reading the real ledger found verdicts stamped after the
        boundary that were written while the feature was still unlanded. An
        integer policy then invited `2` to mean "newer" and "stricter" at once,
        so `>= 1` would accept a row that never met the stricter rule. And the
        third state is the one that decides the lane: a field PRESENT but
        unreadable must be UNKNOWN, never quietly demoted to "old writer"."""
        req = landreq.gate_requirement
        self.assertEqual(req({}), "none")                       # old writer
        self.assertEqual(req(None), "none")                     # no row at all
        # PRESENT-BUT-NULL is the shape grammar-fuzzing found in this: it
        # is UNKNOWN, and `.get()` used to hand it back identically to an
        # ABSENT key, silencing the requirement. One null would have laundered
        # an ungated approve into READY.
        self.assertEqual(req({"gate_caps": None}), "unknown")
        self.assertEqual(req({"gate_caps": []}), "none")        # honest, none
        self.assertEqual(req({"gate_caps": ["other-cap"]}), "none")
        self.assertEqual(req({"gate_caps": ["receipt-v1"]}), "required")
        self.assertEqual(req({"gate_caps": ("receipt-v1", "x")}), "required")
        # PRESENT but unreadable -> UNKNOWN, never "none"
        for junk in ("receipt-v1", 1, True, {"receipt-v1": 1}, [1], [None],
                     ["Receipt-V1 bad!"], dispatches.GATE_CAPS_UNKNOWN):
            self.assertEqual(req({"gate_caps": junk}), "unknown", junk)

    def test_a_CORRUPT_capability_stamp_never_reaches_READY(self):  # noqa: VACUOUS_ASSERTION — the same row is first proven READY with its exact bound receipt before its capability stamp is corrupted
        """Even beside a VALID bound receipt. A row whose stamp cannot be read
        cannot say which rules its approval was written under, so no reading of
        the receipt next to it is trustworthy either — repair the row, do not
        route around it."""
        with serial_process(ran=9):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        d = self._dispatch(self.head)
        from tests._verdict import native_author
        with native_author(self):
            got, err = dispatches.mark_verdict(d["id"], self.head,
                                               gate.evidence_line(row),
                                               polarity="approve", bind_author=True)
        self.assertIsNone(err, err)
        self.assertEqual(got["gate"], row["id"])        # a REAL bound receipt
        self.assertEqual(dispatches.approval_tier_for_verdict(got), ("none", None))
        self.assertEqual(landreq.get(d["id"])[0]["state"], "READY")
        path = dispatches.ledger_path()
        with open(path, encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh if l.strip()]
        for r in rows:
            if r.get("event") == "verdict":
                r["gate_caps"] = "not-a-list"
        with open(path, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        lr, err = landreq.get(d["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["state"], "REVIEWED")
        raw = dispatches.snapshot()[0][d["id"]]
        tier, why = dispatches.approval_tier_for_verdict(raw)
        self.assertEqual(tier.kind, dispatches.TIER_DAMAGED)
        self.assertIn("does not bind this verdict", why)
        self.assertIn("does not bind this verdict", lr["ungated"])
        self.assertEqual(landreq.gate_requirement(raw), "unknown")
        self.assertIn("unreadable", landreq._unknown_gate_caps_why(raw))


# A real discoverable probe whose verdict is decided by a sibling data file —
# the same shape as the 2026-07-31 incident, where a guard test's outcome was
# decided by tree content the lane never touched.
_PROBE_SRC = """import unittest

class StaleProbe(unittest.TestCase):
    def test_probe(self):
        with open("app.txt") as fh:
            got = fh.read().strip()
        self.assertEqual(got, %r)
"""

_PROBE_ID = "tests.test_stale_probe.StaleProbe.test_probe"

# The dual-cause shape: ONE untouched test, TWO
# independent reasons to fail — cause A decided by app.txt (the base's),
# cause B decided by poison.txt (the lane's). Trunk fixes only A.
_DUAL_PROBE_SRC = """import os
import unittest

class StaleProbe(unittest.TestCase):
    def test_probe(self):
        with open("app.txt") as fh:
            self.assertEqual(fh.read().strip(), "v2")
        self.assertFalse(os.path.exists("poison.txt"))
"""

# An INTERACTION probe: red only when the lane's code.txt meets the OLD
# base's app.txt — green at the base alone, green on trunk, green with the
# lane applied to trunk. The reproduction guard exists for exactly this.
_INTERACTION_PROBE_SRC = """import os
import unittest

class StaleProbe(unittest.TestCase):
    def test_probe(self):
        with open("app.txt") as fh:
            app = fh.read().strip()
        if os.path.exists("code.txt"):
            self.assertNotEqual(app, "v1")
"""


# The false-exculpation shapes. lane/gemini-window-750k was told
# "the failing tests ALSO fail on current trunk 5ea88c0deaa9" and two of those
# failures were then run at 5ea88c0 by hand and PASSED: the trunk leg read the
# reference run's EXIT STATUS and never asked which ids failed there. These
# three sources stage the ways a trunk run can end non-zero without the
# failures under investigation being trunk's at all.
#
# (1) TWO failing tests, of which trunk fixes only one.
_SPLIT_PROBE_SRC = """import unittest

class StaleProbe(unittest.TestCase):
    def test_probe(self):
        with open("app.txt") as fh:
            self.assertEqual(fh.read().strip(), "v2")

    def test_other(self):
        with open("other.txt") as fh:
            self.assertEqual(fh.read().strip(), "v2")
"""

_OTHER_ID = "tests.test_stale_probe.StaleProbe.test_other"

# (2) trunk renamed the failing test out of existence, so `python -m unittest
# <id>` there mints a `unittest.loader._FailedTest` and exits non-zero without
# the id under investigation ever running. This is the incident's own shape.
_RENAMED_PROBE_SRC = """import unittest

class StaleProbe(unittest.TestCase):
    def test_probe_renamed_by_trunk(self):
        with open("app.txt") as fh:
            self.assertEqual(fh.read().strip(), "v2")
"""

# (3) the TRUE case that a naive `ids & failed` would lose: on trunk the class
# fixture explodes, so every test under it is red there while unittest prints
# ONE header named setUpClass.
_FIXTURE_PROBE_SRC = """import os
import unittest

class StaleProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists("trunkonly.txt"):
            raise RuntimeError("this fixture cannot run on trunk")

    def test_probe(self):
        with open("app.txt") as fh:
            self.assertEqual(fh.read().strip(), "v2")
"""


class StaleBaseFixture(GateBase):
    """The git world a STALE BASE verdict is read in: `_seed_main` gives
    MAIN an app.txt and a real probe suite over it, `_lane` forks a lane
    that touches only code.txt, and `_advance_main` moves trunk on after the
    fork. `_write` and `_commit` are the primitives under them.

    THIS CLASS HOLDS NO `test_*` METHOD, and that is its contract. unittest
    collects every inherited `test_*` again under each subclass's own id, so
    an arm written here runs once per subclass. A class that needs this
    fixture subclasses THIS class; its arms go in the subclass."""

    def _write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def _commit(self, msg):
        self._git("add", "-A")
        self._git("commit", "-qm", msg)
        return self._git("rev-parse", "HEAD")

    def _seed_main(self, expect, app="v1"):
        """MAIN gets app.txt plus a real probe suite asserting its content."""
        self._git("checkout", "-q", "main")
        self._write("app.txt", app + "\n")
        self._write(os.path.join("tests", "__init__.py"), "")
        self._write(os.path.join("tests", "test_stale_probe.py"),
                    _PROBE_SRC % expect)
        return self._commit("probe suite")

    def _lane(self, name="lane/stale"):
        """A lane off the CURRENT main tip, touching only code.txt."""
        self._git("checkout", "-q", "-b", name)
        self._write("code.txt", "lane work\n")
        return self._commit("lane change")

    def _advance_main(self, app=None, note="drift"):
        """Trunk moves on after the fork; the checkout returns to the lane."""
        lane = self._git("rev-parse", "--abbrev-ref", "HEAD")
        self._git("checkout", "-q", "main")
        self._write("app.txt" if app is not None else "elsewhere.txt",
                    (app if app is not None else note) + "\n")
        sha = self._commit(note)
        self._git("checkout", "-q", lane)
        return sha


class StaleBase(StaleBaseFixture):
    """A FAILED receipt now says WHOSE failure it is — and every verdict here
    is asserted off the stored field and the evidence text, never off the
    absence of a complaint. The negative controls are the point: each one is
    a way a wrong STALE BASE would tell an author to ignore a failure they
    own.

    A subclass of THIS class runs every arm below again under its own id,
    which is right only for one that changes the fixture those arms read. A
    class that only needs the fixture subclasses StaleBaseFixture."""

    def test_a_failure_the_base_owns_is_named_STALE_BASE(self):
        """The incident, reproduced: red at the fork, fixed on trunk since,
        lane touched neither the test nor its data — and the verdict still
        AUTHORIZES nothing."""
        base = self._seed_main(expect="v2", app="v1")   # red at the base
        self._lane()
        trunk = self._advance_main(app="v2", note="trunk fixes the data")
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.STALE_BASE)
        self.assertEqual(check["trunk"], trunk)
        self.assertEqual(check["merge_base"], base)
        self.assertEqual(check["failing_files"],
                         ["tests/test_stale_probe.py"])
        self.assertIn("rebase", check["reason"])
        # The causal leg RAN and is part of the claim, not an inference.
        self.assertIn("STAY green", check["reason"])
        self.assertIn("STALE BASE", gate.evidence_line(row))
        # SAYS, never AUTHORIZES: the run did not go green, so the binding
        # refuses exactly as it would have before this verdict existed.
        state, rid, _why = gate.bind(gate.evidence_line(row), row["head"])
        self.assertEqual(state, "REFUSED")
        self.assertEqual(rid, row["id"])

    def test_a_failure_in_the_lanes_own_diff_stays_plain_FAILED(self):
        """Negative control (a): the failing test file is IN the diff. The
        pre-166 early return said LANE_OWNED from the overlap alone; the
        causal ladder answers the same attribution by MEASUREMENT — the
        lane's rewrite fails on trunk too, so NOT_STALE with the lane named
        as owner. No STALE_BASE, no extra authorization."""
        self._seed_main(expect="v1", app="v1")          # green base
        self._git("checkout", "-q", "-b", "lane/owns")
        self._write(os.path.join("tests", "test_stale_probe.py"),
                    _PROBE_SRC % "v9")
        self._commit("lane rewrites the probe")
        self._advance_main(note="unrelated trunk drift")
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.NOT_STALE)
        self.assertIn(_PROBE_ID, check["reason"])
        self.assertNotIn("STALE BASE", gate.evidence_line(row))
        self.assertNotIn("base-check UNKNOWN", gate.evidence_line(row))

    def test_an_UNCOMMITTED_edit_to_the_failing_test_is_lane_owned(self):
        """The changed set reads the worktree, not just the commits — a dirty
        edit to the failing test cannot hide behind a clean branch diff.
        The causal ladder still measures it: the dirty state fails with the
        lane applied, so NOT_STALE, and the dirty-tree caution rides."""
        self._seed_main(expect="v1", app="v1")
        self._lane()
        self._advance_main(note="unrelated trunk drift")
        self._write(os.path.join("tests", "test_stale_probe.py"),
                    _PROBE_SRC % "v9")                  # dirty, never committed
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.NOT_STALE)
        self.assertIn(_PROBE_ID, check["reason"])
        self.assertIn("DIRTY", check["reason"])

    def test_a_NONCAUSAL_same_file_touch_earns_STALE_BASE(self):
        """task/166's distinguishing repro: red at the base, trunk fixes it,
        and the lane touches the SAME FILE but only a comment — no test body.
        The pre-166 early return said LANE_OWNED and stopped the author; the
        ladder now runs the legs, and the verdict is STALE_BASE with the
        overlap carried as a caution, not a cause."""
        self._seed_main(expect="v2", app="v1")          # red at the base
        self._git("checkout", "-q", "-b", "lane/comment")
        with open(os.path.join(self.repo, "tests", "test_stale_probe.py"),
                  "a") as fh:
            fh.write("\n# a comment the lane adds — no test body touched\n")
        self._commit("lane adds a comment to the probe file")
        trunk = self._advance_main(app="v2", note="trunk fixes the data")
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.STALE_BASE, check["reason"])
        self.assertEqual(check["trunk"], trunk)
        self.assertIn("caution, not a cause", check["reason"])
        self.assertIn("rebase", check["reason"])

    def test_a_lane_edit_to_the_FAILING_TEST_body_can_never_read_STALE_BASE(self):
        """The hunk guard, direct: the causal legs CANNOT see this case —
        a lane that edits the failing test's own body while the base owns
        the failure. The run-level fixture cannot stage it (an edit strong
        enough to mask makes the suite green and no base_check fires at
        all — measured), so the guard is pinned at the _base_check layer
        where the legs' outputs are the inputs: every leg green/readable,
        the lane's diff touching the failing test's hunk => NOT_STALE,
        never STALE_BASE, and the reason names the edit."""
        base = self._seed_main(expect="v2", app="v1")   # red at the base
        self._git("checkout", "-q", "-b", "lane/masker")
        self._write(os.path.join("tests", "test_stale_probe.py"),
                    _PROBE_SRC % "v2" + "\n# lane adds an assertion-free "
                    "helper the probe never calls\n"
                    "def _lane_helper():\n    return 1\n")
        self._commit("lane touches the failing file")
        trunk = self._advance_main(app="v2", note="trunk fixes the data")
        git = gate.vcs.backend(self.repo)
        mb = self._git("merge-base", "main", "HEAD").strip().lower()
        files = {_PROBE_ID: "tests/test_stale_probe.py"}
        # direct: lane added a NON-test def to the failing file — the hunk
        # guard must NOT fire on a helper the probe never calls...
        touched = gate._lane_touched_test_ids(
            self.repo, git, mb, files, {_PROBE_ID})
        self.assertEqual(touched, set())
        # ...and MUST fire when the probe body itself is edited. The base
        # probe already asserts "v2", so a rewrite to the same text is an
        # EMPTY commit with no hunk — the guard must not fire on it, and
        # the fixture only discriminates if the edit lands INSIDE the def.
        self._write(os.path.join("tests", "test_stale_probe.py"),
                    _PROBE_SRC % "v2")
        self._commit("content-identical rewrite — no hunk")
        self.assertEqual(gate._lane_touched_test_ids(
            self.repo, git, mb, files, {_PROBE_ID}), set())
        import re as _re
        path = os.path.join(self.repo, "tests", "test_stale_probe.py")
        with open(path) as fh:
            src = fh.read()
        src = src.replace("got = fh.read().strip()",
                          "got = fh.read().strip().lower()")
        with open(path, "w") as fh:
            fh.write(src)
        self._commit("lane edits inside test_probe's body")
        touched = gate._lane_touched_test_ids(
            self.repo, git, mb, files, {_PROBE_ID})
        self.assertEqual(touched, {_PROBE_ID})

    def test_a_failure_that_ALSO_fails_on_trunk_is_not_laundered(self):
        """Negative control (b): a red trunk is a red trunk. Calling it a
        stale base would tell the whole fleet 'not your problem'."""
        self._seed_main(expect="v2", app="v1")          # red then, red now
        self._lane()
        self._advance_main(note="unrelated trunk drift")   # trunk still red
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.NOT_STALE)
        self.assertIn("ALSO fails on current trunk", check["reason"])
        # NAMED, not counted: the claim is about THESE ids, so
        # the receipt has to be able to show them.
        self.assertIn(_PROBE_ID, check["reason"])
        self.assertNotIn("STALE BASE", gate.evidence_line(row))

    def test_only_the_failures_trunk_ACTUALLY_reproduces_are_called_trunks(self):
        """THE FALSE EXCULPATION. Two failing tests; trunk has
        since fixed exactly one of them. The old trunk leg saw a non-zero
        reference run and told the author both were trunk's — the one message
        that makes an author stop investigating a failure that is really
        theirs (here: the base's, and only a rebase fixes it). The verdict is
        still NOT_STALE, because a rebase onto a trunk that is red for the
        OTHER test cannot make this lane green, but the split is now said."""
        self._git("checkout", "-q", "main")
        self._write("app.txt", "v1\n")                  # red at the base
        self._write("other.txt", "v1\n")                # red at the base too
        self._write(os.path.join("tests", "__init__.py"), "")
        self._write(os.path.join("tests", "test_stale_probe.py"),
                    _SPLIT_PROBE_SRC)
        self._commit("probe suite, two failing tests")
        self._lane("lane/split")                        # touches code.txt only
        self._advance_main(app="v2", note="trunk fixes ONE of the two")
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.NOT_STALE)
        # The test trunk really does own is named as trunk's...
        self.assertIn(_OTHER_ID, check["reason"])
        # ...and the one it does NOT is named as still the author's, which is
        # exactly the sentence the old code could not say.
        self.assertIn("%s did NOT fail there" % _PROBE_ID, check["reason"])
        self.assertIn("still this lane's to answer for", check["reason"])
        self.assertNotIn("STALE BASE", gate.evidence_line(row))

    def test_a_test_trunk_cannot_even_RUN_is_never_reported_as_red_trunk(self):
        """The incident's own mechanism: trunk renamed the failing test away,
        so the reference run exits non-zero on a `_FailedTest` while the id
        under investigation never executes there. Exit status alone called
        that a red trunk. There is no evidence either way now, and UNKNOWN in
        words is what the author gets — an honest 'could not determine' beats
        a confident exculpation every time."""
        self._seed_main(expect="v2", app="v1")          # red at the base
        self._lane("lane/renamed-away")
        self._git("checkout", "-q", "main")
        self._write("app.txt", "v2\n")
        self._write(os.path.join("tests", "test_stale_probe.py"),
                    _RENAMED_PROBE_SRC)
        self._commit("trunk renames the probe out from under the lane")
        self._git("checkout", "-q", "lane/renamed-away")
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertIn("UNKNOWN", check["verdict"])
        # THIS FIXTURE MAKES TRUNK RENAME THE PROBE AWAY, WHICH IS A STALE
        # TEST INVENTORY, so that is the sentence it must produce. Both the
        # stale-inventory reading and the general no-evidence reading are the
        # UNKNOWN verdict and neither exculpates trunk; the difference is
        # whether the author is told WHICH kind of no-evidence they have.
        # What this arm exists for is that trunk is never called red, and
        # that is the assertion below.
        self.assertIn("do not exist on current trunk", check["reason"])
        self.assertIn("not a red trunk", check["reason"])
        self.assertNotIn("ALSO fail", check["reason"])
        self.assertIn("base-check UNKNOWN", gate.evidence_line(row))

    def test_a_trunk_run_red_on_ids_we_never_asked_about_proves_nothing(self):  # noqa: VACUOUS_ASSERTION — the two absences (no stale-inventory sentence, no ALSO-fail claim) sit beside two UNCONDITIONAL positives on the SAME observable, check['reason']: it must name the count under investigation and must name the foreign payload verbatim, so a surface returning an empty or generic reason fails before either absence is reached
        """The same refusal one layer in: a READABLE non-zero trunk run whose
        failures are not the ones under investigation. Staged directly on the
        leg, because a reference run only ever runs the ids it was given —
        the branch is real (a loader failure reaches it whenever unittest
        prints it readably) and must not be left to chance."""
        self._seed_main(expect="v2", app="v1")
        self._lane("lane/alien")
        self._advance_main(app="v2", note="trunk fixes the data")
        # A NAME WE NEVER ASKED ABOUT IS A NAME THAT APPEARS IN NO ID UNDER
        # INVESTIGATION, and the arm is only about that case. A payload
        # naming the CLASS or the METHOD of an id this run requested is a
        # stale inventory instead, which is the sibling arm below. The
        # must-hit keeps the two apart: it fails the moment this payload
        # becomes a component of the id.
        self.assertNotIn("SomeForeignCase", _PROBE_ID,
                         "the alien payload must name nothing in the id "
                         "under investigation, or this arm measures the "
                         "stale-inventory case under the wrong name")
        alien = {"status": "FAILED", "rc": 1, "unreadable": False,
                 "unreadable_reason": None,
                 "failed": {"unittest.loader._FailedTest.SomeForeignCase"}}
        with mock.patch.object(gate, "_reference_run",
                               return_value=(alien, None)):
            check = gate._base_check(self.repo, [
                {"kind": "FAIL", "test": _PROBE_ID, "traceback": "frame"}],
                False)
        self.assertIn("UNKNOWN", check["verdict"])
        self.assertIn("none of the 1 failure under investigation",
                      check["reason"])
        self.assertIn("unittest.loader._FailedTest.SomeForeignCase",
                      check["reason"])
        self.assertNotIn("do not exist on current trunk", check["reason"])
        self.assertNotIn("ALSO fail", check["reason"])

    def test_a_loader_marker_naming_a_CLASS_we_DID_ask_for_is_a_stale_inventory(self):
        """THE SIBLING OF THE ARM ABOVE, AND THEY DIFFER BY ONE WORD.

        When the module imports and the CLASS is gone, unittest mints
        `_FailedTest.<bare class name>`, and that class IS a component of the
        id under investigation — a stale inventory, not a foreign failure.
        Keeping both arms adjacent is the point: the ONLY thing separating
        them is whether the payload names something this run asked for, so a
        predicate that stopped asking that question reddens exactly one of
        the two and the pair says which direction it broke."""
        self._seed_main(expect="v2", app="v1")
        self._lane("lane/class-gone")
        self._advance_main(app="v2", note="trunk fixes the data")
        # THE DIAGNOSIS TRAVELS WITH THE MARKER because the marker alone says
        # only WHICH name did not load. A fixture that omits it is asking this
        # leg to conclude absence from nothing, which is now UNREADABLE — the
        # arm below is about a class that is GONE, so it carries the
        # AttributeError a real run prints when getattr on the module misses.
        gone = {"status": "FAILED", "rc": 1, "unreadable": False,
                "unreadable_reason": None,
                "failed": {"unittest.loader._FailedTest.StaleProbe"},
                "diagnoses": {
                    "unittest.loader._FailedTest.StaleProbe":
                        "AttributeError: module 'tests.test_stale_probe' has "
                        "no attribute 'StaleProbe'"}}
        self.assertIn("StaleProbe", _PROBE_ID.split("."),
                      "this arm's premise is that the payload IS a component "
                      "of the id under investigation")
        with mock.patch.object(gate, "_reference_run",
                               return_value=(gone, None)):
            check = gate._base_check(self.repo, [
                {"kind": "FAIL", "test": _PROBE_ID, "traceback": "frame"}],
                False)
        self.assertIn("UNKNOWN", check["verdict"])
        self.assertIn("do not exist on current trunk", check["reason"])
        self.assertNotIn("ALSO fail", check["reason"])

    def test_a_class_fixture_that_explodes_on_trunk_IS_a_red_trunk(self):
        """THE TRUE CASE, kept. Tightening the leg to named ids must not lose
        the red trunk it exists to report: when trunk's setUpClass raises,
        every test under that class is red there while unittest prints ONE
        header named `setUpClass`. A plain `ids & failed` reads that as
        reproducing nothing and downgrades a real red trunk to UNKNOWN."""
        self._git("checkout", "-q", "main")
        self._write("app.txt", "v1\n")                  # red at the base
        self._write(os.path.join("tests", "__init__.py"), "")
        self._write(os.path.join("tests", "test_stale_probe.py"),
                    _FIXTURE_PROBE_SRC)
        self._commit("probe suite with a class fixture")
        self._lane("lane/fixture")                      # touches code.txt only
        self._git("checkout", "-q", "main")
        self._write("trunkonly.txt", "arms the fixture\n")
        self._commit("trunk arms the fixture that cannot run")
        self._git("checkout", "-q", "lane/fixture")
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.NOT_STALE)
        self.assertIn("ALSO fails on current trunk", check["reason"])
        self.assertIn(_PROBE_ID, check["reason"])
        self.assertNotIn("STALE BASE", gate.evidence_line(row))

    def test_a_lane_that_breaks_an_untouched_test_file_is_NOT_stale_base(self):
        """The failing test file is outside the diff AND the test passes on
        current trunk — both of the obvious legs hold — yet the lane caused
        the failure by editing app.txt. A file-set-plus-trunk predicate would
        mint a wrong STALE BASE here and tell the author to ignore a failure
        they own. The CAUSAL leg answers it now: applied onto trunk, still
        red, the lane's."""
        self._seed_main(expect="v1", app="v1")          # green base
        self._git("checkout", "-q", "-b", "lane/breaks")
        self._write("app.txt", "v3\n")
        self._commit("lane breaks the data the probe reads")
        self._advance_main(note="unrelated trunk drift")   # trunk still green
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.NOT_STALE)
        self.assertIn("applied onto current trunk", check["reason"])
        self.assertIn("the lane owns", check["reason"])
        self.assertIn(_PROBE_ID, check["reason"])
        self.assertNotIn("STALE BASE", gate.evidence_line(row))

    def test_a_DUAL_CAUSE_failure_is_NEVER_stale_base(self):
        """The finding, verbatim shape: test T fails for
        cause A living in the old base AND cause B introduced by the lane;
        trunk fixes only A. The file is untouched (a holds), T reproduces at
        the merge-base (a' holds — via A), trunk is green (b holds) — and the
        pre-causal-leg predicate said STALE_BASE while applying the lane to
        trunk stays red. SAME TEST ID IS NOT CAUSAL PROOF."""
        self._git("checkout", "-q", "main")
        self._write("app.txt", "v1\n")                  # cause A: base data
        self._write(os.path.join("tests", "__init__.py"), "")
        self._write(os.path.join("tests", "test_stale_probe.py"),
                    _DUAL_PROBE_SRC)
        self._commit("probe suite, red at the base for cause A")
        self._git("checkout", "-q", "-b", "lane/dual")
        self._write("poison.txt", "cause B\n")          # the lane's own
        self._commit("lane plants cause B, test file untouched")
        self._advance_main(app="v2", note="trunk fixes cause A only")
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.NOT_STALE)
        self.assertIn("STILL fail", check["reason"])
        self.assertIn("survives the rebase", check["reason"])
        self.assertIn(_PROBE_ID, check["reason"])
        self.assertNotIn("STALE BASE", gate.evidence_line(row))

    def test_a_lane_that_cannot_APPLY_onto_trunk_is_UNKNOWN_never_stale(self):
        """An unresolvable rebase means nobody can answer the causal question
        yet — and the refusal is said in words, never a silent verdict either
        way."""
        self._seed_main(expect="v2", app="v1")          # red at the base
        self._git("checkout", "-q", "-b", "lane/conflicts")
        self._write("app.txt", "vL\n")                  # collides with trunk's fix
        self._commit("lane edits the same data trunk will fix")
        self._advance_main(app="v2", note="trunk fixes the data")
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("does not apply cleanly", check["reason"])
        self.assertIn("base-check UNKNOWN", gate.evidence_line(row))

    def test_a_failure_NEVER_REPRODUCED_without_the_lane_is_not_stale(self):
        """Green reference legs rule out STALE_BASE, not a flake.

        One red lane run followed by green at the base, trunk, and lane-on-trunk
        cannot distinguish an indirect lane interaction from a flake."""
        self._git("checkout", "-q", "main")
        self._write("app.txt", "v1\n")
        self._write(os.path.join("tests", "__init__.py"), "")
        self._write(os.path.join("tests", "test_stale_probe.py"),
                    _INTERACTION_PROBE_SRC)
        self._commit("probe green until the lane meets the OLD base")
        self._lane("lane/interacts")                    # adds code.txt
        self._advance_main(app="v2", note="trunk changes the data")
        self._write("scratch.txt", "uncommitted lane witness\n")
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.NOT_STALE)
        self.assertIn("merge-base", check["reason"])
        self.assertIn("indirect lane interaction", check["reason"])
        self.assertIn("flake", check["reason"])
        self.assertIn("DIRTY", check["reason"])
        self.assertNotIn("implicated", check["reason"])
        self.assertNotIn("STALE BASE", gate.evidence_line(row))

    def test_an_overlapping_nonreproduction_says_lane_change_or_flake(self):
        self._git("checkout", "-q", "main")
        self._write("app.txt", "v1\n")
        self._write(os.path.join("tests", "__init__.py"), "")
        probe = os.path.join("tests", "test_stale_probe.py")
        self._write(probe, _INTERACTION_PROBE_SRC)
        self._commit("probe green until the lane meets the OLD base")
        self._lane("lane/overlaps")
        with open(os.path.join(self.repo, probe), "a") as fh:
            fh.write("\n# lane also touches the failing test file\n")
        self._commit("lane overlaps the probe")
        self._advance_main(app="v2", note="trunk changes the data")
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.NOT_STALE)
        self.assertIn("lane change", check["reason"])
        self.assertIn("flake", check["reason"])
        self.assertNotIn("implicated", check["reason"])

    def test_an_unbuildable_lane_snapshot_is_UNKNOWN_in_words(self):
        """The stale topology is REAL — only the snapshot build is broken —
        and the verdict still refuses to guess, naming the leg."""
        self._seed_main(expect="v2", app="v1")
        self._lane()
        self._advance_main(app="v2", note="trunk fixes the data")
        with mock.patch.object(gate, "_lane_snapshot", return_value=(
                None, "cannot stage the lane's working state: boom")):
            require_supervisor()
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("lane-on-trunk leg", check["reason"])
        self.assertIn("boom", check["reason"])
        self.assertIn("base-check UNKNOWN", gate.evidence_line(row))

    def test_lane_snapshot_captures_dirty_and_untracked_without_touching_index(self):  # noqa: VACUOUS_ASSERTION — snapshot blob contents are unconditional positive controls for the no-error and no-mutation claims
        """The causal leg promises the WHOLE working state, not HEAD alone.

        Keep three states distinct at once: staged content, newer unstaged
        content over the same path, and an untracked file. The dangling
        snapshot must carry the final worktree bytes while the caller's staged
        index and porcelain status remain byte-for-byte unchanged.
        """
        self._write("a.txt", "staged-a\n")
        self._git("add", "a.txt")
        staged = self._git("diff", "--cached", "--binary")
        self._write("a.txt", "working-a\n")
        self._write("untracked.txt", "untracked\n")
        status = self._git("status", "--porcelain=v1")

        snap, err = gate._lane_snapshot(self.repo, gate.vcs.backend(self.repo))
        self.assertIsNone(err, err)
        self.assertEqual(self._git("show", snap + ":a.txt"), "working-a")
        self.assertEqual(self._git("show", snap + ":untracked.txt"),
                         "untracked")
        self.assertEqual(self._git("diff", "--cached", "--binary"), staged)
        self.assertEqual(self._git("status", "--porcelain=v1"), status)

    def test_a_truncated_failure_list_is_UNKNOWN_and_says_skipped(self):
        check = gate._base_check(self.repo, [
            {"kind": "FAIL", "test": _PROBE_ID, "traceback": "frame"},
            {"truncated": 3}], False)
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("SKIPPED", check["reason"])
        self.assertIn("3", check["reason"])

    def test_unreadable_failure_identities_are_UNKNOWN_in_words(self):
        check = gate._base_check(self.repo, [], True)
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("unreadable", check["reason"])

    def test_an_unresolvable_trunk_is_UNKNOWN_never_stale(self):
        stub = types.SimpleNamespace(
            trunk_ref=lambda repo: "origin/main",
            head_sha=lambda repo, ref="HEAD", **kw: None)
        with mock.patch.object(gate.vcs, "backend", return_value=stub):
            check = gate._base_check(self.repo, [
                {"kind": "FAIL", "test": _PROBE_ID, "traceback": "frame"}],
                False)
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("trunk", check["reason"])

    def test_an_unreadable_changed_set_is_UNKNOWN_never_stale(self):
        """Negative control (c): the stale topology is REAL — only the
        changed-set read is broken — and the verdict still refuses to guess."""
        self._seed_main(expect="v2", app="v1")
        self._lane()
        self._advance_main(app="v2", note="trunk fixes the data")
        with mock.patch.object(gate, "_changed_files", return_value=None):
            require_supervisor()
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("changed-file set", check["reason"])
        self.assertIn("base-check UNKNOWN", gate.evidence_line(row))

    def test_a_breached_reference_bound_reports_what_was_SKIPPED(self):
        """The time cap exists, and breaching it is said in words — a cap
        that silently drops the trunk run would let a stale verdict rest on
        a measurement that never happened."""
        self._seed_main(expect="v2", app="v1")
        self._lane()
        self._advance_main(app="v2", note="trunk fixes the data")
        with mock.patch.object(gate, "RECHECK_TIMEOUT", 0.001):
            require_supervisor()
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("SKIPPED", check["reason"])
        self.assertIn("exceeded", check["reason"])

    def test_the_base_verdict_is_bound_into_the_receipt_identity(self):
        """An edited STALE_BASE pasted into a lane-owned receipt stops
        resolving — the verdict is a field a reader relies on. (Pre-166 the
        lane-owned verdict read LANE_OWNED; the causal ladder now answers
        the same attribution NOT_STALE — the receipt-identity law under
        test is unchanged.)"""
        self._seed_main(expect="v1", app="v1")
        self._git("checkout", "-q", "-b", "lane/owns-tamper")
        self._write(os.path.join("tests", "test_stale_probe.py"),
                    _PROBE_SRC % "v9")
        self._commit("lane rewrites the probe")
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["base_check"]["verdict"], gate.NOT_STALE)
        path = gate.receipts_path()
        with open(path, encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        lines[-1]["base_check"] = dict(lines[-1]["base_check"],
                                       verdict=gate.STALE_BASE)
        with open(path, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")
        got, err = gate.by_id(row["id"])
        self.assertIsNone(got)
        # same: a KNOWN version whose stored id disagrees is tamper,
        # and the reader is told to investigate rather than re-gate.
        self.assertIn("IS in the ledger", err)
        self.assertIn("CHANGED", err)
        self.assertNotIn("no minted gate receipt", err)

    def test_gate_show_prints_the_base_verdict(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            gate._show_base_check({"v": 3, "base_check": {
                "verdict": gate.STALE_BASE, "reason": "the base is stale"}})
        self.assertIn("STALE BASE", stdout.getvalue())
        self.assertIn("the base is stale", stdout.getvalue())

    def test_the_absent_test_arm_fires_a_peek_with_a_stale_inventory_is_UNKNOWN(self):
        """A peek whose failing test does not exist on trunk reads UNKNOWN,
        not red trunk — the #150 cure. Mock _reference_run to return
        _FailedTest-wrapped IDs that never intersect clean peek IDs."""
        with mock.patch.object(gate, "_test_file",
                               return_value="tests/test_probe.py"), \
             mock.patch.object(gate, "_changed_files",
                               return_value=set()), \
             mock.patch.object(gate, "_reference_run") as ref:
            ref.side_effect = [
                # trunk leg: FAILED with a _FailedTest-mangled ID.
                #
                # THE PAYLOAD IS THE BARE METHOD NAME BECAUSE THAT IS WHAT
                # unittest MINTS. The loader walks module, then class, then
                # attribute, and names the ONE COMPONENT it could not
                # resolve, so an explicit-id reference run never emits a
                # dotted payload. A fixture carrying one is green on an input
                # the world does not produce.
                #
                # AND THE DIAGNOSIS TRAVELS WITH IT: absence is a conclusion
                # this leg has to EARN, and the marker alone says only which
                # name did not load. This is the absent-METHOD shape, so the
                # AttributeError names the class the getattr missed on.
                ({"status": "FAILED", "rc": 1, "failed": {
                    "unittest.loader._FailedTest.test_probe"},
                 "diagnoses": {
                     "unittest.loader._FailedTest.test_probe":
                         "AttributeError: type object 'StaleProbe' has no "
                         "attribute 'test_probe'"},
                 "unreadable": False,
                 "unreadable_reason": None}, None),
                # lane-on-trunk leg: OK (won't be reached since trunk leg fires first)
                ({"status": "OK", "rc": 0, "failed": set(),
                 "unreadable": False,
                 "unreadable_reason": None}, None),
                # merge-base leg: won't be reached
                ({"status": "OK", "rc": 0, "failed": set(),
                 "unreadable": False,
                 "unreadable_reason": None}, None),
            ]
            check = gate._base_check(self.repo, [
                {"kind": "FAIL", "test": _PROBE_ID, "traceback": "frame"}],
                False)
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("do not exist on current trunk", check["reason"])
        self.assertEqual(_PROBE_ID.rsplit(".", 1)[-1], "test_probe",
                         "the payload above is the BARE METHOD NAME of the "
                         "id under investigation; rename that id and this "
                         "arm silently stops measuring a stale inventory")

    # THE TRANSCRIPTS BELOW ARE THE SHAPE unittest PRINTS, taken from one
    # explicit-id reference run per shape under the gate's own interpreter
    # and re-spelled onto synthetic ids.
    #
    # THEY GO THROUGH parse_result rather than hand-writing the dict a
    # reference run returns, because a hand-written dict is an assertion
    # about what the producer prints and that assertion is the one nobody
    # reviews. A branch guarded only by such a dict can be unreachable for
    # every input the world produces while its arm stays green.
    ABSENT_METHOD = (
        "=" * 70 + "\n"
        "ERROR: test_probe (unittest.loader._FailedTest.test_probe)\n"
        + "-" * 70 + "\n"
        "AttributeError: type object 'StaleProbe' has no attribute "
        "'test_probe'\n\n"
        + "-" * 70 + "\n"
        "Ran 1 test in 0.001s\n\nFAILED (errors=1)\n")
    ABSENT_CLASS = (
        "=" * 70 + "\n"
        "ERROR: StaleProbe (unittest.loader._FailedTest.StaleProbe)\n"
        + "-" * 70 + "\n"
        "AttributeError: module 'tests.test_stale_probe' has no attribute "
        "'StaleProbe'\n\n"
        + "-" * 70 + "\n"
        "Ran 1 test in 0.001s\n\nFAILED (errors=1)\n")
    ABSENT_MODULE = (
        "=" * 70 + "\n"
        "ERROR: test_stale_probe "
        "(unittest.loader._FailedTest.test_stale_probe)\n"
        + "-" * 70 + "\n"
        "ImportError: Failed to import test module: test_stale_probe\n"
        "Traceback (most recent call last):\n"
        '  File "/usr/lib/python3/unittest/loader.py", line 137, in '
        "loadTestsFromName\n"
        "    module = __import__(module_name)\n"
        "ModuleNotFoundError: No module named 'tests.test_stale_probe'\n\n\n"
        + "-" * 70 + "\n"
        "Ran 1 test in 0.000s\n\nFAILED (errors=1)\n")

    # THE SAME MARKER SHAPE FOR THE OPPOSITE FACT, and the whole reason the
    # diagnosis has to survive the reference run. The module is THERE; its own
    # import line raised, so the loader still catches ImportError and still
    # mints `_FailedTest.test_stale_probe`. The header, the marker and the
    # exception TYPE are identical to the absent case above. What differs is
    # WHICH module the error names, and one frame from the test file itself.
    BROKEN_MODULE = (
        "=" * 70 + "\n"
        "ERROR: test_stale_probe "
        "(unittest.loader._FailedTest.test_stale_probe)\n"
        + "-" * 70 + "\n"
        "ImportError: Failed to import test module: test_stale_probe\n"
        "Traceback (most recent call last):\n"
        '  File "/usr/lib/python3/unittest/loader.py", line 137, in '
        "loadTestsFromName\n"
        "    module = __import__(module_name)\n"
        '  File "/repo/tests/test_stale_probe.py", line 1, in <module>\n'
        "    import a_dependency_that_is_not_installed\n"
        "ModuleNotFoundError: No module named "
        "'a_dependency_that_is_not_installed'\n\n\n"
        + "-" * 70 + "\n"
        "Ran 1 test in 0.000s\n\nFAILED (errors=1)\n")

    def _trunk_run(self, transcript):
        """The reference run's own reading of a transcript.

        `_reference_run` builds its dict by calling parse_result on the
        child's stderr, so an arm that hand-writes that dict is asserting
        against its own idea of the parse rather than the parse. This runs
        the real one — the arm is then only as true as the TRANSCRIPT, which
        is the fact it means to encode."""
        parsed = gate.parse_result(transcript)
        return {"status": parsed["status"], "rc": 1,
                "failed": {e["test"] for e in parsed["failures"]},
                "diagnoses": {e["test"]: e.get("traceback")
                              for e in parsed["failures"]},
                "unreadable": parsed["failures_unreadable"],
                "unreadable_reason": parsed["unreadable_reason"]}

    def _base_check_on(self, trunk_transcript, drop_reason=False):
        """`drop_reason` returns the trunk leg in the shape a reader that
        predates the reason field produces — the KEY ABSENT, which is a
        different fact from a reason of None and the one the rendering has
        to survive."""
        trunk_leg = self._trunk_run(trunk_transcript)
        if drop_reason:
            trunk_leg.pop("unreadable_reason")
        with mock.patch.object(gate, "_test_file",
                               return_value="tests/test_probe.py"), \
             mock.patch.object(gate, "_changed_files", return_value=set()), \
             mock.patch.object(gate, "_reference_run") as ref:
            ref.side_effect = [
                (trunk_leg, None),
                ({"status": "OK", "rc": 0, "failed": set(),
                  "unreadable": False,
                  "unreadable_reason": None}, None),
                ({"status": "OK", "rc": 0, "failed": set(),
                  "unreadable": False,
                  "unreadable_reason": None}, None),
            ]
            return gate._base_check(self.repo, [
                {"kind": "FAIL", "test": _PROBE_ID, "traceback": "frame"}],
                False)

    # THE TRANSCRIPT THE REASON EXISTS FOR: a FAILED footer that names no
    # counts at all. The parse cannot check the blocks against anything, so
    # the run is unreadable and trunk's verdict is unobtainable — which is
    # exactly the moment a receipt reader is left with a shrug.
    NO_COUNTS = ("=" * 70 + "\n"
                 "FAIL: test_probe (tests.test_probe.Probe.test_probe)\n"
                 + "-" * 70 + "\n"
                 "Traceback (most recent call last):\n"
                 '  File "/tmp/test_probe.py", line 7, in test_probe\n'
                 '    self.fail("boom")\n'
                 "AssertionError: boom\n\n"
                 + "-" * 70 + "\n"
                 "Ran 1 test in 0.1s\n\nFAILED (unexpected successes=1)\n")

    def test_an_unreadable_trunk_run_says_which_condition_made_it_so(self):
        """THE SEAM THIS LANE EXISTS FOR. The reference run is read once and
        its text is not kept: the receipt carries the verdict and nothing
        else. So a reason known to the parse and dropped between here and the
        sentence cannot be recovered by anyone afterwards, and the UNKNOWN
        degrades to a shrug at the one moment it is holding the answer.

        The expected reason is READ FROM THE PARSE rather than typed, so the
        arm binds the two ENDS of the seam to each other instead of binding
        both to a sentence of its own."""
        run = self._trunk_run(self.NO_COUNTS)
        self.assertTrue(run["unreadable"],
                        "the transcript must be UNREADABLE or this arm never "
                        "reaches the fall-through it is about")
        why = run["unreadable_reason"]
        self.assertTrue(why, "the parse itself states no reason, so the seam "
                             "below has nothing to carry and this arm would "
                             "pass over an unfixed defect")
        check = self._base_check_on(self.NO_COUNTS)
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        self.assertIn(why, check["reason"],
                      "the parse knew why and the verdict does not say it")

    def test_a_reference_run_that_states_no_reason_invents_none(self):
        """THE CONTROL ON THE SAME SENTENCE, and the reason the assertion
        above is not enough by itself: a renderer that always appends
        SOMETHING would satisfy it while making the field decoration. A leg
        carrying no reason must render the sentence unchanged — the two
        renders differ by exactly the parenthetical and nothing else."""
        why = self._trunk_run(self.NO_COUNTS)["unreadable_reason"]
        stated = self._base_check_on(self.NO_COUNTS)["reason"]
        quiet = self._base_check_on(self.NO_COUNTS, drop_reason=True)
        # POSITIVE CONTROLS ON THE SILENT RENDER FIRST: a leg that dropped
        # the key must still produce this verdict and a sentence. An empty
        # reason would satisfy every absence below while saying nothing.
        self.assertEqual(quiet["verdict"], gate.BASE_UNKNOWN)
        silent = quiet["reason"]
        self.assertTrue(silent, "the leg carrying no reason rendered no "
                                "sentence either, so the absence below is "
                                "measured over nothing")
        self.assertNotIn(why, silent)
        self.assertEqual(stated.replace(" (%s)" % why, ""), silent,
                         "the two renders differ by more than the reason "
                         "clause, so the sentence is being rebuilt rather "
                         "than extended")

    def test_an_absent_METHOD_on_trunk_reads_as_a_stale_inventory(self):
        """THE SHAPE THAT PRODUCED THE DEFECT, end to end.

        A lane whose red is a NEW ARM asks trunk about an id trunk has never
        heard of. unittest answers with a _FailedTest carrying the BARE
        METHOD NAME and NO TRACEBACK, because nothing ran and there is no
        stack to print. Before this cure that cost the run twice over: the
        missing traceback made the block INCOMPLETE so the whole run read
        UNREADABLE, and even readable the bare name failed the dotted test,
        so base_check answered 'neither a pass nor a readable failure' — no
        attribution at all for the single most common red this fleet
        produces."""
        # THE PREMISE OF THE ARM, ASSERTED: the transcript must actually be
        # readable now, or the verdict below would be measuring the
        # unreadable fall-through under a different name.
        run = self._trunk_run(self.ABSENT_METHOD)
        self.assertFalse(run["unreadable"],
                         "the absent-method transcript must parse, or this "
                         "arm measures the unreadable path it was written "
                         "to retire")
        self.assertEqual(run["failed"],
                         {"unittest.loader._FailedTest.test_probe"},
                         "the marker must carry the BARE method name — a "
                         "dotted payload here means the transcript was "
                         "invented rather than measured")
        check = self._base_check_on(self.ABSENT_METHOD)
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("do not exist on current trunk", check["reason"])

    def test_an_absent_CLASS_reads_as_a_stale_inventory_too(self):
        """Second of the three loader shapes: the module imports and the
        CLASS is gone, so the marker carries the bare class name and again
        there is no traceback. Same verdict, and it is a separate arm
        because the two die at different getattr calls."""
        run = self._trunk_run(self.ABSENT_CLASS)
        # THE POSITIVE CONTROL ON THE SAME OBSERVABLE, not just the absence
        # of a complaint: name what the parse PRODUCED. `unreadable is False`
        # and `the parse found nothing` are different facts that an
        # assertFalse alone cannot separate.
        self.assertEqual(run["failed"],
                         {"unittest.loader._FailedTest.StaleProbe"},
                         "the marker must carry the BARE class name")
        self.assertFalse(run["unreadable"], "the absent-class transcript "
                         "must parse for this arm to mean anything")
        check = self._base_check_on(self.ABSENT_CLASS)
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("do not exist on current trunk", check["reason"])

    def test_an_absent_MODULE_reads_as_a_stale_inventory_although_it_has_a_traceback(self):
        """Third shape, and the one that separates the two defects. An
        import failure DOES print a traceback, so it was always readable —
        it reached the wrong branch for the OTHER reason, the dotted test,
        and got 'it failed on something else instead'. Keeping it as its own
        arm is what stops a future cure of only the traceback half from
        looking complete."""
        run = self._trunk_run(self.ABSENT_MODULE)
        self.assertEqual(run["failed"],
                         {"unittest.loader._FailedTest.test_stale_probe"},
                         "the marker must carry the bare module name that "
                         "failed to import")
        self.assertFalse(run["unreadable"])
        diagnosis = gate.parse_result(self.ABSENT_MODULE)["failures"][0]
        self.assertIn("ModuleNotFoundError", diagnosis["traceback"] or "",
                      "this shape's readability comes from a REAL traceback "
                      "and NOT from the loader-marker excuse; if the "
                      "traceback stops arriving the arm no longer separates "
                      "the two defects")
        check = self._base_check_on(self.ABSENT_MODULE)
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("do not exist on current trunk", check["reason"])

    def test_a_module_that_IS_there_and_failed_to_import_is_trunks_breakage(self):
        """THE MARKER IS THE SAME AND THE VERDICT MUST NOT BE. A module that
        is GONE and a module that is THERE and raised on import both reach the
        loader's ImportError handler and both mint `_FailedTest.<module>`, so
        the identity alone cannot separate them — and the identity is all the
        absent-id rule reads. Told the wrong one, an author goes looking on
        trunk for a test that is sitting right there.

        The arm asserts BOTH poles off ONE fixture pair that differs only in
        which module the error names, because a verdict that is right about
        the broken case by being wrong about every case is not a cure."""
        run = self._trunk_run(self.BROKEN_MODULE)
        # THE PREMISE, ASSERTED: this fixture must reach the same branch the
        # absent one does, or the arm is measuring a different refusal.
        self.assertEqual(run["failed"],
                         {"unittest.loader._FailedTest.test_stale_probe"},
                         "the broken-module fixture must mint the SAME marker "
                         "the absent one does, or these two are not the pair "
                         "this cure is about")
        self.assertFalse(run["unreadable"])
        self.assertTrue(gate._absent_id(
            "unittest.loader._FailedTest.test_stale_probe", [_PROBE_ID]),
            "the id rule must still call this marker one of ours; the "
            "diagnosis is what separates the two, not the identity")
        check = self._base_check_on(self.BROKEN_MODULE)
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("could not IMPORT", check["reason"])
        self.assertIn("a_dependency_that_is_not_installed", check["reason"],
                      "the verdict must NAME the module the import could not "
                      "find; that name is the LAST thing in the diagnosis and "
                      "so the first thing a bounded reason line removes, which "
                      "is why the verdict renders the exception line and not "
                      "the frame chain above it")
        self.assertNotIn("do not exist on current trunk", check["reason"],
                         "the module IS on trunk and this sentence sends its "
                         "author to look in the one place it is not")
        # THE OTHER POLE, from the fixture that differs only in the module
        # name inside the error. Without this the arm above is satisfied by a
        # branch that fired for every module marker.
        absent = self._base_check_on(self.ABSENT_MODULE)
        self.assertIn("do not exist on current trunk", absent["reason"])
        self.assertNotIn("could not IMPORT", absent["reason"])

    def test_the_discriminator_is_WHICH_module_the_error_names(self):
        """READ OFF THE FIXTURES, NOT TYPED. Both diagnoses come from
        parse_result over the two transcripts, so this arm cannot drift from
        what the parse actually produces — and if the diagnosis ever stops
        carrying the ModuleNotFoundError line, the positive control fails
        before any verdict is asserted."""
        absent = gate.parse_result(self.ABSENT_MODULE)["failures"][0]
        broken = gate.parse_result(self.BROKEN_MODULE)["failures"][0]
        for label, entry in (("absent", absent), ("broken", broken)):
            self.assertIn("ModuleNotFoundError", entry["traceback"] or "",
                          "the %s diagnosis no longer carries the exception "
                          "the discriminator reads" % label)
        self.assertFalse(gate._import_reached_the_module(
            absent["traceback"], "test_stale_probe"))
        self.assertTrue(gate._import_reached_the_module(
            broken["traceback"], "test_stale_probe"))
        # AN IMPORT THAT FAILED WITHOUT A MISSING MODULE REACHED THE MODULE
        # TOO: `cannot import name X from Y` means Y imported and was read.
        # THE TEXT HERE IS THE SHAPE parse_result PRODUCES — the loader frame
        # and what followed it — NOT the raw block. The exception HEADER above
        # a traceback is not kept by the parse, so an assertion written
        # against the raw transcript would be testing a string this predicate
        # never receives.
        self.assertTrue(gate._import_reached_the_module(
            'File "/usr/lib/python3/unittest/loader.py", line 137, in '
            "loadTestsFromName | module = __import__(module_name) | "
            'File "/repo/tests/test_stale_probe.py", line 3, in <module> | '
            "from helm.store import Thing | "
            "ImportError: cannot import name 'Thing' from 'helm.store'",
            "test_stale_probe"))
        # AND THE SHAPES THAT ARE NOT MODULE LOADS SAY NOTHING. A missing
        # METHOD or CLASS mints a marker with NO traceback at all, and a
        # predicate that guessed from an absent diagnosis would answer about
        # a question it was never given.
        self.assertFalse(gate._import_reached_the_module(None, "test_probe"))
        self.assertFalse(gate._import_reached_the_module(
            "AssertionError: 1 != 2", "test_probe"))

    def test_a_loader_marker_for_an_id_NOBODY_ASKED_ABOUT_is_not_absent(self):
        """THE HALF THE OLD RULE GOT RIGHT, AND IT MUST SURVIVE. A marker
        whose payload matches no component of any id under investigation is
        trunk breaking on something we never asked about — no evidence
        either way, and calling it a stale inventory would hand the author a
        false alibi."""
        self.assertFalse(gate._absent_id(
            "unittest.loader._FailedTest.some_unrelated_thing", {_PROBE_ID}))
        self.assertTrue(gate._absent_id(
            "unittest.loader._FailedTest.test_probe", {_PROBE_ID}))
        self.assertFalse(gate._absent_id(_PROBE_ID, {_PROBE_ID}),
                         "a plain id is not a loader marker at all")

    def test_a_block_with_no_traceback_and_no_loader_marker_stays_unreadable(self):
        """THE LOOSENING IS NARROW AND THIS IS WHERE THAT IS PROVED. The
        completeness rule now excuses a missing traceback for ONE shape: a
        block whose identity is a loader marker, which can never have one. A
        FAIL from a test that actually RAN has a traceback by construction,
        so one arriving without it is a malformed block and must still make
        the run unreadable. Mutate the guard's marker test away and this arm
        is the one that notices."""
        ran_but_no_traceback = (
            "=" * 70 + "\n"
            "FAIL: test_probe (tests.test_stale_probe.StaleProbe.test_probe)\n"
            + "-" * 70 + "\n\n"
            + "-" * 70 + "\n"
            "Ran 1 test in 0.001s\n\nFAILED (failures=1)\n")
        parsed = gate.parse_result(ran_but_no_traceback)
        self.assertEqual(parsed["status"], "FAILED")
        self.assertTrue(parsed["failures_unreadable"])

    def test_a_mixed_trunk_failure_is_not_a_stale_inventory(self):
        """A MUTATION FOUND THIS HOLE: `all(_absent_id(...))` -> `any(...)`
        survived, because nothing exercised a MIXED set. If trunk failed on a
        real id AS WELL as on a name that would not load, it is not a stale
        inventory — trunk genuinely broke on something, and calling that "the
        tests do not exist here" hands the author a false alibi."""
        with mock.patch.object(gate, "_test_file",
                               return_value="tests/test_probe.py"), \
             mock.patch.object(gate, "_changed_files", return_value=set()), \
             mock.patch.object(gate, "_reference_run") as ref:
            ref.side_effect = [
                ({"status": "FAILED", "rc": 1, "failed": {
                    "unittest.loader._FailedTest.tests.test_probe.X.test_a",
                    "tests.test_other.RealTest.test_real"},
                  "unreadable": False,
                  "unreadable_reason": None}, None),
                ({"status": "OK", "rc": 0, "failed": set(),
                  "unreadable": False,
                  "unreadable_reason": None}, None),
                ({"status": "OK", "rc": 0, "failed": set(),
                  "unreadable": False,
                  "unreadable_reason": None}, None),
            ]
            check = gate._base_check(self.repo, [
                {"kind": "FAIL", "test": _PROBE_ID, "traceback": "frame"}],
                False)
        self.assertIn("UNKNOWN", check["verdict"])
        self.assertNotIn("do not exist on current trunk", check["reason"])
        self.assertIn("none of the 1 failure under investigation",
                      check["reason"])
        self.assertIn("tests.test_other.RealTest.test_real", check["reason"])

    def test_the_merge_base_leg_reads_a_fixture_explosion_too(self):
        """A MUTATION FOUND THIS ONE AS WELL: swapping `_reproduced` for a
        plain intersection AT THE MERGE-BASE survived, because only the trunk
        leg was covered. The lane's whole claim is that EVERY leg reads its
        reference run by id through one function — an untested leg is where
        the next false exculpation lands."""
        cls_id = _PROBE_ID.rsplit(".", 1)[0] + ".setUpClass"
        # THE MERGE-BASE MUST DIFFER FROM TRUNK OR THE LEG NEVER RUNS: there is
        # an earlier `if mb == trunk` short-circuit that answers NOT_STALE
        # ("the lane is based on current trunk"). My first fixture skipped this
        # and asserted a verdict the leg had no chance to produce — the same
        # mistake twice in one test, both times a leg I never reached.
        self._seed_main(expect="v2", app="v1")
        self._lane("lane/fixture-explosion")
        self._advance_main(app="v2", note="trunk moves on")
        with mock.patch.object(gate, "_test_file",
                               return_value="tests/test_probe.py"), \
             mock.patch.object(gate, "_changed_files", return_value=set()), \
             mock.patch.object(gate, "_reference_run") as ref:
            ref.side_effect = [
                ({"status": "OK", "rc": 0, "failed": set(),
                  "unreadable": False,
                  "unreadable_reason": None}, None),        # trunk leg green
                # LANE-ON-TRUNK MUST BE GREEN TO REACH THE MERGE-BASE LEG.
                # A red one short-circuits to NOT_STALE ("still fails with
                # this lane's changes, so the lane owns it") and the leg under
                # test never runs — my first fixture made exactly that mistake
                # and asserted a verdict the leg had no chance to produce.
                ({"status": "OK", "rc": 0, "failed": set(),
                  "unreadable": False,
                  # lane-on-trunk green
                  "unreadable_reason": None}, None),
                ({"status": "FAILED", "rc": 1, "failed": {cls_id},
                  "unreadable": False,
                  # merge-base: setUpClass
                  "unreadable_reason": None}, None),
            ]
            check = gate._base_check(self.repo, [
                {"kind": "FAIL", "test": _PROBE_ID, "traceback": "frame"}],
                False)
        # The fixture id COVERS the probe, so the base reproduced it — a stale
        # base. A plain intersection sees no match and calls the names absent.
        self.assertEqual(check["verdict"], gate.STALE_BASE)
        # POSITIVE CONTROL ON check["reason"] ITSELF, so the absence below is
        # about the WORDS and not about a reason that is empty or unset.
        self.assertIn("outside this lane's diff", check["reason"])
        self.assertNotIn("do not exist at the merge-base", check["reason"])

    def test_the_MERGE_BASE_leg_separates_a_broken_module_from_an_absent_one(self):
        """THE TRUNK LEG'S TWIN, ASKING THE SAME QUESTION OF A DIFFERENT SHA.

        Both legs conclude ABSENCE from the same evidence — a loader marker
        covering none of the ids — and a module that is present and fails to
        import produces exactly that marker. Curing one leg and not the other
        leaves the same wrong sentence live one screen down, which is what a
        per-case cure looks like from the inside: complete, and half done.

        BOTH POLES OFF ONE PAIR, because a branch that fired for every module
        marker would satisfy the broken half while destroying the absent one.
        """
        broken = self._trunk_run(self.BROKEN_MODULE)
        absent = self._trunk_run(self.ABSENT_MODULE)
        # THE PREMISE: the two runs are indistinguishable by IDENTITY, which
        # is the whole reason the diagnosis has to travel. NAME THE MARKER
        # FIRST — two runs that each parsed to NOTHING are also equal, and
        # that equality would satisfy the pairing while proving the fixtures
        # produce no markers at all.
        self.assertEqual(broken["failed"],
                         {"unittest.loader._FailedTest.test_stale_probe"},
                         "the broken-module fixture does not mint the marker "
                         "this arm is about")
        self.assertEqual(broken["failed"], absent["failed"],
                         "the two fixtures no longer mint the same marker, so "
                         "this arm is not about the ambiguity it names")
        green = {"status": "OK", "rc": 0, "failed": set(), "diagnoses": {},
                 "unreadable": False, "unreadable_reason": None}

        # ONE GEOMETRY, TWO LEGS. Seeding per call made the SECOND reading
        # run against a repo whose merge-base had become trunk, which
        # short-circuits to NOT_STALE before the leg under test runs — the arm
        # then measured a verdict this branch never produces. The repo does
        # not vary between the two readings; only the mocked reference run
        # does, so the seed belongs out here.
        self._seed_main(expect="v2", app="v1")
        self._lane("lane/module-import")
        self._advance_main(app="v2", note="trunk moves on")

        def merge_base_leg(at_base):
            with mock.patch.object(gate, "_test_file",
                                   return_value="tests/test_probe.py"), \
                 mock.patch.object(gate, "_changed_files", return_value=set()), \
                 mock.patch.object(gate, "_reference_run") as ref:
                ref.side_effect = [(green, None), (green, None),
                                   (at_base, None)]
                return gate._base_check(self.repo, [
                    {"kind": "FAIL", "test": _PROBE_ID,
                     "traceback": "frame"}], False)

        said = merge_base_leg(broken)
        self.assertEqual(said["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("could not IMPORT", said["reason"])
        self.assertIn("a_dependency_that_is_not_installed", said["reason"])
        self.assertNotIn("do not exist at the merge-base", said["reason"])
        quiet = merge_base_leg(absent)
        self.assertEqual(quiet["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("do not exist at the merge-base", quiet["reason"])
        self.assertNotIn("could not IMPORT", quiet["reason"])

    def test_a_leg_that_carries_no_diagnoses_still_reads_as_absent(self):
        """THE LEGACY SHAPE, and the reason production asks with `.get`. A
        reference run recorded before the diagnosis travelled — or any reader
        that builds this dict by hand — carries no diagnoses at all, and an
        absent key must not read as evidence that a module was present."""
        legacy = dict(self._trunk_run(self.BROKEN_MODULE))
        legacy.pop("diagnoses")
        self.assertEqual([], gate._import_broke_at(legacy, [_PROBE_ID]),
                         "a run with no diagnoses claimed a module was "
                         "present, which it cannot know")
        # POSITIVE CONTROL ON THE SAME RUN: with its diagnoses it DOES answer.
        self.assertTrue(gate._import_broke_at(self._trunk_run(
            self.BROKEN_MODULE), [_PROBE_ID]))

    def test_the_absent_test_arm_does_not_over_fire_a_genuine_red_trunk_stays_NOT_STALE(self):
        """Negative control: a genuinely failing test on trunk still reads
        NOT_STALE, not UNKNOWN — the new arm only fires on absent tests."""
        with mock.patch.object(gate, "_test_file",
                               return_value="tests/test_probe.py"), \
             mock.patch.object(gate, "_changed_files",
                               return_value=set()), \
             mock.patch.object(gate, "_reference_run") as ref:
            ref.side_effect = [
                # trunk leg: FAILED with the SAME ID as the peek
                ({"status": "FAILED", "rc": 1, "failed": {_PROBE_ID},
                 "unreadable": False,
                 "unreadable_reason": None}, None),
                # lane-on-trunk leg: won't be reached (trunk is red, short-circuits)
                ({"status": "OK", "rc": 0, "failed": set(),
                 "unreadable": False,
                 "unreadable_reason": None}, None),
                ({"status": "OK", "rc": 0, "failed": set(),
                 "unreadable": False,
                 "unreadable_reason": None}, None),
            ]
            check = gate._base_check(self.repo, [
                {"kind": "FAIL", "test": _PROBE_ID, "traceback": "frame"}],
                False)
        self.assertEqual(check["verdict"], gate.NOT_STALE)
        # WORDING UPDATED WITH THE BEHAVIOUR, and STRENGTHENED while here.
        # This arm landed after the lane that made this leg name its ids, so
        # it still pinned the old sentence. Asserting the id too is the point
        # of that change: "a red trunk" without WHICH tests is the sentence
        # that makes an author stop investigating.
        self.assertIn("ALSO fails on current trunk", check["reason"])
        self.assertIn(_PROBE_ID, check["reason"])

    # A SECOND ID, SO THE SCAN HAS SOMETHING TO BE WRONG ABOUT. Every arm
    # above investigates ONE id, and a quantifier over a one-element set is
    # right whichever set it quantifies over — which is exactly why the defect
    # below lived under a green suite. `_OTHER_ID` is the second test in the
    # same probe class.
    def _two_id_check(self, trunk_leg, rest=None):
        rest = rest or [
            {"status": "OK", "rc": 0, "failed": set(), "diagnoses": {},
             "unreadable": False, "unreadable_reason": None}] * 2
        with mock.patch.object(gate, "_test_file",
                               return_value="tests/test_probe.py"), \
             mock.patch.object(gate, "_changed_files", return_value=set()), \
             mock.patch.object(gate, "_reference_run") as ref:
            ref.side_effect = [(trunk_leg, None)] + [(r, None) for r in rest]
            return gate._base_check(self.repo, [
                {"kind": "FAIL", "test": _PROBE_ID, "traceback": "frame"},
                {"kind": "FAIL", "test": _OTHER_ID, "traceback": "frame"}],
                False)

    # THE MARKER SHAPE, WRITTEN ONCE: a class that is gone, with the
    # AttributeError a measured run prints beside it.
    _CLASS_GONE = "unittest.loader._FailedTest.StaleProbe"
    _CLASS_GONE_WHY = ("AttributeError: module 'tests.test_stale_probe' has "
                       "no attribute 'StaleProbe'")

    def test_an_id_that_RESOLVED_on_trunk_is_not_swept_into_an_absence(self):
        """THE SPECIMEN, REDUCED. A trunk reference run is given every failing
        id at once. Some do not resolve there and mint loader markers; the
        rest RUN. An id that ran and PASSED appears in no failure list, so the
        guard — "every name that FAILED here is a marker for one of ours" —
        is satisfied while saying nothing at all about it, and the sentence it
        chose then quantified over the IDS: "the test names are all absent
        here."

        MEASURED, not imagined: seven ids, three unresolvable classes, four
        green on trunk, and the receipt told the author all seven were gone.
        The four were live regressions against tests that exist on trunk by
        class name AND by method name, and this leg wrote them off as
        inventory drift. THE NULL FROM A SCAN IS A FACT ABOUT THE SCAN.

        A positive control alone would have passed here — the three absent
        ids really were absent. Only the second control catches it, so this
        arm seeds BOTH: `_PROBE_ID`, which the marker names, and `_OTHER_ID`,
        which it does not and which therefore resolved."""
        trunk = {"status": "FAILED", "rc": 1, "unreadable": False,
                 "unreadable_reason": None, "failed": {self._CLASS_GONE},
                 "diagnoses": {self._CLASS_GONE: self._CLASS_GONE_WHY}}
        # THE TWO CONTROLS ON THE SCAN ITSELF, ASSERTED BEFORE THE VERDICT:
        # one id this marker DOES name and one it does NOT. A scan whose
        # payload matched both — or neither — would satisfy the verdict below
        # while measuring a set with nothing in it to separate.
        census = gate._existence_census(trunk, {_PROBE_ID, _OTHER_ID})
        self.assertEqual([_OTHER_ID, _PROBE_ID], sorted(census["marked"]),
                         "the class payload must name BOTH ids for the "
                         "control below to be the one this arm needs")
        check = self._two_id_check(trunk)
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        # And with the payload naming only ONE of them, the other RESOLVED.
        method_gone = {"status": "FAILED", "rc": 1, "unreadable": False,
                       "unreadable_reason": None,
                       "failed": {"unittest.loader._FailedTest.test_probe"},
                       "diagnoses": {
                           "unittest.loader._FailedTest.test_probe":
                               "AttributeError: type object 'StaleProbe' has "
                               "no attribute 'test_probe'"}}
        mixed = gate._existence_census(method_gone, {_PROBE_ID, _OTHER_ID})
        self.assertEqual([_PROBE_ID], mixed["marked"])
        self.assertEqual([_OTHER_ID], mixed["unmarked"])
        check = self._two_id_check(method_gone)
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        # THE POSITIVE, FIRST AND UNCONDITIONAL: the verdict NAMES the id
        # that resolved and says the leg does not explain it.
        self.assertIn(_OTHER_ID, check["reason"])
        self.assertIn("DID resolve there and did not fail", check["reason"])
        # ...and only then the exoneration it must never mint.
        self.assertNotIn("do not exist on current trunk", check["reason"])
        self.assertNotIn("stale test inventory", check["reason"])

    def test_a_marker_whose_WHY_is_unreadable_is_not_an_absence(self):  # noqa: VACUOUS_ASSERTION — the positive pole runs FIRST and unconditionally on the SAME observable: the SAME marker carrying a readable diagnosis is asserted to MINT "do not exist on current trunk", and inside the loop each absence is preceded by an unconditional assertIn on that same reason naming the unreadable clause, so a surface returning an empty or generic reason fails before any absence is reached
        """UNREADABLE, ABSENT AND PRESENT ARE THREE VALUES.

        "I could not find these names" and "these names are not there" are
        different facts. A marker says WHICH name did not load and nothing
        about why; the diagnosis beside it is the only thing that can say. A
        leg that defaults the missing diagnosis to absence hands its author
        the one sentence that is believed and acted on — not your fault — on
        evidence it never had.

        BOTH POLES OFF ONE FIXTURE PAIR that differs in the diagnosis alone,
        because a branch that fired for every marker would satisfy the
        unreadable half while destroying the absent one."""
        def leg(why):
            return {"status": "FAILED", "rc": 1, "unreadable": False,
                    "unreadable_reason": None,
                    "failed": {self._CLASS_GONE},
                    "diagnoses": {self._CLASS_GONE: why}}
        # THE POSITIVE POLE, UNCONDITIONAL AND FIRST: the SAME marker with a
        # diagnosis that names the missing attribute IS an absence, so the
        # refusal below is about the diagnosis and not about the marker.
        said = self._two_id_check(leg(self._CLASS_GONE_WHY))
        self.assertEqual(said["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("do not exist on current trunk", said["reason"])
        for why in (None, "", "AssertionError: 1 != 2"):
            quiet = self._two_id_check(leg(why))
            self.assertEqual(quiet["verdict"], gate.BASE_UNKNOWN)
            self.assertIn("recorded no readable reason why", quiet["reason"],
                          "a diagnosis of %r was read as an absence" % (why,))
            self.assertNotIn("do not exist on current trunk", quiet["reason"])

    def test_the_MERGE_BASE_leg_will_not_sweep_a_resolved_id_into_an_absence(self):
        """THE TRUNK LEG'S TWIN, ASKING THE SAME QUESTION OF A DIFFERENT SHA.

        Both legs conclude absence from the same evidence and said it in two
        copies of one sentence; a cure written at one of them reads complete
        while the other still exonerates one screen down. At the merge-base
        the wrong answer is worse still: an id that RESOLVED there and did not
        fail is a failure the base cannot explain, which implicates the lane —
        and the old sentence called it a test that does not exist."""
        green = {"status": "OK", "rc": 0, "failed": set(), "diagnoses": {},
                 "unreadable": False, "unreadable_reason": None}
        at_base = {"status": "FAILED", "rc": 1, "unreadable": False,
                   "unreadable_reason": None,
                   "failed": {"unittest.loader._FailedTest.test_probe"},
                   "diagnoses": {
                       "unittest.loader._FailedTest.test_probe":
                           "AttributeError: type object 'StaleProbe' has no "
                           "attribute 'test_probe'"}}
        # THE MERGE-BASE MUST DIFFER FROM TRUNK OR THE LEG NEVER RUNS.
        self._seed_main(expect="v2", app="v1")
        self._lane("lane/mb-resolved")
        self._advance_main(app="v2", note="trunk moves on")
        check = self._two_id_check(green, rest=[green, at_base])
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        # POSITIVE FIRST: the leg NAMES the id it resolved.
        self.assertIn(_OTHER_ID, check["reason"])
        self.assertIn("DID resolve there and did not fail", check["reason"])
        self.assertNotIn("do not exist at the merge-base", check["reason"])
        # AND THE OTHER POLE OFF THE SAME GEOMETRY: a marker covering BOTH
        # ids still reads as the stale inventory it is.
        both = dict(at_base, failed={self._CLASS_GONE},
                    diagnoses={self._CLASS_GONE: self._CLASS_GONE_WHY})
        quiet = self._two_id_check(green, rest=[green, both])
        self.assertIn("do not exist at the merge-base", quiet["reason"])

    def test_the_three_marker_dispositions_are_read_off_measured_transcripts(self):  # noqa: VACUOUS_ASSERTION — every assertion is an unconditional assertEqual naming one of the three values on a POSITIVE observable, and the transcripts are the ones parse_result already produces; there is no absence claim here to be vacuous about
        """ONE PAIR PER BOUNDARY, off the transcripts the arms above measured
        rather than off a dict typed here. A disposition that answered the
        same value for every input would satisfy any one of these and cannot
        satisfy the set."""
        for transcript, expected in ((self.ABSENT_MODULE, "absent"),
                                     (self.BROKEN_MODULE, "present")):
            entry = gate.parse_result(transcript)["failures"][0]
            self.assertEqual(expected, gate._marker_disposition(
                entry["traceback"], "test_stale_probe"), transcript)
        for transcript, payload in ((self.ABSENT_CLASS, "StaleProbe"),
                                    (self.ABSENT_METHOD, "test_probe")):
            entry = gate.parse_result(transcript)["failures"][0]
            self.assertEqual("absent", gate._marker_disposition(
                entry["traceback"], payload))
            # THE OTHER SIDE OF THE SAME READ: an AttributeError naming a
            # DIFFERENT attribute says nothing about the payload's own fate.
            self.assertEqual("unreadable", gate._marker_disposition(
                entry["traceback"], "something_else"))
        self.assertEqual("unreadable", gate._marker_disposition(None, "x"))


class TheVerdictKeepsTheTokenThatIdentifies(GateBase):
    """A CAP CUTS FROM THE RIGHT AND THE NAME LIVES THERE.

    `_failure_text` bounds every reason at 500 characters, keeps the head and
    marks the cut with an ellipsis. That is honest about the FACT of a cut and
    silent about its CONTENT — and for an import verdict the content is the
    missing module's name, which is the whole point of the sentence. A reader
    gets a paragraph that ends mid-path and no way to recover what it named.

    THE TABLE IS THE SHAPES, not one case. Rationing one part of a sentence
    while its neighbours grow moves the threshold and leaves the class
    intact, so the arms drive the real renderer with a short path, a deep
    one, several of each, and ids far longer than this tree can produce.
    """

    # ONE SENTENCE, ASSEMBLED THE WAY THE TRUNK LEG ASSEMBLES IT. Written out
    # here rather than reached through _base_check because that door needs a
    # four-leg geometry to arrive at this branch, and what these arms measure
    # is the RENDERING — the geometry is already pinned by the arms above.
    PROSE = ("the failing tests do not exist on current trunk %s (%s), but %s "
             "IS there and failed to IMPORT — so this run measures a broken "
             "module rather than a stale inventory, and whose these failures "
             "are could not be determined from it. What the import raised: %s")

    @staticmethod
    def _diagnosis(module):
        """The diagnosis shape `parse_result` actually produces for an absent
        module: the loader frame, then the exception line."""
        return ("loadTestsFromName | ModuleNotFoundError: No module named "
                "'%s'" % module)

    def _verdict(self, names, modules):
        diagnoses = {n: self._diagnosis(m) for n, m in zip(names, modules)}
        return gate._base_unknown(self.PROSE % (
            "deadbeefcafe", "main",
            gate._bounded_list(names, gate._IMPORT_NAMES_BUDGET, ", "),
            gate._import_causes(names, diagnoses)))["reason"]

    @staticmethod
    def _deep(leaf, segments=40):
        return ".".join("segment_%02d" % i for i in range(segments)) + "." + leaf

    def test_a_deep_package_path_keeps_its_leaf_and_its_root(self):
        """THE CASE THE CAP ATE. The leaf is the module that is missing and the
        root is the package it was looked for in; the segments between them are
        the part a reader can infer, so that is where the budget is spent."""
        got = self._verdict(["tests.t_deep.K.test_x"], [self._deep("final_leaf")])
        self.assertIn("final_leaf", got,
                      "the verdict lost the only token that says WHICH module "
                      "failed to import: %r" % got)
        self.assertIn("segment_00", got,
                      "the verdict lost the package the module was looked for "
                      "in, so the reader cannot tell where to look")
        self.assertIn("segments elided", got,
                      "the path was shortened without saying so, which is the "
                      "silent-cut defect wearing a different shape")
        # UNCONDITIONAL CONTROL, SAME RENDERER: an ORDINARY path is left
        # byte-identical, so the assertions above are about the deep case and
        # not about the elision firing on everything.
        ordinary = self._verdict(["tests.t_a.K.test_x"], ["pkg.sub.mod"])
        self.assertIn("named 'pkg.sub.mod'", ordinary,
                      "a path this tree can actually produce was rewritten; "
                      "the elision is meant to be unreachable in the ordinary "
                      "case: %r" % ordinary)
        self.assertNotIn("elided", ordinary,
                         "the ordinary case paid for the deep case")

    def test_the_causes_that_do_not_fit_are_COUNTED_not_dropped(self):
        """A BOUND THAT SKIPS WORK SAYS HOW MUCH. The ellipsis a cap leaves
        tells a reader that something went missing and cannot tell them how
        much; a count can. This is the half that makes the omission readable
        rather than merely visible."""
        names = ["tests.t_%d.K.test_x" % i for i in range(4)]
        got = self._verdict(names, [self._deep("leaf_%d" % i, 30)
                                    for i in range(4)])
        self.assertIn("leaf_0", got,
                      "not one complete cause survived, so the verdict names "
                      "no module at all: %r" % got)
        self.assertIn("more not shown", got,
                      "three causes were dropped and the sentence does not "
                      "say that any exist: %r" % got)
        # UNCONDITIONAL CONTROL: a set small enough to fit says nothing about
        # a remainder, so the clause above tracks the BOUND and is not printed
        # unconditionally.
        one = self._verdict(["tests.t_a.K.test_x"], ["shortmod"])
        self.assertIn("shortmod", one)
        self.assertNotIn("more not shown", one,
                         "a verdict that dropped nothing still claims a "
                         "remainder: %r" % one)

    def test_the_count_of_what_was_dropped_SURVIVES_the_cap(self):  # noqa: VACUOUS_ASSERTION — every row's assertFalse is paired IN THE SAME ITERATION with an assertIn on the same rendered reason, and two unconditional assertions after the loop pin both poles of the remainder clause on the same renderer; the rung cannot see that because each _verdict call mints a fresh producer identity, so no out-of-loop positive can share roots with a loop-local name
        """A BOUND'S OWN SENTENCE MUST BE INSIDE THE BOUND.

        The remainder clause exists so an omission is STATED rather than
        implied. Spending the budget on items and then appending the clause
        puts that sentence outside the bound — so the cap cuts exactly the
        words that said something was cut, and the design's whole benefit is
        the first thing lost, silently, on ORDINARY inputs rather than exotic
        ones.

        THE TABLE IS THE ORDINARY POPULATION, swept across module-name length
        and module count, because the failure was not at the extremes: short
        names let MORE items fit, so more are rendered and the sentence grows,
        and the shapes that broke were the small ones.
        """
        deep = self._deep
        # EACH ROW CARRIES THE TOKEN THAT MUST SURVIVE, rather than reusing
        # the module path: an elided path is rendered with its middle
        # removed, so asserting the WHOLE path would fail on correct output
        # for the deep rows and would be asserting the absence of the very
        # elision the arm above requires.
        rows = [("len %d, %d modules" % (namelen, count),
                 ["tests.t_%02d.K.test_x" % i for i in range(count)],
                 ["m%0*d" % (namelen - 2, i) for i in range(count)],
                 "m%0*d" % (namelen - 2, 0))
                for namelen in (20, 24, 28, 32, 40)
                for count in (3, 4, 9)]
        rows += [
            ("one deep path", ["tests.t_deep.K.test_x"],
             [deep("final_leaf")], "final_leaf"),
            ("nine deep paths",
             ["tests.t_%02d.K.test_x" % i for i in range(9)],
             [deep("leaf_%02d" % i, 30) for i in range(9)], "leaf_00"),
            ("forty modules",
             ["tests.t_%02d.K.test_x" % i for i in range(40)],
             ["m_%02d" % i for i in range(40)], "m_00"),
        ]
        for label, names, modules, must in rows:
            got = self._verdict(names, modules)
            self.assertFalse(
                got.endswith("..."),
                "%s: the finished reason reached the cap, so whatever the "
                "bound dropped is reported by an ellipsis instead of a "
                "count: %r" % (label, got))
            self.assertIn(
                must, got.split("What the import raised:")[-1],
                "%s: no complete cause survived" % label)
        # UNCONDITIONAL, OUTSIDE THE LOOP AND ON THE SAME RENDERER: a set
        # large enough to drop something DOES say so. Without this the loop
        # above is satisfied by a bound that silently renders everything, or
        # by a table whose rows all happen to fit.
        dropped = self._verdict(
            ["tests.t_%02d.K.test_x" % i for i in range(40)],
            ["m_%02d" % i for i in range(40)])
        self.assertIn("more not shown", dropped,
                      "forty modules were rendered without a remainder "
                      "clause, so nothing in this arm is about the count")
        kept = self._verdict(["tests.t_a.K.test_x"], ["shortmod"])
        self.assertNotIn("more not shown", kept,
                         "a verdict that dropped nothing still claims a "
                         "remainder: %r" % kept)

    def test_every_measured_shape_keeps_a_whole_identifying_cause(self):
        """THE PROPERTY, OVER THE SHAPES, rather than one case per method.

        What must hold for every input is narrower than "nothing is cut" and
        it is the part a reader acts on: at least one cause arrives COMPLETE,
        and anything missing is counted. The last two rows are past what this
        tree can produce — its deepest dotted module path is 5 segments — and
        are here so the degradation is measured rather than assumed.
        """
        deep = self._deep
        shapes = [
            ("one short module", ["tests.t_a.K.test_x"], ["shortmod"],
             "shortmod"),
            ("four shallow modules",
             ["tests.t_%d.K.test_x" % i for i in range(4)],
             ["mod_%d" % i for i in range(4)], "mod_0"),
            ("one deep path", ["tests.t_deep.K.test_x"],
             [deep("final_leaf")], "final_leaf"),
            ("four deep paths",
             ["tests.t_%d.K.test_x" % i for i in range(4)],
             [deep("leaf_%d" % i, 30) for i in range(4)], "leaf_0"),
            ("nine deep paths",
             ["tests.t_%02d.K.test_x" % i for i in range(9)],
             [deep("leaf_%02d" % i, 30) for i in range(9)], "leaf_00"),
            ("forty modules",
             ["tests.t_%02d.K.test_x" % i for i in range(40)],
             ["m_%02d" % i for i in range(40)], "m_00"),
        ]
        for label, names, modules, must in shapes:
            got = self._verdict(names, modules)
            self.assertIn(must, got,
                          "%s: no complete cause survived, so the verdict "
                          "identifies nothing: %r" % (label, got))
            # THE SURVIVOR MUST BE A CAUSE, not an id that happens to share
            # the token. Both bounded lists can render a remainder clause, so
            # the split is on the CAUSES marker rather than on that clause.
            causes = got.split("What the import raised:")[-1]
            self.assertIn(must, causes,
                          "%s: the token survives only in the id list, so the "
                          "verdict still does not say what the import RAISED: "
                          "%r" % (label, got))
        # UNCONDITIONAL, OUTSIDE THE LOOP: the table has to be non-empty and
        # the renderer has to be reachable, or every assertion above holds
        # over nothing.
        self.assertTrue(shapes, "the shape table is empty")
        self.assertIn("shortmod",
                      self._verdict(["tests.t_a.K.test_x"], ["shortmod"]),
                      "the renderer does not carry a cause at all, so the "
                      "loop above proves nothing about any shape")


class ExitCodeDiscardedByAPipeTest(unittest.TestCase):
    """THE TOOL TELLS THE CALLER THE ANSWER THEY ARE ABOUT TO READ IS NOT THE
    ANSWER IT GAVE.

    A shell pipeline takes its LAST stage's status, so `helm gate run | tail`
    reports tail's success no matter what the gate did. MEASURED:
    two seats hit this independently and repeatedly, every time
    with the premise naming it live in their context. One announced "gating
    now" off a refused gate that exited 0 through the harness; the other ran
    every gate through the same shape and was saved only by reading
    receipts instead of exit codes, which is luck dressed as discipline.

    A rule two seats violated WHILE HOLDING IT does not need restating — it
    needs to stop being a rule. A pipe and a redirect are distinguishable at
    the fd level, so the tool can know at the moment it matters."""

    def rc_and_stderr(self, rc, isfifo):
        err = io.StringIO()
        with mock.patch.object(gate, "_gate_dispatch", return_value=rc), \
             mock.patch.object(gate, "_exit_code_will_be_discarded",
                               return_value=isfifo), \
             contextlib.redirect_stderr(err):
            got = gate.cmd_gate(["run"])
        return got, err.getvalue()

    def test_a_nonzero_through_a_PIPE_is_warned(self):
        rc, err = self.rc_and_stderr(2, True)
        self.assertEqual(rc, 2, "the real status must still be RETURNED")
        self.assertIn("stdout is a PIPE", err)
        self.assertIn("exited 2", err)

    def test_a_nonzero_that_is_NOT_piped_is_silent(self):
        """UNCONDITIONAL CONTROL on the same observable: identical failure,
        honest invocation, no warning. Without this the check would also pass
        for code that warns on every nonzero — which is noise everyone learns
        to skim past, and then it is worth nothing when it is right."""
        rc, err = self.rc_and_stderr(2, False)
        self.assertEqual(rc, 2)
        self.assertEqual(err, "")

    def test_a_SUCCESS_through_a_pipe_is_silent(self):
        """Both conditions are required. A piped success discards a zero, and
        nothing was lost — warning there would train the reader to ignore it
        before the case that matters ever arrives."""
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: the SAME
        # helper with a nonzero DOES write. Without it, `err == ""` would also
        # hold for a wiring that never warns at all.
        _rc2, loud = self.rc_and_stderr(2, True)
        self.assertIn("stdout is a PIPE", loud)
        rc, err = self.rc_and_stderr(0, True)
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")

    def test_the_detector_keys_on_ISFIFO_not_on_not_a_file(self):
        """A TTY is neither ISFIFO nor ISREG. An operator reading a refusal on
        screen sees the exit code their shell reports and is NOT at risk, so
        keying on 'not a regular file' would warn every interactive user."""
        import stat as _stat
        class FakeStat:
            st_mode = _stat.S_IFCHR | 0o620          # a terminal
        with mock.patch.object(gate.os, "fstat", return_value=FakeStat()):
            self.assertFalse(gate._exit_code_will_be_discarded())
        class FifoStat:
            st_mode = _stat.S_IFIFO | 0o600
        with mock.patch.object(gate.os, "fstat", return_value=FifoStat()):
            self.assertTrue(gate._exit_code_will_be_discarded(),
                            "control: the detector DOES fire on a real pipe")

    def test_an_unreadable_fd_does_not_manufacture_noise(self):
        import stat as _stat
        # CONTROL FIRST: the detector is capable of returning True through
        # this exact path, so False below measures the OSError and not a
        # function that never fires.
        class FifoStat:
            st_mode = _stat.S_IFIFO | 0o600
        with mock.patch.object(gate.os, "fstat", return_value=FifoStat()):
            self.assertTrue(gate._exit_code_will_be_discarded())
        with mock.patch.object(gate.os, "fstat", side_effect=OSError("bad fd")):
            self.assertFalse(gate._exit_code_will_be_discarded())


class HelpAndFlagGuardTest(GateBase):
    """`gate import` got this guard in 5334a3dc; show and list did not, and the
    class stayed live one function away in the same file. Measured on trunk
    aa40daaf: `gate show --help` exited 1 with "not a receipt id: '--help'",
    and `gate list --help` produced output BYTE-IDENTICAL to a bare `gate list`
    — the flag silently dropped and the ACTION RUN. An existence probe that
    performs the action is worse than one that refuses; it is the same shape as
    `seat down <name> --bogus` stopping the seat and exiting 0, the incident
    guard_tail was written for."""

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = gate.cmd_gate(argv)
        return rc, out.getvalue() + err.getvalue()

    def test_help_prints_usage_and_never_acts(self):  # noqa: VACUOUS_ASSERTION — three unconditional controls run before the loop: a help call is honoured (rc 0 + usage), a malformed flag refuses (rc 2), and "not a receipt id" is proven EMITTABLE, which is the exact string the loop asserts absent
        # UNCONDITIONAL CONTROL, outside the loop: one help call really is
        # honoured. Without it an empty loop would satisfy every assertion
        # below by running none of them.
        rc, text = self._run(["show", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("gate show", text)
        # AND THE CONTROL FOR THE ABSENCE ASSERTION BELOW: "not a receipt id"
        # is a string this verb really can emit — it is what trunk said about
        # --help itself. Asserting it is ABSENT means nothing unless something
        # unconditionally proves it can be PRESENT.
        rc_bad, bad = self._run(["show", "-not-a-flag-or-id"])
        self.assertEqual(rc_bad, 2)
        rc_shape, shape = self._run(["show", "zzz"])
        self.assertIn("not a receipt id", shape)
        for argv in (["show", "--help"], ["show", "-h"],
                     ["list", "--help"], ["list", "-h"]):
            with self.subTest(argv=argv):
                rc, text = self._run(argv)
                self.assertEqual(rc, 0)
                self.assertIn("gate %s" % argv[0], text)
                self.assertNotIn("not a receipt id", text)

    def test_help_returns_before_any_ledger_read(self):
        with mock.patch.object(gate, "by_id",
                               side_effect=AssertionError("show acted")):
            rc, text = self._run(["show", "0123456789abcdef", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("gate show", text)
        with mock.patch.object(gate, "receipts",
                               side_effect=AssertionError("list acted")):
            rc, text = self._run(["list", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("gate list", text)

    def test_list_help_is_not_a_listing(self):
        """THE POINTED ONE: --help must not render what a bare list renders.
        The control is the bare call — it has to produce the listing, or
        'they differ' would be satisfied by a verb that does nothing at all."""
        rc_bare, bare = self._run(["list"])
        self.assertEqual(rc_bare, 0)
        self.assertTrue(bare.strip(), "CONTROL: a bare list must render something")
        rc_help, helped = self._run(["list", "--help"])
        self.assertEqual(rc_help, 0)
        self.assertNotEqual(bare, helped, "--help was dropped and the action ran")

    def test_unknown_flags_refuse_before_acting(self):
        with mock.patch.object(gate, "by_id",
                               side_effect=AssertionError("show acted")):
            rc, _ = self._run(["show", "0123456789abcdef", "--bogus"])
        self.assertEqual(rc, 2)
        with mock.patch.object(gate, "receipts",
                               side_effect=AssertionError("list acted")):
            rc, _ = self._run(["list", "--bogus"])
        self.assertEqual(rc, 2)
        # UNCONDITIONAL CONTROL, outside the loop, on the same observable.
        rc, text = self._run(["show", "--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown arg", text)
        for argv in (["show", "--bogus"], ["list", "--bogus"],
                     ["show", "abc", "--bogus"]):
            with self.subTest(argv=argv):
                rc, text = self._run(argv)
                self.assertEqual(rc, 2)
                self.assertIn("unknown arg", text)

    def test_a_non_numeric_limit_refuses_instead_of_raising(self):
        """int(_opt(...)) raised ValueError straight out of the verb. guard_tail
        proves --limit CARRIES a value; only this proves the value is a count."""
        with mock.patch.object(gate, "receipts",
                               side_effect=AssertionError("ledger read first")):
            rc, text = self._run(["list", "--limit", "abc"])
        self.assertEqual(rc, 2)
        self.assertIn("wants a number", text)
        rc_ok, _ = self._run(["list", "--limit", "2"])  # CONTROL: a real one works
        self.assertEqual(rc_ok, 0)

    def test_run_help_returns_before_suite_or_receipt_effects(self):  # noqa: VACUOUS_ASSERTION — a planted receipt proves the ledger is writable; exact line-count stability and the forbidden owner call prove help has no effects
        self.mint("Ran 1 test in 0.1s", "", "OK")
        with open(gate.receipts_path(), encoding="utf-8") as fh:
            before = len(fh.readlines())
        with mock.patch.object(gate, "run",
                               side_effect=AssertionError("suite started")):
            rc, text = self._run(["run", "--help"])
        with open(gate.receipts_path(), encoding="utf-8") as fh:
            after = len(fh.readlines())
        self.assertEqual(rc, 0)
        self.assertIn("usage: helm gate run", text)
        self.assertEqual(after, before, "help minted a stray gate receipt")

    def test_run_help_precedes_an_empty_custom_command(self):  # noqa: VACUOUS_ASSERTION — rc0 and usage prove help won; the forbidden owner call proves the empty separator did not start work
        with mock.patch.object(gate, "run",
                               side_effect=AssertionError("suite started")):
            rc, text = self._run(["run", "--help", "--"])
        self.assertEqual(rc, 0)
        self.assertIn("usage: helm gate run", text)

    def test_run_unknown_and_bad_timeout_refuse_before_suite(self):  # noqa: VACUOUS_ASSERTION — each refusal asserts rc2 and its exact parser reason while the owner call is forbidden
        for argv, reason in ((["run", "--bogus"], "unknown arg"),
                             (["run", "--timeout", "abc"], "wants a number"),
                             (["run", "--timeout", "nan"], "finite number")):
            with self.subTest(argv=argv), \
                    mock.patch.object(gate, "run",
                                      side_effect=AssertionError("suite started")):
                rc, text = self._run(argv)
            self.assertEqual(rc, 2)
            self.assertIn(reason, text)

    def test_run_preserves_command_flags_after_the_separator(self):
        with mock.patch.object(gate, "run",
                               return_value=(None, "CONTROL: owner reached")) as run:
            rc, text = self._run(["run", "--", "python3", "--help"])
        self.assertEqual(rc, 1)
        self.assertIn("CONTROL: owner reached", text)
        # focus=False is part of the pinned contract: a bare `run -- argv`
        # must NEVER arrive focused, or a caller-supplied command could ride
        # the focused kind's binding rights.
        run.assert_called_once_with(repo=None, argv=["python3", "--help"],
                                    label=None, timeout=None, focus=False,
                                    sliced=False, timings=None)

    def test_show_still_resolves_a_real_id(self):
        """The regression control for the whole class: the positional still
        works, so the refusals above are the guard and not a broken verb."""
        rc, text = self._run(["show", "0123456789abcdef"])
        self.assertEqual(rc, 1)
        self.assertIn("no minted gate receipt", text,
                      "a WELL-FORMED id must reach the ledger lookup, not the "
                      "shape check — otherwise this passes without the "
                      "positional ever being resolved")


class ReceiptNamesWhereItRan(GateBase):
    """THE THIRD AXIS. A run happens in a TREE, under an INTERPRETER, on a
    HOST, and a receipt that cannot name all three reads exactly like proof.

    The first fab-minted receipt: CPython 3.14.6, tree 25f18ffc,
    Ran 7017, FAILED with 14 — and all 14 passed on the owner's laptop, because they
    are coupled to the OTHER box. Nothing in the receipt said which box, so a
    reviewer spent a turn on phantom failures and nearly reworked a clean lane.
    """

    def _ledger(self):
        with open(gate.receipts_path(), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _rewrite(self, rows):
        with open(gate.receipts_path(), "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def test_a_minted_receipt_NAMES_the_machine_it_ran_on(self):
        row = self.mint("Ran 3 tests in 0.1s", "", "OK")
        self.assertEqual(row["host"]["node"], platform.uname().node)
        self.assertEqual(row["host"]["system"], platform.uname().system)
        self.assertIn("host=" + platform.uname().node[:24],
                      gate.evidence_line(row))

    def test_a_CUSTOM_command_receipt_still_names_its_host(self):
        """The interpreter is UNKNOWN for a custom command because helm did not
        CHOOSE it. The host has no such caveat — helm ran that command on this
        machine whatever it was — so there is no run whose box is honestly
        unknowable, and therefore none allowed to omit the field."""
        row = self.mint("Ran 1 test in 0.1s", "", "OK")
        self.assertIsNone(row["interpreter"])
        self.assertEqual(row["host"]["node"], platform.uname().node)
        self.assertIn("UNKNOWN", gate.evidence_line(row))       # interpreter
        self.assertNotIn("host=UNKNOWN", gate.evidence_line(row))

    def test_a_receipt_that_cannot_name_its_host_REFUSES_to_bind(self):
        """POSITIVE CONTROL FIRST, on the same observable: a real receipt at
        this tip BINDS. Only then is the hostless twin's refusal a measurement
        of the host field rather than of some unrelated door."""
        with serial_process(ran=9):
            good, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        state, _rid, why = gate.bind(gate.evidence_line(good), self.head)
        self.assertEqual(state, "VERIFIED", why)
        self.assertIn(platform.uname().node[:24], why)

        rows = self._ledger()
        blind = dict(rows[-1], host={})
        blind["id"] = gate._receipt_id(blind)       # self-consistent, hostless
        self._rewrite(rows + [blind])
        state, rid, why = gate.bind("gate:" + blind["id"], self.head)
        self.assertEqual(state, "REFUSED")
        self.assertEqual(rid, blind["id"])
        self.assertIn("does not name the HOST", why)
        self.assertIn("mint a receipt that names its machine", why.lower())
        # a lane room refuses a whole suite, so its road is a focused round
        self.assertIn("`helm gate run --focus`", why)
        self.assertNotIn("Re-run `helm gate run`", why)

    def test_a_LEGACY_receipt_minted_before_the_host_existed_REFUSES(self):
        """Absence is refused, never grandfathered: "it was probably this box"
        is exactly the inference a receipt exists to make unnecessary."""
        with serial_process(ran=9):
            good, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        rows = self._ledger()
        legacy = {k: v for k, v in rows[-1].items() if k != "host"}
        legacy["v"] = 3
        legacy["id"] = gate._receipt_id(legacy)
        self._rewrite(rows + [legacy])
        state, _rid, why = gate.bind("gate:" + legacy["id"], self.head)
        self.assertEqual(state, "REFUSED")
        self.assertIn("does not name the HOST", why)

    def test_the_host_is_BOUND_into_the_receipt_identity(self):
        """An edited `node` must stop resolving, or "which box ran this" is a
        field anyone can rewrite after the fact — the same law the base-check
        verdict earned."""
        row = self.mint("Ran 3 tests in 0.1s", "", "OK")
        rows = self._ledger()
        rows[-1]["host"] = dict(rows[-1]["host"], node="some-other-box")
        self._rewrite(rows)
        got, err = gate.by_id(row["id"])
        self.assertIsNone(got)
        # PRESENT-AND-UNRECOMPUTABLE, not absent. These rows ARE in the
        # ledger; only their bound field was edited. "no minted gate
        # receipt" is the ABSENT sentence, and by-id-conflates-absent-
        # with-unverifiable split the two on purpose — asserting the
        # tamper text here means this arm now fails if anyone ever
        # re-conflates them, which the old assertion could not do.
        self.assertIn("IS in the ledger", err)
        self.assertIn("CHANGED after it was minted", err)

    def test_the_v4_bump_did_not_UNBIND_the_earlier_fields(self):
        """A version bump that WEAKENS the hash is this file's own defect
        class. v4 must still bind the v2 failure list and the v3 base-check."""
        row = self.mint("Ran 3 tests in 0.1s", "", "FAILED (failures=1)")
        self.assertEqual(row["v"], 4)
        stored = self._ledger()
        untouched, err = gate.by_id(row["id"])
        self.assertEqual(untouched["id"], row["id"], err)   # resolves as minted

        self._rewrite([dict(stored[-1], failures=[
            {"kind": "FAIL", "test": "x.y.z", "traceback": "frame"}])])
        got, err = gate.by_id(row["id"])
        self.assertIsNone(got, "the failure list is not bound into the v4 id")
        self.assertIn("IS in the ledger", err)
        self.assertIn("CHANGED after it was minted", err)

        self._rewrite([dict(stored[-1], failures_unreadable=not row[
            "failures_unreadable"])])
        got, err = gate.by_id(row["id"])
        self.assertIsNone(got, "failures_unreadable is not bound into v4")
        self.assertIn("IS in the ledger", err)
        self.assertIn("CHANGED after it was minted", err)

        self._rewrite([dict(stored[-1], base_check={
            "verdict": gate.STALE_BASE, "reason": "forged"})])
        got, err = gate.by_id(row["id"])
        self.assertIsNone(got, "the base check is not bound into the v4 id")
        self.assertIn("IS in the ledger", err)
        self.assertIn("CHANGED after it was minted", err)

    def test_the_host_id_is_a_HASH_and_never_the_machine_id_itself(self):
        """An evidence line travels — into commit messages, into chat, onto a
        public remote. A raw /etc/machine-id there is a host fingerprint nobody
        chose to publish, the same shape hostpath_guard refuses for paths."""
        secret = "feedface" * 4          # a placeholder, shaped like /etc/machine-id
        path = os.path.join(self.tmp, "machine-id")
        with open(path, "w") as fh:
            fh.write(secret + "\n")
        with mock.patch.object(gate, "_MACHINE_ID_PATHS", (path,)):
            ident = gate.host()
        blob = json.dumps(ident)
        digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]
        # The blob DOES carry the discriminator, and it is the hash: the same
        # observable says what is present before it is asked what is absent.
        self.assertIn(digest, blob)
        self.assertTrue(ident["id"])
        self.assertTrue(digest)
        self.assertEqual(ident["id"], digest)
        self.assertNotIn(secret, blob)

    def test_a_box_with_no_machine_id_says_so_instead_of_inventing_one(self):
        path = os.path.join(self.tmp, "machine-id")
        with open(path, "w") as fh:
            fh.write("aaaa\n")
        with mock.patch.object(gate, "_MACHINE_ID_PATHS", (path,)):
            present = gate.host()
        self.assertTrue(present["id"])          # the field DOES populate
        with mock.patch.object(gate, "_MACHINE_ID_PATHS",
                               (os.path.join(self.tmp, "absent"),)):
            ident = gate.host()
        self.assertEqual(ident["id"], "")
        self.assertEqual(ident["node"], platform.uname().node)

    def test_gate_show_NAMES_the_host_and_says_so_when_it_cannot(self):
        row = self.mint("Ran 3 tests in 0.1s", "", "OK")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            gate._cmd_show([row["id"]])
        self.assertIn("host", out.getvalue())
        self.assertIn(platform.uname().node, out.getvalue())
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            gate._cmd_show([row["id"], "--json"])
        self.assertIn(platform.uname().node, out.getvalue())
        rows = self._ledger()
        legacy = {k: v for k, v in rows[-1].items() if k != "host"}
        legacy["v"] = 3
        legacy["id"] = gate._receipt_id(legacy)
        self._rewrite(rows + [legacy])
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            gate._cmd_show([legacy["id"]])
        self.assertIn("predates host recording", out.getvalue())

    # ---- the checkout the run happened in (#159's axis on the line that
    # travels). The integrator had to ask seats in open chat, twice in one day,
    # to "confirm each gate receipt was produced FROM THE LANE, not from a
    # shell in the shared checkout". The line itself should answer that.

    def test_the_evidence_line_NAMES_the_checkout_the_run_happened_in(self):
        row = self.mint("Ran 3 tests in 0.1s", "", "OK")
        self.assertIn("room=repo", gate.evidence_line(row))

    def test_two_rooms_at_ONE_tree_are_told_apart_only_by_the_room(self):
        """The control that shows why `tree=` alone cannot answer it: a lane
        room and the shared checkout sitting at the same commit produce the
        SAME tree id, so a reader comparing tree hashes learns nothing about
        which room ran the suite."""
        room = os.path.join(self.tmp, "lane-room")
        subprocess.run(("git", "worktree", "add", "--detach", room, self.head),
                       cwd=self.repo, capture_output=True, text=True)
        here = self.mint("Ran 3 tests in 0.1s", "", "OK")
        there, err = gate.run(repo=room, argv=[sys.executable]
                              + _emit("Ran 3 tests in 0.1s", "", "OK"))
        self.assertIsNone(err, err)
        self.assertEqual(here["tree"], there["tree"])        # identical trees
        self.assertIn("room=repo", gate.evidence_line(here))
        self.assertIn("room=lane-room", gate.evidence_line(there))

    def test_the_room_is_a_basename_and_never_a_host_path(self):
        """`hostpath_guard` REFUSES a public push carrying `/home/<user>/`, and
        an evidence line ends up in commit messages. The full path stays in
        repo_id where `gate show` reads it; the line carries the basename."""
        row = self.mint("Ran 3 tests in 0.1s", "", "OK")
        line = gate.evidence_line(row)
        self.assertIn("room=repo", line)        # the field IS on the line
        self.assertNotIn("/", line.split("room=")[1].split(" |")[0])
        self.assertNotIn(self.tmp, line)
        self.assertTrue(row["repo_id"])
        self.assertEqual(row["repo_id"], os.path.realpath(self.repo))

    def test_the_whole_line_still_PASSES_the_verdict_evidence_door(self):
        """Every field added here spends a reviewer's evidence budget, so the
        bar is the REAL door and not a number invented in this file: the line
        must survive `mark_verdict`'s own length rule at its worst case — long
        room name, skip count, stale-base suffix — with the gate token excluded
        exactly as that rule excludes it."""
        row = dict(self.mint("Ran 5115 tests in 154.7s", "", "OK"),
                   repo_id="/tmp/" + "lane-with-a-very-long-name" * 4,
                   skipped=8, status="FAILED",
                   base_check={"verdict": gate.STALE_BASE, "trunk": "a" * 40,
                               "reason": "x"})
        line = gate.evidence_line(row)
        self.assertIn("gate:" + row["id"], line)    # a real, whole line
        self.assertIn("STALE BASE", line)
        self.assertNotIn("\n", line)
        budgeted = dispatches._GATE_TOKEN_RE.sub("", line)
        self.assertGreater(len(budgeted), 0)
        self.assertLessEqual(len(budgeted), 256, budgeted)
        # And the two fields this lane added are a small part of that: the
        # cost lives in the stale-base sentence, which predates them.
        without = gate.evidence_line(dict(row, host={}, repo_id=""))
        self.assertLess(len(line) - len(without), 50)


class TheMintRefusesACrossTreeRun(GateBase):
    """`cli.which_helm_warning` is deliberately detect-and-report. The MINT is
    the one place that is not enough, because the receipt authorises a land.

    Every arm drives `_cross_tree_refusal` through the resolved repo, and the
    cross-tree condition is injected at `which_helm_warning` — the single
    place that decides it — so no test has to build two checkouts to describe
    a two-checkout world.
    """

    WARN = "helm on PATH is /other/tree, you are standing in /here"

    def setUp(self):
        super().setUp()
        # THIS CLASS OWNS THAT VARIABLE, so it takes it back from the shared
        # fixture. `helm_tree` sets HELM_CROSS_TREE_GATE=1 because a fixture
        # repo can never BE the running binary's tree and every other arm in the
        # estate needs its gate to run — but the override is precisely what nine
        # arms below are ABOUT, and inheriting it made each of them measure an
        # admitted run. The mark itself stays: these arms inject the cross-tree
        # condition at `which_helm_warning`, so what the fixture repo looks like
        # on disk is not their subject.
        os.environ.pop("HELM_CROSS_TREE_GATE", None)

    def _cross(self, warning=WARN):
        # `gate` imports cli INSIDE the function, so there is no `gate.cli`
        # attribute to patch — the owner of the name is helm.cli itself.
        return mock.patch("helm.cli.which_helm_warning", return_value=warning)

    def test_a_cross_tree_mint_refuses_and_says_why(self):
        with self._cross():
            err = gate._cross_tree_refusal("/here")
        self.assertIn("refusing to mint a receipt", err)
        self.assertIn("authorise a land it did not test", err)
        self.assertIn("/here", err)                 # the EFFECTIVE repo, named
        self.assertIn(self.WARN, err)               # the detector's own words

    def test_a_same_tree_mint_proceeds(self):
        """POSITIVE CONTROL. Without it every arm here would pass against a
        function that refused unconditionally."""
        with self._cross(warning=None):
            self.assertIsNone(gate._cross_tree_refusal("/here"))
        with self._cross():                     # same observable, refuses
            self.assertIn("refusing to mint",
                          gate._cross_tree_refusal("/here") or "")

    def test_the_refusal_reads_the_resolved_repo_not_a_flag_token(self):
        """Finding 1. The first draft asked whether the string
        "--repo" was in argv; `run()` resolves `repo or os.getcwd()`, so a
        bare `--repo` or `--repo ""` presented a token while gating the cwd.
        The function takes the RESOLVED value and derives nothing itself."""
        # PIN THE AST, NOT THE TEXT. The first version of this arm asserted
        # "--repo" was absent from the source and RED immediately — the
        # docstring above explains the old bug using that exact literal. A
        # source pin that can match its own explanation is not a pin. Parsing
        # and dropping the docstring node asks about the CODE.
        fn = ast.parse(textwrap.dedent(
            inspect.getsource(gate._cross_tree_refusal))).body[0]
        body = fn.body[1:] if (isinstance(fn.body[0], ast.Expr)
                               and isinstance(fn.body[0].value, ast.Constant)
                               and isinstance(fn.body[0].value.value, str)
                               ) else fn.body
        code = "\n".join(ast.dump(n) for n in body)
        # POSITIVE CONTROL ON THE SAME OBSERVABLE FIRST: the dump really does
        # carry this function's code, so the two absences below are facts about
        # the body and not about an empty string.
        self.assertIn("which_helm_warning", code)
        self.assertNotIn("--repo", code)     # derives no repo from argv
        self.assertNotIn("getcwd", code)     # and none from the process
        # AND THE DOCSTRING REALLY DOES CONTAIN IT — so the two assertions
        # above are about the body, proven, not about a literal that happens
        # to be absent everywhere.
        self.assertIn("--repo", ast.get_docstring(fn) or "")
    def test_the_decision_follows_the_repo_it_was_handed(self):
        """THE BEHAVIOURAL HALF, kept in its OWN test so it fails apart from
        the pin above — folded together, one failure could not say which check
        bit, and a pin that is never exercised alone is a pin nobody trusts.

        The first draft compared two reason strings while the detector returned
        a CONSTANT, so `cwd=repo` -> `cwd=os.getcwd()` left it green and only
        the AST pin caught the swap. Here the detector answers per-tree, so the
        argument changes the OUTCOME rather than only the wording."""
        def per_tree(cwd=None, package_dir=None):
            return self.WARN if cwd == "/tree-a" else None
        with mock.patch("helm.cli.which_helm_warning", per_tree):
            self.assertIn("refusing to mint",
                          gate._cross_tree_refusal("/tree-a") or "")
            self.assertIsNone(gate._cross_tree_refusal("/tree-b"))

    def test_the_override_admits_but_cannot_run_silently(self):
        """Finding 3. An escape hatch nobody can see is not one."""
        with self._cross():                     # CONTROL: unset, it refuses
            self.assertIn("refusing to mint",
                          gate._cross_tree_refusal("/here") or "")
        os.environ["HELM_CROSS_TREE_GATE"] = "1"
        buf = io.StringIO()
        with self._cross(), contextlib.redirect_stderr(buf):
            self.assertIsNone(gate._cross_tree_refusal("/here"))
        self.assertIn("CROSS-TREE RUN ADMITTED", buf.getvalue())
        self.assertIn("/here", buf.getvalue())

    def test_the_quiet_flag_cannot_silence_the_override_notice(self):
        """THE BYPASS ITSELF: the two knobs together used to admit in silence.
        They answer different questions and one must not disable the other."""
        os.environ["HELM_CROSS_TREE_GATE"] = "1"
        os.environ["HELM_NO_TREE_WARNING"] = "1"
        buf = io.StringIO()
        with self._cross(), contextlib.redirect_stderr(buf):
            gate._cross_tree_refusal("/here")
        self.assertIn("CROSS-TREE RUN ADMITTED", buf.getvalue())

    def test_the_quiet_flag_does_not_admit_the_run_by_itself(self):
        """HELM_NO_TREE_WARNING quiets an advisory; it never spends the
        authority of a receipt."""
        os.environ["HELM_NO_TREE_WARNING"] = "1"
        with self._cross():
            self.assertIn("refusing to mint",
                          gate._cross_tree_refusal("/here") or "")

    def test_only_the_exact_value_one_admits(self):
        # UNCONDITIONAL POSITIVE FIRST, then the admit, then the loop. The
        # loop's own assertions all live inside subTest, so on their own a
        # guard that refused EVERYTHING — including the legitimate "1" — would
        # satisfy every iteration. Both halves are pinned here, outside it.
        with self._cross():
            self.assertIn("refusing to mint",
                          gate._cross_tree_refusal("/here") or "")
        os.environ["HELM_CROSS_TREE_GATE"] = "1"
        with self._cross(), contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(gate._cross_tree_refusal("/here"))
        for value in ("0", "", "true", "yes", "1 "):
            with self.subTest(value=value):
                os.environ["HELM_CROSS_TREE_GATE"] = value
                with self._cross():
                    self.assertIn("refusing to mint",
                                  gate._cross_tree_refusal("/here") or "")

    def test_run_refuses_before_it_takes_a_fifo_position(self):
        """PLACEMENT IS THE POINT. A refused cross-tree run must start nothing
        and queue nothing — the fleet cap is 2 and a wrong-tree run that took a
        slot would starve a right-tree one."""
        acquired = []
        with self._cross(), \
                mock.patch.object(gate, "_acquire_gate",
                                  side_effect=lambda r: acquired.append(r)
                                  or (1, None)):
            row, err = gate.run(repo=self.tmp)
        self.assertIsNone(row)
        self.assertIn("refusing to mint", err)
        self.assertEqual(acquired, [])          # never queued
        # SAME OBSERVABLE, POSITIVE: the identical call DOES queue when the
        # trees agree — so the empty list above is refused-early, not a fixture
        # whose _acquire_gate is simply never reachable.
        with self._cross(warning=None), \
                mock.patch.object(gate, "_acquire_gate",
                                  side_effect=lambda r: acquired.append(r)
                                  or (None, "stop here")):
            _row, err2 = gate.run(repo=self.tmp)
        self.assertEqual(err2, "stop here")
        self.assertEqual(len(acquired), 1)

    def test_a_same_tree_run_still_reaches_the_fifo(self):
        """The control for the arm above: the same call DOES queue when the
        trees agree, so `acquired == []` there means refused-early and not a
        fixture that never queues."""
        acquired = []
        with self._cross(warning=None), \
                mock.patch.object(gate, "_acquire_gate",
                                  side_effect=lambda r: acquired.append(r)
                                  or (None, "stop here")):
            _row, err = gate.run(repo=self.tmp)
        self.assertEqual(err, "stop here")
        self.assertEqual(len(acquired), 1)

    def test_the_reviewers_three_exact_repros_through_the_cli(self):
        """The three reported repros, driven end-to-end and answered in their terms.

        THE TRIPWIRE ON THIS TEST: `_acquire_gate` is stubbed to a refusal, so
        a REGRESSION cannot spawn a real suite from a unit test. Without it,
        the day this guard breaks is the day the suite launches a whole-suite
        run on someone's laptop — the exact cost the guard exists to prevent.

        TWO OF THE THREE MOVED SINCE THAT REVIEW and it is worth being exact:
        `--repo` with no value and `--label --repo` are now refused a rung
        EARLIER, by `cli.guard_tail`'s valued-flag check, which landed after
        that review. `--repo ""` is the one that still reaches `run()` — an
        empty string is not a flag token, so it passes the tail check and
        resolves to the cwd. All three are pinned; only their reasons differ.
        """
        from helm import cli
        # UNCONDITIONAL CONTROL: the stub string IS reachable. A same-tree run
        # walks past the guard into `_acquire_gate` and prints it, so its
        # ABSENCE in each repro below is the guard biting — not a marker that
        # could never have appeared.
        with self._cross(warning=None), \
                mock.patch.object(gate, "_acquire_gate",
                                  return_value=(None, "SUITE WOULD HAVE RUN")):
            ctl = io.StringIO()
            with contextlib.redirect_stderr(ctl):
                cli.main(["gate", "run", "--repo", self.tmp])
        self.assertIn("SUITE WOULD HAVE RUN", ctl.getvalue())
        with self._cross(), \
                mock.patch.object(gate, "_acquire_gate",
                                  return_value=(None, "SUITE WOULD HAVE RUN")):
            for argv, expect in (
                    (["gate", "run", "--repo"], "wants a value"),
                    (["gate", "run", "--label", "--repo"], "wants a value"),
                    (["gate", "run", "--repo", ""], None)):
                with self.subTest(argv=argv):
                    buf, ebuf = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(buf), \
                            contextlib.redirect_stderr(ebuf):
                        rc = cli.main(argv)
                    out = buf.getvalue() + ebuf.getvalue()
                    self.assertNotIn("SUITE WOULD HAVE RUN", out)
                    self.assertNotEqual(rc, 0)
                    self.assertIn(expect or "refusing to mint", out)

    def test_the_refusal_travels_as_json_when_json_was_asked_for(self):
        """Finding 2, answered by PLACEMENT: returning a reason lets
        `_cmd_run`'s existing error path render it. A second printer inside the
        guard would have re-introduced the prose/rc2 shape."""
        buf = io.StringIO()
        with self._cross(), contextlib.redirect_stdout(buf):
            rc = gate._cmd_run(["--json", "--repo", self.tmp])
        self.assertEqual(rc, 1)
        payload = json.loads(buf.getvalue())
        self.assertIs(payload["minted"], False)
        self.assertIn("refusing to mint", payload["reason"])


class InflightCensusTest(GateBase):
    """The TYPED gate census (#112 cure). `inflight()` swallowed an
    unreadable marker directory into None, so task/112's read 4 printed "no
    gate running" about a directory it never saw — the reproduction,
    and the third instance of one class in one night (`seat_homes.walk`,
    `dispatches.stop_candidate`): a primitive that swallows "could not look"
    turns every honest consumer into a fabricator. The census carries THREE
    states — a live owner, a TRUE empty, UNREADABLE-with-reason — and no two
    may ever collapse; `inflight()` stays as the byte-compatible projection
    so the old callers keep their old observable.

    THE BLIND ARMS PATCH THE READ, NEVER chmod 000: chmod does not bind
    root, so under a root-run suite a chmod arm is vacuous — the patch
    raises the same PermissionError for every uid."""

    def deny_listdir(self):
        """os.listdir raises PermissionError for THIS repo's marker dir and
        answers honestly everywhere else — the exact probe."""
        d = gate.inflight_dir(self.repo)
        real = os.listdir

        def deny(path):
            if str(path) == str(d):
                raise PermissionError(13, "Permission denied", str(path))
            return real(path)
        return mock.patch.object(gate.os, "listdir", side_effect=deny)

    def deny_open(self, target):
        """builtins.open raises PermissionError for ONE path only."""
        real = open

        def deny(file, *args, **kwargs):
            if str(file) == str(target):
                raise PermissionError(13, "Permission denied", str(target))
            return real(file, *args, **kwargs)
        return mock.patch("builtins.open", side_effect=deny)

    def test_THREE_states_and_NO_TWO_COLLAPSE(self):
        """LIVE / EMPTY / UNREADABLE driven from real room state, pairwise
        distinct. Both shapes of TRUE empty land on GATE_EMPTY: a marker dir
        that has never been created (every never-gated room in the fleet)
        and one left behind by a closed owner (`_inflight_close` removes
        only its file) — calling either UNREADABLE would print "could not
        measure" on every clean stop."""
        missing = gate.inflight_census(self.repo)
        self.assertEqual(missing, (gate.GATE_EMPTY, None, None))
        nonce = gate._inflight_open(self.repo)
        self.assertIsNotNone(nonce, "the fixture could not register an owner")
        try:
            live = gate.inflight_census(self.repo)
        finally:
            gate._inflight_close(self.repo, nonce)
        closed = gate.inflight_census(self.repo)
        self.assertTrue(os.path.isdir(gate.inflight_dir(self.repo)),
                        "the close must leave the dir for the empty-dir shape")
        self.assertEqual(closed, (gate.GATE_EMPTY, None, None))
        self.assertEqual(live.state, gate.GATE_LIVE)
        self.assertEqual(live.live[0], os.getpid())
        self.assertIsNone(live.reason, "a fully-read census carries no reason")
        with self.deny_listdir():
            blind = gate.inflight_census(self.repo)
        self.assertEqual(blind.state, gate.GATE_UNREADABLE)
        self.assertIsNone(blind.live)
        self.assertIn("could not be listed", blind.reason)
        self.assertIn("Permission denied", blind.reason)
        self.assertEqual(len({missing.state, live.state, blind.state}), 3,
                         "two census states collapsed")

    def test_an_unreadable_OWNER_FILE_is_UNREADABLE_not_a_measured_empty(self):
        """The dir lists fine; ONE owner file inside refuses to open. The
        old code `continue`d — an unreadable owner was no owner — which let
        a permissions accident un-guard a room with a running gate. Bytes
        READ and REJECTED (garbage JSON) stay a measured no-owner: that is
        the stale-marker law, not blindness."""
        nonce = gate._inflight_open(self.repo)
        self.assertIsNotNone(nonce)
        target = os.path.join(gate.inflight_dir(self.repo), nonce + ".json")
        try:
            with self.deny_open(target):
                blind = gate.inflight_census(self.repo)
        finally:
            gate._inflight_close(self.repo, nonce)
        self.assertEqual(blind.state, gate.GATE_UNREADABLE)
        self.assertIsNone(blind.live)
        self.assertIn(nonce, blind.reason)
        self.assertIn("Permission denied", blind.reason)
        # the CONTROL pair: garbage bytes in the same slot are MEASURED
        d = gate.inflight_dir(self.repo)
        with open(os.path.join(d, "junk.json"), "w", encoding="utf-8") as fh:
            fh.write("not json")
        try:
            measured = gate.inflight_census(self.repo)
        finally:
            os.remove(os.path.join(d, "junk.json"))
        self.assertEqual(measured, (gate.GATE_EMPTY, None, None),
                         "rejected bytes are a stale marker, never blindness")

    def test_an_unreadable_LEGACY_marker_degrades_too(self):
        """The single-file source is a second read with the same law; an
        absent legacy file (the normal state, FileNotFoundError) stays a
        TRUE empty."""
        legacy = gate.inflight_path(self.repo)
        with self.deny_open(legacy):
            blind = gate.inflight_census(self.repo)
        self.assertEqual(blind.state, gate.GATE_UNREADABLE)
        self.assertIn("legacy marker", blind.reason)
        self.assertEqual(gate.inflight_census(self.repo),
                         (gate.GATE_EMPTY, None, None))

    def test_a_LIVE_owner_outranks_a_blind_source_and_the_blindness_is_NAMED(self):
        """Precedence with a reason riding along: a legacy owner proves the
        room is gating even while the marker dir refuses to list, so the
        state is LIVE — a positive proof outranks an unproven absence — and
        the reason still names the source that could not be read, because a
        partial census is a fact worth reporting even when the answer is
        already YES."""
        legacy = gate.inflight_path(self.repo)
        with open(legacy, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "ts": "2026-08-05T00:00:00Z"}, fh)
        try:
            with self.deny_listdir():
                partial = gate.inflight_census(self.repo)
        finally:
            os.remove(legacy)
        self.assertEqual(partial.state, gate.GATE_LIVE)
        self.assertEqual(partial.live, (os.getpid(), "2026-08-05T00:00:00Z"))
        self.assertIn("could not be listed", partial.reason)

    def test_inflight_is_a_BYTE_COMPATIBLE_projection_for_old_callers(self):
        """THE PROJECTION CONTRACT, proven not asserted: every old caller of
        `inflight()` (test_inflight_gate.py's hook among them) must observe
        exactly the pre-census behaviour — LIVE -> the (pid, ts) tuple,
        empty -> None, and UNREADABLE -> None. That last None is the very
        conflation the census escapes, preserved HERE deliberately:
        the probe measured None pre-fix and this pins None post-fix,
        so no caller of the projection changes behaviour by one byte."""
        self.assertIsNone(gate.inflight(self.repo))       # empty -> None
        nonce = gate._inflight_open(self.repo)
        self.assertIsNotNone(nonce)
        try:
            live = gate.inflight(self.repo)
            census = gate.inflight_census(self.repo)
        finally:
            gate._inflight_close(self.repo, nonce)
        self.assertIsInstance(live, tuple)
        self.assertEqual(len(live), 2)
        self.assertIsInstance(live[0], int)
        self.assertIsInstance(live[1], str)
        self.assertEqual(live[0], os.getpid())
        self.assertEqual(live, census.live,
                         "the projection and the census disagree on LIVE")
        with self.deny_listdir():
            self.assertEqual(gate.inflight_census(self.repo).state,
                             gate.GATE_UNREADABLE)  # the state IS unreadable
            self.assertIsNone(gate.inflight(self.repo),
                              "unreadable must project to None — the OLD "
                              "observable — never raise or leak the census")
        self.assertIsNone(gate.inflight(self.repo))       # and back to empty


if __name__ == "__main__":
    unittest.main()


class TreeBeforeTimeTest(GateBase):
    """task/1314 face 1: verification compared HEAD, then TIME, and never the
    TREE the receipt already carries.

    WHY head != tip IS THE ORDINARY CASE. `fab gate` snapshots a dirty working
    tree into a synthetic commit, so gate-then-commit — gate what you have,
    commit what passed — always lands in the fallback. MEASURED: an
    approve on task/1287 was refused for arriving ten minutes before its own
    review row, on a receipt naming the reviewed tip's EXACT tree, with an
    empty diff between the two commits. Timestamp order is a proxy for "was
    this run on this content"; the tree answers it outright.

    THE FAST PATH MAY ONLY UPGRADE, and the three negatives below are the
    guarantee — each must land on the SAME refusal it produced before this
    existed, so nothing is reordered."""

    def _green(self):
        """A receipt that CAN bind — which a custom argv can never be.

        My first fixture passed argv=[sys.executable, "-c", ...]. That is the
        idiom of the neighbouring tests, and it is right THERE because those
        tests assert REFUSED: a custom command means helm did not choose the
        interpreter, so bind() refuses on the INTERPRETER clause. Every one of
        my four arms therefore tripped a clause EARLIER than the tree
        comparison, and the branch under test never ran once — while the arms
        looked like they were exercising it. The shared process seam preserves
        helm's canonical serial argv and interpreter while replacing only child
        execution."""
        with serial_process():
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "OK",
                         "fixture broken: this helper promises its callers a "
                         "GREEN receipt and the suite did not come back OK, "
                         "so every arm below is measuring a red it did not "
                         "arrange")
        # THE FIXTURE'S OWN PRECONDITION, asserted rather than assumed: this
        # receipt must bind at its own head, or every arm below is measuring
        # a refusal none of them wrote.
        self.assertEqual(
            gate.bind(gate.evidence_line(row), row["head"])[0], "VERIFIED",
            "fixture cannot bind at its own head — the arms below are void")
        return row

    def _same_tree_successor(self):
        """A commit with a DIFFERENT sha and an IDENTICAL tree — which is what
        an empty commit is, and what a fab snapshot is in practice."""
        self._git("commit", "-q", "--allow-empty", "-m", "same tree, new sha")
        tip = self._git("rev-parse", "HEAD").strip()
        return tip

    def test_a_receipt_whose_TREE_matches_binds_though_its_head_differs(self):
        row = self._green()
        # unconditional positive on the observable under test
        self.assertEqual(
            gate.bind(gate.evidence_line(row), row["head"])[0], "VERIFIED")
        tip = self._same_tree_successor()
        self.assertNotEqual(row["head"], tip, "fixture: the shas must differ")
        self.assertEqual(
            row["tree"], self._git("rev-parse", tip + "^{tree}").strip(),
            "fixture: the trees must be identical")
        # a review opened AFTER the receipt: the timestamp leg would refuse
        later = "2999-01-01T00:00:00Z"
        state, _rid, why = gate.bind(
            gate.evidence_line(row), tip,
            repo_id=os.path.join(self.repo, ".git"), reviewed_ts=later)
        self.assertEqual(state, "VERIFIED", why)
        self.assertIn("tree", why)

    def test_a_DIFFERENT_tree_is_NOT_upgraded(self):
        """The load-bearing negative: same call, content actually changed."""
        row = self._green()
        self.assertEqual(
            gate.bind(gate.evidence_line(row), row["head"])[0], "VERIFIED",
            "positive control dead: the receipt must bind at its own head")
        with open(os.path.join(self.repo, "newfile.txt"), "w") as fh:
            fh.write("content the receipt never saw\n")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "a real change")
        tip = self._git("rev-parse", "HEAD").strip()
        self.assertNotEqual(
            row["tree"], self._git("rev-parse", tip + "^{tree}").strip(),
            "fixture: the trees must differ")
        state, _rid, why = gate.bind(
            gate.evidence_line(row), tip,
            repo_id=os.path.join(self.repo, ".git"),
            reviewed_ts="2999-01-01T00:00:00Z")
        self.assertEqual(state, "REFUSED", why)
        self.assertIn("postdate", why)

    def test_an_UNRESOLVABLE_repo_refuses_before_descendant_binding(self):
        """Repository authority is an earlier axis than ancestry or time.

        Omitting the repo still uses a local receipt's durable origin. Naming
        an unreadable or relative standing repository cannot fall through and
        spend that different repository's authority.
        """
        row = self._green()
        self.assertEqual(
            gate.bind(gate.evidence_line(row), row["head"])[0], "VERIFIED",
            "positive control dead")
        tip = self._same_tree_successor()
        state, _rid, why = gate.bind(
            gate.evidence_line(row), tip, repo_id=None,
            reviewed_ts="2999-01-01T00:00:00Z")
        self.assertEqual(state, "REFUSED", why)
        self.assertIn("postdate", why)
        for repo_id in ("/nope/not/a/repo/.git", "relative/.git"):
            with self.subTest(repo_id=repo_id):
                state, _rid, why = gate.bind(
                    gate.evidence_line(row), tip, repo_id=repo_id,
                    reviewed_ts="2999-01-01T00:00:00Z")
                self.assertEqual(state, "REFUSED", why)
                self.assertIn("receipt authority is UNKNOWN", why)

    def test_the_head_equals_tip_path_is_untouched(self):
        """The original fast path still answers first and still says what it
        always said — the new one sits beside it, not in front of it."""
        row = self._green()
        state, _rid, why = gate.bind(gate.evidence_line(row), row["head"])
        self.assertEqual(state, "VERIFIED", why)
        self.assertNotIn("tree", why)


class BaseCheckDoesNotAssertAuthorshipTest(StaleBaseFixture):
    """task/1314 face 2: the mb == trunk branch told the author their diff
    caused the failures, on evidence that cannot carry it.

    THE TWO RUNS ARE NOT THE SAME SHAPE. The reference run is
    `unittest <those ids>` — focused. The failing run was
    `unittest discover -s tests -t .` — the whole suite. So a failure born of
    suite COMPOSITION (ordering, cross-test state, a fixture another module
    leaked) passes the focused control and fails the real run, and the old
    sentence attributed it to the diff as fact.

    PROVEN with two receipts, both named on task/1314
    (the ids live in the ledger; a fresh clone cannot resolve them, which is
    why they are cited there and not here): both carried that sentence on a
    DOCS-ONLY tree
    whose entire diff was 168 lines of .md, which cannot make a Python test
    fail. Every author who read it was told a machine-wide defect
    was their own.

    NOTE WHAT THE FIXTURE HAS TO DO, because it is the finding restated: the
    trunk leg must be MOCKED green. A lane that touches only code.txt cannot
    naturally make a probe fail that passes on trunk — this branch fires in a
    state a well-behaved diff does not produce, which is exactly why the
    causal claim was unsafe.

    The VERDICT is deliberately unchanged: mb == trunk really does prove the
    base is not stale, which is all this check is chartered to decide."""

    def test_the_sentence_states_the_measurement_and_not_authorship(self):
        self._seed_main(expect="v2", app="v1")   # the probe fails in the suite
        self._lane()                             # forked off the CURRENT tip
        with mock.patch.object(gate, "_test_file",
                               return_value="tests/test_stale_probe.py"), \
             mock.patch.object(gate, "_changed_files", return_value=set()), \
             mock.patch.object(gate, "_reference_run") as ref:
            ref.return_value = ({"status": "OK", "rc": 0, "failed": set(),
                                 "unreadable": False,
                                 "unreadable_reason": None}, None)
            require_supervisor()
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        # POSITIVE CONTROL: the branch under test is the one that ran.
        self.assertEqual(check["verdict"], gate.NOT_STALE)
        self.assertIn("is based on current trunk", check["reason"])
        # THE CURE: no authorship claim, and the gap is named.
        self.assertNotIn("need this lane's changes", check["reason"])
        self.assertIn("does not show the lane caused them", check["reason"])
        self.assertIn("whole suite", check["reason"])


class DirtClauseTest(unittest.TestCase):
    """The one rendering of uncommitted content, three states per half.

    A renderer that reads `row["dirty"]` alone calls a suite that dirtied the
    tree it ran on CLEAN — the state `bind` refuses — so these arms pin both
    halves of the bracket and both kinds of UNKNOWN, and a renderer that loses
    one goes red rather than quiet."""

    def _row(self, **over):
        row = {"dirty": False, "dirty_after": False,
               "head_after": "d" * 40, "tree_after": "e" * 40}
        row.update(over)
        return row

    def test_two_clean_measurements_say_nothing(self):  # noqa: VACUOUS_ASSERTION — silence IS the property, and every other arm in this class asserts a STRING out of the same call, so None here discriminates rather than describing an inert door
        """The control. Every arm below asserts a STRING, and a clause that is
        always present would satisfy all of them."""
        self.assertIsNone(gate.dirt_clause(self._row()))

    def test_dirty_before_names_before(self):
        clause = gate.dirt_clause(self._row(dirty=True))
        self.assertIn("DIRTY WORKTREE (before the run)", clause)

    def test_dirty_after_names_after(self):
        clause = gate.dirt_clause(self._row(dirty_after=True))
        self.assertIn("DIRTY WORKTREE (after the run)", clause)

    def test_dirty_on_BOTH_reads_names_both(self):
        """The half a one-field read or a ternary drops. A both-dirty receipt
        rendered as "before" tells a reader the tree settled down."""
        clause = gate.dirt_clause(self._row(dirty=True, dirty_after=True))
        self.assertIn("DIRTY WORKTREE (before and after the run)", clause)

    def test_a_failed_post_run_read_is_UNKNOWN_not_measured_dirt(self):
        """`_mint_result` writes dirty_after=True when the post-run read
        ERRORED — fail-closed, correct for `bind`, and a lie in a display. The
        receipt's own discriminator is that head_after/tree_after are empty on
        exactly that path."""
        clause = gate.dirt_clause(self._row(dirty_after=True, head_after="",
                                            tree_after=""))
        self.assertIn("DIRT UNKNOWN (after the run)", clause)
        self.assertIn("the post-run read did not complete", clause)
        self.assertNotIn("DIRTY WORKTREE", clause)

    def test_a_pre_bracket_receipt_is_UNKNOWN_for_the_other_reason(self):
        """"the read failed" and "there was no read" send a reader to
        different places, so they are printed apart."""
        clause = gate.dirt_clause({"dirty": False})
        self.assertIn("the receipt predates the post-run read", clause)

    def test_a_non_boolean_dirt_field_is_not_a_measurement(self):
        clause = gate.dirt_clause(self._row(dirty="yes"))
        self.assertIn("DIRT UNKNOWN (before the run)", clause)

    def test_the_evidence_line_carries_the_clause(self):
        """The placement is the whole point: this string is what `gate run`
        prints last, what a reviewer pastes into `dispatch verdict`, and what
        `gate show` re-renders days later for a reader with no scrollback."""
        row = {"id": "abc123", "head": "a" * 40, "tree": "b" * 40,
               "status": "OK", "ran": 5, "suite": True,
               "dirty": True, "dirty_after": False,
               "head_after": "a" * 40, "tree_after": "b" * 40}
        self.assertIn("DIRTY WORKTREE (before the run)", gate.evidence_line(row))

    def test_gate_list_does_not_render_dirt_a_second_time(self):
        """Two renderings of one fact drift apart, and a second one here would
        disagree with `bind` for any receipt dirty on only one half."""
        row = {"id": "abc123", "ts": "2026-09-10T00:00:00Z", "head": "a" * 40,
               "tree": "b" * 40, "status": "OK", "ran": 5, "suite": True,
               "dirty": True, "dirty_after": True,
               "head_after": "a" * 40, "tree_after": "b" * 40}
        rendered = gate._fmt(row)
        self.assertEqual(rendered.count("DIRTY WORKTREE"), 1)
        self.assertFalse(rendered.endswith(" DIRTY"))


class CarriageTest(GateBase):
    """ONE predicate for "does this receipt cover that commit", because it had
    two readers going different ways: `bind` decides authority with it and
    `gateimport.head_divergence` decides whether to warn."""

    def _commit(self, name, text):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)
        self._git("add", name)
        self._git("-c", "user.email=t@t", "-c", "user.name=t",
                  "commit", "-q", "-m", name)
        return self._git("rev-parse", "HEAD")

    def test_a_descendant_CARRIES_its_ancestor(self):
        base = self._git("rev-parse", "HEAD")
        later = self._commit("carry-a.txt", "a\n")
        verdict, _rel, _seq = gate.carriage(self.repo, base, later)
        self.assertEqual(verdict, gate.CARRIED)

    def test_a_SAME_TREE_SIBLING_does_NOT_carry(self):  # noqa: VACUOUS_ASSERTION — the fixture asserts the same-tree precondition unconditionally, and the sibling arm above proves CARRIED on this same call, so NOT_CARRIED here is a discrimination and not a door that answers one word
        """The case a tree comparison calls equal: same content, neither
        commit containing the other, and `bind` refuses it — so the one
        predicate must answer NOT_CARRIED rather than stay quiet."""
        base = self._git("rev-parse", "HEAD")
        self._commit("carry-a.txt", "a\n")
        mine = self._commit("carry-b.txt", "b\n")
        tree = self._git("rev-parse", "HEAD^{tree}")
        snapshot = self._git("-c", "user.email=t@t", "-c", "user.name=t",
                             "commit-tree", tree, "-p", base, "-m", "snapshot")
        self.assertEqual(self._git("rev-parse", snapshot + "^{tree}"),
                         self._git("rev-parse", mine + "^{tree}"),
                         "control: the fixture must build the same-tree case")
        verdict, _rel, _seq = gate.carriage(self.repo, mine, snapshot)
        self.assertEqual(verdict, gate.NOT_CARRIED)

    def test_an_underivable_sequence_is_UNKNOWN_and_not_NOT_CARRIED(self):
        """`bind` refuses both and is right to; a DISCLOSURE that said "this
        does not cover your commit" on a read that never completed would be
        inventing a measurement."""
        base = self._git("rev-parse", "HEAD")
        later = self._commit("carry-c.txt", "c\n")
        real = vcs.backend(self.repo)

        class Underivable:
            name = real.name

            def ancestry(self, *a, **k):
                return real.ancestry(*a, **k)

            def patch_sequence_containment(self, *a, **k):
                return vcs.PATCH_SEQUENCE_UNKNOWN, None, None, None

        with mock.patch.object(vcs, "backend", return_value=Underivable()):
            verdict, _rel, _seq = gate.carriage(self.repo, later, base)
        self.assertEqual(verdict, gate.CARRIAGE_UNKNOWN)


class AProjectDeclaresItsGateCommand(unittest.TestCase):
    """THE ADOPTER'S GATE. helm was always meant to help agent teams build ANY
    project, and a team USING helm must never need to know how helm is made —
    so wherever a verb assumed the helm checkout or helm's suite, that was the
    bug. `gate.SUITE` was a module constant read with no question about which
    repo `run()` had been handed, so gating a project whose suite is pnpm or
    cargo spawned python unittest discovery in a tree with no `tests/`
    directory and minted an UNKNOWN receipt about a command the project never
    asked for. Nothing refused; an unbindable receipt was the only answer.

    The fixture here is an ADOPTER, deliberately: a real git repo that ships no
    helm and declares its own command, which is why it must NOT call
    `_tmphome.helm_tree` (that mark is for a fixture standing in for helm's own
    tree). Its declared suite is a real committed shell script that writes a
    marker OUTSIDE the repo — outside so the run cannot dirty the tree it is
    supposed to bind — and whose exit status comes from a sidecar file, so one
    committed tree can be green or red without moving.

    THE SCRIPT ALSO PRINTS A UNITTEST FOOTER ON STDERR, and that is a probe and
    not scenery: a declared command must not be able to claim a count helm never
    counted.
    """

    FOOTER = "Ran 5 tests in 0.1s\\n\\nOK\\n"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-declared-gate-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        # THE SNAPSHOT OWNER, REGISTERED FIRST SO IT RUNS LAST — the same
        # ordering GateBase takes, and for the same reason: one arm here calls
        # `_tmphome.helm_tree`, whose restoration is an addCleanup and therefore
        # runs after tearDown. See `_tmphome.own_env`.
        self.addCleanup(self._restore_env)
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_ROOM"] = "main"
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_NAME"] = "declared-gate-fixture"
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.environ["HELM_PROC"] = os.path.join(self.tmp, "proc")
        os.makedirs(os.environ["HELM_PROC"])
        # ADMISSION ON A FIXTURE BOX, as GateBase: see `_tmphome.pin_admission`.
        from tests._tmphome import pin_admission
        pin_admission(self, proc=os.environ["HELM_PROC"])
        self.marker = os.path.join(self.tmp, "ran-marker")
        self.rc_file = os.path.join(self.tmp, "declared-rc")
        with open(self.rc_file, "w") as fh:
            fh.write("0\n")
        # REALPATH: the registry compares resolved paths, and a symlinked TMPDIR
        # would make this project "unregistered" for a reason unrelated to the
        # declaration under test.
        self.repo = os.path.realpath(os.path.join(self.tmp, "repo"))
        os.makedirs(self.repo)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "adopter@test")
        self._git("config", "user.name", "adopter test")
        script = os.path.join(self.repo, "run-suite.sh")
        with open(script, "w") as fh:
            # THE CWD IS RECORDED BY THE RUN ITSELF, which is the only witness
            # of it: a relative argv[0] proves the child started SOMEWHERE that
            # has the script, and `$PWD` says which tree that was — the fact a
            # linked worktree's gate turns on.
            fh.write("#!/bin/sh\n"
                     "printf 'ran %%s\\n' \"$PWD\" >> %s\n"
                     "printf '%s' >&2\n"
                     "exit \"$(cat %s)\"\n"
                     % (self.marker, self.FOOTER.replace("\n", "\\n"),
                        self.rc_file))
        os.chmod(script, 0o755)
        self._git("add", "-A")
        self._git("commit", "-qm", "the adopter's own suite")
        self.head = self._git("rev-parse", "HEAD")
        self.tree = self._git("rev-parse", "HEAD^{tree}")
        self.project = "adopter"
        self.register()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _restore_env(self):
        """The fixture's whole ENV_KEYS snapshot, put back last."""
        for key, val in self.prior.items():
            os.environ.pop(key, None)
            if val is not None:
                os.environ[key] = val

    def _git(self, *args):
        out = subprocess.run(("git",) + args, cwd=self.repo, text=True,
                             capture_output=True)
        return out.stdout.strip()

    def register(self, path=None):
        """The PROJECTION half: this repo is a registered helm project."""
        from helm import home
        d = home.global_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "registry.json"), "w") as fh:
            json.dump({"version": 1, "projects": {self.project: {
                "name": self.project, "path": path or self.repo,
                "kind": "git", "status": "active", "sessions": {}}}}, fh)

    def forget(self, path=None):
        """A registry that is READABLE and does not hold this repo — which is a
        different fact from a registry helm could not read, and the refusals say
        so separately."""
        from helm import home
        d = home.global_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "registry.json"), "w") as fh:
            json.dump({"version": 1, "projects": {}}, fh)

    def declare(self, command=("./run-suite.sh",), protocol="exit", block=None,
                path=None, name=None, extra=()):
        """The AUTHORED half: what this project says its gate command is.

        `extra` adds further authored entries as (name, path, block) triples —
        the shape a SECOND declaration inside one repository takes, which is the
        ambiguity the resolver must refuse rather than resolve by sort order.
        """
        from helm import home
        d = home.global_dir()
        os.makedirs(d, exist_ok=True)
        declared = block if block is not None else {
            "command": list(command), "protocol": protocol}
        projects = {name or self.project: {"path": path or self.repo,
                                           gate.DECLARED_GATE_FIELD: declared}}
        for other, where, what in extra:
            projects[other] = {"path": where,
                               gate.DECLARED_GATE_FIELD: what}
        with open(os.path.join(d, "registry-authored.json"), "w") as fh:
            json.dump({"version": 1, "projects": projects}, fh)

    def project_gate(self):
        """The PROJECTION carrying a `gate` block — the file a re-scan rebuilds,
        which is not where a project declares anything."""
        from helm import home
        d = home.global_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "registry.json"), "w") as fh:
            json.dump({"version": 1, "projects": {self.project: {
                "name": self.project, "path": self.repo, "kind": "git",
                "status": "active", "sessions": {},
                gate.DECLARED_GATE_FIELD: {"command": ["true"],
                                           "protocol": "exit"}}}}, fh)

    def ran_lines(self):
        """One `ran <cwd>` line per real execution of the declared script."""
        try:
            with open(self.marker) as fh:
                return fh.read().splitlines()
        except OSError:
            return []

    def ran_count(self):
        return len(self.ran_lines())

    def ran_cwds(self):
        """The working directory each run reported, in order."""
        return [line.split(" ", 1)[1] for line in self.ran_lines()
                if " " in line]

    # ------------------------------------------------------------------ arms

    def test_a_declared_command_runs_from_the_repo_root_and_binds_its_tree(self):
        """(a) The command the project declares is the command that runs, and
        the receipt it mints names the tree it ran on.

        `./run-suite.sh` IS THE ASSERTION ABOUT THE CWD. A relative argv[0]
        resolves only if the child was spawned from the repo root, so the marker
        existing is the measurement that it was; an absolute path would have
        proved nothing about where the run happened.
        """
        self.declare()
        self.assertEqual(self.ran_count(), 0)   # control: nothing has run yet
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(self.ran_count(), 1,
                         "the declared command did not run from the repo root")
        self.assertEqual(row["status"], "OK")
        self.assertEqual(row["rc"], 0)
        self.assertEqual(row["tree"], self.tree)
        self.assertEqual(row["head"], self.head)
        self.assertFalse(row["dirty"])
        self.assertTrue(row["suite"])
        self.assertEqual(row["argv"], ["./run-suite.sh"])
        self.assertEqual(row["suite_command"],
                         {"source": "registry", "argv": ["./run-suite.sh"],
                          "protocol": "exit", "project": "adopter"})
        # The identity axes a binder reads are all present and true.
        self.assertEqual(row["interpreter"]["name"], sys.implementation.name)
        self.assertTrue(row["host"]["node"])
        self.assertEqual(row["repo_id"], self.repo)
        self.assertEqual(row["id"], gate._receipt_id(row))
        # AND THE MINT'S OWN READER ADMITS IT: a row `receipts()` recomputes to
        # a different id is silently skipped, which is the failure that reads as
        # "no such receipt" downstream.
        rows, unavailable, _skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertIn(row["id"], [r["id"] for r in rows])
        self.assertIsNone(gate.row_refusal(row))

    def test_a_declared_command_that_exits_nonzero_is_FAILED(self):
        """(a, red half) THE MUST-HIT for the exit protocol: if this arm could
        not go red, the protocol would be a rubber stamp that calls every run
        green. The script prints `OK` on stderr either way, so this also proves
        the status comes from the exit status and not from the child's prose."""
        self.declare()
        with open(self.rc_file, "w") as fh:
            fh.write("3\n")
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(self.ran_count(), 1)
        self.assertEqual(row["status"], "FAILED")
        self.assertEqual(row["rc"], 3)
        self.assertIn("exited 3", row["detail"])
        # A RED RECEIPT IS AN ADMISSIBLE ROW AND NOT AUTHORITY, which are two
        # questions with two answers: `row_refusal` asks whether the row
        # describes a real run (it does — being red is part of describing one),
        # and the fold rung next door is what refuses it as authority.
        self.assertIsNone(gate._declared_row_refusal(row))
        self.assertIsNone(gate.row_refusal(row))

    def test_a_declared_command_cannot_claim_a_count_it_did_not_measure(self):
        """The forgery arm. The child prints an authentic-looking unittest
        footer on stderr — `Ran 5 tests ... OK` — and the receipt must record NO
        count, because helm counted nothing. A parsed footer here would let any
        declared command mint whatever number it liked."""
        self.declare()
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertIn("Ran 5 tests", self.FOOTER)       # the input really says it
        self.assertIsNone(row["ran"])
        self.assertIsNone(row["skipped"])
        self.assertIsNone(row["elapsed"])
        self.assertTrue(row["failures_unreadable"])
        self.assertEqual(row["failures"], [])
        self.assertIsNone(row["base_check"])
        self.assertIn("counts no tests", row["detail"])

    def test_a_declared_command_keeps_the_output_it_judged_in_the_tail_and_a_sidecar(self):  # noqa: VACUOUS_ASSERTION — the loop runs exactly twice over a literal tuple; its first pass is the unconditional red positive (assertEqual on stderr_tail holding both lines, the sidecar text, the console tee) and its second pass is the green control on the same observables
        """task/2527. The declared command prints one line to stdout and one to
        stderr and exits 1. Before the cure `run` kept only unittest's stream
        (`out = stderr`), so the stdout line went nowhere and two client-project reds
        (a973a6f73ea9e292, 304cfecb480082c4) could not be diagnosed. Now the
        receipt's `stderr_tail` holds BOTH lines, its meta names the source,
        the whole text is in `gate-command-output.txt` beside the receipt
        ledger, the console (this process's stderr) saw it, and `detail`
        names the exit code and the sidecar path.

        CONTROL: the same command exiting 0 mints OK and keeps the SAME tail,
        because a declared receipt has no failure list — the tail is the only
        diagnostic it has, red or green — where a unittest green keeps none
        (`test_a_red_receipt_keeps_the_child_stderr_and_a_green_one_does_not`).
        """
        import contextlib
        import io
        from helm import gateimport
        for rc, status in ((1, "FAILED"), (0, "OK")):
            self.declare(command=("bash", "-c",
                                  "echo THE_STDOUT_LINE; "
                                  "echo THE_STDERR_LINE >&2; exit %d" % rc))
            console = io.StringIO()
            with contextlib.redirect_stderr(console):
                require_supervisor()
                row, err = gate.run(repo=self.repo)
            self.assertIsNone(err, err)
            self.assertEqual((row["status"], row["rc"]), (status, rc))
            self.assertEqual(row["stderr_tail"],
                             "THE_STDOUT_LINE\nTHE_STDERR_LINE\n")
            self.assertEqual(row["stderr_tail_meta"],
                             {"total_bytes": 32, "truncated": False,
                              "source": gate.DECLARED_OUTPUT_SOURCE})
            sidecar = gate.command_output_path()
            self.assertEqual(os.path.basename(sidecar), gate.COMMAND_OUTPUT)
            self.assertEqual(os.path.dirname(sidecar),
                             os.path.dirname(gate.receipts_path()))
            with open(sidecar, encoding="utf-8") as fh:
                self.assertEqual(fh.read(),
                                 "THE_STDOUT_LINE\nTHE_STDERR_LINE\n")
            self.assertEqual(console.getvalue(),
                             "THE_STDOUT_LINE\nTHE_STDERR_LINE\n")
            self.assertIn("bash exited %d" % rc, row["detail"])
            self.assertIn("are in " + sidecar, row["detail"])
            # The v4 row with the extended meta still validates and re-reads.
            self.assertIsNone(gateimport._diagnostic_err(row))
            self.assertIsNone(gateimport._schema_err(row))
            self.assertIsNone(gate.row_refusal(row))
            rows, unavailable, _skipped = gate.receipts()
            self.assertIsNone(unavailable)
            self.assertIn(row["id"], [r["id"] for r in rows])

    def test_a_declared_output_longer_than_the_cap_is_a_truncated_tail_over_a_whole_sidecar(self):
        """The sidecar is the WHOLE text and the receipt is a bounded WINDOW of
        it, bounded by the same STDERR_TAIL_CAP unittest's stderr tail uses.
        The command prints more than the cap on stdout and one last line on
        stderr: the tail keeps the END (that last line survives), the meta says
        truncated and counts the whole stream, and the sidecar's size IS that
        count. Must-miss: a meta key beside `source` that no gate mints is
        still refused by the importer, so widening the grammar by one optional
        field did not open it.
        """
        import contextlib
        import io
        from helm import gateimport
        over = gate.STDERR_TAIL_CAP + 4096
        self.declare(command=("bash", "-c",
                              "head -c %d /dev/zero | tr '\\0' H; echo; "
                              "echo LAST_LINE_STDERR >&2; exit 1" % over))
        with contextlib.redirect_stderr(io.StringIO()):
            require_supervisor()
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        meta = row["stderr_tail_meta"]
        self.assertTrue(meta["truncated"])
        self.assertEqual(meta["source"], gate.DECLARED_OUTPUT_SOURCE)
        self.assertEqual(meta["total_bytes"], over + 1 + len("LAST_LINE_STDERR\n"))
        kept = row["stderr_tail"].encode("utf-8")
        self.assertLessEqual(len(kept), gate.STDERR_TAIL_CAP)
        self.assertGreater(len(kept), gate.STDERR_TAIL_FLOOR)
        self.assertTrue(row["stderr_tail"].endswith("\nLAST_LINE_STDERR\n"))
        self.assertEqual(os.path.getsize(gate.command_output_path()),
                         meta["total_bytes"])
        with open(gate.command_output_path(), encoding="utf-8") as fh:
            whole = fh.read()
        self.assertTrue(whole.startswith("H" * over))
        self.assertTrue(whole.endswith(row["stderr_tail"]))
        self.assertIsNone(gateimport._diagnostic_err(row))
        widened = dict(row, stderr_tail_meta=dict(meta, extra=1))
        self.assertIn("a gate mints exactly",
                      gateimport._diagnostic_err(widened) or "")
        blank = dict(row, stderr_tail_meta=dict(meta, source=""))
        self.assertIn("source is empty",
                      gateimport._diagnostic_err(blank) or "")

    def test_foldcheck_binds_a_declared_receipt_to_that_tip(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive on the SAME instrument runs FIRST: assertEqual(rung.verdict, PASS) over a real minted receipt. The UNKNOWN and REFUSE legs after it are must-misses on that same rung, so neither is an absence claim standing alone
        """(b) The whole point: the fold's tree-vs-receipt rung accepts this
        receipt with nothing in foldcheck changed."""
        from helm import foldcheck
        self.declare()
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        rung = foldcheck.gate_authority(self.repo, self.head,
                                        "gate:" + row["id"])
        self.assertEqual(rung.verdict, foldcheck.PASS, repr(rung))
        # MUST-MISS ON THE SAME INSTRUMENT, so the PASS above is about this
        # receipt and not about a rung that passes whatever it is handed: a
        # token no receipt carries is UNKNOWN, and a tip whose tree differs is
        # REFUSED.
        self.assertEqual(foldcheck.gate_authority(
            self.repo, self.head, "gate:" + ("0" * 16)).verdict,
            foldcheck.UNKNOWN)
        with open(os.path.join(self.repo, "later.txt"), "w") as fh:
            fh.write("a tree the receipt never saw\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "a different tree")
        moved = self._git("rev-parse", "HEAD")
        self.assertNotEqual(self._git("rev-parse", "HEAD^{tree}"), self.tree)
        self.assertEqual(foldcheck.gate_authority(
            self.repo, moved, "gate:" + row["id"]).verdict, foldcheck.REFUSE)

    def test_a_red_declared_receipt_cannot_authorize_a_fold(self):  # noqa: VACUOUS_ASSERTION — REFUSE plus assertIn("FAILED", rung.discriminator) are both positive equalities on the rung's own output; the green half of this pair is the arm directly above, on the same instrument
        """The fold rung's own must-hit: the red run above is REFUSED here, so
        the PASS is about status and not about the token resolving."""
        from helm import foldcheck
        self.declare()
        with open(self.rc_file, "w") as fh:
            fh.write("1\n")
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        rung = foldcheck.gate_authority(self.repo, self.head,
                                        "gate:" + row["id"])
        self.assertEqual(rung.verdict, foldcheck.REFUSE, repr(rung))
        self.assertIn("FAILED", rung.discriminator)

    def test_an_edited_declaration_stops_the_receipt_resolving(self):
        """The block is bound BY PRESENCE, so it cannot be improved after the
        fact: a receipt that ran `./run-suite.sh` cannot be re-labelled as
        having run the project's real suite."""
        self.declare()
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        forged = json.loads(json.dumps(row))
        forged["suite_command"]["protocol"] = "unittest"
        self.assertNotEqual(gate._receipt_id(forged), row["id"])
        forged = json.loads(json.dumps(row))
        forged["suite_command"]["argv"] = ["pnpm", "-r", "test"]
        self.assertNotEqual(gate._receipt_id(forged), row["id"])
        # AND THE ROW-LEVEL READER CATCHES THE DISAGREEMENT on its own, before
        # any id is recomputed: argv and the block must be one command.
        self.assertIn("says it ran", gate.row_refusal(forged) or "")

    def test_the_helm_source_tree_keeps_its_own_suite_with_no_registry_field(self):  # noqa: VACUOUS_ASSERTION — assertNotIn("suite_command") is the point of the arm and it is guarded by three unconditional positives on the same row: source == helm-default, v == 4, and argv == [executable, -c, pass]; a row that never minted fails those first
        """(c) THE CONTROL. helm's own tree needs no declaration and its receipt
        must not move: same argv, same version, and NO `suite_command` block —
        which is what keeps every receipt the fleet already holds resolving."""
        from tests._tmphome import helm_tree
        # The real checkout this test process is running from, which declares
        # nothing in this fixture's empty temp registry.
        import helm as helm_pkg
        real = os.path.dirname(os.path.dirname(
            os.path.abspath(helm_pkg.__file__)))
        plan, err = gate.suite_command(real)
        self.assertIsNone(err, err)
        self.assertEqual(plan["source"], "helm-default")
        self.assertEqual(plan["protocol"], "unittest")
        self.assertEqual(plan["argv"],
                         [os.path.realpath(sys.executable)] + list(gate.SUITE))
        # AND THE MINTED SHAPE, measured rather than reasoned about: a tree that
        # ships helm mints exactly what it always did.
        helm_tree(self, self.repo)
        self._git("commit", "-qm", "this fixture now ships helm")
        head = self._git("rev-parse", "HEAD")
        with mock.patch.object(gate, "SUITE", ("-c", "pass")):
            require_supervisor()
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["head"], head)
        self.assertNotIn("suite_command", row)
        self.assertEqual(row["v"], 4)
        self.assertTrue(row["suite"])
        self.assertEqual(row["argv"],
                         [os.path.realpath(sys.executable), "-c", "pass"])
        self.assertEqual(self.ran_count(), 0,
                         "the helm default must not run the project's script")

    def _bare_repo(self, name):
        """A git checkout under this fixture's scratch root, holding nothing.

        GIT-INITIALISED ON PURPOSE, because `selfrepo.repository_state` reads a
        plain directory with no `.git` as an identity it cannot reach AT ALL —
        the third answer, not False. An arm about "readable, and not helm" has
        to hand the predicate something readable, or it measures the unreachable
        case instead and its sibling arm stops being its control.
        """
        path = os.path.realpath(os.path.join(self.tmp, name))
        os.makedirs(path)
        subprocess.run(("git", "init", "-q", "-b", "main", path),
                       capture_output=True, timeout=30)
        return path

    # ------------------------------------- which is-this-helm predicate (r12)
    #
    # The tree carries TWO readings of "is this helm", and they disagree on
    # purpose (task/2442). `cli._is_helm_checkout` is the WIDE, FAIL-OPEN one:
    # its only consumer is an advisory warning, so it answers True for a tree
    # it cannot reach at all. `selfrepo.is_helm_source_tree` is the NARROW one,
    # designated in trunk for "the caller whose worse failure is a false True".
    # This verb SPAWNS on a true answer, so it is that caller. The three arms
    # below pin the choice at the door: a package root is helm, an entry script
    # is not, and a tree that cannot be read is not.

    def test_the_package_root_is_what_makes_a_tree_helms_own(self):
        """(r12, positive) The narrow predicate reads `helm/__init__.py` — the
        package `import helm` actually binds — and a tree that has it takes
        helm's own discovery command with no registry edit."""
        from helm import selfrepo
        source = self._bare_repo("ships-helm")
        os.makedirs(os.path.join(source, "helm"))
        with open(os.path.join(source, "helm", "__init__.py"), "w") as fh:
            fh.write("# the package root\n")
        # CONTROL ON THE INPUT, at the PRODUCER the door now asks: this is the
        # SOURCE state, not a guess about which branch the door took.
        self.assertTrue(selfrepo.is_helm_source_tree(source))
        plan, err = gate.suite_command(source)
        self.assertIsNone(err, err)
        self.assertEqual(plan["source"], "helm-default")
        self.assertEqual(plan["protocol"], "unittest")
        self.assertEqual(plan["argv"],
                         [os.path.realpath(sys.executable)] + list(gate.SUITE))

    def test_an_adopters_bin_helm_wrapper_is_not_a_helm_source_tree(self):  # noqa: VACUOUS_ASSERTION — the refusal TEXT is asserted positively and unconditionally (two assertIn clauses on err), and the arm above is the accepting case on the otherwise identical directory
        """(r12, negative on an otherwise-valid input) A project that ships a
        one-line `bin/helm` wrapper is an ADOPTER — the natural thing for a team
        using helm to do — and trunk task/2442 ruled that an entry script
        LAUNCHES helm and is not helm's source. This is the SAME kind
        of directory as the arm above — a git checkout under this fixture's
        scratch root, built by the same helper — and the only difference is
        which file is in it, so the refusal can only come from the predicate at
        this door.
        """
        from helm import selfrepo
        wrapper = self._bare_repo("adopter-wrapper")
        os.makedirs(os.path.join(wrapper, "bin"))
        with open(os.path.join(wrapper, "bin", "helm"), "w") as fh:
            fh.write('#!/bin/sh\nexec python3 -m helm "$@"\n')
        os.chmod(os.path.join(wrapper, "bin", "helm"), 0o755)
        # CONTROL: the tree is READABLE (so this is not the unreachable case
        # below) and the narrow predicate says it is not helm's source.
        self.assertFalse(selfrepo.is_helm_source_tree(wrapper))
        plan, err = gate.suite_command(wrapper)
        self.assertIsNone(plan)
        self.assertIn("not a registered helm project", err)
        self.assertIn(gate.DECLARED_GATE_FIELD, err)

    def test_a_tree_helm_cannot_read_is_never_handed_the_discovery_command(self):  # noqa: VACUOUS_ASSERTION — each absent plan sits beside an unconditional positive on the SAME observable: the refusal text is asserted for both locations, and the CONTROL at the end resolves a real helm-default plan out of the identical empty directory once the package root is planted
        """(r12, the live defect) An EMPTY directory and a DEPARTED one are both
        identities helm cannot read a checkout for, and this door refuses both:
        an answer helm cannot read is not a licence to SPAWN
        `python3 -m unittest discover -s tests -t .` on a directory with no
        suite, no tests and, in the departed case, no existence. That is the
        unbindable-receipt failure this verb exists to end, and it arrives
        through the predicate rather than through the registry.

        THE FAIL-OPEN READING IS THE DISCRIMINATOR, and the control asserts it
        from inside the arm rather than leaving it to a memory of a run:
        `cli._is_helm_checkout` — the advisory predicate, which answers True for
        a tree it cannot reach because its consumer is a warning — is asserted
        True on the SAME two locations. A door asking that reading resolves a
        helm-default plan here; this door asks the narrow one and refuses.
        BLAST RADIUS of that control: this arm only — it is a read of the other
        predicate and changes nothing.
        """
        from helm import cli, selfrepo
        empty = os.path.realpath(os.path.join(self.tmp, "empty-dir"))
        os.makedirs(empty)
        departed = os.path.realpath(os.path.join(self.tmp, "departed-dir"))
        self.assertFalse(os.path.exists(departed))
        for where in (empty, departed):
            self.assertRaises(selfrepo.UnreachableCheckout,
                              selfrepo.is_helm_source_tree, where)
            # THE CONTROL, on the same location: the advisory predicate says
            # helm, and this door still refuses.
            self.assertTrue(cli._is_helm_checkout(where),
                            "the fail-open predicate no longer answers True "
                            "here, so this arm is measuring a different world")
            plan, err = gate.suite_command(where)
            self.assertIsNone(plan, "an unreadable tree was handed helm's own "
                                    "discovery command to spawn")
            self.assertIn("not a registered helm project", err)
            self.assertIn(gate.DECLARED_GATE_FIELD, err)
        # AND THE DEFAULT IS REACHABLE FROM THIS FIXTURE, measured on the SAME
        # directory: plant the package root in `empty` and the identical call
        # resolves helm-default. So the two refusals above are the unreadable
        # checkout refusing, and not a default this arm could never have reached.
        os.makedirs(os.path.join(empty, "helm"))
        with open(os.path.join(empty, "helm", "__init__.py"), "w") as fh:
            fh.write("# the package root\n")
        plan, err = gate.suite_command(empty)
        self.assertIsNone(err, err)
        self.assertEqual(plan["source"], "helm-default")
        self.assertEqual(plan["argv"],
                         [os.path.realpath(sys.executable)] + list(gate.SUITE))

    def test_an_unregistered_repo_is_refused_naming_the_field(self):  # noqa: VACUOUS_ASSERTION — the refusal TEXT is asserted positively and unconditionally (four assertIn clauses on err), so assertIsNone(row) cannot pass on a run that never happened
        """(d, first half) A repo helm has never heard of gets a sentence that
        names both steps, not a receipt about python unittest discovery."""
        from helm import home
        self.forget()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(row)
        self.assertIn("not a registered helm project", err)
        self.assertIn("helm sync", err)
        self.assertIn(gate.DECLARED_GATE_FIELD, err)
        self.assertIn(home.authored_path(), err)
        self.assertEqual(self.ran_count(), 0)

    def test_a_registered_project_with_no_command_is_refused_naming_the_field(self):  # noqa: VACUOUS_ASSERTION — same shape as the arm above: three unconditional assertIn clauses on the refusal text carry the claim, and the sibling arm supplies the accepting case
        """(d, second half) Registered is not declared."""
        from helm import home
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(row)
        self.assertIn("declares no gate command", err)
        self.assertIn(gate.DECLARED_GATE_FIELD, err)
        self.assertIn(home.authored_path(), err)
        self.assertEqual(self.ran_count(), 0)

    def test_a_refusal_costs_the_queue_nothing(self):  # noqa: VACUOUS_ASSERTION — the SAME-OBSERVABLE POSITIVE is inside the arm and unconditional: after declaring, the identical call reaches the mocked FIFO and acquired == [self.repo], so the empty list above is refused-early and not a fixture that never queues
        """PLACEMENT, like the cross-tree refusal's: an undeclared project must
        not buy a FIFO position it will never use."""
        acquired = []
        with mock.patch.object(gate, "_acquire_gate",
                               side_effect=lambda r: acquired.append(r)
                               or (None, "stop at the queue")):
            row, err = gate.run(repo=self.repo)
            self.assertIsNone(row)
            self.assertIn("declares no gate command", err)
            self.assertEqual(acquired, [])
            self.assertEqual(self.ran_count(), 0)
            # SAME OBSERVABLE, POSITIVE: the declared run DOES reach the queue,
            # so the empty list is refused-early and not a fixture that never
            # queues at all.
            self.declare()
            row, err = gate.run(repo=self.repo)
        self.assertEqual(err, "stop at the queue")
        self.assertEqual(acquired, [self.repo])

    def test_a_malformed_declaration_is_refused_by_shape(self):  # noqa: VACUOUS_ASSERTION — every subTest asserts a positive assertIn on the refusal text before any absence, and the accepting shape is pinned by every other arm in this class
        """A shell string is not an argv, and an unknown protocol is not a
        protocol. Each refusal names the field and what it should hold."""
        for block, expected in (
                ({"command": "pnpm -r test"}, "not a non-empty list"),
                ({"command": []}, "not a non-empty list"),
                ({"command": ["pnpm", ""]}, "not a non-empty list"),
                ({"command": ["./run-suite.sh"], "protocol": "tap"},
                 "declares gate protocol"),
                ("./run-suite.sh", "not an object")):
            with self.subTest(block=block):
                self.declare(block=block)
                row, err = gate.run(repo=self.repo)
                self.assertIsNone(row)
                self.assertIn(expected, err)
                self.assertIn(gate.DECLARED_GATE_FIELD, err)
                self.assertEqual(self.ran_count(), 0)

    def test_the_evidence_line_names_the_exit_status_not_a_missing_count(self):
        """The line a reviewer pastes must not say `Ran ?` about a runner that
        reports no count: `?` means "I could not read the number", and here
        there is no number to read. It names the command and its exit status,
        which are the two facts the verdict rests on."""
        self.declare()
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        line = gate.evidence_line(row)
        self.assertNotIn("Ran ?", line)
        self.assertIn("exit 0 (./run-suite.sh)", line)
        self.assertIn("whole-suite", line)
        self.assertIn("gate:" + row["id"], line)
        # MUST-MISS on the same renderer: a declared `unittest` command DOES
        # carry a count, so the clause above is about the protocol and not
        # about declared receipts losing their counts.
        self.declare(command=[sys.executable, "-c",
                              "import sys; print(%r, file=sys.stderr)"
                              % "Ran 3 tests in 0.1s\n\nOK"],
                     protocol="unittest")
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertIn("Ran 3", gate.evidence_line(row))

    def test_gate_run_plan_prints_the_resolved_command_and_runs_nothing(self):  # noqa: VACUOUS_ASSERTION — rc == 0 and the exact resolved plan dict are unconditional positives on the same call; the ran-nothing and minted-nothing clauses are what the arm exists to state
        """The seam anything OUTSIDE helm asks before spending a box. A remote
        runner that decides for itself whether a tree is gateable is a second
        copy of this policy, and the copy that exists today refuses every
        adopter project helm now accepts."""
        self.declare()
        out, errs = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errs):
            rc = gate.cmd_gate(["run", "--repo", self.repo, "--plan", "--json"])
        self.assertEqual(rc, 0, errs.getvalue())
        self.assertEqual(json.loads(out.getvalue())["plan"], {
            "source": "registry", "project": "adopter",
            "argv": ["./run-suite.sh"], "protocol": "exit"})
        self.assertEqual(self.ran_count(), 0, "--plan ran the suite")
        self.assertEqual(gate.receipts()[0], [], "--plan minted a receipt")
        # AND IT CARRIES HELM'S REFUSAL VERBATIM, so the caller outside helm
        # never has to author a second sentence about a tree it may not gate.
        self.declare(block={"command": "pnpm -r test"})
        out, errs = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errs):
            rc = gate.cmd_gate(["run", "--repo", self.repo, "--plan", "--json"])
        self.assertEqual(rc, 1)
        answer = json.loads(out.getvalue())
        self.assertIsNone(answer["plan"])
        self.assertIn(gate.DECLARED_GATE_FIELD, answer["reason"])

    def test_a_declared_unittest_protocol_still_reads_the_footer(self):
        """The other protocol a project may declare: a python project with its
        own discovery root keeps unittest's grammar, counts and all."""
        self.declare(command=[sys.executable, "-c",
                              "import sys; print(%r, file=sys.stderr)"
                              % "Ran 7 tests in 0.2s\n\nOK"],
                     protocol="unittest")
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "OK")
        self.assertEqual(row["ran"], 7)
        self.assertFalse(row["failures_unreadable"])
        self.assertEqual(row["suite_command"]["protocol"], "unittest")
        self.assertIsNone(gate.row_refusal(row))

    # ------------------------------------------- the round-one cure arms
    #
    # Five findings, and each arm below names the one it closes. What they have
    # in common: the row or the file they attack is otherwise VALID, so the only
    # clause that can refuse it is the one under test.

    def test_a_self_asserted_command_block_is_not_a_declaration(self):
        """F1 (P1). The block is the receipt's own claim about itself, and the
        frozen historical whole-suite argv admission is independent of it.

        A v4 whole-suite row is held to helm's EXACT frozen serial discovery
        argv, so an honest run of a subset cannot claim whole-suite authority by
        rewriting argv and recomputing its public content id. That clause is
        evaluated independently of the `suite_command` block: a block cannot
        stand in for it, because every field a block's self-consistency compares
        lives inside the row, which would let the row supply its own admission.
        The forgery here is minted by the shipped producer and then relabelled
        consistently: argv and the block agree, the protocol agrees with the
        status, the id recomputes. What it cannot produce is an authored
        declaration saying this project declared `pnpm -r test`.
        """
        self.declare()
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertIsNone(gate.row_refusal(row))        # the honest receipt
        self.assertIn(row["v"], gate.HISTORICAL_SERIAL_VERSIONS)
        forged = json.loads(json.dumps(row))
        forged["argv"] = ["pnpm", "-r", "test"]
        forged["suite_command"]["argv"] = ["pnpm", "-r", "test"]
        forged["id"] = gate._receipt_id(forged)
        # SELF-CONSISTENT AND SELF-IDENTIFYING: the two readers that look only
        # at the row are satisfied, which is why neither can be the authority.
        self.assertIsNone(gate._declared_row_refusal(forged))
        self.assertEqual(gate._receipt_id(forged), forged["id"])
        self.assertIn("no authored declaration", gate.row_refusal(forged) or "")
        # AND THE FROZEN CLAUSE IS STILL THERE, unchanged, for the same row with
        # the block removed — the admission was never weakened, only stopped
        # being skippable.
        stripped = {k: v for k, v in forged.items() if k != "suite_command"}
        stripped["id"] = gate._receipt_id(stripped)
        self.assertIn("historical serial discovery argv",
                      gate.row_refusal(stripped) or "")
        # CONTROL. BLAST RADIUS: this arm only. With `pnpm -r test` ACTUALLY
        # authored for this project, the identical row is admitted — so the
        # refusal above is about ORIGIN, and the cure is not "refuse every
        # declared receipt", which would have taken the whole kind down with it.
        self.declare(command=["pnpm", "-r", "test"])
        self.assertIsNone(gate.row_refusal(forged))

    def test_an_unestablished_origin_names_the_authored_file(self):
        """F1, the sentence half. A reader that cannot establish the origin must
        say so — including on a host that simply has no declaration for that
        project, which is the honest state of a receipt that travelled."""
        from helm import home
        self.declare()
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        with open(home.authored_path(), "w") as fh:      # another host's view
            json.dump({"version": 1, "projects": {}}, fh)
        refusal = gate.row_refusal(row)
        self.assertIn(home.authored_path(), refusal)
        self.assertIn("adopter", refusal)
        self.assertIn("run-suite.sh", refusal)
        # MUST-MISS: an UNREADABLE authored layer is a third answer, and it must
        # not read as "the project declared nothing".
        with open(home.authored_path(), "w") as fh:
            fh.write("{not json")
        self.assertIn("could not be read", gate.row_refusal(row) or "")

    def test_a_projection_gate_block_is_reported_never_executed(self):  # noqa: VACUOUS_ASSERTION — three unconditional assertIn clauses on the refusal text carry the claim, and the authored CONTROL at the end asserts the same observable positively (ran_count == 1); the empty authored_declarations tuple is the product law this arm exists to state
        """F2 (P1). registry.json is the PROJECTION — rebuilt by any re-scan,
        written by every discovery pass — so a `gate` block there is not a
        declaration. The merged view could not say that: it MIGRATED an inline
        authored field into the authored layer, even under a strict read, so a
        projection block came back indistinguishable from one the owner wrote
        and `["true"]` could stand in for a failing default.
        """
        from helm import home, registry
        self.project_gate()                     # projection only, nothing authored
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(row)
        self.assertIn("PROJECTION registry", err)
        self.assertIn(gate.DECLARED_GATE_FIELD, err)
        self.assertIn(home.registry_path(), err)
        self.assertEqual(self.ran_count(), 0)
        # AND IT NEVER BECOMES AUTHORITY. An ordinary (non-strict) load is the
        # migrating call — the WRITE channel for this class, not only the read —
        # so the authored layer must still hold no declaration after it.
        registry.load()
        self.assertEqual(
            registry.authored_declarations(gate.DECLARED_GATE_FIELD), ())
        authored = {}
        if os.path.exists(home.authored_path()):
            with open(home.authored_path()) as fh:
                authored = json.load(fh)
        self.assertNotIn(gate.DECLARED_GATE_FIELD,
                         authored.get("projects", {}).get(self.project, {}))
        # CONTROL, SAME OBSERVABLE. BLAST RADIUS: this arm and the round-trip
        # arm below. The SAME block, authored, runs — and it wins over the
        # projection copy, which is the same-path authored precedence the cure
        # had to preserve.
        self.declare()
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["suite_command"]["argv"], ["./run-suite.sh"])
        self.assertEqual(self.ran_count(), 1)

    def test_a_registry_round_trip_neither_promotes_nor_loses_a_declaration(self):  # noqa: VACUOUS_ASSERTION — the KEEP pole is an unconditional positive on the same observable (authored_declarations == [the declaration], then suite_command resolves it) and runs BEFORE the promote-nothing pole
        """F2's other door. `save()` copies every AUTHORED_FIELD out of the
        MERGED record, so a projection `gate` reached the authored file through
        the save path even with the load migration closed — and the naive fix
        (drop the field from the save) DELETES the owner's real declaration,
        because the authored entry is rewritten wholesale. Both poles here.
        """
        from helm import home, registry
        self.project_gate()
        self.declare()                          # authored, same path
        reg = registry.load()
        self.assertEqual(reg["projects"][self.project][gate.DECLARED_GATE_FIELD],
                         {"command": ["./run-suite.sh"], "protocol": "exit"})
        registry.save(reg)
        self.assertEqual(
            [rec["value"] for rec in
             registry.authored_declarations(gate.DECLARED_GATE_FIELD)],
            [{"command": ["./run-suite.sh"], "protocol": "exit"}])
        plan, err = gate.suite_command(self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(plan["argv"], ["./run-suite.sh"])
        # THE MUST-HIT: with NOTHING authored, the same round-trip over the same
        # projection block promotes nothing. This is the arm that goes red if
        # save() takes its `gate` value from the merged record again.
        os.remove(home.authored_path())
        self.project_gate()
        registry.save(registry.load())
        self.assertEqual(
            registry.authored_declarations(gate.DECLARED_GATE_FIELD), ())
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(row)
        self.assertIn("PROJECTION registry", err)
        self.assertEqual(self.ran_count(), 0)

    def test_a_round_trip_keeps_a_projection_block_answering_projection(self):  # noqa: VACUOUS_ASSERTION — the first pole is an unconditional positive on the same observable (declaration_provenance == (block, "projection") and the projection file still carries the block) and runs BEFORE the absence poles, which are the must-miss and the control
        """F2 at the PRODUCER. The round-trip arm above reads the cure through
        the gate's refusal text; this one reads it where the defect lives.
        `save()` rebuilds the projection from the MERGED record, and `gate` is an
        AUTHORED_FIELD — so the field was stripped from the projection write
        while the same save refused (correctly) to promote it, and a
        projection-only block simply VANISHED on the first ordinary save. The
        observable is `declaration_provenance`: it answered "projection" before
        the round trip and "absent" after, which is why the gate stopped saying
        "that is a projection" and started saying "you declared nothing".
        """
        from helm import home, registry
        self.project_gate()                     # projection only, nothing authored
        block = {"command": ["true"], "protocol": "exit"}
        self.assertEqual(
            registry.declaration_provenance(self.project, self.repo,
                                            gate.DECLARED_GATE_FIELD),
            (block, "projection"))
        registry.save(registry.load())
        self.assertEqual(
            registry.declaration_provenance(self.project, self.repo,
                                            gate.DECLARED_GATE_FIELD),
            (block, "projection"), "the save dropped the projection's own block")
        with open(home.registry_path()) as fh:
            projected = json.load(fh)["projects"][self.project]
        self.assertEqual(projected[gate.DECLARED_GATE_FIELD], block)
        # MUST-MISS on an otherwise-identical input: the SAME projection block
        # with a real authored declaration beside it. The cure must not have
        # become "always write the field back", because that would make
        # registry.json a second writer of the value the owner authored.
        self.declare()
        registry.save(registry.load())
        self.assertEqual(
            registry.declaration_provenance(self.project, self.repo,
                                            gate.DECLARED_GATE_FIELD),
            ({"command": ["./run-suite.sh"], "protocol": "exit"}, "authored"))
        with open(home.registry_path()) as fh:
            projected = json.load(fh)["projects"][self.project]
        self.assertNotIn(gate.DECLARED_GATE_FIELD, projected)
        # CONTROL. BLAST RADIUS: this arm only. With no block in EITHER layer the
        # round trip leaves the field absent — so the "projection" answer above
        # came from the block this fixture wrote, not from a save that invents
        # the field for every project it rebuilds.
        os.remove(home.authored_path())
        self.register()
        registry.save(registry.load())
        self.assertEqual(
            registry.declaration_provenance(self.project, self.repo,
                                            gate.DECLARED_GATE_FIELD),
            (None, None))
        with open(home.registry_path()) as fh:
            projected = json.load(fh)["projects"][self.project]
        self.assertNotIn(gate.DECLARED_GATE_FIELD, projected)

    def test_a_cached_v5_receipt_binds_its_declared_command_block(self):
        """F3 (P2). The v5 cached kind returns through a STRUCTURED identity
        payload, and that early return never reached the presence-bound
        `suite-command-v1` part of the join grammar — so a cached row's
        declaration sat outside its own id and the project could be relabelled
        under an unchanged id while argv stayed hashed.
        """
        self.declare()
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        cached = dict(row, v=gate.CACHED_VERSION, executed=True)
        cached["id"] = gate._receipt_id(cached)
        relabelled = json.loads(json.dumps(cached))
        relabelled["suite_command"]["project"] = "a-project-that-declared-nothing"
        self.assertNotEqual(gate._receipt_id(relabelled), cached["id"])
        relabelled = json.loads(json.dumps(cached))
        relabelled["suite_command"]["protocol"] = "unittest"
        self.assertNotEqual(gate._receipt_id(relabelled), cached["id"])
        # COMPATIBILITY, AND IT IS A CONTROL RATHER THAN A COURTESY. BLAST
        # RADIUS: every cached receipt in every live ledger. A v5 row with no
        # block must gain NO key in the payload, because gaining one re-keys
        # rows that are already written and makes them unreadable.
        plain = {k: v for k, v in cached.items() if k != "suite_command"}
        self.assertNotIn("suite_command", gate._v5_receipt_identity(plain))
        self.assertIn("suite_command", gate._v5_receipt_identity(cached))

    def test_a_linked_worktree_resolves_the_root_declaration_and_runs_in_W(self):
        """F4 (P2). An adopter registers and declares at its root R and then
        works in `git worktree`s of R — which is exactly where a lane's gate
        runs. `project_state` compares exact directories, so every linked
        worktree was refused, and requiring a declaration turned that old
        path-only limitation into a block. The repository is the identity
        (`git rev-parse --git-common-dir`), and the run happens in W.
        """
        self.declare()
        w = os.path.realpath(os.path.join(self.tmp, "lane-one"))
        self._git("worktree", "add", "-q", "-b", "lane/one", w)
        # CONTROL ON THE INPUT: W is not a registered project and ships no helm,
        # so nothing but the repository fold can resolve a command for it.
        from helm import foldcompose, selfrepo
        self.assertEqual(foldcompose.project_state(w), ("unregistered", None))
        # THE PREDICATE THE DOOR ASKS, not the advisory one beside it: a control
        # bound to the other reading would stop saying anything about this door.
        self.assertFalse(selfrepo.is_helm_source_tree(w))
        plan, err = gate.suite_command(w)
        self.assertIsNone(err, err)
        self.assertEqual(plan, {"source": "registry", "project": "adopter",
                                "argv": ["./run-suite.sh"], "protocol": "exit"})
        require_supervisor()
        row, err = gate.run(repo=w)
        self.assertIsNone(err, err)
        self.assertEqual(self.ran_cwds(), [w], "the run did not happen in W")
        self.assertEqual(row["repo_id"], w)
        self.assertEqual(row["head"], self.head)
        self.assertEqual(row["status"], "OK")
        self.assertIsNone(gate.row_refusal(row))
        # MUST-MISS on the same worktree: with the declaration withdrawn, W is
        # refused again and names the field. So the resolution above is the
        # declaration's doing and not "a worktree runs whatever it finds".
        from helm import home
        with open(home.authored_path(), "w") as fh:
            json.dump({"version": 1, "projects": {}}, fh)
        plan, err = gate.suite_command(w)
        self.assertIsNone(plan)
        self.assertIn(gate.DECLARED_GATE_FIELD, err)
        self.assertEqual(len(self.ran_cwds()), 1)

    def test_two_declarations_in_one_repository_refuse_as_ambiguous(self):  # noqa: VACUOUS_ASSERTION — the refusal text of both suite_command and run is asserted positively and unconditionally, and the CONTROL resolves the same worktree to a positive plan
        """F4's other half. Picking the first by sort order would run a command
        the owner declared for a DIFFERENT tree, and a gate receipt is the claim
        that authorises a land — so two declarations inside one repository is a
        refusal, and a worktree with its OWN declaration still keeps it.
        """
        one = os.path.realpath(os.path.join(self.tmp, "lane-one"))
        two = os.path.realpath(os.path.join(self.tmp, "lane-two"))
        self._git("worktree", "add", "-q", "-b", "lane/one", one)
        self._git("worktree", "add", "-q", "-b", "lane/two", two)
        self.declare(extra=[("adopter-lane", one,
                             {"command": ["./run-suite.sh"],
                              "protocol": "exit"})])
        plan, err = gate.suite_command(two)
        self.assertIsNone(plan)
        self.assertIn("authored gate declarations", err)
        self.assertIn(self.repo, err)
        self.assertIn(one, err)
        row, run_err = gate.run(repo=two)
        self.assertIsNone(row)
        self.assertIn("authored gate declarations", run_err)
        self.assertEqual(self.ran_count(), 0, "an ambiguous repository ran a command")
        # CONTROL. BLAST RADIUS: this arm. With one of the two withdrawn the same
        # worktree resolves, so the refusal is about the ambiguity and not about
        # `two`.
        self.declare()
        plan, err = gate.suite_command(two)
        self.assertIsNone(err, err)
        self.assertEqual(plan["argv"], ["./run-suite.sh"])
        # AND SAME-PATH PRECEDENCE SURVIVES THE FOLD: a worktree that declares
        # for ITSELF is not ambiguous with its own root.
        self.declare(extra=[("adopter-lane", one,
                             {"command": ["./elsewhere.sh"],
                              "protocol": "exit"})])
        plan, err = gate.suite_command(one)
        self.assertIsNone(err, err)
        self.assertEqual(plan["argv"], ["./elsewhere.sh"])
        self.assertEqual(plan["project"], "adopter-lane")

    # ------------------------------------------ the round-three cure arms
    #
    # Seven findings, four of them classes that survived two cures. Same
    # discipline as the round-one arms above: the row or the file each one
    # attacks is otherwise VALID, so the only clause that can refuse it is the
    # one under test, and every arm states the control that would go red.

    def _unrelated_repo(self):
        """A SECOND, genuinely different repository — its own common dir, its own
        history, and nothing about it declared anywhere."""
        other = os.path.realpath(os.path.join(self.tmp, "unrelated"))
        os.makedirs(other)

        def git(*args):
            return subprocess.run(("git",) + args, cwd=other, text=True,
                                  capture_output=True).stdout.strip()

        git("init", "-q", "-b", "main")
        git("config", "user.email", "unrelated@test")
        git("config", "user.name", "unrelated test")
        with open(os.path.join(other, "README"), "w") as fh:
            fh.write("a repository that declares nothing\n")
        git("add", "-A")
        git("commit", "-qm", "unrelated")
        return other, git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")

    def test_a_declaration_licenses_only_its_own_repository(self):
        """R1 (P1). The origin check compared the project LABEL, the argv and the
        protocol — three values that all live INSIDE the row and inside its
        content id. So it answered "does such a declaration exist somewhere in
        the owner's authored file", and a declaration authored for project A
        licensed a generic whole-suite row about an UNRELATED repository B: mint
        A's honest receipt, restamp it with B's real head and tree, recompute the
        public id, and the frozen-argv admission was skipped on the strength of a
        block A's declaration established. What makes the block a declaration
        ABOUT THIS TREE is the repository, so that is what is compared.
        """
        self.declare()
        require_supervisor()
        honest, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertIsNone(gate.row_refusal(honest))      # the honest receipt
        other, head, tree = self._unrelated_repo()
        # CONTROL ON THE INPUT: B really is a different repository, measured
        # through the same seam the cure reads.
        self.assertNotEqual(gate._repository_of(other),
                            gate._repository_of(self.repo))
        forged = json.loads(json.dumps(honest))
        forged.update(repo_id=other, head=head, tree=tree,
                      head_after=head, tree_after=tree)
        forged["id"] = gate._receipt_id(forged)
        # SELF-CONSISTENT, BRACKETED AND SELF-IDENTIFYING: every reader that
        # looks only at the row is satisfied, which is why none of them can be
        # the authority.
        self.assertIsNone(gate._declared_row_refusal(forged))
        self.assertEqual(gate._receipt_id(forged), forged["id"])
        refusal = gate.row_refusal(forged) or ""
        self.assertIn("not the repository that declaration was authored for",
                      refusal)
        self.assertIn(other, refusal)
        self.assertIn(self.repo, refusal)
        # CONTROL, SAME OBSERVABLE. BLAST RADIUS: this arm. With the SAME command
        # authored for B, the identical row is admitted — so the refusal is about
        # the repository and the cure is not "refuse every restamped row", which
        # would have taken the declared kind down with it. The honest A receipt
        # then refuses for the same one reason, from the other side.
        self.declare(path=other)
        self.assertIsNone(gate.row_refusal(forged))
        self.assertIn("not the repository that declaration was authored for",
                      gate.row_refusal(honest) or "")

    def test_a_pathless_declaration_reaches_a_linked_worktree(self):
        """R3 (P2). The DOCUMENTED block carries no `path` at all — the owner
        writes one declaration per project, not one per directory — and it worked
        at the registered root through `declaration_provenance`. Enumeration
        emitted "" for its location and the repository fold DROPPED the record,
        so the documented shape refused in every linked worktree of that root,
        which is exactly where a lane's gate runs. A pathless declaration
        resolves to the project's REGISTERED location, consistently, at both
        doors.
        """
        from helm import foldcompose, home, registry, selfrepo
        self.register()
        with open(os.path.join(home.global_dir(),
                               "registry-authored.json"), "w") as fh:
            json.dump({"version": 1, "projects": {self.project: {
                gate.DECLARED_GATE_FIELD: {"command": ["./run-suite.sh"],
                                           "protocol": "exit"}}}}, fh)
        # CONTROL ON THE INPUT: this is the documented block and it already
        # resolved at the root, so the arm below is about the worktree and not
        # about a declaration helm never accepted.
        self.assertEqual(
            registry.declaration_provenance(self.project, self.repo,
                                            gate.DECLARED_GATE_FIELD),
            ({"command": ["./run-suite.sh"], "protocol": "exit"}, "authored"))
        # THE PRODUCER, WHERE THE DEFECT LIVES: the record now names a location.
        self.assertEqual(
            [rec["path"] for rec in
             registry.authored_declarations(gate.DECLARED_GATE_FIELD)],
            [self.repo])
        w = os.path.realpath(os.path.join(self.tmp, "lane-one"))
        self._git("worktree", "add", "-q", "-b", "lane/one", w)
        self.assertEqual(foldcompose.project_state(w), ("unregistered", None))
        self.assertFalse(selfrepo.is_helm_source_tree(w))
        plan, err = gate.suite_command(w)
        self.assertIsNone(err, err)
        self.assertEqual(plan, {"source": "registry", "project": self.project,
                                "argv": ["./run-suite.sh"], "protocol": "exit"})
        require_supervisor()
        row, err = gate.run(repo=w)
        self.assertIsNone(err, err)
        self.assertEqual(self.ran_cwds(), [w], "the run did not happen in W")
        self.assertIsNone(gate.row_refusal(row))
        # MUST-MISS on the same worktree and the same authored bytes: with the
        # project's MEMBERSHIP gone the pathless block resolves to no location,
        # so it declares for nothing and W is refused naming the field. This is
        # what stops the resolution above being "a worktree runs whatever
        # pathless block it finds". BLAST RADIUS: this pair.
        self.forget()
        self.assertEqual(
            registry.authored_declarations(gate.DECLARED_GATE_FIELD), ())
        plan, err = gate.suite_command(w)
        self.assertIsNone(plan)
        self.assertIn(gate.DECLARED_GATE_FIELD, err)
        self.assertEqual(len(self.ran_cwds()), 1)

    def test_a_project_name_containing_a_separator_keeps_its_whole_label(self):
        """N1 (P2). A same-path entry is keyed by the project's plain name and a
        second-path entry by `name@<stamp>`, and the reader split the key at the
        FIRST separator. `team@project` is a legal registry name, so the label
        came back truncated to `team`: the gate resolved and minted under the
        full name, and the origin door then rejected helm's OWN receipt for the
        project it had just run. The stamp is decoded from the recorded path
        instead of guessed from a delimiter.
        """
        from helm import registry
        self.project = "team@adopter"
        self.register()
        self.declare()
        # CONTROL ON THE INPUT: the registry admits this name, so the truncation
        # was never a rejected input being reported.
        registry._checked_value({"projects": {self.project: {
            "name": self.project, "path": self.repo}}})
        self.assertEqual(
            [rec["project"] for rec in
             registry.authored_declarations(gate.DECLARED_GATE_FIELD)],
            [self.project])
        plan, err = gate.suite_command(self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(plan["project"], self.project)
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["suite_command"]["project"], self.project)
        self.assertIsNone(gate.row_refusal(row),
                          "the origin door refused helm's own receipt")
        # MUST-MISS: a key that IS a minted stamp still decodes away, so the cure
        # is "undo only what helm computed" and not "never split". BLAST RADIUS:
        # this pair — together they admit exactly one reading of a key.
        second = os.path.realpath(os.path.join(self.tmp, "second"))
        os.makedirs(second)
        self.declare(name=registry._qualified("adopter", second), path=second)
        self.assertEqual(
            [rec["project"] for rec in
             registry.authored_declarations(gate.DECLARED_GATE_FIELD)],
            ["adopter"])

    def test_an_unreadable_declaration_location_refuses_instead_of_resolving(self):  # noqa: VACUOUS_ASSERTION — every absence here sits beside an unconditional positive on the SAME observable: the two measured identity readings (the sibling's is None, this worktree's folds onto the root's), the positive assertIn clauses on the refusal text, and the CONTROL at the end which resolves the identical worktree to a real plan once the unreadable entry is withdrawn
        """N2 (P2). `_repository_of` answers None when git could not speak, and
        the fold DROPPED such a candidate — so an ambiguous authority became a
        unique one: two declarations inside this repository, one whose location
        reads and one whose location does not, and the resolver ran the readable
        one's command for a tree that carries two. An unknown counts in the
        census, so a lone readable hit beside one refuses.
        """
        one = os.path.realpath(os.path.join(self.tmp, "lane-one"))
        two = os.path.realpath(os.path.join(self.tmp, "lane-two"))
        self._git("worktree", "add", "-q", "-b", "lane/one", one)
        self._git("worktree", "add", "-q", "-b", "lane/two", two)
        self.declare(extra=[("adopter-lane", two,
                             {"command": ["./elsewhere.sh"],
                              "protocol": "exit"})])
        # NOTHING MAY ASK ABOUT `two` WHILE IT EXISTS. `vcs.common_dir` memoises
        # its SUCCESSES, so a "both readable" probe here would cache the
        # sibling's identity and the rest of this arm would measure the cached
        # answer instead of an unreadable one — the reviewer's scenario is
        # explicitly the no-cached-success case, and the both-readable pole is
        # measured by `test_two_declarations_in_one_repository_refuse_as_
        # ambiguous` above, on this same fold.
        shutil.rmtree(two)
        # MEASURED, NOT ASSUMED: the sibling's identity is unreadable while this
        # worktree's still folds onto the root's.
        self.assertIsNone(gate._repository_of(two))
        self.assertEqual(gate._repository_of(one),
                         gate._repository_of(self.repo))
        plan, err = gate.suite_command(one)
        self.assertIsNone(
            plan, "an unreadable sibling declaration became unique authority")
        self.assertIn("census is INCOMPLETE", err)
        self.assertIn(two, err)
        row, run_err = gate.run(repo=one)
        self.assertIsNone(row)
        self.assertIn("census is INCOMPLETE", run_err)
        self.assertEqual(self.ran_count(), 0,
                         "an incomplete census ran a command")
        # CONTROL. BLAST RADIUS: this arm. With the unreadable entry WITHDRAWN
        # the same worktree resolves, so the refusal is the census and not `one`.
        self.declare()
        plan, err = gate.suite_command(one)
        self.assertIsNone(err, err)
        self.assertEqual(plan["argv"], ["./run-suite.sh"])

    def test_a_withdrawn_declaration_is_inactive_until_restored(self):  # noqa: VACUOUS_ASSERTION — the standing positive runs FIRST (the declaration resolves to ./run-suite.sh before the location goes away) and the CONTROL runs LAST on the same observable (a restore resolves it again); the successor marker's absence is the product law this arm exists to state, guarded by those two
        """N3 (P2). `forget` archives the record and KEEPS the authored entry so
        `restore` can put it back, while membership itself is tombstoned by
        `load`. Enumerating the authored file alone re-armed the declaration for
        a location the owner had withdrawn — so when a path came back carrying a
        DIFFERENT repository, that repository was gated with the old command,
        ahead of the unregistered refusal and with no restore anywhere in the
        story. This is not the projection-to-authored promotion the writers guard:
        the value is genuinely authored and the MEMBERSHIP is what is missing.
        """
        from helm import registry
        self.declare()
        plan, err = gate.suite_command(self.repo)
        self.assertIsNone(err, err)                      # the standing positive
        self.assertEqual(plan["argv"], ["./run-suite.sh"])
        shutil.rmtree(self.repo)                         # the location goes away
        self.assertIsNone(registry.forget(self.project, apply=True)[1])
        # A DIFFERENT REPOSITORY APPEARS AT THE OLD PATH. Its own suite writes a
        # second marker, so "did the withdrawn command run" is measurable rather
        # than inferred.
        other_marker = os.path.join(self.tmp, "successor-marker")
        os.makedirs(self.repo)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "successor@test")
        self._git("config", "user.name", "successor test")
        script = os.path.join(self.repo, "run-suite.sh")
        with open(script, "w") as fh:
            fh.write("#!/bin/sh\nprintf 'successor\\n' >> %s\nexit 0\n"
                     % other_marker)
        os.chmod(script, 0o755)
        self._git("add", "-A")
        self._git("commit", "-qm", "a different repository, same path")
        self.assertEqual(
            registry.authored_declarations(gate.DECLARED_GATE_FIELD), ())
        plan, err = gate.suite_command(self.repo)
        self.assertIsNone(plan, "a withdrawn declaration gated a new repository")
        self.assertIn(gate.DECLARED_GATE_FIELD, err)
        row, run_err = gate.run(repo=self.repo)
        self.assertIsNone(row)
        self.assertIn(gate.DECLARED_GATE_FIELD, run_err)
        self.assertFalse(os.path.exists(other_marker),
                         "the withdrawn command ran on the successor repository")
        # CONTROL, SAME OBSERVABLE. BLAST RADIUS: this arm. A RESTORE is the one
        # thing that puts the declaration back, so the refusal above is about the
        # withdrawn membership and not about the authored entry being unreadable.
        self.assertIsNone(registry.restore(self.project, apply=True)[1])
        plan, err = gate.suite_command(self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(plan["argv"], ["./run-suite.sh"])


    # ------------------------------------------- the round-five cure arms
    #
    # Two classes, and both are measured through the doors a real consumer
    # crosses: mint in A, import into B, bind in B for the first; the shipped
    # resolver on a real linked worktree for the second. Same discipline as the
    # rounds above — the row or the world each one attacks is otherwise VALID.

    def _fresh_import_memos(self):
        """gateimport keeps three process-global memos, and this fixture is not
        ImportBase (which clears them per case). An import arm that inherited a
        warm binding memo would measure the PREVIOUS case's ledger, so clear them
        on both sides of the arm."""
        def clear():
            gateimport._REPO_IDENTITIES.clear()
            gateimport._BINDING_ROWS_MEMO.clear()
            gateimport._STORED_IDS_CACHE[0] = None

        clear()
        self.addCleanup(clear)

    def test_a_declaration_licenses_the_repository_that_SPENDS_the_receipt(self):
        """C1 (P1). The origin check read `repo_id` — A FIELD OF THE ROW — as the
        repository a declaration had to cover, and the row therefore chose which
        declaration judged it. The whole ladder: mint A's honest receipt, restamp
        its head/tree with B's, LEAVE `repo_id` naming A, `gate import` it into B
        (which writes B a genuine canonical binding), and bind it in B. A's
        declaration then waived the frozen-argv admission for a whole-suite claim
        about B, which declares nothing. The subject is the repository about to
        SPEND the row, resolved before the row is judged.

        AND THE SAME READING REFUSED HONEST TRAVEL, which is the second pole
        here. A fab-minted receipt records the NODE's path in `repo_id` and
        `gate import` does not rewrite it, so an honest imported row names a
        directory this box has never had — refused as "unreadable" however good
        the importing repository's own declaration was. One cure, both poles.
        """
        self._fresh_import_memos()
        self.declare()
        require_supervisor()
        honest, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        b, head, tree = self._unrelated_repo()
        # CONTROL ON THE INPUT: B is a different repository, measured through the
        # same seam the cure reads, and it declares nothing yet.
        self.assertNotEqual(gate._repository_of(b),
                            gate._repository_of(self.repo))
        forged = json.loads(json.dumps(honest))
        forged.update(head=head, tree=tree, head_after=head, tree_after=tree)
        forged["id"] = gate._receipt_id(forged)
        # EVERY READER THAT LOOKS ONLY AT THE ROW IS SATISFIED, which is why none
        # of them can be the authority.
        self.assertIsNone(gate._declared_row_refusal(forged))
        self.assertEqual(gate._receipt_id(forged), forged["id"])
        artifact = os.path.join(self.tmp, "into-b.jsonl")
        with open(artifact, "w") as fh:
            fh.write(json.dumps(forged) + "\n")
        _stored, verdict, import_err = gateimport.import_receipt(artifact, b)
        self.assertIsNone(import_err, import_err)
        self.assertEqual(verdict, "imported")
        # B HOLDS REAL REPOSITORY CAPABILITY FOR THIS ROW — the import wrote it —
        # so nothing below is refused for want of a binding, and the import did
        # NOT rewrite `repo_id`: the stored row still names A.
        self.assertEqual(gateimport.repository_authorization(forged, b),
                         (True, None))
        placed, by_id_err = gate.by_id(forged["id"])
        self.assertIsNone(by_id_err, by_id_err)
        self.assertEqual(placed["repo_id"], self.repo)
        # POLE ONE, AT THE DOOR THAT SPENDS: B may not inherit A's declaration.
        state, rid, why = gate.bind("gate:" + forged["id"], head, repo_id=b)
        self.assertEqual(state, "REFUSED")
        self.assertEqual(rid, forged["id"])
        self.assertIn("is being spent in", why)
        self.assertIn(b, why)
        self.assertIn(self.repo, why)
        # CONTROL, THE SAME ROW WITH ONE QUESTION CHANGED. Asked with no consuming
        # repository — the row-level question, and exactly the subject the old
        # reading used — the identical row is ADMITTED. So the row is otherwise
        # fully valid and it is the consuming-repository clause that refuses it;
        # this is the assertion pair that goes red if the subject reverts.
        # BLAST RADIUS: this pair.
        self.assertIsNone(gate.row_refusal(forged))
        # POLE TWO: B's OWN declaration admits it, under B's own project name —
        # the label is not the question, and a registry name is per-host anyway.
        self.declare(name="bee", path=b,
                     extra=[(self.project, self.repo,
                             {"command": ["./run-suite.sh"],
                              "protocol": "exit"})])
        state, _rid, why = gate.bind("gate:" + forged["id"], head, repo_id=b)
        self.assertEqual(state, "VERIFIED", why)
        # AND THE FROZEN ADMISSION IS STILL WHAT THE BLOCK WAS BUYING: the same
        # row with the block removed is refused for its noncanonical argv, so the
        # VERIFIED above is a declaration being spent and not a row that would
        # have bound anyway. BLAST RADIUS: this arm.
        stripped = {k: v for k, v in forged.items() if k != "suite_command"}
        stripped["id"] = gate._receipt_id(stripped)
        bare = os.path.join(self.tmp, "no-block.jsonl")
        with open(bare, "w") as fh:
            fh.write(json.dumps(stripped) + "\n")
        _s, verdict, import_err = gateimport.import_receipt(bare, b)
        self.assertIsNone(import_err, import_err)
        state, _rid, why = gate.bind("gate:" + stripped["id"], head, repo_id=b)
        self.assertEqual(state, "REFUSED")
        self.assertIn("historical serial discovery argv", why)

    def test_an_imported_receipt_binds_on_the_importing_repositorys_declaration(self):
        """C1's honest-travel pole (P2), at the same boundary. A receipt
        minted on a fab node carries THAT node's path in `repo_id`; the import
        records the origin separately and keeps the receipt byte for byte. So the
        recorded origin is unreadable here BY CONSTRUCTION, and reading it as the
        subject refused every travelling declared receipt — the exact shape the
        feature exists to serve. Provenance is not authority, and authority is
        the importing repository's own declaration.
        """
        self._fresh_import_memos()
        self.declare()
        require_supervisor()
        honest, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        b, head, tree = self._unrelated_repo()
        travelled = json.loads(json.dumps(honest))
        travelled.update(repo_id="/remote/fab/wt/some-lane-abc12345",
                         head=head, tree=tree,
                         head_after=head, tree_after=tree)
        travelled["id"] = gate._receipt_id(travelled)
        # CONTROL ON THE INPUT: the recorded origin is genuinely unreadable on
        # this box, which is what the old subject read and refused on.
        self.assertIsNone(gate._repository_of(travelled["repo_id"]))
        artifact = os.path.join(self.tmp, "travelled.jsonl")
        with open(artifact, "w") as fh:
            fh.write(json.dumps(travelled) + "\n")
        _s, verdict, import_err = gateimport.import_receipt(artifact, b)
        self.assertIsNone(import_err, import_err)
        self.assertEqual(verdict, "imported")
        placed, by_id_err = gate.by_id(travelled["id"])
        self.assertIsNone(by_id_err, by_id_err)
        self.assertEqual(placed["repo_id"], "/remote/fab/wt/some-lane-abc12345",
                         "the import rewrote the receipt's recorded origin")
        # B DECLARES THE SAME COMMAND, so the row is admitted where it is spent.
        self.declare(name="bee", path=b)
        state, _rid, why = gate.bind("gate:" + travelled["id"], head, repo_id=b)
        self.assertEqual(state, "VERIFIED", why)
        # MUST-MISS ON THE SAME ROW. BLAST RADIUS: this pair. With B's declaration
        # withdrawn the identical imported row is refused, so the admission above
        # is B's declaration and not "an import launders anything it places".
        self.declare()
        state, _rid, why = gate.bind("gate:" + travelled["id"], head, repo_id=b)
        self.assertEqual(state, "REFUSED")
        self.assertIn("is being spent in", why)

    def test_an_unreadable_only_declaration_never_selects_helms_own_suite(self):  # noqa: VACUOUS_ASSERTION — three unconditional positives carry this arm on the SAME observables: the readable-declaration plan equality runs FIRST, the refusal text is asserted positively (UNKNOWN + the unreadable path), and the CONTROL at the end drives the identical worktree under the identical patched default and asserts the marker file EXISTS; the absent marker is the product law the arm exists to state
        """C4 (P2). `_root_declaration` counted an unreadable candidate in the
        census only when a READABLE hit stood beside it; with zero readable hits
        it answered ABSENT, and absent sends `suite_command` on to the one
        fallback that exists — helm's own unittest discovery, in the one tree
        that has it. So in a helm checkout whose only custom declaration sits at
        a worktree git cannot speak for, the override feature answered with the
        very command it overrides. Zero hits beside an unknown is UNKNOWN, and
        the caller refuses toward no spawn.

        THE DEFAULT IS MEASURED, NOT REASONED ABOUT: `SUITE` is patched to a
        command that writes its own marker, so "helm's default ran" is a file on
        disk rather than an inference about which branch was taken.
        """
        from tests._tmphome import helm_tree
        from helm import home, selfrepo
        # THIS FIXTURE REPO NOW SHIPS HELM, because the damage this closes only
        # exists where the default is reachable at all. The worktrees are cut
        # AFTER that commit so they carry the mark too.
        helm_tree(self, self.repo)
        self._git("commit", "-qm", "this fixture now ships helm")
        w = os.path.realpath(os.path.join(self.tmp, "lane-w"))
        readable = os.path.realpath(os.path.join(self.tmp, "lane-custom"))
        gone = os.path.realpath(os.path.join(self.tmp, "lane-gone"))
        for path, branch in ((w, "lane/w"), (readable, "lane/custom"),
                             (gone, "lane/gone")):
            self._git("worktree", "add", "-q", "-b", branch, path)
        # NOTHING MAY ASK ABOUT `gone` WHILE IT EXISTS: `vcs.common_dir` memoises
        # its SUCCESSES, so one probe of a readable `gone` would serve a cached
        # identity for the rest of the process and this arm would measure the
        # cache instead of an unreadable location. It is removed here, before the
        # first declaration that could make any door look at it.
        shutil.rmtree(gone)
        default_marker = os.path.join(self.tmp, "helm-default-ran")
        default = ("-c", "open(%r, 'a').write('default\\n')" % default_marker)
        # THE POSITIVE FIRST, on a READABLE declaration in the same repository:
        # the project's own command outranks the default, which is the feature.
        self.declare(name="adopter-lane", path=readable,
                     command=("./elsewhere.sh",))
        self.assertTrue(selfrepo.is_helm_source_tree(w),
                        "the default must be reachable for this arm to mean anything")
        plan, err = gate.suite_command(w)
        self.assertIsNone(err, err)
        self.assertEqual(plan["source"], "registry")
        self.assertEqual(plan["argv"], ["./elsewhere.sh"])
        # THE DEFECT: the same world with that ONE declaration at an unreadable
        # location. Nothing else changes — same worktree, same repository, same
        # helm checkout.
        self.declare(name="adopter-lane", path=gone,
                     command=("./elsewhere.sh",))
        self.assertIsNone(gate._repository_of(gone))
        self.assertEqual(gate._repository_of(w),
                         gate._repository_of(self.repo))
        plan, err = gate.suite_command(w)
        self.assertIsNone(plan, "an unreadable census selected helm's own suite")
        self.assertIn("UNKNOWN", err)
        self.assertIn(gone, err)
        with mock.patch.object(gate, "SUITE", default):
            row, run_err = gate.run(repo=w)
        self.assertIsNone(row)
        self.assertIn("UNKNOWN", run_err)
        self.assertFalse(os.path.exists(default_marker),
                         "the unreadable census fell back to helm's own suite")
        self.assertEqual(self.ran_count(), 0)
        # CONTROL, SAME OBSERVABLE, SAME PATCHED DEFAULT. BLAST RADIUS: this arm.
        # With the authored layer holding NO declaration at all, the identical
        # worktree DOES take helm's default and the marker appears — so the
        # absence above is the UNKNOWN census refusing, and not a default this
        # fixture could never have reached.
        with open(home.authored_path(), "w") as fh:
            json.dump({"version": 1, "projects": {}}, fh)
        with mock.patch.object(gate, "SUITE", default):
            require_supervisor()
            row, run_err = gate.run(repo=w)
        self.assertIsNone(run_err, run_err)
        self.assertTrue(os.path.exists(default_marker))
        self.assertEqual(row["argv"][1:], list(default))
        self.assertNotIn("suite_command", row)

    # ------------------------------------------- the round-six cure arms
    #
    # One resolver now answers both doors, so these arms attack the GAP between
    # them: a world where what may be SPENT at a location and what would RUN
    # there were different answers. Each one is measured through the shipped
    # producers — a real linked worktree, a real minted receipt, a real bind.

    def test_the_admission_door_shares_the_command_that_would_RUN_here(self):  # noqa: VACUOUS_ASSERTION — every absence here sits beside an unconditional positive on the SAME observable, and two of them run FIRST: both locations' resolved plans are asserted equal, the minted row's own block argv and OK status are asserted, and the SAME `bind` door returns VERIFIED twice (the identical row in R, W's own row in W). The rung cannot link them because each gate.run/gate.bind call mints a fresh producer identity
        """C1 (P1). The origin check scanned the authored file for ANY declaration
        whose argv and protocol matched the row and whose location folded into the
        consuming repository — and "any" is not "the one that would run". A root R
        declaring `["true"]` and a linked worktree W declaring its own real suite
        are ONE repository (`--git-common-dir` is the identity the fold uses, on
        purpose), so R's honest green `true` receipt matched R's own entry, folded
        into W, and was admitted in W as a whole-suite claim about W — waiving the
        frozen-argv admission on the strength of a declaration W does not have.

        NOTHING IS FORGED HERE, which is what makes it a authority defect rather
        than a tampering one: the row is minted by `gate run`, its content id is
        its own, `repo_id` names R honestly, and R really does declare `["true"]`.
        The scan simply asked a weaker question than the spawn does, so both doors
        now ask `effective_declaration` for the CONSUMING location.
        """
        self._fresh_import_memos()
        w = os.path.realpath(os.path.join(self.tmp, "lane-w"))
        self._git("worktree", "add", "-q", "-b", "lane/w", w)
        self.declare(command=("true",),
                     extra=[("adopter-lane", w,
                             {"command": ["./run-suite.sh"],
                              "protocol": "exit"})])
        # CONTROL ON THE INPUT, measured through the same seam the fold reads: R
        # and W are ONE repository, which is what let R's declaration reach W.
        self.assertEqual(gate._repository_of(w), gate._repository_of(self.repo))
        # AND THE TWO LOCATIONS DISAGREE ABOUT THE COMMAND, which is the premise.
        self.assertEqual(gate.suite_command(self.repo)[0]["argv"], ["true"])
        self.assertEqual(gate.suite_command(w)[0]["argv"], ["./run-suite.sh"])
        require_supervisor()
        r_row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(r_row["suite_command"]["argv"], ["true"])
        self.assertEqual(r_row["status"], "OK")
        self.assertEqual(self.ran_count(), 0,
                         "`true` somehow ran the adopter's own script")
        # EVERY READER THAT LOOKS ONLY AT THE ROW IS SATISFIED, so the clause
        # under test is the only one that can refuse it.
        self.assertIsNone(gate._declared_row_refusal(r_row))
        # POLE ONE, AT THE DOOR THAT SPENDS: R's receipt may not be spent in W.
        state, rid, why = gate.bind("gate:" + r_row["id"], self.head, repo_id=w)
        self.assertEqual(state, "REFUSED")
        self.assertEqual(rid, r_row["id"])
        self.assertIn("effectively declares", why)
        self.assertIn("./run-suite.sh", why)
        self.assertIn(w, why)
        # CONTROL, THE SAME ROW WITH ONE QUESTION CHANGED. BLAST RADIUS: this
        # pair. Spent in R — the location that actually declares `["true"]` — the
        # identical row is ADMITTED, so the refusal above is the consuming
        # location's effective declaration and not a row that binds nowhere.
        state, _rid, why = gate.bind("gate:" + r_row["id"], self.head,
                                     repo_id=self.repo)
        self.assertEqual(state, "VERIFIED", why)
        # POLE TWO: W's OWN receipt is admitted in W, so this is a narrowing of
        # WHICH declaration answers and not a refusal of declared receipts.
        w_row, err = gate.run(repo=w)
        self.assertIsNone(err, err)
        self.assertEqual(w_row["suite_command"]["argv"], ["./run-suite.sh"])
        self.assertEqual(self.ran_cwds(), [w])
        state, _rid, why = gate.bind("gate:" + w_row["id"], self.head, repo_id=w)
        self.assertEqual(state, "VERIFIED", why)

    def test_a_consumer_helm_cannot_resolve_a_command_for_refuses_the_bind(self):
        """C1's other half, and the states the two doors disagreed about most
        sharply. `suite_command` REFUSES to spawn from a location whose census is
        AMBIGUOUS (two declarations in one repository and no exact entry for this
        tree) or whose exact declaration is MALFORMED — while the scan answered
        ADMITTED in both, because a matching entry existed somewhere in the fold
        and a malformed candidate was SKIPPED rather than refused on. A receipt
        must not be spendable where the command it claims could not be chosen.
        """
        self._fresh_import_memos()
        w = os.path.realpath(os.path.join(self.tmp, "lane-w"))
        u = os.path.realpath(os.path.join(self.tmp, "lane-u"))
        for path, branch in ((w, "lane/w"), (u, "lane/u")):
            self._git("worktree", "add", "-q", "-b", branch, path)
        self.declare()
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        # THE CONTROL RUNS FIRST, and it is also the unique-inherited-root
        # positive: with ONE declaration in the repository the identical row binds
        # in U. BLAST RADIUS: this arm — every refusal below is this same call
        # with one authored file rewritten.
        state, _rid, why = gate.bind("gate:" + row["id"], self.head, repo_id=u)
        self.assertEqual(state, "VERIFIED", why)
        # AMBIGUITY: two declarations in this repository, neither of them U's own.
        self.declare(extra=[("adopter-lane", w,
                             {"command": ["./run-suite.sh"],
                              "protocol": "exit"})])
        spawn, spawn_err = gate.suite_command(u)
        self.assertIsNone(spawn, "an ambiguous census resolved a command")
        self.assertIn("authored gate declarations", spawn_err)
        state, _rid, why = gate.bind("gate:" + row["id"], self.head, repo_id=u)
        self.assertEqual(state, "REFUSED")
        self.assertIn("authored gate declarations", why)
        self.assertIn(u, why)
        # A MALFORMED EXACT DECLARATION AT THE CONSUMER: U declares for itself and
        # the block is a shell string rather than an argv list, so there is
        # nothing here that could be chosen — and falling through to the
        # repository would run a command the owner did not declare for this tree.
        self.declare(extra=[("adopter-lane", u,
                             {"command": "./run-suite.sh", "protocol": "exit"})])
        spawn, spawn_err = gate.suite_command(u)
        self.assertIsNone(spawn, "a malformed exact declaration resolved")
        self.assertIn("not a non-empty list", spawn_err)
        state, _rid, why = gate.bind("gate:" + row["id"], self.head, repo_id=u)
        self.assertEqual(state, "REFUSED")
        self.assertIn("not a non-empty list", why)

    def test_an_unreadable_registry_refuses_before_any_command_is_selected(self):
        """C3's ordering half (P1). `project_state` answers "unknown" for a
        STRICT-LOAD failure, and that answer stood BELOW both selections and below
        helm's own default. `_root_declaration` reads the authored layer alone, so
        a repository whose merged registry could not be read still had its authored
        entry exact-matched and that command returned — and `run` forwarded it to
        a real spawn under otherwise ordinary preconditions. A failed read cannot
        license a command; it can only say the answer is UNKNOWN.

        AND THE TWO NEGATIVES STAY DISTINCT, which is the reason this refusal has
        its own sentence: a registry helm CAN read and that does not hold this
        repository is the UNREGISTERED answer, and the last pole here measures
        that it did not collapse into the unreadable one.
        """
        self.declare()
        # THE STANDING POSITIVE FIRST, on a READABLE registry: the same authored
        # file resolves and the command really runs.
        plan, err = gate.suite_command(self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(plan["argv"], ["./run-suite.sh"])
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(self.ran_count(), 1)
        # THE DEFECT: the projection registry cannot be strict-read. Nothing else
        # changes — same repo, same authored declaration, same tree.
        from helm import foldcompose, home
        with open(os.path.join(home.global_dir(), "registry.json"), "w") as fh:
            json.dump({"version": 1, "projects": {self.project: []}}, fh)
        self.assertEqual(foldcompose.project_state(self.repo), ("unknown", None))
        plan, err = gate.suite_command(self.repo)
        self.assertIsNone(plan, "a failed registry read still selected a command")
        self.assertIn("UNREADABLE registry", err)
        row, run_err = gate.run(repo=self.repo)
        self.assertIsNone(row)
        self.assertIn("UNREADABLE registry", run_err)
        self.assertEqual(self.ran_count(), 1,
                         "an unreadable registry spawned the declared command")
        # CONTROL, SAME OBSERVABLE. BLAST RADIUS: this arm. The readable
        # projection put back, the identical call runs the command again, so the
        # refusal above is the failed read and nothing about this tree.
        self.register()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(self.ran_count(), 2)
        # AND READABLE-BUT-ABSENT IS STILL ITS OWN ANSWER.
        self.forget()
        with open(home.authored_path(), "w") as fh:
            json.dump({"version": 1, "projects": {}}, fh)
        plan, err = gate.suite_command(self.repo)
        self.assertIsNone(plan)
        self.assertIn("not a registered helm project", err)
        self.assertNotIn("UNREADABLE registry", err)


    # ------------------------------------------ the round-eight cure arms

    def _root_and_lane(self):
        """R declares `["true"]`, its linked worktree W declares the real suite.

        ONE repository, two declarations, which is the census the shared admin
        dir sees — and the exact world a lane lives in.
        """
        w = os.path.realpath(os.path.join(self.tmp, "lane-w"))
        self._git("worktree", "add", "-q", "-b", "lane/w", w)
        self.declare(command=("true",),
                     extra=[("adopter-lane", w,
                             {"command": ["./run-suite.sh"],
                              "protocol": "exit"})])
        return w, gate._repository_of(self.repo)

    def test_the_rows_own_checkout_says_which_tree_of_the_repository_spends_it(self):
        """F1 (P2). A dispatch row records the CHECKOUT it was sent from
        (`repo_root`) beside the repository (`repo_id`), and every reader that
        spends its receipt threw that coordinate away and resolved through the
        shared admin dir instead. A common dir is one value for a repository root
        and every linked worktree of it, so with R declaring one command and the
        lane worktree W declaring another the census finds TWO declarations and
        refuses as ambiguous — and W's own honest receipt, minted by the command W
        itself declares, for a row dispatched in W, was refused with it. The
        narrower fact was in the row all along; the reader re-derived a wider one.
        """
        self._fresh_import_memos()
        w, common = self._root_and_lane()
        # CONTROL ON THE INPUT, through the same seams the doors read: W is a
        # tree of this repository, the common dir is NOT the checkout, and the
        # two locations disagree about the command.
        self.assertEqual(gate._repository_of(w), common)
        self.assertNotEqual(common, w)
        self.assertEqual(gate.suite_command(self.repo)[0]["argv"], ["true"])
        self.assertEqual(gate.suite_command(w)[0]["argv"], ["./run-suite.sh"])
        # THE HONEST STRONG-W RECEIPT: W's own declared command really ran, in W.
        require_supervisor()
        w_row, err = gate.run(repo=w)
        self.assertIsNone(err, err)
        self.assertEqual(w_row["suite_command"]["argv"], ["./run-suite.sh"])
        self.assertEqual(w_row["status"], "OK")
        self.assertEqual(self.ran_cwds(), [w])
        # THE DEFECT, and the ambiguity refusal that MUST STAY: a caller naming
        # only the repository has said nothing about which tree, and two
        # candidates is genuinely ambiguous. Both are named.
        state, rid, why = gate.bind("gate:" + w_row["id"], self.head,
                                    repo_id=common)
        self.assertEqual(state, "REFUSED")
        self.assertEqual(rid, w_row["id"])
        self.assertIn("authored gate declarations", why)
        self.assertIn(self.repo, why)
        self.assertIn(w, why)
        # THE CURE, AND THE DISCRIMINATING PAIR. BLAST RADIUS: these two calls —
        # the same row, the same repository, the same reviewed tip, and the one
        # coordinate the dispatch row carries. Handed it, the identical bind
        # ADMITS, so the refusal above is the missing coordinate and not a
        # receipt that binds nowhere.
        state, rid, why = gate.bind("gate:" + w_row["id"], self.head,
                                    repo_id=common, consuming_repo=w)
        self.assertEqual(state, "VERIFIED", why)
        self.assertEqual(rid, w_row["id"])
        # AND THE COORDINATE NARROWS, IT DOES NOT LAUNDER: R's `true` receipt is
        # still refused in W, because W's effective declaration is what judges it.
        r_row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(r_row["suite_command"]["argv"], ["true"])
        state, _rid, why = gate.bind("gate:" + r_row["id"], self.head,
                                     repo_id=common, consuming_repo=w)
        self.assertEqual(state, "REFUSED")
        self.assertIn("effectively declares", why)

    def test_a_recorded_checkout_this_helm_cannot_read_admits_nothing(self):
        """F1's fail-closed half. A coordinate is only as good as the tree it
        names: a recorded checkout that has since been removed cannot be asked
        what it declares, and falling back to the repository there would answer
        from the very census the coordinate exists to narrow. UNKNOWN, and no
        admission — never the repository's answer wearing the row's coordinate.

        AND A READABLE COORDINATE IN ANOTHER REPOSITORY IS REFUSED, not believed:
        `repo_root` is a field of the row, so a coordinate spent without being
        bound to the repository this answer is for would let the row nominate the
        declaration that judges it — the channel closed for `repo_id` one round
        ago, re-opened one field over.
        """
        self._fresh_import_memos()
        w, common = self._root_and_lane()
        require_supervisor()
        w_row, err = gate.run(repo=w)
        self.assertIsNone(err, err)
        # THE STANDING POSITIVE FIRST, on the same call: while W is readable the
        # identical bind is VERIFIED, so every refusal below is about the
        # coordinate and not about this row.
        state, _rid, why = gate.bind("gate:" + w_row["id"], self.head,
                                     repo_id=common, consuming_repo=w)
        self.assertEqual(state, "VERIFIED", why)
        # POLE ONE: the checkout is gone. MEASURED THROUGH THE SAME SEAM the
        # doors read — git cannot speak for it at all.
        self._git("worktree", "remove", "--force", w)
        shutil.rmtree(w, ignore_errors=True)
        # THE PATH->REPOSITORY ANSWER IS MEMOISED FOR THE PROCESS, and the door
        # under test no longer reads that memo for a spend (round nine: it
        # validates the checkout on the filesystem and reads the seam FRESH). The
        # clear here is only what makes the READER's reading below the
        # filesystem's — the arm beside this one keeps the memo WARM and proves
        # the door does not need it. BLAST RADIUS: this reading.
        vcs._COMMON_DIR.clear()
        self.assertIsNone(gate._repository_of(w))
        state, rid, why = gate.bind("gate:" + w_row["id"], self.head,
                                    repo_id=common, consuming_repo=w)
        self.assertEqual(state, "REFUSED")
        self.assertEqual(rid, w_row["id"])
        self.assertIn("UNKNOWN", why)
        self.assertIn(w, why)
        # AND IT DID NOT QUIETLY BECOME THE REPOSITORY'S QUESTION: the ambiguity
        # sentence is what the common dir answers, and it is not this answer.
        self.assertNotIn("authored gate declarations", why)
        # POLE TWO: a readable coordinate belonging to a DIFFERENT repository.
        other, _head, _tree = self._unrelated_repo()
        self.assertNotEqual(gate._repository_of(other), common)
        state, _rid, why = gate.bind("gate:" + w_row["id"], self.head,
                                     repo_id=common, consuming_repo=other)
        self.assertEqual(state, "REFUSED")
        self.assertIn(other, why)
        self.assertIn("never carries the answer to another repository", why)

    def test_a_row_with_no_coordinate_still_gets_the_repositorys_own_answer(self):
        """F1's compatibility pole, which is the whole reason the fallback stays.
        A row minted before the coordinate existed carries none, and the honest
        answer for it is the repository's — including the ambiguity refusal when
        the repository genuinely cannot say. Nothing here is grandfathered into
        an admission.
        """
        self._fresh_import_memos()
        w, common = self._root_and_lane()
        require_supervisor()
        w_row, err = gate.run(repo=w)
        self.assertIsNone(err, err)
        # AN ABSENT COORDINATE IS EVERY SPELLING OF ABSENT, and each one resolves
        # to the repository the caller named. BLAST RADIUS: this loop with the
        # positive below — a row that names its tree still binds.
        for absent in (None, ""):
            with self.subTest(coordinate=absent):
                state, _rid, why = gate.bind("gate:" + w_row["id"], self.head,
                                             repo_id=common,
                                             consuming_repo=absent)
                self.assertEqual(state, "REFUSED")
                self.assertIn("authored gate declarations", why)
        # THE POSITIVE, SAME OBSERVABLE: with ONE declaration in the repository
        # the coordinate-less bind is VERIFIED, so the refusals above are the
        # ambiguous census and not "a bind without a coordinate never passes".
        self.declare(command=("./run-suite.sh",), path=w, name="adopter-lane")
        state, _rid, why = gate.bind("gate:" + w_row["id"], self.head,
                                     repo_id=common)
        self.assertEqual(state, "VERIFIED", why)


    def test_a_removed_checkout_with_a_warm_memo_admits_nothing(self):
        """Round nine (B). The path-to-repository seam is memoised per process,
        and the round-eight validation of the recorded checkout asked that memo:
        a worktree W REMOVED from the filesystem after the memo warmed still
        answered its repository from the entry, so the validation passed for a
        tree that no longer existed and a scoped close admitted a receipt for
        it. The consuming checkout is validated against the FILESYSTEM at the
        moment of every spending act, and the memo is re-read, never trusted.

        THE MEMO IS KEPT WARM ON PURPOSE — that is the whole arm. The sibling
        above clears it; this one proves the door does not need the clear.
        """
        self._fresh_import_memos()
        w, common = self._root_and_lane()
        require_supervisor()
        w_row, err = gate.run(repo=w)
        self.assertIsNone(err, err)
        # THE LIVE-W CONTROL FIRST, on the same call: admitted while W stands.
        state, _rid, why = gate.bind("gate:" + w_row["id"], self.head,
                                     repo_id=common, consuming_repo=w)
        self.assertEqual(state, "VERIFIED", why)
        key = (type(vcs.backend(w)), os.path.realpath(w))
        self.assertIn(key, vcs._COMMON_DIR, "the memo is not warm")
        self._git("worktree", "remove", "--force", w)
        shutil.rmtree(w, ignore_errors=True)
        # CONTROL ON THE PREMISE, through the same seam the round-eight door
        # read: the memo is STILL warm and STILL answers the old repository for
        # the removed tree — so a door trusting it would admit. BLAST RADIUS:
        # this pair with the refusal below.
        self.assertIn(key, vcs._COMMON_DIR)
        self.assertEqual(gate._repository_of(w), common)
        state, rid, why = gate.bind("gate:" + w_row["id"], self.head,
                                    repo_id=common, consuming_repo=w)
        self.assertEqual(state, "REFUSED")
        self.assertEqual(rid, w_row["id"])
        self.assertIn("UNKNOWN", why)
        self.assertIn(w, why)
        self.assertNotIn("authored gate declarations", why)

    def test_the_act_re_reads_the_memo_instant_from_the_filesystem(self):  # noqa: VACUOUS_ASSERTION — every reading here is an unconditional positive equality: the memo answers the right repository before the poison, the poisoned reader answers the unrelated repository (proving the poison took), the bind is VERIFIED, the memo answers the right repository again after the act and its instant is strictly later than the one recorded before
        """(B)'s other direction: the memo entry is MUTATED under a live tree,
        and the act still answers from the filesystem — re-reading the seam
        fresh and re-stamping the entry with what it measured. A door that
        trusted the memo would refuse this honest bind as "another repository".
        BLAST RADIUS: this arm — the reader control shows the poison took.
        """
        self._fresh_import_memos()
        w, common = self._root_and_lane()
        require_supervisor()
        w_row, err = gate.run(repo=w)
        self.assertIsNone(err, err)
        backend = vcs.backend(w)
        key = (type(backend), os.path.realpath(w))
        self.assertEqual(gate._repository_of(w), common)   # warm, and right
        before = backend.validated_at(w)
        self.assertIsNotNone(before)
        other, _head, _tree = self._unrelated_repo()
        vcs._COMMON_DIR[key] = gate._repository_of(other)
        # CONTROL ON THE PREMISE: a READER now sees the poison.
        self.assertEqual(gate._repository_of(w), gate._repository_of(other))
        state, _rid, why = gate.bind("gate:" + w_row["id"], self.head,
                                     repo_id=common, consuming_repo=w)
        self.assertEqual(state, "VERIFIED", why)
        # AND THE ACT RE-STAMPED THE MEMO with the filesystem's answer, at a
        # later instant than the one it found.
        self.assertEqual(gate._repository_of(w), common)
        self.assertGreater(backend.validated_at(w), before)

    def test_every_spending_door_asks_the_one_effective_resolver(self):
        """The gate-side resolver-identity arm. `_effective_declaration` is the
        ONE answer to "which declaration governs this checkout"; this SPIES THE
        REAL CALL (wrapped, never replaced) and drives the spawn door, the
        row-level admission door and the bind door through the shipped verbs,
        asserting each reached it for the consuming checkout. The composition
        capture, its replay and the verdict writer are proven to reach `bind`
        with the row's checkout by their own spy arms
        (tests.test_compose_contract, tests.test_dispatches), and `bind` is
        one of the doors here. BLAST RADIUS: this arm.
        """
        self._fresh_import_memos()
        w, common = self._root_and_lane()
        require_supervisor()
        w_row, err = gate.run(repo=w)
        self.assertIsNone(err, err)
        seen = []
        real = gate._effective_declaration

        def spy(repo):
            seen.append(repo)
            return real(repo)

        def drives(label, act):
            del seen[:]
            act()
            self.assertIn(w, seen, "%s never asked the resolver for W" % label)

        with mock.patch.object(gate, "_effective_declaration", spy):
            drives("suite_command", lambda: self.assertEqual(
                gate.suite_command(w)[0]["argv"], ["./run-suite.sh"]))
            drives("row_refusal", lambda: self.assertIsNone(
                gate.row_refusal(w_row, consuming_repo=w)))
            drives("_declared_origin_refusal", lambda: self.assertIsNone(
                gate._declared_origin_refusal(w_row, consuming_repo=w)))
            drives("bind", lambda: self.assertEqual(
                gate.bind("gate:" + w_row["id"], self.head, repo_id=common,
                          consuming_repo=w)[0], "VERIFIED"))


class TheGateFixtureRestoresTheEnvironmentItInherited(unittest.TestCase):
    """F5 (P3). ONE environment-restoration owner, in a fixed order.

    `_tmphome.helm_tree` sets HELM_CROSS_TREE_GATE=1, and a restoration
    registered per CALL cannot restore it: unittest runs addCleanup AFTER
    tearDown and in LIFO order, so with two calls in one fixture the captured
    values are None and then "1", tearDown puts an INCOMING 1 back, and the
    first call's cleanup then pops it. The variable is in ENV_KEYS precisely
    because a suite that inherits it passes or fails by what the host exported,
    so losing it mid-run is the same host-coupled class one step later. One
    owner per key, and the whole-environment snapshot registered first, is what
    keeps the process's own value.

    THE PROBE IS THE SHIPPED FIXTURE, driven through the real lifecycle. Nothing
    about ordering is modelled here, because the ordering IS the defect.
    """

    KEY = "HELM_CROSS_TREE_GATE"

    def setUp(self):
        prior = os.environ.get(self.KEY)

        def _restore():
            if prior is None:
                os.environ.pop(self.KEY, None)
            else:
                os.environ[self.KEY] = prior

        self.addCleanup(_restore)

    def _after_one_real_gate_case(self, incoming):
        class Probe(GateBase):
            def runTest(inner):
                # The fixture's own setUp is the subject: it calls `helm_tree`
                # twice, so the override is set while a test body runs.
                inner.assertEqual(os.environ.get("HELM_CROSS_TREE_GATE"), "1")

        if incoming is None:
            os.environ.pop(self.KEY, None)
        else:
            os.environ[self.KEY] = incoming
        result = Probe().run()
        self.assertTrue(result.wasSuccessful(),
                        [result.errors, result.failures])
        return os.environ.get(self.KEY)

    def test_an_incoming_cross_tree_override_survives_the_fixture(self):
        """The defect, measured: before the cure this returned None."""
        self.assertEqual(self._after_one_real_gate_case("1"), "1")

    def test_an_absent_cross_tree_override_stays_absent(self):  # noqa: VACUOUS_ASSERTION — the arm above is the unconditional positive on the same observable (incoming '1' comes back '1'); a fixture must not CONFER the override, so the absence is the product law
        """The absent-file control, and it is why the cure cannot be "always put
        1 back": a process that never set the override must not acquire it from
        a fixture. BLAST RADIUS: this pair — the two arms together admit exactly
        one behaviour, restoring what the process actually arrived with."""
        self.assertIsNone(self._after_one_real_gate_case(None))


class TheUnrunnableGateSaysSoInsteadOfFailing(unittest.TestCase):
    """The probe that turns 153 environment-shaped failures into skips.

    THE FAILURE MODE THIS CLASS EXISTS TO REFUSE is an always-skip probe. A
    probe that answered "unavailable" everywhere would turn 153 red arms into
    153 permanently silent ones, and nothing downstream would ever notice:
    a skip is quiet by design. So every arm here pins ONE of the probe's
    answers against a root it built itself, and the pair — a writable root
    that yields None beside an unwritable one that yields a reason — is the
    must-miss control. Without the None arm, the skip could not be shown to
    DISCRIMINATE, only to fire.

    They ask `_cgroup_root` through mock, never the host's real cgroup, so the
    arms answer the same on a fab node, under `fab gate`, and on a laptop.
    """

    def _root(self, mode=0o700):
        parent = tempfile.mkdtemp(prefix="helm-test-cgroup-")
        root = os.path.join(parent, "r")
        os.makedirs(root)

        def _clean():
            # RESTORE THE MODE BEFORE REMOVING, and only while the directory
            # is still there: one arm removes the root itself, and a 0o500 or
            # 0o600 root cannot be walked — `rmtree(ignore_errors=True)` would
            # swallow that and leave the directory on the node forever.
            if os.path.isdir(root):
                os.chmod(root, 0o700)
            shutil.rmtree(parent, ignore_errors=True)

        self.addCleanup(_clean)
        os.chmod(root, mode)
        return root

    def _reason(self, root):
        with mock.patch.object(gatechild, "_cgroup_root", lambda: root):
            return _gate_supervisor.unavailable_reason()

    def test_a_writable_root_is_the_runnable_answer_and_it_is_None(self):  # noqa: VACUOUS_ASSERTION — the four arms below are the unconditional positives on this same observable (unwritable -> "lacks write access", unsearchable -> "search", missing -> "is missing", unreadable -> "unreadable"); None here is the product law and cannot be asserted any other way
        """MUST-MISS. `fab gate` grants exactly this, and the arms must run
        there — if this ever returns a reason the whole suite goes silent."""
        self.assertIsNone(self._reason(self._root()))

    def test_an_unwritable_root_names_the_access_it_lacks(self):
        """The measured condition on a fab test node, reproduced."""
        reason = self._reason(self._root(mode=0o500))
        self.assertIn("lacks write access", reason)

    def test_an_unsearchable_root_is_reported_too_and_names_search(self):
        reason = self._reason(self._root(mode=0o600))
        self.assertIn("search", reason)

    def test_a_missing_root_is_missing_and_not_an_access_answer(self):
        root = self._root()
        os.rmdir(root)
        reason = self._reason(root)
        self.assertIn("is missing", reason)
        self.assertNotIn("access", reason)

    def test_an_unreadable_cgroup_is_the_environments_fault_in_words(self):
        self.assertIn("unreadable", self._reason(None))

    def test_the_probe_creates_nothing_under_the_root_it_reads(self):
        """READ-ONLY, measured rather than asserted from the source: the probe
        is consulted by every guarded arm and a probe with a side effect would
        leave one cgroup behind per collection.

        THE PLANTED ENTRY IS THE INSTRUMENT'S OWN MUST-HIT. An empty listing
        would have proved nothing here: a listing that cannot see anything is
        empty too, and `_create_cgroup` makes a directory whose name this test
        does not know in advance. The sentinel makes the expected reading
        NON-EMPTY, so the arm fails both when the probe adds something and
        when the listing has gone blind."""
        root = self._root()
        with open(os.path.join(root, "planted"), "w"):
            pass
        self._reason(root)                 # the full three-question path
        self.assertEqual(os.listdir(root), ["planted"])

    def test_the_skip_names_the_measured_cause_and_the_runner_that_works(self):
        """A skip that does not say WHY and WHERE is the same silence the red
        arms were: the reader must be able to go from this sentence to a green
        run without asking anyone."""
        with mock.patch.object(gatechild, "_cgroup_root",
                               lambda: self._root(mode=0o500)):
            with self.assertRaises(unittest.SkipTest) as caught:
                _gate_supervisor.require_supervisor()
        said = str(caught.exception)
        self.assertIn("lacks write access", said)
        self.assertIn("fab gate", said)

    def test_the_raising_door_goes_BOTH_WAYS_over_one_root(self):
        """Both directions of the skip, on one root, in one arm: the call that
        must stay quiet and the call that must fire are the same call, and the
        only thing that changed between them is the permission bit this whole
        cure is about. The quiet half needs no assertion — a SkipTest here
        would be REPORTED, and that is the failure mode (153 silent arms) this
        class exists to catch."""
        root = self._root()
        with mock.patch.object(gatechild, "_cgroup_root", lambda: root):
            _gate_supervisor.require_supervisor()        # writable: no skip
            os.chmod(root, 0o500)
            with self.assertRaises(unittest.SkipTest) as caught:
                _gate_supervisor.require_supervisor()
        self.assertIn("lacks write access", str(caught.exception))


def setUpModule():
    """No dispatch row this module writes walks the host's process table
    (task/3039; see tests._tmphome.pin_live_seats)."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()
