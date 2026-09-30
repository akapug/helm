#!/usr/bin/env python3
"""The stored whole-suite runner set: one legacy name, accepted and never
produced.

MOVED OUT OF `tests/test_gateshard.py`, where it was the one arm that walks
the tree: `helm gate audits` runs every test module that reads the tree, and
carrying the walk there meant running the other 41 s of that module with it.
The body is the text it had there; only its class is new.
"""
import importlib.util
import os
import unittest

from helm import gate


class StoredSuiteRunnersTest(unittest.TestCase):
    def test_the_legacy_gaterunner_is_accepted_and_never_produced(self):
        """`helm.gaterunner` stays in the stored runner set so the receipts it
        minted on a lane that never merged keep reading as they did; this tree
        must never mint another. Both halves, each against a control:

          ACCEPTED   the set still names it, beside unittest.
          NEVER      no module of that name is importable (the same probe
          PRODUCED   finds a real helm module), the writer spawns `-m
                     unittest`, and no helm source outside the three readers
                     that accept or guard the name spells it."""
        self.assertEqual(gate._STORED_SUITE_RUNNERS,
                         frozenset(("unittest", "helm.gaterunner")))
        self.assertIsNotNone(importlib.util.find_spec("helm.gateshard"))
        self.assertIsNone(importlib.util.find_spec("helm.gaterunner"))
        self.assertEqual(gate.SUITE[:2], ("-m", "unittest"))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spelled = set()
        for dp, _dn, fn in os.walk(os.path.join(root, "helm")):
            for n in fn:
                if n.endswith(".py"):
                    p = os.path.join(dp, n)
                    with open(p, encoding="utf-8") as f:
                        if "gaterunner" in f.read():
                            spelled.add(os.path.relpath(p, root))
        self.assertEqual(spelled, {"helm/gate.py", "helm/gateroute.py",
                                   os.path.join("helm", "work", "_gc.py")})


if __name__ == "__main__":
    unittest.main()
