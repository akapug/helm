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
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-restorej-", var="HELM_HOME")

from helm import chat, meld, seats  # noqa: E402

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
    """The marker-keyed auto-restore latch (hc2 r1 findings): keyed on the
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
        chat.post("go do the thing @alice", room="main", who="owner")
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
        old_row = {"ts": "2026-07-21T19:56:00Z", "from": "owner",
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
