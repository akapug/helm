"""Arms for `helm route` — the routing answer that comes from the flags.

EVERY INPUT IS A FROZEN CAPTURE. The flags fixture is the burn-flag fold of
the readings committed under `fixtures/burnflags/`, taken once and written to
`fixtures/route/burn-flags.json`; the bench is a synthetic register in the
shape `seat_usability.join` emits; the store slice keys real edge source ids
to placeholder statements. No arm here reads a live snapshot path, a live
register, or the owner's real store — the one arm that asks the real
resolver asks it about its IDENTITY, not about its contents.

EVERY ARM NAMES THE CONTROL THAT PROVES IT COULD MOVE. A probe spy that was
never installed and a fixture that was never read both produce the same green
as a working check.
"""

import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import burnflags as bf                        # noqa: E402
from helm import route                                  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "route")

# THE FAMILY VOCABULARY, TAKEN FROM THE CATALOG RATHER THAN TYPED. Several
# family names are also the name of a live seat, and a real seat identity in
# tests/ is publication debt that compounds with every copy — so no arm here
# spells one. Unpacking also makes a NEW family a visible event in this file
# instead of a silent widening of every set below.
(NATIVE, BONSAI, CODEX, CURSOR, DOTS3, DS4FLASH, DS4PRO, GEMINI, GPTOSS, GROK,
 KIMI, OPENROUTER, OPUS46, QWEN27, QWENLOCAL) = bf.families()

# THE BENCH NAMES ITS FAMILIES THROUGH *THIS MODULE'S* VOCABULARY, NEVER
# THROUGH AN ORDINAL INTO THE CATALOG'S. bench.json addresses each seat's
# family by index, and while that index read `bf.families()` directly, adding
# a family RELABELLED every seat above the insertion point and pushed the last
# family off the end -- silently, because a list lookup has nothing to refuse.
# The split that minted the flash family sorts third: the pool seat became a
# flash seat, every seat below it slid one place, and the openrouter seat
# stopped existing. Eleven arms went red about pane states, orderings and
# refusals that had not moved at all, and not one of them was about the thing
# that changed. The index addresses THIS tuple now -- spelled in the constants
# above, so it moves only when a person moves it, and the fixed-arity unpack
# is red first.
_BENCH_FAMILIES = (NATIVE, CODEX, DS4PRO, GEMINI, GROK, KIMI, OPENROUTER)

# AND THE ABSENCE IS DECLARED, so it is a decision rather than a gap. The
# flash family is council-only and has no seat anywhere in the fleet (the same
# fact tests/test_burnflags.py carries as _UNSEATED_FAMILIES), so a bench with
# no row for it is this fleet's real shape and `route` drops it at N2 with a
# named reason instead of failing. A family that is neither benched nor named
# here is a fixture DECISION nobody made, so it refuses at import rather than
# arriving as somebody else's relabelled seat.
# The families below are unbenched for a DIFFERENT reason, and the difference
# matters: they are seated in the fleet, they are simply not in THIS bench.
# The bench is frozen and synthetic — eight named seats whose family_index
# addresses the tuple above — so a family that arrived after the freeze does
# not retroactively appear in it, and giving it a row would invent a fleet
# shape nobody measured. What the arms below then exercise for them is the
# real production answer for a family the register does not cover, which is
# the same named drop `route` gives the council-only family beside them.
_UNBENCHED = (BONSAI, DS4FLASH, DOTS3, GPTOSS, OPUS46, QWEN27, QWENLOCAL,
              CURSOR)

# AND THE BURN-FLAG CAPTURE PREDATES SEVERAL OF THEM, WHICH IS ALSO A
# DECLARATION RATHER THAN A PATCH. burn-flags.json is a READING taken off a
# live fleet, keyed by family letter and mapped onto this vocabulary in sorted
# order by `_rekey`. A family minted after that reading has no row in it.
# Inventing one would file made-up burn numbers under a real family's name --
# the fixture lesson this tree paid for twice -- and the honest repair is to
# RE-TAKE the capture from a fleet that runs the seat, which is a live-fleet
# act and not a test edit. So the uncaptured families are NAMED here and left
# out of the mapping; any OTHER length mismatch still refuses, because the
# silent slide is what the length check exists to catch.
_UNCAPTURED = (BONSAI, DOTS3, GPTOSS, OPUS46, QWENLOCAL, CURSOR)
if sorted(_BENCH_FAMILIES + _UNBENCHED) != sorted(bf.families()):
    raise AssertionError(
        "the bench vocabulary covers %r and this tree knows %r: give the new "
        "family a seat in fixtures/route/bench.json or declare it unbenched, "
        "never leave the index to slide"
        % (sorted(_BENCH_FAMILIES + _UNBENCHED), sorted(bf.families())))


def _rekey(captured, kind):
    """A capture keyed family-a..family-g -> the real vocabulary, in the
    catalog's own sorted order. Lane A's convention, for its reason, with its
    length check: a capture that no longer covers the vocabulary must be
    RE-TAKEN, never zipped short against a guess."""
    real = [f for f in bf.families() if f not in _UNCAPTURED]
    keys = sorted(captured)
    if len(keys) != len(real):
        raise AssertionError(
            "the %s capture holds %d families and this tree knows %d "
            "(uncaptured, declared above: %s): re-capture it rather than "
            "guessing the mapping"
            % (kind, len(keys), len(real), ", ".join(_UNCAPTURED) or "none"))
    return {name: captured[key] for name, key in zip(real, keys)}


def _flags_fixture():
    with open(os.path.join(FIXTURES, "burn-flags.json")) as fh:
        snap = json.load(fh)
    families = _rekey(snap["families"], "burn-flag")
    snap["families"] = {name: dict(row, family=name)
                        for name, row in families.items()}
    return snap


def _bench_fixture():
    with open(os.path.join(FIXTURES, "bench.json")) as fh:
        bench = json.load(fh)
    bench["seats"] = {name: (dict(row, family=_BENCH_FAMILIES[row["family_index"]])
                             if "family_index" in row else dict(row))
                      for name, row in bench["seats"].items()}
    return bench


def _store_fixture():
    with open(os.path.join(FIXTURES, "store-slice.json")) as fh:
        return json.load(fh)["entries"]


class World(object):
    """One frozen world, and every seam `route.answer` can take.

    The seams are BUILT FROM THE FIXTURES rather than hand-typed, so an arm
    that wants a different world edits the world and never the plumbing.
    """

    def __init__(self, flags=None, bench=None, entries=None, now=None):
        snap = _flags_fixture()
        self.snapshot = snap
        self.flags = flags if flags is not None else snap["families"]
        self.bench = bench if bench is not None else _bench_fixture()
        self.entries = entries if entries is not None else _store_fixture()
        self.now = now if now is not None else snap["captured_now"] + 120
        self.age = self.now - snap["ts"]
        self.calls = {"flags": 0, "bench": 0, "join": 0, "live": 0, "find": 0}

    # -- the seams ------------------------------------------------------
    def cached_flags(self, now=None, **kw):
        self.calls["flags"] += 1
        return dict(self.flags), self.age

    def bench_seam(self, project=None, seams=None):
        self.calls["bench"] += 1
        return ({name: row for name, row in self.bench["seats"].items()
                 if row["home_room"] == project}, None)

    def join(self, seats=None, now=None, **kw):
        self.calls["join"] += 1
        return {name: dict(self.bench["seats"][name], seat=name,
                           measured_at=now)
                for name in (seats or ()) if name in self.bench["seats"]}

    def live(self, seat, family=None, **kw):
        self.calls["live"] += 1
        row = (self.bench.get("fanout") or {}).get(seat) or {}
        return {"seat": seat, "family": family, "running": row.get("running"),
                "measured": row.get("measured", False), "window_s": 180,
                "why": None if row else "no instance tree for this seat"}

    def find(self, eid, project=None, types=None):
        self.calls["find"] += 1
        text = self.entries.get(eid)
        return {"id": eid, "type": "prior", "statement": text} if text else None

    # -- the world's own premises, DECLARED rather than inherited -------
    #
    # THE CAPTURE IS REAL DATA AND IT IS NOT A CONTRACT. It records one host's
    # burn state on one day: on the day it was taken most families' last
    # reading was eighteen hours old and one was rationed ORANGE, while the
    # capture it replaced had those same families UNMEASURED and nothing
    # rationed. Both are honest folds. Neither is a promise, and an arm below
    # that reads `partial is False`, or compares two families' RANK, is about
    # the routing answer and not about when this fleet last burned -- so it
    # asks for the property it needs here, and the next honest re-capture
    # moves none of them.

    def freshly_read(self):
        """Declare every MEASURED family's reading CURRENT.

        A reading past the freshness bound is PARTIAL by design and that is
        the verb's whole point, so an arm asserting a NON-partial answer is
        asserting that the capture happened to be young. A family with NO
        reading keeps None: unmeasured is not stale, and giving it an age
        would state a measurement nobody took."""
        flags = json.loads(json.dumps(self.flags))
        for row in flags.values():
            if row.get("reading_age_s") is not None:
                row["reading_age_s"] = 0
        self.flags = flags
        return self

    def at_colour(self, **colours):
        """Declare these families' colours, behaviour row included.

        The behaviour travels WITH the colour because the colour alone is a
        label: `route` reads the ration off the behaviour, so a world with a
        changed colour and the old behaviour would admit a family the colour
        says to wall."""
        flags = json.loads(json.dumps(self.flags))
        for family, colour in colours.items():
            if family not in flags:
                # A FAMILY THE CAPTURE DOES NOT HOLD CANNOT BE COLOURED, and
                # does not need to be: no flag row means UNMEASURED, and an
                # unmeasured family carries no ration to confound an ordering
                # arm — which is the whole reason the arms below colour the
                # ENTIRE vocabulary rather than the two families they compare.
                # DECLARED absence only. Any other unknown name is a typo, and
                # a silently skipped typo would leave an arm believing it
                # declared a world it never wrote.
                if family in _UNCAPTURED:
                    continue
                raise AssertionError(
                    "%s is not in the burn-flag capture and is not declared "
                    "uncaptured (%s): re-capture, or declare it"
                    % (family, ", ".join(_UNCAPTURED) or "none"))
            flags[family] = dict(flags[family], colour=colour,
                                 behaviour=dict(bf.BEHAVIOUR[colour]))
        self.flags = flags
        return self

    def seat_field(self, field):
        """seat -> one field of its bench row (None when unset): the resolved
        model and the context percent are planted on the bench, never read."""
        return lambda seat: (self.bench["seats"].get(seat) or {}).get(field)

    def review_sends(self, families, now=None):
        return dict(self.bench.get("review_sends") or {})

    def seams(self, **extra):
        # THE THREE task/3156 READERS, EACH SEAMED TO "NOTHING AUTHORED": no
        # light, no team, no share. That is the world every arm above was
        # written against, and it keeps the verb off this host's registry.
        out = {"cached_flags": self.cached_flags, "bench": self.bench_seam,
               "join": self.join, "fanout_live": self.live, "find": self.find,
               "light": lambda project: (None, None),
               "team": lambda project: None,
               "allocation": lambda project: {},
               # the family door for a team's seats: nothing registered, so
               # every member keeps the family its team names
               "families_of": lambda seats: {},
               "runtime_model": self.seat_field("model"),
               "context": self.seat_field("context_pct"),
               "review_sends": self.review_sends}
        out.update(extra)
        return out

    def ask(self, kind, **kw):
        seams = self.seams(**(kw.pop("seams", None) or {}))
        # THE BENCH IS PROJECT-SCOPED, so an ask with no project is an ask
        # about a project with no seats. The fixture's room is the default
        # here rather than in the verb, where a default project would be a
        # guess about whose bench the caller meant.
        kw.setdefault("project", "helm")
        report, err = route.answer(kind, now=self.now, seams=seams, **kw)
        if err:
            raise AssertionError("the verb refused the ask: %s" % err)
        return report

    def families(self, report):
        return [row["family"] for row in report["answer"]]

    def refused(self, report, family):
        for row in report["refused"]:
            if row["family"] == family:
                return row
        return None

    def admitted(self, report, family):
        for row in report["answer"]:
            if row["family"] == family:
                return row
        return None


class ProofCaseTest(unittest.TestCase):
    """F12 — the owner's own case, and the four-hour control that is the
    whole reason this verb exists."""

    def test_f12_the_answer_is_the_idle_cross_family_seat(self):
        # FRESHNESS IS DECLARED, because this arm is about WHO the answer is
        # and about exit 0 — not about how long ago the fleet last burned.
        # The capture's readings are eighteen hours old, which is PARTIAL by
        # design; inheriting that made this arm assert the capture day.
        world = World().freshly_read()
        report = world.ask("verify", frm="fable", project="helm")
        self.assertEqual(report["answer"][0]["family"], DS4PRO)
        self.assertEqual(route.exit_code(report), 0)
        self.assertFalse(report["partial"])
        # THE ASKING FAMILY IS NO LONGER DROPPED FOR BEING THE AUTHOR'S.
        # E1/E2 are a preference now, so what drops this credential here is
        # the edge that always also applied: an ORANGE native flag. The ONE
        # reported node is still ONE reason, and it is the one that would
        # have to change for this family to be a candidate.
        self.assertEqual(report["from"], NATIVE)
        dropped = world.refused(report, NATIVE)
        self.assertEqual(dropped["node"], route.N1)
        self.assertEqual([r["edge"] for r in dropped["reasons"]], ["E9"])
        self.assertEqual(dropped["colour"], bf.ORANGE)
        # and the walled family is dropped with its own expiry, not guessed
        walled = world.refused(report, CODEX)
        self.assertEqual(walled["colour"], bf.RED)
        self.assertTrue(walled["until"] > world.now)

    def test_the_authors_own_family_is_admitted_and_ranked_last(self):
        """THE LOOSENING, on a world where nothing else walls the credential.

        F12's fixture rations the native flag ORANGE, so it proves only that
        E1 stopped being the deciding edge. This declares the colour GREEN so
        that the ONLY thing that could have dropped the asking family is the
        rule that no longer does — and then pins what replaces it: admitted,
        marked, reasoned and LAST.
        """
        world = World().freshly_read().at_colour(**{NATIVE: bf.GREEN})
        report = world.ask("verify", frm="fable", project="helm")
        self.assertIsNone(world.refused(report, NATIVE))
        own = world.admitted(report, NATIVE)
        self.assertIsNotNone(
            own, "the asking family is neither in the answer nor refused")
        self.assertEqual(own["family_preference"], "same")
        edges = [r["edge"] for r in own["reasons"]]
        self.assertIn("E1", edges)
        self.assertIn("E2", edges)
        # LAST AMONG EQUALS, and the qualifier is the point: the ORANGE
        # rationing term is OUTSIDE the family term, so a family the owner
        # said to route around still sorts below the author's own healthy
        # one. The demotion orders candidates the fleet is equally free to
        # use, it does not promote a rationed family over a green one. And
        # since landing refactor item 5 an equal holds the same QUEUE: a door
        # read ranks on the queue first, so a piled peer sorts below it.
        peers = [r for r in report["answer"] if not r["critical_path_only"]
                 and r["queue_bucket"] == own["queue_bucket"]]
        self.assertGreater(len(peers), 1)
        self.assertEqual(own["rank"], max(r["rank"] for r in peers))
        # the CONTROL on the demotion: a cross-family peer is admitted in the
        # same answer, carries the other preference, and outranks it.
        other = [r for r in peers if r["family"] != NATIVE]
        self.assertTrue(other)
        self.assertEqual(other[0]["family_preference"], "other")
        self.assertLess(other[0]["rank"], own["rank"])
        self.assertFalse(own["critical_path_only"])
        text = "\n".join(route.render(report, now=world.now))
        self.assertIn("THE AUTHOR'S OWN FAMILY", text)
        self.assertIn("family same", text)

    def test_f12_control_a_four_hour_old_reading_is_PARTIAL_not_a_drop(self):
        """THE FOUR WASTED HOURS, PINNED. The integrator's failure was to
        treat an old reading as a measurement of NOW and drop the family. The
        verb says PARTIAL and keeps it."""
        # THE CONTROL ONLY MOVES FROM A FRESH BASE. With the capture's own
        # eighteen-hour readings both halves of this arm are PARTIAL and it
        # proves nothing about age at all — the one property it exists to
        # measure. So the base declares itself current and the aged half is
        # the only thing that differs.
        fresh = World().freshly_read()
        before = fresh.ask("verify", frm="fable", project="helm")
        self.assertIn(DS4PRO, fresh.families(before))   # the control moves
        self.assertFalse(before["partial"])

        aged = World().freshly_read()
        aged.flags = json.loads(json.dumps(aged.flags))
        aged.flags[DS4PRO]["reading_age_s"] = 4 * 3600
        report = aged.ask("verify", frm="fable", project="helm")
        self.assertIn(DS4PRO, aged.families(report))
        self.assertTrue(report["partial"])
        self.assertEqual(route.exit_code(report), 3)
        self.assertTrue(any(DS4PRO in why and "PARTIAL" in why
                            for why in report["partial_why"]))
        row = report["answer"][[r["family"] for r in
                                report["answer"]].index(DS4PRO)]
        self.assertTrue(row["stale_reading"])
        self.assertIn("E15", [r["edge"] for r in row["reasons"]])

    def test_f12_both_nonzero_exits_refuse_a_gating_caller_identically(self):
        """§1 #14: an advisory verb owes the AGENT the difference between
        'park it' and 're-measure'; a GATE may treat neither as consent."""
        world = World()
        empty = dict(world.ask("verify", frm="fable", project="helm"))
        empty["answer"], empty["partial"] = [], False
        self.assertEqual(route.exit_code(empty), 1)
        empty["partial"] = True
        self.assertEqual(route.exit_code(empty), 3)
        self.assertTrue(all(route.exit_code(dict(empty, partial=p,
                                                 answer=[])) != 0
                            for p in (True, False)))


class FlagAdmissionTest(unittest.TestCase):
    """F14 — RED drops, ORANGE is admitted critical-path-only, GREY is
    admitted AND SAYS SO."""

    def _world(self, **colours):
        return World().at_colour(**colours)

    def test_f14_red_drops_orange_is_rationed_grey_is_admitted(self):
        # ALL THREE COLOURS ARE DECLARED, including GREY. An inherited GREY
        # leg is whichever family the capture happens to leave unmeasured, so
        # a capture that measures it turns an arm about GREY ADMISSION into an
        # arm about the fleet's burn schedule.
        world = self._world(**{KIMI: bf.RED, GEMINI: bf.ORANGE,
                               DS4PRO: bf.GREY})
        report = world.ask("council")
        self.assertNotIn(KIMI, world.families(report))
        walled = world.refused(report, KIMI)
        self.assertEqual(walled["node"], route.N3)
        self.assertEqual([r["edge"] for r in walled["reasons"]], ["E11"])
        self.assertEqual(walled["reasons"][0]["says"],
                         bf.BEHAVIOUR[bf.RED]["say"])

        rationed = [r for r in report["answer"] if r["family"] == GEMINI]
        self.assertEqual(len(rationed), 1)
        self.assertTrue(rationed[0]["critical_path_only"])
        self.assertEqual(rationed[0]["cap"]["delegate_factor"],
                         bf.BEHAVIOUR[bf.ORANGE]["delegate_factor"])

        grey = [r for r in report["answer"] if r["family"] == DS4PRO][0]
        self.assertEqual(grey["colour"], bf.GREY)
        says = [r["says"] for r in grey["reasons"] if r["edge"] == "E12"]
        self.assertEqual(says, [bf.BEHAVIOUR[bf.GREY]["say"]])

    def test_f14_control_the_same_family_at_yellow_is_admitted(self):
        """THE CONTROL THAT MAKES THE DROP A MEASUREMENT: one colour changes
        and the same family comes back."""
        self.assertNotIn(KIMI,
                         self._world(**{KIMI: bf.RED}).families(
                             self._world(**{KIMI: bf.RED}).ask("council")))
        world = self._world(**{KIMI: bf.YELLOW})
        self.assertIn(KIMI, world.families(world.ask("council")))

    def test_f14_orange_sorts_below_an_unrationed_family(self):
        """The owner's own ORANGE wording says PREFER ANOTHER FAMILY, so the
        rank obeys the sentence rather than only printing it."""
        world = self._world(**{KIMI: bf.ORANGE})
        order = world.families(world.ask("council"))
        self.assertLess(order.index(DS4PRO), order.index(KIMI))


class EdgeResolutionTest(unittest.TestCase):
    """F8 — an id that does not resolve is an ANSWER, never a silent drop."""

    def test_f8_an_unresolvable_id_renders_UNRESOLVED_and_sets_partial(self):
        world = World()
        gone = "review-rounds-go-to-cross-family-seats-not-same-family-subagents"
        world.entries = dict(world.entries)
        world.entries.pop(gone)
        report = world.ask("verify", frm="fable", project="helm")
        self.assertIn(gone, report["unresolved"])
        self.assertTrue(report["partial"])
        self.assertEqual(route.exit_code(report), 3)
        # THE EDGE STILL RESOLVES even though E1 no longer drops anything:
        # the same-family preference is recorded where it is DETECTED, so a
        # candidate a later edge walls still reports a store id nobody can
        # resolve. Resolving E1 only in the admit branch would have made this
        # unresolved id invisible on exactly the path that walls the family.
        self.assertIsNone(world.admitted(report, NATIVE))
        self.assertIsNotNone(world.refused(report, NATIVE))
        text = "\n".join(route.render(report, now=world.now))
        self.assertIn("UNRESOLVED %s" % gone, text)

    def test_f8_control_the_same_run_with_the_id_present_is_not_partial(self):
        # THE CONTROL IS ABOUT THE UNRESOLVED ID AND NOTHING ELSE, so the only
        # other thing that can set `partial` — a reading past the freshness
        # bound — is declared away rather than left to the capture's age.
        world = World().freshly_read()
        report = world.ask("verify", frm="fable", project="helm")
        self.assertEqual(report["unresolved"], [])
        self.assertFalse(report["partial"])

    def test_f8_every_reason_text_comes_from_the_store_not_from_python(self):
        """The law the module's own docstring states, asserted rather than
        described: swap the store and every sentence changes."""
        world = World()
        world.entries = {eid: "SWAPPED %s" % eid for eid in world.entries}
        report = world.ask("council")
        said = [r["says"] for row in report["answer"] for r in row["reasons"]
                if r["resolved"] and not r["source"].startswith("burnflags.")]
        self.assertTrue(said)
        self.assertTrue(all(text.startswith("SWAPPED ") for text in said))

    def test_f8_the_unexpressed_edge_names_its_source_and_ships_no_number(self):
        """E25: that family's bar is a daily dollar rate on a reader nothing
        builds. It renders UNEXPRESSED with its sources and no threshold."""
        world = World()
        report = world.ask("council")
        self.assertIn("E25", report["unexpressed"])
        row = [r for r in report["answer"] if r["family"] == OPENROUTER][0]
        unexpressed = [r for r in row["reasons"] if r["edge"] == "E25"]
        self.assertEqual(len(unexpressed), 1)
        self.assertIsNone(unexpressed[0]["says"])
        for sid in route.EDGE["E25"]["unexpressed"]:
            self.assertIn(sid, unexpressed[0]["source"])
        self.assertNotIn("E25", [e["id"] for e in route.EDGES
                                 if e["kind"] == "store"])

    def test_the_default_resolver_is_the_trees_typed_first_resolver(self):
        """THE CONTROL FOR EVERY FIXTURE-FED ARM ABOVE. Those arms prove the
        sentence came from whatever `find` returned; this proves that with no
        seam the `find` is the store's own typed-first door, and that a typed
        spelling reaches it intact."""
        from helm.store import load as store_load
        seen = []

        def spy(eid, project=None, types=None):
            seen.append((eid, project))
            return {"statement": "from the real door"}

        with mock.patch.object(store_load, "find_typed", spy):
            text, ok = route.resolve("prior:some-id", project="helm")
        self.assertTrue(ok)
        self.assertEqual(text, "from the real door")
        self.assertEqual(seen, [("prior:some-id", "helm")])

    def test_every_edge_carries_a_source_or_declares_why_it_cannot(self):
        """A STRUCTURAL ARM, because the CONTENT arm cannot run here: whether
        an id resolves is a fact about the owner's store on this host, and an
        arm asserting it would go red on a machine with no store rather than
        on a defect. What this pins is that no edge is a bare sentence."""
        for edge in route.EDGES:
            if edge["kind"] == "store":
                self.assertTrue(edge["sources"], edge["id"])
            elif edge["kind"] == "unexpressed":
                self.assertTrue(edge.get("unexpressed"), edge["id"])
            else:
                self.assertEqual(edge["kind"], "behaviour", edge["id"])
                self.assertFalse(edge["sources"], edge["id"])
            self.assertIn(edge["node"], route.NODES)
        self.assertEqual(len(route.EDGE), len(route.EDGES))

    def test_the_retired_cap_is_named_as_retired_and_never_carried(self):
        """§1 #20: the graph holds ONE cap. The superseded fleet-wide spelling
        is recorded on the edge as retired and is not a second source."""
        edge = route.EDGE["E17"]
        self.assertEqual(len(edge["sources"]), 1)
        self.assertTrue(edge["retired"])
        self.assertNotIn(edge["retired"][0], edge["sources"])
        every = [sid for e in route.EDGES for sid in e["sources"]]
        self.assertNotIn(edge["retired"][0], every)


class BrokenSeatTest(unittest.TestCase):
    """task/3546: a seat the dispatch door refuses as BROKEN (an operator
    hold or a drop storm) is never the router's recommendation."""

    def test_a_broken_seat_is_dropped_at_N4_and_named(self):
        world = World().freshly_read()
        control = world.ask("verify", frm="fable", project="helm",
                            seams={"broken": lambda names: {}})
        self.assertEqual(control["answer"][0]["family"], DS4PRO)
        report = world.ask("verify", frm="fable", project="helm", seams={
            "broken": lambda names: {"seat-c": ["HOLD"]}
            if "seat-c" in names else {}})
        self.assertIsNone(world.admitted(report, DS4PRO))
        self.assertEqual(world.refused(report, DS4PRO)["node"], route.N4)
        self.assertEqual(report["broken"],
                         [{"seat": "seat-c", "facts": ["HOLD"]}])


class CapTest(unittest.TestCase):
    """F21 — the cap arithmetic, and the null that reports the cap only."""

    def test_f21_may_run_is_cap_times_factor_minus_running(self):
        row = route.cap(DS4PRO, bf.GREEN, running=1, measured=True)
        self.assertEqual((row["cap"], row["allowed"], row["may_run"]),
                         (route.MASTER_CAP, 4, 3))

    def test_f21_codex_family_caps_at_three(self):
        row = route.cap(CODEX, bf.GREEN, running=0, measured=True)
        self.assertEqual(row["cap"], route.CODEX_CAP)
        self.assertEqual(row["may_run"], 3)
        other = route.cap(KIMI, bf.GREEN, running=0, measured=True)
        self.assertEqual(other["may_run"], 4)       # the control: 4 elsewhere

    def test_f21_a_null_running_reports_the_cap_and_no_may_run(self):
        row = route.cap(DS4PRO, bf.GREY, running=None, measured=False)
        self.assertIsNone(row["may_run"])
        self.assertIsNone(row["running"])
        self.assertFalse(row["measured"])
        self.assertEqual(row["allowed"], 2)         # GREY rations like yellow
        # and an UNMEASURED reading that carries a number is still unmeasured
        lying = route.cap(DS4PRO, bf.GREY, running=0, measured=False)
        self.assertIsNone(lying["may_run"])
        self.assertIsNone(lying["running"])

    def test_f21_the_colour_rations_and_the_floor_is_zero(self):
        self.assertEqual(route.cap(KIMI, bf.ORANGE, running=0,
                                   measured=True)["may_run"], 1)
        self.assertEqual(route.cap(KIMI, bf.RED, running=0,
                                   measured=True)["may_run"], 0)
        self.assertEqual(route.cap(KIMI, bf.GREEN, running=9,
                                   measured=True)["may_run"], 0)

    def test_f21_the_rendered_line_says_UNMEASURED_rather_than_a_number(self):
        world = World()
        report = world.ask("verify", frm="fable", project="helm")
        text = "\n".join(route.render(report, now=world.now))
        self.assertIn("delegates UNMEASURED (cap 2)", text)
        self.assertIn("delegates 1", text)          # the measured control


class ReviewersAgreementTest(unittest.TestCase):
    """F16 — route and `helm reviewers` name the same seats, or route says
    where they part."""

    def _eligibility(self, seats, unreadable=None):
        def fn(rid, **kw):
            return {"row": {"id": rid}, "eligible": list(seats),
                    "unreadable": dict(unreadable or {})}, None
        return fn

    def test_f16_route_explains_a_seat_the_reviewers_verb_still_admits(self):
        """The reviewers verb has no opinion about FAMILY money. When the two
        part, route names the node that parted them."""
        world = World()
        seam = self._eligibility(["seat-c", "seat-b"])
        report = world.ask("review", project="helm", row="abc123",
                           seams={"eligibility": seam})
        rv = report["reviewers"]
        self.assertEqual(rv["eligible"], ["seat-c", "seat-b"])
        parted = [d for d in rv["difference"] if d["seat"] == "seat-b"]
        self.assertEqual(len(parted), 1)
        self.assertEqual(parted[0]["side"], "reviewers-only")
        self.assertEqual(parted[0]["node"], route.N3)
        self.assertIn("seat-c", [r["seat"] for r in report["answer"]])

    def test_f16_control_with_the_family_unwalled_the_sets_agree(self):
        world = World()
        flags = json.loads(json.dumps(world.flags))
        flags[CODEX] = dict(flags[CODEX], colour=bf.YELLOW,
                              behaviour=dict(bf.BEHAVIOUR[bf.YELLOW]))
        world.flags = flags
        seam = self._eligibility(["seat-c", "seat-b"])
        report = world.ask("review", project="helm", row="abc123",
                           seams={"eligibility": seam})
        parted = [d for d in report["reviewers"]["difference"]
                  if d["seat"] == "seat-b"]
        self.assertEqual(parted, [])

    def test_f16_an_unreadable_row_makes_the_answer_partial(self):
        def refuses(rid, **kw):
            return None, "that row id does not resolve"
        world = World()
        report = world.ask("review", project="helm", row="nope",
                           seams={"eligibility": refuses})
        self.assertTrue(report["partial"])
        self.assertEqual(route.exit_code(report), 3)
        self.assertIsNone(report["reviewers"]["eligible"])

    def test_f16_the_row_leg_is_only_asked_when_a_row_is_given(self):
        calls = []

        def spy(rid, **kw):
            calls.append(rid)
            return {"eligible": [], "unreadable": {}}, None
        world = World()
        world.ask("review", project="helm", seams={"eligibility": spy})
        self.assertEqual(calls, [])                 # no row, no expensive read
        world.ask("review", project="helm", row="r1",
                  seams={"eligibility": spy})
        self.assertEqual(calls, ["r1"])


class NoProbeTest(unittest.TestCase):
    """F7 — the verb never spawns, never probes a vendor, and never blocks."""

    def test_f7_no_probe_door_is_touched_and_the_answer_is_fast(self):
        import socket
        import subprocess
        import urllib.request
        fired = []

        def refuse(name):
            def door(*a, **kw):
                fired.append(name)
                raise AssertionError("the verb reached %s" % name)
            return door

        world = World()
        with mock.patch.object(socket, "socket", refuse("socket")), \
                mock.patch.object(socket, "create_connection",
                                  refuse("create_connection")), \
                mock.patch.object(subprocess, "Popen", refuse("Popen")), \
                mock.patch.object(subprocess, "run", refuse("run")), \
                mock.patch.object(urllib.request, "urlopen",
                                  refuse("urlopen")):
            # task/3465: the bound is CPU time — under a loaded gate the wall
            # clock grades the box, not the in-process answer.
            started = time.process_time()
            report = world.ask("verify", frm="fable", project="helm")
            elapsed = time.process_time() - started
            # THE MUST-HIT CONTROL: the spies are installed and DO fire, so a
            # green arm above is the verb's silence and not a dead patch.
            for door in (socket.socket, subprocess.Popen,
                         urllib.request.urlopen):
                self.assertRaises(AssertionError, door)
        self.assertEqual(sorted(set(fired)),
                         ["Popen", "socket", "urlopen"])
        self.assertTrue(report["answer"])
        self.assertLess(elapsed, 5.0)

    def test_f7_the_modules_import_no_network_or_spawn_machinery(self):
        """The arm above proves it for the SEAMED path; this proves it for
        the default one, which no arm may exercise against the live fleet."""
        for name in ("route", "fanout"):
            path = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "helm", "%s.py" % name)
            with open(path) as fh:
                source = fh.read()
            for banned in ("import socket", "import subprocess",
                           "import urllib", "import http", "requests"):
                self.assertNotIn(banned, source, "%s names %s" % (name, banned))

    def test_f7_every_reader_is_asked_at_most_once(self):
        """A routing answer describes ONE instant. A seam asked twice is two
        instants in one table, which is the defect `eligibility`'s scope
        exists to close."""
        world = World()
        world.ask("verify", frm="fable", project="helm")
        self.assertEqual(world.calls["flags"], 1)
        self.assertEqual(world.calls["bench"], 1)
        self.assertEqual(world.calls["join"], 1)


class BenchAndPolicyTest(unittest.TestCase):
    """N1 and N2 — the owner's rulings, and the project's own bench."""

    def test_a_seat_homed_in_another_project_is_not_a_candidate(self):
        world = World()
        report = world.ask("council", project="helm")
        seats = [row["seat"] for row in report["answer"]]
        self.assertNotIn("seat-h", seats)
        self.assertIn("seat-f", seats)

    def test_a_native_seat_joins_its_bench_through_the_register(self):
        """MEASURED ON THE LIVE FLEET, and it cost the answer its shape: the
        native credential has no proxy, so the usability join answers None and
        the spawn-register resolver answers None, and the family with every
        claude seat in the project had NO BENCH AT ALL. The asking family was
        then dropped at BENCH rather than at the policy that excludes it — a
        true refusal under the wrong node, which sends a reader to the wrong
        repair. The third door reads the register's own VERIFIED runtime and
        translates its spelling into the flag vocabulary."""
        world = World()
        bench = json.loads(json.dumps(world.bench))
        row = bench["seats"]["seat-a"]
        row.pop("family_index")                       # no join answer, no
        row["runtime"] = {"family": "claude",   # noqa: SEAT_NAME — the
                          # REGISTER'S harness word for the native runtime,
                          # which is the whole subject: it is not this
                          # vocabulary's spelling and not any seat's name.
                          "backend": "native"}
        row["runtime_verified"] = True
        world.bench = bench
        world.bench["seats"]["seat-a"]["family"] = None
        report = world.ask("council", project="helm")
        self.assertEqual(report["bench"].get(NATIVE), ["seat-a"])

    def test_an_unverified_runtime_is_display_evidence_and_no_authority(self):
        """THE CONTROL ON THE DOOR ABOVE, and the reason it is a door rather
        than a read: a roster row written under another seat's name seeds the
        runtime block without minting authority, so an unverified one is
        skipped and the seat simply has no family here."""
        world = World()
        bench = json.loads(json.dumps(world.bench))
        row = bench["seats"]["seat-a"]
        row.pop("family_index")
        row["runtime"] = {"family": "claude",   # noqa: SEAT_NAME — the
                          # register's harness word, as above.
                          "backend": "native"}
        # THE POSITIVE CONTROL: the identical row WITH the stamp does join a
        # bench, so the absence below is the stamp's doing and not a fixture
        # this arm failed to wire.
        row["runtime_verified"] = True
        world_ok = World()
        world_ok.bench = json.loads(json.dumps(bench))
        world_ok.bench["seats"]["seat-a"]["family"] = None
        self.assertEqual(
            world_ok.ask("council", project="helm")["bench"].get(NATIVE),
            ["seat-a"])
        row["runtime_verified"] = False
        world.bench = bench
        world.bench["seats"]["seat-a"]["family"] = None
        report = world.ask("council", project="helm")
        self.assertNotIn(NATIVE, report["bench"])

    def test_an_unreadable_register_is_partial_never_an_empty_fleet(self):
        def refuses(project=None, seams=None):
            return {}, "the seat register did not read"
        world = World()
        report = world.ask("council", project="helm",
                           seams={"bench": refuses})
        self.assertEqual(report["answer"], [])
        self.assertTrue(report["partial"])
        self.assertEqual(route.exit_code(report), 3)
        self.assertIn("the seat register did not read", report["partial_why"])

    def test_a_seat_that_cannot_work_drops_its_family_at_the_live_node(self):
        world = World()
        bench = json.loads(json.dumps(world.bench))
        bench["seats"]["seat-c"]["can_take_work"] = False
        world.bench = bench
        report = world.ask("council")
        self.assertNotIn(DS4PRO, world.families(report))
        self.assertEqual(world.refused(report, DS4PRO)["node"], route.N4)

    def test_a_DEAF_only_seat_keeps_its_family_at_the_live_node(self):
        """task/3055 A3, THE RED-FIRST ARM. The dispatch door files a row for
        a seat whose ONLY refusal is that helm cannot wake it, so the router
        must not call its family seatless: it is a candidate, ranked last."""
        world = World()
        bench = json.loads(json.dumps(world.bench))
        bench["seats"]["seat-c"].update(can_take_work=False, reachable=False,
                                        refusals=["reachable"])
        world.bench = bench
        report = world.ask("council")
        self.assertIsNone(world.refused(report, DS4PRO),
                          "a DEAF-only seat dropped its family at N4")
        row = world.admitted(report, DS4PRO)
        self.assertEqual("seat-c", row["seat"])
        self.assertEqual(2, row["reach_rank"])

    def test_CONTROL_a_DEAF_seat_with_a_second_refusal_still_drops(self):  # noqa: VACUOUS_ASSERTION — the refusal row's node N4 is a positive observable; its mirror is test_a_DEAF_only_seat_keeps_its_family_at_the_live_node
        world = World()
        bench = json.loads(json.dumps(world.bench))
        bench["seats"]["seat-c"].update(can_take_work=False, reachable=False,
                                        refusals=["reachable", "upstream"])
        world.bench = bench
        report = world.ask("council")
        self.assertEqual(route.N4, world.refused(report, DS4PRO)["node"])

    def test_a_DEAF_only_seat_ranks_after_a_reachable_one_in_its_family(self):  # noqa: VACUOUS_ASSERTION — the admitted seat's name is the positive observable; the DEAF seat would win on idleness alone
        """Listed last, not first: the DEAF seat is IDLE and its sibling is
        RUNNING, so idleness alone would pick the seat helm cannot wake."""
        world = World()
        bench = json.loads(json.dumps(world.bench))
        bench["seats"]["seat-c2"] = dict(bench["seats"]["seat-c"],
                                         pane="RUNNING")
        bench["seats"]["seat-c"].update(can_take_work=False, reachable=False,
                                        refusals=["reachable"])
        world.bench = bench
        report = world.ask("council")
        self.assertEqual("seat-c2", world.admitted(report, DS4PRO)["seat"])

    def test_a_busy_seat_is_ranked_down_and_never_excluded(self):
        """E14 — capacity is LIVE seats, not idle seats."""
        world = World()
        report = world.ask("council")
        self.assertIn(KIMI, world.families(report))    # RUNNING, admitted
        row = [r for r in report["answer"] if r["family"] == KIMI][0]
        self.assertEqual(row["pane"], "RUNNING")

    def test_the_asking_family_is_dropped_only_where_authorship_binds(self):
        world = World()
        cross = world.ask("verify", frm="fable", project="helm")
        self.assertIsNotNone(world.refused(cross, NATIVE))
        # `research` is not a closing read, so the author is not excluded for
        # being the author — it is excluded by its own rule instead
        other = world.ask("research", frm=DS4PRO, project="helm")
        dropped = world.refused(other, DS4PRO)
        self.assertIsNone(dropped)

    def test_the_declared_short_tasks_family_gets_a_council_never_a_lane(self):
        world = World()
        self.assertIn(GROK, world.families(world.ask("council")))
        built = world.ask("build", frm=CODEX, project="helm")
        self.assertEqual(world.refused(built, GROK)["node"], route.N3)
        self.assertEqual([r["edge"] for r in
                          world.refused(built, GROK)["reasons"]], ["E26"])

    def test_the_two_rating_axes_order_two_kinds_differently(self):
        """E7's own law: the question is never who is best, but whether the
        work is judgment-bound or volume-bound."""
        # A RATION IS A DIFFERENT ORDERING RULE AND WOULD CONFOUND THIS ONE.
        # `route` sinks a rationed family below every unrationed one whatever
        # its rating, so a capture that happens to find one of the two
        # compared families at ORANGE answers this arm with the ration and
        # never reaches the axis. Both sides are declared unrationed, and the
        # only thing left to order them is the rating this arm is about.
        # EVERY family is declared, not only the two this arm compares: the
        # confound is a ration ANYWHERE in the vocabulary, so naming one
        # family leaves the arm at the mercy of whichever other family a
        # capture happens to find rate-limited.
        world = World().at_colour(**{f: bf.YELLOW for f in bf.families()})
        judgment = world.families(world.ask("council"))
        volume = world.families(world.ask("delegate", frm=CODEX))
        self.assertLess(judgment.index(KIMI), judgment.index(DS4PRO))
        self.assertLess(volume.index(GEMINI), volume.index(KIMI))
        self.assertNotEqual(judgment, volume)

    def test_an_unrated_family_sorts_last_and_is_never_given_a_number(self):
        # SAME CONFOUND, OTHER DIRECTION: a RATIONED family sorts below an
        # unrated one, so a capture that rations a rated family makes "unrated
        # sorts last" false for a reason that has nothing to do with rating.
        # The world declares NO family rationed and the tail is then the
        # rating's. Naming one family is not enough: any rationed family sinks
        # below the unrated one and takes the tail for a reason that has
        # nothing to do with rating.
        world = World().at_colour(**{f: bf.YELLOW for f in bf.families()})
        order = world.families(world.ask("council"))
        self.assertEqual(order[-2:], [GROK, OPENROUTER])
        self.assertNotIn(GROK, route.SMARTS)
        self.assertNotIn(GROK, route.SPEED)

    def test_no_snapshot_at_all_is_partial_and_answers_nobody_on_colour(self):
        def empty(now=None, **kw):
            return {}, None
        world = World()
        report = world.ask("council", seams={"cached_flags": empty})
        self.assertTrue(report["partial"])
        self.assertEqual(route.exit_code(report), 3)
        # every family is UNMEASURED, and unmeasured is admitted, not walled
        self.assertEqual(sorted(report["unmeasured"]),
                         sorted(world.families(report)))


class TeamAndLightTest(unittest.TestCase):
    """task/3156: N0b LIGHT, the authored team as the bench, and the share of
    a short family as the project's own colour at N3 and N5."""

    RED_LIGHT = {"colour": "red", "authored": True, "reason": "frozen",
                 "by": "owner", "ts": 1}

    def light(self, lit):
        return lambda project: (lit, None)

    def team(self, *members):
        return lambda project: {
            "project": project, "authored": True, "v": 3, "by": "owner",
            "members": [{"seat": s, "family": f, "role": r}
                        for s, f, r in members], "shares": {}}

    def test_n0b_a_red_light_refuses_build_and_admits_review(self):
        world = World().freshly_read()
        report = world.ask("build", seams={"light": self.light(self.RED_LIGHT)})
        self.assertTrue(report["light"]["refuses"])
        self.assertEqual(report["answer"], [])
        self.assertEqual(route.exit_code(report), 1)
        self.assertEqual(world.calls["bench"], 0, "nothing past N0b was asked")
        text = "\n".join(route.render(report, now=world.now))
        self.assertIn(route.N0B, text)
        self.assertIn("starts nothing new", text)
        # a review finishes work already in flight: admitted
        report = world.ask("review", seams={"light": self.light(self.RED_LIGHT)})
        self.assertFalse(report["light"]["refuses"])
        self.assertTrue(report["answer"])
        self.assertIn("light RED", "\n".join(route.render(report,
                                                          now=world.now)))

    def test_n0b_CONTROL_the_scans_red_is_no_decision(self):
        world = World().freshly_read()
        scan = dict(self.RED_LIGHT, authored=False)
        report = world.ask("build", seams={"light": self.light(scan)})
        # the scan's colour decides nothing, and an unopted answer carries
        # no light at all (the zero-config arm below pins it against trunk)
        self.assertNotIn("light", report)
        self.assertEqual(world.calls["bench"], 1)

    def test_an_authored_team_puts_a_seat_homed_elsewhere_on_this_bench(self):
        """TODAY N2 REFUSES IT: the derived bench is the home room, and a
        reviewer homed in another project's room is not on this one's. The
        owner's team is what puts it there."""
        world = World().freshly_read()
        report = world.ask("review", project="another-project")
        dropped = world.refused(report, DS4PRO)
        self.assertEqual(dropped["node"], route.N2)
        self.assertNotIn("bench_source", report)          # unopted: trunk's
        report = world.ask("review", project="another-project", seams={
            "team": self.team(("seat-h", KIMI, "lead"),
                              ("seat-c", DS4PRO, "reviewer"))})
        self.assertIsNone(world.refused(report, DS4PRO))
        self.assertIn("seat-c", [r["seat"] for r in report["answer"]])
        self.assertEqual(report["bench_source"], "team v3")

    def test_a_role_that_does_not_take_the_kind_keeps_the_seat_off(self):
        world = World().freshly_read()
        report = world.ask("review", project="another-project", seams={
            "team": self.team(("seat-h", KIMI, "lead"),
                              ("seat-c", DS4PRO, "checker"))})
        self.assertEqual(world.refused(report, DS4PRO)["node"], route.N2)
        self.assertEqual(report["team"]["filtered"],
                         [{"seat": "seat-c", "role": "checker"}])
        text = "\n".join(route.render(report, now=world.now))
        self.assertIn("not taking review by role: @seat-c (checker)", text)

    def test_a_team_member_the_join_cannot_name_takes_the_owners_family(self):
        """A seat the usability join has no row for (not started yet, or
        native) lands on the family its team record names."""
        class NoRegister:
            @staticmethod
            def registered_seat_family(name):
                return None, None
        world = World().freshly_read()
        report = world.ask("council", project="another-project", seams={
            "seatmod": NoRegister,
            "team": self.team(("seat-h", KIMI, "lead"),
                              ("seat-q", QWEN27, "reviewer"))})
        self.assertIn("seat-q", report["bench"].get(QWEN27, []))

    def test_the_family_door_rules_the_bench_over_the_authored_word(self):
        """D3 (the task/3156 design read; the cross-family read's Q4
        seam). A NATIVE seat written onto a team as codex: the join cannot name a native seat's
        family (it has no proxy), so the authored word decided and the seat
        landed on the codex bench, the cross-family reviewer. The family
        door names what it spends, and that rules the bench."""
        class NoRegister:
            @staticmethod
            def registered_seat_family(name):
                return None, None
        world = World().freshly_read()

        def join(seats=None, now=None, **kw):
            rows = world.join(seats=seats, now=now)
            rows["seat-n"] = {"seat": "seat-n", "family": None,
                              "can_take_work": None, "measured_at": now}
            return rows
        team = self.team(("seat-h", KIMI, "lead"),
                         ("seat-n", CODEX, "reviewer"))
        report = world.ask("review", project="another-project", seams={
            "join": join, "seatmod": NoRegister, "team": team,
            "families_of": lambda seats: {"seat-n": NATIVE}})
        self.assertNotIn("seat-n", report["bench"].get(CODEX, []))
        self.assertIn("seat-n", report["bench"].get(NATIVE, []))
        self.assertEqual(report["team"]["family_differs"],
                         [{"seat": "seat-n", "authored": CODEX,
                           "spends": NATIVE}])
        text = "\n".join(route.render(report, now=world.now))
        self.assertIn("@seat-n is on the team as codex and spends anthropic",
                      text)
        # CONTROL: a seat the family door cannot name keeps the typed family
        report = world.ask("review", project="another-project", seams={
            "join": join, "seatmod": NoRegister, "team": team,
            "families_of": lambda seats: {}})
        self.assertIn("seat-n", report["bench"].get(CODEX, []))
        self.assertEqual(report["team"]["family_differs"], [])

    def dark_join(self, world, *dark):
        """The frozen join, with `dark` seats UNUSABLE (N4 would drop them)."""
        def join(seats=None, now=None, **kw):
            rows = world.join(seats=seats, now=now)
            for name in dark:
                if name in rows:
                    rows[name] = dict(rows[name], can_take_work=False,
                                      verdict="UNUSABLE")
            return rows
        return join

    def test_every_dark_member_who_takes_the_kind_falls_back(self):
        """D5 (the task/3156 design read; the cross-family read's Q1
        seam): when every member of an authored team who takes this kind is dark, the answer
        was empty while the project's other seats sat idle. The derived bench
        answers instead, without the team's own seats, and the answer says
        so."""
        world = World().freshly_read().at_colour(**{
            f: bf.YELLOW for f in bf.families()})
        team = self.team(("seat-c", DS4PRO, "reviewer"))
        report = world.ask("review", seams={
            "team": team, "join": self.dark_join(world, "seat-c")})
        self.assertTrue(report["bench_source"].startswith("derived bench"),
                        report["bench_source"])
        self.assertIn("every member of team v3 who takes review is dark",
                      report["team"]["fallback"])
        self.assertIn("@seat-c", report["team"]["fallback"])
        self.assertTrue(report["answer"])
        self.assertNotIn("seat-c", [r["seat"] for r in report["answer"]])
        self.assertEqual(world.calls["bench"], 1)
        text = "\n".join(route.render(report, now=world.now))
        self.assertIn("is dark (@seat-c); the derived bench answered", text)
        # CONTROL: with the reviewer able to work, the team answers alone
        report = world.ask("review", seams={"team": team})
        self.assertEqual(report["bench_source"], "team v3")
        self.assertIsNone(report["team"].get("fallback"))
        self.assertEqual(world.calls["bench"], 1, "the derived bench was read")

    def test_takers_refused_at_n1_or_red_at_n3_fall_back_too(self):
        """Round 3, ruling b: the same fallback when the team's only takers
        are refused at N1 (the native credential is the asker, E9) or sit on
        a RED family at N3. The answer was empty and said nothing about the
        project's other seats; now the derived bench answers without the
        team's own seats, and the why names each taker and what refused
        it."""
        world = World().freshly_read().at_colour(**{
            f: bf.YELLOW for f in bf.families()})
        # SINCE LANDING REFACTOR ITEM 5 a native seat reads a door as a
        # fresh-context Opus (E34), so the N1 refusal needs a Fable seat
        world.bench = json.loads(json.dumps(world.bench))
        world.bench["seats"]["seat-a"]["model"] = "claude-fable-5-1"
        report = world.ask("review", seams={
            "team": self.team(("seat-a", NATIVE, "lead"))})
        why = report["team"]["fallback"]
        self.assertIn("every member of team v3 who takes review is refused",
                      why)
        self.assertIn("@seat-a (anthropic, N1 E9)", why)
        self.assertTrue(report["answer"])
        self.assertNotIn("seat-a", [r["seat"] for r in report["answer"]])
        self.assertIn(why, "\n".join(route.render(report, now=world.now)))
        # a RED family at N3
        world = World().freshly_read().at_colour(**dict(
            {f: bf.YELLOW for f in bf.families()}, **{CODEX: bf.RED}))
        report = world.ask("build", seams={
            "team": self.team(("seat-b", CODEX, "builder"))})
        self.assertIn("@seat-b (codex RED, N3 E11)",
                      report["team"]["fallback"])
        self.assertTrue(report["answer"])
        # CONTROL: one taker the team can use, and the team answers alone
        report = world.ask("build", seams={
            "team": self.team(("seat-b", CODEX, "builder"),
                              ("seat-c", DS4PRO, "builder"))})
        self.assertEqual(report["bench_source"], "team v3")
        self.assertIsNone(report["team"]["fallback"])
        self.assertEqual(world.families(report), [DS4PRO])

    def test_an_empty_answer_after_the_fallback_is_never_silent(self):
        """Nothing on the derived bench either: the answer is empty, and it
        still says which taker was refused where, and that the derived bench
        was asked."""
        world = World().freshly_read().at_colour(**dict(
            {f: bf.YELLOW for f in bf.families()}, **{KIMI: bf.RED}))
        report = world.ask("council", project="another-project", seams={
            "team": self.team(("seat-h", KIMI, "reviewer"))})
        self.assertEqual(report["answer"], [])
        self.assertIn("@seat-h (kimi RED, N3 E11)",
                      report["team"]["fallback"])
        text = "\n".join(route.render(report, now=world.now))
        self.assertIn("the derived bench answered", text)
        self.assertIn("@seat-h (kimi RED, N3 E11)", text)
        self.assertIn("none  no family on this bench", text)

    def test_a_team_with_no_member_for_the_kind_falls_back(self):
        world = World().freshly_read().at_colour(**{
            f: bf.YELLOW for f in bf.families()})
        report = world.ask("review", seams={
            "team": self.team(("seat-c", DS4PRO, "checker"))})
        self.assertIn("no member of team v3 takes review",
                      report["team"]["fallback"])
        self.assertTrue(report["answer"])
        # the team's own seat stays off: its role does not take the kind
        self.assertNotIn("seat-c", [r["seat"] for r in report["answer"]])

    def share(self, colour, ratio, family_colour=bf.ORANGE):
        return {CODEX: {"mode": "rate", "share": 30, "shares_total": 100,
                        "over_promised": False, "budget_per_h": 13.9e6,
                        "burn_per_h": 13.9e6 * ratio, "ratio": ratio,
                        "colour": colour, "family_colour": family_colour,
                        "rationed": True, "say": bf.BEHAVIOUR[colour]["say"],
                        "shared": [], "burn_measured": True}}

    def ask_build(self, share):
        world = World().freshly_read().at_colour(**{CODEX: bf.ORANGE})
        return world, world.ask("build", seams={
            "team": self.team(("seat-a", NATIVE, "lead"),
                              ("seat-b", CODEX, "builder")),
            "allocation": lambda project: share})

    def test_the_cap_follows_the_projects_own_colour(self):
        world, report = self.ask_build(self.share(bf.YELLOW, 0.9))
        row = world.admitted(report, CODEX)
        self.assertEqual(row["colour"], bf.YELLOW)
        self.assertEqual(row["family_colour"], bf.ORANGE)
        self.assertFalse(row["critical_path_only"])
        self.assertEqual(row["cap"]["delegate_factor"],
                         bf.BEHAVIOUR[bf.YELLOW]["delegate_factor"])
        self.assertIn("E33", [r["edge"] for r in row["reasons"]])
        text = "\n".join(route.render(report, now=world.now))
        self.assertIn("share codex 30% → 13.9M/h budget, burning 12.5M/h "
                      "(0.90×) → YELLOW for helm", text)
        # CONTROL: the same family with no team reads its fleet colour
        world = World().freshly_read().at_colour(**{CODEX: bf.ORANGE})
        plain = world.admitted(world.ask("build"), CODEX)
        self.assertEqual(plain["colour"], bf.ORANGE)
        self.assertEqual(plain["cap"]["delegate_factor"],
                         bf.BEHAVIOUR[bf.ORANGE]["delegate_factor"])
        self.assertNotIn("E33", [r["edge"] for r in plain["reasons"]])

    def test_past_twice_its_budget_the_project_starts_nothing_new_there(self):
        world, report = self.ask_build(self.share(bf.RED, 2.4))
        dropped = world.refused(report, CODEX)
        self.assertEqual(dropped["node"], route.N3)
        self.assertEqual(dropped["colour"], bf.RED)
        self.assertEqual([r["edge"] for r in dropped["reasons"]][:1], ["E33"])
        text = "\n".join(route.render(report, now=world.now))
        self.assertIn("(2.4×) → RED for helm", text)

    def slot(self, queued):
        """A local family's slots row for this project (`teams._slot_row`
        joined to the allocation row): 30% of 10 lanes, 3 slots."""
        in_use = 3 if queued else 1
        return {QWEN27: {"mode": "slots", "share": 30, "shares_total": 100,
                         "over_promised": False, "budget_per_h": None,
                         "burn_per_h": None, "ratio": in_use / 3.0,
                         "colour": bf.YELLOW, "family_colour": bf.YELLOW,
                         "rationed": False,
                         "say": bf.BEHAVIOUR[bf.YELLOW]["say"],
                         "shared": ["seat-q"], "burn_measured": False,
                         "capacity": 10, "slots": 3.0, "in_use": in_use,
                         "in_use_fleet": 7, "queued": queued,
                         "capacity_measured": True}}

    def test_a_local_lane_past_the_projects_slots_is_queued_not_refused(self):
        """The owner's slots direction: route offers a local reviewer to a
        project whose team holds it; one row past its slots is QUEUED —
        still offered, ranked with ORANGE, and the line says why."""
        world = World().freshly_read().at_colour(**{
            f: bf.YELLOW for f in bf.families()})
        team = self.team(("seat-h", KIMI, "lead"),
                         ("seat-q", QWEN27, "reviewer"),
                         ("seat-c", DS4PRO, "reviewer"))
        report = world.ask("council", seams={
            "team": team, "allocation": lambda project: self.slot(True)})
        self.assertIsNone(world.refused(report, QWEN27))
        row = world.admitted(report, QWEN27)
        self.assertTrue(row["share"]["queued"])
        self.assertIn("E33", [r["edge"] for r in row["reasons"]])
        self.assertEqual(world.families(report)[-1], QWEN27)
        text = "\n".join(route.render(report, now=world.now))
        self.assertIn("slots qwen27 30% of 10 lanes → 3 slots, 3 in use "
                      "(fleet 7 of 10) → YELLOW for helm; QUEUED — new work "
                      "waits for a lane, it is not refused", text)
        # CONTROL: inside its slots the same seat is offered with no E33
        report = world.ask("council", seams={
            "team": team, "allocation": lambda project: self.slot(False)})
        row = world.admitted(report, QWEN27)
        self.assertFalse(row["share"]["queued"])
        self.assertNotIn("E33", [r["edge"] for r in row["reasons"]])

    def test_the_approval_tier_still_decides_who_closes_a_review(self):
        """SLOTS ALLOCATE LANES; THEY DO NOT APPROVE A READER. A review CLOSES
        a row, and the owner's approval tier (E3) says which families may;
        the owner's words are "once these local reviewer lanes are confirmed
        useful". A local family on the team is dropped at N1 for review until
        the tier admits it, whatever its slots say."""
        world = World().freshly_read()
        report = world.ask("review", seams={
            "team": self.team(("seat-h", KIMI, "lead"),
                              ("seat-q", QWEN27, "reviewer")),
            "allocation": lambda project: self.slot(False)})
        self.assertEqual(world.refused(report, QWEN27)["node"], route.N1)
        # CONTROL: council closes nothing, and the same seat is admitted
        report = world.ask("council", seams={
            "team": self.team(("seat-h", KIMI, "lead"),
                              ("seat-q", QWEN27, "reviewer")),
            "allocation": lambda project: self.slot(False)})
        self.assertIsNone(world.refused(report, QWEN27))
        self.assertEqual(world.admitted(report, QWEN27)["seat"], "seat-q")


class TempRegistry(unittest.TestCase):
    """A temp helm home whose registry holds the fixture's project, `helm`,
    and no team: the world the task/3156 readers' DEFAULTS read when no arm
    seams them. Nothing here reads this host's registry."""

    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory(prefix="helm-route-home-")
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        env = {"HELM_HOME": os.path.join(self.tmp, "helm-home")}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        for k in ("MELD_HOME", "HELM_CHAT_DIR"):
            if k in os.environ:
                saved = os.environ.pop(k)
                self.addCleanup(os.environ.__setitem__, k, saved)
        from helm import home, pk
        self.assertTrue(home.helm_home().startswith(self.tmp))
        self.path = os.path.join(self.tmp, "dev", "helm")
        os.makedirs(self.path)
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "helm": {"name": "helm", "path": self.path, "kind": "git",
                     "status": "active", "sessions": {}}}})
        pk.write_json(home.authored_path(), {"version": 1, "projects": {}})

    def author(self, *members, **shares):
        from helm import teams
        row, problem, _code = teams.write(
            "helm", {"members": [{"seat": s, "family": f, "role": r}
                                 for s, f, r in members], "shares": shares},
            0, by="owner", reason="the arm's team", apply=True,
            post=lambda *a, **kw: None, families_of=lambda seats: {})
        self.assertIsNone(problem)
        return row


class FailLoudTest(TempRegistry):
    """D6 (the task/3156 design read): a reader that RAISES reads FAILED,
    never unmeasured, and a share read that failed makes the answer PARTIAL.
    It was appended to the reasons and the answer still read whole, exit 0;
    and the default share reader swallowed a raising burn read into
    "unmeasured", the one answer a failed read cannot give."""

    TEAM = (("seat-a", NATIVE, "lead"), ("seat-b", CODEX, "builder"))

    def test_a_share_read_that_raises_makes_the_answer_partial(self):
        def boom(project):
            raise KeyError("pace")
        world = World().freshly_read().at_colour(**{
            f: bf.YELLOW for f in bf.families()})
        team = lambda project: {"project": project, "authored": True,
                                "v": 3, "by": "owner", "shares": {},
                                "members": [{"seat": s, "family": f,
                                             "role": r}
                                            for s, f, r in self.TEAM]}
        report = world.ask("build", seams={"team": team, "allocation": boom})
        self.assertTrue(report["partial"])
        self.assertIn("the project's shares FAILED (KeyError",
                      " ".join(report["partial_why"]))
        self.assertEqual(route.exit_code(report), 3)
        # CONTROL: the same ask with a share read that answers is whole
        report = world.ask("build", seams={"team": team,
                                           "allocation": lambda p: {}})
        self.assertFalse(report["partial"])

    def test_the_default_share_reader_raises_what_a_reader_raised(self):
        from helm import codexpace
        self.author(*self.TEAM, codex=30)
        world = World().freshly_read().at_colour(**{
            f: bf.YELLOW for f in bf.families()})
        seams = {"team": None, "allocation": None}
        with mock.patch.object(codexpace, "cached_seat_burn",
                               side_effect=ValueError("burn torn")):
            report = world.ask("build", seams=seams)
        self.assertEqual(report["bench_source"], "team v1")
        self.assertTrue(report["partial"])
        self.assertIn("the project's shares FAILED (ValueError",
                      " ".join(report["partial_why"]))
        # CONTROL: no burn snapshot is not a failure: nothing measured, and
        # the answer is whole
        report = world.ask("build", seams=seams)
        self.assertFalse(report["partial"], report["partial_why"])

    def test_lanes_or_capacity_that_do_not_read_make_the_answer_partial(self):
        """Round 3, ruling c: the share reader's lanes (the dispatch ledger)
        and the lane capacity file were each swallowed into "nothing in
        use" / "capacity not measured", on an answer that read whole."""
        from helm import dispatches, teams
        self.author(("seat-h", KIMI, "lead"), ("seat-q", QWEN27, "reviewer"),
                    qwen27=30)
        world = World().freshly_read().at_colour(**{
            f: bf.YELLOW for f in bf.families()})
        seams = {"team": None, "allocation": None, "families_of": None}
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({}, "ledger locked")):
            report = world.ask("council", seams=seams)
        self.assertTrue(report["partial"], report["partial_why"])
        self.assertIn("the project's shares FAILED (LedgerUnread",
                      " ".join(report["partial_why"]))
        os.makedirs(os.path.dirname(teams.capacity_path()), exist_ok=True)
        with open(teams.capacity_path(), "w", encoding="utf-8") as fh:
            fh.write("{torn")
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({}, None)), \
                mock.patch.object(dispatches, "owed", return_value=[]):
            report = world.ask("council", seams=seams)
        self.assertTrue(report["partial"], report["partial_why"])
        self.assertIn("the project's shares FAILED (CapacityUnread",
                      " ".join(report["partial_why"]))
        # CONTROL: both read, and the answer is whole
        os.remove(teams.capacity_path())
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({}, None)), \
                mock.patch.object(dispatches, "owed", return_value=[]):
            report = world.ask("council", seams=seams)
        self.assertFalse(report["partial"], report["partial_why"])

    def test_a_roster_that_does_not_read_makes_the_answer_partial(self):
        """The family door's roster read FAILED (`roster_checked` says so,
        it does not raise). Its default readers turned that into an empty
        roster, so every member was benched and billed on its TYPED family
        in silence: the D3 defect, re-entered through an unreadable input,
        on an answer that read whole. A strict reader is told instead."""
        from helm import seats_roster
        self.author(*self.TEAM, codex=30)
        world = World().freshly_read().at_colour(**{
            f: bf.YELLOW for f in bf.families()})
        seams = {"team": None, "allocation": None, "families_of": None}
        with mock.patch.object(seats_roster, "roster_checked",
                               return_value=({}, True)):
            report = world.ask("build", seams=seams)
        self.assertEqual(report["bench_source"], "team v1")
        self.assertTrue(report["partial"], report["partial_why"])
        why = " ".join(report["partial_why"])
        self.assertIn("the team's seat families FAILED (RosterUnread", why)
        self.assertIn("the project's shares FAILED (RosterUnread", why)
        self.assertEqual(route.exit_code(report), 3)
        # CONTROL: a roster that reads (none on disk: proven empty) is whole
        report = world.ask("build", seams=seams)
        self.assertFalse(report["partial"], report["partial_why"])


# THE COMMIT THAT LAST CHANGED ROUTE'S UNOPTED ANSWER ON PURPOSE. It was the
# trunk task/3156 forked from (byte-identical to trunk's route.py when this
# arm was written); landing refactor item 5 then ranked every door read by
# queue for every project, team or none, and re-pinned it here. A deliberate
# later change to route's unopted answer re-pins this to the commit that
# made it.
TRUNK_REF = "45c55148e3c90091602f9e2334cfb759c0bac2f1"


def _route_at(ref):
    """`helm/route.py` as it stood at `ref`, loaded as a module of THIS tree:
    the same catalog, the same fixtures and the same temp registry, so the
    only thing that differs from `route` is route.py itself."""
    import subprocess
    import types
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        got = subprocess.run(["git", "-C", repo, "show",
                              "%s:helm/route.py" % ref],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise unittest.SkipTest("git could not read route.py at %s (%s)"
                                % (ref[:11], exc.__class__.__name__))
    if got.returncode:
        raise unittest.SkipTest("route.py at %s is not in this checkout's "
                                "history (a shallow clone or an export): %s"
                                % (ref[:11], got.stderr.strip()[:200]))
    mod = types.ModuleType("helm._route_at_%s" % ref[:11])
    mod.__package__ = "helm"
    mod.__file__ = "<helm/route.py at %s>" % ref[:11]
    exec(compile(got.stdout, mod.__file__, "exec"), mod.__dict__)
    return mod


class ZeroConfigTest(TempRegistry):
    """ZERO CONFIG IS UNCHANGED, MEASURED AGAINST TRUNK (the task/3156
    design read's missing arm, then round 3's ruling). `route.answer` runs
    through its DEFAULT light, team, share and family readers on a registry
    that holds the project with no team and no authored light, and TRUNK'S
    `route.answer` (`TRUNK_REF`, loaded from history) runs on the same
    registry and the same frozen world. An unopted project must answer byte
    for byte as trunk does: the same `--json` document, the same render, no
    new key and no new line. Comparing against this lane's own readers
    seamed off missed exactly that: both sides grew the same keys."""

    DEFAULTS = {"light": None, "team": None, "allocation": None,
                "families_of": None}

    def test_an_unopted_project_answers_byte_for_byte_as_trunk(self):  # noqa: VACUOUS_ASSERTION — each kind and asker asserts a non-empty trunk answer first, then two unconditional equalities; the authored-team control asserts the defaults are live
        from helm import teams
        trunk = _route_at(TRUNK_REF)
        self.assertTrue(hasattr(trunk, "_seat_bar"), "premise: the ref is "
                        "the commit that ranked door reads by queue")
        for kind in route.KINDS:
            for frm in (None, "fable"):
                world = World().freshly_read()
                theirs, err = trunk.answer(
                    kind, frm=frm, project="helm", now=world.now,
                    seams=world.seams(**self.DEFAULTS))
                self.assertIsNone(err)
                with mock.patch.object(
                        teams, "live_families",
                        side_effect=AssertionError("roster read")), \
                        mock.patch.object(
                            teams, "project_row",
                            side_effect=AssertionError("shares read")):
                    ours = world.ask(kind, frm=frm, seams=self.DEFAULTS)
                with self.subTest(kind=kind, frm=frm):
                    self.assertTrue(theirs["answer"] or theirs["refused"])
                    self.assertEqual(
                        json.dumps(ours, indent=1, sort_keys=True,
                                   default=str),
                        json.dumps(theirs, indent=1, sort_keys=True,
                                   default=str))
                    for explain in (False, True):
                        self.assertEqual(
                            route.render(ours, now=world.now,
                                         explain=explain),
                            trunk.render(theirs, now=world.now,
                                         explain=explain))
        # CONTROL: the same default readers DO read an authored team, and
        # the answer then says which bench it read
        self.author(("seat-a", NATIVE, "lead"), ("seat-c", DS4PRO, "reviewer"))
        report = World().freshly_read().ask("review", seams=self.DEFAULTS)
        self.assertEqual(report["bench_source"], "team v1")

    def test_an_authored_light_with_no_team_still_refuses_new_work(self):
        """THE PROJECT LIGHT SUPERSEDES THE CREDENTIAL FLAG, team or no team
        (round 3, confirmed as intended). The light is the owner's
        permission, authored with `helm projects state`, and trunk's doors
        already obey it: `dispatch send` (`_project_light_rung`) and `work
        claim` (`registry.admits`) refuse new work in a red project. N0b makes
        route say what those doors do. An authored light is an opt-in, so
        this answer carries the light."""
        from helm import registry
        registry.state("helm", "red", reason="frozen for the release",
                       by="owner", apply=True)
        world = World().freshly_read()
        report = world.ask("build", seams=self.DEFAULTS)
        self.assertTrue(report["light"]["refuses"])
        self.assertEqual(report["answer"], [])
        self.assertEqual(route.exit_code(report), 1)
        self.assertNotIn("team", report)
        # a review finishes work in flight: admitted, and the light is said
        report = world.ask("review", seams=self.DEFAULTS)
        self.assertTrue(report["answer"])
        self.assertIn("light RED", "\n".join(route.render(report,
                                                          now=world.now)))


class NoLaneWaitsWhileAQualifiedReaderIsIdleTest(unittest.TestCase):
    """Landing refactor item 5: a door read goes to an idle approval-tier
    reader before a piled one. seat-a is the native seat, seat-b codex and
    seat-c ds4pro; every colour is declared so no ration confounds a rank."""

    OPUS, FABLE = "claude-opus-5-5", "claude-fable-5-1"

    def world(self, **rows):
        world = World().freshly_read().at_colour(
            **{f: bf.YELLOW for f in bf.families()})
        world.bench = json.loads(json.dumps(world.bench))
        for seat, fields in rows.items():
            world.bench["seats"][seat].update(fields)
        return world

    def test_H1_an_idle_ds4pro_is_named_over_a_codex_holding_seven(self):
        world = self.world(**{"seat-b": {"holding": 7}})
        order = world.families(world.ask("review"))
        self.assertEqual(order[0], DS4PRO)
        self.assertLess(order.index(DS4PRO), order.index(CODEX))
        # CONTROL: unpiled, codex's higher rating puts it first again
        world = self.world(**{"seat-b": {"holding": 0}})
        self.assertEqual(world.families(world.ask("review"))[0], CODEX)

    def test_H3_an_idle_opus_seat_beats_a_pile_and_ds4pro_beats_it(self):
        world = self.world(**{"seat-a": {"model": self.OPUS},
                              "seat-b": {"holding": 7}})
        report = world.ask("review")
        order = world.families(report)
        self.assertLess(order.index(DS4PRO), order.index(NATIVE))
        self.assertLess(order.index(NATIVE), order.index(CODEX))
        self.assertEqual(world.admitted(report, NATIVE)["seat"], "seat-a")
        self.assertIn("E34", [r["edge"] for r in
                              world.admitted(report, NATIVE)["reasons"]])

    def test_M6_fable_and_sonnet_are_never_named_opus_is(self):
        for model in (self.FABLE, "claude-sonnet-5"):
            world = self.world(**{"seat-a": {"model": model}})
            report = world.ask("review")
            self.assertNotIn(NATIVE, world.families(report), model)
            dropped = world.refused(report, NATIVE)
            self.assertEqual(dropped["node"], route.N1)
            self.assertIn("E34", [r["edge"] for r in dropped["reasons"]])
        world = self.world(**{"seat-a": {"model": self.OPUS}})   # control
        self.assertIn(NATIVE, world.families(world.ask("review")))

    def test_M2_a_codex_spark_seat_is_input_and_never_the_reader(self):
        world = self.world(**{"seat-b": {"model": "gpt-5.3-codex-spark"}})
        report = world.ask("review")
        self.assertNotIn(CODEX, world.families(report))
        self.assertEqual(world.refused(report, CODEX)["node"], route.N1)
        self.assertEqual([r["seat"] for r in report["input"]], ["seat-b"])
        for family in (GEMINI, QWEN27):
            self.assertIn(world.refused(report, family)["node"],
                          (route.N1, route.N2), family)
        world = self.world(**{"seat-b": {"model": "gpt-6-astra"}})  # control
        self.assertIn(CODEX, world.families(world.ask("review")))

    def test_M4_with_every_reader_piled_an_answer_is_still_given(self):
        world = self.world(**{s: {"holding": n} for s, n in (
            ("seat-a", 4), ("seat-b", 7), ("seat-c", 3), ("seat-f", 2))})
        report = world.ask("review")
        self.assertTrue(report["answer"])
        self.assertEqual(route.exit_code(report), 0)
        self.assertEqual(sorted(r["queue_bucket"] for r in report["answer"]),
                         [3] * len(report["answer"]))

    def test_M8_an_unreadable_holding_sorts_between_measured_and_piled(self):
        world = self.world(**{"seat-c": {"holding": None},
                              "seat-f": {"holding": 3}})
        order = world.families(world.ask("review"))
        self.assertLess(order.index(CODEX), order.index(DS4PRO))
        self.assertLess(order.index(DS4PRO), order.index(KIMI))

    def test_E9_cites_the_live_ruling_and_names_the_retired_one(self):
        """E9's old source is retired in the store; the edge carries the entry
        that replaced it and names the retired id, as E17 does."""
        edge = route.EDGE["E9"]
        self.assertEqual(edge["sources"], (
            "opus55-is-the-default-fable-is-the-crossmodel-reviewer-of-last-"
            "resort",))
        self.assertEqual(edge["retired"],
                         ("fable-reviews-are-high-stakes-only-not-the-default",))

    def test_the_row_bars_its_author_before_the_pick(self):
        """--row applies the author, input and tier exclusions first."""
        def seam(rid, **kw):
            return {"row": {"id": rid}, "eligible": [], "unreadable": {},
                    "seats": [{"seat": "seat-c", "state": "EXCLUDED",
                               "conjunct": "chain"}]}, None
        world = self.world()
        report = world.ask("review", row="r1", seams={"eligibility": seam})
        self.assertNotIn(DS4PRO, world.families(report))
        self.assertIn("E1", [r["edge"] for r in
                             world.refused(report, DS4PRO)["reasons"]])
        self.assertIn(DS4PRO, world.families(world.ask("review")))  # control


class VerbTest(unittest.TestCase):
    """The CLI surface: the kinds, the refusals, and the router collision."""

    def _run(self, *argv):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = route.cmd_route(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_f19_a_relay_subverb_refuses_by_name_and_points_at_the_relay(self):
        """The disambiguator, and it is the point of the exit-2 branch: a
        confident-looking answer to a question nobody asked is the worst
        outcome of a name collision."""
        for sub in route.ROUTER_SUBVERBS:
            rc, out, err = self._run(sub)
            self.assertEqual(rc, 2, sub)
            self.assertIn("helm router %s" % sub, err)
            self.assertEqual(out, "")

    def test_an_unknown_kind_refuses_by_name(self):
        rc, _out, err = self._run("reveiw")
        self.assertEqual(rc, 2)
        self.assertIn("reveiw", err)
        self.assertIn("|".join(route.KINDS), err)

    def test_a_missing_flag_value_refuses_rather_than_answering(self):
        rc, _out, err = self._run("review", "--from")
        self.assertEqual(rc, 2)
        self.assertIn("--from needs a value", err)

    def test_an_unknown_argument_refuses_by_name(self):
        rc, _out, err = self._run("review", "--gaet", "x")
        self.assertEqual(rc, 2)
        self.assertIn("--gaet", err)

    def test_no_argument_prints_the_usage(self):
        rc, _out, err = self._run()
        self.assertEqual(rc, 2)
        self.assertIn("helm route <kind>", err)

    def test_every_kind_is_named_in_the_usage_line(self):
        for kind in route.KINDS:
            self.assertIn(kind, route._USAGE)


class RenderTest(unittest.TestCase):
    """What a reader actually sees."""

    def test_the_header_carries_the_reading_age_and_the_partial_flag(self):
        # The arm sets PARTIAL itself one line down and asserts the header
        # BEFORE and AFTER, so the base has to be non-partial for its own
        # reason; the capture's eighteen-hour readings made both halves read
        # PARTIAL and the assertion pair meaningless.
        world = World().freshly_read()
        report = world.ask("verify", frm="fable", project="helm")
        head = route.render(report, now=world.now)[0]
        self.assertIn("flags measured", head)
        self.assertNotIn("PARTIAL", head)
        report["partial"] = True
        self.assertIn("PARTIAL", route.render(report, now=world.now)[0])

    def test_every_refusal_names_one_node_and_one_reason(self):
        world = World()
        report = world.ask("verify", frm="fable", project="helm")
        for line in route.render(report, now=world.now):
            if line.startswith("  NOT "):
                self.assertTrue(any(node in line for node in route.NODES),
                                line)

    def test_explain_prints_the_fleet_wide_laws_and_the_default_does_not(self):
        world = World()
        report = world.ask("verify", frm="fable", project="helm")
        lean = "\n".join(route.render(report, now=world.now))
        full = "\n".join(route.render(report, now=world.now, explain=True))
        self.assertNotIn("[E16 ", lean)
        self.assertIn("[E16 ", full)
        self.assertGreater(len(full), len(lean))

    def test_the_hops_acceptance_test_is_always_printed(self):
        world = World()
        report = world.ask("council")
        self.assertTrue(any(line.startswith("  hops ")
                            for line in route.render(report, now=world.now)))

    def test_an_answer_with_nobody_says_so_instead_of_printing_nothing(self):
        world = World()
        world.bench = {"seats": {}, "fanout": {}}
        report = world.ask("council", project="helm")
        text = "\n".join(route.render(report, now=world.now))
        self.assertIn("no family on this bench", text)


if __name__ == "__main__":
    unittest.main()


class AnUnprovenWindowIsUnknownContextTest(unittest.TestCase):
    """task/3534: the context reader answers "window unproven" for a seat
    whose window is only ASSUMED. The route row carries no percentage for it,
    names why, and ranks it in the unknown bucket."""

    def test_an_unproven_window_is_context_unknown_never_a_percentage(self):  # noqa: VACUOUS_ASSERTION — assertTrue(report["answer"]) runs unconditionally before the loop, so the loop has rows to check
        world = World().freshly_read()
        for row in world.bench["seats"].values():
            row["context_pct"] = "window unproven"
        report = world.ask("verify", frm="fable", project="helm")
        self.assertTrue(report["answer"], "the world routed nobody")
        for row in report["answer"]:
            self.assertIsNone(row["context_pct"])
            self.assertEqual(row["context_unknown"], "window unproven")
            self.assertEqual(row["context_bucket"], 1)
