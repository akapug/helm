"""test_pk_launder — pk.launder and the three helpers that wrap it (task/3257).

pk.launder strips C0/C1 controls, format characters (bidi overrides included) and line/paragraph separators,
keeping the characters in `keep` (a tab by default). seats_common._scrub, todos._scrub and chat._dsan wrap it
and keep their behaviour: the two _scrub helpers take strings only, and chat._dsan passes a non-string through.
Invisible characters are written as escapes so this file shows what it tests.
"""
import unittest

from helm import chat, pk, seats_common, todos

HOSTILE = "a\x1b[2Jb\x07\x08c‮D‎E F G\x85H\x00\tI"
LAUNDERED = "a[2JbcDEFGH\tI"


class PkLaunderTest(unittest.TestCase):

    def test_strips_controls_format_characters_and_separators(self):
        self.assertEqual(pk.launder(HOSTILE), LAUNDERED)

    def test_keeps_printable_text_of_any_script(self):
        text = "café 日本 \U0001f600 plain"
        self.assertEqual(pk.launder(text), text)

    def test_keep_names_what_survives(self):
        self.assertEqual(pk.launder("a\tb\nc"), "a\tbc")
        self.assertEqual(pk.launder("a\tb\nc", keep=""), "abc")
        self.assertEqual(pk.launder("a\tb\nc", keep="\t\n"), "a\tb\nc")

    def test_the_three_helpers_equal_pk_launder(self):
        for helper in (seats_common._scrub, todos._scrub, chat._dsan):
            self.assertEqual(helper(HOSTILE), LAUNDERED, helper.__module__)

    def test_non_strings_keep_each_helpers_behaviour(self):
        self.assertIsNone(chat._dsan(None))
        self.assertEqual(chat._dsan(42), 42)
        for helper in (seats_common._scrub, todos._scrub):
            with self.assertRaises(TypeError, msg=helper.__module__):
                helper(None)


if __name__ == "__main__":
    unittest.main()
