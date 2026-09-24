#!/usr/bin/env python3
"""The roster renders EVERY seat — one unresolvable family cannot truncate it.

`_status` enumerates families from the FILESYSTEM while FAMILIES RESOLVES
them: one fact, two sources of truth, and the directory is reliably first (a
seat dir is created by USE, a catalog entry arrives with a LANDED LANE). So a
directory no catalog entry resolves is a normal, transient state — and it
used to take the whole verb down. `_instance_port` raised KeyError mid-loop,
so the rows already printed stayed on screen and every family sorting AFTER
the unknown one silently vanished.

Measured 2026-08-11: `~/.helm/_global/seats/cursor` existed with no `cursor`
in FAMILIES, so `helm seat list` printed codex, died, and never reached
ds4pro/gemini/grok/kimi. Nothing on screen said four seats were missing.

THE DEFECT IS THE TRUNCATION, NOT THE TRACEBACK, so these arms are built to
fail on a roster that merely stops: the unresolvable family is planted
BETWEEN two resolvable ones, and each is asserted BY NAME through its own
rendered row. An arm satisfied by "no exception raised" would be satisfied by
a function that returns after one row, which is precisely the bug.
"""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from helm import seat, seat_health, seat_paths


# SORTED ORDER IS THE FIXTURE. `_status` walks `sorted(os.listdir(root))`, so
# these names make the unresolvable seat land in the MIDDLE — a row that has
# to render after it, and a row that had to render before it.
BEFORE, UNRESOLVABLE, AFTER = "aaafam", "mmmfam", "zzzfam"


def _family_rows(out):
    """{family: [its column-0 ROWS]} from the rendered roster.

    A bare `assertIn(name, out)` CANNOT answer "did this family render". The
    unresolvable row's own warning line prints `(have: aaafam, zzzfam)`, so
    every resolvable family's name is on screen even when enumeration died
    before reaching it — the exact arm that would pass on the bug. Only a
    column-0 line is a rendered seat: instance, usability, warning, and
    legend lines are all indented.
    """
    rows = {}
    for line in out.splitlines():
        if line and not line.startswith(" "):
            rows.setdefault(line.split()[0], []).append(line)
    return rows


class UnresolvableFamilyKeepsTheRosterWholeTest(unittest.TestCase):
    def setUp(self):
        home = tempfile.mkdtemp(prefix="helm-roster-test-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"HELM_HOME": home})
        env.start()
        self.addCleanup(env.stop)
        self.root = seat.seats_root()
        # THE FIXTURE HAS TO BE IN EFFECT, not merely constructed. Asserting
        # the resolved root lands under the temp home is what separates "this
        # arm reads its own planted fleet" from "this arm read the operator's
        # real one and happened to agree".
        self.assertTrue(self.root.startswith(home),
                        "seats_root() must resolve under the temp HELM_HOME, "
                        "not the live fleet: %s" % self.root)

        # A family entry copied from a real one, so the resolvable rows walk
        # the production render (cred read, proxy verdict, drift) rather than
        # a shape invented here. The names are synthetic on purpose: a real
        # family name would stop being unresolvable the day its lane lands.
        table = {BEFORE: dict(seat.FAMILIES["codex"], port=8801),
                 AFTER: dict(seat.FAMILIES["codex"], port=8802)}
        for name in (BEFORE, UNRESOLVABLE, AFTER):
            os.makedirs(os.path.join(self.root, name))
        fams = mock.patch.dict(seat.FAMILIES, table, clear=True)
        fams.start()
        self.addCleanup(fams.stop)

        # THE DOUBLE MUST REACH THE MODULE THAT RAISED. `_instance_port` lives
        # in seat_paths and indexes FAMILIES directly; a patch that reached
        # only `seat` would leave these arms reading the LIVE catalog while
        # claiming their fixture — and passing for the wrong reason, since the
        # live catalog has no `mmmfam` either.
        self.assertIs(seat_paths.FAMILIES, seat.FAMILIES)
        self.assertIs(seat_health.FAMILIES, seat.FAMILIES)
        self.assertEqual(sorted(seat_paths.FAMILIES), [BEFORE, AFTER])
        self.assertEqual(sorted([AFTER, UNRESOLVABLE, BEFORE]),
                         [BEFORE, UNRESOLVABLE, AFTER],
                         "the fixture's whole point is that a row comes AFTER "
                         "the unresolvable one in _status's sorted walk")

    def _roster(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = seat_health._status([])
        return rc, buf.getvalue()

    def test_every_family_after_an_unresolvable_one_still_renders(self):
        rc, out = self._roster()
        rows = _family_rows(out)
        self.assertEqual(rc, 0)
        # BY NAME, NEVER BY COUNT — and every name, so a roster that stops
        # early is red no matter which row it stopped on.
        self.assertEqual(sorted(rows), [BEFORE, UNRESOLVABLE, AFTER])
        self.assertEqual(len(rows[AFTER]), 1)
        # the row AFTER the unresolvable one went through the RESOLVABLE
        # render: "proxy" is `_proxy_live_text`'s word, and reaching it means
        # `_instance_port` was called for a family that sorts past the raise.
        self.assertIn("proxy", rows[AFTER][0])
        self.assertIn("proxy", rows[BEFORE][0])

    def test_the_unresolvable_family_renders_UNKNOWN_and_says_why(self):
        _, out = self._roster()
        rows = _family_rows(out)
        # A DROPPED ROW IS THE SAME CLASS OF LIE AS A CRASH: the reader sees a
        # clean list and concludes the seat does not exist.
        self.assertIn(UNRESOLVABLE, rows)
        row = rows[UNRESOLVABLE][0]
        # BOTH catalog-derived columns say UNKNOWN. A blank column reads as
        # health — the law `_status` already states for the usability join.
        self.assertEqual(row.count("UNKNOWN"), 2, row)
        detail = [ln for ln in out.splitlines()
                  if ln.strip().startswith("⚠") and UNRESOLVABLE in ln]
        self.assertEqual(len(detail), 1, out)
        self.assertIn("unknown family", detail[0])
        self.assertIn(os.path.join(self.root, UNRESOLVABLE), detail[0])


if __name__ == "__main__":
    unittest.main()
