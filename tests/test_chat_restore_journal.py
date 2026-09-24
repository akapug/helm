#!/usr/bin/env python3
"""helm chat restore-journal — log_flush's inverse, born from a real wipe.

The rooms live on tmpfs and the 2026-07-28 reboot erased them; the disk
journal survived and history was rebuilt from it by hand. These tests turn
that one-shot rescue into a contract, and they pin the two traps the rescue
actually hit live:

  1. THE DOUBLE-RESTORE: journal bodies are RENDERED rows, so a naive dedupe
     misses already-restored reconstructions (whose re-render grows a second
     signature tag) and doubles the room.
  2. THE CURSOR STORM: rid-suppression protects every consumer WITH a cursor,
     but a tracked seat with NO cursor reads a room from offset 0 by design —
     the live restore put pending 279 on one seat until end-of-file cursors
     were minted. Restore and mint are ONE act because either alone is a trap.
"""
import contextlib
import io
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import declaring as _declaring  # noqa: E402
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-restorej-", var="HELM_HOME")

from helm import chat, meld, pk, seats  # noqa: E402

ENV_KEYS = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NODE_URL",
            "HELM_CHAT_NAME", "HELM_SCRATCH_GC", "HELM_CACHE_DIR",
            "HELM_CHAT_LOG", "MELD_CHAT_DIR")


class RestoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-restorej-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""     # hermetic: no signed node
        os.environ["HELM_CHAT_NAME"] = "tester"
        os.environ["HELM_SCRATCH_GC"] = "0"
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        self.cwd_prior = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd_prior)
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def wipe_rooms(self):
        """The reboot: tmpfs gone, journal intact."""
        shutil.rmtree(os.environ["HELM_CHAT_DIR"], ignore_errors=True)

    def texts(self, room):
        rows, _ = chat.read(room)
        return [str(r.get("text")) for r in rows]


class RoundTripTest(RestoreBase):
    def test_the_reboot_scenario_end_to_end(self):
        """post -> flush -> wipe -> restore: the reason this verb exists."""
        chat.post("first message", room="main", who="alice")
        chat.post("a multi-line body\nline two\nline three: with a colon",
                  room="main", who="bob")
        self.assertGreater(chat.log_flush(), 0)
        self.wipe_rooms()
        self.assertEqual(chat.read("main")[1], 0)          # proven gone

        out = chat.restore_journal(apply=True)
        self.assertEqual(out["state"], "ok")
        self.assertEqual(out["rooms"]["main"]["restored"], 2)

        rows, _ = chat.read("main")
        self.assertEqual(rows[0]["from"], "alice")
        self.assertEqual(rows[0]["text"], "first message")
        self.assertEqual(rows[0].get("restored"), 1)
        # the multi-line body survives INTACT — the journal writes its
        # newlines raw, so the parser must stitch continuations back together
        self.assertEqual(rows[1]["from"], "bob")
        self.assertEqual(rows[1]["text"],
                         "a multi-line body\nline two\nline three: with a colon")
        # provenance marker closes the restored region
        self.assertEqual(rows[2]["from"], "journal")
        self.assertIn("RESTORED from the disk journal", rows[2]["text"])

    def test_no_second_signature_tag_grows_on_restored_rows(self):
        """The journal body ends in the rendered ' [unsigned]' tag; _fmt
        re-appends one live. Keeping the baked tag doubles it forever —
        measured on the real restore before the strip existed."""
        chat.post("tagged once", room="main", who="alice")
        chat.log_flush()
        self.wipe_rooms()
        chat.restore_journal(apply=True)
        rows, _ = chat.read("main")
        rendered = chat._fmt(rows[0])
        self.assertEqual(rendered.count("[unsigned]"), 1, rendered)

    def test_live_rows_after_the_wipe_are_preserved_byte_identical(self):
        chat.post("pre-reboot", room="main", who="alice")
        chat.log_flush()
        self.wipe_rooms()
        chat.post("post-reboot survivor", room="main", who="carol")
        raw_before = open(chat.room_path("main"), encoding="utf-8").read()
        chat.restore_journal(apply=True)
        raw_after = open(chat.room_path("main"), encoding="utf-8").read()
        self.assertTrue(raw_after.endswith(raw_before),
                        "live rows must be the untouched tail of the new file")
        texts = self.texts("main")
        self.assertEqual(texts[0], "pre-reboot")
        self.assertEqual(texts[-1], "post-reboot survivor")


class IdempotenceTest(RestoreBase):
    def test_applying_twice_changes_nothing(self):
        chat.post("only once", room="main", who="alice")
        chat.log_flush()
        self.wipe_rooms()
        chat.restore_journal(apply=True)
        first = open(chat.room_path("main"), encoding="utf-8").read()
        out = chat.restore_journal(apply=True)
        self.assertEqual(out["rooms"]["main"].get("skipped"), "already restored")
        self.assertEqual(first,
                         open(chat.room_path("main"), encoding="utf-8").read())

    def test_an_UNWIPED_room_restores_nothing(self):
        """The journal always contains what is still live — a restore against
        a healthy room must be a no-op, not a duplication."""
        chat.post("still here", room="main", who="alice")
        chat.log_flush()
        out = chat.restore_journal(apply=True)
        self.assertEqual(out["rooms"]["main"].get("restored", 0), 0)
        self.assertEqual(self.texts("main"), ["still here"])

    def test_an_OLD_VINTAGE_marker_is_recognized_by_content(self):
        """The first (hand-run) rescue minted markers under a different id
        salt. Detection must be by content, or the verb re-restores every
        room the rescue already handled — the exact miss the live dry-run
        caught, 849 rows from doubling."""
        chat.post("history", room="main", who="alice")
        chat.log_flush()
        self.wipe_rooms()
        chat.post("-- rows above were RESTORED from the disk journal after a "
                  "reboot --", room="main", who="journal")
        out = chat.restore_journal(apply=True)
        self.assertEqual(out["rooms"]["main"].get("skipped"), "already restored")


class CursorStormTest(RestoreBase):
    def test_restore_cannot_re_deliver_history_to_a_cursorless_seat(self):
        """THE STORM, reproduced: tracked seat, no cursor -> reads from
        offset 0 by design, so restored history is 'pending' unless the
        restore mints an end-of-file cursor in the same act. Live number
        before the mint existed: pending 279."""
        chat.post("@worker old ask from days ago", room="main", who="alice")
        chat.log_flush()
        self.wipe_rooms()
        # a roster seat with NO cursor anywhere — the storm precondition
        seats.write_roster("worker", session="s1", cwd=self.tmp)
        self.assertIsNone(seats._cursor("main", "worker"))

        out = chat.restore_journal(apply=True)
        self.assertGreaterEqual(out["rooms"]["main"]["cursors_minted"], 1)
        cur = seats._cursor("main", "worker")
        self.assertIsNotNone(cur, "the restore must mint the missing cursor")
        pend = seats._pending_rows("main", "worker", backfill=True)
        self.assertEqual(len(pend), 0,
                         "restored history must never surface as pending")

    def test_a_cursor_whose_rid_SURVIVES_is_left_alone(self):
        """rid-in-file is the ONE test: a consumer parked on a row the merge
        kept must not be rewound or fast-forwarded by the restore."""
        chat.post("pre-wipe", room="main", who="alice")
        chat.log_flush()
        self.wipe_rooms()
        chat.post("post-wipe survivor", room="main", who="carol")
        seats.write_roster("reader", session="s1", cwd=self.tmp)
        st = seats._baseline_state("main", at_start=False)
        seats._write_cursor("main", "reader", *st, base=st[2])
        before = seats._cursor("main", "reader")
        self.assertIsNotNone(before.get("rid"))     # points at the survivor
        chat.restore_journal(apply=True)
        after = seats._cursor("main", "reader")
        self.assertEqual(after.get("rid"), before.get("rid"),
                         "a surviving rid must not be touched")

    def test_a_cursor_whose_rid_was_WIPED_is_repaired_to_eof(self):
        """The 290-row beacon flood, reproduced (integrator attack 1): the
        original rows are gone, so the restored file carries reconstruction
        ids the old cursor cannot find — _tail's rotation law then resets to
        zero and 'accepts duplicate replay'. First version of this test
        asserted the cursor be LEFT ALONE, i.e. asserted the flood."""
        chat.post("first", room="main", who="alice")
        seats.write_roster("reader", session="s1", cwd=self.tmp)
        st = seats._baseline_state("main", at_start=False)
        seats._write_cursor("main", "reader", *st, base=st[2])
        chat.post("@reader second, addressed", room="main", who="alice")
        chat.log_flush()
        os.remove(chat.room_path("main"))            # partial wipe
        chat.restore_journal(apply=True)
        cur = seats._cursor("main", "reader")
        rows, _ = chat.read("main")
        self.assertEqual(cur.get("rid"), rows[-1].get("id"),
                         "a dead rid must be repaired to end-of-file")
        self.assertEqual(
            len(seats._pending_rows("main", "reader", backfill=True)), 0,
            "restored history must never be deliverable")

    def test_SESSION_scoped_cursors_are_repaired_too(self):
        """Delivery prefers the (seat, session) cursor when one exists — a
        repaired seat-level file with a stale session file still floods."""
        chat.post("first", room="main", who="alice")
        seats.write_roster("reader", session="sess12345678", cwd=self.tmp)
        st = seats._baseline_state("main", at_start=False)
        seats._write_cursor("main", "reader", *st, base=st[2],
                            session="sess12345678")
        chat.post("second", room="main", who="alice")
        chat.log_flush()
        os.remove(chat.room_path("main"))
        chat.restore_journal(apply=True)
        cur = seats._cursor("main", "reader", session="sess12345678")
        rows, _ = chat.read("main")
        self.assertEqual(cur.get("rid"), rows[-1].get("id"))

    def test_restore_repairs_delivery_and_wake_cursor_as_one_pair(self):
        chat.post("first", room="main", who="alice")
        chat.log_flush()
        self.wipe_rooms()
        seats.write_roster("reader", session="sess12345678", cwd=self.tmp)
        seats._write_cursor("main", "reader", 1, 2, 0, "dead-delivery",
                            session="sess12345678", base=0)
        seats._write_cursor("main", "reader", 1, 2, 0, "dead-wake",
                            session="sess12345678", beacon=True,
                            held=["1:2:0"], base=0)
        chat.restore_journal(apply=True)
        delivery = seats._cursor("main", "reader", "sess12345678")
        wake = seats._cursor("main", "reader", "sess12345678", beacon=True)
        self.assertEqual((delivery["dev"], delivery["ino"], delivery["off"]),
                         (wake["dev"], wake["ino"], wake["off"]))
        self.assertNotIn("held", wake)
        rows, _ = chat.read("main")
        self.assertEqual(delivery["rid"], rows[-1]["id"])

    def test_rerunning_after_a_crash_between_swap_and_sweep_repairs(self):
        """The swap and the sweep are one act inside the lock, but a crash
        between them is still possible — recovery is RERUN, so the
        already-restored path must sweep instead of shrugging at the marker."""
        chat.post("history", room="main", who="alice")
        chat.log_flush()
        self.wipe_rooms()
        chat.restore_journal(apply=True)
        # simulate the crash's leftover: a cursor pointing at a dead rid
        st = seats._baseline_state("main", at_start=False)
        seats.write_roster("late", session="s1", cwd=self.tmp)
        seats._write_cursor("main", "late", st[0], st[1], 0, "dead0000rid0",
                            base=0)
        out = chat.restore_journal(apply=True)
        self.assertEqual(out["rooms"]["main"].get("skipped"), "already restored")
        self.assertGreaterEqual(out["rooms"]["main"].get("cursors_repaired", 0), 1)
        cur = seats._cursor("main", "late")
        rows, _ = chat.read("main")
        self.assertEqual(cur.get("rid"), rows[-1].get("id"))


class FlushInteractionTest(RestoreBase):
    def test_the_next_flush_does_not_relog_the_restored_room(self):
        """restore rewrites the flush high-water mark; without that the next
        log_flush sees a fingerprint mismatch and appends the entire room to
        the journal again — duplicates that the NEXT restore would then
        faithfully rebuild. The cycle only has to happen once to be forever."""
        chat.post("one", room="main", who="alice")
        chat.post("two", room="main", who="bob")
        chat.log_flush()
        self.wipe_rooms()
        chat.restore_journal(apply=True)
        self.assertEqual(chat.log_flush(), 0,
                         "a flush right after a restore must append nothing")


class ParserEdgeTest(RestoreBase):
    def test_flush_gap_notes_are_not_rows(self):
        chat.post("real row", room="main", who="alice")
        chat.log_flush()
        # a gap note the flusher writes when history was lost before a flush
        with open(os.path.join(chat.journal_dir(),
                               sorted(os.listdir(chat.journal_dir()))[0]),
                  "a", encoding="utf-8") as f:
            f.write("# 2026-07-28T00:00:00Z [main] rotation gap — some rows "
                    "were dropped before this flush\n")
        self.wipe_rooms()
        chat.restore_journal(apply=True)
        self.assertEqual(self.texts("main")[:-1], ["real row"])

    def test_meld_rooms_restore_their_content(self):
        """The 2026-08-02 reboot emptied every meld room — lifecycle skeletons
        survived, the converged spec conversation did not, and participants
        rebuilt it from transcripts by hand. A meld's CONTENT is the work
        product; restore must bring it back like any room."""
        chat.post("meld talk", room="meld-123-topic", who="alice")
        chat.post("keep me", room="main", who="alice")
        chat.log_flush(rooms=["meld-123-topic", "main"])
        self.wipe_rooms()
        out = chat.restore_journal(apply=True)
        self.assertEqual(out["rooms"]["meld-123-topic"]["restored"], 1)
        self.assertEqual(self.texts("meld-123-topic")[:-1], ["meld talk"])
        self.assertEqual(self.texts("main")[:-1], ["keep me"])

    def test_dm_rooms_still_never_restore(self):
        """dm-* was never journaled and stays that way — the meld-content
        change does not widen into the DM lane."""
        chat.post("main row", room="main", who="alice")
        os.makedirs(os.path.dirname(chat.room_path("dm-bob")), exist_ok=True)
        with open(chat.room_path("dm-bob"), "w", encoding="utf-8") as f:
            f.write('{"id": "x1", "ts": "2026-08-02T00:00:00Z", '
                    '"from": "alice", "text": "dm row"}\n')
        chat.log_flush(rooms=["dm-bob", "main"])
        logs = [n for n in os.listdir(chat.journal_dir())
                if n.startswith("chat-") and n.endswith(".log")]
        self.assertTrue(logs)
        with open(os.path.join(chat.journal_dir(), sorted(logs)[0]),
                  encoding="utf-8") as f:
            self.assertNotIn("[dm-bob]", f.read())
        self.wipe_rooms()
        out = chat.restore_journal(apply=True)
        self.assertNotIn("dm-bob", out["rooms"])
        self.assertFalse(os.path.exists(chat.room_path("dm-bob")))
        self.assertEqual(self.texts("main")[:-1], ["main row"])

    def test_restore_replays_meld_state_without_resurrecting_room_rows(self):
        room, _ = meld.invite("seat-b", "restore lifecycle", seat="seat-a")
        meld.join(room, seat="seat-b")
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        chat.log_flush(rooms=[room])
        self.wipe_rooms()
        out = chat.restore_journal(apply=True)
        self.assertEqual(out["meld"]["rooms"][room]["state"], "ok")
        self.assertEqual(meld.state(room, "seat-a")["status"], "active")
        self.assertEqual(meld.state(room, "seat-a")["idx"], 0)

    def test_empty_absent_and_unreadable_are_THREE_different_answers(self):
        """Integrator attack 2, the vacuous-apply class: a restore that
        silently restores nothing reads identically to one that worked.
        Absent journal, empty journal, and unreadable journal must be
        distinguishable in the OUTPUT, and unreadable must be UNKNOWN — a
        floor, not a confident zero."""
        import contextlib, io
        def cli(*args):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = chat.cmd_chat(list(args))
            return rc, out.getvalue(), err.getvalue()
        # absent
        rc, out, err = cli("restore-journal", "--apply")
        self.assertEqual(rc, 0)
        self.assertIn("no journal", out)
        # empty: a journal dir with one zero-record file
        os.makedirs(chat.journal_dir(), exist_ok=True)
        empty = os.path.join(chat.journal_dir(), "chat-2026-07-27.log")
        open(empty, "w").close()
        rc, out, err = cli("restore-journal", "--apply")
        self.assertEqual(rc, 0)
        self.assertIn("EMPTY", out)
        self.assertIn("0 records", out)
        # unreadable: same file, permissions gone — the answer must be
        # UNKNOWN (loud, rc 1), never the empty-journal message
        os.chmod(empty, 0)
        try:
            rc, out, err = cli("restore-journal", "--apply")
        finally:
            os.chmod(empty, 0o600)
        self.assertEqual(rc, 1, "unreadable input means an INCOMPLETE view")
        self.assertIn("UNREADABLE", err)
        self.assertIn("INCOMPLETE", err)
        self.assertNotIn("EMPTY", out)

    def test_malformed_meld_lifecycle_is_unknown_and_cli_nonzero(self):
        room = "meld-9-damaged"
        path = meld.lifecycle_path(room, durable=True)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}\n")
        out = chat.restore_journal(apply=True)
        self.assertEqual(out["state"], "UNKNOWN")
        self.assertEqual(out["meld"]["rooms"][room]["state"], "UNKNOWN")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = chat.cmd_chat(["restore-journal", "--apply"])
        self.assertEqual(rc, 1)
        self.assertIn("meld lifecycle", stderr.getvalue())
        self.assertNotIn("no live melds", stdout.getvalue())

    def test_meld_lifecycle_unknown_reasons_are_secret_safe(self):
        secret = "https://alice:pw@example.invalid authorization=Bearer swordfish"
        meld_reports = (
            {"state": "UNKNOWN", "rooms": {}, "reason": secret},
            {"state": "UNKNOWN", "rooms": {
                "meld-9-damaged": {"state": "UNKNOWN", "reason": secret}}},
        )
        for meld_report in meld_reports:
            report = {"state": "UNKNOWN", "rooms": {}, "meta": {},
                      "meld": meld_report}
            stderr = io.StringIO()
            with mock.patch.object(chat, "restore_journal", return_value=report), \
                    contextlib.redirect_stderr(stderr):
                rc = chat.cmd_chat(["restore-journal"])
            self.assertEqual(rc, 1)
            rendered = stderr.getvalue()
            self.assertIn("[redacted]", rendered)
            self.assertNotIn("alice:pw", rendered)
            self.assertNotIn("swordfish", rendered)

    def test_damaged_journal_lines_are_counted_not_silently_dropped(self):
        chat.post("real", room="main", who="alice")
        chat.log_flush()
        # NOT [0] of the bare listing — .chat-flush.json sorts before any
        # chat-*.log and the first cut of this test corrupted THAT instead
        j = sorted(n for n in os.listdir(chat.journal_dir())
                   if n.startswith("chat-") and n.endswith(".log"))[0]
        path = os.path.join(chat.journal_dir(), j)
        body = open(path).read()
        open(path, "w").write("orphan line with no header\n" + body)
        self.wipe_rooms()
        out = chat.restore_journal(apply=True)
        self.assertEqual(out["meta"]["orphan_lines"], 1)
        self.assertEqual([t for t in self.texts("main")[:-1]], ["real"])

    def test_an_absent_journal_is_a_fresh_host_not_an_error(self):
        out = chat.restore_journal(apply=True)
        self.assertEqual(out["state"], "absent")
        self.assertEqual(out["rooms"], {})

    def test_dry_run_writes_NOTHING(self):
        chat.post("gone until applied", room="main", who="alice")
        chat.log_flush()
        self.wipe_rooms()
        out = chat.restore_journal(apply=False)
        self.assertEqual(out["rooms"]["main"]["restored"], 1)
        self.assertEqual(chat.read("main")[1], 0, "dry run must not write")
        self.assertIsNone(seats._cursor("main", "tester"))


class AutoRestoreTest(RestoreBase):
    """The marker-keyed auto-restore latch (r1 findings): keyed on the
    RESTORED cutover marker, fired from BOTH list_rooms and post — the two
    shapes tonight measured: flush-state is PERSISTENT disk (a sentinel
    keyed on it never fires after the first boot in history), and the
    fleet-dark window had a TIMER post first (proxywatch, 23:30Z) which an
    empty-dir or list-only latch reads as 'live traffic' and disarms on."""

    def test_latch_fires_with_persistent_flush_state_present(self):
        """Finding 1: .chat-flush.json survives the reboot (journal_dir is
        disk). The latch must fire in EXACTLY that state — flush state
        present, room dir wiped, journal intact."""
        chat.post("before the reboot", room="main", who="alice")
        chat.log_flush()
        self.assertTrue(os.path.exists(chat._flush_state_path()))
        self.wipe_rooms()                # tmpfs gone; DISK state survives
        self.assertTrue(os.path.exists(chat._flush_state_path()))
        rooms = chat.list_rooms()
        self.assertIn("main", rooms)
        self.assertEqual(self.texts("main")[:-1], ["before the reboot"])
        self.assertIn("RESTORED from the disk journal", self.texts("main")[-1])

    def test_latch_fires_from_a_TIMER_first_post(self):
        """Finding 2: the fleet-dark recovery shape — proxywatch posted row
        [1] while every agent was down. A post into the wiped bus restores
        BEFORE it speaks, so the timer's row lands on top of history."""
        chat.post("before the reboot", room="main", who="alice")
        chat.log_flush()
        self.wipe_rooms()
        chat.post("proxywatch speaks first", room="main", who="proxywatch")
        texts = self.texts("main")
        self.assertIn("before the reboot", texts)
        self.assertEqual(texts[-1], "proxywatch speaks first")
        self.assertTrue(any("RESTORED from the disk journal" in t
                            for t in texts))

    def test_restored_bus_never_re_fires(self):
        """The cutover marker is the discharge: once a room carries it,
        listing and posting are plain hot-path operations again."""
        chat.post("row", room="main", who="alice")
        chat.log_flush()
        self.wipe_rooms()
        chat.list_rooms()                            # fires, restores, marks
        self.assertIn("RESTORED from the disk journal",
                      self.texts("main")[-1])
        before = self.texts("main")
        chat.post("live again", room="main", who="bob")
        chat.list_rooms()
        self.assertEqual(self.texts("main"), before + ["live again"])

    def test_live_traffic_loses_nothing(self):
        """The merge is dedupe-driven, not cutoff-driven: rows posted after
        the journal's last flush survive the restore beside the archive."""
        chat.post("old row", room="main", who="alice")
        chat.log_flush()
        self.wipe_rooms()
        chat.post("live row", room="main", who="bob")
        texts = self.texts("main")
        self.assertEqual(texts[0], "old row")
        self.assertEqual(texts[-1], "live row")
        self.assertTrue(any("RESTORED from the disk journal" in t
                            for t in texts))

    def test_steady_state_pays_one_stat_not_a_journal_scan(self):
        """F3: after the latch discharges, list/post must not re-scan the
        journal — the tmpfs sentinel answers, and a busy room rotating its
        marker row away changes nothing."""
        chat.post("row", room="main", who="alice")
        chat.log_flush()
        self.wipe_rooms()
        chat.list_rooms()                        # fires, restores, stamps
        self.assertTrue(os.path.exists(chat._restored_sentinel()))
        calls = []
        orig = chat._restore_pending
        chat._restore_pending = lambda: (calls.append(1), orig())[1]
        try:
            chat.list_rooms()
            chat.post("more", room="main", who="bob")
        finally:
            chat._restore_pending = orig
        self.assertEqual(calls, [])

    def test_rotation_of_old_rows_does_not_oscillate(self):
        """F3's correctness leg: old rows rotate out under SIZE_CAP while
        the journal still holds them. Dedupe alone would read them as
        pending and re-restore forever; the sentinel holds the discharge
        across rotation."""
        chat.post("old", room="main", who="alice")
        chat.log_flush()
        self.wipe_rooms()
        chat.list_rooms()                        # restore + stamp
        # rotation: the room file is replaced wholesale (the oldest half,
        # marker included, is gone from the live room)
        os.remove(chat.room_path("main"))
        chat.post("new", room="main", who="bob")
        texts = self.texts("main")
        self.assertEqual(texts, ["new"])         # no re-restore of "old"

    def test_sentinel_dies_with_the_boot_so_the_latch_rearms(self):
        """The sentinel is tmpfs: a wipe removes it, and the NEXT boot's
        first list restores again."""
        chat.post("row", room="main", who="alice")
        chat.log_flush()
        self.wipe_rooms()
        chat.list_rooms()
        self.assertTrue(os.path.exists(chat._restored_sentinel()))
        self.wipe_rooms()                        # the reboot
        self.assertFalse(os.path.exists(chat._restored_sentinel()))
        chat.list_rooms()
        self.assertEqual(self.texts("main")[:-1], ["row"])

    def test_restore_pending_is_False_after_restore_and_True_before(self):
        """The marker IS the discharge — a predicate that never goes False
        re-scans the journal on every list/post forever (idempotent by
        dedupe, so no content test can see it; only the predicate tells)."""
        chat.post("row", room="main", who="alice")
        chat.log_flush()
        self.wipe_rooms()
        self.assertTrue(chat._restore_pending())
        chat.restore_journal(apply=True)
        self.assertFalse(chat._restore_pending())

    def test_fresh_host_without_journal_is_a_noop(self):
        self.assertFalse(os.path.isdir(chat.journal_dir()))
        self.assertEqual(chat.list_rooms(), [])
        chat.post("first ever row", room="main", who="alice")
        self.assertEqual(self.texts("main"), ["first ever row"])

    def test_dm_post_never_pays_the_scan(self):
        """DM lanes are never journaled; a DM post must not run the
        restore predicate at all."""
        chat.post("journaled", room="main", who="alice")
        chat.log_flush()
        self.wipe_rooms()
        called = []
        orig = chat._restore_pending
        chat._restore_pending = lambda: (called.append(1), orig())[1]
        try:
            chat.post("secret", dm="bob", who="alice")
        finally:
            chat._restore_pending = orig
        self.assertEqual(called, [])


class LogflushTimerTest(RestoreBase):
    """log-flush --install-timer: the reboot-loss bound. The cadence mirrors
    the INSTALLED unit (2min boot / 3min active) — installed is truth, the
    write-behind cadence is operational (#195, ruled 2026-08-04)."""

    def test_default_cadence_mirrors_the_installed_unit(self):
        self.assertEqual(chat.LOGFLUSH_BOOT_DELAY_S, 120)
        self.assertEqual(chat.LOGFLUSH_INTERVAL_S, 180)
        _sp, _sv, tp, timer = chat._logflush_timer_units()
        self.assertIn("OnBootSec=120s", timer)
        self.assertIn("OnUnitActiveSec=180s", timer)
        self.assertTrue(tp.endswith("helm-chat-logflush.timer"))

    def test_template_cadence_equals_the_constants(self):
        """#195's guard: the SHIPPED template parses back to its own module
        constants, so the next installed-truth change is ONE edit (the
        constant), never a constant/template pair that can drift. Host state
        (~/.config) is deliberately out of scope — installed-is-truth is
        enforced by humans at install time; the suite pins only the internal
        template==constant agreement."""
        _sp, _sv, _tp, timer = chat._logflush_timer_units()
        self.assertIn("OnBootSec=%ds\n" % chat.LOGFLUSH_BOOT_DELAY_S, timer)
        self.assertIn("OnUnitActiveSec=%ds\n" % chat.LOGFLUSH_INTERVAL_S,
                      timer)
        # and these are the ONLY cadence fields — a stray hardcoded Sec=
        # line beside the parsed pair would be exactly the drift this pins.
        self.assertEqual(timer.count("Sec="), 2, timer)

    def test_dry_run_prints_without_writing(self):
        """No --apply == print both units and the install recipe, touch
        nothing. The no-write half is structural: _logflush_install_timer
        only opens a unit path inside the --apply leg."""
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = chat._logflush_install_timer([])
        self.assertEqual(rc, 0)
        body = out.getvalue()
        self.assertIn("helm-chat-logflush.service", body)
        self.assertIn("helm-chat-logflush.timer", body)
        self.assertIn("OnUnitActiveSec=180s", body)
        self.assertIn("--apply", body)
        src = open(chat.__file__, encoding="utf-8").read()
        apply_leg = src.split('if "--apply" not in args:')[1]
        self.assertLess(apply_leg.index("return 0"),
                        apply_leg.index('open(path, "w"'))

    def test_interval_refuses_garbage_and_zero(self):
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = chat._logflush_install_timer(["--interval", "soon"])
        self.assertEqual(rc, 2)
        with contextlib.redirect_stderr(err):
            rc = chat._logflush_install_timer(["--interval", "0"])
        self.assertEqual(rc, 2)

    def test_units_never_capture_a_worktree(self):
        """The derived-WorkingDirectory law: a persistent unit minted from a
        lane worktree must point at the SHARED checkout, and helm at
        ~/.local/bin — a lane path would die with the worktree."""
        sp, service, _tp, _t = chat._logflush_timer_units()
        self.assertIn("ExecStart=%s chat log-flush"
                      % os.path.join(os.path.expanduser("~"),
                                     ".local", "bin", "helm"), service)
        self.assertNotIn("helm-wt", service)
        self.assertNotIn(sp, service)


class ReplayDeliveryTest(RestoreBase):
    """The composition the P0 names: a restored row is readable history but
    mints NO delivery obligation and wakes NO beacon — the two live harms
    measured 2026-08-03 (a 12-day-old owner directive one step from being
    acted on; two seats designing inside a 35-hour-old replayed meld)."""

    def test_a_restored_mention_is_readable_but_never_delivered(self):
        chat.post("go do the thing @alice", room="main", who="daria")
        chat.log_flush()
        self.wipe_rooms()
        chat.restore_journal(apply=True)
        # READABLE as history — the row is there, marked
        rows, _ = chat.read("main")
        restored = [r for r in rows if "go do the thing" in str(r.get("text"))]
        self.assertTrue(restored)
        self.assertTrue(all(r.get("restored") for r in restored))
        # but the delivery path drops it: no obligation, no wake
        from helm import seats
        self.assertFalse(any(seats.deliverable(r, "alice") for r in restored),
                         "a restored mention reached the delivery path")

    def test_an_UNMARKED_old_row_still_delivers(self):
        """The guard keys on the restored FLAG, not age: a live row that is
        merely old (a seat's cursor far behind) still delivers — dropping it
        would be the visibility change the brief forbids."""
        from helm import seats
        old_row = {"ts": "2026-07-21T19:56:00Z", "from": "daria",
                   "text": "@alice old but live"}
        self.assertTrue(seats.deliverable(old_row, "alice"),
                        "an unmarked old row must still deliver")


class CliTest(RestoreBase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chat.cmd_chat(list(args))
        return rc, out.getvalue() + err.getvalue()

    def test_junk_tail_refuses_before_any_write(self):
        """The APPLY_READER_EXEMPT probe: this verb rewrites room files, so a
        junk token refuses rc 2 BEFORE restore_journal is reached — the
        `seat down codex --bogus --help` class, closed at birth."""
        from unittest import mock
        for argv in (["restore-journal", "--bogus", "--apply"],
                     ["restore-journal", "--bogus"],
                     ["restore-journal", "--apply", "extra"]):
            with mock.patch.object(chat, "restore_journal") as p:
                rc, out = self.run_cli(*argv)
            self.assertEqual(rc, 2, (argv, out))
            self.assertFalse(p.called, argv)

    def test_verb_dry_runs_by_default_and_applies_on_flag(self):
        chat.post("cli row", room="main", who="alice")
        chat.log_flush()
        self.wipe_rooms()
        rc, out = self.run_cli("restore-journal")
        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN", out)
        self.assertEqual(chat.read("main")[1], 0)
        rc, out = self.run_cli("restore-journal", "--apply")
        self.assertEqual(rc, 0)
        self.assertIn("restored 1 room", out)
        self.assertGreater(chat.read("main")[1], 0)


if __name__ == "__main__":
    unittest.main()


class FlushIsolationTest(RestoreBase):
    """One diverged meld must not discard the flush of every other room.

    Measured 2026-08-11: a 3-minute timer fired ~160 consecutive times and wrote
    ZERO rows for eight hours, because meld.flush_lifecycle raised at the top of
    log_flush and the exception propagated before the per-room loop ran. 110 of
    156 rooms were flushable the whole time. These arms fail on that code: the
    first two by exception, the rest by content.
    """

    def test_a_raising_lifecycle_leg_still_flushes_every_healthy_room(self):
        chat.post("coordination row the fleet cannot lose", room="helm", who="alice")
        chat.post("second room, also healthy", room="side", who="bob")
        boom = meld.LifecycleError(
            "meld lifecycle durable journal diverges from RAM for meld-x")
        report = {}
        with mock.patch.object(meld, "flush_lifecycle", side_effect=boom):
            appended = chat.log_flush(report=report)
        # THE WHOLE POINT: rows reached disk despite the lifecycle leg failing.
        self.assertGreater(appended, 0)
        journal = self.journal_text()
        self.assertIn("coordination row the fleet cannot lose", journal)
        self.assertIn("second room, also healthy", journal)

    def test_the_global_lifecycle_failure_is_named_in_the_report(self):
        chat.post("healthy", room="helm", who="alice")
        boom = meld.LifecycleError("meld lifecycle RAM directory UNKNOWN: nope")
        report = {}
        with mock.patch.object(meld, "flush_lifecycle", side_effect=boom):
            chat.log_flush(report=report)
        # THE GATE CORRECTED THIS ARM. It first asserted `quarantined` was
        # non-empty and got {} — rightly, because this fixture has no meld room
        # to skip, so quarantining nothing is the correct outcome. The fact the
        # caller actually needs is that the LIFECYCLE LEG IS DOWN, which is
        # independent of whether any room had to be skipped for it.
        self.assertTrue(report.get("lifecycle_down"),
                        "a flush whose lifecycle leg failed must SAY so, even "
                        "when no room needed quarantining")
        self.assertIn("RAM directory UNKNOWN", report["lifecycle_down"])
        self.assertEqual(report["quarantined"], {})   # nothing to skip: correct

    def test_a_quarantined_meld_gets_no_partial_rendered_flush(self):
        """The coherence rule the old ordering protected, kept by NAME."""
        chat.post("healthy room row", room="helm", who="alice")
        chat.post("meld row that must NOT reach disk", room="meld-bad", who="bob")
        report = {}
        with mock.patch.object(
                meld, "flush_lifecycle",
                return_value=(0, {"meld-bad": "diverges from RAM"})):
            chat.log_flush(report=report)
        journal = self.journal_text()
        self.assertIn("healthy room row", journal)
        self.assertNotIn("meld row that must NOT reach disk", journal)
        self.assertIn("meld-bad", report.get("quarantined") or {})

    def test_one_unreadable_room_does_not_discard_the_rooms_after_it(self):
        """The room loop had no per-room guard: same blast radius, one level down.

        THE FAULT IS PLANTED IN THE BYTES, and it used to be planted in the
        READER — a `chat.read` double raising OSError. That double stopped
        being in effect the moment _flush_one_room started reading through the
        strict door instead, and a fixture aimed at a function the code no
        longer calls proves nothing about the loop it claims to measure. A room
        whose own bytes are damaged needs no cooperation from any test double,
        and it is what the incident actually looked like.

        `aaa-` and `zzz-` are load-bearing: list_rooms() sorts, so the damaged
        room is walked FIRST and everything after it is what the old code
        discarded."""
        chat.post("row in the doomed room", room="aaa-doomed", who="alice")
        chat.post("row in the room that comes after", room="zzz-healthy", who="bob")
        with open(chat.room_path("aaa-doomed"), "ab") as f:
            f.write(b'{"ts": "2026-08-11T17:15:00Z", "from": "alice", "text": "hal')
        report = {}
        appended = chat.log_flush(report=report)
        journal = self.journal_text()
        self.assertGreater(appended, 0)
        self.assertIn("row in the room that comes after", journal)
        self.assertIn("aaa-doomed", report.get("quarantined") or {})
        # AND THE DAMAGED ROOM WAS NOT PARTIALLY FLUSHED. read() would have
        # handed over the intact rows and dropped the torn one in silence,
        # advancing the cursor past a row a repair could still recover.
        self.assertNotIn("row in the doomed room", journal)

    def test_a_healthy_flush_reports_no_quarantine(self):
        """NEGATIVE CONTROL: the quarantine must not fire on a clean run, or
        every arm above would pass against a function that refuses everything."""
        chat.post("nothing wrong here", room="helm", who="alice")
        report = {}
        appended = chat.log_flush(report=report)
        self.assertGreater(appended, 0)
        # POSITIVE CONTROL ON THE SAME OBSERVABLE, because asserting a field is
        # EMPTY passes just as well when the field was never written at all —
        # the vacuous-assertion rung caught exactly that hole here.
        self.assertIn("quarantined", report)
        self.assertIn("flushed_rooms", report)
        self.assertGreater(report["flushed_rooms"], 0)
        self.assertEqual(report["quarantined"], {})
        self.assertIn("nothing wrong here", self.journal_text())

    # -- helper ------------------------------------------------------------
    def journal_text(self):
        d = chat.journal_dir()
        if not os.path.isdir(d):
            return ""
        out = []
        for name in sorted(os.listdir(d)):
            with open(os.path.join(d, name), encoding="utf-8") as f:
                out.append(f.read())
        return "\n".join(out)


class LifecyclePrefixTest(RestoreBase):
    """durable AHEAD of RAM is the correct steady state, not a divergence.

    Measured 2026-08-11 across all 248 lifecycle journal pairs: 46 durable-ahead,
    78 equal, ZERO with RAM ahead — not one corrupt. The old predicate demanded
    durable be an exact PREFIX of RAM, so after any tmpfs wipe without a restore
    (i.e. after every reboot) it could never accept the journal again, and the
    3-minute flush timer failed permanently.
    """

    def _drive(self, ram_unique, durable_unique):
        """Run one room through the lifecycle predicate with controlled journals.
        Returns (appended, append_mock) so an arm can assert on the WRITES, not
        only on the count — a count of 0 is produced by a broken mock just as
        readily as by a correct reconcile."""
        reduced = [(None, ram_unique, None, None), (None, durable_unique, None, None)]
        with mock.patch.object(meld, "_events", return_value=([], None)), \
             mock.patch.object(meld, "_reduce", side_effect=reduced) as reducer, \
             mock.patch.object(meld.eventledger, "append",
                               return_value=True) as appender:
            return meld._flush_one_lifecycle("meld-probe"), appender, reducer

    def test_durable_ahead_of_ram_reconciles_instead_of_raising(self):
        """THE 46. Disk remembers more than RAM; that is what disk is FOR."""
        appended, appender, reducer = self._drive(
            ram_unique=["a", "b"], durable_unique=["a", "b", "c", "d"])
        # UNCONDITIONAL POSITIVE CONTROL: both journals were actually read,
        # so the predicate was REACHED. Without it every assertion here is
        # an absence, and a function that returned early — or a mock that
        # never ran — satisfies them all.
        self.assertEqual(reducer.call_count, 2)
        self.assertEqual(appended, 0)
        # STRUCTURAL, because a 0 alone proves nothing about what happened:
        # reconcile means NOTHING WAS WRITTEN — not appended, and (the danger
        # the diagnosis named explicitly) not truncated either.
        self.assertEqual(appender.call_count, 0)

    def test_durable_behind_ram_still_appends_the_tail(self):
        """POSITIVE CONTROL: the ordinary write-behind case must still work, or
        'reconciles' could mean 'never appends anything again'."""
        appended, appender, _reducer = self._drive(
            ram_unique=["a", "b", "c"], durable_unique=["a"])
        self.assertEqual(appended, 2)
        self.assertEqual(appender.call_count, 2)   # the tail actually got WRITTEN

    def test_a_genuine_fork_still_raises(self):
        """NEGATIVE CONTROL: neither a prefix of the other is the ONLY state this
        error was ever meant to name. Without this arm the fix could accept
        everything and both arms above would still pass."""
        with self.assertRaises(meld.LifecycleError) as caught:
            self._drive(ram_unique=["a", "x"], durable_unique=["a", "y"])
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: assertRaises alone is
        # satisfied by ANY LifecycleError, including one raised for an unrelated
        # reason earlier in the function, so the arm would pass while proving
        # nothing about the fork predicate.
        self.assertIn("diverges from RAM", str(caught.exception))
        self.assertIn("meld-probe", str(caught.exception))


class FlushWatchdogTest(RestoreBase):
    """A REPEATEDLY-FAILING DURABILITY TIMER MUST BE LOUD.

    Measured 2026-08-11: helm-chat-logflush.timer fired faithfully every ~3
    minutes and wrote ZERO rows for EIGHT HOURS — roughly 160 consecutive
    failures without one alert, with every row of the fleet's morning chat in
    RAM only and the owner about to restart the machine.

    THE ONLY ASSERTION THAT MEANS ANYTHING HERE IS "THE MESSAGE EXISTS". An arm
    that asserts nothing was raised is worthless against this class by
    construction: the outage raised on schedule, printed on schedule, and
    reached nobody. So every arm below reads a delivered row out of the
    obligated seat's lane.
    """

    def setUp(self):
        super().setUp()
        # Name the addressee explicitly rather than inheriting the fleet's
        # default lander: an arm that hardcodes a real teammate's seat name
        # becomes a rename hazard, and HELM_LANDER is the documented override.
        self.prior_lander = os.environ.get("HELM_LANDER")
        os.environ["HELM_LANDER"] = "watchdog-probe-lander"

    def tearDown(self):
        if self.prior_lander is None:
            os.environ.pop("HELM_LANDER", None)
        else:
            os.environ["HELM_LANDER"] = self.prior_lander
        super().tearDown()

    # -- helpers -----------------------------------------------------------
    def alerts(self):
        """Every message actually DELIVERED to the obligated seat's lane."""
        rows, _total = chat.read(chat.dm_room(os.environ["HELM_LANDER"]))
        return [r.get("text") or "" for r in rows]

    def room_alerts(self):
        """Every alarm shouted into the fleet-ops room, by author — the OTHER
        channel, and the one that carries the alert when the addressee is not
        on the roster."""
        rows, _total = chat.read(chat.FLUSH_ALERT_ROOM)
        return [r.get("text") or "" for r in rows
                if r.get("from") == chat.FLUSH_WATCHDOG_WHO]

    @contextlib.contextmanager
    def broken_lifecycle(self, why="durable journal diverges from RAM for meld-x"):
        with mock.patch.object(meld, "flush_lifecycle",
                               side_effect=meld.LifecycleError(why)):
            yield

    def run_flush(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chat.cmd_chat(["log-flush"])
        return rc, out.getvalue() + err.getvalue()

    def join_seat(self, seat, session):
        """Join a seat this fixture is not declared as — and PROVE the row.

        TRUNK CONTRACT (`helm/seats_join.join`): an explicit `--seat` that
        DISAGREES with the process's declared identity is refused WHOLE — no
        roster row, no cursor — because honouring it would move the roster
        while `post`/`react` keep stamping the declared name. RestoreBase
        declares HELM_CHAT_NAME=tester, so a bare `seats.join(seat=...)` from
        these arms was refused and the roster stayed EMPTY.

        THE REFUSAL IS A RETURN VALUE, NEVER A RAISE, and that is what made it
        invisible: both arms below discriminate JOINED from ABSENT, an empty
        roster answers UNKNOWN to BOTH, and so they kept running, kept
        asserting, and measured nothing about membership at all. The fixture
        therefore declares the identity it is joining as (the substrate's own
        `declaring` door, which restores the prior value including its
        absence) and asserts THE ROSTER ROW rather than the absence of a
        complaint — this setup may not be able to fail silently again.
        """
        with _declaring(["--seat", seat]):
            seats.join(session=session, seat=seat, cwd=self.tmp)
        self.assertEqual(
            seats.recipient_capability(seat)["membership"], "JOINED",
            "fixture join was REFUSED — the roster never got the row, so the "
            "arm below would read UNKNOWN and discriminate nothing")

    # -- the acceptance ----------------------------------------------------
    def test_the_SECOND_consecutive_failure_DELIVERS_A_MESSAGE(self):
        """RED THEN GREEN INSIDE ONE ARM, on ONE lane: it is read after run 1
        (nothing) and after run 2 (the alert). The empty read means something
        only because the read that follows it proves the lane, the addressee and
        the reader all work — measured, not assumed."""
        chat.post("the morning's coordination", room="helm", who="alice")
        with self.broken_lifecycle():
            self.run_flush()
            after_one_failure = len(self.alerts())
            self.run_flush()
            landed = self.alerts()
        self.assertEqual(len(landed), 1,
                         "the SECOND consecutive failure must wake somebody")
        self.assertIn("CHAT DURABILITY FAILING", landed[0])
        self.assertIn("2 consecutive runs", landed[0])
        self.assertIn("diverges from RAM", landed[0])   # the actual reason
        self.assertEqual(after_one_failure, 0,
                         "one failed run is a room mid-rotation, not an alarm")

    def test_a_flush_that_REFUSES_EVERY_ROOM_is_a_failure_though_it_raises_NOTHING(self):
        """The exact shape isolation created. Before per-room isolation this
        outage raised; after it, the same outage returns 0 and raises nothing —
        which is also what a healthy quiet fleet returns. A watchdog reading the
        int alone is blind to precisely the incident it was built for."""
        every_room_refused = {"quarantined": {"a": "diverged", "b": "diverged"},
                              "flushed_rooms": 0, "stranded_rows": 7,
                              "lifecycle_down": None}
        state, reason = chat.flush_outcome(0, every_room_refused)
        self.assertEqual(state, "failed")
        self.assertIn("nothing reached disk", reason)
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: identical call shape, healthy
        # report. Without it, a classifier that answered "failed" to everything
        # would satisfy the assertions above.
        healthy = {"quarantined": {}, "flushed_rooms": 12, "stranded_rows": 0,
                   "lifecycle_down": None}
        self.assertEqual(chat.flush_outcome(3, healthy), ("ok", ""))
        # ...and the middle state must be its OWN answer, not folded into either
        partial = {"quarantined": {"meld-bad": "diverged"}, "flushed_rooms": 11,
                   "stranded_rows": 4, "lifecycle_down": None}
        self.assertEqual(chat.flush_outcome(9, partial)[0], "degraded")

    def test_a_persistent_failure_RE_ALERTS_on_doubling(self):
        """160 alerts teaches the fleet to filter the alarm; ONE alert renders
        an eight-hour outage as a six-minute-old message. Doubling gives 2, 4,
        8 — each louder than the last by construction."""
        chat.post("row", room="helm", who="alice")
        with self.broken_lifecycle():
            for _ in range(8):
                self.run_flush()
        got = self.alerts()
        self.assertEqual(len(got), 3, "expected alerts at streak 2, 4 and 8")
        self.assertIn("2 consecutive runs", got[0])
        self.assertIn("4 consecutive runs", got[1])
        self.assertIn("8 consecutive runs", got[2])

    def test_recovery_CLOSES_the_alarm_and_re_arms_it(self):
        """An alert that never says 'resolved' leaves the reader guessing, and a
        streak that never resets can only ever fire once per host."""
        chat.post("row", room="helm", who="alice")
        with self.broken_lifecycle():
            self.run_flush()
            self.run_flush()
        self.assertEqual(len(self.alerts()), 1)
        chat.post("later row", room="helm", who="alice")
        self.run_flush()                                   # healthy again
        closed = self.alerts()
        self.assertEqual(len(closed), 2)
        self.assertIn("CHAT DURABILITY RESTORED", closed[1])
        healed = chat.flush_health()
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: a health record that was
        # never written also reads streak 0, so the state must be read from the
        # same dict to prove the reset was recorded rather than merely absent.
        self.assertEqual(healed["state"], "ok")
        self.assertEqual(healed["streak"], 0)
        # RE-ARMED: a second outage must alert again, not stay latched shut.
        with self.broken_lifecycle():
            self.run_flush()
            self.run_flush()
        self.assertEqual(len(self.alerts()), 3)
        self.assertIn("CHAT DURABILITY FAILING", self.alerts()[2])

    def test_an_UNDELIVERED_alert_is_NEVER_recorded_as_alerted(self):
        """The row's own defect, rebuilt inside its cure: a state field claiming
        somebody was told. When delivery fails the streak must stay unmarked so
        the very next run tries again."""
        chat.post("row", room="helm", who="alice")
        with self.broken_lifecycle():
            with mock.patch.object(chat, "_deliver_flush_alert",
                                   return_value=False) as dead:
                self.run_flush()
                self.run_flush()
                while_transport_down = len(self.alerts())
            self.run_flush()                       # transport back up
        landed = self.alerts()
        self.assertEqual(len(landed), 1, "a failed send must be retried")
        self.assertIn("3 consecutive runs", landed[0])
        # THE DOUBLE WAS IN EFFECT (it was reached exactly once, at streak 2)
        # and nothing reached the lane while it was.
        self.assertEqual(dead.call_count, 1)
        self.assertEqual(while_transport_down, 0)

    def test_a_SCOPED_repair_run_never_touches_the_fleet_health_record(self):
        """`--room helm` is what an operator types while REPAIRING an outage.
        Counting it would let one hand-flushed room reset the streak that
        watches the timer's all-rooms run — the health record would read 'ok'
        while the timer kept failing, which is the silence this row exists to
        break."""
        chat.post("row", room="helm", who="alice")
        with self.broken_lifecycle():
            self.run_flush()
            self.run_flush()
        self.assertEqual(chat.flush_health()["streak"], 2)
        self.assertEqual(len(self.alerts()), 1)
        chat.post("repaired row", room="helm", who="alice")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            chat.cmd_chat(["log-flush", "--room", "helm"])
        # THE STREAK SURVIVES THE REPAIR RUN. The positive control is the
        # repair's own EFFECT ON DISK, never a row COUNT — the gate caught this
        # arm asserting "appended 1 row" and measuring 2, correctly: the alarm
        # posts its own alert into #helm, so that row is one the repair flushes
        # too. A count here would have pinned the alarm's volume to this arm and
        # reddened it on the next change to either.
        self.assertIn("repaired row", self.journal_text())
        self.assertEqual(chat.flush_health()["streak"], 2)

    def journal_text(self):
        d = chat.journal_dir()
        if not os.path.isdir(d):
            return ""
        return "\n".join(
            open(os.path.join(d, n), encoding="utf-8").read()
            for n in sorted(os.listdir(d)) if n.startswith("chat-"))

    # -- the cursor age ----------------------------------------------------
    def test_the_cursor_age_is_READABLE_and_a_quiet_fleet_does_not_cry_wolf(self):
        """THE CHEAPEST POSSIBLE DETECTOR, and nothing was reading it: an
        eight-hour cursor behind a three-minute timer."""
        fresh = chat.flush_health()
        self.assertIsNone(fresh["age_s"],
                          "'never flushed' and 'flushed an hour ago' are "
                          "different sentences and must not render alike")
        chat.post("row", room="helm", who="alice")
        self.run_flush()
        flushed = chat.flush_health()
        self.assertIsNotNone(flushed["age_s"])
        self.assertLess(flushed["age_s"], 60)
        self.assertEqual(flushed["state"], "ok")
        self.assertEqual(flushed["interval_s"], chat.LOGFLUSH_INTERVAL_S)
        # A QUIET FLEET IS NOT A STALL: no new rows, so nothing is written and
        # the cursor does not move — the age must still read fresh, or every
        # idle night is an outage.
        self.run_flush()
        self.assertLess(chat.flush_health()["age_s"], 60)

    def test_doctor_SAYS_the_eight_hour_stall_in_words_a_human_reads(self):
        """The operator surface is the deliverable, not the state file. This is
        the row that would have named the outage at 07:37 instead of 10:27."""
        from helm import doctor
        chat.post("row", room="helm", who="alice")
        self.run_flush()
        healthy = doctor.check_chat_durability()
        self.assertTrue(healthy, "the check must EMIT a row, not stay silent")
        self.assertEqual([lvl for lvl, _ in healthy], [doctor.OK])
        self.assertIn("reached disk", healthy[0][1])
        # Now age the record by the measured eight hours. Both facts must move
        # together: the cursor's mtime IS one of the two inputs, so ageing only
        # the health file would leave the newer cursor answering for it.
        eight_hours = time.time() - 8 * 3600
        h = pk.read_json(chat._flush_health_path(), {})
        h["last_ok"] = eight_hours
        pk.write_json(chat._flush_health_path(), h)
        os.utime(chat._flush_state_path(), (eight_hours, eight_hours))
        stalled = doctor.check_chat_durability()
        levels = [lvl for lvl, _ in stalled]
        self.assertIn(doctor.FAIL, levels)
        said = " ".join(msg for _, msg in stalled)
        self.assertIn("8h ago", said)
        self.assertIn("RAM ONLY", said)

    def test_doctor_NAMES_the_quarantined_rooms_and_the_stranded_rows(self):
        """A quarantined room stays undurable forever — there is a detector and
        no cure today — so the operator surface must keep saying its name."""
        from helm import doctor
        chat.post("healthy row", room="helm", who="alice")
        chat.post("meld row", room="meld-stuck", who="bob")
        report = {}
        with mock.patch.object(
                meld, "flush_lifecycle",
                return_value=(0, {"meld-stuck": "diverges from RAM"})):
            chat.flush_watchdog(chat.log_flush(report=report), report)
        said = " ".join(msg for _, msg in doctor.check_chat_durability())
        self.assertIn("meld-stuck", said)
        self.assertIn("stranded in RAM", said)
        # After a clean run the name must be GONE, or this row would pin a
        # permanent scare that no repair could ever clear.
        chat.post("all better", room="helm", who="alice")
        self.run_flush()
        repaired = " ".join(m for _, m in doctor.check_chat_durability())
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: a check that returned NO
        # rows at all would satisfy the assertNotIn below while proving
        # nothing, so the same string must be shown to still carry a reading.
        self.assertIn("reached disk", repaired)
        self.assertNotIn("meld-stuck", repaired)

    def test_an_operator_disable_is_reported_as_the_EXPOSURE_it_is(self):
        """HELM_CHAT_LOG=0 is a choice, not a fault — and it still means the
        rooms are tmpfs with no durable copy at all, which a health surface
        must say out loud rather than render as silence."""
        from helm import doctor
        os.environ["HELM_CHAT_LOG"] = "0"
        rows = doctor.check_chat_durability()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], doctor.WARN)
        self.assertIn("DISABLED", rows[0][1])
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: with the leg back on, the
        # disable row is replaced by a real reading rather than merely absent.
        os.environ.pop("HELM_CHAT_LOG")
        back = doctor.check_chat_durability()
        self.assertNotIn("DISABLED", back[0][1])

    def test_a_JOINED_addressee_is_DM_ed_and_the_room_is_left_alone(self):
        """One live reader needs ONE message. A fleet-ops room that also gets
        every alarm is how #helm becomes unreadable and the next one is
        skimmed."""
        self.join_seat(os.environ["HELM_LANDER"], "sid-lander")
        chat.post("row", room="helm", who="alice")
        with self.broken_lifecycle():
            self.run_flush()
            self.run_flush()
        dm_lane, shouted = self.alerts(), len(self.room_alerts())
        self.assertEqual(len(dm_lane), 1)
        self.assertIn("CHAT DURABILITY FAILING", dm_lane[0])
        self.assertEqual(shouted, 0, "one live reader needs one message")

    def test_an_ABSENT_addressee_never_swallows_the_alarm_into_a_DEAD_LANE(self):
        """seats.dm is fail-open and SUCCEEDS for a seat that never joined, so
        the alarm would have parked in an unread lane and returned a green
        light — the founding defect wearing a success return. The roster says
        nobody holds that name, so the room gets it instead."""
        self.join_seat("some-other-seat", "sid-other")
        chat.post("row", room="helm", who="alice")
        with self.broken_lifecycle():
            self.run_flush()
            self.run_flush()
        shouted, dead_lane = self.room_alerts(), len(self.alerts())
        self.assertEqual(len(shouted), 1, "the alarm must reach a live reader")
        self.assertIn("CHAT DURABILITY FAILING", shouted[0])
        self.assertIn("@" + os.environ["HELM_LANDER"], shouted[0])
        self.assertEqual(dead_lane, 0,
                         "a lane with no seat behind it is not a delivery")

    def test_a_CHRONIC_degraded_run_never_buys_SILENCE_for_a_TOTAL_failure(self):
        """The rate limiter's own hazard, found by re-reading it against the
        incident it was written for. Doubling is right for ONE condition
        persisting and exactly wrong across an ESCALATION: a room quarantined
        for two days pushes the next scheduled alert two days out, so the
        lifecycle leg going fully down inside that window would be silent for
        two days — the eight-hour silence this row exists to kill, rebuilt
        inside its own alarm."""
        chat.post("healthy row", room="helm", who="alice")
        chat.post("meld row", room="meld-stuck", who="bob")
        with mock.patch.object(
                meld, "flush_lifecycle",
                return_value=(0, {"meld-stuck": "diverges from RAM"})):
            for _ in range(4):
                self.run_flush()
        degraded = self.alerts()
        # POSITIVE CONTROL: the ordinary doubling schedule is intact (2 and 4),
        # so the escalation below is measured against a working limiter rather
        # than against one that fires on everything.
        self.assertEqual(len(degraded), 2)
        self.assertIn("refused", degraded[1])
        with self.broken_lifecycle():
            self.run_flush()
        escalated = self.alerts()
        self.assertEqual(len(escalated), 3,
                         "a WORSENING must not wait for the doubling")
        self.assertIn("meld lifecycle leg DOWN", escalated[2])

    def test_an_IMPROVEMENT_is_not_an_alarm(self):
        """NEGATIVE CONTROL for the arm above. Without it the escalation cure
        could simply fire on every state change, and both arms would pass."""
        chat.post("healthy row", room="helm", who="alice")
        chat.post("meld row", room="meld-stuck", who="bob")
        with self.broken_lifecycle():
            self.run_flush()
            self.run_flush()
        after_failure = len(self.alerts())
        with mock.patch.object(
                meld, "flush_lifecycle",
                return_value=(0, {"meld-stuck": "diverges from RAM"})):
            self.run_flush()
        eased = self.alerts()
        self.assertEqual(after_failure, 1)
        self.assertEqual(len(eased), 1,
                         "failed easing back to degraded is an improvement")

    def test_an_UNKNOWN_membership_takes_its_EVIDENCE_from_the_ROOM_leg(self):
        """THE FOUNDING DEFECT, REBUILT INSIDE THE ALARM WRITTEN TO CURE IT.

        The whole row exists because a send SUCCEEDED into a lane nobody reads.
        `_deliver_flush_alert` answered UNKNOWN with `_post_alert(...) or
        dm_landed` — and dm_landed comes from `seats.dm`, which is FAIL-OPEN
        and returns a row for a seat that never joined. So when the roster could
        not say whether the addressee exists, the DM's fail-open success stood
        in for the #helm leg it was explicitly paired with BECAUSE the DM might
        reach nobody. One failed room post and the alarm marked itself
        delivered, the streak went quiet, and an eight-hour outage was
        represented by a message parked in a dead lane.

        The DM still goes out — over-waking is recoverable where silence is not.
        It is simply no longer allowed to ANSWER "was anybody told?".
        """
        # UNKNOWN, production-shaped: an EMPTY roster is not evidence the seat
        # is gone, which is why recipient_capability answers UNKNOWN and not
        # ABSENT. Nothing has joined here.
        self.assertEqual(chat._alert_membership(os.environ["HELM_LANDER"]),
                         "UNKNOWN")
        chat.post("the morning's coordination", room="helm", who="alice")
        with self.broken_lifecycle():
            with mock.patch.object(chat, "_post_alert",
                                   return_value=False) as room_down:
                self.run_flush()
                self.run_flush()
            # POSITIVE CONTROL ON THE SAME OBSERVABLE: the fail-open DM REALLY
            # LANDED in the lane — no stub, the live seats.dm path — so the old
            # `or dm_landed` genuinely had a True to return here. The room leg
            # was reached and refused exactly once, at the streak-2 fire.
            self.assertEqual(len(self.alerts()), 1)
            self.assertEqual(room_down.call_count, 1)
            # AND THE MARK WAS NOT WRITTEN. This field is what silences the
            # next run, so it is the one that must stay honest.
            recorded = pk.read_json(chat._flush_health_path(), {})
            self.assertEqual(recorded.get("alerted_streak") or 0, 0)
            self.assertEqual(recorded.get("streak"), 2)
            # ...so the very next run retries, into the room now that it is up.
            self.run_flush()
        shouted = self.room_alerts()
        self.assertEqual(len(shouted), 1, "an unmarked alert must be retried")
        self.assertIn("3 consecutive runs", shouted[0])
        self.assertIn("@" + os.environ["HELM_LANDER"], shouted[0])
