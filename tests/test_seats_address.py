#!/usr/bin/env python3
"""helm/seats_address.py — the mention grammar and a seat's delivery scope.

TWO SURFACES READ THIS MODULE AND MUST NEVER DISAGREE: the path that WAKES a
seat, and the path that decides a row READS as addressed to it. The module's
own docstring says that is why `_mention_re` is shared; nothing pinned it, so
the sharing was a comment.

THE BUG CLASS `mentions` EXISTS TO PREVENT, stated in its docstring: a bare
`"@" + name in text` substring test matches @dariason and @daria-extra for the
name daria, spending a seat's attention on rows addressed to somebody else.
Every boundary arm below is one of those neighbours.

EVERY ROSTER READ IS EXPLICIT. `seat_scope` and `seat_names` both take the
roster as an argument, so these arms pass a literal dict and the real
`~/.helm` roster is never opened — no HELM_HOME redirect is needed because no
store path is ever resolved.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests import _tmphome               # noqa: E402,F401 — precedes helm.*
from helm import seats_address as addr    # noqa: E402
from helm.seats_common import RENAME_ALIAS_FIELD   # noqa: E402

FOREVER = "2099-01-01T00:00:00Z"
LONG_AGO = "2000-01-01T00:00:00Z"


def renamed(old, until=FOREVER, **row):
    row[RENAME_ALIAS_FIELD] = {"old": old, "until": until}
    return row


class TheMentionGrammar(unittest.TestCase):
    """Boundary-aware and case-insensitive — THE canonical answer, because a
    row that wakes a seat and a row that reads as addressed to it are decided
    by this one function."""

    def test_a_plain_mention_addresses_the_seat(self):
        """THE MUST-HIT CONTROL. Without it every negative arm below would
        pass against a predicate that answers False to everything."""
        self.assertTrue(addr.mentions("hey @alpha, look at this", "alpha"))
        self.assertTrue(addr.mentions("@alpha", "alpha"))
        self.assertTrue(addr.mentions("cc @alpha", "alpha"))

    def test_a_LONGER_name_starting_with_the_seat_is_NOT_this_seat(self):
        """The substring bug, verbatim from the docstring: @dariason is not
        daria. Waking on it spends the seat's attention on someone else's
        row, and the seat has no way to tell it was not addressed."""
        for other in ("@dariason", "@daria-extra", "@daria.two", "@daria_2",
                      "@daria9"):
            self.assertFalse(addr.mentions("hi %s" % other, "daria"),
                             "%s read as addressing daria" % other)
        # the same observable, same needle, answering True — so the five
        # Falses above are the boundary working and not a dead predicate.
        self.assertTrue(addr.mentions("hi @daria!", "daria"))

    def test_a_name_ENDING_in_the_seat_is_not_this_seat_either(self):
        """The left boundary. `(?<![A-Za-z0-9._-])@` is what stops
        `codex@alpha` and `x-@alpha` from waking alpha."""
        self.assertFalse(addr.mentions("mail codex@alpha now", "alpha"))
        self.assertFalse(addr.mentions("see build-@alpha", "alpha"))
        self.assertTrue(addr.mentions("see build @alpha", "alpha"))

    def test_the_match_is_CASE_INSENSITIVE(self):
        """The roster's spelling and a human's typing differ constantly. A
        case-sensitive door drops a real address silently."""
        self.assertTrue(addr.mentions("@ALPHA ping", "alpha"))
        self.assertTrue(addr.mentions("@alpha ping", "ALPHA"))

    def test_a_REGEX_METACHARACTER_in_a_seat_name_is_a_literal(self):
        """Seat names carry dots and dashes. An unescaped needle would make
        `a.c` match `@abc` — a seat woken by a row for a different seat, from
        a name the roster spells legally."""
        self.assertFalse(addr.mentions("@abc", "a.c"))
        self.assertTrue(addr.mentions("@a.c", "a.c"))

    def test_an_empty_text_or_an_empty_NAME_never_addresses_anyone(self):
        """An empty needle must mean NOWHERE, not everywhere: a blank seat
        name reaching this door would otherwise match the `@` in every row on
        the board and wake on all of them."""
        self.assertFalse(addr.mentions("", "alpha"))
        self.assertFalse(addr.mentions(None, "alpha"))
        self.assertFalse(addr.mentions("@alpha and @beta", ""))
        self.assertFalse(addr.mentions("@alpha and @beta", None))
        self.assertTrue(addr.mentions("@alpha and @beta", "beta"))


class TheSeatScope(unittest.TestCase):
    """One roster read per scan pass, threaded down — so the poll path stays
    about one stat per quiet room."""

    ROWS = {
        "alpha": renamed("beta", joined=True, home_room="lane",
                         mute=["Noisy Room", "other"]),
        "session-only": {"session": "sess-1"},
        "sessions-only": {"sessions": ["sess-2"]},
        "listed": {},
    }

    def test_tracked_is_true_for_joined_session_OR_sessions(self):
        """`tracked` cannot depend only on the primary cursor: the primary
        room may not have existed when an otherwise-joined seat entered. All
        three admissions are separate keys and each has to be read."""
        # UNCONDITIONAL CONTROL, ahead of the loop and on the same observable
        # the assertFalse below reads: a populated scope with tracked True.
        self.assertEqual(addr.seat_scope("alpha", self.ROWS),
                         {"home": "lane", "mute": {"noisy-room", "other"},
                          "tracked": True, "aliases": ["beta"]})
        for seat in ("alpha", "session-only", "sessions-only"):
            self.assertTrue(addr.seat_scope(seat, self.ROWS)["tracked"], seat)
        # the NEGATIVE half on the same observable: a row carrying none of
        # the three is not tracked, so `tracked` is a reading of the row and
        # not a constant.
        self.assertEqual(addr.seat_scope("listed", self.ROWS),
                         {"home": None, "mute": set(), "tracked": False,
                          "aliases": []})

    def test_the_ROSTERS_spelling_wins_over_the_callers(self):
        """The measured defect in the docstring: a case variant read here as
        an ABSENT row, so a JOINED seat answered tracked=False to delivery,
        catchup, receipts and the stop fingerprint alike — four surfaces
        wrong at once from one lookup."""
        self.assertEqual(addr.seat_scope("ALPHA", self.ROWS),
                         addr.seat_scope("alpha", self.ROWS))
        self.assertTrue(addr.seat_scope("AlPhA", self.ROWS)["tracked"])

    def test_mute_is_a_set_of_SLUGS_not_the_raw_strings(self):
        """The mute list is typed by the caller and compared against room
        slugs. Keeping the raw spelling would silently never match, so a
        muted room would keep waking the seat."""
        self.assertEqual(addr.seat_scope("alpha", self.ROWS)["mute"],
                         {"noisy-room", "other"})
        self.assertEqual(addr.seat_scope("listed", self.ROWS)["mute"], set())

    def test_an_ABSENT_seat_answers_empty_rather_than_raising(self):
        """A scan pass asks this for every name it sees, including names that
        are not seats. It has to answer, and the empty answer must be
        distinguishable from a real row — which the `alpha` control here is
        the proof of."""
        blank = addr.seat_scope("nobody", self.ROWS)
        self.assertEqual(blank, {"home": None, "mute": set(),
                                 "tracked": False, "aliases": []})
        self.assertEqual(addr.seat_scope("alpha", self.ROWS)["home"], "lane")

    def test_an_EMPTY_seat_name_reads_no_roster_at_all(self):
        """`if seat else {}` — and the alias lookup is guarded separately.
        Passing a falsey seat into the roster would ask "which row is named
        ''", and an ambiguous answer there is worse than no answer."""
        self.assertEqual(addr.seat_scope("", self.ROWS),
                         {"home": None, "mute": set(), "tracked": False,
                          "aliases": []})
        self.assertEqual(addr.seat_scope(None, self.ROWS)["aliases"], [])
        self.assertEqual(addr.seat_scope("alpha", self.ROWS)["aliases"],
                         ["beta"])


class TheNamesARowMayCarry(unittest.TestCase):
    """[seat, *live aliases] — every name a row addressed to this seat may
    be spelled with, so the per-row checks compare against a list instead of
    re-reading the roster."""

    ROWS = {"alpha": renamed("beta", joined=True),
            "expired": renamed("gamma", until=LONG_AGO, joined=True),
            "plain": {"joined": True}}

    def scope(self, seat):
        """The scope a scan pass would have computed, off the LITERAL roster
        above. Never `seat_names(seat)` with no scope — that resolves
        roster_path() and reads the operator's real ~/.helm."""
        return addr.seat_scope(seat, self.ROWS)

    def test_the_seats_own_name_is_always_first(self):
        """Order is contract: the first element is the seat's current name,
        which is what a surface prints when it names the recipient."""
        self.assertEqual(addr.seat_names("alpha", self.scope("alpha")),
                         ["alpha", "beta"])
        self.assertEqual(addr.seat_names("plain", self.scope("plain")),
                         ["plain"])

    def test_a_LIVE_rename_alias_is_carried_and_an_EXPIRED_one_is_not(self):
        """The window is the whole point of the alias: an old name answers
        for a bounded time and then stops. Carrying an expired hop forever
        would let a retired name keep waking the seat that left it, and the
        live arm beside it is what proves the empty answer is the window
        closing rather than the lookup failing."""
        self.assertEqual(addr.seat_names("alpha", self.scope("alpha")),
                         ["alpha", "beta"])
        self.assertEqual(addr.seat_names("expired", self.scope("expired")),
                         ["expired"])

    def test_a_PRECOMPUTED_scope_is_used_instead_of_a_second_roster_read(self):
        """The reason the parameter exists. A scope computed once per pass
        must be honoured verbatim — re-deriving it per row is the cost this
        signature removes, and silently ignoring it would make the whole
        threading pointless while still looking correct."""
        planted = {"aliases": ["planted-old", "second"]}
        self.assertEqual(addr.seat_names("alpha", planted),
                         ["alpha", "planted-old", "second"])
        # the control: with no scope passed, the ROSTER's own answer differs,
        # so the assertion above measures the parameter and not a coincidence.
        self.assertEqual(addr.seat_names("alpha", addr.seat_scope(
            "alpha", self.ROWS)), ["alpha", "beta"])

    def test_a_scope_with_no_aliases_yields_the_seat_alone(self):
        """A seat never renamed is the common case and must not pick up a
        None or an empty string as a second addressable name — `names_match`
        would then compare every row against a blank."""
        self.assertEqual(addr.seat_names("plain", {"aliases": None}),
                         ["plain"])
        self.assertEqual(addr.seat_names("plain", {"aliases": ["", None]}),
                         ["plain"])
        self.assertEqual(addr.seat_names("plain", {"aliases": ["real"]}),
                         ["plain", "real"])


if __name__ == "__main__":
    unittest.main()
