"""Historical authority through the real writer, reducer, and land consumer.

The v3 fixture is the old writer's wire schema, not output manufactured by the
new capture helper. New records exercise the store policy/history boundary.
"""
import copy
import time
import os
from unittest import mock

from helm import dispatches, eventledger, home, landreq, pk, seats, seats_common, ship, store
from helm.store import policy_history
from tests.test_dispatches import DispatchBase


# Frozen pre-tier schema from the 86-event gate/caps/basis census cohort.
# This is a synthetic exemplar of the measured key set, not a copied live row;
# identifiers, prose and timestamps are neutral. No evidence is backfilled.
LEGACY_VERDICT = {"v": 3, "event": "verdict", "id": "1" * 32, "seq": 1,
                  "ts": "2026-08-01T00:00:00Z", "reviewed_tip": "a" * 40,
                  "verdict_ref": "reviewed historical change", "polarity": "approve",
                  "gate": "b" * 16, "gate_caps": ["receipt-v1"], "basis": "measured"}


# Old native v5 authority inside its v1 runtime envelope. This compatibility
# specimen and its precomputed anchor do not call the new capture/producer.
LEGACY_NATIVE = {
    "v": 1, "identity": "reviewer", "session": "historical-session",
    "runtime": {"family": "claude", "agent_harness": "claude", "backend": "native"},
    "resolved": {"agent_harness": "claude", "backend": "native", "family": "claude",
                 "model": None, "provider": None, "upstream_model": None},
    "authority": {"v": 5, "identity": "reviewer", "roster_identity": "reviewer",
                  "session": "historical-session", "runtime_verified": True,
                  "runtime": {"family": "claude", "agent_harness": "claude", "backend": "native"}}}
LEGACY_NATIVE_ANCHOR = "00cfc64f4af9566dd85e74db451e587c"


class RecordedTierTest(DispatchBase):
    def policy(self, members):
        return store.write_prior({
            "id": "test-approval-tier", "statement": "Final approval uses this tier.",
            "confidence": 1.0, "stated_ts": "2026-07-29T00:00:00Z", "source": "human",
            "policy_kind": "approval-tier", "policy_members": members,
            "policy_reason": "independent final approval"},
            root_dir=os.path.join(home.global_dir(), "premises"))

    def record(self, recipient="reviewer"):
        row = self.add(recipient=recipient, ref=self.side)
        with self.verdict_author(), mock.patch.object(
                dispatches.gate, "bind", return_value=("VERIFIED", "b" * 16, "test receipt")):
            verdict, err = dispatches.mark_verdict(
                row["id"], self.side, "reviewed exact tip", polarity="approve",
                basis="measured", bind_author=True)
        self.assertIsNone(err, err)
        self.assertEqual(verdict["verdict_version"], 4)
        self.assertEqual(dispatches.approval_tier_for_verdict(verdict), ("ok", None))
        return verdict

    def assert_authorized(self, verdict):
        replay, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        self.assertIn(verdict["id"], replay)
        row = replay[verdict["id"]]
        self.assertEqual(row["status"], "verdict")
        self.assertEqual(landreq._approval_refusal(row), (None, "ok"))
        return row

    def test_record_then_same_id_policy_edit_and_retire_preserves_authority(self):
        self.policy(["family:claude"])
        verdict = self.record()
        self.policy(["family:codex"])
        self.assert_authorized(verdict)
        retired, err = store.retire("test-approval-tier", "2026-09-09T00:00:00Z", "replaced")
        self.assertIsNone(err, err)
        self.assertIsNotNone(retired)
        self.assert_authorized(verdict)
        self.assertFalse(ship._derived("_global/premises/policy-versions.jsonl"))

    def test_record_then_real_roster_rename_preserves_exact_old_identity(self):
        self.policy(["seat:reviewer"])
        verdict = self.record()
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "reviewer"}):
            seats.write_roster("reviewer", session="verdict-author-session",
                               runtime={"family": "claude", "backend": "native"},
                               presence_beat=False)
            renamed, why = seats.rename_seat("reviewer", "reviewer-renamed", whole_row=True)
        self.assertTrue(renamed, why)
        self.assertIn("reviewer-renamed", seats.roster())
        self.assertNotIn("reviewer", seats.roster())
        row = self.assert_authorized(verdict)
        self.assertEqual(row["recipient"], "reviewer")

    def test_record_then_retired_roster_cannot_erase_recorded_authority(self):
        self.policy(["family:claude"])
        verdict = self.record()
        # No public retire verb: remove the exact binding through the canonical
        # roster store in this isolated home, after proving the binding existed.
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "reviewer"}):
            seats.write_roster("reviewer", session="verdict-author-session",
                               runtime={"family": "claude", "backend": "native"},
                               presence_beat=False)
        self.assertEqual(seats.roster()["reviewer"]["session"], "verdict-author-session")
        with seats._flocked(seats.roster_path() + ".lock"):
            roster = seats_common.roster_for_write()
            del roster["reviewer"]
            pk.write_json(seats.roster_path(), roster)
        self.assertNotIn("reviewer", seats.roster())
        self.assert_authorized(verdict)
        # Tripwires additionally prove the new replay has no live dependency.
        with mock.patch.object(seats, "roster", side_effect=AssertionError("live roster")), \
                mock.patch.object(store, "load_certain_policy", side_effect=AssertionError("live policy")):
            self.assert_authorized(verdict)

    def test_no_policy_is_measured_and_survives_later_declaration(self):
        row = self.add(recipient="reviewer", ref=self.side)
        with self.verdict_author(), mock.patch.object(
                dispatches.gate, "bind", return_value=("VERIFIED", "b" * 16, "test receipt")):
            verdict, err = dispatches.mark_verdict(row["id"], self.side, "reviewed", polarity="approve", bind_author=True)
        self.assertIsNone(err, err)
        self.assertEqual(dispatches.approval_tier_for_verdict(verdict), ("none", None))
        self.policy(["family:codex"])
        self.assertEqual(landreq._approval_refusal(dispatches.snapshot()[0][row["id"]]), (None, "none"))

    def test_history_failure_refuses_append_and_unknown_policy_is_not_absence(self):
        self.policy(["family:claude"])
        row = self.add(recipient="reviewer", ref=self.side)
        before = eventledger.events(dispatches.ledger_path())
        with self.verdict_author(), mock.patch.object(
                dispatches.gate, "bind", return_value=("VERIFIED", "b" * 16, "test receipt")), \
                mock.patch.object(policy_history.eventledger, "append_unlocked", return_value=False):
            verdict, err = dispatches.mark_verdict(row["id"], self.side, "reviewed", polarity="approve", bind_author=True)
        self.assertIsNone(verdict)
        self.assertIn("not retained", err)
        self.assertEqual(eventledger.events(dispatches.ledger_path()), before)
        from helm.store import load
        with mock.patch.object(load, "_policy_hits", side_effect=OSError("unreadable")):
            reference, err = policy_history.capture(self.repo, {"id": row["id"]})
        self.assertIsNone(reference)
        self.assertIn("read failed", err)

    def test_missing_corrupt_and_wrong_context_history_deny(self):
        self.policy(["family:claude"])
        verdict = self.record()
        with open(policy_history.path(), encoding="utf-8") as f:
            saved = f.read()
        for raw in ("", "{bad json}\n"):
            with self.subTest(raw=raw):
                pk.atomic_write(policy_history.path(), raw)
                state, why = dispatches.approval_tier_for_verdict(verdict)
                self.assertEqual(state, "unknown")
                self.assertNotEqual(dispatches.tier_unknown_kind(state), "pre-tier")
                self.assertIsNotNone(why)
        pk.atomic_write(policy_history.path(), saved)
        wrong = copy.deepcopy(verdict)
        wrong["verdict_ref"] = "another verdict"
        wrong["verdict_tier_evidence"]["context"]["verdict_ref"] = "another verdict"
        wrong["verdict_tier_anchor"] = dispatches._proof_anchor("verdict-tier-v1", wrong["verdict_tier_evidence"])
        state, why = dispatches.approval_tier_for_verdict(wrong)
        self.assertEqual(state, "unknown")
        self.assertIn("another verdict", why)
        self.assert_authorized(verdict)

    def test_reanchored_tier_claim_and_future_or_partial_evidence_deny(self):
        self.policy(["family:claude"])
        verdict = self.record()
        # `{"v": 3}` IS THE FUTURE-VERSION ARM, and it must name a version
        # ABOVE the one this writer mints: a version number pinned in a test
        # is a refusal only while it names a shape the reader rejects, and one
        # that catches up to the writer becomes a no-op asserting nothing.
        # `{"family_axis": ...}` asks the same question of the axis field: a
        # recorded axis is re-derived from the author proof, so a row claiming
        # its family was measured when a roster stamp produced it is DAMAGED,
        # exactly as a row claiming the wrong tier state is.
        changes = [{"state": "outside"}, {"v": 3}, {"v": True},
                   {"policy_version": {"version": "0" * 32}},
                   {"family_axis": dispatches.FAMILY_AXIS_MODEL},
                   {"family_model": "some-other-model"}]
        for change in changes:
            with self.subTest(change=change):
                bad = copy.deepcopy(verdict)
                bad["verdict_tier_evidence"].update(change)
                bad["verdict_tier_anchor"] = dispatches._proof_anchor("verdict-tier-v1", bad["verdict_tier_evidence"])
                state, why = dispatches.approval_tier_for_verdict(bad)
                self.assertEqual(state, "unknown")
                self.assertEqual(dispatches.tier_unknown_kind(state), "damaged")
                self.assertIsNotNone(why)
        missing = copy.deepcopy(verdict)
        for key in dispatches.VERDICT_TIER_FIELDS:
            del missing[key]
        state, why = dispatches.approval_tier_for_verdict(missing)
        self.assertEqual(dispatches.tier_unknown_kind(state), "damaged")
        self.assertIn("v4", why)
        self.assert_authorized(verdict)

    def test_a_new_verdict_names_its_family_axis_and_an_older_one_keeps_authority(self):
        """The axis ships as a VERSION, so no minted receipt is rewritten.

        MEASURED ON THE LIVE LEDGER BEFORE THIS SHIPPED: 887 recorded tier
        proofs, every one v1, every one decided on a family with nothing
        saying where that family came from. Widening v1 in place would have
        turned all 887 into DAMAGED at once, because this reader tests the
        field set by equality and anchors the whole dict. So v1 keeps its
        exact shape and exact meaning and is never asked for an axis it could
        not have carried — absent is honest, backfilled would not be.
        """
        self.policy(["family:claude"])
        verdict = self.record()
        evidence = verdict["verdict_tier_evidence"]
        self.assertEqual(evidence["v"], 2)
        # THE FLEET'S OWN AXIS TODAY. A native claude author is a roster stamp,
        # and the verdict now SAYS so rather than leaving a reader to infer it.
        self.assertEqual(evidence["family_axis"], dispatches.FAMILY_AXIS_ROSTER)
        self.assertIsNone(evidence["family_model"])
        self.assertNotEqual(evidence["family_axis"],
                            dispatches.FAMILY_AXIS_UNKNOWN)
        self.assert_authorized(verdict)
        # AND THE OLD SHAPE STILL AUTHORIZES, byte-for-byte as recorded.
        older = copy.deepcopy(verdict)
        older["verdict_tier_evidence"] = {
            k: v for k, v in evidence.items()
            if k not in ("family_axis", "family_model")}
        older["verdict_tier_evidence"]["v"] = 1
        older["verdict_tier_anchor"] = dispatches._proof_anchor(
            "verdict-tier-v1", older["verdict_tier_evidence"])
        self.assertEqual(dispatches.approval_tier_for_verdict(older), ("ok", None))
        # A v1 ROW THAT GREW THE FIELD ANYWAY IS DAMAGED — the versions name
        # two shapes, not a floor to add fields onto.
        grown = copy.deepcopy(older)
        grown["verdict_tier_evidence"]["family_axis"] = \
            dispatches.FAMILY_AXIS_ROSTER
        grown["verdict_tier_anchor"] = dispatches._proof_anchor(
            "verdict-tier-v1", grown["verdict_tier_evidence"])
        state, why = dispatches.approval_tier_for_verdict(grown)
        self.assertEqual(dispatches.tier_unknown_kind(state), "damaged")
        self.assertIsNotNone(why)

    def test_frozen_legacy_event_replays_pre_tier_not_today_policy(self):
        self.policy(["seat:reviewer"])
        row = self.add(recipient="reviewer", ref=self.side)
        event = dict(LEGACY_VERDICT, id=row["id"], reviewed_tip=self.side, seq=row["seq"] + 1)
        eventledger.append(dispatches.ledger_path(), event)
        replay, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        legacy = replay[row["id"]]
        self.assertEqual(legacy["status"], "verdict")
        why, state = landreq._approval_refusal(legacy)
        self.assertEqual(state, "unknown")
        self.assertEqual(dispatches.tier_unknown_kind(state), "pre-tier")
        self.assertIn("PRE-TIER", why)
        self.assertNotIn("COULD NOT EVALUATE", why)
        self.assertEqual(LEGACY_VERDICT["reviewed_tip"], "a" * 40)

    def test_frozen_v3_admission_body_with_live_helpers_rejects_v4(self):
        from pathlib import Path
        source = Path(__file__).with_name("fixtures") / "dispatch_apply_v3.py"
        namespace = dict(vars(dispatches))
        exec(compile(source.read_text(), str(source), "exec"), namespace)
        old_apply = namespace["_apply"]
        self.policy(["family:claude"])
        verdict = self.record()
        events = eventledger.events(dispatches.ledger_path())
        opening = next(e for e in events if e.get("id") == verdict["id"] and e.get("event") == "dispatch")
        event = next(e for e in events if e.get("id") == verdict["id"] and e.get("event") == "verdict")
        state = dispatches._new_state(opening)
        self.assertEqual(state["status"], "open")
        self.assertEqual(dispatches._apply(state, event)["status"], "verdict")
        self.assertIs(old_apply(state, event), state)
        # Change ONLY the event version: the frozen admission body takes the
        # otherwise identical event with live helpers. This isolates its inline
        # version gate; it is not a compatibility claim about an entire reader.
        accepted = old_apply(state, dict(event, v=3))
        self.assertEqual(accepted["status"], "verdict")
        self.assertEqual(accepted["verdict_ref"], event["verdict_ref"])
        self.assertNotIn("verdict_tier_evidence", accepted)

    def test_frozen_native_v5_is_readable_pre_tier_but_malformed_is_not_legacy(self):
        self.policy(["family:claude"])
        row = self.add(recipient="reviewer", ref=self.side)
        event = dict(LEGACY_VERDICT, id=row["id"], reviewed_tip=self.side, seq=row["seq"] + 1,
                     verdict_author_session="historical-session",
                     verdict_author_runtime_evidence=copy.deepcopy(LEGACY_NATIVE),
                     verdict_author_runtime_anchor=LEGACY_NATIVE_ANCHOR)
        eventledger.append(dispatches.ledger_path(), event)
        replay, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        old = replay[row["id"]]
        self.assertEqual(old["status"], "verdict")
        state, why = dispatches.approval_tier_for_verdict(old)
        self.assertEqual(dispatches.tier_unknown_kind(state), "pre-tier")
        self.assertIn("PRE-TIER", why)
        for value in (None, {}, False, {"v": 99}):
            bad = dict(old, verdict_author_runtime_evidence=value)
            state, why = dispatches.approval_tier_for_verdict(bad)
            self.assertEqual(dispatches.tier_unknown_kind(state), "damaged")
        bad = copy.deepcopy(old)
        bad["verdict_author_runtime_evidence"]["v"] = True
        bad["verdict_author_runtime_anchor"] = dispatches._proof_anchor(
            "verdict-author-runtime-v1", bad["verdict_author_runtime_evidence"])
        state, why = dispatches.approval_tier_for_verdict(bad)
        self.assertEqual(dispatches.tier_unknown_kind(state), "damaged")
        self.assertEqual(dispatches._apply(row, dict(event, verdict_author_runtime_evidence=None)), row)

    def test_retained_observation_without_verdict_grants_nothing(self):
        self.policy(["family:claude"])
        row = self.add(recipient="reviewer", ref=self.side)
        append = eventledger.append_unlocked
        def fail_verdict(path, event):
            return False if event.get("event") == "verdict" else append(path, event)
        with self.verdict_author(), mock.patch.object(
                dispatches.gate, "bind", return_value=("VERIFIED", "b" * 16, "test receipt")), \
                mock.patch.object(eventledger, "append_unlocked", side_effect=fail_verdict):
            verdict, err = dispatches.mark_verdict(row["id"], self.side, "reviewed", polarity="approve", bind_author=True)
        self.assertIsNone(verdict)
        self.assertIn("NOT recorded", err)
        self.assertEqual(len(policy_history.snapshot()[0]), 1)
        self.assertNotEqual(dispatches.snapshot()[0][row["id"]]["status"], "verdict")

    def test_actual_projection_and_api_classify_legacy_and_concur(self):
        from helm import web
        self.policy(["seat:reviewer"])
        ids = []
        for polarity in ("approve", "concur"):
            row = self.add(recipient="reviewer", ref=self.side)
            eventledger.append(dispatches.ledger_path(), dict(LEGACY_VERDICT, id=row["id"],
                               reviewed_tip=self.side, seq=row["seq"] + 1, polarity=polarity))
            ids.append(row["id"])
        rows, raw, err = landreq.project_raw()
        self.assertIsNone(err, err)
        held = dict((lr["id"], (landreq.review_hold_kind(lr), why))
                    for lr, why in landreq._unmeasurable_rows(rows, raw))
        self.assertEqual(held[ids[0]][0], "pre-tier")
        self.assertEqual(held[ids[1]][0], "advisory")
        self.assertIn("does not authorize", held[ids[1]][1])
        self.assertNotIn("no reason recorded", held[ids[1]][1])
        with mock.patch.object(web, "_lr_recent_lands", return_value={}), \
                mock.patch.object(web, "_lr_native_chain", return_value={}):
            body = web._lr_project(time.time(), None, all_projects=True)
        self.assertIsNone(body["unavailable"], body["unavailable"])
        by_id = {item["id"]: item for item in body["unmeasurable"]}
        self.assertEqual(by_id[ids[0]]["kind"], "pre-tier")
        self.assertEqual(by_id[ids[1]]["kind"], "advisory")
        self.assertIn("does not authorize", by_id[ids[1]]["reason"])

    def test_strict_population_does_not_turn_denied_directory_or_file_into_absence(self):  # noqa: VACUOUS_ASSERTION — the unconditional strict read proves the policy row exists before each denied accessor must turn that same population UNKNOWN
        from helm.store import load
        self.policy(["family:claude"])
        path = load._policy_hits("approval-tier")[0]["path"]
        self.assertEqual(len(load._policy_hits("approval-tier", strict=True)), 1)
        original_scandir, original_open = os.scandir, open
        def denied_directory(directory):
            if directory == os.path.dirname(path):
                raise PermissionError("test policy directory denied")
            return original_scandir(directory)
        def denied_file(filename, *args, **kwargs):
            if filename == path:
                raise PermissionError("test policy source denied")
            return original_open(filename, *args, **kwargs)
        for target, failure in (("helm.store.load.os.scandir", denied_directory),
                                ("builtins.open", denied_file)):
            with self.subTest(target=target), mock.patch(target, side_effect=failure):
                self.assertEqual(load._policy_hits("approval-tier"), [])
                reference, err = policy_history.capture(self.repo, {"id": "denied"})
                self.assertIsNone(reference)
                self.assertIn("read failed", err)
        self.assertEqual(len(load._policy_hits("approval-tier", strict=True)), 1)
        self.assertFalse(os.path.exists(policy_history.path()))

    def test_malformed_typed_prior_never_falls_back_to_absence(self):
        from helm.store import load
        self.policy(["family:claude"])
        path = load._policy_hits("approval-tier")[0]["path"]
        with open(path, "rb") as source:
            saved = source.read()
        # The first is the separate typed->episodic fallback, not an I/O error.
        malformed = [b"---\nname: old-memory\ndescription: old\npolicy_kind: approval-tier\n---\n",
                     b"\xff", b"---\nid: prior\nstatement: missing closing fence\n",
                     b"---\nid: prior\nid: duplicate\nstatement: test\n---\n",
                     b"---\nid: prior\nstatement: test\npolicy_kind approval-tier\n---\n"]
        self.assertEqual(len(load._policy_hits("approval-tier", strict=True)), 1)
        for raw in malformed:
            with self.subTest(raw=raw):
                with open(path, "wb") as source:
                    source.write(raw)
                reference, err = policy_history.capture(self.repo, {"id": "malformed"})
                self.assertIsNone(reference)
                self.assertIn("read failed", err)
        with open(path, "wb") as source:
            source.write(saved)
        self.assertEqual(len(load._policy_hits("approval-tier", strict=True)), 1)
        self.assertFalse(os.path.exists(policy_history.path()))

    def test_duplicate_attestation_diagnoses_before_append_and_repair_records(self):
        from helm.store import load
        self.policy(["family:claude"])
        path = load._policy_hits("approval-tier")[0]["path"]
        with open(path, encoding="utf-8") as source:
            saved = source.read()
        markers = ("PRIVATE_PAYLOAD_FIRST", "PRIVATE_PAYLOAD_SECOND")
        damaged = saved.replace("---\n", "---\nattest_payload: %s\nattest_payload: %s\n" % markers, 1)
        pk.atomic_write(path, damaged)
        row = self.add(recipient="reviewer", ref=self.side)
        before = eventledger.events(dispatches.ledger_path())
        with self.verdict_author(), mock.patch.object(
                dispatches.gate, "bind", return_value=("VERIFIED", "b" * 16, "test receipt")), \
                mock.patch.object(eventledger, "append_unlocked", wraps=eventledger.append_unlocked) as append:
            verdict, err = dispatches.mark_verdict(
                row["id"], self.side, "reviewed", polarity="approve", bind_author=True)
            self.assertIsNone(verdict)
            self.assertIn("record-time approval tier was not retained", err)
            self.assertIn('"%s":3' % path, err)
            self.assertIn('duplicate frontmatter field (field "attest_payload")', err)
            for marker in markers:
                self.assertNotIn(marker, err)
            append.assert_not_called()
            self.assertEqual(eventledger.events(dispatches.ledger_path()), before)
            self.assertFalse(os.path.exists(policy_history.path()))
            # Repair this same synthetic source and retry the SAME dispatch via
            # the real producer; a blanket refusal would fail this positive.
            pk.atomic_write(path, saved)
            verdict, err = dispatches.mark_verdict(
                row["id"], self.side, "reviewed", polarity="approve", bind_author=True)
            self.assertIsNone(err, err)
            retained_then_verdict = [policy_history.path(), dispatches.ledger_path()]
            # The real producer also writes its attestation sidecars. Assert
            # exactly the two authority-ledger appends, in commit order.
            self.assertEqual([call.args[0] for call in append.call_args_list
                              if call.args[0] in retained_then_verdict], retained_then_verdict)
        self.assertEqual(verdict["verdict_version"], 4)
        self.assert_authorized(verdict)

    def test_frontmatter_syntax_metadata_is_common_and_permissive_mode_unchanged(self):
        from helm.store import load
        self.policy(["family:claude"])
        path = load._policy_hits("approval-tier")[0]["path"]
        defaults = {"id": "", "statement": ""}
        cases = (
            ("---\nid: first\nid: PRIVATE_DUPLICATE\n---\n", "duplicate-field", 3, "id"),
            ("---\nid: first\nPRIVATE_MALFORMED\n---\n", "malformed-field", 3, None),
            ("PRIVATE_ABSENT_FENCE\n", "missing-fence", 2, None),
            ("---\nid: PRIVATE_UNTERMINATED\n", "missing-fence", 3, None),
        )
        for raw, reason, line, key in cases:
            with self.subTest(reason=reason, line=line):
                pk.atomic_write(path, raw)
                with self.assertRaises(pk.FrontmatterSyntaxError) as raised:
                    pk.parse_simple_frontmatter(path, defaults, strict=True)
                exc = raised.exception
                self.assertIsInstance(exc, ValueError)
                self.assertEqual((exc.reason, exc.path, exc.line, exc.key), (reason, path, line, key))
                self.assertNotIn("PRIVATE_", str(exc))
                self.assertEqual(str(exc), pk.FrontmatterSyntaxError.REASONS[reason])
                permissive = pk.parse_simple_frontmatter(path, defaults)
                self.assertIsInstance(permissive, dict)
                reference, err = policy_history.capture(self.repo, {"id": "syntax"})
                self.assertIsNone(reference)
                self.assertIn('"%s":%d' % (path, line), err)
                self.assertIn(pk.FrontmatterSyntaxError.REASONS[reason], err)
                self.assertNotIn("PRIVATE_", err)
        pk.atomic_write(path, "---\nid: first\nid: last\nunknown: one\nunknown: two\n---\n")
        self.assertEqual(pk.parse_simple_frontmatter(path, defaults)["id"], "last")
        # Unknown duplicate fields are still ignored, never echoed as keys.
        pk.atomic_write(path, "---\nid: first\nPRIVATE_UNKNOWN: one\nPRIVATE_UNKNOWN: two\n---\n")
        self.assertEqual(pk.parse_simple_frontmatter(path, defaults, strict=True)["id"], "first")
        self.assertFalse(os.path.exists(policy_history.path()))

    def test_syntax_diagnostic_escapes_and_bounds_path_and_redacts_exception_chain(self):
        from helm.store import load
        # Parser-owned location metadata can itself contain terminal controls.
        # Mock only reading bytes, so the real parser creates the typed error.
        path = '/synthetic/"\\\n\t\x1b' + "\U0001f600" * 300
        with mock.patch("builtins.open", mock.mock_open(read_data="---\nid: first\nid: PRIVATE_VALUE\n---\n")):
            with self.assertRaises(pk.FrontmatterSyntaxError) as raised:
                pk.parse_simple_frontmatter(path, {"id": ""}, strict=True)
        exc = raised.exception
        exc.__cause__ = ValueError("PRIVATE_CHAIN")
        exc.args = ("PRIVATE_EXCEPTION_TEXT",)
        with mock.patch.object(load, "_policy_hits", side_effect=exc):
            reference, err = policy_history.capture(self.repo, {"id": "bounded"})
        self.assertIsNone(reference)
        self.assertIn("(truncated)", err)
        self.assertIn(policy_history.json.dumps("\n\t" + chr(27))[1:-1], err)
        self.assertIn(r'\"', err)
        self.assertTrue(err.isascii())
        self.assertTrue(all(ord(char) >= 32 for char in err))
        self.assertLessEqual(len(err), 4096)
        self.assertNotIn("PRIVATE_", err)
        self.assertIn('duplicate frontmatter field (field "id")', err)
        self.assertFalse(os.path.exists(policy_history.path()))

    def test_invalid_prior_diagnoses_before_append_and_valid_memory_repairs(self):
        from helm.store import load
        self.policy(["family:claude"])
        path = os.path.join(home.global_dir(), "premises", "prior-synthetic-memory.md")
        row = self.add(recipient="reviewer", ref=self.side)
        before = eventledger.events(dispatches.ledger_path())
        malformed = (
            "---\nname: \ndescription: \n---\nPRIVATE_BODY\n",
            "---\nid: PRIVATE_INCOMPLETE_PRIOR\n---\n",
            "---\nname: valid-name\ndescription: valid-description\npolicy_kind: approval-tier\n---\n",
        )
        with self.verdict_author(), mock.patch.object(
                dispatches.gate, "bind", return_value=("VERIFIED", "b" * 16, "test receipt")), \
                mock.patch.object(eventledger, "append_unlocked", wraps=eventledger.append_unlocked) as append:
            for raw in malformed:
                with self.subTest(raw=raw):
                    pk.atomic_write(path, raw)
                    self.assertIsNone(load._parse_prior(path))
                    with self.assertRaises(load.InvalidPriorError) as raised:
                        load._parse_prior(path, strict=True)
                    exc = raised.exception
                    self.assertIsInstance(exc, ValueError)
                    self.assertNotIsInstance(exc, pk.FrontmatterSyntaxError)
                    self.assertEqual((exc.reason, exc.path), ("invalid-prior", path))
                    self.assertEqual(str(exc), load.InvalidPriorError.MESSAGE)
                    verdict, err = dispatches.mark_verdict(
                        row["id"], self.side, "reviewed", polarity="approve", bind_author=True)
                    self.assertIsNone(verdict)
                    self.assertIn('"%s": %s' % (path, load.InvalidPriorError.MESSAGE), err)
                    self.assertNotIn("PRIVATE_", err)
                    append.assert_not_called()
                    self.assertEqual(eventledger.events(dispatches.ledger_path()), before)
                    self.assertFalse(os.path.exists(policy_history.path()))
            # Preserve BOTH existing bulk-memory identity arms without treating
            # empty name+description as a newly valid schema.
            for identity in ("name: valid-memory\ndescription: \n",
                             "name: \ndescription: valid-memory\n"):
                pk.atomic_write(path, "---\n" + identity + "---\nPRIVATE_BODY\n")
                self.assertIsNone(load._parse_prior(path))
                self.assertIsNone(load._parse_prior(path, strict=True))
                self.assertEqual(len(load._policy_hits("approval-tier", strict=True)), 1)
            verdict, err = dispatches.mark_verdict(
                row["id"], self.side, "reviewed", polarity="approve", bind_author=True)
            self.assertIsNone(err, err)
            retained_then_verdict = [policy_history.path(), dispatches.ledger_path()]
            # The real producer also writes its attestation sidecars. Assert
            # exactly the two authority-ledger appends, in commit order.
            self.assertEqual([call.args[0] for call in append.call_args_list
                              if call.args[0] in retained_then_verdict], retained_then_verdict)
        self.assert_authorized(verdict)

    def test_invalid_prior_diagnostic_escapes_path_and_ignores_exception_text(self):
        from helm.store import load
        path = "/synthetic/" + chr(27) + "\n" + "\U0001f600" * 300
        exc = load.InvalidPriorError(path)
        exc.args = ("PRIVATE_EXCEPTION_TEXT",)
        exc.__cause__ = ValueError("PRIVATE_CHAIN")
        with mock.patch.object(load, "_policy_hits", side_effect=exc):
            reference, err = policy_history.capture(self.repo, {"id": "invalid-prior"})
        self.assertIsNone(reference)
        self.assertIn("(truncated)", err)
        self.assertIn(load.InvalidPriorError.MESSAGE, err)
        self.assertTrue(err.isascii())
        self.assertTrue(all(ord(char) >= 32 for char in err))
        self.assertLessEqual(len(err), 4096)
        self.assertNotIn("PRIVATE_", err)
        self.assertFalse(os.path.exists(policy_history.path()))

    def test_generic_policy_failures_remain_type_only_and_do_not_append(self):
        from helm.store import load
        errors = (ValueError("PRIVATE_VALUE"), PermissionError("PRIVATE_PATH"),
                  UnicodeDecodeError("utf8", b"PRIVATE_BYTES", 0, 1, "PRIVATE_REASON"))
        for exc in errors:
            with self.subTest(kind=type(exc).__name__), \
                    mock.patch.object(load, "_policy_hits", side_effect=exc), \
                    mock.patch.object(eventledger, "append_unlocked") as append:
                reference, err = policy_history.capture(self.repo, {"id": "generic"})
                self.assertIsNone(reference)
                self.assertEqual(err, "approval policy read failed: " + type(exc).__name__)
                append.assert_not_called()
        self.assertFalse(os.path.exists(policy_history.path()))

    def test_strict_policy_preserves_shadowing_and_same_bytes(self):
        from helm.store import load
        self.policy(["family:claude"])
        project = "fixture-project"
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            project: {"name": project, "path": self.repo}}})
        memory = os.path.join(self.tmp, "project-memory")
        with mock.patch.object(home, "claude_memory_dir_for", return_value=memory):
            # A same-id retired narrow prior shadows the global live one.
            narrow = dict(load._policy_hits("approval-tier")[0], status="retired")
            store.write_prior(narrow, root_dir=os.path.join(home.project_dir(project), "premises"))
            reference, err = policy_history.capture(self.repo, {"id": "shadow"})
            self.assertIsNone(err, err)
            record, err = policy_history.resolve(reference, {"id": "shadow"})
            self.assertIsNone(err, err)
            self.assertIsNone(record["policy"])
            # Remove the shadow only in the fixture; missing optional roots are
            # allowed, but the global declaration must be visible again.
            narrow_path = load._find("test-approval-tier", project=project)["path"]
            os.unlink(narrow_path)
            path = load._policy_hits("approval-tier")[0]["path"]
            original_open = open
            reads = []
            def counted(filename, *args, **kwargs):
                if filename == path:
                    reads.append(filename)
                return original_open(filename, *args, **kwargs)
            with mock.patch("builtins.open", side_effect=counted):
                reference, err = policy_history.capture(self.repo, {"id": "one-read"})
            self.assertIsNone(err, err)
            self.assertEqual(reads, [path])
            record, err = policy_history.resolve(reference, {"id": "one-read"})
            self.assertEqual(record["policy"]["policy_members"], ["family:claude"])

    def test_strict_adopted_root_denial_and_dangling_source_refuse(self):
        project = "fixture-project"
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            project: {"name": project, "path": self.repo}}})
        memory = os.path.join(self.tmp, "project-memory")
        original_scandir = os.scandir
        def denied_adopted(directory):
            if directory == memory:
                raise PermissionError("test adopted root denied")
            return original_scandir(directory)
        with mock.patch.object(home, "claude_memory_dir_for", return_value=memory):
            good, err = policy_history.capture(self.repo, {"id": "absent-adopted"})
            self.assertIsNone(err, err)
            self.assertIsNotNone(good)
            with mock.patch("helm.store.load.os.scandir",
                            side_effect=denied_adopted):
                denied, why = policy_history.capture(self.repo, {"id": "denied-adopted"})
            self.assertIsNone(denied)
            self.assertIn("read failed", why)
            os.symlink(os.path.join(self.tmp, "missing-target"), memory)
            denied, why = policy_history.capture(self.repo, {"id": "dangling-adopted"})
            self.assertIsNone(denied)
            self.assertIn("read failed", why)
            os.unlink(memory)
            os.makedirs(memory)
            os.symlink(os.path.join(self.tmp, "missing-prior"), os.path.join(memory, "prior-tier.md"))
            denied, why = policy_history.capture(self.repo, {"id": "dangling-prior"})
            self.assertIsNone(denied)
            self.assertIn("read failed", why)

    def test_strict_root_registry_failure_is_not_global_only_policy(self):
        from helm import registry
        # Healthy absent registry/root is a measured no-policy positive control.
        reference, err = policy_history.capture(self.repo, {"id": "empty"})
        self.assertIsNone(err, err)
        self.assertIsNotNone(reference)
        self.assertIsNone(policy_history.resolve(reference, {"id": "empty"})[0]["policy"])
        before = eventledger.events(policy_history.path())
        for path in (home.registry_path(), home.authored_path()):
            original_open = open
            def denied_registry(filename, *args, **kwargs):
                if filename == path:
                    raise PermissionError("test registry denied")
                return original_open(filename, *args, **kwargs)
            with mock.patch("builtins.open", side_effect=denied_registry):
                denied, why = policy_history.capture(self.repo, {"id": "denied-registry"})
            self.assertIsNone(denied)
            self.assertIn("read failed", why)
            for raw in ("{broken}", "{}", '{"projects":{"bad":null}}',
                        '{"projects":{"bad":{"path":false}}}',
                        '{"projects":{"bad":{"cwds":false}}}',
                        '{"projects":{"bad":{"cv_scope":false}}}',
                        '{"projects":{"bad":{"path":"/x","cv_scope":{"cwd_prefixes":false}}}}',
                        '{"projects":{"bad":{"path":"/x"}},"projects":{}}'):
                with self.subTest(path=path, raw=raw):
                    pk.atomic_write(path, raw)
                    denied, why = policy_history.capture(self.repo, {"id": "bad-roots"})
                    self.assertIsNone(denied)
                    self.assertIn("read failed", why)
                    os.unlink(path)
        self.assertEqual(eventledger.events(policy_history.path()), before)
        # Strict merge is read-only even for a mixed-era registry.
        mixed = {"version": 1, "projects": {"fixture": {"path": self.repo, "notes": "old"}}}
        pk.write_json(home.registry_path(), mixed)
        self.assertIn("fixture", registry.load(strict=True)["projects"])
        self.assertEqual(pk.read_json(home.registry_path()), mixed)
        self.assertFalse(os.path.exists(home.authored_path()))

    def test_policy_archive_is_one_sealed_read_and_invalidates_web_freshness(self):
        from helm import web_land_model as model
        self.policy(["family:claude"])
        verdict = self.record()
        with mock.patch.object(model._LrReadSet, "_read_receipts",
                               wraps=model._LrReadSet._read_receipts) as read:
            with model._lr_snapshot() as reads:
                for _ in range(3):
                    self.assertEqual(dispatches.approval_tier_for_verdict(verdict), ("ok", None))
                witness = reads.witness()
            self.assertEqual(read.call_count, 1)
        self.assertIsInstance(witness, str)
        self.assertEqual(model._LrReadSet().recheck(witness), witness)
        # Only the archive changes: neither the verdict nor current policy does.
        pk.atomic_write(policy_history.path(), "{broken}\n")
        self.assertNotEqual(model._LrReadSet().recheck(witness), witness)
        with model._lr_snapshot():
            state, why = dispatches.approval_tier_for_verdict(verdict)
        self.assertEqual(state, "unknown")
        self.assertIsNotNone(why)

    def test_policy_archive_is_indexed_once_per_nonweb_projection(self):
        from helm import projscope
        self.policy(["family:claude"])
        verdict = self.record()
        with mock.patch.object(policy_history.eventledger, "checked_events",
                               wraps=eventledger.checked_events) as read:
            with projscope.scope():
                for _ in range(3):
                    self.assertEqual(dispatches.approval_tier_for_verdict(verdict), ("ok", None))
            self.assertEqual(read.call_count, 1)
            pk.atomic_write(policy_history.path(), "")
            with projscope.scope():
                state, why = dispatches.approval_tier_for_verdict(verdict)
            self.assertEqual(read.call_count, 2)
        self.assertEqual(state, "unknown")
        self.assertIn("missing", why)
