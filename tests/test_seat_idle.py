#!/usr/bin/env python3
"""task/3118: ONE per-seat idle reading, from the seat's own hook record.

Two AX bars could not be measured because nothing in helm said whether a seat
was idle: `.seen` is beaten by the inbox beacon on every poll, so a seat asleep
at its prompt reads fresh forever. The reading (helm/seat_idle.py) is built
from three edges the hooks already write per session: the last tool call, the
last turn start, and the last turn end the stop-guard door answered with rc 0.

EVERY FIXTURE HOOK STREAM IS WRITTEN BY THE REAL PRODUCERS: the recorder's
PostToolUse entry (`record.record`), its UserPromptSubmit entry
(`record.turn_open`), and the stop-guard door itself (`helm chat stop-guard
--hook-json`, through the owed-row world of tests/test_owed_row_resurfaces.py).
A reading checked against a hand-written record would verify the reader
against my idea of the writer.

The arms, one per state the brief names:
  * a busy seat (a call or a turn start after its turn end) reads BUSY;
  * an idle seat owing a row is IDLE-OWING after ten minutes, on the
    idle-dispatch sweep and on `helm dispatch list`, and is re-rung ONCE;
  * an idle seat owing nothing is IDLE and flagged nowhere;
  * a seat whose hook record cannot be read is UNKNOWN, never IDLE;
  * a RESTING seat (lane 3280) is never IDLE-OWING, or a named skip.
"""
import contextlib
import io
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_owed_row_resurfaces import _OwedWorld, SEAT, SID  # noqa: E402

from helm import dispatches, idle_dispatch, record, seats  # noqa: E402
from helm import seat, seat_lifecycle  # noqa: E402,F401 — the facade beside the impl
from helm.seats_roster import seen_path  # noqa: E402

MIN = 60


def _si():
    """The module under test, imported per arm so the pre-change tree reds
    arm by arm rather than as one collection error."""
    from helm import seat_idle
    return seat_idle


def _has_rest():
    try:
        from helm import seat_rest  # noqa: F401
    except ImportError:
        return False
    return True


class _IdleWorld(_OwedWorld):
    """The owed-row world plus the hook stream's other two producers, and an
    idle-dispatch sweep whose only fakes are the ones that would touch the
    host: the beacon census (/proc), the pane probe, and the DM transport."""

    def setUp(self):
        super().setUp()
        self.dms = []
        patches = (
            mock.patch.object(idle_dispatch.seats, "beacon_procs",
                              side_effect=lambda s, strict=False, **k:
                              ([4242], "") if s == SEAT else ([], "")),
            mock.patch.object(idle_dispatch.seats, "dm",
                              side_effect=lambda to, text, **k: (
                                  self.dms.append((to, text)),
                                  ({"id": "row"}, None))[1]),
            mock.patch.object(idle_dispatch, "_liveness", return_value={}),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def call(self, at=None):
        """One PostToolUse through the recorder, as the hook delivers it."""
        event = {"session_id": SID, "tool_name": "Read",
                 "hook_event_name": "PostToolUse",
                 "tool_input": {"file_path": "/x"}, "tool_response": "ok"}
        with (mock.patch("time.time", return_value=at) if at
              else contextlib.nullcontext()):
            record.record(event)

    def counters(self):
        path = os.path.join(record.session_dir(SID), "counters.json")
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def end_turn(self):
        """A turn end the way the harness drives it: a Stop, and when helm
        refuses it the re-stop inside the same turn. Returns the recorded
        turn end."""
        rc, _o, err = self.stop()
        if rc == 2:
            rc, _o, err = self.stop(continuing=True)
        self.assertEqual(rc, 0, err)
        ended = self.counters().get("turn-ended-at")
        self.assertIsInstance(ended, float,
                              "the stop door recorded no turn end")
        return ended

    def fresh(self, at):
        """Presence as a polling beacon leaves it: beaten at `at`."""
        path = seen_path(SEAT)
        if not os.path.exists(path):
            open(path, "w").close()
        os.utime(path, (at, at))

    def sweep(self, at):
        self.fresh(at)
        with mock.patch("time.time", return_value=at):
            return idle_dispatch.check()

    def rings(self):
        return [t for to, t in self.dms
                if to == SEAT and "IDLE-OWING" in t]

    def listing(self, at, *flags):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("time.time", return_value=at), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = dispatches.cmd_dispatch(["list"] + list(flags))
        self.assertEqual(rc, 0, err.getvalue())
        return out.getvalue()


class BusySeatTest(_IdleWorld):

    def test_a_call_after_the_turn_end_reads_busy(self):  # noqa: VACUOUS_ASSERTION — the IDLE reading before the call is the positive control on the same reading
        self.call()
        ended = self.end_turn()
        si = _si()
        # POSITIVE CONTROL on the same seat: the turn end alone reads IDLE,
        # so the BUSY below is the call's doing and not a dead reader.
        self.assertEqual(si.reading(SEAT, now=ended + MIN)["state"], si.IDLE)
        self.call(at=ended + 2 * MIN)
        r = si.reading(SEAT, now=ended + 3 * MIN)
        self.assertEqual(r["state"], si.BUSY, r)
        self.assertEqual(r["since"], ended + 2 * MIN)

    def test_a_turn_start_after_the_turn_end_reads_busy(self):  # noqa: VACUOUS_ASSERTION — the state is asserted equal to BUSY, a value, never an absence
        """A beacon wake starts a turn through UserPromptSubmit before any
        call, and a slow generation can run minutes with no call at all."""
        self.call()
        ended = self.end_turn()
        with mock.patch("time.time", return_value=ended + MIN):
            record.turn_open(SID, "a chat wake")
        r = _si().reading(SEAT, now=ended + 9 * MIN)
        self.assertEqual(r["state"], _si().BUSY, r)
        stale = _si().reading(SEAT, now=ended + 12 * MIN)
        self.assertEqual(stale["state"], _si().UNKNOWN, stale)
        self.assertIn("without a call", stale["why"])

    def test_first_hook_turn_with_no_call_is_bounded_busy(self):
        t = time.time()
        hook = {"session_id": SID, "prompt": "first turn",
                "hook_event_name": "UserPromptSubmit"}
        with mock.patch("time.time", return_value=t):
            record.turn_open(SID, "first turn", hook=hook)
        si = _si()
        r = si.reading(SEAT, now=t + 9 * MIN)
        self.assertEqual(r["state"], si.BUSY, r)
        self.assertIsNone(r["last_call"])
        late = si.reading(SEAT, now=t + 11 * MIN)
        self.assertEqual(late["state"], si.UNKNOWN, late)
        self.assertIn("turn start", late["why"])

    def test_no_turn_end_reads_busy_only_while_the_call_is_recent(self):
        """A session that never recorded a turn end: in flight, or ended
        where helm could not see it. BUSY on a recent call, UNKNOWN after,
        never IDLE."""
        t = time.time()
        self.call(at=t)
        si = _si()
        self.assertEqual(si.reading(SEAT, now=t + MIN)["state"], si.BUSY)
        late = si.reading(SEAT, now=t + si.RECENT_S + MIN)
        self.assertEqual(late["state"], si.UNKNOWN, late)
        self.assertIn("no turn end is recorded", late["why"])

    def test_a_busy_seat_owing_a_row_is_not_flagged(self):  # noqa: VACUOUS_ASSERTION — the row id and `recipient BUSY` are asserted present in the same listing the absent marker is read from
        rid = self.plant("d4d4d4d4d4d4d4d4", "review the canary")
        self.call()
        ended = self.end_turn()
        self.call(at=ended + 10 * MIN)
        at = ended + 11 * MIN
        self.assertEqual(_si().reading(SEAT, now=at)["state"], _si().BUSY)
        self.sweep(at)
        self.assertEqual(self.rings(), [], "a busy seat was rung as idle")
        out = self.listing(at, "--overdue")
        self.assertIn(rid, out)                  # control: the row is listed
        self.assertNotIn("IDLE-OWING", out)
        self.assertIn("recipient BUSY", out)


class IdleOwingTest(_IdleWorld):

    def test_the_stop_door_records_the_turn_end_only_when_it_allows(self):  # noqa: VACUOUS_ASSERTION — the re-stop's recorded float on the same counters file is the positive control
        """The owed-row rung refuses every idle stop once, so an idle seat's
        real turn end is always the RE-STOP. A writer that skipped re-stops
        would never record the turn end of the very seats this measures."""
        self.plant("e5e5e5e5e5e5e5e5", "review the canary")
        self.call()
        rc, _o, err = self.stop()
        self.assertEqual(rc, 2, err)
        self.assertNotIn("turn-ended-at", self.counters(),
                         "a refused stop was recorded as a turn end")
        rc, _o, err = self.stop(continuing=True)
        self.assertEqual(rc, 0, err)
        self.assertIsInstance(self.counters().get("turn-ended-at"), float)

    def test_an_idle_seat_owing_a_row_is_flagged_after_ten_minutes_and_rung_once(self):
        rid = self.plant("f6f6f6f6f6f6f6f6", "review the canary")
        self.call()
        ended = self.end_turn()
        si = _si()
        # NINE MINUTES: idle, owing, and under the bar -- no flag, no ring.
        early = ended + 9 * MIN
        r = si.reading(SEAT, now=early)
        self.assertEqual(r["state"], si.IDLE, r)
        res = self.sweep(early)
        self.assertFalse(any(f.get("idle_owing") for f in res["findings"]))
        self.assertEqual(self.rings(), [])
        # ELEVEN: IDLE-OWING, and the seat -- whose beacon keeps its presence
        # FRESH, the shape every beaconed seat has -- is rung once.
        at = ended + 11 * MIN
        res = self.sweep(at)
        flagged = [f for f in res["findings"] if f.get("idle_owing")]
        self.assertEqual(len(flagged), 1, res["findings"])
        self.assertEqual(flagged[0]["presence"], "fresh")
        rings = self.rings()
        self.assertEqual(len(rings), 1, self.dms)
        self.assertIn(rid[:8], rings[0])
        self.assertIn("helm dispatch triage %s" % rid[:8], rings[0])
        # THE SENDER IS NOT TOLD: the wake is the act, as for a WORKING row
        # leg B can wake (task/3161).
        self.assertEqual([to for to, _t in self.dms if to != SEAT], [])
        # TWELVE: still idle-owing, and NOT rung again -- the ring is bounded.
        res = self.sweep(ended + 12 * MIN)
        self.assertEqual(len(self.rings()), 1, "the idle seat was re-rung "
                                               "inside its backoff")
        held = [f for f in res["findings"] if f.get("idle_owing")]
        self.assertTrue(held and held[0].get("idle_skipped"), held)
        # THE OVERDUE LIST says the same thing about the same row.
        out = self.listing(at, "--overdue")
        self.assertIn("NEEDS CHECK-IN", out)     # control: the row is late
        self.assertIn("IDLE-OWING", out)
        # AND THE MEASUREMENT the next rescore runs answers both bars.
        m = si.measure(hours=1.0, now=ended + 13 * MIN)
        self.assertEqual(m["passes"], 3, m)
        mine = [s for s in m["seats"] if s["seat"] == SEAT]
        self.assertEqual(len(mine), 1, m)
        self.assertEqual(mine[0]["over"], 1, mine)
        self.assertEqual(mine[0]["over_not_resurfaced"], 0, mine)
        self.assertEqual(mine[0]["states"], {"IDLE": 3}, mine)
        self.assertGreaterEqual(mine[0]["idle_owing_min"], 12)
        out = io.StringIO()
        with mock.patch("time.time", return_value=ended + 13 * MIN), \
                contextlib.redirect_stdout(out):
            rc = idle_dispatch.cmd_idle_dispatch(["--owing", "--hours", "1"])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("re-surfaced (rung", out.getvalue())
        self.assertIn("0 over-bar stretches not re-surfaced", out.getvalue())

    def test_an_idle_seat_owing_nothing_is_not_flagged(self):  # noqa: VACUOUS_ASSERTION — the seat is asserted IDLE for 30m and the pass asserted recorded before the empty findings are read
        self.call()
        ended = self.end_turn()
        at = ended + 30 * MIN
        r = _si().reading(SEAT, now=at)
        self.assertEqual(r["state"], _si().IDLE, r)    # control: it IS idle
        self.assertGreaterEqual(r["idle_s"], 30 * MIN)
        res = self.sweep(at)
        self.assertEqual(res["findings"], [])
        self.assertEqual(self.dms, [])
        m = _si().measure(hours=1.0, now=at + MIN)
        self.assertEqual(m["passes"], 1)             # the pass was recorded
        self.assertEqual([s for s in m["seats"] if s["seat"] == SEAT], [])


class UnknownIsNeverIdleTest(_IdleWorld):

    def test_an_unreadable_hook_record_is_unknown_and_never_rung(self):  # noqa: VACUOUS_ASSERTION — the same seat reads IDLE before the record is damaged, and the ring arm in IdleOwingTest is the positive control on rings()
        self.plant("a7a7a7a7a7a7a7a7", "review the canary")
        self.call()
        ended = self.end_turn()
        at = ended + 30 * MIN
        self.assertEqual(_si().reading(SEAT, now=at)["state"], _si().IDLE)
        path = os.path.join(record.session_dir(SID), "counters.json")
        with open(path, "w") as f:
            f.write("{not json")
        r = _si().reading(SEAT, now=at)
        self.assertEqual(r["state"], _si().UNKNOWN, r)
        self.assertIn("unreadable", r["why"])
        self.sweep(at)
        self.assertEqual(self.rings(), [])
        # AND THE MEASUREMENT SAYS UNKNOWN, never "0m idle": a seat whose
        # hooks helm cannot read is unmeasured, not proven busy.
        m = _si().measure(hours=1.0, now=at + MIN)
        mine = [s for s in m["seats"] if s["seat"] == SEAT]
        self.assertEqual(mine and mine[0]["states"], {"UNKNOWN": 1}, m)
        self.assertEqual(m["seats_unknown"], 1)
        self.assertIn("UNKNOWN 1", "\n".join(_si().measure_lines(m)))

    def test_a_session_with_no_hook_record_is_unknown(self):
        """A stop alone writes nothing: the turn-end writer is gated on the
        recorder's own evidence, as turn_open is."""
        rc, _o, _e = self.stop()
        r = _si().reading(SEAT)
        self.assertEqual(r["state"], _si().UNKNOWN, r)
        self.assertIn("no hook record", r["why"])
        del rc

    def test_an_unreadable_roster_is_unknown(self):
        self.call()
        self.end_turn()
        from helm import seats_roster
        with mock.patch.object(seats_roster, "roster_checked",
                               return_value=({}, True)):
            r = _si().reading(SEAT)
        self.assertEqual(r["state"], _si().UNKNOWN, r)
        self.assertIn("roster", r["why"])

    def test_an_edge_that_is_not_a_time_is_unknown(self):  # noqa: VACUOUS_ASSERTION — the state is asserted equal to UNKNOWN for each of three literal values
        self.call()
        ended = self.end_turn()
        path = os.path.join(record.session_dir(SID), "counters.json")
        for bad in ("yesterday", True, ended + 86400):
            c = self.counters()
            c["turn-ended-at"] = bad
            with open(path, "w") as f:
                json.dump(c, f)
            r = _si().reading(SEAT, now=ended + 30 * MIN)
            self.assertEqual(r["state"], _si().UNKNOWN, (bad, r))

    def test_no_pass_in_the_window_is_unknown_not_zero(self):  # noqa: VACUOUS_ASSERTION — rc 1 and the UNKNOWN word are asserted present
        m = _si().measure(hours=2.0)
        self.assertEqual(m["passes"], 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = idle_dispatch.cmd_idle_dispatch(["--owing"])
        self.assertEqual(rc, 1)
        self.assertIn("UNKNOWN", out.getvalue())


@unittest.skipUnless(_has_rest(), "lane owner-pause-is-a-seat-state-3280 "
                     "(helm/seat_rest.py, the RESTING state) is not on this "
                     "base; seat_idle reads it lazily, so this arm runs once "
                     "both are on trunk")
class RestingIsNeverIdleOwingTest(_IdleWorld):

    def test_a_resting_seat_owing_a_row_is_resting_and_never_rung(self):  # noqa: VACUOUS_ASSERTION — the same seat reads IDLE before the rest and RESTING after it
        from helm import pk, seat_rest
        self.plant("b8b8b8b8b8b8b8b8", "review the canary")
        self.call()
        ended = self.end_turn()
        at = ended + 30 * MIN
        self.assertEqual(_si().reading(SEAT, now=at)["state"], _si().IDLE)
        path = seat_rest.path(SEAT)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.atomic_write(path, json.dumps({
            "v": 1, "seat": SEAT, "by": "seat-a",
            "role": "integrator", "because": "drain to pause", "at": ended,
            "until": None, "ended": None}))
        r = _si().reading(SEAT, now=at)
        self.assertEqual(r["state"], _si().RESTING, r)
        self.sweep(at)
        self.assertEqual(self.rings(), [])


class SeatWhereShowsIdleAgeTest(_IdleWorld):

    def test_where_prints_the_idle_age_in_both_forms(self):
        self.call()
        ended = self.end_turn()
        from helm import orcaadopt
        info = {"provenance": "orca-adopted", "state": "LIVE",
                "evidence": "fixture", "sessions": [SID]}
        at = ended + 25 * MIN
        with mock.patch.object(orcaadopt, "resolve", return_value=dict(info)), \
                mock.patch("time.time", return_value=at):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = seat_lifecycle._where_adopted(SEAT, [], "unknown")
            self.assertEqual(rc, 0)
            self.assertIn("idle    : IDLE 25m", out.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                seat_lifecycle._where_adopted(SEAT, ["--json"], "unknown")
        got = json.loads(out.getvalue())
        self.assertEqual(got["idle"]["state"], "IDLE", got)


if __name__ == "__main__":
    unittest.main()
