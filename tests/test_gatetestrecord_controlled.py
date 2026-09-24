"""The controlled result adapter.

Generating both capture seams from one table prevents two implementations of
the event vocabulary from drifting. It does NOT prevent them from observing
at different points: generation makes the METHOD BODIES identical and leaves
the OBSERVATION POINT free, which is where the drift goes.
"""

import unittest

from helm import gatetestrecord


class _Ledger:
    """Records what the adapter committed, in order."""

    def __init__(self):
        self.valid, self.calls = True, []

    def start(self, target, value):
        self.calls.append(("start", value))

    def stop(self, target, value):
        self.calls.append(("stop", value))

    def event(self, target, value, subtest=None, detail=None):
        self.calls.append(("event", target, value))


class _Probe(unittest.TestCase):
    def runTest(self):  # pragma: no cover - never executed
        pass


class ControlledAdapterTest(unittest.TestCase):

    def _result(self, base, ledger=None):
        ledger = ledger or _Ledger()
        cls = gatetestrecord.controlled_result_class(ledger, base=base)
        return cls(unittest.runner._WritelnDecorator(
            __import__("io").StringIO()), False, 0), ledger

    def test_the_base_return_value_is_the_hook_s_return_value(self):  # noqa: VACUOUS_ASSERTION — assertIs against a unique sentinel the base returns is an unconditional positive: a hook returning None or any other object fails
        """A generated method must not swallow what the base returned.

        Every hook answered None regardless of the base, so any caller
        reading a result method's value silently lost it.
        """
        sentinel = object()

        class Base(unittest.TextTestResult):
            def addSuccess(self, test):
                return sentinel

        result, _ledger = self._result(Base)
        self.assertIs(sentinel, result.addSuccess(_Probe()),
                      "the generated hook discarded the base's return value")

    def test_prepare_runs_before_the_base_mutates(self):
        """THE TWO SEAMS MUST OBSERVE AT THE SAME POINT.

        The profile adapter prepares on `call` -- before the wrapped method
        runs -- and commits on `return`. If the generated adapter prepares
        AFTER calling the base, the two share a table and still answer
        differently about the same run, which is the drift the generation
        exists to prevent.
        """
        seen = []

        class Base(unittest.TextTestResult):
            def addSuccess(self, test):
                seen.append("base")
                return None

        real = gatetestrecord._prepare_callback

        def spy(handler, ledger, values):
            seen.append("prepare")
            return real(handler, ledger, values)

        result, ledger = self._result(Base)
        original = gatetestrecord._prepare_callback
        gatetestrecord._prepare_callback = spy
        try:
            result.addSuccess(_Probe())
        finally:
            gatetestrecord._prepare_callback = original

        self.assertEqual(["prepare", "base"], seen,
                         "prepare ran after the base had already mutated")
        # AND THE COMMIT IS STILL AFTER, which is the other half of the
        # ordering and would otherwise be free to regress unnoticed.
        self.assertEqual([("event", "ok")],
                         [(c[0], c[2]) for c in ledger.calls])

    def test_a_prepare_that_raises_invalidates_rather_than_propagates(self):  # noqa: VACUOUS_ASSERTION — test_prepare_runs_before_the_base_mutates is the unconditional positive control on the same ledger: it proves a working prepare DOES commit, so the empty call list here cannot come from a dead ledger
        """Matching the profile seam, which sets valid=False and continues.

        A recorder that raises through a result callback takes down the run
        it was only supposed to observe.
        """
        class Base(unittest.TextTestResult):
            pass

        def boom(handler, ledger, values):
            raise RuntimeError("recorder failed")

        result, ledger = self._result(Base)
        original = gatetestrecord._prepare_callback
        gatetestrecord._prepare_callback = boom
        try:
            result.addSuccess(_Probe())
        finally:
            gatetestrecord._prepare_callback = original
        self.assertFalse(ledger.valid,
                         "a failed prepare left the ledger claiming valid")
        self.assertEqual([], ledger.calls,
                         "a failed prepare still committed something")


if __name__ == "__main__":
    unittest.main()
