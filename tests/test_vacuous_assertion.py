#!/usr/bin/env python3
"""The staged-test vacuity advisory: three real rotten-green shapes, pinned."""
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helm import vacuous_assertion as V  # noqa: E402


def findings(source):
    return V._analyze_source(source, "tests/test_probe.py", [(1, 999)])


def findings_with(source, modules):
    """Analyse SOURCE against a fake package: {relative path: module source}.

    The rung follows a named constant to the module that defines it, so an
    arm about that resolution has to supply the module. A dict reader keeps
    the arm honest about WHICH file the answer came from — a real one on disk
    could satisfy the assertion for a reason the arm never stated.
    """
    return V._analyze_source(source, "tests/test_probe.py", [(1, 999)],
                             source_of=modules.get)


def resolutions_with(source, modules):
    """Exact per-read verdicts; no guarded-final-warning can mask wrong credit."""
    tree = ast.parse(source)
    resolver = V._Resolver(tree, "tests/test_probe.py", modules.get, source)
    reads = [node for node in ast.walk(tree)
             if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
             and isinstance(node.value, ast.Name) and node.value.id == "states"]
    return [(node.attr, resolver.constant(node)) for node in
            sorted(reads, key=lambda node: (node.lineno, node.col_offset))]


class ShapeTest(unittest.TestCase):
    def test_landable_queue_failure_fixture_with_only_empty_result_FIRES(self):
        """Instance one: the fixture never reached subprocess.run, so got=[]
        proved neither failure handling nor a call. The AST shape must warn."""
        source = '''
class T:
    def test_unreadable_repo(self):
        with patch("subprocess.run", side_effect=OSError("no git")):
            got = queue("kimi")
        self.assertEqual(got, [])
'''
        got = findings(source)
        self.assertEqual([x[2] for x in got], ["test_unreadable_repo"])
        self.assertIn("empty/absent", got[0][3])

    def test_guard_that_can_skip_the_only_positive_control_FIRES(self):
        """Instance two's decisive shape: an assertion under `if listy:` is
        not proof that any corpus entry supplied a list or that the arm ran."""
        source = '''
class T:
    def test_caller_copy(self):
        listy = next((k for k, v in first.items() if isinstance(v, list)), None)
        if listy:
            self.assertIn("poison-kw", second[listy])
'''
        got = findings(source)
        self.assertEqual([x[2] for x in got], ["test_caller_copy"])
        self.assertIn("behind if/loop/try", got[0][3])

    def test_guarded_negative_assertion_from_the_REAL_instance_FIRES(self):
        source = '''
class T:
    def test_caller_copy(self):
        listy = next((k for k, v in first.items() if isinstance(v, list)), None)
        if listy:
            first[listy].append("poison-kw")
        second = load_all()
        self.assertNotEqual(second.get("statement"), "POISONED")
        if listy:
            self.assertNotIn("poison-kw", second.get(listy) or [])
'''
        self.assertEqual([x[2] for x in findings(source)], ["test_caller_copy"])

    def test_empty_memo_arms_that_all_agree_FIRES(self):
        """Instance three: HIT/MISS/CORRUPT all returned empty findings because
        the fixture rooted the package incorrectly; equality-to-empty stayed green."""
        source = '''
class T:
    def test_memo_hit(self):
        first = census()
        second = census()
        self.assertEqual(first, second)
        self.assertFalse(first)
'''
        self.assertEqual([x[2] for x in findings(source)], ["test_memo_hit"])

    def test_try_except_pass_with_no_assertion_FIRES(self):
        source = '''
def test_does_not_raise():
    try:
        run_probe()
    except OSError:
        pass
'''
        got = findings(source)
        self.assertEqual(len(got), 1)
        self.assertIn("no unconditional structural assertion", got[0][3])

    def test_unconditional_positive_control_is_SILENT(self):  # noqa: VACUOUS_ASSERTION — detector silence arm
        source = '''
class T:
    def test_memo_hit(self):
        first = census()
        second = census()
        self.assertEqual(first, second)
        self.assertIn("probe_new", first)
'''
        self.assertEqual(findings(source), [])

    def test_plain_assert_truthy_and_nonempty_literal_are_positive(self):  # noqa: VACUOUS_ASSERTION — detector silence arm
        source = '''
def test_rows():
    rows = load()
    assert rows
    assert rows == ["one"]
'''
        self.assertEqual(findings(source), [])

    def test_zero_permitting_comparisons_do_not_clear_the_warning(self):
        source = '''
class T:
    def test_rows(self):
        self.assertGreaterEqual(len(rows), 0)
        self.assertIsNot(left, right)
'''
        self.assertEqual([x[2] for x in findings(source)], ["test_rows"])

    def test_is_not_none_still_permits_an_empty_value(self):
        source = '''
class T:
    def test_rows(self):
        self.assertIsNot(rows, None)
'''
        self.assertEqual([x[2] for x in findings(source)], ["test_rows"])

    def test_assertisnotnone_is_an_unconditional_positive_control(self):  # noqa: VACUOUS_ASSERTION — detector silence arm
        """The two-channel refusal, which is what most of the tree writes.

        `assertIsNone(got)` beside `assertIsNotNone(err)` is the contract the
        module docstring already reads through provenance one notch stronger,
        as `assertIn("required", why)`. The whitelist simply had no arm for
        the weaker spelling, so every one of them fell to the terminal default
        and read as a second ABSENCE instead of the cover for the first.
        """
        covered = '''
class T:
    def test_refusal(self):
        got, err = mark_verdict("x")
        self.assertIsNone(got)
        self.assertIsNotNone(err)
'''
        # POSITIVE CONTROL ON THE SAME FIXTURE, not a second one: strike the
        # single assertion under test and the rung DOES fire. Without this the
        # silence below would be satisfied just as well by a fixture the rung
        # cannot parse or collect at all.
        stripped = covered.replace("        self.assertIsNotNone(err)\n", "")
        self.assertEqual([x[2] for x in findings(stripped)], ["test_refusal"])
        self.assertEqual(findings(covered), [])

    def test_assertisnotnone_no_longer_manufactures_an_absence(self):  # noqa: VACUOUS_ASSERTION — detector silence arm
        """The half that is pure loss: the terminal default did not merely
        withhold credit, it recorded a CLAIM OF ABSENCE over a value the arm
        had just proved present, and that claim then demanded cover of its
        own. Here the real observable is already covered; the not-None line
        was the only thing left warning."""
        source = '''
class T:
    def test_fsync(self):
        row = add()
        synced = collect()
        self.assertIsNotNone(row)
        self.assertIn("ledger", synced)
'''
        # POSITIVE CONTROL ON THE SAME FIXTURE: flip that one line to a real
        # absence and the rung fires, so the silence is this reading of
        # not-None and not an arm skipped wholesale.
        absent = source.replace("assertIsNotNone(row)", "assertIsNone(row)")
        self.assertEqual([x[2] for x in findings(absent)], ["test_fsync"])
        self.assertEqual(findings(source), [])

    def test_assertisnotnone_covering_an_emptiness_claim_is_the_known_price(self):  # noqa: VACUOUS_ASSERTION — detector silence arm
        """THE COST OF THE ARM ABOVE, PINNED SO IT IS A DECISION.

        Not-None is not non-empty. Crediting it therefore lets
        `assertIsNotNone(rows)` cover `assertEqual(rows, [])` on the very same
        name — the founding shape of this whole rung — and that is the one
        thing the fix gives up. It was taken deliberately: a census of every
        arm the credit silenced across the test tree found none that leaned on
        the accident, while withholding the credit left 85 honest arms warned
        and pointed them at `# noqa`. A narrower rung that reads not-None as
        NEUTRAL keeps this catch, and was measured: it recovers only 20 of the
        85 and leaves the two-channel refusal above still warning.

        If this silence ever shelters a real defect, THIS is the arm to flip.
        """
        source = '''
class T:
    def test_rows(self):
        rows = load()
        self.assertIsNotNone(rows)
        self.assertEqual(rows, [])
'''
        # POSITIVE CONTROL ON THE SAME FIXTURE: drop the credited line and the
        # emptiness claim stands uncovered and fires.
        without = source.replace("        self.assertIsNotNone(rows)\n", "")
        self.assertEqual([x[2] for x in findings(without)], ["test_rows"])
        self.assertEqual(findings(source), [])

    def test_reason_bearing_comment_suppresses_intentional_absence(self):  # noqa: VACUOUS_ASSERTION — detector silence arm
        source = '''
class T:
    def test_refusal_writes_nothing(self):
        self.assertEqual(writes, [])  # noqa: VACUOUS_ASSERTION — product invariant
'''
        self.assertEqual(findings(source), [])

    def test_marker_in_a_docstring_or_string_grants_nothing(self):
        source = '''
class T:
    def test_refusal_writes_nothing(self):
        "# noqa: VACUOUS_ASSERTION — not a comment"
        note = "# noqa: VACUOUS_ASSERTION — also not a comment"
        self.assertEqual(writes, [])
'''
        self.assertEqual([x[2] for x in findings(source)],
                         ["test_refusal_writes_nothing"])

    def test_marker_without_a_reason_grants_nothing(self):
        source = '''
class T:
    def test_refusal_writes_nothing(self):
        # noqa: VACUOUS_ASSERTION
        self.assertEqual(writes, [])
'''
        self.assertEqual([x[2] for x in findings(source)],
                         ["test_refusal_writes_nothing"])

    def test_docstring_discussing_assertFalse_does_not_create_an_assertion(self):  # noqa: VACUOUS_ASSERTION — detector silence arm
        source = '''
def test_probe():
    """assertFalse and assertIsNone are discussed here."""
    rows = load()
    assert rows
'''
        self.assertEqual(findings(source), [])

    def test_all_three_historical_memo_arms_FIRE(self):
        """Faithful HIT/MISS/CORRUPT shapes from 86b11db. MISS asserted only
        the local spy list while discarding the production result."""
        source = '''
class T:
    def test_memo_hit(self):
        calls = []
        def spy():
            calls.append(1)
        with patch(spy):
            d1 = wiring.unwired_additions()
            d2 = wiring.unwired_additions()
        self.assertEqual(d1, d2)
        self.assertEqual(len(calls), 1)

    def test_memo_miss(self):
        wiring.unwired_additions()
        calls = []
        def spy():
            calls.append(1)
        with patch(spy):
            wiring.unwired_additions()
        self.assertEqual(calls, [1])

    def test_memo_corrupt(self):
        d1 = wiring.unwired_additions()
        corrupt_memo()
        d2 = wiring.unwired_additions()
        self.assertEqual(d1, d2)
'''
        self.assertEqual([x[2] for x in findings(source)],
                         ["test_memo_hit", "test_memo_miss",
                          "test_memo_corrupt"])

    def test_empty_permitting_in_and_threshold_comparisons_FIRE(self):
        source = '''
def test_membership():
    got = ""
    assert "" in got

def test_threshold():
    got = []
    assert len(got) > -1
'''
        self.assertEqual([x[2] for x in findings(source)],
                         ["test_membership", "test_threshold"])

    def test_zero_trip_comprehension_assertion_is_guarded(self):
        source = '''
class T:
    def test_items(self):
        [self.assertTrue(item) for item in rows]
'''
        got = findings(source)
        self.assertEqual([x[2] for x in got], ["test_items"])
        self.assertIn("behind if/loop/try", got[0][3])

    def test_nonempty_not_equal_and_threshold_controls_are_SILENT(self):  # noqa: VACUOUS_ASSERTION — detector silence arm
        source = '''
class T:
    def test_items(self):
        assert rows != []
        assert len(rows) >= 1
        self.assertGreaterEqual(len(rows), 1)
'''
        self.assertEqual(findings(source), [])

    def test_nested_escape_cannot_suppress_the_outer_test(self):
        source = '''
def test_outer():
    def helper():
        assert rows == []  # noqa: VACUOUS_ASSERTION — helper law only
    assert rows == []
'''
        self.assertEqual([x[2] for x in findings(source)], ["test_outer"])

    def test_local_test_named_function_is_not_a_collected_identity(self):  # noqa: VACUOUS_ASSERTION — detector silence arm
        source = '''
def test_outer():
    def test_inner():
        assert inner == []
    assert outer
'''
        self.assertEqual(findings(source), [])


class AliasingTest(unittest.TestCase):
    """Coverage follows straight-line bindings. Every silent arm here is a
    reduction of a REAL warned test from 2026-07-31 (1d00204/3793b47/3f5f2e8):
    the rung fired on 31 landed guard tests in one commit and its remediation
    text pointed at # noqa — an FP class that trains permanent suppression of
    exactly the tests the rung exists to watch."""

    def test_error_channel_of_an_unpacked_call_is_covered_by_its_value_channel(self):  # noqa: VACUOUS_ASSERTION — detector silence arm
        """tests/test_dispatches.py test_ungated_approve_refuses_...: the
        refusal proof lands on `why`, the absence claim on `out` — channels of
        ONE mark_verdict result."""
        source = '''
class T:
    def test_refusal(self):
        out, why = dispatches.mark_verdict(rid, tip, "looks good", "approve")
        self.assertIsNone(out)
        self.assertIn("verified gate:<token>", why)
'''
        self.assertEqual(findings(source), [])

    def test_value_err_convention_is_one_observable(self):  # noqa: VACUOUS_ASSERTION — detector silence arm
        """tests/test_landreq.py test_the_lifecycle_...: assertIsNone(err) is
        the success proof of the same landreq.get() that produced lr."""
        source = '''
class T:
    def test_lifecycle(self):
        lr, err = landreq.get(rid)
        self.assertIsNone(err, err)
        self.assertEqual(lr["state"], "READY")
'''
        self.assertEqual(findings(source), [])

    def test_before_after_ledger_guard_idiom_is_covered(self):  # noqa: VACUOUS_ASSERTION — detector silence arm
        """The unchanged-ledger guard: `before` reaches the module root through
        the with-binding chain (before -> f -> dispatches), where the
        unguarded status assertion meets it."""
        source = '''
class T:
    def test_refuses_without_changing_the_ledger(self):
        with open(dispatches.ledger_path(), "rb") as f:
            before = f.read()
        out, why = dispatches.mark_verdict(rid, tip, "looks good", "approve")
        self.assertIsNone(out)
        with open(dispatches.ledger_path(), "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(dispatches.snapshot()[0][rid]["status"], "open")
'''
        self.assertEqual(findings(source), [])

    def test_the_room_1071_rc_shape_is_silent(self):  # noqa: VACUOUS_ASSERTION — detector silence arm
        """codex ae7a5d6f HIGH-1: the ORIGINAL measured FP, verbatim shape —
        two unrelated calls sharing no names, so no aliasing can save it;
        only the scalar-zero neutrality does. My first fixture shared `rid`
        across the calls and went silent through argument overlap — it fit
        the mechanism, not the bug."""
        source = '''
class T:
    def test_cli(self):
        rc = run_the_thing()
        self.assertEqual(rc, 0)
        said = capture()
        self.assertIn("expected text", said)
'''
        self.assertEqual(findings(source), [])

    def test_a_lone_scalar_zero_pin_is_not_a_structural_assertion(self):
        """Neutrality must not make rc==0 sufficient on its own."""
        source = '''
class T:
    def test_cli(self):
        rc = run_the_thing()
        self.assertEqual(rc, 0)
'''
        got = findings(source)
        self.assertEqual([x[2] for x in got], ["test_cli"])
        self.assertIn("no unconditional structural assertion", got[0][3])

    def test_scalar_zero_neutrality_does_not_extend_past_bare_names(self):
        """len(rows) == 0 IS the emptiness idiom; a call or subscript against
        zero keeps absence semantics so neutrality cannot launder it."""
        source = '''
class T:
    def test_rows(self):
        self.assertEqual(len(rows), 0)
        self.assertTrue(other)
'''
        got = findings(source)
        self.assertEqual([x[2] for x in got], ["test_rows"])
        self.assertIn("empty/absent", got[0][3])

    def test_two_calls_to_one_function_are_two_observables(self):
        """Adversarial case (a): callee spelling is not provenance."""
        source = '''
class T:
    def test_two(self):
        got = load()
        other = load()
        self.assertTrue(other)
        self.assertEqual(got, [])
'''
        self.assertEqual([x[2] for x in findings(source)], ["test_two"])

    def test_a_shared_argument_is_not_shared_provenance(self):
        """Adversarial case (b): feeding two probes one seed does not make
        proof of the second a proof of the first."""
        source = '''
class T:
    def test_seeded(self):
        got = probe_a(seed)
        other = probe_b(seed)
        self.assertTrue(other)
        self.assertEqual(got, [])
'''
        self.assertEqual([x[2] for x in findings(source)], ["test_seeded"])

    def test_rebinding_kills_the_old_provenance(self):
        """Adversarial case (c): assignment replaces, never accretes —
        got's second binding must not stay covered through its first."""
        source = '''
class T:
    def test_rebound(self):
        got = first_probe()
        got = second_probe()
        other = first_probe()
        self.assertTrue(other)
        self.assertEqual(got, [])
'''
        self.assertEqual([x[2] for x in findings(source)], ["test_rebound"])

    def test_rebinding_one_channel_kills_its_tuple_provenance(self):
        """The shape where union-vs-replace actually diverges: after
        `out = other_probe()`, a proof on `why` must no longer cover `out`
        through the tuple's producer — accretion would keep them linked."""
        source = '''
class T:
    def test_channel_rebound(self):
        out, why = mint_row()
        out = other_probe()
        self.assertIn("ok", why)
        self.assertEqual(out, [])
'''
        self.assertEqual([x[2] for x in findings(source)],
                         ["test_channel_rebound"])

    def test_a_later_rebind_cannot_launder_an_earlier_empty_result(self):
        """codex round-3 FN repro, verbatim: the tuple rebind AFTER the
        absence assertion must not retroactively map the earlier `got` onto
        second_probe's producer, where `why` would launder it."""
        source = '''
class T:
    def test_temporal_fn(self):
        got = first_probe()
        self.assertEqual(got, [])
        got, why = second_probe()
        self.assertIn("ok", why)
'''
        got = findings(source)
        self.assertEqual([x[2] for x in got], ["test_temporal_fn"])
        self.assertIn("empty/absent", got[0][3])

    def test_a_later_rebind_cannot_erase_a_valid_tuple_relationship(self):  # noqa: VACUOUS_ASSERTION — detector silence arm
        """codex round-3 FP repro, verbatim: both assertions ran while
        got/why were channels of first_probe's result; the rebind afterwards
        must not un-cover what was covered when it ran."""
        source = '''
class T:
    def test_temporal_fp(self):
        got, why = first_probe()
        self.assertIsNone(got)
        self.assertIn("refused", why)
        got = second_probe()
'''
        self.assertEqual(findings(source), [])

    def test_a_same_name_rebind_cannot_launder_an_earlier_absence(self):
        """codex round-4 repro, verbatim: the two `got` epochs must not
        intersect at the spelling `got` — a bound name contributes its
        producers, never itself."""
        source = '''
class T:
    def test_same_name_fn(self):
        got = first_probe()
        self.assertEqual(got, [])
        got = second_probe()
        self.assertTrue(got)
'''
        got = findings(source)
        self.assertEqual([x[2] for x in got], ["test_same_name_fn"])
        self.assertIn("empty/absent", got[0][3])

    def test_an_earlier_same_name_positive_cannot_bless_a_later_absence(self):
        """codex round-4, reverse direction: proof of the first binding is
        not proof of the second."""
        source = '''
class T:
    def test_same_name_reverse(self):
        got = first_probe()
        self.assertTrue(got)
        got = second_probe()
        self.assertEqual(got, [])
'''
        got = findings(source)
        self.assertEqual([x[2] for x in got], ["test_same_name_reverse"])
        self.assertIn("empty/absent", got[0][3])

    def test_alias_capture_then_source_rebind_does_not_rewrite_history(self):
        """codex round-6 repro, verbatim: Python copied first_probe's value
        into `got` at assignment time; the analyzer must copy the provenance
        at the same moment, or source's rebind retroactively rewrites got."""
        source = '''
class T:
    def test_alias_capture(self):
        source = first_probe()
        got = source
        source = second_probe()
        self.assertTrue(source)
        self.assertEqual(got, [])
'''
        got = findings(source)
        self.assertEqual([x[2] for x in got], ["test_alias_capture"])
        self.assertIn("empty/absent", got[0][3])

    def test_two_with_as_of_one_callee_are_two_observables(self):
        """meld residual 1, verbatim: the context expression mints producer
        identity; a bare callee spelling is an operation's name, not data."""
        source = '''
class T:
    def test_two_contexts(self):
        with acquire() as got:
            pass
        with acquire() as other:
            pass
        self.assertTrue(other)
        self.assertEqual(got, [])
'''
        self.assertEqual([x[2] for x in findings(source)],
                         ["test_two_contexts"])

    def test_a_spy_alias_is_still_instrumentation(self):
        """meld residual 2, verbatim: alias = calls shares the spy list's
        binding epoch; identity comparison sees through the new spelling."""
        source = '''
class T:
    def test_memo_alias(self):
        calls = []
        alias = calls
        def spy():
            calls.append(1)
        with patch(spy):
            wiring.unwired_additions()
        self.assertEqual(alias, [1])
'''
        got = findings(source)
        self.assertEqual([x[2] for x in got], ["test_memo_alias"])
        self.assertIn("spy instrumentation", got[0][3])

    def test_a_guarded_rebind_cannot_launder_the_unconditional_binding(self):
        """meld residual 3, verbatim: when the branch is not taken, got still
        holds first_probe's empty result — a rotten green. Guarded bindings
        restore on exit, the same law that keeps guarded positives from
        clearing warnings."""
        source = '''
class T:
    def test_branch_rebind(self):
        got = first_probe()
        other = second_probe()
        if condition:
            got = other
        self.assertTrue(other)
        self.assertEqual(got, [])
'''
        self.assertEqual([x[2] for x in findings(source)],
                         ["test_branch_rebind"])

    def test_try_finally_bindings_persist_for_post_try_assertions(self):  # noqa: VACUOUS_ASSERTION — detector silence arm
        """try/finally with no except cannot skip its body: a binding either
        completed or the later assertion hits an unbound name and ERRORS —
        never a rotten pass. The corpus monkeypatch idiom depends on it."""
        source = '''
class T:
    def test_announce_failure(self):
        orig = chat.post
        chat.post = boom
        try:
            row = add_row()
            out, why = mint_verdict(row)
        finally:
            chat.post = orig
        self.assertIsNone(why)
        self.assertEqual(out["status"], "verdict")
'''
        self.assertEqual(findings(source), [])

    def test_a_handler_rebind_cannot_launder_the_unconditional_binding(self):
        """An except handler is a branch: when nothing raises, got still
        holds first_probe's empty result."""
        source = '''
class T:
    def test_handler_rebind(self):
        got = first_probe()
        other = second_probe()
        try:
            risky()
        except Exception:
            got = other
        self.assertTrue(other)
        self.assertEqual(got, [])
'''
        self.assertEqual([x[2] for x in findings(source)],
                         ["test_handler_rebind"])

    def test_a_body_rebind_behind_a_possible_raise_cannot_launder(self):
        """codex fresh-chain repro, verbatim: if risky() raises and the
        handler swallows, `got = other` never executed — "completed or
        unbound" holds only for FRESH names; a rebind cut short leaves the
        OLD binding."""
        source = '''
class T:
    def test_swallowed_prefix(self):
        got = first_probe()
        other = second_probe()
        try:
            risky()
            got = other
        except Exception:
            pass
        self.assertTrue(other)
        self.assertEqual(got, [])
'''
        self.assertEqual([x[2] for x in findings(source)],
                         ["test_swallowed_prefix"])

    def test_receiver_lineage_silence_is_a_documented_tradeoff(self):  # noqa: VACUOUS_ASSERTION — detector silence arm
        """KNOWN FALSE-NEGATIVE BOUNDARY, kept deliberately — room row 1148
        (the integrator's ruling): under strict same-observable semantics
        `got=client.fetch(); other=client.health(); assertTrue(other)` proves
        nothing about `got`, but the shared receiver makes it a plausible
        exercised-substrate signal, and this rung's measured failure mode is
        FP noise training permanent # noqa suppression (31 warns in one
        commit). Stay silent; revisit only if a real vacuous test ships
        through this exact hole."""
        source = '''
class T:
    def test_client(self):
        got = client.fetch()
        other = client.health()
        self.assertTrue(other)
        self.assertEqual(got, [])
'''
        self.assertEqual(findings(source), [])

    def test_receiver_lineage_flows_through_a_binding(self):  # noqa: VACUOUS_ASSERTION — detector silence arm
        """landreq.get()'s result IS landreq lineage: a later positive on the
        same module root covers the error channel even across statements."""
        source = '''
class T:
    def test_refusal(self):
        out, why = dispatches.mark_verdict(rid)
        self.assertIsNone(why)
        self.assertEqual(dispatches.snapshot()[0]["status"], "open")
'''
        self.assertEqual(findings(source), [])

    def test_channels_of_a_rootless_call_still_link_to_each_other(self):  # noqa: VACUOUS_ASSERTION — detector silence arm
        """`self` contributes no roots, so ONLY the sibling link can connect
        these channels — this is the arm that dies if the link is removed."""
        source = '''
class T:
    def test_helper_refusal(self):
        out, why = self.mint()
        self.assertIsNone(out)
        self.assertIn("refused", why)
'''
        self.assertEqual(findings(source), [])

    def test_an_unrelated_call_grants_no_coverage(self):
        """The boundary: aliasing is provenance, not proximity. A positive on
        probe_b's result says nothing about probe_a's."""
        source = '''
class T:
    def test_two_probes(self):
        got = probe_a()
        other = probe_b()
        self.assertTrue(other)
        self.assertEqual(got, [])
'''
        got = findings(source)
        self.assertEqual([x[2] for x in got], ["test_two_probes"])
        self.assertIn("empty/absent", got[0][3])

    def test_elementwise_pack_does_not_cross_link_its_names(self):
        """`a, b = x, y` binds each name to its own element; a positive on b
        must not launder an unproven a."""
        source = '''
class T:
    def test_pack(self):
        a, b = probe_x(), probe_y()
        self.assertTrue(b)
        self.assertEqual(a, [])
'''
        got = findings(source)
        self.assertEqual([x[2] for x in got], ["test_pack"])
        self.assertIn("empty/absent", got[0][3])

    def test_a_literal_bound_collector_still_counts_as_a_positive(self):  # noqa: VACUOUS_ASSERTION — detector silence arm
        """`out = []` has no producers but still denotes THIS binding: the
        epoch token keeps the collector's positive from vanishing into an
        empty snapshot and warning 'no structural assertion'."""
        source = '''
class T:
    def test_collector(self):
        out = []
        consume(out.append)
        self.assertEqual(out, [1])
'''
        self.assertEqual(findings(source), [])

    def test_spy_instrumentation_is_still_not_laundered_by_aliasing(self):
        """The raw-positive law: the spy check asks which assertions were
        WRITTEN. calls aliases nothing that reaches the discarded production
        result, so the miss arm keeps firing."""
        source = '''
class T:
    def test_memo_miss(self):
        calls = []
        def spy():
            calls.append(1)
        with patch(spy):
            wiring.unwired_additions()
        self.assertEqual(calls, [1])
'''
        got = findings(source)
        self.assertEqual([x[2] for x in got], ["test_memo_miss"])
        self.assertIn("spy instrumentation", got[0][3])


class StagedScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-vacuous-")
        self.root = os.path.join(self.tmp, "repo")
        os.makedirs(self.root)
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@example.com"),
                    ("git", "config", "user.name", "t")):
            self.assertEqual(self.sh(*cmd).returncode, 0)
        self.stage("README", "seed\n")
        self.assertEqual(self.sh("git", "commit", "-qm", "seed").returncode, 0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sh(self, *args):
        return subprocess.run(args, cwd=self.root, capture_output=True, text=True,
                              timeout=60)

    def stage(self, rel, content):
        """Clear bytecode before every same-path edit: a same-size, same-second
        source rewrite must never be hidden by a stale pyc in this test corpus."""
        shutil.rmtree(os.path.join(self.root, "tests", "__pycache__"),
                      ignore_errors=True)
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        r = self.sh("git", "add", "--", rel)
        self.assertEqual(r.returncode, 0, r.stderr)

    def scan(self):
        return V.scan(self.root)

    def test_scan_reads_the_index_not_the_worktree(self):
        rel = "tests/test_probe.py"
        staged = "def test_x():\n    assert rows == []\n"
        self.stage(rel, staged)
        with open(os.path.join(self.root, rel), "w") as f:
            f.write("def test_x():\n    assert rows\n")
        got, issues, paths, control = self.scan()
        self.assertEqual(issues, [])
        self.assertEqual(paths, [rel])
        self.assertTrue(control)
        self.assertEqual([x[2] for x in got], ["test_x"])

    def test_old_vacuous_function_outside_added_lines_is_not_reported(self):  # noqa: VACUOUS_ASSERTION — staged-range silence arm
        rel = "tests/test_probe.py"
        old = "def test_old():\n    assert rows == []\n"
        self.stage(rel, old)
        self.assertEqual(self.sh("git", "commit", "-qm", "old").returncode, 0)
        self.stage(rel, old + "\ndef test_new():\n    assert rows\n")
        got, issues, _paths, _control = self.scan()
        self.assertEqual(issues, [])
        self.assertEqual(got, [])

    def test_deleting_the_positive_control_rechecks_the_function(self):
        rel = "tests/test_probe.py"
        old = "def test_x():\n    assert rows == []\n    assert rows\n"
        self.stage(rel, old)
        self.assertEqual(self.sh("git", "commit", "-qm", "old").returncode, 0)
        self.stage(rel, "def test_x():\n    assert rows == []\n")
        got, _issues, _paths, _control = self.scan()
        self.assertEqual([x[2] for x in got], ["test_x"])

    def test_multiline_assertion_intersecting_an_added_line_is_found(self):
        rel = "tests/test_probe.py"
        self.stage(rel, "def test_x():\n    self.assertEqual(\n        rows,\n        [],\n    )\n")
        got, _issues, _paths, _control = self.scan()
        self.assertEqual([x[2] for x in got], ["test_x"])

    def test_syntax_error_is_UNKNOWN_not_a_clean_bill(self):
        self.stage("tests/test_broken.py", "def test_x(:\n    pass\n")
        got, issues, _paths, control = self.scan()
        self.assertEqual(got, [])
        self.assertTrue(control)
        self.assertEqual(len(issues), 1)
        self.assertIn("cannot be parsed", issues[0])

    def test_non_test_python_path_is_out_of_scope(self):
        self.stage("helm/test_probe.py", "def test_x():\n    assert rows == []\n")
        got, issues, paths, _control = self.scan()
        self.assertEqual((got, issues, paths), ([], [], []))


class MainTest(unittest.TestCase):
    def test_scan_failure_warns_and_never_blocks(self):
        script = os.path.join(os.path.dirname(__file__), "..", "helm",
                              "vacuous_assertion.py")
        r = subprocess.run([sys.executable, script, "--staged"], cwd="/tmp",
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("scan FAILED", r.stderr)


if __name__ == "__main__":
    unittest.main()


class ANoqaIsScopedToWhatItMarksTest(unittest.TestCase):
    """A definition-level exemption covers the method; an assertion-level one
    covers that assertion.

    They used to be the same thing: every assertion line was folded into the
    escape set, so a noqa on ONE deliberate absence silently exempted every
    other assertion in the method — including ones added later by someone who
    had no idea an exemption was in force. The narrower marking was not merely
    unsupported, it read as though it had worked, which is the worse failure.
    """

    UNMARKED = (
        "class T:\n"
        "    def test_two_absences(self):\n"
        "        rows = read()\n"
        "        other = read_other()\n"
        "        assert rows == []\n"
        "        assert other == []\n"
    )

    def test_an_unmarked_method_with_absences_FIRES(self):
        """THE CONTROL. Every arm below is about SUPPRESSION, so each one
        passes trivially if the detector never fires on this shape at all."""
        self.assertTrue(findings(self.UNMARKED),
                        "the detector does not fire on two bare absences, so "
                        "nothing below is measuring suppression")

    def test_a_noqa_on_the_DEFINITION_exempts_the_whole_method(self):
        # THE CONTROL FOR THIS METHOD'S OWN ABSENCE, not a neighbour's: the
        # emptiness below means nothing unless THIS source fires unmarked.
        self.assertTrue(findings(self.UNMARKED),
                        "the unmarked source does not fire, so the silence "
                        "below is not evidence the exemption did anything")
        source = self.UNMARKED.replace(
            "    def test_two_absences(self):",
            "    def test_two_absences(self):  # noqa: VACUOUS_ASSERTION — all")
        self.assertEqual(findings(source), [],
                         "a definition-level exemption no longer covers the "
                         "method it is written on")

    def test_a_noqa_on_ONE_assertion_leaves_the_OTHER_reported(self):
        """The whole point. Marking one absence must not buy silence for a
        second, unrelated one."""
        source = self.UNMARKED.replace(
            "        assert rows == []",
            "        assert rows == []  # noqa: VACUOUS_ASSERTION — deliberate")
        self.assertTrue(findings(source),
                        "marking ONE assertion suppressed the method, so the "
                        "unmarked absence beside it went unreported")

    def test_marking_EVERY_absence_is_silence_not_a_different_finding(self):
        """The trap in narrowing it: once each marked absence is dropped, the
        method can look like it has NO absence at all and be reported under a
        different rule — so a correct, explicit exemption would produce a new
        finding instead of silence."""
        self.assertTrue(findings(self.UNMARKED),
                        "the unmarked source does not fire, so the silence "
                        "below is not evidence the exemptions did anything")
        source = self.UNMARKED.replace(
            "        assert rows == []",
            "        assert rows == []  # noqa: VACUOUS_ASSERTION — one",
        ).replace(
            "        assert other == []",
            "        assert other == []  # noqa: VACUOUS_ASSERTION — two")
        self.assertEqual(findings(source), [],
                         "exempting every absence produced a finding under "
                         "another rule instead of silence")

class NamedConstantTest(unittest.TestCase):
    """A NAMED CONSTANT IS A LITERAL WITH A NAME ON IT.

    Comparing against `mod.FAILED` earned an arm no positive credit while the
    identical comparison against `"failed"` did, so a codebase that names its
    states could not write a provable arm at all — the only escapes were
    inlining the literal, which is the transcription defect, and a noqa,
    which is a claim the author does not believe.
    """

    SOURCE = '''
from helm import states


class T:
    def test_verdict(self):
        got = census(["a"])
        self.assertEqual(got["a"], states.%s)
'''

    def test_a_constant_that_resolves_NONEMPTY_is_a_positive_control(self):  # noqa: VACUOUS_ASSERTION — the analyzer returns ONE value per call, so an assertion that it found nothing can never carry a same-call control; the must-differ twin two lines above is a second call by necessity
        """THE MUST-DIFFER CONTROL RIDES THIS ARM, because the assertion is
        itself absence-shaped: an empty finding list is what this analyzer
        returns when it is working AND what it would return if it had stopped
        looking. The same source, the same module, one constant swapped."""
        module = {os.path.join("helm", "states.py"):
                  'FAILED = "failed"\nNOTHING = ""\n'}
        fires = findings_with(self.SOURCE % "NOTHING", module)
        self.assertEqual([x[2] for x in fires], ["test_verdict"])
        silent = findings_with(self.SOURCE % "FAILED", module)
        self.assertEqual(silent, [])

    def test_a_constant_that_resolves_EMPTY_still_FIRES(self):
        """The must-differ twin, and the reason node type cannot be the test:
        a named constant can resolve to nothing, and crediting the NAME would
        make this rung assert something it never measured."""
        got = findings_with(self.SOURCE % "NOTHING",
                            {os.path.join("helm", "states.py"):
                             'FAILED = "failed"\nNOTHING = ""\n'})
        self.assertEqual([x[2] for x in got], ["test_verdict"])
        self.assertIn("empty/absent", got[0][3])

    def test_a_constant_that_cannot_be_FOLLOWED_still_FIRES(self):
        """No module, no resolution, no credit. An unread definition is not
        evidence that the value is non-empty."""
        got = findings_with(self.SOURCE % "FAILED", {})
        self.assertEqual([x[2] for x in got], ["test_verdict"])

    def test_a_constant_bound_to_a_NON_LITERAL_still_FIRES(self):
        """`STATE = compute()` cannot be evaluated without running code, and
        this rung never runs the code it reads."""
        got = findings_with(self.SOURCE % "FAILED",
                            {os.path.join("helm", "states.py"):
                             "FAILED = compute()\n"})
        self.assertEqual([x[2] for x in got], ["test_verdict"])

    def test_an_expected_raise_is_an_event_and_not_an_absence(self):
        """assertRaises WAS LISTED AMONG THE ABSENCE FORMS, which made a
        raise-expectation UNCOVERABLE BY CONSTRUCTION. Its roots are the
        exception CLASS, and nothing ever asserts positively about
        `ValueError` — so no positive control in any method could intersect
        them, and a method whose only assertion was a raise-expectation could
        never be cleared by adding anything.

        The message it drew said the method had no unconditional positive
        control. The raise-expectation IS one: it asserts that something
        HAPPENED."""
        raise_only = ("import unittest\n\n\nclass T(unittest.TestCase):\n"
                      "    def probe(self, x):\n        raise ValueError(x)\n\n"
                      "    def test_probe(self):\n"
                      "        with self.assertRaises(ValueError):\n"
                      "            self.probe('none')\n")
        self.assertEqual(findings_with(raise_only, {}), [],
                         "a method whose only assertion is a raise-"
                         "expectation is reported as having no unconditional "
                         "positive control, and no edit can clear it")
        callable_form = ("import unittest\n\n\nclass T(unittest.TestCase):\n"
                         "    def probe(self, x):\n"
                         "        raise ValueError(x)\n\n"
                         "    def test_probe(self):\n"
                         "        self.assertRaises(ValueError, self.probe, 'x')\n")
        self.assertEqual(findings_with(callable_form, {}), [],
                         "the callable spelling of the same assertion is "
                         "still reported, so the cure reads one syntax rather "
                         "than the assertion")
        # MUST-MISS: a genuinely uncovered absence still fires. Without this
        # the two arms above pass against a rung that has stopped reporting.
        vacuous = ("import unittest\n\n\nclass T(unittest.TestCase):\n"
                   "    def test_probe(self):\n"
                   "        out = []\n"
                   "        self.assertNotIn('never', out)\n")
        self.assertEqual([x[2] for x in findings_with(vacuous, {})],
                         ["test_probe"],
                         "an absence with no positive control went silent, so "
                         "the cure bought its quiet by going blind")

    def test_a_raise_expectation_covers_an_absence_over_the_SAME_name(self):
        """THE ARGUABLE CONSEQUENCE, PINNED SO IT IS A DECISION RATHER THAN A
        SIDE EFFECT — and it is REAL, not hypothetical: the fixture below is
        clean, so the raise-expectation really does cover the absence.

        The roots must be NON-EMPTY or the positive does not register at all
        (`seen.positive` is a set and an empty contribution is falsy), so a
        raise-expectation necessarily contributes the exception's own name.
        For `assertRaises(ValueError)` that covers nothing real. For an
        exception reached THROUGH a module the roots are that module's, and
        they cover an absence over the same module elsewhere in the method.

        DEFENSIBLE, BECAUSE A METHOD THAT RAISED states.Refused DID EXERCISE
        states — the raise is evidence the module was reached, which is
        exactly what a positive control is for. But it is a WIDENING, and a
        widening nobody wrote down is the kind discovered later inside
        somebody's silent arm. This is the record.

        THE FIXTURE HAS TO REACH THE EXCEPTION THROUGH THE MODULE or it
        tests nothing: pairing assertRaises(ValueError) with an absence over
        `states` shares no root, so that fixture comes back clean for a
        reason with nothing to do with the claim. The control below is
        exactly that pairing, and it must FIRE."""
        src = ("import unittest\n\n\nclass T(unittest.TestCase):\n"
               "    def test_probe(self):\n"
               "        from helm import states\n"
               "        with self.assertRaises(states.Refused):\n"
               "            states.helper()\n"
               "        self.assertNotIn('x', states.NAMES)\n")
        module = {os.path.join("helm", "states.py"):
                  "class Refused(Exception):\n    pass\n\n\nNAMES = ()\n"
                  "\n\ndef helper():\n    raise Refused('x')\n"}
        self.assertEqual(findings_with(src, module), [],
                         "a raise-expectation naming states no longer covers "
                         "an absence over states; if that is the behaviour we "
                         "want, this arm is the place that says so")
        # THE CONTROL THAT MAKES THE ABOVE A CLAIM ABOUT SHARED ROOTS rather
        # than about raise-expectations in general: the SAME shape with a
        # builtin exception shares no root with the absence, and still fires.
        unrelated = src.replace("states.Refused", "ValueError")
        self.assertEqual([x[2] for x in findings_with(unrelated, module)],
                         ["test_probe"],
                         "an absence over states was covered by a raise "
                         "naming ValueError, so coverage is not keyed on the "
                         "shared name at all")

    def test_a_function_body_import_is_a_module_import_not_an_UNKNOWN(self):
        """THE TRI-STATE WAS COLLAPSED AT ITS OWN DOOR. `shadowed()` says
        True/False/None for a-different-binding/module-import/UNKNOWN, and
        `_roots` tested `is not False`, so UNKNOWN was handled as SHADOWED:
        the name left the roots AND pinned `_shadowed_reads`, which makes
        the method report even when every absence is covered.

        THE CASE IS THIS SUITE'S OWN CONVENTION — a module imported in the
        method body and used inside an assertion. The positive control is
        present, correct, and unconditional, and the method was told it had
        none."""
        src = ("import unittest\n\n\nclass T(unittest.TestCase):\n"
               "    def test_probe(self):\n"
               "        from helm import states\n"
               "        doc = 'hello world'\n"
               "        self.assertTrue(callable(states.helper), 'premise')\n"
               "        self.assertIn('hello', doc, 'positive')\n"
               "        self.assertNotIn('bye', doc, 'absence')\n")
        module = {os.path.join("helm", "states.py"): "def helper():\n    pass\n"}
        self.assertEqual(findings_with(src, module), [],
                         "a module imported in the method body and read "
                         "inside an assertion is treated as a shadowed name, "
                         "so the method reports despite a covered absence")
        # MUST-MISS ON THE SAME SHAPE: strip the positive control and it must
        # still fire, or the cure bought its silence by going blind.
        blind = src.replace(
            "        self.assertIn('hello', doc, 'positive')\n", "")
        self.assertEqual([x[2] for x in findings_with(blind, module)],
                         ["test_probe"],
                         "with its positive control removed the same method "
                         "stays silent, so the cure disabled the rung rather "
                         "than fixing its reading")

    def test_an_instance_attribute_is_an_observable_and_self_is_not(self):
        """THE SECOND MECHANISM UNDER THE SAME MESSAGE, found by hc4 and not
        covered by the tri-state cure above — proven by running hc4's case
        against that cure and watching it stay red.

        `_roots` skipped every Name spelled `self`, so `self.warned`
        contributed NO ROOTS AT ALL. An absence over it was uncovered BY
        CONSTRUCTION: no positive control could reach it, because the two
        assertions had nothing to share. The method held a correct
        unconditional positive on the very same attribute and was reported
        for having none.

        ONE HOP ONLY. `self.a.b` stays unrooted: a fixture can rebind `b`
        between the two assertions without either spelling changing, so the
        identity the method controls is the first attribute and no further.
        """
        src = ("import unittest\n\n\nclass T(unittest.TestCase):\n"
               "    def setUp(self):\n"
               "        self.warned = []\n\n"
               "    def test_probe(self):\n"
               "        self.warned.append(('k', 't'))\n"
               "        self.assertTrue(self.warned, 'positive')\n"
               "        self.warned[:] = []\n"
               "        self.assertEqual([], self.warned, 'absence')\n")
        self.assertEqual(findings_with(src, {}), [],
                         "an absence over self.<attr> is uncovered by "
                         "construction, so its unconditional positive "
                         "control on the same attribute is never credited")
        # MUST-MISS: the same shape with the positive control removed still
        # fires. Without this the arm above passes against a rung that has
        # stopped reading instance attributes altogether.
        blind = src.replace(
            "        self.assertTrue(self.warned, 'positive')\n", "")
        self.assertEqual([x[2] for x in findings_with(blind, {})],
                         ["test_probe"],
                         "an attribute absence with no positive control went "
                         "silent, so the cure traded a false positive for a "
                         "false negative")

    def test_a_LEXICALLY_SHADOWED_alias_resolves_nothing(self):  # noqa: VACUOUS_ASSERTION — the absence assertion's unconditional positive control is the SAME comparison without the shadow four lines above it, asserted to fire in this same method; a cure that refuses every alias reddens there
        """A refutation of the landed rung, verbatim. The binding map
        was built by walking the whole module, so `states = Fake()` after the
        import never overrode it — and `states.FAILED` still resolved to the
        REAL module's constant, crediting a comparison against a value the
        test had replaced. A re-bound name is not a module alias: the rung
        refuses the whole name rather than guess where the shadow takes
        effect. THE POSITIVE CONTROL IS THE SAME COMPARISON WITHOUT THE
        SHADOW: it must still earn its silence."""
        shadowed = self.SOURCE.replace(
            "class T:", "states = Fake()\n\n\nclass T:")
        module = {os.path.join("helm", "states.py"): 'FAILED = "failed"\n'}
        fires = findings_with(shadowed % "FAILED", module)
        self.assertEqual([x[2] for x in fires], ["test_verdict"],
                         "the alias read THROUGH the shadow into the real "
                         "module — codex-3's measured hole")
        silent = findings_with(self.SOURCE % "FAILED", module)
        self.assertEqual(silent, [], "the unshadowed alias must still "
                                     "resolve, or the cure refuses every "
                                     "named constant")

    def test_rebound_extraction_binds_only_STORE_names(self):  # noqa: VACUOUS_ASSERTION — every case pairs a must-refuse with a must-resolve on the same alias, so a cure that refuses every name reddens on the clean member
        """`cache = {}; cache[states.FAILED] = True` after the import made
        an honest fixture WARN, because the rebound walk counted every Name
        inside an assignment target — including the Load-context `states`
        read inside the subscript. A Name binds only in Store/Del context.
        The mechanism changed under the task/2124 redesign — symtable owns
        binding, a derived predicate owns module mutation — and the CASES
        did not: every must-refuse and must-resolve below is asked of the
        resolver's final answer at the module scope, so the arms survive
        the mechanism they were written against."""
        import ast
        from helm.vacuous_assertion import _Resolver

        def has(src):
            tree = ast.parse(src)
            r = _Resolver(tree, "tests/test_probe.py", None, src)
            if r.unknown:
                return None
            # Ask the module-scope question directly: is `states` still the
            # recorded alias, unshadowed, unmutated, un-reimported?
            if "states" not in r._aliases:
                return False
            if "states" in r._mutated or "states" in r._reimported:
                return False
            return r._shadowed_at("states", None, tree) is False

        refuses = {
            "del of the attribute":
                "from helm import states\ndel states.FAILED\n",
            "del of the name":
                "from helm import states\ndel states\n",
            "plain shadow": "from helm import states\nstates = Fake()\n",
            "re-import (unreachable)":
                "from helm import states\nif False:\n"
                "    from helm import states\n",
            "walrus": "from helm import states\nif (states := Fake()):\n"
                      "    pass\n",
            "for target":
                "from helm import states\nfor states in []:\n    pass\n",
            "with target":
                "from helm import states\nwith open('x') as states:\n"
                "    pass\n",
            "except name":
                "from helm import states\ntry:\n    pass\n"
                "except Exception as states:\n    pass\n",
            "class of the name":
                "from helm import states\nclass states:\n    pass\n",
            "def of the name":
                "from helm import states\ndef states():\n    pass\n",
            "tuple unpack":
                "from helm import states\nstates, other = 1, 2\n",
            "augmented assign": "from helm import states\nstates += 1\n",
        }
        for label, src in refuses.items():
            self.assertIs(has(src), False, "%s must refuse the alias — "
                          "None (UNKNOWN) satisfies assertFalse while "
                          "proving nothing" % label)
        resolves = {
            "clean alias":
                "from helm import states\nassert x == states.FAILED\n",
            "subscript READ inside a target (the regression)":
                "from helm import states\ncache = {}\n"
                "cache[states.FAILED] = True\n",
            "sibling def with the parameter (site scoping)":
                "from helm import states\ndef helper(states):\n"
                "    return states\nassert x == states.FAILED\n",
            "parameter in a def the module-level read never enters":
                "from helm import states\ndef f(states=Fake()):\n"
                "    pass\nassert x == states.FAILED\n",
        }
        for label, src in resolves.items():
            self.assertIs(has(src), True, "%s must still resolve — "
                          "None (UNKNOWN) satisfies assertFalse and must "
                          "redden this side too" % label)

    def test_final_findings_cover_the_attribute_store_and_lambda(self):  # noqa: VACUOUS_ASSERTION — each warned case pairs with the silent honest control below it through the SAME analyzer, so a detector that warns on everything reddens there
        """Alias-membership arms measure the map; these arms measure the
        VERDICT, because two holes lived past the map: `states.FAILED = ""`
        writes the module's own constant (an Attribute Store whose base Name
        carries Load), and a lambda parameter binds its name while the
        assertion visitor traverses the body. Both must WARN, and the honest
        subscript read plus the plain comparison must stay SILENT through the
        same analyzer call chain."""
        module = {os.path.join("helm", "states.py"): 'FAILED = "failed"\n'}

        def verdicts(body):
            src = ("from helm import states\nclass T:\n"
                   "    def test_verdict(self):\n" + body)
            return [x[2] for x in
                    findings_with(src, module)]

        warns = [
            "        states.FAILED = \"\"\n"
            "        self.assertEqual(got[\"a\"], states.FAILED)\n",
            "        (lambda states: self.assertEqual(\n"
            "            got[\"a\"], states.FAILED))(object())\n",
        ]
        for body in warns:
            self.assertEqual(verdicts(body), ["test_verdict"],
                             "the final finding must fire: %r" % body)
        silents = [
            "        cache = {}\n        cache[states.FAILED] = True\n"
            "        self.assertEqual(got[\"a\"], states.FAILED)\n",
            "        self.assertEqual(got[\"a\"], states.FAILED)\n",
        ]
        for body in silents:
            self.assertEqual(verdicts(body), [],
                             "the honest read must stay silent: %r" % body)

    def test_resolver_probes_from_the_design_record(self):  # noqa: VACUOUS_ASSERTION — each pair is must-warn against must-silent through the same analyzer, so a detector answering one way for everything reddens on the other member
        """The five controls the accepted design made a shipping condition,
        present as CONSTRUCTS, each asserting the final analyzer verdict.
        (1) Two lambdas on ONE line: the parameterised one warns, the bare
        one does not — the pairing bug this lane cured matched the sibling
        by line alone. (2) A lambda DEFAULT resolves in the enclosing scope
        while its BODY refuses. (3) A nested def's assertion refuses through
        the enclosing parameter. (4) A comprehension's first iterable
        resolves the outer alias while the iteration variable shadows the
        element. (5) A class-local assignment does not shadow the method's
        module read."""
        module = {os.path.join("helm", "states.py"):
                  'FAILED = "failed"\nALL = ["failed"]\n'}

        def verdicts(body):
            src = ("from helm import states\nclass T:\n"
                   "    def test_verdict(self):\n" + body)
            return [x[2] for x in findings_with(src, module)]

        # (1) same-line siblings: shadowed param warns, bare sibling resolves
        self.assertEqual(
            verdicts("        (lambda states: self.assertEqual(\n"
                     "            got[\"a\"], states.FAILED),\n"
                     "         lambda: self.assertEqual(\n"
                     "            got[\"a\"], states.FAILED))\n"),
            ["test_verdict"],
            "the shadowed same-line lambda must warn")
        self.assertEqual(
            verdicts("        (lambda: self.assertEqual(\n"
                     "            got[\"a\"], states.FAILED))\n"),
            [], "the bare same-line lambda must stay silent")
        # (2) default versus body
        self.assertEqual(
            verdicts("        (lambda states=states: states.FAILED)\n"
                     "        self.assertEqual(got[\"a\"], "
                     "states.FAILED)\n"),
            [], "a default read in the enclosing scope resolves")
        # (3) nested def closure
        self.assertEqual(
            verdicts("        def outer(states):\n"
                     "            self.assertEqual(got[\"a\"],\n"
                     "                states.FAILED)\n"),
            ["test_verdict"],
            "the closure read through an enclosing parameter must warn")
        # (4) comprehension iterable resolves, element shadows
        self.assertEqual(
            verdicts("        seen = [states.FAILED\n"
                     "                for states in states.ALL]\n"
                     "        self.assertEqual(got[\"a\"], "
                     "states.FAILED)\n"),
            [], "the iterable read of the outer alias must resolve")
        # (5) class-local does not shadow the method's global read
        src = ("from helm import states\nclass T:\n"
               "    states = Fake()\n"
               "    def test_verdict(self):\n"
               "        self.assertEqual(got[\"a\"], states.FAILED)\n")
        self.assertEqual([x[2] for x in findings_with(src, module)], [],
                         "a class-local must not shadow the method's read")

    def test_ambiguous_pairing_refuses_only_the_affected_sites(self):  # noqa: VACUOUS_ASSERTION — exact UNKNOWN sites are paired with a resolved site and an honest final verdict in this method
        module = {os.path.join("helm", "states.py"): 'FAILED = "failed"\n'}
        source = ('from helm import states\n'
                  'pair = (lambda states: states.FAILED, lambda: states.FAILED)\n')
        self.assertEqual(resolutions_with(source, module),
                         [("FAILED", (False, None)), ("FAILED", (False, None))])
        # Name + first line is not a unique key: default and outer lambdas
        # share it too, but the compiler lists the default first.
        hostile = self.SOURCE % "FAILED"
        hostile = hostile.replace(
            'self.assertEqual(got["a"], states.FAILED)',
            '(lambda states=(lambda: None): '
            'self.assertEqual(got["a"], states.FAILED))(Fake())')
        self.assertEqual(resolutions_with(hostile, module), [("FAILED", (False, None))])
        self.assertEqual([x[2] for x in findings_with(hostile, module)], ["test_verdict"])
        # An unrelated ambiguity must not poison a clean test in the file.
        honest = (self.SOURCE % "FAILED").replace(
            "class T:", "pair = (lambda states: states, lambda: None)\nclass T:")
        self.assertEqual(resolutions_with(honest, module), [("FAILED", (True, "failed"))])
        self.assertEqual(findings_with(honest, module), [])

    def test_defaults_and_closures_follow_evaluation_ownership(self):
        module = {os.path.join("helm", "states.py"): 'FAILED = "failed"\n'}
        cases = {
            "default outside, body inside": (
                "f = lambda states=states.FAILED: states.FAILED\n",
                [("FAILED", (True, "failed")), ("FAILED", (False, None))]),
            "closure created in a default": (
                "f = lambda states=(\n    lambda: states.FAILED\n)(): None\n",
                [("FAILED", (True, "failed"))]),
            "decorator outside the function": (
                "@(lambda f: states.FAILED)\ndef read(states):\n"
                "    return states.FAILED\n",
                [("FAILED", (True, "failed")), ("FAILED", (False, None))]),
            "unsupported annotation is not a guessed module read": (
                "def read(value: states.FAILED):\n    return states.FAILED\n",
                [("FAILED", (False, None)), ("FAILED", (True, "failed"))]),
            "captured parameter": (
                "def outer(states):\n    def inner():\n"
                "        return states.FAILED\n    return inner\n",
                [("FAILED", (False, None))]),
            "nonlocal parameter": (
                "def outer(states):\n    def inner():\n"
                "        nonlocal states\n        return states.FAILED\n"
                "    return inner\n", [("FAILED", (False, None))]),
            "global skips outer parameter": (
                "def outer(states):\n    def inner():\n"
                "        global states\n        return states.FAILED\n"
                "    return inner\n", [("FAILED", (True, "failed"))]),
            "unshadowed closure": (
                "def outer():\n    def inner():\n"
                "        return states.FAILED\n    return inner\n",
                [("FAILED", (True, "failed"))]),
            "method skips class local": (
                "class T:\n    states = Fake()\n    def read(self):\n"
                "        return states.FAILED\n", [("FAILED", (True, "failed"))]),
        }
        for label, (body, expected) in cases.items():
            with self.subTest(label=label):
                self.assertEqual(resolutions_with("from helm import states\n" + body,
                                                  module), expected)

    def test_comprehension_sites_do_not_borrow_the_iterable_binding(self):
        module = {os.path.join("helm", "states.py"):
                  'FAILED = "failed"\nALL = ["failed"]\n'}
        for left, right in (("[", "]"), ("{", "}"), ("(", ")")):
            source = ("from helm import states\nx = " + left +
                      "states.FAILED for states in states.ALL" + right + "\n")
            with self.subTest(delimiters=left + right):
                self.assertEqual(resolutions_with(source, module),
                                 [("FAILED", (False, None)),
                                  ("ALL", (True, ["failed"]))])
        source = ("from helm import states\nx = {states.FAILED: states.FAILED "
                  "for states in states.ALL}\n")
        self.assertEqual(resolutions_with(source, module),
                         [("FAILED", (False, None)), ("FAILED", (False, None)),
                          ("ALL", (True, ["failed"]))])
        # The compiler visits the iterable's lambda before the element's,
        # unlike ast.walk. A guarded final warning alone masks wrong pairing.
        source = ("from helm import states\nx = [(lambda states: states.FAILED)"
                  "(Fake()) for item in (lambda: [None])()]\n")
        self.assertEqual(resolutions_with(source, module), [("FAILED", (False, None))])
        control = ("from helm import states\nx = [(lambda: states.FAILED)() "
                   "for item in [None]]\n")
        self.assertEqual(resolutions_with(control, module), [("FAILED", (True, "failed"))])
        captured = ("from helm import states\nx = [(lambda: states.FAILED)() "
                    "for states in states.ALL]\n")
        self.assertEqual(resolutions_with(captured, module),
                         [("FAILED", (False, None)), ("ALL", (True, ["failed"]))])

    def test_global_writes_are_module_mutations_not_local_shadows(self):  # noqa: VACUOUS_ASSERTION — module writes warn; read-only global declarations and local assignments are same-method positive controls
        module = {os.path.join("helm", "states.py"): 'FAILED = "failed"\n'}
        source = self.SOURCE % "FAILED"
        assertion = 'self.assertEqual(got["a"], states.FAILED)'
        cases = {
            "global assignment": (
                "def change():\n    global states\n    states = Fake()\n", False),
            "global deletion": (
                "def change():\n    global states\n    del states\n", False),
            "global read only": (
                "def change():\n    global states\n    return states\n", True),
            "local assignment": (
                "def change(states=Fake()):\n    states = Fake()\n", True),
            "parameter may receive module": (
                "def change(states=states):\n    states.FAILED = ''\n", False),
            "module attribute write": (
                "def change():\n    states.FAILED = ''\n", False),
        }
        for label, (helper, resolves) in cases.items():
            fixture = source.replace("class T:", helper + "\nclass T:").replace(
                assertion, "change()\n        " + assertion)
            with self.subTest(label=label):
                # Mutation targets are excluded by resolutions_with: these
                # tuples measure the later read, not the write's node shape.
                expected = (True, "failed") if resolves else (False, None)
                self.assertEqual(resolutions_with(fixture, module), [("FAILED", expected)])
                self.assertEqual([x[2] for x in findings_with(fixture, module)],
                                 [] if resolves else ["test_verdict"])

    def test_default_closure_final_verdict_and_shadow_isolation(self):  # noqa: VACUOUS_ASSERTION — covered test alone versus after a shadow, plus a default-site resolution control
        module = {os.path.join("helm", "states.py"): 'FAILED="failed"\nALL=["x"]\n'}
        source = ('from helm import states\nclass T:\n'
                  '    def test_verdict(self):\n        got = census(["a"])\n'
                  '        (lambda states=(\n'
                  '            lambda: self.assertEqual(got["a"], states.FAILED)\n'
                  '        )(): None)()\n')
        self.assertEqual(resolutions_with(source, module), [("FAILED", (True, "failed"))])
        self.assertEqual(findings_with(source, module), [])
        clean = ('    def test_b(self):\n        got = load()\n'
                 '        self.assertEqual(got, [1])\n        self.assertNotIn(2, got)\n')
        shadow = ('    def test_a(self, states=Fake()):\n        got = load()\n'
                  '        self.assertEqual(got, states.FAILED)\n')
        prefix = "from helm import states\nclass T:\n"
        self.assertEqual(findings_with(prefix + clean, module), [])
        self.assertEqual([x[2] for x in findings_with(prefix + shadow + clean, module)],
                         ["test_a"], "shadow evidence must not escape its test")

    def test_dynamic_module_values_remain_unknown(self):
        source = self.SOURCE % "FAILED"
        for definition in ('FAILED = "failed"\n', ""):
            module = {os.path.join("helm", "states.py"):
                      definition + 'def __getattr__(name):\n    return ""\n'}
            for prefix in ("", "del states.FAILED\n"):
                fixture = source.replace("class T:", prefix + "class T:")
                with self.subTest(definition=definition, prefix=prefix):
                    self.assertEqual(resolutions_with(fixture, module),
                                     [("FAILED", (False, None))])
                    self.assertEqual([x[2] for x in findings_with(fixture, module)],
                                     ["test_verdict"])
        self.assertEqual(resolutions_with(source, {
            os.path.join("helm", "states.py"): 'FAILED = "failed"\n'}),
            [("FAILED", (True, "failed"))])

    def test_namedexpr_destination_survives_comprehension_inlining(self):  # noqa: VACUOUS_ASSERTION — module/global writes must warn while local/nonlocal writes preserve the honest sibling module read
        module = {os.path.join("helm", "states.py"): 'FAILED="failed"\n'}
        forms = {
            "ordinary": "(states := Fake())",
            "list": "[(states := Fake()) for item in [0]]",
            "set": "{(states := Fake()) for item in [0]}",
            "dict": "{item: (states := Fake()) for item in [0]}",
            "generator": "list((states := Fake()) for item in [0])",
            "nested": "[[(states := Fake()) for inner in [0]] for item in [0]]",
            "filter": "[item for item in [0] if (states := Fake())]",
            "lambda default in comprehension":
                "[lambda value=(states := Fake()): None for item in [0]]",
        }
        contexts = {
            "module": ("{expr}\n", False),
            "global helper": (
                "def change():\n    global states\n    {expr}\n", False),
            "local helper": ("def change():\n    {expr}\n", True),
            "parameter helper": ("def change(states):\n    {expr}\n", True),
            "nonlocal helper": (
                "def outer(states):\n    def change():\n"
                "        nonlocal states\n        {expr}\n", True),
            "nested global": (
                "def outer(states):\n    def change():\n"
                "        global states\n        {expr}\n", False),
            "module default": (
                "def change(states={expr}):\n    pass\n", False),
            "local default": (
                "def outer():\n    def change(states={expr}):\n        pass\n", True),
            "global default": (
                "def outer():\n    global states\n"
                "    def change(states={expr}):\n        pass\n", False),
            "nonlocal default": (
                "def outer(states):\n    def inner():\n        nonlocal states\n"
                "        def change(states={expr}):\n            pass\n", True),
            "lambda body": ("f = lambda: {expr}\n", True),
            "lambda default": ("f = lambda states={expr}: None\n", False),
        }
        cases = [(form + "/" + context, template.format(expr=expression), resolves)
                 for form, expression in forms.items()
                 for context, (template, resolves) in contexts.items()]
        cases += [
            ("isolated target", "[None for states in [None]]\n", True),
            ("ambiguous local lambdas",
             "pair = (lambda: (states := Fake()), lambda: None)\n", True),
            ("lambda body in comprehension",
             "[(lambda: (states := Fake()))() for item in [0]]\n", True),
            ("class local", "class C:\n    value = (states := Fake())\n", True),
            ("class global",
             "class C:\n    global states\n    value = (states := Fake())\n", False),
            ("default module, body local",
             "def change(value=(states := Fake())):\n    states = None\n", False),
            ("default local, body global",
             "def outer():\n    def change(value=(states := Fake())):\n"
             "        global states\n        return states\n", True),
            ("clean module", "", True),
        ]
        # The list/module case is the original false-credit reproduction:
        # PEP 709 can omit both a child table and root is_assigned evidence.
        for label, body, resolves in cases:
            source = ("from helm import states\n" + body +
                      "class T:\n    def test_verdict(self):\n        got = load()\n"
                      "        self.assertEqual(got, states.FAILED)\n")
            with self.subTest(label=label):
                expected = (True, "failed") if resolves else (False, None)
                self.assertEqual(resolutions_with(source, module), [("FAILED", expected)])
                self.assertEqual([x[2] for x in findings_with(source, module)],
                                 [] if resolves else ["test_verdict"])

    def test_namedexpr_local_reads_and_import_aliases_stay_distinct(self):
        module = {os.path.join("helm", "states.py"): 'FAILED="failed"\n'}
        for declaration in ("", "nonlocal states\n        "):
            source = ("from helm import states\ndef outer(states):\n"
                      "    def change():\n        " + declaration +
                      "[(states := Fake()) for item in [0]]\n"
                      "        return states.FAILED\n"
                      "class T:\n    def test_verdict(self):\n        got = load()\n"
                      "        self.assertEqual(got, states.FAILED)\n")
            with self.subTest(declaration=declaration):
                self.assertEqual(resolutions_with(source, module),
                                 [("FAILED", (False, None)), ("FAILED", (True, "failed"))])
                self.assertEqual(findings_with(source, module), [])
        for alias in ("states", "status_module"):
            for mutation in ("", "[(%s := Fake()) for item in [0]]\n" % alias):
                source = ("from helm import states as " + alias + "\n" + mutation +
                          "class T:\n    def test_verdict(self):\n        got = load()\n"
                          "        self.assertEqual(got, " + alias + ".FAILED)\n")
                tree = ast.parse(source)
                resolver = V._Resolver(tree, "tests/test_probe.py", module.get, source)
                read = next(node for node in ast.walk(tree)
                            if isinstance(node, ast.Attribute) and node.attr == "FAILED")
                with self.subTest(alias=alias, mutation=mutation):
                    self.assertEqual(resolver.constant(read),
                                     (False, None) if mutation else (True, "failed"))
                    self.assertEqual([x[2] for x in findings_with(source, module)],
                                     ["test_verdict"] if mutation else [])

    def test_scanner_is_standalone_without_an_importable_helm_package(self):
        with open(V.__file__, encoding="utf-8") as stream:
            source = stream.read()
        child = (
            'import json, sys; n={"__name__":"standalone_probe"}; '
            'exec(compile(sys.stdin.read(), "scanner", "exec"), n); '
            'f=n["_analyze_source"]; '
            'print(json.dumps([[r[2] for r in f(s, "tests/test_probe.py", [(1,99)])] '
            'for s in ("def test_x():\\n    assert run() == []\\n", '
            '"def test_x():\\n    assert run() == [1]\\n")]))')
        process = subprocess.run([sys.executable, "-I", "-c", child], input=source,
                                 text=True, capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), [["test_x"], []])

    def test_the_resolver_refuses_a_path_outside_the_repository(self):
        """The alias is attacker-adjacent input in the sense that matters:
        it names a file to open. A dotted name escaping the root is refused
        rather than read."""
        with tempfile.TemporaryDirectory() as root:
            read = V._source_reader(root)
            self.assertIsNone(read(os.path.join("..", "..", "etc", "passwd")))
            # POSITIVE CONTROL on the same reader: a path INSIDE the root is
            # read, so the None above is the refusal and not a dead function.
            inside = os.path.join(root, "helm")
            os.makedirs(inside)
            with open(os.path.join(inside, "states.py"), "w") as fh:
                fh.write("FAILED = 'failed'\n")
            self.assertIn("FAILED", read(os.path.join("helm", "states.py")))

