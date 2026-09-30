"""task/1896 — Claude Code's EMPTY-composer hint is chrome, not a human draft.

Measured 2026-09-09 on three reseeds in one night: `helm seat spawn` ended
INCOMPLETE because the onboarding pre-read refused to type into a composer it
read as a possible human draft. On kimi the refusal quoted the composer
verbatim, and what it quoted was Claude Code's placeholder for an EMPTY
composer:

    pane … composer holds 'Try "how do I log an error?"' — refusing to type or
    spend Enter because it may be a human draft

The capture that reaches the oracle is the prompt row of `terminal read`, with
ANSI (the dim styling the hint is drawn in) stripped by the composer locator and
the `❯`/U+00A0 prefix and whitespace runs normalized away by `composer_body`. So
the body the classifier sees is exactly `Try "how do I log an error?"`.

THE PATTERN IS STRICT ON PURPOSE. Reading a human's draft as chrome is the
failure that costs more — helm would then type its brief after it and spend an
Enter on both — so only a composer whose WHOLE content is one `Try "<text>"`
hint reads clear. Anything typed before, after or beside it, a second line, and
a draft that merely begins with the word Try all stay held.
"""

import unittest

from helm import composers, harness
# The co-occurrence guard in tests/test_seat_facade_injection.py requires the
# facade import beside any impl import; this line exists for that alone.
from helm import seat  # noqa: F401
from helm.seat_lifecycle import _current_prompt_line

# THE MEASURED BODY, from the kimi refusal's own %r of the composer.
HINT = 'Try "how do I log an error?"'


def _tail(row):
    """A pane tail whose current composer row is `row` (the text after the
    glyph), shaped like a real Claude frame: rules around the input box and
    the footer chrome under it."""
    return "\n".join([
        "─" * 40,
        "❯\xa0%s" % row,
        "─" * 40,
        "  opus-5 | ~/dev/example/repo",
        "  ⏵⏵ bypass permissions on",
    ])


def _body(tail):
    line = _current_prompt_line(tail)
    return None if line is None else harness.composer_body(line)


class TheHintReadsAsChromeTest(unittest.TestCase):

    def test_the_measured_hint_is_placeholder_chrome(self):
        self.assertEqual(_body(_tail(HINT)), HINT,
                         "the fixture does not reach the oracle as measured")
        self.assertTrue(harness.composer_is_placeholder(HINT))

    def test_other_hint_examples_are_chrome_too(self):
        for hint in ('Try "fix lint errors"',
                     'Try "write a test for helm/harness.py"',
                     'Try "refactor <filepath>"'):
            with self.subTest(hint=hint):
                self.assertTrue(harness.composer_is_placeholder(hint))

    def test_the_hint_in_dim_styling_with_trailing_spaces_is_chrome(self):
        """The hint is drawn dim; the capture may carry the SGR codes and pad
        the row. Both are stripped before the classifier sees the body."""
        tail = _tail("\x1b[2m%s\x1b[22m   " % HINT)
        self.assertEqual(_body(tail), HINT)
        self.assertEqual(harness.observe_composer(tail, "Run it."),
                         harness.PLACEHOLDER)

    def test_the_census_calls_a_hint_only_composer_clear(self):
        pane = {"handle": "term_x", "title": "seat", "worktree": "/wt/seat",
                "last_output_at": None}
        state, _body_, why = composers.classify(pane, _tail(HINT))
        self.assertEqual(state, composers.CLEAR, why)

    def test_the_older_placeholder_still_reads_as_chrome(self):
        self.assertTrue(harness.composer_is_placeholder(
            harness._COMPOSER_PLACEHOLDERS[0]))


class ADraftStaysHeldTest(unittest.TestCase):
    """THE CONTROLS: the pattern must not swallow a person's text."""

    DRAFTS = (
        'x' + HINT,                          # typed before it
        HINT + 'x',                          # typed after it
        HINT + ' and more',                  # a continuation
        'Try the other flag',                # merely starts with Try
        'try "x" then y',                    # lower-case, with a tail
        'try "how do I log an error?"',      # lower-case alone
        'Try "a" "b"',                       # two quoted parts
        'Try ""',                            # nothing quoted
        'Try "unterminated',
        'Try',
    )

    def test_drafts_are_not_placeholders(self):
        for draft in self.DRAFTS:
            with self.subTest(draft=draft):
                self.assertFalse(harness.composer_is_placeholder(draft))

    def test_the_census_holds_them(self):
        pane = {"handle": "term_x", "title": "seat", "worktree": "/wt/seat",
                "last_output_at": "2026-09-09T00:00:00Z"}
        for draft in ('Try the other flag', HINT + ' and more'):
            with self.subTest(draft=draft):
                state, _b, why = composers.classify(pane, _tail(draft))
                self.assertEqual(state, composers.HELD, why)

    def test_the_hint_with_a_second_typed_line_is_not_the_hint(self):
        """A second composer row below the hint is newer content: the
        composer locator refuses the frame rather than reading the hint row
        alone, so nothing reads clear."""
        tail = "\n".join(["─" * 40, "❯\xa0%s" % HINT, "  second line",
                          "─" * 40, "  ⏵⏵ bypass permissions on"])
        pane = {"handle": "term_x", "title": "seat", "worktree": "/wt/seat",
                "last_output_at": "2026-09-09T00:00:00Z"}
        state, _b, why = composers.classify(pane, tail)
        self.assertNotEqual(state, composers.CLEAR, why)


if __name__ == "__main__":
    unittest.main()
