#!/usr/bin/env python3
"""The scratch reaper EVICTS to disk; it never deletes what it cannot restore.

THE INCIDENT (task/4183): the reaper's second tier removed a live seat's aged,
unheld scratchpad unit under host-memory pressure, with os.unlink and
shutil.rmtree, and kept no record of what it removed. The work came back only
from other databases. The owner's model was that RAM scratch is mirrored to
disk, so nothing in it can be lost. These arms hold that model: a pass copies
each unit to a disk archive, verifies the copy, writes one index row, and only
then removes the RAM copy.

Hermetic like tests/test_scratch.py: fixture trees, a fixture /proc, fixture
mount rows, an archive root in a tempdir and a faked disk-usage reading. No
arm reads or writes /dev/shm, a real session, or the real archive.
"""
import collections
import contextlib
import io
import json
import os
import shutil
import stat
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import scratch, scratch_evict  # noqa: E402
from tests import test_scratch as ts  # noqa: E402

Usage = collections.namedtuple("Usage", "total used free")
REAL_SAME_FS = scratch_evict._same_fs
ROOMY = Usage(10 ** 12, 10 ** 11, 9 * 10 ** 11)
FULL = Usage(10 ** 12, 10 ** 12 - 1, 1)


def index_rows(root, event=None):
    path = os.path.join(root, scratch_evict.INDEX)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        rows = [json.loads(line) for line in f if line.strip()]
    return [r for r in rows if event is None or r.get("event") == event]


def bounded(test, fn, seconds=30):
    """Run fn in a daemon thread and fail, never hang, if it blocks: the arms
    that hand the pass a FIFO must not be able to wedge the suite."""
    box = {}

    def run():
        try:
            box["out"] = fn()
        except BaseException as exc:     # carried back to the test thread
            box["exc"] = exc
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    test.assertFalse(t.is_alive(), "the call blocked for %ds" % seconds)
    if "exc" in box:
        raise box["exc"]
    return box["out"]


def tree_bytes(path):
    out = {}
    for base, _dirs, files in os.walk(path):
        for name in files:
            full = os.path.join(base, name)
            with open(full, "rb") as f:
                out[os.path.relpath(full, path)] = f.read()
    return out


class _Archive(object):
    """The shared part: an archive root in the fixture's tempdir, a roomy
    disk by default, and the RAM case (a unit on another filesystem than the
    archive) unless an arm asks for the same-filesystem move."""

    def archive_setup(self, cross_fs=True):
        self.archive = os.path.join(self.tmp, "archive")
        prior = {k: os.environ.get(k) for k in (
            "HELM_SCRATCH_ARCHIVE", "HELM_SCRATCH_EVICT_PASS_BYTES",
            "HELM_SCRATCH_ARCHIVE_FREE_PCT")}

        def back():
            for k, v in prior.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(back)
        for k in prior:
            os.environ.pop(k, None)
        os.environ["HELM_SCRATCH_ARCHIVE"] = self.archive
        self.usage = mock.patch.object(scratch_evict, "_disk_usage",
                                       return_value=ROOMY)
        self.usage.start()
        self.addCleanup(self.usage.stop)
        p = mock.patch.object(scratch_evict, "_same_fs",
                              side_effect=None if cross_fs else REAL_SAME_FS,
                              return_value=False)
        p.start()
        self.addCleanup(p.stop)
        self.fstype = mock.patch.object(scratch_evict, "_volatile_fs",
                                        return_value=None)
        self.fstype.start()
        self.addCleanup(self.fstype.stop)

    def archived(self):
        return index_rows(self.archive, "evicted")


class EvictTierOneTest(_Archive, unittest.TestCase):
    """Dead-session trees (tier one) under mount pressure."""

    LIVE, DEAD, DEAD2 = ts.GcTest.LIVE, ts.GcTest.DEAD, ts.GcTest.DEAD2
    tree = ts.GcTest.tree
    gc = ts.GcTest.gc
    live_proc = ts.GcTest.live_proc
    tearDown = ts.GcTest.tearDown

    def setUp(self):
        ts.GcTest.setUp(self)
        self.archive_setup()

    def test_a_pressured_pass_moves_the_unit_to_disk_and_indexes_it(self):
        path = self.tree(self.DEAD, files=3)
        before = tree_bytes(path)
        rep = self.gc(apply=True)
        self.assertEqual(rep["reaped"], 1)
        self.assertFalse(os.path.exists(path), "the RAM copy stayed")
        rows = self.archived()
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row["path"], path)
        self.assertEqual(row["session"], self.DEAD)
        self.assertEqual((row["files"], row["bytes"]), (3, 3))
        self.assertEqual(row["tier"], 1)
        self.assertTrue(row["reason"])
        self.assertTrue(row["ts"])
        self.assertTrue(row["archive_path"].startswith(
            os.path.join(self.archive, self.DEAD) + os.sep))
        self.assertEqual(tree_bytes(row["archive_path"]), before)
        self.assertIn(row["archive_path"], scratch.summary(rep))

    def test_a_short_copy_keeps_the_ram_copy_and_says_so(self):  # noqa: VACUOUS_ASSERTION — the RAM copy's three files and the `did not verify` reason are the positive controls; the empty index and archive dir are the claim
        path = self.tree(self.DEAD, files=3)
        real = scratch_evict._copy_file

        def short(src, dst):
            real(src, dst)
            with open(dst, "wb") as f:
                f.write(b"")              # the copy lost its byte
        with mock.patch.object(scratch_evict, "_copy_file", side_effect=short):
            rep = self.gc(apply=True)
        self.assertTrue(os.path.isdir(path), "a failed copy deleted RAM")
        self.assertEqual(sorted(tree_bytes(path)), ["f0", "f1", "f2"])
        self.assertEqual(rep["reaped"], 0)
        self.assertEqual(self.archived(), [])
        whys = [why for p, why in rep["evict_kept"] if p == path]
        self.assertTrue(whys and "did not verify" in whys[0], rep["evict_kept"])
        self.assertIn("KEPT in RAM", scratch.summary(rep))
        self.assertEqual(os.listdir(os.path.join(self.archive, self.DEAD)), [],
                         "the unverified copy was left in the archive")

    def test_a_copy_that_raises_keeps_the_ram_copy(self):
        path = self.tree(self.DEAD, files=2)
        with mock.patch.object(scratch_evict, "_copy_file",
                               side_effect=OSError(28, "No space left")):
            rep = self.gc(apply=True)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(rep["reaped"], 0)
        self.assertIn("No space left", rep["evict_kept"][0][1])

    def test_an_index_that_cannot_be_written_keeps_the_unit_in_ram(self):
        """No row, no eviction: a copy nobody can find again is a loss."""
        path = self.tree(self.DEAD, files=2)
        with mock.patch.object(scratch_evict, "record",
                               side_effect=OSError(30, "Read-only")):
            rep = self.gc(apply=True)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(sorted(tree_bytes(path)), ["f0", "f1"])
        self.assertEqual(rep["reaped"], 0)
        self.assertIn("index could not be written", rep["evict_kept"][0][1])
        self.assertEqual(os.listdir(os.path.join(self.archive, self.DEAD)), [],
                         "the unindexed copy was left in the archive")

    def test_an_archive_under_its_free_floor_keeps_the_unit_in_ram(self):  # noqa: VACUOUS_ASSERTION — the unit's presence and the `free floor` reason in the report and the audit line are the positive controls; no rmtree call is the claim
        path = self.tree(self.DEAD, files=2)
        with mock.patch.object(scratch_evict, "_disk_usage",
                               return_value=FULL), \
                mock.patch.object(shutil, "rmtree") as remove:
            rep = self.gc(apply=True)
        self.assertFalse(remove.called, "a full archive fell back to delete")
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(rep["reaped"], 0)
        self.assertEqual(self.archived(), [])
        self.assertIn("free floor", rep["evict_kept"][0][1])
        self.assertIn("free floor", scratch.summary(rep))

    def test_a_full_archive_prunes_nothing_it_holds(self):  # noqa: VACUOUS_ASSERTION — the earlier unit's bytes still on disk, the RAM tree and ARCHIVE BLOCKED in the summary are the positive controls; no pruned row is the claim
        """The pass deletes nothing, the archive included: under the floor an
        earlier archived unit stays on disk and no `pruned` row is written."""
        earlier = os.path.join(self.archive, "s", "earlier")
        os.makedirs(earlier)
        with open(os.path.join(earlier, "f"), "w") as f:
            f.write("archived work")
        scratch_evict.record(self.archive, {
            "event": "evicted", "path": "/ram/earlier", "session": "s",
            "tier": 2, "reason": "test", "bytes": 13, "files": 1,
            "archive_path": earlier, "epoch": 1000})
        path = self.tree(self.DEAD, files=2)
        with mock.patch.object(scratch_evict, "_disk_usage",
                               return_value=FULL):
            rep = self.gc(apply=True)
        self.assertEqual(tree_bytes(earlier), {"f": b"archived work"})
        self.assertEqual(index_rows(self.archive, "pruned"), [])
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(rep["reaped"], 0)
        line = scratch.summary(rep)
        self.assertIn("ARCHIVE BLOCKED", line)
        self.assertIn("free floor", line)

    def test_a_unit_too_big_for_the_room_left_does_not_block_the_archive(self):
        """Above the floor, one unit that would cross it stays in RAM, but a
        smaller one after it still fits and goes: the summary must not say
        nothing more leaves RAM."""
        big = self.tree(self.DEAD, files=5)
        small = self.tree(self.DEAD2, files=1)
        # 5% of 1000 is a 50-byte floor: 53 free takes the 1-byte unit and
        # refuses the 5-byte one.
        with mock.patch.object(scratch_evict, "_disk_usage",
                               return_value=Usage(1000, 947, 53)):
            rep = self.gc(apply=True)
        self.assertTrue(os.path.isdir(big))
        self.assertFalse(os.path.exists(small), "the unit that fits stayed")
        self.assertEqual([r["path"] for r in self.archived()], [small])
        whys = [why for p, why in rep["evict_kept"] if p == big]
        self.assertTrue(whys and "free floor" in whys[0], rep["evict_kept"])
        self.assertIsNone(rep["archive_blocked"])
        self.assertNotIn("ARCHIVE BLOCKED", scratch.summary(rep))

    def test_an_archive_on_tmpfs_keeps_the_unit_in_ram(self):  # noqa: VACUOUS_ASSERTION — the RAM tree's two files and `tmpfs` in the kept reason are the positive controls; the empty index and absent archive are the claim
        path = self.tree(self.DEAD, files=2)
        with mock.patch.object(scratch_evict, "_volatile_fs",
                               return_value="tmpfs", create=True), \
                mock.patch.object(shutil, "rmtree") as remove:
            rep = self.gc(apply=True)
        self.assertFalse(remove.called, "a RAM archive fell back to delete")
        self.assertEqual(sorted(tree_bytes(path)), ["f0", "f1"])
        self.assertEqual(rep["reaped"], 0)
        self.assertEqual(self.archived(), [])
        self.assertIn("tmpfs", rep["evict_kept"][0][1])
        self.assertIn("ARCHIVE BLOCKED", scratch.summary(rep))
        self.assertFalse(os.path.exists(self.archive),
                         "the pass wrote into a RAM archive")

    def test_a_remove_failed_row_that_cannot_be_written_does_not_crash(self):
        """The unit is archived and indexed; its RAM removal fails and so
        does the second index write. The pass reports both and returns."""
        path = self.tree(self.DEAD, files=2)
        real = scratch_evict.record
        calls = []

        def second_fails(root, row):
            calls.append(row["event"])
            if len(calls) > 1:
                raise OSError(28, "No space left")
            return real(root, row)
        with mock.patch.object(scratch_evict, "record",
                               side_effect=second_fails), \
                mock.patch.object(shutil, "rmtree",
                                  side_effect=OSError(13, "Permission")), \
                mock.patch.object(scratch, "_remove_tree",
                                  side_effect=OSError(13, "Permission")):
            rep = self.gc(apply=True)
        self.assertEqual(calls, ["evicted", "remove_failed"])
        self.assertTrue(os.path.isdir(path))
        errs = " ".join(why for _p, why in rep["errors"])
        self.assertIn("Permission", errs)
        self.assertIn("remove_failed index row could not be written", errs)

    def test_the_per_pass_byte_cap_is_honoured_biggest_first(self):
        big = self.tree(self.DEAD, files=5)
        small = self.tree(self.DEAD2, files=1)
        os.environ["HELM_SCRATCH_EVICT_PASS_BYTES"] = "1"
        rep = self.gc(apply=True)
        self.assertFalse(os.path.exists(big), "biggest-by-inodes goes first")
        self.assertTrue(os.path.isdir(small), "the cap was not honoured")
        self.assertEqual(rep["reaped"], 1)
        self.assertEqual([r["path"] for r in self.archived()], [big])
        whys = [why for p, why in rep["evict_kept"] if p == small]
        self.assertTrue(whys and "byte" in whys[0], rep["evict_kept"])

    def test_a_spent_cap_defers_only_copies_and_a_rename_still_goes(self):
        """Only a copy spends the allowance: after the biggest unit's copy
        spends it, the next unit that needs a copy is deferred and a unit on
        the archive's own filesystem is still renamed."""
        big = self.tree(self.DEAD, files=5)
        mid = self.tree("dddddddd-1111-2222-3333-444444444444", files=2)
        small = self.tree(self.DEAD2, files=1)
        os.environ["HELM_SCRATCH_EVICT_PASS_BYTES"] = "1"
        with mock.patch.object(scratch_evict, "_same_fs",
                               side_effect=lambda p, _root: p == small):
            rep = self.gc(apply=True)
        self.assertFalse(os.path.exists(big), "the copied unit stayed")
        self.assertFalse(os.path.exists(small),
                         "the spent copy cap deferred a zero-cost rename")
        self.assertTrue(os.path.isdir(mid), "a copy went past the cap")
        self.assertEqual(rep["reaped"], 2)
        moved = {r["path"]: r["moved"] for r in self.archived()}
        self.assertEqual(moved, {big: False, small: True})
        whys = [why for p, why in rep["evict_kept"] if p == mid]
        self.assertTrue(whys and "byte" in whys[0], rep["evict_kept"])

    def test_a_special_file_unit_is_refused_before_anything_opens_it(self):  # noqa: VACUOUS_ASSERTION — the `special file holds no data` and `FIFO` reason and the FIFO still on disk are the positive controls; no copy call and no archive entry are the claim
        """A FIFO handed to archive() is refused by its lstat: it holds no
        data, and a copy opens it (a FIFO blocks; a character device reads
        without end). Nothing is copied and the FIFO stays where it is."""
        fifo = os.path.join(self.tmp, "pipe")
        os.mkfifo(fifo)
        with mock.patch.object(scratch_evict, "_copy",
                               side_effect=OSError(5, "opened")) as copy:
            got = bounded(self, lambda: scratch_evict.archive(
                fifo, "s", self.archive))
        self.assertFalse(copy.called, "the special file reached the copy")
        self.assertIn("special file holds no data", got["why"] or "")
        self.assertIn("FIFO", got["why"])
        self.assertFalse(got["moved"])
        self.assertTrue(stat.S_ISFIFO(os.lstat(fifo).st_mode))
        self.assertFalse(os.path.exists(os.path.join(self.archive, "s")),
                         "the refusal still made an archive entry")

    def test_without_a_cap_hit_both_units_go(self):  # noqa: VACUOUS_ASSERTION — reaped == 2 and the two indexed paths are the positive controls
        """The control for the arm above: the same two trees, a roomy cap."""
        big = self.tree(self.DEAD, files=5)
        small = self.tree(self.DEAD2, files=1)
        rep = self.gc(apply=True)
        self.assertEqual(rep["reaped"], 2)
        self.assertEqual(sorted(r["path"] for r in self.archived()),
                         sorted([big, small]))

    def test_a_dry_run_archives_nothing(self):  # noqa: VACUOUS_ASSERTION — the tree's presence is the positive control; the empty index is the claim, and the applying arms above prove the index is written
        path = self.tree(self.DEAD)
        self.gc(apply=False)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(self.archived(), [])

    def test_restore_round_trips_the_bytes(self):  # noqa: VACUOUS_ASSERTION — the restored bytes equal to the pre-eviction bytes and the restored row are the positive controls; the absent path proves the eviction ran first
        os.makedirs(os.path.join(self.root, self.DEAD))
        with open(os.path.join(self.root, self.DEAD, "key"), "w") as f:
            f.write("the answer key")
        path = self.tree(self.DEAD, files=3)     # ages the key file too
        before = tree_bytes(path)
        self.gc(apply=True)
        self.assertFalse(os.path.exists(path))
        rc, msg = scratch_evict.restore(path)
        self.assertEqual(rc, 0, msg)
        self.assertEqual(tree_bytes(path), before)
        self.assertEqual(len(index_rows(self.archive, "restored")), 1)

    def test_restore_by_archive_path_and_refuses_to_overwrite(self):
        path = self.tree(self.DEAD, files=2)
        self.gc(apply=True)
        where = self.archived()[0]["archive_path"]
        self.assertEqual(scratch_evict.restore(where)[0], 0)
        rc, msg = scratch_evict.restore(path)
        self.assertNotEqual(rc, 0)
        self.assertIn("--force", msg)
        with open(os.path.join(path, "new"), "w") as f:
            f.write("written after the restore")
        rc, msg = scratch_evict.restore(path, force=True)
        self.assertEqual(rc, 0, msg)
        aside = [n for n in os.listdir(os.path.dirname(path))
                 if n.startswith(self.DEAD + ".pre-restore-")]
        self.assertEqual(len(aside), 1, "force must move the old copy aside")
        self.assertTrue(os.path.exists(os.path.join(
            os.path.dirname(path), aside[0], "new")))

    def _failed_restore(self, force, how):
        """Evict a unit, then restore it with a copy that half-lands and then
        raises (`raise`) or that lands short (`short`). With `force`, a newer
        copy sits at the path first. -> (path, rc, msg, copy calls)."""
        path = self.tree(self.DEAD, files=2)
        self.gc(apply=True)
        self.assertFalse(os.path.exists(path), "the eviction did not run")
        if force:
            os.makedirs(path)
            with open(os.path.join(path, "mine"), "w") as f:
                f.write("written after the eviction")
        calls = []
        real_copy, real_file = scratch_evict._copy, scratch_evict._copy_file

        def half(src, dst):
            calls.append(dst)
            real_copy(src, dst)
            raise OSError(28, "No space left")

        def short(src, dst):
            calls.append(dst)
            real_file(src, dst)
            with open(dst, "wb") as f:
                f.write(b"")
        name, fake = ("_copy", half) if how == "raise" else \
            ("_copy_file", short)
        with mock.patch.object(scratch_evict, name, side_effect=fake):
            rc, msg = scratch_evict.restore(path, force=force)
        return path, rc, msg, calls

    def assert_put_back(self, path, msg):
        self.assertEqual(tree_bytes(path),
                         {"mine": b"written after the eviction"}, msg)
        aside = [n for n in os.listdir(os.path.dirname(path))
                 if n.startswith(self.DEAD + ".pre-restore-")]
        self.assertEqual(aside, [], "the original was left aside: " + msg)
        self.assertIn("original was put back", msg)

    def test_a_restore_whose_copy_raises_leaves_no_partial_copy(self):
        path, rc, msg, calls = self._failed_restore(False, "raise")
        self.assertTrue(calls, "the copy never ran")
        self.assertEqual(rc, 1)
        self.assertIn("No space left", msg)
        self.assertFalse(os.path.lexists(path), "a partial copy was left")
        self.assertIn("partial copy was removed", msg)

    def test_a_restore_that_does_not_verify_leaves_no_partial_copy(self):
        path, rc, msg, calls = self._failed_restore(False, "short")
        self.assertTrue(calls, "the copy never ran")
        self.assertEqual(rc, 1)
        self.assertIn("does not match", msg)
        self.assertFalse(os.path.lexists(path), "a partial copy was left")

    def test_a_forced_restore_whose_copy_raises_puts_the_original_back(self):
        path, rc, msg, calls = self._failed_restore(True, "raise")
        self.assertTrue(calls, "the copy never ran")
        self.assertEqual(rc, 1)
        self.assertIn("No space left", msg)
        self.assert_put_back(path, msg)

    def test_a_forced_restore_that_does_not_verify_puts_the_original_back(self):
        path, rc, msg, calls = self._failed_restore(True, "short")
        self.assertTrue(calls, "the copy never ran")
        self.assertEqual(rc, 1)
        self.assertIn("does not match", msg)
        self.assert_put_back(path, msg)

    def test_restore_of_an_unknown_path_refuses(self):
        rc, msg = scratch_evict.restore(os.path.join(self.tmp, "never"))
        self.assertNotEqual(rc, 0)
        self.assertIn("no archived copy", msg)


class EvictSameFilesystemTest(_Archive, unittest.TestCase):
    """A unit already on the archive's filesystem is MOVED: a copy there
    would double its bytes on the very disk the pass is relieving."""

    DEAD = ts.GcTest.DEAD
    tree = ts.GcTest.tree
    gc = ts.GcTest.gc
    tearDown = ts.GcTest.tearDown

    def setUp(self):
        ts.GcTest.setUp(self)
        self.archive_setup(cross_fs=False)

    def test_a_same_filesystem_tmpfs_archive_renames_nothing(self):  # noqa: VACUOUS_ASSERTION — the RAM tree's two files and `tmpfs` in the kept reason are the positive controls; the empty index is the claim
        """The rename a same-filesystem unit gets would leave every byte in
        RAM when that filesystem is tmpfs: refused, and the unit is kept."""
        path = self.tree(self.DEAD, files=2)
        with mock.patch.object(scratch_evict, "_volatile_fs",
                               return_value="tmpfs", create=True):
            rep = self.gc(apply=True)
        self.assertEqual(rep["reaped"], 0)
        self.assertEqual(sorted(tree_bytes(path)), ["f0", "f1"])
        self.assertEqual(self.archived(), [])
        self.assertIn("tmpfs", rep["evict_kept"][0][1])

    def test_a_same_filesystem_unit_is_renamed_not_copied(self):  # noqa: VACUOUS_ASSERTION — reaped == 1 and the archived bytes equal to the unit's are the positive controls; no copy call is the claim
        path = self.tree(self.DEAD, files=2)
        before = tree_bytes(path)
        with mock.patch.object(scratch_evict, "_copy_file") as copy:
            rep = self.gc(apply=True)
        self.assertFalse(copy.called)
        self.assertEqual(rep["reaped"], 1)
        self.assertFalse(os.path.exists(path))
        row = self.archived()[0]
        self.assertEqual(tree_bytes(row["archive_path"]), before)

    def test_a_failed_rename_is_never_listed_as_archived(self):  # noqa: VACUOUS_ASSERTION — the one remove_failed row, the listed row with moved=True and its NOT archived state are the positive controls; the empty held() is the claim
        """The `evicted` row is written before the rename; when the rename
        then fails, the archive path holds nothing and the unit is still in
        RAM. The listing says so, and restore finds no archived copy."""
        path = self.tree(self.DEAD, files=2)
        with mock.patch.object(scratch_evict, "move",
                               side_effect=OSError(18, "cross-device")):
            rep = self.gc(apply=True)
        self.assertEqual(rep["reaped"], 0)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(len(index_rows(self.archive, "remove_failed")), 1)
        rows = [r for r in scratch_evict.listing() if r["path"] == path]
        self.assertEqual(len(rows), 1, rows)
        self.assertTrue(rows[0]["moved"])
        self.assertFalse(os.path.lexists(rows[0]["archive_path"]))
        self.assertTrue(rows[0]["state"].startswith("NOT archived"),
                        rows[0]["state"])
        self.assertEqual(scratch_evict.held(
            scratch_evict.read_index(self.archive)), [])
        rc, msg = scratch_evict.restore(path, force=True)
        self.assertEqual(rc, 1)
        self.assertIn("no archived copy", msg)


class EvictTierTwoTest(_Archive, unittest.TestCase):
    """A LIVE session's aged children (tier two) — the incident's own tier."""

    LIVE = ts.SecondTierTest.LIVE
    HOST_WARN = ts.SecondTierTest.HOST_WARN
    restore = ts.SecondTierTest.restore
    fresh = ts.SecondTierTest.fresh
    unit = ts.SecondTierTest.unit
    age = ts.SecondTierTest.age
    live_proc = ts.SecondTierTest.live_proc
    rows = ts.SecondTierTest.rows
    gc = ts.SecondTierTest.gc

    def setUp(self):
        ts.SecondTierTest.setUp(self)
        self.archive_setup()

    def test_a_live_sessions_aged_unit_is_archived_before_it_leaves_ram(self):
        stale = self.unit("reviews", files=3)
        rep = self.gc(apply=True)
        self.assertEqual(rep["tier2_reaped"], 1)
        self.assertFalse(os.path.exists(stale))
        row = self.archived()[0]
        self.assertEqual((row["path"], row["session"], row["tier"]),
                         (stale, self.LIVE, 2))
        self.assertEqual(sorted(tree_bytes(row["archive_path"])),
                         ["f0", "f1", "f2"])

    def test_a_held_unit_is_never_touched_and_never_archived(self):
        held = self.unit("held")
        free = self.unit("free")
        self.live_proc("1001", fd=os.path.join(held, "f0"))
        rep = self.gc(apply=True)
        self.assertTrue(os.path.isdir(held))
        self.assertEqual(sorted(tree_bytes(held)), ["f0", "f1", "f2"])
        self.assertFalse(os.path.exists(free), "the positive control stayed")
        self.assertEqual([r["path"] for r in self.archived()], [free])
        self.assertTrue(any("holds" in why for p, why in rep["tier2_kept"]
                            if p == held))

    def test_a_fifo_in_a_live_scratchpad_is_never_a_unit(self):  # noqa: VACUOUS_ASSERTION — the evicted sibling directory and the `FIFO holds no data` kept reason are the positive controls; no copy call, no error row and no archive row are the claim
        """A FIFO holds no data, so the tier-two listing keeps it out with
        that reason, rather than admitting it as a `file` unit that the
        deletion-time recheck then refuses as `changed kind` on every pass.
        Nothing opens it; the aged directory beside it is the positive
        control that the pass ran."""
        stale = self.unit("reviews", files=2)
        fifo = os.path.join(self.pad, "agent.pipe")
        os.mkfifo(fifo)
        old = time.time() - 8 * 3600
        os.utime(fifo, (old, old))
        os.utime(self.pad, (old, old))
        with mock.patch.object(scratch_evict, "_copy_file",
                               wraps=scratch_evict._copy_file) as copy:
            rep = bounded(self, lambda: self.gc(apply=True))
        self.assertFalse(os.path.exists(stale), "the positive control stayed")
        self.assertTrue(stat.S_ISFIFO(os.lstat(fifo).st_mode))
        self.assertNotIn(fifo, [c.args[0] for c in copy.call_args_list])
        whys = [why for p, why in rep["tier2_kept"] if p == fifo]
        self.assertTrue(whys and "FIFO holds no data" in whys[0],
                        rep["tier2_kept"])
        self.assertNotIn(fifo, [p for p, _why in rep["errors"]])
        self.assertNotIn(fifo, [r["path"] for r in self.archived()])

    def test_the_cli_lists_and_restores(self):
        stale = self.unit("reviews", files=2)
        self.gc(apply=True)
        from helm import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["scratch", "evicted", "--session", self.LIVE])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn(stale, out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["scratch", "restore", stale])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertTrue(os.path.isdir(stale))


class VolatileFsTest(unittest.TestCase):
    """The archive-root filesystem reading itself, against a fixture mount
    table: the longest mount-point prefix of the nearest existing ancestor."""

    def setUp(self):
        import tempfile
        self.tmp = os.path.realpath(tempfile.mkdtemp(
            prefix="helm-test-scratch-evict-mounts-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ram = os.path.join(self.tmp, "ram")
        self.disk = os.path.join(self.tmp, "disk")
        os.makedirs(self.ram)
        os.makedirs(self.disk)
        table = os.path.join(self.tmp, "mounts")
        with open(table, "w") as f:
            f.write("/dev/sda1 / ext4 rw 0 0\n"
                    "tmpfs %s tmpfs rw,size=1g 0 0\n"
                    "ramfs %s/deeper ramfs rw 0 0\n" % (self.ram, self.ram))
        p = mock.patch.object(scratch, "MOUNTS", table)
        p.start()
        self.addCleanup(p.stop)

    def test_a_root_under_a_tmpfs_mount_reads_tmpfs(self):
        root = os.path.join(self.ram, "not", "made", "yet")
        self.assertEqual(scratch_evict._volatile_fs(root), "tmpfs")
        self.assertIn("tmpfs", scratch_evict.unavailable(root))

    def test_the_longest_mount_prefix_wins(self):
        os.makedirs(os.path.join(self.ram, "deeper"))
        root = os.path.join(self.ram, "deeper", "archive")
        self.assertEqual(scratch_evict._volatile_fs(root), "ramfs")

    def test_a_root_on_disk_is_available(self):  # noqa: VACUOUS_ASSERTION — the tmpfs and ramfs arms above are the positive controls on the same table
        root = os.path.join(self.disk, "archive")
        self.assertIsNone(scratch_evict._volatile_fs(root))
        self.assertIsNone(scratch_evict.unavailable(root))

    def test_an_unreadable_mount_table_leaves_the_floor_alone(self):  # noqa: VACUOUS_ASSERTION — the tmpfs arm above, same root, is the positive control
        """No table, no volatile reading: the floor still judges the root."""
        with mock.patch.object(scratch, "MOUNTS",
                               os.path.join(self.tmp, "absent")):
            self.assertIsNone(scratch_evict._volatile_fs(
                os.path.join(self.ram, "archive")))


if __name__ == "__main__":
    unittest.main()
