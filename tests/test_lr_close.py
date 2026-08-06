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
import json
import os
import subprocess
import time
import unittest
from unittest import mock

from helm import dispatches, eventledger, landreq, proxywatch, seats
from tests.test_landreq import LandReqBase, run


class CloseBase(LandReqBase):
    """Fixture verbs shared by every close-reason suite."""

    def gitdir(self):
        return os.path.realpath(os.path.join(self.repo, ".git"))

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
            out, err = dispatches.mark_verdict(row["id"], tip, "findings",
                                               polarity=polarity)
            self.assertIsNone(err)
        return row

    def history_len(self, rid):
        return len(dispatches.history(rid))

    def prune(self, sha, *refs):
        """Destroy `sha` for real: drop the named refs, expire every reflog,
        gc — then POSITIVELY assert the prune with a bare cat-file (rc 1),
        the MUST-HIT for the fixture itself. No cat-file mocking, ever."""
        for ref in refs:
            self.git("update-ref", "-d", ref)
        self.git("reflog", "expire", "--expire-unreachable=now", "--all")
        self.git("gc", "--prune=now", "--quiet")
        p = subprocess.run(["git", "--git-dir", self.gitdir(),
                            "cat-file", "-e", sha], capture_output=True)
        self.assertEqual(p.returncode, 1,
                         "the prune recipe MUST leave %s missing (rc 1), got "
                         "rc %d" % (sha[:12], p.returncode))

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

    def sidecar(self, old, new):
        """Record one ref translation exactly as migrate_refs --apply does."""
        path = os.path.join(landreq.home.global_dir(), ".state",
                            "ref-migrations.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": dispatches.pk.now_ts(),
                                "repo": self.gitdir(), "source": "test",
                                "id": "x", "field": "reviewed_tip",
                                "old": old, "new": new}) + "\n")
        return path

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
        out, err = dispatches.mark_verdict(
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
                out, err = dispatches.mark_verdict(
                    confirmation["id"], confirmation_tip, evidence,
                    polarity=confirmation_polarity)
        else:
            out, err = dispatches.mark_verdict(
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
        proof = {"v": 1, "session": session, "agent_pid": 4101,
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
        proofs = getattr(self, "_proxy_family_proofs", {})
        proofs[session] = ("ds4pro", proof, None)
        self._proxy_family_proofs = proofs

    def proxy_families(self):
        proofs = getattr(self, "_proxy_family_proofs", {})
        return mock.patch.object(
            proxywatch, "proxy_runtime_snapshot",
            side_effect=lambda session: proofs.get(
                session, (None, None, "no session-matched proof")))


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
        self.assertEqual(set(event), dispatches._DELIVERED_REPORT_EVENT_FIELDS)

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

    def test_approval_tier_is_scoped_to_the_row_repository(self):
        target, confirmation, _tip = self.subsumption_case()
        seen = []

        def tier(recipient, repo=None):
            seen.append((recipient, repo))
            return "none", "test has no tier"
        with mock.patch.object(dispatches, "approval_tier", side_effect=tier):
            out, err = self.close_case(target, confirmation)
        self.assertIsNone(err)
        self.assertEqual(out["close_reason"], "subsumed")
        self.assertTrue(seen, "approval tier was never consulted")
        self.assertTrue(all(repo == self.gitdir() for _recipient, repo in seen),
                        seen)

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
        _out, why = dispatches.mark_verdict(
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

    def test_withdrawn_polarity_gate_holds_at_the_boundary(self):
        approve = self.verdict_row("approve", lane="lane/approve-nw")
        before = self.history_len(approve["id"])
        _out, err = dispatches._record_close_proven(
            approve["id"], "withdrawn", self.side, evidence="attested",
            absence_trunk_ref="refs/heads/" + self.main,
            absence_trunk_sha=self.git("rev-parse", self.main))
        self.assertIn("polarity outside", err)
        self.assertEqual(self.history_len(approve["id"]), before)

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
        _out, err = dispatches.mark_verdict(
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
        _out, err = dispatches.mark_verdict(
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
        row = self.verdict_row("approve")
        rc, _out, err = run(["close", row["id"]])
        self.assertEqual(rc, 2)
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
        Measured live: a hardened '..' traversal escape landed and was closed
        COMPLETED having never been installed. Our definition of done does not
        contain the step that makes a guard real."""
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
            self.assertIn("--reason superseded", err)
            self.assertEqual(self.history_len(row["id"]), before)

    def test_open_rows_refuse_landed_with_the_redirect(self):
        row = self.dispatch(lane="lane/open-landed")
        before = self.history_len(row["id"])
        _out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIn("has no verdict", err)
        self.assertIn("out-of-scope", err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_trunk_is_pinned_at_ladder_entry(self):
        """D7: the whole ladder runs against ONE sha resolved at entry — a
        trunk that moves mid-ladder cannot change what gets recorded."""
        row = self.landed_row()
        pinned_before = self.git("rev-parse", self.main)
        real = landreq._landing_proof
        moved = []

        def moving(gitdir, tip, ref):
            # only the LADDER call passes a raw pinned sha as the ref (the
            # projection passes ref names) — move trunk exactly then
            if landreq._sha(ref) and not moved:
                moved.append(self.commit("trunk moves mid-ladder"))
            return real(gitdir, tip, ref)

        with mock.patch.object(landreq, "_landing_proof",
                               side_effect=moving):
            _out, err = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(err)
        self.assertTrue(moved, "the mid-ladder move must actually run")
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


class CloseBuildLandedTest(CloseBase):
    """An OPEN BUILD obligation closes through its landed review descendant."""

    def build_parent(self, supersedes=None, lane="lane/build-parent"):
        kw = {"supersedes": supersedes} if supersedes else {"new_work": True}
        return dispatches.add(
            "builder", lane, ref=self.b, repo=self.repo, kind="build",
            notify=False, **kw)

    def approved_child(self, parent, lane="lane/build-parent-review",
                       force=False):
        row = dispatches.add(
            "reviewer", lane, ref=self.side, repo=self.repo, kind="review",
            notify=False, supersedes=parent["id"], force=force)
        out, err = dispatches.mark_verdict(
            row["id"], self.side, "reviewed build output", polarity="approve")
        self.assertIsNone(err)
        return row

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
        self.assertEqual(set(event), dispatches._BUILD_LANDED_EVENT_FIELDS)
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
        got, err = dispatches.mark_verdict(
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

    def test_a_cherry_picked_review_tip_closes_via_patch_equivalence(self):  # noqa: VACUOUS_ASSERTION — the stored proof_mode and landing_review_id are unconditional positive controls; this is the arm a proof-rejecting mutation must kill (it previously survived)
        """The build ladder accepts patch-equivalent proof — the integrator
        lands by cherry-pick, so the reviewed tip is routinely NO ancestor
        while its content is on trunk. A cross-family review measured a
        mutation rejecting patch-equivalence SURVIVING the suite: this test
        is its killer."""
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

    A prior incident, twice in one day: a landed fix seats could not benefit
    from until relaunch, and a land whose own commit message says it takes
    effect on RELAUNCH. Both rows read CLOSED (LANDED) and neither said a
    running process still held pre-land code.

    Owner rule: CLI-class
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
        _out, err = dispatches.mark_verdict(
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
        it must say so. Measured on a live row: a --new-work review chain
        never touches the build parent, the walk continued silently, and the
        generic landedness refusal printed with no detail for a chain
        problem. The chained sibling test above is the positive control: the
        same shape with supersedes= closes."""
        parent = dispatches.add(
            "builder", "lane/unchained-build", ref=self.b, repo=self.repo,
            kind="build", notify=False, new_work=True)
        child = dispatches.add(
            "reviewer", "lane/unchained-review", ref=self.side,
            repo=self.repo, kind="review", notify=False, new_work=True)
        _out, err = dispatches.mark_verdict(
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
        return its refusal. One helper, two directions — a one-sided test
        cannot see a one-sided guard."""
        parent = dispatches.add(
            "builder", "lane/roots-build-%s-%s" % (review_root, parent_root),
            ref=self.b, repo=self.repo, kind="build", notify=False,
            new_work=True)
        child = dispatches.add(
            "reviewer", "lane/roots-review-%s-%s" % (review_root, parent_root),
            ref=self.side, repo=self.repo, kind="review", notify=False,
            new_work=True)
        _out, err = dispatches.mark_verdict(
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
        """A cross-family review found: CHAIN_UNKNOWN is the truthy string
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
        """A cross-family review found: an absent chain_root is a LEGACY row
        whose chaining is UNKNOWN (404 live rows measured), and UNKNOWN
        suppresses — the
        own-id fallback branded every legacy approve confidently unchained.
        The unchained test above is the positive control: determinate
        disjoint roots still fire."""
        parent = dispatches.add(
            "builder", "lane/legacy-build", ref=self.b, repo=self.repo,
            kind="build", notify=False, new_work=True)
        child = dispatches.add(
            "reviewer", "lane/legacy-review", ref=self.side,
            repo=self.repo, kind="review", notify=False, new_work=True)
        _out, err = dispatches.mark_verdict(
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
    over a live coordination ledger: answerable on 62 of the 65 landed
    closes carrying a delivery class, and 8 of those 65 --live declarations
    were about a land that moved process-class code."""

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
        """A cross-family review's missed arm, and it opened on the COMMON case.

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
        _out, err = dispatches.mark_verdict(child["id"], tip, "reviewed",
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

    def handoff(self, lane, ref, supersedes=None):
        row, why, sent = dispatches.send(
            "codex-3", lane, "review " + lane, ref, repo=self.repo,
            key="key-" + lane, sign=False,
            new_work=supersedes is None, supersedes=supersedes)
        self.assertIsNone(why)
        self.assertTrue(sent)
        return row

    def resolved(self, polarity="fix", upstream=False, push=False):
        if upstream or push:
            self.add_origin()
        first = self.handoff("feature-r1", self.side)
        dispatches.mark_verdict(first["id"], self.side, "findings",
                                polarity=polarity)
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve findings", path="g")
        self.git("checkout", "-q", self.main)
        second = self.handoff("feature-r2", fixed, supersedes=first["id"])
        dispatches.mark_verdict(second["id"], fixed, "re-probed clean",
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
        dispatches.mark_verdict(approved["id"], approved_tip, "clean",
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
        dispatches.mark_verdict(rejected["id"], rejected_tip, "new findings",
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
        dispatches.mark_verdict(first["id"], self.side, "findings",
                                polarity="fix")
        self.git("merge", "--no-edit", "-q", "side")
        descendant = self.commit("unreviewed descendant")
        _out, err = landreq.close(first["id"], "superseded",
                                  evidence="trust me", tip=descendant)
        self.assertIn("no later APPROVE verdict", err)

    def test_gate_capable_ungated_approve_cannot_close(self):
        first = self.handoff("feature-r1", self.side)
        dispatches.mark_verdict(first["id"], self.side, "findings",
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
        (pinned in test_landreq's ContraryDischargeTest). Sharing a chain ROOT
        is weaker than that: when the candidate's supersedes ancestry never
        reaches a row the debt's author sent, no handoff happened — and the
        refusal must name BOTH legs, so the near-miss candidate is visible to
        the next integrator instead of vanishing into the same-author
        conjunction."""
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "other-author"}):
            root = self.handoff("feature-r0", self.side)
        first = self.handoff("feature-r1", self.side,
                             supersedes=root["id"])
        dispatches.mark_verdict(first["id"], self.side, "findings",
                                polarity="fix")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve by another author", path="g")
        self.git("checkout", "-q", self.main)
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "other-author"}):
            second = self.handoff("other-author-r2", fixed,
                                  supersedes=root["id"])
            dispatches.mark_verdict(second["id"], fixed, "clean",
                                    polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        _out, err = landreq.close(first["id"], "superseded",
                                  evidence="wrong author", tip=fixed)
        self.assertIn("same author/repo", err)
        self.assertIn("proved a lane handoff", err)

    def test_unrelated_chain_cannot_launder_the_contrary(self):
        first = self.handoff("feature-r1", self.side)
        dispatches.mark_verdict(first["id"], self.side, "findings",
                                polarity="fix")
        other = self.handoff("other-r2", self.c)
        dispatches.mark_verdict(other["id"], self.c, "clean",
                                polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        _out, err = landreq.close(first["id"], "superseded",
                                  evidence="unrelated", tip=self.c)
        self.assertIn("SAME WORK CHAIN", err)

    def test_a_chained_round_still_needs_git_lineage(self):
        first = self.handoff("feature-r1", self.side)
        dispatches.mark_verdict(first["id"], self.side, "findings",
                                polarity="fix")
        second = self.handoff("feature-r2", self.c, supersedes=first["id"])
        dispatches.mark_verdict(second["id"], self.c, "clean",
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
        dispatches.mark_verdict(first["id"], self.side, "findings",
                                polarity="fix")
        self.git("merge", "--no-edit", "-q", "side")
        _out, err = landreq.close(first["id"], "superseded",
                                  evidence="same", tip=self.side)
        self.assertIn("cannot supersede itself", err)

    def test_stale_tracking_ref_without_origin_cannot_override_local(self):
        first = self.handoff("feature-r1", self.side)
        dispatches.mark_verdict(first["id"], self.side, "findings",
                                polarity="fix")
        self.git("merge", "--no-edit", "-q", "side")
        self.git("checkout", "-q", "-b", "resolution", self.side)
        fixed = self.commit("resolved but not landed locally", path="g")
        second = self.handoff("feature-r2", fixed, supersedes=first["id"])
        dispatches.mark_verdict(second["id"], fixed, "clean",
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
        dispatches.mark_verdict(reviewed_row["id"], self.side, "clean",
                                polarity="approve")
        self.git("checkout", "-q", "side")
        superseding = self.commit("the rework that actually shipped",
                                  path="g2")
        self.git("checkout", "-q", self.main)
        second = self.handoff("approved-then-replaced-r2", superseding,
                              supersedes=reviewed_row["id"])
        dispatches.mark_verdict(second["id"], superseding, "clean",
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
        dispatches.mark_verdict(other_row["id"], self.b, "clean",
                                polarity="approve")
        third = self.handoff("approved-never-contained-r2", superseding,
                             supersedes=other_row["id"])
        dispatches.mark_verdict(third["id"], superseding, "clean",
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
        dispatches.mark_verdict(reviewed_row["id"], self.side, "clean",
                                polarity="approve")
        self.git("checkout", "-q", "side")
        superseding = self.commit("descendant round", path="g3")
        self.git("checkout", "-q", self.main)
        second = self.handoff("approved-and-landed-r2", superseding,
                              supersedes=reviewed_row["id"])
        dispatches.mark_verdict(second["id"], superseding, "clean",
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
            "opus-integrator", "lane/evals", "review evals",
            self.side, repo=self.repo, key="key-evals", sign=False,
            new_work=True)
        self.assertIsNone(why)
        self.assertTrue(sent)
        dispatches.mark_verdict(row["id"], self.side,
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
        self.assertIn("--reason superseded (if superseded)", err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_an_unknown_land_state_fails_closed(self):
        row = self.fix_row()
        before = self.history_len(row["id"])
        with mock.patch.object(landreq, "_landed", return_value=None):
            _out, err = landreq.close(row["id"], "withdrawn",
                                      evidence="evidence")
        self.assertIn("could not prove", err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_a_non_fix_verdict_cannot_be_withdrawn(self):
        row = self.fix_row(polarity="approve")
        _out, err = landreq.close(row["id"], "withdrawn",
                                  evidence="evidence")
        self.assertIn("not a FIX/SUPERSEDE", err)

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


class CloseOutOfScopeTest(CloseBase):
    def test_a_row_whose_work_is_ON_TRUNK_is_refused(self):
        """THIS DOOR'S GUARDS ANSWER AN ADJACENT QUESTION. They test
        whether the obligation is MOOT and whether the work is still LIVE, and
        a ghost BUILD row passes both honestly: its review is never coming, so
        the obligation IS moot. Nothing asked whether the work SHIPPED, so the
        door opened by its own terms and recorded a falsehood by ours —
        out-of-scope on a change the fleet is running.

        Measured by dry-running five reasons against a ghost row: four
        refused correctly and this one WOULD CLOSE. The fourth guard in one
        audit asking an adjacent question, and the only one that WRITES."""
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
    """The translated-object-superseded door. A pre-seat-stamp row
    (author binding ABSENT, exactly as such rows read in a live ledger)
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
        dispatches.mark_verdict(first["id"], self.side, "findings",
                                polarity=polarity)
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve findings", path="g")
        self.git("checkout", "-q", self.main)
        second = self.handoff("feature-r2", fixed, supersedes=first["id"])
        dispatches.mark_verdict(second["id"], fixed, "re-probed clean",
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
        """The translated-pruned shape: unstamped row, reviewed tip pruned,
        sidecar translating to a live object kept under an archive ref (or
        not)."""
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
        dispatches.mark_verdict(second["id"], fixed, "re-probed clean",
                                polarity="approve")
        if merge:
            self.git("merge", "--no-edit", "-q", "r2")
        return second, fixed

    def test_the_translated_pruned_shape_closes_superseded(self):  # noqa: VACUOUS_ASSERTION — the close IS the claim; its control is the paired refusal arms (unpreserved/unapproved) and the mutation arm (door deleted) reddening exactly this
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
        """A cross-family review FIX, pinned: the sidecar binds
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
        dispatches.mark_verdict(other["id"], fixed,
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
        """A cross-family review FIX, pinned: the content relation is
        directional. 'Carries' means the superseder CONTAINS the translated
        cargo (translated is an ancestor of, or patch-equivalent to, the
        superseder) — never that the cargo contains the superseder. An
        approved land of the translated object's own ANCESTOR predates the
        work it claims to resolve."""
        row, _tip, _new = self.translated_pruned_row()
        # the translated object's base — on trunk, approved, predates cargo
        other = self.handoff("lane/old-work", self.a)
        dispatches.mark_verdict(other["id"], self.a,
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
        dispatches.mark_verdict(second["id"], fixed,
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
        """The full bound-author archived shape: author BOUND, verdict
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
        dispatches.mark_verdict(second["id"], fixed, "stitch",
                                polarity="approve")
        out, err = landreq.close(row["id"][:12], "superseded",
                                 evidence="landed revised", tip=fixed)
        self.assertIsNone(err)
        event = [e for e in dispatches.history(row["id"])
                 if e.get("event") == "close"][0]
        self.assertEqual(event["close_proof_mode"], "attested-superseded")

    def test_a_live_reviewed_object_never_takes_the_door(self):  # noqa: VACUOUS_ASSERTION — the refusal IS the claim; its control is the door arms positively closing on the same fixture minus one rung
        row = self.unstamped_row(lane="lane/pre-stamp-live")
        _second, fixed = self.approved_superseder(row)
        before = self.history_len(row["id"])
        _out, err = landreq.close(row["id"], "superseded",
                                  evidence="x", tip=fixed)
        self.assertIsNotNone(err)
        self.assertEqual(self.history_len(row["id"]), before)

    def test_an_unpreserved_translation_refuses(self):  # noqa: VACUOUS_ASSERTION — the refusal IS the claim; its control is the preserved arm (same fixture, archive tag added) positively closing
        """Translated but NOT preserved under any archive/rescue ref is
        stranded's question, not superseded's."""
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
        """The archived-replaced shape: an unstamped FIX row whose reviewed
        tip is ALIVE under an archive tag, while the landed tip carries the
        SAME patch re-derived (object containment false, patch identity
        true)."""
        row = self.unstamped_row(lane=lane)
        tip = self.side
        # the verdict is FIX-polarity (the orphan's original verdict)
        dispatches.mark_verdict(row["id"], tip, "findings", polarity="fix")
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
        dispatches.mark_verdict(second["id"], fixed, "stitch approve",
                                polarity="approve")
        return row, tip, fixed

    def test_the_archived_replaced_shape_closes_superseded(self):  # noqa: VACUOUS_ASSERTION — the close IS the claim; its controls are the two refusal arms (unapproved land, unchained approval) on the same fixture
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
        """The attested-replaced shape: archived original + landed content
        REVISED while re-derived (no patch-id can match), so the archive
        tag plus the landed tip's own approved review is the attestation."""
        row = self.unstamped_row(lane=lane)
        tip = self.side
        dispatches.mark_verdict(row["id"], tip, "findings", polarity="fix")
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
        dispatches.mark_verdict(second["id"], fixed, "stitch approve",
                                polarity="approve")
        return row, tip, fixed

    def test_the_attested_shape_closes_superseded(self):  # noqa: VACUOUS_ASSERTION — the attested close IS the claim; its control is the unapproved-land arm refusing on the identical fixture
        """The attested shape exactly: patch-equivalence cannot hold (the
        adopter revised while re-deriving), so the close records
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
        dispatches.mark_verdict(second["id"], other, "approve",
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
    """Live-measured shapes, as named cases."""

    def test_renamed_lane_stem_hit_refuses_abandon(self):
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

    def test_live_unlanded_lane_branch_refuses(self):
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
        dispatches.mark_verdict(second_row["id"], fixed, "clean",
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
        dispatches.mark_verdict(second["id"], fixed, "clean",
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
        self.assertIn("not a FIX/SUPERSEDE verdict row", err)


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
        _out, err = dispatches.mark_verdict(confirmation["id"], tip, ref,
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
        over a live ledger: 13 contrary rows, all 13 confirmations, zero
        originals, so EVERY USE OF THE DOOR MINTED ONE PERMANENT DEBT.

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
        A live row (whose tip's 3-line delta reached trunk under a
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
