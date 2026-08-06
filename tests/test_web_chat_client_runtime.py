#!/usr/bin/env python3
"""Client-RUNTIME contract tests for the chat panel.

An earlier pass pinned only the SERVER's /api/chat shape, so three CLIENT-JS ordering
defects landed green: (1) pollChat wiped the just-stamped generation/base by
running chatResetLog AFTER stamping them, so a rotation right after any reset
missed rows and a post-reset older-page could not hydrate; (2) chatHydrateParents
sent ids.slice(0,100) yet marked ALL pending ids absent, orphaning the 101st
parent; (3 lives in test_web_chat.py — the server cache TOCTOU/growth).

These tests run the ACTUAL pollChat / chatHydrateParents / chatLoadOlder /
chatResetLog source lifted verbatim from the assembled web UI under a faithful
fake server + minimal DOM (tests/chat_runtime_harness.js), so they exercise the
real statement ORDER — a reorder back to the buggy sequence fails them. Requires node;
skipped (not failed) where node is unavailable, like any optional toolchain."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from helm import web_ui_loader

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "chat_runtime_harness.js")

# the real functions the harness drives — lifted verbatim so a regression in
# their ordering (not just the API) fails the test
EXTRACT = ["pollChat", "chatResetLog", "chatHydrateParents", "chatLoadOlder",
           "chatReconcileQuotes", "chatBumpReply", "chatSetReplyCount", "chatIndex",
           "chatKey", "chatThreadKey", "chatParentKey", "chatParent", "chatQuote",
           "chatQuoteResolved", "chatQuotePending", "chatQuoteGone", "chatFind"]


def _extract_fn(src, name):
    """The verbatim `function NAME(...) {...}` body, brace-matched with string +
    line/block comment awareness (an apostrophe inside a `//` comment must not be
    read as a string delimiter, and a `{` inside a string/comment must not count)."""
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        raise AssertionError("function not found in assembled web UI: " + name)
    i = src.index("{", m.end())
    depth, j, n = 0, i, len(src)
    quote, esc = None, False
    while j < n:
        c = src[j]
        if quote:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
        elif c == "/" and j + 1 < n and src[j + 1] == "/":
            j = src.index("\n", j) if "\n" in src[j:] else n
            continue
        elif c == "/" and j + 1 < n and src[j + 1] == "*":
            j = src.index("*/", j) + 2
            continue
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
        j += 1
    raise AssertionError("unbalanced braces extracting " + name)


class TestChatClientRuntime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        fns = "\n\n".join(_extract_fn(src, name) for name in EXTRACT)
        with open(HARNESS, encoding="utf-8") as f:
            template = f.read()
        assert "/*__INJECT__*/" in template
        assembled = template.replace("/*__INJECT__*/", fns)
        cls.tmp = tempfile.mkdtemp(prefix="helm-chat-runtime-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(assembled)
        # syntax-gate the spliced whole (the task's `node --check`) before running
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        proc = subprocess.run([cls.node, cls.path], capture_output=True, text=True, timeout=60)
        cls.proc = proc
        try:
            cls.results = {r["name"]: r for r in json.loads(proc.stdout or "[]")}
        except json.JSONDecodeError:
            cls.results = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def _result(self, name):
        self.assertIn(name, self.results,
                      "harness produced no result for %s\nstdout=%r\nstderr=%r"
                      % (name, self.proc.stdout, self.proc.stderr))
        return self.results[name]

    def test_rotation_right_after_a_reset_misses_no_rows(self):
        """DEFECT 1a: the windowed reset open must leave CHAT_GEN stamped, so the
        very next poll detects the rotation and repaints the reset tail
        (new20..new69) instead of appending onto stale pre-rotation rows."""
        r = self._result("post_reset_rotation_no_rows_missed")
        self.assertTrue(r["pass"], "rows missed after a post-reset rotation: " + json.dumps(r["detail"]))
        self.assertEqual(r["detail"]["genAfterOpen"], "genA")   # reset did NOT wipe the gen
        self.assertEqual(r["detail"]["rendered"], 50)
        self.assertFalse(r["detail"]["hasOld"])                 # no pre-rotation rows survive

    def test_older_cursor_survives_the_reset_so_a_parent_pages_and_counts(self):
        """DEFECT 1b: the reset must preserve `base` (CHAT_OLDEST_BASE), or the
        older page can never be pulled — the parent stays off-screen and its reply
        count never renders."""
        r = self._result("lazy_parent_pages_and_reply_count_after_reset")
        self.assertTrue(r["pass"], "parent could not page/count after reset: " + json.dumps(r["detail"]))
        self.assertEqual(r["detail"]["baseAfterOpen"], 10)      # cursor survived the reset
        self.assertTrue(r["detail"]["parentOnScreen"])
        self.assertEqual(r["detail"]["replyCount"], "↩1")

    def test_hydration_chunks_all_ids_and_never_orphans_the_101st_parent(self):
        """DEFECT 2: with 101 pending parents chatHydrateParents must request ALL
        of them (chunked <=100) and mark absent only ids it actually SENT — the
        101st parent resolves, never falsely 'rotated out'."""
        r = self._result("hydrate_over_100_parents_101st_not_orphaned")
        self.assertTrue(r["pass"], "the 101st parent was orphaned: " + json.dumps(r["detail"]))
        self.assertEqual(r["detail"]["pendingCount"], 101)
        self.assertTrue(r["detail"]["p100resolved"])
        self.assertFalse(r["detail"]["p100gone"])
        self.assertEqual(r["detail"]["goneSize"], 0)            # nothing branded gone


if __name__ == "__main__":
    unittest.main()
