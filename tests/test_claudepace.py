#!/usr/bin/env python3
"""helm.claudepace — each Claude account's 5h window: pace, projection and one
gentle state (pace5h).

WHAT THESE PIN, one class per claim the module makes:

  PACE        the measured day read at 5-minute probes: its pace and its
              projection, and what the rule answers for it; a hotter day of
              the same shape reads WATCH before the reset and names the hit;
  UNKNOWN     too few readings, or too short a span, is UNKNOWN, never a guess;
              and WATCH is entered only on an hour of readings;
  429         a throttled probe is no new reading: the state stays;
  HYSTERESIS  WATCH holds until the projection falls under 90%, and a reset
              clears it;
  SURFACES    the snapshot carries no address; `helm creds`, `helm burn` and
              the quota page's rows carry the one line;
  STEER       said once per state change, to a Claude seat on that account;
  FLAP        each state is said at most once per window, whatever the
              readings do: a WATCH that clears and comes back is not
              re-said, and simulated noisy windows (fixed seeds) never say
              one state twice;
  ALLOCATE    an equal choice between a WATCH and an OK account picks OK;
  IN FLIGHT   nothing here posts, stops or refuses anything.

Every world is SYNTHETIC: history rows in the shape
`providers.NativeQuotaProvider._probe_one` appends, example.com addresses, no
network. Nothing reads this machine's history, snapshot or homes.
"""
import calendar
import contextlib
import io
import json
import os
import random
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-claudepace-", var="HELM_HOME")

from helm import accounts, claudepace as cp  # noqa: E402

ACCT = "alice@example.com"
OTHER = "bob@example.com"


def at(h, m):
    """An instant on the measured day, UTC."""
    return float(calendar.timegm((2026, 9, 28, h, m, 0)))


RESET = at(19, 50)
MEASURED = [(at(15, 52), 4), (at(17, 22), 33), (at(18, 52), 72),
            (at(19, 37), 93)]
# THE SAME DAY, HOTTER: the builds land an hour earlier.
HOT = [(at(15, 52), 4), (at(17, 22), 33), (at(18, 22), 70), (at(18, 52), 80)]
# THE HOT DAY, then the builds move off and the account flattens.
FLATTENS = HOT + [(at(19, 47), 82)]


def series(knots, reset=RESET, step=300):
    """Probe readings every `step` seconds, linear between the measured
    knots and rounded to the vendor's integer percent."""
    out, t = [], knots[0][0]
    while t <= knots[-1][0]:
        for (a, u), (b, v) in zip(knots, knots[1:]):
            if a <= t <= b:
                out.append((t, float(round(u + (v - u) * (t - a) / (b - a))),
                            reset))
                break
        t += step
    return out


def iso(ts):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def hrow(account, ts, pct, reset=RESET, status="allowed"):
    """One native history row, as the creds probe cycle appends it."""
    gauges = [] if pct is None else [
        {"label": "5h", "kind": "session", "utilization": pct / 100.0,
         "reset": reset, "limit": None, "remaining": None},
        {"label": "7d", "kind": "period", "utilization": 0.5,
         "reset": reset + 4 * 86400, "limit": None, "remaining": None}]
    return {"provider": "anthropic", "account": account, "probed_at": iso(ts),
            "status": status, "primary": "5h" if gauges else None,
            "gauges": gauges, "source_at": None}


def rows_of(points, account=ACCT):
    return [hrow(account, t, u, r) for t, u, r in points]


def walk(points, prior=None):
    """Read the series probe by probe, as the watchdog pass would, carrying
    the prior record -> [(now, record)]."""
    out = []
    for i in range(len(points)):
        now = points[i][0]
        prior = cp.account_reading(points[:i + 1], now, prior)
        out.append((now, prior))
    return out


class PaceTest(unittest.TestCase):
    """The pace is the least-squares slope since the window's start, and the
    projection is the newest reading carried to the reset at that pace."""

    def test_the_measured_day_is_paced_and_projected_from_its_own_readings(self):
        steps = dict(walk(series(MEASURED)))
        rec = steps[at(18, 52)]
        self.assertEqual(rec["used_pct"], 72.0)
        self.assertEqual(rec["reset_at"], RESET)
        # 4% -> 72% over three hours, a little faster in the last ninety
        # minutes: the fit sits between the two segment paces.
        self.assertGreater(rec["pct_per_hour"], 19.3)
        self.assertLess(rec["pct_per_hour"], 26.0)
        self.assertAlmostEqual(rec["projected_pct"],
                               72.0 + rec["pct_per_hour"] * 58 / 60.0,
                               delta=0.2)

    def test_the_measured_day_never_projects_past_the_wall(self):
        """THE BRIEF EXPECTED WATCH HERE, AND THE ARITHMETIC SAYS NO. The day
        landed at 96% at the reset; no segment of it ran faster than 28%/h,
        so no straight line through its readings reaches 100% before 19:50Z.
        Its projection climbs into the nineties, through the band where a
        WATCH would hold, and the rule as the owner stated it ("will run out
        before it resets at the current pace") stays OK — which is also what
        happened."""
        steps = walk(series(MEASURED))
        projected = [r["projected_pct"] for _now, r in steps
                     if r["projected_pct"] is not None]
        self.assertGreater(max(projected), 95.0)       # it came close...
        self.assertLess(max(projected), cp.WATCH_PCT)  # ...and never hit
        self.assertEqual({r["state"] for _now, r in steps},
                         {cp.UNKNOWN, cp.OK})

    def test_a_hotter_day_reads_WATCH_by_18_52_and_names_the_hit(self):
        steps = dict(walk(series(HOT)))
        rec = steps[at(18, 52)]
        self.assertEqual(rec["state"], cp.WATCH)
        self.assertGreaterEqual(rec["projected_pct"], cp.WATCH_PCT)
        self.assertIsNotNone(rec["hit_at"])
        self.assertLess(rec["hit_at"], RESET)          # before the reset
        # it read WATCH well before the last measured point
        first = min(now for now, r in steps.items() if r["state"] == cp.WATCH)
        self.assertLess(first, at(18, 22))
        self.assertIn("hits 100% ~", cp.describe(rec))
        self.assertIn("resets 19:50Z", cp.describe(rec))

    def test_TIGHT_is_the_level_with_time_left_and_needs_no_pace(self):
        rec = cp.account_reading([(at(18, 50), 86.0, RESET)], at(18, 50))
        self.assertEqual(rec["state"], cp.TIGHT)
        self.assertIsNone(rec["pct_per_hour"])
        # CONTROL: the same level with 25 minutes left is not TIGHT
        late = cp.account_reading([(at(19, 25), 86.0, RESET)], at(19, 25))
        self.assertNotEqual(late["state"], cp.TIGHT)


class UnknownTest(unittest.TestCase):
    """Too few readings is UNKNOWN, never a guess."""

    def test_two_readings_are_UNKNOWN(self):
        rec = cp.account_reading([(at(17, 0), 20.0, RESET),
                                  (at(18, 0), 50.0, RESET)], at(18, 0))
        self.assertEqual(rec["state"], cp.UNKNOWN)
        self.assertIsNone(rec["pct_per_hour"])
        self.assertIsNone(rec["projected_pct"])
        self.assertIn("a pace needs 3", rec["why"])

    def test_three_readings_over_ten_minutes_are_UNKNOWN(self):
        pts = [(at(18, 0), 50.0, RESET), (at(18, 5), 60.0, RESET),
               (at(18, 10), 70.0, RESET)]
        self.assertEqual(cp.account_reading(pts, at(18, 10))["state"],
                         cp.UNKNOWN)
        # CONTROL: the same three readings over twenty minutes are paced
        pts = [(at(18, 0), 50.0, RESET), (at(18, 10), 60.0, RESET),
               (at(18, 20), 70.0, RESET)]
        self.assertIsNotNone(
            cp.account_reading(pts, at(18, 20))["pct_per_hour"])

    def test_WATCH_needs_an_hour_of_readings(self):
        """EARLY IN A WINDOW THE LEVER IS LONGEST. A pace over twenty minutes
        is carried up to ~4.6 hours to the reset, so its noise is magnified
        past the hysteresis band; WATCH waits for an hour of readings."""
        pts = [(at(18, 0) + i * 300, 20.0 + 5.0 * i, RESET)
               for i in range(13)]                     # 60%/h, 18:00-19:00
        early = cp.account_reading(pts[:12], pts[11][0])      # 55 minutes
        self.assertEqual(early["state"], cp.UNKNOWN)
        self.assertGreaterEqual(early["projected_pct"], cp.WATCH_PCT)
        self.assertIn("an hour", early["why"])
        self.assertIn("projects", cp.describe(early))
        # CONTROL: the same pace one reading later, an hour in, is WATCH
        self.assertEqual(cp.account_reading(pts, pts[-1][0])["state"],
                         cp.WATCH)

    def test_a_row_whose_gauges_are_not_a_list_is_no_reading(self):
        """review P3-2: one malformed history row is skipped, never a
        TypeError that fails every pass while it sits in the read window."""
        rows = rows_of(series(HOT))
        bad = dict(rows[-1], gauges=5)
        reading = cp.read(rows[:-1] + [bad], at(18, 52))
        rec = reading["accounts"][accounts.measured_key(ACCT)]
        self.assertEqual(rec["measured_at"], series(HOT)[-2][0])

    def test_a_reading_of_the_last_window_is_not_one_of_this(self):
        old = [(at(12, 0), 90.0, at(14, 0)), (at(12, 30), 95.0, at(14, 0)),
               (at(13, 0), 98.0, at(14, 0))]
        rec = cp.account_reading(old, at(14, 10))
        self.assertEqual(rec["state"], cp.UNKNOWN)
        self.assertIn("reset at 14:00Z", rec["why"])


class ThrottleTest(unittest.TestCase):
    """A 429 is "no new reading", never zero."""

    def test_a_429_leaves_the_last_state(self):
        points = series(HOT)
        rows = rows_of(points)
        before = cp.read(rows, at(18, 52))
        key = accounts.measured_key(ACCT)
        self.assertEqual(before["accounts"][key]["state"], cp.WATCH)
        # the throttled probe cycle writes a row with no gauges
        throttled = rows + [hrow(ACCT, at(18, 57), None, status="http_429"),
                            hrow(ACCT, at(19, 2), None, status="http_429")]
        after = cp.read(throttled, at(19, 2), prior=before)
        rec = after["accounts"][key]
        self.assertEqual(rec["state"], cp.WATCH)
        self.assertEqual(rec["used_pct"], 80.0)
        self.assertEqual(rec["measured_at"], at(18, 52))
        self.assertEqual(rec["since"], before["accounts"][key]["since"])
        # CONTROL: the same instant read as a 0% reading would have reset
        # the pace, so the throttled rows really were skipped
        zeroed = rows + [hrow(ACCT, at(18, 57), 0.0)]
        self.assertNotEqual(
            cp.read(zeroed, at(18, 57))["accounts"][key]["used_pct"], 80.0)


class HysteresisTest(unittest.TestCase):

    def test_WATCH_holds_until_the_projection_is_under_90(self):
        steps = walk(series(FLATTENS))
        held = [(now, r) for now, r in steps if r["state"] == cp.WATCH
                and r["projected_pct"] < cp.WATCH_PCT]
        self.assertTrue(held, "the flattening day never sat in the band")
        for now, rec in held:
            self.assertGreaterEqual(rec["projected_pct"], cp.CLEAR_PCT)
            # CONTROL: with no prior the same readings read OK
            bare = cp.account_reading(
                [p for p in series(FLATTENS) if p[0] <= now], now)
            self.assertEqual(bare["state"], cp.OK)
        cleared = [r for _now, r in steps if r["projected_pct"] is not None
                   and r["projected_pct"] < cp.CLEAR_PCT
                   and _now > held[-1][0]]
        self.assertTrue(cleared)
        self.assertTrue(all(r["state"] == cp.OK for r in cleared))

    def test_a_new_window_clears_the_latch(self):
        prior = dict(walk(series(HOT)))[at(18, 52)]
        self.assertEqual(prior["state"], cp.WATCH)
        nxt = RESET + cp.WINDOW_S
        pts = [(at(20, 0), 2.0, nxt), (at(20, 15), 4.0, nxt),
               (at(20, 30), 6.0, nxt)]
        rec = cp.account_reading(pts, at(20, 30), prior)
        self.assertEqual(rec["state"], cp.OK)
        self.assertNotEqual(rec["since"], prior["since"])


class SurfacesTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-claudepace-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "helm"),
            "HELM_CACHE_DIR": os.path.join(self.tmp, "cache")})
        patcher.start()
        self.addCleanup(patcher.stop)
        from helm import brief
        path = brief.usage_history_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            for row in rows_of(series(HOT)) + rows_of(
                    series([(at(17, 0), 5), (at(18, 50), 12)]), OTHER):
                fh.write(json.dumps(row) + "\n")

    def test_the_pass_writes_a_snapshot_with_no_address(self):
        reading = cp.watch_pass(now=at(18, 52))
        self.assertEqual(
            reading["accounts"][accounts.measured_key(ACCT)]["state"], cp.WATCH)
        self.assertEqual(
            reading["accounts"][accounts.measured_key(OTHER)]["state"], cp.OK)
        with open(cp.snapshot_path()) as fh:
            text = fh.read()
        self.assertNotIn(ACCT, text)
        self.assertNotIn(OTHER, text)
        self.assertEqual(sorted(r["label"] for r in
                                json.loads(text)["accounts"].values()),
                         sorted([accounts.mask_identity(ACCT),
                                 accounts.mask_identity(OTHER)]))
        self.assertIsNotNone(cp.cached(now=at(18, 55)))
        # a snapshot past the burn flags' bound is not read
        self.assertIsNone(cp.cached(now=at(18, 52) + 3 * 3600))

    def test_burn_prints_one_line_per_account(self):
        cp.watch_pass(now=at(18, 52))
        lines = cp.burn_lines(now=at(18, 53))
        self.assertEqual(len(lines), 2)
        mine = [l for l in lines if accounts.mask_identity(ACCT) in l][0]
        self.assertIn("5h WATCH: 80%", mine)
        self.assertIn("%/h", mine)
        self.assertIn("hits 100% ~", mine)
        self.assertIn("resets 19:50Z", mine)

    def test_burn_says_not_measured_without_a_snapshot(self):
        self.assertIn("not measured", cp.burn_lines(now=at(18, 53))[0])

    def test_creds_carries_the_line_under_the_account(self):
        from helm import creds

        class Stub(object):
            def accounts(self):
                return [{"account": ACCT, "provider": "anthropic", "tier": "Max"}]

            def cred_state(self):
                return [{"account": ACCT, "cred_state": "ok",
                         "headroom_pct": 20, "resets_at_ms": None}]

            def windows(self):
                return []

        cp.watch_pass(now=at(18, 52))
        out = io.StringIO()
        with mock.patch.object(creds, "default_provider", return_value=Stub()), \
                mock.patch.object(cp.time, "time", return_value=at(18, 53)), \
                contextlib.redirect_stdout(out):
            self.assertEqual(creds.cmd_creds([]), 0)
        text = out.getvalue()
        self.assertIn("5h WATCH: 80%", text)
        self.assertIn("resets 19:50Z", text)


def _claude_home(root, email):
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, ".claude.json"), "w") as fh:
        json.dump({"oauthAccount": {"emailAddress": email}}, fh)
    return root


class SteerTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-claudepace-steer-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = _claude_home(os.path.join(self.tmp, "claude-a"), ACCT)
        self.ctx = {"harness": "claude", "config_home": self.home}
        patcher = mock.patch.dict(os.environ, {"HELM_CHAT_NAME": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def snap(self, state, since=1.0, reset=RESET):
        rec = {"state": state, "used_pct": 81.0, "pct_per_hour": 24.0,
               "hit_at": at(19, 40) if state == cp.WATCH else None,
               "reset_at": reset, "since": since, "label": "a…@example.com"}
        return {"v": 1, "ts": at(19, 0),
                "accounts": {accounts.measured_key(ACCT): rec}}

    def test_the_steer_fires_once_per_state_change(self):
        first = cp.steer(self.ctx, None, snap=self.snap(cp.WATCH))
        self.assertIsNotNone(first)
        line, heard = first
        self.assertIn("5h window is WATCH at 81%, 24%/h", line)
        self.assertIn("resets 19:50Z", line)
        self.assertIn("route NEW builds and reviews", line)
        self.assertIn("keep your own work going", line)
        self.assertNotIn("finish what you are doing", line)
        # the same state is not said again
        self.assertIsNone(cp.steer(self.ctx, heard, snap=self.snap(cp.WATCH)))
        # a new state is
        tight = cp.steer(self.ctx, heard, snap=self.snap(cp.TIGHT, since=2.0))
        self.assertIsNotNone(tight)
        self.assertNotEqual(tight[1], heard)
        # and OK says nothing
        self.assertIsNone(cp.steer(self.ctx, tight[1], snap=self.snap(cp.OK)))

    def test_a_seat_hears_nothing_while_another_claude_account_has_room(self):
        """THE WATCHER MOVES A SEAT'S CREDENTIAL when a window caps, so a
        pressed account's seat is told nothing while any other Claude account
        is not pressed; only when every account is pressed does it hear the
        routing line (the owner: "why bother them with it unless they are in
        a situation where they need the information")."""
        snap = self.snap(cp.TIGHT)
        room = dict(snap["accounts"][accounts.measured_key(ACCT)],
                    state=cp.OK, label="b…@example.com")
        snap["accounts"][accounts.measured_key(OTHER)] = room
        self.assertIsNone(cp.steer(self.ctx, None, snap=snap))
        # CONTROL: every account pressed, so the seat hears it
        snap["accounts"][accounts.measured_key(OTHER)]["state"] = cp.WATCH
        said = cp.steer(self.ctx, None, snap=snap)
        self.assertIsNotNone(said)
        self.assertIn("5h window is TIGHT", said[0])
        self.assertIn("keep your own work going", said[0])

    def test_an_account_with_no_reading_is_not_room(self):
        """UNKNOWN IS NOT ROOM: an account with no reading of its window, or
        a record that names no state, may be as full as this one, so it does
        not silence a pressed seat. The seat hears the routing line, and the
        line does not claim that every account is pressed."""
        other = accounts.measured_key(OTHER)
        for rec in ({"state": cp.UNKNOWN}, {"state": None}, {}):
            snap = self.snap(cp.TIGHT)
            snap["accounts"][other] = dict(rec, label="b…@example.com")
            said = cp.steer(self.ctx, None, snap=snap)
            self.assertIsNotNone(said, rec)
            self.assertIn("known to have room", said[0])
            self.assertNotIn("every Claude account is pressed", said[0])
            self.assertIn("keep your own work going", said[0])
        snap["accounts"][other]["state"] = cp.UNKNOWN
        self.assertIsNotNone(cp.steer(self.ctx, None, snap=snap))
        # CONTROL: an OK account is room, so the seat hears nothing
        snap["accounts"][other]["state"] = cp.OK
        self.assertIsNone(cp.steer(self.ctx, None, snap=snap))

    def test_a_seat_homed_to_the_account_hears_it_even_when_another_has_room(self):
        """A seat HOMED to one credential (a named credhome) is not moved by
        the watcher, so its window is real information to it: it hears the
        state while other accounts have room, and is asked to pace to the
        reset, never to stop."""
        snap = self.snap(cp.TIGHT)
        snap["accounts"][accounts.measured_key(OTHER)] = dict(
            snap["accounts"][accounts.measured_key(ACCT)], state=cp.OK)
        from helm import cred
        with mock.patch.object(cred, "is_credhome", return_value=True):
            said = cp.steer(self.ctx, None, snap=snap)
        self.assertIsNotNone(said)
        self.assertIn("homed to this account", said[0])
        self.assertIn("pace", said[0])
        self.assertNotIn("finish what you are doing", said[0])
        # CONTROL: the same seat on the baseload home hears nothing
        with mock.patch.object(cred, "is_credhome", return_value=False):
            self.assertIsNone(cp.steer(self.ctx, None, snap=snap))

    def test_a_WATCH_that_returns_in_the_same_window_is_not_said_again(self):
        """THE FLAP (review P2-1): WATCH, then OK, then WATCH again early in
        one window is one state, said once. The heard key is the account,
        the state and the window's reset, never the instant it was entered."""
        _line, heard = cp.steer(self.ctx, None, snap=self.snap(cp.WATCH))
        self.assertIsNone(cp.steer(self.ctx, heard, snap=self.snap(cp.OK)))
        # re-entered: a new `since`, and a reset a few seconds off (one
        # window's readings agree on it within RESET_TOLERANCE_S)
        self.assertIsNone(cp.steer(self.ctx, heard, snap=self.snap(
            cp.WATCH, since=3.0, reset=RESET + 30)))
        # a step up in the same window is said, once
        tight = cp.steer(self.ctx, heard, snap=self.snap(cp.TIGHT, since=4.0))
        self.assertIsNotNone(tight)
        self.assertIsNone(cp.steer(self.ctx, tight[1],
                                   snap=self.snap(cp.TIGHT, since=5.0)))
        # a step back down to WATCH is not said at all
        self.assertIsNone(cp.steer(self.ctx, tight[1],
                                   snap=self.snap(cp.WATCH, since=6.0)))
        # CONTROL: the next window's WATCH is a new thing to hear
        nxt = cp.steer(self.ctx, tight[1], snap=self.snap(
            cp.WATCH, since=7.0, reset=RESET + cp.WINDOW_S))
        self.assertIsNotNone(nxt)
        self.assertIn("WATCH", nxt[0])

    def test_only_a_claude_seat_on_that_account_hears_it(self):
        snap = self.snap(cp.WATCH)
        self.assertIsNone(cp.steer(dict(self.ctx, harness="codex"), None,
                                   snap=snap))
        other = _claude_home(os.path.join(self.tmp, "claude-b"), OTHER)
        self.assertIsNone(cp.steer(dict(self.ctx, config_home=other), None,
                                   snap=snap))
        # CONTROL: the account's own seat does
        self.assertIsNotNone(cp.steer(self.ctx, None, snap=snap))

    def test_another_accounts_seat_never_asks_the_seat_family(self):
        """review P3-4: while some account is pressed, a seat on another
        account is answered by its home's account, before the seat register
        is imported and read."""
        other = _claude_home(os.path.join(self.tmp, "claude-b"), OTHER)
        with mock.patch.object(cp, "_proxy_seat",
                               side_effect=AssertionError("asked")):
            self.assertIsNone(cp.steer(dict(self.ctx, config_home=other),
                                       None, snap=self.snap(cp.WATCH)))
        # CONTROL: the pressed account's own seat is still asked
        with mock.patch.object(cp, "_proxy_seat", return_value=True) as ask:
            self.assertIsNone(cp.steer(self.ctx, None,
                                       snap=self.snap(cp.WATCH)))
        ask.assert_called_once_with()

    def test_the_steer_rides_inject_once_per_context_state(self):
        import importlib
        from helm import inject, injection_schema
        # the module, not the function the package re-exports under its name
        _whisper = importlib.import_module("helm.inject._whisper")
        env = {"HELM_HOME": os.path.join(self.tmp, "helm"),
               "HELM_ADOPTED_DIR": os.path.join(self.tmp, "adopted"),
               "HELM_CACHE_DIR": os.path.join(self.tmp, "cache"),
               "HELM_CHAT_DIR": os.path.join(self.tmp, "chat"),
               "HELM_SEAT_NAMES": os.path.join(self.tmp, "seat-names.txt")}
        os.makedirs(env["HELM_ADOPTED_DIR"])
        context = (injection_schema.V3, dict(self.ctx, session="s-1"), {},
                   None, None)
        import time
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(_whisper, "_sample_context",
                                  return_value=context):
            # the snapshot the posting pass would have written, just now
            self.assertTrue(cp.write_snapshot(
                dict(self.snap(cp.WATCH), ts=time.time())))
            said = [inject.gather("carry on with the build", session="s-1")
                    ["reflex"] for _ in range(3)]
        hits = [[l for l in lane if "5h window is WATCH" in l] for lane in said]
        self.assertEqual(len(hits[0]), 1, said[0])
        self.assertEqual(hits[1:], [[], []])


NOISE_START = at(12, 0)
NOISE_RESET = NOISE_START + cp.WINDOW_S


def noisy_window(seed, shape):
    """One synthetic 5h window read every five minutes, the vendor's integer
    percent, from a fixed seed. GAUSS: bursty turns around ~19%/h, a window
    that lands near the wall. ONOFF: builds that start and stop."""
    rng = random.Random(seed)
    used, on, out = 0.0, True, []
    for i in range(60):
        if shape == "gauss":
            used += max(0.0, rng.gauss(1.6, 1.6))
        else:
            if rng.random() < 0.2:
                on = not on
            used += rng.uniform(1.5, 4.0) if on else 0.0
        out.append((NOISE_START + i * 300, float(min(100, round(used))),
                    NOISE_RESET))
    return out


class NoiseTest(unittest.TestCase):
    """PACING THAT CANNOT FLAP (review P2-1). Every window is read pass by
    pass through `read` with the prior snapshot fed back, as the watchdog
    does, and every pass's snapshot is offered to one seat on the account,
    carrying the key it last heard, as `helm inject` does. Deterministic:
    fixed seeds, no clock, no network."""

    #: onoff 19 is the review's seed-261 shape: WATCH at 00:20 (projecting
    #: 171%), OK at 00:45, WATCH again at 01:25, then TIGHT.
    PINNED = (19, "onoff")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-claudepace-noise-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ctx = {"harness": "claude", "config_home": _claude_home(
            os.path.join(self.tmp, "claude-a"), ACCT)}
        patcher = mock.patch.dict(os.environ, {"HELM_CHAT_NAME": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_window(self, points, rows=True):
        """-> (the states pass by pass, [(state said, record)]). With `rows`
        each pass reads history rows through `read`; without, the same
        readings go straight to `account_reading`, the fold `read` runs per
        account (the batch below would spend its time parsing timestamps)."""
        key = accounts.measured_key(ACCT)
        history, snap, said, states, steers = [], None, None, [], []
        for i, (t, used, reset) in enumerate(points):
            if rows:
                history.append(hrow(ACCT, t, used, reset))
                snap = cp.read(history, t, snap)
            else:
                prior = (snap or {"accounts": {}})["accounts"].get(key)
                snap = {"v": cp.V, "ts": t, "accounts": {
                    key: cp.account_reading(points[:i + 1], t, prior)}}
            rec = snap["accounts"][key]
            states.append(rec["state"])
            got = cp.steer(self.ctx, said, snap=snap)
            if got:
                said = got[1]
                steers.append((rec["state"], rec))
        return states, steers

    def test_the_pinned_flap_says_each_state_once(self):
        states, steers = self.run_window(noisy_window(*self.PINNED))
        self.assertEqual([s for s, _r in steers], [cp.WATCH, cp.TIGHT])
        watch = steers[0][1]
        self.assertGreaterEqual(watch["span_s"], 3600)
        # the window really did go pressed, clear and pressed again: the
        # heard key, not a quiet series, is what kept the second WATCH unsaid
        entries = [s for i, s in enumerate(states)
                   if s in cp.PRESSED and (i == 0 or states[i - 1] != s)]
        self.assertGreaterEqual(len(entries), 2)

    def test_simulated_noise_says_each_state_at_most_once_per_window(self):
        reentered = 0
        for shape in ("gauss", "onoff"):
            for seed in list(range(120)) + [261]:
                states, steers = self.run_window(noisy_window(seed, shape),
                                                 rows=False)
                said = [s for s, _r in steers]
                self.assertEqual(len(said), len(set(said)),
                                 "%s seed %d said %s" % (shape, seed, said))
                self.assertNotEqual(said, [cp.TIGHT, cp.WATCH])
                for state, rec in steers:
                    if state == cp.WATCH:
                        self.assertGreaterEqual(rec["span_s"], 3600,
                                                "%s seed %d" % (shape, seed))
                trail = "".join("P" if s in cp.PRESSED else "-"
                                for s in states)
                reentered += bool(re.search("P-+P", trail))
        # CONTROL: the noise is real — some windows still leave WATCH and
        # come back, and none of them said it twice
        self.assertGreater(reentered, 0)


class AllocateTest(unittest.TestCase):
    """Where helm ranks Claude accounts for new work, an equal choice picks
    the account that is not WATCH or TIGHT, and a pressed account is still
    offered: an ordering, never an exclusion."""

    def alloc(self, pressed, headroom=(40, 40)):
        from helm import providers
        tmp = tempfile.mkdtemp(prefix="helm-test-claudepace-alloc-")
        self.addCleanup(shutil.rmtree, tmp, True)
        p = providers.NativeQuotaProvider(
            history_path=os.path.join(tmp, "history.jsonl"))
        accts = [{"name": n, "provider": "anthropic", "home": "/h/" + n,
                  "usable": True, "tier": None} for n in (ACCT, OTHER)]
        states = [{"account": n, "cred_state": "ok", "headroom_pct": h,
                   "tier": None, "status": "allowed"}
                  for n, h in zip((ACCT, OTHER), headroom)]
        snap = {"v": 1, "ts": at(19, 0), "accounts": {
            accounts.measured_key(n): {"state": s, "reset_at": RESET,
                                       "since": 1.0}
            for n, s in pressed.items()}}
        with mock.patch.object(p, "accounts", lambda: accts), \
                mock.patch.object(p, "cred_state", lambda: states), \
                mock.patch.object(p, "_allocation_rules", lambda: {}), \
                mock.patch.object(cp, "cached", return_value=snap):
            return p.allocate("claude-fable-5")

    def test_an_equal_choice_between_WATCH_and_OK_picks_OK(self):
        rows = self.alloc({ACCT: cp.WATCH, OTHER: cp.OK})
        self.assertEqual([r["account"] for r in rows], [OTHER, ACCT])
        self.assertTrue(all(r["eligible"] for r in rows))
        self.assertIn("5h WATCH", rows[1]["why"])
        # CONTROL: with nothing pressed the tie keeps the accounts' order
        rows = self.alloc({ACCT: cp.OK, OTHER: cp.OK})
        self.assertEqual([r["account"] for r in rows], [ACCT, OTHER])
        self.assertEqual(rows[0]["why"], "headroom")

    def test_an_alias_home_reads_its_accounts_state(self):
        """review P3-3: a second home on one account is named "email#dir",
        and the history keeps one key per identity, the email."""
        alias = ACCT + "#claude-a2"
        snap = {"v": 1, "ts": at(19, 0), "accounts": {
            accounts.measured_key(ACCT): {"state": cp.WATCH,
                                          "reset_at": RESET, "since": 1.0}}}
        self.assertEqual(cp.pressed_accounts([alias, OTHER], snap=snap),
                         {alias: cp.WATCH})
        self.assertEqual(cp.account_record(alias, snap)["state"], cp.WATCH)

    def test_the_tie_break_never_outranks_headroom(self):
        rows = self.alloc({ACCT: cp.TIGHT}, headroom=(60, 40))
        self.assertEqual([r["account"] for r in rows], [ACCT, OTHER])
        self.assertTrue(rows[0]["eligible"])


class InFlightTest(unittest.TestCase):
    """Nothing here posts, stops, pauses or refuses: every output is a line,
    a record or an ordering."""

    def test_the_pass_and_the_steer_touch_no_seat_room_or_dispatch(self):
        from helm import chat
        tmp = tempfile.mkdtemp(prefix="helm-test-claudepace-flight-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "snap.json")
        boom = mock.Mock(side_effect=AssertionError("posted"))
        with mock.patch.object(chat, "post", boom):
            reading = cp.watch_pass(now=at(18, 52), rows=rows_of(series(HOT)),
                                    path=path)
            snap = dict(reading)
            home = _claude_home(os.path.join(tmp, "claude-a"), ACCT)
            with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": ""}):
                got = cp.steer({"harness": "claude", "config_home": home},
                               None, snap=snap)
            pressed = cp.pressed_accounts([ACCT, OTHER], snap=snap)
        self.assertIsNotNone(got)
        self.assertEqual(pressed, {ACCT: cp.WATCH})
        boom.assert_not_called()
        import inspect
        source = inspect.getsource(cp)
        for verb in ("chat.post", "dispatches", "interrupt", "kill(",
                     "send_keys", "notify"):
            self.assertNotIn(verb, source)


if __name__ == "__main__":
    unittest.main()
