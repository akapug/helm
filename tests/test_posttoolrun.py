"""Installed terminal output with real catchers and fake owner state only."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from helm import (actors, cli, hookrun, hooks, home, pk, posttoolrun,
                  record, seats_cli, seats_common, seats_cursor, seats_delivery,
                  seats_receipts, seats_rename, toolwhisper)


class PostToolRunTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.chdir, os.getcwd())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Relative open audit events omit dir_fd. Keep the fixture's cwd fake
        # too, including its fd-relative directory cleanup, then restore it.
        os.chdir(self.root)
        self.hits, self.order, self.specs, self.results = {}, [], [], []
        self.mode = None
        self.row = True
        self.fresh = True
        self.skip = False
        self.refuse = False
        self.cursor = {"dev": 1, "ino": 2, "off": 0}
        # REAL CURSOR NAMES IN AN ISOLATED CHAT DIR. The exclusion a cursor
        # transaction takes is keyed on the path's ROOM, so a fixture whose
        # paths cannot be parsed into a room locks nothing and proves nothing
        # about the receipt ordering these arms exist for. The env pin is what
        # keeps the room lock out of the live bus: chat_dir() is env-derived
        # and would otherwise resolve to the running fleet's RAM directory.
        self.stack_env = patch.dict(
            os.environ, {"HELM_CHAT_DIR": str(self.root)})
        self.stack_env.start()
        self.addCleanup(self.stack_env.stop)
        self.paths = [str(self.root / n) for n in
                      ("main.cursor.fake-seat-0f0f0f0f",
                       ".beacon-main.cursor.fake-seat-0f0f0f0f")]
        self.text = json.dumps({"tool_name": "Read", "session_id": "synthetic-only",
                                "cwd": str(self.root), "probe": "nonempty-λ"}, ensure_ascii=False)
        self.lp = self.root / "toolwhisper-latch.json"
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.write = os.write
        self.run_one = hookrun.run_one
        self.prepare = toolwhisper.prepare_for_pair
        self.commit = toolwhisper.commit_for_pair
        self.original_record = record._record

        def patcher(module, name, value):
            self.stack.enter_context(patch.object(module, name, value))

        @contextlib.contextmanager
        def guard(*args, **kwargs):
            self.hit("guard")
            yield False

        def cursor_commit(updates):
            self.hit("cursor_commit")
            self.cursor = dict(updates[self.paths[0]])
            if self.mode == "merge":
                pk.write_json(self.lp, {"unrelated-rule": "newer-state"})
            return True

        def actual_delivery(**kwargs):
            self.hit("deliver_branch")
            if self.mode == "delivery-crash":
                raise RuntimeError("synthetic delivery crash λ")
            if self.mode == "delivery-timeout":
                self.slow("delivery")
            return seats_delivery.deliver(**kwargs)

        def record_body(payload):
            self.hit("record_body")
            self.record_payload = payload
            if self.fresh:
                (self.root / "edit-paths.log").write_text("~/.claude/projects/fake/memory/lesson.md\n")
            if self.mode == "record-crash":
                raise RuntimeError('synthetic record crash "λ"')
            if self.mode == "record-timeout":
                self.slow("record")

        def prepare(session):
            self.hit("prepare_body")
            self.prep_latch_existed = self.lp.exists()
            if self.mode == "prepare-timeout":
                self.slow("prepare")
            if self.mode == "prepare-crash":
                raise RuntimeError("synthetic preparation crash λ")
            result = self.prepare(session)
            self.prep_latch_after = self.lp.exists()
            return result

        def commit(candidate):
            self.hit("latch")
            self.latch_wire = self.raw()
            if self.mode == "latch-failure":
                raise OSError("synthetic latch failure")
            if self.mode == "latch-timeout":
                self.slow("latch")
            return self.commit(candidate)

        def record_verb(args):
            if self.mode == "record-nonzero":
                return self.mark("nonzero:record", 7)
            return record.cmd_record(args)

        def chat(args):
            phase = posttoolrun.current().phase
            if self.mode == phase + "-nonzero":
                return self.mark("nonzero:" + phase, 7)
            # CLI itself and seats_cli are real; this synthetic chat table entry
            # does only argv forwarding, never default room discovery.
            rest = list(args)
            if "--room" in rest:
                i = rest.index("--room")
                del rest[i:i + 2]
            return seats_cli.cmd(rest[0], rest[1:])

        def scope(verb, rest):
            self.hit("scope:" + verb)
            if self.skip and verb == "chat":
                return True
            return False

        def stage(spec, payload, **kwargs):
            phase = posttoolrun.current().phase
            self.specs.append((phase, spec["args"], spec["timeout"]))
            self.order.append("stage:" + phase)
            # Assert installed budgets independently; shorten ONLY fixture timers.
            result = self.run_one(dict(spec, timeout=.03), payload, **kwargs)
            self.results.append((phase, result, dict(kwargs["outcome"])))
            return result

        for module, name, value in (
            (record, "_record", record_body),
            (record, "session_dir", lambda session: str(self.root)),
            (toolwhisper, "prepare_for_pair", prepare),
            (toolwhisper, "commit_for_pair", commit),
            (hooks, "TIMEOUT_S", 10),
            (hooks, "SPECS", ({"name": "deliver", "event": "PostToolUse", "args": "chat deliver --hook-json", "timeout": 2, "scope": "helm"},)),
            (hooks, "hook_skips_here", scope),
            (hookrun, "run_one", stage),
            (cli, "VERBS", {"record": record_verb, "chat": chat}),
            (seats_rename, "recover_seat_rename", lambda: self.mark("rename", True)),
            (seats_cli, "_payload_homing", lambda *args: self.mark("homing", ("main", None))),
            (seats_cli, "_record_posttool_delegation", lambda d: self.mark("delegation", None)),
            (actors, "grant_on_behalf", lambda *a, **k: self.mark("grant", (None, None))),
            (seats_cli, "_assert_own_seat", lambda *a, **k: self.mark("identity", ("fake-seat", "synthetic refusal" if self.refuse else None))),
            (seats_cli, "deliver_any", actual_delivery),
            (seats_delivery, "_delivery_guard", guard),
            (seats_delivery, "_warn_disagreement", lambda *a: False),
            (seats_delivery, "recover_room_rotation", lambda *a: True),
            (seats_delivery, "touch_seen", lambda *a, **k: True),
            (seats_delivery, "seat_for_session", lambda *a: "fake-seat"),
            (seats_delivery, "seat_scope", lambda *a: {"mute": [], "tracked": True}),
            (seats_delivery, "_cursor", lambda *a, **k: dict(self.cursor)),
            (seats_delivery, "_cursor_pair", lambda *a: self.paths),
            (seats_delivery, "_cursor_locks", seats_cursor._cursor_locks),
            (seats_delivery, "_tail", lambda *a: (1, 2, self.cursor["off"],
                 [({"id": "row1", "from": "fake-sender", "text": "@all delivery λ marker", "ts": "synthetic"}, 0, 9)]
                 if self.row and not self.cursor["off"] else [], None, [])),
            (seats_delivery, "deliverable", lambda *a: True),
            (seats_delivery, "_occurrence", lambda *a: "synthetic-occurrence"),
            (seats_delivery, "_commit_cursor_updates", cursor_commit),
            (seats_delivery, "record_delivery_receipt", self.receipt),
            (seats_delivery, "_clip", lambda line: line),
            (seats_delivery, "_scrub", lambda line: line),
            (seats_cursor, "_cursor_txn_closure", lambda paths: set(paths)),
            (seats_cursor, "_cursor_lock_key", str),
            (seats_cursor, "_recover_cursor_txns", lambda paths: self.hit("locked_recovery")),
            (seats_cursor, "_flocked", seats_common._flocked),
            (home, "env", lambda *a: None),
            (seats_delivery.chat, "DM_PREFIX", "dm-"),
            (seats_delivery.chat, "_dsan", str),
            (posttoolrun.os, "write", self.fdwrite),
        ):
            patcher(module, name, value)

    def hit(self, name):
        self.hits[name] = self.hits.get(name, 0) + 1
        self.order.append(name)

    def mark(self, name, value):
        self.hit(name)
        return value

    def slow(self, name):
        self.hit(name + ":sleep")
        try:
            time.sleep(.2)
        except hookrun._Timeout:
            self.hit(name + ":interrupted")
            raise
        self.hit(name + ":completed")

    def raw(self):
        return os.pread(self.output.fileno(), 65536, 0)

    def lock_paths(self):
        """The exclusion a cursor transaction actually holds: ONE lock per
        room, not one file per cursor. A file per cursor could never be
        unlinked at release (unlinking a held lock hands the next caller a
        fresh inode), so it grew the flat chat directory without bound."""
        rooms = sorted({seats_cursor.parse_cursor_path(p)["room"]
                        for p in self.paths})
        return [seats_cursor._cursor_txn_lock_path(r) for r in rooms]

    def locks_held(self):
        wanted, held = self.lock_paths(), []
        self.assertTrue(wanted, "the fixture's paths name no room to lock")
        for path in wanted:
            with open(path, "a") as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    held.append(path)
                else:
                    fcntl.flock(f, fcntl.LOCK_UN)
        return held == wanted

    def fdwrite(self, fd, data):
        if fd != 1:
            return self.write(fd, data)
        self.hit("write")
        self.attempt_flag_at_write = posttoolrun.current().attempted
        if self.mode == "write-failure":
            raise OSError("synthetic write failure")
        if self.mode == "write-zero":
            return 0
        if self.mode == "write-partial":
            if self.hits["write"] == 1:
                return self.write(fd, data[:7])
            raise OSError("synthetic partial write failure")
        if self.mode == "write-short":
            return self.write(fd, data[:31])
        return self.write(fd, data)

    def receipt(self, *args, **kwargs):
        self.hit("receipt_enter")
        self.receipt_wire = self.raw()
        self.receipt_locks = self.locks_held()
        self.receipt_document = json.loads(self.receipt_wire)
        if self.mode == "receipt-failure":
            raise OSError("synthetic receipt failure")
        self.hit("receipt_complete")

    @staticmethod
    def state():
        return (dict(os.environ), sys.stdin, sys.stdout, sys.stderr, list(sys.argv),
                os.getcwd(), signal.getsignal(signal.SIGALRM), signal.getitimer(signal.ITIMER_REAL),
                [(os.fstat(fd), fcntl.fcntl(fd, fcntl.F_GETFD)) for fd in range(3)],
                posttoolrun.current(), record._record)

    def run_pair(self, failed_stderr=None):
        before = self.state()
        self.assertEqual(before[7], (0.0, 0.0))
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryFile() as self.output:
            saved = os.dup(1)
            try:
                os.dup2(self.output.fileno(), 1)
                escaped = None
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(
                        err if failed_stderr is None else failed_stderr), \
                     patch.object(sys, "stdin", io.StringIO(self.text)):
                    try:
                        rc = posttoolrun.run()
                    except Exception as exc:
                        if failed_stderr is None:
                            raise
                        rc, escaped = None, type(exc).__name__
                wire = self.raw()
            finally:
                os.dup2(saved, 1)
                fcntl.fcntl(1, fcntl.F_SETFD, before[8][1][1])
                os.close(saved)
                try:
                    self.assertEqual(self.state(), before)
                finally:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    signal.signal(signal.SIGALRM, before[6])
                    os.environ.clear(); os.environ.update(before[0])
                    sys.stdin, sys.stdout, sys.stderr, sys.argv = before[1:5]
                    os.chdir(before[5])
        self.observed = {"case": self._testMethodName, "hits": dict(self.hits), "order": list(self.order),
                         "specs": list(self.specs), "results": list(self.results), "rc": rc,
                         "wire": wire.decode("utf-8"), "stdout": out.getvalue(), "stderr": err.getvalue(),
                         "restored_before_cleanup": True, "escaped": escaped}
        # Dataflow/must-hit first, status after: never count a dead arm as green.
        if self.hits.get("record_body"):
            self.assertEqual(self.record_payload, json.loads(self.text))
        if self.hits.get("write"):
            self.assertTrue(self.attempt_flag_at_write)
        for phase, args, budget in self.specs:
            self.assertEqual(budget, 10 if phase == "record" else 2)
            if phase == "prepare":
                self.assertEqual(args, "chat deliver --hook-json --room main")
        self.assertEqual(out.getvalue(), "")
        if failed_stderr is None:
            self.assertEqual(rc, 0)
        return wire, err.getvalue()

    def failed_stderr_control(self, closed, mode):
        self.mode = mode
        if closed:
            stream = io.StringIO()
            stream.close()
        else:
            test = self

            class FailingStderr:
                def write(self, text):
                    test.hit("stderr_write_failed")
                    raise OSError("synthetic stderr unavailable")

            stream = FailingStderr()
        wire, _ = self.run_pair(failed_stderr=stream)
        self.assertEqual(self.hits.get("record_body"), 1)
        if mode == "record-timeout":
            self.assertEqual(self.hits.get("record:interrupted"), 1)
            self.assertNotIn("record:completed", self.hits)
        if not closed:
            self.assertGreater(self.hits.get("stderr_write_failed", 0), 0)
        with self.subTest(contract="fail-open return"):
            self.assertIsNone(self.observed["escaped"])
            self.assertEqual(self.observed["rc"], 0)
        with self.subTest(contract="sibling delivery"):
            context = self.delivered(wire)
            self.assertIn("delivery λ marker", context)
            self.assertEqual(self.hits.get("prepare_body"), 1)
            self.assertEqual(self.hits.get("receipt_complete", 0),
                             0 if mode == "receipt-failure" else 1)
        if mode == "receipt-failure":
            with self.subTest(contract="no postpublication repair"):
                self.assertEqual(self.hits.get("write"), 1)
                self.assertNotIn("systemMessage", self.document(wire))
        else:
            with self.subTest(contract="one visible alarm"):
                doc = self.document(wire)
                self.assertIn("UNCHECKED", doc["systemMessage"])
                self.assertIn("record", doc["hookSpecificOutput"]["additionalContext"])

    def test_closed_stderr_record_crash_preserves_delivery(self):
        self.failed_stderr_control(True, "record-crash")

    def test_failing_stderr_record_crash_preserves_delivery(self):
        self.failed_stderr_control(False, "record-crash")

    def test_closed_stderr_record_timeout_preserves_delivery(self):
        self.failed_stderr_control(True, "record-timeout")

    def test_failing_stderr_record_timeout_preserves_delivery(self):
        self.failed_stderr_control(False, "record-timeout")

    def test_closed_stderr_postpublication_failure_is_terminal(self):
        self.failed_stderr_control(True, "receipt-failure")

    def test_failing_stderr_postpublication_failure_is_terminal(self):
        self.failed_stderr_control(False, "receipt-failure")

    def document(self, wire):
        self.assertEqual(len(wire.splitlines()), 1)
        doc = json.loads(wire)
        self.assertEqual(doc["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        return doc

    def delivered(self, wire):
        self.assertEqual(self.hits.get("record_body"), 1)
        self.assertEqual(self.hits.get("deliver_branch"), 1)
        self.assertEqual(self.hits.get("receipt_enter"), 1)
        self.assertTrue(self.receipt_locks)
        self.assertEqual(self.receipt_wire, wire)
        self.assertLess(self.order.index("cursor_commit"), self.order.index("write"))
        self.assertLess(self.order.index("write"), self.order.index("receipt_enter"))
        return self.document(wire)["hookSpecificOutput"]["additionalContext"]

    def actual_receipt_owner(self, mode):
        """Real marker owner and directory I/O, rooted only in this fixture."""
        directory = self.root / "receipts"
        directory.mkdir()
        self.marker = directory / "row" / seats_receipts._hash("synthetic-occurrence") / (seats_receipts._hash("fake-recipient", "hook-delivered") + ".json")
        self.marker_fd = None
        self.receipt_fds = []
        real_open, existing_write, real_fsync = os.open, os.write, os.fsync

        def opened(path, flags, *args, **kwargs):
            if path == self.marker.name and mode == "cancel-before-open":
                self.slow("receipt-before-open")
            fd = real_open(path, flags, *args, **kwargs)
            if str(path) == str(directory) or kwargs.get("dir_fd") is not None:
                self.receipt_fds.append(fd)
            if path == self.marker.name:
                self.marker_fd = fd
                self.hit("marker_created")
            return fd

        def wrote(fd, data):
            if fd != self.marker_fd:
                return existing_write(fd, data)
            self.hit("receipt_write")
            if mode == "cancel-before-write":
                self.slow("receipt")
            if mode == "short-write":
                self.hit("receipt_real_write")
                return self.write(fd, data[:7])
            n = self.write(fd, data)
            self.hit("receipt_real_write")
            if mode == "cancel-after-write":
                self.slow("receipt")
            return n

        def synced(fd):
            if fd == self.marker_fd and mode == "fsync-failure":
                self.hit("receipt_fsync")
                raise OSError("sensitive synthetic path/payload must not appear")
            return real_fsync(fd)

        def receipt(*args, **kwargs):
            self.hit("receipt_enter")
            self.receipt_wire = self.raw()
            self.receipt_locks = self.locks_held()
            self.receipt_document = json.loads(self.receipt_wire)
            seats_receipts.record_delivery_receipt(*args, **kwargs)
            self.hit("receipt_complete")  # returned, NOT proof a marker exists

        for module, name, value in (
            (seats_delivery, "record_delivery_receipt", receipt),
            (seats_receipts, "_root_fd", lambda **k: os.open(directory, os.O_RDONLY | os.O_DIRECTORY)),
            (seats_receipts, "_receipt_path", lambda *a: str(directory / "row")),
            (seats_receipts, "_receipt_room", lambda room: room),
            (seats_receipts, "_row_recipient", lambda *a: ("fake-recipient", "fake-incarnation")),
            (seats_receipts, "_dir_flags", lambda: os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW),
            (seats_receipts, "_occurrence_present", lambda *a: True),
            (seats_receipts.eventledger, "_flags", lambda flags: flags | os.O_CLOEXEC | os.O_NOFOLLOW),
            (os, "open", opened), (os, "write", wrote), (os, "fsync", synced),
        ):
            self.stack.enter_context(patch.object(module, name, value))

    def receipt_cleanup_assertions(self):
        opened = []
        for fd in self.receipt_fds:
            try:
                os.fstat(fd)
            except OSError:
                continue
            opened.append(fd)
        self.observed["receipt_open_fds_before_emergency_cleanup"] = opened
        self.observed["receipt_marker_exists"] = self.marker.exists()
        try:
            with self.subTest(contract="owned descriptors closed"):
                self.assertEqual(opened, [])
            with self.subTest(contract="owned partial marker removed"):
                self.assertFalse(self.marker.exists())
        finally:
            # Negative reference runs may demonstrate a leaked owned fd. Assert
            # FIRST, then close only those exact fixture fds before leaving.
            for fd in opened:
                os.close(fd)

    def test_actual_receipt_success_persists_complete_effect(self):
        self.actual_receipt_owner("success")
        wire, err = self.run_pair()
        self.assertEqual(self.hits.get("receipt_real_write"), 1)
        self.delivered(wire)
        marker = json.loads(self.marker.read_bytes())
        self.assertTrue(marker["delivered"])
        self.assertEqual(marker["occurrence"], "synthetic-occurrence")
        self.assertEqual(marker["effect"], "hook-delivered")
        for fd in self.receipt_fds:
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.assertEqual(err, "")

    def test_actual_receipt_closed_stderr_cannot_block_delivery(self):
        self.actual_receipt_owner("short-write")
        actual = seats_receipts.record_delivery_receipt
        closed = io.StringIO()
        closed.close()
        def receipt(*args, **kwargs):
            with contextlib.redirect_stderr(closed):
                return actual(*args, **kwargs)
        with patch.object(seats_receipts, "record_delivery_receipt", receipt):
            wire, err = self.run_pair()
        self.assertEqual(self.hits.get("receipt_write"), 1)
        self.delivered(wire)
        self.receipt_cleanup_assertions()
        self.assertEqual(err, "")

    def test_actual_receipt_short_write_cleans_and_reports_unknown(self):
        self.actual_receipt_owner("short-write")
        wire, err = self.run_pair()
        self.assertEqual(self.hits.get("receipt_write"), 1)
        self.assertEqual(self.hits.get("receipt_real_write"), 1)
        self.delivered(wire)
        self.receipt_cleanup_assertions()
        self.assertEqual(err, "[helm delivery receipt] receipt persistence failed; delivery UNKNOWN\n")
        self.assertEqual(self.hits["write"], 1)

    def test_actual_receipt_fsync_failure_is_sanitized(self):
        self.actual_receipt_owner("fsync-failure")
        wire, err = self.run_pair()
        self.assertEqual(self.hits.get("receipt_fsync"), 1)
        self.delivered(wire)
        self.receipt_cleanup_assertions()
        self.assertEqual(err, "[helm delivery receipt] receipt persistence failed; delivery UNKNOWN\n")
        self.assertNotIn("sensitive", err)

    def receipt_cancellation(self, mode):
        self.actual_receipt_owner(mode)
        wire, err = self.run_pair()
        self.assertEqual(self.hits.get("marker_created"), 1)
        self.assertEqual(self.hits.get("receipt_write"), 1)
        self.assertEqual(self.hits.get("receipt:interrupted"), 1)
        self.assertEqual(self.hits.get("receipt_real_write", 0), int(mode == "cancel-after-write"))
        self.delivered(wire)
        self.receipt_cleanup_assertions()
        self.assertEqual(self.hits["write"], 1)
        self.assertEqual(self.cursor["off"], 9)
        with self.subTest(contract="cancellation propagates unchecked"):
            self.assertEqual(self.results[-1][2]["status"], "unchecked")
        with self.subTest(contract="cancellation diagnostic"):
            self.assertIn("TIMED OUT", err)

    def test_actual_receipt_cancellation_before_write_cleans_owned_marker(self):
        self.receipt_cancellation("cancel-before-write")

    def test_actual_receipt_cancellation_after_write_cleans_owned_marker(self):
        self.receipt_cancellation("cancel-after-write")

    def test_cancellation_before_exclusive_open_preserves_foreign_marker(self):
        self.actual_receipt_owner("cancel-before-open")
        self.marker.parent.mkdir(parents=True)
        self.marker.write_bytes(b"preexisting receipt")
        wire, err = self.run_pair()
        self.assertEqual(self.hits.get("receipt-before-open:interrupted"), 1)
        self.assertNotIn("marker_created", self.hits)
        self.delivered(wire)
        self.assertEqual(self.marker.read_bytes(), b"preexisting receipt")
        self.assertIn("TIMED OUT", err)

    def test_existing_receipt_marker_is_preserved_without_diagnostic(self):
        self.actual_receipt_owner("existing")
        self.marker.parent.mkdir(parents=True)
        self.marker.write_bytes(b"preexisting receipt")
        wire, err = self.run_pair()
        self.assertNotIn("marker_created", self.hits)
        self.delivered(wire)
        self.assertEqual(self.marker.read_bytes(), b"preexisting receipt")
        self.assertEqual(err, "")

    def test_complete_publication_precedes_receipt_under_existing_locks(self):
        self.mode = "write-short"
        wire, err = self.run_pair()
        context = self.delivered(wire)
        self.assertIn("delivery λ marker", context)
        self.assertIn("helm store add", context)
        self.assertGreater(self.hits["write"], 1)
        self.assertEqual(self.hits["latch"], 1)
        self.assertLess(self.order.index("receipt_complete"), self.order.index("latch"))
        self.assertEqual(self.latch_wire, wire)
        self.assertFalse(self.prep_latch_existed)
        self.assertFalse(self.prep_latch_after)
        self.assertEqual(err, "")

    def test_current_latch_merge_preserves_unrelated_rule(self):
        self.mode = "merge"
        wire, _ = self.run_pair()
        self.delivered(wire)
        latch = pk.read_json(self.lp)
        self.assertEqual(latch["unrelated-rule"], "newer-state")
        self.assertTrue(latch["memoryhole-write"])

    def test_actual_record_crash_combines_alarm_whisper_and_delivery(self):
        self.mode = "record-crash"
        wire, err = self.run_pair()
        context = self.delivered(wire)
        self.assertIn('synthetic record crash "λ"', context)
        self.assertIn("helm store add", context)
        self.assertIn("delivery λ marker", context)
        self.assertIn("UNCHECKED", self.document(wire)["systemMessage"])
        self.assertIn("RuntimeError", err)

    def test_record_timeout_actual_catcher_still_allows_delivery(self):
        self.mode = "record-timeout"
        wire, _ = self.run_pair()
        self.assertEqual(self.hits.get("record:interrupted"), 1)
        self.delivered(wire)
        self.assertIn("record", self.document(wire)["systemMessage"])

    def test_hanging_preparation_does_not_starve_actual_delivery(self):
        self.mode = "prepare-timeout"
        wire, _ = self.run_pair()
        self.assertEqual(self.hits.get("prepare:interrupted"), 1)
        self.assertEqual(self.hits.get("rename"), 1)
        context = self.delivered(wire)
        self.assertIn("prepare", self.document(wire)["systemMessage"])
        self.assertNotIn("helm store add", context)
        self.assertFalse(self.lp.exists())

    def test_preparation_crash_still_allows_actual_delivery(self):
        self.mode = "prepare-crash"
        wire, _ = self.run_pair()
        self.assertEqual(self.hits.get("prepare_body"), 1)
        self.delivered(wire)
        self.assertIn("synthetic preparation crash λ", self.document(wire)["systemMessage"])

    def test_delivery_crash_actual_catcher_is_visible(self):
        self.mode = "delivery-crash"
        wire, _ = self.run_pair()
        self.assertEqual(self.hits.get("deliver_branch"), 1)
        self.assertNotIn("receipt_enter", self.hits)
        self.assertIn("synthetic delivery crash λ", self.document(wire)["systemMessage"])
        self.assertFalse(self.lp.exists())

    def test_delivery_timeout_actual_catcher_is_visible(self):
        self.mode = "delivery-timeout"
        wire, _ = self.run_pair()
        self.assertEqual(self.hits.get("delivery:interrupted"), 1)
        self.assertNotIn("receipt_enter", self.hits)
        self.assertIn("delivery", self.document(wire)["systemMessage"])

    def test_nonzero_each_stage_is_visible(self):
        for phase in ("record", "prepare", "delivery"):
            with self.subTest(phase=phase):
                self.mode = phase + "-nonzero"
                wire, _ = self.run_pair()
                self.assertEqual(self.hits.get("nonzero:" + phase), 1)
                self.assertIn("rc 7", self.document(wire)["systemMessage"])
                self.assertIn(phase, self.document(wire)["systemMessage"])

    def test_registry_loss_preserves_survivor(self):
        with patch.object(posttoolrun, "_record_spec", side_effect=ImportError("synthetic registry")):
            wire, _ = self.run_pair()
        self.assertNotIn("record_body", self.hits)
        self.assertEqual(self.hits.get("deliver_branch"), 1)
        self.assertIn("record registry", self.document(wire)["systemMessage"])

    def test_delivery_registry_loss_still_records_and_alarms(self):
        with patch.object(posttoolrun, "_delivery_spec", side_effect=ImportError("synthetic registry")):
            wire, _ = self.run_pair()
        self.assertEqual(self.hits.get("record_body"), 1)
        self.assertNotIn("deliver_branch", self.hits)
        self.assertIn("delivery registry", self.document(wire)["systemMessage"])

    def test_cli_scope_skip_does_not_scope_recorder(self):
        self.skip = True
        wire, err = self.run_pair()
        self.assertEqual(self.hits.get("record_body"), 1)
        self.assertEqual(self.hits.get("scope:chat"), 2)
        self.assertNotIn("prepare_body", self.hits)
        self.assertNotIn("rename", self.hits)
        self.assertNotIn("deliver_branch", self.hits)
        self.assertEqual((wire, err), (b"", ""))
        self.assertFalse(self.lp.exists())

    def test_identity_refusal_does_not_prepare_deliver_or_latch(self):
        self.refuse = True
        wire, _ = self.run_pair()
        self.assertEqual(self.hits.get("identity"), 2)
        self.assertNotIn("prepare_body", self.hits)
        self.assertNotIn("deliver_branch", self.hits)
        self.assertIn("synthetic refusal", self.document(wire)["systemMessage"])
        self.assertFalse(self.lp.exists())

    def output_failure(self, mode, attempts, expected):
        self.mode = mode
        wire, err = self.run_pair()
        self.assertEqual(self.hits.get("cursor_commit"), 1)
        self.assertEqual(self.hits.get("write"), attempts)
        self.assertNotIn("receipt_enter", self.hits)
        self.assertNotIn("latch", self.hits)
        self.assertEqual(wire, expected)
        self.assertIn("UNKNOWN after stdout attempt", err)
        self.assertFalse(self.lp.exists())

    def test_failed_write_has_no_receipt_latch_or_second_output(self):
        self.output_failure("write-failure", 1, b"")

    def test_partial_write_has_no_receipt_latch_or_repair_output(self):
        self.output_failure("write-partial", 2, b'{"hookS')

    def test_zero_write_has_no_receipt_latch_or_second_output(self):
        self.output_failure("write-zero", 1, b"")

    def test_receipt_failure_is_terminal_without_replay(self):
        self.mode = "receipt-failure"
        wire, err = self.run_pair()
        self.delivered(wire)
        self.assertNotIn("receipt_complete", self.hits)
        self.assertIn("UNKNOWN after stdout attempt", err)
        self.assertEqual(self.hits.get("write"), 1)
        self.assertTrue(pk.read_json(self.lp)["memoryhole-write"])
        self.mode = None
        again, err = self.run_pair()
        self.assertEqual(again, b"")
        self.assertEqual(self.hits.get("receipt_enter"), 1)
        self.assertEqual(err, "")

    def test_latch_failure_after_receipt_does_not_emit_again(self):
        self.mode = "latch-failure"
        wire, err = self.run_pair()
        self.delivered(wire)
        self.assertEqual(self.hits.get("receipt_complete"), 1)
        self.assertEqual(self.hits.get("latch"), 1)
        self.assertEqual(self.hits.get("write"), 1)
        self.assertIn("UNKNOWN after stdout attempt", err)
        self.assertFalse(self.lp.exists())

    def test_latch_timeout_stays_inside_delivery_deadline(self):
        self.mode = "latch-timeout"
        wire, err = self.run_pair()
        self.delivered(wire)
        self.assertEqual(self.hits.get("receipt_complete"), 1)
        self.assertEqual(self.hits.get("latch:interrupted"), 1)
        self.assertEqual(self.hits.get("write"), 1)
        self.assertIn("UNKNOWN after stdout attempt", err)

    def test_already_latched_whisper_does_not_repeat(self):
        pk.write_json(self.lp, {"memoryhole-write": "already"})
        wire, err = self.run_pair()
        context = self.delivered(wire)
        self.assertNotIn("helm store add", context)
        self.assertNotIn("latch", self.hits)
        self.assertEqual(err, "")

    def test_no_output_quiet_event(self):
        self.row = self.fresh = False
        wire, err = self.run_pair()
        self.assertEqual(self.hits.get("record_body"), 1)
        self.assertEqual(self.hits.get("prepare_body"), 1)
        self.assertEqual(self.hits.get("deliver_branch"), 1)
        self.assertNotIn("write", self.hits)
        self.assertEqual((wire, err), (b"", ""))

    def test_whisper_only_publishes_before_latching(self):
        self.row = False
        wire, err = self.run_pair()
        self.assertEqual(self.hits.get("prepare_body"), 1)
        self.assertNotIn("receipt_enter", self.hits)
        self.assertEqual(self.hits.get("latch"), 1)
        self.assertEqual(self.latch_wire, wire)
        self.assertIn("helm store add", self.document(wire)["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(err, "")
