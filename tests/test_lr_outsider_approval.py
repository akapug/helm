#!/usr/bin/env python3
"""WHAT MAKES A LANE READY? AN APPROVAL FROM A SEAT THAT WROTE NONE OF ITS
CHAIN — AND NOTHING ELSE.

The review procedure lets a reviewer of either family COMMIT the cure it finds
and name it with `--patch-tip`; the lane then carries several authors, and the
prose promises that what keeps the families independent is one re-read of the
COMPOSED TIP by a reader who wrote none of it. The machine established no such
thing: `self_reviewed` compared ONE row's sender to ONE row's recipient. These
arms are the machine that makes the promise true, so they ask one question and
they ask it of the whole chain rather than of a row.

MOVED WHOLE OUT OF `tests/test_landreq.py`, which stood 126,090 bytes PAST the
never-track ceiling. No body was rewritten on the way: the class text here is
byte-identical to its text there, apart from the base reference on the `class`
line, which the note below explains and measures.

THE FIXTURE IS REUSED BY REFERENCE, NEVER COPIED. `LandReqBase` and `read_text`
are the objects `tests/test_landreq.py` defines, so a change to the fixture
still reaches these arms and the two files cannot drift apart.
"""
import inspect
import json
import os
import subprocess
import unittest
from unittest import mock

from helm import dispatches, eventledger, gate, landreq
from tests import test_landreq as _landreq
from tests.test_landreq import read_text


# THE BASE IS REACHED THROUGH ITS MODULE, NOT IMPORTED BY NAME, AND THAT IS
# MEASURED RATHER THAN styled. `unittest` collects every TestCase bound at
# module scope, imported ones included, under the id of the module that
# DEFINES it. A plain `from tests.test_landreq import LandReqBase` would
# therefore collect every arm the base carries a second time, here, under the
# same id. The base carries none now, and tests/test_suite_collection.py
# refuses the binding the day it gains one. Reaching the base through
# `_landreq` keeps the fixture shared by reference, which is the point of
# importing it, without republishing a collectable name.


# THIS MODULE DOES NOT READ HOST LIVENESS. Same declaration as
# `tests/test_landreq.py`'s, where the full argument and its measurements live;
# the short version is that every dispatch write here reaches
# `seat_usability.seat_verdict`, which walks the host's whole process table
# (54,114 pids on the build node, 0.677s a walk) to consult a liveness these
# arms never assert on — which made what they OBSERVED depend on what else was
# running beside them.
#
# DECLARED PER MODULE ON PURPOSE, not hoisted into a shared helper: "this
# module does not read host liveness" is a claim about THIS file that someone
# must re-check when its arms change, and a helper import would hide it.
#
# MODULE SCOPE, NOT A BASE CLASS, because a base-class hook would miss every
# class here that inherits `unittest.TestCase` directly, and importing
# `tests/test_landreq.py` does NOT run its setUpModule — unittest runs module
# fixtures per module under test.
_LIVE_SEATS_PATCH = None


def setUpModule():
    global _LIVE_SEATS_PATCH
    from helm import proxywatch
    _LIVE_SEATS_PATCH = mock.patch.object(
        proxywatch, "_live_seats", lambda: (set(), None, {}))
    _LIVE_SEATS_PATCH.start()


def tearDownModule():
    if _LIVE_SEATS_PATCH is not None:
        _LIVE_SEATS_PATCH.stop()


class OutsiderApprovalIsWhatMakesALaneREADYTest(_landreq.LandReqBase):
    """A lane is ready only when a seat that wrote NONE of its chain has
    approved the final tip.

    THE FINDING. The review procedure now lets a reviewer of either family
    COMMIT the cure it finds and name it with `--patch-tip`; the lane then
    carries several authors and the prose promises that what keeps the
    families independent is one re-read of the COMPOSED TIP by a reader who
    wrote none of it. The machine established no such thing. `self_reviewed`
    compared ONE row's sender to ONE row's recipient, and `chain_credits` only
    looks at authorship AFTER a close — so a reviewer R that patched round N
    through a FIX, then received the successor row and APPROVEd round N+1,
    passed the single-author check and the lane read plain READY while R had
    written part of what lands.

    EVERY ROW HERE IS WRITTEN BY THE SHIPPED WRITERS — `dispatches.send` and
    `dispatches.mark_verdict` through the base's author-binding helper — and
    read back through `landreq.get`, the projection the board itself reads.
    A hand-built row would prove what this file's dicts do, never what helm
    mints; `patched_round` asserts the ledger actually RECORDED seat-b as an
    author before any arm reasons about it.

    THE GATE RUNG IS NOT WHAT SEPARATES THESE ARMS. `LandReqBase` pins the
    writer's gate capability off, so an untokened APPROVE reaches READY here;
    the positive arms below reach PLAIN READY over the very same fixture,
    which is what proves the independence rung is the only thing moving.
    """

    def setUp(self):
        super().setUp()
        # A DURABLE CACHE IS A CROSS-TEST CHANNEL unless someone empties it.
        # The key carries the ledger path, so a stale hit needs two temporary
        # homes at one path — but an arm that INSTALLS an unreadable ledger
        # under an unchanged file would read the memo instead of the refusal.
        landreq._CHAIN_CONTRIB_MEMO.clear()
        landreq._LEDGER_FOLD_MEMO.clear()
        self.addCleanup(landreq._CHAIN_CONTRIB_MEMO.clear)
        self.addCleanup(landreq._LEDGER_FOLD_MEMO.clear)
        self.git("checkout", "-q", "side")
        self.cure = self.commit("the reviewer's own cure", path="g")
        self.git("checkout", "-q", self.main)
        # A REAL BOUND RECEIPT, because the token rung sits BELOW this one and
        # would otherwise answer UNVERIFIED for every row here — the positive
        # arms could then never reach plain READY and every negative would be
        # measuring the gate instead of the reviewer. A COMPLETE receipt whose
        # content id the reader RECOMPUTES, seeded into the ledger and asserted
        # present: a chosen id would index nothing and put the hole back.
        receipt = {
            "v": 1, "event": "gate", "ts": dispatches.pk.now_ts(),
            "repo_id": self.repo, "head": "0" * 40, "tree": "1" * 40,
            "dirty": False,
            "interpreter": {"name": "cpython", "version": "3.14.6",
                            "language": "3.14.6",
                            "executable": "/usr/bin/python3"},
            "argv": ["/usr/bin/python3", "-m", "unittest"],
            "status": "OK", "ran": 1, "skipped": 0, "rc": 0}
        receipt["id"] = gate._receipt_id(receipt)
        path = gate.receipts_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(receipt) + "\n")
        landreq._GATE_INDEX_MEMO.clear()
        self.assertIn(receipt["id"], landreq._gate_receipt_index(),
                      "the seeded receipt never reached the index, so every "
                      "arm below would measure the gate rung, not this one")
        binder = mock.patch.object(
            dispatches.gate, "bind",
            return_value=("VERIFIED", receipt["id"], "test receipt"))
        binder.start()
        self.addCleanup(binder.stop)

    def round_(self, lane, recipient, ref, supersedes=None):
        row, why, sent = dispatches.send(
            recipient, lane, "review " + lane, ref, repo=self.repo,
            key="key-" + lane, sign=False,
            new_work=supersedes is None, supersedes=supersedes)
        self.assertIsNone(why, why)
        self.assertTrue(sent)
        return row

    def patched_round(self):
        """Round N: seat-b reviews, finds a MECHANICAL defect, commits the cure
        off the exact reviewed tip and names it. seat-b is now an author."""
        row = self.round_("lane/r1", "seat-b", self.side)
        _v, why = self.mark_verdict(row["id"], row["tip"],
                                    "the guard is inverted", polarity="fix",
                                    patch_tip=self.cure)
        self.assertIsNone(why, why)
        self.assertEqual(
            dispatches.snapshot()[0][row["id"]].get("patch_author"), "seat-b",
            "fixture premise: the chain must RECORD seat-b as an author, or "
            "no arm in this class is about anything")
        return row

    def successor(self, recipient, parent, ref=None, lane=None):
        """The next round of the SAME chain, approved by `recipient`."""
        row = self.round_(lane or ("lane/after-" + parent["id"][:8]),
                          recipient, ref or self.cure,
                          supersedes=parent["id"])
        _v, why = self.mark_verdict(row["id"], row["tip"], "re-read clean",
                                    polarity="approve")
        self.assertIsNone(why, why)
        return row

    def lr(self, row):
        out, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        return out

    def test_an_ORDINARY_single_author_chain_is_READY_exactly_as_before(self):  # noqa: VACUOUS_ASSERTION — THE unconditional positive control for this class: it asserts plain READY on the same observable every negative below reads
        """THE CONTROL, and it is first because every other arm here is a
        negative. One author, one cross-family APPROVE from a seat that wrote
        nothing: plain READY, exactly as before this rung widened."""
        row = self.round_("lane/ordinary", "seat-c", self.side)
        _v, why = self.mark_verdict(row["id"], row["tip"], "clean",
                                    polarity="approve")
        self.assertIsNone(why, why)
        lr = self.lr(row)
        self.assertEqual(lr["state"], "READY")
        self.assertEqual(landreq.ready_word(lr), "READY")
        self.assertIs(landreq.independent_review(lr)[0], True)

    def test_the_reviewer_who_PATCHED_round_N_cannot_make_round_N1_READY(self):
        """THE FINDING ITSELF. seat-b patched round N and then approved the
        successor. The single-author check saw one author and passed."""
        first = self.patched_round()
        second = self.successor("seat-b", first)
        lr = self.lr(second)
        self.assertEqual(lr["state"], "READY",
                         "fixture premise: the row's stored state must be "
                         "READY, or this arm measures some other refusal")
        self.assertEqual(landreq.ready_word(lr), "READY-SELF-REVIEW")
        independent, why = landreq.independent_review(lr)
        self.assertIs(independent, False)
        self.assertIn("contributor approval present (seat-b)", why)
        self.assertIn("outsider approval missing", why)

    def test_one_APPROVE_from_a_seat_that_wrote_NONE_of_the_chain_is_READY(self):
        """THE POSITIVE POLE ON THE SAME CHAIN: one more round, read by a seat
        that appears nowhere in it as sender or patch author."""
        first = self.patched_round()
        second = self.successor("seat-b", first)
        self.assertEqual(landreq.ready_word(self.lr(second)),
                         "READY-SELF-REVIEW",
                         "the negative pole this arm is measured against")
        third = self.successor("seat-c", second)
        lr = self.lr(third)
        self.assertEqual(lr["state"], "READY")
        self.assertEqual(landreq.ready_word(lr), "READY")
        self.assertIn("independent — seat-c",
                      landreq.independent_review(lr)[1])

    def test_a_CONTRIBUTOR_round_after_the_outsider_read_keeps_its_authority(self):
        """An APPROVE from a contributor is RECORDED and counts toward the
        both-agree half; it just never SUPPLIES the independent authority. So
        a later contributor round over the very same tip stays ready on the
        outsider's earlier read — and this is the arm that exercises the
        sibling search rather than the row-local shortcut."""
        first = self.patched_round()
        second = self.successor("seat-b", first)
        third = self.successor("seat-c", second)          # the outsider read
        fourth = self.successor("seat-b", third)          # SAME tip, a writer
        lr = self.lr(fourth)
        self.assertEqual(lr["reviewer"], "seat-b",
                         "fixture premise: the final round's reviewer must be "
                         "a chain author, or the shortcut answers instead")
        self.assertEqual(landreq.ready_word(lr), "READY")
        self.assertIn("seat-c", landreq.independent_review(lr)[1])

    def test_an_outsider_APPROVE_on_ANOTHER_TIP_is_not_a_read_of_this_one(self):
        """The independence has to be about the tip that would LAND. An
        outsider who approved an earlier round read an artifact the composed
        tip has since replaced."""
        first = self.patched_round()
        older = self.successor("seat-c", first, ref=self.b,
                               lane="lane/older-tip")
        final = self.successor("seat-b", older)
        lr = self.lr(final)
        wrote, approved, err = landreq.chain_contributors(lr)
        self.assertIsNone(err, err)
        self.assertIn("seat-c", {row["recipient"] for row, _index in approved},
                      "the control: seat-c's APPROVE really is in this chain, "
                      "so the refusal below is about the TIP and nothing else")
        self.assertIn("seat-b", wrote)
        self.assertEqual(landreq.ready_word(lr), "READY-SELF-REVIEW")
        self.assertIn("outsider approval missing",
                      landreq.independent_review(lr)[1])

    def test_an_outsider_APPROVE_on_ANOTHER_CHAIN_never_travels(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is on the SAME observable and is asserted FIRST: the unrelated chain reads plain READY off its own outsider approve, so the approve is real and binds this very tip before the refusal is measured
        """Scoped by the bound repository plus the chain root. The unrelated
        row below is a REAL approve by a real outsider on the very same commit
        — its own lane reads plain READY — and it still says nothing about
        this chain."""
        first = self.patched_round()
        second = self.successor("seat-b", first)
        other = self.round_("lane/unrelated", "seat-c", self.cure)
        _v, why = self.mark_verdict(other["id"], other["tip"], "clean",
                                    polarity="approve")
        self.assertIsNone(why, why)
        self.assertEqual(landreq.ready_word(self.lr(other)), "READY",
                         "the control: the unrelated chain IS ready, so its "
                         "approve is real and binds this very tip")
        self.assertEqual(self.lr(other)["reviewed_tip"],
                         self.lr(second)["reviewed_tip"],
                         "fixture premise: one tip, two chains")
        self.assertEqual(landreq.ready_word(self.lr(second)),
                         "READY-SELF-REVIEW")

    def test_an_UNREADABLE_chain_is_UNKNOWN_and_never_plainly_READY(self):
        """MISSING EVIDENCE IS NOT EVIDENCE OF INDEPENDENCE. An unreadable
        ledger must not collapse to an empty contributor set, because an empty
        set says nobody wrote this chain — which would make every stranger
        independent exactly where helm knows least.

        THE WORD ALONE CANNOT DISCRIMINATE HERE and the arm says so: the
        contest rung also answers UNVERIFIED over an unreadable ledger. What
        this pins is the SENTENCE, which only this rung mints, plus the
        reader's own refusal one call below it."""
        first = self.patched_round()
        second = self.successor("seat-b", first)
        third = self.successor("seat-c", second)
        lr = self.lr(third)
        self.assertEqual(landreq.ready_word(lr), "READY",
                         "the control: readable, independent, plainly READY")
        # BOTH memos, because the join now reads a SHARED fold: clearing the
        # derived index alone would leave the cached rows answering for a
        # ledger this arm has made unreadable, and the refusal under test
        # would never be reached.
        landreq._CHAIN_CONTRIB_MEMO.clear()
        landreq._LEDGER_FOLD_MEMO.clear()
        # The dispatch fold reads its ledger as BYTES (task/2770): the failure
        # is planted at that door as well as at the row reader.
        with mock.patch.object(eventledger, "checked_events",
                               return_value=([], "PermissionError: denied")), \
                mock.patch.object(eventledger, "read_bytes",
                                  return_value=(None,
                                                "PermissionError: denied")):
            _chains, err = landreq.chain_contributor_index()
            independent, why = landreq.independent_review(lr)
            word = landreq.ready_word(lr)
        self.assertIn("denied", err or "",
                      "the reader never refused, so this arm measured a "
                      "readable ledger")
        self.assertIsNone(independent)
        self.assertIn("UNKNOWN", why)
        self.assertIn("denied", why)
        self.assertEqual(word, "READY-UNVERIFIED")
        self.assertNotEqual(word, "READY")

    def test_a_refusal_is_NEVER_memoised_over_a_later_readable_ledger(self):  # noqa: VACUOUS_ASSERTION — the second assertion is an unconditional positive on the same observable (independence True once the ledger reads again), and it is what the refusal is measured against
        """A transient refusal frozen into the cache would answer UNKNOWN for
        every later reader of an unchanged ledger."""
        first = self.patched_round()
        third = self.successor("seat-c", self.successor("seat-b", first))
        lr = self.lr(third)
        landreq._CHAIN_CONTRIB_MEMO.clear()
        landreq._LEDGER_FOLD_MEMO.clear()
        with mock.patch.object(eventledger, "checked_events",
                               return_value=([], "PermissionError: denied")), \
                mock.patch.object(eventledger, "read_bytes",
                                  return_value=(None,
                                                "PermissionError: denied")):
            self.assertIsNone(landreq.independent_review(lr)[0])
        self.assertIs(landreq.independent_review(lr)[0], True,
                      "the refusal was cached and outlived the condition")

    def test_the_surfaces_that_render_readiness_carry_the_contributor_line(self):
        """ARM 5, pinned on `lr show` and carried on the list line and the land
        nudge. A word that names a condition and withholds WHICH seat wrote
        the chain leaves the reader nothing to act on — the cure is to send the
        composed tip to a seat that is not that one."""
        first = self.patched_round()
        second = self.successor("seat-b", first)
        blocked = self.lr(second)
        shown = landreq._render_show(blocked)
        self.assertIn("outsider  contributor approval present (seat-b)", shown)
        self.assertIn("outsider approval missing", shown)
        self.assertIn("contributor approval present (seat-b)",
                      landreq._line(blocked))
        word, why, _cmd = landreq.land_nudge_instruction(second["id"])
        self.assertEqual(word, "READY-SELF-REVIEW")
        self.assertIn("seat-b", why or "")
        # THE OTHER POLE ON THE SAME SURFACES: an independent row says so, and
        # its list line grows no mark at all.
        third = self.successor("seat-c", second)
        clear = self.lr(third)
        self.assertIn("outsider  independent — seat-c",
                      landreq._render_show(clear))
        self.assertNotIn("outsider approval missing", landreq._line(clear))

    def change_event(self, row, event, remove=(), **changes):
        """Damage only this fixture's stored event, then exercise real replay."""
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        hits = 0
        with open(path, "w", encoding="utf-8") as fh:
            for record in events:
                if record.get("id") == row["id"] and record.get("event") == event:
                    record.update(changes)
                    for key in remove:
                        record.pop(key, None)
                    hits += 1
                fh.write(json.dumps(record) + "\n")
        self.assertEqual(hits, 1)
        landreq._CHAIN_CONTRIB_MEMO.clear()
        landreq._LEDGER_FOLD_MEMO.clear()
        landreq._CONTEST_MEMO.clear()

    def test_a_malformed_chain_is_UNKNOWN_not_a_new_independent_round(self):
        first = self.patched_round()
        final = self.successor("seat-b", first)
        self.assertEqual(landreq.ready_word(self.lr(final)), "READY-SELF-REVIEW")
        unrelated = self.round_("lane/unrelated-valid", "seat-c", self.cure)
        _v, why = self.mark_verdict(unrelated["id"], self.cure, "clean",
                                    polarity="approve")
        self.assertIsNone(why, why)
        self.assertEqual(landreq.ready_word(self.lr(unrelated)), "READY")
        self.change_event(final, "dispatch", chain_root="malformed-root")
        replay = dispatches.snapshot()[0][final["id"]]
        self.assertEqual(replay["chain_root"], dispatches.CHAIN_UNKNOWN)
        lr = self.lr(final)
        self.assertEqual(lr["state"], "READY")
        independent, why = landreq.independent_review(lr)
        self.assertIsNone(independent)
        self.assertIn("UNKNOWN", why)
        self.assertEqual(landreq.ready_word(lr), "READY-UNVERIFIED")
        self.assertEqual(landreq.ready_word(self.lr(unrelated)), "READY",
                         "an unreadable chain must not poison unrelated work")
        self.change_event(unrelated, "dispatch", chain_root="another-bad-root")
        self.assertIsNone(landreq.independent_review(self.lr(unrelated))[0])
        self.assertIsNone(landreq.independent_review(self.lr(final))[0])

    def test_a_damaged_ancestor_does_not_disappear_from_a_readable_successor(self):
        first = self.patched_round()
        outside = self.successor("seat-c", first)
        final = self.successor("seat-b", outside)
        self.assertEqual(landreq.ready_word(self.lr(final)), "READY")
        self.change_event(first, "dispatch", chain_root="broken-founder")
        self.assertIsNone(landreq.independent_review(self.lr(final))[0])
        self.assertEqual(landreq.ready_word(self.lr(final)), "READY-UNVERIFIED")
        self.change_event(first, "dispatch", chain_root=first["chain_root"])
        self.assertEqual(landreq.ready_word(self.lr(final)), "READY")
        self.change_event(outside, "dispatch", chain_root="broken-middle")
        self.assertIsNone(landreq.independent_review(self.lr(final))[0])
        self.assertEqual(landreq.ready_word(self.lr(final)), "READY-UNVERIFIED")

    def test_a_case_variant_sibling_author_is_not_an_outsider_either(self):
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "Seat-C"}):
            first = self.patched_round()
        apparent = self.successor("seat-c", first)
        final = self.successor("seat-b", apparent)
        self.assertEqual(landreq.ready_word(self.lr(final)), "READY-SELF-REVIEW")
        outside = self.successor("seat-d", final)
        agreed = self.successor("seat-b", outside)
        self.assertEqual(landreq.ready_word(self.lr(agreed)), "READY")
        self.assertEqual(dispatches.snapshot()[0][first["id"]]["sender"], "Seat-C")

    def test_a_legacy_home_parent_keeps_its_author_in_the_modern_chain(self):
        legacy = {"v": 1, "id": "a" * 32, "seq": 0, "status": "open",
                  "ts": "2026-04-01T00:00:00Z", "recipient": "seat-a",
                  "sender": "seat-b", "lane": "legacy", "tip": self.side,
                  "deadline_s": 600}
        os.makedirs(os.path.dirname(dispatches.ledger_path()), exist_ok=True)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), legacy))
        parent = dispatches.snapshot()[0][legacy["id"]]
        self.assertEqual(parent["sender"], "seat-b")
        self.assertIsNone(parent["repo_id"])
        self.assertIsNone(parent["chain_root"])
        final = self.successor("seat-b", parent)
        self.assertEqual(final["chain_root"], parent["id"])
        self.assertEqual(landreq.chain_key(parent), landreq.chain_key(final))
        self.assertIn("seat-b", landreq.chain_contributors(self.lr(final))[0])
        self.assertEqual(landreq.ready_word(self.lr(final)), "READY-SELF-REVIEW")
        outside = self.successor("seat-c", final)
        self.assertEqual(landreq.ready_word(self.lr(outside)), "READY")
        agreed = self.successor("seat-b", outside)
        self.assertEqual(landreq.ready_word(self.lr(agreed)), "READY")

    def test_sender_casing_does_not_create_an_outsider_or_rewrite_history(self):
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "Seat-B"}):
            first = self.round_("lane/mixed-sender", "seat-a", self.side)
        self.assertEqual(first["sender"], "Seat-B")
        final = self.successor("seat-b", first)
        lr = self.lr(final)
        self.assertEqual(lr["reviewer"], "seat-b")
        self.assertIn("Seat-B", landreq.chain_contributors(lr)[0])
        self.assertEqual(landreq.ready_word(lr), "READY-SELF-REVIEW")
        self.assertTrue(landreq.self_reviewed("Seat-B", "seat-b"))
        outside = self.successor("seat-c", final)
        self.assertEqual(landreq.ready_word(self.lr(outside)), "READY")
        self.assertEqual(dispatches.snapshot()[0][first["id"]]["sender"], "Seat-B")

    def test_an_OUTSIDE_tier_approval_cannot_rescue_a_contributor(self):
        from helm import home, store
        first = self.patched_round()
        store.write_prior({
            "id": "test-outsider-tier", "statement": "Final review seats.",
            "confidence": 1.0, "stated_ts": "2026-07-29T00:00:00Z",
            "source": "human", "policy_kind": "approval-tier",
            "policy_members": ["seat:seat-b", "seat:seat-d"],
            "policy_reason": "eligible independent review"},
            root_dir=os.path.join(home.global_dir(), "premises"))
        excluded = self.successor("seat-c", first)
        raw = dispatches.snapshot()[0][excluded["id"]]
        self.assertEqual(dispatches.approval_tier_for_verdict(raw)[0], "outside")
        self.assertEqual(self.lr(excluded)["state"], "REVIEWED")
        final = self.successor("seat-b", excluded)
        self.assertEqual(self.lr(final)["state"], "READY")
        self.assertEqual(landreq.ready_word(self.lr(final)), "READY-SELF-REVIEW")
        eligible = self.successor("seat-d", final)
        agreed = self.successor("seat-b", eligible)
        self.assertEqual(landreq.ready_word(self.lr(agreed)), "READY")
        self.assertIn("seat-d", landreq.independent_review(self.lr(agreed))[1])

    def test_a_PRE_TIER_sibling_is_not_independent_authority(self):
        first = self.patched_round()
        old = self.round_("lane/pre-tier", "seat-c", self.cure,
                          supersedes=first["id"])
        verdict, why = self.mark_verdict(old["id"], self.cure, "legacy approve",
                                        polarity="approve", bind_author=False)
        self.assertIsNone(why, why)
        self.assertEqual(dispatches.tier_unknown_kind(
            dispatches.approval_tier_for_verdict(verdict)[0]),
            dispatches.TIER_PRE_TIER)
        self.assertEqual(self.lr(old)["state"], "REVIEWED")
        final = self.successor("seat-b", old)
        self.assertEqual(landreq.ready_word(self.lr(final)), "READY-SELF-REVIEW")
        current = self.successor("seat-c", final)
        agreed = self.successor("seat-b", current)
        self.assertEqual(landreq.ready_word(self.lr(agreed)), "READY")

    def test_an_ungated_sibling_cannot_borrow_the_contributors_gate(self):
        first = self.patched_round()
        ungated = self.round_("lane/ungated-outsider", "seat-c", self.cure,
                              supersedes=first["id"])
        # The writer/binder mismatch is an existing lifecycle specimen: retain
        # a real author/tier bundle while the required gate is genuinely absent.
        with mock.patch.object(dispatches, "GATE_CAPS", (dispatches.GATE_CAP_RECEIPT,)), \
                mock.patch.object(dispatches.gate, "bind",
                                  return_value=("VERIFIED", "", "faulty binder")):
            verdict, why = self.mark_verdict(ungated["id"], self.cure, "clean",
                                            polarity="approve")
        self.assertIsNone(why, why)
        self.assertEqual(landreq.gate_requirement(verdict), "required")
        self.assertEqual(verdict["gate"], "")
        self.assertEqual(self.lr(ungated)["state"], "REVIEWED")
        final = self.successor("seat-b", ungated)
        self.assertEqual(self.lr(final)["state"], "READY")
        self.assertEqual(landreq.ready_word(self.lr(final)), "READY-SELF-REVIEW")
        eligible = self.successor("seat-d", final)
        agreed = self.successor("seat-b", eligible)
        self.assertEqual(landreq.ready_word(self.lr(agreed)), "READY")

    def test_damaged_sibling_authority_is_UNKNOWN_and_not_cached_as_permission(self):
        first = self.patched_round()
        outside = self.successor("seat-c", first)
        final = self.successor("seat-b", outside)
        lr = self.lr(final)
        self.assertEqual(landreq.ready_word(lr), "READY")
        original = dispatches.snapshot()[0][outside["id"]]["verdict_tier_anchor"]
        self.change_event(outside, "verdict", verdict_tier_anchor="damaged")
        self.assertEqual(self.lr(outside)["state"], "REVIEWED")
        self.assertIsNone(landreq.independent_review(lr)[0])
        self.assertEqual(landreq.ready_word(lr), "READY-UNVERIFIED")
        self.change_event(outside, "verdict", verdict_tier_anchor=original)
        self.assertEqual(landreq.ready_word(lr), "READY")
        # A non-ledger failure must be re-read even with both join memos warm.
        from helm.store import policy_history
        with mock.patch.object(policy_history, "resolve",
                               return_value=(None, "test history unreadable")):
            self.assertIsNone(landreq.independent_review(lr)[0])
        self.assertIs(landreq.independent_review(lr)[0], True)

    def test_repository_damage_is_scoped_to_connected_work_not_the_index(self):
        first = self.patched_round()
        outside = self.successor("seat-c", first)
        final = self.successor("seat-b", outside)
        other = self.round_("lane/bystander", "seat-d", self.cure)
        _v, why = self.mark_verdict(other["id"], self.cure, "clean",
                                    polarity="approve")
        self.assertIsNone(why, why)
        self.assertEqual(landreq.ready_word(self.lr(other)), "READY")
        self.assertEqual(landreq.ready_word(self.lr(final)), "READY")
        self.change_event(other, "dispatch", repo_id="")
        self.assertIsNone(landreq.chain_contributor_index()[1])
        self.assertIsNone(landreq.independent_review(self.lr(other))[0])
        self.assertEqual(landreq.ready_word(self.lr(final)), "READY")
        # Two adjacent unreadable rounds still connect to their successor;
        # neither an empty repository nor UNKNOWN is a bucket of unrelated work.
        self.change_event(first, "dispatch", repo_id="")
        self.change_event(outside, "dispatch", chain_root="broken-middle")
        self.assertIsNone(landreq.chain_contributor_index()[1])
        self.assertIsNone(landreq.independent_review(self.lr(final))[0])
        self.assertEqual(landreq.ready_word(self.lr(final)), "READY-UNVERIFIED")
        self.change_event(first, "dispatch", repo_id=first["repo_id"])
        self.change_event(outside, "dispatch", chain_root=outside["chain_root"])
        self.assertEqual(landreq.ready_word(self.lr(final)), "READY",
                         "the unrelated damaged repository is still present")

    def test_kindless_required_gate_approval_has_the_same_local_and_sibling_read(self):
        first = self.patched_round()
        with mock.patch.object(dispatches, "GATE_CAPS", (dispatches.GATE_CAP_RECEIPT,)):
            outside = self.successor("seat-c", first)
        raw = dispatches.snapshot()[0][outside["id"]]
        self.assertIsNone(raw["kind"], "exercise the supported writer default")
        self.assertEqual(landreq.gate_requirement(raw), "required")
        self.assertEqual(landreq.ready_word(self.lr(outside)), "READY")
        final = self.successor("seat-b", outside)
        lr = self.lr(final)
        self.assertEqual(landreq.ready_word(lr), "READY")
        # This fixture's receipt is deliberately v1 and unrelated to the tip's
        # tree. Readiness checks presence; deeper revalidation is another owner.
        receipts = landreq._gate_receipt_index()
        self.assertEqual(receipts[raw["gate"]]["v"], 1)
        self.assertEqual(landreq.ready_word(self.lr(outside), index=receipts), "READY")
        self.assertEqual(landreq.ready_word(lr, index=receipts), "READY")

    def test_same_row_name_fallback_survives_caller_normalization(self):
        # Direct projections have no ledger identity; malformed recipients are
        # not specimens the normal dispatch writer/replay would accept.
        with mock.patch.object(landreq, "_ledger_fold", return_value=({}, [], None)):
            for author, reviewer, same in (
                    (" seat b ", " seat b ", True),
                    ("seat b", " seat b ", True),
                    (" seat b ", "seat b", True),
                    (" @Seat-B ", "seat-b", True),
                    ("seat b", "seat c", False),
                    ("seat-b", "seat-c", False),
                    (" ", "", False), (None, None, False)):
                with self.subTest(author=author, reviewer=reviewer):
                    row = {"repo_id": self.repo, "author": author,
                           "reviewer": reviewer, "reviewed_tip": self.cure}
                    self.assertIs(landreq.self_reviewed(author, reviewer), same)
                    self.assertIs(landreq.independent_review(row, index={})[0],
                                  not same)

    def test_chain_index_resolves_each_legacy_repository_only_once(self):
        # Isolated fold input: the count covers readable and damaged roots,
        # not dispatch minting or the board's other repository readers.
        a, b, c, d = (letter * 32 for letter in "abcd")
        rows = {
            a: {"id": a, "sender": "seat-a"},
            b: {"id": b, "sender": "seat-b"},
            c: {"id": c, "repo_id": self.repo, "chain_root": a,
                "supersedes": a, "sender": "seat-c"},
            d: {"id": d, "chain_root": dispatches.CHAIN_UNKNOWN}}
        with mock.patch.object(landreq, "_fold_key", return_value=None), \
                mock.patch.object(landreq, "_ledger_fold",
                                  return_value=(rows, [], None)), \
                mock.patch.object(dispatches, "home_repo_id",
                                  return_value=(self.repo, None)) as home:
            chains, why = landreq.chain_contributor_index()
        self.assertIsNone(why, why)
        self.assertEqual(home.call_count, 3, "once per absent repository field")
        repo = dispatches._real(self.repo)
        self.assertEqual(set(chains), {(repo, a), (repo, b)})
        self.assertEqual(chains[(repo, a)], ({"seat-a", "seat-c"}, [], None))
        self.assertEqual(chains[(repo, b)], ({"seat-b"}, [], None))

    def test_only_the_sibling_receipt_can_disappear_and_restore_independence(self):
        first = self.patched_round()
        receipts = landreq._gate_receipt_index()
        other = dict(next(iter(receipts.values())), head="2" * 40)
        other["id"] = gate._receipt_id(other)
        self.assertNotIn(other["id"], receipts)
        with open(gate.receipts_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(other) + "\n")
        landreq._GATE_INDEX_MEMO.clear()
        with mock.patch.object(dispatches.gate, "bind", return_value=(
                "VERIFIED", other["id"], "distinct outsider receipt")), \
                mock.patch.object(dispatches, "GATE_CAPS",
                                  (dispatches.GATE_CAP_RECEIPT,)):
            outside = self.successor("seat-c", first)
        final = self.successor("seat-b", outside)
        own, consuming = self.lr(outside), self.lr(final)
        rows, positions, why = dispatches.snapshot_with_verdicts()
        self.assertIsNone(why, why)
        raw = rows[outside["id"]]
        pos = dispatches.verdict_index(positions, outside["id"])
        epoch = dispatches.gate_epoch(rows, positions)
        self.assertIsInstance(pos, int)
        self.assertEqual(epoch, pos)
        self.assertIsNone(landreq._approval_refusal(raw, index=pos, epoch=epoch)[0])
        self.assertEqual((own["state"], consuming["state"]), ("READY", "READY"))
        self.assertEqual(raw["gate"], other["id"])
        self.assertNotEqual(consuming["gate"], raw["gate"])
        lens = dict(landreq._gate_receipt_index())
        self.assertIn(raw["gate"], lens)
        self.assertIn(consuming["gate"], lens)
        self.assertEqual(landreq.ready_word(own, index=lens), "READY")
        self.assertEqual(landreq.ready_word(consuming, index=lens), "READY")
        removed = lens.pop(raw["gate"])
        self.assertIsNone(landreq._ready_receipt_refusal(consuming, lens))
        self.assertIsNone(landreq._approval_refusal(raw, index=pos, epoch=epoch)[0])
        self.assertEqual(landreq.ready_word(own, index=lens), "READY-UNVERIFIED")
        independent, why = landreq.independent_review(consuming, receipts=lens)
        self.assertIsNone(independent)
        self.assertIn("receipt ledger cannot speak", why)
        self.assertEqual(landreq.ready_word(consuming, index=lens),
                         "READY-UNVERIFIED")
        lens[raw["gate"]] = removed
        self.assertIs(landreq.independent_review(consuming, receipts=lens)[0], True)
        self.assertEqual(landreq.ready_word(own, index=lens), "READY")
        self.assertEqual(landreq.ready_word(consuming, index=lens), "READY")
        self.assertEqual(dispatches.snapshot()[0][outside["id"]], raw,
                         "no verdict evidence was changed to move the receipt rung")

    def _legacy_sibling_at_epoch(self, epoch_first):
        first = self.patched_round()
        parent = first
        if epoch_first:
            with mock.patch.object(dispatches, "GATE_CAPS",
                                   (dispatches.GATE_CAP_RECEIPT,)):
                parent = self.successor("seat-b", first)
        outside = self.round_("lane/legacy-capability", "seat-c", self.cure,
                              supersedes=parent["id"])
        apply_event = dispatches._apply

        def old_writer(state, event, **fold):
            # Historical writer specimen: omit the stamp BEFORE the canonical
            # tier producer captures authority, never mutate sealed evidence.
            # THE FOLD'S KEYWORDS PASS THROUGH: `_fold` calls the reducer with
            # current/verdicts/position and swallows any exception, so a
            # two-argument specimen silently dropped every replayed event
            # inside `mark_verdict`'s own snapshot — the row's delivered event
            # among them — and the verdict was minted one seq short, which the
            # real replay then refused as out of sequence.
            if event.get("event") == "verdict" and event.get("id") == outside["id"]:
                event.pop("gate_caps", None)
            return apply_event(state, event, **fold)

        with mock.patch.object(dispatches, "_apply", side_effect=old_writer):
            _v, why = self.mark_verdict(outside["id"], self.cure,
                                        "legacy writer read", polarity="approve")
        self.assertIsNone(why, why)
        caps = () if epoch_first else (dispatches.GATE_CAP_RECEIPT,)
        with mock.patch.object(dispatches, "GATE_CAPS", caps):
            final = self.successor("seat-b", outside)
        rows, positions, why = dispatches.snapshot_with_verdicts()
        self.assertIsNone(why, why)
        raw = rows[outside["id"]]
        pos = dispatches.verdict_index(positions, outside["id"])
        epoch = dispatches.gate_epoch(rows, positions)
        self.assertNotIn("gate_caps", raw)
        self.assertIsInstance(pos, int)
        self.assertIsInstance(epoch, int)
        # Earlier authority and the later receipt rung pass independently of
        # the integer boundary; only accepted position changes eligibility.
        self.assertIsNone(landreq._approval_refusal(raw, index=pos, epoch=None)[0])
        self.assertIsNone(landreq._ready_receipt_refusal(
            raw, landreq._gate_receipt_index()))
        return raw, pos, epoch, self.lr(outside), self.lr(final)

    def test_valid_legacy_sibling_before_integer_epoch_is_eligible(self):
        raw, pos, epoch, own, consuming = self._legacy_sibling_at_epoch(False)
        self.assertLess(pos, epoch)
        self.assertIsNone(landreq._approval_refusal(raw, index=pos, epoch=epoch)[0])
        self.assertEqual(landreq.ready_word(own), "READY")
        self.assertIs(landreq.independent_review(consuming)[0], True)
        self.assertEqual(landreq.ready_word(consuming), "READY")

    def test_valid_legacy_sibling_after_integer_epoch_is_unknown(self):
        raw, pos, epoch, own, consuming = self._legacy_sibling_at_epoch(True)
        self.assertGreater(pos, epoch)
        self.assertEqual(landreq.gate_requirement(raw, index=pos, epoch=epoch),
                         "unknown")
        self.assertIn("stamped no gate_caps",
                      landreq._approval_refusal(raw, index=pos, epoch=epoch)[0])
        self.assertEqual(own["state"], "REVIEWED")
        self.assertEqual(consuming["state"], "READY")
        independent, why = landreq.independent_review(consuming)
        self.assertIsNone(independent)
        self.assertIn("stamped no gate_caps", why)
        self.assertEqual(landreq.ready_word(consuming), "READY-UNVERIFIED")

    def test_missing_receipt_and_empty_exempt_token_have_paired_UNKNOWN_results(self):
        """A SIBLING WHOSE GATE TOKEN WAS DAMAGED AFTER THE FACT IS UNKNOWN,
        in its OWN row and in the row that would consume it — one predicate,
        two readers, the pair this class exists to hold together.

        THE TOKEN IS PART OF WHAT THE VERDICT'S TIER EVIDENCE BINDS, so
        rewriting it damages the recorded tier anchor as well, and the
        row-local refusal therefore arrives at the TIER rung rather than at
        the receipt rung. An earlier shape of this arm asserted that refusal
        was None first and so could never run at all. The receipt rung is
        still pinned, on the same raw row, by asking the shared lens
        directly — the lane cannot reach it while the rung above refuses.
        """
        first = self.patched_round()
        outside = self.successor("seat-c", first)
        final = self.successor("seat-b", outside)
        lr = self.lr(final)
        raw = dispatches.snapshot()[0][outside["id"]]
        token = raw["gate"]
        self.assertEqual(landreq.ready_word(lr), "READY")
        self.assertEqual(landreq.ready_word(self.lr(outside)), "READY")
        for absent, refusal in (
                ("", "APPROVE has no gate token to check"),
                ("f" * 16,
                 "the receipt ledger cannot speak for this gate token")):
            with self.subTest(token=absent):
                self.change_event(outside, "verdict", gate=absent)
                damaged = dispatches.snapshot()[0][outside["id"]]
                self.assertEqual(damaged["gate"], absent)
                self.assertEqual(landreq._ready_receipt_refusal(
                    damaged, landreq._gate_receipt_index()), refusal)
                self.assertEqual(dispatches.approval_tier_for_verdict(
                    damaged)[0], "unknown")
                self.assertEqual(self.lr(outside)["state"], "REVIEWED")
                self.assertIsNone(landreq.independent_review(lr)[0])
                self.assertEqual(landreq.ready_word(lr), "READY-UNVERIFIED")
        # THE CONTROL: the same fixture with the stored token put back. Both
        # readers return, so the arms above were moving this one field and not
        # measuring a lane that was broken to begin with.
        self.change_event(outside, "verdict", gate=token)
        self.assertEqual(landreq.ready_word(self.lr(outside)), "READY")
        self.assertIs(landreq.independent_review(lr)[0], True)
        self.assertEqual(landreq.ready_word(lr), "READY")

    def test_unreadable_caps_and_post_epoch_absence_are_paired_UNKNOWN(self):
        """The WRITER-CAPABILITY half of the same pair: a sibling whose
        gate_caps stopped being readable is UNKNOWN in its own row and in the
        row that would consume it.

        THIS FIXTURE HAS NO INTEGER CUTOVER, and an earlier shape of this arm
        asserted one. `LandReqBase` pins the writer's gate capability OFF, so
        no verdict here stamps a capability an epoch could be founded on: with
        the field REMOVED the boundary was never written at all, and with the
        field PRESENT-but-null there is a stamp the scan can see and no marker
        that explains it, which is LOST. Neither is an int, and the sibling
        rung must answer without one.
        """
        first = self.patched_round()
        outside = self.successor("seat-c", first)
        final = self.successor("seat-b", outside)
        lr = self.lr(final)
        self.assertEqual(landreq.ready_word(self.lr(outside)), "READY")
        self.assertEqual(landreq.ready_word(lr), "READY")
        for absent, cutover, requirement in ((False, dispatches.EPOCH_LOST,
                                              "unknown"),
                                             (True, None, "none")):
            with self.subTest(absent=absent):
                if absent:
                    self.change_event(outside, "verdict", remove=("gate_caps",))
                else:
                    self.change_event(outside, "verdict", gate_caps=None)
                rows, positions, err = dispatches.snapshot_with_verdicts()
                self.assertIsNone(err, err)
                raw = rows[outside["id"]]
                pos = dispatches.verdict_index(positions, outside["id"])
                epoch = dispatches.gate_epoch(rows, positions)
                self.assertIsInstance(pos, int)
                self.assertEqual(epoch, cutover)
                self.assertEqual(
                    landreq.gate_requirement(raw, index=pos, epoch=epoch),
                    requirement)
                # The RECEIPT rung still passes on this row: what moved is the
                # capability the recorded tier evidence bound, not the token.
                self.assertIsNone(landreq._ready_receipt_refusal(
                    raw, landreq._gate_receipt_index()))
                self.assertEqual(
                    dispatches.approval_tier_for_verdict(raw)[0], "unknown")
                self.assertEqual(self.lr(outside)["state"], "REVIEWED")
                self.assertIsNone(landreq.independent_review(lr)[0])
                self.assertEqual(landreq.ready_word(lr), "READY-UNVERIFIED")
                # THE CONTROL, inside the arm: restore the stamp this fixture
                # writes and both readers return to plain READY.
                self.change_event(outside, "verdict", gate_caps=[])
                self.assertEqual(landreq.ready_word(self.lr(outside)), "READY")
                self.assertIs(landreq.independent_review(lr)[0], True)
                self.assertEqual(landreq.ready_word(lr), "READY")

    def test_sibling_eligibility_does_not_depend_on_its_historical_checkout(self):
        first = self.patched_round()
        outside = self.successor("seat-c", first)
        final = self.successor("seat-b", outside)
        lr = self.lr(final)
        self.assertEqual(landreq.ready_word(lr), "READY")
        foreign = os.path.join(self.tmp, "foreign-repository")
        subprocess.run(["git", "init", "-q", foreign], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for checkout in (None, os.path.join(self.tmp, "deleted-checkout"), foreign):
            with self.subTest(checkout=checkout):
                self.change_event(outside, "dispatch", repo_root=checkout)
                self.assertEqual(landreq.ready_word(lr), "READY")
                self.assertIs(landreq.independent_review(lr)[0], True)
        self.assertEqual(dispatches.snapshot()[0][final["id"]]["repo_root"], self.repo)

    def test_tip_comparison_normalizes_both_sides_without_changing_writer_evidence(self):
        """The WRITER normalises the tip it stores and the READER normalises
        the tip it is handed, so one sha spelled two ways is one sha.

        THE CONSUMING ROW IS THE ONLY THING THIS ARM PERTURBS. An earlier
        shape handed the rung a lens of REBUILT approval rows; a rebuilt row
        no longer binds the recorded tier evidence the sibling rung re-derives
        from it, so the lane would have read UNVERIFIED for a reason that has
        nothing to do with tips. Every sibling row here stays byte-identical
        and the arm asserts that at the end.
        """
        first = self.patched_round()
        outside = self.round_("lane/uppercase-tip", "seat-c", self.cure,
                              supersedes=first["id"])
        verdict, why = self.mark_verdict(outside["id"], self.cure.upper(), "clean",
                                        polarity="approve")
        self.assertIsNone(why, why)
        self.assertEqual(verdict["reviewed_tip"], self.cure)
        final = self.successor("seat-b", outside)
        lr = self.lr(final)
        self.assertEqual(landreq.ready_word(lr), "READY")
        for spelling in (self.cure.upper(), " " + self.cure + " ",
                         " " + self.cure.upper() + " "):
            with self.subTest(tip=spelling):
                read = dict(lr, reviewed_tip=spelling)
                self.assertIs(landreq.independent_review(read)[0], True)
                self.assertEqual(landreq.ready_word(read), "READY")
        # THE MUST-HIT: a genuinely DIFFERENT tip is not another spelling of
        # this one, and the sibling's approval must not travel to it — so the
        # normalisation above is reading the sha and not accepting anything.
        elsewhere = dict(lr, reviewed_tip="0" * 40)
        self.assertIs(landreq.independent_review(elsewhere)[0], False)
        self.assertEqual(landreq.ready_word(elsewhere), "READY-SELF-REVIEW")
        self.assertEqual(dispatches.snapshot()[0][outside["id"]]["reviewed_tip"],
                         self.cure)

    def test_a_seat_compared_to_itself_is_the_same_seat_in_either_spelling(self):
        """Canonical first, exact string underneath — and NEITHER direction
        loosens the rung.

        The canonical comparison answers False when either name is not a seat
        token, and False at this rung means "a different seat", so an author
        helm cannot canonicalise would stop being the reviewer of their own
        row. The exact-equality fallback is the rule this rung carried before,
        kept underneath the canonical one. An UNRECORDED name still accuses
        nobody.
        """
        self.assertTrue(landreq.self_reviewed("seat-b", "Seat-B"))
        self.assertTrue(landreq.self_reviewed("seat b", "seat b"))
        self.assertFalse(landreq.self_reviewed("seat-b", "seat-c"))
        self.assertFalse(landreq.self_reviewed("", ""))
        self.assertFalse(landreq.self_reviewed(None, None))

    def test_a_recorded_author_is_matched_as_a_seat_not_as_bytes(self):
        """The same three poles at the CONTRIBUTOR-MEMBERSHIP reader: the
        sibling APPROVE is excluded when the chain records its seat under
        either spelling, and only then.

        The approval rows in the lens are the INDEX'S OWN objects — only the
        recorded author set moves — so the sibling's eligibility is decided by
        the same stored evidence in all three arms. The write door refuses a
        non-canonical recipient, so the last pole is the shape that could only
        arrive on a hand-written row: a recorded name that is neither the same
        seat canonically nor the same bytes is a DIFFERENT seat, and the
        sibling stays an outsider.
        """
        first = self.patched_round()
        outside = self.successor("seat-c", first)
        final = self.successor("seat-b", outside)
        lr = self.lr(final)
        self.assertEqual(landreq.ready_word(lr), "READY")
        index, err = landreq.chain_contributor_index()
        self.assertIsNone(err, err)
        key = landreq.chain_key(lr)
        wrote, approved, unknown = index[key]
        self.assertIn("seat-b", wrote)
        self.assertNotIn("seat-c", wrote,
                         "the sibling reviewer must START as an outsider or "
                         "no arm below is about membership")
        for recorded, independent in (("Seat-C", False), ("seat-c", False),
                                      ("seat c", True)):
            with self.subTest(recorded=recorded):
                lens = dict(index)
                lens[key] = (set(wrote) | {recorded}, approved, unknown)
                self.assertIs(landreq.independent_review(lr, index=lens)[0],
                              independent)

    def test_context_wide_rungs_do_not_become_independence_accusations(self):
        first = self.patched_round()
        outside = self.successor("seat-c", first)
        final = self.successor("seat-b", outside)
        lr = self.lr(final)
        self.assertEqual(landreq.ready_word(lr), "READY")
        stale = dict(lr, base_behind=landreq.STALE_BASE_BEHIND)
        self.assertIs(landreq.independent_review(stale)[0], True)
        self.assertEqual(landreq.ready_word(stale), "READY-STALE-BASE")
        invisible = dict(lr, observable=False)
        self.assertIs(landreq.independent_review(invisible)[0], True)
        self.assertEqual(landreq.ready_word(invisible), "READY-UNVERIFIED")
        other = self.round_("lane/contesting-chain", "seat-d", self.cure)
        _v, why = self.mark_verdict(other["id"], self.cure, "still broken",
                                    polarity="fix")
        self.assertIsNone(why, why)
        self.assertIs(landreq.independent_review(lr)[0], True)
        self.assertEqual(landreq.ready_word(lr), "READY-CONTESTED")
        self.assertEqual(landreq.ready_word(stale), "READY-CONTESTED")

    def test_RECORDED_CONTRIBUTORS_ARE_SUBMISSION_PROVENANCE_not_git_authorship(self):
        """The limit, said where a later reader will look for it. `patch_author`
        is the row's own RECIPIENT — the seat the cure was submitted through —
        and helm never opens the commit to ask who its Git author was."""
        src = inspect.getsource(landreq.chain_contributor_index)
        self.assertIn("SUBMISSION PROVENANCE, NOT PROVEN GIT AUTHORSHIP", src)
        doc = read_text(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "docs", "VERBS.md"))
        self.assertIn("submission provenance", doc)
