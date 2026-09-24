"""One seat's row must never cost the reader every OTHER seat's row.

`_status` renders the fleet by looping over the seats root and printing a row
per family. That loop ran unguarded, so a single raise anywhere beneath it —
`_instance_port` subscripting the FAMILIES table for a family whose entry has
not landed was the live one, on 2026-08-12 — took the WHOLE roster down and
printed a traceback in its place. The reader who needs this surface most is
the one whose fleet is already broken.

The guard here is deliberately a NET, not a diagnosis: the uncatalogued-family
case is named properly at the render seam by `_unresolvable_family_row`. These
tests therefore drive a DOUBLED `_seat_row` that raises on demand rather than
relying on any particular family being unrenderable — so they keep testing the
net itself, and stay true once the render seam classifies that specific case.

Both tests assert the SURVIVORS and the observed call list, never merely the
absence of a raise: "it did not blow up" is satisfied by a loop that never ran,
and a loop that never ran is exactly the defect wearing the fix's clothes.
"""
import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

# `seat` IS IMPORTED EXPLICITLY because the co-occurrence guard in
# tests/test_seat_facade_injection.py requires an accepted facade import
# beside any impl import. That requirement is the whole reason for this line;
# it asserts nothing about import order. (`_status`, used below, is defined
# in seat_health itself.)
from helm import seat, seat_catalog, seat_health, seat_usability  # noqa: F401


class RosterRowIsolationTest(unittest.TestCase):
    # Two CATALOGUED families, so nothing here depends on the catalogue having
    # a hole. RAISER must sort first: the defect was that rows AFTER the bad
    # one vanished, so a survivor sorted before it would prove nothing.
    RAISER = "codex"
    SURVIVOR = "kimi"
    SENTINEL = "ROW-RENDERED-OK"

    def setUp(self):
        for fam in (self.RAISER, self.SURVIVOR):
            self.assertIn(fam, seat_catalog.FAMILIES,
                          "fixture premise: both families must be catalogued")
        self.assertLess(self.RAISER, self.SURVIVOR,
                        "the raising family must sort BEFORE the survivor or "
                        "this test cannot see the rows-after-it-vanished bug")
        self.tmp = tempfile.mkdtemp(prefix="helm-roster-")
        self._prior = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        self.seats = os.path.join(
            os.environ["HELM_HOME"], "_global", "seats")
        for fam in (self.RAISER, self.SURVIVOR):
            os.makedirs(os.path.join(self.seats, fam), exist_ok=True)

    def tearDown(self):
        if self._prior is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self._prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, row):
        """Drive `_status` with `_seat_row` doubled, and PROVE the double ran.

        A patch that silently fails to bind renders the real proxy path, and
        every assertion below would then be about the live system wearing this
        test's name.
        """
        buf = io.StringIO()
        with mock.patch.object(seat_health, "_seat_row", row), \
                mock.patch.object(seat_usability, "join", return_value={}), \
                mock.patch.object(seat_usability, "legend", return_value=""):
            self.assertIs(seat_health._seat_row, row,
                          "the _seat_row double is not in effect")
            with redirect_stdout(buf):
                rc = seat_health._status([])
        return rc, buf.getvalue()

    def test_a_raising_row_is_named_and_costs_no_other_row(self):
        seen = []

        def row(f, **kw):
            seen.append(f)
            if f == self.RAISER:
                raise RuntimeError("boom-%s" % f)
            return "%s %s" % (f, self.SENTINEL)

        rc, out = self._run(row)
        # UNCONDITIONAL POSITIVE CONTROL, asserted before anything about the
        # rendered text. Every assertion below is about what the loop PRINTED,
        # and each would pass just as well if the loop had never run at all.
        self.assertEqual(seen, [self.RAISER, self.SURVIVOR],
                         "_seat_row must be called for BOTH families in "
                         "order; an empty list means the roster loop never "
                         "executed and this test proves nothing")
        self.assertEqual(rc, 0)
        # THE ACTUAL DEFECT: the family sorted after the raiser still renders
        self.assertIn("%s %s" % (self.SURVIVOR, self.SENTINEL), out,
                      "the healthy family sorted AFTER the raising one must "
                      "still render — that row vanishing is the whole bug")
        self.assertIn("ROW FAILED", out)
        self.assertIn("RuntimeError", out,
                      "the failure must name its own exception class — a bare "
                      "'failed' routes nobody anywhere")
        self.assertIn("boom-%s" % self.RAISER, out,
                      "and carry the message, not just the type")

    def test_the_failed_row_is_printed_not_dropped(self):
        seen = []

        def row(f, **kw):
            seen.append(f)
            raise KeyError(f)          # every row fails: nothing may vanish

        rc, out = self._run(row)
        self.assertEqual(seen, [self.RAISER, self.SURVIVOR],
                         "positive control: both families reached _seat_row")
        self.assertEqual(rc, 0)
        # A DROPPED row reads as "no such seat", which is worse than a named
        # error — so BOTH failures must appear by name, not be swallowed.
        for fam in (self.RAISER, self.SURVIVOR):
            self.assertIn(fam, out,
                          "a seat whose row failed must still be NAMED; "
                          "silence here reads as the seat not existing")
        self.assertEqual(out.count("ROW FAILED"), 2,
                         "one failure line per failing row, not one for the "
                         "batch — a collapsed line hides which seats are dark")


if __name__ == "__main__":
    unittest.main()
