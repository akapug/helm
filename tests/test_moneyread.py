"""Generic money readers: synthetic homes, credentials and transport only."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import burnflags, moneyread  # noqa: E402


class MoneyReadTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="helm-test-moneyread-")
        env = {"HOME": os.path.join(self.root, "home"),
               "HELM_HOME": os.path.join(self.root, "helm"),
               "HELM_ADOPTED_DIR": os.path.join(self.root, "adopted"),
               "TMPDIR": os.path.join(self.root, "tmp")}
        for path in env.values():
            os.makedirs(path)
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def table():
        return {"alpha": {"money_reader": "fixture"},
                "beta": {"pool_providers": {
                    "leg": {"money_reader": "fixture"}}},
                "blind": {}}

    def test_catalog_derives_direct_and_pool_readers(self):
        self.assertEqual(moneyread.catalog_bindings(self.table()),
                         {"fixture": ("alpha", "beta")})
        self.assertEqual(moneyread.catalog_families(self.table()),
                         ("alpha", "beta"))
        self.assertEqual(burnflags.money_readers(self.table()),
                         ("alpha", "anthropic", "beta", "codex"))

    def test_one_hashed_credential_is_probed_once_and_no_identity_serializes(self):  # noqa: VACUOUS_ASSERTION — the same arm first asserts one probe, one persisted reading and the exact derived source before asserting that planted identity/key/url strings are absent
        calls = []
        secret = "customer@example.test"
        cred = moneyread.cred_ref(secret, value={"api_key": "sk-live-secret"})

        def probe(ref, now):
            calls.append(ref)
            return {"status": "ok", "measured_at": now,
                    "plan": "https://customer.example/private",
                    "token": "sk-live-secret", "email": secret,
                    "exception": "GET https://customer.example failed",
                    "overflow": {"kind": "prepaid", "balance": 7},
                    "windows": [{"label": "1m", "seconds": 60,
                                 "used_percent": 25, "reset_at": now + 60,
                                 "unit": "requests", "source": "derived"}]}

        readers = {"fixture": {"creds": lambda _families: [cred, cred],
                                "probe": probe,
                                "rows": moneyread.reading_rows}}
        snap = moneyread.probe_snapshot(now=1000, readers=readers,
                                        table=self.table())
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(snap["readings"]), 1)
        record = snap["readings"][0]
        self.assertEqual(record["families"], ["alpha", "beta"])
        self.assertRegex(record["account"], r"^[0-9a-f]{8}$")
        encoded = json.dumps(snap)
        for raw in (secret, "sk-live-secret", "customer.example", "https://"):
            self.assertNotIn(raw, encoded)
        self.assertIsNone(record["plan"])
        self.assertEqual(record["rows"]["alpha"][0]["source"], "derived")
        history = moneyread.history_rows(snap)
        self.assertEqual(history[0]["source"], "derived")
        self.assertEqual(history[0]["gauges"][0]["source"], "derived")
        path = os.path.join(self.root, "money-history.jsonl")
        self.assertTrue(moneyread.append_history(snap, path=path))
        consumed = list(burnflags._history_lines(path=path, provider="fixture"))
        self.assertEqual(consumed[0]["source"], "derived")
        self.assertEqual(consumed[0]["gauges"][0]["source"], "derived")

    def test_derived_window_cannot_be_laundered_by_measured_row_source(self):
        inherited = {"state": "ok", "longest_pct": 100, "source": "derived",
                     "windows": [{"label": "7d", "seconds": 604800,
                                  "used_percent": 100, "reset_at": 2000,
                                  "unit": "requests"}]}
        snap = {"v": 1, "ts": 1000, "readings": [{
            "reader": "fixture", "account": "deadbeef",
            "families": ["alpha"], "status": "ok", "measured_at": 1000,
            "expires_at": 2000, "windows": [],
            "rows": {"alpha": [inherited]}}]}
        mapped = moneyread.inputs(snap, now=1000, table=self.table())
        window = mapped["money"]["alpha"][0]["windows"][0]
        self.assertEqual(window["source"], "derived")
        self.assertNotEqual(burnflags.derive_money(
            "alpha", mapped["money"]["alpha"], measured_at=1000)["colour"],
                            burnflags.RED)

        invalid = json.loads(json.dumps(snap))
        invalid["readings"][0]["rows"]["alpha"][0]["source"] = "invented"
        mapped = moneyread.inputs(invalid, now=1000, table=self.table())
        row = mapped["money"]["alpha"][0]
        self.assertEqual((row["source"], row["windows"][0]["source"]),
                         ("derived", "derived"))
        self.assertNotEqual(burnflags.derive_money(
            "alpha", [row], measured_at=1000)["colour"], burnflags.RED)

        base = {"state": "ok", "longest_pct": 20, "source": "measured",
                "windows": [{"label": "7d", "seconds": 604800,
                             "used_percent": 20, "reset_at": 2000,
                             "unit": "requests", "source": "derived"}]}
        snap = {"v": 1, "ts": 1000, "readings": [{
            "reader": "fixture", "account": "deadbeef",
            "families": ["alpha"], "status": "ok", "measured_at": 1000,
            "expires_at": 2000, "windows": [], "rows": {"alpha": [base]}}]}
        mapped = moneyread.inputs(snap, now=1000, table=self.table())
        self.assertEqual(mapped["money"]["alpha"][0]["source"], "derived")
        direct = json.loads(json.dumps(snap))
        direct["readings"][0]["rows"]["alpha"][0]["windows"][0]["source"] = \
            "measured"
        self.assertEqual(moneyread.inputs(
            direct, now=1000, table=self.table())["money"]["alpha"][0]["source"],
                         "measured")

    def test_reading_rows_uses_worst_window_not_longest_window(self):
        reading = moneyread.Reading(
            "ok", 1000, windows=(
                {"label": "5h", "seconds": 18000, "used_percent": 100,
                 "reset_at": 2000, "unit": "percent", "source": "measured"},
                {"label": "7d", "seconds": 604800, "used_percent": 20,
                 "reset_at": 9000, "unit": "percent", "source": "measured"}))
        self.assertEqual(moneyread.reading_rows(reading)[0]["longest_pct"], 100)

    def test_probe_exception_is_fixed_unread_not_raw_exception_text(self):
        cred = moneyread.cred_ref("identity")

        def fail(_cred, _now):
            raise RuntimeError("GET https://private.example?token=secret")

        readers = {"fixture": {"creds": lambda _families: [cred],
                                "probe": fail,
                                "rows": moneyread.reading_rows}}
        snap = moneyread.probe_snapshot(now=1000, readers=readers,
                                        table=self.table())
        encoded = json.dumps(snap)
        self.assertIn("unread:probe-failed", encoded)
        self.assertNotIn("private.example", encoded)
        self.assertNotIn("secret", encoded)

    def test_missing_snapshot_is_explicit_unknown_for_declared_families(self):
        mapped = moneyread.inputs(None, now=1000, table=self.table(),
                                  error="snapshot-unreadable")
        for family in ("alpha", "beta"):
            row = mapped["money"][family][0]
            self.assertEqual(row["state"], "unread:snapshot-unreadable")
            self.assertIsNone(row["longest_pct"])
        payload, error = moneyread.read_snapshot(
            os.path.join(self.root, "absent.json"))
        self.assertIsNone(payload)
        self.assertEqual(error, "snapshot-absent")
        malformed = {"v": 1, "readings": [{
            "reader": "fixture", "account": "deadbeef",
            "measured_at": 1000, "expires_at": 2000,
            "rows": {"alpha": []}}]}
        row = moneyread.inputs(
            malformed, now=1000, table=self.table())["money"]["alpha"][0]
        self.assertEqual(row["state"], "unread:row-malformed")
        self.assertIsNone(row["longest_pct"])

    def test_snapshot_freshness_honours_expiry_and_future_measurements(self):
        reading = moneyread.Reading(
            "ok", 1000, windows=({"label": "1m", "seconds": 60,
                                  "used_percent": 5, "reset_at": 1060,
                                  "unit": "requests", "source": "derived"},),
            expires_at=1060)
        readers = {"fixture": {"creds": lambda _families: [moneyread.cred_ref("a")],
                                "probe": lambda _cred, _now: reading,
                                "rows": moneyread.reading_rows}}
        snap = moneyread.probe_snapshot(now=1000, readers=readers,
                                        table=self.table())
        exact = moneyread.inputs(snap, now=1060, max_age_s=1000,
                                 table=self.table())
        stale = moneyread.inputs(snap, now=1060.001, max_age_s=1000,
                                 table=self.table())
        future = moneyread.inputs(snap, now=999, max_age_s=1000,
                                  table=self.table())
        self.assertTrue(exact["money_fresh"]["alpha"])
        self.assertFalse(stale["money_fresh"]["alpha"])
        self.assertFalse(future["money_fresh"]["alpha"])

    def test_one_minute_ledger_requires_complete_collection_and_expires(self):
        now, account = 1000, "deadbeef"
        limit = [{"label": "1m", "seconds": 60, "cap": 20,
                  "unit": "requests", "reset_at": None}]
        incomplete = moneyread.ledger_reading(
            now, limit, [], {"complete": False, "since": 940, "through": now},
            account=account)
        self.assertEqual(incomplete.status, "unread:ledger-incomplete")
        events = [
            {"at": 940, "source": "cred:" + account},       # inclusive boundary
            {"at": 939.999, "source": "cred:" + account},   # stale
            {"at": 1000.001, "source": "cred:" + account},  # future
            {"at": 999, "source": "cred:ffffffff"},         # other credential
        ]
        reading = moneyread.ledger_reading(
            now, limit, events,
            {"complete": True, "since": 940, "through": now},
            account=account)
        self.assertEqual(reading.status, "ok")
        self.assertEqual(reading.expires_at, now + 60)
        self.assertEqual(reading.windows[0]["used_percent"], 5.0)
        self.assertEqual(reading.windows[0]["source"], "derived")
        short = moneyread.ledger_reading(
            now, limit, events,
            {"complete": True, "since": 940.001, "through": now},
            account=account)
        self.assertEqual(short.status, "unread:ledger-incomplete")
        identity = "joined-account@example.test"
        joined = moneyread.ledger_reading(
            now, limit, [{"at": now, "source": identity}],
            {"complete": True, "since": 940, "through": now},
            account=moneyread.cred_ref(identity).account)
        self.assertEqual(joined.windows[0]["used_percent"], 5.0)
        invalid = moneyread.ledger_reading(
            now, limit, events,
            {"complete": True, "since": 940, "through": now},
            account="raw-customer-identity")
        self.assertEqual(invalid.status, "unread:ledger-account")

    def test_fixed_window_uses_period_start_and_matching_anchor_only(self):
        account, now = "deadbeef", 1000
        limit = [{"label": "period", "seconds": 100, "cap": 20,
                  "unit": "requests", "reset_at": 1050}]
        anchor = {"measured_at": 970,
                  "windows": [{"label": "period", "used_percent": 20,
                               "reset_at": 1050}]}
        events = [{"at": 960, "source": "cred:" + account},
                  {"at": 970, "source": "cred:" + account},
                  {"at": 980, "source": "cred:" + account}]
        reading = moneyread.ledger_reading(
            now, limit, events,
            {"complete": True, "since": 970, "through": now},
            anchor=anchor, account=account)
        self.assertEqual(reading.windows[0]["used_percent"], 25.0)
        # A prior-period anchor is never carried into this period.
        old = {"measured_at": 970,
               "windows": [{"label": "period", "used_percent": 90,
                            "reset_at": 950}]}
        rebuilt = moneyread.ledger_reading(
            now, limit, events,
            {"complete": True, "since": 950, "through": now},
            anchor=old, account=account)
        self.assertEqual(rebuilt.windows[0]["used_percent"], 15.0)
        # A reset-matched anchor with no direct utilization is not a zero base:
        # rebuild the whole period when coverage permits, otherwise stay unread.
        invalid = {"measured_at": 970,
                   "windows": [{"label": "period", "reset_at": 1050}]}
        rebuilt = moneyread.ledger_reading(
            now, limit, events,
            {"complete": True, "since": 950, "through": now},
            anchor=invalid, account=account)
        self.assertEqual(rebuilt.windows[0]["used_percent"], 15.0)
        unread = moneyread.ledger_reading(
            now, limit, events,
            {"complete": True, "since": 970, "through": now},
            anchor=invalid, account=account)
        self.assertEqual(unread.status, "unread:ledger-incomplete")

    def test_rolling_window_rebuilds_and_never_carries_aggregate_anchor(self):
        account, now = "deadbeef", 1000
        limit = [{"label": "1m", "seconds": 60, "cap": 20,
                  "unit": "requests", "reset_at": None}]
        anchor = {"measured_at": 990,
                  "windows": [{"label": "1m", "used_percent": 80,
                               "reset_at": None}]}
        reading = moneyread.ledger_reading(
            now, limit, [{"at": 999, "source": "cred:" + account}],
            {"complete": True, "since": 940, "through": now},
            anchor=anchor, account=account)
        self.assertEqual(reading.windows[0]["used_percent"], 5.0)

    def test_each_reading_has_its_own_freshness(self):
        table = {"alpha": {"money_reader": "fixture"}}
        daily = {"reader": "fixture", "account": "aaaaaaaa",
                 "families": ["alpha"], "status": "ok", "measured_at": 1000,
                 "expires_at": 2000, "windows": [], "rows": {"alpha": [{
                     "state": "ok", "longest_pct": 20, "source": "measured",
                     "windows": [{"label": "1d", "seconds": 86400,
                                  "used_percent": 20, "reset_at": 2000,
                                  "unit": "requests", "source": "measured"}]}]}}
        minute = {"reader": "fixture", "account": "aaaaaaaa",
                  "families": ["alpha"], "status": "ok", "measured_at": 1000,
                  "expires_at": 1060, "windows": [], "rows": {"alpha": [{
                      "state": "ok", "longest_pct": 50, "source": "derived",
                      "windows": [{"label": "1m", "seconds": 60,
                                   "used_percent": 50, "reset_at": None,
                                   "unit": "requests", "source": "derived"}]}]}}
        mapped = moneyread.inputs(
            {"v": 1, "ts": 1000, "readings": [daily, minute]},
            now=1100, max_age_s=1000, table=table)
        self.assertTrue(mapped["money_fresh"]["alpha"])
        self.assertEqual(len(mapped["money"]["alpha"]), 1,
                         "one account became two rotated pool members")
        merged = mapped["money"]["alpha"][0]
        self.assertEqual(merged["state"], "ok")
        self.assertEqual(merged["longest_pct"], 20)
        self.assertEqual({w["label"]: w["used_percent"]
                          for w in merged["windows"]},
                         {"1d": 20, "1m": None})
        axis = burnflags.derive_money(
            "alpha", mapped["money"]["alpha"], measured_at=1000,
            fresh=mapped["money_fresh"]["alpha"])
        flag = burnflags.compose("alpha", {"money": axis}, now=1100)
        self.assertEqual(flag["money_provenance"], "derived")
        self.assertTrue(flag["capped_by_coverage"])
        with mock.patch.object(burnflags, "family_flag", return_value=flag):
            self.assertEqual(burnflags.can_spend("alpha", now=1100)["answer"],
                             "unknown")

    def test_two_readers_for_one_account_merge_before_the_fold(self):
        table = {"alpha": {"money_reader": "fixture"}}
        daily = {"reader": "vendor", "account": "aaaaaaaa",
                 "families": ["alpha"], "status": "ok", "measured_at": 1000,
                 "expires_at": 2000, "windows": [], "rows": {"alpha": [{
                     "state": "ok", "longest_pct": 100, "source": "measured",
                     "windows": [{"label": "1d", "seconds": 86400,
                                  "used_percent": 100, "reset_at": 2000,
                                  "unit": "requests", "source": "measured"}]}]}}
        minute = {"reader": "ledger", "account": "aaaaaaaa",
                  "families": ["alpha"], "status": "ok", "measured_at": 1000,
                  "expires_at": 1060, "windows": [], "rows": {"alpha": [{
                      "state": "ok", "longest_pct": 5, "source": "derived",
                      "windows": [{"label": "1m", "seconds": 60,
                                   "used_percent": 5, "reset_at": 1060,
                                   "unit": "requests", "source": "derived"}]}]}}
        mapped = moneyread.inputs(
            {"v": 1, "ts": 1000, "readings": [daily, minute]},
            now=1000, max_age_s=1000, table=table)
        self.assertEqual(len(mapped["money"]["alpha"]), 1)
        self.assertEqual(mapped["money"]["alpha"][0]["longest_pct"], 100)
        # THE FOLDED STATE, not only the row count (task/2935): the vendor's
        # measured wall dominates the ledger's derived minute on the SAME
        # account, so the one account is WALLED and the family refuses.
        row = burnflags._money_rows(mapped["money"]["alpha"])[0]
        self.assertEqual(burnflags._account_money(row, 90)["state"],
                         burnflags.ACCOUNT_WALLED)
        axis = burnflags.derive_money("alpha", mapped["money"]["alpha"],
                                      ceiling=90, measured_at=1000)
        self.assertEqual((axis["colour"], axis["cause_id"], axis["expires_at"]),
                         (burnflags.RED, "money:window-wall", 2000))
        flag = burnflags.compose("alpha", {"money": axis}, now=1000)
        with mock.patch.object(burnflags, "family_flag", return_value=flag):
            self.assertEqual(burnflags.can_spend("alpha", now=1000)["answer"],
                             "no")
        # the second shape the two readers produce: the vendor says
        # exhausted with no window at its cap. Merged, the account is the
        # exhausted-without-a-cap cell, UNREAD, never an open account.
        daily["rows"]["alpha"][0].update(state="exhausted", longest_pct=40)
        daily["rows"]["alpha"][0]["windows"][0]["used_percent"] = 40
        mapped = moneyread.inputs(
            {"v": 1, "ts": 1000, "readings": [daily, minute]},
            now=1000, max_age_s=1000, table=table)
        self.assertEqual(mapped["money"]["alpha"][0]["state"], "exhausted")
        row = burnflags._money_rows(mapped["money"]["alpha"])[0]
        self.assertEqual(burnflags._account_money(row, 90)["state"],
                         burnflags.ACCOUNT_UNREAD)

    def test_a_live_exhausted_reading_survives_the_account_join(self):
        """task/2935 finding 3: `_account_row` turned a live `exhausted` row
        into `ok` whenever it carried any number, so the fold's
        exhausted-without-a-measured-cap cell could never be reached and the
        vendor's own "exhausted" read as an OPEN account — a spend YES."""
        table = {"alpha": {"money_reader": "fixture"}}
        vendor = {"reader": "vendor", "account": "aaaaaaaa",
                  "families": ["alpha"], "status": "ok", "measured_at": 1000,
                  "expires_at": 2000, "windows": [], "rows": {"alpha": [{
                      "state": "exhausted", "longest_pct": 40,
                      "source": "measured",
                      "windows": [{"label": "1d", "seconds": 86400,
                                   "used_percent": 40, "reset_at": 2000,
                                   "unit": "requests",
                                   "source": "measured"}]}]}}
        mapped = moneyread.inputs({"v": 1, "ts": 1000, "readings": [vendor]},
                                  now=1000, max_age_s=1000, table=table)
        row = mapped["money"]["alpha"][0]
        self.assertEqual((row["state"], row["longest_pct"]), ("exhausted", 40))
        axis = burnflags.derive_money("alpha", mapped["money"]["alpha"],
                                      ceiling=90, measured_at=1000)
        self.assertEqual((axis["colour"], axis["cause_id"]),
                         (burnflags.GREY, "money:unreadable"))
        self.assertIn("exhausted without a measured cap", axis["cause"])
        flag = burnflags.compose("alpha", {"money": axis}, now=1000)
        with mock.patch.object(burnflags, "family_flag", return_value=flag):
            self.assertEqual(burnflags.can_spend("alpha", now=1000)["answer"],
                             "unknown")
        # CONTROL: the same reading at its cap is a measured wall, so the
        # UNKNOWN above is the missing cap and not the word itself
        vendor["rows"]["alpha"][0]["windows"][0]["used_percent"] = 100
        mapped = moneyread.inputs({"v": 1, "ts": 1000, "readings": [vendor]},
                                  now=1000, max_age_s=1000, table=table)
        self.assertEqual(burnflags.derive_money(
            "alpha", mapped["money"]["alpha"], ceiling=90,
            measured_at=1000)["colour"], burnflags.RED)

    def test_windowless_success_is_unread_after_the_account_join(self):
        table = {"alpha": {"money_reader": "fixture"}}
        good = {"reader": "fixture", "account": "aaaaaaaa",
                "families": ["alpha"], "status": "ok", "measured_at": 1000,
                "expires_at": 2000, "windows": [], "rows": {"alpha": [{
                    "state": "ok", "longest_pct": 20, "source": "measured",
                    "windows": [{"label": "1d", "seconds": 86400,
                                 "used_percent": 20, "reset_at": 2000,
                                 "unit": "requests", "source": "measured"}]}]}}
        empty = {"reader": "fixture", "account": "aaaaaaaa",
                 "families": ["alpha"], "status": "ok", "measured_at": 1000,
                 "expires_at": 2000, "windows": [], "rows": {"alpha": [{
                     "state": "ok", "longest_pct": None, "source": "measured",
                     "windows": []}]}}
        row = moneyread.inputs(
            {"v": 1, "ts": 1000, "readings": [good, empty]},
            now=1000, max_age_s=1000, table=table)["money"]["alpha"][0]
        axis = burnflags.derive_money("alpha", [row], measured_at=1000)
        flag = burnflags.compose("alpha", {"money": axis}, now=1000)
        self.assertTrue(flag["capped_by_coverage"])
        with mock.patch.object(burnflags, "family_flag", return_value=flag):
            self.assertEqual(burnflags.can_spend("alpha", now=1000)["answer"],
                             "unknown")

    def test_windowless_unread_sibling_survives_the_account_join(self):
        table = {"alpha": {"money_reader": "fixture"}}
        good = {"reader": "fixture", "account": "aaaaaaaa",
                "families": ["alpha"], "status": "ok", "measured_at": 1000,
                "expires_at": 2000, "windows": [], "rows": {"alpha": [{
                    "state": "ok", "longest_pct": 20, "source": "measured",
                    "windows": [{"label": "1d", "seconds": 86400,
                                 "used_percent": 20, "reset_at": 2000,
                                 "unit": "requests", "source": "measured"}]}]}}
        unread = {"reader": "fixture", "account": "aaaaaaaa",
                  "families": ["alpha"], "status": "unread:probe-failed",
                  "measured_at": 1000, "expires_at": 2000, "windows": [],
                  "rows": {"alpha": [{"state": "unread:probe-failed",
                                        "longest_pct": None,
                                        "source": "measured", "windows": []}]}}
        mapped = moneyread.inputs(
            {"v": 1, "ts": 1000, "readings": [good, unread]},
            now=1000, max_age_s=1000, table=table)
        row = mapped["money"]["alpha"][0]
        self.assertEqual(row["longest_pct"], 20)
        self.assertIn(None, [w["used_percent"] for w in row["windows"]])
        axis = burnflags.derive_money("alpha", [row], measured_at=1000)
        self.assertTrue(axis["capped_by_coverage"])
        flag = burnflags.compose("alpha", {"money": axis}, now=1000)
        with mock.patch.object(burnflags, "family_flag", return_value=flag):
            self.assertEqual(burnflags.can_spend("alpha", now=1000)["answer"],
                             "unknown")

    def test_same_account_readings_are_one_conjunction_not_rotated_siblings(self):
        table = {"alpha": {"money_reader": "fixture"}}
        daily = {"reader": "fixture", "account": "aaaaaaaa",
                 "families": ["alpha"], "status": "ok", "measured_at": 1000,
                 "expires_at": 2000, "windows": [], "rows": {"alpha": [{
                     "state": "ok", "longest_pct": 100, "source": "measured",
                     "windows": [{"label": "1d", "seconds": 86400,
                                  "used_percent": 100, "reset_at": 2000,
                                  "unit": "requests", "source": "measured"}]}]}}
        minute = {"reader": "fixture", "account": "aaaaaaaa",
                  "families": ["alpha"], "status": "ok", "measured_at": 1000,
                  "expires_at": 1060, "windows": [], "rows": {"alpha": [{
                      "state": "ok", "longest_pct": 5, "source": "derived",
                      "windows": [{"label": "1m", "seconds": 60,
                                   "used_percent": 5, "reset_at": 1060,
                                   "unit": "requests", "source": "derived"}]}]}}
        mapped = moneyread.inputs(
            {"v": 1, "ts": 1000, "readings": [daily, minute]},
            now=1000, max_age_s=1000, table=table)
        self.assertEqual(len(mapped["money"]["alpha"]), 1)
        row = mapped["money"]["alpha"][0]
        self.assertEqual(row["longest_pct"], 100)
        self.assertEqual(row["source"], "derived",
                         "the derived sibling must remain visible as provenance")
        self.assertEqual({w["label"] for w in row["windows"]}, {"1d", "1m"})
        axis = burnflags.derive_money("alpha", [row], ceiling=90,
                                      measured_at=1000)
        self.assertEqual((axis["colour"], axis["cause_id"]),
                         (burnflags.RED, "money:window-wall"))

    def test_last_good_returns_only_direct_measured_anchor(self):
        path = os.path.join(self.root, "history.jsonl")
        account = "deadbeef"
        rows = [
            {"id": "old", "provider": "fixture", "account": account,
             "probed_at": "1970-01-01T00:16:40Z", "status": "ok",
             "source": "measured", "gauges": [{"label": "1d",
              "utilization": 0.2, "reset": 2000}]},
            {"id": "derived", "provider": "fixture", "account": account,
             "probed_at": "1970-01-01T00:17:00Z", "status": "ok",
             "source": "derived", "gauges": [{"label": "1d",
              "utilization": 0.9, "reset": 2000}]},
            {"id": "contradiction", "provider": "fixture", "account": account,
             "probed_at": "1970-01-01T00:17:05Z", "status": "ok",
             "source": "measured", "gauges": [{"label": "1d",
              "utilization": 0.8, "reset": 2000, "source": "derived"}]},
            {"id": "unread", "provider": "fixture", "account": account,
             "probed_at": "1970-01-01T00:17:10Z", "status": "unread:probe-failed",
             "source": "measured", "gauges": []},
        ]
        from helm import eventledger
        for row in rows:
            self.assertTrue(eventledger.append(path, row))
        anchor, error = moneyread.last_good("fixture", account, path=path,
                                            before=1100)
        self.assertIsNone(error)
        self.assertEqual(anchor["measured_at"], 1000)
        self.assertEqual(anchor["windows"][0]["used_percent"], 20.0)

    def test_ledger_tokens_and_dollars_use_only_structured_breakdown(self):
        event = {"at": 1000, "source": "cred:deadbeef", "model": "m",
                 "tokens": 999999,
                 "token_breakdown": {"total_tokens": 50,
                                     "input": {"total_tokens": 10},
                                     "output": {"total_tokens": 5}}}
        complete = {"complete": True, "since": 900, "through": 1000}
        tokens = moneyread.ledger_reading(
            1000, [{"label": "tokens", "seconds": 100, "cap": 100,
                    "unit": "tokens"}], [event], complete, account="deadbeef")
        dollars = moneyread.ledger_reading(
            1000, [{"label": "dollars", "seconds": 100, "cap": 1,
                    "unit": "dollars"}], [event], complete,
            pricing={"m": {"prompt": "0.001", "completion": "0.002"}},
            account="deadbeef")
        self.assertEqual(tokens.windows[0]["used_percent"], 50.0)
        self.assertAlmostEqual(dollars.windows[0]["used_percent"], 2.0)

    def test_snapshot_and_history_paths_are_under_synthetic_helm_home(self):
        self.assertTrue(moneyread.snapshot_path().startswith(
            os.environ["HELM_HOME"] + os.sep))
        self.assertTrue(moneyread.history_path().startswith(
            os.environ["HELM_HOME"] + os.sep))
        snap = {"v": 1, "ts": 1000, "readings": []}
        self.assertTrue(moneyread.write_snapshot(snap))
        self.assertTrue(moneyread.append_history(snap))


# THE FAKE KEY CARRIES A MARKER no emitted byte may contain. The vendor's own
# /key reply echoes a masked label of the key, so the recorded shape below
# carries the marker there too: a reader that copied the label would leak it.
_MARKER = "LEAKMARKER"
_FAKE_KEY = "sk-or-v1-" + _MARKER + "-" + "q" * 44
# 18:34Z; the UTC day it sits in ends at _MIDNIGHT (a whole multiple of 86400)
_MIDNIGHT = 1790208000
_NOW = _MIDNIGHT - 86400 + 66840
# The recorded response shapes, from the vendor's API reference for
# GET /api/v1/key and GET /api/v1/credits (the "data" object each wraps).
_KEY_DATA = {"label": "sk-or-v1-" + _MARKER + "...qqqq", "limit": None,
             "limit_remaining": None, "limit_reset": None,
             "usage": 16.358006897, "usage_daily": 0, "usage_weekly": 0,
             "usage_monthly": 16.358006897, "is_free_tier": False,
             "is_provisioning_key": False, "expires_at": None,
             "rate_limit": {"requests": -1, "interval": "10s"},
             "free_model_daily_requests": {"used": 140, "limit": 1000,
                                           "remaining": 860}}
_CREDITS_DATA = {"total_credits": 50, "total_usage": 16.358006897}
# the catalog FAMILY key under test; its family seat carries the same name
_OR = "openrouter"  # noqa: SEAT_NAME — the catalog family key IS the subject of these arms, and its family seat is named after it
_FAMILIES = ("dots3", "ds4flash", _OR)


class OpenRouterKeyReaderTest(unittest.TestCase):
    """task/2936: the openrouter-key reader and its LEDGER minute, over a
    synthetic helm home and a fake key. The vendor is either a mocked
    transport or a loopback HTTP server; nothing leaves this host."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="helm-test-openrouter-key-")
        env = {"HOME": os.path.join(self.root, "home"),
               "HELM_HOME": os.path.join(self.root, "helm"),
               "HELM_ADOPTED_DIR": os.path.join(self.root, "adopted"),
               "TMPDIR": os.path.join(self.root, "tmp")}
        for path in env.values():
            os.makedirs(path)
        # a proxy in the node's environment must not carry a loopback GET
        loopback = "127.0.0.1,localhost"
        self.env = mock.patch.dict(os.environ, dict(
            env, no_proxy=loopback, NO_PROXY=loopback))
        self.env.start()
        self.calls = []
        self.account = moneyread.cred_ref(_FAKE_KEY).account

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def mint(self, family=_OR, key=_FAKE_KEY,
             base_url="https://openrouter.ai/api/v1"):
        """One seat config in the exact bytes the real mint door writes."""
        from helm import seat
        text = seat._config_yaml_key(8400, "seat-token", "openrouter-nex",
                                     base_url, "or-fast", key,
                                     "nex-agi/nex-n2.5-pro:free")
        path = os.path.join(seat.seat_dir(family), "config.yaml")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def vendor(self, key=None, credits=None, key_fail=None, credits_fail=None):
        """A fake transport answering the two GETs; records what it was sent."""
        def fake(url, secret, timeout=None):
            endpoint = url.rsplit("/", 1)[-1]
            self.calls.append((endpoint, secret == _FAKE_KEY))
            if endpoint == "key":
                return (None, key_fail) if key_fail else (
                    json.loads(json.dumps(key or _KEY_DATA)), None)
            return (None, credits_fail) if credits_fail else (
                json.loads(json.dumps(credits or _CREDITS_DATA)), None)
        return mock.patch.object(moneyread, "_http_json", side_effect=fake)

    def ledger(self, requests=(), prev_pid=4242, last_pid=4242,
               last_ts=_NOW + 5, status="READ"):
        """The proxy-usage ledger in the real producer's shapes: two read
        markers for the openrouter seat and the given popped records."""
        from helm import eventledger, proxy_usage
        path = proxy_usage.ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rows = [proxy_usage.read_event(_OR, _OR, prev_pid,
                                       8400, "READ", None, 0, _NOW - 900)]
        for n, (at, model, source) in enumerate(requests):
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(at))
            rows.append(proxy_usage.request_event(
                _OR, _OR, last_pid, 8400,
                {"timestamp": stamp, "source": source, "model": model,
                 "request_id": "r%d" % n}, last_ts))
        rows.append(proxy_usage.read_event(_OR, _OR,
                                           last_pid, 8400, status, None,
                                           len(requests), last_ts))
        for row in rows:
            self.assertTrue(eventledger.append(path, row))
        return path

    def fold(self, snap, family, now=_NOW):
        mapped = moneyread.inputs(snap, now=now, max_age_s=1800)
        axis = burnflags.derive_money(
            family, mapped["money"][family], ceiling=90,
            measured_at=mapped["money_measured_at"].get(family),
            fresh=mapped["money_fresh"][family])
        flag = burnflags.compose(family, {"money": axis}, now=now)
        with mock.patch.object(burnflags, "family_flag", return_value=flag):
            answer = burnflags.can_spend(family, now=now)["answer"]
        return mapped["money"][family], axis, answer

    @staticmethod
    def record(snap):
        rows = [r for r in snap["readings"] if r["reader"] == "openrouter-key"]
        return rows[0] if len(rows) == 1 else rows

    def serve(self, routes):
        """A loopback HTTP server: path -> (status, headers, body), read at
        request time so a case may change it. -> (base url, [(path, the
        Authorization header it was sent)])."""
        import http.server
        import threading
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                status, headers, body = routes.get(self.path, (404, {}, b""))
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # shutdown() waits one serve_forever poll (stdlib default 0.5s) — once
        # per test; see helm/mcpd.serve_background for the poll trade.
        threading.Thread(target=server.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d" % server.server_address[1], seen

    @staticmethod
    def reply(data):
        return (200, {"Content-Type": "application/json"},
                json.dumps({"data": data}).encode("utf-8"))

    def test_the_three_catalog_families_pick_up_the_reader(self):
        bindings = moneyread.catalog_bindings()
        self.assertEqual(bindings.get("openrouter-key"), _FAMILIES)
        for family in _FAMILIES:
            self.assertIn(family, burnflags.money_readers())
        impl = moneyread.READERS["openrouter-key"]
        self.assertTrue(all(callable(impl.get(k))
                            for k in ("creds", "probe", "rows")))
        # the paid rung is read off the catalog, never a family-name list
        self.assertEqual([moneyread._openrouter_paid(f) for f in _FAMILIES],
                         [False, True, False])

    def test_the_key_read_yields_the_free_day_window_with_the_utc_reset(self):  # noqa: VACUOUS_ASSERTION — the prepaid window's null reset and length ARE its contract, asserted after the same window's measured percent; each family iteration asserts its YELLOW colour and percent
        self.mint()
        self.ledger([(_NOW - 30, "nex-agi/nex-n2.5-pro:free", _FAKE_KEY)])
        with self.vendor():
            snap = moneyread.probe_snapshot(now=_NOW)
        self.assertEqual(sorted(set(self.calls)),
                         [("credits", True), ("key", True)])
        rec = self.record(snap)
        self.assertEqual((rec["status"], rec["account"], rec["families"]),
                         ("ok", self.account, list(_FAMILIES)))
        windows = {w["label"]: w for w in rec["windows"]}
        day = windows["1d-free"]
        self.assertEqual((day["used_percent"], day["reset_at"], day["seconds"],
                          day["unit"], day["source"]),
                         (14.0, _MIDNIGHT, 86400, "requests", "measured"))
        prepaid = windows["prepaid"]
        self.assertAlmostEqual(prepaid["used_percent"], 32.716013794)
        self.assertEqual((prepaid["reset_at"], prepaid["seconds"],
                          prepaid["unit"]), (None, None, "dollars"))
        self.assertAlmostEqual(rec["overflow"]["balance"], 33.641993103)
        self.assertEqual((rec["overflow"]["status"],
                          rec["overflow"]["measured_at"]), ("ok", _NOW))
        self.assertEqual(rec["expires_at"], _MIDNIGHT)
        # the reset is the NEXT 00:00Z, also from exactly midnight
        self.assertEqual(moneyread._next_utc_midnight(_MIDNIGHT),
                         _MIDNIGHT + 86400)
        self.assertEqual(moneyread._next_utc_midnight(_MIDNIGHT - 0.001),
                         _MIDNIGHT)
        # a successful read no longer reads GREY: the free families read
        # the measured day, ds4flash the measured prepaid balance
        for family, pct in ((_OR, 14.0), ("dots3", 14.0)):
            with self.subTest(family=family):
                rows, axis, answer = self.fold(snap, family)
                self.assertEqual([w["label"] for w in rows[0]["windows"]],
                                 ["1d-free"])
                self.assertEqual((axis["colour"], axis["cause_id"],
                                  axis["expires_at"], answer),
                                 (burnflags.YELLOW, "money:has-headroom",
                                  _MIDNIGHT, "yes"))
                self.assertEqual(rows[0]["longest_pct"], pct)
        rows, axis, answer = self.fold(snap, "ds4flash")
        self.assertEqual([w["label"] for w in rows[0]["windows"]], ["prepaid"])
        self.assertEqual((axis["colour"], answer), (burnflags.YELLOW, "yes"))

    def test_a_negative_balance_reads_walled_for_every_family(self):  # noqa: VACUOUS_ASSERTION — each iteration over the non-empty family tuple asserts WALLED and RED window-wall on the same axis before its null expiry
        self.mint()
        with self.vendor(credits={"total_credits": 50, "total_usage": 50.5}):
            snap = moneyread.probe_snapshot(now=_NOW)
        self.assertLess(self.record(snap)["overflow"]["balance"], 0)
        for family in _FAMILIES:
            with self.subTest(family=family):
                rows, axis, answer = self.fold(snap, family)
                row = burnflags._money_rows(rows)[0]
                self.assertEqual(burnflags._account_money(row, 90)["state"],
                                 burnflags.ACCOUNT_WALLED)
                self.assertEqual((axis["colour"], axis["cause_id"], answer),
                                 (burnflags.RED, "money:window-wall", "no"))
                # a prepaid wall has no vendor reset: the owner tops it up
                self.assertIsNone(axis["expires_at"])
        # CONTROL: a positive balance does not wall a free family, whose day
        # is the budget; the paid family still reads its spend
        with self.vendor(credits={"total_credits": 50, "total_usage": 49.0}):
            snap = moneyread.probe_snapshot(now=_NOW)
        self.assertEqual(self.fold(snap, _OR)[1]["colour"],
                         burnflags.YELLOW)
        self.assertEqual(self.fold(snap, "ds4flash")[1]["colour"],
                         burnflags.RED)

    def test_an_unreadable_meter_reads_unread_never_zero(self):  # noqa: VACUOUS_ASSERTION — each literal case asserts the exact unread:<class> status and the GREY unreadable axis on the same reading before its no-number and no-zero checks
        import socket
        routes = {}
        base, seen = self.serve(routes)
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        refused = "http://127.0.0.1:%d" % closed.getsockname()[1]
        closed.close()
        cases = (
            ("auth", base, (401, {}, b'{"error": {"code": 401}}')),
            ("network", refused, None),
            ("schema", base, (200, {}, b"<html>not json</html>")),
            ("schema", base, (200, {}, b'{"error": {"code": 500}}')),
        )
        for why, root, answer in cases:
            with self.subTest(why=why, answer=answer):
                routes["/api/v1/key"] = answer
                api = root + "/api/v1"
                self.mint(base_url=api)
                with mock.patch.object(moneyread, "OPENROUTER_API", api):
                    snap = moneyread.probe_snapshot(now=_NOW)
                rec = self.record(snap)
                self.assertEqual(rec["status"], "unread:" + why)
                for family in _FAMILIES:
                    rows, axis, answer_ = self.fold(snap, family)
                    self.assertIsNone(rows[0]["longest_pct"])
                    self.assertNotIn(0, [w.get("used_percent")
                                         for w in rows[0]["windows"]])
                    self.assertEqual((axis["colour"], axis["cause_id"],
                                      answer_),
                                     (burnflags.GREY, "money:unreadable",
                                      "unknown"))
        # every request that reached the vendor carried the key in its header
        self.assertEqual({auth for _path, auth in seen},
                         {"Bearer " + _FAKE_KEY})

    def test_a_real_transport_reply_reads_ok_with_the_day(self):
        base, seen = self.serve({"/api/v1/key": self.reply(_KEY_DATA),
                                 "/api/v1/credits": self.reply(_CREDITS_DATA)})
        api = base + "/api/v1"
        self.mint(base_url=api)
        with mock.patch.object(moneyread, "OPENROUTER_API", api):
            snap = moneyread.probe_snapshot(now=_NOW)
        rec = self.record(snap)
        self.assertEqual(rec["status"], "ok")
        windows = {w["label"]: w for w in rec["windows"]}
        self.assertEqual((windows["1d-free"]["used_percent"],
                          windows["1d-free"]["reset_at"]), (14.0, _MIDNIGHT))
        self.assertAlmostEqual(windows["prepaid"]["used_percent"], 32.716013794)
        self.assertEqual(seen, [("/api/v1/key", "Bearer " + _FAKE_KEY),
                                ("/api/v1/credits", "Bearer " + _FAKE_KEY)])

    def test_a_redirect_is_refused_and_the_key_goes_nowhere_else(self):  # noqa: VACUOUS_ASSERTION — the other origin's list is filled by the same handler class whose vendor list is asserted to hold both requests first; its emptiness IS the property
        stolen_base, stolen = self.serve({})
        base, seen = self.serve({"/api/v1/key": (
            302, {"Location": stolen_base + "/api/v1/key"}, b"")})
        api = base + "/api/v1"
        self.assertEqual(moneyread._http_json(api + "/key", _FAKE_KEY),
                         (None, "http-3xx"))
        self.mint(base_url=api)
        with mock.patch.object(moneyread, "OPENROUTER_API", api):
            snap = moneyread.probe_snapshot(now=_NOW)
        self.assertEqual(self.record(snap)["status"], "unread:http-3xx")
        self.assertEqual(self.fold(snap, _OR)[1]["colour"], burnflags.GREY)
        # the vendor was asked twice (once per read above); the other origin
        # was never sent anything
        self.assertEqual([path for path, _auth in seen],
                         ["/api/v1/key", "/api/v1/key"])
        self.assertEqual(stolen, [])

    def test_only_a_prepaid_window_may_have_no_length(self):
        self.assertIsNone(moneyread._window({"label": "7d",
                                             "used_percent": 50}))
        self.assertIsNone(moneyread._window({
            "label": "5h", "used_percent": 50, "unit": "requests",
            "reset_at": None}))
        # a missing length beside a reset is malformed, prepaid or not
        self.assertIsNone(moneyread._window({
            "label": "prepaid", "unit": "dollars", "used_percent": 5,
            "reset_at": 2000}))
        for raw in ({"label": "prepaid", "used_percent": 5},
                    {"label": "balance", "unit": "dollars",
                     "used_percent": 5}):
            with self.subTest(raw=raw):
                kept = moneyread._window(raw)
                self.assertEqual((kept["label"], kept["used_percent"],
                                  kept["seconds"], kept["reset_at"]),
                                 (raw["label"], 5, None, None))
        rec = moneyread.reading_dict({"status": "ok", "measured_at": 1,
                                      "windows": [
                                          {"label": "7d", "used_percent": 50},
                                          {"label": "prepaid",
                                           "unit": "dollars",
                                           "used_percent": 5}]})
        self.assertEqual([w["label"] for w in rec["windows"]], ["prepaid"])

    def test_a_hot_minute_stops_warning_once_its_own_minute_is_over(self):  # noqa: VACUOUS_ASSERTION — each literal instant asserts the exact window labels and colour on the same fold; the minute's 85% and its expiry are asserted unconditionally first
        self.mint()
        self.ledger([(_NOW - n, "nex-agi/nex-n2.5-pro:free", _FAKE_KEY)
                     for n in range(17)])                 # 17 of 20: 85%
        with self.vendor():
            snap = moneyread.probe_snapshot(now=_NOW)
        minute = {w["label"]: w for w in self.record(snap)["windows"]}["1m"]
        self.assertEqual((minute["used_percent"], minute["expires_at"]),
                         (85.0, _NOW + moneyread.LEDGER_FRESH_S))
        for at, colour, labels in (
                (_NOW, burnflags.ORANGE, ["1d-free", "1m"]),
                (_NOW + 60, burnflags.ORANGE, ["1d-free", "1m"]),
                (_NOW + 61, burnflags.YELLOW, ["1d-free"])):
            with self.subTest(at=at - _NOW):
                rows, axis, _answer = self.fold(snap, _OR, now=at)
                self.assertEqual([w["label"] for w in rows[0]["windows"]],
                                 labels)
                self.assertEqual(axis["colour"], colour)
        # the day is still the vendor's measurement: once the minute is over
        # the family may spend again, and the reading keeps the minute
        self.assertEqual(self.fold(snap, _OR, now=_NOW + 61)[2], "yes")
        self.assertIn("1m", [w["label"]
                             for w in self.record(snap)["windows"]])

    def test_credits_refused_keeps_the_last_value_stale_and_reads_unknown(self):  # noqa: VACUOUS_ASSERTION — each later pass asserts the stale status, the original age and the carried balance on the same record before its unread prepaid percent
        self.mint()
        with self.vendor():
            first = moneyread.refresh(now=_NOW)
        self.assertEqual(self.record(first)["overflow"]["status"], "ok")
        # a later pass whose /credits read is refused (403), after a restart:
        # the prior value comes back from the snapshot on disk
        for later in (_NOW + 900, _NOW + 1800):
            with self.subTest(later=later), \
                    self.vendor(credits_fail="auth"):
                snap = moneyread.refresh(now=later)
            rec = self.record(snap)
            self.assertEqual(rec["status"], "ok")
            over = rec["overflow"]
            self.assertEqual((over["status"], over["measured_at"]),
                             ("stale", _NOW),
                             "a stale value keeps its ORIGINAL age")
            self.assertAlmostEqual(over["balance"], 33.641993103)
            prepaid = {w["label"]: w for w in rec["windows"]}["prepaid"]
            self.assertIsNone(prepaid["used_percent"])
            for family in _FAMILIES:
                rows, axis, answer = self.fold(snap, family, now=later)
                self.assertEqual((axis["colour"], answer),
                                 (burnflags.GREY, "unknown"))
                self.assertIn("prepaid", axis["cause"])

    def test_the_minute_window_counts_only_free_requests_in_the_last_60s(self):
        path = self.ledger([
            (_NOW - 60, "nex-agi/nex-n2.5-pro:free", _FAKE_KEY),   # inclusive
            (_NOW - 30, "dots-studio/dots-3-note-preview:free", _FAKE_KEY),
            (_NOW, "nex-agi/nex-n2.5-pro:free", _FAKE_KEY),        # inclusive
            (_NOW - 61, "nex-agi/nex-n2.5-pro:free", _FAKE_KEY),   # too old
            (_NOW - 10, "deepseek/deepseek-v4-flash", _FAKE_KEY),  # paid
            (_NOW - 10, "nex-agi/nex-n2.5-pro:free", "sk-or-v1-other"),
            (_NOW + 1, "nex-agi/nex-n2.5-pro:free", _FAKE_KEY),    # future
        ])
        window = moneyread._free_minute(self.account, (_OR,), _NOW)
        self.assertEqual((window["label"], window["seconds"],
                          window["used_percent"], window["unit"],
                          window["source"]),
                         ("1m", 60, 15.0, "requests", "derived"))
        # a seat whose read markers name two sidecar lives cannot prove the
        # minute complete: unread, never a zero
        os.remove(path)
        self.ledger([(_NOW - 5, "nex-agi/nex-n2.5-pro:free", _FAKE_KEY)],
                    prev_pid=1, last_pid=2)
        broken = moneyread._free_minute(self.account, (_OR,), _NOW)
        self.assertEqual(broken["label"], "1m")
        self.assertIsNone(broken["used_percent"])
        # and a seat the ledger never read is not complete either
        missing = moneyread._free_minute(self.account, ("dots3",), _NOW)
        self.assertIsNone(missing["used_percent"])

    def test_a_hot_minute_warns_and_a_quiet_one_leaves_the_day_to_decide(self):
        self.mint()
        free = [(_NOW - n, "nex-agi/nex-n2.5-pro:free", _FAKE_KEY)
                for n in range(17)]                      # 17 of 20: 85%
        path = self.ledger(free)
        with self.vendor():
            hot = moneyread.probe_snapshot(now=_NOW)
        minute = {w["label"]: w for w in self.record(hot)["windows"]}["1m"]
        self.assertEqual(minute["used_percent"], 85.0)
        rows, axis, answer = self.fold(hot, _OR)
        self.assertIn("1m", [w["label"] for w in rows[0]["windows"]])
        self.assertEqual((axis["colour"], axis["cause_id"], answer),
                         (burnflags.ORANGE, "money:estimated", "unknown"))
        self.assertIn("1m", axis["cause"])
        # the paid family is not held by the free-model minute
        self.assertEqual(self.fold(hot, "ds4flash")[1]["colour"],
                         burnflags.YELLOW)
        # a derived minute can never wall, even at its cap
        os.remove(path)
        self.ledger([(_NOW - n / 2.0, "nex-agi/nex-n2.5-pro:free", _FAKE_KEY)
                     for n in range(25)])
        with self.vendor():
            capped = moneyread.probe_snapshot(now=_NOW)
        self.assertEqual(self.fold(capped, "dots3")[1]["colour"],
                         burnflags.ORANGE)
        # quiet (and unreadable) minutes stay in the reading, not the verdict
        os.remove(path)
        self.ledger(free[:3])
        with self.vendor():
            quiet = moneyread.probe_snapshot(now=_NOW)
        labels = [w["label"] for w in self.record(quiet)["windows"]]
        self.assertEqual(labels, ["1d-free", "prepaid", "1m"])
        self.assertEqual(self.fold(quiet, _OR)[1]["colour"],
                         burnflags.YELLOW)

    def test_one_key_on_several_seats_is_read_once_and_only_at_openrouter(self):
        self.mint(_OR)
        self.mint("dots3")
        with self.vendor():
            snap = moneyread.probe_snapshot(now=_NOW)
        self.assertEqual(sorted(self.calls),
                         [("credits", True), ("key", True)])
        self.assertEqual(self.record(snap)["account"], self.account)
        # a bound seat whose block points anywhere else never lends its key
        self.mint("dots3", key="sk-other-" + _MARKER,
                  base_url="https://api.example.test/v1")
        self.calls[:] = []
        with self.vendor():
            snap = moneyread.probe_snapshot(now=_NOW)
        self.assertEqual(sorted(self.calls),
                         [("credits", True), ("key", True)])
        self.assertEqual(self.record(snap)["account"], self.account)
        # no seat holding the key: unread, and nothing is sent anywhere
        from helm import seat
        for family in (_OR, "dots3"):
            os.remove(os.path.join(seat.seat_dir(family), "config.yaml"))
        self.calls[:] = []
        with self.vendor():
            snap = moneyread.probe_snapshot(now=_NOW)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.record(snap)["status"],
                         "unread:credentials-unavailable")

    def test_no_key_bytes_reach_any_output(self):  # noqa: VACUOUS_ASSERTION — the same joined text is asserted to carry the account hash, the 1d-free window and the unread:auth status before the key and its marker are asserted absent
        import contextlib
        import io
        config = self.mint()
        with open(config, encoding="utf-8") as f:
            self.assertIn(_FAKE_KEY, f.read(), "the fixture holds the key")
        self.ledger([(_NOW - 5, "nex-agi/nex-n2.5-pro:free", _FAKE_KEY)])
        out, err = io.StringIO(), io.StringIO()
        emitted = []
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            creds = moneyread.READERS["openrouter-key"]["creds"](_FAMILIES)
            emitted += [repr(creds), str(creds), repr(creds[0].value)]
            with self.vendor():
                good = moneyread.refresh(now=_NOW)
            with self.vendor(credits_fail="auth"):
                stale = moneyread.refresh(now=_NOW + 900)
            with self.vendor(key_fail="auth"):
                failed = moneyread.refresh(now=_NOW + 1800)
            reading = moneyread.READERS["openrouter-key"]["probe"](
                creds[0], _NOW)
            emitted.append(repr(reading))
            for snap in (good, stale, failed):
                mapped = moneyread.inputs(snap, now=_NOW, max_age_s=1800)
                emitted.append(json.dumps(snap, sort_keys=True))
                emitted.append(json.dumps(mapped, sort_keys=True))
                emitted.append(json.dumps(burnflags.fold(
                    dict(mapped, ceiling=90), now=_NOW), sort_keys=True))
        with open(moneyread.snapshot_path(), encoding="utf-8") as f:
            emitted.append(f.read())
        with open(moneyread.history_path(), encoding="utf-8") as f:
            emitted.append(f.read())
        emitted += [out.getvalue(), err.getvalue()]
        text = "\n".join(emitted)
        # POSITIVE CONTROL on the same text: the readings are all there,
        # joined by the key's hash
        self.assertIn(self.account, text)
        self.assertIn('"1d-free"', text)
        self.assertIn("unread:auth", text)
        self.assertNotIn(_FAKE_KEY, text)
        self.assertNotIn(_MARKER, text)
        self.assertEqual(creds[0].account, self.account)
        self.assertEqual(creds[0].value["key"].reveal(), _FAKE_KEY)


if __name__ == "__main__":
    unittest.main()
