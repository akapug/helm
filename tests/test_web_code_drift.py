#!/usr/bin/env python3
"""The console tells you when it is older than its own source.

`helm web` has TWO deployment lifetimes in one process: the assembled web UI
is read per REQUEST and is always current; web.py is imported ONCE and frozen
for the process's life, with no reloader anywhere.

So a change touching both halves goes HALF-LIVE the instant it lands — the new
template renders against the old server — and nothing on the page says so.
That is worse than plain staleness, because the surface LOOKS current.

Measured 2026-07-25: the owner's console had been serving current HTML against
20-hour-old web.py, including `ebc1bc1` "honest presence: a roster dot may only
report evidence about the seat it names". Half of an honesty fix is not honest,
and the owner had no way to know.

Landing is not deploying. This makes the gap visible from the surface the
owner actually reads, rather than from a process listing they would have to
know to run.
"""
import os
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-drift-", var="HELM_HOME")

from helm import web, web_ui_loader  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CodeDriftTest(unittest.TestCase):
    def test_a_freshly_loaded_server_reports_NO_drift(self):
        """The common case must cost nothing and say nothing — a warning that
        cries on every page load is one nobody reads when it matters."""
        with mock.patch.object(web, "_LOADED_STAMP", web._source_stamp()):
            self.assertIsNone(web.code_drift())

    def test_source_newer_than_the_load_stamp_REPORTS(self):
        # Anchor to the SOURCE stamp, not time.time(): the drift window must be
        # relative to how old the tree actually is. time.time() - 7200 only
        # fires when the tree happens to be < 2h old at test time — true on a
        # fresh checkout, false in any worktree that has sat for 2h (measured
        # 2026-07-25: this test went red on a 2.24h-old lane worktree).
        with mock.patch.object(web, "_LOADED_STAMP", web._source_stamp() - 7200):
            d = web.code_drift()
        self.assertIsNotNone(d)
        self.assertGreater(d["stale_seconds"], 3000)
        self.assertIn("restart", d["note"])          # says what to DO
        self.assertIn("loaded", d)                   # and shows both stamps
        self.assertIn("on_disk", d)

    def test_it_never_reports_the_server_as_NEWER_than_its_source(self):
        """Clock skew or a checkout that rewinds mtimes must read as no-drift,
        not as negative staleness rendered to the owner."""
        with mock.patch.object(web, "_LOADED_STAMP", time.time() + 86400):
            self.assertIsNone(web.code_drift())

    def test_it_FAILS_OPEN_and_never_raises(self):
        """It runs inside the endpoint the console calls on every load. A
        broken self-check must never take down the surface it describes."""
        with mock.patch.object(web, "_source_stamp", side_effect=OSError("boom")):
            self.assertIsNone(web.code_drift())
        with mock.patch.object(web, "_source_stamp", return_value=0.0):
            self.assertIsNone(web.code_drift())
        with mock.patch.object(web, "_LOADED_STAMP", 0.0):
            self.assertIsNone(web.code_drift())

    def test_the_stamp_is_SELF_REFERENTIAL_not_cwd_relative(self):
        """It must answer about the tree THIS process loaded, never about
        whichever checkout the caller is standing in — the seat/lane split
        makes those routinely different, and a cwd-relative answer would be
        confidently about the wrong tree."""
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir("/tmp")
        self.assertGreater(web._source_stamp(), 0)

    def test_it_ignores___pycache___entirely(self):
        """Running the server writes into __pycache__. If that counted, the
        console would report drift against itself seconds after starting.

        The FIRST version of this test touched .pyc files and asserted the
        stamp was unchanged — which passed with the __pycache__ skip DELETED,
        because the .py suffix filter already excludes .pyc. It killed no
        mutation and therefore tested nothing. The skip only does real work
        against a .py file living under __pycache__, so that is what this
        plants."""
        cache = os.path.join(REPO, "helm", "__pycache__")
        os.makedirs(cache, exist_ok=True)
        planted = os.path.join(cache, "_drift_probe.py")
        before = web._source_stamp()
        with open(planted, "w", encoding="utf-8") as f:
            f.write("# transient test artifact\n")
        self.addCleanup(lambda: os.path.exists(planted) and os.remove(planted))
        os.utime(planted, (time.time() + 3600, time.time() + 3600))
        self.assertEqual(before, web._source_stamp(),
                         "a .py under __pycache__ must not move the stamp")


class WhoamiCarriesItTest(unittest.TestCase):
    def test_the_endpoint_surfaces_drift_when_there_is_some(self):
        # Same source-relative anchor as above — see that comment.
        with mock.patch.object(web, "_LOADED_STAMP", web._source_stamp() - 7200):
            out = web._api_whoami()
        self.assertIn("code_drift", out)
        self.assertIn("restart", out["code_drift"]["note"])

    def test_and_stays_QUIET_when_there_is_none(self):
        with mock.patch.object(web, "_LOADED_STAMP", web._source_stamp()):
            out = web._api_whoami()
        self.assertNotIn("code_drift", out)


class TheConsoleRENDERSItTest(unittest.TestCase):
    """A server-side field nothing renders is the same as no warning at all —
    which is the whole lesson of the surface this came from."""

    def setUp(self):
        self.ui = web_ui_loader.read_text()

    def test_there_is_an_element_and_a_renderer(self):
        self.assertIn('id="stalebanner"', self.ui)
        self.assertIn("function staleBanner(", self.ui)

    def test_greet_calls_it_BEFORE_its_early_returns(self):
        """greet() bails when no operator name is configured. A warning wired
        after that check would hide exactly when the console is least set up."""
        body = self.ui.split("function greet(w) {", 1)[1]
        call = body.find("staleBanner(")
        bail = body.find("if (!name) return")
        self.assertGreater(call, -1, "greet must call it")
        self.assertGreater(bail, -1)
        self.assertLess(call, bail, "the call must precede the early return")

    def test_it_is_a_SEPARATE_element_from_the_connectivity_banner(self):
        """Both can be true at once — a server that is stale AND unreachable —
        and one element would let either erase the other."""
        self.assertIn('<div id="banner">', self.ui)
        self.assertIn('<div id="stalebanner">', self.ui)


if __name__ == "__main__":
    unittest.main()
