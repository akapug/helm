#!/usr/bin/env python3
"""clarity tests — one NEGATIVE fixture per rule, plus the LIVE POSITIVE CONTROL.

THE CONTROL LAW (from the day three helm guards proved
vacuous with green tests throughout): a die that has never stamped a real part
is not verified, merely unrefuted. tests/fixtures/clarity-positive-control.txt
is a REAL historical helm commit message — commit 6d790248bbd771cb14e8e3fed1
f710efb4adea68 ("land: a commit message is a body too..."), body verbatim from
`git log --format=%B`, with only the Co-Authored-By / Claude-Session trailers
omitted. It is NOT synthetic. It hard-wraps at 72 columns, which is also why
it pins the wrap-aware sentence joiner: read per-line, its 33-word sentence
is two innocent halves and the length rule goes blind. When the control stops
firing, the linter is presumed broken — that test failing is the alarm, not
an inconvenience.

Every negative test asserts THE RULE ID in the findings, not a bare count or
exit code — the assert-the-effect law: except-pass + assert-no-raise is a
guaranteed vacuous pass.

Store fixtures are hermetic (HELM_HOME / HELM_ADOPTED_DIR at tempdirs, the
StoreBase pattern); no real ~/.helm is read or written. Lexicon terms used in
fixtures are synthetic on purpose (tests/ is tracked and helm goes public)."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-clarity-", var="HELM_HOME")

from helm import clarity  # noqa: E402
from helm.clarity import rules  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROL = os.path.join(ROOT, "tests", "fixtures",
                       "clarity-positive-control.txt")


def rule_ids(findings):
    return {f["rule"] for f in findings}


def check(text, mode="descriptive", lexicon=None):
    return clarity.check_text(text, mode=mode, lexicon=lexicon)


class VagueQuantifierTest(unittest.TestCase):
    """STE-AI-06, the rule this table was missing (#216 step 2).

    ASD-STE100 bans approximate quantity words where the number is knowable. It
    earns its place here from measured traffic rather than inheritance: a
    coordination surface saying "several rows are stale" withholds the two facts
    the reader needs — WHICH and HOW MANY — while still reading as a finding."""

    def _hits(self, text):
        return rules._check_vague_quantifier([[(1, text)]], {})

    def test_it_fires_on_the_words_that_hide_a_number(self):  # noqa: VACUOUS_ASSERTION — an UNCONDITIONAL assertTrue(self._hits("Several rows are stale.")) runs before the loop; the rung cannot see through the _hits helper to the checker.
        # UNCONDITIONAL: an emptied loop would assert nothing at all.
        self.assertTrue(self._hits("Several rows are stale."),
                        "the rule does not fire on its own headline case")
        for bad in ("Several rows are stale.",
                    "A number of lanes are open.",
                    "There are multiple stale receipts.",
                    "Most of the branches are dead."):
            self.assertTrue(self._hits(bad), "missed: %s" % bad)

    def test_an_exact_count_is_clean(self):  # noqa: VACUOUS_ASSERTION — an UNCONDITIONAL assertTrue(self._hits(...)) proves the checker is alive before the empty-result assertions; the rung cannot see through the _hits helper.
        """MUST-MISS: the cure the rule asks for must not itself violate it."""
        # POSITIVE CONTROL FIRST, same observable: the checker DOES return hits
        # for a vague sentence, so the empty results below mean "clean", not
        # "the checker is dead".
        self.assertTrue(self._hits("Several rows are stale."))
        for good in ("11 rows are stale.",
                     "97 branches exist and 11 were touched today.",
                     "Two lanes are open."):
            self.assertEqual(self._hits(good), [], "false positive: %s" % good)

    def test_word_boundaries_hold(self):  # noqa: VACUOUS_ASSERTION — same: an unconditional positive hit runs first, so an empty boundary result means CLEAN and not DEAD.
        """'somewhat' must not trip 'some of'; 'multiplexed' must not trip
        'multiple'. A banned-word list without boundaries is a nuisance rule
        that writers learn to ignore, which is worse than no rule."""
        self.assertTrue(self._hits("Several rows are stale."),
                        "checker is dead; the empty results below prove nothing")
        for edge in ("This is somewhat multiplexed.",
                     "The manyfold increase was measured."):
            self.assertEqual(self._hits(edge), [], "boundary leak: %s" % edge)

    def test_it_is_an_ERROR_not_an_advisory(self):
        """one-instruction is advisory because judging it needs a POS tagger.
        This is a closed word list on word boundaries — a writer can always fix
        it by counting, so it has no honest excuse to be soft."""
        row = next(r for r in rules.RULES if r["id"] == "vague-quantifier")
        self.assertEqual(row["severity"], "error")
        self.assertFalse(row["weak"])
        self.assertEqual(row["source"], "STE AI-06")


class RuleCountProseTest(unittest.TestCase):
    """THE HELP STRING MUST COUNT THE TABLE, NOT A MEMORY OF IT.

    helm/cli.py described "Seven STE-derived deterministic rules plus the two
    that are helm's own" while the table held SIX non-helm rules — a number
    typed by a human beside a structure that grows. Adding STE-AI-06 made the
    stale sentence accidentally true, which is exactly how a count like this
    survives review: it is wrong, then it is right again, and nobody re-derives
    it either time.

    So the prose is pinned to the DATA here. This test fails the moment a rule
    is added or removed without the sentence being updated, which is the only
    thing that makes the sentence trustworthy."""

    def test_the_cli_help_states_the_real_counts(self):
        from helm import cli
        from collections import Counter
        by = Counter(r["source"].split()[0] for r in rules.RULES)
        ste, skill, own, adapter = (by["STE"], by["skill"], by["helm"],
                                    by["helmese"])
        self.assertEqual(ste + skill + own + adapter, len(rules.RULES),
                         "a rule carries a source this test does not classify")
        help_text = cli.HELP["clarity"] if hasattr(cli, "HELP") else ""
        if not help_text:                      # locate the help map by shape
            for name in dir(cli):
                v = getattr(cli, name)
                if isinstance(v, dict) and "clarity" in v and isinstance(
                        v.get("clarity"), str) and "clarity check" in v["clarity"]:
                    help_text = v["clarity"]
                    break
        self.assertIn("clarity check", help_text,
                      "could not find the clarity help string to check")
        # UNCONDITIONAL: the table is non-empty and every rule carries a source
        # this test classifies. Without it, an empty RULES would make every
        # count zero and the assertions below could pass on a dead table.
        self.assertGreaterEqual(len(rules.RULES), 8, "the rule table is empty")
        self.assertTrue(ste and skill and own and adapter,
                        "a source bucket is empty")
        words = {11: "Eleven", 10: "Ten", 9: "Nine", 8: "Eight", 7: "Seven",
                 6: "Six", 5: "Five", 4: "Four", 3: "Three", 2: "two"}
        self.assertIn("%s deterministic rules" % words[len(rules.RULES)],
                      help_text,
                      "help says a different total than the table holds (%d)"
                      % len(rules.RULES))
        self.assertIn(("%s STE-derived" % words[ste]).lower(),
                      help_text.lower(),
                      "help misstates the STE-derived count (%d)" % ste)
        self.assertIn(("%s from the writing" % words[skill]).lower(),
                      help_text.lower(),
                      "help misstates the skill-sourced count (%d)" % skill)
        self.assertIn(("%s owner-mode" % words[adapter]).lower(),
                      help_text.lower(),
                      "help misstates the owner-mode count (%d)" % adapter)


class SentenceLengthTest(unittest.TestCase):
    LONG = ("The quick brown fox jumps over the lazy dog while the other "
            "dog watches from the porch and the cat ignores every single "
            "one of them completely.")  # 27 words, measured via rules.words

    def test_over_25_words_fires_in_descriptive_mode(self):
        got = check(self.LONG)
        self.assertIn("sentence-length", rule_ids(got))

    def test_21_words_fires_only_in_strict_mode(self):
        s = ("The seat proxy restarts cleanly whenever the config reload "
             "signal arrives before the very first request has finished "
             "its handshake dance.")  # 21 words, measured via rules.words
        self.assertNotIn("sentence-length", rule_ids(check(s)))
        self.assertIn("sentence-length", rule_ids(check(s, mode="strict")))

    def test_wrapped_long_sentence_still_fires(self):
        # THE POSITIVE CONTROL'S MECHANISM, pinned in miniature: hard-wrapped
        # at ~40 columns, the sentence must still count as ONE sentence.
        wrapped = ("The quick brown fox jumps over the\n"
                   "lazy dog while the other dog watches\n"
                   "from the porch and the cat ignores\n"
                   "every single one of them completely.")
        got = [f for f in check(wrapped) if f["rule"] == "sentence-length"]
        self.assertEqual(len(got), 1)
        self.assertIn("27 words", got[0]["message"])
        self.assertEqual(got[0]["line"], 1)

    def test_unknown_mode_refuses(self):
        with self.assertRaises(ValueError):
            check("Fine.", mode="lenient")


class MergedBoundaryTest(unittest.TestCase):
    """The refusal names the break the splitter could not see.

    `_SENT_SPLIT` needs a capital after the terminator, so a sentence opening
    with a lowercase word MERGES into its predecessor and sentence-length
    reports their SUM. helm's own nouns are lowercase, so this is common — and
    the quoted span starts mid-thought, which sends the author to shorten the
    wrong half. These arms pin the HINT, never a changed verdict.
    """

    # 12 + 11 words. Neither half breaks the 20-word strict cap; together
    # they do, which is the whole failure and the reason a hint is owed.
    MERGED = ("The pre-commit rung on the trunk enforces exactly the plain "
              "unqualified form. helm canon has wanted that same plain form "
              "since the very beginning.")

    def test_the_merged_span_is_still_one_sentence(self):
        # THE VERDICT DOES NOT MOVE. If this ever splits, the fix stopped
        # being diagnostic-only and owes the ruling shape (a) never got.
        self.assertEqual(len(rules.parse(self.MERGED)[0]), 1)

    def test_the_refusal_names_the_lowercase_word(self):
        got = [f for f in check(self.MERGED, mode="strict")
               if f["rule"] == "sentence-length"]
        self.assertEqual(len(got), 1)
        self.assertIn("COUNTED AS ONE SENTENCE", got[0]["message"])
        self.assertIn("'helm'", got[0]["message"])

    def test_a_genuinely_long_sentence_gets_no_hint(self):
        # A MUST-MISS. The hint must not appear on a sentence that really is
        # one, or it teaches authors to look for a boundary that is not there.
        got = [f for f in check(SentenceLengthTest.LONG)
               if f["rule"] == "sentence-length"]
        self.assertEqual(len(got), 1)
        self.assertNotIn("COUNTED AS ONE SENTENCE", got[0]["message"])

    def test_a_dotless_abbreviation_is_not_a_boundary(self):
        # _ABBREV DECIDES ONLY THE DOTLESS ONES, and this arm exists because
        # nothing else reaches it. `i.e.` and `e.g.` carry an internal dot, so
        # the dot rule blocks them first and their arms cannot tell whether
        # _ABBREV still works — delete the whole frozenset and they stay
        # green. `vs.` and `etc.` lose their trailing dot to rstrip and have
        # no internal one, so they are the only cases this clause decides.
        # POSITIVE CONTROL FIRST, same observable, same call.
        self.assertEqual(rules.merged_boundary(
            "Compare the rung, the export. helm canon wants it."), "helm")
        self.assertIsNone(rules.merged_boundary(
            "Compare the rung vs. the export before you decide."))
        self.assertIsNone(rules.merged_boundary(
            "Read the rows, etc. before the fold begins today."))

    def test_a_dotted_abbreviation_is_not_a_boundary(self):
        # Green via the internal-dot rule, NOT via _ABBREV — kept because it
        # is the shape authors actually write, and named so the next reader
        # does not mistake it for coverage of the frozenset.
        self.assertEqual(rules.merged_boundary(
            "Use the rung, the pre-commit check. helm lands it."), "helm")
        self.assertIsNone(rules.merged_boundary(
            "Use the rung, i.e. the pre-commit check, before you land."))
        self.assertIsNone(rules.merged_boundary(
            "Three seats, e.g. codex and kimi, read it before the fold."))

    def test_an_ellipsis_is_not_a_boundary(self):
        # POSITIVE CONTROL: one dot instead of three IS a boundary.
        self.assertEqual(rules.merged_boundary(
            "The pane trailed off. and then resumed a minute later."), "and")
        self.assertIsNone(rules.merged_boundary(
            "The pane trailed off... and then resumed a minute later."))

    def test_an_internal_dot_is_not_a_boundary(self):
        # kimi, reviewing 4a0eb1369993: "...in the U.S. today." named 'today'
        # and sent the author to a boundary that does not exist. An internal
        # dot means the terminator belongs to the TOKEN — U.S., a.m., Ph.D.,
        # a filename — so it is a rule, not more blocklist entries.
        # POSITIVE CONTROL on the same observable first.
        self.assertEqual(rules.merged_boundary(
            "The rung shipped in the US today. helm canon wants it."), "helm")
        self.assertIsNone(rules.merged_boundary(
            "It was adopted in the U.S. today."))
        self.assertIsNone(rules.merged_boundary(
            "The fold ran at 9 a.m. yesterday and finished."))

    def test_a_filename_ending_a_sentence_yields_no_hint(self):
        # DELIBERATELY CONSERVATIVE, and stated so it is not read as a bug:
        # a real boundary after a filename is MISSED rather than mis-named.
        # A wrong hint costs the author the round this rule exists to save;
        # a missing hint costs nothing beyond the status quo.
        self.assertEqual(rules.merged_boundary(
            "Read the rules module. helm canon wants it."), "helm")
        self.assertIsNone(rules.merged_boundary(
            "Read helm/clarity/rules.py. helm canon wants it."))

    def test_a_single_letter_is_not_a_boundary(self):
        # POSITIVE CONTROL: a two-letter left token IS a boundary, so the
        # arm below is about the LENGTH rule and not about the probe working.
        self.assertEqual(rules.merged_boundary(
            "The witness signed it ab. b countersigned the same page."), "b")
        self.assertIsNone(rules.merged_boundary(
            "The witness signed it a. b countersigned the same page."))

    def test_a_capitalised_opener_is_not_a_boundary(self):
        # The splitter ALREADY breaks here, so there is nothing to explain.
        # POSITIVE CONTROL: lowercase the SAME opener and the word comes back.
        self.assertEqual(rules.merged_boundary(
            "The rung enforces it. helm form."), "helm")
        self.assertIsNone(rules.merged_boundary(
            "The rung enforces it. Helm form."))

    def test_the_first_real_boundary_wins_over_an_abbreviation(self):
        # An abbreviation EARLIER in the span must not consume the answer.
        self.assertEqual(
            rules.merged_boundary(
                "Read the rung, i.e. the check. helm canon wants it."),
            "helm")


class ParagraphLengthTest(unittest.TestCase):
    def test_seven_sentences_fire(self):
        para = " ".join("Sentence number %d is here." % i for i in range(7))
        got = [f for f in check(para) if f["rule"] == "paragraph-length"]
        self.assertEqual(len(got), 1)
        self.assertIn("7 sentences", got[0]["message"])

    def test_six_sentences_pass(self):
        para = " ".join("Sentence number %d is here." % i for i in range(6))
        self.assertNotIn("paragraph-length", rule_ids(check(para)))

    def test_blank_line_resets_the_count(self):
        text = ("One is here. Two is here. Three is here. Four is here.\n\n"
                "Five is here. Six is here. Seven is here.")
        self.assertNotIn("paragraph-length", rule_ids(check(text)))


class SemicolonTest(unittest.TestCase):
    def test_semicolon_fires(self):
        got = check("The seat restarted; the proxy did not.")
        self.assertIn("no-semicolon", rule_ids(got))

    def test_semicolon_inside_code_does_not_fire(self):
        self.assertNotIn("no-semicolon",
                         rule_ids(check("Run `a; b` to see both.")))
        self.assertNotIn("no-semicolon",
                         rule_ids(check("```\na; b; c\n```\nProse here.")))


class ContractionTest(unittest.TestCase):
    def test_contractions_fire(self):
        for text in ("Don't restart the seat.", "It's already live.",
                     "The seat won't answer.", "We've landed it."):
            self.assertIn("no-contraction", rule_ids(check(text)), text)

    def test_possessive_is_not_a_contraction(self):
        # upstream's blanket \w+'s flagged every possessive in sight
        self.assertNotIn("no-contraction",
                         rule_ids(check("The seat's home directory moved.")))


class OneInstructionTest(unittest.TestCase):
    def test_chained_imperatives_fire_as_advisory_only(self):
        got = [f for f in check("Run the suite and post the verdict.")
               if f["rule"] == "one-instruction"]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["severity"], "advisory")
        self.assertIn("approximate", got[0]["message"])

    def test_advisory_never_counts_as_a_violation(self):
        findings = check("Run the suite and post the verdict.")
        self.assertEqual([f for f in findings if f["severity"] == "error"], [])

    def test_single_instruction_passes(self):
        self.assertNotIn("one-instruction",
                         rule_ids(check("Run the full suite now.")))

    def test_descriptive_and_clause_passes(self):
        # "and" joining non-imperatives is not a chained instruction
        self.assertNotIn("one-instruction",
                         rule_ids(check("Run logs show retries and errors.")))


class HedgeTermTest(unittest.TestCase):
    def test_hedge_fires_and_carries_the_weak_label(self):
        got = [f for f in check("This is essentially a seamless rollout.")
               if f["rule"] == "hedge-term"]
        self.assertEqual(len(got), 2)          # essentially + seamless
        self.assertTrue(all(f["weak"] for f in got))

    def test_hedge_phrase_fires(self):
        got = check("It is important to note that the seat restarted.")
        self.assertIn("hedge-term", rule_ids(got))


class LexiconDriftTest(unittest.TestCase):
    LEX = {"frob-gate": "the synthetic release gate used by these tests",
           "de-meld": "a synthetic compound term for the variant check"}

    def test_spacing_variant_fires(self):
        got = [f for f in check("The frob gate refused the land.",
                                lexicon=self.LEX)
               if f["rule"] == "lexicon-drift"]
        self.assertEqual(len(got), 1)
        self.assertIn("'frob-gate'", got[0]["message"])

    def test_fused_variant_fires(self):
        got = check("The frobgate refused it.", lexicon=self.LEX)
        self.assertIn("lexicon-drift", rule_ids(got))

    def test_canonical_spelling_passes(self):
        self.assertNotIn("lexicon-drift",
                         rule_ids(check("The frob-gate refused the land.",
                                        lexicon=self.LEX)))

    def test_variant_inside_a_longer_word_does_not_fire(self):
        # 'FrobGateKeeper' contains 'frobgate' but is a different identifier
        self.assertNotIn("lexicon-drift",
                         rule_ids(check("FrobGateKeeper handles it.",
                                        lexicon=self.LEX)))

    def test_synonym_row_fires_only_when_its_term_is_in_the_lexicon(self):
        self.assertIn("de-meld", rules.DRIFT_SYNONYMS)  # the seed row
        text = "We should unmeld the pair."
        self.assertIn("lexicon-drift", rule_ids(check(text, lexicon=self.LEX)))
        # same text, lexicon without the term: the row is INACTIVE — the
        # store stays the authority on which terms exist
        self.assertNotIn("lexicon-drift",
                         rule_ids(check(text,
                                        lexicon={"frob-gate": "gate"})))

    def test_no_lexicon_means_no_drift_findings(self):
        self.assertNotIn("lexicon-drift",
                         rule_ids(check("The frob gate refused.", lexicon={})))

    def test_exempt_common_english_variant_passes(self):
        # "already done" is an ordinary predicate; the coinage is the tag
        lex = {"already-done": "the first-pass ground-truth verdict tag"}
        self.assertNotIn("lexicon-drift",
                         rule_ids(check("That fix is already done.",
                                        lexicon=lex)))


class ProvenanceTest(unittest.TestCase):
    def test_quantitative_claim_without_tier_fires(self):
        got = [f for f in check("The relaunch took 45 seconds this time.")
               if f["rule"] == "provenance"]
        self.assertEqual(len(got), 1)
        self.assertIn("MEASURED / TRACED / INFERRED", got[0]["message"])

    def test_ratio_claim_fires(self):
        self.assertIn("provenance",
                      rule_ids(check("4 of 5 seats mismatched on resume.")))

    def test_verdict_claim_fires(self):
        self.assertIn("provenance",
                      rule_ids(check("All tests pass on the new branch.")))

    def test_tier_in_the_sentence_discharges(self):
        self.assertNotIn("provenance",
                         rule_ids(check("Measured: the relaunch took 45 "
                                        "seconds this time.")))

    def test_tier_earlier_in_the_paragraph_discharges(self):
        # the move's real usage: an opener's tier covers its paragraph
        text = ("MEASURED over the fleet logs. The relaunch took 45 seconds. "
                "9 of 12 seats resumed.")
        self.assertNotIn("provenance", rule_ids(check(text)))

    def test_tier_does_not_leak_across_paragraphs(self):
        text = "MEASURED: it took 45 seconds.\n\n9 of 12 seats resumed."
        got = [f for f in check(text) if f["rule"] == "provenance"]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["line"], 3)

    def test_spec_limits_are_not_claims(self):
        # "max 6 sentences" states a limit, it does not report a measurement
        self.assertNotIn("provenance",
                         rule_ids(check("Keep it to a max of 6 sentences.")))

    def test_question_is_not_a_claim(self):
        self.assertNotIn("provenance",
                         rule_ids(check("Did the relaunch take 45 seconds?")))


class CleanTextTest(unittest.TestCase):
    def test_conforming_text_yields_nothing(self):
        text = ("The seat restarted cleanly. TRACED: the config reload path "
                "rereads the auth directory.\n\nUse the short form.")
        self.assertEqual(check(text, lexicon={"frob-gate": "x"}), [])


class OneTableTest(unittest.TestCase):
    """Both consumers read RULES — the whole architecture in two asserts."""

    def test_every_rule_in_the_table_can_fire(self):  # noqa: VACUOUS_ASSERTION — asserts set EQUALITY against the whole non-empty RULES table; an empty result fails it.
        # a rule that cannot fire is dead weight vouching for nothing; this
        # kitchen-sink text must light up every row of the table
        sink = (
            "Don't touch the frob gate; it's essentially fine because the "
            "quick brown fox jumps over the lazy dog while every other dog "
            "watches from the porch today.\n"
            "Run the suite and post the verdict.\n\n"
            "One here. Two here. Three here. Four here. Five here. Six "
            "here. Seven here. The relaunch took 45 seconds.\n"
            "Several rows are stale.")
        got = rule_ids(check(sink, lexicon={"frob-gate": "x"}))
        # The owner-mode rules are GATED, not dead: they cannot fire on
        # agent-to-agent text by design, so the sink for them runs in owner
        # mode. The union must still cover the whole table.
        owner_sink = "Refactored the loop. The gate \u22a5 held it."
        got |= rule_ids(check(owner_sink, mode="owner"))
        self.assertEqual(got, {r["id"] for r in rules.RULES})

    def test_the_skill_renders_every_rule_and_the_hard_limit(self):
        text = "\n".join(clarity.render_skill({"frob-gate": "the gate"}))
        for r in rules.RULES:
            self.assertIn(r["id"], text)
            self.assertIn(r["guidance"], text)
        for missing in rules.NOT_CHECKABLE:
            self.assertIn(missing, text)
        self.assertIn("frob-gate", text)


class PositiveControlTest(unittest.TestCase):
    """The die has stamped a real part, continuously."""

    def test_control_fixture_is_the_real_commit(self):
        with open(CONTROL, encoding="utf-8") as f:
            raw = f.read()
        # the fixture's identity, pinned: subject line + a phrase that only
        # the real body carries (drift here means someone replaced the
        # control with a synthetic part)
        self.assertIn("a commit message is a body too", raw)
        self.assertIn("shared checkout that nine agents write to", raw)
        self.assertNotIn("Co-Authored-By", raw)   # trailers omitted, cited

    def test_control_still_fires(self):
        with open(CONTROL, encoding="utf-8") as f:
            findings = check(f.read())
        longs = [f for f in findings if f["rule"] == "sentence-length"]
        # the real part carries 26/28/31/33-word sentences; if this drops to
        # zero the LINTER is presumed broken, not the fixture improved
        self.assertGreaterEqual(len(longs), 3)
        self.assertTrue(any(f["severity"] == "error" for f in findings))


class LexiconStoreReadTest(unittest.TestCase):
    """lexicon_terms reads the CURATED roots and declares unavailability."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-clarity-store-")
        self.env_prior = {k: os.environ.get(k)
                          for k in ("HELM_HOME", "HELM_ADOPTED_DIR")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.adopted = os.path.join(self.tmp, "adopted")
        os.makedirs(self.adopted)
        os.environ["HELM_ADOPTED_DIR"] = self.adopted

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_lex(self, dirpath, term, definition):
        os.makedirs(dirpath, exist_ok=True)
        with open(os.path.join(dirpath, "lex-%s.md" % term.rstrip(":")),
                  "w", encoding="utf-8") as f:
            f.write("---\nname: lex-%s\ndescription: \"lexicon: %s\"\n"
                    "metadata:\n  node_type: memory\n  type: lexicon\n"
                    "  term: %s\n  scope: global\n  definition: %s\n---\n"
                    % (term, term, term, definition))

    def test_reads_curated_root_and_strips_trailing_colon(self):
        from helm import home
        self._write_lex(os.path.join(home.global_dir(), "lexicon"),
                        "frob-gate:", "the synthetic gate")
        terms, err = clarity.lexicon_terms()
        self.assertIsNone(err)
        self.assertIn("frob-gate", terms)

    def test_adopted_root_is_not_the_controlled_vocabulary(self):
        # ~180 raw adopted entries carry OTHER projects' vocabularies;
        # treating them as the curated dictionary would flag every post
        # about a neighbour project
        self._write_lex(self.adopted, "neighbour-term", "someone else's word")
        terms, err = clarity.lexicon_terms()
        self.assertIsNone(err)
        self.assertNotIn("neighbour-term", terms)

    def test_unavailable_is_declared_not_empty(self):
        from unittest import mock
        with mock.patch("helm.store.load_all",
                        side_effect=OSError("disk gone")):
            terms, err = clarity.lexicon_terms()
        self.assertEqual(terms, {})
        self.assertIn("disk gone", err)


class CliTest(unittest.TestCase):
    """The verb through a real child process: exit codes are the contract."""

    def _run(self, args, stdin=""):
        p = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r); from helm import cli; "
             "sys.exit(cli.main(sys.argv[1:]))" % ROOT] + list(args),
            input=stdin, capture_output=True, text=True, cwd=ROOT,
            timeout=60,
            env=dict(os.environ, HELM_NO_TREE_WARNING="1"))
        return p.returncode, p.stdout, p.stderr

    def test_violating_stdin_exits_1(self):
        rc, out, _ = self._run(["clarity", "check", "-", "--no-store"],
                               stdin="Don't do it; it's fine.\n")
        self.assertEqual(rc, 1)
        self.assertIn("no-semicolon", out)
        self.assertIn("no-contraction", out)

    def test_clean_stdin_exits_0_and_reports_the_words(self):
        rc, out, _ = self._run(["clarity", "check", "-", "--no-store"],
                               stdin="Use the short form.\n")
        self.assertEqual(rc, 0)
        self.assertIn("0 violations", out)

    def test_unknown_flag_refuses_with_2_before_reading_anything(self):
        rc, _, err = self._run(["clarity", "check", "--bogus"], stdin="x")
        self.assertEqual(rc, 2)
        self.assertIn("--bogus", err)

    def test_unknown_subverb_refuses_with_2(self):
        rc, _, err = self._run(["clarity", "cheack"], stdin="")
        self.assertEqual(rc, 2)
        self.assertIn("cheack", err)
        self.assertIn("check", err)          # the nearest-match hint

    def test_two_positionals_refuse(self):
        rc, _, err = self._run(["clarity", "check", "a.md", "b.md"])
        self.assertEqual(rc, 2)
        self.assertIn("one input", err)

    def test_missing_file_is_an_error_not_a_clean_pass(self):
        rc, _, err = self._run(
            ["clarity", "check", "no-such-file.md", "--no-store"])
        self.assertEqual(rc, 2)
        self.assertIn("no-such-file.md", err)

    def test_json_shape(self):
        rc, out, _ = self._run(
            ["clarity", "check", "-", "--json", "--no-store"],
            stdin="Don't.\n")
        self.assertEqual(rc, 1)
        d = json.loads(out)
        self.assertEqual(d["violations"], 1)
        self.assertEqual(d["findings"][0]["rule"], "no-contraction")
        self.assertIn("per_100_words", d)

    def test_strict_flag_reaches_the_engine(self):
        s = ("The seat proxy restarts cleanly whenever the config reload "
             "signal arrives before the very first request has finished "
             "its handshake dance.\n")      # 21 words, measured
        rc0, _, _ = self._run(["clarity", "check", "-", "--no-store"], stdin=s)
        rc1, out, _ = self._run(
            ["clarity", "check", "-", "--strict", "--no-store"], stdin=s)
        self.assertEqual((rc0, rc1), (0, 1))
        self.assertIn("sentence-length", out)

    def test_rules_subverb_prints_the_table(self):
        rc, out, _ = self._run(["clarity", "rules"])
        self.assertEqual(rc, 0)
        for r in rules.RULES:
            self.assertIn(r["id"], out)
        self.assertIn("not checkable", out)

    def test_skill_subverb_renders_offline(self):
        rc, out, _ = self._run(["clarity", "skill", "--no-store"])
        self.assertEqual(rc, 0)
        self.assertIn("write-time system", out)


if __name__ == "__main__":
    unittest.main()


class OwnerModeTest(unittest.TestCase):
    """The L3 adapter rules (helmese draft-2, amendment 6).

    WHY THESE ARE MODE-GATED and not simply added to the table: agents are
    MEANT to speak the register bare to each other — that is the entire point
    of having one. The owner never agreed to learn it. A rule that fired on
    coordination traffic would nag the fleet for doing the right thing, so the
    gate itself is load-bearing and is asserted in both directions below."""

    UNGLOSSED = "The seat is blocked. The gate ⊥ on a dirty tree."

    def test_the_owner_rules_do_NOT_fire_on_agent_to_agent_text(self):
        """The gate, in the direction that protects the fleet."""
        # MUST-HIT CONTROL: the rules are alive on this text in owner mode, so
        # their absence below is the GATE working and not a dead rule.
        live = {f["rule"] for f in rules.check_text(self.UNGLOSSED, mode="owner")}
        self.assertIn("gloss-once", live)
        for mode in ("descriptive", "strict"):
            hits = {f["rule"] for f in rules.check_text(self.UNGLOSSED, mode=mode)}
            self.assertNotIn("gloss-once", hits, mode)
            self.assertNotIn("mechanism-first", hits, mode)

    def test_the_owner_rules_DO_fire_in_owner_mode(self):
        """The other direction — without this the gate test above passes on a
        rule that never works at all."""
        hits = {f["rule"] for f in rules.check_text(self.UNGLOSSED, mode="owner")}
        self.assertIn("gloss-once", hits)

    def test_an_unglossed_register_symbol_is_flagged(self):
        f = [x for x in rules.check_text(self.UNGLOSSED, mode="owner")
             if x["rule"] == "gloss-once"]
        self.assertEqual(len(f), 1)
        self.assertIn("unglossed", f[0]["message"])
        # the suggestion carries the REGISTER's own plain form, not a guess
        self.assertIn("a hard stop", f[0]["message"])

    def test_glossing_ONCE_satisfies_it_and_the_bare_reuse_is_fine(self):
        """'Gloss once per artifact, then use it bare' — a rule that demanded
        a gloss per occurrence would be the per-sentence noise amendment 6
        explicitly banned."""
        from helm import helmese
        g = helmese.gloss("⊥")
        self.assertTrue(g, "the register lost its gloss for the test symbol")
        text = ("The seat is blocked. The gate ⊥ (%s) held it. "
                "The next gate ⊥ held too." % g)
        # MUST-HIT CONTROL: strip the one gloss and the SAME text fires, so a
        # clean result below means the gloss satisfied the rule.
        self.assertIn("gloss-once", {f["rule"] for f in rules.check_text(
            text.replace(" (%s)" % g, ""), mode="owner")})
        hits = {f["rule"] for f in rules.check_text(text, mode="owner")}
        self.assertNotIn("gloss-once", hits)

    def test_glossing_TWICE_is_flagged(self):
        """The second direction of the same rule. Presence-only would let
        'gloss every sentence' pass, which is the failure being prevented."""
        from helm import helmese
        g = helmese.gloss("⊥")
        text = "The gate ⊥ (%s) fired. The next gate ⊥ (%s) fired too." % (g, g)
        f = [x for x in rules.check_text(text, mode="owner")
             if x["rule"] == "gloss-once"]
        self.assertEqual(len(f), 1)
        self.assertIn("glossed 2 times", f[0]["message"])

    def test_TWO_glosses_in_ONE_sentence_are_counted(self):
        """Counted per OCCURRENCE, not per sentence. The first version searched
        each sentence once, so a doubly-glossed sentence read as clean."""
        from helm import helmese
        g = helmese.gloss("⊥")
        text = "The gate ⊥ (%s) fired and gate ⊥ (%s) fired too." % (g, g)
        f = [x for x in rules.check_text(text, mode="owner")
             if x["rule"] == "gloss-once"]
        self.assertEqual(len(f), 1)
        self.assertIn("glossed 2 times", f[0]["message"])

    def test_a_WRONG_gloss_is_caught_not_just_a_missing_one(self):
        """Shape-only matching accepted any parenthetical. A false expansion is
        worse than none: the owner reads it and believes it."""
        f = [x for x in rules.check_text("The gate ⊥ (banana) fired.",
                                         mode="owner")
             if x["rule"] == "gloss-once"]
        self.assertEqual(len(f), 1)
        self.assertIn("WRONG", f[0]["message"])

    def test_an_INVERTED_safety_gloss_is_caught(self):
        """The sharpest case of the same defect. '禁推main (push to main now)'
        is a prohibition rendered as permission, and it passed."""
        f = [x for x in rules.check_text("禁推main (push to main now) is the rule.",
                                         mode="owner")
             if x["rule"] == "gloss-once"]
        self.assertEqual(len(f), 1)
        self.assertIn("WRONG", f[0]["message"])
        self.assertIn("never push to main", f[0]["message"])

    def test_a_quote_SPANNING_sentences_is_still_exempt(self):
        """Masking used to happen per sentence, after segmentation, so a quote
        that crossed a full stop lost its exemption on the tail."""
        text = 'Evidence: "Gate. Next ⊥ review".'
        hits = {f["rule"] for f in rules.check_text(text, mode="owner")}
        self.assertNotIn("gloss-once", hits)
        # MUST-HIT CONTROL: unquoted, the same tail fires.
        self.assertIn("gloss-once", {f["rule"] for f in rules.check_text(
            "Evidence: Gate. Next ⊥ review.", mode="owner")})

    def test_EVERY_canonical_dual_form_passes_the_WHOLE_owner_table(self):
        """The register must not tell a reader to write text the die rejects.

        Two glosses carried a semicolon (no-semicolon) and one carried
        parentheses the gloss matcher would have truncated on. A controlled
        vocabulary whose own expansions fail the check is not controlled."""
        from helm import helmese
        forms = [("%s (%s) applies here." % (r["symbol"], r["gloss"]))
                 for r in helmese.OPERATORS]
        forms += [("%s (%s) applies here." % (r["dense"], r["plain"]))
                  for r in helmese.SAFETY]
        self.assertGreaterEqual(len(forms), 10, "the register is empty")
        # MUST-HIT CONTROL on the same observable: a DELIBERATELY broken dual
        # form does produce findings here, so the empty results below mean the
        # canonical forms are clean and not that the table stopped running.
        self.assertTrue(rules.check_text("⊥ (banana) applies here.",
                                         mode="owner"),
                        "the owner table returned nothing for a known-bad form")
        for form in forms:
            with self.subTest(form=form[:40]):
                self.assertEqual(rules.check_text(form, mode="owner"), [],
                                 "a canonical dual form fails the table")

    def test_quoted_evidence_is_EXEMPT(self):
        """facts-verbatim outranks the register (amendment 6). The author of a
        verdict quote is FORBIDDEN to rewrite it, so a die that demanded a
        gloss inside one would be demanding a prohibited edit."""
        text = ('The seat is blocked. The verdict said '
                '"the gate ⊥ on a dirty tree" verbatim.')
        # MUST-HIT CONTROL: the identical sentence UNQUOTED fires. Without it a
        # broken rule would look exactly like a working exemption.
        self.assertIn("gloss-once", {f["rule"] for f in rules.check_text(
            text.replace('"', ""), mode="owner")})
        hits = {f["rule"] for f in rules.check_text(text, mode="owner")}
        self.assertNotIn("gloss-once", hits)

    def test_a_MARKDOWN_BLOCKQUOTE_is_exempt(self):
        """Ordinary users call a blockquote 'quoted evidence' and the rule said
        quoted evidence is exempt. It was not."""
        quoted = "The seat is blocked.\n\n> the gate ⊥ on a dirty tree\n"
        hits = {f["rule"] for f in rules.check_text(quoted, mode="owner")}
        self.assertNotIn("gloss-once", hits)
        # MUST-HIT CONTROL: the identical line without the marker fires.
        self.assertIn("gloss-once", {f["rule"] for f in rules.check_text(
            "The seat is blocked.\n\nthe gate ⊥ on a dirty tree\n",
            mode="owner")})

    def test_an_ENTIRELY_quoted_artifact_is_still_exempt(self):
        """The masked document is selected BY KEY, never by truthiness.

        A wholly-quoted artifact masks to an empty document, and `masked or
        raw` read that emptiness as "no masked document" and fell back to the
        RAW text — so the exemption vanished in exactly the case where
        everything was quoted. It hid because the first blockquote test
        prepended an unquoted sentence, which made the masked doc truthy: the
        test was shaped so the bug could not appear."""
        for quoted in ("> the gate ⊥ on a dirty tree",
                       '"the gate ⊥ on a dirty tree"',
                       "`the gate ⊥ on a dirty tree`",
                       "> Gate held.\n> Next ⊥ review."):
            with self.subTest(quoted=quoted[:28]):
                hits = {f["rule"] for f in rules.check_text(quoted, mode="owner")}
                self.assertNotIn("gloss-once", hits)
        # MUST-HIT CONTROL: the same line with NO quoting fires, so the clean
        # results above are the exemption and not a rule that stopped running.
        self.assertIn("gloss-once", {f["rule"] for f in rules.check_text(
            "the gate ⊥ on a dirty tree", mode="owner")})
        # ...and an unquoted symbol beside a quoted line still fires.
        self.assertIn("gloss-once", {f["rule"] for f in rules.check_text(
            "> quoted line\nthe gate ⊥ fired", mode="owner")})

    def test_an_APOSTROPHE_does_not_open_a_quoted_span(self):
        """Straight single quotes are deliberately NOT exempt. Treating them as
        quotes would let one apostrophe swallow the rest of a line and exempt
        text nobody quoted — a false EXEMPTION hides the rule instead of
        firing it, which is the dangerous direction."""
        hits = {f["rule"] for f in rules.check_text(
            "The gate's ⊥ fired here.", mode="owner")}
        self.assertIn("gloss-once", hits)

    def test_a_greater_than_MID_LINE_is_not_a_blockquote(self):
        hits = {f["rule"] for f in rules.check_text(
            "5 > 3 and the gate ⊥ fired.", mode="owner")}
        self.assertIn("gloss-once", hits)

    def test_a_BARE_use_before_the_gloss_is_flagged(self):
        """The rule is "gloss once, THEN bare". A reader who meets the symbol
        undefined is not helped by an expansion further down the card."""
        from helm import helmese
        g = helmese.gloss("⊥")
        f = [x for x in rules.check_text(
            "First gate ⊥ fired. Later gate ⊥ (%s) fired." % g, mode="owner")
            if x["rule"] == "gloss-once"]
        self.assertEqual(len(f), 1)
        self.assertIn("bare BEFORE", f[0]["message"])
        # MUST-MISS CONTROL: the same two sentences in the RIGHT order pass.
        self.assertNotIn("gloss-once", {x["rule"] for x in rules.check_text(
            "First gate ⊥ (%s) fired. Later gate ⊥ fired." % g, mode="owner")})

    def test_MULTI_backtick_code_spans_are_exempt(self):
        """Markdown delimits a code span with a RUN of backticks. Matching only
        single ones left the content of every wider span unmasked."""
        for span in ("``gate ⊥ fired``", "```gate ⊥ fired```",
                     "`gate ⊥ fired`"):
            with self.subTest(span=span):
                self.assertNotIn("gloss-once", {f["rule"] for f in
                                                rules.check_text(span, mode="owner")})
        # MUST-HIT CONTROL: strip the delimiters and it fires.
        self.assertIn("gloss-once", {f["rule"] for f in rules.check_text(
            "gate ⊥ fired", mode="owner")})

    def test_a_dense_safety_entry_is_bounded_on_BOTH_sides(self):
        """The boundary follows the ENTRY KIND, not the token's edge characters.

        Deriving it from the characters was asymmetric in a way one example
        hid: 禁推main ends in ASCII so it gained a trailing guard and
        禁推mainland was fixed, but it BEGINS non-ASCII so x禁推main still
        matched, and 禁改史 is non-ASCII at both ends so it was guarded at
        neither. A safety entry is a word; an operator is a symbol; the
        register knows which, and the edge character was a bad proxy for it."""
        for text in ("x禁推main is odd.", "禁改史x is odd.", "x禁改史y is odd.",
                     "禁推mainland is a place."):
            with self.subTest(text=text):
                self.assertNotIn("gloss-once", {f["rule"] for f in
                                                rules.check_text(text, mode="owner")})
        # MUST-HIT CONTROLS: the real tokens, standing alone, still fire.
        for text in ("禁推main is the rule.", "禁改史 is the rule."):
            with self.subTest(text=text):
                self.assertIn("gloss-once", {f["rule"] for f in
                                             rules.check_text(text, mode="owner")})
        # ...and a symbolic operator stays UNBOUNDED, pressed against text.
        self.assertIn("gloss-once", {f["rule"] for f in rules.check_text(
            "the gate⊥fired", mode="owner")})

    def test_the_safety_dual_form_glosses_from_the_register(self):
        f = [x for x in rules.check_text("禁推main is the rule.", mode="owner")
             if x["rule"] == "gloss-once"]
        self.assertEqual(len(f), 1)
        self.assertIn("never push to main", f[0]["message"])

    def test_mechanism_first_is_ADVISORY_and_judges_only_the_opener(self):
        got = [f for f in rules.check_text(
            "Refactored the seat loop. Landed the fix.", mode="owner")
            if f["rule"] == "mechanism-first"]
        self.assertEqual(len(got), 1, "only the FIRST sentence is judged")
        self.assertEqual(got[0]["severity"], "advisory")

    def test_the_fleets_own_LANDED_phrasing_is_not_a_mechanism(self):
        """A land IS the outcome here. The board exists to announce lands and
        carries 182 such rows, so "Landed X @ sha" is the news rather than the
        mechanism behind it.

        Found by DOGFOODING the advisory through `helm note set` on real fleet
        text, not by review: seventeen review findings did not surface it,
        because it only appears when the rule meets the sentence the fleet
        actually writes most often. Flagging that would have made this rule
        noise on its first day, and a noisy advisory is switched off before
        anyone reads its true findings."""
        for real in ("Landed row-1 @ cfc59b57 — 4 lands tonight, go to board",
                     "Merged the lane into trunk.",
                     "Committed the fix and released the lease."):
            with self.subTest(real=real[:30]):
                self.assertNotIn("mechanism-first", {f["rule"] for f in
                                                     rules.check_text(real, mode="owner")})
        # MUST-HIT CONTROL: a genuine mechanism opener still fires, so the
        # clean results above are a curated exclusion and not a dead rule.
        self.assertIn("mechanism-first", {f["rule"] for f in rules.check_text(
            "Refactored the census loop.", mode="owner")})

    def test_an_outcome_first_opener_passes(self):
        # MUST-HIT CONTROL: swap the opener for a real mechanism verb and it
        # fires. "Landed" is deliberately NOT one here — see NOT_MECHANISM.
        self.assertIn("mechanism-first", {f["rule"] for f in rules.check_text(
            "Rewrote the seat loop. The seat is unblocked.", mode="owner")})
        hits = {f["rule"] for f in rules.check_text(
            "The seat is unblocked. Rewrote the seat loop.", mode="owner")}
        self.assertNotIn("mechanism-first", hits)

    def test_owner_mode_carries_the_strict_sentence_cap(self):
        self.assertGreater(rules.SENTENCE_MAX["owner"], 0, "the cap is unset")
        self.assertEqual(rules.SENTENCE_MAX["owner"], rules.SENTENCE_MAX["strict"])

    def test_an_unknown_mode_still_refuses_and_names_the_real_modes(self):
        self.assertTrue(rules.SENTENCE_MAX, "no modes: the loop asserts nothing")
        with self.assertRaises(ValueError) as cm:
            rules.check_text("x", mode="nonsense")
        # MUST-HIT CONTROL on the observable the loop reads: the message is
        # non-empty and names the bad mode, so an empty string cannot satisfy
        # the membership assertions below by being searched vacuously.
        self.assertIn("nonsense", str(cm.exception))
        for mode in rules.SENTENCE_MAX:
            self.assertIn(mode, str(cm.exception))


class OwnerAdvisoryTest(unittest.TestCase):
    """`clarity.advise` — the tap the four owner-bound verbs call.

    Its laws are the ones that keep a nudge from becoming noise, and each is
    asserted rather than trusted: advisory-only, fail-open, per-surface
    silenceable, budgeted."""

    UNGLOSSED = "The gate ⊥ on a dirty tree."

    def setUp(self):
        self.buf = io.StringIO()
        self._prior = os.environ.pop(clarity.ADVISE_OFF_ENV, None)

    def tearDown(self):
        os.environ.pop(clarity.ADVISE_OFF_ENV, None)
        if self._prior is not None:
            os.environ[clarity.ADVISE_OFF_ENV] = self._prior

    def test_it_emits_on_owner_bound_text(self):
        got = clarity.advise(self.UNGLOSSED, "note", stream=self.buf)
        self.assertEqual(len(got), 1)
        self.assertIn("[helm clarity/note]", got[0])

    def test_it_is_silent_on_clean_text(self):
        # MUST-HIT CONTROL: the advisory is armed in this process.
        self.assertTrue(clarity.advise(self.UNGLOSSED, "note", stream=self.buf))
        self.assertEqual(
            clarity.advise("The seat is blocked.", "note", stream=self.buf), [])

    def test_one_surface_silences_ONLY_itself(self):
        """A silencer that quietly turned off every surface would look
        identical to a working one from the surface being tested."""
        os.environ[clarity.ADVISE_OFF_ENV] = "note"
        self.assertEqual(clarity.advise(self.UNGLOSSED, "note", stream=self.buf), [])
        self.assertTrue(clarity.advise(self.UNGLOSSED, "board", stream=self.buf))

    def test_all_silences_every_surface(self):
        # MUST-HIT CONTROL, unconditional and on the same observable: this
        # surface emits BEFORE the switch is thrown.
        self.assertTrue(clarity.advise(self.UNGLOSSED, "note", stream=self.buf))
        os.environ[clarity.ADVISE_OFF_ENV] = "all"
        for surface in ("note", "board", "asks", "chat"):
            self.assertEqual(
                clarity.advise(self.UNGLOSSED, surface, stream=self.buf), [], surface)

    def test_it_FAILS_OPEN_on_anything(self):
        """An advisory that can raise at a write boundary is worse than no
        advisory: it would break `helm note set`."""
        # MUST-HIT CONTROL: good text still emits, so the silences below are
        # the fail-open path and not an advisory that never runs.
        self.assertTrue(clarity.advise(self.UNGLOSSED, "note", stream=self.buf))
        for bad in (None, "", "   ", 42, object()):
            self.assertEqual(clarity.advise(bad, "note", stream=self.buf), [])

    def test_the_output_is_BUDGETED(self):
        """A wall of findings is a wall the reader learns to skip."""
        text = "\n\n".join("The gate %s on a dirty tree." % s
                           for s in ("⊥", "→", "⊳", "·", ">"))
        got = clarity.advise(text, "note", stream=self.buf)
        self.assertLessEqual(len(got), clarity.ADVISE_MAX + 1)
        self.assertIn("more", got[-1])

    def test_every_owner_only_rule_is_named_in_ONE_place(self):
        """OWNER_ONLY is what check_text gates on AND what the renders label.
        Two copies of this fact would drift."""
        self.assertTrue(rules.OWNER_ONLY)
        ids = {r["id"] for r in rules.RULES}
        for rid in rules.OWNER_ONLY:
            self.assertIn(rid, ids, "%s is gated but not in the table" % rid)


class OwnerBoundCallSitesTest(unittest.TestCase):
    """The four verbs that write text the OWNER reads call the advisory.

    This asserts the WIRING, which is the part a unit test of advise() cannot
    reach: a perfect advisory nobody calls is worth nothing."""

    SURFACES = {"helm/fleetnotes.py": "note", "helm/board.py": "board",
                "helm/ownerasks.py": "asks", "helm/chat.py": "chat"}

    def test_each_owner_bound_module_wires_the_advisory(self):
        self.assertEqual(len(self.SURFACES), 4,
                         "the four owner-bound call-sites are the claim")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # MUST-HIT CONTROL on the same observable the loop reads: this file
        # loads and contains the call. A read that silently returned "" would
        # otherwise make every assertion in the loop unreachable-but-passing.
        with open(os.path.join(root, "helm/fleetnotes.py"),
                  encoding="utf-8") as fh:
            probe = fh.read()
        self.assertIn("advise(", probe, "the control file lost its call-site")
        for rel, surface in self.SURFACES.items():
            with open(os.path.join(root, rel), encoding="utf-8") as fh:
                src = fh.read()
            self.assertGreater(len(src), 500, "%s did not load" % rel)
            self.assertIn('advise(', src, "%s never calls the advisory" % rel)
            self.assertIn('"%s"' % surface, src,
                          "%s does not name its surface" % rel)

    def test_the_chat_tap_is_GATED_on_addressing_the_owner(self):
        """Chat is the one conditional call-site. Ungated, it would advise
        every agent-to-agent row in the fleet."""
        from helm import chat, seats
        names = [n for n in seats.owner_names() if n]
        self.assertTrue(names, "no owner names: every case below is vacuous")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            chat._advise_owner_post("@%s the gate ⊥ fired" % names[0])
        self.assertIn("clarity/chat", buf.getvalue())
        buf2 = io.StringIO()
        with contextlib.redirect_stderr(buf2):
            chat._advise_owner_post("@codex-2 the gate ⊥ fired")
        self.assertEqual(buf2.getvalue(), "")

    def test_the_chat_gate_does_not_PREFIX_match_the_owner_name(self):
        """The finding: a raw `"@" + name in text` substring test fires
        on @dariason and @daria-extra. The gate uses the boundary-aware
        resolver delivery uses, so a name is addressed or it is not."""
        from helm import chat, seats
        names = [n for n in seats.owner_names() if n]
        self.assertTrue(names, "no owner names: every case below is vacuous")
        owner = names[0]

        def emitted(text):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                chat._advise_owner_post(text)
            return buf.getvalue()

        # MUST-HIT: the exact name still fires.
        self.assertIn("clarity/chat", emitted("@%s the gate ⊥ fired" % owner))
        # MUST-MISS: prefix and suffix lookalikes address someone else.
        for lookalike in ("%sson" % owner, "%s-extra" % owner,
                          "%s.two" % owner, "not%s" % owner):
            self.assertEqual(emitted("@%s the gate ⊥ fired" % lookalike), "",
                             "prefix-matched @%s" % lookalike)
