#!/usr/bin/env python3
"""A handoff that carries nothing must never be reported as a satisfied one.

THE FAILURE, measured 2026-07-29 on this integrator's own handoff, in the same
minute it was authored:

    $ helm handoff write  <<< "DONE (this window)\n..."
    helm handoff: WARNING no DONE/REMAINING/NEXT section parsed
    helm handoff: journal entry landed — …/2026-07-29-handoff-0fa7c4ed.md
    $ helm handoff check
    helm handoff: contract satisfied — …/2026-07-29-handoff-0fa7c4ed.md

Two surfaces, one file, opposite verdicts. `check` proved a FILE EXISTED, was
recent, and named the session — and then printed a claim about the CONTRACT.
The warning went to stderr at write time; the next window only ever runs
`check`, so the next window is told the contract is discharged and stops
looking. Existence is not discharge.

WHY THE ENTRY WAS HOLLOW, which is the other half: the heading was
`DONE (this window, post-compaction)`, and the pattern accepted a keyword only
when followed by `:`/`-`/`—` or preceded by `#`. A parenthetical qualifier and
a bare unindented heading — the two most natural ways to write one — both
parsed as nothing. A contract nobody can meet by writing normally is a contract
that will be silently unmet, so the fix widens the pattern AND makes the
checker honest. Either alone leaves the failure reachable.

THE NEGATIVE CONTROLS MATTER AS MUCH: widening a heading pattern until it
matches prose would replace silent-hollow with silent-garbage, and an indented
`DONE` stamped under a task item is a status marker, not a section.
"""
import os
import shutil
import tempfile
import unittest

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import handoff  # noqa: E402

FULL = """DONE (this window, post-compaction)
- the scrub landed at 8b5c149

REMAINING
- 0.3 has no implementation

NEXT: land the resume-turn fix
"""


class SectionPatternTest(unittest.TestCase):
    def test_a_parenthetical_qualifier_on_a_heading_parses(self):
        """The exact heading that parsed as nothing."""
        s = handoff._summaries(FULL)
        self.assertEqual(s.get("done"), "the scrub landed at 8b5c149")

    def test_a_BARE_unindented_heading_parses(self):
        """`REMAINING` alone with bullets under it — no colon, no hash."""
        self.assertEqual(handoff._summaries(FULL).get("remaining"),
                         "0.3 has no implementation")

    def test_the_established_forms_still_parse(self):
        for text in ("## DONE\n- a\n## REMAINING\n- b\n## NEXT\n- c\n",
                     "**DONE**\n- a\n**REMAINING**\n- b\n**NEXT**\n- c\n",
                     "DONE: a\nREMAINING: b\nNEXT: c\n"):
            s = handoff._summaries(text)
            self.assertEqual(sorted(s), ["done", "next", "remaining"], text)

    def test_an_INDENTED_keyword_is_a_status_stamp_not_a_heading(self):
        """The discriminator the bare form rests on. Headings sit at column 0;
        a `DONE` indented under a task item is marking that item."""
        self.assertEqual(handoff._summaries("- fix the thing\n  DONE\n"), {})

    def test_prose_containing_the_words_is_not_a_heading(self):
        self.assertEqual(
            handoff._summaries("we are DONE and the NEXT thing is hard\n"), {})

    def test_a_bulleted_keyword_is_not_a_heading(self):
        self.assertEqual(handoff._summaries("- DONE\n- NEXT\n"), {})

    def test_a_MARKED_heading_may_carry_any_qualifier(self):
        """Found by cross-review against the real shelf, not by imagination.
        `## REMAINING / IN FLIGHT (updated 2026-07-29 10:00Z)` and
        `## NEXT STEPS` are how people actually write these, and the
        parenthetical-only rung rejected both — so a current, richly-written
        handoff read as HOLLOW and its author was told "no handoff artifact,
        the next window starts blind" about a file that existed and carried
        the content. A `#` or `**` is an EXPLICIT heading marker; trusting it
        is what lets the strictness stay where it earns its keep."""
        for head in ("## REMAINING / IN FLIGHT (updated 2026-07-29 10:00Z)",
                     "## REMAINING STEPS", "**REMAINING** — still open",
                     "### REMAINING work left"):
            s = handoff._summaries(head + "\n- the real content\n")
            self.assertEqual(s.get("remaining"), "the real content", head)

    def test_heading_TEXT_never_becomes_the_summary(self):
        """Only a colon introduces content on a marked line. Lifting the
        qualifier instead published "STEPS" and "IN FLIGHT (updated …)" as the
        session's next step — a frontmatter field that is present, wrong, and
        reads as populated, which is worse than empty."""
        self.assertEqual(
            handoff._summaries("## NEXT STEPS\n- land it\n").get("next"),
            "land it")
        self.assertEqual(
            handoff._summaries("## DONE: shipped X\n- detail\n").get("done"),
            "shipped X")

    def test_the_colon_may_sit_INSIDE_the_emphasis(self):
        """`**REMAINING:** the hook` leaves `:** the hook`; testing for the
        colon before stripping the markers published `** the hook`."""
        self.assertEqual(
            handoff._summaries("**REMAINING:** the hook\n").get("remaining"),
            "the hook")

    def test_an_UNMARKED_line_keeps_the_strict_rules(self):
        """The widening is scoped to explicitly-marked lines precisely so this
        stays true — otherwise prose promotes itself into the contract."""
        self.assertEqual(handoff._summaries("DONE and NEXT are hard words\n"), {})
        self.assertEqual(handoff._summaries("  DONE\n"), {})

    def test_the_FIVE_measured_phrasings_all_parse(self):
        """The word-run rung, measured 2026-08-02 — the paren rung's class a
        THIRD time: `DONE this window — THREE lanes` parsed as NOTHING, and
        the entry was still written with an empty done:. Five phrasings real
        authors actually wrote, all of which must parse; narrowing _SECTION
        back to paren-only qualifiers reddens the second and fifth."""
        cases = [
            ("DONE — three lanes", ""),
            ("DONE this window — three lanes", ""),
            ("DONE (this window, post-compaction)", "- three lanes\n"),
            ("DONE: three lanes", ""),
            ("DONE since last write: three lanes", ""),
        ]
        self.assertEqual(len(cases), 5)     # the loop cannot go vacuous
        for head, follow in cases:
            s = handoff._summaries(head + "\n" + follow)
            self.assertEqual(s.get("done"), "three lanes", head)

    def test_the_word_run_is_BOUNDED_and_needs_its_separator(self):
        """The two negative controls the widening rests on: qualifier words
        with NO separator stay prose (the run never promotes a line on its
        own), and a run past the 40-char bound stays prose too — without the
        bound any sentence starting with the keyword donates its clause."""
        # positive control on the SAME observable: the same qualifier run WITH
        # its separator parses, so the empties below are the rules firing
        self.assertEqual(
            handoff._summaries("DONE this window — we went home\n").get("done"),
            "we went home")
        self.assertEqual(
            handoff._summaries("DONE this window we went home\n"), {})
        self.assertEqual(
            handoff._summaries("DONE " + "very " * 12 + "— not a heading\n"),
            {})


class HollowTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-handoff-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _entry(self, body, kind="handoff"):
        p = os.path.join(self.d, "2026-07-29-handoff-abcd1234.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("---\nname: x\nmetadata:\n  type: %s\n  done: \n"
                    "  remaining: \n  next: \n---\n\n%s" % (kind, body))
        return p

    def test_an_entry_with_all_three_sections_is_not_hollow(self):
        self.assertEqual(handoff.hollow(self._entry(FULL)), ())

    def test_an_entry_with_no_sections_names_all_three(self):
        self.assertEqual(handoff.hollow(self._entry("just some prose\n")),
                         ("done", "remaining", "next"))

    def test_a_partial_entry_names_only_what_is_missing(self):
        self.assertEqual(handoff.hollow(self._entry("DONE: a\n")),
                         ("remaining", "next"))

    def test_FRONTMATTER_IS_NOT_PARSED_AS_SECTIONS(self):
        """The self-parse trap. An entry's own frontmatter carries `done:`,
        `remaining:` and `next:` keys, which match the section pattern exactly.
        Parsing the file whole makes a hollow entry read as three POPULATED
        sections whose content is the next key in the block — measured, and
        precisely backwards: the emptier the entry, the healthier it looks."""
        p = self._entry("just some prose\n")
        whole = handoff._summaries(open(p, encoding="utf-8").read())
        self.assertTrue(whole, "the trap must be real, or this proves nothing")
        self.assertEqual(handoff.hollow(p), ("done", "remaining", "next"),
                         "hollow() must read the body, never the frontmatter")

    def test_a_non_handoff_artifact_is_not_judged(self):
        """A repo HANDOFF_NEXT_SESSION.md or a legacy row is not this
        contract's to fail — refusing to judge beats failing loudly on
        something the checker does not own."""
        self.assertEqual(handoff.hollow(self._entry("prose\n", kind="note")), ())

    def test_an_unreadable_path_is_not_judged_either(self):
        self.assertEqual(handoff.hollow(os.path.join(self.d, "gone.md")), ())


class CheckRejectsHollowTest(unittest.TestCase):
    """check() — the surface that said 'satisfied' over an empty contract."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="helm-test-hc-")
        self.j = os.path.join(self.home, "helm", "journal")
        os.makedirs(self.j)
        self._env = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = self.home

    def tearDown(self):
        if self._env is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self._env
        shutil.rmtree(self.home, ignore_errors=True)

    def _write(self, body, sid="abcd1234-0000-0000-0000-000000000000"):
        p = os.path.join(self.j, "2026-07-29-handoff-abcd1234.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("---\nmetadata:\n  type: handoff\n  session_id: %s\n---\n\n%s"
                    % (sid, body))
        return p

    def _check(self, allow_hollow=False):
        from unittest import mock
        sid = "abcd1234-0000-0000-0000-000000000000"
        with mock.patch.object(handoff, "_project", return_value="helm"):
            return handoff.check(sid, "/nonexistent-cwd", 0,
                                 allow_hollow=allow_hollow)

    def test_a_populated_entry_satisfies(self):
        p = self._write(FULL)
        self.assertEqual(self._check(), p)

    def test_a_HOLLOW_entry_does_NOT_satisfy(self):
        """The regression. Before this, the nag went quiet exactly when the
        handoff needed rewriting."""
        self._write("prose with no sections\n")
        self.assertIsNone(self._check())

    def test_allow_hollow_still_FINDS_it_so_the_words_can_differ(self):
        """'you never wrote one' and 'you wrote one that carries nothing' ask
        the author for different things; collapsing them sends someone looking
        for a file that is already there."""
        p = self._write("prose with no sections\n")
        self.assertEqual(self._check(allow_hollow=True), p)


if __name__ == "__main__":
    unittest.main()
