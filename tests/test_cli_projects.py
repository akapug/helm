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
import os
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

# `registry` IS IMPORTED DIRECTLY, not reached through `cli`. It is the same
# module object either way — but `cli` imports it inside the verbs that read
# it now (45 ms of CPU that every per-tool-call hook was paying for a listing
# no hook runs), so `cli.registry` no longer exists as an attribute and the
# old spelling raised AttributeError in all five arms here. Patching the
# module is what these arms always meant; going through `cli` was only how
# they spelled it.
from helm import cli, home, pk, registry, web  # noqa: E402


def run(reg):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(registry, "load", return_value=reg), \
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



class ProjectLightVerbTest(unittest.TestCase):
    """`helm projects state` and the listing column that reads it — through a
    REAL home with both layers on disk, because the thing under test is which
    layer the answer came from and a mocked `load` cannot hold that question.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="helm-cli-light-")
        self.addCleanup(tmp.cleanup)
        prior = {k: os.environ.get(k) for k in ("HELM_HOME", "HELM_CHAT_NAME")}
        os.environ["HELM_HOME"] = os.path.join(tmp.name, "helm-home")
        os.environ["HELM_CHAT_NAME"] = "the-setter"
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None
                                 else os.environ.__setitem__(k, v)
                                 for k, v in prior.items()])
        self.assertTrue(home.helm_home().startswith(tmp.name))
        self.path = os.path.join(tmp.name, "dev", "alpha")
        os.makedirs(self.path)
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "alpha": {"name": "alpha", "path": self.path, "status": "active",
                      "last_seen": 100, "sessions": {"harness-a": 1}},
            "beta": {"name": "beta", "path": self.path + "-b", "status": "dormant",
                     "last_seen": 90, "sessions": {}}}})

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.cmd_projects(list(args))
        return rc, out.getvalue(), err.getvalue()

    def column(self, name):
        rc, out, err = self.call()
        self.assertEqual(rc, 0, err)
        return next(line.split()[1] for line in out.splitlines()
                    if line.split()[:1] == [name])

    def test_the_listing_marks_an_authored_light_and_only_that_one(self):
        self.assertEqual(self.column("alpha"), "active")          # the control
        rc, out, err = self.call("state", "alpha", "yellow",
                                 "--reason", "maintenance only", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("applied alpha (the scan's) -> yellow", out)
        self.assertEqual(self.column("alpha"), "yellow*")
        self.assertEqual(self.column("beta"), "dormant")
        self.assertIn("AUTHORED light", self.call()[1])

    def test_the_read_lists_who_decided_what_and_why(self):  # noqa: VACUOUS_ASSERTION — the empty read is asserted FIRST as the control, then the same read must carry the setter, colour and reason; the absent row sits beside that present one
        rc, out, err = self.call("state")
        self.assertEqual(rc, 0, err)
        self.assertIn("no project light is authored", out)         # the control
        self.call("state", "alpha", "red", "--reason", "frozen for the audit",
                  "--apply")
        rc, out, err = self.call("state")
        row = next(line for line in out.splitlines() if line.startswith("alpha"))
        for needle in ("red", "the-setter", "frozen for the audit"):
            self.assertIn(needle, row)
        self.assertNotIn("beta", out)

    def test_a_colour_without_a_reason_is_refused_and_writes_nothing(self):
        rc, out, err = self.call("state", "alpha", "red", "--apply")
        self.assertEqual(rc, 2)
        self.assertIn("--reason", err)
        self.assertEqual(self.column("alpha"), "active")
        # `clear` needs none: handing the light back decides nothing.
        self.call("state", "alpha", "red", "--reason", "r", "--apply")
        self.assertEqual(self.column("alpha"), "red*")
        rc, out, err = self.call("state", "alpha", "clear", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.column("alpha"), "active")

    def test_a_dry_run_says_so_and_leaves_the_light_alone(self):
        rc, out, err = self.call("state", "alpha", "orange", "--reason", "r")
        self.assertEqual(rc, 0, err)
        self.assertIn("dry-run", out)
        self.assertIn("--apply", out)
        self.assertEqual(self.column("alpha"), "active")

    def test_a_bad_colour_and_an_unknown_project_fail_loudly(self):  # noqa: VACUOUS_ASSERTION — the loop walks a literal three-tuple, so every arm runs, and each asserts a non-zero rc AND its own sentence
        for args, needle in ((("state", "alpha", "purple", "--reason", "r"),
                              "colour must be one of"),
                             (("state", "nobody", "red", "--reason", "r"),
                              "unknown project"),
                             (("state", "alpha"), "usage:")):
            with self.subTest(args=args):
                rc, out, err = self.call(*args)
                self.assertNotEqual(rc, 0)
                self.assertIn(needle, err)


    # ---- the owner's door: the row on the web burn board -----------------

    def wire(self, name="alpha"):
        return web._api_registry()["projects"][name]["light"]

    def test_the_page_is_handed_the_resolved_light_with_its_key(self):  # noqa: VACUOUS_ASSERTION — one unconditional equality on colour, authorship and key of a planted project
        self.assertEqual((self.wire()["colour"], self.wire()["authored"],
                          self.wire()["key"]), ("active", False, "alpha"))

    def test_a_click_sets_the_light_through_the_same_writer_as_the_verb(self):  # noqa: VACUOUS_ASSERTION — the set is asserted positively on the wire AND in the CLI column before the clear is read as absent
        body, status = web._api_projects_state(
            {"name": "alpha", "colour": "orange", "reason": "one credential left"})
        self.assertEqual((status, body.get("ok")), (200, True), body)
        lit = self.wire()
        self.assertEqual((lit["colour"], lit["authored"], lit["by"],
                          lit["reason"]), ("orange", True, "owner", "one credential left"))
        self.assertEqual(self.column("alpha"), "orange*")    # and the CLI agrees
        body, status = web._api_projects_state({"name": "alpha", "colour": "clear"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.wire()["authored"], False)

    def test_the_page_cannot_author_what_the_verb_would_refuse(self):  # noqa: VACUOUS_ASSERTION — a literal five-tuple, each arm asserting status 400 and its sentence; the sibling arm above proves the same door DOES write
        for payload, needle in (
                ({"name": "alpha", "colour": "red"}, "needs a reason"),
                ({"name": "alpha", "colour": "red", "reason": "   "}, "needs a reason"),
                ({"name": "alpha", "colour": "mauve", "reason": "r"}, "colour must be"),
                ({"name": "nobody", "colour": "red", "reason": "r"}, "unknown project"),
                ({"colour": "red", "reason": "r"}, "name and colour")):
            with self.subTest(payload=payload):
                body, status = web._api_projects_state(payload)
                self.assertEqual(status, 400, body)
                self.assertIn(needle, body["error"])
        self.assertEqual(self.wire()["authored"], False)

    def test_a_projection_block_does_not_reach_the_page_as_a_light(self):
        reg = pk.read_json(home.registry_path())
        reg["projects"]["alpha"]["state"] = {"colour": "red", "reason": "forged",
                                             "by": "x", "ts": 1}
        pk.write_json(home.registry_path(), reg)
        self.assertEqual((self.wire()["colour"], self.wire()["authored"]),
                         ("active", False))
        self.assertEqual(self.column("alpha"), "active")


if __name__ == "__main__":
    unittest.main()
