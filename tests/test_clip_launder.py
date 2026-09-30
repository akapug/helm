"""test_clip_launder: the two one-line helpers a terminal prints strip what could reshape it (task/3257).

seat_lifecycle_runtime._one_line and review_door._clip_line collapse whitespace FIRST and then launder
through pk.launder, so an ESC or a bidi override never reaches the terminal while a newline or a tab still
becomes a space. Invisible characters are written as escapes so this file shows what it tests.
"""
import unittest

from helm import seat  # noqa: F401 — the facade the impl import below requires
from helm import review_door, seat_lifecycle_runtime


class OneLineTest(unittest.TestCase):

    def test_esc_and_bidi_are_stripped(self):
        for hostile in ("a\x1b[2Jb", "a\u202eb"):
            self.assertEqual(seat_lifecycle_runtime._one_line(ValueError(hostile)),
                             hostile.replace("\x1b", "").replace("\u202e", ""))

    def test_whitespace_still_becomes_one_space(self):
        self.assertEqual(seat_lifecycle_runtime._one_line(ValueError("a\nb\tc")), "a b c")

    def test_a_message_with_nothing_printable_names_the_class(self):
        self.assertEqual(seat_lifecycle_runtime._one_line(KeyError()), "KeyError")
        self.assertEqual(seat_lifecycle_runtime._one_line(ValueError("\x1b\u202e")), "ValueError")


class ClipLineTest(unittest.TestCase):

    def test_esc_and_bidi_are_stripped(self):
        self.assertEqual(review_door._clip_line("a\x1b[2Jb \u202ec"), "a[2Jb c")

    def test_whitespace_still_becomes_one_space(self):
        self.assertEqual(review_door._clip_line("a\nb\tc"), "a b c")

    def test_it_still_clips_at_n(self):
        self.assertEqual(review_door._clip_line("x" * 200), "x" * 159 + "\u2026")

    def test_the_clip_measures_the_laundered_text(self):
        # 150 visible characters plus 20 ESC: laundered it is 150, under the 160 limit, so it comes back
        # whole; measuring before laundering would have clipped it
        self.assertEqual(review_door._clip_line("x" * 150 + "\x1b" * 20), "x" * 150)


if __name__ == "__main__":
    unittest.main()
