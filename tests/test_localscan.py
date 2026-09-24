#!/usr/bin/env python3
"""localscan tests — the local-session-scan second source. Hermetic: stores
are tmp dirs of planted JSONL, the provider is a stub; no real store, home,
or vendor endpoint is ever touched."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from helm import localscan, providers


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def rec_line(ts, model="claude-opus-4-8", out=100, cache=0, uuid=None):
    return json.dumps({"type": "assistant", "timestamp": iso(ts), "uuid": uuid,
                       "message": {"model": model,
                                   "usage": {"input_tokens": 25000,
                                             "cache_creation_input_tokens": cache,
                                             "cache_read_input_tokens": 24000,
                                             "output_tokens": out}}})


NOW = 1_800_000_000  # a fixed clock: window math must never read time.time()


class ParseTest(unittest.TestCase):
    def test_effort_is_output_plus_cache_creation_only(self):
        # raw input_tokens (cumulative context) and cache_read are IGNORED
        r = localscan.parse_record(rec_line(NOW, model="claude-fable-5",
                                            out=490, cache=42978, uuid="u1"))
        self.assertEqual(r["effort"], 490 + 42978)
        self.assertTrue(r["fable"])
        self.assertEqual(r["uuid"], "u1")

    def test_non_fable_model(self):
        r = localscan.parse_record(rec_line(NOW, model="claude-opus-4-8", out=100, cache=5))
        self.assertFalse(r["fable"])
        self.assertEqual(r["effort"], 105)

    def test_ignores_non_assistant_malformed_and_timestampless(self):
        self.assertIsNone(localscan.parse_record(
            '{"type":"user","message":{"role":"user"}}'))
        self.assertIsNone(localscan.parse_record('{"type":"assistant"}'))
        self.assertIsNone(localscan.parse_record("not json"))
        self.assertIsNone(localscan.parse_record(
            '{"type":"assistant","timestamp":"garbage","message":{}}'))

    def test_missing_usage_is_zero_effort_not_a_crash(self):
        r = localscan.parse_record(json.dumps(
            {"type": "assistant", "timestamp": iso(NOW), "message": {"model": "m"}}))
        self.assertEqual(r["effort"], 0)


class WindowsTest(unittest.TestCase):
    def rec(self, secs_ago, fable, effort):
        return {"ts": NOW - secs_ago, "fable": fable, "effort": effort}

    def test_buckets_by_age_and_fable_subset(self):
        recs = [self.rec(3600, False, 10),        # 1h -> 5h + 7d
                self.rec(4 * 3600, True, 20),     # 4h fable -> 5h + 7d + fable
                self.rec(6 * 3600, False, 40),    # 6h -> 7d only
                self.rec(2 * 86400, True, 80),    # 2d fable -> 7d + fable
                self.rec(8 * 86400, False, 160)]  # 8d -> outside entirely
        w = localscan.sum_windows(recs, NOW)
        self.assertEqual(w["session_5h"], 30)
        self.assertEqual(w["weekly_7d"], 150)
        self.assertEqual(w["fable_7d"], 100)
        self.assertEqual(w["records"], 4)

    def test_future_records_skipped_not_counted(self):
        w = localscan.sum_windows(
            [self.rec(-3600, False, 999), self.rec(60, False, 5)], NOW)
        self.assertEqual((w["session_5h"], w["weekly_7d"], w["records"]), (5, 5, 1))

    def test_empty_scan_is_zero(self):
        w = localscan.sum_windows([], NOW)
        self.assertEqual(w, {"session_5h": 0, "weekly_7d": 0, "fable_7d": 0,
                             "records": 0})


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-localscan-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def plant(self, store, slug, name, lines):
        d = os.path.join(self.tmp, store, slug)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        with open(p, "w") as f:
            f.write("\n".join(lines) + "\n")
        return p

    def test_uuid_dedup_counts_a_doubly_read_line_once(self):
        now = time.time()
        line = rec_line(now - 60, out=100, uuid="dup")
        self.plant("projects", "slug-a", "s.jsonl", [line])
        self.plant("projects", "slug-b", "pruned-copy.jsonl", [line])
        sums, _ = localscan.scan_projects(os.path.join(self.tmp, "projects"), now)
        self.assertEqual(sums["weekly_7d"], 100)
        self.assertEqual(sums["records"], 1)

    def test_mtime_bound_skips_stale_files(self):
        now = time.time()
        # the record LOOKS in-window but the file's mtime is 8d old -> skipped
        p = self.plant("projects", "slug", "old.jsonl", [rec_line(now - 60, out=7)])
        old = now - 8 * 86400
        os.utime(p, (old, old))
        sums, newest = localscan.scan_projects(os.path.join(self.tmp, "projects"), now)
        self.assertEqual(sums["records"], 0)
        self.assertIsNone(newest)

    def test_shared_store_collapses_isolated_store_stays_separate(self):
        now = time.time()
        self.plant("shared/projects", "slug", "s.jsonl",
                   [rec_line(now - 60, out=100, uuid="s1")])
        self.plant("iso-home/projects", "slug", "i.jsonl",
                   [rec_line(now - 60, out=42, uuid="i1")])
        for h in ("home-a", "home-b"):  # two homes symlink onto the shared store
            os.makedirs(os.path.join(self.tmp, h))
            os.symlink(os.path.join(self.tmp, "shared", "projects"),
                       os.path.join(self.tmp, h, "projects"))
        accounts = [
            {"name": "a@x.example", "provider": "anthropic", "home": os.path.join(self.tmp, "home-a")},
            {"name": "b@x.example", "provider": "anthropic", "home": os.path.join(self.tmp, "home-b")},
            {"name": "iso@x.example", "provider": "anthropic", "home": os.path.join(self.tmp, "iso-home")},
            {"name": "cx", "provider": "codex", "home": os.path.join(self.tmp, "cx-home")}]
        rows = localscan.scan_stores(accounts, now)
        self.assertEqual(len(rows), 2)  # shared collapses; codex never scans
        shared = next(r for r in rows if r["shared"])
        self.assertEqual(shared["accounts"], ["a@x.example", "b@x.example"])
        self.assertEqual(shared["weekly_7d"], 100)  # counted once, not per-account
        iso_row = next(r for r in rows if not r["shared"])
        self.assertEqual(iso_row["accounts"], ["iso@x.example"])
        self.assertEqual(iso_row["weekly_7d"], 42)


class StubProvider:
    def __init__(self, accounts, history):
        self._accounts, self._history = accounts, history
        self.probed = 0

    def accounts(self):
        return self._accounts

    def cred_state(self):
        self.probed += 1
        return []

    def history(self, hours):
        return self._history


def hist(account, h5, h7=None, fable=None, probed_at="2026-01-01T00:00:00Z"):
    gauges = [{"label": "5h", "kind": "session", "utilization": h5, "reset": None}]
    if h7 is not None:
        gauges.append({"label": "7d", "kind": "period", "utilization": h7, "reset": None})
    if fable is not None:
        gauges.append({"label": "7d-fable", "kind": "period", "utilization": fable,
                       "reset": None})
    return {"account": account, "provider": "anthropic", "probed_at": probed_at,
            "gauges": gauges}


class CrosscheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-crosscheck-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def home_with(self, name, lines):
        d = os.path.join(self.tmp, name, "projects", "slug")
        os.makedirs(d)
        with open(os.path.join(d, "s.jsonl"), "w") as f:
            f.write("\n".join(lines) + "\n")
        return os.path.join(self.tmp, name)

    def acct(self, name, home):
        return {"name": name, "provider": "anthropic", "home": home}

    def test_agreeing_sources_are_plausible_and_probe_happens_once(self):
        now = time.time()
        home = self.home_with("busy", [rec_line(now - 60, out=100, uuid="x")])
        prov = StubProvider([self.acct("busy@x.example", home)],
                            [hist("busy@x.example", 0.34, 0.12, 0.08)])
        rows = localscan.crosscheck(prov, now)
        self.assertEqual(prov.probed, 1)
        self.assertIn("plausible", rows[0]["signal"])
        self.assertEqual(rows[0]["header"],
                         {"session_5h": 0.34, "weekly_7d": 0.12, "fable_7d": 0.08})

    def test_header_active_scan_empty_is_drift(self):
        home = os.path.join(self.tmp, "empty")
        os.makedirs(os.path.join(home, "projects"))
        prov = StubProvider([self.acct("ghost@x.example", home)],
                            [hist("ghost@x.example", 0.55)])
        rows = localscan.crosscheck(prov, time.time())
        self.assertIn("DRIFT header-active/scan-empty", rows[0]["signal"])
        self.assertIn("55%", rows[0]["signal"])

    def test_scan_active_header_idle_is_drift(self):
        now = time.time()
        home = self.home_with("local", [rec_line(now - 60, out=1234, uuid="y")])
        prov = StubProvider([self.acct("local@x.example", home)],
                            [hist("local@x.example", 0.0)])
        rows = localscan.crosscheck(prov, now)
        self.assertIn("DRIFT scan-active/header-idle", rows[0]["signal"])

    def test_freshest_history_row_wins(self):
        now = time.time()
        home = self.home_with("h", [rec_line(now - 60, out=10, uuid="z")])
        prov = StubProvider(
            [self.acct("h@x.example", home)],
            [hist("h@x.example", 0.0, probed_at="2026-01-01T00:00:00Z"),
             hist("h@x.example", 0.20, probed_at="2026-01-02T00:00:00Z")])
        rows = localscan.crosscheck(prov, now)
        self.assertIn("plausible", rows[0]["signal"])  # the fresher 20% row won

    def test_shared_store_is_commingled_never_a_per_account_verdict(self):
        now = time.time()
        shared = os.path.join(self.tmp, "store", "projects")
        os.makedirs(os.path.join(shared, "slug"))
        with open(os.path.join(shared, "slug", "s.jsonl"), "w") as f:
            f.write(rec_line(now - 60, out=5, uuid="s") + "\n")
        homes = []
        for h in ("ha", "hb"):
            os.makedirs(os.path.join(self.tmp, h))
            os.symlink(shared, os.path.join(self.tmp, h, "projects"))
            homes.append(os.path.join(self.tmp, h))
        prov = StubProvider([self.acct("a@x.example", homes[0]),
                             self.acct("b@x.example", homes[1])],
                            [hist("a@x.example", 0.9), hist("b@x.example", 0.9)])
        rows = localscan.crosscheck(prov, now)
        self.assertEqual(len(rows), 1)
        self.assertIn("commingled", rows[0]["signal"])
        self.assertNotIn("header", rows[0])

    def test_subthreshold_header_with_empty_scan_is_not_agreement(self):
        home = os.path.join(self.tmp, "quiet")
        os.makedirs(os.path.join(home, "projects"))
        prov = StubProvider([self.acct("quiet@x.example", home)],
                            [hist("quiet@x.example", 0.05)])
        rows = localscan.crosscheck(prov, time.time())
        self.assertIn("no strong directional drift", rows[0]["signal"])
        self.assertNotIn("agree on activity", rows[0]["signal"])

    def test_no_header_observation(self):
        now = time.time()
        home = self.home_with("h", [rec_line(now - 60, out=10, uuid="q")])
        prov = StubProvider([self.acct("h@x.example", home)], [])
        rows = localscan.crosscheck(prov, now)
        self.assertIn("no header observation", rows[0]["signal"])


class CmdTest(unittest.TestCase):
    def run_cmd(self, prov, args=()):
        out = io.StringIO()
        with mock.patch.object(providers, "default_provider", return_value=prov), \
                contextlib.redirect_stdout(out):
            rc = localscan.cmd_crosscheck(list(args))
        return rc, out.getvalue()

    def test_cmd_renders_scan_header_and_signal(self):
        tmp = tempfile.mkdtemp(prefix="helm-test-cc-cmd-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        now = time.time()
        d = os.path.join(tmp, "h", "projects", "slug")
        os.makedirs(d)
        with open(os.path.join(d, "s.jsonl"), "w") as f:
            f.write(rec_line(now - 60, model="claude-fable-5", out=1500, uuid="c") + "\n")
        prov = StubProvider(
            [{"name": "h@x.example", "provider": "anthropic", "home": os.path.join(tmp, "h")}],
            [hist("h@x.example", 0.34, 0.12, 0.08)])
        rc, out = self.run_cmd(prov)
        self.assertEqual(rc, 0)
        self.assertIn("helm creds crosscheck", out)
        self.assertIn("h@x.example", out)
        self.assertIn("scan:   5h 1.5k | 7d 1.5k | fable-7d 1.5k tokens over 1 records", out)
        self.assertIn("header: 5h 34% | 7d 12% | fable-7d 8%", out)
        self.assertIn("plausible", out)
        rc, out = self.run_cmd(prov, ["--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)[0]["accounts"], ["h@x.example"])

    def test_cmd_no_stores(self):
        rc, out = self.run_cmd(StubProvider([], []))
        self.assertEqual(rc, 0)
        self.assertIn("no anthropic session stores", out)

    def test_cmd_provider_error_degrades(self):
        class Broken:
            def cred_state(self):
                raise providers.ProviderError("no CLI")
        rc, out = self.run_cmd(Broken())
        self.assertEqual(rc, 1)
        self.assertIn("sessions/resume still work", out)

    def test_creds_verb_dispatches_crosscheck(self):
        from helm import creds
        rc, out = None, io.StringIO()
        with mock.patch.object(localscan, "cmd_crosscheck",
                               return_value=0) as cc, \
                contextlib.redirect_stdout(out):
            rc = creds.cmd_creds(["crosscheck", "--json"])
        self.assertEqual(rc, 0)
        cc.assert_called_once_with(["--json"])


class FmtTest(unittest.TestCase):
    def test_fmt_tokens(self):
        self.assertEqual(localscan.fmt_tokens(0), "0")
        self.assertEqual(localscan.fmt_tokens(999), "999")
        self.assertEqual(localscan.fmt_tokens(1500), "1.5k")
        self.assertEqual(localscan.fmt_tokens(45_000), "45k")
        self.assertEqual(localscan.fmt_tokens(1_200_000), "1.2M")
        self.assertEqual(localscan.fmt_tokens(45_000_000), "45M")


if __name__ == "__main__":
    unittest.main()
