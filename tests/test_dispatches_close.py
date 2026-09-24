#!/usr/bin/env python3
"""`helm.dispatches_close` — the proof that ENDS a row, and where it resolves.

WHAT THIS FILE PROVES is not that the close ladder works: the arms that drive
these recorders and refusals live where they always did, in the consumer
suites that drive `dispatches`. It proves the SPLIT, which is a different
property and the one no other arm asks about — that a name moved out of the
ledger still resolves the way it did before it moved.
"""
import ast
import io
import unittest

from helm import dispatches, dispatches_close
from tests._satellite_resolution import bare_owner_globals, owner_globals

_OWNER = "helm/dispatches.py"
_SATELLITE = "helm/dispatches_close.py"
_ANCHOR = "_close_event_error"

# An EMPTY observable and a CLEAN one look identical: every arm below asserts
# that something is ABSENT, and each would pass just as well if the thing it
# searched were empty -- if `_OWNER_NAMES` named no names, if the satellite
# defined none. So each arm first asserts, unconditionally, that the
# population it is about to search is the size it should be.
_POPULATED = ("the set under test is empty, so an absence proves nothing "
              "here -- check _OWNER_NAMES and the satellite's own defs")


class TheCloseLadderIsPublishedByTheLedgerTest(unittest.TestCase):
    """The declaration, the definition and the binding are one thing."""

    def _declared(self):
        for module, names in dispatches._OWNER_NAMES:
            if module == "dispatches_close":
                return names
        self.fail("dispatches_close is not declared in dispatches._OWNER_NAMES; "
                  "without the declaration the retired-name rung refuses the "
                  "split and nothing publishes these names back onto the ledger")

    def test_every_declared_name_is_DEFINED_at_column_zero_here(self):
        """A table cannot vouch for a name nobody wrote.

        The retired-name rung requires both halves for exactly this reason:
        the declaration says a name was handed away ON PURPOSE, and the
        satellite's own definition says it is really there. Declaring without
        defining would buy the exemption by typing.
        """
        declared = self._declared()
        self.assertEqual(24, len(declared), _POPULATED)
        tree = ast.parse(io.open(_SATELLITE, encoding="utf-8").read())
        defined = {n.name for n in tree.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef))}
        self.assertTrue(defined, _POPULATED)
        missing = [n for n in declared if n not in defined]
        self.assertEqual([], missing,
                         "declared in _OWNER_NAMES but not defined here: %s"
                         % (missing,))

    def test_every_declared_name_is_PUBLISHED_back_onto_the_ledger(self):
        """`dispatches.NAME` must keep working, because that is how it is spelled.

        `_apply` reaches into this module on every replay, `cli.py` resolves
        its verbs by string, and the dispatch suites patch attributes on
        `dispatches` by the hundred. A split that moved the code and forgot to
        publish leaves every one of those consumers dangling, and a diff
        confined to these files cannot see it.
        """
        declared = self._declared()
        self.assertEqual(24, len(declared), _POPULATED)
        unpublished = [n for n in declared if not hasattr(dispatches, n)]
        self.assertEqual([], unpublished,
                         "moved out of dispatches but never published back: %s"
                         % (unpublished,))

    def test_the_published_object_IS_this_modules_object(self):
        """Published, and published from HERE — not a stale same-named twin."""
        declared = sorted(self._declared())
        # ONE CONCRETE ANCHOR, ASSERTED UNCONDITIONALLY. Everything below is
        # built by a comprehension, whose body runs only for names that are
        # there; this names a real published object and cannot be satisfied by
        # an empty declaration.
        self.assertIs(getattr(dispatches, _ANCHOR),
                      getattr(dispatches_close, _ANCHOR),
                      "dispatches.%s is not this module's object" % _ANCHOR)
        # POSITIVE, UNCONDITIONAL, AND ON THE SAME OBSERVABLE. Asserting that
        # no name MISMATCHES would pass just as well on an empty declaration,
        # and an assertion inside the loop would only run for names that are
        # there. This asserts the matching set IS the declared set.
        same = sorted(n for n in declared
                      if getattr(dispatches, n, None)
                      is getattr(dispatches_close, n, None))
        self.assertEqual(declared, same,
                         "these declared names are not the same object on the "
                         "ledger as here: %s"
                         % (sorted(set(declared) - set(same)),))

    def test_NO_LEDGER_NAME_IS_READ_AS_A_BARE_GLOBAL(self):  # noqa: VACUOUS_ASSERTION — ZERO bare globals IS the product law here, and the instrument is controlled two ways: test_the_checker_itself_can_go_RED requires this same walker to find the ledger's own bare globals, and planting one bare _FULL_TIP in the satellite was OBSERVED turning this arm red before the plant was reverted
        """THE MUST-HIT. This is the arm the whole rewrite exists for.

        A bare global — or a `from` import — binds the object ONCE, at import.
        An arm that patches `dispatches.<name>` and then drives a close would
        reach the object bound at import time and measure NOTHING, while every
        structural guard stayed green, because how a name resolves is a
        property of the runtime and those guards read text.

        It goes red on the real defect: nine ledger constants bound by TUPLE
        UNPACKING were left bare by the first cut of the `landreq` split, and
        the only thing that caught them was driving the code. So the owned set
        here is collected from EVERY top-level binding form, and the arm below
        names one constant that only a tuple-aware collector finds.
        """
        owned = owner_globals(io.open(_OWNER, encoding="utf-8").read())
        owned |= {n for _, names in dispatches._OWNER_NAMES for n in names}
        self.assertTrue(owned, _POPULATED)
        self.assertIn("_TRUNK_REF_KEY", owned,
                      "the ledger binds this one by TUPLE UNPACKING, so a "
                      "collector that only recognises simple targets omits "
                      "it; leaving that form out is the exact defect the "
                      "first cut of the landreq split shipped, where nine "
                      "constants stayed bare and surfaced only as NameError")
        owned -= {"dispatches"}
        leaks = bare_owner_globals(_SATELLITE, owned)
        self.assertEqual([], leaks,
                         "%s reads %d ledger name(s) as bare globals, so an "
                         "arm patching dispatches cannot reach them: %s"
                         % (_SATELLITE, len(leaks), leaks[:10]))

    def test_the_checker_itself_can_go_RED(self):
        """THE CONTROL, because an empty finding and a blind instrument look
        identical. A checker that never fires would pass the arm above on a
        file that was never rewritten at all."""
        owned = owner_globals(io.open(_OWNER, encoding="utf-8").read())
        self.assertNotIn("json", owned,
                         "an IMPORTED name is not an owner name; if it were, "
                         "the split would rewrite `json` to `dispatches.json`")
        self.assertTrue(bare_owner_globals(_OWNER, owned),
                        "the ledger reads its OWN names as bare globals "
                        "throughout — that is what a module does — so a "
                        "checker that finds none there is not looking")

    def test_a_LOCAL_that_shares_a_ledger_name_is_not_accused(self):
        """THE OTHER HALF OF THE CONTROL, and the half that was BROKEN.

        A checker that answers "global" for everything would pass the must-hit
        only by luck and would refuse the next correct file it read. Under PEP
        649 that is what this one did: the annotation scope a def now carries
        reports the def's own line, a lineno-keyed lookup descended into it,
        and every local in the body read as a ledger global. This arm plants
        the shape that caught it — a function whose local shadows a real
        ledger name, and a generator expression whose outermost iterable is
        that local, which lives in NO scope of the genexpr's own.
        """
        import os
        import tempfile
        source = ("def f(rows):\n"
                  "    _ID, seen = 0, []\n"
                  "    for _ID in rows:\n"
                  "        seen.append(_ID)\n"
                  "    return sorted(x for x in seen), _FULL_TIP\n")
        handle, path = tempfile.mkstemp(suffix=".py")
        try:
            with io.open(handle, "w", encoding="utf-8") as out:
                out.write(source)
            hits = bare_owner_globals(path, {"_ID", "_FULL_TIP", "seen"})
        finally:
            os.unlink(path)
        # OUTSIDE THE try, DELIBERATELY. An assertion reachable only when the
        # write succeeded is an assertion that a broken fixture skips, and a
        # skipped control is indistinguishable from a passing one.
        self.assertEqual([(5, "_FULL_TIP")], hits,
                         "the only module global here is _FULL_TIP; _ID and "
                         "seen are locals, and naming either is how this "
                         "checker comes to refuse a correct file")


if __name__ == "__main__":
    unittest.main()
