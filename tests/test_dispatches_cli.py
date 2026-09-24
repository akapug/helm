"""The dispatch verb table's own module — the UNTESTED rung's real import.

`helm/dispatches_cli.py` carries `_cmd_dispatch` and the private helpers only
it uses. The code moved WHOLE out of `helm/dispatches.py`, which sat 4,094
bytes under the never-track ceiling with three more cures queued to land in
it; no body was rewritten on the way.

THESE ARMS PIN THE SEAM, NOT THE VERBS. What the verbs do is covered where it
always was. What is new and therefore untested is the BINDING: that the split
is invisible to every caller, and that neither import order deadlocks.
"""
import importlib
import inspect
import subprocess
import sys

from tests.test_dispatches import DispatchBase

from helm import dispatches, dispatches_cli


class TheVerbTableIsReachableFromEitherSideTest(DispatchBase):

    def test_every_owned_name_still_resolves_from_dispatches(self):
        """The split is behaviour-neutral for callers or it is not done.

        `cli.py` resolves `cmd_dispatch` by string and four arms name
        `dispatches._cmd_dispatch` directly, one of them a registry keyed by
        the bare name. Every owned name must answer from the old module.
        """
        self.assertTrue(dispatches_cli.owned(),
                        "the owner list is empty, so this arm would pass on "
                        "a module that published nothing")
        for name in dispatches_cli.owned():
            self.assertIs(getattr(dispatches, name),
                          getattr(dispatches_cli, name),
                          "%s does not answer from dispatches" % name)

    def test_getsource_follows_a_published_name_to_this_file(self):
        """Three arms read the verb table through `inspect.getsource` on the
        OLD module's attribute. A re-bound function carries its real file, so
        they keep working -- asserted rather than assumed."""
        path = inspect.getsourcefile(dispatches._cmd_dispatch)
        self.assertTrue(path.endswith("dispatches_cli.py"), path)
        self.assertIn("def _cmd_dispatch",
                      inspect.getsource(dispatches._cmd_dispatch))

    def test_NEITHER_import_order_deadlocks(self):  # noqa: VACUOUS_ASSERTION — the loop carries every assertion and checked==2 after it is the unconditional must-hit
        """The satellite imports the ledger eagerly and the ledger imports the
        satellite at its tail, so the order a process happens to reach first
        is a real question rather than a theoretical one.

        MEASURED WHEN THIS WAS BUILT: importing NAMES at the ledger's tail
        raised ImportError from a partially initialised module when the
        satellite was imported first -- which is precisely what a test module
        for the satellite does. Importing the MODULE and publishing back is
        the cure, and this arm is what keeps it.
        """
        checked = 0
        for first in ("helm.dispatches_cli", "helm.dispatches"):
            checked += 1
            out = subprocess.run(
                [sys.executable, "-c",
                 "import importlib;importlib.import_module(%r);"
                 "from helm import dispatches;"
                 "print(callable(dispatches._cmd_dispatch))" % first],
                capture_output=True, text=True, cwd=".")
            self.assertEqual(out.returncode, 0,
                             "importing %s first failed:\n%s"
                             % (first, out.stderr[-900:]))
            self.assertEqual(out.stdout.strip(), "True",
                             "importing %s first left the binding unresolved"
                             % first)
        # UNCONDITIONAL: the loop above carries every assertion in this arm,
        # so an empty or short loop would pass having proved nothing.
        self.assertEqual(checked, 2, "both import orders must be exercised")

    def test_the_ledger_does_not_call_back_into_the_verb_table(self):
        """The cut was chosen because it has ZERO in-edges: the verb table is
        a leaf CONSUMER of the ledger. If the ledger grows a call INTO it the
        seam stops being a leaf and the next split gets harder, so the
        property is pinned rather than remembered."""
        src = inspect.getsource(dispatches)
        owned = [n for n in dispatches_cli.owned() if n != "_cmd_dispatch"]
        self.assertTrue(owned, "no owned names to check")
        for name in owned:
            self.assertNotIn(name + "(", src,
                             "dispatches calls %s, which the satellite owns" % name)
        # UNCONDITIONAL POSITIVE CONTROL on the SAME observable: the search
        # really does find a call when one is there. Without it, a `src` that
        # had come back empty would satisfy every assertion above.
        self.assertIn("snapshot(", src,
                      "the source search found no call it certainly makes, so "
                      "the absences above prove nothing")


    def test_the_declaration_and_the_binding_cannot_drift(self):
        """ONE SOURCE OF TRUTH, asserted rather than trusted.

        The retired-name rung reads `dispatches._OWNER_NAMES` to decide that
        these names were handed over rather than retired; the publish loop
        walks the SAME tuple. A second private list would let the rung clear
        a name nothing binds, or bind a name nothing declared -- which is the
        drift the single literal exists to prevent.
        """
        declared = dict(dispatches._OWNER_NAMES)
        self.assertIn("dispatches_cli", declared,
                      "the ledger declares no satellite, so the rung has "
                      "nothing to clear this module's names with")
        self.assertEqual(tuple(declared["dispatches_cli"]),
                         tuple(dispatches_cli.owned()),
                         "the publish loop and the declaration disagree")
        for name in declared["dispatches_cli"]:
            self.assertTrue(hasattr(dispatches_cli, name),
                            "%s is declared but this module does not define "
                            "it; the rung would clear a name nothing binds"
                            % name)


if __name__ == "__main__":
    import unittest
    unittest.main()
