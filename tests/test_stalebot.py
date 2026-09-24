#!/usr/bin/env python3
"""stalebot tests — the housekeeping sweep that PROPOSES terminals (task/445).

Every classifier arm here is DISCRIMINATING by construction: the fixture repo
lands one change by cherry-pick (new object, same patch-id), keeps one change
on an unmerged branch, and rots one file:line anchor after the filing era —
so supersede must name a CARRIER that differs from the reviewed tip, cancel
must require both the rot and the absence, and keep must survive every
unmeasurable input.

THE FALSIFY LIST — each classifier assumption and the arm that makes it false:
  A1 supersede assumes content identity is measurable from the row
      -> a BUILD row's ref (an ancestor base!) never reads supersede
  A2 supersede assumes the sha it names is trunk's object, not the reviewed one
      -> the cherry-pick arm asserts carrier != reviewed tip
  A3 cancel assumes rotted claims mean moot
      -> rotted claims + landed content reads SUPERSEDE, not cancel;
         rotted-ish claims + unmeasurable landing reads KEEP
  A4 retip assumes base_behind is a measurement
      -> base_behind None (UNMEASURED) never retips
  A5 retip assumes a drifted base means the work is still wanted here
      -> landed content outranks any drift
  A6 task-supersede assumes all-cited-on-trunk means carried
      -> one landed sha beside one absent (in-flight work) never supersedes
  A7 task-reanchor assumes every anchor rotted means the work is moot
      -> all-rotted proposes re-anchoring with no close door; partially-fresh
         evidence keeps
  A8 the age gate assumes the untouched clock is the aging fact
      -> a fresh row is not walked while its aged sibling is (must-hit seeded)
  A9 the latch assumes the same ask twice is noise but a changed ask is news
      -> same disposition re-latches; a changed disposition re-proposes immediately
  A10 the latch assumes only a DELIVERED ask spends the budget
      -> a failed post never latches; the next sweep re-covers the window
  A11 the cured bucket assumes membership is cured_unwitnessed's answer
      -> the patched classifier's answer is mirrored verbatim (call proven by
         call-count and kwargs), and against the real classifier an OPEN row
         and an APPROVE-verdicted row never enter while the fixture cure does
  A12 the redispatch proposal assumes the author can be asked
      -> WALLED, unknown-wall, absent and ambiguous authors ride the integrator;
         only one current rostered actor is addressed directly
  A13 the attention budget holds for cured rows
      -> the same redispatch within 3d latches silently; a wall landing
         mid-window CHANGES the disposition and re-proposes immediately

THE SECOND FALSIFY LIST — an adversarial review of tip 781fd733b80b
found six functional blockers whose common shape is that the FIXTURES ISOLATED
the combinations. Each cure below is pinned by an arm that fails on the
pre-cure code:
  A14 the digest may advertise a merely plausible command
      -> the rendered `helm stale redispatch` argv is parsed and executed through
         the real owner-layer path to an exact repo-bound successor send
  A15 a display cap is cosmetic
      -> 20 rows render 12 and latch EXACTLY 12; the other 8 are declared in
         the text and PROPOSED on the next sweep, never recorded as asked
  A16 a row absent from the walk has left the population
      -> an unavailable source SUSPENDS re-arm and keeps the latch; a
         complete read still re-arms (the positive control)
  A17 the disposition word is the whole ask
      -> a SECOND cure commit under an unchanged terminal re-proposes; a
         legacy entry with no fingerprint still latches (no migration burst)
  A18 one git index can answer for the whole ledger
      -> a cure in EACH of two repos is found against its own index; a row
         with no repo_id is UNKNOWN and reported, never judged against cwd
  A19 an id already claimed by the lr walk needs no cure classification
      -> a row that is BOTH stalled and cured yields ONE item, kind=cured,
         carrying the stall clause forward
  A20 a seat's name is its capability family
      -> the family is resolved through family_for with the roster's VERIFIED
         runtime metadata, and an unverified row arrives as unverified
"""
import os
import shlex
import subprocess
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-stalebot-", var="HELM_HOME")

from helm import dispatches, landreq, seats_integrator, stalebot  # noqa: E402


def _integrator():
    """The seat the code under test will address, asked the same way it asks.

    NOT A LITERAL AND NOT THE DEFAULT CONSTANT. These arms are about
    DIGEST GROUPING — that unowned rows ride one post to the integrator —
    so they must name whoever the resolver names. Pinning the default
    here instead would keep passing if the module stopped resolving at
    all and fell back to a constant, which is the defect the resolver
    exists to prevent. The arms that prove RESOLUTION itself seed a
    roster and name the seat in it.
    """
    return seats_integrator.integrator_seat_or_default()


# ---------------------------------------------------------------------------
# the fixture repo: one land-by-cherry-pick, one absent branch, one rotted line
# ---------------------------------------------------------------------------

def _git(repo, *args, date=None):
    env = dict(os.environ,
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    if date:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    r = subprocess.run(["git", "-C", repo] + list(args),
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise AssertionError("git %s: %s" % (args, r.stderr))
    return r.stdout.strip()


def _write(repo, name, text):
    with open(os.path.join(repo, name), "w") as f:
        f.write(text)


class _RepoFixture(unittest.TestCase):
    """main: C0 -> CM1 (adds b, ROTS f.py:1) -> CC (cherry-pick of CT).
    lane: C0 -> CT (touches a.txt only; its change lands as CC, a NEW object).
    absent: C0 -> CA (touches c.txt; lands nowhere)."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.repo = tempfile.mkdtemp(prefix="stalebot-fixture-")
        cls._cleanup = cls.repo
        _git(cls.repo, "init", "-q", "-b", "main")
        _write(cls.repo, "a.txt", "alpha one\n")
        _write(cls.repo, "f.py", "FLAG = original\n")
        _write(cls.repo, "g.py", "STEADY = true\n")
        _git(cls.repo, "add", ".")
        _git(cls.repo, "commit", "-q", "-m", "c0 base",
             date="2026-01-01T00:00:00Z")
        cls.c0 = _git(cls.repo, "rev-parse", "HEAD")
        _git(cls.repo, "checkout", "-q", "-b", "lane")
        _write(cls.repo, "a.txt", "alpha one\nalpha two\n")
        _git(cls.repo, "commit", "-aqm", "ct lane change",
             date="2026-01-01T01:00:00Z")
        cls.ct = _git(cls.repo, "rev-parse", "HEAD")
        _git(cls.repo, "checkout", "-q", "-b", "orphan", cls.c0)
        _write(cls.repo, "c.txt", "never lands\n")
        _git(cls.repo, "add", "c.txt")
        _git(cls.repo, "commit", "-qm", "ca absent work",
             date="2026-01-01T01:30:00Z")
        cls.ca = _git(cls.repo, "rev-parse", "HEAD")
        _git(cls.repo, "checkout", "-q", "main")
        _write(cls.repo, "b.txt", "advance\n")
        _write(cls.repo, "f.py", "FLAG = ROTTED\n")   # the anchor moves
        _git(cls.repo, "add", ".")
        _git(cls.repo, "commit", "-qm", "cm1 trunk advance",
             date="2026-01-01T02:00:00Z")
        cls.cm1 = _git(cls.repo, "rev-parse", "HEAD")
        _git(cls.repo, "cherry-pick", cls.ct,
             date="2026-01-01T03:00:00Z")
        cls.cc = _git(cls.repo, "rev-parse", "HEAD")
        cls.repo_id = dispatches._repo_info(cls.repo)["repo_id"]

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls._cleanup, ignore_errors=True)

    def row(self, **kw):
        base = {"id": "ab12cd34ef56ab78", "kind": "review",
                "repo_id": self.repo_id}
        base.update(kw)
        return base


class SupersedeClassifierTest(_RepoFixture):

    def test_landed_by_cherry_pick_names_the_CARRIER_not_the_reviewed_tip(self):
        """A2: the fleet lands by cherry-pick, so the reviewed object reaches
        trunk under a NEW sha — a supersede proposal naming the reviewed tip
        would point the owner at a commit trunk does not contain."""
        self.assertNotEqual(self.ct, self.cc)   # the fixture IS the discrimination
        term, ev, door = stalebot.classify_dispatch(
            self.row(reviewed_tip=self.ct), trunk="main")
        self.assertEqual(term, stalebot.SUPERSEDE)
        self.assertIn(self.cc[:12], ev)
        self.assertIn("patch-equivalent", ev)
        self.assertNotIn(self.ct[:12], ev)
        self.assertIn("helm lr close ab12cd34ef56 --reason superseded", door)

    def test_landed_by_ancestry_carries_at_the_tip_itself(self):
        term, ev, _door = stalebot.classify_dispatch(
            self.row(reviewed_tip=self.cm1), trunk="main")
        self.assertEqual(term, stalebot.SUPERSEDE)
        self.assertIn(self.cm1[:12], ev)
        self.assertIn("ancestor", ev)

    def test_a_build_rows_base_never_reads_supersede(self):
        """A1: a BUILD row's ref is the base the work was dispatched FROM —
        an ancestor of trunk on nearly every row ever written (48 of 52
        measured), so reading it as content identity would call the whole
        backlog superseded."""
        term, ev, _door = stalebot.classify_dispatch(
            self.row(kind="build", ref=self.c0, reviewed_tip=""),
            trunk="main")
        self.assertEqual(term, stalebot.KEEP)
        self.assertIn("landing unknown", ev)


class CancelClassifierTest(_RepoFixture):

    def test_absent_content_with_rotted_claims_proposes_cancel(self):
        term, ev, door = stalebot.classify_dispatch(
            self.row(reviewed_tip=self.ca,
                     note="work at %s" % self.ca,
                     ts="2026-01-01T01:35:00Z"),
            trunk="main")
        self.assertEqual(term, stalebot.CANCEL)
        self.assertIn("NOT on trunk", ev)
        self.assertIn("helm dispatch cancel ab12cd34ef56", door)

    def test_rotted_claims_on_LANDED_work_reads_supersede_not_cancel(self):
        """A3, first half: precedence. The same rotted claim beside content
        that DID land must propose the close door, not the abandon door —
        cancel on landed work destroys a true row over a stale sentence."""
        term, ev, _door = stalebot.classify_dispatch(
            self.row(reviewed_tip=self.ct,
                     note="work at %s" % self.ca,
                     ts="2026-01-01T01:35:00Z"),
            trunk="main")
        self.assertEqual(term, stalebot.SUPERSEDE)
        self.assertIn(self.cc[:12], ev)

    def test_an_unmeasurable_landing_never_cancels(self):
        """A3, second half: cancel requires the ABSENT proof. A repo this
        sweep cannot read yields UNKNOWN, and UNKNOWN proposes nothing —
        an unread fact is not evidence of moot."""
        term, ev, _door = stalebot.classify_dispatch(
            {"id": "ab12cd34ef56ab78", "kind": "review",
             "reviewed_tip": self.ca, "repo_id": "/nonexistent/.git",
             "note": "work at %s" % self.ca},
            trunk="main")
        self.assertEqual(term, stalebot.KEEP)
        self.assertIn("landing unknown", ev)


class OffchainLandingClassifierTest(_RepoFixture):
    """task/644: UNKNOWN landing on a non-recurring lane consumes task/430's
    detector before defaulting to KEEP.

    THE LIVE SPECIMEN THIS EXISTS FOR: row 652d9794 (parked-dispatch-rebind)
    was proposed still-live-keep with "landing unknown" while its work was on
    trunk under a DIFFERENT lane label. The classifier keys landedness on the
    ROW'S OWN REF, which cannot see a successor that landed under another
    label — so an honest UNKNOWN became a proposal to keep billing a seat.
    """

    # A REAL, RESOLVABLE REPO — and the previous "/nonexistent/.git" was not
    # a harmless fixture shortcut, it ENCODED the defect a probe reproduced:
    # the probe reached its own local ref even though `_close_repo` REJECTS
    # that repo. The classifier now resolves the probe's repo and ref through
    # the same doors, so an unresolvable repo KEEPS — which is its own arm
    # below rather than the default state of every arm here.
    def row(self, **over):
        r = {"id": "cd34ef56ab78cd90", "kind": "build",
             "lane": "parked-widget-lane-x", "seq": 10,
             "repo_id": os.path.join(self.repo, ".git")}
        r.update(over)
        return r

    def _classify(self, found, row=None, calls=None):
        """Classify with the detector stubbed. `calls` receives the stub call
        count, so a caller can assert the detector was ASKED — or never was."""
        with mock.patch.object(landreq, "offchain_landing",
                               return_value=found) as stub:
            out = stalebot.classify_dispatch(row or self.row(), trunk="main")
        if calls is not None:
            calls.append(stub.call_count)
        return out

    def test_a_row_with_NO_REPO_BINDING_keeps_and_is_never_probed(self):
        """Review blocker 1, door 1 of 2 — MEASURED, not assumed. The cure
        resolves the probe's repo through `landreq._close_repo`, and that door
        refuses exactly one shape: a row with no repository binding at all.
        (A merely NONEXISTENT path sails through it and is refused by the
        trunk door instead — that is the arm below, and conflating the two is
        how this arm was first written with a fixture that proved neither.)

        LOAD-BEARING MUTATION: in helm/stalebot.py, inside
        `if rerr or not probe_dir:` add
        `landreq.offchain_landing(row, gitdir=repo, trunk=trunk)` above
        `found = None` — i.e. search the rejected repo and discard the answer.
          python3 -m unittest tests.test_stalebot.OffchainLandingClassifierTest\
.test_a_row_with_NO_REPO_BINDING_keeps_and_is_never_probed
          -> AssertionError: Lists differ: [1, 1] != [1, 0]
        The verdict is UNCHANGED by that mutation, which is the whole point:
        only the call-count assertion can kill it."""
        probes = []
        term, _ev, _door = self._classify(("c" * 40, "widget-lane-x", False),
                                          calls=probes)
        self.assertEqual(term, stalebot.SUPERSEDE)   # UNCONDITIONAL CONTROL
        # These two tokens are stale-bot's WIRE CONTRACT — the console and the
        # sweep read them as strings — so pin the values, not only the names.
        # It is also what makes the control above a presence proof rather than
        # `x == <some attribute>`, which is satisfiable by two Nones.
        self.assertEqual(stalebot.SUPERSEDE, "supersede-candidate")
        self.assertEqual(stalebot.KEEP, "still-live-keep")
        term, ev, _door = self._classify(("c" * 40, "widget-lane-x", False),
                                         row=self.row(repo_id=""),
                                         calls=probes)
        self.assertEqual(term, stalebot.KEEP)
        self.assertIn("landing unknown", ev)
        # ONE observable carries both halves: the resolvable row REACHED the
        # detector and the unbound one did not. A bare `[0]` on a second list
        # would be an absence with its control on a different observable.
        self.assertEqual(probes, [1, 0],
                         "asked for the bound row, never for the unbound one")

    def test_an_UNRESOLVABLE_TRUNK_keeps_and_is_never_probed(self):
        """Review blocker 1, door 2 of 2. The original repro: the probe took
        the caller's RAW `--trunk` string and fell back to its own local ref,
        so an upstream-only authority with a citation on local main proposed
        supersede. The ref is now resolved through `landreq._close_trunk`, and
        a ref that names zero branches KEEPS — a citation nobody else can see
        is not evidence, so the search must not happen at all.

        LOAD-BEARING MUTATION: in helm/stalebot.py replace
        `found = None if terr else landreq.offchain_landing(...)` with an
        unconditional `found = landreq.offchain_landing(row, gitdir=probe_dir,
        trunk=ref)`.
          python3 -m unittest tests.test_stalebot.OffchainLandingClassifierTest\
.test_an_UNRESOLVABLE_TRUNK_keeps_and_is_never_probed
          -> AssertionError: 'supersede-candidate' != 'still-live-keep'
        MEASURED — that mutation trips the VERDICT, because searching past an
        unresolvable ref also USES what it finds. The `probes` assertion below
        covers the quieter variant the verdict cannot see: a change that
        searches the bad ref and then discards the answer, which is equally
        wrong (a citation nobody else can resolve is not evidence regardless
        of what is done with it afterwards) and leaves the verdict at KEEP.

        SECOND MUTATION, measured: hoist the call out of the ternary —
        `_seen = landreq.offchain_landing(...)` then
        `found = None if terr else _seen`. The verdict is unchanged BY
        CONSTRUCTION, and the arm still dies:
          -> AssertionError: Lists differ: [1, 1] != [1, 0]"""
        probes = []
        term, _ev, _door = self._classify(("c" * 40, "widget-lane-x", False),
                                          calls=probes)
        self.assertEqual(term, stalebot.SUPERSEDE)   # UNCONDITIONAL CONTROL
        # These two tokens are stale-bot's WIRE CONTRACT — the console and the
        # sweep read them as strings — so pin the values, not only the names.
        # It is also what makes the control above a presence proof rather than
        # `x == <some attribute>`, which is satisfiable by two Nones.
        self.assertEqual(stalebot.SUPERSEDE, "supersede-candidate")
        self.assertEqual(stalebot.KEEP, "still-live-keep")

        # The path RESOLVES as a repo — `_close_repo` returns it with no error
        # — and is then refused for naming zero refs. Asserted here so the arm
        # cannot silently migrate to the other door if that behaviour changes.
        bad = self.row(repo_id="/nonexistent/.git")
        self.assertEqual(landreq._close_repo(bad, None),
                         ("/nonexistent/.git", None))

        term, ev, _door = self._classify(("c" * 40, "widget-lane-x", False),
                                         row=bad, calls=probes)
        self.assertEqual(term, stalebot.KEEP)
        self.assertIn("landing unknown", ev)
        # ONE observable, both halves — see the sibling arm.
        self.assertEqual(probes, [1, 0],
                         "asked past the good ref, never past the bad one")

    def test_an_offchain_carrier_proposes_supersede_instead_of_keep(self):
        # NEGATIVE CONTROL FIRST, same row: with the detector finding nothing
        # this row still reads KEEP, so the proposal below is the DETECTOR's
        # answer rather than a branch that always fires.
        term, ev, _door = self._classify(None)
        self.assertEqual(term, stalebot.KEEP)
        self.assertIn("landing unknown", ev)

        term, ev, door = self._classify(("c" * 40, "widget-lane-x", False))
        self.assertEqual(term, stalebot.SUPERSEDE)
        self.assertIn("c" * 12, ev, "the carrier sha must be named")
        self.assertIn("lane STEM", ev,
                      "a stem hit is weaker evidence than a label hit and the "
                      "proposal must say which it had")
        self.assertIn("may have landed under another lane label", ev)
        self.assertIn("--reason superseded", door)

        exact = self._classify(("d" * 40, "parked-widget-lane-x", True))[1]
        self.assertIn("cites lane 'parked-widget-lane-x'", exact)
        self.assertNotIn("lane STEM", exact)

    def test_a_detector_that_RAISES_degrades_to_keep_not_to_a_proposal(self):
        """A sweep that cannot measure proposes NOTHING. The bot's whole
        contract is that it proposes and never executes; a detector failure
        must not manufacture a terminal, and it must not wedge the sweep."""
        with mock.patch.object(landreq, "offchain_landing",
                               side_effect=OSError("git vanished")):
            term, ev, _door = stalebot.classify_dispatch(self.row(),
                                                         trunk="main")
        self.assertEqual(term, stalebot.KEEP)
        self.assertIn("landing unknown", ev)


class RetipClassifierTest(_RepoFixture):

    def test_a_base_past_the_measured_bar_proposes_retip(self):
        term, ev, door = stalebot.classify_dispatch(
            self.row(kind="build", ref=self.c0, reviewed_tip=""),
            lr={"base_behind": landreq.STALE_BASE_BEHIND}, trunk="main")
        self.assertEqual(term, stalebot.RETIP)
        self.assertIn(str(landreq.STALE_BASE_BEHIND), ev)
        self.assertIn("helm dispatch retip ab12cd34ef56", door)

    def test_an_unmeasured_base_behind_never_retips(self):
        """A4: base_behind is None on every non-READY row and on any READY row
        whose drift could not be measured — UNMEASURED, never zero (task/266).
        A retip proposed on a non-measurement is a mutation proposed on
        nothing."""
        term, _ev, _door = stalebot.classify_dispatch(
            self.row(kind="build", ref=self.c0, reviewed_tip=""),
            lr={"base_behind": None}, trunk="main")
        self.assertEqual(term, stalebot.KEEP)

    def test_landed_content_outranks_any_base_drift(self):
        """A5: a base drifting under FINISHED work is housekeeping the close
        already performs — re-tipping a landed row would re-open done work."""
        term, ev, _door = stalebot.classify_dispatch(
            self.row(reviewed_tip=self.ct),
            lr={"base_behind": 99999}, trunk="main")
        self.assertEqual(term, stalebot.SUPERSEDE)
        self.assertIn(self.cc[:12], ev)


class TaskClassifierTest(_RepoFixture):

    OLD = 4 * 86400

    def trow(self, **kw):
        base = {"id": "task/9001", "title": "t", "note": "",
                "ts": time.time() - self.OLD,
                "last_updated": time.time() - self.OLD}
        base.update(kw)
        return base

    def test_every_cited_commit_on_trunk_proposes_supersede(self):
        term, ev, door = stalebot.classify_task(
            self.trow(note="landed at %s" % self.cm1), self.repo)
        self.assertEqual(term, stalebot.SUPERSEDE)
        self.assertIn(self.cm1[:12], ev)
        self.assertIn("helm task close task/9001", door)

    def test_a_landed_sha_beside_an_absent_one_never_supersedes(self):
        """A6: one landed citation beside one unlanded is IN-FLIGHT work —
        for a task row a not-on-trunk sha is usually the lane tip still being
        built, the opposite of moot, so neither supersede nor cancel may
        ride the landed half."""
        term, _ev, _door = stalebot.classify_task(
            self.trow(note="landed %s, building %s" % (self.cm1, self.ca)),
            self.repo)
        self.assertEqual(term, stalebot.KEEP)

    def test_every_anchor_rotted_requests_reanchoring_without_a_close_door(self):
        term, ev, door = stalebot.classify_task(
            self.trow(note="check f.py:1 for the flag",
                      ts=1767229200.0),   # 2026-01-01T01:00:00Z — after C0,
            self.repo)                    # before the CM1 rot at 02:00
        self.assertEqual(term, stalebot.REANCHOR)
        self.assertIn("f.py:1", ev)
        self.assertIn("re-anchor", ev)
        self.assertEqual(door, "")

    def test_a_partially_fresh_note_keeps(self):
        """A7: one live anchor is a live note — only the rotted citations need
        repair; neither state is evidence that the task itself is moot."""
        term, _ev, _door = stalebot.classify_task(
            self.trow(note="check f.py:1 and g.py:1", ts=1767229200.0),
            self.repo)
        self.assertEqual(term, stalebot.KEEP)


class AgeGateTest(unittest.TestCase):

    def test_the_fresh_row_is_not_walked_while_its_aged_sibling_is(self):
        """A8, with the must-hit seeded: the aged sibling in the SAME fixture
        proves the walk ran, so the fresh row's absence is a verdict of the
        age gate rather than of a dead scan."""
        now = time.time()
        fresh = {"id": "task/1", "title": "fresh", "status": "open",
                 "last_updated": now - 3600}
        aged = {"id": "task/2", "title": "aged", "status": "open",
                "last_updated": now - 4 * 86400}
        with mock.patch.object(stalebot.landreq, "stalls",
                               return_value=([], None)), \
             mock.patch.object(stalebot.dispatches, "rows", return_value={}), \
             mock.patch.object(stalebot.dispatches, "snapshot",
                               return_value=({}, None)), \
             mock.patch.object(stalebot.dispatches, "owed", return_value=[]), \
             mock.patch.object(stalebot.dispatches, "live_claims",
                               return_value={}), \
             mock.patch.object(stalebot.tasks, "open_rows",
                               return_value=[fresh, aged]):
            items, unavailable, _nd = stalebot.collect(now)
        self.assertEqual([it["id"] for it in items], ["task/2"])
        self.assertEqual(unavailable, [])

    def test_an_unreadable_source_is_reported_never_skipped(self):
        now = time.time()
        with mock.patch.object(stalebot.landreq, "stalls",
                               side_effect=OSError("disk gone")), \
             mock.patch.object(stalebot.dispatches, "rows", return_value={}), \
             mock.patch.object(stalebot.dispatches, "snapshot",
                               return_value=({}, None)), \
             mock.patch.object(stalebot.dispatches, "owed", return_value=[]), \
             mock.patch.object(stalebot.dispatches, "live_claims",
                               return_value={}), \
             mock.patch.object(stalebot.tasks, "open_rows", return_value=[]):
            _items, unavailable, _nd = stalebot.collect(now)
        self.assertTrue(any("lr stalls" in u for u in unavailable), unavailable)


# ---------------------------------------------------------------------------
# the digest + latch + state — hermetic over patched collection/classification
# ---------------------------------------------------------------------------

ID_A1 = "a1" * 8
ID_A2 = "a2" * 8
ID_B1 = "b1" * 8


def _item(rid, recipient, age_s=4 * 86400):
    return {"kind": "dispatch", "id": rid,
            "row": {"id": rid, "recipient": recipient}, "lr": None,
            "age_s": age_s, "why_aged": "%ds old" % age_s}


class _SweepHarness(unittest.TestCase):
    """sweep() over patched collect/classify with a recording _post."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="stalebot-state-")
        self.state = os.path.join(self.tmp, "state.json")
        self.posts = []
        self.addCleanup(__import__("shutil").rmtree, self.tmp,
                        ignore_errors=True)

    def run_sweep(self, items, classify=None, post=True, post_ok=True,
                  now=None):
        classify = classify or (lambda row, lr=None, repo=None, trunk=None:
                                (stalebot.KEEP, "evidence line", ""))
        def fake_post(_owner, text):
            self.posts.append(text)
            return post_ok
        with mock.patch.object(stalebot, "collect",
                               return_value=(items, [], 0)), \
             mock.patch.object(stalebot, "classify_dispatch",
                               side_effect=classify), \
             mock.patch.object(stalebot, "_post", side_effect=fake_post):
            return stalebot.sweep(now=now, post=post, state_path=self.state)


class DigestTest(_SweepHarness):

    def test_one_post_per_owner_and_every_row_id_is_PRESENT(self):
        rep = self.run_sweep([_item(ID_A1, "alpha"), _item(ID_A2, "alpha"),
                              _item(ID_B1, "beta")])
        self.assertEqual(len(self.posts), 2)
        alpha = [p for p in self.posts if p.startswith("@alpha ")]
        beta = [p for p in self.posts if p.startswith("@beta ")]
        self.assertEqual((len(alpha), len(beta)), (1, 1))
        self.assertIn(ID_A1[:12], alpha[0])
        self.assertIn(ID_A2[:12], alpha[0])
        self.assertIn("CONCUR/OVERRULE", alpha[0])
        self.assertIn(ID_B1[:12], beta[0])
        self.assertEqual(sorted(rep["posted"]), ["alpha", "beta"])

    def test_unowned_rows_fold_into_ONE_integrator_digest(self):
        self.run_sweep([_item(ID_A1, ""), _item(ID_A2, "")])
        self.assertEqual(len(self.posts), 1)
        self.assertTrue(self.posts[0].startswith("@" + _integrator()),
                        self.posts[0])
        self.assertIn(ID_A1[:12], self.posts[0])
        self.assertIn(ID_A2[:12], self.posts[0])

    def test_dry_run_still_REPORTS_but_posts_nothing_and_writes_no_state(self):  # noqa: VACUOUS_ASSERTION — the absence halves (no post, no state file) are controlled by the digest content positively asserted here AND by the posting sibling above, which proves the same seams DO fire when post=True
        rep = self.run_sweep([_item(ID_A1, "alpha")], post=False)
        self.assertIn("alpha", rep["digests"])
        self.assertIn(ID_A1[:12], rep["digests"]["alpha"])
        self.assertEqual(self.posts, [])
        self.assertFalse(os.path.exists(self.state))

    def test_dry_run_does_not_require_writable_state_storage(self):  # noqa: VACUOUS_ASSERTION — the rendered digest positively proves the dry-run path executed before the absent lock assertion
        path = "/proc/helm-stale-test/state.json"
        with mock.patch.object(stalebot, "collect",
                               return_value=([_item(ID_A1, "alpha")], [], 0)), \
             mock.patch.object(stalebot, "classify_dispatch",
                               return_value=(stalebot.KEEP, "e", "")):
            rep = stalebot.sweep(post=False, state_path=path)
        self.assertIn(ID_A1[:12], rep["digests"]["alpha"])
        self.assertFalse(os.path.exists(path + ".lock"))


class LatchTest(_SweepHarness):

    def test_a_delivered_ask_latches_for_the_window(self):
        self.run_sweep([_item(ID_A1, "alpha")])
        rep2 = self.run_sweep([_item(ID_A1, "alpha")])
        self.assertEqual(len(self.posts), 1)   # the second sweep stayed silent
        self.assertEqual(rep2["latched"], 1)

    def test_a_changed_proposed_disposition_reproposes_immediately(self):
        """A9: the same keep twice in a day is noise; a keep whose last live
        citation rotted becomes REANCHOR and is news the owner has not heard."""
        self.run_sweep([_item(ID_A1, "alpha")])
        self.run_sweep([_item(ID_A1, "alpha")],
                       classify=lambda row, lr=None, repo=None, trunk=None:
                       (stalebot.REANCHOR, "re-anchor f.py:1", ""))
        self.assertEqual(len(self.posts), 2)
        self.assertIn(ID_A1[:12], self.posts[1])
        self.assertIn(stalebot.REANCHOR, self.posts[1])

    def test_a_FAILED_post_never_latches_and_the_next_sweep_recovers(self):
        """A10 — repo-watch's cursor law: an undelivered digest must not
        advance the latch, or the watcher fails silent while its state
        marches on."""
        rep1 = self.run_sweep([_item(ID_A1, "alpha")], post_ok=False)
        self.assertEqual(rep1["failed"], ["alpha"])
        self.assertEqual(stalebot.read_state(self.state).get("rows"), {})
        self.run_sweep([_item(ID_A1, "alpha")])
        self.assertEqual(len(self.posts), 2)
        self.assertIn(ID_A1[:12], self.posts[1])

    def test_real_post_proves_room_and_dm_before_latching(self):
        from helm import chat, seats
        roster = {"alpha": {"session": "sid"}}
        patches = (
            mock.patch.object(stalebot, "collect",
                              return_value=([_item(ID_A1, "alpha")], [], 0)),
            mock.patch.object(stalebot, "classify_dispatch",
                              return_value=(stalebot.KEEP, "e", "")),
            mock.patch.object(seats, "roster_checked",
                              return_value=(roster, False)),
            mock.patch.object(stalebot, "_live_roster_seats",
                              return_value={"alpha"}),
            mock.patch.object(stalebot, "_author_walled",
                              return_value=(False, "healthy")),
            mock.patch.object(seats, "beacon_procs",
                              return_value=([123], None)),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        first = stalebot.sweep(post=True, state_path=self.state)
        self.assertEqual(first["posted"], ["alpha"])
        public = [row for row in chat.read(stalebot.ROOM)[0]
                  if ID_A1[:12] in row.get("text", "")]
        private = [row for row in chat.read(chat.dm_room("alpha"))[0]
                   if ID_A1[:12] in row.get("text", "")]
        self.assertEqual((len(public), len(private)), (1, 1))
        self.assertEqual(sorted(stalebot.read_state(self.state)["rows"]), [ID_A1])
        second = stalebot.sweep(post=True, state_path=self.state)
        self.assertEqual(second["latched"], 1)
        self.assertEqual(len([row for row in chat.read(stalebot.ROOM)[0]
                              if ID_A1[:12] in row.get("text", "")]), 1)
        self.assertEqual(len([row for row in chat.read(chat.dm_room("alpha"))[0]
                              if ID_A1[:12] in row.get("text", "")]), 1)

    def test_real_keyed_room_failure_stays_unlatched_and_retry_deduplicates(self):
        from helm import chat, seats
        import contextlib
        roster = {"alpha": {"session": "sid"}}
        patches = (
            mock.patch.object(stalebot, "collect",
                              return_value=([_item(ID_A1, "alpha")], [], 0)),
            mock.patch.object(stalebot, "classify_dispatch",
                              return_value=(stalebot.KEEP, "e", "")),
            mock.patch.object(seats, "roster_checked", return_value=(roster, False)),
            mock.patch.object(stalebot, "_live_roster_seats", return_value={"alpha"}),
            mock.patch.object(stalebot, "_author_walled", return_value=(False, "healthy")),
            mock.patch.object(seats, "beacon_procs", return_value=([123], None)),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        real = chat._room_lock
        calls = {"n": 0}

        @contextlib.contextmanager
        def fail_dm_once(room, timeout_s=None):
            calls["n"] += 1
            if calls["n"] == 2:
                yield False
            else:
                with real(room, timeout_s=timeout_s) as locked:
                    yield locked

        with mock.patch.object(chat, "_room_lock", fail_dm_once):
            first = stalebot.sweep(post=True, state_path=self.state)
        self.assertEqual(first["failed"], ["alpha"])
        self.assertEqual(stalebot.read_state(self.state).get("rows"), {})
        self.assertEqual(len([r for r in chat.read(stalebot.ROOM)[0]
                              if ID_A1[:12] in r.get("text", "")]), 1)
        self.assertEqual(len([r for r in chat.read(chat.dm_room("alpha"))[0]
                              if ID_A1[:12] in r.get("text", "")]), 0)
        second = stalebot.sweep(post=True, state_path=self.state)
        self.assertEqual(second["posted"], ["alpha"])
        self.assertEqual(len([r for r in chat.read(stalebot.ROOM)[0]
                              if ID_A1[:12] in r.get("text", "")]), 1)
        self.assertEqual(len([r for r in chat.read(chat.dm_room("alpha"))[0]
                              if ID_A1[:12] in r.get("text", "")]), 1)

    def test_beacon_lost_after_dm_append_never_latches(self):
        from helm import seats
        roster = {"alpha": {"session": "sid"}}
        with mock.patch.object(stalebot, "collect",
                               return_value=([_item(ID_A2, "alpha")], [], 0)), \
             mock.patch.object(stalebot, "classify_dispatch",
                               return_value=(stalebot.KEEP, "e", "")), \
             mock.patch.object(seats, "roster_checked",
                               return_value=(roster, False)), \
             mock.patch.object(stalebot, "_live_roster_seats",
                               return_value={"alpha"}), \
             mock.patch.object(stalebot, "_author_walled",
                               return_value=(False, "healthy")), \
             mock.patch.object(seats, "beacon_procs",
                               side_effect=(([123], None), ([], None))):
            rep = stalebot.sweep(post=True, state_path=self.state)
        self.assertEqual(rep["failed"], ["alpha"])
        self.assertEqual(stalebot.read_state(self.state).get("rows"), {})

    def test_a_room_append_without_an_addressed_wake_never_latches(self):
        from helm import chat, seats
        roster = {"alpha": {"session": "sid"}}
        with mock.patch.object(stalebot, "collect",
                               return_value=([_item(ID_A1, "alpha")], [], 0)), \
             mock.patch.object(stalebot, "classify_dispatch",
                               return_value=(stalebot.KEEP, "e", "")), \
             mock.patch.object(seats, "roster_checked",
                               return_value=(roster, False)), \
             mock.patch.object(stalebot, "_live_roster_seats",
                               return_value={"alpha"}), \
             mock.patch.object(stalebot, "_author_walled",
                               return_value=(False, "healthy")), \
             mock.patch.object(seats, "beacon_procs",
                               return_value=([123], None)), \
             mock.patch.object(chat, "post",
                               side_effect=({"id": "room-row"},
                                            OSError("dm lane unavailable"))):
            rep = stalebot.sweep(post=True, state_path=self.state)
        self.assertEqual(rep["failed"], ["alpha"])
        self.assertEqual(stalebot.read_state(self.state).get("rows"), {})

    def test_a_row_no_longer_aged_rearms(self):
        self.run_sweep([_item(ID_A1, "alpha")])
        self.run_sweep([])                     # the row resolved
        self.assertEqual(stalebot.read_state(self.state).get("rows"), {})
        self.run_sweep([_item(ID_A1, "alpha")])   # re-aged: asked again
        self.assertEqual(len(self.posts), 2)


class StateReportTest(_SweepHarness):

    def test_the_sweep_records_its_own_liveness_and_counts(self):
        now = time.time()
        self.run_sweep([_item(ID_A1, "alpha", age_s=5 * 86400)], now=now)
        st = stalebot.read_state(self.state)
        self.assertEqual(st.get("last_run"), now)
        self.assertEqual(st.get("swept"), 1)
        self.assertEqual(st.get("proposed"), 1)
        self.assertEqual(st.get("oldest_unproposed_s"), 5 * 86400)

    def test_quiet_records_the_run_but_latches_NOTHING(self):
        """A latch on a proposal nobody received would silence the row for
        the whole window without anyone having been asked — quiet must spend
        no attention budget while still stamping the loop's liveness."""
        with mock.patch.object(stalebot, "collect",
                               return_value=([_item(ID_A1, "alpha")], [], 0)), \
             mock.patch.object(stalebot, "classify_dispatch",
                               return_value=(stalebot.KEEP, "e", "")), \
             mock.patch.object(stalebot, "_post",
                               side_effect=AssertionError("quiet must not post")):
            stalebot.sweep(post=True, quiet=True, state_path=self.state)
        st = stalebot.read_state(self.state)
        self.assertTrue(st.get("last_run"))
        self.assertEqual(st.get("rows"), {})   # unposted -> unlatched


class DoctorTest(unittest.TestCase):

    def _run(self, state, timer_exists):
        import tempfile
        from helm import doctor
        tmp = tempfile.mkdtemp(prefix="stalebot-doctor-")
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        unit = os.path.join(tmp, "helm-stale-bot.timer")
        if timer_exists:
            open(unit, "w").write("[Timer]\n")
        with mock.patch.object(stalebot, "read_state", return_value=state), \
             mock.patch.object(stalebot, "_timer_units",
                               return_value=("s", "", unit, "")):
            return doctor.check_stale_bot()

    def test_a_loop_that_never_ran_is_a_finding_not_a_silence(self):
        out = self._run({}, timer_exists=False)
        msgs = [m for _l, m in out]
        self.assertTrue(any("never recorded" in m for m in msgs), msgs)
        self.assertTrue(any("NOT installed" in m for m in msgs), msgs)

    def test_a_dead_timer_is_VISIBLE(self):
        out = self._run({"last_run": time.time() - 3 * 86400, "swept": 5,
                         "proposed": 2, "digests_posted": 1,
                         "oldest_unproposed_s": 7200}, timer_exists=True)
        self.assertTrue(any(l == "WARN" and "PAST TWICE ITS CADENCE" in m
                            for l, m in out), out)

    def test_a_live_loop_reports_its_last_sweeps_counts(self):
        out = self._run({"last_run": time.time() - 3600, "swept": 5,
                         "proposed": 2, "digests_posted": 1,
                         "oldest_unproposed_s": 7200}, timer_exists=True)
        ok = [m for l, m in out if l == "OK"]
        self.assertEqual(len(ok), 1, out)
        self.assertIn("5 row(s) swept", ok[0])
        self.assertIn("2 proposal(s)", ok[0])
        self.assertIn("oldest unproposed 2h", ok[0])


ID_C1 = "c1" * 8


def _cured_item(rid, sender, ahead=2):
    row = {"id": rid, "kind": "review", "polarity": "fix", "status": "verdict",
           "sender": sender, "source": "sid-" + (sender or "missing"),
           "recipient": "rev-seat", "lane": "cure-lane",
           "reviewed_tip": "e" * 40}
    return {"kind": "cured", "id": rid, "row": row, "lr": None,
            "where": ("cureb", "f" * 40, ahead), "age_s": 4 * 86400,
            "why_aged": "author CURED at %s (+%d on cureb), review never "
                        "re-dispatched" % (("f" * 40)[:12], ahead)}


class _CuredSweepHarness(_SweepHarness):
    """sweep() over cured items with the wall reader pinned — classify_cured
    itself runs FOR REAL, because its words are the digest's wire content."""

    def run_cured(self, items, wall=(False, "family famx HEALTHY per "
                                            "proxywatch"),
                  post=True, post_ok=True, now=None, roster=None,
                  beacon=([123], None)):
        from helm import seats
        def fake_post(_owner, text):
            self.posts.append(text)
            return post_ok
        if roster is None:
            roster = {"rev-seat": {"session": "reviewer-sid"}}
            for item in items:
                row = item["row"]
                sender = row.get("sender")
                if sender:
                    roster[sender] = {"session": row.get("source")}
        with mock.patch.object(stalebot, "collect",
                               return_value=(items, [], 0)), \
             mock.patch.object(stalebot, "_seat_roster",
                               return_value=(roster, False)), \
             mock.patch.object(stalebot, "_live_roster_seats",
                               return_value={str(key).casefold()
                                             for key in roster}), \
             mock.patch.object(stalebot, "_author_walled",
                               return_value=wall), \
             mock.patch.object(stalebot, "_wake_walled",
                               return_value=(False, "reviewer healthy")), \
             mock.patch.object(stalebot, "_worktree_for", return_value="/repo"), \
             mock.patch.object(seats, "beacon_procs", return_value=beacon), \
             mock.patch.object(stalebot, "_post", side_effect=fake_post):
            return stalebot.sweep(now=now, post=post, state_path=self.state)


class CuredDigestTest(_CuredSweepHarness):

    def test_the_digest_proposes_the_redispatch_TO_THE_AUTHOR(self):
        """(a) + (d): the POSTED ROOM LINES are counted positively — one post,
        and that one post is addressed to the author and carries the row id,
        the redispatch disposition, and the send door. Never the absence of a
        complaint."""
        rep = self.run_cured([_cured_item(ID_C1, "alpha")])
        self.assertEqual(len(self.posts), 1)
        post = self.posts[0]
        self.assertTrue(post.startswith("@alpha "), post)
        self.assertTrue(ID_C1[:12] in post, post)
        self.assertTrue(stalebot.REDISPATCH in post, post)
        self.assertTrue(
            "helm stale redispatch %s --reviewer rev-seat" % ID_C1 in post,
            post)
        self.assertFalse("cure-lane" in post.split(" — door: ", 1)[-1], post)
        self.assertFalse("<brief>" in post, post)
        self.assertTrue("CONCUR/OVERRULE" in post, post)
        self.assertEqual(rep["posted"], ["alpha"])
        # wire contract: these tokens travel as strings, pin the values
        self.assertEqual(stalebot.REDISPATCH, "redispatch-candidate")
        self.assertEqual(stalebot.PROXY_REDISPATCH, "redispatch-by-proxy")

    def test_a_WALLED_author_rides_the_integrator_digest_by_proxy(self):
        """A12, measured half: the addressee flips to the integrator and the
        disposition to redispatch-by-proxy — a proposal @-ing a walled seat is
        parked with the only reader who cannot act (task/983's walled-author
        row). The one post being INTEGRATOR-addressed with len(posts)==1 is
        the positive control on the same observable the absence assertion
        reads; the author-addressed sibling arm above proves this seam DOES
        mint @author posts when the wall says not-walled."""
        rep = self.run_cured(
            [_cured_item(ID_C1, "seat-b")],
            wall=(True, "family famx QUOTA-402 since x per proxywatch"))
        self.assertEqual(len(self.posts), 1)
        post = self.posts[0]
        self.assertTrue(post.startswith("@" + _integrator()), post)
        self.assertTrue(stalebot.PROXY_REDISPATCH in post, post)
        self.assertTrue("WALLED" in post, post)
        self.assertTrue("QUOTA-402" in post, post)
        self.assertTrue(
            "helm stale redispatch %s --reviewer rev-seat" % ID_C1 in post,
            post)
        self.assertEqual(rep["posted"], [_integrator()])
        self.assertFalse(any(p.startswith("@seat-b ") for p in self.posts),  # noqa: VACUOUS_ASSERTION — controlled by len(posts)==1 + the integrator-addressed positive above on the SAME list; the redispatch sibling arm proves the seam mints @author posts
                         self.posts)

    def test_an_UNKNOWN_wall_state_rides_the_integrator_never_the_author(self):
        """A12, unknown half: an unreadable wall state must not bill a seat
        that may be walled — UNKNOWN rides the integrator digest and says so.
        Same positive-control shape as the walled sibling: one post, counted,
        integrator-addressed."""
        self.run_cured([_cured_item(ID_C1, "seat-b")],
                       wall=(None, "proxywatch state unreadable: gone"))
        self.assertEqual(len(self.posts), 1)
        post = self.posts[0]
        self.assertTrue(post.startswith("@" + _integrator()), post)
        self.assertTrue(stalebot.PROXY_REDISPATCH in post, post)
        self.assertTrue("UNKNOWN" in post, post)
        self.assertTrue("proxywatch state unreadable: gone" in post, post)
        self.assertFalse(any(p.startswith("@seat-b ") for p in self.posts),  # noqa: VACUOUS_ASSERTION — controlled by len(posts)==1 + the integrator-addressed positive above on the SAME list; the redispatch sibling arm proves the seam mints @author posts
                         self.posts)

    def test_a_row_with_no_recorded_author_rides_the_integrator(self):
        self.run_cured([_cured_item(ID_C1, "")])
        self.assertEqual(len(self.posts), 1)
        post = self.posts[0]
        self.assertTrue(post.startswith("@" + _integrator()), post)
        self.assertTrue(stalebot.PROXY_REDISPATCH in post, post)
        self.assertTrue("not currently addressable" in post, post)

    def test_a_nonwalled_but_UNROSTERED_legacy_author_is_not_billed(self):
        item = _cured_item(ID_C1, "claude")
        roster = {"rev-seat": {"session": "reviewer-sid"}}
        self.run_cured([item], wall=(False, "healthy"), roster=roster)
        self.assertEqual(len(self.posts), 1)
        self.assertTrue(self.posts[0].startswith("@" + _integrator()),
                        self.posts[0])
        self.assertTrue("not currently addressable" in self.posts[0],
                        self.posts[0])

    def test_an_absent_historical_reviewer_creates_an_integrator_obligation(self):
        item = _cured_item(ID_C1, "alpha")
        roster = {"alpha": {"session": item["row"]["source"]}}
        self.run_cured([item], roster=roster)
        post = self.posts[0]
        self.assertTrue(post.startswith("@" + _integrator()), post)
        self.assertTrue("choose one current usable reviewer" in post, post)
        self.assertFalse(" — door:" in post, post)

    def test_an_ambiguous_reviewer_is_never_selected_by_roster_order(self):
        item = _cured_item(ID_C1, "alpha")
        roster = {"alpha": {"session": item["row"]["source"]},
                  "Rev-Seat": {"session": "one"},
                  "rev-seat": {"session": "two"}}
        self.run_cured([item], roster=roster)
        post = self.posts[0]
        self.assertTrue(post.startswith("@" + _integrator()), post)
        self.assertIn("ambiguous by case", post)
        self.assertFalse(" — door:" in post, post)

    def test_a_rostered_but_not_current_reviewer_has_no_runnable_door(self):
        item = _cured_item(ID_C1, "alpha")
        roster = {"alpha": {"session": item["row"]["source"]},
                  "rev-seat": {"session": "reviewer-sid"}}
        term, evidence, door, owner = stalebot.classify_cured(
            item["row"], item["where"], roster, live={"alpha"})
        self.assertEqual((term, owner), (stalebot.PROXY_REDISPATCH, ""))
        self.assertIn("no attributable current presence", evidence)
        self.assertEqual(door, "")

    def test_a_reviewer_without_a_live_beacon_has_no_runnable_door(self):
        from helm import seats
        item = _cured_item(ID_C1, "alpha")
        roster = {"alpha": {"session": item["row"]["source"]},
                  "rev-seat": {"session": "reviewer-sid"}}
        with mock.patch.object(stalebot, "_author_walled",
                               return_value=(False, "healthy")), \
             mock.patch.object(seats, "beacon_procs",
                               return_value=([], None)):
            term, evidence, door, owner = stalebot.classify_cured(
                item["row"], item["where"], roster, live=set(roster))
        self.assertEqual((term, owner), (stalebot.PROXY_REDISPATCH, ""))
        self.assertIn("no proven live wake route", evidence)
        self.assertEqual(door, "")

    def test_same_current_actor_and_reviewer_has_no_runnable_door(self):
        from helm import seats
        item = _cured_item(ID_C1, "alpha")
        item["row"]["recipient"] = "alpha"
        roster = {"alpha": {"session": item["row"]["source"]}}
        with mock.patch.object(stalebot, "_author_walled",
                               return_value=(False, "healthy")), \
             mock.patch.object(stalebot, "_wake_walled",
                               return_value=(False, "healthy")), \
             mock.patch.object(stalebot, "_worktree_for", return_value="/repo"), \
             mock.patch.object(seats, "beacon_procs",
                               return_value=([123], None)):
            term, evidence, door, owner = stalebot.classify_cured(
                item["row"], item["where"], roster, live={"alpha"})
        self.assertEqual((term, owner), (stalebot.PROXY_REDISPATCH, ""))
        self.assertIn("self-delivery", evidence)
        self.assertEqual(door, "")


class CuredLatchTest(_CuredSweepHarness):

    def test_the_second_sweep_within_the_window_does_not_repropose(self):
        """(c)/A13: the second sweep inside REPROPOSE_S with an UNCHANGED
        disposition posts nothing new — the post count stays at ONE (counted,
        not inferred from silence) and the report says LATCHED."""
        self.run_cured([_cured_item(ID_C1, "alpha")])
        rep2 = self.run_cured([_cured_item(ID_C1, "alpha")])
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(rep2["latched"], 1)

    def test_a_wall_landing_mid_window_changes_the_disposition_and_reproposes(
            self):
        """A13, news half: redispatch-candidate -> redispatch-by-proxy is a
        disposition CHANGE, and a change is news the reader has not heard —
        it re-proposes immediately, to the NEW addressee."""
        self.run_cured([_cured_item(ID_C1, "alpha")])
        self.run_cured(
            [_cured_item(ID_C1, "alpha")],
            wall=(True, "family famx AUTH-401 since y per proxywatch"))
        self.assertEqual(len(self.posts), 2)
        self.assertTrue(self.posts[0].startswith("@alpha "), self.posts[0])
        self.assertTrue(self.posts[1].startswith("@" + _integrator()),
                        self.posts[1])
        self.assertTrue(stalebot.PROXY_REDISPATCH in self.posts[1],
                        self.posts[1])

    def test_a_SECOND_CURE_COMMIT_reproposes_though_the_disposition_is_equal(  # noqa: VACUOUS_ASSERTION — latched==0 is bracketed on the SAME observables: len(posts)==2 (the re-proposal happened) and the new tip named in posts[1]; sweep 1 asserting len(posts)==1 proves the latch seam does fire
            self):
        """A17 (weak latch fingerprint): the author pushes another
        cure — new tip, more commits ahead — and the disposition is still
        redispatch-candidate, so a terminal-only latch key called it "the same
        ask" and said nothing for the whole window. The key now carries the
        row's CONTENT identity, so the newer cure is news.

        LOAD-BEARING: with `_fingerprint` reduced to `return term` this arm
        fails on len(posts) == 1, while the sibling above (which changes the
        terminal too) still passes — only a content-keyed assertion can see
        this class."""
        first = _cured_item(ID_C1, "alpha", ahead=2)
        self.run_cured([first])
        self.assertEqual(len(self.posts), 1)
        second = _cured_item(ID_C1, "alpha", ahead=3)
        second["where"] = ("cureb", "9" * 40, 3)      # the NEW cure commit
        rep2 = self.run_cured([second])
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(rep2["latched"], 0)
        self.assertTrue(("9" * 12) in self.posts[1],
                        "the digest must name the NEWER cure tip: "
                        + self.posts[1])

    def test_same_tip_reproposes_when_the_current_actor_changes(self):  # noqa: VACUOUS_ASSERTION — two concrete addressed posts positively prove actor identity changed the semantic fingerprint
        item = _cured_item(ID_C1, "legacy-name")
        sid = item["row"]["source"]
        self.run_cured([item], roster={
            "alpha": {"session": sid},
            "rev-seat": {"session": "reviewer-sid"}})
        self.run_cured([item], roster={
            "beta": {"session": sid},
            "rev-seat": {"session": "reviewer-sid"}})
        self.assertEqual(len(self.posts), 2)
        self.assertTrue(self.posts[0].startswith("@alpha "), self.posts[0])
        self.assertTrue(self.posts[1].startswith("@beta "), self.posts[1])

    def test_same_tip_reproposes_when_the_actionable_reviewer_changes(self):  # noqa: VACUOUS_ASSERTION — two concrete runnable doors positively prove reviewer identity changed the semantic fingerprint
        item = _cured_item(ID_C1, "alpha")
        self.run_cured([item])
        item2 = _cured_item(ID_C1, "alpha")
        item2["row"]["recipient"] = "rev-new"
        self.run_cured([item2], roster={
            "alpha": {"session": item2["row"]["source"]},
            "rev-new": {"session": "new-reviewer-sid"}})
        self.assertEqual(len(self.posts), 2)
        self.assertTrue("--reviewer rev-seat" in self.posts[0], self.posts[0])
        self.assertTrue("--reviewer rev-new" in self.posts[1], self.posts[1])

    def test_same_proxy_terminal_reproposes_when_a_door_becomes_runnable(self):  # noqa: VACUOUS_ASSERTION — the second concrete post and runnable door control the first post's deliberate lack of a door
        item = _cured_item(ID_C1, "old-author")
        roster = {"rev-seat": {"session": "reviewer-sid"}}
        self.run_cured([item], roster=roster, beacon=([], None))
        self.assertNotIn(" — door:", self.posts[0])
        self.run_cured([item], roster=roster, beacon=([123], None))
        self.assertEqual(len(self.posts), 2)
        self.assertIn(" — door:", self.posts[1])

    def test_a_LEGACY_state_entry_without_a_fingerprint_still_latches(self):  # noqa: VACUOUS_ASSERTION — the empty posts list is controlled by rep["latched"]==1 on the same sweep (the row WAS walked and deliberately held); the sibling arms in this class post on the identical harness
        """A17, migration half: a state file written before the fingerprint
        existed must not re-propose every latched row in the fleet at once.
        A stored entry with no fingerprint key falls back to the terminal
        comparison and holds — self-healing, because the entry written on its
        next re-propose carries the new key."""
        import json
        legacy = {"last_run": time.time(),
                  "rows": {ID_C1: {"proposed_at": time.time(),
                                   "terminal": stalebot.REDISPATCH}}}
        with open(self.state, "w") as f:
            json.dump(legacy, f)
        rep = self.run_cured([_cured_item(ID_C1, "alpha")])
        self.assertEqual(self.posts, [])
        self.assertEqual(rep["latched"], 1)

    def test_a_resolved_cure_rearms_its_latch(self):  # noqa: VACUOUS_ASSERTION — the empty rows-state is bracketed by two positive controls on the same seams: sweep 1 posted (latch WAS written) and sweep 3 posts AGAIN (len(posts)==2), which only happens if the latch really dropped
        self.run_cured([_cured_item(ID_C1, "alpha")])
        self.assertEqual(len(self.posts), 1)   # the latch was minted by a post
        self.run_cured([])                    # re-dispatched: no longer cured
        self.assertEqual(stalebot.read_state(self.state).get("rows"), {})
        self.run_cured([_cured_item(ID_C1, "alpha")])   # re-cured: asked again
        self.assertEqual(len(self.posts), 2)


class DigestCapTest(_SweepHarness):
    """A15 (P1): a DISPLAY cap may never edit the RECORD."""

    def _many(self, n):
        return [_item("%02x" % i * 8, "alpha") for i in range(n)]

    def test_rows_past_the_cap_are_NOT_recorded_as_asked(self):  # noqa: VACUOUS_ASSERTION — every "not recorded" assertion is bracketed by the positive `sorted(rows_st) == sorted(shown_ids)` and `len(rows_st) == MAX_LINES` on the SAME dict: the record is non-empty and exactly the rendered set
        """THE BLOCKER, on the state file itself. The digest renders 12 lines;
        the first cut latched all 20, so 8 rows were recorded proposed-at-now
        and delivered while the owner never saw them — silent loss of exactly
        the rows this loop exists to surface.

        LOAD-BEARING MUTATION: in helm/stalebot.py restore the latch loop to
        `for it in by_owner[owner]:` (dropping the `rendered(...)[0]`).
          -> AssertionError: 20 != 12
        The digest text is UNCHANGED by that mutation, which is why the
        assertion is on the RECORD and not on the rendered text."""
        items = self._many(20)
        self.run_sweep(items)
        self.assertEqual(len(self.posts), 1)
        rows_st = stalebot.read_state(self.state).get("rows") or {}
        self.assertEqual(len(rows_st), stalebot.MAX_LINES)
        shown_ids = [it["id"] for it in items[:stalebot.MAX_LINES]]
        dropped_ids = [it["id"] for it in items[stalebot.MAX_LINES:]]
        self.assertEqual(sorted(rows_st), sorted(shown_ids))
        for rid in dropped_ids:
            self.assertFalse(rid in rows_st,
                             "%s was capped out of the digest yet recorded as "
                             "asked" % rid[:12])

    def test_the_digest_DECLARES_the_rows_it_did_not_show(self):
        """web_land's closed_total shape: the capped list and the true total
        travel together, so a reader cannot mistake the page for the
        population — and the disclosure says the remainder is NOT latched."""
        self.run_sweep(self._many(20))
        post = self.posts[0]
        self.assertTrue("20 aged or cure-awaiting row(s)" in post, post)
        self.assertTrue("+8 more row(s)" in post, post)
        self.assertTrue("NOT recorded as asked" in post, post)

    def test_the_capped_rows_are_PROPOSED_on_the_very_next_sweep(self):  # noqa: VACUOUS_ASSERTION — the latched COUNT is bracketed by len(posts)==2 and by naming each dropped id in posts[1]; a dead second sweep fails both
        """The cap must DELAY a row, never lose it. The second sweep finds the
        first 12 latched and proposes the remaining 8 — measured as room
        lines, and the ids are named."""
        items = self._many(20)
        self.run_sweep(items)
        rep2 = self.run_sweep(items)
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(rep2["latched"], stalebot.MAX_LINES)
        second = self.posts[1]
        for it in items[stalebot.MAX_LINES:]:
            self.assertTrue(it["id"][:12] in second,
                            "%s must ride the next sweep: %s"
                            % (it["id"][:12], second))

    def test_the_overflow_is_COUNTED_in_the_report_and_the_state(self):
        rep = self.run_sweep(self._many(20))
        self.assertEqual(rep["capped_unlatched"], 8)
        self.assertEqual(
            stalebot.read_state(self.state).get("capped_unlatched"), 8)

    def test_four_daily_sweeps_reach_rows_beyond_three_caps(self):  # noqa: VACUOUS_ASSERTION — four unconditional posts positively control the per-row coverage assertions inside the loop
        """Never-asked rows outrank expired latches, or rows 37+ starve forever."""
        items = self._many(48)
        start = time.time()
        for day in range(4):
            self.run_sweep(items, now=start + day * 86400)
        self.assertEqual(len(self.posts), 4)
        wire = "\n".join(self.posts)
        for item in items:
            self.assertIn(item["id"][:12], wire)


class RearmSuspensionTest(_SweepHarness):
    """A16: an UNKNOWN source must not erase latches."""

    def run_with_unavailable(self, items, unavailable, post_ok=True):
        def fake_post(_owner, text):
            self.posts.append(text)
            return post_ok
        with mock.patch.object(stalebot, "collect",
                               return_value=(items, list(unavailable), 0)), \
             mock.patch.object(stalebot, "classify_dispatch",
                               return_value=(stalebot.KEEP, "e", "")), \
             mock.patch.object(stalebot, "_post", side_effect=fake_post):
            return stalebot.sweep(post=True, state_path=self.state)

    def test_an_unreadable_source_KEEPS_the_latch_of_a_row_it_cannot_see(self):
        """THE BLOCKER: the cure scan fails, so its rows are absent from
        `items` — and the re-arm read that absence as "the row left the
        population" and dropped the latch, so the next sweep re-asked an owner
        about rows they had already been asked about, and the record of what
        was asked was destroyed by the failure to look.

        LOAD-BEARING MUTATION: drop the `if unavailable:` guard so re-arm
        always runs.
          -> AssertionError: {} != {<ID_A1>: ...}  (the latch was erased)"""
        self.run_with_unavailable([_item(ID_A1, "alpha")], [])
        self.assertEqual(len(self.posts), 1)
        rows_after_clean = stalebot.read_state(self.state).get("rows") or {}
        self.assertEqual(sorted(rows_after_clean), [ID_A1])
        # the row vanishes from the population, but the SOURCE was unreadable
        rep = self.run_with_unavailable([], ["cured-fix scan: git exploded"])
        kept = stalebot.read_state(self.state).get("rows") or {}
        self.assertIn(ID_A1, kept)
        self.assertEqual(rep["rearm_suspended"], True)
        self.assertEqual(
            stalebot.read_state(self.state).get("rearm_suspended"), True)

    def test_a_COMPLETE_read_still_rearms(self):  # noqa: VACUOUS_ASSERTION — THIS ARM IS ITSELF THE POSITIVE CONTROL for its sibling, and its own empty-rows assertion is bracketed by the preceding assertEqual proving the latch was WRITTEN first
        """The positive control on the same observable: with every source
        readable, the vanished row's latch IS dropped — so the arm above
        measures the unavailable guard rather than a re-arm that never runs."""
        self.run_with_unavailable([_item(ID_A1, "alpha")], [])
        self.assertEqual(sorted(stalebot.read_state(self.state)["rows"]),
                         [ID_A1])
        rep = self.run_with_unavailable([], [])
        self.assertEqual(stalebot.read_state(self.state).get("rows"), {})
        self.assertEqual(rep["rearm_suspended"], False)


class SourceWakeTest(_SweepHarness):

    def _source_sweep(self, problems, calls):
        with mock.patch.object(stalebot, "collect",
                               return_value=([], problems, 0)), \
             mock.patch.object(stalebot, "_post",
                               side_effect=lambda owner, text:
                               calls.append((owner, text)) or True):
            return stalebot.sweep(post=True, state_path=self.state)

    def test_an_unreadable_repo_is_an_addressed_integrator_obligation(self):
        calls = []
        problem = "cured-fix scan: repo '/gone/.git' is unreadable — 2 row(s) UNKNOWN"
        rep = self._source_sweep([problem], calls)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], _integrator())
        self.assertIn(problem, calls[0][1])
        self.assertEqual(rep["posted"], [_integrator()])
        self.assertEqual(rep["alerts"][0]["terminal"],
                         stalebot.SOURCE_UNAVAILABLE)

    def test_mutable_error_detail_and_count_do_not_mint_a_new_source_identity(self):
        calls = []
        first = "cured-fix scan ('/gone/.git'): timeout — 2 row(s) UNKNOWN"
        second = "cured-fix scan ('/gone/.git'): lock failed — 9 row(s) UNKNOWN"
        rep1 = self._source_sweep([first], calls)
        rep2 = self._source_sweep([second], calls)
        self.assertEqual(rep1["alerts"][0]["id"], rep2["alerts"][0]["id"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(rep2["latched"], 1)

    def test_structured_repo_source_key_survives_count_changes_without_collapsing(self):
        calls = []
        first = stalebot._SourceProblem(
            "cured-fix:/repo-a.git",
            "cured-fix scan: no verified checkout — 2 rows UNKNOWN")
        changed = stalebot._SourceProblem(
            "cured-fix:/repo-a.git",
            "cured-fix scan: no verified checkout — 9 rows UNKNOWN")
        other = stalebot._SourceProblem(
            "cured-fix:/repo-b.git",
            "cured-fix scan: no verified checkout — 9 rows UNKNOWN")
        rep1 = self._source_sweep([first], calls)
        rep2 = self._source_sweep([changed, other], calls)
        ids1 = {alert["id"] for alert in rep1["alerts"]}
        ids2 = {alert["id"] for alert in rep2["alerts"]}
        self.assertTrue(ids1.issubset(ids2))
        self.assertEqual(len(ids2), 2)

    def test_one_recovered_source_rearms_while_another_remains_unavailable(self):
        calls = []
        gone = "cured-fix scan ('/gone/.git'): timeout"
        tasks = "task ledger: unreadable"
        first = self._source_sweep([gone, tasks], calls)
        ids = {alert["evidence"]: alert["id"] for alert in first["alerts"]}
        self.assertEqual(len(ids), 2)
        second = self._source_sweep([tasks], calls)
        rows = stalebot.read_state(self.state).get("rows") or {}
        self.assertNotIn(ids[gone], rows)
        self.assertIn(ids[tasks], rows)
        self.assertEqual(second["rearm_suspended"], True)


class RunnableDoorTest(_RepoFixture):
    """The advertised command executes the real owner-layer persistence path."""

    def _cured_parent(self):
        import tempfile
        from helm import seats
        state = tempfile.mkdtemp(prefix="stale-real-ledger-")
        self.addCleanup(__import__("shutil").rmtree, state, ignore_errors=True)
        ledger = os.path.join(state, "dispatches.jsonl")
        branch = "redispatch-" + self._testMethodName
        _git(self.repo, "checkout", "-q", "-b", branch, self.ca)
        self.addCleanup(_git, self.repo, "branch", "-D", branch)
        _write(self.repo, branch + ".txt", "cure\n")
        _git(self.repo, "add", branch + ".txt")
        _git(self.repo, "commit", "-qm", "cure for redispatch test")
        cure = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "main")
        roster = {"seat-a": {"session": "author-sid"},
                  "seat-c": {"session": "reviewer-sid"},
                  "operator": {"session": "operator-sid"}}
        patches = [
            mock.patch.object(dispatches, "ledger_path", return_value=ledger),
            mock.patch.object(dispatches, "home_repo_id",
                              return_value=(self.repo_id, None)),
            mock.patch.object(dispatches, "_acting_author",
                              return_value=("operator", None)),
            mock.patch.object(dispatches.home, "session_id",
                              return_value="operator-sid"),
            mock.patch.object(seats, "roster_checked",
                              return_value=(roster, False)),
            mock.patch.object(stalebot, "_live_roster_seats",
                              return_value=set(roster)),
            mock.patch.object(seats, "beacon_procs", return_value=([123], None)),
            mock.patch.object(stalebot, "_author_walled",
                              return_value=(False, "healthy")),
            mock.patch.object(stalebot, "_wake_walled",
                              return_value=(False, "healthy")),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        parent = dispatches.add(
            "seat-c", "UI cleanup $(false); review", ref=self.ca,
            repo=self.repo, kind="review", new_work=True, notify=False)
        self.assertIsNotNone(parent)
        fixed, err = dispatches.mark_verdict(
            parent["id"], self.ca, "findings", polarity="fix", basis="measured")
        self.assertIsNone(err)
        self.assertEqual(fixed["polarity"], "fix")
        return fixed, cure, roster

    def test_generated_successor_persists_exact_authority_and_delivery(self):
        import contextlib, io
        parent, cure, roster = self._cured_parent()
        term, _evidence, door, owner = stalebot.classify_cured(
            parent, ("redispatch-cure", cure, 1), roster, live=set(roster))
        self.assertEqual((term, owner), (stalebot.REDISPATCH, "operator"))
        argv = shlex.split(door)
        self.assertEqual(argv[:3], ["helm", "stale", "redispatch"])
        with contextlib.redirect_stdout(io.StringIO()):
            rc = stalebot.cmd_stale(argv[2:])
        self.assertEqual(rc, 0)
        rows = dispatches.rows()
        children = [row for row in rows.values()
                    if row.get("supersedes") == parent["id"]]
        self.assertEqual(len(children), 1)
        child = children[0]
        self.assertEqual(child["tip"], cure)
        self.assertEqual(child["kind"], "review")
        self.assertEqual(child["repo_id"], self.repo_id)
        self.assertEqual(child["repo_root"], self.repo)
        self.assertEqual(child["recipient"], "seat-c")
        self.assertEqual(child["delivery"], "observed")

    def test_first_attempt_preserves_cure_diagnostic_before_any_append(self):  # noqa: VACUOUS_ASSERTION — object identity with the exact typed diagnostic is the unconditional positive control before ledger absence
        parent, _cure, _roster = self._cured_parent()
        diagnostic = dispatches.CureDiagnostic("ambiguous carrier", ambiguous=1)
        before = set(dispatches.rows())
        with mock.patch.object(dispatches, "cured_unwitnessed",
                               return_value=([], diagnostic)):
            got, why, sent = stalebot.redispatch_cured(parent["id"], "seat-c")
        self.assertIsNone(got)
        self.assertFalse(sent)
        self.assertIs(why, diagnostic)
        self.assertEqual(set(dispatches.rows()), before)

    def test_mixed_case_at_reviewer_uses_canonical_target_after_persist(self):
        from helm import seats
        parent, _cure, _roster = self._cured_parent()
        seen = []

        def beacon(target, strict=False):
            seen.append(target)
            return [123], None

        with mock.patch.object(seats, "beacon_procs", side_effect=beacon):
            row, why, sent = stalebot.redispatch_cured(parent["id"], "@Seat-C")
        self.assertIsNone(why, why)
        self.assertTrue(sent)
        self.assertIsNotNone(row)
        self.assertEqual(seen, ["seat-c", "seat-c"])

    def test_post_persist_beacon_failure_reconciles_the_exact_successor(self):
        from helm import seats
        parent, _cure, _roster = self._cured_parent()
        with mock.patch.object(seats, "beacon_procs",
                               side_effect=(([123], None), ([], None))):
            first, why, sent = stalebot.redispatch_cured(parent["id"], "seat-c")
        self.assertIsNotNone(first)
        self.assertFalse(sent)
        self.assertIn("successor recorded", why)
        again, retry_why, retry_sent = stalebot.redispatch_cured(
            parent["id"], "seat-c")
        self.assertIsNone(retry_why)
        self.assertTrue(retry_sent)
        self.assertEqual(again["id"], first["id"])
        children = [row for row in dispatches.rows().values()
                    if row.get("supersedes") == parent["id"]]
        self.assertEqual([row["id"] for row in children], [first["id"]])

    def test_a_cancelled_successor_yields_a_FRESH_active_retry(self):
        """THE LOOP THIS CLOSES: cancel the successor and the cure is unwitnessed
        again, so the sweep re-proposes the row — but the retry re-derived the
        SAME operation key, reconciled onto the CANCELLED row and reported
        success. No live review was ever minted again, and CURE AWAITING REVIEW
        repeated every window with nobody able to end it. The operator door has
        to actually work: a spent successor advances the operation identity.

        MUST REJECT: a retry while the successor is still OPEN, before AND
        after the fresh mint. Both must return the SAME row — a cure that
        minted a new live review on every redispatch would satisfy a
        different-id assertion and be strictly worse than the loop."""
        parent, cure, _roster = self._cured_parent()
        first, why, sent = stalebot.redispatch_cured(parent["id"], "seat-c")
        self.assertIsNone(why, why)
        self.assertTrue(sent)
        same, why, sent = stalebot.redispatch_cured(parent["id"], "seat-c")
        self.assertIsNone(why, why)
        self.assertTrue(sent)
        self.assertEqual(same["id"], first["id"])       # must-miss, live row
        _cancelled, err = dispatches.mark_cancel(
            first["id"], "reviewer stood down")
        self.assertIsNone(err, err)
        self.assertEqual(dispatches.rows()[first["id"]]["status"], "cancelled")
        again, why, sent = stalebot.redispatch_cured(parent["id"], "seat-c")
        self.assertIsNone(why, why)
        self.assertTrue(sent)
        self.assertNotEqual(again["id"], first["id"])
        live = dispatches.rows()[again["id"]]
        self.assertEqual(live["supersedes"], parent["id"])
        self.assertEqual(live["tip"], cure)
        self.assertEqual(live["status"], "open")
        self.assertEqual(live["delivery"], "observed")
        snap = dispatches.rows()
        carrier = dispatches.carrier(snap[parent["id"]], snap)
        self.assertIsNotNone(carrier, "the cure is witnessed by a LIVE row")
        self.assertEqual(carrier["id"], again["id"])
        stable, why, sent = stalebot.redispatch_cured(parent["id"], "seat-c")
        self.assertIsNone(why, why)
        self.assertTrue(sent)
        self.assertEqual(stable["id"], again["id"])     # must-miss, no nonce
        self.assertEqual(len([r for r in snap.values()
                              if r.get("supersedes") == parent["id"]]), 2)

    def test_a_WITHDRAWN_successor_that_reviewed_the_cure_is_not_redispatched(self):  # noqa: VACUOUS_ASSERTION — the first redispatch minting a successor and the chain census naming it are unconditional positives read before the unchanged ledger line count
        """A successor that recorded a FIX on the exact cure tip and was then
        withdrawn reviewed that cure, so redispatch refuses and appends
        nothing. CONTROL: `test_a_cancelled_successor_yields_a_FRESH_active_retry`
        drives the same producers with the successor CANCELLED, which recorded
        no verdict, and redispatch mints a fresh review. MUTATION: dropping
        the `reviewed` argument in `redispatch_cured` mints a new successor."""
        parent, cure, _roster = self._cured_parent()
        first, why, sent = stalebot.redispatch_cured(parent["id"], "seat-c")
        self.assertIsNone(why, why)
        self.assertTrue(sent)
        _v, err = dispatches.mark_verdict(
            first["id"], cure, "findings on the cure", polarity="fix",
            basis="measured")
        self.assertIsNone(err, err)
        _lr, err = landreq.withdraw(first["id"], "left the board unlanded")
        self.assertIsNone(err, err)
        snap = dispatches.rows()
        self.assertIsNone(dispatches.carrier(snap[parent["id"]], snap),
                          "the premise failed: the withdrawn successor still "
                          "carries the parent")
        self.assertEqual(
            {t: r["id"] for t, r in dispatches.chain_reviewed_tips(
                snap[parent["id"]], snap).items()},
            {cure.lower(): first["id"]})
        ledger = dispatches.ledger_path()
        with open(ledger, encoding="utf-8") as fh:
            lines = len(fh.readlines())
        rows_before = set(snap)
        got, why, sent = stalebot.redispatch_cured(parent["id"], "seat-c")
        self.assertIsNone(got)
        self.assertFalse(sent)
        self.assertEqual(why, "row has no unique current cure identity")
        with open(ledger, encoding="utf-8") as fh:
            self.assertEqual(len(fh.readlines()), lines)
        self.assertEqual(set(dispatches.rows()), rows_before)

    def test_git_cure_ambiguity_between_outer_scan_and_lock_refuses_new_append(self):
        parent, cure, _roster = self._cured_parent()
        original = dispatches._cure_index
        calls = []

        def index(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                return original(*args, **kwargs)
            return {parent["reviewed_tip"]: (
                dispatches.CURE_AMBIGUOUS,
                (("branch-a", cure), ("branch-b", "f" * 40)))}, None

        before = set(dispatches.rows())
        with mock.patch.object(dispatches, "_cure_index", side_effect=index):
            got, why, sent = stalebot.redispatch_cured(parent["id"], "seat-c")
        self.assertIsNone(got)
        self.assertFalse(sent)
        self.assertIn("ambiguous live cure carriers", str(why))
        self.assertEqual(len(calls), 2)
        self.assertEqual(set(dispatches.rows()), before)

    def test_cure_landing_between_outer_scan_and_locked_scan_refuses_append(self):
        parent, _cure, _roster = self._cured_parent()
        original = dispatches._cure_index
        calls = []

        def index(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                return original(*args, **kwargs)
            return {}, None

        before = set(dispatches.rows())
        with mock.patch.object(dispatches, "_cure_index", side_effect=index):
            got, why, sent = stalebot.redispatch_cured(parent["id"], "seat-c")
        self.assertIsNone(got)
        self.assertFalse(sent)
        self.assertIn("no longer uniquely cure-awaiting", str(why))
        self.assertEqual(len(calls), 2)
        self.assertEqual(set(dispatches.rows()), before)

    def test_a_different_successor_refuses_atomically_after_initial_snapshot(self):
        parent, cure, _roster = self._cured_parent()
        original = dispatches._append_dispatch
        injected = []

        def append(row, **kwargs):
            if kwargs.get("cured_operation") and not injected:
                other, err = dispatches.add(
                    "seat-c", "other successor", ref=cure, repo=self.repo,
                    kind="review", supersedes=parent["id"], notify=False,
                    _reason=True)
                self.assertIsNone(err, err)
                injected.append(other)
            return original(row, **kwargs)

        with mock.patch.object(dispatches, "_append_dispatch", side_effect=append):
            got, why, sent = stalebot.redispatch_cured(parent["id"], "seat-c")
        self.assertIsNone(got)
        self.assertFalse(sent)
        self.assertIn("different or additional successor", why)
        children = [row for row in dispatches.rows().values()
                    if row.get("supersedes") == parent["id"]]
        self.assertEqual([row["id"] for row in children], [injected[0]["id"]])

    def test_self_reviewer_refuses_before_any_successor_is_written(self):  # noqa: VACUOUS_ASSERTION — the exact self-delivery refusal positively proves preflight ran before ledger identity is compared
        import contextlib, io
        parent, _cure, _roster = self._cured_parent()
        before = set(dispatches.rows())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = stalebot.cmd_stale(
                ["redispatch", parent["id"], "--reviewer", "operator"])
        self.assertEqual(rc, 1)
        self.assertIn("self-delivery", err.getvalue())
        self.assertEqual(set(dispatches.rows()), before)


class CuredCollectTest(_RepoFixture):
    """The CURED population at collect() — membership is cured_unwitnessed's,
    REUSED, never a second census (A11)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # the cure: one commit AHEAD of the reviewed tip on a live branch —
        # exactly cure_state's CURE_AWAITING shape (branch tip != reviewed tip)
        _git(cls.repo, "checkout", "-q", "-b", "cureb", cls.ct)
        _write(cls.repo, "a.txt", "alpha one\nalpha two\nalpha cure\n")
        _git(cls.repo, "commit", "-aqm", "the cure",
             date="2026-01-01T04:00:00Z")
        cls.cure_tip = _git(cls.repo, "rev-parse", "HEAD")
        _git(cls.repo, "checkout", "-q", "main")

    def _collect(self, snap, **cured_patch):
        now = time.time()
        patches = [
            mock.patch.object(stalebot.landreq, "stalls",
                              return_value=([], None)),
            mock.patch.object(stalebot.dispatches, "rows", return_value={}),
            mock.patch.object(stalebot.dispatches, "snapshot",
                              return_value=(snap, None)),
            mock.patch.object(stalebot.dispatches, "owed", return_value=[]),
            mock.patch.object(stalebot.dispatches, "live_claims",
                              return_value={}),
            mock.patch.object(stalebot.tasks, "open_rows", return_value=[]),
        ]
        if cured_patch:
            patches.append(mock.patch.object(
                stalebot.dispatches, "cured_unwitnessed", **cured_patch))
        started = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        out = stalebot.collect(now, repo=self.repo, trunk="main")
        return out + (started[-1] if cured_patch else None,)

    def test_open_and_verdicted_rows_never_enter_while_the_cured_one_does(self):
        """(b), against the REAL classifier over the fixture repo: the FIX
        row whose reviewed tip a live branch carries one-ahead enters; the
        OPEN row (no verdict) and the APPROVE-verdicted row are excluded by
        the classifier this sweep CALLS — the cured item doubles as the
        must-hit proving the scan ran."""
        cured = {"id": "ce" * 8, "kind": "review", "polarity": "fix",
                 "status": "verdict", "reviewed_tip": self.ct,
                 "repo_id": self.repo_id,
                 "sender": "alpha", "recipient": "rev", "lane": "cure-lane",
                 "ts": "2026-01-01T02:00:00Z"}
        opened = {"id": "0b" * 8, "kind": "review", "status": "open",
                  "reviewed_tip": self.ct, "sender": "alpha"}
        approved = {"id": "ad" * 8, "kind": "review", "polarity": "approve",
                    "status": "verdict", "reviewed_tip": self.ct,
                    "sender": "alpha"}
        snap = {r["id"]: r for r in (cured, opened, approved)}
        items, unavailable, _nd, _stub = self._collect(snap)
        self.assertEqual(unavailable, [])
        self.assertEqual([it["id"] for it in items], ["ce" * 8])
        it = items[0]
        self.assertEqual(it["kind"], "cured")
        self.assertEqual(it["where"], ("cureb", self.cure_tip, 1))
        self.assertTrue("review never re-dispatched" in it["why_aged"],
                        it["why_aged"])

    def test_membership_is_the_classifiers_answer_not_a_second_census(self):
        """A11: patch the ONE classifier and collect mirrors it verbatim,
        threading the SAME repo and trunk — a collect that re-derived
        membership would keep its own answer and never call."""
        row = {"id": "aa" * 8, "polarity": "fix", "sender": "alpha",
               "reviewed_tip": "e" * 40, "repo_id": self.repo_id,
               "ts": "2026-01-01T02:00:00Z"}
        snap = {row["id"]: row}
        items, unavailable, _nd, stub = self._collect(
            snap, return_value=([(row, ("br", "e" * 40, 3))], None))
        self.assertEqual(unavailable, [])
        self.assertEqual([it["id"] for it in items], ["aa" * 8])
        self.assertEqual(items[0]["where"], ("br", "e" * 40, 3))
        self.assertEqual(stub.call_count, 1)
        self.assertEqual(stub.call_args.kwargs.get("root"), self.repo)
        self.assertEqual(stub.call_args.kwargs.get("trunk"),
                         "refs/heads/main")

    def test_an_unreadable_cure_scan_is_reported_never_a_false_zero(self):
        """cured_unwitnessed's own law, honoured at the sweep: ([], err) must
        surface as UNAVAILABLE — an empty result and a broken walk are
        indistinguishable to a reader otherwise."""
        row = {"id": "aa" * 8, "polarity": "fix", "sender": "alpha",
               "reviewed_tip": "e" * 40, "repo_id": self.repo_id}
        items, unavailable, _nd, _stub = self._collect(
            {row["id"]: row},
            return_value=([], "for-each-ref failed: 128"))
        self.assertEqual(items, [])
        self.assertTrue(any("cured-fix scan: for-each-ref failed: 128" in u
                            for u in unavailable), unavailable)


class MultiRepoCureTest(unittest.TestCase):
    """A18: the LEDGER is global; a GIT INDEX is not."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.mkdtemp(prefix="stalebot-multirepo-")
        cls.repos, cls.tips = {}, {}
        for name in ("alpha", "beta"):
            path = os.path.join(cls.tmp, name)
            os.makedirs(path)
            _git(path, "init", "-q", "-b", "main")
            _write(path, "a.txt", "base\n")
            _git(path, "add", ".")
            _git(path, "commit", "-qm", "c0", date="2026-01-01T00:00:00Z")
            _git(path, "checkout", "-q", "-b", "cureb")
            _write(path, "a.txt", "base\nwork\n")
            _git(path, "commit", "-aqm", "the reviewed work",
                 date="2026-01-01T01:00:00Z")
            reviewed = _git(path, "rev-parse", "HEAD")
            _write(path, "a.txt", "base\nwork\ncure\n")
            _git(path, "commit", "-aqm", "the cure",
                 date="2026-01-01T02:00:00Z")
            cure = _git(path, "rev-parse", "HEAD")
            _git(path, "checkout", "-q", "main")
            cls.repos[name] = path
            cls.tips[name] = (reviewed, cure,
                              dispatches._repo_info(path)["repo_id"])

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _row(self, name, rid):
        reviewed, _cure, repo_id = self.tips[name]
        return {"id": rid, "kind": "review", "polarity": "fix",
                "status": "verdict", "reviewed_tip": reviewed,
                "repo_id": repo_id, "repo_root": self.repos[name],
                "sender": "seat-a", "recipient": "seat-c",
                "lane": "l-" + name, "ts": "2026-01-01T03:00:00Z"}

    def _collect(self, snap, trunk="main", repo=None):
        with mock.patch.object(stalebot.landreq, "stalls",
                               return_value=([], None)), \
             mock.patch.object(stalebot.dispatches, "rows", return_value={}), \
             mock.patch.object(stalebot.dispatches, "snapshot",
                               return_value=(snap, None)), \
             mock.patch.object(stalebot.dispatches, "owed", return_value=[]), \
             mock.patch.object(stalebot.dispatches, "live_claims",
                               return_value={}), \
             mock.patch.object(stalebot.tasks, "open_rows", return_value=[]):
            return stalebot.collect(time.time(), repo=repo, trunk=trunk)

    def test_a_cure_in_EACH_repo_is_found_against_ITS_OWN_index(self):
        """THE BLOCKER: one shared cwd index answered "no live branch carries
        this tip" for every row belonging to any other repository, which
        `cure_state` reads as NO BRANCH — the cure reported not to exist. A
        silent, whole-repo false negative that would grow with every repo the
        fleet works in.

        BOTH ids are asserted present: a single-index implementation finds at
        most one of them, whichever repo it happened to be pointed at."""
        a_id, b_id = "a1" * 8, "b1" * 8
        snap = {a_id: self._row("alpha", a_id), b_id: self._row("beta", b_id)}
        items, unavailable, _nd = self._collect(snap)
        self.assertEqual(unavailable, [])
        self.assertEqual(sorted(it["id"] for it in items), sorted([a_id, b_id]))
        found = {it["id"]: it["where"] for it in items}
        self.assertEqual(found[a_id],
                         ("cureb", self.tips["alpha"][1], 1))
        self.assertEqual(found[b_id],
                         ("cureb", self.tips["beta"][1], 1))

    def test_each_repo_uses_its_resolved_local_trunk_not_origin_main(self):  # noqa: VACUOUS_ASSERTION — both concrete repository row ids positively prove local trunk resolution found the cures
        """These fixtures have no origin; hard-coded origin/main finds nothing."""
        a_id, b_id = "a1" * 8, "b1" * 8
        snap = {a_id: self._row("alpha", a_id),
                b_id: self._row("beta", b_id)}
        items, unavailable, _nd = self._collect(snap, trunk=None)
        self.assertEqual(unavailable, [])
        self.assertEqual(sorted(item["id"] for item in items),
                         sorted((a_id, b_id)))

    def test_explicit_repo_excludes_foreign_and_unbound_rows(self):
        a_id, b_id, legacy_id = "a1" * 8, "b1" * 8, "c1" * 8
        legacy = self._row("alpha", legacy_id)
        legacy.pop("repo_id")
        snap = {a_id: self._row("alpha", a_id),
                b_id: self._row("beta", b_id), legacy_id: legacy}
        items, unavailable, _nd = self._collect(
            snap, repo=self.repos["alpha"], trunk="main")
        self.assertEqual(unavailable, [])
        self.assertEqual([item["id"] for item in items], [a_id])

    def test_a_valid_sibling_checkout_recovers_a_stale_recorded_placement(self):
        parent_id, sibling_id = "a1" * 8, "a2" * 8
        parent = self._row("alpha", parent_id)
        parent["repo_root"] = os.path.join(self.tmp, "deleted-checkout")
        sibling = self._row("alpha", sibling_id)
        sibling.update(status="open", polarity=None,
                       repo_root=self.repos["alpha"])
        items, unavailable, _nd = self._collect(
            {parent_id: parent, sibling_id: sibling})
        self.assertEqual(unavailable, [])
        self.assertEqual([item["id"] for item in items], [parent_id])

    def test_two_verified_worktrees_of_one_repo_scan_instead_of_UNKNOWN(self):  # noqa: VACUOUS_ASSERTION — the exact cured row id, its branch/tip/ahead, and the identical cured_by_repo row set are unconditional positive controls on the same scan the empty problem lists describe
        """SEAT-PER-WORKTREE GUARANTEES THIS SHAPE. Each seat's rows carry its
        own worktree path, and every worktree of one repository shares ONE
        common-dir, so two VERIFIED placements under one repo_id is the normal
        steady state rather than an ambiguity. Calling it UNKNOWN blinded the
        scheduled global sweep — which passes no explicit repo — for the whole
        repository the moment a second writer appeared, on exactly the rows the
        sweep exists to surface. A linked worktree shares refs/heads and the
        object store with its siblings, so `_cure_index` reads the SAME answer
        from any of them and there is nothing to disambiguate. `cured_by_repo`,
        the other canonical consumer of this population, already resolves the
        placement by taking the first verified carried checkout; both consumers
        are asserted here to agree on one snapshot.

        MUST REJECT: a carried repo_root that does NOT verify against this
        repo_id. The foreign checkout below is a real work tree whose path
        sorts BEFORE every alpha placement, so a cure that merely took the
        lexically-first carried path would measure alpha's cure against beta's
        tree and report the cure missing."""
        parent_id, sibling_id, foreign_id = "a1" * 8, "a2" * 8, "a3" * 8
        worktree = os.path.join(self.tmp, "alpha-second-checkout")
        _git(self.repos["alpha"], "worktree", "add", "-q", "--detach",
             worktree, "main")
        self.addCleanup(_git, self.repos["alpha"], "worktree", "remove",
                        "--force", worktree)
        foreign = os.path.join(self.tmp, "aaa-foreign-checkout")
        _git(self.repos["beta"], "worktree", "add", "-q", "--detach",
             foreign, "main")
        self.addCleanup(_git, self.repos["beta"], "worktree", "remove",
                        "--force", foreign)
        self.assertLess(foreign, self.repos["alpha"])
        self.assertIsNone(
            stalebot._verified_placement(self.tips["alpha"][2], foreign),
            "the must-reject candidate has to be genuinely foreign")
        parent = self._row("alpha", parent_id)
        sibling = self._row("alpha", sibling_id)
        sibling.update(status="open", polarity=None, repo_root=worktree)
        stale = self._row("alpha", foreign_id)
        stale.update(status="open", polarity=None, repo_root=foreign)
        snap = {parent_id: parent, sibling_id: sibling, foreign_id: stale}
        items, unavailable, _nd = self._collect(snap)
        self.assertEqual(unavailable, [])
        self.assertEqual([item["id"] for item in items], [parent_id])
        self.assertEqual(items[0]["where"], ("cureb", self.tips["alpha"][1], 1))
        rows, problems, facts = dispatches.cured_by_repo(snap, trunk="main")
        self.assertEqual(problems, [])
        self.assertEqual([row.get("id") for row, _where in rows], [parent_id])
        self.assertEqual(facts["blind_repos"], 0)

    def test_explicit_repo_foreign_successor_cannot_suppress_local_cure(self):
        parent_id, child_id = "a1" * 8, "b2" * 8
        parent = self._row("alpha", parent_id)
        parent["chain_root"] = parent_id
        child = self._row("beta", child_id)
        child.update(status="open", polarity=None, supersedes=parent_id,
                     chain_root=parent_id)
        items, unavailable, _nd = self._collect(
            {parent_id: parent, child_id: child},
            repo=self.repos["alpha"], trunk="main")
        self.assertEqual(unavailable, [])
        self.assertEqual([item["id"] for item in items], [parent_id])

    def test_a_row_with_NO_repo_id_is_reported_UNKNOWN_never_judged(self):
        """The fleet's no-cwd-fallback law: an absent binding resolved against
        whatever tree the sweep happens to stand in is how one repo's history
        answers another repo's question. The bound row beside it is the
        must-hit proving the scan ran."""
        a_id, orphan = "a1" * 8, "0f" * 8
        row = self._row("alpha", orphan)
        row["repo_id"] = ""
        snap = {a_id: self._row("alpha", a_id), orphan: row}
        items, unavailable, _nd = self._collect(snap)
        self.assertEqual([it["id"] for it in items], [a_id])
        self.assertTrue(any("carry no repo_id" in u for u in unavailable),
                        unavailable)

    def test_an_UNREADABLE_repo_is_reported_and_never_silently_empty(self):
        a_id, gone = "a1" * 8, "9e" * 8
        row = self._row("alpha", gone)
        row["repo_id"] = os.path.join(self.tmp, "vanished", ".git")
        snap = {a_id: self._row("alpha", a_id), gone: row}
        items, unavailable, _nd = self._collect(snap)
        self.assertEqual([it["id"] for it in items], [a_id])
        self.assertTrue(any("unreadable" in u for u in unavailable),
                        unavailable)

    def test_a_NUL_bearing_repo_id_degrades_to_UNKNOWN_instead_of_crashing(self):
        gone = "9e" * 8
        row = self._row("alpha", gone)
        row["repo_id"] = "/tmp/bad\0repo/.git"
        items, unavailable, _nd = self._collect({gone: row})
        self.assertEqual(items, [])
        self.assertTrue(any("unreadable" in u for u in unavailable), unavailable)

    def test_whitespace_repo_id_is_reported_unknown_not_scanned_empty(self):
        gone = "9e" * 8
        row = self._row("alpha", gone)
        row["repo_id"] += " "
        with mock.patch.object(stalebot, "_git_authority_for") as authority, \
                mock.patch.object(stalebot.dispatches,
                                  "cured_unwitnessed") as cured:
            items, unavailable, _nd = self._collect({gone: row})
        self.assertEqual(items, [])
        self.assertTrue(any("unreadable" in u for u in unavailable), unavailable)
        authority.assert_not_called()
        cured.assert_not_called()


class SeparateGitDirAuthorityTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.mkdtemp(prefix="stalebot-separate-git-")
        cls.repo = os.path.join(cls.tmp, "checkout")
        cls.gitdir = os.path.join(cls.tmp, "authority.git")
        subprocess.run([
            "git", "init", "-q", "-b", "main",
            "--separate-git-dir=" + cls.gitdir, cls.repo], check=True)
        _write(cls.repo, "a.txt", "base\n")
        _git(cls.repo, "add", ".")
        _git(cls.repo, "commit", "-qm", "base")
        _git(cls.repo, "checkout", "-q", "-b", "cureb")
        _write(cls.repo, "a.txt", "base\nreviewed\n")
        _git(cls.repo, "commit", "-aqm", "reviewed")
        cls.reviewed = _git(cls.repo, "rev-parse", "HEAD")
        _write(cls.repo, "a.txt", "base\nreviewed\ncure\n")
        _git(cls.repo, "commit", "-aqm", "cure")
        cls.cure = _git(cls.repo, "rev-parse", "HEAD")
        _git(cls.repo, "checkout", "-q", "main")
        cls.repo_id = dispatches._repo_info(cls.repo)["repo_id"]

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _row(self):
        return {"id": "5a" * 8, "kind": "review", "polarity": "fix",
                "status": "verdict", "reviewed_tip": self.reviewed,
                "repo_id": self.repo_id, "sender": "seat-a",
                "recipient": "seat-c", "lane": "separate-git"}

    def test_common_dir_without_checkout_placement_stays_unknown(self):  # noqa: VACUOUS_ASSERTION — the verified common-dir identity and explicit unavailable diagnostic positively control the empty cured set
        self.assertEqual(self.repo_id, self.gitdir)
        cured, unavailable = stalebot._cured_population(
            {self._row()["id"]: self._row()}, trunk="main")
        self.assertEqual(cured, [])
        self.assertEqual(len(unavailable), 1)
        self.assertIn("no verified checkout placement", unavailable[0])

    def test_actuator_without_checkout_placement_refuses_actionably(self):
        row = self._row()
        self.assertEqual(stalebot._verified_placement(self.repo_id, self.repo),
                         self.repo)
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({row["id"]: row}, None)), \
             mock.patch.object(dispatches, "home_repo_id",
                               return_value=(self.repo_id, None)):
            successor, why, sent = stalebot.redispatch_cured(row["id"], "seat-c")
        self.assertIsNone(successor)
        self.assertFalse(sent)
        self.assertIn("no verified checkout placement", why)


class CureOutranksStallTest(unittest.TestCase):
    """A19: LR stall identity must not suppress the cure."""

    def test_a_row_that_is_BOTH_stalled_and_cured_gets_the_CURE(self):
        """THE BLOCKER: `if rid in lr_ids: continue` meant a row that was both
        an aged lr loop and cured-unwitnessed kept ONLY its lr classification
        — which knows nothing about cures — so the cure stayed invisible on
        exactly the rows that had been stalled longest.

        ONE id still yields ONE item (one latch key, never two rival asks),
        and the stall's own aging clause survives in the digest line."""
        rid = "a1" * 8
        row = {"id": rid, "polarity": "fix", "sender": "seat-a",
               "reviewed_tip": "e" * 40}
        lr = {"id": rid, "state": "REVIEW", "dwell_s": 5 * 86400,
              "owed_by": "reviewer", "reviewer": "seat-c"}
        with mock.patch.object(stalebot.landreq, "stalls",
                               return_value=([lr], None)), \
             mock.patch.object(stalebot.dispatches, "rows",
                               return_value={rid: row}), \
             mock.patch.object(stalebot.dispatches, "snapshot",
                               return_value=({rid: row}, None)), \
             mock.patch.object(stalebot.dispatches, "owed", return_value=[]), \
             mock.patch.object(stalebot.dispatches, "live_claims",
                               return_value={}), \
             mock.patch.object(stalebot, "_cured_population",
                               return_value=([(row, ("br", "f" * 40, 2))],
                                             [])), \
             mock.patch.object(stalebot.tasks, "open_rows", return_value=[]):
            items, unavailable, _nd = stalebot.collect(time.time())
        self.assertEqual(unavailable, [])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "cured")
        self.assertEqual(items[0]["where"], ("br", "f" * 40, 2))
        self.assertTrue("review never re-dispatched" in items[0]["why_aged"],
                        items[0]["why_aged"])
        self.assertTrue("REVIEW stage threshold" in items[0]["why_aged"],
                        "the stall clause must survive: "
                        + items[0]["why_aged"])


class CanonicalWallFamilyTest(unittest.TestCase):
    """Wall routing uses the dispatch source session's verified runtime."""

    def _walled(self, roster_row, family=("famx", None)):
        from helm import seat as seatmod
        row = {"source": "source-sid"}
        with mock.patch.object(seatmod, "family_for",
                               return_value=family) as fam:
            out = stalebot._author_walled(
                row, ("Renamed-Seat", roster_row),
                wall_snapshot=({"famx": {"state": "AUTH-401",
                                          "since": "s"}}, None))
        return out, fam

    def test_source_session_runtime_outranks_the_seats_newest_runtime(self):
        roster_row = {
            "session": "newer-sid", "runtime": {"family": "other"},
            "runtime_verified": True,
            "runtime_sessions": {"source-sid": {
                "runtime": {"family": "famx"}, "verified": True}}}
        (walled, why), fam = self._walled(roster_row)
        self.assertEqual(walled, True)
        self.assertIn("AUTH-401", why)
        self.assertEqual(fam.call_args.args,
                         ("Renamed-Seat", {"family": "famx"}, True))

    def test_unavailable_source_session_metadata_is_not_replaced_by_latest(self):
        roster_row = {"session": "newer-sid",
                      "runtime": {"family": "other"},
                      "runtime_verified": True}
        (_walled, _why), fam = self._walled(roster_row)
        self.assertEqual(fam.call_args.args, ("Renamed-Seat", None, False))

    def test_source_session_resolves_a_fully_renamed_author(self):  # noqa: VACUOUS_ASSERTION — the exact renamed actor and absence of an authority error are the structural outputs under test
        row = {"sender": "old-name", "source": "source-sid"}
        actor, _r, why = stalebot._actor_for_dispatch(
            row, {"totally-new-name": {"session": "source-sid"}})
        self.assertEqual((actor, why), ("totally-new-name", None))

    def test_ambiguous_session_or_case_refuses_instead_of_choosing_first(self):
        row = {"sender": "seat-a", "source": "shared"}
        actor, _r, why = stalebot._actor_for_dispatch(
            row, {"Seat-A": {"session": "shared"},
                  "seat-a": {"session": "shared"}})
        self.assertIsNone(actor)
        self.assertIn("remembered by", why)

    def test_a_missing_source_session_never_falls_back_to_recycled_sender(self):
        row = {"sender": "seat-a", "source": "gone-sid"}
        actor, _r, why = stalebot._actor_for_dispatch(
            row, {"seat-a": {"session": "new-sid"}})
        self.assertIsNone(actor)
        self.assertIn("no current roster owner", why)

    def test_one_source_owner_still_refuses_a_case_equivalent_roster_duplicate(self):
        row = {"sender": "old-name", "source": "source-sid"}
        actor, _r, why = stalebot._actor_for_dispatch(
            row, {"Seat-A": {"session": "source-sid"},
                  "seat-a": {"session": "other-sid"}})
        self.assertIsNone(actor)
        self.assertIn("ambiguous by case", why)

    def test_live_reviewer_sessions_with_different_families_refuse(self):
        from helm import beacons, seat as seatmod
        roster_row = {"session": "sid-a", "sessions": ["sid-a", "sid-b"],
                      "runtime_sessions": {
                          "sid-a": {"runtime": {"family": "fam-a"},
                                    "verified": True},
                          "sid-b": {"runtime": {"family": "fam-b"},
                                    "verified": True}}}
        with mock.patch.object(beacons, "live_sessions",
                               return_value={"sid-a": 1, "sid-b": 2}), \
             mock.patch.object(seatmod, "family_for",
                               side_effect=lambda _n, runtime, _v:
                               (runtime["family"], None)):
            walled, why = stalebot._wake_walled(
                "reviewer", roster_row,
                ({"fam-a": {"state": "HEALTHY"},
                  "fam-b": {"state": "HEALTHY"}}, None))
        self.assertIsNone(walled)
        self.assertIn("span provider families", why)

    def test_cli_source_uses_the_current_verified_runtime(self):  # noqa: VACUOUS_ASSERTION — the exact family_for call positively proves the current verified runtime crossed the CLI sentinel boundary
        from helm import seat as seatmod
        roster_row = {"session": "current-sid",
                      "runtime": {"family": "famx"},
                      "runtime_verified": True}
        with mock.patch.object(seatmod, "family_for",
                               return_value=("famx", None)) as fam:
            walled, _why = stalebot._author_walled(
                {"source": "cli"}, ("seat-a", roster_row),
                wall_snapshot=({"famx": {"state": "HEALTHY"}}, None))
        self.assertEqual(walled, False)
        self.assertEqual(fam.call_args.args,
                         ("seat-a", {"family": "famx"}, True))


class AuthorWallTest(unittest.TestCase):

    def _wall(self, family, snap, err=None):
        from helm import seat as seatmod
        with mock.patch.object(seatmod, "family_for", return_value=family):
            return stalebot._author_walled(
                {"source": "sid"}, ("seat-a", {"session": "sid"}),
                wall_snapshot=(snap, err))

    def test_a_dark_state_walls_and_HEALTHY_does_not_on_the_same_family(self):  # noqa: VACUOUS_ASSERTION — the dark-state result positively controls the same-family HEALTHY non-wall assertion
        from helm import proxywatch
        dark = sorted(proxywatch._UPSTREAM_DARK)[0]
        walled, why = self._wall(("famx", None),
                                 {"famx": {"state": dark, "since": "s"}})
        self.assertEqual(walled, True)
        self.assertIn(dark, why)
        walled2, _why2 = self._wall(("famx", None),
                                    {"famx": {"state": "HEALTHY"}})
        self.assertEqual(walled2, False)

    def test_cached_UNKNOWN_with_a_dark_latch_remains_paused(self):
        walled, why = self._wall(
            ("famx", None),
            {"famx": {"state": "UNKNOWN", "dark": True, "since": "s"}})
        self.assertEqual(walled, True)
        self.assertIn("dark latch held", why)

    def test_no_proxy_family_is_NOT_walled(self):
        walled, why = self._wall((None, None), None, "must never be read")
        self.assertEqual(walled, False)
        self.assertIn("no proxy family", why)

    def test_family_resolution_error_is_UNKNOWN_never_unwalled(self):
        walled, why = self._wall((None, "runtime unavailable"), {})
        self.assertIsNone(walled)
        self.assertIn("unavailable", why)

    def test_an_unreadable_snapshot_is_UNKNOWN_never_healthy(self):
        walled, why = self._wall(("famx", None), None,
                                 "proxywatch state unreadable: x")
        self.assertIsNone(walled)
        self.assertIn("unreadable", why)

    def test_a_family_with_no_recorded_verdict_is_UNKNOWN(self):
        walled, why = self._wall(("famx", None), {})
        self.assertIsNone(walled)
        self.assertIn("no readable famx family record", why)


class CliHonestyTest(unittest.TestCase):

    def test_root_help_discovers_sweep_and_mutating_redispatch(self):
        import contextlib, io
        from helm import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["--help"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("stale sweep", text)
        self.assertIn("redispatch <dispatch-id>", text)
        self.assertIn("explicit mutating actuator", text)

    def test_redispatch_help_is_successful_and_names_mutability(self):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = stalebot.cmd_stale(["redispatch", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("redispatch <dispatch-id>", out.getvalue())
        self.assertIn("records and delivers", out.getvalue())

    def test_redispatch_threads_an_explicit_checkout_placement(self):
        import contextlib, io
        row = {"id": "d" * 32, "recipient": "seat-c", "lane": "review"}
        with mock.patch.object(stalebot, "redispatch_cured",
                               return_value=(row, None, True)) as acted, \
             contextlib.redirect_stdout(io.StringIO()):
            rc = stalebot.cmd_stale([
                "redispatch", "a" * 32, "--reviewer", "seat-c",
                "--repo", "/placed/checkout"])
        self.assertEqual(rc, 0)
        acted.assert_called_once_with(
            "a" * 32, "seat-c", repo="/placed/checkout")

    def test_an_unknown_subverb_refuses_exit_2(self):
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = stalebot.cmd_stale(["bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown subverb", err.getvalue())

    def test_junk_after_sweep_refuses_before_any_work(self):
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = stalebot.cmd_stale(["sweep", "--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("--bogus", err.getvalue())


class TheIntegratorIsResolvedNotSpelledTest(_CuredSweepHarness):
    """The seat that reads an unowned row is a ROLE, asked at the moment of use.

    THE DEFECT A MODULE CONSTANT BUILDS IN. `INTEGRATOR = "<a seat name>"`
    keeps naming that seat after the roster stops carrying it, and every
    surface downstream reports a delivery it never made: the constant is
    truthy, the digest is keyed by it, the post is written, and the send door
    resolves the addressee to nobody while the sweep reports success.

    AND THE TWO USES ARE NOT THE SAME USE. A RECIPIENT needs a name a send can
    reach, so it takes the loud-default door. An IDENTITY CHECK — is this
    reviewer the integrator? — is a THREE-state question, and an integrator
    nobody can resolve does not fail the comparison, it makes it unanswerable.
    Answering False there advertises a door that self-delivery may refuse at
    send time; answering True asserts a collision nobody measured.
    """

    def test_the_resolver_separates_UNREADABLE_from_ABSENT(self):
        live = {seats_integrator.INTEGRATOR_SEAT_DEFAULT: {"session": "sid"}}
        # POSITIVE CONTROL FIRST, unconditional and on the same call: a roster
        # carrying an integrator resolves, so a None below is about the state
        # under test and not about the resolver declining everything.
        seat, why = stalebot._integrator_for(live, False)
        self.assertEqual(seat, seats_integrator.INTEGRATOR_SEAT_DEFAULT)
        self.assertIsNone(why)

        unreadable, unreadable_why = stalebot._integrator_for({}, True)
        self.assertIsNone(unreadable)
        self.assertIn("could not be read", unreadable_why)

        absent, absent_why = stalebot._integrator_for({}, False)
        self.assertIsNone(absent)
        self.assertIn("does not resolve", absent_why)
        # THE TWO REASONS MUST NOT BE THE SAME SENTENCE. A proven-empty roster
        # and an unreadable one are the distinction the tri-state exists for,
        # and a caller that renders `why` is the only surface a reader sees.
        self.assertNotEqual(absent_why, unreadable_why)

    def test_an_UNRESOLVABLE_integrator_costs_the_CLAIM_not_the_DOOR(self):
        """The discriminating pair, and the second half is the one that has to
        be right: an unanswerable question may not cost the answer to a
        different one.

        With a resolved integrator that IS the reviewer, self-delivery would
        genuinely refuse, so the door is withheld. With NO resolvable
        integrator nobody has CHECKED whether they collide — but the
        redispatch door still works, and refusing it would destroy a working
        capability on the strength of a fact nobody measured. So the terminal
        keeps its door and the unanswered question rides as a clause."""
        from helm import seats
        who = "seat-a-integrator"

        def classify(roster, recipient, failed=False):
            item = _cured_item(ID_C1, None)
            item["row"]["recipient"] = recipient
            with mock.patch.object(stalebot, "_author_walled",
                                   return_value=(False, "healthy")), \
                 mock.patch.object(stalebot, "_wake_walled",
                                   return_value=(False, "healthy")), \
                 mock.patch.object(stalebot, "_worktree_for",
                                   return_value="/repo"), \
                 mock.patch.object(seats, "beacon_procs",
                                   return_value=([123], None)):
                return stalebot.classify_cured(
                    item["row"], item["where"], roster,
                    roster_failed=failed, live=set(roster))

        # RESOLVED, AND THE REVIEWER IS THE INTEGRATOR: a real collision.
        term, evidence, door, owner = classify(
            {who: {"session": "sid"}}, who)
        self.assertEqual((term, owner), (stalebot.PROXY_REDISPATCH, ""))
        self.assertIn("self-delivery would refuse", evidence)
        self.assertIn(who, evidence)
        self.assertEqual(door, "")

        # A READABLE ROSTER THAT CARRIES NO INTEGRATOR — and this is the
        # REACHABLE unresolvable state, measured rather than assumed. A
        # roster_failed pass never arrives here at all: `_actor_for_name`
        # cannot resolve the REVIEWER from an unreadable roster and returns
        # three branches earlier, so the only way to stand at this line with
        # no integrator is a roster that reads fine and names none.
        term, evidence, door, owner = classify(
            {"seat-b": {"session": "sid"}}, "seat-b")
        self.assertEqual((term, owner), (stalebot.PROXY_REDISPATCH, ""))
        # THE DOOR SURVIVES, and this is the assertion the arm exists for. A
        # refusal here would be a claim about the REDISPATCH capability made
        # on the strength of an unmeasured fact about a DIFFERENT question,
        # and the door works whether or not anyone can name the integrator.
        self.assertTrue(door, "an unresolvable integrator destroyed a door "
                              "that still works")
        # AND THE UNANSWERED QUESTION IS STATED rather than silently dropped.
        self.assertIn("WHO would operate is UNKNOWN", evidence)
        self.assertIn("never measured", evidence)

    def test_a_fingerprint_carries_the_ROLE_so_a_rename_does_not_repropose(self):
        """A fingerprint answers IS THIS THE SAME ASK. Keying an unowned row on
        the integrator's current NAME makes every latched row re-propose the
        day that seat is renamed, and a rename does not change the ask."""
        unowned = {"kind": "task", "terminal": stalebot.PROXY_REDISPATCH,
                   "owner": "", "reviewer": "", "door": ""}
        owned = dict(unowned, owner="seat-a")
        # POSITIVE CONTROL: an OWNED row still keys on its owner, so the
        # assertion below is about the unowned branch and not about the key
        # having stopped carrying an actor at all.
        self.assertIn("actor=seat-a", stalebot._fingerprint(owned))

        key = stalebot._fingerprint(unowned)
        self.assertIn("actor=" + stalebot.UNOWNED_ACTOR, key)
        # AND THE KEY SURVIVES A RENAME, which is the property, measured
        # rather than argued: resolving a different integrator changes nothing.
        with mock.patch.object(seats_integrator, "integrator_seat",
                               return_value=("seat-b-integrator", None)):
            self.assertEqual(stalebot._fingerprint(unowned), key)



if __name__ == "__main__":
    unittest.main()
