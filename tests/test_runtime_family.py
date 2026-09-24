#!/usr/bin/env python3
"""Runtime family evidence: the harness's own environment is testimony.

The 2026-08-01 drain blocker, measured end-to-end: the approval tier admits
family:claude, but family evidence comes only from minted proxy records or a
roster runtime stamp with runtime_verified=True — and every NATIVE claude
seat had runtime={} empty, so every one of its approves read tier-unknown and
no discharge could consume them. The contrary pile could not drain.

The fix mirrors the existing pi leg for harness identity, then narrows family:
CLAUDECODE is inherited and identifies only the claude-code harness; native
Claude family requires CLAUDE_CODE_SESSION_ID, backend=native, and no
ANTHROPIC_BASE_URL override. Proxy seats derive family from proxywatch's exact
session-to-live-route proof instead of this environment label.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helm import dispatches, proxywatch, seats, seats_runtime, store  # noqa: E402

ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "HELM_CHAT_NAME", "HELM_CHAT_ROOM",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "CLAUDECODE", "ANTHROPIC_BASE_URL", "HELM_MODEL_FAMILY",
            "HELM_AGENT_HARNESS", "HELM_MODEL_BACKEND", "PI_CODING_AGENT")


def derived(env):
    """What the ROSTER sees: translator output through the one validator.

    The translator hands RAW candidates on now — the parse and the rejection
    both belong to `_runtime_metadata`, so a malformed launch stamp is judged
    at the same boundary a malformed direct call is. These arms therefore bind
    the composed contract rather than an intermediate shape no caller trusts.
    """
    return seats._runtime_metadata(seats._runtime_environment(env))


class RuntimeEnvironmentTest(unittest.TestCase):
    """Direct unit arms on the env translator — every leg, both polarities."""

    def test_session_bearing_claude_runtime_testifies_native_family(self):
        out, _rej = derived({"CLAUDE_CODE_SESSION_ID": "x" * 8})
        self.assertEqual(out.get("agent_harness"), "claude")
        self.assertEqual(out.get("family"), "claude")
        self.assertEqual(out.get("backend"), "native")

    def test_inherited_CLAUDECODE_identifies_harness_only(self):
        out, _rej = derived({"CLAUDECODE": "1"})
        self.assertEqual(out.get("agent_harness"), "claude")
        self.assertNotIn("family", out)
        self.assertNotIn("backend", out)

    def test_a_base_url_override_silences_the_family_leg_only(self):
        """A proxy seat runs claude-code pointed elsewhere: the harness fact
        survives, the family fact is unknowable from env alone."""
        out, _rej = derived({
            "CLAUDE_CODE_SESSION_ID": "s", "ANTHROPIC_BASE_URL":
            "http://127.0.0.1:8317/v1"})
        self.assertEqual(out.get("agent_harness"), "claude")
        self.assertNotIn("family", out)

    def test_an_explicit_launch_stamp_always_outranks_the_derive(self):
        out, _rej = derived({
            "CLAUDE_CODE_SESSION_ID": "s", "HELM_MODEL_FAMILY": "codex"})
        self.assertEqual(out.get("family"), "codex")

    def test_pi_keeps_precedence_and_blocks_the_claude_family_leg(self):
        """pi deliberately traverses claude-adjacent env; harness=pi means
        the claude-family derivation may not fire."""
        out, _rej = derived({
            "PI_CODING_AGENT": "true", "CLAUDE_CODE_SESSION_ID": "s"})
        self.assertEqual(out.get("agent_harness"), "pi")
        self.assertNotIn("family", out)

    def test_an_explicit_proxy_backend_contradicts_native_family(self):  # noqa: VACUOUS_ASSERTION — native positive control proves the family leg fires
        """backend=proxy is launch testimony that the serving
        path is non-native — family must not derive even without a base-url;
        backend=native is the positive control that the gate is the backend,
        not the mere presence of the field."""
        out, _rej = derived({
            "CLAUDE_CODE_SESSION_ID": "s", "HELM_MODEL_BACKEND": "proxy"})
        self.assertEqual(out.get("agent_harness"), "claude")
        self.assertEqual(out.get("backend"), "proxy")
        self.assertNotIn("family", out)
        out, _rej = derived({
            "CLAUDE_CODE_SESSION_ID": "s", "HELM_MODEL_BACKEND": "router"})
        self.assertNotIn("family", out)
        out, _rej = derived({
            "CLAUDE_CODE_SESSION_ID": "s", "HELM_MODEL_BACKEND": "native"})
        self.assertEqual(out.get("family"), "claude")

    def test_uppercase_valid_stamps_normalize_and_still_derive(self):  # noqa: VACUOUS_ASSERTION — detector silence arm
        """codex meld e:1785568214: the validator's contract is
        case-insensitive, so the derive predicates must read values the way it
        will. My raw pass-through compared unnormalized strings and silently
        stopped deriving family for a capitalized-but-VALID launch stamp."""
        out, rejected = derived({
            "CLAUDE_CODE_SESSION_ID": "s", "HELM_AGENT_HARNESS": "Claude"})
        self.assertFalse(rejected)
        self.assertEqual(out.get("agent_harness"), "claude")
        self.assertEqual(out.get("family"), "claude")
        out, rejected = derived({
            "CLAUDE_CODE_SESSION_ID": "s", "HELM_MODEL_BACKEND": "Native"})
        self.assertFalse(rejected)
        self.assertEqual(out.get("backend"), "native")
        self.assertEqual(out.get("family"), "claude")
        out, rejected = derived({
            "CLAUDE_CODE_SESSION_ID": "s", "HELM_MODEL_BACKEND": "PROXY"})
        self.assertFalse(rejected)
        self.assertEqual(out.get("backend"), "proxy")
        self.assertNotIn("family", out)      # case does not weaken the gate

    def test_a_rejected_explicit_stamp_stays_unknown_never_rederived(self):  # noqa: VACUOUS_ASSERTION — each absence is paired with a positive rejection or harness control
        """The sharp shape: a malformed explicit stamp fails
        the token check, vanishes from the parsed metadata, and the derive
        legs would have auto-upgraded the seat — a broken launch stamp
        becoming a forged authorization. Raw presence is testimony."""
        out, rejected = derived({
            "CLAUDECODE": "1", "HELM_MODEL_FAMILY": "codex/family"})
        self.assertTrue(rejected)
        self.assertNotIn("family", out)
        out, rejected = derived({
            "CLAUDECODE": "1", "HELM_AGENT_HARNESS": "pi/harness"})
        self.assertTrue(rejected)
        self.assertNotIn("agent_harness", out)
        self.assertNotIn("family", out)
        out, rejected = derived({
            "CLAUDE_CODE_SESSION_ID": "s", "HELM_MODEL_BACKEND": "prox/y"})
        self.assertTrue(rejected)
        self.assertEqual(out.get("agent_harness"), "claude")
        self.assertNotIn("backend", out)
        self.assertNotIn("family", out)
        out, _rej = derived({
            "PI_CODING_AGENT": "true", "HELM_AGENT_HARNESS": "bad/value"})
        self.assertNotIn("agent_harness", out)

    def test_an_unlabelled_environment_stays_unlabelled(self):
        self.assertEqual(derived({}), ({}, False))


class FamilyEvidenceEndToEndTest(unittest.TestCase):
    """The acceptance seam: a self-written join under a native claude env
    yields exactly the evidence _seat_families feeds the approval tier."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-rtfam-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_NAME"] = "native-claude-seat"

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_native_claude_join_mints_verified_family_evidence(self):
        runtime = seats._runtime_environment({"CLAUDE_CODE_SESSION_ID": "sid"})
        seats.write_roster("native-claude-seat", session="sid", runtime=runtime)
        row = seats.roster()["native-claude-seat"]
        self.assertEqual(row["runtime"].get("family"), "claude")
        self.assertIs(row.get("runtime_verified"), True)
        families, why = dispatches._approval_identity_families("native-claude-seat")
        self.assertIsNone(why, why)
        self.assertEqual(families, {"claude"})

    def test_verified_native_runtime_without_a_backend_stamp_keeps_authority(self):  # noqa: VACUOUS_ASSERTION — verified row + family/v4/anchor controls prove only absent backend is under test
        seats.write_roster("native-claude-seat", runtime={
            "agent_harness": "claude", "family": "claude"})
        row = seats.roster()["native-claude-seat"]
        self.assertIs(row.get("runtime_verified"), True)
        self.assertNotIn("backend", row["runtime"])
        families, evidence, anchor, why = \
            dispatches._approval_identity_family_evidence("native-claude-seat")
        self.assertIsNone(why, why)
        self.assertEqual(families, {"claude"})
        self.assertEqual(evidence["v"], 4)
        self.assertIsNotNone(anchor)
        self.assertIsNone(dispatches._family_evidence_error(
            evidence, "native-claude-seat", "claude", anchor))

    def test_a_proxied_join_leaves_family_evidence_absent_not_guessed(self):  # noqa: VACUOUS_ASSERTION — exact current proxy testimony is positively written before approval authority is withheld
        runtime = seats._runtime_environment({
            "CLAUDE_CODE_SESSION_ID": "sid",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317/v1"})
        seats.write_roster("native-claude-seat", session="sid", runtime=runtime)
        with mock.patch.object(proxywatch, "proxy_runtime_snapshot") as snapshot:
            families, why = dispatches._approval_identity_families(
                "native-claude-seat")
        self.assertIsNone(families)
        self.assertIn("no measured exact-session proxy runtime stamp", why)
        snapshot.assert_not_called()

    def test_proxy_stamp_family_must_match_the_reproved_route(self):
        runtime = {"agent_harness": "claude", "family": "family-a",
                   "backend": "proxy"}
        proof = {"session": "measured-session"}
        seats.write_roster("native-claude-seat", session=proof["session"],
                           runtime=runtime)
        with mock.patch.object(proxywatch, "_proxy_proof_runtime",
                               return_value=(runtime, None)):
            entry, err = seats.stamp_proxy_runtime(
                proof["session"], runtime, proof)
        self.assertIsNone(err, err)
        self.assertEqual(entry["source"], "proxywatch")
        with mock.patch.object(
                proxywatch, "_proxy_proof_runtime",
                return_value=(runtime, None)), mock.patch.object(
                    proxywatch, "proxy_runtime_snapshot",
                    return_value=("family-b", proof, None)):
            families, why = dispatches._approval_identity_families(
                "native-claude-seat")
        self.assertIsNone(families)
        self.assertIn("contradicts its exact-session stamp", why)

    def test_a_prior_session_measurement_never_authorizes_the_current_session(self):  # noqa: VACUOUS_ASSERTION — the old exact proxywatch entry is the positive control before current-session authority is withheld
        runtime = {"agent_harness": "claude", "family": "claude",
                   "backend": "proxy"}
        seats.write_roster("native-claude-seat", session="old-session",
                           runtime=runtime)
        proof = {"session": "old-session"}
        with mock.patch.object(proxywatch, "_proxy_proof_runtime",
                               return_value=(runtime, None)):
            entry, err = seats.stamp_proxy_runtime(
                "old-session", runtime, proof)
        self.assertIsNone(err, err)
        self.assertEqual(entry["source"], "proxywatch")
        seats.write_roster("native-claude-seat", session="current-session",
                           runtime=runtime)
        with mock.patch.object(proxywatch, "proxy_runtime_snapshot") as snapshot:
            families, why = dispatches._approval_identity_families(
                "native-claude-seat")
        self.assertIsNone(families)
        self.assertIn("no measured exact-session proxy runtime stamp", why)
        snapshot.assert_not_called()


class HostProvenStampCarriesTheRowSummaryTest(unittest.TestCase):
    """A fact established off the launch path must outlive the session id.

    THREE WRITERS ESTABLISH RUNTIME IDENTITY AND ALL THREE OWE THE SAME PAIR.
    The seat's own join writes the exact per-session entry and the row-level
    summary; the host-owned lifecycle bind and proxywatch's measurement are
    the other two. The exact entry is keyed BY SESSION ID and the summary is
    what the seat's NEXT session inherits, because `write_roster`'s
    carry-forward leg reads `row["runtime_verified"]`. A writer that records
    only the exact half therefore establishes the seat's runtime for exactly
    one session id and nothing beyond it -- the same fact present on one entry
    path and absent on the rest. These arms bind both halves on both
    non-launch writers.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-rtcarry-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_NAME"] = "measured-seat"

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    #: The claude-code AGENT HARNESS, which is a runtime axis and not a
    #: seat: the roster happens to hold a row spelled the same way.
    ROUTE = {"agent_harness": "claude",  # noqa: SEAT_NAME — the harness axis
             "family": "route-family",
             "backend": "proxy"}

    def _stamp(self, session, runtime=None):
        runtime = runtime or self.ROUTE
        proof = {"session": session}
        with mock.patch.object(proxywatch, "_proxy_proof_runtime",
                               return_value=(runtime, None)):
            return seats.stamp_proxy_runtime(session, runtime, proof)

    def test_a_measured_proxy_stamp_writes_the_row_level_summary(self):  # noqa: VACUOUS_ASSERTION — the BEFORE arm names the absence and the AFTER arm asserts the same observable equals ROUTE unconditionally in the same call
        seats.write_roster("measured-seat", session="s1")
        before = seats.roster()["measured-seat"]
        self.assertIsNone(before.get("runtime"))
        self.assertIsNot(before.get("runtime_verified"), True)
        entry, err = self._stamp("s1")
        self.assertIsNone(err, err)
        self.assertEqual(entry["source"], "proxywatch")
        after = seats.roster()["measured-seat"]
        self.assertEqual(after["runtime"], self.ROUTE)
        self.assertIs(after.get("runtime_verified"), True)

    def test_the_next_session_inherits_what_the_measurement_established(self):  # noqa: VACUOUS_ASSERTION — every arm is a positive equality on the carried entry; the only assertIsNone is on the writer error channel
        seats.write_roster("measured-seat", session="s1")
        _entry, err = self._stamp("s1")
        self.assertIsNone(err, err)
        # The seat's NEXT session arrives with no runtime evidence of its own
        # — a resume, a rebind, a /clear. Before the summary was carried this
        # leg had nothing to carry forward and the exact entry stayed keyed to
        # the dead id.
        seats.write_roster("measured-seat", session="s2")
        row = seats.roster()["measured-seat"]
        self.assertEqual(row.get("session"), "s2")
        carried = (row.get("runtime_sessions") or {}).get("s2")
        self.assertIsInstance(carried, dict)
        self.assertIs(carried.get("verified"), True)
        self.assertEqual(carried.get("runtime"), self.ROUTE)
        runtime, verified = seats_runtime.runtime_for_session(row, "s2")
        self.assertIs(verified, True)
        self.assertEqual(runtime, self.ROUTE)

    def test_a_lifecycle_bind_carries_the_entry_it_retained_not_its_argument(self):  # noqa: VACUOUS_ASSERTION — the terminal arms assert the row summary EQUALS the retained measurement; the assertIsNone is the writer error channel
        """A lifecycle bind that finds a prior MEASUREMENT keeps it and drops
        its own launch label; the summary must agree with what was retained,
        or the two surfaces answer differently about one session."""
        seats.write_roster("measured-seat", session="s1")
        _entry, err = self._stamp("s1")
        self.assertIsNone(err, err)
        label = {"agent_harness": "claude",  # noqa: SEAT_NAME — harness axis
                 "family": "label-family",
                 "backend": "proxy"}
        with mock.patch.object(proxywatch, "_proxy_proof_runtime",
                               return_value=(self.ROUTE, None)):
            entry, err = seats.bind_lifecycle_runtime(
                "measured-seat", "s1", label)
        self.assertIsNone(err, err)
        self.assertEqual(entry["source"], "proxywatch")
        row = seats.roster()["measured-seat"]
        self.assertEqual(row["runtime"], self.ROUTE)
        self.assertIs(row.get("runtime_verified"), True)

    def test_a_lifecycle_bind_with_no_prior_measurement_carries_its_own_stamp(self):
        seats.write_roster("measured-seat", session="s1")
        before = seats.roster()["measured-seat"]
        self.assertIsNone(before.get("runtime"))
        stamp = {"agent_harness": "claude",  # noqa: SEAT_NAME — harness axis
                 "family": "lifecycle-family",
                 "backend": "proxy"}
        entry, err = seats.bind_lifecycle_runtime("measured-seat", "s1", stamp)
        self.assertIsNone(err, err)
        self.assertEqual(entry["source"], "lifecycle")
        row = seats.roster()["measured-seat"]
        self.assertEqual(row["runtime"], stamp)
        self.assertIs(row.get("runtime_verified"), True)

    def test_evidence_about_another_session_never_moves_the_summary(self):
        """The summary is the fallback `runtime_for_session` serves for the
        row's CURRENT session and for no other, so evidence about an older id
        may not write it. The positive arm runs in the same call, on the same
        observable, so a refusal cannot be a silently broken helper."""
        row = {"session": "s2", "runtime": {"family": "standing"},
               "runtime_verified": True}
        entry = {"runtime": self.ROUTE, "verified": True,
                 "source": "proxywatch"}
        seats_runtime._carry_row_summary(row, "s1", entry)
        self.assertEqual(row["runtime"], {"family": "standing"})
        seats_runtime._carry_row_summary(row, "s2", entry)
        self.assertEqual(row["runtime"], self.ROUTE)

    def test_an_unverified_entry_never_upgrades_the_summary(self):
        row = {"session": "s1", "runtime": {"family": "standing"},
               "runtime_verified": True}
        seats_runtime._carry_row_summary(
            row, "s1", {"runtime": self.ROUTE, "verified": False,
                        "source": "runtime-conflict"})
        self.assertEqual(row["runtime"], {"family": "standing"})
        seats_runtime._carry_row_summary(
            row, "s1", {"runtime": {}, "verified": True})
        self.assertEqual(row["runtime"], {"family": "standing"})
        seats_runtime._carry_row_summary(
            row, "s1", {"runtime": self.ROUTE, "verified": True})
        self.assertEqual(row["runtime"], self.ROUTE)


class ContradictedJoinTest(unittest.TestCase):
    """codex meld e:1785566436, the six pins. An explicit-but-REJECTED stamp
    translates to {} exactly like a genuinely evidence-free beat did, and
    write_roster's preserve-on-empty law then kept stale verified authority
    alive — the validation-then-drop class one layer downstream of the
    round-2 finding. Three cases now: replace, preserve, CLEAR."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-contra-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_NAME"] = "seat-under-test"

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed_verified_claude(self):
        seats.write_roster("seat-under-test", runtime={
            "agent_harness": "claude", "family": "claude"})
        row = seats.roster()["seat-under-test"]
        self.assertEqual(row["runtime"].get("family"), "claude")
        self.assertIs(row.get("runtime_verified"), True)   # the trap is armed

    def test_pin1_an_invalid_only_join_clears_stale_verified_authority(self):  # noqa: VACUOUS_ASSERTION — seed_verified_claude positively arms stale authority
        """codex's exact repro."""
        self.seed_verified_claude()
        runtime = seats._runtime_environment({
            "CLAUDECODE": "1", "HELM_AGENT_HARNESS": "pi/harness"})
        seats.write_roster("seat-under-test", runtime=runtime)
        row = seats.roster()["seat-under-test"]
        self.assertNotIn("runtime", row)
        self.assertNotIn("runtime_verified", row)
        families, why = dispatches._approval_identity_families("seat-under-test")
        self.assertIsNone(families)
        self.assertIn("no verified native runtime", why)

    def test_pin2_malformed_family_or_backend_in_a_non_claude_env_clears(self):
        for bad in ({"HELM_MODEL_FAMILY": "codex/family"},
                    {"HELM_MODEL_BACKEND": "prox/y"}):
            with self.subTest(env=bad):
                self.seed_verified_claude()
                runtime = seats._runtime_environment(bad)
                seats.write_roster("seat-under-test", runtime=runtime)
                row = seats.roster()["seat-under-test"]
                self.assertNotIn("runtime", row)
                self.assertNotIn("runtime_verified", row)

    def test_pin3_an_evidence_free_beat_preserves_a_good_stamp(self):
        """The control that keeps CLEAR honest: an ordinary presence beat
        carries no runtime evidence and must not disturb the label."""
        self.seed_verified_claude()
        seats.write_roster("seat-under-test", runtime=seats._runtime_environment({}))
        row = seats.roster()["seat-under-test"]
        self.assertEqual(row["runtime"].get("family"), "claude")
        self.assertIs(row.get("runtime_verified"), True)

    def test_pin4_valid_metadata_still_replaces(self):
        self.seed_verified_claude()
        runtime = seats._runtime_environment({"HELM_MODEL_FAMILY": "codex"})
        seats.write_roster("seat-under-test", runtime=runtime)
        row = seats.roster()["seat-under-test"]
        self.assertEqual(row["runtime"].get("family"), "codex")
        self.assertIs(row.get("runtime_verified"), True)

    def test_pin5_only_known_validated_fields_are_ever_persisted(self):
        """No private marker exists to leak now — the validator owns the
        rejection fact and returns it beside the metadata. What lands is
        exactly the known, validated fields."""
        self.seed_verified_claude()
        runtime = seats._runtime_environment({
            "CLAUDECODE": "1", "HELM_MODEL_FAMILY": "bad/value"})
        seats.write_roster("seat-under-test", runtime=runtime)
        row = seats.roster()["seat-under-test"]
        # PARTIAL rejection: the harness survived and REPLACES the whole
        # label, so the stale family authority is gone either way — replace
        # overwrites the dict, it never merges a survivor onto stale fields.
        self.assertEqual(set(row["runtime"]) - set(seats._RUNTIME_ENV), set())
        self.assertNotIn("family", row["runtime"])
        families, why = dispatches._approval_identity_families("seat-under-test")
        self.assertIsNone(families)
        self.assertIn("no verified native runtime", why)
        runtime2 = seats._runtime_environment({"HELM_MODEL_FAMILY": "codex"})
        seats.write_roster("seat-under-test", runtime=runtime2)
        stored = seats.roster()["seat-under-test"]["runtime"]
        self.assertEqual(set(stored) - set(seats._RUNTIME_ENV), set())
        self.assertEqual(stored.get("family"), "codex")

    def test_pin6_a_clear_removes_authority_and_never_creates_it(self):
        """The direction that matters: this path can only WEAKEN a label, so
        a foreign mirror gains nothing from it."""
        self.seed_verified_claude()
        runtime = seats._runtime_environment({"HELM_AGENT_HARNESS": "bad/one"})
        seats.write_roster("seat-under-test", runtime=runtime)
        families, why = dispatches._approval_identity_families("seat-under-test")
        self.assertIsNone(families)
        self.assertIn("no verified native runtime", why)
        state, message = dispatches.approval_tier("seat-under-test")
        self.assertIn(state, ("unknown", "none", "outside"))
        self.assertNotEqual(state, "ok")


class DirectWriterTest(unittest.TestCase):
    """The PUBLIC validation boundary. join(runtime=...) and
    direct launch mirrors never touch the env translator, so a rejection fact
    carried only by the translator left the public writer blind. The validator
    owns both facts now, so every path — env or direct — obeys one law."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-direct-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_NAME"] = "reviewer"

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed(self):
        seats.write_roster("reviewer", runtime={
            "agent_harness": "claude", "family": "claude"})
        row = seats.roster()["reviewer"]
        self.assertIs(row.get("runtime_verified"), True)   # trap armed

    def test_a_direct_malformed_write_clears_stale_authority(self):  # noqa: VACUOUS_ASSERTION — every subtest seeds and asserts verified authority first
        """codex's exact repro, verbatim: write_roster(runtime={'family':
        'codex/family'}) bypasses the translator entirely."""
        for bad in ({"family": "codex/family"},
                    {"agent_harness": "pi/harness"},
                    {"backend": "prox/y"}):
            with self.subTest(runtime=bad):
                self.seed()
                seats.write_roster("reviewer", runtime=bad)
                row = seats.roster()["reviewer"]
                self.assertNotIn("runtime", row)
                self.assertNotIn("runtime_verified", row)
                families, why = dispatches._approval_identity_families("reviewer")
                self.assertIsNone(families)
                self.assertIn("no verified native runtime", why)

    def test_a_direct_empty_write_preserves(self):
        self.seed()
        seats.write_roster("reviewer", runtime={})
        row = seats.roster()["reviewer"]
        self.assertEqual(row["runtime"].get("family"), "claude")
        self.assertIs(row.get("runtime_verified"), True)

    def test_a_direct_valid_write_replaces(self):
        self.seed()
        seats.write_roster("reviewer", runtime={"family": "codex"})
        self.assertEqual(seats.roster()["reviewer"]["runtime"].get("family"),
                         "codex")

    def test_the_validator_reports_both_facts_for_every_caller(self):
        """The whole-object law stated directly: one function, two facts, no
        path around it."""
        self.assertEqual(seats._runtime_metadata({"family": "codex"}),
                         ({"family": "codex"}, False))
        self.assertEqual(seats._runtime_metadata({"family": "bad/one"}),
                         ({}, True))
        self.assertEqual(seats._runtime_metadata({}), ({}, False))
        self.assertEqual(seats._runtime_metadata(None), ({}, False))
        self.assertEqual(
            seats._runtime_metadata({"family": "codex", "backend": "bad/one"}),
            ({"family": "codex"}, True))


class TheRuntimeNamesTheModelTest(unittest.TestCase):
    """WHICH MODEL ANSWERED, as opposed to which family the seat is labelled.

    Both proxy route shapes carry `upstream_model` and
    `_proxy_proof_runtime` collapsed the route to a family and dropped it, so
    every consumer reading `runtime` saw the family and nothing finer. The
    field is a RAW fact and never a tier: it says what served the request, and
    nothing here licenses reading a missing model as "same family"."""

    def test_the_runtime_carries_the_model_the_route_already_bound(self):
        """The route measured it; this asserts it survives the derivation.

        The CONTROL is the family resolved from the same route in the same
        call: if the derivation broke altogether, family would be None and the
        model assertion would be passing on a corpse."""
        from tests._runtime_proof import runtime_proof
        proof = runtime_proof()
        runtime, err = proxywatch._proxy_proof_runtime(proof)
        self.assertIsNone(err)
        self.assertEqual(  # noqa: SEAT_NAME — a catalog FAMILY, which is what this assertion is ABOUT; the collision with a seat of the same name is incidental and a house-convention placeholder would resolve to no family at all
            runtime.get("family"), "ds4pro",
            "control: the family still resolves from this route")
        self.assertEqual(runtime.get("model"),
                         proof["route"]["upstream_model"],
                         "and the model the route bound is no longer dropped")

    def test_a_model_may_carry_the_punctuation_that_REAL_model_ids_use(self):
        """A colon and a slash are ordinary in a model id and forbidden in a
        seat token, and the difference is not cosmetic: a value that fails
        validation sets `rejected`, and `rejected` is what CLEARS a standing
        label. Reusing the seat rule would make a correctly measured model
        ERASE the runtime facts that already work.

        THE CONTROL IS THE FAMILY FIELD IN THE SAME CALL: it still refuses a
        slash, so this proves the model has its OWN rule rather than that
        validation was loosened for everyone."""
        out, rejected = seats._runtime_metadata(
            {"family": "kimi", "model": "nex-n2.5-pro:free"})
        self.assertEqual(out, {"family": "kimi",
                               "model": "nex-n2.5-pro:free"})
        self.assertFalse(rejected)

        out, rejected = seats._runtime_metadata(
            {"family": "codex", "model": "openai/gpt-5.6-sol"})
        self.assertEqual(out.get("model"), "openai/gpt-5.6-sol")
        self.assertFalse(rejected)

        out, rejected = seats._runtime_metadata({"family": "bad/one"})
        self.assertEqual((out, rejected), ({}, True),
                         "control: the FAMILY rule is unchanged and still "
                         "refuses a slash")

    def test_a_model_id_keeps_the_case_its_producer_gave_it(self):
        """Every other field is lowercased; this one is not, deliberately. A
        model id belongs to the PROVIDER's grammar, and re-spelling another
        system's identifier is how two systems come to disagree about which
        one thing they are naming.

        THE CONTROL IS A FIELD THAT IS STILL FOLDED, in the same call."""
        out, rejected = seats._runtime_metadata(
            {"family": "CODEX", "model": "Qwen3-Max"})
        self.assertFalse(rejected)
        self.assertEqual(out.get("model"), "Qwen3-Max",
                         "the provider's spelling is preserved exactly")
        self.assertEqual(out.get("family"), "codex",
                         "control: family is still casefolded, so this is a "
                         "per-field rule and not a blanket change")

    def test_a_malformed_model_is_REJECTED_rather_than_quietly_dropped(self):
        """`rejected` is the difference between "no evidence" and "explicit
        and unreadable", and only the second may clear a standing label."""
        out, rejected = seats._runtime_metadata(
            {"family": "kimi", "model": "not a model"})
        self.assertEqual(out, {"family": "kimi"})
        self.assertTrue(rejected)

        out, rejected = seats._runtime_metadata({"family": "kimi"})
        self.assertEqual((out, rejected), ({"family": "kimi"}, False),
                         "control: an ABSENT model is silence, not a "
                         "rejection, and must not clear anything")

    def test_an_entry_written_BEFORE_a_derived_field_existed_still_verifies(self):
        """THE OUTAGE AN ADDITIVE IMPROVEMENT CAUSES IF NOBODY WRITES THIS ARM.

        `_validated_proxywatch_entry` re-derives the runtime from the entry's
        immutable proof and compares it to the stored testimony. Under
        byte-equality, every field the derivation LEARNS retroactively
        invalidates every row written before it existed — no stored row has
        the new key, the comparison fails for all of them, and every seat
        loses its verified family the moment the new code lands. Measured
        exactly that when `model` arrived: 28 arms red on runtimes that were
        never wrong.

        THE CONTROL IS A CONTRADICTION, not an absence: a stored family that
        DISAGREES with the proof must still be refused, or this would have
        bought compatibility by removing the check."""
        from tests._runtime_proof import runtime_proof
        proof = runtime_proof(session="seat-under-test")
        measured, err = proxywatch._proxy_proof_runtime(proof)
        self.assertIsNone(err)
        self.assertIn("model", measured,
                      "the derivation learns a field the old entry cannot "
                      "have — which is the whole premise of this arm")

        old = {key: value for key, value in measured.items() if key != "model"}
        entry = {"runtime": old, "verified": True, "source": "proxywatch",
                 "proxy_proof": proof}
        self.assertIsNotNone(
            seats_runtime._validated_proxywatch_entry(entry, "seat-under-test"),
            "an entry written before the field existed asserts nothing false "
            "and must keep its authority")

        contradicting = dict(old, family="grok")  # noqa: SEAT_NAME — a catalog FAMILY deliberately DIFFERENT from the one this proof derives; the point is that it contradicts
        entry = {"runtime": contradicting, "verified": True,
                 "source": "proxywatch", "proxy_proof": proof}
        self.assertIsNone(
            seats_runtime._validated_proxywatch_entry(entry, "seat-under-test"),
            "control: stored testimony that CONTRADICTS the proof is still "
            "refused, so tolerance was bought without removing the check")

    def test_the_native_identity_EQUALITY_survives_a_model(self):
        """THE TRAP THIS WHOLE FIELD HAD TO BE DECLARED TO AVOID.

        `dispatches._approval_identity_family_evidence` decides native
        identity with `metadata == runtime`. A key the validator does not know
        is dropped from `metadata`, that equality goes FALSE, and the
        native-family path stops resolving SILENTLY — no error, no red, just a
        seat that can no longer prove its own family. Declaring the field is
        what keeps the round trip exact, and this arm is the thing that goes
        red if a later change smuggles a runtime key in undeclared."""
        runtime = {  # noqa: SEAT_NAME — a catalog FAMILY and a real model id; both are the SUBJECT here, and a placeholder family would not exercise the native branch this equality guards
            "family": "claude", "backend": "native",
            "model": "claude-opus-5"}
        metadata, rejected = seats._runtime_metadata(runtime)
        self.assertFalse(rejected)
        self.assertEqual(metadata, runtime,
                         "a runtime carrying a model must round-trip the "
                         "validator UNCHANGED, or native identity breaks")

        # THE DISCRIMINATOR, and it is what stops this arm being a tautology:
        # the equality MUST be sensitive to an undeclared key, because that
        # sensitivity is the whole failure mode. If the validator ever began
        # passing unknown keys through, the assertion above would still hold
        # and would certify nothing.
        smuggled = dict(runtime, courier="x")
        metadata, _rejected = seats._runtime_metadata(smuggled)
        self.assertNotEqual(metadata, smuggled,
                            "an UNDECLARED key must break the round trip — "
                            "that is exactly how the native-family path dies "
                            "silently, and an arm that cannot see it proves "
                            "nothing about the arm above")


class ResolvedRouteReachesEverySurfaceTest(unittest.TestCase):
    """The seat surfaces say WHICH MODEL ANSWERED, from the stamped proof.

    THE CONTROL IS ONE ALIAS DOWN TWO RUNGS. `ds4-pro` is served by a free
    provider on `deepseek-v4-pro` and by a paid one on
    `deepseek/deepseek-v4-flash`; a surface reading the alias renders those
    two seats identically, which is the exact defect — a seat named for one
    model answering on another with nothing on any screen saying so. Every
    arm here stamps both proofs and requires the two renderings to differ.
    """

    # A CATALOG FAMILY AND ITS PROVIDER BLOCKS, which is what these arms are
    # about: a placeholder name resolves to no route, no family and no rung.
    # The two routes share an alias AND a model and differ only in provider
    # and cost rung, so a surface reading either the alias or the model alone
    # renders these two seats identically.
    FAMILY = "ds4pro"          # noqa: SEAT_NAME — the catalog family under test
    FREE = {"alias": "ds4-pro", "provider": "opencode-go",
            "upstream_model": "deepseek-v4-pro",
            "base_url": "https://opencode.ai/zen/go/v1"}
    PAID = {"alias": "ds4-pro", "provider": "deepseek-direct",
            "upstream_model": "deepseek-v4-pro",
            "base_url": "https://api.deepseek.com/v1"}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-resolved-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        # THE LIVE HOME IS NEVER THE SUBJECT OF AN ARM: assert the redirection
        # took before a single roster byte is written.
        self.assertTrue(os.environ["HELM_HOME"].startswith(self.tmp))

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seat_on(self, seat, route):
        """One seat whose EXACT session carries a measured proof for `route`."""
        from tests._runtime_proof import runtime_proof
        proof = runtime_proof(session="sid-" + seat, route=route)
        runtime, err = proxywatch._proxy_proof_runtime(proof)
        self.assertIsNone(err, err)
        seats.write_roster(seat, session=proof["session"], runtime=runtime)
        _entry, err = seats.stamp_proxy_runtime(proof["session"], runtime,
                                                proof)
        self.assertIsNone(err, err)
        return proof

    def test_the_roster_report_carries_the_route_and_its_rendering(self):
        self._seat_on("free-seat", self.FREE)
        self._seat_on("paid-seat", self.PAID)
        rows = {r["seat"]: r for r in seats.roster_report()["seats"]}
        self.assertEqual(rows["free-seat"]["resolved"]["provider"],
                         "opencode-go")
        self.assertEqual(rows["paid-seat"]["resolved"]["provider"],
                         "deepseek-direct")
        self.assertEqual(
            (rows["free-seat"]["resolved"]["model"],
             rows["free-seat"]["resolved"]["upstream_model"]),
            (rows["paid-seat"]["resolved"]["model"],
             rows["paid-seat"]["resolved"]["upstream_model"]),
            "control: alias AND model are identical on both rungs")
        self.assertNotEqual(rows["free-seat"]["resolved_text"],
                            rows["paid-seat"]["resolved_text"])
        self.assertIn("(free)", rows["free-seat"]["resolved_text"])
        self.assertIn("(paid)", rows["paid-seat"]["resolved_text"])

    def test_the_label_every_roster_surface_prints_names_the_model(self):
        self._seat_on("free-seat", self.FREE)
        self._seat_on("paid-seat", self.PAID)
        labels = {r["seat"]: seats.runtime_label(r)
                  for r in seats.roster_report()["seats"]}
        self.assertIn("opencode-go/deepseek-v4-pro", labels["free-seat"])
        self.assertIn("deepseek-direct/deepseek-v4-pro", labels["paid-seat"])
        self.assertNotEqual(labels["free-seat"], labels["paid-seat"])
        # the light presence bar is the other roster surface and must not
        # word the same seat differently
        light = {r["seat"]: seats.runtime_label(r)
                 for r in seats.presence_report()}
        self.assertEqual(light["paid-seat"], labels["paid-seat"])

    def test_a_seat_with_no_proof_keeps_the_label_it_always_had(self):
        """MUST-HIT CONTROL for every arm above: no proof, no route claim."""
        seats.write_roster("plain-seat", session="s" * 32,
                           runtime={"agent_harness": "claude",  # noqa: SEAT_NAME — the harness/family words of a native runtime
                                    "family": "claude", "backend": "native"})
        row = [r for r in seats.roster_report()["seats"]
               if r["seat"] == "plain-seat"][0]
        self.assertIsNone(row["resolved"])
        self.assertEqual(row["resolved_text"], "")
        self.assertEqual(seats.runtime_label(row),  # noqa: SEAT_NAME — the rendered family/harness words, not a seat
                         "claude · claude/native")

    def test_the_seats_own_answer_about_itself_comes_from_the_proof(self):
        from helm import seat
        self._seat_on("paid-seat", self.PAID)
        phrase = seat.seat_model_phrase("paid-seat", self.FAMILY)
        self.assertEqual(
            phrase, "model ds4-pro -> deepseek-direct/deepseek-v4-pro (paid)")
        # MUST-HIT CONTROL: an unstamped seat is told it is UNMEASURED rather
        # than handed the alias it was named for.
        self.assertIn("UNMEASURED",
                      seat.seat_model_phrase("nobody", self.FAMILY))

    def test_a_verdict_renders_the_route_it_recorded_not_todays(self):
        from helm import dispatches as d
        row = {"verdict_author_runtime_evidence": {
            "resolved": {"agent_harness": "claude", "backend": "proxy",  # noqa: SEAT_NAME — the HARNESS word a proof records, not a seat
                         "family": self.FAMILY, "model": "ds4-pro",
                         "provider": self.PAID["provider"],
                         "upstream_model": self.PAID["upstream_model"]}}}
        self.assertEqual(
            d.verdict_resolved_model(row),
            "ds4-pro -> deepseek-direct/deepseek-v4-pro (paid)")
        free = {"verdict_author_runtime_evidence": {
            "resolved": dict(row["verdict_author_runtime_evidence"]["resolved"],
                             provider="opencode-go")}}
        self.assertEqual(
            d.verdict_resolved_model(free),
            "ds4-pro -> opencode-go/deepseek-v4-pro (free)")
        self.assertNotEqual(d.verdict_resolved_model(free),
                            d.verdict_resolved_model(row),
                            "same alias, same model, different rung")
        # A NATIVE AUTHORITY RECORDS NO ROUTE and prints nothing: an ordinary
        # absence must not render as an alarm beside every claude reviewer.
        native = {"verdict_author_runtime_evidence": {
            "resolved": {"agent_harness": "claude", "backend": "native",  # noqa: SEAT_NAME — the harness and family words a NATIVE authority records
                         "family": "claude", "model": None, "provider": None,
                         "upstream_model": None}}}
        self.assertEqual(d.verdict_resolved_model(native), "")
        self.assertEqual(d.verdict_resolved_model({}), "")


class TheFamilyTheTierJudgedNamesItsOwnSourceTest(unittest.TestCase):
    """`route.APPROVAL_TIER` is a tuple of FAMILY names, so the whole
    cross-family guarantee rests on one word — and two producers write it.

    A MEASURED PROXY ROUTE derives the family from the alias/provider/upstream
    the proof bound; the SEAT'S ROSTER STAMP is the launch seam describing
    itself. Both land in the same `resolved.family` string, and until the axis
    existed a verdict could not say which one it was — a tier computed from
    the weaker input read exactly like one computed from a measurement.
    """

    NATIVE = {"v": 5, "identity": "reviewer", "roster_identity": "reviewer",
              "session": "s", "runtime_verified": True,
              "runtime": {"family": "claude", "agent_harness": "claude",  # noqa: SEAT_NAME — the family/harness words an authority records
                          "backend": "native"}}

    def _proxy(self, upstream="deepseek-v4-flash"):
        """A v3 ENVELOPE as `_verdict_author_runtime_evidence` writes one. The
        axis reader is pure over this shape; the proof's own validation is
        `_verdict_author_runtime_error`'s subject and is exercised there."""
        return {"resolved": {"agent_harness": "claude", "backend": "proxy",  # noqa: SEAT_NAME — the harness word a proof records
                             "family": "ds4pro", "model": "ds4-pro",
                             "provider": "openrouter",
                             "upstream_model": upstream},
                "authority": {"v": 3}}

    def _native(self, **runtime):
        authority = dict(self.NATIVE,
                         runtime=dict(self.NATIVE["runtime"], **runtime))
        return dispatches._verdict_author_runtime_evidence(
            "reviewer", "s", "claude", authority)  # noqa: SEAT_NAME — the FAMILY key the authority declares and this arm's subject, not a seat

    def test_a_measured_route_is_the_model_axis_and_a_roster_stamp_says_so(self):
        model = dispatches.verdict_family_axis(self._proxy())
        self.assertEqual(model, (dispatches.FAMILY_AXIS_MODEL,
                                 "deepseek-v4-flash"))
        roster = dispatches.verdict_family_axis(self._native())
        self.assertEqual(roster, (dispatches.FAMILY_AXIS_ROSTER, None))
        # THE TWO MUST NOT SHARE A VALUE. A seat whose turn a route measured,
        # and a seat whose family nothing measured at all, are the exact pair
        # this row exists to tell apart.
        self.assertNotEqual(model, roster)
        self.assertNotEqual(model[0], roster[0])

    def test_an_unreadable_family_source_is_unknown_and_never_either_word(self):
        # EVERY ARM HERE IS AN ABSENCE, so the model/roster pair is the control
        # and is re-measured in this call on the same observable: the reader
        # does answer MODEL and ROSTER for the inputs that earn them.
        self.assertEqual(dispatches.verdict_family_axis(self._proxy())[0],
                         dispatches.FAMILY_AXIS_MODEL)
        self.assertEqual(dispatches.verdict_family_axis(self._native())[0],
                         dispatches.FAMILY_AXIS_ROSTER)
        unknown = dispatches.FAMILY_AXIS_UNKNOWN
        # A VERSION NEITHER ARM ANTICIPATED. The tempting spelling — model when
        # v3, roster otherwise — would report a future authority as "the seat's
        # roster said so" when no roster was ever read.
        future = dict(self._proxy(), authority={"v": 6})
        self.assertEqual(dispatches.verdict_family_axis(future), (unknown, None))
        # A ROUTE THAT NAMES NO MODEL cannot earn the strongest word here.
        for empty in (None, "", "   "):
            with self.subTest(upstream=empty):
                self.assertEqual(
                    dispatches.verdict_family_axis(self._proxy(empty)),
                    (unknown, None))
        for broken in (None, {}, {"resolved": {}}, {"authority": {"v": 3}},
                       {"resolved": None, "authority": None}):
            with self.subTest(evidence=broken):
                self.assertEqual(dispatches.verdict_family_axis(broken),
                                 (unknown, None))

    def test_the_surface_prints_the_fallback_and_stays_silent_on_an_open_row(self):
        note = dispatches.verdict_family_axis_note
        self.assertEqual(note({"verdict_author_runtime_evidence": self._native()}),
                         dispatches.FAMILY_AXIS_ROSTER)
        self.assertEqual(
            note({"verdict_author_runtime_evidence":
                  dict(self._proxy(), authority={"v": 6})}), "UNKNOWN")
        # A MEASURED ROUTE PRINTS NOTHING HERE because `verdict_resolved_model`
        # already renders it; the control is that the same call sees the two
        # noisy axes above answer.
        self.assertEqual(
            note({"verdict_author_runtime_evidence": self._proxy()}), "")
        # AN OPEN ROW HAS NO VERDICT, so there is no family to describe and
        # UNKNOWN would accuse every line of a table of its own question.
        self.assertEqual(note({"id": "open-row"}), "")
        self.assertEqual(note(None), "")

    def test_a_native_authority_carries_the_model_it_recorded(self):  # noqa: VACUOUS_ASSERTION — the carry assertion IS the unconditional positive control on `resolved["model"]`, the same observable the two absence arms read, in the same call
        # THE PRODUCER EXISTS: `seats_runtime` validates a `model` field, and
        # this branch copied its two neighbours and dropped it.
        carried = self._native(model="claude-opus-5")
        self.assertEqual(carried["resolved"]["model"], "claude-opus-5")
        self.assertEqual(carried["runtime"]["model"], "claude-opus-5")
        # AND IT STILL DOES NOT BUY INDEPENDENCE: family and model come from
        # one self-declaring seam, so the axis is ROSTER either way.
        self.assertEqual(dispatches.verdict_family_axis(carried),
                         (dispatches.FAMILY_AXIS_ROSTER, None))
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: a runtime with no model
        # still resolves None, so the field above is read and not manufactured.
        self.assertIsNone(self._native()["resolved"]["model"])
        self.assertNotIn("model", self._native()["runtime"])


class WeakRungDoesNotWearAStrongFamilysNameTest(unittest.TestCase):
    """The identity collapse, cured in the CATALOG rather than by a guard.

    The measured defect: one family declared two models -- a strong pro id on
    two providers and a cheap non-reasoning id on a third -- so a proof taken
    on the cheap route resolved to that family and carried its approval
    identity. The approval tier admits FAMILIES, so the declaration WAS the
    grant. Splitting the weak route into its own family makes the existing
    family check refuse it, with no second door beside the catalog.
    """

    PRO_FAMILY = "ds4pro"        # noqa: SEAT_NAME — the catalog family under test
    WEAK_FAMILY = "ds4flash"     # noqa: SEAT_NAME — the catalog family under test
    FLASH = {"alias": "deepseek-v4-flash", "provider": "openrouter",  # noqa: SEAT_NAME — the catalog's own provider block name
             "upstream_model": "deepseek/deepseek-v4-flash",
             "base_url": "https://openrouter.ai/api/v1"}

    def test_the_flash_route_no_longer_resolves_to_the_pro_family(self):
        from helm import seat
        family, err = seat.proxy_route_family(self.FLASH)
        self.assertIsNone(err, err)
        self.assertEqual(family, self.WEAK_FAMILY)
        self.assertNotEqual(family, self.PRO_FAMILY)
        # CONTROL: both PRO routes still resolve to the pro family, so the
        # split moved one route and did not break the family it left.
        pro = [r for r in seat.proxy_routes(self.PRO_FAMILY)]
        self.assertEqual(len(pro), 2, pro)
        for route in pro:
            self.assertEqual(seat.proxy_route_family(route),
                             (self.PRO_FAMILY, None))
            self.assertEqual(route["upstream_model"], "deepseek-v4-pro")

    def test_the_approval_tier_admits_the_pro_family_and_not_the_weak_one(self):
        """Through the EXISTING family selector, with no new rule anywhere."""
        from helm import verdict_tier
        policy = {"id": "fleet-approval-tier", "class": "certain",
                  "_policy_confidence_valid": True,
                  "_policy_source_valid": True,
                  "policy_kind": "approval-tier",
                  "policy_members": ["family:" + self.PRO_FAMILY],
                  "policy_reason": "only the declared tier may final-approve"}
        state, why = verdict_tier.evaluate(policy, "seat-under-test",
                                           {self.PRO_FAMILY})
        self.assertEqual((state, why), ("ok", None),
                         "control: the pro family is inside the tier")
        state, why = verdict_tier.evaluate(policy, "seat-under-test",
                                           {self.WEAK_FAMILY})
        self.assertEqual(state, "outside")
        self.assertIn("outside the recorded approval tier", why)

    def test_no_pool_family_declares_two_models_over_the_whole_table(self):
        """THE CLASS, not the instance, asserted OVER THE CATALOG ITSELF.

        A pool is ONE model offered by several vendors. A second model in a
        pool is a route that inherits the family's name, and family is what
        the tier admits -- so the next weak rung declared under a strong
        family goes red HERE, at import and in this arm, rather than in a
        review weeks later. Read off the table so a family nobody has written
        yet is covered.
        """
        # THE PRIVATE INVARIANT COMES FROM THE IMPL AND THE PUBLIC TABLE FROM
        # THE FACADE. _pool_serves_one_model is internal to the catalog and
        # does not belong on helm.seat; the table it guards does, so this
        # scope names both doors and uses each for what it owns.
        from helm import seat, seat_catalog
        self.assertIsNone(seat_catalog._pool_serves_one_model())
        pools = {f: fam["pool_providers"] for f, fam
                 in seat.FAMILIES.items() if fam.get("pool_providers")}
        self.assertTrue(pools, "control: the table still declares pools")
        for family, rows in pools.items():
            self.assertEqual(
                len({row["upstream_model"] for row in rows.values()}), 1,
                "%s serves more than one model" % family)
        # MUST-HIT CONTROL: the exact shape this closes, fed to the same
        # reader, must be REFUSED — otherwise the green above could mean the
        # check looks at nothing.
        collapsed = {"a-family": {"pool_providers": {
            "strong": {"upstream_model": "big-pro"},
            "weak": {"upstream_model": "little-flash"}}}}
        refusal = seat_catalog._pool_serves_one_model(collapsed)
        self.assertIn("a-family", refusal)
        self.assertIn("2 models", refusal)


class TierUnknownKindTest(unittest.TestCase):
    """WHICH KIND OF "helm cannot say" — sabotage at distinct stages.

    The compose refusal used to call every unknown transient and tell the
    reader they heal. Measured false 2026-08-11 — gemini dark on MALFORMED200
    since 16:33Z with no proof stored at all, grok QUOTA-402, kimi
    AUTH-UNAVAILABLE — and a dark upstream stores NOTHING, so it is not a
    stale clock and no re-read reaches it.

    THE STATES MUST NOT BE MISREADABLE AS EACH OTHER, so each arm breaks a
    DIFFERENT stage of the real resolver and pins the kind that stage
    measured. Nothing here hands the resolver a tier state: the roster and the
    proxywatch record are written, and the classification is read back out."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-tierkind-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_NAME"] = "seat-under-test"
        self.policy = mock.patch.object(
            store, "load_certain_policy",
            return_value=({"id": "p1", "policy_reason": "because",
                           "policy_members": ["family:codex"]}, None))
        self.policy.start()
        self.addCleanup(self.policy.stop)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def kind(self, seat="seat-under-test"):
        state, why = dispatches.approval_tier(seat)
        self.assertEqual(state, "unknown", why)
        return dispatches.tier_unknown_kind(state), why

    def proxy_seat(self):
        """A seat that IS proxied and IS stamped — every later arm's baseline,
        so a failure below is the stage under test and not a missing setup.

        The stamp's proof is re-derived at READ time too on this base
        (`seats_runtime._verified_exact_runtime`), so arms that read back
        through the resolver must keep `_proxy_proof_runtime` patched — the
        pattern FamilyEvidenceEndToEndTest already pins."""
        runtime = {"agent_harness": "claude", "family": "codex",
                   "backend": "proxy"}
        proof = {"session": "measured-session"}
        seats.write_roster("seat-under-test", session=proof["session"],
                           runtime=runtime)
        with mock.patch.object(proxywatch, "_proxy_proof_runtime",
                               return_value=(runtime, None)):
            entry, err = seats.stamp_proxy_runtime(
                proof["session"], runtime, proof)
        self.assertIsNone(err, err)
        self.assertEqual(entry["source"], "proxywatch")
        return runtime, proof

    def test_DARK_is_a_seat_that_stores_no_proof_at_all(self):  # noqa: VACUOUS_ASSERTION — every arm asserts a CONCRETE kind constant and a substring of the measured sentence; there is no absence assertion here, and test_a_measured_answer_carries_no_kind_at_all is the ok/outside control on the same call
        """THE FOURTH STATE. Two live shapes on 2026-08-11: an unseated slot
        with no session (three unstamped seats), and a seated seat whose dark
        upstream was never stamped (two proxied seats). Neither is stale
        evidence; there is no evidence."""
        seats.write_roster("seat-under-test", runtime={
            "agent_harness": "claude", "backend": "proxy"})
        kind, why = self.kind()
        self.assertEqual(kind, dispatches.TIER_DARK)
        self.assertIn("no exact roster session", why)

        seats.write_roster("seat-under-test", session="live-session",
                           runtime={"agent_harness": "claude",
                                    "family": "codex", "backend": "proxy"})
        with mock.patch.object(proxywatch, "proxy_runtime_snapshot") as snap:
            kind, why = self.kind()
        self.assertEqual(kind, dispatches.TIER_DARK)
        self.assertIn("no measured exact-session proxy runtime stamp", why)
        # AND NOTHING WAS EVEN ASKED: dark is decided before any live read, so
        # "retry" is not merely useless here, there is no read to retry.
        snap.assert_not_called()

    def test_DAMAGED_is_stored_evidence_that_contradicts_itself(self):
        """Stored records that read wrong forever — the re-proved family
        against the seat's own exact-session stamp, and the policy prior's
        own members. (The source lane's first DAMAGED stage, a proxywatch
        error TAGGED damaged, has no member on this base: proxywatch's
        snapshot errors arrive untagged and land in the UNCLASSIFIED arm.)"""
        runtime, proof = self.proxy_seat()
        with mock.patch.object(
                proxywatch, "_proxy_proof_runtime",
                return_value=(runtime, None)), \
                mock.patch.object(
                    proxywatch, "proxy_runtime_snapshot",
                    return_value=("other-family", proof, None)):
            kind, why = self.kind()
        self.assertEqual(kind, dispatches.TIER_DAMAGED)
        self.assertIn("contradicts its exact-session stamp", why)
        # A SECOND STAGE, in the POLICY rather than the seat.
        with mock.patch.object(
                store, "load_certain_policy",
                return_value=({"id": "p1", "policy_reason": "r",
                               "policy_members": ["family:cod ex"]}, None)):
            kind, why = self.kind()
        self.assertEqual(kind, dispatches.TIER_DAMAGED)
        self.assertIn("malformed selector", why)
        # A THIRD: a declared policy with no stated reason.
        with mock.patch.object(
                store, "load_certain_policy",
                return_value=({"id": "p1", "policy_reason": "",
                               "policy_members": ["family:codex"]}, None)):
            kind, why = self.kind()
        self.assertEqual(kind, dispatches.TIER_DAMAGED)
        self.assertIn("no policy_reason", why)

    def test_TRANSIENT_is_a_live_step_that_failed_with_the_record_intact(self):
        """A roster caught mid-write: the subject is unchanged and the next
        read may answer. (The source lane's other transient member — a canary
        failure proxywatch TAGGED live — has no member on this base, where
        snapshot errors arrive untagged: see the UNCLASSIFIED arm.)"""
        self.proxy_seat()
        with mock.patch.object(seats, "roster_checked", return_value=({}, True)):
            kind, why = self.kind()
        self.assertEqual(kind, dispatches.TIER_TRANSIENT)
        self.assertIn("roster runtime record is unreadable", why)

    def test_UNNAMED_is_a_recipient_that_is_not_one_roster_seat(self):
        """Permanent under re-reading like DAMAGED, and separated from it
        because the cure is neither patience nor a repair — the ROW is wrong.

        TWO SHAPES, TWO SITES. A valid-but-unseated token RESOLVES fine — the
        resolver deliberately permits an address before its seat joins — so it
        reaches UNNAMED at the roster-record check. Only a MALFORMED token
        reaches the resolver's own refusal. Both are pinned."""
        seats.write_roster("seat-under-test", runtime={
            "agent_harness": "claude", "family": "codex", "backend": "native"})
        # THE POSITIVE CONTROL FIRST, on the same call: the seat that DOES
        # resolve answers ok, so the unnamed readings below are about the name
        # and not about a roster this setUp failed to write.
        self.assertEqual(dispatches.approval_tier("seat-under-test")[0], "ok")
        # SHAPE 1 — a well-formed token with no roster row.
        kind, why = self.kind(seat="nobody-by-that-name")
        self.assertEqual(kind, dispatches.TIER_UNNAMED)
        self.assertIn("nobody-by-that-name", why)
        # SHAPE 2 — a token the addressing rules refuse outright.
        kind, why = self.kind(seat="not a seat token/at all")
        self.assertEqual(kind, dispatches.TIER_UNNAMED)
        self.assertIn("exact seat token", why)

    def test_the_evidence_resolver_tags_its_OWN_unresolvable_recipient(self):
        """TWO DOORS ANSWER UNNAMED.

        `approval_tier` resolves the recipient itself and returns UNNAMED
        before the evidence resolver is ever called, so the arm above — which
        goes through `approval_tier` — SHADOWS the second site entirely.

        It is reachable, and it matters: `_approval_identity_family_evidence`
        is called directly by other production paths (contrary-family checks,
        landed-review family checks), and its `why` now carries a kind those
        callers can act on. So the arm must knock on THAT door rather than
        the one in front of it."""
        seats.write_roster("seat-under-test", runtime={
            "agent_harness": "claude", "family": "codex", "backend": "native"})
        # THE POSITIVE CONTROL through the same door: a name that resolves
        # answers with evidence and no `why` at all.
        families, _ev, _anchor, why = \
            dispatches._approval_identity_family_evidence("seat-under-test")
        self.assertIsNone(why, why)
        self.assertEqual(families, {"codex"})
        # ITS OWN REFUSAL, not the roster-record check one step later — a
        # malformed token is the only input that reaches it, which is why an
        # unseated-but-valid name looked like coverage and was not.
        families, _ev, _anchor, why = \
            dispatches._approval_identity_family_evidence("not a seat token/x")
        self.assertIsNone(families)
        self.assertIn("exact seat token", why)
        self.assertEqual(dispatches._kind_of(why), dispatches.TIER_UNNAMED)
        # and the site one step later, which answers the same word for the
        # other shape, so this arm covers BOTH doors of the resolver
        families, _ev, _anchor, why = \
            dispatches._approval_identity_family_evidence("nobody-by-that-name")
        self.assertIsNone(families)
        self.assertIn("no unique canonical roster runtime record", why)
        self.assertEqual(dispatches._kind_of(why), dispatches.TIER_UNNAMED)

    def test_an_untagged_unknown_is_UNCLASSIFIED_and_never_inferred(self):  # noqa: VACUOUS_ASSERTION — the DARK no-stamp reading is the tagged positive control through the identical approval_tier consumer, and the untagged arm asserts a concrete kind constant plus the sentence, never an absence
        """The absence of a classification is its own answer. proxywatch on
        this base answers its four failure worlds (nothing recorded, record
        malformed, record aged out, live re-proof failed) in ONE untagged
        sentence, so no kind was measured — and the resolver says exactly
        that rather than borrowing a neighbouring sentence's confidence."""
        # THE TAGGED POSITIVE CONTROL through the identical consumer: a seat
        # with a session and no stamp reads DARK, so the unclassified reading
        # below is the missing tag and not a dead double.
        seats.write_roster("seat-under-test", session="live-session",
                           runtime={"agent_harness": "claude",
                                    "family": "codex", "backend": "proxy"})
        self.assertEqual(self.kind()[0], dispatches.TIER_DARK)
        runtime, _proof = self.proxy_seat()
        with mock.patch.object(
                proxywatch, "_proxy_proof_runtime",
                return_value=(runtime, None)), \
                mock.patch.object(
                    proxywatch, "proxy_runtime_snapshot",
                    return_value=(None, None, "same old sentence")):
            kind, why = self.kind()
        self.assertEqual(kind, dispatches.TIER_UNCLASSIFIED)
        self.assertIn("same old sentence", why)

    def test_a_measured_answer_carries_no_kind_at_all(self):
        """THE MUST-MISS CONTROL. Without it every arm above passes for a
        build that stamps a kind on every answer — including "ok" and the
        DEFINITIVE "outside", whose whole meaning is that nothing is unknown
        about it and no re-read is owed."""
        seats.write_roster("seat-under-test", runtime={
            "agent_harness": "claude", "family": "codex", "backend": "native"})
        state, why = dispatches.approval_tier("seat-under-test")
        self.assertEqual(state, "ok", why)
        self.assertIsNone(dispatches.tier_unknown_kind(state))
        with mock.patch.object(
                store, "load_certain_policy",
                return_value=({"id": "p1", "policy_reason": "r",
                               "policy_members": ["family:gemini"]}, None)):
            state, why = dispatches.approval_tier("seat-under-test")
        self.assertEqual(state, "outside", why)
        self.assertIsNone(dispatches.tier_unknown_kind(state))
        self.assertFalse(dispatches.tier_unknown_heals_itself(state))

    def test_the_carrier_is_indistinguishable_from_the_plain_state_word(self):  # noqa: VACUOUS_ASSERTION — every assertion is a positive equality against a concrete expected value ("unknown", the json text, the plain-word anchor); the assertRaises is the negative half and cannot pass by silence
        """THE COMPATIBILITY CONTRACT, pinned at the boundaries that would
        break silently. The kind rides a `str` subclass precisely so every
        call site, test double and persisted proof stays byte-identical — and
        `_proof_anchor` hashes tier_state into an IMMUTABLE landing proof
        (`landing_review_tier_state`, `confirmation_tier_state`), so a
        carrier that serialized differently would invalidate every anchor
        ever minted, on a path no unit arm touches. json.dumps normalizes str
        subclasses; this is the arm that says so out loud rather than
        trusting it."""
        import json
        carried = dispatches.TierUnknown(dispatches.TIER_DARK, "unknown")
        self.assertEqual(carried, "unknown")
        self.assertIn(carried, ("outside", "unknown"))
        self.assertEqual("%s" % carried, "unknown")
        self.assertEqual(json.dumps({"tier": carried}), '{"tier": "unknown"}')
        self.assertEqual({carried: 1}["unknown"], 1)
        self.assertEqual(
            dispatches._proof_anchor("d", {"tier_state": carried}),
            dispatches._proof_anchor("d", {"tier_state": "unknown"}))
        with self.assertRaises(ValueError):
            dispatches.TierUnknown("invented-kind", "unknown")

    def test_only_TRANSIENT_is_allowed_to_promise_a_retry(self):
        """The single predicate every surface must ask before wording an
        unknown, pinned in both directions so a later 'helpful' widening of
        the heals-itself set reddens here first."""
        self.assertTrue(dispatches.tier_unknown_heals_itself(
            dispatches.TIER_TRANSIENT))
        for kind in (dispatches.TIER_DARK, dispatches.TIER_DAMAGED,
                     dispatches.TIER_UNNAMED, dispatches.TIER_UNCLASSIFIED,
                     None):
            self.assertFalse(dispatches.tier_unknown_heals_itself(kind), kind)


if __name__ == "__main__":
    unittest.main()
