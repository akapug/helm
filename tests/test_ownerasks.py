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

from helm import eventledger, home, ownerasks, pk, record, seats  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_ROOM", "MELD_CHAT_ROOM",
            "HELM_CHAT_OWNER_NAMES", "HELM_CHAT_DELIVER",
            "HELM_STOP_GUARD", "HELM_STOP_GUARD_INBOX",
            "HELM_STOP_GUARD_CLAIMS", "HELM_STOP_GUARD_INDEX",
            "HELM_STOP_GUARD_WHISPER", "HELM_STOP_GUARD_BEACON",
            "HELM_SCRATCH_GC", "HELM_CACHE_DIR",
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
        os.environ["HELM_CHAT_OWNER_NAMES"] = "owner"
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        # this module drives the stop-guard hook, whose silent-mechanical lane
        # runs the scratch reaper — a real DELETE under /tmp/claude-*. A test
        # never mutates a harness store (tests/test_scratch.py pins this).
        os.environ["HELM_SCRATCH_GC"] = "0"
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")

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
        row, _err = ownerasks.add("wire the exit briefing", source="chat-7")
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
        self.assertEqual(ownerasks.add("x")[0]["source"], "cli")  # ids scrubbed

    def test_lifecycle_done_then_report_snapshots_append_only(self):
        row, _err = ownerasks.add("ship the ledger")
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
        rid = ownerasks.add("just tell me when the deploy lands")[0]["id"]
        rep, why = ownerasks.mark_report(rid, "post-9")   # report without done: legal
        self.assertIsNone(why)
        self.assertEqual(rep["status"], "reported")
        _r, why = ownerasks.mark_done(rid, "sha")         # closed rows stay closed
        self.assertIn("already reported", why)

    def test_guards_refuse_empty_refs_and_unknown_ids(self):
        rid = ownerasks.add("a")[0]["id"]
        self.assertIn("evidence", ownerasks.mark_done(rid, "  ")[1])
        self.assertIn("chat-post id", ownerasks.mark_report(rid, "")[1])
        self.assertIn("no such ask", ownerasks.mark_done("nope", "sha")[1])
        self.assertIsNone(ownerasks.add("   ")[0])           # empty ask never lands

    def test_fail_open_on_unwritable_ledger(self):
        os.makedirs(os.path.dirname(home.global_dir()), exist_ok=True)
        with open(home.global_dir(), "w", encoding="utf-8") as f:
            f.write("not a dir")                          # _global is a FILE
        self.assertIsNone(ownerasks.add("lost?")[0])         # no raise, no lie
        self.assertEqual(ownerasks.rows(), {})            # reads fail open too
        rc, _o, err = self.asks("add", "lost?", "--needs", "owner login")
        self.assertEqual(rc, 1)
        self.assertIn("NOT recorded", err)                # loud, never silent

    def test_cli_lifecycle_done_stays_open_until_report(self):
        rc, out, _e = self.asks("add", "fix", "the", "flaky", "test",
                                "--needs", "owner ruling")
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
        rid = ownerasks.add("survives corruption")[0]["id"]
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
            rc = cli.VERBS["asks"](["add", "via", "the", "front", "door",
                                    "--needs", "owner credential"])
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
        rid = ownerasks.add("post the eval verdicts")[0]["id"]
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
        rid = ownerasks.add("land the fix")[0]["id"]
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
        rid = ownerasks.add("quick one")[0]["id"]
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

    def test_unavailable_ledger_is_unknown_on_cli_and_stop_whisper(self):
        seats.join(seat="wisp", cwd="/tmp/p")
        with mock.patch.object(eventledger, "checked_events",
                               return_value=([], "PermissionError: denied")):
            rc, _out, err = self.asks("list")
            self.assertEqual(rc, 1)
            self.assertIn("owner debt UNKNOWN", err)
            rc, _out, err = self.guard(args=["--seat", "wisp"])
            self.assertEqual(rc, 2)
            self.assertIn("owner-ask ledger UNAVAILABLE", err)
            self.assertIn("UNKNOWN", err)

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
        rid = ownerasks.add("secret owner words")[0]["id"]
        self.assertEqual(self.guard(args=["--seat", "wisp"])[0], 2)
        lp = os.path.join(home.global_dir(), ".state", "stop-whisper-ledger.jsonl")
        with open(lp, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f]
        self.assertEqual(rows[0]["id"], "ask:%s:open" % rid)
        self.assertNotIn("secret owner words", json.dumps(rows))  # ids, never text


if __name__ == "__main__":
    unittest.main()


class ClarityRefusesBeforeTheWriteTest(AsksBase):
    """The clarity die ran AFTER the append, so a 103-word ask printed
    "max 20 in owner mode" and landed anyway — and this ledger has no edit,
    amend or supersede verb, so the row sat on the owner's queue permanently
    in the form the guard had just said he could not read. There was no cure
    available after the write.
    """

    LONG = ("HELM_CONSOLE_URL is unset so a decision push tells your phone to "
            "answer on helm web without a link it can open, and the console "
            "binds a loopback address which on a phone means the phone itself, "
            "so the tap was always going to be dead and the ask was wrong.")

    def test_an_unreadable_ask_is_REFUSED_and_nothing_is_written(self):
        # POSITIVE CONTROL FIRST, on the same observable: a SHORT ask files.
        # Without it, "nothing written" is also what a verb that refuses
        # everything produces.
        rc, out, _err = self.asks("add", "Set the push topic.",
                                  "--needs", "the topic value")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(ownerasks.rows()), 1)

        rc, _out, err = self.asks("add", self.LONG, "--needs", "the URL")
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", err)
        self.assertIn("no edit verb", err)
        # THE LEDGER IS UNCHANGED — the whole point is that a refused ask
        # leaves nothing behind to be stuck with.
        self.assertEqual(len(ownerasks.rows()), 1)

    def test_the_existing_silence_switch_lifts_the_refusal_too(self):
        """A half-working escape hatch is worse than none: the env that
        silences the advisory must also lift the refusal, or a caller who
        opted out still cannot write."""
        os.environ["HELM_CLARITY_ADVISE_OFF"] = "asks"
        try:
            rc, out, _err = self.asks("add", self.LONG, "--needs", "the URL")
        finally:
            os.environ.pop("HELM_CLARITY_ADVISE_OFF", None)
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(ownerasks.rows()), 1)
        # and with the switch OFF again the same text is refused, so the
        # assertion above is the switch acting rather than the check missing
        rc, _out, err = self.asks("add", self.LONG, "--needs", "the URL")
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", err)

    def test_a_LIBRARY_caller_cannot_file_a_row_the_owner_cannot_read(self):
        """The guard must sit on the WRITE, not on one door.

        `ownerasks.add` is PUBLIC and until this arm the only thing checking
        was `cmd_asks`, so the ledger was one new caller away from a permanent
        unreadable row — and it has add|done|report|list and NO edit verb, so
        there is no cure after the append. The web queue and the hooks are the
        obvious next callers and neither goes through the CLI.

        This arm calls `add` DIRECTLY, which is the whole point: an arm that
        went through `self.asks(...)` would stay green with the check back in
        the CLI and would prove nothing about this change.
        """
        # POSITIVE CONTROL on the same observable, first: a clean ask really
        # does land through this same call path. Without it, "the bad one is
        # absent" is also what a wholly broken ledger produces.
        row, err = ownerasks.add("Set the push topic.")
        self.assertIsNotNone(row, "the control ask must file, or the refusal "
                                  "below proves nothing")
        self.assertIsNone(err)

        row, err = ownerasks.add("ship the thing; then tell me")
        self.assertIsNone(row)
        # THE REFUSAL MUST SAY WHY, and it must say it in the checker's own
        # words. A bare None here would force every caller to re-derive the
        # reason, which is what the CLI used to do and what the pair removed.
        self.assertIn("semicolon", err)
        self.assertNotEqual(err, ownerasks.UNWRITABLE,
                            "an unreadable ask is the CALLER's input, never an "
                            "operational fault — the CLI picks its exit code "
                            "off exactly this distinction")

        # ASSERT THE EFFECT, not the absence of a complaint: the refused text
        # must be absent from the LEDGER and the control present, so a guard
        # that shrugged and wrote anyway cannot pass this.
        # rows() is a DICT keyed by id — len() alone would not have caught a
        # guard that wrote the wrong TEXT, which is why this reads the values.
        asks = [r["ask"] for r in ownerasks.rows().values()]
        self.assertIn("Set the push topic.", asks)
        self.assertNotIn("ship the thing; then tell me", asks)
        self.assertEqual(len(ownerasks.rows()), 1)

    def test_the_library_refusal_reads_the_needs_text_too(self):
        """The CLI joined ask and --needs before checking. Moving the guard
        into `add` must not quietly drop half its input: an unreadable NEEDS
        is exactly as unfixable as an unreadable ask, for the same reason."""
        row, err = ownerasks.add("Set the topic.", needs="the value")
        self.assertIsNotNone(row)
        self.assertIsNone(err)
        row, err = ownerasks.add("Set the topic.",
                                 needs="the value; and the URL")
        self.assertIsNone(row)
        self.assertIn("semicolon", err)   # the NEEDS half is what tripped it
        self.assertEqual(len(ownerasks.rows()), 1)

    def test_an_empty_ask_and_a_dead_ledger_do_not_report_the_same_reason(self):
        """The whole argument for the pair: three refusals, three sentences.

        A bare None made "you typed nothing", "the owner cannot read this" and
        "the disk declined the write" indistinguishable to every caller — an
        easy conflation to ship under cover of the module's never-raise law.
        UNWRITABLE is the only one compared by VALUE, because it is the only
        one that is an operational fault rather than input."""
        self.assertEqual(ownerasks.add("   ")[1], "an ask needs words")
        self.assertIn("semicolon", ownerasks.add("a; b")[1])
        # POSITIVE CONTROL: a good ask reports NO error through the same path.
        self.assertIsNone(ownerasks.add("Set the push topic.")[1])

        # AND THE OPERATIONAL FAULT IS THE SENTINEL, not a lookalike sentence.
        # Break the LEDGER FILE rather than _global: the adds above already
        # created that directory, so the sibling arm's trick (write a file
        # where the dir belongs) only works on an untouched home and would
        # fail here for a reason that has nothing to do with the ledger.
        os.remove(ownerasks.ledger_path())
        os.mkdir(ownerasks.ledger_path())        # a DIRECTORY where the jsonl goes
        self.assertEqual(ownerasks.add("a clean ask")[1], ownerasks.UNWRITABLE)

    def test_an_unreadable_checker_FAILS_OPEN_and_the_ask_still_files(self):
        """A guard that cannot run must not become a guard that blocks — the
        same fail-open law the shared advisory states."""
        with mock.patch("helm.clarity.check_text",
                        side_effect=RuntimeError("checker down")):
            rc, out, _err = self.asks("add", self.LONG, "--needs", "the URL")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(ownerasks.rows()), 1)


class NeedsRequiredTest(AsksBase):
    """An ask with no named HUMAN-ONLY input is a task, not an owner gate.

    Measured on a live queue: 19 open asks, the oldest four days
    old, and ELEVEN WERE NOT OWNER GATES — engineering work nobody had started,
    policy the owner had already decided, and items already finished. They
    buried the four that were real, and the owner read none of them because the
    volume made the queue worthless.

    The test is MECHANICAL rather than a judgement about prose: name what only a
    human can supply. Same split `kind` and verdict `polarity` already took —
    required at the CLI for new writes, still optional in add() so historical
    rows and library callers replay unchanged.
    """

    def test_an_ask_without_a_named_human_only_input_is_REFUSED(self):
        rc, _out, err = self.asks("add", "some engineering task nobody started")
        self.assertEqual(rc, 2, "an unnamed gate must not be recorded")
        self.assertIn("HUMAN-ONLY", err)
        # it must say what to do INSTEAD, or it just blocks without teaching
        self.assertIn("task list", err)
        rc, out, _ = self.asks("list")
        self.assertIn("no asks", out, "nothing may have been written")

    def test_a_named_human_only_input_records_AND_renders(self):
        rc, _out, err = self.asks("add", "reconnect the tracker connector",
                                  "--needs", "the owner's OAuth login")
        self.assertEqual(rc, 0, err)
        rc, out, _ = self.asks("list")
        self.assertIn("reconnect the tracker connector", out)
        # the needs line must RENDER — a field recorded but never shown is dead
        # weight, and it is the whole reason the row is in the owner's queue
        self.assertIn("needs: the owner's OAuth login", out)

    def test_add_the_library_call_still_accepts_no_needs_for_replay(self):
        """Historical rows and library callers are untouched: only the CLI's
        NEW-write path requires the declaration."""
        row, _err = ownerasks.add("a row written before needs existed")
        self.assertIsNotNone(row)
        self.assertIsNone(row.get("needs"))
        rc, out, _ = self.asks("list")
        self.assertIn("a row written before needs existed", out)
        self.assertNotIn("needs:", out, "a row with no needs shows no needs line")
