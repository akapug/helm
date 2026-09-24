#!/usr/bin/env python3
"""helm fixedtext: what a checkout hands a seat before its first turn."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-fixedtext-", var="HELM_HOME")

from helm import cli, doctor, fixedtext, seats_runtime  # noqa: E402
# `seat` IS IMPORTED EXPLICITLY because the co-occurrence guard in
# tests/test_seat_facade_injection.py requires an accepted facade import
# beside any impl import. It asserts nothing about import order.
from helm import seat  # noqa: E402,F401


def _row(cwd, session, env=None):
    """A roster row in the shape a join writes. This module reads `cwd` and
    the recorded harness, and the runtime comes from the production
    translator so the day it learns a field this fixture learns it too."""
    runtime = seats_runtime._runtime_environment(
        env or {"CLAUDE_CODE_SESSION_ID": session})
    return {"session": session, "sessions": [session], "cwd": cwd,
            "runtime": runtime}


def _codex_harness(session):
    return {"HELM_AGENT_HARNESS": "codex", "CODEX_SESSION_ID": session}


class _Estate(unittest.TestCase):
    """A home directory holding the harness's own folder, and one checkout
    two levels beneath it."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="helm-test-fixedtext-case-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = os.path.join(self.tmp, ".claude")
        self.checkout = os.path.join(self.tmp, "dev", "proj")
        os.makedirs(self.checkout)

    def write(self, path, size):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("x" * size)
        return path

    def measure(self):
        return fixedtext.measure(self.checkout, self.config)


class MeasureTest(_Estate):

    def test_every_door_a_checkout_loads_through_is_sized(self):
        personal = self.write(os.path.join(self.config, "CLAUDE.md"), 100)
        outer = self.write(os.path.join(self.tmp, "dev", ".claude", "CLAUDE.md"), 20)
        own = self.write(os.path.join(self.checkout, "CLAUDE.md"), 3)
        local = self.write(os.path.join(self.checkout, "CLAUDE.local.md"), 4)
        index = self.write(fixedtext.memory_index(self.checkout, self.config), 1000)
        got = self.measure()
        self.assertEqual(got["files"], [(personal, 100), (outer, 20), (own, 3),
                                        (local, 4), (index, 1000)])
        self.assertEqual((got["total"], got["unreadable"]), (1127, []))

    def test_a_file_that_does_not_exist_is_not_listed(self):
        """CONTROL: the one file that does exist is, so the emptiness of the
        rest is absence and not a reader that lists nothing."""
        own = self.write(os.path.join(self.checkout, "CLAUDE.md"), 7)
        self.assertEqual(self.measure()["files"], [(own, 7)])

    def test_the_harness_override_names_the_config_home(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/elsewhere"}):
            self.assertEqual(fixedtext.config_dir(), "/elsewhere")
        with mock.patch.dict(os.environ):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            self.assertTrue(fixedtext.config_dir().endswith(os.sep + ".claude"))

    def test_the_personal_file_is_counted_ONCE(self):  # noqa: VACUOUS_ASSERTION — the assertion is an equality against a NON-EMPTY list naming the file and its size, so a reader that listed nothing reddens it
        """The chain walks through the home directory and finds the personal
        file a second time. One file is loaded once."""
        personal = self.write(os.path.join(self.config, "CLAUDE.md"), 100)
        twice = [p for p in (os.path.join(self.config, "CLAUDE.md"),
                             os.path.join(self.tmp, ".claude", "CLAUDE.md"))]
        self.assertEqual(twice[0], twice[1], "control: the two doors are one file")
        self.assertEqual(self.measure()["files"], [(personal, 100)])

    def test_the_memory_index_is_where_the_harness_keeps_it(self):
        """The harness's own folding, read off a project directory it made:
        every character outside the alphanumerics becomes a dash, a dot
        included."""
        self.assertEqual(
            fixedtext.memory_index("/srv/w/proj/.claude/worktrees/lane_1", "/c"),
            "/c/projects/-srv-w-proj--claude-worktrees-lane-1/memory/MEMORY.md")

    def test_UNREADABLE_is_not_absent(self):  # noqa: VACUOUS_ASSERTION — the first assertion pins a non-empty files list AND a non-empty unreadable list; the empty one is the stated control
        own = self.write(os.path.join(self.checkout, "CLAUDE.md"), 7)
        index = self.write(fixedtext.memory_index(self.checkout, self.config), 50)
        real = os.stat

        def stat(path, *a, **k):
            if path == index:
                raise PermissionError(13, "denied", path)
            return real(path, *a, **k)
        with mock.patch.object(fixedtext.os, "stat", side_effect=stat):
            got = self.measure()
        self.assertEqual((got["files"], got["unreadable"], got["total"]),
                         ([(own, 7)], [index], 7))
        # CONTROL: the same estate with nothing refusing is whole.
        self.assertEqual(self.measure()["unreadable"], [])


class HarnessTest(_Estate):
    """A file is fixed text for a checkout only when a seat homed there runs
    the harness that loads it."""

    def estate(self):
        own = self.write(os.path.join(self.checkout, "CLAUDE.md"), 10)
        agents = self.write(os.path.join(self.checkout, "AGENTS.md"), 9000)
        return own, agents

    def survey(self, rows):
        with mock.patch.object(fixedtext, "codex_dir",
                               return_value=os.path.join(self.tmp, ".codex")):
            return fixedtext.survey(rows, self.config)[0]

    def test_the_fixture_rows_are_what_the_translator_records(self):
        self.assertEqual(fixedtext.harness_of(_row(self.checkout, "s1")),
                         fixedtext.DEFAULT_HARNESS)
        self.assertEqual(fixedtext.harness_of(
            _row(self.checkout, "s2", _codex_harness("s2"))), "codex")

    def test_a_codex_MODEL_in_the_claude_harness_is_not_handed_the_codex_file(self):
        """Every seat here runs the claude harness, whatever model answers
        behind it. The codex file is SHOWN and is not counted."""
        own, agents = self.estate()
        got = self.survey({"seat-a": _row(self.checkout, "s1")})
        self.assertEqual((got["files"], got["total"]), ([(own, 10)], 10))
        self.assertEqual(got["unloaded"], [(agents, 9000)])

    def test_a_seat_in_the_codex_harness_makes_that_file_fixed_text(self):
        own, agents = self.estate()
        got = self.survey({"seat-a": _row(self.checkout, "s1"),
                           "seat-x": _row(self.checkout, "s2", _codex_harness("s2"))})
        self.assertEqual(sorted(got["files"]), sorted([(own, 10), (agents, 9000)]))
        self.assertEqual((got["total"], got["unloaded"]), (9010, []))

    def test_an_unrecorded_or_unknown_harness_is_read_as_the_default(self):
        """Never as a harness that loads nothing: that would print a zero
        for a seat that is handed the default files."""
        own, _agents = self.estate()
        for row in ({"cwd": self.checkout},
                    {"cwd": self.checkout, "runtime": {"agent_harness": "pi"}},
                    {"cwd": self.checkout, "runtime": "not a mapping"}):
            with self.subTest(row=row):
                self.assertEqual(self.survey({"seat-a": row})["files"], [(own, 10)])

    def test_the_report_shows_the_uncounted_file_and_says_why(self):
        _own, agents = self.estate()
        out = io.StringIO()
        with mock.patch.object(fixedtext, "live_rows", return_value=(
                {"seat-a": _row(self.checkout, "s1")}, False)), \
                mock.patch.object(fixedtext, "config_dir", return_value=self.config), \
                mock.patch.object(fixedtext, "codex_dir",
                                  return_value=os.path.join(self.tmp, ".codex")), \
                contextlib.redirect_stdout(out):
            self.assertEqual(fixedtext.cmd([]), 0)
        line = [l for l in out.getvalue().splitlines() if agents in l]
        self.assertEqual(len(line), 1, out.getvalue())
        self.assertIn("NOT COUNTED", line[0])


class SurveyTest(_Estate):

    def rows(self):
        other = os.path.join(self.tmp, "dev", "other")
        os.makedirs(other)
        self.write(os.path.join(self.checkout, "CLAUDE.md"), 10)
        self.write(os.path.join(other, "CLAUDE.md"), 900)
        return other, {"seat-b": _row(self.checkout, "s2"),
                       "seat-a": _row(self.checkout, "s1"),
                       "seat-c": _row(other, "s3"),
                       "seat-homeless": {"session": "s4", "sessions": ["s4"]},
                       "seat-broken": "not a row"}

    def test_the_unit_is_the_checkout_and_every_seat_in_it_is_named(self):
        other, rows = self.rows()
        self.assertEqual(fixedtext.checkouts(rows),
                         {self.checkout: ["seat-a", "seat-b"], other: ["seat-c"]})

    def test_a_home_that_is_not_printable_text_is_never_rendered(self):  # noqa: VACUOUS_ASSERTION — the equality above it pins the two printable checkouts as PRESENT in the same return value
        other, rows = self.rows()
        rows["seat-esc"] = _row(self.checkout + "\x1b[2J", "s9")
        self.assertEqual(sorted(fixedtext.checkouts(rows)),
                         sorted([self.checkout, other]))
        self.assertNotIn("seat-esc", sum(fixedtext.checkouts(rows).values(), []))

    def test_the_heaviest_checkout_is_first(self):
        other, rows = self.rows()
        got = fixedtext.survey(rows, self.config)
        self.assertEqual([(r["checkout"], r["total"], r["seats"]) for r in got],
                         [(other, 900, ["seat-c"]),
                          (self.checkout, 10, ["seat-a", "seat-b"])])

    def test_the_doctor_rung_names_the_heaviest_and_who_shares_it(self):
        other, rows = self.rows()
        level, line = fixedtext.findings(rows, config=self.config)[0]
        self.assertEqual(level, doctor.OK)
        self.assertIn(other, line)
        self.assertIn("seat-c", line)
        _other, rows = other, dict(rows, **{"seat-d": _row(other, "s5")})
        self.assertIn("seat-c, seat-d",
                      fixedtext.findings(rows, config=self.config)[0][1])

    def test_a_partial_row_WARNS_and_says_so(self):
        other, rows = self.rows()
        heavy = os.path.join(other, "CLAUDE.md")
        real = os.stat

        def stat(path, *a, **k):
            if path == heavy:
                raise PermissionError(13, "denied", path)
            return real(path, *a, **k)
        self.write(os.path.join(other, "CLAUDE.local.md"), 5000)
        with mock.patch.object(fixedtext.os, "stat", side_effect=stat):
            level, line = fixedtext.findings(rows, config=self.config)[0]
        self.assertEqual(level, doctor.WARN)
        self.assertIn("PARTIAL", line)

    def test_an_unreadable_roster_is_UNMEASURED_never_no_seats(self):
        level, line = fixedtext.findings({}, failed=True)[0]
        self.assertEqual(level, doctor.WARN)
        self.assertIn("UNMEASURED", line)
        # CONTROL: a roster that WAS read and is empty says so, at OK.
        self.assertEqual(fixedtext.findings({}, failed=False)[0][0], doctor.OK)

    def test_the_doctor_check_survives_a_survey_that_raises(self):
        def boom():
            raise RuntimeError("the survey broke")
        level, line = doctor.check_fixed_text(survey=boom)[0]
        self.assertEqual(level, doctor.WARN)
        self.assertIn("UNMEASURED", line)
        self.assertIn("check_fixed_text", doctor.CHECKS)


class VerbTest(_Estate):

    def run_verb(self, argv, rows=None, failed=False):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(fixedtext, "live_rows", return_value=(rows or {}, failed)), \
                mock.patch.object(fixedtext, "config_dir", return_value=self.config), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["fixedtext"] + argv)
        return rc, out.getvalue(), err.getvalue()

    def test_the_report_lists_each_file_under_its_checkout(self):
        own = self.write(os.path.join(self.checkout, "CLAUDE.md"), 2048)
        rc, out, _err = self.run_verb([], {"seat-a": _row(self.checkout, "s1")})
        self.assertEqual(rc, 0)
        self.assertIn("%s loads 2.0 KB" % self.checkout, out)
        self.assertIn("shared by seat-a", out)
        self.assertIn(own, out)

    def test_json_carries_the_same_rows(self):
        self.write(os.path.join(self.checkout, "CLAUDE.md"), 5)
        rc, out, _err = self.run_verb(["--json"], {"seat-a": _row(self.checkout, "s1")})
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertEqual([(r["checkout"], r["total"], r["seats"]) for r in doc],
                         [(self.checkout, 5, ["seat-a"])])

    def test_an_unknown_flag_is_refused(self):
        rc, _out, err = self.run_verb(["--prune"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown arg '--prune'", err)

    def test_an_unreadable_roster_prints_UNREADABLE_and_fails(self):
        rc, out, err = self.run_verb([], failed=True)
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("UNREADABLE", err)


if __name__ == "__main__":
    unittest.main()
