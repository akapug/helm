#!/usr/bin/env python3
"""Client-RUNTIME contract tests for the chat panel (brick #4, second round).

The first round pinned only the SERVER's /api/chat shape, so three CLIENT-JS ordering
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
EXTRACT = ["pollChat", "chatDmState", "chatResetLog", "chatHydrateParents", "chatLoadOlder",
           "chatReconcileQuotes", "chatBumpReply", "chatSetReplyCount", "chatIndex",
           "chatKey", "chatThreadKey", "chatParentKey", "chatParent", "chatQuote",
           "chatQuoteResolved", "chatQuotePending", "chatQuoteGone", "chatFind",
           # THE FETCH HELPER IS PART OF THE SUBJECT NOW. The poll's DEADLINE is
           # enforced inside `j` (AbortController + timeout), so a harness that
           # stubbed `j` could not exercise a read that never answers — the one
           # failure that leaves no evidence anywhere. The harness supplies
           # `fetch` instead.
           "j", "lrDur", "lrAgo", "cardBoundS", "cardStale", "cardSource",
           # and the two cards the poll feeds, so what a hung first read leaves
           # on the page is read off the real renderers
           "dashChatAgeS", "dashChatExpired", "dashAnswers",
           "dashAnswersClock", "dashFleetClock",
           # the owner's name the poll stamps before it draws a row
           "chatOwnerStamp", "chatPostAs", "chatTypedName"]


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

    def test_dm_state_is_what_the_RENDERER_was_handed(self):
        """The FIX on 3afc06af: the JS arm was right and NOTHING PYTHON
        LOOKED AT IT.

        `chat_runtime_harness.js` records CHAT_DM_STATE inside the chatRooms stub
        — the render boundary — and its `dm_state_rides_the_poll` scenario does
        discriminate moving the assignment after the render. But no method here
        ever called `_result` for it, and setUpClass stores `cls.proc` without
        asserting its returncode. So the production reorder could make the
        scenario fail and Node exit 1 while every assertion in this file stayed
        green. An exact-tree whole-suite receipt cannot cure an arm the suite
        never observes.

        THE THREE VALUES ARE PINNED INDIVIDUALLY, not just `pass`. `pass` is a
        conjunction computed in JS; pinning each pole means a harness that
        silently stopped distinguishing them cannot hide behind its own AND.
        """
        r = self._result("dm_state_rides_the_poll")
        d = r["detail"]
        self.assertEqual(d["afterOld"], "unreported",
                         "an OLD server sends no dm key at all, so the rail must "
                         "say UNREPORTED rather than inventing a state: "
                         + json.dumps(d))
        self.assertEqual(d["afterModernOk"], "ok",
                         "a modern server measuring COMPLETE must render ok: "
                         + json.dumps(d))
        self.assertEqual(d["afterModernIncomplete"], "incomplete",
                         "a modern server measuring INCOMPLETE must render it, "
                         "or the owner is told the DM lane is whole when it is "
                         "not: " + json.dumps(d))
        self.assertTrue(r["pass"], json.dumps(d))

    def test_every_harness_scenario_is_OBSERVED_by_this_suite(self):  # noqa: VACUOUS_ASSERTION — no-scenario-failed IS the product law here; three unconditional positive controls cover the same observables (named scenario present, stdout non-empty, >=4 scenarios)
        """The structural half of the same finding, and it outlives me.

        The arm above fixes ONE unobserved scenario. The next scenario added to
        the harness is unobserved again unless somebody remembers to write a
        method for it — which is exactly what happened here. This asserts the
        Node process EXITED CLEAN and that every scenario it emitted reports
        pass, so an unobserved failure can no longer be silently green.

        It is deliberately NOT in setUpClass: a hard failure there breaks every
        method in the file with one confusing error, and the ruling was
        to keep scenario-specific diagnostics. This is the net; the methods
        above are the diagnosis.
        """
        # POSITIVE CONTROL ON self.proc BEFORE ASSERTING ITS ZERO: rc == 0 is an
        # absence claim, and a harness that never ran also has no failures. This
        # proves the process produced output at all.
        self.assertTrue(self.proc.stdout.strip(),
                        "the harness produced no stdout, so every assertion "
                        "below is about a run that did not happen")
        self.assertEqual(self.proc.returncode, 0,
                         "the harness exited %r — a scenario failed and no "
                         "method in this file would have noticed.\nstdout=%s"
                         "\nstderr=%s" % (self.proc.returncode,
                                           self.proc.stdout, self.proc.stderr))
        # CONTROL: the harness must have produced scenarios at all. Without this
        # an empty results dict satisfies the all-pass check trivially — the
        # JSONDecodeError path above sets results to {}.
        # POSITIVE CONTROL ON THE SAME OBSERVABLE, a named scenario rather than
        # a count: a size assertion still leaves every comparison below relative
        # to whatever the run happened to emit, and this pins that the results
        # dict is the real one.
        self.assertIn("dm_state_rides_the_poll", self.results)
        self.assertGreaterEqual(len(self.results), 6,
                                "harness emitted %d scenarios; an empty or "
                                "truncated run makes the check below vacuous"
                                % len(self.results))
        # STATED AS A POSITIVE EQUALITY, not "failed == []". The empty-list form
        # is satisfied by an empty comprehension over an empty dict, and `failed`
        # is a fresh binding that does not inherit the size control above. This
        # compares the PASSING set against the FULL set — both derived from the
        # same results, both non-empty by the assertion above — so it cannot go
        # green on a run that produced nothing.
        passed = sorted(n for n, r in self.results.items() if r.get("pass"))
        self.assertEqual(passed, sorted(self.results),
                         "harness scenarios failed: %s"
                         % ", ".join(sorted(set(self.results) - set(passed))))

    def test_a_first_read_that_NEVER_ANSWERS_renders_NOT_READ(self):
        """THE PENDING FIRST READ, which no rejected-read arm can reach.

        `await j(url)` with no deadline never settles on a request that hangs, so
        the catch never runs: neither stamp is written, the `finally` that
        releases CHAT_POLLING is never reached (every later beat returns at the
        lock), and the answers card — hidden until a read populates it — stays
        hidden for the life of the page, which on a card hidden at zero is the
        same picture as a measured zero.

        The deadline is asserted as a DERIVATION rather than a number: the poll
        hands `j` cardBoundS(cadence) in milliseconds, which at the 0.05s cadence
        this scenario publishes is 200. The age comes off a held clock advanced
        130s past the failed attempt, so NOT READ is measured with an age."""
        r = self._result("pending_first_read_is_NOT_READ")
        d = r["detail"]
        self.assertEqual(d["deadlineMs"], 200,
                         "the poll must pass cardBoundS(0.05) * 1000: " + json.dumps(d))
        self.assertGreater(d["failedStamp"], 0,
                           "a read that timed out must stamp the failure like a "
                           "rejected one: " + json.dumps(d))
        self.assertEqual(d["answeredStamp"], 0)      # no success has ever happened
        self.assertFalse(d["lockedAfter"], "CHAT_POLLING was never released")
        self.assertIn("NOT READ", d["card"])
        self.assertIn("2m ago", d["card"])           # the attempt's own age
        self.assertIn("NOT READ", d["fleet"])
        # THE LOCK, MEASURED AT THE TRANSPORT: the next beat reached fetch and
        # the rows it answered are on screen, so the release is a fact about the
        # poll rather than about a flag.
        self.assertEqual(d["reads"], 2, json.dumps(d))
        self.assertEqual(d["rendered"], 3, json.dumps(d))
        self.assertTrue(r["pass"], json.dumps(d))

    def test_a_read_that_answers_INSIDE_the_deadline_renders_for_real(self):
        """THE POSITIVE CONTROL FOR THE ARM ABOVE, over the same transport and
        the same deadline: a read that answers late but in time renders its rows,
        stamps no failure, and draws the answers card off the envelope THIS POLL
        fetched — with the `helm chat read` provenance line every chat-fed card
        carries. Without this, NOT READ above could be a harness that cannot
        render anything."""
        r = self._result("a_read_that_answers_LATE_still_renders")
        d = r["detail"]
        self.assertEqual(d["rendered"], ["L0", "L1"], json.dumps(d))
        self.assertEqual(d["failedStamp"], 0, json.dumps(d))
        self.assertGreater(d["answeredStamp"], 0, json.dumps(d))
        self.assertIn("answers for you", d["card"])
        self.assertIn("the node is back", d["card"])
        self.assertIn("helm chat read", d["card"])
        self.assertTrue(r["pass"], json.dumps(d))

    def test_the_owners_name_is_stamped_from_the_reset_open_before_its_rows_draw(self):
        """The page ships no person's name, so the only name it can mark the
        owner's rows with is the one the server announces on the reset open.
        Every row of that open must already see it; an incremental poll, which
        carries no name, keeps it; a server that named nobody leaves the box
        reading "owner" and no row marked."""
        r = self._result("owner_name_rides_the_reset_open")
        d = r["detail"]
        self.assertEqual(d["opened"], ["harbor"] * 3, json.dumps(d))
        self.assertEqual(d["placeholder"], "harbor")
        self.assertEqual(d["later"], ["harbor"], json.dumps(d))
        self.assertEqual(d["kept"], "harbor")
        self.assertEqual(d["unnamed"], [""] * 4, json.dumps(d))
        self.assertEqual(d["unnamedPlaceholder"], "owner")
        self.assertTrue(r["pass"], json.dumps(d))

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
