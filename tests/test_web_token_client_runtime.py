#!/usr/bin/env python3
"""Client-RUNTIME contract tests for the mutation-bearer lifecycle (the owner's
first-post-forbidden bug, lane chat-first-post-token).

The bearer is PER-PROCESS (web.py MUTATION_TOKEN) and reaches the page only by
serve-time substitution (_ui() replaces the __HELM_TOKEN__ marker). The owner's
tab is LONG-LIVED while the web unit recycles under it (RuntimeMaxSec=1h
bound-staleness expiry, `helm rearm` after a fold) — so the tab's first post
after a recycle carried a DEAD token and 403'd with "forbidden (mutations
always require the bearer token the UI carries)" until a manual reload. The
fix: post() refreshes the token from our own served page on a 403 and retries
ONCE — a genuine forbidden still errors loudly (same-token refresh retries
nothing; a failed refresh leaves the original 403 to surface).

These tests run the ACTUAL refreshToken / post / homePost source lifted
verbatim from the assembled web UI under a fake fetch that mirrors web.py's
do_POST guard and _ui() substitution (tests/token_runtime_harness.js); the
page served for the refresh is the REAL assembled page byte-for-byte, so the
refresh regex is pinned against the true declaration. Requires node; skipped
(not failed) where node is unavailable, like any optional toolchain."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from helm import web_ui_loader
from tests.test_web_chat_client_runtime import _extract_fn

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "token_runtime_harness.js")
MARKER = "__HELM" + "_TOKEN__"   # split like the UI's guard: never let a tool
                                 # that substitutes the marker rewrite this file


def _ui_src():
    return web_ui_loader.read_text()


class TestTokenSourceContract(unittest.TestCase):
    """Static contracts the serve-time substitution + refresh depend on —
    node-free so they hold even where the runtime harness skips."""

    def test_marker_appears_exactly_once(self):
        """_ui() replaces EVERY marker occurrence with the live token. A second
        occurrence (e.g. the refresh guard written verbatim instead of split)
        would be silently rewritten at serve time and corrupt its meaning."""
        self.assertEqual(_ui_src().count(MARKER), 1)

    def test_declaration_is_let_and_carries_the_marker(self):
        """refreshToken reassigns TOKEN — a revert to `const` makes recovery a
        runtime TypeError. The declaration is also the substitution target and
        the refresh regex's anchor, so pin its exact shape."""
        m = re.search(r'^(const|let) TOKEN = "([^"]*)"', _ui_src(), re.M)
        self.assertIsNotNone(m, "TOKEN declaration not found in assembled web UI")
        self.assertEqual(m.group(1), "let")
        self.assertEqual(m.group(2), MARKER)

    def test_all_mutations_ride_the_shared_post_helper(self):
        """One retry-carrying path: no fetch outside post() may attach the
        bearer, or that mutation silently misses the stale-token recovery."""
        src = _ui_src()
        carriers = [ln for ln in src.splitlines() if "Bearer " in ln and "fetch(" in ln]
        self.assertEqual(len(carriers), 1, "bearer-carrying fetches beyond post(): %r" % carriers)
        self.assertIn("function post(url, body)", src)


class TestTokenClientRuntime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = _ui_src()
        decl = re.search(r'^let TOKEN = "[^"]*";.*$', src, re.M)
        assert decl, "TOKEN declaration not found"
        home = re.search(r"^const homePost = .*$", src, re.M)
        assert home, "homePost not found"
        fns = "\n\n".join([decl.group(0)] +
                          [_extract_fn(src, n) for n in ("refreshToken", "post")] +
                          [home.group(0)])
        with open(HARNESS, encoding="utf-8") as f:
            template = f.read()
        assert "/*__INJECT__*/" in template
        cls.tmp = tempfile.mkdtemp(prefix="helm-token-runtime-")
        page = os.path.join(cls.tmp, "web_ui.html")
        # the refresh reads the REAL assembled page bytes from this temp path
        with open(page, "wb") as f:
            f.write(web_ui_loader.read_bytes())
        assembled = (template.replace("/*__INJECT__*/", fns)
                     .replace('"__PAGE_PATH__"', json.dumps(page)))
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(assembled)
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
        r = self.results[name]
        self.assertTrue(r["pass"], name + " failed: " + json.dumps(r["detail"]))
        return r

    def test_stale_bearer_first_post_recovers(self):
        """THE OWNER'S BUG: a post carrying the token of a recycled process must
        refresh from the served page and succeed on exactly one retry — the
        first post after page-open never surfaces forbidden for the page's own
        staleness."""
        r = self._result("stale_bearer_first_post_recovers")
        self.assertEqual(r["detail"]["pageFetches"], 1)
        self.assertEqual(r["detail"]["token"], "T2")   # adopted, not re-fetched next time

    def test_next_post_rides_the_adopted_token(self):
        r = self._result("next_post_rides_the_adopted_token")
        self.assertEqual(r["detail"]["posts"], 1)      # the post really landed…
        self.assertEqual(r["detail"]["pageFetches"], 0)  # …with no refresh round-trip

    def test_genuine_forbidden_still_loud(self):
        """A REAL forbidden (server refuses the very token it serves) is never
        masked: one attempt, no retry, the server's exact words thrown."""
        r = self._result("genuine_forbidden_still_loud")
        self.assertEqual(r["detail"]["posts"], 1)      # bounded: never an infinite retry

    def test_refresh_down_original_403_surfaces(self):
        r = self._result("refresh_down_original_403_surfaces")
        self.assertEqual(r["detail"]["posts"], 1)

    def test_non_403_never_refreshes(self):
        r = self._result("non_403_never_refreshes")
        self.assertEqual(r["detail"]["message"], "body wants a JSON object")  # the 400 threw loudly…
        self.assertEqual(r["detail"]["pageFetches"], 0)  # …and never touched the bearer path

    def test_unsubstituted_page_never_adopts_the_marker(self):
        """A raw (file://-opened or mis-templated) page must never install the
        marker text as a bearer."""
        r = self._result("unsubstituted_page_never_adopts_the_marker")
        self.assertEqual(r["detail"]["token"], "T1")

    def test_refresh_regex_finds_the_real_declaration(self):
        """Pins the _ui() substitution ↔ client-regex contract against the real
        assembled web UI bytes."""
        r = self._result("refresh_regex_finds_the_real_declaration")
        self.assertEqual(r["detail"]["token"], "T3")

    def test_homepost_error_object_contract(self):
        """homePost rides post() (shares the retry) yet still RESOLVES with an
        {error: ...} object — its callers render r.error, they never catch."""
        r = self._result("homepost_error_object_contract")
        self.assertEqual(
            r["detail"]["badValue"],
            {"error": "forbidden (mutations always require the bearer token the UI carries)"})
        self.assertIs(r["detail"]["goodValue"]["ok"], True)


if __name__ == "__main__":
    unittest.main()
