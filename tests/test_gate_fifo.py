#!/usr/bin/env python3
"""The whole-suite gate FIFO: durable order outranks poll rate."""
import ast
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from helm import chat, gate, gatechild, pk, seats, seats_claims, seats_common
from tests._tmphome import corroborate as _tmp_corroborate
from tests._tmphome import helm_tree
from tests._tmphome import declare as _tmp_declare
from tests._tmphome import session_for as _tmp_session_for
from tests._tmphome import pin_admission, pin_admission_code
from tests._gate_supervisor import require_supervisor


def _assert_gate_status(case, row, expected):
    """Assert a gate row's status AND publish the row's own diagnosis.

    gate.py names the bug class this closes: "a surface that holds the
    deciding information and does not say it". A bare equality prints
    'FAILED' != 'OK' and throws away `detail` and `failures` — the two fields
    that say WHICH inner test failed and why.

    That is not hypothetical. This module's own
    test_completed_suite_quiesces_a_forking_detached_tree came back FAILED
    under a whole-suite gate while passing alone, and the receipt carried the
    verdict with not one word of its cause: the inner run's detail and failure
    list existed, in this very row, and the assertion discarded both. The
    investigation that followed had to reason from a tail of unrelated stderr.
    """
    case.assertEqual(row.get("status"), expected,
                     "gate row detail=%r failures=%r" % (
                         row.get("detail"), row.get("failures")))


ENV_KEYS = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_ROOM",
            "HELM_CHAT_NODE_URL", "HELM_CHAT_NAME", "HELM_PROC",
            "CLAUDE_SESSION_ID",
            "CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID")


class GuardDiagnosticTest(unittest.TestCase):
    GENERIC = "gate guard exited before reporting its supervisor"
    GUARD_ARGV = [
        "--guard", "--parent", "1", "--parent-start", "2",
        "--position", "deadbeef", "--ready-fd", "9",
        "--report-fd", "10", "--", "python3",
    ]

    def parent_rendered(self, child_stderr):
        read_fd, write_fd = os.pipe()
        try:
            with os.fdopen(write_fd, "w") as stream:
                write_fd = None
                stream.write(child_stderr)
            proc = mock.Mock(stderr=os.fdopen(read_fd, "r"))
            read_fd = None
            try:
                return gate._guard_diagnostic(proc, self.GENERIC)
            finally:
                proc.stderr.close()
        finally:
            for fd in (read_fd, write_fd):
                if fd is not None:
                    os.close(fd)

    def rendered(self, reason=None):
        child = io.StringIO()
        with mock.patch.object(sys, "stderr", child):
            if reason is not None:
                self.assertEqual(gatechild._refuse(reason), 125)
        return self.parent_rendered(child.getvalue())

    def guard_reason(self, **patches):
        child = io.StringIO()
        managers = [mock.patch.object(sys, "stderr", child)]
        managers.extend(mock.patch.object(gatechild, name, value)
                        for name, value in patches.items())
        exits = []
        try:
            for manager in managers:
                exits.append(manager)
                manager.__enter__()
            self.assertEqual(gatechild._guard(self.GUARD_ARGV), 125)
        finally:
            for manager in reversed(exits):
                manager.__exit__(None, None, None)
        return child.getvalue()

    def test_create_cgroup_names_missing_root_as_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "absent")
            with mock.patch.object(gatechild, "_cgroup_root",
                                   return_value=root):
                path, reason = gatechild._create_cgroup("deadbeef")
        self.assertIsNone(path)
        self.assertIn(root, reason)
        self.assertIn("missing", reason)
        self.assertNotIn("access", reason)

    @unittest.skipIf(os.geteuid() == 0,
                     "root bypasses directory write mode bits")
    def test_create_cgroup_names_real_root_write_denial(self):
        with tempfile.TemporaryDirectory() as root:
            os.chmod(root, 0o555)
            try:
                with mock.patch.object(gatechild, "_cgroup_root",
                                       return_value=root):
                    path, reason = gatechild._create_cgroup("deadbeef")
            finally:
                os.chmod(root, 0o700)
        self.assertIsNone(path)
        self.assertIn(root, reason)
        self.assertIn("write access", reason)
        self.assertNotIn("search access", reason)

    def test_create_cgroup_names_missing_interface_as_missing(self):
        with tempfile.TemporaryDirectory() as root:
            probe = os.path.join(
                root, "helm-gate-deadbeef", "cgroup.procs")
            with mock.patch.object(gatechild, "_cgroup_root",
                                   return_value=root):
                path, reason = gatechild._create_cgroup("deadbeef")
        self.assertIsNone(path)
        self.assertIn(probe, reason)
        self.assertIn("missing required interface", reason)
        self.assertNotIn("access", reason)

    @unittest.skipIf(os.geteuid() == 0,
                     "root bypasses file write mode bits")
    def test_join_cgroup_real_failures_are_precise_and_distinct(self):
        with tempfile.TemporaryDirectory() as root:
            invalid_path = os.path.join(root, "not-a-gate-cgroup")
            missing_path = os.path.join(root, "helm-gate-cafebabe")
            path = os.path.join(root, "helm-gate-deadbeef")
            os.mkdir(path)
            probe = os.path.join(path, "cgroup.procs")
            with open(probe, "w") as f:
                f.write("")
            with mock.patch.object(gatechild, "_cgroup_root",
                                   return_value=root):
                invalid = gatechild._join_cgroup(invalid_path)
                missing = gatechild._join_cgroup(missing_path)
                os.chmod(probe, 0o444)
                try:
                    denied = gatechild._join_cgroup(path)
                finally:
                    os.chmod(probe, 0o600)
                joined = gatechild._join_cgroup(path)
                with mock.patch.object(gatechild, "_cgroup_members",
                                       return_value=[]):
                    retained = gatechild._join_cgroup(path)
        self.assertEqual(joined, (True, None))
        failures = (invalid, missing, denied, retained)
        reasons = [result[1] for result in failures]
        self.assertTrue(all(result[0] is False for result in failures))
        self.assertTrue(all(reasons))
        self.assertEqual(len(set(reasons)), 4)
        self.assertIn(invalid_path, invalid[1])
        self.assertIn("invalid", invalid[1])
        self.assertIn(missing_path, missing[1])
        self.assertIn("missing", missing[1])
        self.assertIn(probe, denied[1])
        self.assertIn("write access", denied[1])
        self.assertIn(probe, retained[1])
        self.assertIn(str(os.getpid()), retained[1])
        self.assertIn("did not retain", retained[1])

    def test_child_reason_and_parent_reader_compose_at_the_error_surface(self):
        root = "/sys/fs/cgroup/user.slice/session-N.scope"
        reason = "cgroup root %s lacks write/search access" % root
        child = self.guard_reason(
            _arm_parent_death=mock.Mock(return_value=True),
            _proc_rows=mock.Mock(return_value={
                os.getpid(): (1, 2, "S")}),
            _create_cgroup=mock.Mock(return_value=(None, reason)))
        rendered = self.parent_rendered(child)
        self.assertIn(self.GENERIC, rendered)
        self.assertIn(root, rendered)
        self.assertIn("write/search access", rendered)

    def test_queued_process_surfaces_child_reason_before_unrunnable_return(self):
        reason = "guard proc row is unreadable for pid 71"
        child = io.StringIO()
        with mock.patch.object(sys, "stderr", child):
            self.assertEqual(gatechild._refuse(reason), 125)
        stderr = tempfile.TemporaryFile(mode="w+")
        stderr.write(child.getvalue())
        stderr.seek(0)
        proc = mock.Mock(pid=71, stderr=stderr)
        watcher = mock.Mock()
        watcher.wait.return_value = 0

        def refused(_proc, report_fd):
            os.close(report_fd)
            return None, self.GENERIC

        position = {"id": "deadbeef", "starttime": 9}
        try:
            with mock.patch.object(gatechild, "_cgroup_path",
                                   return_value="/tmp/helm-gate-deadbeef"), \
                    mock.patch.object(gatechild, "_kill_cgroup",
                                      return_value=True), \
                    mock.patch.object(seats, "_get_live_pid_starttime",
                                      return_value=10), \
                    mock.patch.object(subprocess, "Popen",
                                      side_effect=(proc, watcher)), \
                    mock.patch.object(gate, "_guard_supervisor",
                                      side_effect=refused), \
                    mock.patch.object(gate, "_kill_group"):
                result = gate._queued_process("/tmp", ["python3"], position, 1)
        finally:
            stderr.close()
        self.assertEqual(result[:3], ("", "", None))
        self.assertIn(self.GENERIC, result[3])
        self.assertIn(reason, result[3])

    def test_silent_child_cannot_invent_a_specific_reason(self):  # noqa: VACUOUS_ASSERTION — the adjacent child-write control proves the same parent reader surfaces a real reason; this arm pins that empty child evidence stays the generic UNKNOWN diagnosis
        self.assertEqual(self.rendered(), self.GENERIC)

    def test_distinct_guard_failures_render_distinct_specific_reasons(self):
        missing = self.guard_reason(
            _arm_parent_death=mock.Mock(return_value=True),
            _proc_rows=mock.Mock(return_value={}))
        root = "/sys/fs/cgroup/user.slice/session-N.scope"
        denied_reason = "cgroup root %s lacks write access" % root
        denied = self.guard_reason(
            _arm_parent_death=mock.Mock(return_value=True),
            _proc_rows=mock.Mock(return_value={
                os.getpid(): (1, 2, "S")}),
            _create_cgroup=mock.Mock(return_value=(None, denied_reason)))
        rendered = [self.parent_rendered(row) for row in (missing, denied)]
        self.assertNotEqual(rendered[0], rendered[1])
        self.assertIn("guard proc row is unreadable", rendered[0])
        self.assertIn(denied_reason, rendered[1])
        for result in rendered:
            self.assertIn(self.GENERIC, result)


class GateQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-gate-fifo-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_ROOM"] = "queue-room"
        os.environ["HELM_CHAT_NODE_URL"] = ""
        self.repo = os.path.join(self.tmp, "repo")
        self.proc = os.path.join(self.tmp, "proc")
        os.makedirs(self.repo)
        os.makedirs(self.proc)
        # The fixture proc tree (stat rows, no cmdlines) is for the queue's
        # readers. ADMISSION never read HELM_PROC: it gets its own fixture box
        # here and in every child `worker` (task/1740).
        os.environ["HELM_PROC"] = self.proc
        self.admission_proc, self.admissions = pin_admission(self)
        subprocess.run(("git", "init", "-q", "-b", "main"), cwd=self.repo,
                       check=True)
        subprocess.run(("git", "config", "user.email", "fifo@test"),
                       cwd=self.repo, check=True)
        subprocess.run(("git", "config", "user.name", "fifo test"),
                       cwd=self.repo, check=True)
        # THIS FIXTURE REPO STANDS IN FOR A TREE THAT SHIPS HELM, which is
        # the only tree whose whole-suite command helm may assume — see
        # `helm_tree`. An adopter project declares its own instead.
        helm_tree(self, self.repo)
        with open(os.path.join(self.repo, "a"), "w") as f:
            f.write("one\n")
        subprocess.run(("git", "add", "-A"), cwd=self.repo, check=True)
        subprocess.run(("git", "commit", "-qm", "first"), cwd=self.repo,
                       check=True)

    @classmethod
    def setUpClass(cls):
        # THE ENV THIS CLASS WAS HANDED. tearDownClass compares against it,
        # which is the only point in the lifecycle AFTER every test's cleanup.
        cls._entry_env = {k: os.environ.get(k)
                          for k in ("HELM_HOME", "HELM_CHAT_DIR")}

    @staticmethod
    def _assert_env_unpoisoned(entry):
        """Raise unless HELM_HOME/HELM_CHAT_DIR are what `entry` recorded.

        Kept a staticmethod so an arm can call it directly and prove it
        refuses — a witness nobody can make fail is not a witness."""
        now = {k: os.environ.get(k) for k in entry}
        assert now == entry, (
            "a test in this class left HELM_HOME/HELM_CHAT_DIR changed: %r != "
            "%r. A per-test env snapshot restored via addCleanup runs AFTER "
            "tearDown and undoes the class restore, pointing both keys at a "
            "deleted tree." % (now, entry))

    @classmethod
    def tearDownClass(cls):
        cls._assert_env_unpoisoned(cls._entry_env)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for key, val in self.prior.items():
            os.environ.pop(key, None)
            if val is not None:
                os.environ[key] = val

    def alive(self, pid, start, state="S"):
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d, exist_ok=True)
        # /proc/<pid>/stat fields after comm: state, ppid, then field 5..21,
        # then field 22 starttime at tail index 19.
        fields = [state, "1"] + ["0"] * 17 + [str(start)]
        with open(os.path.join(d, "stat"), "w") as f:
            f.write("%d (gate child) %s\n" % (pid, " ".join(fields)))

    def dead(self, pid):
        shutil.rmtree(os.path.join(self.proc, str(pid)), ignore_errors=True)

    def enqueue(self, seat, pid, start):
        self.alive(pid, start)
        state, row, err = seats.gate_queue_enqueue(
            self.repo, seat, pid=pid, proc_dir=self.proc)
        self.assertEqual((state, err), ("QUEUED", None))
        return row

    def fixture_suite(self, source):
        tests = os.path.join(self.repo, "tests")
        os.makedirs(tests, exist_ok=True)
        with open(os.path.join(tests, "__init__.py"), "w") as f:
            f.write("")
        with open(os.path.join(tests, "_gate_pid.py"), "w") as f:
            f.write('''import os


def host_self():
    with open("/proc/self/status") as status:
        nspid = next(line for line in status if line.startswith("NSpid:"))
    with open("/proc/self/stat") as stat:
        fields = stat.read().rsplit(") ", 1)[1].split()
    return int(nspid.split()[1]), int(fields[19])


def host_child(pid):
    parent, _starttime = host_self()
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name) as stat:
                fields = stat.read().rsplit(") ", 1)[1].split()
            if int(fields[1]) != parent:
                continue
            with open("/proc/%s/status" % name) as status:
                nspid = next(line for line in status
                             if line.startswith("NSpid:"))
            if int(nspid.split()[-1]) == pid:
                return int(name), int(fields[19])
        except (OSError, StopIteration, IndexError, ValueError):
            continue
    raise RuntimeError("child host PID is unavailable")
''')
        with open(os.path.join(tests, "test_fifo_fixture.py"), "w") as f:
            f.write(source)
        subprocess.run(("git", "add", "-A"), cwd=self.repo, check=True)
        subprocess.run(("git", "commit", "-qm", "fifo fixture"), cwd=self.repo,
                       check=True)

    def lines(self, path):
        try:
            with open(path) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def wait_for(self, predicate, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("condition did not become true within %.1fs" % timeout)

    def expire_claim(self, resource):
        """Retire a live claim with no wall-clock wait.

        The record stores ABSOLUTE `exp_mono`/`exp_wall`, so pushing both into
        the past expires it exactly as time would have — the synchronisation
        point that replaces `time.sleep(<ttl>)` in tests that only need the
        claim to be gone, not to observe it aging. This suite already writes
        those fields directly elsewhere; this names the idiom once."""
        path = seats.claims_path()
        claims = pk.read_json(path) or {}
        row = claims.get(resource)
        self.assertIsNotNone(row, "no claim on %s to expire" % resource)
        row["exp_mono"] = seats._now_mono() - 1.0
        row["exp_wall"] = time.time() - 1.0
        pk.write_json(path, claims)

    def worker(self, seat, timeout=None):
        """A whole-suite `gate.run` in its own process, as seat `seat`.

        It spawns the gate guard, so it needs the guard's cgroup supervisor
        and skips the calling arm where that is measured as absent. Every
        other guard failure still reaches the arm as a red."""
        require_supervisor()
        env = dict(os.environ)
        env["HELM_CHAT_NAME"] = seat
        env["CODEX_SESSION_ID"] = "session-" + seat
        # THE CHILD EXPORTS A SESSION; THE ROSTER IS WHAT MAKES IT MEAN
        # SOMETHING, and the roster lives on disk under the HELM_HOME the child
        # inherits — so the parent writes the binding. Without it the child is
        # a DECLARED name nothing corroborates, the gate falls back to its
        # SystemLeaseCapability holder, and the queue posts read
        # `@system:gate:<sid8>` where these arms look for `@alpha`.
        self.addCleanup(_tmp_corroborate(seat, "session-" + seat))
        code = pin_admission_code(self.admission_proc, self.admissions) + (
            "from helm import gate; import sys; "
            "row, err = gate.run(repo=%r, timeout=%r); "
            "print(err or row['id']); sys.exit(1 if err else 0)"
            % (self.repo, timeout))
        return subprocess.Popen((sys.executable, "-c", code), env=env,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    def test_an_orphan_is_readable_without_running_a_queue_operation(self):
        """task/407: every publisher of a GATE ORPHAN notice today sits INSIDE
        a queue operation, so a launcher that dies while nothing else is
        gating is announced to nobody. This is the read that does not need
        anyone else to gate first.

        BOTH CONTROLS, because they catch opposite failures and neither
        substitutes for the other: a probe that never reports an orphan and a
        probe that reports every row both look calm from one side.
        """
        alive = self.enqueue("alpha", 201, 2001)
        doomed = self.enqueue("beta", 202, 2002)

        # NEGATIVE CONTROL FIRST: nothing is orphaned while both launchers
        # live, so a sweep that always fires is caught before its positive.
        quiet, err = seats.gate_queue_orphans(self.repo, proc_dir=self.proc)
        self.assertIsNone(err, err)
        self.assertEqual(quiet, [], "a sweep that reports live rows as "
                                    "orphans cannot be trusted when it does "
                                    "report one: %r" % (quiet,))

        # Now beta's launcher is gone — the fixture /proc entry IS the process.
        shutil.rmtree(os.path.join(self.proc, "202"))

        found, err = seats.gate_queue_orphans(self.repo, proc_dir=self.proc)
        self.assertIsNone(err, err)
        self.assertEqual([row["id"] for row in found], [doomed["id"]],
                         "the dead launcher's row must be the only orphan")
        self.assertEqual(found[0]["holder"], "beta")
        # AND THE LIVE ROW MUST STILL NOT BE IN IT. Equality above already
        # says so, but naming it separately is what makes a future widening
        # of the predicate fail here rather than pass quietly.
        self.assertNotIn(alive["id"], [row["id"] for row in found])
        # THE QUEUE IS UNCHANGED BY LOOKING: this is a diagnostic, and one
        # that reaps would make `helm gate list` destructive.
        live, err = seats.gate_queue_snapshot(self.repo, proc_dir=self.proc)
        self.assertIsNone(err, err)
        self.assertEqual([row["id"] for row in live], [alive["id"]])

    def test_deep_queue_backfills_every_orphan_beyond_sixty_four(self):
        """The old symptom appeared only after a deep wait. Exercise 72 real
        queue positions so a hidden 64-row traversal cap cannot return a
        confident finish while leaving the tail unrecorded.
        """
        rows = [self.enqueue("deep-%02d" % i, 400 + i, 4000 + i)
                for i in range(72)]
        survivor = rows[-1]
        for i in range(71):
            self.dead(400 + i)

        pending, err = seats.gate_queue_recover_orphans(
            self.repo, proc_dir=self.proc)
        self.assertIsNone(err, err)
        self.assertEqual(len(pending), 71)
        self.assertEqual(pending[-1]["seq"], 71,
                         "the walk stopped before the row beyond a 64 cap")
        for row in pending:
            self.assertEqual(row["diagnostic"], {
                "kind": "orphan", "status": "UNKNOWN",
                "stage": "waiting", "reason": "launcher-gone"})
        active, err = seats.gate_queue_snapshot(self.repo, proc_dir=self.proc)
        self.assertIsNone(err, err)
        self.assertEqual([row["id"] for row in active], [survivor["id"]],
                         "mutation control: a live tail must stay runnable, not "
                         "be classified UNKNOWN with the dead population")

    def test_deep_finisher_survives_failed_publication_without_a_receipt(self):
        rows = [self.enqueue("attempt-%02d" % i, 600 + i, 6000 + i)
                for i in range(67)]
        for row in rows[:65]:
            state, _current, err = seats.gate_queue_try_start(
                self.repo, row["id"], pid=row["pid"], proc_dir=self.proc)
            self.assertEqual((state, err), ("START", None))
            ok, _nxt, _pending, err = seats.gate_queue_prepare_finish(
                self.repo, row["id"], pid=row["pid"], proc_dir=self.proc,
                result="OK gate:0123456789abcdef")
            self.assertTrue(ok, err)
            self.assertEqual(seats.gate_queue_ack_terminal(
                self.repo, row["id"]), (True, None))
            ok, _nxt, pending, err = seats.gate_queue_finish(
                self.repo, row["id"], pid=row["pid"], proc_dir=self.proc)
            self.assertTrue(ok, err)
            self.assertEqual(pending, [])

        victim, survivor = rows[65:]
        state, _current, err = seats.gate_queue_try_start(
            self.repo, victim["id"], pid=victim["pid"], proc_dir=self.proc)
        self.assertEqual((state, err), ("START", None))
        ok, _nxt, _pending, err = seats.gate_queue_prepare_finish(
            self.repo, victim["id"], pid=victim["pid"], proc_dir=self.proc,
            result="UNKNOWN — receipt import failed")
        self.assertTrue(ok, err)
        self.dead(victim["pid"])
        pending, err = seats.gate_queue_recover_orphans(
            self.repo, proc_dir=self.proc)
        self.assertIsNone(err, err)
        self.assertEqual([row["id"] for row in pending], [victim["id"]])
        self.assertEqual(pending[0]["seq"], 66)
        self.assertEqual(pending[0]["diagnostic"], {
            "kind": "finish", "status": "UNKNOWN", "stage": "finishing",
            "reason": "receipt import failed"})
        with mock.patch.object(gate, "_gate_post", return_value=False):
            gate._gate_post_orphans(self.repo, pending, "observer")
        self.assertEqual(seats.gate_queue_terminals(self.repo), (pending, None))
        self.assertEqual(gate.receipts(), ([], None, 0))
        active, err = seats.gate_queue_snapshot(self.repo, proc_dir=self.proc)
        self.assertIsNone(err, err)
        self.assertEqual([row["id"] for row in active], [survivor["id"]],
                         "positive control: the live follower was also orphaned")

    def test_gate_post_requires_chat_append_evidence(self):
        with mock.patch.object(chat, "post", return_value=None):
            self.assertFalse(gate._gate_post(self.repo, "GATE TEST", "observer"),
                             "no exception was mistaken for publication")
        appended = {"id": "message-id"}
        with mock.patch.object(chat, "post", return_value=appended):
            self.assertTrue(gate._gate_post(self.repo, "GATE TEST", "observer"))

    def test_failed_orphan_publication_stays_pending_until_acknowledged(self):
        doomed = self.enqueue("doomed", 501, 5001)
        follower = self.enqueue("follower", 502, 5002)
        self.dead(501)
        state, current, err = seats.gate_queue_try_start(
            self.repo, follower["id"], pid=502, proc_dir=self.proc)
        self.assertEqual((state, err), ("START", None))
        pending = current.pop("orphaned")
        self.assertEqual([row["id"] for row in pending], [doomed["id"]])

        with mock.patch.object(gate, "_gate_post", return_value=False):
            gate._gate_post_orphans(self.repo, pending, "follower")
        kept, err = seats.gate_queue_terminals(self.repo)
        self.assertIsNone(err, err)
        self.assertEqual([row["id"] for row in kept], [doomed["id"]],
                         "a failed best-effort post erased the only diagnosis")

        posts = []
        with mock.patch.object(
                gate, "_gate_post",
                side_effect=lambda repo, text, observer: posts.append(text) or True):
            gate._gate_post_orphans(self.repo, kept, "follower")
        self.assertEqual(len(posts), 1)
        self.assertIn("diagnostic=UNKNOWN", posts[0])
        self.assertIn("stage=waiting", posts[0])
        self.assertIn("reason=launcher-gone", posts[0])
        self.assertEqual(seats.gate_queue_terminals(self.repo), ([], None))

    def test_unknown_finish_import_failure_is_durable_before_delivery(self):
        state, row, err = seats.gate_queue_enqueue(
            self.repo, "import-failure", pid=os.getpid())
        self.assertEqual((state, err), ("QUEUED", None))
        state, position, err = seats.gate_queue_try_start(
            self.repo, row["id"], pid=os.getpid())
        self.assertEqual((state, err), ("START", None))
        position["_legacy"] = None
        with mock.patch.object(gate, "_gate_post", return_value=False), \
                mock.patch.object(gate, "_admission_release", return_value=None):
            ok, err = gate._finish_position(
                self.repo, position, status="UNKNOWN",
                detail="receipt import failed")
        self.assertTrue(ok, err)
        pending, err = seats.gate_queue_terminals(self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["diagnostic"], {
            "kind": "finish", "status": "UNKNOWN", "stage": "finishing",
            "reason": "receipt import failed"})
        self.assertIn("UNKNOWN — receipt import failed",
                      pending[0]["announced"])

        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            gate._print_queue_orphans(self.repo)
        shown = stderr.getvalue()
        self.assertIn("PENDING TERMINAL", shown)
        self.assertIn("diagnostic=UNKNOWN", shown)
        self.assertIn("reason=receipt import failed", shown)

    def test_json_list_keeps_receipt_array_and_reports_terminal_on_stderr(self):
        row = self.enqueue("json-orphan", 504, 5004)
        self.dead(504)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(os, "getcwd", return_value=self.repo), \
                mock.patch("sys.stdout", stdout), \
                mock.patch("sys.stderr", stderr):
            rc = gate._cmd_list(["--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(stdout.getvalue().strip(), "[]")
        self.assertIn("ORPHANED QUEUE ROW", stderr.getvalue())
        self.assertIn("diagnostic=UNKNOWN", stderr.getvalue())
        pending, err = seats.gate_queue_terminals(self.repo)
        self.assertIsNone(err, err)
        self.assertEqual([terminal["id"] for terminal in pending], [row["id"]])

    def test_unavailable_receipt_ledger_cannot_suppress_terminal_diagnostic(self):
        row = self.enqueue("ledger-orphan", 505, 5005)
        self.dead(505)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(os, "getcwd", return_value=self.repo), \
                mock.patch.object(gate, "receipts",
                                  return_value=([], "ledger unreadable", 0)), \
                mock.patch("sys.stdout", stdout), \
                mock.patch("sys.stderr", stderr):
            rc = gate._cmd_list(["--json"])
        self.assertEqual(rc, 1)
        self.assertEqual(stdout.getvalue(), "")
        shown = stderr.getvalue()
        self.assertIn("ORPHANED QUEUE ROW", shown)
        self.assertIn("diagnostic=UNKNOWN", shown)
        self.assertIn("receipt ledger unavailable: ledger unreadable", shown)
        pending, err = seats.gate_queue_terminals(self.repo)
        self.assertIsNone(err, err)
        self.assertEqual([terminal["id"] for terminal in pending], [row["id"]])

    def test_successful_receipt_finish_is_acknowledged_not_unknown(self):
        state, row, err = seats.gate_queue_enqueue(
            self.repo, "successful", pid=os.getpid())
        self.assertEqual((state, err), ("QUEUED", None))
        state, position, err = seats.gate_queue_try_start(
            self.repo, row["id"], pid=os.getpid())
        self.assertEqual((state, err), ("START", None))
        position["_legacy"] = None
        posts = []
        with mock.patch.object(
                gate, "_gate_post",
                side_effect=lambda repo, text, observer: posts.append(text) or True), \
                mock.patch.object(gate, "_admission_release", return_value=None):
            ok, err = gate._finish_position(
                self.repo, position, status="OK",
                receipt="0123456789abcdef")
        self.assertTrue(ok, err)
        self.assertEqual(posts, [
            "GATE FINISH #1 @successful OK gate:0123456789abcdef"])
        self.assertNotIn("UNKNOWN", posts[0],
                         "mutation control: every finish became UNKNOWN")
        self.assertEqual(seats.gate_queue_terminals(self.repo), ([], None))
        self.assertEqual(seats.gate_queue_snapshot(self.repo), ([], None))

    def test_a_finishing_orphan_carries_the_result_it_already_announced(self):
        """task/408: SIGKILL cannot be caught, so a launcher can die between
        the GATE FINISH post and the row's removal. The row then says
        `finishing`, which is shared by a minted PASS/FAIL, a minted UNKNOWN,
        an UNMINTED run and a REFUSED admission — the reader cannot tell, and
        the wording cure could only stop the notice CONTRADICTING a result
        already on the wall, never say what it was.

        The stamp rides the write that already sets `finishing`, so it opens
        no new window: a death BEFORE that write leaves exactly today's row.
        """
        row = self.enqueue("alpha", 301, 3001)
        state, _r, err = seats.gate_queue_try_start(
            self.repo, row["id"], pid=301, proc_dir=self.proc)
        self.assertEqual((state, err), ("START", None))

        ok, _nxt, _orph, err = seats.gate_queue_prepare_finish(
            self.repo, row["id"], pid=301, proc_dir=self.proc,
            result="OK gate:abc1234567890def")
        self.assertTrue(ok, err)
        self.assertIsNone(err)

        # The launcher dies here — after the stamp, before the removal.
        shutil.rmtree(os.path.join(self.proc, "301"))

        found, err = seats.gate_queue_orphans(self.repo, proc_dir=self.proc)
        self.assertIsNone(err, err)
        self.assertEqual(len(found), 1, "the dead finisher is the orphan")
        self.assertEqual(found[0]["state"], "finishing")
        self.assertEqual(found[0].get("announced"), "OK gate:abc1234567890def",
                         "the row reached finalization and must carry the "
                         "result its FINISH line announced: %r" % (found[0],))
        recovered, err = seats.gate_queue_recover_orphans(
            self.repo, proc_dir=self.proc)
        self.assertIsNone(err, err)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["diagnostic"], {
            "kind": "finish", "status": "OK", "stage": "finishing",
            "reason": "receipt-minted"})
        self.assertEqual(recovered[0]["announced"],
                         "OK gate:abc1234567890def")
        self.assertEqual(seats.gate_queue_snapshot(
            self.repo, proc_dir=self.proc), ([], None))

    def test_an_unstamped_finishing_orphan_is_unchanged_not_worse(self):
        """THE FAILURE-DIRECTION CONTROL, and it is the half that makes the
        stamp safe to add: a launcher that dies BEFORE the stamp must leave
        exactly the row it leaves today. A cure whose absence is indetectable
        is decoration; a cure whose absence makes things WORSE is a hazard.
        """
        row = self.enqueue("beta", 302, 3002)
        state, _r, err = seats.gate_queue_try_start(
            self.repo, row["id"], pid=302, proc_dir=self.proc)
        self.assertEqual((state, err), ("START", None))
        # No prepare_finish at all — death before finalization was reached.
        shutil.rmtree(os.path.join(self.proc, "302"))

        found, err = seats.gate_queue_orphans(self.repo, proc_dir=self.proc)
        self.assertIsNone(err, err)
        self.assertEqual(len(found), 1)
        self.assertNotEqual(found[0]["state"], "finishing",
                            "a row that never reached finalization must not "
                            "read as one that did")
        self.assertIsNone(found[0].get("announced"),
                          "an unannounced row must not claim a result: %r"
                          % (found[0],))

    def test_positions_start_in_fifo_order_and_a_late_poller_cannot_barge(self):
        a = self.enqueue("slow", 101, 1001)
        b = self.enqueue("middle", 102, 1002)
        c = self.enqueue("fast", 103, 1003)

        state, _row, err = seats.gate_queue_try_start(
            self.repo, c["id"], pid=103, proc_dir=self.proc)
        self.assertEqual((state, err), ("WAIT", None))
        state, _row, err = seats.gate_queue_try_start(
            self.repo, a["id"], pid=101, proc_dir=self.proc)
        self.assertEqual((state, err), ("START", None))
        state, _row, err = seats.gate_queue_try_start(
            self.repo, b["id"], pid=102, proc_dir=self.proc)
        self.assertEqual((state, err), ("WAIT", None))

        ok, nxt, orphaned, err = seats.gate_queue_finish(
            self.repo, a["id"], pid=101, proc_dir=self.proc)
        self.assertTrue(ok, err)
        self.assertEqual(orphaned, [])
        self.assertEqual(nxt["id"], b["id"])
        state, _row, err = seats.gate_queue_try_start(
            self.repo, c["id"], pid=103, proc_dir=self.proc)
        self.assertEqual((state, err), ("WAIT", None))
        state, _row, err = seats.gate_queue_try_start(
            self.repo, b["id"], pid=102, proc_dir=self.proc)
        self.assertEqual((state, err), ("START", None))

    def test_dead_or_recycled_head_is_swept_before_grant(self):
        dead = self.enqueue("dead", 111, 2001)
        live = self.enqueue("live", 112, 2002)
        self.dead(111)
        state, row, err = seats.gate_queue_try_start(
            self.repo, live["id"], pid=112, proc_dir=self.proc)
        self.assertEqual((state, err), ("START", None))
        self.assertEqual([r["id"] for r in row["orphaned"]], [dead["id"]])
        ids = [r["id"] for r in seats.gate_queue_snapshot(
            self.repo, proc_dir=self.proc)[0]]
        self.assertNotIn(dead["id"], ids)

        seats.gate_queue_finish(self.repo, live["id"], pid=112,
                                proc_dir=self.proc)
        recycled = self.enqueue("recycled", 121, 3001)
        follower = self.enqueue("follower", 122, 3002)
        self.alive(121, 9999)  # same pid, another generation
        state, _row, err = seats.gate_queue_try_start(
            self.repo, follower["id"], pid=122, proc_dir=self.proc)
        self.assertEqual((state, err), ("START", None))
        ids = [r["id"] for r in seats.gate_queue_snapshot(
            self.repo, proc_dir=self.proc)[0]]
        self.assertNotIn(recycled["id"], ids)

    def test_live_child_keeps_the_slot_when_its_launcher_dies(self):
        owner = self.enqueue("owner", 131, 4001)
        follower = self.enqueue("follower", 132, 4002)
        state, _row, err = seats.gate_queue_try_start(
            self.repo, owner["id"], pid=131, proc_dir=self.proc)
        self.assertEqual((state, err), ("START", None))
        self.alive(231, 5001)
        ok, err = seats.gate_queue_bind_child(
            self.repo, owner["id"], 231, launcher_pid=131,
            proc_dir=self.proc)
        self.assertTrue(ok, err)
        self.dead(131)

        state, _row, err = seats.gate_queue_try_start(
            self.repo, follower["id"], pid=132, proc_dir=self.proc)
        self.assertEqual((state, err), ("WAIT", None))
        self.dead(231)
        state, _row, err = seats.gate_queue_try_start(
            self.repo, follower["id"], pid=132, proc_dir=self.proc)
        self.assertEqual((state, err), ("START", None))

    def test_real_whole_suite_processes_serialize_and_post_transitions(self):
        log = os.path.join(self.tmp, "starts")
        release = os.path.join(self.tmp, "release-")
        self.fixture_suite("""import os
import time
import unittest

class FifoFixture(unittest.TestCase):
    def test_hold(self):
        who = os.environ["HELM_CHAT_NAME"]
        with open(%r, "a") as f:
            f.write(who + "\\n")
        end = time.monotonic() + 10
        while not os.path.exists(%r + who) and time.monotonic() < end:
            time.sleep(0.02)
        self.assertTrue(os.path.exists(%r + who))
""" % (log, release, release))

        a = self.worker("alpha")
        self.wait_for(lambda: os.path.exists(log)
                      and self.lines(log) == ["alpha"])
        b = self.worker("beta", timeout=0.5)
        self.wait_for(lambda: len(seats.gate_queue_snapshot(self.repo)[0]) == 2)
        positions = [row["id"] for row in
                     seats.gate_queue_snapshot(self.repo)[0]]
        cgroup_root = gatechild._cgroup_root()
        self.assertIsNotNone(cgroup_root)
        cgroups = [os.path.join(cgroup_root, gatechild._CGROUP_PREFIX + position)
                   for position in positions]
        self.assertEqual(self.lines(log), ["alpha"])
        # Queue wait is not child runtime: beta waits longer than its own 0.5s
        # timeout here, then still gets the full budget once admitted.
        time.sleep(0.7)
        open(release + "alpha", "w").close()
        self.wait_for(lambda: self.lines(log) == ["alpha", "beta"])
        open(release + "beta", "w").close()
        out_a, err_a = a.communicate(timeout=15)
        out_b, err_b = b.communicate(timeout=15)
        self.assertEqual((a.returncode, b.returncode), (0, 0),
                         (out_a, err_a, out_b, err_b))
        self.assertEqual(seats.gate_queue_snapshot(self.repo)[0], [])  # noqa: VACUOUS_ASSERTION — the same queue held two measured rows at line 204
        self.wait_for(lambda: all(not os.path.exists(path) for path in cgroups))

        rows, total = chat.read("queue-room")
        self.assertEqual(total, len(rows))
        self.assertTrue(all(row.get("ambient") == 1 for row in rows))
        text = [row["text"] for row in rows]
        alpha_start = next(i for i, line in enumerate(text)
                           if "GATE START" in line and "@alpha" in line)
        alpha_finish = next(i for i, line in enumerate(text)
                            if line.startswith("GATE FINISH")
                            and " @alpha " in line.split(" — NEXT")[0])
        beta_start = next(i for i, line in enumerate(text)
                          if "GATE START" in line and "@beta" in line)
        beta_finish = next(i for i, line in enumerate(text)
                           if line.startswith("GATE FINISH")
                           and " @beta " in line.split(" — NEXT")[0])
        self.assertLess(alpha_start, alpha_finish)
        self.assertLess(alpha_finish, beta_start)
        self.assertLess(beta_start, beta_finish)
        self.assertIn("NEXT @beta", text[alpha_finish])

    def test_suite_keeps_the_launchers_pid_namespace(self):  # noqa: VACUOUS_ASSERTION — the gated fixture must create a parseable marker whose exact inode equals the caller's live PID namespace
        observed = os.path.join(self.tmp, "suite-pid-namespace")
        expected = os.stat("/proc/self/ns/pid").st_ino
        self.fixture_suite("""import os
import unittest

class PidNamespaceFixture(unittest.TestCase):
    def test_records_pid_namespace(self):
        with open(%r, "w") as f:
            f.write(str(os.stat("/proc/self/ns/pid").st_ino))
""" % observed)
        require_supervisor()
        row, err = gate.run(repo=self.repo, timeout=5)
        self.assertIsNone(err, err)
        _assert_gate_status(self, row, "OK")
        with open(observed) as f:
            self.assertEqual(int(f.read()), expected)

    def test_legacy_gatelock_blocks_fifo_start_until_the_old_holder_releases(self):  # noqa: VACUOUS_ASSERTION — worker start after release is the positive witness for the earlier absence
        log = os.path.join(self.tmp, "legacy-start")
        release = os.path.join(self.tmp, "legacy-release")
        self.fixture_suite("""import os
import time
import unittest

class LegacyFixture(unittest.TestCase):
    def test_waits(self):
        open(%r, "w").close()
        end = time.monotonic() + 10
        while not os.path.exists(%r) and time.monotonic() < end:
            time.sleep(0.02)
        self.assertTrue(os.path.exists(%r))
""" % (log, release, release))
        resource, err = gate._legacy_gate_resource(self.repo)
        self.assertIsNone(err)
        self.assertEqual(resource, "gatelock:repo")
        ok, _message, lease = seats.claim(
            resource, "legacy", ttl=60, session="legacy-session")
        self.assertTrue(ok)

        worker = self.worker("fifo")
        try:
            self.wait_for(
                lambda: len(seats.gate_queue_snapshot(self.repo)[0]) == 1)
            time.sleep(0.3)
            self.assertFalse(os.path.exists(log))
            ok, message = seats.release(
                resource, "legacy", lease=lease, session="legacy-session")
            self.assertTrue(ok, message)
            lease = None
            self.wait_for(lambda: os.path.exists(log))
            open(release, "w").close()
            out, errout = worker.communicate(timeout=15)
            self.assertEqual(worker.returncode, 0, (out, errout))
            self.assertNotIn(resource, {row["resource"]
                                        for row in seats.claims_list()})
        finally:
            if lease:
                seats.release(resource, "legacy", lease=lease,
                              session="legacy-session")
            open(release, "a").close()
            if worker.poll() is None:
                worker.communicate(timeout=15)

    def test_same_seat_and_session_still_contend_with_an_existing_legacy_run(self):  # noqa: VACUOUS_ASSERTION — worker start after release proves the same identity was genuinely waiting
        log = os.path.join(self.tmp, "same-holder-start")
        release = os.path.join(self.tmp, "same-holder-release")
        self.fixture_suite("""import os
import time
import unittest

class SameHolderFixture(unittest.TestCase):
    def test_waits(self):
        open(%r, "w").close()
        end = time.monotonic() + 10
        while not os.path.exists(%r) and time.monotonic() < end:
            time.sleep(0.02)
        self.assertTrue(os.path.exists(%r))
""" % (log, release, release))
        resource, err = gate._legacy_gate_resource(self.repo)
        self.assertIsNone(err)
        ok, _message, lease = seats.claim(
            resource, "fifo", ttl=60, session="session-fifo")
        self.assertTrue(ok)
        worker = self.worker("fifo")
        try:
            self.wait_for(
                lambda: len(seats.gate_queue_snapshot(self.repo)[0]) == 1)
            time.sleep(0.3)
            self.assertFalse(os.path.exists(log))
            ok, message = seats.release(
                resource, "fifo", lease=lease, session="session-fifo")
            self.assertTrue(ok, message)
            lease = None
            self.wait_for(lambda: os.path.exists(log))
            open(release, "w").close()
            out, errout = worker.communicate(timeout=15)
            self.assertEqual(worker.returncode, 0, (out, errout))
        finally:
            if lease:
                seats.release(resource, "fifo", lease=lease,
                              session="session-fifo")
            open(release, "a").close()
            if worker.poll() is None:
                worker.communicate(timeout=15)

    def test_legacy_hold_renews_through_slow_receipt_finalization(self):  # noqa: VACUOUS_ASSERTION — witnessed=True proves the live claim during delayed finalization before asserting release
        self.fixture_suite("""import unittest

class LegacyRenewFixture(unittest.TestCase):
    def test_ok(self):
        pass
""")
        os.environ["HELM_CHAT_NAME"] = "legacy-renew"
        resource, err = gate._legacy_gate_resource(self.repo)
        self.assertIsNone(err)
        real = gate._mint_result
        witnessed = []

        def slow(*args, **kwargs):
            time.sleep(1.2)
            witnessed.append(resource in {row["resource"]
                                           for row in seats.claims_list()})
            return real(*args, **kwargs)

        require_supervisor()
        with mock.patch.object(gate, "_GATE_LEGACY_TTL_S", 1), \
                mock.patch.object(gate, "_GATE_LEGACY_RENEW_S", 0.05), \
                mock.patch.object(gate, "_mint_result", side_effect=slow):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        _assert_gate_status(self, row, "OK")
        self.assertEqual(witnessed, [True])
        self.assertNotIn(resource, {claim["resource"]
                                    for claim in seats.claims_list()})

    def test_claim_loss_after_child_exit_cannot_mint_a_receipt(self):  # noqa: VACUOUS_ASSERTION — competitor acquisition is the positive witness before the receipt ledger must remain empty
        self.fixture_suite("""import unittest

class FinalizeLossFixture(unittest.TestCase):
    def test_ok(self):
        pass
""")
        os.environ["HELM_CHAT_NAME"] = "finalize-loss"
        resource, err = gate._legacy_gate_resource(self.repo)
        self.assertIsNone(err)
        real = gate._mint_result
        competitor = []

        def replace_before_mint(*args, **kwargs):
            legacy = args[-1]
            ok, message = seats.release(
                resource, legacy.holder, lease=legacy.lease,
                session=legacy.session, strict=True)
            self.assertTrue(ok, message)
            ok, message, lease = seats.claim(
                resource, "competitor", ttl=60,
                session="competitor-session", strict=True)
            self.assertTrue(ok, message)
            competitor.append(lease)
            return real(*args, **kwargs)

        require_supervisor()
        try:
            with mock.patch.object(gate, "_mint_result",
                                   side_effect=replace_before_mint):
                row, err = gate.run(repo=self.repo)
            self.assertIsNone(row)
            self.assertIn("unminted", err)
            rows, unavailable, skipped = gate.receipts()
            self.assertEqual((rows, unavailable, skipped), ([], None, 0))
        finally:
            if competitor:
                seats.release(resource, "competitor", lease=competitor[0],
                              session="competitor-session", strict=True)

    def test_legacy_renewal_failure_kills_the_suite_and_releases_both_locks(self):  # noqa: VACUOUS_ASSERTION — fixture start proves both locks existed before the forced renewal loss
        started = os.path.join(self.tmp, "legacy-renewal-start")
        self.fixture_suite("""import time
import unittest

class LegacyLossFixture(unittest.TestCase):
    def test_waits(self):
        open(%r, "w").close()
        time.sleep(10)
""" % started)
        os.environ["HELM_CHAT_NAME"] = "legacy-loss"
        resource, err = gate._legacy_gate_resource(self.repo)
        self.assertIsNone(err)
        real = seats.refresh_claim
        def lose_after_start(*args, **kwargs):
            if os.path.exists(started):
                return False, "legacy hold was replaced"
            return real(*args, **kwargs)

        require_supervisor()
        with mock.patch.object(seats, "refresh_claim",
                               side_effect=lose_after_start), \
                mock.patch.object(gate, "_GATE_LEGACY_RENEW_S", 0.03), \
                mock.patch.object(gate, "_GATE_RENEW_S", 0.05):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(row)
        self.assertIn("legacy gate-lock renewal failed", err)
        self.assertTrue(os.path.exists(started))
        self.assertEqual(seats.gate_queue_snapshot(self.repo)[0], [])
        self.assertNotIn(resource, {claim["resource"]
                                    for claim in seats.claims_list()})

    def test_expired_legacy_renewal_never_mints_a_replacement_lease(self):
        resource, err = gate._legacy_gate_resource(self.repo)
        self.assertIsNone(err)
        ok, message, lease = seats.claim(
            resource, "renew-owner", ttl=60, session="renew-session",
            strict=True)
        self.assertTrue(ok, message)
        with mock.patch.object(gate, "_GATE_LEGACY_RENEW_S", 0.03):
            legacy = gate._LegacyGateLease(
                resource, "renew-owner", "renew-session", lease)
            ok, message = seats.release(
                resource, "renew-owner", lease=lease,
                session="renew-session", strict=True)
            self.assertTrue(ok, message)
            self.wait_for(lambda: legacy.error() is not None)
            self.assertNotIn(resource, {row["resource"]
                                        for row in seats.claims_list()})
            ok, message = legacy.close()
        self.assertFalse(ok)
        self.assertIn("renewal failed", message)

    def test_strict_claim_refuses_an_unavailable_lock(self):  # noqa: VACUOUS_ASSERTION — assertRaises is the positive refusal witness; no state mutation is the contract
        class NoLock:
            f = None
            def __enter__(self):
                return self
            def __exit__(self, *_exc):
                return False

        with mock.patch.object(seats, "_flocked", return_value=NoLock()), \
                mock.patch.object(seats, "CLAIM_LOCK_WAIT_S", 0):
            with self.assertRaisesRegex(OSError, "lock is unavailable"):
                seats.claim("gatelock:repo", "strict", strict=True)

    def _fake_proc(self, ino, pid, state):
        """A /proc naming `pid` as the FLOCK holder of `ino`, in `state`.

        Synthetic rather than a live SIGSTOP on purpose: a fixture that
        stops a real process to observe a lock is the shape that produced
        the flake this diagnostic exists to explain. The parser is what is
        under test, so feed it bytes, not timing.
        """
        proc = tempfile.mkdtemp(prefix="helm-fakeproc-")
        self.addCleanup(shutil.rmtree, proc, True)
        with open(os.path.join(proc, "locks"), "w") as f:
            f.write("1: POSIX  ADVISORY  WRITE 1 00:2f:1 0 EOF\n")
            f.write("2: FLOCK  ADVISORY  WRITE %d 00:2f:%d 0 EOF\n" % (pid, ino))
        if state is not None:
            os.makedirs(os.path.join(proc, str(pid)))
            tail = " ".join([state, "1"] + ["0"] * 17 + ["99999"])
            with open(os.path.join(proc, str(pid), "stat"), "w") as f:
                f.write("%d (fake) %s\n" % (pid, tail))
        return proc

    def test_the_lock_refusal_NAMES_a_stopped_holder(self):
        """A wedge and mere contention time out identically; only the
        holder's state tells them apart, so the message must carry it."""
        path = os.path.join(self.tmp, "probe.lock")
        open(path, "w").close()
        ino = os.stat(path).st_ino

        stopped = seats._lock_unavailable(
            path, proc_dir=self._fake_proc(ino, 424242, "T"))
        self.assertIn("STOPPED pid 424242", stopped)
        self.assertIn("never releases", stopped)
        self.assertIn("lock is unavailable", stopped)

        # POSITIVE CONTROL — same parser, same inode, running holder. Without
        # this arm a _flock_holder that always returned None would pass every
        # assertion above by falling through to the bare sentence.
        running = seats._lock_unavailable(
            path, proc_dir=self._fake_proc(ino, 515151, "R"))
        self.assertIn("pid 515151", running)
        self.assertNotIn("STOPPED", running)

        # FALLBACK — no FLOCK row for this inode at all. A diagnostic that
        # raises here would be worse than a vague one.
        bare = seats._lock_unavailable(
            path, proc_dir=self._fake_proc(ino + 1, 626262, "T"))
        self.assertEqual(bare, "claim lock is unavailable")

        # A VANISHED DIRECTORY IS NOT CONTENTION, AND USED TO SAY IT WAS.
        # No holder has two causes: a lock nobody happens to hold, and a lock
        # that CANNOT BE OPENED because its directory is gone. Both fell
        # through to the bare sentence, byte-identical — measured —
        # so an env leak that repointed HELM_HOME at a deleted tree read as a
        # contending process and stayed invisible through a whole suite. The
        # two need opposite operator moves: wait, versus fix your HELM_HOME.
        vanished = os.path.join(self.tmp, "no-such-dir", "claims.json.lock")
        gone_msg = seats._lock_unavailable(
            vanished, proc_dir=self._fake_proc(ino, 424242, "T"))
        self.assertIn("directory does not exist", gone_msg)
        self.assertIn("NOTHING HOLDS IT", gone_msg)
        self.assertNotEqual(gone_msg, bare)
        # MUST-MISS: an openable path with no holder keeps the ORIGINAL bare
        # sentence. If this arm ever reports the vanished text, the check has
        # started firing on the normal case.
        self.assertEqual(bare, "claim lock is unavailable")
        # ...and the lock FILE's own absence must stay silent: it is created on
        # demand, so only the DIRECTORY is evidence.
        never_created = os.path.join(self.tmp, "not-yet.lock")
        self.assertEqual(
            seats._lock_unavailable(
                never_created, proc_dir=self._fake_proc(ino + 1, 1, "R")),
            "claim lock is unavailable")

    def test_a_leaking_sibling_is_caught_by_the_post_class_witness(self):  # noqa: VACUOUS_ASSERTION — the assertRaises IS the must-hit, and it carries THREE unconditional same-observable controls above it: the witness accepts a clean env, the poisoner is asserted to PASS, and HELM_HOME is asserted to equal the temp value so the two layers demonstrably disagree
        """THE MUST-HIT, AND TWO OF MY OWN ATTEMPTS FAILED IT FIRST.

        The review seeded a synthetic future GateQueueTest that re-introduces the
        per-test env snapshot, ran it followed by my earlier invariant arm, and
        THE ARM PASSED while the env stayed poisoned. An ordinary test body runs
        BEFORE tearDown and cleanups, and the next setUp captures the
        already-poisoned values as self.prior — so "current env equals my own
        self.tmp" only proves setUp planted it, and can never observe a SIBLING
        after that sibling's cleanup.

        The witness is `_assert_env_unpoisoned`, called from tearDownClass,
        which is the one point after EVERY test's cleanup (proven ordering:
        setUpClass, test, tearDown, cleanup, ..., tearDownClass).

        AND THE POISONER NEEDS BOTH LAYERS, which my first draft got wrong: a
        test that merely snapshots and restores leaks NOTHING, because it puts
        back exactly what it took. The defect needs a tearDown restoring the
        CLASS value and a cleanup re-applying the SETUP value, with cleanup
        last. Only then does the pair disagree and the temp value survive."""
        import tempfile as _tf

        keys = ("HELM_HOME", "HELM_CHAT_DIR")
        entry = {k: os.environ.get(k) for k in keys}
        temp = os.path.join(_tf.gettempdir(), "poisoned-does-not-exist", "helm")

        class Poisoner(unittest.TestCase):
            def setUp(self):
                os.environ["HELM_HOME"] = temp

            def runTest(self):
                prior = {k: os.environ.get(k) for k in keys}

                def restore():
                    for key, value in prior.items():
                        if value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = value
                self.addCleanup(restore)

            def tearDown(self):
                for key, value in entry.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

        # CONTROL: the witness ACCEPTS a clean env. Without it, a witness that
        # refused everything would satisfy the must-hit below.
        GateQueueTest._assert_env_unpoisoned(entry)

        result = unittest.TextTestRunner(
            verbosity=0, stream=io.StringIO()).run(Poisoner())
        self.assertTrue(result.wasSuccessful(),
                        "control: the poisoner must PASS — a leak that "
                        "announced itself as a failure would need no witness")
        self.assertEqual(os.environ.get("HELM_HOME"), temp,
                         "control: the two layers must actually disagree, or "
                         "there is nothing for the witness to catch")

        # MUST-HIT: cleanup has run and the temp value survived.
        with self.assertRaises(AssertionError):
            GateQueueTest._assert_env_unpoisoned(entry)

        for key, value in entry.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        GateQueueTest._assert_env_unpoisoned(entry)

    def test_strict_claim_read_distinguishes_missing_from_unresolvable_path(self):  # noqa: VACUOUS_ASSERTION — the assertRaisesRegex is the contract, and its unconditional same-call positive control is the first line of the body: `_claims_read(strict=True) == {}` proves a RESOLVED missing file reads as proven-empty, so a function that raised for everything fails there first
        """Only open(resolved_path) can prove the claims file is absent.

        With a relative HELM_HOME and the process cwd removed, claims_path()
        itself raises FileNotFoundError. Resolving it inside the open try made
        strict=True report that unnameable state as a proven-empty ledger.
        """
        self.assertEqual(seats._claims_read(strict=True), {},
                         "control: a resolved missing file is proven empty")
        gone = tempfile.mkdtemp(prefix="helm-test-claims-nopath-")
        prior_cwd = os.getcwd()

        # THIS CLEANUP RESTORES THE CWD ONLY, AND DELIBERATELY NOT THE ENV.
        # It used to re-apply a HELM_HOME/HELM_CHAT_DIR snapshot taken HERE —
        # i.e. setUp's temp values — and `addCleanup` runs AFTER `tearDown`
        # (proven: test, tearDown, cleanup). So tearDown correctly restored the
        # suite-wide values and then this ran and put the temp ones back,
        # leaving HELM_HOME and HELM_CHAT_DIR pointing into a tree tearDown had
        # just deleted. Every later module in the process then resolved
        # claims_path() into a directory that no longer existed, and five
        # test_dispatches tests died on "claim lock is unavailable" — which,
        # until the companion fix in seats_claims._lock_unavailable, was the
        # same sentence real contention produces.
        #
        # tearDown owns both keys for the whole class. A per-test snapshot of
        # them can only ever be redundant or wrong, and here it was wrong.
        def restore():
            try:
                os.chdir(prior_cwd)
            except OSError:
                os.chdir(tempfile.gettempdir())
        self.addCleanup(restore)

        os.chdir(gone)
        os.rmdir(gone)
        os.environ["HELM_HOME"] = "relative-home"
        os.environ.pop("HELM_CHAT_DIR", None)
        with self.assertRaisesRegex(
                OSError, "claim state is unreadable: FileNotFoundError"):
            seats._claims_read(strict=True)

    def test_strict_claim_lifecycle_refuses_malformed_state(self):  # noqa: VACUOUS_ASSERTION — each strict operation reads concrete malformed bytes, refuses, and preserves them exactly
        os.makedirs(os.path.dirname(seats.claims_path()), exist_ok=True)
        cases = (
            ("truncated", "{truncated"),
            ("malformed-row", '{"_fence": 7, "gatelock:malformed": '
             '{"holder": "old", "lease": "oldlease"}}'),
            ("duplicate-row", ('{"_fence": 2, "gatelock:malformed": '
             '{"holder": "first", "session": "one", "lease": "lease1", '
             '"fence": 1, "exp_mono": %r, "exp_wall": 1, "ts": "one"}, '
             '"gatelock:malformed": {"holder": "second", '
             '"session": "two", "lease": "lease2", "fence": 2, '
             '"exp_mono": %r, "exp_wall": 2, "ts": "two"}}')
             % (seats._now_mono() + 60, seats._now_mono() + 60)),
        )
        operations = (
            ("claim", lambda: seats.claim(
                "gatelock:malformed", "strict", strict=True)),
            ("refresh", lambda: seats.refresh_claim(
                "gatelock:malformed", "strict", lease="lease",
                session="session", strict=True)),
            ("release", lambda: seats.release(
                "gatelock:malformed", "strict", lease="lease",
                session="session", strict=True)),
        )
        for case, malformed in cases:
            for label, operation in operations:
                with self.subTest(case=case, operation=label):
                    with open(seats.claims_path(), "w") as f:
                        f.write(malformed)
                    with self.assertRaisesRegex(OSError, "state is unreadable"):
                        operation()
                    with open(seats.claims_path()) as f:
                        self.assertEqual(f.read(), malformed)
            with open(seats.claims_path(), "w") as f:
                f.write(malformed)
            result, err = seats.claim_guard(
                "gatelock:malformed", "strict", "lease", "session",
                lambda: "committed")
            self.assertIsNone(result)
            self.assertIn("state is unreadable", err)
            with open(seats.claims_path()) as f:
                self.assertEqual(f.read(), malformed)

    def test_strict_claim_lifecycle_waits_through_brief_lock_contention(self):
        resource = "gatelock:contention"
        os.makedirs(os.path.dirname(seats.claims_path()), exist_ok=True)
        def contend(action):
            ready = os.path.join(self.tmp, "claims-lock-ready")
            try:
                os.unlink(ready)
            except FileNotFoundError:
                pass
            code = """import fcntl
import time
f = open(%r, "a")
fcntl.flock(f.fileno(), fcntl.LOCK_EX)
open(%r, "w").close()
time.sleep(0.12)
""" % (seats.claims_path() + ".lock", ready)
            holder = subprocess.Popen((sys.executable, "-c", code))
            self.wait_for(lambda: os.path.exists(ready))
            started = time.monotonic()
            try:
                result = action()
                elapsed = time.monotonic() - started
            finally:
                holder.communicate(timeout=1)
            self.assertGreater(elapsed, 0.08)
            self.assertLess(elapsed, 1)
            return result

        ok, message, lease = contend(lambda: seats.claim(
            resource, "strict", ttl=60, session="strict-session",
            strict=True))
        self.assertTrue(ok, message)
        operations = (
            ("refresh", lambda: seats.refresh_claim(
                resource, "strict", lease=lease, session="strict-session",
                ttl=60, strict=True)),
            ("guard", lambda: seats.claim_guard(
                resource, "strict", lease, "strict-session",
                lambda: "committed")),
            ("release", lambda: seats.release(
                resource, "strict", lease=lease, session="strict-session",
                strict=True)),
        )
        for label, operation in operations:
            with self.subTest(operation=label):
                result = contend(operation)
                if label == "guard":
                    self.assertEqual(result, ("committed", None))
                else:
                    self.assertTrue(result[0], result[1])

    def test_thread_start_failure_releases_the_legacy_claim(self):  # noqa: VACUOUS_ASSERTION — the injected constructor failure proves acquisition occurred before rollback empties the claim
        resource, err = gate._legacy_gate_resource(self.repo)
        self.assertIsNone(err)
        with mock.patch.object(threading.Thread, "start",
                               side_effect=RuntimeError("thread unavailable")):
            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                gate._acquire_legacy_gate(
                    self.repo, "thread-start", "thread-session")
        self.assertNotIn(resource, {row["resource"]
                                    for row in seats.claims_list()})

    def test_launcher_death_leaves_only_the_short_legacy_ttl(self):  # noqa: VACUOUS_ASSERTION — the claim is asserted present after process death before expiry removes it
        resource, err = gate._legacy_gate_resource(self.repo)
        self.assertIsNone(err)
        code = """import os
from helm import gate, seats
resource, err = gate._legacy_gate_resource(%r)
assert err is None
ok, message, lease = seats.claim(
    resource, 'dead-launcher', ttl=1, session='dead-session', strict=True)
assert ok, message
gate._LegacyGateLease(resource, 'dead-launcher', 'dead-session', lease)
os._exit(0)
""" % self.repo
        run = subprocess.run((sys.executable, "-c", code),
                             env=dict(os.environ), timeout=10)
        self.assertEqual(run.returncode, 0)
        self.assertIn(resource, {row["resource"]
                                 for row in seats.claims_list()})
        # THE TTL PASSES ON THE CLOCK THE SWEEP READS, not by sleeping it out.
        # exp_mono is the child's monotonic + 1s, and nothing can move it after
        # the launcher dies: its renewer is a daemon thread that dies with it
        # and would not renew before _GATE_LEGACY_RENEW_S (5s) anyway. So the
        # sweep's clock 1.1s on is the state a real 1.1s wait produced.
        later = seats_common._now_mono() + 1.1
        with mock.patch.object(seats_common, "_now_mono",
                               return_value=later), \
                mock.patch.object(seats_claims, "_now_mono",
                                  return_value=later):
            self.assertNotIn(resource, {row["resource"]
                                        for row in seats.claims_list()})

    def test_linked_worktrees_share_the_legacy_gate_resource(self):  # noqa: VACUOUS_ASSERTION — both concrete Git worktrees resolve and are asserted equal to the named legacy key
        linked = os.path.join(self.tmp, "legacy-linked")
        subprocess.run(("git", "worktree", "add", "-q", "-b", "legacy-linked",
                        linked), cwd=self.repo, check=True)
        primary, primary_err = gate._legacy_gate_resource(self.repo)
        secondary, secondary_err = gate._legacy_gate_resource(linked)
        self.assertEqual((primary_err, secondary_err), (None, None))
        self.assertEqual((primary, secondary), ("gatelock:repo",) * 2)

    def test_successful_guard_keeps_a_genuine_test_red_failed(self):
        self.fixture_suite("""import unittest

class FailingFixture(unittest.TestCase):
    def test_fails(self):
        self.fail("real assertion")
""")
        os.environ["HELM_CHAT_NAME"] = "genuine-red"
        require_supervisor()
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        _assert_gate_status(self, row, "FAILED")
        self.assertEqual(row["rc"], 1)
        self.assertEqual(row["ran"], 1)
        self.assertNotIn("gate guard:", row["detail"])

    def test_timeout_releases_the_slot_and_records_unknown(self):
        self.fixture_suite("""import time
import unittest

class TimeoutFixture(unittest.TestCase):
    def test_hangs(self):
        time.sleep(2)
""")
        os.environ["HELM_CHAT_NAME"] = "timeout-seat"
        require_supervisor()
        row, err = gate.run(repo=self.repo, timeout=0.05)
        self.assertIsNone(err, err)
        _assert_gate_status(self, row, "UNKNOWN")
        self.assertIsNone(row["rc"])
        self.assertEqual(seats.gate_queue_snapshot(self.repo)[0], [])  # noqa: VACUOUS_ASSERTION — UNKNOWN receipt and FINISH line prove the slot existed
        self.assertEqual(seats.gate_queue_terminals(self.repo), ([], None),
                         "a successfully published UNKNOWN finish stayed pending")
        text = [r["text"] for r in chat.read("queue-room")[0]]
        self.assertTrue(any(line.startswith("GATE FINISH")
                            and "UNKNOWN" in line for line in text))

    def test_runner_error_is_named_in_unknown_finish(self):
        # A SEAT HOLDS THE GATE AS ITSELF; MACHINERY HOLDS IT AS MACHINERY
        # (gate.py `_acquire_gate`). This arm asserts the SEAT spelling in the
        # finish post, so the fixture has to be a seat.
        _tmp_declare(self, "runner-error")
        with mock.patch.object(
                gate, "_queued_process",
                return_value=("", "", None, "known runner error")):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(row)
        self.assertEqual(err, "known runner error")
        finishes = [
            r["text"] for r in chat.read("queue-room")[0]
            if r["text"].startswith("GATE FINISH")
        ]
        self.assertEqual(
            finishes,
            ["GATE FINISH #1 @runner-error UNKNOWN — known runner error"])

    def test_live_child_revalidates_and_exit_stops_validation(self):
        self.fixture_suite("""import time
import unittest

class RenewFixture(unittest.TestCase):
    def test_lives_across_renewals(self):
        time.sleep(0.2)
""")
        os.environ["HELM_CHAT_NAME"] = "renew-seat"
        calls = []
        real = seats.gate_queue_renew

        def counted(*args, **kwargs):
            calls.append(time.monotonic())
            return real(*args, **kwargs)

        require_supervisor()
        with mock.patch.object(gate, "_GATE_RENEW_S", 0.03), \
                mock.patch.object(seats, "gate_queue_renew", side_effect=counted):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["ran"], 1, row)
        _assert_gate_status(self, row, "OK")
        self.assertGreaterEqual(len(calls), 2)
        count = len(calls)
        time.sleep(0.08)
        self.assertEqual(len(calls), count)

    def test_gatechild_wrapper_needs_no_inherited_pythonpath(self):
        """The gate's child wrapper finds helm with NO inherited PYTHONPATH.

        THE LAUNCHER IS A CHILD, SO IT IS PINNED AS ONE (task/1740 P3). This
        spawned `bin/helm gate run` bare, and a child never imports `tests`, so
        its admission read THIS node's /proc and wrote the node's live ledger:
        on a full node it exited 3 and this arm went red by the node's load.
        The launcher now runs bin/helm's own code after `pin_admission_code`,
        and the environment still carries no PYTHONPATH, which is the subject.
        Its intent row landing in the FIXTURE ledger is the proof it was
        admitted on the fixture box."""
        self.fixture_suite("""import unittest

class WrapperFixture(unittest.TestCase):
    def test_ok(self):
        pass
""")
        root = os.path.dirname(os.path.dirname(gate.__file__))
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONSAFEPATH", None)
        pybin = os.path.join(self.tmp, "python-bin")
        os.makedirs(pybin)
        os.symlink(sys.executable, os.path.join(pybin, "python3"))
        env["PATH"] = pybin + os.pathsep + env.get("PATH", "")
        env["HELM_CHAT_NAME"] = "no-pythonpath"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # sys.path, not PYTHONPATH: the launcher must import helm before
        # bin/helm's own insert runs, and its cwd is a fixture repo that ships a
        # stub `helm` package. Nothing here reaches the child's environment.
        helm_bin = os.path.join(root, "bin", "helm")
        code = ("import runpy, sys\nsys.path.insert(0, %r)\n" % root
                + pin_admission_code(self.admission_proc, self.admissions)
                + "sys.argv = [%r, 'gate', 'run', '--repo', %r]\n"
                  "runpy.run_path(%r, run_name='__main__')\n"
                % (helm_bin, self.repo, helm_bin))
        self.assertFalse(os.path.exists(self.admissions))
        require_supervisor()
        run = subprocess.run((os.path.join(pybin, "python3"), "-c", code),
                             cwd=self.repo, env=env, text=True,
                             capture_output=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("whole-suite", run.stdout)
        self.assertTrue(os.path.exists(self.admissions),
                        "the launcher was not admitted on the fixture box: "
                        "its admission went to this node")

    def test_custom_diagnostic_command_never_enters_the_whole_suite_fifo(self):
        require_supervisor()
        with mock.patch.object(seats, "gate_queue_enqueue",
                               side_effect=AssertionError("queued custom command")):
            row, err = gate.run(repo=self.repo, argv=[
                sys.executable, "-c",
                "import sys; print('Ran 1 test in 0.001s\\n\\nOK', file=sys.stderr)"])
        self.assertIsNone(err, err)
        self.assertEqual(row["ran"], 1)
        self.assertFalse(row["suite"])

    def test_launcher_holds_slot_through_receipt_finalization(self):
        owner = self.enqueue("owner", 151, 7001)
        follower = self.enqueue("follower", 152, 7002)
        state, _row, err = seats.gate_queue_try_start(
            self.repo, owner["id"], pid=151, proc_dir=self.proc)
        self.assertEqual((state, err), ("START", None))
        self.alive(251, 8001)
        ok, err = seats.gate_queue_bind_child(
            self.repo, owner["id"], 251, launcher_pid=151,
            proc_dir=self.proc)
        self.assertTrue(ok, err)
        self.dead(251)  # suite exited; launcher is still minting its receipt
        state, _row, err = seats.gate_queue_try_start(
            self.repo, follower["id"], pid=152, proc_dir=self.proc)
        self.assertEqual((state, err), ("WAIT", None))
        ok, nxt, _orphaned, err = seats.gate_queue_finish(
            self.repo, owner["id"], pid=151, proc_dir=self.proc)
        self.assertTrue(ok, err)
        self.assertEqual(nxt["id"], follower["id"])

    def test_interrupted_waiter_removes_its_own_position(self):  # noqa: VACUOUS_ASSERTION — side effect records the one live queued row before interruption
        os.environ["HELM_CHAT_NAME"] = "interrupted"
        seen = []

        def interrupt(*_args, **_kwargs):
            seen.append(len(seats.gate_queue_snapshot(self.repo)[0]))
            raise KeyboardInterrupt

        with mock.patch.object(seats, "gate_queue_try_start",
                               side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                gate._acquire_gate(self.repo)
        self.assertEqual(seen, [1])
        self.assertEqual(seats.gate_queue_snapshot(self.repo)[0], [])
        self.assertEqual(seats.gate_queue_terminals(self.repo), ([], None),
                         "the cancellation line posted but was never acknowledged")
        text = [row["text"] for row in chat.read("queue-room")[0]]
        terminal = next(line for line in text
                        if line.startswith("GATE TERMINAL"))
        self.assertIn("diagnostic=UNKNOWN", terminal)
        self.assertIn("stage=queue-acquire", terminal)
        self.assertIn("reason=launcher-exited-before-gate-start", terminal)

    def test_interrupt_during_queued_post_removes_the_position(self):  # noqa: VACUOUS_ASSERTION — callback proves the position exists before the injected interrupt
        os.environ["HELM_CHAT_NAME"] = "queued-post"
        real = gate._gate_post

        def interrupt(repo, text, holder):
            if text.startswith("GATE QUEUED"):
                self.assertEqual(len(seats.gate_queue_snapshot(self.repo)[0]), 1)
                raise KeyboardInterrupt
            return real(repo, text, holder)

        with mock.patch.object(gate, "_gate_post", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                gate._acquire_gate(self.repo)
        self.assertEqual(seats.gate_queue_snapshot(self.repo)[0], [])

    def test_receipt_parser_exception_still_releases_the_active_slot(self):  # noqa: VACUOUS_ASSERTION — parser records the active running row before raising
        self.fixture_suite("""import unittest

class PassFixture(unittest.TestCase):
    def test_ok(self):
        pass
""")
        os.environ["HELM_CHAT_NAME"] = "parser-crash"
        seen = []

        def broken_parser(_out):
            seen.append(seats.gate_queue_snapshot(self.repo)[0][0]["state"])
            raise RuntimeError("parser broke")

        require_supervisor()
        with mock.patch.object(gate, "parse_result", side_effect=broken_parser):
            with self.assertRaisesRegex(RuntimeError, "parser broke"):
                gate.run(repo=self.repo)
        self.assertEqual(seen, ["running"])
        self.assertEqual(seats.gate_queue_snapshot(self.repo)[0], [])

    def test_chat_failure_cannot_strand_or_reorder_the_queue(self):
        self.fixture_suite("""import unittest

class PassFixture(unittest.TestCase):
    def test_ok(self):
        pass
""")
        os.environ["HELM_CHAT_NAME"] = "silent"
        require_supervisor()
        with mock.patch.object(gate, "_gate_post", return_value=False):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        _assert_gate_status(self, row, "OK")
        self.assertEqual(seats.gate_queue_snapshot(self.repo)[0], [])  # noqa: VACUOUS_ASSERTION — the minted OK receipt proves admission completed

    def test_linked_worktrees_share_one_repository_queue(self):
        linked = os.path.join(self.tmp, "linked")
        subprocess.run(("git", "worktree", "add", "-q", "-b", "linked", linked),
                       cwd=self.repo, check=True)
        a = self.enqueue("main", 161, 9001)
        self.alive(162, 9002)
        state, b, err = seats.gate_queue_enqueue(
            linked, "linked", pid=162, proc_dir=self.proc)
        self.assertEqual((state, err), ("QUEUED", None))
        self.assertEqual([r["id"] for r in seats.gate_queue_snapshot(
            self.repo, proc_dir=self.proc)[0]], [a["id"], b["id"]])
        self.assertEqual([r["id"] for r in seats.gate_queue_snapshot(
            linked, proc_dir=self.proc)[0]], [a["id"], b["id"]])

    def test_same_launcher_invocations_get_distinct_positions(self):
        self.alive(171, 10001)
        state, a, err = seats.gate_queue_enqueue(
            self.repo, "same", pid=171, proc_dir=self.proc)
        self.assertEqual((state, err), ("QUEUED", None))
        state, b, err = seats.gate_queue_enqueue(
            self.repo, "same", pid=171, proc_dir=self.proc)
        self.assertEqual((state, err), ("QUEUED", None))
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual([r["id"] for r in seats.gate_queue_snapshot(
            self.repo, proc_dir=self.proc)[0]], [a["id"], b["id"]])
        state, _row, err = seats.gate_queue_try_start(
            self.repo, a["id"], pid=171, proc_dir=self.proc)
        self.assertEqual((state, err), ("START", None))
        state, _row, err = seats.gate_queue_try_start(
            self.repo, b["id"], pid=171, proc_dir=self.proc)
        self.assertEqual((state, err), ("WAIT", None))

    def test_another_launcher_cannot_advance_or_renew_a_position(self):
        owner = self.enqueue("owner", 172, 10101)
        self.alive(173, 10102)
        state, _row, err = seats.gate_queue_try_start(
            self.repo, owner["id"], pid=173, proc_dir=self.proc)
        self.assertEqual(state, "UNKNOWN")
        self.assertIn("not owned", err)

        state, _row, err = seats.gate_queue_try_start(
            self.repo, owner["id"], pid=172, proc_dir=self.proc)
        self.assertEqual((state, err), ("START", None))
        self.alive(272, 10201)
        ok, err = seats.gate_queue_bind_child(
            self.repo, owner["id"], 272, launcher_pid=173,
            proc_dir=self.proc)
        self.assertFalse(ok)
        self.assertIn("awaiting", err)
        self.assertEqual(seats.gate_queue_snapshot(
            self.repo, proc_dir=self.proc)[0][0]["state"], "starting")

        ok, err = seats.gate_queue_bind_child(
            self.repo, owner["id"], 272, launcher_pid=172,
            proc_dir=self.proc)
        self.assertTrue(ok, err)
        ok, err = seats.gate_queue_renew(
            self.repo, owner["id"], 272, launcher_pid=173,
            proc_dir=self.proc)
        self.assertFalse(ok)
        self.assertIn("binding changed", err)
        ok, err = seats.gate_queue_renew(
            self.repo, owner["id"], 272, launcher_pid=172,
            proc_dir=self.proc)
        self.assertTrue(ok, err)

    def test_terminal_supervisor_cannot_bind_or_renew_a_position(self):
        owner = self.enqueue("terminal-child", 273, 10202)
        state, _row, err = seats.gate_queue_try_start(
            self.repo, owner["id"], pid=273, proc_dir=self.proc)
        self.assertEqual((state, err), ("START", None))

        self.alive(274, 10203, state="Z")
        ok, err = seats.gate_queue_bind_child(
            self.repo, owner["id"], 274, launcher_pid=273,
            proc_dir=self.proc)
        self.assertFalse(ok)
        self.assertIn("cannot bind", err)

        self.alive(274, 10203)
        ok, err = seats.gate_queue_bind_child(
            self.repo, owner["id"], 274, launcher_pid=273,
            proc_dir=self.proc)
        self.assertTrue(ok, err)
        for state in ("Z", "X"):
            self.alive(274, 10203, state=state)
            ok, err = seats.gate_queue_renew(
                self.repo, owner["id"], 274, launcher_pid=273,
                proc_dir=self.proc)
            self.assertFalse(ok)
            self.assertIn("no longer alive", err)
        self.alive(274, 10203)
        ok, err = seats.gate_queue_renew(
            self.repo, owner["id"], 274, launcher_pid=273,
            proc_dir=self.proc)
        self.assertTrue(ok, err)

    def test_recycled_launcher_generation_cannot_start_its_old_position(self):
        owner = self.enqueue("owner", 174, 10301)
        self.alive(174, 99999)
        state, row, err = seats.gate_queue_try_start(
            self.repo, owner["id"], pid=174, proc_dir=self.proc)
        self.assertEqual(state, "UNKNOWN")
        self.assertIsNone(row)
        self.assertIn("disappeared", err)

    def test_stopped_waiter_is_cancelled_instead_of_blocking_forever(self):
        stopped = self.enqueue("stopped", 176, 10501)
        follower = self.enqueue("follower", 177, 10502)
        self.alive(176, 10501, state="T")
        state, row, err = seats.gate_queue_try_start(
            self.repo, follower["id"], pid=177, proc_dir=self.proc)
        self.assertEqual((state, err), ("START", None))
        self.assertEqual([r["id"] for r in row["orphaned"]], [stopped["id"]])

    def test_zombie_head_is_an_orphan_not_live_work(self):
        dead = self.enqueue("zombie", 181, 11001)
        follower = self.enqueue("follower", 182, 11002)
        self.alive(181, 11001, state="Z")
        state, row, err = seats.gate_queue_try_start(
            self.repo, follower["id"], pid=182, proc_dir=self.proc)
        self.assertEqual((state, err), ("START", None))
        self.assertEqual([r["id"] for r in row["orphaned"]], [dead["id"]])

    def test_gatechild_process_identity_requires_executable_state(self):
        for pid, state in ((183, "Z"), (184, "X")):
            self.alive(pid, 11003, state=state)
            self.assertFalse(gatechild._same_process(
                pid, 11003, proc_dir=self.proc))
        self.alive(185, 11004, state="S")
        self.assertTrue(gatechild._same_process(
            185, 11004, proc_dir=self.proc))
        self.assertFalse(gatechild._same_process(
            185, 99999, proc_dir=self.proc))

    def test_stale_fence_refuses_instead_of_reordering_waiters(self):
        first = self.enqueue("first", 191, 12001)
        state = pk.read_json(seats.gate_queue_path(self.repo), {})
        state["fence"] = 0
        pk.write_json(seats.gate_queue_path(self.repo), state)
        self.alive(192, 12002)
        status, row, err = seats.gate_queue_enqueue(
            self.repo, "second", pid=192, proc_dir=self.proc)
        self.assertEqual(status, "UNKNOWN")
        self.assertIsNone(row)
        self.assertIn("fence", err)
        self.assertEqual(first["seq"], 1)

    def test_keyboard_interrupt_kills_the_admitted_child_before_release(self):  # noqa: VACUOUS_ASSERTION — exact running child is asserted before the injected interrupt
        self.fixture_suite("""import time
import unittest

class InterruptFixture(unittest.TestCase):
    def test_waits(self):
        time.sleep(10)
""")
        os.environ["HELM_CHAT_NAME"] = "interrupter"
        child = []

        def interrupt(_repo, _position, proc, supervisor, _deadline):
            active = seats.gate_queue_snapshot(self.repo)[0]
            self.assertEqual((active[0]["state"], active[0]["child_pid"]),
                             ("running", supervisor[0]))
            child.extend(((proc.pid, seats._get_pid_starttime(proc.pid)),
                          supervisor))
            raise KeyboardInterrupt

        require_supervisor()
        with mock.patch.object(gate, "_wait_process", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                gate.run(repo=self.repo)
        self.assertEqual(len(child), 2)
        for process in child:
            self.wait_for(lambda p=process: not seats._is_pid_alive(
                p[0], p[1], require_starttime=True), timeout=5)
        self.assertEqual(seats.gate_queue_snapshot(self.repo)[0], [])

    def test_interrupt_during_start_post_kills_child_before_release(self):  # noqa: VACUOUS_ASSERTION — the START callback captures the exact bound child before interruption
        self.fixture_suite("""import time
import unittest

class StartPostFixture(unittest.TestCase):
    def test_waits(self):
        time.sleep(10)
""")
        os.environ["HELM_CHAT_NAME"] = "start-post"
        child = []
        real = gate._gate_post

        def interrupt(repo, text, holder):
            if text.startswith("GATE START"):
                row = seats.gate_queue_snapshot(self.repo)[0][0]
                child.append((row["child_pid"], row["child_starttime"]))
                raise KeyboardInterrupt
            return real(repo, text, holder)

        require_supervisor()
        with mock.patch.object(gate, "_gate_post", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                gate.run(repo=self.repo)
        self.wait_for(lambda: child and not seats._is_pid_alive(
            child[0][0], child[0][1], require_starttime=True), timeout=5)
        self.assertEqual(seats.gate_queue_snapshot(self.repo)[0], [])

    def test_interrupt_during_finish_post_still_releases_position(self):  # noqa: VACUOUS_ASSERTION — callback asserts FINISHING before the finally release
        self.fixture_suite("""import unittest

class FinishPostFixture(unittest.TestCase):
    def test_ok(self):
        pass
""")
        os.environ["HELM_CHAT_NAME"] = "finish-post"
        real = gate._gate_post

        def interrupt(repo, text, holder):
            if text.startswith("GATE FINISH"):
                self.assertEqual(seats.gate_queue_snapshot(
                    self.repo)[0][0]["state"], "finishing")
                raise KeyboardInterrupt
            return real(repo, text, holder)

        with mock.patch.object(gate, "_gate_post", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                gate.run(repo=self.repo)
        self.assertEqual(seats.gate_queue_snapshot(self.repo)[0], [])

    def test_the_orphan_of_a_finishing_row_cannot_contradict_its_finish(self):  # noqa: VACUOUS_ASSERTION — assertRaises(KeyboardInterrupt) is the CAPTURE MECHANISM, not the observable; the captured FINISH text and the live row it captured are both asserted unconditionally after it
        """THE WINDOW IS REAL, SO THE WORDING CARRIES THE BURDEN.

        task/109: "post-before-remove can emit ORPHAN after FINISH".
        The test above pins that window from the other side — at the instant
        the FINISH text is posted the row is STILL THERE, in `finishing`. A
        launcher earlyoom-killed there leaves exactly that row for the next
        seat's partition to publish as a GATE ORPHAN, with its own FINISH
        already on the wall. Removing the row first would close this window
        and open a worse one: the next waiter polls every _GATE_WAIT_S and
        would race its GATE START ahead of this FINISH, turning the queue
        narrative asserted in this file (alpha_finish < beta_start) into a
        coin flip on a box whose load spikes produced this row. So the order
        stays, and the notice for the REAL captured row — not a synthetic
        dict — must read true beside the FINISH it may follow."""
        self.fixture_suite("""import unittest

class FinishWindowFixture(unittest.TestCase):
    def test_ok(self):
        pass
""")
        os.environ["HELM_CHAT_NAME"] = "finish-window"
        captured = []
        real = gate._gate_post

        def capture(repo, text, holder):
            if text.startswith("GATE FINISH"):
                captured.append(
                    (text, seats.gate_queue_snapshot(self.repo)[0][0]))
                raise KeyboardInterrupt
            return real(repo, text, holder)

        require_supervisor()
        with mock.patch.object(gate, "_gate_post", side_effect=capture):
            with self.assertRaises(KeyboardInterrupt):
                gate.run(repo=self.repo)
        self.assertEqual(len(captured), 1, captured)
        finish, row = captured[0]
        # THE STATUS WORD AND ITS RECEIPT TOKEN, TOGETHER. A bare "gate:"
        # substring also matches the machinery holder `@system:gate:<pid>`, so
        # an UNMINTED finish whose guard never started satisfies it: measured
        # on a node whose cgroup root lacks write access, where this arm read
        # "GATE FINISH #1 @system:gate:1733288 UNKNOWN — gate guard exited
        # before reporting its supervisor" and passed.
        self.assertRegex(finish, r" [A-Z]+ gate:[0-9a-f]+",
                         "the fixture must finish with a MINTED receipt or "
                         "this proves nothing about contradicting a verdict: "
                         "%r" % finish)
        self.assertEqual(row["state"], "finishing", row)
        notice = gate._orphan_consequence(row)
        self.assertIn("it is OVER", notice, notice)
        self.assertIn("GATE FINISH", notice,
                      "the orphan must name the line that may already carry "
                      "the result, or it reads as a denial of %r" % finish)
        self.assertNotIn("NO RECEIPT EXISTS", notice,
                         "the attempt whose own FINISH says %r was told no "
                         "receipt exists" % finish)
        self.assertNotIn("skip", notice.lower(), notice)

    def test_dead_launcher_kills_its_bound_suite_child(self):
        log = os.path.join(self.tmp, "parent-death-start")
        descendant_path = os.path.join(self.tmp, "parent-death-descendant")
        self.fixture_suite("""import os
import subprocess
import sys
import time
import unittest

class ParentDeathFixture(unittest.TestCase):
    def test_waits(self):
        child = subprocess.Popen([sys.executable, "-c",
                                  "import time; time.sleep(10)"])
        from tests._gate_pid import host_child, host_self
        child_pid, _starttime = host_child(child.pid)
        with open(%r, "w") as f:
            f.write(str(child_pid))
        open(%r, "w").close()
        time.sleep(10)
""" % (descendant_path, log))
        launcher = self.worker("doomed")
        self.wait_for(lambda: os.path.exists(log)
                      and os.path.exists(descendant_path))
        with open(descendant_path) as f:
            descendant_pid = int(f.read())
        descendant = descendant_pid, seats._get_pid_starttime(descendant_pid)
        self.assertIsNotNone(descendant[1])
        self.wait_for(lambda: seats.gate_queue_snapshot(self.repo)[0]
                      and seats.gate_queue_snapshot(self.repo)[0][0].get(
                          "state") == "running")
        row = seats.gate_queue_snapshot(self.repo)[0][0]
        self.assertEqual(row["state"], "running")
        child = row["child_pid"], row["child_starttime"]
        self.assertTrue(seats._is_pid_alive(
            child[0], child[1], require_starttime=True))
        launcher.kill()
        launcher.communicate(timeout=5)
        self.wait_for(lambda: not seats._is_pid_alive(
            child[0], child[1], require_starttime=True), timeout=5)
        self.wait_for(lambda: not seats._is_pid_alive(
            descendant[0], descendant[1], require_starttime=True), timeout=5)

    def test_stopped_supervisor_resumes_tree_cleanup_after_launcher_death(self):  # noqa: VACUOUS_ASSERTION — the exact detached generation and observed T supervisor are positive witnesses before both must die
        descendant_path = os.path.join(self.tmp, "stopped-supervisor-descendant")
        stop_path = os.path.join(self.tmp, "stop-supervisor")
        self.fixture_suite("""import os
import signal
import subprocess
import sys
import time
import unittest

class StoppedSupervisorFixture(unittest.TestCase):
    def test_stops_supervisor(self):
        child = subprocess.Popen([sys.executable, "-c",
                                  "import time; time.sleep(10)"],
                                 start_new_session=True)
        from tests._gate_pid import host_child, host_self
        child_pid, starttime = host_child(child.pid)
        with open(%r, "w") as f:
            f.write("%%d %%s" %% (child_pid, starttime))
        while not os.path.exists(%r):
            time.sleep(0.01)
        time.sleep(10)
""" % (descendant_path, stop_path))
        launcher = self.worker("stopped-supervisor")
        launcher_start = seats._get_pid_starttime(launcher.pid)
        self.assertIsNotNone(launcher_start)

        def launcher_state():
            row = seats._proc_stat_link(launcher.pid, with_state=True)
            if row and row[0] == launcher_start:
                return row[2]
            return None

        descendant = supervisor = None
        try:
            self.wait_for(lambda: os.path.exists(descendant_path))
            with open(descendant_path) as f:
                descendant = tuple(map(int, f.read().split()))
            self.wait_for(lambda: seats.gate_queue_snapshot(self.repo)[0]
                          and seats.gate_queue_snapshot(self.repo)[0][0].get(
                              "state") == "running")
            row = seats.gate_queue_snapshot(self.repo)[0][0]
            supervisor = row["child_pid"], row["child_starttime"]
            open(stop_path, "w").close()
            os.kill(supervisor[0], signal.SIGSTOP)
            self.wait_for(lambda: seats._gate_pid_state(
                *supervisor) in ("T", "t"))
            launcher.kill()
            self.wait_for(lambda: launcher_state() == "Z")
            self.wait_for(lambda: not seats._is_pid_alive(
                supervisor[0], supervisor[1], require_starttime=True), timeout=5)
            self.wait_for(lambda: not seats._is_pid_alive(
                descendant[0], descendant[1], require_starttime=True), timeout=5)
            self.assertEqual(launcher_state(), "Z")
            launcher.communicate(timeout=5)
            self.wait_for(lambda: not seats.gate_queue_snapshot(self.repo)[0],
                          timeout=5)
        finally:
            if launcher.poll() is None:
                launcher.kill()
            launcher.communicate(timeout=5)
            for process in (supervisor, descendant):
                if process and seats._is_pid_alive(
                        process[0], process[1], require_starttime=True):
                    try:
                        os.kill(process[0], signal.SIGCONT)
                        os.kill(process[0], signal.SIGKILL)
                    except OSError:
                        pass

    def test_stopped_guard_resumes_cleanup_after_launcher_death(self):  # noqa: VACUOUS_ASSERTION — exact stopped guard and detached generation exist before launcher death, then the external watchdog must remove both without FIFO activity
        descendant_path = os.path.join(self.tmp, "stopped-guard-descendant")
        self.fixture_suite("""import subprocess
import sys
import time
import unittest
from tests._gate_pid import host_child

class StoppedGuardFixture(unittest.TestCase):
    def test_waits(self):
        child = subprocess.Popen([sys.executable, "-c",
                                  "import time; time.sleep(10)"],
                                 start_new_session=True)
        child_pid, starttime = host_child(child.pid)
        with open(%r, "w") as f:
            f.write("%%d %%s" %% (child_pid, starttime))
        time.sleep(10)
""" % descendant_path)
        launcher = self.worker("stopped-guard")
        launcher_start = seats._get_pid_starttime(launcher.pid)
        self.assertIsNotNone(launcher_start)
        guard = supervisor = descendant = None
        try:
            self.wait_for(lambda: os.path.exists(descendant_path))
            with open(descendant_path) as f:
                descendant = tuple(map(int, f.read().split()))
            self.wait_for(lambda: seats.gate_queue_snapshot(self.repo)[0]
                          and seats.gate_queue_snapshot(self.repo)[0][0].get(
                              "state") == "running")
            row = seats.gate_queue_snapshot(self.repo)[0][0]
            supervisor = row["child_pid"], row["child_starttime"]
            link = seats._proc_stat_link(supervisor[0])
            self.assertIsNotNone(link)
            guard = link[1], seats._get_pid_starttime(link[1])
            self.assertIsNotNone(guard[1])
            os.kill(guard[0], signal.SIGSTOP)
            self.wait_for(lambda: seats._gate_pid_state(
                *guard) in ("T", "t"))
            launcher.kill()
            self.wait_for(lambda: (seats._proc_stat_link(
                launcher.pid, with_state=True) or (None, None, None))[2] == "Z")
            self.wait_for(lambda: not seats._is_pid_alive(
                guard[0], guard[1], require_starttime=True), timeout=5)
            self.wait_for(lambda: not seats._is_pid_alive(
                descendant[0], descendant[1], require_starttime=True), timeout=5)
            launcher.communicate(timeout=5)
            self.wait_for(lambda: not seats.gate_queue_snapshot(self.repo)[0],
                          timeout=5)
        finally:
            if launcher.poll() is None:
                launcher.kill()
            launcher.communicate(timeout=5)
            for process in (guard, supervisor, descendant):
                if process and seats._is_pid_alive(
                        process[0], process[1], require_starttime=True):
                    try:
                        os.kill(process[0], signal.SIGCONT)
                        os.kill(process[0], signal.SIGKILL)
                    except OSError:
                        pass

    def stop_outside_claims_lock(self, launcher, park, parked):
        """SIGSTOP the launcher only once its renewer is parked OUTSIDE the
        claims flock, and prove by /proc/locks that it holds none (task/2991).

        A stop at an arbitrary instant can land inside the flock, because
        the renewer takes it every 0.1s, and a STOPPED holder never releases
        it. The arm's own strict claim then raised "held by STOPPED pid",
        which is correct product behaviour for that world and not the world
        this arm asserts about.

        STOP, CHECK AND CONTINUE IS NOT A CURE. gatechild's watchdog polls the
        launcher every 50ms and kills the suite on the first "T" it reads, so
        a stop that is then reversed has already set off the kill. The
        resumed launcher would see its suite die and release the legacy
        claim, so the "claim is still held" assertion would fail. So this
        stops ONCE, at a point the launcher itself reports as outside the
        lock."""
        open(park, "w").close()
        self.wait_for(lambda: os.path.exists(parked))
        os.kill(launcher.pid, signal.SIGSTOP)
        self.wait_for(lambda: seats._gate_pid_state(
            launcher.pid, seats._get_pid_starttime(launcher.pid))
            in ("T", "t"))
        # A stopped process cannot change its locks, so this reading is
        # stable. If some other launcher thread held the flock, this line
        # names it here instead of a strict claim failing 2s later.
        holder = seats._flock_holder(seats.claims_path() + ".lock")
        self.assertNotEqual((holder or (None,))[0], launcher.pid,
                            "the launcher was stopped HOLDING the claims flock")

    def test_stopped_launcher_kills_suite_before_legacy_ttl_expires(self):  # noqa: VACUOUS_ASSERTION — an exact live suite generation dies without FIFO activity before an old runner acquires the expired compatibility claim
        self.stopped_launcher_arm(plant=False)

    def test_stopped_launcher_arm_never_stops_it_inside_the_claims_lock(self):  # noqa: VACUOUS_ASSERTION — the planted launcher is asserted to HOLD the claims flock before the stop, and the shared arm's strict claim is the must-hit that raises when it is stopped holding it
        """task/2991: the launcher is INSIDE the claims flock when the arm
        comes to stop it — planted, not waited for.

        train147 went red on the arm above alone: its own strict claim raised
        "claim lock is unavailable — held by STOPPED pid". The launcher renews
        its legacy lease every 0.1s under that flock, so a SIGSTOP at an
        arbitrary instant sometimes lands inside it, and a stopped holder never
        releases. Here the launcher's renewer sits in the flock until the arm
        asks it to park, so the stop instant can no longer be lucky."""
        self.stopped_launcher_arm(plant=True)

    def stopped_launcher_arm(self, plant):
        suite_path = os.path.join(self.tmp, "stopped-launcher-suite")
        park, parked, hold, inside = (
            os.path.join(self.tmp, "stopped-launcher-" + name)
            for name in ("park", "parked", "hold", "inside"))
        self.fixture_suite("""import os
import time
import unittest

class StoppedLauncherFixture(unittest.TestCase):
    def test_waits(self):
        from tests._gate_pid import host_child, host_self
        pid, starttime = host_self()
        with open(%r, "w") as f:
            f.write("%%d %%s" %% (pid, starttime))
        time.sleep(600)
""" % suite_path)
        env = dict(os.environ)
        env["HELM_CHAT_NAME"] = "stopped-launcher"
        env["CODEX_SESSION_ID"] = "session-stopped-launcher"
        # THE TTL IS NO LONGER THE DEADLINE. It was 1s, and `timeout=1` below
        # relied on that equality to prove the death came from STOP-DETECTION
        # rather than from expiry — one number doing two jobs, the second of
        # which (detection completes within 1s) starves on a loaded fab and
        # produced seven false reds. A generous TTL means a death observed at
        # all cannot be expiry, so the proof no longer needs a tight clock.
        #
        # THE RENEWER CAN PARK, AND IN THE PLANTED ARM IT CAN HOLD. Both hooks
        # sit on the one launcher thread that takes the claims flock while the
        # suite runs (`helm-gate-legacy-renew`; the main thread's FIFO renewal
        # takes the FIFO lock, not this one). `parking_refresh` parks only
        # AFTER refresh_claim returns, i.e. after the flock is released.
        # `holding_read` runs INSIDE refresh_claim's flock and stays there
        # until the arm asks to park, then half a second longer, so an arm
        # that stops without waiting for `parked` stops a holder.
        # THE LAUNCHER IS A CHILD, SO IT IS PINNED AS ONE (task/1740 P3): the
        # suite's tripwire cannot see it, and bare it read this node's cap.
        code = pin_admission_code(self.admission_proc, self.admissions) + """import os
import threading
import time
from helm import gate, seats, seats_claims
gate._GATE_LEGACY_TTL_S = 30
gate._GATE_LEGACY_RENEW_S = 0.1
gate._GATE_RENEW_S = 0.1
park, parked, hold, inside = %r, %r, %r, %r
refresh, read = seats.refresh_claim, seats_claims._claims_read


def parking_refresh(*args, **kwargs):
    got = refresh(*args, **kwargs)
    if os.path.exists(park):
        open(parked, "w").close()
        threading.Event().wait()
    return got


def holding_read(strict=False):
    if threading.current_thread().name == "helm-gate-legacy-renew" \\
            and os.path.exists(hold) and not os.path.exists(inside):
        open(inside, "w").close()
        while not os.path.exists(park):
            time.sleep(0.01)
        time.sleep(0.5)
    return read(strict)


seats.refresh_claim = parking_refresh
if hold:
    seats_claims._claims_read = holding_read
gate.run(repo=%r)
""" % (park, parked, hold if plant else None, inside, self.repo)
        require_supervisor()
        launcher = subprocess.Popen((sys.executable, "-c", code), env=env,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
        lease = suite = None
        resource, err = gate._legacy_gate_resource(self.repo)
        self.assertIsNone(err)
        try:
            self.wait_for(lambda: os.path.exists(suite_path))
            with open(suite_path) as f:
                suite = tuple(map(int, f.read().split()))
            self.assertTrue(seats._is_pid_alive(
                suite[0], suite[1], require_starttime=True))
            # ADMITTED ON THE FIXTURE BOX: the running launcher's intent row is
            # in the fixture ledger, not in this node's.
            self.assertEqual(
                [row["pid"] for row in gate._admissions_load(self.admissions)],
                [launcher.pid])
            if plant:
                open(hold, "w").close()
                self.wait_for(lambda: os.path.exists(inside))
                # CONTROL: the residue is really there. Without it a plant that
                # never fired would pass this arm by being the arm above.
                self.assertEqual(
                    (seats._flock_holder(seats.claims_path() + ".lock")
                     or (None,))[0], launcher.pid,
                    "the planted launcher does not hold the claims flock")
            self.stop_outside_claims_lock(launcher, park, parked)
            # No explicit timeout: this no longer races the TTL, so the
            # default budget absorbs a starved poll loop on a co-loaded box.
            #
            # AND THE FIXTURE SLEEPS 600s, NOT 10s, WHICH IS WHAT MAKES THE
            # OBSERVED DEATH MEAN ANYTHING. Caught at review: with
            # a 10s fixture the child EXITS ON ITS OWN inside this budget, so a
            # broken stop-detection path still satisfies the wait — the test
            # then proves death-before-expiry and NOT death-by-stop-detection.
            # My own mutation "killed" that mutant in 10.266s, i.e. by winning a
            # race by a hair; one second of scheduling stall flips it green. A
            # fixture that cannot plausibly exit on its own inside the window
            # removes the confound BY CONSTRUCTION rather than out-running it.
            self.wait_for(lambda: not seats._is_pid_alive(
                suite[0], suite[1], require_starttime=True))
            # THE ORDERING, PROVEN BY STATE. The suite is dead while the legacy
            # claim is still HELD — so the death cannot have been expiry, which
            # is exactly what the old `timeout=1` inferred from the clock and
            # what a widened timeout would have silently stopped proving.
            held, why, no_lease = seats.claim(
                resource, "old-runner", ttl=60,
                session="old-session", strict=True)
            self.assertFalse(held, "legacy claim already expired: %s" % why)
            self.assertIsNone(no_lease)
            # Now retire it DETERMINISTICALLY instead of sleeping past it. The
            # record carries absolute exp_mono/exp_wall, so rewriting them is a
            # real synchronisation point and costs no wall-clock at all.
            self.expire_claim(resource)
            ok, message, lease = seats.claim(
                resource, "old-runner", ttl=60,
                session="old-session", strict=True)
            self.assertTrue(ok, message)
            self.assertFalse(seats._is_pid_alive(
                suite[0], suite[1], require_starttime=True))
        finally:
            if lease:
                seats.release(
                    resource, "old-runner", lease=lease,
                    session="old-session", strict=True)
            if launcher.poll() is None:
                try:
                    os.kill(launcher.pid, signal.SIGCONT)
                    launcher.kill()
                except OSError:
                    pass
            launcher.communicate(timeout=5)
            if suite and seats._is_pid_alive(
                    suite[0], suite[1], require_starttime=True):
                try:
                    os.kill(suite[0], signal.SIGKILL)
                except OSError:
                    pass

    def test_launcher_death_before_child_bind_never_starts_suite(self):  # noqa: VACUOUS_ASSERTION — guard, supervisor, and watchdog are exact positive witnesses before all die without creating the suite marker
        bind_path = os.path.join(self.tmp, "prebind-entered")
        suite_path = os.path.join(self.tmp, "prebind-suite-started")
        self.fixture_suite("""import os
import signal
import time
import unittest

class PrebindFixture(unittest.TestCase):
    def test_must_not_start(self):
        open(%r, "w").close()
        os.kill(os.getppid(), signal.SIGSTOP)
        time.sleep(10)
""" % suite_path)
        env = dict(os.environ)
        env["HELM_CHAT_NAME"] = "prebind-death"
        env["CODEX_SESSION_ID"] = "session-prebind-death"
        code = pin_admission_code(self.admission_proc, self.admissions) + """import time
from unittest import mock
from helm import gate, seats
real = seats.gate_queue_bind_child
def delayed(*args, **kwargs):
    open(%r, "w").close()
    time.sleep(10)
    return real(*args, **kwargs)
with mock.patch.object(seats, "gate_queue_bind_child", side_effect=delayed):
    gate.run(repo=%r)
""" % (bind_path, self.repo)
        require_supervisor()
        launcher = subprocess.Popen((sys.executable, "-c", code), env=env,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
        guard = supervisor = watchdog = None

        def children(pid):
            try:
                with open("/proc/%d/task/%d/children" % (pid, pid)) as f:
                    return [int(child) for child in f.read().split()]
            except OSError:
                return []

        try:
            self.wait_for(lambda: os.path.exists(bind_path))
            self.wait_for(lambda: len(children(launcher.pid)) == 2)
            roles = {}
            for pid in children(launcher.pid):
                with open("/proc/%d/cmdline" % pid, "rb") as f:
                    argv = f.read().split(b"\0")
                if b"--guard" in argv:
                    roles["guard"] = pid, argv
                if b"--watch" in argv:
                    roles["watchdog"] = pid, argv
            self.assertEqual(set(roles), {"guard", "watchdog"})
            pid, guard_argv = roles["guard"]
            guard = pid, seats._get_pid_starttime(pid)
            self.assertIsNotNone(guard[1])
            self.assertIn(b"--guard", guard_argv)
            pid, watchdog_argv = roles["watchdog"]
            watchdog = pid, seats._get_pid_starttime(pid)
            self.assertIsNotNone(watchdog[1])
            self.assertIn(b"--watch", watchdog_argv)
            self.wait_for(lambda: len(children(guard[0])) == 1)
            pid = children(guard[0])[0]
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                supervisor_argv = f.read().split(b"\0")
            supervisor = pid, seats._get_pid_starttime(pid)
            self.assertIsNotNone(supervisor[1])
            self.assertIn(b"--supervise", supervisor_argv)
            self.assertFalse(os.path.exists(suite_path))
            os.kill(supervisor[0], signal.SIGSTOP)
            self.wait_for(lambda: seats._gate_pid_state(
                *supervisor) in ("T", "t"))
            launcher.kill()
            launcher.communicate(timeout=5)
            self.wait_for(lambda: not seats.gate_queue_snapshot(self.repo)[0],
                          timeout=5)
            for process in (guard, supervisor, watchdog):
                self.wait_for(lambda p=process: not seats._is_pid_alive(
                    p[0], p[1], require_starttime=True), timeout=5)
            self.assertFalse(os.path.exists(suite_path))
        finally:
            if launcher.poll() is None:
                launcher.kill()
            launcher.communicate(timeout=5)
            for process in (guard, supervisor, watchdog):
                if process and seats._is_pid_alive(
                        process[0], process[1], require_starttime=True):
                    try:
                        os.kill(process[0], signal.SIGCONT)
                        os.kill(process[0], signal.SIGKILL)
                    except OSError:
                        pass

    def test_sigkill_supervisor_cannot_release_detached_work(self):  # noqa: VACUOUS_ASSERTION — exact suite and detached generations are live beside a Z supervisor, then both must die before the launcher releases
        descendant_path = os.path.join(self.tmp, "sigkill-descendant")
        self.fixture_suite("""import os
import subprocess
import sys
import time
import unittest

class SigkillSupervisorFixture(unittest.TestCase):
    def test_detaches(self):
        child = subprocess.Popen([sys.executable, "-c",
                                  "import time; time.sleep(15)"],
                                 start_new_session=True)
        from tests._gate_pid import host_child, host_self
        suite_pid, suite_start = host_self()
        child_pid, starttime = host_child(child.pid)
        with open(%r, "w") as f:
            f.write("%%d %%s %%d %%s" %% (
                suite_pid, suite_start, child_pid, starttime))
        time.sleep(15)
""" % descendant_path)
        launcher = self.worker("sigkill-supervisor")
        suite = descendant = supervisor = None
        try:
            self.wait_for(lambda: os.path.exists(descendant_path))
            with open(descendant_path) as f:
                row = tuple(map(int, f.read().split()))
            suite, descendant = row[:2], row[2:]
            self.wait_for(lambda: seats.gate_queue_snapshot(self.repo)[0]
                          and seats.gate_queue_snapshot(self.repo)[0][0].get(
                              "state") == "running")
            row = seats.gate_queue_snapshot(self.repo)[0][0]
            supervisor = row["child_pid"], row["child_starttime"]
            for process in (suite, descendant):
                self.assertTrue(seats._is_pid_alive(
                    process[0], process[1], require_starttime=True))
            os.kill(supervisor[0], signal.SIGKILL)
            out, errout = launcher.communicate(timeout=10)
            self.assertIn(launcher.returncode, (0, 1), (out, errout))
            self.assertTrue((out + errout).strip())
            for process in (suite, descendant):
                self.assertFalse(seats._is_pid_alive(
                    process[0], process[1], require_starttime=True))
            self.assertEqual(seats.gate_queue_snapshot(self.repo)[0], [])
        finally:
            if launcher.poll() is None:
                launcher.kill()
            launcher.communicate(timeout=5)
            for process in (supervisor, suite, descendant):
                if process and seats._is_pid_alive(
                        process[0], process[1], require_starttime=True):
                    try:
                        os.kill(process[0], signal.SIGKILL)
                    except OSError:
                        pass

    def test_sigkill_guard_cannot_release_detached_work(self):  # noqa: VACUOUS_ASSERTION — exact guard, supervisor, suite, and detached generations exist before guard death; the latter three must die before release
        descendant_path = os.path.join(self.tmp, "sigkill-guard-descendant")
        self.fixture_suite("""import os
import subprocess
import sys
import time
import unittest

class SigkillGuardFixture(unittest.TestCase):
    def test_detaches(self):
        child = subprocess.Popen([sys.executable, "-c",
                                  "import time; time.sleep(15)"],
                                 start_new_session=True)
        from tests._gate_pid import host_child, host_self
        suite_pid, suite_start = host_self()
        child_pid, starttime = host_child(child.pid)
        with open(%r, "w") as f:
            f.write("%%d %%s %%d %%s" %% (
                suite_pid, suite_start, child_pid, starttime))
        time.sleep(15)
""" % descendant_path)
        launcher = self.worker("sigkill-guard")
        guard = supervisor = suite = descendant = None
        try:
            self.wait_for(lambda: os.path.exists(descendant_path))
            with open(descendant_path) as f:
                row = tuple(map(int, f.read().split()))
            suite, descendant = row[:2], row[2:]
            self.wait_for(lambda: seats.gate_queue_snapshot(self.repo)[0]
                          and seats.gate_queue_snapshot(self.repo)[0][0].get(
                              "state") == "running")
            row = seats.gate_queue_snapshot(self.repo)[0][0]
            supervisor = row["child_pid"], row["child_starttime"]
            link = seats._proc_stat_link(supervisor[0])
            self.assertIsNotNone(link)
            guard = link[1], seats._get_pid_starttime(link[1])
            self.assertIsNotNone(guard[1])
            for process in (supervisor, suite, descendant):
                self.assertTrue(seats._is_pid_alive(
                    process[0], process[1], require_starttime=True))
            os.kill(guard[0], signal.SIGKILL)
            out, errout = launcher.communicate(timeout=10)
            self.assertIn(launcher.returncode, (0, 1), (out, errout))
            self.assertTrue((out + errout).strip())
            for process in (supervisor, suite, descendant):
                self.assertFalse(seats._is_pid_alive(
                    process[0], process[1], require_starttime=True))
            self.assertEqual(seats.gate_queue_snapshot(self.repo)[0], [])
        finally:
            if launcher.poll() is None:
                launcher.kill()
            launcher.communicate(timeout=5)
            for process in (guard, supervisor, suite, descendant):
                if process and seats._is_pid_alive(
                        process[0], process[1], require_starttime=True):
                    try:
                        os.kill(process[0], signal.SIGKILL)
                    except OSError:
                        pass

    def test_sigkill_guard_and_supervisor_cannot_release_detached_work(self):  # noqa: VACUOUS_ASSERTION — guard, supervisor, suite, and detached generations are live in one cgroup before both cleanup owners die; cgroup.kill must remove the latter two before an old claim succeeds
        descendant_path = os.path.join(self.tmp, "sigkill-both-descendant")
        self.fixture_suite("""import subprocess
import sys
import time
import unittest
from tests._gate_pid import host_child, host_self

class SigkillBothFixture(unittest.TestCase):
    def test_detaches(self):
        child = subprocess.Popen([sys.executable, "-c",
                                  "import time; time.sleep(15)"],
                                 start_new_session=True)
        suite_pid, suite_start = host_self()
        child_pid, starttime = host_child(child.pid)
        with open(%r, "w") as f:
            f.write("%%d %%s %%d %%s" %% (
                suite_pid, suite_start, child_pid, starttime))
        time.sleep(15)
""" % descendant_path)
        launcher = self.worker("sigkill-both")
        guard = supervisor = suite = descendant = lease = None
        resource, err = gate._legacy_gate_resource(self.repo)
        self.assertIsNone(err)
        try:
            self.wait_for(lambda: os.path.exists(descendant_path))
            with open(descendant_path) as f:
                row = tuple(map(int, f.read().split()))
            suite, descendant = row[:2], row[2:]
            self.wait_for(lambda: seats.gate_queue_snapshot(self.repo)[0]
                          and seats.gate_queue_snapshot(self.repo)[0][0].get(
                              "state") == "running")
            row = seats.gate_queue_snapshot(self.repo)[0][0]
            supervisor = row["child_pid"], row["child_starttime"]
            link = seats._proc_stat_link(supervisor[0])
            self.assertIsNotNone(link)
            guard = link[1], seats._get_pid_starttime(link[1])
            self.assertIsNotNone(guard[1])
            for process in (guard, supervisor, suite, descendant):
                self.assertTrue(seats._is_pid_alive(
                    process[0], process[1], require_starttime=True))
            os.kill(supervisor[0], signal.SIGSTOP)
            self.wait_for(lambda: seats._gate_pid_state(
                *supervisor) in ("T", "t"))
            os.kill(guard[0], signal.SIGKILL)
            os.kill(supervisor[0], signal.SIGKILL)
            out, errout = launcher.communicate(timeout=10)
            self.assertIn(launcher.returncode, (0, 1), (out, errout))
            self.assertTrue((out + errout).strip())
            for process in (suite, descendant):
                self.assertFalse(seats._is_pid_alive(
                    process[0], process[1], require_starttime=True))
            self.assertEqual(seats.gate_queue_snapshot(self.repo)[0], [])
            ok, message, lease = seats.claim(
                resource, "old-runner", ttl=60,
                session="old-session", strict=True)
            self.assertTrue(ok, message)
            self.assertFalse(seats._is_pid_alive(
                descendant[0], descendant[1], require_starttime=True))
        finally:
            if lease:
                seats.release(
                    resource, "old-runner", lease=lease,
                    session="old-session", strict=True)
            if launcher.poll() is None:
                launcher.kill()
            launcher.communicate(timeout=5)
            for process in (guard, supervisor, suite, descendant):
                if process and seats._is_pid_alive(
                        process[0], process[1], require_starttime=True):
                    try:
                        os.kill(process[0], signal.SIGKILL)
                    except OSError:
                        pass

    def test_timeout_kills_descendants_after_the_group_leader_exits(self):
        descendant_path = os.path.join(self.tmp, "timeout-descendant")
        self.fixture_suite("""import os
import subprocess
import sys
import time
import unittest

class DescendantFixture(unittest.TestCase):
    def test_forks(self):
        child = subprocess.Popen([sys.executable, "-c",
                                  "import time; time.sleep(60)"])
        from tests._gate_pid import host_child, host_self
        child_pid, starttime = host_child(child.pid)
        with open(%r, "w") as f:
            f.write("%%d %%s" %% (child_pid, starttime))
        time.sleep(60)
""" % descendant_path)
        os.environ["HELM_CHAT_NAME"] = "descendants"
        require_supervisor()
        started = time.monotonic()
        row, err = gate.run(repo=self.repo, timeout=0.5)
        self.assertIsNone(err, err)
        _assert_gate_status(self, row, "UNKNOWN")
        # A HANG BACKSTOP, NOT THE JUDGEMENT. At 3s this arm FAILED at
        # 3.021 against a 3.000 bound — it missed by TWENTY-ONE
        # MILLISECONDS, 0.7% over — and it took four unrelated lanes red
        # in one hour. A bound a real run can cross by 0.7% is a coin with
        # a slight bias, not a test. `assertLess(elapsed, 3)` asks WAS THE
        # MACHINE FAST when the claim is DID THE GATE REFUSE TO WAIT.
        # The separation was widened at the SOURCE rather than the
        # threshold slid inside a gap too narrow to hold it: the fixture
        # sleeps 60s, so waiting costs ~60s and 20 still discriminates
        # with 3x margin. Raising the number ALONE past 10 would have
        # kept every test passing and destroyed the discrimination.
        # THE MARGIN IS THE WHOLE JUSTIFICATION. A contention model
        # motivated this lane and was REFUTED — four concurrent suites on
        # one node produced zero occurrences of these arms — and the load
        # figure once quoted here was the laptop's, not the fab node
        # that ran them. Contention made the defect visible; it was never
        # what made it true.
        self.assertLess(time.monotonic() - started, 20)
        with open(descendant_path) as f:
            descendant_pid, descendant_start = map(int, f.read().split())
        self.wait_for(lambda: not seats._is_pid_alive(
            descendant_pid, descendant_start, require_starttime=True), timeout=5)
        self.assertEqual(seats.gate_queue_snapshot(self.repo)[0], [])  # noqa: VACUOUS_ASSERTION — the exact descendant existed and was killed before the queue emptied

    def test_timeout_kills_detached_descendants_without_waiting_for_pipe_eof(self):
        descendant_path = os.path.join(self.tmp, "timeout-detached-descendant")
        self.fixture_suite("""import os
import subprocess
import sys
import time
import unittest

class DetachedDescendantFixture(unittest.TestCase):
    def test_detaches(self):
        child = subprocess.Popen([sys.executable, "-c",
                                  "import time; time.sleep(60)"],
                                 start_new_session=True)
        from tests._gate_pid import host_child, host_self
        child_pid, starttime = host_child(child.pid)
        with open(%r, "w") as f:
            f.write("%%d %%s" %% (child_pid, starttime))
        time.sleep(60)
""" % descendant_path)
        os.environ["HELM_CHAT_NAME"] = "detached-descendant"
        require_supervisor()
        started = time.monotonic()
        row, err = gate.run(repo=self.repo, timeout=0.5)
        self.assertIsNone(err, err)
        _assert_gate_status(self, row, "UNKNOWN")
        # A HANG BACKSTOP, NOT THE JUDGEMENT. At 3s this arm FAILED at
        # 3.021 against a 3.000 bound — it missed by TWENTY-ONE
        # MILLISECONDS, 0.7% over — and it took four unrelated lanes red
        # in one hour. A bound a real run can cross by 0.7% is a coin with
        # a slight bias, not a test. `assertLess(elapsed, 3)` asks WAS THE
        # MACHINE FAST when the claim is DID THE GATE REFUSE TO WAIT.
        # The separation was widened at the SOURCE rather than the
        # threshold slid inside a gap too narrow to hold it: the fixture
        # sleeps 60s, so waiting costs ~60s and 20 still discriminates
        # with 3x margin. Raising the number ALONE past 10 would have
        # kept every test passing and destroyed the discrimination.
        # THE MARGIN IS THE WHOLE JUSTIFICATION. A contention model
        # motivated this lane and was REFUTED — four concurrent suites on
        # one node produced zero occurrences of these arms — and the load
        # figure once quoted here was the laptop's, not the fab node
        # that ran them. Contention made the defect visible; it was never
        # what made it true.
        self.assertLess(time.monotonic() - started, 20)
        with open(descendant_path) as f:
            descendant_pid, descendant_start = map(int, f.read().split())
        self.wait_for(lambda: not seats._is_pid_alive(
            descendant_pid, descendant_start, require_starttime=True), timeout=5)
        self.assertEqual(seats.gate_queue_snapshot(self.repo)[0], [])  # noqa: VACUOUS_ASSERTION — a detached exact descendant held the pipes before bounded tree shutdown emptied the queue

    def test_completed_suite_reaps_an_adopted_detached_descendant(self):
        descendant_path = os.path.join(self.tmp, "completed-detached-descendant")
        self.fixture_suite("""import subprocess
import sys
import unittest

class CompletedDetachedFixture(unittest.TestCase):
    def test_detaches_and_returns(self):
        child = subprocess.Popen([sys.executable, "-c",
                                  "import time; time.sleep(60)"],
                                 start_new_session=True)
        from tests._gate_pid import host_child, host_self
        child_pid, starttime = host_child(child.pid)
        with open(%r, "w") as f:
            f.write("%%d %%s" %% (child_pid, starttime))
""" % descendant_path)
        os.environ["HELM_CHAT_NAME"] = "completed-detached"
        require_supervisor()
        started = time.monotonic()
        row, err = gate.run(repo=self.repo, timeout=2)
        self.assertIsNone(err, err)
        _assert_gate_status(self, row, "OK")
        # A HANG BACKSTOP, NOT THE JUDGEMENT. At 3s this arm FAILED at
        # 3.021 against a 3.000 bound — it missed by TWENTY-ONE
        # MILLISECONDS, 0.7% over — and it took four unrelated lanes red
        # in one hour. A bound a real run can cross by 0.7% is a coin with
        # a slight bias, not a test. `assertLess(elapsed, 3)` asks WAS THE
        # MACHINE FAST when the claim is DID THE GATE REFUSE TO WAIT.
        # The separation was widened at the SOURCE rather than the
        # threshold slid inside a gap too narrow to hold it: the fixture
        # sleeps 60s, so waiting costs ~60s and 20 still discriminates
        # with 3x margin. Raising the number ALONE past 10 would have
        # kept every test passing and destroyed the discrimination.
        # THE MARGIN IS THE WHOLE JUSTIFICATION. A contention model
        # motivated this lane and was REFUTED — four concurrent suites on
        # one node produced zero occurrences of these arms — and the load
        # figure once quoted here was the laptop's, not the fab node
        # that ran them. Contention made the defect visible; it was never
        # what made it true.
        self.assertLess(time.monotonic() - started, 20)
        with open(descendant_path) as f:
            descendant_pid, descendant_start = map(int, f.read().split())
        self.wait_for(lambda: not seats._is_pid_alive(
            descendant_pid, descendant_start, require_starttime=True), timeout=5)
        self.assertEqual(seats.gate_queue_snapshot(self.repo)[0], [])  # noqa: VACUOUS_ASSERTION — the adopted detached generation died before a green suite result released the queue

    def test_completed_suite_quiesces_a_forking_detached_tree(self):
        descendants_path = os.path.join(self.tmp, "forking-detached-descendants")
        # POSIX_SPAWN, NOT Popen, AND THAT IS THE FIX RATHER THAN A STYLE
        # CHOICE. This loop deliberately abandons a new detached child every
        # 2ms — quiescing them is the whole subject of the test — so nothing
        # ever reaps them. A Popen for a process you will never wait on is a
        # handle whose __del__ raises ResourceWarning at collection, and under
        # `-W error::ResourceWarning` those land in the gate's captured output
        # AFTER unittest's own footer. The runner still prints OK; the GATE
        # PARSER then reads "the summary says OK but the run count is
        # unreadable or not its terminal footer" and mints UNKNOWN — a module
        # whose every test passes yet whose receipt can never bind, invisible
        # because the runner says OK.
        #
        # os.posix_spawn gives the same detached child and the same pid with
        # NO handle to leak, so there is nothing to release and nothing to
        # warn about. The repo's rule is to free the resource, never to
        # silence the channel: "a warning channel nobody can read is a
        # warning channel where a real one goes unseen."
        #
        # THE CHILD CALLS setsid() ITSELF; THE `setsid=True` KWARG IS NOT
        # PORTABLE AND THIS LANE PROVED IT THE EXPENSIVE WAY. That kwarg needs
        # POSIX_SPAWN_SETSID at interpreter BUILD time and raises
        # NotImplementedError("posix_spawn: setsid unavailable on this
        # platform") where it is absent — measured on the gate's own
        # CPython-3.14.6 while the local 3.14.4 has it, so a run that passes
        # where you stand can fail where it binds. os.setsid() is plain POSIX
        # and available everywhere, so the detachment moves into the child's
        # first statement where no build flag can take it away.
        spawner = """import os
import sys
import time
from tests._gate_pid import host_child, host_self

os.setsid()

while True:
    pid = os.posix_spawn(
        sys.executable,
        [sys.executable, "-c", "import os; os.setsid(); import time; time.sleep(60)"],
        os.environ)
    child_pid, starttime = host_child(pid)
    with open(%r, "a") as f:
        f.write("%%d %%s\\n" %% (child_pid, starttime))
        f.flush()
    time.sleep(0.002)
""" % descendants_path
        self.fixture_suite("""import os
import sys
import time
import unittest

class ForkingDetachedFixture(unittest.TestCase):
    def test_detaches_and_forks(self):
        # Same reason as the spawner above: this detached process outlives the
        # fixture by design, so a Popen here is a handle nobody will ever reap.
        os.posix_spawn(sys.executable, [sys.executable, "-c", %r],
                       os.environ)
        deadline = time.monotonic() + 2
        while not os.path.exists(%r) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(os.path.exists(%r))
""" % (spawner, descendants_path, descendants_path))
        os.environ["HELM_CHAT_NAME"] = "forking-detached"
        require_supervisor()
        started = time.monotonic()
        row, err = gate.run(repo=self.repo, timeout=2)
        self.assertIsNone(err, err)
        _assert_gate_status(self, row, "OK")
        # A HANG BACKSTOP, NOT THE JUDGEMENT. At 3s this arm FAILED at
        # 3.021 against a 3.000 bound — it missed by TWENTY-ONE
        # MILLISECONDS, 0.7% over — and it took four unrelated lanes red
        # in one hour. A bound a real run can cross by 0.7% is a coin with
        # a slight bias, not a test. `assertLess(elapsed, 3)` asks WAS THE
        # MACHINE FAST when the claim is DID THE GATE REFUSE TO WAIT.
        # The separation was widened at the SOURCE rather than the
        # threshold slid inside a gap too narrow to hold it: the fixture
        # sleeps 60s, so waiting costs ~60s and 20 still discriminates
        # with 3x margin. Raising the number ALONE past 10 would have
        # kept every test passing and destroyed the discrimination.
        # THE MARGIN IS THE WHOLE JUSTIFICATION. A contention model
        # motivated this lane and was REFUTED — four concurrent suites on
        # one node produced zero occurrences of these arms — and the load
        # figure once quoted here was the laptop's, not the fab node
        # that ran them. Contention made the defect visible; it was never
        # what made it true.
        self.assertLess(time.monotonic() - started, 20)
        with open(descendants_path) as f:
            descendants = [tuple(map(int, line.split())) for line in f]
        self.assertGreater(len(descendants), 0)
        survivors = [row for row in descendants if seats._is_pid_alive(
            row[0], row[1], require_starttime=True)]
        try:
            self.assertEqual(survivors, [])
        finally:
            for pid, starttime in survivors:
                if seats._is_pid_alive(pid, starttime, require_starttime=True):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
        self.assertEqual(seats.gate_queue_snapshot(self.repo)[0], [])  # noqa: VACUOUS_ASSERTION — the exact generations prove a mutating detached tree was frozen and killed before green release

    def test_gate_guard_never_kills_a_concurrent_launcher_child(self):  # noqa: VACUOUS_ASSERTION — the thread records one exact live unrelated generation before the gate returns, then that same generation must survive
        started_path = os.path.join(self.tmp, "guard-concurrent-started")
        self.fixture_suite("""import time
import unittest

class GuardConcurrentFixture(unittest.TestCase):
    def test_waits(self):
        open(%r, "w").close()
        time.sleep(0.5)
""" % started_path)
        unrelated = []

        def spawn_unrelated():
            self.wait_for(lambda: os.path.exists(started_path))
            proc = subprocess.Popen([sys.executable, "-c",
                                     "import time; time.sleep(10)"])
            unrelated.append((proc, seats._get_pid_starttime(proc.pid)))

        require_supervisor()
        thread = threading.Thread(target=spawn_unrelated)
        thread.start()
        try:
            row, err = gate.run(repo=self.repo, timeout=2)
            thread.join(timeout=2)
            self.assertIsNone(err, err)
            _assert_gate_status(self, row, "OK")
            self.assertEqual(len(unrelated), 1)
            proc, starttime = unrelated[0]
            self.assertTrue(seats._is_pid_alive(
                proc.pid, starttime, require_starttime=True))
        finally:
            thread.join(timeout=2)
            for proc, _starttime in unrelated:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate(timeout=5)

    def test_gate_guard_never_kills_a_preexisting_helpers_orphan(self):  # noqa: VACUOUS_ASSERTION — the preexisting helper writes one exact orphan generation before exit, then that same generation must survive gate cleanup
        started_path = os.path.join(self.tmp, "guard-helper-started")
        child_path = os.path.join(self.tmp, "guard-helper-child")
        self.fixture_suite("""import time
import unittest

class GuardHelperFixture(unittest.TestCase):
    def test_waits(self):
        open(%r, "w").close()
        time.sleep(0.5)
""" % started_path)
        helper_code = """import os
import subprocess
import sys
import time
while not os.path.exists(%r):
    time.sleep(0.01)
child = subprocess.Popen([sys.executable, "-c",
                          "import time; time.sleep(10)"],
                         start_new_session=True)
with open("/proc/%%d/stat" %% child.pid) as f:
    starttime = f.read().rsplit(") ", 1)[1].split()[19]
with open(%r, "w") as f:
    f.write("%%d %%s" %% (child.pid, starttime))
""" % (started_path, child_path)
        require_supervisor()
        helper = subprocess.Popen((sys.executable, "-c", helper_code))
        child = None
        try:
            row, err = gate.run(repo=self.repo, timeout=2)
            helper.communicate(timeout=5)
            self.assertIsNone(err, err)
            _assert_gate_status(self, row, "OK")
            with open(child_path) as f:
                child = tuple(map(int, f.read().split()))
            self.assertTrue(seats._is_pid_alive(
                child[0], child[1], require_starttime=True))
        finally:
            if helper.poll() is None:
                helper.kill()
            helper.communicate(timeout=5)
            if child and seats._is_pid_alive(
                    child[0], child[1], require_starttime=True):
                try:
                    os.kill(child[0], signal.SIGKILL)
                except OSError:
                    pass

    def test_descendant_walk_exceeds_the_python_recursion_limit(self):  # noqa: VACUOUS_ASSERTION — 1,103 concrete proc rows must all return in deepest-first order
        root, depth = 5000, 1103
        for offset in range(1, depth + 1):
            pid, parent = root + offset, root + offset - 1
            path = os.path.join(self.proc, str(pid))
            os.makedirs(path)
            fields = ["S", str(parent)] + ["0"] * 17 + [str(20000 + offset)]
            with open(os.path.join(path, "stat"), "w") as f:
                f.write("%d (deep child) %s\n" % (pid, " ".join(fields)))
        descendants = gatechild._descendants(root, proc_dir=self.proc)
        self.assertEqual(len(descendants), depth)
        self.assertEqual(descendants[0][0], root + depth)
        self.assertEqual(descendants[-1][0], root + 1)

    def test_periodic_validation_does_not_rewrite_queue_state(self):
        owner = self.enqueue("owner", 201, 13001)
        state, _row, err = seats.gate_queue_try_start(
            self.repo, owner["id"], pid=201, proc_dir=self.proc)
        self.assertEqual((state, err), ("START", None))
        self.alive(301, 14001)
        ok, err = seats.gate_queue_bind_child(
            self.repo, owner["id"], 301, launcher_pid=201,
            proc_dir=self.proc)
        self.assertTrue(ok, err)
        path = seats.gate_queue_path(self.repo)
        before = os.stat(path).st_mtime_ns
        ok, err = seats.gate_queue_renew(
            self.repo, owner["id"], 301, launcher_pid=201,
            proc_dir=self.proc)
        self.assertTrue(ok, err)
        self.assertEqual(os.stat(path).st_mtime_ns, before)

    def test_unrelated_repositories_use_different_queue_files(self):  # noqa: VACUOUS_ASSERTION — both real Git roots resolve to distinct concrete paths
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        subprocess.run(("git", "init", "-q", "-b", "main"), cwd=other,
                       check=True)
        self.assertNotEqual(seats.gate_queue_path(self.repo),
                            seats.gate_queue_path(other))

    def test_legacy_queue_without_terminal_outbox_upgrades_on_mutation(self):
        first = self.enqueue("legacy", 210, 15000)
        path = seats.gate_queue_path(self.repo)
        state = pk.read_json(path, {})
        state.pop("terminals")
        pk.write_json(path, state)
        second = self.enqueue("upgrade", 211, 15001)
        upgraded = pk.read_json(path, {})
        self.assertEqual(upgraded["terminals"], [])
        self.assertEqual([row["id"] for row in upgraded["positions"]],
                         [first["id"], second["id"]])

    def test_malformed_terminal_outbox_refuses_instead_of_clearing_it(self):
        self.enqueue("owner", 212, 15002)
        path = seats.gate_queue_path(self.repo)
        state = pk.read_json(path, {})
        state["terminals"] = [{"id": "lost-evidence"}]
        pk.write_json(path, state)
        self.alive(213, 15003)
        status, row, err = seats.gate_queue_enqueue(
            self.repo, "second", pid=213, proc_dir=self.proc)
        self.assertEqual(status, "UNKNOWN")
        self.assertIsNone(row)
        self.assertIn("malformed", err)
        self.assertEqual(pk.read_json(path, {})["terminals"],
                         [{"id": "lost-evidence"}],
                         "unreadable evidence was silently cleared")

    def test_queue_file_bound_to_another_repository_refuses(self):
        first = self.enqueue("first", 211, 15001)
        path = seats.gate_queue_path(self.repo)
        state = pk.read_json(path, {})
        state["repo_id"] = "/wrong/repository"
        pk.write_json(path, state)
        self.alive(212, 15002)
        status, row, err = seats.gate_queue_enqueue(
            self.repo, "second", pid=212, proc_dir=self.proc)
        self.assertEqual(status, "UNKNOWN")
        self.assertIsNone(row)
        self.assertIn("schema", err)
        self.assertEqual(first["seq"], 1)

    def test_malformed_state_and_unavailable_lock_refuse_new_starts(self):
        os.makedirs(os.path.dirname(seats.gate_queue_path(self.repo)), exist_ok=True)
        with open(seats.gate_queue_path(self.repo), "w") as f:
            f.write("not json")
        self.alive(141, 6001)
        state, row, err = seats.gate_queue_enqueue(
            self.repo, "alice", pid=141, proc_dir=self.proc)
        self.assertEqual(state, "UNKNOWN")
        self.assertIsNone(row)
        self.assertIn("unreadable", err.lower())

        os.unlink(seats.gate_queue_path(self.repo))

        class NoLock:
            f = None
            def __enter__(self):
                return self
            def __exit__(self, *_exc):
                return False

        # PATCHED WHERE THE READER LOOKS, not on the facade. gate_queue_enqueue
        # now lives in helm/seats_gate_queue.py and resolves `_flocked` from
        # ITS OWN globals, so patching the seats alias sets an attribute that
        # nothing in the call path ever reads. A patch target is a dependency
        # on module STRUCTURE, and the split moved the structure.
        from helm import seats_gate_queue
        with mock.patch.object(seats_gate_queue, "_flocked",
                               return_value=NoLock()):
            state, row, err = seats.gate_queue_enqueue(
                self.repo, "alice", pid=141, proc_dir=self.proc)
        self.assertEqual(state, "UNKNOWN")
        self.assertIsNone(row)
        self.assertIn("lock", err.lower())


class OrphanNoticeStatesItsConsequenceTest(unittest.TestCase):
    """AN ORPHAN NOTICE MUST SAY WHAT IS KNOWN, AND SAY UNKNOWN OTHERWISE.

    task/109. The notice read "GATE ORPHAN #N @seat pid=P — skipped", and
    `skipped` is a benign word for a state that is not benign: the reader
    cannot tell from it whether a receipt was minted, so they supply the
    answer they already expected. MEASURED: a gate wrapper
    earlyoom-killed while QUEUED produced exit=1, no local receipt, and that
    word; three seats reached three different conclusions about the same class
    of failure before anyone read a log.

    THE STATE IS A FLOOR UNDER PROGRESS, NOT A DESCRIPTION OF THE END. Only
    the launcher advances it and only the launcher mints, so an orphan's state
    is the last thing helm ever learned about that attempt. Four classes, and
    what each one can honestly claim:

      waiting/starting  NO SUITE PROCESS EXISTED — gatechild's supervisor
                        blocks on a one-byte barrier the launcher writes only
                        AFTER the row reaches `running` (gatechild._supervise,
                        "No suite process exists before the launcher durably
                        binds this supervisor"). So nothing ran and nothing can
                        be minted later. ONE sentence for the two, deliberately
                        and asserted below: the consequence is identical and
                        the state= token keeps the events distinct.
      running           the barrier was released. Whether the suite finished,
                        and whether the launcher minted before dying, are
                        UNKNOWN — minting and the `finishing` write are two
                        steps and death fits between them.
      finishing         the attempt is OVER, but that one path is shared by a
                        minted PASS/FAIL, a minted UNKNOWN (a receipt that
                        exists and binds nothing), an UNMINTED run, a REFUSED
                        admission and a runner error: WHICH is UNKNOWN from
                        the row.
      anything else     helm did not read a state it understands, and must say
                        so rather than defaulting into one of the answers.

    Every sentence is scoped to THE ATTEMPT. One row is one attempt; the first
    wording said "this lane has no verdict", which is an attempt-local fact
    reported lane-wide and false exactly when a lane has an earlier receipt."""

    LANE = ("one row is ONE attempt — a lane whose earlier run minted a "
            "receipt still produces this notice, so a lane-scoped claim here "
            "is false exactly when it matters")
    BENIGN = ("a benign word for a state that is not benign is the whole "
              "defect of task/109, regrown")
    # THE WORDS A FUTURE AUTHOR REACHES FOR INSTEAD OF "skipped". Every one of
    # these describes a DISPOSAL the queue never performed: an orphan row was
    # not skipped, discarded, dropped, cleared or cleaned up — helm LOST TRACK
    # of a launcher, and each of these words tells the reader a decision was
    # made where none was. Lowercase substrings on purpose, so "skipped",
    # "skipping" and "discarding" are all caught by their stem.
    # Review named five more on the exact tip — ignore/ignoring, pruned,
    # reaped, evicted, removed — and they are the same class as the first
    # thirteen: each describes a DISPOSAL the queue never performed. "reaped"
    # and "evicted" are the sharpest, because they are what a reader EXPECTS
    # a queue to do to a dead row, so they would pass review unnoticed.
    BENIGN_WORDS = ("skip", "discard", "dropp", "abandon", "cancel",
                    "ignor", "harmless", "no-op", "nothing to do",
                    "cleaned up", "cleared", "benign", "stale entry",
                    "prune", "reap", "evict", "removed", "expired",
                    "garbage", "swept", "retire", "bypass")

    def _notice(self, state, shown=None):
        posts = []
        with mock.patch.object(gate, "_gate_post",
                               lambda repo, msg, obs: posts.append(msg)):
            gate._gate_post_orphans(
                "/repo", [{"seq": 3, "holder": "s1", "pid": 42,
                           "state": state}], "observer")
        self.assertEqual(len(posts), 1, "expected exactly one orphan notice")
        notice = posts[0]
        self.assertIn("GATE ORPHAN #3 @s1 pid=42 state=%s"
                      % (state if shown is None else shown), notice,
                      "the notice is not the notice: %r" % notice)
        return notice

    def test_a_waiter_that_never_left_the_queue_says_no_receipt_exists(self):
        queued = self._notice("waiting")
        self.assertIn("NO RECEIPT EXISTS", queued,
                      "an attempt that ran nothing must say so — %r" % queued)
        self.assertIn("none can arrive later", queued,
                      "the reader must be told that waiting will not help")
        self.assertIn("neither red nor green", queued,
                      "an absent verdict is not a colour; say it is absent")

    def test_a_waiter_killed_at_the_start_barrier_says_the_same_thing(self):
        # THE COLLAPSE IS THE ASSERTION, not an oversight: a `starting` row
        # never got the one-byte barrier, so no suite process existed and the
        # CONSEQUENCE is identical to `waiting`. The header still separates
        # the two events, and that is asserted here too.
        held = self._notice("starting")
        self.assertIn("NO RECEIPT EXISTS", held,
                      "a row that died at the barrier ran nothing — %r" % held)
        from_starting = gate._orphan_consequence({"state": "starting"})
        from_waiting = gate._orphan_consequence({"state": "waiting"})
        self.assertIn("NO RECEIPT EXISTS", from_starting, from_starting)
        self.assertIn("NO RECEIPT EXISTS", from_waiting, from_waiting)
        self.assertEqual(from_starting, from_waiting,
                         "the two never-ran states must share ONE consequence")
        self.assertIn("state=starting", held,
                      "the event that produced the sentence is not shown")
        self.assertNotIn("state=waiting", held,
                         "the header lost the state, so the two events are "
                         "indistinguishable in chat")

    def test_a_waiter_that_died_while_running_says_UNKNOWN_not_an_answer(self):
        ran = self._notice("running")
        self.assertIn("UNKNOWN", ran,
                      "helm cannot know this and must say so — %r" % ran)
        self.assertIn("helm gate list", ran,
                      "the reader must be pointed at the one surface that "
                      "can answer")
        self.assertIn("ABSENT receipt answers nothing", ran,
                      "the three ledger outcomes need three reactions")
        # AND IT MUST NOT ATTRIBUTE ANY RECEIPT TO *THIS* ATTEMPT.
        # Receipts carry no queue-attempt identity, so a PRESENT
        # receipt answers about whichever run minted it — which may be a prior
        # run on the same tree. The earlier wording said "a FAILED receipt IS
        # an answer", handing a reader another run's verdict as this one's,
        # while the `waiting` sentence three lines up kept the discipline. One
        # module, two standards, and only the weaker one was reachable from a
        # death mid-run.
        self.assertIn("no queue-attempt identity", ran,
                      "the reader must be told WHY the ledger cannot answer "
                      "for this attempt, not merely to go look: %r" % ran)
        self.assertIn("answers about the run that MINTED it", ran, self.BENIGN)
        self.assertNotIn("IS an answer", ran,
                         "no receipt answers for THIS attempt while receipts "
                         "carry no attempt id: %r" % ran)
        # AND IT MUST NOT SPEND THE REVIEW VOCABULARY (row
        # 45b6d0c46161). "verdict" is a REVIEWER's polarity in the dispatch
        # ledger; this notice is about RECEIPTS, and `helm gate list` — the one
        # surface it names — prints receipts and never verdicts. The first
        # wording called a red receipt "a verdict", which tells a reader their
        # REVIEW is settled when all they hold is a failed run.
        self.assertNotIn("verdict", ran.lower(),
                         "a receipt is not a verdict, and the sentence that "
                         "says what a reader may conclude is the worst place "
                         "to blur them: %r" % ran)
        self.assertIn("UNREADABLE row is a ledger fault", ran,
                      "an unreadable row is not a missing one, and re-running "
                      "mints another instead of fixing it")
        # AND IT MUST NOT CLAIM THE NEVER-RAN ANSWER. Without this, one
        # wording covering both classes would satisfy the arm above while
        # destroying the distinction the whole fix exists to make.
        self.assertNotIn("NO RECEIPT EXISTS", ran,
                         "a running death was told no receipt exists, which "
                         "is the collapse this test forbids")

    def test_a_waiter_that_died_at_finalization_says_the_attempt_is_over(self):
        ended = self._notice("finishing")
        self.assertIn("it is OVER", ended,
                      "finalization is not mid-run; the suite already ended")
        self.assertIn("GATE FINISH", ended,
                      "a row holds `finishing` WHILE its own FINISH is being "
                      "posted, so the orphan must name that line instead of "
                      "contradicting a result already on the wall")
        self.assertIn("UNMINTED", ended,
                      "a finished run whose ledger write failed is not a "
                      "minted receipt and needs its own reaction")
        # THE FOURTH OUTCOME, AND THE ONLY ONE THAT LOOKS LIKE AN ANSWER
        # WITHOUT BEING ONE (row 45b6d0c46161: "finishing omits minted
        # UNKNOWN"). A run whose interpreter or bound could not be established
        # mints a receipt that EXISTS and prints in `helm gate list` and still
        # binds nothing. A reader who finds it, having been told only about
        # "minted PASS/FAIL", concludes the work is done. The other three
        # outcomes announce their own incompleteness; this one does not.
        self.assertIn("minted UNKNOWN", ended,
                      "a present-but-unbindable receipt reads as a green one "
                      "unless this enumeration names it: %r" % ended)
        self.assertIn("binds nothing", ended,
                      "naming minted UNKNOWN without saying it authorizes no "
                      "landing just adds a word to the list")
        self.assertIn("REFUSED", ended,
                      "admission refusal reaches finalization having run "
                      "nothing, and the row cannot tell it apart")
        self.assertIn("UNKNOWN", ended, "so it must be named UNKNOWN")
        self.assertIn("helm gate list", ended,
                      "naming an UNKNOWN without naming the surface that can "
                      "resolve it leaves the reader exactly where they were")
        self.assertNotIn("NO RECEIPT EXISTS", ended,
                         "an ended attempt may well have minted one")

    def test_a_state_helm_cannot_read_says_UNKNOWN_rather_than_guessing(self):
        unread = self._notice(None, shown="UNKNOWN")
        self.assertIn("cannot read how far this attempt got", unread,
                      "an unreadable state must announce itself — %r" % unread)
        self.assertIn("conclude nothing from this line", unread, self.BENIGN)
        self.assertIn("helm gate list", unread,
                      "even total ignorance must point at the surface that "
                      "can end it")
        self.assertNotIn("NO RECEIPT EXISTS", unread,
                         "an unknown state was given the never-ran answer, "
                         "which is a claim about a state helm never read")
        from_garbage = gate._orphan_consequence({"state": "bogus"})
        from_missing = gate._orphan_consequence({})
        self.assertIn("UNKNOWN", from_garbage, from_garbage)
        self.assertIn("UNKNOWN", from_missing, from_missing)
        self.assertEqual(from_garbage, from_missing,
                         "a garbage state and a missing one are the same "
                         "ignorance and must read the same")

    def test_every_authoritative_queue_state_has_its_own_sentence(self):  # noqa: VACUOUS_ASSERTION — the rung sees the IN-LOOP positive and calls it conditional; FOUR controls stand outside every branch and any one defeats vacuity: (a) assertTrue(authoritative) refuses an empty authority, which is the only way the loop runs zero times; (b) assertEqual(covered, authoritative) is an unconditional whole-set comparison against an IMPORTED value, not a retyped literal; (c) assertEqual(checked, len(authoritative)) forces every state to have been visited; (d) the in-loop assertIn("state=<literal>") proves each notice is that state's own before anything is asserted absent from it. Mutation-proven: adding "draining" to seats_gate_queue._GATE_STATES fails this arm; restoring passes.
        # Supplement to the FIX bound on c5f75161: _ORPHAN_CONSEQUENCE
        # hand-lists the same four states that seats_gate_queue._GATE_STATES
        # already declares, and the tests hard-coded that list a THIRD time.
        # Nothing tied the copies together, so adding a fifth valid state to
        # the authority would make this table fall through to
        # _ORPHAN_UNREADABLE — "helm cannot read how far this attempt got" —
        # which is a LIE: helm reads it fine, this table just never learned
        # the word. The unreadable sentence is for states helm CANNOT parse,
        # and quietly widening it to mean "states I forgot" destroys the one
        # distinction the whole rung exists to make.
        #
        # So the authority is IMPORTED, never retyped: add a state to
        # _GATE_STATES and this arm reddens until someone writes its sentence.
        from helm import seats_gate_queue
        authoritative = set(seats_gate_queue._GATE_STATES)
        self.assertTrue(authoritative, "the authority is empty — this arm "
                                       "would pass vacuously against it")
        covered = set(gate._ORPHAN_CONSEQUENCE)
        self.assertEqual(covered, authoritative,
                         "every state the queue can WRITE needs a sentence "
                         "here; a state with none renders as UNREADABLE, "
                         "which claims helm could not read what it read fine")
        # AND EACH SENTENCE IS ITS OWN, not the unreadable fallback smuggled
        # in under a real key — equality of key sets cannot catch that.
        checked = 0
        for state in sorted(authoritative):
            notice = self._notice(state)
            # THE POSITIVE SHARES THE LOOP BODY WITH THE ABSENCE, so neither
            # can survive an iteration that did not happen: this proves the
            # notice is THIS state's notice before asserting what it lacks.
            self.assertIn("state=%s" % state, notice,
                          "%s did not render its own notice: %r"
                          % (state, notice))
            self.assertNotIn("cannot read how far", notice,
                             "%s is an authoritative state and must not "
                             "render the unreadable sentence: %r"
                             % (state, notice))
            checked += 1
        self.assertEqual(checked, len(authoritative),
                         "the loop skipped a state it claimed to check")

    def _published(self, row):
        """THE OPERATIONAL PUBLISHER, not the list accessor (row
        02c7cddc6887). The first arms called the non-reaping reader while the
        row still existed — a path production does not take. The queue hands
        a dead row to THIS function and has already persisted its deletion,
        so whatever this line omits is omitted forever."""
        posts = []
        with mock.patch.object(gate, "_gate_post",
                               lambda repo, msg, obs: posts.append(msg)):
            gate._gate_post_orphans("/repo", [row], "obs")
        self.assertEqual(len(posts), 1, "the publisher emitted %d lines"
                                        % len(posts))
        return posts[0]

    def test_the_OPERATIONAL_publisher_carries_the_stamp_too(self):
        """BOTH SURFACES, ONE FORMATTER. The publisher ignored `announced`
        entirely while `helm gate list` rendered it, so if any queue operation
        won before someone ran the list, the stamp was deleted and the only
        notice ever emitted omitted it — unrecoverable afterwards."""
        line = self._published({"seq": 4, "holder": "s1", "pid": 42,
                                "state": "finishing",
                                "announced": "OK gate:abc1234567890def"})
        self.assertIn("state=finishing", line)
        self.assertIn("OK gate:abc1234567890def", line,
                      "the operational publisher dropped the stamp: %r" % line)
        self.assertIn("naming a minted receipt", line)
        self.assertIn("NOT proof the receipt binds", line,
                      "an OK token was presented as BINDING, which only "
                      "the binder can decide: %r" % line)

    def test_only_a_MINTED_result_may_say_do_not_re_run(self):  # noqa: VACUOUS_ASSERTION — the rung sees the assertNotIn inside the subTest loop and calls its positive conditional. Three unconditional controls stand outside every branch: the `settled` line is asserted to CONTAIN both "already decided and MINTED" and "repeats work that is done" before the loop runs, and the `bare` line is asserted to contain "no binding receipt is named" after it — so the formatter is proven to emit the strong advice for a minted result and the weak one for an unminted one regardless of the loop. Inside the loop each iteration asserts TWO positives ("NOT a settled result", "a re-run may be exactly what is needed") before the negative, so an empty tuple cannot make it vacuous. Mutation-proven: treating every stamped value as settled fails 4 subtests.
        """_finish_position stamps REFUSED, UNMINTED and UNKNOWN as
        well as OK/FAILED, and for those three a retry can be NECESSARY. The
        first version said "the attempt is OVER and re-running repeats work
        that is done" for every stamped value — materially the opposite advice
        in three of five cases, and the same blanket-claim mistake already
        cured inside _ORPHAN_CONSEQUENCE two functions away.
        """
        # A FAILED RECEIPT BINDS NOTHING. The first version
        # classified `FAILED gate:<id>` as MINTED and told the reader the
        # attempt was OVER — advice to leave a RED gate unrepeated on the
        # strength of its own redness. The binder rejects every FAILED
        # receipt, so this settles nothing about whether the work is done.
        red = self._published({"seq": 5, "holder": "s1", "pid": 42,
                               "state": "finishing",
                               "announced": "FAILED gate:def4567890abcdef"})
        self.assertIn("binds NOTHING", red)
        self.assertIn("re-run is the normal next step", red)
        self.assertNotIn("repeats work that is done", red,
                         "a RED run was reported as work already done: %r" % red)

        for value in ("REFUSED", "UNMINTED", "UNKNOWN",
                      # A MINTED UNKNOWN CARRIES A TOKEN AND STILL BINDS
                      # NOTHING, which is why this branch never consults it.
                      "UNKNOWN gate:0123456789abcdef"):
            with self.subTest(announced=value):
                line = self._published({"seq": 6, "holder": "s1", "pid": 42,
                                        "state": "finishing",
                                        "announced": value})
                self.assertIn("NOT a settled result", line,
                              "%s was rendered as settled: %r" % (value, line))
                self.assertIn("a re-run may be exactly what is needed", line)
                self.assertNotIn("repeats work that is done", line,
                                 "%s told the reader NOT to re-run, which is "
                                 "the opposite of the truth: %r" % (value, line))

        # A SUBSTRING IS NOT A TOKEN. `OK not-gate:available` contains
        # "gate:" and names no receipt; the first version called it MINTED.
        for value, why in (("OK", "no token at all"),
                           ("OK not-gate:available", "a substring, not a token"),
                           ("OK gate:zzzz", "non-hex is not a receipt id")):
            with self.subTest(announced=value, why=why):
                line = self._published({"seq": 7, "holder": "s1", "pid": 42,
                                        "state": "finishing", "announced": value})
                self.assertIn("names no canonical receipt", line,
                              "%s (%s) was read as minted: %r"
                              % (value, why, line))
                self.assertNotIn("repeats work that is done", line)

    def test_an_unstamped_row_publishes_exactly_what_it_did_before(self):
        """THE FAILURE-DIRECTION CONTROL for the publisher: a row with no
        stamp must gain no advice at all, so the stamp can only ADD."""
        line = self._published({"seq": 8, "holder": "s1", "pid": 42,
                                "state": "running"})
        self.assertIn("state=running", line)
        self.assertNotIn("re-run", line)
        self.assertNotIn("announced", line)

    def test_no_class_of_orphan_may_call_itself_skipped(self):  # noqa: VACUOUS_ASSERTION — the rung sees the IN-LOOP positive and calls it conditional; three unconditional controls stand outside every branch and any ONE of them makes vacuity impossible: (a) assertIn("gate orphan #3 @s1 pid=42", blob) paired with assertNotIn("skip", blob) on that same observable, both at top level; (b) five assertIn("state=<literal>") on the five notices; (c) assertEqual(checked, 5 * len(BENIGN_WORDS)) under a >=10 floor, which FORCES 50+ absence assertions to have executed — an empty tuple or an unrun loop reddens on the count rather than passing clean. Mutation-proven: planting the review's own synonym "discarded" fails this arm; restoring passes.
        # THE REGROWTH ARM. Every arm above asserts the PRESENCE of new
        # wording, and a notice carrying BOTH the new sentence and the old
        # benign word satisfies all of them — measured: reinserting "skipped"
        # left the three original arms green.
        queued = self._notice("waiting")
        held = self._notice("starting")
        ran = self._notice("running")
        ended = self._notice("finishing")
        unread = self._notice(None, shown="UNKNOWN")
        # EACH NAME IS PROVEN TO BE ITS OWN NOTICE before the ban is asserted
        # over it: a banned word is absent from an empty string too.
        self.assertIn("state=waiting", queued, queued)
        self.assertIn("state=starting", held, held)
        self.assertIn("state=running", ran, ran)
        self.assertIn("state=finishing", ended, ended)
        self.assertIn("state=UNKNOWN", unread, unread)
        # THE BAN IS A SET, NOT A HABIT (row 45b6d0c46161: "blacklist
        # tests miss synonyms such as 'discarded'"). The first version banned
        # `skip` per-notice and four more words only against the JOINED blob —
        # two different strengths for one rule, and a hand-typed list with no
        # floor under it. Any word a future author reaches for instead of
        # "skipped" reintroduces the exact defect while every arm stays green,
        # because the defect is BENIGNNESS, not the string "skip". So: one
        # tuple, every word against EVERY notice, and a size floor so a later
        # edit cannot quietly shrink the ban back down to what it was.
        # AN UNCONDITIONAL PAIR FIRST, on ONE observable, so this test can
        # never read clean off a loop that did not run: the positive proves
        # `blob` is the real joined text and the ban is asserted over that
        # same string, both outside every branch. The per-notice loop below
        # is the STRONGER check (it says WHICH notice regressed); this pair is
        # the one that cannot be made vacuous.
        blob = " ".join((queued, held, ran, ended, unread)).lower()
        self.assertIn("gate orphan #3 @s1 pid=42", blob,
                      "the joined text is not the notices: %r" % blob)
        self.assertNotIn("skip", blob, self.BENIGN)
        notices = (("waiting", queued), ("starting", held), ("running", ran),
                   ("finishing", ended), ("UNKNOWN", unread))
        self.assertEqual(len(notices), 5, "a notice class stopped being tested")
        self.assertGreaterEqual(len(self.BENIGN_WORDS), 10, self.BENIGN)
        checked = 0
        for state, notice in notices:
            # THE POSITIVE TRAVELS WITH THE ABSENCE, IN THE SAME BODY. A
            # banned word is absent from an empty string too, so each
            # iteration first proves it is holding a REAL notice — the two
            # assertions then vanish together or not at all.
            self.assertIn("GATE ORPHAN #3 @s1 pid=42", notice,
                          "%s: this is not a notice: %r" % (state, notice))
            for word in self.BENIGN_WORDS:
                self.assertNotIn(word, notice.lower(),
                                 "%s: %s (%r)" % (state, self.BENIGN, notice))
                checked += 1
        # THE LOOP RAN. An empty notices/BENIGN_WORDS makes every assertNotIn
        # above vacuous and the test still passes green.
        self.assertEqual(checked, 5 * len(self.BENIGN_WORDS))

    def test_the_notice_speaks_about_the_attempt_never_the_lane(self):
        queued = self._notice("waiting")
        ran = self._notice("running")
        ended = self._notice("finishing")
        self.assertIn("this attempt", queued, self.LANE)
        self.assertIn("this attempt", ran, self.LANE)
        self.assertIn("this attempt", ended, self.LANE)
        self.assertNotIn("lane", queued.lower(), self.LANE)
        self.assertNotIn("lane", ran.lower(), self.LANE)
        self.assertNotIn("lane", ended.lower(), self.LANE)

    def test_the_four_classes_do_not_share_one_sentence(self):
        """The discriminating arm — over the SENTENCE, not the notice.

        MEASURED, and the reason this arm is not the one it replaces: every
        notice carries its own `state=` token, so two whole notices differ
        even when their sentences are identical. The predecessor compared
        whole notices and could not fail — mapping `running` onto the
        never-ran sentence left it green while the distinction it existed to
        protect was gone."""
        # POSITIVE CONTROL ON THE SAME OBSERVABLES: each must BE a sentence
        # before an inequality means anything — two empty strings are equal,
        # and two error strings would differ for the wrong reason.
        queued = gate._orphan_consequence({"state": "waiting"})
        ran = gate._orphan_consequence({"state": "running"})
        ended = gate._orphan_consequence({"state": "finishing"})
        unread = gate._orphan_consequence({"state": None})
        self.assertIn("this attempt", queued, queued)
        self.assertIn("this attempt", ran, ran)
        self.assertIn("this attempt", ended, ended)
        self.assertIn("this attempt", unread, unread)
        self.assertNotEqual(queued, ran, "never-ran and running collapsed")
        self.assertNotEqual(ran, ended, "running and finalized collapsed")
        self.assertNotEqual(ended, unread, "finalized and unreadable "
                            "collapsed, so the state is decorative")
        self.assertNotEqual(queued, unread, "never-ran and unreadable "
                            "collapsed: a proven absence read as ignorance")
        self.assertEqual(len({queued, ran, ended, unread}), 4,
                         "four classes, %d sentences: %r"
                         % (len({queued, ran, ended, unread}),
                            [queued, ran, ended, unread]))


if __name__ == "__main__":
    unittest.main()


def _reap_guarded_if(node):
    """Does entering this ast.If's body IMPLY reap? THE ONE DEFINITION.

    The fourth finding on this one predicate: the arm that proved it
    correct defined its OWN copy, so it exercised a DUPLICATE and not the
    function the live scan calls — if the real one drifts, the test still
    passes. That is the same defect as the three before it, one level up: a
    check sitting ADJACENT to the thing it claims to check. Both the scan and
    its arms now call THIS.

    A TOP-LEVEL SYNTACTIC WHITELIST, DELIBERATELY (the boundary was flagged
    as non-blocking and it is worth naming rather than discovering):
    a nested valid conjunction such as `x and (reap and y)` is REJECTED even
    though it does imply reap. Refusing a safe-but-unrecognised shape costs an
    author one rewrite; accepting an unsafe one costs a killed process, so the
    conservative direction is the correct one for a guard-checker.

    Name-presence is not the question. `not reap and X` runs the body when
    reap is FALSE and `reap or X` runs it when reap is FALSE, so the test must
    carry `reap` as a BARE CONJUNCT — the whole test, or one term of an `and`
    chain — which is exactly the shape where body-entry implies reap.
    """
    test = node.test
    conjuncts = (test.values
                 if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And)
                 else [test])
    return any(isinstance(c, ast.Name) and c.id == "reap" for c in conjuncts)


def _unguarded_signals(tree):
    """(unguarded_linenos, seen_count) for _gate_signal calls under `tree`.

    HOISTED SO IT CAN BE DRIVEN ON SYNTHETIC SOURCE (finding A).
    Driving it only over the live function tests it against source that has no
    else-signal in it — so the orelse propagation, which is the subtlest line
    in the walk, was asserted by nobody. The same reason its predicate is
    shared: a scanner validated only against the code it guards is validated
    against its author's imagination.
    """
    unguarded, seen = [], 0

    def walk(node, guarded):
        nonlocal seen
        if isinstance(node, ast.If):
            inner = guarded or _reap_guarded_if(node)
            for stmt in node.body:
                walk(stmt, inner)
            # An `else` of a reap-If is NOT guarded: the body runs exactly
            # when reap is false.
            for stmt in node.orelse:
                walk(stmt, guarded)
            return
        if isinstance(node, ast.Call) and \
                getattr(node.func, "id", None) == "_gate_signal":
            seen += 1
            if not guarded:
                unguarded.append(getattr(node, "lineno", -1))
        for child in ast.iter_child_nodes(node):
            walk(child, guarded)

    walk(tree, False)
    return unguarded, seen


class GateListIsNotDestructiveTest(unittest.TestCase):
    """The FIX on 210c68d6: the DIAGNOSTIC WAS DESTRUCTIVE.

    `helm gate list` reached `_gate_partition`, whose `_gate_position_live`
    answered the liveness question by SIGKILLing stopped launchers and then
    re-reading what survived. So a READ killed processes — and worse, its
    first sweep could kill while still seeing the pid, reporting NO orphan for
    a row it had just destroyed. A reader that changes what it reads cannot be
    run twice for the same answer, which is the one thing a diagnostic is for.

    Reaping now belongs to the four queue MUTATIONS, which pass reap=True.
    Everything else gets the pure classifier BY DEFAULT, so an unaudited
    caller fails toward doing less.
    """

    def _bucket(self):
        return {"positions": [{"seq": 1, "pid": 424242, "starttime": "111",
                               "state": "queued"}]}

    def test_the_READ_path_never_signals_while_the_MUTATION_path_still_reaps(self):
        """LOAD-BEARING MUTATION: drop the `reap and` guard from the trailing
        stopped-launcher branch in `_gate_position_live`.
          python3 -m unittest tests.test_gate_fifo\\
.GateListIsNotDestructiveTest\\
.test_the_READ_path_never_signals_while_the_MUTATION_path_still_reaps
          -> AssertionError: a READ path signalled a process

        BOTH POLARITIES ON PURPOSE. An arm that only proved the read is quiet
        would pass just as well if reaping were deleted outright, which would
        leave stopped launchers holding queue slots forever.
        """
        from helm import seats_gate_queue
        sent = []
        with mock.patch.object(seats_gate_queue, "_gate_signal",
                               lambda *a, **k: sent.append(a)), \
                mock.patch.object(seats_gate_queue, "_gate_pid_state",
                                  return_value="T"):
            # MUST-HIT: the branch under test is only reachable for a STOPPED
            # launcher, so prove the fixture actually produces one.
            self.assertIn(seats_gate_queue._gate_pid_state(1, "x"), ("T", "t"))

            rows, orphaned = seats_gate_queue._gate_partition(self._bucket())
            self.assertEqual(sent, [], "a READ path signalled a process")
            # AND THE HONEST CLASSIFICATION: a stopped launcher is a LIVE
            # process holding its slot. It is not an orphan; it is stopped.
            self.assertEqual([r["seq"] for r in rows], [1])
            self.assertEqual(orphaned, [])

            seats_gate_queue._gate_partition(self._bucket(), reap=True)
            self.assertTrue(sent, "the MUTATION path stopped reaping")

    def test_every_signal_in_the_classifier_sits_behind_the_reap_guard(self):  # noqa: VACUOUS_ASSERTION — assertGreaterEqual(seen, 4) is an unconditional positive control on the SAME walk: an AST scan that found no signal calls reddens there before the emptiness claim is reached
        """THE STRUCTURAL HALF, because the behavioural arm can only reach the
        branches its fixture happens to build.

        THE FIRST VERSION OF THIS ARM WAS TOO WEAK AND THE MUTATION PROVED IT:
        it accepted a signal if ANY earlier line in the function contained
        "if reap", so dropping the guard from the LAST branch left it green
        while the behavioural arm went red. A structural claim checked by
        substring is a claim about text; this walks the AST and asks whether
        each call is genuinely INSIDE a reap-guarded If, which is the thing
        the sentence always meant.
        """
        import ast
        import inspect
        import textwrap
        from helm import seats_gate_queue
        tree = ast.parse(textwrap.dedent(
            inspect.getsource(seats_gate_queue._gate_position_live)))

        unguarded, seen = _unguarded_signals(tree)
        # MUST-HIT: an AST that found no signal at all would satisfy the
        # emptiness assertion below for the wrong reason.
        self.assertGreaterEqual(seen, 4, "the AST scan found no signal calls")
        self.assertEqual(unguarded, [],
                         "a _gate_signal call is NOT inside a reap-guarded "
                         "branch, so a READ can reach it (offsets: %s)"
                         % unguarded)

    def test_the_checker_refuses_a_negated_or_disjunctive_reap_guard(self):  # noqa: VACUOUS_ASSERTION — the ACCEPT loop runs first and asserts True on four real guards, so a predicate that returned False for everything (which would satisfy every refusal below) reddens before the refusals are reached
        """The probe, pinned — A CHECKER NEEDS ITS OWN TEST.

        The first structural arm matched a SUBSTRING and the mutation caught
        it. Its AST replacement matched the NAME `reap` anywhere in the test,
        and that broke too: `if not reap and ...` keeps the name,
        inverts the meaning, and was certified clean at seen=6 unguarded=[].

        Both failures are the same one the arm exists to catch — a check that
        looks ADJACENT to the thing it claims to check. So this drives the
        predicate directly over hand-built guards rather than over the live
        function, because the live function only ever contains the shape I
        already wrote.
        """
        import ast

        def guards(expr):
            # DRIVES THE LIVE PREDICATE, not a re-typed copy of it — that
            # duplication was the review's finding, and a test of a duplicate
            # cannot see the real one drift.
            return _reap_guarded_if(ast.parse("if %s:\n    pass" % expr).body[0])

        # ACCEPTED: body entry implies reap.
        for expr in ("reap", "reap and x", "x and reap", "reap and x and y"):
            self.assertTrue(guards(expr), "rejected a real guard: %s" % expr)
        # REFUSED: body can run while reap is FALSE.
        for expr in ("not reap", "not reap and x", "reap or x", "x or reap",
                     "not (reap and x)", "x"):
            self.assertFalse(guards(expr),
                             "certified a guard that does NOT imply reap: %s"
                             % expr)

    def test_the_scanner_flags_a_signal_in_the_ELSE_of_a_reap_guard(self):
        """Finding A: the orelse branch is the subtlest line in the
        walk and the live function has no else-signal, so nothing asserted it.
        Driven on SYNTHETIC source, where the shape can exist.

        LOAD-BEARING MUTATION: propagate `inner` instead of `guarded` into the
        orelse loop in _unguarded_signals -> this arm reddens with unguarded=[]
        while every arm over the live function stays green.
        """
        guarded_body = ast.parse(
            "if reap:\n    _gate_signal(1)\n")
        in_else = ast.parse(
            "if reap:\n    pass\nelse:\n    _gate_signal(1)\n")
        # MUST-HIT: the scanner sees the call at all, so an empty result below
        # would be a real verdict rather than a broken walk.
        self.assertEqual(_unguarded_signals(guarded_body)[1], 1)
        self.assertEqual(_unguarded_signals(guarded_body)[0], [],
                         "a signal INSIDE `if reap` was called unguarded")
        unguarded, seen = _unguarded_signals(in_else)
        self.assertEqual(seen, 1)
        self.assertTrue(unguarded,
                        "a signal in the ELSE of `if reap` runs exactly when "
                        "reap is FALSE and must be reported unguarded")

    def test_the_whole_module_census_finds_no_other_signal_path(self):
        """Finding B: a one-function scan cannot see signalling
        moved into a helper. This is the standing census asked for —
        previously it was run once by hand and the number quoted.
        """
        import inspect
        from helm import seats_gate_queue
        tree = ast.parse(inspect.getsource(seats_gate_queue))
        kills, signals = [], []
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)]:
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                name = (getattr(node.func, "id", None)
                        or getattr(node.func, "attr", None))
                if name == "kill":
                    kills.append(fn.name)
                elif name == "_gate_signal":
                    signals.append(fn.name)
        # MUST-HIT: an empty census would satisfy both set-assertions below.
        self.assertTrue(kills and signals, "the module census found nothing")
        self.assertEqual(set(kills), {"_gate_signal"},
                         "os.kill escaped the one primitive that owns it: %s"
                         % sorted(set(kills)))
        self.assertEqual(set(signals), {"_gate_position_live"},
                         "a _gate_signal call moved outside the classifier, "
                         "where the reap guard cannot reach it: %s"
                         % sorted(set(signals)))

    def test_all_four_queue_mutations_actually_pass_reap(self):
        """Finding C: reap=False is the read-safe default, and a
        mutation that FORGETS reap=True silently stops reaping stopped
        launchers, leaking queue slots forever. The positive arm passed
        reap=True itself and therefore proved nothing about the real callers.
        """
        import inspect
        from helm import seats_gate_queue
        MUTATORS = {"gate_queue_enqueue", "gate_queue_try_start",
                    "gate_queue_prepare_finish", "gate_queue_finish"}
        tree = ast.parse(inspect.getsource(seats_gate_queue))
        # EVERY CALL, NOT PER-FUNCTION EXISTENCE (the late-round finding).
        # The previous arm added a function to `reaping` the moment ONE of its
        # _gate_partition calls carried reap=True — so a SECOND, default-reap
        # call inside the same mutator slipped past and silently stopped
        # reaping on that path. Existence is not universality.
        total, reaping, offenders = 0, 0, []
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name in MUTATORS]:
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call)
                        and getattr(node.func, "id", None) == "_gate_partition"):
                    continue
                total += 1
                if any(k.arg == "reap"
                       and isinstance(k.value, ast.Constant)
                       and k.value.value is True
                       for k in node.keywords):
                    reaping += 1
                else:
                    offenders.append("%s:%d" % (fn.name, node.lineno))
        # MUST-HIT: a scan that found no calls would satisfy the equality.
        self.assertGreaterEqual(total, 4,
                                "the mutator scan found no _gate_partition "
                                "calls, so it is not looking where they are")
        self.assertEqual(offenders, [],
                         "a _gate_partition call inside a queue MUTATION does "
                         "not pass reap=True, so stopped launchers keep their "
                         "slots on that path: %s" % offenders)
        self.assertEqual(reaping, total)

    def test_every_partition_call_site_has_a_DECLARED_disposition(self):  # noqa: VACUOUS_ASSERTION — assertGreaterEqual(checked, 7) is an unconditional positive control on the SAME walk and is asserted FIRST: a scan that found no call sites reddens there before either emptiness claim is reached, and that floor already caught a wrong threshold once
        """WHOLE-OBJECT-OR-REFUSE (the integrator ruling, and the review
        diagnosis): five correct findings on one lane is the
        signature of a PER-CASE checker that adding cases cannot complete.

        Every arm before this one asked "is every case I FOUND good?", so an
        unrecognised shape passed silently and needed a new round to discover.
        This asks the complement: EVERY call site must fall into exactly one
        DECLARED disposition, and anything unclassified FAILS rather than being
        skipped. A new call site, a new mutator, or a partition call added to a
        read verb then reddens BY CONSTRUCTION.

        Writing it found four call sites I had never audited — the nested add /
        start / prepare / finish closures inside the mutators — which is the
        point: the per-case arms could not have named them.

        LOAD-BEARING MUTATION: add a _gate_partition call to any function not
        in DECLARED -> "undeclared _gate_partition call site".
        """
        import inspect
        from helm import seats_gate_queue
        TARGETS = ("_gate_partition", "_gate_rows")
        # THE DECLARATION, keyed by EXACT QUALIFIED OWNER. reaps = must pass
        # reap=True; pure = must pass no reap at all; forwards = passes the
        # caller's own reap through.
        #
        # QUALIFIED because the bare-name form was falsified: an unrelated
        # nested `def add` anywhere in the module inherited "add": "reaps" and
        # passed without ever being audited. The bare map also declared the
        # OUTER mutators, which never call _gate_partition at all — only their
        # nested closures do — so half of it matched nothing while reading as
        # coverage.
        DECLARED = {
            "_gate_rows": "forwards",
            "gate_queue_enqueue.add": "reaps",
            "gate_queue_try_start.start": "reaps",
            "gate_queue_prepare_finish.prepare": "reaps",
            "gate_queue_finish.finish": "reaps",
            "gate_queue_recover_orphans.recover": "pure",
            "gate_queue_orphans": "pure",
            "gate_queue_snapshot": "pure",
        }
        tree = ast.parse(inspect.getsource(seats_gate_queue))

        # ENUMERATE THE CALLS FIRST, independently of any owner walk. The
        # ordering IS the fix for this census's own false-green: the previous
        # version collected calls only while walking ast.FunctionDef bodies, so
        # a call inside an ASYNC def or at MODULE SCOPE never entered the map.
        # Actual sites rose 7 -> 8 while `checked` stayed 7 and both emptiness
        # claims stayed true. Counting first turns an unattributable call site
        # into a FAILURE instead of an absence.
        # AND THE ENUMERATION ITSELF WAS NAME-KEYED, which is the hole
        # the probe demanded be closed rather than patched. `getattr(
        # n.func, "id", None) in TARGETS` sees a BARE NAME CALL and nothing
        # else, so `seats_gate_queue._gate_partition(...)`, an alias
        # `f = _gate_partition; f(...)`, a handler-dict entry
        # `HANDLERS["x"](...)`, and `functools.partial(_gate_partition, ...)`
        # were not mis-judged — they were NEVER ENUMERATED. And the floor
        # control above cannot catch that, because a floor is a LOWER bound: an
        # invisible site does not reduce the count. The arm was structurally
        # incapable of detecting its own blind spot.
        #
        # THE FIX IS TO STOP ENUMERATING SPELLINGS. Every reference to a target
        # is collected — Name or Attribute, in any position — and then split:
        # a reference in CALL position is resolved as before, and a reference
        # anywhere else has ESCAPED. An escaped function can be invoked later
        # with any reap binding this file cannot see, so it is a REFUSAL rather
        # than an absence. That retires aliases, dict values, partials,
        # decorators and returns in one rule instead of four cases, and a fifth
        # spelling nobody has thought of reddens by construction.
        def target_of(node):
            """The target name this node REFERENCES, or None."""
            if isinstance(node, ast.Name) and node.id in TARGETS:
                return node.id
            if isinstance(node, ast.Attribute) and node.attr in TARGETS:
                return node.attr
            return None

        all_calls = [n for n in ast.walk(tree)
                     if isinstance(n, ast.Call) and target_of(n.func)]
        in_call_position = {id(n.func) for n in all_calls}
        all_refs = [n for n in ast.walk(tree) if target_of(n)]
        escaped = ["%s at line %d" % (target_of(n), n.lineno)
                   for n in all_refs if id(n) not in in_call_position]

        owner = {}

        def bind(node, path):
            """Innermost enclosing def wins, and async/class scopes count."""
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.ClassDef)):
                    bind(child, path + [child.name])
                    continue
                if isinstance(child, ast.Call) and target_of(child.func):
                    owner[id(child)] = ".".join(path) if path else "<module>"
                bind(child, path)

        bind(tree, [])

        # POSITIVE CONTROL, unconditional and FIRST, on the enumeration every
        # claim below reads from. Seven distinct calls today; my first floor
        # said ten, taken from a census that printed each call twice.
        self.assertGreaterEqual(len(all_calls), 7,
                                "the scan enumerated %d call sites, fewer than "
                                "the measured floor — the walk is broken, not "
                                "the code" % len(all_calls))
        unowned = ["line %d" % n.lineno for n in all_calls if id(n) not in owner]
        self.assertEqual(unowned, [],
                         "a call site the owner walk could not attribute, so "
                         "it would be judged by nobody: %s" % unowned)

        # THE ESCAPE CLAIM READS A DIFFERENT WALK, SO IT NEEDS ITS OWN FLOOR.
        # The control above guards `all_calls`; every sentence below is about
        # `all_refs`, and an emptiness assertion over a walk that found nothing
        # is satisfied by the nothing. A reference walk must see AT LEAST every
        # call it already found, so this floor cannot be met by accident.
        self.assertGreaterEqual(
            len(all_refs), len(all_calls),
            "the reference walk (%d) found fewer nodes than the call walk (%d), "
            "which is impossible unless the reference walk is broken — every "
            "call's func IS a reference" % (len(all_refs), len(all_calls)))
        self.assertGreaterEqual(
            len(all_refs), 7,
            "the reference walk found %d references to %s, below the measured "
            "floor — the walk is broken, not the code" % (len(all_refs), TARGETS))
        self.assertEqual(
            escaped, [],
            "a partition function is REFERENCED without being called here, so "
            "it can be invoked later with a reap binding this census cannot "
            "see — an alias, a dict value, a partial, a decorator or a return. "
            "The disposition of that call is undecidable from this module, so "
            "it must be refused rather than skipped: %s" % escaped)

        # WHAT BINDS `reap` AT THIS CALL, RESOLVED AGAINST THE CALLEE'S REAL
        # SIGNATURE rather than pattern-matched out of the call's keywords.
        # The late-round finding, and the third distinct hole in a row
        # from ONE root: the check inspected call-site SYNTAX, so it could only
        # ever see the spelling it was written for. `pure` tested
        # `"reap" not in kw`, and _gate_partition(bucket, "/proc", True) binds
        # reap POSITIONALLY as the third argument — keywords empty, the census
        # still reporting 7 sites and all four error lists empty, while the
        # read reaps. Binding through the signature makes positional, keyword
        # and forwarded spellings ONE answer, which is why this retires the
        # class instead of adding a fourth case to it.
        def binds_reap(node):
            """(kind, value) where kind is absent | node | unknown."""
            callee = target_of(node.func)
            fn = getattr(seats_gate_queue, callee, None) if callee else None
            if fn is None:
                return ("unknown", None)
            params = list(inspect.signature(fn).parameters)
            if "reap" not in params:
                return ("unknown", None)
            # *args or **kwargs makes the binding UNRESOLVABLE, and an
            # unresolvable binding must fail every disposition rather than
            # falling through to whichever one happens not to look at it.
            if any(isinstance(a, ast.Starred) for a in node.args):
                return ("unknown", None)
            if any(k.arg is None for k in node.keywords):
                return ("unknown", None)
            idx = params.index("reap")
            if len(node.args) > idx:
                return ("node", node.args[idx])
            for k in node.keywords:
                if k.arg == "reap":
                    return ("node", k.value)
            return ("absent", None)

        undeclared, wrong = [], []
        for node in all_calls:
            qual = owner[id(node)]
            want = DECLARED.get(qual)
            if want is None:
                undeclared.append("%s:%d" % (qual, node.lineno))
                continue
            kind, value = binds_reap(node)
            if kind == "unknown":
                ok = False                    # FAIL CLOSED, every disposition
            elif want == "reaps":
                ok = kind == "node" and isinstance(value, ast.Constant) \
                    and value.value is True
            elif want == "pure":
                ok = kind == "absent"
            else:                                     # forwards
                ok = kind == "node" and isinstance(value, ast.Name) \
                    and value.id == "reap"
            if not ok:
                wrong.append("%s:%d declared %s, binds reap=%s"
                             % (qual, node.lineno, want, kind))

        self.assertEqual(undeclared, [],
                         "undeclared _gate_partition call site — every caller "
                         "must state whether it reaps, is pure, or forwards, "
                         "and an unclassified one is a REFUSAL not a skip: %s"
                         % undeclared)
        self.assertEqual(wrong, [],
                         "a call site does not match its declared disposition: "
                         "%s" % wrong)
        # AND THE COMPLEMENT: a declaration matching no call site is a stale
        # claim of coverage. This is what the bare-name map hid — it declared
        # outer verbs that never call _gate_partition, and nothing said so.
        unused = sorted(set(DECLARED) - {owner[id(n)] for n in all_calls})
        self.assertEqual(unused, [],
                         "these dispositions are declared for call sites that "
                         "do not exist, so they assert coverage of nothing: %s"
                         % unused)
