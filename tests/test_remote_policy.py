#!/usr/bin/env python3
"""What a remote read counts as (helm/remote_policy.py), and the credit and
pace that gate a launch (helm/remote_credit.py).

EVERY CELL OF THE ONE TABLE is asserted by value, and every guard is fed the
input that must trip it beside the one that must not: the classifier a lane
that touches each irreversible class and one that touches none, each
falsifier the evidence that reverts the arm and the evidence that is one short
of it, the credit gate an account at its floor and one above it.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helm import pk, remote_credit, remote_policy as rp  # noqa: E402

ENV_KEYS = (rp.SWITCH_ENV, remote_credit.FLOOR_ENV, remote_credit.BUDGET_ENV)


class TableTest(unittest.TestCase):

    def test_every_cell_by_value(self):  # noqa: VACUOUS_ASSERTION — the table is first asserted equal to eight named cells, so the loop runs eight times
        want = {
            (rp.CROSS_MODEL, rp.REVERSIBLE, rp.ARM_ON): rp.REVIEW_LEG,
            (rp.CROSS_MODEL, rp.REVERSIBLE, rp.ARM_OFF): rp.REVIEW_LEG,
            (rp.CROSS_MODEL, rp.IRREVERSIBLE, rp.ARM_ON): rp.REVIEW_LEG,
            (rp.CROSS_MODEL, rp.IRREVERSIBLE, rp.ARM_OFF): rp.REVIEW_LEG,
            (rp.SAME_MODEL, rp.REVERSIBLE, rp.ARM_ON): rp.REVIEW_LEG,
            (rp.SAME_MODEL, rp.REVERSIBLE, rp.ARM_OFF): rp.CONCUR,
            (rp.SAME_MODEL, rp.IRREVERSIBLE, rp.ARM_ON): rp.CONCUR,
            (rp.SAME_MODEL, rp.IRREVERSIBLE, rp.ARM_OFF): rp.CONCUR,
        }
        self.assertEqual(set(rp.TABLE), set(want))
        for cell, klass in want.items():
            got = rp.classify("claude-opus-5-5", *cell)
            self.assertEqual(got["class"], klass, cell)
            if klass == rp.CONCUR:
                self.assertIn("different-model", got["owed"], cell)
            else:
                self.assertIsNone(got["owed"], cell)

    def test_sonnet_and_haiku_never_review_in_any_arm(self):
        for model in ("claude-sonnet-4-5", "sonnet", "claude-3-5-haiku",
                      "HAIKU"):
            for cell in rp.TABLE:
                self.assertEqual(rp.classify(model, *cell)["class"], rp.REFUSED,
                                 (model, cell))
        self.assertEqual(rp.classify(None, *next(iter(rp.TABLE)))["class"],
                         rp.REFUSED)

    def test_a_key_outside_the_table_refuses(self):
        self.assertEqual(rp.classify("opus", "sideways", rp.REVERSIBLE,
                                     rp.ARM_ON)["class"], rp.REFUSED)

    def test_the_relation(self):
        self.assertEqual(rp.relation("claude-opus-5-5", "opus")[0], rp.SAME_MODEL)
        self.assertEqual(rp.relation("claude-opus-5-5", "claude-opus-5-4[1m]")[0],
                         rp.SAME_MODEL)
        self.assertEqual(rp.relation("claude-opus-5-5", "claude-fable-5")[0],
                         rp.CROSS_MODEL)
        self.assertEqual(rp.relation("claude-opus-5-5", "gpt-5.5")[0],
                         rp.CROSS_MODEL)
        self.assertEqual(rp.relation("claude-opus-5-5", None, "codex")[0],
                         rp.CROSS_MODEL)
        # UNKNOWN is the stricter arm, never the looser one
        self.assertEqual(rp.relation("claude-opus-5-5", None, "claude")[0],
                         rp.SAME_MODEL)
        self.assertEqual(rp.relation("claude-opus-5-5")[0], rp.SAME_MODEL)

    def test_the_switch_defaults_to_the_ruling(self):
        self.assertEqual(rp.arm_switch({}), rp.ARM_ON)
        self.assertEqual(rp.arm_switch({rp.SWITCH_ENV: "on"}), rp.ARM_ON)
        for off in ("off", "0", "concur", "OFF"):
            self.assertEqual(rp.arm_switch({rp.SWITCH_ENV: off}), rp.ARM_OFF)


class ReversibilityTest(unittest.TestCase):
    """Ruling 2 (a)-(d), from the diff and the brief, failing toward owed."""

    def rev(self, paths, added=None, brief="", doors=()):
        return rp.reversibility(paths, added or {}, brief, doors)

    def test_an_ordinary_lane_is_reversible(self):
        lane, hits = self.rev(["helm/render.py", "tests/test_render.py"],
                              {"helm/render.py": ["return text.strip()"]},
                              "tidy the renderer")
        self.assertEqual((lane, hits), (rp.REVERSIBLE, []))

    def test_each_class_makes_it_irreversible(self):  # noqa: VACUOUS_ASSERTION — the case list is a non-empty literal and every case asserts IRREVERSIBLE plus its class letter
        cases = [
            ("a", ["db/migrations/0042_add.py"], {}),
            ("a", ["helm/store.py"], {"helm/store.py": ["shutil.rmtree(path)"]}),
            ("a", ["app/pay.py"], {"app/pay.py": ["charge(stripe_key)"]}),
            ("b", ["helm/reaper.py"], {}),
            ("b", ["helm/x.py"], {"helm/x.py": ["os.kill(pid, signal.SIGTERM)"]}),
            ("c", ["helm/x.py"], {"helm/x.py": ["run(['git', 'push', '--force'])"]}),
            ("d", ["helm/stopguard.py"], {}),
            ("d", ["hooks/pre-commit"], {}),
        ]
        for letter, paths, added in cases:
            lane, hits = self.rev(paths, added)
            self.assertEqual(lane, rp.IRREVERSIBLE, (letter, paths))
            self.assertTrue(any(h.startswith("(%s)" % letter) for h in hits),
                            (letter, hits))

    def test_a_declared_door_and_the_brief_count(self):
        lane, hits = self.rev(["helm/dispatches.py"], {},
                              doors=("helm/dispatches*.py",))
        self.assertEqual(lane, rp.IRREVERSIBLE)
        self.assertIn("declared door", hits[0])
        lane, _hits = self.rev(["helm/render.py"], {}, "this runs the migration")
        self.assertEqual(lane, rp.IRREVERSIBLE)

    def test_tests_and_docs_are_not_live_systems(self):
        lane, _hits = self.rev(
            ["tests/test_kill.py", "docs/RELEASE.md"],
            {"tests/test_kill.py": ["os.kill(pid, 9)"],
             "docs/RELEASE.md": ["git push --force"]})
        self.assertEqual(lane, rp.REVERSIBLE)

    def test_an_unread_diff_fails_toward_owed(self):
        self.assertEqual(self.rev(None)[0], rp.IRREVERSIBLE)


class FalsifierTest(unittest.TestCase):

    DAY = 86400

    def test_calibration_below_half_trips_and_at_half_does_not(self):
        low = [{"found": 2, "of": 6}, {"found": 1, "of": 4}] * 3
        trip = rp.calibration_trip(low)
        self.assertTrue(trip[0])
        self.assertAlmostEqual(trip[1], 0.3)
        self.assertFalse(rp.calibration_trip([{"found": 3, "of": 6},
                                              {"found": 2, "of": 4}] * 3)[0])
        # another sample, a zero-size read and a nonsense read are not evidence
        self.assertFalse(rp.calibration_trip([{"found": 0, "of": 9,
                                               "sample": "pilot"},
                                              {"found": 0, "of": 0},
                                              {"found": 5, "of": 2}])[0])
        self.assertFalse(rp.calibration_trip([])[0])

    def test_calibration_needs_a_minimum_sample_before_it_trips(self):
        """task/3517: one read below half tripped the arm. Below
        CALIBRATION_MIN_READS reads the share is reported and never trips;
        at the minimum the same share trips."""
        self.assertEqual(rp.CALIBRATION_MIN_READS, 5)
        read = {"found": 0, "of": 4}
        for n in range(1, rp.CALIBRATION_MIN_READS):
            tripped, share, detail = rp.calibration_trip([read] * n)
            self.assertFalse(tripped, n)
            self.assertEqual(share, 0.0)
            self.assertIn("minimum", detail)
        self.assertTrue(rp.calibration_trip(
            [read] * rp.CALIBRATION_MIN_READS)[0])

    def test_two_cross_family_contradictions_within_a_week_trip(self):
        approves = [{"tip": "t1", "ts": 100}, {"tip": "t2", "ts": 200},
                    {"tip": "t3", "ts": 300}]
        fixes = [{"tip": "t1", "ts": 1000, "family": "codex"},
                 {"tip": "t2", "ts": 1000 + 6 * self.DAY, "family": "kimi"}]
        found = rp.contradictions(approves, fixes)
        self.assertEqual([c["tip"] for c in found], ["t1", "t2"])
        self.assertTrue(rp.contradiction_trip(found)[0])

    def test_what_does_not_count(self):  # noqa: VACUOUS_ASSERTION — the trip arm above is the positive control on the same observable; each case here is one input short of it
        approves = [{"tip": "t1", "ts": 100}, {"tip": "t2", "ts": 200}]
        cases = {
            "a week apart": [{"tip": "t1", "ts": 1000, "family": "codex"},
                             {"tip": "t2", "ts": 1000 + 8 * self.DAY,
                              "family": "codex"}],
            "same family": [{"tip": "t1", "ts": 1000, "family": "claude"},
                            {"tip": "t2", "ts": 1100, "family": "codex"}],
            "unknown family": [{"tip": "t1", "ts": 1000, "family": None},
                               {"tip": "t2", "ts": 1100, "family": "codex"}],
            "before the approve": [{"tip": "t1", "ts": 50, "family": "codex"},
                                   {"tip": "t2", "ts": 1100, "family": "codex"}],
            "another tip": [{"tip": "t9", "ts": 1000, "family": "codex"},
                            {"tip": "t2", "ts": 1100, "family": "codex"}],
        }
        for name, fixes in cases.items():
            found = rp.contradictions(approves, fixes)
            self.assertFalse(rp.contradiction_trip(found)[0], name)


class CreditTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-remote-credit-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        self.now = time.time()

    def tearDown(self):
        for key, value in self.prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def reading(self, account, left, limit=250.0, used=None, age_s=0,
                expires_days=40):
        return {"account": account, "left": left, "limit": limit,
                "used": used if used is not None else limit - left,
                "at": pk.epoch_ts(self.now - age_s),
                "expires": pk.epoch_ts(self.now + expires_days * 86400)}

    def test_the_first_account_above_the_floor_pays(self):
        accounts = [{"email": "one@example.com"}, {"email": "two@example.com"}]
        live = {"one@example.com": self.reading("one@example.com", 4.0),
                "two@example.com": self.reading("two@example.com", 200.0)}
        chosen, reading, refusals = remote_credit.choose_account(
            accounts, lambda a: live[a["email"]], [], 5.0, self.now)
        self.assertEqual(chosen["email"], "two@example.com")
        self.assertEqual(reading["basis"], "live")
        self.assertIn("at or below the $5.00 floor", refusals[0])

    def test_every_account_at_the_floor_refuses(self):
        accounts = [{"email": "one@example.com"}]
        chosen, _r, refusals = remote_credit.choose_account(
            accounts, lambda a: self.reading("one@example.com", 5.0), [], 5.0,
            self.now)
        self.assertIsNone(chosen)
        self.assertEqual(len(refusals), 1)

    def test_an_unreadable_account_uses_a_fresh_memory_or_is_skipped(self):
        accounts = [{"email": "one@example.com"}]
        dead = lambda a: {"error": "no live access token"}  # noqa: E731
        fresh = [self.reading("one@example.com", 120.0, age_s=3600)]
        chosen, reading, _ = remote_credit.choose_account(accounts, dead, fresh,
                                                          5.0, self.now)
        self.assertEqual(chosen["email"], "one@example.com")
        self.assertIn("remembered", reading["basis"])
        stale = [self.reading("one@example.com", 120.0, age_s=2 * 86400)]
        chosen, _r, refusals = remote_credit.choose_account(accounts, dead,
                                                            stale, 5.0, self.now)
        self.assertIsNone(chosen)
        self.assertIn("unreadable", refusals[0])

    def test_an_account_with_no_credit_block_is_skipped(self):
        chosen, _r, refusals = remote_credit.choose_account(
            [{"email": "x@example.com"}],
            lambda a: {"account": "x@example.com", "left": None, "limit": None,
                       "at": pk.now_ts()}, [], 5.0, self.now)
        self.assertIsNone(chosen)
        self.assertIn("no cloud-session credit", refusals[0])

    def test_the_budget_knob(self):
        """No daily cap unless the operator sets one: the owner never asked
        for promo credit to be rationed per day, and an unset knob that
        spread the credit to its expiry refused launches he wanted."""
        readings = [self.reading("one@example.com", 200.0, expires_days=40),
                    self.reading("two@example.com", 100.0, expires_days=10)]
        budget, why = remote_credit.daily_budget(readings, self.now, {})
        self.assertIsNone(budget)
        self.assertIn("no daily cap", why)
        budget, _why = remote_credit.daily_budget(
            readings, self.now, {remote_credit.BUDGET_ENV: "auto"})
        self.assertAlmostEqual(budget, 200.0 / 40 + 100.0 / 10, places=2)
        self.assertEqual(remote_credit.daily_budget(
            readings, self.now, {remote_credit.BUDGET_ENV: "7.5"})[0], 7.5)
        self.assertEqual(remote_credit.daily_budget(
            readings, self.now, {remote_credit.BUDGET_ENV: "0"})[0], 0.0)
        self.assertIsNone(remote_credit.daily_budget([], self.now, {})[0])

    def test_a_scarce_max_pool_replaces_the_spread_with_a_ceiling(self):
        """task/3517: while the anthropic flag is ORANGE or RED, expiring promo
        credit spends FIRST: `auto` stops spreading it to expiry and caps the
        day at a generous ceiling instead. A fixed number and the unset knob
        are the operator's own answers and are not changed."""
        readings = [self.reading("one@example.com", 200.0, expires_days=40)]
        auto = {remote_credit.BUDGET_ENV: "auto"}
        spread, _why = remote_credit.daily_budget(readings, self.now, auto)
        self.assertAlmostEqual(spread, 5.0, places=2)
        budget, why = remote_credit.daily_budget(readings, self.now, auto,
                                                 scarce="ORANGE")
        self.assertEqual(budget, remote_credit.DEFAULT_SCARCE_CEILING_USD)
        self.assertIn("ORANGE", why)
        self.assertGreater(remote_credit.DEFAULT_SCARCE_CEILING_USD, spread)
        own = dict(auto, **{remote_credit.SCARCE_CEILING_ENV: "40"})
        self.assertEqual(remote_credit.daily_budget(
            readings, self.now, own, scarce="RED")[0], 40.0)
        self.assertEqual(remote_credit.daily_budget(
            readings, self.now, {remote_credit.BUDGET_ENV: "7.5"},
            scarce="RED")[0], 7.5)
        self.assertIsNone(remote_credit.daily_budget(
            readings, self.now, {}, scarce="RED")[0])
        # the floor still holds while the pool is scarce
        chosen, _r, refusals = remote_credit.choose_account(
            [{"email": "one@example.com"}],
            lambda a: self.reading("one@example.com", 4.0), [], 5.0, self.now)
        self.assertIsNone(chosen)
        self.assertIn("floor", refusals[0])

    def test_the_scarce_reading_is_the_cached_anthropic_flag(self):
        from helm import burnflags
        for colour, want in (("ORANGE", "ORANGE"), ("RED", "RED"),
                             ("YELLOW", None), ("GREEN", None)):
            with mock.patch.object(burnflags, "family_flag",
                                   return_value={"colour": colour}) as asked:
                self.assertEqual(remote_credit.max_pool_scarce(), want, colour)
            asked.assert_called_once_with(burnflags.NATIVE_FAMILY)
        with mock.patch.object(burnflags, "family_flag", return_value=None):
            self.assertIsNone(remote_credit.max_pool_scarce())
        with mock.patch.object(burnflags, "family_flag",
                               side_effect=OSError("unreadable")):
            self.assertIsNone(remote_credit.max_pool_scarce())

    def test_spent_today_is_measured_from_the_readings(self):  # noqa: VACUOUS_ASSERTION — asserts one exact measured value, 4.0, from planted readings
        day0 = self.now - (self.now % 86400)
        rows = [{"account": "a@example.com", "used": 10.0,
                 "at": pk.epoch_ts(day0 - 3600)},
                {"account": "a@example.com", "used": 13.5,
                 "at": pk.epoch_ts(day0 + 60)},
                {"account": "a@example.com", "used": 14.0,
                 "at": pk.epoch_ts(min(self.now, day0 + 120))},
                {"account": "b@example.com", "error": "HTTP 401",
                 "at": pk.epoch_ts(day0 + 60)}]
        self.assertAlmostEqual(remote_credit.spent_today(rows, self.now), 4.0)

    def test_flat_for_reads_how_long_the_account_spent_nothing(self):
        t = self.now - 10000
        rows = [{"account": "a@example.com", "used": 5.0, "at": pk.epoch_ts(t)},
                {"account": "a@example.com", "used": 6.0,
                 "at": pk.epoch_ts(t + 600)},
                {"account": "a@example.com", "used": 6.0,
                 "at": pk.epoch_ts(t + 1200)},
                {"account": "b@example.com", "used": 1.0,
                 "at": pk.epoch_ts(t + 1300)},
                {"account": "a@example.com", "error": "HTTP 429",
                 "at": pk.epoch_ts(t + 2000)},
                {"account": "a@example.com", "used": 6.0,
                 "at": pk.epoch_ts(t + 2400)}]
        self.assertEqual(remote_credit.flat_for(rows, "a@example.com", t), 1800)
        self.assertEqual(remote_credit.flat_for(rows, "A@example.com", t + 1000),
                         1200)
        self.assertIsNone(remote_credit.flat_for(rows, "a@example.com", t + 1300))
        self.assertIsNone(remote_credit.flat_for([], "a@example.com", 0))

    def test_the_floor_knob(self):
        self.assertEqual(remote_credit.floor_usd({}), remote_credit.DEFAULT_FLOOR_USD)
        self.assertEqual(remote_credit.floor_usd({remote_credit.FLOOR_ENV: "20"}),
                         20.0)
        self.assertEqual(remote_credit.floor_usd({remote_credit.FLOOR_ENV: "x"}),
                         remote_credit.DEFAULT_FLOOR_USD)

    def test_a_reading_never_carries_the_token(self):
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        with open(os.path.join(home, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {"accessToken": "tok-SECRET-VALUE",
                                         "expiresAt": int((self.now + 3600)
                                                          * 1000)}}, f)
        with open(os.path.join(home, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"emailAddress": "one@example.com"}}, f)
        seen = []

        def get_json(url, headers):
            seen.append(headers)
            return {remote_credit.CREDIT_KEY: {
                "limit_dollars": 250, "used_dollars": 4.27,
                "remaining_dollars": 245.73,
                "resets_at": "2026-11-05T07:59:00+00:00"}}
        r = remote_credit.read_credit(home, get_json)
        self.assertEqual((r["left"], r["limit"], r["account"]),
                         (245.73, 250, "one@example.com"))
        self.assertIn("tok-SECRET-VALUE", seen[0]["Authorization"])
        self.assertNotIn("tok-SECRET-VALUE", json.dumps(r))
        # a dead token is never presented
        with open(os.path.join(home, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {"accessToken": "tok-SECRET-VALUE",
                                         "expiresAt": 1000}}, f)
        r = remote_credit.read_credit(home, get_json)
        self.assertIn("no live access token", r["error"])
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
