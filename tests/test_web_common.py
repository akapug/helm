#!/usr/bin/env python3
"""helm/web_common.py — the console's shared low-level bindings.

THIS FILE FOUND A DEFECT ON ITS FIRST RUN, which is the argument for writing
it. `web_common` reached its `time` binding ONLY through the globals() copy it
takes from `helm.web` at import — so the name existed when web.py had already
been imported, and was ABSENT on a direct `from helm import web_common`. Two
functions use it:

  * `_cockpit_beat()` raised NameError outright;
  * `code_drift()` caught the NameError in its own fail-open and answered
    "no drift" — the half-live report silenced by an import-order accident,
    on the one function whose entire job is to stop the console lying about
    which code it is running. The owner had already run a 20h-stale server
    behind current HTML once; that is the incident code_drift exists for.

Nothing caught it because nothing imported the module. The census said so —
"wired, unverified" — for as long as the module has existed. The cure is one
explicit `import time`; these are the arms that keep it.

HERMETIC: no server is started, the real ~/.helm is never opened, and every
module-level list this file perturbs (`_DEFAULT_ROOM`, `_COCKPIT_BEAT`) is
restored in tearDown, because they are process-wide caches other suites read.
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests import _tmphome               # noqa: E402,F401 — precedes helm.*
from helm import web_common as wc        # noqa: E402


class TheDriftReport(unittest.TestCase):
    """`helm web` has TWO deployment lifetimes in one process: the UI is
    assembled per request and is always current, while the server module is
    frozen at import. A change touching both halves goes HALF-LIVE the moment
    it lands, and nothing on the page can tell you."""

    def setUp(self):
        self._real_stamp = wc._source_stamp

    def tearDown(self):
        wc._source_stamp = self._real_stamp

    def test_a_NEWER_tree_is_reported_with_the_gap_it_measured(self):
        """THE ARM THE MISSING `time` BINDING WOULD HAVE FAILED. Every field
        of the report is formatted through `time`, so an unbound name here
        was swallowed by the fail-open below and the answer was None — a
        stale server reporting itself current."""
        wc._source_stamp = lambda: wc._LOADED_STAMP + 3600
        drift = wc.code_drift()
        self.assertIsNotNone(drift)
        self.assertEqual(drift["stale_seconds"], 3600)
        self.assertEqual(drift["loaded"],
                         time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                       time.gmtime(wc._LOADED_STAMP)))
        self.assertEqual(drift["on_disk"],
                         time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                       time.gmtime(wc._LOADED_STAMP + 3600)))
        self.assertIn("restart helm web", drift["note"])

    def test_an_UNCHANGED_tree_reports_no_drift(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the FIRST line of the method: a stamp one hour forward yields a report with stale_seconds 3600, on the same code_drift() observable. The rung cannot link them because every call mints a fresh producer identity.
        """The must-be-silent half. Without it the arm above passes against a
        function that reports drift unconditionally, and an operator learns
        to ignore the banner — which is the same as not having it."""
        wc._source_stamp = lambda: wc._LOADED_STAMP + 3600
        self.assertEqual(wc.code_drift()["stale_seconds"], 3600)
        wc._source_stamp = lambda: wc._LOADED_STAMP
        self.assertIsNone(wc.code_drift())
        wc._source_stamp = lambda: wc._LOADED_STAMP - 60   # clock went back
        self.assertIsNone(wc.code_drift())

    def test_an_UNREADABLE_tree_reports_no_drift_rather_than_zero(self):  # noqa: VACUOUS_ASSERTION — the last two lines ARE the unconditional control: with a real stamp the same code_drift() call reports stale_seconds 5. Each call mints a fresh producer identity, so the rung cannot link the pair.
        """`_source_stamp` answers 0.0 when the walk fails. Zero is not "the
        tree is ancient"; it is "we could not look", and reporting a drift of
        50-odd years from it would be the self-check breaking the page it is
        describing. POSITIVE CONTROL: the same call with a real stamp does
        produce a report, so the None here is the guard and not a dead arm."""
        wc._source_stamp = lambda: 0.0
        self.assertIsNone(wc.code_drift())
        wc._source_stamp = lambda: wc._LOADED_STAMP + 5
        self.assertEqual(wc.code_drift()["stale_seconds"], 5)

    def test_the_check_FAILS_OPEN_on_any_exception(self):
        """A broken self-check must never take down the console it is trying
        to describe. Stated in the docstring; this is what holds it — and the
        control on the line after is what keeps the fail-open from hiding a
        permanently-broken function, which is exactly what it DID hide."""
        def boom():
            raise RuntimeError("the walk blew up")
        wc._source_stamp = boom
        self.assertIsNone(wc.code_drift())
        wc._source_stamp = lambda: wc._LOADED_STAMP + 7
        self.assertEqual(wc.code_drift()["stale_seconds"], 7)

    def test_the_loaded_stamp_is_frozen_at_IMPORT_and_is_a_real_time(self):
        """The whole report is a comparison against this number. If it were
        recomputed per call the two sides would always agree and the banner
        could never fire; if it were zero the report would be suppressed
        forever by the arm above."""
        self.assertGreater(wc._LOADED_STAMP, 0.0)
        first = wc._LOADED_STAMP
        self._real_stamp()            # a fresh reading must not move it
        self.assertEqual(wc._LOADED_STAMP, first)
        # and the frozen number is a PAST time, not a future one — a stamp
        # ahead of the tree makes `now <= _LOADED_STAMP` permanently true and
        # suppresses the banner for the life of the process.
        self.assertLessEqual(wc._LOADED_STAMP, time.time() + 1)


class TheSourceStamp(unittest.TestCase):
    """Self-referential on purpose: it resolves from __file__, so it answers
    about the tree THIS server is running, never about whichever checkout the
    caller happens to be standing in."""

    def test_the_stamp_is_drawn_from_the_PACKAGE_directory(self):
        """Re-walked independently rather than re-derived from the function:
        the value must be the newest .py mtime under the package that holds
        web_common.py itself."""
        pkg = os.path.dirname(os.path.abspath(wc.__file__))
        newest = 0.0
        for root, _dirs, names in os.walk(pkg):
            if "__pycache__" in root:
                continue
            for n in names:
                if n.endswith(".py"):
                    newest = max(newest, os.path.getmtime(os.path.join(root,
                                                                       n)))
        self.assertGreater(newest, 0.0)
        self.assertEqual(wc._source_stamp(), newest)

    def test_an_unwalkable_tree_answers_ZERO_rather_than_raising(self):
        """0.0 is the typed "could not look", and `code_drift` reads it as
        such. Letting the OSError out instead would take down whichever
        request asked for the banner. POSITIVE CONTROL on the same call: the
        unpatched walk answers a real time, so the 0.0 is the guard firing
        and not a function that always answers zero."""
        with mock.patch("os.walk", side_effect=OSError("unreadable")):
            self.assertEqual(wc._source_stamp(), 0.0)
        self.assertGreater(wc._source_stamp(), 0.0)


class TheQueryHelper(unittest.TestCase):
    def test_q1_takes_the_FIRST_value_and_falls_back_to_the_default(self):
        """parse_qs hands back a list per key. Taking the list itself would
        push `['main']` into a room name; taking [0] off an empty list would
        IndexError on `?room=` with nothing after it, which a browser sends
        whenever a field is cleared."""
        qs = {"room": ["lane", "second"], "empty": [], "blank": [""]}
        self.assertEqual(wc._q1(qs, "room"), "lane")
        self.assertEqual(wc._q1(qs, "blank"), "")
        self.assertIsNone(wc._q1(qs, "missing"))
        self.assertEqual(wc._q1(qs, "missing", "fallback"), "fallback")
        self.assertEqual(wc._q1(qs, "empty", "fallback"), "fallback")


class TheOpeningRoom(unittest.TestCase):
    """A LITERAL "main" was the old default and it put the owner's console in
    the one room his fleet does not talk in. The derivation is asked twice —
    the process cwd, then the code's own directory — and reserved #main is the
    honest last resort, never the first answer."""

    def setUp(self):
        self._prior = list(wc._DEFAULT_ROOM)
        del wc._DEFAULT_ROOM[:]

    def tearDown(self):
        wc._DEFAULT_ROOM[:] = self._prior

    def test_a_derived_room_is_used_instead_of_main(self):
        from helm import seats
        with mock.patch.object(seats, "derive_home_room_typed",
                               return_value=(seats.DERIVE_OK, "a-project")):
            self.assertEqual(wc.default_room(), "a-project")

    def test_a_project_less_box_falls_back_to_reserved_main(self):
        """DERIVE_NONE from the cwd AND from the package directory is what a
        systemd unit with no WorkingDirectory genuinely looks like."""
        from helm import seats
        with mock.patch.object(seats, "derive_home_room_typed",
                               return_value=(seats.DERIVE_NONE, None)):
            self.assertEqual(wc.default_room(), "main")

    def test_the_SECOND_ask_is_the_packages_own_directory(self):
        """The fix that cannot drift with a unit file nobody remembers to
        edit: when the cwd derives nothing, the code's own location is asked,
        and THAT answer is the one that ships."""
        from helm import seats
        seen = []

        def fake(path):
            seen.append(path)
            if len(seen) == 1:
                return seats.DERIVE_NONE, None
            return seats.DERIVE_OK, "from-the-package"
        with mock.patch.object(seats, "derive_home_room_typed", fake):
            self.assertEqual(wc.default_room(), "from-the-package")
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[1], wc.PACKAGE_DIR)

    def test_a_BROKEN_derivation_never_takes_the_server_down(self):
        """Homing is a convenience; serving the console is not. An exception
        out of the derivation must land on #main, and the control beside it
        proves #main here is the fallback rather than the only answer this
        function can produce."""
        from helm import seats
        with mock.patch.object(seats, "derive_home_room_typed",
                               side_effect=RuntimeError("no git here")):
            self.assertEqual(wc.default_room(), "main")
        del wc._DEFAULT_ROOM[:]
        with mock.patch.object(seats, "derive_home_room_typed",
                               return_value=(seats.DERIVE_OK, "real")):
            self.assertEqual(wc.default_room(), "real")

    def test_the_answer_is_CACHED_and_the_derivation_runs_once(self):
        """The derivation shells out to git. Re-running it per request would
        put a subprocess on the console's hottest path."""
        from helm import seats
        with mock.patch.object(seats, "derive_home_room_typed",
                               return_value=(seats.DERIVE_OK, "once")) as m:
            self.assertEqual(wc.default_room(), "once")
            self.assertEqual(wc.default_room(), "once")
            self.assertEqual(wc.default_room(), "once")
        self.assertEqual(m.call_count, 1)


class TheCockpitBeat(unittest.TestCase):
    def setUp(self):
        self._prior = list(wc._COCKPIT_BEAT)

    def tearDown(self):
        wc._COCKPIT_BEAT[:] = self._prior

    def test_the_beat_stamps_a_real_time_and_does_not_raise(self):
        """The function that raised NameError outright before `time` was
        imported explicitly. It sits on the 2s poll path, so it had to be
        cheap — and it was cheap by being broken."""
        wc._COCKPIT_BEAT[0] = 0.0
        before = time.time()
        wc._cockpit_beat()
        self.assertEqual(len(wc._COCKPIT_BEAT), 1)
        self.assertGreater(wc._COCKPIT_BEAT[0], 0.0)
        self.assertGreaterEqual(wc._COCKPIT_BEAT[0], before)
        self.assertLessEqual(wc._COCKPIT_BEAT[0], time.time())


class TheMutationBearer(unittest.TestCase):
    def test_every_POST_has_a_token_to_demand(self):
        """A hostile page can fire cross-origin POSTs at 127.0.0.1 but can
        never READ our UI to learn the token. An empty or None token would
        make the 403 unreachable and every mutation open to any page the
        owner has in another tab."""
        self.assertIsInstance(wc.MUTATION_TOKEN, str)
        self.assertNotEqual(wc.MUTATION_TOKEN, "")
        self.assertEqual(wc.MUTATION_TOKEN.strip(), wc.MUTATION_TOKEN)

    def test_the_loopback_bind_is_not_a_wildcard(self):
        """BIND is what keeps the console off the LAN. A wildcard here would
        publish an owner surface with a per-process bearer to the network."""
        self.assertEqual(wc.BIND, "127.0.0.1")
        self.assertIsInstance(wc.DEFAULT_PORT, int)
        self.assertGreater(wc.DEFAULT_PORT, 1024)


if __name__ == "__main__":
    unittest.main()
