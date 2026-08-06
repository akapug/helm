#!/usr/bin/env python3
"""The whole-suite gate FIFO: durable order outranks poll rate."""
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

from helm import chat, gate, gatechild, pk, seats


ENV_KEYS = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_ROOM",
            "HELM_CHAT_NODE_URL", "HELM_CHAT_NAME", "HELM_PROC",
            "CLAUDE_SESSION_ID",
            "CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID")


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
        # The fleet suite cap counts REAL processes off /proc; the fixture
        # proc tree (stat rows, no cmdlines) keeps admission deterministic.
        os.environ["HELM_PROC"] = self.proc
        subprocess.run(("git", "init", "-q", "-b", "main"), cwd=self.repo,
                       check=True)
        subprocess.run(("git", "config", "user.email", "fifo@test"),
                       cwd=self.repo, check=True)
        subprocess.run(("git", "config", "user.name", "fifo test"),
                       cwd=self.repo, check=True)
        with open(os.path.join(self.repo, "a"), "w") as f:
            f.write("one\n")
        subprocess.run(("git", "add", "-A"), cwd=self.repo, check=True)
        subprocess.run(("git", "commit", "-qm", "first"), cwd=self.repo,
                       check=True)

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
        env = dict(os.environ)
        env["HELM_CHAT_NAME"] = seat
        env["CODEX_SESSION_ID"] = "session-" + seat
        code = ("from helm import gate; import sys; "
                "row, err = gate.run(repo=%r, timeout=%r); "
                "print(err or row['id']); sys.exit(1 if err else 0)"
                % (self.repo, timeout))
        return subprocess.Popen((sys.executable, "-c", code), env=env,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

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
        row, err = gate.run(repo=self.repo, timeout=5)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "OK")
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

        with mock.patch.object(gate, "_GATE_LEGACY_TTL_S", 1), \
                mock.patch.object(gate, "_GATE_LEGACY_RENEW_S", 0.05), \
                mock.patch.object(gate, "_mint_result", side_effect=slow):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "OK")
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
                mock.patch.object(seats, "STRICT_CLAIM_LOCK_WAIT_S", 0):
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
        time.sleep(1.1)
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

    def test_timeout_releases_the_slot_and_records_unknown(self):
        self.fixture_suite("""import time
import unittest

class TimeoutFixture(unittest.TestCase):
    def test_hangs(self):
        time.sleep(2)
""")
        os.environ["HELM_CHAT_NAME"] = "timeout-seat"
        row, err = gate.run(repo=self.repo, timeout=0.05)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "UNKNOWN")
        self.assertIsNone(row["rc"])
        self.assertEqual(seats.gate_queue_snapshot(self.repo)[0], [])  # noqa: VACUOUS_ASSERTION — UNKNOWN receipt and FINISH line prove the slot existed
        text = [r["text"] for r in chat.read("queue-room")[0]]
        self.assertTrue(any(line.startswith("GATE FINISH")
                            and "UNKNOWN" in line for line in text))

    def test_runner_error_is_named_in_unknown_finish(self):
        os.environ["HELM_CHAT_NAME"] = "runner-error"
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

        with mock.patch.object(gate, "_GATE_RENEW_S", 0.03), \
                mock.patch.object(seats, "gate_queue_renew", side_effect=counted):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["ran"], 1, row)
        self.assertEqual(row["status"], "OK")
        self.assertGreaterEqual(len(calls), 2)
        count = len(calls)
        time.sleep(0.08)
        self.assertEqual(len(calls), count)

    def test_gatechild_wrapper_needs_no_inherited_pythonpath(self):
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
        run = subprocess.run((os.path.join(root, "bin", "helm"), "gate", "run",
                              "--repo", self.repo), cwd=self.repo, env=env,
                             text=True, capture_output=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("whole-suite", run.stdout)

    def test_custom_diagnostic_command_never_enters_the_whole_suite_fifo(self):
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
        with mock.patch.object(gate, "_gate_post", return_value=False):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "OK")
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

    def test_stopped_launcher_kills_suite_before_legacy_ttl_expires(self):  # noqa: VACUOUS_ASSERTION — an exact live suite generation dies without FIFO activity before an old runner acquires the expired compatibility claim
        suite_path = os.path.join(self.tmp, "stopped-launcher-suite")
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
        code = """from helm import gate
gate._GATE_LEGACY_TTL_S = 30
gate._GATE_LEGACY_RENEW_S = 0.1
gate._GATE_RENEW_S = 0.1
gate.run(repo=%r)
""" % self.repo
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
            os.kill(launcher.pid, signal.SIGSTOP)
            self.wait_for(lambda: seats._gate_pid_state(
                launcher.pid, seats._get_pid_starttime(launcher.pid))
                in ("T", "t"))
            # No explicit timeout: this no longer races the TTL, so the
            # default budget absorbs a starved poll loop on a co-loaded box.
            #
            # AND THE FIXTURE SLEEPS 600s, NOT 10s, WHICH IS WHAT MAKES THE
            # OBSERVED DEATH MEAN ANYTHING. @codex-2 caught this at review: with
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
        code = """import time
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
                                  "import time; time.sleep(10)"])
        from tests._gate_pid import host_child, host_self
        child_pid, starttime = host_child(child.pid)
        with open(%r, "w") as f:
            f.write("%%d %%s" %% (child_pid, starttime))
        time.sleep(10)
""" % descendant_path)
        os.environ["HELM_CHAT_NAME"] = "descendants"
        started = time.monotonic()
        row, err = gate.run(repo=self.repo, timeout=0.5)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "UNKNOWN")
        self.assertLess(time.monotonic() - started, 3)
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
                                  "import time; time.sleep(10)"],
                                 start_new_session=True)
        from tests._gate_pid import host_child, host_self
        child_pid, starttime = host_child(child.pid)
        with open(%r, "w") as f:
            f.write("%%d %%s" %% (child_pid, starttime))
        time.sleep(10)
""" % descendant_path)
        os.environ["HELM_CHAT_NAME"] = "detached-descendant"
        started = time.monotonic()
        row, err = gate.run(repo=self.repo, timeout=0.5)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "UNKNOWN")
        self.assertLess(time.monotonic() - started, 3)
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
                                  "import time; time.sleep(10)"],
                                 start_new_session=True)
        from tests._gate_pid import host_child, host_self
        child_pid, starttime = host_child(child.pid)
        with open(%r, "w") as f:
            f.write("%%d %%s" %% (child_pid, starttime))
""" % descendant_path)
        os.environ["HELM_CHAT_NAME"] = "completed-detached"
        started = time.monotonic()
        row, err = gate.run(repo=self.repo, timeout=2)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "OK")
        self.assertLess(time.monotonic() - started, 3)
        with open(descendant_path) as f:
            descendant_pid, descendant_start = map(int, f.read().split())
        self.wait_for(lambda: not seats._is_pid_alive(
            descendant_pid, descendant_start, require_starttime=True), timeout=5)
        self.assertEqual(seats.gate_queue_snapshot(self.repo)[0], [])  # noqa: VACUOUS_ASSERTION — the adopted detached generation died before a green suite result released the queue

    def test_completed_suite_quiesces_a_forking_detached_tree(self):
        descendants_path = os.path.join(self.tmp, "forking-detached-descendants")
        spawner = """import subprocess
import sys
import time
from tests._gate_pid import host_child, host_self

while True:
    child = subprocess.Popen([sys.executable, "-c",
                              "import time; time.sleep(10)"],
                             start_new_session=True)
    child_pid, starttime = host_child(child.pid)
    with open(%r, "a") as f:
        f.write("%%d %%s\\n" %% (child_pid, starttime))
        f.flush()
    time.sleep(0.002)
""" % descendants_path
        self.fixture_suite("""import os
import subprocess
import sys
import time
import unittest

class ForkingDetachedFixture(unittest.TestCase):
    def test_detaches_and_forks(self):
        subprocess.Popen([sys.executable, "-c", %r],
                         start_new_session=True)
        deadline = time.monotonic() + 2
        while not os.path.exists(%r) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(os.path.exists(%r))
""" % (spawner, descendants_path, descendants_path))
        os.environ["HELM_CHAT_NAME"] = "forking-detached"
        started = time.monotonic()
        row, err = gate.run(repo=self.repo, timeout=2)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "OK")
        self.assertLess(time.monotonic() - started, 3)
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

        thread = threading.Thread(target=spawn_unrelated)
        thread.start()
        try:
            row, err = gate.run(repo=self.repo, timeout=2)
            thread.join(timeout=2)
            self.assertIsNone(err, err)
            self.assertEqual(row["status"], "OK")
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
        helper = subprocess.Popen((sys.executable, "-c", helper_code))
        child = None
        try:
            row, err = gate.run(repo=self.repo, timeout=2)
            helper.communicate(timeout=5)
            self.assertIsNone(err, err)
            self.assertEqual(row["status"], "OK")
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


if __name__ == "__main__":
    unittest.main()
