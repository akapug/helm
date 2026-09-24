#!/usr/bin/env python3
"""The dispatch write door obeys the project's light.

ITS OWN MODULE BECAUSE `tests/test_dispatches.py` IS AT ITS BLOB CEILING, the
same reason `tests/test_dispatch_brief_ref.py` gives: the never-track rung
refuses a staged source file over 1.0 MiB and these arms pushed that file to
1,048,601 bytes. The fixture they build on — the ledger, the roster and the
temp home — is `DispatchBase`'s, and costs one import.

THE MODULE IS IMPORTED, NEVER ITS NAMES. `from tests.test_dispatches import
DispatchBase` would bind every TestCase in that module into THIS namespace and
unittest discovery would run the whole dispatch suite twice."""
import unittest

from tests import _tmphome           # noqa: F401 — must precede helm.*
from helm import dispatches
from tests import test_dispatches as td


class TheDispatchDoorObeysTheProjectLightTest(td.DispatchBase):
    """A dispatch is where work is HANDED OUT, so it is the second door the
    owner's colour holds at (`helm work claim` is the first, and both ask
    `registry.admits`). The seat and budget rungs are about supply; this one is
    about permission, and it is inside `_base` so every writer shares it."""

    def setUp(self):
        super().setUp()
        from helm import home, pk
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "proj": {"name": "proj", "path": self.repo, "kind": "git",
                     "status": "dormant", "sessions": {}}}})

    def light(self, colour):
        from helm import registry
        row, err = registry.state("proj", colour, reason="the owner decided",
                                  by="owner", apply=True)
        self.assertIsNone(err)

    def attempt(self, **kwargs):
        from tests._tmphome import dispatch_home
        args = {"recipient": "grok", "lane": "lane-light", "ref": self.a,
                "repo": self.repo, "notify": False, "_reason": True}
        args.update(kwargs)
        args.setdefault("new_work", "supersedes" not in args)
        with dispatch_home(self.repo):
            return dispatches.add(**args)

    def test_each_colour_is_graded_for_new_builds_continuations_and_reviews(self):  # noqa: VACUOUS_ASSERTION — a literal table in which every colour has at least one ADMITTED cell asserted as a real row beside its refused ones, and green admits all three
        parent = self.add(recipient="grok", kind="build")     # in flight, minted unlit
        table = (("green", True, True, True), ("yellow", True, True, True),
                 ("orange", True, True, True), ("red", False, True, True))
        for colour, new_build, continued, review in table:
            self.light(colour)
            for label, want, kwargs in (
                    ("new build", new_build, {"kind": "build"}),
                    ("continuation", continued, {"kind": "build",
                                                 "supersedes": parent["id"],
                                                 "force": True}),
                    ("review", review, {"kind": "review"})):
                with self.subTest(colour=colour, door=label):
                    row, err = self.attempt(lane="lane-%s-%s" % (colour, label[:3]),
                                            **kwargs)
                    self.assertEqual(row is not None, want, err)
                    if not want:
                        self.assertIn("proj is %s" % colour.upper(), err)
                        self.assertIn("helm projects state proj", err)

    def test_force_is_about_the_recipient_and_does_not_open_a_red_project(self):  # noqa: VACUOUS_ASSERTION — the refused forced write is followed by the SAME forced write admitted once the light is handed back
        self.light("red")
        row, err = self.attempt(kind="build", force=True)
        self.assertIsNone(row)
        self.assertIn("proj is RED", err)
        self.light("clear")
        row, err = self.attempt(kind="build", force=True)
        self.assertIsNotNone(row, err)

    def test_yellow_admits_the_row_and_says_its_reason_on_the_advisory_channel(self):  # noqa: VACUOUS_ASSERTION — the unlit write's empty note list is the control for the lit write's exactly-one note, same channel, same fixture
        row, err = self.attempt(kind="build", lane="lane-unlit")
        self.assertIsNotNone(row, err)
        self.assertFalse([n for n in row.get(dispatches._ADMISSION_NOTES, ())
                          if "YELLOW" in n])                  # the control
        self.light("yellow")
        row, err = self.attempt(kind="build", lane="lane-lit")
        self.assertIsNotNone(row, err)
        notes = [n for n in row.get(dispatches._ADMISSION_NOTES, ())
                 if "proj is YELLOW" in n]
        self.assertEqual(len(notes), 1, row.get(dispatches._ADMISSION_NOTES))
        self.assertIn("the owner decided", notes[0])

    def test_an_unreadable_registry_never_refuses_a_row(self):
        """A corrupt registry is not the owner saying no — the law the budget
        rung keeps about its own reader, kept here about this one."""
        from helm import home
        self.light("red")
        row, err = self.attempt(kind="build")
        self.assertIsNone(row)                # the rung IS live on this fixture
        self.assertIn("proj is RED", err)
        with open(home.registry_path(), "w") as f:
            f.write("{ not json")
        row, err = self.attempt(kind="build")
        self.assertIsNotNone(row, err)


if __name__ == "__main__":
    unittest.main()
