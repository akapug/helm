#!/usr/bin/env python3
"""task/3899: each seat's mood on the surfaces seats and the owner already read.

  AGENTS   `helm chat seats`, the roster listing agents read, carries one
           mood cell per row: the seat's latest measured state and its age,
           its own word, and DIVERGES when it says it is fine while helm
           measures it stuck or grinding. The cell reads two small files per
           seat and never measures, so the listing costs what it did.
  OWNER    the console's seats panel draws ONE face per seat, coloured by
           the measured state, dim when the seat's last sign of work is older
           than an hour, with the reason on hover (`/api/roster/mood`, on the
           panel's 60-second side channel beside `/api/roster/git`). The face
           smiles for flowing, frowns for stuck and blocked-on-owner, and is
           level for grinding, idle, walled and a state it does not know
           (task/4034).

Every world is a temp HELM_HOME and chat dir; the JS arm runs the real
function under node and is skipped where node is absent.
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-seatmood-surface-", var="HELM_HOME")

from helm import chat, pk, seats, web, web_ui_loader  # noqa: E402
# the seat facade beside any seat impl the arms reach (the co-occurrence law)
from helm import seat  # noqa: E402,F401
from tests.test_web_chat_client_runtime import _extract_fn  # noqa: E402

SEAT = "mood-seat"
NOW = 1_790_000_000.0
MIN = 60


def _sm():
    from helm import seatmood
    return seatmood


def _sf():
    from helm import seatmood_surface
    return seatmood_surface


def reading(state="stuck", seat=SEAT, **signals):
    sig = {"idle": "BUSY", "idle_s": None, "turn_opened": None,
           "progress_at": None}
    sig.update(signals)
    return {"seat": seat, "state": state, "score": 70,
            "reason": "the same refusal by author-gate 4 times in a row",
            "cause": "loop:refusal", "self": None, "self_at": None,
            "checkin": None, "divergent": False, "signals": sig}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-seatmood-surface-case-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "helm"),
            "HELM_CHAT_DIR": os.path.join(self.tmp, "chat"),
            "HELM_CHAT_NODE_URL": "", "HELM_CHAT_NAME": "",
            "HELM_CHAT_ROOM": "main",
            "HELM_ADOPTED_DIR": os.path.join(self.tmp, "adopted")})
        env.start()
        self.addCleanup(env.stop)

    def said(self, word, at, rating=None):
        sm = _sm()
        os.makedirs(sm._state_dir(), exist_ok=True)
        pk.write_json(sm._file(SEAT, ".json"),
                      {"seat": SEAT, "word": word, "why": None, "at": at,
                       "rating": rating, "blocker": None, "win": None})


class CellTest(Base):
    def test_the_cell_names_the_latest_measure_its_age_and_the_gap(self):
        sf = _sf()
        sf.remember(reading("stuck"), NOW - 4 * MIN)
        self.assertEqual(sf.cell(SEAT, now=NOW), " · mood stuck (4m ago)")
        self.said("fine", NOW - 10 * MIN)
        self.assertEqual(sf.cell(SEAT, now=NOW),
                         " · mood stuck (4m ago), says fine: DIVERGES")
        self.said("stuck", NOW - 10 * MIN, rating=2)
        self.assertEqual(sf.cell(SEAT, now=NOW),
                         " · mood stuck (4m ago), says stuck 2/5")

    def test_a_seat_nobody_measured_shows_only_what_it_said(self):
        sf = _sf()
        self.assertEqual(sf.cell(SEAT, now=NOW), "")
        self.said("fine", NOW - 10 * MIN)
        self.assertEqual(sf.cell(SEAT, now=NOW), " · says fine (10m ago)")
        # a measure older than a day is not today's mood
        sf.remember(reading("stuck"), NOW - 25 * 3600)
        self.assertEqual(sf.cell(SEAT, now=NOW), " · says fine (10m ago)")

    def test_a_hostile_or_tampered_record_prints_nothing(self):  # noqa: VACUOUS_ASSERTION — test_the_cell_names_the_latest_measure_its_age_and_the_gap drives the same cell() over the same two files to a non-empty line
        sf, sm = _sf(), _sm()
        self.assertEqual(sf.cell("lane\x1b[2Jpwn", now=NOW), "")
        os.makedirs(sm._state_dir(), exist_ok=True)
        pk.write_json(sm._file(SEAT, ".measured"),
                      {"seat": SEAT, "state": "stuck\x1b[2J", "at": NOW})
        pk.write_json(sm._file(SEAT, ".json"),
                      {"word": "fine‮", "at": NOW})
        self.assertEqual(sf.cell(SEAT, now=NOW), "")

    def test_every_measure_is_remembered_for_the_listing(self):
        sm, sig = _sm(), __import__("helm.seatmood_signals",
                                    fromlist=["x"])
        raw = {"seat": SEAT, "refusals": [
            {"at": NOW - (4 - i) * MIN, "guard": "author-gate",
             "reason": "PreToolUse:Bash"} for i in range(4)],
            "counters": {}, "idle": {"state": "BUSY", "idle_s": None,
                                     "turn_opened": NOW - MIN, "why": "x"},
            "progress": None, "wall": None, "card": None,
            "context_pct": None, "self": None, "unread": []}
        with mock.patch.object(sig, "live_seats",
                               return_value=([SEAT], {}, None)), \
                mock.patch.object(sig, "inputs",
                                  return_value=({SEAT: raw}, [])):
            sm.floor(now=NOW)
        self.assertEqual(_sf().latest(SEAT)["state"], "stuck")
        self.assertEqual(_sf().cell(SEAT, now=NOW + MIN),
                         " · mood stuck (1m ago)")


class ChatSeatsTest(Base):
    """The listing agents read, through its real verb."""

    def test_helm_chat_seats_carries_the_mood_cell(self):
        chat._ensure_dir()
        pk.write_json(seats.roster_path(), {
            SEAT: {"session": "a" * 32, "sessions": ["a" * 32],
                   "home_room": "main", "project": "helm"},
            "calm-seat": {"session": "b" * 32, "sessions": ["b" * 32],
                          "home_room": "main", "project": "helm"}})
        for name in (SEAT, "calm-seat"):
            with open(seats.seen_path(name), "w"):
                pass
        now = time.time()
        _sf().remember(reading("stuck"), now - 2 * MIN)
        self.said("fine", now - 5 * MIN)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seats.cmd("seats", [], "main")
        self.assertEqual(rc, 0, err.getvalue())
        lines = out.getvalue().splitlines()
        mine = [ln for ln in lines if SEAT in ln]
        calm = [ln for ln in lines if "calm-seat" in ln]
        self.assertEqual(len(mine), 1, lines)
        self.assertIn("mood stuck (2m ago), says fine: DIVERGES", mine[0])
        self.assertEqual(len(calm), 1, lines)
        self.assertNotIn("mood", calm[0])


class RecencyTest(unittest.TestCase):
    def test_busy_without_a_recent_call_is_dim_not_perpetually_now(self):
        sf = _sf()
        stale = reading("stuck", idle="BUSY",
                        turn_opened=NOW - 5 * 3600,
                        last_call=NOW - 4 * 3600)
        self.assertEqual(sf.recency(stale, NOW), ("today", 4 * 3600))
        self.assertEqual(sf.projection(stale, NOW)["recency"], "today")
        fresh = dict(stale, signals=dict(stale["signals"],
                                     last_call=NOW - 2 * MIN))
        self.assertEqual(sf.recency(fresh, NOW), ("now", 2 * MIN))
        self.assertEqual(sf.recency(reading("stuck", idle="BUSY"), NOW),
                         ("older", None))

    def test_idle_reader_last_call_reaches_the_dot_projection(self):  # noqa: VACUOUS_ASSERTION — the same projection returns today for stale last_call and now for a fresh one, both asserted to non-empty values
        from helm import seatmood_signals as sig, seat_idle
        last = NOW - 4 * 3600
        with mock.patch.object(seat_idle, "reading", return_value={
                "state": "BUSY", "turn_opened": NOW - 5 * 3600,
                "last_call": last, "idle_s": None}):
            idle = sig._idle(SEAT, {}, None, NOW)
        self.assertEqual(idle["last_call"], last)
        mood = _sm().judge({"seat": SEAT, "idle": idle, "progress": None,
                            "refusals": [], "counters": {}, "wall": None,
                            "card": None, "context_pct": None,
                            "self": None, "unread": []}, NOW)
        dot = _sf().projection(mood, NOW)
        self.assertEqual((dot["recency"], dot["age_s"]),
                         ("today", 4 * 3600))
        fresh = dict(mood, signals=dict(mood["signals"],
                                        last_call=NOW - 2 * MIN))
        self.assertEqual(_sf().projection(fresh, NOW)["recency"], "now")

    def test_the_four_buckets(self):
        sf = _sf()
        busy = reading("flowing", last_call=NOW)
        self.assertEqual(sf.recency(busy, NOW), ("now", 0))
        cases = ((5 * MIN, "now"), (40 * MIN, "under 1 h"),
                 (5 * 3600, "today"), (30 * 3600, "older"))
        for ago, bucket in cases:
            idle = reading("idle", idle="IDLE", idle_s=ago)
            self.assertEqual(sf.recency(idle, NOW)[0], bucket, ago)
        progress = reading("idle", idle="UNKNOWN", progress_at=NOW - 20 * MIN)
        self.assertEqual(sf.recency(progress, NOW), ("under 1 h", 20 * MIN))
        self.assertEqual(sf.recency(reading("idle", idle="UNKNOWN"), NOW),
                         ("older", None))


class WebApiTest(Base):
    def setUp(self):
        super().setUp()
        from helm import web_roster
        web_roster._ROSTER_MOOD_CACHE.clear()
        self.addCleanup(web_roster._ROSTER_MOOD_CACHE.clear)

    def test_the_mood_route_serves_one_projection_per_seat(self):
        m = dict(reading("stuck", last_call=time.time()), self="fine", divergent=True,
                 self_at=pk.epoch_ts(NOW), checkin={
                     "rating": 4, "why": None, "blocker": "fab is down",
                     "win": None})
        self.assertIn("/api/roster/mood", web.QUERY_API)
        with mock.patch.object(_sm(), "floor", return_value=[m]) as floor:
            body, status = web.QUERY_API["/api/roster/mood"]({})
            again, _status = web.QUERY_API["/api/roster/mood"]({})
        self.assertEqual(status, 200)
        self.assertEqual(floor.call_count, 1, "the 60s cache serves repeats")
        self.assertEqual(again, body)
        got = body["moods"][SEAT]
        self.assertEqual(
            {k: got[k] for k in ("state", "reason", "self", "rating",
                                 "blocker", "divergent", "recency")},
            {"state": "stuck", "reason": m["reason"], "self": "fine",
             "rating": 4, "blocker": "fab is down", "divergent": True,
             "recency": "now"})

    def test_a_floor_that_cannot_be_read_is_unavailable_never_empty(self):
        with mock.patch.object(_sm(), "floor",
                               side_effect=OSError("the roster could not "
                                                   "be read")):
            body, status = web.QUERY_API["/api/roster/mood"]({})
        self.assertEqual(status, 200)
        self.assertIs(body.get("unavailable"), True)
        self.assertIn("roster could not be read", body.get("why", ""))


ARMS = r"""
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const m = (state, recency, extra) => Object.assign(
  {state, recency, reason: "3 refusals by author-gate in 20 min", self: null,
   rating: null, blocker: null, divergent: false}, extra || {});
console.log(JSON.stringify({
  stuck_now: moodDot(m("stuck", "now")),
  flowing_today: moodDot(m("flowing", "today")),
  divergent: moodDot(m("stuck", "under 1 h", {self: "fine", rating: 5, divergent: true, blocker: "<b>x</b>"})),
  absent: moodDot(undefined),
  unknown_state: moodDot(m("weird", "now")),
  faces: Object.fromEntries(["flowing", "grinding", "stuck", "blocked-on-owner",
    "walled", "idle", "weird"].map(s => [s, moodDot(m(s, "now"))])),
}));
"""


class MoodPollTest(unittest.TestCase):
    def test_failed_fetch_clears_the_previous_dot_and_repaints(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        poll = _extract_fn(web_ui_loader.read_text(), "pollRosterMood")
        driver = r'''
let ROSTER_MOOD = {}, LAST_ROSTER = {seats: [{seat: "mood-seat"}]};
let fail = false, paints = [];
const $ = () => ({classList: {contains: () => true}});
const document = {hidden: false};
const j = async () => {
  if (fail) throw Error("unavailable");
  return {moods: {"Mood-Seat": {state: "stuck"}}};
};
const renderRoster = () => paints.push(!!ROSTER_MOOD["mood-seat"]);
async function main() {
  await pollRosterMood();
  fail = true;
  await pollRosterMood();
  console.log(JSON.stringify({paints, remaining: Object.keys(ROSTER_MOOD)}));
}
main().catch(e => { console.error(e); process.exitCode = 1; });
'''
        proc = subprocess.run([node, "-e", poll + "\n" + driver],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout),
                         {"paints": [True, False], "remaining": []})


class MoodDotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        fn = _extract_fn(src, "moodFace") + "\n" + _extract_fn(src, "moodDot")
        cls.tmp = tempfile.mkdtemp(prefix="helm-mooddot-")
        path = os.path.join(cls.tmp, "run.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(fn + "\n" + ARMS)
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
        self.assertTrue(self.got, "the node driver produced nothing — "
                        "stderr=%r" % self.proc.stderr[:300])

    def test_one_dot_coloured_by_state_with_the_reason_on_hover(self):
        dot = self.got["stuck_now"]
        self.assertEqual(dot.count("<span"), 1, dot)
        self.assertIn('class="rmood stuck"', dot)
        self.assertIn("stuck · 3 refusals by author-gate in 20 min", dot)
        self.assertIn("now", dot)

    def test_a_stale_seat_is_dim_and_a_gap_is_named(self):
        self.assertIn('class="rmood flowing dim"', self.got["flowing_today"])
        div = self.got["divergent"]
        self.assertIn("says fine 5/5", div)
        self.assertIn("DIVERGES", div)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", div)
        self.assertNotIn("<b>x</b>", div)

    def test_no_reading_draws_nothing_and_an_unknown_state_is_neutral(self):
        self.assertEqual(self.got["absent"], "")
        self.assertIn('class="rmood unknown"', self.got["unknown_state"])

    def test_each_state_draws_its_face_in_the_state_colour(self):
        """task/4034: the dot became a face. Each face is one inline SVG
        inside the one span, painted only in currentColor so the state's
        colour and the dim rule still reach it; its mouth curves up for
        flowing, down for stuck and blocked-on-owner, and is level for the
        rest, an unknown state included."""
        want = {"flowing": "smile", "grinding": "level", "stuck": "frown",
                "blocked-on-owner": "frown", "walled": "level",
                "idle": "level", "weird": "level"}
        faces = self.got["faces"]
        self.assertEqual(set(faces), set(want))
        self.assertEqual(_mouth(faces["flowing"]), "smile")
        self.assertEqual(_mouth(faces["stuck"]), "frown")
        self.assertEqual(_mouth(faces["idle"]), "level")
        self.assertIn('stroke="currentColor"', faces["grinding"])
        for state, kind in want.items():
            html = faces[state]
            self.assertEqual(html.count("<span"), 1, html)
            self.assertEqual(html.count("<svg"), 1, html)
            self.assertNotIn("\u25cf", html)
            self.assertIn('class="rmood %s"' % (
                "unknown" if state == "weird" else state), html)
            self.assertIn('stroke="currentColor"', html)
            self.assertNotRegex(html, r"#[0-9a-fA-F]{3,6}\b",
                                "a hard-coded colour would beat the state's")
            self.assertEqual(_mouth(html), kind, (state, html))

    def test_the_hover_text_is_unchanged_and_stays_escaped(self):
        self.assertIn('title="flowing · 3 refusals by author-gate in 20 min'
                      ' · last sign of work: now"', self.got["faces"]["flowing"])
        self.assertIn('title="mood unknown · 3 refusals by author-gate in 20'
                      ' min · last sign of work: now"',
                      self.got["faces"]["weird"])
        div = self.got["divergent"]
        self.assertIn("blocker: &lt;b&gt;x&lt;/b&gt;", div)
        self.assertNotIn("<b>", div)


def _mouth(html):
    """smile, frown or level, read off the face's one mouth path: a level
    mouth is a straight line, a curved one is a quadratic whose control point
    sits below its ends (a smile; SVG y grows downward) or above them."""
    paths = re.findall(r'<path d="([^"]+)"', html)
    if len(paths) != 1:
        return "paths=%r" % (paths,)
    d = paths[0]
    if re.fullmatch(r"M[\d.]+ [\d.]+H[\d.]+", d):
        return "level"
    q = re.fullmatch(r"M[\d.]+ ([\d.]+)Q[\d.]+ ([\d.]+) [\d.]+ ([\d.]+)", d)
    if not q:
        return "unparsed %r" % d
    y0, cy, y1 = map(float, q.groups())
    return "smile" if cy > max(y0, y1) else "frown" if cy < min(y0, y1) else "flat"


if __name__ == "__main__":
    unittest.main()
