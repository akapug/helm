#!/usr/bin/env python3
"""The web chat folds acks into receipts.

THE SYMPTOM. The owner's chat showed 31 rows of "handled in-session" when a
seat acked 31 rows one call at a time. An ack is a receipt, not
conversation: consecutive DONE acks from one sender render as ONE line that
counts the rows they acked (`helm chat read` folds the same runs,
tests/test_ack_is_a_receipt.py), and a bulk row counts the ids it names. A
single ack still shows; a blocked ack carries its reason and never folds.

The scenarios (tests/chat_ack_fold_scenarios.js) run the REAL pollChat and
chatLoadOlder, lifted from the assembled web UI, in the world
tests/chat_runtime_harness.js builds: its minimal DOM and faithful fake
server, spliced up to its injection marker. The acks arrive one per poll, as
the incident's did, and in one response, as a reset open paints them.
Requires node; skipped where node is unavailable, like its sibling."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from helm import web_ui_loader
from tests import test_web_chat_client_runtime as runtime

HERE = os.path.dirname(os.path.abspath(__file__))
SCENARIOS = os.path.join(HERE, "chat_ack_fold_scenarios.js")

#: The fold's own functions, lifted beside the runtime harness's set when
#: the page defines them. A page without them still runs its real pollChat
#: and chatLoadOlder here, so the fold arms fail on what the page draws, and
#: the controls pass; a page whose pollChat calls one it lost fails every
#: scenario that reaches the call.
FOLD = ["chatAckIds", "chatAckFolds", "chatAckLine", "chatAckRun",
        "chatAckAppend", "chatPageHtml"]


class TestChatAckFold(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        names = list(dict.fromkeys(runtime.EXTRACT + ["dayStamp"] + [
            n for n in FOLD
            if re.search(r"function\s+" + n + r"\s*\(", src)]))
        fns = "\n\n".join(runtime._extract_fn(src, n) for n in names)
        with open(runtime.HARNESS, encoding="utf-8") as f:
            world = f.read().split("/*__INJECT__*/")[0]
        with open(SCENARIOS, encoding="utf-8") as f:
            scenarios = f.read()
        cls.tmp = tempfile.mkdtemp(prefix="helm-chat-ackfold-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(world + "\n" + fns + "\n" + scenarios)
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        cls.proc = subprocess.run([cls.node, cls.path], capture_output=True,
                                  text=True, timeout=60)
        try:
            cls.results = {r["name"]: r for r in json.loads(cls.proc.stdout or "[]")}
        except json.JSONDecodeError:
            cls.results = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def result(self, name):
        self.assertIn(name, self.results,
                      "no result for %s\nstdout=%r\nstderr=%r"
                      % (name, self.proc.stdout, self.proc.stderr))
        r = self.results[name]
        self.assertTrue(r["pass"], json.dumps(r["detail"]))
        return r

    def test_31_acks_one_per_poll_render_as_one_line(self):  # noqa: VACUOUS_ASSERTION — the scenario's own counts are pinned exactly (lines, receipts), read off the real DOM the page drew
        """The incident's shape: RED before the cure, 33 lines."""
        d = self.result("acks_one_per_poll_fold_into_one_line")["detail"]
        self.assertEqual((d["lines"], d["receipts"], d["n"]), (3, 1, "31"))

    def test_31_acks_in_one_response_render_as_one_line(self):  # noqa: VACUOUS_ASSERTION — the scenario's own counts are pinned exactly (lines, receipts), read off the real DOM the page drew
        d = self.result("acks_in_one_poll_fold_into_one_line")["detail"]
        self.assertEqual((d["lines"], d["receipts"]), (3, 1))

    def test_a_bulk_row_renders_the_rows_it_acks(self):  # noqa: VACUOUS_ASSERTION — the scenario's own counts are pinned exactly (lines, receipts), read off the real DOM the page drew
        d = self.result("a_bulk_row_counts_its_ids")["detail"]
        self.assertEqual((d["lines"], d["receipts"]), (3, 1))

    def test_an_older_page_folds_its_acks(self):  # noqa: VACUOUS_ASSERTION — the scenario's own counts are pinned exactly (lines, receipts), read off the real DOM the page drew
        d = self.result("an_older_page_folds_its_acks")["detail"]
        self.assertEqual((d["lines"], d["receipts"]), (52, 1))

    def test_a_single_ack_still_shows(self):
        """Control: passes on the unfolded page too."""
        d = self.result("a_single_ack_still_shows")["detail"]
        self.assertEqual(len(d["texts"]), 3)

    def test_acks_without_ack_do_not_become_a_receipt(self):  # noqa: VACUOUS_ASSERTION — scenario pins three visible rows and zero receipts
        d = self.result("acks_without_ack_do_not_fold")["detail"]
        self.assertEqual((len(d["texts"]), d["receipts"]), (3, 0))

    def test_acks_led_by_another_id_name_only_ack(self):  # noqa: VACUOUS_ASSERTION — scenario pins one receipt counting one row, as chat.ack_ids does
        d = self.result("acks_led_by_another_id_name_only_ack")["detail"]
        self.assertEqual((d["lines"], d["receipts"], d["n"]), (3, 1, "1"))

    def test_a_blocked_ack_another_sender_and_a_message_break_a_run(self):
        """Control: passes on the unfolded page too."""
        d = self.result("what_breaks_a_run")["detail"]
        self.assertEqual(len(d["texts"]), 7)

    def test_every_scenario_ran_and_passed(self):  # noqa: VACUOUS_ASSERTION — the scenario names are pinned exactly before the exit status is read
        self.assertEqual(sorted(self.results), sorted(
            ["acks_one_per_poll_fold_into_one_line",
             "acks_in_one_poll_fold_into_one_line",
             "a_bulk_row_counts_its_ids", "an_older_page_folds_its_acks",
             "a_single_ack_still_shows", "acks_without_ack_do_not_fold",
             "acks_led_by_another_id_name_only_ack", "what_breaks_a_run"]),
            self.proc.stdout)
        self.assertEqual(self.proc.returncode, 0,
                         "stdout=%s\nstderr=%s" % (self.proc.stdout,
                                                   self.proc.stderr))

if __name__ == "__main__":
    unittest.main()
