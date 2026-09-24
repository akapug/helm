#!/usr/bin/env python3
"""The roster console's listedness badge, EXECUTED rather than grepped.

The claims footer on the owner's roster tab renders liveness (STALE/UNKNOWN)
and, until this lane, said nothing about LISTEDNESS — so it printed holders the
seat table above it did not list, which is the contradiction this lane is named
for, surviving on the surface the owner actually looks at.

WHY THE MARK IS A TOP-LEVEL FUNCTION AND NOT A CONST INSIDE renderRoster: a
decision buried in a paint function can only be checked by grepping the source
for its spelling, and the sibling roster test says so in its own words — it
lifts renderRoster "for the WIRED check only, never executed". A census pinned
to spelling keeps passing while the behaviour rots. Pulled out, the judgment is
a pure function of one claim, so it runs here for real. Its Python twin makes
the same argument: claim_unlisted_mark is deliberately separate from its
renderer so the mark is ONE judgment.

THE WORDS ARE ASSERTED, NOT JUST THE PRESENCE OF A BADGE. They are the CLI's
verbatim vocabulary, and the point of this lane is that two surfaces describing
one fact must not describe it two ways. Requires node; skipped where absent.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helm import web_ui_loader  # noqa: E402
from tests.test_web_chat_client_runtime import _extract_fn  # noqa: E402

ARMS = r"""
const out = [];
const mark = state => rosterListedMark({resource: "r", holder: "h", listedness: state});
// THE OLD SERVER'S ENVELOPE, not a hand-made state string: the previous
// generation returned a perfectly valid roster payload whose claim rows simply
// had no `listedness` key. Mapping the real mark over the real shape is what
// the direct state calls above cannot catch.
const OLD_ENVELOPE = {room: "main", seats: [{seat: "a"}], claims: [
  {resource: "worktree:helm:lane-a", holder: "alice", remaining: 900, liveness: "fresh"},
  {resource: "worktree:helm:lane-b", holder: "bob", remaining: 60, liveness: "stale"},
]};
console.log(JSON.stringify({
  unlisted: mark("unlisted"),
  unknown: mark("unknown"),
  unmeasurable: mark("unmeasurable"),
  listed: mark("listed"),
  absent_field: rosterListedMark({resource: "r"}),
  no_claim_at_all: rosterListedMark(null),
  // PRESENT-BUT-UNINTERPRETABLE, the three shapes a reviewer named: the server
  // ANSWERED and this browser cannot read the answer. None of them may claim
  // the field was absent.
  present_null: rosterListedMark({resource: "r", listedness: null}),
  present_number: rosterListedMark({resource: "r", listedness: 7}),
  present_future: rosterListedMark({resource: "r", listedness: "future-state"}),
  old_envelope_marks: OLD_ENVELOPE.claims.map(c => rosterListedMark(c)),
}));
"""

# The CLI's own sentences (seats_claims.claim_unlisted_mark). Asserted here so
# the two surfaces cannot drift into two vocabularies for one fact — which is
# the drift that makes a reader believe they are two different findings.
CLI_WORDS = {"unlisted": "NO SEAT ROW",
             "unknown": "ROSTER UNREADABLE",
             "unmeasurable": "HOLDER UNREADABLE"}

# The three shapes where the server DID report and this page cannot read the
# answer. They must NOT borrow the absent-key wording.
PRESENT_UNREADABLE = ("present_null", "present_number", "present_future")


class TestRosterListedMark(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        fn = _extract_fn(web_ui_loader.read_text(), "rosterListedMark")
        cls.tmp = tempfile.mkdtemp(prefix="helm-listedmark-")
        path = os.path.join(cls.tmp, "run.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(fn + "\n" + ARMS)
        chk = subprocess.run([cls.node, "--check", path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        cls.proc = subprocess.run([cls.node, path], capture_output=True,
                                  text=True, timeout=60)
        try:
            cls.got = json.loads(cls.proc.stdout or "{}")
        except json.JSONDecodeError:
            cls.got = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def setUp(self):
        # A DRIVER THAT DIED PRODUCES {} AND EVERY `assertNotIn` BELOW WOULD
        # PASS ON IT. The run is checked once, here, so an empty result set is
        # a failure rather than a clean sweep of vacuous negatives.
        self.assertTrue(self.got, "the node driver produced nothing — stdout=%r "
                                  "stderr=%r" % (self.proc.stdout[:300],
                                                 self.proc.stderr[:300]))

    def test_each_unhealthy_state_renders_the_CLI_S_OWN_WORDS(self):
        for state, words in CLI_WORDS.items():
            self.assertIn(state, self.got, "arm did not run for %r" % state)
            self.assertIn(words, self.got[state],
                          "the console says something other than the CLI for "
                          "%r — one fact, two vocabularies" % state)

    def test_a_LISTED_holder_gets_no_badge(self):
        """The pole. A marker that fires on every state carries no information,
        and this one shares a line with the liveness badge — a second
        unconditional word there is noise on the row the owner scans."""
        self.assertEqual(self.got.get("listed"), "",
                         "a healthy holder was badged")
        for words in CLI_WORDS.values():
            self.assertNotIn(words, self.got.get("listed") or "")

    def test_an_ABSENT_field_is_NOT_REPORTED_and_never_a_silent_all_clear(self):
        """THIS ARM ASSERTED THE OPPOSITE AND A REVIEWER OVERTURNED IT.

        It required an absent `listedness` to render NOTHING, reasoning that
        printing nothing beats printing a reassuring word. That is wrong HERE
        because this surface communicates health BY THE ABSENCE OF A WARNING —
        the healthy `listed` case is also exactly empty. So "" is not neutral,
        it IS the all-clear, and my arm was pinning a false one.

        The case is a half-live deploy: a new browser polling the PREVIOUS
        server generation gets a valid payload whose claim rows carry no
        listedness, and every unlisted holder then drew exactly like a
        measured-healthy one — this lane's own ambiguity, back at the version
        boundary."""
        self.assertIn("NOT REPORTED", self.got.get("absent_field") or "",
                      "an absent key rendered a SILENT row for a field nobody "
                      "measured — identical to a measured-listed holder")
        # A NULL CLAIM IS A THIRD THING AND NO LONGER SHARES THIS BADGE. It is
        # not an old server that omitted a field; it is a row that could not be
        # read at all, so it says CLAIM UNREADABLE. This assertion USED to
        # demand NOT REPORTED here, and keeping it would have re-collapsed two
        # facts the structural branch had just separated.
        self.assertIn("CLAIM UNREADABLE", self.got.get("no_claim_at_all") or "",
                      "an unreadable claim row borrowed the absent-key wording")
        self.assertNotIn("NOT REPORTED", self.got.get("no_claim_at_all") or "",
                         "a missing ROW was reported as a missing FIELD")
        self.assertEqual(self.got.get("listed"), "",
                         "the pole moved: `listed` must remain the ONLY "
                         "no-badge state, or the badge means nothing")

    def test_a_PRESENT_but_uninterpretable_value_is_NOT_called_ABSENT(self):
        """A REVIEWER'S SECOND ROUND ON THIS FUNCTION, and the cure for the
        cure. The previous version routed every unknown value to the
        absent-key badge, so {listedness: null}, {listedness: 7} and a future
        enum all told the owner the server had not reported — while the server
        HAD reported and this page simply could not read it.

        That is the same false claim as the silent all-clear, aimed the other
        way, and it breaks the MIRROR version boundary: an older browser
        meeting a NEWER server must not announce absence."""
        for key in PRESENT_UNREADABLE:
            got = self.got.get(key) or ""
            self.assertIn("UNRECOGNIZED", got,
                          "%s did not render the present-but-unreadable "
                          "state" % key)
            self.assertNotIn("NOT REPORTED", got,
                             "%s claimed the server did not report a field it "
                             "DID report" % key)

    def test_the_OLD_SERVERS_ENVELOPE_badges_every_row(self):
        """The compatibility arm the direct state calls could not provide.

        Not a hand-made state string — a whole valid roster payload of the
        PREVIOUS shape, mapped through the real mark. Every row must be badged;
        a single silent row here is a holder the owner would read as healthy on
        evidence that was never sent."""
        marks = self.got.get("old_envelope_marks")
        self.assertTrue(marks, "the old-envelope arm did not run")
        for i, m in enumerate(marks):
            self.assertIn("NOT REPORTED", m or "",
                          "row %d of an old-shape envelope rendered silently"
                          % i)


if __name__ == "__main__":
    unittest.main()
