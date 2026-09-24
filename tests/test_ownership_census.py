#!/usr/bin/env python3
"""The census is the instrument a WRITE will be based on, so it is tested
like one: every count it prints has an arm, and every absence it asserts has
an unconditional positive control on the same observable.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from tests._tmphome import pin_suite_guard        # noqa: F401
from helm import ownership, ownership_census, turnresponse


def _rows(**by_id):
    """{id: row} — each value is (status, owner) for whichever key applies."""
    return {k: {"status": v[0], "who": v[1]} for k, v in by_id.items()}


def _fixed(mapping, default=(ownership.HOLDING, "fixture")):
    """A state_of that answers from a table, counting its own calls."""
    calls = []

    def state_of(seat):
        calls.append(seat)
        return mapping.get(seat, default)
    return state_of, calls


class LedgerCensusTest(unittest.TestCase):

    def test_rows_are_counted_by_verdict_and_attributed_to_their_seat(self):
        state_of, _calls = _fixed({"dark-one": (ownership.DARK, "silent"),
                                   "live-one": (ownership.HOLDING, "working")})
        rows = _rows(a=("open", "dark-one"), b=("open", "dark-one"),
                     c=("open", "live-one"))
        c = ownership_census.ledger_census(
            "t", rows, "who", ("open",), (), state_of)
        self.assertEqual(2, c.by_verdict[ownership.DARK])
        self.assertEqual(1, c.by_verdict[ownership.HOLDING])
        self.assertEqual(2, c.seats["dark-one"][2], "row count per seat")
        self.assertEqual(ownership.DARK, c.seats["dark-one"][0])
        self.assertEqual(3, c.owed)

    def test_a_seat_is_judged_ONCE_however_many_rows_it_holds(self):
        """Not a saving — a CONSISTENCY requirement. The probe behind
        `state_of` reads a live process table, so asking per ROW lets one
        seat's answer change midway through its own rows and produces a
        census where some of a seat's rows hold and the rest are dark, from
        one reading of one fleet."""
        state_of, calls = _fixed({})
        rows = _rows(**{"r%d" % i: ("open", "seat-a") for i in range(5)})
        rows["other"] = {"status": "open", "who": "seat-b"}
        c = ownership_census.ledger_census(
            "t", rows, "who", ("open",), (), state_of)
        # POSITIVE CONTROL FIRST, on the same observable: the recorder DID
        # fill, so the small number below is one-call-per-seat and not a
        # state_of that was never wired to the loop at all.
        self.assertEqual(6, c.owed, "the loop did not classify the rows")
        self.assertEqual(sorted(calls), ["seat-a", "seat-b"])

    def test_an_EXCLUDED_state_is_counted_and_named_at_its_true_size(self):
        """A census that silently drops what it does not judge reports a
        smaller world than the one it is about, and the reader cannot tell an
        empty category from one nobody looked at."""
        state_of, _calls = _fixed({})
        rows = _rows(a=("open", "seat-a"), b=("held", "seat-a"),
                     c=("held", "seat-b"))
        c = ownership_census.ledger_census(
            "t", rows, "who", ("open",), ("held",), state_of)
        self.assertEqual(2, c.set_aside["held"])
        # CONTROL ON THE SAME OBSERVABLE: the open row WAS judged, so the
        # exclusion above is the aside-state and not a loop that judged
        # nothing.
        self.assertEqual(1, c.owed)
        self.assertNotIn("seat-b", c.seats, "a set-aside row was judged")

    def test_an_aside_state_with_no_rows_still_reports_its_zero(self):
        """The whole point of naming an exclusion is that the reader learns
        it is empty rather than never looked at."""
        state_of, _calls = _fixed({})
        c = ownership_census.ledger_census(
            "t", _rows(a=("open", "seat-a")), "who", ("open",), ("held",),
            state_of)
        # POSITIVE CONTROL on the same observable: the SAME call with a held
        # row really does count it, so the zero below is an empty category
        # being reported and not a counter that never increments.
        loaded = ownership_census.ledger_census(
            "t", _rows(a=("open", "seat-a"), b=("held", "seat-a")), "who",
            ("open",), ("held",), _fixed({})[0])
        self.assertEqual(1, loaded.set_aside["held"])
        self.assertIn("held", c.set_aside)
        self.assertEqual(0, c.set_aside["held"])

    def test_a_row_naming_NOBODY_is_counted_not_dropped(self):
        state_of, calls = _fixed({})
        rows = _rows(a=("open", ""), b=("open", None), c=("open", "seat-a"))
        c = ownership_census.ledger_census(
            "t", rows, "who", ("open",), (), state_of)
        self.assertEqual(2, c.unowned)
        self.assertEqual(1, c.owed)
        self.assertEqual(["seat-a"], calls, "an unowned row was judged")

    def test_the_arithmetic_covers_every_row_the_ledger_holds(self):
        """`account()` exists so a caller can ASSERT the columns add up
        rather than trust the renderer, because the number this census is
        read for is the one a write will be based on."""
        state_of, _calls = _fixed({})
        rows = _rows(a=("open", "seat-a"), b=("held", "seat-a"),
                     c=("open", ""), d=("closed", "seat-b"))
        c = ownership_census.ledger_census(
            "t", rows, "who", ("open",), ("held",), state_of)
        shown, total = c.account()
        self.assertEqual(shown, total)
        self.assertEqual(3, total, "a closed row is neither owed nor aside")


class GapSeatsTest(unittest.TestCase):

    def test_a_seat_IN_the_gap_is_reported_because_it_retires_the_constant(self):
        """`DEAD_AFTER_S` is justified ONLY by a measured bimodal
        distribution. A seat sitting near the line means the distribution
        changed and the number needs re-deriving — the census is how that
        announces itself instead of being discovered by a wrongly reverted
        row."""
        floor = ownership.DEAD_AFTER_S
        found = ownership_census.gap_seats({"edge": floor * 1.1,
                                            "live": 30.0,
                                            "dark": floor * 9})
        self.assertEqual([("edge", floor * 1.1)], found)

    def test_a_BIMODAL_fleet_reports_an_empty_gap(self):
        """The control for the arm above: this is the shape that justified
        the constant, so it must read clear."""
        floor = ownership.DEAD_AFTER_S
        quiet = {"live": 0.0, "also-live": 60.0, "dark": floor * 6}
        # POSITIVE CONTROL, unconditional and first, through the SAME call
        # with the SAME band: add one seat sitting on the line and it is
        # selected. So the empty result below is a statement about this
        # distribution, not a predicate that never selects anything.
        self.assertEqual([("edge", float(floor))],
                         ownership_census.gap_seats(
                             dict(quiet, edge=float(floor))))
        self.assertEqual([], ownership_census.gap_seats(quiet))

    def test_a_seat_that_never_recorded_a_turn_is_not_in_the_gap(self):
        """None is "no turn ever recorded", which `owner_state` already calls
        DARK outright — it is not a quiet time and cannot be compared to a
        threshold."""
        floor = float(ownership.DEAD_AFTER_S)
        # POSITIVE CONTROL, unconditional and first, through the same call: a
        # seat WITH a quiet time on the line is selected out of the very same
        # mapping, so the empty result below is about the None.
        self.assertEqual([("edge", floor)],
                         ownership_census.gap_seats({"never": None,
                                                     "edge": floor}))
        self.assertEqual([], ownership_census.gap_seats({"never": None}))


class RoomsBySeatTest(unittest.TestCase):

    def _claims(self, payload):
        fd, path = tempfile.mkstemp(prefix="helm-test-claims-", suffix=".json")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        with open(path, "w") as fh:
            fh.write(payload)
        return path

    def test_rooms_are_grouped_by_holder_and_meta_keys_are_skipped(self):
        path = self._claims(json.dumps({
            "_fence": 418,
            "worktree:helm:lane-one": {"holder": "Seat-A"},
            "worktree:helm:lane-two": {"holder": "seat-a"},
            "worktree:helm:lane-three": {"holder": "seat-b"},
            "worktree:helm:unheld": {"holder": ""},
        }))
        rooms, err = ownership_census._rooms_by_seat("/helm", claims=path)
        self.assertIsNone(err)
        # THE READER RETURNS TWO MAPS: rooms in THIS project, and the seats
        # holding rooms in a project this root cannot resolve. Discarding the
        # second is how a same-named lane in another repository resolved to a
        # local room and read gate-EMPTY.
        by_seat, foreign = rooms
        self.assertEqual(["/helm-wt/lane-one", "/helm-wt/lane-two"],
                         sorted(by_seat["seat-a"]), "holder is casefolded")
        self.assertEqual(["/helm-wt/lane-three"], by_seat["seat-b"])
        self.assertEqual(2, len(by_seat),
                         "a meta key or empty holder became a seat")
        self.assertEqual({}, foreign, "no fixture claim names a foreign project")

    def test_an_UNREADABLE_claims_file_is_an_ERROR_not_an_empty_map(self):
        """Empty would mean "this seat holds no room", which is exactly the
        sentence that makes a working seat look idle and its rows
        revertible."""
        bad = self._claims("{not json")
        # POSITIVE CONTROL on the same reader, unconditional and first.
        good = self._claims(json.dumps(
            {"worktree:helm:lane-one": {"holder": "seat-a"}}))
        rooms, err = ownership_census._rooms_by_seat("/helm", claims=good)
        self.assertIsNone(err)
        self.assertIn("seat-a", rooms[0])
        rooms, err = ownership_census._rooms_by_seat("/helm", claims=bad)
        self.assertIsNone(rooms)
        self.assertTrue(err, "an unreadable claims file returned no reason")


class _Census(object):
    def __init__(self, state):
        self.state = state


class DescendantProbeTest(unittest.TestCase):

    def setUp(self):
        from helm import gate
        self.gate = gate

    def _probe(self, rooms, states, rooms_err=None):
        return ownership_census._descendant_probe(
            rooms, lambda p: _Census(states[p]), rooms_err)

    def _control(self):
        """UNCONDITIONAL POSITIVE CONTROL, and every arm below opens with it.

        The three answers this probe gives are True, False and None, so an
        arm asserting one of them is asserting a VALUE that an inert probe —
        one returning a constant, or never reaching its rooms at all — would
        satisfy just as happily. This drives the same factory to the two
        answers the arm is NOT about, so the arm's own assertion is about the
        input it varies rather than about a probe that cannot discriminate.
        """
        live = self._probe({"seat-a": ["/live"]},
                           {"/live": self.gate.GATE_LIVE})
        empty = self._probe({"seat-a": ["/empty"]},
                            {"/empty": self.gate.GATE_EMPTY})
        self.assertIs(True, live("seat-a"), "the probe cannot report YES")
        # AND NOT-GATING IS **UNKNOWN**, NOT FREE. False needs BOTH halves of
        # the conjunction measured, and the subagent half has no foreign-seat
        # reader — so a read gate that is merely EMPTY proves nothing about
        # whether the seat is working.
        self.assertIsNone(empty("seat-a"),
                          "an unmeasured subagent population reported as NO")

    def test_a_LIVE_gate_in_a_room_the_seat_holds_is_descendant_work(self):  # noqa: VACUOUS_ASSERTION — the tri-state VALUE is the contract; _control drives the same factory to both other answers, which the rung cannot credit because each call mints a fresh producer
        """The case the conjunction exists for: a whole-suite gate on the slow
        node runs ~34 minutes, during which the seat completes no turn and is
        emphatically working."""
        self._control()
        probe = self._probe({"seat-a": ["/r1"]},
                            {"/r1": self.gate.GATE_LIVE})
        self.assertIs(True, probe("seat-a"))

    def test_gates_read_EMPTY_is_still_UNKNOWN_while_subagents_are_unmeasured(self):
        """The conjunction has two halves and only one has a reader. Returning
        False here would free the rows of a seat whose subagent is mid-build,
        which is the failure the second half exists to prevent."""
        self._control()
        probe = self._probe({"seat-a": ["/r1", "/r2"]},
                            {"/r1": self.gate.GATE_EMPTY,
                             "/r2": self.gate.GATE_EMPTY})
        self.assertIsNone(probe("seat-a"))

    def test_an_UNREADABLE_room_makes_the_answer_UNKNOWN_not_free(self):  # noqa: VACUOUS_ASSERTION — product law: an unmeasured room is UNKNOWN and UNKNOWN never reverts; the neighbouring all-readable call is asserted FALSE one line above
        self._control()
        # AND THE NEAREST NEIGHBOUR, same rooms, same probe: with BOTH rooms
        # readable the answer is False, so the None below is the unreadable
        # room and nothing else about this call.
        self.assertIsNone(self._probe(
            {"seat-a": ["/r1", "/r2"]},
            {"/r1": self.gate.GATE_EMPTY, "/r2": self.gate.GATE_EMPTY})("seat-a"))
        self.assertIsNone(self._probe(
            {"seat-a": ["/r1", "/r2"]},
            {"/r1": self.gate.GATE_EMPTY,
             "/r2": self.gate.GATE_UNREADABLE})("seat-a"))

    def test_a_LIVE_room_wins_over_an_unreadable_sibling(self):  # noqa: VACUOUS_ASSERTION — asserts TRUE, not an absence; flagged only because the probe is a fresh producer per call
        """A proven YES needs no further reading: the seat is working."""
        self._control()
        probe = self._probe({"seat-a": ["/r1", "/r2"]},
                            {"/r1": self.gate.GATE_LIVE,
                             "/r2": self.gate.GATE_UNREADABLE})
        self.assertIs(True, probe("seat-a"))

    def test_an_unreadable_CLAIMS_file_makes_every_seat_UNKNOWN(self):  # noqa: VACUOUS_ASSERTION — product law: not knowing which rooms a seat holds is UNKNOWN, never free; the same rooms and states without the error are asserted TRUE one line above
        """Not knowing which rooms belong to whom is not the same as knowing
        a seat holds none — and the rooms map here is DELIBERATELY populated
        with a LIVE room, so the None is the error and not an empty map."""
        self._control()
        rooms, states = {"seat-a": ["/r1"]}, {"/r1": self.gate.GATE_LIVE}
        # the SAME rooms and states, without the error, answer True
        self.assertIs(True, self._probe(rooms, states)("seat-a"))
        self.assertIsNone(self._probe(rooms, states, rooms_err="boom")("seat-a"))

    def test_a_seat_holding_no_room_is_still_UNKNOWN(self):
        """Holding no LANE is not the same as running no SUBAGENT, and only
        the lane half is readable. The same map answers True for the seat that
        does hold the gating room, so the UNKNOWN is this seat's unmeasured
        half rather than a probe that never answers."""
        self._control()
        rooms, states = {"other": ["/r1"]}, {"/r1": self.gate.GATE_LIVE}
        self.assertIs(True, self._probe(rooms, states)("other"))
        self.assertIsNone(self._probe(rooms, states)("seat-a"))


class LiveInputsTest(unittest.TestCase):
    """Real roster/presence/claims readers; only root discovery and gate I/O fake."""
    NOW = 1_700_000_000.0
    SEAT = "Seat-A"
    SESSION = "session-a"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="helm-test-census-adapters-")
        self.addCleanup(tmp.cleanup)
        self.directory = tmp.name
        self.root = os.path.join(tmp.name, "other-project")
        env = mock.patch.dict(os.environ, {
            "HELM_CHAT_DIR": tmp.name,
            "HELM_TURNSTAMP_DIR": os.path.join(tmp.name, "turnstamps"),
            # PINNED, because `_tmphome` pins HELM_HOME and NOT HOME: without
            # this the transcript reader resolves `~/.claude` and these arms
            # read the operator's real sessions.
            "HELM_CLAUDE_DIR": os.path.join(tmp.name, "claude"),
        })
        env.start()
        self.addCleanup(env.stop)
        roots = mock.patch("helm.work._lanes.find_root", return_value=self.root)
        self.find_root = roots.start()
        self.addCleanup(roots.stop)

    def _roster(self, last_seen, cwd=None):
        """A real roster row carries a `cwd`, and the census needs it: the
        PENDING window is derived from the SESSION's effective settings scope,
        which is that seat's own project `.claude` directory. A row without
        one has an unresolvable scope and yields UNKNOWN rather than a
        borrowed number, so omitting it here would test the refusal instead of
        the path."""
        from helm.seats_common import roster_path
        row = {"session": self.SESSION, "last_seen": last_seen,
               "cwd": self.root if cwd is None else cwd}
        with open(roster_path(), "w", encoding="utf-8") as fh:
            json.dump({self.SEAT: row}, fh)

    def _claim(self, project):
        from helm.seats_common import claims_path
        row = {"holder": self.SEAT, "session": self.SESSION,
               "lease": "lease-a", "fence": 1, "exp_mono": self.NOW,
               "exp_wall": self.NOW, "ts": "2026-09-11T00:00:00Z"}
        with open(claims_path(), "w", encoding="utf-8") as fh:
            json.dump({"_fence": 1, "worktree:%s:lane-one" % project: row}, fh)

    def test_missing_seen_file_validates_the_real_roster_fallback(self):  # noqa: VACUOUS_ASSERTION — pre-existing arm; its positive control is the quiet-dict equality asserted unconditionally before each absence
        from helm import seats_roster
        self._roster(self.NOW - 60)
        self.assertFalse(os.path.exists(seats_roster.seen_path(self.SEAT)))
        _state, quiet, known = ownership_census.live_inputs(now=self.NOW)
        self.assertTrue(known)
        self.assertEqual({self.SEAT: 60.0}, quiet)
        for bad in ("bad", float("nan"), float("inf"), 10 ** 400):
            with self.subTest(last_seen=bad):
                self._roster(bad)
                _state, quiet, known = ownership_census.live_inputs(now=self.NOW)
                self.assertFalse(known)
                self.assertIsNone(quiet[self.SEAT])
                out = ownership_census.render(
                    [], gaps=ownership_census.gap_seats(quiet),
                    distribution_known=known,
                    distribution_proves_terminal_response=True)
                self.assertIn("TRIPWIRE: UNKNOWN", out)
                self.assertNotIn("tripwire: clear", out)
        # Missing history keeps the pre-existing no-presence representation.
        self._roster(None)
        _state, quiet, known = ownership_census.live_inputs(now=self.NOW)
        self.assertTrue(known)
        self.assertEqual({self.SEAT: None}, quiet)

    def test_seen_file_overrides_bad_fallback_and_zero_now_is_not_wall_time(self):
        from helm import seats_roster
        self._roster("bad")
        _state, _quiet, known = ownership_census.live_inputs(now=self.NOW)
        self.assertFalse(known)
        path = seats_roster.seen_path(self.SEAT)
        with open(path, "w", encoding="utf-8"):
            pass
        os.utime(path, (self.NOW - 60, self.NOW - 60))
        _state, quiet, known = ownership_census.live_inputs(now=self.NOW)
        self.assertTrue(known)
        self.assertEqual(60.0, quiet[self.SEAT])
        _state, quiet, known = ownership_census.live_inputs(now=0)
        self.assertTrue(known)
        self.assertEqual(-(self.NOW - 60), quiet[self.SEAT])

    def test_claim_project_is_bound_to_the_actual_non_helm_root(self):  # noqa: VACUOUS_ASSERTION — pre-existing arm; the HOLDING assertion and census.assert_called_once_with are the unconditional positive controls preceding each negative
        from helm import gate, turnstamp
        self._roster(self.NOW - 60)
        # A fixture stamp gets the consumer past its unrelated coverage gate.
        path, err = turnstamp.record(
            self.SEAT, self.SESSION, now=self.NOW - 3 * ownership.DEAD_AFTER_S)
        self.assertIsNone(err)
        self.assertTrue(path)
        self._claim("other-project")
        with mock.patch("helm.gate.inflight_census",
                        return_value=_Census(gate.GATE_LIVE)) as census:
            state, _quiet, _known = ownership_census.live_inputs(now=self.NOW)
            self.assertEqual(ownership.HOLDING, state(self.SEAT)[0])
            census.assert_called_once_with(self.root + "-wt/lane-one")
            # A same-label Helm claim must never borrow that local live gate.
            census.reset_mock()
            self._claim("helm")
            state, _quiet, _known = ownership_census.live_inputs(now=self.NOW)
            self.assertEqual(ownership.UNKNOWN, state(self.SEAT)[0])
            census.assert_not_called()
            # No root is not a relative 'None-wt' repository.
            self._claim("other-project")
            self.find_root.return_value = None
            state, _quiet, _known = ownership_census.live_inputs(now=self.NOW)
            self.assertEqual(ownership.UNKNOWN, state(self.SEAT)[0])
            census.assert_not_called()

    def _transcript(self, lines, partial=None, extra=None):
        """A real transcript at the path the ADAPTER will resolve: bound by
        the session id in the filename under the pinned Claude dir."""
        import datetime
        proj = os.path.join(self.directory, "claude", "projects", "-p")
        os.makedirs(proj, exist_ok=True)
        path = os.path.join(proj, "%s.jsonl" % self.SESSION)

        def iso(e):
            return datetime.datetime.fromtimestamp(
                e, datetime.timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        with open(path, "w", encoding="utf-8") as fh:
            for kind, at in lines:
                if kind == "a":
                    fh.write(json.dumps({
                        "type": "assistant", "timestamp": iso(at),
                        "message": {"role": "assistant",
                                    "stop_reason": "end_turn",
                                    "content": [{"type": "text",
                                                 "text": "x"}]}}) + "\n")
                else:
                    fh.write(json.dumps({
                        "type": "user", "timestamp": iso(at),
                        "message": {"role": "user", "content": "go"}}) + "\n")
            if extra is not None:
                fh.write(extra + "\n")
            if partial is not None:
                fh.write(partial)
        return path

    def test_the_ADAPTER_reads_a_real_transcript_and_holds_a_fresh_seat(self):
        """THE POSITIVE CONTROL FOR EVERY ADAPTER ARM BELOW: driven through
        the real `live_inputs`, not the module, so the session binding, the
        settings scope and the reader are all exercised as the verb uses
        them."""
        self._roster(self.NOW - 60)
        self._transcript([("u", self.NOW - 300), ("a", self.NOW - 200),
                          ("u", self.NOW - 100)])
        state, _q, _k = ownership_census.live_inputs(now=self.NOW)
        verdict, why = state(self.SEAT)
        self.assertEqual(ownership.HOLDING, verdict, why)
        self.assertIn("terminal response", why)

    #: A CUTOFF SIZE THE FIXTURES MUST EXCEED. A transcript smaller than this
    #: is read in full by any tail-bounded reader as well as by a whole-file
    #: one, so it cannot discriminate between them and an arm built on it
    #: passes whatever the reader does.
    CUTOFF_PROBE_BYTES = 8 * 1024 * 1024

    def _evidence_for(self, seat):
        """The TurnEvidence the adapter actually built, taken from the real
        `owner_state` call rather than reconstructed."""
        seen = {}
        real = ownership.owner_state

        def spy(s_, rows, turn_evidence, descendants, now=None):
            seen["ev"] = turn_evidence(s_, (rows or {}).get(s_) or {})
            return real(s_, rows, turn_evidence, descendants, now=now)
        with mock.patch.object(ownership, "owner_state", spy):
            state, _q, _k = ownership_census.live_inputs(now=self.NOW)
            verdict, why = state(seat)
        return seen["ev"], verdict, why

    def _fat_transcript(self, recent_at, stale_at, pad_bytes):
        """A transcript whose RECENT terminal response sits further from EOF
        than the former tail, with a STALE one near the end. A tail-only
        reader answers `stale_at`; a whole-file reader answers `recent_at`."""
        import datetime
        proj = os.path.join(self.directory, "claude", "projects", "-p")
        os.makedirs(proj, exist_ok=True)
        path = os.path.join(proj, "%s.jsonl" % self.SESSION)

        def iso(e):
            return datetime.datetime.fromtimestamp(
                e, datetime.timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        def rec(kind, at, pad=None):
            if kind == "a":
                row = {"type": "assistant", "timestamp": iso(at),
                       "message": {"role": "assistant",
                                   "stop_reason": "end_turn",
                                   "content": [{"type": "text", "text": "x"}]}}
            else:
                row = {"type": "user", "timestamp": iso(at),
                       "message": {"role": "user", "content": "go"}}
            if pad:
                row["pad"] = "p" * pad
            return json.dumps(row) + "\n"

        with open(path, "w", encoding="utf-8") as fh:
            fh.write(rec("a", recent_at))
            written, i = 0, 0
            while written < pad_bytes:
                line = rec("u", stale_at - 10000 + i, pad=2000)
                fh.write(line)
                written += len(line)
                i += 1
            fh.write(rec("a", stale_at))
            fh.write(rec("u", stale_at + 1))
        self.assertGreater(os.path.getsize(path), self.CUTOFF_PROBE_BYTES,
                           "the fixture must exceed the cutoff probe size "
                           "or it cannot discriminate a tail-bounded reader "
                           "from a whole-file one")
        return path

    def test_the_ADAPTER_finds_a_RECENT_response_BEYOND_A_TAIL_CUTOFF(self):
        """THE ARM A TAIL-BOUNDED READER CANNOT PASS, through live_inputs.

        The recent terminal response sits more than 8 MiB from EOF with a
        STALE one near the end. A reader bounded to a tail reports the stale
        timestamp and calls a live seat quiet; a whole-file reader reports the
        recent one. The fixture asserts its own size, so it cannot silently
        stop discriminating between the two.
        """
        self._roster(self.NOW - 60)
        recent = self.NOW - 120
        stale = self.NOW - 99 * 3600
        self._fat_transcript(recent, stale, self.CUTOFF_PROBE_BYTES + 262144)
        ev, verdict, why = self._evidence_for(self.SEAT)
        self.assertIsNone(ev.err)
        self.assertIsNone(ev.incomplete)
        self.assertAlmostEqual(recent, ev.ts, places=2,
                               msg="the adapter reported the positionally "
                                   "later but OLDER record")
        self.assertEqual(ownership.HOLDING, verdict, why)

    def test_the_ADAPTER_reads_a_WHOLLY_STALE_large_transcript_as_stale(self):
        """The pair: the same shape with NO recent record reports the stale
        timestamp, so the arm above is the recent record being found and not
        a reader that always answers fresh."""
        self._roster(self.NOW - 60)
        stale = self.NOW - 99 * 3600
        self._fat_transcript(stale - 20, stale, self.CUTOFF_PROBE_BYTES + 262144)
        ev, _verdict, _why = self._evidence_for(self.SEAT)
        self.assertIsNone(ev.incomplete)
        self.assertAlmostEqual(stale, ev.ts, places=2)

    def test_the_ADAPTER_marks_INCOMPLETE_when_the_budget_cuts_the_file(self):
        """The same file under a reduced budget: the unread remainder could
        hold a newer response, so the evidence is incomplete — and an
        incomplete reading may still HOLD, which is what keeps the verb
        usable."""
        self._roster(self.NOW - 60)
        recent = self.NOW - 120
        stale = self.NOW - 99 * 3600
        self._fat_transcript(recent, stale, self.CUTOFF_PROBE_BYTES + 262144)
        # POSITIVE CONTROL, unconditional and first: at the real budget this
        # same file reads COMPLETE.
        ev, _v, _w = self._evidence_for(self.SEAT)
        self.assertIsNone(ev.incomplete)
        saved = turnresponse.MAX_SCAN_BYTES
        turnresponse.MAX_SCAN_BYTES = 65536
        try:
            ev, verdict, why = self._evidence_for(self.SEAT)
        finally:
            turnresponse.MAX_SCAN_BYTES = saved
        self.assertIsNotNone(ev.incomplete)
        self.assertIn("budget", ev.incomplete)
        # The visible tail still carries the STALE record, and a truncated
        # reading of it cannot revert — UNKNOWN, never DARK.
        self.assertNotEqual(ownership.DARK, verdict)

    def test_the_ADAPTER_holds_a_FRESH_seat_whose_read_was_truncated(self):
        """Incomplete coverage never argues against a LIVE answer: a hole or a
        cut can only hide MORE recent activity."""
        self._roster(self.NOW - 60)
        recent = self.NOW - 120
        self._fat_transcript(recent, recent + 1,
                             self.CUTOFF_PROBE_BYTES + 262144)
        saved = turnresponse.MAX_SCAN_BYTES
        turnresponse.MAX_SCAN_BYTES = 65536
        try:
            ev, verdict, why = self._evidence_for(self.SEAT)
        finally:
            turnresponse.MAX_SCAN_BYTES = saved
        self.assertIsNotNone(ev.incomplete)
        self.assertEqual(ownership.HOLDING, verdict, why)

    def test_the_ADAPTER_reports_PENDING_for_a_young_unfollowed_record(self):
        """A terminal record with nothing after it, inside the Stop-hook
        window: a veto may still be in flight, so it is neither HOLDING on
        that evidence nor revertible."""
        self._roster(self.NOW - 60)
        self._transcript([("u", self.NOW - 20), ("a", self.NOW - 5)])
        ev, verdict, why = self._evidence_for(self.SEAT)
        self.assertIsNotNone(ev.pending)
        self.assertIsNone(ev.ts)
        self.assertEqual(ownership.UNKNOWN, verdict)

    def test_the_ADAPTER_cannot_reach_DARK_through_the_descendant_half(self):
        """THE HONEST BOUND, pinned rather than assumed, because it decides
        what the adapter arms CAN show.

        `_descendant_probe` returns False only when BOTH halves are measured,
        and the subagent half has no foreign-seat reader — so it never returns
        False today and DARK is structurally unreachable for a ROSTERED seat
        however silent its transcript is. The dark rows on the live board all
        come from the identity path (no roster row), never this conjunction.

        This is why the coverage gate below is exercised at the PREDICATE and
        not here: it guards the revert, and the revert is behind a door that
        does not open yet. Saying so is the point of the arm — a future reader
        who makes the probe answerable must find this and re-derive it rather
        than discover it by reverting somebody's rows.
        """
        from helm import gate
        self._roster(self.NOW - 60)
        old = self.NOW - 99 * 3600
        self._transcript([("u", old - 10), ("a", old), ("u", old + 10)])
        self._claim("other-project")
        with mock.patch("helm.gate.inflight_census",
                        return_value=_Census(gate.GATE_EMPTY)):
            state, _q, _k = ownership_census.live_inputs(now=self.NOW)
            verdict, why = state(self.SEAT)
        self.assertEqual(ownership.UNKNOWN, verdict)
        self.assertIn("descendant", why,
                      "DARK became reachable through this half; the coverage "
                      "gate now guards a live revert and needs an adapter arm")

    def test_the_ADAPTER_carries_a_transcript_HOLE_into_the_evidence(self):
        """codex's requested adapter control, at the seam it can actually be
        observed. The predicate arms prove what an incomplete reading DOES;
        this proves the adapter produces one from a real corrupt transcript
        rather than swallowing it — the two together are the claim."""
        self._roster(self.NOW - 60)
        old = self.NOW - 99 * 3600
        seen = {}
        real = ownership.owner_state

        def spy(seat, rows, turn_evidence, descendants, now=None):
            seen["ev"] = turn_evidence(seat, (rows or {}).get(seat) or {})
            return real(seat, rows, turn_evidence, descendants, now=now)

        # UNCONDITIONAL POSITIVE CONTROL, first: a clean transcript through
        # the same spy carries NO incomplete marker, so the marker below is
        # the corrupt record and not something the adapter always sets.
        self._transcript([("u", old - 10), ("a", old), ("u", old + 10)])
        with mock.patch.object(ownership, "owner_state", spy):
            state, _q, _k = ownership_census.live_inputs(now=self.NOW)
            state(self.SEAT)
        self.assertIsNotNone(seen["ev"].ts)
        self.assertIsNone(seen["ev"].incomplete)

        self._transcript([("u", old - 10), ("a", old), ("u", old + 10)],
                         extra="{not json")
        with mock.patch.object(ownership, "owner_state", spy):
            state, _q, _k = ownership_census.live_inputs(now=self.NOW)
            state(self.SEAT)
        self.assertIsNotNone(seen["ev"].ts, "the hole failed the reading")
        self.assertIn("MORE RECENT", seen["ev"].incomplete or "")

    def test_the_ADAPTER_still_HOLDS_a_fresh_seat_whose_transcript_has_a_hole(self):
        """The asymmetry at the adapter: a hole can only hide MORE recent
        activity, so it never argues against a live answer. This is what keeps
        the verb usable — a transcript being written right now ends mid-record
        as its normal state."""
        self._roster(self.NOW - 60)
        self._transcript([("u", self.NOW - 300), ("a", self.NOW - 200)],
                         partial='{"type": "assi')
        state, _q, _k = ownership_census.live_inputs(now=self.NOW)
        self.assertEqual(ownership.HOLDING, state(self.SEAT)[0])

    def _project_stop_timeout(self, seconds):
        """A REAL project-scope settings file configuring one Stop hook.

        The window is not a constant this module owns — it is whatever the
        seat's own launch settings configure, read from that seat's project
        `.claude`. Writing it here means the arm below changes the SAME input
        the harness changes, rather than reaching past the reader.
        """
        claude = os.path.join(self.root, ".claude")
        os.makedirs(claude, exist_ok=True)
        with open(os.path.join(claude, "settings.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"hooks": {"Stop": [
                {"hooks": [{"type": "command", "command": "true",
                            "timeout": seconds}]}]}}, fh)

    def test_live_inputs_uses_bound_project_stop_timeout(self):
        """THE WINDOW IS THE SEAT'S OWN, AND ONLY THE WINDOW MOVES HERE.

        PENDING and SETTLED are this module's POLICY about terminal RESPONSE
        RECENCY, and nothing more. A response younger than the seat's own
        Stop-hook bound is treated as PENDING because the harness persists the
        record BEFORE invoking Stop hooks, so a hook may still be running.
        Past the bound the policy treats it as settled — which is NOT a proof
        that nothing can retract the response, and NOT a claim that the
        harness accepted it. A foreign Stop hook outside helm's dispatcher can
        veto after any bound this module knows about.

        ONE transcript, ONE roster row, ONE clock, ONE cwd — the only thing
        that differs between the two readings is the project Stop timeout, in
        SECONDS. Anything else moving would make this an arm about two
        different worlds.
        """
        self._roster(self.NOW - 60)
        # 100s old, and NOTHING after it: the terminal record is the last
        # thing in the file, so its age is the only question.
        self._transcript([("u", self.NOW - 300), ("a", self.NOW - 100)])

        # SETTLED: 100s exceeds the configured 60s policy window; no
        # harness-acceptance claim.
        self._project_stop_timeout(60)
        ev_settled, verdict_settled, why_settled = self._evidence_for(self.SEAT)
        self.assertFalse(ev_settled.pending, why_settled)
        self.assertEqual(self.NOW - 100, ev_settled.ts,
                         "the settled reading must carry the EXACT terminal "
                         "timestamp, not merely a non-None one")
        self.assertEqual(ownership.HOLDING, verdict_settled, why_settled)

        # PENDING: the identical 100s of age against a 600s bound. A hook with
        # 600 seconds to answer may still veto this very response.
        self._project_stop_timeout(600)
        ev_pending, verdict_pending, why_pending = self._evidence_for(self.SEAT)
        self.assertTrue(ev_pending.pending, why_pending)
        self.assertIsNone(ev_pending.ts,
                          "a PENDING reading must carry no settled timestamp; "
                          "publishing one would certify a response a live "
                          "hook can still retract")
        self.assertEqual(ownership.UNKNOWN, verdict_pending, why_pending)

        # THE READINGS MUST ACTUALLY DIFFER. Both halves above would pass
        # against a reader that ignored the bound if the two verdicts happened
        # to coincide, so the discrimination is asserted rather than implied.
        self.assertNotEqual(verdict_settled, verdict_pending)

    def test_the_ADAPTER_yields_UNKNOWN_when_settings_cannot_be_read(self):
        """A malformed settings file must degrade ONE seat's evidence, never
        abort the census: the reader is called outside the per-seat try, so a
        raise there took down every seat."""
        self._roster(self.NOW - 60)
        self._transcript([("u", self.NOW - 300), ("a", self.NOW - 200),
                          ("u", self.NOW - 100)])
        claude = os.path.join(self.directory, "claude")
        os.makedirs(claude, exist_ok=True)
        with open(os.path.join(claude, "settings.json"), "w",
                  encoding="utf-8") as fh:
            fh.write(json.dumps({"hooks": [1]}))
        state, _q, _k = ownership_census.live_inputs(now=self.NOW)
        verdict, why = state(self.SEAT)
        self.assertEqual(ownership.UNKNOWN, verdict)
        self.assertIn("timeout unreadable", why)

    def test_explicit_project_cannot_override_a_different_root(self):
        self._claim("other-project")
        rooms, err = ownership_census._rooms_by_seat(
            self.root, project="other-project")
        self.assertIsNone(err)
        self.assertEqual([self.root + "-wt/lane-one"],
                         rooms[0][self.SEAT.casefold()])
        rooms, err = ownership_census._rooms_by_seat(self.root, project="helm")
        self.assertIsNone(rooms)
        self.assertIn("matching resolved repository root", err)


class RenderTest(unittest.TestCase):

    def _census(self, **kw):
        state_of, _calls = _fixed({"dark-one": (ownership.DARK, "silent 9h")})
        rows = _rows(a=("open", "dark-one"), b=("open", "live-one"),
                     c=("held", "dark-one"))
        return ownership_census.ledger_census(
            "dispatch", rows, "who", ("open",), ("held",), state_of)

    def test_the_excluded_category_prints_its_own_size(self):
        out = ownership_census.render([self._census()])
        self.assertIn("held", out)
        self.assertIn("SET ASIDE", out)

    def test_a_BEACON_appears_as_evidence_and_never_as_a_verdict(self):
        """The measurement the whole module was built from: one seat read
        COVERED, had completed no turn in 11.2 hours, and held 23 rows. So
        the beacon is reported beside the verdict and takes no part in it —
        `owner_state` has no parameter through which one could keep a row."""
        out = ownership_census.render([self._census()],
                                      beacons={"dark-one": "COVERED"})
        self.assertIn("COVERED", out, "the evidence was not reported")
        self.assertIn("never a condition", out)
        # AND THE STRUCTURAL HALF, asserted rather than trusted to the prose:
        # the decision function cannot be handed a beacon at all.
        import inspect
        params = inspect.signature(ownership.owner_state).parameters
        self.assertNotIn("beacon", params)
        self.assertNotIn("beacons", params)
        # control: the signature really was read and is not empty
        self.assertIn("descendants", params)

    def test_the_tripwire_never_certifies_a_clock_that_is_not_about_turns(self):
        """The gap is computed from whatever clock the caller has, and the only
        one available is `.seen` — beacon-touched presence. Printing "clear"
        off it certifies the TURN-COMPLETION threshold against a population
        that is not about turns, in the one line whose job is to police that.
        """
        out = ownership_census.render([self._census()], gaps=(),
                                      distribution_proves_terminal_response=False)
        self.assertIn("NOT APPLICABLE", out)
        self.assertIn("certifies nothing", out)
        # POSITIVE CONTROL on the same renderer: a clock that DOES prove
        # completion is allowed to say clear, so the refusal above is about
        # the source and not a renderer that can never certify anything.
        good = ownership_census.render([self._census()], gaps=(),
                                       distribution_proves_terminal_response=True)
        self.assertIn("clear", good)

    def test_an_unread_population_certifies_nothing_either(self):
        """A failed roster read yields an empty quiet map, which is the SAME
        representation as a measured empty — so an all-clear off it turns a
        read failure into a verdict about the threshold's validity."""
        out = ownership_census.render([self._census()], gaps=(),
                                      distribution_known=False)
        self.assertIn("TRIPWIRE: UNKNOWN", out)
        # the sentence wraps, so assert a fragment that survives the wrap
        self.assertIn("not be read", out)
        # CONTROL: a KNOWN distribution through the same call reaches a real
        # tripwire sentence instead.
        known = ownership_census.render([self._census()], gaps=(),
                                        distribution_known=True,
                                        distribution_proves_terminal_response=True)
        self.assertIn("clear", known)

    def test_a_populated_gap_is_reported_loudly(self):
        loud = ownership_census.render([self._census()],
                                       gaps=[("edge", 7200.0)],
                                       distribution_proves_terminal_response=True)
        self.assertIn("TRIPWIRE", loud)
        self.assertIn("edge", loud)

    def test_populated_presence_gap_cannot_judge_completion_distribution(self):
        gaps = [("edge", float(ownership.DEAD_AFTER_S))]
        measured = ownership_census.render(
            [self._census()], gaps=gaps, distribution_proves_terminal_response=True)
        self.assertIn("distribution is no longer bimodal", measured)
        self.assertIn("re-deriving", measured)
        self.assertIn("edge", measured)
        presence = ownership_census.render(
            [self._census()], gaps=gaps,
            distribution_source="roster .seen",
            distribution_proves_terminal_response=False)
        self.assertIn("NOT APPLICABLE", presence)
        self.assertIn("roster .seen", presence)
        self.assertIn("CORROBORATION-ONLY", presence)
        self.assertIn("edge", presence)
        self.assertNotIn("no longer bimodal", presence)
        self.assertNotIn("re-deriving", presence)
        self.assertNotIn("No seat sits near", presence)
        unread = ownership_census.render(
            [], gaps=gaps, distribution_known=False,
            distribution_proves_terminal_response=True)
        self.assertIn("TRIPWIRE: UNKNOWN", unread)
        self.assertNotIn("no longer bimodal", unread)

    def test_a_hostile_roster_key_loses_its_control_bytes(self):
        hostile = "evil\x1b[31m\u202ename"
        out = ownership_census._label(hostile)
        # POSITIVE CONTROL, unconditional and first: a LEGITIMATE seat comes
        # through the same door BYTE-IDENTICAL, so the scrub below is the
        # hostile bytes and not a door that mangles or empties everything.
        # Byte-identity is what makes laundering safe to apply everywhere —
        # a name that still resolves after passing through.
        self.assertEqual("seat-a", ownership_census._label("seat-a"))
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\u202e", out)
        self.assertIn("evil", out, "the door discarded the whole name")

    def test_the_tripwire_line_renders_a_roster_key_through_the_door(self):
        """gap_seats carries ROSTER KEYS — unvalidated at the join seam — and
        the tripwire is the most prominent column on this surface."""
        out = ownership_census.render([], gaps=[("ok-seat", 7200.0)])
        self.assertIn("ok-seat", out)
        hostile = ownership_census.render(
            [], gaps=[("bad\x1b[31mseat", 7200.0)])
        self.assertNotIn("\x1b", hostile,
                         "a roster key reached the terminal unlaundered")
        self.assertIn("bad", hostile)

    def test_a_dark_seat_line_launders_too(self):
        """Those names are LEDGER-sourced rather than roster keys, but a
        dispatch recipient is free text as well."""
        state_of, _calls = _fixed({"bad\x1b[31mseat": (ownership.DARK, "why")})
        c = ownership_census.ledger_census(
            "t", _rows(a=("open", "bad\x1b[31mseat")), "who", ("open",), (),
            state_of)
        out = ownership_census.render([c])
        self.assertIn("bad", out, "the line was not rendered at all")
        self.assertNotIn("\x1b", out)


class VerbTailTest(unittest.TestCase):

    def test_an_unknown_tail_REFUSES_before_any_work_runs(self):
        """`ownership census --json` printed the plain census and exited 0 —
        telling a caller who asked for JSON that they got it. Same silence the
        work CLI's unknown-flag scan exists to end."""
        # POSITIVE CONTROL, unconditional and first: the bare subverb is
        # ACCEPTED by the same guard, so the refusals below are the tail.
        from helm.cli import guard_tail
        # BOTH POLES UNCONDITIONAL AND FIRST, so neither depends on the loop
        # below running: the bare subverb is ACCEPTED by this guard, and a
        # junk tail is REFUSED by it. Without the second one outside the
        # loop, an empty iterable would leave only an assertion that the
        # guard says None — which a guard that says None to everything
        # satisfies.
        self.assertIsNone(guard_tail("helm ownership census", []))
        self.assertEqual(2, guard_tail("helm ownership census", ["extra"]))
        for tail in (["extra"], ["--json"], ["--", "x"]):
            self.assertEqual(
                2, guard_tail("helm ownership census", tail),
                "%r was accepted as a tail on a verb with no flags" % (tail,))

    def test_a_missing_or_wrong_subverb_refuses(self):
        self.assertEqual(2, ownership_census.cmd_ownership([]))
        self.assertEqual(2, ownership_census.cmd_ownership(["bogus"]))


if __name__ == "__main__":
    unittest.main()
