#!/usr/bin/env python3
"""gc tests — hermetic (the test_store pattern): HELM_HOME + HELM_CACHE_DIR +
HELM_ADOPTED_DIR all point at a tempdir; real estate never touched. Exhaust is
PLANTED (oversized ledgers, stale session files, dated backups) and the laws
proven: dry-run reaps nothing, apply reaps only class-exhaust, authored bytes
survive byte-identical, premise-check survives a gc pass."""
import contextlib
import fcntl
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import gc, home, inject, keepalive, pk, premise, store  # noqa: E402

OLD = 45 * gc.DAY   # comfortably past every prune budget
BIG = 6 * gc.MB     # comfortably past every size budget


def run(fn, args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(args)
    return rc, out.getvalue(), err.getvalue()


class GcBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-gc-")
        # Hermetic cwd: the work-worktrees stream reads the AMBIENT git repo
        # (find_root walks up from cwd, no HELM_HOME seam) — a non-repo cwd
        # isolates it so an empty estate stays empty regardless of live lanes.
        self.cwd_prior = os.getcwd()
        os.chdir(self.tmp)
        self.env_prior = {k: os.environ.get(k) for k in (
            "HELM_HOME", "HELM_CACHE_DIR", "MELD_CACHE_DIR", "HELM_ADOPTED_DIR",
            "HELM_CHAT_DIR")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        # THE CHAT SEAM, and leaving it unset made this suite a live-fleet reaper.
        # chat lives in tmpfs at /dev/shm/helm-chat, which no HELM_HOME redirect
        # touches, so the chat-cursors stream read the REAL fleet directory while
        # every other stream read tmp. Measured on this branch before the fix:
        # scan() returned over=True with 24,001 victims, all of them live-fleet
        # files, and ApplyTest calls `gc --apply`, which reaps every reapable row
        # — so RUNNING THE TEST SUITE would have deleted 24,001 cursors out from
        # under the running fleet. The seat that survived would then re-read its
        # whole room and re-deliver everything it had already seen.
        #
        # Same class as the inject tests two hours earlier (e04cce4, "inject tests
        # read the LIVE fleet's chat, so their result depended on it"). There the
        # cost was a flaky assertion; here it is destruction of live state, which
        # is what a non-hermetic test earns you once the code under test can WRITE.
        # Every env seam a stream reads belongs in this dict, not just the ones
        # the current assertions happen to notice.
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ.pop("MELD_CACHE_DIR", None)
        for d in ("cache", "adopted", "chat"):
            os.makedirs(os.path.join(self.tmp, d))

    def tearDown(self):
        os.chdir(self.cwd_prior)
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plant(self, path, body="x\n", days_old=0):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        if days_old:
            t = time.time() - days_old * gc.DAY
            os.utime(path, (t, t))
        return path

    def snapshot(self, root):
        out = {}
        for r, _dirs, files in os.walk(root):
            for f in files:
                p = os.path.join(r, f)
                with open(p, "rb") as fh:
                    out[p] = fh.read()
        return out

    def row(self, rows, stream):
        return next(r for r in rows if r["stream"] == stream)


class ScanTest(GcBase):
    def test_empty_estate_all_in_budget(self):
        # The suite itself may run inside a legitimately leased worktree. The
        # temp HELM_HOME intentionally hides that real lease registry, so an
        # unmocked host scan would misclassify the test runner as an orphan.
        with mock.patch("helm.work.gc_orphans", return_value=[]):
            rc, out, _ = run(gc.cmd_gc, [])
        self.assertEqual(rc, 0)
        self.assertIn("every stream in budget", out)
        self.assertIn("(%d declared)" % len(gc.POLICIES), out)

    def test_bad_flag_is_usage(self):
        rc, _, err = run(gc.cmd_gc, ["--force"])
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)
        rc, _, _ = run(gc.cmd_gc, ["--dry", "--apply"])
        self.assertEqual(rc, 2)

    def test_size_age_and_count_budgets_measure(self):
        self.plant(gc._cache("keepalive-log.jsonl"), "x" * BIG)
        self.plant(gc._state("inject-seen", "old.json"), days_old=OLD // gc.DAY)
        self.plant(gc._state("inject-seen", "new.json"))
        rows = gc.scan()
        self.assertTrue(self.row(rows, "keepalive-log")["over"])
        seen = self.row(rows, "inject-seen")
        self.assertTrue(seen["over"])
        self.assertEqual([os.path.basename(v) for v in seen["victims"]],
                         ["old.json"])
        self.assertFalse(self.row(rows, "events")["over"])

    def test_stamp_beats_mtime_for_backup_age(self):
        # old stamp + fresh mtime -> stale; fresh stamp + old mtime -> kept
        # (copy2/move carry the ORIGIN's mtime — the wrong axis)
        stale = time.strftime("%Y%m%dT%H%M%S", time.gmtime(time.time() - OLD))
        fresh = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        self.plant(gc._cache("config-backups", stale + "-0__a.json"))
        self.plant(gc._cache("config-backups", fresh + "-0__b.json"),
                   days_old=OLD // gc.DAY)
        victims = self.row(gc.scan(), "config-backups")["victims"]
        self.assertEqual([os.path.basename(v) for v in victims],
                         [stale + "-0__a.json"])

    def test_active_session_dir_stays_fresh(self):
        # dir mtime old, one file inside fresh -> newest-mtime-within keeps it
        d = gc._state("reflex-state", "sid1")
        self.plant(os.path.join(d, "counters.json"), "{}")
        t = time.time() - OLD
        os.utime(d, (t, t))
        self.assertFalse(self.row(gc.scan(), "reflex-state")["over"])

    def test_count_zero_never_sorts_or_reads_generation_birth_times(self):  # noqa: VACUOUS_ASSERTION — exact nonempty victims and byte total prove the unsorted measurement path
        with mock.patch("helm.gc._born", side_effect=AssertionError("sorted")), \
                mock.patch("helm.gc._size", return_value=7):
            budget, used, victims, size = gc._measure_count(
                ["b", "a"], {"count": 0}, 0)
        self.assertEqual((budget, used, victims, size),
                         ("count>0", "2", ["b", "a"], 14))

    def test_keepalive_writer_and_gc_share_configured_owner_path_and_lock(self):  # noqa: VACUOUS_ASSERTION — _log plants a real row and finder returns its exact owner path
        self.assertEqual(keepalive._log_path(), gc._keepalive_path())
        self.assertEqual(keepalive._log_path(), gc._cache("keepalive-log.jsonl"))
        with mock.patch("helm.keepalive.fcntl.flock") as locked:
            keepalive._log({"action": "skip", "reason": "test"})
        self.assertIn(mock.call(mock.ANY, fcntl.LOCK_EX), locked.call_args_list)
        row = self.row(gc.scan(), "keepalive-log")
        self.assertEqual(row["policy"]["find"](), [keepalive._log_path()])
        self.assertEqual(row["policy"]["lock"](keepalive._log_path()),
                         keepalive._log_path() + ".lock")

    def test_store_cache_writer_and_gc_share_owner_lock(self):
        with mock.patch("helm.inject._entries.fcntl.flock") as locked:
            inject.load_entries()
        path = inject._cache_file()
        self.assertTrue(os.path.exists(path),
                        "positive control: owner did not mint its cache")
        self.assertIn(mock.call(mock.ANY, fcntl.LOCK_EX), locked.call_args_list)
        row = self.row(gc.scan(), "store-cache")
        self.assertIn(path, row["policy"]["find"]())
        self.assertEqual(row["policy"]["lock"](path), path + ".lock")

    def test_fail_open_stream_error_continues(self):  # noqa: VACUOUS_ASSERTION — planted oversized log proves the unaffected stream measured while one owner row errors
        self.plant(gc._cache("keepalive-log.jsonl"), "x" * BIG)
        blocked = gc._cache("config-backups")
        listdir = os.listdir
        for failure in (PermissionError("owner directory unreadable"),
                        OSError("owner directory failed")):
            with self.subTest(failure=failure.__class__.__name__):
                def names(path):
                    if path == blocked:
                        raise failure
                    return listdir(path)

                with mock.patch("helm.gc.os.listdir", side_effect=names):
                    rows = gc.scan()
                    self.assertTrue(self.row(rows, "keepalive-log")["over"],
                                    "positive control: another stream did not measure")
                    failed = [row for row in rows if row.get("error")]
                    self.assertEqual([row["stream"] for row in failed],
                                     ["config-backups"])
                    self.assertFalse(failed[0]["over"])
                    rc, out, _ = run(gc.cmd_gc, [])
                self.assertEqual(rc, 0)
                self.assertIn("ERR", out)
                self.assertIn("1 stream error", out)
                self.assertNotIn("every stream in budget", out)


class CursorRefusalTest(GcBase):
    """The r1 REFUTE, and the hermeticity hole found while fixing it."""

    def _cursor(self, sid, room="main"):
        return self.plant(os.path.join(
            os.environ["HELM_CHAT_DIR"], "%s.cursor.seat-a.%s" % (room, sid)), "9\n")

    def test_unprovable_liveness_is_a_loud_ERR_never_a_clean_sweep(self):
        """THE FINDING. chat.dead_cursors returns `err` precisely so that an
        unprovable liveness refuses to act. r1 collapsed that to `[]` under a bare
        `except Exception: return []`, so BOTH "I could not prove any session
        dead" and "I crashed" reached gc as an empty victim list — which gc reads
        as nothing-to-prune and prints in its reassuring `in budget` line.

        A reaper that proved nothing reporting CLEAN is the vacuous pass: the
        check ran, the input was absent, the answer was confident. gc already
        owned the honest channel (row["error"] -> ERR line, _reapable refuses it),
        so the fix was to DELETE the handler's own error handling and let the
        framework's surface it."""
        self._cursor("dead0001")
        with mock.patch("helm.chat.dead_cursors",
                        return_value=([], 0, "no session homes readable")):
            row = self.row(gc.scan(), "chat-cursors")
            self.assertIn("unprovable", row.get("error", ""))
            self.assertFalse(gc._reapable(row), "a refused row must never reap")
            rc, out, _ = run(gc.cmd_gc, ["--apply"])
        self.assertEqual(rc, 0)                 # gc is a janitor, not a gate
        self.assertIn("ERR", out)
        # and the refusal must not be laundered into the reassuring line
        for line in out.splitlines():
            if "in budget:" in line:
                self.assertNotIn("chat-cursors", line,
                                 "a stream that could not measure read as in budget")

    def test_a_crashing_cursor_scan_also_surfaces(self):
        """The other half of r1's `except Exception: return []`. A crash is not
        evidence of an empty estate."""
        with mock.patch("helm.chat.dead_cursors",
                        side_effect=OSError("chat dir vanished")):
            row = self.row(gc.scan(), "chat-cursors")
        self.assertIn("vanished", row.get("error", ""))
        self.assertFalse(row["over"], "a crashed measurement is not an over-budget one")

    def test_the_cursor_stream_reads_only_the_test_chat_dir(self):
        """HERMETICITY PIN, and the reason it exists is not hypothetical. With
        HELM_CHAT_DIR unset this suite read /dev/shm/helm-chat — the live fleet's
        tmpfs — and scan() returned 24,001 victims that ApplyTest's `gc --apply`
        would have deleted out from under running seats.

        So this asserts the stream is confined by PATH, not merely that the counts
        look plausible: every victim must live under this test's OWN tempdir. A
        count-based assertion would pass just as happily against production.

        And it checks against self.tmp rather than re-reading HELM_CHAT_DIR, which
        is the variable the planting used — asserting a value against itself is
        consistent by construction and would hold no matter where that variable
        pointed. self.tmp is the hermetic boundary the whole class rests on."""
        for i in range(3):
            self._cursor("dead%04d" % i)
        with mock.patch("helm.sessions.live_sids", return_value=set()):
            row = self.row(gc.scan(), "chat-cursors")
        self.assertTrue(row["victims"], "planted dead cursors were not found")
        for v in row["victims"]:
            self.assertTrue(v.startswith(self.tmp),
                            "gc reached outside the test tempdir: " + v)

    def test_apply_reaps_only_planted_cursors_and_keeps_the_live_one(self):
        live = "0fa7c4ed-9a5e-46a0-b2da-f862eb8afad6"
        keep = self._cursor(live[:8])
        drop = self._cursor("dead0001")
        with mock.patch("helm.sessions.live_sids", return_value={live}):
            rc, _out, _ = run(gc.cmd_gc, ["--apply"])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(keep), "reaped a LIVE session's cursor")
        self.assertFalse(os.path.exists(drop))


class OrphanCursorLockTest(GcBase):
    """The lock outlives its cursor and NO rung in the tree could select it.

    `chat.dead_cursors` skips `.lock` names in its own scan and reaches a lock
    only as the companion of a cursor it selected, so a lock whose cursor left
    by another door (`_unlink_seat_state` is keyed on the seat and takes the
    cursor alone) was unreachable forever. Measured on a live bus:
    dead-cursors selected 0 victims of 45,002 cursors on the same pass that
    left 2,354 of these stranded.

    The lock is an flock HANDLE, so every arm here is really about one thing:
    unlinking a lock somebody HOLDS does not release it, it detaches the name,
    and the next caller opens that name onto a fresh inode and takes an flock
    the holder cannot see. Two writers, one critical section.

    THE ROW NOW TAKES EVERY SIBLING, orphan or not (chatdebris.sibling_locks):
    the cursor stack takes one lock per room, so no sibling is anyone's handle
    any more, and 49,718 of them were the bulk of the live directory."""

    LIVE = "0fa7c4ed-9a5e-46a0-b2da-f862eb8afad6"
    # Standalone locks: having NO subject file is their permanent, correct
    # state. A filter that asked only "has no subject" nominates every one of
    # them — including the topology lock that serialises cursor creation.
    STANDALONE = (".cursor-topology.lock", ".cursor-room.main.lock",
                  ".cursor-estate.seat-a.lock", ".cursor-txn-room.main.lock",
                  ".roster.json.lock")

    def _chat(self, name, body="x\n"):
        return self.plant(os.path.join(os.environ["HELM_CHAT_DIR"], name), body)

    def _pair(self, sid, room="main", beacon=False):
        """A cursor and its stable sibling lock, as seats_cursor mints them."""
        stem = "%s%s.cursor.seat-a.%s" % (".beacon-" if beacon else "", room, sid)
        return self._chat(stem, "9\n"), self._chat(stem + ".lock", "")

    def _row(self):
        with mock.patch("helm.sessions.live_sids", return_value={self.LIVE}):
            return self.row(gc.scan(), "chat-cursor-locks")

    def test_a_lock_whose_cursor_is_gone_is_nominated(self):
        cursor, lock = self._pair("dead0001")
        os.remove(cursor)
        row = self._row()
        self.assertEqual(row["victims"], [lock])
        self.assertTrue(row["over"])

    def test_a_present_cursors_sibling_is_nominated_and_the_cursor_is_not(self):
        """The cursor's absence is not the selector. The cursor stack takes
        one lock per room and opens no per-cursor sibling, so a live cursor's
        lock is exactly as dead as an orphan's and both are nominated. The
        CURSOR is never a victim of this row: the victim set is asserted
        exactly, and the live cursor must still be there."""
        live_cursor, live_lock = self._pair(self.LIVE[:8])
        orphan_cursor, orphan = self._pair("dead0001")
        os.remove(orphan_cursor)
        row = self._row()
        self.assertEqual(sorted(row["victims"]), sorted([orphan, live_lock]))
        self.assertTrue(os.path.exists(live_cursor))

    def test_a_beacon_cursors_orphan_lock_is_nominated(self):
        cursor, lock = self._pair("dead0001", beacon=True)
        os.remove(cursor)
        self.assertIn(lock, self._row()["victims"])

    def test_standalone_locks_are_never_nominated(self):
        """THE SAFETY ARM. Every name here is a lock with no subject BY DESIGN
        and a live holder at any moment; deleting one is the two-inode bug.
        The orphan is the must-hit — it proves this scan really read this
        directory, so the survivals below are a decision and not a no-op."""
        planted = [self._chat(n, "") for n in self.STANDALONE]
        cursor, orphan = self._pair("dead0001")
        os.remove(cursor)
        row = self._row()
        self.assertEqual(row["victims"], [orphan],
                         "the selector reached a standalone lock")
        for path in planted:
            self.assertTrue(os.path.exists(path), "would have reaped " + path)

    def test_a_held_orphan_lock_is_skipped_not_deleted(self):
        """THE CORRECTNESS ARM. A held lock must survive. The finder now drops
        every inode /proc/locks names, so a lock held BEFORE the scan is never
        nominated at all; `_reap`'s own flock probe on the victim covers a
        lock taken between scan and unlink (tests/test_chatdebris.py pins that
        race). The second orphan is the must-hit: it shows the sweep ran and
        does reap this class, so the held one surviving is a refusal, never
        the row failing to fire."""
        held_cursor, held = self._pair("dead0001")
        os.remove(held_cursor)
        free_cursor, free = self._pair("dead0002")
        os.remove(free_cursor)
        holder = open(held, "a")
        try:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            with mock.patch("helm.sessions.live_sids", return_value={self.LIVE}):
                rc, out, _ = run(gc.cmd_gc, ["--apply"])
        finally:
            holder.close()
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(free), "must-hit: the free orphan survived")
        self.assertIn("pruned " + free, out)
        self.assertTrue(os.path.exists(held), "gc unlinked a lock somebody HELD")
        self.assertNotIn("pruned " + held, out)

    def test_apply_reaps_every_sibling_and_keeps_the_live_cursor(self):
        live_cursor, live_lock = self._pair(self.LIVE[:8])
        dead_cursor, dead_lock = self._pair("dead0001")
        os.remove(dead_cursor)
        with mock.patch("helm.sessions.live_sids", return_value={self.LIVE}):
            rc, _out, _ = run(gc.cmd_gc, ["--apply"])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(live_cursor), "reaped a LIVE cursor")
        self.assertFalse(os.path.exists(live_lock))
        self.assertFalse(os.path.exists(dead_lock))

    def test_dry_run_nominates_and_reaps_nothing(self):
        """The NOMINATION assert is the must-hit, and it is here because this
        arm was written without it and then survived every mutant I ran —
        including the one that makes the finder select nothing at all. "The
        file still exists" is true for free when the row never fires, so a
        dry-run arm has to prove there was something to reap first."""
        cursor, lock = self._pair("dead0001")
        os.remove(cursor)
        self.assertEqual(self._row()["victims"], [lock],
                         "nothing was nominated, so surviving proves nothing")
        with mock.patch("helm.sessions.live_sids", return_value={self.LIVE}):
            rc, out, _ = run(gc.cmd_gc, [])
        self.assertEqual(rc, 0)
        self.assertIn("chat-cursor-locks", out)
        self.assertTrue(os.path.exists(lock), "dry-run deleted a file")

    def test_the_lock_stream_reads_only_the_test_chat_dir(self):
        """HERMETICITY PIN, for the reason the cursor class carries one: with
        HELM_CHAT_DIR unset this suite reads /dev/shm/helm-chat, and an
        `--apply` arm then reaps the live fleet. Asserted by PATH — a count
        would pass just as happily against production."""
        for i in range(3):
            cursor, _lock = self._pair("dead%04d" % i)
            os.remove(cursor)
        row = self._row()
        self.assertEqual(len(row["victims"]), 3, "planted orphans were not found")
        for victim in row["victims"]:
            self.assertTrue(victim.startswith(self.tmp),
                            "gc reached outside the test tempdir: " + victim)


class DryRunTest(GcBase):
    def test_dry_default_reaps_nothing(self):
        self.plant(gc._cache("keepalive-log.jsonl"), "x" * BIG)
        self.plant(gc._state("inject-seen", "old.json"), days_old=OLD // gc.DAY)
        self.plant(gc._state("attest-queue.jsonl"), "q" * (300 * 1024))
        before = self.snapshot(self.tmp)
        for args in ([], ["--dry"]):
            rc, out, _ = run(gc.cmd_gc, args)
            self.assertEqual(rc, 0)
            self.assertIn("would", out)
            self.assertIn("nothing touched", out)
        self.assertEqual(self.snapshot(self.tmp), before)


class ApplyTest(GcBase):
    def test_rotate_is_atomic_dated_and_byte_preserving(self):  # noqa: VACUOUS_ASSERTION — exact keepalive archive bytes and one receipt are positive controls
        body = "x" * BIG
        live = self.plant(gc._cache("keepalive-log.jsonl"), body)
        rc, out, _ = run(gc.cmd_gc, ["--apply"])
        self.assertEqual(rc, 0)
        self.assertIn("rotated", out)
        self.assertFalse(os.path.exists(live))
        generations = gc._keepalive_generations()
        self.assertEqual(len(generations), 1)
        with open(generations[0], encoding="utf-8") as f:
            self.assertEqual(f.read(), body)
        self.assertEqual(len(pk.read_events(10)), 1)

    def test_held_owner_lock_skips_mutation_and_mints_no_receipt(self):  # noqa: VACUOUS_ASSERTION — surviving victim and SKIPPED output prove contention fired
        live = self.plant(gc._cache("keepalive-log.jsonl"), "x" * BIG)
        lock = open(live + ".lock", "a+")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            rc, out, _ = run(gc.cmd_gc, ["--apply"])
        finally:
            lock.close()
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(live))
        self.assertIn("SKIPPED", out)
        self.assertIn("reaped 0 items; 1 skipped", out)
        self.assertEqual(pk.read_events(10), [])

    def test_prune_reaps_stale_keeps_fresh(self):
        old = time.strftime("%Y%m%dT%H%M%S", time.gmtime(time.time() - OLD))
        new = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        stale = (
            self.plant(gc._cache("keepalive-log.jsonl.%sZ" % old)),
            self.plant(gc._cache("store-cache-old.json"),
                       days_old=OLD // gc.DAY),
        )
        fresh = (
            self.plant(gc._cache("keepalive-log.jsonl.%sZ" % new),
                       days_old=OLD // gc.DAY),
            self.plant(gc._cache("store-cache-new.json")),
        )
        rc, out, _ = run(gc.cmd_gc, ["--apply"])
        self.assertEqual(rc, 0)
        self.assertTrue(all(not os.path.exists(path) for path in stale))
        self.assertTrue(all(os.path.exists(path) for path in fresh))
        self.assertIn("reaped 2 items", out)

    def test_replaced_and_freshened_generations_centralize_skip_accounting(self):
        replaced = self.plant(gc._cache("store-cache-replaced.json"),
                              days_old=OLD // gc.DAY)
        freshened = self.plant(gc._cache("store-cache-freshened.json"),
                               days_old=OLD // gc.DAY)
        pruned = self.plant(gc._cache("store-cache-pruned.json"),
                            days_old=OLD // gc.DAY)
        row = self.row(gc.scan(), "store-cache")
        replacement = self.plant(replaced + ".new", "new\n")
        os.replace(replacement, replaced)
        os.utime(freshened, None)
        with mock.patch("helm.gc.scan", return_value=[row]):
            rc, out, _ = run(gc.cmd_gc, ["--apply"])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(replaced))
        self.assertTrue(os.path.exists(freshened))
        self.assertFalse(os.path.exists(pruned))
        self.assertEqual(out.count("SKIPPED "), 2)
        self.assertIn("reaped 1 item", out)
        self.assertIn("2 skipped", out)
        events = pk.read_events(10)
        self.assertEqual(len(events), 1)
        self.assertIn("reaped 1 item", events[0]["summary"])

    def test_apply_writes_one_receipt(self):
        self.plant(gc._cache("store-cache-old.json"), days_old=OLD // gc.DAY)
        run(gc.cmd_gc, ["--apply"])
        events = pk.read_events(10)
        self.assertEqual([e["verb"] for e in events], ["gc"])
        self.assertIn("reaped 1 item", events[0]["summary"])

    def test_owner_mismatch_never_enters_keepalive_generation_actuator(self):
        old = time.strftime("%Y%m%dT%H%M%S", time.gmtime(time.time() - OLD))
        foreign = self.plant(gc._state("events.jsonl.%sZ" % old))
        newline = self.plant(gc._cache("keepalive-log.jsonl.%sZ\n" % old))
        owned = self.plant(gc._cache("keepalive-log.jsonl.%sZ" % old))
        rc, out, _ = run(gc.cmd_gc, ["--apply"])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(foreign),
                        "keepalive owner pruned another stream's generation")
        self.assertTrue(os.path.exists(newline),
                        "newline suffix passed the immutable-name boundary")
        self.assertFalse(os.path.exists(owned),
                         "positive control: owned stale generation was not pruned")
        self.assertIn("reaped 1 item", out)

    def test_report_only_streams_never_reaped(self):
        queue = self.plant(gc._state("attest-queue.jsonl"), "q" * (300 * 1024))
        trash = self.plant(gc._cache("skills-trash", "20250101T000000-0-old.md"))
        drain = self.plant(os.path.join(os.environ["HELM_ADOPTED_DIR"],
                                        "archive", "drain-2025010100", "raw.md"),
                           days_old=200)
        rc, out, _ = run(gc.cmd_gc, ["--apply"])
        self.assertEqual(rc, 0)
        for kept in (queue, trash, drain):
            self.assertTrue(os.path.exists(kept), kept)
        self.assertIn("report-only", out)
        self.assertIn("--retry-queue", out)
        self.assertIn("owner reaps", out)


class AuthoredSafetyTest(GcBase):
    def test_apply_never_touches_authored_content(self):
        home.scaffold_global()
        store.write_prior({"id": "law1", "statement": "authored survives gc",
                           "confidence": 0.9, "stated_ts": pk.now_ts()})
        self.plant(os.path.join(home.global_dir(), "heuristics", "h.md"), "# h\n",
                   days_old=OLD // gc.DAY)
        self.plant(os.path.join(home.project_dir("proj"), "premises", "p.md"),
                   "# p\n", days_old=OLD // gc.DAY)
        authored = {p: b for p, b in self.snapshot(self.tmp).items()
                    if ".state" not in p and os.sep + "cache" + os.sep not in p}
        self.assertGreaterEqual(len(authored), 3)
        self.plant(gc._cache("keepalive-log.jsonl"), "x" * BIG)
        self.plant(gc._state("inject-seen", "old.json"), days_old=OLD // gc.DAY)
        run(gc.cmd_gc, ["--apply"])
        after = self.snapshot(self.tmp)
        for p, body in authored.items():
            self.assertEqual(after.get(p), body, p)

    def test_premise_check_survives_a_gc_pass(self):
        # the card's declared risk test: gc must never break digest verification
        stmt = "gc reaps exhaust, never authored bytes"
        # a REAL attested entry (native record lands) — the risk test is that gc
        # must never break the primary proof, so it must be a proof, not a
        # payload-only stub (which is now honestly NOT ATTESTED).
        rc, _, _ = run(premise.cmd_premise, ["gc-law", "|", stmt])
        self.assertEqual(rc, 0)
        self.plant(gc._cache("keepalive-log.jsonl"), "x" * BIG)
        self.plant(gc._state("inject-seen", "old.json"), days_old=OLD // gc.DAY)
        rc, out, _ = run(gc.cmd_gc, ["--apply"])
        self.assertEqual(rc, 0)
        rc, out, _ = run(premise.cmd_premise_check, ["gc-law"])
        self.assertEqual(rc, 0)
        self.assertIn("MATCH", out)


class TableTest(GcBase):
    def test_every_policy_declares_one_budget_axis(self):
        for p in gc.POLICIES:
            axes = [k for k in ("size", "age", "count") if k in p]
            self.assertEqual(len(axes), 1, p["stream"])
            self.assertIn(p["cls"], ("exhaust", "source", "archive", "state"))
            self.assertIn(p["act"], ("rotate", "prune", "report"))

    def test_only_exhaust_is_ever_reapable(self):  # noqa: VACUOUS_ASSERTION — the table has a separately asserted exact nonempty owner set below
        for p in gc.POLICIES:
            if p["cls"] != "exhaust":
                self.assertEqual(p["act"], "report", p["stream"])
            row = {"over": True, "cls": p["cls"]}
            self.assertEqual(gc._reapable(row), p["cls"] == "exhaust")

    def test_owner_protocols_are_selective_and_complete(self):  # noqa: VACUOUS_ASSERTION — exact nonempty owner set is the positive control
        owners = {p["stream"]: p["owner"] for p in gc.POLICIES
                  if p.get("owner")}
        self.assertEqual(owners, {
            "chat-cursor-locks": "seats-cursor",
            "keepalive-log": "keepalive",
            "rotated-generations": "keepalive",
            "store-cache": "inject-entries",
        })
        for p in gc.POLICIES:
            if p.get("owner"):
                self.assertEqual(p["cls"], "exhaust")
                self.assertIn(p["act"], ("rotate", "prune"))
                self.assertTrue(p.get("lock") or p.get("immutable"))


if __name__ == "__main__":
    unittest.main()


class TimerShipsTest(unittest.TestCase):
    """The drain must SHIP its own cadence. gc knew its retention policy from
    the start and nothing ever ran it — 11,192 items over budget when the
    sustainability audit finally measured it (2026-07-30). A policy with no
    scheduler is a policy that does not exist, so the installer is part of the
    verb, and these controls keep it honest."""

    def test_units_are_wellformed_and_hourly(self):
        spath, service, tpath, timer = gc.timer_units()
        self.assertTrue(spath.endswith("helm-gc.service"))
        self.assertTrue(tpath.endswith("helm-gc.timer"))
        self.assertIn("ExecStart=", service)
        self.assertIn("gc --apply", service)          # the DRAIN, not a dry-run
        self.assertIn("OnUnitActiveSec=3600", timer)  # hourly by default
        self.assertIn("Persistent=true", timer)       # survives a reboot gap
        self.assertIn("WantedBy=timers.target", timer)

    def test_workingdirectory_is_the_shared_checkout_never_a_worktree(self):
        # A persistent unit that captured a disposable lane worktree as its cwd
        # would die with that worktree; work.find_root folds a lane back to the
        # shared checkout. Also the never-track law: no operator path may be a
        # LITERAL in the tracked template.
        _s, service, _t, _tm = gc.timer_units()
        line = [l for l in service.splitlines()
                if l.startswith("WorkingDirectory=")]
        self.assertEqual(len(line), 1, service)
        cwd = line[0].split("=", 1)[1]
        self.assertNotIn("-wt/", cwd, "unit captured a lane worktree")
        self.assertTrue(os.path.isdir(cwd), cwd)
        src = open(os.path.join(os.path.dirname(gc.__file__), "gc.py")).read()
        self.assertNotIn(cwd, src, "operator path baked into the template")

    def test_interval_must_be_positive(self):
        ok, detail = gc.ensure_timer(interval=0)
        self.assertFalse(ok)
        self.assertIn("at least 1 second", detail)

    def test_install_timer_flag_is_accepted_and_reports(self):
        # MUST-NOT-HIT the scan path: --install-timer never reaps.
        with mock.patch.object(gc, "ensure_timer",
                               return_value=(True, "timer enabled")) as m, \
                mock.patch.object(gc, "scan") as scanned:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = gc.cmd_gc(["--install-timer"])
            self.assertEqual(rc, 0)
            m.assert_called_once()
            scanned.assert_not_called()
            self.assertIn("timer enabled", buf.getvalue())
