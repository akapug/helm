#!/usr/bin/env python3
"""helm.codexpace — percent per hour per codex account, and the fleet runway.

WHAT THESE PIN, one class per claim the module makes:

  RATE     a synthetic budget history yields the slope it was built with, on
           the account's LONGEST window, and only the segment after the last
           reset counts;
  UNKNOWN  too few readings, or too short a span, is UNKNOWN and never 0;
  MEMBER   a credential whose member is not proven (a Team record with no
           user id) keeps its own series, never lends its numbers to a
           sibling on the same workspace, and is left out of the fleet supply;
  RUNWAY   supply over rate, set against the next Pro reset, and the fold's
           step table moves the codex money axis UP when the runway is short
           and DOWN ("use it") when it is long and an account will strand —
           never to RED on a projection, never to GREEN past an unread
           account;
  CREDITS  each held reset credit valued by the reset clock: a Pro one on
           the horizon account adds nothing before the horizon, one on an
           account resetting past it counts a fresh week, a Team one counts
           its week wherever its reset lies (a workspace credits-depleted
           wall included), and an unknown weight or balance is UNKNOWN,
           never 0;
  WALL     one chat line and one phone push per wall, never twice, with a
           failed channel retried alone.

Every world here is SYNTHETIC: rows in `codexbudget.probe_record`'s shape,
ledger events in `proxy_usage.request_event`'s shape, example.com addresses.
Nothing reads this machine's history, ledger or snapshots.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-codexpace-", var="HELM_HOME")

from helm import burnflags, codexpace  # noqa: E402

NOW = 1790000000.0
HOUR = 3600.0
WEEK = 7 * 86400


def brow(email, pct, reset_at, plan="pro", account_id=None, user_id="user",
         file=None, five=12.0, state=None, credits=0, reached=None):
    """One budget row in `codexbudget.probe_record`'s shape. `pct` None is an
    account the probe could not read. `credits` is the HELD reset-credit
    count the usage body carried (None: the body carried no balance), and
    `reached` the vendor's reached-type."""
    account_id = account_id or "acct-" + email.split("@")[0]
    user_id = None if user_id is None else user_id + "-" + email.split("@")[0]
    file = file or "codex-%s.json" % email.split("@")[0]
    if pct is None:
        return {"account_id": account_id, "user_id": user_id, "email": email,
                "plan": plan, "file": file, "files": [file], "state": "unknown",
                "status": "network-error", "reached_type": None,
                "longest_pct": None, "windows": []}
    windows = [{"label": "7d", "seconds": WEEK, "used_percent": pct,
                "reset_at": reset_at, "reset_after_seconds": 0},
               {"label": "5h", "seconds": 5 * 3600, "used_percent": five,
                "reset_at": reset_at - 100 * HOUR, "reset_after_seconds": 0}]
    return {"account_id": account_id, "user_id": user_id, "email": email,
            "plan": plan, "file": file, "files": [file],
            "state": state or ("exhausted" if pct >= 100 else "ok"),
            "status": "blocked" if pct >= 100 else "allowed",
            "reached_type": reached, "longest_pct": pct, "windows": windows,
            "reset_credits": (None if credits is None else
                              {"available": credits, "applicable": 0})}


def event(email, at, tokens, seat="seat-a", family="codex"):
    """One ledger request in `proxy_usage.request_event`'s shape."""
    return {"v": 1, "kind": "request", "family": family, "seat": seat,
            "source": email, "at": at, "failed": False,
            "token_breakdown": {"input": {"total_tokens": int(tokens)},
                                "output": {"total_tokens": 0}}}


def world(specs, now=NOW, hours=6.0, step=900.0):
    """(history lines, ledger requests) for accounts that each rise at a
    fixed rate and each draw a fixed token rate.

    spec: {email, start, rate (%/h), reset_h, plan, tokens_h, seat, ...}.
    The vendor's integer percent is reproduced by rounding every reading."""
    lines, requests = [], []
    t0 = now - hours * HOUR
    t = t0
    while t <= now + 1e-6:
        rows = []
        for s in specs:
            pct = min(100.0, round(s["start"] + s["rate"] * (t - t0) / HOUR))
            rows.append(brow(s["email"], pct, now + s["reset_h"] * HOUR,
                             plan=s.get("plan", "pro"),
                             account_id=s.get("account_id"),
                             user_id=s.get("user_id", "user"),
                             file=s.get("file"),
                             credits=s.get("credits", 0),
                             reached=s.get("reached")))
        lines.append(codexpace.history_line(rows, t))
        t += step
    tick = 300.0
    t = t0 + tick
    while t <= now + 1e-6:
        for s in specs:
            if s.get("tokens_h"):
                requests.append(event(s["email"], t, s["tokens_h"] * tick / HOUR,
                                      seat=s.get("seat", "seat-a")))
        t += tick
    return lines, requests


def rows_at(specs, now=NOW, hours=6.0):
    return [brow(s["email"],
                 min(100.0, round(s["start"] + s["rate"] * hours)),
                 now + s["reset_h"] * HOUR, plan=s.get("plan", "pro"),
                 account_id=s.get("account_id"),
                 user_id=s.get("user_id", "user"), file=s.get("file"))
            for s in specs]


def account(reading, needle):
    hits = [a for a in reading["accounts"] if needle in a["account"]]
    assert len(hits) == 1, (needle, [a["account"] for a in reading["accounts"]])
    return hits[0]


#: A Pro account at 70% climbing 2%/h, 100h from its reset, and a Team account
#: at 60% climbing 10%/h. Fleet: 62M tokens/h against ~800M left -> ~13h,
#: an order of magnitude under the 100h horizon.
SHORT = [{"email": "pro-a@example.com", "start": 58, "rate": 2.0,
          "reset_h": 100, "plan": "pro", "tokens_h": 50e6},
         {"email": "team-a@example.com", "start": 0, "rate": 10.0,
          "reset_h": 150, "plan": "team", "tokens_h": 12e6,
          "seat": "seat-b"}]
#: One Pro account at 20% climbing 1%/h, 30h from its reset: 80h of runway
#: against a 30h horizon, and it strands half its week.
LONG = [{"email": "pro-b@example.com", "start": 14, "rate": 1.0,
         "reset_h": 30, "plan": "pro", "tokens_h": 25e6}]


class RateTest(unittest.TestCase):
    def test_the_rate_is_the_slope_the_history_was_built_with(self):
        lines, requests = world([{"email": "pro-a@example.com", "start": 40,
                                  "rate": 1.5, "reset_h": 90,
                                  "tokens_h": 30e6}])
        reading = codexpace.read(lines, requests, NOW)
        acct = account(reading, "p…@example.com")
        self.assertEqual(acct["state"], "measured", acct)
        self.assertEqual(acct["window"], "7d",
                         "the rate rides the LONGEST window, not the 5h")
        self.assertAlmostEqual(acct["pct_per_hour"], 1.5, delta=0.15)
        self.assertEqual(acct["points"], 25)
        self.assertAlmostEqual(acct["span_h"], 6.0, places=3)
        self.assertAlmostEqual(acct["used_pct"], 49.0)
        # (100 - 49) / 1.5 is ~34h to the wall, against 90h to the reset
        self.assertAlmostEqual(acct["hours_to_wall"], 34.0, delta=3.5)
        self.assertTrue(acct["walls_before_reset"])
        # tokens per percent: 30M/h over 1.5%/h
        self.assertAlmostEqual(acct["tokens_per_pct"] / 20e6, 1.0, delta=0.1)
        self.assertEqual(acct["weight_source"], "measured")

    def test_only_the_segment_after_the_last_reset_counts(self):
        """A window that restarted mid-series is two segments. Fitting across
        the drop would read a NEGATIVE burn; the fit starts at the reset."""
        before = [brow("pro-a@example.com", 90 + i, NOW + 2 * HOUR)
                  for i in range(8)]
        after = [brow("pro-a@example.com", 2 * i, NOW + 160 * HOUR)
                 for i in range(9)]
        lines = [codexpace.history_line([r], NOW - (16 - i) * 900)
                 for i, r in enumerate(before + after)]
        acct = account(codexpace.read(lines, [], NOW, window_h=12),
                       "p…@example.com")
        self.assertEqual(acct["points"], 9, acct)
        self.assertAlmostEqual(acct["pct_per_hour"], 8.0, delta=0.5)
        self.assertGreater(acct["pct_per_hour"], 0)


class UnknownNeverZeroTest(unittest.TestCase):
    def test_two_readings_are_UNKNOWN_and_carry_no_number(self):
        lines = [codexpace.history_line(
            [brow("pro-a@example.com", 40 + i, NOW + 90 * HOUR)],
            NOW - (1 - i) * HOUR) for i in range(2)]
        acct = account(codexpace.read(lines, [], NOW), "p…@example.com")
        self.assertEqual(acct["state"], "unknown")
        self.assertIsNone(acct["pct_per_hour"],
                          "too few readings must be UNKNOWN, never 0")
        self.assertIsNone(acct["hours_to_wall"])
        self.assertIn("2 reading", acct["why"])
        # THE POSITIVE CONTROL on the same observable: one more reading, an
        # hour further back, and the same account has a rate.
        lines.insert(0, codexpace.history_line(
            [brow("pro-a@example.com", 39, NOW + 90 * HOUR)], NOW - 2 * HOUR))
        acct = account(codexpace.read(lines, [], NOW), "p…@example.com")
        self.assertEqual(acct["state"], "measured")
        self.assertIsNotNone(acct["pct_per_hour"])

    def test_three_readings_inside_half_an_hour_are_UNKNOWN(self):
        lines = [codexpace.history_line(
            [brow("pro-a@example.com", 40, NOW + 90 * HOUR)],
            NOW - (2 - i) * 900) for i in range(3)]
        acct = account(codexpace.read(lines, [], NOW), "p…@example.com")
        self.assertEqual(acct["state"], "unknown")
        self.assertIsNone(acct["pct_per_hour"])
        self.assertIn("0.5h", acct["why"])

    def test_an_account_the_newest_pass_could_not_read_is_UNREAD(self):  # noqa: VACUOUS_ASSERTION — the None is the unread law; the account's state and the fleet's verdict on the same reading are exact positive assertions
        lines, requests = world(LONG)
        lines[-1] = codexpace.history_line(
            [brow("pro-b@example.com", None, None)], NOW)
        reading = codexpace.read(lines, requests, NOW)
        acct = account(reading, "p…@example.com")
        self.assertEqual(acct["state"], "unread")
        self.assertIsNone(acct["used_pct"])
        self.assertNotEqual(reading["fleet"]["state"], "measured")
        self.assertEqual(reading["verdict"], burnflags.RUNWAY_UNKNOWN)


class MemberTest(unittest.TestCase):
    """THE task/2981 CLASS AT THE READER. On a Team plan the account id is
    the WORKSPACE: a series keyed on it would pour every member's readings
    into one line."""

    SPECS = [{"email": "admin@example.com", "start": 0, "rate": 10.0,
              "reset_h": 150, "plan": "team", "account_id": "ws-1",
              "tokens_h": 12e6},
             {"email": "other@example.com", "start": 90, "rate": 0.0,
              "reset_h": 120, "plan": "team", "account_id": "ws-1",
              "user_id": None, "file": "codex-other.json"}]

    def test_a_member_unknown_account_never_lends_a_sibling_its_numbers(self):  # noqa: VACUOUS_ASSERTION — not-proven is the law under test; both accounts' rates and levels are pinned as exact numbers on the same reading
        lines, requests = world(self.SPECS)
        reading = codexpace.read(lines, requests, NOW)
        admin = account(reading, "a…@example.com")
        other = account(reading, "o…@example.com")
        self.assertNotEqual(admin["member"], other["member"],
                            "one workspace id, two members, two series")
        self.assertTrue(admin["proven"])
        self.assertFalse(other["proven"])
        # each series is its OWN numbers: admin climbs, the other is flat
        self.assertAlmostEqual(admin["pct_per_hour"], 10.0, delta=0.5)
        self.assertAlmostEqual(other["pct_per_hour"], 0.0, delta=0.05)
        self.assertAlmostEqual(admin["used_pct"], 60.0)
        self.assertAlmostEqual(other["used_pct"], 90.0)
        # and the unproven credential is LEFT OUT of the supply, so the fleet
        # reading is a floor and nothing steps on it
        self.assertEqual(reading["fleet"]["state"], "floor")
        self.assertIn(other["account"], reading["fleet"]["left_out"])
        self.assertEqual(reading["verdict"], burnflags.RUNWAY_UNKNOWN)

    def test_the_control_without_the_unproven_member_is_a_full_reading(self):
        lines, requests = world(self.SPECS[:1] + [dict(SHORT[0])])
        reading = codexpace.read(lines, requests, NOW)
        self.assertEqual(reading["fleet"]["state"], "measured", reading["fleet"])
        self.assertEqual(reading["fleet"]["left_out"], [])

    def test_two_unproven_members_on_one_workspace_stay_two_series(self):  # noqa: VACUOUS_ASSERTION — the member set is pinned at exactly two and the second series' rate at its built value
        specs = [dict(self.SPECS[1]),
                 dict(self.SPECS[1], email="third@example.com", start=10,
                      rate=5.0, file="codex-third.json")]
        lines, _requests = world(specs)
        reading = codexpace.read(lines, [], NOW)
        members = {a["member"] for a in reading["accounts"]}
        self.assertEqual(len(members), 2)
        third = account(reading, "t…@example.com")
        self.assertAlmostEqual(third["pct_per_hour"], 5.0, delta=0.3)

    def test_a_history_entry_with_no_member_key_is_ignored(self):
        lines, requests = world(LONG)
        for line in lines:
            line["accounts"].append({"label": "stray@example.com",
                                     "plan": "pro", "windows": [
                                         {"label": "7d", "seconds": WEEK,
                                          "used_percent": 99.0,
                                          "reset_at": NOW + HOUR}]})
        reading = codexpace.read(lines, requests, NOW)
        self.assertEqual([a["account"] for a in reading["accounts"]],
                         ["p…@example.com"])


class RunwayTest(unittest.TestCase):
    def test_a_short_runway_names_its_inputs_and_its_horizon(self):
        lines, requests = world(SHORT)
        reading = codexpace.read(lines, requests, NOW)
        fleet = reading["fleet"]
        self.assertEqual(fleet["state"], "measured", fleet)
        self.assertAlmostEqual(fleet["tokens_per_hour"] / 62e6, 1.0, delta=0.05)
        self.assertAlmostEqual(fleet["runway_h"], 13.0, delta=2.0)
        self.assertAlmostEqual(fleet["horizon_h"], 100.0, delta=0.01)
        self.assertEqual(fleet["horizon_kind"], "pro-reset")
        self.assertEqual(reading["verdict"], burnflags.RUNWAY_SHORT)
        self.assertIn("runway 13h", reading["cause"])
        self.assertIn("100h", reading["cause"])
        self.assertIn("next Pro reset", reading["cause"])
        # the top seat over the last 3h, by ledger share
        self.assertEqual(fleet["top_seats"][0][0], "seat-a")
        self.assertIn("seat-a 81%", reading["cause"])
        # NO IDENTITY in the fold's input
        self.assertNotIn("@", json.dumps(codexpace.fold_input(reading)))

    def test_a_long_runway_with_a_stranding_account_reads_use_it(self):  # noqa: VACUOUS_ASSERTION — runway, strand and verdict are each pinned to the value the world was built with
        lines, requests = world(LONG)
        reading = codexpace.read(lines, requests, NOW)
        self.assertEqual(reading["fleet"]["state"], "measured")
        self.assertAlmostEqual(reading["fleet"]["runway_h"], 80.0, delta=6.0)
        acct = account(reading, "p…@example.com")
        self.assertAlmostEqual(acct["strand_pct"], 50.0, delta=3.0)
        self.assertEqual(reading["verdict"], burnflags.RUNWAY_USE_IT)
        self.assertIn("strand", reading["cause"])

    def test_a_long_runway_with_nothing_stranding_is_even(self):
        """The inverse needs BOTH halves: runway well above the horizon AND
        an account that will strand. Here the horizon account walls right at
        its reset and a fresh second Pro account burns its week to the end:
        runway well above the horizon, and nothing strands."""
        specs = [{"email": "pro-c@example.com", "start": 54, "rate": 1.0,
                  "reset_h": 40, "plan": "pro", "tokens_h": 25e6},
                 {"email": "pro-d@example.com", "start": 0, "rate": 0.6,
                  "reset_h": 168, "plan": "pro", "tokens_h": 15e6,
                  "seat": "seat-c"}]
        lines, requests = world(specs)
        reading = codexpace.read(lines, requests, NOW)
        self.assertEqual(reading["fleet"]["state"], "measured", reading["fleet"])
        for acct in reading["accounts"]:
            self.assertLess(acct["strand_pct"], codexpace.STRAND_PCT, acct)
        self.assertGreaterEqual(reading["fleet"]["runway_h"],
                                codexpace.WELL_ABOVE * 40.0)
        self.assertEqual(reading["verdict"], burnflags.RUNWAY_EVEN)
        # THE CONTROL on the strand half: slow the second account so it will
        # strand, and the same world reads use-it
        specs[1] = dict(specs[1], rate=0.4, start=0)
        lines, requests = world(specs)
        reading = codexpace.read(lines, requests, NOW)
        self.assertEqual(reading["verdict"], burnflags.RUNWAY_USE_IT)

    def test_no_pro_account_means_no_horizon_and_no_step(self):  # noqa: VACUOUS_ASSERTION — an absent horizon is the contract; the verdict on the same reading is pinned exactly
        specs = [dict(SHORT[1])]
        lines, requests = world(specs)
        reading = codexpace.read(lines, requests, NOW)
        self.assertIsNone(reading["fleet"]["horizon_h"])
        self.assertEqual(reading["verdict"], burnflags.RUNWAY_UNKNOWN)

    def test_an_unreadable_ledger_is_UNKNOWN_not_an_idle_fleet(self):
        lines, _requests = world(SHORT)
        reading = codexpace.read(lines, None, NOW)
        self.assertEqual(reading["fleet"]["state"], "unknown")
        self.assertIsNone(reading["fleet"]["runway_h"])
        self.assertEqual(reading["verdict"], burnflags.RUNWAY_UNKNOWN)
        # THE CONTROL: the same world with an EMPTY ledger is a real reading
        # of no spend — idle, not unknown.
        reading = codexpace.read(lines, [], NOW)
        self.assertTrue(reading["fleet"]["idle"])

    def test_an_unweighted_account_borrows_its_plans_measured_weight(self):
        """A second Team account that has not moved 3% yet has no weight of
        its own; the same plan's measured one is used and SAID."""
        specs = SHORT + [{"email": "team-b@example.com", "start": 5,
                          "rate": 0.0, "reset_h": 140, "plan": "team"}]
        lines, requests = world(specs)
        reading = codexpace.read(lines, requests, NOW)
        # both Team addresses mask to one spelling, so the rows are told
        # apart by the percentage each was built at
        a, = [x for x in reading["accounts"] if x["used_pct"] == 60.0]
        b, = [x for x in reading["accounts"] if x["used_pct"] == 5.0]
        self.assertEqual(a["weight_source"], "measured")
        self.assertEqual(b["weight_source"], "same-plan")
        self.assertEqual(b["tokens_per_pct"], a["tokens_per_pct"])
        self.assertEqual(reading["fleet"]["state"], "measured")


def _day(hours):
    """The weekday the cause names for an instant `hours` after NOW, taken
    the way a reader's clock takes it."""
    import time
    return time.strftime("%A", time.localtime(NOW + hours * HOUR))


#: A Pro account walled on a RATE LIMIT whose reset lies past SHORT's 100h
#: horizon; it carries no ledger tokens, so it borrows the Pro weight.
LATE_PRO = {"email": "pro-z@example.com", "start": 100, "rate": 0.0,
            "reset_h": 160, "plan": "pro", "reached": "rate_limit_reached",
            "credits": 1}


class CreditTest(unittest.TestCase):
    """THE RESET CLOCK (the owner's law): a credit spent on an account whose
    natural reset is h hours away gives a fresh week now and moves that
    reset to 168h out. Against the horizon that decides, per credit, whether
    the runway may count it."""

    @staticmethod
    def read(specs, requests=True):
        lines, reqs = world(specs)
        return codexpace.read(lines, reqs if requests is True else requests,
                              NOW)

    @staticmethod
    def one(reading, kind):
        per = [p for p in reading["fleet"]["credits"]["per"]
               if p["kind"] == kind]
        assert len(per) == 1, reading["fleet"]["credits"]
        return per[0]

    def test_a_credit_on_the_horizon_account_adds_no_supply_before_it(self):  # noqa: VACUOUS_ASSERTION — the zero value is the reset-clock law; held/usable counts, the week in tokens, its worth in hours and the cause sentence are exact positives on the same reading
        reading = self.read([dict(SHORT[0], credits=1), SHORT[1]])
        fleet, credits = reading["fleet"], reading["fleet"]["credits"]
        self.assertEqual((credits["held"], credits["usable"]), (1, 1))
        self.assertEqual(credits["value_tokens"], 0.0,
                         "spending it first forfeits the horizon refill")
        self.assertEqual(credits["runway_h"], fleet["runway_h"])
        credit = self.one(reading, "horizon")
        self.assertEqual(credit["value_tokens"], 0.0)
        # its week: 25M tokens per percent (50M/h over 2%/h) -> 2.5B, which
        # the fleet's 62M/h burns in ~40h
        self.assertAlmostEqual(credit["week_tokens"] / 2.5e9, 1.0, delta=0.05)
        self.assertAlmostEqual(credit["worth_h"], 40.3, delta=3.0)
        self.assertEqual(reading["verdict"], burnflags.RUNWAY_SHORT)
        self.assertIn("runway 13h is under", reading["cause"])
        self.assertIn("+1 Pro reset credit held: it cannot help before %s's "
                      "reset without forfeiting it; worth ~%s after"
                      % (_day(100), codexpace._hours(credit["worth_h"])),
                      reading["cause"])
        self.assertNotIn("@", reading["cause"])

    def test_a_credit_on_a_pro_account_resetting_past_the_horizon_counts(self):  # noqa: VACUOUS_ASSERTION — the counted value, the runway with it and the cause are pinned to the built world, and the three-credit control flips the verdict
        reading = self.read(SHORT + [LATE_PRO])
        credits = reading["fleet"]["credits"]
        credit = self.one(reading, "counted")
        self.assertIs(credit["usable"], True)
        self.assertAlmostEqual(credit["value_tokens"] / 2.5e9, 1.0, delta=0.05)
        self.assertEqual(credits["value_tokens"], credit["value_tokens"])
        # 0.8B of supply + 2.5B from the credit over 62M/h
        self.assertAlmostEqual(credits["runway_h"], 53.0, delta=4.0)
        self.assertAlmostEqual(reading["fleet"]["runway_h"], 13.0, delta=2.0)
        self.assertEqual(reading["verdict"], burnflags.RUNWAY_SHORT)
        self.assertIn("(13h of supply + ", reading["cause"])
        self.assertIn("from 1 reset credit)", reading["cause"])
        self.assertIn("counted: its account resets after %s's reset (%s)"
                      % (_day(100), _day(160)), reading["cause"])
        # THE CONTROL that the credit is what moved it: three of them lift
        # the runway past the horizon, and the same world reads even
        reading = self.read(SHORT + [dict(LATE_PRO, credits=3)])
        self.assertGreater(reading["fleet"]["credits"]["runway_h"], 100.0)
        self.assertEqual(reading["verdict"], burnflags.RUNWAY_EVEN)
        self.assertIn("+3 Pro reset credits held, each: counted",
                      reading["cause"])

    def test_credits_on_one_account_are_sequenced_before_the_horizon(self):
        late = dict(LATE_PRO, credits=4)
        reading = self.read(SHORT + [late])
        credits = reading["fleet"]["credits"]
        counted = [p for p in credits["per"] if p["value_tokens"]]
        self.assertEqual(len(counted), 3,
                         "the fourth week cannot be redeemed until after the "
                         "100h horizon")
        self.assertAlmostEqual(credits["value_tokens"] /
                               counted[0]["week_tokens"], 3.0, delta=0.01)

    def test_a_credit_on_a_workspace_credits_depleted_wall_counts(self):  # noqa: VACUOUS_ASSERTION — held/usable counts, the Team week in tokens, the verdict and the cause sentence are exact positives on the same reading
        """A reset is measured to lift the workspace walls, so a Team credit
        held there counts its Team week before the horizon."""
        depleted = {"email": "team-w@example.com", "start": 100, "rate": 0.0,
                    "reset_h": 150, "plan": "team", "credits": 1,
                    "reached": "workspace_owner_credits_depleted"}
        reading = self.read(SHORT + [depleted])
        credits = reading["fleet"]["credits"]
        self.assertEqual((credits["held"], credits["usable"]), (1, 1))
        credit = self.one(reading, "counted")
        self.assertIs(credit["usable"], True)
        # the Team weight: 12M/h over 10%/h -> 1.2M a percent, 120M a week
        self.assertAlmostEqual(credit["value_tokens"] / 1.2e8, 1.0, delta=0.05)
        self.assertEqual(credits["value_tokens"], credit["value_tokens"])
        self.assertEqual(reading["verdict"], burnflags.RUNWAY_SHORT)
        self.assertIn("+1 Team reset credit held: counted: spent once its "
                      "account is empty it adds a Team week, ~%s, before the "
                      "horizon" % codexpace._hours(credit["worth_h"]),
                      reading["cause"])

    def test_a_team_credit_counts_even_when_its_reset_comes_first(self):  # noqa: VACUOUS_ASSERTION — the Team week counted is an exact positive; the Pro control's zero is the reset-clock law, pinned beside its horizon and its sentence
        """THE OWNER'S RULING ON SMALLER ACCOUNTS: a Team week is spent again
        in well under a day, so the clock a reset moves costs little and the
        credit counts wherever its own reset lies. A Pro credit in the same
        place adds nothing before the horizon."""
        early_team = {"email": "team-e@example.com", "start": 100,
                      "rate": 0.0, "reset_h": 40, "plan": "team",
                      "credits": 1, "reached": "rate_limit_reached"}
        reading = self.read(SHORT + [early_team])
        credit = self.one(reading, "counted")
        self.assertEqual(credit["plan"], "team")
        self.assertAlmostEqual(credit["value_tokens"] / 1.2e8, 1.0, delta=0.05)
        # THE CONTROL: the same place on a Pro account adds nothing before
        # the horizon, because spending it first forfeits its own reset —
        # which, being the soonest Pro reset, IS the horizon
        early_pro = dict(early_team, email="pro-e@example.com", plan="pro")
        reading = self.read(SHORT + [early_pro])
        self.assertAlmostEqual(reading["fleet"]["horizon_h"], 40.0, delta=0.01)
        credit = self.one(reading, "horizon")
        self.assertEqual(credit["value_tokens"], 0.0)
        self.assertIn("without forfeiting it;", credit["why"])

    def test_an_unknown_weight_is_an_unknown_credit_and_never_0(self):  # noqa: VACUOUS_ASSERTION — None is the unknown-never-0 law; the unknown count, the verdict and the cause are exact positives, and the weighted control counts the same credit
        """A credit that would land supply before the horizon, on an account
        with no tokens-per-percent weight: its worth is UNKNOWN, and a short
        runway beside it is a floor that proves no shortage."""
        lonely = {"email": "team-q@example.com", "start": 100, "rate": 0.0,
                  "reset_h": 150, "plan": "team", "credits": 1,
                  "reached": "rate_limit_reached"}
        reading = self.read([SHORT[0], lonely])
        credits = reading["fleet"]["credits"]
        credit = self.one(reading, "unknown")
        self.assertIsNone(credit["value_tokens"], "unknown, never 0")
        self.assertIsNone(credit["week_tokens"])
        self.assertIsNone(credit["worth_h"])
        self.assertEqual(credits["unknown"], 1)
        self.assertLess(reading["fleet"]["runway_h"], 100.0)
        self.assertEqual(reading["verdict"], burnflags.RUNWAY_UNKNOWN)
        self.assertIn("shortage is not proven", reading["cause"])
        self.assertIn("no tokens-per-percent weight", reading["cause"])
        # THE CONTROL: a measured Team account lends the plan's weight, the
        # credit is counted, and the same world reads short again
        reading = self.read([SHORT[0], SHORT[1], lonely])
        self.assertEqual(self.one(reading, "counted")["usable"], True)
        self.assertEqual(reading["verdict"], burnflags.RUNWAY_SHORT)

    def test_an_unknown_fleet_rate_leaves_a_credits_worth_unknown(self):  # noqa: VACUOUS_ASSERTION — None is the unknown-never-0 law; the credit's kind and the sentence saying its worth is unknown are exact positives on the same reading
        """No ledger tokens: no weight and no rate. The credit's worth is
        unknown and SAID so; it is never rendered as 0h."""
        reading = self.read([dict(SHORT[0], credits=1), SHORT[1]],
                            requests=[])
        credit = self.one(reading, "horizon")
        self.assertIsNone(credit["worth_h"])
        self.assertIsNone(credit["week_tokens"])
        self.assertIn("worth an unknown amount after", credit["why"])
        self.assertNotIn(" 0h", credit["why"])
        self.assertIsNone(reading["fleet"]["credits"]["runway_h"])

    def test_an_unread_balance_is_unknown_never_0(self):  # noqa: VACUOUS_ASSERTION — the unread list, the unknown count, both verdicts and both cause sentences are exact positives; the absent credit sentence is the zero-balance control
        reading = self.read([SHORT[0], dict(SHORT[1], credits=None)])
        credits = reading["fleet"]["credits"]
        self.assertEqual(credits["unread"], ["t…@example.com"])
        self.assertEqual(credits["unknown"], 1,
                         "a Team account resetting past the horizon could "
                         "hold a credit that lands supply before it")
        self.assertEqual(reading["verdict"], burnflags.RUNWAY_UNKNOWN)
        self.assertIn("reset-credit balance unread on 1 Team account",
                      reading["cause"])
        # an unread balance on the HORIZON account cannot add supply before
        # the horizon whatever it holds, so the shortage stands
        reading = self.read([dict(SHORT[0], credits=None), SHORT[1]])
        self.assertEqual(reading["fleet"]["credits"]["unknown"], 0)
        self.assertEqual(reading["verdict"], burnflags.RUNWAY_SHORT)
        self.assertIn("reset-credit balance unread on 1 Pro account",
                      reading["cause"])
        # THE CONTROL: both balances read as zero is the plain short runway,
        # with no credit sentence at all
        reading = self.read(SHORT)
        self.assertEqual(reading["verdict"], burnflags.RUNWAY_SHORT)
        self.assertNotIn("credit", reading["cause"])

    def test_the_history_carries_the_counts_and_nothing_else(self):
        row = brow("pro-a@example.com", 40.0, NOW + 90 * HOUR, credits=2)
        row["reset_credits"]["profile_image_url"] = "https://x.example/p.png"
        entry, = codexpace.history_line([row], NOW)["accounts"]
        self.assertEqual(entry["reset_credits"],
                         {"available": 2, "applicable": 0})
        for junk in ({"available": True}, {"available": "1"}, "2", None,
                     {"available": -1}):
            row["reset_credits"] = junk
            entry, = codexpace.history_line([row], NOW)["accounts"]
            self.assertIsNone(entry["reset_credits"], junk)

    def test_the_fold_carries_the_credit_numbers_and_no_account(self):
        reading = self.read(SHORT + [LATE_PRO])
        fold = codexpace.fold_input(reading)
        self.assertEqual(fold["credits"]["held"], 1)
        self.assertEqual(fold["credits"]["usable"], 1)
        self.assertAlmostEqual(fold["credits"]["runway_h"], 53.0, delta=4.0)
        self.assertNotIn("@", json.dumps(fold))
        self.assertNotIn("per", fold["credits"])
        text = "\n".join(codexpace.render(reading))
        self.assertIn("reset credits: 1 held, 1 usable", text)
        self.assertIn("p…@example.com", text)


class FoldStepTest(unittest.TestCase):
    """THROUGH THE TABLE-DRIVEN FOLD: the runway is a fold INPUT, and the
    step is a row of `_RUNWAY_STEP_TABLE`, not a second fold."""

    @staticmethod
    def fold(rows, runway):
        inputs = {"ceiling": 90.0, "money": {"codex": rows},
                  "money_measured_at": {"codex": NOW}}
        if runway is not None:
            inputs["runway"] = {"codex": runway}
        return burnflags.fold(inputs, now=NOW)["families"]["codex"]

    def test_a_short_runway_steps_the_codex_money_axis_UP_one_colour(self):  # noqa: VACUOUS_ASSERTION — the absent address is the identity law; colour, cause_id and cause text are pinned on the same flag
        lines, requests = world(SHORT)
        reading = codexpace.read(lines, requests, NOW)
        rows = rows_at(SHORT)
        self.assertEqual(self.fold(rows, None)["colour"], burnflags.YELLOW,
                         "fixture: the budget alone must read YELLOW")
        flag = self.fold(rows, codexpace.fold_input(reading))
        self.assertEqual(flag["colour"], burnflags.ORANGE)
        self.assertEqual(flag["cause_id"], "money:runway-short")
        self.assertIn("runway 13h", flag["cause"])
        self.assertIn("next Pro reset", flag["cause"])
        self.assertNotIn("@", flag["cause"])

    def test_a_long_runway_with_a_stranding_account_steps_DOWN(self):  # noqa: VACUOUS_ASSERTION — the base colour is pinned before the step and the stepped colour and cause after it
        lines, requests = world(LONG)
        reading = codexpace.read(lines, requests, NOW)
        rows = rows_at(LONG)
        self.assertEqual(self.fold(rows, None)["colour"], burnflags.YELLOW)
        flag = self.fold(rows, codexpace.fold_input(reading))
        self.assertEqual(flag["colour"], burnflags.GREEN)
        self.assertEqual(flag["cause_id"], "money:runway-use-it")
        self.assertIn("use it", flag["cause"])

    def test_a_projection_never_steps_to_RED(self):  # noqa: VACUOUS_ASSERTION — the colour is pinned exactly before and after the step
        rows = [brow("pro-a@example.com", 85.0, NOW + 100 * HOUR)]
        self.assertEqual(self.fold(rows, None)["colour"], burnflags.ORANGE)
        runway = {"verdict": burnflags.RUNWAY_SHORT, "state": "measured",
                  "cause": "codex runway 5h is under the 100h horizon"}
        self.assertEqual(self.fold(rows, runway)["colour"], burnflags.ORANGE)

    def test_a_measured_wall_is_never_stepped_down(self):  # noqa: VACUOUS_ASSERTION — the colour is pinned exactly before and after the step
        rows = [brow("pro-a@example.com", 100.0, NOW + 100 * HOUR)]
        self.assertEqual(self.fold(rows, None)["colour"], burnflags.RED)
        runway = {"verdict": burnflags.RUNWAY_USE_IT, "state": "measured",
                  "cause": "codex runway 900h; use it"}
        self.assertEqual(self.fold(rows, runway)["colour"], burnflags.RED)

    def test_an_unread_account_keeps_GREEN_out_of_reach(self):  # noqa: VACUOUS_ASSERTION — the refused step is paired with the same step reaching GREEN once the unread account is gone
        rows = [brow("pro-a@example.com", 20.0, NOW + 30 * HOUR),
                brow("pro-z@example.com", None, None)]
        base = self.fold(rows, None)
        self.assertEqual(base["colour"], burnflags.YELLOW)
        self.assertTrue(base["capped_by_coverage"])
        runway = {"verdict": burnflags.RUNWAY_USE_IT, "state": "measured",
                  "cause": "codex runway 900h; use it"}
        self.assertEqual(self.fold(rows, runway)["colour"], burnflags.YELLOW)
        # THE CONTROL: the same step with the unread account gone reaches GREEN
        self.assertEqual(self.fold(rows[:1], runway)["colour"], burnflags.GREEN)

    def test_the_step_table_names_every_verdict_and_every_colour(self):  # noqa: VACUOUS_ASSERTION — the table's length is pinned to the full product, so every loop iteration runs
        verdicts = (burnflags.RUNWAY_SHORT, burnflags.RUNWAY_USE_IT,
                    burnflags.RUNWAY_EVEN, burnflags.RUNWAY_UNKNOWN)
        for verdict in verdicts:
            for colour in burnflags.COLOURS:
                self.assertIn((verdict, colour), burnflags._RUNWAY_STEP_TABLE)
        self.assertEqual(len(burnflags._RUNWAY_STEP_TABLE),
                         len(verdicts) * len(burnflags.COLOURS))
        for (verdict, colour), to in burnflags._RUNWAY_STEP_TABLE.items():
            if colour != burnflags.RED:
                self.assertNotEqual(to, burnflags.RED,
                                    "a projection reached RED: %s %s"
                                    % (verdict, colour))

    def test_an_unknown_or_missing_runway_changes_nothing(self):  # noqa: VACUOUS_ASSERTION — no step is the contract; each case pins colour and cause_id equal to the unstepped flag
        rows = rows_at(SHORT)
        base = self.fold(rows, None)
        for runway in ({"verdict": burnflags.RUNWAY_UNKNOWN, "cause": "x"},
                       {"verdict": "sideways", "cause": "x"}, {}):
            flag = self.fold(rows, runway)
            self.assertEqual(flag["colour"], base["colour"])
            self.assertEqual(flag["cause_id"], base["cause_id"])


class WallNoticeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-codexpace-walls-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "walls.json")
        self.posts, self.pushes = [], []

    def post(self, body):
        self.posts.append(body)

    def push(self, body, **_kw):
        self.pushes.append(body)
        return True

    def rows(self, reset_h=50):
        return [brow("walled@example.com", 100.0, NOW + reset_h * HOUR,
                     plan="team"),
                brow("open@example.com", 30.0, NOW + 90 * HOUR)]

    def announce(self, rows, now, requests=()):
        events = codexpace.wall_events(rows, None, list(requests), now)
        return codexpace.announce(events, now=now, post=self.post,
                                  push=self.push, path=self.path)

    def test_the_wall_notifies_once_and_not_twice(self):
        requests = [event("walled@example.com", NOW - 600, 9e6),
                    event("walled@example.com", NOW - 300, 1e6,
                          seat="seat-b")]
        self.announce(self.rows(), NOW, requests)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len(self.pushes), 1)
        body = self.posts[0]
        self.assertIn("walled@example.com", body)
        self.assertIn("7d", body)
        self.assertIn("seat-a 90%", body)
        self.assertIn("1 of 2", body)
        self.assertEqual(self.pushes[0], body)
        # the SAME wall on the next pass, and the one after: silence
        self.announce(self.rows(), NOW + 900)
        self.announce(self.rows(), NOW + 1800)
        self.assertEqual((len(self.posts), len(self.pushes)), (1, 1))

    def test_a_new_wall_after_the_reset_is_a_new_event(self):  # noqa: VACUOUS_ASSERTION — the observable is what reached the room and the phone, counted exactly
        """The positive control on the latch key: the wall is (member,
        reset instant), so the next week's wall speaks again."""
        self.announce(self.rows(reset_h=50), NOW)
        self.announce(self.rows(reset_h=50 + 168), NOW + 60 * HOUR)
        self.assertEqual((len(self.posts), len(self.pushes)), (2, 2))

    def test_an_open_account_announces_nothing(self):
        rows = [brow("open@example.com", 99.0, NOW + 90 * HOUR)]
        self.assertEqual(codexpace.wall_events(rows, None, [], NOW), [])
        # THE CONTROL on the same observable: at 100 it is a wall
        rows = [brow("open@example.com", 100.0, NOW + 90 * HOUR)]
        self.assertEqual(len(codexpace.wall_events(rows, None, [], NOW)), 1)

    def test_a_failed_channel_is_retried_alone(self):  # noqa: VACUOUS_ASSERTION — the observable is what reached the room and the phone, counted exactly after each call
        def broken(body):
            raise OSError("chat down")
        events = codexpace.wall_events(self.rows(), None, [], NOW)
        codexpace.announce(events, now=NOW, post=broken, push=self.push,
                           path=self.path)
        self.assertEqual((len(self.posts), len(self.pushes)), (0, 1))
        self.announce(self.rows(), NOW + 900)
        self.assertEqual((len(self.posts), len(self.pushes)), (1, 1),
                         "the chat line is retried and the push is not")
        self.announce(self.rows(), NOW + 1800)
        self.assertEqual((len(self.posts), len(self.pushes)), (1, 1))


class PassTest(unittest.TestCase):
    """The watchdog pass: history, snapshot, fold input and the one notice,
    under a temp home."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-codexpace-pass-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "home"),
            "HELM_CACHE_DIR": os.path.join(self.tmp, "cache")})
        env.start()
        self.addCleanup(env.stop)

    def test_the_pass_records_history_folds_and_notifies_once(self):  # noqa: VACUOUS_ASSERTION — the post and push counts, the history timestamps and the snapshot age are pinned exactly outside any loop
        rows = [brow("walled@example.com", 100.0, NOW + 50 * HOUR,
                     plan="team"),
                brow("pro-a@example.com", 40.0, NOW + 90 * HOUR)]
        with mock.patch("helm.chat.post") as post, \
                mock.patch("helm.notify.owner_push",
                           return_value=True) as push:
            first = codexpace.watch_pass(rows, now=NOW)
            second = codexpace.watch_pass(rows, now=NOW + 900)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(push.call_count, 1)
        self.assertEqual(post.call_args.kwargs.get("room"), "helm")
        for got in (first, second):
            self.assertIn("verdict", got)
            self.assertNotIn("@", json.dumps(got))
        lines, err = codexpace.read_history(now=NOW + 900)
        self.assertIsNone(err)
        self.assertEqual([line["ts"] for line in lines], [NOW, NOW + 900])
        reading, age = codexpace.cached_reading(now=NOW + 900)
        self.assertEqual(age, 0)
        self.assertEqual(len(reading["accounts"]), 2)
        self.assertEqual(codexpace.cached_fold_input(now=NOW + 900)["verdict"],
                         second["verdict"])
        # THE SAME LEDGER READ WRITES THE PER-SEAT BURN (task/3156)
        burn = codexpace.cached_seat_burn(now=NOW + 900)
        self.assertEqual(burn["ts"], NOW + 900)
        self.assertEqual(burn["window_s"], codexpace.SEAT_BURN_WINDOW_S)

    def test_a_pass_that_did_not_read_the_pool_writes_nothing(self):
        self.assertIsNone(codexpace.watch_pass(None, now=NOW))
        lines, err = codexpace.read_history(now=NOW)
        self.assertEqual((lines, err), ([], None))
        self.assertEqual(codexpace.cached_reading(now=NOW), (None, None))
        # THE CONTROL: a pass that read even one row writes both
        self.assertIsNotNone(codexpace.watch_pass(
            [brow("pro-a@example.com", 40.0, NOW + 90 * HOUR)], now=NOW))
        self.assertEqual(len(codexpace.read_history(now=NOW)[0]), 1)

    def test_proxywatch_hands_the_runway_to_the_fold(self):
        from helm import proxywatch
        self.assertIsNone(proxywatch._codex_pace_pass(
            {"ts": NOW, "codex_budget": None}))
        rows = [brow("pro-a@example.com", 40.0, NOW + 90 * HOUR)]
        rep = {"ts": NOW, "codex_budget": rows, "upstream": {}}
        with mock.patch("helm.chat.post"), \
                mock.patch("helm.notify.owner_push", return_value=True):
            rep["codex_runway"] = proxywatch._codex_pace_pass(rep)
        self.assertIn("verdict", rep["codex_runway"])
        with mock.patch.object(proxywatch, "read_vendor_resets",
                               return_value=({}, None)):
            payload = proxywatch._burn_flags_pass(rep)
        self.assertEqual(payload["readers"]["runway"], ["codex"])


class SeatBurnTest(unittest.TestCase):
    """task/3156: the ledger reader generalized to every proxy family, summed
    per seat for a project's bill — and the codex reader is unchanged."""

    def reqs(self):
        return [event("a@example.com", NOW - 1 * HOUR, 3e6, seat="seat-a"),
                event("a@example.com", NOW - 2 * HOUR, 3e6, seat="seat-a"),
                event("b@example.com", NOW - 1 * HOUR, 1.5e6, seat="seat-b"),
                # another family on the same ledger
                event("k@example.com", NOW - 1 * HOUR, 6e6, seat="seat-c",
                      family="kimi"),
                # outside the three-hour window
                event("a@example.com", NOW - 4 * HOUR, 90e6, seat="seat-a"),
                # a failed request spends nothing
                dict(event("a@example.com", NOW - 1 * HOUR, 90e6,
                           seat="seat-a"), failed=True)]

    def test_every_family_and_every_seat_is_summed_per_hour(self):
        got = codexpace.seat_burn(self.reqs(), NOW)
        self.assertEqual(got["window_s"], 3 * HOUR)
        self.assertEqual(sorted(got["families"]), ["codex", "kimi"])
        codex = got["families"]["codex"]
        self.assertAlmostEqual(codex["seats"]["seat-a"], 2e6)      # 6M / 3h
        self.assertAlmostEqual(codex["seats"]["seat-b"], 0.5e6)
        self.assertAlmostEqual(codex["per_hour"], 2.5e6)
        self.assertAlmostEqual(got["families"]["kimi"]["seats"]["seat-c"], 2e6)
        self.assertNotIn("@", json.dumps(got))

    def test_the_codex_reader_is_unchanged(self):
        """The pace and the wall events still see codex and nothing else."""
        led = codexpace._Ledger(self.reqs())
        self.assertEqual({r[4] for r in led.rows}, {"codex"})
        self.assertEqual(led.seats(NOW - 3 * HOUR, NOW),
                         [("seat-a", 0.8), ("seat-b", 0.2)])
        # CONTROL: the every-family reader holds the other family's rows
        every = codexpace._Ledger(self.reqs(), family=None)
        self.assertEqual({r[4] for r in every.rows}, {"codex", "kimi"})

    def test_an_unreadable_ledger_is_not_a_zero(self):
        got = codexpace.seat_burn(None, NOW)
        self.assertEqual(got["families"], {})
        self.assertIn("could not be read", got["why"])

    def test_the_snapshot_round_trips_and_ages_out(self):
        tmp = tempfile.mkdtemp(prefix="helm-test-seat-burn-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "seat-burn.json")
        self.assertIsNone(codexpace.cached_seat_burn(now=NOW, path=path))
        self.assertTrue(codexpace.write_seat_burn(
            codexpace.seat_burn(self.reqs(), NOW), path=path))
        got = codexpace.cached_seat_burn(now=NOW + 60, path=path)
        self.assertAlmostEqual(got["families"]["codex"]["per_hour"], 2.5e6)
        self.assertIsNone(codexpace.cached_seat_burn(
            now=NOW + burnflags.max_age_s() + 1, path=path))


class SurfaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-codexpace-surface-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "home"),
            "HELM_CACHE_DIR": os.path.join(self.tmp, "cache")})
        env.start()
        self.addCleanup(env.stop)

    @staticmethod
    def run_burn(args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = burnflags.cmd_burn(args)
        return rc, out.getvalue(), err.getvalue()

    def test_helm_burn_carries_ONE_runway_line(self):
        rc, out, _err = self.run_burn([])
        self.assertIn("codex runway: not measured", out)
        lines, requests = world(SHORT)
        codexpace.write_snapshot(codexpace.read(lines, requests, NOW))
        with mock.patch("time.time", return_value=NOW + 60):
            rc, out, _err = self.run_burn([])
        runway = [l for l in out.splitlines() if "codex runway" in l]
        self.assertEqual(len(runway), 1, out)
        self.assertIn("runway 13h", runway[0])
        self.assertIn("short", runway[0])

    def test_helm_burn_says_what_the_held_credits_are_worth(self):
        lines, requests = world([dict(SHORT[0], credits=1), SHORT[1]])
        codexpace.write_snapshot(codexpace.read(lines, requests, NOW))
        with mock.patch("time.time", return_value=NOW + 60):
            _rc, out, _err = self.run_burn([])
        runway, = [l for l in out.splitlines() if "codex runway" in l]
        self.assertIn("+1 Pro reset credit held: it cannot help before", runway)
        self.assertIn("worth ~", runway)

    def test_the_runway_verb_reads_live_and_refuses_junk(self):
        rc, _out, err = self.run_burn(["runway", "--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("--bogus", err)
        rc, _out, err = self.run_burn(["runway", "--window", "0"])
        self.assertEqual(rc, 2)
        rc, out, _err = self.run_burn(["runway", "--json"])
        self.assertEqual(rc, 3, "no history yet is UNKNOWN, exit 3")
        self.assertEqual(json.loads(out)["verdict"], burnflags.RUNWAY_UNKNOWN)
        # THE CONTROL: with history on disk the verb reads it
        lines, _requests = world(LONG)
        for line in lines:
            codexpace._append_line(line)
        with mock.patch("time.time", return_value=NOW), \
                mock.patch("helm.proxy_usage.events",
                           return_value=(world(LONG)[1], {}, None)):
            rc, out, _err = self.run_burn(["runway", "--window", "6"])
        self.assertEqual(rc, 0, out)
        self.assertIn("p…@example.com", out)
        self.assertIn("%/h", out)


if __name__ == "__main__":
    unittest.main()
