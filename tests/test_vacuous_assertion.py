#!/usr/bin/env python3
"""The staged-test vacuity advisory: three real rotten-green shapes, pinned."""
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
        """codex adversarial (a): callee spelling is not provenance."""
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
        """codex adversarial (b): feeding two probes one seed does not make
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
        """codex adversarial (c): assignment replaces, never accretes —
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
        (opus-integrator, 2026-08-01): under strict same-observable semantics
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
