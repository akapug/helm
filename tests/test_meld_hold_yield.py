#!/usr/bin/env python3
"""task/3743 A+B: a task's pair meld keeps a held round open, and a YIELD in
it reaches the peer as an addressed row.

FIELD DATA (a product project's builder and codex pair, one overnight run).
An async review inside a task's pair meld closed with [DONE], the fast-meld
discipline, so every later builder YIELD bounced MELD-PEER-CLOSED and paid a
re-invite plus a READY handshake: four times in one round. And a YIELD was
no owed row at all, so a lapsed 30-minute beacon swallowed one for about 48
minutes.

(A) In a task's pair meld a [HOLD] whose text says `HOLDING: <what the
holder is doing>` keeps the round open past recv-timeout, and the peer's
next YIELD resumes the round. A [HOLD] with no reason behaves as today, and
a [DONE] still closes. (B) A YIELD there @mentions every peer in the round,
so the one @mention path carries it: the doorbell counts it, the tool
boundary delivers it and the stop guard blocks an idle stop on it.

THE MATRIX, peer state x event, one arm per cell (`PairMatrixTest`):

    peer state           YIELD arrives                recv-timeout passes
    live in round        addressed, recv returns it   recv again (+ HOLD hint)
    HOLD with a reason   addressed, round resumes     MELD-HELD, stays open
    HOLD with no reason  addressed, recv returns it   today's lines, DONE close
    DONE                 MELD-PEER-CLOSED, no row     recv returns the DONE
    never joined         posted, NOT addressed        MELD-NOJOIN (today)

`PlainMeldControlTest` holds every other meld kind at today's behaviour.
`DeadBeaconDeliveryTest` is the (B) arm with no beacon armed.

Hermetic: every chat dir and helm home is a temp dir, the chat node URL is
set but empty, and no row reaches a live room.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import beacon_doorbell, chat, meld, review_door, seats  # noqa: E402
from helm.seats_address import seat_names  # noqa: E402
from tests import test_meld as tm  # noqa: E402
from tests import test_stop_spiral as tss  # noqa: E402

#: The scratch reaper stays off in THIS module, not only through the base
#: class: `DeadBeaconDeliveryTest` drives the stop guard, whose silent lane
#: deletes dead-session scratch, and the source audits read one module alone.
_ENV_PRIOR = {}


def setUpModule():
    _ENV_PRIOR["HELM_SCRATCH_GC"] = os.environ.get("HELM_SCRATCH_GC")
    os.environ["HELM_SCRATCH_GC"] = "0"


def tearDownModule():
    for key, was in _ENV_PRIOR.items():
        if was is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = was


#: A task's pair meld: `<scope>-<N>` (task/3749).
ROOM = "helm-3743"
RING = "dispatch row r1 (send)"
REASON = "reviewing tip 0123abcd against F1 to F3"
OUTCOME = ("MELD OUTCOME: AGREED | BAR: the one harm | FALSIFIERS: a second "
           "room | FINDINGS: F1=cured-in-patch | TIP: %s | NEXT: record it "
           "on the row" % ("c" * 40))


def recv(room, seat):
    code, lines = meld.recv(room, timeout=0, seat=seat, poll=0.01)
    return code, "\n".join(lines)


def addressed(row, seat):
    """Does the one @mention path carry `row` to `seat`: the tool boundary
    delivers it, and the doorbell counts it as an addressed row?"""
    kind = beacon_doorbell._kind(row, ROOM, seat, seat_names(seat))
    return seats.deliverable(row, seat, room=ROOM) and kind == "addressed"


class HoldReasonTest(unittest.TestCase):
    """The one reading of what a [HOLD] says its holder is doing."""

    def test_the_words_after_the_label_are_the_reason(self):
        row = "@x [MELD e:7] F1 read.\nHOLDING: %s [HOLD]" % REASON
        self.assertEqual(meld.hold_reason(row), REASON)
        self.assertEqual(meld.hold_reason(
            "[MELD e:7] HOLDING:   curing F1,\n  back with the tip [HOLD]"),
            "curing F1,")

    def test_no_label_or_an_empty_one_is_no_reason(self):
        self.assertEqual(meld.hold_reason("[MELD e:7] HOLDING: x [HOLD]"),
                         "x")                     # the positive control
        for text in ("[MELD e:7] more coming [HOLD]",
                     "[MELD e:7] HOLDING: [HOLD]",
                     "[MELD e:7] holding: lower case is prose [HOLD]", ""):
            with self.subTest(text=text):
                self.assertIsNone(meld.hold_reason(text))


class PairBase(tm.MeldBase):
    """A pair round in ROOM: seat-a convened it on a dispatch ring, seat-b
    joined and has read seat-a's first YIELD. seat-b is the PEER whose state
    each arm sets; seat-a acts."""

    def open_round(self, join=True):
        meld.invite("seat-b", "the plan", seat="seat-a", room=ROOM, ring=RING)
        if not join:
            return
        meld.join(ROOM, seat="seat-b")
        self.assertEqual(recv(ROOM, "seat-a")[0], 0)            # READY
        meld.say(ROOM, "YIELD", "the plan", seat="seat-a")
        self.assertEqual(recv(ROOM, "seat-b")[0], 0)            # the seed
        self.assertEqual(recv(ROOM, "seat-b")[0], 0)            # the plan

    def peer_says(self, marker, text):
        """seat-b posts one chunk, and seat-a's recv takes it."""
        meld.say(ROOM, marker, text, seat="seat-b")
        code, out = recv(ROOM, "seat-a")
        self.assertEqual(code, 0, out)
        self.assertIn(text.splitlines()[0][:20], out)

    def yield_arrives(self, text="cured at tip 4567"):
        """seat-a YIELDs; return the row it posted."""
        before = chat.read(ROOM)[1]
        meld.say(ROOM, "YIELD", text, seat="seat-a")
        rows, total = chat.read(ROOM)
        self.assertEqual(total, before + 1)
        return rows[-1]


class PairMatrixTest(PairBase):
    """Peer state x event in a task's pair meld."""

    # -- live in round --------------------------------------------------
    def test_live_peer_YIELD_arrives_as_an_addressed_row(self):
        self.open_round()
        self.peer_says("YIELD", "agreed on one to three")
        row = self.yield_arrives()
        self.assertTrue(row["text"].startswith("@seat-b [MELD e:"))
        self.assertTrue(addressed(row, "seat-b"))
        self.assertFalse(addressed(row, "seat-a"), "never its own author")
        code, out = recv(ROOM, "seat-b")
        self.assertEqual(code, 0)
        self.assertIn("cured at tip 4567", out)
        self.assertIn("floor: YOURS", out)

    def test_live_peer_recv_timeout_says_recv_again_and_names_the_hold(self):
        self.open_round()
        self.peer_says("YIELD", "agreed on one to three")
        meld.say(ROOM, "YIELD", "and the fourth?", seat="seat-a")
        code, out = recv(ROOM, "seat-a")
        self.assertEqual(code, meld.EXIT_BOUND)
        self.assertIn("no reply yet: recv again", out)
        self.assertNotIn("MELD-HELD", out)
        # the way to keep the round open is printed where a seat would close it
        self.assertIn('--marker HOLD "HOLDING: <what you are doing>"', out)

    # -- HOLD with a reason ---------------------------------------------
    def test_held_peer_recv_timeout_keeps_the_round_open(self):
        self.open_round()
        self.peer_says("HOLD", "F1 read. HOLDING: %s" % REASON)
        code, out = recv(ROOM, "seat-a")
        self.assertEqual(code, meld.EXIT_BOUND)              # the bound fired
        self.assertIn("MELD-HELD room=%s" % ROOM, out)
        self.assertIn("seat-b is HOLDING: %s" % REASON, out)
        self.assertIn("stays open", out)
        self.assertNotIn("--marker DONE", out, "no close is offered")
        self.assertNotIn("no reply yet", out)
        # the HOLDER's own bound says the same: it holds, nothing to close
        code, out = recv(ROOM, "seat-b")
        self.assertEqual(code, meld.EXIT_BOUND)
        self.assertIn("you are HOLDING: %s" % REASON, out)
        self.assertNotIn("--marker DONE", out)
        for seat in ("seat-a", "seat-b"):
            self.assertEqual(meld.state(ROOM, seat)["status"], "active")

    def test_held_peer_YIELD_arrives_and_resumes_the_round(self):
        self.open_round()
        self.peer_says("HOLD", "HOLDING: %s" % REASON)
        recv(ROOM, "seat-b")                  # the holder's bound passes
        row = self.yield_arrives()            # no MELD-PEER-CLOSED
        self.assertTrue(addressed(row, "seat-b"))
        code, out = recv(ROOM, "seat-b")
        self.assertEqual(code, 0)
        self.assertIn("cured at tip 4567", out)
        self.assertIn("floor: YOURS", out)
        meld.say(ROOM, "YIELD", "F1 cured; re-read clean", seat="seat-b")
        code, out = recv(ROOM, "seat-a")
        self.assertEqual(code, 0, out)        # the same round, still talking
        self.assertIn("re-read clean", out)

    def test_pair_exchange_cap_stays_a_real_bound_despite_reasoned_hold(self):  # noqa: VACUOUS_ASSERTION — the cap refusal asserts its positive reason and fall line beside the unchanged consumed-row index
        self.open_round()  # seat-b already accepted the seed and first YIELD
        for n in range(3):
            meld.say(ROOM, "HOLD", "HOLDING: %s; chunk %d" % (REASON, n),
                     seat="seat-a")
            self.assertEqual(recv(ROOM, "seat-b")[0], 0)
        st = meld.state(ROOM, "seat-b")
        self.assertEqual(st["exchanges"], st["cap"])
        meld.say(ROOM, "YIELD", "one more at the real cap", seat="seat-a")
        code, out = recv(ROOM, "seat-b")
        self.assertEqual(code, meld.EXIT_BOUND)
        self.assertIn("reason=cap", out)
        self.assertIn("fall to async NOW", out)
        self.assertEqual(meld.state(ROOM, "seat-b")["idx"], st["idx"])

    # -- HOLD with no reason ----------------------------------------------
    def test_unreasoned_hold_recv_timeout_behaves_as_today(self):
        self.open_round()
        self.peer_says("HOLD", "more coming")
        code, out = recv(ROOM, "seat-a")
        self.assertEqual(code, meld.EXIT_BOUND)
        self.assertIn("no reply yet: recv again", out)
        self.assertIn("helm chat meld say %s --marker DONE" % ROOM, out)
        self.assertNotIn("MELD-HELD", out)

    def test_unreasoned_hold_YIELD_arrives_as_an_addressed_row(self):
        self.open_round()
        self.peer_says("HOLD", "more coming")
        row = self.yield_arrives()
        self.assertTrue(addressed(row, "seat-b"))
        code, out = recv(ROOM, "seat-b")
        self.assertEqual(code, 0)
        self.assertIn("cured at tip 4567", out)

    # -- DONE -------------------------------------------------------------
    def test_done_peer_YIELD_is_still_refused(self):  # noqa: VACUOUS_ASSERTION — the refused YIELD's unchanged row count is bracketed by the DONE that then posts exactly one row on the same count
        self.open_round()
        meld.say(ROOM, "DONE", OUTCOME, seat="seat-b")
        before = chat.read(ROOM)[1]
        with self.assertRaises(SystemExit) as cm:
            meld.say(ROOM, "YIELD", "one more thought", seat="seat-a")
        self.assertIn("MELD-PEER-CLOSED", str(cm.exception))
        self.assertEqual(chat.read(ROOM)[1], before, "a refusal posted a row")
        meld.say(ROOM, "DONE", OUTCOME, seat="seat-a")     # the way out posts
        self.assertEqual(chat.read(ROOM)[1], before + 1)

    def test_done_peer_recv_returns_the_close_not_a_timeout(self):
        self.open_round()
        meld.say(ROOM, "DONE", OUTCOME, seat="seat-b")
        code, out = recv(ROOM, "seat-a")
        self.assertEqual(code, 0)
        self.assertIn("peer left", out)
        self.assertNotIn("MELD-HELD", out)
        self.assertEqual(meld.state(ROOM, "seat-a")["status"], "peer-done")
        self.assertEqual(recv(ROOM, "seat-a")[0], 2)          # closed stays

    # -- never joined -----------------------------------------------------
    def test_unjoined_peer_YIELD_posts_but_wakes_nobody(self):
        """One wake: the dispatch ring. A reader who never joined is never
        walled by the room (docs/MELD_REVIEW_DOOR.md, falsifier (c))."""
        self.open_round(join=False)
        row = self.yield_arrives("the plan")
        self.assertTrue(row["text"].startswith("[MELD e:"), row["text"])
        self.assertFalse(addressed(row, "seat-b"))
        meld.join(ROOM, seat="seat-b")
        self.assertIn("PROBLEM:", recv(ROOM, "seat-b")[1])
        self.assertIn("the plan", recv(ROOM, "seat-b")[1])

    def test_unjoined_peer_recv_timeout_says_nojoin(self):
        self.open_round(join=False)
        meld.say(ROOM, "YIELD", "the plan", seat="seat-a")
        code, out = recv(ROOM, "seat-a")
        self.assertEqual(code, meld.EXIT_BOUND)
        self.assertIn("MELD-NOJOIN", out)
        self.assertNotIn("MELD-HELD", out)

    # -- the room says it -------------------------------------------------
    def test_the_pair_seed_says_hold_not_done_for_longer_work(self):
        self.open_round(join=False)
        seed = chat.read(ROOM)[0][0]["text"]
        self.assertIn("MELD DISCIPLINE", seed)
        self.assertIn("HOLDING:", seed)
        self.assertNotIn("close [DONE] with the async continuation", seed)
        self.assertTrue(seed.endswith("[HOLD]"))
        self.assertIsNone(meld.hold_reason(seed), "the seed holds nothing")


class PlainMeldControlTest(tm.MeldBase):
    """Every other meld kind keeps today's behaviour."""

    def test_a_plain_meld_keeps_its_discipline_mentions_and_timeout(self):  # noqa: VACUOUS_ASSERTION — each absence sits beside an unconditional assertIn on the same seed or timeout text, and the YIELD's non-delivery is the plain-room law test_meld.TestSay pins with its DONE control
        room, _ = self.open_meld()
        seed = chat.read(room)[0][0]["text"]
        self.assertIn("close [DONE] with the async continuation", seed)
        self.assertEqual(recv(room, "seat-a")[0], 0)           # READY
        meld.say(room, "YIELD", "the plan", seat="seat-a")
        self.assertFalse(seats.deliverable(chat.read(room)[0][-1], "seat-b",
                                           room=room))
        recv(room, "seat-b")                                   # the seed
        recv(room, "seat-b")                                   # the plan
        meld.say(room, "HOLD", "HOLDING: %s" % REASON, seat="seat-b")
        self.assertEqual(recv(room, "seat-a")[0], 0)
        code, out = recv(room, "seat-a")
        self.assertEqual(code, meld.EXIT_BOUND)
        self.assertIn("no reply yet: recv again", out)
        self.assertIn("--marker DONE", out)
        self.assertNotIn("MELD-HELD", out)
        self.assertNotIn("HOLDING:", out)

    def test_plain_meld_has_the_same_real_cap_after_a_reasoned_hold(self):
        with mock.patch.dict(os.environ, {"HELM_MELD_CAP": "2"}):
            room, _ = self.open_meld()
            self.assertEqual(recv(room, "seat-a")[0], 0)  # READY
            self.assertEqual(recv(room, "seat-b")[0], 0)  # seed
            meld.say(room, "HOLD", "HOLDING: %s" % REASON, seat="seat-a")
            self.assertEqual(recv(room, "seat-b")[0], 0)
            self.assertEqual(meld.state(room, "seat-b")["exchanges"], 2)
            meld.say(room, "YIELD", "next normal chunk", seat="seat-a")
            code, out = recv(room, "seat-b")
            self.assertEqual(code, meld.EXIT_BOUND)
            self.assertIn("reason=cap", out)
            self.assertIn("fall to async NOW", out)
            self.assertNotIn("next normal chunk", out)


class ConsumedYieldDeliveryTest(PairBase):
    """A recv that showed this session an addressed YIELD must not leave a
    second copy in its boundary inbox or block its next idle stop."""

    def test_recv_consumes_only_the_yield_it_showed_this_session(self):  # noqa: VACUOUS_ASSERTION — the next addressed YIELD on the same boundary is asserted delivered after the consumed one yields no line
        seats.join(session="s-b", cwd=self.tmp, seat="seat-b")
        with mock.patch.dict(os.environ, {"CODEX_SESSION_ID": "s-b"}):
            self.open_round()
            # A second YIELD tests the paired-session path, not the seed.
            meld.say(ROOM, "YIELD", "F1 from the peer", seat="seat-a")
            code, out = recv(ROOM, "seat-b")
            self.assertEqual(code, 0, out)
            self.assertIn("F1 from the peer", out)
        meld.say(ROOM, "HOLD", "HOLDING: %s" % REASON, seat="seat-b")
        self.assertEqual([], review_door.pair_turns_owed("seat-b"))
        blocks, _warns = seats.stop_guard(session="s-b", room="main",
                                          seat="seat-b")
        self.assertNotIn("undelivered message(s)", "\n".join(blocks))
        self.assertIsNone(seats.deliver_any(session="s-b", seat="seat-b"))
        # Do not mark the whole room read: a subsequent YIELD is still owed.
        row = self.yield_arrives("F2 from the peer")
        self.assertTrue(addressed(row, "seat-b"))
        line = seats.deliver_any(session="s-b", seat="seat-b")
        self.assertIsNotNone(line)
        self.assertIn("F2 from the peer", line)

    def test_earlier_addressed_row_is_not_lost_or_a_reason_to_replay_yield(self):
        seats.join(session="s-b", cwd=self.tmp, seat="seat-b")
        with mock.patch.dict(os.environ, {"CODEX_SESSION_ID": "s-b"}):
            self.open_round()
            chat.post("@seat-b older addressed work", room=ROOM,
                      who="seat-a", sign=False)
            meld.say(ROOM, "YIELD", "new peer yield", seat="seat-a")
            self.assertIn("new peer yield", recv(ROOM, "seat-b")[1])
        meld.say(ROOM, "HOLD", "HOLDING: %s" % REASON, seat="seat-b")
        blocks, _warns = seats.stop_guard(session="s-b", room="main",
                                          seat="seat-b")
        self.assertIn("1 undelivered message(s)", "\n".join(blocks))
        line = seats.deliver_any(session="s-b", seat="seat-b")
        self.assertIsNotNone(line)
        self.assertIn("older addressed work", line)
        self.assertIsNone(seats.deliver_any(session="s-b", seat="seat-b"),
                          "the recv-consumed later YIELD must not replay")


class DeadBeaconDeliveryTest(tss.SpiralBase):
    """(B) with the peer's beacon dead: this fixture arms no waiter, so the
    tool boundary and the stop guard are the only paths left, and both must
    carry the YIELD."""

    def setUp(self):
        super().setUp()
        seats.join(session="s-b", cwd=self.tmp, seat="seat-b")
        meld.invite("seat-b", "the plan", seat="seat-a", room=ROOM, ring=RING)
        meld.join(ROOM, seat="seat-b")
        meld.recv(ROOM, timeout=0, seat="seat-a", poll=0.01)      # READY
        meld.say(ROOM, "YIELD", "the plan: problem, invariants", seat="seat-a")

    def test_an_idle_stop_is_blocked_on_the_YIELD(self):
        blocks, _warns = seats.stop_guard(session="s-b", room="main",
                                          seat="seat-b")
        self.assertIn("undelivered message(s)", "\n".join(blocks))

    def test_the_next_tool_boundary_delivers_the_YIELD(self):
        line = seats.deliver_any(session="s-b", seat="seat-b")
        self.assertIsNotNone(line)
        self.assertIn("#%s" % ROOM, line)
        self.assertIn("the plan: problem, invariants", line)


if __name__ == "__main__":
    unittest.main()
