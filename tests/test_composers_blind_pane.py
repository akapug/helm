"""task/1906 — the census must not skip the population it exists to find.

`helm seat composers` answers "is a pane holding unsent text". On 2026-09-09 it
answered "0 held" twice, eleven minutes apart, while five panes provably held
Helm's own onboarding brief. The cause was not a wrong classification: `scan()`
guarded its read with `if p.get("last_output_at") is not None`, so it NEVER READ
a pane the metaharness had not seen output from — and that is exactly the
defective population, because a pane holding an unsent brief has never turned
and therefore has never produced output.

A guard whose false-negative rate is 100% ON ITS TARGET CLASS is worse than no
guard, because it manufactures confidence: an absent census sends you to look,
while this one answered and stopped the looking.

THE FIX PRESERVES THE PROPERTY THE OLD CODE WAS PROTECTING. `last_output_at`
remains the discriminator that keeps a blind pane out of the CLEAR bucket — it
is now consulted where an ANSWER IS ACTUALLY MISSING (an empty or unlocatable
tail) instead of before we have looked at all. Both directions are pinned
below: the blind-with-a-composer case must classify, and the blind-with-nothing
cases must still refuse.
"""

import unittest
from unittest import mock

from helm import composers, harness
# `seat` IS IMPORTED EXPLICITLY because the co-occurrence guard in
# tests/test_seat_facade_injection.py requires an accepted facade import
# beside any impl import. That requirement is the whole reason for this line.
# It asserts nothing about import order and nothing about what would happen
# without it.
from helm import seat  # noqa: F401
from helm.seat_lifecycle import _current_prompt_line

# DERIVED FROM PRODUCTION, NEVER TRANSCRIBED. If helm renames or
# extends its placeholder chrome this fixture follows; a literal
# copy would keep asserting against a string production stopped
# emitting, which is how these arms go green while reading nothing.
PLACEHOLDER = harness._COMPOSER_PLACEHOLDERS[0]


def _tail(composer_text):
    """A pane tail whose CURRENT composer holds `composer_text`.

    Shaped from a real pane read, not invented: the banner rows, the rule, the
    `❯ ` prompt, and the footer chrome are what `orca terminal read --screen`
    returns for a live Claude pane.
    """
    return "\n".join([
        " ▐▛███▛█   Claude Code v2.1.266",
        "▝▜██████▀  model · API Usage Billing",
        "  ▝▝ ▝▝    ~/wt/seat",
        "─" * 40,
        "❯ %s" % composer_text,
    ])


def _pane(handle="term_probe", last_output_at=None):
    return {"handle": handle, "title": "seat", "worktree": "/wt/seat",
            "last_output_at": last_output_at}


class TheFixtureIsProductionShapedTest(unittest.TestCase):
    """THE ARMS BELOW ARE WORTHLESS IF THE TAIL IS NOT A REAL COMPOSER.

    Every arm asserting "blind + readable composer classifies" would pass
    vacuously against a tail production's own locator cannot parse — it would
    fall through to the unlocatable branch and return CANNOT_TELL for the
    RIGHT reason while proving nothing. So the fixture is validated through the
    PRODUCTION accessor first.
    """

    def test_the_fixture_tail_holds_a_composer_production_can_find(self):
        line = _current_prompt_line(_tail("Run `helm seat boot-brief`."))
        self.assertIsNotNone(
            line, "the fixture tail is not a composer production can locate — "
                  "every arm in this module would pass for the wrong reason")
        self.assertIn("boot-brief", line)

    def test_the_placeholder_fixture_is_one_production_recognises(self):
        """Without this, the CLEAR assertion on the placeholder row could pass
        because the body simply is not a placeholder and something else made it
        clear. Validated through production's own predicate."""
        line = _current_prompt_line(_tail(PLACEHOLDER))
        self.assertIsNotNone(line, "the placeholder fixture is not locatable")
        self.assertTrue(
            harness.composer_is_placeholder(harness.composer_body(line)),
            "production does not read the placeholder fixture as placeholder "
            "chrome, so the CLEAR arm proves nothing about that branch")

    def test_the_empty_and_unlocatable_fixtures_really_are(self):  # noqa: VACUOUS_ASSERTION — the positive control is its sibling test_the_fixture_tail_holds_a_composer_production_can_find, which asserts PRESENCE through the same production accessor; the pair is what proves this locator discriminates
        self.assertIsNone(_current_prompt_line(""),
                          "the empty fixture is not empty")
        self.assertIsNone(
            _current_prompt_line("some output\nwith no prompt line at all"),
            "the unlocatable fixture accidentally contains a composer")


class ABlindPaneIsReadNotSkippedTest(unittest.TestCase):
    """THE CURE, and the two refusals it must not trade away."""

    def test_a_never_observed_pane_with_a_composer_is_classified(self):
        """THE ARM THIS ROW EXISTS FOR, PINNED TO AN EXACT STATE.

        An earlier cut asserted only `!= CANNOT_TELL`, and a review refused it:
        a blind shortcut returning CLEAR — or HELD — unconditionally passes
        that while destroying every distinction the module exists to draw. An
        assertion true of the passing case but not SPECIFIC to it proves
        nothing. So the whole blind population is pinned by exact state below,
        and this arm names the one the outage was made of.
        """
        state, body, why = composers.classify(
            _pane(last_output_at=None), _tail("Run `helm seat boot-brief`."))
        self.assertEqual(
            state, composers.HELD,
            "a never-observed pane holding an unrecorded draft must read HELD: "
            "%s" % why)
        self.assertIn("boot-brief", body or "")

    def test_the_whole_blind_population_keeps_its_exact_states(self):  # noqa: VACUOUS_ASSERTION — the flagged observable is the CLEAR row (empty composer); its unconditional positive control is the HELD row two lines above, asserted on the same call in the same table, plus HELM_PENDING/HELM_STRANDED off one predicate. Four exact states is a stronger control than the one the rung asks for, and a review conditioned this exemption on the seam and placeholder arms existing, which they now do
        """EVERY BRANCH, BLIND, BY NAME — the mutation surface a review named.

        A shortcut that answers one constant for blind panes passes any
        `!= CANNOT_TELL` arm and fails here on its first row.
        """
        exact = "Run `helm seat boot-brief`."
        fresh = {"text": exact, "key": "k", "generation": 1}

        cases = [
            ("unrecorded draft", _tail("something a human typed"), None,
             composers.HELD),
            ("empty composer", _tail(""), None, composers.CLEAR),
            # BLOCKER 2. The canonical placeholder is the shape a
            # pane shows when it holds NOTHING, and omitting it left the one
            # blind row that most resembles a held draft unpinned: it has a
            # locatable composer with a non-empty body, so only the
            # placeholder branch separates it from HELD.
            ("canonical placeholder", _tail(PLACEHOLDER), None,
             composers.CLEAR),
        ]
        for label, tail, injection, want in cases:
            state, _body, why = composers.classify(
                _pane(last_output_at=None), tail, injection=injection)
            self.assertEqual(state, want,
                             "blind pane, %s: expected %s, got %s (%s)"
                             % (label, want, state, why))

        # An EXACT injection that is not yet proven persistent, and the same
        # one once it is — two different states off one predicate, so a
        # constant-returning shortcut cannot satisfy both.
        with mock.patch("helm.resumeturn.injection_persistent",
                        return_value=False):
            state, _b, why = composers.classify(
                _pane(last_output_at=None), _tail(exact), injection=fresh)
        self.assertEqual(state, composers.HELM_PENDING,
                         "blind pane, exact non-persistent injection: %s" % why)

        with mock.patch("helm.resumeturn.injection_persistent",
                        return_value=True):
            state, _b, why = composers.classify(
                _pane(last_output_at=None), _tail(exact), injection=fresh)
        self.assertEqual(state, composers.HELM_STRANDED,
                         "blind pane, exact persistent injection: %s" % why)

    def test_a_blind_pane_whose_read_FAILED_is_cannot_tell_not_classified(self):
        """A read that was ATTEMPTED AND FAILED is not the same world as a read
        that was skipped, and neither is a read that succeeded. read_error must
        win over everything, blind or not."""
        state, _body, why = composers.classify(
            _pane(last_output_at=None), None, read_error="pane vanished")
        self.assertEqual(state, composers.CANNOT_TELL,
                         "a failed read was classified anyway")
        self.assertIn("pane read failed", why)

    def test_a_never_observed_pane_with_an_empty_tail_is_still_cannot_tell(self):
        """NEGATIVE CONTROL. `last_output_at` still keeps a blind pane out of
        the CLEAR bucket — that is the property the old code protected and the
        cure must not trade a false negative for a false positive."""
        state, _body, why = composers.classify(_pane(last_output_at=None), "")
        self.assertEqual(state, composers.CANNOT_TELL,
                         "a blind pane with an empty tail was classified")
        self.assertIn("lastOutputAt null", why)

    def test_a_never_observed_pane_with_no_locatable_composer_is_cannot_tell(self):
        """NEGATIVE CONTROL, second shape: readable output, no composer."""
        state, _body, why = composers.classify(
            _pane(last_output_at=None), "output\nwith no prompt line at all")
        self.assertEqual(state, composers.CANNOT_TELL,
                         "a blind pane with no locatable composer was classified")
        self.assertIn("lastOutputAt null", why)

    def test_an_observed_pane_with_an_empty_tail_says_so_without_blaming_orca(self):  # noqa: VACUOUS_ASSERTION — the positive control is the two blind arms above, which assert the same string IS present; asserting its absence here is what proves the wording is branch-specific rather than unconditional
        """The non-blind branch must keep its own wording — a pane orca HAS
        seen output from does not get the lastOutputAt explanation."""
        state, _body, why = composers.classify(_pane(last_output_at=123.0), "")
        self.assertEqual(state, composers.CANNOT_TELL)
        self.assertNotIn("lastOutputAt null", why)


class ScanReadsEveryPaneTest(unittest.TestCase):
    """The defect lived in scan(), not classify() — pin the read itself."""

    class _Adapter:
        def __init__(self, panes):
            self._panes = panes
            self.read_handles = []

        def list(self):
            return self._panes

        def read(self, handle, limit=None):
            self.read_handles.append(handle)
            return _tail("Run `helm seat boot-brief`.")

    def test_scan_attempts_the_read_on_a_pane_never_observed(self):
        ad = self._Adapter([_pane("term_blind", last_output_at=None),
                            _pane("term_seen", last_output_at=123.0)])
        # ONE double, and it is the real seam scan() calls. An earlier draft
        # also patched a name that does not exist on this module (create=True),
        # which asserts nothing and would tell a later reader it does.
        with mock.patch("helm.resumeturn.recorded_injections",
                        return_value={}) as recorded:
            rows, err = composers.scan(adapter=ad)
        self.assertTrue(recorded.called,
                        "the injection seam was never consulted — the double "
                        "is not in effect and this arm read the live system")
        self.assertIsNone(err)
        self.assertIn("term_blind", ad.read_handles,
                      "scan STILL skips the read on a pane orca never observed "
                      "— this is the defect itself, and every downstream "
                      "classification is decided by a read that never happened")
        self.assertIn("term_seen", ad.read_handles,
                      "control: the ordinary pane must still be read")
        blind = [r for r in rows if r["handle"] == "term_blind"]
        self.assertTrue(blind, "the blind pane produced no row at all")
        # BLOCKER 1, and it was right that I fixed this class in
        # the direct classify arms and left it standing in the SEAM arm. The
        # earlier `!= CANNOT_TELL` is passed by a scan-level regression that
        # rewrites a successfully-read blind draft to CLEAR — which is the
        # original outage's exact shape ("0 held" while panes held briefs),
        # reached through scan() rather than through classify().
        self.assertEqual(
            blind[0]["state"], composers.HELD,
            "the blind pane holds an unrecorded draft and scan() did not "
            "report it HELD: %s" % blind[0]["why"])
        seen = [r for r in rows if r["handle"] == "term_seen"]
        self.assertTrue(seen, "the ordinary pane produced no row at all")
        self.assertEqual(
            seen[0]["state"], composers.HELD,
            "CONTROL: the ordinary pane reads the SAME tail, so a blind-only "
            "HELD would prove the blind branch and nothing about scan(): %s"
            % seen[0]["why"])


if __name__ == "__main__":
    unittest.main()
