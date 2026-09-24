#!/usr/bin/env python3
"""helm query — the single authoritative query facade (Step 5, #163).

Tests query_is_open, query_is_unheld_open, query_is_held, query_is_owner_gated,
query_is_stalled, and query_did_land mode whitelisting.
"""

import unittest
from unittest import mock

from helm import query, landreq


class QueryFacadeTest(unittest.TestCase):
    def test_query_is_open_includes_held_and_open_and_excludes_closed(self):
        self.assertTrue(query.query_is_open({"status": "open"}))
        self.assertTrue(query.query_is_open({"status": "held"}))
        self.assertTrue(query.query_is_open({"status": "OPEN"}))
        self.assertFalse(query.query_is_open({"status": "closed"}))
        self.assertFalse(query.query_is_open({"status": "cancelled"}))
        self.assertFalse(query.query_is_open(None))

    def test_query_is_unheld_open_and_query_is_held_distinguish_states(self):
        self.assertTrue(query.query_is_unheld_open({"status": "open"}))
        self.assertFalse(query.query_is_unheld_open({"status": "held"}))
        self.assertTrue(query.query_is_held({"status": "held"}))
        self.assertFalse(query.query_is_held({"status": "open"}))

    def test_query_is_owner_gated_returns_boolean_flag(self):
        self.assertTrue(query.query_is_owner_gated({"owner_gated": True}))
        self.assertFalse(query.query_is_owner_gated({"owner_gated": False}))
        self.assertFalse(query.query_is_owner_gated({}))
        self.assertFalse(query.query_is_owner_gated(None))

    def test_query_is_stalled_excludes_owner_gated_and_terminal_rows(self):
        self.assertTrue(query.query_is_stalled({"status": "open", "stalled": True}))
        self.assertFalse(query.query_is_stalled({"status": "open", "stalled": False}))
        # Owner-gated row is never a machine stall
        self.assertFalse(query.query_is_stalled({"status": "held", "stalled": True, "owner_gated": True}))
        # Terminal rows are never stalled
        self.assertFalse(query.query_is_stalled({"status": "closed", "stalled": True}))
        self.assertFalse(query.query_is_stalled({"status": "cancelled", "stalled": True}))

    def test_an_administratively_retired_row_is_not_STALLED(self):
        """Retirement is terminal, and it does NOT change the status word.

        A retired land request keeps status="open" and gains `retired_admin`
        (query.query_is_retired_admin is the reader). So the terminal-status
        list above cannot see it, and without an explicit rung a retired row
        that was already past threshold keeps reading STALLED forever — it
        stays on stall boards and in stall counts that nag about work nobody
        can act on. The three sibling predicates already carry this rung;
        query_is_stalled was the one that did not.
        """
        retired = {"status": "open", "stalled": True, "retired_admin": True}
        self.assertFalse(query.query_is_stalled(retired))
        # THE CONTROL: the SAME row without the flag really is stalled, so it
        # is `retired_admin` that moved the answer and not the fixture shape.
        control = dict(retired)
        del control["retired_admin"]
        self.assertTrue(query.query_is_stalled(control))
        # and a falsey flag must not retire anything
        self.assertTrue(query.query_is_stalled(
            dict(retired, retired_admin=False)))

    def test_query_did_land_delegates_only_to_whitelisted_modes(self):
        with mock.patch.object(landreq, "landed_ever", return_value=True) as mock_landed:
            res = query.query_did_land("/git", "sha1", "refs/heads/main")
            self.assertTrue(res)
            mock_landed.assert_called_once_with("/git", "sha1", "refs/heads/main")

        with mock.patch.object(landreq, "ancestry", return_value=True) as mock_anc:
            res = query.query_did_land("/git", "sha1", "refs/heads/main", mode="ancestry")
            self.assertTrue(res)
            mock_anc.assert_called_once_with("/git", "sha1", "refs/heads/main")

        with mock.patch.object(landreq, "patch_identity", return_value=True) as mock_pid:
            res = query.query_did_land("/git", "sha1", "refs/heads/main", mode="patch_identity")
            self.assertTrue(res)
            mock_pid.assert_called_once_with("/git", "sha1", "refs/heads/main")

        with self.assertRaises(ValueError):
            query.query_did_land("/git", "sha1", "refs/heads/main", mode="arbitrary_callable")

    def test_content_presence_is_NOT_allowlisted_because_nothing_implements_it(self):  # noqa: VACUOUS_ASSERTION — assertIn('landed_ever', ALLOWED_LANDED_MODES) is an unconditional positive on the SAME tuple, and the assertRaises below is a positive observable, so an empty allowlist reddens rather than passing
        """A review blocker, four rounds and two authors old.

        `content_presence` sat in ALLOWED_LANDED_MODES with NO implementation
        anywhere, so getattr(landreq, mode) returned None and the call fell
        through to the deprecated landreq._landed. Asking for the ONE
        revert-aware fact silently returned the predicate that cannot see a
        revert — the facade substituting a weaker answer under the requested
        name, which is the exact thing a facade exists to prevent.

        WHY IT IS REMOVED RATHER THAN WRITTEN: landreq.landed_ever's own
        docstring settles it — net tree presence is not answerable from ancestry
        OR patch-identity, "and nothing else here answers it either". Writing it
        needs a tree-content probe and a land-then-revert arm against REAL git.
        task/958's gap stays DECLARED rather than filled by a name that resolves
        to something weaker.
        """
        self.assertNotIn("content_presence", query.ALLOWED_LANDED_MODES)
        self.assertIn("landed_ever", query.ALLOWED_LANDED_MODES)   # MUST-HIT
        with self.assertRaises(ValueError):
            query.query_did_land("/git", "sha1", "refs/heads/main",
                                 mode="content_presence")

    def test_an_allowlisted_mode_with_NO_predicate_REFUSES_not_substitutes(self):  # noqa: VACUOUS_ASSERTION — the assertRaises(AssertionError) IS the positive observable and the message is asserted, so a call that never reached the branch fails on 'AssertionError not raised' before any absence is read
        """THE CLASS, not the instance. Removing one name from the list does not
        stop the next one being added without an implementation — the SILENT
        FALL-THROUGH is what turned that mistake into a wrong answer, and it is
        what is actually cured here.

        LOAD-BEARING MUTATION: restore `return landreq._landed(...)` in place of
        the raise -> this arm reddens while every delegating arm above stays
        green, because they all name predicates that DO exist.
        """
        self.assertNotIn("ghost_mode", query.ALLOWED_LANDED_MODES)  # MUST-HIT
        with mock.patch.object(query, "ALLOWED_LANDED_MODES",
                               query.ALLOWED_LANDED_MODES + ("ghost_mode",)):
            self.assertFalse(hasattr(landreq, "ghost_mode"))
            with self.assertRaises(AssertionError) as caught:
                query.query_did_land("/git", "sha1", "refs/heads/main",
                                     mode="ghost_mode")
        self.assertIn("must not substitute", str(caught.exception))

    def test_the_facade_never_reaches_the_deprecated__landed(self):
        """THE CENSUS, which is the acceptance arm the integrator named: no path
        through this module may call landreq._landed. A grep is the right
        instrument here because the module is small and the name is exact — and
        it is seeded with a MUST-HIT so a scan that matches nothing cannot pass
        as a clean one."""
        import inspect
        src = inspect.getsource(query)
        self.assertIn("landreq", src)                 # MUST-HIT: the scan works
        self.assertNotIn("landreq._landed(", src,
                         "the facade reaches the deprecated lossy predicate")


if __name__ == "__main__":
    unittest.main()
