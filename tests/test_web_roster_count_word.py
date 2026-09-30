#!/usr/bin/env python3
"""COUNT-WORD UNIFICATION (console walk 3, finding 4) — the seat counts.

Finding 4: "at work" and "live" each named two different seat counts. The cure
is one word per count, and a word never reused for another count:

  * ACTIVE — a beat inside 2 minutes, not unverified, not an ephemeral done SA,
    not the owner's own row. Home's seats tile (`homeSeats`), the Seats page's
    "on now" strip (`dashFleet`) and its roster count (`#rostercount` in
    `renderRoster`) count this set, and every roster row and picker option
    already labels such a seat "active" (`presenceLabel`).
  * JOINED — every non-ephemeral seat, gone ones included: `#rostercount`
    beside the active count, and the "message a seat" picker's
    `#seatpickcount` in `seatPicker`. Never "live": 57 joined seats were called
    live when 14 were active.
  * "able to work" stays the Projects headline's own count (`web_board._usable`:
    up, not walled, not paused, not on a RED family) and "at work" its
    claimless seats. Neither may name the active count: a walled-off seat is
    active and is NOT able to work.

One payload is fed to every renderer and each count is read against its
siblings, so a surface that counts a different set, or words the same set
differently, fails here. The functions are the REAL ones, lifted verbatim from
the assembled web UI and run under node with a minimal DOM. Requires node;
skipped (not failed) where node is unavailable, like any optional toolchain.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from helm import web_ui_loader

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "roster_count_word_harness.js")


def _lift(src, name):
    """The verbatim `function NAME(…) {…}` or `const NAME = …;` source.

    Brace/paren/regex/comment aware: a `/` opens a regex only where an
    expression may start, and a `//` or `/*` inside the body is skipped so an
    apostrophe or brace inside a comment or string never derails the match."""
    m = re.search(r"(?:function\s+" + re.escape(name) + r"\s*\(|const\s+"
                  + re.escape(name) + r"\s*=)", src)
    if not m:
        raise AssertionError("not found in assembled web UI: " + name)
    is_fn = src[m.start()] == "f"
    i, n = m.end(), len(src)
    depth, quote, esc_next, last = (1 if is_fn else 0), None, False, "="
    while i < n:
        c = src[i]
        if quote:
            if esc_next:
                esc_next = False
            elif c == "\\":
                esc_next = True
            elif c == quote:
                quote = None
            i += 1
            continue
        if c in "\"'`":
            quote = c
        elif c == "/" and i + 1 < n and src[i + 1] == "/":
            i = src.index("\n", i) if "\n" in src[i:] else n
            continue
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            i = src.index("*/", i) + 2
            continue
        elif c == "/" and (last in "=(,:[!&|?{};+-*%<>~^" or last == ""):
            i += 1
            in_class, esc_next = False, False
            while i < n:
                r = src[i]
                if esc_next:
                    esc_next = False
                elif r == "\\":
                    esc_next = True
                elif r == "[":
                    in_class = True
                elif r == "]":
                    in_class = False
                elif r == "/" and not in_class:
                    break
                i += 1
        elif c in "({[":
            depth += 1
        elif c in ")}]":
            depth -= 1
            if is_fn and c == "}" and depth == 0:
                return src[m.start():i + 1]
        if not c.isspace():
            last = c
        i += 1
    raise AssertionError("unterminated source lifting " + name)


class RosterCountWordTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        cls.src = web_ui_loader.read_text()
        lifted = {name: _lift(cls.src, name) for name in (
            "lrDur", "lrAgo", "cardSource", "dashFleet", "homeWord", "homeTile",
            "homeSeats", "renderRoster", "seatPicker")}
        with open(HARNESS, encoding="utf-8") as f:
            template = f.read()
        assert "/*__INJECT__*/" in template
        assembled = template.replace("/*__INJECT__*/",
                                     "\n\n".join(lifted[n] for n in lifted))
        cls.tmp = tempfile.mkdtemp(prefix="helm-roster-count-")
        path = os.path.join(cls.tmp, "run.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(assembled)
        chk = subprocess.run([cls.node, "--check", path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        proc = subprocess.run([cls.node, path], capture_output=True,
                              text=True, timeout=60)
        assert proc.returncode == 0, "harness failed:\n" + proc.stderr
        cls.out = {r["name"]: r for r in json.loads(proc.stdout or "[]")}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def _strip(self, html):
        return re.sub(r"<[^>]*>", "", html)

    def _count(self, word, text):
        """The number written before `word` in `text`, or None."""
        m = re.search(r"(\d+) " + re.escape(word) + r"\b", text)
        return int(m.group(1)) if m else None

    def test_one_payload_draws_one_active_count_in_one_word_everywhere(self):
        """Home's tile, the Seats strip and the roster count, fed ONE payload,
        each read "2 active": a and b. The quiet, the unverified, the gone, the
        ephemeral SA and the owner are not counted, on any of the three."""
        mix = self.out["presence_mix"]
        texts = {k: self._strip(mix[k]) for k in ("home", "fleet", "roster")}
        self.assertEqual({k: self._count("active", t) for k, t in texts.items()},
                         {"home": 2, "fleet": 2, "roster": 2}, texts)

    def test_the_active_count_never_wears_another_counts_word(self):  # noqa: VACUOUS_ASSERTION — the first statement asserts the mixed roster count exactly (2 active), and each loop pass asserts its own count present before its absences
        """b is walled off by its vendor and still active, so the active count
        is not "able to work" (the Projects headline's count, which leaves a
        walled seat out); "at work" is the Projects headline's claimless
        seats; "live" named the joined seats. The counts of this file
        are asserted first, so every absence reads a real render."""
        self.assertEqual(self._count("active", self._strip(
            self.out["presence_mix"]["roster"])), 2)
        for name in ("mixed_and_owner", "presence_mix", "empty"):
            roster = self._strip(self.out[name]["roster"])
            self.assertIsNotNone(self._count("active", roster), (name, roster))
            for word in ("able to work", "at work", "live"):
                self.assertNotIn(word, roster, (name, word, roster))
        mix = self.out["presence_mix"]
        for key in ("home", "fleet"):
            text = self._strip(mix[key])
            self.assertEqual(self._count("active", text), 2, (key, text))
            for word in ("able to work", "at work", "live"):
                self.assertNotIn(word, text, (key, word, text))

    def test_the_joined_count_is_one_number_on_the_roster_and_the_picker(self):  # noqa: VACUOUS_ASSERTION — the roster and picker counts of the same run are asserted exactly (5 and 5) before the "live" absences are read over them
        """The roster count and the picker count one joined set: every
        non-ephemeral seat, gone ones included, never the done SA and never the
        owner. a, b, q, u and g are 5 on both."""
        mix = self.out["presence_mix"]
        self.assertEqual(self._count("joined", self._strip(mix["roster"])), 5,
                         mix["roster"])
        self.assertEqual(self._count("joined seats", mix["picker"]), 5,
                         mix["picker"])
        self.assertIn("1 done SA hidden", mix["picker"])
        for name in ("mixed_and_owner", "presence_mix"):
            for key in ("roster", "picker"):
                self.assertNotIn("live", self.out[name][key], (name, key))

    def test_the_mixed_payload_counts_its_active_seats_and_its_joined(self):
        """A positive control that the harness renders real numbers, not an
        empty page: a and b are active, and a, b and the gone c are 3 joined on
        the roster and the picker alike (the ephemeral e and the owner row are
        in neither count)."""
        roster = self._strip(self.out["mixed_and_owner"]["roster"])
        self.assertIn("2 active \u00b7 3 joined", roster)
        self.assertIn("here", self.out["mixed_and_owner"]["roster"])
        self.assertIn("3 joined seats", self.out["mixed_and_owner"]["picker"])

    def test_the_empty_payload_reads_active_and_no_joined(self):
        """A calm roster still says "0 active" and "0 joined", and the picker
        "no seats joined" — the words hold at zero."""
        roster = self._strip(self.out["empty"]["roster"])
        self.assertIn("0 active \u00b7 0 joined", roster)
        self.assertIn("no seats joined", self.out["empty"]["picker"])

    def test_the_word_is_the_real_lifted_body_not_a_stub(self):
        """A review showed a lift can pass if the function the scanner cut
        short is a stub: this proves the lifted bodies are the real ones,
        whose body strings carry the count words."""
        self.assertIn("active", _lift(self.src, "renderRoster"))
        self.assertIn("joined", _lift(self.src, "seatPicker"))
        self.assertIn("active", _lift(self.src, "dashFleet"))
        self.assertIn("active", _lift(self.src, "homeSeats"))


class SeatPickerMarkupTest(unittest.TestCase):
    """Node-free: the picker's own box names its list as the count does."""

    def test_the_picker_box_calls_its_list_joined_never_live(self):
        """The message box said "57 live seats" (finding 4); its count says
        joined now, and the box it counts for must not call the same list
        live in its placeholder or its hover."""
        src = web_ui_loader.read_text()
        box = re.search(r'<input id="seatkey"[^>]*>', src)
        self.assertIsNotNone(box)
        self.assertIn("joined seat", box.group(0))
        self.assertNotIn("live", box.group(0))

if __name__ == "__main__":
    unittest.main()
