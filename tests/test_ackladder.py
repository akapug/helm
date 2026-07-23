#!/usr/bin/env python3
"""AX primitive #3 — the ACK / CONSUME LADDER (SENT != SEEN != ACTED).

Hermetic like test_seats: HELM_HOME + HELM_CHAT_DIR are tmp dirs, the signed
transport is killed (HELM_CHAT_NODE_URL set-but-empty), owner names pinned,
and every ambient session/model mark scrubbed so no live harness leaks into
derive_seat/whoname. Seats join with a NON-git tmp cwd so they stay un-homed
(all-room legacy scope) — the ladder is tested off the cursor, not homing."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, pk, seats  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_ROOM", "MELD_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE",
            "MELD_CHAT_ROOM_SOURCE", "HELM_CHAT_OWNER_NAMES",
            "HELM_CHAT_DELIVER", "HELM_CELL_BIN",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "CLAUDECODE", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL")


class LadderBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-ack-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_OWNER_NAMES"] = "david"

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------
    def join(self, seat, session=None):
        seats.join(session=session or ("sess-" + seat), seat=seat,
                   cwd=self.tmp)      # tmp is not a git repo => un-homed

    def mask_old(self, *seats_):
        """Freeze a seat's .seen mtime in the distant past so the SEEN
        derivation can only come from the delivery CURSOR — isolates the
        touch_seen fallback out of the cursor tests."""
        old = time.time() - 100000
        for s in seats_:
            os.utime(seats.seen_path(s), (old, old))

    def states(self, sender="senderS"):
        items, _total = seats.pending(seat=sender)
        return sorted((i["to"], i["state"]) for i in items)

    def run_cmd(self, verb, args=()):
        out, err = io.StringIO(), io.StringIO()
        fake = types.SimpleNamespace(buffer=io.BytesIO(b"{}"))
        with mock.patch.object(sys, "stdin", fake), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = seats.cmd(verb, list(args), "main")
        return rc, out.getvalue(), err.getvalue()


class SentSeenTests(LadderBase):
    def test_sent_then_seen_off_the_cursor(self):
        """A DM is SENT-not-SEEN until the recipient's cursor passes it; then
        SEEN — proven off the CURSOR alone (seen mtime re-masked to the past
        after delivery, so the fallback cannot be the thing that flipped it)."""
        row, err = seats.dm("recipT", "please review the tip", who="senderS")
        self.assertIsNone(err)
        self.join("recipT")
        self.mask_old("recipT")
        self.assertEqual(self.states(), [("recipT", "sent")])

        seats.deliver(session="sess-recipT", room=seats.dm_lane("recipT"),
                      seat="recipT", emit=lambda ln: None)
        self.mask_old("recipT")           # SEEN must now come from the cursor
        self.assertEqual(self.states(), [("recipT", "seen")])

    def test_no_cursor_no_ground_stays_sent(self):
        """A recipient that never joined has no cursor and no fresh presence —
        the row is SENT (stranded), never falsely SEEN."""
        seats.dm("ghost", "anyone home?", who="senderS")
        # ghost never joined: no cursor, no .seen file
        self.assertEqual(self.states(), [("ghost", "sent")])

    def test_touch_seen_fallback_later_second_is_seen(self):
        """The second SEEN signal: activity in a strictly-later second than
        the row (no cursor movement at all). Same-second activity does NOT
        count — it guards against the row's own post-second."""
        row, _ = seats.dm("recipT", "hi", who="senderS")
        self.join("recipT")
        mts = seats._ts_epoch(row["ts"])
        os.utime(seats.seen_path("recipT"), (mts + 5, mts + 5))
        st, _a = seats.consume_state(row, seats.dm_lane("recipT"), "recipT")
        self.assertEqual(st, "seen")
        os.utime(seats.seen_path("recipT"), (mts, mts))    # same second
        st, _a = seats.consume_state(row, seats.dm_lane("recipT"), "recipT")
        self.assertEqual(st, "sent")


class AckTests(LadderBase):
    def test_seen_then_acted_via_ack(self):
        """An explicit ack moves SEEN -> ACTED and the row drops out of the
        sender's pending view (consumed)."""
        row, _ = seats.dm("recipT", "do the thing", who="senderS")
        self.join("recipT")
        seats.deliver(session="sess-recipT", room=seats.dm_lane("recipT"),
                      seat="recipT", emit=lambda ln: None)
        self.mask_old("recipT")
        self.assertEqual(self.states(), [("recipT", "seen")])

        res, err = seats.ack(row["id"], "done", who="recipT")
        self.assertIsNone(err)
        self.assertFalse(res["dup"])
        st, ackstate = seats.consume_state(row, seats.dm_lane("recipT"),
                                           "recipT")
        self.assertEqual((st, ackstate), ("acted", "done"))
        self.assertEqual(self.states(), [])          # hidden once acted

    def test_ack_done_vs_blocked_with_note(self):
        """done carries no reason; blocked carries its reason in the ack row's
        text, and the derived ackstate reflects each."""
        a, _ = seats.dm("recipT", "task A", who="senderS")
        b, _ = seats.dm("recipT", "task B", who="senderS")
        self.join("recipT")

        ra, _ = seats.ack(a["id"], "done", who="recipT")
        self.assertEqual(ra["row"].get("ackstate"), "done")
        self.assertEqual(ra["row"].get("text"), "")

        rb, _ = seats.ack(b["id"], "blocked", note="waiting on creds",
                          who="recipT")
        self.assertEqual(rb["row"].get("ackstate"), "blocked")
        self.assertEqual(rb["row"].get("text"), "waiting on creds")

        st_a, ack_a = seats.consume_state(a, seats.dm_lane("recipT"), "recipT")
        st_b, ack_b = seats.consume_state(b, seats.dm_lane("recipT"), "recipT")
        self.assertEqual((st_a, ack_a), ("acted", "done"))
        self.assertEqual((st_b, ack_b), ("acted", "blocked"))

    def test_ack_nonexistent_id_refused(self):
        res, err = seats.ack("deadbeefdead", "done", who="recipT")
        self.assertIsNone(res)
        self.assertIn("no message matches", err)

    def test_ack_foreign_row_refused(self):
        """Only a recipient can ack — a stranger acking a DM addressed to
        someone else is refused, and nothing is written."""
        row, _ = seats.dm("recipT", "yours only", who="senderS")
        before = len(chat.read(seats.dm_lane("recipT"))[0])
        res, err = seats.ack(row["id"], "done", who="stranger")
        self.assertIsNone(res)
        self.assertIn("not you", err)
        self.assertEqual(len(chat.read(seats.dm_lane("recipT"))[0]), before)

    def test_ack_non_addressed_row_refused(self):
        """A plain room post addresses nobody — there is nothing to ack."""
        row = chat.post("just chatter", room="main", who="senderS")
        res, err = seats.ack(row["id"], "done", who="recipT")
        self.assertIsNone(res)
        self.assertIn("not an addressed message", err)

    def test_double_ack_is_idempotent(self):
        """A repeat identical ack appends NO second row (append-only, but
        idempotent)."""
        row, _ = seats.dm("recipT", "once", who="senderS")
        self.join("recipT")
        r1, _ = seats.ack(row["id"], "done", who="recipT")
        self.assertFalse(r1["dup"])
        lane = seats.dm_lane("recipT")
        n1 = sum(1 for m in chat.read(lane)[0] if m.get("ack") == row["id"])
        r2, _ = seats.ack(row["id"], "done", who="recipT")
        self.assertTrue(r2["dup"])
        n2 = sum(1 for m in chat.read(lane)[0] if m.get("ack") == row["id"])
        self.assertEqual((n1, n2), (1, 1))

    def test_ack_state_change_appends_and_last_wins(self):
        """A DIFFERENT state (done -> blocked) is not a duplicate: it appends,
        and the derived state is the latest ack."""
        row, _ = seats.dm("recipT", "changes", who="senderS")
        self.join("recipT")
        seats.ack(row["id"], "done", who="recipT")
        r2, _ = seats.ack(row["id"], "blocked", note="regressed", who="recipT")
        self.assertFalse(r2["dup"])
        st, ackstate = seats.consume_state(row, seats.dm_lane("recipT"),
                                           "recipT")
        self.assertEqual((st, ackstate), ("acted", "blocked"))

    def test_ack_row_never_wakes(self):
        """An ack marker is state the sender PULLS, never a wake — deliverable
        drops it like a reaction, even with a blocked note as text."""
        row, _ = seats.dm("recipT", "x", who="senderS")
        self.join("recipT")
        res, _ = seats.ack(row["id"], "blocked", note="reason text here",
                           who="recipT")
        ackrow = res["row"]
        self.assertFalse(seats.deliverable(ackrow, "senderS",
                                           seats.dm_lane("recipT")))
        self.assertFalse(seats.deliverable(ackrow, "recipT",
                                           seats.dm_lane("recipT")))


class PendingViewTests(LadderBase):
    def test_pending_shows_unconsumed_hides_acted(self):
        """The sender's pending view shows EXACTLY the un-consumed/un-acted
        rows (SENT and SEEN) and hides the ones a recipient acted on."""
        for s in ("t1", "t2", "t3"):
            self.join(s)
        s1, _ = seats.dm("t1", "still open", who="senderS")           # sent
        s2, _ = seats.dm("t2", "will be acked", who="senderS")        # acted
        chat.post("@t3 look here", room="main", who="senderS")        # sent
        self.mask_old("t1", "t2", "t3")

        seats.ack(s2["id"], "done", who="t2")
        self.assertEqual(self.states(), [("t1", "sent"), ("t3", "sent")])

    def test_partial_ack_of_multi_recipient_keeps_the_rest(self):
        """A mention of two seats: one acks, the other still shows — the unit
        is per (recipient, row), not per row."""
        for s in ("t1", "t3"):
            self.join(s)
        m = chat.post("@t1 and @t3 please", room="main", who="senderS")
        self.mask_old("t1", "t3")
        seats.ack(m["id"], "done", who="t1")
        self.assertEqual(self.states(), [("t3", "sent")])

    def test_pending_ordered_oldest_first(self):
        """Longest-stranded on top. Both rows land in ONE lane (main) so file
        order is deterministic; the OLDER row is posted SECOND with a pinned
        older ts (pk.now_ts is only second-resolution) — so ONLY the ts sort,
        not append order, can float it to the top."""
        self.join("t1")
        self.join("t2")
        with mock.patch.object(pk, "now_ts", return_value="2026-01-01T00:00:09Z"):
            chat.post("@t1 newer", room="main", who="senderS")
        with mock.patch.object(pk, "now_ts", return_value="2026-01-01T00:00:01Z"):
            chat.post("@t2 older", room="main", who="senderS")
        items, total = seats.pending(seat="senderS")
        self.assertEqual(total, 2)
        self.assertEqual([i["ts"] for i in items],
                         ["2026-01-01T00:00:01Z", "2026-01-01T00:00:09Z"])
        self.assertEqual([i["to"] for i in items], ["t2", "t1"])

    def test_pending_empty_when_all_consumed(self):
        row, _ = seats.dm("t1", "solo", who="senderS")
        self.join("t1")
        seats.ack(row["id"], "done", who="t1")
        items, total = seats.pending(seat="senderS")
        self.assertEqual((items, total), ([], 0))

    def test_pending_only_my_outbound(self):
        """pending is scoped to the querying sender — another seat's outbound
        rows never appear."""
        self.join("t1")
        seats.dm("t1", "from S", who="senderS")
        seats.dm("t1", "from other", who="otherSender")
        items, _ = seats.pending(seat="senderS")
        self.assertEqual([i["text"] for i in items], ["from S"])


class CliTests(LadderBase):
    def test_cli_ack_and_pending_roundtrip(self):
        self.join("recipT")
        row, _ = seats.dm("recipT", "cli path", who="senderS")
        self.mask_old("recipT")

        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        self.assertIn("SENT", out)
        self.assertIn(row["id"][:8], out)
        self.assertIn("recipT", out)

        rc, out, err = self.run_cmd(
            "ack", [row["id"], "done", "--seat", "recipT"])
        self.assertEqual(rc, 0, err)
        self.assertIn("acked", out)
        self.assertIn("DONE", out)

        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        self.assertIn("nothing outbound is waiting", out)

    def test_cli_ack_blocked_with_multiword_note(self):
        self.join("recipT")
        row, _ = seats.dm("recipT", "blocked path", who="senderS")
        rc, out, err = self.run_cmd(
            "ack", [row["id"], "blocked", "--seat", "recipT",
                    "--note", "waiting on the creds handoff"])
        self.assertEqual(rc, 0, err)
        self.assertIn("BLOCKED", out)
        self.assertIn("waiting on the creds handoff", out)
        st, ackstate = seats.consume_state(row, seats.dm_lane("recipT"),
                                           "recipT")
        self.assertEqual((st, ackstate), ("acted", "blocked"))

    def test_cli_ack_foreign_refused_nonzero(self):
        row, _ = seats.dm("recipT", "not yours", who="senderS")
        rc, out, err = self.run_cmd(
            "ack", [row["id"], "done", "--seat", "stranger"])
        self.assertEqual(rc, 1)
        self.assertIn("not you", err)

    def test_cli_ack_missing_id_usage(self):
        rc, out, err = self.run_cmd("ack", ["--seat", "recipT"])
        self.assertEqual(rc, 2)
        self.assertIn("usage: helm chat ack", err)

    def test_cli_double_ack_idempotent_message(self):
        self.join("recipT")
        row, _ = seats.dm("recipT", "twice", who="senderS")
        self.run_cmd("ack", [row["id"], "done", "--seat", "recipT"])
        rc, out, _ = self.run_cmd(
            "ack", [row["id"], "done", "--seat", "recipT"])
        self.assertEqual(rc, 0)
        self.assertIn("already acked", out)


class HostileNameSinkTests(LadderBase):
    """The display-launder tripwire (test_display_launder_tripwire) names this
    class as the runtime proof that the consume-ladder sinks strip a planted
    identity. ESC (screen-clear) + BIDI (line reorder) are the two markers."""
    ESC, BIDI = "\x1b[2J", "‮"

    def _hostile_dm(self, sender="senderS"):
        # a row whose recipient carries control/bidi — planted OUTSIDE the
        # validated join seam (chat.post does not re-validate the dm token).
        name = "evil%s%s" % (self.ESC, self.BIDI)
        row = chat.post("payload", who=sender, dm=name)
        return row, name

    def test_pending_launders_planted_recipient(self):
        row, _name = self._hostile_dm()
        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        self.assertNotIn(self.ESC, out)
        self.assertNotIn(self.BIDI, out)
        self.assertIn(row["id"][:8], out)

    def test_ack_refusal_launders_planted_recipient(self):
        row, _name = self._hostile_dm()
        rc, out, err = self.run_cmd(
            "ack", [row["id"], "done", "--seat", "stranger"])
        self.assertEqual(rc, 1)
        self.assertNotIn(self.ESC, err)
        self.assertNotIn(self.BIDI, err)


if __name__ == "__main__":
    unittest.main()
