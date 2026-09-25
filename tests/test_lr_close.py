#!/usr/bin/env python3
"""helm lr close — the one terminal verb over the land-request lifecycle.

Hermetic, exactly as tests/test_landreq.py: HELM_HOME/HELM_CHAT_DIR are tmp
dirs, every git repo is minted in setUp, the real ledgers are never touched.
Shared discipline throughout this file:
  * every refusal test asserts LEDGER HISTORY LENGTH UNCHANGED — the effect,
    never the absence of a complaint;
  * every scan-shaped fixture seeds a MUST-HIT positive control before an
    empty result is trusted (the stranded prune recipe positively asserts the
    prune itself with a real `cat-file -e` rc 1 — no cat-file mocking, ever).
"""
import ast
import contextlib
import json
import os
import re
import subprocess
import threading
import time
import unittest
from unittest import mock

from helm import (carriageckpt, dispatches, eventledger, foldckpt, landreq,
                  obligation, projscope, proxywatch, rowstate, rowworld, seats,
                  vcs)
from tests._satellite_resolution import ledger_sources
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_landreq as _landreq
from tests.test_landreq import run


# THIS MODULE DOES NOT READ HOST LIVENESS. Same declaration as test_landreq's,
# and the full argument lives there; the short version is that every dispatch
# write here walks the host's whole process table (54,114 pids on the build
# node, 0.677s a walk) to consult a liveness these tests never assert on, and
# that made what they OBSERVED depend on what else was running beside them.
#
# DECLARED PER MODULE ON PURPOSE, not hoisted into a shared helper: "this
# module does not read host liveness" is a claim about THIS file that someone
# must re-check when its tests change, and a helper import would hide it.
#
# MODULE SCOPE, NOT `CloseBase`. This file inherits LandReqBase through
# CloseBase, and importing that module does NOT run its setUpModule — unittest
# runs module fixtures per module under test. A base-class hook would also miss
# every class here that does not descend from CloseBase.
_LIVE_SEATS_PATCH = None

from helm import proxywatch as _proxywatch_for_capture      # noqa: E402
_REAL_LIVE_SEATS = _proxywatch_for_capture._live_seats


def setUpModule():
    global _LIVE_SEATS_PATCH
    from helm import proxywatch
    _LIVE_SEATS_PATCH = mock.patch.object(
        proxywatch, "_live_seats", lambda: (set(), None, {}))
    _LIVE_SEATS_PATCH.start()


def tearDownModule():
    global _LIVE_SEATS_PATCH
    if _LIVE_SEATS_PATCH is not None:
        _LIVE_SEATS_PATCH.stop()
    # THE GLOBAL GOES BACK TO WHAT IMPORT LEFT: other modules import from this
    # one, so a stopped patcher left here is data they can reach (task/3039).
    _LIVE_SEATS_PATCH = None


class TheLivenessStandInIsInEffectHereTooTest(unittest.TestCase):
    """Per-module control: the stand-in must be in effect in THIS module.

    test_landreq having one proves nothing about this file — its setUpModule
    does not run for these tests. Inherits `unittest.TestCase` directly so a
    future base-class refactor cannot quietly drop it.
    """

    def test_the_bound_census_is_NOT_the_real_one(self):  # noqa: VACUOUS_ASSERTION — identity against the import-time capture is the positive control; a value assertion cannot discriminate because the build node's real census also returns an empty fleet. Same shape mutation-tested in test_landreq.
        from helm import proxywatch
        self.assertIsNot(
            proxywatch._live_seats, _REAL_LIVE_SEATS,
            "the liveness stand-in is NOT in effect in test_lr_close — this "
            "module is reading the real host again")


class CloseBase(_landreq.LandReqBase):
    """Fixture verbs shared by every close-reason suite."""

    def landed_then_trunk_edits(self, same_line):
        """Trunk CARRIES the reviewed delta by ancestry and has edited the same
        FILE since. `same_line` is the one input that decides whether the
        three-way merge can still reproduce trunk: a later edit to the reviewed
        delta's own line conflicts with it, one far from it does not. Both
        halves leave the reviewed postimage absent from trunk, which is what
        makes this pair the control for `_postimages_at_head`.

        ON THE SHARED BASE because two suites need this one shape: the witness
        comparison, and the checkpoint door whose saving it is the only fixture
        here that actually pays a `merge-tree`."""
        path = os.path.join(self.repo, "wide")
        rows = ["L%d" % n for n in range(40)]

        def write(lines):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            self.git("add", "wide")
            self.git("commit", "-q", "-m", "w")
            return self.git("rev-parse", "HEAD")

        write(rows)
        base = self.git("rev-parse", "HEAD")
        rows[1] = "L1 reviewed"
        reviewed = write(list(rows))
        rows[1 if same_line else 38] = "later"
        write(rows)
        return base, reviewed

    def close_event(self, rid):
        """The LAST close event this row appended, read back from the ledger.

        A replay-rejection arm must mutate what the REAL WRITER WROTE. A
        hand-built event proves only that the validator refuses a shape the
        test author invented, which is the one shape no forger has to use.
        """
        rows = [e for e in eventledger.events(dispatches.ledger_path())
                if e.get("event") == "close" and e.get("id") == rid]
        self.assertTrue(rows, "the close verb appended no close event")
        return rows[-1]

    def reanchored(self, event, **changes):
        """A forger's best move: mutate the witness AND re-anchor it.

        An arm that edits the witness and leaves the OLD anchor in place tests
        the anchor comparison and never the field it claims to test — the
        anchor check fires first and every later assertion rides on it.
        """
        witness = json.loads(json.dumps(event["content_witness"]))
        witness.update(changes)
        return dict(event, content_witness=witness,
                    content_witness_anchor=dispatches._proof_anchor(
                        landreq.CONTENT_ALGORITHM, witness))

    def pair_cases(self, event, other_sha):
        """(expected-substring, forged-event) for every way a pair can lie.

        THE SUBSTITUTE MUST ACTUALLY SUBSTITUTE. The first version of this
        passed `rev-parse <trunk>` as the stand-in sha — which is precisely
        what the witness records as its trunk, so the "wrong trunk" case
        mutated nothing, the validator correctly returned None, and the arm
        reported that the ledger ADMITS a forged pair. The forgery was the
        no-op. These three assertions make a no-op mutation fail loudly here
        rather than as a false finding about the code under test.
        """
        witness = event["content_witness"]
        self.assertNotEqual(other_sha, witness["source"]["commit"],
                            "the wrong-source case would mutate nothing")
        self.assertNotEqual(other_sha, witness["trunk"],
                            "the wrong-trunk case would mutate nothing")
        self.assertNotEqual(other_sha, witness["carrier"]["commit"],
                            "the substitute sha is the carrier itself")
        both, no_witness, no_anchor = dict(event), dict(event), dict(event)
        both.pop("content_witness"), both.pop("content_witness_anchor")
        no_witness.pop("content_witness")
        no_anchor.pop("content_witness_anchor")
        source = json.loads(json.dumps(event["content_witness"]["source"]))
        source["commit"] = other_sha
        carrier = json.loads(json.dumps(event["content_witness"]["carrier"]))
        carrier["commit"] = "not-a-sha"
        # RE-ANCHORED SCHEMA FORGERIES (the second finding). Every one
        # of these leaves source, trunk and carrier COMMIT untouched and
        # re-seals the anchor, so a validator that checks only the bindings
        # plus its checksum admits them — and `_content_equivalent_replay`
        # would then reject the very witness the ledger called terminal.
        bad_parent = json.loads(json.dumps(event["content_witness"]["source"]))
        bad_parent["parents"] = ["not-a-sha"]
        short_tree = json.loads(json.dumps(event["content_witness"]["source"]))
        short_tree["tree"] = short_tree["tree"][:12]
        extra_key = json.loads(json.dumps(event["content_witness"]["source"]))
        extra_key["author"] = "someone"
        wrong_tree = json.loads(json.dumps(event["content_witness"]["source"]))
        wrong_tree["tree"] = other_sha          # a REAL full id, wrong object
        wrong_carrier = json.loads(json.dumps(event["content_witness"]["carrier"]))
        wrong_carrier["commit"] = other_sha     # a REAL commit, not the carrier
        witness = event["content_witness"]
        surplus = dict(witness, unexpected="field")
        short = {k: v for k, v in witness.items() if k != "matches"}
        return (("needs both", both),
                ("needs both", no_witness),
                ("needs both", no_anchor),
                ("does not bind", dict(event, content_witness_anchor="0" * 32)),
                ("source other than the reviewed tip",
                 self.reanchored(event, source=source)),
                ("different trunk", self.reanchored(event, trunk=other_sha)),
                ("no full-id commit",
                 self.reanchored(event, carrier=carrier)),
                # v / algorithm / matches — the three the replay enforces and
                # admission did not.
                ("not a v1 record", self.reanchored(event, v=2)),
                # True == 1 in Python, so a truthiness or != test admits this.
                ("not a v1 record", self.reanchored(event, v=True)),
                ("written by", self.reanchored(
                    event, algorithm="content-equivalent-v2")),
                ("non-unique carrier", self.reanchored(event, matches=2)),
                ("non-unique carrier", self.reanchored(event, matches=True)),
                # record shape: a tree or parent nobody can compare later
                ("malformed parent", self.reanchored(event, source=bad_parent)),
                ("no full-id tree", self.reanchored(event, source=short_tree)),
                ("{commit,parents,tree} record",
                 self.reanchored(event, source=extra_key)),
                # the witness's own key set
                ("do not match the v1 schema",
                 dict(event, content_witness=surplus,
                      content_witness_anchor=dispatches._proof_anchor(
                          landreq.CONTENT_ALGORITHM, surplus))),
                ("do not match the v1 schema",
                 dict(event, content_witness=short,
                      content_witness_anchor=dispatches._proof_anchor(
                          landreq.CONTENT_ALGORITHM, short))),
                ("has no trunk_ref", self.reanchored(event, trunk_ref="")),
                # THE IDENTITY VALUES. Replay RECOMPUTES both and
                # compares exactly, so "non-empty" was never the language:
                # garbage here, re-anchored, previously produced a TERMINAL
                # close whose own replay returns False.
                ("payload_digest is not a 64-hex content identity",
                 self.reanchored(event, payload_digest="garbage")),
                ("newline_fingerprint is not a 64-hex content identity",
                 self.reanchored(event, newline_fingerprint="garbage")),
                # EMPTY is what the WRITER emits for a true empty commit and
                # what REPLAY refuses as unreadable — so admission must refuse
                # it too, or it admits a proof that cannot replay.
                ("payload_digest is not a 64-hex content identity",
                 self.reanchored(event, payload_digest="EMPTY")),
                # A 63-hex near-miss, because a length-blind check passes it.
                ("payload_digest is not a 64-hex content identity",
                 self.reanchored(event, payload_digest="a" * 63)),
                # SEMANTIC FORGERIES (the fourth). Every one of these is
                # SHAPE-VALID and re-anchored, so no syntax check can refuse
                # them; only running the witness through its own replay can.
                ("does not replay against its own bound repository",
                 self.reanchored(event, payload_digest="0" * 64)),
                ("does not replay against its own bound repository",
                 self.reanchored(event, source=wrong_tree)),
                ("does not replay against its own bound repository",
                 self.reanchored(event, carrier=wrong_carrier)))

    def verdict_row(self, polarity="approve", ref=None, lane=None,
                    recipient="codex-3"):
        """One verdict row of the named polarity bound to `ref` (default: the
        divergent side tip, NOT on trunk)."""
        tip = ref or self.side
        lane = lane or "lane/close-%s" % (polarity or "undeclared")
        row, why, sent = dispatches.send(
            recipient, lane, "review " + lane, tip, repo=self.repo,
            key="key-" + lane, sign=False, new_work=True)
        self.assertIsNone(why)
        self.assertTrue(sent)
        if polarity is None:
            self.assertTrue(eventledger.append(dispatches.ledger_path(), {
                "v": 3, "event": "verdict", "seq": row["seq"] + 1,
                "id": row["id"], "ts": dispatches.pk.now_ts(),
                "reviewed_tip": tip, "verdict_ref": "legacy review"}))
        else:
            out, err = self.mark_verdict(row["id"], tip, "findings",
                                               polarity=polarity)
            self.assertIsNone(err)
        return row

    def history_len(self, rid):
        return len(dispatches.history(rid))

    def pruned_verdict_row(self, polarity="fix", lane="lane/pruned-work"):
        """A verdict row whose reviewed tip is a DESTROYED object: commit on a
        temp branch, dispatch+verdict against it, then prune the branch."""
        base = self.git("rev-parse", self.main)
        self.git("checkout", "-q", "-b", "doomed", base)
        with open(os.path.join(self.repo, "doomed.txt"), "w",
                  encoding="utf-8") as f:
            f.write("work that the rewrite will destroy\n")
        self.git("add", "doomed.txt")
        self.git("commit", "-q", "-m", "doomed work")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        row = self.verdict_row(polarity=polarity, ref=tip, lane=lane)
        self.prune(tip, "refs/heads/doomed")
        return row, tip

    def legacy_chainless(self, row):
        """Rewrite one opening as a real pre-chain legacy dispatch."""
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for event in events:
                if event.get("id") == row["id"] \
                        and event.get("event") == "dispatch":
                    event.pop("chain_root", None)
                    event.pop("supersedes", None)
                f.write(json.dumps(event, separators=(",", ":")) + "\n")

    def subsumption_case(self, original_polarity="approve",
                         confirmation_polarity="approve", same_chain=True,
                         marked=True, confirmation_landed=True,
                         original_landed=False, gate_capable=False,
                         gate_prefixed=False, original_kind="review",
                         confirmation_kind="review", original_legacy=False,
                         confirmation_statement=None):
        target = dispatches.add(
            "codex-3", "lane/evolved-r1", ref=self.side, repo=self.repo,
            kind=original_kind, notify=False, new_work=True)
        out, err = self.mark_verdict(
            target["id"], self.side, "original verdict",
            polarity=original_polarity)
        self.assertIsNone(err)
        if original_legacy:
            self.legacy_chainless(target)
        if original_landed:
            self.git("merge", "--no-edit", "-q", "side")
        if confirmation_landed:
            confirmation_tip = self.commit("founder semantics reimplemented")
        else:
            self.git("checkout", "-q", "-b", "evolved", self.main)
            confirmation_tip = self.commit("founder semantics reimplemented")
            self.git("checkout", "-q", self.main)
        evidence = confirmation_statement or (
            "Subsumption verified evolved founder semantics on trunk now"
            if marked else "Founder semantics are present on trunk now")
        if gate_prefixed:
            evidence = "gate:%s 42 OK/0 at deadbeef. %s" % (
                "a" * 16, evidence)
        kw = {"supersedes": target["id"]} if same_chain else {"new_work": True}
        confirmation = dispatches.add(
            "claude-reviewer", "lane/evolved-r2", ref=confirmation_tip,
            repo=self.repo, kind=confirmation_kind, notify=False, **kw)
        if gate_capable:
            with mock.patch.object(
                    dispatches, "GATE_CAPS", (dispatches.GATE_CAP_RECEIPT,)), \
                    mock.patch.object(
                        dispatches.gate, "bind",
                        return_value=("VERIFIED", "a" * 16, "test receipt")):
                out, err = self.mark_verdict(
                    confirmation["id"], confirmation_tip, evidence,
                    polarity=confirmation_polarity)
        else:
            out, err = self.mark_verdict(
                confirmation["id"], confirmation_tip, evidence,
                polarity=confirmation_polarity)
        self.assertIsNone(err)
        return target, confirmation, confirmation_tip

    def family_evidence(self, same=False, unknown=None):
        """Historical v1 fixture: replay must keep accepting already-landed proof."""
        def families(identity):
            family = "codex" if identity == "integrator" else \
                ("codex" if same else "claude") \
                if identity == "claude-reviewer" else None
            values = set() if unknown == identity or family is None else {family}
            evidence = {"v": 1, "identity": identity,
                        "minted_families": sorted(values),
                        "roster_family": None, "roster_verified": False}
            return (values, evidence,
                    dispatches._subsumed_family_anchor(evidence), None)
        return mock.patch.object(
            dispatches, "_approval_identity_family_evidence",
            side_effect=families)

    def verified_runtime(self, identity, family, harness="claude",
                         backend="native", session=None):
        """Self-write one verified roster runtime without laundering identity."""
        old = os.environ.get("HELM_CHAT_NAME")
        os.environ["HELM_CHAT_NAME"] = identity
        try:
            runtime = {"family": family, "agent_harness": harness,
                       "backend": backend}
            seats.write_roster(identity, session=session, runtime=runtime)
        finally:
            if old is None:
                os.environ.pop("HELM_CHAT_NAME", None)
            else:
                os.environ["HELM_CHAT_NAME"] = old
        self.assertIs(seats.roster()[identity].get("runtime_verified"), True)

    def verified_proxy(self, identity):
        """A proxy-labelled row whose authority is only its measured route."""
        session = "proxy-session-" + identity
        self.verified_runtime(identity, "costume", backend="proxy",
                              session=session)
        proof = {"v": 2, "session": session, "agent_harness": "claude",
                 "agent_pid": 4101,
                 "agent_starttime": 701, "model": "ds4-pro",
                 "local_base_url": "http://127.0.0.1:8360",
                 "proxy_pid": 4201, "proxy_identity": "proc:702",
                 "proxy_config": "/safe/config.yaml",
                 "config_sha256": "a" * 64,
                 "route": {"alias": "ds4-pro", "provider": "opencode-go",
                           "upstream_model": "deepseek-v4-pro",
                           "base_url": "https://opencode.ai/zen/go/v1"},
                 "observed_at": 1000,
                 "canary": {"state": "HEALTHY", "status": 200}}
        # DERIVED FROM THE PROOF, never hand-written to match it. The minter
        # requires the caller's runtime to EQUAL its own re-derivation, so a
        # fixture that transcribes the fields it expects is pinned to today's
        # derivation and reddens the moment that derivation learns anything --
        # `model` is one such field. The single production caller
        # (`proxywatch._stamp_proxy_runtime_proofs`) derives and hands the
        # result straight back, so deriving here IS the shape production
        # sends, and a hand-assembled dict is a shape no caller produces. The
        # three fields such a dict would name are asserted below instead:
        # they are the CHECK, not the feed.
        runtime, err = proxywatch._proxy_proof_runtime(proof)
        self.assertIsNone(err, err)
        family = runtime["family"]
        resolved, err = proxywatch._proxy_proof_family(proof)
        self.assertIsNone(err, err)
        self.assertEqual(
            (family, runtime.get("agent_harness"), runtime.get("backend")),
            (resolved, "claude", "proxy"),  # noqa: SEAT_NAME — 'claude' here is the agent_harness AXIS value, the same literal this fixture's own proof carries, and never a seat identity
            "control: the derivation still names the three fields this "
            "fixture used to hand-write, and its family agrees with the "
            "independent resolver -- so deriving is not a way of agreeing "
            "with whatever came back")
        entry, err = seats.stamp_proxy_runtime(session, runtime, proof)
        self.assertIsNone(err, err)
        self.assertEqual(entry["source"], "proxywatch")
        proofs = getattr(self, "_proxy_family_proofs", {})
        proofs[session] = (family, proof, None)
        self._proxy_family_proofs = proofs

    def proxy_families(self):
        proofs = getattr(self, "_proxy_family_proofs", {})

        def snapshot(session, expected_proof=None):
            result = proofs.get(
                session, (None, None, "no session-matched proof"))
            if result[1] is not None and expected_proof != result[1]:
                return None, None, "roster proof did not match measured proof"
            return result

        return mock.patch.object(
            proxywatch, "proxy_runtime_snapshot", side_effect=snapshot)


class CloseDeliveredReportTest(CloseBase):
    """A report deliverable closes BUILD work without inventing a review/land."""

    ARTIFACT = "artifact:reports/close-door-audit.json#blake2b:0123456789abcdef"
    REPORT = "a1b2c3d4e5f6"
    EVIDENCE = "report artifact handed to the integrator"

    def build_row(self, lane="lane/report-build", kind="build", **kwargs):
        return self.dispatch(lane=lane, kind=kind, **kwargs)

    def close_report(self, row, **kwargs):
        values = {"artifact_ref": self.ARTIFACT, "report_ref": self.REPORT,
                  "evidence": self.EVIDENCE}
        values.update(kwargs)
        return landreq.close(row["id"], "delivered-report", **values)

    def correction_event(self, row, **changes):
        state = dispatches.snapshot()[0][row["id"]]
        event = {"v": 3, "event": "close-correction",
                 "seq": state["seq"] + 1, "id": row["id"],
                 "ts": dispatches.pk.now_ts(),
                 "close_reason": "delivered-report", "close_proof_version": 1,
                 "artifact_ref": self.ARTIFACT, "report_ref": self.REPORT,
                 "close_evidence": self.EVIDENCE, "corrects_event": "cancel",
                 "corrects_reason": state.get("cancel_reason")}
        event.update(changes)
        return event

    def test_open_build_closes_as_delivered_report_without_review_or_land_claims(self):
        row = self.build_row()
        out, err = self.close_report(row)
        self.assertIsNone(err)
        self.assertEqual(out["state"], "DELIVERED_REPORT")
        self.assertTrue(out["terminal"])
        self.assertEqual(out["owed_by"], "nobody")
        self.assertFalse(out["stalled"])
        self.assertFalse(out["landed"])
        self.assertFalse(out["merged_local"])
        self.assertEqual(out["land_state"], "NOT_CLAIMED")
        self.assertEqual(out["base_sha"], self.side)
        self.assertIsNone(out["reviewed_tip"])
        self.assertIsNone(out["polarity"])
        self.assertEqual(out["artifact_ref"], self.ARTIFACT)
        self.assertEqual(out["report_ref"], self.REPORT)
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(replayed["status"], "closed")
        self.assertEqual(replayed["tip"], self.side)
        self.assertNotIn("reviewed_tip", replayed)
        event = dispatches.history(row["id"])[-1]
        self.assertEqual(event["event"], "close")
        # Same whole-object rule, same one subtraction: `close_actor` is
        # environment-dependent provenance and sits in no schema.
        self.assertEqual(set(event) - {dispatches.CLOSE_ACTOR_FIELD},
                         dispatches._DELIVERED_REPORT_EVENT_FIELDS)

    def test_required_refs_and_evidence_have_typed_bounded_identity_shapes(self):
        row = self.build_row()
        base = ["close", row["id"][:12], "--reason", "delivered-report"]
        required = (("--artifact-ref", self.ARTIFACT),
                    ("--report-ref", self.REPORT),
                    ("--evidence", self.EVIDENCE))
        for missing, _value in required:
            args = base[:]
            for flag, value in required:
                if flag != missing:
                    args.extend((flag, value))
            rc, _out, err = run(args)
            self.assertEqual(rc, 2, (missing, err))
            self.assertIn("requires --artifact-ref", err)
        before = self.history_len(row["id"])
        cases = (("artifact_ref", " \t", "artifact ref is required"),
                 ("artifact_ref", "x", "<scheme>:<nonempty-value>"),
                 ("artifact_ref", "1bad:value", "URI-style scheme"),
                 ("artifact_ref", "artifact:", "<scheme>:<nonempty-value>"),
                 ("artifact_ref", "artifact:has space", "no whitespace"),
                 ("report_ref", "chat\npost", "printable line"),
                 ("report_ref", "y", "full 12-character lowercase hex"),
                 ("report_ref", "ABCDEF123456", "full 12-character lowercase hex"),
                 ("report_ref", "g" * 12, "full 12-character lowercase hex"),
                 ("artifact_ref", "a:" + "x" * 511, "at most 512"),
                 ("report_ref", "r" * 257, "at most 256"),
                 ("evidence", "e" * 257, "at most 256"))
        for field, value, expected in cases:
            with self.subTest(field=field, expected=expected):
                out, err = self.close_report(row, **{field: value})
                self.assertIsNone(out)
                self.assertIn(expected, err)
                self.assertEqual(self.history_len(row["id"]), before)
        for field, value in (("tip", self.side), ("repo", self.repo),
                             ("trunk", self.main)):
            out, err = self.close_report(row, **{field: value})
            self.assertIsNone(out)
            self.assertIn("no tip override, repository, or trunk proof", err)
            self.assertEqual(self.history_len(row["id"]), before)

    def test_only_open_explicit_build_rows_are_supported(self):
        review = self.build_row(lane="lane/report-review", kind="review")
        before = self.history_len(review["id"])
        out, err = self.close_report(review)
        self.assertIsNone(out)
        self.assertIn("explicit BUILD", err)
        self.assertEqual(self.history_len(review["id"]), before)

        legacy = self.build_row(lane="lane/report-legacy", kind=None)
        before = self.history_len(legacy["id"])
        out, err = self.close_report(legacy)
        self.assertIsNone(out)
        self.assertIn("explicit BUILD", err)
        self.assertEqual(self.history_len(legacy["id"]), before)

        cancelled = self.build_row(lane="lane/report-cancelled")
        dispatches.mark_cancel(cancelled["id"], "owner stopped the work")
        before = self.history_len(cancelled["id"])
        out, err = self.close_report(cancelled)
        self.assertIsNone(out)
        self.assertIn("OPEN BUILD", err)
        self.assertEqual(self.history_len(cancelled["id"]), before)

    def test_forged_close_event_is_inert_on_replay(self):
        row = self.build_row()
        state = dispatches.snapshot()[0][row["id"]]
        base = {"v": 3, "event": "close", "seq": state["seq"] + 1,
                "id": row["id"], "ts": dispatches.pk.now_ts(),
                "close_reason": "delivered-report", "close_proof_version": 1,
                "artifact_ref": self.ARTIFACT, "report_ref": self.REPORT,
                "close_evidence": self.EVIDENCE}
        for forged in (dict(base, report_ref="bad\nref"),
                       dict(base, forged=True),
                       {key: value for key, value in base.items()
                        if key != "artifact_ref"}):
            with self.subTest(keys=sorted(forged)):
                eventledger.append_unlocked(dispatches.ledger_path(), forged)
                replayed = dispatches.snapshot()[0][row["id"]]
                self.assertEqual(replayed["status"], "open")
                self.assertIsNone(replayed.get("close_reason"))

    def test_retry_identity_is_exact_artifact_report_and_evidence(self):
        row = self.build_row()
        first, err = self.close_report(row)
        self.assertIsNone(err)
        before = self.history_len(row["id"])
        again, err = self.close_report(row)
        self.assertIsNone(err)
        self.assertEqual(again, first)
        self.assertEqual(self.history_len(row["id"]), before)
        for field, value in (("artifact_ref", self.ARTIFACT + ".v2"),
                             ("report_ref", "f0e1d2c3b4a5"),
                             ("evidence", self.EVIDENCE + " twice")):
            out, err = self.close_report(row, **{field: value})
            self.assertIsNone(out)
            self.assertIn("retired once", err)
            self.assertEqual(self.history_len(row["id"]), before)

    def test_delivered_report_successor_absorbs_parent_chain_debt(self):
        parent = self.build_row(lane="lane/report-chain-parent")
        dispatches._mark_delivered(parent["id"], "post-parent")
        self.age(parent["id"], 3 * 86400)
        child = self.build_row(
            lane="lane/report-chain-child", supersedes=parent["id"])
        out, err = self.close_report(child)
        self.assertIsNone(err)
        self.assertEqual(out["state"], "DELIVERED_REPORT")
        lrs, raw, unavailable = landreq.project_raw()
        self.assertIsNone(unavailable)
        self.assertTrue(lrs[parent["id"]]["stalled"])
        self.assertNotIn(parent["id"],
                         [lr["id"] for lr in landreq._stalled_rows(lrs, raw)])

    def test_cli_projection_exposes_refs_and_no_land_claim(self):
        row = self.build_row()
        rc, out, err = run([
            "close", row["id"][:12], "--reason", "delivered-report",
            "--artifact-ref", self.ARTIFACT, "--report-ref", self.REPORT,
            "--evidence", self.EVIDENCE, "--json"])
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertEqual(data["state"], "DELIVERED_REPORT")
        self.assertEqual(data["land_state"], "NOT_CLAIMED")
        rc, shown, err = run(["show", row["id"][:12]])
        self.assertEqual(rc, 0, err)
        self.assertIn("BUILD — CLOSED (DELIVERED_REPORT)", shown)
        self.assertIn(self.ARTIFACT, shown)
        self.assertIn(self.REPORT, shown)
        self.assertIn("NOT CLAIMED", shown)
        rc, listed, err = run(["list", "--all"])
        self.assertEqual(rc, 0, err)
        self.assertIn("DELIVERED_REPORT", listed)
        self.assertIn("CLOSED (DELIVERED_REPORT)", listed)

    def test_historical_cancel_requires_explicit_annotation_and_preserves_history(self):
        row = self.build_row(lane="lane/historical-report")
        reason = "old operator called this delivered in prose"
        cancelled, err = dispatches.mark_cancel(row["id"], reason)
        self.assertIsNone(err)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertNotIn(row["id"], landreq.project()[0])
        rc, out, err = run([
            "annotate-delivered-report", row["id"][:12],
            "--artifact-ref", self.ARTIFACT, "--report-ref", self.REPORT,
            "--evidence", self.EVIDENCE, "--json"])
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertEqual(data["state"], "DELIVERED_REPORT")
        self.assertTrue(data["delivered_report_correction"])
        self.assertEqual(data["cancel_reason"], reason)
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(replayed["status"], "closed")
        self.assertEqual(replayed["cancel_reason"], reason)
        history = dispatches.history(row["id"])
        self.assertEqual([event["event"] for event in history[-2:]],
                         ["cancel", "close-correction"])
        self.assertEqual(history[-2]["reason"], reason)
        self.assertEqual(history[-1]["corrects_reason"], reason)
        before = self.history_len(row["id"])
        same, err = landreq.annotate_delivered_report(
            row["id"], self.ARTIFACT, self.REPORT, self.EVIDENCE)
        self.assertIsNone(err)
        self.assertEqual(same["state"], "DELIVERED_REPORT")
        self.assertEqual(self.history_len(row["id"]), before)
        conflict, err = landreq.annotate_delivered_report(
            row["id"], self.ARTIFACT + ".v2", self.REPORT, self.EVIDENCE)
        self.assertIsNone(conflict)
        self.assertIn("refusing different artifact/report evidence", err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_annotation_refuses_unsupported_rows_and_forged_events(self):
        open_row = self.build_row(lane="lane/report-open-annotation")
        out, err = landreq.annotate_delivered_report(
            open_row["id"], self.ARTIFACT, self.REPORT, self.EVIDENCE)
        self.assertIsNone(out)
        self.assertIn("cancelled BUILD", err)

        review = self.build_row(lane="lane/report-review-annotation", kind="review")
        dispatches.mark_cancel(review["id"], "review abandoned")
        before = self.history_len(review["id"])
        out, err = landreq.annotate_delivered_report(
            review["id"], self.ARTIFACT, self.REPORT, self.EVIDENCE)
        self.assertIsNone(out)
        self.assertIn("explicit BUILD", err)
        self.assertEqual(self.history_len(review["id"]), before)

        cancelled = self.build_row(lane="lane/report-forged-correction")
        dispatches.mark_cancel(cancelled["id"], "arbitrary cancellation")
        forged = self.correction_event(cancelled, forged=True)
        eventledger.append_unlocked(dispatches.ledger_path(), forged)
        replayed = dispatches.snapshot()[0][cancelled["id"]]
        self.assertEqual(replayed["status"], "cancelled")
        self.assertIsNone(replayed.get("close_reason"))

    def test_legacy_completion_hints_are_reason_only_and_unverified(self):
        completed = self.build_row(lane="lane/hint-completed")
        dispatches.mark_cancel(
            completed["id"],
            "DELIVERED not abandoned: report artifact handed to the integrator")
        negative = self.build_row(lane="lane/hint-negative")
        dispatches.mark_cancel(
            negative["id"],
            "integrator took the lane; r6 delivered neither specified item")
        whole_row_only = self.build_row(
            lane="lane/delivered-only-outside-reason",
            note="DELIVERED appears in this note, not the cancellation")
        dispatches.mark_cancel(whole_row_only["id"], "operator stopped the work")
        undelivered = self.build_row(lane="lane/hint-undelivered")
        dispatches.mark_cancel(undelivered["id"], "work remains undelivered")

        rows, unavailable = landreq.legacy_completion_hints()
        self.assertIsNone(unavailable)
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(set(by_id), {completed["id"], negative["id"]})
        for rid in (completed["id"], negative["id"]):
            self.assertEqual(len(by_id[rid]["id"]), 32)
            self.assertEqual(by_id[rid]["classification"], "UNVERIFIED")
            self.assertEqual(by_id[rid]["delivered_report_eligible"], "UNKNOWN")
        self.assertIn("not authority", landreq._LEGACY_COMPLETION_WHY)
        self.assertIn("negative", landreq._LEGACY_COMPLETION_WHY)
        self.assertIn("delivered neither", by_id[negative["id"]]["cancel_reason"])

    def test_legacy_completion_hints_exclude_nonbuild_and_non_cancelled_rows(self):
        open_build = self.build_row(lane="lane/hint-open")
        review = self.build_row(lane="lane/hint-review", kind="review")
        dispatches.mark_cancel(review["id"], "DELIVERED review findings")
        legacy = self.build_row(lane="lane/hint-legacy", kind=None)
        dispatches.mark_cancel(legacy["id"], "DELIVERED historical work")
        rows, unavailable = landreq.legacy_completion_hints()
        self.assertIsNone(unavailable)
        ids = {row["id"] for row in rows}
        self.assertNotIn(open_build["id"], ids)
        self.assertNotIn(review["id"], ids)
        self.assertNotIn(legacy["id"], ids)

    def test_hint_query_is_read_only_and_annotation_is_the_only_promotion(self):
        row = self.build_row(lane="lane/hint-read-only")
        reason = "DELIVERED: report is ready for audit"
        dispatches.mark_cancel(row["id"], reason)
        path = dispatches.ledger_path()
        with open(path, "rb") as f:
            before = f.read()
        rows, unavailable = landreq.legacy_completion_hints()
        self.assertIsNone(unavailable)
        self.assertEqual([item["id"] for item in rows], [row["id"]])
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(replayed["status"], "cancelled")
        self.assertIsNone(replayed.get("close_reason"))
        rc, out, err = run(["legacy-completion-hints", "--json"])
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["rows"][0]["id"], row["id"])
        self.assertIn("not authority", payload["why"])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)
        promoted, err = landreq.annotate_delivered_report(
            row["id"], self.ARTIFACT, self.REPORT, self.EVIDENCE)
        self.assertIsNone(err)
        self.assertEqual(promoted["state"], "DELIVERED_REPORT")
        self.assertEqual(landreq.legacy_completion_hints()[0], [])

    def test_cancel_reason_prose_never_infers_delivered_report(self):
        row = self.build_row(lane="lane/report-no-prose-inference")
        reason = ("delivered-report artifact=%s report=%s evidence=%s" %
                  (self.ARTIFACT, self.REPORT, self.EVIDENCE))
        dispatches.mark_cancel(row["id"], reason)
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(replayed["status"], "cancelled")
        self.assertIsNone(replayed.get("close_reason"))
        self.assertNotIn(row["id"], landreq.project()[0])


class CloseSubsumedTest(CloseBase):
    """A later same-chain cross-family approval confirms evolved trunk work."""

    def close_case(self, target, confirmation, dry_run=False):
        with self.family_evidence():
            return landreq.close(
                target["id"], "subsumed", evidence=confirmation["id"][:12],
                trunk=self.main, dry_run=dry_run)

    def assert_subsumption_ready(self, target, confirmation):
        """Positive control: every clause passes before one test mutates one."""
        dry, err = self.close_case(target, confirmation, dry_run=True)
        self.assertIsNone(err)
        self.assertEqual(dry["id"], target["id"])
        self.assertEqual(dry["confirmation_id"], confirmation["id"])
        self.assertEqual(dry["reason"], "subsumed")
        return dry

    def proof_args(self, dry):
        return {"confirmation_id": dry["confirmation_id"],
                "confirmation_tip": dry["confirmation_tip"],
                "confirmation_ref": dry["confirmation_ref"],
                "original_author": dry["original_author"],
                "confirmation_recipient": dry["confirmation_recipient"],
                "original_author_family": dry["original_author_family"],
                "confirmation_recipient_family":
                dry["confirmation_recipient_family"],
                "original_verdict_anchor": dry["original_verdict_anchor"],
                "confirmation_verdict_anchor":
                dry["confirmation_verdict_anchor"],
                "original_family_evidence": dry["original_family_evidence"],
                "confirmation_family_evidence":
                dry["confirmation_family_evidence"],
                "original_family_anchor": dry["original_family_anchor"],
                "confirmation_family_anchor": dry["confirmation_family_anchor"],
                "confirmation_tier_state": dry["confirmation_tier_state"],
                "confirmation_gate_requirement":
                dry["confirmation_gate_requirement"],
                "confirmation_gate": dry["confirmation_gate"],
                "confirmation_approval_anchor":
                dry["confirmation_approval_anchor"],
                "closing_repo_id": dry["closing_repo_id"],
                "closing_trunk_ref": dry["closing_trunk_ref"],
                "closing_trunk_sha": dry["closing_trunk_sha"],
                "proof_mode": dry["proof_mode"],
                "original_proof_mode": dry["original_proof_mode"]}

    def event_from_dry(self, target, confirmation, dry):
        event = {"v": 3, "event": "close",
                 "seq": dispatches.snapshot()[0][target["id"]]["seq"] + 1,
                 "id": target["id"], "ts": dispatches.pk.now_ts(),
                 "close_reason": "subsumed", "reviewed_tip": self.side,
                 "close_evidence": confirmation["id"][:12],
                 "close_proof_version": 1}
        for key, value in self.proof_args(dry).items():
            event["close_proof_mode" if key == "proof_mode" else key] = value
        return event

    def test_happy_path_persists_confirmation_families_and_proofs(self):
        target, confirmation, tip = self.subsumption_case()
        out, err = self.close_case(target, confirmation)
        self.assertIsNone(err)
        self.assertEqual(out["close_reason"], "subsumed")
        self.assertEqual(out["confirmation_id"], confirmation["id"])
        self.assertEqual(out["confirmation_tip"], tip)
        self.assertEqual(out["original_author_family"], "codex")
        self.assertEqual(out["confirmation_recipient_family"], "claude")
        self.assertEqual(out["close_proof_mode"], "ancestor")
        self.assertEqual(out["original_proof_mode"], "absent")
        self.assertEqual(out["original_author"], "integrator")
        self.assertEqual(out["confirmation_recipient"], "claude-reviewer")
        self.assertEqual(out["confirmation_tier_state"], "none")
        self.assertEqual(out["confirmation_gate_requirement"], "none")
        for key in ("original_verdict_anchor", "confirmation_verdict_anchor",
                    "original_family_anchor", "confirmation_family_anchor",
                    "confirmation_approval_anchor"):
            self.assertRegex(out[key], r"^[0-9a-f]{32}$", key)
        card = landreq.card(out)
        self.assertEqual(card["confirmation_id"], confirmation["id"])
        self.assertEqual(card["confirmation_tip"], tip)
        self.assertEqual(card["original_author_family"], "codex")
        self.assertEqual(card["confirmation_recipient_family"], "claude")
        self.assertEqual(card["confirmation_tier_state"], "none")
        self.assertEqual(card["confirmation_approval_anchor"],
                         out["confirmation_approval_anchor"])
        self.assertIn("CLOSED (SUBSUMED)", landreq._line(out))
        shown = landreq._render_show(out)
        self.assertIn("CLOSED (SUBSUMED)", shown)
        self.assertIn(confirmation["id"], shown)
        self.assertIn("codex -> claude", shown)

    def test_claude_label_running_codex_is_refused_as_same_family(self):  # noqa: VACUOUS_ASSERTION — two verified codex rows positively arm the equality gate
        """The label says claude; the verified serving model says codex.

        This is the exact subagent laundering threat: different visible names and
        a Claude harness grant zero diversity when both resolved models are Codex.
        """
        target, confirmation, _tip = self.subsumption_case()
        self.verified_proxy("integrator")
        self.verified_proxy("claude-reviewer")
        before = self.history_len(target["id"])
        with self.proxy_families():
            out, err = landreq.close(
                target["id"], "subsumed", evidence=confirmation["id"][:12],
                trunk=self.main)
        self.assertIsNone(out)
        self.assertIn("both rows resolve to ds4pro", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_genuine_claude_and_codex_resolved_models_pass(self):
        target, confirmation, _tip = self.subsumption_case()
        self.verified_proxy("integrator")
        self.verified_runtime("claude-reviewer", "claude")
        with self.proxy_families():
            out, err = landreq.close(
                target["id"], "subsumed", evidence=confirmation["id"][:12],
                trunk=self.main)
        self.assertIsNone(err)
        self.assertEqual(out["close_reason"], "subsumed")
        self.assertEqual(out["original_author_family"], "ds4pro")
        self.assertEqual(out["confirmation_recipient_family"], "claude")
        self.assertEqual(out["original_family_evidence"]["v"], 3)
        self.assertEqual(out["confirmation_family_evidence"]["v"], 4)

    def test_v4_runtime_snapshot_cannot_claim_a_different_family(self):  # noqa: VACUOUS_ASSERTION — accepted dry-run proves the base event before mutation
        target, confirmation, _tip = self.subsumption_case()
        self.verified_proxy("integrator")
        self.verified_runtime("claude-reviewer", "claude")
        with self.proxy_families():
            dry, err = landreq.close(
                target["id"], "subsumed", evidence=confirmation["id"][:12],
                trunk=self.main, dry_run=True)
        self.assertIsNone(err)
        event = self.event_from_dry(target, confirmation, dry)
        proof = dict(event["confirmation_family_evidence"])
        proof["runtime"] = dict(proof["runtime"], family="codex")
        event["confirmation_family_evidence"] = proof
        event["confirmation_family_anchor"] = \
            dispatches._subsumed_family_anchor(proof)
        current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        err = dispatches._close_event_error(
            event, current[target["id"]], current=current, verdicts=verdicts)
        self.assertIn("does not prove the recorded native family", err)

    def test_historical_v2_runtime_without_backend_replays_unchanged(self):  # noqa: VACUOUS_ASSERTION — a nonempty valid anchor positively proves the historical object before acceptance
        evidence = {"v": 2, "identity": "claude-reviewer",
                    "roster_identity": "claude-reviewer",
                    "runtime": {"agent_harness": "claude", "family": "claude"},
                    "runtime_verified": True}
        anchor = dispatches._subsumed_family_anchor(evidence)
        self.assertIsNotNone(anchor)
        self.assertIsNone(dispatches._family_evidence_error(
            evidence, "claude-reviewer", "claude", anchor))

    def test_v3_proxy_route_tamper_is_inert_even_with_a_matching_new_anchor(self):  # noqa: VACUOUS_ASSERTION — accepted dry-run proves the untampered route first
        target, confirmation, _tip = self.subsumption_case()
        self.verified_proxy("integrator")
        self.verified_runtime("claude-reviewer", "claude")
        with self.proxy_families():
            dry, err = landreq.close(
                target["id"], "subsumed", evidence=confirmation["id"][:12],
                trunk=self.main, dry_run=True)
        self.assertIsNone(err)
        event = self.event_from_dry(target, confirmation, dry)
        evidence = dict(event["original_family_evidence"])
        proof = dict(evidence["proxy_proof"])
        proof["route"] = dict(proof["route"], provider="costume-provider")
        evidence["proxy_proof"] = proof
        event["original_family_evidence"] = evidence
        event["original_family_anchor"] = \
            dispatches._subsumed_family_anchor(evidence)
        current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        err = dispatches._close_event_error(
            event, current[target["id"]], current=current, verdicts=verdicts)
        self.assertIn("proxy proof is malformed", err)

    def test_unresolvable_runtime_is_unknown_and_refuses(self):  # noqa: VACUOUS_ASSERTION — verified original runtime is the positive control
        target, confirmation, _tip = self.subsumption_case()
        self.verified_proxy("integrator")
        before = self.history_len(target["id"])
        with self.proxy_families():
            out, err = landreq.close(
                target["id"], "subsumed", evidence=confirmation["id"][:12],
                trunk=self.main)
        self.assertIsNone(out)
        self.assertIn("confirmation recipient family is UNKNOWN", err)
        self.assertIn("no unique canonical roster runtime record", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_bool_True_is_not_an_integer_family_proof_version(self):  # noqa: VACUOUS_ASSERTION — v1 positive control proves only bool is rejected
        self.assertIsNotNone(dispatches._subsumed_family_anchor(
            {"v": 1, "identity": "integrator"}))
        self.assertIsNone(dispatches._subsumed_family_anchor(
            {"v": True, "identity": "integrator"}))

    def test_historical_v1_replay_is_not_reinterpreted_by_runtime_resolution(self):
        target, confirmation, _tip = self.subsumption_case()
        out, err = self.close_case(target, confirmation)
        self.assertIsNone(err)
        self.assertEqual(out["close_reason"], "subsumed")
        self.assertEqual(out["original_family_evidence"]["v"], 1)
        self.assertEqual(out["confirmation_family_evidence"]["v"], 1)
        # The new resolver cannot prove either actor in this hermetic roster.
        families, why = dispatches._approval_identity_families("integrator")
        self.assertIsNone(families)
        self.assertIn("no unique canonical roster runtime record", why)
        # Replay still consumes the immutable v1 proof exactly as landed; neither
        # a later policy nor the forward-only resolver revises history.
        with mock.patch.object(
                dispatches, "approval_tier",
                return_value=("outside", "policy changed after close")):
            replayed = dispatches.snapshot()[0][target["id"]]
        self.assertEqual(replayed["close_reason"], "subsumed")

    def test_gate_capable_bound_confirmation_survives_locked_write_and_replay(self):
        target, confirmation, _tip = self.subsumption_case(gate_capable=True)
        current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        index = dispatches.verdict_index(verdicts, confirmation["id"])
        self.assertEqual(landreq.gate_requirement(
            current[confirmation["id"]], index=index,
            epoch=dispatches.gate_epoch(current, verdicts)), "required")
        out, err = self.close_case(target, confirmation)
        self.assertIsNone(err)
        self.assertEqual(out["close_reason"], "subsumed")
        self.assertEqual(out["confirmation_gate_requirement"], "required")
        self.assertEqual(out["confirmation_gate"], "a" * 16)
        self.assertRegex(out["confirmation_approval_anchor"], r"^[0-9a-f]{32}$")
        self.assertEqual(dispatches.snapshot()[0][target["id"]]["close_reason"],
                         "subsumed")

    def test_gate_prefixed_live_confirmation_evidence_is_accepted(self):  # noqa: VACUOUS_ASSERTION — confirmation id and parsed live verdict prove the helper returned a real close
        target, confirmation, _tip = self.subsumption_case(
            gate_capable=True, gate_prefixed=True)
        out, err = self.close_case(target, confirmation)
        self.assertIsNone(err)
        self.assertEqual(out["confirmation_id"], confirmation["id"])
        verdict = dispatches.snapshot()[0][confirmation["id"]]
        self.assertTrue(dispatches._subsumption_ref(verdict["verdict_ref"]))

    def test_subsumption_marker_must_begin_its_sentence(self):
        marker = "Subsumption verified founder semantics on trunk now"
        self.assertFalse(dispatches._subsumption_ref("NOT " + marker))
        self.assertFalse(dispatches._subsumption_ref(
            "gate:%s NOT %s" % ("a" * 16, marker)))
        self.assertTrue(dispatches._subsumption_ref(
            "gate:%s 42 OK/0 at deadbeef. %s" % ("a" * 16, marker)))

    def test_selector_must_resolve_one_later_verdict(self):  # noqa: VACUOUS_ASSERTION — passing dry-run precedes selector-only mutations
        target, confirmation, _tip = self.subsumption_case()
        self.assert_subsumption_ready(target, confirmation)
        before = self.history_len(target["id"])
        with self.family_evidence():
            out, err = landreq.close(
                target["id"], "subsumed", evidence="no-such-row",
                trunk=self.main)
        self.assertIsNone(out)
        self.assertIn("no such confirmation dispatch", err)
        self.assertEqual(self.history_len(target["id"]), before)
        with self.family_evidence():
            out, err = landreq.close(
                target["id"], "subsumed", evidence=target["id"],
                trunk=self.main)
        self.assertIsNone(out)
        self.assertIn("later durable verdict", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_fix_debt_closes_when_confirmation_answers_its_findings(self):
        target, confirmation, _tip = self.subsumption_case(
            original_polarity="fix",
            confirmation_statement=(
                "Subsumption verified FIX findings were answered on trunk: "
                "the rebuilt writer rechecks every mutable input"))
        out, err = self.close_case(target, confirmation)
        self.assertIsNone(err)
        self.assertEqual(out["polarity"], "fix")
        self.assertEqual(out["close_reason"], "subsumed")
        replayed = dispatches.snapshot()[0][target["id"]]
        self.assertEqual(replayed["polarity"], "fix")
        self.assertEqual(replayed["close_reason"], "subsumed")
        shown = landreq._render_show(out)
        self.assertIn("CHANGES_REQUESTED — CLOSED (SUBSUMED)", shown)
        self.assertIn("FIX findings were answered", shown)

    def test_fix_debt_refuses_generic_subsumption_wording(self):  # noqa: VACUOUS_ASSERTION — passing FIX control differs only by confirmation wording
        control, control_confirmation, _tip = self.subsumption_case(
            original_polarity="fix",
            confirmation_statement=(
                "Subsumption verified FIX findings were answered on trunk: "
                "the rebuilt writer rechecks every mutable input"))
        self.assert_subsumption_ready(control, control_confirmation)
        target, confirmation, _tip = self.subsumption_case(
            original_polarity="fix")
        before = self.history_len(target["id"])
        out, err = self.close_case(target, confirmation)
        self.assertIsNone(out)
        self.assertIn("FIX findings", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_original_supersede_remains_outside_subsumption(self):  # noqa: VACUOUS_ASSERTION — passing FIX control differs only by original polarity
        control, control_confirmation, _tip = self.subsumption_case(
            original_polarity="fix",
            confirmation_statement=(
                "Subsumption verified FIX findings were answered on trunk: "
                "the rebuilt writer rechecks every mutable input"))
        self.assert_subsumption_ready(control, control_confirmation)
        target, confirmation, _tip = self.subsumption_case(
            original_polarity="supersede",
            confirmation_statement=(
                "Subsumption verified FIX findings were answered on trunk: "
                "the rebuilt writer rechecks every mutable input"))
        before = self.history_len(target["id"])
        out, err = self.close_case(target, confirmation)
        self.assertIsNone(out)
        self.assertIn("APPROVE/FIX", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_confirmation_fix_still_refuses(self):  # noqa: VACUOUS_ASSERTION — passing control differs only by confirmation polarity
        control, control_confirmation, _tip = self.subsumption_case()
        self.assert_subsumption_ready(control, control_confirmation)
        target, confirmation, _tip = self.subsumption_case(
            confirmation_polarity="fix")
        before = self.history_len(target["id"])
        out, err = self.close_case(target, confirmation)
        self.assertIsNone(out)
        self.assertIn("confirmation verdict polarity", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_fix_marker_must_begin_its_sentence_and_name_a_resolution(self):
        marker = ("Subsumption verified FIX findings were answered on trunk: "
                  "the lock rechecks policy")
        self.assertTrue(dispatches._subsumption_ref(marker, "fix"))
        self.assertTrue(dispatches._subsumption_ref(
            "gate:%s 42 OK/0. %s" % ("a" * 16, marker), "fix"))
        self.assertFalse(dispatches._subsumption_ref("NOT " + marker, "fix"))
        self.assertFalse(dispatches._subsumption_ref(
            "Subsumption verified FIX findings were answered on trunk: ", "fix"))

    def test_fix_and_approve_closes_keep_the_same_event_schema(self):  # noqa: VACUOUS_ASSERTION — both dry-runs positively bind before their exact event key sets are compared
        approved, approved_confirmation, _tip = self.subsumption_case()
        approved_dry = self.assert_subsumption_ready(
            approved, approved_confirmation)
        fixed, fixed_confirmation, _tip = self.subsumption_case(
            original_polarity="fix",
            confirmation_statement=(
                "Subsumption verified FIX findings were answered on trunk: "
                "the lock rechecks policy"))
        fixed_dry = self.assert_subsumption_ready(fixed, fixed_confirmation)
        self.assertEqual(
            set(self.event_from_dry(approved, approved_confirmation, approved_dry)),
            set(self.event_from_dry(fixed, fixed_confirmation, fixed_dry)))

    def test_locked_writer_rechecks_fix_specific_wording(self):  # noqa: VACUOUS_ASSERTION — passing FIX dry-run precedes one confirmation-ref mutation
        target, confirmation, _tip = self.subsumption_case(
            original_polarity="fix",
            confirmation_statement=(
                "Subsumption verified FIX findings were answered on trunk: "
                "the lock rechecks policy"))
        dry = self.assert_subsumption_ready(target, confirmation)
        proof = self.proof_args(dry)
        proof["confirmation_ref"] = \
            "Subsumption verified implementation exists on trunk now"
        before = self.history_len(target["id"])
        with self.family_evidence():
            out, err = dispatches._record_close_proven(
                target["id"], "subsumed", self.side,
                evidence=confirmation["id"][:12], **proof)
        self.assertIsNone(out)
        self.assertIn("FIX findings-answered", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_fix_subsumption_replay_survives_pruning_the_original(self):  # noqa: VACUOUS_ASSERTION — successful close and prune helper's real cat-file rc=1 control precede replay
        target, confirmation, _tip = self.subsumption_case(
            original_polarity="fix",
            confirmation_statement=(
                "Subsumption verified FIX findings were answered on trunk: "
                "the lock rechecks policy"))
        out, err = self.close_case(target, confirmation)
        self.assertIsNone(err)
        self.prune(self.side, "refs/heads/side")
        replayed = dispatches.snapshot()[0][target["id"]]
        self.assertEqual(replayed["polarity"], "fix")
        self.assertEqual(replayed["close_reason"], "subsumed")

    def test_original_and_confirmation_must_both_be_review_rows(self):  # noqa: VACUOUS_ASSERTION — passing controls differ only by one dispatch kind
        cases = (({"original_kind": "build"}, "not a review dispatch"),
                 ({"confirmation_kind": "build"},
                  "confirmation verdict is not a review dispatch"))
        self.assertEqual(len(cases), 2, "both review-kind clauses need fixtures")
        for kwargs, expected in cases:
            with self.subTest(expected=expected):
                control, control_confirmation, _tip = self.subsumption_case()
                self.assert_subsumption_ready(control, control_confirmation)
                target, confirmation, _tip = self.subsumption_case(**kwargs)
                before = self.history_len(target["id"])
                out, err = self.close_case(target, confirmation)
                self.assertIsNone(out)
                self.assertIn(expected, err)
                self.assertEqual(self.history_len(target["id"]), before)

    def test_legacy_chainless_debt_uses_confirmation_parent_as_chain_root(self):
        target, confirmation, _tip = self.subsumption_case(original_legacy=True)
        current = dispatches.snapshot()[0]
        self.assertIsNone(current[target["id"]]["chain_root"])
        self.assertEqual(current[confirmation["id"]]["supersedes"], target["id"])
        self.assertEqual(current[confirmation["id"]]["chain_root"], target["id"])
        out, err = self.close_case(target, confirmation)
        self.assertIsNone(err)
        self.assertEqual(out["close_reason"], "subsumed")
        replayed = dispatches.snapshot()[0][target["id"]]
        self.assertEqual(replayed["close_reason"], "subsumed")
        self.assertEqual(replayed["confirmation_id"], confirmation["id"])

    def test_legacy_chainless_unlinked_confirmation_refuses(self):  # noqa: VACUOUS_ASSERTION — linked legacy control differs only by confirmation ancestry
        control, control_confirmation, _tip = self.subsumption_case(
            original_legacy=True)
        self.assert_subsumption_ready(control, control_confirmation)
        target, confirmation, _tip = self.subsumption_case(
            original_legacy=True, same_chain=False)
        before = self.history_len(target["id"])
        out, err = self.close_case(target, confirmation)
        self.assertIsNone(out)
        self.assertIn("not linked to the same chain/repo", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_unrelated_chain_refuses_with_every_other_clause_true(self):  # noqa: VACUOUS_ASSERTION — passing control differs only by chain identity
        control, control_confirmation, _tip = self.subsumption_case()
        self.assert_subsumption_ready(control, control_confirmation)
        target, confirmation, _tip = self.subsumption_case(same_chain=False)
        before = self.history_len(target["id"])
        out, err = self.close_case(target, confirmation)
        self.assertIsNone(out)
        self.assertIn("same chain/repo", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_repository_mismatch_refuses_independently_of_chain(self):  # noqa: VACUOUS_ASSERTION — passing dry-run precedes one repo-id mutation
        target, confirmation, _tip = self.subsumption_case()
        self.assert_subsumption_ready(target, confirmation)
        events = eventledger.events(dispatches.ledger_path())
        with open(dispatches.ledger_path(), "w", encoding="utf-8") as f:
            for event in events:
                if event.get("id") == confirmation["id"] \
                        and event.get("event") == "dispatch":
                    event["repo_id"] = os.path.join(self.tmp, "other.git")
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
        before = self.history_len(target["id"])
        out, err = self.close_case(target, confirmation)
        self.assertIsNone(out)
        self.assertIn("same chain/repo", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_approval_refusal_is_reused(self):  # noqa: VACUOUS_ASSERTION — passing dry-run precedes one approval refusal
        target, confirmation, _tip = self.subsumption_case()
        self.assert_subsumption_ready(target, confirmation)
        before = self.history_len(target["id"])
        with self.family_evidence(), mock.patch.object(
                landreq, "_approval_refusal",
                return_value=("no minted gate receipt", "ok")):
            out, err = landreq.close(
                target["id"], "subsumed", evidence=confirmation["id"],
                trunk=self.main)
        self.assertIsNone(out)
        self.assertIn("no minted gate receipt", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_approval_tier_resolver_uses_explicit_repository_scope(self):
        with mock.patch(
                "helm.inject._ledger.project_for_cwd",
                return_value="target-project") as scope, \
                mock.patch("helm.store.load_certain_policy",
                           return_value=(None, "no policy")), \
                mock.patch("helm.store.policy_declared", return_value=False):
            state, _why = dispatches.approval_tier(
                "claude-reviewer", repo=self.gitdir())
        self.assertEqual(state, "none")
        scope.assert_called_once_with(self.gitdir())

    def test_recorded_approval_tier_is_bound_to_the_row_repository(self):
        from helm.store import policy_history
        target, confirmation, _tip = self.subsumption_case()
        with mock.patch.object(
                dispatches, "_approval_tier_uncached",
                side_effect=AssertionError("close borrowed current policy")), \
                mock.patch.object(policy_history, "resolve",
                                  wraps=policy_history.resolve) as resolve:
            out, err = self.close_case(target, confirmation)
        self.assertIsNone(err, err)
        self.assertEqual(out["close_reason"], "subsumed")
        self.assertGreater(resolve.call_count, 0, "retained policy was never read")
        contexts = [call.args[1] for call in resolve.call_args_list]
        self.assertEqual({context["repo_id"] for context in contexts}, {self.gitdir()})
        self.assertIn(confirmation["id"], {context["id"] for context in contexts})

    def test_confirmation_evidence_needs_marker_and_concrete_trunk_statement(self):  # noqa: VACUOUS_ASSERTION — passing control differs only by evidence grammar
        control, control_confirmation, _tip = self.subsumption_case()
        self.assert_subsumption_ready(control, control_confirmation)
        target, confirmation, _tip = self.subsumption_case(marked=False)
        before = self.history_len(target["id"])
        out, err = self.close_case(target, confirmation)
        self.assertIsNone(out)
        self.assertIn("Subsumption verified", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_confirmation_must_be_on_current_pinned_trunk(self):  # noqa: VACUOUS_ASSERTION — passing control differs only by confirmation land state
        control, control_confirmation, _tip = self.subsumption_case()
        self.assert_subsumption_ready(control, control_confirmation)
        target, confirmation, _tip = self.subsumption_case(
            confirmation_landed=False)
        before = self.history_len(target["id"])
        out, err = self.close_case(target, confirmation)
        self.assertIsNone(out)
        self.assertIn("confirmation reviewed tip is absent", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_original_on_trunk_refuses_with_every_other_clause_true(self):  # noqa: VACUOUS_ASSERTION — passing control differs only by original land state
        control, control_confirmation, _tip = self.subsumption_case()
        self.assert_subsumption_ready(control, control_confirmation)
        target, confirmation, _tip = self.subsumption_case(original_landed=True)
        before = self.history_len(target["id"])
        out, err = self.close_case(target, confirmation)
        self.assertIsNone(out)
        self.assertIn("original reviewed change is on trunk", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_unknown_original_proof_refuses_with_every_other_clause_true(self):  # noqa: VACUOUS_ASSERTION — passing dry-run precedes one UNKNOWN proof mutation
        target, confirmation, _tip = self.subsumption_case()
        self.assert_subsumption_ready(target, confirmation)
        before = self.history_len(target["id"])
        real = landreq._landing_proof

        def unknown_original(gitdir, tip, ref):
            return "unknown" if tip == self.side else real(gitdir, tip, ref)
        with self.family_evidence(), mock.patch.object(
                landreq, "_landing_proof", side_effect=unknown_original):
            out, err = landreq.close(
                target["id"], "subsumed", evidence=confirmation["id"],
                trunk=self.main)
        self.assertIsNone(out)
        self.assertIn("could not prove the original", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_each_identity_must_be_exactly_one_different_family(self):  # noqa: VACUOUS_ASSERTION — passing dry-run precedes one family derivation mutation
        target, confirmation, _tip = self.subsumption_case()
        self.assert_subsumption_ready(target, confirmation)
        before = self.history_len(target["id"])
        cases = ((True, None, "both rows resolve"),
                 (False, "integrator", "exactly one identity family"),
                 (False, "claude-reviewer", "exactly one identity family"))
        self.assertEqual(len(cases), 3, "same and both UNKNOWN clauses need fixtures")
        for same, unknown, expected in cases:
            with self.subTest(same=same, unknown=unknown):
                with self.family_evidence(same=same, unknown=unknown):
                    out, err = landreq.close(
                        target["id"], "subsumed",
                        evidence=confirmation["id"], trunk=self.main)
                self.assertIsNone(out)
                self.assertIn(expected, err)
                self.assertEqual(self.history_len(target["id"]), before)

    def test_prefix_and_full_selector_retries_are_idempotent(self):  # noqa: VACUOUS_ASSERTION — successful close proves the fixture before intentional no-append retries
        target, confirmation, _tip = self.subsumption_case()
        self.assert_subsumption_ready(target, confirmation)
        first, err = self.close_case(target, confirmation)
        self.assertIsNone(err)
        before = self.history_len(target["id"])
        again, err = self.close_case(target, confirmation)
        self.assertIsNone(err)
        self.assertEqual(again, first)
        self.assertEqual(self.history_len(target["id"]), before)
        with self.family_evidence():
            same, err = landreq.close(
                target["id"], "subsumed", evidence=confirmation["id"],
                trunk=self.main)
        self.assertIsNone(err)
        self.assertEqual(same, first)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_racing_identical_close_returns_fresh_terminal_projection(self):  # noqa: VACUOUS_ASSERTION — stale pre-close control is followed by a real close and a terminal retry result
        target, confirmation, _tip = self.subsumption_case()
        stale, err = landreq.get(target["id"])
        self.assertIsNone(err)
        self.assertIsNone(stale["close_reason"])
        first, err = self.close_case(target, confirmation)
        self.assertIsNone(err)
        self.assertEqual(first["close_reason"], "subsumed")
        again, err = landreq._close_ladder_subsumed(
            stale, confirmation["id"][:12], None, self.main, False)
        self.assertIsNone(err)
        self.assertEqual(again["close_reason"], "subsumed")
        self.assertTrue(again["terminal"])

    def test_retry_refuses_a_different_repository(self):  # noqa: VACUOUS_ASSERTION — successful close precedes one repository mutation
        target, confirmation, _tip = self.subsumption_case()
        self.assert_subsumption_ready(target, confirmation)
        first, err = self.close_case(target, confirmation)
        self.assertIsNone(err)
        before = self.history_len(target["id"])
        with self.family_evidence():
            out, err = landreq.close(
                target["id"], "subsumed", evidence=confirmation["id"][:12],
                repo=os.path.join(self.tmp, "missing-repo"), trunk=self.main)
        self.assertIsNone(out)
        self.assertIn("not a readable Git working tree", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_retry_refuses_a_different_trunk(self):  # noqa: VACUOUS_ASSERTION — successful close precedes one trunk-ref mutation
        target, confirmation, _tip = self.subsumption_case()
        self.assert_subsumption_ready(target, confirmation)
        first, err = self.close_case(target, confirmation)
        self.assertIsNone(err)
        before = self.history_len(target["id"])
        self.git("branch", "other-trunk", self.main)
        with self.family_evidence():
            out, err = landreq.close(
                target["id"], "subsumed", evidence=confirmation["id"][:12],
                trunk="other-trunk")
        self.assertIsNone(out)
        self.assertIn("different closure", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_locked_writer_refuses_one_missing_schema_field(self):  # noqa: VACUOUS_ASSERTION — passing dry-run precedes deletion of one schema field
        target, confirmation, _tip = self.subsumption_case()
        dry = self.assert_subsumption_ready(target, confirmation)
        before = self.history_len(target["id"])
        partial = self.proof_args(dry)
        partial.pop("confirmation_ref")
        with self.family_evidence():
            out, err = dispatches._record_close_proven(
                target["id"], "subsumed", self.side,
                evidence=confirmation["id"][:12], **partial)
        self.assertIsNone(out)
        self.assertIn("missing=confirmation_ref", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_closed_row_refuses_a_different_confirmation(self):  # noqa: VACUOUS_ASSERTION — successful close precedes one confirmation mutation
        target, confirmation, _tip = self.subsumption_case()
        self.assert_subsumption_ready(target, confirmation)
        first, err = self.close_case(target, confirmation)
        self.assertIsNone(err)
        before = self.history_len(target["id"])
        other_tip = self.commit("founder semantics evolved again")
        other = dispatches.add(
            "claude-reviewer", "lane/evolved-r3", ref=other_tip,
            repo=self.repo, kind="review", notify=False,
            supersedes=confirmation["id"])
        _out, why = self.mark_verdict(
            other["id"], other_tip,
            "Subsumption verified founder semantics on trunk again",
            polarity="approve")
        self.assertIsNone(why)
        with self.family_evidence():
            out, err = landreq.close(
                target["id"], "subsumed", evidence=other["id"],
                trunk=self.main)
        self.assertIsNone(out)
        self.assertIn("retired once", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_locked_writer_binds_selector_to_the_confirmation(self):  # noqa: VACUOUS_ASSERTION — passing dry-run precedes one selector mutation
        target, confirmation, _tip = self.subsumption_case()
        dry = self.assert_subsumption_ready(target, confirmation)
        before = self.history_len(target["id"])
        with self.family_evidence():
            out, err = dispatches._record_close_proven(
                target["id"], "subsumed", self.side,
                evidence="deadbeef", **self.proof_args(dry))
        self.assertIsNone(out)
        self.assertIn("no such confirmation dispatch", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_locked_writer_rechecks_selector_uniqueness(self):  # noqa: VACUOUS_ASSERTION — passing dry-run precedes one colliding-id mutation
        target, confirmation, _tip = self.subsumption_case()
        dry = self.assert_subsumption_ready(target, confirmation)
        current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        prefix = confirmation["id"][:8]
        collision = prefix + ("0" * 24)
        if collision == confirmation["id"]:
            collision = prefix + ("f" * 24)
        current[collision] = dict(current[confirmation["id"]], id=collision)
        before = self.history_len(target["id"])
        with mock.patch.object(
                dispatches, "snapshot_with_verdicts",
                return_value=(current, verdicts, None)):
            out, err = dispatches._record_close_proven(
                target["id"], "subsumed", self.side, evidence=prefix,
                **self.proof_args(dry))
        self.assertIsNone(out)
        self.assertIn("ambiguous confirmation dispatch", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_tampered_family_claim_without_matching_digest_is_inert(self):  # noqa: VACUOUS_ASSERTION — passing dry-run precedes one family-claim mutation
        target, confirmation, _tip = self.subsumption_case()
        dry = self.assert_subsumption_ready(target, confirmation)
        event = self.event_from_dry(target, confirmation, dry)
        event["original_author_family"] = "forged-family"
        eventledger.append_unlocked(dispatches.ledger_path(), event)
        self.assertIsNone(
            dispatches.snapshot()[0][target["id"]].get("close_reason"))

    def test_coherent_family_snapshot_is_rechecked_at_the_locked_writer(self):  # noqa: VACUOUS_ASSERTION — replay trusts writer-captured identity while the writer rechecks its live source
        target, confirmation, _tip = self.subsumption_case()
        dry = self.assert_subsumption_ready(target, confirmation)
        event = self.event_from_dry(target, confirmation, dry)
        evidence = dict(event["confirmation_family_evidence"],
                        minted_families=["fabricated"])
        event["confirmation_recipient_family"] = "fabricated"
        event["confirmation_family_evidence"] = evidence
        event["confirmation_family_anchor"] = \
            dispatches._subsumed_family_anchor(evidence)
        current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        self.assertIsNone(dispatches._close_event_error(
            event, current[target["id"]], current=current, verdicts=verdicts))
        err = dispatches._close_event_error(
            event, current[target["id"]], current=current, verdicts=verdicts,
            verify_families=True)
        self.assertIn("family is UNKNOWN or conflicts", err)

    def test_replay_refuses_a_contradictory_pinned_git_proof(self):  # noqa: VACUOUS_ASSERTION — passing dry-run precedes one trunk-sha mutation
        target, confirmation, _tip = self.subsumption_case()
        dry = self.assert_subsumption_ready(target, confirmation)
        event = self.event_from_dry(target, confirmation, dry)
        event["closing_trunk_sha"] = self.side
        current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        err = dispatches._close_event_error(
            event, current[target["id"]], current=current, verdicts=verdicts)
        self.assertIn("Git proof", err)
        eventledger.append_unlocked(dispatches.ledger_path(), event)
        self.assertIsNone(
            dispatches.snapshot()[0][target["id"]].get("close_reason"))

    def test_subsumption_replay_survives_pruning_the_absent_original(self):
        target, confirmation, _tip = self.subsumption_case()
        out, err = self.close_case(target, confirmation)
        self.assertIsNone(err)
        self.assertEqual(out["close_reason"], "subsumed")
        self.prune(self.side, "refs/heads/side")
        replayed = dispatches.snapshot()[0][target["id"]]
        self.assertEqual(replayed["close_reason"], "subsumed")
        self.assertEqual(replayed["confirmation_id"], confirmation["id"])

    def test_each_durable_authorization_anchor_is_load_bearing(self):  # noqa: VACUOUS_ASSERTION — accepted base event precedes one-field mutations
        target, confirmation, _tip = self.subsumption_case(gate_capable=True)
        dry = self.assert_subsumption_ready(target, confirmation)
        current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        base = self.event_from_dry(target, confirmation, dry)
        self.assertIsNone(dispatches._close_event_error(
            base, current[target["id"]], current=current, verdicts=verdicts))
        cases = (("original_verdict_anchor", "f" * 32),
                 ("confirmation_verdict_anchor", "e" * 32),
                 ("original_family_anchor", "d" * 32),
                 ("confirmation_family_anchor", "c" * 32),
                 ("confirmation_approval_anchor", "b" * 32),
                 ("confirmation_tier_state", "outside"),
                 ("confirmation_gate_requirement", "unknown"),
                 ("confirmation_gate", ""))
        errors = [dispatches._close_event_error(
            dict(base, **{field: value}), current[target["id"]],
            current=current, verdicts=verdicts) for field, value in cases]
        self.assertEqual(len(errors), 8, "every captured authorization field mutated")
        self.assertTrue(all(errors), errors)

    def test_whole_object_forgery_is_inert(self):  # noqa: VACUOUS_ASSERTION — passing dry-run precedes one extra-field mutation
        target, confirmation, _tip = self.subsumption_case()
        dry = self.assert_subsumption_ready(target, confirmation)
        event = self.event_from_dry(target, confirmation, dry)
        event["forged"] = True
        eventledger.append_unlocked(dispatches.ledger_path(), event)
        self.assertIsNone(
            dispatches.snapshot()[0][target["id"]].get("close_reason"))

    def test_locked_writer_rechecks_confirmation_evidence(self):  # noqa: VACUOUS_ASSERTION — passing dry-run precedes one evidence mutation
        target, confirmation, _tip = self.subsumption_case()
        dry = self.assert_subsumption_ready(target, confirmation)
        before = self.history_len(target["id"])
        mismatch = self.proof_args(dry)
        mismatch["confirmation_ref"] = \
            "Subsumption verified forged on trunk now"
        with self.family_evidence():
            out, err = dispatches._record_close_proven(
                target["id"], "subsumed", self.side,
                evidence=confirmation["id"][:12], **mismatch)
        self.assertIsNone(out)
        self.assertIn("confirmation fields", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_locked_writer_rechecks_confirmation_family(self):  # noqa: VACUOUS_ASSERTION — passing dry-run precedes one family mutation
        target, confirmation, _tip = self.subsumption_case()
        dry = self.assert_subsumption_ready(target, confirmation)
        before = self.history_len(target["id"])
        with self.family_evidence(same=True):
            out, err = dispatches._record_close_proven(
                target["id"], "subsumed", self.side,
                evidence=confirmation["id"][:12], **self.proof_args(dry))
        self.assertIsNone(out)
        self.assertIn("confirmation recipient family", err)
        self.assertEqual(self.history_len(target["id"]), before)

    def test_cli_dry_run_and_tip_usage(self):  # noqa: VACUOUS_ASSERTION — successful CLI dry-run precedes adding only the incoherent tip flag
        target, confirmation, _tip = self.subsumption_case()
        before = self.history_len(target["id"])
        args = ["close", target["id"][:12], "--reason", "subsumed",
                "--evidence", confirmation["id"][:12], "--repo", self.repo,
                "--trunk", self.main, "--dry-run", "--json"]
        with self.family_evidence():
            rc, out, err = run(args)
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertEqual(data["confirmation_id"], confirmation["id"])
        self.assertEqual(data["original_proof_mode"], "absent")
        self.assertEqual(self.history_len(target["id"]), before)
        rc, _out, err = run(args + ["--tip", self.side])
        self.assertEqual(rc, 2)
        self.assertIn("--tip belongs to --reason superseded", err)


class CloseBoundaryPhysicsTest(CloseBase):
    """D4: every state gate under the write lock AND in the replay arm."""

    def close_landed_direct(self, row, tip=None):
        sha = self.git("rev-parse", self.main)
        return dispatches._record_close_proven(
            row["id"], "landed", tip or self.side,
            closing_repo_id=self.gitdir(),
            closing_trunk_ref="refs/heads/" + self.main,
            closing_trunk_sha=sha, proof_mode="ancestor")

    def test_conflicting_second_close_is_refused_at_the_boundary(self):
        row = self.verdict_row("approve")
        self.git("merge", "--no-edit", "-q", "side")
        out, err = self.close_landed_direct(row)
        self.assertIsNone(err)
        self.assertEqual(out["close_reason"], "landed")
        before = self.history_len(row["id"])
        _out, err = dispatches._record_close_proven(
            row["id"], "stranded", self.side, evidence="a different terminal",
            closing_repo_id=self.gitdir(),
            control_sha=self.git("rev-parse", self.main))
        self.assertIn("retired once", err)
        self.assertIn("close --reason landed", err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_per_reason_idempotent_retry_reconciles_without_appending(self):
        row = self.verdict_row("approve")
        self.git("merge", "--no-edit", "-q", "side")
        one, err = self.close_landed_direct(row)
        self.assertIsNone(err)
        before = self.history_len(row["id"])
        self.commit("trunk moves on")           # D8: the sha is NOT identity
        two, err = self.close_landed_direct(row)
        self.assertIsNone(err)
        self.assertEqual(two["closing_trunk_sha"], one["closing_trunk_sha"])
        self.assertEqual(self.history_len(row["id"]), before)

    def test_close_refuses_open_rows_and_wrong_polarity_at_the_boundary(self):
        open_row = self.dispatch(lane="lane/still-open")
        before = self.history_len(open_row["id"])
        _out, err = self.close_landed_direct(open_row)
        self.assertIn("not a verdict row", err)
        self.assertEqual(self.history_len(open_row["id"]), before)
        fix = self.verdict_row("fix", lane="lane/fix-row")
        before = self.history_len(fix["id"])
        _out, err = self.close_landed_direct(fix)
        self.assertIn("polarity outside", err)
        self.assertEqual(self.history_len(fix["id"]), before)

    def test_the_withdrawn_writer_gate_is_now_TOTAL_over_declared_polarities(self):  # noqa: VACUOUS_ASSERTION — opens with an unconditional positive control on the SAME helper and table (subsumed still excludes concur), proving the refusal string is producible before any absence is read as evidence
        """THE WRITER'S POLARITY GATE FOR `withdrawn` IS TOTAL, AND THAT IS
        WHY THIS ARM IS SHAPED THE WAY IT IS.

        `dispatches.POLARITIES` has exactly four entries and this reason's
        table names all four, so NO declared polarity is outside it and a
        "polarity outside" refusal is UNPRODUCIBLE here. An arm asserting that
        refusal on this reason asserts against a string the writer cannot
        emit — green forever, and about nothing.

        What must hold instead is that the widening is not a BLANKET. So this
        drives the door for every declared polarity, pins that the refusal for
        an UNDECLARED row still exists one door further in (the ladder), and
        carries a scoping control on a reason nobody ruled on.
        """
        self.assertEqual(("approve", "fix", "supersede", "concur"),
                         dispatches.POLARITIES,
                         "the polarity vocabulary changed; this arm's claim "
                         "that the table is TOTAL must be re-derived")
        def write(rid, reason):
            return dispatches._record_close_proven(
                rid, reason, self.side, evidence="attested",
                absence_trunk_ref="refs/heads/" + self.main,
                absence_trunk_sha=self.git("rev-parse", self.main))[1]

        # NO CROSS-REASON CONTROL HERE, and the reason is measured: the
        # obvious one (`subsumed`, whose table still excludes concur) refuses
        # on its exact-set-equality SCHEMA before the polarity gate is
        # reached, so it proves nothing about polarity. The unconditional
        # control this arm does carry is the POLARITIES equality above plus
        # the UNDECLARED ladder refusal below, which is a real refusal string
        # produced on this very reason.

        for polarity in dispatches.POLARITIES:
            row = self.verdict_row(polarity, lane="lane/total-%s" % polarity)
            err = write(row["id"], "withdrawn")
            # NOT assertIsNone: this writer has a dozen later binding checks
            # and an unrelated one refusing here would look like a polarity
            # refusal to a reader. The claim is scoped to the gate that moved.
            self.assertNotIn("polarity outside", err or "",
                             "the writer refused %r on POLARITY" % polarity)
        # THE REFUSAL STILL EXISTS, one door further in: an undeclared row is
        # turned away by the ladder, which is where the boundary now lives.
        undeclared = self.verdict_row(polarity=None, lane="lane/total-none")
        before = self.history_len(undeclared["id"])
        _out, err = landreq.close(undeclared["id"], "withdrawn",
                                  evidence="attested")
        self.assertIn("withdrawable", err or "")
        self.assertEqual(self.history_len(undeclared["id"]), before)
        # AND THE WIDENING WAS SCOPED: a reason nobody ruled on is untouched.
        self.assertNotIn("concur", dispatches._CLOSE_POLARITY["subsumed"])

    def test_reviewed_tip_mismatch_refuses(self):
        row = self.verdict_row("approve")
        before = self.history_len(row["id"])
        _out, err = self.close_landed_direct(row, tip=self.b)
        self.assertIn("reviewed tip does not match", err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_forged_later_close_event_is_inert_in_replay(self):
        """A raw appended `close` the writer would refuse changes NOTHING:
        wrong polarity domain, junk fields, and a second close after a real
        one are all inert on a fresh disk replay."""
        fix = self.verdict_row("fix", lane="lane/forge-target")
        sha = self.git("rev-parse", self.main)
        forgeries = [
            # landed onto a FIX row — polarity outside the domain
            {"v": 3, "event": "close", "seq": 2, "id": fix["id"],
             "ts": dispatches.pk.now_ts(), "close_reason": "landed",
             "reviewed_tip": self.side, "close_proof_version": 1,
             "closing_repo_id": self.gitdir(),
             "closing_trunk_ref": "refs/heads/" + self.main,
             "closing_trunk_sha": sha, "close_proof_mode": "ancestor"},
            # stranded with a junk control sha
            {"v": 3, "event": "close", "seq": 2, "id": fix["id"],
             "ts": dispatches.pk.now_ts(), "close_reason": "stranded",
             "reviewed_tip": self.side, "close_proof_version": 1,
             "close_evidence": "forged", "closing_repo_id": self.gitdir(),
             "control_sha": "not-a-sha", "close_proof_mode": "object-pruned"},
            # withdrawn with no evidence at all
            {"v": 3, "event": "close", "seq": 2, "id": fix["id"],
             "ts": dispatches.pk.now_ts(), "close_reason": "withdrawn",
             "reviewed_tip": self.side, "close_proof_version": 1,
             "absence_trunk_ref": "refs/heads/" + self.main,
             "absence_trunk_sha": sha},
        ]
        for forged in forgeries:
            with open(dispatches.ledger_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(forged) + "\n")
            replayed = dispatches.snapshot()[0][fix["id"]]
            self.assertIsNone(replayed.get("close_reason"),
                              "forged %r applied" % forged["close_reason"])
            self.assertEqual(replayed["status"], "verdict")

    def test_a_valid_close_event_survives_a_fresh_disk_replay(self):
        row = self.verdict_row("approve")
        self.git("merge", "--no-edit", "-q", "side")
        out, err = self.close_landed_direct(row)
        self.assertIsNone(err)
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(replayed["close_reason"], "landed")
        self.assertEqual(replayed["close_proof_mode"], "ancestor")
        self.assertEqual(replayed["closing_trunk_sha"],
                         out["closing_trunk_sha"])
        self.assertEqual(replayed["close_proof_version"], 1)
        # and a SECOND close event appended after it is inert (monotonic)
        with open(dispatches.ledger_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "v": 3, "event": "close", "seq": replayed["seq"] + 1,
                "id": row["id"], "ts": dispatches.pk.now_ts(),
                "close_reason": "withdrawn", "reviewed_tip": self.side,
                "close_proof_version": 1, "close_evidence": "flip attempt",
                "absence_trunk_ref": "refs/heads/" + self.main,
                "absence_trunk_sha": out["closing_trunk_sha"]}) + "\n")
        again = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(again["close_reason"], "landed")

    def test_close_composes_with_legacy_retirements_exclusively(self):
        """A discharged/withdrawn/close-landed row refuses every close, and a
        close-closed row refuses the legacy writers — retired once, any
        order."""
        fix = self.verdict_row("fix", lane="lane/legacy-first")
        self.git("merge", "--no-edit", "-q", "side")
        out, err = dispatches._record_withdraw_proven(
            fix["id"], self.side, "kept off trunk")
        self.assertIsNone(err)
        before = self.history_len(fix["id"])
        _out, err = dispatches._record_close_proven(
            fix["id"], "stranded", self.side, evidence="stranded attempt",
            closing_repo_id=self.gitdir(),
            control_sha=self.git("rev-parse", self.main))
        self.assertIn("already retired by withdraw", err)
        self.assertEqual(self.history_len(fix["id"]), before)
        # and the other direction: close first, discharge refused after
        fix2 = self.verdict_row("fix", lane="lane/close-first",
                                ref=self.b)
        sha = self.git("rev-parse", self.main)
        out, err = dispatches._record_close_proven(
            fix2["id"], "withdrawn", self.b, evidence="attested absent",
            absence_trunk_ref="refs/heads/" + self.main,
            absence_trunk_sha=sha)
        # self.b IS on trunk, but the boundary does not read git — landreq
        # owns that proof; here the write is exercised directly.
        self.assertIsNone(err)
        before = self.history_len(fix2["id"])
        _out, err = dispatches._record_discharge_proven(
            fix2["id"], self.b, self.c, fix2["id"], "later round",
            "landed", "local")
        self.assertIsNone(_out)
        self.assertIn("already retired by close --reason withdrawn", err)
        self.assertEqual(self.history_len(fix2["id"]), before)
        _out, err = dispatches._record_withdraw_proven(
            fix2["id"], self.b, "a second withdraw shape")
        self.assertIsNone(_out)
        self.assertIn("already retired by close --reason withdrawn", err)
        self.assertEqual(self.history_len(fix2["id"]), before)
        # and a FORGED legacy discharge event appended after the close is
        # inert on replay — retired-once holds at all three layers
        with open(dispatches.ledger_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "v": 3, "event": "discharge",
                "seq": dispatches.snapshot()[0][fix2["id"]]["seq"] + 1,
                "id": fix2["id"], "ts": dispatches.pk.now_ts(),
                "reviewed_tip": self.b, "superseding_tip": self.c,
                "superseding_id": fix2["id"], "discharge_ref": "forged",
                "contrary_state": "landed",
                "contrary_target": "local"}) + "\n")
        replayed = dispatches.snapshot()[0][fix2["id"]]
        self.assertFalse(replayed.get("discharged"))
        self.assertEqual(replayed["close_reason"], "withdrawn")

    def test_abandon_and_close_are_exclusive_at_writer_and_replay_boundaries(self):
        clean = lambda _repo, _lane: {
            "mention": {"state": "none"}, "branch_state": "none",
            "worktree_state": "none"}
        first = dispatches.add(
            "codex-3", "lane/abandon-first", ref=self.side, repo=self.repo,
            kind="review", notify=False, new_work=True)
        _out, err = self.mark_verdict(
            first["id"], self.side, "findings", polarity="fix")
        self.assertIsNone(err)
        abandoned, err = dispatches._record_abandon_proven(
            first["id"], self.side, "object destroyed",
            lambda _repo, _tip: False, clean)
        self.assertIsNone(err)
        before = self.history_len(first["id"])
        out, err = dispatches._record_close_proven(
            first["id"], "stranded", self.side, evidence="also stranded",
            closing_repo_id=self.gitdir(),
            control_sha=self.git("rev-parse", self.main),
            proof_mode="object-pruned")
        self.assertIsNone(out)
        self.assertIn("already retired by abandon", err)
        self.assertEqual(self.history_len(first["id"]), before)

        second = dispatches.add(
            "codex-3", "lane/close-before-abandon", ref=self.side,
            repo=self.repo, kind="review", notify=False, new_work=True)
        _out, err = self.mark_verdict(
            second["id"], self.side, "findings", polarity="fix")
        self.assertIsNone(err)
        closed, err = dispatches._record_close_proven(
            second["id"], "stranded", self.side, evidence="stranded first",
            closing_repo_id=self.gitdir(),
            control_sha=self.git("rev-parse", self.main),
            proof_mode="object-pruned")
        self.assertIsNone(err)
        before = self.history_len(second["id"])
        out, err = dispatches._record_abandon_proven(
            second["id"], self.side, "late abandon",
            lambda _repo, _tip: False, clean)
        self.assertIsNone(out)
        self.assertIn("already retired by close --reason stranded", err)
        self.assertEqual(self.history_len(second["id"]), before)
        forged = {"v": 3, "event": "abandon", "seq": closed["seq"] + 1,
                  "id": second["id"], "ts": dispatches.pk.now_ts(),
                  "reviewed_tip": self.side, "repo_id": self.gitdir(),
                  "reason": "forged late abandon", "object_state": "missing",
                  "object_proof_mode": "cat-file-batch-check",
                  "object_proof_version": 1, "trunk_mention_state": "none",
                  "trunk_mention_proof_mode": "structured-message-scan",
                  "trunk_mention_proof_version": 1, "branch_state": "none",
                  "branch_proof_mode": "git-ref-and-ancestry",
                  "branch_proof_version": 1, "worktree_state": "none",
                  "worktree_proof_mode": "git-worktree-status",
                  "worktree_proof_version": 1, "land_state": "UNKNOWN"}
        eventledger.append_unlocked(dispatches.ledger_path(), forged)
        replayed = dispatches.snapshot()[0][second["id"]]
        self.assertEqual(replayed["close_reason"], "stranded")
        self.assertFalse(replayed.get("abandoned", False))

    def test_a_racing_identical_close_reconciles_at_the_boundary(self):
        """The winner's event is already on the ledger when the loser's locked
        re-read runs — the loser reconciles as success, ONE event total. (The
        :972-pattern race with the proofs outside the lock is exercised at the
        close() level in CloseSupersededTest.)"""
        row = self.verdict_row("approve")
        self.git("merge", "--no-edit", "-q", "side")
        one, err = self.close_landed_direct(row)
        self.assertIsNone(err)
        before = self.history_len(row["id"])
        two, err = self.close_landed_direct(row)
        self.assertIsNone(err)
        self.assertEqual(two["closing_trunk_sha"], one["closing_trunk_sha"])
        self.assertEqual(self.history_len(row["id"]), before)


class LivenessProbeTest(CloseBase):
    """The four-places-work-lives probes, unit level, tri-state everywhere."""

    def test_object_exists_is_tri_state_on_the_bare_form(self):
        gitdir = self.gitdir()
        # MUST-HIT positive control before any negative is trusted
        self.assertIs(landreq._object_exists(gitdir, self.b), True)
        missing = "1" * 40
        self.assertIs(landreq._object_exists(gitdir, missing), False)
        self.assertIsNone(landreq._object_exists(gitdir, None))
        self.assertIsNone(landreq._object_exists(None, self.b))
        # a repo that cannot be asked is UNKNOWN, never "missing"
        self.assertIsNone(landreq._object_exists(
            os.path.join(self.tmp, "not-a-repo"), self.b))

    def test_a_really_pruned_object_reads_False_not_None(self):
        """The prune recipe (D10b): real reflog expire + gc, positively
        asserted by the fixture itself — then the probe answers False."""
        _row, tip = self.pruned_verdict_row()
        self.assertIs(landreq._object_exists(self.gitdir(), tip), False)

    def test_stems_strip_every_family_costume(self):
        for name, want in (
                ("refs/heads/lane/probe-rescue-work", "probe-rescue-work"),
                ("refs/remotes/origin/lane/probe-rescue-work-r2",
                 "probe-rescue-work"),
                ("rescue/probe-rescue-work", "probe-rescue-work"),
                ("prerebase/probe-rescue-work-r3", "probe-rescue-work"),
                ("wt/probe-rescue-work-deadbeef1", "probe-rescue-work"),
                ("lane/probe-rescue-work-r2-r3", "probe-rescue-work"),
                ("plain-name", "plain-name")):
            self.assertEqual(landreq._stem(name), want, name)

    def test_stem_match_is_generous_above_the_floor_and_floored_below(self):
        self.assertTrue(landreq._stems_match("probe-rescue-work",
                                             "probe-rescue-work-extended"))
        self.assertTrue(landreq._stems_match("probe-rescue-work-extended",
                                             "probe-rescue-work"))
        self.assertTrue(landreq._stems_match("tiny", "tiny"))  # exact: always
        self.assertFalse(landreq._stems_match("a", "lane-a-b"))  # floored
        self.assertFalse(landreq._stems_match("", "anything"))
        self.assertFalse(landreq._stems_match("probe-rescue-work",
                                              "other-lane-name"))

    def test_family_refs_probe_hits_renamed_lanes_and_fails_closed(self):
        gitdir = self.gitdir()
        self.git("branch", "lane/probe-rescue-work-extended", self.b)
        # MUST-HIT: the family probe finds the renamed lane
        matches, err = landreq._lane_family_refs(
            gitdir, "lane/probe-rescue-work", None)
        self.assertIsNone(err)
        self.assertIn("refs/heads/lane/probe-rescue-work-extended",
                      [ref for ref, _obj in matches])
        # a foreign lane misses
        matches, err = landreq._lane_family_refs(
            gitdir, "lane/completely-unrelated", None)
        self.assertIsNone(err)
        self.assertEqual(matches, [])
        # an unreadable repo is UNKNOWN, never an empty family
        matches, err = landreq._lane_family_refs(
            os.path.join(self.tmp, "no-repo"), "lane/probe-rescue-work", None)
        self.assertIsNone(matches)
        self.assertIn("could not enumerate", err)

    def test_family_worktree_probe_matches_branch_and_path_basename(self):
        gitdir = self.gitdir()
        wt = os.path.join(self.tmp, "probe-rescue-work-wt")
        self.git("worktree", "add", "--detach", "-q", wt, self.b)
        matches, err = landreq._lane_family_worktrees(
            gitdir, "lane/probe-rescue-work", None)
        self.assertIsNone(err)
        self.assertIn(os.path.realpath(wt),
                      [os.path.realpath(r["path"]) for r in matches])
        # branch-named worktree matches through the BRANCH field
        wt2 = os.path.join(self.tmp, "unrelated-dirname")
        self.git("worktree", "add", "-q", "-b",
                 "lane/probe-rescue-work-r2", wt2, self.b)
        matches, err = landreq._lane_family_worktrees(
            gitdir, "lane/probe-rescue-work", None)
        self.assertIsNone(err)
        self.assertIn(os.path.realpath(wt2),
                      [os.path.realpath(r["path"]) for r in matches])
        # a foreign family misses both
        matches, err = landreq._lane_family_worktrees(
            gitdir, "lane/completely-unrelated", None)
        self.assertIsNone(err)
        self.assertEqual(matches, [])
        # unreadable → UNKNOWN
        matches, err = landreq._lane_family_worktrees(
            None, "lane/probe-rescue-work", None)
        self.assertIsNone(matches)
        self.assertTrue(err)

    def test_translation_sidecar_unreadable_is_an_error_not_empty(self):
        # ABSENT: empty map, no error
        table, err = landreq._ref_translations_checked()
        self.assertEqual((table, err), ({}, None))
        # PRESENT: readable map
        path = self.sidecar(self.b, self.c)
        table, err = landreq._ref_translations_checked()
        self.assertIsNone(err)
        self.assertEqual(table[self.b], self.c)   # MUST-HIT
        # UNREADABLE: an error, never a silently empty map
        os.chmod(path, 0)
        try:
            table, err = landreq._ref_translations_checked()
            self.assertEqual(table, {})
            self.assertTrue(err, "an unreadable sidecar must carry a reason")
            # and the tolerant wrapper keeps the historical fail-open shape
            self.assertEqual(landreq.ref_translations(), {})
        finally:
            os.chmod(path, 0o600)


class CloseCliTest(CloseBase):
    """T-CLI: every documented flag+valued pair through the REAL parser (the
    regression class of the once-broken lr-land --ack-deletions tuple)."""

    def test_every_documented_flag_drives_the_real_parser(self):
        row = self.verdict_row("approve")
        self.git("merge", "--no-edit", "-q", "side")
        rc, out, err = run(["close", row["id"][:12], "--reason", "landed", "--live",
                            "--evidence", "observed on main by test",
                            "--repo", self.repo, "--trunk", self.main,
                            "--dry-run", "--json"])
        self.assertEqual((rc, err), (0, ""))
        report = json.loads(out)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["proof_mode"], "ancestor")

    def test_reason_misuse_is_usage_tier_with_suggestion(self):
        """BOTH HALVES OF REASON MISUSE ARE USAGE TIER, rc 2 and no append.

        A MISSPELLED reason is refused with the suggestion its spelling is
        nearest to; an ABSENT one is refused because `--reason` is required.
        The two are pinned in one arm because they are one contract — the verb
        does not decide a disposition on the operator's behalf, so there is no
        reason-less path that classifies a row and answers with a terminal.
        """
        row = self.verdict_row("approve")
        rc, _out, err = run(["close", row["id"], "--repo", self.repo,
                             "--trunk", self.main])
        self.assertEqual(rc, 2, err)
        self.assertIn("--reason is required", err)
        rc, _out, err = run(["close", row["id"], "--reason", "landd"])
        self.assertEqual(rc, 2)
        self.assertIn("did you mean 'landed'", err)

    def test_flag_reason_incoherence_is_usage_tier(self):
        row = self.verdict_row("fix", lane="lane/incoherent")
        for args, needle in (
                (["close", row["id"], "--reason", "landed", "--live", "--tip",
                  "a" * 40], "--tip belongs to"),
                (["close", row["id"], "--reason", "superseded"],
                 "requires --tip"),
                (["close", row["id"], "--reason", "withdrawn", "--repo",
                  self.repo], "--repo/--trunk belong to"),
                (["close", row["id"], "--reason", "out-of-scope", "--trunk",
                  "main"], "--repo/--trunk belong to")):
            rc, _out, err = run(args)
            self.assertEqual(rc, 2, args)
            self.assertIn(needle, err)
        rc, _out, err = run(["close", row["id"], "--reason", "withdrawn",
                             "--wat"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown arg", err)


class CloseLandedTest(CloseBase):
    def landed_row(self):
        row = self.verdict_row("approve")
        self.git("merge", "--no-edit", "-q", "side")
        return row

    def test_ancestor_close_records_the_proof_and_preserves_polarity(self):
        row = self.landed_row()
        pinned = self.git("rev-parse", self.main)
        rc, out, err = run(["close", row["id"][:12], "--reason", "landed", "--live"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("CLOSED (LANDED)", out)
        snap = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(snap["close_reason"], "landed")
        self.assertEqual(snap["close_proof_mode"], "ancestor")
        self.assertEqual(snap["closing_trunk_sha"], pinned)
        self.assertEqual(snap["closing_trunk_ref"],
                         "refs/heads/" + self.main)
        self.assertEqual(snap["closing_repo_id"], self.gitdir())
        self.assertEqual(snap["polarity"], "approve")

    def _drift(self, *states):
        return [(st, "pre-commit", "why") for st in states]

    def test_a_STALE_guard_rail_refuses_the_live_declaration(self):
        """BUILT != WIRED. --live says this reached the RUNNING fleet, and a
        generated hook reaches nothing until INSTALLED — landing puts it in
        the generator while git keeps executing the old .git/hooks snapshot.
        Measured: a40f21d3 hardened a '..' traversal escape, landed,
        and was closed COMPLETED as #92 having never been installed. Our
        definition of done does not contain the step that makes a guard real."""
        row = self.landed_row()
        with mock.patch("helm.work._guard.stale_guard_hooks",
                        return_value=self._drift("STALE")):
            rc, _out, err = run(["close", row["id"][:12], "--reason",
                                 "landed", "--live"])
        self.assertEqual(rc, 1)
        self.assertIn("guard rail is not armed", err)
        self.assertIn("install-guard --apply", err,
                      "a refusal must name the command that clears it")
        self.assertIsNone(
            dispatches.snapshot()[0][row["id"]].get("close_reason"),
            "nothing may be written")
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE, and the
        # strongest available one: close the SAME row through the escape and
        # watch close_reason actually fill in. That an id exists proves little;
        # this proves the field was writable the whole time and the refusal is
        # what kept it empty.
        rc2, _o2, err2 = run(["close", row["id"][:12], "--reason", "landed",
                              "--needs-restart", "the guard rail"])
        self.assertEqual((rc2, err2), (0, ""))
        self.assertEqual(
            dispatches.snapshot()[0][row["id"]]["close_reason"], "landed")

    def test_a_MISSING_rail_does_NOT_refuse(self):
        """THE CONTROL, and it caught a real defect rather than decorating one.
        Collapsing the detector's tri-state broke 5 existing closes: a repo
        with no hooks at all reports MISSING for every one. MISSING is not
        drift — that repo never opted into helm's rail and is running no old
        rules. Refusing its lands would be a stranger's opinion about someone
        else's hooks. Only installed-and-DIFFERENT is this rung's business."""
        row = self.landed_row()
        with mock.patch("helm.work._guard.stale_guard_hooks",
                        return_value=self._drift("MISSING", "UNKNOWN")):
            rc, _out, err = run(["close", row["id"][:12], "--reason",
                                 "landed", "--live"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["close_reason"],
                         "landed")

    def test_needs_restart_is_the_escape_while_the_rail_is_stale(self):
        """The rung refuses a DECLARATION, never the close. Declaring the
        obligation is the honest path and stays open, or a stale rail would
        strand every land behind someone else's drift."""
        row = self.landed_row()
        with mock.patch("helm.work._guard.stale_guard_hooks",
                        return_value=self._drift("STALE")):
            rc, _out, err = run(["close", row["id"][:12], "--reason", "landed",
                                 "--needs-restart", "the guard rail"])
        self.assertEqual((rc, err), (0, ""))
        snap = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(snap["close_delivery_restart"], "the guard rail")

    def test_an_unreadable_rail_FAILS_OPEN(self):
        """Opposite posture to the shipped-check in out-of-scope, deliberately.
        That one fails CLOSED because a wrong close records a falsehood; this
        one fails OPEN because a read hiccup must never cost a land that Git
        has already proven. The asymmetry is the point: refuse where being
        wrong writes, admit where being wrong only delays."""
        row = self.landed_row()
        with mock.patch("helm.work._guard.stale_guard_hooks",
                        side_effect=OSError("hook dir vanished")):
            rc, _out, err = run(["close", row["id"][:12], "--reason",
                                 "landed", "--live"])
        self.assertEqual((rc, err), (0, ""))
        # STRUCTURAL: the close actually happened. rc 0 alone would also be
        # true of a verb that returned early and wrote nothing.
        snap = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(snap["close_reason"], "landed")
        self.assertEqual(snap["close_delivery_class"], "cli")

    def test_patch_equivalent_close_records_the_mode(self):
        row = self.verdict_row("approve")
        self.git("cherry-pick", self.side)
        out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(err)
        snap = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(snap["close_proof_mode"], "patch-equivalent")

    def test_absent_and_unknown_refuse_without_appending(self):
        row = self.verdict_row("approve")     # side NOT merged
        before = self.history_len(row["id"])
        _out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIn("neither an ancestor", err)
        self.assertEqual(self.history_len(row["id"]), before)
        with mock.patch.object(landreq, "_landing_proof",
                               return_value="unknown"):
            _out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIn("could not prove whether", err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_fix_and_supersede_rows_refuse_landed(self):
        for polarity in ("fix", "supersede"):
            row = self.verdict_row(polarity, lane="lane/landed-" + polarity)
            self.git("merge", "--no-edit", "-q", "side") \
                if polarity == "fix" else None
            before = self.history_len(row["id"])
            _out, err = landreq.close(row["id"], "landed", live=True)
            self.assertIn("CONTRARY, not a resolution", err)
            # the doors it offers, and whether each opens, are measured in
            # RefusalsNameMeasuredDoorsTest; here only that it offers them
            self.assertIn("Doors that take contrary debt", err)
            self.assertEqual(self.history_len(row["id"]), before)

    def test_open_rows_refuse_landed_with_the_redirect(self):
        row = self.dispatch(lane="lane/open-landed")
        before = self.history_len(row["id"])
        _out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIn("has no verdict", err)
        self.assertIn("out-of-scope", err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_trunk_is_pinned_at_ladder_entry(self):  # noqa: VACUOUS_ASSERTION — every comparison here carries an unconditional positive control on the SAME observable: assertTrue(pinned_taken) proves the ladder actually pinned, assertTrue(moved) proves trunk actually moved mid-ladder, and assertTrue(operands) proves a landing proof actually ran after the pin, so no assertNotIn below can pass by emptiness
        """D7: the whole ladder runs against ONE sha resolved at entry — a
        trunk that moves mid-ladder cannot change what the ladder PROVES
        AGAINST, and not merely what it writes down."""
        row = self.landed_row()
        pinned_before = self.git("rev-parse", self.main)
        real = landreq._landing_proof
        real_pin = landreq._close_trunk
        pinned_taken, moved, operands = [], [], []

        def pinning(*a, **k):
            out = real_pin(*a, **k)
            pinned_taken.append(True)      # the ladder has pinned; moves count
            return out

        def moving(gitdir, tip, ref):
            # DISCRIMINATE ON LADDER ENTRY, NEVER ON THE SHAPE OF `ref`. This
            # hook used to fire on the first call whose ref looked like a raw
            # sha, on the premise that only the ladder passes one. That premise
            # died: the projection now resolves each trunk ref to its immutable
            # sha BEFORE asking, deliberately, because asking about a moving
            # ref name let a local-only land be published as upstream. So "ref
            # is a sha" separates nothing, and this hook fired during the
            # PROJECTION — moving trunk before the pin and making the pin look
            # broken when it was not. Keyed on the pin itself, the test asks
            # its real question again.
            if pinned_taken and not moved:
                moved.append(self.commit("trunk moves mid-ladder"))
            if pinned_taken:
                # THE PROOF'S ACTUAL OPERAND, which is the thing this test is
                # about. See the assertion below.
                operands.append(ref)
            return real(gitdir, tip, ref)

        with mock.patch.object(landreq, "_close_trunk", side_effect=pinning), \
             mock.patch.object(landreq, "_landing_proof", side_effect=moving):
            _out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(err)
        self.assertTrue(pinned_taken, "the ladder never pinned trunk at all")
        self.assertTrue(moved, "the mid-ladder move must actually run")
        # THE RECORDED PIN IS NOT THE PROOF. `closing_trunk_sha` is copied from
        # `_close_trunk`'s return, so it reads correctly no matter WHAT the
        # ladder then asked Git about — and the mutation that matters
        # (`_close_landed_proof(gitdir, reviewed, trunk_ref, pinned)` proving
        # against `trunk_ref`, the MOVING NAME, instead of `pinned`) left every
        # assertion below the line untouched and SURVIVED. So the operand each
        # proof consumed is asserted directly: every post-pin call must name
        # the pinned commit, and none may name the ref.
        self.assertTrue(operands, "the ladder proved nothing after pinning")
        self.assertEqual([pinned_before], sorted(set(operands)),
                         "a landing proof ran against something other than "
                         "the entry pin: %r" % (operands,))
        self.assertNotIn(self.main, operands,
                         "the ladder proved against the moving trunk REF")
        self.assertNotIn(moved[0], operands,
                         "the ladder proved against the trunk it moved to")
        snap = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(snap["closing_trunk_sha"], pinned_before)
        self.assertNotEqual(snap["closing_trunk_sha"], moved[0])

    def test_retry_after_trunk_movement_returns_the_row_exit_0(self):
        """The :2258 port for close --reason landed (D8): identity is repo +
        trunk REF alias, the sha deliberately excluded."""
        row = self.landed_row()
        rc, _out, err = run(["close", row["id"], "--reason", "landed", "--live",
                             "--trunk", self.main])
        self.assertEqual((rc, err), (0, ""))
        anchor = dispatches.snapshot()[0][row["id"]]["closing_trunk_sha"]
        self.commit("trunk moved after the close")
        before = self.history_len(row["id"])
        rc, _out, err = run(["close", row["id"], "--reason", "landed", "--live",
                             "--trunk", self.main])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.history_len(row["id"]), before)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]
                         ["closing_trunk_sha"], anchor)

    def test_a_different_trunk_on_a_closed_row_refuses(self):
        row = self.landed_row()
        _out, err = landreq.close(row["id"], "landed", trunk=self.main, live=True)
        self.assertIsNone(err)
        self.git("branch", "other-trunk", self.main)
        _out, err = landreq.close(row["id"], "landed", trunk="other-trunk", live=True)
        self.assertIn("refusing a different closure", err)


class CloseBuildLandedBase(CloseBase):
    """The BUILD fixtures: `build_parent` opens a build obligation, and
    `approved_child` adds a review row under it with an approving verdict.

    THIS CLASS HOLDS NO `test_*` METHOD, and that is its contract. unittest
    collects every inherited `test_*` again under each subclass's own id, so
    an arm written here runs once per subclass. A class that needs this
    fixture subclasses THIS class; its arms go in the subclass."""

    def build_parent(self, supersedes=None, lane="lane/build-parent"):
        kw = {"supersedes": supersedes} if supersedes else {"new_work": True}
        return dispatches.add(
            "builder", lane, ref=self.b, repo=self.repo, kind="build",
            notify=False, **kw)

    def approved_child(self, parent, lane="lane/build-parent-review",
                       force=False, ref=None):
        # `ref` is ADDITIVE and defaults to the historical self.side, so no
        # existing caller changes behaviour; the content arms need a reviewed
        # tip whose carrier actually lives on trunk.
        ref = ref or self.side
        row = dispatches.add(
            "reviewer", lane, ref=ref, repo=self.repo, kind="review",
            notify=False, supersedes=parent["id"], force=force)
        out, err = self.mark_verdict(
            row["id"], ref, "reviewed build output", polarity="approve")
        self.assertIsNone(err)
        return row


class CloseBuildLandedTest(CloseBuildLandedBase):
    """An OPEN BUILD obligation closes through its landed review descendant.

    A subclass of THIS class runs every arm below again under its own id,
    which is right only for one that changes the fixture those arms read. A
    class that only needs the fixture subclasses CloseBuildLandedBase."""

    def test_build_parent_closes_through_its_landed_review_child(self):
        parent = self.build_parent()
        child = self.approved_child(parent)
        self.git("merge", "--no-edit", "-q", "side")
        out, err = landreq.close(parent["id"], "landed", live=True)
        self.assertIsNone(err)
        self.assertEqual(out["state"], "LANDED")
        self.assertEqual(out["kind"], "build")
        self.assertEqual(out["close_reason"], "landed")
        self.assertEqual(out["landing_review_id"], child["id"])
        self.assertEqual(out["landing_review_tip"], self.side)
        replayed = dispatches.snapshot()[0][parent["id"]]
        self.assertEqual(replayed["status"], "closed")
        self.assertEqual(replayed["landing_review_id"], child["id"])
        event = dispatches.history(parent["id"])[-1]
        self.assertEqual(event["close_proof_version"], 2)
        # THE WHOLE-OBJECT RULE, MINUS THE ONE ORTHOGONAL FIELD. `close_actor`
        # is stamped by the writer from its own declared identity and is
        # ABSENT when a process declares none, so it is present or absent by
        # ENVIRONMENT — production subtracts it from every exact-set check for
        # that reason, and this arm reads the schema the same way rather than
        # inventing a second spelling of the rule.
        self.assertEqual(set(event) - {dispatches.CLOSE_ACTOR_FIELD},
                         dispatches._BUILD_LANDED_EVENT_FIELDS)
        # AND IT IS THE FIELD WE THINK IT IS: a seated writer stamped it, so
        # the subtraction above is removing provenance and not hiding drift.
        self.assertTrue(event.get(dispatches.CLOSE_ACTOR_FIELD)
                        or landreq._acting_seat() is None,
                        "a seated writer recorded no actor: %s" % event)
        self.assertNotIn("reviewed_tip", event)
        self.assertNotIn("close_evidence", event)
        self.assertNotIn(parent["id"],
                         {row["id"] for row in dispatches.open_rows()})
        shown = landreq._render_show(out)
        self.assertIn("BUILD — CLOSED (LANDED via APPROVED REVIEW)", shown)
        self.assertIn(child["id"], shown)

    def test_locked_writer_refuses_incomplete_build_landed_proof(self):  # noqa: VACUOUS_ASSERTION — successful dry-run precedes removal of one exact schema field
        parent = self.build_parent(lane="lane/build-schema")
        self.approved_child(parent, lane="lane/build-schema-review")
        self.git("merge", "--no-edit", "-q", "side")
        dry, err = landreq.close(parent["id"], "landed", dry_run=True, live=True)
        self.assertIsNone(err)
        before = self.history_len(parent["id"])
        out, err = dispatches._record_close_proven(
            parent["id"], "landed", None, close_proof_version=2,
            landing_review_id=dry["landing_review_id"],
            landing_review_tip=dry["landing_review_tip"],
            landing_review_verdict_anchor=dry["landing_review_verdict_anchor"],
            landing_review_tier_state=dry["landing_review_tier_state"],
            landing_review_gate_requirement=
            dry["landing_review_gate_requirement"],
            landing_review_gate=dry["landing_review_gate"],
            closing_repo_id=dry["closing_repo_id"],
            closing_trunk_ref=dry["closing_trunk_ref"],
            closing_trunk_sha=dry["closing_trunk_sha"],
            proof_mode=dry["proof_mode"],
            translated_tip=dry["translated_tip"])
        self.assertIsNone(out)
        self.assertIn("approval anchor does not match", err)
        self.assertEqual(self.history_len(parent["id"]), before)

    def test_locked_writer_refuses_a_recreated_review_verdict_anchor(self):  # noqa: VACUOUS_ASSERTION — successful dry-run precedes one coherent-but-wrong verdict identity
        parent = self.build_parent(lane="lane/build-anchor")
        self.approved_child(parent, lane="lane/build-anchor-review")
        self.git("merge", "--no-edit", "-q", "side")
        dry, err = landreq.close(parent["id"], "landed", dry_run=True, live=True)
        self.assertIsNone(err)
        wrong = "f" * 32
        approval = dispatches._landed_review_approval_anchor(
            wrong, dry["landing_review_tier_state"],
            dry["landing_review_gate_requirement"], dry["landing_review_gate"])
        before = self.history_len(parent["id"])
        out, err = dispatches._record_close_proven(
            parent["id"], "landed", None, close_proof_version=2,
            landing_review_id=dry["landing_review_id"],
            landing_review_tip=dry["landing_review_tip"],
            landing_review_verdict_anchor=wrong,
            landing_review_tier_state=dry["landing_review_tier_state"],
            landing_review_gate_requirement=
            dry["landing_review_gate_requirement"],
            landing_review_gate=dry["landing_review_gate"],
            landing_review_approval_anchor=approval,
            closing_repo_id=dry["closing_repo_id"],
            closing_trunk_ref=dry["closing_trunk_ref"],
            closing_trunk_sha=dry["closing_trunk_sha"],
            proof_mode=dry["proof_mode"],
            translated_tip=dry["translated_tip"])
        self.assertIsNone(out)
        self.assertIn("verdict anchor does not match", err)
        self.assertEqual(self.history_len(parent["id"]), before)

    def test_closed_build_parent_refuses_every_later_mutation(self):  # noqa: VACUOUS_ASSERTION — a successful LANDED close is the positive control before all three mutation refusals
        parent = self.build_parent(lane="lane/build-terminal")
        self.approved_child(parent, lane="lane/build-terminal-review")
        self.git("merge", "--no-edit", "-q", "side")
        out, err = landreq.close(parent["id"], "landed", live=True)
        self.assertIsNone(err)
        self.assertEqual(out["state"], "LANDED")
        before = self.history_len(parent["id"])
        got, err = self.mark_verdict(
            parent["id"], self.b, "late verdict", polarity="approve")
        self.assertIsNone(got)
        self.assertIn("already closed", err)
        got, err = dispatches.mark_cancel(parent["id"], "late cancellation")
        self.assertIsNone(got)
        self.assertIn("already closed", err)
        got, err = dispatches.rebind(
            parent["id"], "another-seat", force=True, reason="late rebind")
        self.assertIsNone(got)
        self.assertIn("only an OPEN row", err)
        self.assertEqual(self.history_len(parent["id"]), before)

    def test_build_base_on_trunk_does_not_replace_child_landing_proof(self):  # noqa: VACUOUS_ASSERTION — child-absent refusal precedes landing exactly that child and a successful close
        parent = self.build_parent(lane="lane/base-is-not-review")
        child = self.approved_child(parent, lane="lane/base-is-not-review-review")
        self.assertEqual(subprocess.run(
            ("git", "merge-base", "--is-ancestor", self.b, self.main),
            cwd=self.repo).returncode, 0,
            "the BUILD base must positively be on trunk before the refusal")
        before = self.history_len(parent["id"])
        out, err = landreq.close(parent["id"], "landed", live=True)
        self.assertIsNone(out)
        self.assertIn("review descendant", err)
        self.assertEqual(self.history_len(parent["id"]), before)
        self.git("merge", "--no-edit", "-q", "side")
        out, err = landreq.close(parent["id"], "landed", live=True)
        self.assertIsNone(err)
        self.assertEqual(out["landing_review_id"], child["id"])

    def test_same_root_sibling_is_not_a_review_descendant(self):  # noqa: VACUOUS_ASSERTION — the sibling's accepted APPROVE is positively asserted before exact-ancestry refusal
        root = self.build_parent(lane="lane/root")
        parent = self.build_parent(supersedes=root["id"], lane="lane/child-build")
        sibling = self.approved_child(root, lane="lane/sibling-review",
                                      force=True)
        self.git("merge", "--no-edit", "-q", "side")
        before = self.history_len(parent["id"])
        out, err = landreq.close(parent["id"], "landed", live=True)
        self.assertIsNone(out)
        self.assertIn("exact review descendant", err)
        self.assertEqual(self.history_len(parent["id"]), before)
        self.assertEqual(dispatches.snapshot()[0][sibling["id"]]["polarity"],
                         "approve")

    def test_a_cherry_picked_review_tip_closes_via_patch_equivalence(self):  # noqa: VACUOUS_ASSERTION — the stored proof_mode and landing_review_id are unconditional positive controls; this is the arm a proof-rejecting mutation must kill (the FIX: it previously survived)
        """The build ladder accepts patch-equivalent proof — the integrator
        lands by cherry-pick, so the reviewed tip is routinely NO ancestor
        while its content is on trunk. A mutation rejecting
        patch-equivalence was measured SURVIVING the suite: this test is its
        killer."""
        parent = self.build_parent(lane="lane/cherry-build")
        child = self.approved_child(parent, lane="lane/cherry-review")
        self.git("cherry-pick", self.side)   # content lands, sha does not
        out, err = landreq.close(parent["id"], "landed", live=True)
        self.assertIsNone(err)
        self.assertEqual(out["landing_review_id"], child["id"])
        event = dispatches.history(parent["id"])[-1]
        self.assertEqual(event["close_proof_mode"], "patch-equivalent")

    def test_transitive_review_descendant_closes_the_build_parent(self):  # noqa: VACUOUS_ASSERTION — stored grandchild review id proves the transitive walk produced a real close
        parent = self.build_parent(lane="lane/transitive-build")
        middle = self.build_parent(
            supersedes=parent["id"], lane="lane/transitive-middle")
        child = self.approved_child(middle, lane="lane/transitive-review")
        self.git("merge", "--no-edit", "-q", "side")
        out, err = landreq.close(parent["id"], "landed", live=True)
        self.assertIsNone(err)
        self.assertEqual(out["landing_review_id"], child["id"])


_KEEP = object()   # 'do not force this row's chain_root' in the matrix below


class CloseLiveStepTest(CloseBase):
    """THE LIVE STEP — reaching TRUNK and reaching the RUNNING FLEET are two
    facts, and until this existed the ledger recorded only the first while
    implying the second.

    The incident, twice in one day: a landed fix seats could not
    benefit from until relaunch, and a "closes #158" land whose own commit
    message says it takes effect on RELAUNCH. Both rows read CLOSED (LANDED)
    and neither said a running process still held pre-land code.

    Owner rule (premise land-to-live-compression-owner-directive): CLI-class
    code is live AT LAND because every helm invocation is a fresh process off
    main; process-class code (beacons, the web service, proxies, daemons,
    in-memory seat state) is not, and stays not until something re-arms. The
    closer declares which; helm never guesses, and no declaration is REFUSED
    rather than defaulted to the quiet answer."""

    def landed_row(self, lane=None):
        row = self.verdict_row("approve", lane=lane)
        self.git("merge", "--no-edit", "-q", "side")
        return row

    # ------------------------------------------------ the declaration binds

    def test_cli_class_is_recorded_and_reads_back_as_live_at_land(self):
        row = self.landed_row(lane="lane/live-cli")
        out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(err)
        self.assertEqual(out["close_delivery_class"], "cli")
        event = dispatches.history(row["id"])[-1]
        self.assertEqual(event["close_delivery_class"], "cli")
        # and it survives a full replay off the ledger, not just this write
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(replayed["close_delivery_class"], "cli")
        self.assertIn("LIVE AT LAND",
                      landreq._delivery_phrase(landreq.get(row["id"])[0]))

    def test_cli_class_never_claims_a_restart_happened(self):
        """THE NEGATIVE CONTROL. A CLI-class close is the quiet, correct case
        and must stay quiet: it records NO restart target, prints no open
        loop, and never asserts that anything was cycled — helm restarts
        nothing, ever, and a ledger that implied otherwise would be the same
        false-completion bug one layer down."""
        row = self.landed_row(lane="lane/live-cli-control")
        out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(err)
        self.assertIsNone(out.get("close_delivery_restart"))
        event = dispatches.history(row["id"])[-1]
        self.assertNotIn("close_delivery_restart", event)
        lr = landreq.get(row["id"])[0]
        cli_line, cli_show = landreq._line(lr), landreq._render_show(lr)
        # MUST-HIT first: the same surfaces, on a process-class row, DO carry
        # every needle below. Without this control the assertNotIns would pass
        # just as happily against a renderer that prints nothing at all.
        other = self.verdict_row("approve", lane="lane/live-cli-control-x")
        _out, err = landreq.close(other["id"], "landed",
                                  needs_restart="the beacons")
        self.assertIsNone(err)
        loud = landreq.get(other["id"])[0]
        loud_line, loud_show = landreq._line(loud), landreq._render_show(loud)
        self.assertIn("LIVE OPEN LOOP", loud_line)
        self.assertIn("re-arm", loud_line)
        self.assertIn("the beacons", loud_line)
        self.assertIn("helm rearm", loud_show)
        # ...and the CLI-class row carries none of them. The noqa is exact:
        # the control above is a DIFFERENT ROW's surfaces, so no provenance
        # chain can reach these names, but it is unconditional and it runs.
        self.assertNotIn("OPEN LOOP", cli_line)  # noqa: VACUOUS_ASSERTION — the loud row above proves every needle is findable
        self.assertNotIn("OPEN LOOP", cli_show)
        self.assertNotIn("re-arm", cli_line)
        self.assertNotIn("re-arm", cli_show)
        self.assertNotIn("the beacons", cli_line)
        self.assertNotIn("the beacons", cli_show)

    def test_process_class_records_and_names_what_holds_pre_land_code(self):
        row = self.landed_row(lane="lane/live-process")
        out, err = landreq.close(row["id"], "landed",
                                 needs_restart="every chat-wait beacon")
        self.assertIsNone(err)
        self.assertEqual(out["close_delivery_class"], "process")
        self.assertEqual(out["close_delivery_restart"],
                         "every chat-wait beacon")
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(replayed["close_delivery_restart"],
                         "every chat-wait beacon")

    def test_process_class_row_renders_as_an_open_loop(self):
        """A row closed process-class with nothing re-armed is an HONEST open
        loop, and printing it is the point — on the compact row AND the show
        surface, naming what still holds pre-land code and the verb that
        measures it."""
        row = self.landed_row(lane="lane/live-open-loop")
        _out, err = landreq.close(row["id"], "landed",
                                  needs_restart="the helm-web unit")
        self.assertIsNone(err)
        lr = landreq.get(row["id"])[0]
        line = landreq._line(lr)
        self.assertIn("LIVE OPEN LOOP", line)
        self.assertIn("the helm-web unit", line)
        shown = landreq._render_show(lr)
        self.assertIn("LIVE OPEN LOOP", shown)
        self.assertIn("the helm-web unit", shown)
        self.assertIn("helm rearm", shown)
        # and on the wire the console renders from, not only the terminal
        card = landreq.card(lr)
        self.assertEqual(card["close_delivery_class"], "process")
        self.assertEqual(card["close_delivery_restart"], "the helm-web unit")

    # ------------------------------------------------------- UNKNOWN refuses

    def test_a_landed_close_with_no_declaration_refuses_and_appends_nothing(self):
        """The whole law: an unprovable answer never silently becomes live."""
        row = self.landed_row(lane="lane/live-undeclared")
        before = self.history_len(row["id"])
        out, err = landreq.close(row["id"], "landed")
        self.assertIsNone(out)
        self.assertIn("must declare the LIVE step", err)
        self.assertIn("--live", err)
        self.assertIn("--needs-restart", err)
        self.assertEqual(self.history_len(row["id"]), before)  # noqa: VACUOUS_ASSERTION — the MUST-HIT that follows closes the same row and asserts the length GREW; the rung cannot follow history_len's provenance
        snap = dispatches.snapshot()[0][row["id"]]
        self.assertIsNone(snap.get("close_reason"))
        # MUST-HIT: the very same close, declared, DOES append — so the
        # unchanged length above is the guard's doing, not a dead ledger.
        ok, err = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(err)
        self.assertEqual(ok["close_reason"], "landed")
        self.assertGreater(self.history_len(row["id"]), before)

    def test_declaring_both_classes_refuses_and_appends_nothing(self):
        row = self.landed_row(lane="lane/live-both")
        before = self.history_len(row["id"])
        out, err = landreq.close(row["id"], "landed", live=True,
                                 needs_restart="the beacons")
        self.assertIsNone(out)
        self.assertIn("declares ONE delivery class", err)
        self.assertEqual(self.history_len(row["id"]), before)  # noqa: VACUOUS_ASSERTION — the MUST-HIT that follows closes the same row and asserts the length GREW; the rung cannot follow history_len's provenance
        # MUST-HIT: the very same close, declared, DOES append — so the
        # unchanged length above is the guard's doing, not a dead ledger.
        ok, err = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(err)
        self.assertEqual(ok["close_reason"], "landed")
        self.assertGreater(self.history_len(row["id"]), before)

    def test_an_unnamed_restart_target_refuses_and_appends_nothing(self):
        """`--needs-restart ""` is a restart obligation nobody can discharge:
        it names no process, so no owner can ever act on it."""
        row = self.landed_row(lane="lane/live-unnamed")
        before = self.history_len(row["id"])
        out, err = landreq.close(row["id"], "landed", needs_restart="   ")
        self.assertIsNone(out)
        self.assertIn("must NAME what", err)
        self.assertEqual(self.history_len(row["id"]), before)  # noqa: VACUOUS_ASSERTION — the MUST-HIT that follows closes the same row and asserts the length GREW; the rung cannot follow history_len's provenance
        # MUST-HIT: the very same close, declared, DOES append — so the
        # unchanged length above is the guard's doing, not a dead ledger.
        ok, err = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(err)
        self.assertEqual(ok["close_reason"], "landed")
        self.assertGreater(self.history_len(row["id"]), before)

    def test_git_speaks_before_the_live_step_does(self):
        """RUNG ORDER, and it is load-bearing: a row Git never proved landed
        must hear THAT, not a lecture about --live. The declaration is the
        last rung precisely so it only ever asks about a change already
        proven on trunk."""
        row = self.verdict_row("approve", lane="lane/live-not-landed")
        before = self.history_len(row["id"])
        _out, err = landreq.close(row["id"], "landed")   # side NOT merged
        self.assertIn("neither an ancestor", err)
        self.assertNotIn("LIVE step", err)
        self.assertEqual(self.history_len(row["id"]), before)  # noqa: VACUOUS_ASSERTION — the MUST-HIT that follows closes the same row and asserts the length GREW; the rung cannot follow history_len's provenance
        self.git("merge", "--no-edit", "-q", "side")     # the MUST-HIT
        # MUST-HIT: the very same close, declared, DOES append — so the
        # unchanged length above is the guard's doing, not a dead ledger.
        ok, err = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(err)
        self.assertEqual(ok["close_reason"], "landed")
        self.assertGreater(self.history_len(row["id"]), before)

    def test_the_declaration_belongs_to_reason_landed_alone(self):
        """No other terminal claims a change reached the running fleet."""
        row = self.verdict_row("fix", lane="lane/live-wrong-reason")
        before = self.history_len(row["id"])
        out, err = landreq.close(row["id"], "withdrawn",
                                 evidence="kept off trunk", live=True)
        self.assertIsNone(out)
        self.assertIn("belongs to --reason landed", err)
        self.assertEqual(self.history_len(row["id"]), before)  # noqa: VACUOUS_ASSERTION — the MUST-HIT that follows closes the same row and asserts the length GREW; the rung cannot follow history_len's provenance
        # MUST-HIT: the SAME close, minus the misplaced declaration, lands an
        # event — so "nothing appended" above is the guard, not a dead ledger.
        out, err = landreq.close(row["id"], "withdrawn",
                                 evidence="kept off trunk")
        self.assertIsNone(err)
        self.assertEqual(out["close_reason"], "withdrawn")
        self.assertGreater(self.history_len(row["id"]), before)

    # ------------------------------------------------- pre-declaration rows

    def test_a_pre_declaration_row_renders_UNDECLARED_never_live(self):
        """Rows closed before this field existed carry neither key. They must
        replay (the terminal is immutable) and must read UNKNOWN — absence is
        never softened into "cli", which is exactly the implication that cost
        us the two incidents."""
        row = self.landed_row(lane="lane/live-legacy")
        _out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(err)
        # MUST-HIT before the strip: the row reads LIVE AT LAND right now, so
        # the UNDECLARED reading below is the strip's doing and not a renderer
        # that says UNDECLARED about everything.
        declared = landreq.get(row["id"])[0]
        self.assertIn("LIVE AT LAND", landreq._line(declared))
        self.assertIn("LIVE AT LAND", landreq._render_show(declared))
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for event in events:
                if event.get("id") == row["id"] \
                        and event.get("event") == "close":
                    event.pop("close_delivery_class", None)
                    event.pop("close_delivery_restart", None)
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err)
        self.assertEqual(lr["close_reason"], "landed",
                         "a pre-declaration close must still replay")
        self.assertIsNone(lr.get("close_delivery_class"))
        line, shown = landreq._line(lr), landreq._render_show(lr)
        self.assertIn("DELIVERY UNDECLARED", line)
        self.assertIn("DELIVERY UNDECLARED", shown)
        self.assertIn("UNKNOWN", line)
        self.assertIn("UNKNOWN", shown)
        self.assertNotIn("LIVE AT LAND", line)
        self.assertNotIn("LIVE AT LAND", shown)

    def test_a_malformed_declaration_is_inert_on_replay(self):
        """A forged event the writer would refuse must not bind on replay —
        and it must not bind by DEGRADING to live, either."""
        row = self.landed_row(lane="lane/live-forged")
        _out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(err)
        # MUST-HIT: the close BINDS before the forgery, so the None below is
        # the validator's refusal and not a row that never closed at all.
        self.assertEqual(landreq.get(row["id"])[0]["close_reason"], "landed")
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for event in events:
                if event.get("id") == row["id"] \
                        and event.get("event") == "close":
                    event["close_delivery_class"] = "definitely-live"
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err)
        self.assertIsNone(lr.get("close_reason"),
                          "a malformed declaration makes the close INERT")
        self.assertIsNone(lr.get("close_delivery_class"))
        self.assertIsNotNone(
            dispatches._delivery_error({"close_delivery_class": "cli",
                                        "close_delivery_restart": "beacons"}),
            "a CLI-class land declares no restart target")
        self.assertIsNone(
            dispatches._delivery_error({}),
            "both keys absent is the admissible pre-declaration shape")

    # -------------------------------------------------------- the two doors

    def test_a_build_row_carries_its_own_declaration(self):
        """A BUILD row closes with --reason landed too; leaving that door
        undeclared would reopen the exact hole the other door closes."""
        parent = dispatches.add(
            "builder", "lane/live-build", ref=self.b, repo=self.repo,
            kind="build", notify=False, new_work=True)
        child = dispatches.add(
            "reviewer", "lane/live-build-review", ref=self.side,
            repo=self.repo, kind="review", notify=False,
            supersedes=parent["id"])
        _out, err = self.mark_verdict(
            child["id"], self.side, "reviewed", polarity="approve")
        self.assertIsNone(err)
        self.git("merge", "--no-edit", "-q", "side")
        before = self.history_len(parent["id"])
        _out, err = landreq.close(parent["id"], "landed")
        self.assertIn("must declare the LIVE step", err)
        self.assertEqual(self.history_len(parent["id"]), before)
        out, err = landreq.close(parent["id"], "landed",
                                 needs_restart="the beacons")
        self.assertIsNone(err)
        self.assertEqual(out["close_delivery_class"], "process")
        self.assertEqual(out["close_delivery_restart"], "the beacons")
        event = dispatches.history(parent["id"])[-1]
        self.assertEqual(event["close_proof_version"], 2)
        self.assertEqual(event["close_delivery_restart"], "the beacons")
        self.assertIn("LIVE OPEN LOOP",
                      landreq._render_show(landreq.get(parent["id"])[0]))

    def test_an_unchained_approve_refusal_names_the_class_and_the_cure(self):  # noqa: VACUOUS_ASSERTION — assertIn(none CHAINS), assertIn(candidate-id) and assertIn(--supersedes) are unconditional positive controls on err content; the assertNotIn only proves the OLD generic message was displaced
        """The door KNOWS an approve fell on chain topology, not landedness —
        it must say so. Measured on the live directive row: a
        --new-work review chain never touches the build parent, the walk
        continued silently, and the generic landedness refusal printed with
        no detail for a chain problem. The chained sibling test above is the
        positive control: the same shape with supersedes= closes."""
        parent = dispatches.add(
            "builder", "lane/unchained-build", ref=self.b, repo=self.repo,
            kind="build", notify=False, new_work=True)
        child = dispatches.add(
            "reviewer", "lane/unchained-review", ref=self.side,
            repo=self.repo, kind="review", notify=False, new_work=True)
        _out, err = self.mark_verdict(
            child["id"], self.side, "reviewed", polarity="approve")
        self.assertIsNone(err)
        self.git("merge", "--no-edit", "-q", "side")
        _out, err = landreq.close(parent["id"], "landed",
                                  needs_restart="the beacons")
        self.assertIsNotNone(err)
        self.assertIn("none CHAINS to this parent", err)
        self.assertIn(child["id"][:12], err)
        self.assertIn("--supersedes", err)
        self.assertNotIn("no exact review descendant", err)

    def _unchained_refusal_with_roots(self, review_root, parent_root):
        """Drive the build-landed door with the two chain roots FORCED, and
        return its refusal. One helper, two directions — the point was
        that a one-sided test cannot see a one-sided guard."""
        parent = dispatches.add(
            "builder", "lane/roots-build-%s-%s" % (review_root, parent_root),
            ref=self.b, repo=self.repo, kind="build", notify=False,
            new_work=True)
        child = dispatches.add(
            "reviewer", "lane/roots-review-%s-%s" % (review_root, parent_root),
            ref=self.side, repo=self.repo, kind="review", notify=False,
            new_work=True)
        _out, err = self.mark_verdict(
            child["id"], self.side, "reviewed", polarity="approve")
        self.assertIsNone(err)
        self.git("merge", "--no-edit", "-q", "side")
        real = dispatches.snapshot_with_verdicts

        def forced():
            current, verdicts, unavailable = real()
            # HERMETIC PER CALL: this helper is invoked repeatedly inside one
            # test, so rows from earlier iterations otherwise accumulate and
            # legitimately fire the arm — a fixture leak that reads exactly
            # like a code defect (it cost one confusing subtest failure).
            current = {rid: row for rid, row in current.items()
                       if rid in (parent["id"], child["id"])}
            if review_root is not _KEEP and child["id"] in current:
                current[child["id"]] = dict(current[child["id"]],
                                            chain_root=review_root)
            if parent_root is not _KEEP and parent["id"] in current:
                current[parent["id"]] = dict(current[parent["id"]],
                                             chain_root=parent_root)
            return current, verdicts, unavailable

        with mock.patch.object(dispatches, "snapshot_with_verdicts", forced):
            _out, err = landreq.close(parent["id"], "landed",
                                      needs_restart="the beacons")
        self.assertIsNotNone(err)
        return err

    def test_an_UNKNOWN_root_on_EITHER_side_suppresses_the_unchained_arm(self):  # noqa: VACUOUS_ASSERTION — the determinate/determinate control at the end positively asserts the arm still FIRES, so the two suppression assertions cannot pass by the arm being dead
        """codex FIX r3 second pass: CHAIN_UNKNOWN is the truthy string
        "UNKNOWN", so a truthiness guard admitted it and compared it as an
        id — on EITHER side. Both directions are tested because a one-sided
        test cannot see a one-sided guard, and the determinate/determinate
        case is the must-hit proving the arm is not simply switched off."""
        for review_root, parent_root in (
                (dispatches.CHAIN_UNKNOWN, _KEEP),
                (_KEEP, dispatches.CHAIN_UNKNOWN),
                (None, _KEEP),
                (_KEEP, None)):
            with self.subTest(review=review_root, parent=parent_root):
                err = self._unchained_refusal_with_roots(review_root,
                                                         parent_root)
                self.assertIn("exact review descendant", err)
                self.assertNotIn("none CHAINS", err)
        # MUST-HIT: two DETERMINATE, DIFFERENT roots still reach the arm —
        # without this the four suppressions above prove nothing
        err = self._unchained_refusal_with_roots("rootaaaa1111", "rootbbbb2222")
        self.assertIn("none CHAINS", err)

    def test_an_unknown_chain_root_suppresses_the_unchained_arm(self):  # noqa: VACUOUS_ASSERTION — assertIn(exact review descendant) positively pins the generic refusal displacing the arm; the assertNotIn proves only which message won
        """codex FIX r3: an absent chain_root is a LEGACY row whose chaining
        is UNKNOWN (404 live rows measured), and UNKNOWN suppresses — the
        own-id fallback branded every legacy approve confidently unchained.
        The unchained test above is the positive control: determinate
        disjoint roots still fire."""
        parent = dispatches.add(
            "builder", "lane/legacy-build", ref=self.b, repo=self.repo,
            kind="build", notify=False, new_work=True)
        child = dispatches.add(
            "reviewer", "lane/legacy-review", ref=self.side,
            repo=self.repo, kind="review", notify=False, new_work=True)
        _out, err = self.mark_verdict(
            child["id"], self.side, "reviewed", polarity="approve")
        self.assertIsNone(err)
        self.git("merge", "--no-edit", "-q", "side")
        real = dispatches.snapshot_with_verdicts
        def legacy_shaped():
            current, verdicts, unavailable = real()
            for rid in (child["id"],):
                if rid in current:
                    current[rid] = dict(current[rid], chain_root=None)
            return current, verdicts, unavailable
        with mock.patch.object(dispatches, "snapshot_with_verdicts",
                               legacy_shaped):
            _out, err = landreq.close(parent["id"], "landed",
                                      needs_restart="the beacons")
        self.assertIsNotNone(err)
        self.assertIn("exact review descendant", err)
        self.assertNotIn("none CHAINS", err)

    def test_the_cli_drives_both_doors_and_prints_the_live_step(self):
        row = self.landed_row(lane="lane/live-cli-surface")
        rc, _out, err = run(["close", row["id"][:12], "--reason", "landed"])
        self.assertEqual(rc, 1)
        self.assertIn("must declare the LIVE step", err)
        rc, out, err = run(["close", row["id"][:12], "--reason", "landed",
                            "--needs-restart", "the seat proxies"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("CLOSED (LANDED)", out)
        self.assertIn("LIVE OPEN LOOP", out)
        self.assertIn("the seat proxies", out)

    def test_the_cli_refuses_both_doors_at_the_usage_tier(self):
        row = self.landed_row(lane="lane/live-cli-usage")
        rc, _out, err = run(["close", row["id"][:12], "--reason", "withdrawn",
                             "--live"])
        self.assertEqual(rc, 2)
        self.assertIn("--live/--needs-restart belong to --reason landed", err)
        before = self.history_len(row["id"])
        snap = dispatches.snapshot()[0][row["id"]]
        self.assertIsNone(snap.get("close_reason"))
        # MUST-HIT: the very same close, declared, DOES append — so the
        # unchanged length above is the guard's doing, not a dead ledger.
        ok, err = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(err)
        self.assertEqual(ok["close_reason"], "landed")
        self.assertGreater(self.history_len(row["id"]), before)
        landed = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(landed.get("close_reason"), "landed")

    def test_a_dry_run_reports_the_live_step_and_appends_nothing(self):
        row = self.landed_row(lane="lane/live-dry")
        before = self.history_len(row["id"])
        rc, out, err = run(["close", row["id"][:12], "--reason", "landed",
                            "--needs-restart", "the web unit", "--dry-run"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("WOULD close", out)
        self.assertIn("LIVE OPEN LOOP", out)
        self.assertEqual(self.history_len(row["id"]), before)  # noqa: VACUOUS_ASSERTION — the MUST-HIT that follows closes the same row and asserts the length GREW; the rung cannot follow history_len's provenance
        # MUST-HIT: the same argv without --dry-run DOES append, so "nothing
        # appended" is the dry run's doing.
        rc, out, err = run(["close", row["id"][:12], "--reason", "landed",
                            "--needs-restart", "the web unit"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("CLOSED (LANDED)", out)
        self.assertGreater(self.history_len(row["id"]), before)


class LiveDeclarationIsCheckedTest(CloseBase):
    """--live was a DECLARATION nothing ever compared against the land.

    The class above records which delivery class the closer declares. Nothing
    asked whether the declaration was TRUE of the code that actually landed,
    so a land that moved helm/web.py or a beacon could close --live and the
    ledger would record "live at land" about code no running process would
    execute until it re-armed. Same BUILT != WIRED shape as the guard-rail
    rung beside it, reached from the other side: that one asks whether THIS
    BOX is armed, this one asks what THIS LAND touched.

    The base the row does not record comes from topology — the two-parent
    merge that carried the reviewed tip onto trunk has trunk-before as its
    first parent, so its own diff is exactly what the land added. MEASURED
    Measured over the coordination ledger: answerable on 62 of the 65
    landed closes carrying a delivery class, and 8 of those 65 --live
    declarations were about a land that moved process-class code."""

    def touch(self, path, text, branch="side"):
        """Add one file to `branch` and return its tip."""
        head = self.git("symbolic-ref", "--short", "HEAD")
        self.git("checkout", "-q", branch)
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
        self.git("add", path)
        self.git("commit", "-q", "-m", "touch " + path)
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", head)
        return tip

    def landed_row_touching(self, path, lane):
        """An approved row whose land carries `path` onto trunk by merge."""
        tip = self.touch(path, "# a long-lived process holds this")
        row = self.verdict_row("approve", ref=tip, lane=lane)
        self.git("merge", "--no-edit", "-q", "side")
        return row, tip

    # ------------------------------------------------------- both directions

    def test_a_live_land_touching_process_class_code_is_REFUSED(self):
        """The direction that did not exist. helm/web_server.py is held by the
        helm-web unit from start to restart, so `--live` about a land that
        moved it is false the moment it is written."""
        row, tip = self.landed_row_touching("helm/web_server.py",
                                            "lane/live-touches-web")
        before = self.history_len(row["id"])
        rc, _out, err = run(["close", row["id"][:12], "--reason", "landed",
                             "--live"])
        self.assertEqual(rc, 1)
        self.assertIn("PROCESS-CLASS", err)
        self.assertIn("helm/web_server.py", err,
                      "a refusal must NAME the file it is about")
        self.assertIn("--needs-restart", err,
                      "a refusal must name the escape that clears it")
        self.assertEqual(self.history_len(row["id"]), before)  # noqa: VACUOUS_ASSERTION — the escape below closes the SAME row and asserts the length GREW
        self.assertIsNone(
            dispatches.snapshot()[0][row["id"]].get("close_reason"),
            "nothing may be written")
        # POSITIVE CONTROL on the same observable: the truer declaration
        # closes the SAME row, so the empty field above is this rung's doing
        # and not a ledger that was never writable.
        rc2, _o2, err2 = run(["close", row["id"][:12], "--reason", "landed",
                              "--needs-restart", "the helm-web unit"])
        self.assertEqual((rc2, err2), (0, ""))
        snap = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(snap["close_reason"], "landed")
        self.assertEqual(snap["close_delivery_class"], "process")
        self.assertGreater(self.history_len(row["id"]), before)
        # and the mechanism really ran over this land's real files
        self.assertIn("helm/web_server.py",
                      landreq._land_files(self.gitdir(), tip,
                                          self.git("rev-parse", self.main))[0])

    def test_a_cli_class_land_closes_CLEAN(self):
        """The other direction, and the one that must not become a tax: a land
        that touches only CLI code still closes --live with no argument."""
        row, tip = self.landed_row_touching("helm/landreq.py",
                                            "lane/live-touches-cli")
        rc, out, err = run(["close", row["id"][:12], "--reason", "landed",
                            "--live"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("CLOSED (LANDED)", out)
        self.assertEqual(
            dispatches.snapshot()[0][row["id"]]["close_delivery_class"], "cli")
        # NOT VACUOUS: the clean pass must be a computed empty intersection,
        # never an empty file list. Enumerate what the rung actually saw.
        files, why = landreq._land_files(self.gitdir(), tip,
                                         self.git("rev-parse", self.main))
        self.assertIsNone(why)
        self.assertIn("helm/landreq.py", files)
        self.assertEqual([p for p in files
                          if landreq._process_class_path(p)], [])

    # ------------------------------------------------------------- can't-tell

    def test_an_unresolvable_sha_is_a_CANT_TELL_and_never_refuses(self):
        """UNKNOWN IS NOT A REFUSAL — the constraint the sibling rung fails
        open on, asserted here at both altitudes."""
        gitdir = self.gitdir()
        pinned = self.git("rev-parse", self.main)
        for tip in ("not-a-sha", "", "0" * 39):
            files, why = landreq._land_files(gitdir, tip, pinned)
            self.assertIsNone(files, tip)
            self.assertTrue(why, "a can't-tell must carry a reason")
            self.assertIsNone(landreq._process_class_refusal(
                "row", gitdir, tip, "main", pinned, landreq.DELIVERY_CLI))
        # a resolvable-but-absent object is the same answer, from git itself
        self.assertIsNone(landreq._land_files(gitdir, "0" * 40, pinned)[0])
        # MUST-HIT: the identical call with a REAL landed tip does answer, so
        # the Nones above are the arms and not a helper that never works.
        tip = self.touch("helm/web.py", "# held by helm-web")
        self.git("merge", "--no-edit", "-q", "side")
        answered, why = landreq._land_files(gitdir, tip,
                                            self.git("rev-parse", self.main))
        self.assertIsNone(why)
        self.assertIn("helm/web.py", answered)

    def test_a_patch_equivalent_land_cannot_attribute_and_closes_CLEAN(self):
        """The can't-tell arm that reaches all the way through the verb. A
        cherry-picked land leaves the reviewed tip OFF trunk, so there is no
        ancestry path and no fold merge to read a base from — even though the
        content that landed is process-class. The row still closes: a rung
        that cannot measure must not author a refusal."""
        tip = self.touch("helm/beacons.py", "# held by an armed waiter")
        row = self.verdict_row("approve", ref=tip, lane="lane/live-cherry")
        self.git("cherry-pick", tip)
        gitdir, pinned = self.gitdir(), self.git("rev-parse", self.main)
        files, why = landreq._land_files(gitdir, tip, pinned)
        self.assertIsNone(files)
        self.assertIn("merge", why)
        out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(err)
        self.assertEqual(out["close_proof_mode"], "patch-equivalent")
        self.assertEqual(out["close_delivery_class"], "cli")

    # ------------------------------------------------------------- the set

    def test_the_BUILD_door_asks_about_the_TRANSLATED_tip(self):
        """The missed arm, and it opened on the COMMON case.

        _close_ladder_build_landed already computes a `translated` tip,
        precisely because a rebased lane's ORIGINAL object can be gone. It
        then passed the ORIGINAL to the process-class rung — so _land_files
        answered UNKNOWN, UNKNOWN fails open by design, and --live closed on
        a land that moved process-class code. The hole was in the door I
        added so that there would not be one.

        This pins the seam directly: the rung must be asked about the tip the
        ladder RESOLVED, not the one the row recorded. Both doors now call it
        the same way (`translated or reviewed_tip`), which is what the
        ordinary ladder always did."""
        import inspect
        src = inspect.getsource(landreq._close_ladder_build_landed)
        # UNCONDITIONAL CONTROL: the rung is called at this door at all.
        # Without it, deleting the call entirely would satisfy every
        # assertion below by making the wrong spelling absent too.
        self.assertIn("_process_class_refusal(", src,
                      "the BUILD door no longer asks the process-class "
                      "question at all — that is the hole, not a fix for it")
        call = src[src.index("_process_class_refusal("):]
        call = call[:call.index(")")]
        self.assertIn("translated", call,
                      "the BUILD door asks about a tip it did not resolve; a "
                      "rebased lane's original object is gone, _land_files "
                      "answers UNKNOWN, and UNKNOWN fails open — so --live "
                      "closes on process-class code. Got: %r" % call)

    def test_only_imported_MODULES_are_process_class(self):
        """The suffix rule is load-bearing, and it caught a real false refusal
        rather than decorating one. The first replay of this rung over history
        refused a land for helm/web_ui.html — but web_ui_loader re-reads the
        manifest and every fragment ON EACH REQUEST, deliberately, to keep the
        old hot-template lifetime. Those files ARE live at land. A long-lived
        process freezes what it imported, not what it re-reads."""
        # UNCONDITIONAL both ways before either loop runs, so an empty or
        # skipped table can never make this pass by not asserting.
        self.assertTrue(landreq._process_class_path("helm/web_server.py"))
        self.assertFalse(landreq._process_class_path("helm/web_ui.html"))
        for held in ("helm/web.py", "helm/web_server.py", "helm/web_ui_loader.py",
                     "helm/beacons.py", "helm/chat.py", "helm/watchdog.py",
                     "./helm/proxywatch.py"):
            self.assertTrue(landreq._process_class_path(held), held)
        for live in ("helm/web_ui.html", "helm/web_ui/manifest.txt",
                     "helm/web_ui/views/roster.part", "helm/landreq.py",
                     "helm/cli.py", "tests/test_web_tasks.py", "docs/WEB.md",
                     "", None):
            self.assertFalse(landreq._process_class_path(live), live)

    def test_the_BUILD_door_is_checked_too(self):
        """Both landed doors or neither. A BUILD row closes with --reason
        landed through an approved review descendant, and leaving that door
        unchecked would reopen the exact hole the other one closes."""
        parent = dispatches.add("builder", "lane/build-live", ref=self.b,
                                repo=self.repo, kind="build", notify=False,
                                new_work=True)
        tip = self.touch("helm/web_chat.py", "# held by helm-web")
        child = dispatches.add("reviewer", "lane/build-live-review", ref=tip,
                               repo=self.repo, kind="review", notify=False,
                               supersedes=parent["id"])
        _out, err = self.mark_verdict(child["id"], tip, "reviewed",
                                            polarity="approve")
        self.assertIsNone(err)
        self.git("merge", "--no-edit", "-q", "side")
        before = self.history_len(parent["id"])
        out, err = landreq.close(parent["id"], "landed", live=True)
        self.assertIsNone(out)
        self.assertIn("helm/web_chat.py", err)
        self.assertEqual(self.history_len(parent["id"]), before)  # noqa: VACUOUS_ASSERTION — the escape below closes the SAME row and asserts the length GREW
        # POSITIVE CONTROL: the same door opens for the truer declaration.
        out, err = landreq.close(parent["id"], "landed",
                                 needs_restart="the helm-web unit")
        self.assertIsNone(err)
        self.assertEqual(out["close_delivery_class"], "process")
        self.assertGreater(self.history_len(parent["id"]), before)


class CloseSupersededTest(CloseBase):
    """The discharge ladder ported whole onto close --reason superseded,
    keeping every designed kill, plus the approve-row widening."""

    def handoff(self, lane, ref, supersedes=None, force=False):
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

    def test_superseded_closes_the_debt_and_records_the_pinned_trunk(self):
        first, second, fixed = self.resolved()
        pinned = self.git("rev-parse", self.main)
        out, err = landreq.close(first["id"][:12], "superseded",
                                 evidence="r2 approved", tip=fixed)
        self.assertIsNone(err)
        snap = dispatches.snapshot()[0][first["id"]]
        self.assertEqual(snap["close_reason"], "superseded")
        self.assertEqual(snap["superseding_tip"], fixed)
        self.assertEqual(snap["superseding_id"], second["id"])
        self.assertEqual(snap["close_contrary_state"], "landed")
        self.assertEqual(snap["close_contrary_target"], "local")
        self.assertEqual(snap["closing_trunk_sha"], pinned)  # D7 audit trail
        self.assertEqual(snap["polarity"], "fix")            # verdict intact

    def test_identical_retry_is_idempotent_and_conflict_is_refused(self):
        first, _second, fixed = self.resolved()
        _out, err = landreq.close(first["id"], "superseded",
                                  evidence="resolved", tip=fixed)
        self.assertIsNone(err)
        before = self.history_len(first["id"])
        _out, err = landreq.close(first["id"], "superseded",
                                  evidence="resolved", tip=fixed)
        self.assertIsNone(err)
        self.assertEqual(self.history_len(first["id"]), before)
        _out, err = landreq.close(first["id"], "superseded",
                                  evidence="different", tip=fixed)
        self.assertIn("retired once", err)
        self.assertEqual(self.history_len(first["id"]), before)

    def test_an_earlier_approve_cannot_launder_a_later_fix(self):
        base = self.git("rev-parse", self.main)
        self.git("checkout", "-q", "-b", "approved-first", base)
        with open(os.path.join(self.repo, "shared"), "w",
                  encoding="utf-8") as f:
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
        with open(os.path.join(self.repo, "shared"), "w",
                  encoding="utf-8") as f:
            f.write("same patch\n")
        self.git("add", "shared")
        self.git("commit", "-q", "-m", "rejected later")
        rejected_tip = self.git("rev-parse", "HEAD")
        rejected = self.handoff("rejected-later", rejected_tip)
        self.mark_verdict(rejected["id"], rejected_tip, "new findings",
                                polarity="fix")
        self.git("checkout", "-q", self.main)
        _out, err = landreq.close(rejected["id"], "superseded",
                                  evidence="old approval", tip=approved_tip)
        self.assertIn("no later APPROVE", err)

    def test_concurrent_identical_close_reconciles_as_success(self):
        """The :972 pattern at the close() level: the racing writer lands the
        SAME close during the ladder's pre-lock ledger read."""
        first, second, fixed = self.resolved()
        real = dispatches.snapshot_with_verdicts
        before = self.history_len(first["id"])
        pinned = self.git("rev-parse", self.main)
        fired = []

        def race():
            if not fired:
                fired.append(True)
                row, why = dispatches._record_close_proven(
                    first["id"], "superseded", self.side,
                    evidence="resolved", superseding_tip=fixed,
                    superseding_id=second["id"], contrary_state="landed",
                    contrary_target="local",
                    closing_trunk_ref="refs/heads/" + self.main,
                    closing_trunk_sha=pinned)
                self.assertIsNone(why)
                self.assertEqual(row["close_reason"], "superseded")
            return real()

        with mock.patch.object(dispatches, "snapshot_with_verdicts",
                               side_effect=race):
            _out, err = landreq.close(first["id"], "superseded",
                                      evidence="resolved", tip=fixed)
        self.assertIsNone(err)
        self.assertEqual(self.history_len(first["id"]), before + 1)

    def test_close_requires_an_approved_superseding_dispatch(self):
        first = self.handoff("feature-r1", self.side)
        self.mark_verdict(first["id"], self.side, "findings",
                                polarity="fix")
        self.git("merge", "--no-edit", "-q", "side")
        descendant = self.commit("unreviewed descendant")
        _out, err = landreq.close(first["id"], "superseded",
                                  evidence="trust me", tip=descendant)
        self.assertIn("no later APPROVE verdict", err)

    def test_gate_capable_ungated_approve_cannot_close(self):
        first = self.handoff("feature-r1", self.side)
        self.mark_verdict(first["id"], self.side, "findings",
                                polarity="fix")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve without gate", path="g")
        self.git("checkout", "-q", self.main)
        second = self.handoff("feature-r2", fixed, supersedes=first["id"])
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": second["seq"] + 1,
            "id": second["id"], "ts": dispatches.pk.now_ts(),
            "reviewed_tip": fixed, "verdict_ref": "approve without receipt",
            "polarity": "approve", "gate": "",
            "gate_caps": [dispatches.GATE_CAP_RECEIPT]}))
        self.git("merge", "--no-edit", "-q", "side")
        before = self.history_len(first["id"])
        _out, err = landreq.close(first["id"], "superseded",
                                  evidence="ungated successor", tip=fixed)
        self.assertIn("no later APPROVE", err)
        self.assertEqual(self.history_len(first["id"]), before)

    def test_gate_capable_bound_approve_can_close(self):
        with mock.patch.object(
                dispatches, "GATE_CAPS", (dispatches.GATE_CAP_RECEIPT,)), \
                mock.patch.object(
                    dispatches.gate, "bind",
                    return_value=("VERIFIED", "a" * 16, "test receipt")):
            first, _second, fixed = self.resolved()
        out, err = landreq.close(first["id"], "superseded",
                                 evidence="gated successor", tip=fixed)
        self.assertIsNone(err)
        self.assertEqual(dispatches.snapshot()[0][first["id"]]
                         ["close_reason"], "superseded")

    def test_a_same_chain_approve_that_never_touched_the_author_refuses(self):
        """THE HANDOFF ADMIT'S LIMIT. A cross-sender approve that DECLARES it
        continues the author's own row is a provable handoff and pays the debt
        (pinned in test_lr_contrary's ContraryDischargeTest). Sharing a chain ROOT
        is weaker than that: when the candidate's supersedes ancestry never
        reaches a row the debt's author sent, no handoff happened — and the
        refusal must name BOTH legs, so the near-miss candidate is visible to
        the next integrator instead of vanishing into the same-author
        conjunction."""
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "other-author"}):
            root = self.handoff("feature-r0", self.side)
        first = self.handoff("feature-r1", self.side,
                             supersedes=root["id"])
        self.mark_verdict(first["id"], self.side, "findings",
                                polarity="fix")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve by another author", path="g")
        self.git("checkout", "-q", self.main)
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "other-author"}):
            second = self.handoff("other-author-r2", fixed,
                                  supersedes=root["id"], force=True)
            self.mark_verdict(second["id"], fixed, "clean",
                                    polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        _out, err = landreq.close(first["id"], "superseded",
                                  evidence="wrong author", tip=fixed)
        self.assertIn("same author/repo", err)
        self.assertIn("proved a lane handoff", err)

    def test_unrelated_chain_cannot_launder_the_contrary(self):
        first = self.handoff("feature-r1", self.side)
        self.mark_verdict(first["id"], self.side, "findings",
                                polarity="fix")
        other = self.handoff("other-r2", self.c)
        self.mark_verdict(other["id"], self.c, "clean",
                                polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        _out, err = landreq.close(first["id"], "superseded",
                                  evidence="unrelated", tip=self.c)
        self.assertIn("SAME WORK CHAIN", err)

    def test_a_chained_round_still_needs_git_lineage(self):
        first = self.handoff("feature-r1", self.side)
        self.mark_verdict(first["id"], self.side, "findings",
                                polarity="fix")
        second = self.handoff("feature-r2", self.c, supersedes=first["id"])
        self.mark_verdict(second["id"], self.c, "clean",
                                polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        _out, err = landreq.close(first["id"], "superseded",
                                  evidence="same chain, wrong tip",
                                  tip=self.c)
        self.assertIn("does not contain", err)

    def test_unknown_git_proof_refuses_without_appending(self):
        first, _second, fixed = self.resolved()
        real = landreq._landed

        def unknown(gitdir, tip, ref):
            if tip == self.side and ref == fixed:
                return None
            return real(gitdir, tip, ref)

        before = self.history_len(first["id"])
        with mock.patch.object(landreq, "_landed", side_effect=unknown):
            _out, err = landreq.close(first["id"], "superseded",
                                      evidence="resolved", tip=fixed)
        self.assertIn("could not prove", err)
        self.assertEqual(self.history_len(first["id"]), before)

    def test_the_original_tip_cannot_supersede_itself(self):
        first = self.handoff("feature-r1", self.side)
        self.mark_verdict(first["id"], self.side, "findings",
                                polarity="fix")
        self.git("merge", "--no-edit", "-q", "side")
        _out, err = landreq.close(first["id"], "superseded",
                                  evidence="same", tip=self.side)
        self.assertIn("cannot supersede itself", err)

    def test_stale_tracking_ref_without_origin_cannot_override_local(self):
        first = self.handoff("feature-r1", self.side)
        self.mark_verdict(first["id"], self.side, "findings",
                                polarity="fix")
        self.git("merge", "--no-edit", "-q", "side")
        self.git("checkout", "-q", "-b", "resolution", self.side)
        fixed = self.commit("resolved but not landed locally", path="g")
        second = self.handoff("feature-r2", fixed, supersedes=first["id"])
        self.mark_verdict(second["id"], fixed, "clean",
                                polarity="approve")
        self.git("checkout", "-q", self.main)
        self.git("update-ref", "refs/remotes/origin/main", fixed)
        self.assertFalse(self.git("remote"))
        _out, err = landreq.close(first["id"], "superseded",
                                  evidence="stale ref", tip=fixed)
        self.assertIn("authoritative trunk", err)

    def test_configured_origin_without_a_tracking_ref_is_unknown(self):
        bare = os.path.join(self.tmp, "unfetched-origin.git")
        subprocess.run(["git", "init", "--bare", "-q", bare], check=True)
        self.git("push", "-q", bare, "%s:main" % self.main)
        self.git("remote", "add", "origin", bare)
        first, _second, fixed = self.resolved()
        _out, err = landreq.close(first["id"], "superseded",
                                  evidence="not remotely observed", tip=fixed)
        self.assertIn("tracking ref", err)

    def test_superseding_tip_must_reach_authoritative_upstream(self):
        first, _second, fixed = self.resolved(upstream=True, push=False)
        _out, err = landreq.close(first["id"], "superseded",
                                  evidence="not pushed", tip=fixed)
        self.assertIn("authoritative trunk", err)
        self.git("push", "-q", "origin", self.main)
        out, err = landreq.close(first["id"], "superseded",
                                 evidence="pushed", tip=fixed)
        self.assertIsNone(err)
        snap = dispatches.snapshot()[0][first["id"]]
        self.assertEqual(snap["close_contrary_target"], "upstream")

    def test_approve_row_supersession_positive_and_noncontaining_inverse(self):
        """The widening: an approved-but-replaced round closes superseded when
        the superseding tip CONTAINS it, landed by cherry-pick, while the
        reviewed change itself never reached trunk."""
        reviewed_row = self.handoff("approved-then-replaced", self.side)
        self.mark_verdict(reviewed_row["id"], self.side, "clean",
                                polarity="approve")
        self.git("checkout", "-q", "side")
        superseding = self.commit("the rework that actually shipped",
                                  path="g2")
        self.git("checkout", "-q", self.main)
        second = self.handoff("approved-then-replaced-r2", superseding,
                              supersedes=reviewed_row["id"])
        self.mark_verdict(second["id"], superseding, "clean",
                                polarity="approve")
        self.git("cherry-pick", superseding)   # S's diff lands, R's never does
        gitdir = self.gitdir()
        self.assertTrue(landreq._landed(gitdir, self.side, superseding))
        self.assertFalse(landreq._landed(
            gitdir, self.side, self.git("rev-parse", self.main)))
        out, err = landreq.close(reviewed_row["id"], "superseded",
                                 evidence="replaced by the rework",
                                 tip=superseding)
        self.assertIsNone(err)
        snap = dispatches.snapshot()[0][reviewed_row["id"]]
        self.assertEqual(snap["close_reason"], "superseded")
        self.assertEqual(snap["close_contrary_state"], "none")
        # inverse: a non-containing tip refuses at the containment rung
        other_row = self.handoff("approved-never-contained", self.b)
        self.mark_verdict(other_row["id"], self.b, "clean",
                                polarity="approve")
        third = self.handoff("approved-never-contained-r2", superseding,
                             supersedes=other_row["id"])
        self.mark_verdict(third["id"], superseding, "clean",
                                polarity="approve")
        _out, err = landreq.close(other_row["id"], "superseded",
                                  evidence="unrelated", tip=superseding)
        # side forked BEFORE self.b, so the superseding tip does not carry
        # b's change — the containment rung refuses.
        self.assertIn("does not contain", err)

    def test_reviewed_tip_on_trunk_refuses_superseded_for_approve_rows(self):
        """The refutation sub-finding rung, MANDATORY for approve rows:
        annotation truth over the trivially-contains trap."""
        reviewed_row = self.handoff("approved-and-landed", self.side)
        self.mark_verdict(reviewed_row["id"], self.side, "clean",
                                polarity="approve")
        self.git("checkout", "-q", "side")
        superseding = self.commit("descendant round", path="g3")
        self.git("checkout", "-q", self.main)
        second = self.handoff("approved-and-landed-r2", superseding,
                              supersedes=reviewed_row["id"])
        self.mark_verdict(second["id"], superseding, "clean",
                                polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")   # BOTH land
        before = self.history_len(reviewed_row["id"])
        _out, err = landreq.close(reviewed_row["id"], "superseded",
                                  evidence="but it landed", tip=superseding)
        self.assertIn("IS on trunk", err)
        self.assertIn("--reason landed", err)
        self.assertEqual(self.history_len(reviewed_row["id"]), before)

    def test_open_rows_have_no_verdict_to_supersede(self):
        row = self.dispatch(lane="lane/open-supersede")
        _out, err = landreq.close(row["id"], "superseded",
                                  evidence="nope", tip=self.c)
        self.assertIn("has no verdict to supersede", err)

    def test_cli_superseded_json_happy_path_and_unknown_flag(self):
        first, _second, fixed = self.resolved()
        rc, out, err = run(["close", first["id"][:12], "--reason",
                            "superseded", "--tip", fixed, "--evidence",
                            "r2 approved", "--json"])
        self.assertEqual((rc, err), (0, ""), err)
        self.assertEqual(json.loads(out)["id"], first["id"])
        rc, _out, err = run(["close", first["id"], "--reason", "superseded",
                             "--tip", fixed, "--evidence", "x", "--wat"])
        self.assertEqual(rc, 2)


class CloseWithdrawnTest(CloseBase):
    """The withdraw ladder ported whole onto close --reason withdrawn."""

    def fix_row(self, polarity="fix"):
        row, why, sent = dispatches.send(
            "opus-integrator", "lane/evals-0723", "review evals-0723",
            self.side, repo=self.repo, key="key-evals", sign=False,
            new_work=True)
        self.assertIsNone(why)
        self.assertTrue(sent)
        self.mark_verdict(row["id"], self.side,
                                "findings: keep untracked", polarity=polarity)
        return row

    def test_withdrawn_records_the_pinned_absence_anchor(self):
        row = self.fix_row()
        pinned = self.git("rev-parse", self.main)
        rc, out, err = run(["close", row["id"][:12], "--reason", "withdrawn",
                            "--evidence", "kept untracked as instructed"])
        self.assertEqual((rc, err), (0, ""))
        snap = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(snap["close_reason"], "withdrawn")
        self.assertEqual(snap["close_evidence"],
                         "kept untracked as instructed")
        self.assertEqual(snap["absence_trunk_sha"], pinned)
        self.assertEqual(snap["absence_trunk_ref"],
                         "refs/heads/" + self.main)
        self.assertEqual(snap["polarity"], "fix")   # verdict preserved

    def test_a_landed_reviewed_tip_cannot_be_withdrawn(self):
        row = self.fix_row()
        self.git("merge", "--no-edit", "-q", "side")
        before = self.history_len(row["id"])
        _out, err = landreq.close(row["id"], "withdrawn",
                                  evidence="claim it is not landed")
        self.assertIn("IS on trunk", err)
        self.assertIn("The door for work on trunk follows the verdict", err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_an_unknown_land_state_fails_closed(self):  # noqa: VACUOUS_ASSERTION — the load-bearing assertions are POSITIVE (the refusal names "could not prove" and "FAIL-CLOSED"); the unchanged history length is the intentional absence that pins no close was written
        row = self.fix_row()
        before = self.history_len(row["id"])
        # THE DOUBLE GOES ON THE FUNCTION THIS DOOR ACTUALLY CALLS — see the
        # twin arm in test_landreq. A double on the retired `_landed` wrapper
        # is never consulted, so the real proof runs and the close SUCCEEDS
        # while the arm believes it is testing a refusal.
        with mock.patch.object(landreq, "_landing_proof",
                               return_value="unknown"):
            _out, err = landreq.close(row["id"], "withdrawn",
                                      evidence="evidence")
        self.assertIn("could not prove", err)
        self.assertIn("FAIL-CLOSED", err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_an_APPROVE_is_answered_by_the_landing_proof_not_its_polarity(self):
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME DOOR: an UNDECLARED row
        # still hits the polarity wall, so the absences below discriminate
        # rather than merely holding.
        # THE POSITIVE FORM: this fixture's tip is NOT on trunk, so an
        # APPROVE whose work is provably absent now reaches the landing proof
        # and CLOSES. A refusal assertion here would encode the replaced law.
        row = self.fix_row(polarity="approve")
        before = self.history_len(row["id"])
        _out, err = landreq.close(row["id"], "withdrawn", evidence="evidence")
        self.assertIsNone(err, "an APPROVE with provably absent work was "
                               "refused: %s" % err)
        self.assertEqual(self.history_len(row["id"]), before + 1)

        # THE INVARIANT, same door: with the SAME work on trunk the landing
        # proof refuses it, and not on polarity.
        landed = self.verdict_row("approve", lane="lane/close-approve-landed")
        self.git("merge", "--no-edit", "-q", "side")
        before = self.history_len(landed["id"])
        _o2, err2 = landreq.close(landed["id"], "withdrawn",
                                  evidence="evidence")
        self.assertIn("IS on trunk", err2 or "")
        self.assertNotIn("withdrawable", err2 or "")
        self.assertEqual(self.history_len(landed["id"]), before)

    def test_an_UNDECLARED_polarity_is_still_refused(self):
        row = self.fix_row(polarity=None)
        _out, err = landreq.close(row["id"], "withdrawn", evidence="evidence")
        self.assertIn("withdrawable", err)

    def test_withdrawn_needs_evidence(self):
        row = self.fix_row()
        _out, err = landreq.close(row["id"], "withdrawn")
        self.assertIn("evidence", err)

    def test_identical_retry_reconciles_and_a_conflict_refuses(self):
        row = self.fix_row()
        _out, err = landreq.close(row["id"], "withdrawn",
                                  evidence="resolution A")
        self.assertIsNone(err)
        before = self.history_len(row["id"])
        _out, err = landreq.close(row["id"], "withdrawn",
                                  evidence="resolution A")
        self.assertIsNone(err)
        self.assertEqual(self.history_len(row["id"]), before)
        _out, err = landreq.close(row["id"], "withdrawn",
                                  evidence="resolution B")
        self.assertIn("retired once", err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_a_closed_row_accepts_no_second_terminal_either_way(self):
        row = self.fix_row()
        _out, err = landreq.close(row["id"], "withdrawn",
                                  evidence="withdrawn first")
        self.assertIsNone(err)
        fixed = self.commit("a later superseding tip", path="h")
        _out, err = landreq.close(row["id"], "superseded",
                                  evidence="superseded second", tip=fixed)
        self.assertIn("retired once", err)


class UnreadableTipAtTheDoorsTest(CloseBase):
    """AN OBJECT THIS CLONE CANNOT RESOLVE NEVER RETIRES A ROW AS ABSENT.

    `withdrawn` records that a row's work is NOT on trunk and carries nothing
    else, so it may act only on an `absent` measured against an object this
    repository holds. A ladder that publishes `absent` for a pruned tip
    whenever the remote and the reflog are silent about it hands this door a
    row to retire on nothing. `subsumed` is the door that SHOULD
    still adjudicate a vanished original, because its cross-family
    confirmation proven on trunk bounds what the missing object cannot say,
    and it now asks `_vanished_absence` by name.

    EVERY ARM CONFIGURES A REACHABLE ORIGIN, the one condition under which the
    old ladder converted a pruned tip to ABSENT. The existing pruned-tip arms
    run without a remote, so the vanished pass could not look and they stayed
    green over the defect; each arm here asserts the conversion's input before
    it asserts anything about a door."""

    EVIDENCE = "the verdict is accepted; the artifact should not exist"

    def _origin(self):
        """A reachable origin carrying current trunk, and its tracking ref."""
        bare = os.path.join(self.tmp, "origin.git")
        if not os.path.isdir(bare):
            subprocess.run(["git", "init", "--bare", "-q", bare], check=True)
            self.git("remote", "add", "origin", bare)
        self.git("push", "-q", "-f", "origin", self.main)
        self.git("fetch", "-q", "origin")

    def _subsumed(self, target, confirmation):
        with self.family_evidence():
            return landreq.close(
                target["id"], "subsumed", evidence=confirmation["id"][:12],
                trunk=self.main, dry_run=True)

    def test_a_pruned_tip_never_withdraws_and_a_readable_absent_one_does(self):
        row, tip = self.pruned_verdict_row(polarity="fix",
                                           lane="lane/pruned-withdraw")
        self._origin()
        self.assertEqual(
            landreq._vanished_proof(self.gitdir(), tip), "absent",
            "precondition: the old ladder's ABSENT input, or this arm cannot "
            "tell the cure from the defect")
        # THE POSITIVE CONTROL, same repository, same door, same trunk: a
        # READABLE tip provably off trunk still withdraws. A cure that
        # answered unknown for everything would disable this door and pass
        # the refusal below unchanged.
        control = self.verdict_row(polarity="fix", lane="lane/readable-absent")
        plan, why = landreq.close(control["id"], "withdrawn",
                                  evidence=self.EVIDENCE, dry_run=True)
        self.assertIsNone(why)
        self.assertEqual(plan["proof_mode"], "absent")
        # THE CLAIM: the rehearsal and the write both refuse, and nothing lands.
        before = self.history_len(row["id"])
        plan, why = landreq.close(row["id"], "withdrawn",
                                  evidence=self.EVIDENCE, dry_run=True)
        self.assertIsNone(plan)
        self.assertIn("could not prove", why or "")
        self.assertIn("FAIL-CLOSED", why or "")
        out, why = landreq.close(row["id"], "withdrawn",
                                 evidence=self.EVIDENCE)
        self.assertIsNone(out)
        self.assertIn("could not prove", why or "")
        self.assertEqual(self.history_len(row["id"]), before)

    def test_a_vanished_original_is_still_subsumed_through_its_own_door(self):  # noqa: VACUOUS_ASSERTION — both absences have unconditional positives on the SAME door: the subsumed refusal follows this arm's own subsumed admission of the identical row, and the withdrawn refusal's positive is the sibling arm's readable-absent withdrawal in the same repository shape
        target, confirmation, _tip = self.subsumption_case()
        self.prune(self.side, "refs/heads/side")
        self._origin()
        self.assertEqual(landreq._vanished_proof(self.gitdir(), self.side),
                         "absent", "precondition: every source is silent")
        # the ladder does not call it absent...
        trunk = self.git("rev-parse", self.main)
        self.assertEqual(
            landreq._landing_proof(self.gitdir(), self.side, trunk), "unknown")
        # ...and the door that carries a confirmation still admits it: the
        # unconditional positive on this door's rehearsal, asserted first.
        dry, why = self._subsumed(target, confirmation)
        self.assertIsNone(why)
        self.assertEqual(dry["reason"], "subsumed")
        self.assertEqual(dry["original_proof_mode"], "absent")
        # while the door that carries nothing refuses the same object
        before = self.history_len(target["id"])
        plan, why = landreq.close(target["id"], "withdrawn",
                                  evidence=self.EVIDENCE, dry_run=True)
        self.assertIsNone(plan)
        self.assertIn("could not prove", why or "")
        self.assertEqual(self.history_len(target["id"]), before)
        # AND THE CONVERSION IS BOUNDED, NOT ASSUMED: with a remote the
        # vanished pass cannot ask, the same door on the same row refuses.
        self.git("remote", "set-url", "origin",
                 os.path.join(self.tmp, "nowhere.git"))
        dry, why = self._subsumed(target, confirmation)
        self.assertIsNone(dry)
        self.assertIn("could not prove the original reviewed tip absent",
                      why or "")


class WithdrawalIsTheThirdAnswerToAFixTest(CloseBase):
    """The terminal for *you are right, this should not exist* (task/2619).

    THE DEFECT WAS DISCOVERY, AND THAT WAS MEASURED BEFORE ANY DOOR WAS
    DESIGNED. On a scratch ledger the withdrawn close ALREADY admitted a
    FIX-verdicted row run by its author, ALREADY read terminal / owed_by
    nobody / stalled False, and the row ALREADY left
    `obligation.unanswered_fixes` — the population owed-bot re-delivers hourly
    — in the same pass. Nothing was broken in the ledger. What was broken is
    that no instrument the author met ever said the word: `dispatch cancel`
    refused with a sentence that named no alternative, and the owed remedy
    text offered `cure` and `--supersedes` and stopped, so the only move the
    tooling made possible was re-dispatching the artifact a reviewer had just
    correctly said should not exist.

    So these arms assert the NAMING at the two doors an author actually meets,
    and pin the ledger behaviour they were already owed underneath it.
    """

    def fix_row(self, lane="lane/withdraw-third-answer", polarity="fix"):
        row, why, sent = dispatches.send(
            "seat-b", lane, "review " + lane, self.side, repo=self.repo,
            key="key-" + lane, sign=False, new_work=True)
        self.assertIsNone(why)
        self.assertTrue(sent)
        _out, err = self.mark_verdict(
            row["id"], self.side, "findings: this should not exist",
            polarity=polarity)
        self.assertIsNone(err, err)
        return row

    def owed_ids(self):
        """The rows `helm owed` bills, read through the production function."""
        items, _forks, unavailable = obligation.unanswered_fixes()
        self.assertIsNone(unavailable, unavailable)
        return {i["row"]: i for i in items}

    def test_the_cancel_refusal_NAMES_the_withdrawn_door(self):
        """The first instrument an author meets after accepting a FIX.

        THE REFUSAL ITSELF IS CORRECT AND STAYS — a reviewed dispatch is not
        cancelled, and the effect assertion below pins that no cancel event is
        written. What is asserted here is the half that was missing: the exact
        command that IS the door, with the row id in it, so the author is not
        left with `--supersedes` as the only reachable move."""
        row = self.fix_row(lane="lane/cancel-names-the-door")
        before = self.history_len(row["id"])
        out, err = dispatches.mark_cancel(
            row["id"], "the artifact should not exist")
        self.assertIsNone(out)
        self.assertIn("a reviewed dispatch is not cancelled", err)
        self.assertIn("--reason withdrawn", err)
        self.assertIn("helm lr close %s" % row["id"][:12], err)
        self.assertIn("--evidence", err)
        # THE EFFECT, never the absence of a complaint: the row is untouched
        # and no cancel event was appended.
        self.assertEqual(self.history_len(row["id"]), before,
                         "the refusal still wrote to the ledger")
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"],
                         "verdict")

    def test_the_owed_remedy_NAMES_the_third_answer(self):
        """The sentence owed-bot re-delivers hourly, read from its producer.

        `owedpush` carries this string VERBATIM — its own arms assert that —
        so a third branch missing here is a third branch missing from every
        DM. The two existing branches are asserted beside it: the cure and the
        `--supersedes` re-dispatch are still named, because a remedy text that
        traded one omission for another would pass a bare `withdrawn` check."""
        row = self.fix_row(lane="lane/owed-names-the-door")
        item = self.owed_ids().get(row["id"])
        self.assertIsNotNone(item, "the FIX row is not billed at all, so the "
                                   "text below is not the one authors read")
        what = item["what"]
        self.assertIn("--supersedes %s" % row["id"][:12], what)
        self.assertIn("curing alone creates no review", what)
        self.assertIn("--reason withdrawn", what)
        self.assertIn("helm lr close %s" % row["id"][:12], what)

    def test_the_author_closes_it_and_owed_stops_counting_it(self):
        """The whole loop, through the accessors production uses.

        THE POSITIVE CONTROL IS THE SAME ROW ONE STATEMENT EARLIER: it IS
        billed before the close, so its absence afterwards is the close doing
        work rather than a burn-down that never saw it."""
        row = self.fix_row(lane="lane/author-withdraws")
        author = row["sender"]
        self.assertIn(row["id"], self.owed_ids(),
                      "the row is not owed even BEFORE the close, so the "
                      "exclusion below proves nothing")
        os.environ["HELM_CHAT_NAME"] = author
        out, err = landreq.close(
            row["id"], "withdrawn",
            evidence="I accept the finding in full; the artifact should not "
                     "exist. Deleted it; refutation recorded on its task.")
        self.assertIsNone(err, err)
        self.assertEqual(out["close_reason"], "withdrawn")
        self.assertTrue(out["terminal"], out)
        self.assertFalse(out["stalled"], out)
        self.assertEqual(out["owed_by"], "nobody")
        self.assertNotIn(row["id"], self.owed_ids(),
                         "a withdrawn-closed row is still billed as an "
                         "unanswered FIX — owed-bot goes on nagging it")
        stalled, serr = landreq.stalls()
        self.assertIsNone(serr, serr)
        self.assertNotIn(row["id"], {s["id"] for s in stalled or []})

    def test_the_page_says_the_AUTHOR_withdrew_it(self):
        """The REVIEWER's reading, which nothing answered.

        A FIX row that ends with no cure and no successor reads, from outside,
        as a verdict ignored. The ledger records the closing hand
        (`withdrawing_seat`) and the projection dropped it, so no surface
        could say whose withdrawal it was. Both halves are asserted: the
        projected row now CARRIES the seat, and the page names it."""
        row = self.fix_row(lane="lane/reviewer-sees-agreement")
        os.environ["HELM_CHAT_NAME"] = row["sender"]
        _out, err = landreq.close(
            row["id"], "withdrawn",
            evidence="agreed in full; the artifact is deleted")
        self.assertIsNone(err, err)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr.get("withdrawing_seat"), row["sender"],
                         "the projection drops the recorded hand, so no "
                         "renderer can name it")
        page = landreq._render_show(lr)
        self.assertIn("CLOSED (WITHDRAWN)", page)
        self.assertIn("agreed in full", page)
        self.assertIn("by the AUTHOR %s" % row["sender"], page)
        self.assertIn("ACCEPTED, not ignored", page)

    def test_a_stranger_may_close_it_and_the_page_DISCLOSES_that(self):
        """THE MEASURED TRUTH, NOT THE EXPECTED ONE.

        `_close_ladder_withdrawn` binds NO author, deliberately and in
        writing: the integrator carrying out a verdict is a legitimate closer,
        and the rows this reason exists for include older verdicts recorded
        before author binding was stable (its own comment names the specimen).
        Adding an author gate here would refuse exactly that population, so
        this lane did not add one — it disclosed the consequence instead.

        This arm is therefore the DISCRIMINATOR for the one above: the same
        door, the same accepted close, and a page that says something
        different. A renderer that printed the agreement sentence
        unconditionally would pass that arm and fail this one."""
        row = self.fix_row(lane="lane/stranger-withdraws")
        os.environ["HELM_CHAT_NAME"] = "a-passing-integrator"
        out, err = landreq.close(
            row["id"], "withdrawn",
            evidence="retired on the author's behalf")
        self.assertIsNone(err, "the door refused an unrelated seat — that is "
                               "a guard this lane did not install: %s" % err)
        self.assertEqual(out["close_reason"], "withdrawn")
        page = landreq._render_show(landreq.get(row["id"])[0])
        self.assertIn("is NOT the author", page)
        self.assertNotIn("ACCEPTED, not ignored", page,
                         "a stranger's withdrawal claims the author's "
                         "agreement, which nothing recorded")

    def test_the_missing_evidence_refusal_names_BOTH_answers(self):  # noqa: VACUOUS_ASSERTION — the load-bearing assertions are POSITIVE (three exact substrings of the refusal), and the unchanged history rides an UNCONDITIONAL control on the same observable and the same door: a twin row closed WITH evidence appends first, asserted with assertGreater, so a ledger that never grows fails the arm before the absence is read
        """The door's own sentence admitted one of the two whys it takes.

        Git proves the reviewed change absent and can say nothing about why.
        Naming only *the resolution was carried out* tells an author who
        accepted the verdict that they are at the wrong door."""
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE, first: the
        # SAME door on a twin row WITH evidence appends, so the unchanged
        # history below is this refusal holding rather than a fixture whose
        # ledger never grows.
        control = self.fix_row(lane="lane/evidence-control")
        grew_from = self.history_len(control["id"])
        _out, err = landreq.close(control["id"], "withdrawn",
                                  evidence="agreed; the artifact is deleted")
        self.assertIsNone(err, err)
        self.assertGreater(self.history_len(control["id"]), grew_from,
                           "the ledger does not grow on this door at all, so "
                           "the refusal below proves nothing")

        row = self.fix_row(lane="lane/evidence-names-both")
        before = self.history_len(row["id"])
        _out, err = landreq.close(row["id"], "withdrawn")
        self.assertIn("resolution was carried out", err)
        self.assertIn("ACCEPTED", err)
        self.assertIn("should not exist", err)
        self.assertEqual(self.history_len(row["id"]), before,
                         "the refusal still wrote to the ledger")


class CloseOutOfScopeTest(CloseBase):
    def test_a_row_whose_work_is_ON_TRUNK_is_refused(self):
        """#187 — THIS DOOR'S GUARDS ANSWER AN ADJACENT QUESTION. They test
        whether the obligation is MOOT and whether the work is still LIVE, and
        a ghost BUILD row passes both honestly: its review is never coming, so
        the obligation IS moot. Nothing asked whether the work SHIPPED, so the
        door opened by its own terms and recorded a falsehood by ours —
        out-of-scope on a change the fleet is running.

        Measured by dry-running five reasons against a ghost row:
        four refused correctly and this one WOULD CLOSE. Fourth guard that
        night asking an adjacent question, and the only one that WRITES."""
        row = self.dispatch(lane="lane/oos-shipped")
        with mock.patch.object(dispatches, "discharging_row",
                               return_value=("chain", "d" * 32, None)):
            rc, _out, err = run(["close", row["id"][:12], "--reason",
                                 "out-of-scope", "--evidence", "moot"])
        self.assertEqual(rc, 1)
        self.assertIn("ON TRUNK", err)
        self.assertIn("dddddddddddd", err, "the refusal must NAME the discharger")
        snap = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(snap["status"], "open", "nothing may be written")

    def test_the_refusal_NAMES_the_door_when_the_build_has_it(self):
        """THE COMPOSITION IS THE UNIT. This guard refuses the wrong terminal;
        the `discharged` ladder supplies the right one — and a refusal that
        names no door leaves the operator exactly as stuck as the bug did.
        Read live rather than hard-coded: that door lands in its own lane, so
        naming a reason this build lacks would trade a dead end for a wrong
        turn."""
        row = self.dispatch(lane="lane/oos-route")
        with mock.patch.object(dispatches, "discharging_row",
                               return_value=("chain", "d" * 32, None)), \
             mock.patch.object(landreq, "CLOSE_CLI_REASONS",
                               landreq.CLOSE_CLI_REASONS + ("discharged",)):
            _rc, _out, err = run(["close", row["id"][:12], "--reason",
                                  "out-of-scope", "--evidence", "moot"])
        self.assertIn("--reason discharged", err)

    def test_the_refusal_STAYS_GENERIC_when_the_door_is_absent(self):
        """The other side of that read: without the door landed, the refusal
        must not send anyone at a reason the CLI would reject."""
        row = self.dispatch(lane="lane/oos-nodoor")
        # PIN the absence rather than reading the ambient tuple: the door
        # lands in its own lane, and a test that assumes today's reason list
        # is a test with an expiry date on it.
        without = tuple(r for r in landreq.CLOSE_CLI_REASONS
                        if r != "discharged")
        with mock.patch.object(dispatches, "discharging_row",
                               return_value=("chain", "d" * 32, None)), \
             mock.patch.object(landreq, "CLOSE_CLI_REASONS", without):
            _rc, _out, err = run(["close", row["id"][:12], "--reason",
                                  "out-of-scope", "--evidence", "moot"])
        self.assertIn("proves the land", err)
        self.assertNotIn("--reason discharged", err)

    def test_a_row_with_no_discharger_still_closes(self):
        """THE CONTROL, and the honest limit of the fix. A row whose relation
        was never recorded cannot be PROVEN shipped, so this door still admits
        it — the refusal covers what is provable, never everything true."""
        row = self.dispatch(lane="lane/oos-unprovable")
        with mock.patch.object(dispatches, "discharging_row",
                               return_value=(None, None, "nothing records it")):
            rc, _out, err = run(["close", row["id"][:12], "--reason",
                                 "out-of-scope", "--evidence", "moot"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"], "cancelled")

    def test_an_unreadable_trunk_FAILS_CLOSED(self):
        """Same posture as the liveness block below it: could-not-look is
        never looked-and-found-nothing. An unreadable trunk must refuse rather
        than assume the work never landed."""
        row = self.dispatch(lane="lane/oos-blind")
        with mock.patch.object(landreq, "_trio_trunk",
                               return_value=(None, None, None, "trunk unreadable")):
            rc, _out, err = run(["close", row["id"][:12], "--reason",
                                 "out-of-scope", "--evidence", "moot"])
        self.assertEqual(rc, 1)
        self.assertIn("FAIL-CLOSED", err)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"], "open")

    def test_open_row_closes_through_a_cancel_event(self):
        row = self.dispatch(lane="lane/oos-moot")
        rc, out, err = run(["close", row["id"][:12], "--reason",
                            "out-of-scope", "--evidence",
                            "deliverable was the diagnosis, delivered"])
        self.assertEqual((rc, err), (0, ""))
        snap = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(snap["status"], "cancelled")
        self.assertEqual(snap["cancel_reason"],
                         "deliverable was the diagnosis, delivered")
        kinds = [e.get("event")
                 for e in dispatches.history(row["id"])]
        self.assertIn("cancel", kinds)        # NO new event kind
        self.assertNotIn("close", kinds)
        # idempotent retry through cancel's own boundary
        before = self.history_len(row["id"])
        rc, _out, err = run(["close", row["id"], "--reason", "out-of-scope",
                             "--evidence",
                             "deliverable was the diagnosis, delivered"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_every_verdict_polarity_is_refused(self):
        """D3 pin, four assertions: attestation never terminates reviewed
        work."""
        for polarity, needle in (("fix", "declared resolution debt"),
                                 ("supersede", "declared resolution debt"),
                                 ("approve", "attestation never terminates"),
                                 (None, "attestation never terminates")):
            row = self.verdict_row(polarity,
                                   lane="lane/oos-%s" % (polarity or "und"))
            before = self.history_len(row["id"])
            _out, err = landreq.close(row["id"], "out-of-scope",
                                      evidence="moot")
            self.assertIn(needle, err, polarity)
            self.assertEqual(self.history_len(row["id"]), before, polarity)

    def test_a_live_lane_family_ref_at_the_tip_refuses(self):
        row = self.dispatch(lane="lane/oos-live-work")
        self.git("branch", "lane/oos-live-work", self.side)
        before = self.history_len(row["id"])
        _out, err = landreq.close(row["id"], "out-of-scope",
                                  evidence="moot")
        self.assertIn("live at refs/heads/lane/oos-live-work", err)
        self.assertIn("rebind", err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_a_family_ref_that_moved_past_the_tip_still_refuses(self):
        row = self.dispatch(lane="lane/oos-advanced")
        self.git("checkout", "-q", "-b", "lane/oos-advanced-r2", self.side)
        self.commit("the lane moved on", path="g")
        self.git("checkout", "-q", self.main)
        _out, err = landreq.close(row["id"], "out-of-scope",
                                  evidence="moot")
        self.assertIn("live at refs/heads/lane/oos-advanced-r2", err)

    def test_a_dirty_family_worktree_refuses(self):
        row = self.dispatch(lane="lane/oos-dirty-family", ref=self.b)
        wt = os.path.join(self.tmp, "oos-dirty-family-wt")
        self.git("worktree", "add", "--detach", "-q", wt, self.b)
        with open(os.path.join(wt, "uncommitted.txt"), "w",
                  encoding="utf-8") as f:
            f.write("live work\n")
        _out, err = landreq.close(row["id"], "out-of-scope",
                                  evidence="moot")
        self.assertIn("live at %s" % wt, err)

    def test_unreadable_liveness_is_unknown_and_refuses(self):
        row = self.dispatch(lane="lane/oos-unknown")
        with mock.patch.object(landreq, "_lane_family_refs",
                               return_value=(None, "ref table gone")):
            _out, err = landreq.close(row["id"], "out-of-scope",
                                      evidence="moot")
        self.assertIn("FAIL-CLOSED", err)

    def test_a_landed_tip_on_an_open_row_is_tolerated_and_annotated(self):
        row = self.dispatch(lane="lane/oos-already-landed", ref=self.b)
        out, err = landreq.close(row["id"], "out-of-scope",
                                 evidence="review became moot")
        self.assertIsNone(err)
        self.assertEqual(out["landed_check"], "true")
        self.assertEqual(out["status"], "cancelled")

    def test_evidence_is_required(self):
        row = self.dispatch(lane="lane/oos-no-evidence")
        _out, err = landreq.close(row["id"], "out-of-scope")
        self.assertIn("needs evidence", err)


class CloseTranslatedSupersededTest(CloseBase):
    """#101 — the translated-object-superseded door. A pre-seat-stamp row
    (author binding ABSENT, exactly as 9f667bd9 reads in the live ledger)
    whose reviewed tip was DESTROYED by a rewrite, translated by the sidecar
    to a LIVE object that is PRESERVED under an archive/rescue ref but NOT on
    trunk BY DESIGN. Today the author rung refuses it before any git fact is
    consulted — the one remaining unmeasurable. The door substitutes
    attestation (destroyed + translated + preserved + a landed cross-family
    approved superseder) for the binding the stamp era could not produce."""

    def handoff(self, lane, ref, supersedes=None):
        row, why, sent = dispatches.send(
            "codex-3", lane, "review " + lane, ref, repo=self.repo,
            key="key-" + lane, sign=False,
            new_work=supersedes is None, supersedes=supersedes)
        self.assertIsNone(why)
        self.assertTrue(sent)
        return row

    def resolved(self, polarity="fix"):
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
        return first, second, fixed

    def unstamped_row(self, lane="lane/pre-stamp"):
        """A verdict row whose events carry NO author/sender/chain — the
        pre-seat-stamp shape, minted by stripping the fields the stamp era
        did not record (mirrors legacy_chainless, plus author/sender)."""
        row = self.verdict_row(polarity=None, lane=lane)
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for event in events:
                if event.get("id") == row["id"]:
                    for field in ("author", "sender", "chain_root",
                                  "supersedes"):
                        event.pop(field, None)
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
        return row

    def translated_pruned_row(self, lane="lane/pre-stamp", preserve=True):
        """The 9f667bd9 shape: unstamped row, reviewed tip pruned, sidecar
        translating to a live object kept under an archive ref (or not)."""
        row = self.unstamped_row(lane=lane)
        tip = self.side
        # translated object: a real commit that never reaches trunk
        self.git("checkout", "-q", "-b", "rewritten", self.a)
        new = self.commit("rewritten cargo", path="h")
        self.git("checkout", "-q", self.main)
        if preserve:
            self.git("tag", "archive/%s-rescue" % lane.rsplit("/", 1)[-1],
                     new)
        else:
            self.git("update-ref", "-d", "refs/heads/rewritten")
        self.prune(tip, "refs/heads/side")
        self.sidecar(tip, new)
        return row, tip, new

    def approved_superseder(self, row, recipient="codex-3", merge=True):
        """A LANDED superseding tip with a later cross-family APPROVE."""
        self.git("checkout", "-q", "-b", "r2", self.main)
        fixed = self.commit("superseding round", path="g2")
        self.git("checkout", "-q", self.main)
        second = self.handoff("lane/pre-stamp-r2", fixed,
                              supersedes=row["id"])
        self.mark_verdict(second["id"], fixed, "re-probed clean",
                                polarity="approve")
        if merge:
            self.git("merge", "--no-edit", "-q", "r2")
        return second, fixed

    def test_the_9f667bd9_shape_closes_superseded(self):  # noqa: VACUOUS_ASSERTION — the close IS the claim; its control is the paired refusal arms (unpreserved/unapproved) and the mutation arm (door deleted) reddening exactly this
        row, _tip, new = self.translated_pruned_row()
        _second, fixed = self.approved_superseder(row)
        out, err = landreq.close(row["id"][:12], "superseded",
                                 evidence="r2 landed, archive-preserved",
                                 tip=fixed)
        self.assertIsNone(err)
        snap = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(snap["close_reason"], "superseded")
        self.assertEqual(snap["superseding_tip"], fixed)
        # the proof mode lives on the durable close event; the snapshot view
        # projects the close without it (same as every other reason)
        event = [e for e in dispatches.history(row["id"])
                 if e.get("event") == "close"][0]
        self.assertEqual(event["close_proof_mode"], "translated-superseded")
        self.assertEqual(event["translated_tip"], new)

    def test_an_unrelated_approved_land_never_closes_a_translated_row(self):  # noqa: VACUOUS_ASSERTION — the refusal IS the claim; its control is the chained arm (next test) positively closing on the identical fixture plus one link
        """The r1-review FIX, pinned: the sidecar binds
        reviewed -> translated, NOT translated -> superseder. An approved
        land that neither CARRIES the translated change (ancestry /
        patch-equivalence) nor DECLARES the resolution (a --supersedes
        chain back to this row) is an unrelated work item and must not
        launder this row's close."""
        row, _tip, _new = self.translated_pruned_row()
        self.git("checkout", "-q", "-b", "u", self.main)
        fixed = self.commit("unrelated work", path="zz")
        self.git("checkout", "-q", self.main)
        other = self.handoff("lane/someone-elses-work", fixed)  # NO chain
        self.mark_verdict(other["id"], fixed,
                                "approved, and nothing to do with the row",
                                polarity="approve")
        self.git("merge", "--no-edit", "-q", "u")
        before = self.history_len(row["id"])
        out, err = landreq.close(row["id"], "superseded",
                                 evidence="an unrelated land claims the row",
                                 tip=fixed)
        self.assertEqual(out, None)
        self.assertIn("neither carries the translated change", err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_a_predating_approved_land_never_closes_a_translated_row(self):  # noqa: VACUOUS_ASSERTION — the refusal IS the claim; its control is the chained arm (next test) positively closing
        """The r2-review FIX, pinned: the content relation is
        directional. 'Carries' means the superseder CONTAINS the translated
        cargo (translated is an ancestor of, or patch-equivalent to, the
        superseder) — never that the cargo contains the superseder. An
        approved land of the translated object's own ANCESTOR predates the
        work it claims to resolve."""
        row, _tip, _new = self.translated_pruned_row()
        # the translated object's base — on trunk, approved, predates cargo
        other = self.handoff("lane/old-work", self.a)
        self.mark_verdict(other["id"], self.a,
                                "approved work from before the rewrite",
                                polarity="approve")
        before = self.history_len(row["id"])
        out, err = landreq.close(row["id"], "superseded",
                                 evidence="a predating land claims the row",
                                 tip=self.a)
        self.assertEqual(out, None)
        self.assertIn("neither carries the translated change", err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_a_chained_re_derivation_closes_the_translated_row(self):  # noqa: VACUOUS_ASSERTION — the close IS the claim; its control is the unchained arm (previous test) refusing on the identical fixture minus the link
        """The attestation-for-binding path the door exists for: the land
        does NOT carry the translated change (the adopter revised while
        re-deriving, so no patch-id can match), but the superseding review
        DECLARES the resolution by chaining back to the original row."""
        row, _tip, _new = self.translated_pruned_row()
        self.git("checkout", "-q", "-b", "r2", self.main)
        fixed = self.commit("re-derived content, not patch-equivalent",
                            path="g3")
        self.git("checkout", "-q", self.main)
        second = self.handoff("lane/pre-stamp-r2", fixed,
                              supersedes=row["id"])
        self.mark_verdict(second["id"], fixed,
                                "cross-family approve of the re-derivation",
                                polarity="approve")
        self.git("merge", "--no-edit", "-q", "r2")
        out, err = landreq.close(row["id"], "superseded",
                                 evidence="chained re-derivation landed",
                                 tip=fixed)
        self.assertIsNone(err)
        event = [e for e in dispatches.history(row["id"])
                 if e.get("event") == "close"][0]
        self.assertEqual(event["close_proof_mode"], "translated-superseded")

    def test_a_bound_author_row_still_walks_the_old_ladder(self):  # noqa: VACUOUS_ASSERTION — the ABSENCE of close_proof_mode is the claim; its control is the next arm, same shape with an archived object, positively asserting the mode IS recorded
        """The substitution is ONLY for the binding the stamp era could not
        produce — a row WITH an author and a CONTAINED superseder never
        sees the new door (containment is its proof)."""
        first, _second, fixed = self.resolved()
        out, err = landreq.close(first["id"][:12], "superseded",
                                 evidence="r2 approved", tip=fixed)
        self.assertIsNone(err)
        event = [e for e in dispatches.history(first["id"])
                 if e.get("event") == "close"][0]
        self.assertNotIn("close_proof_mode", event)

    def test_a_bound_author_archived_row_closes_through_the_door(self):  # noqa: VACUOUS_ASSERTION — the recorded proof mode IS the claim; mutating the door's second entry turns exactly this arm RED
        """The 18d28565 full shape: author BOUND (opus-integrator), verdict
        FIX, original archived, content landed REVISED. Containment refuses
        at the main rung, and the SAME door carries it at the second entry
        — one ladder, two doors in."""
        row = self.verdict_row(polarity="fix", lane="lane/bound-archived")
        tip = self.side
        self.git("tag", "archive/bound-archived-original", tip)
        self.git("checkout", "-q", self.main)
        with open(os.path.join(self.repo, "g"), "w",
                  encoding="utf-8") as f:
            f.write("side\nrevised\n")
        self.git("add", "g")
        self.git("commit", "-q", "-m", "re-derived, revised")
        fixed = self.git("rev-parse", "HEAD")
        second = self.handoff("lane/bound-archived-r2", fixed,
                              supersedes=row["id"])
        self.mark_verdict(second["id"], fixed, "stitch",
                                polarity="approve")
        out, err = landreq.close(row["id"][:12], "superseded",
                                 evidence="landed revised", tip=fixed)
        self.assertIsNone(err)
        event = [e for e in dispatches.history(row["id"])
                 if e.get("event") == "close"][0]
        self.assertEqual(event["close_proof_mode"], "attested-superseded")

    def test_a_bound_author_row_whose_tip_vanished_still_reaches_the_door(self):
        """THE ROUTING IS ASKED FOR BY NAME, NOT INHERITED FROM A COLLAPSE.
        An author-bound row whose reviewed object gc destroyed reaches the
        destroyed-and-translated arm through the superseded ladder's
        containment rung. The landing ladder answers `unknown` for a vanished
        object, so that rung asks `_vanished_absence` before it routes.
        Without a reachable origin the vanished pass cannot look and the rung
        refuses."""
        row = self.verdict_row(polarity="fix", lane="lane/bound-pruned")
        tip = self.side
        self.git("checkout", "-q", "-b", "rewritten", self.a)
        new = self.commit("rewritten cargo", path="h")
        self.git("checkout", "-q", self.main)
        self.git("tag", "archive/bound-pruned-rescue", new)
        self.prune(tip, "refs/heads/side")
        self.sidecar(tip, new)
        _second, fixed = self.approved_superseder(row)
        _out, err = landreq.close(row["id"], "superseded", evidence="landed",
                                  tip=fixed, dry_run=True)
        self.assertIn("could not prove the contrary tip is in the superseding "
                      "tip", err or "")
        bare = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "--bare", "-q", bare], check=True)
        self.git("remote", "add", "origin", bare)
        self.git("push", "-q", "origin", self.main)
        self.git("fetch", "-q", "origin")
        self.assertEqual(landreq._vanished_proof(self.gitdir(), tip), "absent",
                         "precondition: every source is silent about it")
        out, err = landreq.close(row["id"][:12], "superseded",
                                 evidence="r2 landed, archive-preserved",
                                 tip=fixed)
        self.assertIsNone(err)
        event = [e for e in dispatches.history(row["id"])
                 if e.get("event") == "close"][0]
        self.assertEqual(event["close_proof_mode"], "translated-superseded")
        self.assertEqual(event["translated_tip"], new)

    def test_a_live_reviewed_object_never_takes_the_door(self):  # noqa: VACUOUS_ASSERTION — the refusal IS the claim; its control is the door arms positively closing on the same fixture minus one rung
        row = self.unstamped_row(lane="lane/pre-stamp-live")
        _second, fixed = self.approved_superseder(row)
        before = self.history_len(row["id"])
        _out, err = landreq.close(row["id"], "superseded",
                                  evidence="x", tip=fixed)
        self.assertIsNotNone(err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_an_unpreserved_translation_refuses(self):  # noqa: VACUOUS_ASSERTION — the refusal IS the claim; its control is the preserved arm (same fixture, archive tag added) positively closing
        """Translated but NOT preserved under any archive/rescue ref is not
        this door's row. The translation here is gc'd with its branch, so this
        arm is the DESTROYED-translation refusal; a translation that is still
        a live object refuses too, and no door is offered for it (measured in
        RefusalsNameMeasuredDoorsTest)."""
        row, _tip, _new = self.translated_pruned_row(preserve=False)
        _second, fixed = self.approved_superseder(row)
        before = self.history_len(row["id"])
        _out, err = landreq.close(row["id"], "superseded",
                                  evidence="x", tip=fixed)
        self.assertIsNotNone(err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_translated_object_on_trunk_redirects_to_landed(self):  # noqa: VACUOUS_ASSERTION — the redirect IS the claim; its control is the not-on-trunk arm positively closing superseded
        row, _tip, new = self.translated_pruned_row()
        self.git("merge", "--no-edit", "-q", "rewritten")
        _second, fixed = self.approved_superseder(row)
        before = self.history_len(row["id"])
        _out, err = landreq.close(row["id"], "superseded",
                                  evidence="x", tip=fixed)
        self.assertIn("--reason landed", err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_an_unlanded_superseder_refuses(self):  # noqa: VACUOUS_ASSERTION — the refusal IS the claim; its control is the landed arm positively closing on the same row
        row, _tip, _new = self.translated_pruned_row()
        _second, fixed = self.approved_superseder(row, merge=False)
        before = self.history_len(row["id"])
        _out, err = landreq.close(row["id"], "superseded",
                                  evidence="x", tip=fixed)
        self.assertIn("not reached", err)
        self.assertEqual(self.history_len(row["id"]), before)

    def archived_replaced_row(self, lane="lane/archived-orphan"):
        """The 18d28565 shape: an unstamped FIX row whose reviewed tip is
        ALIVE under an archive tag, while the landed tip carries the SAME
        patch re-derived (object containment false, patch identity true)."""
        row = self.unstamped_row(lane=lane)
        tip = self.side
        # the verdict is FIX-polarity (the orphan's original verdict)
        self.mark_verdict(row["id"], tip, "findings", polarity="fix")
        # the archive tag preserves the orphaned tip exactly
        self.git("tag", "archive/%s-original" % lane.rsplit("/", 1)[-1], tip)
        # the landed superseder: same CHANGE to the same file, fresh object
        # (patch-identical — the archived-superseded proof). The REVISED
        # shape (attested-superseded) has its own fixture below.
        self.git("checkout", "-q", self.main)
        with open(os.path.join(self.repo, "g"), "w",
                  encoding="utf-8") as f:
            f.write("side\n")
        self.git("add", "g")
        self.git("commit", "-q", "-m", "re-derived orphan content")
        fixed = self.git("rev-parse", "HEAD")
        second = self.handoff("lane/archived-orphan-r2", fixed,
                              supersedes=row["id"])
        self.mark_verdict(second["id"], fixed, "stitch approve",
                                polarity="approve")
        return row, tip, fixed

    def test_the_18d28565_shape_closes_superseded(self):  # noqa: VACUOUS_ASSERTION — the close IS the claim; its controls are the two refusal arms (unapproved land, unchained approval) on the same fixture
        """Archive-tag + replaced content: the reviewed object is ALIVE, so
        translation has nothing to say — but the landed tip is
        patch-equivalent to the archived original, and that is the proof a
        re-derived landing offers. The archive tag must be NAMED on the
        recorded close."""
        row, tip, fixed = self.archived_replaced_row()
        out, err = landreq.close(row["id"][:12], "superseded",
                                 evidence="stitch landed re-derived",
                                 tip=fixed)
        self.assertIsNone(err)
        snap = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(snap["close_reason"], "superseded")
        event = [e for e in dispatches.history(row["id"])
                 if e.get("event") == "close"][0]
        self.assertEqual(event["close_proof_mode"], "archived-superseded")
        self.assertIn("archive/", event["close_evidence"])

    def attested_replaced_row(self, lane="lane/attested-orphan"):
        """The TRUE 18d28565 shape: archived original + landed content
        REVISED while re-derived (no patch-id can match), so the archive
        tag plus the landed tip's own approved review is the attestation."""
        row = self.unstamped_row(lane=lane)
        tip = self.side
        self.mark_verdict(row["id"], tip, "findings", polarity="fix")
        self.git("tag", "archive/%s-original" % lane.rsplit("/", 1)[-1], tip)
        self.git("checkout", "-q", self.main)
        with open(os.path.join(self.repo, "g"), "w",
                  encoding="utf-8") as f:
            f.write("side\nrevised by the adopter\n")
        self.git("add", "g")
        self.git("commit", "-q", "-m", "re-derived orphan content, revised")
        fixed = self.git("rev-parse", "HEAD")
        second = self.handoff("lane/attested-orphan-r2", fixed,
                              supersedes=row["id"])
        self.mark_verdict(second["id"], fixed, "stitch approve",
                                polarity="approve")
        return row, tip, fixed

    def test_the_attested_shape_closes_superseded(self):  # noqa: VACUOUS_ASSERTION — the attested close IS the claim; its control is the unapproved-land arm refusing on the identical fixture
        """18d28565 exactly: patch-equivalence cannot hold (the adopter
        revised while re-deriving), so the close records
        attested-superseded — the archive tag names the preserved original
        and the landed tip's independent APPROVE attests the resolution."""
        row, tip, fixed = self.attested_replaced_row()
        out, err = landreq.close(row["id"][:12], "superseded",
                                 evidence="stitch landed, revised",
                                 tip=fixed)
        self.assertIsNone(err)
        event = [e for e in dispatches.history(row["id"])
                 if e.get("event") == "close"][0]
        self.assertEqual(event["close_proof_mode"], "attested-superseded")

    def test_an_unapproved_landed_tip_never_attests(self):  # noqa: VACUOUS_ASSERTION — the refusal IS the claim; its control is the attested arm positively closing once the approve exists
        """The attestation is the landed tip's OWN later APPROVE: a landed
        revised tip with NO approved review has only the archive tag, and
        a tag alone closes nothing."""
        row, tip, fixed = self.attested_replaced_row()
        # strip the superseder's approve, leaving only the tag + the land
        path = dispatches.ledger_path()
        events = [e for e in eventledger.events(path)
                  if not (e.get("event") == "verdict"
                          and e.get("reviewed_tip") == fixed
                          and e.get("id") != row["id"])]
        with open(path, "w", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
        before = self.history_len(row["id"])
        _out, err = landreq.close(row["id"], "superseded",
                                  evidence="x", tip=fixed)
        self.assertIsNotNone(err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_archive_tag_without_patch_equivalence_refuses(self):  # noqa: VACUOUS_ASSERTION — the refusal IS the claim; its control is the chained-approval arm positively closing on the same archive tag
        """An archive tag is a name, not a proof: if the landed tip's patch
        does NOT match the archived original's, the close refuses — the tag
        alone must never launder unrelated work into a supersession."""
        row, tip, _fixed = self.archived_replaced_row()
        # a DIFFERENT landed tip whose history does NOT contain the
        # re-derived land (built from BEFORE it), touching a different file
        self.git("checkout", "-q", "-b", "r3", self.c)
        other = self.commit("unrelated landing", path="unrelated")
        self.git("checkout", "-q", self.main)
        self.git("merge", "--no-edit", "-q", "r3")
        # approved, but NOT chained to the original — an attestation that
        # never claims to resolve THIS row must never close it
        second = self.handoff("lane/archived-orphan-r3", other)
        self.mark_verdict(second["id"], other, "approve",
                                polarity="approve")
        before = self.history_len(row["id"])
        _out, err = landreq.close(row["id"], "superseded",
                                  evidence="x", tip=other)
        self.assertIsNotNone(err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_the_original_row_can_never_attest_for_itself(self):  # noqa: VACUOUS_ASSERTION — the refusal IS the claim; its control is every door arm positively closing with an independent approve present
        """Structural independence: with no author binding, the superseding
        APPROVE must live on a DIFFERENT ledger row, later in append order —
        the original approving its own superseder is no attestation. Mint a
        same-row approve shape by pointing the superseding tip AT a tip the
        original row itself approved (impossible in practice; the row-id
        clause is what stands between the door and self-dealing)."""
        row, _tip, _new = self.translated_pruned_row()
        _second, fixed = self.approved_superseder(row)
        # remove the independent approve: the only remaining approve touching
        # `fixed` is one we delete, so no different-row candidate survives
        path = dispatches.ledger_path()
        events = [e for e in eventledger.events(path)
                  if not (e.get("event") == "verdict"
                          and e.get("reviewed_tip") == fixed
                          and e.get("id") != row["id"])]
        with open(path, "w", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
        before = self.history_len(row["id"])
        _out, err = landreq.close(row["id"], "superseded",
                                  evidence="x", tip=fixed)
        # With the independent approve gone the door must refuse; WHICH rung
        # fires first is an implementation detail (the translated content
        # rule now precedes the candidate scan), so assert the refusal CLASS
        # — the superseder is unbound and nothing closes — not the wording.
        self.assertIsNotNone(err)
        self.assertRegex(err, "different row|neither carries the translated")
        self.assertEqual(self.history_len(row["id"]), before)


class CloseStrandedTest(CloseBase):
    def test_full_ladder_positive_records_the_control_sha(self):
        row, tip = self.pruned_verdict_row()
        control = self.git("rev-parse", self.main)
        rc, out, err = run(["close", row["id"][:12], "--reason", "stranded",
                            "--evidence",
                            "tip pruned by trunk rewrite; no translation"])
        self.assertEqual((rc, err), (0, ""))
        snap = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(snap["close_reason"], "stranded")
        self.assertEqual(snap["control_sha"], control)
        self.assertEqual(snap["close_proof_mode"], "object-pruned")
        self.assertEqual(snap["closing_repo_id"], self.gitdir())
        self.assertEqual(snap["polarity"], "fix")

    def test_open_rows_are_redirected(self):
        row = self.dispatch(lane="lane/open-stranded")
        _out, err = landreq.close(row["id"], "stranded", evidence="pruned")
        self.assertIn("open row", err)
        self.assertIn("out-of-scope", err)

    def test_a_still_present_object_refuses_stranded(self):
        row = self.verdict_row("fix", lane="lane/still-here")
        before = self.history_len(row["id"])
        _out, err = landreq.close(row["id"], "stranded", evidence="pruned?")
        self.assertIn("still an object", err)
        self.assertIn("adjudicable", err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_positive_control_rung_is_the_mass_termination_guard(self):
        """A repo that cannot prove its own trunk object never proves an
        absence. (The prune fixture itself never mocks cat-file; this rung
        isolation keys our helper by sha.)"""
        row, tip = self.pruned_verdict_row(lane="lane/control-guard")
        control = self.git("rev-parse", self.main)
        real = landreq._object_exists

        def blind_control(gitdir, sha):
            if sha == control:
                return None
            return real(gitdir, sha)

        before = self.history_len(row["id"])
        with mock.patch.object(landreq, "_object_exists",
                               side_effect=blind_control):
            _out, err = landreq.close(row["id"], "stranded",
                                      evidence="pruned")
        self.assertIn("cannot prove its own trunk object", err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_an_unknown_object_state_fails_closed(self):
        row, tip = self.pruned_verdict_row(lane="lane/unknown-object")
        real = landreq._object_exists

        def unknown_tip(gitdir, sha):
            if sha == tip:
                return None
            return real(gitdir, sha)

        with mock.patch.object(landreq, "_object_exists",
                               side_effect=unknown_tip):
            _out, err = landreq.close(row["id"], "stranded",
                                      evidence="pruned")
        self.assertIn("FAIL-CLOSED on an unknown object state", err)

    def test_translation_to_a_live_object_refuses(self):
        row, tip = self.pruned_verdict_row(lane="lane/translated-live")
        self.sidecar(tip, self.b)
        _out, err = landreq.close(row["id"], "stranded", evidence="pruned")
        self.assertIn("translates to live object", err)
        self.assertIn("--reason landed", err)

    def test_an_unreadable_sidecar_refuses(self):
        row, tip = self.pruned_verdict_row(lane="lane/sidecar-gone")
        path = self.sidecar(tip, self.b)
        os.chmod(path, 0)
        try:
            _out, err = landreq.close(row["id"], "stranded",
                                      evidence="pruned")
        finally:
            os.chmod(path, 0o600)
        self.assertIn("sidecar is unreadable", err)

    def test_evidence_is_required(self):
        row, _tip = self.pruned_verdict_row(lane="lane/no-evidence")
        _out, err = landreq.close(row["id"], "stranded")
        self.assertIn("needs evidence", err)


class AdversarialFixturesTest(CloseBase):
    """Tonight's live shapes, as named cases."""

    def test_318b4e5f_shape_renamed_lane_stem_hit_refuses_abandon(self):
        """FIX row, tip pruned, the work landed under a RENAMED lane whose
        ref survives — the stem probe matches the renamed ref (containment,
        not equality) and the refusal NAMES it."""
        row, tip = self.pruned_verdict_row(lane="lane/probe-rescue-work")
        self.git("branch", "lane/probe-rescue-work-extended", self.main)
        before = self.history_len(row["id"])
        _out, err = landreq.close(row["id"], "stranded",
                                  evidence="looks pruned")
        self.assertIn("refs/heads/lane/probe-rescue-work-extended", err)
        self.assertIn("stranded refused", err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_b970911e_shape_live_unlanded_lane_branch_refuses(self):
        """Tip pruned, translation maps to the LIVE lane tip which is NOT on
        main — stranded refuses at the translation rung, and with the sidecar
        removed refuses again at the family rung; landed refuses too. The
        row stays on the frontier."""
        row, tip = self.pruned_verdict_row(
            polarity="approve", lane="lane/withdraw-contradicted-exit")
        self.git("checkout", "-q", "-b",
                 "lane/withdraw-contradicted-exit", self.main)
        with open(os.path.join(self.repo, "unlanded.txt"), "w",
                  encoding="utf-8") as f:
            f.write("never on main\n")
        self.git("add", "unlanded.txt")
        self.git("commit", "-q", "-m", "live unlanded work")
        lane_tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        path = self.sidecar(tip, lane_tip)
        _out, err = landreq.close(row["id"], "stranded", evidence="pruned")
        self.assertIn("translates to live object", err)
        os.remove(path)
        _out, err = landreq.close(row["id"], "stranded", evidence="pruned")
        self.assertIn("lane/withdraw-contradicted-exit", err)
        self.assertIn("stranded refused", err)
        self.sidecar(tip, lane_tip)
        _out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIn("recorded translation", err)
        snap = dispatches.snapshot()[0][row["id"]]
        self.assertIsNone(snap.get("close_reason"))   # still on the frontier

    def test_uncommitted_worktree_14_dirty_files_refuses(self):
        row, tip = self.pruned_verdict_row(lane="lane/wt-dirty-family")
        wt = os.path.join(self.tmp, "wt-dirty-family-x")
        self.git("worktree", "add", "--detach", "-q", wt, self.main)
        for i in range(14):
            with open(os.path.join(wt, "dirty-%d.txt" % i), "w",
                      encoding="utf-8") as f:
                f.write("uncommitted\n")
        before = self.history_len(row["id"])
        _out, err = landreq.close(row["id"], "stranded", evidence="pruned")
        self.assertIn("lane-family worktree exists at %s" % wt, err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_translation_provable_landing_terminates_as_landed_rewritten(self):
        """Approve row, tip pruned, sidecar maps to a sha ON trunk — landed
        succeeds through the translation (D2) and the SAME row refuses
        stranded."""
        row, tip = self.pruned_verdict_row(polarity="approve",
                                           lane="lane/rewritten-landed")
        self.sidecar(tip, self.b)
        _out, err = landreq.close(row["id"], "stranded", evidence="pruned")
        self.assertIn("translates to live object", err)
        out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(err)
        snap = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(snap["close_reason"], "landed")
        self.assertEqual(snap["close_proof_mode"], "translated-ancestor")
        self.assertEqual(snap["translated_tip"], self.b)

    def test_paraphrased_landing_message_stays_unknown_and_refuses(self):
        """No code path consults commit subjects: a paraphrase AND the
        strongest literal token form both leave the proof unknown and the
        close refused (heuristics never author — documented boundary; the
        a-fortiori literal-subject case is the pin)."""
        row, tip = self.pruned_verdict_row(polarity="approve",
                                           lane="lane/paraphrase-probe")
        self.commit("landed the paraphrase-probe lane work as agreed")
        self.commit("land: lane/paraphrase-probe")
        before = self.history_len(row["id"])
        _out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIn("could not prove whether", err)
        self.assertEqual(self.history_len(row["id"]), before)
        _out, err = landreq.close(row["id"], "landed", trunk=self.main, live=True)
        self.assertIn("could not prove whether", err)
        self.assertEqual(self.history_len(row["id"]), before)


class CloseSurfacesTest(CloseBase):
    """Projection + owner surfaces for closed rows: terminal, dwell frozen,
    timeline ends CLOSED_<REASON>, and no surface ever implies an approval
    the ledger does not carry (exact per-polarity positive strings, D10a)."""

    def test_closed_row_is_terminal_off_the_board_and_dwell_frozen(self):
        row = self.verdict_row("approve")
        self.git("merge", "--no-edit", "-q", "side")
        out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(err)
        self.assertTrue(out["terminal"])
        self.assertEqual(out["close_reason"], "landed")
        self.assertEqual(out["owed_by"], "nobody")
        self.assertFalse(out["stalled"])
        self.assertEqual(out["timeline"][-1]["state"], "CLOSED_LANDED")
        self.assertNotIn(row["id"], [r["id"] for r in landreq.loops()[0]])
        self.assertIn(row["id"], [r["id"] for r in landreq.loops(True)[0]])
        # D10d: the dwell is FROZEN — re-projected with the clock advanced,
        # the number does not grow
        frozen = out["dwell_s"]
        later = landreq.get(row["id"], time.time() + 10000)[0]
        self.assertEqual(later["dwell_s"], frozen)
        self.assertTrue(later["dwell_known"])

    def test_the_card_carries_the_close_fields(self):
        row = self.verdict_row("approve")
        self.git("merge", "--no-edit", "-q", "side")
        landreq.close(row["id"], "landed", live=True)
        cards = {c["id"]: c
                 for c in landreq.board_section()["loops"]}
        # terminal rows are off the default board; assert the card SHAPE from
        # the --all projection instead
        lr = landreq.get(row["id"])[0]
        card = landreq.card(lr)
        self.assertEqual(card["close_reason"], "landed")
        self.assertEqual(card["close_ts"], lr["close_ts"])
        self.assertIn("close_contradicted", card)
        self.assertFalse(card["close_contradicted"])
        self.assertNotIn(row["id"], cards)

    def test_monotonic_close_rows_never_reobserve_git(self):
        """The :2258 monotonicity port for close --reason landed: a closed
        row's projection consults NO live repository."""
        row = self.verdict_row("approve")
        self.git("merge", "--no-edit", "-q", "side")
        _out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(err)
        with mock.patch.object(landreq, "_git") as git, \
                mock.patch.object(landreq, "_git_observe") as observe:
            replayed = landreq.get(row["id"])[0]
        git.assert_not_called()
        observe.assert_not_called()
        self.assertEqual(replayed["close_reason"], "landed")
        self.assertTrue(replayed["terminal"])

    def test_undeclared_landed_renders_exactly_and_never_approved(self):
        row = self.verdict_row(None, ref=self.b, lane="lane/und-landed")
        _out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(err)
        lr = landreq.get(row["id"])[0]
        text = landreq._render_show(lr)
        self.assertIn("REVIEWED (UNDECLARED) — CLOSED (LANDED)", text)
        self.assertNotIn("APPROVED", text)

    def test_fix_stranded_renders_exactly(self):
        row, _tip = self.pruned_verdict_row(lane="lane/fix-stranded-show")
        _out, err = landreq.close(row["id"], "stranded",
                                  evidence="tip pruned by trunk rewrite")
        self.assertIsNone(err)
        lr = landreq.get(row["id"])[0]
        text = landreq._render_show(lr)
        self.assertIn("CHANGES_REQUESTED — CLOSED (STRANDED)", text)
        self.assertNotIn("APPROVED", text)
        self.assertEqual(lr["timeline"][-1]["state"], "CLOSED_STRANDED")

    def test_approve_landed_renders_its_exact_string(self):
        row = self.verdict_row("approve")
        self.git("merge", "--no-edit", "-q", "side")
        landreq.close(row["id"], "landed", live=True)
        lr = landreq.get(row["id"])[0]
        text = landreq._render_show(lr)
        self.assertIn("READY — CLOSED (LANDED)", text)

    def test_landed_rewritten_renders_for_translated_proofs(self):
        row, tip = self.pruned_verdict_row(polarity="approve",
                                           lane="lane/rewritten-render")
        self.sidecar(tip, self.b)
        _out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(err)
        lr = landreq.get(row["id"])[0]
        self.assertIn("CLOSED (LANDED_REWRITTEN)",
                      landreq._render_show(lr))
        self.assertIn("CLOSED (LANDED_REWRITTEN)", landreq._line(lr))

    def test_a_later_land_re_exposes_a_close_withdrawn_row_as_contrary(self):
        """The :1312 port: close --reason withdrawn stays FALSIFIABLE — a
        later land re-exposes the row as CONTRARY, non-terminal, owed by the
        integrator, flagged close_contradicted."""
        row = self.verdict_row("fix", lane="lane/withdrawn-then-landed")
        out, err = landreq.close(row["id"], "withdrawn",
                                 evidence="absent at close time")
        self.assertIsNone(err)
        self.assertTrue(out["terminal"])
        self.git("merge", "--no-edit", "-q", "side")
        reland = landreq.get(row["id"])[0]
        self.assertTrue(reland["contrary"],
                        "a later land must re-expose the row")
        self.assertFalse(reland["terminal"])
        self.assertEqual(reland["owed_by"], "integrator")
        self.assertTrue(reland["close_contradicted"])
        self.assertEqual(reland["close_reason"], "withdrawn")
        # and the :1332 port — the card carries the contradiction
        cards = {c["id"]: c for c in landreq.board_section()["loops"]}
        self.assertIn(row["id"], cards,
                      "a contradicted row re-appears on the board")
        self.assertTrue(cards[row["id"]]["close_contradicted"])
        self.assertEqual(cards[row["id"]]["owed_by"], "integrator")

    def test_a_later_land_reopens_a_withdrawn_row_of_EVERY_polarity(self):  # noqa: VACUOUS_ASSERTION — the control is inside a loop over a four-element literal tuple, so it runs exactly four times and never zero; hoisting it out would assert about one polarity instead of all four
        """THE WIDENING'S CONSEQUENCE, and it did not arrive with the widening.

        Admitting APPROVE and CONCUR to `withdrawn` created rows whose
        falsifiable premise nothing rechecked: `close_contradicted` derived
        from `live_contrary`, which is FIX/SUPERSEDE-only, so a withdrawn
        APPROVE that later landed stayed silently terminal forever — while the
        clause above promises the opposite for every withdrawal.

        The withdrawal's premise is ITS OWN — the work is ABSENT — and a later
        land falsifies that whatever sign the verdict carried. What the
        correction OBLIGES differs by polarity, and the arm asserts both
        halves rather than assuming the FIX answer generalizes.
        """
        # ONE FIXTURE, ONE MERGE, FOUR ROWS — and NEVER a second self.setUp().
        # Re-running setUp re-captures `self.prior` from an environment this
        # fixture has ALREADY modified, so tearDown then writes the temporary
        # HELM_* values back as if they were the originals. That leaks an
        # isolated home into every module that runs after this one, and the
        # damage lands somewhere unrelated — measured as three roster-history
        # failures in tests/test_seatname_guard.py, which shares nothing with
        # this file but the environment.
        rows = {}
        for polarity in ("approve", "concur", "fix", "supersede"):
            row = self.verdict_row(polarity, lane="lane/withdrawn-%s" % polarity)
            _out, err = landreq.close(row["id"], "withdrawn",
                                      evidence="absent at close time")
            self.assertIsNone(err, "%s could not be withdrawn" % polarity)
            # UNCONDITIONAL POSITIVE CONTROL on the same row: while the work
            # stays ABSENT the row is terminal and owes nobody, so the
            # reopening below is the later LAND and not a close that never
            # took.
            before = landreq.get(row["id"])[0]
            self.assertTrue(before["terminal"])
            self.assertFalse(before["close_contradicted"])
            rows[polarity] = row

        self.git("merge", "--no-edit", "-q", "side")

        for polarity, row in rows.items():
            with self.subTest(polarity=polarity):
                after = landreq.get(row["id"])[0]
                # THE FLAG IS THE CLAIM, FOR EVERY POLARITY: the withdrawal
                # said ABSENT and the work is on trunk, so the record is
                # corrected rather than left asserting something false.
                self.assertTrue(after["close_contradicted"],
                                "a landed withdrawal kept its absence claim")
                # WHAT FOLLOWS FROM IT DIFFERS BY POLARITY, and that is the
                # distinction the cure exists to preserve. A do-not-land
                # verdict whose work landed owes the integrator an answer. An
                # APPROVE whose work landed reached the outcome it authorized:
                # state LANDED is a genuine terminal and there is no
                # obligation to manufacture.
                if polarity in ("fix", "supersede"):
                    self.assertFalse(after["terminal"])
                    self.assertEqual("integrator", after["owed_by"])
                else:
                    self.assertTrue(after["terminal"])
                    self.assertEqual("LANDED", after["state"])
                    self.assertFalse(after["contrary"])

    def test_reopened_withdrawal_list_and_card_agree_for_every_polarity(self):
        """THE FLAG WENT BIMODAL AND ITS RENDERER DID NOT.

        Admitting APPROVE and CONCUR to `withdrawn` split what
        `close_contradicted` implies: a reopened FIX or SUPERSEDE owes the
        integrator an answer, a reopened APPROVE or CONCUR reached the outcome
        it authorized and owes NOBODY. The list mark printed "owed by
        integrator" unconditionally, so half the reopened rows advertised a
        debt `owed_by` does not hold — and the card, which reads the field,
        disagreed with the line on the same row.

        `owed_by` is the authoritative holder, so both surfaces must agree
        with IT rather than with each other's prose.
        """
        rows = {}
        for polarity in ("approve", "concur", "fix", "supersede"):
            row = self.verdict_row(polarity, lane="lane/parity-%s" % polarity)
            _out, err = landreq.close(row["id"], "withdrawn",
                                      evidence="absent at close time")
            self.assertIsNone(err, "%s could not be withdrawn" % polarity)
            rows[polarity] = row

        self.git("merge", "--no-edit", "-q", "side")
        cards = {c["id"]: c for c in landreq.board_section()["loops"]}
        # MUST-HIT FOR THE ABSENCE HALF: the APPROVE/CONCUR arms below assert
        # these rows are NOT on the board, which an empty board would satisfy
        # for the wrong reason. The board is non-empty and holds exactly the
        # two polarities that still owe.
        self.assertEqual({rows["fix"]["id"], rows["supersede"]["id"]},
                         set(cards),
                         "the board did not hold exactly the owing rows")

        for polarity, row in rows.items():
            with self.subTest(polarity=polarity):
                lr = landreq.get(row["id"])[0]
                line = landreq._line(lr)
                # MUST-HIT. Every assertion below about what the line does NOT
                # say is vacuous if the mark never rendered, and the mark is
                # exactly what a regression here would drop.
                self.assertTrue(lr["close_contradicted"],
                                "the withdrawal kept its absence claim")
                self.assertIn("close CONTRADICTED", line,
                              "the contradiction mark did not render at all")
                # THE LINE AGREES WITH THE DISPLAY AUTHORITY, not with a rule
                # restated in prose and not with the raw enforcement field.
                # _owed_by_whom routes through ball_holder, which carries
                # precedence owed_by does not.
                holder = landreq._owed_by_whom(lr)
                self.assertIn("owed by %s" % holder, line)

                # DIRECT CARD PARITY, all four polarities, no membership
                # question involved: the card projects the same holder the
                # line prints, so the two surfaces cannot drift.
                row_card = landreq.card(lr)
                role, who = landreq.ball_holder(lr)
                self.assertEqual(role, row_card["holder_role"])
                self.assertEqual(who, row_card["holder_seat"])
                self.assertEqual(holder,
                                 "%s (%s)" % (role, who) if who else role)
                self.assertTrue(row_card["close_contradicted"])

                if polarity in ("fix", "supersede"):
                    self.assertEqual("integrator", lr["owed_by"])
                    self.assertFalse(lr["terminal"])
                    self.assertTrue(lr["contrary"])
                    self.assertIn("owed by integrator", line)
                else:
                    # The half the old renderer billed wrongly.
                    self.assertEqual("nobody", lr["owed_by"])
                    self.assertEqual("nobody", holder)
                    self.assertEqual("LANDED", lr["state"])
                    self.assertTrue(lr["terminal"])
                    self.assertFalse(lr["contrary"])
                    self.assertNotIn("owed by integrator", line)

                # CARD PARITY, AND IT IS BIMODAL TOO — which I got wrong
                # first and this arm caught. board_section is LAND LOOPS: it
                # carries OPEN loops, so a reopened FIX or SUPERSEDE belongs
                # on it and a reopened APPROVE or CONCUR does NOT, being
                # terminal and owing nobody. The board's silence about those
                # two is the same statement the line makes with "owed by
                # nobody", so parity here means the two surfaces agree about
                # THE DEBT, never that both print the row.
                if polarity in ("fix", "supersede"):
                    self.assertIn(row["id"], cards,
                                  "an open contradicted loop left the board")
                    card = cards[row["id"]]
                    self.assertTrue(card["close_contradicted"])
                    self.assertEqual(lr["owed_by"], card["owed_by"],
                                     "the card and the row disagree on who owes")
                else:
                    self.assertNotIn(row["id"], cards,
                                     "a row owing nobody was billed as an "
                                     "open land loop")

    def test_a_reopened_withdrawal_is_not_retroactively_a_do_not_land(self):
        """THE ARM THAT REFUSES THE OBVIOUS WRONG FIX. Adding approve to
        `live_contrary` would have satisfied the arm above and been wrong:
        that flag means a row landed DESPITE a do-not-land verdict, and an
        APPROVE landing is the desired outcome, not a violation. Widening it
        would badge every landed approved row as contrary.

        What a falsified withdrawal re-opens is the OBLIGATION, never the
        verdict's meaning.
        """
        # POSITIVE CONTROL, unconditional and first: a FIX that lands IS a
        # contrary, so the absence below is the polarity and not a flag that
        # stopped being set.
        fix = self.verdict_row("fix", lane="lane/contrary-fix")
        _o, err = landreq.close(fix["id"], "withdrawn", evidence="absent")
        self.assertIsNone(err)
        approve = self.verdict_row("approve", lane="lane/contrary-approve")
        _o, err = landreq.close(approve["id"], "withdrawn", evidence="absent")
        self.assertIsNone(err)
        self.git("merge", "--no-edit", "-q", "side")
        self.assertTrue(landreq.get(fix["id"])[0]["contrary"])
        landed_approve = landreq.get(approve["id"])[0]
        self.assertTrue(landed_approve["close_contradicted"],
                        "the obligation must still re-open")
        self.assertFalse(landed_approve["contrary"],
                         "a landed APPROVE was badged as landed-despite-a-"
                         "do-not-land verdict")

    def test_an_UNKNOWN_land_state_leaves_a_withdrawal_retired(self):
        """Fail closed in the reopening direction too: absence was proven at
        close time and nothing has disproven it, so an unreadable land state
        is not a contradiction."""
        row = self.verdict_row("approve", lane="lane/withdrawn-unknown")
        _o, err = landreq.close(row["id"], "withdrawn", evidence="absent")
        self.assertIsNone(err)
        with mock.patch.object(landreq, "_landing_proof",
                               return_value="unknown"):
            unknown = landreq.get(row["id"])[0]
        self.assertFalse(unknown["close_contradicted"])
        self.assertTrue(unknown["terminal"])

    def test_superseded_close_shows_contrary_history_not_a_launder(self):
        """The :875 shape under close: the contrary FACT stays visible beside
        the terminal, blind to live git (recorded, monotonic)."""
        row = self.verdict_row("fix", lane="lane/sup-history")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve findings", path="g")
        self.git("checkout", "-q", self.main)
        second_row, why, sent = dispatches.send(
            "codex-3", "lane/sup-history-r2", "review r2", fixed,
            repo=self.repo, key="key-sup-r2", sign=False,
            supersedes=row["id"])
        self.assertIsNone(why)
        self.mark_verdict(second_row["id"], fixed, "clean",
                                polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        out, err = landreq.close(row["id"], "superseded",
                                 evidence="r2 approved", tip=fixed)
        self.assertIsNone(err)
        self.assertEqual(out["state"], "CHANGES_REQUESTED")
        self.assertTrue(out["contrary"] and out["terminal"])
        self.assertEqual(out["contrary_state"], "landed")
        self.assertEqual(out["contrary_target"], "local")
        self.assertEqual(out["owed_by"], "nobody")
        self.assertEqual(out["timeline"][-1]["state"], "CLOSED_SUPERSEDED")
        shown = landreq._render_show(out)
        self.assertIn("CONTRARY", shown.upper())
        self.assertIn("CLOSED (SUPERSEDED)", shown)
        self.assertIn(fixed, shown)
        line = landreq._line(out)
        self.assertIn("CLOSED (SUPERSEDED) by %s" % fixed[:12], line)
        # blind git replay keeps the recorded facts (monotonic)
        blind = {"observable": False, "local": False, "upstream": False,
                 "has_upstream": False}
        with mock.patch.object(landreq, "_git_observe", return_value=blind):
            replayed = landreq.get(row["id"])[0]
        self.assertTrue(replayed["contrary"] and replayed["terminal"])
        self.assertEqual(replayed["contrary_state"], "landed")

    def test_stalls_and_unmeasurable_exclude_closed_rows(self):
        row = self.verdict_row(None, ref=self.b, lane="lane/und-quiet")
        landreq.close(row["id"], "landed", live=True)
        self.assertEqual(landreq.stalls()[0], [])
        self.assertEqual(landreq.unmeasurable()[0], [])


class CloseDryRunTest(CloseBase):
    def test_dry_run_appends_nothing_on_the_pass_path(self):
        row = self.verdict_row("approve")
        self.git("merge", "--no-edit", "-q", "side")
        before = self.history_len(row["id"])
        rc, out, err = run(["close", row["id"], "--reason", "landed", "--live",
                            "--dry-run"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("WOULD close", out)
        self.assertIn("nothing appended", out)
        self.assertEqual(self.history_len(row["id"]), before)
        self.assertIsNone(dispatches.snapshot()[0][row["id"]]
                          .get("close_reason"))

    def test_dry_run_refuse_path_mirrors_the_live_refusal_exactly(self):
        row = self.verdict_row("fix", lane="lane/dry-refuse")
        before = self.history_len(row["id"])
        rc, _out, dry_err = run(["close", row["id"], "--reason", "landed", "--live",
                                 "--dry-run"])
        self.assertEqual(rc, 1)
        rc, _out, live_err = run(["close", row["id"], "--reason", "landed", "--live"])
        self.assertEqual(rc, 1)
        self.assertEqual(dry_err.replace(" [dry-run]", ""), live_err)
        self.assertIn("CONTRARY, not a resolution", dry_err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_stranded_dry_run_carries_the_blob_containment_advisory(self):
        row, _tip = self.pruned_verdict_row(lane="lane/dry-stranded")
        before = self.history_len(row["id"])
        rc, out, err = run(["close", row["id"], "--reason", "stranded",
                            "--evidence", "pruned by rewrite", "--dry-run",
                            "--json"])
        self.assertEqual((rc, err), (0, ""))
        report = json.loads(out)
        self.assertEqual(report["proof_mode"], "object-pruned")
        self.assertIn("advisory only", report["blob_containment"])
        self.assertIn("never gates", report["blob_containment"])
        self.assertEqual(self.history_len(row["id"]), before)

    def test_out_of_scope_dry_run_appends_nothing(self):
        row = self.dispatch(lane="lane/dry-oos")
        before = self.history_len(row["id"])
        rc, out, err = run(["close", row["id"], "--reason", "out-of-scope",
                            "--evidence", "moot", "--dry-run", "--json"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.history_len(row["id"]), before)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"],
                         "open")

    def test_a_dry_run_answers_while_another_holder_has_the_ledger_lock(self):  # noqa: VACUOUS_ASSERTION — the uncontended answer is asserted to (True, None) first and the contended one to the same exact list; `waited` is the bounded wait itself and the unchanged history is the no-append law
        """A REHEARSAL NEVER WAITS ON THE DISPATCH LEDGER LOCK.

        The /api/lr rebuild asks `off_frontier_closable` about each row; a
        rehearsal that took the lock made that rebuild hold it without a break,
        and every fleet `dispatch send` and `dispatch verdict` waited minutes
        behind questions that can never append. The census path is driven
        here whole: the same door, a FIX row git proves absent from trunk,
        and the lock held by another descriptor for the whole question. The
        wait is bounded so a regression is red rather than hung."""
        row = self.verdict_row("fix", lane="lane/dry-takes-no-lock")
        lr = landreq.get(row["id"])[0]
        verdict = {"close_reason": "withdrawn",
                   "evidence": "the lane is gone and no ref holds the work"}
        before = self.history_len(row["id"])
        # UNCONDITIONAL POSITIVE CONTROL, uncontended and first: the door
        # says it WOULD close this row, so the contended answer below is the
        # same question answered, not a refusal that returned early.
        self.assertEqual(landreq.off_frontier_closable(lr, verdict),
                         (True, None))
        answers = []
        worker = threading.Thread(
            target=lambda: answers.append(
                landreq.off_frontier_closable(lr, verdict)),
            daemon=True)
        with eventledger.locked(dispatches.ledger_path()) as held:
            self.assertTrue(held, "the arm could not take the lock itself")
            worker.start()
            worker.join(60)
            waited = worker.is_alive()
        worker.join()
        self.assertFalse(waited, "a dry run waited on the dispatch ledger "
                                 "lock another holder had")
        self.assertEqual(answers, [(True, None)])
        self.assertEqual(self.history_len(row["id"]), before,
                         "a rehearsal appended")


class AliasEquivalenceTest(CloseBase):
    """The deprecated verbs are argument-mapping veneers over close() — ONE
    code path. Each alias writes the SAME `close` event the direct call
    writes (the direct call then reconciles it as an idempotent retry of
    itself — identity, not similarity), and each prints its deprecation."""

    def test_withdraw_alias_writes_the_close_event_direct_close_owns(self):
        row = self.verdict_row("fix", lane="lane/alias-withdrawn")
        rc, _out, err = run(["withdraw", row["id"], "kept", "off", "trunk"])
        self.assertEqual(rc, 0, err)
        self.assertIn("helm lr withdraw is deprecated: use helm lr close "
                      "--reason withdrawn", err)
        event = next(e for e in dispatches.history(row["id"])
                     if e.get("event") == "close")
        self.assertEqual(event["close_reason"], "withdrawn")
        self.assertEqual(event["close_evidence"], "kept off trunk")
        before = self.history_len(row["id"])
        _out2, err2 = landreq.close(row["id"], "withdrawn",
                                    evidence="kept off trunk")
        self.assertIsNone(err2)               # identity: idempotent retry
        self.assertEqual(self.history_len(row["id"]), before)

    def test_close_landed_alias_writes_the_close_event_direct_close_owns(self):
        row = self.verdict_row(None, ref=self.b, lane="lane/alias-landed")
        rc, _out, err = run(["close-landed", row["id"], "--trunk", self.main,
                             "--live"])
        self.assertEqual(rc, 0, err)
        self.assertIn("helm lr close-landed is deprecated: use helm lr close "
                      "--reason landed", err)
        event = next(e for e in dispatches.history(row["id"])
                     if e.get("event") == "close")
        self.assertEqual(event["close_reason"], "landed")
        self.assertEqual(event["close_proof_mode"], "ancestor")
        before = self.history_len(row["id"])
        _out2, err2 = landreq.close(row["id"], "landed", trunk=self.main, live=True)
        self.assertIsNone(err2)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_discharge_alias_writes_the_close_event_direct_close_owns(self):
        first = self.verdict_row("fix", lane="lane/alias-superseded")
        self.git("checkout", "-q", "side")
        fixed = self.commit("alias resolve", path="g")
        self.git("checkout", "-q", self.main)
        second, why, sent = dispatches.send(
            "codex-3", "lane/alias-superseded-r2", "review r2", fixed,
            repo=self.repo, key="key-alias-r2", sign=False,
            supersedes=first["id"])
        self.assertIsNone(why)
        self.mark_verdict(second["id"], fixed, "clean",
                                polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        rc, _out, err = run(["discharge", first["id"], fixed,
                             "r2", "approved"])
        self.assertEqual(rc, 0, err)
        self.assertIn("helm lr discharge is deprecated: use helm lr close "
                      "--reason superseded", err)
        event = next(e for e in dispatches.history(first["id"])
                     if e.get("event") == "close")
        self.assertEqual(event["close_reason"], "superseded")
        self.assertEqual(event["superseding_tip"], fixed)
        self.assertEqual(event["close_evidence"], "r2 approved")
        before = self.history_len(first["id"])
        _out2, err2 = landreq.close(first["id"], "superseded",
                                    evidence="r2 approved", tip=fixed)
        self.assertIsNone(err2)
        self.assertEqual(self.history_len(first["id"]), before)

    def test_alias_refusal_paths_mirror_close(self):
        open_row = self.dispatch(lane="lane/alias-refuse")
        rc, _out, err = run(["withdraw", open_row["id"], "evidence"])
        self.assertEqual(rc, 1)
        self.assertIn("deprecated", err)
        # The refusal wording follows the widened gate: the population is no
        # longer FIX/SUPERSEDE, it is every DECLARED polarity, so the alias
        # mirrors "not a withdrawable verdict row".
        self.assertIn("not a withdrawable verdict row", err)


class CloseResolvedTest(CloseBase):
    """`--reason resolved` — the POLARITY-WRONG door.

    Population: a FIX/SUPERSEDE verdict whose OWN reviewed tip reached trunk.
    Every other reason refuses it, and `withdrawn` would be a LIE because it
    proves ABSENCE while this work is present. The discriminator against
    withdrawn is land_state, so the ancestry rung is the one that must never
    soften — it is also the one that was WRONG on the first cut (it used
    _landed, which admits patch-equivalence) and is pinned twice below."""

    RESOLUTION = "Resolution verified on trunk: the AST reimplementation is " \
                 "live and the roster check sees comment-free Call nodes"

    def resolved_families(self, same=False):
        """Family resolution for the two identities THIS suite uses.

        CloseBase.family_evidence only knows 'integrator'/'claude-reviewer';
        these fixtures use the live shape instead (a claude-family author
        confirmed by kimi), and real resolution needs a roster the hermetic
        tmp home does not have. `same=True` collapses both to one family so
        the cross-family rung can be exercised in the refusing direction."""
        def families(identity):
            family = {"opus-integrator": "claude",
                      "kimi": "claude" if same else "kimi"}.get(identity)
            values = set() if family is None else {family}
            evidence = {"v": 1, "identity": identity,
                        "minted_families": sorted(values),
                        "roster_family": None, "roster_verified": False}
            return (values, evidence,
                    dispatches._subsumed_family_anchor(evidence), None)
        return mock.patch.object(
            dispatches, "_approval_identity_family_evidence",
            side_effect=families)

    def test_a_same_family_confirmation_is_refused(self):
        """Cross-family is a RUNG, not a convention. Same fixture, same
        evidence, one difference: the confirming reviewer shares the author's
        family — which is what reviewer-shopping would look like if it were
        also in-family."""
        original, confirmation = self.resolved_pair()
        before = self.history_len(original["id"])
        self.assertGreater(before, 0)
        with self.resolved_families(same=True):
            rc, _out, err = run(["close", original["id"][:12], "--reason",
                                 "resolved", "--evidence",
                                 confirmation["id"][:12]])
        self.assertEqual(rc, 1)
        self.assertIn("cross-family", err)
        self.assertEqual(self.history_len(original["id"]), before)

    def resolved_pair(self, polarity="supersede", confirm_polarity="supersede",
                      confirm_ref=None, confirm_recipient="kimi"):
        """(original, confirmation) — original's tip MERGED to trunk so it is a
        true ANCESTOR, confirmation appended strictly later."""
        # The AUTHOR must resolve to an identity family or the cross-family
        # rung refuses with "got none" — setUp deliberately clears
        # HELM_CHAT_NAME, so a fixture that does not seed it can never reach
        # the rungs past it. Mirrors the live shape exactly: a claude-family
        # author confirmed by a kimi reviewer.
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        self.addCleanup(os.environ.pop, "HELM_CHAT_NAME", None)
        original = self.verdict_row(polarity, recipient="codex-3")
        self.git("merge", "--no-edit", "-q", "side")
        tip = self.git("rev-parse", self.main)
        # kind="review" explicitly: the shared verdict_row helper does not set
        # it, and the door requires a REVIEW verdict. Built here rather than by
        # widening that helper, which every other suite in this file shares.
        confirmation, why, sent = dispatches.send(
            confirm_recipient, "lane/confirming-round", "confirm the round",
            tip, repo=self.repo, key="key-confirming-round", sign=False,
            kind="review", new_work=True)
        self.assertIsNone(why)
        self.assertTrue(sent)
        ref = self.RESOLUTION if confirm_ref is None else confirm_ref
        _out, err = self.mark_verdict(confirmation["id"], tip, ref,
                                            polarity=confirm_polarity)
        self.assertIsNone(err)
        return original, confirmation

    def test_a_supersede_confirmation_closes_and_records_the_pin(self):
        """THE HAPPY PATH, and the SUPERSEDE confirmation is the whole point:
        this is the temporal deadlock ended. A door that demanded an APPROVE
        here would refuse the exact case it was built for.

        Also the end-to-end proof that `resolved` reaches the ledger — the
        rungs refusing correctly says nothing about whether a close can be
        WRITTEN, and _CLOSE_STATE_FIELDS had no `resolved` entry until this
        test demanded one."""
        original, confirmation = self.resolved_pair()
        pinned = self.git("rev-parse", self.main)
        before = self.history_len(original["id"])
        self.assertGreater(before, 0)
        with self.resolved_families():
            rc, out, err = run(["close", original["id"][:12], "--reason",
                                "resolved", "--evidence",
                                confirmation["id"][:12]])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("CLOSED", out)
        snap = dispatches.snapshot()[0][original["id"]]
        self.assertEqual(snap["close_reason"], "resolved")
        # the PIN travels, so the sha this close was DECIDED on stays auditable
        # after any later trunk movement
        self.assertEqual(snap["closing_trunk_sha"], pinned)
        self.assertEqual(snap["closing_trunk_ref"], "refs/heads/" + self.main)
        self.assertEqual(snap["confirmation_id"], confirmation["id"])
        self.assertEqual(snap["close_proof_mode"], "resolved-on-pinned-trunk")
        # the original's own polarity is preserved — resolved retires the debt,
        # it never rewrites the verdict that was recorded
        self.assertEqual(snap["polarity"], "supersede")
        self.assertGreater(self.history_len(original["id"]), before)

    def test_the_consumed_confirmation_stops_being_a_live_contrary_debt(self):
        """THE DOOR'S OWN EXHAUST. Closing the original leaves the CONFIRMATION
        row open forever: its tip is on trunk (it was written ON the landed
        successor, which is the point of it) under a non-approve polarity —
        the contrary signature exactly — and nothing ever closes it. Measured
        Measured: 13 contrary rows, all 13 confirmations,
        zero originals, so EVERY USE OF THE DOOR MINTED ONE PERMANENT DEBT.

        The FACT stays true and keeps rendering; only the DEBT is retired,
        which is exactly how `discharged` behaves."""
        original, confirmation = self.resolved_pair()
        with self.resolved_families():
            rc, _out, err = run(["close", original["id"][:12], "--reason",
                                 "resolved", "--evidence",
                                 confirmation["id"][:12]])
        self.assertEqual((rc, err), (0, ""))
        lrs, why = landreq.project()
        self.assertIsNone(why)
        conf = lrs[confirmation["id"]]
        # POSITIVE CONTROL on the same observable: the row really IS the
        # landed-under-a-non-approve-verdict shape, so the suppression below
        # is about CONSUMPTION and not about the row failing to qualify.
        self.assertTrue(conf["contrary"],
                        "fixture must produce a genuine contrary row, else "
                        "the assertions below are vacuous")
        self.assertEqual(conf["polarity"], "supersede")
        # the FACT is preserved and NAMED; the DEBT is gone
        self.assertEqual(conf["consumed_by"], original["id"])
        self.assertTrue(conf["terminal"])
        self.assertEqual(conf["owed_by"], "nobody")

    def test_an_unconsumed_confirmation_row_stays_a_live_debt(self):
        """THE NEGATIVE CONTROL, and it is the whole point of keying on the
        LINK rather than on the shape: a confirmation-shaped row that no close
        recorded as evidence is still an unexplained land under a non-approve
        verdict, and must keep billing the integrator. If this ever goes
        terminal, the fix has stopped distinguishing consumed evidence from
        an unauthorized land — which is the alarm's entire job."""
        _original, confirmation = self.resolved_pair()   # no close is run
        lrs, why = landreq.project()
        self.assertIsNone(why)
        conf = lrs[confirmation["id"]]
        self.assertTrue(conf["contrary"])
        self.assertIsNone(conf["consumed_by"])
        self.assertFalse(conf["terminal"])
        self.assertEqual(conf["owed_by"], "integrator")

    def test_the_ancestry_rung_refuses_a_patch_identity_only_tip(self):
        """RULING (i), and the defect this test exists for was LIVE: the first
        cut called _landed(), which returns True for `patch-equivalent` as
        well as `ancestor`, while the comment beside it said ancestry-only.
        b71f8dab (tip 453fb099, whose 3-line delta reached trunk under a
        different commit) walked past this rung to the phrase check.

        A patch-id match cannot tell 'this work landed' from 'someone else
        wrote the same lines', and a binding ledger may not guess."""
        original = self.verdict_row("supersede", recipient="codex-3")
        # cherry-pick reproduces the DELTA on trunk without making the
        # reviewed tip an ancestor of it.
        self.git("cherry-pick", "-x", self.side)
        before = self.history_len(original["id"])
        # POSITIVE CONTROL on the very observable the refusal asserts about:
        # "history unchanged" is vacuous if the history were empty or the id
        # wrong, and both failures read exactly like a clean refusal.
        self.assertGreater(before, 0, "ledger history must be non-empty "
                                      "before an unchanged-length assertion "
                                      "can mean anything")
        # MUST-HIT for the fixture itself: the delta really is patch-present,
        # so a softer rung WOULD have admitted this row.
        proof = landreq._landing_proof(self.gitdir(), self.side,
                                       self.git("rev-parse", self.main))
        self.assertEqual(proof, "patch-equivalent",
                         "fixture must produce a patch-equivalent-but-not-"
                         "ancestor tip, got %r" % proof)
        rc, _out, err = run(["close", original["id"][:12], "--reason",
                             "resolved", "--evidence", "deadbeefdead"])
        self.assertEqual(rc, 1)
        self.assertIn("PATCH IDENTITY", err)
        self.assertEqual(self.history_len(original["id"]), before)

    def test_an_absent_tip_is_routed_to_withdrawn_not_resolved(self):
        """The population boundary. Work ABSENT from trunk is withdrawn's
        question; answering it here would let `resolved` swallow the reason
        that exists to prove absence."""
        original = self.verdict_row("supersede", recipient="codex-3")
        before = self.history_len(original["id"])
        # POSITIVE CONTROL on the very observable the refusal asserts about:
        # "history unchanged" is vacuous if the history were empty or the id
        # wrong, and both failures read exactly like a clean refusal.
        self.assertGreater(before, 0, "ledger history must be non-empty "
                                      "before an unchanged-length assertion "
                                      "can mean anything")
        rc, _out, err = run(["close", original["id"][:12], "--reason",
                             "resolved", "--evidence", "deadbeefdead"])
        self.assertEqual(rc, 1)
        self.assertIn("withdrawn's question", err)
        self.assertEqual(self.history_len(original["id"]), before)

    def test_a_fix_confirmation_is_refused_however_it_is_worded(self):
        """AMENDMENT A. FIX's meaning IS not-resolved, so a FIX carrying the
        phrase is incoherent, not persuasive — and an honest confused reviewer
        can construct one. The phrase is deliberately PERFECT here so the
        refusal can only come from the polarity rung."""
        original, confirmation = self.resolved_pair(confirm_polarity="fix")
        before = self.history_len(original["id"])
        # POSITIVE CONTROL on the very observable the refusal asserts about:
        # "history unchanged" is vacuous if the history were empty or the id
        # wrong, and both failures read exactly like a clean refusal.
        self.assertGreater(before, 0, "ledger history must be non-empty "
                                      "before an unchanged-length assertion "
                                      "can mean anything")
        rc, _out, err = run(["close", original["id"][:12], "--reason",
                             "resolved", "--evidence", confirmation["id"][:12]])
        self.assertEqual(rc, 1)
        self.assertIn("never fix", err)
        self.assertEqual(self.history_len(original["id"]), before)

    def test_the_phrase_is_read_at_the_head_and_nowhere_else(self):
        """The anchor's real job: a reviewer DISPUTING a resolution must not
        mint a close out of their own refutation. Having spent polarity to end
        the temporal deadlock, this phrase IS the check."""
        disputing = ("I dispute the claim that " + self.RESOLUTION)
        original, confirmation = self.resolved_pair(confirm_ref=disputing)
        before = self.history_len(original["id"])
        # POSITIVE CONTROL on the very observable the refusal asserts about:
        # "history unchanged" is vacuous if the history were empty or the id
        # wrong, and both failures read exactly like a clean refusal.
        self.assertGreater(before, 0, "ledger history must be non-empty "
                                      "before an unchanged-length assertion "
                                      "can mean anything")
        # MUST-HIT: the same sentence ANCHORED does parse, so the refusal below
        # is about POSITION and not about the wording.
        self.assertTrue(dispatches.resolution_statement(self.RESOLUTION))
        self.assertIsNone(dispatches.resolution_statement(disputing))
        rc, _out, err = run(["close", original["id"][:12], "--reason",
                             "resolved", "--evidence", confirmation["id"][:12]])
        self.assertEqual(rc, 1)
        self.assertIn("at the head", err)
        self.assertEqual(self.history_len(original["id"]), before)

    def test_a_row_can_never_confirm_its_own_resolution(self):
        original = self.verdict_row("supersede", recipient="codex-3")
        self.git("merge", "--no-edit", "-q", "side")
        before = self.history_len(original["id"])
        # POSITIVE CONTROL on the very observable the refusal asserts about:
        # "history unchanged" is vacuous if the history were empty or the id
        # wrong, and both failures read exactly like a clean refusal.
        self.assertGreater(before, 0, "ledger history must be non-empty "
                                      "before an unchanged-length assertion "
                                      "can mean anything")
        rc, _out, err = run(["close", original["id"][:12], "--reason",
                             "resolved", "--evidence", original["id"][:12]])
        self.assertEqual(rc, 1)
        self.assertIn("confirm its own resolution", err)
        self.assertEqual(self.history_len(original["id"]), before)


if __name__ == "__main__":
    unittest.main()


class CloseCarriedTest(CloseBase):
    """task/756 — `--reason carried`, the door for rows whose work IS on
    trunk but whose discharge was never chain-linked.

    THE CLASS IS MEASURED, NOT HYPOTHETICAL: a READY row whose lander closed
    nothing, and a FIX-verdicted row whose work landed anyway. Every other
    reason refuses them for a correct reason, so they sit forever and the
    anomaly count they hold up has a floor above zero BY CONSTRUCTION.

    THE GATE IS A MEASUREMENT. Authorization comes from
    `dispatches.carriage_proof`, which delegates to the `rowworld` relation —
    replay the work onto trunk HEAD from its chain-bound base and ask whether
    the result IS HEAD. That is STRICTLY STRONGER than what `landed` demands,
    which is why a reason that closes MORE rows is not a Goodhart hole."""

    def test_the_registers_are_all_in_step(self):
        """A ninth reason trips four independent registers, and a MISSING one
        is a KeyError at write time rather than a refusal — so they are
        pinned together here instead of being discovered one crash apart."""
        self.assertIn("carried", landreq.CLOSE_CLI_REASONS)
        self.assertIn("carried", dispatches.CLOSE_REASONS)
        self.assertIn("carried", dispatches._CLOSE_POLARITY)
        self.assertIn("carried", dispatches._CLOSE_STATE_FIELDS)
        self.assertIn("carried", rowstate._CLOSE_TERMINAL)
        self.assertEqual(rowstate._CLOSE_TERMINAL["carried"], rowstate.LANDED)
        # ...and it reaches the operator, or nobody can use it.
        self.assertIn("carried", landreq.USAGE)

    def test_carried_refuses_without_evidence(self):
        row = self.verdict_row(polarity="fix")
        out, err = landreq.close(row["id"], "carried", dry_run=True)
        self.assertIsNone(out)
        self.assertIn("needs evidence", err)

    def test_a_NEVER_VERDICTED_row_whose_FILED_sha_is_on_trunk_closes(self):
        """task/2090 — the specimen shape. A review row awaiting its FIRST
        verdict carries no reviewed_tip (only a verdict populates that field),
        so the door read "bound to no immutable tip" while its own filed sha —
        chain-bound, immutable, exactly the anchor
        carriage-replay-base-must-be-chain-bound names — sat beside the
        refusal unread. The close must name WHICH field it bound.

        THE MUST-DIFFER CONTROL IS THE SECOND ROW OF THE SAME CLOSE ATTEMPT
        POPULATION: an identical never-verdicted row whose filed sha is NOT on
        trunk must still refuse — hc4 measured three of the five live stranded
        rows exactly that way, and a cure that unblocks all five is wrong."""
        # FILED, AND NEVER VERDICTED — NOT polarity=None, which APPLIES A
        # VERDICT EVENT carrying reviewed_tip and so resolves through the
        # FIRST anchor and never reaches the fallback under test (hc4's
        # revert measurement: the suite stayed green with the cure removed).
        # A bare dispatch carries only its filed sha.
        landed = self.dispatch(ref=self.side, lane="lane/carried-never",
                               kind="review")
        self.git("cherry-pick", "--no-commit", self.side)
        self.git("commit", "-qm", "carry the never-verdicted work")
        # THE MUST-DIFFER CONTROL, IN THE SAME REPO AND BEFORE THE CLOSE: a
        # second never-verdicted row whose filed sha is real work that never
        # reaches trunk — planted AFTER the carry so its patch is a distinct
        # file and the two rows cannot collide on patch identity. A cure that
        # unblocks everything satisfies the first half and fails here; hc4
        # measured three of the five live stranded rows exactly this way.
        other = self.commit("unlanded", path="h")
        unlanded = self.dispatch(ref=other, lane="lane/carried-never-unlanded",
                                 kind="review")
        out, err = landreq.close(landed["id"], "carried",
                                 evidence="the lander closed nothing",
                                 repo=self.repo, dry_run=True)
        self.assertIsNone(err)
        self.assertIsNotNone(out, "a never-verdicted row whose filed sha is "
                                  "patch-equivalent on trunk was refused")
        self.assertEqual(out["carried_tip"], self.side,
                         "the close did not name the field it bound")
        out2, err2 = landreq.close(unlanded["id"], "carried",
                                   evidence="the lander closed nothing",
                                   repo=self.repo, dry_run=True)
        self.assertIsNone(out2, "a never-verdicted row whose filed sha is "
                                "NOT on trunk closed — the refusal is the "
                                "property that keeps this door honest")
        self.assertTrue(err2)

    def test_a_NEVER_VERDICTED_row_CLOSES_FOR_REAL_not_only_in_dry_run(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the dry run on the SAME row through the SAME door directly above, asserting carried_tip; a red below it is the write and never the proof
        """THE WRITE, WHICH EVERY ARM ABOVE STOPS ONE LINE SHORT OF.

        task/2090 cured the LADDER and both of its arms pass dry_run=True,
        which returns the proof dict before `_record_close_proven` is ever
        called. So the ladder proves carriage, names `carried_tip` from the
        filed sha — and then hands `lr.get("reviewed_tip")` to a writer whose
        exemption list names landed-v2, delivered-report and discharged but
        NOT carried. On a never-verdicted row that field is None by
        construction, so the real close refuses "close needs the original
        full reviewed commit id" while every dry run says it would open.

        Measured live on row dc4b344a8950 before this arm existed.

        THE CONTROL IS THE SAME ROW THROUGH THE SAME DOOR IN DRY RUN: it must
        still be admitted, so a red here is the WRITE failing and never the
        proof.
        """
        landed = self.dispatch(ref=self.side, lane="lane/carried-real-write",
                               kind="review")
        self.git("cherry-pick", "--no-commit", self.side)
        self.git("commit", "-qm", "carry the never-verdicted work")
        # CONTROL FIRST, unconditional and on the same row: the ladder admits
        # it. Anything red below is the writer, not the carriage proof.
        dry, dry_err = landreq.close(landed["id"], "carried",
                                     evidence="the lander closed nothing",
                                     repo=self.repo, dry_run=True)
        self.assertIsNone(dry_err)
        self.assertEqual(dry["carried_tip"], self.side)
        # THE ACT.
        out, err = landreq.close(landed["id"], "carried",
                                 evidence="the lander closed nothing",
                                 repo=self.repo)
        self.assertIsNone(err, "the ladder admitted this row in dry run and "
                               "the WRITE refused it")
        self.assertIsNotNone(out)
        # THE INVARIANT IS THE CLOSE REASON, NOT A STATUS STRING.
        # `closed_state` is the authority and says why in as many words:
        # "status alone cannot answer it". A structured close records
        # `close_reason`; only some terminals also move `status`, so an arm
        # that pinned `status == "closed"` here would be asserting the
        # delivered-report contract against a carried close.
        replayed = dispatches.snapshot()[0][landed["id"]]
        self.assertEqual(dispatches.closed_state(replayed), "carried")
        self.assertEqual(replayed["carried_tip"], self.side)
        # A ROW THAT WAS NEVER REVIEWED MUST NOT READ AS REVIEWED AFTERWARDS.
        # The carried pair is the provenance; the reviewed tip stays empty.
        self.assertFalse(replayed.get("reviewed_tip"))

    def test_a_REHEARSAL_REFUSES_EXACTLY_WHERE_THE_WRITE_REFUSES(self):
        """task/2857, THE REGRESSION CONTROL. A rehearsal and a real close
        must differ in nothing but the write.

        MEASURED ON THE LIVE BOARD, which is why this arm exists: a
        CONCUR-polarity row dry-ran as "WOULD close -- --reason carried
        (proof reached-trunk)" and the real close answered "verdict polarity
        outside --reason carried's domain". 84 of 469 sweep candidates were
        in that state, so trusting the rehearsal would have produced 84
        refusals mid-batch. Nothing was appended, so the damage was an
        operator acting on a false yes rather than a corrupt ledger.

        WHY THE LADDER COULD NOT HAVE CAUGHT IT. The carriage proof is
        CORRECT for this row -- the work really is on trunk -- and every
        fact that refuses it is ROW STATE the writer re-reads under the
        ledger lock. A ladder that answered the rehearsal from its own proof
        was answering a different question from the one the operator asked.
        That is the shape, and it is not specific to `carried`: the arm two
        classes up found the same class one hole earlier (task/2090, a
        never-verdicted row whose dry run passed and whose write refused
        "close needs the original full reviewed commit id") and cured it by
        adding a real-write arm for that ONE reason. This cures it for every
        reason at once, so THAT arm is now the control for this one.

        THE ASSERTION IS EQUALITY OF THE TWO ANSWERS, not the wording. A
        future refusal may say something else and this arm should still
        hold; what must never come back is a rehearsal that says yes where
        the write says no.
        """
        row = self.dispatch(ref=self.side, lane="lane/rehearsal-equals-write",
                            kind="review")
        self.mark_verdict(row["id"], self.side, "reviewed", polarity="concur")
        # THE WORK REALLY IS CARRIED, so the ladder's own proof SUCCEEDS and
        # the refusal below can only come from the writer. Without this the
        # arm would pass on a row the ladder rejects, proving nothing.
        self.git("cherry-pick", "--no-commit", self.side)
        self.git("commit", "-qm", "carry the concur-verdicted work")
        dry, dry_err = landreq.close(row["id"], "carried",
                                     evidence="the lander closed nothing",
                                     repo=self.repo, dry_run=True)
        live, live_err = landreq.close(row["id"], "carried",
                                       evidence="the lander closed nothing",
                                       repo=self.repo)
        self.assertIsNone(live, "the write admitted a concur row into "
                                "carried's domain")
        self.assertIn("polarity", live_err or "")
        self.assertIsNone(dry, "THE REHEARSAL SAID WOULD CLOSE AND THE WRITE "
                               "REFUSED -- task/2857 is back: %r" % (dry,))
        self.assertEqual(dry_err, live_err,
                         "the rehearsal and the write refused for DIFFERENT "
                         "reasons, so one of them is not running the other's "
                         "validator")
        # THE POSITIVE CONTROL, and it is the half that makes the two
        # assertions above mean something. An identical row whose polarity IS
        # in carried's domain must still rehearse CLEAN through the same
        # door in the same repository -- otherwise this arm would pass with
        # the door bricked shut for every row.
        ok = self.dispatch(ref=self.side, lane="lane/rehearsal-control",
                           kind="review")
        self.mark_verdict(ok["id"], self.side, "reviewed", polarity="approve")
        out, err = landreq.close(ok["id"], "carried",
                                 evidence="the lander closed nothing",
                                 repo=self.repo, dry_run=True)
        self.assertIsNone(err, "the control row was refused, so the refusals "
                               "above say nothing about polarity: %s" % err)
        self.assertIsNotNone(out)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["carried_tip"], self.side)

    def test_a_carried_close_binds_a_LIVE_row_and_never_a_retired_one(self):
        """THE EXEMPTION IS FOR A ROW STILL OWED, NOT FOR EVERY ROW WITHOUT A
        REVIEWED TIP. A cancelled row and an already-closed row carry no
        reviewed tip either, and retirement is terminal: a carried event
        aimed at one must be inert, or a hand-appended row relabels history.

        THE EVENT IS THE REAL ONE. It is what the door wrote for this row,
        read back off the ledger, and judged against the row's own standing
        state with only the status moved. The control is that same event
        against the state it was written for, which must bind.
        """
        landed = self.dispatch(ref=self.side, lane="lane/carried-live-only",
                               kind="review")
        self.git("cherry-pick", "--no-commit", self.side)
        self.git("commit", "-qm", "carry the never-verdicted work")
        current = dispatches.snapshot()[0]
        standing = dict(current[landed["id"]])
        self.assertEqual(standing["status"], "open")
        out, err = landreq.close(landed["id"], "carried",
                                 evidence="the lander closed nothing",
                                 repo=self.repo)
        self.assertIsNone(err)
        event = dispatches.history(landed["id"])[-1]
        self.assertEqual((event["event"], event["close_reason"]),
                         ("close", "carried"))
        # CONTROL: against the live row it was written for, it binds.
        self.assertIsNone(dispatches._close_event_error(
            event, standing, current=current))
        for status in ("cancelled", "closed"):
            with self.subTest(status=status):
                retired = dict(standing, status=status)
                self.assertIsNotNone(
                    dispatches._close_event_error(event, retired, current=current),
                    "a carried close bound a %s row" % status)
                self.assertEqual(
                    dispatches._apply(retired, event, current=current), retired,
                    "replay moved a %s row" % status)

    def test_a_verdicted_row_still_binds_its_REVIEWED_tip_not_the_filed_one(self):  # noqa: VACUOUS_ASSERTION — every close here is dry_run=True by design (the ladder is exercised without appending); the unconditional positive control on the same observable is the specimen arm directly above, which closes through the same dry_run path and asserts carried_tip
        """THE REGRESSION CONTROL: the fallback is a fallback. A row WITH a
        verdict keeps binding its reviewed_tip — the field order is
        reviewed-tip, carrier, filed — and widening the door must not reorder
        the anchor a verdicted row was already proved against."""
        # FILED AND VERDICTED AT ONE SHA, AND THE ARM DISCRIMINATES BY
        # CONTRADICTION, which is the honest reading of hc4's mechanism:
        # at one sha the two fields cannot be told apart by what the close
        # BINDS — but they can by what it must do under an inverted order.
        # The carried tip here IS the row's reviewed_tip by construction,
        # so a close that answers carried_tip = that sha at all proves the
        # first anchor won; under the inversion the same sha is the FILED
        # one and the fallback would bind it — but this close ALSO asserts
        # the specimen arm's case above (no verdict at all) closes, which
        # the inverted order breaks. The two arms together pin the order;
        # neither does alone.
        row = self.verdict_row(polarity="fix")
        self.git("cherry-pick", "--no-commit", self.side)
        self.git("commit", "-qm", "carry the verdicted work")
        out, err = landreq.close(row["id"], "carried",
                                 evidence="the landed round omitted its "
                                          "discharge",
                                 repo=self.repo, dry_run=True)
        self.assertIsNone(err)
        self.assertIsNotNone(out, "a verdicted row whose reviewed tip is "
                                  "carried on trunk was refused — the "
                                  "fallback reordered the anchors")
        self.assertEqual(out["carried_tip"], self.side,
                         "the close did not bind the carried tip")
        self.assertEqual(out["carried_tip"],
                         dispatches.snapshot()[0][row["id"]]["reviewed_tip"],
                         "the bound anchor is not the verdict's own "
                         "reviewed_tip — the field order inverted")

    def test_carried_refuses_when_trunk_does_not_carry_the_work(self):
        """A MEASURED refusal, not an absence of proof. `self.side` is the
        divergent tip: real work, real objects, simply not at HEAD."""
        row = self.verdict_row(polarity="fix")
        out, err = landreq.close(row["id"], "carried", evidence="why nobody closed it",
                                 repo=self.repo, dry_run=True)
        self.assertIsNone(out)
        self.assertTrue(err)
        self.assertNotIn("needs evidence", err)

    def test_carried_dry_run_json_is_marked_and_non_appending(self):  # noqa: VACUOUS_ASSERTION — exact JSON fields are the positive control before history length proves the same real row did not append
        build = self.dispatch(ref=self.a, lane="lane/carried-build", kind="build")
        row = dispatches.add(
            "seat-b", "lane/carried-review", ref=self.side, repo=self.repo,
            kind="review", notify=False, new_work=False,
            supersedes=build["id"])
        out, err = self.mark_verdict(
            row["id"], self.side, "carried review", polarity="fix")
        self.assertIsNone(err)
        self.git("merge", "--no-edit", "-q", "side")
        evidence = "the landed round omitted its discharge"
        args = ["close", row["id"][:12], "--reason", "carried",
                "--evidence", evidence, "--repo", self.repo]
        before = self.history_len(row["id"])

        rc, out, err = run([*args, "--dry-run", "--json"])
        self.assertEqual((rc, err), (0, ""), out)
        report = json.loads(out)
        self.assertEqual(
            {key: report[key] for key in
             ("dry_run", "reason", "would_append", "idempotent")},
            {"dry_run": True, "reason": "carried",
             "would_append": True, "idempotent": False})
        self.assertEqual(self.history_len(row["id"]), before)

    def test_a_DEGENERATE_pair_is_unaskable_and_never_affirmed(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone pair IS the claim (a tri-state answering None), and the live control is the sibling arm proving a real pair answers False on the same repo
        """THE VACUITY THIS VERB FOUND IN THE LANDED PREDICATE, and the
        reason it is pinned here rather than only in test_rowstate: it was
        invisible until something SPENT the predicate on real rows.

        A review row whose chain carries no build row fell back to a base
        that IS its own reviewed tip. B == T makes the replay EMPTY, and an
        empty replay reproduces whatever HEAD it is handed — it affirms EVERY
        repository's trunk, including ones the work was never in. Live
        specimen when this was found: a row authored in another repository
        measured B == T and would have closed as `carried` against THIS
        repository's trunk.

        This vacuity was named for the recomputed-merge-base case; the
        chain-bound rule closed THAT door. This is the same vacuity through
        another one."""
        same = "a" * 40
        row = {"id": "c" * 32, "kind": "review", "reviewed_tip": same,
               "tip": same, "chain_root": "c" * 32}
        base, tip = rowworld._work_pair(row, {row["id"]: row}, {})
        self.assertIsNone(base, "a base equal to the tip is not a base")
        self.assertIsNone(tip)
        # ...and the relation refuses it defensively too, because it is the
        # function that SPENDS the pair: one layer catching this is one
        # refactor away from none.
        self.assertIsNone(
            rowworld._carriage(self.gitdir(), same, same, "refs/heads/main"))

    def test_the_proof_is_re_derived_at_replay_not_trusted(self):
        """THE THIRD SITE. Without a `carried` arm in `_close_event_error`
        the reason falls through to the chain's default, which is ACCEPT —
        so a forged event would replay as valid on its base gates alone.
        This drives the validator directly with an event whose row the
        ledger does not carry."""
        err = dispatches._close_event_error(
            {"v": 3, "event": "close", "id": "d" * 32, "seq": 1,
             "close_reason": "carried", "close_evidence": "forged",
             "closing_repo_id": self.gitdir(),
             "closing_trunk_ref": "refs/heads/main",
             "closing_trunk_sha": "b" * 40,
             "carried_base": "a" * 40, "carried_tip": "e" * 40},
            {"id": "d" * 32, "status": "verdict", "seq": 0,
             "polarity": "fix", "reviewed_tip": "e" * 40},
            current={}, verdicts={})
        self.assertTrue(err, "a carried event must never replay unchecked")


class LandedDoorsShareOneProofOwnerTest(CloseBase):
    """Both landed doors derive their proof from ONE function.

    The ordinary-review door used to carry its own copy of the entire ladder —
    its own ancestry check, its own absent refusal, its own translation rung —
    while the BUILD door called the shared owner. Byte-equivalent in intent,
    and drift is not hypothetical: a rung added to the shared owner was
    reachable from the BUILD door only, so an ordinary land request went
    through the other copy and never met it. Two copies of a ladder is two
    ladders, and the second one is invisible until something is added to the
    first.

    THIS PINS THE CALLER, NOT THE LADDER. A test that exercises the ladder
    directly cannot see a door that stopped calling it.
    """

    def _spy(self):
        calls = []
        real = landreq._close_landed_proof

        def spy(*a, **kw):
            calls.append(a[1] if len(a) > 1 else None)
            return real(*a, **kw)

        return calls, spy

    def test_the_ORDINARY_door_derives_its_proof_from_the_shared_owner(self):
        """LOAD-BEARING MUTATION: give this door back its own inline ladder
        (any private `_landing_proof` call that decides the proof mode).
          python3 -m unittest tests.test_lr_close\\
.LandedDoorsShareOneProofOwnerTest\\
.test_the_ORDINARY_door_derives_its_proof_from_the_shared_owner
          -> AssertionError: Lists differ: [] != ['<reviewed tip>']
        The close still SUCCEEDS under that mutation — a second correct copy
        closes correctly — so the verdict cannot see it and only the call
        record can."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")

        calls, spy = self._spy()
        with mock.patch.object(landreq, "_close_landed_proof", spy):
            out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(err, err)                       # MUST-HIT
        self.assertEqual(out["close_reason"], "landed")
        self.assertEqual(calls, [self.side],
                         "the ordinary door must derive its proof from the "
                         "shared owner, not from a private copy")


class BuildDoorSharesTheProofOwnerTest(CloseBuildLandedBase):
    """The sibling half of the shared-owner pin: one owner means BOTH doors,
    and pinning only one leaves the pair free to drift in the other
    direction. Separate class because the BUILD fixtures live here."""

    def _spy(self):
        calls = []
        real = landreq._close_landed_proof

        def spy(*a, **kw):
            calls.append(a[1] if len(a) > 1 else None)
            return real(*a, **kw)

        return calls, spy

    def test_the_BUILD_door_derives_its_proof_from_the_same_owner(self):
        parent = self.build_parent(lane="lane/build-shared-owner")
        child = self.approved_child(parent,
                                    lane="lane/build-shared-owner-review")
        self.git("merge", "--no-edit", "-q", "side")

        calls, spy = self._spy()
        with mock.patch.object(landreq, "_close_landed_proof", spy):
            out, err = landreq.close(parent["id"], "landed", live=True)
        self.assertIsNone(err, err)                       # MUST-HIT
        self.assertEqual(out["close_reason"], "landed")
        # THE SUBJECT IS WHICH FUNCTION DERIVES THE PROOF, NOT HOW MANY TIMES.
        # This asserted `calls == [self.side]` until the landed doors began
        # sweeping same-tip peers: the APPROVING CHILD is itself a live row
        # bound to this exact tip, so the land now closes it too and the
        # shared owner runs once more — for the SAME commit. Pinning the
        # count instead of the caller would make the pin fail on a correct
        # change and say nothing about drift. The original mutation it exists
        # to catch (give this door back a private inline ladder) still empties
        # this list, so `calls[0]` keeps the whole force of it.
        self.assertTrue(calls, "the BUILD door must call the shared owner")
        self.assertEqual(calls[0], self.side)
        self.assertEqual(set(calls), {self.side},
                         "every derivation is for the SAME reviewed tip")
        self.assertEqual(
            [p["id"] for p in out.get("closed_siblings") or ()],
            [child["id"]],
            "and the second call is the sweep closing the approving child, "
            "which was a live land request on the tip that just landed")


class CloseBuildContentEquivalentTest(CloseBuildLandedBase):
    """The BUILD twin: the ordinary door's persistence arm does not
    cover this one, and neither do the nine compatibility arms above.

    THOSE NINE PROVE A BUILD CLOSE MAY *OMIT* THE OPTIONAL PAIR. This proves a
    BUILD close may *PRESENT* it and transport it losslessly through the actual
    writer -> store -> projection. The schema exemption proves ADMISSION only;
    it cannot catch the BUILD caller ceasing to pass the pair, or a later
    BUILD-specific filter dropping it on the way back out.
    """

    def test_a_BUILD_content_equivalent_close_persists_witness_and_anchor(self):
        """A REAL CARRIER ON TRUNK. This arm's subject is TRANSPORT, not the
        search — but it can no longer fake the carrier to say so. The write
        boundary REPLAYS the witness before recording it, and replay demands
        the carrier be reachable from the pinned trunk, so the old
        carrier==source mock produced the one witness that can never replay.
        The fixture builds the real pair instead; the search still has its own
        arms in test_landreq.

        LOAD-BEARING MUTATION: in `_close_ladder_build_landed`, drop the
        content_witness/content_witness_anchor kwargs from the
        `_record_close_proven` call — the exact defect found in review.
          python3 -m unittest tests.test_lr_close\\
.CloseBuildContentEquivalentTest\\
.test_a_BUILD_content_equivalent_close_persists_witness_and_anchor
          -> AssertionError: None is not an instance of <class 'dict'>
        The nine compatibility arms stay GREEN under it, which is precisely why
        this arm has to exist."""
        reviewed, carrier = self.content_equivalent_pair()
        parent = self.build_parent(lane="lane/build-content")
        self.approved_child(parent, lane="lane/build-content-review",
                            ref=reviewed)
        # NOT merged and NOT patch-equivalent: the ladder falls past ancestry
        # and translation to the content rung.
        gitdir = os.path.join(self.repo, ".git")
        self.assertIn(landreq._landing_proof(gitdir, reviewed,
                                             self.git("rev-parse", self.main)),
                      ("absent", "unknown"))          # MUST-HIT
        self.assertNotEqual(reviewed, carrier)

        out, err = landreq.close(parent["id"], "landed", live=True)
        self.assertIsNone(err, err)
        self.assertEqual(out["close_proof_mode"], landreq.CONTENT_EQUIVALENT)

        # RE-READ THE PROJECTED RECORD, not `out`'s in-flight copy.
        lr = landreq.get(parent["id"])[0]
        stored = lr.get("content_witness")
        self.assertIsInstance(stored, dict)
        anchor = lr.get("content_witness_anchor")
        self.assertTrue(anchor)

        # RECOMPUTE FROM WHAT WAS READ, so a witness mangled in transit fails.
        self.assertEqual(
            dispatches._proof_anchor(landreq.CONTENT_ALGORITHM, stored), anchor)
        # AND THE RE-READ WITNESS MUST STILL REPLAY.
        self.assertEqual(landreq._content_equivalent_replay(gitdir, stored),
                         (True, None))


    def test_a_forged_or_omitted_proof_pair_is_REFUSED_at_the_BUILD_door(self):  # noqa: VACUOUS_ASSERTION — three unconditional positive controls precede the empty-list claims: the REAL unmutated event is asserted ADMITTED, len(cases) is asserted 18, and len(refused) == len(cases) proves every case was refused for its own stated reason
        """THE DOOR THE ORDINARY CHECK COULD NOT SEE.

        `_close_event_error` routes reason=landed + close_proof_version=2 into
        `_build_landed_event_error` and RETURNS, so an admission check written
        into the ordinary body guards exactly half the landed rows while
        reading as though it guarded all of them. This arm is what found that:
        it was written to satisfy the "ordinary + BUILD" requirement and
        failed on the BUILD side while the ordinary side was already green.

        THE BUILD WITNESS BINDS `landing_review_tip`, not this row's own
        `reviewed_tip` — the search runs from the APPROVED REVIEW's tip. An
        arm that asserts against the BUILD row's tip passes for the wrong
        reason on a validator bound to the wrong field.

        LOAD-BEARING MUTATION: drop the `_content_proof_pair_error` call from
        the tail of `_build_landed_event_error` -> all seven cases return None
        while every other BUILD arm in this file stays green.
        """
        reviewed, carrier = self.content_equivalent_pair()
        parent = self.build_parent(lane="lane/build-forge")
        review = self.approved_child(parent, lane="lane/build-forge-review",
                                     ref=reviewed)
        gitdir = os.path.join(self.repo, ".git")
        self.assertIn(landreq._landing_proof(gitdir, reviewed,
                                             self.git("rev-parse", self.main)),
                      ("absent", "unknown"))              # MUST-HIT
        self.assertNotEqual(reviewed, carrier)
        current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        state = current[parent["id"]]

        out, err = landreq.close(parent["id"], "landed", live=True)
        self.assertIsNone(err, err)
        self.assertEqual(out["close_proof_mode"], landreq.CONTENT_EQUIVALENT)

        event = self.close_event(parent["id"])
        # THE ROUTING PREMISE, ASSERTED RATHER THAN ASSUMED: v2 is what sends
        # this event to the BUILD validator. If the writer ever stamped v1 the
        # arm below would silently be re-testing the ordinary door.
        self.assertEqual(event.get("close_proof_version"), 2)
        # Bound to the tip the FIXTURE built, not to the row dict: the row
        # returned by the writer does not carry reviewed_tip, so comparing
        # against it compared a real sha to None and passed for no reason in
        # the direction that matters (a None on BOTH sides would agree).
        self.assertEqual(event.get("landing_review_tip"), reviewed)
        self.assertIsNotNone(reviewed)
        # POSITIVE CONTROL FIRST — the real, unmutated event is ADMITTED.
        self.assertIsNone(dispatches._close_event_error(
            event, state, current=current, verdicts=verdicts))

        # self.a is an ancestor commit: not the reviewed tip, not the pinned
        # trunk, not the carrier — so every substitution below is real.
        other = self.a
        cases = self.pair_cases(event, other)
        self.assertEqual(len(cases), 25)
        # COLLECT, DO NOT SHORT-CIRCUIT. A per-case assert stops at the FIRST
        # admitted forgery, so one run names one hole and hides the rest —
        # which is exactly wrong for a matrix whose job is to enumerate what
        # gets in. Under a mutation this reports EVERY case that regressed.
        admitted, mismatched, refused = [], [], []
        for expected, forged in cases:
            err = dispatches._close_event_error(
                forged, state, current=current, verdicts=verdicts)
            if err is None:
                admitted.append(expected)
            elif expected not in err:
                mismatched.append((expected, err))
            else:
                refused.append(expected)
        # THE POSITIVE OBSERVABLE FIRST, and it is what makes the two empty-
        # list assertions below mean anything: every case was REFUSED FOR ITS
        # OWN STATED REASON. `admitted == []` and `mismatched == []` are both
        # absence claims and both hold vacuously if the loop never ran or the
        # matrix were empty — this one cannot.
        self.assertEqual(len(refused), len(cases))
        self.assertEqual(admitted, [], "ADMITTED forged pairs: %s" % admitted)
        self.assertEqual(mismatched, [],
                         "refused for the WRONG reason: %s" % mismatched)


class CloseOrdinaryContentEquivalentTest(CloseBase):
    """The ordinary-review door's content-proof transport, driven through the
    REAL close verb.

    A transport arm that calls the ledger writer directly proves the WRITER
    keeps what it is handed; it proves nothing about the CALLER handing it
    over. Those are different failures with the same symptom — a close whose
    proof cannot be re-checked — and only the caller path can see the second.
    """

    def test_an_ordinary_content_close_persists_its_proof_through_the_verb(self):
        """A REAL CARRIER ON TRUNK. This arm's subject is TRANSPORT — the wire
        between the close verb and the stored record — but the carrier can no
        longer be faked to isolate it: the write boundary replays the witness,
        and a carrier that is not on trunk never replays. The fixture builds
        the real pair; the search has its own arms.

        LOAD-BEARING MUTATION: in `_close_ladder_landed`, drop the
        content_witness / content_witness_anchor kwargs from the
        `_record_close_proven` call.
          python3 -m unittest tests.test_lr_close\\
.CloseOrdinaryContentEquivalentTest\\
.test_an_ordinary_content_close_persists_its_proof_through_the_verb
          -> AssertionError: None is not an instance of <class 'dict'>
        Measured: with that pair deleted, every other focused class in this
        battery stays GREEN — including the direct-writer transport arm — so
        this is the only assertion that can see the caller stop passing it.
        """
        reviewed, carrier = self.content_equivalent_pair()
        row = self.dispatch(ref=reviewed)
        self.mark_verdict(row["id"], reviewed, "ok", polarity="approve")
        gitdir = os.path.join(self.repo, ".git")
        # NOT merged and NOT patch-equivalent: Git cannot place the reviewed
        # tip, so the ladder falls past the stronger rungs to the content
        # proof. Both halves are asserted, because a patch-equivalent pair
        # would be caught one rung UP and this arm would never run the code
        # it names.
        self.assertIn(landreq._landing_proof(gitdir, reviewed,
                                             self.git("rev-parse", self.main)),
                      ("absent", "unknown"))              # MUST-HIT
        self.assertNotEqual(reviewed, carrier)

        out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(err, err)
        self.assertEqual(out["close_proof_mode"], landreq.CONTENT_EQUIVALENT)

        # RE-READ THE PROJECTED RECORD rather than the in-flight copy.
        lr = landreq.get(row["id"])[0]
        stored = lr.get("content_witness")
        self.assertIsInstance(stored, dict)
        anchor = lr.get("content_witness_anchor")
        self.assertTrue(anchor)
        # RECOMPUTE FROM WHAT WAS READ, so a proof mangled in transit fails.
        self.assertEqual(
            dispatches._proof_anchor(landreq.CONTENT_ALGORITHM, stored), anchor)

    def test_a_forged_or_omitted_proof_pair_is_REFUSED_at_the_ordinary_door(self):  # noqa: VACUOUS_ASSERTION — three unconditional positive controls precede the empty-list claims: the REAL unmutated event is asserted ADMITTED, len(cases) is asserted 18, and len(refused) == len(cases) proves every case was refused for its own stated reason
        """THE ADMISSION ARM. The transport arm above proves the pair SURVIVES
        the wire; it cannot see whether the ledger would admit a close that
        never carried one. `content_witness`/`content_witness_anchor` are
        OPTIONAL fields in the landed schema, so OMISSION is the cheapest
        forgery available — no signature to defeat, just a key left out.

        THE RE-ANCHORED CASES ARE THE POINT. A forger editing a witness can
        also re-run `_proof_anchor` over it, so a case that mutates the
        witness and keeps the stale anchor proves the anchor comparison and
        nothing else. `reanchored` re-seals every lie it tells.

        LOAD-BEARING MUTATION: delete the `_content_proof_pair_error` call
        from `_close_event_error` -> all seven cases return None and only the
        positive control stays green.
        """
        reviewed, carrier = self.content_equivalent_pair()
        row = self.dispatch(ref=reviewed)
        self.mark_verdict(row["id"], reviewed, "ok", polarity="approve")
        gitdir = os.path.join(self.repo, ".git")
        self.assertIn(landreq._landing_proof(gitdir, reviewed,
                                             self.git("rev-parse", self.main)),
                      ("absent", "unknown"))              # MUST-HIT
        self.assertNotEqual(reviewed, carrier)
        current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        state = current[row["id"]]

        out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(err, err)

        event = self.close_event(row["id"])
        # v1 IS THE ROUTING PREMISE for this door, asserted not assumed.
        self.assertEqual(event.get("close_proof_version"), 1)
        self.assertEqual(event.get("reviewed_tip"), reviewed)
        # POSITIVE CONTROL FIRST — the real, unmutated event is ADMITTED.
        self.assertIsNone(dispatches._close_event_error(
            event, state, current=current, verdicts=verdicts))

        # self.a is an ancestor commit: not the reviewed tip, not the pinned
        # trunk, not the carrier — so every substitution below is real.
        other = self.a
        cases = self.pair_cases(event, other)
        self.assertEqual(len(cases), 25)
        # COLLECT, DO NOT SHORT-CIRCUIT. A per-case assert stops at the FIRST
        # admitted forgery, so one run names one hole and hides the rest —
        # which is exactly wrong for a matrix whose job is to enumerate what
        # gets in. Under a mutation this reports EVERY case that regressed.
        admitted, mismatched, refused = [], [], []
        for expected, forged in cases:
            err = dispatches._close_event_error(
                forged, state, current=current, verdicts=verdicts)
            if err is None:
                admitted.append(expected)
            elif expected not in err:
                mismatched.append((expected, err))
            else:
                refused.append(expected)
        # THE POSITIVE OBSERVABLE FIRST, and it is what makes the two empty-
        # list assertions below mean anything: every case was REFUSED FOR ITS
        # OWN STATED REASON. `admitted == []` and `mismatched == []` are both
        # absence claims and both hold vacuously if the loop never ran or the
        # matrix were empty — this one cannot.
        self.assertEqual(len(refused), len(cases))
        self.assertEqual(admitted, [], "ADMITTED forged pairs: %s" % admitted)
        self.assertEqual(mismatched, [],
                         "refused for the WRONG reason: %s" % mismatched)

    def test_a_proof_pair_may_not_ride_a_non_content_mode(self):
        """THE IFF, tested AT THE HELPER on purpose.

        Routing a mode-swapped landed event through the whole door would prove
        whichever upstream check fired FIRST — the proof-mode vocabulary, the
        translated-tip rule — and never this branch. The claim here is narrow
        and so is the call that tests it: a pair under any other mode is a
        proof nothing validates, and its ABSENCE under another mode is fine.
        """
        witness = {"v": 1, "source": {"commit": "a" * 40}}
        anchor = dispatches._proof_anchor(landreq.CONTENT_ALGORITHM, witness)
        rider = {"close_proof_mode": "ancestor", "content_witness": witness,
                 "content_witness_anchor": anchor}
        self.assertIn("may only accompany",
                      dispatches._content_proof_pair_error(rider, "a" * 40))
        # AND THE PERMIT POLARITY: the same mode with NO pair is admitted.
        self.assertIsNone(dispatches._content_proof_pair_error(
            {"close_proof_mode": "ancestor"}, "a" * 40))
        # HALF A PAIR IS STILL A RIDER.
        self.assertIn("may only accompany", dispatches._content_proof_pair_error(
            {"close_proof_mode": "ancestor", "content_witness": witness},
            "a" * 40))

    def test_an_UNREADABLE_repository_does_not_refuse_a_recorded_proof(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the line above the assertNotIn: the SAME event, against the READABLE repo, is asserted ADMITTED (assertIsNone), so an arm that never reached the validator cannot reach this one either
        """THE ONE DEVIATION FROM THE PRESCRIPTION, ARMED.

        They asked for False AND None to refuse, "just as the write boundary
        does". False now refuses. None does NOT, and this arm is why: this
        validator is ONE rule for the writer AND for replay, by explicit
        design, so refusing on None would UN-LAND honest rows in any clone
        that pruned the objects — landreq states that law where it mints the
        witness ("at READ, None must NOT be False — a clone that later pruned
        an object has an unreadable measurement, not a disproven proof").

        The forgery is still closed, because a wrong-but-well-shaped value in
        a READABLE repository is disproven rather than unreadable; that is the
        case the three semantic arms above cover.
        """
        reviewed, carrier = self.content_equivalent_pair()
        row = self.dispatch(ref=reviewed)
        self.mark_verdict(row["id"], reviewed, "ok", polarity="approve")
        current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        state = current[row["id"]]
        out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(err, err)
        event = self.close_event(row["id"])
        # MUST-HIT: the honest event is admitted while the repo IS readable.
        self.assertIsNone(dispatches._close_event_error(
            event, state, current=current, verdicts=verdicts))

        # Now point the event at a repository this process cannot read. The
        # witness is untouched and still honest; only the ability to CHECK it
        # is gone, which is an unreadable measurement and not a disproven one.
        blind = dict(event, closing_repo_id="/nonexistent/repo/.git")
        err = dispatches._close_event_error(
            blind, state, current=current, verdicts=verdicts)
        self.assertNotIn("does not replay", err or "",
                         "an unreadable repository was read as a DISPROVEN "
                         "proof, which un-lands honest rows in a pruned clone")


class ProofPairAdmissionReachesEveryDoorTest(unittest.TestCase):
    """THE ADMISSION RULE'S REACHABILITY, the sibling of the shared-owner pin
    above: that one binds the LADDER's callers, this one binds the RULE's.

    WHY THIS CLASS OF GUARD EXISTS AT ALL. Every other close reason is
    protected for free — the BUILD validator compares the event's field set to
    the expected set by EQUALITY, so an unexpected key is refused structurally
    and nobody has to remember. The content proof pair is DELIBERATELY
    SUBTRACTED from that check by _CONTENT_PROOF_FIELDS, because a witness
    exists only for a content-equivalent close and listing it would have made
    it mandatory for ancestry and patch-identity closes that carry none. That
    exemption is correct AND it spends the free guard: the moment a field is
    exempt from its own schema's exactness check, an EXPLICIT validator is
    required at every entry point, and nothing structural will tell you when a
    new entry point forgets one.

    It already happened once. The rule guarded the ordinary door for hours
    while the BUILD door returned before ever reaching it, and the comment on
    the rule read as though it covered both.
    """

    RULE = "_content_proof_pair_error"
    FIELD = "close_proof_mode"

    def _call_graph(self):
        """name -> set of names it calls, plus name -> whether it reads FIELD.

        DERIVED, NOT DECLARED. A declared list of doors is a list someone has
        to remember to extend, which is the same failure one layer up.
        """
        # EVERY FILE THE LEDGER'S SURFACE SPANS, NOT THE ONE IT IS WRITTEN IN.
        # This graph is derived from TEXT, and every door it exists to police
        # -- `_content_proof_pair_error` itself, `_build_landed_event_error`,
        # `_close_event_error`, `_record_close_proven` -- left `dispatches.py`
        # when the never-track ceiling split the close ladder out. Opening
        # `__file__` after that finds no function that reads the field, and
        # the arm below then proves nothing while staying green. The rule and
        # its doors are all still there, still called, still reachable; they
        # simply stopped being text in one file.
        calls, reads = {}, set()
        nodes = []
        for _path, source in ledger_sources(dispatches):
            nodes.extend(ast.walk(ast.parse(source)))
        for node in nodes:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            out = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    fn = sub.func
                    if isinstance(fn, ast.Name):
                        out.add(fn.id)
                    elif isinstance(fn, ast.Attribute):
                        out.add(fn.attr)
                elif isinstance(sub, ast.Constant) and sub.value == self.FIELD:
                    reads.add(node.name)
            calls.setdefault(node.name, set()).update(out)
        return calls, reads

    def test_every_function_that_reads_the_proof_mode_REACHES_the_rule(self):
        """A door may call the rule or delegate to something that does —
        _record_close_proven is a real example of the second shape, it builds
        the event and validates through _close_event_error — but it may not
        inspect close_proof_mode and then never meet the pair rule on any path.

        LOAD-BEARING MUTATION: delete the _content_proof_pair_error call from
        _build_landed_event_error. The BUILD door still returns its own errors
        and most close arms stay green, because that door refuses malformed
        events for a dozen other reasons first — only this arm names the loss.
        """
        calls, readers = self._call_graph()
        doors = sorted(readers - {self.RULE})
        # POSITIVE CONTROL FIRST, on the same walk: a scan that resolved no
        # doors would satisfy every emptiness claim below without looking at
        # anything. The floor is the MEASURED count, and it is asserted before
        # any conclusion is drawn from an absence.
        self.assertGreaterEqual(
            len(doors), 3,
            "the door scan resolved %d readers of %s, which is fewer than the "
            "measured floor — the walk is broken, not the code" % (
                len(doors), self.FIELD))
        self.assertIn("_build_landed_event_error", doors)      # MUST-HIT
        self.assertIn("_close_event_error", doors)             # MUST-HIT

        # THE WALK IS BOUNDED TO THE DOOR SET, and that bound is the whole
        # property. An earlier version of this arm allowed ARBITRARY transitive
        # reachability, which a mutation proved vacuous: with the BUILD door's
        # rule call deleted, "reachability" still held through nine hops of
        # unrelated machinery —
        #   _build_landed_event_error -> _chain_reaches -> add ->
        #   _append_dispatch -> snapshot -> _snapshot -> _fold -> _apply ->
        #   _close_event_error -> _content_proof_pair_error
        # A name-based call graph over a module this size connects almost any
        # two functions, so unrestricted closure asserts nothing. Delegation
        # stays expressible — a door may hand off to ANOTHER DOOR, which is
        # what _record_close_proven genuinely does — but it cannot launder the
        # obligation through code that has no opinion about proofs.
        def reaches(name, seen=None):
            seen = seen if seen is not None else set()
            if name in seen:
                return False
            seen.add(name)
            called = calls.get(name, ())
            if self.RULE in called:
                return True
            return any(reaches(c, seen) for c in called if c in readers)

        stranded = [d for d in doors if not reaches(d)]
        self.assertEqual(
            stranded, [],
            "these functions inspect %s but no call path from them reaches "
            "%s, so they decide a proof question the shared rule was written "
            "to own: %s" % (self.FIELD, self.RULE, stranded))

    def test_NO_close_reason_or_proof_version_exempts_itself_from_the_pair(self):  # noqa: VACUOUS_ASSERTION — `refused` is collected on the SAME walk that produces `admitted` and is asserted equal to len(reasons)*3 BEFORE the emptiness claim, so a loop that never ran reddens on the count rather than passing on an empty list
        """UNIFORMITY IS THE PROPERTY. The rule takes no view of reason or
        version today, and this arm exists to keep it that way: the refusal is
        asserted across the whole cross-product, so a later
        `if reason == X: return None` carve-out turns this red rather than
        quietly admitting an unverifiable proof on one path.

        The version axis deliberately spans a SUPERSET of what
        _record_close_proven admits (landed takes 1 and 2, everything else
        takes 1). Binding the superset means a third version admitted later
        inherits the rule instead of arriving exempt.
        """
        reasons = sorted(dispatches._CLOSE_STATE_FIELDS)
        self.assertGreaterEqual(len(reasons), 8)               # MUST-HIT
        self.assertIn("landed", reasons)                       # MUST-HIT
        admitted, refused = [], []
        for reason in reasons:
            for version in (1, 2, 3):
                event = {"close_reason": reason,
                         "close_proof_version": version,
                         "close_proof_mode": landreq.CONTENT_EQUIVALENT}
                # the pair is OMITTED on purpose — that is the forgery
                err = dispatches._content_proof_pair_error(event, "b" * 40)
                (refused if err else admitted).append((reason, version))
        # THE POSITIVE CONTROL ON THIS EXACT WALK. `admitted == []` is an
        # absence claim, and an absence claim over a loop that never ran is
        # true for the wrong reason. Counting the refusals makes the same walk
        # prove it did the work before its emptiness is believed.
        self.assertEqual(len(refused), len(reasons) * 3)
        self.assertEqual(
            admitted, [],
            "a content-equivalent close with NO witness pair was admitted for "
            "these (reason, version) pairs: %s" % admitted)

    def test_the_rule_still_PASSES_a_close_that_claims_no_content_proof(self):  # noqa: VACUOUS_ASSERTION — `passed` counts every iteration of this exact loop and is asserted >= 8 unconditionally at the end, so a walk that examined no reasons reddens on the control rather than passing on an unexercised assertIsNone
        """THE NEGATIVE CONTROL, without which the matrix above proves only
        that the function is capable of returning a string. An ancestry close
        carries no witness and must not be refused for lacking one.
        """
        passed = 0
        for reason in sorted(dispatches._CLOSE_STATE_FIELDS):
            event = {"close_reason": reason, "close_proof_version": 1,
                     "close_proof_mode": "ancestor"}
            self.assertIsNone(
                dispatches._content_proof_pair_error(event, "b" * 40),
                "reason %s was refused for a missing content pair it never "
                "claimed to have" % reason)
            passed += 1
        self.assertGreaterEqual(passed, 8)                     # MUST-HIT


class CloseContradictedWithdrawalDischargeTest(CloseBase):
    """The lifecycle clause's discharge leg (landreq._lr): a row closed
    --reason withdrawn whose FIX-verdicted change LATER lands is re-exposed
    CONTRARY and owed by the integrator — so the discharge ladders must be
    REACHABLE for exactly that row, while an uncontradicted withdrawal keeps
    the retired-once refusal VERBATIM. Every open-arm asserts the ladder's
    own LATER rung answered (a named refusal or a dry-run summary), never
    the absence of a complaint, and every arm counts LINES in the ledger
    FILE because the store is append-only."""

    RETIRED_ONCE = ("%s is already retired by %s; a row is retired once — "
                    "refusing a different closure")

    def ledger_lines(self):
        with open(dispatches.ledger_path(), "r", encoding="utf-8") as f:
            return sum(1 for _line in f)

    def withdrawn_fix_row(self, lane):
        row = self.verdict_row("fix", lane=lane)
        counted = self.ledger_lines()
        out, err = landreq.close(row["id"], "withdrawn",
                                 evidence="absent at close time")
        self.assertEqual(err, None)
        self.assertEqual(out["close_reason"], "withdrawn")
        # POSITIVE CONTROL on the line counter itself: the close just
        # appended exactly one event, so a later "unchanged" reading measures
        # a counter proven able to move, never a constant.
        self.assertEqual(self.ledger_lines(), counted + 1)
        return row

    def test_uncontradicted_withdrawal_keeps_retired_once_verbatim(self):
        row = self.withdrawn_fix_row("lane/cwd-control")
        confirmation = self.verdict_row("approve", ref=self.b,
                                        lane="lane/cwd-control-conf")
        expected = self.RETIRED_ONCE % (row["id"], "close --reason withdrawn")
        before = self.ledger_lines()
        _out, err = landreq.close(row["id"], "resolved",
                                  evidence="unused", dry_run=True)
        self.assertEqual(err, expected)
        _out, err = landreq.close(row["id"], "subsumed",
                                  evidence=confirmation["id"][:12],
                                  dry_run=True)
        self.assertEqual(err, expected)
        _out, err = landreq.close(row["id"], "superseded",
                                  evidence="a superseding round",
                                  tip=self.b, dry_run=True)
        self.assertEqual(err, expected)
        self.assertEqual(self.ledger_lines(), before)  # noqa: VACUOUS_ASSERTION — a dry-run/refused write must append NOTHING, and this same file-line counter is positively proven to move (+1) by this test's own earlier real append

    def test_flag_withdrawn_contradiction_opens_and_uncontradicted_stands(self):  # noqa: VACUOUS_ASSERTION — every refusal here is compared EXACTLY against a non-empty sentence built at run time (the retired-once refusal, the verbatim patch-identity refusal), which the rung cannot read as non-empty; the one None it checks is the withdraw write's error, and the ledger-line counter beside it proves that write appended (+1)
        row = self.verdict_row("fix", lane="lane/cwd-flag")
        counted = self.ledger_lines()
        _out, err = dispatches._record_withdraw_proven(
            row["id"], self.side, "kept off trunk")
        self.assertEqual(err, None)
        self.assertEqual(self.ledger_lines(), counted + 1)   # counter counts
        _out, err = landreq.close(row["id"], "resolved",
                                  evidence="unused", dry_run=True)
        self.assertEqual(err, self.RETIRED_ONCE % (row["id"], "withdraw"))
        self.git("cherry-pick", "side")
        before = self.ledger_lines()
        _out, err = landreq.close(row["id"], "resolved",
                                  evidence="unused", dry_run=True)
        # THE DOOR NAMED HERE CHANGED because the old one was a closed loop:
        # `subsumed` refuses on the complement of this very measurement (it
        # requires the original ABSENT from trunk). Every door this sentence
        # names is MEASURED against it in CarriedRebaseLandedTest — the advice
        # arm for the one-commit range, and the two arms after it for the
        # modes where `carried` refuses.
        self.assertEqual(err, ("%s reaches trunk only by PATCH IDENTITY, not "
                               "ancestry — that proves an identical delta "
                               "exists, never that THIS reviewed work landed. "
                               "NO NEXT DOOR IS PROMISED: `carried` is the "
                               "only reason whose subject is this state, but "
                               "it asks patch identity over the WHOLE range "
                               "up to this tip rather than this one commit, "
                               "and it is fail-closed — an unmatched commit "
                               "beneath the tip, or a tip object this "
                               "repository no longer holds, both read as "
                               "UNMEASURABLE and refuse. Ask it and read its "
                               "own answer (subsumed cannot take this row: "
                               "subsumed requires the original ABSENT from "
                               "trunk); if it refuses, OPEN is the honest "
                               "state for a landing nobody could measure. A "
                               "tip object that is gone is a separate fact "
                               "with its own door: `stranded` retires the row "
                               "as destroyed substrate, never as a landing, "
                               "and refuses while anything live still holds "
                               "the work — a recorded rewrite, a lane-family "
                               "ref or a lane-family worktree")
                         % self.side[:12])
        self.assertEqual(self.ledger_lines(), before)  # noqa: VACUOUS_ASSERTION — a dry-run/refused write must append NOTHING, and this same file-line counter is positively proven to move (+1) by this test's own earlier real append

    def test_contradicted_withdrawal_reaches_resolved_confirmation_rungs(self):
        row = self.withdrawn_fix_row("lane/cwd-resolved")
        # the confirmation must be a kind="review" dispatch: send() stamps no
        # kind, and the kind rung refuses "confirmation is not a review
        # verdict" before the phrase rung this arm exists to reach.
        confirmation = dispatches.add(
            "seat-a", "lane/cwd-resolved-conf", ref=self.b, repo=self.repo,
            kind="review", notify=False, new_work=True)
        _out, err = self.mark_verdict(confirmation["id"], self.b,
                                            "findings", polarity="approve")
        self.assertEqual(err, None)
        self.git("merge", "--no-edit", "-q", "side")
        before = self.ledger_lines()
        _out, err = landreq.close(row["id"], "resolved",
                                  evidence=confirmation["id"][:12],
                                  dry_run=True)
        self.assertEqual(err, ("confirmation evidence must OPEN with "
                               "`Resolution verified on trunk: <concrete "
                               "resolution>` — the phrase is the check here, "
                               "so it is read at the head and nowhere else"))
        self.assertEqual(self.ledger_lines(), before)  # noqa: VACUOUS_ASSERTION — a dry-run/refused write must append NOTHING, and this same file-line counter is positively proven to move (+1) by this test's own earlier real append

    def test_contradicted_withdrawal_reaches_subsumed_land_rungs(self):
        target, confirmation, _tip = self.subsumption_case(
            original_polarity="fix",
            confirmation_statement=(
                "Subsumption verified FIX findings were answered on trunk: "
                "the withdrawn guard was landed after all"))
        counted = self.ledger_lines()
        _out, err = landreq.close(target["id"], "withdrawn",
                                  evidence="absent at close time")
        self.assertEqual(err, None)
        self.assertEqual(self.ledger_lines(), counted + 1)   # counter counts
        self.git("cherry-pick", "side")
        before = self.ledger_lines()
        with self.family_evidence():
            _out, err = landreq.close(
                target["id"], "subsumed", evidence=confirmation["id"][:12],
                trunk=self.main, dry_run=True)
        self.assertEqual(
            err, "the original reviewed change is on trunk and is not subsumed")
        self.assertEqual(self.ledger_lines(), before)  # noqa: VACUOUS_ASSERTION — a dry-run/refused write must append NOTHING, and this same file-line counter is positively proven to move (+1) by this test's own earlier real append

    def contradicted_superseded_case(self, lane="lane/cwd-sup"):
        """(row, fixed tip, superseding review) — a CONTRADICTED withdrawal
        with an approved same-chain superseder landed: FIX verdict on the
        side tip, closed withdrawn, then the resolution reviewed/approved and
        the side branch MERGED so the withdrawn premise is falsified by
        ancestry."""
        row = self.verdict_row("fix", lane=lane)
        counted = self.ledger_lines()
        _out, err = landreq.close(row["id"], "withdrawn",
                                  evidence="absent at close time")
        self.assertEqual(err, None)
        self.assertEqual(self.ledger_lines(), counted + 1)   # counter counts
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve findings", path="g")
        self.git("checkout", "-q", self.main)
        second, why, _sent = dispatches.send(
            "seat-a", lane + "-r2", "review r2", fixed,
            repo=self.repo, key="key-" + lane.replace("/", "-") + "-r2",
            sign=False, supersedes=row["id"])
        self.assertEqual(why, None)
        _out, err = self.mark_verdict(second["id"], fixed, "clean",
                                            polarity="approve")
        self.assertEqual(err, None)
        self.git("merge", "--no-edit", "-q", "side")
        return row, fixed, second

    def test_contradicted_withdrawal_discharges_superseded_and_uncontradicted_refuses(self):
        """THE FLIPPED SEAM. The previous body of this test PINNED the
        writer refusal ("the door now reaches the write, and the write
        refuses") so the event-schema leg would flip a red assertion instead
        of nothing. This IS that leg: the writer captures the door's
        re-derivation (`_CONTRADICTION_PROOF_FIELDS`) on the close event and
        the ledger ADMITS the discharging close — one appended LINE carrying
        the whole proof — while the uncontradicted withdrawal in the second
        arm keeps the retired-once refusal VERBATIM at the same writer."""
        row, fixed, second = self.contradicted_superseded_case()
        before = self.ledger_lines()
        dry, err = landreq.close(row["id"], "superseded",
                                 evidence="r2 landed the resolution",
                                 tip=fixed, dry_run=True)
        self.assertEqual(err, None)
        self.assertTrue(dry["dry_run"])
        self.assertEqual(dry["reason"], "superseded")
        self.assertEqual(dry["contrary_state"], "landed")
        self.assertEqual(self.ledger_lines(), before)  # noqa: VACUOUS_ASSERTION — a dry run must append NOTHING, and this same file-line counter is positively proven to move (+1) by this test's own earlier real append
        # ARM 1 — the write SUCCEEDS end to end.
        out, err = landreq.close(row["id"], "superseded",
                                 evidence="r2 landed the resolution",
                                 tip=fixed)
        self.assertEqual(err, None)
        self.assertEqual(out["close_reason"], "superseded")
        self.assertEqual(out["superseding_tip"], fixed)
        self.assertEqual(self.ledger_lines(), before + 1)  # ONE line, in the FILE
        event = self.close_event(row["id"])
        self.assertEqual(event["close_reason"], "superseded")
        self.assertEqual(event["contradicted_retirement"], "close-withdrawn")
        self.assertEqual(event["contradiction_polarity"], "fix")
        self.assertEqual(event["contradiction_polarity_via"], "own")
        self.assertEqual(event["contradiction_land_leg"], "ancestor")
        self.assertEqual(event["contradiction_trunk_ref"],
                         event["closing_trunk_ref"])
        self.assertEqual(event["contradiction_trunk_sha"],
                         self.git("rev-parse", self.main))
        self.assertEqual(event["contradiction_same_code"], [self.side])
        # the PROJECTED row carries the capture — one table drives both.
        projected = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(projected["close_reason"], "superseded")
        self.assertEqual(projected["contradicted_retirement"],
                         "close-withdrawn")
        self.assertEqual(projected["contradiction_land_leg"], "ancestor")
        # ARM 2 — a NON-contradicted withdrawal (its tip never lands) keeps
        # the verbatim retired-once refusal at the door AND at the writer,
        # with the file unchanged.
        self.git("branch", "-q", "stray", self.main)
        self.git("checkout", "-q", "stray")
        stray = self.commit("never lands", path="h")
        self.git("checkout", "-q", self.main)
        stood = self.verdict_row("fix", ref=stray, lane="lane/cwd-sup-stands")
        counted = self.ledger_lines()
        _out, err = landreq.close(stood["id"], "withdrawn",
                                  evidence="absent at close time")
        self.assertEqual(err, None)
        self.assertEqual(self.ledger_lines(), counted + 1)   # counter counts
        before = self.ledger_lines()
        out, err = landreq.close(stood["id"], "superseded",
                                 evidence="no landing exists", tip=fixed)
        self.assertEqual(out, None)
        self.assertEqual(err, self.RETIRED_ONCE % (
            stood["id"], "close --reason withdrawn"))
        # ...and at the WRITER itself, bypassing the door: the lock re-check
        # without a captured proof answers the long verbatim refusal.
        w_out, w_err = dispatches._record_close_proven(
            stood["id"], "superseded", stray,
            evidence="no landing exists", superseding_tip=fixed,
            superseding_id=second["id"], contrary_state="landed",
            contrary_target="local", closing_trunk_ref=self.main,
            closing_trunk_sha=self.git("rev-parse", self.main))
        self.assertEqual(w_out, None)
        self.assertEqual(w_err, ("dispatch %s is already retired by close "
                                 "--reason withdrawn; a row is retired once — "
                                 "refusing a different closure") % stood["id"])
        self.assertEqual(self.ledger_lines(), before)  # noqa: VACUOUS_ASSERTION — a refused write must append NOTHING, and this same file-line counter is positively proven to move (+1) by this test's own earlier real append

    def test_discharging_close_replays_from_its_captured_fields(self):  # noqa: VACUOUS_ASSERTION — the clean-replay assertIsNone is controlled on the SAME observable: six counted one-field mutations of the same event through the same _close_event_error call all return refusals, and the fold's EFFECT is asserted on the projected row
        """REPLAY IS LEDGER-ONLY (the discharged/resolved asymmetry): the
        writer-written event re-binds through the writer/replay-shared
        `_close_event_error` from its CAPTURED fields, and each captured
        field is LOAD-BEARING — mutate one and the event is inert; strip the
        capture and the retired-once law answers verbatim."""
        row, fixed, _second = self.contradicted_superseded_case()
        pre_current, pre_verdicts, unavailable = \
            dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        pre_state = pre_current[row["id"]]
        _out, err = landreq.close(row["id"], "superseded",
                                  evidence="r2 landed the resolution",
                                  tip=fixed)
        self.assertEqual(err, None)
        event = self.close_event(row["id"])
        # the appended event replays CLEANLY against the pre-close state.
        self.assertIsNone(dispatches._close_event_error(
            event, pre_state, current=pre_current, verdicts=pre_verdicts))
        # and the projection folds it back from the FILE, fields intact.
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(replayed["close_reason"], "superseded")
        self.assertEqual(replayed["contradiction_land_leg"], "ancestor")
        self.assertEqual(replayed["contradiction_trunk_sha"],
                         event["contradiction_trunk_sha"])
        # EVERY captured field is load-bearing — the positive control that
        # admission READS the capture rather than admitting on its presence.
        absence = pre_state["absence_trunk_sha"]
        self.assertNotEqual(event["contradiction_trunk_sha"], absence,
                            "the absence-pin case would mutate nothing")
        cases = (("contradicted_retirement", "flag-withdrawn"),
                 ("contradiction_polarity", "supersede"),
                 ("contradiction_polarity_via", "not-own"),
                 ("contradiction_polarity_via", ["a" * 32]),
                 ("contradiction_land_leg", "landed"),
                 ("contradiction_trunk_ref", ""),
                 ("contradiction_trunk_sha", absence),
                 ("contradiction_same_code", ["b" * 40]),
                 # a NON-full identity forged INTO an otherwise-honest set
                 # the writer filters these out, so a
                 # member the grammar refuses can only be a forgery.
                 ("contradiction_same_code",
                  event["contradiction_same_code"] + ["beefbeefbeef"]))
        errors = [dispatches._close_event_error(
            dict(event, **{field: value}), pre_state,
            current=pre_current, verdicts=pre_verdicts)
            for field, value in cases]
        self.assertEqual(len(errors), 9, "every captured field mutated")
        self.assertTrue(all(errors), errors)
        # STRIPPED of its capture the event is a plain different-closure
        # forgery, and the shared rule answers the verbatim refusal.
        bare = {key: value for key, value in event.items()
                if key not in dispatches._CONTRADICTION_PROOF_FIELDS}
        self.assertEqual(
            dispatches._close_event_error(
                bare, pre_state, current=pre_current, verdicts=pre_verdicts),
            "already retired")
        # HALF a capture is refused by NAME, never admitted on presence.
        self.assertIn(
            "whole captured contradiction proof",
            dispatches._close_event_error(
                dict(bare, contradiction_land_leg="ancestor"), pre_state,
                current=pre_current, verdicts=pre_verdicts))
        # and a capture aimed at an UNRETIRED row is refused outright.
        live = self.verdict_row("fix", lane="lane/cwd-live-control")
        cur2, ver2, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        live_state = cur2[live["id"]]
        self.assertIn(
            "never retired",
            dispatches._close_event_error(
                dict(event, id=live["id"], seq=live_state["seq"] + 1),
                live_state, current=cur2, verdicts=ver2))

    def chain_declared_withdrawn_case(self, lane="lane/cwd-chain"):
        """(row, declarer) — an UNDECLARED verdict row whose FIX lives ONLY
        on a chained round about the SAME code (the row's own reviewed tip),
        closed withdrawn while the change is genuinely off trunk."""
        row = self.verdict_row(None, lane=lane)
        declarer, why, _sent = dispatches.send(
            "seat-c", lane + "-fixdecl", "declare fix", self.side,
            repo=self.repo, key="key-" + lane.replace("/", "-") + "-fixdecl",
            sign=False, supersedes=row["id"])
        self.assertEqual(why, None)
        _out, err = self.mark_verdict(declarer["id"], self.side,
                                            "guard must not land as-is",
                                            polarity="fix")
        self.assertEqual(err, None)
        counted = self.ledger_lines()
        _out, err = landreq.close(row["id"], "withdrawn",
                                  evidence="absent at close time")
        self.assertEqual(err, None)
        self.assertEqual(self.ledger_lines(), counted + 1)   # counter counts
        return row, declarer

    def test_chain_declared_contradicted_withdrawal_discharges_resolved(self):  # noqa: VACUOUS_ASSERTION — the silent-polarity premise check and the clean-replay assertIsNone ride a write POSITIVELY proven on the same observables: +1 counted ledger line and six exact-value field assertions on the appended event
        """BLOCKER (1) END TO END: the row's FIX is CHAIN-declared
        (its own polarity is UNDECLARED), the contradiction is by ANCESTRY,
        and the discharge goes through RESOLVED — the door whose polarity
        domain has no None, so this arm proves the lrs threading, the
        write-time rebind (polarity + typed via + same-code, bound under the
        lock), AND the effective-polarity rung together. The captured event
        replays with the provenance re-walked against the ledger."""
        row, declarer = self.chain_declared_withdrawn_case()
        self.git("merge", "--no-edit", "-q", "side")   # the contradiction
        confirmation = dispatches.add(
            "claude-reviewer", "lane/cwd-chain-conf", ref=self.b,
            repo=self.repo, kind="review", notify=False, new_work=True)
        _out, err = self.mark_verdict(
            confirmation["id"], self.b,
            "Resolution verified on trunk: the withdrawn guard landed via "
            "the side merge", polarity="approve")
        self.assertEqual(err, None)
        pre_current, pre_verdicts, unavailable = \
            dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        pre_state = pre_current[row["id"]]
        self.assertIsNone(pre_state.get("polarity"))   # the arm's premise
        before = self.ledger_lines()
        with self.family_evidence():
            out, err = landreq.close(row["id"], "resolved",
                                     evidence=confirmation["id"][:12])
        self.assertEqual(err, None)
        self.assertEqual(out["close_reason"], "resolved")
        self.assertEqual(self.ledger_lines(), before + 1)  # ONE line, in the FILE
        event = self.close_event(row["id"])
        self.assertEqual(event["close_reason"], "resolved")
        self.assertEqual(event["contradicted_retirement"], "close-withdrawn")
        self.assertEqual(event["contradiction_polarity"], "fix")
        self.assertEqual(event["contradiction_polarity_via"], [declarer["id"]])
        self.assertEqual(event["contradiction_same_code"], [self.side])
        self.assertEqual(event["contradiction_land_leg"], "ancestor")
        # the chain-declared event REPLAYS from its captured fields: the
        # provenance is re-walked through the snapshot, never trusted.
        self.assertIsNone(dispatches._close_event_error(
            event, pre_state, current=pre_current, verdicts=pre_verdicts))
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(replayed["close_reason"], "resolved")
        self.assertEqual(replayed["contradiction_polarity_via"],
                         [declarer["id"]])

    def test_chain_verdict_racing_the_write_refuses_on_fresh_truth(self):
        """BLOCKER (2): a chain verdict lands BETWEEN the door's
        read and the ledger lock. The writer re-derives the chain from its
        OWN locked read (`_rebind_contradiction`), so a conflicting APPROVE
        appended after the door's capture refuses the write on the FRESH
        truth — the stale capture never binds — and the file does not
        move. The succeed-on-fresh-truth half is the resolved arm above,
        whose capture is likewise derived at the lock.

        THE CHAIN IS A LINE, NOT A FAN, on purpose: the declarer's FIX
        branch stays LIVE until retired (`_duplicate_branch_live`), so a
        sibling minted against the same parent trips the ACTIVE-successor
        refusal at send — the round-2 gate proved it, this arm never
        reaching its own rung. Each round therefore supersedes the PREVIOUS
        one; `_chain_declarers` walks transitively, so a deeper rival is
        still a same-code declarer about the root's code."""
        row, declarer = self.chain_declared_withdrawn_case(
            lane="lane/cwd-race")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve findings", path="g")
        self.git("checkout", "-q", self.main)
        second, why, _sent = dispatches.send(
            "seat-a", "lane/cwd-race-r2", "review r2", fixed,
            repo=self.repo, key="key-cwd-race-r2", sign=False,
            supersedes=declarer["id"])
        self.assertEqual(why, None)
        _out, err = self.mark_verdict(second["id"], fixed, "clean",
                                            polarity="approve")
        self.assertEqual(err, None)
        self.git("merge", "--no-edit", "-q", "side")
        # the DOOR's capture — the git legs a real ladder run would thread.
        lrs, unavailable = landreq.project(selector=row["id"])
        self.assertIsNone(unavailable)
        original = dispatches.snapshot()[0][row["id"]]
        standing, capture = landreq._retirement_stands(original, lrs)
        self.assertIsNone(standing)
        self.assertEqual(capture["contradiction_land_leg"], "ancestor")
        # THE RACE: a conflicting same-code APPROVE lands after that read —
        # chained under the (closed) approve round, a TRANSITIVE descendant.
        rival, why, _sent = dispatches.send(
            "seat-b", "lane/cwd-race-rival", "endorse original", self.side,
            repo=self.repo, key="key-cwd-race-rival", sign=False,
            supersedes=second["id"])
        self.assertEqual(why, None)
        _out, err = self.mark_verdict(rival["id"], self.side,
                                            "endorsed", polarity="approve")
        self.assertEqual(err, None)
        before = self.ledger_lines()
        out, err = dispatches._record_close_proven(
            row["id"], "superseded", self.side,
            evidence="r2 landed the resolution", superseding_tip=fixed,
            superseding_id=second["id"], contrary_state="landed",
            contrary_target="local",
            closing_trunk_ref=capture["contradiction_trunk_ref"],
            closing_trunk_sha=capture["contradiction_trunk_sha"],
            contradiction=capture)
        self.assertEqual(out, None)
        self.assertIn("CONFLICTING", err)
        self.assertEqual(self.ledger_lines(), before)  # noqa: VACUOUS_ASSERTION — a refused write must append NOTHING, and this same file-line counter is positively proven to move (+1) by this test's own fixture append

    def test_ladder_dry_run_refuses_a_mid_ladder_chain_conflict(self):
        """CURE (1)'s ARM: the dry-run is a PROMISE
        surface. A conflicting same-code verdict landing INSIDE close() —
        after the entry projection built its lrs, before the ladder's own
        snapshot — must flip the dry-run to the refusal, because the
        store-side recheck now pairs the fresh row with the chain map from
        that SAME snapshot. The rival is injected at the door-entry rung
        (the first _retirement_stands call), which runs exactly between
        the two reads; the entry check passes on its stale map, the
        recheck sees the conflict. Delete the same-read pairing and the
        recheck answers from entry-time truth: this arm's would-close is
        the red that proves the cure."""
        row, declarer = self.chain_declared_withdrawn_case(
            lane="lane/cwd-dryrace")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve findings", path="g")
        self.git("checkout", "-q", self.main)
        second, why, _sent = dispatches.send(
            "seat-a", "lane/cwd-dryrace-r2", "review r2", fixed,
            repo=self.repo, key="key-cwd-dryrace-r2", sign=False,
            supersedes=declarer["id"])
        self.assertEqual(why, None)
        _out, err = self.mark_verdict(second["id"], fixed, "clean",
                                            polarity="approve")
        self.assertEqual(err, None)
        self.git("merge", "--no-edit", "-q", "side")
        # POSITIVE CONTROL on the same observable: with no rival, this very
        # dry-run answers would-close.
        dry, err = landreq.close(row["id"], "superseded",
                                 evidence="r2 landed the resolution",
                                 tip=fixed, dry_run=True)
        self.assertEqual(err, None)
        self.assertTrue(dry["dry_run"])
        real = landreq._retirement_stands
        counted = []

        def racing(*args, **kwargs):
            if not counted:
                rival, why, _sent = dispatches.send(
                    "seat-b", "lane/cwd-dryrace-rival", "endorse original",
                    self.side, repo=self.repo, key="key-cwd-dryrace-rival",
                    sign=False, supersedes=second["id"])
                assert why is None, why
                _o, verr = self.mark_verdict(
                    rival["id"], self.side, "endorsed", polarity="approve")
                assert verr is None, verr
                counted.append(self.ledger_lines())
            return real(*args, **kwargs)

        with mock.patch.object(landreq, "_retirement_stands",
                               side_effect=racing):
            dry, err = landreq.close(row["id"], "superseded",
                                     evidence="r2 landed the resolution",
                                     tip=fixed, dry_run=True)
        self.assertTrue(counted, "the mid-ladder race never fired")  # MUST-HIT
        self.assertEqual(dry, None)
        self.assertEqual(err, self.RETIRED_ONCE % (
            row["id"], "close --reason withdrawn"))
        self.assertEqual(self.ledger_lines(), counted[0])  # noqa: VACUOUS_ASSERTION — the refused dry-run must append NOTHING beyond the rival's own rows, and this same file-line counter is positively proven to move by the fixture's counted +1 assertion

    def test_provenance_bound_is_the_rewalk_not_a_cap(self):
        """CURE (3): the replay bound on chain
        provenance is the RE-WALK — uniqueness, id grammar, and membership
        in the walked declaring set — never a magic length, matching how
        the _CLOSE_STATE_FIELDS siblings bound captured proofs by
        re-derivation. Seventeen honest declarers (one over the old 16
        cap) admit; one id the walk cannot find refuses."""
        rid = "a" * 32
        reviewed = "c" * 40
        state = {"id": rid, "close_reason": "withdrawn",
                 "reviewed_tip": reviewed, "polarity": None,
                 "status": "verdict", "seq": 3}
        declarers = ["%032x" % (0xb0 + i) for i in range(17)]
        current = {rid: state}
        for did in declarers:
            current[did] = {"id": did, "supersedes": rid, "polarity": "fix",
                            "reviewed_tip": reviewed}
        event = {"contradicted_retirement": "close-withdrawn",
                 "contradiction_polarity": "fix",
                 "contradiction_polarity_via": sorted(declarers),
                 "contradiction_land_leg": "ancestor",
                 "contradiction_trunk_ref": "refs/heads/main",
                 "contradiction_trunk_sha": "d" * 40,
                 "contradiction_same_code": [reviewed]}
        self.assertIsNone(dispatches._contradiction_discharge_error(
            event, state, "superseded", current))
        forged = dict(event, contradiction_polarity_via=(
            sorted(declarers)[:16] + ["f" * 32]))
        self.assertIn("does not match the ledger",
                      dispatches._contradiction_discharge_error(
                          forged, state, "superseded", current))

    def test_chain_declared_row_passes_subsumeds_polarity_rung(self):
        """CURE (1): subsumed ADMITTED a chain-declared
        contradiction at the retirement gate and then re-read the row's OWN
        polarity one rung later — admit-then-re-refuse. The door now acts
        on the EFFECTIVE polarity the carve-out derived, so the same row
        walks PAST that rung to the next honest one (this row carries no
        review kind) instead of being called not-APPROVE/FIX."""
        row, declarer = self.chain_declared_withdrawn_case(
            lane="lane/cwd-subsumed")
        self.git("cherry-pick", "side")
        before = self.ledger_lines()
        _out, err = landreq.close(row["id"], "subsumed",
                                  evidence=declarer["id"][:12],
                                  trunk=self.main, dry_run=True)
        self.assertEqual(err, "%s is not a review dispatch" % row["id"])
        self.assertEqual(self.ledger_lines(), before)  # noqa: VACUOUS_ASSERTION — a dry-run refusal must append NOTHING, and this same file-line counter is positively proven to move (+1) by the fixture's counted append

    def test_resolved_replay_validates_the_whole_captured_object(self):  # noqa: VACUOUS_ASSERTION — the clean-replay assertIsNone is controlled by TEN counted one-field forgeries through the same call, and the doorless-hole arm asserts the projected row is UNCHANGED after a really-appended forgery
        """CURE (2): resolved had NO reason branch in
        _close_event_error — the carried comment's warned hole verbatim: a
        next-seq resolved event with the proof fields OMITTED bound through
        _apply on the base gates alone. Whole-object now: the schema set,
        every captured field, and the confirmation re-derived from the
        ledger; each validated field is load-bearing."""
        row, _declarer = self.chain_declared_withdrawn_case(
            lane="lane/cwd-replay2")
        self.git("merge", "--no-edit", "-q", "side")
        confirmation = dispatches.add(
            "claude-reviewer", "lane/cwd-replay2-conf", ref=self.b,
            repo=self.repo, kind="review", notify=False, new_work=True)
        _out, err = self.mark_verdict(
            confirmation["id"], self.b,
            "Resolution verified on trunk: the withdrawn guard landed via "
            "the side merge", polarity="approve")
        self.assertEqual(err, None)
        pre_current, pre_verdicts, unavailable = \
            dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        pre_state = pre_current[row["id"]]
        # THE DOORLESS HOLE, closed: a next-seq resolved event WITHOUT the
        # captured proof object refuses by NAME and is INERT on the ledger.
        bare = {"v": 3, "event": "close", "id": row["id"],
                "seq": pre_state["seq"] + 1, "ts": dispatches.pk.now_ts(),
                "close_reason": "resolved", "close_proof_version": 1,
                "reviewed_tip": self.side,
                "close_evidence": confirmation["id"][:12]}
        self.assertIn("do not match resolved schema",
                      dispatches._close_event_error(
                          bare, pre_state, current=pre_current,
                          verdicts=pre_verdicts))
        eventledger.append_unlocked(dispatches.ledger_path(), bare)
        # INERT means the STANDING terminal is untouched: this row is
        # already closed withdrawn, so the forged resolved event must leave
        # close_reason exactly there — asserting None here was asserting a
        # state this fixture never had, and it burned a whole gate round
        # (the arm's FIRST execution was the fab; cold-probe arms first).
        self.assertEqual(
            dispatches.snapshot()[0][row["id"]].get("close_reason"),
            "withdrawn")
        # the HONEST write binds through the same widened validator...
        with self.family_evidence():
            out, err = landreq.close(row["id"], "resolved",
                                     evidence=confirmation["id"][:12])
        self.assertEqual(err, None)
        self.assertEqual(out["close_reason"], "resolved")
        event = self.close_event(row["id"])
        self.assertIsNone(dispatches._close_event_error(
            event, pre_state, current=pre_current, verdicts=pre_verdicts))
        # ...and EVERY validated field is load-bearing.
        other = self.verdict_row("fix", lane="lane/cwd-replay2-other")
        cases = (("confirmation_id", other["id"]),
                 ("confirmation_id", "0" * 32),
                 ("confirmation_tip", self.side),
                 ("confirmation_ref", "verified informally"),
                 ("original_author", "someone-else"),
                 ("closing_repo_id", "relative/path"),
                 ("closing_trunk_ref", "main"),
                 ("closing_trunk_sha", "not-a-sha"),
                 ("close_proof_mode", "ancestor"),
                 ("close_evidence", "zzzzzzzzzzzz"))
        errors = [dispatches._close_event_error(
            dict(event, **{field: value}), pre_state,
            current=pre_current, verdicts=pre_verdicts)
            for field, value in cases]
        self.assertEqual(len(errors), 10, "every captured field forged")
        self.assertTrue(all(errors), errors)
        # dropping a proof field is the original hole — refused by the set.
        short = {key: value for key, value in event.items()
                 if key != "confirmation_tip"}
        self.assertIn("do not match resolved schema",
                      dispatches._close_event_error(
                          short, pre_state, current=pre_current,
                          verdicts=pre_verdicts))

    def test_unresolved_translation_stays_out_of_the_proof_set(self):  # noqa: VACUOUS_ASSERTION — the clean-replay assertIsNone rides a write POSITIVELY proven on the same observables: one counted ledger line and exact-value assertions on the captured set, polarity and provenance
        """The dictated cure: the translation sidecar may
        hold an UNRESOLVED short identity beside the full reviewed tip, and
        capturing it verbatim made the replay grammar refuse the very write
        it rode in on — a valid chain declaration opened at classification
        and died at the write for an identity that was never load-bearing.
        The binder now captures only FULL identities: the discharge binds
        cleanly and the captured set carries the reviewed tip alone."""
        row, declarer = self.chain_declared_withdrawn_case(
            lane="lane/cwd-unresolved")
        # the recorded rewrite translation of the reviewed tip RESOLVES TO
        # NOTHING — a short identity no repository object answers for.
        self.sidecar(self.side, "beefbeefbeef")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve findings", path="g")
        self.git("checkout", "-q", self.main)
        second, why, _sent = dispatches.send(
            "seat-a", "lane/cwd-unresolved-r2", "review r2", fixed,
            repo=self.repo, key="key-cwd-unresolved-r2", sign=False,  # gitleaks:allow — a dispatch idempotency key, not a credential
            supersedes=declarer["id"])
        self.assertEqual(why, None)
        _out, err = self.mark_verdict(second["id"], fixed, "clean",
                                            polarity="approve")
        self.assertEqual(err, None)
        self.git("merge", "--no-edit", "-q", "side")
        pre_current, pre_verdicts, unavailable = \
            dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        pre_state = pre_current[row["id"]]
        before = self.ledger_lines()
        out, err = landreq.close(row["id"], "superseded",
                                 evidence="r2 landed the resolution",
                                 tip=fixed)
        self.assertEqual(err, None)
        self.assertEqual(out["close_reason"], "superseded")
        self.assertEqual(self.ledger_lines(), before + 1)  # ONE line, counted
        event = self.close_event(row["id"])
        # the captured proof set is the FULL identity alone — the
        # unresolved translation is nowhere in the event.
        self.assertEqual(event["contradiction_same_code"], [self.side])
        self.assertEqual(event["contradiction_polarity"], "fix")
        self.assertEqual(event["contradiction_polarity_via"],
                         [declarer["id"]])
        self.assertIsNone(dispatches._close_event_error(
            event, pre_state, current=pre_current, verdicts=pre_verdicts))


class CarriedRebaseLandedTest(CloseBase):
    """THE ROW CLASS THE CLOSE LADDER HAD NO DOOR FOR (task/close-ladder).

    Measured on the live board: three rows FINISHED for 2-14 hours
    and structurally unclosable — 0a36e61fd6cd, 18e895caf600, 158ce57d13fb.
    Each carries a FIX verdict and each has its reviewed tip on trunk BY
    PATCH IDENTITY, because this fleet REBASES before it lands. Every door
    refused, and each refusal was individually correct:

      landed      contrary chain polarity (the verdict is FIX)
      resolved    ancestry only, and these are patch-equivalent
      subsumed    needs the original ABSENT from trunk; it is PRESENT
      superseded  needs a later APPROVE on the SAME chain; the cure rounds
                  were dispatched --new-work, so no chain link exists
      withdrawn   proves ABSENCE, which would be a lie here
      carried     "carriage could not be MEASURED"

    `carried` is the door whose docstring already describes these rows, and
    it could not measure ANY of them. Two independent faults, both pinned
    below:

    FAULT A — NO WITNESS COULD SEE A REBASE-LAND. `rowworld._carriage` asks
    only about trunk HEAD's CONTENT (postimages at HEAD, replay is a no-op),
    and ordinary later edits to the same files defeat both. Neither witness
    can see the state `git cherry` reports in one call: every commit up to
    the reviewed tip REACHED trunk under a rewritten sha.

    FAULT B — THE DOOR HAD NEVER WRITTEN ONCE. `_record_close_proven` accepts
    carried_base/carried_tip and DROPPED them, so the writer's own
    re-derivation compared a real pair against two absent fields and refused
    every carried close. Zero `carried` rows in a ledger with 390 closes over
    eight other reasons; only a dry run had ever been exercised."""

    def lane_and_rebase_land(self):
        """Trunk carries the lane's PATCH under a rewritten sha, and has moved
        on top of the same file since. That second half is not decoration: it
        is what defeats both content witnesses, and without it this fixture
        would pass through the old code and prove nothing."""
        base = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "carried-lane")
        reviewed = self.commit("R", path="feature")
        self.git("checkout", "-q", self.main)
        self.commit("d")                       # trunk moves: parents differ
        self.git("cherry-pick", reviewed)
        landed = self.git("rev-parse", "HEAD")
        self.commit("later", path="feature")   # trunk edits the same file
        # MUST-HIT: the fixture really is a rebase-land, not an ancestry one.
        self.assertNotEqual(landed, reviewed)
        self.assertEqual(
            landreq._landing_proof(self.gitdir(), reviewed,
                                   "refs/heads/" + self.main),
            "patch-equivalent")
        return base, reviewed

    def chained_fix_row(self, base, reviewed):
        """A FIX-verdicted review row chained to a build row — 0a36e61fd6cd's
        exact shape (a work pair exists; no witness can affirm it)."""
        build = self.dispatch(ref=base, lane="lane/carried-chained",
                              kind="build")
        row = dispatches.add(
            "seat-b", "lane/carried-chained-review", ref=reviewed,
            repo=self.repo, kind="review", notify=False, new_work=False,
            supersedes=build["id"])
        _out, err = self.mark_verdict(row["id"], reviewed,
                                            "findings", polarity="fix")
        self.assertIsNone(err)
        return row

    def rootless_fix_row(self, reviewed):
        """A --new-work FIX review row that is its OWN chain root —
        158ce57d13fb's shape, where `_work_pair` has no base at all."""
        return self.verdict_row(polarity="fix", ref=reviewed,
                                lane="lane/carried-rootless")

    # ---------------------------------------------------------- fault A

    def test_no_content_witness_can_see_a_rebase_land(self):  # noqa: VACUOUS_ASSERTION — the assertIsNot/assertIsNone pair IS the claim (two witnesses that cannot speak), and the unconditional positive control is the `_nonempty_delta is True` must-hit above them: without an askable delta every witness would answer None for a reason that has nothing to do with this finding
        """The DIAGNOSIS, pinned so a future weakening of the third witness
        cannot be excused as 'the old ones covered it anyway'."""
        base, reviewed = self.lane_and_rebase_land()
        trunk = "refs/heads/" + self.main
        self.assertIs(rowworld._nonempty_delta(self.gitdir(), base, reviewed),
                      True, "the delta must be askable or this proves nothing")
        self.assertIsNot(
            rowworld._postimages_at_head(self.gitdir(), base, reviewed, trunk),
            True)
        self.assertIsNot(
            rowworld._replay_is_a_noop(self.gitdir(), base, reviewed, trunk),
            True)
        self.assertIsNone(rowworld._carriage(self.gitdir(), base, reviewed,
                                             trunk))

    def test_the_cheap_content_witness_cannot_predict_the_replay(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the assertEqual(seen, {False: True, True: False}) after the loop: it pins both of the expensive witness's answers by value, and the subTest assertions inside the loop are the per-side detail
        """`_postimages_at_head` is 5ms and `_replay_is_a_noop` is 140ms over
        the off-frontier census, so a False from the cheap one reads as a
        licence to skip the merge. It is not one: the SAME False sits over both
        answers of the expensive witness, and the row it would drop is the only
        kind the replay ever buys."""
        trunk = "refs/heads/" + self.main
        seen = {}
        for same_line in (False, True):
            with self.subTest(same_line=same_line):
                base, reviewed = self.landed_then_trunk_edits(same_line)
                # MUST-HITS: an askable delta, and the ANCESTRY shape, whose
                # empty `git cherry` range is why the replay is the only family
                # that can affirm one of these rows at all.
                self.assertIs(
                    rowworld._nonempty_delta(self.gitdir(), base, reviewed),
                    True, "an unaskable delta would silence every witness")
                self.assertEqual(
                    self.git("merge-base", "--is-ancestor", reviewed, trunk),
                    "", "the fixture must be the ancestry shape")
                self.assertEqual(
                    rowworld._reached_trunk(self.gitdir(), reviewed, trunk),
                    (None, None),
                    "an ancestor leaves the history witness an empty range")
                # THE CHEAP WITNESS ANSWERS THE SAME WAY ON BOTH SIDES.
                self.assertIs(
                    rowworld._postimages_at_head(self.gitdir(), base,
                                                 reviewed, trunk),
                    False)
                seen[same_line] = rowworld._replay_is_a_noop(
                    self.gitdir(), base, reviewed, trunk)
                self.assertIs(
                    rowworld._carriage(self.gitdir(), base, reviewed, trunk),
                    True if seen[same_line] is True else None)
        # AND THE EXPENSIVE ONE DOES NOT: the far edit is carried, the edit on
        # the delta's own line conflicts. One True is what a filter on the
        # cheap answer would have thrown away.
        self.assertEqual(seen, {False: True, True: False})

    def test_a_chained_fix_row_closes_on_the_reached_trunk_witness(self):  # noqa: VACUOUS_ASSERTION — assertIsNone(err) is not the observable; history_len +1 and the read-back event's carried_tip/close_proof_mode are, and both are unconditional positive assertions on the row this arm names
        base, reviewed = self.lane_and_rebase_land()
        row = self.chained_fix_row(base, reviewed)
        before = self.history_len(row["id"])
        out, err = landreq.close(row["id"], "carried",
                                 evidence="the cure round never chain-linked",
                                 repo=self.repo)
        self.assertIsNone(err)
        self.assertEqual(out["close_reason"], "carried")
        self.assertEqual(self.history_len(row["id"]), before + 1)
        event = self.close_event(row["id"])
        self.assertEqual(event["carried_tip"], reviewed)
        self.assertEqual(event["close_proof_mode"], "reached-trunk")

    def test_a_rootless_new_work_fix_row_closes_too(self):  # noqa: VACUOUS_ASSERTION — the (None, None) must-hit proves the fixture really is the no-pair shape before anything is concluded from a success, and history_len +1 plus the read-back carried_tip are the unconditional positive controls
        """FAULT 2 as the sweep framed it: a --new-work review row is its own
        chain root, `_work_pair` returns (None, None), and carried
        fail-closed forever. The reached-trunk witness needs no base."""
        _base, reviewed = self.lane_and_rebase_land()
        row = self.rootless_fix_row(reviewed)
        rows, _v, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        # MUST-HIT: this really is the no-pair shape the sweep measured.
        self.assertEqual(
            rowworld._work_pair(rows[row["id"]], rows,
                                rowworld._carriers(rows)),
            (None, None))
        before = self.history_len(row["id"])
        out, err = landreq.close(row["id"], "carried",
                                 evidence="dispatched --new-work; no chain",
                                 repo=self.repo)
        self.assertIsNone(err)
        self.assertEqual(out["close_reason"], "carried")
        self.assertEqual(self.history_len(row["id"]), before + 1)
        event = self.close_event(row["id"])
        self.assertIsNone(event.get("carried_base"),
                          "a baseless witness must not invent a base")
        self.assertEqual(event["carried_tip"], reviewed)

    # ---------------------------------------------------------- fault B

    def test_the_writer_records_the_pair_it_measured(self):  # noqa: VACUOUS_ASSERTION — the whole-dict equality on the four recorded fields is the positive control, and the carried_base None it asserts is a MEASURED absence (the reached-trunk witness derives its own range) pinned beside a non-None carried_tip in the same dict
        """The door had never written once: the pair was accepted as kwargs
        and dropped, so the writer's own re-derivation refused every event.
        Asserting the FIELDS, not merely that a close succeeded."""
        base, reviewed = self.lane_and_rebase_land()
        row = self.chained_fix_row(base, reviewed)
        pre_current, pre_verdicts, unavailable = \
            dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        pre_state = pre_current[row["id"]]
        out, err = landreq.close(row["id"], "carried", evidence="why",
                                 repo=self.repo)
        self.assertIsNone(err)
        event = self.close_event(row["id"])
        self.assertEqual(
            {key: event.get(key) for key in
             ("close_reason", "carried_base", "carried_tip",
              "close_proof_mode")},
            {"close_reason": "carried", "carried_base": None,
             "carried_tip": reviewed, "close_proof_mode": "reached-trunk"})
        # ...and the SAME event survives the third site, which is what the
        # dropped fields were breaking.
        self.assertIsNone(dispatches._close_event_error(
            event, pre_state, current=pre_current, verdicts=pre_verdicts))
        self.assertEqual(out["closing_trunk_ref"],
                         "refs/heads/" + self.main)

    def test_a_forged_pair_is_still_refused_at_replay(self):  # noqa: VACUOUS_ASSERTION — the sibling arm proves this exact event replays CLEAN unforged, so a refusal here cannot be the validator refusing everything
        base, reviewed = self.lane_and_rebase_land()
        row = self.chained_fix_row(base, reviewed)
        pre_current, pre_verdicts, _u = dispatches.snapshot_with_verdicts()
        pre_state = pre_current[row["id"]]
        _out, err = landreq.close(row["id"], "carried", evidence="why",
                                  repo=self.repo)
        self.assertIsNone(err)
        event = self.close_event(row["id"])
        for forged, expect in (
                (dict(event, carried_tip="f" * 40), "do not match"),
                (dict(event, carried_base="f" * 40), "do not match"),
                (dict(event, close_proof_mode="carriage-replay"),
                 "witness")):
            self.assertTrue(
                dispatches._close_event_error(
                    forged, pre_state, current=pre_current,
                    verdicts=pre_verdicts),
                "a forged %s replayed clean" % expect)

    # ------------------------------------------------- the refusal kept

    def test_genuinely_unlanded_work_still_refuses(self):  # noqa: VACUOUS_ASSERTION — the `_landing_proof == absent` must-hit is the unconditional positive control that the fixture really is unlanded, and history_len unchanged is asserted against a counter this suite proves moves
        """THE DISCRIMINATOR. `git cherry` printing `+` means NOT landed, and
        that must gate the new witness or it is a hole that eats real work."""
        self.lane_and_rebase_land()
        row = self.verdict_row(polarity="fix", ref=self.side,
                               lane="lane/carried-unlanded")
        # MUST-HIT: the control really is unlanded by the stated discriminator.
        self.assertEqual(
            landreq._landing_proof(self.gitdir(), self.side,
                                   "refs/heads/" + self.main),
            "absent")
        before = self.history_len(row["id"])
        out, err = landreq.close(row["id"], "carried", evidence="try it",
                                 repo=self.repo)
        self.assertIsNone(out)
        self.assertTrue(err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_a_partly_landed_stack_refuses(self):  # noqa: VACUOUS_ASSERTION — the `patch-equivalent` must-hit proves the TOP commit really does cherry-match, so the refusal is demonstrably about the range and not about a fixture that never landed anything
        """A `+` ANYWHERE in the range refuses, not just at the tip. A row
        whose prerequisite commit never landed does not have its work on
        trunk however cleanly the top commit cherry-matches."""
        base = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "partial-lane")
        prereq = self.commit("unlanded-prerequisite", path="prereq")
        reviewed = self.commit("top", path="feature")
        self.git("checkout", "-q", self.main)
        self.commit("d")
        self.git("cherry-pick", reviewed)   # only the TOP lands
        # MUST-HIT: the top commit alone really does read patch-equivalent, so
        # the refusal below is about the RANGE and not about the tip.
        self.assertEqual(
            landreq._landing_proof(self.gitdir(), reviewed,
                                   "refs/heads/" + self.main),
            "patch-equivalent")
        row = self.chained_fix_row(base, reviewed)
        before = self.history_len(row["id"])
        out, err = landreq.close(row["id"], "carried", evidence="try it",
                                 repo=self.repo)
        self.assertIsNone(out)
        # THE EXACT SENTENCE OF THE UNMATCHED RUNG, and the sha it names.
        # "did not reach" also appears in the generic unmeasurable refusal —
        # a mutation that stopped recognising `+` at all fell through to that
        # message and this arm stayed green. A substring two rungs share
        # discriminates neither of them.
        self.assertIn("could not be matched onto", err)
        self.assertIn(prereq[:12], err)
        self.assertEqual(self.history_len(row["id"]), before)

    # ------------------------------------- the rungs inside the witness

    def test_an_empty_range_affirms_nothing(self):  # noqa: VACUOUS_ASSERTION — the empty `git cherry` output is asserted directly as the must-hit, and the same instrument is shown answering True on a non-empty range in the same method
        """A tip already REACHABLE from trunk hands `git cherry` an EMPTY
        range, and an empty range is satisfied by every possible trunk — the
        same vacuity the degenerate-pair rung refuses one layer up. Silence,
        not affirmation: this door deliberately does not grow `landed`'s
        ancestry leg by the back way."""
        trunk = "refs/heads/" + self.main
        # MUST-HIT: the range really is empty, so the None below is about THIS
        # rung and not about a git that failed to run.
        self.assertEqual(self.git("cherry", trunk, self.b), "")
        self.assertEqual(rowworld._reached_trunk(self.gitdir(), self.b,
                                                 trunk), (None, None))
        # ...and the same call DOES answer on a non-empty range.
        _base, reviewed = self.lane_and_rebase_land()
        self.assertEqual(rowworld._reached_trunk(self.gitdir(), reviewed,
                                                 "refs/heads/" + self.main),
                         (True, None))

    def test_a_tip_missing_from_its_own_listing_affirms_nothing(self):  # noqa: VACUOUS_ASSERTION — the listing is asserted NON-EMPTY and uniformly `-` before the None is read, so the refusal can only come from the tip's own absence
        """`git cherry` can hand back a range that is entirely `-` while never
        mentioning the tip it was asked about: merges are skipped, and a
        commit whose equivalent it finds on the other side can drop out of
        the listing altogether. Affirming there is reading someone else's
        answer about someone else's commit.

        THE FIXTURE IS TWO SIBLING LANES, not a lane that merged trunk. That
        first shape looks equivalent and is not — merging trunk into a lane
        makes `git cherry` mark the lane's OWN landed commits `+`, which
        exits at the unmatched rung and never reaches the one this arm is
        about (measured on a four-commit probe)."""
        self.git("checkout", "-q", "-b", "l1")
        self.commit("X", path="f1")
        x = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.git("checkout", "-q", "-b", "l2")
        self.commit("Y", path="f2")
        y = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.git("cherry-pick", x)
        self.git("cherry-pick", y)
        self.commit("Z")
        self.git("checkout", "-q", "l1")
        self.git("merge", "--no-edit", "-q", "l2")
        merged = self.git("rev-parse", "HEAD")
        trunk = "refs/heads/" + self.main
        listing = self.git("cherry", trunk, merged)
        # MUST-HIT: NON-EMPTY and uniformly `-`, and the tip is absent from
        # it. Without all three the None below would prove something else.
        self.assertTrue(listing)
        self.assertTrue(all(line.startswith("-")
                            for line in listing.splitlines()), listing)
        self.assertNotIn(merged, listing)
        self.assertEqual(rowworld._reached_trunk(self.gitdir(), merged,
                                                 trunk), (None, None))

    def lane_and_clean_land(self):
        """Trunk carries the lane's patch and has NOT touched those files
        since, so the CONTENT witnesses can still speak. Kept alive because
        the new family must not quietly become the only one that answers."""
        base = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "clean-lane")
        reviewed = self.commit("R", path="feature")
        self.git("checkout", "-q", self.main)
        self.commit("d")
        self.git("cherry-pick", reviewed)
        # MUST-HIT: this fixture really does get an answer from the FIRST
        # family, or the arm below proves nothing about which one recorded.
        self.assertIs(rowworld._carriage(self.gitdir(), base, reviewed,
                                         "refs/heads/" + self.main), True)
        return base, reviewed

    def test_the_replay_witness_records_the_base_it_replayed(self):  # noqa: VACUOUS_ASSERTION — the whole-dict equality on the three recorded fields is the positive control, and its carried_base is a real sha rather than the reached-trunk witness's measured None
        """The base is only recordable when a base EXISTED, so the
        carried_base field is invisible to every reached-trunk arm — a
        mutation that dropped it again survived the whole suite until this
        arm existed."""
        base, reviewed = self.lane_and_clean_land()
        row = self.chained_fix_row(base, reviewed)
        before = self.history_len(row["id"])
        _out, err = landreq.close(row["id"], "carried", evidence="why",
                                  repo=self.repo)
        self.assertIsNone(err)
        self.assertEqual(self.history_len(row["id"]), before + 1)
        event = self.close_event(row["id"])
        self.assertEqual(
            {key: event.get(key) for key in
             ("carried_base", "carried_tip", "close_proof_mode")},
            {"carried_base": base, "carried_tip": reviewed,
             "close_proof_mode": "carriage-replay"})

    # ----------------------------------------------- the closed loop

    # THE DOORS A REFUSAL NAMES, READ BACK OUT OF IT so each can be MEASURED
    # against the row rather than trusted. NAMED is every door the sentence
    # spells as a door; SUBJECT is the one it says this state belongs to;
    # PROMISE is the phrasing that sends a reader to a door AS THE REMEDY.
    NAMED = "`%s`"
    SUBJECT = "`%s` is the only reason whose subject is this state"
    PROMISE = "through %s instead"

    def doors(self, err, phrase):
        return [door for door in landreq.CLOSE_CLI_REASONS
                if (phrase % door) in err]

    def ask(self, row, reason, evidence="no chain link was ever recorded"):
        """One dry-run close, typed the way a reader following the advice
        would type it: no trunk named, so every door measures against the
        row's own landing trunk."""
        return landreq.close(row["id"], reason, evidence=evidence,
                             dry_run=True)

    def test_the_advice_names_a_door_that_admits(self):
        """FAULT 1. `resolved` refuses a patch-equivalent row and NAMES the
        door to use next. That advice was `subsumed` for two weeks, and
        `subsumed`'s git rung refuses on the exact COMPLEMENT of the
        measurement that produced the advice — it requires the original
        ABSENT from trunk, and patch identity means the original's own delta
        is what trunk carries. Advice pointing at a door whose premise the
        referring measurement falsifies is a closed loop, and three live rows
        sat in it for 2 to 14 hours.

        THIS ARM BINDS THE TWO TOGETHER. Asserting the new wording alone
        would let the next author re-point it at another dead door and stay
        green; what is pinned here is that the door the refusal names as this
        state's subject ADMITS the one shape it can.

        AND HERE IS EXACTLY WHAT THIS FIXTURE CAN PROVE, written down because
        it once read as more. The world below is built with ONE `cherry-pick`,
        so the range beneath the tip is a SINGLE commit and `git cherry` reads
        uniformly `-`. That is the one shape in which `carried` is certain to
        admit, and this arm stayed green through the whole time the refusal
        promised `carried` for rows it refuses. The two arms after this one
        build the shapes where it refuses — a prerequisite that never landed,
        and a tip object gc pruned — and it is THEY that go red if the
        sentence promises a door again."""
        # `subsumption_case` is REUSED rather than hand-rolled because every
        # rung `subsumed` checks before its git rung must be SATISFIED — a
        # control that dies at "is not a review dispatch" or "not linked to
        # the same chain" never reaches the rung this arm is about, and both
        # of those killed the first two cuts of this test.
        target, confirmation, _tip = self.subsumption_case(
            original_polarity="fix",
            confirmation_statement=(
                "Subsumption verified FIX findings were answered on trunk: "
                "the cure round landed rebased"))
        self.git("cherry-pick", "side")        # trunk gains the PATCH, not the sha
        # MUST-HIT: the row really is in the state the advice is given for.
        self.assertEqual(
            landreq._landing_proof(self.gitdir(), self.side,
                                   "refs/heads/" + self.main),
            "patch-equivalent")

        # the refusal that gives the advice, and the door it names
        _out, resolved_err = landreq.close(target["id"], "resolved",
                                           evidence=confirmation["id"][:12],
                                           trunk=self.main, dry_run=True)
        self.assertIn("PATCH IDENTITY", resolved_err)
        named = self.doors(resolved_err, self.SUBJECT)
        self.assertEqual(named, ["carried"],
                         "the refusal must name exactly one door as this "
                         "state's subject")

        # the OLD advice really was a loop, and the loop is still there — so a
        # regression that re-points at subsumed cannot pass on wording alone.
        with self.family_evidence():
            _out, subsumed_err = landreq.close(
                target["id"], "subsumed", evidence=confirmation["id"][:12],
                trunk=self.main, dry_run=True)
        self.assertEqual(
            subsumed_err,
            "the original reviewed change is on trunk and is not subsumed")

        # ...and the door it DOES name admits this row.
        out, err = landreq.close(target["id"], named[0],
                                 evidence="no chain link was ever recorded",
                                 trunk=self.main, dry_run=True)
        self.assertIsNone(err)
        self.assertEqual(out["reason"], "carried")
        self.assertIs(out["would_append"], True)

    def test_the_advice_promises_nothing_over_an_unlanded_prerequisite(self):
        """MODE ONE of the two the one-commit arm cannot build: a stack whose
        TOP commit trunk carries by patch identity while the PREREQUISITE
        beneath it never landed. `resolved` reads the top commit alone and
        refuses on patch identity; `carried` reads the whole range and
        refuses on the prerequisite. The old advice, "close it through carried
        instead", sent this row to a door that refuses it.

        EVERY DOOR THE SENTENCE NAMES IS ASKED HERE, and the list of them is
        pinned, so a door added to the sentence later arrives unmeasured and
        reddens this arm instead of riding in on the wording. `carried`
        refuses in the mode the sentence names for it; `stranded`, named for a
        tip object that is gone, refuses because this one still reads."""
        base = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "partial-lane")
        prereq = self.commit("unlanded-prerequisite", path="prereq")
        reviewed = self.commit("top", path="feature")
        self.git("checkout", "-q", self.main)
        self.commit("d")
        self.git("cherry-pick", reviewed)   # only the TOP lands
        # MUST-HIT: the top commit alone reads patch-equivalent, the one state
        # the advice is given for, so the refusal below is that rung's.
        self.assertEqual(
            landreq._landing_proof(self.gitdir(), reviewed,
                                   "refs/heads/" + self.main),
            "patch-equivalent")
        row = self.chained_fix_row(base, reviewed)

        _out, advice = self.ask(row, "resolved")
        self.assertIn("%s reaches trunk only by PATCH IDENTITY" % reviewed[:12],
                      advice)

        # the door the advice names as this state's subject, asked
        out, carried_err = self.ask(row, "carried")
        self.assertIsNone(out)
        self.assertIn("could not be matched onto", carried_err)
        self.assertIn(prereq[:12], carried_err)
        # so a promise here names a door this arm just watched refuse
        self.assertEqual(self.doors(advice, self.PROMISE), [],
                         "the refusal promises a door that refused this row: "
                         "%s" % carried_err)
        self.assertEqual(self.doors(advice, self.SUBJECT), ["carried"])
        self.assertIn("an unmatched commit beneath the tip", advice)
        self.assertEqual(self.doors(advice, self.NAMED),
                         ["stranded", "carried"],
                         "a door the sentence names must be measured here")

        # the door the sentence names for a GONE object, asked about one
        # that reads
        out, stranded_err = self.ask(row, "stranded",
                                     evidence="the lane was never pruned")
        self.assertIsNone(out)
        self.assertIn("is still an object", stranded_err)

    def test_the_advice_names_the_door_a_gone_tip_has(self):
        """MODE TWO: the reviewed tip object is gone. Such a row reaches
        `resolved`'s patch-identity rung ONLY through the proof kept while
        the object was readable — nothing live can read a patch id out of an
        object that is not there — and `carried` refuses it at the carriage
        proof's own object rung, not through the landing ladder.

        AND IT IS THE ROW THAT REFUTES A UNIVERSAL. "If carried refuses, no
        close door covers this row" is a claim about every door, measured
        against one. `stranded` exists for exactly a reviewed object that is
        gone, and it admits this row. So the sentence names it, and this arm
        asks it.

        THE REPOSITORY HAS A REACHABLE ORIGIN, deliberately. That is the one
        condition under which the vanished pass can LOOK for a gone object, so
        this fixture holds exactly the object whose landing word is contested:
        the live ladder answers `unknown` for it, the vanished pass `absent`.
        The kept positive answers before either, and that is what this arm
        pins."""
        self.git("checkout", "-q", "-b", "gone-lane")
        reviewed = self.commit("R", path="feature")
        self.git("checkout", "-q", self.main)
        self.commit("d")
        self.git("cherry-pick", reviewed)   # trunk gains the PATCH, not the sha
        bare = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "--bare", "-q", bare], check=True)
        self.git("remote", "add", "origin", bare)
        self.git("push", "-q", "origin", self.main)   # trunk only, never R
        self.git("fetch", "-q", "origin")
        pinned = self.git("rev-parse", self.main)
        row = self.verdict_row(polarity="fix", ref=reviewed,
                               lane="lane/advice-gone-tip")
        # the proof is KEPT while the object reads — the ladder keeps a
        # positive under full object ids — and then gc takes the object.
        self.assertEqual(
            landreq._landing_proof(self.gitdir(), reviewed, pinned),
            "patch-equivalent")
        self.prune(reviewed, "refs/heads/gone-lane")
        # MUST-HIT: after the prune the kept proof is the witness that is
        # left. The live ladder, asked the same question, cannot read a patch
        # id and answers `unknown`; the vanished pass, which the origin lets
        # look, finds nothing holding the object and answers `absent`.
        self.assertEqual(
            landreq._kept_landing_proof(self.gitdir(), reviewed, pinned),
            "patch-equivalent")
        self.assertEqual(
            landreq._derive_landing_proof(self.gitdir(), reviewed, pinned),
            "unknown")
        self.assertEqual(landreq._vanished_absence(self.gitdir(), reviewed),
                         "absent")

        _out, advice = self.ask(row, "resolved")
        self.assertIn("%s reaches trunk only by PATCH IDENTITY" % reviewed[:12],
                      advice)

        # the door the advice names as this state's subject, asked
        out, carried_err = self.ask(row, "carried")
        self.assertIsNone(out)
        self.assertIn("carriage could not be MEASURED", carried_err)
        self.assertIn("the tip object %s is not in" % reviewed[:12],
                      carried_err)
        self.assertEqual(self.doors(advice, self.PROMISE), [],
                         "the refusal promises a door that refused this row: "
                         "%s" % carried_err)
        self.assertIn("a tip object this repository no longer holds", advice)

        # the door the sentence names for a gone object, asked about one —
        # and it ADMITS, so a sentence that leaves it out says no door covers
        # a row that one does
        out, err = self.ask(row, "stranded",
                            evidence="gc pruned the lane after it landed")
        self.assertIsNone(err)
        self.assertEqual(out["reason"], "stranded")
        self.assertIs(out["would_append"], True)
        self.assertEqual(self.doors(advice, self.NAMED),
                         ["stranded", "carried"],
                         "a door that admits this row must be named, and a "
                         "door the sentence names must be measured here")


class CarriageReplayDerivedTest(CloseBase):
    """THE REPLAY WITNESS'S ANSWER, RE-DERIVED THROUGH THE FOLD CHECKPOINT'S
    DOOR (task/2813, `helm/carriageckpt.py`).

    WHY THE FIXTURE IS THE CARRIED-THEN-EDITED SHAPE AND NOT A SIMPLER ONE.
    Every arm here counts `merge-tree --write-tree` spawns, so the fixture has
    to be one where the replay actually runs: a rebase-land whose postimage is
    still at trunk affirms on the CHEAP witness and never reaches the merge, and
    a zero counted against that costs nothing and proves nothing.
    `landed_then_trunk_edits(False)` is the one shape where the cheap witness
    refuses and the merge affirms, so the count and the answer both come from
    the read this door exists to remove.

    EVERY ARM HERE SPIES AT BOTH DOORS. `rowworld`'s reads go through the `vcs`
    seam and `landreq`'s go through its own `--git-dir` spawn, and a count taken
    at one of them cannot see a question that moved to the other."""

    def spawn_spy(self):
        """Every git argv this test spawns, at BOTH doors, as
        [(door, argv)] — live until the test ends."""
        seen = []
        real_vcs, real_lr = vcs.GitVcs._spawn, landreq._git_spawn

        def at_vcs(backend, cwd, args, timeout, env, stdin):
            seen.append(("vcs", tuple(str(a) for a in args)))
            return real_vcs(backend, cwd, args, timeout, env, stdin)

        def at_landreq(gitdir, args, input_text, env=None):
            seen.append(("landreq", tuple(str(a) for a in args)))
            return real_lr(gitdir, args, input_text, env=env)

        for patch in (mock.patch.object(vcs.GitVcs, "_spawn", at_vcs),
                      mock.patch.object(landreq, "_git_spawn", at_landreq)):
            patch.start()
            self.addCleanup(patch.stop)
        return seen

    @staticmethod
    def merges(seen):
        return [argv for _door, argv in seen if argv[0] == "merge-tree"]

    def trunk_sha(self):
        return self.git("rev-parse", "--verify",
                        "refs/heads/%s^{commit}" % self.main).lower()

    def projection(self, base, tip, trunk_sha, region=True):
        """One census-shaped read: a scope, the region the census declares, and
        the production witness. `region=False` is the writer's road — no region
        is declared and nothing may be served."""
        door = carriageckpt.derived() if region \
            else contextlib.nullcontext(None)
        with projscope.scope():
            with door as got:
                answer = dispatches._carriage_replay_witness(
                    self.gitdir(), base, tip, "refs/heads/" + self.main,
                    trunk_sha)
        return answer, got

    def carried_pair(self):
        """(base, tip, trunk sha) whose answer only the merge can give."""
        base, tip = self.landed_then_trunk_edits(False)
        trunk = self.trunk_sha()
        # MUST-HIT on the PROCESS: a tree that changed under this interpreter
        # cannot name its own generation, and every arm here would then be
        # measuring the no-store road rather than the store.
        self.assertIsNotNone(foldckpt.policy())
        # MUST-HITS on the fixture: the cheap witness refuses, so the merge is
        # the only source of the answer, and the merge affirms, so an arm that
        # loses the answer reads as a changed value rather than as silence.
        self.assertIs(rowworld._postimages_at_head(
            self.gitdir(), base, tip, trunk), False)
        self.assertIs(rowworld._replay_is_a_noop(
            self.gitdir(), base, tip, trunk), True)
        return base, tip, trunk

    def warmed(self, base, tip, trunk):
        """Derive, then PROVE the next projection is served — so a later
        derivation is a measurement of the perturbation and not of a store that
        was never holding."""
        self.projection(base, tip, trunk)
        _answer, region = self.projection(base, tip, trunk)
        self.assertEqual((region.served, region.derived), (1, 0),
                         "the store must hold before a perturbation means "
                         "anything")

    def test_a_second_projection_pays_no_merge_tree_and_serves_the_same_answer(self):  # noqa: VACUOUS_ASSERTION — the warm projection's EMPTY merge-tree list is the finding, and its unconditional control is assertTrue(paid) over the cold projection's spawns at the same two spies; two projections are two producers by construction, so no one binding can carry both, and assertEqual(second, first) pins the served value itself
        """ARM 1. The bill this door exists to remove, counted."""
        base, tip, trunk = self.carried_pair()
        seen = self.spawn_spy()
        first, cold = self.projection(base, tip, trunk)
        paid = self.merges(seen)
        del seen[:]
        second, warm = self.projection(base, tip, trunk)
        # THE UNCONDITIONAL POSITIVE CONTROL: the cold projection really pays a
        # merge-tree at one of the two doors. Without it the zero below would
        # be a broken counter reading as a cure.
        self.assertTrue(paid, "the cold projection paid no merge-tree, so the "
                              "warm zero measures nothing")
        self.assertEqual(self.merges(seen), [])
        self.assertIs(first[0], True)
        self.assertEqual(second, first)
        self.assertEqual((cold.served, cold.derived, cold.refused), (0, 1, 0))
        self.assertEqual((warm.served, warm.derived, warm.refused), (1, 0, 0))

    def test_nothing_is_served_to_a_reader_that_declares_no_region(self):  # noqa: VACUOUS_ASSERTION — every assertion here is POSITIVE (a non-empty merge-tree list and answer True), and the state it perturbs is established by warmed()'s own unconditional (served, derived) == (1, 0)
        """The writer's road. `carriage_proof` authorizes a real close and
        re-derives one under the lock; neither declares a region, so both must
        pay the witness every time however warm the store is."""
        base, tip, trunk = self.carried_pair()
        self.warmed(base, tip, trunk)
        seen = self.spawn_spy()
        answer, _no_region = self.projection(base, tip, trunk, region=False)
        self.assertIs(answer[0], True)
        self.assertTrue(self.merges(seen),
                        "an undeclared reader was served a stored answer")

    def test_a_fold_being_recorded_is_never_served_and_its_recorder_sees_the_replay(self):  # noqa: VACUOUS_ASSERTION — the empty merge-tree list is the CONTROL's, asserted beside served == 1 in the same region; the arm's own observables are positive: a non-empty merge-tree list under the recorder and a non-empty planned expression set equal to an unregioned derivation's
        """ARM 3. `foldckpt.recording_active`, THE GUARD THAT KEEPS A STORED
        ANSWER OUT OF THE FOLD CHECKPOINT.

        THE PATH IS PRODUCTION'S. The census's `withdrawn` door asks its
        ladder in dry run, the ladder reaches `dispatches._record_close_proven`,
        and that folds the dispatch ledger under the lock INSIDE the region
        `frontier_verdicts` declared. A `carried` close in that fold's tail
        therefore re-derives through this witness with a region open AND a
        recording running. The fold checkpoint keys on every git read the fold
        took, and a served answer takes none, so the checkpoint would go on
        restoring that close after a pruned tip or a moved merge driver had
        changed its answer.

        THE CONTROL IS IN THE SAME REGION, ONE CALL EARLIER, and the question
        IS served there. The live derivation under the recorder is therefore
        the guard's effect, not a store that stopped holding."""
        base, tip, trunk = self.carried_pair()
        ref = "refs/heads/" + self.main
        with foldckpt.recording() as unregioned:
            expected = dispatches._carriage_replay_witness(
                self.gitdir(), base, tip, ref, trunk)
        self.warmed(base, tip, trunk)
        seen = self.spawn_spy()
        with projscope.scope():
            with carriageckpt.derived() as region:
                served = dispatches._carriage_replay_witness(
                    self.gitdir(), base, tip, ref, trunk)
                control = (region.served, self.merges(seen))
                del seen[:]
                with foldckpt.recording() as rec:
                    folded = dispatches._carriage_replay_witness(
                        self.gitdir(), base, tip, ref, trunk)
        self.assertEqual(control, (1, []),
                         "the store was not holding in this region, so the "
                         "arm below measures nothing")
        self.assertEqual(region.served, 1,
                         "a stored answer was served into a fold being "
                         "recorded")
        self.assertTrue(self.merges(seen), "the fold's replay did not run live")
        self.assertEqual(folded, served)
        self.assertEqual(folded, expected)
        # THE PROPERTY ITSELF: the fold's recorder holds exactly what a
        # derivation outside any region records, so the checkpoint re-verifies
        # the same objects whether or not a census was running.
        self.assertTrue(foldckpt.plan(rec)[self.gitdir()]["exprs"])
        self.assertEqual(foldckpt.plan(rec), foldckpt.plan(unregioned))

    def test_a_derivation_taken_inside_a_fold_is_never_stored(self):  # noqa: VACUOUS_ASSERTION — the absent store directory has its unconditional positive control on the same observable: the same question outside the recording writes exactly one generation file, asserted by name
        """ARM 4. The guard's write half. Without it the fold's own replay
        becomes an entry, and the NEXT fold inside a region over the same
        objects is served it: arm 3's leak, reached through the store's write
        side instead of its read side."""
        base, tip, trunk = self.carried_pair()
        with projscope.scope():
            with carriageckpt.derived() as region:
                with foldckpt.recording():
                    answer = dispatches._carriage_replay_witness(
                        self.gitdir(), base, tip, "refs/heads/" + self.main,
                        trunk)
        self.assertIs(answer[0], True)
        self.assertEqual((region.served, region.derived, region.refused),
                         (0, 0, 0))
        self.assertFalse(os.path.isdir(carriageckpt.store_dir()),
                         "a derivation taken inside a fold was written")
        # THE UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE: the same
        # question asked outside a recording IS written.
        self.projection(base, tip, trunk)
        self.assertEqual(sorted(os.listdir(carriageckpt.store_dir())),
                         [foldckpt.policy()[:16] + carriageckpt.SUFFIX])

    def test_a_new_question_keeps_the_answers_the_store_already_held(self):  # noqa: VACUOUS_ASSERTION — the empty merge-tree list follows an unconditional assertTrue over the same spy for the write that precedes it, and served == 1 with answer True are positive observables of the read itself
        """ARM 5. A write ADDS to a repository's section while that section
        still describes git; it does not replace it.

        Whether the entries already in the file survive a write turns on one
        comparison: the environment the file recorded against the one this
        walk read under. A JSON round trip hands the first back as lists, and
        `foldckpt.plan` spells the second as tuples, so compared as they come
        they never match. Every write would then discard the repository's
        other entries, and a question set that grew by one between lands would
        alternate until trunk moved: one read paying the new question, the next
        paying all the rest.

        THE NEW QUESTION HERE IS A MOVED TRUNK, because that is a second key
        over objects the section already holds. Nothing the old key read has
        changed, so its answer is still the one git would give."""
        base, tip, trunk = self.carried_pair()
        self.warmed(base, tip, trunk)
        self.commit("later trunk")
        moved = self.trunk_sha()
        self.assertNotEqual(moved, trunk)
        seen = self.spawn_spy()
        _answer, grown = self.projection(base, tip, moved)
        # THE UNCONDITIONAL POSITIVE CONTROL: the new key really was derived
        # and written, so the read below follows a write rather than none.
        self.assertTrue(self.merges(seen))
        self.assertEqual((grown.served, grown.derived), (0, 1))
        del seen[:]
        answer, again = self.projection(base, tip, trunk)
        self.assertEqual(self.merges(seen), [],
                         "the write for the new key discarded the answer the "
                         "store already held")
        self.assertEqual((again.served, again.derived), (1, 0))
        self.assertIs(answer[0], True)

    def test_what_the_witness_read_moving_invalidates_it_and_an_unrelated_object_does_not(self):  # noqa: VACUOUS_ASSERTION — each perturbation asserts a NON-empty merge-tree list beside derived == 1, and the control's empty list is a different projection's spawn record; warmed() asserts unconditionally that the store was holding before every perturbation, so a store that never held cannot be read as an invalidation
        """ARM 2. Four perturbations and their control, each measured from a
        store this arm has just proved was holding."""
        base, tip, trunk = self.carried_pair()
        seen = self.spawn_spy()

        def after(what):
            del seen[:]
            _answer, region = self.projection(base, tip, trunk)
            return self.merges(seen), region, what

        # THE CONTROL FIRST, because it is what says the other three are about
        # the perturbation: an object the witness never read appears, and the
        # answer is still served.
        self.warmed(base, tip, trunk)
        self.git("checkout", "-q", "-b", "unrelated")
        unrelated = self.commit("junk", path="junk")
        self.git("checkout", "-q", self.main)
        paid, region, _what = after("an unrelated object")
        self.assertEqual(paid, [])
        self.assertEqual((region.served, region.derived), (1, 0))

        # A REPLACEMENT REF ON AN OBJECT THE WITNESS READ. The read pins
        # replacement OFF, so this changes no answer — and it still invalidates,
        # because the fingerprint covers what git's machinery reads beside the
        # objects rather than what this particular overlay happens to disable.
        self.git("update-ref", "refs/replace/" + tip, unrelated)
        paid, region, _what = after("a replacement ref")
        self.assertTrue(paid, "a replacement ref served a stored answer")
        self.assertEqual((region.served, region.derived), (0, 1))
        self.git("update-ref", "-d", "refs/replace/" + tip)

        # A GRAFTS FILE.
        self.warmed(base, tip, trunk)
        grafts = os.path.join(self.gitdir(), "info", "grafts")
        os.makedirs(os.path.dirname(grafts), exist_ok=True)
        with open(grafts, "w", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (tip, base))
        paid, region, _what = after("a grafts file")
        self.assertTrue(paid, "a grafts file served a stored answer")
        self.assertEqual((region.served, region.derived), (0, 1))
        os.unlink(grafts)

        # TRUNK MOVES. It is in the KEY rather than in the verification, so a
        # land is a different question and never a stale entry.
        self.warmed(base, tip, trunk)
        self.commit("later trunk")
        moved = self.trunk_sha()
        self.assertNotEqual(moved, trunk)
        del seen[:]
        _answer, region = self.projection(base, tip, moved)
        self.assertTrue(self.merges(seen), "a moved trunk served the answer "
                                           "taken against the old one")
        self.assertEqual((region.served, region.derived), (0, 1))

    def test_a_policy_bump_invalidates_everything_and_the_same_policy_does_not(self):  # noqa: VACUOUS_ASSERTION — served == 0 under another generation sits beside two positive observables, a non-empty merge-tree list and a second store file whose name is asserted, and the same-generation control is warmed()'s unconditional (1, 0)
        """ARM 3. The generation is the code this process executes, so a
        deploy invalidates every entry at once — and keeps the old file, which
        is what lets a lane's own binary and the hub's coexist."""
        base, tip, trunk = self.carried_pair()
        self.warmed(base, tip, trunk)          # the control: same policy
        before = sorted(os.listdir(carriageckpt.store_dir()))
        self.assertEqual(len(before), 1, "one generation, one file")
        seen = self.spawn_spy()
        other = "b" * 64
        self.assertNotEqual(other, foldckpt.policy())
        with mock.patch.object(foldckpt, "policy", lambda: other):
            _answer, region = self.projection(base, tip, trunk)
        self.assertTrue(self.merges(seen),
                        "another generation was served this one's answer")
        self.assertEqual((region.served, region.derived), (0, 1))
        after = sorted(os.listdir(carriageckpt.store_dir()))
        self.assertEqual(len(after), 2, "the other generation's file")
        self.assertIn(before[0], after, "the bump destroyed the old file")

    def test_a_derivation_whose_reads_cannot_be_replanned_is_not_stored(self):
        """The fail-safe direction. A git read outside the `vcs` seam taints
        the recording, `foldckpt.plan` refuses it, and nothing is written — so
        the next projection derives again instead of serving an answer whose
        inputs nothing can re-verify."""
        base, tip, trunk = self.carried_pair()
        seen = self.spawn_spy()
        real = rowworld._carriage

        def unseamed(*args, **kwargs):
            vcs.unseamed("a probe standing in for a read around the seam")
            return real(*args, **kwargs)

        with mock.patch.object(rowworld, "_carriage", unseamed):
            _answer, region = self.projection(base, tip, trunk)
        self.assertEqual((region.served, region.derived, region.refused),
                         (0, 1, 1))
        self.assertFalse(os.path.isdir(carriageckpt.store_dir()),
                         "an unreplannable derivation was written")
        del seen[:]
        _answer, again = self.projection(base, tip, trunk)
        self.assertTrue(self.merges(seen),
                        "the refused derivation was served back")
        self.assertEqual((again.served, again.derived), (0, 1))
        # THE UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE: the very
        # same projection, with nothing taints its recording, DOES write the
        # store — so the absence above is the taint's effect and not a path
        # this arm was looking in the wrong place for.
        self.assertEqual(sorted(os.listdir(carriageckpt.store_dir())),
                         [foldckpt.policy()[:16] + carriageckpt.SUFFIX])


class RefusalsNameMeasuredDoorsTest(CloseBase):
    """EVERY DOOR A REFUSAL OFFERS IS DRIVEN ON A ROW THAT REACHES IT.

    A refusal that ends "that row wants --reason X" is a prediction about a
    command the reader has not run. A refusal's own arm pins its wording, and
    the offered door's own arms build rows that never reach that refusal, so
    neither can see an offer that is false. The contrary-debt sentence three
    refusals share offers five doors because each takes a mode the others
    refuse: `superseded` refuses the rows `carried`, `resolved`, `withdrawn`
    and `stranded` each take.

    EACH ARM BUILDS ONE ROW IN ONE MEASURED MODE, drives every refusal that row
    reaches, reads back the doors each refusal OFFERS, and drives the door that
    mode belongs to. Two directions, and each catches a different lie:
      - the mode's door must be OFFERED, or the sentence says no door covers a
        row one does (a false universal);
      - the offered door must ADMIT the row, or the sentence is a false
        promise.
    The list a sentence offers is PINNED, and `test_every_offered_door_has_a_
    measured_mode` requires every door on it to own an arm below, so a door
    added to a sentence arrives unmeasured and turns red.

    OFFERED means spelled `NAME` or `--reason NAME`. The sentences write a
    door they mention only to say it refuses in plain words, which is what
    lets this read a warning apart from a promise.
    """

    OFFER = r"(`%s`|--reason %s(?![\w-]))"
    # what each shared sentence offers, in CLOSE_CLI_REASONS order
    CONTRARY = ["superseded", "withdrawn", "stranded", "resolved", "carried"]
    ON_TRUNK = ["landed", "superseded", "resolved", "carried",
                "endorsement-moot"]
    UNVERDICTED = ["landed", "out-of-scope", "discharged"]
    MOOT = ["out-of-scope"]
    # the arm that measures each offered door ADMITTING a row that reached
    # the sentence offering it
    CONTRARY_MODES = {
        "superseded": "test_superseded_takes_a_contrary_a_later_approve_answered",
        "withdrawn": "test_withdrawn_takes_a_contrary_that_never_reached_trunk",
        "stranded": "test_stranded_takes_a_contrary_whose_tip_gc_pruned",
        "resolved": "test_resolved_takes_a_confirmed_ancestry_contrary",
        "carried": "test_carried_takes_a_rebase_landed_contrary"}
    ON_TRUNK_MODES = {
        "landed": "test_landed_takes_an_approve_a_withdraw_found_on_trunk",
        "superseded": CONTRARY_MODES["superseded"],
        "resolved": CONTRARY_MODES["resolved"],
        "carried": CONTRARY_MODES["carried"],
        "endorsement-moot":
            "test_endorsement_moot_takes_a_concur_on_trunk_by_ancestry"}
    UNVERDICTED_MODES = {
        "landed": "test_discharged_is_offered_with_the_landing_it_reads",
        "out-of-scope": "test_an_open_rows_moot_obligation_is_out_of_scopes",
        "discharged": "test_discharged_is_offered_with_the_landing_it_reads"}

    # THE WORLDS, reused from the suites that own them rather than rebuilt: a
    # second copy of a fixture is a second thing that can stop building the
    # state its name claims.
    handoff = CloseSupersededTest.handoff
    superseded_world = CloseSupersededTest.resolved
    lane_and_rebase_land = CarriedRebaseLandedTest.lane_and_rebase_land
    chained_fix_row = CarriedRebaseLandedTest.chained_fix_row
    RESOLUTION = CloseResolvedTest.RESOLUTION
    resolved_pair = CloseResolvedTest.resolved_pair
    resolved_families = CloseResolvedTest.resolved_families
    absent_fix_row = CloseWithdrawnTest.fix_row
    unstamped_row = CloseTranslatedSupersededTest.unstamped_row
    translated_pruned_row = CloseTranslatedSupersededTest.translated_pruned_row
    approved_superseder = CloseTranslatedSupersededTest.approved_superseder

    def offered(self, err):
        return [door for door in landreq.CLOSE_CLI_REASONS
                if re.search(self.OFFER % (re.escape(door), re.escape(door)),
                             err)]

    def ask(self, row, reason, **kw):
        """One dry-run close, typed the way a reader following the advice
        would type it: no trunk named, a delivery class where `landed` needs
        one, trunk HEAD as the superseding tip where `superseded` needs one,
        and evidence where a door asks for it."""
        kw.setdefault("dry_run", True)
        if reason == "superseded":
            kw.setdefault("tip", self.git("rev-parse", self.main))
        if reason == "landed":
            kw.setdefault("live", True)
        elif reason != "endorsement-moot":
            kw.setdefault("evidence", "the refusal offered this door")
        return landreq.close(row["id"], reason, **kw)

    def admits(self, row, reason, **kw):
        out, err = self.ask(row, reason, **kw)
        self.assertIsNone(err, "`%s` refused a row a refusal offered it for: "
                          "%s" % (reason, err))
        self.assertEqual(out["reason"], reason)
        self.assertIs(out["would_append"], True)
        return out

    def refuses(self, row, reason, **kw):
        out, err = self.ask(row, reason, **kw)
        self.assertIsNone(out, "`%s` admitted: %r" % (reason, out))
        self.assertTrue(err)
        return err

    def owner_offered(self, row, err, owner):
        """The doors `err` offers, after checking the one this row's mode
        belongs to is among them. When it is not, the failure MEASURES the
        doors that are offered on this same row and quotes their answers, so
        a sentence that sends this row elsewhere is shown refusing it."""
        offered = self.offered(err)
        if owner not in offered:
            answers = {door: (self.ask(row, door)[1] or "ADMITS")[:160]
                       for door in offered}
            self.fail("`%s` takes this row and the refusal does not offer "
                      "it; the doors it does offer answered %r. The refusal: "
                      "%s" % (owner, answers, err))
        return offered

    def contrary_sentences(self, row, owner, **door_kw):
        """Every refusal that ends on the contrary-door list, then the door
        this mode belongs to. `landed` and `endorsement-moot` refuse a
        contrary on polarity before any git rung; `out-of-scope` refuses one
        on its declared debt."""
        for reason, needle in (("landed", "CONTRARY, not a resolution"),
                               ("out-of-scope", "declared resolution debt"),
                               ("endorsement-moot", "not an endorsement")):
            err = self.refuses(row, reason)
            self.assertIn(needle, err)
            self.assertEqual(self.owner_offered(row, err, owner),
                             self.CONTRARY,
                             "%s's refusal offers doors this suite did not "
                             "measure, or leaves out one it did: %s"
                             % (reason, err))
            self.assertIn("NO NEXT DOOR IS PROMISED", err)
        return self.admits(row, owner, **door_kw)

    def on_trunk_sentence(self, row, owner):
        err = self.refuses(row, "withdrawn")
        self.assertIn("a withdraw over landed work is false", err)
        self.assertEqual(self.owner_offered(row, err, owner), self.ON_TRUNK,
                         err)
        self.assertIn("The door for work on trunk follows the verdict", err)
        return err

    # ------------------------------------------------ the pins themselves

    def test_every_offered_door_has_a_measured_mode(self):  # noqa: VACUOUS_ASSERTION — each sorted() comparison pins the mode map against a refusal whose offered list is first pinned POSITIVELY against a non-empty module literal on the line above it; the callable() checks run per declared door and the map literals are non-empty
        """A DOOR ADDED TO A SENTENCE ARRIVES UNMEASURED unless this is red.
        Each shared sentence is read out of a REAL refusal, not out of the
        source, and every door it offers must map to an arm here that drives
        that door to an admission."""
        row = self.absent_fix_row()
        _out, err = self.ask(row, "landed")
        self.assertEqual(self.offered(err), self.CONTRARY, err)
        self.assertEqual(sorted(self.offered(err)),
                         sorted(self.CONTRARY_MODES))
        self.git("merge", "--no-edit", "-q", "side")
        _out, err = self.ask(row, "withdrawn")
        self.assertEqual(self.offered(err), self.ON_TRUNK, err)
        self.assertEqual(sorted(self.offered(err)),
                         sorted(self.ON_TRUNK_MODES))
        open_row = self.dispatch(lane="lane/offered-unverdicted")
        _out, err = self.ask(open_row, "resolved", evidence="none")
        self.assertEqual(self.offered(err), self.UNVERDICTED, err)
        self.assertEqual(sorted(self.offered(err)),
                         sorted(self.UNVERDICTED_MODES))
        for modes in (self.CONTRARY_MODES, self.ON_TRUNK_MODES,
                      self.UNVERDICTED_MODES):
            for door, arm in modes.items():
                self.assertTrue(callable(getattr(self, arm, None)),
                                "`%s` is offered and no arm measures it: %s"
                                % (door, arm))

    # ------------------------------------------------ contrary debt, by mode

    def test_superseded_takes_a_contrary_a_later_approve_answered(self):
        """A FIX merged to trunk, then answered by a later APPROVE on the same
        chain whose tip landed — the one mode `superseded` exists for."""
        first, _second, fixed = self.superseded_world()
        out = self.contrary_sentences(first, "superseded", tip=fixed)
        self.assertEqual(out["reason"], "superseded")
        err = self.on_trunk_sentence(first, "superseded")
        self.assertIn("(ancestor)", err)

    def test_carried_takes_a_rebase_landed_contrary(self):
        """A FIX whose work reached trunk under a rewritten sha, dispatched
        with no chain link to its cure. `superseded` REFUSES it, which is why
        no sentence offers `superseded` alone for contrary debt, and
        `carried` admits."""
        base, reviewed = self.lane_and_rebase_land()
        row = self.chained_fix_row(base, reviewed)
        err = self.refuses(row, "superseded")
        self.assertIn("no later APPROVE verdict", err)
        self.contrary_sentences(row, "carried")
        self.on_trunk_sentence(row, "carried")

    def test_resolved_takes_a_confirmed_ancestry_contrary(self):
        """A FIX merged by ANCESTRY and confirmed by a later cross-family
        review stating the resolution. `superseded` refuses it: the
        confirmation is not on the FIX's chain."""
        original, confirmation = self.resolved_pair(polarity="fix")
        with self.resolved_families():
            err = self.refuses(original, "superseded")
            self.assertIn("no later APPROVE verdict", err)
            self.contrary_sentences(original, "resolved",
                                    evidence=confirmation["id"][:12])
            self.on_trunk_sentence(original, "resolved")

    def test_withdrawn_takes_a_contrary_that_never_reached_trunk(self):
        """A FIX whose tip is off trunk. Two more refusals offer `withdrawn`
        for it, and both are driven here: `resolved` calls the absent work
        "withdrawn's question", and `dispatch cancel` sends a FIX whose
        artifact should not exist to `--reason withdrawn` while the object
        reads."""
        row = self.absent_fix_row()
        err = self.refuses(row, "resolved", evidence="none")
        self.assertIn("withdrawn's question", err)
        _out, why = dispatches.mark_cancel(row["id"], "artifact deleted")
        self.assertIn("which it can while that object still reads", why)
        self.assertEqual(self.offered(why), ["withdrawn", "stranded"], why)
        self.contrary_sentences(row, "withdrawn")

    def test_stranded_takes_a_contrary_whose_tip_gc_pruned(self):
        """A FIX whose reviewed object gc pruned. `withdrawn` REFUSES it —
        the absence of an object this clone cannot read is UNKNOWN — so
        `dispatch cancel`'s advice must say that and offer `stranded`, which
        admits."""
        row, _tip = self.pruned_verdict_row(polarity="fix",
                                            lane="lane/offered-pruned-fix")
        _out, why = dispatches.mark_cancel(row["id"], "artifact deleted")
        self.assertIn("withdrawn refuses", why)
        self.assertEqual(self.offered(why), ["withdrawn", "stranded"], why)
        err = self.refuses(row, "withdrawn")
        self.assertIn("FAIL-CLOSED", err)
        self.contrary_sentences(row, "stranded",
                                evidence="gc pruned the deleted artifact")

    def test_a_chain_declared_contrary_gets_the_same_doors(self):
        """The row's own polarity is UNDECLARED and a chained round declares
        the FIX. `landed` refuses it through its second sentence, which ends
        on the same list, and the door this mode has — the work is off
        trunk — is `withdrawn`."""
        row = self.verdict_row(None, lane="lane/offered-chain")
        declarer, why, _sent = dispatches.send(
            "seat-c", "lane/offered-chain-fixdecl", "declare fix", self.side,
            repo=self.repo, key="key-offered-chain-fixdecl", sign=False,
            supersedes=row["id"])
        self.assertIsNone(why)
        self.assertTrue(declarer["id"])
        declared, err = self.mark_verdict(declarer["id"], self.side,
                                          "guard must not land",
                                          polarity="fix")
        self.assertIsNone(err)
        self.assertEqual(declared["status"], "verdict")
        err = self.refuses(row, "landed")
        self.assertIn("carries a chain-declared FIX", err)
        self.assertEqual(self.offered(err), self.CONTRARY, err)
        self.admits(row, "withdrawn")

    # ------------------------------------------ work on trunk, by verdict

    def test_landed_takes_an_approve_a_withdraw_found_on_trunk(self):
        """An APPROVE merged to trunk. `superseded` refuses it, so
        `withdrawn`'s refusal cannot offer `superseded` for every verdict,
        and `landed` admits. `endorsement-moot` calls the same row landed's,
        and is held to it on the same row."""
        row = self.verdict_row("approve", lane="lane/offered-approve")
        self.git("merge", "--no-edit", "-q", "side")
        err = self.refuses(row, "superseded")
        self.assertIn("no later APPROVE verdict", err)
        self.on_trunk_sentence(row, "landed")
        err = self.refuses(row, "endorsement-moot")
        self.assertIn("An approve over landed work is landed's row.", err)
        self.admits(row, "landed")

    def test_endorsement_moot_takes_a_concur_on_trunk_by_ancestry(self):
        """A CONCUR on trunk by ANCESTRY is `endorsement-moot`'s, and one on
        trunk only by PATCH IDENTITY is refused there — which is why the
        sentence carries the condition rather than offering the door for
        every concur."""
        row = self.dispatch(ref=self.b, lane="lane/offered-concur",
                            kind="review")
        self.mark_verdict(row["id"], self.b, "reviewed", polarity="concur")
        self.on_trunk_sentence(row, "endorsement-moot")
        self.admits(row, "endorsement-moot")
        _base, reviewed = self.lane_and_rebase_land()
        copied = self.dispatch(ref=reviewed, lane="lane/offered-concur-pe",
                               kind="review")
        self.mark_verdict(copied["id"], reviewed, "reviewed",
                          polarity="concur")
        err = self.on_trunk_sentence(copied, "endorsement-moot")
        self.assertIn("(patch-equivalent)", err)
        self.assertIn("whose tip is on trunk by ANCESTRY", err)
        err = self.refuses(copied, "endorsement-moot")
        self.assertIn("PATCH IDENTITY", err)

    def test_the_landed_outcome_superseded_names_is_landeds(self):
        """`superseded` over an APPROVE whose own tip is on trunk says "use
        --reason landed". Measured true: `landed` admits that row."""
        first, _second, fixed = self.superseded_world(polarity="approve")
        err = self.refuses(first, "superseded", tip=fixed)
        self.assertIn("landed outcome, not a supersession", err)
        self.assertEqual(self.offered(err), ["landed"], err)
        self.admits(first, "landed")

    def test_an_undeclared_verdict_over_landed_work_is_landeds(self):
        """`endorsement-moot` refuses an undeclared verdict. Calling that row
        `discharged`'s would be false — `discharged` refuses every verdict
        row — and `landed` admits it."""
        row = self.verdict_row(None, ref=self.b,
                               lane="lane/offered-undeclared")
        err = self.refuses(row, "endorsement-moot")
        self.assertIn("An undeclared verdict over landed work is landed's "
                      "row.", err)
        self.assertEqual(self.offered(err), self.CONTRARY, err)
        err = self.refuses(row, "discharged")
        self.assertIn("is a verdict row", err)
        self.admits(row, "landed")

    def test_an_absent_concur_is_withdrawns_or_expireds(self):
        """`endorsement-moot` sends an ABSENT concur to "withdrawn's
        question, or expired's". Both admit it."""
        row = self.dispatch(ref=self.side, lane="lane/offered-absent-concur",
                            kind="review")
        self.mark_verdict(row["id"], self.side, "reviewed",
                          polarity="concur")
        err = self.refuses(row, "endorsement-moot")
        self.assertIn("withdrawn's question, or expired's", err)
        self.admits(row, "withdrawn")
        self.admits(row, "expired")

    # ----------------------------------- a translation, and what follows it

    def test_landed_takes_an_approve_whose_translation_reached_trunk(self):
        """A reviewed tip gc pruned, recorded as rewritten onto trunk. Two
        refusals offer `landed` for it — `withdrawn` over landed work, and
        `stranded` over a live translation — and both offer it for the
        verdicts it takes. Measured both ways: `landed` admits the APPROVE
        and refuses the FIX as a contrary, so the FIX is offered nothing."""
        row, withdraw, strand = self.translated_refusals("approve")
        self.assertEqual(self.offered(withdraw), ["landed"], withdraw)
        self.assertEqual(self.offered(strand), ["landed"], strand)
        self.assertEqual(self.admits(row, "landed")["reason"], "landed")
        row, withdraw, strand = self.translated_refusals("fix")
        self.assertEqual(self.offered(withdraw), ["landed"], withdraw)
        self.assertEqual(self.offered(strand), ["landed"], strand)
        err = self.refuses(row, "landed")
        self.assertIn("CONTRARY, not a resolution", err)

    def translated_refusals(self, polarity):
        """(row, withdrawn's refusal, stranded's refusal) over a reviewed tip
        gc pruned and recorded as rewritten onto trunk."""
        row, tip = self.pruned_verdict_row(
            polarity=polarity, lane="lane/offered-translated-" + polarity)
        self.sidecar(tip, self.b)
        withdraw = self.refuses(row, "withdrawn")
        self.assertIn("through its recorded translation", withdraw)
        self.assertIn("for any other verdict no door is promised here",
                      withdraw)
        strand = self.refuses(row, "stranded", evidence="pruned")
        self.assertIn("translates to live object", strand)
        self.assertIn("for a FIX or SUPERSEDE no door is promised here",
                      strand)
        return row, withdraw, strand

    def test_a_destroyed_translation_is_strandeds(self):
        """`superseded`'s translated door calls a destroyed translation
        "stranded's question". Measured true: `stranded` admits the row."""
        row, _tip, _new = self.translated_pruned_row(preserve=False)
        _second, fixed = self.approved_superseder(row)
        err = self.refuses(row, "superseded", tip=fixed)
        self.assertIn("is itself destroyed — this is stranded's question",
                      err)
        self.admits(row, "stranded", evidence="the rewrite destroyed both")

    def test_a_live_unpreserved_translation_is_offered_no_door(self):
        """A translation that is still a LIVE object, under no archive ref.
        Calling it "stranded's question" would be false: `stranded` refuses a
        live translation, and `landed` refuses it off trunk. So the sentence
        offers nothing, and says so."""
        row = self.unstamped_row(lane="lane/pre-stamp")
        tip = self.side
        self.git("checkout", "-q", "-b", "rewritten", self.a)
        new = self.commit("rewritten cargo", path="h")
        self.git("checkout", "-q", self.main)
        self.prune(tip, "refs/heads/side")
        self.sidecar(tip, new)
        _second, fixed = self.approved_superseder(row)
        err = self.refuses(row, "superseded", tip=fixed)
        self.assertIn("preserved under no archive/rescue ref", err)
        self.assertIn("no door is promised here", err)
        self.assertEqual(self.offered(err), [], err)
        err = self.refuses(row, "stranded", evidence="pruned")
        self.assertIn("translates to live object %s" % new[:12], err)
        err = self.refuses(row, "landed")
        self.assertIn("recorded translation", err)

    def translated_on_trunk(self, polarity):
        """(row, superseder tip) — the pre-stamp shape `superseded`'s
        translated door reads: a verdict row of `polarity` with its author,
        sender and chain stripped, its reviewed tip gc pruned, the recorded
        translation archived and MERGED to trunk, and a later approved
        superseder landed."""
        row = self.verdict_row(polarity, lane="lane/pre-stamp")
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for event in events:
                if event.get("id") == row["id"]:
                    for field in ("author", "sender", "chain_root",
                                  "supersedes"):
                        event.pop(field, None)
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
        tip = self.side
        self.git("checkout", "-q", "-b", "rewritten", self.a)
        new = self.commit("rewritten cargo", path="h")
        self.git("checkout", "-q", self.main)
        self.git("tag", "archive/pre-stamp-rescue", new)
        self.prune(tip, "refs/heads/side")
        self.sidecar(tip, new)
        self.git("merge", "--no-edit", "-q", "rewritten")
        _second, fixed = self.approved_superseder(row)
        return row, fixed

    def translated_sentences(self, row, fixed):
        """`superseded`'s translated door and `stranded` both refuse this
        row and both offer `landed`, and nothing else."""
        err = self.refuses(row, "superseded", tip=fixed)
        self.assertIn("translated change itself IS on trunk", err)
        self.assertEqual(self.offered(err), ["landed"], err)
        self.assertIn("it refuses a FIX or SUPERSEDE as a contrary", err)
        strand = self.refuses(row, "stranded", evidence="pruned")
        self.assertIn("translates to live object", strand)
        self.assertEqual(self.offered(strand), ["landed"], strand)
        return err, strand

    def test_a_translation_on_trunk_is_landeds_for_an_undeclared_verdict(self):
        """`superseded`'s translated door takes any polarity, and `landed`
        does not. Both refusals over this shape offer `landed` for an
        undeclared verdict, and it admits one."""
        row, fixed = self.translated_on_trunk(None)
        err, _strand = self.translated_sentences(row, fixed)
        self.assertIn("admits an APPROVE or an undeclared verdict", err)
        self.assertEqual(self.admits(row, "landed")["reason"], "landed")

    def test_a_translation_on_trunk_is_landeds_for_an_approve(self):
        row, fixed = self.translated_on_trunk("approve")
        err, _strand = self.translated_sentences(row, fixed)
        self.assertIn("admits an APPROVE or an undeclared verdict", err)
        self.assertEqual(self.admits(row, "landed")["reason"], "landed")

    def test_a_translation_on_trunk_offers_a_fix_nothing(self):
        """The FIX over the same shape: `landed` refuses it as a contrary,
        and both refusals say no door is promised for one."""
        row, fixed = self.translated_on_trunk("fix")
        err, strand = self.translated_sentences(row, fixed)
        self.assertIn("no door is promised here for one", err)
        self.assertIn("for a FIX or SUPERSEDE no door is promised here",
                      strand)
        err = self.refuses(row, "landed")
        self.assertIn("CONTRARY, not a resolution", err)

    # ------------------------------------------------ a row nobody reviewed

    def test_discharged_is_offered_with_the_landing_it_reads(self):
        """A BUILD row whose approved successor is MERGED and not yet closed.
        `out-of-scope` finds the work on trunk by asking git and offers
        `discharged`, which reads the successor's RECORDED landing: it
        refuses until that successor is closed landed, then admits. So the
        offer carries that condition. `landed` takes the BUILD row through
        its approved review at every step."""
        build = self.dispatch(ref=self.a, lane="lane/offered-build",
                              kind="build")
        review = dispatches.add(
            "seat-b", "lane/offered-build-review", ref=self.b,
            repo=self.repo, kind="review", notify=False,
            supersedes=build["id"])
        approved, err = self.mark_verdict(review["id"], self.b, "clean",
                                          polarity="approve")
        self.assertIsNone(err)
        self.assertEqual(approved["status"], "verdict")
        err = self.refuses(build, "out-of-scope")
        self.assertIn("its work is ON TRUNK", err)
        self.assertIn("once %s is itself closed landed" % review["id"][:12],
                      err)
        self.assertEqual(self.offered(err), ["discharged"], err)
        for reason in ("stranded", "expired"):
            err = self.refuses(build, reason)
            self.assertEqual(self.offered(err), self.MOOT, err)
            self.assertIn("refuses work that SHIPPED", err)
        for reason in ("resolved", "endorsement-moot"):
            err = self.refuses(build, reason, **(
                {"evidence": "none"} if reason == "resolved" else {}))
            self.assertEqual(self.offered(err), self.UNVERDICTED, err)
        err = self.refuses(build, "discharged")
        self.assertIn("no successor on it carries a landed", err)
        self.admits(build, "landed")
        closed, err = landreq.close(review["id"], "landed", live=True)
        self.assertIsNone(err)
        self.assertEqual(closed["close_reason"], "landed")
        self.assertEqual(self.admits(build, "discharged")["reason"],
                         "discharged")
        self.admits(build, "landed")

    def test_a_same_tip_peer_discharges_once_it_is_closed_landed(self):
        """The TIP tier of the same offer. The peer's landed close is written
        with the internal `fan_out=False`, because a real sweep would close
        this very row through `discharged` and leave nothing to ask."""
        row = self.dispatch(ref=self.b, lane="lane/offered-peer-a",
                            kind="review")
        peer = self.dispatch(ref=self.b, lane="lane/offered-peer-b",
                             kind="review")
        approved, err = self.mark_verdict(peer["id"], self.b, "clean",
                                          polarity="approve")
        self.assertIsNone(err)
        self.assertEqual(approved["status"], "verdict")
        err = self.refuses(row, "out-of-scope")
        self.assertIn("once %s is itself closed landed" % peer["id"][:12],
                      err)
        self.refuses(row, "discharged")
        closed, err = landreq.close(peer["id"], "landed", live=True,
                                    fan_out=False)
        self.assertIsNone(err)
        self.assertEqual(closed["close_reason"], "landed")
        self.assertEqual(self.admits(row, "discharged")["reason"],
                         "discharged")

    def test_an_open_rows_moot_obligation_is_out_of_scopes(self):  # noqa: VACUOUS_ASSERTION — the five offered-list comparisons are against the class's non-empty pins, which the rung reads as bare equalities; the refusal they read is asserted positively by `refuses` (assertTrue) and the discharged refusal's text below, and the admission asserts would_append True
        """A plain open review with no successor. Three refusals offer
        `out-of-scope` for it and it admits; `discharged`, which is offered
        only with its condition, refuses, because nothing carried the
        work."""
        row = self.dispatch(lane="lane/offered-open")
        err = self.refuses(row, "landed")
        self.assertEqual(self.offered(err), self.MOOT, err)
        err = self.refuses(row, "stranded")
        self.assertEqual(self.offered(err), self.MOOT, err)
        err = self.refuses(row, "expired")
        self.assertEqual(self.offered(err), self.MOOT, err)
        err = self.refuses(row, "resolved", evidence="none")
        self.assertEqual(self.offered(err), self.UNVERDICTED, err)
        err = self.refuses(row, "endorsement-moot")
        self.assertEqual(self.offered(err), self.UNVERDICTED, err)
        err = self.refuses(row, "discharged")
        self.assertIn("nothing on the ledger records what discharged", err)
        self.assertEqual(self.admits(row, "out-of-scope")["reason"],
                         "out-of-scope")

    def test_out_of_scope_refuses_a_live_lane_as_its_offer_says(self):
        """The offer says `out-of-scope` refuses work still LIVE on a
        lane-family ref and says which. Measured: it does."""
        row = self.dispatch(lane="lane/offered-live")
        self.git("branch", "lane/offered-live", "side")
        err = self.refuses(row, "landed")
        self.assertIn("still LIVE on a lane-family ref", err)
        err = self.refuses(row, "out-of-scope")
        self.assertIn("live at refs/heads/lane/offered-live", err)


class CloseLandedFansOutToSameTipPeersTest(CloseBase):
    """ONE LAND CLOSES EVERY ROW BOUND TO THE SAME COMMIT.

    THE OWNER'S QUESTION, verbatim: "even if new reviews create
    new rows, when something lands they should all close, no?" He was reading
    the pipeline view, where ONE piece of work rendered as three separate live
    rows at three different ages. Measured the same morning: 46 live rows over
    34 distinct lanes — 12 rows of pure double-count, and lane
    `project-raw-batch-is-unproven-end-to-end` carried THREE live rows, ALL
    bound to ONE tip, in three DIFFERENT chains (a deliberate second
    cross-family leg, plus one re-ask nobody needed).

    So this fixture is that board, minimally: one landing row, one same-tip
    peer in ANOTHER chain, one same-tip peer in the SAME chain that never got
    a verdict, one same-tip peer carrying FIX (debt, not duplication), and one
    same-LANE peer at a DIFFERENT tip (a lane is a LABEL; two efforts may
    reuse one and must not close together).

    Every arm asserts the closed state EXISTS on the ledger snapshot —
    `close_reason` present and named — never that no exception was raised.
    """

    def peer(self, lane, tip, recipient, polarity="approve", supersedes=None,
             kind="review"):
        """One dispatched review row on `tip`, optionally verdicted.

        `force=True` because the duplicate-mint guard REFUSES a second
        not-closed row on the same lane in the same repo — which is the exact
        board shape under test, and in production these are minted with the
        same deliberate override (a parallel cross-family leg is a legitimate
        fork). Without it this fixture cannot be built at all.

        `kind="review"` is stated because it is LOAD-BEARING, not decoration.
        A row that never got a verdict has no `reviewed_tip`, so its bound
        commit can only be read off `ref` — and `ref` on a BUILD row is a
        BASE, not reviewed content. UNKNOWN kind therefore binds nothing
        (asserted below). The first cut of this fixture left kind unset and
        the never-verdicted peer silently fell out of the sweep.
        """
        row, why, sent = dispatches.send(
            recipient, lane, "review " + lane, tip, repo=self.repo,
            key="key-%s-%s" % (lane, recipient), sign=False, kind=kind,
            new_work=supersedes is None, supersedes=supersedes, force=True)
        self.assertIsNone(why)
        self.assertTrue(sent)
        if polarity:
            _out, err = self.mark_verdict(row["id"], tip, "findings",
                                                polarity=polarity)
            self.assertIsNone(err)
        return row

    def staged(self):
        """(landing, same-chain unreviewed, other-chain approve, fix, other-tip).

        The reviewed tip is `self.side`, merged to trunk here so the landed
        ladder's ancestry proof is real rather than mocked.
        """
        lane = "lane/one-unit-many-rows"
        landing = self.peer(lane, self.side, "seat-a")
        same_chain = self.peer(lane, self.side, "seat-b", polarity=None,
                               supersedes=landing["id"])
        other_chain = self.peer(lane, self.side, "seat-c")
        debt = self.peer(lane, self.side, "seat-d", polarity="fix")
        # SAME LANE, DIFFERENT TIP — the case that must NOT close. `self.c` is
        # on trunk already, so if anything here closed it, it would be the
        # lane rule doing it and not the tip rule.
        other_tip = self.peer(lane, self.c, "seat-e")
        self.git("merge", "--no-edit", "-q", "side")
        return landing, same_chain, other_chain, debt, other_tip

    def reason_of(self, rid):
        snap, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        return (snap[rid] or {}).get("close_reason")

    def test_landing_one_row_closes_every_same_tip_peer(self):  # noqa: VACUOUS_ASSERTION — the two assertIsNone survivors are read through `reason_of`, and the two assertEqual lines directly above them read the SAME helper on the SAME snapshot and find "landed"/"discharged"; a helper that could only ever answer None cannot produce those
        landing, same_chain, other_chain, debt, other_tip = self.staged()
        # PRE-STATE, so a later "closed" reading cannot be a fixture that was
        # already closed before the verb ran.
        for row in (landing, same_chain, other_chain, debt, other_tip):
            self.assertIsNone(self.reason_of(row["id"]),
                              "fixture row %s starts live" % row["id"][:12])
        out, err = landreq.close(landing["id"], "landed",
                                 evidence="folded", live=True)
        self.assertIsNone(err)
        self.assertEqual(out["close_reason"], "landed")
        # THE HEADLINE: both same-tip peers are terminal, by NAME.
        self.assertEqual(self.reason_of(other_chain["id"]), "landed",
                         "a same-tip peer in ANOTHER chain must close")
        self.assertEqual(self.reason_of(same_chain["id"]), "discharged",
                         "a same-tip peer that never got a verdict must close")
        # AND THE TWO THAT MUST SURVIVE.
        self.assertIsNone(self.reason_of(debt["id"]),
                          "a FIX verdict on the same tip is DEBT — its "
                          "findings are not discharged by the land")
        self.assertIsNone(self.reason_of(other_tip["id"]),
                          "a row sharing only the LANE LABEL is separate work")

    def test_the_close_result_names_every_row_it_closed(self):
        """A fan-out nobody can read is as unusable as no fan-out: the owner's
        board and the operator both need to know which rows left."""
        landing, same_chain, other_chain, debt, _other = self.staged()
        out, err = landreq.close(landing["id"], "landed",
                                 evidence="folded", live=True)
        self.assertIsNone(err)
        self.assertEqual(
            sorted(p["id"] for p in out["closed_siblings"]),
            sorted([same_chain["id"], other_chain["id"]]))
        self.assertEqual([p["id"] for p in out["sibling_refusals"]],
                         [debt["id"]])
        self.assertIn("FIX", out["sibling_refusals"][0]["why"])

    def test_the_dry_run_names_the_peers_and_appends_nothing(self):  # noqa: VACUOUS_ASSERTION — the unchanged-line-count assertion is followed UNCONDITIONALLY by a real close on the same row, read through the same counter, asserted GREATER; a counter that could not move cannot satisfy both
        landing, same_chain, other_chain, debt, _other = self.staged()
        with open(dispatches.ledger_path(), encoding="utf-8") as fh:
            before = sum(1 for _line in fh)
        out, err = landreq.close(landing["id"], "landed", evidence="folded",
                                 live=True, dry_run=True)
        self.assertIsNone(err)
        # THE PREVIEW SPLITS THE SET THE WAY THE WRITE DOES: a rehearsal that
        # promises to close the FIX peer rehearses an action the write refuses.
        self.assertEqual(sorted(out["would_close_siblings"]),
                         sorted([same_chain["id"], other_chain["id"]]))
        self.assertEqual(out["would_leave_open_siblings"], [debt["id"]])
        with open(dispatches.ledger_path(), encoding="utf-8") as fh:
            after = sum(1 for _line in fh)
        self.assertEqual(after, before)
        # POSITIVE CONTROL on the counter: the real close moves it.
        _out, err = landreq.close(landing["id"], "landed", evidence="folded",
                                  live=True)
        self.assertIsNone(err)
        with open(dispatches.ledger_path(), encoding="utf-8") as fh:
            self.assertGreater(sum(1 for _line in fh), before)

    def test_a_same_tip_peer_in_ANOTHER_REPO_is_never_touched(self):  # noqa: VACUOUS_ASSERTION — `assertIn(same_chain)` two lines above the assertNotIn is the unconditional positive control on the SAME list from the SAME call; an empty result fails it first
        """The same sha in two repositories is a coincidence, not a relation.

        THE SELECTOR IS THE SUBJECT, deliberately, and the first cut of this
        arm proves why: it re-pointed the whole `dispatches.snapshot` seam and
        drove a real close through it. That snapshot is also what the DISCHARGE
        door re-walks, so the doctored (pre-close) view made every peer refuse
        and the sweep returned []. The must-hit fired on my own fixture — the
        arm was measuring a broken double, not the repo filter.
        """
        landing, same_chain, other_chain, _debt, _other = self.staged()
        snap, _un = dispatches.snapshot()
        self.assertEqual(snap[other_chain["id"]]["repo_id"], self.gitdir(),
                         "the control peer must start in THIS repo")
        foreign = dict(snap, **{other_chain["id"]: dict(
            snap[other_chain["id"]], repo_id="/elsewhere/.git")})
        with mock.patch.object(landreq.dispatches, "snapshot",
                               return_value=(foreign, None)):
            peers, unavailable = landreq._same_tip_siblings(
                landreq.get(landing["id"])[0], self.side)
        self.assertIsNone(unavailable)
        ids = [str(p["id"]) for p in peers]
        # MUST-HIT ON THE SAME CALL: the same-chain peer is untouched by the
        # doctoring and MUST be selected, so an empty list can never be read
        # as "the repository filter worked".
        self.assertIn(same_chain["id"], ids)
        self.assertNotIn(other_chain["id"], ids)

    def test_a_retry_finishes_a_sweep_that_was_left_partial(self):
        """The sweep is FAIL-OPEN, so a peer can survive a transient refusal.
        If the only verb that sweeps refuses to run twice, the repair is
        closing each straggler by hand — the exact state this change ends. So
        the idempotent retry re-sweeps, while the row's own close is untouched.
        """
        landing, same_chain, other_chain, _debt, _o = self.staged()
        # The land happens with the sweep suppressed: this is what a partial
        # fan-out leaves behind.
        out, err = landreq.close(landing["id"], "landed", evidence="folded",
                                 live=True, fan_out=False)
        self.assertIsNone(err)
        self.assertEqual(out["close_reason"], "landed")
        self.assertIsNone(self.reason_of(other_chain["id"]),
                          "the suppressed sweep must actually leave it open")
        first = self.close_event(landing["id"])
        out, err = landreq.close(landing["id"], "landed", evidence="folded",
                                 live=True)
        self.assertIsNone(err)
        self.assertEqual(self.reason_of(other_chain["id"]), "landed")
        self.assertEqual(self.reason_of(same_chain["id"]), "discharged")
        # AND THE ROW ITSELF APPENDED NOTHING SECOND TIME: same close event,
        # byte for byte, so the retry repaired the sweep and not the closure.
        self.assertEqual(self.close_event(landing["id"]), first)

    def test_a_dry_run_on_an_ALREADY_landed_row_writes_nothing(self):  # noqa: VACUOUS_ASSERTION — the unchanged line count and both assertIsNone peers are followed UNCONDITIONALLY by the SAME call without --dry-run, which is asserted to move the counter AND to fill close_reason on the same peer; a counter that could not move and a reader that could only answer None cannot satisfy both halves
        """THE IDEMPOTENT BRANCH HONOURS --dry-run, and this arm exists because
        the first cut of the retry sweep did not. It reached the sweep past the
        `dry_run` every other branch checks, so a rehearsal on an already-landed
        row would have issued REAL closes on its peers. Found by running the
        verb against the live board, not by re-reading the code."""
        landing, same_chain, other_chain, _debt, _o = self.staged()
        out, err = landreq.close(landing["id"], "landed", evidence="folded",
                                 live=True, fan_out=False)
        self.assertIsNone(err)
        self.assertEqual(out["close_reason"], "landed")   # it IS landed now
        with open(dispatches.ledger_path(), encoding="utf-8") as fh:
            before = sum(1 for _line in fh)
        out, err = landreq.close(landing["id"], "landed", evidence="folded",
                                 live=True, dry_run=True)
        self.assertIsNone(err)
        self.assertEqual(sorted(out["would_close_siblings"]),
                         sorted([same_chain["id"], other_chain["id"]]))
        with open(dispatches.ledger_path(), encoding="utf-8") as fh:
            self.assertEqual(sum(1 for _line in fh), before)
        # THE PEERS ARE STILL OPEN, by name — the observable a line count
        # cannot see if some other append had happened to balance it.
        self.assertIsNone(self.reason_of(other_chain["id"]))
        self.assertIsNone(self.reason_of(same_chain["id"]))
        # POSITIVE CONTROL, unconditional and on both observables at once: the
        # SAME call without --dry-run closes them and moves the counter.
        _out, err = landreq.close(landing["id"], "landed", evidence="folded",
                                  live=True)
        self.assertIsNone(err)
        self.assertEqual(self.reason_of(other_chain["id"]), "landed")
        with open(dispatches.ledger_path(), encoding="utf-8") as fh:
            self.assertGreater(sum(1 for _line in fh), before)

    def test_an_UNKNOWN_kind_row_binds_nothing_and_is_left_alone(self):  # noqa: VACUOUS_ASSERTION — `declared` is the unconditional positive control on the SAME observable (the closed_siblings list and `reason_of`), minted in the same fixture at the same tip and asserted present two lines above
        """FAIL CLOSED ON UNKNOWN. `ref` means REVIEWED TIP on a review row and
        BASE on a build row; a row that declares neither cannot say which, and
        a base that happens to equal a landed reviewed tip is not landed work.
        So a kind-less row with no verdict binds nothing and stays open."""
        lane = "lane/one-unit-many-rows"
        landing = self.peer(lane, self.side, "seat-a")
        unknown = self.peer(lane, self.side, "seat-b", polarity=None,
                            kind=None)
        # MUST-HIT CONTROL: a peer that IS declared review, same tip, same
        # fixture — so an empty sweep cannot be read as "the sweep is broken".
        declared = self.peer(lane, self.side, "seat-c", polarity=None)
        self.git("merge", "--no-edit", "-q", "side")
        snap, _un = dispatches.snapshot()
        self.assertIsNone(snap[unknown["id"]].get("kind"),
                          "the fixture must actually record an UNKNOWN kind")
        out, err = landreq.close(landing["id"], "landed", evidence="folded",
                                 live=True)
        self.assertIsNone(err)
        closed = [p["id"] for p in out.get("closed_siblings") or ()]
        self.assertIn(declared["id"], closed)          # the control fired
        self.assertNotIn(unknown["id"], closed)
        self.assertIsNone(self.reason_of(unknown["id"]))

    def test_the_discharged_close_replays_under_the_TIP_tier(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone on the validator is followed UNCONDITIONALLY by two forged variants of the SAME event through the SAME call, both asserted to be refused; a validator that always answered None fails those
        """The peer with no verdict closes through #177's door on the new tier,
        and the event it wrote must survive the replay validator — otherwise
        the row is closed today and unreadable tomorrow."""
        landing, same_chain, _other, debt, _o = self.staged()
        pre_current, pre_verdicts, unavailable = \
            dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        pre_state = pre_current[same_chain["id"]]
        _out, err = landreq.close(landing["id"], "landed", evidence="folded",
                                  live=True)
        self.assertIsNone(err)
        event = self.close_event(same_chain["id"])
        self.assertEqual(event["close_reason"], "discharged")
        self.assertIn(event["discharge_tier"], dispatches.DISCHARGE_TIERS)
        current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        # REPLAY'S OWN VANTAGE, and getting it wrong is what this comment is
        # for. Replay hands the validator the row's state BEFORE the event,
        # against the projection as it stood AT that event — so the landing
        # row is already closed (it is, in `current`) while THIS row is not
        # yet. Feeding the post-close `current` unmodified makes the walk say
        # "row is already terminal", which measures the fixture's vantage and
        # nothing about the tier.
        at_event = dict(current, **{same_chain["id"]: pre_state})
        self.assertIsNotNone(pre_state)   # the pre-close state was real
        self.assertIsNone(dispatches._close_event_error(
            event, pre_state, current=at_event, verdicts=verdicts))
        # POSITIVE CONTROL ON THE VALIDATOR ITSELF, unconditional and on the
        # same observable. A validator that returned None for everything would
        # satisfy the line above just as happily, so the same call is made
        # against the writer's own event with the TIER corrupted and with the
        # DISCHARGING ID pointed at a row that discharges nothing. Both must
        # be refused BY NAME.
        bad_tier = dispatches._close_event_error(
            dict(event, discharge_tier="lane"), pre_state,
            current=at_event, verdicts=verdicts)
        self.assertIn("discharge tier must be", str(bad_tier))
        bad_id = dispatches._close_event_error(
            dict(event, discharging_id=debt["id"]), pre_state,
            current=at_event, verdicts=verdicts)
        self.assertIsNotNone(bad_id)

