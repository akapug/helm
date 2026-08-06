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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helm import dispatches, seats  # noqa: E402

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
        """codex round 2: backend=proxy is launch testimony that the serving
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
        """codex round 2, the sharp shape: a malformed explicit stamp fails
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
        seats.write_roster("native-claude-seat", runtime=runtime)
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

    def test_a_proxied_join_leaves_family_evidence_absent_not_guessed(self):
        runtime = seats._runtime_environment({
            "CLAUDE_CODE_SESSION_ID": "sid",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317/v1"})
        seats.write_roster("native-claude-seat", runtime=runtime)
        families, why = dispatches._approval_identity_families("native-claude-seat")
        self.assertIsNone(families)
        self.assertIn("no verified native runtime", why)


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
    """codex round 4: the PUBLIC validation boundary. join(runtime=...) and
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


if __name__ == "__main__":
    unittest.main()
