"""The instruction that arms an inbox beacon has ONE home, and no place that
tells a seat how to arm one may say anything else.

The harness ends every Monitor at a deadline it caps, and a re-arm mints a new
ID. An instruction that promises a watch which never ends leaves a seat deaf
the first time the deadline passes, and a seat that wrote the ID down hunts
for a process that no longer exists.
"""
import os
import unittest

from helm import seat, seats_advice

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RETIRED = "persistent: " + "true"   # split so this file is not its own hit
SCANNED = (".py", ".md", ".part", ".txt", ".json", ".sh", "")


def shipped_text():
    """{relative path: text} for everything a seat or an operator reads."""
    out = {}
    for top in ("helm", "docs", "bin"):
        for base, _dirs, names in os.walk(os.path.join(ROOT, top)):
            if "__pycache__" in base:
                continue
            for name in names:
                if os.path.splitext(name)[1] not in SCANNED:
                    continue
                path = os.path.join(base, name)
                try:
                    with open(path, encoding="utf-8") as f:
                        out[os.path.relpath(path, ROOT)] = f.read()
                except (OSError, UnicodeDecodeError):
                    continue
    return out


class TheArmingInstructionHasOneHome(unittest.TestCase):

    def test_the_call_carries_the_deadline_the_harness_enforces(self):
        call = seats_advice.beacon_monitor("zed")
        self.assertEqual(
            call, 'Monitor(command: "helm chat wait --seat zed --follow", '
                  'timeout_ms: 1800000)')
        self.assertIn("--follow --replace\"",
                      seats_advice.beacon_monitor("zed", replace=True))
        self.assertNotIn("persistent", call)

    def test_a_placeholder_seat_survives_a_later_format(self):
        """Callers embed the call in their own templates; a stray percent sign
        in it would raise at the caller, far from here."""
        self.assertEqual(seats_advice.beacon_monitor("%(s)s") % {"s": "zed"},
                         seats_advice.beacon_monitor("zed"))
        self.assertEqual(seats_advice.beacon_monitor("%s") % "zed",
                         seats_advice.beacon_monitor("zed"))
        for sentence in (seats_advice.BEACON_EXPIRY,
                         seats_advice.BEACON_EXPIRY_TERSE):
            self.assertNotIn("%", sentence)

    def test_the_expiry_rule_says_the_three_things_a_seat_needs(self):
        for sentence in (seats_advice.BEACON_EXPIRY,
                         seats_advice.BEACON_EXPIRY_TERSE):
            with self.subTest(sentence=sentence):
                low = sentence.lower()
                self.assertIn("30 minutes", low)
                self.assertIn("same turn", low)
                self.assertIn("id", low.split())

    def test_no_shipped_file_promises_a_watch_that_never_ends(self):
        shipped = shipped_text()
        # must-hits: the corpus is real, and it contains the homes we expect
        self.assertGreater(len(shipped), 100)
        self.assertIn(os.path.join("helm", "seats_advice.py"), shipped)
        self.assertIn(os.path.join("docs", "NEW_AGENT_GUIDE.md"), shipped)
        self.assertEqual(sorted(p for p, t in shipped.items() if RETIRED in t), [])

    def test_the_docs_say_the_producers_words(self):
        shipped = shipped_text()
        call = seats_advice.beacon_monitor("<you>")
        guide = shipped[os.path.join("docs", "NEW_AGENT_GUIDE.md")]
        self.assertIn(call, guide)
        flat = " ".join(guide.split())
        self.assertIn(seats_advice.BEACON_EXPIRY, flat)

    def test_the_prompts_a_seat_is_launched_with_render_the_producer(self):
        call = seats_advice.beacon_monitor("zed")
        for text in (seat.rearm_prompt("zed"),
                     seat.rearm_prompt("zed", role="lead")):
            self.assertIn(call, text)
            self.assertIn(seats_advice.BEACON_EXPIRY, text)

    def test_every_module_that_prints_the_call_takes_it_from_the_producer(self):
        """A literal `Monitor(command:` outside the producer is a second home.
        The producer's own module is the one allowed hit, which is also the
        must-hit that proves the scan can see the pattern."""
        hits = sorted(p for p, t in shipped_text().items()
                      if p.startswith("helm" + os.sep) and "Monitor(command:" in t)
        self.assertEqual(hits, [os.path.join("helm", "seats_advice.py")])


if __name__ == "__main__":
    unittest.main()
