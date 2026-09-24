#!/usr/bin/env python3
"""Durable per-attempt injection-ledger spool protocol tests."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import inject  # noqa: E402
from helm.inject import _ledger  # noqa: E402


def _row(label, padding=""):
    return {
        "v": 2,
        "ts": "2026-08-27T00:00:00Z",
        "project": None,
        "sample": {
            "encoding": "utf-8",
            "rendered_bytes": 0,
            "lane_bytes": {lane: 0 for lane in
                           ("whisper", "pinned", "jit", "reflex")},
        },
        "context": {},
        "context_sources": {},
        "silent": True,
        "label": label,
        "padding": padding,
    }


def _fd_path(fd):
    try:
        return os.path.realpath("/proc/self/fd/%d" % fd)
    except OSError:
        return ""


class DurableSpoolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-ledger-spool-")
        self.prior = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")

    def tearDown(self):
        if self.prior is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self.prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def path(self):
        return inject._ledger_path()

    @property
    def queue(self):
        return _ledger._queue_dir(self.path)

    def reset(self):
        shutil.rmtree(os.environ["HELM_HOME"], ignore_errors=True)

    def names(self):
        try:
            return sorted(os.listdir(self.queue))
        except FileNotFoundError:
            return []

    def rows(self):
        return _ledger._spool_rows(self.path)

    def append(self, label, max_bytes=inject.LEDGER_MAX, padding=""):
        attempt = _ledger._ledger_begin()
        self.assertIsNotNone(attempt)
        self.assertTrue(_ledger._ledger_finish(
            attempt, _row(label, padding=padding), max_bytes=max_bytes))

    def ready_without_commit(self, label):
        attempt = _ledger._ledger_begin()
        self.assertIsNotNone(attempt)
        with mock.patch.object(_ledger, "_commit_spool", return_value=False):
            self.assertTrue(_ledger._ledger_finish(attempt, _row(label)))
        self.assertTrue(any(name.endswith(".ready") for name in self.names()))

    def test_v1_compatibility_is_separate_from_exact_v2_v3_validation(self):
        self.assertTrue(_ledger._canonical_ledger_row({
            "v": 1, "ts": "legacy", "silent": True}))
        good = _row("exact")
        self.assertTrue(_ledger._canonical_ledger_row(good))
        wrong_total = json.loads(json.dumps(good))
        wrong_total["sample"]["rendered_bytes"] = 1
        self.assertFalse(_ledger._canonical_ledger_row(wrong_total),
                         ">= must not validate an inexact UTF-8 total")
        unknown = json.loads(json.dumps(good))
        unknown["context"] = {"harness": "unknown"}
        unknown["context_sources"] = {"harness": None}
        self.assertFalse(_ledger._canonical_ledger_row(unknown),
                         "unknown harness plus a None source is not authority")

    def test_subprocess_serialization_failure_survives_exit_and_recovers_unknown(self):
        code = """
import os
from helm.inject import _ledger

def fail(*args, **kwargs):
    raise TypeError('serialization refused')

a = _ledger._ledger_begin()
if a is None:
    os._exit(21)
_ledger.json.dumps = fail
os._exit(22 if _ledger._ledger_finish(a, {'not': 'serializable'}) else 0)
"""
        env = dict(os.environ, PYTHONPATH=os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        child = subprocess.run([sys.executable, "-c", code], env=env,
                               check=False)
        self.assertEqual(child.returncode, 0)
        self.assertEqual(len([n for n in self.names()
                              if n.endswith(".intent")]), 1)
        self.append("parent")
        self.assertEqual([row["label"] for row in self.rows()], ["parent"])
        self.assertTrue(any(".unknown." in name and name.endswith(".intent")
                            for name in self.names()))
        self.assertIs(self.rows().complete, False)
        self.assertEqual(self.rows().skipped, 2,
                         "migration and failed serialization count once each")

    def test_commit_lock_timeout_and_open_failure_leave_ready_for_later_writer(self):
        for failure in ("timeout", "open"):
            with self.subTest(failure=failure):
                self.reset()
                self.ready_without_commit("queued")
                if failure == "timeout":
                    with mock.patch.object(_ledger, "_acquire",
                                           return_value=False):
                        self.assertFalse(_ledger._commit_spool(
                            self.path, inject.LEDGER_MAX))
                else:
                    with mock.patch.object(
                            _ledger, "_open_stable_lock",
                            side_effect=PermissionError("lock open refused")):
                        self.assertFalse(_ledger._commit_spool(
                            self.path, inject.LEDGER_MAX))
                self.assertTrue(any(name.endswith(".ready")
                                    for name in self.names()))
                self.append("later")
                labels = [row["label"] for row in self.rows()]
                self.assertCountEqual(labels, ["queued", "later"])

    def _append_failure(self, failure):
        self.reset()
        self.append("seed")
        self.ready_without_commit("failed")
        real_write, real_fsync, real_close = os.write, os.fsync, os.close
        writes = {"ledger": 0}

        def short_write(fd, data):
            if _fd_path(fd) == self.path:
                writes["ledger"] += 1
                if writes["ledger"] == 1:
                    return real_write(fd, data[:max(1, len(data) // 3)])
                raise OSError("append refused after short write")
            return real_write(fd, data)

        def failed_fsync(fd):
            if _fd_path(fd) == self.path:
                real_fsync(fd)
                raise OSError("ledger fsync refused")
            return real_fsync(fd)

        def failed_close(fd):
            target = _fd_path(fd)
            flags = _ledger.fcntl.fcntl(fd, _ledger.fcntl.F_GETFL)
            result = real_close(fd)
            if target == self.path and flags & os.O_APPEND:
                raise OSError("ledger append close refused")
            return result

        patch = {
            "short": mock.patch.object(_ledger.os, "write",
                                       side_effect=short_write),
            "fsync": mock.patch.object(_ledger.os, "fsync",
                                       side_effect=failed_fsync),
            "close": mock.patch.object(_ledger.os, "close",
                                       side_effect=failed_close),
        }[failure]
        with patch:
            _ledger._commit_spool(self.path, inject.LEDGER_MAX)
        self.assertTrue(any(".inflight." in name for name in self.names()))
        self.append("later")
        labels = [row["label"] for row in self.rows()]
        self.assertEqual(labels.count("failed"), 1)
        self.assertEqual(labels.count("later"), 1)

    def test_short_write_failure_leaves_inflight_and_later_writer_continues(self):
        self._append_failure("short")

    def test_fsync_failure_leaves_inflight_without_duplicate(self):
        self._append_failure("fsync")

    def test_close_failure_leaves_inflight_without_duplicate(self):
        self._append_failure("close")

    def test_migration_unknown_ages_after_two_rotations_and_never_mints_dot_2(self):
        self.append("seed")
        migration = [name for name in self.names() if name.endswith(".migration")]
        self.assertEqual(len(migration), 1)
        self.append("large", max_bytes=100, padding="x" * 400)
        self.assertTrue(os.path.exists(self.path + ".1"))
        self.assertIn(migration[0], self.names(),
                      "the first rotation still captures the migration generation")
        self.append("second", max_bytes=100)
        self.assertNotIn(migration[0], self.names())
        self.assertIs(self.rows().complete, True)
        self.assertFalse(os.path.exists(self.path + ".2"))
        with open(self.path + ".1", "rb") as generation:
            first_one = generation.read()
        self.append("large-again", max_bytes=100, padding="y" * 400)
        self.append("third", max_bytes=100)
        self.assertFalse(os.path.exists(self.path + ".2"))
        with open(self.path + ".1", "rb") as generation:
            later_one = generation.read()
        self.assertNotEqual(later_one, first_one,
                            ".1 must be replaced, never extended into .2")

    def test_stable_lock_is_data_free_and_survives_rotations_by_inode(self):
        self.append("one")
        lock = self.path + ".lock"
        before = os.stat(lock)
        self.append("two", max_bytes=1)
        self.append("three", max_bytes=1)
        after = os.stat(lock)
        self.assertEqual((after.st_dev, after.st_ino),
                         (before.st_dev, before.st_ino))
        self.assertEqual(after.st_size, 0)

    def test_stale_inflight_never_ages_into_complete(self):  # noqa: VACUOUS_ASSERTION — the valid inflight envelope and two successful generation replacements are the positive controls for the final incomplete verdict
        self.append("seed")
        generation = _ledger._path_generation(self.path)
        attempt = "b" * 32
        inflight = os.path.join(
            self.queue, "%s.inflight.%s" % (attempt, generation))
        _ledger._write_new(inflight, _ledger._ready_bytes(attempt, _row("lost")))
        _ledger._fsync_dir(self.queue)
        fd = _ledger._open_stable_lock(self.path)
        try:
            self.assertTrue(_ledger._acquire(fd, _ledger.fcntl.LOCK_EX))
            _ledger._rotate_locked(self.path, -1)
            _ledger._rotate_locked(self.path, -1)
        finally:
            _ledger._release(fd)
        self.assertNotIn(generation, {
            _ledger._path_generation(self.path),
            _ledger._path_generation(self.path + ".1")})
        self.assertIn(os.path.basename(inflight), self.names())
        self.assertIs(self.rows().complete, False)

    def test_committed_tombstone_prevents_duplicate_across_failed_cleanup_and_rotations(self):  # noqa: VACUOUS_ASSERTION — a committed row plus retained tombstone/evidence positively controls both rotations before exact single-row recovery
        with mock.patch.object(_ledger, "_finalize_committed",
                               return_value=False):
            self.append("protected")
            tombstones = [name for name in self.names()
                          if ".committed." in name]
            self.assertEqual(len(tombstones), 1)
            self.assertTrue(any(".inflight." in name for name in self.names()))
            self.assertEqual([row["label"] for row in self.rows()].count(
                "protected"), 1)
            self.append("rotate-one", max_bytes=1)
            self.append("rotate-two", max_bytes=1)
        self.append("cleanup")
        labels = [row["label"] for row in self.rows()]
        self.assertEqual(labels.count("protected"), 0,
                         "stale evidence resurrected an aged committed row")
        self.assertFalse(any(name.startswith(tombstones[0].split(".", 1)[0])
                             for name in self.names()))

    def test_commit_never_reads_or_parses_full_generations(self):
        self.append("seed")
        real = _ledger._read_path

        def refuse_generation(candidate):
            if candidate in (self.path, self.path + ".1"):
                raise AssertionError("commit read a full ledger generation")
            return real(candidate)

        with mock.patch.object(_ledger, "_read_path",
                               side_effect=refuse_generation):
            self.append("bounded")
        self.assertIn("bounded", [row["label"] for row in self.rows()])
        self.assertFalse(any(name.endswith(".ready") for name in self.names()))

    def test_no_protocol_path_ever_calls_os_link(self):
        with mock.patch.object(_ledger.os, "link", create=True) as link:
            self.append("one")
            self.append("two", max_bytes=1)
            self.append("three", max_bytes=1)
        link.assert_not_called()

    def test_unknown_marker_failure_preserves_intent_and_does_not_block_append(self):
        self.append("seed")
        lost = _ledger._ledger_begin()
        self.assertIsNotNone(lost)
        attempt = lost.attempt
        _ledger._ledger_abort(lost)
        real = _ledger._plant_unknown

        def refuse(queue, candidate, generation, reason):
            if candidate == attempt and reason == "intent":
                raise OSError("unknown marker refused")
            return real(queue, candidate, generation, reason)

        with mock.patch.object(_ledger, "_plant_unknown", side_effect=refuse):
            self.append("unblocked")
        self.assertIn(attempt + ".intent", self.names())
        self.assertIn("unblocked", [row["label"] for row in self.rows()])
        self.append("recovery")
        self.assertNotIn(attempt + ".intent", self.names())
        self.assertTrue(any(name.startswith(attempt + ".unknown.")
                            for name in self.names()))

    def test_reader_boundary_keeps_generation_and_witness_at_one_instant(self):
        self.append("before")
        generation = _ledger._path_generation(self.path)
        witness_attempt = "a" * 32
        _ledger._plant_unknown(self.queue, witness_attempt, generation, "race")
        captured = threading.Event()
        release = threading.Event()
        result = {}
        real_pread = _ledger._pread_exact
        blocked = {"done": False}

        def pread(fd, size):
            if _fd_path(fd) in (self.path, self.path + ".1") \
                    and not blocked["done"]:
                blocked["done"] = True
                captured.set()
                self.assertTrue(release.wait(5))
            return real_pread(fd, size)

        def read_snapshot():
            result["rows"] = _ledger._spool_rows(self.path)

        with mock.patch.object(_ledger, "_pread_exact", side_effect=pread):
            reader = threading.Thread(target=read_snapshot)
            reader.start()
            self.assertTrue(captured.wait(5), "reader never crossed capture")
            self.append("rotate-one", max_bytes=1)
            self.append("rotate-two", max_bytes=1)
            self.assertFalse(any(name.startswith(witness_attempt)
                                 for name in self.names()),
                             "writer did not prune the stale witness control")
            release.set()
            reader.join(5)
        self.assertFalse(reader.is_alive())
        self.assertEqual([row["label"] for row in result["rows"]], ["before"])
        self.assertIs(result["rows"].complete, False,
                      "post-capture prune crossed the reader boundary")

    def _run_crash(self, stage):
        code = r'''
import os
from helm.inject import _ledger

path = _ledger._ledger_path()
stage = os.environ['HELM_CRASH_STAGE']
real_dir = _ledger._fsync_dir
real_rename = _ledger.os.rename
real_replace = _ledger.os.replace
real_fsync = _ledger.os.fsync

def fsync_dir(candidate):
    real_dir(candidate)
    names = os.listdir(candidate) if candidate.endswith('.queue') else []
    if stage == 'intent' and any(n.endswith('.intent') for n in names) and not any(n.endswith('.ready') for n in names):
        os._exit(71)
    if stage == 'ready' and any(n.endswith('.ready') for n in names):
        os._exit(72)

def rename(source, target):
    real_rename(source, target)
    if stage == 'inflight' and '.inflight.' in target:
        os._exit(73)

def replace(source, target):
    real_replace(source, target)
    if stage == 'rotation' and source == path and target == path + '.1':
        os._exit(75)

def fsync(fd):
    real_fsync(fd)
    try:
        target = os.path.realpath('/proc/self/fd/%d' % fd)
    except OSError:
        target = ''
    if stage == 'ledger' and target == path:
        os._exit(74)

_ledger._fsync_dir = fsync_dir
_ledger.os.rename = rename
_ledger.os.replace = replace
_ledger.os.fsync = fsync
attempt = _ledger._ledger_begin()
if attempt is None:
    os._exit(80)
row = {
    'v': 2, 'ts': '2026-08-27T00:00:00Z', 'project': None,
    'sample': {'encoding': 'utf-8', 'rendered_bytes': 0,
               'lane_bytes': {'whisper': 0, 'pinned': 0, 'jit': 0, 'reflex': 0}},
    'context': {}, 'context_sources': {}, 'silent': True,
    'label': 'crashed', 'padding': ''}
_ledger._ledger_finish(
    attempt, row, max_bytes=100 if stage == 'rotation' else _ledger.LEDGER_MAX)
os._exit(81)
'''
        env = dict(os.environ, HELM_CRASH_STAGE=stage,
                   PYTHONPATH=os.path.dirname(
                       os.path.dirname(os.path.abspath(__file__))))
        return subprocess.run([sys.executable, "-c", code], env=env,
                              check=False).returncode

    def test_every_named_crash_boundary_recovers_without_duplicate(self):
        expected = {"intent": 71, "ready": 72, "inflight": 73,
                    "ledger": 74, "rotation": 75}
        for stage, exitcode in expected.items():
            with self.subTest(stage=stage):
                self.reset()
                self.append("seed")
                if stage == "rotation":
                    with open(self.path, "ab") as ledger:
                        ledger.write((json.dumps(_row("oversized", "z" * 400)) +
                                      "\n").encode("utf-8"))
                self.assertEqual(self._run_crash(stage), exitcode)
                self.append("parent")
                labels = [row.get("label") for row in self.rows()]
                if stage == "intent":
                    self.assertNotIn("crashed", labels)
                else:
                    self.assertEqual(labels.count("crashed"), 1)
                self.assertEqual(labels.count("parent"), 1)

    def test_lock_list_and_read_unavailability_are_none_not_absence(self):
        self.append("seed")
        with mock.patch.object(_ledger, "_open_stable_lock",
                               side_effect=PermissionError("lock")):
            self.assertIs(_ledger._spool_rows(self.path).complete, None)
        real_listdir = os.listdir

        def refuse_queue(candidate):
            if candidate == self.queue:
                raise PermissionError("list")
            return real_listdir(candidate)

        with mock.patch.object(_ledger.os, "listdir", side_effect=refuse_queue):
            self.assertIs(_ledger._spool_rows(self.path).complete, None)
        real_pread = os.pread

        def refuse_read(fd, size, offset):
            if _fd_path(fd) in (self.path, self.path + ".1"):
                raise OSError("read")
            return real_pread(fd, size, offset)

        with mock.patch.object(_ledger.os, "pread", side_effect=refuse_read):
            self.assertIs(_ledger._spool_rows(self.path).complete, None)

    def test_raw_migration_and_legacy_markers_remain_readable_and_unknown(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as ledger:
            ledger.write(json.dumps({"legacy": "current"}) + "\n")
        with open(self.path + ".1", "w", encoding="utf-8") as ledger:
            ledger.write(json.dumps({"legacy": "prior"}) + "\n")
        pending = self.path + ".incomplete.pending.7"
        inode = self.path + ".incomplete.1.2.8"
        with open(pending, "wb"), open(inode, "wb"):
            pass
        with open(self.path + ".lock", "w", encoding="ascii") as lock:
            lock.write("9.10\n")
        self.append("new")
        rows = self.rows()
        self.assertEqual([row.get("legacy") for row in rows[:2]],
                         ["prior", "current"])
        self.assertEqual(rows[-1]["label"], "new")
        self.assertIs(rows.complete, False)
        self.assertFalse(os.path.exists(pending))
        self.assertFalse(os.path.exists(inode))
        self.assertEqual(os.path.getsize(self.path + ".lock"), 0)
        reasons = {name.rsplit(".", 1)[-1] for name in self.names()
                   if ".unknown." in name}
        self.assertTrue({"migration", "legacy-pending", "legacy-inode",
                         "legacy-lock"}.issubset(reasons))

    def test_failed_legacy_conversion_stays_readable_and_never_blocks_append(self):
        self.append("seed")
        legacy = self.path + ".incomplete.pending.failed"
        with open(legacy, "wb"):
            pass
        real = _ledger._plant_unknown

        def refuse(queue, attempt, generation, reason):
            if reason.startswith("legacy-"):
                raise OSError("legacy conversion refused")
            return real(queue, attempt, generation, reason)

        with mock.patch.object(_ledger, "_plant_unknown", side_effect=refuse):
            self.append("still-appended")
        self.assertTrue(os.path.exists(legacy))
        self.assertIn("still-appended", [row["label"] for row in self.rows()])
        self.assertIs(self.rows().complete, False)


class AdmissionAndSeenOwnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-ledger-admission-")
        self.prior = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")

    def tearDown(self):
        if self.prior is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self.prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_v2_and_v3_paths_hit_admission_before_any_injection_owner(self):
        for harness in (None, "claude"):
            with self.subTest(harness=harness), mock.patch.dict(
                    os.environ, ({"HELM_AGENT_HARNESS": harness}
                                 if harness else {}), clear=False), \
                    mock.patch("helm.inject._whisper._ledger_begin",
                               return_value=None) as begin, \
                    mock.patch("helm.inject._whisper._sample_context") as sample, \
                    mock.patch("helm.inject._whisper._lanes") as lanes, \
                    mock.patch("helm.inject._whisper._seen_save") as seen, \
                    mock.patch("helm.inject._whisper.reflex.fire") as reflex:
                if harness is None:
                    os.environ.pop("HELM_AGENT_HARNESS", None)
                sections = inject.gather("must not deliver", session="s", cwd="/x")
            self.assertEqual(sections, {lane: [] for lane in
                                       ("whisper", "pinned", "jit", "reflex")})
            begin.assert_called_once_with()
            sample.assert_not_called()
            lanes.assert_not_called()
            seen.assert_not_called()
            reflex.assert_not_called()

    def test_ready_failure_consumes_no_seen_reflex_coinage_council_or_greet_latch(self):  # noqa: VACUOUS_ASSERTION — four nonempty staged lanes are the unconditional positive control before every persistence owner is asserted untouched
        staged = []

        def fire(*args, **kwargs):
            staged.append(("reflex", kwargs.get("persist")))
            return [{"id": "r", "steer": "staged"}]

        def coinage(*args, **kwargs):
            staged.append(("coinage", kwargs.get("persist")))
            return "REFLEX: staged coinage", "coinage:staged"

        def council(*args, **kwargs):
            staged.append(("council", kwargs.get("persist")))
            return "REFLEX: staged council", "council:staged"

        def refuse_finish(attempt, _row):
            _ledger._ledger_abort(attempt)
            return False

        with mock.patch.object(inject, "_seen_load", return_value={
                "turn": 0, "fired": {}, "pinned": None, "nudges": {}}), \
                mock.patch("helm.inject._whisper._seen_save") as seen, \
                mock.patch("helm.inject._whisper.reflex.fire",
                           side_effect=fire), \
                mock.patch.object(inject, "_coinage", side_effect=coinage), \
                mock.patch("helm.inject._whisper._coinage_candidates",
                           return_value={"staged"}), \
                mock.patch("helm.inject._whisper._council_reach",
                           side_effect=council), \
                mock.patch("helm.inject._whisper._whisper",
                           return_value=["BRIEF: staged"]), \
                mock.patch("helm.inject._whisper._mark_greeted") as greeted, \
                mock.patch("helm.inject._whisper._ledger_finish",
                           side_effect=refuse_finish):
            got = inject.gather("staged", session="s", cwd="/x")
        self.assertEqual(got, {lane: [] for lane in
                               ("whisper", "pinned", "jit", "reflex")})
        self.assertIn(("reflex", False), staged)
        self.assertIn(("coinage", False), staged)
        self.assertIn(("council", False), staged)
        self.assertNotIn(("reflex", True), staged)
        self.assertNotIn(("coinage", True), staged)
        self.assertNotIn(("council", True), staged)
        seen.assert_not_called()
        greeted.assert_not_called()

    def test_ready_failure_suppresses_prebuilt_output_and_retains_intent(self):
        sections = {"whisper": [], "pinned": ["must not deliver"],
                    "jit": [], "reflex": []}
        comparison = (None, "prompt", [], None, None)
        mutations = {"seen": None, "reflex": None, "coinage": None,
                     "council": None, "greet": False}
        with mock.patch("helm.inject._whisper._gather_admitted",
                        return_value=(sections, _row("refused"), mutations,
                                      comparison)), \
                mock.patch.object(_ledger, "_ready_bytes",
                                  side_effect=TypeError("serialize")):
            got = inject.gather("prompt")
        self.assertEqual(got, {lane: [] for lane in
                               ("whisper", "pinned", "jit", "reflex")})
        queue = _ledger._queue_dir(inject._ledger_path())
        self.assertEqual(len([name for name in os.listdir(queue)
                              if name.endswith(".intent")]), 1)
        self.assertFalse(any(name.endswith(".ready")
                             for name in os.listdir(queue)))

    def test_seen_owner_forget_waits_for_save_and_is_the_final_actuation(self):
        from helm.inject import _ledger as ledger
        started = threading.Event()
        release = threading.Event()
        forgot = threading.Event()
        real_write = ledger.pk.write_json

        def blocked_write(path, value):
            started.set()
            self.assertTrue(release.wait(5))
            return real_write(path, value)

        state = {"turn": 1, "fired": {}, "pinned": None, "nudges": {}}
        with mock.patch.object(ledger.pk, "write_json", side_effect=blocked_write):
            saver = threading.Thread(target=ledger._seen_save, args=("s", state))
            saver.start()
            self.assertTrue(started.wait(5), "save never acquired its owner lock")

            def forget():
                self.assertTrue(ledger.forget_session("s"))
                forgot.set()

            owner = threading.Thread(target=forget)
            owner.start()
            self.assertFalse(forgot.wait(0.05),
                             "owner actuation crossed an active save")
            release.set()
            saver.join(5)
            owner.join(5)
        self.assertFalse(saver.is_alive())
        self.assertFalse(owner.is_alive())
        self.assertFalse(os.path.exists(ledger._seen_path("s")),
                         "the completed owner actuation was overwritten")


if __name__ == "__main__":
    unittest.main()
