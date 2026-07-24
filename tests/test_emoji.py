#!/usr/bin/env python3
"""helm.emoji — shortcode expansion, the demojize degrade path, and the
width helpers the TUI's wide-char handling sits on. Pure functions, hermetic."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import emoji  # noqa: E402


class ExpandTest(unittest.TestCase):
    def test_shortcodes_expand_at_post_time(self):
        self.assertEqual(emoji.expand(":fire: it :rocket:"), "🔥 it 🚀")
        self.assertEqual(emoji.expand(":tada::100:"), "🎉💯")
        self.assertEqual(emoji.expand(":+1: and :-1:"), "👍 and 👎")

    def test_unknown_shortcodes_pass_untouched(self):
        self.assertEqual(emoji.expand("a :nope_such_code: b"), "a :nope_such_code: b")
        self.assertEqual(emoji.expand("ratio 3:4:5 stays"), "ratio 3:4:5 stays")

    def test_empty_and_none(self):
        self.assertEqual(emoji.expand(""), "")
        self.assertEqual(emoji.expand(None), "")

    def test_map_size_and_purity(self):
        self.assertGreaterEqual(len(emoji.MAP), 120)  # the documented ~120 floor
        for k, v in emoji.MAP.items():
            self.assertTrue(v and ":" not in v, (k, v))

    def test_demojize_degrade_roundtrip(self):
        s = emoji.expand("ship :fire: now :warning:")
        self.assertNotIn(":fire:", s)
        back = emoji.demojize(s)
        self.assertIn(":fire:", back)
        self.assertIn(":warning:", back)
        self.assertNotIn("🔥", back)


class WidthTest(unittest.TestCase):
    def test_ascii_and_wide(self):
        self.assertEqual(emoji.width("abc"), 3)
        self.assertEqual(emoji.width("🔥"), 2)
        self.assertEqual(emoji.width("a🔥b"), 4)
        self.assertEqual(emoji.width(""), 0)

    def test_vs16_and_zwj_are_zero_width(self):
        self.assertEqual(emoji.width("❤️"), 2)   # heart + VS16
        self.assertEqual(emoji.ch_width("‍"), 0)

    def test_clip_never_splits_a_wide_char(self):
        self.assertEqual(emoji.clip("a🔥b", 2), "a")   # 🔥 would straddle col 2
        self.assertEqual(emoji.clip("a🔥b", 3), "a🔥")
        self.assertEqual(emoji.clip("abc", 99), "abc")

    def test_wrap_width_aware(self):
        lines = emoji.wrap("aa bb cc", 5)
        self.assertTrue(all(emoji.width(l) <= 5 for l in lines))
        self.assertEqual(" ".join(lines).split(), ["aa", "bb", "cc"])
        self.assertEqual(emoji.wrap("", 10), [""])
        hard = emoji.wrap("🔥🔥🔥🔥", 4)  # no spaces — hard break, no split glyph
        self.assertTrue(all(emoji.width(l) <= 4 for l in hard))
        self.assertEqual("".join(hard), "🔥🔥🔥🔥")


if __name__ == "__main__":
    unittest.main()
