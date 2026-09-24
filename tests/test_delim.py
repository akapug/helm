#!/usr/bin/env python3
"""helm delim — the pipe-delimited capture grammar.

Four verbs took their body as one `|`-split argument and every one of them
guarded only `len(parts) < 2`. A pipe INSIDE a statement makes MORE fields, so
the guard never fired, and the surplus CASCADED: statement truncated at the
first pipe, its tail promoted to keywords, keywords promoted to domain, domain
dropped off the end. Silent, rc 0 — and `helm premise` then signed the
truncated head into the attestation chain at confidence 1.00.

These tests hold three things down:
  1. the escape EXISTS, so a field containing a pipe is expressible at all;
  2. an over-arity body is REFUSED and writes nothing;
  3. the guard's PARTIALITY is pinned, so no later reader can cite it as
     total. That last one is a test whose job is to keep a limitation true.
"""
import contextlib
import io
import os
import shutil
import tempfile
import unittest

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-delim-", var="HELM_HOME")

from helm import delim  # noqa: E402


class FieldsTest(unittest.TestCase):
    """The parser alone — total, and the escape is the only escape."""

    def test_plain_split_is_unchanged(self):
        self.assertEqual(delim.fields("a|b|c"), ["a", "b", "c"])
        self.assertEqual(delim.fields("no pipes here"), ["no pipes here"])
        self.assertEqual(delim.fields(""), [""])

    def test_escaped_pipe_is_a_literal_and_does_NOT_split(self):
        self.assertEqual(delim.fields(r"a \| b"), ["a | b"])
        self.assertEqual(delim.fields(r"id | a \| b | kw"), ["id ", " a | b ", " kw"])

    def test_every_other_backslash_is_DATA(self):
        """Prose carries regexes and Windows paths. Forcing an author to
        double them would trade a rare bug for a permanent tax."""
        self.assertEqual(delim.fields(r"match \b\w+ in C:\Users\tester"),
                         [r"match \b\w+ in C:\Users\tester"])
        self.assertEqual(delim.fields(r"tex \alpha"), [r"tex \alpha"])

    def test_a_trailing_lone_backslash_is_data_not_a_crash(self):
        self.assertEqual(delim.fields("ends with a backslash \\"),
                         ["ends with a backslash \\"])
        self.assertEqual(delim.fields("\\"), ["\\"])

    def test_it_is_TOTAL_on_adversarial_input(self):
        """It runs in front of every capture; it may never raise."""
        for s in ("|", "||", "|||", "\\", "\\\\", "\\|", "|\\", "\\|\\|",
                  " ", "\n|\n", "a" * 5000 + "|" + "b" * 5000, "\x00|\x01"):
            delim.fields(s)          # named by not raising

    def test_the_documented_limit_of_a_narrow_escape(self):
        r"""`\\|` is a literal backslash and THEN a delimiter — a literal
        backslash-followed-by-pipe cannot be written. That is a real cost of
        not requiring doubling, and it is pinned here rather than discovered."""
        self.assertEqual(delim.fields(r"a \\| b"), ["a \\\\", " b"])


class SplitTest(unittest.TestCase):
    def test_within_arity_returns_stripped_parts_and_no_refusal(self):
        parts, refused = delim.split("id | statement | kw | dom", 4, "g")
        self.assertIsNone(refused)
        self.assertEqual(parts, ["id", "statement", "kw", "dom"])

    def test_over_arity_refuses_and_returns_NO_parts(self):
        parts, refused = delim.split("id | a | b | c | d", 4, "g")
        self.assertIsNone(parts)     # never a half-parse the caller might use
        self.assertIsNotNone(refused)

    def test_a_SHORT_body_is_not_this_functions_business(self):
        """A missing field and an eaten field are different bugs with
        different messages; callers keep their own required-field checks."""
        parts, refused = delim.split("just-an-id", 4, "g")
        self.assertIsNone(refused)
        self.assertEqual(parts, ["just-an-id"])

    def test_the_refusal_ANSWERS_the_operators_next_question(self):
        _, refused = delim.split("id | the tool said OK | but the payload "
                                 "was mangled | kw | dom", 4, "the-grammar")
        self.assertIn("5 fields", refused)          # what it counted
        self.assertIn("at most 4", refused)         # against what
        self.assertIn("the-grammar", refused)       # the shape it wanted
        self.assertIn("the tool said OK", refused)  # THE PARSE, field by field
        self.assertIn("but the payload was mangled", refused)
        self.assertIn(r"\|", refused)               # and how to say it instead

    def test_the_refusal_counts_in_the_singular_when_one_field_is_over(self):
        _, one = delim.split("a|b|c|d|e", 4, "g")
        self.assertIn("the 1 that does not fit", one)
        _, two = delim.split("a|b|c|d|e|f", 4, "g")
        self.assertIn("the 2 that do not fit", two)

    def test_an_escaped_body_passes_the_arity_it_would_have_blown(self):
        """The escape is what makes the refusal fair — there is now always a
        way to say the thing that was refused."""
        parts, refused = delim.split(r"id | a \| b | kw | dom", 4, "g")
        self.assertIsNone(refused)
        self.assertEqual(parts[1], "a | b")


class EchoTest(unittest.TestCase):
    def test_an_empty_field_READS_as_empty(self):
        """An empty field rendering as nothing is how the cascade stayed
        invisible in the first place."""
        self.assertEqual(delim.echo("   "), "(empty)")
        self.assertEqual(delim.echo(""), "(empty)")

    def test_a_long_field_is_clipped_not_dumped(self):
        out = delim.echo("x" * 500)
        self.assertLess(len(out), 100)
        self.assertTrue(out.endswith("…"))


class VerbBase(unittest.TestCase):
    """Each verb driven end-to-end — dogfood, not just unit coverage."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-delim-")
        self.prior = {k: os.environ.get(k) for k in ("HELM_HOME",)}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_verb(self, fn, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fn(list(args))
        return rc, out.getvalue() + err.getvalue()

    def store(self, *args):
        from helm import store
        return self.run_verb(store.cmd_store, args)

    def get(self, pid):
        from helm import store
        rc, out = self.run_verb(store.cmd_store, ["get", pid])
        return rc, out


class StoreAddTest(VerbBase):
    def test_THE_INCIDENT_the_five_field_premise_now_refuses(self):
        """The exact body measured at HEAD on 2026-07-25: it stored the head
        as the statement, promoted the tail to keywords, promoted keywords to
        domain, dropped the domain, and exited 0."""
        rc, out = self.store("add", "premise",
                             "probe-a | the tool said OK | but the payload "
                             "was mangled | mangled,silent | helm")
        self.assertEqual(rc, 2)
        self.assertIn("5 fields", out)
        rc, _ = self.get("probe-a")
        self.assertEqual(rc, 1)      # and NOTHING was written

    def test_a_refusal_writes_NOTHING(self):
        """A guard that refuses after writing is worse than no guard: it
        reports failure and leaves the damage."""
        self.store("add", "prior", "ghost | a | b | c | d | e | f")
        rc, out = self.get("ghost")
        self.assertEqual(rc, 1)
        self.assertIn("not found", out)

    def test_prose_in_the_CONFIDENCE_slot_refuses_instead_of_storing_0_6(self):
        """The one slot with a type is the one place a within-arity cascade
        can be caught exactly. It used to hit `except ValueError: conf = 0.6`
        and drop the text with no message at all."""
        rc, out = self.store("add", "prior",
                             "probe-b | a claim with a | pipe in it")
        self.assertEqual(rc, 2)
        self.assertIn("not a number", out)
        self.assertIn("pipe in it", out)     # names what it choked on
        rc, _ = self.get("probe-b")
        self.assertEqual(rc, 1)

    def test_a_real_confidence_still_parses(self):
        rc, _ = self.store("add", "prior", "probe-c | a belief | 0.8 | probekw")
        self.assertEqual(rc, 0)
        rc, out = self.get("probe-c")
        self.assertIn("0.80", out)

    def test_the_ESCAPE_stores_a_literal_pipe_in_the_RIGHT_field(self):
        """The root defect closed: a statement containing a pipe was not
        merely mis-stored before, it was INEXPRESSIBLE."""
        rc, _ = self.store("add", "premise",
                           r"probe-d | mangled \| and the tool said OK "
                           r"| mangled,silent | helm")
        self.assertEqual(rc, 0)
        rc, out = self.get("probe-d")
        self.assertIn("mangled | and the tool said OK", out)
        self.assertIn("mangled,silent", out)     # keywords NOT the tail
        self.assertIn("helm", out)               # domain NOT dropped

    def test_every_type_carries_its_own_arity(self):
        """prior takes 5, premise 4 — a shared cap would refuse legal priors
        or admit an over-long premise."""
        rc, _ = self.store("add", "prior", "p5 | s | 0.7 | kw | dom")
        self.assertEqual(rc, 0)
        rc, out = self.store("add", "premise", "p4 | s | kw | dom | extra")
        self.assertEqual(rc, 2)
        self.assertIn("at most 4", out)


class PremiseVerbTest(VerbBase):
    def test_it_refuses_BEFORE_it_can_attest_a_fragment(self):
        """This verb signs parts[1] into the chain, so a cascade here does not
        store a fragment — it ATTESTS one at confidence 1.00."""
        from helm.premise import _capture
        rc, out = self.run_verb(_capture.cmd_premise,
                                ["pv-a | statement | kw | dom | overflow",
                                 "--no-attest"])
        self.assertEqual(rc, 2)
        self.assertIn("5 fields", out)
        rc, _ = self.get("pv-a")
        self.assertEqual(rc, 1)

    def test_the_escape_reaches_the_attesting_verb_too(self):
        from helm.premise import _capture
        rc, out = self.run_verb(_capture.cmd_premise,
                                [r"pv-b | a \| b | kw | dom", "--no-attest"])
        self.assertEqual(rc, 0)
        rc, got = self.get("pv-b")
        self.assertIn("a | b", got)


class Arity2VerbTest(VerbBase):
    """reflex and mentor take `id | steer` and nothing else, so ANY unescaped
    pipe in a steer is over-arity — the guard is TOTAL for these two."""

    def test_reflex_add_refuses_a_piped_steer(self):
        from helm import reflex
        rc, out = self.run_verb(reflex.cmd_reflex,
                                ["add", "rx-a | a steer with a | pipe"])
        self.assertEqual(rc, 2)
        self.assertIn("3 fields", out)
        self.assertIn("at most 2", out)

    def test_reflex_add_takes_the_escaped_form(self):
        from helm import reflex
        rc, _ = self.run_verb(reflex.cmd_reflex,
                              ["add", r"rx-b | a steer with a \| pipe"])
        self.assertEqual(rc, 0)

    def test_mentor_refuses_a_piped_steer(self):
        """Both the SUBCOMMAND and the project must be real or this never
        reaches the guard: mentor refuses an unknown subcommand, then an
        unknown project at :349, and only then splits at :354. Written the
        lazy way first, it passed on `unknown subcommand 'realproj'` — a test
        that agreed with the fix without ever running it."""
        from helm import home, mentor, pk
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "realproj": {"name": "realproj", "path": "/p/realproj"}}})
        rc, out = self.run_verb(mentor.cmd_mentor,
                                ["teach", "realproj", "mt-a | steer with | pipe"])
        self.assertEqual(rc, 2)
        self.assertIn("3 fields", out)      # MY guard, not the project gate
        self.assertIn("at most 2", out)


class ThePartialityIsPinnedTest(VerbBase):
    """This test's job is to keep a LIMITATION true.

    A stray pipe that lands within the arity in a type-valid slot is
    syntactically identical to a deliberate full-arity call. No validator
    distinguishes them. If someone later 'fixes' this case with a heuristic,
    this test fails and they must come argue for it — which is the point,
    because the alternative is a guard quietly advertised as total.
    """

    def test_a_within_arity_misplit_STILL_passes_and_that_is_known(self):
        rc, _ = self.store("add", "premise", "lim-a | stmt with | a pipe")
        self.assertEqual(rc, 0)
        rc, out = self.get("lim-a")
        self.assertIn("stmt with", out)     # statement truncated, as designed
        self.assertIn("a pipe", out)        # tail read as keywords, legally

    def test_and_the_module_SAYS_so_rather_than_implying_totality(self):
        self.assertIn("partial BY CONSTRUCTION", delim.__doc__)


if __name__ == "__main__":
    unittest.main()
