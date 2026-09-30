#!/usr/bin/env python3
"""An Opus SEAT that wrote none of the work is an approval-tier reader of
its own family's work (the owner's ruling: "I think opus seats should be in
the upper tier, we are probably eating lots of tokens on extra rounds", and
one fresh non-author Opus read can approve alone).

THE PROBLEM, MEASURED: three ledger rules outside the verdict door demand
two families, so a claude-authored lane approved by a claude Opus seat never
reached them however independent the seat was: rowstate's derived APPROVED
skips a same-family approve, `lr close --reason resolved` refuses "resolved
needs cross-family eyes", and `--reason subsumed` refuses a same-family
confirmation at the writer and again at replay.

THE RULE THE CURE READS IS THE RECORDED POLICY'S. The approval-tier policy
names a MODEL with a `model:` selector; a verdict recorded under a policy
version whose selector names the model the seat's runtime records takes the
NON-AUTHOR rule: its same-family APPROVE (or, for resolved, SUPERSEDE) counts
when the seat is not the work's sender and wrote no round of its chain
(`landreq.chain_authors`). Every verdict recorded under a family-only policy,
and every seat whose runtime names no model or another model, keeps the
family rule, so an old event replays exactly as it did.

ARMS, one table per rule:
  * a non-author Opus seat's same-family approve derives it under the new
    policy version;
  * the SAME seat as an author of the work does not;
  * a claude seat on another model does not;
  * a seat whose runtime records no model falls back to the family rule;
  * under the old, family-only policy version it keeps the family rule.
Replay stability: a v2 tier record (the old writer's shape) authorizes as
recorded and keeps the family rule; the old selector grammar still reads a
`model:` selector as malformed, which is what v1 and v2 records were derived
under.
"""
import copy
import os
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import dispatches, home, landreq, rowstate, rowworld, store  # noqa: E402
from helm import verdict_tier  # noqa: E402
from helm.store import policy_history  # noqa: E402
# The modules, never their TestCases: tests/test_suite_collection.py says why.
from tests import test_lr_close as _close  # noqa: E402
from tests import test_rowstate as _rowstate  # noqa: E402
from tests._verdict import native_author  # noqa: E402

OPUS = "claude-opus-5-5"
FABLE = "claude-fable-5-1"
#: The reading seat, and a seat that sends work without reading it.
READER = "opus-reader"
AUTHOR = "opus-author"
#: The approval-tier policy before the owner's ruling, and the version that
#: names the Opus model with a `model:` selector.
OLD_POLICY = ["family:codex", "family:claude"]
NEW_POLICY = OLD_POLICY + ["model:" + OPUS]

#: (case, policy, the reader's runtime model, the reader wrote the work,
#: the rule admits it). The positive first, then each control.
CASES = (
    ("non-author opus seat", NEW_POLICY, OPUS, False, True),
    ("the same seat wrote the work", NEW_POLICY, OPUS, True, False),
    ("a claude seat on another model", NEW_POLICY, FABLE, False, False),
    ("the runtime names no model", NEW_POLICY, None, False, False),
    ("the old family-only policy", OLD_POLICY, OPUS, False, False))


class _Seat(object):
    """Shared fixture verbs: the policy, the acting seat, one family."""

    def policy(self, members):
        return store.write_prior({
            "id": "test-approval-tier",
            "statement": "Final approval uses this tier.",
            "confidence": 1.0, "stated_ts": "2026-09-25T00:00:00Z",
            "source": "human", "policy_kind": "approval-tier",
            "policy_members": members,
            "policy_reason": "independent final approval"},
            root_dir=os.path.join(home.global_dir(), "premises"))

    def acting(self, seat):
        """This process acts as `seat` (the dispatch sender)."""
        return mock.patch.dict(os.environ, {"HELM_CHAT_NAME": seat})

    def one_family(self, family="claude"):
        """Every identity resolves to one family: the same-family case each
        rule refused before the ruling."""
        def families(identity):
            evidence = {"v": 1, "identity": identity,
                        "minted_families": [family],
                        "roster_family": None, "roster_verified": False}
            return ({family}, evidence,
                    dispatches._subsumed_family_anchor(evidence), None)
        return mock.patch.object(
            dispatches, "_approval_identity_family_evidence",
            side_effect=families)

    def off_trunk(self, n):
        """A commit of its own on a branch trunk never merges, so no two
        cases in one test share a reviewed tip."""
        self.git("checkout", "-q", "-b", "work-%d" % n, self.main)
        tip = self.commit("work %d" % n, path="work-%d" % n)
        self.git("checkout", "-q", self.main)
        return tip

    def read(self, row, tip, ref, polarity, model):
        """READER's verdict on `row`, recorded under the model its runtime
        names, and the tier record the verdict carries."""
        with native_author(self, "claude", model=model):
            out, err = dispatches.mark_verdict(
                row["id"], tip, ref, polarity=polarity, basis="measured",
                bind_author=True)
        self.assertIsNone(err, err)
        return out


class SubsumedTest(_Seat, _close.CloseBase):
    """`lr close --reason subsumed`, at the writer and at replay."""

    def pair(self, n, policy, model, wrote):
        """(target, confirmation): the round a later round subsumes, and the
        confirming round of the same chain READER approves. When `wrote`,
        READER sent the subsumed round, so READER is its author."""
        self.policy(policy)
        original = self.off_trunk(n)
        with self.acting(READER if wrote else AUTHOR):
            target = dispatches.add(
                "seat-c", "lane/subsumed-%d-r1" % n, ref=original,
                repo=self.repo, kind="review", notify=False, new_work=True)
        _out, err = self.mark_verdict(target["id"], original,
                                      "original verdict", polarity="approve")
        self.assertIsNone(err, err)
        tip = self.commit("founder semantics reimplemented %d" % n,
                          path="subsumed-%d" % n)
        with self.acting(AUTHOR):
            confirmation = dispatches.add(
                READER, "lane/subsumed-%d-r2" % n, ref=tip, repo=self.repo,
                kind="review", notify=False, supersedes=target["id"])
        self.read(confirmation, tip, "Subsumption verified evolved founder "
                  "semantics on trunk now", "approve", model)
        return target, confirmation

    def test_a_non_author_opus_seat_confirms_a_subsumption_of_its_family(self):  # noqa: VACUOUS_ASSERTION — CASES is a fixed non-empty table; the admitted case is asserted closed on the replayed ledger, each control refused with the ledger unchanged
        for n, (case, policy, model, wrote, admits) in enumerate(CASES):
            with self.subTest(case=case):
                target, confirmation = self.pair(n, policy, model, wrote)
                before = self.history_len(target["id"])
                with self.one_family():
                    out, err = landreq.close(
                        target["id"], "subsumed",
                        evidence=confirmation["id"][:12], trunk=self.main)
                if admits:
                    self.assertIsNone(err, err)
                    replayed = dispatches.snapshot()[0][target["id"]]
                    self.assertEqual(
                        (replayed["close_reason"],
                         replayed["original_author_family"],
                         replayed["confirmation_recipient_family"]),
                        ("subsumed", "claude", "claude"))
                else:
                    self.assertIsNone(out)
                    self.assertIn("cross-family", str(err))
                    self.assertEqual(self.history_len(target["id"]), before)


class ResolvedTest(_Seat, _close.CloseBase):
    """`lr close --reason resolved`, writer-side as it has always been."""

    RESOLUTION = ("Resolution verified on trunk: the reimplementation is "
                  "live and the roster check sees it")

    def pair(self, n, policy, model, wrote):
        """(original, confirmation): a SUPERSEDE whose own tip reached trunk,
        and a later review READER confirms with SUPERSEDE. When `wrote`,
        READER sent the original, so READER is its author."""
        self.policy(policy)
        reviewed = self.off_trunk(n)
        with self.acting(READER if wrote else AUTHOR):
            original, why, sent = dispatches.send(
                "seat-c", "lane/resolved-%d" % n, "review it", reviewed,
                repo=self.repo, key="key-resolved-%d" % n, sign=False,
                kind="review", new_work=True)
        self.assertIsNone(why, why)
        _out, err = self.mark_verdict(original["id"], reviewed, "findings",
                                      polarity="supersede")
        self.assertIsNone(err, err)
        self.git("merge", "--no-edit", "-q", "work-%d" % n)
        tip = self.git("rev-parse", self.main)
        with self.acting(AUTHOR):
            confirmation, why, sent = dispatches.send(
                READER, "lane/confirming-%d" % n, "confirm the round", tip,
                repo=self.repo, key="key-confirming-%d" % n, sign=False,
                kind="review", new_work=True)
        self.assertIsNone(why, why)
        self.read(confirmation, tip, self.RESOLUTION, "supersede", model)
        return original, confirmation

    def test_a_non_author_opus_seat_confirms_a_resolution_of_its_family(self):  # noqa: VACUOUS_ASSERTION — CASES is a fixed non-empty table; the admitted case is asserted closed on the replayed ledger, each control refused with the ledger unchanged
        for n, (case, policy, model, wrote, admits) in enumerate(CASES):
            with self.subTest(case=case):
                original, confirmation = self.pair(n, policy, model, wrote)
                before = self.history_len(original["id"])
                with self.one_family():
                    out, err = landreq.close(
                        original["id"], "resolved",
                        evidence=confirmation["id"][:12])
                if admits:
                    self.assertIsNone(err, err)
                    replayed = dispatches.snapshot()[0][original["id"]]
                    self.assertEqual(
                        (replayed["close_reason"],
                         replayed["confirmation_id"]),
                        ("resolved", confirmation["id"]))
                else:
                    self.assertIsNone(out)
                    self.assertIn("cross-family", str(err))
                    self.assertEqual(self.history_len(original["id"]),
                                     before)


class ApprovedRecordTest(_Seat, _close.CloseBase):
    """Derived APPROVED, the record half: which approvals of a repository
    take the non-author rule (`rowworld.non_author_approvals`)."""

    def approval(self, n, policy, model, wrote):
        """A review row READER approves. When `wrote`, READER sent an
        earlier row of the same chain, so READER wrote a round of it."""
        self.policy(policy)
        tip = self.off_trunk(n)
        with self.acting(READER if wrote else AUTHOR):
            first = dispatches.add(
                "seat-c", "lane/approved-%d" % n, ref=tip,
                repo=self.repo, kind="review", notify=False, new_work=True)
        with self.acting(AUTHOR):
            row = dispatches.add(
                READER, "lane/approved-%d" % n, ref=tip,
                repo=self.repo, kind="review", notify=False,
                supersedes=first["id"])
        self.read(row, tip, "read clean", "approve", model)
        return row

    def test_a_non_author_opus_seats_approve_takes_the_non_author_rule(self):  # noqa: VACUOUS_ASSERTION — CASES is a fixed non-empty table; the admitted approval is asserted IN the set and each control asserted out of it
        rows = {}
        for n, (case, policy, model, wrote, admits) in enumerate(CASES):
            rows[case] = (self.approval(n, policy, model, wrote), admits)
        current, err = dispatches.snapshot()
        self.assertFalse(err)
        found = rowworld.non_author_approvals(current, {})
        for case, (row, admits) in rows.items():
            with self.subTest(case=case):
                self.assertEqual(row["id"] in found, admits, case)

    def test_a_LANE_AUTHOR_never_takes_it(self):
        """The builder rowstate names for the lane is an author too."""
        row = self.approval(0, NEW_POLICY, OPUS, False)
        current, err = dispatches.snapshot()
        self.assertFalse(err)
        lane = current[row["id"]]["lane"]
        self.assertIn(row["id"], rowworld.non_author_approvals(current, {}))
        self.assertNotIn(row["id"], rowworld.non_author_approvals(
            current, {lane: frozenset((READER,))}))

    def test_an_OLD_tier_record_authorizes_as_recorded_and_keeps_the_family_rule(self):
        """Replay stability: the v2 record the previous writer minted under
        the family-only policy reads exactly as it did, and never takes the
        non-author rule, whatever model its seat ran. The control: the same
        seat's record under the new policy version does take it."""
        rid = self.approval(0, NEW_POLICY, OPUS, False)["id"]
        new = dispatches.snapshot()[0][rid]
        self.assertEqual(new["verdict_tier_evidence"]["approval_rule"],
                         "non-author")
        self.assertIsNone(dispatches.non_author_tier_error(new))
        rid = self.approval(1, OLD_POLICY, OPUS, False)["id"]
        recorded = dispatches.snapshot()[0][rid]
        old = copy.deepcopy(recorded)
        evidence = old["verdict_tier_evidence"]
        self.assertEqual(evidence.pop("approval_rule"), "family")
        evidence["v"] = 2
        old["verdict_tier_anchor"] = dispatches._proof_anchor(
            "verdict-tier-v1", evidence)
        self.assertEqual(dispatches.approval_tier_for_verdict(old),
                         ("ok", None))
        self.assertIsNotNone(dispatches.non_author_tier_error(old))
        current, err = dispatches.snapshot()
        self.assertFalse(err)
        current[old["id"]] = old
        self.assertNotIn(old["id"], rowworld.non_author_approvals(current, {}))


class RuntimeModelBindingTest(unittest.TestCase):
    """A `model:` selector names the model that answered, never a proxy alias."""

    POLICY = {"id": "p", "class": "certain",
              "_policy_confidence_valid": True,
              "_policy_source_valid": True,
              "policy_kind": "approval-tier",
              "policy_members": ["family:codex", "model:" + OPUS],
              "policy_reason": "the tier"}

    def test_a_proxy_ALIAS_cannot_earn_the_upstream_models_rule(self):
        """A Codex proxy accepts Claude's Opus alias but serves GPT. The alias
        is protocol routing, not the model that answered; treating it as the
        runtime model grants the Opus-only non-author rule to the GPT seat."""
        upstream = "gpt-6-astra"
        family_evidence = {
            "v": 3,
            "proxy_proof": {"model": OPUS,
                            "route": {"upstream_model": upstream}}}
        self.assertEqual(dispatches._evidence_model(family_evidence), upstream)

        author = {"resolved": {
            "family": "codex", "backend": "proxy", "model": OPUS,
            "provider": "openai", "upstream_model": upstream}}
        row = {key: None for key in dispatches._VERDICT_TIER_CONTEXT}
        row.update(id="a" * 32, recipient="proxy-reader",
                   verdict_author_runtime_evidence=author)
        reference = {"version": "b" * 32}
        with mock.patch.object(policy_history, "capture",
                               return_value=(reference, None)), \
                mock.patch.object(policy_history, "resolve", return_value=(
                    {"policy": self.POLICY}, None)):
            captured, err = dispatches._record_verdict_tier(row)
        self.assertIsNone(err, err)
        self.assertEqual(captured["verdict_tier_evidence"]["state"], "ok")
        self.assertEqual(captured["verdict_tier_evidence"]["approval_rule"],
                         "family")


class SelectorGrammarTest(unittest.TestCase):
    """The old grammar is what every v1 and v2 record was derived under."""

    POLICY = {"id": "p", "class": "certain", "_policy_confidence_valid": True,
              "_policy_source_valid": True, "policy_kind": "approval-tier",
              "policy_members": NEW_POLICY, "policy_reason": "the tier"}

    def test_the_old_grammar_reads_a_model_selector_as_malformed(self):
        state, why = verdict_tier.evaluate(self.POLICY, "opus-reader",
                                           {"claude"})
        self.assertEqual(dispatches.tier_unknown_kind(state), "damaged")
        self.assertIn("malformed selector", why)

    def test_the_model_aware_read_admits_the_named_model_only(self):  # noqa: VACUOUS_ASSERTION — both fixed non-empty model tables assert the exact admission tuple for every member
        for model in (OPUS, OPUS + "[1m]", "anthropic/" + OPUS + "[1m]"):
            with self.subTest(model=model):
                self.assertEqual(verdict_tier.admit(
                    self.POLICY, "opus-reader", {"claude"}, model),
                    ("ok", None, "non-author"))
        for model in (FABLE, None):
            with self.subTest(model=model):
                self.assertEqual(verdict_tier.admit(
                    self.POLICY, "opus-reader", {"claude"}, model),
                    ("ok", None, "family"))


class ApprovedDeriveTest(unittest.TestCase):
    """Derived APPROVED, the derivation half: rowstate reads the set."""

    def world(self, non_author):
        approval = _rowstate.review_row(
            "aprv" + "0" * 28, "split", recipient=READER, polarity="approve",
            reviewed_tip=_rowstate.APPROVED_TIP, gate=_rowstate.GATE_ID)
        return _rowstate.world(
            receipts_by_head={_rowstate.APPROVED_TIP: (_rowstate.receipt(
                _rowstate.APPROVED_TIP, _rowstate.APPROVED_TREE),)},
            branches={"lane/split": _rowstate.APPROVED_TIP},
            branch_commits={"lane/split": (_rowstate.APPROVED_TIP,)},
            approvals={"split": (approval,)},
            authors={"split": frozenset(("claude-builder",))},
            families={"claude-builder": frozenset(("claude",)),
                      READER: frozenset(("claude",))},
            non_author=non_author)

    def test_a_same_family_approve_the_record_admits_approves(self):
        """A same-family approve the non-author rule admits derives APPROVED;
        without the rule it stays GATED, as it always has."""
        row = _rowstate.build_row("d" * 32, "split")
        admitted = rowstate.derive(row, self.world(frozenset(
            ("aprv" + "0" * 28,))))
        self.assertEqual(admitted.state, rowstate.APPROVED)
        refused = rowstate.derive(row, self.world(frozenset()))
        self.assertTrue(refused.evidence)
        self.assertEqual(refused.state, rowstate.GATED)


def setUpModule():
    """No dispatch row this module writes walks the host's process table
    (task/3039; see tests._tmphome.pin_live_seats)."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()


if __name__ == "__main__":
    unittest.main()
