#!/usr/bin/env python3
"""No seats surface promises that `helm chat read` catches a seat up.

MOVED OUT OF `tests/test_seats.py` (DrainInstructionsNameTheRealVerbTest),
where it was the one arm that lists the package: `helm gate audits` runs every
test module that reads the tree, and carrying the listing there meant running
the other 33 s of that module with it. It reads only the package's source, so
it needs none of that class's fixture. The body is the text it had there.
"""
import os
import unittest

from helm import seats


class NoSurfacePromisesReadCatchesYouUpTest(unittest.TestCase):

    def test_no_surface_promises_that_read_catches_you_up(self):
        # THE WHOLE SEATS PACKAGE, not seats.py — and the split is what
        # taught this test what it was actually asserting. The invariant is
        # "no SURFACE promises that read catches you up"; pinning it to one
        # FILENAME measured where the sentence lived, not whether it was
        # right. When the beacon drain moved to seats_join.py the assertion
        # went green-then-red for a reason that had nothing to do with the
        # promise it guards.
        d = os.path.dirname(seats.__file__)
        names = sorted(f for f in os.listdir(d)
                       if f == "seats.py" or f.startswith("seats_"))
        # CONTROL: the scan sees the package at all. Without it, a renamed
        # directory makes both assertions below vacuously true.
        self.assertGreaterEqual(len(names), 3, "the seats package scan found "
                                               "almost nothing: %s" % names)
        src = ""
        for f in names:
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                src += fh.read()
        self.assertNotIn("helm chat read to catch up", src)
        # and the drain that DOES work is the one we name
        self.assertIn("catchup --including-mentions", src)


if __name__ == "__main__":
    unittest.main()
