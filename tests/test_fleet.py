#!/usr/bin/env python3
"""helm.fleet — the composition-truth verb. Hermetic where /proc is the input
(probes mocked), but the PARSERS under test are always the real ones:
session's record reader / _resume_sid and fleet's daemon matcher/walk run on
real inputs, never mocked. SID truth is DELEGATED: fleet consumes
session._proc_claude_rows() verbatim and re-derives none of it — pinned here
both behaviorally and against the module source."""
import ast
import contextlib
import inspect
import io
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import fleet, pk, seats, session, who  # noqa: E402

SID_A = "12345678-1234-1234-1234-123456789abc"
SID_B = "87654321-4321-4321-4321-cba987654321"
SID_C = "11112222-3333-4444-5555-666677778888"


def srow(pid, sid=None, identity="unknown", root=None, cwd="/w",
         possible=(), reason="record-missing", start="g1", environ=()):
    """One session census row as the census actually shapes it."""
    return {"pid": pid, "resume": sid if identity == "resume" else None,
            "declared": sid if identity == "declared" else None,
            "declared_reason": "record-ok" if identity == "declared"
                               else reason,
            "cwd": cwd, "root": root, "identity": identity, "session": sid,
            "possible_sessions": list(possible), "child": False,
            "ancestor_sid8": "", "force": False,
            "start": start,
            "environ": None if environ is None else dict(environ)}


class FleetRowsBase(unittest.TestCase):
    """The fleet-rows fixture: `_wire` patches every probe `fleet.rows`
    reads (the process census and its environ reads, daemons, roster,
    terminals, generation, pane and throttle), `_rows` and `_rows_full`
    build the table under those patches, and `_render` and `_render_rc` run
    `helm fleet` under them.

    THIS CLASS HOLDS NO `test_*` METHOD, and that is its contract. unittest
    collects every inherited `test_*` again under each subclass's own id, so
    an arm written here runs once per subclass. A class that needs this
    fixture subclasses THIS class; its arms go in the subclass."""

    def _wire(self, envs, census=(), daemons=(), daemons_failed=False,
              daemon_for=None, roster=({}, False), terminals=([], False),
              census_failed=False, who_failed=False, census_partial=False,
              generation=None, pane_for=None, unproven=(), throttle=None):
        merged = []
        for r in census:
            r = dict(r)
            # envs is keyed by pid; None = the bracketed environ read failed
            r["environ"] = envs.get(r["pid"], r.get("environ"))
            merged.append(r)
        census = {r["pid"]: r for r in merged}
        daemons = dict(daemons)
        daemon_for = daemon_for or (
            lambda pid, start, ds, unproven: (
                ("daemon", sorted(ds)[0]) if ds else ("headless", None)))
        generation = generation or (lambda pid, start: True)
        pane_for = pane_for or (lambda env, terms: (None, True))
        return [
            mock.patch.object(fleet, "_census",
                              lambda: (census, census_failed, who_failed,
                                       census_partial)),
            mock.patch.object(fleet, "_daemon_pids",
                              lambda: (daemons, set(unproven), daemons_failed)),
            mock.patch.object(fleet, "_daemon_for", daemon_for),
            mock.patch.object(fleet, "_roster", lambda: roster),
            mock.patch.object(
                fleet, "_orca_terminals", lambda daemon, start, env: terminals),
            mock.patch.object(fleet, "_pane_for", pane_for),
            mock.patch.object(fleet, "_generation_intact", generation),
            # THE THROTTLE READ REACHES THE REAL /sys/fs/cgroup, and the
            # fixture pids in this file (41, 10) EXIST on a live box — so
            # without this an arm would read whatever those kernel processes
            # are doing and its result would depend on host state. Stubbed
            # beside the census for the same reason the census is stubbed.
            mock.patch.object(fleet, "_throttle",
                              throttle or (lambda pid, start: ({}, 0, "", True))),
        ]

    def _rows_full(self, *a, **kw):
        ps = self._wire(*a, **kw)
        for p in ps:
            p.start()
        try:
            return fleet.rows()
        finally:
            for p in ps:
                p.stop()

    def _rows(self, *a, **kw):
        table, daemons, _flags = self._rows_full(*a, **kw)
        return table, daemons

    def _render_rc(self, *a, args=(), **kw):
        buf = io.StringIO()
        ps = self._wire(*a, **kw)
        for p in ps:
            p.start()
        try:
            with contextlib.redirect_stdout(buf):
                rc = fleet.cmd_fleet(list(args))
        finally:
            for p in ps:
                p.stop()
        return buf.getvalue(), rc

    def _render(self, *a, **kw):
        return self._render_rc(*a, **kw)[0]


class FleetRowsTest(FleetRowsBase):
    """The fleet-rows arms, on FleetRowsBase's wired probes.

    A subclass of THIS class runs every arm below again under its own id,
    which is right only for one that changes the fixture those arms read. A
    class that only needs the fixture subclasses FleetRowsBase."""

    def test_stamps_and_deck_are_read_from_the_live_env(self):
        envs = {10: {"HELM_CHAT_NAME": "a-seat",
                     "CLAUDE_CODE_CHILD_SESSION": "1",
                     "CLAUDE_CODE_SESSION_ID": "x",
                     "HELM_SKILL_DECK": "/x/special-deck/skills"}}
        census = [srow(10, SID_A, "declared", root="/h/.claude")]
        # the deck tag is config-driven: the authored host block supplies the
        # {substring: tag} map. Mock the accessor so no site deck name is baked
        # into the test and the label-mechanism itself is what's under test.
        with mock.patch("helm.registry.authored_host",
                        return_value={"deck_labels": {"special-deck": "SD"}}):
            rows, daemons = self._rows(envs, census, {99: "111"})
        r = rows[0]
        self.assertEqual((r["seat"], r["stamps"], r["deck"], r["daemon"]),
                         ("a-seat", 2, "SD", 99))
        self.assertEqual((r["sid"], r["sid_src"]), (SID_A, "record"))
        self.assertFalse(r["unknown"])
        self.assertEqual(daemons, [99])

    def test_every_column_is_probed_never_cached(self):
        # the verb exists BECAUSE cached mental models rot: rows() must call
        # the live probes (census included) on every invocation
        calls = {"census": 0, "daemons": 0}
        rows_ = [srow(1), srow(2)]

        def census():
            calls["census"] += 1
            return {r["pid"]: r for r in rows_}, False, False, False

        def daemon_pids():
            calls["daemons"] += 1
            return {}, set(), False
        ps = self._wire({1: {}, 2: {}}, rows_)
        ps[0] = mock.patch.object(fleet, "_census", census)
        ps[1] = mock.patch.object(fleet, "_daemon_pids", daemon_pids)
        for p in ps:
            p.start()
        try:
            fleet.rows()
            fleet.rows()
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(calls, {"census": 2, "daemons": 2})

    def test_unreadable_environ_is_unknown_not_absence(self):
        # env=None is a FAILED probe: home/deck show ?, stamps unproven, the
        # row is marked unknown, and the footer counts it
        census = [srow(5, SID_A, "declared")]
        rows, _ = self._rows({5: None}, census)
        r = rows[0]
        self.assertTrue(r["unknown"])
        self.assertEqual((r["home"], r["deck"], r["stamps"]),
                         ("?", "?", None))
        out = self._render({5: None}, census)
        self.assertIn("1 row(s) carry UNKNOWN columns", out)
        self.assertIn("failed probes, not absence", out)

    def test_roster_exception_surfaces_as_roster_error(self):
        rows, _ = self._rows({6: {}}, [srow(6, root="/r")], roster=({}, True))
        self.assertEqual((rows[0]["seat"], rows[0]["seat_src"]),
                         (None, "roster-error"))
        # round-2 finding 4: a failed roster probe is a row-level UNKNOWN —
        # the JSON bit must agree with the footer, and the render must never
        # claim the affirmative '(no seat)' fact
        self.assertTrue(rows[0]["unknown"])
        out = self._render({6: {}}, [srow(6, root="/r")], roster=({}, True))
        self.assertNotIn("(no seat)", out)
        self.assertIn("UNKNOWN columns", out)
        # a readable env with a seat name still wins over a broken roster
        rows, _ = self._rows({6: {"HELM_CHAT_NAME": "s"}}, [srow(6)],
                             roster=({}, True))
        self.assertEqual(rows[0]["seat_src"], "env")

    def test_a_seat_RENAMED_WHILE_LIVE_prints_its_CURRENT_key(self):  # noqa: VACUOUS_ASSERTION — the FIRST statement pair in the body is an unconditional positive on the same observables (seat, seat_src) == ('seat-b', 'env-alias'); the assertFalse(seat_unrenderable) beside it is what a resolved key MEANS, and the '?' pole of that flag is pinned by test_a_HOSTILE_roster_key_cannot_be_reached_through_an_alias
        """A LAUNCH-TIME RECORD IS NOT A CURRENT IDENTITY (task/2739).

        `/proc/<pid>/environ` is written at exec and the kernel never rewrites
        it, so a renamed seat declares its old spelling until it exits. This
        column printed that spelling, and the widest column of the census the
        owner reads to decide what to kill then named a seat that no longer
        exists. The roster already records the rename and it is already in
        hand here.

        Every arm in this pair is its own control: the SAME env declaration,
        the SAME pid, and only the roster's rename record differs."""
        census = [srow(6, SID_A, "declared", root="/r")]
        env = {6: {"HELM_CHAT_NAME": "seat-a"}}
        renamed = {"seat-b": {"session": SID_A,
                              seats.RENAME_ALIAS_FIELD: {
                                  "old": "seat-a", "at": "x", "prior": [],
                                  "until": pk.epoch_ts(time.time() + 3600)}}}
        rows, _ = self._rows(env, census, roster=(renamed, False))
        self.assertEqual((rows[0]["seat"], rows[0]["seat_src"]),
                         ("seat-b", "env-alias"))
        # the assertEqual directly above is the unconditional positive on this
        # same row; a resolved key is by definition renderable, and the '?'
        # side of this flag is pinned by the hostile-key arm below.
        self.assertFalse(rows[0]["seat_unrenderable"])  # noqa: VACUOUS_ASSERTION
        # CONTROL 1: no rename record on the roster and the declaration stands
        # exactly as it always did.
        rows, _ = self._rows(env, census,
                             roster=({"seat-b": {"session": SID_A}}, False))
        self.assertEqual((rows[0]["seat"], rows[0]["seat_src"]),
                         ("seat-a", "env"))
        # CONTROL 2: an EXPIRED window is not an identity — the record outlives
        # the window it grants, so reading the record instead of `live_alias`
        # would keep answering with a key the roster stopped honouring.
        stale = {"seat-b": {"session": SID_A,
                            seats.RENAME_ALIAS_FIELD: {
                                "old": "seat-a", "at": "x", "prior": [],
                                "until": pk.epoch_ts(time.time() - 60)}}}
        rows, _ = self._rows(env, census, roster=(stale, False))
        self.assertEqual((rows[0]["seat"], rows[0]["seat_src"]),
                         ("seat-a", "env"))

    def test_a_REUSED_name_renamed_AGAIN_does_not_relabel_the_OLD_process(self):  # noqa: VACUOUS_ASSERTION — the assertNotEqual is a second reading of an observable this same method has already pinned POSITIVELY twice on the same roster and the same pid: ('seat-c','env-alias') for the new generation and ('seat-b','env-alias') for the old one. Both are unconditional, both precede it, and either going empty reddens the arm before the absence check is reached
        """THE ALIAS ANSWERS ABOUT A NAME; THE SESSION SAYS WHICH PROCESS.

        Found by a cross-family reviewer on this lane's first tip. Rename
        seat-a to seat-b, admit a FRESH seat-a, rename that one to seat-c.
        `live_alias("seat-a")` now answers seat-c and it is RIGHT about the
        name — seat-a really is seat-c's prior spelling. But the ORIGINAL
        seat-a process is still running, still declaring `seat-a` at exec
        because environ is frozen, and it belongs to SEAT-B. Resolving its pid
        to seat-c labels an OLD GENERATION with a NEWER seat's name, in the
        widest column of the census the owner reads to decide what to kill.

        Both arms share the SAME env declaration, the SAME pid and the SAME
        two-alias roster. ONLY THE SESSION DIFFERS, which is the whole claim."""
        env = {6: {"HELM_CHAT_NAME": "seat-a"}}
        alias = {"old": "seat-a", "at": "x", "prior": [],
                 "until": pk.epoch_ts(time.time() + 3600)}
        reused = {"seat-b": {"session": SID_A, seats.RENAME_ALIAS_FIELD: alias},
                  "seat-c": {"session": SID_C, seats.RENAME_ALIAS_FIELD: alias}}
        # THE NEW GENERATION resolves to seat-c: its session is the one seat-c
        # claims. Unconditional positive pole, so the arm below is a different
        # answer and not a rung that stopped resolving.
        rows, _ = self._rows(env, [srow(6, SID_C, "declared", root="/r")],
                             roster=(reused, False))
        self.assertEqual((rows[0]["seat"], rows[0]["seat_src"]),
                         ("seat-c", "env-alias"))
        # THE DEFECT, AND THE CURE IS SHARPER THAN LEAVING IT UNRESOLVED: the
        # old process's session is the one SEAT-B claims, and seat-b is what
        # that process actually is now. It must print seat-b and MUST NOT print
        # seat-c, which is what a name-first resolution gave it.
        rows, _ = self._rows(env, [srow(6, SID_A, "declared", root="/r")],
                             roster=(reused, False))
        self.assertEqual((rows[0]["seat"], rows[0]["seat_src"]),
                         ("seat-b", "env-alias"))
        self.assertNotEqual(rows[0]["seat"], "seat-c")
        # AND A PID WITH NO SESSION cannot corroborate anything, so no alias
        # may move it — the declaration stands and `seat_src` says so.
        rows, _ = self._rows(env, [srow(6, None, "unknown", root="/r")],
                             roster=(reused, False))
        self.assertEqual((rows[0]["seat"], rows[0]["seat_src"]),
                         ("seat-a", "env"))

    def test_a_declared_name_that_is_ITS_OWN_ROSTER_KEY_never_moves(self):  # noqa: VACUOUS_ASSERTION — the first _rows call in the body is an unconditional positive control on the SAME roster and the SAME observable: it resolves to ('seat-b', 'env-alias'), so the unmoved ('seat-a', 'env') below is the exact-key law firing and not a rung that stopped resolving
        """`live_alias`'s own law — an exact roster key is never an alias — is
        what keeps this rung from aliasing a LIVE seat onto another row. The
        alias arm above is the control that proves the rung resolves at all."""
        census = [srow(6, SID_A, "declared", root="/r")]
        env = {6: {"HELM_CHAT_NAME": "seat-a"}}
        alias = {"seat-b": {"session": SID_A,
                            seats.RENAME_ALIAS_FIELD: {
                                "old": "seat-a", "at": "x", "prior": [],
                                "until": pk.epoch_ts(time.time() + 3600)}}}
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE: this roster
        # DOES resolve, so the unmoved answer below is the exact-key law and
        # not a rung that stopped resolving.
        rows, _ = self._rows(env, census, roster=(alias, False))
        self.assertEqual((rows[0]["seat"], rows[0]["seat_src"]),
                         ("seat-b", "env-alias"))
        both = dict(alias)
        both["seat-a"] = {"session": "sid-other"}   # re-admitted, live again
        rows, _ = self._rows(env, census, roster=(both, False))
        self.assertEqual((rows[0]["seat"], rows[0]["seat_src"]),
                         ("seat-a", "env"))

    def test_an_UNREADABLE_roster_cannot_resolve_an_alias_and_says_so(self):
        """A rung must say which of its inputs failed. With the roster
        unreadable there is no rename record helm may trust, so the declaration
        stands — and `seat_src` stays `env`, never `env-alias`, so a reader can
        tell a resolved key from an unresolved declaration.

        THE ROWS HERE ARE NON-EMPTY ON PURPOSE, and the first fixture I wrote
        was not. `roster_checked` answers ({}, True) on a failed read, so an
        empty-rows fixture cannot tell "the rung honoured the failure bit" from
        "there was nothing to resolve" — a mutant that ignored the bit entirely
        survived it. Rows that DO carry the alias make the failure bit the only
        thing that can decide the answer. That shape is about this rung's
        CONTRACT rather than a reachable production call, which is exactly why
        it has to be asserted rather than assumed from the caller."""
        census = [srow(6, SID_A, "declared", root="/r")]
        carries = {"seat-b": {"session": SID_A,
                              seats.RENAME_ALIAS_FIELD: {
                                  "old": "seat-a", "at": "x", "prior": [],
                                  "until": pk.epoch_ts(time.time() + 3600)}}}
        seat, src = fleet._seat_for(SID_A, {"HELM_CHAT_NAME": "seat-a"},
                                    carries, True)
        self.assertEqual((seat, src), ("seat-a", "env"))
        # CONTROL ON THE SAME FIXTURE: clear the failure bit and the very same
        # rows DO resolve, so the arm above is the bit being honoured.
        self.assertEqual(fleet._seat_for(SID_A, {"HELM_CHAT_NAME": "seat-a"},
                                         carries, False),
                         ("seat-b", "env-alias"))
        # and the production shape, end to end
        rows, _ = self._rows({6: {"HELM_CHAT_NAME": "seat-a"}}, census,
                             roster=({}, True))
        self.assertEqual((rows[0]["seat"], rows[0]["seat_src"]),
                         ("seat-a", "env"))

    def test_a_HOSTILE_roster_key_cannot_be_reached_through_an_alias(self):
        """THE ALIAS IS A SECOND DOOR INTO THE SAME COLUMN, so it owes the same
        rung the roster arm already holds: `write_roster` carries no token
        check, so a directly-written row could name anything, and resolving an
        alias would print that key into the census's widest column. The
        canonical-key control is the same call with the same alias record."""
        hostile = {"beta\u202e": {"session": SID_A,
                                  seats.RENAME_ALIAS_FIELD: {
                                      "old": "seat-a", "at": "x", "prior": [],
                                      "until": pk.epoch_ts(time.time() + 3600)}}}
        # the canonical declaration is kept rather than the hostile key
        self.assertEqual(fleet._seat_for(SID_A, {"HELM_CHAT_NAME": "seat-a"},
                                         hostile, False),
                         ("seat-a", "env"))
        ok = {"beta-two": dict(hostile["beta\u202e"])}
        self.assertEqual(fleet._seat_for(SID_A, {"HELM_CHAT_NAME": "seat-a"},
                                         ok, False),
                         ("beta-two", "env-alias"))

    def test_probed_empty_roster_is_a_fact_not_unknown(self):
        # the affirmative counterpart: roster probe SUCCEEDED and holds no
        # seat -> '(no seat)' renders and the row is not unknown
        census = [srow(6, SID_A, "declared", root="/r")]
        rows, _ = self._rows({6: {}}, census, roster=({}, False))
        self.assertFalse(rows[0]["unknown"])
        self.assertFalse(rows[0]["seat_unrenderable"])
        self.assertIn("(no seat)", self._render({6: {}}, census))

    def test_empty_scrubbed_seat_name_renders_unrenderable_and_sets_json_bit(self):
        # Present seat identity whose unprintable/hostile name scrubs to empty:
        # seat_unrenderable is True, unknown is True, seat is '?', and render
        # prints UNRENDERABLE rather than claiming '(no seat)' absence.
        census = [srow(6, SID_A, "declared", root="/r")]
        env = {6: {"HELM_CHAT_NAME": "\x00\x01"}}
        rows, _ = self._rows(env, census, roster=({}, False))
        self.assertEqual(rows[0]["seat"], "?")
        self.assertTrue(rows[0]["seat_unrenderable"])
        self.assertTrue(rows[0]["unknown"])
        out = self._render(env, census)
        self.assertIn("UNRENDERABLE", out)
        self.assertNotIn("(no seat)", out)

    def test_a_partially_scrubbed_name_cannot_ALIAS_a_real_seat(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the real-seat census asserted BEFORE the loop — seat=='alpha', unrenderable False — on the same observable
        """THE COLLISION A REVIEW FOUND (gate:7ed2df89b215598c), and the arm
        above is why it hid: a name that scrubs to NOTHING was handled, and a
        name that scrubs to SOMETHING silently became that something.

        Reproduced on the reviewed tip before the cure:
            _seat_label('alpha')       -> 'alpha'
            _seat_label('alpha\\u202e') -> 'alpha'   seat_unrenderable=False
        so an attacker-shaped HELM_CHAT_NAME rendered as a legitimate seat in
        the first and widest column of the census the owner reads to decide
        what to kill. Laundering made it SAFE TO PRINT, never TRUE."""
        census = [srow(6, SID_A, "declared", root="/r")]
        # CONTROL FIRST: the real seat is untouched, so the assertions below
        # are the alias being refused and not the census failing to resolve.
        real, _ = self._rows({6: {"HELM_CHAT_NAME": "alpha"}}, census,
                             roster=({}, False))
        self.assertEqual(real[0]["seat"], "alpha")
        self.assertFalse(real[0]["seat_unrenderable"])

        for hostile, why in (("alpha‮", "RLO"),
                             ("alpha​", "zero-width space"),
                             ("alpha ", "trailing space")):
            with self.subTest(why=why):
                env = {6: {"HELM_CHAT_NAME": hostile}}
                rows, _ = self._rows(env, census, roster=({}, False))
                self.assertNotEqual(rows[0]["seat"], "alpha",
                                    "%s aliased the real seat" % why)
                self.assertEqual(rows[0]["seat"], "?")
                self.assertTrue(rows[0]["seat_unrenderable"])
                self.assertIn("UNRENDERABLE", self._render(env, census))

    def test_a_hostile_ROSTER_KEY_cannot_alias_a_real_seat_either(self):
        """DEFENCE IN DEPTH, and the distinction is measured rather than
        assumed — an earlier draft of this docstring called it the twin of the
        env arm and that was wrong. `home.chat_name()` RAISES SeatNameError on
        a noncanonical name, so nothing hostile reaches the roster THROUGH
        JOIN; `write_roster` itself carries no token check, so this rung
        guards a direct roster write. Kept because the cost is one call and
        the roster is a file — not because a reachable writer is known."""
        census = [srow(6, SID_A, "declared", root="/r")]
        # CONTROL: a canonical roster key still resolves normally.
        ok, _ = self._rows({6: {}}, census,
                           roster=({"beta-two": {"session": SID_A}}, False))
        self.assertEqual((ok[0]["seat"], ok[0]["seat_src"]),
                         ("beta-two", "roster"))
        rows, _ = self._rows({6: {}}, census,
                             roster=({"beta-two‮": {"session": SID_A}},
                                     False))
        self.assertNotEqual(rows[0]["seat"], "beta-two")
        self.assertEqual(rows[0]["seat"], "?")
        self.assertTrue(rows[0]["seat_unrenderable"])

    def test_the_rung_accepts_what_helm_ITSELF_GENERATES(self):
        """THE ORACLE THE ARM BELOW LACKS, and the reason it lacks it.

        The arm below pins seven name shapes I HAND-WROTE. They are literals,
        so it is not blind by the derived-expectation rule — but they are
        shapes I IMAGINED. `seats.auto_name` is what helm actually ISSUES, and
        it was available the whole time. If the generator or `pk.slug` ever
        emitted something `_SEAT_TOKEN` rejects, my list could not notice: it
        only knows what I thought of.

        A derived oracle cannot see a bug because it agrees with itself; an
        IMAGINED oracle cannot see one because it only knows the author's
        imagination. One question catches both — if the system started
        emitting something new tomorrow, would this test see it?

        So this drives the REAL producer across the inputs that actually shape
        its output — project basename, spaces, dots, case, non-ASCII, the
        no-cwd fallback — plus the two shapes it reaches for when the base is
        taken (the -N dedup) or unavailable (the agent-<sid8> hex floor). All
        MEASURED canonical today; this is what makes that a standing property
        instead of a fact about tonight."""
        sid = "12345678-1234-1234-1234-123456789abc"
        emitted = []
        for cwd in ("/w/helm", "/w/My Project", "/w/weird.name",
                    "/w/UPPER_Case", "/w/uber-projekt", None):
            with self.subTest(cwd=cwd):
                name = seats.auto_name(sid, cwd)
                emitted.append(name)
                self.assertTrue(
                    seats._SEAT_TOKEN.fullmatch(name),
                    "helm GENERATES %r and its own identity rung refuses it — "
                    "the census would render a real seat UNRENDERABLE" % name)
        # the two shapes auto_name reaches for beyond the base case, spelled
        # the way its own source spells them.
        for name in ("%s-2" % emitted[0], "agent-%s" % sid.replace("-", "")[:8]):
            with self.subTest(name=name):
                self.assertTrue(seats._SEAT_TOKEN.fullmatch(name), name)
        # CONTROL on the same predicate: it is not simply saying yes. A name
        # helm could never issue is still refused, so the accepts above are
        # the rung agreeing with the generator and not a vacuous rung.
        self.assertFalse(seats._SEAT_TOKEN.fullmatch("alpha\u202e"))
        self.assertFalse(seats._SEAT_TOKEN.fullmatch("has space"))

    def test_every_live_seat_name_survives_the_canonical_rung(self):  # noqa: VACUOUS_ASSERTION — this test IS the positive direction: every subTest asserts seat==name and unrenderable is False, so there is no absence to control for
        """THE OTHER DIRECTION, which is the one that breaks an owner console
        rather than a threat model: a rung that refuses hostile names is only
        safe if it accepts every REAL one. Measured over the live population
        before landing — 22 roster seats and 7 live HELM_CHAT_NAME values, all
        canonical — and pinned here against the shapes helm actually issues."""
        for name in ("alpha", "beta-two", "gamma-delta-epsilon",
                     "delta7", "epsilon-long-hyphenated-name",
                     "seat.with.dots", "seat_with_underscores"):
            with self.subTest(name=name):
                self.assertTrue(seats._SEAT_TOKEN.fullmatch(name), name)
                census = [srow(6, SID_A, "declared", root="/r")]
                rows, _ = self._rows({6: {"HELM_CHAT_NAME": name}}, census,
                                     roster=({}, False))
                self.assertEqual(rows[0]["seat"], name)
                self.assertFalse(rows[0]["seat_unrenderable"])

    def test_an_EXPLICIT_EMPTY_name_is_present_not_absent(self):
        """The exact-tip finding, and the door the rung did not cover.

        `if name:` read an EXPLICIT empty identity — `HELM_CHAT_NAME=`, which
        `session._full_environ` faithfully preserves as "" — as ABSENT, so it
        fell through to the ROSTER branch and the process was handed whatever
        seat owned that session. Measured before the cure: `_seat_for('sid',
        {'HELM_CHAT_NAME': ''}, {'real': ...})` returned ('real', 'roster')
        with unrenderable=False.

        An empty declaration says "I am nobody", which is not the same as
        saying nothing. Only the second may fall through — and the control
        below is that arm, because a rung that also swallowed the ABSENT case
        would break every roster-resolved row on the fleet."""
        census = [srow(6, SID_A, "declared", root="/r")]
        roster = ({"real": {"session": SID_A}}, False)
        # CONTROL FIRST: a genuinely ABSENT key still resolves via the roster.
        ok, _ = self._rows({6: {}}, census, roster=roster)
        self.assertEqual((ok[0]["seat"], ok[0]["seat_src"]), ("real", "roster"))
        self.assertFalse(ok[0]["seat_unrenderable"])
        # ...and an EXPLICIT empty one is refused at the env arm instead.
        rows, _ = self._rows({6: {"HELM_CHAT_NAME": ""}}, census, roster=roster)
        self.assertNotEqual(rows[0]["seat"], "real",
                            "an empty declaration aliased the roster identity")
        self.assertEqual((rows[0]["seat"], rows[0]["seat_src"]), ("?", "env"))
        self.assertTrue(rows[0]["seat_unrenderable"])

    def test_a_row_can_be_BOTH_refused_and_probe_unknown(self):
        """The second finding: the footer partitioned on
        `seat_unrenderable`, which assumed the two causes were exclusive. They
        co-occur, and the probe evidence was the half that vanished — a
        hostile name plus a failed cwd probe printed only the refusal."""
        census = [srow(6, SID_A, "declared", root="/r", cwd=None)]
        env = {6: {"HELM_CHAT_NAME": "alpha‮"}}
        rows, _ = self._rows(env, census, roster=({}, False))
        self.assertTrue(rows[0]["seat_unrenderable"])
        self.assertTrue(rows[0]["probe_unknown"],
                        "the failed cwd probe was swallowed by the refusal")
        out = self._render(env, census)
        self.assertIn("failed probes, not absence", out)
        self.assertIn("show UNRENDERABLE", out)

    # The row keys that carry UNKNOWN and its two causes. A site that puts any
    # of these into a mapping is writing the vocabulary, whatever syntax it uses.
    _UNKNOWN_VOCAB = ("unknown", "probe_unknown", "seat_unrenderable")

    # ALLOWLIST OF LOCATIONS, derived by running the scan below over the real
    # module — not from memory. Everything else that writes the vocabulary is
    # an offender, including syntaxes nobody has thought of yet.
    _MAY_WRITE_THE_VOCAB = {
        "rows": "the row BUILDER — constructs each row with its causes",
        "_mark_unknown": "the ONLY post-builder writer, and it always "
                         "sets a cause",
    }

    @staticmethod
    def _vocab_key_writes(tree, vocab):
        """Every site putting a VOCAB word into a mapping, keyed by function.

        Detects the four ways to name a mapping key without indirection: a
        keyword argument, a dict-literal key, a subscript in STORE context,
        and a constant argument to a mutator method. READS are deliberately
        untouched — fleet._daemon_for returns the bare string "unknown" as a
        daemon-attribution verdict, a HOMONYM of the row key, and a scan that
        flagged it would be teaching people to ignore this guard."""
        mutators = {"update", "setdefault", "__setitem__"}
        found = {}

        def note(scope, kind, word, lineno):
            found.setdefault(scope, []).append("%s %r:%d" % (kind, word, lineno))

        def check(n, scope):
            if isinstance(n, ast.keyword) and n.arg in vocab:
                note(scope, "kwarg", n.arg, getattr(n.value, "lineno", 0))
            elif isinstance(n, ast.Dict):
                for k in n.keys:
                    if isinstance(k, ast.Constant) and k.value in vocab:
                        note(scope, "dict-key", k.value, k.lineno)
            elif (isinstance(n, ast.Subscript)
                    and isinstance(n.ctx, ast.Store)
                    and isinstance(n.slice, ast.Constant)
                    and n.slice.value in vocab):
                note(scope, "subscript-store", n.slice.value, n.lineno)
            elif (isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr in mutators):
                for a in n.args:
                    if isinstance(a, ast.Constant) and a.value in vocab:
                        note(scope, n.func.attr + "()", a.value, a.lineno)

        # EVERY PRODUCER-CAPABLE SCOPE, not every FunctionDef. Descending from
        # the module and renaming the scope at each producer boundary is what
        # makes module-level statements and lambdas reachable at all; the
        # previous cut iterated FunctionDef nodes, so anything outside a def
        # was never visited and returned clean.
        # Scope names are QUALIFIED, because the allowlist matches on this
        # string. A bare name would allowlist `rows` ANYWHERE — a method
        # `C.rows` writing the vocabulary would pass as though it were the
        # module-level builder. I found that hole by testing a belief I had
        # just told a reviewer was untested; it is the same too-coarse
        # identity that produced the previous two defeats.
        def header_nodes(node):
            """Everything in a def/lambda/class that is NOT its body, DERIVED
            from the node rather than listed from memory.

            Decorators, argument defaults, annotations, return annotations,
            base classes and class keywords all evaluate WHERE THE DEF IS
            WRITTEN, so attributing them to the definition's own scope lets a
            write hide in the header of an allowlisted owner and inherit its
            permission.

            THIS IS DERIVED, AND THE PREVIOUS CUT WAS NOT. That one
            hand-enumerated the fields — decorator_list, defaults,
            kw_defaults, the three arg lists, returns — and a review defeated
            it three ways in one verdict: `*args: <write>` and `**kwargs:
            <write>` (vararg/kwarg carry their OWN annotations and were in no
            list I wrote), and `class C(metaclass=<write>)` (ClassDef
            keywords, which I never enumerated at all). A hand-written field
            list is my imagination standing in for the grammar, which is the
            same failure as a hand-written test oracle.

            Asking the NODE what its children are makes the answer complete
            by construction, including for grammar Python has not shipped
            yet: anything that is not a body statement is header."""
            body = getattr(node, "body", None)
            body = body if isinstance(body, list) else ([body] if body else [])
            skip = {id(stmt) for stmt in body}
            return [c for c in ast.iter_child_nodes(node) if id(c) not in skip]

        # ONE visitor that dispatches on THE NODE ITSELF. An earlier cut had
        # a helper that checked a node then descended into it, with the
        # boundary test applied only to CHILDREN — so a def handed to that
        # helper directly was walked in its PARENT's scope, and `def evil`
        # nested inside allowlisted `rows` was attributed to `rows` and
        # allowed. Dispatching on the node removes the class of bug rather
        # than the instance.
        def visit(node, scope, prefix):
            if isinstance(node, ast.ClassDef):
                for h in header_nodes(node):           # bases, decorators AND
                    visit(h, scope, prefix)            # keywords (metaclass=)
                for stmt in node.body:
                    visit(stmt, scope, prefix + node.name + ".")
                return
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = prefix + node.name
                for h in header_nodes(node):
                    visit(h, scope, prefix)            # header: parent scope
                for stmt in node.body:
                    visit(stmt, qual, qual + ".")
                return
            if isinstance(node, ast.Lambda):
                for h in header_nodes(node):
                    visit(h, scope, prefix)            # header: parent scope
                visit(node.body, "<lambda@%d>" % node.lineno, prefix)
                return
            check(node, scope)
            for child in ast.iter_child_nodes(node):
                visit(child, scope, prefix)

        for child in ast.iter_child_nodes(tree):
            visit(child, "<module>", "")
        return found

    def test_NO_site_writes_the_UNKNOWN_vocabulary_outside_its_two_owners(self):
        """THE STRUCTURAL GUARD — an ALLOWLIST of locations, after the
        blocklist of syntaxes was defeated on review.

        The first cut of this guard matched one syntax: an ast.Assign whose
        target is a Subscript named "unknown". A review defeated it in one
        line by appending an unused `_future_unknown_bypass(row)` calling
        `row.update(unknown=True)`. All 233 fleet tests stayed green. That
        future site mints the exact NEITHER-BUCKET state — unknown with no
        cause, rc=1 with no owner-facing sentence — without touching
        `_mark_unknown` and without a single subscript assignment.

        A guard that enumerates FORBIDDEN SYNTAXES is a blocklist: it must be
        widened for update, then setdefault, then |=, then whatever Python
        adds, and it is silently wrong in between. So this inverts to an
        ALLOWLIST OF LOCATIONS. Only two functions may write the vocabulary;
        every other site is an offender no matter how it spells the write.
        That is the same inversion landed in dispatches this session, applied
        to the guard that was itself a blocklist.

        THEN A REVIEW DEFEATED IT A SECOND TIME, and the second one is the
        better lesson: `future = lambda row: row.update(unknown=True)` at
        module scope returned {}. Not key indirection — an INVISIBLE FUTURE
        PRODUCER. The scan iterated FunctionDef nodes, so a lambda (not a
        FunctionDef) and anything at module scope (inside no def at all) were
        never visited, and the scan reported clean by never looking. My
        own stated limit had covered indirection and said nothing about
        SCOPE COVERAGE, so I had under-scoped my own honesty note.

        It now descends from the module and renames the scope at each
        producer boundary, so module statements, lambdas, and nested defs are
        all reachable and each is named in its own right.

        HONEST LIMIT, because a guard that overclaims is worse than a narrow
        one: this reads syntax, so KEY INDIRECTION defeats it —
        `k = "unknown"; row[k] = True` is invisible here, as is any write
        through a name computed at runtime. The runtime invariant in
        test_NO_call_shape_... is the backstop for what syntax cannot see,
        and neither arm alone is the whole claim. (A write in a nested def
        USED to be attributed to its enclosing function and hidden by an
        allowlisted parent; the scope-descent fixed that too, and the nested
        case is a planted control below.)"""
        scan = self._vocab_key_writes
        vocab = self._UNKNOWN_VOCAB

        # MUST-HIT CONTROLS, and the first is a review's exact bypass kept as
        # a live regression. A scan that cannot flag a planted offender would
        # report the real module clean by failing to look.
        planted = [
            ("kwarg update (codex-2's exact bypass)",
             "def _future_unknown_bypass(row):\n    row.update(unknown=True)\n"),
            ("dict-literal update",
             'def f(r):\n    r.update({"unknown": True})\n'),
            ("setdefault",
             'def f(r):\n    r.setdefault("unknown", True)\n'),
            ("dict |= merge",
             'def f(r):\n    r |= {"unknown": True}\n'),
            ("plain subscript store",
             'def f(r):\n    r["unknown"] = True\n'),
            # The THIRD bypass: a producer in a scope the scan never
            # visited. Not key indirection — an invisible future producer.
            ("lambda at MODULE scope (codex-2's third)",
             "future = lambda row: row.update(unknown=True)\n"),
            ("bare module-level statement",
             'row = {}\nrow["unknown"] = True\n'),
            ("lambda INSIDE a function",
             'def f():\n    return lambda r: r.update(unknown=True)\n'),
            ("nested def inside another def",
             'def outer(r):\n'
             '    def inner(x):\n        x["unknown"] = True\n'
             '    return inner\n'),
            # IMPERSONATION. I found these by testing a belief I had just
            # told a reviewer was untested. The allowlist matches on the
            # scope string, so an unqualified name would allowlist `rows`
            # ANYWHERE — a method or nested def merely NAMED like an owner
            # would inherit the owner's permission.
            ("method impersonating the builder",
             'class C:\n    def rows(self, r):\n        r["unknown"] = True\n'),
            ("method impersonating the helper",
             'class C:\n'
             '    def _mark_unknown(self, r):\n        r["unknown"] = True\n'),
            ("nested def impersonating the builder",
             'def outer():\n'
             '    def rows(r):\n        r["unknown"] = True\n'),
            ("class BODY at module scope",
             'class C:\n    row = {}\n    row["unknown"] = True\n'),
            # HEADER vs BODY. The second half: decorators and argument
            # defaults evaluate where the def is WRITTEN, so attributing them
            # to the function's own scope lets a write hide in the header of
            # an allowlisted owner and inherit its permission.
            ("decorator on the allowlisted builder",
             '@deco(unknown=True)\ndef rows(r):\n    pass\n'),
            ("argument default on the allowlisted builder",
             'def rows(r, _x=D.update(unknown=True)):\n    pass\n'),
            ("kwonly default on the allowlisted builder",
             'def rows(r, *, _k=D.update(unknown=True)):\n    pass\n'),
            # NESTED INSIDE an allowlisted owner — found by testing the
            # visitor against its own structure, not by review.
            ("def nested inside the allowlisted builder",
             'def rows(r):\n'
             '    def evil(x):\n        x["unknown"] = True\n'
             '    return evil\n'),
            ("class nested inside the allowlisted builder",
             'def rows(r):\n    class C:\n'
             '        def go(self, x):\n            x["unknown"] = True\n'),
            ("lambda nested inside the allowlisted builder",
             'def rows(r):\n    return lambda x: x.update(unknown=True)\n'),
            # HEADER FIELDS I HAND-ENUMERATED AND MISSED. A review defeated
            # the listed-by-hand version three ways in one verdict; the
            # derived version catches these plus the two below it that I
            # never thought to plant.
            ("*args annotation on the allowlisted builder",
             'def rows(*args: D.update(unknown=True)):\n    pass\n'),
            ("**kwargs annotation on the allowlisted builder",
             'def rows(**kw: D.update(unknown=True)):\n    pass\n'),
            ("class metaclass= keyword",
             'class C(metaclass=D.update(unknown=True)):\n    pass\n'),
            ("positional-only annotation",
             'def rows(a: D.update(unknown=True), /):\n    pass\n'),
            ("return annotation",
             'def rows() -> D.update(unknown=True):\n    pass\n'),
        ]
        # THE UNCONDITIONAL POSITIVE CONTROL, hoisted out of the loop below.
        # Everything after this runs inside a for, so an empty or truncated
        # `planted` would skip every control and leave the final assertion
        # proving nothing — helm's vacuous-assertion rung caught that shape
        # here twice. This one call cannot be skipped, and it is a review's
        # exact bypass, so the single most important control is the one that
        # does not depend on loop iteration.
        self.assertTrue(
            scan(ast.parse("def _future_unknown_bypass(row):\n"
                           "    row.update(unknown=True)\n"), vocab),
            "the scan cannot see the bypass this guard was rewritten for, so "
            "its verdict on the real module below means nothing")
        # The SECOND defeat, also unconditional: a module-scope lambda. The
        # scan returned {} for this while passing every control above, so a
        # control set that only covers def-scope proves nothing about reach.
        self.assertTrue(
            scan(ast.parse("f = lambda row: row.update(unknown=True)\n"), vocab),
            "the scan cannot see a MODULE-SCOPE LAMBDA producer, which is "
            "how it read clean while a live bypass sat in the file")
        # ONE function computes offenders, and it is called on a KNOWN-BAD
        # module and on the real one. Both assertions below therefore
        # constrain the SAME observable — which is what makes the empty
        # result meaningful, and is also the only form helm's
        # vacuous-assertion rung accepts as a positive control (it matches
        # on the root NAME, so a separately-named control variable does not
        # cover the assertion it was written for).
        def offenders_in(src):
            writes = scan(ast.parse(src) if isinstance(src, str) else src,
                          vocab)
            return {fn: sites for fn, sites in writes.items()
                    if fn not in self._MAY_WRITE_THE_VOCAB}

        self.assertTrue(
            offenders_in('def evil(r):\n    r["unknown"] = True\n'),
            "the allowlist filter produced NO offender for a module that "
            "plainly contains one, so the empty result below would prove "
            "nothing about the real module")
        self.assertEqual(len(planted), 24,
                         "the planted-offender set was truncated to %d, so "
                         "the loop controls cannot vouch for the scan"
                         % len(planted))
        for label, src in planted:
            self.assertTrue(
                scan(ast.parse(src), vocab),
                "the scan did NOT flag a planted offender (%s), so its "
                "verdict on the real module means nothing" % label)

        # BOTH CONTROLS ON ONE OBSERVABLE, adjacent on purpose. A negative
        # control alone cannot tell "reads are correctly ignored" from "the
        # scan is dead"; the positive beside it settles which, and the two
        # catch opposite failures.
        write_src = 'def f(r):\n    r["unknown"] = True\n'
        read_src = 'def f(r):\n    return r.get("unknown")\n'
        self.assertTrue(
            scan(ast.parse(write_src), vocab),
            "the scan is dead: it did not flag a plain write, so the READ "
            "control below would pass for the wrong reason")
        self.assertFalse(
            scan(ast.parse(read_src), vocab),
            "reading the field was flagged as a write — the guard would cry "
            "wolf and get deleted by whoever needs to read it")

        # CONTROL: the allowlist names must EXIST, or a rename silently
        # empties the guard while leaving it green.
        module_fns = {n.name for n in ast.walk(ast.parse(inspect.getsource(fleet)))
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

        def missing_from(names):
            return sorted(set(names) - module_fns)

        # Same one-helper-two-inputs shape as offenders_in: a name fleet
        # provably does NOT have must come back missing, or an empty result
        # for the real allowlist would only mean module_fns came back empty.
        self.assertTrue(
            missing_from(["_a_name_fleet_definitely_does_not_define"]),
            "a name fleet does not define was NOT reported missing, so the "
            "function inventory is empty and the check below is vacuous")
        self.assertEqual(missing_from(self._MAY_WRITE_THE_VOCAB), [],
                         "allowlisted %r no longer exist in fleet, so the "
                         "allowlist is stale and permits nothing it names"
                         % (missing_from(self._MAY_WRITE_THE_VOCAB),))

        real_src = inspect.getsource(fleet)
        self.assertEqual(offenders_in(real_src), {},
                         "these write UNKNOWN or one of its causes outside "
                         "the two owners, so a row can be minted that lands "
                         "in NEITHER footer bucket: %s" % (offenders_in(real_src),))

    def test_NO_call_shape_of_the_helper_yields_an_UNCATEGORIZED_unknown(self):
        """THE INVARIANT, ASKED OF THE SIGNATURE INSTEAD OF OF MY MEMORY.

        `unknown` with no cause bit is rc=1 with NO owner-facing sentence —
        the NEITHER-bucket class this helper exists to close. I closed it at
        the four call sites and then reopened it myself: a `probe=False`
        escape, added so "a closed class has a door", emitted precisely that
        row, and the test that stood here asserted the uncategorized row was
        CORRECT. It had no call site and could not have gained a right one.
        A review caught it.

        So this arm does not enumerate the call shapes I remember. It reads
        them off `inspect.signature` and drives every one, and it reads the
        CAUSE NAMES off the footer's own partition rather than restating
        them. Both oracles are things helm already contains, and neither is
        the code under test — the footer CONSUMES these bits, `_mark_unknown`
        produces them, so the check cannot pass by agreeing with itself.

        A bypass that suppresses the only cause the helper can state fails
        here the day it lands. A genuine second cause — its own bit, its own
        footer sentence, a producer that emits it — passes without an edit."""
        src = inspect.getsource(fleet)
        causes = sorted(set(re.findall(
            r'for r in unknowns if r\.get\("(\w+)"\)', src)))
        # MUST-HIT: an empty or shrunken derivation would make every
        # assertion below vacuously true, so prove the oracle read the footer
        # before trusting a single verdict it produces.
        self.assertIn("probe_unknown", causes,
                      "derived the footer's cause bits as %r — the partition "
                      "at fleet._render moved or was renamed, so this test "
                      "was about to pass without checking anything" % (causes,))

        sig = inspect.signature(fleet._mark_unknown).parameters
        # POSITIVE CONTROL for the emptiness assertion below. "No undrivable
        # parameters" and "I could not read the signature at all" produce the
        # SAME empty list, so prove the read worked by finding the one
        # parameter that must always be present. helm's own vacuous-assertion
        # rung caught this arm missing exactly this, which is the class the
        # arm is itself about.
        self.assertIn("row", sig,
                      "signature read returned %r — every assertion below "
                      "would pass by having looked at nothing" % (list(sig),))
        params = [p for p in sig.values() if p.name != "row"]
        undrivable = [p.name for p in params if not isinstance(p.default, bool)]
        self.assertEqual(undrivable, [],
                         "cannot drive %r, so this test would report 'no "
                         "uncategorized row reachable' having never looked. "
                         "Extend the driver to cover it." % (undrivable,))

        names = [p.name for p in params]
        for combo in itertools.product((True, False), repeat=len(names)):
            kwargs = dict(zip(names, combo))
            row = {}
            fleet._mark_unknown(row, **kwargs)
            self.assertTrue(row.get("unknown"),
                            "_mark_unknown(%r) did not mark the row" % kwargs)
            self.assertTrue(
                [c for c in causes if row.get(c)],
                "_mark_unknown(%r) produced unknown=True with NONE of the "
                "footer's cause bits %r — rc=1, and the row appears under "
                "neither owner-facing sentence: %r" % (kwargs, causes, row))

    def test_late_probe_failures_still_carry_their_cause(self):
        """Three findings: terminal-inventory failure, unproven pane, and
        duplicate pane all mutate `unknown` AFTER the row is built. Each used
        to leave probe_unknown false, so the row vanished from both footers
        while the JSON still claimed a two-cause model."""
        census = [srow(6, SID_A, "declared", root="/r")]
        # (a) terminal inventory failed
        rows, _ = self._rows({6: {}}, census, daemons={9: "d1"},
                             terminals=([], True))
        self.assertTrue(rows[0]["unknown"])
        self.assertTrue(rows[0]["probe_unknown"], "terminal-inventory failure")
        # (b) pane not proven
        rows, _ = self._rows({6: {}}, census, daemons={9: "d1"},
                             terminals=([{"handle": "t1"}], False),
                             pane_for=lambda env, terms: (None, False))
        self.assertTrue(rows[0]["probe_unknown"], "unproven pane")
        # (c) two rows claiming one pane
        two = [srow(6, SID_A, "declared", root="/r"),
               srow(7, SID_B, "declared", root="/r")]
        rows, _ = self._rows({6: {}, 7: {}}, two, daemons={9: "d1"},
                             terminals=([{"handle": "t1"}], False),
                             pane_for=lambda env, terms: ("t1", True))
        for r in rows:
            self.assertTrue(r["probe_unknown"], "duplicate pane")

    def test_the_footer_separates_a_REFUSED_name_from_a_FAILED_probe(self):
        """SEEN ON THE OWNER'S SURFACE, not inferred from the table dict.

        Both cases set `unknown`, and before this the footer called every one
        of them a FAILED PROBE — which sent a reader hunting a broken
        instrument while a noncanonical name sat in the row it was printed
        for. The next move differs: a failed probe means doubt the
        instrument, a refused identity means doubt the PROCESS. The tool
        already knew which; it just said the wrong one."""
        census = [srow(6, SID_A, "declared", root="/r"),
                  srow(7, SID_B, "declared", root="/r")]
        env = {6: {"HELM_CHAT_NAME": "alpha‮"},   # refused identity
               7: None}                                 # genuinely failed probe
        out = self._render(env, census)
        self.assertIn("UNRENDERABLE — a name was present", out)
        self.assertIn("Identify the process by pid, never by that name", out)
        self.assertIn("failed probes, not absence", out)
        # ...and each counts ONE row, so neither swallowed the other.
        self.assertIn("1 row(s) show UNRENDERABLE", out)
        self.assertIn("1 row(s) carry UNKNOWN columns", out)

    def test_roster_fallback_uses_session_not_a_nonexistent_pid_field(self):
        roster = {"codex-2": {"session": SID_A, "sessions": [SID_A]}}
        census = [srow(6, SID_A, "declared", root="/r")]
        rows, _ = self._rows({6: {}}, census, roster=(roster, False))
        self.assertEqual((rows[0]["seat"], rows[0]["seat_src"]),
                         ("codex-2", "roster"))

    def test_duplicate_roster_session_is_ambiguous_and_unknown(self):
        roster = {"a": {"session": SID_A}, "b": {"sessions": [SID_A]}}
        census = [srow(6, SID_A, "declared", root="/r")]
        rows, _ = self._rows({6: {}}, census, roster=(roster, False))
        self.assertEqual(rows[0]["seat_src"], "roster-ambiguous")
        self.assertTrue(rows[0]["unknown"])


class RosterCheckedTest(unittest.TestCase):
    def test_missing_is_empty_but_malformed_or_wrong_shape_is_failed(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "roster.json")
            with mock.patch.object(seats, "roster_path", return_value=path):
                self.assertEqual(seats.roster_checked(), ({}, False))
                for value in ("{bad", "[]", '{"seat": "bad"}',
                              '{"seat": {"session": 7}}',
                              '{"seat": {"sessions": "not-a-list"}}',
                              '{"seat": {"sessions": ["ok", 7]}}'):
                    with open(path, "w") as f:
                        f.write(value)
                    self.assertEqual(seats.roster_checked(), ({}, True), value)
                good = {"seat": {"session": SID_A}}
                with open(path, "w") as f:
                    json.dump(good, f)
                self.assertEqual(seats.roster_checked(), (good, False))


class SidDelegationTest(FleetRowsBase):
    """Finding 1: SID truth is session._proc_claude_rows(),
    consumed whole — record, argv, who, cwd-candidate rungs AND the final
    generation recheck — never a fleet-side splice of private helpers."""

    def test_census_calls_the_whole_census_verbatim(self):
        rows = [srow(7, SID_A, "declared", root="/r")]
        with mock.patch.object(
                session, "_proc_claude_census",
                return_value={"rows": rows, "listing_failed": False,
                              "who_failed": False,
                              "census_partial": False}) as prc:
            self.assertEqual(fleet._census(),
                             ({7: rows[0]}, False, False, False))
        prc.assert_called_once_with()

    def test_fleet_source_rederives_no_sid_or_config_parsing(self):
        # the design law, pinned at the source level: fleet may CALL the
        # census; the private sid/config helpers it once spliced are gone,
        # and (round-2 finding 1) so are the second comm scan and the
        # unbracketed /proc cwd re-read
        src = inspect.getsource(fleet)
        for banned in ("_proc_snapshot", "_session_record", "_resume_sid",
                       "_sid_for", "_sids_for", "argv~ancestor",
                       "_config_root", "CLAUDE_CONFIG_DIR",
                       "_claude_pids", "glob", "readlink",
                       # round-3 finding 1: env facts come from the census
                       # bracket — fleet never re-opens a proc environ file
                       "proc/%d/environ", "_environ(",
                       # round-3 finding 2: the completeness-blind rows-only
                       # shape is not fleet's entry point
                       "_proc_claude_rows"):
            self.assertNotIn(banned, src, banned)

    def test_every_census_identity_maps_to_its_source_label(self):
        census = [srow(1, SID_A, "declared", root="/r"),
                  srow(2, SID_A, "resume"),
                  srow(3, SID_B, "who")]
        rows, _ = self._rows({1: {}, 2: {}, 3: {}}, census)
        self.assertEqual([(r["sid"], r["sid_src"]) for r in rows],
                         [(SID_A, "record"), (SID_A, "argv"),
                          (SID_B, "who")])

    def test_cwd_candidate_rung_surfaces_instead_of_being_dropped(self):
        census = [srow(4, possible=[SID_A, SID_B])]
        rows, _ = self._rows({4: {}}, census)
        self.assertEqual(rows[0]["candidates"], [SID_A, SID_B])
        self.assertIn("(2 cwd-candidate(s))", self._render({4: {}}, census))

    def test_second_record_holder_is_double_open_not_ancestor(self):
        # "another pid records this SID" proves DOUBLE-OPEN, not a fork
        census = [srow(1, SID_A, "declared", root="/r"),
                  srow(2, SID_A, "resume")]
        rows, _ = self._rows({1: {}, 2: {}}, census)
        self.assertTrue(all(r["double_open"] for r in rows))
        out = self._render({1: {}, 2: {}}, census)
        self.assertIn("DOUBLE-OPEN", out)
        self.assertIn("live in MULTIPLE pids", out)

    def test_rows_come_solely_from_the_census_no_second_scan(self):
        # round-2 finding 1: a pid the census rejected (its generation
        # recheck failed — no two reads cohere) must never be resurrected by
        # a fleet-side comm scan and composed into a row of fictions
        rows, _ = self._rows({5: {}}, census=())
        self.assertEqual(rows, [])

    def test_census_none_cwd_is_never_re_read_from_proc(self):
        # round-2 finding 1: census cwd=None means the BRACKETED probe
        # failed. Use our OWN pid, whose /proc/<pid>/cwd is readable — a
        # surviving unbracketed fallback would return a real path and clear
        # the unknown bit; the row must stay '?' and UNKNOWN
        pid = os.getpid()
        census = [srow(pid, SID_A, "declared", root="/r", cwd=None)]
        rows, _ = self._rows({pid: {}}, census)
        self.assertEqual(rows[0]["cwd"], "?")
        self.assertTrue(rows[0]["unknown"])

    def test_failed_record_probe_reason_marks_row_unknown(self):
        census = [srow(6, reason="record-replaced", root="/r")]
        rows, _ = self._rows({6: {}}, census)
        self.assertTrue(rows[0]["unknown"])
        # an affirmative blank (record-missing) is NOT a failed probe
        rows, _ = self._rows({6: {}}, [srow(6, reason="record-missing",
                                            root="/r")])
        self.assertFalse(rows[0]["unknown"])


class HomeColumnTest(FleetRowsBase):
    """Finding 4 / finding 3: home is session's canonical
    config root for the TARGET process — never the inspector's ~/.claude,
    never a fleet-side re-derivation of config policy."""

    def test_home_is_the_census_root_not_the_inspector_home(self):
        census = [srow(8, SID_A, "declared", root="/tmp/other-home/.claude")]
        rows, _ = self._rows({8: {"HOME": "/tmp/other-home"}}, census)
        self.assertEqual(rows[0]["home"], "/tmp/other-home/.claude")

    def test_untrusted_or_unproven_config_root_renders_unknown(self):
        # session returns root=None for config-untrusted (incl. the
        # CLAUDE_CONFIG_DIR-present-but-invalid case): never guess ~/.claude
        rows, _ = self._rows({8: {}}, [srow(8, reason="config-untrusted",
                                            root=None)])
        self.assertEqual(rows[0]["home"], "?")
        # round-2 finding 4: home='?' is unproven evidence — the row-level
        # bit must say so, not hand JSON consumers a false known-row bit
        self.assertTrue(rows[0]["unknown"])


class DaemonDetectionTest(unittest.TestCase):
    def test_substring_lookalikes_are_not_daemons(self):
        for argv in (["node", "/x/not-daemon-entry.js"],
                     ["bash", "-c", "tail -f daemon-entry.js.log"],
                     ["node", "/x/daemon-entry.js.bak"],
                     ["--script=daemon-entry.js"]):
            self.assertFalse(fleet._is_daemon_argv(argv), argv)

    def test_non_orca_runtimes_reading_the_script_are_not_daemons(self):
        # Finding 4: the SHAPE must be the orca daemon's, not any
        # argv element that basenames to daemon-entry.js
        for argv in (["cat", "/tmp/daemon-entry.js"],
                     ["python", "worker.py", "/tmp/daemon-entry.js"],
                     ["vi", "daemon-entry.js"],
                     ["node", "worker.js", "/x/daemon-entry.js"]):
            self.assertFalse(fleet._is_daemon_argv(argv), argv)

    def test_real_orca_daemon_shapes_match(self):
        self.assertTrue(fleet._is_daemon_argv(
            ["/tmp/.mount_orca-x/orca-ide",
             "/tmp/.mount_orca-x/resources/app.asar.unpacked/out/main/"
             "daemon-entry.js", "--socket", "/x.sock"]))
        self.assertTrue(fleet._is_daemon_argv(
            ["node", "/opt/orca/daemon-entry.js"]))
        self.assertTrue(fleet._is_daemon_argv(["daemon-entry.js"]))

    def test_ppid_walk_parses_real_proc_stat(self):
        self.assertEqual(fleet._stat_ppid(os.getpid()), os.getppid())
        self.assertIsNone(fleet._stat_ppid(2 ** 22 + 12345))  # no such pid


class DaemonWalkTest(unittest.TestCase):
    """codex finding 2: daemon identity is re-proven at match time (argv
    still daemon-shaped, starttime still the scanned incarnation) — bare set
    membership across PID reuse is never trusted. The REAL _daemon_for walk
    runs; only the /proc probes are mocked."""
    DAEMON_ARGV = ["orca-ide", "/x/daemon-entry.js", "--socket", "/s"]

    def _walk(self, tree, daemons, argv=None, start="111", unproven=()):
        links = {p: ("g%d" % p, parent) for p, parent in tree.items()}
        with mock.patch.object(fleet, "_stat_link", lambda p: links.get(p)), \
             mock.patch.object(fleet, "_cmdline_argv", lambda p: argv), \
             mock.patch.object(session, "_proc_start", lambda p: start):
            return fleet._daemon_for(7, "g7", daemons, set(unproven))

    def test_matching_incarnation_is_a_proven_daemon(self):
        self.assertEqual(self._walk({7: 99}, {99: "111"},
                                    argv=self.DAEMON_ARGV),
                         ("daemon", 99))

    def test_stale_starttime_membership_is_unknown_not_a_host(self):
        # deterministic probe from the review: stale set {99} + parent 99
        self.assertEqual(self._walk({7: 99}, {99: "111"},
                                    argv=self.DAEMON_ARGV, start="222"),
                         ("unknown", None))

    def test_reused_pid_running_a_lookalike_is_unknown(self):
        self.assertEqual(self._walk({7: 99}, {99: "111"},
                                    argv=["cat", "/tmp/daemon-entry.js"]),
                         ("unknown", None))

    def test_walk_to_init_is_the_only_proven_headless(self):
        self.assertEqual(self._walk({7: 5, 5: 1}, {99: "111"}),
                         ("headless", None))

    def test_unparsable_hop_is_unknown_never_headless(self):
        self.assertEqual(self._walk({7: 5}, {99: "111"}), ("unknown", None))

    def test_exhausted_depth_is_unknown_never_headless(self):
        tree = {p: p + 1 for p in range(7, 40)}
        self.assertEqual(self._walk(tree, {}), ("unknown", None))

    def test_walk_through_an_unproven_pid_is_unknown_never_headless(self):
        # round-2 finding 2, exact probe: daemon-shaped 99 whose starttime
        # read failed is UNPROVEN; child 7->99->1 must answer UNKNOWN, not
        # walk through the maybe-daemon to init and claim proven HEADLESS
        self.assertEqual(self._walk({7: 99, 99: 1}, {}, unproven={99}),
                         ("unknown", None))

    def test_unreadable_cmdline_ancestor_is_unknown(self):
        # a hop whose cmdline could not be read cannot be ruled out as the
        # daemon — headless is unprovable through it
        self.assertEqual(self._walk({7: 42, 42: 1}, {}, unproven={42}),
                         ("unknown", None))

    def test_intermediate_parent_reuse_invalidates_the_chain(self):
        calls = {7: 0}

        def link(pid):
            if pid == 7:
                calls[7] += 1
                return ("g7", 50) if calls[7] == 1 else ("g7", 1)
            return {50: ("g50", 99)}.get(pid)
        with mock.patch.object(fleet, "_stat_link", side_effect=link), \
             mock.patch.object(fleet, "_cmdline_argv",
                               return_value=self.DAEMON_ARGV), \
             mock.patch.object(session, "_proc_start", return_value="d1"):
            self.assertEqual(fleet._daemon_for(7, "g7", {99: "d1"}, set()),
                             ("unknown", None))


class CensusRecheckFailureTest(unittest.TestCase):
    def test_live_stat_read_failure_is_unknown_not_generation_mismatch(self):
        error = PermissionError(13, "stat unreadable")
        with mock.patch.object(session, "_proc_bytes", side_effect=error):
            self.assertIsNone(session._census_matches(41, "g1", b"claude\0"))

    def test_gone_pid_is_proven_absence(self):
        error = FileNotFoundError(2, "gone")
        with mock.patch.object(session, "_proc_bytes", side_effect=error):
            self.assertFalse(session._census_matches(41, "g1", b"claude\0"))

    def test_unparsable_live_stat_is_unknown_not_absence(self):
        with mock.patch.object(session, "_proc_bytes",
                               return_value=b"41 (claude) malformed"):
            self.assertIsNone(session._census_matches(41, "g1", b"claude\0"))

    def test_torn_stat_during_unknown_stub_recheck_keeps_unknown_row(self):
        stub = {"pid": 41, "uid": os.geteuid(), "start": "g1"}

        def scan(accounts=None, status=None):
            status.update({"listing_failed": False, "failed_pids": set()})
            return []

        with mock.patch.object(session.os, "listdir", return_value=["41"]), \
             mock.patch.object(session, "_census_snapshot",
                               return_value=("unknown", stub)), \
             mock.patch.object(session, "_proc_bytes",
                               return_value=b"41 (claude) malformed"), \
             mock.patch.object(who, "scan", side_effect=scan):
            census = session._proc_claude_census()
        self.assertEqual(len(census["rows"]), 1)
        self.assertEqual(census["rows"][0]["pid"], 41)
        self.assertTrue(census["rows"][0]["probe_failed"])
        self.assertFalse(census["census_partial"])


class WhoCensusContextTest(unittest.TestCase):
    def _census(self, who_rows, failed_pids=()):
        snap = {"pid": 41, "uid": os.geteuid(), "start": "g1",
                "cmdline": b"claude\0", "environ": b"HOME=/alt\0",
                "argv": ["claude"], "env": {"HOME": "/alt"},
                "cwd": "/w", "stdin": "/dev/pts/1"}

        def scan(accounts=None, status=None):
            status.update({"listing_failed": False,
                           "failed_pids": set(failed_pids)})
            return who_rows

        with mock.patch.object(session.os, "listdir", return_value=["41"]), \
             mock.patch.object(session, "_census_snapshot",
                               return_value=("ok", snap)), \
             mock.patch.object(session, "_session_record",
                               return_value=(None, "record-missing",
                                             "/alt/.claude")), \
             mock.patch.object(session, "_census_matches", return_value=True), \
             mock.patch.object(session, "_cwd_session_ids", return_value=[]), \
             mock.patch.object(who, "scan", side_effect=scan):
            return session._proc_claude_census()["rows"][0]

    def test_who_sid_from_another_home_is_conflict_not_identity(self):
        wr = {"pid": 41, "provider": "anthropic", "child": False,
              "home": "/inspector/.claude", "cwd": "/w",
              "session": SID_A, "session_candidates": []}
        row = self._census([wr])
        self.assertIsNone(row["session"])
        self.assertTrue(row["who_context_mismatch"])

    def test_partial_who_scan_marks_that_pid_failed(self):
        row = self._census([], failed_pids={41})
        self.assertIsNone(row["session"])
        self.assertTrue(row["who_probe_failed"])


class DaemonScanTest(unittest.TestCase):
    """codex round-2 finding 2: partial probe failures inside the daemon
    scan must surface as UNPROVEN pids — never be silently dropped behind
    scan_failed=False and later converted into a proven-HEADLESS absence.
    The REAL _daemon_pids runs; only the /proc probes are mocked."""

    def _scan(self, argvs, starts, listing=None, gone=()):
        names = [str(p) for p in argvs] if listing is None else listing

        def cmdline(pid):
            if pid in gone:
                return "gone", None
            argv = argvs.get(pid)
            return ("failed", None) if argv is None else ("ok", argv)

        with mock.patch.object(fleet.os, "listdir", lambda p: names), \
             mock.patch.object(fleet, "_cmdline_probe", side_effect=cmdline), \
             mock.patch.object(session, "_proc_start",
                               lambda p: starts.get(p)):
            return fleet._daemon_pids()

    DAEMON = ["orca-ide", "/x/daemon-entry.js", "--socket", "/s"]

    def test_proven_daemon_is_bracketed_with_its_starttime(self):
        self.assertEqual(self._scan({99: self.DAEMON}, {99: "111"}),
                         ({99: "111"}, set(), False))

    def test_daemon_shape_without_starttime_is_unproven_not_dropped(self):
        # the review's exact probe: daemon argv recognized, _proc_start=None
        # -> previously ({}, False); now the pid survives as UNPROVEN
        self.assertEqual(self._scan({99: self.DAEMON}, {}),
                         ({}, {99}, False))

    def test_unreadable_cmdline_is_unproven_not_dropped(self):
        self.assertEqual(self._scan({77: None}, {}), ({}, {77}, False))

    def test_pid_gone_during_cmdline_scan_is_absence_not_partial(self):
        self.assertEqual(self._scan({77: None}, {}, gone={77}),
                         ({}, set(), False))

    def test_ordinary_processes_enter_neither_set(self):
        self.assertEqual(self._scan({8: ["bash", "-c", "sleep 1"]}, {}),
                         ({}, set(), False))

    def test_failed_proc_listing_is_scan_failed(self):
        with mock.patch.object(fleet.os, "listdir",
                               mock.Mock(side_effect=OSError)):
            self.assertEqual(fleet._daemon_pids(), ({}, set(), True))


class OrcaTerminalsTest(unittest.TestCase):
    """Terminal inventory is bound to one daemon/runtime and must be complete."""

    def _terms(self, listing, status=None, daemon_ppid=10, start="g1"):
        status = status or {
            "ok": True, "result": {"app": {"pid": 10},
                                    "runtime": {"runtimeId": "r1"}}}
        replies = iter((status, listing))
        env = {"ORCA_USER_DATA_PATH": os.path.expanduser("~/.config/orca")}
        with mock.patch.object(fleet, "_orca_json",
                               side_effect=lambda *a: next(replies)), \
             mock.patch.object(fleet, "_daemon_user_data",
                               return_value=os.path.realpath(env["ORCA_USER_DATA_PATH"])), \
             mock.patch.object(fleet, "_stat_ppid", return_value=daemon_ppid), \
             mock.patch.object(session, "_proc_start", return_value=start):
            return fleet._orca_terminals(99, "g1", env)

    @staticmethod
    def listing(terms=(), truncated=False, total=None, runtime="r1"):
        terms = list(terms)
        return {"ok": True, "result": {"terminals": terms,
                                         "truncated": truncated,
                                         "totalCount": len(terms) if total is None else total},
                "_meta": {"runtimeId": runtime}}

    def test_documented_complete_shape_is_runtime_bound(self):
        terms = [{"handle": "t1", "tabId": "a", "leafId": "b",
                  "connected": True, "writable": True}]
        got, failed = self._terms(self.listing(terms))
        self.assertFalse(failed)
        self.assertEqual(got[0]["handle"], "t1")
        self.assertEqual((got[0]["_orca_cli"], got[0]["_runtime_id"]),
                         ("orca", "r1"))

    def test_successful_empty_list_is_a_fact_not_a_failure(self):
        self.assertEqual(self._terms(self.listing()), ([], False))

    def test_truncated_or_count_mismatch_is_failed(self):
        self.assertEqual(self._terms(self.listing([], truncated=True)),
                         ([], True))
        self.assertEqual(self._terms(self.listing([], total=1)), ([], True))

    def test_wrong_runtime_or_daemon_owner_is_failed(self):
        self.assertEqual(self._terms(self.listing(runtime="other")),
                         ([], True))
        self.assertEqual(self._terms(self.listing(), daemon_ppid=11),
                         ([], True))

    def test_wrong_shapes_are_failed_probes(self):
        for listing in (None, {"ok": True, "result": []},
                        {"ok": True, "result": {"terminals": {}}},
                        {"ok": True, "result": {}}):
            self.assertEqual(self._terms(listing), ([], True), listing)


class UnknownPlumbingTest(FleetRowsBase):
    """Finding 3 / finding 2: a failed probe is UNKNOWN in the
    ROW and the FOOTER — never converted into HEADLESS/no-pane or an
    owner-cannot-see claim."""

    def test_unprovable_host_renders_unknown_not_headless(self):
        unk = lambda pid, start, ds, unproven: ("unknown", None)  # noqa: E731
        census = [srow(3, SID_A, "declared", root="/r")]
        rows, _ = self._rows({3: {}}, census, daemon_for=unk)
        self.assertEqual(rows[0]["daemon_state"], "unknown")
        self.assertTrue(rows[0]["unknown"])
        out, rc = self._render_rc({3: {}}, census, daemon_for=unk)
        self.assertEqual(rc, 1)  # row UNKNOWN must never ride a shell PASS
        self.assertIn("host=?", out)
        self.assertNotIn("HEADLESS", out)
        self.assertNotIn("owner cannot see", out)
        self.assertIn("UNKNOWN columns", out)

    def test_proven_headless_still_raises_the_ghost_warning(self):
        census = [srow(3, SID_A, "declared", root="/r")]
        out = self._render({3: {}}, census)  # no daemons -> proven headless
        self.assertIn("HEADLESS/no-pane", out)
        self.assertIn("owner cannot see", out)
        self.assertNotIn("UNKNOWN columns", out)

    def test_daemon_scan_failure_makes_every_host_unknown(self):
        boom = lambda p, start, ds, unp: self.fail("walk must not run")  # noqa: E731
        rows, _ = self._rows({3: {}}, [srow(3, SID_A, "declared", root="/r")],
                             daemons_failed=True, daemon_for=boom)
        self.assertEqual(rows[0]["daemon_state"], "unknown")
        self.assertTrue(rows[0]["unknown"])

    def test_terminal_list_failure_marks_hosted_rows_unknown(self):
        census = [srow(3, SID_A, "declared", root="/r")]
        rows, _ = self._rows({3: {}}, census, daemons={99: "1"},
                             terminals=([], True))
        self.assertEqual(rows[0]["daemon"], 99)
        self.assertIsNone(rows[0]["pane"])
        self.assertTrue(rows[0]["unknown"])
        # a SUCCESSFUL empty terminal list is a fact, not a failed probe
        rows, _ = self._rows({3: {}}, census, daemons={99: "1"},
                             terminals=([], False))
        self.assertFalse(rows[0]["unknown"])

    def test_unreadable_cwd_marks_the_row_unknown(self):
        rows, _ = self._rows({3: {}}, [srow(3, SID_A, "declared", root="/r",
                                            cwd=None)])
        self.assertEqual(rows[0]["cwd"], "?")
        self.assertTrue(rows[0]["unknown"])


class GenerationBracketTest(FleetRowsBase):
    """Round-3 finding 1: a census row is only coherent for ITS
    process generation. Env facts come from the census's bracketed environ
    (never a later live re-read), and the host walk's fresh /proc reads are
    only composed in when a FINAL recheck proves the same generation still
    owns the pid — run after all display probes."""

    def test_seat_deck_stamps_come_from_the_census_bracket(self):
        # our OWN pid: if fleet still re-read the live proc environ it would
        # get THIS process's real environment, not the bracketed marker
        pid = os.getpid()
        census = [srow(pid, SID_A, "declared", root="/r")]
        envs = {pid: {"HELM_CHAT_NAME": "bracketed-seat",
                      "CLAUDE_CODE_SESSION_ID": "x",
                      "HELM_SKILL_DECK": "/x/helm-skills"}}
        rows, _ = self._rows(envs, census)
        r = rows[0]
        self.assertEqual((r["seat"], r["seat_src"], r["stamps"], r["deck"]),
                         ("bracketed-seat", "env", 1, "helm"))
        self.assertFalse(r["unknown"])

    def test_reused_pid_display_probes_are_discarded_not_composed(self):
        # the review's exact probe: old canonical row (sid/home/cwd) + a
        # reused pid answering the walk as proven-HEADLESS with a new seat.
        # The failed recheck must kill the host claim to UNKNOWN — never
        # compose old census facts with the new process's ancestry
        census = [srow(7, SID_A, "declared", root="/r", start="g-old")]
        wire = dict(daemon_for=lambda pid, start, ds, unp: ("headless", None),
                    generation=lambda pid, start: False)
        rows, _ = self._rows({7: {"HELM_CHAT_NAME": "old-seat"}}, census,
                             **wire)
        r = rows[0]
        self.assertEqual((r["daemon_state"], r["daemon"], r["pane"]),
                         ("unknown", None, None))
        self.assertTrue(r["unknown"])
        out = self._render({7: {"HELM_CHAT_NAME": "old-seat"}}, census,
                           **wire)
        self.assertIn("host=?", out)
        self.assertNotIn("HEADLESS", out)
        self.assertIn("UNKNOWN columns", out)

    def test_failed_recheck_also_kills_a_daemon_pane_claim(self):
        census = [srow(7, SID_A, "declared", root="/r", cwd="/w/a")]
        terms = [{"handle": "term_1", "worktreePath": "/w/a"}]
        rows, _ = self._rows({7: {}}, census, daemons={99: "1"},
                             terminals=(terms, False),
                             generation=lambda pid, start: False)
        r = rows[0]
        self.assertEqual((r["daemon_state"], r["daemon"], r["pane"]),
                         ("unknown", None, None))
        self.assertTrue(r["unknown"])

    def test_intact_generation_keeps_the_proven_host(self):
        # affirmative counterpart, and the recheck receives the row's OWN
        # exported bracket generation
        seen = []
        census = [srow(7, SID_A, "declared", root="/r", start="g-live")]
        rows, _ = self._rows({7: {}}, census, daemons={99: "1"},
                             generation=lambda pid, start: (
                                 seen.append((pid, start)) or True))
        self.assertEqual(seen, [(7, "g-live")])
        self.assertEqual(rows[0]["daemon"], 99)
        self.assertFalse(rows[0]["unknown"])

    def test_recheck_runs_after_every_display_probe(self):
        order = []
        census = {7: srow(7, SID_A, "declared", root="/r", cwd="/w/a")}

        def daemon_for(pid, start, ds, unp):
            order.append("walk")
            return "daemon", 99

        def terminals(daemon, start, env):
            order.append("terminals")
            return [{"handle": "t"}], False

        def generation(pid, start):
            order.append("recheck")
            return True
        with mock.patch.object(fleet, "_census",
                               lambda: (census, False, False, False)), \
             mock.patch.object(fleet, "_daemon_pids",
                               lambda: ({99: "1"}, set(), False)), \
             mock.patch.object(fleet, "_daemon_for", daemon_for), \
             mock.patch.object(fleet, "_roster", lambda: ({}, False)), \
             mock.patch.object(fleet, "_orca_terminals", terminals), \
             mock.patch.object(fleet, "_pane_for",
                               lambda env, terms: ("t", True)), \
             mock.patch.object(fleet, "_generation_intact", generation):
            fleet.rows()
        self.assertEqual(order, ["walk", "terminals", "recheck"])

    def test_generation_intact_is_a_real_starttime_recheck(self):
        # the REAL probe: our own live pid verifies against its true
        # starttime, and NOTHING else — wrong bracket, missing bracket, and
        # a nonexistent pid all refuse
        pid = os.getpid()
        start = session._proc_start(pid)
        self.assertIsNotNone(start)
        self.assertTrue(fleet._generation_intact(pid, start))
        self.assertFalse(fleet._generation_intact(pid, "999"))
        self.assertFalse(fleet._generation_intact(pid, None))
        self.assertFalse(fleet._generation_intact(2 ** 22 + 12345, start))


class CensusCompletenessTest(FleetRowsBase):
    """Round-3 finding 2: the sole-source census carries a
    completeness channel. A failed /proc enumeration is estate-UNKNOWN
    (exit 1), never a certified-empty fleet; a failed who scan marks every
    sub-declared/resume row sid-UNKNOWN."""

    def test_census_failure_cannot_certify_an_empty_estate(self):
        table, daemons, flags = self._rows_full(
            {}, (), census_failed=True)
        self.assertEqual(table, [])
        self.assertTrue(flags["census_failed"])
        out, rc = self._render_rc({}, (), census_failed=True)
        self.assertEqual(rc, 1)
        self.assertIn("CENSUS FAILED", out)
        self.assertIn("UNKNOWN, not empty", out)
        self.assertNotIn("0 live claude process(es)", out)

    def test_census_failure_reaches_json_consumers(self):
        out, rc = self._render_rc({}, (), census_failed=True,
                                  args=("--json",))
        self.assertEqual(rc, 1)
        self.assertTrue(json.loads(out)["census_failed"])
        # and a healthy run still certifies the affirmative bit
        out, rc = self._render_rc({}, (), args=("--json",))
        self.assertEqual(rc, 0)
        self.assertFalse(json.loads(out)["census_failed"])

    def test_successful_empty_census_is_a_fact(self):
        out, rc = self._render_rc({}, ())
        self.assertEqual(rc, 0)
        self.assertIn("0 live claude process(es)", out)

    def test_who_scan_failure_marks_sub_who_rows_sid_unknown(self):
        # declared/resume rows outrank the who rung and stay proven; rows
        # whose ladder bottomed out BELOW them may only look unresolved
        # because the who probe vanished — UNKNOWN, not a proven blank
        census = [srow(1, SID_A, "declared", root="/r"),
                  srow(2, SID_B, "resume", root="/r"),
                  srow(3, possible=[SID_A], root="/r"),
                  srow(4, root="/r")]
        child = srow(5, root="/r")
        child["child"] = True  # who is suppressed for children by design
        envs = {p: {} for p in (1, 2, 3, 4, 5)}
        rows, _ = self._rows(envs, census + [child], who_failed=True)
        by = {r["pid"]: r for r in rows}
        self.assertFalse(by[1]["unknown"])
        self.assertFalse(by[2]["unknown"])
        self.assertTrue(by[3]["unknown"])
        self.assertTrue(by[4]["unknown"])
        self.assertFalse(by[5]["unknown"])
        # the healthy counterpart: same rows, probed who, no taint
        rows, _ = self._rows(envs, census + [child])
        self.assertFalse(any(r["unknown"] for r in rows))


def pfrow(pid, start="g1"):
    """One probe-failed census row exactly as session shapes it: comm proved
    claude, then a mandatory read failed while the pid persisted."""
    return {"pid": pid, "resume": None, "declared": None,
            "declared_reason": "probe-failed", "cwd": None, "root": None,
            "start": start, "environ": None, "identity": "unknown",
            "session": None, "possible_sessions": [], "child": False,
            "ancestor_sid8": "", "force": False, "probe_failed": True}


class PerPidProbeFailureTest(FleetRowsBase):
    """HIGH: per-PID mandatory probe failures below the global bits
    must never vanish as proven absence. A post-comm failure is an UNKNOWN
    row in the output AND the exit status; a pre-comm failure is
    census_partial — the estate total is a floor, surfaced like
    listing_failed, exit nonzero."""

    def test_probe_failed_row_is_unknown_and_fails_the_exit_status(self):
        census = [pfrow(41)]
        table, _daemons, flags = self._rows_full({}, census)
        r = table[0]
        self.assertTrue(r["probe_failed"])
        self.assertTrue(r["unknown"])
        self.assertIsNone(r["sid"])
        self.assertFalse(flags["census_failed"])
        self.assertFalse(flags["census_partial"])
        out, rc = self._render_rc({}, census)
        self.assertEqual(rc, 1)
        self.assertIn("pid 41", out)
        self.assertIn("mandatory census probe", out)
        self.assertIn("never proven absence", out)
        self.assertIn("UNKNOWN columns", out)

    def test_probe_failed_reaches_json_consumers(self):
        out, rc = self._render_rc({}, [pfrow(41)], args=("--json",))
        self.assertEqual(rc, 1)
        data = json.loads(out)
        self.assertTrue(data["rows"][0]["probe_failed"])
        self.assertTrue(data["rows"][0]["unknown"])
        self.assertFalse(data["census_partial"])

    def test_census_partial_prints_a_floor_and_exits_nonzero(self):
        census = [srow(1, SID_A, "declared", root="/r")]
        out, rc = self._render_rc({1: {}}, census, census_partial=True)
        self.assertEqual(rc, 1)
        self.assertIn("at least 1 live claude process(es)", out)
        self.assertIn("CENSUS PARTIAL", out)
        self.assertIn("floor", out)

    def test_census_partial_reaches_json_consumers(self):
        out, rc = self._render_rc({}, (), census_partial=True,
                                  args=("--json",))
        self.assertEqual(rc, 1)
        self.assertTrue(json.loads(out)["census_partial"])
        # the healthy counterpart still certifies the affirmative bits
        out, rc = self._render_rc({}, (), args=("--json",))
        self.assertEqual(rc, 0)
        self.assertFalse(json.loads(out)["census_partial"])

    def test_healthy_estate_total_is_not_a_floor(self):
        out, rc = self._render_rc({1: {}},
                                  [srow(1, SID_A, "declared", root="/r")])
        self.assertEqual(rc, 0)
        self.assertNotIn("at least", out)
        self.assertNotIn("CENSUS PARTIAL", out)


@unittest.skipIf(os.geteuid() == 0, "root bypasses file permissions")
class ProbeFailedEndToEndTest(unittest.TestCase):
    """The finding's exact probe, END TO END through the CLI: a planted
    /proc pid whose comm proves 'claude' and whose cmdline raises
    PermissionError must surface as an UNKNOWN row and a nonzero exit —
    helm fleet must never certify 0 live processes after failing to read a
    KNOWN claude pid."""

    def _run(self, args):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        base = os.path.join(tmp, "41")
        os.makedirs(base)
        with open(os.path.join(base, "comm"), "wb") as f:
            f.write(b"claude\n")
        cmdline = os.path.join(base, "cmdline")
        with open(cmdline, "wb") as f:
            f.write(b"claude\0")
        with open(os.path.join(base, "environ"), "wb") as f:
            f.write(b"HOME=/nonexistent-home\0")
        fields = ["S"] + [str(i) for i in range(4, 22)] + ["424242", "0", "0"]
        with open(os.path.join(base, "stat"), "w") as f:
            f.write("41 (claude) %s\n" % " ".join(fields))
        os.symlink(tmp, os.path.join(base, "cwd"))
        os.chmod(cmdline, 0)
        self.addCleanup(os.chmod, cmdline, 0o644)
        buf = io.StringIO()
        with mock.patch.object(session, "PROC", tmp), \
             mock.patch.object(who, "scan", return_value=[]), \
             mock.patch.object(fleet, "_daemon_pids",
                               lambda: ({}, set(), False)), \
             mock.patch.object(fleet, "_daemon_for",
                               lambda *a: ("unknown", None)), \
             mock.patch.object(fleet, "_roster", lambda: ({}, False)), \
             contextlib.redirect_stdout(buf):
            rc = fleet.cmd_fleet(list(args))
        return buf.getvalue(), rc

    def test_unreadable_known_claude_pid_is_unknown_row_nonzero_exit(self):
        out, rc = self._run(["--json"])
        self.assertEqual(rc, 1)
        data = json.loads(out)
        [row] = data["rows"]
        self.assertEqual(row["pid"], 41)
        self.assertTrue(row["probe_failed"])
        self.assertTrue(row["unknown"])
        self.assertIsNone(row["sid"])
        self.assertFalse(data["census_failed"])
        self.assertFalse(data["census_partial"])
        out, rc = self._run([])
        self.assertEqual(rc, 1)
        self.assertIn("pid 41", out)
        self.assertIn("mandatory census probe", out)


class SidParserTest(unittest.TestCase):
    """The REAL session parsers fleet's census delegates to — no mocks on
    the parser under test."""

    def test_record_with_wrong_procstart_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            sdir = os.path.join(root, "sessions")
            os.makedirs(sdir)
            with open(os.path.join(sdir, "4242.json"), "w") as f:
                json.dump({"pid": 4242, "sessionId": SID_A,
                           "procStart": "111"}, f)
            uid = os.geteuid()
            self.assertEqual(
                session._read_session_record(root, 4242, uid, "222"),
                (None, "record-stale"))
            # same record, matching procStart: accepted — proves the guard
            # (not some earlier failure) is what refused the stale one
            self.assertEqual(
                session._read_session_record(root, 4242, uid, "111"),
                (SID_A, "record-ok"))

    def test_resume_flag_consuming_another_flag_yields_no_sid(self):
        self.assertIsNone(session._resume_sid(
            ["claude", "--resume", "--model", "opus"]))

    def test_conflicting_resume_sids_yield_no_sid(self):
        self.assertIsNone(session._resume_sid(
            ["claude", "--resume", SID_A, "--resume=" + SID_B]))

    def test_valid_resume_beside_an_invalid_occurrence_fails_closed(self):
        # Parser note: contradictory evidence poisons the parse —
        # a valid --resume plus a bare/invalid repeat is UNKNOWN
        self.assertIsNone(session._resume_sid(
            ["claude", "--resume", SID_A, "--resume"]))
        self.assertIsNone(session._resume_sid(
            ["claude", "--resume", SID_A, "--resume=not-a-sid"]))
        self.assertIsNone(session._resume_sid(
            ["claude", "--resume=" + SID_A[:8], "--resume", SID_A]))

    def test_agreeing_repeats_still_resolve(self):
        self.assertEqual(session._resume_sid(
            ["claude", "--resume", SID_A, "--resume=" + SID_A]), SID_A)


class PaneIdentityTest(FleetRowsBase):
    """Pane identity comes from runtime pane-key resolution, never cwd."""

    TERMS = ([{"handle": "term_new", "ptyId": "pty-1", "worktreeId": "w1",
               "connected": True, "writable": True,
               "_runtime_id": "r1"}], False)
    ENV = {"ORCA_PANE_KEY": "tab:leaf", "ORCA_USER_DATA_PATH": "/orca",
           "ORCA_WORKTREE_ID": "w1", "ORCA_TERMINAL_HANDLE": "term_stale"}
    RESOLVED = {"terminal": {"handle": "term_new", "ptyId": "pty-1"}}

    def test_hosted_row_resolves_replacement_handle_by_pane_key(self):
        census = [srow(10, SID_A, "declared", root="/r", cwd="/w/x")]
        with mock.patch.object(fleet, "_orca_runtime_call",
                               return_value=self.RESOLVED):
            rows, _ = self._rows(
                {10: self.ENV}, census, {99: "s1"}, terminals=self.TERMS,
                pane_for=fleet._pane_for)
        self.assertEqual(rows[0]["pane"], "term_new")
        self.assertFalse(rows[0]["unknown"])

    def test_two_rows_cannot_both_certify_one_pane(self):
        census = [srow(10, SID_A, "declared", root="/r"),
                  srow(11, SID_B, "declared", root="/r")]
        with mock.patch.object(fleet, "_orca_runtime_call",
                               return_value=self.RESOLVED):
            rows, _ = self._rows(
                {10: self.ENV, 11: self.ENV}, census, {99: "s1"},
                terminals=self.TERMS, pane_for=fleet._pane_for)
        self.assertTrue(all(r["pane"] is None and r["unknown"] for r in rows))

    def test_hosted_row_without_authoritative_pane_key_is_unknown(self):
        census = [srow(10, SID_A, "declared", root="/r", cwd="/w/x")]
        out, rc = self._render_rc(
            {10: {}}, census, daemons={99: "s1"}, terminals=self.TERMS,
            pane_for=fleet._pane_for, args=("--json",))
        self.assertEqual(rc, 1)
        self.assertTrue(json.loads(out)["rows"][0]["unknown"])


class EstateProbeExitTest(FleetRowsBase):
    """fable review MED (both lenses): every estate-wide failed probe — who
    scan, daemon scan, terminal list — must reach the machine-readable
    verdict exactly like census_failed/census_partial: exit 1 and a named
    --json completeness bit. A scripted consumer keying on rc or the JSON
    estate bits must never read PASS while that truth went unprobed."""

    def _bits(self, *a, **kw):
        out, rc = self._render_rc(*a, args=("--json",), **kw)
        return json.loads(out), rc

    def test_who_failure_gates_exit_code_and_json(self):
        census = [srow(4, root="/r")]
        _out, rc = self._render_rc({4: {}}, census, who_failed=True)
        self.assertEqual(rc, 1)
        data, rc = self._bits({4: {}}, census, who_failed=True)
        self.assertEqual(rc, 1)
        self.assertTrue(data["who_failed"])
        # healthy counterpart still certifies the affirmative bit
        data, rc = self._bits({4: {}}, census)
        self.assertEqual(rc, 0)
        self.assertFalse(data["who_failed"])

    def test_daemon_scan_failure_gates_exit_code_and_json(self):
        census = [srow(3, SID_A, "declared", root="/r")]
        boom = lambda p, start, ds, unp: self.fail("walk must not run")  # noqa: E731
        _out, rc = self._render_rc({3: {}}, census, daemons_failed=True,
                                   daemon_for=boom)
        self.assertEqual(rc, 1)
        data, rc = self._bits({3: {}}, census, daemons_failed=True,
                              daemon_for=boom)
        self.assertEqual(rc, 1)
        self.assertTrue(data["daemons_failed"])
        data, rc = self._bits({3: {}}, census)
        self.assertEqual(rc, 0)
        self.assertFalse(data["daemons_failed"])

    def test_partial_daemon_census_is_a_floor_and_gates_exit(self):
        census = [srow(3, SID_A, "declared", root="/r")]
        data, rc = self._bits({3: {}}, census, unproven={77})
        self.assertEqual(rc, 1)
        self.assertTrue(data["daemons_partial"])
        out, rc = self._render_rc({3: {}}, census, unproven={77})
        self.assertEqual(rc, 1)
        self.assertIn("DAEMON CENSUS PARTIAL", out)
        self.assertIn("at least 0 orca daemon", out)

    def test_terminal_list_failure_gates_exit_code_and_json(self):
        census = [srow(3, SID_A, "declared", root="/r")]
        hosted = dict(daemons={99: "1"}, terminals=([], True))
        _out, rc = self._render_rc({3: {}}, census, **hosted)
        self.assertEqual(rc, 1)
        data, rc = self._bits({3: {}}, census, **hosted)
        self.assertEqual(rc, 1)
        self.assertTrue(data["terms_failed"])
        data, rc = self._bits({3: {}}, census, daemons={99: "1"})
        self.assertEqual(rc, 0)
        self.assertFalse(data["terms_failed"])

    def test_text_render_names_the_failed_estate_probes(self):
        out, rc = self._render_rc({4: {}}, [srow(4, root="/r")],
                                  who_failed=True, daemons_failed=True)
        self.assertEqual(rc, 1)
        self.assertIn("estate-wide probe(s) FAILED", out)
        self.assertIn("who scan", out)
        self.assertIn("daemon scan", out)
        self.assertIn("never proven blanks", out)


class ProbeOrderingTest(unittest.TestCase):
    """fable review LOW: the daemon scan runs AFTER the census bracket. A
    daemon that starts between the two scans — whose freshly-spawned claude
    IS censused — is then in the set, so the ppid walk cannot pass through
    the missing pid to init and read a false proven-HEADLESS (a ghost
    warning for a process with a live pane)."""

    def test_daemon_scan_runs_after_the_census(self):
        order = []

        def census():
            order.append("census")
            return {}, False, False, False

        def daemon_pids():
            order.append("daemons")
            return {}, set(), False
        with mock.patch.object(fleet, "_census", census), \
             mock.patch.object(fleet, "_daemon_pids", daemon_pids), \
             mock.patch.object(fleet, "_roster", lambda: ({}, False)):
            fleet.rows()
        self.assertEqual(order, ["census", "daemons"])


class PaneMappingTest(unittest.TestCase):
    TERMS = [{"handle": "term_1", "ptyId": "pty-1", "worktreeId": "w1",
              "connected": True, "writable": True, "_runtime_id": "r1"},
             {"handle": "term_old", "ptyId": "pty-old",
              "worktreeId": "w1", "connected": True, "writable": True,
              "_runtime_id": "r1"}]
    ENV = {"ORCA_PANE_KEY": "stable-tab:stable-leaf",
           "ORCA_USER_DATA_PATH": "/orca-data", "ORCA_WORKTREE_ID": "w1"}
    RESOLVED = {"terminal": {"handle": "term_1", "ptyId": "pty-1",
                              "tabId": "stable-tab",
                              "leafId": "stable-leaf"}}

    def test_runtime_pane_key_binds_live_handle_pty_and_worktree(self):
        with mock.patch.object(fleet, "_orca_runtime_call",
                               return_value=self.RESOLVED) as call:
            self.assertEqual(fleet._pane_for(self.ENV, self.TERMS),
                             ("term_1", True))
        call.assert_called_once_with(
            "/orca-data", "r1", "terminal.resolvePane",
            {"paneKey": "stable-tab:stable-leaf"})

    def test_live_conflicting_inherited_handle_refuses(self):
        env = dict(self.ENV, ORCA_TERMINAL_HANDLE="term_old")
        with mock.patch.object(fleet, "_orca_runtime_call",
                               return_value=self.RESOLVED):
            self.assertEqual(fleet._pane_for(env, self.TERMS), (None, False))

    def test_stale_absent_handle_allows_identity_proven_remint(self):
        env = dict(self.ENV, ORCA_TERMINAL_HANDLE="gone-handle")
        with mock.patch.object(fleet, "_orca_runtime_call",
                               return_value=self.RESOLVED):
            self.assertEqual(fleet._pane_for(env, self.TERMS),
                             ("term_1", True))

    def test_missing_key_runtime_or_live_pty_is_unproven(self):
        self.assertEqual(fleet._pane_for({}, self.TERMS), (None, False))
        with mock.patch.object(fleet, "_orca_runtime_call", return_value=None):
            self.assertEqual(fleet._pane_for(self.ENV, self.TERMS),
                             (None, False))
        wrong = {"terminal": {"handle": "term_1", "ptyId": "other"}}
        with mock.patch.object(fleet, "_orca_runtime_call", return_value=wrong):
            self.assertEqual(fleet._pane_for(self.ENV, self.TERMS),
                             (None, False))


if __name__ == "__main__":
    unittest.main()

def _widen(v):
    """Let an arm write (counts, blind, why) and mean known = no reason.

    `_throttle` gained a typed knownness so a failed read could reach fleet's
    exit code; an arm that cares about knownness passes the fourth element
    itself, and the rest keep saying what they were already saying.
    """
    return v if len(v) == 4 else (v[0], v[1], v[2], not v[2])


class ThrottleIsReportedWhereItBelongsTest(FleetRowsBase):
    """A cgroup above the per-process leaves has ONE counter that every
    descendant inherits, so printing it on every row repeats a single fact
    once per process and buries the level that is about one of them."""

    ENVS = {41: {"HELM_CHAT_NAME": "seat-a"}, 42: {"HELM_CHAT_NAME": "seat-b"}}
    # BUILT BY THE FILE'S OWN FACTORY, not by hand: a census row carries
    # identity, possible_sessions and the bracket fields, and a dict invented
    # from the three keys this arm cares about raises KeyError inside rows()
    # rather than testing anything.
    CENSUS = (srow(41, SID_A, "declared", root="/h/.claude", start="g1"),
              srow(42, SID_B, "declared", root="/h/.claude", start="g2"))

    def _out(self, per_pid):
        return self._render(self.ENVS, census=self.CENSUS,
                            throttle=lambda pid, start: _widen(per_pid[pid]))

    def test_a_failed_acquisition_reaches_the_ROW_bits_and_the_footer(self):  # noqa: VACUOUS_ASSERTION — the assertNotIn is preceded on the SAME render by two unconditional positives: rc must be 0 and both rows' unknown bits must read False, so an inert renderer reddens above rather than passing the absence
        """ONE END-TO-END CONTROL. The row-level `unknown` bit's stated
        contract is that it carries EVERY failed probe the row rests on, and
        the file says WHY directly under it: computed once where the causes
        are in hand, so no reader re-derives the disjunction and drifts.

        A failed throttle acquisition is a failed probe. It was reaching the
        estate flag and the row's own throttle_known field while the
        disjunction that names failed probes never asked, so JSON carried a
        known-row bit beside an explicit failure and the footer omitted it.

        rc is NOT the subject here — it already worked before this and is
        asserted only so a reader can see the whole path agree.
        """
        good = lambda pid, start: ({}, 0, "", True)  # noqa: E731
        out, rc = self._render_rc(self.ENVS, census=self.CENSUS,
                                  throttle=good)
        self.assertEqual(rc, 0, "control: every probe succeeding must leave "
                                "the rows known, or the bits below say nothing")
        self.assertNotIn("carry UNKNOWN columns", out)
        rows = json.loads(self._render(self.ENVS, census=self.CENSUS,
                                       throttle=good, args=("--json",)))["rows"]
        self.assertEqual([r["unknown"] for r in rows], [False, False])

        bad = lambda pid, start: ({}, 1, "the read did not finish", False)  # noqa: E731
        out, rc = self._render_rc(self.ENVS, census=self.CENSUS, throttle=bad)
        rows = json.loads(self._render(self.ENVS, census=self.CENSUS,
                                       throttle=bad, args=("--json",)))["rows"]
        self.assertEqual([r["unknown"] for r in rows], [True, True],
                         "the row bit must carry the failed acquisition")
        self.assertEqual([r["probe_unknown"] for r in rows], [True, True],
                         "and so must probe_unknown — a read that did not "
                         "answer IS a failed probe, where a seat refusal is not")
        self.assertIn("carry UNKNOWN columns", out)
        self.assertEqual(rc, 1)

    def test_a_seat_refusal_alone_is_not_a_failed_probe(self):
        """The two disjunctions differ on purpose and joining a new cause to
        both must not collapse them: an unrenderable name is refused, not a
        probe that failed."""
        envs = {41: {"HELM_CHAT_NAME": "a\x1b[2Jb"},
                42: {"HELM_CHAT_NAME": "seat-b"}}
        rows = json.loads(self._render(
            envs, census=self.CENSUS,
            throttle=lambda pid, start: ({}, 0, "", True),
            args=("--json",)))["rows"]
        refused = [r for r in rows if r.get("seat_unrenderable")]
        self.assertEqual(len(refused), 1, "control: one name must be refused")
        self.assertTrue(refused[0]["unknown"])
        self.assertFalse(refused[0]["probe_unknown"],
                         "a refusal is not a failed probe")

    def _row_block(self, out, pid):
        """The lines of ONE pid's row, header to the next header.

        AN ASSERTION ABOUT A ROW MAY NOT READ THE WHOLE SCREEN. The estate
        footer prints the SHARED value, so a render-wide assertIn for C's
        counter is satisfied by the footer whenever C's value happens to
        equal the shared one — which is exactly the case that discriminates
        a membership-keyed subtraction from a path-keyed one, so the arm
        went blind in the only case it was written for.
        """
        block, inside = [], False
        for line in out.split("\n"):
            if line.startswith("  pid "):
                if inside:
                    break
                inside = line.split()[1] == str(pid)
                if inside:
                    block.append(line)
                continue
            if not inside:
                continue
            # A ROW'S CONTINUATION LINES ARE INDENTED SEVEN; the estate
            # footer is indented two. Stopping only at the NEXT row header
            # runs the LAST row's block to end-of-output and swallows the
            # footer — which is the same whole-screen read this helper
            # exists to prevent, and it passed the discriminating probe
            # while doing it.
            if not line.startswith("       "):
                break
            block.append(line)
        return "\n".join(block)

    def test_the_footer_subtracts_only_from_the_rows_it_factored(self):  # noqa: VACUOUS_ASSERTION — the assertNotIn's control is the unconditional assertEqual(seen, [9, 7]) AFTER the loop, which fails if either iteration was skipped, plus the assertTrue(c_row) and assertIn on C's own block inside each one
        """A shared path is shared among THE COUNTED ROWS. Subtracting it from
        a row that was never in that computation deletes that row's own value
        on the authority of a fact that was never about it."""
        envs = {**self.ENVS, 43: {"HELM_CHAT_NAME": "seat-c"}}
        census = self.CENSUS + (srow(43, SID_C, "declared",
                                     root="/h/.claude", start="g3"),)
        seen = []
        for c_value in (9, 7):
            per_pid = {41: ({"/x/agents.slice": 7}, 0, "", True),
                       42: ({"/x/agents.slice": 7}, 0, "", True),
                       # PARTIAL: one level acquired, another did not answer,
                       # so C is not eligible for the factoring at all.
                       43: ({"/x/agents.slice": c_value}, 1, "", False)}
            out = self._render(envs, census=census,
                               throttle=lambda pid, start: per_pid[pid])
            self.assertIn("shared by all 2 of 2 observed process(es)", out,
                          "control: A and B still factor")
            c_row = self._row_block(out, 43)
            self.assertTrue(c_row, "control: C's row must render at all, or "
                                   "the assertion below is about nothing")
            self.assertIn("agents.slice=%d" % c_value, c_row,
                          "C's own value survives ON C'S OWN ROW (value %d)"
                          % c_value)
            # AND THE MEMBERS LOST IT, which is the other half of factoring:
            # if A still carried the shared count the footer would be a
            # duplicate rather than a replacement.
            self.assertNotIn("agents.slice=", self._row_block(out, 41),
                             "a factored member must not repeat the shared "
                             "count on its own row")
            seen.append(c_value)
        # UNCONDITIONAL, because every assertion above is inside the loop and
        # a loop that never ran passes them all. Both cases matter: C's value
        # DIFFERING from the shared one and C's value EQUAL to it, which is
        # the case a path-keyed subtraction cannot tell from membership.
        self.assertEqual(seen, [9, 7])

    def test_a_throttle_read_that_did_not_complete_gates_the_exit_code(self):
        """This file's rc comment says every row-level or estate-wide failed
        probe gates the exit code, because UNKNOWN in JSON beside rc 0 launders
        a failed read into a successful shell verdict. The throttle read was
        the one probe that reached only the printed sentence."""
        _, rc = self._render_rc(self.ENVS, census=self.CENSUS,
                                throttle=lambda pid, start: ({}, 0, "", True))
        self.assertEqual(rc, 0, "control: a completed read must NOT gate rc, "
                                "or the 1 below says nothing about knownness")
        out, rc = self._render_rc(
            self.ENVS, census=self.CENSUS,
            throttle=lambda pid, start: ({}, 1, "the read did not finish",
                                         False))
        self.assertEqual(rc, 1)

    def test_the_shared_footer_names_who_it_left_out(self):
        """Two numbers say a subset was taken and do not say WHICH processes
        it left out. A population a reader cannot name is one they cannot
        check."""
        # A THIRD PROCESS ONLY THIS ARM NEEDS. Widening the shared fixture
        # would silently change what every other arm in the class measures.
        envs = {**self.ENVS, 43: {"HELM_CHAT_NAME": "seat-c"}}
        census = self.CENSUS + (srow(43, SID_C, "declared",
                                     root="/h/.claude", start="g3"),)
        per_pid = {41: ({"/x/agents.slice": 11136,
                         "/x/agents-p41.slice": 9548}, 0, "", True),
                   42: ({"/x/agents.slice": 11136}, 0, "", True),
                   43: ({}, 0, "", True)}
        out = self._render(envs, census=census,
                           throttle=lambda pid, start: per_pid[pid])
        self.assertIn("shared by all 2 of 3 observed process(es)", out)
        self.assertIn("reported no counter at all", out)
        self.assertIn("seat-c", out)

    def test_fleet_ASKS_the_seam_rather_than_reading_cgroups_itself(self):
        seen = []

        def spy(pid, start):
            seen.append((pid, start))
            return {}, 0, "", True
        self._rows_full(self.ENVS, census=self.CENSUS, throttle=spy)
        # THE GENERATION MUST REACH IT: without the bracketed start a reused
        # pid would be reported under the former seat's name.
        self.assertEqual(sorted(seen), [(41, "g1"), (42, "g2")])

    def test_a_level_every_row_shares_is_NOT_on_the_rows(self):
        out = self._out({41: ({"/x/agents.slice": 11136,
                               "/x/agents-p41.slice": 9548}, 0, ""),
                         42: ({"/x/agents.slice": 11136}, 0, "")})
        # the shared level appears ONCE, as an estate line, not per row
        self.assertEqual(out.count("agents.slice=11136"), 1, out)
        self.assertIn("shared by all", out)
        # MUST-HIT on the same render: the level that is NOT shared survives
        # on its own row, so the factoring removed a duplicate rather than
        # swallowing the finding.
        self.assertIn("agents-p41.slice=9548", out)

    def test_a_level_with_DIFFERENT_counts_is_not_treated_as_shared(self):
        """Same cgroup path, different numbers, is not one fact — it cannot
        be reported once without choosing whose number to print."""
        out = self._out({41: ({"/x/agents.slice": 11136}, 0, ""),
                         42: ({"/x/agents.slice": 22}, 0, "")})
        self.assertNotIn("shared by all", out)
        self.assertIn("11136", out)
        self.assertIn("=22", out)

    def test_an_unattributed_read_says_so_on_the_row(self):
        out = self._out({41: ({}, 0, "the process at this pid is not the "
                                    "one the census bracketed"),
                         42: ({}, 0, "")})
        self.assertIn("throttling UNKNOWN", out)
        self.assertIn("bracketed", out)

    def test_a_quiet_estate_prints_NO_throttle_line(self):  # noqa: VACUOUS_ASSERTION — the same body renders a SECOND estate that does have a count and asserts the line appears, so a renderer that never printed would redden here rather than pass the absence
        """Counters at zero say nothing rather than a reassuring zero, which
        would read as a clean bill the counters cannot support."""
        out = self._out({41: ({}, 0, ""), 42: ({}, 0, "")})
        self.assertNotIn("throttle", out)
        # MUST-HIT: the same renderer DOES print when there is something, so
        # the silence above is the quiet case and not a dead branch.
        loud = self._out({41: ({"/x/a.slice": 5}, 0, ""), 42: ({}, 0, "")})
        self.assertIn("throttle", loud)



class TheThrottleSeamCarriesItsReasonTest(unittest.TestCase):
    """`_throttle` returns (counts, blind, why, known) and the renderer prints a row
    whenever `why` is set. Suppressing `why` because the observation was
    ATTRIBUTED loses the one case that needs it: `observe` attributes a
    reading taken with no census bracket and reports the gap in `why`."""

    START = 4242

    def _world(self, rel, blind=None):
        """A whole synthetic world — /proc AND the cgroup tree — for one pid.

        NOTHING HERE IS DERIVED FROM THE HOST. A root parameter alone still
        leaves the fixture taking the path and the generation from the box it
        runs on, so the answer stays partly a property of that box; supplying
        both means every case below is determinate on any machine. `rel` is
        the cgroup line's path, so "/" is a process in the cgroup ROOT and a
        deeper path is an ordinary seat. `blind` names one segment to leave
        without a counter interface.
        """
        world = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, world, True)
        cg, proc = os.path.join(world, "cgroup"), os.path.join(world, "proc")
        pid_dir = os.path.join(proc, "7")
        os.makedirs(pid_dir)
        os.makedirs(cg, exist_ok=True)
        with open(os.path.join(pid_dir, "cgroup"), "w",
                  encoding="utf-8") as fh:
            fh.write("0::%s\n" % rel)
        with open(os.path.join(pid_dir, "stat"), "w", encoding="utf-8") as fh:
            fh.write("7 (helm) %s\n"
                     % " ".join(["S"] + ["0"] * 18 + [str(self.START)]))
        # THE ROOT CARRIES NO COUNTER INTERFACE, as on a real box: a
        # controller's files do not exist there by kernel design.
        d = cg
        for part in [x for x in rel.strip("/").split("/") if x]:
            d = os.path.join(d, part)
            os.makedirs(d, exist_ok=True)
            if part == blind:
                continue
            with open(os.path.join(d, "memory.events.local"), "w",
                      encoding="utf-8") as fh:
                fh.write("low 0\nhigh 9548\nmax 0\noom 0\noom_kill 0\n")
        return cg, proc

    def _ask(self, rel, blind=None, start=START):
        cg, proc = self._world(rel, blind)
        return fleet._throttle(7, start, root=cg, proc=proc)

    def test_an_unbracketed_read_still_says_it_was_unbracketed(self):
        counts, blind, why, known = self._ask("/a.slice/b.scope")
        self.assertEqual(why, "", "control: a bracketed read of a world that "
                                  "DOES answer has no reason to report, so "
                                  "the reason below is the gap")
        self.assertTrue(known, "and it is KNOWN, or the False below says "
                               "nothing about the missing bracket")
        counts, blind, why, known = self._ask("/a.slice/b.scope", start=None)
        self.assertIn("no census generation", why)
        self.assertFalse(known, "an unbracketed read is NOT known")

    def test_a_process_in_the_cgroup_ROOT_is_an_ordinary_answer(self):
        """The root carries no counter interface by kernel design, and that is
        the ONE structural exemption — a process sitting there is observable,
        not blind."""
        counts, blind, why, known = self._ask("/")
        self.assertTrue(known)
        self.assertEqual((counts, blind, why), ({}, 0, ""))

    def test_a_level_with_no_counter_interface_is_NOT_known(self):
        """And the exemption is the root alone: the same absence one level
        down is a read this seam could not take."""
        counts, blind, why, known = self._ask("/a.slice/b.scope",
                                              blind="a.slice")
        self.assertFalse(known)
        self.assertEqual(blind, 1)


class AThrottleReadThatDidNotAnswerIsNotAZeroTallyTest(FleetRowsBase):
    """`throttle_counts` {} is a process whose counters ALL READ ZERO. A read
    that raised, and a read taken against a pid that has since changed
    generation, both published that same {} (and a blind count of 0), so
    `helm fleet --json` carried a clean tally for a read that never answered
    for the row. Each is now None, with the reason on the row and, for the
    raise, a breadcrumb. The control is the answered zero tally, still {}."""

    ENVS = ThrottleIsReportedWhereItBelongsTest.ENVS
    CENSUS = ThrottleIsReportedWhereItBelongsTest.CENSUS

    def _json_rows(self, **wire):
        return json.loads(self._render(self.ENVS, census=self.CENSUS,
                                       args=("--json",), **wire))["rows"]

    def test_a_raise_is_None_with_its_reason_and_a_breadcrumb(self):
        from helm import record, seatceiling
        real = fleet._throttle
        good = self._json_rows(throttle=lambda pid, start: ({}, 0, "", True))
        self.assertEqual([(r["throttle_counts"], r["throttle_blind"],
                           r["throttle_known"]) for r in good],
                         [({}, 0, True)] * 2,
                         "control: an answered zero tally stays {} and 0")
        home = tempfile.mkdtemp(prefix="helm-fleet-throttle-raise-")
        self.addCleanup(shutil.rmtree, home, True)
        with mock.patch.dict(os.environ, {"HELM_HOME": home}), \
                mock.patch.object(seatceiling, "observe",
                                  side_effect=RuntimeError("cgroup gone")):
            rows = self._json_rows(throttle=real)
            out, rc = self._render_rc(self.ENVS, census=self.CENSUS,
                                      throttle=real)
            crumbs = [c for c in record.swallows()
                      if c.get("where") == "fleet._throttle"]
        why = "the throttle read raised RuntimeError"
        self.assertEqual([(r["throttle_counts"], r["throttle_blind"],
                           r["throttle_known"], r["throttle_why"])
                          for r in rows], [(None, None, False, why)] * 2)
        self.assertEqual([r["unknown"] for r in rows], [True, True])
        self.assertEqual(out.count("throttling UNKNOWN — " + why), 2, out)
        self.assertEqual(rc, 1, "a read that did not answer gates the exit")
        self.assertEqual(len(crumbs), 4, "one breadcrumb per raised read")
        self.assertEqual({c.get("exc") for c in crumbs}, {"RuntimeError"})

    def test_a_read_of_a_pid_that_changed_generation_is_None_not_zero(self):
        good = lambda pid, start: ({}, 0, "", True)  # noqa: E731
        kept = self._json_rows(throttle=good)
        self.assertEqual([r["throttle_counts"] for r in kept], [{}, {}],
                         "control: an intact generation keeps its tally")
        rows = self._json_rows(throttle=good,
                               generation=lambda pid, start: False)
        self.assertEqual([(r["throttle_counts"], r["throttle_blind"],
                           r["throttle_known"]) for r in rows],
                         [(None, None, False)] * 2)
        self.assertEqual(["changed generation" in r["throttle_why"]
                          for r in rows], [True, True])
