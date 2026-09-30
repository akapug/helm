#!/usr/bin/env python3
"""An ack is a receipt, not conversation.

THE SYMPTOM THESE ARMS PIN. A seat cleared its doorbell's owed count by
running `helm chat ack <id> done --note "handled in-session"` once per row:
31 calls, 31 room rows, and `helm chat read` and the web chat printed every
one of them as a line of conversation. The doorbell (task/3154) made an ack
the way a row leaves the count, and `helm chat ack` took one id, so a seat
that had handled 31 rows had one way to say so: 31 rows.

Two rules were missing:
  * `helm chat ack` takes many ids in one call and writes ONE row per room
    (a room holds the ack for its own rows: the doorbell, the hook and the
    stop guard read an ack in its target's room), naming every id.
  * A read folds a receipt: consecutive DONE acks from one sender print as
    one line that counts the rows they acked. A single ack still prints as
    it did. A blocked ack carries a reason its sender must read, so it never
    folds.

Every arm runs the CLI doors (`helm chat ack`, `helm chat read`) in the
ladder's hermetic home. The web chat's fold is pinned in
tests/test_web_chat_ack_fold.py, and the doorbell, hook and stop guard in
tests/test_doorbell_release.py.
"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, pull_delivery, seats, seats_ack  # noqa: E402
from tests._tmphome import declare as _tmp_declare  # noqa: E402
from tests.test_ackladder import LadderBase  # noqa: E402


def _ack_row(target, who="recipT", note="handled in-session", room="main",
             state="done"):
    """An ack row written straight to the room, past the CLI door: `target`
    is one id, or a list of ids for one bulk row."""
    return chat.post(note, room=room, who=who, ack=target, ackstate=state)


class ReadBase(LadderBase):

    def read_lines(self, room="main"):
        """`helm chat read --room ROOM` through the CLI door -> its lines."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chat.cmd_chat(["read", "--room", room])
        self.assertEqual(rc, 0, err.getvalue())
        return [ln for ln in out.getvalue().splitlines() if ln.strip()]

    def ack_lines(self, lines):
        return [ln for ln in lines if " ACK " in ln]


class ReadFoldsAckRunsTest(ReadBase):

    def test_acks_without_the_ack_discriminator_are_not_receipts(self):
        """A forged/malformed `acks` field alone must hide no row."""
        malformed = {"from": "recipT", "acks": ["a" * 12, "b" * 12],
                     "ackstate": "done"}
        self.assertEqual(chat.ack_ids(malformed), ())
        self.assertEqual(chat.ack_ids({"ack": "a" * 12,
                                       "acks": ["b" * 12]}), ("a" * 12,))
        self.assertEqual(chat.ack_runs([(0, malformed), (1, malformed)]),
                         [[(0, malformed)], [(1, malformed)]])

    def test_31_acks_from_one_sender_read_as_one_line(self):
        """The incident's shape. RED before the cure: 33 lines, 31 of them
        `ACK DONE -> <id>: handled in-session`."""
        chat.post("before", room="main", who="senderS")
        for i in range(31):
            _ack_row("%012x" % i)
        chat.post("after", room="main", who="senderS")
        lines = self.read_lines()
        self.assertEqual(len(lines), 3, lines)
        acks = self.ack_lines(lines)
        self.assertEqual(len(acks), 1, lines)
        self.assertIn("recipT ACK DONE -> 31 rows", acks[0])
        self.assertIn("handled in-session", acks[0])
        self.assertIn("before", lines[0])
        self.assertIn("after", lines[2])

    def test_one_bulk_row_reads_as_the_rows_it_acks(self):
        """One row naming 31 ids reads as 31 rows acked. RED before the
        cure: it read as an ack of its first id alone."""
        _ack_row(["%012x" % i for i in range(31)])
        acks = self.ack_lines(self.read_lines())
        self.assertEqual(len(acks), 1, acks)
        self.assertIn("ACK DONE -> 31 rows", acks[0])

    def test_a_run_counts_the_ids_of_its_bulk_rows(self):
        """Two bulk rows of 3 ids and one single ack, consecutive, from one
        sender: one line, 7 rows."""
        _ack_row(["a" * 12, "b" * 12, "c" * 12])
        _ack_row("d" * 12)
        _ack_row(["e" * 12, "f" * 12, "1" * 12])
        acks = self.ack_lines(self.read_lines())
        self.assertEqual(len(acks), 1, acks)
        self.assertIn("ACK DONE -> 7 rows", acks[0])


class ReadFoldControlTest(ReadBase):
    """What must still print: a single ack as it always has, a blocked ack
    with its reason, and acks the fold must not join."""

    def test_a_single_ack_still_reads_as_one_ack(self):
        chat.post("before", room="main", who="senderS")
        _ack_row("0123456789ab")
        chat.post("after", room="main", who="senderS")
        lines = self.read_lines()
        self.assertEqual(len(lines), 3, lines)
        self.assertIn("recipT ACK DONE -> 01234567: handled in-session",
                      lines[1])

    def test_blocked_acks_never_fold(self):
        """A blocked ack's text is the reason its sender must act on."""
        _ack_row("a" * 12, state="blocked", note="waiting on creds")
        _ack_row("b" * 12, state="blocked", note="needs the owner")
        acks = self.ack_lines(self.read_lines())
        self.assertEqual(len(acks), 2, acks)
        self.assertIn("ACK BLOCKED -> aaaaaaaa: waiting on creds", acks[0])
        self.assertIn("ACK BLOCKED -> bbbbbbbb: needs the owner", acks[1])

    def test_a_blocked_ack_breaks_a_done_run(self):
        _ack_row("a" * 12)
        _ack_row("b" * 12, state="blocked", note="waiting on creds")
        _ack_row("c" * 12)
        acks = self.ack_lines(self.read_lines())
        self.assertEqual(len(acks), 3, acks)

    def test_acks_from_two_senders_do_not_join(self):
        _ack_row("a" * 12, who="recipT")
        _ack_row("b" * 12, who="recipU")
        _ack_row("c" * 12, who="recipT")
        acks = self.ack_lines(self.read_lines())
        self.assertEqual(len(acks), 3, acks)

    def test_a_message_between_acks_breaks_the_run(self):
        _ack_row("a" * 12)
        chat.post("in between", room="main", who="senderS")
        _ack_row("b" * 12)
        lines = self.read_lines()
        self.assertEqual(len(self.ack_lines(lines)), 2, lines)
        self.assertEqual(len(lines), 3, lines)


class FoldedLinesCountOnceTest(unittest.TestCase):
    """A folded run prints ONE line; the pull's whole-read accounting
    (pull_delivery.whole) counts it once, not once per row it folded."""

    def test_rows_folded_into_the_line_before_them_cost_nothing(self):  # noqa: VACUOUS_ASSERTION — the observable is a count, 3002, pinned exactly; the arm below cuts the same accounting to 1
        """RED before the cure: a folded row is None, which whole() could
        not size."""
        lines = ["[1] a first line"] + [None] * 3000 + ["[3002] the last"]
        self.assertEqual(pull_delivery.whole(lines), 3002)

    def test_a_cut_line_still_cuts_the_rows_folded_into_it(self):
        """A line past the head a harness shows is not whole, and neither
        are the rows folded into it."""
        big = "x" * (pull_delivery.FULL_BYTES + 1)
        lines = ["[1] head", big, None, None, "[4] tail"]
        self.assertEqual(pull_delivery.whole(lines), 1)


class BulkAckTest(LadderBase):
    """`helm chat ack <id> <id> ...`: one call, one row per room."""

    def setUp(self):
        super().setUp()
        # an ack is an ACT: the acting session is declared as recipT (the
        # ladder's CliTests fixture), and `--seat recipT` asserts it
        _tmp_declare(self, "recipT")

    def ask(self, n, room="main", to="recipT"):
        return [chat.post("@%s ask %d" % (to, i), room=room, who="senderS")
                for i in range(n)]

    def ack(self, ids, *extra, seat="recipT"):
        """`helm chat ack --seat SEAT IDS EXTRA`: --note eats the rest of the
        line, so the seat goes first."""
        return self.run_cmd("ack", ["--seat", seat] + list(ids) + list(extra))

    def test_one_call_acks_31_rows_with_one_row(self):  # noqa: VACUOUS_ASSERTION — the sender's states() counted 31 before the ack, and the new row names all 31 ids
        """RED before the cure: the second id was read as the ack state and
        the call refused."""
        self.join("recipT")
        rows = self.ask(31)
        self.assertEqual(len(self.states()), 31)
        before = len(chat.read("main")[0])
        rc, out, err = self.ack([r["id"] for r in rows],
                                "--note", "handled in-session")
        self.assertEqual(rc, 0, err)
        after = chat.read("main")[0]
        self.assertEqual(len(after), before + 1, after[before:])
        row = after[-1]
        self.assertEqual(row.get("acks"), [r["id"] for r in rows])
        self.assertEqual(row.get("ack"), rows[0]["id"])
        self.assertEqual((row.get("text"), row.get("ackstate")),
                         ("handled in-session", "done"))
        self.assertEqual(self.states(), [])
        self.assertIn("acked 31 rows DONE", out)

    def test_the_state_word_may_follow_the_ids(self):
        self.join("recipT")
        rows = self.ask(2)
        rc, _out, err = self.ack([r["id"] for r in rows], "blocked",
                                 "--note", "waiting on creds")
        self.assertEqual(rc, 0, err)
        row = chat.read("main")[0][-1]
        self.assertEqual((row.get("acks"), row.get("ackstate")),
                         ([r["id"] for r in rows], "blocked"))

    def test_ids_in_two_rooms_write_one_row_in_each(self):  # noqa: VACUOUS_ASSERTION — each room's last row names that room's ids exactly; the empty states() follows them
        """An ack lives in its target's room, where the doorbell, the hook
        and the stop guard look for it."""
        self.join("recipT")
        here, there = self.ask(2), self.ask(3, room="other-project")
        rc, _out, err = self.ack([r["id"] for r in here + there])
        self.assertEqual(rc, 0, err)
        self.assertEqual(chat.read("main")[0][-1].get("acks"),
                         [r["id"] for r in here])
        self.assertEqual(chat.read("other-project")[0][-1].get("acks"),
                         [r["id"] for r in there])
        self.assertEqual(self.states(), [])

    def test_a_refused_id_writes_nothing(self):  # noqa: VACUOUS_ASSERTION — a refusal that writes nothing is the product law; rc 1 and the refusal text are the positive half
        """The call is whole or nothing: one id addressed to another seat
        refuses the call, and no row is written for the ids that were
        fine."""
        self.join("recipT")
        rows = self.ask(2)
        # the foreign row is a DM to another seat: the declared session is
        # recipT's, so it cannot join a second seat to be @mentioned
        foreign, derr = seats.dm("recipU", "not yours", who="senderS")
        self.assertIsNone(derr, derr)
        before = len(chat.read("main")[0])
        rc, _out, err = self.ack([r["id"] for r in rows] + [foreign["id"]])
        self.assertEqual(rc, 1, err)
        self.assertIn("not you", err)
        self.assertIn("nothing was acked", err)
        self.assertEqual(len(chat.read("main")[0]), before)

    def test_a_typo_id_in_a_mixed_list_refuses_the_whole_call(self):
        """Pin from the mentor's red read: an id that EXISTS NOWHERE (a
        typo) in a mixed list still refuses the WHOLE call and writes
        nothing — a typed id that resolves to no row means the intent is
        unclear, so it is refused, never skipped like an id that resolves
        to a row an ack cannot clear."""
        self.join("recipT")
        rows = self.ask(2)
        before = len(chat.read("main")[0])
        rc, _out, err = self.ack([rows[0]["id"], "deadbeefdead",
                                  rows[1]["id"]])
        self.assertEqual(rc, 1, err)
        self.assertIn("no message matches", err)
        self.assertIn("nothing was acked", err)
        self.assertEqual(len(chat.read("main")[0]), before)

    def test_an_unaddressed_id_in_the_list_is_skipped_not_refused(self):
        """RED before the cure: one @all id in the list refused the whole
        call, so the row the seat actually handled stayed counted. After
        the cure the addressed row acks and the @all id is reported
        skipped: a read or a catchup clears it, an ack cannot. Exactly one
        row is written, and it names the addressed id alone."""
        self.join("recipT")
        row = self.ask(1)[0]
        all_row = chat.post("@all standup", who="senderS")
        before = len(chat.read("main")[0])
        rc, out, err = self.ack([row["id"], all_row["id"]])
        self.assertEqual(rc, 0, err)
        after = chat.read("main")[0]
        self.assertEqual(len(after), before + 1, after[before:])
        self.assertEqual(after[-1].get("ack"), row["id"])
        self.assertNotIn("acks", after[-1])
        self.assertIn("skipped 1 not-addressed row", out)
        self.assertIn(all_row["id"][:8], out)

    def test_a_list_of_only_unaddressed_ids_refuses(self):
        """Invariant, green today and after: every id the call names is
        skipped, so the call refuses as it always has and writes nothing —
        a seat that named only rows it cannot ack learns that."""
        self.join("recipT")
        a = chat.post("@all one", who="senderS")
        b = chat.post("@all two", who="senderS")
        before = len(chat.read("main")[0])
        rc, _out, err = self.ack([a["id"], b["id"]])
        self.assertEqual(rc, 1, err)
        self.assertIn("nothing was acked", err)
        self.assertEqual(len(chat.read("main")[0]), before)

    def test_an_unaddressed_id_named_twice_is_skipped_once(self):
        """RED on 68d7e3ee9e1: an addressed id is acked once however often
        it is named, but an @all id named twice was reported as TWO skipped
        rows, the same id printed twice."""
        self.join("recipT")
        row = self.ask(1)[0]
        all_row = chat.post("@all standup", who="senderS")
        rc, out, err = self.ack([row["id"], all_row["id"], all_row["id"]])
        self.assertEqual(rc, 0, err)
        self.assertIn("skipped 1 not-addressed row:", out)
        self.assertEqual(out.count(all_row["id"][:8]), 1, out)

    def test_an_unreadable_roster_refuses_rather_than_skips(self):
        """RED on 68d7e3ee9e1: "addressed to no one" is a claim about the
        roster, and the skip path read it fail-open, so a roster that tore
        after the ids were located turned the seat's own @mention into a
        skipped row (rc 0, "a read or catchup clears it") while the DM in
        the same call was acked. An UNREADABLE roster refuses exactly as
        it did before the skip existed: the whole call, nothing written.
        The DM is in the list because a torn roster cannot hide a DM (its
        recipient is on the row), so without it every id would skip and the
        all-skipped refusal would mask the fail-open."""
        from helm.seats_common import roster_path
        self.join("recipT")
        dm, derr = seats.dm("recipT", "direct ask", who="senderS")
        self.assertIsNone(derr, derr)
        _m, dm_room, lerr = seats_ack._locate_row(dm["id"])
        self.assertIsNone(lerr, lerr)
        mention = self.ask(1)[0]
        all_row = chat.post("@all standup", who="senderS")
        path = roster_path()
        with open(path, "rb") as f:
            good = f.read()
        real = seats_ack._locate_rows

        def locate_then_tear(ids, *a, **kw):
            found = real(ids, *a, **kw)
            with open(path, "w", encoding="utf-8") as f:
                f.write("{torn")
            return found
        before = (len(chat.read("main")[0]), len(chat.read(dm_room)[0]))
        try:
            with mock.patch.object(seats_ack, "_locate_rows",
                                   locate_then_tear):
                rc, out, err = self.ack([dm["id"], mention["id"],
                                         all_row["id"]])
        finally:
            with open(path, "wb") as f:
                f.write(good)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("roster could not be read", err)
        self.assertIn("nothing was acked", err)
        self.assertNotIn("skipped", out)
        self.assertEqual((len(chat.read("main")[0]),
                          len(chat.read(dm_room)[0])), before)

    def test_a_repeat_ack_names_the_seat_not_its_capability(self):
        """RED on 68d7e3ee9e1 and on main: the idempotent line printed the
        CLI door's AdmittedActor, `by <AdmittedActor recipT #…>`, where the
        seat's name belongs."""
        self.join("recipT")
        row = self.ask(1)[0]
        rc, _out, err = self.ack([row["id"]])
        self.assertEqual(rc, 0, err)
        rc, out, err = self.ack([row["id"]])
        self.assertEqual(rc, 0, err)
        self.assertIn("already acked DONE by recipT — no new row", out)
        self.assertNotIn("AdmittedActor", out)

    def test_the_verbs_doc_says_a_repeat_is_not_written(self):
        """RED on 68d7e3ee9e1: the rewrap left `is not not written again`,
        which says a repeat IS written."""
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "docs", "VERBS.md")
        with open(path, encoding="utf-8") as f:
            text = " ".join(f.read().split())
        self.assertNotIn("is not not written", text)
        self.assertIn("an id already acked the same way is not written "
                      "again", text)

    def test_two_state_words_refuse(self):
        self.join("recipT")
        rows = self.ask(1)
        rc, _out, err = self.ack([rows[0]["id"], "done", "blocked"])
        self.assertEqual(rc, 2, err)
        self.assertIn("one state", err)

    def test_a_repeat_writes_nothing_and_a_new_id_writes_only_itself(self):  # noqa: VACUOUS_ASSERTION — the same call wrote a row the first time, and the third call writes exactly one row naming the new id
        self.join("recipT")
        rows = self.ask(3)
        ids = [r["id"] for r in rows]
        self.assertEqual(self.ack(ids[:2])[0], 0)
        n = len(chat.read("main")[0])
        rc, out, err = self.ack(ids[:2])
        self.assertEqual(rc, 0, err)
        self.assertIn("already acked", out)
        self.assertEqual(len(chat.read("main")[0]), n)
        rc, _out, err = self.ack(ids)
        self.assertEqual(rc, 0, err)
        new = chat.read("main")[0][n:]
        self.assertEqual(len(new), 1, new)
        self.assertEqual(new[0].get("ack"), ids[2])
        self.assertNotIn("acks", new[0])

    def test_one_id_named_twice_is_acked_once(self):
        self.join("recipT")
        rows = self.ask(2)
        a, b = rows[0]["id"], rows[1]["id"]
        rc, _out, err = self.ack([a, b, a[:8]])
        self.assertEqual(rc, 0, err)
        self.assertEqual(chat.read("main")[0][-1].get("acks"), [a, b])

    def test_a_single_id_writes_the_single_shape(self):  # noqa: VACUOUS_ASSERTION — the row's `ack` is the id, pinned; the absent `acks` key is the single shape itself
        """Control: one id writes the row it always wrote."""
        self.join("recipT")
        rows = self.ask(1)
        rc, out, err = self.ack([rows[0]["id"]])
        self.assertEqual(rc, 0, err)
        row = chat.read("main")[0][-1]
        self.assertEqual(row.get("ack"), rows[0]["id"])
        self.assertNotIn("acks", row)
        self.assertIn("acked %s DONE" % rows[0]["id"][:8], out)

    def test_the_usage_names_the_bulk_form(self):
        rc, _out, err = self.run_cmd("ack", ["--seat", "recipT"])
        self.assertEqual(rc, 2)
        self.assertIn("helm chat ack <id> [<id> ...]", err)


if __name__ == "__main__":
    unittest.main()
