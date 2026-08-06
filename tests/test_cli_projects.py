#!/usr/bin/env python3
"""`helm projects` — one malformed row must not blind the whole listing.

The read pulled p["name"] as a bare subscript sitting in a row of .get()s, so a
single registry entry missing that field raised KeyError and took out the entire
project list — every healthy project on the machine unreadable because of one
bad neighbour. A traceback is not a loud failure, it is an unhandled one.

The recovery is not a guess: a project is FILED UNDER its name in the registry
dict, so the key is the MORE authoritative source and the copy inside the value
is the weaker, redundant one. Rows that are not dicts at all cannot be recovered
that way, so they are counted and reported rather than silently skipped — a
listing that quietly drops rows reads exactly like a listing with nothing to
drop.
"""
import io
import contextlib
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import cli  # noqa: E402


def run(reg):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(cli.registry, "load", return_value=reg), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.cmd_projects([])
    return rc, out.getvalue(), err.getvalue()


GOOD = {"name": "helm", "status": "active", "last_seen": 100,
        "sessions": {"claude": 3}, "path": "/dev/helm"}


class ProjectsTest(unittest.TestCase):
    def test_a_row_missing_its_name_does_not_kill_the_listing(self):
        """The reported defect: KeyError: 'name' on a corrupted registry."""
        nameless = {k: v for k, v in GOOD.items() if k != "name"}
        nameless["path"] = "/dev/other"
        rc, out, err = run({"projects": {"helm": GOOD, "recovered": nameless}})
        self.assertEqual(rc, 0, err)
        self.assertIn("helm", out)
        self.assertIn("recovered", out,
                      "the registry KEY is the authoritative name — use it")

    def test_the_key_is_preferred_over_nothing_but_never_over_a_real_name(self):
        """Negative control. Recovering from the key must not START rewriting
        names that are already there, or the fix quietly renames every row."""
        rc, out, err = run({"projects": {"filed-under-this": GOOD}})
        self.assertEqual(rc, 0, err)
        self.assertIn("helm", out, "an existing name must win over the key")
        self.assertNotIn("filed-under-this", out)

    def test_an_unusable_row_is_counted_and_reported_never_silently_dropped(self):
        """A cap that skips work must never produce a confident answer: a
        listing that silently drops rows is indistinguishable from one with
        nothing to drop."""
        rc, out, err = run({"projects": {"broken": "not-a-dict"}})
        self.assertEqual(rc, 1)
        self.assertIn("unreadable", err)
        self.assertIn("1", err)

    def test_an_empty_registry_still_says_run_sync(self):
        rc, out, err = run({"projects": {}})
        self.assertEqual(rc, 0)
        self.assertIn("helm sync", out)

    def test_a_missing_projects_dict_is_not_a_crash(self):
        rc, out, err = run({})
        self.assertEqual(rc, 0)
        self.assertIn("helm sync", out)


if __name__ == "__main__":
    unittest.main()
