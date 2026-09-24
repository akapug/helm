#!/usr/bin/env python3
"""CONTRARY — WHAT DOES A LAND THAT DEFIED ITS VERDICT OBLIGE, HOW DO WE KNOW
IT HAPPENED, AND WHERE MUST IT BE VISIBLE?

Seven classes, one question, asked at each of its three joints.

  * WHAT IT OBLIGES. `ContraryDischargeTest` pins that the history stays after
    an approved round retires the operational debt; `ContraryDischargeArmsTest`
    pins the discharge predicate itself (of 52 in-flight contrary rows on the
    live board, 32 were discharged through succession and 20 genuinely were
    not); `FixContraryCarrierDoorTest` pins that the succession door speaks the
    SAME vocabulary for "on trunk" that the row's own landed marker does.
  * HOW WE KNOW. `contrary_state` says WHAT happened, `contrary_provenance`
    says HOW WE KNOW: two branches emit the identical string "landed" from
    entirely different evidence, a live git observation and a string recorded
    at close time that may never be re-checkable.
  * WHERE IT IS VISIBLE. `ContraryHonoredOnEverySurfaceTest` pins that `list`,
    `show` and `card()` cannot disagree — the owner's console once counted
    ELEVEN contrary rows over SIX live ones — and
    `ConfirmationRowIsNeverContraryTest` pins the row KIND the classifier's
    vocabulary lacked, the confirmation round whose healthy shape is the
    contrary signature exactly.

MOVED WHOLE OUT OF `tests/test_landreq.py`, which stood 126,090 bytes PAST the
never-track ceiling. No body was rewritten on the way: each class text here is
byte-identical to its text there, apart from the base reference on the `class`
line, which the note below explains and measures.

THE FIXTURE IS REUSED BY REFERENCE, NEVER COPIED. `LandReqBase` and `run` are
the objects `tests/test_landreq.py` defines, so a change to the fixture still
reaches these arms and the two files cannot drift apart.
"""
import json
import os
import subprocess
import time
import unittest
from unittest import mock

from helm import dispatches, eventledger, gate, landreq
from tests import test_landreq as _landreq
from tests.test_landreq import run


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

# THE ENV KEYS THESE ARMS SET, AND A MODULE-SCOPE RESTORE FOR THEM.
#
# `LandReqBase.setUp` snapshots `tests.test_landreq.ENV_KEYS` — HELM_CHAT_NAME
# among them — and its tearDown puts every one back, so each arm below is
# already covered per test by the fixture this module imports by reference.
# `tests/test_env_hygiene.py` reads ONE MODULE AT A TIME with `ast` and cannot
# see an inherited fixture, so on the move out of `tests/test_landreq.py` this
# file read as a leak and the whole-suite gate went red on it. It was a true
# statement about this file and a false one about the process.
#
# NAMING THE KEY IS NOT THE CURE; RESTORING IT IS. This tuple is not an
# allowlist entry bought to quiet a rung — `tearDownModule` below actually puts
# each key back from a snapshot taken before any arm in this module ran, which
# is a second, independent guarantee that also covers any future class here
# that does not descend from `LandReqBase` (`ContraryDischargeArmsTest` already
# does not).
_ENV_KEYS_SET_HERE = ("HELM_CHAT_NAME",)
_ENV_PRIOR = {}


def setUpModule():
    global _LIVE_SEATS_PATCH
    from helm import proxywatch
    _ENV_PRIOR.update({k: os.environ.get(k) for k in _ENV_KEYS_SET_HERE})
    _LIVE_SEATS_PATCH = mock.patch.object(
        proxywatch, "_live_seats", lambda: (set(), None, {}))
    _LIVE_SEATS_PATCH.start()


def tearDownModule():
    if _LIVE_SEATS_PATCH is not None:
        _LIVE_SEATS_PATCH.stop()
    for key, value in _ENV_PRIOR.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class ContraryDischargeTest(_landreq.LandReqBase):
    """A contrary FIX/SUPERSEDE land remains history after an approved round
    retires the operational debt."""

    def handoff(self, lane, ref, supersedes=None, force=False):
        """A round. `supersedes` names the round it continues — which is the
        WORK IDENTITY discharge now walks, and the reason a superseding round
        must be dispatched with --supersedes rather than merely be later."""
        row, why, sent = dispatches.send(
            "codex-3", lane, "review " + lane, ref, repo=self.repo,
            key="key-" + lane, sign=False,
            new_work=supersedes is None, supersedes=supersedes, force=force)
        self.assertIsNone(why)
        self.assertTrue(sent)
        return row

    def resolved(self, polarity="fix", upstream=False, push=False):
        if upstream or push:
            self.add_origin()
        first = self.handoff("feature-r1", self.side)
        self.mark_verdict(first["id"], self.side, "findings",
                                polarity=polarity)
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve findings", path="g")
        self.git("checkout", "-q", self.main)
        second = self.handoff("feature-r2", fixed, supersedes=first["id"])
        self.mark_verdict(second["id"], fixed, "re-probed clean",
                                polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        if push:
            self.git("push", "-q", "origin", self.main)
        return first, second, fixed

    def test_a_discharge_that_recorded_NOTHING_claims_no_provenance(self):
        """FIX round 2, and the arm that pins the NARROW cure.

        `contrary_state` reads `discharge_contrary` on the discharged branch,
        so a discharged row whose `discharge_contrary` is ABSENT falls through
        to None and the fact renders "STATE UNREAD" — while the provenance
        still said "recorded". One sentence claiming a state was recorded and
        that no state can be read.

        MY FIRST CURE DELETED `discharged` FROM THE MAPPING AND WAS WRONG:
        that branch replays a PERSISTED STRING and maps to "recorded"
        correctly, which
        `test_provenance_mirrors_the_state_branch_order_in_the_source` pins on
        purpose. It caught the removal. The defect was never the flag, it was
        not checking whether the branch PRODUCED anything.
        """
        first, _second, fixed = self.resolved()
        lr, why = landreq.discharge(first["id"][:12], fixed, "r2 approved")
        self.assertIsNone(why, why)

        # MUST-HIT: the real discharged row records its state and therefore
        # KEEPS its word. Without this the refusals below could be a
        # classifier that stopped answering "recorded" at all.
        self.assertEqual(lr["contrary_state"], "landed")
        self.assertEqual(lr["contrary_provenance"], "recorded")

        # THE CASE ITSELF IS NOT CONSTRUCTIBLE FROM HERE, AND I TRIED TWICE.
        # A review is right that one version of this arm did not
        # discriminate — reverting the conjunction stayed green, which is FALSE
        # PROTECTION and worse than no arm. So rather than leave a decorative
        # assertion, here is exactly what I measured and where it stops:
        #
        #   - dispatches.py sets `discharged=True` and `discharge_contrary`
        #     TOGETHER, guarded by `contrary_state in ("landed","merged-local")`,
        #     so no supported write produces the pair apart;
        #   - `withdraw` is the mirror annotation and sets `withdrawn`, NOT
        #     `discharged`, so it is not the path either;
        #   - `discharged` is absent from `dispatches.snapshot()` rows — it is
        #     added by a later fold — so stripping the field from a snapshot row
        #     and re-projecting exercises nothing. My own MUST-HIT caught that
        #     attempt, which is the second time tonight a must-hit stopped a
        #     decorative arm from shipping.
        #
        # The conjunction is therefore DEFENSIVE against a legacy row shape
        # (discharged before the field existed), and it is unpinned. A probe
        # measured the case, so its reproduction is the missing piece and I
        # have asked for it rather than invent a third fixture.

    def test_discharge_closes_only_the_debt_and_preserves_contrary_history(self):
        first, second, fixed = self.resolved()
        lr, why = landreq.discharge(first["id"][:12], fixed, "r2 approved")
        self.assertIsNone(why)
        self.assertEqual(lr["state"], "CHANGES_REQUESTED")
        self.assertTrue(lr["contrary"] and lr["terminal"])
        self.assertEqual(lr["close_reason"], "superseded")   # via lr close
        self.assertEqual(lr["owed_by"], "nobody")
        self.assertFalse(lr["stalled"])
        self.assertEqual(lr["superseding_tip"], fixed)
        self.assertEqual(lr["superseding_id"], second["id"])
        self.assertEqual(lr["close_evidence"], "r2 approved")
        self.assertEqual(lr["contrary_state"], "landed")
        self.assertEqual([step["state"] for step in lr["timeline"]][-1],
                         "CLOSED_SUPERSEDED")
        frozen = lr["dwell_s"]
        self.assertEqual(landreq.get(first["id"], time.time() + 10000)[0]["dwell_s"],
                         frozen)
        self.assertNotIn(first["id"], [r["id"] for r in landreq.loops()[0]])
        self.assertIn(first["id"], [r["id"] for r in landreq.loops(True)[0]])
        self.assertEqual(landreq.stalls()[0], [])
        self.assertNotIn(first["id"],
                         [r["id"] for r in landreq.board_section()["loops"]])
        shown = run(["show", first["id"][:12]])[1]
        self.assertIn("CONTRARY", shown.upper())
        self.assertIn("CLOSED (SUPERSEDED)", shown)
        self.assertIn(fixed, shown)
        listed = run(["list", "--all"])[1]
        self.assertIn("CLOSED (SUPERSEDED) by %s" % fixed[:12], listed)
        blind = {"observable": False, "local": False, "upstream": False,
                 "has_upstream": False}
        with mock.patch.object(landreq, "_git_observe", return_value=blind):
            replayed = landreq.get(first["id"])[0]
        self.assertTrue(replayed["contrary"])
        self.assertEqual(replayed["close_reason"], "superseded")
        self.assertEqual(replayed["contrary_state"], "landed")
        self.assertEqual(replayed["contrary_target"], "local")

    def test_supersede_contrary_can_be_discharged_without_changing_state(self):
        first, _second, fixed = self.resolved("supersede")
        lr, why = landreq.discharge(first["id"], fixed, "replacement approved")
        self.assertIsNone(why)
        self.assertEqual(lr["state"], "SUPERSEDED")
        self.assertTrue(lr["contrary"] and lr["terminal"])
        self.assertEqual(lr["close_reason"], "superseded")

    def test_identical_retry_is_idempotent_and_conflict_is_refused(self):
        first, _second, fixed = self.resolved()
        one, why = landreq.discharge(first["id"], fixed, "resolved")
        self.assertIsNone(why)
        before = len(dispatches.history(first["id"]))
        two, why = landreq.discharge(first["id"], fixed, "resolved")
        self.assertIsNone(why)
        self.assertEqual(two, one)
        self.assertEqual(len(dispatches.history(first["id"])), before)
        _lr, why = landreq.discharge(first["id"], fixed, "different")
        self.assertIn("retired once", why)

    def test_an_earlier_approve_cannot_launder_a_later_fix(self):
        base = self.git("rev-parse", self.main)
        self.git("checkout", "-q", "-b", "approved-first", base)
        with open(os.path.join(self.repo, "shared"), "w", encoding="utf-8") as f:
            f.write("same patch\n")
        self.git("add", "shared")
        self.git("commit", "-q", "-m", "approved first")
        approved_tip = self.git("rev-parse", "HEAD")
        approved = self.handoff("approved-first", approved_tip)
        self.mark_verdict(approved["id"], approved_tip, "clean",
                                polarity="approve")
        self.git("checkout", "-q", self.main)
        self.git("cherry-pick", approved_tip)

        self.git("checkout", "-q", "-b", "rejected-later", base)
        with open(os.path.join(self.repo, "shared"), "w", encoding="utf-8") as f:
            f.write("same patch\n")
        self.git("add", "shared")
        self.git("commit", "-q", "-m", "rejected later")
        rejected_tip = self.git("rev-parse", "HEAD")
        rejected = self.handoff("rejected-later", rejected_tip)
        self.mark_verdict(rejected["id"], rejected_tip, "new findings",
                                polarity="fix")
        self.git("checkout", "-q", self.main)
        events = eventledger.events(dispatches.ledger_path())
        forged = next(dict(event) for event in events
                      if event.get("id") == rejected["id"]
                      and event.get("event") == "verdict")
        with open(dispatches.ledger_path(), "w", encoding="utf-8") as f:
            for event in [forged] + events:  # pre-genesis duplicate: replay rejects it
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
        self.assertEqual(dispatches.snapshot()[0][rejected["id"]]["status"],
                         "verdict")
        self.assertTrue(landreq.get(rejected["id"])[0]["contrary"])
        self.assertTrue(landreq._landed(
            os.path.realpath(os.path.join(self.repo, ".git")),
            rejected_tip, approved_tip))
        _lr, why = landreq.discharge(rejected["id"], approved_tip,
                                     "old approval")
        self.assertIn("no later APPROVE", why)
        self.assertFalse(landreq.get(rejected["id"])[0]["discharged"])

    def test_concurrent_identical_discharge_reconciles_as_success(self):
        first, second, fixed = self.resolved()
        real = dispatches.snapshot_with_verdicts
        before = len(dispatches.history(first["id"]))
        fired = []

        def race():
            if not fired:
                fired.append(True)
                row, why = dispatches._record_discharge_proven(
                    first["id"], self.side, fixed, second["id"], "resolved",
                    "landed", "local")
                self.assertIsNone(why)
                self.assertTrue(row["discharged"])
            return real()

        with mock.patch.object(dispatches, "snapshot_with_verdicts",
                               side_effect=race):
            lr, why = landreq.discharge(first["id"], fixed, "resolved")
        self.assertIsNone(why)
        self.assertTrue(lr["discharged"])
        self.assertEqual(len(dispatches.history(first["id"])), before + 1)

    def test_discharge_requires_an_approved_superseding_dispatch(self):
        first = self.handoff("feature-r1", self.side)
        self.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        self.git("merge", "--no-edit", "-q", "side")
        descendant = self.commit("unreviewed descendant")
        _lr, why = landreq.discharge(first["id"], descendant, "trust me")
        self.assertIn("no later APPROVE verdict", why)

    def test_gate_capable_ungated_approve_cannot_discharge(self):
        first = self.handoff("feature-r1", self.side)
        self.mark_verdict(
            first["id"], self.side, "findings", polarity="fix")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve without gate", path="g")
        self.git("checkout", "-q", self.main)
        second = self.handoff(
            "feature-r2", fixed, supersedes=first["id"])
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": second["seq"] + 1,
            "id": second["id"], "ts": dispatches.pk.now_ts(),
            "reviewed_tip": fixed, "verdict_ref": "approve without receipt",
            "polarity": "approve", "gate": "",
            "gate_caps": [dispatches.GATE_CAP_RECEIPT]}))
        candidate = landreq.get(second["id"])[0]
        self.assertEqual(candidate["state"], "REVIEWED")
        self.assertIn("PRE-TIER", candidate["ungated"])
        raw = dispatches.snapshot()[0][second["id"]]
        self.assertEqual(landreq.gate_requirement(raw), "required")
        self.assertEqual(raw["gate"], "")
        self.git("merge", "--no-edit", "-q", "side")
        before = len(dispatches.history(first["id"]))
        _lr, why = landreq.discharge(first["id"], fixed, "ungated successor")
        self.assertIn("no later APPROVE", why)
        self.assertEqual(len(dispatches.history(first["id"])), before)

    def test_faulty_binder_missing_token_cannot_discharge_with_current_tier(self):
        """Inject VERIFIED/no-token, then supply an honest bound successor.

        This is a reader backstop against a faulty binder, not a claim that
        normal gate.bind can produce this missing-token shape.
        """
        with mock.patch.object(dispatches, "GATE_CAPS",
                               (dispatches.GATE_CAP_RECEIPT,)), \
                mock.patch.object(gate, "bind", return_value=(
                    "VERIFIED", "", "injected missing token")) as binding:
            first, second, fixed = self.resolved()
        self.assertEqual(binding.call_count, 2)
        candidate = dispatches.snapshot()[0][second["id"]]
        self.assertEqual(dispatches.approval_tier_for_verdict(candidate),
                         ("none", None))
        self.assertEqual(candidate["verdict_version"], 4)
        self.assertIn("no minted gate receipt", landreq._approval_refusal(candidate)[0])
        before = len(dispatches.history(first["id"]))
        out, why = landreq.discharge(first["id"], fixed, "faulty binder")
        self.assertIsNone(out)
        self.assertIn("no later APPROVE", why)
        self.assertEqual(len(dispatches.history(first["id"])), before)
        third = self.handoff("feature-r3", fixed, supersedes=second["id"])
        with mock.patch.object(dispatches, "GATE_CAPS",
                               (dispatches.GATE_CAP_RECEIPT,)), \
                mock.patch.object(gate, "bind", return_value=(
                    "VERIFIED", "a" * 16, "test receipt")) as binding:
            got, why = self.mark_verdict(third["id"], fixed, "bound successor",
                                          polarity="approve")
        self.assertIsNone(why, why)
        binding.assert_called_once()
        self.assertEqual(landreq._approval_refusal(got), (None, "none"))
        out, why = landreq.discharge(first["id"], fixed, "bound successor")
        self.assertIsNone(why, why)
        self.assertEqual(out["close_reason"], "superseded")

    def test_gate_capable_bound_approve_can_discharge(self):
        with mock.patch.object(
                dispatches, "GATE_CAPS", (dispatches.GATE_CAP_RECEIPT,)), \
                mock.patch.object(
                    dispatches.gate, "bind",
                    return_value=("VERIFIED", "a" * 16, "test receipt")):
            first, _second, fixed = self.resolved()
        lr, why = landreq.discharge(first["id"], fixed, "gated successor")
        self.assertIsNone(why)
        self.assertEqual(lr["close_reason"], "superseded")

    def forge_open(self, rid, drop=(), **fields):
        """Rewrite one row's opening dispatch event, the way a hand-edit or a
        pre-chain writer would look to replay. Real code never rewrites the
        append-only ledger; this is how legacy and corrupt rows get in front
        of it (the `rewrite` idiom in tests/test_dispatch_chain.py)."""
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for event in events:
                if event.get("id") == rid and event.get("event") == "dispatch":
                    for key in drop:
                        event.pop(key, None)
                    event.update(fields)
                f.write(json.dumps(event, separators=(",", ":")) + "\n")

    def legacy(self, row):
        """Strip a row's chain fields so replay reads it as a pre-chain LEGACY
        row — the shape of everything on the live ledger before chains."""
        self.forge_open(row["id"], drop=("chain_root", "supersedes"))

    def test_a_successor_approve_on_the_authors_chain_pays_the_debt(self):
        """THE LANE-HANDOFF ADMIT, chain leg. A lane that changed hands
        mid-life (author -> successor integrator) used to leave its contrary
        debt payable by nobody: the successor's approve failed only the flat
        sender==author clause. The successor's round names the author's row as
        its parent, so the handoff is PROVEN by the record and the debt is
        payable."""
        first = self.handoff("feature-r1", self.side)
        self.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve by the successor", path="g")
        self.git("checkout", "-q", self.main)
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        second = self.handoff("feature-r2", fixed, supersedes=first["id"])
        self.mark_verdict(second["id"], fixed, "clean", polarity="approve")
        os.environ["HELM_CHAT_NAME"] = "integrator"
        self.git("merge", "--no-edit", "-q", "side")
        lr, why = landreq.discharge(first["id"], fixed, "handed-off successor")
        self.assertIsNone(why)
        # Delegation truth: the alias routes through close, whose event
        # records close_reason — the discharged flag belongs to legacy
        # replay alone.
        self.assertEqual(lr["close_reason"], "superseded")
        self.assertEqual(lr["superseding_id"], second["id"])
        self.assertEqual(lr["superseding_tip"], fixed)

    def test_legacy_same_stem_recorded_handoff_admits(self):
        """THE LANE-HANDOFF ADMIT, legacy leg. Chainless rows cannot prove a
        handoff by ancestry, so the proof is the lane family record itself:
        same stem family AND the successor visibly dispatched in that family
        beyond the candidate approve — the lane changed hands in the record."""
        first = self.handoff("resolve-thing", self.side)
        self.legacy(first)
        self.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve by the successor", path="g")
        self.git("checkout", "-q", self.main)
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        # The recorded handoff: the successor demonstrably WORKS this lane
        # family (a second dispatch of its own), not one drive-by approve.
        self.handoff("resolve-thing-review", self.c)
        second = self.handoff("resolve-thing-r2", fixed)
        self.legacy(second)
        self.mark_verdict(second["id"], fixed, "clean", polarity="approve")
        os.environ["HELM_CHAT_NAME"] = "integrator"
        self.git("merge", "--no-edit", "-q", "side")
        lr, why = landreq.discharge(first["id"], fixed, "handed-off successor")
        self.assertIsNone(why)
        # Delegation truth: the alias routes through close, whose event
        # records close_reason — the discharged flag belongs to legacy
        # replay alone.
        self.assertEqual(lr["close_reason"], "superseded")
        self.assertEqual(lr["superseding_id"], second["id"])

    def test_lane_prefix_is_family_to_its_bare_stem(self):
        """#156: `_lane_stem` stripped the -rN suffix but NOT the lane/
        prefix, so a family recorded BOTH ways (142: 798 bare rows / 82
        lane/-prefixed) silently failed the handoff family match. One named
        normalisation strips BOTH, every reader and writer alike."""
        self.assertEqual(landreq._lane_stem("lane/resolve-thing-r2"),
                         landreq._lane_stem("resolve-thing"))
        self.assertEqual(landreq._lane_stem("lane/g-review-r2"),
                         landreq._lane_stem("g"))
        # A prefix alone is not membership: a genuinely different stem must
        # still not match.
        self.assertNotEqual(landreq._lane_stem("lane/other-thing"),
                            landreq._lane_stem("resolve-thing"))

    def test_legacy_handoff_admits_across_the_lane_prefix_split(self):
        """The escape hatch BITES here: a legacy contrary recorded bare and
        its successor rounds recorded lane/-prefixed are ONE work family —
        but the family match saw 'resolve-thing' vs 'lane/resolve-thing' and
        refused, exactly the silent failure #142's split guarantees. After
        the fix the recorded handoff admits across the spelling split."""
        first = self.handoff("resolve-thing", self.side)
        self.legacy(first)
        self.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve by the successor", path="g")
        self.git("checkout", "-q", self.main)
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        # Same family, OTHER spelling: the successor visibly works the
        # lane/-prefixed name of the contrary's bare stem.
        self.handoff("lane/resolve-thing-review", self.c)
        second = self.handoff("lane/resolve-thing-r2", fixed)
        self.legacy(second)
        self.mark_verdict(second["id"], fixed, "clean", polarity="approve")
        os.environ["HELM_CHAT_NAME"] = "integrator"
        self.git("merge", "--no-edit", "-q", "side")
        lr, why = landreq.discharge(first["id"], fixed, "handed-off successor")
        self.assertIsNone(why)
        self.assertEqual(lr["close_reason"], "superseded")
        self.assertEqual(lr["superseding_id"], second["id"])

    def test_legacy_chain_leg_admits_only_an_author_row_in_family(self):
        """On a LEGACY contrary no chain binds the walk to the debt, so the
        author row the walk reaches must itself sit in the contrary's lane
        family. The sender here has NO second dispatch in the family — the
        legacy leg refuses it — so the in-family walk is the single admitting
        proof, and this test measures exactly it."""
        first = self.handoff("resolve-thing", self.side)
        self.legacy(first)
        self.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        # The author's own chained round IN the contrary's family.
        bridge = self.handoff("resolve-thing-r2", self.c)
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve by the successor", path="g")
        self.git("checkout", "-q", self.main)
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        second = self.handoff("resolve-thing-r3", fixed, supersedes=bridge["id"])
        self.mark_verdict(second["id"], fixed, "clean", polarity="approve")
        os.environ["HELM_CHAT_NAME"] = "integrator"
        self.git("merge", "--no-edit", "-q", "side")
        lr, why = landreq.discharge(first["id"], fixed, "in-family chain")
        self.assertIsNone(why)
        # Delegation truth: the alias routes through close, whose event
        # records close_reason — the discharged flag belongs to legacy
        # replay alone.
        self.assertEqual(lr["close_reason"], "superseded")
        self.assertEqual(lr["superseding_id"], second["id"])

    def test_legacy_debt_refuses_a_chain_to_the_authors_unrelated_row(self):
        """THE HOLE THE REPAIR CLOSES. A candidate chained to ANY unrelated
        row of the author's used to pay ANY legacy debt of that author — the
        walk proved the author's NAME, not the author's WORK. Out-of-family
        ancestry now refuses, and the refusal names the failed handoff."""
        first = self.handoff("feature-r1", self.side)
        self.legacy(first)
        self.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        # The author's UNRELATED row — a different lane family entirely.
        unrelated = self.handoff("proxy-oauth-mint", self.a)
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve elsewhere", path="g")
        self.git("checkout", "-q", self.main)
        os.environ["HELM_CHAT_NAME"] = "drive-by"
        second = self.handoff("totally-else", fixed, supersedes=unrelated["id"])
        self.mark_verdict(second["id"], fixed, "clean", polarity="approve")
        os.environ["HELM_CHAT_NAME"] = "integrator"
        self.git("merge", "--no-edit", "-q", "side")
        _lr, why = landreq.discharge(first["id"], fixed, "unrelated chain")
        self.assertIn("no later APPROVE", why)
        self.assertIn("no cross-sender candidate proved a lane handoff", why)
        self.assertIn(second["id"][:12], why)
        self.assertFalse(landreq.get(first["id"])[0]["discharged"])

    def test_an_unrelated_seat_approve_in_a_different_lane_family_refuses(self):
        """A cross-sender approve OUTSIDE the contrary's lane family proves no
        handoff — and the refusal now names BOTH failed legs, so the next
        integrator does not re-diagnose from scratch."""
        first = self.handoff("resolve-thing", self.side)
        self.legacy(first)
        self.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve elsewhere", path="g")
        self.git("checkout", "-q", self.main)
        os.environ["HELM_CHAT_NAME"] = "drive-by"
        second = self.handoff("unrelated-lane", fixed)
        self.mark_verdict(second["id"], fixed, "clean", polarity="approve")
        os.environ["HELM_CHAT_NAME"] = "integrator"
        self.git("merge", "--no-edit", "-q", "side")
        _lr, why = landreq.discharge(first["id"], fixed, "drive-by approve")
        self.assertIn("no later APPROVE", why)
        self.assertIn("no cross-sender candidate proved a lane handoff", why)
        self.assertIn("outside the contrary's lane family", why)
        self.assertIn(second["id"][:12], why)
        self.assertFalse(landreq.get(first["id"])[0]["discharged"])

    def test_an_unreadable_chain_refuses_naming_the_unprovable_leg(self):
        """TRI-STATE, fail-closed: a candidate whose supersedes link is corrupt
        cannot prove the handoff, and the refusal says exactly which leg was
        unprovable rather than a bare conjunction."""
        first = self.handoff("feature-r1", self.side)
        self.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve with a corrupt link", path="g")
        self.git("checkout", "-q", self.main)
        os.environ["HELM_CHAT_NAME"] = "other-author"
        second = self.handoff("feature-r2", fixed, supersedes=first["id"])
        self.mark_verdict(second["id"], fixed, "clean", polarity="approve")
        os.environ["HELM_CHAT_NAME"] = "integrator"
        # Corrupt ONLY the parent link; the chain_root still matches, so the
        # handoff walk is the single thing standing between this and a
        # discharge — exactly the leg this test measures.
        self.forge_open(second["id"], supersedes="not-a-chain-id")
        self.git("merge", "--no-edit", "-q", "side")
        _lr, why = landreq.discharge(first["id"], fixed, "corrupt link")
        self.assertIn("no cross-sender candidate proved a lane handoff", why)
        self.assertIn("UNPROVABLE", why)
        self.assertIn(second["id"][:12], why)
        self.assertFalse(landreq.get(first["id"])[0]["discharged"])

    def test_a_forked_approve_by_an_unrelated_seat_cannot_discharge(self):
        """THE ADMIT IS NOT A LOOSENING. On a CHAINED contrary the ancestry is
        the only proof: a fork off the same chain by a seat whose walk never
        reaches a row of the debt's author still refuses — same lane family,
        same chain root, still not a handoff from THIS author."""
        os.environ["HELM_CHAT_NAME"] = "seat-x"
        root = self.handoff("feature-r0", self.a)
        os.environ["HELM_CHAT_NAME"] = "integrator"
        first = self.handoff("feature-r1", self.side, supersedes=root["id"])
        self.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve on a fork", path="g")
        self.git("checkout", "-q", self.main)
        os.environ["HELM_CHAT_NAME"] = "other-author"
        # A FORK off the root — legitimate chain membership, but its walk
        # (second -> root) contains no row authored by `integrator`.
        second = self.handoff(
            "feature-r2", fixed, supersedes=root["id"], force=True)
        self.mark_verdict(second["id"], fixed, "clean", polarity="approve")
        os.environ["HELM_CHAT_NAME"] = "integrator"
        self.git("merge", "--no-edit", "-q", "side")
        _lr, why = landreq.discharge(first["id"], fixed, "forked approve")
        self.assertIn("same author/repo", why)
        self.assertIn("no cross-sender candidate proved a lane handoff", why)
        self.assertIn("without a row authored by integrator", why)
        self.assertFalse(landreq.get(first["id"])[0]["discharged"])

    def test_an_approve_from_a_different_repo_cannot_discharge(self):
        first = self.handoff("feature-r1", self.side)
        self.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve for clone", path="g")
        self.git("checkout", "-q", self.main)
        self.git("merge", "--no-edit", "-q", "side")
        other_repo = os.path.join(self.tmp, "other-repo")
        subprocess.run(["git", "clone", "-q", self.repo, other_repo], check=True)
        # ON THE CHAIN ON PURPOSE, same reason as the author test above: an
        # unlinked round would be rejected by the chain gate too, and two checks
        # rejecting one input measure neither. The current writer correctly
        # refuses a repo-B child of a repo-A parent, so mint the chain leg under
        # repo A and inject the historical foreign binding below that door.
        second = self.handoff(
            "other-repo-r2", fixed, supersedes=first["id"])
        self.forge_open(
            second["id"],
            repo_id=dispatches._repo_info(other_repo)["repo_id"],
            repo_root=other_repo)
        second = dispatches.rows()[second["id"]]
        self.mark_verdict(second["id"], fixed, "clean", polarity="approve")
        _lr, why = landreq.discharge(first["id"], fixed, "wrong repo")
        self.assertIn("same author/repo", why)

    def test_unrelated_approved_tip_cannot_launder_the_contrary(self):
        """UNRELATED WORK, and the ledger now says so out loud. `other-r2` is a
        separate chain, so it is not a candidate at all — which is the whole
        repair: an approve of different work used to have to be caught by Git
        lineage AFTER being accepted as a candidate, and any later approved
        trunk commit trivially contains a change already on trunk."""
        first = self.handoff("feature-r1", self.side)
        self.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        other = self.handoff("other-r2", self.c)
        self.mark_verdict(other["id"], self.c, "clean", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        _lr, why = landreq.discharge(first["id"], self.c, "unrelated")
        self.assertIn("SAME WORK CHAIN", why)
        self.assertFalse(landreq.get(first["id"])[0]["discharged"])

    def test_a_chained_round_still_needs_git_lineage(self):
        """THE OTHER HALF, bound separately. Naming the parent is necessary and
        NOT sufficient: a round that really is on this chain but whose tip does
        not contain the reviewed change is still refused, by Git. Without this
        test the lineage check would be unmeasured — the chain gate above
        rejects its input first, so reverting lineage would leave that test
        green."""
        first = self.handoff("feature-r1", self.side)
        self.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        second = self.handoff("feature-r2", self.c, supersedes=first["id"])
        self.mark_verdict(second["id"], self.c, "clean", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        _lr, why = landreq.discharge(first["id"], self.c, "same chain, wrong tip")
        self.assertIn("does not contain", why)
        self.assertFalse(landreq.get(first["id"])[0]["discharged"])

    def test_unknown_git_proof_refuses_without_appending(self):
        first, _second, fixed = self.resolved()
        real = landreq._landed

        def unknown(gitdir, tip, ref):
            if tip == self.side and ref == fixed:
                return None
            return real(gitdir, tip, ref)
        before = len(dispatches.history(first["id"]))
        with mock.patch.object(landreq, "_landed", side_effect=unknown):
            _lr, why = landreq.discharge(first["id"], fixed, "resolved")
        self.assertIn("could not prove", why)
        self.assertEqual(len(dispatches.history(first["id"])), before)

    def test_the_original_tip_cannot_discharge_itself(self):
        first = self.handoff("feature-r1", self.side)
        self.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        self.git("merge", "--no-edit", "-q", "side")
        _lr, why = landreq.discharge(first["id"], self.side, "same")
        self.assertIn("cannot supersede itself", why)

    def test_stale_tracking_ref_without_origin_cannot_override_local_authority(self):
        first = self.handoff("feature-r1", self.side)
        self.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        self.git("merge", "--no-edit", "-q", "side")
        self.git("checkout", "-q", "-b", "resolution", self.side)
        fixed = self.commit("resolved but not landed locally", path="g")
        second = self.handoff("feature-r2", fixed, supersedes=first["id"])
        self.mark_verdict(second["id"], fixed, "clean", polarity="approve")
        self.git("checkout", "-q", self.main)
        self.git("update-ref", "refs/remotes/origin/main", fixed)
        self.assertFalse(self.git("remote"))
        self.assertTrue(landreq.get(first["id"])[0]["contrary"])
        _lr, why = landreq.discharge(first["id"], fixed, "stale ref")
        self.assertIn("authoritative trunk", why)
        self.assertFalse(landreq.get(first["id"])[0]["discharged"])

    def test_configured_origin_without_a_tracking_ref_is_unknown_not_local(self):
        bare = os.path.join(self.tmp, "unfetched-origin.git")
        subprocess.run(["git", "init", "--bare", "-q", bare], check=True)
        self.git("push", "-q", bare, "%s:main" % self.main)
        self.git("remote", "add", "origin", bare)
        with self.assertRaises(subprocess.CalledProcessError):
            self.git("rev-parse", "--verify", "refs/remotes/origin/main")
        first, _second, fixed = self.resolved()
        _lr, why = landreq.discharge(first["id"], fixed, "not remotely observed")
        self.assertIn("tracking ref", why)
        self.assertFalse(landreq.get(first["id"])[0]["discharged"])

    def test_superseding_tip_must_reach_authoritative_upstream(self):
        first, _second, fixed = self.resolved(upstream=True, push=False)
        _lr, why = landreq.discharge(first["id"], fixed, "not pushed")
        self.assertIn("authoritative trunk", why)
        self.git("push", "-q", "origin", self.main)
        lr, why = landreq.discharge(first["id"], fixed, "pushed")
        self.assertIsNone(why)
        self.assertEqual(lr["close_reason"], "superseded")
        self.assertEqual(lr["contrary_target"], "upstream")
        moved = {"observable": True, "local": True, "upstream": False,
                 "has_upstream": True}
        with mock.patch.object(landreq, "_git_observe", return_value=moved):
            historical = landreq.get(first["id"])[0]
            shown = landreq._render_show(historical)
        self.assertEqual(historical["contrary_state"], "landed")
        self.assertEqual(historical["contrary_target"], "upstream")
        self.assertIn("LANDED on upstream trunk", shown)
        self.assertNotIn("MERGED_LOCAL on upstream trunk", shown)

    def test_patch_equivalent_lineage_is_accepted_when_ancestry_is_false(self):
        first = self.handoff("feature-r1", self.side)
        self.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        self.git("checkout", "-q", "-b", "resolution", self.main)
        self.git("cherry-pick", self.side)
        fixed = self.commit("fix on cherry-picked base", path="g")
        second = self.handoff("feature-r2", fixed, supersedes=first["id"])
        self.mark_verdict(second["id"], fixed, "clean", polarity="approve")
        self.git("checkout", "-q", self.main)
        self.git("merge", "--no-edit", "-q", "resolution")
        gitdir = os.path.realpath(os.path.join(self.repo, ".git"))
        self.assertFalse(landreq._is_ancestor(gitdir, self.side, fixed))
        self.assertTrue(landreq._landed(gitdir, self.side, fixed))
        lr, why = landreq.discharge(first["id"], fixed, "patch-equivalent")
        self.assertIsNone(why)
        self.assertEqual(lr["close_reason"], "superseded")

    def test_cli_discharge_uses_short_id_and_refuses_unknown_flags(self):
        first, _second, fixed = self.resolved()
        rc, out, err = run(["discharge", first["id"][:12], fixed,
                            "r2", "approved", "--json"])
        self.assertEqual(rc, 0, err)
        self.assertIn("deprecated: use helm lr close --reason superseded", err)
        self.assertEqual(json.loads(out)["close_reason"], "superseded")
        rc, _out, err = run(["discharge", first["id"], fixed,
                             "again", "--wat"])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)


class ContraryProvenanceNamesItsEvidenceTest(_landreq.LandReqBase):
    """`contrary_state` says WHAT happened; `contrary_provenance` says HOW WE KNOW.

    Two branches of `contrary_state` emit the identical string "landed" from
    entirely different evidence — a live git observation, and a string recorded
    at close time that may never be re-checkable. Measured across 2223 real
    rows: 418 contrary rows carry it from an observation, 135 from the stored
    `close_contrary_state`, and every one of those 135 has `observable` False,
    so git could not be consulted for them at all. The fixtures below are built
    from the shapes that census found: a live-observed contrary closes with NO
    close reason (411 of 418), and a recorded one closes `superseded` (135 of
    135).
    """

    def rounds(self, close_as_superseded, tag):
        """A FIX round resolved by a later approved round, the fix's own tip
        then reaching trunk — the contrary shape.

        `tag` keys every dispatch and commit apart, because an arm needs BOTH
        provenances at once and a repeated dispatch key is deduplicated rather
        than sent.
        """
        first, why, sent = dispatches.send(
            "seat-under-test", "prov-r1-" + tag, "review r1", self.side,
            repo=self.repo, key="key-prov-r1-" + tag, sign=False,
            new_work=True)
        self.assertIsNone(why)
        self.assertTrue(sent, "the first round was deduplicated, not sent")
        self.mark_verdict(first["id"], self.side, "findings",
                                polarity="fix")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve findings " + tag, path="g-" + tag)
        self.git("checkout", "-q", self.main)
        second, why, sent = dispatches.send(
            "seat-under-test", "prov-r2-" + tag, "review r2", fixed, repo=self.repo,
            key="key-prov-r2-" + tag, sign=False, supersedes=first["id"])
        self.assertIsNone(why)
        self.assertTrue(sent, "the second round was deduplicated, not sent")
        self.mark_verdict(second["id"], fixed, "re-probed clean",
                                polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        if close_as_superseded:
            out, err = landreq.close(first["id"], "superseded",
                                     evidence="r2 approved", tip=fixed)
            self.assertIsNone(err)
            return out
        row, err = landreq.get(first["id"])
        self.assertIsNone(err)
        return row

    def test_a_live_observation_and_a_recorded_string_differ_in_provenance_alone(self):
        """THE ARM THIS FIELD EXISTS FOR.

        Both rows must still say "landed" — that collapse is deliberate and
        load-bearing, because `contrary_state` is an ENFORCEMENT input.
        What must differ is the provenance. An implementation emitting a
        constant, or mirroring `contrary_state`, satisfies every other
        assertion here and fails the last one.
        """
        observed = self.rounds(False, "obs")
        recorded = self.rounds(True, "rec")

        self.assertTrue(observed["contrary"], "the observed row is not contrary")
        self.assertTrue(recorded["contrary"], "the recorded row is not contrary")
        self.assertEqual(observed["contrary_state"], "landed")
        self.assertEqual(recorded["contrary_state"], "landed",
                         "the two states must stay IDENTICAL — a diverging "
                         "state is a refusal regression, not a fix")
        self.assertEqual(observed["contrary_provenance"], "observed")
        self.assertEqual(recorded["contrary_provenance"], "recorded")
        self.assertNotEqual(observed["contrary_provenance"],
                            recorded["contrary_provenance"],
                            "two different kinds of evidence still render "
                            "identically, which is the whole defect")

    def test_provenance_mirrors_the_state_branch_order_in_the_source(self):
        """THE MIRRORING IS THE CONTRACT, AND NO FIXTURE CAN REACH IT.

        `contrary_state`'s first branch is fed by a raw `discharged` field that
        `landreq.discharge` can no longer set: it is DEPRECATED and maps onto
        `close --reason superseded` with, in its own words, "no second code
        path". The branch survives only for legacy rows already in the ledger
        (27 in the live estate), so no supported verb can produce one.

        The invariant is therefore guarded structurally: both expressions must
        test `discharged` FIRST and `close_contrary_state` SECOND. Editing one
        branch order and not the other is exactly how the provenance would come
        to describe a different branch than the one that produced the value.

        THE OTHER PRECEDENCE CASE IS UNREACHABLE TOO, and that is measured
        rather than assumed. A fixture arm was written for "a recorded row
        whose tip ALSO landed", to prove the provenance names the branch that
        ANSWERED rather than the strongest evidence available. Its own control
        refused it: after `close --reason superseded` the row reports neither
        `landed` nor `merged_local`, so the collision never forms. That agrees
        with the estate — all 135 recorded rows carry `observable` False, so
        git cannot be consulted for them and no live landing can compete with
        the stored string. The arm was DELETED rather than weakened, because
        an arm whose premise cannot occur proves nothing about the branch it
        names, and this structural check covers the ordering it was after.
        """
        import ast as _ast
        with open(landreq.__file__, encoding="utf-8") as _fh:
            src = _fh.read()
        tree = _ast.parse(src)
        found = {}
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Assign) or len(node.targets) != 1:
                continue
            name = getattr(node.targets[0], "id", None)
            if name not in ("contrary_state", "contrary_provenance"):
                continue
            if not isinstance(node.value, _ast.IfExp):
                continue
            conds, cur = [], node.value
            while isinstance(cur, _ast.IfExp):
                conds.append(_ast.get_source_segment(src, cur.test) or "")
                cur = cur.orelse
            found.setdefault(name, conds)

        # MUST-HIT: a walk that found nothing must not read as agreement.
        self.assertIn("contrary_state", found, "never found contrary_state")
        self.assertIn("contrary_provenance", found,
                      "never found contrary_provenance")
        # contrary_state keeps its ORDER: discharged, then the recorded string.
        state = found["contrary_state"]
        self.assertGreaterEqual(len(state), 3,
                                "contrary_state has too few branches to compare")
        self.assertEqual(state[0], "discharged",
                         "contrary_state no longer tests discharged first")
        self.assertIn("close_contrary_state", state[1],
                      "contrary_state no longer tests the recorded string second")

        # PROVENANCE HAS FEWER BRANCHES THAN THE STATE IT ANNOTATES, so the
        # invariant is a MAPPING rather than a matching order (codex, round 1:
        # `discharged` was a lifecycle label in an evidence field). Both of
        # contrary_state's first two branches replay a PERSISTED string, so both
        # must land in the SAME recorded branch here; anything git actually
        # observed must land in the other. Re-ordering contrary_state without
        # re-deriving this mapping is exactly how the two would come apart.
        prov = found["contrary_provenance"]
        self.assertGreaterEqual(len(prov), 2,
                                "contrary_provenance has too few branches")
        self.assertIn("discharged", prov[0],
                      "the recorded branch no longer covers the discharge path")
        self.assertIn("close_contrary_state", prov[0],
                      "the recorded branch no longer covers the close path")
        self.assertTrue("landed" in prov[1] or "merged_local" in prov[1],
                        "the observed branch is no longer fed by a live "
                        "landing: %r" % (prov[1],))

    def test_a_real_contrary_row_carries_its_provenance_into_lr_list(self):
        """THE ARTIFACT, NOT THE HELPER. `contrary_provenance_clause` being
        correct in isolation says nothing about whether any operator ever sees
        it — that gap is the whole of codex's blocker (1). This renders a real
        contrary row through the real `_line` and reads the output.
        """
        observed = self.rounds(False, "render-obs")
        # GUARD ON THE FIELD, NOT THE RENDERED WORD. A contrary row has
        # SEVERAL legitimate renderings — plain "CONTRARY:", "CONTRARY?" when
        # succession is unverified, "CONFIRMATION:" for the discharge
        # instrument, and "SUPERSEDED-CLOSED:" when the verdict was honored
        # through succession, which is what THIS fixture produces. An earlier
        # cut of this arm asserted the word "CONTRARY" appears and failed on a
        # row that was contrary the whole time: I asserted the rendering when
        # I meant the state.
        self.assertTrue(observed["contrary"],
                        "fixture stopped being contrary, so this arm proves "
                        "nothing about contrary rendering")
        line = landreq._line(observed)
        self.assertIn("[observed on trunk]", line,
                      "an operator reading `lr list` still cannot tell a "
                      "verified landing from a recorded string")
        # THE PHRASE THE SPLICED VERSION BROKE, pinned so the regression cannot
        # return quietly: the provenance is a TRAILING clause, so the fact and
        # everything after it stay contiguous. Splicing it back into the fact
        # would cut this phrase in half.
        self.assertIn("LANDED, and the FIX verdict was HONORED", line,
                      "provenance was spliced into the fact again — the fact "
                      "and its following clause must stay contiguous")
        self.assertTrue(line.rstrip().endswith("[observed on trunk])")
                        or "[observed on trunk]" in line.split("(attest")[0],
                        "the provenance clause is not trailing its mark")

    def test_a_row_that_is_not_contrary_claims_no_provenance(self):
        row, why, sent = dispatches.send(
            "seat-under-test", "prov-open", "review open", self.side, repo=self.repo,
            key="key-prov-open", sign=False, new_work=True)
        self.assertIsNone(why)
        self.assertTrue(sent)
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err)
        # THE POSITIVE CONTROL: the key is PRESENT and merely empty, so an
        # absent field cannot masquerade as "no provenance".
        self.assertIn("contrary_provenance", lr,
                      "the field vanished rather than answering None")
        self.assertFalse(lr["contrary"])
        self.assertIsNone(lr["contrary_provenance"])
        self.assertIsNone(lr["contrary_state"])

    def test_the_enforcement_literals_are_untouched_by_the_new_field(self):  # noqa: VACUOUS_ASSERTION — this arm IS the intentional negative (provenance must not enter the state vocabulary); its positive half asserts both states are IN the enforcement set and both provenances are IN their own
        """THE REGRESSION GUARD FOR THE DESIGN CONSTRAINT.

        `contrary_state` is read as an enforcement input, not merely rendered:
        `dispatches` refuses a discharge whose state is not in
        ("landed", "merged-local") and matches `discharge_contrary` against it
        exactly. This pins that the provenance did NOT become a new value of
        that vocabulary — the change that would have turned a labelling fix
        into a refusal regression.
        """
        observed = self.rounds(False, "enf-obs")
        recorded = self.rounds(True, "enf-rec")
        self.assertIn(observed["contrary_state"], ("landed", "merged-local"),
                      "the observed state left the enforcement vocabulary")
        self.assertIn(recorded["contrary_state"], ("landed", "merged-local"),
                      "the recorded state left the enforcement vocabulary")
        # THE POSITIVE HALF ON THE SAME OBSERVABLE: provenance must land in
        # ITS OWN vocabulary, not merely stay out of the state's. Absence
        # alone is satisfied by None, which would say nothing at all.
        self.assertIn(observed["contrary_provenance"],
                      ("observed", "recorded", "discharged"),
                      "the observed provenance is outside its own vocabulary")
        self.assertIn(recorded["contrary_provenance"],
                      ("observed", "recorded", "discharged"),
                      "the recorded provenance is outside its own vocabulary")
        self.assertNotIn(observed["contrary_provenance"],
                         ("landed", "merged-local"),
                         "provenance leaked into the state vocabulary")
        self.assertNotIn(recorded["contrary_provenance"],
                         ("landed", "merged-local"),
                         "provenance leaked into the state vocabulary")


class ContraryProvenanceReachesTheOperatorTest(_landreq.LandReqBase):
    """The renderers, because a recorded field nobody can read is half a change.

    codex returned FIX on the first round with exactly two blockers: the human
    renderers dropped the new field, and a routine landed approve appeared to
    receive `observed` provenance despite contrary=false. Both are arms here.

    THE SECOND ONE TRACES UPSTREAM, and the arm says so rather than hiding it:
    `contrary` requires a fix/supersede polarity, while `contrary_state` has a
    fourth branch (`else "landed" if landed`) that `contrary` does not. So a
    routine approve-then-land row ALREADY reads contrary=False with
    contrary_state="landed", on trunk, before provenance existed. Provenance
    mirrors the branch that answered — that is its contract — so it is
    populated there too. The cure is not to break the mirror but to render
    both only where the alarm is, which every consumer already did implicitly.
    """

    def _approved_and_landed(self):
        """A ROUTINE row: approve verdict, reviewed tip reaches trunk."""
        row, why, sent = dispatches.send(
            "seat-under-test", "prov-routine", "review routine", self.side,
            repo=self.repo, key="key-prov-routine", sign=False, new_work=True)
        self.assertIsNone(why)
        self.assertTrue(sent, "the routine round was deduplicated, not sent")
        self.mark_verdict(row["id"], self.side, "clean",
                                polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err)
        return lr

    def test_a_routine_landed_approve_renders_no_provenance(self):
        """codex's blocker (2), as a test rather than as a promise."""
        lr = self._approved_and_landed()
        self.assertFalse(lr["contrary"],
                         "fixture is not routine — it is a contrary row, so "
                         "this arm would prove nothing about routine rows")
        # THE PRE-EXISTING OVER-POPULATION, asserted rather than described, so
        # that if someone later narrows contrary_state this arm says so loudly
        # instead of silently becoming vacuous.
        self.assertEqual(lr["contrary_state"], "landed",
                         "contrary_state is no longer populated on a routine "
                         "landed row — the upstream shape this arm is built "
                         "on has changed and the reasoning needs re-doing")
        line = landreq._line(lr)
        for word in ("observed on trunk", "recorded when closed"):
            self.assertNotIn(word, line,
                             "a routine landed approve rendered provenance "
                             "(%r) — the alarm gate is not holding" % word)

        # POSITIVE CONTROL ON THE SAME OBSERVABLE, unconditional, flagged by
        # helm's vacuous-assertion rung and it was right: every assertion above
        # is an ABSENCE, so a `_line` that renders provenance for NO row — or
        # returns an empty string — passes this arm with the gate doing
        # nothing. The same row through the same door, with the field set, must
        # produce the word; that is what makes its absence above attributable.
        primed = dict(lr, contrary=True, contrary_provenance="observed")
        self.assertIn(
            "observed on trunk", landreq._line(primed),
            "_line emits no provenance for ANY row, so the absences asserted "
            "above say nothing about the routine-row gate")

    def test_each_provenance_word_reaches_the_operator(self):
        """THE CURE FOR BLOCKER (1). Every value renders, through one door."""
        for value, word in (("observed", "observed on trunk"),
                            ("recorded", "recorded when closed")):
            self.assertEqual(
                landreq.contrary_provenance_clause(
                    {"contrary_provenance": value}),
                " [%s]" % word,
                "provenance %r did not reach the operator" % value)
        # POSITIVE CONTROL ON THE OTHER SIDE: absent provenance renders NOTHING,
        # so the arms above cannot be satisfied by an unconditional suffix that
        # appends words to every row alike.
        self.assertEqual(landreq.contrary_provenance_clause({}), "",
                         "a row with no provenance still gained a clause")
        # AND THE FACT STAYS UNSPLIT — the regression that reddened ten arms.
        self.assertEqual(
            landreq.contrary_fact({"contrary_state": "merged-local",
                                   "contrary_provenance": "observed"}),
            "MERGED_LOCAL",
            "provenance leaked back into the fact, which breaks every "
            "renderer phrase that reads '<FACT> on <target>'")

    def test_an_unreadable_state_says_so_instead_of_claiming_merged_local(self):
        """AN IN-PASS FIX FOUND WHILE WIRING THE RENDERERS, same law as the lane.

        Both terminal renderers read `"LANDED" if state == "landed" else
        "MERGED_LOCAL"`, so a row whose contrary_state could not be read
        asserted MERGED_LOCAL — a physical claim nobody measured. It is
        reachable: a discharged row whose `discharge_contrary` is unset yields
        contrary=True with contrary_state=None. The web card already said
        "STATE UNREAD", so the two surfaces described such a row DIFFERENTLY,
        which is precisely what this lane exists to end.
        """
        self.assertEqual(landreq.contrary_fact({"contrary_state": None}),
                         "STATE UNREAD",
                         "an unreadable state still claims a landing shape")
        self.assertEqual(
            landreq.contrary_fact({"contrary_state": "merged-local"}),
            "MERGED_LOCAL",
            "the real merged-local case regressed while fixing the unread one")

    def test_a_warm_cached_row_without_the_field_renders_the_old_way(self):
        """codex asked for a SCHEMA VERSION so a stale warm body cannot emit
        rows missing the new field. I am answering the RISK instead of adding
        the version, and this arm is the answer.

        The warm projection is bounded by `_WARM_QUIET_HOUR_S` (3600, PINNED to
        the hour its contract names), so a body predating a deploy can only be
        served for an hour. In that window a row simply LACKS the key — and
        every reader here uses `.get()`, so the clause is empty and the line
        renders exactly as it did before the field existed. Absence degrades to
        the OLD behaviour, never to a wrong claim, which is the property that
        makes a version unnecessary rather than merely unimplemented.

        A version would still be an improvement, and it is NOT free: the warm
        body is a shared caching seam other lanes read, so versioning it is
        their change to bless, not a labelling lane's to make. What a labelling
        lane owes is proof that its own absence is harmless. That is this.
        """
        legacy = {"contrary_state": "landed"}          # no contrary_provenance
        self.assertEqual(landreq.contrary_provenance_clause(legacy), "",
                         "a pre-field cached row gained a provenance clause "
                         "out of nowhere")
        self.assertEqual(landreq.contrary_fact(legacy), "LANDED",
                         "a pre-field cached row stopped rendering its fact")
        # AND AN EXPLICIT None, which is what every legacy row in the ledger
        # carries once the field exists — distinct from the key being absent.
        self.assertEqual(
            landreq.contrary_provenance_clause({"contrary_state": "landed",
                                                "contrary_provenance": None}),
            "", "an explicit None provenance rendered a clause")

    def test_an_absent_provenance_renders_nothing_and_a_present_one_renders_words(self):
        """The render door, both directions, so the emptiness above is about
        ABSENCE and not about a dead renderer."""
        self.assertEqual(
            landreq.contrary_provenance_clause({"contrary_provenance": None}), "")
        self.assertEqual(
            landreq.contrary_provenance_clause({"contrary_provenance": "recorded"}),
            " [recorded when closed]")
        self.assertEqual(
            landreq.contrary_provenance_clause({"contrary_provenance": "observed"}),
            " [observed on trunk]")

    def test_the_recorded_branch_requires_the_VALUE_not_just_the_flag(self):  # noqa: VACUOUS_ASSERTION — this is an AST predicate over production source, not an absence assertion; its positive control is that the walk MUST find the assignment (asserted first) and that the observed branch is asserted present in the same read
        """The third round, and the reversal it named as the bar.

        The behaviour cannot be reached from a supported verb — dispatches sets
        `discharged` and `discharge_contrary` TOGETHER, `withdraw` sets
        `withdrawn` instead, and `discharged` is absent from snapshot rows — so
        an arm that constructs the case does not exist to be written. The review's
        stated alternative is to pin the VALUE-PRESENCE predicate itself, with
        the hostile reversal red.

        THE REVERSAL THIS KILLS: replacing `(discharged and
        row.get("discharge_contrary"))` with bare `discharged`. That is exactly
        the defect — a discharged row whose discharge recorded NOTHING renders
        "STATE UNREAD" beside "[recorded when closed]".

        It is a source assertion and I would normally argue against one. Here
        the sibling arm
        `test_provenance_mirrors_the_state_branch_order_in_the_source` already
        pins this same assignment's SHAPE and caught me deleting `discharged`
        from it entirely, so the structural bar is the established one for this
        rule and a second, behavioural door does not exist.
        """
        import ast as _ast
        import inspect as _inspect
        src = _inspect.getsource(landreq)
        found = None
        for node in _ast.walk(_ast.parse(src)):
            if isinstance(node, _ast.Assign) and len(node.targets) == 1 \
                    and getattr(node.targets[0], "id", None) == \
                    "contrary_provenance" \
                    and isinstance(node.value, _ast.IfExp):
                found = _ast.get_source_segment(src, node.value.test) or ""
        self.assertIsNotNone(
            found, "MUST-HIT: never found the contrary_provenance assignment, "
                   "and a walk that finds nothing must not read as agreement")
        self.assertIn(
            "discharge_contrary", found,
            "the recorded branch no longer requires the discharge to have "
            "RECORDED anything — bare `discharged` makes a discharge that "
            "recorded nothing claim '[recorded when closed]' beside a fact of "
            "STATE UNREAD: %r" % found)
        self.assertIn(
            "discharged", found,
            "the recorded branch no longer covers the discharge path at all; "
            "both persisted-string branches must map to recorded: %r" % found)

    def test_the_card_and_the_terminal_print_the_same_provenance_words(self):  # noqa: VACUOUS_ASSERTION — this arm asserts an EQUALITY of two parsed tables, not an absence, and it already carries its own non-vacuity guards: assertIsNotNone on the python table (a walk that finds nothing must not read as agreement), assertTrue on the card path, and assertIsNotNone on the regex block, each failing loudly if its source went unread
        """THE SEAM ARM. Both renderer files' comments already PROMISE they
        print the same words, and they had drifted anyway on the unread case.
        A promise in a comment is not a mechanism; this reads both sources.
        """
        import ast as _ast
        import os as _os
        import re as _re
        with open(landreq.__file__, encoding="utf-8") as _fh:
            src = _fh.read()
        py_words = None
        for node in _ast.walk(_ast.parse(src)):
            if isinstance(node, _ast.Assign) and len(node.targets) == 1 \
                    and getattr(node.targets[0], "id", None) == \
                    "_CONTRARY_PROVENANCE_WORDS":
                py_words = _ast.literal_eval(node.value)
        self.assertIsNotNone(py_words,
                             "never found _CONTRARY_PROVENANCE_WORDS — a walk "
                             "that finds nothing must not read as agreement")

        card = _os.path.join(_os.path.dirname(landreq.__file__),
                             "web_ui", "scripts", "00-core.js.part")
        self.assertTrue(_os.path.exists(card), "the card source moved: %s" % card)
        with open(card, encoding="utf-8") as fh:
            js = fh.read()
        block = _re.search(r"const PROV = \{(.*?)\};", js, _re.S)
        self.assertIsNotNone(block, "never found the card's PROV table")
        js_words = dict(_re.findall(r'(\w+)\s*:\s*"([^"]+)"', block.group(1)))
        self.assertEqual(js_words, py_words,
                         "the card and the terminal name the same provenance "
                         "differently, so one row reads two ways")


class ContraryDischargeArmsTest(unittest.TestCase):
    """CONTRARY claimed a verdict was DEFIED, on rows whose verdict was HONORED
    through succession — the successor landed and the door closed the row,
    which is the process WORKING. Measured on the live board: of 52 in-flight
    contrary rows, 32 were discharged and 20 genuinely were not.

    RENAMED from ContraryDischargeTest: this module already held
    a lifecycle class by that exact name, and the later definition SHADOWED
    it — the discharge-lifecycle tests silently stopped running the day this
    class landed (b83f56ca). Two classes, one name, zero warnings is the
    rotten-green shape the vacuity tripwire exists for, one level up.

    TWO ARMS, and the second is not optional. (a) an approve whose reviewed_tip
    IS this row's tip or a git DESCENDANT of it -- succession by continuation.
    (b) an approve on a supersedes row naming THIS row as parent -- the ladder
    discharge, which is CITATION binding and shares NO git lineage, because a
    successor that REBUILT the content has no ancestry relation to the refused
    tip. Arm (a) alone leaves every walked ladder shouting forever; 16 of the
    32 discharges came through (b).
    """

    CHAIN_ROOT = "root-1"

    def rows(self, *specs):
        chain = {self.CHAIN_ROOT: []}
        for r in specs:
            r.setdefault("chain_root", self.CHAIN_ROOT)
            chain[self.CHAIN_ROOT].append(r)
        return chain

    def test_arm_a_continuation_discharges(self):  # noqa: VACUOUS_ASSERTION — the LOUD case below is the unconditional negative control on the same observable
        """An approve on the SAME tip is succession by continuation."""
        row = {"id": "r1", "reviewed_tip": "aaa", "polarity": "supersede"}
        appr = {"id": "r2", "reviewed_tip": "aaa", "polarity": "approve"}
        chain = self.rows(row, appr)
        self.assertEqual(
            landreq.contrary_discharge(row, chain, {"aaa"}, {}, set()), "a")

    def test_arm_b_citation_discharges_without_lineage(self):  # noqa: VACUOUS_ASSERTION — asserts the positive "b" on the same observable
        """THE ARM THAT ANCESTRY CANNOT REACH. The approve's tip shares no
        lineage with this row's tip and the ancestry ledger says NO -- it
        discharges anyway, because it belongs to a supersedes row naming this
        row as parent. Without this, a rebuilt successor never quiets its
        parent and the #177 ladder is a cure nobody can walk."""
        row = {"id": "r1", "reviewed_tip": "aaa", "polarity": "fix"}
        appr = {"id": "r2", "reviewed_tip": "zzz", "polarity": "approve",
                "supersedes": "r1"}
        chain = self.rows(row, appr)
        self.assertEqual(
            landreq.contrary_discharge(row, chain, {"zzz"},
                                       {("aaa", "zzz"): False}, set()), "b")

    def test_an_unrelated_later_approve_does_NOT_discharge(self):  # noqa: VACUOUS_ASSERTION — arm-a and arm-b tests above are the unconditional positives on the same observable
        """THE TRUE-CONTRARY CONTROL, and the reason "any later approve in the
        chain" was rejected: an approve on an UNRELATED tip must not silence a
        standing verdict. Live: b71f8dab's chain holds a later on-trunk approve
        (cd8eee6a) bound to a different tip; it stays LOUD."""
        row = {"id": "r1", "reviewed_tip": "aaa", "polarity": "fix"}
        appr = {"id": "r2", "reviewed_tip": "zzz", "polarity": "approve"}
        chain = self.rows(row, appr)
        self.assertIsNone(
            landreq.contrary_discharge(row, chain, {"zzz"},
                                       {("aaa", "zzz"): False}, set()))

    def test_an_uncomputed_pair_is_UNVERIFIED_never_a_guess(self):  # noqa: VACUOUS_ASSERTION — the three decided cases above are the positives; this pins the undecided one
        """The ledger is warm-up-honest: a pair not yet computed renders
        UNVERIFIED and is collected for the bounded backfill, never guessed in
        either direction."""
        row = {"id": "r1", "reviewed_tip": "aaa", "polarity": "fix"}
        appr = {"id": "r2", "reviewed_tip": "zzz", "polarity": "approve"}
        chain = self.rows(row, appr)
        unseen = set()
        self.assertEqual(
            landreq.contrary_discharge(row, chain, {"zzz"}, {}, unseen),
            "unverified")
        self.assertIn(("aaa", "zzz"), unseen, "the pair was not queued")

    def test_an_offtrunk_approve_cannot_discharge(self):  # noqa: VACUOUS_ASSERTION — the arm-a test above is the same observable with the tip ON trunk
        """An approve whose own tip never reached trunk discharges nothing --
        it is a verdict, not a landing."""
        row = {"id": "r1", "reviewed_tip": "aaa", "polarity": "supersede"}
        appr = {"id": "r2", "reviewed_tip": "aaa", "polarity": "approve"}
        chain = self.rows(row, appr)
        self.assertIsNone(
            landreq.contrary_discharge(row, chain, set(), {}, set()))

    @staticmethod
    def confirmation(**kw):
        """The live hydra shape (d0c72ad9/62c5a2cc/68e1a449):
        kind=review, --supersedes a contrary parent, supersede verdict whose
        evidence OPENS the resolved-door sentence, reviewed tip = the cure
        carrier already on trunk."""
        row = {"id": "r2", "kind": "review", "supersedes": "r1",
               "reviewed_tip": "zzz", "polarity": "supersede",
               "verdict_ref": landreq.CONFIRMATION_EVIDENCE
               + " zzz IS ancestor of origin/main."}
        row.update(kw)
        return row

    def test_arm_c_a_confirmation_row_is_the_discharge_instrument(self):
        """#149 value-space class: the row KIND the classifier's vocabulary
        lacked. supersede-verdict + landed-tip is this row's HEALTHY shape —
        the carrier landed before the verdict by design — so it answers "c"
        with NO git question left to ask (reach deliberately empty here)."""
        row = self.confirmation()
        self.assertEqual(
            landreq.contrary_discharge(row, self.rows(row), set(), {}, set()),
            "c")

    def test_the_phrase_is_load_bearing_not_the_supersede_shape(self):
        """The same dispatch shape WITHOUT the opening sentence is exactly the
        contrary signature and stays loud. Live control: 5ebc8f1b/c4dc53e6/
        4a37f62e were filed with evidence "R" and must keep alarming. The
        phrase must OPEN the evidence — quoted mid-prose it authorizes
        nothing (the keyword-inference ban, module header) — and must carry
        a CONCRETE resolution: the bare sentence with an empty tail is
        refused by the door's own parser, so it is refused here identically."""
        for ev in ("R", "see " + landreq.CONFIRMATION_EVIDENCE + " zzz",
                   landreq.CONFIRMATION_EVIDENCE):
            row = self.confirmation(verdict_ref=ev)
            self.assertFalse(landreq.confirmation_row(row), ev)
            self.assertIsNone(
                landreq.contrary_discharge(row, self.rows(row), set(), {},
                                           set()), ev)
        # the unconditional positive on the same observable
        self.assertTrue(landreq.confirmation_row(self.confirmation()))

    def test_polarity_is_load_bearing_a_fix_confirmation_stays_loud(self):  # noqa: VACUOUS_ASSERTION — the positive-control loop walks the LITERAL two-element CONFIRMATION_POLARITIES constant (pinned non-empty by test_the_predicate_reads_the_doors_own_contracts_not_copies), and each iteration asserts the "c"/"b" positives unconditionally
        """The exact-tip repro (verdict on 1710265fd9a7): the first cut
        never read polarity, so a FIX-polarity but otherwise
        confirmation-shaped row passed the predicate — self "c" and parent
        "b" minted out of the exact verdict whose meaning is "NOT resolved".
        The gate is the door's own shared set (CONFIRMATION_POLARITIES):
        FIX refused, approve and supersede admitted — both directions."""
        fix = self.confirmation(polarity="fix")
        self.assertFalse(landreq.confirmation_row(fix))
        self.assertIsNone(
            landreq.contrary_discharge(fix, self.rows(fix), set(), {}, set()))
        # the parent leg of the same repro: a FIX citation discharges nothing
        parent = {"id": "r1", "reviewed_tip": "aaa", "polarity": "fix"}
        self.assertIsNone(landreq.contrary_discharge(
            parent, self.rows(parent, self.confirmation(polarity="fix")),
            {"zzz"}, {("aaa", "zzz"): False}, set()))
        # the positive controls on the same observables, per admitted polarity
        for pol in landreq.CONFIRMATION_POLARITIES:
            conf = self.confirmation(polarity=pol)
            self.assertTrue(landreq.confirmation_row(conf), pol)
            self.assertEqual(landreq.contrary_discharge(
                conf, self.rows(conf), set(), {}, set()), "c", pol)
            self.assertEqual(landreq.contrary_discharge(
                parent, self.rows(parent, self.confirmation(polarity=pol)),
                {"zzz"}, {("aaa", "zzz"): False}, set()), "b", pol)

    def test_the_predicate_reads_the_doors_own_contracts_not_copies(self):
        """The anti-drift pin: the polarity set IS the constant the resolved
        close rung reads, and the phrase gate answers exactly as the door's
        own parser does — a paraphrase is how the polarity hole happened."""
        self.assertEqual(landreq.CONFIRMATION_POLARITIES,
                         ("approve", "supersede"))
        self.assertNotIn("fix", landreq.CONFIRMATION_POLARITIES)
        self.assertEqual(landreq.CONFIRMATION_EVIDENCE,
                         dispatches._RESOLUTION)
        row = self.confirmation()
        self.assertEqual(
            landreq.confirmation_row(row),
            dispatches.resolution_statement(row["verdict_ref"]) is not None)
        self.assertTrue(landreq.confirmation_row(row))

    def test_kind_and_citation_are_load_bearing_too(self):
        """A BUILD row wearing the sentence is NOT the door's instrument (live
        control: 28dd290747dc, kind=build, genuinely half-carried, must stay
        loud), and neither is a review with no supersedes parent."""
        for row in (self.confirmation(kind="build"),
                    self.confirmation(supersedes=None)):
            self.assertFalse(landreq.confirmation_row(row))
            self.assertIsNone(
                landreq.contrary_discharge(row, self.rows(row), set(), {},
                                           set()))
        self.assertTrue(landreq.confirmation_row(self.confirmation()))

    def test_arm_b_a_confirmation_discharges_its_parent_by_citation(self):
        """The b-arm extension: the confirmation round IS the ladder discharge
        for the parent it cites — like an on-trunk approve, ancestry says NO
        and the citation carries it anyway."""
        row = {"id": "r1", "reviewed_tip": "aaa", "polarity": "fix"}
        conf = self.confirmation()
        chain = self.rows(row, conf)
        self.assertEqual(
            landreq.contrary_discharge(row, chain, {"zzz"},
                                       {("aaa", "zzz"): False}, set()), "b")

    def test_an_offtrunk_or_phraseless_citation_cannot_discharge_the_parent(self):
        """Both gates hold on the parent side too: the confirmation's own tip
        must be ON trunk (reach), and a phraseless supersede citation (the
        "R" shape) discharges nothing."""
        row = {"id": "r1", "reviewed_tip": "aaa", "polarity": "fix"}
        conf = self.confirmation()
        self.assertIsNone(landreq.contrary_discharge(
            row, self.rows(row, conf), set(), {("aaa", "zzz"): False}, set()))
        bare = self.confirmation(verdict_ref="R")
        self.assertIsNone(landreq.contrary_discharge(
            row, self.rows(row, bare), {"zzz"}, {("aaa", "zzz"): False},
            set()))
        # the positive control on the same observable: phrase + on-trunk tip
        self.assertEqual(landreq.contrary_discharge(
            row, self.rows(row, self.confirmation()), {"zzz"},
            {("aaa", "zzz"): False}, set()), "b")


class FixContraryCarrierDoorTest(_landreq.LandReqBase):
    """The succession gate admitted only carriers whose EXACT sha was in the
    trunk rev-list, while the row's own LANDED marker walks the full landing
    ladder (ancestry, else patch id — this repo cherry-picks its lands,
    `_landing_proof`'s stated law). Thus a literal-ancestor carrier discharged
    while an equivalent chain-linked approval bound to a patch-equivalent
    carrier remained contrary. Same door, two vocabularies for "on trunk",
    and the narrower one answered.

    THE DOOR IS ONE GATE, BOTH POLARITIES. These arms drive the annotator
    (`_annotate_contrary_discharge`), not a hand-built reach set, because the
    defect lived in what the annotator could SEE, and a fixture that hands
    the walk a friendly reach would assert around the hole."""

    ROOT = "carrier-root"

    def setUp(self):
        super().setUp()
        # The gate measures against origin/main BY NAME (`_trunk_reach`'s
        # pinned ref). This host mints fixture repos on `master`, which would
        # hand every arm an empty reach and let the negative controls pass
        # vacuously — the must-hits below caught exactly that. Rename to the
        # ref the door actually consults.
        self.git("branch", "-m", self.main, "main")
        self.main = "main"

    def annotate(self, row, sib, gitdir=None):
        """Stamp `row` through the real annotator against the fixture repo.
        Returns the contrary_discharge stamp."""
        gitdir = gitdir or self.repo
        repo_id = gitdir if gitdir.endswith("/.git") \
            else os.path.join(gitdir, ".git")
        for r in (row, sib):
            r.setdefault("chain_root", self.ROOT)
            r["repo_id"] = repo_id
        row.setdefault("contrary", True)
        row.setdefault("terminal", False)
        for field in ("succession_state", "succession_carrier",
                      "succession_unknown_reason", "contrary_discharge"):
            row.pop(field, None)
        out = {row["id"]: row}
        current = {row["id"]: row, sib["id"]: sib}
        landreq._annotate_contrary_discharge(
            out, current=current, gitdir=gitdir,
            reach=landreq._trunk_reach(gitdir))
        return row.get("contrary_discharge")

    def cherry_carrier(self):
        """A side-branch cure whose PATCH lands on trunk under a NEW sha
        (cherry-pick), so the cure sha is provably NOT an ancestor of
        origin/main while its content provably is."""
        self.add_origin()
        self.git("checkout", "-q", "-b", "cure-carrier", self.main)
        cure = self.commit("cure", path="h")
        self.git("checkout", "-q", self.main)
        self.commit("trunk-advanced", path="trunk-only")
        self.git("cherry-pick", cure)
        self.git("push", "-q", "origin", self.main)
        # FIXTURE LAW, must-hit before any arm leans on it: content on
        # trunk, sha not — the exact live shape, proven by the same
        # instrument the door will consult (`_git` speaks git DIRECTORIES —
        # persisted repo_id values end in /.git).
        self.assertNotIn(cure, landreq._trunk_reach(self.repo))
        self.assertEqual(
            landreq._landing_proof(os.path.join(self.repo, ".git"), cure,
                                   "origin/main"),
            "patch-equivalent")
        return cure

    def test_a_patch_equivalent_carrier_discharges_a_fix_contrary(self):
        """THE MISSING DOOR: FIX-contrary + chain-linked LANDED approve whose
        bound carrier is on trunk by patch — discharges as "b", the ladder
        discharge, exactly as its literal-ancestor twin always did."""
        cure = self.cherry_carrier()
        row = {"id": "r1", "reviewed_tip": self.side, "polarity": "fix"}
        appr = {"id": "r2", "kind": "review", "supersedes": "r1",
                "reviewed_tip": cure, "polarity": "approve"}
        self.assertEqual(self.annotate(row, appr), "b")

    def test_the_carrier_proof_is_named_never_flattened(self):
        """`patch-equivalent` RELIEVES and is never flattened into
        `ancestor` (the module's own proof law): the stamped carrier evidence
        must say WHICH proof carried the landing, or a later reader re-runs
        git to find out."""
        cure = self.cherry_carrier()
        repo_id = os.path.join(self.repo, ".git")
        row = {"id": "r1", "reviewed_tip": self.side, "polarity": "fix",
               "chain_root": self.ROOT, "contrary": True, "terminal": False,
               "repo_id": repo_id}
        appr = {"id": "r2", "kind": "review", "supersedes": "r1",
                "reviewed_tip": cure, "polarity": "approve",
                "chain_root": self.ROOT, "repo_id": repo_id}
        out = {"r1": row}
        current = {"r1": row, "r2": appr}
        landreq._annotate_succession(
            out, current, landreq._trunk_reach(self.repo), gitdir=self.repo)
        self.assertEqual(row["succession_state"], landreq.SUCCESSION_MOVED)
        self.assertEqual(row["succession_carrier"]["landed_proof"],
                         "patch-equivalent")

    def test_a_literal_ancestor_carrier_still_discharges(self):
        """REGRESSION PIN: the literal-ancestor carrier is untouched by the
        wider gate, and its evidence says `ancestor`."""
        self.add_origin()
        reach = landreq._trunk_reach(self.repo)
        self.assertIn(self.c, reach)     # must-hit: trunk head is published
        row = {"id": "r1", "reviewed_tip": self.side, "polarity": "fix"}
        appr = {"id": "r2", "kind": "review", "supersedes": "r1",
                "reviewed_tip": self.c, "polarity": "approve"}
        self.assertEqual(self.annotate(row, appr), "b")

    def test_one_gate_answers_both_target_polarities(self):  # noqa: VACUOUS_ASSERTION — the loop walks a LITERAL two-element tuple, and every iteration asserts the positive "b" unconditionally
        """The fold, not a parallel predicate: a SUPERSEDE-polarity contrary
        walks the SAME gate over the SAME patch-equivalent carrier and
        discharges identically — no per-polarity door to drift apart."""
        cure = self.cherry_carrier()
        for polarity in ("fix", "supersede"):
            row = {"id": "r1", "reviewed_tip": self.side,
                   "polarity": polarity}
            appr = {"id": "r2", "kind": "review", "supersedes": "r1",
                    "reviewed_tip": cure, "polarity": "approve"}
            self.assertEqual(self.annotate(row, appr), "b", polarity)

    def test_a_non_landed_carrier_does_not_open_the_door(self):
        """FAIL-CLOSED: the same citation shape whose bound ref is MEASURED
        absent from trunk (`git cherry` '+') discharges nothing — the door
        widened to the landing ladder, not to "any cited sha"."""
        self.add_origin()
        self.git("checkout", "-q", "side")
        stray = self.commit("stray", path="h")
        self.git("checkout", "-q", self.main)
        # must-hit: the fixture really is off trunk by the door's own ladder
        self.assertEqual(
            landreq._landing_proof(os.path.join(self.repo, ".git"), stray,
                                   "origin/main"),
            "absent")
        row = {"id": "r1", "reviewed_tip": self.side, "polarity": "fix"}
        appr = {"id": "r2", "kind": "review", "supersedes": "r1",
                "reviewed_tip": stray, "polarity": "approve"}
        self.assertIsNone(self.annotate(row, appr))

    def test_unreadable_carrier_evidence_is_UNKNOWN_never_a_discharge(self):  # noqa: VACUOUS_ASSERTION — the closing assertEqual(annotate(...), "b") is the unconditional positive control on the same observable, through the readable repo
        """STORE LAW: evidence that cannot be read yields UNKNOWN, never a
        discharge. The same discharging shape probed through a gitdir that is
        not a repository must render UNVERIFIED — and must never mint "b"."""
        cure = self.cherry_carrier()
        row = {"id": "r1", "reviewed_tip": self.side, "polarity": "fix"}
        appr = {"id": "r2", "kind": "review", "supersedes": "r1",
                "reviewed_tip": cure, "polarity": "approve"}
        broken = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(broken)
        stamp = self.annotate(row, appr, gitdir=broken)
        self.assertNotIn(stamp, ("a", "b", "c"))
        self.assertEqual(stamp, "unverified")
        # the unconditional positive control on the same observable: the
        # identical shape through the READABLE repo discharges — so the
        # UNVERIFIED above is the gitdir's doing, not the fixture's
        self.assertEqual(self.annotate(dict(row), dict(appr)), "b")

    def test_a_vanished_carrier_object_is_UNKNOWN_never_absent(self):
        """A missing object is an unreadable landing question, not proof that
        the carrier failed to land. The real backend checks the fixture repo's
        object database and keeps the target loud."""
        self.add_origin()
        missing = "0" * 40
        repo_id = os.path.join(self.repo, ".git")
        row = {"id": "r1", "reviewed_tip": self.side, "polarity": "fix",
               "repo_id": repo_id}
        appr = {"id": "r2", "kind": "review", "supersedes": "r1",
                "reviewed_tip": missing, "polarity": "approve",
                "repo_id": repo_id}
        proof = landreq._landing_proofs(self.repo)
        self.assertEqual(proof(row, appr), landreq.PROOF_UNKNOWN)
        self.assertEqual(self.annotate(row, appr), "unverified")
        self.assertIn("landing proof is unreadable",
                      row["succession_unknown_reason"])

    def test_a_rebased_cure_tip_discharges_by_patch_identity(self):  # noqa: VACUOUS_ASSERTION — the closing assertEqual(..., "b") is the unconditional positive; the assertNotIn above it pins fixture geometry beside its assertIn sibling
        """A rebased cure keeps its reviewed series tip off ancestry while
        trunk carries the same patches under new shas (`git cherry` '-'). One
        predicate covers both landing shapes: ancestry OR patch identity,
        exactly how the carrier row's own LANDED marker already decided."""
        self.add_origin()
        self.git("checkout", "-q", "-b", "cure-series", self.main)
        c1 = self.commit("cure-1", path="h")
        cure = self.commit("cure-2", path="i")
        # REBASE the two-commit series onto trunk and land it there; the
        # verdicted tips keep their pre-rebase shas.
        self.git("checkout", "-q", self.main)
        self.commit("trunk-advanced", path="trunk-only")
        self.git("cherry-pick", c1, cure)
        self.git("push", "-q", "origin", self.main)
        reach = landreq._trunk_reach(self.repo)
        self.assertNotIn(cure, reach)          # pre-rebase sha, off trunk
        # must-hit on the PER-COMMIT question the door asks: the bound tip's
        # own patch is on trunk ('-'), even though the branch beneath it
        # still carries never-landed history (the base's `side` commit).
        self.assertIn("- " + cure,
                      self.git("cherry", "origin/main", cure).splitlines())
        row = {"id": "r1", "reviewed_tip": self.side, "polarity": "fix"}
        appr = {"id": "r2", "kind": "review", "supersedes": "r1",
                "reviewed_tip": cure, "polarity": "approve"}
        self.assertEqual(self.annotate(row, appr), "b")

    def test_a_patch_blind_merge_tip_is_UNKNOWN_never_a_discharge(self):  # noqa: VACUOUS_ASSERTION — the closing assertEqual(annotate(...), "b") is the unconditional positive control on the same observable, through the patch-landed carrier
        """`git cherry` cannot see a merge commit (`rev-list --no-merges`
        underneath — `vcs.landed_state`'s stated blind spot), so an approve
        bound to an off-trunk MERGE tip has NO patch identity to compare:
        the door must answer UNKNOWN, never a discharge and never a
        confident no."""
        cure = self.cherry_carrier()
        merge_tip = self.git("commit-tree", "side^{tree}", "-p", "side",
                             "-p", self.main, "-m", "merge")
        # must-hit: the instrument really is blind here, and the tip is off
        # trunk — the geometry the arm claims
        self.assertNotIn(merge_tip, landreq._trunk_reach(self.repo))
        self.assertEqual(
            landreq._landing_proof(os.path.join(self.repo, ".git"),
                                   merge_tip, "origin/main"),
            "unknown")
        row = {"id": "r1", "reviewed_tip": self.side, "polarity": "fix"}
        appr = {"id": "r2", "kind": "review", "supersedes": "r1",
                "reviewed_tip": merge_tip, "polarity": "approve"}
        stamp = self.annotate(row, appr)
        self.assertNotIn(stamp, ("a", "b", "c"))
        self.assertEqual(stamp, "unverified")
        # the unconditional positive control on the same observable: the
        # same row through the readable, patch-landed carrier discharges —
        # so the UNVERIFIED above is the merge tip's doing
        self.assertEqual(
            self.annotate(dict(row), {**appr, "reviewed_tip": cure}), "b")


class ContraryHonoredOnEverySurfaceTest(_landreq.LandReqBase):
    """#135 two-surfaces class: the annotation existed and `lr list` rendered
    it, but `lr show` still printed "owed by integrator" and `card()` — the
    exact wire shape /api/lr serves — dropped the stamp entirely, so the
    owner's console counted ELEVEN contrary rows over SIX live ones (measured;
    five were honored through succession). One projected row, one
    stamp, THREE renderers — these pin that they cannot disagree.

    Display truth only: `owed_by` is computed BEFORE the annotation on
    purpose (enforcement stays put), and the enforcement field is asserted
    unchanged beside every honored rendering."""

    def contrary_lr(self):
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
        self.git("merge", "--no-edit", "-q", "side")   # landed despite FIX
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable, unavailable)
        lr = lrs[row["id"]]
        # MUST-HIT controls: the row IS contrary and the annotation pass ran.
        self.assertTrue(lr["contrary"])
        self.assertIn("contrary_discharge", lr)
        return lr

    def test_lr_list_renders_honored_and_stays_loud_without_a_stamp(self):
        lr = self.contrary_lr()
        # this chain holds no on-trunk approve, so the projection stamps None
        # and the row stays LOUD — the negative control on the real fixture
        self.assertIsNone(lr["contrary_discharge"])
        self.assertIn("CONTRARY: LANDED despite FIX verdict", landreq._line(lr))
        lr["contrary_discharge"] = "a"
        line = landreq._line(lr)
        self.assertIn("SUPERSEDED-CLOSED", line)
        self.assertIn("HONORED through succession (continuation)", line)
        self.assertNotIn("CONTRARY:", line)
        lr["contrary_discharge"] = "b"
        self.assertIn("HONORED through succession (ladder discharge)",
                      landreq._line(lr))

    def test_lr_show_says_honored_when_the_discharge_is_stamped(self):
        lr = self.contrary_lr()
        # unstamped: the loud line, verbatim — the pre-fix rendering was
        # "owed by integrator" on honored rows too
        self.assertIn("owed by integrator", landreq._render_show(lr))
        lr["contrary_discharge"] = "a"
        shown = landreq._render_show(lr)
        self.assertIn("HONORED through succession (continuation)", shown)
        self.assertNotIn("owed by integrator", shown)
        # DISPLAY ONLY: the enforcement field still bills the integrator
        self.assertEqual(lr["owed_by"], "integrator")
        lr["contrary_discharge"] = "b"
        self.assertIn("HONORED through succession (ladder discharge)",
                      landreq._render_show(lr))

    def test_lr_show_names_an_unverified_succession_instead_of_plain_debt(self):
        lr = self.contrary_lr()
        lr["contrary_discharge"] = "unverified"
        lr["succession_unknown_reason"] = \
            "supersedes chain is malformed or unreadable"
        shown = landreq._render_show(lr)
        self.assertIn("succession UNVERIFIED", shown)
        self.assertIn(lr["succession_unknown_reason"], shown)
        self.assertIn("no discharge was inferred", shown)
        self.assertNotIn("it converges", shown)
        self.assertIn("owed by integrator", shown)   # still billed: fail-closed
        wire = landreq.card(lr)
        self.assertEqual(wire["succession_state"], lr["succession_state"])
        self.assertEqual(wire["succession_unknown_reason"],
                         lr["succession_unknown_reason"])

    def test_card_carries_the_stamp_and_agrees_with_list_row_for_row(self):
        lr = self.contrary_lr()
        for stamp in (None, "a", "b", "unverified"):
            lr["contrary_discharge"] = stamp
            wire = landreq.card(lr)
            self.assertEqual(wire["contrary_discharge"], stamp)
            # PARITY, the property itself: the wire says honored exactly when
            # the list renderer prints honored for the SAME row — and both
            # agree with THE display predicate, the one authority.
            self.assertEqual(landreq.honored_display(lr),
                             "HONORED through succession" in landreq._line(lr))
            self.assertEqual(wire["contrary_discharge"] in ("a", "b"),
                             landreq.honored_display(lr))
            holder = "nobody" if stamp in ("a", "b") else "integrator"
            self.assertEqual((wire["holder_role"], wire["holder_seat"]),
                             (holder, None), stamp)
            self.assertEqual(landreq._owed_by_whom(lr), holder, stamp)
        # and the enforcement copy is untouched by any of it
        self.assertEqual(landreq.card(lr)["owed_by"], "integrator")

    def test_an_honored_row_that_is_also_stalled_is_quiet_on_list_AND_show(self):
        """The composite (dispatch 97d8899a): with `stalled` True the
        first cut left list/show shouting STALLED over a row the home band
        rendered honored-quiet — the two-surfaces defect again, one field
        over. One predicate (honored_display) now answers everywhere: the
        honored banner wins over the stalled ALARM, and only the ALARM —
        `stalled`, `owed_by` and the stall billing stay exactly as measured."""
        lr = self.contrary_lr()
        lr["stalled"] = True
        lr["stall_threshold_s"] = 3600   # the dwell tail renders its word too
        # LOUD control first: unstamped, the composite alarms on both words.
        self.assertFalse(landreq.honored_display(lr))
        line = landreq._line(lr)
        self.assertIn("STALLED", line)
        self.assertIn("CONTRARY: LANDED despite FIX verdict", line)
        self.assertIn("STALLED", landreq._render_show(lr))
        lr["contrary_discharge"] = "a"
        self.assertTrue(landreq.honored_display(lr))
        line = landreq._line(lr)
        self.assertIn("HONORED through succession (continuation)", line)
        self.assertNotIn("STALLED", line)
        shown = landreq._render_show(lr)
        self.assertIn("HONORED through succession (continuation)", shown)
        # the headline STALLED and the threshold tail's alarm word both
        # yield; the measurement itself stays visible and says why it is
        # not billed instead of pretending "ok"
        self.assertNotIn("STALLED", shown)
        self.assertIn("not billed (HONORED through succession)", shown)
        # DISPLAY ONLY: the enforcement fields are untouched by the quieting
        self.assertTrue(lr["stalled"])
        self.assertEqual(lr["owed_by"], "integrator")
        self.assertTrue(landreq.card(lr)["stalled"])


class ConfirmationRowIsNeverContraryTest(_landreq.LandReqBase):
    """THE HYDRA, measured live: the #177 ladder's own CONFIRMATION
    rounds rendered as contrary — supersede verdict + landed tip is the
    contrary signature exactly, AND the confirmation round's healthy shape by
    design (the carrier lands BEFORE the verdict). Every cure round therefore
    ADDED a contrary row; the owner's card sat at 11 for hours while rows
    churned underneath (d0c72ad9, 62c5a2cc, 68e1a449, 87a923eb). The #149
    value-space class: a row KIND the classifier's vocabulary lacked.

    These are the REAL-PROJECTION fixtures: a ladder built through actual
    dispatch/verdict/merge, projected, annotated against this repo's trunk,
    and read through every surface (_line / _render_show / card)."""

    PHRASE = landreq.CONFIRMATION_EVIDENCE

    def ladder(self, evidence=None, polarity="supersede"):
        """A contrary parent + its confirmation round, exactly as the resolved
        door files them: parent reviewed at `side`, FIX filed, side merged
        anyway (the contrary), then a kind=review --supersedes round whose
        verdict re-reads the SAME landed tip with the resolved-door evidence."""
        self.add_origin()
        parent = self.dispatch(ref=self.side, lane="lane/hydra", kind="review")
        self.mark_verdict(parent["id"], self.side, "findings",
                                polarity="fix")
        self.git("merge", "--no-edit", "-q", "side")   # landed despite FIX
        # publish under the name the annotator's reach walk reads
        # (origin/main — the production trunk ref, hardcoded there), whatever
        # this fixture repo's default branch happens to be called
        self.git("push", "-q", "origin", "%s:refs/heads/main" % self.main)
        conf = self.dispatch(ref=self.side, lane="lane/hydra", kind="review",
                             supersedes=parent["id"])
        if evidence is None:
            evidence = self.PHRASE + " %s IS ancestor of origin/%s." % (
                self.side[:8], self.main)
        self.mark_verdict(conf["id"], self.side, evidence,
                                polarity=polarity)
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable, unavailable)
        # project() annotates against os.getcwd(); the fixture's trunk lives
        # in self.repo, so re-run the SAME production annotator against it —
        # the call project_raw makes, with the repo named.
        landreq._annotate_contrary_discharge(lrs, gitdir=self.repo)
        return lrs[parent["id"]], lrs[conf["id"]]

    def test_a_confirmation_row_reads_confirmation_on_every_surface(self):
        parent, conf = self.ladder()
        # MUST-HIT control: the row wears the full contrary signature — the
        # fact fields stay TRUE (display truth only, like "a"/"b").
        self.assertTrue(conf["contrary"])
        self.assertEqual(conf["contrary_state"], "landed")
        self.assertEqual(conf["polarity"], "supersede")
        self.assertEqual(conf["contrary_discharge"], "c")
        self.assertTrue(landreq.honored_display(conf))
        line = landreq._line(conf)
        self.assertIn("CONFIRMATION: LANDED by design", line)
        self.assertNotIn("CONTRARY", line)
        self.assertNotIn("HONORED through succession", line)   # its own words
        shown = landreq._render_show(conf)
        self.assertIn("CONFIRMATION round: the reviewed tip is the landed "
                      "resolution by design", shown)
        self.assertNotIn("owed by integrator", shown)
        wire = landreq.card(conf)
        self.assertEqual(wire["contrary_discharge"], "c")
        self.assertTrue(wire["contrary"])          # the fact rides untouched
        self.assertEqual(conf["owed_by"], "integrator")  # enforcement stays
        self.assertEqual(landreq.ball_holder(conf), ("nobody", None))
        self.assertEqual(landreq._owed_by_whom(conf), "nobody")
        self.assertEqual((wire["holder_role"], wire["holder_seat"]),
                         ("nobody", None))

    def test_the_confirmation_discharges_its_parent_chain(self):
        parent, conf = self.ladder()
        # the b-arm ladder discharge, stamped through the confirmation's
        # citation — no approve anywhere in this chain
        self.assertEqual(parent["contrary_discharge"], "b")
        self.assertIn("HONORED through succession (ladder discharge)",
                      landreq._line(parent))
        self.assertNotIn("CONTRARY:", landreq._line(parent))
        # DISPLAY ONLY, both rows: enforcement still bills the integrator
        self.assertEqual(parent["owed_by"], "integrator")
        self.assertEqual(conf["owed_by"], "integrator")

    def test_a_supersede_verdict_without_the_phrase_stays_loud(self):
        """The live "R" shape (5ebc8f1b/c4dc53e6/4a37f62e): a confirmation
        filed with truncated evidence is indistinguishable from a real
        unauthorized land, so it MUST keep alarming — the phrase is the
        contract, and fail-closed is the design."""
        parent, conf = self.ladder(evidence="R")
        self.assertIsNone(conf["contrary_discharge"])
        self.assertFalse(landreq.honored_display(conf))
        self.assertIn("CONTRARY: LANDED despite SUPERSEDE verdict",
                      landreq._line(conf))
        # and with no phrase there is no citation discharge either: the
        # parent stays loud too — the genuine-contrary control in the same
        # topology
        self.assertIsNone(parent["contrary_discharge"])
        self.assertIn("CONTRARY: LANDED despite FIX verdict",
                      landreq._line(parent))

    def test_a_fix_polarity_confirmation_stays_loud_on_both_rows(self):
        """The exact-tip repro, at the surface: a confirmation-shaped
        round whose verdict is FIX — the polarity whose meaning is "NOT
        resolved" — must alarm on BOTH rows even wearing the resolved-door
        sentence. Before the polarity gate this projected quiet "c" + "b"."""
        parent, conf = self.ladder(polarity="fix")
        self.assertIsNone(conf["contrary_discharge"])
        self.assertFalse(landreq.honored_display(conf))
        self.assertIn("CONTRARY: LANDED despite FIX verdict",
                      landreq._line(conf))
        self.assertIsNone(parent["contrary_discharge"])
        self.assertIn("CONTRARY: LANDED despite FIX verdict",
                      landreq._line(parent))

    def test_a_stalled_confirmation_is_quiet_and_names_its_reason(self):
        """The composite from the base lane's seam (honored+stalled), one
        stamp over: the stalled ALARM yields to the confirmation words on
        list AND show, while the measurement fields stay billed."""
        _parent, conf = self.ladder()
        conf["stalled"] = True
        conf["stall_threshold_s"] = 3600
        line = landreq._line(conf)
        self.assertIn("CONFIRMATION: LANDED by design", line)
        self.assertNotIn("STALLED", line)
        shown = landreq._render_show(conf)
        self.assertNotIn("STALLED", shown)
        self.assertIn("not billed (CONFIRMATION row — the discharge "
                      "instrument)", shown)
        self.assertTrue(conf["stalled"])           # the field is untouched
