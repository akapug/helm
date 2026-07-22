#!/usr/bin/env python3
"""helm ownerasks — the owner-ask ledger + its stop-whisper top rung.
Hermetic: tmp HELM_HOME/HELM_CHAT_DIR, transport killed, ambient session ids
scrubbed (the seats-test law)."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import home, ownerasks, pk, record, seats  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_ROOM", "MELD_CHAT_ROOM",
            "HELM_CHAT_OWNER_NAMES", "HELM_CHAT_DELIVER",
            "HELM_STOP_GUARD", "HELM_STOP_GUARD_INBOX",
            "HELM_STOP_GUARD_CLAIMS", "HELM_STOP_GUARD_INDEX",
            "HELM_STOP_GUARD_WHISPER",
            "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR", "HELM_ACTOR",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "CLAUDECODE", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL")


class AsksBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-asks-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_OWNER_NAMES"] = "david"
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def asks(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ownerasks.cmd_asks(list(args))
        return rc, out.getvalue(), err.getvalue()


class LedgerTest(AsksBase):
    def test_add_opens_a_durable_row(self):
        row = ownerasks.add("wire the exit briefing", source="chat-7")
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["source"], "chat-7")
        self.assertIsNone(row["done_ref"])
        p = ownerasks.ledger_path()
        self.assertEqual(p, os.path.join(home.global_dir(), "owner-asks.jsonl"))
        self.assertTrue(os.path.exists(p))
        self.assertEqual(ownerasks.rows()[row["id"]]["ask"],
                         "wire the exit briefing")

    def test_source_defaults_to_cli_floor(self):
        self.assertEqual(ownerasks.add("x")["source"], "cli")  # ids scrubbed

    def test_lifecycle_done_then_report_snapshots_append_only(self):
        row = ownerasks.add("ship the ledger")
        rid = row["id"]
        done, why = ownerasks.mark_done(rid, "abc1234")
        self.assertIsNone(why)
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["done_ref"], "abc1234")
        # done is NOT closed — the owner has not heard it (owner-surface bar)
        self.assertEqual([r["id"] for r in ownerasks.unreported()], [rid])
        rep, why = ownerasks.mark_report(rid, "post-42")
        self.assertIsNone(why)
        self.assertEqual(rep["status"], "reported")
        self.assertEqual(rep["report_ref"], "post-42")
        self.assertEqual(rep["done_ref"], "abc1234")   # snapshot carries history
        self.assertEqual(ownerasks.unreported(), [])
        with open(ownerasks.ledger_path(), encoding="utf-8") as f:
            lines = [json.loads(l) for l in f]
        self.assertEqual(len(lines), 3)                # append-only, never rewrite
        self.assertEqual([l["status"] for l in lines],
                         ["open", "done", "reported"])

    def test_report_alone_closes_and_reported_is_terminal(self):
        rid = ownerasks.add("just tell me when the deploy lands")["id"]
        rep, why = ownerasks.mark_report(rid, "post-9")   # report without done: legal
        self.assertIsNone(why)
        self.assertEqual(rep["status"], "reported")
        _r, why = ownerasks.mark_done(rid, "sha")         # closed rows stay closed
        self.assertIn("already reported", why)

    def test_guards_refuse_empty_refs_and_unknown_ids(self):
        rid = ownerasks.add("a")["id"]
        self.assertIn("evidence", ownerasks.mark_done(rid, "  ")[1])
        self.assertIn("chat-post id", ownerasks.mark_report(rid, "")[1])
        self.assertIn("no such ask", ownerasks.mark_done("nope", "sha")[1])
        self.assertIsNone(ownerasks.add("   "))           # empty ask never lands

    def test_fail_open_on_unwritable_ledger(self):
        os.makedirs(os.path.dirname(home.global_dir()), exist_ok=True)
        with open(home.global_dir(), "w", encoding="utf-8") as f:
            f.write("not a dir")                          # _global is a FILE
        self.assertIsNone(ownerasks.add("lost?"))         # no raise, no lie
        self.assertEqual(ownerasks.rows(), {})            # reads fail open too
        rc, _o, err = self.asks("add", "lost?")
        self.assertEqual(rc, 1)
        self.assertIn("NOT recorded", err)                # loud, never silent

    def test_cli_lifecycle_done_stays_open_until_report(self):
        rc, out, _e = self.asks("add", "fix", "the", "flaky", "test")
        self.assertEqual(rc, 0)
        rid = out.split()[1]
        rc, out, _e = self.asks("done", rid, "deadbee")
        self.assertEqual(rc, 0)
        self.assertIn("still OPEN", out)                  # the bar, said out loud
        rc, out, _e = self.asks("list", "--open")
        self.assertIn(rid, out)                           # done ≠ closed
        self.assertIn("done:deadbee", out)
        rc, out, _e = self.asks("report", rid, "post-3")
        self.assertEqual(rc, 0)
        self.assertIn("closed", out)
        rc, out, _e = self.asks("list", "--open")
        self.assertNotIn(rid, out)                        # ONLY report closes
        rc, out, _e = self.asks("list", "--json")
        self.assertEqual(json.loads(out)[0]["report_ref"], "post-3")

    def test_invalid_utf8_line_skips_good_rows_survive(self):
        rid = ownerasks.add("survives corruption")["id"]
        with open(ownerasks.ledger_path(), "ab") as f:
            f.write(b"\xff\xfe torn write \xff\n")       # non-utf8 garbage
        self.assertEqual(ownerasks.rows()[rid]["ask"], "survives corruption")
        self.assertEqual(self.asks("list")[0], 0)         # CLI never tracebacks

    def test_cli_usage_floors(self):
        self.assertEqual(self.asks()[0], 2)
        self.assertEqual(self.asks("add")[0], 2)
        self.assertEqual(self.asks("done", "x")[0], 2)
        self.assertEqual(self.asks("bogus")[0], 2)

    def test_cli_verb_is_wired(self):
        from helm import cli
        self.assertIn("asks", cli.VERBS)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.VERBS["asks"](["add", "via", "the", "front", "door"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(ownerasks.unreported()), 1)

    def test_oldest_unreported_orders_by_ts(self):
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        ownerasks._append({"id": "old1", "ts": old, "ask": "the elder ask",
                           "source": "t", "status": "open", "done_ref": None,
                           "report_ref": None, "last_updated": old})
        ownerasks.add("the newer ask")
        self.assertEqual(ownerasks.oldest_unreported()["id"], "old1")


class AskWhisperTest(AsksBase):
    """The stop-whisper's NEW top rung: an unreported owner ask soft-holds the
    stop once per (ask, status) fingerprint — 'report it to the owner'."""

    def guard(self, payload=None, args=()):
        stdin = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        out, err = io.StringIO(), io.StringIO()
        fake = types.SimpleNamespace(buffer=io.BytesIO(stdin or b"{}"))
        with mock.patch.object(sys, "stdin", fake), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seats.cmd("stop-guard", ["--hook-json", *args], "main")
        return rc, out.getvalue(), err.getvalue()

    def test_unreported_ask_fires_once_then_latches(self):
        seats.join(seat="wisp", cwd="/tmp/p")
        rid = ownerasks.add("post the eval verdicts")["id"]
        rc, _o, err = self.guard(args=["--seat", "wisp"])
        self.assertEqual(rc, 2)                          # the soft hold
        self.assertIn("[helm stop-whisper]", err)
        self.assertIn("post the eval verdicts", err)     # names THE ask
        self.assertIn("report it to the owner", err)
        self.assertIn("helm asks report %s" % rid, err)  # pull-depth pointer
        rc, _o, err = self.guard(args=["--seat", "wisp"])
        self.assertEqual(rc, 0, err)                     # latched — never a loop
        self.assertNotIn("stop-whisper", err)

    def test_done_but_unreported_refires_once_report_silences(self):
        seats.join(seat="wisp", cwd="/tmp/p")
        rid = ownerasks.add("land the fix")["id"]
        self.assertEqual(self.guard(args=["--seat", "wisp"])[0], 2)
        self.assertEqual(self.guard(args=["--seat", "wisp"])[0], 0)  # latched
        ownerasks.mark_done(rid, "abc1234")              # status level changes…
        rc, _o, err = self.guard(args=["--seat", "wisp"])
        self.assertEqual(rc, 2)                          # …so the rung re-fires
        self.assertIn("done-UNREPORTED", err)
        ownerasks.mark_report(rid, "post-1")             # the OWNER heard it
        rc, _o, err = self.guard(args=["--seat", "wisp"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("stop-whisper", err)            # all reported = SILENT

    def test_all_reported_is_silent_from_the_start(self):
        seats.join(seat="wisp", cwd="/tmp/p")
        rid = ownerasks.add("quick one")["id"]
        ownerasks.mark_report(rid, "post-0")
        rc, _o, err = self.guard(args=["--seat", "wisp"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("stop-whisper", err)

    def test_one_ask_per_whisper_the_oldest(self):
        seats.join(seat="wisp", cwd="/tmp/p")
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        ownerasks._append({"id": "eldr", "ts": old, "ask": "the elder ask",
                           "source": "t", "status": "open", "done_ref": None,
                           "report_ref": None, "last_updated": old})
        ownerasks.add("the newer ask")
        rc, _o, err = self.guard(args=["--seat", "wisp"])
        self.assertEqual(rc, 2)
        self.assertEqual(err.count("[helm stop-whisper]"), 1)
        self.assertIn("the elder ask", err)              # oldest, and ONLY it
        self.assertNotIn("the newer ask", err)

    def test_ask_rung_outranks_stuck_and_respects_cap(self):
        seats.join(session="s-a1", seat="wisp", cwd="/tmp/p")
        pk.write_json(os.path.join(record.session_dir("s-a1"), "counters.json"),
                      {"stuck-streak": 3})
        ownerasks.add("a" * 200)                         # oversized owner words
        rc, _o, err = self.guard({"session_id": "s-a1"}, args=["--seat", "wisp"])
        self.assertEqual(rc, 2)
        self.assertIn("owner ask", err)                  # top of the ladder
        self.assertNotIn("wedged", err)                  # stuck waits its turn
        line = next(l for l in err.splitlines() if "stop-whisper" in l)
        self.assertLessEqual(len(line.encode("utf-8")), seats.STOP_WHISPER_CAP)

    def test_fail_closed_and_kill_switch(self):
        seats.join(seat="wisp", cwd="/tmp/p")
        ownerasks.add("real ask")
        os.environ["HELM_STOP_GUARD_WHISPER"] = "0"      # existing switch governs
        rc, _o, err = self.guard(args=["--seat", "wisp"])
        self.assertEqual(rc, 0, err)
        os.environ.pop("HELM_STOP_GUARD_WHISPER")
        p = ownerasks.ledger_path()                      # garbled ledger:
        with open(p, "w", encoding="utf-8") as f:        # fail-closed to nothing
            f.write("not json{{\n")
        rc, _o, err = self.guard(args=["--seat", "wisp"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("stop-whisper", err)

    def test_fire_rides_the_whisper_ledger_ids_only(self):
        seats.join(seat="wisp", cwd="/tmp/p")
        rid = ownerasks.add("secret owner words")["id"]
        self.assertEqual(self.guard(args=["--seat", "wisp"])[0], 2)
        lp = os.path.join(home.global_dir(), ".state", "stop-whisper-ledger.jsonl")
        with open(lp, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f]
        self.assertEqual(rows[0]["id"], "ask:%s:open" % rid)
        self.assertNotIn("secret owner words", json.dumps(rows))  # ids, never text


if __name__ == "__main__":
    unittest.main()
