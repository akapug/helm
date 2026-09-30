"""The nightly release dry run: its launch, its record, its streak, its timer.

No release runs on a live host here and no timer is installed. The launcher
is planted. Most arms hand `run` a launcher that writes the log a launch would
bring home; one hands it the REAL release tool against the release fixture's
world (tests/test_release_tool.py: a trunk, a public and a private repository
on disk, stub gh and gitleaks), so the whole path from the hub's fresh fetch
to the recorded verdict runs once for real.
"""
import contextlib
import datetime
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from helm import eventledger, gatecanary, releasenightly as rn
from tests.test_release_tool import OWNER, TOOL, ReleaseFixture, _git, _write

_SHA_CMD = ["fab", "nodes", "--json"]
# The real `subprocess.run`, captured before any test patch touches it. The
# surgical `fab nodes --json` seams delegate every other call (git, the real
# release tool) to this one, so a patched test still drives the unpatched
# path for real.
_REAL_RUN = subprocess.run

SHA_A, SHA_B = "a" * 40, "b" * 40
FIRST = datetime.date(2026, 6, 1)
# What the release tool prints first under --nightly.
BANNER = "helm     release: NIGHTLY (a dry run of trunk as it stands)\n"


def _night(n):
    return (FIRST + datetime.timedelta(days=n)).isoformat()


def _at(n, hour, minute=0):
    """Epoch seconds of night `n` at hour:minute, this host's local time (a
    June date, so no clock change falls inside the fixture)."""
    d = FIRST + datetime.timedelta(days=n)
    return time.mktime((d.year, d.month, d.day, hour, minute, 0, 0, 0, -1))


def _rows(global_dir):
    rows, why = eventledger.checked_events(rn.history_path(global_dir),
                                           strict=True)
    assert why is None, why
    return rows


def _patch_fab_nodes(hosts):
    """Make `fab nodes --json` answer with the given node list, while every
    other subprocess call (git, the real release tool) runs for real. This is
    the seam that lets the fail-closed host check pass in tests: the check
    names the host against a controlled fleet, and nothing else is faked."""
    def respond(args, *a, **kw):
        if list(map(str, args)) == _SHA_CMD:
            body = json.dumps({"generated": "test",
                               "nodes": [{"host": h} for h in hosts]}).encode()
            return mock.Mock(returncode=0, stdout=body, stderr=b"")
        return _REAL_RUN(args, *a, **kw)
    return mock.patch.object(rn.subprocess, "run", side_effect=respond)


def _patch_fab_nodes_raising(exc):
    """Make `fab nodes --json` raise `exc` (fab missing, a timeout), while
    every other subprocess call runs for real. Returns a ContextManager."""
    def respond(args, *a, **kw):
        if list(map(str, args)) == _SHA_CMD:
            raise exc
        return _REAL_RUN(args, *a, **kw)
    return mock.patch.object(rn.subprocess, "run", side_effect=respond)


class StreakTest(unittest.TestCase):
    """The record read as a streak of green NIGHTS."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="helm-nightly-streak-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def record(self, n, verdict, step=None, source=rn.TIMER, hour=2):
        started = _at(n, hour, 30)
        green = verdict == rn.GREEN
        row = rn.history_row(verdict, started, started + 420, source,
                             step=step, reason="planted", trunk=SHA_A,
                             candidate=SHA_B if green else None,
                             host="node-1", log="/planted.log",
                             code=0 if green else 1)
        self.assertTrue(rn.append_verdict(row, self.home))

    def held(self, n, hour=12):
        return rn.streak(self.home, now=_at(n, hour))

    def test_seven_green_nights_read_seven_of_seven(self):
        for n in range(7):
            self.record(n, rn.GREEN)
        held = self.held(6)
        self.assertEqual((held["state"], held["count"]), (rn.KNOWN, 7))
        self.assertEqual(rn.streak_line(held),
                         "7 of 7 consecutive green nightlies; last red: none")
        # six read six, and the night before the first recorded one stops it
        held = self.held(5)
        self.assertEqual(rn.streak_line(held),
                         "6 of 7 consecutive green nightlies; last red: none")
        self.assertEqual((held["stopped"]["night"], held["stopped"]["verdict"]),
                         (_night(-1), rn.MISSING))
        # an eighth still reads the falsifier met, and says how long it runs
        self.record(7, rn.GREEN)
        self.assertEqual(rn.streak_line(self.held(7)),
                         "7 of 7 consecutive green nightlies (a streak of 8); "
                         "last red: none")

    def test_a_red_night_resets_the_streak(self):  # noqa: VACUOUS_ASSERTION — a count of 0 is the contract; the whole line pins the red night's date and step
        for n in range(7):
            self.record(n, rn.GREEN)
        self.record(7, rn.RED, step="gate/attribution")
        held = self.held(7)
        self.assertEqual(held["count"], 0)
        self.assertEqual(rn.streak_line(held),
                         "0 of 7 consecutive green nightlies; last red: %s "
                         "gate/attribution" % _night(7))
        # a green rerun the same night does not wash the red night out
        self.record(7, rn.GREEN, hour=9)
        self.assertEqual(self.held(7)["count"], 0)
        for n in (8, 9):
            self.record(n, rn.GREEN)
        self.assertEqual(rn.streak_line(self.held(9)),
                         "2 of 7 consecutive green nightlies; last red: %s "
                         "gate/attribution" % _night(7))

    def test_a_missing_night_breaks_the_streak(self):  # noqa: VACUOUS_ASSERTION — the first read pins a count of 2 before any 0 is asserted, and the stopping night is pinned each time
        for n in (0, 1, 2, 4, 5):          # night 3: nothing ran
            self.record(n, rn.GREEN)
        held = self.held(5)
        self.assertEqual(held["count"], 2)
        self.assertEqual((held["stopped"]["night"], held["stopped"]["verdict"]),
                         (_night(3), rn.MISSING))
        self.assertIn("no nightly run was recorded", held["stopped"]["reason"])
        with mock.patch.object(rn, "timer_installed", return_value=False), \
                mock.patch.object(rn, "configured_host", return_value=None):
            lines = rn.status_lines(held)
        self.assertEqual(lines[0], "helm release nightly: 2 of 7 consecutive "
                                   "green nightlies; last red: none")
        self.assertIn("the streak stops at %s: MISSING" % _night(3),
                      "\n".join(lines))
        self.assertIn("NOT INSTALLED", "\n".join(lines))
        # the hub ran and the dry run did not (the host was down): MISSING too
        self.record(6, rn.MISSING)
        held = self.held(6)
        self.assertEqual(held["count"], 0)
        self.assertEqual((held["stopped"]["night"], held["stopped"]["verdict"]),
                         (_night(6), rn.MISSING))

    def test_tonight_is_missing_only_once_it_is_due(self):
        for n in range(3):
            self.record(n, rn.GREEN)
        # 01:00, before tonight's timer: last night is the newest due night
        self.assertEqual(rn.streak(self.home, now=_at(3, 1))["count"], 3)
        # noon, past the timer and its grace, and nothing ran tonight
        late = rn.streak(self.home, now=_at(3, 12))
        self.assertEqual(late["count"], 0)
        self.assertEqual((late["stopped"]["night"], late["stopped"]["verdict"]),
                         (_night(3), rn.MISSING))
        # a run already recorded tonight counts before the grace is out
        self.record(3, rn.GREEN)
        self.assertEqual(rn.streak(self.home, now=_at(3, 3))["count"], 4)

    def test_a_manual_run_neither_counts_nor_breaks(self):
        for n in range(3):
            self.record(n, rn.GREEN)
        self.record(2, rn.RED, step="gate/battery", source=rn.MANUAL, hour=15)
        held = self.held(2, hour=18)
        self.assertEqual(held["count"], 3)
        self.assertEqual(held["last_manual"]["step"], "gate/battery")
        # a manual green does not fill a night the timer missed
        self.record(3, rn.GREEN, source=rn.MANUAL, hour=9)
        held = self.held(3)
        self.assertEqual(held["count"], 0)
        self.assertEqual(held["stopped"]["night"], _night(3))
        self.assertEqual(rn.streak_line(held),
                         "0 of 7 consecutive green nightlies; last red: none")

    def test_an_unreadable_record_reads_unknown(self):
        for n in range(7):
            self.record(n, rn.GREEN)
        # the control: read whole, the same record is seven green nights
        self.assertEqual(self.held(6)["count"], 7)
        path = rn.history_path(self.home)
        with open(path, "rb") as f:
            whole = f.read()
        for label, extra in (
                ("a line that is not JSON", b"not json\n"),
                ("a verdict of the wrong shape", json.dumps(
                    {"v": 1, "event": rn.VERDICT_EVENT,
                     "verdict": rn.GREEN}).encode() + b"\n")):
            with self.subTest(label):
                with open(path, "wb") as f:
                    f.write(whole + extra)
                held = self.held(6)
                self.assertEqual((held["state"], held["count"]),
                                 (rn.UNKNOWN, None))
                line = rn.streak_line(held)
                self.assertTrue(line.startswith("UNKNOWN"), line)
                self.assertNotIn(" of 7", line)
        # a record that cannot be opened as a file at all
        os.unlink(path)
        os.makedirs(path)
        held = self.held(6)
        self.assertEqual((held["state"], held["count"]), (rn.UNKNOWN, None))
        # the verb says UNKNOWN, never a count, and exits 3
        out = io.StringIO()
        with mock.patch.object(rn, "streak", return_value=held), \
                mock.patch.object(rn, "timer_installed", return_value=True), \
                mock.patch.object(rn, "configured_host", return_value="node-1"), \
                contextlib.redirect_stdout(out):
            rc = rn.cmd_release(["nightly"])
        self.assertEqual(rc, 3)
        self.assertIn("helm release nightly: UNKNOWN", out.getvalue())
        self.assertNotIn(" of 7", out.getvalue())


class TimerTest(unittest.TestCase):

    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="helm-nightly-cfg-")
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        patch = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.cfg})
        patch.start()
        self.addCleanup(patch.stop)
        self._fab = _patch_fab_nodes(["node-1", "node-0"])
        self._fab.start()
        self.addCleanup(self._fab.stop)
        os.environ.pop(rn.HOST_ENV, None)

    def test_the_timer_unit_renders_the_chosen_host_and_time(self):
        from helm import work
        ok, detail = rn.write_host("node-1")
        self.assertTrue(ok, detail)
        with open(rn.config_env_path(), encoding="utf-8") as f:
            self.assertEqual(f.read(), "%s=node-1\n" % rn.HOST_ENV)
        self.assertEqual(rn.configured_host(), "node-1")
        spath, service, tpath, timer = rn.timer_units()
        here = os.path.dirname(os.path.dirname(os.path.abspath(rn.__file__)))
        cwd = work.find_root(here) or here
        self.assertIn("WorkingDirectory=%s\n" % cwd, service)
        self.assertIn("EnvironmentFile=-%s\n" % rn.config_env_path(), service)
        self.assertIn(" release nightly run --repo %s --timer\n" % cwd, service)
        # the service names itself to its run as the timer
        self.assertIn(
            "Environment=HELM_RELEASE_NIGHTLY_SOURCE=timer\n", service)
        self.assertIn("OnCalendar=*-*-* 02:30:00\n", timer)
        self.assertIn("Persistent=true\n", timer)
        self.assertTrue(spath.endswith(os.sep + "helm-release-nightly.service"))
        self.assertTrue(tpath.endswith(os.sep + "helm-release-nightly.timer"))
        # the service's run launches on the host the config names
        self.assertEqual(rn.launcher(rn.configured_host())[:2], ["fab", "node-1"])

    def test_the_night_keeps_clear_of_the_gate_canary(self):
        """The dry run starts an hour before the canary's serial suite takes
        the same build host."""
        def minutes(at):
            h, m, _s = (int(x) for x in at.split(":"))
            return h * 60 + m
        self.assertGreaterEqual(
            (minutes(gatecanary.TIMER_AT) - minutes(rn.TIMER_AT)) % (24 * 60), 60)

    def test_a_host_that_is_not_a_plain_name_is_refused(self):
        ok, detail = rn.write_host("node-1; rm -rf ~")
        self.assertFalse(ok)
        self.assertIn("plain host name", detail)
        self.assertFalse(os.path.exists(rn.config_env_path()))
        _write(rn.config_env_path(), "%s=a b\n" % rn.HOST_ENV)
        self.assertIsNone(rn.configured_host())
        _write(rn.config_env_path(), "# the host\n%s=node-b\n" % rn.HOST_ENV)
        self.assertEqual(rn.configured_host(), "node-b")

    def test_the_install_writes_nothing_when_switched_off(self):
        with mock.patch.dict(os.environ, {rn.TIMER_ENV: "0"}), \
                mock.patch.object(rn.subprocess, "run") as ran:
            ok, detail = rn.ensure_timer()
        self.assertIsNone(ok)
        self.assertIn(rn.TIMER_ENV, detail)
        self.assertEqual(ran.call_count, 0)

    def test_the_suite_plants_the_switch(self):
        """tests/__init__.py turns this install off for the whole suite, as it
        does the canary's and autocompact's: a test that reaches
        `ensure_timer` in-process or in a child must never write this host's
        units or run its systemctl."""
        if "tests" not in sys.modules:
            self.skipTest("runner did not import the tests package, so its "
                          "suite-wide environment is not planted")
        self.assertEqual(sys.modules["tests"].PLANTED.get(rn.TIMER_ENV), "0")
        self.assertEqual(rn.timer_switched_off(), "0")


class RunTest(unittest.TestCase):
    """`run` with a planted launcher: the hub's half, from the fresh fetch to
    the recorded row."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-nightly-run-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = dict(os.environ, GIT_AUTHOR_NAME="t",
                        GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="t",
                        GIT_COMMITTER_EMAIL="t@example.invalid")
        for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            self.env.pop(k, None)
        self.src = os.path.join(self.tmp, "src")
        os.makedirs(self.src)
        _git(self.src, "init", "-q", "-b", "main", "--template=")
        self.commit("the first trunk")
        self.hub = os.path.join(self.tmp, "hub")
        _git(self.tmp, "clone", "-q", self.src, self.hub)
        _git(self.hub, "remote", "add", "aspublic",
             "git@github.com:example-owner/helm.git")
        self.home = os.path.join(self.tmp, "global")
        self._fab = _patch_fab_nodes(["node-1", "node-0"])
        self._fab.start()
        self.addCleanup(self._fab.stop)

    def commit(self, text):
        _write(os.path.join(self.src, "file.txt"), text + "\n")
        _git(self.src, "add", "-A")
        _git(self.src, "commit", "-q", "-m", text, env=self.env)
        return _git(self.src, "rev-parse", "HEAD")

    def launch(self, text, code, host="node-1"):
        seen = {}

        def runner(argv, log, env):
            seen["argv"] = list(argv)
            room = argv[argv.index("--repo") + 1]
            seen["room"] = room
            seen["head"] = _git(room, "rev-parse", "HEAD")
            with open(log, "a", encoding="utf-8") as f:
                f.write(text)
            return code
        return rn.run(repo=self.hub, global_dir=self.home, runner=runner,
                      host=host, source=rn.TIMER), seen

    def test_the_launch_is_the_release_tools_nightly_on_the_host_at_fresh_trunk(self):
        trunk = self.commit("after the hub cloned")   # the hub has not fetched it
        row, seen = self.launch(BANNER + "NIGHTLY  GREEN trunk=%s candidate=%s\n"
                                % (trunk, SHA_B), 0)
        cut = seen["argv"].index("--")
        self.assertEqual(seen["argv"][:cut], ["fab", "node-1", "--key", rn.FAB_KEY,
                                              "--repo", seen["room"]])
        self.assertEqual(seen["argv"][cut + 1:], [
            "python3", "scripts/release/release.py", "--nightly",
            "--trunk", trunk,
            "--public", "https://github.com/example-owner/helm.git",
            "--private", self.src])
        self.assertEqual(seen["head"], trunk)
        self.assertFalse(os.path.exists(seen["room"]), "the peek room was kept")
        self.assertEqual((row["verdict"], row["trunk"], row["candidate"],
                          row["host"], row["source"], row["exit"]),
                         (rn.GREEN, trunk, SHA_B, "node-1", rn.TIMER, 0))
        self.assertTrue(row["recorded"])
        self.assertEqual([r["id"] for r in _rows(self.home)], [row["id"]])
        with open(row["log"], encoding="utf-8") as f:
            self.assertIn("NIGHTLY  GREEN", f.read())

    def test_a_failing_step_records_red_naming_that_step(self):
        trunk = _git(self.src, "rev-parse", "HEAD")
        row, _seen = self.launch(
            BANNER + "REFUSED  seat attribution: 1 line(s) in helm/ ...\n"
            "NIGHTLY  RED step=gate/attribution trunk=%s\n" % trunk, 1)
        self.assertEqual((row["verdict"], row["step"], row["trunk"], row["exit"]),
                         (rn.RED, "gate/attribution", trunk, 1))
        self.assertIn("seat attribution", row["reason"])
        [kept] = _rows(self.home)
        self.assertEqual((kept["verdict"], kept["step"]),
                         (rn.RED, "gate/attribution"))
        self.assertTrue(rn.streak_line(rn.streak(self.home)).endswith(
            "last red: %s gate/attribution" % row["night"]))

    def test_a_launch_that_never_reached_the_host_is_a_missing_night(self):
        row, _seen = self.launch("ssh: connect to host node-1 port 22: No route "
                                 "to host\nfab: remote launch failed\n", 255)
        self.assertEqual((row["verdict"], row["step"], row["exit"]),
                         (rn.MISSING, None, 255))
        self.assertIn("did not start on node-1", row["reason"])
        self.assertIn("remote launch failed", row["reason"])
        held = rn.streak(self.home)
        self.assertEqual((held["count"], held["stopped"]["verdict"]),
                         (0, rn.MISSING))

    def test_a_tool_that_started_and_gave_no_verdict_is_red(self):  # noqa: VACUOUS_ASSERTION — the log table is a non-empty literal and each case pins RED at tool and its reason
        for text in (BANNER + "Traceback (most recent call last):\n",
                     "fab: id=lane-1a2b-1  LOG: node-1:~/fab/logs/x.log  "
                     "(reattach: fab tail node-1 x)\nexit 97\n"):
            with self.subTest(text=text[:20]):
                row, _seen = self.launch(text, 1)
                self.assertEqual((row["verdict"], row["step"]), (rn.RED, "tool"))
                self.assertIn("gave no verdict", row["reason"])

    def test_green_needs_the_tools_green_line_and_a_clean_exit(self):  # noqa: VACUOUS_ASSERTION — the unconditional control reads GREEN first; the table is a non-empty literal and each case pins RED and its reason
        trunk = _git(self.src, "rev-parse", "HEAD")
        line = BANNER + "NIGHTLY  GREEN trunk=%s candidate=%s\n"
        row, _seen = self.launch(line % (trunk, SHA_B), 0)
        self.assertEqual(row["verdict"], rn.GREEN)       # the control
        for text, code, said in ((line % (trunk, SHA_B), 3, "exit 3"),
                                 (line % (SHA_A, SHA_B), 0, "names trunk"),
                                 (line % (trunk, "none"), 0, "no candidate")):
            with self.subTest(said=said):
                row, _seen = self.launch(text, code)
                self.assertEqual((row["verdict"], row["step"]), (rn.RED, "tool"))
                self.assertIn(said, row["reason"])

    def test_no_host_is_a_missing_night_and_launches_nothing(self):
        called = []
        with mock.patch.object(rn, "configured_host", return_value=None):
            row = rn.run(repo=self.hub, global_dir=self.home,
                         runner=lambda *a: called.append(a), source=rn.TIMER)
        self.assertEqual((row["verdict"], called), (rn.MISSING, []))
        self.assertIn("--install-timer --host", row["reason"])
        self.assertTrue(row["recorded"])

    def test_a_trunk_that_cannot_be_fetched_is_a_missing_night(self):
        _git(self.hub, "remote", "set-url", "origin",
             os.path.join(self.tmp, "gone"))
        called = []
        row = rn.run(repo=self.hub, global_dir=self.home, host="node-1",
                     runner=lambda *a: called.append(a), source=rn.TIMER)
        self.assertEqual((row["verdict"], called), (rn.MISSING, []))
        self.assertIn("fetched fresh", row["reason"])

    def test_a_dirty_reused_room_is_a_missing_night_and_runs_nothing(self):
        """fab ships a room's WORKING TREE, uncommitted and untracked files
        too, so a peek room at trunk that somebody left dirty would run a
        release tool that is not trunk's, and its green would be about that
        tool. The night did not run the dry run of trunk: MISSING, nothing
        launched, and the room is left as it was found."""
        from helm import work
        trunk = _git(self.src, "rev-parse", "HEAD")
        root = work.find_root(os.path.realpath(self.hub)) \
            or os.path.realpath(self.hub)
        rc, peeked = work.peek(root, trunk)
        self.assertEqual(rc, 0, peeked)
        room = peeked["path"]
        _write(os.path.join(room, "file.txt"), "not trunk's bytes\n")
        green = BANNER + "NIGHTLY  GREEN trunk=%s candidate=%s\n" % (trunk, SHA_B)
        row, seen = self.launch(green, 0)
        self.assertEqual((row["verdict"], seen), (rn.MISSING, {}))
        self.assertIn("uncommitted", row["reason"])
        self.assertTrue(row["recorded"])
        with open(os.path.join(room, "file.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "not trunk's bytes\n")
        # the control: the same room, clean again, is reused and launched
        _git(room, "checkout", "-q", "--", "file.txt")
        row, seen = self.launch(green, 0)
        self.assertEqual((row["verdict"], seen["room"]), (rn.GREEN, room))

    def test_a_host_fab_does_not_list_is_a_missing_night(self):
        """(d) `run` with a host that `fab nodes --json` does not list returns
        MISSING, naming the absence, and never hands the launcher a name the
        fleet does not own. The planted runner is never invoked."""
        with _patch_fab_nodes(["node-0"]):
            row, seen = self.launch(
                BANNER + "NIGHTLY  GREEN trunk=%s candidate=%s\n"
                % (_git(self.src, "rev-parse", "HEAD"), SHA_B),
                0, host="node-1")
        self.assertEqual(row["verdict"], rn.MISSING)
        self.assertIn("not in the fleet", row["reason"])
        self.assertEqual(seen, {}, "no launch reached the planted runner")
        self.assertTrue(row["recorded"])

    def test_run_never_launches_when_fab_cannot_answer(self):
        """(d) `run` when `fab nodes --json` cannot answer (fab missing) also
        returns MISSING without launching."""
        with _patch_fab_nodes_raising(FileNotFoundError("no fab")):
            row, seen = self.launch(BANNER + "NIGHTLY  GREEN trunk=%s "
                                     "candidate=%s\n"
                                     % (_git(self.src, "rev-parse", "HEAD"),
                                        SHA_B), 0, host="node-1")
        self.assertEqual(row["verdict"], rn.MISSING)
        self.assertIn("not on PATH", row["reason"])
        self.assertEqual(seen, {}, "no launch reached the planted runner")
        self.assertTrue(row["recorded"])


class HostCheckTest(unittest.TestCase):
    """`write_host` and `run` refuse a host that `fab nodes --json` does not
    list, and refuse when fab cannot answer at all. The fleet's own node list
    is the only authority: no hand-written allowlist, and the launcher is never
    handed a name the fleet does not own."""

    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="helm-nightly-host-")
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        patch = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.cfg})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop(rn.HOST_ENV, None)

    def _fab(self, stdout=b"", returncode=0, timeout=None, raise_=None,
             args=None):
        """Patch `subprocess.run` inside `fab_hosts` to answer one controlled
        way. `raise_`, if an exception, is re-raised on the call (fab missing,
        a timeout). `args`, if given, asserts the command was
        `fab nodes --json`; `timeout` asserts the timeout it was given. Returns a
        ContextManager of the patched mock."""
        def run(*a, **kw):
            if args is not None and a:
                self.assertEqual(list(a[0]), ["fab", "nodes", "--json"], a)
            if timeout is not None and kw.get("timeout") != timeout:
                raise AssertionError(
                    "expected timeout %s, got %s" % (timeout, kw.get("timeout")))
            if raise_ is not None:
                raise raise_
            r = mock.Mock(returncode=returncode)
            r.stdout = stdout
            r.stderr = b""
            return r
        m = mock.Mock(side_effect=run)
        return mock.patch.object(rn.subprocess, "run", m)

    def test_a_host_fab_does_not_list_is_refused_before_it_is_written(self):
        """(a) A host that is not a fab node is refused. `write_host` must not
        plant the config file. The planted fleet lists `node-0`; `node-1` is
        not one of them. At trunk this same assertion fails on behaviour
        because the old `write_host` only checked shape and `node-1` passes it."""
        with self._fab(stdout=b'{"generated": "x", "nodes": [{"host": "node-0"}]}'):
            ok, detail = rn.write_host("node-1")
        self.assertFalse(ok, detail)
        self.assertIn("not in the build fleet", detail)
        self.assertFalse(os.path.exists(rn.config_env_path()),
                         "a host fab does not know must never be written")

    def test_a_host_that_is_a_fab_verb_is_refused(self):  # noqa: VACUOUS_ASSERTION — five named verbs are checked, each a real subTest; the fleet is pinned to node-0 and the verb names are asserted, so the loop cannot pass vacuously
        """(b) A name that looks like a host but is a fab verb — `gc`, `test`,
        `gate` — must be refused the same way: the launcher would run `fab gc`
        (the verb), not a host pin. The fleet lists `node-0`; `gc` is absent.
        The patch makes this a plain behaviour failure, not a missing-name error:
        `gc` passes the plain-shape check at trunk and would be written there."""
        for verb in ("gc", "test", "gate", "status", "kill"):
            with self.subTest(host=verb):
                with self._fab(stdout=b'{"nodes": [{"host": "node-0"}]}'):
                    ok, detail = rn.write_host(verb)
                self.assertFalse(ok, detail)
                self.assertIn("not in the build fleet", detail)
                self.assertFalse(os.path.exists(rn.config_env_path()))

    def test_a_listed_host_is_accepted(self):
        """(b) A host that `fab nodes --json` actually lists is accepted and
        written, exactly as before."""
        with self._fab(stdout=b'{"nodes": [{"host": "node-1"}, "junk"]}'):
            ok, detail = rn.write_host("node-1")
        self.assertTrue(ok, detail)
        self.assertTrue(os.path.exists(rn.config_env_path()))
        with open(rn.config_env_path(), encoding="utf-8") as f:
            self.assertEqual(f.read(), "%s=node-1\n" % rn.HOST_ENV)
        self.assertEqual(rn.configured_host(), "node-1")

    def test_fab_missing_refuses(self):
        """(c) `fab` not on PATH: `subprocess.run` raises FileNotFoundError.
        `write_host` refuses, naming the absence."""
        with self._fab(raise_=FileNotFoundError("no fab")):
            ok, detail = rn.write_host("node-1")
        self.assertFalse(ok)
        self.assertIn("not on PATH", detail)

    def test_fab_timeout_refuses(self):
        """(c) `fab nodes --json` times out: `TimeoutExpired`. Refused, naming
        the timeout."""
        with self._fab(raise_=subprocess.TimeoutExpired(
                ["fab", "nodes", "--json"], 15)):
            ok, detail = rn.write_host("node-1")
        self.assertFalse(ok)
        self.assertIn("timed out", detail)

    def test_fab_nonzero_exit_refuses(self):
        """(c) `fab nodes --json` exits non-zero: refused, naming the exit
        and its tail."""
        with self._fab(stdout=b"nope\n", returncode=2):
            ok, detail = rn.write_host("node-1")
        self.assertFalse(ok)
        self.assertIn("failed", detail)
        self.assertIn("exit 2", detail)

    def test_fab_unparseable_refuses(self):
        """(c) `fab nodes --json` answers but is not JSON: refused, naming the
        parse failure."""
        with self._fab(stdout=b"{ not json at all\n"):
            ok, detail = rn.write_host("node-1")
        self.assertFalse(ok)
        self.assertIn("unparseable", detail)

    def test_fab_empty_node_list_refuses(self):
        """(c) `fab nodes --json` answers with no node list: nothing is a
        valid host, so every host is refused, naming that."""
        with self._fab(stdout=b'{"nodes": []}'):
            ok, detail = rn.write_host("node-1")
        self.assertFalse(ok)
        self.assertIn("not in the build fleet", detail)

    def test_the_fleet_query_outwaits_a_live_probe(self):
        """`fab nodes --json` probes every node before it answers: 33.9 s on
        the hub that runs the nightly timer (measured). A timeout
        under that makes every night a MISSING night, so the call waits
        longer."""
        seen = {}

        def run(*a, **kw):
            seen["timeout"] = kw.get("timeout")
            return mock.Mock(returncode=0, stderr=b"",
                             stdout=b'{"nodes": [{"host": "node-1"}]}')
        with mock.patch.object(rn.subprocess, "run", side_effect=run):
            ok, detail = rn.write_host("node-1")
        self.assertTrue(ok, detail)
        self.assertGreater(seen["timeout"], 34)

    def test_an_answer_that_is_not_an_object_refuses(self):
        """An answer that is JSON but not an object (a list) is not fab's
        shape: refused as unparseable, never a traceback out of the nightly."""
        with self._fab(stdout=b'[{"host": "node-1"}]'):
            ok, detail = rn.write_host("node-1")
        self.assertFalse(ok)
        self.assertIn("unparseable", detail)


class SourceAttributionTest(unittest.TestCase):
    """The CLI decides timer vs. manual from the HELM_RELEASE_NIGHTLY_SOURCE marker, not from
    `--timer` alone: hand-typed `--timer` runs must never claim the timer's streak. Each test
    drives the `run` subcommand and reads the source it hands to run()."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-nightly-source-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = dict(os.environ, GIT_AUTHOR_NAME="t",
                        GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="t",
                        GIT_COMMITTER_EMAIL="t@example.invalid")
        for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            self.env.pop(k, None)
        self.src = os.path.join(self.tmp, "src")
        os.makedirs(self.src)
        _git(self.src, "init", "-q", "-b", "main", "--template=")
        self.commit("the first trunk")
        self.hub = os.path.join(self.tmp, "hub")
        _git(self.tmp, "clone", "-q", self.src, self.hub)
        _git(self.hub, "remote", "add", "aspublic",
             "git@github.com:example-owner/helm.git")
        self.home = os.path.join(self.tmp, "global")

    def commit(self, text):
        _write(os.path.join(self.src, "file.txt"), text + "\n")
        _git(self.src, "add", "-A")
        _git(self.src, "commit", "-q", "-m", text, env=self.env)
        return _git(self.src, "rev-parse", "HEAD")

    def _cli_source(self, flags, env):
        """The source `helm release nightly run --repo HUB <flags>` hands to run(), with the
        process environment holding exactly `env` for the marker: the CLI path decides it, so a
        regression there is caught. The marker is removed when `env` lacks it, even if the
        runner of this test has it set (a run under the installed unit would)."""
        seen = {}

        def fake_run(repo=None, source=None, **_kw):
            seen["source"] = source
            return {"verdict": rn.GREEN, "step": None, "reason": "ok", "trunk": None,
                    "host": None, "wall_s": 0, "log": None, "recorded": True}
        with mock.patch.dict(os.environ, env), mock.patch.object(rn, "run", fake_run), \
                contextlib.redirect_stdout(io.StringIO()):
            if "HELM_RELEASE_NIGHTLY_SOURCE" not in env:
                os.environ.pop("HELM_RELEASE_NIGHTLY_SOURCE", None)
            rn._cmd_nightly(["run", "--repo", self.hub] + flags)
        return seen["source"]

    def test_hand_timer_without_marker_is_manual(self):
        """(a) `--timer` with no marker is manual. RED at trunk, where `--timer` alone was the
        timer's."""
        self.assertEqual(self._cli_source(["--timer"], {}), rn.MANUAL)

    def test_timer_marker_with_flag_is_timer(self):
        """(b) `--timer` plus HELM_RELEASE_NIGHTLY_SOURCE=timer is the timer's."""
        self.assertEqual(self._cli_source(["--timer"], {"HELM_RELEASE_NIGHTLY_SOURCE": "timer"}),
                         rn.TIMER)

    def test_no_flag_with_marker_still_manual(self):
        """(c) the marker without `--timer` stays manual: a person's bare `run`."""
        self.assertEqual(self._cli_source([], {"HELM_RELEASE_NIGHTLY_SOURCE": "timer"}), rn.MANUAL)

    def test_no_flag_no_marker_manual(self):
        """(d) neither: manual, the default."""
        self.assertEqual(self._cli_source([], {}), rn.MANUAL)


class EndToEndTest(ReleaseFixture):
    """The whole path once for real: the hub fetches the fixture trunk and
    opens a peek room, and the planted launcher runs the REAL release tool in
    it the way the fab host does. The verdict recorded is the tool's own."""

    def setUp(self):
        super().setUp()
        self._fab = _patch_fab_nodes(["node-1", "node-0"])
        self._fab.start()
        self.addCleanup(self._fab.stop)
        self.hub = os.path.join(self.tmp, "hub")
        _git(self.tmp, "clone", "-q", self.src, self.hub)
        _git(self.hub, "remote", "add", "aspublic", self.public)
        self.global_dir = os.path.join(self.tmp, "global")

    def nightly(self):
        def runner(argv, log, env):
            cmd = argv[argv.index("--") + 1:]
            room = argv[argv.index("--repo") + 1]
            self.assertEqual(cmd[:3], ["python3", "scripts/release/release.py",
                                       "--nightly"])
            # The fab host runs the room's own copy of the tool; the fixture's
            # tree carries none, so this checkout's tool runs with the room as
            # its source. The public repository is a directory here, so its
            # GitHub name is given.
            _argv, fixture_env, work = self.command()
            full = [sys.executable, TOOL] + cmd[2:] + [
                "--source", room, "--gh-repo", "%s/helm" % OWNER, "--work", work]
            with open(log, "a", encoding="utf-8") as f:
                return subprocess.run(full, stdout=f, stderr=subprocess.STDOUT,
                                      env=fixture_env, timeout=300).returncode
        return rn.run(repo=self.hub, global_dir=self.global_dir, runner=runner,
                      host="node-1", source=rn.TIMER)

    def test_a_green_night_then_a_red_one_naming_its_step(self):  # noqa: VACUOUS_ASSERTION — nothing written is the contract; the GREEN row, its candidate sha and the RED row naming its step pin that both runs happened
        row = self.nightly()
        with open(row["log"], encoding="utf-8") as f:
            log = f.read()
        self.assertEqual(row["verdict"], rn.GREEN, log)
        self.assertEqual(row["trunk"], _git(self.src, "rev-parse", "HEAD"))
        self.assertRegex(row["candidate"], r"^[0-9a-f]{40}$")
        self.assertNothingWritten(log)
        red = self.plant_attribution()
        row = self.nightly()
        with open(row["log"], encoding="utf-8") as f:
            log = f.read()
        self.assertEqual((row["verdict"], row["step"], row["trunk"]),
                         (rn.RED, "gate/attribution", red), log)
        self.assertNothingWritten(log)
        self.assertEqual([(r["verdict"], r["step"]) for r in _rows(self.global_dir)],
                         [(rn.GREEN, None), (rn.RED, "gate/attribution")])


if __name__ == "__main__":
    unittest.main()
