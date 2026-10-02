"""helm.burnspend — one seat's own spend, joined to its account's weekly %.

task/4015: `helm burn spend` shows, for the seat's own sessions, the calls they
made and the context they re-read versus output, deduped by message.id and
priced per model, beside the weekly percent of the account the seat's home
holds, over that account's weekly window.

EVERY TEST READS ONLY WHAT IT PLANTS. The seat's config home is patched to a
temp dir, the snapshot is passed in or stubbed, and the session id is named, so
no test reads the real ~/.claude or the live claudepace snapshot.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import accounts, burnflags, burnspend  # noqa: E402
from tests._tmphome import pin_live_seats  # noqa: E402

EMAIL = "seat-owner@example.test"


def setUpModule():
    pin_live_seats()


def _iso(at):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(at))


def _entry(mid, at, model=burnspend.OPUS_55, **usage):
    usage = dict({"input_tokens": 1, "cache_read_input_tokens": 0,
                  "output_tokens": 1}, **usage)
    return {"type": "assistant", "timestamp": None if at is None else _iso(at),
            "message": {"id": mid, "model": model, "usage": usage}}


def _write(path, entries):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


class _Home(unittest.TestCase):
    """A planted config home: an account file and one project dir."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.join(tmp.name, "claude-home")
        self.project = os.path.join(self.home, "projects", "-work-repo")
        os.makedirs(self.project)
        with open(os.path.join(self.home, ".claude.json"), "w") as fh:
            json.dump({"oauthAccount": {"emailAddress": EMAIL}}, fh)
        patcher = mock.patch.object(burnspend, "config_home",
                                    return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = time.time()

    def log(self, sid, entries, *sub):
        path = os.path.join(self.project, *(sub or (sid + ".jsonl",)))
        _write(path, entries)
        return path


class PriceUsageTest(unittest.TestCase):
    """price_usage splits usage into the buckets the task names, and prices
    only the models helm catalogs."""

    def _priced(self, model, **usage):
        return burnspend.price_usage(burnspend.usage_records(
            [_entry("msg_a", None, model=model, **usage)]))

    def test_buckets_are_split_by_fresh_cache_read_cache_write_and_output(self):
        b = self._priced(burnspend.OPUS_55, input_tokens=100,
                         cache_read_input_tokens=200,
                         cache_creation_input_tokens=300, output_tokens=40,
                         output_tokens_details={"thinking_tokens": 10},
                         cache_creation={"ephemeral_5m_input_tokens": 120,
                                         "ephemeral_1h_input_tokens": 180})
        self.assertEqual((b["fresh_input"], b["cache_read"],
                          b["cache_creation_5m"], b["cache_creation_1h"],
                          b["cache_creation_unsplit"], b["output"],
                          b["thinking"]), (100, 200, 120, 180, 0, 40, 10))
        # 120 at 1.25x, 180 at 2x.
        self.assertEqual(b["cache_creation_5m_equivalent"], 150)
        self.assertEqual(b["cache_creation_1h_equivalent"], 360)

    def test_opus_5_5_reads_at_its_own_rate_and_is_a_known_model(self):
        # The real id is dashed: `claude-opus-5-5`, also with its [1m] tag.
        b = self._priced("claude-opus-5-5", cache_read_input_tokens=200)
        self.assertEqual(b["cache_read"], 200)
        self.assertEqual(b["cache_read_equivalent"], 10)
        self.assertEqual(b["unpriced_records"], 0)
        self.assertNotIn(burnspend.model_id("claude-opus-5-5"), b["unknown_models"])
        b = self._priced("claude-opus-5-5[1m]", cache_read_input_tokens=200)
        self.assertEqual(b["cache_read"], 200)
        self.assertEqual(b["cache_read_equivalent"], 10)
        self.assertEqual(b["unpriced_records"], 0)
        self.assertNotIn(burnspend.model_id("claude-opus-5-5[1m]"), b["unknown_models"])

    def test_other_catalogued_models_read_at_a_tenth_and_fable_at_its_rate(self):
        self.assertEqual(self._priced("claude-opus-5",
                                      cache_read_input_tokens=200)
                         ["cache_read_equivalent"], 20)
        self.assertEqual(self._priced("claude-fable-5-1",
                                      cache_read_input_tokens=200)
                         ["cache_read_equivalent"], 5)

    def test_an_uncatalogued_model_is_unknown_and_never_priced(self):
        b = self._priced("claude-opus-4-8", cache_read_input_tokens=100,
                         cache_creation={"ephemeral_5m_input_tokens": 40})
        self.assertEqual(b["unknown_models"], ["claude-opus-4-8"])
        self.assertEqual(b["unpriced_records"], 1)
        self.assertEqual(b["cache_read"], 100)
        self.assertEqual(b["cache_read_equivalent"], 0)
        self.assertEqual(b["cache_creation_5m_equivalent"], 0)

    def test_a_write_the_log_does_not_split_is_counted_with_unknown_rate(self):
        b = self._priced(burnspend.OPUS_55, cache_creation_input_tokens=50)
        self.assertEqual(b["cache_creation_unsplit"], 50)
        self.assertIsNone(b["cache_creation_unsplit_equivalent"])
        # Only the part the split does not cover is unsplit.
        b = self._priced(burnspend.OPUS_55, cache_creation_input_tokens=70,
                         cache_creation={"ephemeral_5m_input_tokens": 50})
        self.assertEqual((b["cache_creation_5m"], b["cache_creation_unsplit"]),
                         (50, 20))


class DedupeByIdTest(unittest.TestCase):
    """usage_records dedupes by message.id, not by api block."""

    def test_entries_sharing_message_id_count_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "proj", "abc.jsonl")
            rec = _entry("msg_dup", None)
            _write(log, [rec, dict(rec)])
            self.assertEqual(len(burnspend.usage_records([log])), 1)


class RenderTest(unittest.TestCase):
    """The human lines never crash on an unmeasured weekly percent."""

    def _reading(self):
        return {"records": 1, "sessions": ["s1"], "account": "acct",
                "window": "the last 7 days",
                "buckets": burnspend.price_usage(burnspend.usage_records(
                    [_entry("m", None)]))}

    def test_an_unmeasured_weekly_percent_reads_not_measured(self):
        lines = burnspend.render(self._reading(), weekly_pct=None)
        self.assertIn("  weekly pace: not measured", lines)

    def test_a_measured_weekly_percent_is_printed(self):
        lines = burnspend.render(self._reading(), weekly_pct=42.0,
                                 weekly_reset_at=time.time() + 3600)
        self.assertTrue(any("42% of the week used" in line for line in lines))


class AccountJoinTest(_Home):
    """The account is the one the seat's own home holds, keyed as claudepace
    keys its snapshot, so the weekly percent joins."""

    def test_the_home_account_joins_the_snapshot_weekly_percent(self):
        self.log("s1", [_entry("m1", self.now - 60)])
        key = accounts.measured_key(EMAIL)
        snap = {"accounts": {key: {"weekly_pct": 42.0,
                                   "weekly_reset_at": self.now + 86400}}}
        reading = burnspend.spend_reading(["s1"], now=self.now, snap=snap)
        self.assertEqual(reading["account"], key)
        self.assertEqual(reading["weekly_pct"], 42.0)
        self.assertNotEqual(reading["account"], self.home)

class ConfigHomeTest(unittest.TestCase):
    """The seat's home is its own CLAUDE_CONFIG_DIR, not the default home."""

    def test_the_home_is_the_seats_config_dir(self):  # noqa: VACUOUS_ASSERTION — the observable is a non-empty path compared for equality with the planted CLAUDE_CONFIG_DIR
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/x/seat-home"}):
            self.assertEqual(burnspend.config_home(),
                             os.path.realpath("/x/seat-home"))


class WindowTest(_Home):
    """The spend covers the account's weekly window, by each message's own
    timestamp, never the logs' whole lifetime."""

    def test_a_message_before_the_weekly_window_is_left_out(self):
        self.log("s1", [_entry("old", self.now - 10 * 86400),
                        _entry("new", self.now - 3600),
                        _entry("untimed", None)])
        key = accounts.measured_key(EMAIL)
        snap = {"accounts": {key: {"weekly_pct": 10.0,
                                   "weekly_reset_at": self.now + 86400}}}
        reading = burnspend.spend_reading(["s1"], now=self.now, snap=snap)
        self.assertEqual(reading["records"], 1)
        self.assertEqual(reading["untimed"], 1)
        self.assertEqual(reading["since"],
                         burnspend._iso(self.now + 86400 - burnspend.WEEK_S))
        # With no measured reset: the last seven days, said so.
        reading = burnspend.spend_reading(["s1"], now=self.now, snap={})
        self.assertEqual(reading["records"], 1)
        self.assertIn("not measured", reading["window"])


class SessionScopeTest(_Home):
    """Per-seat means the seat's own sessions, never every session in the
    project dir."""

    def test_another_session_in_the_same_project_dir_is_not_summed(self):
        self.log("mine", [_entry("a", self.now - 60)])
        self.log("theirs", [_entry("b", self.now - 60),
                            _entry("c", self.now - 60)])
        self.log("mine", [_entry("sub", self.now - 60)],
                 "mine", "subagents", "agent-1.jsonl")
        reading = burnspend.spend_reading(["mine"], now=self.now, snap={})
        self.assertEqual(reading["records"], 2)
        self.assertEqual(reading["sessions"], ["mine"])

    def test_the_default_session_is_the_running_one(self):
        self.log("mine", [_entry("a", self.now - 60)])
        self.log("theirs", [_entry("b", self.now - 60)])
        from helm import home
        with mock.patch.object(home, "session_id", return_value="mine"):
            reading = burnspend.spend_reading(now=self.now, snap={})
        self.assertEqual((reading["sessions"], reading["records"]),
                         (["mine"], 1))
        with mock.patch.object(home, "session_id", return_value=None):
            reading = burnspend.spend_reading(now=self.now, snap={})
        self.assertFalse(reading["source_ok"])
        self.assertIn("no session id", reading["why"])


class SpendVerbTest(_Home):
    """The spend subcommand is wired into `helm burn` and reads only the
    planted home."""

    def _run(self, *args):
        out = io.StringIO()
        with mock.patch("helm.claudepace.cached", return_value=None), \
                contextlib.redirect_stdout(out):
            rc = burnflags.cmd_burn(("spend",) + args)
        return rc, out.getvalue()

    def test_spend_prints_the_seats_spend_without_a_weekly_reading(self):
        self.log("s1", [_entry("m1", self.now - 60)])
        rc, text = self._run("--session", "s1")
        self.assertEqual(rc, 0)
        self.assertIn("1 assistant message(s) in 1 session(s)", text)
        self.assertIn("weekly pace: not measured", text)

    def test_spend_json_carries_the_account_and_window(self):
        self.log("s1", [_entry("m1", self.now - 60)])
        rc, text = self._run("--json", "--session", "s1")
        self.assertEqual(rc, 0)
        doc = json.loads(text)
        self.assertEqual(doc["account"], accounts.measured_key(EMAIL))
        self.assertEqual(doc["records"], 1)

    def test_an_unknown_argument_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self._run("--jsno")[0], 2)


if __name__ == "__main__":
    unittest.main()
