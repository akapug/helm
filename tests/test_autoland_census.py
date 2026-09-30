#!/usr/bin/env python3
"""Auto-land's pre-gate audits run the lane census, never a swallowed plan.

`autoland.Ops.audits` runs the tree-wide audits, the integrator's four and
the lane census of the composed diff (`gateaudits.lane_census`) through fab
on the composed room. Every arm here drives the REAL `Ops.audits` against a
fixture room and replaces only fab (`gatewindow._fab`), so an arm reads the
exact argv auto-land would have run and the log it wrote.

THE PIN. The census replaced a `gate.focus_plan` call that took 60-140 s on
every train and whose refusal a bare `except` swallowed: in 30 audit logs it
refused every time and the train ran a shorter list without saying so. So
`focus_plan` is never reached here, and a census that cannot be read refuses
the run with its reason instead of running less (task/1090).
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-autoland-census-", var="HELM_HOME")

from helm import autoland, cli, gate, gateaudits, gatewindow  # noqa: E402

_PASSING = ("import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_t(self):\n"
            "        self.assertTrue(True)\n")

ROOM = {
    "tests/__init__.py": "",
    "helm/__init__.py": "",
    "helm/harness.py": "X = 1\n",
    "docs/NOTES.md": "notes\n",
    "tests/test_harness.py": _PASSING,
    "tests/test_resumeturn.py": _PASSING + "P = 'helm/harness.py'\n",
    "tests/test_web_lr.py": _PASSING + "class CardRuntimeBase(object):\n"
                                       "    pass\n",
    "tests/test_seat_lineage_board.py": _PASSING + (
        "def card():\n"
        "    from tests.test_web_lr import CardRuntimeBase\n"
        "    return CardRuntimeBase\n"),
    "tests/test_quiet.py": _PASSING,
}
ROOM.update(("tests/%s.py" % n, _PASSING) for n in
            gateaudits.AUDITS + autoland.PRE_GATE_AUDITS)
LANE = {"helm/harness.py": "X = 2\n",
        "tests/test_web_lr.py": ROOM["tests/test_web_lr.py"] + "# more\n"}

GREEN = (0, "", "Ran 40 tests in 1.000s\n\nOK\n")


def _git(cwd, *args):
    proc = subprocess.run(("git",) + args, cwd=cwd, capture_output=True,
                          text=True)
    if proc.returncode != 0:
        raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


class _Room(unittest.TestCase):

    def room(self, change=LANE):
        """A composed room: trunk is ROOM, HEAD adds `change`. -> (room,
        trunk sha)."""
        room = os.path.realpath(tempfile.mkdtemp(prefix="helm-census-room-"))
        self.addCleanup(shutil.rmtree, room, ignore_errors=True)
        _git(room, "init", "-q", "-b", "main")
        _git(room, "config", "user.name", "t")
        _git(room, "config", "user.email", "t@example.invalid")
        heads = []
        for files, message in ((ROOM, "trunk"), (change, "train1: merge")):
            for rel, text in files.items():
                path = os.path.join(room, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
            _git(room, "add", "-A")
            _git(room, "commit", "-q", "--allow-empty", "-m", message)
            heads.append(_git(room, "rev-parse", "HEAD"))
        return room, heads[0]

    def audits(self, room, trunk, answer=GREEN):
        """(ok, detail, log, the argv fab was handed or None)."""
        seen = []

        def fab(argv, timeout=None, env=None):
            seen.append(list(argv))
            return answer

        with mock.patch.object(gatewindow, "_fab", side_effect=fab):
            ok, detail, log = autoland.Ops().audits(room, room, trunk)
        self.assertLessEqual(len(seen), 1, seen)
        return ok, detail, log, (seen[0] if seen else None)

    def ran(self, argv):
        """The module list of a serial fab line's argv."""
        return argv[argv.index("unittest") + 1:]


class TheListItRunsIsTheWideCensus(_Room):

    def test_parity_with_helm_gate_audits_wide_in_order(self):  # noqa: VACUOUS_ASSERTION — `why` is None only beside an exact, non-empty list equality on the modules the same call returned
        room, trunk = self.room()
        ok, detail, _log, argv = self.audits(room, trunk)
        self.assertTrue(ok, detail)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["gate", "audits", "--wide", "--json", "--repo",
                           room, "--base", trunk, "--tip", "HEAD"])
        self.assertEqual(rc, 0, err.getvalue())
        wide = json.loads(out.getvalue())["modules"]
        self.assertEqual(self.ran(argv), wide)
        modules, _reasons, why = gateaudits.lane_modules(room, trunk, "HEAD")
        self.assertIsNone(why, why)
        self.assertEqual(self.ran(argv), modules)
        # The census part is there, after the audits that lead it, and a
        # module no rule reaches is not.
        lead = gateaudits.modules(autoland.PRE_GATE_AUDITS)
        self.assertEqual(self.ran(argv), lead + [
            "tests.test_harness", "tests.test_resumeturn",
            "tests.test_seat_lineage_board", "tests.test_web_lr"])
        self.assertIn("tests.test_harness", self.ran(argv))
        self.assertNotIn("tests.test_quiet", self.ran(argv))

    def test_focus_plan_is_never_reached(self):
        room, trunk = self.room()
        plan = mock.Mock(side_effect=AssertionError("focus_plan was asked"))
        explained = mock.Mock(side_effect=AssertionError("asked"))
        with mock.patch.object(gate, "focus_plan", plan), \
                mock.patch.object(gate, "focus_plan_explained", explained):
            ok, detail, _log, argv = self.audits(room, trunk)
        self.assertTrue(ok, detail)
        self.assertIsNotNone(argv)
        self.assertEqual((plan.call_count, explained.call_count), (0, 0))

    def test_the_log_names_each_census_module_and_its_rule(self):
        room, trunk = self.room()
        ok, detail, log, argv = self.audits(room, trunk)
        self.assertTrue(ok, detail)
        with open(log, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        census, why = gateaudits.lane_census(room, trunk, "HEAD")
        self.assertIsNone(why, why)
        extra = census["extra"]
        self.assertTrue(extra)
        self.assertTrue(lines[0].startswith(
            "census: %d extra from 2 touched paths (" % len(extra)),
            lines[0])
        self.assertEqual(lines[1:1 + len(extra)],
                         ["%s: %s" % (m, census["reasons"][m])
                          for m in extra])
        self.assertIn("tests.test_resumeturn: names helm/harness.py", lines)
        self.assertIn("tests.test_seat_lineage_board: imports "
                      "tests.test_web_lr", lines)
        self.assertTrue(lines[1 + len(extra)].startswith("$ fab test "),
                        lines[1 + len(extra)])
        # No audit carries a reason line: the audits are the list's lead.
        self.assertNotIn("tests.test_wiring: audit", lines)

    def test_the_detail_counts_audits_and_census_and_times_the_census(self):
        room, trunk = self.room()
        ok, detail, _log, argv = self.audits(room, trunk)
        self.assertTrue(ok, detail)
        census, _why = gateaudits.lane_census(room, trunk, "HEAD")
        total, extra = len(self.ran(argv)), len(census["extra"])
        self.assertTrue(detail.startswith(
            "%d modules (%d audits + %d census), census "
            % (total, total - extra, extra)), detail)
        self.assertRegex(detail, r"census \d+\.\d s; serial audits, fab exit "
                                 r"0: Ran 40 tests in 1\.000s; OK$")

    def test_a_change_nobody_names_runs_the_audits_alone_and_says_so(self):
        room, trunk = self.room(change={"docs/NOTES.md": "more\n"})
        ok, detail, log, argv = self.audits(room, trunk)
        self.assertTrue(ok, detail)
        self.assertEqual(self.ran(argv),
                         gateaudits.modules(autoland.PRE_GATE_AUDITS))
        with open(log, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertTrue(lines[0].startswith(
            "census: 0 extra from 1 touched paths ("), lines[0])
        self.assertTrue(lines[1].startswith("$ fab test "), lines[1])
        self.assertIn("(%d audits + 0 census)" % len(self.ran(argv)), detail)


class AFailureNamesTheModuleAndTheRuleThatChoseIt(_Room):

    def test_a_red_census_module_is_named_with_its_reason(self):
        room, trunk = self.room()
        red = (1, "", "\n".join((
            "FAIL: test_t (tests.test_resumeturn.T.test_t)",
            "ERROR: setUpClass (tests.test_wiring.T)",
            "Ran 40 tests in 1.000s", "", "FAILED (failures=1, errors=1)")))
        ok, detail, _log, _argv = self.audits(room, trunk, answer=red)
        self.assertFalse(ok)
        self.assertIn("; failing: tests.test_resumeturn (names "
                      "helm/harness.py), tests.test_wiring (audit)", detail)

    def test_a_green_run_names_no_failure(self):
        room, trunk = self.room()
        ok, detail, _log, _argv = self.audits(room, trunk)
        self.assertTrue(ok, detail)
        self.assertNotIn("failing", detail)


class ACensusThatCannotBeReadHoldsTheTrain(_Room):

    def test_a_symlink_refusal_is_ok_none_with_its_reason_and_no_run(self):
        room, trunk = self.room()
        os.symlink("test_web_lr.py", os.path.join(room, "tests",
                                                  "test_alias.py"))
        _git(room, "add", "-A")
        _git(room, "commit", "-qm", "alias")
        ok, detail, log, argv = self.audits(room, trunk)
        self.assertIsNone(ok)
        self.assertIsNone(argv, "a shorter list ran in the census's place")
        self.assertIsNone(log)
        self.assertIn("the lane census of the composed room cannot be read",
                      detail)
        self.assertIn("tests/test_alias.py is a symlink", detail)

    def test_a_census_that_raises_is_ok_none_and_named(self):
        room, trunk = self.room()
        with mock.patch.object(gateaudits, "lane_census",
                               side_effect=RuntimeError("tree unreadable")):
            ok, detail, _log, argv = self.audits(room, trunk)
        self.assertIsNone(ok)
        self.assertIsNone(argv)
        self.assertIn("RuntimeError: tree unreadable", detail)
        # CONTROL: the same room, census unpatched, runs.
        ok, detail, _log, argv = self.audits(room, trunk)
        self.assertTrue(ok, detail)
        self.assertIsNotNone(argv)

    def test_a_listed_audit_the_room_lacks_is_todays_refusal(self):
        room, trunk = self.room()
        os.remove(os.path.join(room, "tests", "test_wiring.py"))
        ok, detail, _log, argv = self.audits(room, trunk)
        self.assertIsNone(ok)
        self.assertIsNone(argv)
        self.assertEqual(detail, "the audit list names tests the room does "
                                 "not carry: test_wiring")


if __name__ == "__main__":
    unittest.main()
