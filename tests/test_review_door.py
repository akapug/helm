#!/usr/bin/env python3
"""The review door's meld predicates: what a meld must hand back to the rows.

A meld's output is the BAR, closed by each party's last [DONE] carrying one
MELD OUTCOME block. These arms pin the parser, the room reader and the
citation a verdict or hold records with `--meld ROOM`, over a temp chat home
and rows posted the way meld.py posts them.
"""
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import burnflags, chat, dispatches, meld, review_door as RD, seats, tasks  # noqa: E402
from tests._tmphome import pin_live_seats  # noqa: E402


def setUpModule():
    pin_live_seats()


TIP = "c" * 40
OTHER = "d" * 40
ENV_KEYS = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NODE_URL",
            "HELM_CHAT_ROOM", "HELM_CHAT_NAME")


def block(word="AGREED", tip=TIP, drop=None):
    fields = [("BAR", "the one harm"),
              ("FALSIFIERS", "a second room; a dark reader strands it"),
              ("FINDINGS", "F1=cured-in-patch; F2=note"),
              ("TIP", tip), ("NEXT", "record the verdict")]
    return "MELD OUTCOME: %s | %s" % (word, " | ".join(
        "%s: %s" % kv for kv in fields if kv[0] != drop))


class ParseOutcomeTest(unittest.TestCase):
    def test_a_whole_block_parses(self):
        got, why = RD.parse_outcome("@b [MELD e:1] %s [DONE]" % block())
        self.assertIsNone(why)
        self.assertEqual(got["outcome"], "AGREED")
        self.assertEqual(got["tip"], TIP)
        self.assertEqual(got["findings"], "F1=cured-in-patch; F2=note")
        self.assertEqual(got["next"], "record the verdict")

    def test_task_disposition_requires_context_and_same_open_story(self):
        self.assertIn("task/N", RD._finding_error("F1=task/not-an-id"))
        parsed, why = RD.parse_outcome(
            block().replace("F1=cured-in-patch", "F1=task/77"))
        self.assertIsNone(why, why)
        self.assertEqual(parsed["findings"], "F1=task/77; F2=note")

    def test_newline_separated_fields_parse_too(self):
        text = block().replace(" | ", "\n")
        self.assertEqual(RD.parse_outcome(text)[0]["bar"], "the one harm")

    def test_every_field_is_required(self):
        for field in RD.BAR_FIELDS:
            with self.subTest(field=field):
                got, why = RD.parse_outcome(block(drop=field))
                self.assertIsNone(got)
                self.assertIn(field, why)

    def test_the_tip_is_one_full_commit_id(self):
        got, why = RD.parse_outcome(block(tip="c0ffee"))
        self.assertIsNone(got)
        self.assertIn("full commit id", why)

    def test_the_outcome_word_is_closed(self):
        for word, ok in (("AGREED", True), ("split", True), ("RESEARCH", True),
                         ("DONE", False), ("", False)):
            with self.subTest(word=word):
                got, _why = RD.parse_outcome(block(word=word))
                self.assertEqual(got is not None, ok)

    def test_no_block_is_said(self):
        got, why = RD.parse_outcome("state + next action [DONE]")
        self.assertIsNone(got)
        self.assertIn("no MELD OUTCOME", why)


class RoomTest(unittest.TestCase):
    ROOM = "meld-1790000000-the-bar"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-review-door-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_ROOM"] = "main"
        os.environ.pop("HELM_CHAT_NAME", None)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, who, text, epoch=1790000000):
        chat.post("[MELD e:%d] %s" % (epoch, text), room=self.ROOM, who=who,
                  sign=False)

    LANE = "door-lane"

    def seed(self, convener="author", invited="reader", topic=None):
        self.post(convener, "PROBLEM: %s | convener=%s invited=%s cap=5 "
                  "recv-timeout=90s | discipline [HOLD]"
                  % (topic or self.LANE + ": the bar", convener, invited))

    def test_both_parties_agreed_on_one_tip(self):
        self.seed()
        self.post("reader", "findings first [YIELD]")
        self.post("author", "%s [DONE]" % block())
        self.post("reader", "%s [DONE]" % block())
        got = RD.room_outcome(self.ROOM)
        self.assertTrue(got["agreed"], got["why"])
        self.assertEqual(got["tip"], TIP)
        self.assertEqual(got["parties"], ["author", "reader"])

    def test_one_side_alone_agrees_nothing(self):
        """The convener closed the room with its own block; the reader never
        spoke. `done` is one side's word."""
        self.seed()
        self.post("author", "%s [DONE]" % block())
        got = RD.room_outcome(self.ROOM)
        self.assertFalse(got["agreed"])
        self.assertIsNone(got["outcome"])
        self.assertIn("no [DONE] from reader", got["why"])

    def test_a_reader_who_left_to_research_agrees_nothing(self):
        self.seed()
        self.post("author", "%s [DONE]" % block())
        self.post("reader", "%s [DONE]" % block("RESEARCH"))
        got = RD.room_outcome(self.ROOM)
        self.assertFalse(got["agreed"])
        self.assertEqual(got["outcome"], "SPLIT")

    def test_two_AGREED_blocks_that_contradict_each_other_agree_nothing(self):
        """FINDING 4. AGREED is one agreement: both blocks must name the same
        bar, the same disposition for every finding, the same tip and the
        same next action. Each field, contradicted alone, is a SPLIT."""
        for field, other in (("BAR", "a different harm"),
                             ("FINDINGS", "F1=note; F2=refuted"),
                             ("NEXT", "open another round")):
            with self.subTest(field=field):
                shutil.rmtree(os.environ["HELM_CHAT_DIR"], ignore_errors=True)
                self.seed()
                self.post("author", "%s [DONE]" % block())
                theirs = block().split(" | ")
                theirs = " | ".join(
                    part if not part.startswith(field + ":")
                    else "%s: %s" % (field, other) for part in theirs)
                self.post("reader", "%s [DONE]" % theirs)
                got = RD.room_outcome(self.ROOM)
                self.assertFalse(got["agreed"])
                self.assertEqual(got["outcome"], "SPLIT")
                self.assertIn("differ on %s" % field, got["why"])
                fields, why = RD.meld_citation(
                    self.ROOM, {"sender": "author", "recipient": "reader",
                                "lane": self.LANE}, [TIP])
                self.assertIsNone(why)
                self.assertEqual(fields["meld_outcome"], "split")

    def test_a_block_that_says_two_things_agrees_nothing(self):  # noqa: VACUOUS_ASSERTION — the clean-block control runs unconditionally before the loop and asserts agreed plus a recorded citation on the same observables
        """A party's ONE block naming a field twice, or one finding with two
        dispositions, is ambiguous: a reader keeping the last occurrence read
        AGREED where the party had also written the opposite. Not agreed, and
        the citation is refused naming the duplicate. The first case is the
        control: one clean block per party agrees."""
        clean = block()
        cases = (
            ("BAR", clean.replace("BAR: the one harm",
                                  "BAR: a contradictory harm | "
                                  "BAR: the one harm"), "BAR twice"),
            ("FINDINGS", clean.replace("F1=cured-in-patch",
                                       "F1=refuted; F1=cured-in-patch"),
             "finding f1 twice"),
            ("FINDINGS spelled apart", clean.replace(
                "F1=cured-in-patch", "F1=refuted; f1 =cured-in-patch"),
             "finding f1 twice"),
            ("TIP", clean.replace("TIP: %s" % TIP, "TIP: %s | TIP: %s"
                                  % (OTHER, TIP)), "TIP twice"),
            ("NEXT", clean.replace("NEXT: record the verdict",
                                   "NEXT: open another round | "
                                   "NEXT: record the verdict"), "NEXT twice"),
            ("two blocks", clean + " " + block("SPLIT"),
             "two MELD OUTCOME blocks"))
        def read(theirs):
            shutil.rmtree(os.environ["HELM_CHAT_DIR"], ignore_errors=True)
            self.seed()
            self.post("author", "%s [DONE]" % clean)
            self.post("reader", "%s [DONE]" % theirs)
            return RD.room_outcome(self.ROOM), RD.meld_citation(
                self.ROOM, {"sender": "author", "recipient": "reader",
                            "lane": self.LANE}, [TIP])

        got, (fields, why) = read(clean)
        self.assertTrue(got["agreed"], got["why"])
        self.assertEqual(fields["meld_outcome"], "agreed")
        for name, theirs, dup in cases:
            with self.subTest(case=name):
                got, (fields, why) = read(theirs)
                self.assertFalse(got["agreed"])
                self.assertNotEqual(got["outcome"], "AGREED")
                self.assertIn(dup, got["why"])
                self.assertIsNone(fields)
                self.assertIn(dup, why)

    def test_the_same_block_spelled_differently_still_agrees(self):
        """THE CONTROL: case, spacing and finding order are not content."""
        self.seed()
        self.post("author", "%s [DONE]" % block())
        self.post("reader", "MELD OUTCOME: agreed | bar:  THE ONE harm | "
                  "falsifiers: A dark reader  strands it;a second room | "
                  "FINDINGS: F2=note ;F1=cured-in-patch | TIP: %s | "
                  "NEXT: Record the verdict [DONE]" % TIP)
        self.assertTrue(RD.room_outcome(self.ROOM)["agreed"])

    def test_two_tips_are_recorded_as_split_never_agreed(self):
        """FINDING 5. The citation records exactly the room's verdict: two
        AGREED blocks on different tips are not an agreement, so the row
        must not read `agreed` because the tip check had nothing to check."""
        self.seed(topic="door-lane: the bar (chain %s)" % ("ab" * 6))
        self.post("author", "%s [DONE]" % block())
        self.post("reader", "%s [DONE]" % block(tip=OTHER))
        fields, why = RD.meld_citation(
            self.ROOM, {"sender": "author", "recipient": "reader",
                        "lane": self.LANE, "chain_root": "ab" * 16},
            [TIP, OTHER])
        self.assertIsNone(why)
        self.assertEqual(fields["meld_outcome"], "split")

    def test_two_tips_agree_nothing(self):
        self.seed()
        self.post("author", "%s [DONE]" % block())
        self.post("reader", "%s [DONE]" % block(tip=OTHER))
        got = RD.room_outcome(self.ROOM)
        self.assertFalse(got["agreed"])
        self.assertIn("differ on TIP", got["why"])

    def test_the_LAST_done_of_each_party_is_the_one_read(self):
        self.seed()
        self.post("author", "%s [DONE]" % block("RESEARCH"))
        self.post("reader", "%s [DONE]" % block())
        self.post("author", "%s [DONE]" % block())
        self.assertTrue(RD.room_outcome(self.ROOM)["agreed"])

    def test_a_stale_epoch_and_a_non_party_are_never_read(self):
        self.seed()
        self.post("author", "%s [DONE]" % block())
        self.post("reader", "%s [DONE]" % block(), epoch=1)       # dead meld
        self.post("stranger", "%s [DONE]" % block())
        got = RD.room_outcome(self.ROOM)
        self.assertFalse(got["agreed"])
        self.post("reader", "%s [DONE]" % block())
        self.assertTrue(RD.room_outcome(self.ROOM)["agreed"])

    def test_a_room_that_is_not_a_meld_room_is_refused_by_name(self):
        for room in ("main", "meld-x", "meld-1-" + "r" * 200, "meld-1-a\nb"):
            with self.subTest(room=room):
                got = RD.room_outcome(room)
                self.assertFalse(got["agreed"])
                self.assertEqual(got["parties"], [])

    def test_the_citation_records_the_outcome_between_the_rows_parties(self):
        self.seed()
        self.post("author", "%s [DONE]" % block())
        self.post("reader", "%s [DONE]" % block())
        row = {"sender": "author", "recipient": "reader", "lane": self.LANE}
        fields, why = RD.meld_citation(self.ROOM, row, [TIP])
        self.assertIsNone(why)
        size = fields.pop("meld_bytes")
        # exact-round authority: the citation names the round it read
        self.assertEqual(fields.pop("meld_epoch"), 1790000000)
        self.assertEqual(fields, {"meld_room": self.ROOM,
                                  "meld_outcome": "agreed"})
        # falsifier (g): the room's size rides the citation onto the row
        self.assertEqual(size, sum(len(m["text"].encode("utf-8"))
                                   for m in chat.read(self.ROOM)[0]))
        _f, why = RD.meld_citation(self.ROOM, row, [OTHER])
        self.assertIn("which this record is not about", why)
        _f, why = RD.meld_citation(
            self.ROOM, {"sender": "author", "recipient": "someone",
                        "lane": self.LANE}, [TIP])
        self.assertIn("not this row's author and reader", why)

    def test_task_disposition_binds_only_open_work_in_reviewed_story(self):
        reviewed, err = tasks.add("reviewed work", "author",
                                  project="helm-test", force_new=True)
        self.assertIsNone(err, err)
        self.assertEqual(reviewed["status"], "open")
        same, err = tasks.add("first finding", "author",
                              project="helm-test", continues=reviewed["id"],
                              force_new=True)
        self.assertIsNone(err, err)
        self.assertEqual(same["continues"], reviewed["id"])
        other, err = tasks.add("a different story", "author",
                               project="helm-test", force_new=True)
        self.assertIsNone(err, err)
        self.assertEqual(other["status"], "open")
        self.seed()
        row = {"sender": "author", "recipient": "reader", "lane": self.LANE,
               "task": reviewed["id"], "id": "a" * 32}
        # This narrow citation unit supplies its root in the current dispatch
        # projection; a caller-only dict cannot authorize the reviewed task.
        snapshot = mock.patch.object(dispatches, "snapshot",
                                     return_value=({row["id"]: row}, None))
        snapshot.start()
        self.addCleanup(snapshot.stop)
        for tid, accepted in ((same["id"], True), (other["id"], False),
                              ("task/999999", False)):
            with self.subTest(tid=tid):
                outcome = block().replace("F1=cured-in-patch",
                                          "F1=%s" % tid)
                self.post("author", "%s [DONE]" % outcome)
                self.post("reader", "%s [DONE]" % outcome)
                fields, why = RD.meld_citation(self.ROOM, row, [TIP])
                self.assertEqual(fields is not None, accepted, why)
                if not accepted:
                    self.assertIn(tid, why)
        closed, err = tasks.update(same["id"], status="closed",
                                   closed_reason="verified in scratch fixture")
        self.assertIsNone(err, err)
        outcome = block().replace("F1=cured-in-patch",
                                  "F1=%s" % same["id"])
        self.post("author", "%s [DONE]" % outcome)
        self.post("reader", "%s [DONE]" % outcome)
        fields, why = RD.meld_citation(self.ROOM, row, [TIP])
        self.assertIsNone(fields)
        self.assertIn("open", why)

    def test_a_split_meld_is_recordable_as_split(self):
        self.seed()
        self.post("author", "%s [DONE]" % block())
        self.post("reader", "%s [DONE]" % block("SPLIT"))
        fields, why = RD.meld_citation(
            self.ROOM, {"sender": "author", "recipient": "reader",
                        "lane": self.LANE}, [TIP])
        self.assertIsNone(why)
        self.assertEqual(fields["meld_outcome"], "split")

    def test_a_meld_without_every_outcome_is_not_citable(self):
        self.seed()
        self.post("author", "%s [DONE]" % block())
        self.post("reader", "done, no block [DONE]")
        fields, why = RD.meld_citation(
            self.ROOM, {"sender": "author", "recipient": "reader",
                        "lane": self.LANE}, [TIP])
        self.assertIsNone(fields)
        self.assertIn("no readable MELD OUTCOME from every party", why)


class NotAboutThisWorkTest(unittest.TestCase):
    """The `--meld` refusal for a meld that was not about the row's work
    names the proof that failed and, for the subject, the exact marker.

    THE FIXTURES ARE REAL STATEMENTS that name the row, its chain and its
    tip in prose, and none of them binds: the second one names the chain in
    the words a refusal that only says 'must name chain <id12>' asks for.
    The binding token is `(chain <id12>)`, with the parentheses, which
    every invite helm prints carries (`chain_mark`), so the refusal prints
    that token and an invite that carries it."""

    ROW = {"id": "ec4ec9759b70fcb4347c179c276a4626", "sender": "author",
           "recipient": "reader", "lane": "launch-payments",
           "chain_root": "c67061c8073083e828782234d69992bb"}
    TIP = "fed587de73760fec015857be6910540880190cb5"
    STATEMENTS = (
        "launch-payments closing row ec4ec9759b70: machine-readable bar for "
        "exact tip fed587de737",
        "chain c67061c80730 closing row ec4ec9759b70 launch-payments bar at "
        "fed587de73760fec015857be6910540880190cb5",
        "opening-row=ec4ec9759b70fcb4347c179c276a4626 "
        "opening-chain=c67061c8073083e828782234d69992bb | "
        "chain/c67061c8073083e828782234d69992bb launch-payments closing "
        "review at fed587de73760fec015857be6910540880190cb5")
    EPOCH = 1790373088

    setUp, tearDown = RoomTest.setUp, RoomTest.tearDown

    def room(self, n, topic, tips=None):
        """A closed meld in its own room: the seed carries `topic`, and each
        party's [DONE] carries one block on its tip (`tips`, author first)."""
        room = "meld-%d-closing-%d" % (self.EPOCH + n, n)
        chat.post("[MELD e:%d] PROBLEM: %s | convener=author invited=reader "
                  "cap=5 recv-timeout=90s | discipline [HOLD]"
                  % (self.EPOCH + n, topic), room=room, who="author",
                  sign=False)
        for who, tip in zip(("author", "reader"), tips or (self.TIP,) * 2):
            chat.post("[MELD e:%d] %s [DONE]" % (self.EPOCH + n,
                                                 block(tip=tip)),
                      room=room, who=who, sign=False)
        return room

    def test_each_real_statement_is_refused_with_the_marker_it_lacks(self):  # noqa: VACUOUS_ASSERTION — the loop's `why` is each element of `got`, and the unconditional controls before it assert three refusals and the marker in the second one
        got = [RD.meld_citation(self.room(n, topic), self.ROW, [self.TIP])
               for n, topic in enumerate(self.STATEMENTS)]
        self.assertEqual([fields for fields, _why in got], [None] * 3)
        self.assertIn("(chain c67061c80730)", got[1][1])
        self.assertIn("carries no chain marker", got[1][1])
        for topic, (_fields, why) in zip(self.STATEMENTS, got):
            with self.subTest(statement=topic[:40]):
                self.assertIn("carries no chain marker", why)
                self.assertIn("(chain c67061c80730)", why)
                self.assertNotIn("a row with no chain", why)
                self.assertNotIn("every tip its blocks name", why)

    def test_the_invite_the_refusal_prints_opens_a_meld_that_is_cited(self):
        """The remedy is proven by using it: the topic inside the printed
        invite, as a new meld's statement, is accepted on the same row."""
        _fields, why = RD.meld_citation(self.room(0, self.STATEMENTS[1]),
                                        self.ROW, [self.TIP])
        invite = RD.meld_invite("author", "launch-payments", "UNDER-ARMED",
                                self.ROW["chain_root"])
        self.assertIn(invite, why)
        topic = invite.split('"')[1]
        fields, why = RD.meld_citation(self.room(1, topic), self.ROW,
                                       [self.TIP])
        self.assertIsNone(why)
        self.assertEqual(fields["meld_outcome"], "agreed")

    def test_the_real_statement_with_the_marker_added_is_cited(self):
        """THE CONTROL: the second room's own words plus the token."""
        topic = self.STATEMENTS[1] + " (chain c67061c80730)"
        fields, why = RD.meld_citation(self.room(0, topic), self.ROW,
                                       [self.TIP])
        self.assertIsNone(why)
        self.assertEqual(fields["meld_outcome"], "agreed")

    def test_a_marker_for_another_chain_is_named_beside_this_one(self):
        topic = self.STATEMENTS[1] + " (chain %s)" % ("ab" * 6)
        _fields, why = RD.meld_citation(self.room(0, topic), self.ROW,
                                        [self.TIP])
        self.assertIn("(chain %s)" % ("ab" * 6), why)
        self.assertIn("(chain c67061c80730)", why)
        self.assertNotIn("carries no chain marker", why)

    def test_a_split_on_a_tip_outside_the_record_names_the_tip(self):
        """A SPLIT has no agreed tip, so the earlier tip check passes it and
        this one is the only reader of each party's tip. The refusal names
        the tip and leaves the statement, which is right, alone."""
        topic = self.STATEMENTS[1] + " (chain c67061c80730)"
        _fields, why = RD.meld_citation(
            self.room(0, topic, (self.TIP, OTHER)), self.ROW, [self.TIP])
        self.assertIn(OTHER[:12], why)
        self.assertNotIn("chain marker", why)


from tests import test_dispatches as td  # noqa: E402

run = td.run


class AboutChainTest(unittest.TestCase):
    """`about_chain` needs BOTH proofs: every tip the blocks name is one this
    chain sent or a reviewer patched, and the problem statement names this
    chain. A statement with a chain marker never falls through to the lane;
    one without binds by the lane only for a caller with no chain id."""

    A, B = "a" * 32, "b" * 32

    def got(self, seed, tip=TIP, split=None):
        if split:
            return {"seed": seed, "tip": None, "outcomes": {
                "author": {"tip": split[0]}, "reader": {"tip": split[1]}}}
        return {"seed": seed, "tip": tip, "outcomes": {
            "author": {"tip": tip}, "reader": {"tip": tip}}}

    def mark(self, chain):
        return "door-lane: the bar (chain %s)" % chain[:12]

    def test_a_marker_for_this_chain_on_a_tip_outside_it_is_not_about_it(self):
        seed = self.mark(self.A)
        self.assertTrue(RD.about_chain(self.got(seed), self.A, "door-lane",
                                       [TIP]))
        self.assertFalse(RD.about_chain(self.got(seed, OTHER), self.A,
                                        "door-lane", [TIP]))

    def test_a_marker_for_ANOTHER_chain_never_falls_through_to_the_lane(self):
        seed = self.mark(self.A)
        self.assertTrue(RD.about_chain(self.got(seed), self.A, "door-lane",
                                       [TIP]))
        self.assertFalse(RD.about_chain(self.got(seed), self.B, "door-lane",
                                        [TIP]))

    def test_this_chain_its_marker_and_its_tip_are_about_it(self):
        """THE CONTROL: the right chain, a member tip, the matching marker."""
        self.assertTrue(RD.about_chain(self.got(self.mark(self.A)),
                                       self.A, "door-lane", [TIP, OTHER]))

    def test_every_marker_must_be_this_chains(self):
        self.assertTrue(RD.about_chain(self.got(self.mark(self.A)), self.A,
                                       "door-lane", [TIP]))
        seed = "door-lane: the bar (chain %s) (chain %s)" % (
            self.A[:12], self.B[:12])
        self.assertFalse(RD.about_chain(self.got(seed), self.A, "door-lane",
                                        [TIP]))

    def test_a_split_binds_by_every_tip_its_parties_name(self):
        seed = self.mark(self.A)
        self.assertTrue(RD.about_chain(self.got(seed, split=(TIP, OTHER)),
                                       self.A, "door-lane", [TIP, OTHER]))
        self.assertFalse(RD.about_chain(self.got(seed, split=(TIP, OTHER)),
                                        self.A, "door-lane", [TIP]))

    def test_a_lane_only_statement_binds_only_a_caller_with_no_chain_id(self):
        """Kept for the callers that have no id to match: T0's design meld,
        sent before the build row's chain exists, and a legacy lane-keyed
        chain. Every invite printed for a chain with an id carries its
        marker, so such a caller requires it."""
        seed = "door-lane: design meld"
        self.assertTrue(RD.about_chain(self.got(seed), None, "door-lane",
                                       [TIP]))
        self.assertFalse(RD.about_chain(self.got(seed, OTHER), None,
                                        "door-lane", [TIP]))
        self.assertFalse(RD.about_chain(self.got(seed), self.A, "door-lane",
                                        [TIP]))

    def test_every_printed_invite_carries_the_marker_it_is_bound_by(self):
        invite = RD.meld_invite("reader", "door-lane", "MELD", self.A)
        self.assertIn("(chain %s)" % self.A[:12], invite)
        self.assertNotIn("(chain", RD.meld_invite("reader", "door-lane",
                                                  "MELD", "lane:door-lane"))


class MeldOutcomeRidesTheRowTest(td.DispatchBase):
    """A4: the reviewer records the meld's outcome on the row, naming the
    room, through the verdict and hold doors that already exist. The fixture
    author is the dispatch home's own seat; the reader is `seat-b`."""

    ROOM = "meld-1790000000-the-bar"
    EPOCH = 1790000000

    def setUp(self):
        """Every hold here is the READER's: the source-clean door admits only
        the row's recipient (task/3053). Rows are still authored by the
        fixture's own seat, so none is a self-review."""
        super().setUp()
        self.review_task, err = tasks.add("meld reviewed work", "author",
                                          project="helm-test", force_new=True)
        self.assertIsNone(err, err)
        real = dispatches._acting_author

        def acting(action="author this dispatch"):
            if action == "hold this row":
                return "seat-b", None
            return real(action)
        patch = mock.patch.object(dispatches, "_acting_author", acting)
        patch.start()
        self.addCleanup(patch.stop)

    def add(self, **kwargs):
        kwargs.setdefault("task", self.review_task["id"])
        return super().add(**kwargs)

    def meld(self, tip, word="AGREED", parties=("integrator", "seat-b"),
             about=None):
        """A closed meld. `about` is the row whose chain the problem
        statement names; a meld binds to the chain it was about."""
        convener, reader = parties
        chain = (about or {}).get("chain_root") or "f" * 12
        chat.post("[MELD e:%d] PROBLEM: bar (chain %s) | convener=%s "
                  "invited=%s cap=5 recv-timeout=90s | discipline [HOLD]"
                  % (self.EPOCH, chain[:12], convener, reader),
                  room=self.ROOM, who=convener, sign=False)
        for who in parties:
            chat.post("[MELD e:%d] %s [DONE]" % (self.EPOCH, block(word, tip)),
                      room=self.ROOM, who=who, sign=False)

    def fix(self, row, *flags):
        with self.verdict_author():
            return run(dispatches.cmd_dispatch, [
                "verdict", row["id"], row["tip"], "--fix", "--measured",
                "--finding", "the one harm inside the bar", "--prior-relation", "new",
                "--worse-than-main", "helm/dispatches.py", *flags,
                "the one harm inside the bar, cured"])

    def test_a_fix_with_the_patch_records_the_agreed_meld(self):
        row = self.add(ref=self.b, recipient="seat-b")
        self.meld(self.b, about=row)
        rc, out, err = self.fix(row, "--patch-tip", self.c, "--meld", self.ROOM)
        self.assertEqual(rc, 0, err)
        self.assertIn("meld %s — AGREED" % self.ROOM, out)
        folded = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(folded["meld_room"], self.ROOM)
        self.assertEqual(folded["meld_outcome"], "agreed")

    def test_a_meld_FIX_without_a_cure_is_refused_and_the_cure_admits_it(self):
        row = self.add(ref=self.b, recipient="seat-b")
        self.meld(self.b, about=row)
        rc, _out, err = self.fix(row, "--no-patch-because", "design",
                                 "--meld", self.ROOM)
        self.assertEqual(rc, 1, err)
        self.assertIn("--meld on a FIX needs --patch-tip", err)
        rc, _out, err = self.fix(row, "--patch-tip", self.c, "--meld", self.ROOM)
        self.assertEqual(rc, 0, err)

    def test_meld_diff_fix_can_cite_an_author_applied_cure_without_patch_tip(self):
        row = self.add(ref=self.b, recipient="seat-b")
        self.forge_open(row["id"], review_mode="MELD-DIFF")
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["review_mode"],
                         "MELD-DIFF")
        self.meld(self.b, about=row)
        rc, out, err = self.fix(row, "--no-patch-because",
                                "author applies the agreed diff",
                                "--meld", self.ROOM)
        self.assertEqual(rc, 0, err)
        self.assertIn("meld %s — AGREED" % self.ROOM, out)
        folded = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(folded["meld_outcome"], "agreed")
        self.assertEqual(folded["no_patch_because"],
                         "author applies the agreed diff")
        self.assertNotIn("patch_tip", folded)

    def test_meld_diff_does_not_exempt_an_uncured_fix(self):
        row = self.add(ref=self.b, recipient="seat-b")
        self.forge_open(row["id"], review_mode="MELD-DIFF")
        self.meld(self.b, about=row)
        self.assertIsNone(dispatches._cite_meld(
            self.ROOM, dispatches.snapshot()[0][row["id"]], "fix",
            (row["tip"], ""))[0])

    def test_a_meld_between_other_seats_is_refused(self):
        row = self.add(ref=self.b, recipient="seat-b")
        self.meld(self.b, parties=("integrator", "seat-c"), about=row)
        rc, _out, err = self.fix(row, "--patch-tip", self.c, "--meld", self.ROOM)
        self.assertEqual(rc, 1)
        self.assertIn("not this row's author and reader", err)

    def test_a_meld_about_another_tip_is_refused(self):
        row = self.add(ref=self.b, recipient="seat-b")
        self.meld(self.side, about=row)
        rc, _out, err = self.fix(row, "--patch-tip", self.c, "--meld", self.ROOM)
        self.assertEqual(rc, 1)
        self.assertIn("which this record is not about", err)

    def test_a_research_meld_is_recorded_as_research(self):
        """Recordable, because the verdict after it is real; it simply does
        not read as melded."""
        row = self.add(ref=self.b, recipient="seat-b")
        self.meld(self.b, word="RESEARCH", about=row)
        rc, out, err = self.fix(row, "--patch-tip", self.c, "--meld", self.ROOM)
        self.assertEqual(rc, 0, err)
        self.assertIn("not agreed", out)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["meld_outcome"],
                         "research")

    def test_a_source_clean_hold_records_the_meld_and_a_release_drops_it(self):
        row = self.add(ref=self.b, recipient="seat-b")
        self.meld(self.b, about=row)
        rc, out, err = run(dispatches.cmd_dispatch, [
            "hold", row["id"], "read clean inside the bar; fab Ran 5 tests OK",
            "--source-clean", self.b, "--meld", self.ROOM])
        self.assertEqual(rc, 0, err)
        self.assertIn("meld %s — AGREED" % self.ROOM, out)
        held = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(held["meld_outcome"], "agreed")
        rc, _out, err = run(dispatches.cmd_dispatch, ["release", row["id"]])
        self.assertEqual(rc, 0, err)
        released = dispatches.snapshot()[0][row["id"]]
        self.assertNotIn("meld_room", released)
        self.assertNotIn("meld_outcome", released)

    def test_a_repeated_clean_hold_with_a_new_meld_refuses_loudly(self):
        """FINDING 9. The same source-clean hold repeated with a --meld it did
        not carry must not return as a no-op that drops the citation: it
        refuses and names the move that records it."""
        row = self.add(ref=self.b, recipient="seat-b")
        self.meld(self.b, about=row)
        rc, _out, err = run(dispatches.cmd_dispatch, [
            "hold", row["id"], "read clean; fab Ran 5 tests OK", "--source-clean", self.b])
        self.assertEqual(rc, 0, err)
        rc, _out, err = run(dispatches.cmd_dispatch, [
            "hold", row["id"], "read clean; fab Ran 5 tests OK", "--source-clean", self.b,
            "--meld", self.ROOM])
        self.assertEqual(rc, 1, "the citation was dropped silently")
        self.assertIn("was NOT recorded", err)
        self.assertNotIn("meld_room", dispatches.snapshot()[0][row["id"]])

    def test_a_release_returns_what_the_replay_reads(self):
        """FINDING 10. The release's own return must drop the hold's meld
        and hand, as the replay does: the caller is told what the ledger
        says."""
        row = self.add(ref=self.b, recipient="seat-b")
        self.meld(self.b, about=row)
        rc, _out, err = run(dispatches.cmd_dispatch, [
            "hold", row["id"], "read clean; fab Ran 5 tests OK", "--source-clean", self.b,
            "--meld", self.ROOM])
        self.assertEqual(rc, 0, err)
        released, why = dispatches.mark_release(row["id"])
        self.assertIsNone(why)
        replayed = dispatches.snapshot()[0][row["id"]]
        for key in ("meld_room", "meld_outcome", "hold_actor",
                    "source_clean_tip"):
            self.assertNotIn(key, released, key)
            self.assertEqual(key in released, key in replayed, key)

    def test_a_hold_names_a_meld_only_with_a_clean_claim(self):
        row = self.add(ref=self.b, recipient="seat-b")
        self.meld(self.b, about=row)
        rc, _out, err = run(dispatches.cmd_dispatch, [
            "hold", row["id"], "waiting; fab Ran 5 tests OK", "--meld", self.ROOM])
        self.assertEqual(rc, 1)
        self.assertIn("only with --source-clean", err)
        rc, _out, err = run(dispatches.cmd_dispatch, [
            "hold", row["id"], "waiting; fab Ran 5 tests OK", "--source-clean", self.b,
            "--meld", self.ROOM])
        self.assertEqual(rc, 0, err)

    def test_replay_keeps_only_the_shape_the_writer_emits(self):
        state = {"status": "open", "tip": self.b}
        self.assertEqual(dispatches._meld_record(
            {"meld_room": self.ROOM, "meld_outcome": "agreed"}),
            {"meld_room": self.ROOM, "meld_outcome": "agreed"})
        for forged in ({"meld_room": "main", "meld_outcome": "agreed"},
                       {"meld_room": self.ROOM, "meld_outcome": "AGREED"},
                       {"meld_room": self.ROOM, "meld_outcome": "yes"},
                       {"meld_room": self.ROOM}, {}):
            self.assertEqual(dispatches._meld_record(forged), {}, forged)
        self.assertEqual(state["status"], "open")


class DoorBase(td.DispatchBase):
    """The real CLI, ledger and fold. The author is the dispatch home's seat;
    the reader is `seat-b`. The live beacon probe is injected, never the
    host's process table."""

    READER = "seat-b"

    def setUp(self):
        super().setUp()
        self.beacon = None
        patch = mock.patch.object(RD, "live_beacon",
                                  side_effect=lambda peer: self.beacon)
        patch.start()
        self.addCleanup(patch.stop)
        self._lane = 0
        self.review_task, err = tasks.add("fixture review task", "author",
                                          project="helm-test", force_new=True)
        self.assertIsNone(err, err)

    def add(self, **kwargs):
        if kwargs.get("kind") == "review" and "supersedes" not in kwargs:
            kwargs.setdefault("task", self.review_task["id"])
        return super().add(**kwargs)

    def first(self, tip):
        type(self)._lane_n = getattr(type(self), "_lane_n", 0) + 1
        self.lane = "door-lane-%d" % type(self)._lane_n
        return self.add(ref=tip, recipient=self.READER, kind="review",
                        lane=self.lane, task=self.review_task["id"])

    def fix(self, row, path="helm/a.py", **kw):
        kw.setdefault("finding_count", 1)
        kw.setdefault("prior_relation", "new")
        if "patch_tip" not in kw:
            kw.setdefault("no_patch_because", "a design finding")
            kw.setdefault("findings", ["design finding %d in %s (%s)" %
                                       (i + 1, path, row["id"][:12])
                                       for i in range(kw["finding_count"])])
        out, why = dispatches.mark_verdict(
            row["id"], row["tip"], "one finding", "fix", basis="measured",
            worse_than_main_paths=[path], **kw)
        self.assertIsNone(why, why)
        return out

    def send(self, parent, tip, *flags, kind="review", body="read this tip"):
        args = ["send", self.READER, self.lane, body, "--ref", tip,
                "--kind", kind, "--repo", self.repo]
        args += ["--supersedes", parent["id"]] if parent else [
            "--new-work", "--task", self.review_task["id"], "--part"]
        rc, out, err = run(dispatches.cmd_dispatch, args + list(flags))
        # THE ROW THE VERB SAYS IT WROTE: rows of one chain share a lane and
        # often a second, so "newest by timestamp" would pick the parent.
        import re
        made = re.search(r"helm dispatch: ([0-9a-f]{32}) @", out)
        rows = dispatches.snapshot()[0]
        return rc, out, err, rows.get(made.group(1)) if made else None

    def branch(self, name, base):
        """A new commit off `base`, so each round is a distinct tip."""
        self.git("checkout", "-q", "-B", "b-" + name, base)
        tip = self.commit_file(name, name)
        self.git("checkout", "-q", self.main)
        return tip

    def rooms(self):
        """Every meld room this test's sends opened BESIDE the chain's pair
        meld, which every send now opens a round in (the pair room is
        `pair_room()`)."""
        return sorted(f for f in os.listdir(chat.chat_dir())
                      if f.startswith("meld-") and ".meld." in f
                      and not f.startswith(RD.PAIR_PREFIX))

    def pair_room(self, row):
        room, _key = RD.pair_room(row)
        return room

    def latest_seed(self, room):
        return meld.latest_seed(chat.read(room)[0])[2] or ""

    def agreed_meld(self, chain, tip, lane="the bar"):
        """A meld this seat convened with the reader, closed AGREED by both,
        whose problem statement names `chain`."""
        from helm import meld as _m, pk
        epoch = int(time.time())
        room = "meld-%d-the-bar-%s" % (epoch, str(chain)[:6])
        pk.atomic_write(_m.state_path(room, "integrator"), json.dumps({
            "room": room, "status": "done-mutual", "role": "convener",
            "self": "integrator", "peer": self.READER,
            "peers": [self.READER], "epoch": epoch, "exchanges": 2,
            "spoke_peers": [self.READER], "done_peers": [self.READER]}))
        chat.post("[MELD e:%d] PROBLEM: %s (chain %s) | convener=integrator "
                  "invited=%s cap=5 recv-timeout=90s | d [HOLD]"
                  % (epoch, lane, str(chain)[:12], self.READER), room=room,
                  who="integrator", sign=False)
        for who in ("integrator", self.READER):
            chat.post("[MELD e:%d] %s [DONE]" % (epoch, block(tip=tip)),
                      room=room, who=who, sign=False)
        return room

    def room_of(self, state_file):
        with open(os.path.join(chat.chat_dir(), state_file)) as f:
            return json.load(f)["room"]

    def two_answered(self):
        one = self.first(self.a)
        self.fix(one)
        rc, _out, err, two = self.send(one, self.b)
        self.assertEqual(rc, 0, err)
        self.fix(two, path="helm/b.py")
        return two


class ReviewDoorTest(DoorBase):
    """T0-T3 at `dispatch send`."""

    WHISPER_RULES = (
        "xfam-reviewer-fixes-its-own-findings",
        "per-case-handler-spiral-cure-is-whole-object-or-refuse",
        "xfam-review-never-blocks-a-reversible-land",
    )

    def test_round_three_whispers_the_three_rules_and_full_falsifier_ask_everywhere(self):  # noqa: VACUOUS_ASSERTION — unconditional block and delivery controls precede the surface loop
        """Rounds 1-2 stay quiet; round 3 prints, stores, delivers and seeds one block."""
        one = self.first(self.a)
        self.fix(one)
        rc, out, err, two = self.send(one, self.b)
        self.assertEqual(rc, 0, err)
        for text in (out, dispatches.brief_of(two)[0],
                     self.latest_seed(self.pair_room(two))):
            self.assertNotIn(self.WHISPER_RULES[0], text)

        self.fix(two, path="helm/b.py")
        delivered = []
        with mock.patch.object(
                seats, "dm",
                side_effect=lambda _to, text, **_kw:
                (delivered.append(text) or {"id": "dm-round-3"}, None)):
            rc, out, err, three = self.send(two, self.c)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(delivered), 1)
        block = RD.round_whisper(3)
        self.assertTrue(block)
        for text in (out, dispatches.brief_of(three)[0], delivered[0],
                     self.latest_seed(self.pair_room(three))):
            with self.subTest(surface=text[:40]):
                self.assertIn(block, text)
                for rule in self.WHISPER_RULES:
                    self.assertIn(rule, text)
                self.assertIn("FULL falsifier set", text)
        self.assertEqual(RD.round_whisper(1), "")
        self.assertEqual(RD.round_whisper(2), "")

    def test_a_changed_reading_at_the_same_round_refuses_stale_meld_plan(self):  # noqa: VACUOUS_ASSERTION — the T2 preflight is a positive control and the refusal names the changed reading
        two = self.two_answered()
        sender, why = dispatches._acting_author()
        self.assertIsNone(why)
        planned = RD.plan("review", sender, self.READER, self.lane, self.c,
                          supersedes=two["id"], beacon=lambda _seat: True)
        self.assertEqual(planned["row"]["meld_door"]["trigger"], "T2")
        original = dispatches.chain_rounds

        def changed(*args, **kwargs):
            info, err = original(*args, **kwargs)
            if info:
                info = dict(info, prescription="FINISH")
            return info, err

        append = dispatches._append_dispatch

        def after_preflight(*args, **kwargs):
            with mock.patch.object(dispatches, "chain_rounds",
                                   side_effect=changed):
                return append(*args, **kwargs)

        before = len(dispatches.snapshot()[0])
        with mock.patch.object(dispatches, "_append_dispatch",
                               side_effect=after_preflight):
            rc, _out, refusal, row = self.send(two, self.c)
        self.assertEqual(rc, 1)
        self.assertIsNone(row)
        self.assertIn("review chain changed", refusal)
        self.assertEqual(len(dispatches.snapshot()[0]), before)

    def test_a_door_that_fails_open_still_sends_round_three(self):
        two = self.two_answered()
        with mock.patch.object(RD, "plan", side_effect=RuntimeError("unreadable")):
            rc, out, err, three = self.send(two, self.c)
        self.assertEqual(rc, 0, err)
        self.assertIsNotNone(three)
        self.assertIn("review door could not read", out)
        self.assertIn(RD.round_whisper(3), dispatches.brief_of(three)[0])

    def test_round_three_fan_out_does_not_repeat_whisper(self):
        two = self.two_answered()
        rc, _out, err, three = self.send(two, self.c)
        self.assertEqual(rc, 0, err)
        self.assertEqual(three["round_whisper"], RD.round_whisper(3))
        rc, _out, err, fanout = self.send(two, self.c, "--force",
                                          body="independent reading at same tip")
        self.assertEqual(rc, 0, err)
        self.assertNotEqual(fanout["id"], three["id"])
        self.assertNotIn("round_whisper", fanout)
        self.assertNotIn("xfam-reviewer-fixes-its-own-findings",
                         dispatches.brief_of(fanout)[0])

    def test_generated_guidance_does_not_spend_the_sender_argv_cap(self):
        two = self.two_answered()
        body = "x" * (dispatches.MESSAGE_ARG_CAP - 100)
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="codex"):
            rc, _out, err, three = self.send(two, self.c, body=body)
        self.assertEqual(rc, 0, err)
        self.assertGreater(len(dispatches.brief_of(three)[0]),
                           dispatches.MESSAGE_ARG_CAP)
        self.assertEqual(dispatches.brief_of(three)[0].count(body), 1)

    def test_meld_diff_bar_does_not_order_a_reviewer_patch(self):
        two = self.two_answered()
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="codex"), \
                mock.patch.object(burnflags, "family_flag",
                                  return_value={"colour": "GREEN"}), \
                mock.patch.object(dispatches, "_review_mode_choice",
                                  return_value="MELD-DIFF"):
            rc, _out, err, three = self.send(two, self.c)
        self.assertEqual(rc, 0, err)
        brief = dispatches.brief_of(three)[0]
        self.assertIn("REVIEW FIX MODE: MELD-DIFF", brief)
        self.assertIn("MELD BAR", brief)
        self.assertNotIn("--patch-tip", brief)

    def test_a_non_codex_bar_keeps_the_default_fix_procedure(self):
        two = self.two_answered()
        rc, _out, err, three = self.send(two, self.c)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("review_mode", three)
        self.assertIn("Otherwise the reviewer fixes findings in this pass",
                      dispatches.brief_of(three)[0])

    def test_codex_review_mode_is_stable_per_chain_alternates_by_chain_and_is_recorded(self):  # noqa: VACUOUS_ASSERTION — unconditional mode and non-Codex controls surround the per-chain loop
        """PATCH/MELD-DIFF is chain-deterministic; only Codex review rows carry it."""
        modes = []
        for lane, tip in (("mode-one", self.a), ("mode-two", self.b)):
            self.lane = lane
            delivered = []
            with mock.patch.object(dispatches, "_verified_family",
                                   return_value="codex"), \
                    mock.patch.object(burnflags, "family_flag",
                                      return_value={"colour": "GREEN"}), \
                    mock.patch.object(
                        seats, "dm",
                        side_effect=lambda _to, text, **_kw:
                        (delivered.append(text) or {"id": "dm-mode"}, None)):
                rc, out, err, root = self.send(None, tip)
            self.assertEqual(rc, 0, err)
            self.assertEqual(len(delivered), 1)
            mode = root.get("review_mode")
            self.assertIn(mode, ("PATCH", "MELD-DIFF"))
            self.assertIn(mode, out)
            self.assertIn(mode, dispatches.brief_of(root)[0])
            self.assertIn(mode, delivered[0])
            modes.append(mode)
            if len(modes) == 1:
                current = dispatches.snapshot()[0]
                self.assertEqual(
                    dispatches._review_mode_choice(
                        current, self.READER, "another-repository", "next-chain"),
                    "MELD-DIFF")
                self.assertEqual(
                    dispatches._review_mode_choice(
                        current, "another-codex-reader", "another-repository",
                        "next-chain"), "PATCH")
                self.assertEqual(
                    dispatches._review_mode_choice(
                        current, "another-codex-reader", root["repo_id"],
                        root["chain_root"]), mode)
                manual = {"id": "f" * 32, "chain_root": "f" * 32,
                          "kind": "review", "recipient": self.READER,
                          "repo_id": "prior-project",
                          "message_body": (
                              "FIX MODE FOR THIS CHAIN (task/3698 A/B, chain 1, "
                              "set by hand until 3670 lands): PATCH. Cure it.")}
                self.assertEqual(dispatches._review_mode_choice(
                    {manual["id"]: manual}, self.READER, self.repo,
                    "second-chain"), "MELD-DIFF")
            with mock.patch.object(dispatches, "_verified_family",
                                   return_value="codex"), \
                    mock.patch.object(burnflags, "family_flag",
                                      return_value={"colour": "GREEN"}):
                rc, retry_out, err, retry = self.send(root, tip)
            self.assertEqual(rc, 0, err)
            self.assertEqual(retry["review_mode"], mode)
            self.assertIn(mode, retry_out)
        self.assertNotEqual(modes[0], modes[1])

        self.lane = "non-codex-mode"
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="claude"):
            rc, out, err, row = self.send(None, self.c)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("review_mode", row)
        self.assertNotIn("MELD-DIFF", out)

    def test_orange_family_review_row_is_meld_diff_and_records_its_cause(self):
        two = self.two_answered()
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="codex"), \
                mock.patch.object(burnflags, "family_flag",
                                  return_value={"colour": "ORANGE"}):
            rc, out, err, three = self.send(two, self.c)
        self.assertEqual(rc, 0, err)
        self.assertEqual(three["review_mode"], "MELD-DIFF")
        self.assertEqual(three["review_mode_cause"],
                         "burn flags ORANGE for this family")
        self.assertIn("REVIEW FIX MODE: MELD-DIFF", out)

    def test_red_family_review_row_is_review_only_and_records_its_cause(self):
        two = self.two_answered()
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="codex"), \
                mock.patch.object(burnflags, "family_flag",
                                  return_value={"colour": "RED"}):
            rc, out, err, three = self.send(two, self.c)
        self.assertEqual(rc, 0, err)
        self.assertEqual(three["review_mode"], "RED")
        self.assertEqual(three["review_mode_cause"],
                         "burn flags RED for this family")
        self.assertIn("REVIEW FIX MODE: REVIEW ONLY", out)
        self.assertNotIn("--patch-tip", out)
        # The READER's brief carries the line too, so the reader is told it
        # reviews only, and the brief's suffix binds the recorded mode.
        self.assertIn("REVIEW FIX MODE: REVIEW ONLY",
                      dispatches.brief_of(three)[0])
        from helm import compose_contract
        _contract, why = compose_contract.parse(three, {three["id"]: three})
        self.assertNotIn("generated review suffix does not bind", str(why))

    def test_stale_family_flag_falls_to_meld_diff_not_patch(self):
        two = self.two_answered()
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="codex"), \
                mock.patch.object(burnflags, "family_flag",
                                  return_value=None):
            rc, out, err, three = self.send(two, self.c)
        self.assertEqual(rc, 0, err)
        self.assertEqual(three["review_mode"], "MELD-DIFF")
        self.assertEqual(three["review_mode_cause"],
                         "a fresh burn flag is absent for this family")
        self.assertNotIn("--patch-tip", out)

    def test_green_family_review_row_keeps_the_alternation(self):  # noqa: VACUOUS_ASSERTION — the recorded mode is asserted to be one of the two A/B arms
        two = self.two_answered()
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="codex"), \
                mock.patch.object(burnflags, "family_flag",
                                  return_value={"colour": "GREEN"}):
            rc, out, err, three = self.send(two, self.c)
        self.assertEqual(rc, 0, err)
        self.assertIn(three["review_mode"], ("PATCH", "MELD-DIFF"))
        self.assertNotIn("review_mode_cause", three)

    def test_stated_meld_diff_mode_wins_over_alternation_default(self):  # noqa: VACUOUS_ASSERTION — positive controls on MELD-DIFF review_mode and absence of PATCH
        """task/4051: a stated MELD-DIFF in the brief or --review-mode flag stores
        MELD-DIFF even when the alternation default would pick PATCH."""
        self.lane = "stated-meld-diff"
        # Control: fresh chain with no stated mode defaults to PATCH on GREEN
        with mock.patch.object(dispatches, "_verified_family", return_value="codex"), \
                mock.patch.object(burnflags, "family_flag", return_value={"colour": "GREEN"}):
            rc, out, err, row = self.send(None, self.a, body="normal brief")
        self.assertEqual(rc, 0, err)
        self.assertEqual(row.get("review_mode"), "PATCH")

        # 1. Stated in brief prose via 'REVIEW FIX MODE: MELD-DIFF'
        self.lane = "stated-in-brief"
        body = "Author instructions.\n\nREVIEW FIX MODE: MELD-DIFF\nProceed with diff."
        with mock.patch.object(dispatches, "_verified_family", return_value="codex"), \
                mock.patch.object(burnflags, "family_flag", return_value={"colour": "GREEN"}):
            rc, out, err, stated_row = self.send(None, self.b, body=body)
        self.assertEqual(rc, 0, err)
        self.assertEqual(stated_row.get("review_mode"), "MELD-DIFF")
        brief = dispatches.brief_of(stated_row)[0]
        self.assertIn("REVIEW FIX MODE: MELD-DIFF", brief)
        self.assertNotIn("REVIEW FIX MODE: PATCH", brief)

        # 2. Stated via CLI flag --review-mode MELD-DIFF
        self.lane = "stated-via-flag"
        with mock.patch.object(dispatches, "_verified_family", return_value="codex"), \
                mock.patch.object(burnflags, "family_flag", return_value={"colour": "GREEN"}):
            rc, out, err, flag_row = self.send(None, self.c, "--review-mode", "MELD-DIFF", body="flag brief")
        self.assertEqual(rc, 0, err)
        self.assertEqual(flag_row.get("review_mode"), "MELD-DIFF")
        brief_flag = dispatches.brief_of(flag_row)[0]
        self.assertIn("REVIEW FIX MODE: MELD-DIFF", brief_flag)
        self.assertNotIn("REVIEW FIX MODE: PATCH", brief_flag)

    def test_conflicting_stated_review_modes_are_refused(self):  # noqa: VACUOUS_ASSERTION — positive control on non-zero exit and error text naming both modes
        """task/4051: conflicting stated review modes are refused, naming both."""
        self.lane = "conflicting-modes"
        # 1. Conflicting lines in brief
        conflict_body = "Instructions:\nREVIEW FIX MODE: PATCH\nREVIEW FIX MODE: MELD-DIFF"
        with mock.patch.object(dispatches, "_verified_family", return_value="codex"), \
                mock.patch.object(burnflags, "family_flag", return_value={"colour": "GREEN"}):
            rc, out, err, _row = self.send(None, self.a, body=conflict_body)
        self.assertNotEqual(rc, 0)
        self.assertIn("MELD-DIFF", out + err)
        self.assertIn("PATCH", out + err)

        # 2. Conflicting flag and brief
        with mock.patch.object(dispatches, "_verified_family", return_value="codex"), \
                mock.patch.object(burnflags, "family_flag", return_value={"colour": "GREEN"}):
            rc, out, err, _row = self.send(None, self.a, "--review-mode", "PATCH",
                                           body="REVIEW FIX MODE: MELD-DIFF")
        self.assertNotEqual(rc, 0)
        self.assertIn("MELD-DIFF", out + err)
        self.assertIn("PATCH", out + err)

    def test_burn_forced_mode_wins_over_stated_mode(self):  # noqa: VACUOUS_ASSERTION — positive controls on forced review_mode and cause override
        """task/4051: a burn-forced mode (RED/ORANGE) still wins over a stated mode."""
        self.lane = "burn-forced-orange"
        body = "REVIEW FIX MODE: PATCH — please commit cure"
        # On ORANGE, stated PATCH is overridden by burn-forced MELD-DIFF
        with mock.patch.object(dispatches, "_verified_family", return_value="codex"), \
                mock.patch.object(burnflags, "family_flag", return_value={"colour": "ORANGE"}):
            rc, out, err, row_orange = self.send(None, self.a, body=body)
        self.assertEqual(rc, 0, err)
        self.assertEqual(row_orange.get("review_mode"), "MELD-DIFF")
        self.assertIn("burn flags ORANGE", row_orange.get("review_mode_cause", ""))

        # On RED, stated PATCH is overridden by burn-forced RED (review only)
        self.lane = "burn-forced-red"
        with mock.patch.object(dispatches, "_verified_family", return_value="codex"), \
                mock.patch.object(burnflags, "family_flag", return_value={"colour": "RED"}):
            rc, out, err, row_red = self.send(None, self.b, body=body)
        self.assertEqual(rc, 0, err)
        self.assertEqual(row_red.get("review_mode"), "RED")
        self.assertIn("burn flags RED", row_red.get("review_mode_cause", ""))

    def test_diff_handoff_passes_on_stated_meld_diff_row(self):  # noqa: VACUOUS_ASSERTION — positive control on rc 0 verdict and diff_handoff receipt
        """task/4051: a row with stated MELD-DIFF accepts a --diff-handoff verdict."""
        self.lane = "handoff-pass"
        body = "Please review this.\n\nREVIEW FIX MODE: MELD-DIFF"
        with mock.patch.object(dispatches, "_verified_family", return_value="codex"), \
                mock.patch.object(burnflags, "family_flag", return_value={"colour": "GREEN"}):
            rc, out, err, row = self.send(None, self.a, body=body)
        self.assertEqual(rc, 0, err)
        self.assertEqual(row["review_mode"], "MELD-DIFF")

        # Now post a diff in the pair meld room and file a verdict with diff_handoff
        room = self.pair_room(row)
        epoch, _who, _seed = meld.latest_seed(chat.read(room)[0])
        patch = self.git("diff", self.a, self.b, "--", "state") + "\n"
        message = chat.post("[MELD e:%d] %s" % (epoch, patch), room=room,
                            who=self.READER, sign=False)
        self.fix(row, path="state",
                 no_patch_because="author applies exact reviewer diff",
                 diff_handoff=room + "/" + message["id"])
        folded = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(folded["status"], "verdict")
        self.assertTrue(dispatches._has_diff_handoff(folded))
        self.assertEqual(folded["diff_handoff"]["msg_id"], message["id"])

    def test_an_unknown_or_refusing_flag_never_stamps_patch(self):  # noqa: VACUOUS_ASSERTION — the loop is over a fixed three-flag table, and each pass asserts the MELD-DIFF mode and its cause
        """GREY is unmeasured and a refusing reach axis cannot spend: both are
        UNKNOWN for the fix mode, so both are MELD-DIFF, never PATCH, even where
        the alternation would have chosen PATCH."""
        for tip, flag, cause in (
                (self.a, {"colour": "GREY"}, "burn flags GREY for this family"),
                (self.b, {"colour": "GREEN", "axes": {"reach": "ORANGE"}},
                 "burn flags reach ORANGE for this family"),
                (self.c, {"colour": "YELLOW", "axes": {"reach": "RED"}},
                 "burn flags reach RED for this family")):
            self.lane = "unknown-flag-" + tip[:6]
            with mock.patch.object(dispatches, "_verified_family",
                                   return_value="codex"), \
                    mock.patch.object(burnflags, "family_flag",
                                      return_value=flag), \
                    mock.patch.object(dispatches, "_review_mode_choice",
                                      return_value="PATCH"):
                rc, out, err, row = self.send(None, tip)
            self.assertEqual(rc, 0, err)
            self.assertEqual((row["review_mode"], row["review_mode_cause"]),
                             ("MELD-DIFF", cause), flag)
            self.assertIn("REVIEW FIX MODE: MELD-DIFF", out, flag)
            self.assertNotIn("--patch-tip", dispatches.brief_of(row)[0], flag)

    def test_an_orange_row_is_meld_diff_to_every_meld_diff_door(self):
        """A burn-forced MELD-DIFF row is MELD-DIFF to the doors that read the
        recorded mode: its reader can record the --diff-handoff receipt it was
        told to produce. Only the task/3698 A/B count ignores it."""
        self.lane = "orange-receipt"
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="codex"), \
                mock.patch.object(burnflags, "family_flag",
                                  return_value={"colour": "ORANGE"}):
            rc, _out, err, root = self.send(None, self.a)
        self.assertEqual(rc, 0, err)
        self.assertEqual(dispatches._review_mode_of(root), "MELD-DIFF")
        self.assertIsNone(dispatches._ab_mode_of(root))
        room = self.pair_room(root)
        epoch, _who, _seed = meld.latest_seed(chat.read(room)[0])
        patch = self.git("diff", self.a, self.b, "--", "state") + "\n"
        message = chat.post("[MELD e:%d] %s" % (epoch, patch), room=room,
                            who=self.READER, sign=False)
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(time.time() - 2))
        with mock.patch.object(dispatches.pk, "now_ts", return_value=old):
            self.fix(root, no_patch_because="exact diff posted in the pair "
                     "room", diff_handoff=room + "/" + message["id"])
        folded = dispatches.snapshot()[0][root["id"]]
        self.assertEqual(folded["status"], "verdict")
        self.assertTrue(dispatches._has_diff_handoff(folded))
        self.assertEqual(folded["diff_handoff"]["msg_id"], message["id"])

    def test_an_orange_add_moved_by_rebind_carries_one_mode_line(self):
        two = self.two_answered()
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="codex"), \
                mock.patch.object(burnflags, "family_flag",
                                  return_value={"colour": "ORANGE"}):
            authored, why = dispatches.add(
                self.READER, self.lane, ref=self.c, repo=self.repo,
                kind="review", supersedes=two["id"], notify=False,
                _reason=True)
            self.assertIsNone(why, why)
            self.assertEqual(authored["review_mode_cause"],
                             "burn flags ORANGE for this family")
            moved, why = dispatches.rebind(
                authored["id"], "seat-c", reason="route to live seat",
                force=True, notify=False)
        self.assertIsNone(why, why)
        brief = dispatches.brief_of(moved["new"])[0]
        self.assertEqual(brief.count("REVIEW FIX MODE:"), 1, brief)
        self.assertEqual(moved["new"]["review_mode"], "MELD-DIFF")

    def test_a_burn_forced_row_does_not_move_the_alternation(self):
        """The task/3698 parity counts only rows the alternation chose: a
        forced row on another chain leaves this reader's next chain where it
        was, and a forced row inside a chain never fixes that chain's mode."""
        forced = {"id": "f" * 32, "chain_root": "f" * 32, "kind": "review",
                  "recipient": self.READER, "repo_id": "prior-project",
                  "review_mode": "MELD-DIFF",
                  "review_mode_cause": "burn flags ORANGE for this family"}
        self.assertEqual(dispatches._review_mode_of(forced), "MELD-DIFF")
        current = {forced["id"]: forced}
        self.assertEqual(dispatches._review_mode_choice(
            current, self.READER, self.repo, "next-chain"), "PATCH")
        self.assertEqual(dispatches._review_mode_choice(
            current, self.READER, "prior-project", forced["id"]), "PATCH")
        chosen = dict(forced, id="e" * 32, chain_root="e" * 32)
        chosen.pop("review_mode_cause")
        current[chosen["id"]] = chosen
        self.assertEqual(dispatches._review_mode_choice(
            current, self.READER, self.repo, "next-chain"), "MELD-DIFF")


    def test_add_and_rebind_record_guidance_from_the_ledger_and_keep_the_whole_brief(self):
        two = self.two_answered()
        original = "the review note: " + ("details " * 80).strip()
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="codex"), \
                mock.patch.object(burnflags, "family_flag",
                                  return_value={"colour": "GREEN"}):
            authored, why = dispatches.add(
                self.READER, self.lane, ref=self.c, note=original,
                repo=self.repo, kind="review", supersedes=two["id"],
                notify=False, _reason=True)
        self.assertIsNone(why, why)
        self.assertEqual(authored["review_mode"], "PATCH")
        self.assertEqual(authored["round_whisper"], RD.round_whisper(3))
        self.assertEqual(dispatches.brief_of(authored)[0],
                         RD.round_whisper(3) + "\n\n" +
                         dispatches.REVIEW_MODE_LINES["PATCH"])
        self.assertEqual(authored["note"], original)
        # An authored add has no brief: its note remains separate, and the
        # guidance is still a real, content-addressed brief on the ledger.
        self.assertIsNone(authored["message_hash"])

        with mock.patch.object(dispatches, "_verified_family",
                               return_value="codex"), \
                mock.patch.object(burnflags, "family_flag",
                                  return_value={"colour": "GREEN"}):
            moved, why = dispatches.rebind(
                authored["id"], "seat-c", reason="route to live seat",
                force=True, notify=False)
        self.assertIsNone(why, why)
        child = moved["new"]
        self.assertEqual(child["review_mode"], "PATCH")
        self.assertNotIn("round_whisper", child)
        self.assertEqual(dispatches.brief_of(child)[0].count("REVIEW FIX MODE:"), 1)
        self.assertEqual(dispatches.brief_of(child)[0].count("xfam-reviewer-fixes"), 0)

    def test_quoted_mode_in_author_prose_still_gets_exact_generated_suffix(self):
        self.lane = "quote-mode"
        quoted = ("Quote from the design discussion: " +
                  dispatches.REVIEW_MODE_LINES["PATCH"] +
                  "\nThe actual reviewer instructions follow.")
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="codex"), \
                mock.patch.object(burnflags, "family_flag",
                                  return_value={"colour": "GREEN"}):
            rc, _out, err, row = self.send(None, self.a, body=quoted)
        self.assertEqual(rc, 0, err)
        full = dispatches.brief_of(row)[0]
        self.assertTrue(full.startswith(quoted))
        self.assertTrue(full.endswith(dispatches.REVIEW_MODE_LINES["PATCH"]))
        self.assertEqual(full.count(dispatches.REVIEW_MODE_LINES["PATCH"]), 2)
        self.assertEqual(len(os.listdir(dispatches.brief_dir())), 1)

    def test_rebind_preserves_an_original_send_brief_and_changes_only_guidance(self):
        two = self.two_answered()
        original = "author's exact brief: " + "context " * 900
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="codex"), \
                mock.patch.object(burnflags, "family_flag",
                                  return_value={"colour": "GREEN"}), \
                mock.patch.object(dispatches, "_review_mode_choice",
                                  return_value="MELD-DIFF"):
            rc, _out, err, sent = self.send(two, self.c, body=original)
        self.assertEqual(rc, 0, err)
        initial_hash = sent["message_hash"]
        self.assertIn(original, dispatches.brief_of(sent)[0])
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="claude"):
            moved, why = dispatches.rebind(
                sent["id"], "seat-c", reason="route to live seat",
                force=True, notify=False)
        self.assertIsNone(why, why)
        child = moved["new"]
        self.assertNotIn("review_mode", child)
        whole = dispatches.brief_of(child)[0]
        self.assertTrue(whole.startswith(original + "\n\n"))
        self.assertNotIn("REVIEW FIX MODE:", whole)
        self.assertEqual(whole.count("xfam-reviewer-fixes"), 0)
        self.assertEqual(sent["message_hash"], initial_hash)
        self.assertEqual(child["message_hash"], initial_hash)

    def test_codex_guidance_only_rebind_to_claude_has_no_stale_brief(self):
        self.lane = "guidance-only"
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="codex"), \
                mock.patch.object(burnflags, "family_flag",
                                  return_value={"colour": "GREEN"}):
            authored, why = dispatches.add(
                self.READER, self.lane, ref=self.a, repo=self.repo,
                kind="review", new_work=True, notify=False, _reason=True,
                task=self.review_task["id"])
        self.assertIsNone(why, why)
        self.assertEqual(dispatches.brief_of(authored)[0],
                         dispatches.REVIEW_MODE_LINES["PATCH"])
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="claude"):
            moved, why = dispatches.rebind(
                authored["id"], "seat-c", reason="new reader",
                force=True, notify=False)
        self.assertIsNone(why, why)
        child = dispatches.snapshot()[0][moved["new"]["id"]]
        self.assertNotIn("review_mode", child)
        self.assertNotIn("brief_ref", child)
        self.assertNotIn("brief_bytes", child)
        self.assertEqual(child["message_body"], None)
        self.assertEqual(dispatches.brief_of(child)[1], dispatches.BODY_NONE)

    def test_legacy_hashed_review_without_a_brief_refuses_rebind(self):
        self.lane = "legacy-hashed-review"
        parent = self.add(ref=self.a, recipient=self.READER, kind="review",
                          lane=self.lane)
        self.forge_open(parent["id"], message_hash="a" * 32,
                        message_body=None)
        before = len(dispatches.snapshot()[0])
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="claude"):
            moved, why = dispatches.rebind(
                parent["id"], "seat-c", reason="new reader",
                force=True, notify=False)
        self.assertIsNone(moved)
        self.assertIn("authored hash but its text is UNKNOWN", why)
        self.assertEqual(len(dispatches.snapshot()[0]), before)
        self.assertEqual(dispatches.snapshot()[0][parent["id"]]["status"], "open")

    # -- T1 ---------------------------------------------------------------

    def test_T1_a_design_finding_at_round_two_prints_the_meld_and_writes_the_row(self):  # noqa: VACUOUS_ASSERTION — DoorMutationTest plants the violation this absence rules out and the arm fails; T2 positive control: test_T2_opens_the_meld_when_the_reader_is_live_and_idle
        one = self.first(self.a)
        self.fix(one)
        rc, out, err, row = self.send(one, self.b)
        self.assertEqual(rc, 0, err)
        self.assertIn("MELD NUDGE (T1)", out)
        self.assertIn("DESIGN: no cure committed: a design finding", out)
        room = self.pair_room(row)
        self.assertIn("this send opens them as the next round of the task's "
                      "pair meld:\n    helm chat meld recv %s" % room, out)
        self.assertIn("%s: settle the design" % self.lane,
                      self.latest_seed(room))
        self.assertEqual(row["meld_door"]["trigger"], "T1")
        self.assertEqual(self.rooms(), [], "T1 nudges, it never opens a meld")

    def test_T1_mode_diff_no_patch_without_diff_receipt_is_not_mechanical(self):
        self.lane = "mode-diff-no-patch"
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="codex"), \
                mock.patch.object(dispatches, "_review_mode_choice",
                                  return_value="MELD-DIFF"):
            rc, _out, err, root = self.send(None, self.a)
            self.assertEqual(rc, 0, err)
            self.assertEqual(root["review_mode"], "MELD-DIFF")
            self.fix(root, no_patch_because="author applies mechanical diff")
            rc, out, err, next_row = self.send(root, self.b)
            self.assertEqual(rc, 0, err)
            self.assertIn("MELD NUDGE (T1)", out)
            self.assertIn("no cure committed: author applies mechanical diff", out)
            self.assertEqual(next_row["meld_door"]["trigger"], "T1")
            _cancelled, why = dispatches.mark_cancel(next_row["id"], "retip")
            self.assertIsNone(why, why)
            rc, out, err, disputed = self.send(root, self.c,
                                               "--disputes", "F1 is false")
            self.assertEqual(rc, 0, err)
            self.assertIn("MELD NUDGE (T1)", out)
            self.assertEqual(disputed["meld_door"]["disputed"], 1)

    def test_T1_validated_mode_diff_handoff_exempts_confirmation(self):
        self.lane = "mode-diff-receipt"
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="codex"), \
                mock.patch.object(burnflags, "family_flag",
                                  return_value={"colour": "GREEN"}), \
                mock.patch.object(dispatches, "_review_mode_choice",
                                  return_value="MELD-DIFF"):
            rc, _out, err, root = self.send(None, self.a)
        self.assertEqual(rc, 0, err)
        room = self.pair_room(root)
        epoch, _who, _seed = meld.latest_seed(chat.read(room)[0])
        patch = self.git("diff", self.a, self.b, "--", "state") + "\n"
        message = chat.post("[MELD e:%d] %s" % (epoch, patch), room=room,
                            who=self.READER, sign=False)
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(time.time() - 2))
        with mock.patch.object(dispatches.pk, "now_ts", return_value=old):
            self.fix(root, no_patch_because="exact diff posted in the pair room",
                     diff_handoff=room + "/" + message["id"])
        rc, out, err, child = self.send(root, self.b)
        self.assertEqual(rc, 0, err)
        self.assertTrue(child.get("diff_application"))
        self.assertNotIn("MELD NUDGE (T1)", out)
        self.assertEqual(child["meld_door"]["action"], "exempt")
        self.assertIn("adopted mechanical cure tip", child["meld_door"]["why"])

    def test_T1_receipted_diff_does_not_exempt_unrelated_child(self):
        self.lane = "mode-diff-unrelated"
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="codex"), \
                mock.patch.object(burnflags, "family_flag",
                                  return_value={"colour": "GREEN"}), \
                mock.patch.object(dispatches, "_review_mode_choice",
                                  return_value="MELD-DIFF"):
            rc, _out, err, root = self.send(None, self.a)
        self.assertEqual(rc, 0, err)
        room = self.pair_room(root)
        epoch, _who, _seed = meld.latest_seed(chat.read(room)[0])
        patch = self.git("diff", self.a, self.b, "--", "state") + "\n"
        message = chat.post("[MELD e:%d] %s" % (epoch, patch), room=room,
                            who=self.READER, sign=False)
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(time.time() - 2))
        with mock.patch.object(dispatches.pk, "now_ts", return_value=old):
            self.fix(root, no_patch_because="exact diff posted in the pair room",
                     diff_handoff=room + "/" + message["id"])
        rc, out, err, child = self.send(root, self.side)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("diff_application", child)
        self.assertNotEqual(child.get("meld_door", {}).get("action"), "exempt")
        self.assertNotIn("MELD NUDGE (T1)", out)
        info, why = dispatches.chain_rounds("integrator", root["id"], self.side)
        self.assertIsNone(why)
        self.assertEqual(info["rounds_after"], 2)

    def test_T1_an_all_mechanical_patch_fix_never_nudges(self):  # noqa: VACUOUS_ASSERTION — DoorMutationTest plants the violation this absence rules out and the arm fails; T2 positive control: test_T2_opens_the_meld_when_the_reader_is_live_and_idle
        """Falsifier (b)'s arm: T1 firing here means its scope is wrong."""
        one = self.first(self.a)
        self.fix(one, patch_tip=self.c)
        # THE NEXT ROUND CARRIES THE CURE (task/3288): a ref without it is
        # refused at the door before T1 could be asked.
        rc, out, err, row = self.send(one, self.branch("next", self.c))
        self.assertEqual(rc, 0, err)
        self.assertNotIn("MELD NUDGE", out)
        self.assertNotIn("meld_door", row)

    def test_T1_MIXED_offers_the_meld_for_the_design_finding_only(self):
        """Five mechanical cures and one design finding: adopting the patch
        is neither a round nor held back, and the meld is for the design
        finding alone."""
        one = self.first(self.a)
        self.fix(one, patch_tip=self.c,
                 design_findings=["the fold should own the count"])
        rc, out, err, row = self.send(one, self.c)
        self.assertEqual(rc, 0, err)
        self.assertIn("MELD NUDGE (T1)", out)
        self.assertIn("DESIGN: the fold should own the count", out)
        self.assertIn("patch %s is adopted in this tip" % self.c[:12], out)
        self.assertNotIn("DISPUTED", out)
        self.assertEqual(row["meld_door"]["design"], 1)
        self.assertTrue(row["meld_door"]["patch"])

    def test_T1_a_disputed_finding_nudges(self):
        one = self.first(self.a)
        self.fix(one, patch_tip=self.c)
        rc, out, err, _row = self.send(one, self.branch("next", self.c),
                                       "--disputes",
                                       "F2 misreads the cap; F3 is moot")
        self.assertEqual(rc, 0, err)
        self.assertIn("DISPUTED: F2 misreads the cap", out)
        self.assertIn("DISPUTED: F3 is moot", out)

    # -- T2 ---------------------------------------------------------------

    def test_T2_opens_the_meld_when_the_reader_is_live_and_idle(self):  # noqa: VACUOUS_ASSERTION — the empty other-room list is falsifier (b) of the pair meld; the pair room's newest seed is asserted to carry the bar and the chain beside it
        two = self.two_answered()
        self.beacon = True
        rc, out, err, row = self.send(two, self.c)
        self.assertEqual(rc, 0, err)
        self.assertIn("MELD OPENED", out)
        self.assertEqual(row["meld_door"]["action"], "auto-open")
        # P2: the bar is a ROUND of the chain's pair meld, never a room of
        # its own
        self.assertEqual(self.rooms(), [])
        room = self.pair_room(row)
        self.assertIn("MELD OPENED %s" % room, out)
        seed = self.latest_seed(room)
        self.assertIn("%s: agree the bar" % self.lane, seed)
        self.assertIn("(chain %s)" % two["chain_root"][:12], seed)
        # the row is still written, and it carries the BAR as the fallback
        self.assertIn("MELD BAR", dispatches.brief_of(row)[0] or "")

    def test_T2_briefs_the_bar_when_the_reader_has_no_live_beacon(self):  # noqa: VACUOUS_ASSERTION — DoorMutationTest plants the violation this absence rules out and the arm fails; T2 positive control: test_T2_opens_the_meld_when_the_reader_is_live_and_idle
        two = self.two_answered()
        self.beacon = False
        rc, out, err, row = self.send(two, self.c)
        self.assertEqual(rc, 0, err)
        self.assertIn("no live meld (seat-b has no live beacon)", out)
        self.assertEqual(row["meld_door"]["action"], "brief")
        self.assertEqual(self.rooms(), [])

    def test_T2_briefs_the_bar_when_the_reader_has_a_row_in_flight(self):  # noqa: VACUOUS_ASSERTION — DoorMutationTest plants the violation this absence rules out and the arm fails; T2 positive control: test_T2_opens_the_meld_when_the_reader_is_live_and_idle
        two = self.two_answered()
        self.add(ref=self.side, recipient=self.READER, kind="review",
                 lane="elsewhere")
        self.beacon = True
        rc, out, err, row = self.send(two, self.c)
        self.assertEqual(rc, 0, err)
        self.assertIn("1 open row(s) in flight", out)
        self.assertEqual(row["meld_door"]["action"], "brief")
        self.assertEqual(self.rooms(), [])

    def test_T2_async_because_skips_it_and_is_recorded(self):  # noqa: VACUOUS_ASSERTION — DoorMutationTest plants the violation this absence rules out and the arm fails; T2 positive control: test_T2_opens_the_meld_when_the_reader_is_live_and_idle
        two = self.two_answered()
        self.beacon = True
        rc, out, err, row = self.send(two, self.c, "--async-because",
                                      "reader-is-mid-gate")
        self.assertEqual(rc, 0, err)
        self.assertEqual(row["async_because"], "reader-is-mid-gate")
        self.assertEqual(row["meld_door"]["action"], "async")
        self.assertEqual(self.rooms(), [])

    def test_T2_exempts_the_reviewers_adopted_patch_tip(self):  # noqa: VACUOUS_ASSERTION — DoorMutationTest plants the violation this absence rules out and the arm fails; T2 positive control: test_T2_opens_the_meld_when_the_reader_is_live_and_idle
        one = self.first(self.a)
        self.fix(one)
        rc, _out, err, two = self.send(one, self.b)
        self.assertEqual(rc, 0, err)
        self.fix(two, path="helm/b.py", patch_tip=self.c)
        self.beacon = True
        rc, out, err, row = self.send(two, self.c)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("MELD OPENED", out)
        self.assertEqual(row["meld_door"]["action"], "exempt")
        self.assertEqual(self.rooms(), [])

    def test_T2_exempts_a_chain_whose_bar_a_meld_already_AGREED(self):
        """The round that carries out an agreed bar is the cure, not round
        four: the door opens no second meld over it."""
        two = self.two_answered()
        room = self.agreed_meld(two["chain_root"], self.b)
        self.beacon = True
        rc, out, err, row = self.send(two, self.c)
        self.assertEqual(rc, 0, err)
        self.assertEqual(row["meld_door"]["action"], "exempt")
        self.assertIn("bar was AGREED in %s" % room, out)
        self.assertNotIn("MELD OPENED", out)

    def test_T2_exempts_a_tip_already_on_the_chain(self):  # noqa: VACUOUS_ASSERTION — DoorMutationTest plants the violation this absence rules out and the arm fails; T2 positive control: test_T2_opens_the_meld_when_the_reader_is_live_and_idle
        """One tip sent to several readers is one round."""
        two = self.two_answered()
        self.beacon = True
        rc, _out, err, row = self.send(two, self.b)
        self.assertEqual(rc, 0, err)
        self.assertEqual(row["meld_door"]["action"], "exempt")
        self.assertEqual(self.rooms(), [])

    # -- T0 ---------------------------------------------------------------

    def test_T0_an_irreversible_first_build_row_needs_a_design_meld(self):
        self.lane = "prod-task-3112"
        body = "run the migration against the prod database tonight"
        rc, _out, err, row = self.send(None, self.b, kind="build", body=body)
        self.assertEqual(rc, 2)
        self.assertIn("T0", err)
        self.assertIn("run the migration", err)
        self.assertIn("helm chat meld invite seat-b", err)
        self.assertIn(RD.PAIR_PLAN, err)
        self.assertIn("--into %s-3112" % RD.pair_scope(
            {"repo_id": dispatches._repo_info(self.repo)["repo_id"]}), err)
        self.assertIsNone(row, "the build row was written before the meld")
        rc, _out, err, row = self.send(None, self.b, "--async-because",
                                       "staging-only", kind="build",
                                       body=body)
        self.assertEqual(rc, 0, err)
        self.assertEqual(row["meld_door"]["action"], "async")

    def test_T0_binds_the_cited_meld_to_the_reader_and_the_tip(self):
        """FINDING 6. A cited design meld must be between this row's author
        and reader, about this lane, and AGREED on this row's tip — exactly
        the binding a verdict's --meld gets."""
        self.lane = "prod-lane-bound"
        body = "run the migration against the prod database tonight"

        def design_meld(peer, tip, epoch):
            room = "meld-%d-design" % epoch
            chat.post("[MELD e:%d] PROBLEM: %s: design meld | "
                      "convener=integrator invited=%s cap=5 "
                      "recv-timeout=90s | d [HOLD]"
                      % (epoch, self.lane, peer), room=room,
                      who="integrator", sign=False)
            for who in ("integrator", peer):
                chat.post("[MELD e:%d] %s [DONE]" % (epoch, block(tip=tip)),
                          room=room, who=who, sign=False)
            return room

        for peer, tip, epoch in (("seat-c", self.b, 1790000001),
                                 (self.READER, self.c, 1790000002)):
            with self.subTest(peer=peer):
                room = design_meld(peer, tip, epoch)
                rc, _out, err, row = self.send(None, self.b, "--meld", room,
                                               kind="build", body=body)
                self.assertEqual(rc, 2, "an unbound design meld was accepted")
                self.assertIn("is not an AGREED design meld for this row",
                              err)
                self.assertIsNone(row)
        room = design_meld(self.READER, self.b, 1790000003)
        rc, _out, err, row = self.send(None, self.b, "--meld", room,
                                       kind="build", body=body)
        self.assertEqual(rc, 0, err)
        self.assertEqual(row["meld_door"]["action"], "cited")

    def test_T2_is_not_exempted_by_a_meld_about_ANOTHER_chain(self):
        """FINDING 7, at the door. An agreed meld with the same reader about
        other work leaves this chain's round three untouched."""
        two = self.two_answered()
        self.agreed_meld("0123456789ab", self.b, lane="other-work")
        self.beacon = True
        rc, out, err, row = self.send(two, self.c)
        self.assertEqual(rc, 0, err)
        self.assertEqual(row["meld_door"]["action"], "auto-open",
                         "a meld about another chain exempted this one")
        self.assertIn("MELD OPENED", out)

    def test_the_AUTHORS_own_clean_hold_through_the_real_door_settles_nothing(self):
        """FINDING 2, through the real hold door: the author's clean claim on
        its own open row is refused there and the rung still walls the
        three-round chain; the reader's hold, recorded as the reader's, ends
        it. The fold binds the same hand for an event that never passed the
        door (SpiralBase's `test_what_counts_as_an_answer`)."""
        from helm import seats_stop_signals as signals
        two = self.two_answered()
        rc, _out, err, three = self.send(two, self.c)
        self.assertEqual(rc, 0, err)
        rc, _out, err = run(dispatches.cmd_dispatch, [
            "hold", three["id"], "looks clean to me", "--source-clean",
            self.c])
        self.assertNotEqual(rc, 0, "the author held its own row clean")
        self.assertIn("only this row's recipient", err)
        self.assertEqual(dispatches.snapshot()[0][three["id"]]["status"],
                         "open")
        info, _err = dispatches.review_spiral("integrator")
        self.assertIsNotNone(info, "the author's own clean claim silenced "
                                   "the rung")
        self.assertEqual(info["rounds"], 3)
        block_text, _warn = signals._spiral_gate("author-hold", "main",
                                                 "integrator")
        self.assertIn("review spiral", block_text or "")
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": self.READER}):
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "hold", three["id"], "read clean; fab Ran 5 tests OK", "--source-clean", self.c])
        self.assertEqual(rc, 0, err)
        self.assertEqual(dispatches.snapshot()[0][three["id"]]["hold_actor"],
                         self.READER)
        self.assertIsNone(dispatches.review_spiral("integrator")[0])

    def test_T0_ordinary_build_briefs_are_untouched(self):  # noqa: VACUOUS_ASSERTION — DoorMutationTest plants the violation this absence rules out and the arm fails; T2 positive control: test_T2_opens_the_meld_when_the_reader_is_live_and_idle
        self.lane = "plain-lane"
        rc, _out, err, row = self.send(None, self.b, kind="build",
                                       body="delete the dead helper and rename")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("meld_door", row)

    def test_irreversible_phrases_match_on_words(self):
        self.assertEqual(RD.irreversible_hits("drop  TABLE users"),
                         ["drop table"])
        self.assertEqual(RD.irreversible_hits("a backdrop table"), [])
        self.assertEqual(RD.irreversible_hits("write to prod, then purge the cache"),
                         ["write to prod", "purge the"])


class FalsifierCensusTest(DoorBase):
    """`helm dispatch melds`: the two falsifiers, measured on real door rows.
    Each reading is planted both ways, so HOLDS and FALSIFIED are both seen
    from the same census."""

    def opened(self):
        two = self.two_answered()
        self.beacon = True
        rc, out, err, row = self.send(two, self.c)
        self.assertEqual(rc, 0, err)
        self.assertEqual(row["meld_door"]["action"], "auto-open")
        return self.pair_room(row)

    def test_nothing_fired_reads_UNMEASURED_not_HOLDS(self):
        got = RD.census()
        self.assertIsNone(got["t2"]["join_rate"])
        self.assertTrue(got["t2"]["reading"].startswith("UNMEASURED"))
        self.assertTrue(got["t1"]["reading"].startswith("UNMEASURED"))

    def test_a_reader_who_joins_in_the_window_HOLDS_and_one_who_never_does_FALSIFIES(self):
        room = self.opened()
        got = RD.census()
        self.assertEqual(got["t2"]["opened_rows"], 1)
        self.assertEqual(got["t2"]["join_rate"], 0.0)
        self.assertTrue(got["t2"]["reading"].startswith("FALSIFIED"))
        epoch = meld.state(room, "integrator")["epoch"]
        chat.post("@integrator [MELD e:%d] READY:%d (seat-b joined %s)"
                  % (epoch, epoch, room), room=room, who=self.READER,
                  sign=False)
        got = RD.census()
        self.assertEqual(got["t2"]["join_rate"], 1.0)
        self.assertEqual(got["t2"]["reading"], "HOLDS")

    def test_T1_after_an_all_mechanical_patch_FALSIFIES_and_a_design_parent_HOLDS(self):
        one = self.first(self.a)
        self.fix(one, patch_tip=self.c)
        planted = self.add(ref=self.branch("next", self.c),
                           recipient=self.READER, kind="review",
                           lane=self.lane, supersedes=one["id"],
                           door={"meld_door": {"trigger": "T1",
                                               "action": "nudge",
                                               "disputed": 0}})
        got = RD.census()
        self.assertEqual(got["t1"]["fired"], 1)
        self.assertEqual(got["t1"]["rows"], [planted["id"][:12]])
        self.assertTrue(got["t1"]["reading"].startswith("FALSIFIED"))
        # the same firing after a design-class read is in scope
        two = self.first(self.a)
        self.fix(two)
        self.add(ref=self.b, recipient=self.READER, kind="review",
                 lane=self.lane, supersedes=two["id"],
                 door={"meld_door": {"trigger": "T1", "action": "nudge",
                                     "disputed": 0}})
        got = RD.census()
        self.assertEqual((got["t1"]["fired"], got["t1"]["on_all_mechanical"]),
                         (2, 1))

    def test_the_verb_prints_both_readings(self):
        rc, out, err = run(dispatches.cmd_dispatch, ["melds", "--hours", "24"])
        self.assertEqual(rc, 0, err)
        self.assertIn("(a) T2 join rate", out)
        self.assertIn("(b) T1 after an all-mechanical patch FIX", out)
        self.assertIn("prior for (a)", out)
        self.assertIn("review mode A/B", out)
        self.assertIn("tokens: UNKNOWN", out)

    def test_the_verb_prints_an_UNKNOWN_round_count_as_UNKNOWN(self):
        row = {"chain": "c" * 32, "mode": "UNKNOWN",
               "enrollment_round": "UNKNOWN",
               "active_review_rounds": "UNKNOWN", "cure_cycles": "UNKNOWN",
               "first_send_ts": None, "first_hold_ts": None,
               "send_to_hold_s": "UNKNOWN", "reviewer_tokens": "UNKNOWN",
               "author_tokens": "UNKNOWN"}
        with mock.patch.object(RD, "mode_metrics", return_value={
                "chains": [row], "by_mode": {}, "token_reading": "UNKNOWN"}):
            rc, out, err = run(dispatches.cmd_dispatch,
                               ["melds", "--hours", "24"])
        self.assertEqual(rc, 0, err)
        self.assertIn("UNKNOWN active read round(s); UNKNOWN cure cycle(s)",
                      out)

    def test_the_verb_refuses_unknown_repeated_and_nonpositive_arguments(self):
        rc, out, err = run(dispatches.cmd_dispatch,
                           ["melds", "--hours", "1", "--json"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(json.loads(out)["window_h"], 1)
        for args in (("--bogus",), ("--hours", "0"),
                     ("--hours", "1", "--hours", "900"),
                     ("--json", "--json"), ("--hours", "--json")):
            with self.subTest(args=args):
                rc, out, err = run(dispatches.cmd_dispatch, ["melds", *args])
                self.assertEqual(rc, 2)
                self.assertEqual(out, "")
                self.assertIn("helm dispatch melds:", err)


class ModeMetricsCensusTest(unittest.TestCase):
    """task/3713 D1: one chain-root fold for the PATCH/MELD-DIFF trial.

    These are synthetic projections plus the ACCEPTED event slices that made
    them. No arm reads the live Helm home. The PATCH chain deliberately has the
    task/3693 shape: the enrolled reader cures once, its patch comes back for
    confirmation, and another reader records an extra fixture cure at that same
    patch tip. Only the enrolled reader's active read is the A/B round/cure.
    """

    PATCH = "a" * 32
    DIFF = "b" * 32
    A, B, C, D = (x * 40 for x in "abcd")

    @staticmethod
    def dispatch(rid, chain, ts, tip, recipient="seat-b",
                 sender="seat-a", mode=None, status="open", **fields):
        row = {"v": 3, "event": "dispatch", "seq": 0, "id": rid,
               "ts": ts, "kind": "review", "sender": sender,
               "recipient": recipient, "lane": "mode-metrics", "tip": tip,
               "chain_root": chain, "status": status, "repo_id": "/repo"}
        if mode:
            row["review_mode"] = mode
        row.update(fields)
        return row

    @staticmethod
    def event(row, kind, ts, **fields):
        out = {"v": 3, "event": kind, "seq": fields.pop("seq", 1),
               "id": row["id"], "ts": ts}
        out.update(fields)
        return out

    def fixture(self):
        # PATCH: one real mode read/cure. B is the enrolled reviewer's patch
        # adoption/confirmation; the independent reader's extra FIX at B must
        # not turn it into another PATCH-mode round or cure cycle.
        a1 = self.dispatch("1" * 32, self.PATCH, "2026-09-29T00:00:00Z",
                           self.A, mode="PATCH", status="verdict",
                           polarity="fix", reviewed_tip=self.A,
                           verdict_ts="2026-09-29T00:05:00Z",
                           patch_tip=self.B, patch_author="seat-b")
        # The projection is what the fold leaves after the re-hold: the
        # CURRENT hold is 00:30. Only the accepted event slice still holds the
        # first one (00:20), so an arm reading the projection's hold_ts fails.
        a2 = self.dispatch("2" * 32, self.PATCH, "2026-09-29T00:06:00Z",
                           self.B, mode="PATCH", status="held",
                           source_clean_tip=self.B, hold_actor="seat-b",
                           hold_ts="2026-09-29T00:30:00Z",
                           hold_reason="clean again")
        a3 = self.dispatch("3" * 32, self.PATCH, "2026-09-29T00:07:00Z",
                           self.B, recipient="seat-c", status="verdict",
                           polarity="fix", reviewed_tip=self.B,
                           verdict_ts="2026-09-29T00:12:00Z",
                           patch_tip=self.C, patch_author="seat-c")

        # MELD-DIFF: an earlier independent read makes mode enrollment round 2.
        # Two sends of D to the enrolled reviewer are fan-out, one active round.
        b0 = self.dispatch("4" * 32, self.DIFF, "2026-09-29T01:00:00Z",
                           self.C, recipient="seat-c", status="verdict",
                           polarity="fix", reviewed_tip=self.C,
                           verdict_ts="2026-09-29T01:05:00Z")
        b1 = self.dispatch("5" * 32, self.DIFF, "2026-09-29T01:06:00Z",
                           self.D, mode="MELD-DIFF", status="verdict",
                           polarity="fix", reviewed_tip=self.D,
                           verdict_ts="2026-09-29T01:10:00Z",
                           no_patch_because="mode asks the author to apply it")
        # The fan-out send is ANSWERED too, with its own FIX at D: counted per
        # row it would be a second round and a second cure cycle.
        b2 = self.dispatch("6" * 32, self.DIFF, "2026-09-29T01:07:00Z",
                           self.D, mode="MELD-DIFF", status="verdict",
                           polarity="fix", reviewed_tip=self.D,
                           verdict_ts="2026-09-29T01:12:00Z",
                           no_patch_because="mode asks the author to apply it")

        current = {r["id"]: r for r in (a1, a2, a3, b0, b1, b2)}
        accepted = {
            a1["id"]: [dict(a1, status="open", polarity=None,
                            reviewed_tip=None, verdict_ts=None,
                            patch_tip=None, patch_author=None),
                       self.event(a1, "verdict", "2026-09-29T00:05:00Z",
                                  reviewed_tip=self.A, polarity="fix",
                                  patch_tip=self.B,
                                  patch_author="seat-b")],
            a2["id"]: [dict(a2, status="open", source_clean_tip=None,
                            hold_actor=None, hold_ts=None),
                       self.event(a2, "hold", "2026-09-29T00:20:00Z",
                                  source_clean_tip=self.B,
                                  hold_actor="seat-b", reason="clean"),
                       self.event(a2, "release", "2026-09-29T00:25:00Z", seq=2),
                       self.event(a2, "hold", "2026-09-29T00:30:00Z", seq=3,
                                  source_clean_tip=self.B,
                                  hold_actor="seat-b", reason="clean again")],
            a3["id"]: [dict(a3, status="open", polarity=None,
                            reviewed_tip=None, verdict_ts=None,
                            patch_tip=None, patch_author=None),
                       self.event(a3, "verdict", "2026-09-29T00:12:00Z",
                                  reviewed_tip=self.B, polarity="fix",
                                  patch_tip=self.C,
                                  patch_author="seat-c")],
            b0["id"]: [dict(b0, status="open", polarity=None,
                            reviewed_tip=None, verdict_ts=None),
                       self.event(b0, "verdict", "2026-09-29T01:05:00Z",
                                  reviewed_tip=self.C, polarity="fix")],
            b1["id"]: [dict(b1, status="open", polarity=None,
                            reviewed_tip=None, verdict_ts=None,
                            no_patch_because=None),
                       self.event(b1, "verdict", "2026-09-29T01:10:00Z",
                                  reviewed_tip=self.D, polarity="fix",
                                  no_patch_because="mode asks the author to apply it")],
            b2["id"]: [dict(b2, status="open", polarity=None,
                            reviewed_tip=None, verdict_ts=None,
                            no_patch_because=None),
                       self.event(b2, "verdict", "2026-09-29T01:12:00Z",
                                  reviewed_tip=self.D, polarity="fix",
                                  no_patch_because="mode asks the author to apply it")],
        }
        return current, accepted

    def test_cross_chain_fanout_patch_adoption_and_release_rehold_fold_once(self):
        current, accepted = self.fixture()
        got = RD.mode_metrics(current, accepted, cutoff=0)
        rows = {r["chain"]: r for r in got["chains"]}

        patch = rows[self.PATCH]
        self.assertEqual((patch["mode"], patch["enrollment_round"],
                          patch["active_review_rounds"], patch["cure_cycles"]),
                         ("PATCH", 1, 1, 1))
        self.assertEqual(patch["send_to_hold_s"], 20 * 60)
        self.assertEqual(patch["first_hold_ts"], "2026-09-29T00:20:00Z",
                         "release/re-hold replaced the first accepted hold")

        diff = rows[self.DIFF]
        self.assertEqual((diff["mode"], diff["enrollment_round"],
                          diff["active_review_rounds"], diff["cure_cycles"]),
                         ("MELD-DIFF", 2, 1, 1))
        self.assertEqual(diff["send_to_hold_s"], "UNKNOWN")
        self.assertEqual(got["by_mode"]["PATCH"]["chains"], 1)
        self.assertEqual(got["by_mode"]["MELD-DIFF"]["chains"], 1)

    def test_missing_token_evidence_is_UNKNOWN_never_bytes_or_zero(self):
        current, accepted = self.fixture()
        for row in current.values():
            row.update(brief_bytes=999_999, meld_bytes=888_888)
        got = RD.mode_metrics(current, accepted, cutoff=0)
        self.assertEqual(len(got["chains"]), 2)
        for row in got["chains"]:
            self.assertEqual(row["reviewer_tokens"], "UNKNOWN")
            self.assertEqual(row["author_tokens"], "UNKNOWN")
            self.assertNotIn(row["reviewer_tokens"], (0, 999_999, 888_888))
        self.assertEqual(got["by_mode"]["PATCH"]["reviewer_tokens"], "UNKNOWN")

    def test_two_tuple_snapshot_marks_accepted_event_metrics_UNKNOWN(self):
        current, _accepted = self.fixture()
        with mock.patch.object(RD, "_prior_join_rate",
                               return_value={"convened": 0,
                                             "joined_in_window": 0,
                                             "unreadable": 0,
                                             "join_rate": None}), \
                mock.patch.object(RD, "pair_census", return_value={"kept": True}):
            got = RD.census(
                snap=(current, None), hours=24,
                now=dispatches.instant_epoch("2026-09-29T02:00:00Z"))
        rows = {r["chain"]: r for r in got["mode_ab"]["chains"]}
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[self.PATCH]["active_review_rounds"], "UNKNOWN")
        self.assertEqual(rows[self.PATCH]["cure_cycles"], "UNKNOWN")
        self.assertEqual(rows[self.PATCH]["send_to_hold_s"], "UNKNOWN")
        self.assertTrue({"t2", "t1", "retro", "prior", "pair"} <= set(got))

    def test_census_reads_one_coherent_accepted_event_snapshot(self):
        current, accepted = self.fixture()
        folded = (current, {}, accepted, {}, None)
        with mock.patch.object(dispatches, "snapshot_and_events",
                               return_value=folded) as read, \
                mock.patch.object(dispatches, "snapshot",
                                  side_effect=AssertionError("second fold")), \
                mock.patch.object(RD, "_prior_join_rate",
                                  return_value={"convened": 0,
                                                "joined_in_window": 0,
                                                "unreadable": 0,
                                                "join_rate": None}), \
                mock.patch.object(RD, "pair_census", return_value={"kept": True}):
            got = RD.census(
                hours=24,
                now=dispatches.instant_epoch("2026-09-29T02:00:00Z"))
        read.assert_called_once_with()
        self.assertEqual(len(got["mode_ab"]["chains"]), 2)
        rows = {r["chain"]: r for r in got["mode_ab"]["chains"]}
        patch = rows[self.PATCH]
        # The chain list comes from the state slice alone; these three come
        # only from the ACCEPTED slice of that same read, so a census that
        # dropped or re-read it cannot produce them.
        self.assertEqual((patch["active_review_rounds"], patch["cure_cycles"],
                          patch["send_to_hold_s"]), (1, 1, 20 * 60))
        self.assertEqual(rows[self.DIFF]["active_review_rounds"], 1)
        self.assertEqual(rows[self.DIFF]["cure_cycles"], 1)
        # The existing census is additive: its old top-level products survive.
        self.assertTrue({"t2", "t1", "retro", "prior", "pair"} <= set(got))

    def test_a_chain_with_two_recorded_modes_reads_UNKNOWN_never_zero(self):
        current, accepted = self.fixture()
        current["6" * 32]["review_mode"] = "PATCH"
        got = RD.mode_metrics(current, accepted, cutoff=0)
        diff = {r["chain"]: r for r in got["chains"]}[self.DIFF]
        self.assertEqual(
            (diff["mode"], diff["enrollment_round"],
             diff["active_review_rounds"], diff["cure_cycles"],
             diff["send_to_hold_s"]),
            ("UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN"),
            "no single mode means no enrolled reader: its rounds and cures "
            "were not measured")
        self.assertEqual(got["by_mode"]["UNKNOWN"]["cure_cycles"], "UNKNOWN")
        self.assertEqual(got["by_mode"]["PATCH"]["cure_cycles"], 1)

    def test_a_burn_forced_row_is_not_a_second_mode_on_its_chain(self):
        current, accepted = self.fixture()
        current["6" * 32].update(review_mode="PATCH",
                                 review_mode_cause="burn flags ORANGE for "
                                                   "this family")
        got = RD.mode_metrics(current, accepted, cutoff=0)
        diff = {r["chain"]: r for r in got["chains"]}[self.DIFF]
        self.assertEqual(diff["mode"], "MELD-DIFF")
        self.assertNotIn("UNKNOWN", got["by_mode"])

    def test_a_strangers_accepted_hold_is_not_the_readers_answer(self):
        row = self.dispatch("e" * 32, self.PATCH, "2026-09-29T00:00:00Z",
                            self.A, mode="PATCH")
        hold = self.event(row, "hold", "2026-09-29T00:10:00Z",
                          source_clean_tip=self.A, hold_actor="seat-a",
                          reason="not the reader")
        state = dispatches._apply(dict(row), hold)
        self.assertEqual(state["source_clean_tip"], self.A)
        current = {row["id"]: state}
        accepted = {row["id"]: [dict(row), hold]}
        patch = RD.mode_metrics(current, accepted, cutoff=0)["chains"][0]
        self.assertEqual((patch["active_review_rounds"], patch["first_hold_ts"],
                          patch["send_to_hold_s"]), (0, None, "UNKNOWN"))

        hold = dict(hold, hold_actor="seat-b")
        current[row["id"]] = dispatches._apply(dict(row), hold)
        accepted[row["id"]][-1] = hold
        patch = RD.mode_metrics(current, accepted, cutoff=0)["chains"][0]
        self.assertEqual((patch["active_review_rounds"], patch["first_hold_ts"],
                          patch["send_to_hold_s"]),
                         (1, "2026-09-29T00:10:00Z", 600))

    def test_a_cancelled_row_with_another_mode_still_makes_the_chain_unknown(self):
        current, accepted = self.fixture()
        baseline = {r["chain"]: r for r in RD.mode_metrics(
            current, accepted, cutoff=0)["chains"]}[self.DIFF]
        self.assertEqual(baseline["mode"], "MELD-DIFF")
        row = self.dispatch("e" * 32, self.DIFF, "2026-09-29T00:50:00Z",
                            self.C, mode="PATCH")
        cancel = self.event(row, "cancel", "2026-09-29T00:51:00Z",
                            reason="rebound")
        current[row["id"]] = dispatches._apply(dict(row), cancel)
        self.assertEqual(current[row["id"]]["status"], "cancelled")
        accepted[row["id"]] = [dict(row), cancel]
        diff = {r["chain"]: r for r in RD.mode_metrics(
            current, accepted, cutoff=0)["chains"]}[self.DIFF]
        self.assertEqual((diff["mode"], diff["active_review_rounds"],
                          diff["cure_cycles"], diff["send_to_hold_s"]),
                         ("UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN"))

    def test_an_entirely_cancelled_chain_keeps_its_accepted_hold(self):
        row = self.dispatch("e" * 32, self.PATCH, "2026-09-29T00:00:00Z",
                            self.A, mode="PATCH")
        hold = self.event(row, "hold", "2026-09-29T00:10:00Z",
                          source_clean_tip=self.A, hold_actor="seat-b",
                          reason="clean")
        cancel = self.event(row, "cancel", "2026-09-29T00:20:00Z",
                            seq=2, reason="rebound")
        state = dispatches._apply(dict(row), hold)
        state = dispatches._apply(state, cancel)
        self.assertEqual(state["status"], "cancelled")
        got = RD.mode_metrics({row["id"]: state},
                              {row["id"]: [dict(row), hold, cancel]}, cutoff=0)
        self.assertEqual(len(got["chains"]), 1)
        patch = got["chains"][0]
        self.assertEqual((patch["mode"], patch["active_review_rounds"],
                          patch["first_hold_ts"], patch["send_to_hold_s"]),
                         ("PATCH", 1, "2026-09-29T00:10:00Z", 10 * 60))

    def test_a_cancelled_enrolled_row_with_an_unreadable_kind_hides_rounds(self):
        current, accepted = self.fixture()
        baseline = {r["chain"]: r for r in RD.mode_metrics(
            current, accepted, cutoff=0)["chains"]}[self.PATCH]
        self.assertEqual((baseline["active_review_rounds"],
                          baseline["cure_cycles"]), (1, 1))
        row = self.dispatch("e" * 32, self.PATCH, "2026-09-28T23:50:00Z",
                            self.A, mode="PATCH")
        cancel = self.event(row, "cancel", "2026-09-28T23:51:00Z",
                            reason="rebound")
        state = dispatches._apply(dict(row), cancel)
        current[row["id"]] = dispatches._note_unknown_kind(state, "future")
        self.assertEqual(dispatches.unknown_event_kinds(current[row["id"]]),
                         ("future",))
        accepted[row["id"]] = [dict(row), cancel]
        patch = {r["chain"]: r for r in RD.mode_metrics(
            current, accepted, cutoff=0)["chains"]}[self.PATCH]
        self.assertEqual((patch["active_review_rounds"], patch["cure_cycles"],
                          patch["send_to_hold_s"]),
                         ("UNKNOWN", "UNKNOWN", "UNKNOWN"))

    def test_retracted_fix_is_neither_a_round_nor_a_cure(self):
        current, accepted = self.fixture()
        rid = "1" * 32
        current[rid].update(polarity="retracted", retracted_polarity="fix",
                            verdict_retracted=True)
        accepted[rid].append(self.event(current[rid], "verdict-retract",
                                        "2026-09-29T00:08:00Z", seq=2))
        patch = {r["chain"]: r for r in RD.mode_metrics(
            current, accepted, cutoff=0)["chains"]}[self.PATCH]
        self.assertEqual((patch["active_review_rounds"], patch["cure_cycles"]),
                         (1, 0))
        self.assertEqual(patch["send_to_hold_s"], 20 * 60)
        self.assertEqual(RD.mode_metrics(current, accepted, cutoff=0)
                         ["by_mode"]["PATCH"]["cure_cycles"], 0)

    def test_retracted_fanout_does_not_hide_a_live_fix_at_the_same_tip(self):
        current, accepted = self.fixture()
        for rid in ("5" * 32, "6" * 32):
            current[rid].update(polarity="retracted", retracted_polarity="fix",
                                verdict_retracted=True)
            accepted[rid].append(self.event(current[rid], "verdict-retract",
                                            "2026-09-29T01:15:00Z", seq=2))
        diff = {r["chain"]: r for r in RD.mode_metrics(
            current, accepted, cutoff=0)["chains"]}[self.DIFF]
        self.assertEqual((diff["active_review_rounds"], diff["cure_cycles"]),
                         (0, 0))
        current["6" * 32].update(polarity="fix", retracted_polarity=None,
                                 verdict_retracted=False)
        accepted["6" * 32].pop()
        diff = {r["chain"]: r for r in RD.mode_metrics(
            current, accepted, cutoff=0)["chains"]}[self.DIFF]
        self.assertEqual((diff["active_review_rounds"], diff["cure_cycles"]),
                         (1, 1))

    def test_retraction_keeps_an_earlier_accepted_hold_as_the_answer(self):
        current, accepted = self.fixture()
        rid = "1" * 32
        accepted[rid].insert(1, self.event(current[rid], "hold",
                                            "2026-09-29T00:02:00Z",
                                            source_clean_tip=self.A,
                                            hold_actor="seat-b"))
        accepted[rid].insert(2, self.event(current[rid], "release",
                                            "2026-09-29T00:03:00Z", seq=2))
        accepted[rid][3]["seq"] = 3
        current[rid].update(polarity="retracted", retracted_polarity="fix",
                            verdict_retracted=True)
        accepted[rid].append(self.event(current[rid], "verdict-retract",
                                        "2026-09-29T00:08:00Z", seq=4))
        patch = {r["chain"]: r for r in RD.mode_metrics(
            current, accepted, cutoff=0)["chains"]}[self.PATCH]
        self.assertEqual((patch["active_review_rounds"], patch["cure_cycles"],
                          patch["first_hold_ts"]),
                         (2, 0, "2026-09-29T00:02:00Z"))
        current[rid].update(polarity="fix", retracted_polarity=None,
                            verdict_retracted=False)
        accepted[rid].pop()
        patch = {r["chain"]: r for r in RD.mode_metrics(
            current, accepted, cutoff=0)["chains"]}[self.PATCH]
        self.assertEqual((patch["active_review_rounds"], patch["cure_cycles"]),
                         (1, 1))

    def held_then(self, end):
        """The fixture plus an independent reader's row that holds B clean at
        00:10, before a2's 00:20, then ENDS with `end` at 00:11. The row state
        is the real fold's, from the accepted slice: (state, PATCH chain)."""
        current, accepted = self.fixture()
        row = self.dispatch("8" * 32, self.PATCH, "2026-09-29T00:08:00Z",
                            self.B, recipient="seat-d")
        accepted[row["id"]] = [
            dict(row),
            self.event(row, "hold", "2026-09-29T00:10:00Z",
                       source_clean_tip=self.B, hold_actor="seat-d",
                       reason="clean"),
            self.event(row, end, "2026-09-29T00:11:00Z", seq=2, reason="moot")]
        state = dispatches._apply(dict(row), accepted[row["id"]][1])
        state = dispatches._apply(state, accepted[row["id"]][2])
        current[row["id"]] = state
        return state, {r["chain"]: r for r in RD.mode_metrics(
            current, accepted, cutoff=0)["chains"]}[self.PATCH]

    def test_a_cancel_after_a_clean_hold_does_not_rewrite_the_first_hold(self):
        """A later CANCEL rewrites the first hold no more than a release or a
        retraction does: the chain first read source-clean when the fold took
        that hold. The control is the same row RELEASED instead."""
        state, patch = self.held_then("release")
        self.assertEqual(state["status"], "open")
        self.assertEqual(patch["first_hold_ts"], "2026-09-29T00:10:00Z")
        self.assertEqual(patch["send_to_hold_s"], 10 * 60)
        state, patch = self.held_then("cancel")
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["seq"], 2)
        self.assertEqual(patch["first_hold_ts"], "2026-09-29T00:10:00Z",
                         "a cancel rewrote the first accepted hold")
        self.assertEqual(patch["send_to_hold_s"], 10 * 60)
        # an independent reader's row: no mode round and no cure
        self.assertEqual((patch["active_review_rounds"], patch["cure_cycles"]),
                         (1, 1))

    def enrolled_held_then(self, end):
        """The fixture plus a fan-out send of A to the ENROLLED reader at
        23:50, before a1's 00:00, that holds A clean at 00:01 and then ENDS
        with `end` at 00:02. The row state is the real fold's, from the
        accepted slice: (state, PATCH chain)."""
        current, accepted = self.fixture()
        row = self.dispatch("c" * 32, self.PATCH, "2026-09-28T23:50:00Z",
                            self.A, mode="PATCH")
        accepted[row["id"]] = [
            dict(row),
            self.event(row, "hold", "2026-09-29T00:01:00Z",
                       source_clean_tip=self.A, hold_actor="seat-b",
                       reason="clean"),
            self.event(row, end, "2026-09-29T00:02:00Z", seq=2, reason="moot")]
        state = dispatches._apply(dict(row), accepted[row["id"]][1])
        state = dispatches._apply(state, accepted[row["id"]][2])
        current[row["id"]] = state
        return state, {r["chain"]: r for r in RD.mode_metrics(
            current, accepted, cutoff=0)["chains"]}[self.PATCH]

    def test_a_cancel_of_the_enrolled_readers_held_row_does_not_rewrite_its_send(self):
        """A cancel rewrites the SEND no more than it rewrites the hold. When
        the enrolled reader's first send is the row it held clean, and that
        row is later cancelled, the clock still starts at that send. A clock
        that keeps the cancelled row's hold and drops its send starts at a
        later fan-out send: 60 s, where the ledger says 660 s. The control is
        the same row RELEASED instead."""
        state, patch = self.enrolled_held_then("release")
        self.assertEqual(state["status"], "open")
        self.assertEqual(patch["first_send_ts"], "2026-09-28T23:50:00Z")
        self.assertEqual(patch["first_hold_ts"], "2026-09-29T00:01:00Z")
        self.assertEqual(patch["send_to_hold_s"], 11 * 60)
        state, patch = self.enrolled_held_then("cancel")
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["seq"], 2)
        self.assertEqual(patch["first_send_ts"], "2026-09-28T23:50:00Z",
                         "a cancel moved the first send past the hold it "
                         "answered")
        self.assertEqual(patch["first_hold_ts"], "2026-09-29T00:01:00Z")
        self.assertEqual(patch["send_to_hold_s"], 11 * 60)
        # a1's FIX answers the same tip A: the rounds and cures do not move
        self.assertEqual((patch["active_review_rounds"], patch["cure_cycles"]),
                         (1, 1))

    def claim_on_holds(self, **claim):
        """The fixture where the ENROLLED reader's a1 takes a hold at 00:02
        (released at 00:03, before its FIX) and its new row at D takes one at
        00:41, each with `claim` over a well-formed source-clean claim.
        (the D row's folded state, PATCH chain)."""
        current, accepted = self.fixture()
        rid = "1" * 32
        accepted[rid].insert(1, self.event(
            current[rid], "hold", "2026-09-29T00:02:00Z", **dict(
                {"source_clean_tip": self.A, "hold_actor": "seat-b",
                 "reason": "clean"}, **claim)))
        accepted[rid].insert(2, self.event(
            current[rid], "release", "2026-09-29T00:03:00Z", seq=2))
        accepted[rid][3]["seq"] = 3
        row = self.dispatch("9" * 32, self.PATCH, "2026-09-29T00:40:00Z",
                            self.D, mode="PATCH")
        accepted[row["id"]] = [dict(row), self.event(
            row, "hold", "2026-09-29T00:41:00Z", **dict(
                {"source_clean_tip": self.D, "hold_actor": "seat-b",
                 "reason": "clean"}, **claim))]
        state = dispatches._apply(dict(row), accepted[row["id"]][1])
        current[row["id"]] = state
        patch = {r["chain"]: r for r in RD.mode_metrics(
            current, accepted, cutoff=0)["chains"]}[self.PATCH]
        return state, (patch["first_hold_ts"], patch["send_to_hold_s"],
                       patch["active_review_rounds"], patch["cure_cycles"])

    def test_a_hold_the_fold_took_without_its_clean_claim_is_no_answer(self):
        """The fold can TAKE a hold and still drop its source-clean claim: a
        hold owes one holder, so an owner-gated one keeps no clean tip, and a
        tip `_clean_tip_of` cannot read installs none. The projection reads an
        ordinary or owner-gated hold, and the trial must read what it reads.
        The control is a well-formed claim: the clock stops at 00:02 and D is
        a second active round."""
        state, got = self.claim_on_holds()
        self.assertEqual((state["status"], state["source_clean_tip"]),
                         ("held", self.D))
        self.assertEqual(got, ("2026-09-29T00:02:00Z", 2 * 60, 2, 1))
        # the fold TOOK each hold below (seq 1, held) and kept no clean tip
        state, got = self.claim_on_holds(owner_gated=True)
        self.assertEqual((state["seq"], state["status"], state["owner_gated"],
                          state.get("source_clean_tip")),
                         (1, "held", True, None))
        self.assertEqual(got, ("2026-09-29T00:20:00Z", 20 * 60, 1, 1),
                         "an owner-gated hold stopped the source-clean clock")
        state, got = self.claim_on_holds(source_clean_tip="b" * 39)
        self.assertEqual((state["seq"], state["status"],
                          state.get("source_clean_tip")), (1, "held", None))
        self.assertEqual(got, ("2026-09-29T00:20:00Z", 20 * 60, 1, 1),
                         "a hold with an unreadable clean tip stopped the clock")

    def test_a_row_this_helm_cannot_read_in_full_reads_UNKNOWN_never_zero(self):
        """A NEWER HELM appended an event of a kind this fold has no arm for,
        then its verdict. This fold records the kind on the row and takes
        nothing after it (tests.test_ledger_unknown_kinds), so the slice left
        counts the enrolled reader's cure as 0 and can miss the first hold."""
        def unread(current, accepted, rid):
            current[rid] = dict(accepted[rid][0], **{
                dispatches.UNKNOWN_KINDS_FIELD: ("future-kind",)})
            accepted[rid] = accepted[rid][:1]

        # the ENROLLED reader's row: its rounds, cures and first hold
        current, accepted = self.fixture()
        unread(current, accepted, "1" * 32)
        got = RD.mode_metrics(current, accepted, cutoff=0)
        rows = {r["chain"]: r for r in got["chains"]}
        patch = rows[self.PATCH]
        self.assertEqual((patch["mode"], patch["active_review_rounds"],
                          patch["cure_cycles"], patch["send_to_hold_s"]),
                         ("PATCH", "UNKNOWN", "UNKNOWN", "UNKNOWN"))
        self.assertEqual(got["by_mode"]["PATCH"]["cure_cycles"], "UNKNOWN")
        # the other chain reads every row in full and is still measured
        self.assertEqual((rows[self.DIFF]["active_review_rounds"],
                          rows[self.DIFF]["cure_cycles"]), (1, 1))

        # an INDEPENDENT reader's row counts no mode round, but its hidden
        # events may hold the chain's first source-clean hold
        current, accepted = self.fixture()
        unread(current, accepted, "3" * 32)
        patch = {r["chain"]: r for r in RD.mode_metrics(
            current, accepted, cutoff=0)["chains"]}[self.PATCH]
        self.assertEqual((patch["active_review_rounds"], patch["cure_cycles"],
                          patch["send_to_hold_s"], patch["first_hold_ts"]),
                         (1, 1, "UNKNOWN", None))

    def test_a_snapshot_without_accepted_events_reads_UNKNOWN_never_zero(self):
        current, accepted = self.fixture()
        with mock.patch.object(RD, "_prior_join_rate",
                               return_value={"convened": 0,
                                             "joined_in_window": 0,
                                             "unreadable": 0,
                                             "join_rate": None}), \
                mock.patch.object(RD, "pair_census", return_value={}):
            got = RD.census(
                snap=(current, None), hours=24,
                now=dispatches.instant_epoch("2026-09-29T02:00:00Z"))
        self.assertEqual(len(got["mode_ab"]["chains"]), 2)
        for row in got["mode_ab"]["chains"]:
            self.assertEqual((row["active_review_rounds"], row["cure_cycles"],
                              row["send_to_hold_s"]),
                             ("UNKNOWN", "UNKNOWN", "UNKNOWN"), row["chain"])
        # the positive control: the same state WITH its events is measured
        got = RD.mode_metrics(current, accepted, cutoff=0)
        self.assertEqual(got["chains"][0]["cure_cycles"], 1)

    def reissued(self, recipient):
        """The MELD-DIFF chain without its fan-out send, after the enrolled
        reader seat-b RETRACTS its FIX at D and `retract --reissue` mints the
        successor to `recipient`: the same tip, `--supersedes` the retracted
        row, and no review guidance, so no mode (task/3713 D4). The retracted
        state is the real fold's. -> (retracted state, DIFF chain, metrics)"""
        current, accepted = self.fixture()
        del current["6" * 32], accepted["6" * 32]
        rid, kid = "5" * 32, "7" * 32
        retract = self.event(current[rid], "verdict-retract",
                             "2026-09-29T01:20:00Z",
                             retract_reason="miscounted", retract_reads="fix",
                             retract_basis="inferred", retract_role="author",
                             retract_seat="seat-b", retracted_polarity="fix",
                             retracted_tip=self.D, retract_proof_version=1,
                             retract_successor=kid)
        state = dispatches._apply(current[rid], retract)
        current[rid] = state
        accepted[rid].append(retract)
        row = self.dispatch(kid, self.DIFF, "2026-09-29T01:20:01Z", self.D,
                            recipient=recipient, supersedes=rid)
        current[kid] = dict(row, status="verdict", polarity="fix",
                            reviewed_tip=self.D,
                            verdict_ts="2026-09-29T01:21:00Z")
        accepted[kid] = [dict(row), self.event(
            row, "verdict", "2026-09-29T01:21:00Z", reviewed_tip=self.D,
            polarity="fix", no_patch_because="mode asks the author to apply it")]
        got = RD.mode_metrics(current, accepted, cutoff=0)
        return (state, {r["chain"]: r for r in got["chains"]}[self.DIFF],
                got, accepted[kid][-1])

    def test_the_enrolled_readers_reissued_fix_is_its_cure(self):
        """A retracted FIX is no cure, but `retract --reissue` hands the SAME
        reader the same tip, and its reissued FIX is one. The reissue carries
        no mode (D4), so a fold that enrols only stamped rows read the live
        MELD-DIFF chain 9c15ba0a1fe4 at 0 cures. The control is the same
        successor sent to another seat: an independent reader's FIX."""
        state, diff, got, verdict = self.reissued("seat-b")
        self.assertEqual((verdict["event"], verdict["polarity"],
                          verdict["reviewed_tip"]), ("verdict", "fix", self.D))
        self.assertEqual((state["status"], state["polarity"],
                          state["verdict_retracted"]),
                         ("verdict", "retracted", True))
        self.assertEqual((diff["mode"], diff["active_review_rounds"],
                          diff["cure_cycles"]), ("MELD-DIFF", 1, 1),
                         "the enrolled reader's reissued FIX was not counted")
        self.assertEqual(got["by_mode"]["MELD-DIFF"]["cure_cycles"], 1)
        state, diff, got, verdict = self.reissued("seat-c")
        self.assertGreater(diff["enrollment_round"], 0)
        self.assertEqual((verdict["event"], verdict["polarity"],
                          verdict["reviewed_tip"]), ("verdict", "fix", self.D))
        self.assertIs(state["verdict_retracted"], True)
        self.assertEqual((diff["mode"], diff["active_review_rounds"],
                          diff["cure_cycles"]), ("MELD-DIFF", 0, 0),
                         "another seat's FIX was charged to the mode reader")
        self.assertEqual(got["by_mode"]["MELD-DIFF"]["cure_cycles"], 0)


class DoorAndRungAgreeTest(DoorBase):
    """THE SEAM: one chain, read by the send door before a send and by the
    stop rung after it, must get one answer. Both read the same fold and
    the same answered-rounds reading (`dispatches._answered_reading`); the
    round in flight is in the rung's count and in neither reading. Each
    arm builds three answered rounds through the real CLI and verdict door,
    asks the door about round four, sends it, then asks the rung."""

    def both(self, counts, relations, paths):
        from helm import seats_stop_signals as signals
        tips = [self.a, self.b, self.c]
        last = None
        for tip, count, relation, path in zip(tips, counts, relations, paths):
            if last is None:
                row = self.first(tip)
            else:
                rc, _out, err, row = self.send(last, tip)
                self.assertEqual(rc, 0, err)
            self.fix(row, path=path, finding_count=count,
                     prior_relation=relation)
            # ONE ROUND PER MOMENT: rounds written in one second have no
            # order the trajectory can read, which is its own UNKNOWN.
            self.age(row["id"], 900 - 300 * tips.index(tip))
            last = row
        nxt = self.commit("round-four")
        self.beacon = True
        door = RD.plan("review", "integrator", self.READER, self.lane, nxt,
                       supersedes=last["id"], beacon=lambda peer: True)
        rc, out, err, sent = self.send(last, nxt)
        self.assertEqual(rc, 0, err)
        info, err = dispatches.review_spiral("integrator")
        self.assertIsNone(err)
        block, warn = signals._spiral_gate("seam", "main", "integrator")
        return door, sent, out, info, block, warn

    def agree(self, door, sent, info, block):
        reading = door["row"]["meld_door"]["reading"]
        self.assertEqual(reading, info["prescription"],
                         "the door and the rung read one chain differently")
        self.assertEqual(sent["meld_door"]["reading"], reading)
        self.assertEqual(info["rounds"], 4)
        walled = block is not None
        melds = door["action"] in ("auto-open", "brief")
        self.assertEqual(walled, melds, "the rung walls a chain the door "
                         "let through, or the reverse: %s / %s"
                         % (door["action"], block))

    def test_a_converging_chain_is_FINISH_at_both_and_neither_melds(self):  # noqa: VACUOUS_ASSERTION — the absent meld is paired with a positive FINISH reading and note on the same row; the MELD arm in this class is the control, and DoorMutationTest kills the unexempted mutant
        door, sent, out, info, block, warn = self.both(
            (6, 3, 1), ("new", "regression-of-cure", "regression-of-cure"),
            ("helm/a.py",) * 3)
        self.agree(door, sent, info, block)
        self.assertEqual(info["prescription"], "FINISH")
        self.assertEqual(sent["meld_door"]["action"], "finish")
        self.assertIn("no meld: FINISH", out)
        self.assertNotIn("MELD OPENED", out)
        self.assertEqual(self.rooms(), [])
        self.assertIn("FINISH", warn)

    def test_a_flat_chain_is_MELD_at_both_and_both_meld(self):
        door, sent, out, info, block, _warn = self.both(
            (2, 2, 2), ("new", "uncured", "uncured"), ("helm/a.py",) * 3)
        self.agree(door, sent, info, block)
        self.assertEqual(info["prescription"], "MELD")
        self.assertIn("MELD OPENED", out)
        self.assertIn("review spiral", block)

    def test_an_under_armed_chain_is_UNDER_ARMED_at_both_and_both_meld_the_bar(self):
        door, sent, out, info, block, _warn = self.both(
            (1, 1, 1), ("new", "new", "new"),
            ("helm/a.py", "helm/b.py", "helm/c.py"))
        self.agree(door, sent, info, block)
        self.assertEqual(info["prescription"], "UNDER-ARMED")
        self.assertIn("UNDER-ARMED", out)
        self.assertIn("agrees the BAR", block)


class DoorAndRungAgreeOnOrderAndPatchesTest(DoorBase):
    """The seam, on two chain shapes where a door and a rung can read one
    chain differently. Each runs one chain through the door and the rung."""

    def rung(self, session):
        from helm import seats_stop_signals as signals
        info, err = dispatches.review_spiral("integrator")
        self.assertIsNone(err)
        block_text, _warn = signals._spiral_gate(session, "main", "integrator")
        return info, block_text

    def test_two_sends_in_one_second_read_the_same_at_both(self):
        """FINDING 3. B and C are sent inside one second and B is never read.
        The door ordered its unsent C at 'now + 1' and dropped B (2 rounds);
        the rung saw B and C tie and kept both (3 rounds, MELD). Both now
        order by the ledger's append order: C is newest, B was never read,
        and the chain is at two rounds for both."""
        one = self.first(self.a)
        self.fix(one)
        self.age(one["id"], 900)
        rc, _out, err, two = self.send(one, self.b)
        self.assertEqual(rc, 0, err)
        door = RD.plan("review", "integrator", self.READER, self.lane, self.c,
                       supersedes=two["id"], beacon=lambda peer: True)
        rounds_door, _ = dispatches.chain_rounds("integrator", two["id"],
                                                 self.c)
        rc, _out, err, three = self.send(two, self.c)
        self.assertEqual(rc, 0, err)
        stamp = two["ts"]
        self.age(two["id"], 0, ts=stamp)
        self.age(three["id"], 0, ts=stamp)
        info, block_text = self.rung("same-second")
        rung_rounds = info["rounds"] if info else 0
        self.assertEqual(rounds_door["rounds_after"], rung_rounds,
                         "the door and the rung counted one chain differently")
        self.assertEqual(rung_rounds, 2)
        self.assertEqual(door["trigger"] == "T2", block_text is not None,
                         "the rung walls a chain the door let through")

    def test_a_chain_of_answered_patch_tips_reads_three_rounds_at_both(self):
        """FINDING 1, at the seam. Each read returns FIX with a patch and the
        author adopts it: three FIX reads are three rounds at the rung, and
        the door treats the fourth send — the newest patch, the closing step
        — as round three's meld point too."""
        d = self.commit("d")
        tips = (self.a, self.b, self.c, d)
        last = self.first(tips[0])
        for i in range(3):
            self.fix(last, patch_tip=tips[i + 1])
            self.age(last["id"], 1200 - 300 * i)
            if i < 2:
                rc, _out, err, last = self.send(last, tips[i + 1])
                self.assertEqual(rc, 0, err)
        door = RD.plan("review", "integrator", self.READER, self.lane, d,
                       supersedes=last["id"], beacon=lambda peer: True)
        rc, out, err, sent = self.send(last, d)
        self.assertEqual(rc, 0, err)
        info, block_text = self.rung("patch-chain")
        self.assertIsNotNone(info, "the answered patch tips were hidden")
        self.assertEqual(info["rounds"], 3)
        self.assertEqual(door["trigger"], "T2")
        self.assertIn(door["action"], ("auto-open", "brief"))
        self.assertIn("review spiral", block_text or "")


class DoorMutationTest(unittest.TestCase):
    """Each trigger and exemption, reverted or widened, is killed by its arm.
    A mutant rewrites one line of the shipped function and runs one arm."""

    CASES = (
        # T1 widened to every FIX: falsifier (b) must catch it
        (RD, "_plan_t1", "if not design and not disputed:", "if False:",
         "test_T1_an_all_mechanical_patch_fix_never_nudges"),
        # T1 removed: the design finding goes unoffered
        (RD, "_plan_t1", "if not design and not disputed:", "if True:",
         "test_T1_a_design_finding_at_round_two_prints_the_meld_and_writes_the_row"),
        # T2 opens a meld whatever the reader's state
        (RD, "_plan_t2", "if live is True and not in_flight:", "if True:",
         "test_T2_briefs_the_bar_when_the_reader_has_no_live_beacon"),
        (RD, "_plan_t2", "if live is True and not in_flight:",
         "if live is True:",
         "test_T2_briefs_the_bar_when_the_reader_has_a_row_in_flight"),
        # --async-because ignored
        (RD, "_plan_t2", "    if async_because:\n", "    if False:\n",
         "test_T2_async_because_skips_it_and_is_recorded"),
        # the adopted patch tip counted as a new round at the door
        (RD, "plan", "    if not info[\"new_round\"] and not closing:\n",
         "    if False:\n",
         "test_T2_exempts_the_reviewers_adopted_patch_tip"),
        # an agreed meld ignored: the door opens a second one over it
        (RD, "plan", "        melded = room if ok else None\n",
         "        melded = None\n",
         "test_T2_exempts_a_chain_whose_bar_a_meld_already_AGREED"),
        # FINISH no longer exempt at the door: it melds what the rung lets go
        (RD, "_plan_t2", 'if info["prescription"] == "FINISH":',
         "if False:",
         "test_a_converging_chain_is_FINISH_at_both_and_neither_melds"),
        # THE SEAM: the rung reads every counted round again, in-flight
        # included, and calls a converging chain MELD while the door says
        # FINISH
        (dispatches, "review_spiral",
         "prescription, evidence = dispatches._answered_reading(view, "
         "current)",
         "prescription, evidence = dispatches._spiral_prescription(view, "
         "current)",
         "test_a_converging_chain_is_FINISH_at_both_and_neither_melds"),
        # T0's phrase set emptied
        (RD, "_plan_t0", "    if not hits:\n", "    if True:\n",
         "test_T0_an_irreversible_first_build_row_needs_a_design_meld"),
    )

    def test_each_door_mutant_is_killed_by_its_arm(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a fixed non-empty CASES tuple and asserts testsRun == 1 and exactly one failure on every pass
        import inspect
        for module, symbol, before, after, arm in self.CASES:
            with self.subTest(mutant=symbol, arm=arm):
                source = inspect.getsource(getattr(module, symbol))
                self.assertEqual(source.count(before), 1, before)
                # THE FUNCTION'S OWN NAMESPACE, not the module it is
                # reached through: a name moved to a ledger satellite
                # runs there and spells ledger names `dispatches.NAME`
                # (task/3407). For a function defined in `module` the
                # two are the same dict.
                namespace = dict(getattr(module, symbol).__globals__)
                exec(compile(source.replace(before, after), "door-mutant",
                             "exec"), namespace)
                owner = next(c for c in (ReviewDoorTest, DoorAndRungAgreeTest)
                             if hasattr(c, arm))
                with mock.patch.object(module, symbol, namespace[symbol]):
                    result = unittest.TestResult()
                    owner(arm).run(result)
                self.assertEqual(result.testsRun, 1)
                self.assertEqual(len(result.failures) + len(result.errors), 1,
                                 "arm %s did not kill its mutant" % arm)


if __name__ == "__main__":
    unittest.main()
