#!/usr/bin/env python3
"""The standing meld: one open room per working pair (task/3560).

Premise always-on-slack-like-chat-is-an-owner-priority made mechanical. A
working pair keeps ONE standing room across tasks: no exchange cap, `say`
never blocks and wakes the other members by @mention (their beacon does the
rest), `recv` returns at once, a member pulls a third seat in for exactly one
exchange, and a seat invited later can join. Dispatch send (and the FIX
verdict's hand-back) for that pair name the standing room instead of minting
a per-chain pair meld. The capped meld keeps its own tests (test_meld,
test_pair_meld) unchanged.

The owner's acceptance, as a test with fakes: two seats run 3 dispatch
chains through ONE standing room with zero new per-task rooms and nobody
blocked in recv; a third seat joins for one exchange and leaves.

Hermetic: every chat and helm home here is a temp dir.
"""
import io
import os
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import os as _os, sys as _sys  # noqa: E401,E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import chat, dispatches, meld, tasks  # noqa: E402
from helm import meld_standing as S  # noqa: E402
from helm import review_door as RD  # noqa: E402
from tests import test_meld as tm  # noqa: E402
from tests import test_review_door as trd  # noqa: E402

LEAD, WORKER, THIRD, LATER = "demo-claude", "bonsai", "codex", "mentor"


def cli(*args, seat=None):
    argv = list(args) + (["--seat", seat] if seat else [])
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = meld.cmd(argv)
    return rc, out.getvalue(), err.getvalue()


def texts(room):
    return [m.get("text") or "" for m in chat.read(room)[0]]


def never_waits():
    """RECV NEVER WAITS, counted rather than timed: any sleep inside the
    block fails the arm, where a wall-clock ceiling would go red on a loaded
    host with no defect."""
    return mock.patch("time.sleep",
                      side_effect=AssertionError("a standing recv waited"))


class StandingRoomTest(tm.MeldBase):
    """The standing mode on the meld verbs."""

    def setUp(self):
        super().setUp()
        # THE CAPPED MELD'S BOUNDS ARE SET TIGHT, so an arm that passes
        # proves the standing room does not inherit them.
        bounds = mock.patch.dict(os.environ, {
            "HELM_MELD_CAP": "2", "HELM_MELD_RECV_TIMEOUT_S": "90"})
        bounds.start()
        self.addCleanup(bounds.stop)
        self.room, self.lines = S.open_room(WORKER, seat=LEAD)

    def test_the_room_is_a_pure_function_of_the_pair(self):
        self.assertEqual(S.room_for(LEAD, WORKER), S.room_for(WORKER, LEAD))
        self.assertEqual(self.room, S.room_for(LEAD, WORKER))
        self.assertTrue(S.is_standing_room(self.room))
        self.assertFalse(RD.is_pair_room(self.room))
        self.assertLessEqual(len(self.room), 60)
        self.assertNotEqual(S.room_for("api.a", "x"), S.room_for("api-a", "x"))

    def test_open_is_idempotent_and_wakes_the_peer(self):
        before = texts(self.room)
        self.assertEqual(len(before), 1)
        self.assertTrue(before[0].startswith("@%s " % WORKER), before[0])
        room, lines = S.open_room(LEAD, seat=WORKER)
        self.assertEqual(room, self.room)
        self.assertEqual(texts(self.room), before)
        self.assertIn("STANDING-EXISTS", lines[0])

    def test_no_exchange_cap_and_done_does_not_seal(self):
        for i in range(12):
            who = LEAD if i % 2 == 0 else WORKER
            marker = "DONE" if i == 3 else None
            S.say(self.room, "message %d" % i, marker=marker, seat=who)
        code, lines = S.recv(self.room, seat=WORKER)
        self.assertEqual(code, 0)
        got = "\n".join(lines)
        for i in range(0, 12, 2):
            self.assertIn("message %d" % i, got)
        self.assertNotIn("message 1\n", got + "\n")   # its own rows

    def test_say_mentions_the_others_and_recv_never_waits(self):
        S.say(self.room, "ping", seat=LEAD)
        self.assertTrue(texts(self.room)[-1].startswith("@%s " % WORKER))
        with never_waits():
            code, lines = S.recv(self.room, seat=LEAD)   # nothing new for LEAD
        self.assertEqual(code, 0)
        self.assertIn("nothing new", lines[-1])
        code, lines = S.recv(self.room, seat=WORKER)
        self.assertEqual(code, 0)
        self.assertIn("ping", "\n".join(lines))
        code, lines = S.recv(self.room, seat=WORKER)  # consumed
        self.assertIn("nothing new", lines[-1])

    def test_a_third_seat_is_pulled_in_for_one_exchange_and_leaves(self):
        S.pull(self.room, THIRD, "is the fence right?", seat=LEAD)
        self.assertTrue(texts(self.room)[-1].startswith("@%s " % THIRD))
        S.join(self.room, seat=THIRD)
        code, lines = S.recv(self.room, seat=THIRD)
        self.assertEqual(code, 0)
        self.assertIn("is the fence right?", "\n".join(lines))
        lines = S.say(self.room, "yes, epoch-fenced", seat=THIRD)
        self.assertIn("left", "\n".join(lines).lower())
        self.assertNotIn(THIRD, S.participants(self.room))
        with self.assertRaises(SystemExit):
            S.say(self.room, "one more", seat=THIRD)
        with self.assertRaises(SystemExit):
            S.join(self.room, seat=THIRD)
        for member in (LEAD, WORKER):
            with never_waits():                       # nobody waits
                code, lines = S.recv(self.room, seat=member)
            self.assertEqual(code, 0)
            self.assertIn("yes, epoch-fenced", "\n".join(lines))
        chat.post("@%s [STANDING] a row after leaving" % LEAD,
                  room=self.room, who=THIRD, sign=False)
        code, lines = S.recv(self.room, seat=LEAD)
        self.assertNotIn("after leaving", "\n".join(lines))

    def test_a_guest_cannot_pull_or_invite(self):
        S.pull(self.room, THIRD, "a question", seat=LEAD)
        with self.assertRaises(SystemExit):
            S.pull(self.room, LATER, "another", seat=THIRD)
        with self.assertRaises(SystemExit):
            S.add(self.room, LATER, seat=THIRD)

    def test_a_seat_invited_later_can_join(self):
        with self.assertRaises(SystemExit):
            S.join(self.room, seat=LATER)
        S.add(self.room, LATER, seat=WORKER)
        lines = S.join(self.room, seat=LATER)
        self.assertIn("STANDING-JOINED", lines[0])
        S.say(self.room, "hello from the later seat", seat=LATER)
        code, lines = S.recv(self.room, seat=LEAD)
        self.assertIn("hello from the later seat", "\n".join(lines))
        self.assertIn(LATER, S.participants(self.room))

    def test_a_non_member_is_refused_and_its_rows_are_ignored(self):
        with self.assertRaises(SystemExit):
            S.say(self.room, "hijack", seat=LATER)
        chat.post("@%s [STANDING] forged" % LEAD, room=self.room, who=LATER,
                  sign=False)
        chat.post("[STANDING-ADD] seat=%s by=%s" % (LATER, LATER),
                  room=self.room, who=LATER, sign=False)
        self.assertNotIn(LATER, S.participants(self.room))
        code, lines = S.recv(self.room, seat=LEAD)
        self.assertNotIn("forged", "\n".join(lines))

    def rotate_past_the_open_row(self, rounds=3):
        """Post filler under a tiny SIZE_CAP until the room has rotated
        `rounds` times and its [STANDING-OPEN] row is long gone."""
        rotations = []
        real = chat._rotate

        def counting(*a, **kw):
            done = real(*a, **kw)
            if done:
                rotations.append(1)
            return done
        with mock.patch.object(chat, "SIZE_CAP", 2048), \
                mock.patch.object(chat, "_rotate", counting):
            n = 0
            while len(rotations) < rounds:
                chat.post("filler %d %s" % (n, "x" * 200), room=self.room,
                          who="bystander", sign=False)
                n += 1
                self.assertLess(n, 500, "the room never rotated")
        self.assertFalse(any("[STANDING-OPEN]" in t for t in texts(self.room)))

    def test_membership_survives_rotation(self):
        S.add(self.room, LATER, seat=WORKER)
        S.join(self.room, seat=LATER)
        S.say(self.room, "before the rotation", seat=LEAD)
        code, lines = S.recv(self.room, seat=WORKER)
        self.assertIn("before the rotation", "\n".join(lines))
        self.rotate_past_the_open_row()
        self.assertEqual(sorted(S.participants(self.room)),
                         sorted([LEAD, WORKER, LATER]))
        # the cursor survives too: nothing old is re-delivered
        code, lines = S.recv(self.room, seat=WORKER)
        self.assertNotIn("before the rotation", "\n".join(lines))
        self.assertIn("nothing new", lines[-1])
        S.say(self.room, "after the rotation", seat=LEAD)
        code, lines = S.recv(self.room, seat=WORKER)
        self.assertEqual(code, 0)
        self.assertIn("after the rotation", "\n".join(lines))
        # pull, a later add and join, and a second open all still work
        S.pull(self.room, THIRD, "still standing?", seat=WORKER)
        S.join(self.room, seat=THIRD)
        S.say(self.room, "yes", seat=THIRD)
        self.assertNotIn(THIRD, S.participants(self.room))
        S.add(self.room, "late2", seat=LEAD)
        S.join(self.room, seat="late2")
        self.assertIn("late2", S.participants(self.room))
        _room, lines = S.open_room(LEAD, seat=WORKER)
        self.assertIn("STANDING-EXISTS", lines[0])
        # and it survives a second run of rotations
        self.rotate_past_the_open_row()
        self.assertEqual(sorted(S.participants(self.room)),
                         sorted([LEAD, WORKER, LATER, "late2"]))
        self.assertEqual(S.open_for(LEAD, WORKER), self.room)

    def test_either_member_reopens_a_room_both_left(self):
        for opener in (WORKER, LEAD):
            S.leave(self.room, seat=LEAD)
            S.leave(self.room, seat=WORKER)
            self.assertEqual(S.participants(self.room), [])
            self.assertIsNone(S.open_for(LEAD, WORKER))
            peer = LEAD if opener == WORKER else WORKER
            room, lines = S.open_room(peer, seat=opener)
            self.assertEqual(room, self.room)
            self.assertIn("STANDING-OPENED", lines[0])
            self.assertEqual(sorted(S.participants(self.room)),
                             sorted([LEAD, WORKER]))
            S.say(self.room, "back again %s" % opener, seat=opener)
            code, lines = S.recv(self.room, seat=peer)
            self.assertIn("back again %s" % opener, "\n".join(lines))
        # an outsider still cannot reopen a room its pair left
        S.leave(self.room, seat=LEAD)
        S.leave(self.room, seat=WORKER)
        chat.post("[STANDING-OPEN] pair=%s,%s by=%s" % (LATER, WORKER, LATER),
                  room=self.room, who=LATER, sign=False)
        self.assertEqual(S.participants(self.room), [])

    def test_a_rotation_that_keeps_failing_after_the_carry_keeps_members(self):
        """Every rotation ATTEMPT checkpoints before it installs. Attempts
        that fail after the carry step, more of them than checkpoints are
        kept, must never evict the checkpoint keyed by the room's real
        first row."""
        from helm import seats_receipts
        S.add(self.room, LATER, seat=WORKER)
        S.join(self.room, seat=LATER)
        carried = []
        real_carry = S.carry_rotation

        def counting(*a, **kw):
            carried.append(1)
            return real_carry(*a, **kw)
        with mock.patch.object(S, "carry_rotation", counting):
            self.rotate_past_the_open_row(rounds=1)
            first = chat.tkey(chat.read(self.room)[0][0])
            carried.clear()
            with mock.patch.object(chat, "SIZE_CAP", 4000), \
                    mock.patch.object(seats_receipts, "remap_room_receipts",
                                      return_value=False):
                n = 0
                while len(carried) <= S._CARRIES + 2:
                    S.say(self.room, "say %d %s" % (n, "y" * 300),
                          seat=LEAD if n % 2 else WORKER)
                    n += 1
                    self.assertLess(n, 200, "no rotation was attempted")
                self.assertEqual(chat.tkey(chat.read(self.room)[0][0]), first)
                self.assertEqual(sorted(S.participants(self.room)),
                                 sorted([LEAD, WORKER, LATER]))
                self.assertEqual(S.open_for(LEAD, WORKER), self.room)
                S.say(self.room, "still here", seat=LEAD)
                code, lines = S.recv(self.room, seat=LATER)
                self.assertIn("still here", "\n".join(lines))
            # and once rotation succeeds again, membership still holds
            self.rotate_past_the_open_row(rounds=1)
            self.assertNotEqual(chat.tkey(chat.read(self.room)[0][0]), first)
        self.assertEqual(sorted(S.participants(self.room)),
                         sorted([LEAD, WORKER, LATER]))
        S.say(self.room, "after the retries", seat=WORKER)
        code, lines = S.recv(self.room, seat=LEAD)
        self.assertIn("after the retries", "\n".join(lines))

    def test_an_unreadable_checkpoint_keeps_the_room_unrotated(self):
        """A sidecar that exists but will not parse is not a room that never
        rotated: the carry refuses, the room stays whole, the sidecar is
        left as it was."""
        path = S._carry_path(self.room)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        with mock.patch.object(chat, "SIZE_CAP", 2048):
            for n in range(20):
                chat.post("filler %d %s" % (n, "x" * 200), room=self.room,
                          who="bystander", sign=False)
        self.assertIn("[STANDING-OPEN]", texts(self.room)[0])
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{not json")
        self.assertEqual(sorted(S.participants(self.room)),
                         sorted([LEAD, WORKER]))

    def test_a_pair_seat_reopens_when_only_a_later_member_is_left(self):
        S.add(self.room, LATER, seat=WORKER)
        S.join(self.room, seat=LATER)
        S.leave(self.room, seat=LEAD)
        S.leave(self.room, seat=WORKER)
        self.assertEqual(S.participants(self.room), [LATER])
        room, lines = S.open_room(WORKER, seat=LEAD)
        self.assertEqual(room, self.room)
        self.assertIn("STANDING-OPENED", lines[0])
        self.assertEqual(sorted(S.participants(self.room)),
                         sorted([LEAD, WORKER, LATER]))
        S.say(self.room, "reopened with the later seat", seat=LEAD)
        for seat in (WORKER, LATER):
            code, lines = S.recv(self.room, seat=seat)
            self.assertIn("reopened with the later seat", "\n".join(lines))

    def test_a_colliding_pair_open_is_not_reported_opened(self):
        S.leave(self.room, seat=LEAD)
        S.leave(self.room, seat=WORKER)
        with mock.patch.object(S, "room_for", lambda _a, _b: self.room):
            with self.assertRaises(SystemExit) as got:
                S.open_room(THIRD, seat=LATER)
        self.assertNotIn("STANDING-OPENED", str(got.exception))
        self.assertEqual(S.participants(self.room), [])

    def test_an_idle_standing_room_is_never_retired(self):
        from helm import chatdebris
        pair, _lines = meld.invite(WORKER, "a huddle", seat=LEAD)
        old = time.time() - 30 * chatdebris.DAY
        for room in (self.room, pair):
            os.utime(chat.room_path(room), (old, old))
        idle = [r for r, _s in chatdebris.retirable_rooms()]
        self.assertIn(pair, idle)
        self.assertNotIn(self.room, idle)
        self.assertEqual(chatdebris.retire_room(self.room)[0], None)

    def test_the_cli_routes_a_standing_room_to_the_standing_mode(self):
        room = S.room_for(LATER, THIRD)
        rc, out, err = cli("standing", THIRD, seat=LATER)
        self.assertEqual(rc, 0, err)
        self.assertIn(room, out)
        rc, out, err = cli("say", room, "no marker needed", seat=THIRD)
        self.assertEqual(rc, 0, err)
        rc, out, err = cli("recv", room, seat=LATER)
        self.assertEqual(rc, 0, err)
        self.assertIn("no marker needed", out)
        rc, out, err = cli("invite", LEAD, "--into", room, seat=THIRD)
        self.assertEqual(rc, 0, err)
        rc, out, err = cli("join", room, seat=LEAD)
        self.assertEqual(rc, 0, err)
        rc, out, err = cli("pull", room, WORKER, "one question", seat=LEAD)
        self.assertEqual(rc, 0, err)
        rc, out, err = cli("say", room, "the answer", seat=WORKER)
        self.assertEqual(rc, 0, err)
        self.assertNotIn(WORKER, S.participants(room))
        rc, out, err = cli("status", seat=LATER)
        self.assertIn(room, out)
        # the capped meld is untouched: a plain invite still mints its room
        rc, out, err = cli("invite", WORKER, "a one-question huddle",
                           seat=LEAD)
        self.assertEqual(rc, 0, err)
        self.assertIn("MELD-INVITED", out)


class StandingPeerGuardTest(tm.MeldBase):
    """A standing room opens with a SEAT. A flag-shaped peer, or a peer the
    roster has no row for, is refused and opens nothing; an empty roster is
    UNKNOWN and still proceeds, exactly as the dispatch-send recipient guard
    does (the refusal reuses its one registry, not a second one)."""

    def _no_room(self, room):
        self.assertNotIn(room, chat.list_rooms())

    def test_help_opens_no_room(self):  # noqa: VACUOUS_ASSERTION — only the empty observable is asserted here; the positive control (a room does open) lives in the opening tests
        # The incident: `--help` was taken for a peer and OPENED a room (the
        # leading '-' is a legal seat-token char, so the shape check passed).
        # A help flag must print usage and open NOTHING.
        for flag in ("--help", "-h"):
            rc, out, err = cli("standing", flag, seat=LEAD)
            self.assertEqual(rc, 0, err)
            self.assertIn("usage", out, (flag, out))
            self.assertEqual(
                [r for r in chat.list_rooms() if r.startswith(S.PREFIX)], [],
                "%r opened a standing room" % flag)

    def test_a_missing_peer_prints_usage_and_opens_nothing(self):  # noqa: VACUOUS_ASSERTION — only the empty observable is asserted here; the positive control lives in the opening tests
        rc, out, err = cli("standing", seat=LEAD)
        self.assertEqual(rc, 2, err)
        self.assertIn("wants a peer", err, err)   # RED on base: no such line
        self.assertIn("usage", out + err)
        self.assertEqual([r for r in chat.list_rooms()
                          if r.startswith(S.PREFIX)], [])

    def test_a_flag_shaped_peer_is_refused(self):
        rc, out, err = cli("standing", "--bogus", seat=LEAD)
        self.assertEqual(rc, 2, (rc, out, err))
        self.assertIn("--bogus", err, err)
        self._no_room(S.room_for(LEAD, "--bogus"))

    def test_a_peer_the_roster_lacks_is_refused(self):
        self.plant_roster({LEAD: {}})      # populated: absence is now proven
        rc, out, err = cli("standing", "no-such-seat", seat=LEAD)
        self.assertEqual(rc, 2, (rc, out, err))
        self.assertIn("no-such-seat", err, err)
        self._no_room(S.room_for(LEAD, "no-such-seat"))

    def test_an_unknown_roster_still_proceeds(self):  # noqa: VACUOUS_ASSERTION — this test IS the positive control: it asserts the room present and fails if the guard over-reached
        # The fail-open arm: an EMPTY roster is UNKNOWN, not ABSENT, so a real
        # seat still opens — the refusal must not over-reach to the empty case.
        room, _lines = S.open_room(WORKER, seat=LEAD)
        self.assertEqual(room, S.room_for(LEAD, WORKER))
        self.assertIn(room, chat.list_rooms())

    def test_a_room_paired_with_a_real_seat_is_kept(self):
        # The other arm of the sweep's new keep gate: a standing room whose
        # pair is BOTH real seats stays open, exactly as before the change.
        # The gate now keeps a standing room only while both members are known
        # seats; this proves it has not started retiring a room the incident
        # never touched. Green on base AND tip: a preservation control, not a
        # new-behaviour test (the four refusal tests above are the red-on-base
        # ones).
        from helm import chatdebris
        room = S.room_for(LEAD, WORKER)
        chat.post("@%s [STANDING-OPEN] pair=%s,%s by=%s | discipline"
                  % (WORKER, LEAD, WORKER, LEAD), room=room, who=LEAD, sign=False)
        old = time.time() - 30 * chatdebris.DAY
        os.utime(chat.room_path(room), (old, old))
        self.assertNotIn(
            room, [r for r, _s in chatdebris.retirable_rooms()],
            "a real-pair standing room became retirable")
        _report, refusal = chatdebris.retire_room(room)
        self.assertIsNotNone(refusal, refusal)

    def test_a_help_paired_room_is_retirable(self):
        # The room the incident left behind: a real seat paired with the flag
        # '--help'. Its pair names no known seat, so the sweep clears it.
        from helm import chatdebris
        room = S.room_for(LEAD, "--help")
        chat.post("@--help [STANDING-OPEN] pair=%s,--help by=%s | discipline"
                  % (LEAD, LEAD), room=room, who=LEAD, sign=False)
        old = time.time() - 30 * chatdebris.DAY
        os.utime(chat.room_path(room), (old, old))
        idle = [r for r, _s in chatdebris.retirable_rooms()]
        self.assertIn(room, idle)
        report, refusal = chatdebris.retire_room(room)
        self.assertIsNone(refusal, refusal)
        self.assertIsNotNone(report)


class StandingDispatchTest(trd.DoorBase):
    """The owner's acceptance through the real CLI, ledger and fold: three
    tasks for one pair run through ONE standing room."""

    READER = WORKER

    def setUp(self):
        super().setUp()
        self.author = dispatches._acting_author()[0]

    def meld_rooms(self):
        """Every meld room, a task's `<scope>-<N>` pair meld included."""
        return sorted(r for r in chat.list_rooms()
                      if r.startswith("meld-") or RD.is_pair_room(r))

    def test_three_tasks_run_through_one_standing_room(self):  # noqa: VACUOUS_ASSERTION — the loop runs a fixed three-task tuple and asserts the standing room IN each output; the meld-room list is asserted EQUAL to that one room
        room, _lines = S.open_room(self.READER, seat=self.author)
        rows = []
        for n, tip in zip((9101, 9102, 9103), (self.a, self.b, self.c)):
            self.review_task, err = tasks.add(
                "fixture standing %d" % n, "author", tid=n,
                project="helm-test", force_new=True)
            self.assertIsNone(err, err)
            self.lane = "standing-task-%d" % n
            rc, out, err, sent = self.send(None, tip)
            self.assertEqual(rc, 0, err)
            self.assertIn(room, out)
            self.assertNotIn("your pair meld for this task", out)
            rows.append(sent)
        # zero per-task rooms: the standing room is the only meld room
        self.assertEqual(self.meld_rooms(), [room])
        self.assertEqual([f for f in os.listdir(chat.chat_dir())
                          if ".meld." in f], [])
        rounds = [t for t in texts(room) if t.startswith("[STANDING-ROUND]")]
        self.assertEqual(len(rounds), 3)
        for sent in rows:
            self.assertTrue(any(sent["id"][:12] in t for t in rounds))
        # nobody blocked: the reader's recv returns at once with the rounds
        with never_waits():
            code, lines = S.recv(room, seat=self.READER)
        self.assertEqual(code, 0)
        for n in (9101, 9102, 9103):
            self.assertIn("task/%d" % n, "\n".join(lines))
        # a third seat joins for one exchange and leaves
        S.pull(room, THIRD, "does the fold hold?", seat=self.author)
        S.join(room, seat=THIRD)
        S.say(room, "it holds", seat=THIRD)
        self.assertNotIn(THIRD, S.participants(room))
        self.assertEqual(self.meld_rooms(), [room])

    def test_without_a_standing_room_the_pair_meld_is_unchanged(self):
        self.review_task, err = tasks.add(
            "fixture standing 9104", "author", tid=9104,
            project="helm-test", force_new=True)
        self.assertIsNone(err, err)
        self.lane = "standing-task-9104"
        rc, out, err, sent = self.send(None, self.a)
        self.assertEqual(rc, 0, err)
        self.assertIn("your pair meld for this task: %s (task/9104"
                      % self.pair_room(sent), out)

    def test_a_retried_round_is_posted_once(self):
        room, _lines = S.open_room(self.READER, seat=self.author)
        row = {"id": "e" * 32, "chain_root": "e" * 32, "lane": "task-9105",
               "sender": self.author, "recipient": self.READER,
               "kind": "review", "tip": self.a, "repo_id": "/x/helm/.git"}
        one = RD.open_pair_round(row, current={row["id"]: row})
        two = RD.open_pair_round(row, current={row["id"]: row})
        self.assertEqual((one["room"], two["room"]), (room, room))
        rounds = [t for t in texts(room) if t.startswith("[STANDING-ROUND]")]
        self.assertEqual(len(rounds), 1)

    def test_a_round_three_review_seeds_its_stored_whisper_once_without_mention(self):
        room, _lines = S.open_room(self.READER, seat=self.author)
        whisper = RD.round_whisper(3)
        row = {"id": "f" * 32, "chain_root": "f" * 32, "lane": "task-9107",
               "sender": self.author, "recipient": self.READER,
               "kind": "review", "tip": self.a, "repo_id": "/x/helm/.git",
               "round_whisper": whisper}
        for _attempt in range(2):
            opened = RD.open_pair_round(row, current={row["id"]: row})
            self.assertEqual(opened["room"], room)
        rounds = [t for t in texts(room) if t.startswith("[STANDING-ROUND]")]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0].count(whisper), 1)
        self.assertNotIn("@", rounds[0])

    def test_a_fix_verdict_hands_back_through_the_standing_room(self):
        room, _lines = S.open_room(self.READER, seat=self.author)
        self.lane = "standing-task-9106"
        _rc, _out, _err, sent = self.send(None, self.a)
        verdict = self.fix(sent)
        with mock.patch.object(dispatches, "_nudge",
                               return_value=True) as nudge:
            dispatches._verdict_author_nudge(verdict)
        self.assertIn(room, nudge.call_args[0][1])
        self.assertEqual(self.meld_rooms(), [room])


def setUpModule():
    from tests._tmphome import pin_live_seats
    pin_live_seats()


if __name__ == "__main__":
    unittest.main()
