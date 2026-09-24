#!/usr/bin/env python3
"""helm/yaml_scalar.py — the ONE scalar reader for the generated proxy config.

WHY THIS FILE EXISTS AT ALL. The module's own docstring records that two
copies of this reader disagreed on 7 of 19 measured poles, and that the worst
disagreement was SILENT: a single-quoted value carrying a trailing comment
came back with its quotes still on, so a watchdog compared `'abc'` against the
generator's `abc` and minted drift on a field nobody had edited. The cure was
to keep ONE reader. Nothing then pinned what that reader answers, so the
census read it as wired-and-unverified and the seven poles lived only in
prose.

EVERY ARM HERE IS A POLE, and each one names the wrong answer it refuses.
The absence arms carry their positive control in the same method, on the same
observable (the same call's first element), because an arm that only ever
sees None passes just as well when the reader has stopped returning values at
all.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from helm.yaml_scalar import (ABSENT, yaml_scalar, yaml_scalar_or_raise,
                              yaml_scalar_typed)


class AbsenceIsTyped(unittest.TestCase):
    """The reader's absence is a NAMED class, never a bare None, because the
    plan side refuses on unreadable and the watchdog tolerates not-there."""

    def test_an_empty_line_is_absent_and_a_written_one_is_not(self):
        self.assertEqual(yaml_scalar(""), (None, ABSENT))
        self.assertEqual(yaml_scalar("   \t "), (None, ABSENT))
        # POSITIVE CONTROL on the same observable: this call proves the first
        # element can be a value, so the two Nones above are the reader's
        # answer and not a reader that has stopped answering.
        self.assertEqual(yaml_scalar("written"), ("written", None))

    def test_a_comment_only_line_is_absent_not_the_comment_text(self):
        """`# note` is null in the producer's grammar. Returning "# note" as
        the value would ship a comment into config as a setting."""
        self.assertEqual(yaml_scalar("  # just a note"), (None, ABSENT))
        self.assertEqual(yaml_scalar("value  # just a note"),
                         ("value", None))

    def test_absent_and_unreadable_are_different_errs(self):
        """The whole reason err is a string and not a bool: a caller that
        tolerates not-there must still refuse on not-readable."""
        self.assertEqual(yaml_scalar("")[1], ABSENT)
        self.assertEqual(yaml_scalar("'unclosed")[1],
                         "malformed single-quoted scalar")
        self.assertNotEqual(yaml_scalar("'unclosed")[1], ABSENT)


class QuotesComeOff(unittest.TestCase):
    """THE ORIGINAL DRIFT. A quoted value with a trailing comment must reach
    the caller as the string the generator wrote — unquoted, uncommented."""

    def test_a_single_quoted_value_with_a_trailing_comment_loses_both(self):
        """The measured silent disagreement, verbatim: the upstream example
        file's own documented style. A startswith/endswith reader falls to
        the bare branch here (the line ends in a letter) and answers
        "'abc' # c" — a different string than the generator wrote."""
        self.assertEqual(yaml_scalar("'abc'  # trailing"), ("abc", None))

    def test_a_double_quoted_value_with_a_trailing_comment_loses_both(self):
        self.assertEqual(yaml_scalar('"abc"  # trailing'), ("abc", None))

    def test_single_quotes_escape_by_doubling(self):
        """YAML's own rule. `''` inside a single-quoted scalar is one quote;
        a reader that strips the outer pair and stops hands back `it''s`."""
        self.assertEqual(yaml_scalar("'it''s here'"), ("it's here", None))
        self.assertEqual(yaml_scalar("'it''s'  # c"), ("it's", None))

    def test_an_empty_quoted_string_is_a_VALUE_and_never_absent(self):
        """An empty scalar and an empty quoted string are different facts.
        Collapsing them is why absence had to be typed in the first place."""
        self.assertEqual(yaml_scalar("''"), ("", None))
        self.assertEqual(yaml_scalar('""'), ("", None))
        # the same observable carrying the OTHER answer, so "" being falsey
        # cannot be read here as the absent branch having fired.
        self.assertEqual(yaml_scalar("")[1], ABSENT)
        self.assertIsNone(yaml_scalar("''")[1])

    def test_trailing_junk_after_a_quoted_scalar_REFUSES(self):
        """Only a comment may follow the closing quote. Anything else is a
        line this reader cannot honestly claim to have understood."""
        self.assertEqual(yaml_scalar('"abc" junk')[1],
                         "trailing content after double-quoted scalar")
        self.assertEqual(yaml_scalar("'abc' junk")[1],
                         "malformed single-quoted scalar")
        self.assertIsNone(yaml_scalar('"abc" # ok')[1])


class TheCommentRuleIsWhitespaceThenHash(unittest.TestCase):
    r"""ANY whitespace opens a comment — the generator's rule and YAML's. The
    first unification cut kept the OTHER copy, which split on a single space
    only, and would have shipped `abc\t#c` into config as its own value."""

    def test_a_TAB_before_the_hash_opens_a_comment(self):
        self.assertEqual(yaml_scalar("abc\t# c"), ("abc", None))

    def test_two_spaces_before_the_hash_open_a_comment(self):
        """`abc  #c` is the ordinary emitted form; a one-space split leaves
        the second space and the hash glued to the value."""
        self.assertEqual(yaml_scalar("abc  #c"), ("abc", None))

    def test_a_hash_with_NO_whitespace_before_it_is_part_of_the_scalar(self):
        """Which is why this is not a bare split on "#". A colour, a fragment
        and an anchor all live inside real values."""
        self.assertEqual(yaml_scalar("abc#c"), ("abc#c", None))
        self.assertEqual(yaml_scalar("#ffffff"), (None, ABSENT))  # leading #


class ThePlanSideRefuses(unittest.TestCase):
    def test_or_raise_raises_the_err_it_was_given(self):
        """The plan side cannot tolerate an unreadable desired state, and the
        message it raises is the err class so an operator learns which."""
        with self.assertRaises(ValueError) as caught:
            yaml_scalar_or_raise("   ")
        self.assertEqual(str(caught.exception), ABSENT)
        with self.assertRaises(ValueError) as caught:
            yaml_scalar_or_raise("'unclosed")
        self.assertEqual(str(caught.exception),
                         "malformed single-quoted scalar")
        # POSITIVE CONTROL on the same call: a readable line must NOT raise,
        # or the two assertRaises above would pass against a function that
        # refuses everything.
        self.assertEqual(yaml_scalar_or_raise("'abc'  # c"), "abc")


class QuotingIsTypeInformation(unittest.TestCase):
    """yaml_scalar_typed exists for ONE question: the fork field's producer
    (yaml.v3 into a Go bool) rejects a QUOTED boolean, so a watchdog reading
    the stripped scalar alone would certify syntax the producer refuses."""

    def test_a_quoted_boolean_is_reported_as_quoted(self):
        self.assertEqual(yaml_scalar_typed('"true"  # c'),
                         ("true", None, True))
        self.assertEqual(yaml_scalar_typed("'true'"), ("true", None, True))

    def test_a_bare_boolean_is_reported_as_unquoted(self):
        """The control that makes the quoted arms mean something: the flag
        must be able to read False, or `quoted=True` proves nothing."""
        self.assertEqual(yaml_scalar_typed("true  # c"),
                         ("true", None, False))

    def test_the_stripped_value_is_IDENTICAL_to_the_plain_reader(self):
        """The typed reader adds a flag; it may never become a second
        grammar. Two readers disagreeing is the defect this module cured."""
        # UNCONDITIONAL CONTROL, ahead of the loop: both readers answer a
        # real value for a real line, so the agreement asserted below is two
        # readers agreeing rather than two readers both returning nothing.
        self.assertEqual(yaml_scalar_typed("'x'  # c"), ("x", None, True))
        self.assertEqual(yaml_scalar("'x'  # c"), ("x", None))
        for line in ("'it''s'  # c", '"abc" # c', "abc\t#c", "abc#c",
                     "", "  # only", "''", "'unclosed"):
            self.assertEqual(yaml_scalar_typed(line)[:2], yaml_scalar(line),
                             "typed reader diverged on %r" % line)


if __name__ == "__main__":
    unittest.main()
