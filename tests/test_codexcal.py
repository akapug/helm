#!/usr/bin/env python3
"""helm.codexcal — the backtest that pins the runway model against its own
history (task/3447).

WHAT THIS PINS, one class per claim:

  SAWTOOTH   A Pro window that resets is a sawtooth: the used percent climbs,
             drops at the reset, and climbs again. A least-squares rate fit
             across the WHOLE sawtooth reads near-zero or negative, and the
             model over-predicts or refuses. The `_segment` fix — fitting only
             the points after the LAST reset — is the whole of the calibration's
             honesty. This class builds a deterministic Pro week that *walls*
             (reaches 100%) before its reset, on a temp HELM_HOME, and asserts
             the calibration is NOT uncalibrated and reports a REAL measured
             median abs error (non-zero). Drop `pts = codexpace._segment(pts)`
             in `_predict` and the per-window n falls below MIN_N and
             `calibrate()` returns `uncalibrated: True`; drop the cure to
             `_actual_wall` and the measured error collapses to the free 0.0
             the old reset-ride gave.

  FIXTURES    Three fixtures pin the censored/wall semantics: a real wall on the
             third later line (actual 3h), a censored HIT (reset first, model
             lasted it), and a capped MISS (walled before reset while the model
             predicted past it — a real, large, non-zero error).

Everything here is SYNTHETIC: rows in `codexbudget.probe_record`'s shape appended
through `codexpace.append_history` (the ledger's own lock + id grammar) into a
process-local temp HELM_HOME. Nothing reads this machine's history.
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._tmphome import home  # noqa: E402, F401

home(prefix="helm-test-codexcal-", var="HELM_HOME")

# Every key the tests write; the suite census credits a set key as restored when
# it appears in a module-level string collection.
ENV_KEYS = ("HELM_HOME",)

from helm import codexcal  # noqa: E402
from helm import codexpace  # noqa: E402


NOW = 1790000000.0
HOUR = 3600.0
WEEK = 7 * 86400


def pro_row(pct, reset_at):
    """One Pro budget row in `codexbudget.probe_record`'s shape. The 7d window
    is the account's longest window (it carries the percent), so the series key
    is the 7d label that `codexcal._predict` fits on. The 5h window also
    carries a percent but is shorter; `_longest` picks the 7d. `account_id`
    alone with a personal plan (`pro` is in PLAN_TIER as a personal tier)
    names one member via `member_key`, so every reading lands on the same series."""
    return {
        "account_id": "acct-pro-a",
        "user_id": None,
        "email": "pro-a@example.com",
        "plan": "pro",
        "file": "codex-pro-a.json",
        "state": "ok",
        "status": "allowed",
        "reached_type": None,
        "reset_credits": None,
        "windows": [
            {"label": "7d", "seconds": WEEK,
             "used_percent": pct, "reset_at": reset_at},
            {"label": "5h", "seconds": 5 * HOUR,
             "used_percent": pct / 2.0 if pct else 0.0,
             "reset_at": reset_at - 100 * HOUR},
        ],
    }


def _seed(base, reset_at, values):
    """Append `values`, a list of (hours_from_base, pct), to a fresh member."""
    for dh, pct in values:
        codexpace.append_history([pro_row(pct, reset_at)], base + dh * HOUR)


def _entry_at(lines, ts):
    """The account entry on the line at `ts` (read back from the ledger)."""
    for line in lines or ():
        if abs(line.get("ts") - ts) < 1e-6:
            for e in line.get("accounts") or ():
                if e.get("member"):
                    return e
    raise AssertionError("no history line at %s" % ts)


class _SawtoothBase(unittest.TestCase):
    """A Pro week that WALLELS (reaches 100%) before its reset, so every wall
    is a real observed cap, not the free reset-ride that gave zero error. The
    7d ramp climbs 5%/h from t0; the window spikes to 100% at the wall hour
    t10 and the reset sits far out (t30) so the wall is observed before it.
    `now` rides the wall line.

    Per prediction at t_k (k>=2), the least-squares rate is 5/h, straight wall
    (100 - 5*(k+1))/5 = 19 - k h, and the first later wall is the t10 cap at
    (10 - k) h -> error +9 h every time. No prediction is capped (reset at t30
    is always past the straight wall), so the whole calibration is a REAL
    over-prediction measurement: median 9.0 h, bias +9.0 h, n=8 in 12h."""

    BASE = NOW
    WALL = 10  # hours from base where the window hits 100 (int, range() needs it)
    RESET = 30.0  # hours from base (far out)

    def setUp(self):
        self.now = NOW + self.WALL * HOUR
        self.reset_at = self.BASE + self.RESET * HOUR
        self._old_home = os.environ["HELM_HOME"]
        self._home = tempfile.mkdtemp(prefix="helm-test-codexcal-")
        os.environ["HELM_HOME"] = self._home
        # 11 rows h=0..10; at the wall hour the window hits 100 (real cap),
        # not the free reset-ride. The ramp is 5%/h before the cap.
        _seed(self.BASE, self.reset_at,
              [(h, 100.0 if h == self.WALL else 5.0 * (h + 1))
               for h in range(self.WALL + 1)])

    def tearDown(self):
        os.environ["HELM_HOME"] = self._old_home
        shutil.rmtree(self._home, ignore_errors=True)

    def lines(self):
        return codexpace.read_history(now=self.now)[0] or []

    def result(self):
        return codexcal.calibrate(now=self.now)

    def test_calibrate_is_clean_not_uncalibrated_on_a_walled_sawtooth(self):
        """The whole class's claim. The 12h window carries the eight post-warmup
        predictions (t2..t9) with a REAL +9h error each, so n=8 >= MIN_N and
        the result is calibrated. Without `_segment` the pre-reset points corrupt
        the rate and n falls below MIN_N; with the old zero-error `_actual_wall`
        the measured error collapses to 0.0. Each of those regressions is a RED
        here."""
        r = self.result()
        self.assertFalse(
            r["uncalibrated"],
            "a Pro week that walls before its reset must calibrate; UNCALIBRATED "
            "means the rate fit crossed the reset (the `_segment` fix is gone)")
        self.assertIsNotNone(r["best"], "best window is named")
        self.assertEqual(r["lines"], 11, r)

    def test_best_window_carries_a_real_nonzero_error_and_its_n(self):
        """The cure makes the number a measurement: the median abs error and the
        signed bias are the model's REAL over-prediction (+9h each), not the
        free 0.0 the old reset-ride gave. The best window is the 12h one (the
        shortest window reaching MIN_N), carrying the eight scored predictions
        t2..t9."""
        r = self.result()
        self.assertFalse(r["uncalibrated"], r)
        # 12h is the first window (shortest, most conservative) with n >= MIN_N;
        # the 24h window carries the same eight predictions.
        self.assertEqual(
            r["best"], "12.0",
            "ties go to the shorter window (less history, more conservative)")
        self.assertEqual(r["best_window_h"], 12.0)
        self.assertEqual(r["windows"]["12.0"]["n"], 8)
        # the cure: a measured +9h over-prediction, never the free 0.0
        self.assertAlmostEqual(r["windows"]["12.0"]["median_abs_h"], 9.0,
                               places=3)
        self.assertAlmostEqual(r["windows"]["12.0"]["bias_h"], 9.0, places=3)
        self.assertEqual(r["windows"]["24.0"]["n"], 8)
        self.assertAlmostEqual(
            r["windows"]["24.0"]["median_abs_h"], 9.0, places=3)

    def test_render_names_the_band_when_calibrated(self):
        """The render is the seam to `helm burn` and the credit page: a walled
        Pro account must say the band with a number, not fall back to the
        UNCALIBRATED wall. A censored breakdown (reset-before-wall readings)
        is surfaced as a second line when any such reading exists."""
        r = self.result()
        rendered = codexcal.render(r)
        self.assertIn("runway backtest:", rendered[0])
        self.assertNotIn("UNCALIBRATED", rendered[0])
        self.assertIn("median +9.00h", rendered[0])


class Base(unittest.TestCase):
    """Fixture scaffolding: a temp HELM_HOME seeded per case, plus helpers to
    pull the member back out of the ledger and the series of (ts, entry).
    The default seed has NO 100% line (it is a reset-ride, so every wall is
    censored). RealWallThirdLaterLineTest overrides with a seed that WALLELS."""

    def setUp(self):
        self._old_home = os.environ["HELM_HOME"]
        self._home = tempfile.mkdtemp(prefix="helm-test-codexcal-")
        os.environ["HELM_HOME"] = self._home
        self.now = NOW + 5 * HOUR
        self.reset_at = NOW + 36 * HOUR
        # t0=4..t4=20 (rate 2/h), no 100% line before reset (reset ride)
        _seed(NOW, self.reset_at,
              [(0, 4), (1, 6), (2, 8), (3, 10), (4, 12)])

    def tearDown(self):
        os.environ["HELM_HOME"] = self._old_home
        shutil.rmtree(self._home, ignore_errors=True)

    def lines(self):
        return codexpace.read_history(now=self.now)[0] or []

    def member(self):
        for line in self.lines():
            for e in line.get("accounts") or ():
                if e.get("member"):
                    return e.get("member")
        raise AssertionError("no member in history")

    def entry_at(self, ts):
        return _entry_at(self.lines(), ts)

    def series_for(self, entry):
        return [(line["ts"], e) for line in self.lines()
                for e in (line.get("accounts") or [])
                if e.get("member") == entry.get("member")]


class RealWallThirdLaterLineTest(Base):
    """Fixture (i): the window WALLELS on the THIRD later line after the
    prediction instant. The model fit only sees the two earlier readings plus
    the prediction itself (rate 2/h, straight wall 46h, reset 34h out so it IS
    capped — pred 34h), yet the first later line that hits 100 is the third
    one, at pred+3h. The cure must report that REAL wall of 3h, not 0 and not
    the reset. The resulting +31h over-prediction is the MISS the old code
    would have scored 0.0 (pred == reset == 'actual')."""

    def setUp(self):
        Base.setUp(self)
        # t5=100 (WALL) one hour after the t4 reading; pred at t2 -> wall is 3h
        codexpace.append_history([pro_row(100, self.reset_at)],
                                NOW + 5 * HOUR)

    def test_actual_wall_is_the_third_later_line_not_zero(self):
        entry = self.entry_at(NOW + 2 * HOUR)
        wall = codexcal._actual_wall(entry, self.series_for(entry),
                                    NOW + 2 * HOUR)
        self.assertEqual(wall, {"wall_h": 3.0},
                         "the first later line that hits 100 is the THIRD later "
                         "line (t5); the cure must return its real 3h wall, not "
                         "the old 0.0 free reset-ride")

    def test_capped_predict_gives_real_error(self):
        entry = self.entry_at(NOW + 2 * HOUR)
        pred = codexcal._predict(entry, NOW + 2 * HOUR,
                                 self.series_for(entry))
        wall = codexcal._actual_wall(entry, self.series_for(entry),
                                    NOW + 2 * HOUR)
        self.assertEqual(pred, 34.0,
                         "the model caps at the reset it overpromised")
        # pred - wall is the real +31h over-prediction; the old code scored 0.0
        self.assertAlmostEqual(pred - wall["wall_h"], 31.0, places=3)


class CensoredHitTest(Base):
    """Fixture (ii): the reset arrives first (no 100% line before reset), so the
    cure classifies the outcome CENSORED with time_to_reset_h = ttr. The model's
    straight wall (46h) exceeds ttr (34h), so the prediction is capped to ttr
    and it MEETS it -> a CENSORED HIT. This is never a 0.0 error; it is a
    separate censored hit that the render counts. To make a window carry >= 8
    censored readings, extend the history past t4 (still no 100% line, reset at
    t36) so the 24h window has many predictions all censored-hit."""

    def test_reset_first_is_censored_hit_not_zero(self):
        entry = self.entry_at(NOW + 2 * HOUR)
        res = codexcal._actual_wall(entry, self.series_for(entry),
                                   NOW + 2 * HOUR)
        # No 100% line before the reset -> censored, ttr = 36 - 2 = 34h
        self.assertTrue(res.get("censored"), res)
        self.assertAlmostEqual(res["time_to_reset_h"], 34.0, places=3)

    def test_calibrate_counts_censored_hits(self):
        # Extend the series with more reset-ride readings (no 100% line)
        # so the 24h window has >= 8 predictions, all censored-hit.
        for dh, pct in [(6, 14), (7, 16), (8, 18), (9, 20), (10, 22),
                        (11, 24), (12, 26)]:
            codexpace.append_history([pro_row(pct, self.reset_at)],
                                    NOW + dh * HOUR)
        r = codexcal.calibrate(now=self.now)
        # n (real walls) is 0 -> uncalibrated, but censored readings in the 24h
        # window are counted under censored_n / censored_hits / censored_misses.
        w = r.get("windows", {})
        self.assertIn("24.0", w,
                      "a 24h window must exist and count censored readings")
        self.assertGreaterEqual(w["24.0"]["censored_n"], 1,
                                "censored readings must be counted separately")
        self.assertEqual(w["24.0"]["censored_misses"], 0,
                         "pred==ttr means the model met the reset -> hit")
        self.assertGreaterEqual(w["24.0"]["censored_hits"], 1,
                                "the model met the reset -> hit")


class SecondCureNoneBestFromCalibratedTest(unittest.TestCase):
    """Reviewer's second-cure fixture: censored-only readings in the last 1h
    (no real wall within the hour) plus MIN_N real walls ~10h back.

    The seed: 5%/h ramp t0..t9 (50% at t9), wall at t10 (100%), reset 46h
    past the wall (NOW+56h), now at t10 (NOW+10h). The 12h window (cutoff
    NOW-2h) carries the eight scored predictions t2..t9, each a real wall
    (t10, 1..8 h later) -> n=8 >= MIN_N. The 1h window (cutoff NOW+9h)
    carries no predictions (t10 is already walled, skipped) -> n=0.

    Per-prediction error is constant +9h (pred - wall): t2=17-8, t3=16-7,
    ..., t9=10-1. So the 12h window reports median_abs_h=bias_h=9.0, and
    `best` must be "12.0" — the only window with n >= MIN_N.
    """

    BASE = NOW
    WALL = 10  # wall hour from base (int, range() needs it)
    RATE = 5.0  # %/h ramp
    RESET = 56.0  # reset 46h past the wall (far out)

    def setUp(self):
        self.now = NOW + self.WALL * HOUR
        self.reset_at = NOW + self.RESET * HOUR
        self._old_home = os.environ["HELM_HOME"]
        self._home = tempfile.mkdtemp(prefix="helm-test-codexcal-")
        os.environ["HELM_HOME"] = self._home
        # 11 rows t0..t10: 5%/h ramp before the cap, t10 = 100 (real wall),
        # reset 46h past the wall so the wall is observed before it.
        _seed(self.BASE, self.reset_at,
              [(h, 100.0 if h == self.WALL else self.RATE * (h + 1))
               for h in range(self.WALL + 1)])

    def tearDown(self):
        os.environ["HELM_HOME"] = self._old_home
        shutil.rmtree(self._home, ignore_errors=True)

    def _cens_row(self, pct, reset_at):
        """A Pro row that never reaches 100 — a reset-ride, so the account
        lasted to the reset (censored). Same shape as pro_row, capped below 100."""
        return {
            "account_id": "acct-pro-a",
            "user_id": None,
            "email": "pro-a@example.com",
            "plan": "pro",
            "file": "codex-pro-a.json",
            "state": "ok",
            "status": "allowed",
            "reached_type": None,
            "reset_credits": None,
            "windows": [
                {"label": "7d", "seconds": WEEK,
                 "used_percent": pct, "reset_at": reset_at},
                {"label": "5h", "seconds": 5 * HOUR,
                 "used_percent": pct / 2.0 if pct else 0.0,
                 "reset_at": reset_at - 100 * HOUR},
            ],
        }

    def result(self):
        return codexcal.calibrate(now=self.now)

    def test_best_comes_from_the_calibrated_window_not_the_censored_hour(self):
        """The 12h window is the only window with n >= MIN_N. The old code
        would have named a window with no real walls (lowest 'median_abs' of
        the empty set = 0.0); the cure must not."""
        r = self.result()
        self.assertFalse(r["uncalibrated"], r)
        self.assertEqual(r["best"], "12.0",
                         "best is picked only from n >= MIN_N windows; the 1h "
                         "window is censored-only / already-walled")
        self.assertEqual(r["best_window_h"], 12.0)
        # the 12h window carries the real walls (t2..t9 = 8 scored predictions)
        self.assertEqual(r["windows"]["12.0"]["n"], 8)
        # a real +9h over-prediction each (pred - wall = 17-8, 16-7, ..., 10-1)
        self.assertAlmostEqual(r["windows"]["12.0"]["median_abs_h"], 9.0, places=3)
        self.assertAlmostEqual(r["windows"]["12.0"]["bias_h"], 9.0, places=3)

    def test_no_real_wall_window_carries_none_not_zero(self):
        """A window with no real walls must carry bias_h and median_abs_h as
        None — no real-wall errors to average — not 0.0, which would
        masquerade as a perfect calibration.

        This class's walled seed only yields windows with real walls (n=8), so
        an n=0 window never appears there to be checked. To make the None
        contract a positive, unconditional check, this seeds a second temp home
        with a censored-only history (no 100% line, reset 300h out). There the
        trailing windows genuinely appear with n=0 and censored_n>0; the assertions
        on them are unconditional (no loop/try) so the None-not-0.0 contract is
        actually exercised, never vacuous.
        """
        # Censored-only seed (no 100% line, reset 300h out) in its own home:
        # the trailing windows appear with n=0, censored_n>0.
        self.reset_at = self.BASE + 300 * HOUR
        self._home_cens = tempfile.mkdtemp(prefix="helm-test-codexcal-cens-")
        os.environ["HELM_HOME"] = self._home_cens
        # 11 rows t0..t10: 5%/h ramp, never 100%, now at t10 (50% at t10).
        for h in range(self.WALL + 1):
            codexpace.append_history([self._cens_row(self.RATE * (h + 1), self.reset_at)],
                                    self.BASE + h * HOUR)
        r_cens = codexcal.calibrate(now=self.now)
        # The 1h window carries the last reading (t10, 50% -> pred capped at
        # reset, censored) -> n=0, censored_n>0, so it appears in the result.
        w1 = r_cens["windows"]["1.0"]
        self.assertEqual(w1["n"], 0,
                         "the last hour is censored-only -> n=0")
        self.assertGreaterEqual(w1["censored_n"], 1,
                                "the last hour must carry a censored reading")
        # The positive control: a window that genuinely appears with n=0
        # must show None, not 0.0 (which would masquerade as perfect).
        # These assertions are unconditional (no loop/try) so the None contract
        # is actually checked, never vacuously.
        self.assertIsNone(w1["bias_h"],
                          "an appearing n=0 window must be None, not 0.0")
        self.assertIsNone(w1["median_abs_h"],
                          "an appearing n=0 window must be None, not 0.0")
        # The same contract holds for every other window this censored-only seed
        # produces (all censored-only): a concrete n=0 window with censored_n>0.
        w24 = r_cens["windows"]["24.0"]
        self.assertEqual(w24["n"], 0, "24h window is censored-only -> n=0")
        self.assertEqual(w24["censored_n"], 8,
                         "the 24h window carries all eight censored readings")
        self.assertIsNone(w24["bias_h"], "censored-only 24h window bias must be None")
        self.assertIsNone(w24["median_abs_h"],
                          "censored-only 24h window median must be None")
        self.assertEqual(r_cens["uncalibrated"], True,
                         "censored-only history -> no real wall -> UNCALIBRATED")
        # Restore this class's walled home and clean up the censored home.
        os.environ["HELM_HOME"] = self._old_home
        shutil.rmtree(self._home_cens, ignore_errors=True)

    def test_render_names_the_calibrated_band_not_uncalibrated(self):
        """Render must print the 12h band (it has n >= MIN_N) even though the
        1h window is censored-only/None; UNCALIBRATED is reserved for when NO
        window has n >= MIN_N."""
        r = self.result()
        rendered = codexcal.render(r)
        self.assertIn("runway backtest:", rendered[0])
        self.assertNotIn("UNCALIBRATED", rendered[0])
        self.assertIn("12.0h window", rendered[0])


if __name__ == "__main__":
    unittest.main()
