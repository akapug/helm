#!/usr/bin/env python3
"""PROJECT-SCOPED PIPELINE PROJECTIONS (task/974).

The land pipeline used to render EVERY dispatch row regardless of which
project it belonged to, so another project's row inflated the owner's own
burn-down. The cure has three halves and each is pinned here:

  1. DEFAULT SCOPE — rows whose repo_id resolves to another project leave
     the loop/stall listings and their counts;
  2. NO SILENT TRUNCATION — the withheld population is DISCLOSED
     (`withheld_split` / `withheld_line`), a number and an escape, never an
     absence, and a row whose project cannot be resolved goes in a disclosed
     UNRESOLVED bucket rather than being guessed into either side;
  3. THE ESCAPE — `--all-projects` renders every row, each foreign one
     labeled with the project that owns it.

THE MAPPER UNDER TEST IS REAL. Only the registry FILE is synthetic
(`pin_registry` swaps `registry.load`); classification still runs through
`project_for_cwd`'s longest-prefix resolution — mocking the mapper itself
would assert about a lambda and never about the resolver.

FOREIGN ROWS ARE MINTED BY THE FOREIGN REPO'S OWN HELM (`dispatch_home`),
because that WAS the only way such a row honestly existed — the write door
refused foreign refs, and disarming it would build a ledger state
production cannot produce.

SINCE task/2437 A REGISTERED PROJECT'S ROW IS ADMITTED BY THE REAL DOOR, so
`CwdScopedCliListingsTest` mints its cross-project rows with NO `dispatch_home`
at all: the honest shape is now the plain one. The older arms keep the helper
because they model an UNREGISTERED or differently-homed repository, which is
still only reachable that way.
"""
import json
import os
import shutil
import sys
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import tests._tmphome  # noqa: F401 — one tmp HELM_HOME per process
from helm import dispatches, eventledger, landreq, registry
from tests._tmphome import dispatch_home
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_landreq as _landreq
from tests.test_landreq import run


class ProjectScopeBase(_landreq.LandReqBase):
    def clone(self, name):
        """A second repository beside the fixture's own — same objects, its
        own gitdir, so a row minted there carries a DIFFERENT repo_id."""
        other = os.path.join(self.tmp, name)
        subprocess.run(["git", "clone", "-q", self.repo, other], check=True)
        return other

    def pin_registry(self, projects):
        """The registry answers with EXACTLY these projects. realpath'd so
        the registered prefix matches the realpath'd repo_id the dispatch
        writer stamps — the same relationship the live registry has."""
        data = {"projects": {n: {"name": n, "path": os.path.realpath(p)}
                             for n, p in projects.items()}}
        patcher = mock.patch.object(registry, "load", return_value=data)
        patcher.start()
        self.addCleanup(patcher.stop)

    def chdir(self, where):
        """Stand in a checkout, the way a seat does. Restored on the way out.

        ONE RESTORE, AND IT IS TO A DIRECTORY THAT OUTLIVES THE FIXTURE
        (task/2437 round four). `unittest` runs `tearDown()` BEFORE
        `doCleanups()`, and `LandReqBase.tearDown` removes `self.tmp` — so a
        per-hop `addCleanup(os.chdir, os.getcwd())` registers a restore INTO
        the fixture's own tree, and the second hop's cleanup then chdir'd into
        a directory that had just been deleted. Every arm that stood in two
        checkouts errored with a bare `FileNotFoundError: .../other-repo` and
        no traceback at all — the raising frame is `unittest.case`'s own call
        of the cleanup, and `case.py` sets `__unittest = True`, so the frame
        filter ate the whole stack and left an exception with no origin.
        Measured: five arms in this module, all and only the ones that hop
        twice.

        So the restore is registered ONCE, on the first hop, and it names the
        cwd this test STARTED in — the suite's own root, which no fixture
        teardown can remove. Later hops just move.
        """
        if getattr(self, "_entry_cwd", None) is None:
            self._entry_cwd = os.getcwd()
            self.addCleanup(os.chdir, self._entry_cwd)
        os.chdir(where)

    def foreign_add(self, repo, lane, ref, deadline_s=60):
        """A row minted AS the foreign repository's own helm."""
        with dispatch_home(repo):
            row = dispatches.add("seat-a", lane, ref=ref, repo=repo,
                                 new_work=True, kind="review",
                                 deadline_s=deadline_s, notify=False)
        self.assertIsNotNone(row)
        return row


class DefaultScopeWithholdsForeignRowsTest(ProjectScopeBase):
    def test_default_scope_withholds_the_foreign_row_and_disclosure_counts_it(self):
        other = self.clone("other-repo")
        self.pin_registry({"homeproj": self.repo, "otherproj": other})
        home = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(home["id"], "post-h")
        self.age(home["id"], 3600)
        foreign = self.foreign_add(other, "lane/foreign-work", self.c)
        dispatches._mark_delivered(foreign["id"], "post-f")
        self.age(foreign["id"], 3600)

        # The scope derives through the ONE mapper: home repo -> "homeproj".
        scope = landreq.board_scope()
        self.assertEqual(scope["project"], "homeproj")
        self.assertIsNone(scope["why"])

        lrs, raw, unavailable = landreq.project_raw(time.time(), scope=scope)
        self.assertIsNone(unavailable)
        # WITHHELD, NEVER DROPPED: the projection still holds the row, marked
        # with the project that owns it (`helm lr show` keeps answering).
        self.assertIn(foreign["id"], lrs)
        self.assertTrue(lrs[foreign["id"]]["foreign"])
        self.assertEqual(lrs[foreign["id"]]["foreign_project"], "otherproj")

        # THE PRIMARY BOARD — and through it the card's contrary count,
        # which is derived FROM the loops list (00-core.js `live.filter(
        # c => c.contrary)`), so leaving loops IS leaving contrary.
        # Positive control on the SAME observable: the home row renders.
        loop_ids = [r["id"] for r in landreq._loop_rows(lrs, raw)]
        self.assertIn(home["id"], loop_ids)
        self.assertNotIn(foreign["id"], loop_ids)

        # THE STALL LIST — the foreign row IS a stall the wide view can see
        # (control), so its absence from the default list is the scope
        # working, not the row failing to stall.
        stalled_all = [r["id"] for r in
                       landreq._stalled_rows(lrs, raw, all_projects=True)]
        self.assertIn(foreign["id"], stalled_all)
        stalled_ids = [r["id"] for r in landreq._stalled_rows(lrs, raw)]
        self.assertIn(home["id"], stalled_ids)
        self.assertNotIn(foreign["id"], stalled_ids)

        # THE DISCLOSURE COUNT EQUALS THE FIXTURE'S OWN POPULATION: exactly
        # ONE foreign row was minted above, owned by exactly one project.
        w = landreq.withheld_split(lrs, scope)
        self.assertEqual(w["foreign"], 1)
        self.assertEqual(w["by_project"], {"otherproj": 1})
        self.assertEqual(w["unresolved"], 0)
        self.assertEqual(w["scope"], "homeproj")

    def test_cli_default_discloses_and_all_projects_labels_the_foreign_row(self):
        other = self.clone("other-repo")
        self.pin_registry({"homeproj": self.repo, "otherproj": other})
        home = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(home["id"], "post-h")
        foreign = self.foreign_add(other, "lane/foreign-work", self.c)
        dispatches._mark_delivered(foreign["id"], "post-f")

        # --cold: this arm tests the COLD projection's disclosure; the warm
        # accelerator is a different transport with its own vintage rules.
        rc, out, _err = run(["list", "--cold"])
        self.assertEqual(rc, 0)
        # The home row renders; the foreign row does not — and the header
        # SAYS what left, whose it was, and the escape. A number and an
        # escape, never an absence. Asserted by ROW IDENTITY (the id prefix
        # `_line` prints), with the rendered lines in the message so a
        # failure names what actually rendered.
        rows_shown = "|".join(out.splitlines()[2:6])
        self.assertIn(home["id"][:12], out, "rows: " + rows_shown)
        self.assertNotIn(foreign["id"][:12], out, "rows: " + rows_shown)
        disclosure = out.splitlines()[1] if out.count("\n") else out
        self.assertIn("project scope: homeproj", out, disclosure)
        self.assertIn("1 row from other projects withheld (otherproj 1)",
                      out, disclosure)
        self.assertIn("--all-projects shows them", out, disclosure)

        rc2, out2, _err2 = run(["list", "--all-projects"])
        self.assertEqual(rc2, 0)
        self.assertIn(home["id"][:12], out2)
        self.assertIn(foreign["id"][:12], out2)
        self.assertIn("ALL PROJECTS", out2)
        self.assertIn("shown labeled", out2)
        self.assertIn("FOREIGN — project otherproj owns this row", out2)


class UnresolvableRepoIsDisclosedNotGuessedTest(ProjectScopeBase):
    def test_unresolvable_repo_lands_in_the_disclosed_unresolved_bucket(self):
        other = self.clone("other-repo")
        stray = self.clone("stray-repo")
        # `stray` is DELIBERATELY not registered: its repo_id resolves to no
        # project, which must answer UNRESOLVED — never "mine" (that would
        # adopt exactly the rows least entitled to a home) and never
        # "foreign to project X" (a guess with a name on it). And UNRESOLVED
        # is not exile: absence of provenance is not foreignness, so the row
        # KEEPS ITS PLACE ON THE BOARD (the partial-projection law), marked,
        # and the disclosure counts it as its own bucket.
        self.pin_registry({"homeproj": self.repo, "otherproj": other})
        home = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(home["id"], "post-h")
        stray_row = self.foreign_add(stray, "lane/stray-work", self.c)
        dispatches._mark_delivered(stray_row["id"], "post-s")

        scope = landreq.board_scope()
        lrs, raw, unavailable = landreq.project_raw(time.time(), scope=scope)
        self.assertIsNone(unavailable)
        self.assertTrue(lrs[stray_row["id"]]["project_unresolved"])
        self.assertFalse(lrs[stray_row["id"]].get("foreign", False))

        loop_ids = [r["id"] for r in landreq._loop_rows(lrs, raw)]
        self.assertIn(home["id"], loop_ids)
        self.assertIn(stray_row["id"], loop_ids)   # VISIBLE, never exiled
        # ... and its rendered line carries the mark, so a reader cannot
        # mistake it for a row this board vouches for.
        stray_lr = lrs[stray_row["id"]]
        self.assertIn("UNRESOLVED", landreq._line(stray_lr))

        w = landreq.withheld_split(lrs, scope)
        self.assertEqual(w["unresolved"], 1)   # the ONE stray row minted above
        self.assertEqual(w["foreign"], 0)
        line = landreq.withheld_line(w)
        self.assertIn("1 row of UNRESOLVED project shown, marked", line)


class UnavailableLedgerRefusesWholesaleTest(ProjectScopeBase):
    def test_unavailable_body_carries_no_confident_withheld_zero(self):
        """An unreadable ledger refuses the WHOLE board — the new disclosure
        fields obey the same law as `filed`: None on every unavailable body,
        never a dict of zeros, because "0 withheld" is a claim about a
        population this body never read (no confident narrower board)."""
        from helm import web
        with mock.patch.object(landreq, "project_raw",
                               return_value=({}, {}, "the ledger is a lie")):
            body = web._lr_project(time.time(), None)
        self.assertEqual(body["unavailable"], "the ledger is a lie")
        self.assertEqual(body["loops"], [])
        self.assertIsNone(body["withheld"])
        self.assertIsNone(body["filed"])
        # And the one-owner renderer SAYS unknown for a missing reading
        # rather than inventing a zero.
        self.assertIn("UNKNOWN", landreq.withheld_line(None))

    def test_unresolvable_scope_marks_nothing_and_says_so(self):
        """No repository identity -> NO narrower board: every row renders and
        the disclosure names why the scope could not be resolved."""
        other = self.clone("other-repo")
        self.pin_registry({"homeproj": self.repo, "otherproj": other})
        home = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(home["id"], "post-h")
        foreign = self.foreign_add(other, "lane/foreign-work", self.c)
        dispatches._mark_delivered(foreign["id"], "post-f")

        with mock.patch.object(dispatches, "home_repo_id",
                               return_value=(None, "no package identity")):
            scope = landreq.board_scope()
        self.assertIsNone(scope["repo_id"])
        self.assertEqual(scope["why"], "no package identity")
        lrs, raw, unavailable = landreq.project_raw(time.time(), scope=scope)
        self.assertIsNone(unavailable)
        loop_ids = [r["id"] for r in landreq._loop_rows(lrs, raw)]
        self.assertIn(home["id"], loop_ids)
        self.assertIn(foreign["id"], loop_ids)     # WIDE, not narrower
        line = landreq.withheld_line(landreq.withheld_split(lrs, scope))
        self.assertIn("UNRESOLVED", line)
        self.assertIn("no package identity", line)
        self.assertIn("EVERY project's rows", line)


class WebBodyCarriesTheDisclosureTest(ProjectScopeBase):
    def test_default_web_body_withholds_and_discloses_and_escape_labels(self):
        other = self.clone("other-repo")
        self.pin_registry({"homeproj": self.repo, "otherproj": other})
        home = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(home["id"], "post-h")
        foreign = self.foreign_add(other, "lane/foreign-work", self.c)
        dispatches._mark_delivered(foreign["id"], "post-f")

        from helm import web
        body = web._lr_project(time.time(), None)
        loop_ids = [c["id"] for c in body["loops"]]
        self.assertIn(home["id"], loop_ids)
        self.assertNotIn(foreign["id"], loop_ids)
        self.assertEqual(body["withheld"]["foreign"], 1)
        self.assertEqual(body["withheld"]["by_project"], {"otherproj": 1})
        self.assertEqual(body["withheld"]["scope"], "homeproj")
        self.assertFalse(body["all_projects"])

        wide = web._lr_project(time.time(), None, all_projects=True)
        wide_ids = [c["id"] for c in wide["loops"]]
        self.assertIn(foreign["id"], wide_ids)
        self.assertTrue(wide["all_projects"])
        # The escape's rows carry their labels on the wire — an unlabeled
        # foreign row is the confusion the scope exists to end.
        card = [c for c in wide["loops"] if c["id"] == foreign["id"]][0]
        self.assertTrue(card["foreign"])
        self.assertEqual(card["foreign_project"], "otherproj")


class ObservationAuthorityIsStrictlyOwnedTest(ProjectScopeBase):
    def test_no_git_runs_in_a_repository_this_board_cannot_prove_it_owns(self):
        """A FIX on this lane: VISIBILITY and GIT AUTHORITY are two
        predicates. `_this_boards_row` (not foreign) keeps an unresolved row
        VISIBLE; `_observation_owned` must still refuse it the git leg,
        because its repo_id is a REAL repository the registry cannot assign,
        and `_observe`'s cannot-resolve-locally path reaches `ls-remote`
        inside it. The door is SPIED, not inferred from a complaint's
        absence: the home repo's observation is the positive control on the
        SAME spy, so an empty call list cannot pass as suppression."""
        other = self.clone("other-repo")
        stray = self.clone("stray-repo")
        self.pin_registry({"homeproj": self.repo, "otherproj": other})
        home = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(home["id"], "post-h")
        # A verdict CLOSES each loop — the git leg only fires past `closed`,
        # so without these the spy would record nothing and prove nothing.
        dispatches.mark_verdict(home["id"], self.side, "ok",
                                polarity="approve")
        foreign = self.foreign_add(other, "lane/foreign-work", self.b)
        dispatches._mark_delivered(foreign["id"], "post-f")
        dispatches.mark_verdict(foreign["id"], self.b, "ok",
                                polarity="approve")
        stray_row = self.foreign_add(stray, "lane/stray-work", self.c)
        dispatches._mark_delivered(stray_row["id"], "post-s")
        dispatches.mark_verdict(stray_row["id"], self.c, "ok",
                                polarity="approve")
        home_gitdir = dispatches._repo_info(self.repo)["repo_id"]
        other_gitdir = dispatches._repo_info(other)["repo_id"]
        stray_gitdir = dispatches._repo_info(stray)["repo_id"]

        scope = landreq.board_scope()
        with mock.patch.object(landreq, "_git_observe",
                               wraps=landreq._git_observe) as spy:
            lrs, raw, unavailable = landreq.project_raw(time.time(),
                                                        scope=scope)
        self.assertIsNone(unavailable)
        observed = [c.args[0] if c.args else c.kwargs.get("gitdir")
                    for c in spy.call_args_list]
        # POSITIVE CONTROL on the same observable: this board's own repo IS
        # observed, so the door demonstrably fires in this fixture.
        self.assertIn(home_gitdir, observed)
        # THE FINDING: the unresolved row's real repository is never entered.
        self.assertNotIn(stray_gitdir, observed)
        # And the foreign row's repository stays unentered too (a64fd802c's
        # original scope, preserved through the predicate split).
        self.assertNotIn(other_gitdir, observed)

        # The rows LOSE only the git leg, not their place or their marks:
        # unresolved stays visible-and-marked, and both render UNKNOWN on
        # every git-derived field rather than a fact nothing measured.
        self.assertIn(stray_row["id"],
                      [r["id"] for r in landreq._loop_rows(lrs, raw)])
        self.assertTrue(lrs[stray_row["id"]]["project_unresolved"])
        self.assertFalse(lrs[stray_row["id"]]["observable"])
        self.assertFalse(lrs[stray_row["id"]]["landed"])
        self.assertTrue(lrs[foreign["id"]]["foreign"])
        self.assertFalse(lrs[foreign["id"]]["observable"])


class WarmJsonRowsTest(ProjectScopeBase):
    """task/1821 (owner-directed) — `--json` rides the warm projection.

    The warm guard used to conflate SHAPE (json vs card) with SCOPE (which
    rows), so the machine-readable path — the one agents reach for by
    correct instinct — was the one excluded from the accelerator: measured
    2026-08-28, human path 307ms, agent path 90s over the SAME rows. The
    cure attaches the raw row product to the warm body (`rows`,
    web_land._lr_project) and lets only SCOPE decide warm-vs-cold. These
    arms pin the two halves the cure could silently lose: the SHAPE
    contract (warm stdout is byte-identical to the cold replay's) and the
    ABSENCE contract (a refused chain removes the field, never empties it).
    """

    def _minted_row(self):
        home = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(home["id"], "post-h")
        return home

    def test_warm_json_stdout_is_byte_identical_to_the_cold_replay(self):
        """ONE PINNED INSTANT feeds both transports, because the rows carry
        clock-derived fields (dwell) and a byte comparison across two real
        instants would measure the clock, not the contract."""
        home = self._minted_row()
        t0 = time.time()
        from helm import web
        with mock.patch.object(time, "time", return_value=t0):
            rc_cold, out_cold, err_cold = run(["list", "--json", "--cold"])
            self.assertEqual(rc_cold, 0)
            body = web._lr_project(t0, None)
        cold_rows = json.loads(out_cold)
        # POSITIVE CONTROL on the fixture itself: the minted row is IN the
        # cold answer, so the equality below compares real content and not
        # two empty lists.
        self.assertEqual([r["id"] for r in cold_rows], [home["id"]])
        self.assertIsInstance(body.get("rows"), list)
        # THE WIRE ROUND-TRIP IS PART OF THE CLAIM: the CLI receives the
        # body through json.load, so the fixture must cross the same
        # serialization boundary the live transport imposes.
        wire = json.loads(json.dumps(
            dict(body, read_age_s=5, ledger_age_s=600)))
        with mock.patch.object(landreq, "warm_lr_body",
                               return_value=(wire, None)):
            with mock.patch.object(time, "time", return_value=t0):
                rc_warm, out_warm, err_warm = run(["list", "--json"])
        self.assertEqual(rc_warm, 0)
        self.assertEqual(out_warm, out_cold)
        # The freshness note is STATED, on stderr, where it cannot enter
        # the parsed stream — and the disclosure line rides stderr on both
        # transports, same shape.
        self.assertIn("WARM projection, computed 5s ago", err_warm)
        self.assertIn("--cold replays", err_warm)
        self.assertIn("project scope", err_warm)
        self.assertIn("project scope", err_cold)

    def test_builder_attaches_rows_and_REMOVES_them_on_chain_refusal(self):
        """ABSENT, NEVER EMPTY: an attached-then-refused `rows` would ride
        the unavailable body as a confident zero-or-partial answer, and the
        CLI's vintage check reads absence as `fall back cold`."""
        self._minted_row()
        from helm import web
        body = web._lr_project(time.time(), None)
        # POSITIVE CONTROL: on a healthy build the field exists and names
        # exactly the loop frontier's rows, same ids, same order.
        self.assertIsInstance(body.get("rows"), list)
        self.assertEqual([r["id"] for r in body["rows"]],
                         [c["id"] for c in body["loops"]])
        with mock.patch.object(
                landreq, "_loop_rows",
                side_effect=landreq._ChainUntrustworthy("chain broken")):
            refused = web._lr_project(time.time(), None)
        self.assertEqual(refused["unavailable"], "chain broken")
        self.assertNotIn("rows", refused)


class CwdScopedCliListingsBase(ProjectScopeBase):
    """The cwd fixture: `two_projects` registers a second project and mints
    one row in each through the real write door, and `dispatch_cli` runs the
    `helm dispatch` door with its output captured.

    THIS CLASS HOLDS NO `test_*` METHOD, and that is its contract. unittest
    collects every inherited `test_*` again under each subclass's own id, so
    an arm written here runs once per subclass. A class that needs this
    fixture subclasses THIS class; its arms go in the subclass."""

    def dispatch_cli(self, argv):
        import contextlib as _c
        import io as _io
        out, err = _io.StringIO(), _io.StringIO()
        with _c.redirect_stdout(out), _c.redirect_stderr(err):
            rc = dispatches.cmd_dispatch(argv)
        return rc, out.getvalue(), err.getvalue()

    def two_projects(self):
        other = self.clone("other-repo")
        self.pin_registry({"homeproj": self.repo, "otherproj": other})
        home = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(home["id"], "post-h")
        foreign, why = dispatches.add(
            "seat-a", "lane/other-project-work", ref=self.c, repo=other,
            new_work=True, kind="review", deadline_s=60, notify=False,
            _reason=True)
        self.assertIsNotNone(
            foreign, "the registered project's row was refused at the write "
            "door, so there is nothing for the listings to scope: %s" % (why,))
        self.assertEqual(foreign["repo_id"],
                         dispatches._repo_info(other)["repo_id"])
        dispatches._mark_delivered(foreign["id"], "post-f")
        return other, home, foreign


class CwdScopedCliListingsTest(CwdScopedCliListingsBase):
    """THE LISTING SCOPES TO THE DIRECTORY YOU RUN IT IN (task/2437).

    `board_scope(repo_id)` always answered correctly for any repository; only
    its DEFAULT was helm-anchored, because it came from `home_repo_id()` — the
    location of the running helm PACKAGE. Measured on the live box from another
    registered project's checkout: the bare call answered the HELM project while
    `board_scope(<that repo's gitdir>)` answered that project. So a seat working
    in another project's checkout read helm's board and nothing on the surface
    said so.

    THE FOREIGN ROW IS MINTED THROUGH THE REAL WRITE DOOR HERE, with no
    `dispatch_home` — that is the point of the cure and it is why these arms are
    in this module rather than beside a mock: the row exists the way the world
    now mints it, admitted because the registry places its repository, while
    this fixture's `home_repo_id` still points at the base repo.

    A subclass of THIS class runs every arm below again under its own id,
    which is right only for one that changes the fixture those arms read. A
    class that only needs the fixture subclasses CwdScopedCliListingsBase."""

    def test_lr_list_from_each_checkout_scopes_to_that_checkouts_project(self):
        other, home, foreign = self.two_projects()

        # FROM THE OTHER PROJECT'S CHECKOUT: its row is LOCAL and helm's is the
        # withheld one. The disclosure names the scope, so the narrowing is
        # never a silent absence.
        self.chdir(other)
        # THE DISCRIMINATING FACT, asserted before the rendering: what moved is
        # the DEFAULT, not the marking. From this same cwd the bare
        # `board_scope()` still answers the package's project — that is the old
        # behaviour, still reachable — while the CLI door now fills the operand
        # in from the directory.
        self.assertEqual(landreq.board_scope()["project"], "homeproj")
        self.assertEqual(landreq._cli_scope()["project"], "otherproj")
        rc, out, _err = run(["list", "--cold"])
        self.assertEqual(rc, 0, out)
        self.assertIn(foreign["id"][:12], out)
        self.assertNotIn(home["id"][:12], out)
        self.assertIn("project scope: otherproj", out)

        # FROM THE HOME CHECKOUT, the same ledger, the mirror answer.
        self.chdir(self.repo)
        rc, out, _err = run(["list", "--cold"])
        self.assertEqual(rc, 0, out)
        self.assertIn(home["id"][:12], out)
        self.assertNotIn(foreign["id"][:12], out)
        self.assertIn("project scope: homeproj", out)
        self.assertIn("--all-projects shows them", out)

        # AND THE ESCAPE THE CODE ALREADY HAD: `lr list --all-projects` (the
        # flag task/974 shipped) renders both, the foreign one labeled.
        rc, out, _err = run(["list", "--all-projects"])
        self.assertEqual(rc, 0, out)
        self.assertIn(home["id"][:12], out)
        self.assertIn(foreign["id"][:12], out)
        self.assertIn("FOREIGN — project otherproj owns this row", out)

    def test_dispatch_list_from_each_checkout_scopes_and_names_the_escape(self):
        other, home, foreign = self.two_projects()

        self.chdir(other)
        rc, out, err = self.dispatch_cli(["list"])
        self.assertEqual(rc, 0, err)
        self.assertIn(foreign["id"], out)
        self.assertNotIn(home["id"], out)
        self.assertIn("in project otherproj", out)

        self.chdir(self.repo)
        rc, out, err = self.dispatch_cli(["list"])
        self.assertEqual(rc, 0, err)
        self.assertIn(home["id"], out)
        self.assertNotIn(foreign["id"], out)
        # THE CLAUSE CARRIES ITS SIZE, which is `_narrow`'s own contract: a
        # reader must be able to tell a scoped listing over an empty axis from
        # a scoped listing that set work aside.
        self.assertIn("1 set aside", out)
        self.assertIn("--all-projects", out)

        rc, out, err = self.dispatch_cli(["list", "--all-projects"])
        self.assertEqual(rc, 0, err)
        self.assertIn(home["id"], out)
        self.assertIn(foreign["id"], out)

    def test_an_identity_scoped_listing_is_NEVER_narrowed_by_project(self):  # noqa: VACUOUS_ASSERTION — the assertNotIn IS the control and it is asserted FIRST on the same observable and the same cwd; the unconditional positive is the assertIn on the identity-scoped listing
        """A ROW THAT NAMES YOU IS YOURS WHEREVER ITS CODE LIVES.

        `--mine`/`--issued` is what the resume-turn hook tells every compacted
        seat to run, and hiding one of its rows because the seat is standing in
        another checkout rebuilds task/1007 on a new axis: the reader with the
        least context reads "no matching rows" as "you are free" while holding
        live work. The PAIR is the proof — the same row, the same cwd, two
        listings: withheld without the identity filter, present with it."""
        from tests._tmphome import declaring
        other = self.clone("other-repo")
        self.pin_registry({"homeproj": self.repo, "otherproj": other})
        # ADDRESSED TO THE SEAT THIS PROCESS ALREADY DECLARES. `declaring` does
        # not override an ambient HELM_CHAT_NAME without an explicit `--seat`
        # (its own documented rule), and this fixture declares `integrator` in
        # setUp — so a row addressed to anybody else would measure the identity
        # door rather than the project narrowing.
        mine, why = dispatches.add("integrator", "lane/elsewhere", ref=self.c,
                                   repo=other, new_work=True, kind="review",
                                   deadline_s=60, notify=False, _reason=True)
        self.assertIsNotNone(mine, "%s" % (why,))
        self.chdir(self.repo)

        rc, out, err = self.dispatch_cli(["list"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn(mine["id"], out,
                         "the project narrowing did not run, so the arm below "
                         "proves nothing")

        with declaring(default="integrator"):
            rc, out, err = self.dispatch_cli(["list", "--mine"])
        self.assertEqual(rc, 0, err)
        self.assertIn(mine["id"], out,
                      "a seat standing in another checkout lost its own open "
                      "row: %s%s" % (out, err))


class CliScopeFollowsTheClassifierTest(CwdScopedCliListingsBase):
    """THE CLI NARROWINGS OBEY THE BOARD'S OWN CLASSIFIER (task/2437 round two).

    Round one scoped `helm dispatch list` and `helm dispatch triage` with
    `_real(row["repo_id"]) == _real(scope_repo)` while their clause advertised a
    PROJECT. A project is not a repository: the registry records several paths
    per project (`cv_scope.cwd_prefixes`), so a seat standing in one of a
    project's repositories saw NONE of its sibling repositories' rows under a
    header naming the whole project. Equality also exiled every legacy row,
    whose `repo_id` is absent and therefore matches no scope at all — so the
    oldest obligations in the ledger became the ones a scoped listing could
    never print. `landreq._mark_foreign_rows` already had the policy and the
    four states; these arms pin the CLI to it.

    THIS CLASS INHERITS THE CWD FIXTURE ABOVE rather than rebuilding it — the
    `chdir`, `dispatch_cli` and `two_projects` helpers are the same seat-standing-
    in-a-checkout shape, and a second copy of them is a second fixture.
    """

    def registry_data(self, projects):
        """{name: [paths]} -> the registry document the real resolver reads.

        Split out from the pin so the UNAVAILABLE-lookup helper below can serve
        the SAME document for the reads that succeed — a second literal copy of
        this shape is a second fixture, and the arm needs the successful reads
        to be indistinguishable from the pinned ones."""
        return {"projects": {
            name: {"name": name, "path": os.path.realpath(paths[0]),
                   "cv_scope": {"cwd_prefixes": [os.path.realpath(p)
                                                 for p in paths]}}
            for name, paths in projects.items()}}

    def pin_registry_scoped(self, projects):
        """{name: [paths]} — a project with SEVERAL registered repositories.

        That is what `cv_scope.cwd_prefixes` records and what
        `project_for_cwd` resolves against (the multi-prefix producer
        `inject._ledger` reads). The single-path helper on the base cannot
        express it, and a project with two checkouts is the ORDINARY case the
        equality filter could not see."""
        data = self.registry_data(projects)
        patcher = mock.patch.object(registry, "load", return_value=data)
        patcher.start()
        self.addCleanup(patcher.stop)
        # WHAT THE READ WILL SEE, kept for the helper below: a fixture that
        # re-pins the registry has to be able to put the FIRST pin back, and
        # retyping that mapping in the arm is a second copy of the fixture.
        self.pinned = {name: list(paths) for name, paths in projects.items()}

    def legacy_row(self, rid="b7c8d9e0f1a2b3c4d5e6f70819a2b3c4"):
        """One v1 row through the real event ledger and the real replay, which
        preserves an ABSENT repository. This is the population the equality
        filter silently exiled, and it is the majority shape of the ledger's
        oldest rows."""
        path = dispatches.ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.assertTrue(eventledger.append(path, {
            "v": 1, "id": rid, "seq": 0, "status": "open",
            "ts": "2026-04-01T00:00:00Z", "recipient": "seat-a",
            "lane": "ancient", "tip": self.a, "deadline_s": 600}))
        self.assertIsNone(dispatches.rows()[rid].get("repo_id"),
                          "the legacy fixture carries a repository")
        return rid

    def three_repositories(self):
        """(sibling, stranger, home row, sibling row, stranger row, legacy id).

        One project registering TWO repositories, one other project registering
        a third, and one legacy row belonging to nothing recorded — the four
        states of the classifier, each with a row."""
        sibling, stranger = self.clone("sibling-repo"), self.clone("stranger")
        self.pin_registry_scoped({"homeproj": [self.repo, sibling],
                                  "strangerproj": [stranger]})
        home = self.dispatch(deadline_s=60)
        sib, why = dispatches.add("seat-a", "lane/sibling-work", ref=self.c,
                                  repo=sibling, new_work=True, kind="review",
                                  deadline_s=60, notify=False, _reason=True)
        self.assertIsNotNone(sib, "the sibling repository's row was refused at "
                             "the write door: %s" % (why,))
        far, why = dispatches.add("seat-a", "lane/stranger-work", ref=self.c,
                                  repo=stranger, new_work=True, kind="review",
                                  deadline_s=60, notify=False, _reason=True)
        self.assertIsNotNone(far, "%s" % (why,))
        return sibling, stranger, home, sib, far, self.legacy_row()

    def unregistered_repositorys_row(self):
        """(repo, row) — one row whose repository IS recorded and which no
        registered project claims: `project_unresolved`, the sub-bucket
        `three_repositories` has no row for (round two, finding 2's control
        gap — it seeds local twice, foreign and origin_unknown).

        THE ROW GOES THROUGH THE REAL WRITE DOOR, which REFUSES a repository no
        project claims — so the registry is pinned WITH that repository for the
        write and re-pinned WITHOUT it for the read. That is the ordinary world
        rather than a trick: a checkout registered when the work was filed and
        dropped since, or a registry that cannot be read now, and
        `_project_of` answers None for all of those alike. The pin the READ
        must see is the one already in place when this is called, so no arm
        retypes the mapping it has already pinned."""
        projects = self.pinned
        unclaimed = self.clone("unclaimed-repo")
        self.pin_registry_scoped(dict(projects, goneproj=[unclaimed]))
        row, why = dispatches.add("seat-a", "lane/unclaimed-work", ref=self.c,
                                  repo=unclaimed, new_work=True, kind="review",
                                  deadline_s=60, notify=False, _reason=True)
        self.assertIsNotNone(row, "the write door refused the row this arm's "
                             "sub-bucket is made of: %s" % (why,))
        self.assertEqual(row["repo_id"],
                         dispatches._repo_info(unclaimed)["repo_id"],
                         "the row carries NO repository, so it is the OTHER "
                         "sub-bucket and this fixture proves nothing")
        self.pin_registry_scoped(projects)
        self.assertIsNone(dispatches._project_of(unclaimed),
                          "a registered project still claims the repository, "
                          "so this row classifies as local or foreign")
        return unclaimed, row

    def sections(self, out):
        """(the project rows, the UNKNOWN bucket) — as two strings.

        A SUBSTRING SEARCH OVER THE WHOLE LISTING LOSES THE SECTION, and the
        section IS the claim this lane makes: `assertIn(legacy, out)` passes
        identically whether the row was printed as this project's work or under
        a heading saying nobody can place it. So every arm below asks WHICH side
        of the heading a row landed on.

        THE MARKER MUST BE UNIQUE OR THIS HELPER LIES, which is not
        hypothetical: the first cut of the narrowing clause said "listed under
        its own UNKNOWN PROVENANCE heading", so the phrase appeared in the
        HEADER and every split put the whole table on the UNKNOWN side. A
        duplicate reddens here rather than silently inverting every arm."""
        self.assertLessEqual(
            out.count("UNKNOWN PROVENANCE"), 1,
            "the bucket heading's text also appears elsewhere in the listing, "
            "so this split is meaningless: %s" % out)
        head, marker, tail = out.partition("UNKNOWN PROVENANCE")
        return head, (marker + tail if marker else "")

    def test_dispatch_list_keeps_the_sibling_and_BUCKETS_the_legacy_row(self):  # noqa: VACUOUS_ASSERTION — every absence here is paired with an unconditional positive on the SAME observable and the same read: the row absent from the project section is asserted PRESENT in the unknown section, and the sibling and home rows are asserted present in the project section
        """THE FOUR STATES, from one cwd and one read (task/2468).

        A boolean scope filter keeps the legacy row and files it under the
        reader's project, so a small project's listing reads as a large one: on
        the live ledger a client project's checkout answers ten rows of which
        nine are another repository's July and August work. Keeping the row is
        right; claiming it is not.

        The stranger's absence is the control: without it the arm would pass for
        a filter that never ran.

        AND THE HEADING SAYS WHAT EACH SUB-BUCKET'S ROWS CARRY (round two,
        finding 2), which is why the `project_unresolved` row is minted here:
        one sentence covering both halves said a row records NOTHING about
        where its work lives — false of a row that carries a canonical
        `repo_id` — and promised `helm sync` would place it, which a failed
        registry LOOKUP cannot support."""
        _sib_repo, _stranger, home, sib, far, legacy = self.three_repositories()
        _unclaimed, orphan = self.unregistered_repositorys_row()
        self.chdir(self.repo)
        rc, out, err = self.dispatch_cli(["list"])
        self.assertEqual(rc, 0, err)
        project, unknown = self.sections(out)
        self.assertIn(home["id"], project)
        self.assertIn(sib["id"], project,
                      "a SIBLING repository of this very project was withheld "
                      "by a clause naming the project: %s%s" % (out, err))
        self.assertNotIn(far["id"], out,
                         "another project's row survived the narrowing, so "
                         "this arm proves nothing about the three above")
        self.assertIn("in project homeproj", project)
        # THE ROW IS STILL PRINTED — hiding it is the other half of the bug the
        # round-two cure was written for — and it is printed OUTSIDE the project.
        self.assertNotIn(legacy, project,
                         "a row whose provenance NOTHING in the ledger records "
                         "was filed under the project the reader happens to be "
                         "standing in: %s%s" % (out, err))
        self.assertIn(legacy, unknown,
                      "the unplaceable row was hidden rather than bucketed, "
                      "which loses the oldest obligations in the ledger: "
                      "%s%s" % (out, err))
        # THE SECOND SUB-BUCKET, on the same read: a row that DOES name a
        # repository, which no registered project claims.
        self.assertNotIn(orphan["id"], project, "%s%s" % (out, err))
        self.assertIn(orphan["id"], unknown, "%s%s" % (out, err))
        self.assertIn("2 row(s) NOT counted in project homeproj", unknown)
        # EACH SUB-BUCKET'S OWN SENTENCE, word for word. A shared sentence is
        # what made the heading false about one of the two halves.
        self.assertIn("1 row(s) record NO repository — the row carries no "
                      "`repo_id`", unknown)
        self.assertIn("1 row(s) DO record a repository, and the project "
                      "registry lookup for it came back empty", unknown)
        self.assertIn("what this reports is the failed LOOKUP, not that the "
                      "checkout is unregistered", unknown)
        # AND THE REPAIR PROMISE IS GONE. `_project_of` returns None for an
        # unregistered repository, an unresolvable path AND an unreadable
        # registry alike, so nothing here can promise a sync places the row.
        self.assertNotIn("helm sync", unknown,
                         "the heading still promises a repair a failed lookup "
                         "cannot support: %s" % unknown)

    def test_the_same_ledger_from_another_checkout_still_calls_it_UNKNOWN(self):
        """THE CONTROL FOR THE BUCKET ITSELF: one ledger, two cwds.

        An unplaceable row that moved from "the client project's" to "helm's"
        as the reader walked between checkouts would be the same defect with a
        new owner, and an arm run from ONE directory cannot tell a bucket from
        a relabelling. The pair is the proof: the sibling row swaps sides
        between these two reads (local here, withheld there), and the legacy
        row does not move."""
        _sib_repo, stranger, home, sib, far, legacy = self.three_repositories()
        self.chdir(self.repo)
        rc, out, err = self.dispatch_cli(["list"])
        self.assertEqual(rc, 0, err)
        project, unknown = self.sections(out)
        self.assertIn(sib["id"], project)
        self.assertIn(legacy, unknown)

        self.chdir(stranger)
        rc, out, err = self.dispatch_cli(["list"])
        self.assertEqual(rc, 0, err)
        project, unknown = self.sections(out)
        self.assertIn(far["id"], project,
                      "the second read is not scoped to the second checkout, "
                      "so it says nothing about the bucket: %s%s" % (out, err))
        self.assertNotIn(sib["id"], project,
                         "the project axis did not run from this cwd")
        self.assertNotIn(home["id"], project)
        self.assertNotIn(legacy, project,
                         "the unplaceable row was adopted by the SECOND "
                         "project, so the bucket is a relabelling and not a "
                         "bucket: %s%s" % (out, err))
        self.assertIn(legacy, unknown)
        self.assertIn("NOT counted in project strangerproj", unknown)

    def test_json_carries_the_bucket_as_a_field_from_both_checkouts(self):  # noqa: VACUOUS_ASSERTION — the loop is over a two-element literal tuple, so both iterations always run, and each asserts scope_class EQUAL to a value (local, then origin_unknown) on the same parsed array the absence is read from
        """THE MACHINE READER HAS NO HEADINGS, so the bucket has to be a field
        it can filter — and dropping the row from the array instead would make
        `--json` the one surface where an unplaceable obligation is invisible.

        Read from BOTH checkouts for the same reason the rendering arm is:
        `scope_class` that tracked the reader's cwd would be the original bug
        wearing a field name.

        ITS CONTROL IS THE OTHER TWO ROWS IN THE SAME ASSERTION SET, and it is
        the half of this lane the bucket probe cannot reach: disabling the
        bucket leaves this arm GREEN (the field is computed either way), while
        on trunk `scope_class` does not exist at all and every `assertEqual`
        here reads None. So the three rendering arms prove the BUCKET and this
        one proves the FIELD; neither substitutes for the other.

        BOTH UNPLACEABLE CLASSES ARE IN THE ARRAY (round two, finding 1): the
        stderr disclosure names the classes a machine reader must filter on, and
        it can only name them truthfully if both are actually reachable here."""
        _sib_repo, stranger, home, sib, far, legacy = self.three_repositories()
        _unclaimed, orphan = self.unregistered_repositorys_row()
        # THE WITHHELD COUNT IS PER-CWD and is stated rather than pattern-
        # matched: from home ONE foreign row is absent, from the stranger TWO
        # are — the same two rows that were local a line earlier. A count read
        # off the output could not tell a truthful disclosure from a constant.
        for cwd, label, mine, theirs, withheld in (
                (self.repo, "homeproj", (home["id"], sib["id"]),
                 far["id"], 1),
                (stranger, "strangerproj", (far["id"],), home["id"], 2)):
            self.chdir(cwd)
            rc, out, err = self.dispatch_cli(["list", "--json"])
            self.assertEqual(rc, 0, err)
            classes = {row["id"]: row.get("scope_class")
                       for row in json.loads(out)}
            self.assertNotIn(theirs, classes,
                             "another project's row is in the array, so this "
                             "arm proves nothing about the classes below")
            for rid in mine:
                self.assertEqual(classes.get(rid), "local", err)
            self.assertEqual(
                classes.get(legacy), "origin_unknown",
                "the unplaceable row is missing from the array or wears the "
                "reader's project, read from %s: %s" % (cwd, err))
            self.assertEqual(
                classes.get(orphan["id"]), "project_unresolved",
                "a row whose repository no registered project claims is "
                "missing from the array or wears the reader's project, read "
                "from %s: %s" % (cwd, err))
            # THE DISCLOSURE SAYS RETAINED ABOUT EXACTLY THESE TWO, and names
            # both classes so the reader can spell the filter that excludes
            # them. A foreign row IS withheld from this read, so the WITHHELD
            # half is the control right here: the two sentences carry different
            # counts over the same call, which is what the single sentence they
            # replace could not do.
            # THE WORDING IS THE QUALIFIED ONE (round three): what this
            # surface measured is a LOOKUP that failed or came back empty, and
            # `--json` has no heading anywhere to walk that back.
            self.assertIn("2 row(s) this registry could not place are RETAINED "
                          "in it, stamped `scope_class` origin_unknown / "
                          "project_unresolved", err)
            self.assertIn("so it is NOT counted among project %s's rows, and "
                          "nothing here proves the row is in NO project"
                          % label, err)
            self.assertNotIn("no registered project places", err)
            self.assertIn("%d row(s) in a repository another project claims "
                          "are WITHHELD from it" % withheld, err)
            self.assertEqual(err.count("WITHHELD"), 1,
                             "the array's RETAINED rows are being called "
                             "withheld as well: %r" % (err,))

    def test_json_never_calls_a_RETAINED_row_withheld(self):  # noqa: VACUOUS_ASSERTION — the absence of "WITHHELD" is measured against the CONTROL in the same arm, which asserts the same word PRESENT on the same command and the same cwd once one foreign row exists
        """A ROW THIS ARRAY IS HOLDING IS NOT A ROW IT WITHHELD (round two,
        finding 1).

        THE POPULATION IS THE POINT: one local row and one `origin_unknown`
        row, NO foreign row. The project narrowing drops zero, `--json` returns
        both rows, and the first cut nevertheless told the machine reader "NOT
        the whole ledger" and pointed it at `--all-projects` — the escape for
        rows it was already holding. The array here IS the whole ledger.

        THE CONTROL IS THE SECOND HALF, on the same command and the same cwd
        with ONE fact changed: a foreign row now exists, and then the withheld
        sentence MUST appear — otherwise this arm would pass for a cure that
        simply deleted the disclosure.

        BLAST RADIUS: both rows are minted into this test's own tmp ledger (the
        legacy one through the real event ledger, the foreign one through the
        real write door) and every registry pin is per-test."""
        self.pin_registry_scoped({"homeproj": [self.repo]})
        home = self.dispatch(deadline_s=60)
        legacy = self.legacy_row()
        self.chdir(self.repo)
        rc, out, err = self.dispatch_cli(["list", "--json"])
        self.assertEqual(rc, 0, err)
        ids = [row["id"] for row in json.loads(out)]
        self.assertEqual(sorted(ids), sorted([home["id"], legacy]),
                         "the array is not the whole ledger of this fixture, "
                         "so there is nothing for the RETAINED claim to be "
                         "about: %r" % (ids,))
        self.assertIn("1 row(s) this registry could not place are RETAINED in "
                      "it, stamped `scope_class` origin_unknown", err)
        self.assertIn("nothing here proves the row is in NO project", err)
        self.assertNotIn("no registered project places", err)
        self.assertNotIn("WITHHELD", err,
                         "a row the array RETURNED was disclosed as withheld: "
                         "%r" % (err,))
        self.assertNotIn("NOT the whole ledger", err,
                         "the array holds every row in the ledger and the "
                         "disclosure says otherwise: %r" % (err,))
        # AND THE CLAUSE NAMES THIS SURFACE'S OWN DESTINATION: `--json` has no
        # headings, so "its own bucket below" would be a place the reader
        # cannot look.
        self.assertIn("RETAINED in this array under its own `scope_class`", err)
        self.assertNotIn("bucket below", err)

        # THE CONTROL.
        other = self.clone("other-repo")
        self.pin_registry_scoped({"homeproj": [self.repo],
                                  "otherproj": [other]})
        foreign, why = dispatches.add(
            "seat-a", "lane/other-project-work", ref=self.c, repo=other,
            new_work=True, kind="review", deadline_s=60, notify=False,
            _reason=True)
        self.assertIsNotNone(foreign, "%s" % (why,))
        rc, out, err = self.dispatch_cli(["list", "--json"])
        self.assertEqual(rc, 0, err)
        ids = [row["id"] for row in json.loads(out)]
        self.assertNotIn(foreign["id"], ids,
                         "the project narrowing did not run, so the withheld "
                         "sentence below would prove nothing")
        self.assertIn("1 row(s) in a repository another project claims are "
                      "WITHHELD from it, so it is NOT the whole ledger", err)
        self.assertIn("1 row(s) this registry could not place are RETAINED in "
                      "it", err)

    def make_lookup_unavailable(self, repo_id):
        """Make the ORDINARY registry read fail for exactly ONE repository's
        lookup — the one `project_for_cwd` performs for `repo_id` — and answer
        normally for every other, including the caller-scope resolution
        `cwd_scope` does first. Returns the list of cwds the resolver asked
        about, so an arm can prove the failing lookup actually happened.

        THE KEY IS THE ROW'S OWN `repo_id`, which `_repo_info` stamps as the
        canonical GIT COMMON DIR rather than the worktree path — that is the
        string `_scope_class` hands the resolver, and keying on the checkout
        directory instead would never match.

        THE FAILURE PATH IS THE PRODUCTION ONE. Only the FILE read is patched:
        `registry.load` raises `OSError`, `project_for_cwd(strict=False)`
        swallows it to None (its documented fail-open), and `_scope_class` then
        sees the very same None a genuinely unregistered repository produces.
        The classifier, the resolver and both renderers run exactly as shipped —
        patching any of them would assert about the patch.

        WHY ONE READ CAN SUCCEED AND THE NEXT FAIL: `registry.load` reacquires
        its lock and re-reads the file on every call, so a listing has no
        registry SNAPSHOT. `cwd_scope` resolves the caller's project through one
        read and each per-row classification is another — the window this arm
        stands in is the window the live code has.
        """
        target = dispatches._real(repo_id)
        self.assertIsNotNone(target, "the fixture repository id does not "
                                     "resolve, so the keyed failure below can "
                                     "never fire")
        data = self.registry_data(self.pinned)
        asked = []

        def load(*_a, **_kw):
            # WHICH LOOKUP IS THIS? `project_for_cwd` holds the cwd it is
            # resolving in its own frame; every other caller of `registry.load`
            # has no such local and gets the ordinary document.
            cwd = sys._getframe(1).f_locals.get("cwd")
            if cwd is not None:
                asked.append(dispatches._real(cwd))
            if cwd is not None and dispatches._real(cwd) == target:
                raise OSError("registry unavailable for this read")
            return data

        # A PLAIN FUNCTION, NEVER A MOCK'S `side_effect`: the mock wrapper
        # becomes the calling frame, so `_getframe(1)` would read mock's own
        # locals and every lookup would succeed — measured, and it made the
        # keyed failure silently never fire.
        patcher = mock.patch.object(registry, "load", new=load)
        patcher.start()
        self.addCleanup(patcher.stop)
        return asked

    def test_an_unavailable_lookup_is_never_rendered_as_proof_of_no_project(self):  # noqa: VACUOUS_ASSERTION — every assertNotIn here is paired with an unconditional positive on the SAME string family and the same read: the qualified sentence is asserted PRESENT in the same stdout/stderr the asserting wordings are asserted absent from
        """A LOOKUP THAT WAS UNAVAILABLE IS NOT A PROJECT THAT DOES NOT EXIST
        (round three).

        Round two qualified the UNKNOWN heading and left two sentences about the
        same rows asserting the opposite — the list narrowing clause ("a row
        naming no repository this registry can place is in NO project") and
        `--json`'s accounting ("row(s) no registered project places"), the
        second on a surface with no heading to contradict it. This arm stands in
        the window that makes both false: a REGISTERED sibling repository whose
        ordinary lookup is unavailable at classification time.

        THE POSITIVE IS THE FIRST READ, through the shipped CLI on the shipped
        producers: while the registry answers, the sibling's row is `local` and
        prints inside the project section. ONE FACT then changes.

        THE CONTROL THAT THE ARM BROKE THE *LATER* LOOKUP, not the caller's: the
        sibling row can only reach the bucket when `scope_project` RESOLVED —
        with no project `_unplaceable_classes` holds `origin_unknown` alone and
        `project_unresolved` stays in the listing. The stranger project's row
        being withheld is the same fact from the other side. And `asked` proves
        the failing lookup was really attempted rather than silently skipped.

        THE CONTROL THAT IT WOULD GO RED WITHOUT THE CURE: the two asserting
        wordings are asserted ABSENT from the whole text stdout and the whole
        JSON stderr, and on the pre-cure tree both are emitted by the narrowing
        clause on every listing that buckets a row — which is every read in this
        arm. BLAST RADIUS: those strings exist nowhere else in the tree, so
        these assertions can only fail at the clause under test; the ledger rows
        and every registry pin are this test's own.

        THE OTHER TWO POLES STAY: the genuinely unregistered repository's row
        and the legacy no-repository row keep their classes throughout, so the
        cure cannot be a blanket relabelling of the bucket.
        """
        _sibling, _stranger, home, sib, far, legacy = self.three_repositories()
        _unclaimed, orphan = self.unregistered_repositorys_row()
        self.chdir(self.repo)

        rc, out, err = self.dispatch_cli(["list"])
        self.assertEqual(rc, 0, err)
        project, unknown = self.sections(out)
        self.assertIn(sib["id"], project,
                      "the registered sibling's row is not local while the "
                      "registry answers, so the read below has no changed "
                      "fact to be about: %s%s" % (out, err))
        self.assertNotIn(sib["id"], unknown)

        asked = self.make_lookup_unavailable(sib["repo_id"])
        rc, out, err = self.dispatch_cli(["list"])
        self.assertEqual(rc, 0, err)
        self.assertIn(dispatches._real(sib["repo_id"]), asked,
                      "the resolver never asked about the sibling repository, "
                      "so nothing in this arm drove the unavailable lookup")
        project, unknown = self.sections(out)
        self.assertIn(home["id"], project, "%s%s" % (out, err))
        self.assertNotIn(far["id"], out,
                         "the stranger project's row is not withheld, so the "
                         "project axis did not resolve and the bucket below "
                         "proves nothing: %s%s" % (out, err))
        self.assertIn(sib["id"], unknown,
                      "the row whose lookup was unavailable was counted as "
                      "this project's own: %s%s" % (out, err))
        self.assertNotIn(sib["id"], project)
        self.assertIn(legacy, unknown)
        self.assertIn(orphan["id"], unknown)
        # WHAT THE WHOLE LISTING SAYS ABOUT IT — read over the ENTIRE stdout,
        # because the defect was a clause ABOVE the heading, not inside it.
        self.assertIn("nothing here proves the row is in NO project", out)
        self.assertNotIn("can place is in NO project", out,
                         "the listing asserts non-membership from a lookup it "
                         "could not perform: %s" % out)
        self.assertNotIn("no registered project places", out)

        rc, out, err = self.dispatch_cli(["list", "--json"])
        self.assertEqual(rc, 0, err)
        rows = {row["id"]: row for row in json.loads(out)}
        self.assertEqual(rows.get(sib["id"], {}).get("scope_class"),
                         "project_unresolved",
                         "the machine surface lost the row or wore the "
                         "reader's project for it: %s" % err)
        self.assertEqual(rows[sib["id"]].get("repo_id"), sib["repo_id"],
                         "the known repository was erased along with the "
                         "failed lookup")
        self.assertIn("nothing here proves the row is in NO project", err)
        self.assertNotIn("can place is in NO project", err,
                         "`--json` has no heading to qualify this and asserts "
                         "non-membership anyway: %s" % err)
        self.assertNotIn("no registered project places", err)

    def test_dispatch_triage_keeps_the_sibling_and_BUCKETS_the_legacy_row(self):
        """THE SAME POLICY ON THE PICKUP SURFACE. `triage` buckets its BULK
        listing with the same question, so the two verbs cannot answer
        differently about one directory — and the unplaceable row stays
        PICKABLE, because a row nobody can place is still owed by the seat it
        names.

        BOTH SUB-BUCKETS ARE SEEDED HERE TOO (round two, finding 2): the two
        verbs share `_unplaceable_heading`, and an arm that reads only one
        surface cannot tell a shared producer from two that agree today."""
        _sib_repo, _stranger, home, sib, far, legacy = self.three_repositories()
        _unclaimed, orphan = self.unregistered_repositorys_row()
        for row in (home, sib, far, orphan):
            dispatches._mark_delivered(row["id"], "post-" + row["id"][:4])
        self.chdir(self.repo)
        rc, out, err = self.dispatch_cli(["triage"])
        self.assertEqual(rc, 0, err)
        project, unknown = self.sections(out)
        self.assertIn(home["id"][:12], project)
        self.assertIn(sib["id"][:12], project, "%s%s" % (out, err))
        self.assertNotIn(far["id"][:12], out)
        self.assertNotIn(legacy[:12], project, "%s%s" % (out, err))
        self.assertIn(legacy[:12], unknown, "%s%s" % (out, err))
        self.assertNotIn(orphan["id"][:12], project, "%s%s" % (out, err))
        self.assertIn(orphan["id"][:12], unknown, "%s%s" % (out, err))
        self.assertIn("2 row(s) NOT counted in project homeproj", unknown)
        self.assertIn("1 row(s) record NO repository — the row carries no "
                      "`repo_id`", unknown)
        self.assertIn("1 row(s) DO record a repository, and the project "
                      "registry lookup for it came back empty", unknown)
        self.assertNotIn("helm sync", unknown,
                         "the pickup surface still promises a repair a failed "
                         "lookup cannot support: %s" % unknown)
        # THE FOREIGN SET-ASIDE COUNTS ONLY THE FOREIGN ROW. Counting the
        # unplaceable one here would tell the reader to go run triage from "that
        # project's checkout" about a row that has no checkout to name.
        self.assertIn("1 row outside project homeproj set aside", err)

    def test_to_SEAT_is_never_narrowed_by_project(self):  # noqa: VACUOUS_ASSERTION — the withheld half IS the control and runs FIRST on the same observable and cwd; the unconditional positive is the row appearing in the `--to` listing
        """`--to SEAT` ASKS AN IDENTITY QUESTION (finding 3) — "what does that
        seat owe" — and the answer must not depend on which checkout the asker
        stands in. The coupling that makes this load-bearing rather than tidy:
        the `--mine` identity REFUSAL recommends `--to SEAT` as its recovery, so
        the one flag the shipped text points a stuck seat at was the one the
        narrowing hid work behind.

        THE PAIR IS THE PROOF: the same row, the same cwd, two listings."""
        other, _home, foreign = self.two_projects()
        self.chdir(self.repo)
        rc, out, err = self.dispatch_cli(["list"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn(foreign["id"], out,
                         "the project narrowing did not run, so the arm below "
                         "proves nothing")
        rc, out, err = self.dispatch_cli(["list", "--to", "seat-a"])
        self.assertEqual(rc, 0, err)
        self.assertIn(foreign["id"], out,
                      "asking what a NAMED seat owes lost a row because the "
                      "asker was standing elsewhere: %s%s" % (out, err))
        self.assertIn("addressed to @seat-a", out)
        self.assertNotIn("in project", out,
                         "the identity-scoped listing still reports a project "
                         "narrowing it no longer performs")

    def test_json_discloses_the_narrowing_and_keeps_the_array(self):
        """THE MACHINE READER IS THE ONE THAT CANNOT SEE THE SCOPE (finding 4):
        the `--json` return sits ABOVE both surfaces that print the clause, so
        the array holds one project's rows while nothing in the output says so. The array stays byte-compatible — the disclosure is on stderr
        — and it names the escapes, because a narrowing a reader cannot widen is
        the same dead end the write door's refusal was."""
        _other, home, foreign = self.two_projects()
        self.chdir(self.repo)
        rc, out, err = self.dispatch_cli(["list", "--json"])
        self.assertEqual(rc, 0, err)
        ids = [row["id"] for row in json.loads(out)]
        self.assertIn(home["id"], ids)
        self.assertNotIn(foreign["id"], ids,
                         "the JSON was not narrowed at all, so there is nothing "
                         "for the disclosure to be about")
        self.assertIn("in project homeproj", err)
        self.assertIn("--all-projects", err)
        self.assertIn("--mine", err)
        rc, out, err = self.dispatch_cli(["list", "--json", "--all-projects"])
        self.assertEqual(rc, 0, err)
        ids = [row["id"] for row in json.loads(out)]
        self.assertIn(foreign["id"], ids,
                      "the escape the disclosure names does not work")

    def test_the_cure_census_is_scoped_and_its_set_aside_is_counted(self):  # noqa: VACUOUS_ASSERTION — the two-candidate pool and the exact set-aside line are unconditional positives on the same observable, asserted before the escape
        """THE CURE CENSUS ESCAPED THE SCOPE ITS OWN VERB HAD JUST DECLARED
        (finding 6). `triage` narrowed the owed frontier to this project and then
        handed the WHOLE snapshot to `cured_by_repo`, whose default population is
        every eligible FIX row — so the surface offered another project's cures
        back under the same header, with no line saying how many or how to see
        them all.

        THE FIX ROW IS MINTED BY THE REAL VERDICT PRODUCER in the foreign
        repository, and the observable is the ELIGIBLE population: the local
        candidate is measured, the foreign one is counted as set aside and named
        with its escape, and `--all-projects` measures it here."""
        other, _home, foreign = self.two_projects()
        local = self.dispatch(lane="lane/local-fix", deadline_s=60)
        dispatches._mark_delivered(local["id"], "post-l")
        row, err = self.mark_verdict(local["id"], self.side, "reviewed-local",
                                     polarity="fix")
        self.assertIsNone(err, err)
        with dispatch_home(other):
            row, err = self.mark_verdict(foreign["id"], self.c,
                                         "reviewed-foreign", polarity="fix")
        self.assertIsNone(err, err)
        snap = dispatches.rows()
        pool = [r["id"] for r in dispatches.cure_eligible(snap)]
        self.assertEqual(sorted(pool), sorted([local["id"], foreign["id"]]),
                         "the fixture did not produce two cure candidates, so "
                         "a set-aside count of one would mean nothing")

        self.chdir(self.repo)
        rc, out, err = self.dispatch_cli(["triage"])
        self.assertEqual(rc, 0, err)
        self.assertIn("1 cure candidate outside project homeproj not measured",
                      err)
        self.assertIn("--all-projects", err)
        rc, out, err = self.dispatch_cli(["triage", "--all-projects"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("cure candidate outside project", err,
                         "the escape still set a candidate aside")


class LrExpiredScopesItsWarmPopulationTest(ProjectScopeBase):
    """`lr expired` TOOK THE WARM BODY UNCONDITIONALLY (task/2437 round two,
    finding 5).

    `lr list` already knew the rule one verb over: the warm projection is built
    for the running `helm web`'s scope, anchored to the helm PACKAGE, so a caller
    standing in another project's checkout is asking a question that body does
    not answer. This census took those rows anyway while its COLD path went
    through `_cli_scope` — so one command reported a population from helm's board
    in one process and from the caller's project in another, and with `--apply`
    it is a population that gets CLOSED. The two halves also disagreed inside one
    run: the warm ROWS came from helm while the trunk observation comes from the
    caller's own cwd, so helm's rows were measured against another project's
    trunk.
    """

    def test_expired_from_a_foreign_checkout_falls_through_to_the_cold_replay(self):
        """THE PAIR ON ONE WARM BODY: from the HOME checkout the census still
        reports `warm` (the must-hit — a guard that refused the accelerator
        everywhere would pay the cold replay's 18s for nothing), and from another
        project's checkout it reports `cold` and says why."""
        other = self.clone("other-repo")
        self.pin_registry({"homeproj": self.repo, "otherproj": other})
        # THE CENSUS RESOLVES `origin/main` IN THE CWD'S OWN REPOSITORY (it
        # hardcodes that trunk), so BOTH checkouts need one: this repo gets a
        # bare upstream and the clone's `origin` is this repo, which needs the
        # branch under that exact name whatever `git init` called HEAD here.
        self.add_origin()
        self.git("push", "-q", "origin", "%s:main" % self.main)
        self.git("branch", "-f", "main", self.main)
        self.git("fetch", "-q", "origin", cwd=other)
        home = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(home["id"], "post-h")
        warm = {"rows": list(dispatches.rows().values()), "withheld": {}}
        patcher = mock.patch.object(landreq, "warm_lr_body",
                                    return_value=(warm, None))
        patcher.start()
        self.addCleanup(patcher.stop)

        self.chdir(self.repo)
        rc, out, err = run(["expired"])
        self.assertIn("[warm projection]", out,
                      "the accelerator stopped serving the checkout it is "
                      "built for: %s%s" % (out, err))

        self.chdir(other)
        rc, out, err = run(["expired"])
        self.assertIn("cold projection", out,
                      "another project's checkout was served helm's warm "
                      "population: %s%s" % (out, err))
        self.assertIn("cold replay", err)
        self.assertIn("otherproj", err)


class _TeardownDeletesTheTree(unittest.TestCase):
    """The REAL teardown ordering, with nothing else in it.

    `unittest` runs `tearDown()` and only THEN `doCleanups()`, and
    `LandReqBase.tearDown` removes `self.tmp` — that ordering is the whole
    subject, so this fixture reproduces exactly it and pays no git init.

    ITS METHOD IS NOT NAMED `test_*` OR `runTest`, on purpose: discovery
    collects every `TestCase` subclass in a module, and `loadTestsFromTestCase`
    falls back to `runTest` when it finds no `test_*` method — so either
    spelling would enrol this fixture, and the CONTROL below it, as real suite
    members and the control's whole point is to error.
    """

    # THE HELPER UNDER TEST IS THE SHIPPED ONE, taken off the base class by
    # reference rather than retyped: an arm holding its own copy of a fixture
    # helper measures the copy.
    chdir = ProjectScopeBase.chdir

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-cwd-")
        self.repo = os.path.join(self.tmp, "repo")
        self.other = os.path.join(self.tmp, "other-repo")
        os.makedirs(self.repo)
        os.makedirs(self.other)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def two_hops(self):
        self.chdir(self.other)
        self.assertEqual(os.path.realpath(os.getcwd()),
                         os.path.realpath(self.other))
        self.chdir(self.repo)
        self.assertEqual(os.path.realpath(os.getcwd()),
                         os.path.realpath(self.repo))


class _PerHopRestore(_TeardownDeletesTheTree):
    """THE CONTROL, and the only thing it changes is the helper.

    BLAST RADIUS: this class is private to this module, is never collected by
    discovery (its name does not start with `Test` and it defines no `test_*`
    method), and is instantiated only by the arm below — which runs it into a
    `TestResult` of its own. It touches no shared state beyond its own
    `mkdtemp`, so a red here can implicate nothing else.
    """

    def chdir(self, where):
        prior = os.getcwd()
        self.addCleanup(os.chdir, prior)
        os.chdir(where)


class CwdRestoreOutlivesTheFixtureTest(unittest.TestCase):
    """A CWD RESTORE REGISTERED INSIDE THE FIXTURE'S TREE DIES WITH IT
    (task/2437 round four).

    Five arms in this module — every one that stood in two checkouts — errored
    with a bare `FileNotFoundError` naming a fixture directory and NO
    traceback, because the second hop's `addCleanup(os.chdir, prior)` pointed
    back into `self.tmp` and `tearDown` had already removed it. The missing
    traceback is why this went unread for three rounds: the raising frame is
    `unittest.case`'s own call of the cleanup and `case.py` sets
    `__unittest = True`, so the frame filter left an exception with no origin.
    """

    def _run(self, case):
        result = unittest.TestResult()
        case.run(result)
        return result

    def test_two_hops_restore_cleanly_and_the_per_hop_restore_does_not(self):  # noqa: VACUOUS_ASSERTION — the control runs FIRST on the same observable and asserts exactly one error carrying FileNotFoundError, so the cured run's empty errors list is measured against a result that can hold one
        entry = os.getcwd()
        self.addCleanup(os.chdir, entry)

        # THE CONTROL FIRST, on the same observable and the same helper seam:
        # the pre-cure restore errors in cleanup, so this arm is measuring a
        # failure mode that a fixture teardown can actually produce.
        broken = self._run(_PerHopRestore("two_hops"))
        self.assertEqual(len(broken.errors), 1,
                         "the pre-cure helper cleaned up without error, so "
                         "this arm proves nothing about the cure: %r"
                         % (broken.errors,))
        self.assertIn("FileNotFoundError", broken.errors[0][1])
        self.assertEqual(broken.failures, [], "%r" % (broken.failures,))
        os.chdir(entry)

        # AND THE CURED HELPER, which the whole module now shares.
        cured = self._run(_TeardownDeletesTheTree("two_hops"))
        self.assertEqual(cured.errors, [], "%r" % (cured.errors,))
        self.assertEqual(cured.failures, [], "%r" % (cured.failures,))
        self.assertEqual(os.path.realpath(os.getcwd()),
                         os.path.realpath(entry),
                         "the cured helper left the process standing "
                         "somewhere else")


class JsonDisclosureFollowsTheWITHHOLDINGTest(ProjectScopeBase):
    """`--json` DISCLOSES A NARROWING, NOT THE EXISTENCE OF A SCOPE
    (task/2437 round four).

    Round two printed the scope paragraph to stderr whenever ANY clause had
    been recorded, and the project clause is recorded whether it set aside one
    row or none — so `helm dispatch list --json` run from inside any Git
    checkout wrote a paragraph to a channel whose documented content is empty,
    and two trunk arms asserting `(0, "")` on exactly that call went red. The
    disclosure explains rows MISSING from the array; with nothing withheld
    there is nothing to explain.
    """

    # THE SHIPPED CLI HARNESS, by reference to the fixture base that holds it.
    dispatch_cli = CwdScopedCliListingsBase.dispatch_cli

    def test_json_stderr_is_empty_until_the_project_axis_withholds_a_row(self):  # noqa: VACUOUS_ASSERTION — the must-hit half asserts the disclosure PRESENT on the same stderr, the same command and the same cwd with one row withheld
        # THE UNSCOPED WORLD FIRST: one project, its own row, a real checkout.
        # Nothing is set aside, so nothing is disclosed.
        self.pin_registry({"homeproj": self.repo})
        home = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(home["id"], "post-h")
        self.chdir(self.repo)
        rc, out, err = self.dispatch_cli(["list", "--json"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(err, "",
                         "a listing that withheld nothing still wrote to the "
                         "channel a script reads errors on: %r" % (err,))
        self.assertIn(home["id"], [row["id"] for row in json.loads(out)],
                      "the fixture's own row is missing, so an empty stderr "
                      "says nothing about the disclosure")

        # THE MUST-HIT CONTROL, on the SAME command and the same cwd, with one
        # fact changed: a second registered project's row now exists and the
        # scope holds it back. The disclosure MUST appear, or this cure has
        # deleted the finding-4 cure instead of bounding it.
        #
        # BLAST RADIUS: the foreign row is minted through the real write door
        # into this test's own tmp ledger, and the registry pin is per-test.
        other = self.clone("other-repo")
        self.pin_registry({"homeproj": self.repo, "otherproj": other})
        foreign, why = dispatches.add(
            "seat-a", "lane/other-project-work", ref=self.c, repo=other,
            new_work=True, kind="review", deadline_s=60, notify=False,
            _reason=True)
        self.assertIsNotNone(foreign, "%s" % (why,))
        dispatches._mark_delivered(foreign["id"], "post-f")
        rc, out, err = self.dispatch_cli(["list", "--json"])
        self.assertEqual(rc, 0, err)
        ids = [row["id"] for row in json.loads(out)]
        self.assertNotIn(foreign["id"], ids,
                         "the project narrowing did not run, so there is "
                         "nothing for the disclosure to be about")
        self.assertIn("in project homeproj", err,
                      "a row WAS withheld and the machine reader was not "
                      "told: %r" % (err,))
        self.assertIn("--all-projects", err)


class ScopeMarksNeverWriteTheFoldTest(ProjectScopeBase):
    """THE SCOPE VERDICT GOES ON A COPY, NEVER ON THE LEDGER FOLD'S OWN ROW.

    `_mark_foreign_rows` decides `foreign` / `foreign_repo` / `foreign_project`
    / `project_unresolved` / `origin_unknown` over the rows it is handed, and
    those rows are `project_raw`'s fold — the same objects it returns as its
    RAW snapshot. A classifier that writes them in place therefore writes the
    fold. Measured through the real caller on a frozen copy of the live ledger:
    of 3397 eligible rows, 118 take a mark, and with the marks written in place
    all 118 reach the returned raw snapshot.

    IT WAS LATENT ONLY BECAUSE EVERY READER PAID FOR ITS OWN FOLD. A shared
    fold is what a write-maintained snapshot IS, and these marks are the one
    annotation on this projection that is not a property of the row: they
    answer whose project is ASKING. One reader's answer reaching a second
    reader's rows is not a crash — it is a board rendering another project's
    scope verdict with exactly the confidence it renders its own, which is the
    single failure the whole scope axis exists to prevent.

    SO BOTH ARMS ASSERT ON OBJECTS, never on a rendering. The first says a
    scoped projection leaves the fold's own rows as it found them; the second
    says two scopes over ONE rows mapping cannot see each other's verdicts,
    which is the property the shared fold will stand on.
    """

    def two_projects(self):
        """Two registered projects, one delivered row in each, both minted
        through the real write door — the shape task/2437 admits."""
        other = self.clone("other-repo")
        self.pin_registry({"homeproj": self.repo, "otherproj": other})
        home = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(home["id"], "post-h")
        foreign, why = dispatches.add(
            "seat-a", "lane/other-project-work", ref=self.c, repo=other,
            new_work=True, kind="review", deadline_s=60, notify=False,
            _reason=True)
        self.assertIsNotNone(
            foreign, "the registered project's row was refused at the write "
            "door, so there is no foreign row to mark: %s" % (why,))
        dispatches._mark_delivered(foreign["id"], "post-f")
        return other, home, foreign

    def test_a_scoped_projection_leaves_the_folds_own_rows_as_it_found_them(self):  # noqa: VACUOUS_ASSERTION — the unchanged-fold comparison is preceded in this method by two unconditional assertions that the same projection DID mark the foreign row and DID name its project, so a classifier that stopped classifying fails here first
        other, home, foreign = self.two_projects()
        fold = {}
        real_read = dispatches.snapshot_and_events

        def capture():
            out = real_read()
            # The fold's rows AND a copy of each taken BEFORE any annotator
            # runs. The comparison is then derived from what the fold actually
            # held, so a mark this arm has never heard of still fails it.
            fold["rows"] = out[0]
            fold["before"] = {rid: dict(row) for rid, row in out[0].items()}
            return out

        with mock.patch.object(dispatches, "snapshot_and_events", capture):
            lrs, raw, unavailable = landreq.project_raw(
                time.time(), scope=landreq.board_scope())
        self.assertIsNone(unavailable)

        # THE MUST-HIT CONTROL, unconditional and on the same fields: the
        # projection still marks the foreign row and still names its owner.
        # Without it the comparison below passes on a classifier that stopped
        # classifying, which is the one way this arm could be worth nothing.
        self.assertTrue(lrs[foreign["id"]]["foreign"],
                        "nothing was marked at all, so the fold being "
                        "unwritten says nothing")
        self.assertEqual(lrs[foreign["id"]]["foreign_project"], "otherproj")

        # AND THE FOLD'S OWN OBJECTS ARE WHAT IS ASSERTED ON. `raw` IS that
        # mapping — the rows a shared fold would hand to the next reader.
        self.assertIs(raw, fold["rows"],
                      "the capture did not hold the mapping project_raw "
                      "returns, so this arm measured some other fold")
        for rid in (foreign["id"], home["id"]):
            self.assertEqual(
                fold["rows"][rid], fold["before"][rid],
                "the projection wrote on the fold's own row %s; a reader "
                "sharing this fold would inherit this board's scope" % rid[:12])

    def test_two_scopes_over_one_fold_do_not_see_each_others_verdicts(self):  # noqa: VACUOUS_ASSERTION — every absence here is on a key this method also asserts PRESENT, on the same mapping and the same two rows, for the reader that owns that verdict
        other, home, foreign = self.two_projects()

        # BOTH SCOPES COME FROM THE REAL DOOR, one per checkout, because a
        # hand-built scope dict would test whatever shape this arm imagined.
        self.chdir(self.repo)
        home_scope = landreq._cli_scope()
        self.chdir(other)
        other_scope = landreq._cli_scope()
        self.assertEqual((home_scope["project"], other_scope["project"]),
                         ("homeproj", "otherproj"))

        # ONE FOLD, TWO READERS — the shape a write-maintained snapshot has.
        rows, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        mine = landreq._mark_foreign_rows(rows, home_scope)
        theirs = landreq._mark_foreign_rows(rows, other_scope)

        # EACH READER'S OWN VERDICT, unconditionally: from homeproj the other
        # project's row is foreign, and from otherproj this project's is.
        self.assertEqual(mine[foreign["id"]]["foreign_project"], "otherproj")
        self.assertEqual(theirs[home["id"]]["foreign_project"], "homeproj")

        # AND NEITHER READER INHERITS THE OTHER'S ANSWER. Same key, same rows,
        # one shared mapping underneath both.
        self.assertNotIn("foreign", theirs[foreign["id"]],
                         "the otherproj reader is carrying the homeproj "
                         "reader's verdict about its own row")
        self.assertNotIn("foreign", mine[home["id"]],
                         "the homeproj reader is carrying the otherproj "
                         "reader's verdict about its own row")
        self.assertNotIn("foreign", rows[foreign["id"]],
                         "a scope verdict was written onto the shared fold")
        self.assertNotIn("foreign", rows[home["id"]],
                         "a scope verdict was written onto the shared fold")


def setUpModule():
    """No dispatch row this module writes walks the host's process table
    (task/3039; see tests._tmphome.pin_live_seats)."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()


if __name__ == "__main__":
    unittest.main()
