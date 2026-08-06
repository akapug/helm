"""Tests for helm/claimev.py — the claim-evidence rung.

Owner canon 2026-07-31: a claim that SOUNDS settled feels finished, and
summarising someone else's summary feels like work. The rung warns when the
outgoing message claims what the turn never measured.

TWO ARMS, mandatory per the brief: a message that EARNS its claims (the turn
ran a measurement tool) must produce silence; a message that does not must
produce the named line. An always-silent rung and a working one must be
distinguishable, or the rung proves nothing.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from helm import claimev


def _rec(content):
    """A transcript record. tool_result content is written as role=user, the
    shape the real transcript uses (meld R2 depends on it: a user-role
    tool_result is NOT a turn boundary); everything else is assistant."""
    items = content if isinstance(content, list) else [content]
    role = "user" if any(isinstance(i, dict) and i.get("type") == "tool_result"
                         for i in items) else "assistant"
    return json.dumps({"message": {"role": role, "content": content}})


class ClaimEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-claimev-")
        self.tp = os.path.join(self.tmp, "t.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, *contents):
        with open(self.tp, "w") as f:
            f.write("\n".join(_rec(c) for c in contents))

    def test_an_unearned_claim_fires_and_names_itself(self):
        """FIRE arm: proof word + bare count + unresolved sha, no tool calls
        this turn. Every claim must be named with WHY it is unearned — the
        no-empty-refusal law."""
        self._write([{"type": "text", "text": "turn start"}],
                    [{"type": "text",
                      "text": "verified: 5705 tests green at deadbeef1234"}])
        lines, readable = claimev.gate_lines(self.tp)
        self.assertTrue(readable)
        self.assertTrue(any("verified" in l for l in lines), lines)
        self.assertTrue(any("5705 tests" in l for l in lines), lines)
        self.assertTrue(any("deadbeef1234" in l for l in lines), lines)

    def test_an_earned_claim_is_silent(self):
        """SILENCE arm: the claim's VALUES anchor to a successful tool's
        output this turn. Meld R1's exact anchor: the count (5705) AND the
        sha (deadbeef1234) both appear in the measurement the turn ran, so
        every claim is earned."""
        self._write(
            [{"type": "text", "text": "turn start"}],
            [{"type": "tool_use", "name": "Bash", "id": "t1", "input": {}}],
            [{"type": "tool_result", "tool_use_id": "t1",
              "is_error": False,
              "content": [{"type": "text",
                           "text": "Ran 5705 tests OK at deadbeef1234"}]}],
            [{"type": "text",
              "text": "verified: 5705 tests green at deadbeef1234"}])
        lines, readable = claimev.gate_lines(self.tp)
        self.assertTrue(readable)
        self.assertEqual(lines, [])

    def test_a_chat_post_is_not_a_measurement(self):
        """A turn whose only tool is chat.post earns nothing — that is the
        summarising-a-summary defect exactly."""
        self._write([{"type": "text", "text": "turn start"}],
                    [{"type": "tool_use", "name": "ChatPost", "input": {}}],
                    [{"type": "text", "text": "confirmed: the land is green"}])
        lines, _ = claimev.gate_lines(self.tp)
        self.assertTrue(any("confirmed" in l for l in lines), lines)

    def test_a_tool_from_the_PREVIOUS_turn_does_not_earn_this_one(self):
        """The evidence must be THIS turn's. A Bash call before the previous
        assistant text is last turn's work and earns nothing here."""
        self._write([{"type": "tool_use", "name": "Bash", "input": {}}],
                    [{"type": "text", "text": "previous turn's message"}],
                    [{"type": "text", "text": "verified: 42% faster"}])
        lines, _ = claimev.gate_lines(self.tp)
        self.assertTrue(any("42%" in l for l in lines), lines)

    def test_an_unreadable_transcript_is_skipped_never_clean(self):
        """The third law: a blind read must not masquerade as a healthy
        message. readable=False is the honest answer."""
        lines, readable = claimev.gate_lines(
            os.path.join(self.tmp, "no-such.jsonl"))
        self.assertEqual(lines, [])
        self.assertFalse(readable)

    def test_a_landed_claim_needs_a_trunk_check_this_turn(self):
        self._write([{"type": "text", "text": "turn start"}],
                    [{"type": "text", "text": "landed on main at abc1234"}])
        lines, _ = claimev.gate_lines(self.tp)
        self.assertTrue(any("landed" in l for l in lines), lines)


class Codex2DefectTest(unittest.TestCase):
    """codex-2's five-defect FIX on cc24ee2b, each pinned."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-claimev-c2-")
        self.tp = os.path.join(self.tmp, "t.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, *contents):
        with open(self.tp, "w") as f:
            f.write("\n".join(_rec(c) for c in contents))

    def test_d1_a_failed_bash_earns_nothing(self):
        """codex-2 #1: a FAILED measurement (is_error=True) must not earn the
        claim — a red command is not a measurement of green."""
        self._write(
            [{"type": "text", "text": "turn start"}],
            [{"type": "tool_use", "name": "Bash", "id": "t1", "input": {}}],
            [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
              "content": [{"type": "text", "text": "command failed"}]}],
            [{"type": "text", "text": "verified: all tests green"}])
        lines, _ = claimev.gate_lines(self.tp)
        self.assertTrue(any("verified" in l for l in lines), lines)

    def test_d1_an_empty_monitor_earns_nothing(self):
        """codex-2 #1: a Monitor/TaskOutput that returned NO content is not a
        measurement — it observed nothing."""
        self._write(
            [{"type": "text", "text": "turn start"}],
            [{"type": "tool_use", "name": "Monitor", "id": "t1", "input": {}}],
            [{"type": "tool_result", "tool_use_id": "t1", "is_error": False,
              "content": []}],
            [{"type": "text", "text": "measured: 42% land rate"}])
        lines, _ = claimev.gate_lines(self.tp)
        self.assertTrue(any("42%" in l for l in lines), lines)

    def test_d2_quoted_and_negated_claims_do_not_fire(self):
        """codex-2 #2: a claim in quotes (reported speech) or negated (a
        denial) is not the seat's own settled assertion. POSITIVE CONTROL:
        the SAME claim unquoted and unnegated MUST fire — proving the absence
        is the scrubbing, not a blind detector."""
        self._write([{"type": "text", "text": "turn start"}],
                    [{"type": "text",
                      "text": 'the report claimed "42% land rate" but I did '
                              'NOT verify it'}])
        lines, _ = claimev.gate_lines(self.tp)
        self.assertEqual(lines, [], lines)
        # control: unquoted + asserted, no tool this turn -> fires
        self._write([{"type": "text", "text": "turn start"}],
                    [{"type": "text", "text": "I measured 42% land rate"}])
        ctrl, _ = claimev.gate_lines(self.tp)
        self.assertTrue(any("42%" in l for l in ctrl), ctrl)

    def test_d2b_a_uuid_is_not_a_sha_claim(self):
        """codex-2's UUID note: a session/task UUID is an identifier, not a
        commit, so it must not fire the unresolved-sha arm. POSITIVE CONTROL:
        a real commit SHA in the same position DOES fire."""
        self.assertEqual(claimev.findings(
            "session c93cafba11404a5c99ad59e82cfabfb2 resumed", {}), [])
        self.assertEqual(claimev.findings(
            "session 58e6f94a-1140-4a5c-99ad-59e82cfabfb2 resumed", {}), [])
        shapes = [s for s, _, _ in claimev.findings(
            "tip 331adcb7762dac24dfea80bd2d70974acf2551a7 approved", {})]
        self.assertIn("unresolved-sha", shapes)

    def test_d3_earlier_same_turn_tools_count(self):
        """codex-2 #3: a turn that goes text -> tool -> final text still earns
        from the tool that precedes the final message (the old boundary
        dropped tools before an intervening text). POSITIVE CONTROL: the SAME
        final claim with the tool REMOVED must fire — proving the silence is
        the earned tool, not a blind detector."""
        self._write(
            [{"type": "text", "text": "previous turn"}],
            [{"type": "text", "text": "thinking out loud first"}],
            [{"type": "tool_use", "name": "Bash", "id": "t1", "input": {}}],
            [{"type": "tool_result", "tool_use_id": "t1", "is_error": False,
              "content": [{"type": "text", "text": "Ran 5705 OK"}]}],
            [{"type": "text", "text": "verified: 5705 tests green"}])
        lines, _ = claimev.gate_lines(self.tp)
        self.assertEqual(lines, [], lines)
        # control: identical shape but NO tool/result -> the claim is unearned
        self._write(
            [{"type": "text", "text": "previous turn"}],
            [{"type": "text", "text": "thinking out loud first"}],
            [{"type": "text", "text": "verified: 5705 tests green"}])
        ctrl, _ = claimev.gate_lines(self.tp)
        self.assertTrue(any("verified" in l for l in ctrl), ctrl)


class MeldContractTest(unittest.TestCase):
    """codex-2's meld contract (the one-pass bar), each repro pinned with a
    positive control. The four confirmed defects from the 8dbfcba5 FIX."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-claimev-meld-")
        self.tp = os.path.join(self.tmp, "t.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, *contents):
        with open(self.tp, "w") as f:
            f.write("\n".join(_rec(c) for c in contents))

    def _write_prompt(self, text):
        with open(self.tp, "a") as f:
            f.write("\n" + json.dumps(
                {"message": {"role": "user", "content": text}}))

    def test_r1_an_unrelated_successful_tool_earns_nothing(self):
        """Meld R1: relevance is an EXACT ANCHOR. A successful Bash that ran
        `pwd` (returning a generic path) contains neither the claimed number
        nor sha, so it earns NOTHING. POSITIVE CONTROL: a Bash whose output
        contains the claimed value earns the silence."""
        self._write(
            [{"type": "text", "text": "turn start"}],
            [{"type": "tool_use", "name": "Bash", "id": "t1",
              "input": {"command": "pwd"}}],
            [{"type": "tool_result", "tool_use_id": "t1", "is_error": False,
              # a GENERIC path, never the box's real one (never-track guards
              # the real path as a private needle)
              "content": [{"type": "text", "text": "/home/operator/work"}]}],
            [{"type": "text", "text": "verified: 42% land rate"}])
        lines, _ = claimev.gate_lines(self.tp)
        self.assertTrue(any("42%" in l for l in lines), lines)
        # control: the SAME claim, but the tool output contains the value
        self._write(
            [{"type": "text", "text": "turn start"}],
            [{"type": "tool_use", "name": "Bash", "id": "t1", "input": {}}],
            [{"type": "tool_result", "tool_use_id": "t1", "is_error": False,
              "content": [{"type": "text", "text": "land rate: 42%"}]}],
            [{"type": "text", "text": "42% land rate"}])
        self.assertEqual(claimev.gate_lines(self.tp)[0], [])

    def test_r1_a_missing_tool_result_is_not_success(self):
        """Meld R1 fail-closed: a tool_use with NO tool_result reads as NOT
        succeeded, never defaulted to success. Drives the REAL _turn path
        through the LANDED arm (the arm where a phantom success flips the
        outcome: a git input naming origin/main earns 'landed' only if the
        result was real). Mutation: default a missing result to success ->
        the landed claim goes silent -> this goes RED."""
        self._write(
            [{"type": "text", "text": "turn start"}],
            [{"type": "tool_use", "name": "Bash", "id": "t1",
              "input": {"command":
                        "git merge-base --is-ancestor abc1234 origin/main"}}],
            [{"type": "text", "text": "landed on main"}])
        lines, _ = claimev.gate_lines(self.tp)
        self.assertTrue(any("landed" in l for l in lines), lines)

    def test_r2_commentary_within_a_turn_does_not_truncate(self):
        """Meld R2: the boundary is the previous genuine USER PROMPT, not any
        assistant text. tool -> commentary -> final keeps the tool. POSITIVE
        CONTROL: the previous turn's tool (before the prompt) does NOT earn."""
        self._write(
            [{"type": "text", "text": "previous turn"}],
            [{"type": "tool_use", "name": "Bash", "id": "t0", "input": {}}],
            [{"type": "tool_result", "tool_use_id": "t0", "is_error": False,
              "content": [{"type": "text", "text": "old result"}]}])
        self._write_prompt("measure the tests now")
        self._write(
            [{"type": "tool_use", "name": "Bash", "id": "t1", "input": {}}],
            [{"type": "tool_result", "tool_use_id": "t1", "is_error": False,
              "content": [{"type": "text", "text": "Ran 5705 OK"}]}],
            [{"type": "text", "text": "commentary mid-turn"}],
            [{"type": "text", "text": "5705 tests green"}])
        lines, _ = claimev.gate_lines(self.tp)
        self.assertEqual(lines, [], lines)
        # control: the SAME final claim with the in-turn tool REMOVED fires —
        # the previous turn's (pre-prompt) tool must not carry it
        self._write(
            [{"type": "text", "text": "previous turn"}],
            [{"type": "tool_use", "name": "Bash", "id": "t0", "input": {}}],
            [{"type": "tool_result", "tool_use_id": "t0", "is_error": False,
              "content": [{"type": "text", "text": "Ran 5705 OK"}]}])
        self._write_prompt("now claim it")
        self._write([{"type": "text", "text": "5705 tests green"}])
        ctrl, _ = claimev.gate_lines(self.tp)
        self.assertTrue(any("5705" in l for l in ctrl), ctrl)

    def test_r3_identical_text_in_two_turns_refires(self):
        """Meld R3: message_id keys on the record's stable uuid. Same text in
        two DISTINCT records (different uuid) yields different ids; the SAME
        record re-read yields the same id."""
        import json as _json
        def rec_u(text, uuid):
            return _json.dumps({"uuid": uuid, "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}]}})
        p1 = os.path.join(self.tmp, "a.jsonl")
        p2 = os.path.join(self.tmp, "b.jsonl")
        with open(p1, "w") as f:
            f.write(rec_u("verified: 42% land rate", "uuid-AAA"))
        with open(p2, "w") as f:
            f.write(rec_u("verified: 42% land rate", "uuid-BBB"))
        m1, m2 = claimev.assessment(p1)[2], claimev.assessment(p2)[2]
        # POSITIVE CONTROL: assessment returns real, non-empty, stable ids —
        # the NotEqual below is only meaningful if both are real values
        self.assertTrue(m1 and m2, (m1, m2))
        self.assertNotEqual(m1, m2)
        self.assertEqual(m1, claimev.assessment(p1)[2])  # same record -> same id

    def test_r4_smartquote_contraction_and_clause_local_negation(self):
        """Meld R4: smart quotes and code spans are scrubbed; clause-local
        negation keeps a later positive ('not verified before; now verified').
        POSITIVE CONTROL for each scrubbed/coded form."""
        # smart-quoted claim is reported speech
        self.assertEqual(claimev.findings(
            "the report said “42% land rate”", []), [])
        # code-span claim is not a claim
        self.assertEqual(claimev.findings(
            "it returned `42% land rate`", []), [])
        # negated clause then positive clause: the positive survives
        hits = claimev.findings("not verified before; now verified", [])
        self.assertTrue(any(s == "proof-word" for s, _, _ in hits), hits)

    def test_r4_codex2_hold_violations(self):
        """codex-2's HOLD probes, pinned: contraction negation,
        64-hex SHA, and ASCII single-quoted reported speech. Each with a
        positive control proving the detector isn't just blind."""
        # a contraction negation is a denial, not a claim
        self.assertEqual(claimev.findings("this wasn't verified", []), [])
        self.assertEqual(claimev.findings("I didn't confirm it", []), [])
        # ASCII single-quoted speech is reported, not claimed
        self.assertEqual(claimev.findings(
            "the report said '42% land rate'", []), [])
        # a full 64-hex SHA stands alone as a claim
        self.assertTrue(any(s == "unresolved-sha" for s, _, _ in
                            claimev.findings("tip " + "a" * 64, [])))
        # POSITIVE CONTROLS: the same shapes as real claims DO fire
        self.assertTrue(any(s == "proof-word" for s, _, _ in
                            claimev.findings("verified: all green", [])))
        self.assertTrue(any(s == "bare-number" for s, _, _ in
                            claimev.findings("I measured 42% land rate", [])))

    def test_r4_sha_context_rule(self):
        """Meld R4 SHA rule: full-40 hex stands alone; short hex needs commit
        context; UUIDs and generic hex never fire. Positive controls for the
        contextual short SHA and the full-40."""
        self.assertEqual(claimev.findings(
            "session c93cafba11404a5c99ad59e82cfabfb2 resumed", []), [])
        self.assertEqual(claimev.findings(
            "the token deadbeef was cached", []), [])
        # short SHA with commit context fires
        self.assertTrue(any(s == "unresolved-sha" for s, _, _ in
                            claimev.findings("landed commit abc1234 today", [])))
        # full-40 stands alone
        self.assertTrue(any(s == "unresolved-sha" for s, _, _ in
                            claimev.findings(
                                "tip 331adcb7762dac24dfea80bd2d70974acf2551a7", [])))
class OwnerLayerBarTest(unittest.TestCase):
    """codex-2's EXPANDED immutable bar (2026-08-01): the whole-object
    owner-layer pass over R1-R4. Each repro is pinned with a positive
    control, and the grammars are pinned as TABLES — a mutation removing
    apostrophe classification, UNKNOWN, contrast reset, or 64-acceptance
    goes red."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-claimev-bar-")
        self.tp = os.path.join(self.tmp, "t.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, *contents):
        with open(self.tp, "w") as f:
            f.write("\n".join(_rec(c) for c in contents))

    # --- R1: exact-token anchoring -------------------------------------

    def test_r1_a_substring_is_not_a_measurement(self):
        """'142 rows' must NOT earn a '42%' claim — a substring is not the
        token that was claimed. POSITIVE CONTROL: the exact token earns."""
        tools = [{"name": "Bash", "input": "wc -l x", "succeeded": True,
                  "content": "142 rows"}]
        hits = claimev.findings("42% land rate", tools)
        self.assertTrue(any(s == "bare-number" for s, _, _ in hits), hits)
        tools[0]["content"] = "land rate: 42%"
        self.assertEqual(claimev.findings("42% land rate", tools), [])

    def test_r1_the_input_echo_is_not_the_measurement(self):
        """`echo 42` returning 'done' earns nothing — the command's echo of
        the claim is not a measurement of it. The value must be in the
        RESULT content."""
        tools = [{"name": "Bash", "input": "echo 42", "succeeded": True,
                  "content": "done"}]
        hits = claimev.findings("42% land rate", tools)
        self.assertTrue(any(s == "bare-number" for s, _, _ in hits), hits)

    def test_r1_landed_needs_the_canonical_trunk_not_local_main(self):
        """`git branch --show-current` returning local 'main' is NOT evidence
        of a land. The trunk anchor is origin/main (or refs/remotes/
        origin/main), in a GIT command. POSITIVE CONTROL: an origin/main
        resolution earns."""
        local = [{"name": "Bash", "input": "git branch --show-current",
                  "succeeded": True, "content": "main"}]
        hits = claimev.findings("landed on main", local)
        self.assertTrue(any(s == "landed-claim" for s, _, _ in hits), hits)
        echo = [{"name": "Bash", "input": "echo main",
                 "succeeded": True, "content": "main"}]
        hits = claimev.findings("landed on main", echo)
        self.assertTrue(any(s == "landed-claim" for s, _, _ in hits), hits)
        canon = [{"name": "Bash",
                  "input": "git merge-base --is-ancestor abc1234 origin/main",
                  "succeeded": True, "content": ""}]
        self.assertEqual(claimev.findings("landed on main", canon), [])

    # --- R2: both envelopes; an unsupported envelope is never clean -----

    def test_r2_the_flat_envelope_parses(self):
        """The flat {role,content} envelope is a first-class transcript: a
        claim in it fires exactly as in the wrapped envelope, and an earned
        claim in it is silent."""
        flat = json.dumps({"role": "assistant", "content": [
            {"type": "text", "text": "verified: 42% land rate"}]})
        with open(self.tp, "w") as f:
            f.write(flat)
        lines, readable = claimev.gate_lines(self.tp)
        self.assertTrue(readable)
        self.assertTrue(any("42%" in l for l in lines), lines)
        # control: flat envelope, the value measured this turn -> silent
        flat_tool = json.dumps({"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "id": "t1", "input": {}}]})
        flat_res = json.dumps({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": False,
             "content": [{"type": "text", "text": "42%"}]}]})
        with open(self.tp, "w") as f:
            f.write("\n".join([flat_tool, flat_res, flat]))
        self.assertEqual(claimev.gate_lines(self.tp)[0], [])

    def test_r2_an_unsupported_envelope_is_unknown_never_clean(self):
        """A message-shaped record missing its role fits NEITHER envelope:
        readable=False, never a vacuous CLEAN. POSITIVE CONTROL: a pure
        wrapped transcript is readable."""
        with open(self.tp, "w") as f:
            f.write(json.dumps({"message": {"content": "handoff"}}))
        lines, readable = claimev.gate_lines(self.tp)
        self.assertFalse(readable)
        self.assertEqual(lines, [])
        self._write([{"type": "text", "text": "just a note"}])
        self.assertTrue(claimev.gate_lines(self.tp)[1])

    def test_r2_typed_metadata_does_not_poison_real_messages(self):
        """Current transcripts interleave typed metadata with messages. Those
        records are not malformed envelopes; the real measured turn remains
        readable across metadata before, within, and after it."""
        records = [
            {"type": "attachment", "attachment": {}},
            {"message": {"role": "user", "content": "measure it"}},
            {"type": "last-prompt", "lastPrompt": "measure it"},
            {"message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Bash", "id": "t1",
                 "input": {"command": "printf 42"}}]}},
            {"type": "mode", "mode": "default"},
            {"message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "is_error": False, "content": "42"}]}},
            {"type": "queue-operation", "operation": "enqueue"},
            {"uuid": "answer", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "42% land rate"}]}},
            {"type": "ai-title", "aiTitle": "Measured"},
        ]
        with open(self.tp, "w") as f:
            f.write("\n".join(json.dumps(r) for r in records))
        self.assertEqual(claimev.gate_lines(self.tp), ([], True))

    def test_r2_fixture_preserves_records_and_prompt_parsing_is_live(self):
        """The non-vacuous fixture: a transcript BUILT by appends (prompt
        + turn), never rewritten away. The boundary prompt truncates the
        previous turn's tools — deleting prompt parsing leaves the OLD
        tool earning the claim and this goes RED."""
        def append(obj):
            with open(self.tp, "a") as f:
                f.write(json.dumps(obj) + "\n")
        append({"message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "id": "t0", "input": {}}]}})
        append({"message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t0", "is_error": False,
             "content": [{"type": "text", "text": "5705"}]}]}})
        append({"message": {"role": "user", "content": "now claim it"}})
        append({"message": {"role": "assistant", "content": [
            {"type": "text", "text": "5705 tests green"}]}})
        lines, _ = claimev.gate_lines(self.tp)
        self.assertTrue(any("5705" in l for l in lines), lines)

    # --- R3: no-UUID fallback is offset+hash on ONE growing transcript --

    def test_r3_nouuuid_identical_records_in_one_growing_transcript(self):
        """TWO byte-identical no-UUID records in ONE growing transcript: the
        later one is a DISTINCT message and must re-fire (different id),
        while a re-read of the SAME final record stays latched (same id).
        The old text-only hash collapsed the two."""
        def rec(text):
            return json.dumps({"message": {"role": "assistant", "content": [
                {"type": "text", "text": text}]}})
        with open(self.tp, "w") as f:
            f.write(rec("verified: 42% land rate"))
        first = claimev.assessment(self.tp)[2]
        self.assertEqual(first, claimev.assessment(self.tp)[2])  # same record
        with open(self.tp, "a") as f:
            f.write("\n" + rec("verified: 42% land rate"))
        second = claimev.assessment(self.tp)[2]
        self.assertTrue(first and second, (first, second))
        self.assertNotEqual(first, second)                     # later record

    def test_r3_assessment_binds_findings_and_identity_to_one_read(self):
        """The transcript may grow between calls. Assessment must derive the
        finding and latch identity from one owner-layer snapshot."""
        self._write([{"type": "text", "text": "42 tests green"}])
        with mock.patch.object(claimev, "_turn",
                               wraps=claimev._turn) as read_turn:
            lines, readable, mid = claimev.assessment(self.tp)
        self.assertTrue(readable)
        self.assertTrue(lines)
        self.assertTrue(mid)
        self.assertEqual(read_turn.call_count, 1)

    # --- R4: the whole-object grammar, pinned as tables -----------------

    def test_r4_quote_pair_classes(self):
        """Every quote-pair class marks its content reported speech;
        POSITIVE CONTROL: the same claim outside quotes fires."""
        for op, cl in (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")):
            self.assertEqual(claimev.findings(
                "the report said %s42%% land rate%s" % (op, cl), []), [],
                (op, cl))
        self.assertTrue(any(s == "bare-number" for s, _, _ in
                            claimev.findings("I measured 42% land rate", [])))

    def test_r4_code_spans_and_fences(self):
        """Inline code and fenced blocks are not claims; POSITIVE CONTROL:
        the same claim OUTSIDE the span, in this same test, fires."""
        self.assertEqual(claimev.findings(
            "it returned `42% land rate`", []), [])
        self.assertEqual(claimev.findings(
            "```\n42% land rate\n```\n", []), [])
        self.assertTrue(any(s == "bare-number" for s, _, _ in
                            claimev.findings("it returned 42% land rate", [])))

    def test_r4_arbitrary_contraction_is_a_denial(self):
        """ANY word ending n't negates its clause (wasn't/isn't/couldn't/
        wouldn't), not a three-word list; the plain forms too."""
        for w in ("wasn't", "isn't", "didn't", "couldn't", "wouldn’t"):
            self.assertEqual(claimev.findings("this %s verified" % w, []),
                             [], w)
        for w in ("not", "never", "no", "without"):
            self.assertEqual(claimev.findings("%s verified" % w, []), [], w)
        # control: the unnegated claim fires
        self.assertTrue(any(s == "proof-word" for s, _, _ in
                            claimev.findings("verified: all green", [])))

    def test_r4_apostrophe_classification_each_direction(self):
        """The word-internal apostrophe rule is pinned in BOTH directions
        through the ONE classifier (the quote opener): (1) after a
        contraction the rest of the sentence still classifies ('wasn't
        measured; the tip abc1234...' — a blind opener turns the tail into
        quoted speech and the sha goes silent); (2) a contraction inside a
        quote does not END it ('it wasn't verified' stays reported speech
        to the real close)."""
        got = [s for s, _, _ in claimev.findings(
            "this wasn't measured; the tip abc1234 is mine", [])]
        self.assertIn("unresolved-sha", got)
        self.assertEqual(claimev.findings(
            "the report said 'it wasn't verified'", []), [])

    def test_r4_contrast_reset_keeps_the_positive(self):
        """Negation scopes to its clause only — a contrast boundary resets
        it. 'not verified but now verified' keeps the positive; so does
        '; now'."""
        # control, unconditional: with NO contrast the denial stands
        self.assertEqual(claimev.findings("not verified at all", []), [])
        for msg in ("not verified before; now verified",
                    "not verified but now verified",
                    "unproven yesterday, however measured today"):
            hits = claimev.findings(msg, [])
            self.assertTrue(any(s == "proof-word" for s, _, _ in hits), msg)

    def test_r4_unbalanced_delimiter_credits_nothing(self):
        """An unbalanced quote or code tick makes the message
        unclassifiable: NO candidate may be silently credited. POSITIVE
        CONTROL: the balanced form classifies normally."""
        self.assertEqual(claimev.findings(
            'the report said "42% land rate', []), [])
        self.assertEqual(claimev.findings(
            "it returned `42% land rate", []), [])
        self.assertTrue(any(s == "bare-number" for s, _, _ in
                            claimev.findings("I measured 42% land rate", [])))

    def test_r4_sha_length_table(self):
        """The CLOSED length grammar: 7-12 (with context), 40 and 64 fire;
        6, 13-39 (contextual or not), 41-63, 65+ never do. A 32-hex UUID
        with dashes never fires."""
        fires = {7: True, 12: True, 40: True, 64: True,
                 6: False, 13: False, 32: False, 39: False, 41: False,
                 63: False, 65: False}
        for length, expect in fires.items():
            tok = "a" * length
            got = [s for s, _, _ in claimev.findings("tip " + tok, [])]
            self.assertEqual("unresolved-sha" in got, expect, length)
        self.assertEqual(claimev.findings(
            "session 58e6f94a-1140-4a5c-99ad-59e82cfabfb2 resumed", []), [])
        # POSITIVE CONTROL, unconditional: a plain 7-hex tip fires
        self.assertTrue(any(s == "unresolved-sha" for s, _, _ in
                            claimev.findings("tip abc1234", [])))

    def test_r4_contextual_short_sha_and_the_at_trap(self):
        """Short hex fires with commit context ('landed commit abc1234',
        'tip abc1234', 'landed on main at abc1234'); generic 'at' without a
        commit verb never makes one ('token cached at deadbeef')."""
        for msg in ("landed commit abc1234 today", "tip abc1234 gated",
                    "landed on main at abc1234"):
            got = [s for s, _, _ in claimev.findings(msg, [])]
            self.assertIn("unresolved-sha", got, msg)
        self.assertEqual(claimev.findings(
            "token cached at deadbeef", []), [])
        self.assertEqual(claimev.findings(
            "the token deadbeef was cached", []), [])
        # POSITIVE CONTROL, unconditional: context word + short hex fires
        self.assertTrue(any(s == "unresolved-sha" for s, _, _ in
                            claimev.findings("sha deadbeef resolved", [])))

    def test_r4_mutation_killers(self):
        """The four mutations codex-2 named, each driven red HERE:
        apostrophe classification removed -> \"wasn't\" opens a quote and
        the clause dies unbalanced; UNKNOWN removed -> an unbalanced quote
        credits; contrast reset removed -> 'not ... ; now verified' stays
        denied; 64-acceptance removed -> the 64-hex tip goes silent. These
        are the same cases as the pins above, asserted as one table so a
        single mutation fails a single named line."""
        table = [
            ("this wasn't verified", "proof-word", False),       # apostrophe
            ('the report said "42% land rate', "bare-number", False),  # UNKNOWN
            ("not verified before; now verified", "proof-word", True),  # reset
            ("tip " + "a" * 64, "unresolved-sha", True),          # 64
        ]
        for msg, shape, expect in table:
            got = [s for s, _, _ in claimev.findings(msg, [])]
            self.assertEqual(shape in got, expect, (msg, shape))
class CappedBarCalibrationTest(unittest.TestCase):
    """The RULED counter-cap (2026-08-01, opus-integrator): TWO primary-path
    calibrations fixed, everything else pinned as DOCUMENTED KNOWN MISSES.
    The misses are this WARN-only rung's calibrated edge: a pin asserts
    CURRENT behavior so drift is visible — it is not a claim of
    correctness. Revisit trigger: the warn-rate misleads in practice
    (measured), never an unbounded completeness audit."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-claimev-cap-")
        self.tp = os.path.join(self.tmp, "t.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, *contents):
        with open(self.tp, "w") as f:
            f.write("\n".join(_rec(c) for c in contents))

    # --- CALIBRATION 1: the R1 canonical-git inversion ------------------

    def test_cap_r1_the_honest_merge_base_earns(self):
        """The PRIMARY path: `git merge-base --is-ancestor <sha> origin/main`
        succeeds -> 'landed on main' is earned, NOT warned. The pre-cap bug
        matched the trunk against the JSON-serialized input envelope, where
        the quote characters killed the word boundary — an honest
        measurement false-warned (the suppression-training class)."""
        self._write(
            [{"type": "text", "text": "turn start"}],
            [{"type": "tool_use", "name": "Bash", "id": "t1",
              "input": {"command":
                        "git merge-base --is-ancestor abc1234 origin/main"}}],
            [{"type": "tool_result", "tool_use_id": "t1", "is_error": False,
              "content": [{"type": "text", "text": ""}]}],
            [{"type": "text", "text": "landed on main"}])
        self.assertEqual(claimev.gate_lines(self.tp)[0], [])

    def test_cap_r1_contentless_resolution_earns_the_named_sha(self):
        """The primary predicate's exit status resolves its SHA even though it
        returns no content. The number arm stays content-only, and a failed
        predicate earns neither shape."""
        sha = "abc1234"
        landed = [{"name": "Bash", "input":
                   "git merge-base --is-ancestor %s origin/main" % sha,
                   "succeeded": True, "content": ""}]
        self.assertEqual(claimev.findings(
            "landed commit %s on main" % sha, landed), [])

        resolved = [{"name": "Bash", "input": "git cat-file -e %s" % sha,
                     "succeeded": True, "content": ""}]
        self.assertEqual(claimev.findings("tip %s" % sha, resolved), [])

        failed = [dict(resolved[0], succeeded=False)]
        self.assertTrue(any(s == "unresolved-sha" for s, _, _ in
                            claimev.findings("tip %s" % sha, failed)))

        echo = [{"name": "Bash", "input": "echo 42", "succeeded": True,
                 "content": ""}]
        self.assertTrue(any(s == "bare-number" for s, _, _ in
                            claimev.findings("42% land rate", echo)))

    def test_cap_r1_a_chained_echo_does_not_earn(self):
        """The inversion's other side: `git status >/dev/null; echo
        origin/main` is a STATUS command whose echo names the trunk — it
        measured nothing about the land and must still warn."""
        self._write(
            [{"type": "text", "text": "turn start"}],
            [{"type": "tool_use", "name": "Bash", "id": "t1",
              "input": {"command": "git status >/dev/null; echo origin/main"}}],
            [{"type": "tool_result", "tool_use_id": "t1", "is_error": False,
              "content": [{"type": "text", "text": "origin/main"}]}],
            [{"type": "text", "text": "landed on main"}])
        lines, _ = claimev.gate_lines(self.tp)
        self.assertTrue(any("landed" in l for l in lines), lines)

    # --- CALIBRATION 2: the tri-state law (unreadable never credits) ----

    def test_cap_r2_empty_transcript_is_unreadable_not_clean(self):
        """An EMPTY transcript has no outgoing message to judge — nothing
        earned, so not a clean bill."""
        open(self.tp, "w").close()
        lines, readable = claimev.gate_lines(self.tp)
        self.assertEqual(lines, [])
        self.assertFalse(readable)

    def test_cap_r2_an_unmodeled_role_is_unreadable_not_clean(self):
        """A record of a role the rung does not model (system) is content
        the reader cannot account for: unsupported, never clean."""
        with open(self.tp, "w") as f:
            f.write(json.dumps({"role": "system", "content": "policy"}))
        lines, readable = claimev.gate_lines(self.tp)
        self.assertEqual(lines, [])
        self.assertFalse(readable)

    def test_cap_r1_output_trunk_alone_does_not_earn(self):
        """Reverse pin: a result that PRINTS 'origin/main' without the
        command resolving it earns nothing. A mutation re-admitting
        content-side trunk evidence goes red here."""
        self._write(
            [{"type": "text", "text": "turn start"}],
            [{"type": "tool_use", "name": "Bash", "id": "t1",
              "input": {"command": "git fetch --dry-run"}}],
            [{"type": "tool_result", "tool_use_id": "t1", "is_error": False,
              "content": [{"type": "text", "text": "origin/main up to date"}]}],
            [{"type": "text", "text": "landed on main"}])
        lines, _ = claimev.gate_lines(self.tp)
        self.assertTrue(any("landed" in l for l in lines), lines)

    def test_cap_r2_mixed_record_keeps_its_prompt_boundary(self):
        """A user record carrying BOTH a tool_result and genuine text is a
        boundary: the text part truncates the prior turn's evidence, so a
        bare claim after it fires."""
        def append(obj):
            with open(self.tp, "a") as f:
                f.write(json.dumps(obj) + "\n")
        append({"message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "id": "t0", "input": {}}]}})
        append({"message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t0", "is_error": False,
             "content": [{"type": "text", "text": "42"}]},
            {"type": "text", "text": "now claim it yourself"}]}})
        append({"message": {"role": "assistant", "content": [
            {"type": "text", "text": "42% land rate"}]}})
        lines, _ = claimev.gate_lines(self.tp)
        self.assertTrue(any("42%" in l for l in lines), lines)

    def test_cap_r1_no_shell_chain_of_any_kind_earns(self):
        """codex-2's in-cap follow-through: purity rejects EVERY shell
        composition, not just ';' — newline chains, $(...) and backtick
        substitution, and a trailing # comment all name the trunk without
        resolving it. POSITIVE CONTROL: the pure resolution still earns."""
        for cmd in ("git status\necho origin/main",
                    "git status $(echo origin/main)",
                    "git status `echo origin/main`",
                    "git status # origin/main"):
            tools = [{"name": "Bash", "input": cmd, "succeeded": True,
                      "content": "origin/main"}]
            hits = claimev.findings("landed on main", tools)
            self.assertTrue(any(s == "landed-claim" for s, _, _ in hits),
                            cmd)
        pure = [{"name": "Bash",
                 "input": "git merge-base --is-ancestor abc1234 origin/main",
                 "succeeded": True, "content": ""}]
        self.assertEqual(claimev.findings("landed on main", pure), [])

    def test_cap_r2_contentless_user_record_is_unreadable(self):
        """A user record with NO content parts (null, [], {}) is content the
        reader cannot account for: unsupported, never a clean prompt."""
        for content in (None, [], {}):
            with open(self.tp, "w") as f:
                f.write(json.dumps({"message": {"role": "user",
                                                "content": content}}))
            lines, readable = claimev.gate_lines(self.tp)
            self.assertFalse(readable, content)
            self.assertEqual(lines, [])

    def test_cap_r2_ignored_unmodeled_role_would_silent(self):
        """Reverse pin: with the system-role record present, an assistant
        claim that FOLLOWS it still fires (the transcript parses) — so the
        unreadable=False of test_cap_r2_an_unmodeled_role_is_unreadable is
        the ROLE's doing, not a dead file. A mutation silently ignoring
        unmodeled roles goes red: readable would flip True."""
        with open(self.tp, "w") as f:
            f.write(json.dumps({"role": "system", "content": "policy"})
                    + "\n" + json.dumps({"message": {
                        "role": "assistant", "content": [
                            {"type": "text", "text": "42% land rate"}]}}))
        lines, readable = claimev.gate_lines(self.tp)
        self.assertFalse(readable)          # the unmodeled role is surfaced
        self.assertEqual(lines, [])         # and nothing is credited

    # --- DOCUMENTED KNOWN MISSES (pins assert CURRENT behavior) ----------

    def test_miss_r1_only_the_first_numeric_shape_is_checked(self):
        """KNOWN MISS: '42% land rate and 5705 tests green' with no tools
        names only the first bare-number. Revisit if a one-warn-per-shape
        need is measured."""
        hits = claimev.findings("42% land rate and 5705 tests green", [])
        self.assertEqual(len([h for h in hits if h[0] == "bare-number"]), 1)

    def test_miss_r2_only_the_final_text_block_is_audited(self):
        """KNOWN MISS: one assistant record with two text blocks audits only
        the final block; an earlier block's claims escape."""
        rec = json.dumps({"message": {"role": "assistant", "content": [
            {"type": "text", "text": "verified: 42% land rate"},
            {"type": "text", "text": "a note with no claims"}]}})
        with open(self.tp, "w") as f:
            f.write(rec)
        self.assertEqual(claimev.gate_lines(self.tp)[0], [])

    def test_miss_r3_identity_is_tail_relative(self):
        """KNOWN MISS (codex-2's exact repro): in a transcript past the
        256KiB tail, appending a byte-identical final record can yield the
        SAME message id — the latch sees one message, not two. Revisit if a
        latch collision is measured on a real long transcript."""
        rec = json.dumps({"message": {"role": "assistant", "content": [
            {"type": "text", "text": "verified: 42% land rate"}]}})
        # >256KiB of flat-envelope padding BEFORE the first record, so the
        # tail window slides past it when the identical second record lands
        pad = json.dumps({"role": "user", "content": "x" * 3000})
        with open(self.tp, "w") as f:
            f.write("\n".join([pad] * 120) + "\n" + rec)
        first = claimev.assessment(self.tp)[2]
        with open(self.tp, "a") as f:
            f.write("\n" + rec)
        # CURRENT (tail-relative) behavior: the appended identical record
        # collides. Pin asserts the collision is KNOWN, not that it is
        # right. If this pin goes red on a fix, the miss is closed.
        second = claimev.assessment(self.tp)[2]
        self.assertTrue(first and second, (first, second))
        self.assertEqual(first, second)

    def test_miss_r4_lexical_edges(self):
        """KNOWN MISSES, one pin per edge codex-2's audit named: guillemets
        are not quote pairs; double-backtick is not a code fence; 'verified
        with no failures' reads as a denial ('no'); 'not now verified'
        fires (the negation does not reach across 'now'); 'yet' is not a
        contrast word. CURRENT behavior pinned; correctness not claimed."""
        self.assertTrue(any(s == "bare-number" for s, _, _ in
                            claimev.findings("il a dit «42% rate»", [])))
        self.assertTrue(any(s == "bare-number" for s, _, _ in
                            claimev.findings("``42% rate``", [])))
        self.assertEqual(claimev.findings("verified with no failures", []),
                         [])
        self.assertTrue(any(s == "proof-word" for s, _, _ in
                            claimev.findings("not now verified", [])))
        self.assertEqual(claimev.findings("not verified, yet verified", []),
                         [])
