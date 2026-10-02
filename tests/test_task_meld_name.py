#!/usr/bin/env python3
"""A task's pair meld is named `<scope>-<N>` (helm-3742): one persistent
room per task number, the place every agent expects to talk about it.

One class an invariant:
  1. ONE ROOM PER CHAIN FOR ITS LIFE. A task whose room already opened under
     the legacy name (`meld-0-pair-<scope>-task-<N>`) keeps that room: its
     chat log or a lifecycle journal says so. Only a task with no room yet
     gets the new name, so no conversation moves or splits.
  2. EVERY READER KNOWS BOTH SHAPES, and the new recognizer is bounded: a
     scope of at most 16 characters, then `-<digits>`. An ad-hoc room that
     ends in digits is not a task's room.
  3. A chain with no task keeps `meld-0-pair-<scope>-chain-<id12>`.
  4. Every surface that prints a meld name prints the new name for a new
     task chain.

Each arm has a control beside it. Hermetic: every chat dir and helm home is
a temp dir, and no row reaches a live room.
"""
import json
import os
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E401,E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import chat, dispatches, meld, pk, review_done, seats  # noqa: E402
from helm import review_door as RD  # noqa: E402
from tests import test_meld as tm  # noqa: E402
from tests import test_pair_meld as tpm  # noqa: E402

row = tpm.row
ROOT = tpm.ROOT
KID = tpm.KID
TIP = tpm.TIP
RING = tpm.RING
LEGACY = "meld-0-pair-helm-task-3112"
NEW = "helm-3112"
#: Rooms that end in digits and are NOT a task's room. The first is a real
#: shape on the bus (an ad-hoc meld whose topic ended in a task number).
AD_HOC = ("meld-1790361906-hold-actor-backfill-3131",
          "meld-1790361906-3131",        # a 15-character "scope"
          "dm-3742",                     # the DM namespace
          "abcdefghijklmnopq-12",        # a 17-character scope
          "-3742", "helm--3742", "helm-3742-", "helm-", "helm-0",
          "helm-0123", "helm-1234567890", "Helm-3742", "helm_x-3742",
          "helm-3742-x", "helm", "main")


def plant(path, text="{}\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class TaskRoomLifeTest(tm.MeldBase):
    """Invariant 1: one room per chain for its life."""

    def plants(self, room):
        """(label, path) for each record that says `room` has opened."""
        return (("its chat log", chat.room_path(room)),
                ("its lifecycle journal", meld.lifecycle_path(room)),
                ("its durable lifecycle journal",
                 meld.lifecycle_path(room, durable=True)))

    def test_1_a_task_with_no_room_yet_gets_the_new_name(self):
        r = row()
        self.assertEqual(RD.pair_room(r, {ROOT: r}), (NEW, "task/3112"))
        other = row(repo="/x/adopter-project-dev/.git", lane="task-12")
        self.assertEqual(RD.pair_room(other, {ROOT: other}),
                         ("adopter-afd5094f-12", "task/12"))

    def test_1_a_task_whose_legacy_room_opened_keeps_it(self):  # noqa: VACUOUS_ASSERTION — a fixed three-plant tuple; each case asserts pair_room EQUAL to the new name, then to the legacy room
        root = row()
        kid = row(rid=KID, lane="renamed-r2")
        cur = {ROOT: root, KID: kid}
        for label, path in self.plants(LEGACY):
            with self.subTest(plant=label):
                # CONTROL: nothing planted, so the task gets the new name
                self.assertEqual(RD.pair_room(root, cur)[0], NEW)
                plant(path)
                self.assertEqual(RD.pair_room(root, cur),
                                 (LEGACY, "task/3112"))
                self.assertEqual(RD.pair_room(kid, cur)[0], LEGACY,
                                 "a later round left the chain's room")
                os.remove(path)

    def test_1_one_tasks_legacy_room_moves_no_other_task(self):
        plant(meld.lifecycle_path(LEGACY))
        other = row(lane="pair-meld-per-task-3113")
        self.assertEqual(RD.pair_room(other, {ROOT: other})[0], "helm-3113")
        mine = row()
        self.assertEqual(RD.pair_room(mine, {ROOT: mine})[0], LEGACY)

    def test_1_a_task_room_that_opened_under_the_new_name_stays(self):
        r = row()
        self.assertEqual(RD.pair_room(r, {ROOT: r})[0], NEW)
        meld.invite("seat-b", "round one", seat="seat-a", room=NEW, ring=RING)
        self.assertTrue(os.path.exists(chat.room_path(NEW)))
        self.assertEqual(RD.pair_room(r, {ROOT: r})[0], NEW)
        self.assertFalse(os.path.exists(chat.room_path(LEGACY)))


class TaskRoomRecognizerTest(unittest.TestCase):
    """Invariant 2, the pure half: both shapes are a task's pair meld, and
    the new recognizer is bounded."""

    def test_2_both_shapes_are_pair_rooms_and_meld_rooms(self):  # noqa: VACUOUS_ASSERTION — a fixed tuple of eight room names, each asserted True by both recognizers
        for room in (NEW, "example-3669", "adopter-afd5094f-12",
                     "abcdefghijklmnop-12", "a-1", "x9-999999999",
                     LEGACY, "meld-0-pair-helm-chain-" + ROOT[:12]):
            with self.subTest(room=room):
                self.assertTrue(RD.is_pair_room(room), room)
                self.assertTrue(RD.is_meld_room(room), room)

    def test_2_an_ad_hoc_room_ending_in_digits_is_not_a_task_room(self):
        # CONTROL: the longest scope the recognizer takes is admitted
        self.assertTrue(RD.is_pair_room("abcdefghijklmnop-12"))
        for room in AD_HOC:
            with self.subTest(room=room):
                self.assertFalse(RD.is_pair_room(room), room)
        # an ad-hoc meld stays a meld room: it is only not a TASK's room
        self.assertTrue(RD.is_meld_room(AD_HOC[0]))
        self.assertFalse(RD.is_meld_room("helm"))

    def test_2_a_meld_reference_names_either_shape(self):
        self.assertEqual(RD.split_meld_ref(NEW + "@1790000000"),
                         (NEW, 1790000000))
        self.assertEqual(RD.split_meld_ref(NEW), (NEW, None))
        # CONTROL: the legacy shapes read as before
        self.assertEqual(RD.split_meld_ref(LEGACY + "@5"), (LEGACY, 5))
        self.assertEqual(RD.split_meld_ref(AD_HOC[0]), (AD_HOC[0], None))
        for ref in ("helm@1", "helm-3742-x@1", NEW + "@", NEW + "@x"):
            with self.subTest(ref=ref):
                self.assertEqual(RD.split_meld_ref(ref), (None, None))

    def test_2_every_room_pair_room_mints_is_recognised(self):  # noqa: VACUOUS_ASSERTION — a fixed 7x2 grid; each minted room is asserted True by both recognizers and EQUAL to its slug
        """Whatever the project, the room pair_room names is one its readers
        admit: a scope the new shape cannot carry keeps the legacy name."""
        for repo in ("/x/helm/.git", "/x/adopter-project-dev/.git",
                     "/x/" + "long-project-name" * 4 + "/.git",
                     "/x/foo.bar/.git", "/x/meld/.git", "/x/dm/.git",
                     "/x/---/.git"):
            for lane in ("task-12", "plain-lane"):
                with self.subTest(repo=repo, lane=lane):
                    r = row(repo=repo, lane=lane)
                    room = RD.pair_room(r, {ROOT: r})[0]
                    self.assertTrue(RD.is_pair_room(room), room)
                    self.assertTrue(RD.is_meld_room(room), room)
                    self.assertEqual(pk.slug(room), room)
                    self.assertLessEqual(len(room), 60)

    def test_2_a_recorded_meld_room_of_either_shape_survives_the_fold(self):  # noqa: VACUOUS_ASSERTION — each fixed shape asserts the record EQUAL to a non-empty dict; the empty dict is the control
        for room in (NEW, LEGACY):
            with self.subTest(room=room):
                self.assertEqual(dispatches._meld_record(
                    {"meld_room": room, "meld_outcome": "agreed"}),
                    {"meld_room": room, "meld_outcome": "agreed"})
        # CONTROL: a room that is no meld is dropped
        self.assertEqual(dispatches._meld_record(
            {"meld_room": "helm", "meld_outcome": "agreed"}), {})


class TaskRoomReadersTest(tm.MeldBase):
    """Invariant 2, the readers: each one that finds, opens or cites a pair
    meld finds, opens and cites the new shape too."""

    def convener_state(self, room, seat="seat-a", epoch=None):
        epoch = int(time.time()) if epoch is None else epoch
        pk.atomic_write(meld.state_path(room, seat), json.dumps({
            "room": room, "role": "convener", "self": seat,
            "peer": "seat-b", "peers": ["seat-b"], "epoch": epoch,
            "status": "active"}))

    def test_2_the_lifecycle_enumeration_lists_both_shapes(self):  # noqa: VACUOUS_ASSERTION — the enumeration is asserted EQUAL to two named rooms, a positive value
        for room in (NEW, LEGACY, AD_HOC[0], AD_HOC[1],
                     "meld-0-standing-seat-a--seat-b"):
            plant(meld.lifecycle_path(room))
        self.assertEqual(RD._lifecycle_pair_rooms(), sorted([LEGACY, NEW]))

    def test_2_a_round_opens_in_a_task_room(self):
        room, _lines = meld.invite("seat-b", "round one", seat="seat-a",
                                   room=NEW, ring=RING)
        self.assertEqual(room, NEW)
        self.assertEqual(len(meld.seeds(chat.read(NEW)[0])), 1)
        # CONTROL: a room that is neither a meld nor a task's room is refused
        with self.assertRaises(SystemExit) as cm:
            meld.invite("seat-b", "round one", seat="seat-a", room="helm",
                        ring=RING)
        self.assertIn("not a meld room name", str(cm.exception))

    def test_2_a_task_rooms_outcome_is_read(self):
        epoch = 1790000000
        chat.post("[MELD e:%d] PROBLEM: task/3112 row %s at %s (chain %s) | "
                  "round 1 | pairing: mixed | convener=author invited=reader "
                  "cap=5 recv-timeout=90s | d [HOLD]"
                  % (epoch, ROOT[:12], TIP[:12], ROOT[:12]),
                  room=NEW, who="author", sign=False)
        for who in ("author", "reader"):
            chat.post("[MELD e:%d] %s [DONE]" % (epoch, tpm.outcome()),
                      room=NEW, who=who, sign=False)
        got = RD.room_outcome(NEW)
        self.assertTrue(got["agreed"], got["why"])
        self.assertEqual(got["parties"], ["author", "reader"])
        # CONTROL: a room that is no meld is refused by name
        self.assertEqual(RD.room_outcome("helm")["why"],
                         "not a meld room name")

    def test_2_the_diff_handoff_reference_names_either_shape(self):
        msg = "a" * 12
        r = row()
        for room in (NEW, LEGACY):
            with self.subTest(room=room):
                _f, why = dispatches._cite_diff_handoff(
                    "%s/%s" % (room, msg), r, {ROOT: r}, TIP)
                self.assertNotIn("needs the exact pair meld ROOM/MSGID", why)
                self.assertEqual(review_done._diff_handoff_words(
                    {"--diff-handoff": ["%s/%s" % (room, msg)]}, "fix"),
                    ["--diff-handoff", "%s/%s" % (room, msg)])
        # CONTROL: an ad-hoc room is no pair meld, so the reference is refused
        _f, why = dispatches._cite_diff_handoff(
            "%s/%s" % (AD_HOC[0], msg), r, {ROOT: r}, TIP)
        self.assertIn("needs the exact pair meld ROOM/MSGID", why)
        self.assertEqual(review_done._diff_handoff_words(
            {"--diff-handoff": ["%s/%s" % (AD_HOC[0], msg)]}, "fix"),
            ["--diff-handoff",
             review_done._q(review_done.DIFF_HANDOFF_PLACEHOLDER)])

    def test_2_the_door_census_reads_task_room_state(self):
        ad_hoc = "meld-1790000000-topic"
        for room in (NEW, ad_hoc):
            chat.post("a row", room=room, who="seat-a", sign=False)
            self.convener_state(room)
        got = RD._prior_join_rate(time.time() - 3600, 600)
        self.assertEqual(got["convened"] + got["unreadable"], 2, got)

    def test_2_the_T2_census_reads_task_room_state(self):  # noqa: VACUOUS_ASSERTION — two fixed rooms, each asserted IN the census list
        chain = ROOT[:12]
        for room in (NEW, LEGACY):
            with self.subTest(room=room):
                chat.post("[MELD e:1790000000] PROBLEM: agree the bar "
                          "(chain %s) | convener=seat-a invited=seat-b [HOLD]"
                          % chain, room=room, who="seat-a", sign=False)
                self.convener_state(room)
                got = RD._t2_rooms({chain: []}, "seat-a")
                self.assertIn(room, [x for x, _st in got.get(chain, [])])

    def test_2_the_council_reach_whisper_skips_a_task_room(self):  # noqa: VACUOUS_ASSERTION — the control room asserts read.called True on the same double
        from helm import inject
        for room, skipped in ((NEW, True), (LEGACY, True), ("side", False)):
            with self.subTest(room=room), \
                    mock.patch.dict(os.environ,
                                    {"HELM_CHAT_NAME": "seat-a"}), \
                    mock.patch.object(seats, "resolve_homing",
                                      return_value=(room, "test")), \
                    mock.patch.object(chat, "read",
                                      return_value=([], 0)) as read:
                self.assertIsNone(inject._council_reach(None, None,
                                                        persist=False))
                self.assertEqual(read.called, not skipped)


class ChainRoomNameTest(tm.MeldBase):
    """Invariant 3: a chain with no task keeps its chain-keyed name."""

    def test_3_a_chain_with_no_task_keeps_the_chain_name(self):
        bare = row(lane="plain-lane")
        self.assertEqual(RD.pair_room(bare, {ROOT: bare}),
                         ("meld-0-pair-helm-chain-" + ROOT[:12],
                          "chain/" + ROOT[:12]))
        # CONTROL: a task-keyed chain in the same project gets the new name
        task = row(lane="plain-task-12")
        self.assertEqual(RD.pair_room(task, {ROOT: task})[0], "helm-12")

    def test_3_two_tasks_on_the_first_row_keep_the_chain_name(self):  # noqa: VACUOUS_ASSERTION — the room is asserted EQUAL to the chain-keyed name, a positive value
        r = row(lane="task-1-and-task-2")
        self.assertEqual(RD.pair_room(r, {ROOT: r})[0],
                         "meld-0-pair-helm-chain-" + ROOT[:12])


class TaskRoomPrintTest(tpm._PairDoorBase):
    """Invariant 4, through the real CLI: what `dispatch send` prints and
    rings names the new room for a new task chain."""

    def scope(self):
        return RD.pair_scope({"repo_id": dispatches._repo_info(
            self.repo)["repo_id"]})

    def test_4_dispatch_send_prints_the_new_name(self):
        self.lane = "print-task-7101"
        room = "%s-%s" % (self.scope(),
                          self.review_task["id"].split("/", 1)[1])
        with mock.patch.object(seats, "dm",
                               return_value=({"id": "dm-1"}, None)) as dm:
            rc, out, err, sent = self.send(None, self.a)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.pair_room(sent), room)
        self.assertIn("your pair meld for this task: %s (%s, round 1"
                      % (room, self.review_task["id"]), out)
        self.assertIn("helm chat meld recv %s" % room, out)
        self.assertIn("PAIR MELD for %s: %s" % (self.review_task["id"], room),
                      dm.call_args[0][1])
        self.assertEqual(len(meld.seeds(chat.read(room)[0])), 1)

    def test_4_a_task_whose_legacy_room_opened_prints_that_room(self):  # noqa: VACUOUS_ASSERTION — the room is asserted EQUAL to the legacy name and its line IN the output; the absent new room is the falsifier beside them
        """CONTROL for the arm above, and invariant 1 through the CLI: the
        second round goes to the room the first one opened."""
        self.lane = "print-task-7103"
        legacy = "meld-0-pair-%s-task-%s" % (
            self.scope(), self.review_task["id"].split("/", 1)[1])
        meld.invite("seat-b", "an earlier round", seat="integrator",
                    room=legacy, ring=RING)
        rc, out, err, sent = self.send(None, self.a)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.pair_room(sent), legacy)
        self.assertIn("your pair meld for this task: %s (%s, round 2"
                      % (legacy, self.review_task["id"]), out)
        new_room = "%s-%s" % (
            self.scope(), self.review_task["id"].split("/", 1)[1])
        self.assertNotEqual(new_room, legacy)
        self.assertFalse(os.path.exists(chat.room_path(new_room)),
                         "the conversation split")
        # The same falsifier must fire if this task's new room appears.
        plant(chat.room_path(new_room))
        with self.assertRaisesRegex(AssertionError, "the conversation split"):
            self.assertFalse(os.path.exists(chat.room_path(new_room)),
                             "the conversation split")

    def test_4_the_T0_hint_names_the_new_room(self):
        self.lane = "prod-task-7104"
        rc, _out, err, sent = self.send(
            None, self.a, kind="build",
            body="run the migration against the prod database tonight")
        self.assertEqual(rc, 2)
        self.assertIsNone(sent)
        self.assertIn("--into %s-7104" % self.scope(), err)
        self.assertNotIn("--into meld-0-pair-", err)


def setUpModule():
    """No dispatch row this module writes walks the host's process table."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()


if __name__ == "__main__":
    unittest.main()
