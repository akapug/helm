"""The roster's USABILITY JOIN — `helm seat list` answering "can this seat
take work right now", not just "is its proxy up".

THE TWO LIVE CASES THESE FIXTURES ARE BUILT FROM, measured 2026-08-11 when the
owner asked "why did we build the roster if we weren't going to actually use
it?" — two seats on the live fleet, both rendered by the roster as healthy:

    the primary proxy seat   "proxy UP ... 239h06m left"   42 HOURS DARK
    its fourth instance      "proxy UP pid ... port ..."   PANE GONE

Fixtures use the house convention (seat-a, seat-b, seat-d, seat-native), never
a live identity: tests/ is public-bound.

Every reader is INJECTED here — the join takes health/upstream/
open_recipients/register/live_seats/canonical seams precisely so the three
input surfaces can be fixtured independently and no test needs a live fleet, a
proxy, a process census, or a real ledger on disk. The register seam is named
`register`, not `roster`: a parameter named for the accessor makes its own call
site read as a roster read to the AST count-pin, which caught it.

EVERY ASSERTION IS ON ONE SEAT'S ROW. Never on the whole rendered screen: the
surface carries a legend that names USABLE, DEGRADED, UNUSABLE, UNKNOWN and
the word "holding", so a screen-wide `in` check passes on the legend alone
while every row is blank.
"""
import time
import unittest
from unittest import mock

from helm import seat_usability


# ---------------------------------------------------------------------------
# fixture builders — one per input surface
# ---------------------------------------------------------------------------

def _health(rows):
    """A stand-in for proxywatch.health: records the kwargs it was called with
    so a test can prove the DISPLAY asked for no probe and no canary."""
    seen = {}

    def fn(**kwargs):
        seen.update(kwargs)
        return {"ts": 0, "seats": list(rows)}
    fn.seen = seen
    return fn


def _hrow(seat, **over):
    """One proxywatch health row. Defaults are the HEALTHY shape, so every
    test below changes exactly the one field it is about."""
    row = {"seat": seat, "family": "codex", "turn_state": "ok",
           "turn_evidence": None, "transcript_age_s": 60.0,
           "pane_live": True, "census_blind": None, "error": None}
    row.update(over)
    return row


def _upstream(records=None, err=None):
    return lambda: (records if err is None else None, err)


def _ledger(counts=None, unavailable=None):
    return lambda: (counts if unavailable is None else None, unavailable)


def _roster(reg=None, boom=None):
    def fn():
        if boom:
            raise OSError(boom)
        return dict(reg or {})
    return fn


_IDENTITY = lambda name: (name, None)          # noqa: E731 — a test seam

_HEALTHY_UP = {"codex": {"state": "HEALTHY", "dark": False}}
_REGISTERED = {"seat-a": {"runtime_verified": True},
               "seat-d": {"runtime_verified": True}}


def _panes(names=(), blind=None, per_seat=None):
    """The `_live_seats` shape: names, HOST-WIDE blindness, and the refusals
    that belong to one seat each."""
    return lambda: (set(names), blind, dict(per_seat or {}))


def _join(rows, upstream=None, ledger=None, reg=None, roster_boom=None,
          seats=None, panes=()):
    # `upstream` takes a RECORDS DICT or an already-built reader. Wrapping a
    # reader in _upstream() again produced (<function>, None) — a real failure
    # this file caught on itself: the row then said "not a family map" instead
    # of the injected reason, so the test measured the wrapper, not the join.
    reader = upstream if callable(upstream) else \
        _upstream(_HEALTHY_UP if upstream is None else upstream)
    return seat_usability.join(
        seats=seats,
        health=_health(rows),
        upstream=reader,
        open_recipients=_ledger({} if ledger is None else ledger),
        register=_roster(_REGISTERED if reg is None else reg, boom=roster_boom),
        canonical=_IDENTITY, live_seats=_panes(panes))


def _line(seat, rows, **kw):
    return seat_usability.line(seat, _join(rows, **kw))


# ---------------------------------------------------------------------------


seat_usability_DEAF = "DEAF"   # beacons' proven-no-wake-path verdict, as it lands on the roster row


def _att(state, age_s=0.0, why=None):
    """One roster row carrying an attendance verdict `age_s` seconds old."""
    return {"runtime_verified": True,
            "attendance": {"state": state, "at": time.time() - age_s,
                           "why": why}}


class AHealthySeatHelmCannotWakeIsUNUSABLE(unittest.TestCase):
    """REACHABILITY IS NOT HEALTH, and before this rung nothing asked.

    A seat can answer USABLE on every other rung — pane live, upstream
    healthy, turn ok, runtime verified — while holding work it can never be
    woken to read. Under the native-wake-only law a seat with no armed beacon
    is a mailbox nobody opens, and health cannot say so.
    """

    def test_a_live_seat_with_no_beacon_cannot_take_work(self):  # noqa: VACUOUS_ASSERTION — test_a_covered_seat_is_untouched is the unconditional positive control on this same verdict: a reachable seat must read USABLE, so a rung that refused everything fails there
        """Kills the whole rung: delete it and this seat reads USABLE, the
        verdict that routes work to a seat nothing can wake."""
        rows = _join([_hrow("seat-a")], panes=("seat-a",),
                     reg={"seat-a": _att(seat_usability_DEAF)})
        row = rows["seat-a"]
        self.assertEqual(row["verdict"], seat_usability.UNUSABLE)
        self.assertIs(row["can_take_work"], False)
        self.assertIs(row["reachable"], False)
        self.assertIn("wake", row["reason"])
        # THE REPAIR IS NAMED, because a refusal an operator cannot act on is
        # the bug class this module's pane rung already avoids.
        self.assertIn("helm chat wait --seat seat-a --follow", row["reason"])

    def test_a_covered_seat_is_untouched(self):  # noqa: VACUOUS_ASSERTION — asserts USABLE and reachable True, a positive verdict rather than an absence
        """THE FALSE-ALARM DIRECTION, which is the one that would teach every
        reader to skim the verdict column. A proven-reachable seat keeps the
        answer it had before this rung existed."""
        rows = _join([_hrow("seat-a")], panes=("seat-a",),
                     reg={"seat-a": _att("covered")})
        self.assertEqual(rows["seat-a"]["verdict"], seat_usability.USABLE)
        self.assertIs(rows["seat-a"]["reachable"], True)
        self.assertEqual(rows["seat-a"]["reason"], "")

    def test_no_attendance_row_never_refuses(self):
        """Kills refusing on ABSENCE. The register does not MINT roster rows,
        so a seat nobody enrolled has no verdict — and on a box where the
        beacons timer has never run, no seat has one. Such a box must still
        route work."""
        rows = _join([_hrow("seat-a")], panes=("seat-a",),
                     reg={"seat-a": {"runtime_verified": True}})
        self.assertEqual(rows["seat-a"]["verdict"], seat_usability.USABLE)
        self.assertIsNone(rows["seat-a"]["reachable"])

    def test_unproven_and_vacant_are_announced_not_refused(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone rides WITH an assertEqual on the verdict in the same call: the row must read USABLE, a positive observable, so a rung that refused on an unproven verdict fails here regardless of the None
        """UNPROVEN is the census saying it could not tell; VACANT is a PROVEN
        LIVE wake path with an empty house. Neither is a proven absence of a
        wake path, and refusing on either would brick routing wherever the
        census is degraded or a seat is merely between occupants."""
        for state in ("UNPROVEN", "VACANT"):
            with self.subTest(state=state):
                rows = _join([_hrow("seat-a")], panes=("seat-a",),
                             reg={"seat-a": _att(state)})
                row = rows["seat-a"]
                self.assertEqual(row["verdict"], seat_usability.USABLE)
                self.assertIsNone(row["reachable"])
                self.assertIn(state, row["reachable_why"])

    def test_a_stale_verdict_stops_gating(self):
        """Kills a frozen DEAF outliving its writer. The register is written
        by a timer, and a timer can be stopped or never wired — so a verdict
        that kept gating after its writer stopped would hold a seat at
        UNUSABLE long after it re-armed, with nothing reporting why."""
        rows = _join([_hrow("seat-a")], panes=("seat-a",),
                     reg={"seat-a": _att(seat_usability_DEAF, age_s=86400)})
        row = rows["seat-a"]
        self.assertEqual(row["verdict"], seat_usability.USABLE)
        self.assertIsNone(row["reachable"])
        self.assertIn("stale", row["reachable_why"])

    def test_a_gone_pane_keeps_its_own_prescription(self):
        """ORDERING, asserted rather than assumed: a seat whose pane is GONE
        is unreachable too, and `helm seat spawn` re-arms the beacon as part
        of restoring the pane. The pane rung must still win, or the operator
        is handed the narrower repair for the wider fault."""
        rows = _join([_hrow("seat-a", pane_live=False)],
                     reg={"seat-a": _att(seat_usability_DEAF)})
        row = rows["seat-a"]
        self.assertEqual(row["verdict"], seat_usability.UNUSABLE)
        self.assertIn("pane GONE", row["reason"])
        self.assertNotIn("chat wait", row["reason"])


class TheStaleSemanticAgeCase(unittest.TestCase):
    """A seat whose PROXY is perfect and whose last completed turn was 42 hours
    ago. The roster said "proxy UP ... 239h06m left" and nothing else."""

    def test_a_stale_semantic_age_renders_DEGRADED_and_NAMES_the_age(self):
        got = _line("seat-a", [_hrow("seat-a", turn_state="idle",
                                    turn_evidence="out of work, not stuck",
                                    transcript_age_s=42 * 3600 + 13 * 60)])
        self.assertTrue(" DEGRADED " in got, got)
        self.assertTrue("last=42h13m" in got, got)
        self.assertTrue("no completed turn in 42h13m" in got, got)

    def test_CONTROL_the_same_row_with_a_fresh_turn_renders_USABLE(self):
        """UNCONDITIONAL CONTROL on the SAME observable and the same renderer:
        one field changes, so the DEGRADED above measures the age and not a
        renderer that says DEGRADED to everything."""
        got = _line("seat-a", [_hrow("seat-a")])
        self.assertTrue(" USABLE " in got, got)
        self.assertTrue("last=0h01m" in got, got)
        self.assertEqual(got.split(" - ")[0], got)      # no reason clause


class TheGonePaneCase(unittest.TestCase):
    """The instance that rendered "proxy UP pid ... port ..." with no pane
    behind it. A proxy is a listening socket; it is not a seat."""

    def test_a_GONE_pane_is_UNUSABLE_and_the_row_SAYS_pane_GONE(self):
        got = _line("seat-d", [_hrow("seat-d", pane_live=False,
                                      turn_state="off")])
        self.assertTrue(" UNUSABLE " in got, got)
        self.assertTrue("pane=GONE" in got, got)
        self.assertTrue("pane GONE" in got, got)
        self.assertTrue("helm seat spawn seat-d" in got, got)

    def test_CONTROL_the_same_row_with_a_live_pane_is_not_unusable(self):
        got = _line("seat-d", [_hrow("seat-d")])
        self.assertTrue(" USABLE " in got, got)
        self.assertTrue("pane=live" in got, got)

    def test_a_BLIND_census_is_UNKNOWN_and_never_GONE(self):
        """"no readable process names this seat" and "helm could not read every
        process on this host" are different facts. Only the first earns GONE —
        a positive claim from a census that could not look is the founding
        defect pointing the other way."""
        got = _line("seat-a", [_hrow("seat-a", pane_live=None,
                                    census_blind="ps refused")])
        self.assertTrue("pane=UNKNOWN" in got, got)
        self.assertTrue(" UNKNOWN " in got, got)
        self.assertTrue("ps refused" in got, got)


class TheWall(unittest.TestCase):
    """The provider refusing the family. proxywatch has classified this for a
    long time and the roster never asked."""

    def test_a_walled_family_is_UNUSABLE_and_the_row_NAMES_the_wall(self):
        got = _line("seat-a", [_hrow("seat-a")],
                    upstream={"codex": {"state": "AUTH-UNAVAILABLE",
                                        "dark": True,
                                        "since": "2026-08-11T09:30:05Z"}})
        self.assertTrue(" UNUSABLE " in got, got)
        self.assertTrue("upstream=AUTH-UNAVAILABLE" in got, got)
        self.assertTrue("since 2026-08-11T09:30:05Z" in got, got)

    def test_a_dark_STATE_walls_even_when_the_dark_flag_is_missing(self):
        """The record's `state` is the authority proxywatch's own _UPSTREAM_DARK
        set names; a missing `dark` bool must not read as healthy."""
        got = _line("seat-a", [_hrow("seat-a")],
                    upstream={"codex": {"state": "QUOTA-402"}})
        self.assertTrue(" UNUSABLE " in got, got)
        self.assertTrue("upstream=QUOTA-402" in got, got)

    def test_family_mixed_uses_validated_dark_boolean_not_state_membership(self):
        wall = _line("seat-a", [_hrow("seat-a")], upstream={"codex": {
            "state": "FAMILY-MIXED", "dark": True, "seats": {
                "seat-a": {"state": "AUTH-401", "dark": True},
                "seat-b": {"state": "QUOTA-402", "dark": True}}}})
        split = _line("seat-a", [_hrow("seat-a")], upstream={"codex": {
            "state": "UNKNOWN", "dark": False, "seats": {
                "seat-a": {"state": "PROXY-COOLDOWN", "dark": True},
                "seat-b": {"state": "HEALTHY", "dark": False}}}})
        self.assertTrue(" UNUSABLE " in wall, wall)
        self.assertTrue("upstream=FAMILY-MIXED" in wall, wall)
        self.assertTrue(" USABLE " in split, split)
        self.assertTrue("upstream=UNKNOWN" in split, split)
        self.assertFalse(" UNUSABLE " in split, split)

    def test_CONTROL_a_healthy_family_on_the_same_fixture_is_USABLE(self):
        got = _line("seat-a", [_hrow("seat-a")])
        self.assertTrue(" USABLE " in got, got)
        self.assertTrue("upstream=HEALTHY" in got, got)


class UnreadableInputsRenderUNKNOWNPerField(unittest.TestCase):
    """helm's law, and the whole reason this join is worth having: the surface
    it extends was not wrong, it was CONFIDENT."""

    def test_CONTROL_every_reader_healthy_leaves_no_UNKNOWN_on_the_row(self):
        """UNCONDITIONAL FIRST. Each test below asserts UNKNOWN appears; this
        one proves the row CAN come out without it, so those are measuring the
        broken reader rather than a renderer that always says UNKNOWN."""
        got = _line("seat-a", [_hrow("seat-a")])
        # POSITIVE FIRST: the row rendered at all, and rendered every field.
        # Without this, an empty string satisfies the absence below.
        self.assertTrue("turn=ok" in got, got)
        self.assertTrue("pane=live" in got, got)
        self.assertTrue("holding=0" in got, got)
        self.assertFalse("UNKNOWN" in got, got)

    def test_an_unreadable_proxywatch_makes_turn_last_and_pane_UNKNOWN(self):
        def boom(**_kw):
            raise OSError("proxywatch state unreadable")
        rows = seat_usability.join(
            seats=["seat-a"], health=boom, upstream=_upstream(_HEALTHY_UP),
            open_recipients=_ledger({}), register=_roster(_REGISTERED),
            canonical=_IDENTITY, live_seats=_panes(["seat-a"]))
        got = seat_usability.line("seat-a", rows)
        self.assertTrue("turn=UNKNOWN" in got, got)
        self.assertTrue("last=UNKNOWN" in got, got)
        self.assertTrue("pane=UNKNOWN" in got, got)
        self.assertTrue(" UNKNOWN " in got, got)
        self.assertTrue("proxywatch state unreadable" in got, got)

    def test_an_unreadable_ledger_makes_holding_UNKNOWN_and_NOT_zero(self):
        """A zero would say "this seat is free", which is the same false
        confidence one field down."""
        rows = seat_usability.join(
            seats=["seat-a"], health=_health([_hrow("seat-a")]),
            upstream=_upstream(_HEALTHY_UP),
            open_recipients=_ledger(None, unavailable="ledger line 4 is torn"),
            register=_roster(_REGISTERED), canonical=_IDENTITY,
            live_seats=_panes(["seat-a"]))
        got = seat_usability.line("seat-a", rows)
        self.assertTrue("holding=UNKNOWN" in got, got)
        self.assertFalse("holding=0" in got, got)
        self.assertTrue("ledger line 4 is torn" in got, got)
        self.assertTrue(" UNKNOWN " in got, got)

    def test_an_unreadable_upstream_cache_is_UNKNOWN_and_NOT_healthy(self):
        got = _line("seat-a", [_hrow("seat-a")],
                    upstream=_upstream(None, err="proxywatch state is 90m old"))
        self.assertTrue("upstream=UNKNOWN" in got, got)
        self.assertFalse("upstream=HEALTHY" in got, got)
        self.assertTrue("proxywatch state is 90m old" in got, got)

    def test_a_family_with_no_recorded_verdict_is_UNKNOWN_not_healthy(self):
        got = _line("seat-a", [_hrow("seat-a")], upstream={})
        self.assertTrue("upstream=UNKNOWN" in got, got)
        self.assertTrue("no verdict for family codex" in got, got)

    def test_an_unreadable_register_makes_runtime_UNKNOWN_and_says_why(self):
        """The register does NOT gate the verdict (see `_seat_row`), so this
        also pins that a soft note never silently upgrades to a verdict."""
        got = _line("seat-a", [_hrow("seat-a")], roster_boom="EACCES on roster")
        self.assertTrue("runtime=UNKNOWN" in got, got)
        self.assertTrue("EACCES on roster" in got, got)
        self.assertTrue(" USABLE " in got, got)

    def test_UNVERIFIED_is_a_MEASUREMENT_and_never_the_fallback(self):
        """Shipped the other way for an hour, in the module whose whole subject
        is this failure: the join-raised fallback row rendered five honest
        UNKNOWNs and `runtime=UNVERIFIED` — a confident claim about a register
        nobody read. UNVERIFIED now needs a register row that WAS read."""
        # CONTROL FIRST, same field: a READ row that is not verified DOES say
        # UNVERIFIED, so the UNKNOWN below is the unread case and not a dead
        # branch that never emits the word.
        read_but_unverified = _line(
            "seat-a", [_hrow("seat-a")],
            reg={"seat-a": {"runtime_verified": False}})
        self.assertTrue("runtime=UNVERIFIED" in read_but_unverified,
                        read_but_unverified)
        self.assertTrue("runtime=UNKNOWN" in seat_usability.line("seat-a", {}),
                        seat_usability.line("seat-a", {}))

    def test_a_seat_with_no_register_row_is_UNREGISTERED_not_unverified(self):
        got = _line("seat-a", [_hrow("seat-a")], reg={})
        self.assertTrue("runtime=UNREGISTERED" in got, got)

    def test_an_EMPTY_join_map_still_renders_every_field_as_UNKNOWN(self):
        """The shape `_status` falls back to when the join itself raises: a
        roster still prints, and every field says UNKNOWN rather than blank."""
        got = seat_usability.line("seat-a", {})
        # EVERY field, runtime included — the one that shipped as a confident
        # UNVERIFIED here is exactly why this enumerates rather than samples.
        for field in ("turn=UNKNOWN", "last=UNKNOWN", "pane=UNKNOWN",
                      "upstream=UNKNOWN", "holding=UNKNOWN",
                      "runtime=UNKNOWN"):
            self.assertTrue(field in got, "%s missing from %r" % (field, got))
        self.assertTrue(" UNKNOWN " in got, got)


class ADarkVerdictNamesTheStateNotACulprit(unittest.TestCase):
    """task/1903: eleven dark states rendered ONE sentence blaming the provider.

    ROUND ONE of this cure split them three ways and STILL assigned causes to
    two buckets. Review showed that was the same defect one level down: EMPTY200/MALFORMED200 prove only that HELM'S OWN
    CANARY answered 200 and OUR validation failed — not that the provider
    answered — and "the provider is refusing" is false for AUTH-UNAVAILABLE, a
    client-side TIMEOUT-500, an unknown-origin RATE-LIMITED, the FAMILY-MIXED
    aggregate and a latched UNKNOWN.

    The rule is therefore: NEUTRAL MEASURED-STATE WORDING FOR EVERY
    UNTYPED-ORIGIN STATE, local wording only where the token encodes the origin.
    """

    def test_our_validation_states_name_the_measurement_not_an_origin(self):
        for state in ("MALFORMED200", "EMPTY200"):
            why = seat_usability._dark_reason(state, "2026-09-04T03:26:54Z")
            self.assertIn(state, why)
            self.assertIn("helm's canary endpoint answered HTTP 200", why)
            self.assertIn("HELM'S OWN validation rejected", why)
            # it must claim NEITHER an origin NOR a remedy
            self.assertNotIn("the provider is refusing", why)
            self.assertNotIn("the provider answered", why)
            self.assertNotIn("will not change it", why)

    def test_the_one_state_that_encodes_its_origin_keeps_local_wording(self):
        why = seat_usability._dark_reason("PROXY-COOLDOWN", "2026-09-04T03:26:54Z")
        self.assertIn("HELM'S OWN PROXY", why)
        self.assertIn("before any request left the box", why)
        # AND IT MUST NOT LEAD WITH THE WORD ITS TAIL DENIES. A lead of
        # "upstream PROXY-COOLDOWN" over a tail naming helm's own proxy is a
        # claim of origin beside its own contradiction, and a reader skimming
        # for whether a provider is down reads the lead. The must-miss is
        # test_every_untyped_origin_state_asserts_no_cause, where the same
        # word is REQUIRED because there it is the honest report.
        self.assertFalse(why.startswith("upstream "), why)
        # AND THE CORRECTION DOES NOT OVERSHOOT: naming the refusal ours must
        # not be read as clearing the provider, so the sentence says outright
        # that it measures nothing about it.
        self.assertIn("measures NOTHING about the provider", why)

    def test_every_untyped_origin_state_asserts_no_cause(self):
        """The states a review named, and the generic ones. NONE may blame the
        provider, because the recorded state does not encode where the failure
        came from."""
        for state in ("AUTH-UNAVAILABLE", "TIMEOUT-500", "RATE-LIMITED",
                      "UPSTREAM-4XX", "UPSTREAM-5XX", "UPSTREAM-OVERLOADED",
                      "AUTH-401", "QUOTA-402", "FAMILY-MIXED", "UNKNOWN"):
            why = seat_usability._dark_reason(state, "2026-09-04T03:26:54Z")
            self.assertIn(state, why, "reason for %r does not name it" % state)
            self.assertIn("does not encode WHERE the failure originated", why)
            self.assertNotIn("the provider is refusing", why)

    def test_the_three_classes_stay_DISTINGUISHABLE(self):  # noqa: VACUOUS_ASSERTION — every assertion here is an inequality between non-empty rendered strings; there is no absence assertion in this arm
        """THE MUST-MISS FOR THIS CURE. The cheapest wrong fix is to make every
        state render the SAME neutral sentence, which passes every arm above
        and destroys the only two distinctions that carry information."""
        ours = seat_usability._dark_reason("PROXY-COOLDOWN", "T")
        val = seat_usability._dark_reason("MALFORMED200", "T")
        other = seat_usability._dark_reason("RATE-LIMITED", "T")
        self.assertNotEqual(ours, val)
        self.assertNotEqual(ours, other)
        self.assertNotEqual(val, other)

    def test_the_census_covers_aggregate_and_latched_states_too(self):
        """Round one's census omitted the aggregate and latched states
        and only checked that the token appeared, so a FALSE FALLBACK passed.
        This derives the set from proxywatch INCLUDING the aggregate, and
        asserts the fallback is the NEUTRAL sentence rather than merely
        non-empty."""
        from helm import proxywatch
        states = (set(proxywatch._UPSTREAM_DARK)
                  | {proxywatch._PROXY_COOLDOWN}
                  | set(getattr(proxywatch, "_UPSTREAM_AGGREGATE", ()))
                  | {"UNKNOWN"})
        self.assertGreaterEqual(len(states), 13,
                                "the dark set shrank — this arm measures nothing")
        typed = seat_usability._DARK_OURS | seat_usability._DARK_OUR_VALIDATION
        for state in sorted(states):
            why = seat_usability._dark_reason(state, "2026-01-01T00:00:00Z")
            self.assertIn(state, why)
            if state not in typed:
                self.assertIn("does not encode WHERE the failure originated",
                              why, "untyped state %r asserted a cause" % state)


class TheWorkHoldingCount(unittest.TestCase):
    """The count runs through the REAL `dispatches.open_recipients` over a
    fixture ledger — mocking the number would test nothing but arithmetic."""

    def counts(self, ledger_rows):
        from helm import dispatches
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(ledger_rows, None)):
            return dispatches.open_recipients()[0]

    def row(self, seat, ledger_rows):
        return seat_usability.line(
            seat, seat_usability.join(
                seats=[seat], health=_health([_hrow(seat)]),
                upstream=_upstream(_HEALTHY_UP),
                open_recipients=lambda: (self.counts(ledger_rows), None),
                register=_roster({seat: {"runtime_verified": True}}),
                canonical=_IDENTITY, live_seats=_panes([seat])))

    LEDGER = {
        "d1": {"id": "d1", "status": "open", "recipient": "seat-a",
               "kind": "build"},
        "d2": {"id": "d2", "status": "open", "recipient": "seat-a",
               "kind": "review"},      # a LAND REQUEST — same ledger
        "d3": {"id": "d3", "status": "open", "recipient": "gemini",
               "kind": "build"},
        "d4": {"id": "d4", "status": "closed", "recipient": "seat-a",
               "kind": "build"},
    }

    def test_the_count_equals_the_fixture_rows_addressed_to_that_seat(self):
        counts = self.counts(self.LEDGER)
        # STRUCTURAL: the fold produced a populated map, so the number below
        # is a measurement and not the default of an empty dict.
        self.assertEqual(sorted(counts), ["gemini", "seat-a"])
        self.assertEqual(counts.get("seat-a"), 2)
        self.assertTrue("holding=2" in self.row("seat-a", self.LEDGER),
                        self.row("seat-a", self.LEDGER))

    def test_a_dispatch_and_a_land_request_are_ONE_ledger_and_BOTH_count(self):
        """kind=build is a dispatch, kind=review is an lr. The owner named two
        ledgers; there is one, and the count would be half if this were wrong."""
        only_builds = {k: v for k, v in self.LEDGER.items()
                       if v.get("kind") != "review"}
        self.assertEqual(self.counts(only_builds).get("seat-a"), 1)
        self.assertEqual(self.counts(self.LEDGER).get("seat-a"), 2)

    def test_a_CLOSED_row_is_not_counted_against_the_seat(self):
        closed_only = {"d4": self.LEDGER["d4"]}
        self.assertEqual(self.counts(closed_only).get("seat-a"), None)
        self.assertTrue("holding=0" in self.row("seat-a", closed_only),
                        self.row("seat-a", closed_only))

    def test_another_seats_rows_do_not_land_on_this_one(self):
        counts = self.counts(self.LEDGER)
        self.assertEqual(sorted(counts), ["gemini", "seat-a"])
        self.assertEqual(counts.get("gemini"), 1)
        self.assertTrue("holding=1" in self.row("gemini", self.LEDGER),
                        self.row("gemini", self.LEDGER))

    def test_the_count_goes_through_the_CANONICAL_recipient_seam(self):
        """proxywatch's own fuse resolves a seat name to its ledger recipient
        before counting; the roster must use the SAME resolution or the two
        surfaces disagree about whose rows these are."""
        got = seat_usability.line(
            "alias", seat_usability.join(
                seats=["alias"], health=_health([_hrow("alias")]),
                upstream=_upstream(_HEALTHY_UP),
                open_recipients=_ledger({"resolved-seat": 7}),
                register=_roster({"alias": {"runtime_verified": True}}),
                canonical=lambda name: ("resolved-seat", None),
                live_seats=_panes(["alias"])))
        self.assertTrue("holding=7" in got, got)

    def test_an_unresolvable_seat_name_is_UNKNOWN_holding_not_zero(self):
        got = seat_usability.line(
            "alias", seat_usability.join(
                seats=["alias"], health=_health([_hrow("alias")]),
                upstream=_upstream(_HEALTHY_UP),
                open_recipients=_ledger({"resolved-seat": 7}),
                register=_roster({"alias": {"runtime_verified": True}}),
                canonical=lambda name: (None, "two seats answer to 'alias'"),
                live_seats=_panes(["alias"])))
        self.assertTrue("holding=UNKNOWN" in got, got)
        self.assertTrue("two seats answer to 'alias'" in got, got)


class TheLadderOwnsStaleness(unittest.TestCase):
    """CAUGHT ON THE LIVE FLEET the first time this rendered:

        codex  DEGRADED turn=ok last=0h56m ... - no completed turn in 0h56m

    a line that contradicts itself in eight characters. `turn_state` subtracts
    the HOST SUSPEND GAP from the age before comparing it to HANG_S; the row
    carries the RAW WALL age. Re-comparing that raw age here was a SECOND,
    DIFFERENT staleness rule wearing the first one's name."""

    def test_turn_ok_past_the_RAW_bar_does_NOT_claim_a_missed_turn(self):
        got = _line("seat-a", [_hrow("seat-a", turn_state="ok",
                                    transcript_age_s=56 * 60)])
        self.assertTrue("last=0h56m" in got, got)
        self.assertFalse("no completed turn" in got, got)
        self.assertTrue(" USABLE " in got, got)

    def test_CONTROL_the_ladder_saying_stale_DOES_name_the_age(self):
        """Same age, same renderer, the ladder's verdict changed — so the
        silence above is about `ok` and not a clause that never fires."""
        got = _line("seat-a", [_hrow("seat-a", turn_state="idle",
                                    turn_evidence="out of work",
                                    transcript_age_s=56 * 60)])
        self.assertTrue("no completed turn in 0h56m" in got, got)


class MeasuredRefusalOutranksAnUnreadableSibling(unittest.TestCase):
    """Precedence: a fact helm HOLDS must not be downgraded to UNKNOWN because
    some OTHER field would not read."""

    def test_a_GONE_pane_still_reads_UNUSABLE_with_the_ledger_dark(self):
        rows = seat_usability.join(
            seats=["seat-d"],
            health=_health([_hrow("seat-d", pane_live=False,
                                  turn_state="off")]),
            upstream=_upstream(_HEALTHY_UP),
            open_recipients=_ledger(None, unavailable="ledger torn"),
            register=_roster(_REGISTERED), canonical=_IDENTITY,
            live_seats=_panes(["seat-d"]))
        got = seat_usability.line("seat-d", rows)
        self.assertTrue(" UNUSABLE " in got, got)
        self.assertTrue("holding=UNKNOWN" in got, got)

    def test_a_STARVED_turn_reads_UNUSABLE_and_carries_its_evidence(self):
        got = _line("seat-a", [_hrow(
            "seat-a", turn_state="starved",
            turn_evidence="proxy refusing the seat's own traffic",
            transcript_age_s=42 * 3600)])
        self.assertTrue(" UNUSABLE " in got, got)
        self.assertTrue("proxy refusing the seat's own traffic" in got, got)
        self.assertTrue("no completed turn in 42h00m" in got, got)

    def test_a_HUNG_turn_reads_UNUSABLE(self):
        got = _line("seat-a", [_hrow("seat-a", turn_state="hung",
                                    turn_evidence="pane LIVE; stale",
                                    transcript_age_s=3 * 3600)])
        self.assertTrue(" UNUSABLE " in got, got)
        self.assertTrue("turn=hung" in got, got)


class TheDisplayDoesNotProbe(unittest.TestCase):
    """A verb an operator scans constantly must not open an HTTP request per
    seat. proxywatch's own `upstream_snapshot` already states the law."""

    def test_the_join_tells_health_to_skip_the_probe_AND_the_canary(self):
        fn = _health([_hrow("seat-a")])
        seat_usability.join(health=fn, upstream=_upstream(_HEALTHY_UP),
                            open_recipients=_ledger({}),
                            register=_roster(_REGISTERED), canonical=_IDENTITY,
                            live_seats=_panes(["seat-a"]))
        # POSITIVE FIRST: health WAS called, so the two False readings below
        # are the flags this join set and not an untouched empty dict.
        self.assertEqual(sorted(fn.seen), ["include_probe", "include_upstream"])
        self.assertEqual(fn.seen.get("include_probe"), False)
        self.assertEqual(fn.seen.get("include_upstream"), False)

    def test_the_join_calls_each_reader_EXACTLY_ONCE_for_the_whole_fleet(self):
        """N seats must not cost N ledger folds or N process censuses."""
        calls = []
        fleet = [_hrow("seat-a"), _hrow("seat-b"), _hrow("seat-c"),
                 _hrow("seat-d")]

        def ledger():
            calls.append("ledger")
            return {}, None

        def up():
            calls.append("upstream")
            return _HEALTHY_UP, None

        def census():
            calls.append("census")
            return set(), None

        rows = seat_usability.join(health=_health(fleet), upstream=up,
                                   open_recipients=ledger,
                                   register=_roster(_REGISTERED),
                                   canonical=_IDENTITY, live_seats=census)
        self.assertEqual(len(rows), 4)
        self.assertEqual(calls.count("ledger"), 1)
        self.assertEqual(calls.count("upstream"), 1)
        self.assertEqual(calls.count("census"), 1)


class HealthHonoursTheProbeGate(unittest.TestCase):
    """The seam the join relies on. Both arms share ONE fixture: if the mocks
    are wrong the PROBING arm fails too, so a green skip-arm cannot be a mock
    that never reached the rung."""

    def run_health(self, **kw):
        import tempfile
        from helm import autocompact, pi, proxywatch, seat, seats
        hits = []
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(proxywatch, "_watched_seats",
                                  return_value=["fixture-seat"]), \
                mock.patch.object(seats, "roster", return_value={}), \
                mock.patch.object(seat, "family_for",
                                  return_value=("seat-a", None)), \
                mock.patch.object(seat, "_proxy_home", return_value=tmp), \
                mock.patch.object(seat, "_instance_dir", return_value=tmp), \
                mock.patch.object(proxywatch, "log_observation",
                                  return_value={"state": None, "detail": None,
                                                "status_401": {}}), \
                mock.patch.object(autocompact, "_newest_transcript",
                                  return_value=None), \
                mock.patch.object(proxywatch, "_live_seats",
                                  return_value=(set(), None, {})), \
                mock.patch.object(pi, "seat_port", return_value=(8317, None)), \
                mock.patch.object(proxywatch, "probe",
                                  side_effect=lambda *a, **k: (
                                      hits.append(a) or ("401", "ok", 1))):
            rep = proxywatch.health(include_upstream=False, **kw)
        return rep, hits

    def test_CONTROL_the_default_pass_DOES_reach_the_probe_rung(self):
        rep, hits = self.run_health()
        self.assertEqual(len(rep["seats"]), 1)
        self.assertEqual(len(hits), 1)

    def test_include_probe_False_calls_the_probe_ZERO_times(self):
        rep, hits = self.run_health(include_probe=False)
        self.assertEqual(len(rep["seats"]), 1)      # the pass still ran
        self.assertEqual(len(hits), 0)
        self.assertEqual(rep["seats"][0].get("probe"), None)


class WiredIntoTheRosterSurface(unittest.TestCase):
    """THE HELPER BEING RIGHT SAYS NOTHING ABOUT IT BEING CALLED — the lesson
    a mutation already taught this exact file's neighbour (replacing the list
    column's `live += upstream_phrase(...)` with `live += ""` passed all 138
    seat tests). `_seat_row` is the only thing an operator ever sees."""

    # The block `_seat_row` returns, BY INDEX. Asserting a POSITION is what
    # binds each verdict to the seat above it — the first cut filtered lines
    # by name prefix and could not tell the family's PROXY line from the
    # family's VERDICT line, because after `.strip()` both start with the same
    # name. It asserted `len(...) == 1` against a list that always held 2.
    FAMILY_PROXY, FAMILY_VERDICT, INSTANCE_PROXY, INSTANCE_VERDICT = range(4)

    def rendered(self, usability, family="seat-a", instances=("seat-b",),
                 resolvable=True, catalog=None):
        from helm import seat
        # THE FIXTURE DECLARES ITS FAMILY IN THE CATALOG, and that is
        # load-bearing rather than tidy-up. `_seat_row` used to render a full
        # block for ANY name via `FAMILIES.get(family) or {}`; it now
        # DISCRIMINATES, because port/mode/cred are unknowable for a name the
        # catalog cannot resolve — see
        # test_an_UNRESOLVABLE_family_renders_UNKNOWN_not_a_full_row, which
        # pins that contract and is the reason this line exists.
        #
        # Declaring the SYNTHETIC name beats renaming to a real family: these
        # arms are about the verdict/proxy INTERLEAVE, and "codex" would drag
        # in the pooled-capacity branch (another arm's subject) while a
        # proxy-key family would swap the cred column — both changing what
        # renders for reasons unrelated to interleave. Names also stay bound
        # to the `_hrow` keys the verdicts are looked up by.
        entry = ({family: dict({"port": 8317, "mode": "proxy"},
                               **(catalog or {}))}
                 if resolvable and family not in seat.FAMILIES else {})
        with mock.patch.dict(seat.FAMILIES, entry), \
                mock.patch.object(seat, "seat_dir", return_value="/nonexistent"), \
                mock.patch.object(seat, "cred_state",
                                  return_value=("VALID", "valid until T",
                                                "a@c.example")), \
                mock.patch.object(seat, "_minted_instances",
                                  return_value=list(instances)), \
                mock.patch.object(seat, "_proxy_live_text",
                                  return_value=("proxy UP pid 1 port 8317",
                                                None)):
            return seat._seat_row(family, usability=usability)

    def test_the_FAMILY_row_carries_its_own_usability_line(self):
        rows = _join([_hrow("seat-a", turn_state="idle",
                            turn_evidence="out of work",
                            transcript_age_s=42 * 3600),
                      _hrow("seat-b")])
        block = self.rendered(rows).splitlines()
        self.assertEqual(len(block), 4)          # 2 proxy lines, 2 verdicts
        self.assertTrue("proxy UP pid 1" in block[self.FAMILY_PROXY],
                        block[self.FAMILY_PROXY])
        got = block[self.FAMILY_VERDICT]
        self.assertTrue(got.strip().startswith("seat-a "), got)
        self.assertTrue("DEGRADED" in got, got)
        self.assertTrue("no completed turn in 42h00m" in got, got)

    def test_each_INSTANCE_gets_its_OWN_verdict_under_its_own_proxy_line(self):
        rows = _join([_hrow("seat-a"),
                      _hrow("seat-b", pane_live=False, turn_state="off")])
        block = self.rendered(rows).splitlines()
        self.assertEqual(len(block), 4)
        self.assertTrue("USABLE" in block[self.FAMILY_VERDICT],
                        block[self.FAMILY_VERDICT])
        got = block[self.INSTANCE_VERDICT]
        self.assertTrue(got.strip().startswith("seat-b "), got)
        self.assertTrue("UNUSABLE" in got, got)
        self.assertTrue("pane=GONE" in got, got)

    def test_each_PROXY_row_names_the_model_that_seat_launches_on(self):
        """`instance_models` (seat_catalog) lets one family span two models —
        codex-4 on gpt-5.6-sol while codex-7 stays gpt-6-astra — and the
        operator's only way to SEE which seat runs which is this column. The
        roster is the surface; a catalog nobody can read off the screen sends
        him to the source to answer "what is this seat running?"."""
        rows = _join([_hrow("seat-a"), _hrow("seat-b")])
        declared = self.rendered(rows, catalog={
            "model": "fam-default",
            "instance_models": {"seat-b": "declared-model"}}).splitlines()
        self.assertIn("fam-default", declared[self.FAMILY_PROXY])
        self.assertIn("declared-model", declared[self.INSTANCE_PROXY])
        # THE CONTROL is the same fixture with the declaration removed and
        # nothing else changed: the instance then shows the family model,
        # which is what it launches on — so the column is read per ROW and is
        # not the family's answer repeated.
        plain = self.rendered(rows, catalog={"model": "fam-default"}).splitlines()
        self.assertIn("fam-default", plain[self.INSTANCE_PROXY])
        self.assertNotIn("declared-model", plain[self.INSTANCE_PROXY])

    def test_a_None_usability_leaves_the_PROXY_rows_byte_identical(self):
        """The extension must not rewrite what the roster already said —
        `seat doctor` and every existing reader see the same proxy lines."""
        rows = _join([_hrow("seat-a"), _hrow("seat-b")])
        with_join = self.rendered(rows).splitlines()
        without = self.rendered(None).splitlines()
        self.assertEqual(len(without), 2)
        self.assertEqual(without[0], with_join[self.FAMILY_PROXY])
        self.assertEqual(without[1], with_join[self.INSTANCE_PROXY])

    def test_an_UNRESOLVABLE_family_renders_UNKNOWN_not_a_full_row(self):
        """THE CONTRACT THE THREE ARMS ABOVE NOW DEPEND ON, stated once.

        `_seat_row` used to render a full block for ANY name. It now takes an
        UNKNOWN path for a family the catalog cannot resolve, and the sibling
        fixtures declare their family precisely so they keep testing
        interleave rather than this. Without this arm that declaration reads
        as a fixture tidy-up and the contract change has no executable proof.

        WHY UNKNOWN AND NOT A FULL ROW: port, mode and cred are not merely
        absent for an unresolvable name, they are UNKNOWABLE. Rendering the
        familiar 4-line block would put values on screen the surface cannot
        have — trading a crash for a confident wrong row, which is the same
        defect one layer quieter. The roster's original bug was a stump that
        READ AS WHOLE; a row that reads as resolved is that bug again.
        """
        from helm import seat
        # MUST-HIT CONTROL ON THE SAME OBSERVABLE, and not linter appeasement:
        # an EMPTY or mocked-away FAMILIES would satisfy the absence below
        # vacuously, and every assertion after it would describe a degenerate
        # world where nothing is resolvable. The catalog has to be readable
        # and populated for "nofam is unresolvable" to mean anything.
        self.assertIn("codex", seat.FAMILIES)
        self.assertNotIn("nofam", seat.FAMILIES,
                         "the arm's premise is that this name is unresolvable")
        rows = _join([_hrow("nofam"), _hrow("seat-b")],
                     seats=["nofam", "seat-b"])
        block = self.rendered(rows, family="nofam",
                              resolvable=False).splitlines()
        # 2 proxy lines, 2 verdicts, and the warning the resolvable path has
        # no reason to emit
        self.assertEqual(len(block), 5, block)
        warn = block[4]
        self.assertIn("unknown family", warn)
        self.assertIn("nofam", warn)
        row = block[self.FAMILY_PROXY]
        # BOTH catalog-derived columns SAY UNKNOWN — a blank column reads as
        # health. This is also the positive pole for the two absences below:
        # they constrain this same rendered row.
        self.assertEqual(row.count("UNKNOWN"), 2, row)
        # ...and the row states neither of the things it cannot know. The
        # mocked proxy/cred seams are never consulted on this path, so their
        # fixture values reaching the row would mean the guard was bypassed.
        self.assertNotIn("proxy UP pid 1", row)
        self.assertNotIn("a@c.example", row)
        # the usability join is the ONLY thing still knowable (it is keyed by
        # seat NAME, not by catalog), so dropping it leaves 2 rows + warning
        bare = self.rendered(None, family="nofam",
                             resolvable=False).splitlines()
        self.assertEqual(len(bare), 3, bare)

    def test_the_capacity_suffix_still_lands_on_the_last_PROXY_line(self):
        """It used to be appended to a concatenated string that happened to end
        with the last proxy line; the interleave must not push it onto a
        verdict line. The pooled-capacity branch is keyed on the literal family
        name, so this arm uses that name and joins a row under it."""
        from helm import codexhomes, seats
        rows = _join([_hrow("codex"), _hrow("seat-b")],
                     seats=["codex", "seat-b"])
        with mock.patch.object(seats, "roster", return_value={}), \
                mock.patch.object(codexhomes, "capacity",
                                  return_value={"total": 8}):
            block = self.rendered(rows, family="codex").splitlines()
        self.assertEqual(len(block), 4)
        self.assertTrue("[instances: 0 live / cap 8]"
                        in block[self.INSTANCE_PROXY],
                        block[self.INSTANCE_PROXY])
        self.assertFalse("[instances:" in block[self.INSTANCE_VERDICT],
                         block[self.INSTANCE_VERDICT])


class TheReturnIsTheContractAndTheRenderIsBuiltOnIt(unittest.TestCase):
    """Owner catch mid-build: "another p0 impl, another built-not-wired class,
    no?" A roster that only RENDERS usability is still an instrument somebody
    has to read. The typed answer is the primary artifact."""

    def test_every_row_carries_verdict_reason_can_take_work_and_measured_at(self):
        rows = _join([_hrow("seat-a")], seats=["seat-a"])
        row = rows["seat-a"]
        self.assertEqual(row["verdict"], seat_usability.USABLE)
        self.assertEqual(row["reason"], "")
        self.assertTrue(row["can_take_work"] is True)
        self.assertEqual(row["holding"], 0)
        self.assertTrue(isinstance(row["measured_at"], float))

    # noqa: VACUOUS_ASSERTION — the control is UNCONDITIONAL and on the SAME
    # field: the first two lines assert can_take_work is True for a healthy
    # row, so the `is False` below cannot be an absent key.
    def test_can_take_work_is_FALSE_only_for_a_MEASURED_refusal(self):
        # CONTROL on the SAME field: the healthy row reads True, so False
        # below is the measurement and not an absent key defaulting.
        healthy = _join([_hrow("seat-a")])
        self.assertTrue(healthy["seat-a"]["can_take_work"] is True)
        gone = _join([_hrow("seat-a", pane_live=False, turn_state="off")])
        self.assertTrue(gone["seat-a"]["can_take_work"] is False)
        self.assertEqual(gone["seat-a"]["verdict"], seat_usability.UNUSABLE)

    # noqa: VACUOUS_ASSERTION — same shape: the healthy row is asserted True
    # on the same field first, so `is None` below is the measured tri-state.
    def test_can_take_work_is_NONE_for_UNKNOWN_and_never_False(self):
        """UNKNOWN is helm unable to tell. Collapsing it into False would make
        every guard refuse on absence — the class where an aged-out record
        downgrades a legitimate case."""
        healthy = _join([_hrow("seat-a")])
        self.assertTrue(healthy["seat-a"]["can_take_work"] is True)
        blind = _join([_hrow("seat-a")],
                      upstream=_upstream(None, err="cache is 90m old"))
        row = blind["seat-a"]
        self.assertEqual(row["verdict"], seat_usability.UNKNOWN)
        self.assertTrue(row["can_take_work"] is None)

    def test_can_take_work_is_TRUE_for_DEGRADED_with_the_caveat_named(self):
        rows = _join([_hrow("seat-a", turn_state="idle", turn_evidence="idle",
                            transcript_age_s=42 * 3600)])
        row = rows["seat-a"]
        self.assertEqual(row["verdict"], seat_usability.DEGRADED)
        self.assertTrue(row["can_take_work"] is True)
        self.assertTrue("42h00m" in row["reason"], row["reason"])

    def verdict_for(self, **over):
        fn = _health([_hrow("seat-a")])
        kw = dict(health=fn, upstream=_upstream(_HEALTHY_UP),
                  open_recipients=self.ledger, register=_roster(_REGISTERED),
                  canonical=_IDENTITY, live_seats=_panes(["seat-a"]))
        kw.update(over)
        return fn, seat_usability.seat_verdict("seat-a", **kw)

    def setUp(self):
        self.folds = []

        def ledger():
            self.folds.append(1)
            return {"seat-a": 3}, None
        self.ledger = ledger

    def test_seat_verdict_scopes_the_measurement_to_the_ONE_seat_asked(self):
        """A router must not measure the whole fleet to admit one dispatch."""
        fn, (state, why, row) = self.verdict_for(need_holding=True)
        self.assertEqual(fn.seen.get("seats"), ["seat-a"])
        self.assertEqual(state, seat_usability.USABLE)
        self.assertEqual(row["holding"], 3)
        self.assertEqual(why, "")

    def test_seat_verdict_folds_NO_ledger_by_default(self):
        """The count is a DISPLAY fact — no rung of the verdict reads it — and
        folding it here put a SECOND snapshot read on every dispatch write,
        which tests/test_dispatch_chain.py pins against by call count. `-` is
        not-asked: neither 0 (this seat is free) nor UNKNOWN (helm looked and
        failed)."""
        # CONTROL FIRST, on the same seam: asking for it DOES fold once.
        self.verdict_for(need_holding=True)
        self.assertEqual(len(self.folds), 1)
        _fn, (state, _why, row) = self.verdict_for()
        self.assertEqual(len(self.folds), 1)      # unchanged: no second fold
        self.assertEqual(state, seat_usability.USABLE)
        self.assertTrue(row["holding"] is None)
        self.assertEqual(row["holding_scope"], "not-asked")
        self.assertTrue("holding=-" in seat_usability.line("seat-a",
                                                           {"seat-a": row}))

    def test_the_render_reads_the_STORED_verdict_rather_than_deriving_one(self):
        """Two derivations is how the display and the router come to disagree.
        Overwriting the stored verdict must change the rendered line."""
        rows = _join([_hrow("seat-a")], seats=["seat-a"])
        rows["seat-a"]["verdict"] = "SENTINEL"
        rows["seat-a"]["reason"] = "planted"
        got = seat_usability.line("seat-a", rows)
        self.assertTrue("SENTINEL" in got, got)
        self.assertTrue("planted" in got, got)


class ASeatWithNoProxyIsScopedNotBlind(unittest.TestCase):
    """Caught by wiring the router BEFORE shipping it: proxywatch watches
    minted PROXY seats, so every NATIVE claude seat on the fleet came back
    turn/last/pane UNREADABLE — and the dispatch gate would have stamped
    "usability UNKNOWN" on the majority of dispatches helm sends. A warning
    every row carries is a warning nobody reads."""

    def native(self, panes=(), blind=None, reg=None):
        rows = seat_usability.join(
            seats=["seat-native"], health=_health([]),
            upstream=_upstream(_HEALTHY_UP), open_recipients=_ledger({"seat-native": 2}),
            register=_roster({"seat-native": {"runtime_verified": True}}
                           if reg is None else reg),
            canonical=_IDENTITY, live_seats=_panes(panes, blind))
        return rows, seat_usability.line("seat-native", rows)

    def test_a_LIVE_native_pane_is_USABLE_with_the_proxy_fields_marked_na(self):
        rows, got = self.native(panes=["seat-native"])
        self.assertEqual(rows["seat-native"]["scope"], "pane-only")
        self.assertEqual(rows["seat-native"]["verdict"], seat_usability.USABLE)
        self.assertTrue("turn=n/a" in got, got)
        self.assertTrue("upstream=n/a" in got, got)
        self.assertTrue("pane=live" in got, got)
        self.assertTrue("holding=2" in got, got)
        self.assertFalse("UNKNOWN" in got, got)

    def test_a_GONE_native_pane_is_STILL_caught_and_named(self):
        """The pane census keys on HELM_CHAT_NAME, so the seat-d question is
        answerable for a native seat too — that half must not be lost."""
        rows, got = self.native(panes=["someone-else"])
        self.assertEqual(rows["seat-native"]["verdict"], seat_usability.UNUSABLE)
        self.assertTrue(rows["seat-native"]["can_take_work"] is False)
        self.assertTrue("pane=GONE" in got, got)

    def test_the_repair_named_for_a_native_pane_is_resume_NOT_spawn(self):
        """`helm seat spawn` mints a PROXY seat. Prescribing it here would hand
        the operator an instrument that cannot fix their case."""
        _rows, got = self.native(panes=[])
        self.assertTrue("helm seat resume seat-native" in got, got)
        self.assertFalse("helm seat spawn" in got, got)

    def test_a_BLIND_census_on_a_native_seat_is_UNKNOWN_never_GONE(self):
        rows, got = self.native(panes=[], blind="the census could not look")
        self.assertEqual(rows["seat-native"]["verdict"], seat_usability.UNKNOWN)
        self.assertTrue("pane=UNKNOWN" in got, got)
        self.assertTrue("the census could not look" in got, got)


class OneSeatsCensusRefusalStaysOnOneRow(unittest.TestCase):
    """`helm seat list` is where the blast radius was visible (task/2739).

    `_read_panes` now carries a THIRD element — the refusals that belong to
    exactly one seat — and `join` gives each of them to its own row. MEASURED
    before the split: one seat renamed while live put its own pid and refusal
    sentence into nine other seats' lines and turned 29 measured `pane=GONE`
    answers into `pane=UNKNOWN`.
    """

    def native(self, panes=(), blind=None, per_seat=None, seat="seat-native"):
        census = lambda: (set(panes), blind, dict(per_seat or {}))  # noqa: E731
        rows = seat_usability.join(
            seats=["seat-native", "seat-other"], health=_health([]),
            upstream=_upstream(_HEALTHY_UP), open_recipients=_ledger({}),
            register=_roster({"seat-native": {"runtime_verified": True},
                              "seat-other": {"runtime_verified": True}}),
            canonical=_IDENTITY, live_seats=census)
        return rows, seat_usability.line(seat, rows)

    def test_a_refused_seat_is_UNKNOWN_and_its_NEIGHBOUR_is_still_GONE(self):
        """Both halves in one arm, because either alone passes for the wrong
        reason: an UNKNOWN everywhere is the old defect, and a GONE everywhere
        is a per-seat channel that never arrives."""
        rows, mine = self.native(
            per_seat={"seat-native": "seat-native (renamed while live)"})
        self.assertEqual(rows["seat-native"]["verdict"], seat_usability.UNKNOWN)
        self.assertIn("pane=UNKNOWN", mine)
        self.assertIn("renamed while live", mine)
        _rows, theirs = self.native(
            per_seat={"seat-native": "seat-native (renamed while live)"},
            seat="seat-other")
        self.assertIn("pane=GONE", theirs)
        self.assertNotIn("renamed while live", theirs,
                         "one seat's refusal was printed on another's row")

    def test_a_HOST_WIDE_reason_still_reaches_BOTH_rows(self):
        """The control on the same observable: `seat-other` reading GONE above
        is the split working, not a row that can never go UNKNOWN."""
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE: the loop
        # below cannot pass by never running.
        _rows, first = self.native(blind="the census could not look")
        self.assertIn("pane=UNKNOWN", first)
        self.assertIn("the census could not look", first)
        for name in ("seat-native", "seat-other"):
            _rows, got = self.native(blind="the census could not look",
                                     seat=name)
            self.assertIn("pane=UNKNOWN", got)
            self.assertIn("the census could not look", got)

    def test_a_census_that_RAISES_is_UNKNOWN_and_names_the_class(self):
        """The seam is injectable, and a double whose shape does not match the
        one producer must fail LOUDLY as UNKNOWN rather than be tolerated into
        a narrower answer. The three-tuple arms above are the control that the
        seam works at all."""
        rows = seat_usability.join(
            seats=["seat-native"], health=_health([]),
            upstream=_upstream(_HEALTHY_UP), open_recipients=_ledger({}),
            register=_roster({"seat-native": {"runtime_verified": True}}),
            canonical=_IDENTITY,
            live_seats=lambda: (_ for _ in ()).throw(OSError("ps refused")))
        self.assertEqual(rows["seat-native"]["verdict"], seat_usability.UNKNOWN)
        got = seat_usability.line("seat-native", rows)
        self.assertIn("ps refused", got)
        self.assertIn("could not be taken", got)


class TheDispatchGateIsTheConsumer(unittest.TestCase):
    """THE POINT OF THE LANE. A rendered verdict nobody reads is the same bug
    one layer up, so the join has a caller that ACTS: the recipient-admission
    rung every dispatch write path already passes through."""

    def gate(self, verdict_state, reason, row, force=False):
        from helm import dispatches, seat_usability as su
        with mock.patch.object(su, "seat_verdict",
                               return_value=(verdict_state, reason, row)):
            return dispatches._validate_recipient_usable("seat-a", force)

    def test_a_LIVE_but_UNUSABLE_recipient_is_REFUSED_naming_the_reason(self):
        ok, refusal, warning = self.gate(
            seat_usability.UNUSABLE,
            "upstream AUTH-UNAVAILABLE since T — the provider is refusing",
            {"can_take_work": False, "pane": True})
        self.assertFalse(ok)
        self.assertTrue(warning is None)
        self.assertTrue("AUTH-UNAVAILABLE" in refusal, refusal)
        self.assertTrue("force=True" in refusal, refusal)

    def test_CONTROL_a_USABLE_recipient_is_admitted_with_NO_warning(self):
        """UNCONDITIONAL CONTROL: the same rung admits cleanly, so the refusal
        above measures the verdict and not a gate that refuses everything."""
        ok, refusal, warning = self.gate(
            seat_usability.USABLE, "", {"can_take_work": True, "pane": True})
        self.assertTrue(ok)
        self.assertTrue(refusal is None)
        self.assertTrue(warning is None)

    # noqa: VACUOUS_ASSERTION — the UNCONDITIONAL positive control is the
    # first arm: the SAME rung, the SAME UNUSABLE verdict, one field flipped,
    # refuses and returns a reason. So silence here is the pane clause, not a
    # gate that says nothing.
    def test_a_GONE_pane_is_ADMITTED_SILENTLY_and_never_refused(self):
        """The ledger is DURABLE and the beacon replays open rows on relaunch,
        so filing work for a seat between panes is the normal working case —
        refusing it would break routing to every seat mid-relaunch, and SAYING
        so on every such row is noise nobody would keep reading. Measured: the
        first cut warned here, and every ordinary dispatch in the suite tripped
        it (four test_dispatch_chain arms went red on the warning alone)."""
        loud = self.gate(
            seat_usability.UNUSABLE, "upstream AUTH-UNAVAILABLE since T",
            {"can_take_work": False, "pane": True})
        self.assertFalse(loud[0], "control: the same rung DOES refuse")
        ok, refusal, warning = self.gate(
            seat_usability.UNUSABLE, "pane GONE — no live process",
            {"can_take_work": False, "pane": False})
        self.assertTrue(ok)
        self.assertTrue(refusal is None)
        self.assertTrue(warning is None)

    def test_an_UNKNOWN_recipient_is_ADMITTED_and_SAYS_so(self):
        """Refusing on absence would brick every box where proxywatch has never
        run. Unknown is announced, never silently promoted to healthy."""
        ok, refusal, warning = self.gate(
            seat_usability.UNKNOWN, "upstream UNREADABLE (cache 90m old)",
            {"can_take_work": None, "pane": True})
        self.assertTrue(ok)
        self.assertTrue(refusal is None)
        self.assertTrue("UNKNOWN" in warning, warning)
        self.assertTrue("cache 90m old" in warning, warning)

    def test_a_DEGRADED_recipient_is_ADMITTED_carrying_its_caveat(self):
        ok, refusal, warning = self.gate(
            seat_usability.DEGRADED, "no completed turn in 42h13m",
            {"can_take_work": True, "pane": True})
        self.assertTrue(ok)
        self.assertTrue(refusal is None)
        self.assertTrue("42h13m" in warning, warning)

    def test_force_skips_the_gate_entirely_and_costs_nothing(self):
        from helm import dispatches, seat_usability as su

        def boom(*_a, **_k):
            raise AssertionError("force must not measure the seat at all")
        with mock.patch.object(su, "seat_verdict", side_effect=boom):
            ok, refusal, warning = dispatches._validate_recipient_usable(
                "codex", True)
        self.assertTrue(ok)
        self.assertTrue(refusal is None)
        self.assertTrue(warning is None)

    def test_the_GATE_ITSELF_failing_admits_the_row_and_says_it_was_blind(self):
        """A dispatch must not be lost because a usability reader raised — and
        a broken guard must not masquerade as a clean admission."""
        from helm import dispatches, seat_usability as su
        with mock.patch.object(su, "seat_verdict",
                               side_effect=OSError("proc table gone")):
            ok, refusal, warning = dispatches._validate_recipient_usable(
                "codex", False)
        self.assertTrue(ok)
        self.assertTrue(refusal is None)
        self.assertTrue("proc table gone" in warning, warning)
        self.assertTrue("unverified" in warning, warning)


class TheGateIsWiredIntoBothWritePaths(unittest.TestCase):
    """THE RUNG BEING RIGHT SAYS NOTHING ABOUT IT BEING CALLED. `send()` and
    `add()` are two doors onto the same ledger and only one of them has ever
    grown a guard first."""

    def refuse(self):
        return (False, "recipient 'seat-a' is UNUSABLE right now: walled", None)

    def test_send_REFUSES_through_the_usability_rung(self):
        from helm import dispatches
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=("me", None)), \
             mock.patch.object(dispatches, "_recipient_operand",
                               return_value=("seat-a", None)), \
             mock.patch.object(dispatches, "_validate_recipient_rostered",
                               return_value=(True, None)), \
             mock.patch.object(dispatches, "_validate_recipient_usable",
                               return_value=self.refuse()) as gate:
            row, why, sent = dispatches.send("seat-a", "lane", "msg", "HEAD")
        self.assertEqual(gate.call_count, 1)
        self.assertTrue(row is None)
        self.assertFalse(sent)
        self.assertTrue("UNUSABLE" in why, why)

    def test_add_REFUSES_through_the_usability_rung(self):
        from helm import dispatches
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=("me", None)), \
             mock.patch.object(dispatches, "_recipient_operand",
                               return_value=("seat-a", None)), \
             mock.patch.object(dispatches, "_validate_recipient_rostered",
                               return_value=(True, None)), \
             mock.patch.object(dispatches, "_validate_recipient_usable",
                               return_value=self.refuse()) as gate:
            row, why = dispatches.add("seat-a", "lane", _reason=True)
        self.assertEqual(gate.call_count, 1)
        self.assertTrue(row is None)
        self.assertTrue("UNUSABLE" in why, why)


class TheLegendDoesNotSatisfyARowAssertion(unittest.TestCase):
    """A guard on this file's own test method: the legend names every verdict
    word, so a screen-wide assertion would pass with every row blank."""

    def test_the_legend_carries_the_word_UNKNOWN_and_is_not_a_row(self):
        legend = seat_usability.legend()
        self.assertTrue("UNKNOWN" in legend, legend)
        self.assertTrue("holding" in legend, legend)
        self.assertFalse("turn=" in legend, legend)


if __name__ == "__main__":
    unittest.main()
