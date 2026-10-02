#!/usr/bin/env python3
"""The LAND PIPELINE card — `GET /api/lr` and the renderer that draws it.

WHY THIS SURFACE EXISTS. helm's owner is GUI-first and reads on a phone. The
roster tab covers seats, the ledger tab covers turns, and until this card
NOTHING covered lanes: the land pipeline lived only in `helm lr list`. So every
statement about a lane reached him as an agent's prose, with nothing on screen
able to contradict it. This card is the first time the record speaks for itself.

WHAT THESE TESTS ARE FOR, in priority order:

1. UNAVAILABILITY IS LOUD. The single most important property. An unreadable
   dispatch ledger must render the UNKNOWN STRIP — not an empty happy list, not
   a stale board, not a zero. So these tests plant the failure and assert the
   STRIP RENDERS, in the rendered HTML, rather than asserting that nothing
   raised. An except/pass test would pass against a card that draws nothing at
   all, which is the exact defect.

2. THE FIELD NAMES ARE PINNED. `gate` / `ungated` / `polarity` / `review_sha`
   are read by the card to decide whether a row is VERIFIED. A rename on the
   landreq side would make every row silently fall back to its absent branch —
   and the absent branch reads UNVERIFIED, which is honest today but would be a
   LIE the moment real receipts exist and stop being found. A downgrade that
   looks like honesty is the failure mode these pin against.

3. THE RENDERER IS RUN, NOT MIRRORED. The client tests below execute the ACTUAL
   lrShow/dashLr source lifted verbatim out of the assembled web UI
   under node, following tests/test_web_chat_client_runtime.py. A python mirror
   of JS rots silently; this cannot.

Hermetic: the fixture is tests/test_landreq.py's LandReqBase (tmp HELM_HOME,
tmp chat dir, a git repo minted per test), so the real ledger and repos are
never touched.
"""
import ast
import collections
import contextlib
import errno
import io
import json
import os
import stat
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import boardread, dispatches, doctor, eventledger, gate, landreq, \
    pk, projscope, registry, web, web_cache, web_land, web_ui_loader  # noqa: E402
from helm import work  # noqa: E402
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_landreq as _landreq  # noqa: E402
from tests.test_landreq import run  # noqa: E402
from tests._ownerverbs import owner_verbs  # noqa: E402
# THE BUILDING BAND IS MEASURED AGAINST A REAL LANE TREE, so its arms borrow
# the fixture that already mints one rather than growing a second one here: a
# second definition of "a lane room" is how the two surfaces that read lanes
# come to disagree about what one is.
from tests.test_work import WorkBase, _sh  # noqa: E402
from tests.test_web_chat_client_runtime import _extract_fn  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _dispatch_bytes_denied(reason="PermissionError: denied"):
    """The DISPATCH ledger's read failing at the door it is read through —
    and only that ledger. The dispatch fold reads its ledger ONCE AS BYTES
    (task/2770), not through `checked_events`, so a failure planted only at the
    row reader no longer reaches it."""
    real = eventledger.read_bytes

    def denied(path):
        if path == dispatches.ledger_path():
            return None, reason
        return real(path)
    return mock.patch.object(eventledger, "read_bytes", denied)


# The verification axis. A rename on either of these does not break a build and
# does not raise — it just makes every row take the "absent" branch forever.
GATE_FIELDS = ("gate", "ungated")
# The RELATEDNESS axis, and it fails the same silent way: `chain_root` absent
# makes every row fall back to naming itself, which draws a board of singletons
# — the exact flat wall the grouping exists to remove, arriving as a rename
# rather than an error. `supersedes` absent makes a multi-round chain unable to
# elect the round happening NOW, so it silently shows the server's first row
# under a fold labelled "earlier rounds". Both degrade quietly and honestly,
# which is why nothing else would catch a rename.
GROUP_FIELDS = ("chain_root", "supersedes")
# Everything else the card reads off a row. Named individually on purpose: a
# set-equality assertion would fail on every future ADDITION, and the point is
# to catch removals and renames, not to freeze the shape.
CARD_FIELDS = ("id", "state", "lane", "branch", "review_sha", "review_sha_full",
               "author", "reviewer", "kind", "polarity", "polarity_source",
               "attest_source", "attest_state", "attest_detail",
               "dwell_s", "entered_ts", "dwell_known", "closed_ts", "closed_ts_unreadable",
               "closed_ts_impossible", "ledger_refused",
               "stalled", "observable", "land_state", "landed", "merged_local",
               "has_upstream", "contrary", "contrary_state",
               "contrary_provenance",
               "contrary_discharge", "discharged",
               "superseding_tip", "withdrawn", "withdraw_contradicted",
               "abandoned", "abandon_reason", "abandon_ts",
               "abandon_object_state", "abandon_proof_mode",
               "abandon_proof_version", "abandon_trunk_mention_state",
               "abandon_trunk_mention_proof_mode",
               "abandon_trunk_mention_proof_version", "abandon_branch_state",
               "abandon_branch_proof_mode", "abandon_branch_proof_version",
               "abandon_worktree_state", "abandon_worktree_proof_mode",
               "abandon_worktree_proof_version", "abandon_land_state",
               "closed_by_landing", "landing_trunk_sha", "owed_by",
               "owner_gated", "hold_ts", "hold_reason", "holder_role", "holder_seat",
               "owed_seat_standing",
               "close_reason", "close_evidence",
               "receipt_state", "timeline") \
    + GATE_FIELDS + GROUP_FIELDS


# Direct `dispatches.add` calls below pass `new_work=True` for the same reason
# LandReqBase.dispatch does: since the work-chain landed, a row must declare
# whether it is new work or supersedes a parent, and every fixture here mints an
# independent lane. Stamping --new-work on a superseding round would assert the
# defect that contract exists to prevent.
# THIS MODULE DOES NOT READ HOST LIVENESS. The full argument is in
# test_landreq; the short version is that every dispatch write here walks the
# host's whole process table (54,114 pids on the build node, 0.677s a walk) to
# consult a liveness these tests never assert on — which also made what they
# OBSERVED depend on whatever else was running beside them.
#
# DECLARED PER MODULE, not hoisted: this is a claim about THIS file that some-
# one must re-check when its tests change, and a shared helper would let a
# module inherit the claim without anyone making it. Captured at IMPORT because
# several modules install the same stand-in and a setUpModule running while
# another module's patch was live would capture the PATCH as "real".
_LIVE_SEATS_PATCH = None

from helm import proxywatch as _proxywatch_for_capture      # noqa: E402
_REAL_LIVE_SEATS = _proxywatch_for_capture._live_seats


def setUpModule():
    global _LIVE_SEATS_PATCH
    from helm import proxywatch
    _LIVE_SEATS_PATCH = mock.patch.object(
        proxywatch, "_live_seats", lambda: (set(), None, {}))
    _LIVE_SEATS_PATCH.start()


def tearDownModule():
    global _LIVE_SEATS_PATCH
    if _LIVE_SEATS_PATCH is not None:
        _LIVE_SEATS_PATCH.stop()
    # THE GLOBAL GOES BACK TO WHAT IMPORT LEFT: other modules import from this
    # one, so a stopped patcher left here is data they can reach (task/3039).
    _LIVE_SEATS_PATCH = None
    # Join every _swr_rebuild worker this module kicked, so none leaks its late
    # `_qstate` write into the next module's fixtures (task/3304).
    web_cache.drain_swr()


class TheLivenessStandInIsInEffectInWebLrTest(unittest.TestCase):
    """Per-module control. Another module having one proves nothing here:
    unittest runs module fixtures per module under test. Inherits
    `unittest.TestCase` directly so a base-class refactor cannot drop it."""

    def test_the_bound_census_is_NOT_the_real_one(self):  # noqa: VACUOUS_ASSERTION — identity against the import-time capture IS the positive control; a value assertion cannot discriminate because the build node's real census also returns an empty fleet. Mutation-tested in test_landreq (OK 0.042s with the fixture, FAILED 0.815s without).
        from helm import proxywatch
        self.assertIsNot(
            proxywatch._live_seats, _REAL_LIVE_SEATS,
            "the liveness stand-in is NOT in effect in this module — it is "
            "reading the real host again")


class LrApiBase(_landreq.LandReqBase):
    def setUp(self):
        super().setUp()
        # THE LANDS LEG READS A REPOSITORY, so every /api/lr test must say
        # WHICH one. Without this the card would log the developer's own
        # checkout — a suite whose answers change with the last thing anyone
        # merged, which is not a test. Patched at the base so a new /api/lr
        # test cannot inherit the hole; the fixture repo is the same one
        # LandReqBase already builds trunk and side branches in.
        self.repo_git = self.git("rev-parse", "--absolute-git-dir")
        info = mock.patch.object(dispatches, "_repo_info",
                                 return_value={"repo": self.repo,
                                               "repo_id": self.repo_git})
        info.start()
        self.addCleanup(info.stop)
        # The web process selects the serving Helm PROJECT from the registry,
        # never from its process cwd. Bind this source checkout to the fixture's
        # canonical repo path so every /api/lr test names both halves of that
        # authority rather than inheriting the developer's live registry.
        package = os.path.dirname(os.path.realpath(web.__file__))
        reg = mock.patch.object(registry, "load", return_value={
            "version": 1,
            "projects": {
                "helm-fixture": {
                    "name": "helm-fixture", "path": self.repo,
                    "cv_scope": {"cwd_prefixes": [package]},
                },
            },
        })
        reg.start()
        self.addCleanup(reg.stop)
        # A COLD READ HERE WAITS FOR ITS BUILD. Every read through the route
        # after `forget()` is a cold start, and the product answers a cold
        # start that outlasts `_LR_COLD_WAIT_S` with `{"warming": True}` --
        # a body with no `loops`, `unavailable` or `stalled_ids`. On a build
        # host loaded by a whole-suite gate the fixture's build passed that
        # wait, and four arms raised KeyError about a board they never read.
        # These arms assert what the board SAYS, so they wait for it; the
        # warming answer itself has its own arms, which call `_cached_swr`
        # with their own `cold_wait` and are untouched by this.
        wait = mock.patch.object(web_land, "_LR_COLD_WAIT_S", 120)
        wait.start()
        self.addCleanup(wait.stop)
        self.forget()

    def fold(self, lane, tip, extra=""):
        """Land `tip` the way this repo's integrator lands: an empty commit on
        trunk whose subject carries the landing grammar. THE FOLD IS THE
        RECORD — no verb writes anything here, which is the whole point of the
        card reading it."""
        subject = "fold: %s at %s%s" % (lane, tip, extra)
        self.git("commit", "-q", "--allow-empty", "-m", subject)
        return self.git("rev-parse", "HEAD")

    def tearDown(self):
        self.forget()          # never leak a tmp-home board into the next test
        web_cache.drain_swr()  # ...nor a rebuild worker into it (task/3304)
        super().tearDown()

    def forget(self):
        """Expire the recompute FLOOR, and nothing else. The floor is deliberate
        in production — landreq.project() spawns git per closed row per repo —
        and would otherwise serve one test's board to the next.

        Deliberately narrow: this is the ONLY cache /api/lr is allowed to have.
        If a second memo is ever added under the floor, these tests are how you
        find out, because a memo below the floor can pin a healthy board over an
        unreadable ledger (see the mtime test below — a browser found that one)."""
        web._qstate.pop("lr", None)
        web._qinflight.pop("lr", None)

    def lr(self):
        """A FRESH read through the real route entry."""
        self.forget()
        body, status = web.QUERY_API["/api/lr"]({})
        self.assertEqual(status, 200)
        return body

    def lr_at(self, base):
        """A fresh read with the projection's clock PINNED to `base`.

        A dwell is `now` minus a backdated stamp, and the two are read at
        DIFFERENT instants: the fixture truncates to a whole second when it
        backdates, the projection reads the clock again when it renders. So an
        exact-equality dwell assertion is really asserting that no second
        boundary crossed in between — true almost always, false on a loaded
        node, and it reddened a whole suite for a lane touching neither file
        (helm task/2232).

        PINNING IS THE HONEST FIX RATHER THAN A TOLERANCE: with both instants
        equal the equality is exact by construction, and a window would have
        widened what the arm accepts in order to survive a clock it could have
        simply held still. Pass the value `age()` returns.

        THE PATCH IS THE CLOCK THE PROJECTION READS, for one call. landreq
        resolves `now` from `time.time()` when its caller supplies none, and
        the route entry supplies none, so this is the only seam a read through
        the real route offers.
        """
        with mock.patch.object(landreq.time, "time", return_value=float(base)):
            return self.lr()

    def one(self, body):
        self.assertEqual(len(body["loops"]), 1, body["loops"])
        return body["loops"][0]

    def edit_event(self, rid, event, drop_ts=False, **fields):
        """Rewrite ONE ledger event of one row in place. The append-only ledger
        is never rewritten by real code; this is how a corrupt or partial row
        gets planted without a mock standing in for the record."""
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        hit = 0
        with open(path, "w", encoding="utf-8") as f:
            for ev in events:
                if ev.get("id") == rid and ev.get("event") == event:
                    ev.update(fields)
                    if drop_ts:
                        ev.pop("ts", None)
                    hit += 1
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")
        self.assertEqual(hit, 1, "no %s event was rewritten" % event)

    def legacy_undeclared(self, row, evidence="legacy review"):
        """Plant an old verdict; current writers must reject this shape."""
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": row["seq"] + 1,
            "id": row["id"], "ts": dispatches.pk.now_ts(),
            "reviewed_tip": row["tip"], "verdict_ref": evidence}))

    def unreadable_receipts(self):
        """A real land-receipt ledger at mode 000 — the mechanism, not a mock,
        and NOT the dispatch ledger."""
        path = landreq.receipts_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"id": self.side, "schema": "helm.land/2"}) + "\n")
        os.chmod(path, 0o000)
        # tearDown removes the whole tmp home before cleanups run, so restoring
        # the mode is best-effort — it exists for a mid-test failure.
        self.addCleanup(lambda: os.path.exists(path) and os.chmod(path, 0o600))


class ApiShapeTest(LrApiBase):
    def test_the_route_is_registered_and_projects_the_open_loops(self):
        row = self.dispatch(lane="lane/example-alpha")
        self.assertIn("/api/lr", web.QUERY_API)
        card = self.one(self.lr())
        self.assertEqual(card["id"], row["id"])
        # #142: the writer strips the lane/ prefix at mint, so the card
        # projects the stored bare spelling.
        self.assertEqual(card["lane"], "example-alpha")
        self.assertEqual(card["state"], "OPEN")
        self.assertEqual(card["review_sha"], self.side[:12])
        self.assertEqual(card["review_sha_full"], self.side)

    def test_the_api_shows_only_the_carrying_chain_frontier(self):  # noqa: VACUOUS_ASSERTION — exact non-empty [child] API output proves the route projected a row while excluding only its carried predecessor; an empty response fails
        parent = self.dispatch(lane="lane/round-one")
        child = self.dispatch(lane="lane/round-two", supersedes=parent["id"])
        body = self.lr()
        self.assertIsNone(body["unavailable"])
        self.assertEqual([row["id"] for row in body["loops"]], [child["id"]])
        # The predecessor stays canonically open; the API suppresses duplicate
        # debt rather than fabricating a close event or terminal state.
        projected = landreq.project()[0][parent["id"]]
        self.assertFalse(projected["terminal"])
        self.assertIsNone(projected["close_reason"])

    def test_every_field_the_card_reads_is_present_on_the_row(self):
        """A rename anywhere upstream silently downgrades a row instead of
        failing, so the names are pinned here rather than trusted."""
        self.dispatch()
        card = self.one(self.lr())
        for field in CARD_FIELDS:
            self.assertIn(field, card, "landreq.card() dropped %r" % field)

    def test_the_gate_fields_read_ABSENT_and_never_default_to_verified(self):
        """The gate lane has not landed, so no row can be verified. The absent
        value must be FALSY in both fields — an absent verification field that
        defaulted truthy would paint an unchecked row as checked."""
        self.dispatch()
        card = self.one(self.lr())
        self.assertEqual(card["gate"], "")
        self.assertIsNone(card["ungated"])

    def test_freshness_is_reported_so_the_card_can_say_how_old_it_is(self):
        self.dispatch()
        body = self.lr()
        self.assertIsInstance(body["read_age_s"], int)
        self.assertIsInstance(body["ledger_age_s"], int)
        self.assertNotIn("ledger_mtime", body)   # ages on the wire, not epochs

    def test_a_stalled_row_is_named_in_stalled_ids(self):
        row = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(row["id"], "post-1")
        self.age(row["id"], 3600)
        body = self.lr()
        self.assertEqual(body["stalled_ids"], [row["id"]])
        self.assertTrue(self.one(body)["stalled"])

    def test_an_undeclared_verdict_is_reported_unmeasurable_never_dropped(self):
        """The honest-refusal half: a REVIEWED row with no polarity is not
        stall-checkable in either direction. Silently absent would read as
        healthy, which is the whole reason unmeasurable() exists."""
        row = self.dispatch()
        self.legacy_undeclared(row, "review-post-9")
        body = self.lr()
        self.assertEqual([u["id"] for u in body["unmeasurable"]], [row["id"]])
        self.assertIn("UNDECLARED", body["unmeasurable"][0]["reason"])
        self.assertEqual(self.one(body)["state"], "REVIEWED")
        self.assertIsNone(self.one(body)["polarity"])

    def test_a_contrary_row_carries_WHICH_physical_fact_contradicts_it(self):
        """`contrary` alone lets a console say "contrary" without saying what
        happened. contrary_state is the LANDED-vs-MERGED_LOCAL distinction the
        CLI already prints, and the card prints it beside the verdict."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
        self.git("merge", "--no-edit", "-q", "side")   # landed despite FIX
        card = self.one(self.lr())
        self.assertTrue(card["contrary"])
        self.assertEqual(card["contrary_state"], "landed")
        self.assertEqual(card["polarity"], "fix")
        # The DISCHARGE stamp rides the same wire (#135 two-surfaces class:
        # `lr list` rendered HONORED while the console counted the row as
        # live debt). This chain holds no on-trunk approve, so the honest
        # value is None — unstamped, which every renderer keeps LOUD.
        self.assertIn("contrary_discharge", card)
        self.assertIsNone(card["contrary_discharge"])
        # ...and so does the CLASSIFICATION, not just its inputs. An unstamped
        # chain is NOT honored, and the wire now says so itself rather than
        # leaving the browser to work it out.
        self.assertIn("honored", card)
        self.assertFalse(card["honored"])

    def test_the_payload_classification_equals_the_server_predicate_for_EVERY_row(self):  # noqa: VACUOUS_ASSERTION — two UNCONDITIONAL controls run before the loop: assertTrue(raw) proves the projection is non-empty, and assertTrue(card(honored_lr)['honored']) proves the field carries TRUE, so the equality below cannot pass on an all-False population.
        """ONE implementation, asserted over the whole projection rather than
        a sample.

        This replaces a PARITY test between two implementations. Parity was
        the right guard while the twin existed — measured 2026-08-05 the two
        still agreed on 276 of 276 live rows, so the twin was never the cause
        of anything — but two implementations that agree today are a drift
        waiting to happen, and only one of them fed the owner's screen."""
        rows = [self.dispatch(lane="lane/parity-%d" % i, kind="review")
                for i in range(3)]
        raw, _current, err = landreq.project_raw()
        # MUST-HIT CONTROL ON THE SAME OBSERVABLE, and it is not ceremony: a
        # fresh fixture is all-unhonored, so the equality loop below would hold
        # trivially between False and False for every row and prove only that
        # both sides can say no. This asserts the field can carry TRUE and that
        # card() routes the predicate rather than a constant.
        honored_lr = dict(next(iter(raw.values())),
                          contrary=True, contrary_discharge="a")
        self.assertTrue(landreq.honored_display(honored_lr))
        self.assertTrue(landreq.card(honored_lr)["honored"],
                        "card() does not carry a TRUE classification")
        self.assertIsNone(err, "the projection failed: %s" % err)
        self.assertTrue(raw, "no rows projected; this test would be vacuous")
        # AGAINST THE RULE, NOT AGAINST THE FUNCTION. card() CALLS
        # honored_display, so comparing the two only pins that the code agrees
        # with itself — it cannot fail for any implementation of the predicate,
        # however wrong. The expectation below is the RULE written out by hand
        # (amendment: honored iff the row is contrary AND carries an a/b/c
        # discharge stamp), so a predicate that drifts from the rule fails here
        # even though the wire and the function would still agree.
        checked = 0
        for lr in raw.values():
            card = landreq.card(lr)
            expected = bool(lr.get("contrary")) and \
                lr.get("contrary_discharge") in ("a", "b", "c")
            self.assertEqual(card["honored"], expected,
                             "row %s: the wire disagrees with the RULE "
                             "(contrary=%r discharge=%r)"
                             % (lr.get("id"), lr.get("contrary"),
                                lr.get("contrary_discharge")))
            checked += 1
        self.assertGreaterEqual(checked, len(rows),
                                "fewer rows checked than were planted")

    def test_delivered_report_is_an_explicit_closed_api_state_without_land_claim(self):
        row = self.dispatch(lane="lane/report-api", kind="build")
        closed, err = landreq.close(
            row["id"], "delivered-report",
            artifact_ref="artifact:report-api.json#abc123",
            report_ref="1234abcd5678",
            evidence="artifact handed to the integrator")
        self.assertIsNone(err)
        self.assertEqual(closed["state"], "DELIVERED_REPORT")
        body = self.lr()
        self.assertEqual(body["loops"], [])
        card = body["closed_recent"][0]
        self.assertEqual(card["id"], row["id"])
        self.assertEqual(card["state"], "DELIVERED_REPORT")
        self.assertEqual(card["land_state"], "NOT_CLAIMED")
        self.assertFalse(card["landed"])
        self.assertEqual(card["artifact_ref"], "artifact:report-api.json#abc123")
        self.assertEqual(card["report_ref"], "1234abcd5678")
        self.assertEqual(card["owed_by"], "nobody")

    def test_a_landed_row_leaves_the_loops_and_enters_the_closed_window(self):
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        body = self.lr()
        self.assertEqual(body["loops"], [])            # terminal, not in flight
        self.assertEqual([c["id"] for c in body["closed_recent"]], [row["id"]])
        card = body["closed_recent"][0]
        self.assertEqual(card["state"], "LANDED")
        for field in ("artifact_ref", "report_ref",
                      "delivered_report_correction", "cancel_reason"):
            self.assertNotIn(field, card)

    def test_a_row_closed_days_ago_is_outside_the_24h_window(self):
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        self.age(row["id"], 3 * 86400)
        body = self.lr()
        self.assertEqual(body["closed_recent"], [])
        self.assertEqual(body["loops"], [])

    def test_an_unreadable_stamp_is_not_counted_as_closed_just_now(self):
        """dispatches._age_s answers 0 — "just now" — for a stamp it cannot
        parse, so a row with a corrupt ts would otherwise be the freshest thing
        in the closed-today list. UNKNOWN is not recent."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for ev in events:
                if ev.get("id") == row["id"]:
                    ev["ts"] = "not-a-stamp"
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")
        # the row is still THERE and still terminal — it is the WINDOW that
        # refuses it, not the projection losing the row
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        self.assertTrue(lrs[row["id"]]["terminal"])
        self.assertEqual(self.lr()["closed_recent"], [])


class FiledPopulationTest(LrApiBase):
    """The owner's 2026-08-04 ask, verbatim: "why don't the pipeline readings
    also include the number of filed rows, or do they somewhere?" They did
    not, anywhere: the board knew a 24h closed window (`closed_total`) and an
    in-flight list, and the all-time population behind them was computed by
    nothing. `filed` answers it from the SAME project_raw snapshot — no second
    ledger walk under the recompute floor — and these pin the wire keys, the
    partition property, and the loud-unavailability invariant (`filed` is a
    dict exactly when the board rendered; None on every unavailable body,
    never a confident zero)."""

    def off_trunk(self, name):
        """A tip on its own branch, NOT on trunk — so merging `side` for the
        landed arm cannot silently land this row too."""
        self.git("checkout", "-q", "-b", name, self.a)
        tip = self.commit(name, path=name)
        self.git("checkout", "-q", self.main)
        return tip

    def test_the_filed_split_partitions_the_whole_ledger(self):  # noqa: VACUOUS_ASSERTION — the exact six-bucket non-empty dict equality is the unconditional positive control; an empty board, a failed read, or a leaked arm all fail it
        # one row per bucket, each on its own tip so no arm leaks into another
        self.dispatch(lane="lane/filed-open", ref=self.off_trunk("open-tip"))
        held = self.dispatch(lane="lane/filed-held",
                             ref=self.off_trunk("held-tip"))
        self.legacy_undeclared(held, "review-post-7")     # REVIEWED — held
        landed = self.dispatch(lane="lane/filed-landed", ref=self.side)
        self.mark_verdict(landed["id"], self.side, "ok",
                                polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")      # LANDED — terminal
        report = self.dispatch(lane="lane/filed-report", kind="build",
                               ref=self.off_trunk("report-tip"))
        _closed, err = landreq.close(
            report["id"], "delivered-report",
            artifact_ref="artifact:filed.json#abc123",
            report_ref="1234abcd5678", evidence="artifact handed off")
        self.assertIsNone(err)                # closed, and NOT landed
        moot = self.dispatch(lane="lane/filed-moot",
                             ref=self.off_trunk("moot-tip"))
        _row, err = dispatches.mark_cancel(moot["id"], "moot")
        self.assertIsNone(err)                # cancelled transit — non-loop
        body = self.lr()
        self.assertIsNone(body["unavailable"])
        # `in_flight` is a wire-only REVERSE-SKEW alias of `open` (web_land)
        # and is deliberately NOT part of the partition — filed_split's own
        # buckets still sum to total, which is the property under test.
        # `underived` IS ZERO HERE AND THAT IS THE CONTROL FOR THE BUCKET,
        # not an omission: every fixture row above is built in this repo with
        # a real tip, so each one's landedness is genuinely OBSERVABLE and
        # belongs in open/held/landed/closed. A bucket that counted anything
        # on a fully readable board would be counting the wrong predicate.
        # `open_frontier` / `off_frontier` are a CROSS-CUT of `open`, not two
        # more members of the partition (task/2381). They are asserted here by
        # exact value so a reader sees which side the fixture's open row falls
        # on, and the partition invariant below still sums the SIX buckets.
        #
        # THE OPEN ROW IS ON THE FRONTIER AND UNCLASSIFIED, and both halves
        # are the property: it is a dispatched row with NO verdict, so it
        # carries no reviewed tip and there is nothing to place against trunk
        # — the `tip` rung refuses it. A row helm cannot place is work OWED,
        # never residue, because the verb the residue term names will not
        # touch it; it is counted in the frontier term and disclosed by name
        # inside it.
        # `closable` / `not_closable` are a cross-cut of `off_frontier` for
        # the same reason and on the same law: what the classification PLACED
        # is not what the close ladder will take, and one number for both is
        # what told the owner 316 rows were clearable while the authorizing
        # witness refused 182. They partition the residue, which is empty here.
        self.assertEqual(body["filed"],
                         {"total": 5, "open": 1, "held": 1, "landed": 1,
                          "closed": 1, "non_loop": 1, "underived": 0,
                          "open_frontier": 1, "off_frontier": 0,
                          "unclassified": 1, "in_flight": 1,
                          "closable": 0, "not_closable": 0})
        f = body["filed"]
        self.assertEqual(f["off_frontier"], f["closable"] + f["not_closable"],
                         "the door terms no longer partition the residue")
        self.assertEqual(f["total"], f["open"] + f["held"] + f["underived"]
                         + f["landed"] + f["closed"] + f["non_loop"],
                         "the split no longer partitions the ledger")
        self.assertEqual(f["open"], f["open_frontier"] + f["off_frontier"],
                         "the frontier terms no longer partition `open`")
        self.assertEqual(f["in_flight"], f["open"],
                         "the alias must carry the SAME number, never a "
                         "second predicate")
        self.assertNotIn("in_flight", landreq.filed_split(
            *landreq.project_raw()[:2]),
            "the alias belongs on the wire, not in the projection whose "
            "buckets are required to partition")

    def test_an_unreadable_ledger_answers_filed_UNKNOWN_not_zero(self):  # noqa: VACUOUS_ASSERTION — the positive control is the verbatim failure reason on the same body; filed=None is the intentional absence under test
        self.dispatch()                       # the population is NOT zero
        with mock.patch.object(eventledger, "checked_events",
                               return_value=([], "PermissionError: denied")), \
                _dispatch_bytes_denied():
            body = self.lr()
        self.assertIsInstance(body["unavailable"], str)
        self.assertIn("denied", body["unavailable"])   # landreq's own words
        self.assertIsNone(body["filed"],
                          "a failed read rendered a confident filed count")

    def test_a_handler_failure_body_carries_filed_UNKNOWN_too(self):  # noqa: VACUOUS_ASSERTION — the positive control is the named exception on the same body; filed=None is the intentional absence under test
        with mock.patch.object(landreq, "project_raw",
                               mock.Mock(side_effect=RuntimeError("boom"))):
            body = self.lr()
        self.assertIsInstance(body["unavailable"], str)
        self.assertIn("RuntimeError", body["unavailable"])
        self.assertIsNone(body["filed"])

    def test_an_untrustworthy_chain_keeps_filed_UNKNOWN(self):
        """The board refused past project_raw; a filed dict beside an
        unavailable strip would be two verdicts about one read."""
        lrs = {"a": {"id": "a", "terminal": False},
               "b": {"id": "b", "terminal": False}}
        raw = {"a": {"id": "a", "v": 3, "supersedes": "b",
                     "chain_root": "ROOT"},
               "b": {"id": "b", "v": 3, "supersedes": "a",
                     "chain_root": "ROOT"}}
        with mock.patch.object(landreq, "project_raw",
                               return_value=(lrs, raw, None)):
            body = self.lr()
        self.assertIn("cycle", body["unavailable"])
        self.assertIsNone(body["filed"])


class LiveObligationsWireTest(LrApiBase):
    """ONE CENSUS, THREE CONSUMERS: the header's filed split, the scheduler
    model and the kanban cards all read the SAME off-frontier walk of one
    build, through the real route, over a real repository — so the number the
    strip prints, the line the waits draw and each card's own mark cannot
    disagree, and the walk the board already paid for is not paid twice."""

    def left_over(self):
        """LEFT OVER: a FIX-verdicted row whose lane is gone — no ref, no
        room, no lease — and whose reviewed tip is on trunk by ancestry."""
        tip = self.commit("left-over-on-trunk", path="left")
        row = self.dispatch(ref=tip, lane="left-over-lane")
        dispatches._mark_delivered(row["id"], "post-left-over")
        _out, err = self.mark_verdict(row["id"], tip, "reviewed",
                                      polarity="fix")
        self.assertIsNone(err, err)
        return row

    def test_an_off_frontier_row_collapses_and_a_live_one_stays_listed(self):  # noqa: VACUOUS_ASSERTION — the live row is asserted LISTED by exact equality and the left-over row is asserted on the collapsed line by exact count and reason, on the same body
        gone = self.left_over()
        # LIVE: its lane branch still exists and holds work off trunk
        self.git("branch", "lane/still-building", self.side)
        live = self.dispatch(ref=self.side, lane="still-building")
        calls = []
        real = landreq.frontier_verdicts

        def counted(*a, **kw):
            calls.append(len(a[0]))
            return real(*a, **kw)

        with mock.patch.object(landreq, "frontier_verdicts", counted):
            body = self.lr()
        self.assertIsNone(body["unavailable"])
        self.assertEqual(len(calls), 1, "the census walked twice in one "
                         "build: %r" % calls)
        cards = {c["id"]: c for c in body["loops"]}
        self.assertEqual(cards[gone["id"]]["frontier"], "landed-by-ancestry")
        self.assertEqual(cards[live["id"]]["frontier"], "on-frontier")
        model = body["scheduler"]
        listed = [r["id"] for g in model["groups"] for r in g["rows"]]
        self.assertEqual(listed, [live["id"]])
        self.assertEqual([(c["class"], c["count"]) for c in model["collapsed"]],
                         [("off_frontier", 1)])
        self.assertEqual(model["collapsed"][0]["by_reason"],
                         {"landed-by-ancestry": 1})
        # THE HEADER COUNTED THE SAME ROW FROM THE SAME WALK
        self.assertEqual(body["filed"]["off_frontier"], 1)
        self.assertEqual(model["listed_count"] + model["collapsed_count"],
                         model["row_count"])

    def test_a_body_whose_census_was_not_taken_claims_no_collapse(self):
        """No frontier on the cards is no frontier claim: every live row is
        listed, exactly as before the split."""
        self.left_over()
        with mock.patch.object(landreq, "frontier_verdicts",
                               return_value={}):
            body = self.lr()
        self.assertIsNone(body["unavailable"])
        self.assertEqual(body["scheduler"]["collapsed"], [])
        self.assertEqual(body["scheduler"]["listed_count"], 1)
        self.assertIsNone(body["loops"][0]["frontier"])


class UnavailableIsLoudTest(LrApiBase):
    """The property this whole surface exists for."""

    def test_untrustworthy_chain_topology_answers_UNKNOWN_not_a_partial_board(self):
        lrs = {"a": {"id": "a", "terminal": False},
               "b": {"id": "b", "terminal": False}}
        raw = {"a": {"id": "a", "v": 3, "supersedes": "b",
                     "chain_root": "ROOT"},
               "b": {"id": "b", "v": 3, "supersedes": "a",
                     "chain_root": "ROOT"}}
        with mock.patch.object(landreq, "project_raw",
                               return_value=(lrs, raw, None)):
            body = self.lr()
        self.assertIn("cycle", body["unavailable"])
        self.assertEqual(body["loops"], [])
        self.assertEqual(body["stalled_ids"], [])
        self.assertEqual(body["unmeasurable"], [])

    def test_an_unreadable_dispatch_ledger_answers_UNKNOWN_not_an_empty_board(self):
        self.dispatch()                       # a real row exists on the ledger
        with mock.patch.object(eventledger, "checked_events",
                               return_value=([], "PermissionError: denied")), \
                _dispatch_bytes_denied():
            body = self.lr()
        self.assertIsInstance(body["unavailable"], str,
                              "a read that FAILED reported itself as fine")
        self.assertIn("denied", body["unavailable"])   # landreq's own words
        self.assertEqual(body["loops"], [])
        self.assertEqual(body["stalled_ids"], [])
        self.assertEqual(body["unmeasurable"], [])
        self.assertEqual(body["closed_recent"], [])

    def test_a_handler_failure_names_itself_unavailable_at_200(self):
        """The page must not die — but it must not report a healthy empty
        pipeline it never read, either. 200 + a NAMED reason, never
        `{"unavailable": true}`: a strip that cannot say why is a dead end."""
        with mock.patch.object(landreq, "project_raw",
                               mock.Mock(side_effect=RuntimeError("boom"))):
            body = self.lr()
        self.assertIsInstance(body["unavailable"], str)
        self.assertIn("RuntimeError", body["unavailable"])
        self.assertIn("UNKNOWN", body["unavailable"])
        self.assertEqual(body["loops"], [])

    def test_a_failed_board_read_is_RECORDED_where_an_agent_can_find_it(self):
        """The half no renderer can cover. The card says UNKNOWN to whoever is
        looking at it; this endpoint is also the only thing that knows the read
        failed at all, so it records the outcome and the identity of the
        process that took it. Without this leg a dead board is visible to the
        owner's eye and to nothing else on the fleet — no log, no counter, no
        doctor check."""
        with tempfile.TemporaryDirectory() as home_dir, \
                mock.patch.dict(os.environ, {"HELM_HOME": home_dir}):
            boardread._LAST.update(ts=0.0, outcome=None)
            with mock.patch.object(landreq, "project_raw",
                                   mock.Mock(side_effect=RuntimeError("boom"))):
                body = self.lr()
            self.assertIn("RuntimeError", body["unavailable"])   # control
            st = boardread.state()
            self.assertTrue(st["recorded"])
            self.assertEqual(st["outcome"], "failed")
            self.assertEqual(st["observer"], "live")
            self.assertIn("RuntimeError", st["reason"])
            self.assertEqual(st["failures"], 1)
            # …and the SAME record is what turns the owner's blank card into a
            # finding an agent running `helm doctor` reads.
            rows = doctor.check_board_reads()
            self.assertEqual([lvl for lvl, _ in rows], [doctor.FAIL])

    def test_a_healthy_board_read_records_that_a_server_is_reading(self):  # noqa: VACUOUS_ASSERTION — the control on the same observable is the failing read at the end of this method, which the walker cannot see through because it lands on a second binding (`failed`) rather than back onto `body`
        """A failures-only record cannot tell a healthy server from no server,
        so the healthy read is recorded too — and it must NOT read as a fault."""
        with tempfile.TemporaryDirectory() as home_dir, \
                mock.patch.dict(os.environ, {"HELM_HOME": home_dir}):
            boardread._LAST.update(ts=0.0, outcome=None)
            body = self.lr()
            self.assertIsNone(body["unavailable"])
            self.assertEqual(boardread.state()["outcome"], "ok")
            rows = doctor.check_board_reads()
            self.assertEqual([lvl for lvl, _ in rows], [doctor.OK])
            # THE POSITIVE CONTROL ON THE SAME OBSERVABLE, and it is also the
            # property: the very next read that fails moves the record off
            # `ok`, so the arm above pins a reading rather than a file nothing
            # ever writes.
            with mock.patch.object(landreq, "project_raw",
                                   mock.Mock(side_effect=RuntimeError("boom"))):
                failed = self.lr()
            self.assertIn("RuntimeError", failed["unavailable"])
            self.assertEqual(boardread.state()["outcome"], "failed")

    def test_a_ledger_that_goes_unreadable_WITHOUT_moving_surfaces_anyway(self):
        """Found in a browser, not in a test. The first version of this endpoint
        memoised the projection on the ledgers' mtime+size, so it would only
        re-walk when something had "changed". `chmod 000` changes NEITHER —
        and the card went on painting the last healthy board, every row green,
        over a record it could no longer read. The floor may bound the cost of
        the read; nothing may bound the truth of it."""
        self.dispatch()
        before = self.lr()
        self.assertEqual(len(before["loops"]), 1)
        path = dispatches.ledger_path()
        stat_before = os.stat(path)
        with mock.patch.object(eventledger, "checked_events",
                               return_value=([], "PermissionError: denied")), \
                _dispatch_bytes_denied():
            after = self.lr()          # ONLY the floor was expired
        stat_after = os.stat(path)
        self.assertEqual(stat_before.st_mtime_ns, stat_after.st_mtime_ns)
        self.assertEqual(stat_before.st_size, stat_after.st_size)
        self.assertIn("denied", after["unavailable"])
        self.assertEqual(after["loops"], [])

    @unittest.skipIf(os.geteuid() == 0, "root reads through any mode bits")
    def test_the_real_permission_path_is_loud_too(self):
        """The mock above pins the contract; this pins the MECHANISM — a real
        chmod, through the real eventledger read, with nothing patched."""
        self.dispatch()
        self.assertEqual(len(self.lr()["loops"]), 1)
        path = dispatches.ledger_path()
        os.chmod(path, 0o000)
        try:
            body = self.lr()
        finally:
            os.chmod(path, 0o600)
        self.assertIsInstance(body["unavailable"], str)
        self.assertIn("PermissionError", body["unavailable"])
        self.assertEqual(body["loops"], [])
        self.assertEqual(len(self.lr()["loops"]), 1)   # and it recovers


class APartialReadIsStillAFailedReadTest(LrApiBase):
    """Finding 1. `project()` reads the dispatch ledger TWICE — once
    for the canonical snapshot and once for the grouped events that carry every
    TRANSITION TIMESTAMP. Only the second one failing was silent: `events()`
    drops checked_events' reason and answers `[]`, which is byte-identical to an
    empty ledger, so the projection kept every row and lost every clock.

    THE SECOND READ NO LONGER EXISTS. It was removed for a LATER defect of a
    different shape — two reads that both SUCCEED can straddle an append, see
    AnAppendCannotLandBetweenTwoReadsTest below — so `project()` derives the
    snapshot and the grouped events from ONE `checked_events` and there is no
    half of it left to fail on its own. The property this class was written for
    is what still has to hold, and it is asserted here against the read that
    remains: a dispatch ledger that cannot be read renders UNKNOWN with no
    rows, never a calm board dated from nothing."""

    def failing_dispatch_read(self):
        """The real eventledger with the DISPATCH ledger unreadable — and only
        it. The receipt index still reads, so the failure under test is the
        only thing that changed (planting it by call ORDER instead would now
        break the receipt read, which is fail-open and would leave the board
        rendering happily)."""
        real = eventledger.checked_events

        def flaky(path, strict=False):
            if path == dispatches.ledger_path():
                return [], "PermissionError: denied"
            return real(path, strict=strict)
        stack = contextlib.ExitStack()
        stack.enter_context(
            mock.patch.object(eventledger, "checked_events", flaky))
        stack.enter_context(_dispatch_bytes_denied())
        return stack

    def test_a_dispatch_ledger_that_cannot_be_read_is_reported_UNKNOWN(self):
        row = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(row["id"], "post-1")
        base = self.age(row["id"], 3600)
        healthy = self.lr_at(base)
        self.assertTrue(self.one(healthy)["stalled"])   # the truth, read whole
        self.assertEqual(self.one(healthy)["dwell_s"], 3600)

        with self.failing_dispatch_read():
            body = self.lr()
        self.assertIsInstance(body["unavailable"], str,
                              "a failed read reported itself as a whole one")
        self.assertIn("denied", body["unavailable"])
        self.assertEqual(body["loops"], [])

    def test_the_old_shape_was_an_hour_old_row_rendering_as_brand_new(self):
        """Named for the exact lie: the row survives, its clock does not, and
        `_age_s` answers 0 for a stamp that is not there. Pinned as a NEGATIVE
        so a regression cannot pass by rendering the row calmly."""
        row = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(row["id"], "post-1")
        self.age(row["id"], 3600)
        with self.failing_dispatch_read():
            body = self.lr()
        self.assertEqual(body["stalled_ids"], [])      # no rows at all…
        self.assertEqual(body["loops"], [])            # …because none were read
        self.assertIsNotNone(body["unavailable"])      # …and it SAYS so

    def test_the_pair_read_returns_its_own_reason_rather_than_empty_dicts(self):
        """The mechanism, at the seam. Two empty dicts and no reason is the
        shape that reads as "the ledger holds nothing"."""
        with mock.patch.object(eventledger, "checked_events",
                               return_value=([], "OSError: EIO")), \
                _dispatch_bytes_denied("OSError: EIO"):
            current, by_id, taken, _order, unavailable = dispatches.snapshot_and_events()
        self.assertEqual((current, by_id, taken), ({}, {}, {}))
        self.assertEqual(unavailable, "OSError: EIO")

    def test_a_readable_ledger_still_folds_AND_groups_every_event(self):
        """The other half of the seam: the pair read must not turn a WORKING
        read into a refusal, and must answer BOTH projections."""
        row = self.dispatch()
        current, by_id, taken, _order, unavailable = dispatches.snapshot_and_events()
        self.assertIsNone(unavailable)
        self.assertTrue(by_id[row["id"]])
        self.assertEqual(current[row["id"]]["status"], "open")
        # …and the third view: the events the fold TOOK, which for a healthy
        # row is every event it holds
        self.assertEqual(taken[row["id"]], by_id[row["id"]])


class AnIdTheFoldCannotOPENIsAHoleInTheBoardTest(LrApiBase):
    """The fourth round, one layer before the projection.

    `strict=True` validates the PHYSICAL shape of a ledger line — complete
    JSON, an object, a non-empty id — and says nothing about whether those
    events amount to an obligation. Their repro is one well-formed row: a
    `delivered` event with no dispatch before it, as the ENTIRE ledger.
    `_new_state` refuses the genesis, `_fold` skips the id, and the board goes
    out with `loops: []`, `closed_total: 0` and `unavailable: null` — which the
    real renderer prints as "the dispatch ledger READ cleanly" with the nav
    badge at 0. A whole record helm wholly failed to project rendered as a
    clean empty board.

    AND THE PER-ROW PATH CANNOT SAY IT. `ledger_refused` hangs off a canonical
    row; at genesis there is none, so the display I built for refusals is
    structurally blind here. The claim is about the WHOLE record, so it is
    answered where that claim is made — the same way as the projection failure
    beside it, rather than two ways for one shape."""

    GENESIS = {"id": "a" * 32, "v": 3, "seq": 1, "event": "delivered",
               "ts": "2026-07-30T00:00:00Z", "delivery_ref": "post-1"}

    def orphan(self):
        os.makedirs(os.path.dirname(dispatches.ledger_path()), exist_ok=True)
        self.assertTrue(eventledger.append(dispatches.ledger_path(),
                                           dict(self.GENESIS)))

    def test_a_delivered_genesis_alone_is_UNKNOWN_not_an_empty_board(self):
        self.orphan()
        body = self.lr()
        self.assertIsInstance(body["unavailable"], str,
                              "a record helm could not project said it read "
                              "cleanly")
        self.assertIn("no openable dispatch", body["unavailable"])
        self.assertIn("a" * 12, body["unavailable"])       # WHICH id to repair
        self.assertEqual(body["loops"], [])
        self.assertEqual(body["closed_total"], 0)

    def test_it_takes_the_board_even_beside_HEALTHY_rows(self):
        """The half that makes it a whole-record claim rather than a filter: a
        board that renders the rows it could open and drops the one it could
        not is describing part of the record as the whole."""
        row = self.dispatch(ref=self.side)
        healthy = self.lr()
        self.assertEqual([c["id"] for c in healthy["loops"]], [row["id"]])

        self.orphan()
        body = self.lr()
        self.assertIsNotNone(body["unavailable"])
        self.assertEqual(body["loops"], [])
        self.assertNotIn(row["id"], json.dumps(body["loops"]))

    def test_the_reason_reaches_the_TERMINAL_too(self):
        self.orphan()
        rc, _out, err = run(["list"])
        self.assertEqual(rc, 1)
        self.assertIn("no openable dispatch", err)
        loops, unavailable = landreq.loops()
        self.assertIsNone(loops)
        self.assertIn("no openable dispatch", unavailable)

    def test_the_VERBS_keep_working_over_the_same_ledger(self):
        """The projection refuses; the mutation verbs' lenient snapshot is a
        different contract and must not be collateral. A row nobody can open is
        not a reason to stop cancelling the rows you can."""
        row = self.dispatch(ref=self.side)
        self.orphan()
        current, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        self.assertIn(row["id"], current)
        self.assertNotIn(self.GENESIS["id"], current)

    def test_a_HEALTHY_ledger_has_no_unopenable_ids(self):
        """The negative that keeps the refusal from being a permanent outage —
        and it is measured on the shape the fixture actually writes, so a
        future genesis change that stops folding is caught here rather than in
        the owner's browser."""
        row = self.dispatch(ref=self.side)
        dispatches._mark_delivered(row["id"], "post-1")
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        current, held, _taken, _order, unavailable = dispatches.snapshot_and_events()
        self.assertIsNone(unavailable)
        self.assertEqual(sorted(held), sorted(current))
        self.assertIsNone(self.lr()["unavailable"])


class AnAppendCannotLandBetweenTwoReadsTest(LrApiBase):
    """The fourth audit, and the ONE defect on this card that is not the
    missing-value shape.

    Nothing here is absent, unreadable or malformed, and both reads SUCCEED.
    `project()` read the dispatch ledger twice — `snapshot()` for the canonical
    state, then `events_by_id()` for the transition stamps — and a `delivered`
    event appended BETWEEN them was in the second read and not the first. The
    board then rendered the headline OPEN, owed by the integrator, above its
    own timeline showing AWAITING_REVIEW, with `notified: true` beside it, and
    `unavailable: None` over all of it. A card contradicting itself is worse
    than one that says UNKNOWN, because nothing on it says which half to
    believe.

    No value check can catch this: the fix is that the two derivations share
    ONE append boundary, so a row is either wholly in this projection or wholly
    in the next."""

    def straddle(self, row):
        """The real ledger, with ONE append landing immediately after the read
        that project() makes. Under the old two-read shape this is exactly the
        field race — an append between a chmod-free pair of successful reads."""
        # THE DISPATCH LEDGER IS READ AS BYTES NOW (task/2770), so the race
        # lands after THAT read — the one the fold, the grouping and the
        # checkpoint's prefix proof are all cut from.
        real = eventledger.read_bytes
        fired = []

        def racing(path):
            out = real(path)
            if path == dispatches.ledger_path() and not fired:
                fired.append(True)       # never re-enter from the append itself
                dispatches._mark_delivered(row["id"], "post-1")
            return out
        return mock.patch.object(eventledger, "read_bytes", racing), fired

    def test_a_delivery_appended_mid_read_cannot_split_a_row_in_half(self):
        row = self.dispatch()
        patch, fired = self.straddle(row)
        with patch:
            lrs, unavailable = landreq.project()
        self.assertTrue(fired, "the fixture never appended anything")
        self.assertIsNone(unavailable)
        lr = lrs[row["id"]]
        states = [step["state"] for step in lr["timeline"]]
        # ONE boundary: the delivery is in every derived fact or in none of
        # them. Asserted as the AGREEMENT and then as the three values, so a
        # projection that agrees on the WRONG side is still caught below.
        self.assertEqual(lr["state"] == "AWAITING_REVIEW", lr["notified"])
        self.assertEqual(lr["notified"], "AWAITING_REVIEW" in states)
        self.assertEqual(lr["state"], "OPEN")
        self.assertFalse(lr["notified"])
        self.assertNotIn("AWAITING_REVIEW", states)

    def test_the_append_is_not_lost_it_is_in_the_NEXT_projection(self):
        """The control that keeps the fix from being "ignore late writes": a
        board as of one instant is not a board that stopped reading."""
        row = self.dispatch()
        patch, _fired = self.straddle(row)
        with patch:
            landreq.project()
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        self.assertEqual(lrs[row["id"]]["state"], "AWAITING_REVIEW")
        self.assertTrue(lrs[row["id"]]["notified"])

    def test_the_projection_reads_the_dispatch_ledger_exactly_ONCE(self):
        """The mechanism, because the mechanism IS the property here: two reads
        cannot share a boundary they do not have. A future implementation that
        genuinely proves both reads saw the same append boundary would have to
        replace this test with the assertion of THAT proof — it may not simply
        read twice again."""
        self.dispatch()
        real, real_bytes = eventledger.checked_events, eventledger.read_bytes
        seen = []

        def counting(path, strict=False):
            seen.append(path)
            return real(path, strict=strict)

        # BOTH DOORS ARE COUNTED: the dispatch fold reads its ledger as bytes
        # (task/2770), and a second read through either door is the straddle.
        def counting_bytes(path):
            seen.append(path)
            return real_bytes(path)
        with mock.patch.object(eventledger, "checked_events", counting), \
                mock.patch.object(eventledger, "read_bytes", counting_bytes):
            landreq.project()
        self.assertEqual(seen.count(dispatches.ledger_path()), 1, seen)


class OneReadIsNotYetOneRuleTest(LrApiBase):
    """The cross-family review of the shared-read fix, and the useful
    kind of finding: the straddle was closed and the SAME contradiction class
    survived one layer in.

    1. THE READ THE FIX CONSOLIDATED ON WAS LENIENT. `checked_events` skips a
       complete corrupt JSONL row by default — the historical projection
       contract, so one bad row cannot blind every obligation a mutation verb
       needs — and on this surface that made a corrupted ledger answer
       `unavailable: null` with whatever rows survived, which the card renders
       as "the dispatch ledger READ cleanly", nav badge 0.

    2. ONE READ, TWO ACCEPTANCE RULES. The fold refuses an out-of-sequence v3
       verdict; the raw slice keeps it. `seq=99` produced canonical OPEN with a
       timeline showing REVIEWED and `unavailable: null` over both — the exact
       shape of the straddle, arrived at without any race at all."""

    def corrupt_line(self):
        with open(dispatches.ledger_path(), "a", encoding="utf-8") as f:
            f.write("{not json at all}\n")

    def test_a_corrupt_ledger_row_makes_the_whole_board_UNKNOWN(self):
        self.dispatch(ref=self.side)
        healthy = self.lr()                      # the control: it DID read
        self.assertEqual(len(healthy["loops"]), 1)
        self.assertIsNone(healthy["unavailable"])

        self.corrupt_line()
        body = self.lr()
        self.assertIsInstance(body["unavailable"], str,
                              "a corrupt ledger reported itself as read cleanly")
        self.assertIn("corrupt ledger line", body["unavailable"])
        self.assertEqual(body["loops"], [])      # and NOT a partial board
        self.assertEqual(body["closed_total"], 0)

    def test_the_verbs_keep_their_lenient_read_on_purpose(self):
        """The other half, and why this is not one rule for the whole file:
        refusing to cancel a dispatch because some unrelated row is malformed
        helps nobody. The board claims to describe the WHOLE record; a verb
        acts on ONE row."""
        row = self.dispatch(ref=self.side)
        self.corrupt_line()
        current, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        self.assertIn(row["id"], current)

    def test_a_verdict_the_fold_REFUSED_is_not_on_the_timeline(self):
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.assertEqual(self.one(self.lr())["state"], "READY")     # control
        self.edit_event(row["id"], "verdict", seq=99)   # out of sequence
        lrs, unavailable = landreq.project()
        lr = lrs[row["id"]]
        self.assertIsNone(unavailable)
        # the canonical state is the ONLY acceptance rule, and the timeline
        # answers to it
        self.assertEqual(lr["state"], "OPEN")
        self.assertEqual([s["state"] for s in lr["timeline"]], ["OPEN"])

    def test_and_the_refusal_is_SAID_rather_than_silently_dropped(self):
        """Dropping it quietly is the same defect in the other direction: the
        ledger holds a verdict event, and a row that shows no sign of it leaves
        the reader nothing to repair."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.edit_event(row["id"], "verdict", seq=99)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["ledger_refused"], ["verdict"])
        self.assertIn("REFUSED", landreq._line(lr))
        self.assertIn("absent from the state and the timeline",
                      landreq._render_show(lr))
        self.assertEqual(landreq.card(lr)["ledger_refused"], ["verdict"])

    def refused_then_accepted_verdict(self, refused_ts="2001-01-01T00:00:00Z"):
        """TWO verdict events for one row: an out-of-sequence one the fold
        refuses, stamped long ago, then the real one it accepts. The canonical
        row is legitimately CLOSED either way — which is exactly why the state
        cannot say which event closed it."""
        row = self.dispatch(ref=self.side)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "id": row["id"], "v": 3, "seq": 99, "event": "verdict",
            "ts": refused_ts, "reviewed_tip": self.side,
            "verdict_ref": "the refused one", "polarity": "approve"}))
        self.mark_verdict(row["id"], self.side, "the accepted one",
                                polarity="approve")
        return row

    def test_a_REFUSED_event_cannot_supply_the_ACCEPTED_one_s_stamp(self):
        """The follow-up, and the step the first answer was short by.
        Deferring to the canonical state reconciles WHETHER a transition
        happened; it is blind to WHICH event caused it. With one refused and
        one accepted verdict the row is closed either way, so the state raised
        no objection and the reader took the first verdict it saw — the refused
        one. The card printed a verdict from 2001 with a 25-year dwell, an
        `entered_ts` of 2001, `ledger_refused: []` and `unavailable: null`."""
        row = self.refused_then_accepted_verdict()
        lr = landreq.get(row["id"])[0]
        self.assertEqual(row["status"] if False else lr["state"], lr["state"])
        # the ACCEPTED verdict is the one that dates the row. Scan for the
        # refused stamp's DATE, never the bare year: the projection embeds
        # per-run hex (the side sha, the row id), and a bare 4-digit year
        # collides with 4 hex digits about once in 250 runs — measured live
        # as a whole-suite gate flake (receipt 514791bb19722e35, 2026-08-04,
        # "2099" inside a freshly minted sha)
        self.assertNotIn("2001-01-01", json.dumps(lr))
        self.assertRegex(lr["entered_ts"],
                         r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
        self.assertTrue(lr["dwell_known"])
        self.assertLess(lr["dwell_s"], 3600)
        step = [s for s in lr["timeline"] if s["state"] == lr["state"]][0]
        self.assertEqual(step["ts"], lr["entered_ts"])

    def test_and_the_refused_TWIN_is_still_named(self):
        """Bound separately from the stamp: a fix that took the right stamp and
        said nothing would leave the owner with an unrepaired ledger row and no
        sign of it — the quiet half of this same defect."""
        row = self.refused_then_accepted_verdict()
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["ledger_refused"], ["verdict"])
        self.assertIn("REFUSED", landreq._line(lr))

    def test_the_stamp_is_the_ACCEPTED_one_even_when_it_is_the_OLDER(self):
        """The control that proves it is identity and not 'prefer the newest':
        put the refused verdict in the FUTURE and the accepted one still wins."""
        row = self.refused_then_accepted_verdict(refused_ts="2099-01-01T00:00:00Z")
        lr = landreq.get(row["id"])[0]
        # the refused stamp's DATE, never the bare year — the hex-collision
        # flake class named on the 2001 twin above; this arm is the one the
        # gate actually caught
        self.assertNotIn("2099-01-01", json.dumps(lr))
        self.assertTrue(lr["dwell_known"])
        self.assertEqual(lr["ledger_refused"], ["verdict"])

    def test_token_exempt_first_verdict_rejoins_its_existing_attest_read_only(self):  # noqa: VACUOUS_ASSERTION — fixture proves raw-over/logical-under evidence and non-empty sidecar, then pins canonical evidence, attest state, refused twin, and zero emit
        """The exact five-row historical repair, without retroactive signing.

        Old writer accepted a 262-ish token-bearing verdict, old reducer dropped
        it, and a shorter same-sequence remint became canonical. On the reconciled
        reducer the FIRST event is accepted again, so its existing attest matches;
        the remint remains durable as a refused historical verdict. Display reads
        neither append nor emit."""
        row = self.dispatch(ref=self.side)
        evidence = "gate:0123456789abcdef " + "x" * 241
        self.assertGreater(len(evidence), 256)
        self.assertLessEqual(len(dispatches._GATE_TOKEN_RE.sub("", evidence)), 256)
        seq = row["seq"] + 1
        first = {"v": 3, "event": "verdict", "seq": seq, "id": row["id"],
                 "ts": "2026-08-03T15:23:13Z", "reviewed_tip": self.side,
                 "verdict_ref": evidence, "polarity": "approve", "gate": "",
                 "gate_caps": []}
        second = dict(first, ts="2026-08-03T15:46:10Z",
                      verdict_ref="shorter remint")
        self.assertTrue(eventledger.append(dispatches.ledger_path(), first))
        self.assertTrue(eventledger.append(dispatches.ledger_path(), second))
        canonical = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(canonical["verdict_ref"], evidence)
        binding = dispatches._binding_key(canonical, self.side, evidence)
        intent = {"v": 1, "event": "intent", "id": row["id"], "ts": "t",
                  "room": "main", "binding": binding}
        done = {"v": 1, "event": "done", "id": row["id"], "ts": "t",
                "room": "main", "binding": binding, "payload": "", "turn": "",
                "receipt": "", "chain": None, "kind": "cite-tier",
                "text": "VERDICT historical"}
        self.assertTrue(eventledger.append(dispatches.attest_path(), intent))
        self.assertTrue(eventledger.append(dispatches.attest_path(), done))
        with open(dispatches.attest_path(), "rb") as f:
            before = f.read()
        self.assertTrue(before, "historical repair fixture wrote no attest sidecar")
        from helm import chat
        with mock.patch.object(chat, "post") as post:
            lr = landreq.get(row["id"])[0]
            card = landreq.card(lr)
        with open(dispatches.attest_path(), "rb") as f:
            after = f.read()
        self.assertEqual(before, after)
        post.assert_not_called()
        self.assertEqual(lr["polarity"], "approve")
        self.assertEqual(lr["polarity_source"], "dispatch-store")
        self.assertEqual(lr["attest_state"], "cite-tier")
        self.assertEqual(lr["attest_source"], "attest-sidecar")
        self.assertEqual(lr["ledger_refused"], ["verdict"])
        self.assertEqual(card["attest_state"], "cite-tier")
        self.assertIn("dispatch fold REFUSED historical verdict event",
                      landreq._line(lr))

    def test_attest_binding_mismatch_is_a_durable_visible_axis(self):
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "canonical", "approve")
        canonical = dispatches.snapshot()[0][row["id"]]
        wrong = {"v": 1, "event": "intent", "id": row["id"], "ts": "t",
                 "room": "main", "binding": "f" * 32}
        with open(dispatches.attest_path(), "w", encoding="utf-8") as f:
            f.write(json.dumps(wrong) + "\n")
        with open(dispatches.attest_path(), "rb") as f:
            before = f.read()
        lr = landreq.get(row["id"])[0]
        with open(dispatches.attest_path(), "rb") as f:
            after = f.read()
        self.assertEqual(before, after)
        self.assertEqual(canonical["polarity"], "approve")
        self.assertEqual(lr["attest_state"], "unverifiable")
        self.assertEqual(lr["attest_detail"], "attest intent binding mismatch")
        self.assertEqual(landreq.card(lr)["attest_state"], "unverifiable")
        self.assertIn("attest UNVERIFIABLE from attest sidecar", landreq._line(lr))
        self.assertIn("ATTEST", landreq._render_show(lr).upper())

    def test_the_fold_reports_which_events_it_TOOK(self):
        """At the seam, because this is the fact no consumer can reconstruct:
        two same-kind events, one accepted, and only the fold knows which."""
        row = self.refused_then_accepted_verdict()
        _current, by_id, taken, _order, _u = dispatches.snapshot_and_events()
        verdicts = [e for e in by_id[row["id"]] if e.get("event") == "verdict"]
        took = [e for e in taken[row["id"]] if e.get("event") == "verdict"]
        self.assertEqual(len(verdicts), 2)          # the record HOLDS two
        self.assertEqual(len(took), 1)              # the fold TOOK one
        self.assertEqual(took[0]["verdict_ref"], "the accepted one")

    def test_an_annotation_the_fold_never_applies_is_not_called_refused(self):
        """The negative that keeps the term honest. `notify-failed` is a real
        recorded fact the fold does not act on; naming every unapplied event
        would mark healthy rows and turn the warning into noise — and would be
        a second copy of `_apply`'s branch set, which is the rule split this
        whole line of defects came from."""
        row = self.dispatch(ref=self.side)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "id": row["id"], "v": 3, "seq": 1, "event": "notify-failed",
            "ts": "2026-07-30T00:00:00Z", "reason": "chat node down"}))
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["ledger_refused"], [])
        self.assertTrue(lr["notify_failed"])        # …and it is still READ

    def test_a_healthy_row_refuses_nothing(self):
        """The negative that keeps the term meaningful — every ordinary row
        would otherwise carry a warning that means nothing."""
        row = self.dispatch(ref=self.side)
        dispatches._mark_delivered(row["id"], "post-1")
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["ledger_refused"], [])
        self.assertNotIn("REFUSED", landreq._line(lr))
        self.assertEqual([s["state"] for s in lr["timeline"]],
                         ["OPEN", "AWAITING_REVIEW", "READY"])
        # EVERY step keeps its stamp. Narrowing the stamps to the fold's
        # accepted slice must not drop the event that OPENS the row — it is
        # accepted by `_new_state` rather than by `_apply`, and losing it takes
        # the opening instant with it (the timeline still DRAWS an OPEN step,
        # so a state-name-only assertion cannot see this).
        for step in lr["timeline"]:
            self.assertRegex(step["ts"] or "",
                             r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

    def test_two_BYTE_IDENTICAL_events_are_still_told_apart(self):
        """Why the accepted slice is matched by IDENTITY and not equality. Two
        events can be byte-identical with only one accepted — the second is
        out of sequence by then — and an equality test finds the refused twin
        "in" the accepted list and reports a clean row over an unrepaired
        ledger."""
        row = self.dispatch(ref=self.side)
        twin = {"id": row["id"], "v": 3, "seq": 1, "event": "verdict",
                "ts": "2026-07-30T00:00:00Z", "reviewed_tip": self.side,
                "verdict_ref": "twin", "polarity": "approve"}
        for _ in range(2):
            self.assertTrue(eventledger.append(dispatches.ledger_path(),
                                               dict(twin)))
        _current, by_id, taken, _order, _u = dispatches.snapshot_and_events()
        held = [e for e in by_id[row["id"]] if e.get("event") == "verdict"]
        took = [e for e in taken[row["id"]] if e.get("event") == "verdict"]
        self.assertEqual(len(held), 2)          # identical, and only one usable
        self.assertEqual(len(took), 1)
        self.assertEqual(landreq.get(row["id"])[0]["ledger_refused"],
                         ["verdict"])

    def test_a_delivery_the_fold_REFUSED_is_reconciled_the_same_way(self):
        """The other transition, bound separately: a fix that reconciled only
        the verdict would leave this one contradicting the headline."""
        row = self.dispatch(ref=self.side)
        dispatches._mark_delivered(row["id"], "post-1")
        self.assertEqual(self.one(self.lr())["state"], "AWAITING_REVIEW")
        self.edit_event(row["id"], "delivered", seq=99)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "OPEN")
        self.assertFalse(lr["notified"])
        self.assertNotIn("AWAITING_REVIEW", [s["state"] for s in lr["timeline"]])
        self.assertEqual(lr["ledger_refused"], ["delivered"])


class APartialProjectionIsAFailedProjectionTest(LrApiBase):
    """The SECOND audit, finding 1 — the same class one level lower down.

    `project()` wrapped each row in `except Exception: continue`, so a row it
    could not build was dropped from the board with nothing said: the snapshot
    still held it, `unavailable` stayed None, and the card printed "0 in
    flight … the dispatch ledger READ cleanly" over a ledger with a live land
    loop on it. One junk row emptied the board.

    His trigger was a `repo_id` that is not a path. It reaches git as itself —
    the field is copied verbatim out of the ledger row — and `_trunk_refs`
    uses it as a CACHE KEY, so a list raises TypeError one statement before
    `_git` would have declined it.

    Two separate properties follow, and they are tested separately: a repo
    binding helm cannot read makes the row UNOBSERVABLE (it survives, and says
    its trunk position is unknown), and anything that still raises takes the
    whole board to UNKNOWN rather than quietly shrinking it."""

    def junk_repo_id(self, rid, value):
        """Rewrite ONE row's repo_id on the ledger. Nothing re-validates this
        field between the ledger and git, which is how a non-path gets in."""
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        hit = 0
        with open(path, "w", encoding="utf-8") as f:
            for ev in events:
                if ev.get("id") == rid and "repo_id" in ev:
                    ev["repo_id"] = value
                    hit += 1
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")
        self.assertEqual(hit, 1, "no repo_id was rewritten — the fixture "
                                 "planted nothing and this test proves nothing")

    def approved(self):
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        return row

    def test_a_repo_id_that_is_not_a_path_keeps_its_row_on_the_board(self):
        row = self.approved()
        self.assertEqual(len(self.lr()["loops"]), 1)     # the healthy control
        self.junk_repo_id(row["id"], ["not", "a", "path"])
        body = self.lr()
        self.assertIsNone(body["unavailable"])
        self.assertEqual([c["id"] for c in body["loops"]], [row["id"]])

    def test_the_board_never_reports_a_clean_zero_over_a_snapshot_with_rows(self):
        """The lie itself, pinned as a NEGATIVE so no future shape can pass by
        rendering a calm empty board: the record holds the row, so the card may
        not simultaneously show no rows and claim it read successfully."""
        row = self.approved()
        self.junk_repo_id(row["id"], ["not", "a", "path"])
        current, snap_unavailable = dispatches.snapshot()
        self.assertIsNone(snap_unavailable)
        self.assertIn(row["id"], current)          # the RECORD has the row
        body = self.lr()
        self.assertFalse(not body["loops"] and body["unavailable"] is None,
                         "the board was empty AND reported a clean read while "
                         "the snapshot it came from held a land loop")

    def test_the_surviving_row_claims_no_git_fact_it_never_obtained(self):
        """Surviving is not enough. A repo binding that cannot be read is the
        UNOBSERVABLE the card already renders as NOT OBSERVED — never a row
        quietly reported as not on trunk."""
        row = self.approved()
        self.junk_repo_id(row["id"], ["not", "a", "path"])
        card = self.one(self.lr())
        self.assertFalse(card["observable"])
        self.assertFalse(card["landed"])
        self.assertFalse(card["merged_local"])

    def test_a_row_that_still_raises_takes_the_whole_board_to_UNKNOWN(self):
        """The backstop for every fault nobody has met yet, driven at the seam.
        helm cannot certify the rows that DID project — it does not know
        whether the fault is in this row or in the read that produced all of
        them — so the board says UNKNOWN and names the row to repair."""
        row = self.approved()
        with mock.patch.object(landreq, "_lr",
                               mock.Mock(side_effect=RuntimeError("boom"))):
            body = self.lr()
        self.assertIsInstance(body["unavailable"], str,
                              "a projection that lost a row said it was fine")
        self.assertIn(row["id"], body["unavailable"])   # WHICH row to repair
        self.assertIn("RuntimeError", body["unavailable"])
        self.assertIn("INCOMPLETE", body["unavailable"])
        self.assertEqual(body["loops"], [])


class ClosureIsMeasuredAtClosureTest(LrApiBase):
    """Finding 2, both halves.

    The window asked "when did this row enter its current state", which for a
    land loop is the VERDICT. An approve from three days ago whose change
    merges NOW leaves the in-flight list at that instant and was also outside a
    verdict-dated 24h window, so it fell off the card completely while the
    footer went on claiming nothing had closed."""

    def approved_days_ago_then_merged_now(self, days=3):
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.age(row["id"], days * 86400)          # the VERDICT is old
        self.git("merge", "--no-edit", "-q", "side")   # the LANDING is now
        return row

    def test_an_old_approve_that_merges_now_is_not_reported_as_nothing_closed(self):
        row = self.approved_days_ago_then_merged_now()
        body = self.lr()
        self.assertEqual(body["loops"], [])            # it did leave in-flight
        # and it is COUNTED, not dropped: the record cannot date this closure
        # (git keeps no moment for the merge helm observed), and saying so is
        # the only honest answer available.
        self.assertEqual(body["closed_unknown_when"], 1)
        lrs, _u = landreq.project()
        self.assertTrue(lrs[row["id"]]["terminal"])
        self.assertIsNone(lrs[row["id"]]["closed_ts"])

    def test_a_verdict_INSIDE_the_window_still_proves_the_closure_is_too(self):
        """A closure cannot precede the state it closed from, so an entry stamp
        inside the window is a LOWER BOUND that settles the question. Those rows
        stay in the list rather than joining the unknown pile."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        body = self.lr()
        self.assertEqual([c["id"] for c in body["closed_recent"]], [row["id"]])
        self.assertEqual(body["closed_unknown_when"], 0)
        self.assertEqual(body["closed_total"], 1)

    def test_ABANDONED_is_closed_recent_with_land_state_UNKNOWN(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        row = self.dispatch(ref=self.side, kind="review")
        self.mark_verdict(row["id"], self.side, "reviewed", polarity="fix")
        with mock.patch.object(landreq, "_commit_exists", return_value=False):
            lr, why = landreq.abandon(row["id"], "history rewrite wrote off work")
        self.assertIsNone(why)
        self.assertEqual(lr["state"], "ABANDONED")
        body = self.lr()
        self.assertEqual(body["loops"], [])
        self.assertEqual([c["id"] for c in body["closed_recent"]], [row["id"]])
        card = body["closed_recent"][0]
        self.assertTrue(card["abandoned"])
        self.assertEqual(card["land_state"], "UNKNOWN")
        self.assertEqual(card["abandon_reason"], "history rewrite wrote off work")

    def test_a_withdrawal_is_dated_from_the_withdrawal_not_from_the_verdict(self):
        """The rows that DO carry a closure stamp must use it. A FIX verdict
        from three days ago withdrawn a moment ago closed a moment ago."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
        self.age(row["id"], 3 * 86400)
        landreq.withdraw(row["id"], "abandoned in favour of the verdict")
        body = self.lr()
        self.assertEqual([c["id"] for c in body["closed_recent"]], [row["id"]])
        self.assertEqual(body["closed_unknown_when"], 0)
        card = body["closed_recent"][0]
        self.assertEqual(card["close_reason"], "withdrawn")
        # the CLOSURE stamp, not the three-day-old verdict it closed
        self.assertNotEqual(card["closed_ts"], card["entered_ts"])
        # …and it is a READABLE one, which is the control for the corrupt case
        # below: a fix that flagged every closure unreadable would drop every
        # stamp to None and still satisfy the line above.
        self.assertFalse(card["closed_ts_unreadable"])
        self.assertRegex(card["closed_ts"], r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

    def corrupt_one_events_ts(self, rid, event):
        """Break the stamp on ONE event of one row — the retirement, not the
        dispatch — so the row is dated everywhere else and undateable exactly
        where the closure is read from."""
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        hit = 0
        with open(path, "w", encoding="utf-8") as f:
            for ev in events:
                if ev.get("id") == rid and ev.get("event") == event:
                    ev["ts"] = "not-a-stamp"
                    hit += 1
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")
        self.assertEqual(hit, 1, "no %s event was corrupted — the fixture "
                                 "planted nothing" % event)

    def withdrawn_with_an_unreadable_stamp(self):
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
        landreq.withdraw(row["id"], "abandoned in favour of the verdict")
        self.corrupt_one_events_ts(row["id"], "close")
        return row

    def test_a_retirement_stamp_that_is_not_a_stamp_is_never_copied_out(self):
        """The SECOND audit, finding 2. The retirement ts was parsed for
        the dwell and then the RAW value was copied to `closed_ts`, so a
        withdrawn row carrying `ts: "not-a-stamp"` reached the card as
        `closed_ts: "not-a-stamp"` and rendered "closed not-a-stamp" — junk
        printed as the answer to the one question the field exists for."""
        row = self.withdrawn_with_an_unreadable_stamp()
        body = self.lr()
        card = [c for c in body["closed_recent"] if c["id"] == row["id"]]
        self.assertEqual(len(card), 1, body["closed_recent"])
        self.assertIsNone(card[0]["closed_ts"],
                          "an unparseable stamp was handed to the card as the "
                          "closure instant")
        self.assertTrue(card[0]["closed_ts_unreadable"])

    def test_the_unreadable_stamp_is_not_collapsed_into_having_no_stamp(self):
        """The other half, and this card's second-commonest defect: ABSENT
        must not read as UNREADABLE. "git kept no moment for this merge" and
        "the ledger's stamp is corrupt" are different facts with different
        repairs, so a row that HAS a bad stamp may not answer like a landing
        that never had one."""
        stamped = self.withdrawn_with_an_unreadable_stamp()
        # a git-OBSERVED landing: terminal, and no closure stamp exists
        # anywhere. On its OWN branch — merging `side` would put the withdrawn
        # row's tip on trunk and re-expose it as contrary, which is a different
        # lifecycle and not the comparison being made here.
        self.git("checkout", "-q", "-b", "side-observed", self.a)
        landing = self.commit("observed", path="h")
        self.git("checkout", "-q", self.main)
        other = dispatches.add("codex-3", "lane/observed", ref=landing,
                               repo=self.repo, new_work=True)
        self.mark_verdict(other["id"], landing, "ok", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side-observed")
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        self.assertTrue(lrs[stamped["id"]]["closed_ts_unreadable"])
        self.assertFalse(lrs[other["id"]]["closed_ts_unreadable"])
        self.assertIsNone(lrs[other["id"]]["closed_ts"])

    def test_thirteen_closed_rows_are_reported_as_thirteen_not_as_twelve(self):
        """The truncation half. The cap is a phone budget; presenting the
        capped LENGTH as the count made a 13th closed lane render as the
        number 12."""
        for i in range(13):
            self.git("checkout", "-q", "-b", "side-%d" % i, self.a)
            sha = self.commit("side %d" % i, path="f%d" % i)
            self.git("checkout", "-q", self.main)
            row = dispatches.add("codex-3", "lane/l%d" % i, ref=sha,
                                 repo=self.repo, new_work=True)
            self.mark_verdict(row["id"], sha, "ok", polarity="approve")
            self.git("merge", "--no-edit", "-q", "side-%d" % i)
        body = self.lr()
        self.assertEqual(body["closed_total"], 13)
        self.assertEqual(len(body["closed_recent"]), web._LR_CLOSED_CAP)
        self.assertLess(len(body["closed_recent"]), body["closed_total"])


class TheTimelineSaysWHICHKindOfMissingItIsTest(LrApiBase):
    """The ELEVENTH of this class, found while fixing the tenth — the same two
    lies as the closure stamp, in a rendering nobody had audited.

    1. A STAMP THAT IS NOT A STAMP WENT THROUGH VERBATIM. A verdict event with
       a corrupt ts printed `READY not-a-stamp`, on the card and in `helm lr
       show` both: junk in the position that answers "when did this happen".
    2. EVERY ABSENT STAMP WAS EXPLAINED AS A GIT OBSERVATION. "(git-observed,
       no ledger stamp)" is true of exactly ONE step — the landing git
       noticed, which carries no moment anywhere — and it was printed over any
       missing ts, including an OPEN step. helm does not read git at all until
       a verdict exists, so that reading is one the card could never have
       obtained.

    Three kinds of missing, three answers."""

    def step(self, lr, state):
        got = [s for s in lr["timeline"] if s["state"] == state]
        self.assertEqual(len(got), 1, lr["timeline"])
        return got[0]

    def test_an_unreadable_stamp_is_dropped_and_named_not_printed(self):
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.edit_event(row["id"], "verdict", ts="not-a-stamp")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertEqual(lr["tier_kind"], dispatches.TIER_DAMAGED)
        step = self.step(lr, "REVIEWED")
        self.assertIsNone(step["ts"])
        self.assertTrue(step["ts_unreadable"])
        self.assertNotIn("not-a-stamp", json.dumps(lr["timeline"]))
        # and the terminal stops printing it too
        self.assertIn("REVIEWED         (the ledger's stamp here is NOT A "
                      "TIMESTAMP)", landreq._render_show(lr))

    def test_a_ledger_event_with_no_stamp_is_not_called_git_observed(self):
        row = self.dispatch(ref=self.side)
        self.edit_event(row["id"], "dispatch", drop_ts=True)
        lr = landreq.get(row["id"])[0]
        step = self.step(lr, "OPEN")
        self.assertIsNone(step["ts"])
        self.assertFalse(step.get("observed"),
                         "an OPEN step was reported as git-observed; git is "
                         "not read at all before a verdict exists")
        self.assertFalse(step.get("ts_unreadable"))
        self.assertIn("OPEN             (no stamp on the ledger event)",
                      landreq._render_show(lr))

    def test_the_step_that_really_is_git_observed_says_so_itself(self):
        """The control, and the reason the annotation exists at all: a landing
        helm noticed has no moment anywhere, in any record."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        lr = landreq.get(row["id"])[0]
        step = self.step(lr, "LANDED")
        self.assertIsNone(step["ts"])
        self.assertTrue(step["observed"])
        self.assertIn("LANDED           (git-observed, no ledger stamp)",
                      landreq._render_show(lr))


class ARefusalAndABindingAreBothTrueAtOnceTest(LrApiBase):
    """The SECOND audit, finding 3, at the seam that MAKES the pair.

    The card's verification chip used to carry two different facts in one slot
    — the gate id bound to a row, and the record's refusal to call that row
    READY — with `gate` tested first. This pins that a row really can carry
    both, so the renderer's fixture for it is not hypothetical: a verdict
    written by a gate-capable writer whose `gate_caps` stamp cannot be read is
    refused READY (corruption may never authorize the transition), and nothing
    stops that same verdict from carrying a perfectly well-formed gate id."""

    def unreadable_caps_on_a_bound_verdict(self, rid, gate="0c7f3a91ab"):
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        hit = 0
        with open(path, "w", encoding="utf-8") as f:
            for ev in events:
                if ev.get("id") == rid and ev.get("event") == "verdict":
                    ev.update(gate=gate, gate_caps=None)
                    hit += 1
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")
        self.assertEqual(hit, 1, "no verdict event was rewritten")

    def test_a_bound_gate_and_a_refusal_are_reported_together(self):
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.assertEqual(self.one(self.lr())["state"], "READY")   # control
        self.unreadable_caps_on_a_bound_verdict(row["id"])
        card = self.one(self.lr())
        self.assertEqual(card["gate"], "0c7f3a91ab")
        self.assertEqual(card["tier_kind"], dispatches.TIER_DAMAGED)
        self.assertIn("does not bind this verdict", card["ungated"])
        self.assertEqual(card["state"], "REVIEWED")
        # Independently retain the gate diagnosis without overriding damage
        # to the earlier author-bound verdict context.
        raw = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(landreq.gate_requirement(raw), "unknown")
        self.assertIn("gate_caps", landreq._unknown_gate_caps_why(raw))


class AnUnreadableReceiptLedgerIsNotAnAbsentOneTest(LrApiBase):
    """Finding 4. `_receipts_by_tip` suppressed every non-corruption
    I/O failure into `{}`, so `_receipt_for` found no row, answered R_NONE, and
    the card printed "land receipt none". A PermissionError read as PROOF OF
    ABSENCE.

    Only the RECEIPT read is broken here. The dispatch ledger stays readable and
    git stays authoritative, because the point is that the lifecycle is
    unaffected and only the DIAGNOSTIC was false."""

    def landed_row(self):
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        return row

    @unittest.skipIf(os.geteuid() == 0, "root reads through any mode bits")
    def test_a_receipt_ledger_that_cannot_be_read_says_so_not_none(self):
        row = self.landed_row()
        self.unreadable_receipts()
        card = self.lr()["closed_recent"][0]
        self.assertEqual(card["id"], row["id"])
        self.assertEqual(card["receipt_state"], landreq.R_UNREADABLE)
        self.assertNotEqual(card["receipt_state"], landreq.R_NONE)

    @unittest.skipIf(os.geteuid() == 0, "root reads through any mode bits")
    def test_the_git_lifecycle_is_untouched_by_the_unreadable_index(self):
        """Receipts are diagnostics and may never gate landing. UNREADABLE has
        to be as fail-open as the empty index it replaces."""
        self.landed_row()
        self.unreadable_receipts()
        card = self.lr()["closed_recent"][0]
        self.assertEqual(card["state"], "LANDED")
        self.assertTrue(card["landed"])

    @unittest.skipIf(os.geteuid() == 0, "root reads through any mode bits")
    def test_the_reason_reaches_the_reader_verbatim(self):
        row = self.landed_row()
        self.unreadable_receipts()
        lr = landreq.get(row["id"])[0]
        self.assertIn("PermissionError", lr["receipt_reason"])
        self.assertIn("land receipt ledger UNREADABLE", landreq._line(lr))
        self.assertIn("NOT the same as no receipt", landreq._render_show(lr))

    def test_a_CORRUPT_row_stays_REJECTED_and_does_not_become_unreadable(self):
        """The two failures are different claims and must stay different: a
        malformed complete row is a receipt that EXISTS and is bad."""
        path = landreq.receipts_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json at all}\n")
        state, _row, why = landreq._receipt_for(self.side,
                                                landreq._receipts_by_tip())
        self.assertEqual(state, landreq.R_REJECTED)
        self.assertIn("corrupt ledger line", why)

    def test_an_index_that_READ_and_holds_nothing_is_still_none(self):
        """The negative that keeps UNREADABLE honest: a receipt ledger that
        genuinely has no row for this tip must NOT start claiming UNKNOWN."""
        self.landed_row()
        state, _row, _why = landreq._receipt_for(self.side,
                                                 landreq._receipts_by_tip())
        self.assertEqual(state, landreq.R_NONE)


class AnUnreadableLedgerReachesTheRowsThatSKIPGitTest(LrApiBase):
    """The SECOND time unreadable receipt storage rendered as "receipt: none"
    on this card, and it slipped through because the first fix landed one call
    site downstream.

    `_receipt_for` was taught to answer UNREADABLE — but two row kinds never
    reached it. An OPEN loop and a CLOSED-BY-LANDING row both skipped
    `_observe` ENTIRELY (git is not read for either: nothing is observed before
    a verdict, and a recorded closure is immutable), and their receipt state
    then came from a `.get(..., R_NONE)` DEFAULT. So a PermissionError on the
    receipt index printed "land receipt none" on exactly the rows nobody had
    looked up. The git observation is what those rows skip; the receipt answer
    is a dict lookup in an index the projection has already read."""

    def closed_by_landing(self):
        row = self.dispatch(ref=self.side)
        self.legacy_undeclared(row)
        self.git("merge", "--no-edit", "-q", "side")
        lr, why = landreq.close_landed(row["id"], trunk=self.main, live=True)
        self.assertIsNone(why, why)
        self.assertEqual(lr["close_reason"], "landed")
        return row

    @unittest.skipIf(os.geteuid() == 0, "root reads through any mode bits")
    def test_an_OPEN_row_says_the_index_was_UNREADABLE_not_that_none_exists(self):
        self.dispatch(ref=self.side)
        self.unreadable_receipts()
        card = self.one(self.lr())
        self.assertEqual(card["state"], "OPEN")            # the row kind
        self.assertEqual(card["receipt_state"], landreq.R_UNREADABLE)
        self.assertNotEqual(card["receipt_state"], landreq.R_NONE)

    @unittest.skipIf(os.geteuid() == 0, "root reads through any mode bits")
    def test_a_CLOSED_BY_LANDING_row_says_it_too(self):
        row = self.closed_by_landing()
        self.unreadable_receipts()
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["close_reason"], "landed")     # the row kind
        self.assertEqual(lr["receipt_state"], landreq.R_UNREADABLE)
        self.assertIn("PermissionError", lr["receipt_reason"])

    @unittest.skipIf(os.geteuid() == 0, "root reads through any mode bits")
    def test_the_LIFECYCLE_of_both_kinds_is_untouched(self):
        """Receipts are diagnostics and may never gate anything. Consulting the
        index for these rows must not change what they ARE — and in particular
        must not start reading git for a row that had deliberately skipped it."""
        landed = self.closed_by_landing()
        # notify=False keeps it genuinely OPEN: this arm is about receipts not
        # changing what a row IS, and delivery is what moves OPEN ->
        # AWAITING_REVIEW. A mention here would change the row out from under
        # the assertion for a reason that has nothing to do with receipts.
        open_row = dispatches.add("codex-3", "lane/open", ref=self.b,
                                  repo=self.repo, new_work=True, notify=False)
        self.unreadable_receipts()
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        self.assertTrue(lrs[landed["id"]]["terminal"])
        self.assertEqual(lrs[landed["id"]]["close_reason"], "landed")
        self.assertEqual(lrs[open_row["id"]]["state"], "OPEN")
        self.assertFalse(lrs[open_row["id"]]["observable"])

    def test_a_READABLE_index_holding_nothing_still_answers_none(self):
        """The negative that keeps UNREADABLE honest on these rows too: an
        index that was actually READ and has no row for this tip is an absence,
        and must not start claiming UNKNOWN."""
        self.dispatch(ref=self.side)
        path = landreq.receipts_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w", encoding="utf-8").close()
        card = self.one(self.lr())
        self.assertEqual(card["state"], "OPEN")
        self.assertEqual(card["receipt_state"], landreq.R_NONE)

    def test_the_projection_never_INVENTS_a_receipt_state_it_was_not_given(self):
        """THE THIRD BYPASS, on the python side. `card()` defaulted the field to
        R_NONE, so any row that reached it without one — a future path that
        skips the lookup the way these two skipped `_observe` — was published to
        the browser as a receipt helm looked for and did not find. A projection
        may not answer a question its input never answered."""
        row = self.dispatch(ref=self.side)
        lr = landreq.get(row["id"])[0]
        del lr["receipt_state"]
        self.assertIsNone(landreq.card(lr)["receipt_state"])
        self.assertNotEqual(landreq.card(lr)["receipt_state"], landreq.R_NONE)


class AVerdictAtAnUnknownInstantIsStillAVerdictTest(LrApiBase):
    """The fourth audit, finding 1 — the missing-value shape at the one
    place it bills the owner for time.

    A verdict event whose `ts` the ledger does not carry collapsed to None, and
    None means "there was no verdict event" everywhere downstream. Two lies
    followed from the one collapse:

    1. `post_verdict = verdict_ts or delivered_ts or open_ts` fell through to
       the DELIVERY instant. His exact repro: APPROVE at an unknown time,
       delivery at 00:10, board read at 03:00 => state READY, entered_ts 00:10,
       dwell_known TRUE, dwell 10200s, STALLED. Every one of those numbers is a
       measurement of an interval the record never recorded.
    2. `_timeline` tested the STAMP to decide whether the STEP existed, so the
       verdict vanished off the row's history entirely — a card showing OPEN ->
       AWAITING_REVIEW above a headline that says READY.

    The fallback belongs to the LEGACY row alone: a snapshot verdict with no
    discrete event, which is genuinely absent rather than undated."""

    HOURS = 2 * 3600 + 50 * 60          # the 2h50 of the reproduction

    def approve_then_lose_the_verdict_stamp(self, drop=True):
        row = self.dispatch(ref=self.side)
        dispatches._mark_delivered(row["id"], "post-1")
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        # THE INSTANT IS KEPT so the arms below can pin the projection to it;
        # an exact-dwell assertion read at a later instant is asserting that
        # no second boundary crossed, which is not what any of them mean.
        self.aged_at = self.age(row["id"], self.HOURS)
        if drop:
            self.edit_event(row["id"], "verdict", drop_ts=True)
        return row

    def test_the_control_the_same_row_WITH_a_stamp_is_dated_and_STALLS(self):
        """First, that the fixture really does produce the confident reading —
        otherwise the test below passes over a row that was never dateable."""
        self.approve_then_lose_the_verdict_stamp(drop=False)
        card = self.one(self.lr_at(self.aged_at))
        self.assertEqual(card["state"], "READY")
        self.assertTrue(card["dwell_known"])
        self.assertEqual(card["dwell_s"], self.HOURS)
        self.assertTrue(card["stalled"])          # past the 1h land budget

    def test_an_APPROVE_helm_cannot_date_is_not_dated_from_the_DELIVERY(self):
        self.approve_then_lose_the_verdict_stamp()
        card = self.one(self.lr())
        self.assertEqual(card["state"], "REVIEWED")  # timestamp binding is damaged
        self.assertEqual(card["tier_kind"], dispatches.TIER_DAMAGED)
        self.assertFalse(card["dwell_known"])
        self.assertIsNone(card["entered_ts"])
        self.assertNotEqual(card["dwell_s"], self.HOURS)

    def test_and_it_can_never_STALL_on_a_number_nothing_measured(self):
        """Pinned separately from the dwell: a fix that flagged the dwell
        unknown while still billing the stall would satisfy the test above and
        leave the alarm — the loudest thing on the card — asserting."""
        self.approve_then_lose_the_verdict_stamp()
        body = self.lr()
        self.assertFalse(self.one(body)["stalled"])
        self.assertEqual(body["stalled_ids"], [])

    def test_the_verdict_STEP_survives_a_ledger_event_with_no_stamp(self):
        """The other half of the same collapse, and the defect reported but not
        fixed alongside the three: the step is drawn by the EVENT's existence,
        never by its stamp."""
        row = self.approve_then_lose_the_verdict_stamp()
        lr = landreq.get(row["id"])[0]
        states = [s["state"] for s in lr["timeline"]]
        self.assertEqual(states, ["OPEN", "AWAITING_REVIEW", "REVIEWED"])
        self.assertEqual(lr["tier_kind"], dispatches.TIER_DAMAGED)
        step = [s for s in lr["timeline"] if s["state"] == "REVIEWED"][0]
        self.assertIsNone(step["ts"])
        # …and it says WHICH kind of missing it is: there is no stamp here, so
        # neither "the stamp is not a timestamp" nor "git observed this".
        self.assertFalse(step.get("ts_unreadable"))
        self.assertFalse(step.get("observed"))
        self.assertIn("REVIEWED         (no stamp on the ledger event)",
                      landreq._render_show(lr))

    def test_a_DELIVERY_with_no_stamp_is_still_a_delivery(self):
        """The same two lies one stage earlier: the row was awaiting review
        since an unknown moment, not since it opened, and it HAD been
        notified — `notified: false` beside a delivery event on the record is
        the ledger's own fact denied because helm could not date it."""
        row = self.dispatch(ref=self.side)
        dispatches._mark_delivered(row["id"], "post-1")
        self.age(row["id"], self.HOURS)
        self.edit_event(row["id"], "delivered", drop_ts=True)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "AWAITING_REVIEW")
        self.assertTrue(lr["notified"])
        self.assertIn("AWAITING_REVIEW", [s["state"] for s in lr["timeline"]])
        self.assertFalse(lr["dwell_known"])
        self.assertIsNone(lr["entered_ts"])

    def test_a_LEGACY_row_with_no_verdict_event_still_falls_back(self):
        """The case the `or` chain was written for, kept working: a snapshot
        verdict carries no discrete event at all, which is ABSENCE, and dating
        it from the opening stamp is the best the record supports."""
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                           time.gmtime(time.time() - self.HOURS))
        rid = "aabbccddeeff0011"
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "id": rid, "ts": ts, "recipient": "codex-3", "lane": "lane/legacy",
            "ref": self.side, "tip": self.side, "note": None, "deadline_s": 60,
            "source": "old", "status": "verdict", "verdict_ref": "reviewed",
            "reviewed_tip": self.side, "last_updated": ts}))
        lr = landreq.get(rid)[0]
        self.assertTrue(lr["dwell_known"])
        self.assertEqual(lr["entered_ts"], ts)      # the OPENING stamp
        self.assertGreaterEqual(lr["dwell_s"], self.HOURS)

    def test_a_SUPERSEDE_with_no_stamp_has_no_closure_INSTANT_to_be_wrong_about(self):
        """The closure stamp of a SUPERSEDED row IS its verdict, so the marker
        reaches that read too — and there it means ABSENCE. Reported as
        `closed_ts_unreadable` it would print "the ledger's own retirement
        stamp is NOT A TIMESTAMP" over a row that carries no stamp at all,
        which is this card's two states swapped rather than distinguished."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "superseded",
                                polarity="supersede")
        self.edit_event(row["id"], "verdict", drop_ts=True)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "SUPERSEDED")
        self.assertTrue(lr["terminal"])
        self.assertIsNone(lr["closed_ts"])
        self.assertFalse(lr["closed_ts_unreadable"])

    def test_the_two_owners_of_presence_answer_it_separately(self):
        """Bound at the seam rather than only end to end, because there are two
        halves and either one alone would let the collapse back in: the reader
        that MINTS the marker, and the chooser that must not fall past it.

        The marker is deliberately FALSY — as a stamp it holds no instant — so
        `verdict_ts or delivered_ts` is the ORIGINAL defect again rather than
        an accidental second guard that would leave `_first` unmeasured."""
        events = [{"event": "dispatch", "ts": "2026-07-30T00:00:00Z"},
                  {"event": "delivered", "ts": "2026-07-30T00:10:00Z"},
                  {"event": "verdict"}]                  # no ts at all
        open_ts, delivered_ts, verdict_ts = landreq._transitions(events)
        self.assertIs(verdict_ts, landreq.UNSTAMPED)     # PRESENT, undated
        self.assertFalse(bool(verdict_ts))               # …and holds no instant
        self.assertEqual(delivered_ts, "2026-07-30T00:10:00Z")
        self.assertIs(landreq._first(verdict_ts, delivered_ts, open_ts),
                      landreq.UNSTAMPED)
        # an event that is genuinely ABSENT is the one case that falls through
        self.assertEqual(landreq._first(None, delivered_ts, open_ts),
                         "2026-07-30T00:10:00Z")

    def test_the_presence_marker_never_reaches_the_wire(self):
        """It is a marker, not an instant: `/api/lr` would answer 500 rather
        than a board if one reached the JSON encoder, so the check runs THROUGH
        the encoder rather than beside it."""
        self.approve_then_lose_the_verdict_stamp()
        body = json.loads(json.dumps(self.lr()))
        card = body["loops"][0]
        self.assertIsNone(card["entered_ts"])
        self.assertNotIn("UNSTAMPED", json.dumps(body))


class AClosureDatedInTheFutureIsNotRecentTest(LrApiBase):
    """The fourth audit, finding 3. The window had one side.

    `now - ts <= 86400` is satisfied by every instant in the FUTURE as well —
    the subtraction goes negative and sails under the ceiling — so a row whose
    record says it closed in 2099 was counted, confidently, as "closed in the
    last 24h", printed at the top of the list (the sort is newest-first), and
    changed the exact total the footer reports. A closure that has not happened
    yet is not recent; it is a broken clock or a broken record."""

    FUTURE = "2099-01-01T00:00:00Z"

    def withdrawn_with(self, stamp):
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
        landreq.withdraw(row["id"], "abandoned in favour of the verdict")
        self.age(row["id"], 3 * 86400)      # the verdict is OUTSIDE the window
        if stamp:
            self.edit_event(row["id"], "close", ts=stamp)
        return row

    def test_the_control_a_row_withdrawn_NOW_is_counted(self):
        """The fixture must be able to produce a counted row, or the assertion
        below is measuring an empty board."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
        landreq.withdraw(row["id"], "abandoned in favour of the verdict")
        body = self.lr()
        self.assertEqual([c["id"] for c in body["closed_recent"]], [row["id"]])
        self.assertEqual(body["closed_total"], 1)

    def test_a_row_closed_in_2099_is_not_closed_in_the_last_24h(self):
        self.withdrawn_with(self.FUTURE)
        body = self.lr()
        self.assertEqual(body["closed_recent"], [])
        self.assertEqual(body["closed_total"], 0)
        # counted, never dropped: helm cannot date this closure, and that is
        # its own answer rather than a silent absence from the footer
        self.assertEqual(body["closed_unknown_when"], 1)

    def test_the_dwell_of_a_row_retired_at_an_impossible_instant_is_UNKNOWN(self):
        """The same stamp read the other way. The dwell measures to the
        retirement instant; an unusable one fell back to `now` and kept
        COUNTING, so a lane retired at an unreadable moment rendered an age
        that grows every time the card is drawn."""
        row = self.withdrawn_with(self.FUTURE)
        lr = landreq.get(row["id"])[0]
        self.assertTrue(lr["terminal"])
        self.assertFalse(lr["dwell_known"])

    def test_a_row_that_ENTERED_its_state_in_the_future_has_no_dwell_either(self):
        """`_age_s` clamps its subtraction at 0, so a future entry stamp read
        as a permanent, confident "0m" — the freshest thing on the board, for
        as long as the row exists."""
        row = self.dispatch(ref=self.side)
        with mock.patch.object(dispatches.pk, "now_ts", return_value=self.FUTURE):
            verdict, err = self.mark_verdict(
                row["id"], self.side, "ok", polarity="approve")
        self.assertIsNone(err, err)
        self.assertEqual(dispatches.approval_tier_for_verdict(verdict), ("none", None))
        card = self.one(self.lr())
        self.assertEqual(card["state"], "READY")
        self.assertFalse(card["dwell_known"])
        self.assertFalse(card["stalled"])

    def test_the_window_helper_answers_all_three_states(self):
        """The owner of both readings, unit-tested at its own boundary: the
        closure stamp and the entered_ts lower bound go through this one
        function, so a guard applied to one comparison and not the other is not
        a shape this can take."""
        now = 1_900_000_000.0
        self.assertEqual(web._lr_window(now - 60, now), "in")
        self.assertEqual(web._lr_window(now - 2 * 86400, now), "out")
        self.assertEqual(web._lr_window(now + 86400, now), "unusable")
        self.assertEqual(web._lr_window(None, now), "unusable")
        # a stamp seconds ahead is a clock that stepped, not a record from the
        # future — the tolerance is named and small
        self.assertEqual(web._lr_window(now + 5, now), "in")

    def test_an_impossible_closure_stamp_is_never_SENT_to_the_card(self):
        """Conceded: this one was left short. The window
        ruled the 2099 instant unusable and the ROW still reached the card
        carrying `closed_ts: "2099-01-01T00:00:00Z"`, which the panel printed
        as the closure instant — so the surface both showed the impossible date
        and, via its entry lower bound, acted on it. Absurd is not the same as
        harmless: the row is on the board, and the date beside it is a value
        nothing measured."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
        landreq.withdraw(row["id"], "abandoned in favour of the verdict")
        self.edit_event(row["id"], "close", ts=self.FUTURE)
        body = self.lr()
        card = [c for c in body["closed_recent"] if c["id"] == row["id"]]
        self.assertEqual(len(card), 1, body["closed_recent"])
        self.assertIsNone(card[0]["closed_ts"], "the impossible stamp was sent")
        self.assertNotIn(self.FUTURE, json.dumps(body))
        # it is its own state, NOT the unreadable one: nothing here failed to
        # parse, and telling him to re-read a stamp that reads fine is a repair
        # instruction for the wrong defect
        self.assertTrue(card[0]["closed_ts_impossible"])
        self.assertFalse(card[0]["closed_ts_unreadable"])
        # …and the row is still SHOWN, because the entry stamp does prove the
        # closure fell inside the window — dropping it would be the older lie
        self.assertEqual(body["closed_total"], 1)

    def test_the_TIMELINE_step_refuses_the_same_stamp(self):
        """The near-miss, caught by asserting the whole wire rather than the
        one field: suppressing the impossible CLOSURE instant left the same
        value printing one line lower, because the retirement annotation is
        itself a timeline step — `WITHDRAWN 2099-01-01T00:00:00Z`, rendered
        verbatim on the card. A stamp is refused in every position it can be
        read from, or it is not refused."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
        landreq.withdraw(row["id"], "abandoned in favour of the verdict")
        self.edit_event(row["id"], "close", ts=self.FUTURE)
        lr = landreq.get(row["id"])[0]
        step = [s for s in lr["timeline"]
                if s["state"] == "CLOSED_WITHDRAWN"][0]
        self.assertIsNone(step["ts"])
        self.assertTrue(step["ts_impossible"])
        self.assertFalse(step.get("ts_unreadable"))     # not that repair
        self.assertNotIn(self.FUTURE, json.dumps(lr["timeline"]))
        self.assertIn("CLOSED_WITHDRAWN (the ledger's stamp here is dated in "
                      "the FUTURE)", landreq._render_show(lr))

    def test_an_ordinary_stamp_is_still_printed(self):
        """The control for all of it: a real stamp must survive four refusals."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
        landreq.withdraw(row["id"], "abandoned in favour of the verdict")
        lr = landreq.get(row["id"])[0]
        step = [s for s in lr["timeline"]
                if s["state"] == "CLOSED_WITHDRAWN"][0]
        self.assertRegex(step["ts"], r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
        self.assertFalse(step.get("ts_impossible"))
        self.assertFalse(lr["closed_ts_impossible"])
        self.assertEqual(lr["closed_ts"], step["ts"])

    def test_the_terminal_says_it_too(self):
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
        landreq.withdraw(row["id"], "abandoned in favour of the verdict")
        self.edit_event(row["id"], "close", ts=self.FUTURE)
        lr = landreq.get(row["id"])[0]
        self.assertIn("closure stamp IMPOSSIBLE", landreq._line(lr))
        self.assertIn("dated in the FUTURE", landreq._render_show(lr))

    def test_a_retirement_with_NO_stamp_at_all_is_undateable_too(self):
        """The third: `retired and retire_ts` walked past the no-stamp
        case entirely, so a withdraw event recorded without a ts left the row
        terminal with dwell_known TRUE and 259200s GROWING every draw. The flag
        is what asserts the retirement; the stamp only says when."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
        landreq.withdraw(row["id"], "abandoned in favour of the verdict")
        self.age(row["id"], 3 * 86400)
        self.edit_event(row["id"], "close", drop_ts=True)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["close_reason"], "withdrawn")
        self.assertTrue(lr["terminal"])
        self.assertFalse(lr["dwell_known"])
        # the NUMBER stays (every consumer sorts and formats it); what may not
        # survive is its presentation as a measurement
        self.assertIn("dwell UNKNOWN", landreq._line(lr))
        self.assertIn("never measured", landreq._render_show(lr))
        # nothing to re-read and nothing impossible — the stamp is simply absent
        self.assertFalse(lr["closed_ts_unreadable"])
        self.assertFalse(lr["closed_ts_impossible"])
        self.assertIsNone(lr["closed_ts"])

    def test_a_TERMINAL_row_s_dwell_stops_when_it_CLOSED(self):
        """The second blocker, and the one named as left-unfixed.

        A git-OBSERVED landing is terminal and carries no closure stamp
        ANYWHERE — git records no moment for the merge helm noticed. The dwell
        measured to `now` anyway, so the same closed row read 3600s at one
        projection and 7200s an hour later, both with dwell_known TRUE. Git
        proves the interval ENDED and supplies no END INSTANT; a number over
        that is the same confident-clock-over-an-unknown as the other four.

        I deferred this because a terminal row never stalls, so it misleads
        without alarming — which is precisely the argument for fixing it: quiet
        is the failure mode this card exists to remove."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.age(row["id"], 3600)
        self.git("merge", "--no-edit", "-q", "side")
        now = time.time()
        first = landreq.project(now)[0][row["id"]]
        later = landreq.project(now + 3600)[0][row["id"]]
        for lr in (first, later):
            self.assertEqual(lr["state"], "LANDED")
            self.assertTrue(lr["terminal"])
            self.assertIsNone(lr["closed_ts"])
            self.assertFalse(lr["dwell_known"])
        self.assertIn("dwell UNKNOWN", landreq._line(first))
        self.assertIn("none to measure TO", landreq._render_show(first))

    def test_a_terminal_row_that_DOES_carry_its_closure_instant_is_measured(self):
        """The control, and the half that must not break: a withdrawal writes a
        real closure stamp, so the interval has both ends and the dwell is a
        measurement — one that does NOT keep growing after the closure."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
        self.age(row["id"], 3600)
        landreq.withdraw(row["id"], "abandoned in favour of the verdict")
        now = time.time()
        first = landreq.project(now)[0][row["id"]]
        later = landreq.project(now + 3600)[0][row["id"]]
        self.assertTrue(first["dwell_known"] and later["dwell_known"])
        self.assertEqual(first["dwell_s"], later["dwell_s"])   # it STOPPED
        self.assertGreaterEqual(first["dwell_s"], 3600)

    def test_a_LIVE_row_still_measures_to_now(self):
        """The other control: only a CLOSED interval ends before now, and a
        fix that stopped every clock would hide every real stall."""
        row = self.dispatch(ref=self.side)
        dispatches._mark_delivered(row["id"], "post-1")
        # THE PROJECTION'S CLOCK IS THE ONE THE FIXTURE BACKDATED FROM. Taking
        # time.time() here instead reads the clock a second time and makes the
        # two exact dwells below depend on no boundary having crossed.
        now = float(self.age(row["id"], 3600))
        first = landreq.project(now)[0][row["id"]]
        later = landreq.project(now + 3600)[0][row["id"]]
        self.assertFalse(first["terminal"])
        self.assertTrue(first["dwell_known"])
        self.assertEqual(first["dwell_s"], 3600)
        self.assertEqual(later["dwell_s"], 7200)               # still running

    def test_a_lower_bound_inside_the_window_still_proves_the_closure(self):
        """The half that must NOT change: an undateable closure whose row
        entered its state an hour ago closed within the window, because a loop
        cannot close before the state it closed from."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")     # git-OBSERVED landing
        body = self.lr()
        self.assertEqual([c["id"] for c in body["closed_recent"]], [row["id"]])
        self.assertEqual(body["closed_unknown_when"], 0)


class ADwellOfZeroIsNotAMeasurementTest(LrApiBase):
    """The FIFTH of this class, found while fixing the four. `dispatches._age_s`
    answers 0 for a stamp it cannot parse — i.e. "just now", the most flattering
    value there is — so a row whose entry event carries a corrupt or missing ts
    rendered 0m, could never cross a stall threshold, and sorted to the top of
    the board as the freshest thing on it. The projection already knew this
    hazard for the closed WINDOW (an unparseable stamp is refused there) and
    still handed the same fabricated zero to the dwell column."""

    def corrupt_the_stamps(self, rid):
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for ev in events:
                if ev.get("id") == rid:
                    ev["ts"] = "not-a-stamp"
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")

    def test_an_unreadable_entry_stamp_marks_the_dwell_UNKNOWN(self):
        row = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(row["id"], "post-1")
        self.assertTrue(self.one(self.lr())["dwell_known"])   # readable first
        self.corrupt_the_stamps(row["id"])
        card = self.one(self.lr())
        self.assertFalse(card["dwell_known"])
        self.assertEqual(card["dwell_s"], 0)     # the fabricated value survives…
        # …flagged, so nothing downstream may read it as an age

    def test_the_terminal_row_prints_a_question_mark_not_a_zero(self):
        row = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(row["id"], "post-1")
        self.corrupt_the_stamps(row["id"])
        lr = landreq.get(row["id"])[0]
        line = landreq._line(lr)
        self.assertNotIn("0m", line)
        self.assertIn("dwell UNKNOWN", line)
        self.assertIn("UNKNOWN (this row's age was never measured",
                      landreq._render_show(lr))


class RecomputeFloorTest(LrApiBase):
    def test_the_floor_holds_a_board_and_releasing_it_shows_the_change(self):
        """landreq.project_raw() spawns git per closed row per repo — the
        thread-pool starvation class that bit /api/chat. The floor is asserted
        by its EFFECT: a real ledger change is withheld until it expires."""
        self.dispatch(lane="lane/example-first")
        first = self.lr()
        # #142: rows mint with the lane/ prefix stripped, so every projection
        # below carries the stored bare spelling.
        self.assertEqual([c["lane"] for c in first["loops"]], ["example-first"])

        self.dispatch(lane="lane/example-second")
        held, status = web.QUERY_API["/api/lr"]({})     # no forget: floor holds
        self.assertEqual(status, 200)
        self.assertEqual([c["lane"] for c in held["loops"]], ["example-first"])

        after = self.lr()                                # floor released
        self.assertEqual(sorted(c["lane"] for c in after["loops"]),
                         ["example-first", "example-second"])

    def test_inside_the_floor_the_projection_is_not_walked_at_all(self):
        """The floor's actual job: repeated polls (45s client, several tabs)
        must not each spawn git. Asserted by making a second walk fatal."""
        self.dispatch()
        self.lr()
        with mock.patch.object(landreq, "project_raw",
                               mock.Mock(side_effect=AssertionError("re-walked"))):
            body, _status = web.QUERY_API["/api/lr"]({})   # no forget
        self.assertIsNone(body["unavailable"])
        self.assertEqual(len(body["loops"]), 1)

    def test_the_floor_never_outlives_the_read_it_reports(self):  # noqa: VACUOUS_ASSERTION — the first body's bound is the baseline; the positive control on the same field is the held body's read_age_s of at least 12
        """The freshness header is what makes the floor honest — a body served
        out of the cache must say how old it is, not pretend it is now."""
        self.dispatch()
        took = time.time()
        first = self.lr()
        took = time.time() - took
        # A fresh body is no older than its own build, in whole seconds: 0
        # whenever the build took under a second (the slower build node's
        # recorded whole suites took longer, and read 1).
        self.assertLessEqual(first["read_age_s"], int(took))
        with mock.patch.object(web.time, "time",
                               mock.Mock(return_value=time.time() + 12)):
            held, _status = web.QUERY_API["/api/lr"]({})   # no forget
        self.assertGreaterEqual(held["read_age_s"], 12)

    def test_read_age_is_the_readings_age_and_projected_age_the_bodys(self):  # noqa: VACUOUS_ASSERTION — the first body's ages, bounded by its own build, are the baseline; the positive control on the same fields is the held body's projected_age_s at a full floor-gap with read_age_s strictly below it
        """TWO AGES ON THE WIRE. Past the floor, a poll whose fingerprint
        matches the body's is a read of every input: `read_age_s` restarts
        from that read while `projected_age_s` keeps counting from the build.
        The cards judge their bound on the first — measured on the owner's
        console, judging it on the second rendered every land-pipeline band
        STALE for most of each cycle over an unmoved ledger.

        LOAD-BEARING MUTATION: stamp `read_age_s` from `built` alone.
          -> AssertionError: the two ages agree

        BOTH OFFSETS ARE DERIVED FROM THE FLOOR, NOT TYPED, and the second is
        a GAP rather than an offset. They were 40 and 80 beside a floor of 30,
        and every step below depends on being PAST the floor — the first poll
        only records a mark because the body is stale, and the second only
        confirms one because its own entry, the one that first rebuild
        stamped, is stale too. Read as offsets from `base` those two numbers
        hide that the second measures from the FIRST, so raising the floor
        over their difference drops the second poll into the FRESH branch
        where no fingerprint is compared at all. The failure then reports two
        equal ages, which reads as the mutation this test is here to catch
        rather than as a constant that moved underneath it."""
        self.dispatch()
        took = time.time()
        first = self.lr()
        took = time.time() - took
        # The baseline: a fresh body is no older than its own build, in whole
        # seconds (0 whenever the build took under a second).
        self.assertLessEqual(first["read_age_s"], int(took))
        self.assertLessEqual(first["projected_age_s"], int(took))
        base = time.time()
        # BOTH POLLS MUST BE PAST THE FLOOR, AND THE SECOND ONE IS PAST IT
        # RELATIVE TO THE BODY THE FIRST ONE REBUILT — not to `base`. The
        # rebuild the first poll kicks stamps its entry at `stale_at`, so what
        # the second poll's staleness is measured against is the GAP between
        # them. Typed absolutes hid that: 40 and 80 beside a floor of 30 are a
        # gap of 40, and reading them as offsets from `base` puts the second
        # poll in the FRESH branch the moment the floor passes 40 — where no
        # fingerprint is compared and no mark is confirmed, which is the whole
        # subject here.
        #
        # AND THE SECOND POLL STAYS INSIDE `_LR_UNCHANGED_MAX_S`: past the cap
        # it rebuilds instead of confirming, a different branch that would
        # prove nothing about these two ages.
        stale_at = web._LR_FLOOR_S + 10
        held_at = stale_at + web._LR_FLOOR_S + 10
        self.assertLess(held_at - stale_at,
                        web_land_model_mod()._LR_UNCHANGED_MAX_S)
        with mock.patch.object(web.time, "time",
                               mock.Mock(return_value=base + stale_at)):
            stale, _status = web.QUERY_API["/api/lr"]({})   # records the mark
        _join_swr("lr")                                      # ...and rebuilds
        self.assertGreaterEqual(stale["read_age_s"], stale_at)
        self.assertEqual(stale["read_age_s"], stale["projected_age_s"],
                         "a first comparison confirms nothing")
        with mock.patch.object(web.time, "time",
                               mock.Mock(return_value=base + held_at)):
            held, _status = web.QUERY_API["/api/lr"]({})    # the mark matches
        self.assertGreaterEqual(held["projected_age_s"],
                                held_at - stale_at - 1)
        self.assertLess(held["read_age_s"], held["projected_age_s"])
        self.assertLessEqual(held["read_age_s"], 1)


# THE CARD RUNTIME HARNESS WENT WITH THE CARDS IT RAN (task/3643, slice 6).
# `CardRuntimeBase` lifted the task queue's headline and rows (`tqHeadHTML`,
# `tqCard`) and one land request's markup (`lbReqHTML`) out of the page and
# ran them under node; the backlog those drew is the Work page's List lens
# now and the land requests are its cards, read from /api/work. Their arms
# are tests/test_web_work_page.py: what a card says (CardWordsTest), a number
# that was not read drawn as "?" or "at least" and never 0
# (OneCountEverywhereTest), and a to-do row's drawer with its notes
# (LensesTest, DrawerTest). The queue's server totals stay pinned in
# tests/test_tasks.py and tests/test_web_tasks.py.


class AHungReadIsAnAnswerTest(unittest.TestCase):
    """Finding 3, run rather than reasoned about.

    `pollLr` used `fetch` with no deadline, so a hung `/api/lr` never rejected,
    the UNKNOWN branch never ran, and the card sat on its first paint — empty,
    with the nav badge at 0 — for as long as the socket stayed open.
    Blank-and-zero is how "nothing outstanding" looks.

    This drives the REAL `j`/`lrShow`/`pollLr` out of assembled web UI against a
    fetch that accepts the request and never answers, and asserts what the
    answer every reader of it is handed (`LR_LAST`) and the badge actually
    hold. A structural assertion (`the source
    mentions AbortController`) would pass against a timeout wired to nothing."""

    HUNG = """
const fs = require("fs");
const LR_FETCH_MS = 300;            // the real constant, shortened for the probe
let LR_LAST = null;
const renderNav = () => {};
// what every reader of the answer is handed, in one word
const said = () => (LR_LAST.pending ? "PENDING"
  : LR_LAST.rebuilding ? "REBUILDING:" + LR_LAST.rebuilding
  : "UNAVAILABLE:" + LR_LAST.unavailable);
// a HUNG endpoint with REAL abort semantics: the request is accepted, never
// answered, and rejects only if something aborts it.
let aborted = false;
global.fetch = (url, opt) => new Promise((_res, rej) => {
  if (opt && opt.signal)
    opt.signal.addEventListener("abort", () => {
      aborted = true;
      rej(new DOMException("aborted", "AbortError"));
    });
});
lrShow({pending: true});
const first = {held: said()};
// TWO POLLS, because one missed deadline is a rebuild and two running is a
// fault. The old probe polled once and pinned the first miss as UNKNOWN; the
// property it was really protecting -- a hung read must not stay PENDING --
// is now asserted at BOTH steps.
let busy = null;
pollLr();
setTimeout(() => {
  busy = {held: said()};
  pollLr();
  setTimeout(() => {
    process.stdout.write(JSON.stringify(
      {first, busy, aborted, held: said()}));
    process.exit(0);
  }, 1500);
}, 1500);
"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        cls.tmp = tempfile.mkdtemp(prefix="helm-lr-hung-")
        cls.path = os.path.join(cls.tmp, "hung.js")
        body = "\n\n".join(_extract_fn(src, n) for n in ("j", "lrShow", "pollLr"))
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(body + cls.HUNG)
        p = subprocess.run([cls.node, cls.path], capture_output=True, text=True,
                           timeout=60)
        assert p.returncode == 0, p.stderr
        cls.out = json.loads(p.stdout)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def test_the_element_is_never_empty(self):
        """The exact reproduction: first DOM blank. (The badge it also left
        at 0 is not this poll's any more: it is the Work page's stuck count,
        tests/test_web_work_page.py.)"""
        self.assertEqual(self.out["first"]["held"], "PENDING")

    def test_a_hung_read_becomes_an_UNKNOWN_instead_of_staying_pending(self):
        """Without a deadline this stays "PENDING" forever, which is the bug:
        the card shows a state it can never leave.

        IT LEAVES PENDING AT THE FIRST MISS AND REACHES UNKNOWN AT THE SECOND.
        One missed deadline is indistinguishable from a rebuild -- the
        projection costs 22-28s and the card waits 30 -- so the first miss
        says BUSY. A genuinely hung endpoint misses again, and that is what
        makes the alarm mean something. Both steps are asserted here because
        either alone would admit a defect: stopping at the first would let a
        dead endpoint read as busy forever, and stopping at the second would
        not prove the card ever leaves PENDING."""
        self.assertTrue(self.out["aborted"], "the read carried no deadline")
        # FIRST MISS: out of PENDING, into the busy reading, no alarm.
        self.assertTrue(self.out["busy"]["held"].startswith("REBUILDING:"),
                        self.out["busy"]["held"])
        self.assertIn("rebuilding this projection", self.out["busy"]["held"])
        self.assertNotIn("did not answer", self.out["busy"]["held"])
        # SECOND MISS: the alarm, with the reason verbatim.
        self.assertTrue(self.out["held"].startswith("UNAVAILABLE:"),
                        self.out["held"])
        self.assertIn("did not answer", self.out["held"])
        self.assertIn("timed out after", self.out["held"])


class TheConsoleRendersItTest(unittest.TestCase):
    """The wiring leg. A card the page never mounts, polls or badges is a
    server field nothing renders — which is this project's recurring defect,
    not a hypothetical one."""

    def setUp(self):
        self.ui = web_ui_loader.read_text()

    def test_the_deadline_is_longer_than_the_projection_it_times(self):
        """A DEADLINE SET BELOW THE COST OF ITS OWN SUBJECT CANNOT SUCCEED.

        The tree documents the cold projection at 22-28s in three separate
        places while the card aborted at 12s, so every rebuild the owner
        happened to land on reported his ledger unreadable. The deadline must
        clear the documented upper bound and still sit inside the poll, or one
        of the two failure modes is guaranteed.
        """
        import re as _re
        fetch_ms = int(_re.search(r"const LR_FETCH_MS = (\d+);", self.ui).group(1))
        poll_ms = int(_re.search(r"const LR_POLL_MS = (\d+);", self.ui).group(1))
        self.assertGreater(fetch_ms, 28000,
                           "the deadline must clear the 22-28s projection")
        self.assertLess(fetch_ms, poll_ms,
                        "and still report a hang before the next read")

    def test_a_SUSTAINED_silence_still_reaches_the_alarm(self):
        """BUSY MUST NOT BE UNCONDITIONAL, or a dead endpoint reads as a busy
        one forever. One missed deadline is a rebuild; two running is a fault,
        and the counter resets on any answer at all."""
        self.assertIn("pollLr.misses < 2", self.ui)
        self.assertIn("pollLr.misses = deadline ? (pollLr.misses || 0) + 1 : 0",
                      self.ui)
        # the reset on a successful read, so a busy card cannot latch
        self.assertIn('d = await j("/api/lr", LR_FETCH_MS);\n    pollLr.misses = 0;',
                      self.ui)

    def test_there_is_an_element_a_renderer_and_a_poll(self):
        # THE WALL IS GONE WITH ITS RENDERERS (task/3585), and the land board
        # after it (task/3643): the land requests are Work page cards off
        # /api/work, and the poll still feeds every other reader of this
        # answer — on you (the owner asks and holds) among them
        self.assertNotIn('id="lrsec"', self.ui)
        self.assertIn("onYouShow({lr: d})", _extract_fn(self.ui, "lrShow"))
        self.assertIn("setInterval(pollLr, LR_POLL_MS)", self.ui)
        self.assertIn("const LR_POLL_MS = 45000;", self.ui)

    def test_the_card_is_drawn_before_the_first_answer_not_after(self):
        """The pending paint has to happen at load, ahead of the first poll —
        otherwise the blank window is exactly the hang window."""
        self.assertIn("lrShow({pending: true});", self.ui)
        self.assertLess(self.ui.index("lrShow({pending: true});"),
                        self.ui.index("setInterval(pollLr, LR_POLL_MS)"))

    def test_the_read_carries_a_deadline_shorter_than_the_poll(self):
        """A deadline at or past the poll interval cannot report a hang before
        the next read overwrites the question."""
        self.assertIn('j("/api/lr", LR_FETCH_MS)', self.ui)
        fetch_ms = int(re.search(r"const LR_FETCH_MS = (\d+);", self.ui).group(1))
        poll_ms = int(re.search(r"const LR_POLL_MS = (\d+);", self.ui).group(1))
        self.assertLess(fetch_ms, poll_ms)

    def test_the_land_requests_are_work_page_cards_and_only_live_there(self):  # noqa: VACUOUS_ASSERTION — the Work page's one mount is asserted counted exactly once and placed by index between the project rows and Fleet, the positive control for the absences
        """The owner merged the pages, then asked why the land requests were
        still two boards (task/3585), then why the backlog and the pipeline
        were two pages (task/3643): the wall's mount and the land board are
        gone, and the Work page's one section is the only place its rows are
        drawn — after the project rows, before Fleet, none of it on History."""
        self.assertNotIn('<section id="lrsec">', self.ui)
        self.assertNotIn("lrfold", self.ui)  # noqa: VACUOUS_ASSERTION — the fold is deliberately DELETED page-wide; the page's single mount below is the positive control
        self.assertNotIn('id="pipekb"', self.ui)
        mount = '<section class="wk" id="wkmain" data-wk="main">'
        self.assertEqual(self.ui.count(mount), 1)
        i = self.ui.index(mount)
        self.assertGreater(i, self.ui.index('id="brows"'))
        self.assertLess(i, self.ui.index('id="view-quota"'))
        self.assertGreater(self.ui.index('id="view-history"'), i)

    def test_the_poll_is_not_gated_on_the_home_tab_being_open(self):
        """The badge has to reach him while he is reading chat — which is
        exactly where the unbacked prose arrives.

        BOUNDED BY THE FUNCTION, not by the distance to where it is scheduled.
        The first version sliced from `pollLr` to `setInterval(pollLr`, and
        when the boot moved to the bottom of the script — a `let` above it put
        the first paint in a temporal dead zone and killed the whole page —
        that slice swallowed two thousand unrelated lines and went red over
        another view's `document.hidden`. A test whose subject is a distance
        reports on whatever moved."""
        self.assertNotIn("view-work", _extract_fn(self.ui, "pollLr"))
        self.assertNotIn("document.hidden", _extract_fn(self.ui, "pollLr"))
        # …and the schedule is a TOP-LEVEL statement (column zero), never one
        # reached only from inside a tab-visibility branch.
        self.assertIn("\nsetInterval(pollLr, LR_POLL_MS);\n", self.ui)

    def test_the_work_page_gets_the_alarm_badge(self):
        """The badge points where the cards live — the Work page's own tab,
        rolled up onto the Work button (task/3445, task/3643): it is the
        page's stuck count, off the same /api/work body its stage row reads,
        so a badge never sends him to a page that cannot explain it."""
        self.assertIn('navBadge("flow", b.n, true, b.title, b.bound);',
                      _extract_fn(self.ui, "wkPaintAll"))
        self.assertNotIn('navBadge("pipeline"', self.ui)  # noqa: VACUOUS_ASSERTION — two badges off two counts would disagree; the assertIn above is the positive control on the same call
        self.assertIn('navBadge("chat", NAV_CHAT', self.ui)   # unchanged

    def test_the_endpoint_is_documented_in_WEB_md(self):
        doc = os.path.join(ROOT, "docs", "WEB.md")
        with open(os.path.abspath(doc), encoding="utf-8") as f:
            web_md = f.read()
        self.assertIn("/api/lr", web_md)
        self.assertIn("helm lr list", web_md)


class ServeStaleWhileRevalidatingTest(LrApiBase):
    """The home-card flap, measured on the owner's screen: `_cached` BLOCKS the
    first request after every 30s TTL lapse for the whole 22-28s projection,
    the card aborts its fetch at 12s, and the surface alternated healthy /
    "pipeline UNKNOWN" all evening. `_cached_swr` serves the previous body
    age-stamped while ONE background rebuild runs, and past a hard cap stops
    serving its past and blocks for the truth."""

    def swr_state(self, age_s, body):
        web._qstate["lr"] = (time.time() - age_s, body)

    def _cold(self):
        web._qstate.pop("lr", None)
        web._qcold.pop("lr", None)
        web._qinflight.pop("lr", None)

    def test_a_slow_cold_build_answers_warming_instead_of_blocking(self):  # noqa: VACUOUS_ASSERTION — the warming answer IS the claim; its control is the FAST-build arm below proving the same call shape returns real data, and mutating the wait away turns 2 arms RED
        """COLD START. Before this, the first read after any service restart
        BLOCKED on a whole projection — measured 21.4s against the card's 12s
        fetch deadline — so the owner's first view asserted "DISPATCH LEDGER
        UNREADABLE". The ledger was readable; it had not been COMPUTED."""
        self._cold()
        gate = threading.Event()
        try:
            t0 = time.time()
            body = web._cached_swr("lr", web._LR_FLOOR_S, web._LR_HARD_TTL_S,
                                   lambda: (gate.wait(30), {"loops": []})[1],
                                   cold_body={"warming": True}, cold_wait=0.3)
            # CONTROL, unconditional: this same call shape DOES return real
            # data when the build is fast (pinned in its own arm below), so a
            # warming answer here is about the SLOW build, not about a branch
            # that can only ever answer warming.
            self.assertEqual(body, {"warming": True})
            self.assertLess(time.time() - t0, 5,
                            "cold read blocked on the build instead of bounding")
        finally:
            gate.set()

    def test_a_FAST_cold_build_still_returns_real_data(self):
        """THE REGRESSION THIS PREVENTS, and it is not hypothetical: the first
        cut returned the placeholder IMMEDIATELY, so every first caller took a
        body it did not need — including callers with no deadline — and this
        suite went from green to 51 errors. The wait is what keeps the new
        regime invisible until it is actually needed."""
        self._cold()
        body = web._cached_swr("lr", web._LR_FLOOR_S, web._LR_HARD_TTL_S,
                               lambda: {"loops": [{"id": "fast"}]},
                               cold_body={"warming": True}, cold_wait=10)
        self.assertEqual(body.get("loops"), [{"id": "fast"}])
        self.assertNotIn("warming", body)

    def test_a_cold_build_that_RAISES_is_never_masked_by_warming(self):  # noqa: VACUOUS_ASSERTION — assertRaises IS the claim; an unconditional healthy-build control runs first on the same call shape, and mutating the fall-through turns 2 arms RED
        """A placeholder must never stand in front of a real failure. The
        rebuild sets its event in a finally, so event-set-with-no-entry is
        exactly a build that raised — the one case where "warming" would be a
        reassuring lie about a handler that is genuinely broken. It must fall
        through to the fail-LOUD path this endpoint has always had."""
        self._cold()

        def boom():
            raise RuntimeError("handler is broken")

        # CONTROL FIRST, unconditional: a HEALTHY cold build with the very
        # same arguments answers with real data. Without it, a cold path that
        # always raised would satisfy the assertion below.
        self._cold()
        self.assertEqual(
            web._cached_swr("lr", web._LR_FLOOR_S, web._LR_HARD_TTL_S,
                            lambda: {"loops": [{"id": "ok"}]},
                            cold_body={"warming": True}, cold_wait=10
                            ).get("loops"), [{"id": "ok"}])
        self._cold()
        with self.assertRaises(RuntimeError):
            web._cached_swr("lr", web._LR_FLOOR_S, web._LR_HARD_TTL_S, boom,
                            cold_body={"warming": True}, cold_wait=10)

    def test_the_cold_regime_is_opt_in_and_off_by_default(self):  # noqa: VACUOUS_ASSERTION — the with/try wrapping the control is a gate teardown, not a condition; the control runs on every pass
        """A caller that passes no cold_body keeps the old blocking contract
        exactly — this is what stops the change reaching every other cached
        endpoint in the process."""
        self._cold()
        body = web._cached_swr("lr", web._LR_FLOOR_S, web._LR_HARD_TTL_S,
                               lambda: {"loops": []})
        self.assertEqual(body.get("loops"), [])
        self.assertNotIn("warming", body)
        # CONTROL on the same observable: WITH the opt-in, the identical slow
        # build does answer warming — so the absence above is the opt-in being
        # off, not a mechanism that never fires.
        self._cold()
        gate = threading.Event()
        try:
            self.assertEqual(
                web._cached_swr("lr", web._LR_FLOOR_S, web._LR_HARD_TTL_S,
                                lambda: (gate.wait(30), {"loops": []})[1],
                                cold_body={"warming": True}, cold_wait=0.3),
                {"warming": True})
        finally:
            gate.set()

    def test_a_stale_body_is_served_without_blocking_on_the_rebuild(self):
        """STALE regime: entry older than the floor, younger than the cap.
        The request must return the PREVIOUS body immediately even though the
        rebuild (gated open here, like a real 25s projection) has not finished
        — under the old blocking path this call sits until the gate opens,
        which is exactly the mutation that turns this test RED."""
        stale = {"read_ts": time.time() - 60, "ledger_mtime": None,
                 "unavailable": None, "receipts_skipped": None,
                 "loops": [{"id": "sentinel-stale"}], "stalled_ids": [],
                 "unmeasurable": [], "closed_recent": [], "closed_total": 0,
                 "closed_unknown_when": 0}
        gate_open = threading.Event()
        fresh = dict(stale, loops=[{"id": "sentinel-fresh"}])
        with mock.patch.object(web, "_lr_build",
                               side_effect=lambda: (gate_open.wait(10), fresh)[1]):
            self.swr_state(web._LR_FLOOR_S + 5, stale)
            got = {}
            t = threading.Thread(target=lambda: got.update(
                body=web.QUERY_API["/api/lr"]({})[0]))
            t.start()
            t.join(3)
            # The EFFECT: answered fast, with the stale body, build still gated.
            self.assertFalse(t.is_alive(),
                             "request blocked on the rebuild — the flap")
            self.assertEqual(got["body"]["loops"][0]["id"], "sentinel-stale")
            # And the age header says so — staleness in the open, never hidden.
            self.assertGreaterEqual(got["body"]["read_age_s"], 59)
            # TRUTH ARRIVES <= ONE BUILD LATE: open the gate, the background
            # rebuild stores, the next read serves the fresh body.
            gate_open.set()
            for _ in range(50):
                with web._qlock:
                    ent = web._qstate.get("lr")
                if ent and ent[1]["loops"][0]["id"] == "sentinel-fresh":
                    break
                time.sleep(0.1)
            body, _ = web.QUERY_API["/api/lr"]({})
            self.assertEqual(body["loops"][0]["id"], "sentinel-fresh")

    def test_an_ancient_body_blocks_for_the_truth_never_serves_its_past(self):
        """ANCIENT regime: past the hard cap the right to serve staleness is
        gone — the chmod-000 law bounded in time. The request must come back
        with the FRESH build, not the old body."""
        old = {"read_ts": time.time() - 300, "loops": [{"id": "ancient"}],
               "unavailable": None, "ledger_mtime": None,
               "receipts_skipped": None, "stalled_ids": [], "unmeasurable": [],
               "closed_recent": [], "closed_total": 0,
               "closed_unknown_when": 0}
        fresh = dict(old, read_ts=time.time(), loops=[{"id": "truth"}])
        with mock.patch.object(web, "_lr_build", return_value=fresh):
            self.swr_state(web._LR_HARD_TTL_S + 1, old)
            body, _ = web.QUERY_API["/api/lr"]({})
        self.assertEqual(body["loops"][0]["id"], "truth")

    def test_ages_ANCIENT_under_the_old_120s_cap_serve_stale_with_their_age(self):  # noqa: VACUOUS_ASSERTION — the for iterates a LITERAL 3-tuple and the try wraps a gate teardown, not a condition; the unconditional served-tally at method scope turns RED if any iteration is skipped
        """THE 2026-08-11 OUTCOME-A RELATIONSHIP, at the ages where it binds —
        not the constant's spelling. The projection was MEASURED at 249-268s
        (cj, sequential runs, same box) against the old 120s cap, so every
        entry aged 120-250s was ANCIENT while its replacement was still
        building: a guaranteed foreground block every cycle, rendered as
        "pipeline UNKNOWN" on a healthy system. Under the 600s cap those ages
        are STALE: served immediately, age stamped in the open, one background
        rebuild in flight. 130/250/590 span the band — below the old cap's
        ghost, at the measured rebuild, and just under the new cap — so a
        regression anywhere in the band turns an age RED, not just the edge."""
        gate_open = threading.Event()
        served = []
        fresh = {"read_ts": time.time(), "ledger_mtime": None,
                 "unavailable": None, "receipts_skipped": None,
                 "loops": [{"id": "sentinel-fresh"}], "stalled_ids": [],
                 "unmeasurable": [], "closed_recent": [], "closed_total": 0,
                 "closed_unknown_when": 0}
        with mock.patch.object(web, "_lr_build",
                               side_effect=lambda: (gate_open.wait(10),
                                                    fresh)[1]):
            try:
                for age in (130, 250, 590):
                    with self.subTest(age=age):
                        stale = dict(fresh, read_ts=time.time() - age,
                                     loops=[{"id": "sentinel-stale"}])
                        self.swr_state(age, stale)
                        got = {}
                        t = threading.Thread(target=lambda: got.update(
                            body=web.QUERY_API["/api/lr"]({})[0]))
                        t.start()
                        t.join(3)
                        # The EFFECT at every age: answered fast with the
                        # stale body while the rebuild is still gated — the
                        # old cap turned exactly this call into a foreground
                        # block at 130 and 250.
                        self.assertFalse(
                            t.is_alive(),
                            "blocked at age %ds — the band regressed" % age)
                        self.assertEqual(got["body"]["loops"][0]["id"],
                                         "sentinel-stale")
                        # ...and the staleness is IN THE OPEN, which is what
                        # pays for serving it: the owner reads a true "Nm old",
                        # never a false UNKNOWN.
                        self.assertGreaterEqual(got["body"]["read_age_s"],
                                                age - 1)
                        served.append(age)
            finally:
                gate_open.set()
            # Drain the one parked background worker inside the patch so its
            # store lands before the mock departs and nothing leaks onward.
            for _ in range(50):
                with web._qlock:
                    parked = "lr" in web._qinflight
                if not parked:
                    break
                time.sleep(0.1)
        # UNCONDITIONAL, at method scope: every age in the band was actually
        # exercised. A loop that silently skipped an iteration — or a subTest
        # swallowing an early return — turns this RED instead of vacuous.
        self.assertEqual(served, [130, 250, 590])

    def test_past_600s_the_hard_band_still_blocks_it_did_not_become_infinite(self):
        """The bound the widened band must keep: 600 buys headroom over the
        measured ~250s rebuild, not a license to serve any past forever — a
        cap that quietly became infinite would re-open the chmod-000 hole this
        regime exists to bound in time. An entry aged past 600s is ANCIENT in
        ABSOLUTE seconds, deliberately not relative to the constant: raising
        the cap further is a new ruling that owes its own measurement, and
        this arm is where that debt comes due."""
        old = {"read_ts": time.time() - 601, "loops": [{"id": "ancient"}],
               "unavailable": None, "ledger_mtime": None,
               "receipts_skipped": None, "stalled_ids": [], "unmeasurable": [],
               "closed_recent": [], "closed_total": 0,
               "closed_unknown_when": 0}
        fresh = dict(old, read_ts=time.time(), loops=[{"id": "truth"}])
        with mock.patch.object(web, "_lr_build", return_value=fresh):
            self.swr_state(601, old)
            body, _ = web.QUERY_API["/api/lr"]({})
        self.assertEqual(body["loops"][0]["id"], "truth",
                         "an entry older than 600s was served instead of "
                         "blocking for the truth")

    def test_a_background_unavailable_replaces_the_stale_healthy_body(self):
        """The half the chmod-000 incident is about: an UNREADABLE ledger's
        truth must reach the surface. Under SWR it arrives at most one build
        late — the background rebuild's `unavailable` REPLACES the healthy
        stale body rather than the healthy body pinning forever."""
        healthy = {"read_ts": time.time() - 60, "loops": [], "stalled_ids": [],
                   "unavailable": None, "ledger_mtime": None,
                   "receipts_skipped": None, "unmeasurable": [],
                   "closed_recent": [], "closed_total": 0,
                   "closed_unknown_when": 0}
        broken = dict(healthy, unavailable="dispatch ledger unreadable (test)")
        with mock.patch.object(web, "_lr_build", return_value=broken):
            self.swr_state(web._LR_FLOOR_S + 5, healthy)
            web.QUERY_API["/api/lr"]({})          # kicks the background rebuild
            for _ in range(50):
                with web._qlock:
                    ent = web._qstate.get("lr")
                if ent and ent[1].get("unavailable"):
                    break
                time.sleep(0.1)
        body, _ = web.QUERY_API["/api/lr"]({})
        self.assertIn("unreadable", body["unavailable"])

    def test_a_failed_worker_spawn_never_strands_an_ancient_reader(self):
        """codex's review blocker, measured with a deterministic probe: the
        in-flight event is PUBLISHED before Thread.start(), so a spawn failure
        orphans it — no worker exists to run _swr_rebuild's finally, and every
        future ANCIENT reader blocks 300s-per-loop on the orphan, forever
        (manual event release was required in the probe). The guard must
        retract and set the EXACT published event before the failure
        propagates, so a later reader rebuilds instead of hanging."""
        stale = {"read_ts": time.time() - 60, "loops": [{"id": "old"}],
                 "unavailable": None, "ledger_mtime": None,
                 "receipts_skipped": None, "stalled_ids": [], "unmeasurable": [],
                 "closed_recent": [], "closed_total": 0,
                 "closed_unknown_when": 0}
        fresh = dict(stale, read_ts=time.time(), loops=[{"id": "rebuilt"}])
        self.swr_state(web._LR_FLOOR_S + 5, stale)
        with mock.patch.object(web.threading, "Thread",
                               side_effect=RuntimeError("no threads (test)")):
            with self.assertRaises(RuntimeError):
                web._cached_swr("lr", web._LR_FLOOR_S, web._LR_HARD_TTL_S,
                                web._lr_build)
        # The EFFECT: nothing orphaned — the event was retracted...
        self.assertNotIn("lr", web._qinflight,
                         "orphaned in-flight event after failed spawn")
        # ...and a subsequent ANCIENT reader completes promptly with a real
        # rebuild instead of waiting 300s on a ghost.
        self.swr_state(web._LR_HARD_TTL_S + 1, stale)
        got = {}
        with mock.patch.object(web, "_lr_build", return_value=fresh):
            t = threading.Thread(target=lambda: got.update(
                body=web.QUERY_API["/api/lr"]({})[0]))
            t.start()
            t.join(5)
        self.assertFalse(t.is_alive(), "ancient reader hung on the orphan")
        self.assertEqual(got["body"]["loops"][0]["id"], "rebuilt")

    def test_start_raising_after_launch_never_kills_the_successors_event(self):
        """codex's round-two blocker, deterministically modeled: start() can
        raise AFTER the OS worker launched. The spawner's catch retracts event
        A while worker A is alive; a successor stale-hit publishes event B and
        worker B; when worker A finishes, a blind pop(key) would remove and
        set B while worker B is still running (probe measured
        successor_worker_still_blocked=True under the old code). The worker
        owns its event: the finally pops the mapping only when it still holds
        that EXACT event, so B survives worker A's completion."""
        stale = {"read_ts": time.time() - 60, "loops": [{"id": "old"}],
                 "unavailable": None, "ledger_mtime": None,
                 "receipts_skipped": None, "stalled_ids": [], "unmeasurable": [],
                 "closed_recent": [], "closed_total": 0,
                 "closed_unknown_when": 0}
        a_finish = threading.Event()

        RealThread = threading.Thread   # captured BEFORE the patch: the test
        # module and web import the SAME threading module object, so patching
        # web.threading.Thread replaces it here too — the unpatched reference
        # is what keeps the wrapper from wrapping itself into RecursionError
        # (which subclasses RuntimeError and silently satisfied assertRaises,
        # so worker A never existed in any earlier version of this arm).

        class LaunchesThenRaises:
            """A Thread whose start() REALLY launches the target, then raises
            — the exact double-failure shape."""
            def __init__(self, target=None, args=(), daemon=None):
                self.t = RealThread(target=target, args=args, daemon=True)
            def start(self):
                self.t.start()
                raise RuntimeError("started then raised (test)")

        b_gate = threading.Event()

        # Role is bound by CLOSURE, not by call order: an earlier draft routed
        # a shared mock by call count and the two workers swapped roles under
        # scheduling jitter — worker B drew the a_finish gate and popped its
        # own event the moment the test released "A". Each publish gets its
        # own fn, so the interleaving is deterministic by construction.
        done_a = threading.Event()

        def build_a():
            a_finish.wait(10)
            # done_a is set IMMEDIATELY BEFORE return: the test's window of
            # interest opens the moment A's build completes, because only
            # after that does A's finally run — the meld shape; the prior
            # sleep(0.5) raced this and passed before the defect could occur.
            done_a.set()
            return dict(stale, loops=[{"id": "worker-a"}])

        def build_b():
            b_gate.wait(10)
            return dict(stale, loops=[{"id": "worker-b"}])

        self.swr_state(web._LR_FLOOR_S + 5, stale)
        with mock.patch.object(web.threading, "Thread", LaunchesThenRaises):
            with self.assertRaises(RuntimeError):
                web._cached_swr("lr", web._LR_FLOOR_S,
                                web._LR_HARD_TTL_S, build_a)
        # Worker A is ALIVE (its event already retracted). A successor
        # stale hit publishes event B + worker B (real Thread now),
        # and worker B parks on b_gate — provably still running.
        self.swr_state(web._LR_FLOOR_S + 5, stale)
        web._cached_swr("lr", web._LR_FLOOR_S, web._LR_HARD_TTL_S, build_b)
        with web._qlock:
            ev_b = web._qinflight.get("lr")
        self.assertIsNotNone(ev_b, "successor event was not published")
        # Let worker A finish while B is still gated. Event-driven, never a
        # bare sleep (the meld shape — the sleep raced A's finally and
        # passed before the defect occurred): wait for A's build to complete,
        # then POLL for A's finally to act on the mapping. Under the mutant
        # the transition IS the defect (B popped+set while B still runs);
        # under the fix the mapping never transitions and the poll drains.
        a_finish.set()
        self.assertTrue(done_a.wait(5), "worker A never finished its build")
        deadline = time.time() + 3
        while time.time() < deadline:
            with web._qlock:
                present = web._qinflight.get("lr")
            if present is not ev_b or ev_b.is_set():
                break                  # a transition happened — judge it
            time.sleep(0.02)
        with web._qlock:
            survived = web._qinflight.get("lr")
        self.assertIs(survived, ev_b,
                      "worker A's completion removed the successor's "
                      "in-flight event")
        self.assertFalse(ev_b.is_set(),
                         "worker A set the successor's event while the "
                         "successor was still running")
        # Release B; its OWN finally departs the mapping and sets it.
        b_gate.set()
        for _ in range(50):
            time.sleep(0.05)
            with web._qlock:
                still = web._qinflight.get("lr") is ev_b
            if not still:
                break
        self.assertTrue(ev_b.is_set(),
                        "B finished without setting its own event")


class RecentLandsRideTheSamePipelineBodyTest(LrApiBase, _landreq.ReceiptBase):
    """The dashboard's RECENT LANDS row — READ OFF THE SAME LEDGER `helm lr
    list` READS, riding INSIDE the /api/lr cached body.

    THREE SOURCES ARE AVAILABLE TO THIS CARD AND TWO OF THEM ARE WRONG, each for
    its own reason. `land-receipts.jsonl` has one writer, the manual verb
    `helm lr land`, so it can only show a land somebody additionally remembered
    to witness — and the verb fails open, which makes forgetting silent.
    `git log <trunk> --grep ^fold:` needs no verb and no memory, but under the
    ff-only landing discipline trunk advances by fast-forward, mints no fold
    commit, and that grammar matches no real land: it reports weeks-old rows on a
    day full of lands, and all it can add is a disclaimer saying lands are
    missing from the list.

    SO THE SOURCE IS THE LEDGER'S OWN `close --reason landed` EVENTS, and the
    ruling behind that is general rather than about this card: every card on
    Board renders from the same source the command line reads, with its read age
    shown, or it comes off. A card with a source of its own is a card free to
    disagree with `helm lr list`, and each of the two above is.

    The arms below drive the REAL close path (`landreq.close_landed`, through
    the `closed_by_landing` fixture) and then read the REAL route, so no arm
    here describes a payload the producer cannot emit."""

    def landed(self, lane="lane/foo", **kw):
        """One row closed as LANDED by the shipped close ladder -> the lr row.

        NOT A HAND-WRITTEN LEDGER EVENT. `close_landed` is what writes the
        close_reason, the closing trunk ref and sha, and the proof mode this
        card renders — so a fixture that appended its own row would be testing
        a world the ladder never produces, and would have kept passing while
        the ladder's field names moved."""
        row = self.dispatch(ref=self.side, lane=lane, **kw)
        self.legacy_undeclared(row)
        self.git("merge", "--no-edit", "-q", "side")
        lr, why = landreq.close_landed(row["id"], trunk=self.main, live=True)
        self.assertIsNone(why, why)
        self.assertEqual(lr["close_reason"], "landed")
        return lr

    # ── the cure, measured at the card ────────────────────────────────────
    def test_an_ff_only_land_appears_on_the_card(self):  # noqa: VACUOUS_ASSERTION — the absences are about a FAST-FORWARD and each has an unconditional positive beside it — assertNotEqual(before, after) and assertEqual(after, tip) prove trunk moved to the lane tip, and the card then asserts a NON-EMPTY lane list and total 1
        """THE OWNER'S COMPLAINT, AS AN ARM. Under ff-only landing no fold
        commit is minted, and the previous reader could see nothing at all. This
        land reaches the card because the LEDGER recorded it, with no fold
        commit anywhere in the story.

        The fast-forward is asserted, not assumed: if this fixture merged with a
        commit the arm would pass over the very shape the old reader COULD see,
        and prove nothing about the cure."""
        # BRANCHED OFF THE CURRENT TRUNK so a fast-forward is actually
        # possible. The fixture's `side` has diverged, and merging that would
        # mint a commit — the very shape the OLD reader could see, which would
        # make this arm pass over the wrong world.
        self.git("checkout", "-q", "-b", "ffwork", self.main)
        self.git("commit", "-q", "--allow-empty", "-m", "ff-only lane work")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        row = self.dispatch(ref=tip)
        self.legacy_undeclared(row)
        before = self.git("rev-parse", self.main)
        self.git("merge", "-q", "--ff-only", "ffwork")
        after = self.git("rev-parse", self.main)
        self.assertNotEqual(before, after)
        # A FAST-FORWARD MINTS NO COMMIT OF ITS OWN: trunk's tip IS the lane's,
        # so there is no fold commit anywhere for a trunk grammar to match.
        self.assertEqual(after, tip)
        self.assertEqual(self.git("log", "-1", "--pretty=%s", self.main),
                         "ff-only lane work")
        lr, why = landreq.close_landed(row["id"], trunk=self.main, live=True)
        self.assertIsNone(why, why)
        rl = self.lr()["recent_lands"]
        self.assertIsNone(rl["unavailable"])
        self.assertEqual([r["lane"] for r in rl["rows"]], [lr["lane"]])
        self.assertEqual(rl["total"], 1)
        self.assertEqual(rl["source"], "helm lr list")

    def test_the_card_and_helm_lr_list_agree_on_the_newest_landed_row(self):  # noqa: VACUOUS_ASSERTION — both readers are asserted NON-EMPTY before they are compared: the card names a lane and the CLI list is indexed at [0], and assertNotEqual on the two lane labels proves the fixture really planted two distinguishable lands
        """THE RULING AS ONE ASSERTION: both readers are driven over ONE planted
        ledger and must name the same newest land. Two sources cannot disagree
        if there is only one, and this is the arm that says so."""
        # ONE SECOND, STAMPED DELIBERATELY. Both closes would otherwise carry
        # the SAME whole-second stamp and the stamps alone would not order them
        # — which is the tie the producer breaks on the ledger's close append
        # index, and NOT the question this arm is asking. The first land is
        # written through the stamp clock one second back (gmtime floors, so
        # floor(t - 1) is strictly before any later stamp), which makes the two
        # lands genuinely ordered by instant, so the agreement below is about
        # the two readers rather than about a tie-break. It replaces a 1.1s
        # sleep that bought the same order.
        with mock.patch.object(pk, "now_ts",
                               lambda: pk.epoch_ts(time.time() - 1)):
            first = self.landed(lane="lane/older")
        self.git("checkout", "-q", "-b", "side2", self.main)
        self.git("commit", "-q", "--allow-empty", "-m", "second lane work")
        newer = self.dispatch(ref=self.git("rev-parse", "side2"),
                              lane="lane/newer")
        self.legacy_undeclared(newer)
        self.git("checkout", "-q", self.main)
        self.git("merge", "--no-edit", "-q", "side2")
        second, why = landreq.close_landed(newer["id"], trunk=self.main,
                                           live=True)
        self.assertIsNone(why, why)
        self.assertNotEqual(first["lane"], second["lane"])
        # the fixture's premise, asserted: ordered by instant, not by a tie
        self.assertLess(first["closed_ts"], second["closed_ts"])
        card = self.lr()["recent_lands"]
        self.assertEqual(card["rows"][0]["lane"], second["lane"])
        # THE COMMAND LINE, over the same ledger, through the same projection
        # `helm lr list` renders — landreq.loops(include_landed=True).
        rows, unavailable = landreq.loops(include_landed=True)
        self.assertIsNone(unavailable)
        # ONE COMPARATOR, NOT TWO. "Which land is newest" has a single owner —
        # `web_land_model._lr_land_order` — and the CLI's rows are ordered here by
        # the very function the card's producer sorts with. A comparator written
        # again in this file would be the arm agreeing with itself, and it is not
        # a substitute for the listing's own order: `helm lr list` groups by STAGE
        # and dwell, which answers a different question about the same rows.
        from helm import web_land_model as model
        cli = [row for _position, row in sorted(
            enumerate(r for r in rows if r.get("close_reason") == "landed"),
            key=lambda pair: model._lr_land_order(
                model._lr_epoch(pair[1].get("closed_ts")),
                pair[1].get("close_seq"), pair[0]))]
        self.assertEqual(cli[0]["lane"], second["lane"])
        self.assertEqual(len(cli), card["total"])
        self.assertEqual([r["lane"] for r in card["rows"]],
                         [r["lane"] for r in cli[:web._LR_LANDS_CAP]])

    def test_newest_first_and_every_row_carries_its_own_age(self):  # noqa: VACUOUS_ASSERTION — assertTrue(older["closed_ts"]) is the unconditional control on the stamp, and the row assertions that follow are equality against a NON-EMPTY lane plus an isinstance on a real int
        older = self.landed(lane="lane/older")
        self.assertTrue(older["closed_ts"])
        rl = self.lr()["recent_lands"]
        row = rl["rows"][0]
        # THE ROW'S OWN LANE, not the label handed to `dispatch`: the ledger
        # launders a lane on the way in, and an arm comparing against the input
        # string would be asserting the fixture rather than the projection.
        self.assertEqual(row["lane"], older["lane"])
        self.assertIsInstance(row["age_s"], int)
        self.assertGreaterEqual(row["age_s"], 0)
        self.assertEqual(row["ts"], older["closed_ts"])

    def test_two_lands_in_ONE_SECOND_are_ordered_by_the_CLOSE_THE_LEDGER_TOOK_LAST(self):  # noqa: VACUOUS_ASSERTION — the record is MEASURED first and unconditionally: two landed rows are found, their lanes differ, and both close positions are read as ints and asserted ordered, so the rendered order below is compared against a fact taken from the ledger
        """A REAL TIE, FORCED RATHER THAN WAITED FOR, AND BROKEN BY THE RECORD.

        Two closes inside the same second carry the SAME whole-second stamp, so
        the stamps cannot order them. The rows are OPENED in one order and CLOSED
        in the other here — open first, open second, close second, close first —
        because a reader that breaks the tie on row order is reading the opening
        sequence, and that put the OLDER close on top under a heading that says
        newest first.

        The ledger's own append index for each close event is what orders them
        now (`dispatches._close_position` -> `close_seq`), so the land whose close
        the ledger took LAST renders first. The tie is produced by holding the
        epoch reader still for the duration of the read, which makes every row's
        stamp equal — the stamps' own failure mode, not an invented one."""
        # OPENED FIRST, CLOSED LAST. Two lanes off trunk so both can land.
        self.git("checkout", "-q", "-b", "seqfirst", self.main)
        self.git("commit", "-q", "--allow-empty", "-m", "opened first")
        opened_first = self.dispatch(ref=self.git("rev-parse", "seqfirst"),
                                     lane="lane/opened-first")
        self.legacy_undeclared(opened_first)
        self.git("checkout", "-q", "-b", "seqsecond", self.main)
        self.git("commit", "-q", "--allow-empty", "-m", "opened second")
        opened_second = self.dispatch(ref=self.git("rev-parse", "seqsecond"),
                                      lane="lane/opened-second")
        self.legacy_undeclared(opened_second)
        self.git("checkout", "-q", self.main)
        self.git("merge", "--no-edit", "-q", "seqfirst")
        self.git("merge", "--no-edit", "-q", "seqsecond")
        # CLOSED IN THE OPPOSITE ORDER, which is the whole point of the fixture.
        #
        # THE WRITER'S OWN CLOCK, HELD STILL ACROSS BOTH CLOSES. Every accepted
        # close stamps its own event from `pk.now_ts` inside the ledger writer,
        # on a SEPARATE call per close, so a wall-second crossing between these
        # two writes gives the two rows different whole-second stamps and the tie
        # this fixture is about stops existing — a correct close-order result
        # would then fail the stamp-equality reading at the end. The stamp is
        # frozen AT ITS PRODUCER instead: one real reading, taken once and
        # returned for the duration of both writes, so the two rows carry the
        # same second BY CONSTRUCTION rather than by winning a race. Holding the
        # card's epoch reader still (below) makes the rendered ages equal; it
        # cannot make two already-written stamps equal, which is why the freeze
        # belongs here, at the write.
        frozen_epoch = time.time()
        held = [frozen_epoch]
        with mock.patch.object(pk, "now_ts",
                               side_effect=lambda: pk.epoch_ts(held[0])):
            second, why = landreq.close_landed(opened_second["id"],
                                               trunk=self.main, live=True)
            self.assertIsNone(why, why)
            first, why = landreq.close_landed(opened_first["id"],
                                              trunk=self.main, live=True)
            self.assertIsNone(why, why)
        # THE FREEZE REACHED THE WRITER, AND THE ROWS ARE THE PROOF: the value
        # the patched producer returns is the value BOTH closes carry. A patch
        # aimed at a clock the writer does not read would leave two real stamps
        # here and this control would say so.
        frozen_ts = pk.epoch_ts(frozen_epoch)
        self.assertTrue(frozen_ts)
        self.assertEqual(second["closed_ts"], frozen_ts)
        self.assertEqual(first["closed_ts"], frozen_ts)
        # THE RECORD, MEASURED BEFORE ANYTHING IS RENDERED: two landed rows on
        # distinguishable lanes, each carrying the ledger position of its own
        # close, and the row opened FIRST carries the LATER close position. Every
        # claim about the card below is against these readings.
        rows, unavailable = landreq.loops(include_landed=True)
        self.assertIsNone(unavailable)
        by_lane = {r["lane"]: r for r in rows
                   if r.get("close_reason") == "landed"}
        self.assertEqual(len(by_lane), 2, sorted(by_lane))
        self.assertNotEqual(first["lane"], second["lane"])
        closed_last = by_lane[first["lane"]]["close_seq"]
        closed_early = by_lane[second["lane"]]["close_seq"]
        self.assertIsInstance(closed_last, int)
        self.assertIsInstance(closed_early, int)
        self.assertGreater(closed_last, closed_early,
                           "the fixture did not close them in the order it "
                           "claims: %r" % (by_lane,))
        from helm import web_land_model as model
        with mock.patch.object(model, "_lr_epoch", return_value=1000.0):
            rl = self.lr()["recent_lands"]
        lanes = [r["lane"] for r in rl["rows"]]
        self.assertEqual(len(lanes), 2)
        # NEWEST CLOSE FIRST — the row whose close the ledger appended LAST, and
        # the one that was OPENED first. An order taken from the rows' opening
        # sequence renders these two the other way round.
        self.assertEqual(lanes, [first["lane"], second["lane"]])
        # AND THE TWO STAMPS REALLY ARE EQUAL, so this is a tie the instant
        # cannot break rather than an ordinary ordered pair.
        self.assertEqual(len({r["ts"] for r in rl["rows"]}), 1, rl["rows"])
        # MUST-HIT, RUN LAST SO IT CANNOT DISTURB THE TWO-ROW READINGS ABOVE:
        # the equality just asserted is the FREEZE's doing and not the clock's
        # kindness. Two more closes go through the same writer with the patched
        # producer ADVANCED a whole second BETWEEN them, and they come out with
        # DIFFERENT raw seconds. So a stamp equality here is caused by holding
        # the producer still, and this arm discriminates the mechanism instead
        # of asserting whatever the wall clock happened to do.
        self.git("checkout", "-q", "-b", "seqthird", self.main)
        self.git("commit", "-q", "--allow-empty", "-m", "opened third")
        opened_third = self.dispatch(ref=self.git("rev-parse", "seqthird"),
                                     lane="lane/opened-third")
        self.legacy_undeclared(opened_third)
        self.git("checkout", "-q", "-b", "seqfourth", self.main)
        self.git("commit", "-q", "--allow-empty", "-m", "opened fourth")
        opened_fourth = self.dispatch(ref=self.git("rev-parse", "seqfourth"),
                                      lane="lane/opened-fourth")
        self.legacy_undeclared(opened_fourth)
        self.git("checkout", "-q", self.main)
        self.git("merge", "--no-edit", "-q", "seqthird")
        self.git("merge", "--no-edit", "-q", "seqfourth")
        straddle = [time.time()]
        boundary = straddle[0] + 1.0
        with mock.patch.object(pk, "now_ts",
                               side_effect=lambda: pk.epoch_ts(straddle[0])):
            third, why = landreq.close_landed(opened_third["id"],
                                              trunk=self.main, live=True)
            self.assertIsNone(why, why)
            straddle[0] = boundary
            fourth, why = landreq.close_landed(opened_fourth["id"],
                                               trunk=self.main, live=True)
            self.assertIsNone(why, why)
        self.assertTrue(third["closed_ts"])
        self.assertTrue(fourth["closed_ts"])
        self.assertNotEqual(third["closed_ts"], fourth["closed_ts"])

    def test_the_row_carries_the_tip_the_gate_token_and_the_trunk_it_landed_on(self):  # noqa: VACUOUS_ASSERTION — assertTrue(row["reviewed_tip"]) runs first and unconditionally on the same row, so every equality below is between two non-empty values
        lr = self.landed()
        rl = self.lr()["recent_lands"]
        row = rl["rows"][0]
        # UNCONDITIONAL POSITIVE CONTROL FIRST: the row carried a real tip, so
        # the equalities below are between two NON-EMPTY values rather than
        # between two absences agreeing with each other.
        self.assertTrue(row["reviewed_tip"], row)
        self.assertEqual(row["reviewed_tip"],
                         lr.get("review_sha") or lr.get("reviewed_tip"))
        self.assertEqual(row["gate"],
                         lr.get("landing_review_gate") or lr.get("gate") or "")
        self.assertEqual(row["trunk_ref"], lr.get("closing_trunk_ref"))
        self.assertEqual(row["trunk_sha"], lr.get("closing_trunk_sha"))

    def test_the_recorded_close_proof_is_READ_not_re_derived(self):  # noqa: VACUOUS_ASSERTION — assertTrue(lr["close_proof_mode"]) runs first and unconditionally, so the equality and the tri-state claim are both about the POPULATED branch
        """The ladder proved the land at close time against the trunk it names
        in the same event. Re-asking git here would buy no new fact and would
        rebuild the second reading this card exists to delete — so `how` IS the
        recorded mode, and a row with no recorded mode answers None rather than
        False. There is deliberately no False: a `landed` close is the record
        asserting the land."""
        lr = self.landed()
        row = self.lr()["recent_lands"]["rows"][0]
        # UNCONDITIONAL POSITIVE CONTROL FIRST, on the same observable: the close
        # ladder really recorded a proof mode here, so the equality below is
        # between two NON-EMPTY values and the tri-state assertion is about the
        # populated branch rather than about two Nones agreeing.
        self.assertTrue(lr.get("close_proof_mode"), lr)
        self.assertEqual(row["how"], lr.get("close_proof_mode"))
        self.assertIs(row["on_trunk"], True if lr.get("close_proof_mode") else None)
        self.assertNotEqual(row["on_trunk"], False)

    # ── the receipt the LANDING REVIEW bound, not the build row's own ──────
    def gate_receipt(self, tip, repo_id):
        """One REAL whole-suite receipt in THIS test's gate store -> gate:<id>.

        Minted in the shipped grammar and read back through `gate.receipts()`,
        which recomputes every id and DROPS the rows that disagree — silently. A
        fixture one field out of grammar would arrive at the approve door as "no
        receipt in this store", so the read-back is the input control, not
        ceremony.
        """
        row = {"v": 4, "event": "gate", "ts": pk.now_ts(), "repo_id": repo_id,
               "head": tip, "tree": self.git("rev-parse", tip + "^{tree}"),
               "dirty": False, "head_after": tip,
               "tree_after": self.git("rev-parse", tip + "^{tree}"),
               "dirty_after": False,
               "interpreter": {"name": "cpython", "version": "3.12.3",
                               "language": "3.12.3",
                               "executable": "/usr/bin/python3"},
               "host": {"node": "fixture-node", "system": "Linux",
                        "release": "6.8.0", "id": "ab" * 8},
               "argv": ["/usr/bin/python3", "-m", "unittest", "discover",
                        "-s", "tests", "-t", "."],
               "suite": True, "label": None, "rc": 0, "wall": 12.5,
               "status": "OK", "ran": 100, "skipped": 1, "detail": "",
               "elapsed": 12.0, "failures": [], "failures_unreadable": False,
               "base_check": None}
        row["id"] = gate._receipt_id(row)
        path = gate.receipts_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        rows, unavailable, skipped = gate.receipts()
        self.assertIsNone(unavailable, "fixture gate store unreadable")
        self.assertEqual(skipped, 0, "the minted receipt failed its own "
                                     "integrity recompute — fix the fixture")
        self.assertIn(row["id"], [str(r.get("id")) for r in rows])
        return row["id"]

    def test_the_build_row_carries_the_receipt_ITS_LANDING_REVIEW_bound(self):  # noqa: VACUOUS_ASSERTION — the absence is assertFalse(lr.get("gate")), which STATES the premise of the arm, and three unconditional positive controls on the same world run before it: the approve bound the receipt (verdict["gate"] == token), the close recorded it (landing_review_gate == token), and the card row carries that same non-empty token
        """A BUILD ROW HAS NO GATE OF ITS OWN, and reading only `gate` therefore
        dropped a receipt the ledger HELD. The receipt a build-landed row was
        verified by is the one its APPROVED REVIEW CHILD bound, which the close
        ladder records as `landing_review_gate` — the token `helm lr show` names.
        The card read `gate`, which on a build row is empty, and said no token
        was recorded over a land that carries one.

        Every step here is the shipped door: a real receipt in the gate store, a
        real approve that BINDS it (the door refuses an unbound token), and the
        real `close --reason landed` ladder."""
        parent = dispatches.add("builder", "lane/build-gated", ref=self.b,
                                repo=self.repo, kind="build", notify=False,
                                new_work=True)
        child = dispatches.add("reviewer", "lane/build-gated-review",
                               ref=self.side, repo=self.repo, kind="review",
                               notify=False, supersedes=parent["id"])
        token = self.gate_receipt(self.side, child.get("repo_id"))
        verdict, err = self.mark_verdict(
            child["id"], self.side, "reviewed build output gate:" + token,
            polarity="approve")
        self.assertIsNone(err, err)
        # UNCONDITIONAL POSITIVE CONTROL ON THE WORLD: the approve really bound
        # the receipt, so the claim below is about the CARD's read and not about
        # a fixture that never carried a token.
        self.assertEqual(verdict["gate"], token)
        self.git("merge", "--no-edit", "-q", "side")
        lr, err = landreq.close(parent["id"], "landed", live=True)
        self.assertIsNone(err, err)
        self.assertEqual(lr["landing_review_gate"], token)
        self.assertFalse(lr.get("gate"), "the BUILD row carries no gate of its "
                                         "own — that is the premise of this arm")
        rows = {r["lane"]: r for r in self.lr()["recent_lands"]["rows"]}
        self.assertIn(lr["lane"], rows, rows)
        self.assertEqual(rows[lr["lane"]]["gate"], token)
        # AND THE BUILD-LANDED CLOSE RECORDS ITS LEDGER POSITION TOO. The ordering
        # arm on this class drives a review row, which takes the generic close arm;
        # a build-landed close has an arm of its own (`close_proof_version` 2), so
        # the same field is measured here or nothing measures it. Read back through
        # the projection, which is the only reader of it.
        board, unavailable = landreq.loops(include_landed=True)
        self.assertIsNone(unavailable)
        landed = [r for r in board if r["id"] == parent["id"]]
        self.assertEqual(len(landed), 1, board)      # positive control: the row
        self.assertEqual(landed[0]["close_reason"], "landed")
        self.assertIsInstance(landed[0]["close_seq"], int)

    # ── an unreadable closure instant stays unreadable ────────────────────
    def test_a_closure_the_ledger_cannot_date_does_not_borrow_the_stage_entry(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control runs FIRST on the same observable: before the corruption the same card row carries a non-empty ts and an int age_s, and the planted corruption count is asserted to be exactly one
        """THE STAGE ENTRY IS A DIFFERENT EVENT. `closed_ts` is None with
        `closed_ts_unreadable` when the ledger holds something that is not a
        timestamp where the closure stamp belongs; `entered_ts` is the instant the
        row entered its stage. Substituting one for the other printed a confident
        date and an age measured from it over a closure nothing measured.

        The corruption is planted in the REAL close event of a REAL landed row,
        and the helper asserts it planted exactly one."""
        lr = self.landed(lane="lane/corrupt")
        # UNCONDITIONAL POSITIVE CONTROL FIRST, on the same observable: before
        # the corruption this land carries a readable instant and a real age.
        good = self.lr()["recent_lands"]["rows"][0]
        self.assertTrue(good["ts"])
        self.assertIsInstance(good["age_s"], int)
        self.assertFalse(good["ts_unreadable"])
        # THE CORRUPTION IS PLANTED IN THE ROW'S OWN CLOSE EVENT and nowhere
        # else, so the land is dated everywhere except where its closure is read
        # from — and the count is asserted, because a fixture that planted
        # nothing would leave every claim below describing a healthy row.
        path = dispatches.ledger_path()
        events, hit = eventledger.events(path), 0
        with open(path, "w", encoding="utf-8") as handle:
            for event in events:
                if event.get("id") == lr["id"] and event.get("event") == "close":
                    event["ts"] = "not-a-stamp"
                    hit += 1
                handle.write(json.dumps(event, separators=(",", ":")) + "\n")
        self.assertEqual(hit, 1, "the fixture corrupted no close event")
        projected, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        row = projected[lr["id"]]
        self.assertTrue(row["closed_ts_unreadable"], row)
        self.assertIsNone(row["closed_ts"])
        self.assertTrue(row["entered_ts"], "the stage entry is still readable, "
                                           "which is what made the substitution "
                                           "possible")
        card = self.lr()["recent_lands"]["rows"][0]
        self.assertIsNone(card["ts"])
        self.assertIsNone(card["age_s"])
        self.assertTrue(card["ts_unreadable"])
        # THE ROW IS KEPT. An undateable land is a real land, and dropping it
        # would answer a question about a stamp by deleting the record.
        self.assertEqual(card["lane"], lr["lane"])

    # ── the task number, which is read and never joined ───────────────────
    def test_the_task_number_is_read_out_of_the_close_evidence(self):
        """The number the owner recognises, extracted from a field the row
        already carries. A join against `helm task`'s own ledger would be the
        second source the ruling forbids."""
        self.assertEqual(web._lr_land_task("LAND 59 task/2362: composed"), "2362")
        self.assertEqual(web._lr_land_task("cure for task 2355 landed"), "2355")
        self.assertEqual(web._lr_land_task("task2364 answers card"), "2364")

    def test_evidence_naming_no_task_answers_None_rather_than_guessing(self):
        """The control on the arm above, and the reason the card renders NO TASK
        in words: a blank there would read as a render that failed. A bare year
        and a sha fragment are not task numbers."""
        # UNCONDITIONAL POSITIVE CONTROL FIRST, on the same reader: it really
        # extracts a number when the record names one, so every None below is a
        # refusal rather than a reader that answers None to everything.
        self.assertEqual(web._lr_land_task("LAND 61 task/2355: cured"), "2355")
        for evidence in ("LAND 60 via compose-train-181", "", None,
                         "landed 2026 at deadbeef", 17):
            self.assertIsNone(web._lr_land_task(evidence), repr(evidence))

    def test_a_task_number_wider_than_the_grammar_is_not_a_valid_looking_prefix(self):  # noqa: VACUOUS_ASSERTION — the in-range extraction one line above each refusal is the unconditional positive control on the same reader, and it returns a non-empty string
        """A WIDTH BOUND WITH NO TRAILING BOUNDARY IS NOT A WIDTH BOUND. The
        pattern took three to five digits and stopped, so `task/123456` matched
        its first five and the card printed `task/12345` — a DIFFERENT task that
        exists. An identifier outside the supported width became a valid-looking
        one instead of an absence, which is the one direction this reader may
        never fail in: the owner recognises rows by that number."""
        # UNCONDITIONAL POSITIVE CONTROL, same reader, in-range: five digits are
        # supported and are read, so the refusals below are about the WIDTH.
        self.assertEqual(web._lr_land_task("LAND 62 task/12345: cured"), "12345")
        for evidence in ("LAND 62 task/123456: cured", "task 1234567 landed",
                         "task12345678"):
            self.assertIsNone(web._lr_land_task(evidence), repr(evidence))

    def test_the_extraction_reads_a_field_the_producer_actually_writes(self):
        """INPUT CONTROL ON THE FIXTURE'S OWN SHAPE, derived rather than
        transcribed: `close_evidence` and `close_proof_mode` are read off the
        lr row, so this arm asserts `landreq._lr` really projects both. Without
        it every arm above could be green over two keys the projection never
        sets, which is exactly how a card comes to render nothing forever."""
        import ast
        with open(landreq.__file__, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        projected = {node.value for node in ast.walk(tree)
                     if isinstance(node, ast.Constant)
                     and isinstance(node.value, str)}
        # UNCONDITIONAL POSITIVE CONTROL: the parse really found a corpus. An
        # empty set would make every membership claim below a report about a
        # failed read rather than about the producer.
        self.assertGreater(len(projected), 500)
        for key in ("close_evidence", "close_proof_mode", "close_reason",
                    "closing_trunk_ref", "closing_trunk_sha", "closed_ts"):
            self.assertIn(key, projected, key)
        lr = self.landed()
        for key in ("close_reason", "close_evidence", "close_proof_mode",
                    "closing_trunk_ref", "closing_trunk_sha", "closed_ts"):
            self.assertIn(key, lr, key)

    # ── the refusals ──────────────────────────────────────────────────────
    def test_a_dead_dispatch_ledger_makes_the_LANDS_reading_UNKNOWN_too(self):
        """THE PROPERTY THIS LANE DELIBERATELY GAVE UP, PINNED SO NOBODY
        RESTORES IT BY ACCIDENT. A lands leg with a source of its own is
        independent of the dispatch ledger, and that independence is exactly
        what lets it disagree with `helm lr list`. An unreadable ledger must
        therefore make the lands UNKNOWN, in the projection's OWN words, and the
        card must say so rather than show a number from somewhere else."""
        self.landed()
        with mock.patch.object(landreq, "project_raw",
                               return_value=({}, {}, "ledger unreadable")):
            body = self.lr()
        self.assertEqual(body["unavailable"], "ledger unreadable")
        rl = body["recent_lands"]
        self.assertEqual(rl["rows"], [])
        self.assertEqual(rl["unavailable"], "ledger unreadable")
        self.assertIsNone(rl["total"])

    def test_a_PERSISTED_body_from_the_PREVIOUS_reader_is_UNKNOWN_not_lands(self):
        """OBSERVED, NOT REASONED ABOUT. `_cached_swr` persists this body, so a
        body built by the trunk-walking reader is restored from disk into a
        process running the ledger one — measured on the live instance while
        this lane was being written, with the card serving 38-day-old fold rows
        under the new card's sentences and its own source line vouching for
        them. Its rows are dicts, they carry lanes, and every shape guard the
        renderer has passes them.

        The rows cannot be translated — they were derived from a reader this
        build no longer has — so the answer is UNKNOWN, naming which reading is
        missing, and the next rebuild past the floor replaces it."""
        old_shape = {"rows": [{"lane": "fold-lane", "reviewed_tip": "a" * 40,
                               "fold_sha": "f" * 40, "witnessed": False,
                               "on_trunk": None, "ts": "2026-08-05T00:00:00Z"}],
                     "window_total": None, "window_s": 86400,
                     "rows_truncated": False, "fold_only": True,
                     "unavailable": None}
        from helm import web_land_model as model
        with mock.patch.object(model, "_lr_recent_lands",
                               return_value=dict(old_shape)):
            rl = self.lr()["recent_lands"]
        self.assertEqual(rl["rows"], [],
                         "fold rows from the previous reader reached the card")
        self.assertIn("previous lands reader", str(rl["unavailable"]))
        self.assertIn("total or source", str(rl["unavailable"]))
        self.assertIsNone(rl["total"])
        # THE POSITIVE CONTROL ON THE SAME OBSERVABLE: the CURRENT producer's
        # shape is NOT refused, so the guard discriminates vintage rather than
        # refusing every body it is handed.
        self.landed()
        fresh = self.lr()["recent_lands"]
        self.assertIsNone(fresh["unavailable"])
        self.assertEqual(len(fresh["rows"]), 1)

    def test_the_fallback_envelopes_carry_the_SAME_KEYS_as_the_real_one(self):  # noqa: VACUOUS_ASSERTION — assertGreaterEqual(len(envelopes), 2) is the unconditional positive control and it runs BEFORE either loop, so a parse that found nothing reddens rather than passing
        """The new keys were added to the model's default and the fallbacks were
        argued safe because the renderer returns on `unavailable` first. That
        reasons about today's JS, not about the CONTRACT — /api/lr and any
        non-JS consumer read those envelopes directly, and an error path is
        exactly where a typed consumer must not gain undefined fields.

        Read STRUCTURALLY out of the source rather than by triggering a handler
        exception, because a fabricated 500 proves the exception path and not
        the compatibility one, and both must hold."""
        import ast
        from helm import web_land
        with open(web_land.__file__, encoding="utf-8") as f:
            src = f.read()
        envelopes = []
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Dict):
                continue
            keys = {k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if "rows_truncated" in keys and "rows" in keys:
                envelopes.append((node.lineno, keys))
        # POSITIVE CONTROL FIRST: if this finds nothing, the arm would pass by
        # vacuity and say nothing about any envelope.
        self.assertGreaterEqual(len(envelopes), 2,
                                "expected the exception and compatibility "
                                "envelopes; found %d" % len(envelopes))
        for lineno, keys in envelopes:
            for key in ("total", "source", "unavailable"):
                self.assertIn(key, keys,
                              "web_land.py:%d builds a recent_lands envelope "
                              "without %r — /api/lr would answer a shape the "
                              "real producer never sends" % (lineno, key))
        # AND THE DEAD KEYS ARE GONE FROM EVERY ENVELOPE, not only from the
        # model: a fallback still sending `fold_only` would hand the card a
        # disclaimer the owner ruled off the page.
        for lineno, keys in envelopes:
            for dead in ("fold_only", "window_total", "window_s"):
                self.assertNotIn(dead, keys, "web_land.py:%d" % lineno)

    def test_an_UNTRUNCATED_list_does_not_claim_more_exist(self):
        """One land is one row and nothing was cut. A `rows_truncated` hardwired
        True would pass the truncation arm and lie on every card the owner ever
        sees."""
        self.landed()
        rl = self.lr()["recent_lands"]
        self.assertEqual(len(rl["rows"]), 1)
        self.assertFalse(rl["rows_truncated"])
        self.assertEqual(rl["total"], 1)
        self.assertIsNone(rl["unavailable"])

    def test_a_row_closed_for_any_other_reason_is_not_a_land(self):  # noqa: VACUOUS_ASSERTION — the positive control is in the SAME RUN and on the same observable: after the emptiness claim a real landing is planted and the card is asserted to carry exactly its lane
        """The negative control on the selection. The ledger closes rows for
        eleven reasons and only ONE of them is a landing; a card that counted
        closes that were not landings would be congratulating the fleet for
        work it removed."""
        row = self.dispatch(lane="lane/report", kind="build")
        lr, why = landreq.close(
            row["id"], "delivered-report",
            artifact_ref="artifact:report.json#abc123",
            report_ref="1234abcd5678",
            evidence="artifact handed to the integrator")
        self.assertIsNone(why, why)
        self.assertEqual(lr["close_reason"], "delivered-report")
        self.assertTrue(lr["terminal"])
        rl = self.lr()["recent_lands"]
        self.assertEqual(rl["rows"], [])
        self.assertEqual(rl["total"], 0)
        self.assertIsNone(rl["unavailable"])
        # AND THE POSITIVE CONTROL IN THE SAME RUN: the selection is not simply
        # blind. One real landing on the same ledger does reach the card.
        landed = self.landed(lane="lane/landed")
        rl = self.lr()["recent_lands"]
        self.assertEqual([r["lane"] for r in rl["rows"]], [landed["lane"]])

    def test_the_cap_is_a_DISPLAY_budget_over_a_measured_total(self):  # noqa: VACUOUS_ASSERTION — every assertion here is a positive equality against a real number - CAP rows shown, CAP+2 total, truncation true - and none of them is an absence
        """A display budget may never edit a count. Unlike the trunk grammar,
        this reader knows the whole population, so the cap discloses itself
        against a real number."""
        for i in range(web._LR_LANDS_CAP + 2):
            self.git("checkout", "-q", "-b", "lane%d" % i, self.main)
            self.git("commit", "-q", "--allow-empty", "-m", "work %d" % i)
            row = self.dispatch(ref=self.git("rev-parse", "HEAD"),
                                lane="lane/n%d" % i)
            self.legacy_undeclared(row)
            self.git("checkout", "-q", self.main)
            self.git("merge", "--no-edit", "-q", "lane%d" % i)
            lr, why = landreq.close_landed(row["id"], trunk=self.main, live=True)
            self.assertIsNone(why, why)
        rl = self.lr()["recent_lands"]
        self.assertEqual(len(rl["rows"]), web._LR_LANDS_CAP)
        self.assertEqual(rl["total"], web._LR_LANDS_CAP + 2)
        self.assertTrue(rl["rows_truncated"])

    def test_the_native_chain_summary_rides_the_same_body(self):
        """The chain leg stayed independent — it reads the premise store, not
        this ledger — so it must still answer beside an UNKNOWN lands reading."""
        body = self.lr()
        self.assertIn("native_chain", body)
        self.assertIn("count", body["native_chain"])


class TheDashboardIsWiredTest(unittest.TestCase):
    """The wiring leg for the home dashboard band (owner 2026-07-28: "much
    more of an above-the-fold dashboard of the system and its components").
    Same rationale as TheConsoleRendersItTest: a renderer nothing mounts or
    feeds is a server field nothing renders."""

    def setUp(self):
        self.ui = web_ui_loader.read_text()

    def test_the_band_exists_and_holds_no_second_pipeline_copy(self):
        # THE BAND IS HOME'S TILES NOW (task/3445 L3): the pipeline is the
        # Work page's; its work tile is its glance and carries no copy of it,
        # and the band's pipeline row is the stage row atop that page
        # (task/3643)
        self.assertIn('<div class="hgrid">', self.ui)
        band = self.ui.split('<div class="hgrid">', 1)[1].split('<details class="grp" id="homenotes">', 1)[0]
        self.assertIn('id="hwork"', band)     # positive control: the slice IS the band
        self.assertNotIn('id="pipesum"', self.ui)
        self.assertNotIn('id="lrsec"', band)  # noqa: VACUOUS_ASSERTION — the wall must be ABSENT from the band; its one mount is pinned by test_the_wall_is_the_work_pages_kanban_and_only_lives_there
        self.assertNotIn("lrfold", self.ui)

    def test_every_cell_boots_saying_not_read_yet(self):
        """ZERO vs UNREAD is the band's whole law: before any poll answers,
        every cell must carry words, not an empty element a reader skims as
        a quiet zero."""
        band = self.ui.split('<div class="hgrid">', 1)[1].split('<details class="grp" id="homenotes">', 1)[0]
        self.assertGreaterEqual(band.count("not read yet"), 6)

    def test_the_presence_poll_feeds_the_fleet_row(self):
        self.assertIn("function dashFleet(", self.ui)
        self.assertIn("dashFleet(list)", _extract_fn(self.ui, "chatPresence"))

    def test_the_signing_pulse_is_the_history_pages_and_home_says_one_word(self):
        """The owner explicitly dislikes duplicated UI: ONE signing pulse.
        It lived in the band's record row, then beside the board's headline
        (task/2975), then on Home's signing-record tile. Its row text (the
        last turn, its author, its age) is the history page's own pulse, so
        the Home tile says only whether signing is live, off the same
        /api/ledger read, and the chain's check sits atop the local record."""
        self.assertNotIn('id="dreggstrip"', self.ui)
        tile = self.ui.split('<section class="htile" data-go="#history">', 1)[1].split("</section>", 1)[0]
        self.assertIn('id="hrecord"', tile)
        self.assertNotIn('id="dchain"', tile)
        hist = self.ui.split('id="view-history"', 1)[1]
        self.assertIn('id="cavepulse"', hist)
        self.assertIn('id="dchain"', hist.split('id="tierlocal"', 1)[1])


class DashBandCardsRuntimeTest(unittest.TestCase):
    """The band's cards that are left, run under node: the chain cell off the
    pipeline read, and the two cards the chat poll feeds. Its pipeline rows
    (in flight, owed by, landed) went with their renderers when the one land
    board took their numbers (task/3585); their arms went with them."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        line = [ln for ln in src.splitlines() if ln.startswith("const esc = ")]
        assert len(line) == 1, "assembled web UI's esc definition moved"
        cls.tmp = tempfile.mkdtemp(prefix="helm-dashpipe-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            # the band's DOM + sibling cells, stubbed to inert objects so the
            # REAL dashLr runs unmodified and its innerHTML is the assertion
            f.write("""const found = {};
const element = sel => {
  let html = "", hidden = null;
  return {
    get innerHTML() { return html; },
    set innerHTML(value) { html = value; },
    get hidden() { return hidden; },
    set hidden(value) { hidden = value; },
    querySelectorAll: () => []
  };
};
const $ = sel => found[sel] || (found[sel] = element(sel));
let DASH_FLEET_TS = 0, DASH_ANSWERS_TS = 0;
let DASH_CHAT_FAILED = 0, DASH_CHAT_CADENCE_S = 2;
const chatShort = s => String(s || "");
// A CONTROLLED CLOCK, so a read can be AGED without sleeping. It replaces
// Date.now for the whole run; every stamp and every age below is therefore
// measured on one clock, which is what the cards do in a browser.
let NOW = 1.6e12;
Date.now = () => NOW;
""" + line[0] + "\n\n"
                    + "\n\n".join(_extract_fn(src, n)
                                  for n in ("lrDur", "lrAgo",
                                            # EVERY CARD ON THE BAND IS THE
                                            # REAL ONE: a stub cannot hold
                                            # the band's own law, that a card
                                            # past its read's freshness bound
                                            # renders none of its last values
                                            "cardBoundS", "cardStale",
                                            # the pipeline reading's own
                                            # bound (task/3632)
                                            "lrStale",
                                            "cardSource", "dashChain",
                                            "dashChatAgeS", "dashChatExpired",
                                            "dashAnswersClock", "dashFleetClock",
                                            "dashFleet", "dashAnswers",
                                            "dashLr"))
                    + """

const input = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
// THE CHAT-FED CARDS RENDER FIRST WHEN THE INPUT SUPPLIES THEM, then the clock
// is advanced, then the pipeline read arrives — which is the real order: those
// two cards are painted by a 2s poll between two 45s pipeline reads.
if (input.presence) dashFleet(input.presence);
if (input.chat) dashAnswers(input.chat);
if (input.chat_failed) DASH_CHAT_FAILED = Date.now();
if (input.advance_s) NOW += input.advance_s * 1000;
if (typeof input.cadence_s === "number") DASH_CHAT_CADENCE_S = input.cadence_s;
if (input.clock_only) { dashAnswersClock(); dashFleetClock(); }
else dashLr(input.board);
// A CELL NOBODY TOUCHED READS AS THE EMPTY STRING, never as a crash: a run
// that renders only the chat-fed cards asks nothing of the pipeline cells, and
// their absence is a fact about that run rather than a harness failure.
const cell = sel => (found[sel] ? found[sel].innerHTML : "");
process.stdout.write(JSON.stringify({
  chain: cell("#dchain"), fleet: cell("#dfleet"),
  answers: cell("#danswers"),
  answers_hidden: found["#danswers"] ? found["#danswers"].hidden : null
}));
""")
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def run_band(self, loops, **kw):
        d = {"read_age_s": 3, "ledger_age_s": 40, "unavailable": None,
             "loops": loops, "stalled_ids": [], "unmeasurable": [],
             "closed_recent": [], "closed_total": 0}
        harness = {k: kw.pop(k) for k in
                   ("presence", "chat", "chat_failed", "advance_s",
                    "cadence_s", "clock_only") if k in kw}
        d.update(kw)
        return self.run_input(dict(harness, board=d))

    def run_input(self, payload):
        """One harness run over the WHOLE band: the chat-fed cards, the clock and
        the pipeline read, in the order a browser applies them."""
        path = os.path.join(self.tmp, "board.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        p = subprocess.run([self.node, self.path, path],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    # ── the home-card law, on the pipeline-fed card ────────────────────────
    def a_card_holding_values(self, age_s):
        """One band run over a populated read of a given age, carrying a chain
        reading the chain cell could print."""
        return self.run_band(
            [], read_age_s=age_s,
            native_chain={"count": 41, "verified": True, "head_index": 40})

    def test_every_card_on_the_band_renders_its_values_at_a_fresh_read(self):
        """THE POSITIVE CONTROL FOR THE ARM BELOW, and it is the arm that says
        the refusal is a refusal rather than a card that never worked. A read
        three seconds old renders its values."""
        out = self.a_card_holding_values(3)
        self.assertIn("41", out["chain"])
        self.assertIn("· read ", out["chain"])

    def test_no_card_on_the_band_renders_a_value_past_its_read(self):  # noqa: VACUOUS_ASSERTION — the arm above is the unconditional positive control on the same observable at age 3, and the absence below is paired with a present STALE sentence on the same cell
        """THE OWNER'S RULE, ON ALL OF THEM: "if it is there it does not go
        stale". It was implemented on two cards, so at a read past the bound the
        totals still printed a count and the chain still claimed a verified
        record count — the most actionable values on the page, standing on a
        read that had stopped arriving. A number he acts on is exactly the
        number that may not outlive its read."""
        out = self.a_card_holding_values(181)
        self.assertIn("STALE", out["chain"])
        self.assertIn("comes back with the next fresh read", out["chain"])
        # AND THE VALUE IS GONE, not dimmed
        self.assertNotIn("41", out["chain"])

    def test_the_chain_card_tells_the_owner_no_helm_verb(self):  # noqa: VACUOUS_ASSERTION — each branch's absence of a verb is paired with an unconditional positive control on the same markup (the count, FAILS, STALE, the dchain cell), and the plain wording is asserted present
        """RULE 2 ON HISTORY › THIS MACHINE ONLY › CHAIN (console walk 3, #6).
        The card read "chain ✓ 96 records verified · head #95helm premise-check
        --chain · read 0s ago · check one record: `helm premise-check <id>`":
        two commands for a reader who does not use a terminal, and the first
        glued to the head index with no space. Each branch the card draws is
        read here, with the row's static half from the page itself."""
        chain = {"count": 96, "verified": True, "head_index": 95}
        verified = self.run_band([], native_chain=chain)["chain"]
        failed = self.run_band([], native_chain=dict(
            chain, verified=False, detail="record 12 re-hashes to a new digest"))["chain"]
        stale = self.run_band([], read_age_s=181, native_chain=chain)["chain"]
        row = web_ui_loader.read_text().split('id="chainsum"', 1)[1] \
            .split("</section>", 1)[0]
        # POSITIVE CONTROLS on the same renders: each branch is the one named
        self.assertIn("96", verified)
        self.assertIn("FAILS verification", failed)
        self.assertIn("STALE", stale)
        self.assertIn('id="dchain"', row)
        for name, markup in (("verified", verified), ("failed", failed),
                             ("stale", stale), ("row", row)):
            self.assertEqual(owner_verbs(markup), [], "%s: %s" % (name, markup))
        # SAID PLAINLY, and the read's age is its own clause after the index
        text = re.sub(r"<[^>]*>", "", verified)
        self.assertIn("chain verified: 96 records", text)
        self.assertIn("head #95 · ", text)

    # ── the two cards the CHAT poll feeds expire on the CHAT clock ─────────
    PRESENCE = [{"seat": "seat-a", "presence": "fresh", "last_seen": 9}]
    ANSWER = {"owner_answers": [{"from": "seat-b", "text": "@daria ready",
                                 "ts": "2026-09-12T18:00:00Z", "age_s": 30}],
              "owner_mentions": 1}

    def test_the_chat_fed_cards_render_at_a_fresh_chat_read(self):
        """POSITIVE CONTROL, unconditional and on the same two observables the
        arms below assert about: a chat read that just answered paints the seat
        and the reply."""
        out = self.run_input({"presence": self.PRESENCE, "chat": self.ANSWER,
                              "board": {"pending": True}})
        self.assertIn("seat-a", out["fleet"])
        self.assertIn("ready", out["answers"])
        self.assertIs(out["answers_hidden"], False)

    def test_a_chat_read_that_stopped_takes_both_cards_off_on_ITS_cadence(self):  # noqa: VACUOUS_ASSERTION — the arm above is the unconditional positive control on the same two cells, and each absence here is paired with an asserted STALE sentence in the same cell
        """THE CLOCK WAS THE PIPELINE'S AND THE CARDS ARE THE CHAT POLL'S. These
        two are repainted every 2s, so four missed beats is 8 seconds; they were
        bounded at 120s against the 45s pipeline heartbeat, which let a dead chat
        endpoint keep an unread count and a seat list standing for two minutes
        under a card whose whole claim is "right now". Twenty seconds is stale
        here and was current before."""
        out = self.run_input({"presence": self.PRESENCE, "chat": self.ANSWER,
                              "advance_s": 20, "clock_only": True,
                              "board": {"pending": True}})
        self.assertIn("STALE", out["answers"])
        self.assertIn("STALE", out["fleet"])
        self.assertNotIn("ready", out["answers"])
        # THE SEAT LIST COMES OFF WITH THE WORD. A list printed after STALE is
        # the shape where the reader takes the dots for the fleet and the word
        # for decoration.
        self.assertNotIn("seat-a", out["fleet"])

    def test_the_stretched_cadence_is_the_one_the_poll_is_running_at(self):
        """While the SSE doorbell is live the poll acts every 10s, so four beats
        is 40 seconds and 20 is NOT stale. The bound follows the cadence the poll
        publishes rather than a constant, which is the whole cure: one number,
        written where the stretch is decided."""
        out = self.run_input({"presence": self.PRESENCE, "chat": self.ANSWER,
                              "advance_s": 20, "cadence_s": 10,
                              "clock_only": True, "board": {"pending": True}})
        self.assertIn("ready", out["answers"])
        self.assertNotIn("STALE", out["answers"])
        self.assertIn("seat-a", out["fleet"])

    def test_a_chat_read_that_never_answered_ONCE_says_so(self):
        """THE FIRST-READ FAILURE, which a success stamp cannot carry. With no
        prior answer there is no timestamp for a bound to expire, so a failed
        first poll left the card exactly as the boot left it — hidden — which on
        a card hidden at zero is the same picture as "nothing is waiting for
        you". The failure is stamped, so the card says NOT READ immediately and
        without needing a success first."""
        out = self.run_input({"chat_failed": True, "clock_only": True,
                              "board": {"pending": True}})
        self.assertIn("NOT READ", out["answers"])
        self.assertIs(out["answers_hidden"], False)
        self.assertIn("has not answered once", out["answers"])
        self.assertIn("NOT READ", out["fleet"])

    def test_the_home_answers_card_and_the_seats_row_tell_the_owner_no_helm_verb(self):  # noqa: VACUOUS_ASSERTION — each render's absence of a verb follows an unconditional positive control on the same markup, asserting the branch named was drawn
        """RULE 2 ON THE TWO CHAT-FED ROWS (console walk 3, open points). Home's
        answers card named `helm chat read` as its source, told him to "read
        `helm chat read` meanwhile" when the server sent no reading, and to
        "run `helm chat read`" when the read stopped; the Seats page's on-now
        row named `helm fleet` as its source and told him to run it when
        stale. Every branch each row draws is read here."""
        fresh = self.run_input({"presence": self.PRESENCE, "chat": self.ANSWER,
                                "board": {"pending": True}})
        unsent = self.run_input({"chat": {"owner_mentions": 1},
                                 "board": {"pending": True}})
        stale = self.run_input({"presence": self.PRESENCE, "chat": self.ANSWER,
                                "advance_s": 20, "clock_only": True,
                                "board": {"pending": True}})
        never = self.run_input({"chat_failed": True, "clock_only": True,
                                "board": {"pending": True}})
        cells = {"answers": fresh["answers"], "answers_unsent": unsent["answers"],
                 "answers_stale": stale["answers"],
                 "answers_never": never["answers"], "fleet": fresh["fleet"],
                 "fleet_stale": stale["fleet"], "fleet_never": never["fleet"]}
        # POSITIVE CONTROLS on the same renders: each is the branch named
        self.assertIn("ready", cells["answers"])
        self.assertIn("NOT SENT", cells["answers_unsent"])
        self.assertIn("STALE", cells["answers_stale"])
        self.assertIn("NOT READ", cells["answers_never"])
        self.assertIn("seat-a", cells["fleet"])
        self.assertIn("STALE", cells["fleet_stale"])
        self.assertIn("NOT READ", cells["fleet_never"])
        for name, markup in cells.items():
            self.assertEqual(owner_verbs(markup), [], "%s: %s" % (name, markup))

    def test_a_page_that_has_simply_not_polled_yet_is_not_a_failure(self):  # noqa: VACUOUS_ASSERTION — the arm above is the unconditional positive control on the same cell and the same code path: with the failure stamped, that cell carries NOT READ; without it, nothing is written at all
        """The boot state is not a refusal. Between the first paint and the first
        poll there is no answer and no failure, and a card claiming UNKNOWN there
        would cry wolf on every page load."""
        out = self.run_input({"clock_only": True, "board": {"pending": True}})
        self.assertEqual(out["answers"], "")
        self.assertNotIn("NOT READ", out["fleet"])


class DashAnswersCardRuntimeTest(unittest.TestCase):
    """"ANSWERS FOR YOU" — one small card, run under node (owner ruling,
    task/2364: replies to questions he asked, in plain language, with their
    read age, off the same ledger the push notification path reads).

    DRIVEN BY THE REAL FUNCTION AGAINST A REAL ELEMENT, because every claim
    worth making here is about a rendered string he reads: that it hides at
    zero, that an unreadable chat read says NOT SENT rather than "no replies",
    that a reply's own age is shown, and that a hostile author name cannot
    inject markup into the one card that renders text somebody else wrote."""

    EXTRACT = ["cardSource", "dashAnswers"]

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        line = [ln for ln in src.splitlines() if ln.startswith("const esc = ")]
        assert len(line) == 1, "assembled web UI's esc definition moved"
        cls.tmp = tempfile.mkdtemp(prefix="helm-dashanswers-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write("const lrAgo = s => String(s) + \"s\";\nlet shown = null;\n"
                    "const showView = v => { shown = v; };\n"
                    "let DASH_ANSWERS_TS = 0;\n" + line[0] + """
let html = "", hidden = null, wired = 0;
const cell = {get innerHTML() { return html; },
              set innerHTML(v) { html = v; },
              get hidden() { return hidden; },
              set hidden(v) { hidden = v; },
              querySelectorAll: q => {
                if (q !== "[data-gochat]") return [];
                const n = (String(html).match(/data-gochat/g) || []).length;
                const out = [];
                for (let i = 0; i < n; i++) out.push({set onclick(fn) { wired++; this._fn = fn; },
                                                      get onclick() { return this._fn; }});
                return out;
              }};
const $ = sel => (sel === "#danswers" ? cell : null);

"""
                    + "\n\n".join(_extract_fn(src, n) for n in cls.EXTRACT)
                    + """

dashAnswers(JSON.parse(require("fs").readFileSync(process.argv[2], "utf8")));
process.stdout.write(JSON.stringify({
  html, hidden, wired, stamped: DASH_ANSWERS_TS > 0
}));
""")
            chk = subprocess.run([cls.node, "--check", cls.path],
                                 capture_output=True, text=True)
            assert chk.returncode == 0, "node --check failed:\n" + chk.stderr

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def render(self, body):
        path = os.path.join(self.tmp, "chat.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(body, f)
        p = subprocess.run([self.node, self.path, path],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    @staticmethod
    def answer(**kw):
        """One row as `_owner_signal` emits it — every key is written by that
        function, so no arm here renders a world the server cannot send."""
        base = {"from": "seat-a", "text": "@daria the proxy is back up",
                "ts": "2026-09-12T18:00:00Z", "room": "main", "age_s": 120}
        base.update(kw)
        return base

    def test_a_reply_addressed_to_him_renders_its_author_text_and_age(self):
        out = self.render({"owner_answers": [self.answer()]})
        self.assertFalse(out["hidden"])
        self.assertIn("answers for you", out["html"])
        self.assertIn("seat-a", out["html"])
        self.assertIn("the proxy is back up", out["html"])
        self.assertIn("120s", out["html"])
        self.assertTrue(out["stamped"])

    def test_the_populated_card_NAMES_THE_READ_IT_CAME_FROM(self):
        """THE HOME-CARD LAW ON THIS CARD TOO. Every card on the band says which
        command-line read it came from and how old that read is; this one was the
        exception, so the one card whose whole claim is "these are waiting for you
        RIGHT NOW" was also the one with no provenance the owner could check.

        The age is 0 because this function runs only from an answered poll, which
        is the same reading the fleet row beside it publishes."""
        out = self.render({"owner_answers": [self.answer()],
                           "owner_mentions": 1})
        # UNCONDITIONAL POSITIVE CONTROL FIRST, on the same render: the card
        # really drew the populated branch, so the provenance claims below are
        # about a card with rows rather than about an empty string.
        self.assertIn("darow", out["html"])
        self.assertIn("the proxy is back up", out["html"])
        self.assertIn("dcsrc", out["html"])
        self.assertIn("chat · read 0s", out["html"])

    def test_the_card_is_HIDDEN_at_zero_rather_than_empty(self):
        """An empty card on this page is one more thing to look past, and chat
        states emptiness properly. The same law the decision badge follows."""
        out = self.render({"owner_answers": []})
        self.assertTrue(out["hidden"])
        self.assertEqual(out["html"], "")

    def test_a_missing_reading_says_NOT_SENT_and_never_no_replies(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control runs BEFORE the loop and on the same observable: a body carrying the reading renders the reply text and carries no NOT SENT
        """An absent key means the server predates the reading, or its chat read
        FAILED — facts about the server, which must not render as a quiet room.
        The card's whole job is to be believed when it is empty."""
        # UNCONDITIONAL POSITIVE CONTROL FIRST, same observable: a body that DOES
        # carry the reading renders the reply and says nothing about NOT SENT, so
        # the loop below is measuring the vintage branch.
        good = self.render({"owner_answers": [self.answer()]})
        self.assertIn("the proxy is back up", good["html"])
        self.assertNotIn("NOT SENT", good["html"])
        for body in ({}, {"owner_answers": None}, {"owner_answers": "two"}):
            out = self.render(body)
            self.assertFalse(out["hidden"], body)
            self.assertIn("NOT SENT", out["html"])
            self.assertIn('href="#chat"', out["html"])   # where the replies are

    def test_the_count_it_prints_is_the_count_it_was_sent(self):
        out = self.render({"owner_answers": [self.answer(text="@daria one"),
                                             self.answer(text="@daria two")],
                           "owner_mentions": 2})
        self.assertIn('<span class="dnum">2</span>', out["html"])
        self.assertIn("2 replies", out["html"])
        one = self.render({"owner_answers": [self.answer()],
                           "owner_mentions": 1})
        self.assertIn("one reply", one["html"])

    # ── the preview is four; the count is the population ──────────────────
    def test_the_headline_counts_the_POPULATION_and_says_four_are_shown(self):
        """THE PRODUCER COUNTS BEFORE IT CAPS, deliberately: `owner_mentions` is
        every addressed reply and the newest-four slice is the last step, written
        that way so this card could say four of nine honestly. Rendering the
        length of the list as the headline threw that away — nine replies
        addressed him, the card said 4 and then said "4 replies addressed to you
        since you last read the room", which is a count taken from a capped list
        describing itself."""
        out = self.render({"owner_mentions": 9, "owner_answers": [
            self.answer(text="@daria reply %d" % i) for i in range(4)]})
        self.assertIn('<span class="dnum">9</span>', out["html"])
        self.assertIn("9 replies", out["html"])
        self.assertIn("newest 4 shown", out["html"])
        self.assertIn("5 older", out["html"])
        self.assertNotIn("4 replies", out["html"])

    def test_four_of_four_claims_no_omission(self):
        """THE MUST-STAY CONTROL. The disclosure is about a REAL gap, so a card
        holding everything addressed to him may not print one — a cap that
        announces itself when nothing was cut teaches him to ignore it."""
        out = self.render({"owner_mentions": 4, "owner_answers": [
            self.answer(text="@daria reply %d" % i) for i in range(4)]})
        self.assertIn('<span class="dnum">4</span>', out["html"])
        self.assertIn("4 replies", out["html"])
        self.assertNotIn("shown", out["html"])
        self.assertNotIn("older", out["html"])

    def test_a_count_the_server_did_not_send_is_not_invented(self):  # noqa: VACUOUS_ASSERTION — the arm above is the unconditional positive control on the same observable with the count present; here the same two rows render a headline of 2, asserted positively
        """An older server sends no count, and a count SMALLER than the rows in
        hand is a body disagreeing with itself. Neither licenses a claim about a
        population, so the card counts what it holds and asserts no total."""
        for body in ({}, {"owner_mentions": 1}, {"owner_mentions": "9"}):
            payload = dict(body, owner_answers=[self.answer(text="@daria a"),
                                                self.answer(text="@daria b")])
            out = self.render(payload)
            self.assertIn('<span class="dnum">2</span>', out["html"], body)
            self.assertIn("2 replies", out["html"], body)
            self.assertNotIn("older", out["html"], body)

    def test_an_age_the_server_did_not_send_reads_as_unknown_never_as_now(self):
        """THE ROW'S AGE CELL IS THE SUBJECT, and the absence is asserted there
        rather than over the whole card: the card also names its OWN read, whose
        age legitimately IS zero (this function runs from an answered poll), so a
        card-wide search for a zero age now answers about the provenance line and
        says nothing about the row it was written for."""
        out = self.render({"owner_answers": [self.answer(age_s=None)]})
        cells = re.findall(r'<span class="daage">(.*?)</span>', out["html"])
        # UNCONDITIONAL POSITIVE CONTROL, same observable: exactly one row age
        # cell rendered, so the claim below is about a cell that exists.
        self.assertEqual(len(cells), 1, out["html"])
        self.assertEqual(cells[0], "age unknown")

    def test_a_hostile_author_or_text_cannot_inject_markup(self):
        """THE ONE CARD THAT RENDERS BYTES SOMEBODY ELSE WROTE. Chat rows come
        from every seat on the fleet, so this is the card where an escape hole
        is reachable by anyone who can post."""
        out = self.render({"owner_answers": [self.answer(
            **{"from": "<img src=x onerror=1>",
               "text": "<script>alert(1)</script>"})]})
        self.assertNotIn("<img", out["html"])
        self.assertNotIn("<script>", out["html"])
        self.assertIn("&lt;img", out["html"])
        self.assertIn("&lt;script&gt;", out["html"])

    def test_the_only_action_on_the_card_is_the_jump_to_chat(self):
        """It is not an inbox: nothing here acknowledges, dismisses or holds a
        row. The single control opens the room, which is what clears it."""
        out = self.render({"owner_answers": [self.answer()]})
        self.assertIn("data-gochat", out["html"])
        self.assertEqual(out["wired"], 1)
        for word in ("dismiss", "acknowledge", "mark read", "clipboard"):
            self.assertNotIn(word, out["html"])




class OwnerBoardSurfaceMatrixTest(LrApiBase):
    """THE SURFACE-BY-STATE MATRIX (task/2381 round 2). Each state is built
    for real — dispatch rows, lane branches, merges, cherry-picks, the kept
    landing proof written through its one door — beside a CONTROL (a live
    review whose lane holds work off trunk), read once through the real
    `/api/lr` route, and then read off every surface the owner and the fleet
    read it from:

      waits groups     `/api/board` waits: the live obligations, by holder
      waits lines      `/api/board` waits_collapsed: one counted line a class
      kanban lines     `/api/board` lanes.on_main and lanes.collapsed
      helm lr list     the CLI listing and its marks

    (The pipeline wall was a fifth surface; it went with its renderers when
    the one land board took its rows, task/3585. The page's kanban columns,
    a project's Lanes tab, were a sixth; they went with that tab when the
    Work page took it over, task/3643: the Work page places each row off
    `/api/work`, the one work reader over these same lanes, whose placement
    of a folded, on-trunk or fix-owed row is tests/test_work_model.py's.)

    One test per state, one subTest per surface cell. In every cell the
    accounting holds on both counting surfaces — listed plus collapsed is
    every row — and the two agree row for row: a row counted on a line on one
    is on the same line on the other, never listed there."""

    SURFACES = ("waits_groups", "waits_lines", "kanban_lines", "lr_list")
    ON_MAIN = {"waits_groups": None, "waits_lines": "on_main",
               "kanban_lines": "on_main", "lr_list": "ALREADY ON TRUNK"}

    def listed(role, lr_mark=None):
        return {"waits_groups": role, "waits_lines": None,
                "kanban_lines": None, "lr_list": lr_mark}

    #: state -> (lane, the expected cell on each surface). `waits_groups` is
    #: the holder ROLE of the group listing the row; `lr_list` the marks its
    #: line must carry (None: no ALREADY ON TRUNK on it).
    #: A SOURCE-CLEAN HOLD ON TRUNK IS OWED, NEVER "NO VERDICT" (task/3053,
    #: the author's ruling on the seam): the landing does not settle it, so it
    #: stays a listed wait on whoever owes its move, and the waits row carries
    #: the one sentence `landreq.source_clean_on_main` builds as `owed`.
    CLOSE_OWED = "on main · SOURCE-CLEAN close owed by the integrator"
    REHOLD_OWED = "on main · RE-HOLD owed by @"
    EXPECT = {
        "live_review": ("fresh-review", listed("reviewer")),
        "clean_hold_on_trunk": ("clean-on-trunk", dict(
            listed("integrator",
                   lr_mark=("ALREADY ON TRUNK", "SOURCE-CLEAN", CLOSE_OWED)),
            owed=CLOSE_OWED)),
        "clean_hold_no_holder_on_trunk": ("clean-no-holder", dict(
            listed("reviewer", lr_mark=("ALREADY ON TRUNK", REHOLD_OWED)),
            owed=REHOLD_OWED)),
        "build_merged": ("built-merged", ON_MAIN),
        "build_picked_proof": ("built-picked", ON_MAIN),
        "build_picked_no_proof": ("picked-unproved", listed("builder")),
        "build_unstarted": ("sent-unclaimed", listed("builder")),
        "fix_on_main": ("fix-on-main",
                        listed("integrator", lr_mark="CONTRARY")),
    }
    del listed
    #: the builds whose work nobody measured on trunk: an UNKNOWN bills
    #: nobody for a rebase (the live review's BEHIND is a PROVEN no, and true)
    UNKNOWN_WORK = ("build_picked_no_proof", "build_unstarted")

    # -- the states ------------------------------------------------------------

    def delivered(self, ref, lane, kind="review"):
        row = self.dispatch(ref=ref, lane=lane, kind=kind, deadline_s=60)
        dispatches._mark_delivered(row["id"], "post-" + lane)
        self.age(row["id"], 3600)
        return row["id"]

    def lane_commit(self, lane, base, merge=False, pick=False):
        """A lane branch minted at `base` with one authored commit, the way a
        seat does it, landed by merge or by cherry-pick when asked."""
        branch = "lane/" + lane
        self.git("branch", branch, base)
        self.git("checkout", "-q", branch)
        tip = self.commit("work " + lane, path=lane)
        self.git("checkout", "-q", self.main)
        if merge:
            self.git("merge", "--no-edit", "-q", branch)
        if pick:
            # `-x` names the source commit, as a train's pick does: without a
            # message of its own, a pick made in the same second as the commit
            # it copies IS that commit, and trunk would hold it by ancestry
            self.git("cherry-pick", "-x", tip)
        return tip

    def make_live_review(self):
        # a fresh review whose label names no branch: the census `tip` rung
        return self.delivered(self.side, "fresh-review")

    def make_clean_hold_on_trunk(self):
        rid = self.delivered(self.b, "clean-on-trunk")
        # HELD BY ITS RECIPIENT: since task/3053 the hold door takes a
        # source-clean hold only from the row's recipient, as the process's
        # corroborated identity (`dispatches._acting_author`), and this
        # fixture's own process is the integrator.
        rows, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable, unavailable)
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=(rows[rid]["recipient"], None)):
            _row, err = dispatches.mark_hold(
                rid, "SOURCE-CLEAN: read clear, the gate is the integrator's; fab Ran 5 tests OK",
                source_clean_tip=self.b)
        self.assertIsNone(err, err)
        return rid

    def make_clean_hold_no_holder_on_trunk(self):
        # A HOLD WRITTEN BEFORE THE DOOR STAMPED ITS HOLDER, appended as the
        # live ledger holds one: the door now refuses an unproven hand, so the
        # ledger is the only way this shape exists.
        rid = self.delivered(self.b, "clean-no-holder")
        rows, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable, unavailable)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "hold", "seq": int(rows[rid]["seq"]) + 1,
            "id": rid, "ts": dispatches.pk.now_ts(),
            "reason": "SOURCE-CLEAN: read clear, the gate is the integrator's",
            "owner_gated": False, "source_clean_tip": self.b}))
        return rid

    def make_build_merged(self):
        rid = self.delivered(self.c, "built-merged", kind="build")
        self.lane_commit("built-merged", self.c, merge=True)
        return rid

    def make_build_picked_proof(self):
        rid = self.delivered(self.c, "built-picked", kind="build")
        self.proofs.append(self.lane_commit("built-picked", self.c,
                                            pick=True))
        return rid

    def make_build_picked_no_proof(self):
        rid = self.delivered(self.c, "picked-unproved", kind="build")
        self.lane_commit("picked-unproved", self.c, pick=True)
        return rid

    def make_build_unstarted(self):
        # sent against the trunk sha itself; no builder has claimed a lane
        return self.delivered(self.c, "sent-unclaimed", kind="build")

    def make_fix_on_main(self):
        # its lane still holds the next round off trunk, so the census keeps
        # it on the frontier and only the on-main rule could fold it
        self.git("branch", "lane/fix-on-main", self.side)
        row = self.dispatch(ref=self.b, lane="fix-on-main", deadline_s=60)
        dispatches._mark_delivered(row["id"], "post-fix-on-main")
        _out, err = self.mark_verdict(row["id"], self.b, "reviewed",
                                      polarity="fix")
        self.assertIsNone(err, err)
        self.age(row["id"], 3600)
        return row["id"]

    def world(self, *states):
        """The control plus one row per state -> {state: (row id, lane)}. Every
        kept proof is written after the last trunk move, against that trunk,
        through the ledger's one door (MUST-HIT)."""
        self.proofs = []
        self.git("branch", "lane/control-review", self.side)
        rows = {"control": (self.delivered(self.side, "control-review"),
                            "control-review")}
        for state in states:
            rows[state] = (getattr(self, "make_" + state)(),
                           self.EXPECT[state][0])
        trunk = self.git("rev-parse", self.main)
        for tip in self.proofs:
            self.assertEqual(landreq._landing_proof(self.repo_git, tip, trunk),
                             landreq.PROOF_PATCH_EQUIVALENT)
        return rows

    # -- the surfaces ------------------------------------------------------------

    def surfaces(self, body, rows):
        """{state: {surface: cell}} for every row of `rows`, read off `body`
        exactly as each surface reads it, plus the two accountings."""
        from helm import scheduler, web_board
        self.assertIsNone(body.get("unavailable"), body.get("unavailable"))
        self.assertFalse(body.get("warming"))
        # THE JOIN KEYS ITS RECORD BY THE SCOPE'S PROJECT NAME, which this
        # fixture's registry does not resolve (`scope_why` says so); a label
        # stands in for the name and nothing else in the body is touched
        scope = body["withheld"]["scope"] or "fixture"
        joined = dict(body, withheld=dict(body["withheld"], scope=scope))
        _sec, rec = web_board._lands_join(lambda _qs: (joined, 200))
        rec = rec[scope]
        lanes = rec["lanes"]
        _rc, listing, _err = run(["list"])
        waits_lines = {c["class"]: c["count"] for c in rec["waits_collapsed"]}
        kanban_lines = {c["class"]: c["count"] for c in lanes["collapsed"]}
        on_main = lanes["on_main"] or {"count": 0, "lanes": []}
        out = {}
        for state, (rid, lane) in rows.items():
            title = scheduler.plain_title(lane)
            group = [g["label"] for g in rec["waits"]
                     if title in [r["plain_title"] for r in g["rows"]]]
            line = [ln for ln in listing.splitlines() if rid[:12] in ln]
            owed = [r.get("source_clean_on_main") for g in rec["waits"]
                    for r in g["rows"] if r.get("plain_title") == title]
            out[state] = {
                "waits_owed": owed[0] if owed else None,
                "waits_groups": group[0].split(" @")[0] if group else None,
                "kanban_lines": "on_main" if lane in on_main["lanes"] else None,
                "lr_list": line[0] if line else None}
        model = body["scheduler"]
        return out, {
            "waits_lines": waits_lines, "kanban_lines": dict(
                kanban_lines, **({"on_main": on_main["count"]}
                                 if on_main["count"] else {})),
            "model": (model["listed_count"], model["collapsed_count"],
                      model["row_count"]),
            # every live card is a card, a cut the cap counted, or on the
            # re-hold line (task/3130)
            "kanban": (len(lanes["loops"])
                       + sum(lanes["loops_more"].values())
                       + (lanes["rehold"] or {}).get("count", 0),
                       on_main["count"] + sum(kanban_lines.values()),
                       len([c for c in body["loops"] if not c["honored"]]))}

    def check(self, state):
        rows = self.world(state)
        got, whole = self.surfaces(self.lr(), rows)
        lane, want = self.EXPECT[state]
        cell, ctl = got[state], got["control"]
        for surface in self.SURFACES:
            with self.subTest(state=state, surface=surface):
                expect = want[surface]
                if surface == "waits_lines":
                    self.assertEqual(whole["waits_lines"],
                                     {expect: 1} if expect else {})
                elif surface == "kanban_lines":
                    self.assertEqual(cell["kanban_lines"], expect)
                    self.assertEqual(whole["kanban_lines"],
                                     {expect: 1} if expect else {})
                elif surface == "lr_list":
                    self.assertIsNotNone(cell["lr_list"],
                                         "the row is missing from lr list")
                    marks = expect if isinstance(expect, tuple) \
                        else (expect,) if expect else ()
                    for mark in marks:
                        self.assertIn(mark, cell["lr_list"])
                    if "ALREADY ON TRUNK" not in marks:
                        self.assertNotIn("ALREADY ON TRUNK", cell["lr_list"])
                    if state in self.UNKNOWN_WORK:
                        self.assertNotIn("BEHIND", cell["lr_list"])
                else:
                    self.assertEqual(cell[surface], expect)
        with self.subTest(state=state, surface="owed"):
            # THE ONE SENTENCE ON THE WAITS ROW, and on no other state's
            if want.get("owed"):
                self.assertIn(want["owed"], cell["waits_owed"] or "")
            else:
                self.assertIsNone(cell["waits_owed"])
        with self.subTest(state=state, surface="control"):
            self.assertEqual((ctl["waits_groups"], ctl["kanban_lines"]),
                             ("reviewer", None))
        with self.subTest(state=state, surface="accounting"):
            listed_n, collapsed_n, rows_n = whole["model"]
            self.assertEqual(listed_n + collapsed_n, rows_n)
            live_n, folded_n, filed_n = whole["kanban"]
            self.assertEqual(live_n + folded_n, filed_n)
            self.assertEqual(whole["waits_lines"], whole["kanban_lines"],
                             "the waits and the kanban folded different rows")

    def test_a_live_review_with_no_verdict_tip(self):  # noqa: VACUOUS_ASSERTION — `check` asserts every cell by exact equality or a present mark, the control row by exact tuple, and both accountings by equality
        self.check("live_review")

    def test_a_source_clean_hold_on_trunk(self):  # noqa: VACUOUS_ASSERTION — `check` asserts every cell by exact equality or a present mark, the control row by exact tuple, and both accountings by equality
        self.check("clean_hold_on_trunk")

    def test_a_NO_HOLDER_source_clean_hold_on_trunk(self):  # noqa: VACUOUS_ASSERTION — `check` asserts every cell by exact equality or a present mark, the control row by exact tuple, and both accountings by equality
        self.check("clean_hold_no_holder_on_trunk")

    def test_a_build_landed_by_merge(self):  # noqa: VACUOUS_ASSERTION — `check` asserts every cell by exact equality or a present mark, the control row by exact tuple, and both accountings by equality
        self.check("build_merged")

    def test_a_build_landed_by_cherry_pick_with_its_proof_kept(self):  # noqa: VACUOUS_ASSERTION — `check` asserts every cell by exact equality or a present mark, the control row by exact tuple, and both accountings by equality
        self.check("build_picked_proof")

    def test_a_build_landed_by_cherry_pick_with_no_proof(self):  # noqa: VACUOUS_ASSERTION — `check` asserts every cell by exact equality or a present mark, the control row by exact tuple, and both accountings by equality
        self.check("build_picked_no_proof")

    def test_a_build_sent_at_trunk_and_not_started(self):  # noqa: VACUOUS_ASSERTION — `check` asserts every cell by exact equality or a present mark, the control row by exact tuple, and both accountings by equality
        self.check("build_unstarted")

    def test_a_FIX_verdict_on_a_tip_that_is_on_main(self):  # noqa: VACUOUS_ASSERTION — `check` asserts every cell by exact equality or a present mark, the control row by exact tuple, and both accountings by equality
        self.check("fix_on_main")

    def test_a_restored_schema_3_cache(self):  # noqa: VACUOUS_ASSERTION — the control asserts the restored record's on-main cell PRESENT by exact equality before any cell is compared
        """THE BODY THE SERVER BEFORE THIS LANE SAVED — no census stamp, no
        on-main word, a BUILD's containment its base's — restored on a
        MATCHING witness. It is refused by its schema, so every surface reads
        the rebuilt projection, cell for cell. THE CONTROL: the same record
        under the current schema number IS restored, and the owner board then
        counts a build nobody has started as on main — so the equality is the
        refusal's doing."""
        from helm import web_land_model as model
        rows = self.world("build_unstarted", "clean_hold_on_trunk")
        fresh, whole = self.surfaces(self.lr(), rows)
        with mock.patch.object(web_land, "_lr_recent_lands",
                               return_value={"rows": [], "total": 0,
                                             "source": "helm lr list",
                                             "unavailable": None}), \
                mock.patch.object(web_land, "_lr_native_chain",
                                  return_value={"count": 0,
                                                "unavailable": None}), \
                model._lr_snapshot() as reads:
            body = web_land._lr_build()
            witness = reads.witness()
        self.assertIsInstance(witness, str)
        for key in ("loops", "_scheduler_rows"):
            for card in body[key]:
                for field in ("frontier", "frontier_rung",
                              "on_main_unverdicted"):
                    card.pop(field, None)
                if card.get("kind") == "build":
                    card["trunk_contains_tip"] = True    # its base, on trunk
        path = web_cache._persist_path("lr")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema": 3, "stored_ts": time.time(), "body": body,
                       "input_witness": witness}, fh)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        self.addCleanup(web_cache._qrestored.discard, "lr")

        def restored():
            web_cache._qrestored.discard("lr")
            return self.surfaces(self.lr(), rows)

        # THE CONTROL FIRST, while the witness still matches: under a
        # matching schema number the record IS restored, and serves the
        # reading this lane exists to end
        with mock.patch.object(web_cache, "_PERSIST_SCHEMA", 3):
            poisoned, _whole = restored()
        self.assertEqual(poisoned["build_unstarted"]["kanban_lines"],
                         "on_main", "THE CONTROL: the record under a matching "
                         "schema was not restored, so the equality below "
                         "would prove nothing about the schema")
        self.assertIsNone(fresh["build_unstarted"]["kanban_lines"])
        got, got_whole = restored()
        marks = ("ALREADY ON TRUNK", "SOURCE-CLEAN", "CONTRARY", "BEHIND")

        def read(surface, cell):
            # a line or a card is compared by the marks it carries: its dwell
            # text is a clock, and two reads are seconds apart
            return tuple(m for m in marks if m in cell) \
                if surface == "lr_list" and cell else cell
        for state in rows:
            for surface in self.SURFACES:
                if surface in got[state]:
                    with self.subTest(state="schema-3/" + state,
                                      surface=surface):
                        self.assertEqual(read(surface, got[state][surface]),
                                         read(surface, fresh[state][surface]))
        with self.subTest(state="schema-3", surface="lines and accounting"):
            self.assertEqual(got_whole, whole)


if __name__ == "__main__":
    unittest.main()


class ProjectionSurvivesARestartTest(LrApiBase):
    """The EMPTY regime is the one a restart keeps re-opening.

    `_cached_swr` reasons well about FRESH, STALE and ANCIENT because each has
    a previous body. EMPTY has none, so every server restart re-opens the
    defect: a cold /api/lr costs 22-28s against the card's 12s fetch deadline
    and the surface renders DISPATCH LEDGER UNREADABLE about a ledger that is
    perfectly readable and merely uncomputed. The cure gives EMPTY a past.

    THE SAFETY ARGUMENT IS THE ADMITTED TIMESTAMP, not the mechanism: a saved
    body is current only while the input it was computed from is UNCHANGED.
    Young is a fact about the clock; the question is about the INPUT."""

    def setUp(self):
        super().setUp()
        web_cache._qstate.pop("t757", None)
        web_cache._qrestored.discard("t757")
        self.addCleanup(web_cache._qstate.pop, "t757", None)
        self.addCleanup(web_cache._qrestored.discard, "t757")

    def _saved(self, read_ts, key="t757", wit="W1", body=None):
        body = {"read_ts": read_ts, "loops": []} if body is None else body
        web_cache._persist_store(key, read_ts, body, wit)
        return body

    @staticmethod
    def _snap(token):
        """The DEGENERATE snapshot: an input identified by a LITERAL.

        Identity, not a date. A timestamp comparison asks "is the input newer
        than the body", which an mtime cannot honestly answer: it is settable,
        coarse enough to repeat inside one second, and completely unmoved by a
        chmod that makes a ledger unreadable — each of which admits a body
        whose input has actually changed. The witness answers the stricter and
        simpler question, is this the SAME input, so anything that is not an
        exact match reads as a change."""
        return web_cache._ConstSnapshot(token)

    def test_the_REAL_projection_has_a_persistable_gate_epoch_witness(self):
        """The full build, not a direct reader probe, must survive restart.

        The old marker-only arm called `gate_epoch()` directly and proved its
        term moved. The production build reaches the same reader through replay
        validation with an explicit `(current, verdicts)` pair. While the epoch
        lens remained installed, the reader's own `_gate_epoch_uncached()` made
        a fresh snapshot, replay called the public door with that pair, and the
        lens routed straight back to the still-unmemoised reader. `_serve`
        caught RecursionError as blindness, so the live board looked healthy
        while `_persist_store` silently wrote nothing. This arm exercises the
        complete projection and asserts the artifact, not just the term."""
        from helm import web_land_model as model

        key = "t757_real_projection"
        path = web_cache._persist_path(key)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        # This fixture has no fold commit or premise chain, and those two
        # independent legs correctly report unavailable; unavailable bodies are
        # intentionally never persisted. Hold those legs at known healthy
        # values so this arm discriminates the land projection's witness rather
        # than the fixture's unrelated absence.
        with mock.patch.object(web_land, "_lr_recent_lands",
                               return_value={"rows": [], "unavailable": None}), \
                mock.patch.object(web_land, "_lr_native_chain",
                                  return_value={"count": 0,
                                                "unavailable": None}), \
                model._lr_snapshot() as reads:
            body = web_land._lr_build()
            witness = reads.witness()
        self.assertIsInstance(witness, str)
        self.assertEqual(reads._blind, [])
        self.assertIn("gateepoch",
                      {term.split("\x1f")[0]
                       for term in witness.split("\x1e")})
        self.assertIsNone(body.get("unavailable"))
        self.assertIn("_scheduler_rows", body)
        self.assertIn("_scheduler_active_ids", body)

        web_cache._persist_store(key, time.time(), body, witness)
        self.assertTrue(os.path.isfile(path),
                        "a healthy real projection produced no restart artifact")
        with open(path, encoding="utf-8") as stream:
            saved = json.load(stream)
        self.assertEqual(saved["schema"], web_cache._PERSIST_SCHEMA)
        self.assertEqual(saved["body"]["_scheduler_rows"],
                         body["_scheduler_rows"])

    def test_a_body_whose_INPUT_IS_UNCHANGED_is_admitted_current(self):  # noqa: VACUOUS_ASSERTION — assertIsNotNone plus assertLess on the returned stamp and an exact loops== equality are all positive; nothing here asserts an absence
        now = time.time()
        self._saved(now, wit="W1")           # computed from input W1...
        got = web_cache.persist_load("t757", 30, self._snap("W1"))
        self.assertIsNotNone(got, "a current body was refused")
        self.assertLess(time.time() - got[0], 30,
                        "a current body was aged into STALE")
        self.assertEqual(got[1]["loops"], [])

    def test_a_body_the_LEDGER_OUTRAN_is_admitted_STALE_though_young(self):
        """THE ARM THAT MATTERS. Stored seconds ago, so a clock-based gate
        would serve it as FRESH — but the ledger moved after it was computed,
        so it never saw that write. It must land in the STALE regime, which
        serves it while rebuilding AND states the age, rather than being
        trusted for being young."""
        now = time.time()
        self._saved(now - 5, wit="W1")       # body computed 5s ago from W1...
        # ...and the input is a DIFFERENT one now, which only a load-time read
        # of the input itself can see.
        got = web_cache.persist_load("t757", 30, self._snap("W2"))
        self.assertIsNotNone(got, "an outrun body should be served, not dropped")
        self.assertGreater(time.time() - got[0], 30,
                           "an outrun body was admitted as FRESH — the surface "
                           "would show a confident stale board")
        # UNCONDITIONAL POSITIVE CONTROL on the same loader: a body that DID
        # see the newest write is admitted young, so the ageing above is a
        # discrimination rather than a loader that ages everything.
        self._saved(now, key="t757b", wit="W9")
        fresh = web_cache.persist_load("t757b", 30, self._snap("W9"))
        self.assertLess(time.time() - fresh[0], 30)

    def test_a_body_saved_before_the_census_stamp_is_refused_by_its_schema(self):
        """THE SAVED BODY'S SHAPE IS AN INPUT TOO, and this lane changed it: a
        restored body's cards must carry the census (`frontier`,
        `frontier_rung`) and a BUILD row's lane-based containment, because the
        scheduler and the kanban split on those fields at serve time. The
        server that ran before this lane saved neither, under schema 3, and a
        restore admitted that record FRESH on a matching witness. MEASURED on
        a spare-port server of this tree against the live home: every card
        `frontier` None, so all 931 non-listed rows folded into "absorbed or
        settled by a later round" (267 were off the frontier and 65
        unplaceable) and a BUILD sent at trunk was counted on the on-main line
        — the readings this lane exists to end, served for as long as the
        restored body stayed FRESH. A saved body proves its shape by its
        schema number, so the number moved and the old one is refused.
        THE CONTROL: the same record under the current schema is admitted."""
        now = time.time()
        path = web_cache._persist_path("t757")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        body = {"read_ts": now, "loops": [], "_scheduler_active_ids": [],
                "_scheduler_rows": [{"id": "b", "kind": "build",
                                     "terminal": False, "honored": False,
                                     "trunk_contains_tip": True}]}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema": 3, "stored_ts": now, "body": body,
                       "input_witness": "W1"}, fh)
        self.assertIsNone(web_cache.persist_load("t757", 30, self._snap("W1")),
                          "a body saved before the census stamp was restored "
                          "and would be split on fields it does not carry")
        self._saved(now, wit="W1", body=body)
        self.assertIsNotNone(
            web_cache.persist_load("t757", 30, self._snap("W1")),
            "the same record under the current schema was refused")

    def test_a_body_saved_before_the_source_clean_exemption_is_refused_by_its_schema(self):
        """task/3053, the author's ruling on the seam: `on_main_unverdicted`
        now reads `source_clean_tip` off each card, and a HELD source-clean
        row on trunk is a listed wait carrying `source_clean_on_main`. A body
        saved under schema 5 carries neither, so on a matching witness its
        source-clean hold would fold onto "on main with no verdict recorded"
        again and the wall would call a clean read "a review that never
        happened". A saved body proves its shape by its schema number, so the
        number moved and 5 is refused. THE CONTROL: the same record under the
        current schema is admitted."""
        now = time.time()
        path = web_cache._persist_path("t757")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        body = {"read_ts": now, "loops": [], "_scheduler_active_ids": [],
                "_scheduler_rows": [{"id": "h", "kind": "review",
                                     "state": "AWAITING_REVIEW",
                                     "terminal": False, "honored": False,
                                     "trunk_contains_tip": True,
                                     "on_main_unverdicted": True}]}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema": 5, "stored_ts": now, "body": body,
                       "input_witness": "W1"}, fh)
        self.assertIsNone(web_cache.persist_load("t757", 30, self._snap("W1")),
                          "a body saved before the source-clean exemption was "
                          "restored and would fold a clean hold as no verdict")
        self._saved(now, wit="W1", body=body)
        self.assertIsNotNone(
            web_cache.persist_load("t757", 30, self._snap("W1")),
            "the same record under the current schema was refused")

    def test_an_UNDATEABLE_body_is_UNKNOWN_and_never_served(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the final persist_load on t757_ok through the SAME loader, which must return a body; the None checks are the intentional absences under test
        """A cache that cannot establish freshness must leave the regime EMPTY
        — which behaves exactly as today — rather than answer confidently.
        The owner can tell a timeout from a working board; he cannot tell a
        frozen board from a live one."""
        now = time.time()
        wit = self._snap("W1")
        for name, body, use_wit in (
                ("not-a-mapping", ["nope"], wit),
                # NO WITNESS AT ALL is a first-class refusal: a caller that
                # cannot identify its own input cannot be told its body is
                # current, so the cache declines to restore.
                ("no-witness", {"read_ts": now, "loops": []}, None),
                # A WITNESS THAT RAISES is the same answer: the input state is
                # unknown, so the body is unusable rather than assumed good.
                ("witness-raises", {"read_ts": now, "loops": []},
                 lambda: (_ for _ in ()).throw(OSError("no ledger"))),
                ("witness-nonsense", {"read_ts": now, "loops": []},
                 lambda: 12345),
                ("witness-empty", {"read_ts": now, "loops": []}, lambda: "")):
            with self.subTest(case=name):
                web_cache._persist_store("t757_" + name, now, body, "W1")
                self.assertIsNone(
                    web_cache.persist_load("t757_" + name, 30, use_wit),
                    "%s was served despite being unidentifiable" % name)
        # UNCONDITIONAL POSITIVE CONTROL: the same loader DOES serve a body it
        # can date on both sides, so the refusals above discriminate rather
        # than describing a loader that refuses everything.
        self._saved(now, key="t757_ok", wit="W1")
        self.assertIsNotNone(web_cache.persist_load("t757_ok", 30, wit))

    def test_restoration_is_attempted_ONCE_not_on_every_empty_poll(self):
        """`_qrestored` marks the ATTEMPT. A key with nothing saved must not
        re-read the disk on every EMPTY poll, and a body refused for staleness
        must not be re-offered a second later."""
        calls = []
        real = web_cache.persist_load

        def counting(key, ttl, snap=None):
            # THE SPY FOLLOWS THE SIGNATURE IT WRAPS. Mine did not
            # after the witness was threaded through, and the arm
            # failed with a TypeError rather than a wrong answer —
            # which is the good direction, but a spy that drifts from
            # its subject can also silently swallow an argument.
            calls.append(key)
            return real(key, ttl, snap)

        build = lambda: {"loops": [], "probe": "t757"}      # noqa: E731
        with mock.patch.object(web_cache, "persist_load", counting):
            for _ in range(3):
                web_cache._cached_swr("t757", 30, 120, build)
            # UNCONDITIONAL POSITIVE ON THE PRODUCTION CALL ITSELF, not on an
            # accumulator. Collecting the returns into a list and asserting
            # on THAT proves only that the test's own bookkeeping ran, which
            # says nothing about what the cache answered — the vacuity rung
            # reads such a list as another spy, correctly. A fourth real call,
            # asserted directly, is the observation.
            self.assertEqual(
                web_cache._cached_swr("t757", 30, 120, build).get("probe"),
                "t757", "the cache did not serve a real body")
        self.assertEqual(calls.count("t757"), 1,
                         "the disk was read on every empty poll: %r" % calls)

    def test_the_save_runs_AFTER_the_waiters_are_released(self):
        """Taking the write off the global lock was not enough: it still sat
        on the COMPLETION path, so a blocked ANCIENT reader waited for the disk
        even though its answer already existed in _qstate — measured as a
        0.455s wait behind an injected 0.4s save. Write-behind means BEHIND,
        including behind the waiters."""
        key = "t757_behind"
        web_cache._qstate.pop(key, None)
        web_cache._qrestored.discard(key)
        self.addCleanup(web_cache._qstate.pop, key, None)
        self.addCleanup(web_cache._qrestored.discard, key)
        seen = {}
        real = web_cache._persist_store

        def watching(k, ts, body, witness):
            # STRUCTURAL, not timing: by the time the writer runs the in-flight
            # entry must already be gone, which is what releases the waiters.
            seen["inflight"] = k in web_cache._qinflight
            seen["published"] = k in web_cache._qstate
            return real(k, ts, body, witness)

        ev = threading.Event()
        with mock.patch.object(web_cache, "_persist_store", watching):
            web_cache._swr_rebuild(
                key, lambda: {"read_ts": time.time(), "loops": []}, ev,
                web_cache._ConstSnapshot("W1"))
        self.assertIn("inflight", seen, "the writer never ran")
        self.assertTrue(ev.is_set(), "the waiter event was never set")
        self.assertTrue(seen["published"],
                        "the body was not published before the save")
        self.assertFalse(seen["inflight"],
                         "the save ran while waiters were still blocked on it")

    def test_a_body_whose_LEG_failed_is_not_saved(self):
        """The projection's legs name their OWN unavailable — recent_lands
        reads trunk, native_chain reads the premise store — precisely so one
        going dark does not blank the board. A top-level-only check saved a
        body with a transient leg failure and restored it as current after
        that leg recovered."""
        now = time.time()
        for leg in ("recent_lands", "native_chain"):
            with self.subTest(leg=leg):
                k = "t757_leg_" + leg
                web_cache._persist_store(
                    k, now, {"read_ts": now, "loops": [],
                             leg: {"unavailable": "transient"}}, "W1")
                self.assertFalse(os.path.exists(web_cache._persist_path(k)),
                                 "%s failure was saved" % leg)
        # UNCONDITIONAL POSITIVE CONTROL: the same shape with HEALTHY legs is
        # saved through the same writer, so the refusals discriminate.
        ok = "t757_leg_ok"
        web_cache._persist_store(ok, now, {"read_ts": now, "loops": [],
                                           "recent_lands": {"rows": []},
                                           "native_chain": {"count": 1}}, "W1")
        self.assertTrue(os.path.exists(web_cache._persist_path(ok)),
                        "the writer refuses healthy bodies too")

    def test_a_SLOW_witness_read_cannot_make_an_aged_body_look_FRESH(self):
        """A CLOCK TOCTOU ACROSS THE RESTORE. `_cached_swr` samples `now`
        before the restore; `persist_load` does real I/O (stat of every ledger,
        the trunk, the premise store) and ages a mismatched body against the
        clock AFTERWARDS. Classifying with the earlier instant subtracts a
        later timestamp from an earlier one and lands short of ttl — measured
        with a 2s witness delay, a body aged to 31s classified at ~29s, served
        FRESH, zero rebuilds. The witness said stale and the arithmetic un-said
        it."""
        key = "t757_toctou"
        web_cache._qstate.pop(key, None)
        web_cache._qrestored.discard(key)
        self.addCleanup(web_cache._qstate.pop, key, None)
        self.addCleanup(web_cache._qrestored.discard, key)
        self._saved(time.time(), key=key, wit="W1")
        builds = []

        class Slow:
            """A read-set whose COLLECTION costs real time, which the live one's
            does: a stat per ledger, a trunk resolution per repository, a tier
            per reviewer."""

            def __call__(self):
                return contextlib.nullcontext(self)

            def recheck(self, _saved):
                time.sleep(2.0)      # the collection I/O the real one performs
                return "W2"          # ...and the input MOVED

            def witness(self):
                return "W2"

        kicked = threading.Event()

        def build():
            builds.append(1)
            kicked.set()
            return {"read_ts": time.time(), "loops": [], "probe": key}

        web_cache._cached_swr(key, 30, 120, build, snapshot=Slow())
        # THE REBUILD IS A BACKGROUND THREAD — at 31s against ttl=30 the body
        # lands in STALE, which kicks ONE rebuild and serves the previous body
        # immediately. Asserting on `builds` without waiting measures the
        # scheduler, not the classification.
        self.assertTrue(kicked.wait(20),
                        "an outrun body was classified FRESH across a slow "
                        "witness read, so no rebuild was kicked")
        self.assertEqual(len(builds), 1, "more than one rebuild was kicked")

    # ── input-identity completeness ───────────────────────────────────────

    def test_the_witness_covers_TRUNK_and_the_PREMISE_STORE_not_just_ledgers(self):
        """The body proves every row's landedness against TRUNK and embeds
        `native_chain`, read from the premise store. A ledger-only fingerprint
        would stay byte-identical while trunk moves and the chain grows,
        restoring the body as FRESH over inputs that changed.

        IT DRIVES `_lr_project`, THE BODY, RATHER THAN THE LANDS LEG, and that
        is a repoint rather than a weakening (task/2355). A trunk-walking lands
        leg resolves trunk on its way to `git log <trunk> --grep ^fold:`, so
        calling it is enough to put trunk in the record; this one reads the
        LEDGER and touches git not at all. Trunk is pinned once at the top of
        `_lr_project`, which is where the rows are then proved against it — so
        the claim is about the BODY's read-set, which is what the witness
        describes. Driving the leg here would leave the arm green over a record
        that does not cover trunk at all.

        MEASURED ON WHAT THE BUILD ITSELF READ, not on a parallel list of terms
        someone remembered to add. The ops in the record are the ops it
        consumed.

        THE PREMISE STORE IS NOW A CONTENT TERM, NOT A `stat` LABEL, and that is
        the TOCTOU cure showing through: the chain used to be covered by a stat
        taken beside the read, which is a second resolution of the name and says
        nothing about what the file contained. `premisechain` carries the path,
        the inode and the digest of ONE open, so this asserts the stronger of
        the two properties the old spelling could express."""
        from helm import web_land, web_land_model as m
        with m._lr_snapshot() as reads:
            web_land._lr_project(time.time(), None)
            m._lr_newest_mtime()
            witness = reads.witness()
        # UNCONDITIONAL POSITIVE CONTROL, FIRST: the legs really collected
        # something, so the membership claims below are about a real record.
        self.assertIsInstance(witness, str)
        terms = witness.split("\x1e")
        ops = {t.split("\x1f")[0] for t in terms}
        labels = {json.loads(t.split("\x1f")[1])[0]
                  for t in terms if t.startswith("stat\x1f")}
        self.assertIn("refsha", ops, "trunk is not resolved through the record")
        self.assertIn("repo", ops, "the repository identity is not recorded")
        self.assertIn("premisechain", ops,
                      "the premise store is not fingerprinted: %r" % (ops,))
        self.assertIn("ledger", labels, "the ledgers stopped being fingerprinted")

    def test_a_MARKER_ONLY_epoch_change_MOVES_the_witness(self):  # noqa: VACUOUS_ASSERTION — the analyzer counts Eq as positive only against a NON-EMPTY LITERAL, and this arm's pre-state `first_value` is legally a UNION (an int, "EPOCH-LOST", or None) that no single literal comparison can pin; the unconditional positive controls come FIRST and on the observable this arm is actually about — `assertIsInstance(first, str)`, `assertIn("gateepoch", ops-of-first)` proving the term is really in the record, and an equality against a SECOND unmutated read proving the witness is stable — so a difference after the marker write cannot be an artefact of an empty or nondeterministic witness
        """The GATE EPOCH moves with no ledger, trunk or premise write.

        `landreq.project_raw` consumes `dispatches.gate_epoch` to DEMOTE an
        approval that predates the freeze, and the epoch answers from a MARKER
        FILE that no `stat` in this record describes. So before this term
        existed, writing or corrupting that marker alone left the witness
        BYTE-IDENTICAL and restored a stale READY/REVIEWED body as FRESH —
        measured at CL96 and reproduced here as an executable arm
        rather than kept as a report.

        THE MUTATION IS MARKER-ONLY BY CONSTRUCTION. Nothing else is touched:
        no dispatch is written, no ref moves, no premise appended. If the
        witness still moves, it moved because the epoch term is in it.

        IT ASSERTS THE VALUE MOVED BEFORE ASSERTING THE WITNESS MOVED, because
        a witness that changed while the input did not would be a different
        (and worse) defect, and an arm that only compared witnesses could not
        tell the two apart."""
        import json as _json
        from helm import web_land_model as m
        from helm import dispatches as d

        with m._lr_snapshot() as reads:
            first_value = d.gate_epoch()
            first = reads.witness()
        # UNCONDITIONAL POSITIVE CONTROL, FIRST and on the same observable:
        # this reader demonstrably produces a witness, and that witness
        # demonstrably carries the epoch term. Everything below is then a claim
        # about a real record rather than about an empty one.
        self.assertIsInstance(first, str)
        self.assertIn("gateepoch",
                      {t.split("\x1f")[0] for t in first.split("\x1e")},
                      "the epoch is not fingerprinted: %r" % (first,))
        # AND A POSITIVE CONTROL ON THE PRE-STATE ITSELF. `first_value` is
        # otherwise only ever read inside a NOT-equal, so nothing would catch
        # it answering something outside `gate_epoch`'s documented contract —
        # an int, EPOCH_LOST, or None. A pre-state nobody asserts is a
        # comparison against an unknown.
        self.assertTrue(
            first_value is None or first_value == d.EPOCH_LOST
            or isinstance(first_value, int),
            "gate_epoch answered outside its contract: %r" % (first_value,))

        # THE SECOND HALF OF THE CONTROL, and the vacuity rung was right to
        # demand it: this arm's claim is that the witness MOVES, and a witness
        # that moved on every call would satisfy that for free. So prove
        # STABILITY first — two snapshots with NOTHING mutated between them
        # must produce the IDENTICAL witness. Only then does a difference
        # below carry information.
        with m._lr_snapshot() as reads:
            d.gate_epoch()
            unchanged = reads.witness()
        self.assertEqual(first, unchanged,
                         "the witness is not stable across two reads of an "
                         "UNCHANGED world, so a later difference proves "
                         "nothing: %r vs %r" % (first, unchanged))

        path = d.epoch_path()
        try:
            with open(path, encoding="utf-8") as fh:
                prior = fh.read()
        except OSError:
            prior = None

        def _restore():
            if prior is None:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            else:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(prior)
        self.addCleanup(_restore)

        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ not valid json")     # present and untrustworthy

        with m._lr_snapshot() as reads:
            second_value = d.gate_epoch()
            second = reads.witness()

        # THE INPUT REALLY MOVED. Without this the arm could pass on a witness
        # that changed for an unrelated reason.
        self.assertEqual(second_value, d.EPOCH_LOST)
        self.assertNotEqual(
            first_value, second_value,
            "the marker mutation did not change the epoch: %r" % (first_value,))
        self.assertNotEqual(
            first, second,
            "A MARKER-ONLY MUTATION LEFT THE WITNESS IDENTICAL, so a stale "
            "body restores as FRESH across a change to the input that decides "
            "whether an unstamped approve counts: %r" % (first,))
        self.assertIn('"EPOCH-LOST"', second,
                      "the moved value is not the thing that moved the "
                      "witness: %r" % (second,))
        _json.loads('"EPOCH-LOST"')          # the identity is canonical JSON

    def test_a_REGISTRY_ONLY_remap_MOVES_the_witness(self):  # noqa: VACUOUS_ASSERTION — the analyzer counts Eq as positive only against a NON-EMPTY LITERAL and this arm compares two computed witnesses, so its absence-shaped assertions cannot be paired structurally; the unconditional positive controls come FIRST and on the same observable — `assertIn("boardscope", ops-of-first)` proving the term is really in the record, and an equality against a SECOND unmutated read proving the witness is stable — so a difference after the remap cannot be an artefact of an empty or nondeterministic witness
        """THE BOARD'S SCOPE moves with no ledger, trunk or marker write.

        `landreq.board_scope` has two layers. The repo layer is already covered
        — `home_repo_id` is served through `_read_repo`. The PROJECT layer is
        not: `_project_of` resolves through the REGISTRY, so a registry-only
        remap changes which rows read local, foreign and unresolved, and with
        them the withheld counts and the observation authority the card states,
        while every other recorded term stays byte-identical.

        measured at CL96. A forbid-list entry in the seam scan
        would REPORT the outside read and leave it outside; serving it is the
        cure the scan was asking for.

        THE MUTATION IS REGISTRY-ONLY BY CONSTRUCTION: the resolver
        `_project_of` imports is patched and nothing else is touched — no
        dispatch written, no ref moved, no marker altered."""
        from helm import web_land_model as m
        from helm import landreq as lr

        def snap():
            with m._lr_snapshot() as reads:
                value = lr.board_scope()
                return value, reads.witness()

        first_scope, first = snap()
        # UNCONDITIONAL POSITIVE CONTROLS, FIRST and on the same observable.
        self.assertIsInstance(first, str)
        self.assertIn("boardscope",
                      {t.split("\x1f")[0] for t in first.split("\x1e")},
                      "the board scope is not fingerprinted: %r" % (first,))
        # STABILITY: a witness that moved on every call would make the
        # difference below meaningless.
        _, unchanged = snap()
        self.assertEqual(first, unchanged,
                         "the witness is not stable across an UNCHANGED "
                         "world: %r vs %r" % (first, unchanged))

        with mock.patch("helm.inject._ledger.project_for_cwd",
                        return_value="synthetic-proj"):
            second_scope, second = snap()

        # THE INPUT REALLY MOVED, asserted before the witness claim.
        self.assertEqual(second_scope.get("project"), "synthetic-proj")
        self.assertNotEqual(
            first_scope.get("project"), second_scope.get("project"),
            "the remap did not change the scope: %r" % (first_scope,))
        self.assertNotEqual(
            first, second,
            "A REGISTRY-ONLY REMAP LEFT THE WITNESS IDENTICAL, so a board "
            "restores as FRESH across a change to the input that decides which "
            "rows are local, foreign and withheld: %r" % (first,))
        self.assertIn("synthetic-proj", second,
                      "the moved value is not what moved the witness: %r"
                      % (second,))

    def test_an_ORDINARY_observable_board_can_PERSIST_and_a_VANISHED_object_moves_it(self):
        """OBJECT EXISTENCE IS A LIVE READ OF A MOVING REPOSITORY.

        `_objexist_map` writes `("objexist", gitdir) -> {sha: (present, kind)}`
        into the projection cache, and the cache IS this record. Unclassified,
        that write made the record BLIND — so a board with ANY observable row
        could never persist, which is this lane's entire goal. Classified as a
        DERIVED key it would persist and be WRONG, because an object can appear
        by fetch or vanish by gc with no ledger write, no trunk move and no
        marker change.

        SO IT IS A TERM. Ruled on task/1137: `objexist-pending` is genuinely
        derived (built from row dicts, reads nothing external) and earns the
        exemption; the LIVE key does not and is recorded.

        THE READ DID NOT MOVE, and that was binding. Trunk's law is
        row-observation-lazy, never repository-lazy — "that actually needs an
        answer buys the batch through `_objexist_map`" — so the cure is a
        CLASSIFIER change and touches `landreq` nowhere. This arm writes the two
        keys through the SAME DOOR `landreq` writes them through, rather than
        calling a helper that would be more forgiving than the real path."""
        from helm import web_land_model as m
        gd = "/repo/.git"

        def build(objmap):
            reads = m._LrReadSet()
            reads[gd] = ("refs/heads/main", "refs/remotes/origin/main")
            reads[("objexist-pending", gd)] = ["abc123"]
            reads[("objexist", gd)] = objmap
            return reads

        present = build({"abc123": (True, "commit")})
        again = build({"abc123": (True, "commit")})
        vanished = build({"abc123": (False, None)})

        # UNCONDITIONAL POSITIVE CONTROL, FIRST: the record certifies at all.
        witness = present.witness()
        self.assertIsInstance(witness, str)
        self.assertIn("objexist", witness,
                      "object existence is not fingerprinted: %r" % (witness,))
        # THE LANE'S GOAL: no blind reason, so this board can be saved.
        self.assertEqual(
            present._blind, [],
            "an ORDINARY board with an observable row still cannot persist: %r"
            % (present._blind,))
        # STABILITY before movement, or a difference proves nothing.
        self.assertEqual(witness, again.witness(),
                         "the witness is not stable across identical worlds")
        # AND THE MOVEMENT: an object that vanished is a different world.
        self.assertNotEqual(
            witness, vanished.witness(),
            "A VANISHED OBJECT LEFT THE WITNESS IDENTICAL, so a board judged "
            "on 'this tip exists' restores as FRESH over a repository that no "
            "longer says so: %r" % (witness,))

    def test_object_existence_REPLAY_asks_the_exact_saved_batch(self):  # noqa: VACUOUS_ASSERTION — assertIsInstance(saved, str) and exact operand equality prove the term exists before unchanged and moved replay assertions
        """A persistable term is not enough: every term must replay after restart.

        The result map can collapse to a digest, so the SHA list belongs in the
        operands captured before `_objexist_map` pops its pending key. This drives
        that real pop/write path, then proves an unchanged repository reproduces
        the exact witness and a changed answer moves it."""
        from helm import web_land_model as m
        gd = "/repo/.git"
        tips = ["a" * 40, "b" * 40]
        present = {tips[0]: (True, "commit"), tips[1]: (False, None)}

        with mock.patch.object(landreq, "_object_exists_batch",
                               return_value=present) as batch:
            reads = m._LrReadSet()
            reads[("objexist-pending", gd)] = tips
            self.assertEqual(present, landreq._objexist_map(gd, reads))
            saved = reads.witness()

            self.assertIsInstance(saved, str)
            self.assertEqual(reads._blind, [])
            terms = [term.split("\x1f") for term in saved.split("\x1e")]
            objterm = next(term for term in terms if term[0] == "objexist")
            self.assertEqual([gd] + tips, json.loads(objterm[1]))

            replay = m._LrReadSet()
            self.assertEqual(saved, replay.recheck(saved),
                             "an unchanged object batch could not restore")
            self.assertEqual(
                [mock.call(gd, tips), mock.call(gd, tips)],
                batch.call_args_list,
                "build and restart replay did not ask the same exact batch")

        moved = {tips[0]: (False, None), tips[1]: (False, None)}
        with mock.patch.object(landreq, "_object_exists_batch",
                               return_value=moved):
            replay = m._LrReadSet()
            self.assertNotEqual(
                saved, replay.recheck(saved),
                "a moved object-existence answer restored the body as CURRENT")

        with mock.patch.object(landreq, "_object_exists_batch",
                               return_value=None):
            live = m._LrReadSet()
            live[("objexist-pending", gd)] = tips
            failed = landreq._objexist_map(gd, live)
            self.assertEqual({}, failed,
                             "batch failure no longer falls through per SHA")
            self.assertIsInstance(
                failed, landreq._ObjectExistenceBatchFailure,
                "the live cache erased failure into an ordinary empty map")
            self.assertIsNone(live.witness(),
                              "an unreadable live batch remained persistable")
            self.assertTrue(live._blind,
                            "the unreadable live read did not name blindness")

            replay = m._LrReadSet()
            self.assertIsNone(
                replay.recheck(saved),
                "an UNREADABLE object batch compared as an ordinary empty map "
                "and restored the body as CURRENT")
            self.assertTrue(replay._blind,
                            "the unreadable replay did not name its blindness")

    def test_the_ATTEST_sidecar_is_identified_by_the_leg_that_reads_it(self):
        """Attestation rides the badges from its OWN sidecar, which is a
        separate file precisely so it can move independently: an append changes
        what the card says with no ledger write and no trunk move. The row loop
        is what consumes it, so the row loop is what records its identity."""
        from helm import web_land_model as m
        with mock.patch.object(landreq, "project_raw",
                               return_value=({}, {}, None)):
            with m._lr_snapshot() as reads:
                web_land._lr_project(time.time(), None)
                witness = reads.witness()
        self.assertIsInstance(witness, str)
        labels = {json.loads(t.split("\x1f")[1])[0]
                  for t in witness.split("\x1e") if t.startswith("stat\x1f")}
        self.assertIn("attest", labels,
                      "the attest sidecar is not fingerprinted: %r" % (labels,))

    def test_ABSENT_and_UNREADABLE_are_DIFFERENT_inputs(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone on the unreadable record has its unconditional positive control FIRST and on the same observable: `one.witness()` over a REAL file is asserted to be a str, so this reader demonstrably produces witnesses
        """Collapsing them is the defect this witness exists to avoid: a path
        that cannot be READ reported as a file that is not THERE. For the same
        path they produced identical terms, so a chmod that hid a ledger left
        the fingerprint unchanged and the old body was restored as current.

        AND THEY DIFFER IN KIND, not only in value. Absent is a MEASURED fact
        about the world that a later reading can contradict, so it identifies
        its input; unreadable is the instrument failing, so it identifies
        nothing and the whole record goes blind."""
        from helm import web_land_model as m
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        real = os.path.join(d, "real.jsonl")
        open(real, "w").close()
        hidden = os.path.join(d, "hid")
        os.makedirs(hidden, mode=0o000, exist_ok=True)
        self.addCleanup(os.chmod, hidden, 0o755)
        gone = os.path.join(d, "not-there.jsonl")
        buried = os.path.join(hidden, "x.jsonl")
        # UNCONDITIONAL POSITIVE CONTROL, TAKEN FIRST AND ON THE SAME
        # OBSERVABLE: a REAL file identifies itself, so this reader demonstrably
        # produces witnesses and the refusal below is about the chmod.
        one = m._LrReadSet()
        one.stat("ledger", real)
        self.assertIsInstance(one.witness(), str)
        absent = m._LrReadSet()
        absent.stat("ledger", gone)
        unreadable = m._LrReadSet()
        unreadable.stat("ledger", buried)
        self.assertIsInstance(absent.witness(), str,
                              "a file that is not there is a MEASUREMENT")
        self.assertNotEqual(absent.witness(), one.witness(),
                            "absent and present share a witness term")
        self.assertIsNone(unreadable.witness(),
                          "a ledger that cannot be READ identified itself, so "
                          "a chmod would restore the old body as current")

    def test_BOTH_build_paths_certify_the_reads_THE_BODY_MADE(self):
        """WHAT REPLACED "sample the witness before the build".

        Ordering was the old answer and it was never total: it puts the two
        readings in sequence, and an input that moves and moves BACK leaves them
        equal while the body in between saw the middle. The witness is now
        PROJECTED from the read-set the body consumed through, so the question
        is no longer WHEN it was sampled but WHAT it describes — and the answer
        has to be "the reads the body actually made", on BOTH routes: the
        background worker and the ANCIENT foreground fallthrough where a build
        that overran its hard ttl lands.

        A read the body made mid-build MUST be in the saved witness, and a read
        it never made must not — which is the property no ordering rule can
        state, because a sample taken first cannot contain the first, and a
        sample taken last cannot exclude the second."""
        class Recording:
            """The minimal read-set contract: serve, record, project."""

            def __init__(self):
                self.served = []

            def __call__(self):
                return contextlib.nullcontext(self)

            def read(self, name):
                self.served.append(name)
                return name

            def witness(self):
                return "|".join(sorted(self.served)) or None

            def recheck(self, _saved):
                return self.witness()

        saved = {}
        for name, drive in (("t757_fg", "foreground"), ("t757_bg", "background")):
            snap = Recording()
            web_cache._qstate.pop(name, None)
            web_cache._qrestored.discard(name)
            self.addCleanup(web_cache._qstate.pop, name, None)
            self.addCleanup(web_cache._qrestored.discard, name)
            path = web_cache._persist_path(name)
            self.addCleanup(lambda p=path: os.path.exists(p) and os.unlink(p))

            def build(s=snap):
                s.read("during")            # a read the body makes MID-BUILD
                return {"read_ts": time.time(), "loops": []}

            if drive == "foreground":
                web_cache._cached(name, 30, build, snapshot=snap)
            else:
                web_cache._swr_rebuild(name, build, threading.Event(),
                                       snapshot=snap)
            with open(path, encoding="utf-8") as fh:
                saved[name] = json.load(fh)["input_witness"]
        # ONE UNCONDITIONAL ASSERTION NAMING BOTH ROUTES, because a loop-local
        # assertion does not fire at all when the loop does not run — which is
        # exactly how a route that silently persisted nothing would hide.
        # "during" present says the witness describes the build's own read;
        # nothing else present says it invented none.
        self.assertEqual({"t757_fg": "during", "t757_bg": "during"}, saved)

    # ── persistence safety, one arm per property ──────────────────────────

    def test_an_UNAVAILABLE_body_is_never_SAVED(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control re-saves a HEALTHY body at the same key with the same witness and asserts it IS served, so the refusal above cannot be a dead writer
        """A failure body is a true answer for the request that produced it and
        a lie about the world afterwards. Saved, it outlives its cause: the
        recovery that already happened cannot dislodge it while the input sits
        unchanged, so the card shows a failure the system no longer has."""
        now = time.time()
        web_cache._persist_store("t757_unav", now,
                                 {"read_ts": now, "unavailable": "ledger gone"},
                                 "W1")
        # ASSERT THE ARTIFACT, NOT THE ROUND TRIP. `persist_load` ALSO refuses
        # an unavailable body, so a check that goes through it cannot see the
        # WRITE-side rule disappear — measured: deleting the write guard left
        # this arm green because the read guard caught it. Two guards are
        # right; an arm that cannot tell which one fired is not.
        self.assertFalse(os.path.exists(web_cache._persist_path("t757_unav")),
                         "a failure body was written to disk")
        self.assertIsNone(web_cache.persist_load("t757_unav", 30,
                                                 self._snap("W1")),
                          "a failure body was persisted and served back")
        # UNCONDITIONAL POSITIVE CONTROL on the same writer: a HEALTHY body at
        # the same key, same witness, same call IS saved and served.
        self._saved(now, key="t757_unav", wit="W1")
        self.assertIsNotNone(web_cache.persist_load("t757_unav", 30,
                                                    self._snap("W1")),
                             "the writer is dead, so the refusal proved nothing")

    def test_an_UNAVAILABLE_body_already_on_disk_is_never_RESTORED(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control rewrites the identical hand-authored record WITHOUT the failure marker and asserts it loads, isolating `unavailable` as the cause
        """The write-side refusal cannot reach a file written before it
        existed, and that file is still on disk."""
        now = time.time()
        path = web_cache._persist_path("t757_old")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema": web_cache._PERSIST_SCHEMA, "stored_ts": now, "input_witness": "W1",
                       "body": {"read_ts": now, "unavailable": "old failure"}},
                      fh)
        self.assertIsNone(web_cache.persist_load("t757_old", 30,
                                                 self._snap("W1")))
        # POSITIVE CONTROL: the identical hand-written shape WITHOUT the
        # failure marker loads, so the refusal is about `unavailable` and not
        # about hand-written files.
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema": web_cache._PERSIST_SCHEMA, "stored_ts": now, "input_witness": "W1",
                       "body": {"read_ts": now, "loops": []}}, fh)
        self.assertIsNotNone(web_cache.persist_load("t757_old", 30,
                                                    self._snap("W1")))

    def test_a_FUTURE_timestamp_is_refused_rather_than_pinning_the_body(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control re-saves the same key and witness with a SANE stamp and asserts it loads
        """Age is `now - stored_ts`. One clock skew, one restored backup or one
        hand-edited file makes that NEGATIVE — younger than every ttl there
        will ever be — so the entry can never leave FRESH and never triggers
        the rebuild that would replace it. A body pinned fresh forever is the
        confident stale board this whole feature exists to avoid."""
        now = time.time()
        self._saved(now + 3600, key="t757_fut", wit="W1")
        self.assertIsNone(web_cache.persist_load("t757_fut", 30,
                                                 self._snap("W1")),
                          "a future-dated body was admitted")
        # POSITIVE CONTROL: the same key and witness with a SANE stamp loads.
        self._saved(now, key="t757_fut", wit="W1")
        self.assertIsNotNone(web_cache.persist_load("t757_fut", 30,
                                                    self._snap("W1")))

    def test_the_SHARED_cache_persists_NOTHING(self):
        """Write-behind was wired into `_cached`, which every surface uses —
        so bodies carrying credential identity and home paths were serialised
        to disk for a feature only the land projection can use. Persistence
        now belongs to callers that can identify their own input, which is the
        same condition under which a body could ever be restored."""
        for key in ("t757_shared", "t757_swr_nowit"):
            web_cache._qstate.pop(key, None)
            web_cache._qrestored.discard(key)
            self.addCleanup(web_cache._qstate.pop, key, None)
            self.addCleanup(web_cache._qrestored.discard, key)
        web_cache._cached("t757_shared", 30, lambda: {"secret": "creds"})
        self.assertFalse(os.path.exists(web_cache._persist_path("t757_shared")),
                         "the shared cache wrote a body to disk")
        # ...and _cached_swr with NO witness is equally silent.
        web_cache._cached_swr("t757_swr_nowit", 30, 120,
                              lambda: {"read_ts": time.time(), "loops": []})
        self.assertFalse(
            os.path.exists(web_cache._persist_path("t757_swr_nowit")),
            "a witness-less swr caller wrote a body to disk")
        # UNCONDITIONAL POSITIVE CONTROL on the same writer: WITH a witness a
        # file appears, so the two absences above measure the scoping rule and
        # not a writer that never works.
        web_cache._persist_store("t757_shared", time.time(),
                                 {"read_ts": time.time(), "loops": []}, "W1")
        self.assertTrue(os.path.exists(web_cache._persist_path("t757_shared")))

    def test_the_persisted_file_is_PRIVATE(self):
        """Fleet state on a shared box. A group-readable projection is an
        exposure whether or not today's body happens to be dull."""
        self._saved(time.time(), key="t757_mode", wit="W1")
        mode = stat.S_IMODE(os.stat(web_cache._persist_path("t757_mode")).st_mode)
        self.assertEqual(mode, 0o600, "persisted body is %o" % mode)

    def test_a_ROW_REPOSITORY_S_TRUNK_is_recorded_BY_THE_ROW_LOOP(self):
        """A reviewer measured the wrong authority being fingerprinted, and the
        enumeration that replaced it was still a SECOND list.

        Rows carry their own `repo_id`, and landedness is computed against THAT
        repository's RESOLVED LOCAL AND UPSTREAM TRUNK (never HEAD, which a bare
        or common gitdir keeps on local main while refs/remotes/origin/main
        moves underneath it). So a fold reaching repo B flips a READY badge
        while this repo's trunk and every ledger sit unchanged. The previous
        cure walked the ledger a second time to enumerate those repositories —
        correct, and still a second reading, which is the shape this lane exists
        to retire. The record now holds whichever repositories the ROW LOOP
        resolved, because `project_raw`'s cache IS the read-set."""
        from helm import web_land_model as m
        upstream = ["AAA"]

        def fake_trunk_refs(gitdir, cache):
            if gitdir not in cache:
                cache[gitdir] = ("refs/heads/main", "refs/remotes/origin/main")
            return cache[gitdir]

        def fake_resolve(gitdir, ref, cache):
            key = ("refsha", gitdir, ref)
            if key not in cache:
                cache[key] = upstream[0] if "origin" in ref else "LOCAL-FIXED"
            return cache[key]

        def rows(now=None, selector=None, cache=None, scope=None,
                 repo_id=None):
            # THE ROW LOOP, mechanically: each row's repository is judged
            # against its own trunk, through the cache the caller handed in.
            for rid in ("/repo/a/.git", "/repo/b/.git"):
                local, up = fake_trunk_refs(rid, cache)
                fake_resolve(rid, up, cache)
                fake_resolve(rid, local, cache)
            return {}, {}, None

        def collect():
            with mock.patch.object(landreq, "project_raw", side_effect=rows):
                with m._lr_snapshot() as reads:
                    web_land._lr_project(time.time(), None)
                    return reads.witness()

        before = collect()
        # UNCONDITIONAL POSITIVE CONTROL: nothing moved, so the projection is
        # STABLE. Without it the inequality below would also pass against a
        # witness that were simply nondeterministic.
        self.assertIsInstance(before, str)
        self.assertEqual(before, collect(), "the projection is not even stable")
        self.assertIn("/repo/b/.git", before,
                      "a repository the row loop resolved was never recorded")
        upstream[0] = "BBB"                  # ONLY the upstream trunk moves
        self.assertNotEqual(
            before, collect(),
            "the upstream trunk moved and the witness did not — this is the "
            "exact shape that restores a stale board FRESH")

    def test_an_UNCLASSIFIED_projection_cache_key_makes_the_record_BLIND(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone is preceded unconditionally, on the same observable, by assertIsInstance(reads.witness(), str) over the two CLASSIFIED cache shapes
        """THE DEFAULT THAT KEEPS THE DOOR SINGLE. `landreq` memoises into the
        dict this read-set IS, and two of its keys name a MUTABLE binding while
        one is derived from a pin already recorded. A key nobody has classified
        is a read nobody has decided about, and the honest answer is that this
        snapshot cannot describe itself — never a silent skip, which is how an
        input rejoins the body without rejoining the witness."""
        from helm import web_land_model as m
        reads = m._LrReadSet()
        reads[("refsha", "/x/.git", "refs/heads/main")] = "abc123"
        # UNCONDITIONAL POSITIVE CONTROL FIRST, on the same observable: the two
        # classified shapes DO identify themselves, so the refusal below is
        # about the unknown key and not about a reader that refuses everything.
        reads[("base_behind", "/x/.git", "tip", "abc123")] = 4
        reads["/x/.git"] = ("refs/heads/main", None)
        self.assertIsInstance(reads.witness(), str)
        reads[("some_future_memo", "/x/.git")] = "value"
        self.assertIsNone(reads.witness(),
                          "an unclassified read left the record persistable, "
                          "so a future memo would ride in unwitnessed")

    def test_a_body_of_UNKNOWN_SHAPE_is_refused_though_its_witness_matches(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone refusal has its unconditional positive control on the SAME loader and the SAME record, taken BEFORE the schema is mutated: assertIsNotNone proves the fixture loads as written, so the refusal cannot be an unloadable-fixture artefact
        """The witness proves the DATA a body was computed from is unchanged.
        It says nothing about the CODE that shaped it. Across a deploy that
        adds, renames or re-nests a field, the ledgers can be byte-identical
        while the saved body is the wrong shape — and on a matching witness it
        restores FRESH, so the console renders a stale schema as current with
        no rebuild that would ever correct it."""
        now = time.time()
        self._saved(now, key="t757_schema", wit="W1")
        path = web_cache._persist_path("t757_schema")
        with open(path, encoding="utf-8") as fh:
            rec = json.load(fh)
        # UNCONDITIONAL POSITIVE CONTROL, TAKEN FIRST: as written this record
        # DOES load. Without it, the refusal below would also pass if the
        # fixture never produced a loadable record at all.
        self.assertIsNotNone(
            web_cache.persist_load("t757_schema", 30, self._snap("W1")),
            "the fixture never loaded, so a refusal proves nothing")
        rec["schema"] = rec.get("schema", 0) + 1      # a shape this code cannot read
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        self.assertIsNone(
            web_cache.persist_load("t757_schema", 30, self._snap("W1")),
            "a body of unrecognised shape was restored on a matching witness")

    def test_the_predecessor_schema_is_rejected_and_rebuilt(self):
        key = "t757_predecessor_schema"
        path = web_cache._persist_path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema": 1,
                       "stored_ts": time.time(), "input_witness": "W1",
                       "body": {"read_ts": 1, "loops": ["old-shape"]}}, fh)
        web_cache._qstate.pop(key, None)
        web_cache._qrestored.discard(key)
        web_cache._qinflight.pop(key, None)
        self.addCleanup(web_cache._qstate.pop, key, None)
        self.addCleanup(web_cache._qrestored.discard, key)
        self.addCleanup(web_cache._qinflight.pop, key, None)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        built = []

        def build():
            built.append(True)
            return {"read_ts": time.time(), "loops": ["current-shape"]}

        got = web_cache._cached_swr(key, 30, 120, build,
                                    snapshot=self._snap("W1"))
        self.assertEqual(got["loops"], ["current-shape"])
        self.assertEqual(built, [True], "old schema bypassed the rebuild")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["schema"], web_cache._PERSIST_SCHEMA)

    def test_the_RESTORE_read_happens_OUTSIDE_the_global_cache_lock(self):  # noqa: VACUOUS_ASSERTION — the lock question is only observable from inside the reader, so the spy IS the instrument; assertTrue(free) is the unconditional positive control that the restore ran at all, and the assertion below is a True, not an absence
        """`_qlock` is global to EVERY cache key. `persist_load` reads the
        saved envelope AND stats the ledgers, the trunk and the premise store
        to rebuild a witness, so holding the lock across it makes one key's
        disk latency every key's — measured by a reviewer as a 0.350s restore
        blocking an unrelated, already-FRESH read for the same 0.350s."""
        free = []
        real_load = web_cache.persist_load

        def watching(key, ttl, snap=None):
            # `acquire(blocking=False)` on a plain Lock fails if THIS thread
            # already holds it, which is exactly the question.
            got = web_cache._qlock.acquire(blocking=False)
            free.append(got)
            if got:
                web_cache._qlock.release()
            return real_load(key, ttl, snap)

        now = time.time()
        self._saved(now, key="t757_lock", wit="W1")
        web_cache._qstate.pop("t757_lock", None)
        web_cache._qrestored.discard("t757_lock")
        with mock.patch.object(web_cache, "persist_load", side_effect=watching):
            web_cache._cached_swr("t757_lock", 30, 120,
                                  lambda: {"read_ts": now, "loops": []},
                                  snapshot=self._snap("W1"))
        self.assertTrue(free, "the restore never ran, so nothing was measured")
        self.assertTrue(free[0],
                        "the restore read the disk while holding _qlock — one "
                        "key's I/O is now every key's latency")

    def test_the_disk_write_happens_OUTSIDE_the_global_cache_lock(self):  # noqa: VACUOUS_ASSERTION — the lock question can only be observed from inside the writer, so the spy is the instrument; the unconditional positive control is the assertEqual on the served body's probe field, which fails if the cache answered nothing
        """`_qlock` is global to EVERY cache key, so a disk write held under it
        stalls readers of unrelated keys that were already FRESH — observed
        as a fresh read blocked for the full duration of an injected I/O
        delay. The in-memory entry is published before the save; nothing in
        this server life waits on the file."""
        held = []
        real_store = web_cache._persist_store

        def watching(key, ts, body, witness):
            # `acquire(blocking=False)` succeeds only if the lock is FREE.
            got = web_cache._qlock.acquire(blocking=False)
            held.append(not got)          # True == the lock was held over I/O
            if got:
                web_cache._qlock.release()
            return real_store(key, ts, body, witness)

        key = "t757_lock"
        web_cache._qstate.pop(key, None)
        web_cache._qrestored.discard(key)
        self.addCleanup(web_cache._qstate.pop, key, None)
        self.addCleanup(web_cache._qrestored.discard, key)
        with mock.patch.object(web_cache, "_persist_store", watching):
            served = web_cache._cached_swr(
                key, 30, 120,
                lambda: {"read_ts": time.time(), "loops": [], "probe": key},
                snapshot=self._snap("W1"))
        # UNCONDITIONAL POSITIVE ON THE PRODUCTION CALL'S OWN RESULT, not on
        # the spy list beside it: proving the instrumentation ran says nothing
        # about what the cache answered.
        self.assertEqual(served.get("probe"), key,
                         "the cache did not serve a real body")
        self.assertTrue(held, "the writer never ran, so nothing was measured")
        self.assertNotIn(True, held,
                         "the global cache lock was held across disk I/O")

    def test_a_record_with_NO_witness_field_is_UNKNOWN_not_current(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control rewrites the same file WITH a matching witness and asserts it loads
        """A file written before witnesses existed cannot prove anything about
        today's input. It must read as UNKNOWN and leave the regime EMPTY,
        never as "unchanged"."""
        now = time.time()
        path = web_cache._persist_path("t757_legacy")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema": web_cache._PERSIST_SCHEMA, "stored_ts": now,
                       "body": {"read_ts": now, "loops": []}}, fh)
        self.assertIsNone(web_cache.persist_load("t757_legacy", 30,
                                                 self._snap("W1")),
                          "a witness-less record was treated as current")
        # POSITIVE CONTROL: the same file WITH a matching witness loads.
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema": web_cache._PERSIST_SCHEMA, "stored_ts": now, "input_witness": "W1",
                       "body": {"read_ts": now, "loops": []}}, fh)
        self.assertIsNotNone(web_cache.persist_load("t757_legacy", 30,
                                                    self._snap("W1")))


class TheBuildBindsToOneTrunkCommitTest(LrApiBase):
    """THE WITNESS A-B-A (task/915, found in review).

    The freshness witness fingerprinted (ref, resolved sha) at ONE instant.
    `_lr_recent_lands` then re-resolved the SAME MUTABLE REF for two `git log`
    walks and a landing proof per row, and `project_raw` proved every land loop
    against it again. Production trunk is `refs/remotes/origin/main`, which a
    foldcheck force-fetch MOVES and can REWIND (tests/test_foldcheck.py). Over
    a measured 22-28s build that is trunk A at the witness, trunk B through the
    body, trunk A again before the save — so an A witness is stored over a
    B-or-mixed body and `persist_load` admits it FRESH on the next server life.

    SAMPLING THE WITNESS BEFORE AND AFTER CANNOT DETECT THIS: equal endpoints
    are the definition of an A-B-A. Only binding every read in the build to ONE
    resolved commit closes it, which is what these arms measure.

    THE FIXTURE MOVES A REAL REF IN A REAL REPOSITORY. `git update-ref` on a
    remote-tracking ref is exactly what a force-fetch does, and nothing here
    stands in for git — a mock could not tell a pinned read from an unpinned
    one, because the whole difference is which commit the argv names."""

    def setUp(self):
        super().setUp()
        # ORIGIN CONFIGURED, so `_lr_repo` answers with the UPSTREAM ref —
        # `refs/remotes/origin/<main>`, the ref production actually reads and
        # the only one a fetch can move under a running build.
        self.git("remote", "add", "origin", self.repo)
        self.trunk_ref = "refs/remotes/origin/" + self.main
        # A — trunk carrying ONE land, and NOT carrying `side`.
        self.fold("lane-a", self.b[:12])
        self.A = self.git("rev-parse", "HEAD")
        self.git("update-ref", self.trunk_ref, self.A)
        # B — A plus a SECOND land, and that land really puts `side` on trunk,
        # so A and B disagree about a row's landedness as well as about the
        # lands card. One fixture, both legs.
        self.git("merge", "-q", "--no-ff", "-m",
                 "fold: lane-b at %s" % self.side[:12], "side")
        self.B = self.git("rev-parse", "HEAD")
        self.assertNotEqual(self.A, self.B)

    def move_trunk(self, sha):
        """A force-fetch, mechanically: the ref now names another commit."""
        self.git("update-ref", self.trunk_ref, sha)

    @staticmethod
    def collect(reads):
        """DRIVE THE BODY, then project. Never a hand-written list of reads: the
        witness is a projection of what the projection READ, so a fixture that
        enumerated the reads itself would be measuring its own bookkeeping
        rather than the build's.

        IT DRIVES `_lr_project` (task/2355). Calling the three legs individually
        is equivalent only while `_lr_recent_lands` resolves trunk on its way to
        `git log --grep ^fold:`. This one reads the LEDGER and touches git not at
        all, so the trunk pin lives at the top of `_lr_project` — where the rows
        are then proved against it — and driving the legs would collect a record
        with no trunk in it, leaving every arm below green over exactly the
        binding they exist to measure."""
        from helm import web_land, web_land_model as m
        m._lr_newest_mtime()
        web_land._lr_project(time.time(), None)
        return reads.witness()

    # ── the build's trunk binding ────────────────────────────────────────

    def test_a_trunk_that_MOVES_MID_BUILD_cannot_enter_the_body(self):
        """The arm that makes the defect FALSE. The build resolves trunk at A,
        the ref then moves to B exactly as a background fetch would, and every
        later reading inside that build must still describe A — because the
        witness this body is stored under is a projection of that resolution.

        THE OBSERVABLE IS THE WITNESS, not a lane list. It was a lane list while
        the lands card was a trunk walk whose rows named the fold commits; the
        card reads the ledger now, so the thing this arm is actually about — one
        build, one resolved commit — is read off the record the build kept."""
        from helm import web_land, web_land_model as m
        self.move_trunk(self.A)
        with m._lr_snapshot() as reads:
            web_land._lr_project(time.time(), None)   # resolves trunk: A
            self.move_trunk(self.B)                   # the fetch, mid-build
            web_land._lr_project(time.time(), None)
            witness = reads.witness()
        # UNCONDITIONAL POSITIVE CONTROL on the SAME observable, FIRST: the
        # build really resolved a commit through the record. Without it the
        # absence below would also pass against a build that read nothing.
        self.assertIsInstance(witness, str)
        self.assertIn("refsha", {t.split("\x1f")[0]
                                 for t in witness.split("\x1e")},
                      "the build did not resolve trunk through the record")
        self.assertIn(self.A, witness, "the witness does not name the commit")
        self.assertNotIn(self.B, witness,
                         "the witness named a commit taken after the pin — "
                         "this is the A-B-A: an A witness over a B body")

    def test_WITHOUT_the_pin_the_same_move_DOES_reach_the_body(self):
        """THE CONTROL THAT MAKES THE ARM ABOVE A MEASUREMENT AND NOT A HABIT.

        Identical fixture, identical move, no snapshot — so nothing is pinned
        and the ref is re-resolved exactly as it was before this cure. It MUST
        see B. If it does not, the ref never really moved, and the arm above
        would be green for reasons that have nothing to do with pinning."""
        from helm import web_land, web_land_model as m
        self.move_trunk(self.A)
        with m._lr_snapshot() as reads:
            web_land._lr_project(time.time(), None)
            pinned = reads.witness()
        self.move_trunk(self.B)
        with m._lr_snapshot() as reads:
            web_land._lr_project(time.time(), None)
            moved = reads.witness()
        self.assertIn(self.A, pinned)
        self.assertIn(self.B, moved,
                      "the unpinned read did not follow the moved ref, so the "
                      "pinned arm above measures nothing")
        self.assertNotIn(self.B, pinned)

    def test_EVERY_git_read_in_ONE_build_names_the_PINNED_commit(self):
        """The build makes several git reads off trunk — the pin itself and a
        landedness proof per row. A read that takes the ref NAME instead of the
        pinned commit lets a fetch between any two of them produce a body whose
        rows describe different trunks under one read stamp and one witness.

        A ROW IS PLANTED BECAUSE THE READS THIS ARM WATCHES ARE THE ROW PROOFS.
        No lands walk supplies them (task/2355), and an empty ledger makes no
        proof at all — so without a row the emptiness check below would pass over
        a build that never touched git."""
        from helm import web_land, web_land_model as m
        # A VERDICT'D ROW, not merely a dispatched one: nothing is observed
        # before a verdict (`_will_observe_git`), so an OPEN row makes no git
        # read and this arm would have nothing to watch.
        self.legacy_undeclared(self.dispatch(ref=self.side))
        self.move_trunk(self.A)
        seen = []
        real = landreq._git_spawn

        def spy(gitdir, args, input_text, env=None):
            seen.append(tuple(str(a) for a in args))
            return real(gitdir, args, input_text, env)

        with mock.patch.object(landreq, "_git_spawn", side_effect=spy):
            with m._lr_snapshot() as reads:
                # THE PIN ONLY, not a whole leg: every read this build makes is
                # served ONCE, so a leg run before the move would answer the
                # one after it out of the record and spawn nothing at all —
                # which is the cure, and would leave this arm measuring an
                # empty list. The pin is what the lands leg must then consume.
                gitdir, trunk = reads.repo()
                reads.refsha(gitdir, trunk)
                self.move_trunk(self.B)
                mark = len(seen)
                web_land._lr_project(time.time(), None)
        after = seen[mark:]
        # TWO UNCONDITIONAL POSITIVE CONTROLS, both on the same observable as
        # the absence: git really ran after the move, and what it ran named the
        # PINNED commit. A dead spy or a leg that never spawned would fail here
        # rather than passing the emptiness check below.
        self.assertTrue(after, "no git read happened after the move at all")
        self.assertTrue([a for a in after if any(self.A in x for x in a)],
                        "no read after the move consumed the pinned commit")
        self.assertEqual(
            [a for a in after if any(self.trunk_ref in x for x in a)], [],
            "a read after the pin still named the MOVING ref, so this build "
            "can still be answered about two different trunks")

    # ── the per-row proof leg (landreq) ──────────────────────────────────

    def test_a_ROW_PROOF_is_judged_against_the_pin_not_the_moved_ref(self):
        """`project_raw` proves each land loop against trunk per row. With the
        ref name in the argv, row 1 could be judged against trunk A and row 900
        against trunk B, and the board reports both under one read stamp — and
        under one witness. The pin rides in the projection cache the caller
        hands in, so every row is judged against ONE commit."""
        gitdir = self.repo_git
        pinned = {}
        landreq._trunk_refs(gitdir, pinned)
        self.assertEqual(
            landreq._resolved_ref(gitdir, self.trunk_ref, pinned), self.A,
            "the pin did not resolve the ref that was set to A")
        self.move_trunk(self.B)              # `side` is on trunk from here on
        # THE PINNED CACHE: judged against A, where `side` has NOT landed.
        self.assertFalse(landreq._git_observe(gitdir, self.side, pinned)["upstream"],
                         "a row was judged against a trunk the witness never "
                         "described")
        # UNCONDITIONAL POSITIVE CONTROL, same repo, same tip, same instant —
        # the ONLY difference is the pin. A fresh cache re-resolves and sees B,
        # where `side` HAS landed. Both readings are true; only one of them
        # belongs in a body the witness will certify.
        self.assertTrue(landreq._git_observe(gitdir, self.side, {})["upstream"],
                        "the unpinned read did not see the moved ref, so the "
                        "pinned assertion above discriminates nothing")

    def test_the_ANNOTATION_pin_is_the_SAME_reading_as_the_build_pin(self):
        """THE LEG THAT IS BOUND INDIRECTLY, MEASURED RATHER THAN ASSERTED.

        `_landing_proofs` keeps its OWN per-repository trunk pin and resolves
        it through `_git`, not through the build's cache — so it is bound to
        the build's snapshot only because ONE projscope spans the whole cycle
        and its argv is `_resolved_ref`'s to the character. Two spellings would
        silently become two readings of a moving name, and the succession and
        frontier annotations would then describe a trunk the witness never
        named, with nothing anywhere disagreeing.

        So the ref MOVES between the two readings. If they are one reading the
        annotation still judges against A, where `side` has not landed; if they
        are two, the second sees B, where it has — and the proof flips."""
        seen = []
        real = landreq._git_spawn

        def spy(gitdir, args, input_text, env=None):
            seen.append(tuple(str(a) for a in args))
            return real(gitdir, args, input_text, env)

        owner = {"repo_id": self.repo_git}
        carrier = {"repo_id": self.repo_git, "reviewed_tip": self.side}
        with mock.patch.object(landreq, "_git_spawn", side_effect=spy), \
                projscope.scope():
            pinned = landreq._resolved_ref(self.repo_git, self.trunk_ref, {})
            self.move_trunk(self.B)      # the fetch, BETWEEN the two readings
            proof = landreq._landing_proofs(self.repo_git)(owner, carrier)
        # A PEEL IS THE ARGV **AND** THE ^{commit} OPERAND. Every ref read
        # in landreq now shares one argv, so the prefix alone no longer
        # distinguishes a peel from a plain resolve and this filter would
        # count both. The memo keys still differ — they carry the operand —
        # so what changed is this test's proxy for "is a peel", not the
        # property it measures.
        peels = [a for a in seen
                 if a[:len(landreq._REF_ARGV)] == landreq._REF_ARGV
                 and a[-1].endswith("^{commit}")]
        # THE MEASUREMENT AND ITS OWN POSITIVE CONTROL IN ONE ASSERTION: the
        # build pinned A, and the annotation — which re-pins for itself — still
        # answered ABSENT, which is only true of trunk A. Against B it is
        # PROOF_ANCESTOR, so this equality is the discrimination.
        self.assertEqual([self.A, landreq.PROOF_ABSENT], [pinned, proof])
        # AND THE MECHANISM, not just the outcome: ONE peel spawn served both.
        self.assertEqual(1, len(peels),
                         "the two trunk pins are separate readings of a "
                         "moving ref: %r" % (peels,))

    def test_a_JUNK_repo_binding_never_reaches_the_cache_KEY(self):
        """`repo_id` is copied verbatim out of the ledger row and never
        re-validated, so `["not", "a", "path"]` arrives at a resolver as
        itself. `_trunk_refs` refuses a non-string BEFORE its cache lookup
        precisely because an unhashable member in a cache KEY raises TypeError
        one statement before git would have declined it — a whole projection
        lost to one junk row (reproduced). A SECOND door into the
        same cache must not re-open that."""
        cache = {}
        self.assertEqual("", landreq._resolved_ref(["not", "a", "path"],
                                                   self.trunk_ref, cache),
                         "a junk repo binding was resolved, not refused")
        self.assertEqual({}, cache, "a junk binding reached the cache key")
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME CACHE: a REAL gitdir and
        # the SAME ref resolve and memoise, so the refusals above discriminate
        # the binding rather than describing a dead function.
        landreq._resolved_ref(self.repo_git, self.trunk_ref, cache)
        self.assertIn(self.A, list(cache.values()),
                      "a real binding did not resolve or did not memoise")

    def test_project_raw_ADOPTS_the_caller_s_pin(self):
        """The thread has to reach all the way in. A cure that pinned the lands
        leg while `project_raw` kept re-resolving would make the witness LOOK
        sound over a body half of which it never described — this row's own
        text says a partial fix is worse than none."""
        seen = {}

        def spy(now=None, selector=None, cache=None, scope=None,
                repo_id=None):
            seen["cache"] = cache
            return {}, {}, None

        with mock.patch.object(landreq, "project_raw", side_effect=spy):
            from helm import web_land_model as m
            with m._lr_snapshot() as pin:
                web_land._lr_project(time.time(), None)
            inside = seen.get("cache")
            # CLEARED, so the second reading cannot be the first one's
            # leftover — the failure this arm would otherwise be blind to is a
            # second call that never happened at all.
            seen.clear()
            web_land._lr_project(time.time(), None)
            outside = seen.get("cache")
        # UNCONDITIONAL POSITIVE CONTROL on both observables: each call really
        # reached `project_raw` and really handed it the read-set, which IS the
        # projection cache — `landreq` memoises `(gitdir) -> refs` and
        # `("refsha", …) -> commit` into this object, so the row loop's own
        # trunk readings land in the record the witness is projected from.
        self.assertEqual([True, True],
                         [isinstance(inside, m._LrReadSet),
                          isinstance(outside, m._LrReadSet)])
        self.assertIs(inside, pin,
                      "the projection built its own cache and threw the "
                      "build's read-set away")
        # AND THE PIN IS AN EXTENSION, NEVER A REQUIREMENT: outside a scope
        # the same call gets a throwaway, so `helm lr` and every other caller
        # are untouched by this cure.
        self.assertIsNot(outside, pin)

    # ── the wiring, which is where a silent no-op would live ─────────────

    def test_the_SAVED_witness_is_the_projection_of_the_BODY_S_read_set(self):
        """REQUIRED ARM 4. The whole design in one observable: the
        string on disk beside the body must be exactly what the object that
        served the body's reads projects — not a string that merely compares
        equal to one taken beside it.

        BOTH ROUTES, because `_cached_swr` falls through to the FOREGROUND
        `_cached` for a caller with no cold body while a slow build takes the
        BACKGROUND worker, and a binding that held on one of them would leave
        the hazard open on whichever route the day's build happened to take."""
        seen = {}

        class Reads:
            """A read-set the arm can interrogate: it serves reads, records
            them, and projects the record. Exactly the contract web_cache is
            written against, with nothing else in it."""

            def __init__(self):
                self.served = []
                self.projections = 0

            def __call__(self):
                seen.setdefault("entered", 0)
                seen["entered"] += 1
                return contextlib.nullcontext(self)

            def read(self, name):
                self.served.append(name)
                return name

            def witness(self):
                self.projections += 1
                return "+".join(self.served) or None

            def recheck(self, _saved):
                return self.witness()

        stored = {}
        for key, route in (("t757p4fg", "foreground"), ("t757p4bg", "background")):
            snap = Reads()
            web_cache._qstate.pop(key, None)
            web_cache._qrestored.discard(key)
            self.addCleanup(web_cache._qstate.pop, key, None)
            self.addCleanup(web_cache._qrestored.discard, key)
            path = web_cache._persist_path(key)
            self.addCleanup(lambda p=path: os.path.exists(p) and os.unlink(p))

            def build(s=snap):
                s.read("trunk@A")
                s.read("tier:claude")
                return {"read_ts": time.time(), "loops": []}

            if route == "foreground":
                web_cache._cached(key, 30, build, snapshot=snap)
            else:
                web_cache._swr_rebuild(key, build, threading.Event(),
                                       snapshot=snap)
            with open(path, encoding="utf-8") as fh:
                stored[key] = json.load(fh)["input_witness"]
            stored[key + "/projected"] = snap.witness()
        # ONE UNCONDITIONAL ASSERTION NAMING BOTH ROUTES. The saved string IS
        # the projection of the reads the body made, in the body's own order,
        # on each route — and it is a real, non-empty projection, so this
        # cannot be satisfied by two Nones agreeing.
        self.assertEqual(
            {"t757p4fg": "trunk@A+tier:claude",
             "t757p4fg/projected": "trunk@A+tier:claude",
             "t757p4bg": "trunk@A+tier:claude",
             "t757p4bg/projected": "trunk@A+tier:claude"}, stored)

    def test_api_lr_builds_its_body_INSIDE_the_read_set_it_witnesses(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone on the throwaway read-set has its unconditional control on the same observable one line above: assertIsInstance(projected, str) proves a read-set's projection is a real string here
        """THE LOAD-BEARING WIRING. Everything in web_land_model degrades to a
        THROWAWAY read-set when no snapshot is open, so a version of this cure
        that forgot the call site would still READ as bound and prove nothing.
        This arm reads the read-set OBJECT from inside the real endpoint's own
        build, and requires it to be the one whose projection is saved."""
        from helm import web_land_model as m
        saw = {}

        def build():
            saw["reads"] = m._lr_reads()
            saw["reads"].stat("ledger", __file__)   # a real, identifiable read
            return {"read_ts": time.time(), "ledger_mtime": None,
                    "probe": "t757-probe",
                    "unavailable": None, "receipts_skipped": None,
                    "filed": None, "loops": [], "stalled_ids": [],
                    "unmeasurable": [], "closed_recent": [], "closed_total": 0,
                    "closed_unknown_when": 0}

        self.forget()
        web_cache._qrestored.discard("lr")
        self.addCleanup(web_cache._qrestored.discard, "lr")
        path = web_cache._persist_path("lr")
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with mock.patch.object(web, "_lr_build", build):
            body, status = web.QUERY_API["/api/lr"]({})
        # UNCONDITIONAL POSITIVE CONTROLS, each on an observable a claim below
        # rests on: the endpoint answered, the body is the one THIS build
        # produced, the build saw a read-set, and that read-set projects.
        self.assertEqual(200, status)
        self.assertEqual("t757-probe", body["probe"])
        self.assertIn("reads", saw, "the build never ran")
        self.assertIsInstance(saw["reads"], m._LrReadSet)
        projected = saw["reads"].witness()
        self.assertIsInstance(projected, str)
        # AND `_lr_reads` HANDS BACK A THROWAWAY OUTSIDE A SNAPSHOT — measured
        # rather than assumed, because the identity claim would be satisfied
        # for free by an accessor that returned one shared object.
        loose_one, loose_two = m._lr_reads(), m._lr_reads()
        loose_one.stat("ledger", __file__)
        self.assertIsNot(loose_one, loose_two)
        self.assertIsNone(loose_two.witness(),
                          "a throwaway read-set inherited another's record")
        # THE ENDPOINT'S OWN SAVED WITNESS IS THAT PROJECTION. Reading the file
        # rather than an internal flag: this is the artifact a NEXT SERVER LIFE
        # compares against, and it is the only thing that can certify a body.
        #
        # WAITED FOR, BECAUSE WRITE-BEHIND MEANS BEHIND THE RESPONSE. A cold
        # /api/lr builds on a background worker and the save deliberately runs
        # after every waiter is released, so the endpoint returning is not the
        # save having happened. Polling for the artifact measures the same fact
        # without racing it; the bound is generous and its expiry is a failure
        # with its own message rather than a bare FileNotFoundError.
        deadline = time.time() + 20
        while not os.path.exists(path) and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(path),
                        "/api/lr never persisted a body at all, so the witness "
                        "comparison below could not run")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(projected, json.load(fh)["input_witness"],
                             "/api/lr saved a witness its build's read-set "
                             "did not produce")

    # ── the negative control for the whole cure ──────────────────────────

    def test_an_UNCHANGED_trunk_still_witnesses_IDENTICALLY_and_reads_FRESH(self):
        """The failure this cure could hide behind: bind hard enough and every
        build looks changed, so nothing ever restores and the arms above stay
        green over a feature that no longer works. A repository nobody touched
        must produce the SAME witness twice and admit its body as CURRENT."""
        from helm import web_land_model as m
        self.move_trunk(self.A)
        with m._lr_snapshot() as reads:
            first = self.collect(reads)
        with m._lr_snapshot() as reads:
            second = self.collect(reads)
        # POSITIVE CONTROLS: these are real readings of the real repository,
        # not two equal failures — each witness names the commit trunk stands
        # on. Asserted on BOTH, so neither can be an empty string.
        self.assertIn(self.A, first, "the witness is not a real trunk reading")
        self.assertIn(self.A, second, "the second witness is not a reading")
        self.assertEqual(first, second,
                         "an untouched repository witnessed differently twice")
        key = "t915fresh"
        web_cache._qstate.pop(key, None)
        self.addCleanup(web_cache._qstate.pop, key, None)
        web_cache._persist_store(key, time.time(), {"read_ts": 1, "loops": []},
                                 first)
        got = web_cache.persist_load(key, 30, web_cache._ConstSnapshot(second))
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: the loader
        # handed back THE SAVED BODY, so the age reading below is about a real
        # restoration and not about a None that never loaded.
        self.assertEqual({"read_ts": 1, "loops": []}, got[1])
        self.assertLess(time.time() - got[0], 30,
                        "an unchanged input was aged into STALE")

    def test_a_MOVED_trunk_still_ages_the_body_out(self):
        """And the other direction, because a pin that froze the WITNESS too
        would restore a genuinely stale board forever. The pin lives for one
        build; a NEW build opens a new one and reads the world as it is."""
        from helm import web_land_model as m
        self.move_trunk(self.A)
        with m._lr_snapshot() as reads:
            first = self.collect(reads)
        self.move_trunk(self.B)
        with m._lr_snapshot() as reads:
            second = self.collect(reads)
        self.assertIn(self.B, second, "the new build did not see the new trunk")
        self.assertNotEqual(first, second,
                            "trunk moved between two builds and the witness "
                            "did not — a stale board would restore FRESH")
        key = "t915stale"
        web_cache._qstate.pop(key, None)
        self.addCleanup(web_cache._qstate.pop, key, None)
        web_cache._persist_store(key, time.time(), {"read_ts": 1, "loops": []},
                                 first)
        got = web_cache.persist_load(key, 30, web_cache._ConstSnapshot(second))
        self.assertIsNotNone(got, "a moved input dropped the body entirely")
        self.assertGreater(time.time() - got[0], 30,
                           "a body whose trunk moved was served FRESH")

    # ── the FAILURE half of the pin (task/915, round two) ───────

    def test_TWO_UNRESOLVABLE_pins_MUST_NOT_COMPARE_EQUAL(self):  # noqa: VACUOUS_ASSERTION — the [None, None] assertion runs AFTER an unconditional positive control on the same observable: with a working peel the same two collections are asserted to be str AND to differ across the very move the failed pair must not hide
        """THE A-B-A THAT SURVIVED THE FIRST CURE, and it lives on the FAILURE
        path rather than the success path the arms above close.

        When the immutable peel RESOLVED, the witness named a commit and the
        body read that commit — bound. When it FAILED, the witness recorded
        UNRESOLVABLE and `_lr_recent_lands` fell back to the MOVING REF, so
        the body came from trunk B while the witness said "I could not tell".
        A second failure produced the SAME term, UNRESOLVABLE equalled
        UNRESOLVABLE, and the B body restored as FRESH: A-fail / B-body /
        A-fail reads current.

        THE SHAPE IS WORTH THE NAME, because it looks like caution: an UNKNOWN
        state was added without pinning what the MEASURED case still costs.
        Two unknowns comparing equal is a permissive default — and "I could
        not identify this input" is exactly the condition under which the
        input may have done anything at all."""
        from helm import web_land_model as m
        self.move_trunk(self.A)
        # THE POSITIVE CONTROL RUNS FIRST AND ON THE SAME OBSERVABLE: with the
        # peel working these are real strings, and they DIFFER across the very
        # move that the failed pair must not be allowed to hide.
        with m._lr_snapshot() as reads:
            good_a = self.collect(reads)
        self.move_trunk(self.B)
        with m._lr_snapshot() as reads:
            good_b = self.collect(reads)
        self.assertIsInstance(good_a, str)
        self.assertIsInstance(good_b, str)
        self.assertNotEqual(good_a, good_b,
                            "the working peel did not even see the move, so "
                            "the refusal below discriminates nothing")
        # AND NOW THE PEEL FAILS — the ref still exists and still resolves
        # through `_trunk_refs`, it is the `^{commit}` peel that cannot
        # answer, which is precisely the transient a force-fetch produces.
        with mock.patch.object(landreq, "_resolve_trunk_commit",
                               return_value=""):
            self.move_trunk(self.A)
            with m._lr_snapshot() as reads:
                fail_a = self.collect(reads)
            self.move_trunk(self.B)
            with m._lr_snapshot() as reads:
                fail_b = self.collect(reads)
        self.assertEqual([None, None], [fail_a, fail_b],
                         "an unresolvable pin still produced a witness — two "
                         "of these compare EQUAL, which certifies a body "
                         "built from a ref that moved between them")

    def test_a_failed_pin_neither_SAVES_a_body_nor_RESTORES_one(self):
        """WHAT AN UNRESOLVABLE PIN NOW DOES, stated as an observable rather
        than as an internal flag: the entry becomes UNUSABLE in both
        directions. `_persist_store` requires a witness and writes no file
        without one; `persist_load` cannot identify the input and answers
        None, leaving the cache EMPTY — which is exactly how this endpoint
        behaved before persistence existed. Refusing to serve is acceptable;
        silently serving a body assembled from a moving ref is not."""
        from helm import web_land_model as m
        self.move_trunk(self.A)
        key = "t915unpinned"
        path = web_cache._persist_path(key)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        # POSITIVE CONTROLS, all three on the SAME observables the refusals
        # below use: a working peel yields a witness VALUE, that value puts a
        # real file on disk, and the loader hands the body back.
        with m._lr_snapshot() as reads:
            self.collect(reads)
            good = web_cache._witness_value(reads)
        self.assertIsInstance(good, str)
        web_cache._persist_store(key, time.time(), {"read_ts": 1, "loops": []},
                                 good)
        self.assertTrue(os.path.exists(path),
                        "a witnessed body did not persist at all, so the "
                        "absence asserted below proves nothing")
        self.assertIsNotNone(
            web_cache.persist_load(key, 30, web_cache._ConstSnapshot(good)))
        os.unlink(path)
        with mock.patch.object(landreq, "_resolve_trunk_commit",
                               return_value=""):
            with m._lr_snapshot() as reads:
                self.collect(reads)
                bad = web_cache._witness_value(reads)
            self.assertIsNone(bad, "an unresolvable pin produced a witness "
                                   "VALUE, so the body would be certified")
            web_cache._persist_store(key, time.time(),
                                     {"read_ts": 2, "loops": []}, bad)
            self.assertFalse(os.path.exists(path),
                             "a body whose input could not be identified was "
                             "written to disk anyway")
            # AND THE RESTORE LEG. A body saved under a REAL witness in an
            # earlier server life must not come back while this life cannot
            # identify the input at all.
            web_cache._persist_store(key, time.time(),
                                     {"read_ts": 3, "loops": []}, good)
            self.assertTrue(os.path.exists(path))
            with m._lr_snapshot() as reads:
                self.assertIsNone(
                    web_cache.persist_load(key, 30, reads),
                    "a saved body was restored while the current input could "
                    "not be identified")

    # THE "FAILED PIN FOLLOWS THE MOVING REF" ARM IS DELETED WITH ITS READER
    # (task/2355). Its subject was `pinned = _resolved_ref(...) or trunk` inside
    # the lands walk: when the peel failed, that fallback handed three git reads
    # the moving NAME while the witness said UNRESOLVABLE. The lands leg makes no
    # git read at all now — it reads the ledger — so there is no fallback left to
    # take. The GENERAL property survives and is held by the arm directly above:
    # an unresolvable pin produces NO witness value, so the body neither persists
    # nor restores, whatever any leg did with the ref.

    # ── the ONE spelling of the peel, which IS the memo identity ─────────

    def test_every_trunk_peel_in_ONE_build_uses_the_CANONICAL_argv(self):  # noqa: VACUOUS_ASSERTION — both absences carry an unconditional positive control on the SAME observable one line earlier: assertTrue(peels) proves peels were spawned before their argv is compared, and the assertIsNot(_MISS, …) read proves the eviction key names a memo slot that EXISTS before forget() is asked to empty it
        """EXACT ARGV IS THE MEMO KEY, so a second spelling is a second spawn
        of a MOVING name — and `_landing_proofs` keeps its own trunk pin,
        which is bound to the build's snapshot ONLY because its argv is
        `_resolved_ref`'s to the character. task/915 recorded that binding as
        indirect and unmeasured; this measures it across every peel site at
        once, so a respelling on EITHER side reddens here rather than silently
        becoming two readings.

        The flags are complementary, not alternatives (measured on git 2.53.0,
        this repository): without `--quiet` a missing ref is rc 128 plus a
        fatal, indistinguishable from a broken repository; with
        `--end-of-options` placed AFTER the ref it is parsed as a revision and
        also dies 128. So the ORDER is part of the spelling."""
        seen = []
        real = landreq._git_spawn

        def spy(gitdir, args, input_text, env=None):
            seen.append(tuple(str(a) for a in args))
            return real(gitdir, args, input_text, env)

        owner = {"repo_id": self.repo_git}
        carrier = {"repo_id": self.repo_git, "reviewed_tip": self.side}
        from helm import web_land_model as m
        with mock.patch.object(landreq, "_git_spawn", side_effect=spy):
            with m._lr_snapshot() as reads:
                gitdir, trunk = reads.repo()
                reads.refsha(gitdir, trunk)
                m._lr_recent_lands()
                landreq._landing_proofs(self.repo_git)(owner, carrier)
        suffixed = [a for a in seen if a and a[-1].endswith("^{commit}")]
        # AN OBJECT-EXISTENCE PROBE IS NOT A REF PEEL, and only the second is
        # what this arm is about: the peel's exact argv IS the memo identity
        # binding a MOVING name to one snapshot. `cat-file -e <sha>^{commit}`
        # wears the same suffix while asking whether an object is present, and
        # its argument is an immutable id with no memo and nothing to drift.
        # It is excluded BY VERB and then held to the property that makes the
        # exclusion safe: every one of them names a full object id, so the
        # exclusion cannot become a hiding place for a moving name.
        existence = [a for a in suffixed if a[:2] == ("cat-file", "-e")]
        for probe in existence:
            self.assertRegex(probe[-1], r"\A[0-9a-f]{40}\^\{commit\}\Z",
                             "an existence probe peeled something that is not "
                             "a full object id: %r" % (probe,))
        peels = [a for a in suffixed if a not in existence]
        # UNCONDITIONAL POSITIVE CONTROL on the same observable as the
        # equality: peels really happened. A build that spawned none would
        # otherwise satisfy every claim below for free.
        self.assertTrue(peels, "no ref was peeled to a commit at all")
        self.assertEqual([landreq._REF_ARGV], sorted({a[:-1] for a in peels}),
                         "two spellings of one question: %r" % (peels,))
        # AND THE EVICTION KEY IS THE READ KEY. `projscope.forget` is silent
        # on a missing key, so a drifted literal would evict nothing and say
        # nothing while a failed probe stayed frozen for the projection.
        with projscope.scope():
            landreq._resolved_ref(self.repo_git, self.trunk_ref, {})
            key = landreq._peel_key(self.repo_git, self.trunk_ref)
            self.assertIsNot(projscope._MISS,
                             projscope._state().cache.get(key, projscope._MISS),
                             "the eviction key names no read that exists")
            projscope.forget(key)
            self.assertIs(projscope._MISS,
                          projscope._state().cache.get(key, projscope._MISS),
                          "forgetting the peel key evicted nothing")


class TheWitnessCoversTheAPPROVALTIERTest(LrApiBase):
    """THE POLICY IS AN INPUT, AND IT IS THE ONE THAT MOVES WITH NO FILE
    CHANGING (task/915 round two; task/757 round three).

    `landreq._lr` asks `dispatches.approval_tier(recipient, repo)` for every
    row whose verdict would otherwise be READY, and a reviewer the tier does
    not admit demotes that row to REVIEWED. The tier is resolved from an
    owner-revisable prior in the typed store PLUS the reviewer's MEASURED live
    runtime family — so it moves with no ledger write, no trunk move and no
    file on this box changing.

    THE FAILURE IS THE DANGEROUS DIRECTION: not a row that quietly gains a
    badge, but an OLD APPROVED BODY surviving the moment its approval stopped
    counting. The ledgers, trunk, the premise store and the attest sidecar all
    stay byte-identical, so the witness matched and the body restored FRESH.

    IT IS LIVE. The approval tier was revised 2026-08-11 (claude, codex,
    ds4pro, kimi and grok admitted; gemini-flash and codex-spark excluded at
    model level), and measured the same day on the live ledger: 19 distinct
    (reviewer, repository) keys carry an approve, 13 of which resolve UNKNOWN
    right now.

    ROUND THREE MOVED THE ANSWER FROM A SECOND ENUMERATION TO THE LENS. The
    previous cure walked the ledger itself to list approve-polarity rows and
    resolved each key BESIDE the build — correct on the paths it covered, and
    still two readings, which `projscope` could only bind for a HASHABLE key.
    `dispatches.tier_lens` now routes the body's own call into the read-set, so
    the resolution the row consumed IS the resolution the witness names."""

    def approved(self):
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        return row

    @staticmethod
    def resolved(recipient, repo):
        """One tier read taken THE WAY THE BODY TAKES IT — through the public
        seam, so whatever the lens does to it is what the row gets."""
        return dispatches.approval_tier(recipient, repo=repo)

    def test_the_witness_carries_the_TIER_THE_BODY_RESOLVED(self):
        """The term is the ANSWER and its ARGUMENTS are the body's own. A
        tidied recipient or a normalised repo asks a DIFFERENT question than
        the row asked, so the fingerprint would describe a state no badge was
        ever judged by."""
        from helm import web_land_model as m
        row = self.approved()
        with m._lr_snapshot() as reads:
            got = self.resolved(row["recipient"], row["repo_id"])
            witness = reads.witness()
        # UNCONDITIONAL POSITIVE CONTROL FIRST, on the same observable: the
        # seam really answered, and it answered the 2-tuple every caller of
        # `approval_tier` has always been handed.
        self.assertIsInstance(got, tuple)
        self.assertEqual(2, len(got))
        self.assertIsInstance(witness, str)
        tier = [t for t in witness.split("\x1e") if t.startswith("tier\x1f")]
        self.assertEqual([[row["recipient"], row["repo_id"]]],
                         [json.loads(t.split("\x1f")[1]) for t in tier],
                         "the witness asked a different question than the row")
        self.assertEqual([list(got)],
                         [json.loads(t.split("\x1f")[2]) for t in tier],
                         "the witness recorded a tier the body never got")

    def test_a_tier_that_goes_UNKNOWN_ages_an_APPROVED_body_OUT(self):  # noqa: VACUOUS_ASSERTION — the assertNotEqual has two unconditional positive controls on the same observable: the same tier read twice is asserted IDENTICAL, and the same saved body is asserted to restore YOUNG (body equality plus assertLess) while the tier still admits
        """THE OWNER-FACING HALF. A body computed while a reviewer was inside
        the tier is a claim that those rows may be merged. Once the tier can
        no longer say so, that body is a confident stale board — the one
        outcome this lane's design note calls WORSE than the timeout it
        replaces, because he can tell a timeout from a working board and
        cannot tell a frozen board from a live one."""
        from helm import web_land_model as m
        row = self.approved()
        key = "t915tier"
        self.addCleanup(web_cache._qstate.pop, key, None)
        path = web_cache._persist_path(key)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

        def witness_under(state, why):
            with mock.patch.object(dispatches, "_approval_tier_uncached",
                                   return_value=(state, why)):
                with m._lr_snapshot() as reads:
                    self.resolved(row["recipient"], row["repo_id"])
                    return reads.witness()

        admitted = witness_under("ok", None)
        lost = witness_under("unknown", "no family evidence")
        # POSITIVE CONTROLS on the same observable: the reading is a real
        # string, and the SAME tier read twice is IDENTICAL — so the inequality
        # below is the tier moving and not a witness that differs from itself.
        self.assertIsInstance(admitted, str)
        self.assertEqual(admitted, witness_under("ok", None),
                         "an unchanged tier witnessed differently twice")
        self.assertNotEqual(admitted, lost,
                            "the tier went UNKNOWN and the fingerprint did "
                            "not move — every READY badge in a saved body "
                            "would restore as current")
        web_cache._persist_store(key, time.time(), {"read_ts": 1, "loops": []},
                                 admitted)
        # UNCONDITIONAL POSITIVE CONTROL: the SAME saved body restores YOUNG
        # while the tier still admits, so the ageing below discriminates the
        # tier rather than describing a loader that refuses everything.
        fresh = web_cache.persist_load(key, 30,
                                       web_cache._ConstSnapshot(admitted))
        self.assertEqual({"read_ts": 1, "loops": []}, fresh[1])
        self.assertLess(time.time() - fresh[0], 30)
        stale = web_cache.persist_load(key, 30,
                                       web_cache._ConstSnapshot(lost))
        self.assertIsNotNone(stale, "the body was dropped rather than aged")
        self.assertGreater(time.time() - stale[0], 30,
                           "an approved body outlived its approval and was "
                           "served FRESH")

    def test_the_TIER_IS_RESOLVED_ONCE_and_NEVER_CERTIFIED_when_it_is_not(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone(blind) has its unconditional positive control one line above and on the SAME observable: assertIsInstance(witness, str) proves the identified reading of the same record DID project
        """REQUIRED ARM 1, THIRD MUTATION — and it is UNREPRESENTABLE now
        rather than caught. The arm states BOTH halves, because the half that
        does not hold is the one a reader must not be allowed to assume.

        WHAT HOLDS: an IDENTIFIED key resolves exactly once per snapshot and
        the body and the witness are that one resolution. Outside a snapshot the
        identical pair resolves twice, which is the control.

        WHAT DOES NOT HOLD, AND WHY THAT IS SAFE: an operand with no identity
        cannot be a memo key, so it is COMPUTED EACH TIME — `projscope`'s own
        law, kept deliberately, because a projection that caches under a
        colliding key is worse than one that cannot cache. Round three's defect
        was NOT the double compute; it was that one of those computes fed a
        SEPARATE WITNESS which then compared equal to another failure. There is
        no separate witness now, and a record that served an unidentified
        operand is BLIND — so the two answers cannot be reconciled into a
        certificate, they simply cannot produce one."""
        from helm import web_land_model as m
        calls = []

        def counting(recipient, repo=None):
            calls.append((recipient, repo))
            return ("ok", None)

        with mock.patch.object(dispatches, "_approval_tier_uncached",
                               side_effect=counting):
            with m._lr_snapshot() as reads:
                good_one = self.resolved("claude", "/x/.git")
                good_two = self.resolved("claude", "/x/.git")
                identified = len(calls)
                witness = reads.witness()
                self.resolved("claude", [])          # the hostile binding
                blind = reads.witness()
            # THE CONTROL THAT MAKES THIS A MEASUREMENT AND NOT A HABIT:
            # outside a snapshot the identical IDENTIFIED pair resolves TWICE,
            # so the count above is the lens and not a resolver that happens to
            # be called once.
            before_loose = len(calls)
            self.resolved("claude", "/x/.git")
            self.resolved("claude", "/x/.git")
        self.assertEqual([("ok", None), ("ok", None)], [good_one, good_two],
                         "the seam stopped answering, so nothing was measured")
        self.assertEqual(1, identified,
                         "an identified key resolved the tier twice inside one "
                         "snapshot: %r" % (calls,))
        self.assertEqual(2, len(calls) - before_loose,
                         "the unscoped pair did not resolve twice, so the "
                         "scoped count measures nothing")
        self.assertIsInstance(witness, str,
                              "the identified reading produced no witness, so "
                              "the None below discriminates nothing")
        self.assertIsNone(blind,
                          "a record that served an unidentified operand still "
                          "certified a body")

    def test_an_UNHASHABLE_repo_binding_leaves_NO_CACHE_ARTIFACT(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control runs FIRST and on the same observables: the identical build with a real repo binding DOES project a witness and DOES put a file on disk, so the absences below are the hostile operand and not a dead writer
        """REQUIRED ARM 3. `repo_id` is copied verbatim out of the
        ledger row and never re-validated, so `[]` arrives at the tier resolver
        as itself. It is not the name of a repository — not today, not in a
        saved record a later server life compares against — so the read that
        consumed it identifies nothing, and a snapshot that cannot identify what
        it served may not be persisted at all.

        STATED AS THE ARTIFACT, NOT AS A FLAG: no file on disk. That is the only
        thing a next server life can be misled by."""
        from helm import web_land_model as m
        key = "t757_hostile"
        path = web_cache._persist_path(key)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with mock.patch.object(dispatches, "_approval_tier_uncached",
                               return_value=("ok", None)):
            # UNCONDITIONAL POSITIVE CONTROL, FIRST AND ON BOTH OBSERVABLES.
            with m._lr_snapshot() as reads:
                self.resolved("claude", "/real/.git")
                good = reads.witness()
            self.assertIsInstance(good, str)
            web_cache._persist_store(key, time.time(),
                                     {"read_ts": 1, "loops": []}, good)
            self.assertTrue(os.path.exists(path),
                            "a witnessed body did not persist at all, so the "
                            "absence below proves nothing")
            os.unlink(path)
            with m._lr_snapshot() as reads:
                self.resolved("claude", ["not", "a", "path"])
                bad = reads.witness()
        self.assertIsNone(bad, "a hostile repo binding still produced a "
                               "comparable fingerprint")
        web_cache._persist_store(key, time.time(),
                                 {"read_ts": 2, "loops": []}, bad)
        self.assertFalse(os.path.exists(path),
                         "a body whose input could not be identified was "
                         "written to disk anyway")

    def test_the_body_reaches_the_read_set_WITHOUT_BEING_ASKED_TO(self):
        """THE LENS IS THE WIRING, and wiring is where a silent no-op lives.

        `approval_tier` is called from inside `_approval_refusal`, four frames
        below anything web_land_model wrote — nobody there can be relied on to
        remember a read-set exists. The lens routes it for the duration of the
        snapshot and restores the previous resolution on exit, so every write
        path (the land door, the close ladders, the send advisory) still
        resolves live exactly as before.

        MEASURED ON THE RECORD, NOT ON A PATCHED METHOD. Patching the instance
        attribute cannot see this: the lens captures the BOUND METHOD when the
        snapshot opens, so a later `mock.patch.object(reads, "tier", ...)` is
        installed behind it and the real resolver answers — which is how this
        arm first passed for the wrong reason. The record is the observable.
        """
        from helm import web_land_model as m
        with mock.patch.object(dispatches, "_approval_tier_uncached",
                               return_value=("ok", None)):
            with m._lr_snapshot() as reads:
                got = self.resolved("claude", "/x/.git")
                landed = [op for op, _okey in reads._reads if op == "tier"]
            # AND THE LENS IS UNINSTALLED ON EXIT, measured on a FRESH record:
            # a lens that leaked would answer another projection's rows.
            after = m._LrReadSet()
            with mock.patch.object(m, "_lr_reads", return_value=after):
                self.resolved("claude", "/x/.git")
        # UNCONDITIONAL POSITIVE CONTROLS: the seam answered, and it answered
        # THROUGH the read-set rather than around it.
        self.assertEqual(("ok", None), got)
        self.assertEqual(["tier"], landed,
                         "the body's tier call did not reach the read-set")
        self.assertEqual([], list(after._reads),
                         "the tier lens outlived its snapshot")

    def test_a_RAISING_tier_makes_the_entry_UNUSABLE(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone is preceded, unconditionally and on the same observable, by an assertIsInstance on a witness taken from the identical fixture with a working resolver: this shape demonstrably DOES produce a witness, so the None below is the raise
        """An instrument that failed says NOTHING about whether the input
        moved, so it may never contribute a term two builds could match on.
        `_approval_refusal`'s own fail-closed catch turns a raise into "unknown"
        FOR THE ROW — which is right, the row must still render — but for the
        WITNESS it has to turn into no witness at all."""
        from helm import web_land_model as m
        # POSITIVE CONTROL FIRST: this fixture does produce a witness.
        with mock.patch.object(dispatches, "_approval_tier_uncached",
                               return_value=("ok", None)):
            with m._lr_snapshot() as reads:
                self.resolved("claude", "/x/.git")
                self.assertIsInstance(reads.witness(), str)
        with mock.patch.object(dispatches, "_approval_tier_uncached",
                               side_effect=RuntimeError("proxy canary died")):
            with m._lr_snapshot() as reads:
                # THE ROW STILL GETS AN ANSWER. A build that died here would
                # blank the board, which is a worse surface than a stale one.
                self.assertEqual("unknown",
                                 self.resolved("claude", "/x/.git")[0])
                self.assertIsNone(reads.witness(),
                                  "a tier that could not be read at all still "
                                  "produced a comparable fingerprint")


# ── THE TWO STATIC SCANS: COVERAGE AIDS, NOT THE CURE ────────────────────
#
# READ THIS BEFORE READING A ZERO FROM EITHER OF THEM. Both are static readers
# of Python source text, and a static reader of Python's dynamic imports CANNOT
# BE COMPLETED — `_SEAM_UNSEEABLE` below enumerates what the seam one
# structurally cannot see, and that list does not end, because there is always
# one more spelling. Every clean result they give is EVIDENCE that a bypass is
# not visible in one file's text. Neither is a proof that no seam escapes the
# door, and `_SeamEvidence` refuses to be read as one: its result has no truth
# value, so it cannot be collapsed into a yes.
#
# THE INVARIANT IS HELD BY THE DOOR. `_LrReadSet._open_once` is the one place a
# file is opened, the `_Sealed` identity is what a body must be derived from,
# and the runtime arms on the real object are what kill a bypass of them —
# `test_the_object_door_REFUSES_a_moving_ref`,
# `test_a_LINK_SWAPPED_between_VALIDATION_and_OPEN_is_REFUSED`,
# `test_the_STAMP_comes_from_the_DESCRIPTOR_not_from_the_NAME` and the two
# O_NOFOLLOW arms. Delete both scans and the cure still stands. They are kept
# because they have caught real seams nobody had an arm for — the premise chain
# and the receipt index both sat outside the door for rounds — which is exactly
# what a coverage aid is for, and exactly as far as one goes.
#
# Round four shipped the seam scan with its must-hit SEEDED INSIDE THE READ-SET.
# That control could only ever demonstrate that the scan finds a forbidden seam
# it already enumerated; it said nothing about the seams it had never named, and
# the premise and ledger seams were live behind it the whole time. A must-hit
# drawn from the population you already cover is not a control, it is a mirror.
#
# So both scans are FUNCTIONS OVER SOURCE TEXT, which lets each arm point them
# at source they have never seen — a planted bypass in the exact seam that was
# missing — and require the catch BEFORE trusting a zero on the real modules.
# That is the control the round-four arm could not have: it is outside the shape
# being fixed.

# The seams that read a MUTABLE binding, and the FORM in which each one reads.
# "any": the name is a seam however it is called. "noargs": only the
# zero-argument form performs the read — `premise.verify_chain(recs)` is a pure
# function of records the read-set already served, while `premise.verify_chain()`
# opens the file itself. That distinction IS blocker (1)'s cure, and it is
# statically decidable, so the scan encodes it instead of trusting a comment.
_SEAMS = {
    ("landreq", "_resolved_ref"): "any",
    ("landreq", "_trunk_refs"): "any",
    ("landreq", "_git"): "any",
    ("landreq", "_git_spawn"): "any",
    ("landreq", "_landing_proof"): "any",
    ("landreq", "_receipt_rows"): "any",        # THE LEDGER SEAM
    ("landreq", "receipts_path"): "any",        # THE LEDGER SEAM
    ("dispatches", "approval_tier"): "any",
    ("dispatches", "_approval_tier_uncached"): "any",
    ("dispatches", "ledger_path"): "any",       # THE LEDGER SEAM
    ("dispatches", "attest_path"): "any",
    # THE REPO-SELECTION SEAM, and the one this list was BLIND TO for a round.
    # `home_repo_id` answers WHICH REPOSITORY this board reports on, it is the
    # read that REPLACED the registry route after that route measured as a
    # security defect (see `_LrReadSet._read_repo`), and it moves with no file
    # on this box changing. A consumer that re-derives it around the door is a
    # second reading of the one input this class exists to keep single — the
    # exact shape, on the exact read, that this lane is about. It was EXCLUDED
    # from this list on a rationale that measured FALSE; the correction is
    # written out at `_SEAM_PROBE`, and removing this line again reddens
    # `test_the_REPO_SELECTION_READ_IS_FORBIDDEN_OUTSIDE_THE_DOOR`.
    ("dispatches", "home_repo_id"): "any",      # THE REPO-SELECTION SEAM
    ("premise", "chain_records"): "any",        # THE PREMISE SEAM
    ("premise", "verify_chain"): "noargs",      # THE PREMISE SEAM
    ("os", "stat"): "any",
    # THE READERS THE TWO SEALED DOORS REPLACED, listed so the respelling is
    # forbidden and not merely unused. `receipt_rows` no longer calls
    # `landreq._receipt_rows`; it opens the ledger itself. Anyone reaching the
    # same bytes through `eventledger` by PATH would be a second door onto one
    # input — the shape whose two answers this class exists to prevent.
    ("eventledger", "checked_events"): "any",
    ("eventledger", "latest_checked"): "any",
    ("eventledger", "latest"): "any",
    ("eventledger", "events"): "any",
    # AND THE RESOLVER ITSELF, which is what MOVES underneath both doors. A
    # consumer that re-derives a helm-home path is re-running the resolution the
    # record already pinned, which is a review's TOCTOU with the reopen spelled
    # out longhand.
    ("home", "global_dir"): "any",
}

# THE DOOR IS NOT ONLY THE CLASS. `_lr_ledger_paths` and `_lr_input_path` are
# the read-set's OWN readers — `ledger_paths` and `input_path` serve through
# them — so the ledger and sidecar path calls inside them are the door doing its
# job, not a consumer walking around it. Named explicitly, as a narrow
# allowlist, because "any module-level function" would exempt every bypass.
_SEAM_DOORS = ("_LrReadSet", "_lr_ledger_paths", "_lr_input_path")


def _seam_arg_names(args):
    """Every parameter name in an `ast.arguments`, in one place."""
    return [a.arg for a in (list(args.posonlyargs) + list(args.args)
                            + list(args.kwonlyargs) + [args.vararg, args.kwarg])
            if a is not None]


class _SeamBindings:
    """What a bare name COULD name at one node — BY SCOPE, THEN BY ORDER.

    THE SURFACE SPELLING IS NOT THE SEAM. `import premise as p` and
    `from premise import chain_records` both reach `premise.chain_records`, and
    a scan that matched `Attribute(Name('premise'), 'chain_records')` saw
    neither. A review ruled that a BLOCKER after I had proposed it as an accepted
    limit, and the ruling is right: a bypass scan whose evasion is a rename is
    not a capability check, it is a spelling check.

    AND THE MAP THAT FIXED THAT WAS ONE FLAT DICT FOR THE WHOLE FILE, keyed by
    bare name and written by `ast.walk`, which is neither source order nor
    scope. Its docstring claimed the collapse "can only ever ADD a hit". THAT
    WAS FALSE IN BOTH DIRECTIONS, and the two directions are the same bug seen
    from opposite ends:

      * real alias FIRST, a harmless rebinding of the same name SECOND — the
        rebinding overwrote the alias and the genuine bypass vanished, so the
        scan answered `outside=[] unresolved=[]`, WHICH IS EXACTLY WHAT A CLEAN
        MODULE ANSWERS;
      * the same two lines in the OTHER ORDER — the real alias survived, and the
        harmless call site was then resolved THROUGH IT and reported as a hit it
        never was.

    So one arbitrary winner produced a silent miss in one order and a confident
    false accusation in the other, and no arm written in one order could see the
    other. Both are cured here, and they are cured by DIFFERENT means because
    only one of them is statically decidable:

    SCOPE IS DECIDABLE, SO IT IS RESOLVED. A `from . import landreq` inside a
    function binds inside THAT function; a parameter named `recs` shadows a
    module-level `recs` for that function's whole body; a method does not see
    its class body's names. That is Python, it is decidable from the tree, and
    it makes a function-local rebinding stop erasing a module-level alias.

    ORDER IS ONLY DECIDABLE WITHIN ONE SCOPE, SO OUTSIDE ONE IT IS REFUSED. A
    use in the SAME scope as the bindings is straight-line: the last binding at
    or before that position wins. A use in an INNER scope runs LATER — a
    function body sees whatever the module-level name was rebound to by call
    time, which no static reader knows. There the honest answer is not to pick a
    winner at all: every distinct binding is returned as a CANDIDATE, and
    `_seam_evidence` reports a name whose candidates DISAGREE about whether the
    call reaches a seam as UNRESOLVABLE. That is this scan's own standing rule —
    an unresolvable call is not a clean scan — applied to the one place the
    resolver had been quietly breaking it."""

    def __init__(self, tree, package):
        self._tree = tree               # keeps every id() key alive
        self._package = package
        self._binds = [{}]              # scope -> {name: [(pos, target)]}
        self._kind = ["module"]
        self._chain = {}                # id(node) -> scopes, innermost first
        self.count = 0                  # import bindings, for the reach report
        self._descend(tree, (0,))

    # ── construction ────────────────────────────────────────────────────
    def _open(self, chain, kind):
        self._binds.append({})
        self._kind.append(kind)
        return (len(self._binds) - 1,) + chain

    def _bind(self, scope, name, target, node):
        """`target` is a dotted module path, or None for a VALUE binding.

        A value binding is recorded rather than ignored because SHADOWING IS
        THE POINT: `def f(recs)` must stop `recs` resolving to an outer
        `from premise import chain_records as recs`."""
        self._binds[scope].setdefault(name, []).append(
            ((node.lineno, node.col_offset), target))
        if target is not None:
            self.count += 1

    def _imports(self, node):
        """[(bound name, dotted target)] for one Import/ImportFrom."""
        if isinstance(node, ast.Import):
            # `import a.b.c` binds `a`; `import a.b.c as x` binds x -> a.b.c.
            return [(alias.asname or alias.name.split(".")[0],
                     alias.name if alias.asname else alias.name.split(".")[0])
                    for alias in node.names]
        base = node.module or ""
        if node.level:
            parts = self._package.split(".") if self._package else []
            parts = parts[:len(parts) - (node.level - 1)]
            base = ".".join([p for p in parts + [base] if p])
        return [(alias.asname or alias.name,
                 (base + "." + alias.name) if base else alias.name)
                for alias in node.names
                if alias.name != "*"]   # `*` is reported at the call site

    def _descend(self, node, chain):
        self._chain[id(node)] = chain
        scope = chain[0]
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for name, target in self._imports(node):
                self._bind(scope, name, target, node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.Lambda, ast.ClassDef)):
            # THE BODY IS THE NEW SCOPE; EVERYTHING ELSE UNDER THIS NODE IS
            # NOT. Decorators, defaults, annotations, bases and class keywords
            # are all evaluated where the `def` or `class` is WRITTEN, so
            # resolving them inside the new scope would let a parameter shadow
            # the alias its own default came from.
            #
            # AND THE SPLIT IS "BODY vs THE REST", NOT A LIST OF NODE KINDS. A
            # kind this walker forgot to name would get no scope at all, and an
            # unscoped node resolves to nothing — a silent miss wearing an
            # answer's clothes, which is the class of defect being cured here.
            lam = isinstance(node, ast.Lambda)
            body = [node.body] if lam else list(node.body)
            if not lam:
                self._bind(scope, node.name, None, node)
            inner = self._open(chain,
                               "class" if isinstance(node, ast.ClassDef)
                               else "function")
            if not isinstance(node, ast.ClassDef):
                for name in _seam_arg_names(node.args):
                    self._bind(inner[0], name, None, node)
            in_body = {id(stmt) for stmt in body}
            for sub in ast.iter_child_nodes(node):
                self._descend(sub, inner if id(sub) in in_body else chain)
            return
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            self._bind(scope, node.id, None, node)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            self._bind(scope, node.name, None, node)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                self._bind(scope, name, None, node)
        for sub in ast.iter_child_nodes(node):
            self._descend(sub, chain)

    # ── resolution ──────────────────────────────────────────────────────
    def candidates(self, node, name):
        """Every dotted target `name` could carry AT `node`, or [].

        A `None` entry means a VALUE was bound there, so an attribute off it is
        not a module attribute. More than one entry means the binding is
        AMBIGUOUS at this point and the caller must report rather than decide."""
        chain = self._chain.get(id(node))
        if chain is None:
            return []
        # A method does not see its class body's names, so a class scope is
        # only ever consulted when the use is IN it.
        lookup = [chain[0]] + [s for s in chain[1:] if self._kind[s] != "class"]
        pos = (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
        for depth, scope in enumerate(lookup):
            binds = self._binds[scope].get(name)
            if not binds:
                continue
            if depth == 0:
                # SAME SCOPE: straight-line, so the last binding at or before
                # this position is the one in effect. Nothing before it means
                # the name is not yet bound here at all.
                return [t for p, t in binds if p <= pos][-1:]
            # AN OUTER SCOPE runs to completion before this body ever does, so
            # position decides nothing. Every distinct binding is a candidate.
            out = []
            for _pos, target in binds:
                if target not in out:
                    out.append(target)
            return out
        return []


def _seam_aliases(tree, package):
    """The binding resolver for `tree`. See `_SeamBindings`."""
    return _SeamBindings(tree, package)


def _seam_bound(tree):
    """Every name this module BINDS to a value rather than to a module.

    THE DISCRIMINATOR THAT KEEPS THE UNRESOLVABLE REPORT USABLE. `reads.stat(...)`
    and `os.stat(...)` are the same shape; the first is a local object's method
    and the second is the seam. A name assigned, bound as a parameter, defined
    as a function or caught as an exception is a VALUE — so an attribute off it
    is not a module attribute, and the scan says nothing. A name that is neither
    an import alias nor a local binding is one the scan CANNOT see (this module
    splats `vars(helm.web)` into its globals), and that is reported."""
    names = set()
    for node in ast.walk(tree):
        args = getattr(node, "args", None)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) \
                and isinstance(args, ast.arguments):
            names.update(a.arg for a in (list(args.posonlyargs) + list(args.args)
                                         + list(args.kwonlyargs)
                                         + [args.vararg, args.kwarg])
                         if a is not None)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    return names


def _dotted_name(node):
    """The dotted spelling of a Name/Attribute chain, as a list, or None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    parts.reverse()
    return parts


def _heads(binds, node):
    """The candidate bindings of a dotted expression's HEAD, at that node.

    [] when the expression is not a dotted name at all, or when nothing in
    scope binds its head."""
    parts = _dotted_name(node)
    return binds.candidates(node, parts[0]) if parts else []


def _seam_target(parts, target, seams):
    """(seam key, rule) for ONE candidate binding of `parts[0]`, or (None, None).

    RESOLVED TARGET, NOT SURFACE SPELLING. The head is replaced by whatever the
    import bound it to and the seam matches when the FUNCTION is the last
    component and the MODULE appears anywhere before it — so `helm.landreq`,
    `landreq`, a `landreq as lr` alias and `from .premise._chain import
    chain_records` all land on the same key.

    ONE CANDIDATE AT A TIME, because a name can have more than one binding in
    scope and this function must not be the place that silently picks a winner
    — that pick is the defect `_SeamBindings` documents. The caller asks this
    of EVERY candidate and reports a disagreement."""
    if target is None:
        return None, None
    comps = target.split(".") + parts[1:]
    for key, rule in seams.items():
        if key[1] == comps[-1] and key[0] in comps[:-1]:
            return key, rule
    return None, None


_ACQUIRERS = ("getattr", "__import__", "import_module")


def _is_acquirer(parts, targets):
    """Does this resolved callee name a DYNAMIC-ACQUISITION primitive?

    ANY CANDIDATE IS ENOUGH. This branch REPORTS rather than resolves — its
    output goes into `unresolved`, which already means "the scan cannot decide
    this" — so an ambiguous binding whose other spelling is a primitive is
    named rather than dropped. Over-naming here costs a caveat; under-naming
    costs the silence a bypass hides in.

    THE SAME RULING AS `_seam_target`, APPLIED WHERE IT WAS NEVER APPLIED. The
    seam half of this scan resolves a call through the import bindings, because
    A review ruled that matching the surface spelling makes the check a spelling
    check whose evasion is a rename. The ACQUISITION half then matched the last
    dotted component against three literal strings — the exact defect, one
    branch away from its own cure.

    MEASURED, on the live scanner, before this existed: of ten spellings of the
    three primitives, seven were caught and THREE were missed, and all three
    misses were one shape — the renamed direct import. `from importlib import
    import_module as imp` then `imp("helm.landreq")` came back with an EMPTY
    unresolved list, and so did `__import__ as bi`; `getattr as ga` survived only
    as a "computed callee" report that names no module, and only because the
    plant happened to call the result immediately. A review reported the
    import_module cell; it was three cells of one hole.

    Resolving first closes all three at once and costs nothing the scan was not
    already paying: the candidates are the ones `_SeamBindings` already
    resolved, and an unimported name (a bare builtin `getattr`) simply resolves
    to itself."""
    if not parts:
        return False
    mods = [t for t in targets if t is not None]
    if not mods:
        return parts[-1] in _ACQUIRERS
    return any((t.split(".") + parts[1:])[-1] in _ACQUIRERS for t in mods)


# ── WHAT THIS SCAN STRUCTURALLY CANNOT SEE ───────────────────────────────
#
# Written HERE, next to the code, and RETURNED WITH EVERY RESULT, because a
# limitation that lives in a review comment or a dispatch body is a limitation
# nobody inherits. Five of this lane's eleven review rounds were one more
# spelling of one of these; the list is the answer to that regress, and ADDING
# SPELLINGS IS NOT. This is a coverage aid, not the guarantee.
_SEAM_UNSEEABLE = (
    "eval/exec, and any seam named by a string this scan never runs",
    "a name whose binding cannot be decided statically — a module-level name "
    "rebound anywhere is AMBIGUOUS inside every function body, and is reported "
    "as unresolvable rather than resolved",
    "indirection through a THIRD module: this reads one file at a time, so a "
    "helper that calls the seam on this file's behalf is invisible here",
    "`from X import *`, which binds names no static reader can enumerate",
    "a module fetched at runtime into a local, which is caught at the "
    "ACQUISITION and never at the call site",
    "a name bound to a value ANYWHERE in the file, which silences the "
    "unresolvable report everywhere in it — `_seam_bound` is deliberately "
    "module-wide, and that is a suppressor, not a proof",
    "any file other than the two this lane points it at",
)


class _SeamEvidence(collections.namedtuple(
        "_SeamEvidence", "inside outside unresolved reach blind_spots")):
    """THE RESULT OF A HEURISTIC, AND IT REFUSES TO BE READ AS A VERDICT.

    A clean scan is EVIDENCE that no seam call is visible in this file's source
    text. It is NOT a guarantee that no consumer reaches a seam — see
    `blind_spots`, which every result carries.

    THE REASON IT CAN NEVER BE MORE THAN EVIDENCE, IN ONE SENTENCE: THIS SCAN
    NEVER OWNS THE RUNTIME OBJECT. It reads text, so it can only ever speak
    about the spellings it can enumerate, and the next case is always outside
    the enumerated set. That is why the answer to a miss is a wider caveat and
    not a wider scan, and why a heuristic may INFORM a reviewer and may never
    CERTIFY completeness.

    THE GUARANTEE LIVES SOMEWHERE
    ELSE AND SOMEWHERE PROVABLE: the door. `_LrReadSet._open_once` is the single
    place a file is opened, the `_Sealed` identity is what a body must be built
    from, and the arms that kill a bypass of it are runtime arms on the real
    object — `test_the_object_door_REFUSES_a_moving_ref`,
    `test_a_LINK_SWAPPED_between_VALIDATION_and_OPEN_is_REFUSED`,
    `test_the_STAMP_comes_from_the_DESCRIPTOR_not_from_the_NAME`,
    `test_the_NOFOLLOW_DEFAULT_is_the_guard_and_not_just_the_call_site` and
    `test_a_PLATFORM_WITHOUT_O_NOFOLLOW_REFUSES_rather_than_FOLLOWS`. Delete
    this scan and the cure still stands; delete the door and nothing does.

    SO THERE IS NO TRUTH VALUE ON THIS OBJECT. `if evidence:`, `assertTrue(
    evidence)` and `assert scan(...)` all raise, by construction, because the
    one thing a reader must never do with this result is collapse it to a
    yes/no. Read `.outside`, `.unresolved` and `.reach`, and say what they
    support — `.caveat()` spells out the sentence they support."""

    __slots__ = ()

    def __bool__(self):
        raise TypeError(
            "a seam scan is a HEURISTIC and its result has no truth value: a "
            "clean scan is evidence, not a guarantee. Read .outside / "
            ".unresolved / .reach and state what they support; the guarantee "
            "is the door (_LrReadSet._open_once + the _Sealed identity), not "
            "this scan. " + self.caveat())

    def caveat(self):
        """The sentence a caller is entitled to say about this result."""
        return ("this scan cannot see: " + "; ".join(self.blind_spots)
                + ". A zero here is coverage, not proof.")


def _seam_evidence(source, label, seams=_SEAMS, doors=_SEAM_DOORS,
                   package="helm"):
    """EVIDENCE about forbidden seam CALLS in `source`. NOT A PROOF.

    -> `_SeamEvidence(inside the door, outside it, UNRESOLVABLE outside it,
    reach, blind_spots)`.

    WHAT A ZERO HERE MEANS, EXACTLY: no call reaching a listed seam is VISIBLE
    IN THIS FILE'S SOURCE TEXT to a per-file static reader. It does NOT mean no
    seam escapes the door. `_SEAM_UNSEEABLE` — returned as `blind_spots` on
    every result, and reproduced in `_SeamEvidence`'s docstring — enumerates
    what this form structurally cannot see, and that list cannot be finished:
    static resolution of Python's dynamic imports is not completable, so each
    round that adds one more spelling buys one round of quiet and then another
    spelling. The list is the honest end of that regress.

    THE INVARIANT IS PROTECTED BY THE DOOR, NOT BY THIS FUNCTION.
    `_LrReadSet._open_once` and the `_Sealed` identity are what make a body
    built off a moving read unrepresentable, and the arms named in
    `_SeamEvidence` are what kill a bypass of them at runtime on the real
    object. This scan is a coverage aid ON TOP of a cure that stands without
    it: it is worth keeping because it has caught real seams nobody had an arm
    for — the premise chain and the receipt index both sat outside the door for
    rounds — but a green scan certifies nothing on its own.

    AN UNRESOLVABLE CALL IS NOT A CLEAN SCAN. A form this cannot decide —
    `getattr(mod, name)()`, a bare call to a name no import in this file binds,
    a name whose binding is AMBIGUOUS at the call site — is reported rather than
    skipped, because a skipped form is indistinguishable from a module with no
    bypass in it, which is the exact failure this whole round is about.
    Unresolvable forms INSIDE a door are not reported: the door is the
    sanctioned reader of moving inputs, and `_lr_ledger_paths` really does probe
    `gate.receipts_path` with `getattr` because that lane has not landed.

    AND IT REPORTS ITS REACH, so a zero is readable. `reach` counts the calls
    walked, the calls whose head RESOLVED to a module through the bindings, and
    the import bindings found: a file with zero resolvable calls is visibly zero
    rather than silently clean."""
    tree = ast.parse(source)
    binds = _seam_aliases(tree, package)
    bound = _seam_bound(tree)
    fn_names = {key[1] for key in seams}
    mod_names = {key[0] for key in seams}
    door_nodes = {id(x) for n in ast.walk(tree)
                  if isinstance(n, (ast.ClassDef, ast.FunctionDef))
                  and n.name in doors for x in ast.walk(n)}
    inside, outside, unresolved = [], [], []
    calls = resolved = 0
    called = set()

    def where(node):
        return "%s:%d" % (label, node.lineno)

    for node in ast.walk(tree):
        # DYNAMIC ACCESS NAMING A SEAM, called or not: the one evasion no
        # resolver can follow, so it is named rather than missed.
        #
        # THE MODULE HALF IS AS IMPORTANT AS THE FUNCTION HALF, and leaving it
        # out was a real hole rather than a fastidiousness: `m =
        # importlib.import_module("helm.landreq")` binds a MODULE to a local
        # name, after which `m._git(...)` is indistinguishable from any other
        # object's method and the value-vs-module discriminator below correctly
        # says nothing about it. Catching the ACQUISITION is the only place that
        # form can be seen at all, so a string naming a seam module is reported
        # exactly like a string naming a seam function.
        if isinstance(node, ast.Call) \
                and _is_acquirer(_dotted_name(node.func),
                                 _heads(binds, node.func)):
            const = {a.value for a in node.args
                     if isinstance(a, ast.Constant) and isinstance(a.value, str)}
            named = const | {part for text in const for part in text.split(".")}
            dynamic = len(node.args) > 1 and not isinstance(node.args[1],
                                                            ast.Constant)
            if (named & (fn_names | mod_names)) \
                    or (dynamic and node.args
                        and any(t is not None
                                for t in _heads(binds, node.args[0]))):
                if id(node) not in door_nodes:
                    unresolved.append("%s dynamic access names a seam"
                                      % where(node))
        if not isinstance(node, ast.Call):
            continue
        calls += 1
        parts = _dotted_name(node.func)
        if parts is None:
            # A COMPUTED CALLEE. Only worth reporting when the expression names
            # a seam function somewhere inside it; otherwise it is a closure or
            # a factory and this scan has nothing to say about it.
            spelled = {n.value for n in ast.walk(node.func)
                       if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            spelled |= {n.attr for n in ast.walk(node.func)
                        if isinstance(n, ast.Attribute)}
            if (spelled & fn_names) and id(node) not in door_nodes:
                unresolved.append("%s computed callee names a seam" % where(node))
            continue
        called.add(id(node.func))
        cands = [t for t in _heads(binds, node.func) if t is not None]
        if cands:
            resolved += 1
            # EVERY CANDIDATE IS ASKED, AND A DISAGREEMENT IS REPORTED. When
            # one binding is in scope the answer is a verdict; when several are
            # and they say different things about this call, the honest output
            # is `unresolved`, not whichever one a dict happened to keep. That
            # dict is what made a rebind erase a real alias in one source order
            # and manufacture a hit in the other.
            found = {_seam_target(parts, target, seams) for target in cands}
            live = {(key, rule) for key, rule in found if key is not None
                    and not (rule == "noargs"
                             and (node.args or node.keywords))}
            if len(found) == 1 and len(live) == 1:
                key = live.pop()[0]
                (inside if id(node) in door_nodes else outside).append(
                    "%s %s.%s" % (where(node), key[0], key[1]))
            elif live and id(node) not in door_nodes:
                unresolved.append(
                    "%s %r is bound %d different ways in scope, one of which "
                    "reaches a seam" % (where(node), parts[0], len(cands)))
        elif parts[-1] in fn_names and parts[0] not in bound \
                and id(node) not in door_nodes:
            unresolved.append("%s no import in this file binds %r"
                              % (where(node), parts[0]))

    # A SEAM HANDED AWAY UNCALLED is the shape the cured code itself used to
    # have: `self._serve("premisechain", (path,), premise.chain_records)` passes
    # the seam as a value for someone else to call, which a call-only scan sees
    # as nothing at all. It is reported as unresolvable rather than as a hit,
    # because where it is invoked is exactly what cannot be decided here.
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Name, ast.Attribute)) \
                or id(node) in called or id(node) in door_nodes:
            continue
        parts = _dotted_name(node)
        if parts is None or len(parts) < 1:
            continue
        if any(_seam_target(parts, target, seams)[0] is not None
               for target in _heads(binds, node)):
            unresolved.append("%s seam handed away uncalled" % where(node))

    return _SeamEvidence(inside, outside, sorted(set(unresolved)),
                         {"calls": calls, "resolved": resolved,
                          "aliases": binds.count},
                         _SEAM_UNSEEABLE)


_RESOLVERS = ("_resolved_ref", "_resolve_trunk_commit")


def _falls_back_from_a_sha(node, resolvers=_RESOLVERS, suffix="_sha"):
    """Is this expression a RESOLVED COMMIT standing in an `or`?"""
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) \
            else getattr(func, "id", None)
        return name in resolvers
    return isinstance(node, ast.Name) and node.id.endswith(suffix)


def _sha_fallback_scan(source, label, **kw):
    """Every `<resolved sha> or <something that is not empty>` in `source`.

    THE SHAPE, NOT THE SPELLING. A failed peel that falls back to a ref NAME
    puts the moving binding back into the argv while the witness records a
    failure — and two failures compare EQUAL, so the record reads as a world
    that did not move while the body was answered about two trunks. Round two
    cured that at one call site by hand; this is the class.

    `or ""` IS THE CURE, NOT THE DEFECT, and the scan must not confuse them: an
    empty fallback REFUSES (the caller's `_sha("")` or `if not ref` then
    declines), while a fallback to a NAME substitutes. So a falsy constant tail
    is not a hit, and that discrimination is itself controlled below."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)):
            continue
        if not _falls_back_from_a_sha(node.values[0], **kw):
            continue
        tail = node.values[-1]
        if isinstance(tail, ast.Constant) and not tail.value:
            continue
        hits.append("%s:%d" % (label, node.lineno))
    return hits


class OneReadSetTwoProductsTest(LrApiBase):
    """THE SHAPE FIX ITSELF (task/757 round four).

    Three cure rounds each closed one door into one hazard and a fourth door
    appeared, because the witness was COMPUTED ALONGSIDE the body rather than
    DERIVED FROM it. These arms measure the properties that make the class
    unrepresentable rather than the three instances that were caught."""

    # THE PER-FILE INSTRUMENT CONTROL. Round four's must-hit lived inside
    # `_LrReadSet`, which only web_land_model.py HAS — so web_land.py could have
    # failed to parse, failed to walk, or been the wrong file entirely, and the
    # arm would still have gone green off its sibling's hit. Each file therefore
    # names a call IT REALLY MAKES, and the scan must find that before the same
    # scan's zero on the same file is read as a finding. The probe MUST name a
    # call the file makes TODAY, so it moves with the code: a probe naming a
    # call that has been removed makes this arm fail LOUDLY rather than let the
    # zero below be read off a file the scan never really searched, which is
    # exactly the service it is here to perform.
    #
    # THE PROBE IS NOT A REASON TO EXCLUDE A SEAM, AND THIS COMMENT USED TO SAY
    # IT WAS. Its exact words were: "`home_repo_id` is deliberately NOT in
    # `_SEAMS` — the probe has to be a real call that is not itself forbidden,
    # or the assertEqual([], got.outside) below could never pass." THAT WAS
    # FALSE, and a probe measured it false with this arm's own accessors before
    # ruling the exclusion a blocker. Two independent facts each break it:
    #
    #   * the probe scan below passes its OWN `seams={probe: "any"}` and
    #     `doors=()`, so what `_SEAMS` contains does not reach it at all; and
    #   * the real call — `dispatches.home_repo_id()` in
    #     `_LrReadSet._read_repo` — sits INSIDE `_LrReadSet`, which is a door,
    #     so with the seam forbidden it classifies INSIDE and `got.outside` is
    #     still []. Measured on both files: outside=[] unresolved=[].
    #
    # So the seam CAN be forbidden and the assertion CAN pass, together, which
    # is the whole point: it is the read-set's own reader, and the door is what
    # makes it legal. The exclusion bought nothing and cost the guarantee —
    # while it stood, a module-level consumer of the canonical repo-selection
    # read was invisible here, so an edit moving THE read this lane is about
    # out of the read-set would have kept this arm green.
    #
    # WHAT IS ACTUALLY TRUE OF A PROBE is narrower: it must be a call the file
    # really makes, and it should be a DIFFERENT call from the one the arm most
    # cares about. A must-hit drawn from the population under test is a mirror
    # rather than a control — the lesson `_SEAMS`'s own header records — so the
    # repo-selection read is now the SUBJECT and `landreq._origin_configured`
    # is the probe: the other movable input `_read_repo` names, one line
    # further down, not forbidden, and load-bearing for which trunk answers.
    # web_land_model's probe was `dispatches._repo_info` before that, until the
    # read-set stopped resolving repository identity through the registry
    # (a measured security defect — see `_read_repo`).
    _SEAM_PROBE = {"web_land_model.py": ("landreq", "_origin_configured"),
                   "web_land.py": ("landreq", "card")}

    def test_NO_SEAM_CALL_IS_VISIBLE_IN_EITHER_FILES_SOURCE_TEXT(self):  # noqa: VACUOUS_ASSERTION — every empty-list assertion is preceded, per file and on the same instrument, by an unconditional assertTrue on a probe seam that file really calls; the zero is only read after the scan is shown to find something in THAT file
        """WHAT THIS ARM CAN SUPPORT, AND ITS NAME NOW SAYS ONLY THAT.

        It used to be called `test_NO_CONSUMER_reads_git_or_the_tier_OUTSIDE_
        the_read_set`, and that name claimed a universal a per-file static
        reader cannot establish. What it measures is narrower and still worth
        measuring: NO CALL REACHING A LISTED SEAM IS VISIBLE, TO A PER-FILE
        STATIC READER, IN THE SOURCE TEXT OF helm/web_land.py AND
        helm/web_land_model.py. `_SEAM_UNSEEABLE` — asserted below, on the
        result — is the list of ways a real consumer could sit outside that
        sentence, and it cannot be completed.

        THE CAPABILITY IS ENFORCED BY THE DOOR, NOT BY THIS ARM. The read-set
        is a capability and a capability with a side door is decoration, but
        what closes the side door is `_LrReadSet._open_once` plus the `_Sealed`
        identity, and what kills a bypass of THOSE is the runtime arms further
        down this class — `test_the_object_door_REFUSES_a_moving_ref`,
        `test_a_LINK_SWAPPED_between_VALIDATION_and_OPEN_is_REFUSED`,
        `test_the_STAMP_comes_from_the_DESCRIPTOR_not_from_the_NAME`,
        `test_the_NOFOLLOW_DEFAULT_is_the_guard_and_not_just_the_call_site`.
        This arm is coverage on top of that cure: it catches a seam nobody
        wrote an arm for, which is real value and is not a proof.

        WHY IT KEEPS EARNING ITS PLACE. The seam list includes the premise and
        the ledger because round four's scan enumerated git and the tier, went
        green, and two whole-file CONTENT reads sat outside the door the entire
        time. A scan is only as good as its forbidden list, so the list is the
        finding — and a list is exactly the thing that cannot be proven whole.

        EVERY ZERO IS READ ONLY AFTER THE REACH IS ASSERTED — the calls walked,
        the calls whose head RESOLVED, and the import bindings found — because a
        resolver that silently resolved nothing produces exactly this zero."""
        import helm.web_land as wl
        import helm.web_land_model as wlm
        for mod in (wlm, wl):
            base = os.path.basename(mod.__file__)
            with self.subTest(module=base):
                with open(mod.__file__, encoding="utf-8") as fh:
                    source = fh.read()
                probe = self._SEAM_PROBE[base]
                seen = _seam_evidence(source, base, seams={probe: "any"}, doors=())
                self.assertTrue(seen.outside,
                                "the scan found no %s.%s in %s, so it did not "
                                "really read this file and its zero below "
                                "means nothing" % (probe + (base,)))
                got = _seam_evidence(source, base)
                # THE REACH IS THE INSTRUMENT CONTROL, and it is asserted
                # BEFORE any absence is read. A scan that walked no call, or
                # walked calls and resolved none of them through the alias map,
                # is not a clean module — it is a broken resolver.
                self.assertTrue(got.reach["calls"] and got.reach["resolved"]
                                and got.reach["aliases"],
                                "the scan resolved nothing in %s, so its zeros "
                                "below are the resolver failing rather than "
                                "the file being clean: %r" % (base, got.reach))
                self.assertEqual([], got.outside,
                                 "a consumer reads a moving input around the "
                                 "read-set, so its reading can never enter the "
                                 "witness: %r" % (got.outside,))
                self.assertEqual([], got.unresolved,
                                 "a call outside the read-set cannot be "
                                 "resolved to a target at all, so this file's "
                                 "zero above is a claim the scan cannot "
                                 "support: %r" % (got.unresolved,))
                # AND THE ZERO CARRIES ITS OWN CAVEAT OUT OF THIS ARM. The
                # blind spots ride on the result, so a reader who reaches this
                # green through a failure message, a repr or a debugger sees
                # what the two zeros above do NOT cover.
                self.assertTrue(got.blind_spots,
                                "the result stopped declaring what it cannot "
                                "see, which is how a heuristic's zero starts "
                                "getting read as a guarantee again")
                self.assertIn("not proof", got.caveat())

    # THE BYPASS THE EXCLUSION WAS BLIND TO, in a review's own construction:
    # the canonical repo-selection read, at MODULE level, APPENDED TO A COPY OF
    # THE REAL web_land_model SOURCE. Two properties earn that shape over a
    # freestanding snippet — the canonical call and the bypass are then in ONE
    # scan of ONE text, so "forbidden here, legal there" is a single
    # measurement rather than two; and the surrounding file is the real one, so
    # nothing about the plant is arranged to be findable.
    #
    # NOTHING IS WRITTEN. `_seam_evidence` takes source TEXT, so the planted
    # "tree" is a str and the real file is opened read-only. An arm that plants
    # by editing a file plants in the operator's tree, and a probe inherits
    # none of an arm's isolation.
    _REPO_PLANT = ("\n\n"
                   "from helm import dispatches as d\n"
                   "\n"
                   "\n"
                   "def bypass_outside_read_set():\n"
                   "    return d.home_repo_id()\n")

    def test_the_REPO_SELECTION_READ_IS_FORBIDDEN_OUTSIDE_THE_DOOR(self):
        """A DOCUMENTED EXCLUSION WITH NO ARM THAT REDDENS WHEN IT IS REMOVED
        is the form of a guarantee without the substance, and that is what
        `home_repo_id`'s absence from `_SEAMS` was. Nothing complained when the
        exclusion was written, nothing would have complained if a later reader
        widened it, and the comment justifying it READ AS AUTHORITATIVE while
        being false. This arm is the substance: it converts that comment into a
        check.

        WHY THIS SEAM AND NOT ANOTHER. `dispatches.home_repo_id` decides WHICH
        REPOSITORY the board reports on, and it is the read that REPLACED the
        registry route after that route measured as a security defect. This
        lane's whole subject is that a read must reach the RECORD and not just
        the body, so the read that most matters is the one whose absence from
        the forbidden list cost the most: while the exclusion stood, an edit
        moving this read out of `_LrReadSet` kept the seam scan green.

        BOTH DIRECTIONS IN ONE SCAN, BECAUSE EITHER ALONE IS CHEAP TO SATISFY.
        A scan that flagged nothing would pass the INSIDE half; a scan that
        flagged everything would pass the OUTSIDE half. The planted consumer
        must be reported OUTSIDE **while the read-set's own reader stays
        INSIDE**, in the same walk of the same text.

        THE INSIDE HALF IS THE CORRECTED RATIONALE STATED AS A MEASUREMENT.
        The rationale this replaces claimed the seam could not be forbidden
        without breaking `assertEqual([], got.outside)`. It can: the real call
        lives in `_LrReadSet`, which is a door, so forbidding the seam moves it
        into `inside` and `outside` stays empty. The assertion below is that
        sentence, checked."""
        import helm.web_land_model as wlm
        with open(wlm.__file__, encoding="utf-8") as fh:
            real = fh.read()
        # The canonical read, measured on the untouched file FIRST, so the
        # line number below is read off the code rather than typed here.
        canonical = [hit for hit in
                     _seam_evidence(real, "web_land_model.py").inside
                     if hit.endswith("dispatches.home_repo_id")]
        self.assertEqual(
            1, len(canonical),
            "the read-set's own repo-selection read is not reported inside the "
            "door: either `(dispatches, home_repo_id)` has left `_SEAMS` — the "
            "exclusion this arm exists to forbid — or the read has left "
            "`_LrReadSet`, which is the defect itself: %r" % (canonical,))

        planted = real + self._REPO_PLANT
        # `.index` rather than a literal: if the plant text ever drifts this
        # raises instead of quietly asserting about the wrong line.
        line = planted.splitlines().index("    return d.home_repo_id()") + 1
        got = _seam_evidence(planted, "web_land_model.py")
        self.assertEqual(
            ["web_land_model.py:%d dispatches.home_repo_id" % line],
            got.outside,
            "a module-level consumer of the canonical repo-selection read, "
            "planted in front of the scan, was not reported outside the door — "
            "so this scan is blind to exactly the edit this lane forbids: %r"
            % (got.outside,))
        self.assertEqual(
            canonical,
            [hit for hit in got.inside
             if hit.endswith("dispatches.home_repo_id")],
            "forbidding the seam moved the read-set's OWN read out of the "
            "door, which would make the seam unaddable and is the thing the "
            "false rationale predicted: %r" % (got.inside,))
        self.assertEqual([], got.unresolved,
                         "the planted file stopped resolving, so neither list "
                         "above is the finding it looks like: %r"
                         % (got.unresolved,))

    # THE FOUR SPELLINGS OF ONE SEAM, planted in source the scan has never
    # seen. The first two are a review's blocker: a MODULE alias and a DIRECT
    # alias, neither of which is spelled `<module>.<function>` anywhere. The
    # third is the direct alias RENAMED, which is the same evasion one step
    # further, and the fourth is the fully dotted package path, which the old
    # `Attribute(Name)` shape could not even parse.
    _ALIAS_PLANT = (
        "import premise as p\n"                      # 1  module alias
        "from premise import chain_records\n"        # 2  direct alias
        "from landreq import _receipt_rows as rr\n"  # 3  renamed direct alias
        "import helm.landreq\n"                      # 4  dotted package path
        "\n"
        "def a_module_alias_bypass():\n"
        "    return p.chain_records()\n"
        "\n"
        "def a_direct_alias_bypass():\n"
        "    return chain_records()\n"
        "\n"
        "def a_renamed_direct_alias():\n"
        "    return rr()\n"
        "\n"
        "def a_dotted_module_bypass():\n"
        "    return helm.landreq._receipt_rows()\n"
        "\n"
        "def a_dynamic_bypass():\n"
        "    return getattr(p, 'chain_records')()\n"
        "\n"
        "def a_handoff(register):\n"
        "    return register(p.chain_records)\n"
        "\n"
        "def a_runtime_module(importlib):\n"
        "    mod = importlib.import_module('helm.landreq')\n"
        "    return mod._receipt_rows()\n"
        "\n"
        "class _LrReadSet(dict):\n"
        "    def premise_chain(self):\n"
        "        return p.chain_records()\n"
        "\n"
        "def the_pure_form(recs):\n"
        "    return p.verify_chain(recs)\n"
        "\n"
        "def not_a_module(reads):\n"
        "    return reads.stat('ledger', '/x')\n")

    def test_the_seam_evidence_CATCHES_a_MODULE_alias_and_a_DIRECT_alias(self):
        """THE MUST-HIT, SEEDED OUTSIDE THE SHAPE THAT WAS JUST FIXED.

        Round four's control was a forbidden seam planted INSIDE `_LrReadSet` —
        inside the population the scan already covered — and round five's was
        the two seams that had just been added to the list. Each proved the scan
        could find what it already knew about. A review ruled the alias hole a
        BLOCKER after I had offered it as an accepted limit, and the ruling
        stands: `import premise as p` and `from premise import chain_records`
        both reach the seam, neither is spelled `premise.chain_records`, and the
        scan went green over both.

        So the plant is four spellings of one seam in source this scan has never
        seen, OUTSIDE any door, and ALL of them must be caught before the zeros
        on the real modules mean anything.

        THE DISCRIMINATION IS ASSERTED IN THE SAME BREATH, because a scan that
        flags everything is as useless as one that flags nothing: the identical
        call INSIDE a door must land inside, `verify_chain(recs)` — the pure
        form, which performs no read — must not be flagged, and `reads.stat(...)`
        must not be mistaken for `os.stat` merely because the attribute matches.
        That last one is what makes the unresolvable report readable at all."""
        got = _seam_evidence(self._ALIAS_PLANT, "planted")
        self.assertEqual(
            ["planted:7 premise.chain_records",       # module alias
             "planted:10 premise.chain_records",      # direct alias
             "planted:13 landreq._receipt_rows",      # renamed direct alias
             "planted:16 landreq._receipt_rows"],     # dotted package path
            got.outside,
            "an aliased bypass planted right in front of the scan was not "
            "caught, so its zero on the real modules is a mirror: %r"
            % (got.outside,))
        self.assertEqual(
            ["planted:30 premise.chain_records"], got.inside,
            "the same call inside the door was not exempted, so the scan flags "
            "the door itself and can never go green honestly: %r" % (got.inside,))

    def test_the_seam_evidence_REPORTS_what_it_cannot_resolve(self):
        """AN UNRESOLVABLE CALL IS NOT A CLEAN SCAN (a review's instrument rule).

        `getattr(p, 'chain_records')()` reaches the seam and no static resolver
        can follow it. Skipping it silently is indistinguishable from a module
        with no bypass — which is the failure this round is about — so it is
        REPORTED, and so is a seam handed away as a value for someone else to
        call. The second is not hypothetical: the cured code itself used to pass
        `premise.chain_records` into `_serve` as a callable.

        THE RUNTIME-MODULE FORM IS CAUGHT AT THE ACQUISITION, NOT AT THE CALL,
        and that asymmetry is deliberate. `mod = import_module("helm.landreq")`
        then `mod._receipt_rows()` is, at the call site, indistinguishable from
        any other local object's method — the value-vs-module discriminator is
        RIGHT to say nothing there, or `reads.stat(...)` would be flagged on
        every line. So the string that names a seam MODULE is reported where it
        appears, which is the only place that form is visible at all. The plant
        holds both halves: the acquisition on line 25 is reported and the call
        on line 26 is not.

        AND THE REACH IS PART OF THE ANSWER. `reach` counts the calls walked and
        the calls whose head resolved, so a file with zero resolvable calls
        reads as visibly zero instead of silently clean."""
        got = _seam_evidence(self._ALIAS_PLANT, "planted")
        self.assertEqual(
            ["planted:19 computed callee names a seam",
             "planted:19 dynamic access names a seam",
             "planted:22 seam handed away uncalled",
             "planted:25 dynamic access names a seam"], got.unresolved,
            "a form the scan cannot resolve passed silently, so a bypass "
            "written that way would read as a clean module: %r"
            % (got.unresolved,))
        self.assertEqual({"calls": 12, "resolved": 6, "aliases": 4}, got.reach,
                         "the scan's own reach moved, so every count asserted "
                         "against it above is measuring a different walk: %r"
                         % (got.reach,))
        # AND THE OTHER DIRECTION, which is what keeps the report usable: a
        # scan over source with no alias at all must say so rather than
        # answering the same empty lists a clean module answers.
        blank = _seam_evidence(
            "def f(reads):\n    return reads.stat('a', 'b')\n", "blank")
        self.assertEqual({"calls": 1, "resolved": 0, "aliases": 0}, blank.reach,
                         "a file the resolver reached nothing in did not "
                         "report zero reach: %r" % (blank.reach,))
        self.assertEqual(([], [], []),
                         (blank.inside, blank.outside, blank.unresolved))

    # ONE COLLISION, BOTH SOURCE ORDERS, TWO OPPOSITE SYMPTOMS.
    #
    # Each pair is the SAME two statements — a genuine alias and a harmless
    # rebinding of the same bare name — swapped. The flat module-wide alias map
    # kept whichever `ast.walk` reached last, so:
    #
    #   real alias FIRST  -> the rebinding erased it and the scan answered
    #                        `outside=[] unresolved=[]`, WHICH IS THE ANSWER A
    #                        CLEAN MODULE GIVES;
    #   rebinding FIRST   -> the alias survived and the HARMLESS call site was
    #                        resolved through it and reported as a hit.
    #
    # A silent miss and a false accusation from one defect, and AN ARM WRITTEN
    # IN EITHER ORDER IS BLIND TO THE OTHER — the miss shows up as an empty
    # list that no reviewer double-checks, the false hit as a line number that
    # looks like a finding. Hence both orders, on both halves of the scan.
    _ORDER_PAIRS = (
        ("acquisition, real alias first",
         "from importlib import import_module as ld\n"
         "\n"
         "def a_runtime_module_bypass():\n"
         "    mod = ld('helm.landreq')\n"
         "    return mod._receipt_rows()\n"
         "\n"
         "from helm.lexicon import load as ld\n"
         "\n"
         "def a_harmless_reader():\n"
         "    return ld('helm.landreq')\n"),
        ("acquisition, rebinding first",
         "from helm.lexicon import load as ld\n"
         "\n"
         "def a_harmless_reader():\n"
         "    return ld('helm.landreq')\n"
         "\n"
         "from importlib import import_module as ld\n"
         "\n"
         "def a_runtime_module_bypass():\n"
         "    mod = ld('helm.landreq')\n"
         "    return mod._receipt_rows()\n"),
        ("seam, real alias first",
         "from premise import chain_records as recs\n"
         "\n"
         "def a_real_bypass():\n"
         "    return recs()\n"
         "\n"
         "from helm.cache import records as recs\n"
         "\n"
         "def a_harmless_reader():\n"
         "    return recs()\n"),
        ("seam, rebinding first",
         "from helm.cache import records as recs\n"
         "\n"
         "def a_harmless_reader():\n"
         "    return recs()\n"
         "\n"
         "from premise import chain_records as recs\n"
         "\n"
         "def a_real_bypass():\n"
         "    return recs()\n"),
    )

    def test_A_REBOUND_NAME_gives_the_SAME_answer_in_BOTH_SOURCE_ORDERS(self):  # noqa: VACUOUS_ASSERTION — the `[] == got.outside` absences are bracketed by unconditional positives OUTSIDE the loop: the pair table's length is pinned to four before it, and the collected `seen` is pinned to two halves of two orders each after it, so an emptied table reddens here rather than passing quietly
        """THE ORDER-COLLAPSE DEFECT, MEASURED FROM BOTH ENDS AT ONCE.

        MEASURED ON THE OLD RESOLVER, on these exact four plants: real-alias-
        first answered `outside=[] unresolved=[]` on BOTH halves — the genuine
        `import_module` bypass and the genuine `chain_records` bypass each
        vanished — while rebinding-first answered
        `['plant:4 dynamic access names a seam', 'plant:9 ...']` and
        `['plant:4 premise.chain_records', 'plant:9 premise.chain_records']`,
        the second of which accuses `helm.cache.records` of being the premise
        seam. One arbitrary winner, a silent miss in one order and a false
        accusation in the other.

        THE PROPERTY, AND WHY IT IS THE PROPERTY: SWAPPING TWO STATEMENTS MUST
        NOT CHANGE THE VERDICT. That is stronger than pinning either order's
        expected list, because it fails whichever direction the resolver
        collapses — and it is the assertion no single-order arm can make.

        AND THE ANSWER IS NOT A HIT, IT IS A REPORT. Which binding is live
        inside a function body at call time is not statically decidable, so
        `_seam_evidence` names the ambiguity in `unresolved` instead of
        guessing. `unresolved` is this scan's standing word for "cannot
        decide"; the defect was that the resolver had been deciding anyway."""
        # THE TABLE MUST STILL HOLD BOTH HALVES IN BOTH ORDERS. Every
        # assertion below is inside a loop over it, so an emptied or halved
        # table would agree with itself perfectly and prove nothing — the same
        # "matrix that shrank" failure the acquisition census guards against.
        self.assertEqual(4, len(self._ORDER_PAIRS),
                         "the pair table no longer carries two halves in two "
                         "orders, so the agreement below is between fewer "
                         "things than the defect had")
        seen = {}
        for label, src in self._ORDER_PAIRS:
            got = _seam_evidence(src, "plant")
            # UNCONDITIONAL POSITIVE FIRST: this plant is not empty. Both
            # orders agreeing on NOTHING is exactly the silent-miss failure,
            # so the agreement below is only worth reading once each half is
            # shown to still report the collision it contains.
            self.assertTrue(got.unresolved,
                            "%s reported nothing at all, so the agreement "
                            "asserted below is two silences agreeing — which "
                            "is the false-negative half of this very defect"
                            % label)
            self.assertEqual([], got.outside,
                             "%s resolved an ambiguous binding to a definite "
                             "seam hit, which is the false-accusation half: %r"
                             % (label, got.outside))
            seen.setdefault(label.split(",")[0], []).append(
                (label, [u.split(" ", 1)[1] for u in got.unresolved]))
        # AND WHAT WAS ACTUALLY COLLECTED, asserted as a count of things that
        # HAPPENED: two halves, two orders each, all four scanned.
        self.assertEqual([("acquisition", 2), ("seam", 2)],
                         sorted((half, len(pair)) for half, pair in seen.items()),
                         "the four plants did not land as two halves in two "
                         "orders, so the comparison below is not the one this "
                         "arm claims to make: %r" % (sorted(seen),))
        for half, pair in seen.items():
            (first_label, first), (second_label, second) = pair
            self.assertEqual(
                first, second,
                "the %s half answers differently depending on which of two "
                "statements is written first (%s -> %r, %s -> %r), so the "
                "resolver is still collapsing binding order into one winner"
                % (half, first_label, first, second_label, second))

    def test_a_FUNCTION_LOCAL_REBIND_does_not_erase_a_MODULE_LEVEL_alias(self):
        """THE SCOPE HALF, WHICH IS DECIDABLE AND SO IS RESOLVED, NOT REPORTED.

        The flat map had no scopes at all, so a `from helm.cache import records
        as recs` written inside one function overwrote a module-level
        `from premise import chain_records as recs` for the WHOLE FILE.
        MEASURED ON THE OLD RESOLVER, this plant answered `outside=[]` — the
        real bypass on line 4 was invisible — while the local rebind and the
        parameter that shadows it were the things being trusted.

        Scope is decidable from the tree, so here it is resolved rather than
        reported: line 4 is a HIT, the sibling's PARAMETER named `recs` is a
        value and says nothing, and the function-local rebind resolves to its
        own import. Three different answers for one spelling, which is the
        whole point of tracking scopes."""
        got = _seam_evidence(
            "from premise import chain_records as recs\n"
            "\n"
            "def a_real_bypass():\n"
            "    return recs()\n"
            "\n"
            "def a_sibling_with_its_own_name(recs):\n"
            "    return recs()\n"
            "\n"
            "def a_local_rebind():\n"
            "    from helm.cache import records as recs\n"
            "    return recs()\n", "plant")
        self.assertEqual(
            ["plant:4 premise.chain_records"], got.outside,
            "the module-level alias was erased by a rebinding in a function "
            "that cannot reach it, or a shadowing name was mistaken for it: %r"
            % (got.outside,))
        self.assertEqual([], got.unresolved,
                         "a binding this scan CAN decide was reported as "
                         "undecidable, which trades the old false negatives "
                         "for noise nobody will read: %r" % (got.unresolved,))

    def test_EVERY_NODE_GETS_A_SCOPE_and_HEADER_positions_still_resolve(self):  # noqa: VACUOUS_ASSERTION — the only empty-list assertion is the unscoped-node census, and it runs after two unconditional positives: the five-header ordered equality BEFORE the loop (which fails if the walker drops any header kind, whatever the census says), and, inside a loop over a literal two-module tuple that cannot be empty, a measured floor on the node count plus an equality between the walk and the binder's map
        """THE WALKER'S OWN CONTROL: AN UNSCOPED NODE RESOLVES TO NOTHING.

        Tracking scopes means the resolver now asks "which scope is this node
        in?", and a node the walker never visited has no answer — so it silently
        resolves to nothing, which is the SAME SILENT MISS that the flat map
        produced, rebuilt one level down. That failure mode is invisible in
        every other arm here: the plants would simply come back clean.

        SO THE SPLIT IS BODY-vs-EVERYTHING-ELSE RATHER THAN A LIST OF NODE
        KINDS, and this arm holds it to that. The plant puts one seam call in
        each header position that is NOT a body — a decorator, a parameter
        default, a parameter ANNOTATION, a class keyword and a lambda default —
        because a walker that enumerated kinds would drop whichever it forgot,
        and every one of these is evaluated in the ENCLOSING scope where the
        alias lives.

        AND THE CENSUS IS RUN ON THE REAL MODULES, not on the plant, because
        the plant only contains shapes I already thought of — which is exactly
        the mirror this lane keeps rebuilding. Every node of both production
        trees must have a scope."""
        got = _seam_evidence("import premise as p\n"
                             "\n"
                             "@p.chain_records()\n"
                             "def a_decorator_bypass():\n"
                             "    return 1\n"
                             "\n"
                             "def a_default_bypass(rows=p.chain_records()):\n"
                             "    return rows\n"
                             "\n"
                             "def an_annotation(rows: p.chain_records() = None):\n"
                             "    return rows\n"
                             "\n"
                             "class A(metaclass=p.chain_records()):\n"
                             "    pass\n"
                             "\n"
                             "f = lambda x=p.chain_records(): x\n", "header")
        self.assertEqual(
            ["header:10", "header:13", "header:16", "header:3", "header:7"],
            sorted(hit.split(" ")[0] for hit in got.outside),
            "a seam call in a header position was not resolved, so the walker "
            "is dropping a node kind and everything under it scans clean: %r"
            % (got.outside,))
        import helm.web_land as wl
        import helm.web_land_model as wlm
        for mod in (wlm, wl):
            base = os.path.basename(mod.__file__)
            with self.subTest(module=base):
                with open(mod.__file__, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
                binder = _seam_aliases(tree, "helm")
                # BY IDENTITY, NOT BY COUNT OF YIELDS. `ast.walk` hands back
                # the SHARED `Load`/`Store`/`Add` singletons once per node
                # that references them, so a raw length comparison reports a
                # gap of a thousand nodes that does not exist — a true reading
                # bound to the wrong claim. web_land_model.py yields 3556 and
                # holds 2415 distinct nodes; web_land.py yields 816 and holds
                # 560.
                walked = {id(n): n for n in ast.walk(tree)}
                # POSITIVE FIRST: this really is a large tree AND the binder
                # really populated its map, so the zero below is a census over
                # something rather than over nothing. The floor is MEASURED
                # AGAINST WHAT THIS LINE COUNTS — distinct nodes, 560 in
                # web_land.py and 2415 in web_land_model.py at this tip — and
                # not a round number from the shape of the file I had in mind.
                # The raw yield counts are the larger 816 and 3556; pinning
                # the floor to those would be a true reading attached to the
                # wrong claim.
                self.assertGreater(len(walked), 500,
                                   "%s parsed to a trivial tree, so the "
                                   "coverage claim below is about nothing"
                                   % base)
                self.assertEqual(len(walked), len(binder._chain),
                                 "the binder's scope map and the walk "
                                 "disagree about how many distinct nodes %s "
                                 "has, so one of them is not reading this "
                                 "file" % base)
                self.assertEqual(
                    [], sorted({type(n).__name__ for key, n in walked.items()
                                if key not in binder._chain}),
                    "nodes in %s were never given a scope, and an unscoped "
                    "node resolves to nothing — a silent miss wearing a clean "
                    "scan's clothes" % base)

    def test_the_seam_EVIDENCE_REFUSES_to_be_read_as_a_VERDICT(self):
        """THE DEMOTION, AS A PROPERTY OF THE VALUE RATHER THAN OF A DOCSTRING.

        Five of this lane's eleven review rounds were one more spelling the
        seam scan could not see, because a static reader of Python's dynamic
        imports cannot be completed and every round found the next one. The end
        of that regress is not a more complete scan. It is a result that cannot
        be mistaken for a proof.

        SO THERE IS NO TRUTH VALUE ON IT. `if scan(...)`, `assertTrue(scan(...))`
        and `assert scan(...)` — the three shapes that turn evidence into a
        certification — all raise, and the raise carries the caveat. A docstring
        is advice a reader can skip; this is a wall a reader hits.

        AND EVERY RESULT CARRIES WHAT IT CANNOT SEE. `blind_spots` is on the
        value, so it appears in the repr, in any `%r` failure message, and in
        `.caveat()`. The list is deliberately finite AND deliberately
        incomplete-by-construction: `_SEAM_UNSEEABLE` names the classes, and the
        docstring says outright that adding spellings is the regress rather than
        the cure."""
        got = _seam_evidence("import premise as p\n"
                             "def f():\n"
                             "    return p.chain_records()\n", "plant")
        # THE POSITIVE CONTROL: this really is a working scan result, not an
        # object that raises on everything.
        self.assertEqual(["plant:3 premise.chain_records"], got.outside)
        with self.assertRaises(TypeError) as caught:
            bool(got)
        self.assertIn("evidence, not a guarantee", str(caught.exception))
        self.assertIn("_LrReadSet._open_once", str(caught.exception),
                      "the refusal does not tell the reader where the real "
                      "guarantee lives, so it only removes an answer")
        self.assertEqual(_SEAM_UNSEEABLE, got.blind_spots)
        for expected in ("eval/exec", "from X import *", "one file at a time"):
            self.assertIn(expected, got.caveat(),
                          "the caveat stopped naming a whole class of thing "
                          "this scan cannot see, so a reader of the result "
                          "inherits a narrower limit than the real one")
        self.assertIn("not proof", got.caveat())

    # EVERY WAY THE THREE PRIMITIVES CAN BE BOUND, as a matrix rather than as
    # the one cell a reviewer happened to try. Each entry is (label, source);
    # each source names a real seam, so a miss is the scan failing and not the
    # plant being empty.
    _ACQUISITION_MATRIX = (
        ("getattr bare builtin",
         "import premise as p\n"
         "def f():\n"
         "    return getattr(p, 'chain_records')()\n"),
        ("getattr direct import",
         "import premise as p\n"
         "from builtins import getattr\n"
         "def f():\n"
         "    return getattr(p, 'chain_records')()\n"),
        ("getattr RENAMED import",
         "import premise as p\n"
         "from builtins import getattr as ga\n"
         "def f():\n"
         "    return ga(p, 'chain_records')()\n"),
        ("__import__ bare builtin",
         "def f():\n"
         "    m = __import__('helm.landreq')\n"
         "    return m._receipt_rows()\n"),
        ("__import__ direct import",
         "from builtins import __import__\n"
         "def f():\n"
         "    m = __import__('helm.landreq')\n"
         "    return m._receipt_rows()\n"),
        ("__import__ RENAMED import",
         "from builtins import __import__ as bi\n"
         "def f():\n"
         "    m = bi('helm.landreq')\n"
         "    return m._receipt_rows()\n"),
        ("import_module module attribute",
         "import importlib\n"
         "def f():\n"
         "    m = importlib.import_module('helm.landreq')\n"
         "    return m._receipt_rows()\n"),
        ("import_module RENAMED module attr",
         "import importlib as il\n"
         "def f():\n"
         "    m = il.import_module('helm.landreq')\n"
         "    return m._receipt_rows()\n"),
        ("import_module direct import",
         "from importlib import import_module\n"
         "def f():\n"
         "    m = import_module('helm.landreq')\n"
         "    return m._receipt_rows()\n"),
        ("import_module RENAMED import",
         "from importlib import import_module as imp\n"
         "def f():\n"
         "    m = imp('helm.landreq')\n"
         "    return m._receipt_rows()\n"),
    )

    def test_EVERY_BINDING_OF_EVERY_ACQUISITION_PRIMITIVE_IS_CAUGHT(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the exact ten-label ordered equality on `caught`, which asserts the scan REPORTED every binding by name; the two empty-list assertions that remain are intentional absences (`missed`, and the quiet-source control that must NOT be reported or every CAUGHT is a stuck yes)
        """A REVIEW FOUND ONE CELL; THE HOLE WAS THREE, AND THIS IS THE MATRIX.

        The reported miss was a renamed direct `import_module` alias. Measuring
        the whole matrix before curing it showed the same shape in all three
        primitives: of ten bindings, SEVEN were caught and THREE were missed,
        and every miss was the renamed direct import. Two of them —
        `__import__ as bi` and `import_module as imp` — came back with an
        EMPTY unresolved list, which is the worst available answer because it
        is the same answer a clean module gives. `getattr as ga` survived only
        as a "computed callee" report that names no module, and only because
        that plant calls the result immediately; bound to a variable first, it
        would have gone silent too.

        WHY A MATRIX AND NOT AN ELEVENTH ARM. The cure resolves the callee
        through the alias map, so the scan no longer has a per-spelling
        surface to miss — but that is a claim about a mechanism, and the way
        this defect arrived twice was a reviewer testing one spelling at a
        time. Enumerating the bindings makes the next unhandled one fail here
        rather than in someone's review, and makes THE COUNT the assertion: a
        primitive added to `_ACQUIRERS` without its bindings added here shows
        up as a matrix that no longer covers its own subject.

        THE SEAM IS NAMED IN EVERY CELL so a "caught" can never come from an
        empty plant, and the count is asserted against `_ACQUIRERS` so this
        cannot silently shrink to the cells that pass."""
        caught, missed = [], []
        for label, src in self._ACQUISITION_MATRIX:
            got = _seam_evidence(src, "plant")
            hits = [u for u in got.unresolved
                    if "dynamic access names a seam" in u]
            (caught if hits else missed).append(
                label if hits else (label, got.unresolved, got.outside))
        # THE EFFECT, ASSERTED POSITIVELY AND UNCONDITIONALLY, BEFORE ANY
        # ABSENCE. `missed == []` is equally what an EMPTY matrix produces, so
        # the claim that earns this arm is the one below: TEN bindings were
        # each REPORTED by name. That is a count of things that HAPPENED, not
        # of things that failed to.
        self.assertEqual(
            [label for label, _ in self._ACQUISITION_MATRIX], caught,
            "the scan did not report every binding in the matrix, so the "
            "empty `missed` below is a matrix that shrank rather than a scan "
            "that caught: %r" % (caught,))
        self.assertEqual(10, len(caught),
                         "the matrix no longer walks the ten bindings it was "
                         "measured against: %r" % (caught,))
        # AND THE SCAN CAN STILL SAY NO: a plant with no acquisition at all
        # must NOT be reported, or "everything is caught" is just a scan that
        # flags everything and the matrix below is satisfied by a stuck yes.
        quiet = _seam_evidence("import premise as p\n"
                           "def f(recs):\n"
                           "    return p.verify_chain(recs)\n", "quiet")
        self.assertEqual([], [u for u in quiet.unresolved
                              if "dynamic access names a seam" in u],
                         "source with no dynamic acquisition was reported as "
                         "having one, so every CAUGHT above is a stuck yes")
        self.assertEqual(
            [], missed,
            "a dynamic-acquisition binding reached a seam and the scan did not "
            "report it, so a bypass written that way reads as a clean module — "
            "which is the exact defect codex-3 found in one cell: %r" % (missed,))
        # THE MATRIX MUST COVER ITS OWN SUBJECT. Every primitive the scan
        # claims needs at least a bare and a RENAMED binding here, or the
        # census above is green over a spelling nobody enumerated.
        for fn in _ACQUIRERS:
            cells = [lbl for lbl, _ in self._ACQUISITION_MATRIX
                     if lbl.startswith(fn + " ")]
            self.assertTrue(
                any("RENAMED" in c for c in cells) and len(cells) >= 2,
                "%r is in _ACQUIRERS with no renamed binding in the matrix, so "
                "the one shape that defeated all three primitives is "
                "unmeasured for it: %r" % (fn, cells))

    def test_NO_RESOLVED_SHA_FALLS_BACK_TO_A_MOVING_REF_NAME(self):  # noqa: VACUOUS_ASSERTION — the empty-list assertion runs per file AFTER an unconditional assertTrue on the same scanner over the same file with the ref-name shape admitted, which proves this file really was parsed and walked
        """THE FIFTH BLOCKER AS A CLASS RATHER THAN TWO LINE NUMBERS.

        A review named landreq's `_git_observe` and `_base_behind`. Reading found
        two more of the same shape — the receipt-anchor verdict in `_observe`
        and the out-of-scope close — and the reason a fifth blocker existed at
        all is that round two cured ONE INSTANCE of this and never swept the
        class. So the sweep is the arm, not the two fixes.

        THE DOMAIN IS STATED WITH THE RESULT, deliberately: this is every
        or-expression whose left side is a resolved-sha call or a `*_sha` name,
        across helm/landreq.py and helm/web_land_model.py. It is not "there are
        no other fallbacks anywhere"."""
        from helm import landreq as lr
        from helm import web_land_model as wlm
        for mod in (lr, wlm):
            base = os.path.basename(mod.__file__)
            with self.subTest(module=base):
                with open(mod.__file__, encoding="utf-8") as fh:
                    source = fh.read()
                # INSTRUMENT CONTROL ON THIS FILE, and it is deliberately the
                # WALK rather than a looser rule. The first version of this
                # control admitted the `*_ref` name shape on the assumption both
                # files choose between ref names; the focused gate reddened,
                # because web_land_model.py has no such expression at all. That
                # is the control doing its job — a probe seeded from the shape
                # in my head, not from the file. So it now counts the nodes the
                # scanner actually walks: a zero here means the parse or the
                # walk never happened, which is the only way its zero below
                # could be an artefact rather than a finding.
                node_count = [n for n in ast.walk(ast.parse(source))
                              if isinstance(n, ast.BoolOp)
                              and isinstance(n.op, ast.Or)]
                self.assertTrue(node_count,
                                "the scanner walked no or-expression at all in "
                                "%s, so it did not read this file and its zero "
                                "below means nothing" % base)
                self.assertEqual(
                    [], _sha_fallback_scan(source, base),
                    "a resolved commit falls back to a moving ref name, so a "
                    "failed peel silently re-enters the argv while the witness "
                    "records only a failure: %r"
                    % (_sha_fallback_scan(source, base),))

    def test_the_fallback_scan_CATCHES_a_bypass_PLANTED_in_a_third_function(self):
        """THE SWEEP'S OWN MUST-HIT, and its discrimination.

        A zero from a scan that matches nothing looks exactly like a clean
        module — the failure this whole round is about. So: two plants in
        functions this scanner has never seen, in both spellings of the shape
        (the resolved-call form and the `*_sha` name form), must both be caught.

        AND THE CURED SHAPE MUST NOT BE, which matters as much: `or ""` is the
        honest refusal that `_landing_proofs._pin` deliberately keeps, and a
        scanner that flagged it would force a real cure to be reverted to go
        green."""
        planted = (
            "def a_third_function(gitdir, ref, cache):\n"
            "    ref = _resolved_ref(gitdir, ref, cache) or ref\n"
            "    return ref\n"
            "\n"
            "def a_fourth_function(gitdir, tip, cache):\n"
            "    return _landed(gitdir, tip, local_sha or local_ref)\n"
            "\n"
            "def the_cured_shape(repo_id, trunk, refs):\n"
            "    return (_resolved_ref(repo_id, trunk, refs) or '').lower()\n")
        self.assertEqual(["planted:2", "planted:6"],
                         _sha_fallback_scan(planted, "planted"),
                         "the sweep cannot see a fallback planted in front of "
                         "it, or it flags the empty-string refusal that IS the "
                         "cure — either way its zero above means nothing")

    def test_the_object_door_REFUSES_a_moving_ref(self):
        """REQUIRED ARM 1, FIRST MUTATION — stated as the door's own check.

        Round one was `git log <trunk ref>`: a read that LOOKS like a derivation
        of the pin, is answered about whatever the name pointed at as it ran,
        and therefore put lands in the body that the witness never described.
        The immutable-object door is where that becomes unrepresentable — a
        non-object operand is refused and the record goes blind, so a body built
        from one can never be certified.

        MEASURED ON THE REAL SEAM, not on a fixture: the ref comes out of this
        repository's own `repo()` reading."""
        from helm import web_land_model as m
        with m._lr_snapshot() as reads:
            gitdir, trunk = reads.repo()
            pinned = reads.refsha(gitdir, trunk)
            # UNCONDITIONAL POSITIVE CONTROL FIRST, on the same door and the
            # same repository: the PINNED COMMIT passes and really reads git.
            ok = reads.object_read(gitdir, "log", pinned, "--max-count=1",
                                   "--pretty=%H")
            self.assertIsNotNone(ok, "the door refused the pinned commit too, "
                                     "so the refusal below discriminates none")
            self.assertEqual(pinned, (ok.stdout or "").strip())
            self.assertIsInstance(reads.witness(), str)
            # ...and the MOVING NAME, which git would answer just as happily.
            self.assertIsNone(reads.object_read(gitdir, "log", trunk,
                                                "--max-count=1"),
                              "a moving ref was walked as though it named an "
                              "immutable object")
            self.assertIsNone(reads.witness(),
                              "a read of a moving name left the record "
                              "persistable, which is round one exactly")

    def test_the_ANNOTATION_pin_lands_in_the_SAME_record(self):
        """`_landing_proofs` keeps a per-repository trunk pin for succession and
        frontier debt, and it used to live in a dict PRIVATE to that closure —
        bound to the build's snapshot only because one `projscope` spanned the
        cycle and its peel argv matched `_resolved_ref`'s to the character. An
        indirect binding is one respelling away from being two readings of a
        moving name, with the annotations then describing a trunk the witness
        never named and nothing anywhere disagreeing.

        Sharing the caller's cache makes it structural: the pin is IN the
        record, so it is in the witness."""
        gitdir = self.repo_git
        owner = {"repo_id": gitdir}
        carrier = {"repo_id": gitdir, "reviewed_tip": self.side}
        from helm import web_land_model as m
        with m._lr_snapshot() as reads:
            proof = landreq._landing_proofs(gitdir, cache=reads)(owner, carrier)
            recorded = {(op, json.loads(okey)[0])
                        for op, okey in reads._reads}
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: the annotator
        # really ran and really answered about this repository, so an empty
        # record below would be a missing pin and not a proof that never fired.
        self.assertIn(proof, (landreq.PROOF_ANCESTOR, landreq.PROOF_ABSENT,
                              landreq.PROOF_PATCH_EQUIVALENT))
        self.assertIn(("refsha", gitdir), recorded,
                      "the annotator's trunk pin never reached the record, so "
                      "the trunk it judged against is not in the witness: %r"
                      % (sorted(recorded),))

    def test_a_CHANGED_read_set_REJECTS_the_restore(self):  # noqa: VACUOUS_ASSERTION — nothing here asserts an absence; both regimes are positive (assertEqual on the restored body plus assertLess, then assertIsNotNone plus assertGreater)
        """REQUIRED ARM 5. The other direction of the whole feature:
        bind hard enough and every build looks changed, so nothing ever
        restores and every arm above stays green over a cache that no longer
        works. A world nobody touched must re-collect IDENTICALLY and admit its
        body; a world that moved must not."""
        from helm import web_land_model as m
        key = "t757_recheck"
        path = web_cache._persist_path(key)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        probe = os.path.join(tempfile.mkdtemp(), "input.jsonl")
        self.addCleanup(shutil.rmtree, os.path.dirname(probe),
                        ignore_errors=True)
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("one\n")

        def collect():
            with m._lr_snapshot() as reads:
                reads.stat("ledger", probe)
                return reads.witness()

        saved = collect()
        # UNCONDITIONAL POSITIVE CONTROL, FIRST AND ON THE SAME OBSERVABLE: an
        # untouched world re-collects to the SAME projection, so the rejection
        # below is a discrimination and not a collector that never repeats.
        self.assertIsInstance(saved, str)
        web_cache._persist_store(key, time.time(), {"read_ts": 1, "loops": []},
                                 saved)
        with m._lr_snapshot() as reads:
            fresh = web_cache.persist_load(key, 30, reads)
        self.assertEqual({"read_ts": 1, "loops": []}, fresh[1],
                         "an unchanged input did not restore its body at all")
        self.assertLess(time.time() - fresh[0], 30,
                        "an unchanged input was aged into STALE")
        with open(probe, "a", encoding="utf-8") as fh:
            fh.write("two\n")               # the input MOVES between lives
        with m._lr_snapshot() as reads:
            stale = web_cache.persist_load(key, 30, reads)
        self.assertIsNotNone(stale, "a moved input dropped the body entirely")
        self.assertGreater(time.time() - stale[0], 30,
                           "a body whose input moved was served FRESH — the "
                           "confident stale board this whole feature exists "
                           "to avoid")

    def test_the_RESTORE_replays_the_SAVED_operands_not_a_second_enumeration(self):  # noqa: VACUOUS_ASSERTION — the assertNotEqual has its unconditional positive control first and on the same observable: assertEqual(saved, reads.recheck(saved)) proves an unchanged world replays IDENTICALLY
        """WHY A BOUNDED REPLAY IS TOTAL, measured rather than argued.

        `recheck` re-reads exactly the operands the saved build consumed. That
        is only sound because every operand is either fixed or DERIVED from
        another recorded read — so a world that would make the build read a
        DIFFERENT operand must first change a result this replay compares. The
        arm makes that concrete: the record holds the ENUMERATION as its own
        read, so a changed enumeration is a changed term."""
        from helm import web_land_model as m
        paths = [["/a.jsonl"]]
        with mock.patch.object(m, "_lr_ledger_paths",
                               side_effect=lambda: list(paths[0])), \
             mock.patch.object(m._LrReadSet, "_read_stat",
                               staticmethod(lambda p: ["FIXED", p])):
            with m._lr_snapshot() as reads:
                reads.stat("ledger", reads.ledger_paths()[0])
                saved = reads.witness()
            # POSITIVE CONTROL FIRST: replaying the same world reproduces the
            # same projection, so the inequality below is the enumeration.
            self.assertIsInstance(saved, str)
            with m._lr_snapshot() as reads:
                self.assertEqual(saved, reads.recheck(saved))
            paths[0] = ["/a.jsonl", "/b.jsonl"]     # a ledger JOINS the set
            with m._lr_snapshot() as reads:
                moved = reads.recheck(saved)
        self.assertNotEqual(saved, moved,
                            "the ledger SET changed and the replay could not "
                            "see it, so an unread input would ride in silently")

    def test_a_saved_record_this_code_CANNOT_REPLAY_is_refused(self):  # noqa: VACUOUS_ASSERTION — every refusal is a COMBINED record whose replayable half is asserted, in the same regime and on the same reader, to project a real witness string; so None can only be the refusal and never an empty read-set
        """A record naming an operation this code no longer has is a record
        whose meaning changed under it. Comparing it would be guessing, and the
        guess that costs is the one that says CURRENT.

        EVERY MALFORMED RECORD CARRIES A REPLAYABLE TERM, AND THAT IS THE WHOLE
        ARM. The previous version fed each malformed record ALONE, and a
        mutation matrix measured it surviving `return None` -> `continue`: an
        implementation that SKIPS the unreplayable term reaches
        `return self.witness()` holding a read-set that performed NO READS, and
        `witness()` answers None for exactly that reason. So `assertIsNone` was
        satisfied by two different mechanisms — genuine refusal, and nothing was
        read — and could not tell them apart. Measured: bare unknown-op answers
        None under both; a replayable term beside it answers None under the real
        code and a witness STRING under the skipping one.

        AND THE OLD POSITIVE CONTROL DID NOT SAVE IT, which is worth stating
        because a reader who sees a control present will assume it covered this.
        It replayed a DIFFERENT record, so it answered "can this reader ever
        produce a witness" when the question was "is THIS record refused". The
        control is now on the same axis and inside the same case: the prefix
        term is asserted to replay to a real string, so the combined record
        demonstrably CONTAINS a reading that projects — and None can only mean
        the refusal fired.

        `empty` IS THE ONE CASE THAT CANNOT BE MADE DISCRIMINATING, and it is
        said rather than dressed up. An empty record has no term to prefix, and
        the branch behind its guard (`len(fields) != 3`) refuses the same input,
        so no implementation that reaches either guard can answer anything but
        None. It is kept as a boundary input, not as evidence."""
        from helm import web_land_model as m
        with m._lr_snapshot() as reads:
            reads.stat("ledger", __file__)
            good = reads.witness()
        self.assertIsInstance(good, str)
        for name, malformed in (
                ("unknown-op", "sometime\x1f[\"x\"]\x1f\"v\""),
                ("bad-operands", "stat\x1fnot-json\x1f\"v\""),
                ("wrong-arity", "stat\x1f[]\x1f\"v\""),
                ("empty-objexist", "objexist\x1f[\"/repo/.git\"]\x1f{}"),
                ("truncated", "stat\x1f[\"ledger\"]")):
            with self.subTest(case=name):
                # THE CONTROL, ON THE SAME AXIS AND IN THE SAME REGIME: the
                # half this record carries beside the malformed term really
                # replays and really projects. Without this the None below
                # would be indistinguishable from a read-set that read nothing.
                with m._lr_snapshot() as reads:
                    self.assertIsInstance(
                        reads.recheck(good), str,
                        "the replayable half did not project, so the refusal "
                        "below would prove nothing")
                with m._lr_snapshot() as reads:
                    self.assertIsNone(
                        reads.recheck(good + "\x1e" + malformed),
                        "%s was SKIPPED and the rest of the record compared "
                        "anyway, so a saved witness whose meaning changed "
                        "under this code reads as CURRENT" % name)
        # THE BOUNDARY INPUT, kept without a claim it cannot support.
        with m._lr_snapshot() as reads:
            self.assertIsNone(reads.recheck(""))

    # ── BLOCKER (1): the CONTENT of the two files the record only STATTED,
    #    AND THE TOCTOU INSIDE THE TWO DOORS THAT SERVE IT ──────────────────
    #
    # EVERY ARM BELOW DRIVES A REAL FILE. The previous round's arms patched
    # `helm.premise.chain_records` and `helm.landreq._receipt_rows` — the exact
    # two functions the cure STOPS CALLING — so after the cure they would have
    # been fixtures nobody consulted, passing while reading the live home. A
    # double that the code under test no longer reaches is not a control.

    def chain_at(self, path, records):
        """Write a premise chain file, in the chain's own append-only grammar."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
        return path

    def ledger_at(self, path, rows):
        """Write a receipt ledger file the strict event parser accepts."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        return path

    def scratch(self, name):
        # REALPATH, because `eventledger._prepare` refuses a ledger whose parent
        # resolves through a symlink — a temp root that is one (a TMPDIR under
        # /var, a macOS /tmp) would make every ledger arm below read UNREADABLE
        # and pass for the wrong reason.
        d = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return os.path.join(d, name)

    def bytes_at(self, path):
        with open(path, "rb") as fh:
            return fh.read()

    def test_a_LINK_SWAPPED_between_VALIDATION_and_OPEN_is_REFUSED(self):
        """THE SIXTH DOOR: the seal said WHAT WAS OPENED, not what the NAME MEANT.

        `_read_receipts` validated through `eventledger._prepare` — which lstat`s
        the ledger and refuses a symlink — and then handed the PATH STRING to
        `_open_once`, which resolved that name a SECOND time. Between those two
        syscalls the name can stop meaning the same file, and MEASURED at the
        reviewed tip it did: the decoy's rows came back under a `_Sealed` stamp
        naming the ledger and carrying the DECOY's inode and digest. The record
        and the body described two worlds, which is the one thing this class
        exists to make unrepresentable, alive in the door built against it
        (exact-tip round six).

        THE THREAT MODEL IS ACCIDENT, NOT ADVERSARY, and the arm is written to
        say so. Atomic replace via rename, log rotation, a deploy swapping a
        link, an editor writing through a temp file — each retargets a name
        while a reader stands between its own two syscalls. Standing in that
        window ON PURPOSE is how a race becomes a test instead of a flake: the
        wrapper below performs the REAL validation and then does what the
        accident does, so the window is entered every run rather than one run
        in ten thousand.

        THE REFUSAL IS `_Blind` AND THAT IS THE POINT. Not an exception, not an
        empty ledger — a read that HAPPENED and IDENTIFIES NOTHING, so the
        witness is null, no projection persists, and nothing is certified. A
        forged row served under any wrapper would be the defect; a forged row
        served under a SEAL is the defect that certifies itself."""
        from helm import eventledger
        from helm import web_land_model as m
        path = self.ledger_at(self.scratch("land-receipts.jsonl"),
                              [{"id": "HONEST", "topic": "helm.land"}])
        decoy = self.ledger_at(os.path.join(os.path.dirname(path), "decoy.jsonl"),
                               [{"id": "FORGED", "topic": "helm.land"}])
        real_prepare = eventledger._prepare
        swapped = []

        def prepare_then_swap(target, create=False):
            checked = real_prepare(target, create=create)   # the REAL validation
            if not swapped and checked == os.path.abspath(path):
                swapped.append(checked)
                os.unlink(checked)
                os.symlink(decoy, checked)                  # the accident
            return checked

        with mock.patch.object(eventledger, "_prepare", prepare_then_swap):
            got = m._LrReadSet._read_receipts(path)
        # THE POSITIVE CONTROL ON THE SAME OBSERVABLE, FIRST AND
        # UNCONDITIONALLY. "no FORGED row" is an ABSENCE, and an absence is
        # also what a reader that returned nothing at all would produce — so
        # before believing the refusal, drive the SAME door over the SAME
        # decoy with NO swap and require the forged row to come back SEALED.
        # That is what proves this observable can be non-empty through this
        # exact code path, which is the only thing that makes its emptiness
        # below evidence of anything.
        proof = m._LrReadSet._read_receipts(decoy)
        self.assertIsInstance(proof, m._Sealed,
                              "the decoy is not readable through this door at "
                              "all, so an empty read below proves nothing")
        self.assertEqual(["FORGED"], [r["id"] for r in proof.value[0]],
                         "the decoy's rows do not reach this door unswapped, "
                         "so their absence after the swap is not a refusal")
        # AND THE WINDOW WAS REALLY ENTERED.
        self.assertTrue(swapped, "the validation was never reached, so this arm "
                                 "never entered the window it exists to test")
        self.assertTrue(os.path.islink(path),
                        "the name was not replaced by a link, so a refusal "
                        "below would be refusing something else")
        self.assertEqual(os.path.realpath(path), os.path.realpath(decoy),
                         "the link does not point at the decoy, so 'no forged "
                         "row' cannot discriminate")
        rows = got.value[0] if isinstance(got.value, tuple) else []
        self.assertEqual([], [r for r in rows if r.get("id") == "FORGED"],
                         "the swapped-in target's rows were SERVED — the door "
                         "followed a link the validation had just refused: %r"
                         % (rows,))
        self.assertIsInstance(
            got, m._Blind,
            "a read that reached a file the validation never saw was sealed "
            "rather than refused, so the witness CERTIFIES the decoy: %r"
            % (getattr(got, "stamp", None),))

    def test_the_STAMP_comes_from_the_DESCRIPTOR_not_from_the_NAME(self):
        """THE LANE'S PREMISE ITSELF, which a mutation found undefended.

        Every arm here proved the door refuses the WRONG file. None proved
        where the stamp's identity COMES FROM — so replacing the fstat with a
        fresh `os.stat(path)` inside `_open_once`, which is precisely the
        defect this class exists to prevent, survived the whole suite. The
        symlink arms cannot see it (O_NOFOLLOW refuses before the stamp is
        built) and the ordinary arms cannot see it (name and descriptor agree),
        which is exactly how a premise ends up with no arm on it.

        SO MAKE THEM DISAGREE, with the commonest accident there is: the file
        is atomically REPLACED by a rename after the open. The descriptor still
        holds the original inode and the original bytes; the NAME now means a
        different file. A stamp derived from the descriptor reports what it
        served. A stamp derived from the name reports a file whose bytes this
        read never saw — a record describing one world and a body another,
        which is the whole defect in one line.

        THE REPLACEMENT IS DRIVEN THROUGH `os.open` ITSELF so the window is
        entered every run: the wrapper opens, replaces, and returns the fd it
        already holds. That is a race made deterministic, not a race hoped for."""
        from helm import web_land_model as m
        path = self.ledger_at(self.scratch("land-receipts.jsonl"),
                              [{"id": "ORIGINAL", "topic": "helm.land"}])
        successor = self.ledger_at(
            os.path.join(os.path.dirname(path), "successor.jsonl"),
            [{"id": "SUCCESSOR", "topic": "helm.land"}])
        original_ino = os.stat(path).st_ino
        successor_ino = os.stat(successor).st_ino
        self.assertNotEqual(original_ino, successor_ino,
                            "the two files share an inode, so this arm cannot "
                            "tell the descriptor from the name")
        real_open, replaced = os.open, []

        def open_then_replace(target, *a, **kw):
            fd = real_open(target, *a, **kw)
            if target == path and not replaced:
                replaced.append(True)
                os.rename(successor, path)   # atomic replace, fd unaffected
            return fd

        with mock.patch.object(os, "open", open_then_replace):
            stamp, data, st = m._LrReadSet._open_once(path)
        self.assertTrue(replaced, "the file was never replaced, so the name "
                                  "and the descriptor never disagreed and this "
                                  "arm proves nothing")
        self.assertEqual(successor_ino, os.stat(path).st_ino,
                         "the NAME does not resolve to the successor, so the "
                         "assertions below cannot discriminate")
        self.assertEqual(original_ino, st.st_ino,
                         "the returned stat is not the opened descriptor's")
        self.assertEqual(original_ino, stamp[2],
                         "THE STAMP CARRIES THE NAME'S CURRENT INODE, not the "
                         "one it served: the record names a file these bytes "
                         "never came from: %r" % (stamp,))
        self.assertEqual(["ORIGINAL"],
                         [json.loads(ln)["id"]
                          for ln in data.decode().splitlines() if ln],
                         "the bytes came from the successor, so the read "
                         "followed the name rather than its own descriptor")

    def test_the_NOFOLLOW_DEFAULT_is_the_guard_and_not_just_the_call_site(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control runs BEFORE the assertRaises on the same file and the same function: `_open_once(link, nofollow=False)` is asserted to return the TARGET's rows and a stamp naming the link, so the refusal that follows is about the FLAG and not about an unreadable fixture
        """THE DEFAULT IS A CONTRACT AND A MUTATION PROVED NOBODY HELD IT TO IT.

        `_read_receipts` passes `nofollow=True` explicitly, so flipping the
        DEFAULT to False changed no behaviour any arm observed and survived.
        But the default is the actual safety claim — it is what makes the LAX
        case the one that must be justified, and it is what a third caller
        added later will silently inherit. An undefended default is a comment.

        So this asserts the bare call: `_open_once(link)` with no keyword must
        refuse a symlink, and the same call opting out must accept it. Both
        directions, because a guard that refuses everything is not a guard."""
        from helm import web_land_model as m
        target = self.ledger_at(self.scratch("target.jsonl"),
                                [{"id": "TARGET", "topic": "helm.land"}])
        link = os.path.join(os.path.dirname(target), "link.jsonl")
        os.symlink(target, link)
        # THE POSITIVE CONTROL: the link IS readable when following is asked
        # for, so the refusal below is about the flag and not about the file.
        stamp, data, _st = m._LrReadSet._open_once(link, nofollow=False)
        self.assertEqual(["TARGET"], [json.loads(ln)["id"]
                                      for ln in data.decode().splitlines() if ln])
        self.assertEqual(link, stamp[0])
        with self.assertRaises(OSError) as caught:
            m._LrReadSet._open_once(link)          # NO KEYWORD: the default
        self.assertEqual(
            errno.ELOOP, caught.exception.errno,
            "the bare call followed a symlink, so every future caller that "
            "does not name the flag inherits the defect this round cured: %r"
            % (caught.exception,))

    def test_a_PLATFORM_WITHOUT_O_NOFOLLOW_REFUSES_rather_than_FOLLOWS(self):
        """THE FLAG'S OWN ABSENCE, WHICH NO ARM COULD SEE.

        Every other arm here runs where `os.O_NOFOLLOW` exists, so all of them
        stayed green against the one degradation that matters: the house
        spelling `getattr(os, "O_NOFOLLOW", 0)` becomes `|= 0` where the
        constant is missing, and the guard evaporates without a word. The open
        succeeds, the link is followed, the seal certifies the target — the
        exact defect this round cured, restored by a platform rather than by an
        edit, and invisible to a suite that never runs on such a platform.

        THIS IS THE LANE'S OWN SHAPE and that is why it earns an arm rather
        than a comment: a check that consults something NARROWER than the
        property it claims, and fails in the reassuring direction. Canon says a
        bound that skips its work yields UNKNOWN and never a confident answer,
        and this door already has an UNKNOWN channel — `_Blind`. So the
        constant is removed and the door must refuse.

        THE OPT-OUT PATH IS ASSERTED UNCHANGED IN THE SAME BREATH, because a
        refusal that also broke `_read_chain` would swap one outage for
        another: that reader never asks for the protection, so it must be
        entirely unaffected by the platform lacking it."""
        from helm import web_land_model as m
        path = self.ledger_at(self.scratch("land-receipts.jsonl"),
                              [{"id": "HONEST", "topic": "helm.land"}])
        # THE POSITIVE CONTROL: with the constant present this very file reads.
        self.assertIsInstance(m._LrReadSet._read_receipts(path), m._Sealed)
        saved = os.O_NOFOLLOW
        del os.O_NOFOLLOW                     # the platform that lacks it
        self.addCleanup(setattr, os, "O_NOFOLLOW", saved)
        try:
            self.assertFalse(hasattr(os, "O_NOFOLLOW"),
                             "the constant is still present, so this arm is "
                             "measuring the ordinary platform")
            with self.assertRaises(OSError) as caught:
                m._LrReadSet._open_once(path)
            self.assertEqual(
                errno.ENOSYS, caught.exception.errno,
                "the door opened WITHOUT the protection it was asked for, so "
                "on such a platform a swapped link is followed and sealed and "
                "nothing says so: %r" % (caught.exception,))
            blind = m._LrReadSet._read_receipts(path)
            self.assertIsInstance(
                blind, m._Blind,
                "an unprovidable guarantee was answered with a CERTIFIED read "
                "instead of UNKNOWN: %r" % (getattr(blind, "stamp", None),))
            # AND THE READER THAT NEVER ASKED IS UNTOUCHED.
            chain = self.chain_at(self.scratch("attest-chain.jsonl"),
                                  [{"chain_index": 1, "rec_hash": "", "prev": ""}])
            self.assertIsInstance(
                m._LrReadSet._read_chain(chain), m._Sealed,
                "the premise chain, which never asks for O_NOFOLLOW, broke on "
                "a platform lacking it — one outage traded for another")
        finally:
            os.O_NOFOLLOW = saved

    def test_the_cure_REFUSES_NOTHING_the_validation_already_ACCEPTS(self):
        """THE COST OF THE GUARD, MEASURED — because O_NOFOLLOW is a REFUSAL.

        A flag that turns a working read into a crash would be a worse defect
        than the one it cures, and the owner's live complaint on this lane is
        that his ledger reads too SLOWLY to render — a permanently unreadable
        one is the same symptom, forever. So the claim that earns the flag is
        narrow and checkable: `_prepare` ALREADY rejects a symlinked leaf, so
        every path it accepts is a path O_NOFOLLOW also accepts, and the flag
        closes a race without narrowing the set of ledgers helm will read.

        AND THE ASYMMETRY IS ASSERTED, not just documented. `_read_chain` opts
        OUT, because the premise chain's own writer appends through
        `open(path, "a+")` and follows links: a reader stricter than its writer
        would make a chain helm itself wrote permanently `_Blind`, nulling the
        witness and stopping the projection persisting — this lane's defect,
        reintroduced as a fix."""
        from helm import web_land_model as m
        path = self.ledger_at(self.scratch("land-receipts.jsonl"),
                              [{"id": "HONEST", "topic": "helm.land"}])
        got = m._LrReadSet._read_receipts(path)
        self.assertIsInstance(got, m._Sealed,
                              "an ORDINARY regular-file ledger stopped being "
                              "readable, so the guard costs more than it saves")
        self.assertEqual(["HONEST"], [r["id"] for r in got.value[0]])
        # THE DELIBERATE ASYMMETRY: a symlinked CHAIN still reads.
        target = self.chain_at(self.scratch("real-chain.jsonl"),
                               [{"chain_index": 1, "rec_hash": "", "prev": ""}])
        link = os.path.join(os.path.dirname(target), "attest-chain.jsonl")
        os.symlink(target, link)
        chain = m._LrReadSet._read_chain(link)
        self.assertIsInstance(
            chain, m._Sealed,
            "a symlinked premise chain was refused, but the chain's own writer "
            "appends through a link — so helm can no longer read a file it "
            "still writes, and the projection never persists")
        self.assertEqual(1, len(chain.value))

    def test_the_PREMISE_CONTENT_moves_the_witness_at_a_FROZEN_INODE(self):  # noqa: VACUOUS_ASSERTION — the assertNotEqual runs after TWO unconditional positive controls on the same observable: the first witness is asserted to be a str, and an UNCHANGED chain is asserted to re-collect the IDENTICAL witness
        """Blocker (1), stated as the property that makes it
        impossible: one path AND one inode over two different chain contents
        must not produce the same witness.

        THE INODE IS FROZEN BY REWRITING THE SAME FILE IN PLACE, which is the
        measurement isolating the claim. The previous round froze a MOCKED stat
        instead; that removed the other reasons the witness could move, but it
        also meant the arm never touched a file. Rewriting one inode removes
        exactly the same alternatives — same path, same dev/ino — and does it
        against the real reader, so what remains is the content digest."""
        from helm import web_land_model as m
        from helm.premise import _chain
        path = self.scratch("attest-chain.jsonl")
        with mock.patch.object(_chain, "_chain_path", lambda: path):
            self.chain_at(path, [{"chain_index": 1}])
            before = os.stat(path).st_ino
            with m._lr_snapshot() as reads:
                self.assertEqual([{"chain_index": 1}], reads.premise_chain())
                first = reads.witness()
            # POSITIVE CONTROL FIRST, on the same observable: an UNCHANGED
            # chain must re-collect IDENTICALLY, so the inequality below is the
            # content and not the instrument.
            self.assertIsInstance(first, str)
            with m._lr_snapshot() as reads:
                reads.premise_chain()
                self.assertEqual(first, reads.witness())
            self.chain_at(path, [{"chain_index": 1}, {"chain_index": 2}])
            self.assertEqual(before, os.stat(path).st_ino,
                             "the rewrite moved the inode, so this arm no "
                             "longer isolates the CONTENT from the file")
            with m._lr_snapshot() as reads:
                reads.premise_chain()
                moved = reads.witness()
        self.assertNotEqual(first, moved,
                            "a record was appended and the witness could not "
                            "see it, so a body built on the new chain restores "
                            "under the old one's fingerprint")

    def test_the_PREMISE_INODE_moves_the_witness_at_FROZEN_CONTENT(self):  # noqa: VACUOUS_ASSERTION — the assertNotEqual runs after two unconditional positives: the first witness is asserted to be a str, and the twin's BYTES are asserted equal to the original's, which is what makes the moved witness the FILE and not the content
        """The other half of the seal, and the half a content digest alone does
        not provide: the same path, the same BYTES, a different FILE.

        A helm-home swap, a restore-from-backup, a `mv` of a rotated chain into
        place — each leaves a reader answering identical content out of a file
        that is not the one the previous build read. The stamp carries dev and
        ino from the same `fstat` that served those bytes, so this is a term
        that CHANGED and the body ages out."""
        from helm import web_land_model as m
        from helm.premise import _chain
        path = self.scratch("attest-chain.jsonl")
        with mock.patch.object(_chain, "_chain_path", lambda: path):
            self.chain_at(path, [{"chain_index": 1}])
            with m._lr_snapshot() as reads:
                reads.premise_chain()
                first = reads.witness()
            self.assertIsInstance(first, str)
            twin = self.chain_at(path + ".twin", [{"chain_index": 1}])
            self.assertEqual(self.bytes_at(path), self.bytes_at(twin),
                             "the twin's bytes differ, so a moved witness "
                             "below would be the CONTENT and not the file")
            os.replace(twin, path)
            with m._lr_snapshot() as reads:
                reads.premise_chain()
                swapped = reads.witness()
        self.assertNotEqual(first, swapped,
                            "a different file answered under the same name "
                            "with the same bytes and the record could not tell")

    def test_the_premise_door_SERVES_the_file_it_RECORDED(self):  # noqa: VACUOUS_ASSERTION — nothing here asserts an absence; every assertion is a positive equality, and the resolver is unconditionally asserted to have MOVED to the other file before the served content is read
        """The exact-tip blocker: THE DOOR ITSELF HAD THE TOCTOU.

        `premise_chain` recorded the operand `input_path("premise")` resolved,
        and then called `premise.chain_records()` — which resolves the chain
        path AGAIN and opens whatever it names at that instant. A resolver that
        moves in that gap leaves the record naming file A while the body
        consumed file B: the original same-input divergence, alive inside the
        door built to close it.

        SO THE RESOLVER IS MOVED IN EXACTLY THAT GAP. The first resolution
        answers A and every later one answers B, which is the adversarial world
        rather than a slow one — no timing, no sleep, no luck. The body must be
        served A, because A is what the record names.

        THE INSTRUMENT IS CONTROLLED BY ASKING THE RESOLVER AFTERWARDS. If the
        move never took effect the arm would pass while proving nothing, so the
        resolver is required to answer B at the end and the recorded operand is
        required to be A."""
        from helm import web_land_model as m
        from helm.premise import _chain
        moved_to = self.chain_at(self.scratch("B.jsonl"),
                                 [{"chain_index": 9}, {"chain_index": 10}])
        recorded = self.chain_at(self.scratch("A.jsonl"), [{"chain_index": 1}])
        resolutions = []

        def moving_resolver():
            resolutions.append(1)
            return recorded if len(resolutions) == 1 else moved_to

        with mock.patch.object(_chain, "_chain_path", moving_resolver):
            with m._lr_snapshot() as reads:
                served = reads.premise_chain()
                operands = {op: json.loads(okey) for op, okey in reads._reads}
            # THE MOVE REALLY HAPPENED: the resolver now answers the other file.
            self.assertEqual(moved_to, _chain._chain_path(),
                             "the resolver never moved, so this arm cannot "
                             "distinguish the cure from the defect")
        self.assertEqual([recorded], operands.get("premisechain"),
                         "the record does not name the file it opened: %r"
                         % (operands,))
        self.assertEqual([{"chain_index": 1}], served,
                         "the door recorded one file and served the contents "
                         "of another — the record names %r and the body read "
                         "the file the resolver moved to" % (recorded,))

    def test_the_receipt_door_SERVES_the_file_it_RECORDED(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone(failed) shares its observable with an unconditional positive on the SAME returned pair: the rows are asserted to be exactly ["one"], A's content, and the resolver is asserted to have moved to B
        """The same TOCTOU in the other door, and the same adversarial move.

        `receipt_rows` recorded `landreq.receipts_path()` and then called
        `landreq._receipt_rows()`, which calls `receipts_path()` AGAIN and hands
        the second answer to the event reader. One resolution for the record,
        another for the body."""
        from helm import web_land_model as m
        moved_to = self.ledger_at(self.scratch("B.jsonl"),
                                  [{"id": "nine", "topic": "helm.land"}])
        recorded = self.ledger_at(self.scratch("A.jsonl"),
                                  [{"id": "one", "topic": "helm.land"}])
        resolutions = []

        def moving_resolver():
            resolutions.append(1)
            return recorded if len(resolutions) == 1 else moved_to

        with mock.patch.object(landreq, "receipts_path", moving_resolver):
            with m._lr_snapshot() as reads:
                rows, failed = reads.receipt_rows()
                operands = {op: json.loads(okey) for op, okey in reads._reads}
            self.assertEqual(moved_to, landreq.receipts_path(),
                             "the resolver never moved, so this arm cannot "
                             "distinguish the cure from the defect")
        self.assertIsNone(failed)
        self.assertEqual([recorded], operands.get("receiptrows"),
                         "the record does not name the ledger it opened: %r"
                         % (operands,))
        self.assertEqual(["one"], [r.get("id") for r in rows],
                         "the door recorded one ledger and served the rows of "
                         "another")

    def test_the_chain_is_read_ONCE_for_the_count_the_head_AND_the_verdict(self):
        """The same-input DOUBLE READ, which is the half of blocker (1) that
        needed no clock to go wrong.

        `_lr_native_chain` read the chain for its count and head, then called
        `verify_chain()`, which read it AGAIN for the verdict and its "(N
        records)" wording. One card could therefore say 87 records and "chain
        verified (88 records)".

        THE OPENS ARE COUNTED, NOT THE PARSES, and that is the difference from
        the previous round's arm. It counted calls to `premise.chain_records` —
        a function the cure no longer calls at all, so it would have counted
        zero of everything and passed. `_open_once` is the one place a file
        descriptor is created, so counting it counts readings of the file."""
        from helm import web_land_model as m
        from helm.premise import _chain
        path = self.chain_at(self.scratch("attest-chain.jsonl"),
                             [{"chain_index": 1, "rec_hash": "", "prev": ""}])
        opens = []
        real = m._LrReadSet._open_once     # already the plain function

        def counted(target, **kw):
            # **kw, NOT a copied default: this double must forward whatever the
            # door passes (`nofollow=` is the live one) rather than re-decide
            # it. A double that pins the flag would keep passing while the door
            # it stands in for changed underneath it.
            opens.append(target)
            return real(target, **kw)

        with mock.patch.object(_chain, "_chain_path", lambda: path), \
             mock.patch.object(m._LrReadSet, "_open_once",
                               staticmethod(counted)), \
             mock.patch("helm.premise.verify_chain",
                        side_effect=lambda recs=None: (True, "")) as verify:
            with m._lr_snapshot():
                summary = m._lr_native_chain()
        self.assertEqual(1, summary["count"])       # the body really ran
        self.assertEqual([path], opens,
                         "the chain file was opened %d times to build ONE "
                         "card, so its fields can disagree with each other: %r"
                         % (len(opens), opens))
        self.assertEqual([1], [len(c.args) + len(c.kwargs)
                               for c in verify.call_args_list],
                         "verify_chain was called with no records, so it "
                         "re-opened the file this card had already read")

    def test_the_LEDGER_CONTENT_moves_the_witness_at_a_FROZEN_INODE(self):  # noqa: VACUOUS_ASSERTION — the assertNotEqual runs after two unconditional positives on the same observable: the served rows are asserted to be (["one"], None) and the first witness a str, then an UNCHANGED ledger is asserted to re-collect the IDENTICAL witness
        """The ledger seam, which is blocker (1)'s shape in the file nobody had
        enumerated — found by WIDENING the bypass scan rather than by reading.

        The receipts ledger's path was in the record and its inode was in the
        record; its ROWS, which every recent-lands badge is a function of, were
        read straight out of `landreq`. Same one-inode isolation, same claim."""
        from helm import web_land_model as m
        path = self.ledger_at(self.scratch("receipts.jsonl"),
                              [{"id": "one", "topic": "helm.land"}])
        before = os.stat(path).st_ino
        with mock.patch.object(landreq, "receipts_path", lambda: path):
            with m._lr_snapshot() as reads:
                self.assertEqual((["one"], None),
                                 ([r["id"] for r in reads.receipt_rows()[0]],
                                  reads.receipt_rows()[1]))
                first = reads.witness()
            self.assertIsInstance(first, str)
            with m._lr_snapshot() as reads:
                reads.receipt_rows()
                self.assertEqual(first, reads.witness())
            self.ledger_at(path, [{"id": "one", "topic": "helm.land"},
                                  {"id": "two", "topic": "helm.land"}])
            self.assertEqual(before, os.stat(path).st_ino,
                             "the rewrite moved the inode, so this arm no "
                             "longer isolates the CONTENT from the file")
            with m._lr_snapshot() as reads:
                reads.receipt_rows()
                moved = reads.witness()
        self.assertNotEqual(first, moved,
                            "a receipt was appended and the witness could not "
                            "see it, so every badge on the card can go stale "
                            "under a fingerprint that still reads fresh")

    def test_the_LEDGER_PARSE_splits_on_NEWLINE_and_nothing_else(self):
        """The split that had to come OUT of `checked_events` so the door could
        own its descriptor — pinned here because this cure is what moved it, and
        because the tempting one-liner is wrong in a way no ledger arm would see.

        `bytes.splitlines` also breaks on a bare b"\\r". MEASURED, because the
        difference is not the one prose usually claims: unlike `str`, bytes
        splitlines leaves \\v, \\f and the information separators alone, and CR
        is the whole gap. A COMPLETE row carrying one therefore becomes two —
        the first half loses its terminator and is dropped as an unterminated
        tail, the second parses as a whole row — so corruption that must POISON
        a strict read folds away silently instead.

        THE UNTERMINATED TAIL IS THE OTHER HALF and must keep behaving: it is
        outside the durability boundary and is ignored in both modes, which is
        what makes the CR case a defect rather than a matter of taste."""
        clean = b'{"id": "a"}\n{"id": "b"}\n'
        rows, corrupt = eventledger.checked_rows(clean, strict=True)
        # UNCONDITIONAL POSITIVE CONTROL: this parser really parses complete
        # rows, so the corruption asserted below is the SPLIT and not a reader
        # that rejects everything.
        self.assertEqual((["a", "b"], None),
                         ([r["id"] for r in rows], corrupt))
        rows, corrupt = eventledger.checked_rows(b'{"id": "a"}\r{"id": "b"}\n',
                                                 strict=True)
        self.assertEqual([], rows)
        self.assertTrue(
            corrupt and corrupt.startswith(eventledger.CORRUPT_PREFIX),
            "one complete malformed row was split into a dropped tail and a "
            "clean row, so a corrupt ledger read as a good one: %r" % (corrupt,))
        rows, corrupt = eventledger.checked_rows(b'{"id": "a"}\n{"id": "b"',
                                                 strict=True)
        self.assertEqual((["a"], None), ([r["id"] for r in rows], corrupt),
                         "an unterminated tail stopped being ignored, which is "
                         "the durability boundary moving")

    def test_an_UNREADABLE_or_CORRUPT_receipt_ledger_leaves_the_record_BLIND(self):  # noqa: VACUOUS_ASSERTION — each assertIsNone runs after an unconditional positive control on the same observable: the identical reader over a READABLE ledger is asserted to produce a real witness string, and each failing regime is additionally asserted to carry a failure marker
        """helm COULD NOT LOOK, so this snapshot must never be certified.

        THE PREVIOUS CURE RESTED ON AN ACCIDENT. It relied on `_receipt_rows`'
        failure marker having module-private OBJECT keys, so `json.dumps` raised,
        so the identity came out None. The outcome was right and the mechanism
        was a coincidence any future respelling of the marker would remove
        silently. The door now answers `_Blind` with a named reason, and both
        failure regimes are driven through REAL files rather than a double.

        AND AN ABSENT LEDGER IS NOT EITHER OF THEM. A missing file is a MEASURED
        "no receipts are recorded" and must still project, or the card would go
        UNKNOWN on every box that has not landed anything yet."""
        from helm import web_land_model as m
        good = self.ledger_at(self.scratch("receipts.jsonl"),
                              [{"id": "one", "topic": "helm.land"}])
        with mock.patch.object(landreq, "receipts_path", lambda: good):
            with m._lr_snapshot() as reads:
                reads.receipt_rows()
                self.assertIsInstance(reads.witness(), str,
                                      "a readable ledger did not project, so "
                                      "the Nones below are not the failure")
        # ABSENT: measured, and it must still certify.
        absent = self.scratch("gone.jsonl")
        with mock.patch.object(landreq, "receipts_path", lambda: absent):
            with m._lr_snapshot() as reads:
                self.assertEqual(([], None), reads.receipt_rows())
                self.assertIsInstance(reads.witness(), str,
                                      "a ledger that is measurably not there "
                                      "was treated as a failed instrument")
        # UNREADABLE: the path is not a regular file at all.
        blocked = self.scratch("blocked.jsonl")
        os.makedirs(blocked)
        with mock.patch.object(landreq, "receipts_path", lambda: blocked):
            with m._lr_snapshot() as reads:
                rows, failed = reads.receipt_rows()
                self.assertEqual([], rows)
                self.assertTrue(failed, "an unreadable ledger reported no "
                                        "failure, so the card would read it "
                                        "as an empty one")
                self.assertIsNone(reads.witness(),
                                  "an unreadable receipt ledger left the "
                                  "snapshot persistable")
        # CORRUPT: a COMPLETE row the strict parser rejects.
        torn = self.scratch("torn.jsonl")
        os.makedirs(os.path.dirname(torn), exist_ok=True)
        with open(torn, "w", encoding="utf-8") as fh:
            fh.write('{"id": "one"}\n{"no": "id"}\n')
        with mock.patch.object(landreq, "receipts_path", lambda: torn):
            with m._lr_snapshot() as reads:
                rows, failed = reads.receipt_rows()
                self.assertEqual([], rows)
                self.assertTrue(failed, "a corrupt ledger was folded as an "
                                        "empty one")
                self.assertIsNone(reads.witness(),
                                  "a corrupt receipt ledger left the snapshot "
                                  "persistable")

    def test_an_UNREADABLE_chain_is_UNKNOWN_on_the_card_not_ZERO(self):
        """The premise half of the same split, and the reason `premise_chain`
        answers None rather than [].

        `premise.chain_records` swallows OSError and answers [], so a chain helm
        could not read rendered as a chain with no records in it — a silent
        downgrade on the RECORD row. The distinction used to be recovered from a
        separate `identity("premise")` stat, which is a second resolution of the
        name and therefore a second chance to describe a different file. It now
        falls out of the one reading."""
        from helm import web_land_model as m
        from helm.premise import _chain
        blocked = self.scratch("chain-dir")
        os.makedirs(blocked)
        with mock.patch.object(_chain, "_chain_path", lambda: blocked):
            with m._lr_snapshot() as reads:
                summary = m._lr_native_chain()
                self.assertIsNone(reads.witness(),
                                  "a chain helm could not read left the "
                                  "snapshot persistable")
        self.assertIsNone(summary["count"])
        self.assertIn("could not be read", summary["unavailable"] or "")
        # AND THE MEASURED EMPTY IS NOT THAT: a chain file that is simply not
        # there is zero records, certifiable, and must not read as UNKNOWN.
        missing = self.scratch("never-written.jsonl")
        with mock.patch.object(_chain, "_chain_path", lambda: missing):
            with m._lr_snapshot() as reads:
                summary = m._lr_native_chain()
                self.assertIsInstance(reads.witness(), str)
        self.assertEqual(0, summary["count"])
        self.assertIsNone(summary["unavailable"])

    # ── BLOCKER (2): a mutation that outlived the reading it replaced ──────

    def test_a_POPPED_slot_re_read_as_a_DIFFERENT_world_goes_BLIND(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone is preceded on the SAME observable and the same object by an unconditional assertIsInstance proving this record DOES project, and by the agreeing-re-read regime which must still project
        """Blocker (2), reproduced in its measured form.

        `_landing_proofs._pin` POPS a trunkrefs slot on a failed probe so the
        next carrier row can retry it. `_observe` returned early on an
        already-recorded slot, so the retry's answer went into the BODY and the
        failure's stayed in the WITNESS: measured as a body carrying
        ("refs/heads/main", None) under a witness still saying (null, null).

        AN AGREEING RE-READ MUST STILL PROJECT. That is the half that keeps the
        cure from being "any pop poisons the record", which would disable the
        cache for every repository whose trunk probe ever blinks."""
        from helm import web_land_model as m
        agreeing = m._LrReadSet()
        agreeing["/g"] = ["refs/heads/main", None]
        before = agreeing.witness()
        agreeing.pop("/g", None)
        agreeing["/g"] = ["refs/heads/main", None]
        self.assertIsInstance(before, str)
        self.assertEqual(before, agreeing.witness(),
                         "a pop and an IDENTICAL re-read poisoned the record, "
                         "so a blinking probe would disable the cache")
        diverging = m._LrReadSet()
        diverging["/g"] = [None, None]
        self.assertIsInstance(diverging.witness(), str,
                              "the failed reading did not project at all, so "
                              "the None below is not the disagreement")
        diverging.pop("/g", None)
        diverging["/g"] = ["refs/heads/main", None]
        self.assertIsNone(diverging.witness(),
                          "the body took the retry's trunk and the witness "
                          "kept the failure's, which is round four's measured "
                          "divergence exactly")

    def test_EVERY_dict_WRITE_DOOR_lands_in_the_record(self):
        """`dict.update`, `dict.setdefault` and `dict.__ior__` are implemented
        in C against the concrete storage and DO NOT call an overridden
        `__setitem__`. Each was therefore a write into the projection cache that
        the recorder never saw.

        MEASURED THROUGH THE CLASSIFIER RATHER THAN BY MOCKING IT: an
        unclassified key must make the record blind, and it can only do that if
        the write reached `__setitem__`. The enumeration is the point — a fourth
        writer added to `dict` would show up here as a name with no arm."""
        from helm import web_land_model as m
        writers = {
            "setitem": lambda d: d.__setitem__(("unclassified",), 1),
            "update": lambda d: d.update({("unclassified",): 1}),
            "setdefault": lambda d: d.setdefault(("unclassified",), 1),
            "ior": lambda d: d.__ior__({("unclassified",): 1}),
        }
        for name, write in writers.items():
            with self.subTest(writer=name):
                reads = m._LrReadSet()
                reads["/g"] = ["refs/heads/main", None]
                self.assertIsInstance(reads.witness(), str,
                                      "the classified write did not project, "
                                      "so the None below is not the bypass")
                write(reads)
                self.assertIsNone(reads.witness(),
                                  "%s put an unclassified read into the "
                                  "projection cache without reaching the "
                                  "recorder" % name)
                self.assertIn(("unclassified",), reads,
                              "%s did not actually write, so the blind above "
                              "proves nothing about a real bypass" % name)

    # ── BLOCKER (3): an abbreviation is a prefix search, not a name ────────

    def test_an_ABBREVIATED_sha_is_SERVED_and_never_CERTIFIED(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone on the witness runs after an unconditional positive control on the same door and the same record: the FULL object name is asserted to pass AND to leave a real witness string
        """Blocker (3). A seven-character prefix resolves to whatever
        single object currently starts with it, so the same question answers
        ANCESTOR today and UNKNOWN once a second object shares the prefix —
        measured as one witness over two proofs.

        THE TWO REFUSALS ARE DIFFERENT AND THE DIFFERENCE IS THE DESIGN. A ref
        name must never be READ here at all: git would answer it just as
        happily, about whatever it points at. An abbreviation names a real
        object NOW, so the read PROCEEDS and the surface renders — what it may
        never do is enter the witness. Serving and certifying are separate
        powers, which is `_Blind`'s whole split, and an arm that only checked
        the witness would not notice a cure that broke the card."""
        from helm import web_land_model as m
        full = "0" * 40
        pinned = m._LrReadSet()
        pinned["/g"] = ["refs/heads/main", None]
        self.assertTrue(pinned._pinned((full,)),
                        "the door refused a full object name, so the checks "
                        "below discriminate nothing")
        self.assertIsInstance(pinned.witness(), str)
        for name, operand in (("sha256", "0" * 64), ("abbrev-7", "0" * 7),
                              ("abbrev-12", "0" * 12), ("abbrev-39", "0" * 39)):
            with self.subTest(operand=name):
                reads = m._LrReadSet()
                reads["/g"] = ["refs/heads/main", None]
                self.assertTrue(reads._pinned((operand,)),
                                "%s was REFUSED rather than served — an "
                                "abbreviation names a real object now and the "
                                "card must still render it" % name)
                if len(operand) in (40, 64):
                    self.assertIsInstance(reads.witness(), str,
                                          "a full object name was treated as "
                                          "an abbreviation")
                    continue
                self.assertIsNone(reads.witness(),
                                  "%s left the snapshot persistable, so a "
                                  "proof read off a prefix search can be "
                                  "restored as current" % name)
        moving = m._LrReadSet()
        moving["/g"] = ["refs/heads/main", None]
        self.assertFalse(moving._pinned(("refs/remotes/origin/main",)),
                         "a moving ref name was walked as an immutable object")

    def test_an_ABBREVIATED_tip_RESOLVES_and_the_RESOLUTION_is_in_the_witness(self):
        """WHERE THE ABBREVIATION ACTUALLY COMES FROM, and why refusing it was
        not enough on its own.

        Blocker (3) is not hypothetical and it is not rare: the FOLD GRAMMAR
        carries a 12-hex tip, so every recent-lands row asks its landing
        question about a prefix. Measured on the fab — the abbreviation refusal
        alone turned the witness None on every real build, which would have made
        this lane ship a cache that could never warm. Requiring full identity
        means RESOLVING the prefix through the door and recording what it found,
        which is strictly stronger than refusing: the prefix is the operand, the
        full object id is the answer, and a prefix that stops denoting this
        object is a term that changed.

        AN UNRESOLVABLE PREFIX IS MEASURED, NOT BLIND. It is `_Blind`'s own
        split — "no object here starts with this" is a fact a later reading can
        contradict, not a failed instrument — and it has to be, or a single fold
        naming a tip this clone lacks would stop the whole card from ever
        caching."""
        from helm import web_land_model as m
        full, short = self.side, self.side[:12]
        self.assertEqual(40, len(full))     # the fixture really is a full sha
        with m._lr_snapshot() as reads:
            got = reads.object_id(self.repo_git, short)
            witness = reads.witness()
        self.assertEqual(full, got,
                         "the abbreviated tip did not resolve to its object, "
                         "so every recent-lands row asks about a prefix")
        self.assertIsInstance(witness, str,
                              "resolving a prefix left the record blind, which "
                              "is the failure that would ship a cache that "
                              "never warms")
        self.assertIn(full, witness,
                      "the RESOLUTION is not in the witness, so a prefix that "
                      "goes ambiguous re-answers under an unmoved fingerprint")
        with m._lr_snapshot() as reads:
            self.assertEqual("", reads.object_id(self.repo_git, "0" * 12))
            self.assertIsInstance(
                reads.witness(), str,
                "an unresolvable prefix went BLIND rather than measured, so "
                "one fold naming an absent tip stops the card caching at all")


if __name__ == "__main__":
    unittest.main()


class ThePinnedReadHoldsTheProjectionsClockTest(LrApiBase):
    """THE MECHANISM EVERY EXACT-DWELL ARM NOW RESTS ON, ASSERTED.

    Several arms read a backdated row and assert its dwell EQUALS the offset
    they backdated by. That is only true if the instant the fixture stamped
    from and the instant the projection renders at are the same one, and
    before `lr_at` they were two reads of a wall clock: usually equal, and on
    a loaded fab node one second apart. A whole suite went red on a lane that
    touches neither this file nor the projection (helm task/2232).

    A TOLERANCE WOULD HAVE SURVIVED THE CLOCK BY WIDENING WHAT THE ARMS
    ACCEPT. Pinning makes the equality exact by construction instead, and
    this class is what says the pin is real rather than decorative — without
    it, `lr_at` could ignore its argument entirely and every arm using it
    would still pass, because the unpinned reading is usually right.
    """

    def aged_row(self, seconds=3600):
        row = self.dispatch(ref=self.side)
        dispatches._mark_delivered(row["id"], "post-1")
        return self.age(row["id"], seconds)

    def test_the_pinned_clock_MOVES_the_dwell_by_exactly_the_offset(self):
        """The positive control: the projection really does read the pinned
        value, so a dwell rendered under it is a measurement against that
        instant and not against now."""
        base = self.aged_row()
        self.assertTrue(base, "age() handed back no instant to pin to, so "
                              "every pinned read below is pinned to nothing")
        seen = []
        seen.append(self.one(self.lr_at(base))["dwell_s"])
        seen.append(self.one(self.lr_at(base + 60))["dwell_s"])
        seen.append(self.one(self.lr_at(base + 3600))["dwell_s"])
        self.assertEqual([3600, 3660, 7200], seen,
                         "the pinned clock did not reach the projection — "
                         "lr_at is ignoring its argument and every exact "
                         "dwell assertion in this file is back to racing a "
                         "wall clock: %r" % (seen,))

    def test_the_SAME_instant_reads_the_SAME_dwell_every_time(self):
        """The determinism the exact assertions depend on, stated on its own.

        The arm above proves the clock is HONOURED; this proves that honouring
        it is STABLE, which is the half that says a green here will still be
        green on a node under load.
        """
        base = self.aged_row()
        seen = [self.one(self.lr_at(base))["dwell_s"] for _ in range(4)]
        self.assertEqual([3600, 3600, 3600, 3600], seen,
                         "four reads at one instant disagreed, so the dwell "
                         "still depends on something other than the clock it "
                         "was given: %r" % (seen,))

    def test_a_clock_ONE_SECOND_ON_moves_the_unpinned_read_and_not_the_pinned(self):
        """THE DEFECT, STAGED DETERMINISTICALLY, which is the only way to show
        it at all: the failure that reddened a suite is a race, so it cannot
        be reproduced by running the old arm and hoping.

        A clock held ONE SECOND past the instant the row was staged from is
        exactly what a loaded node looks like from inside this fixture. Under
        it the UNPINNED read renders one second more than the offset it was
        staged with -- the 10201-versus-10200 shape, on demand -- while the
        pinned read is unmoved.

        THE CLOCK IS A CONSTANT AND NOT A COUNTER, deliberately. A counter
        would make this arm assert how many times the read path calls
        time.time(), which is a fact about code it is not testing and would
        redden on any refactor that adds or removes one call. A constant is
        one second ahead however often it is asked.

        Asserting the unpinned value is safe HERE and nowhere else, because
        here the clock is controlled rather than raced.
        """
        base = self.aged_row()
        one_on = float(base) + 1.0

        seen = []
        with mock.patch.object(landreq.time, "time", return_value=one_on):
            seen.append(self.one(self.lr())["dwell_s"])
        self.assertEqual([3601], seen,
                         "the staged clock did not reach the projection, so "
                         "this arm is not staging the race it claims to and "
                         "the pinned reading below proves nothing: %r"
                         % (seen,))
        del seen[:]
        with mock.patch.object(landreq.time, "time", return_value=one_on):
            seen.append(self.one(self.lr_at(base))["dwell_s"])
        self.assertEqual([3600], seen,
                         "the pinned read moved under a clock one second on, "
                         "so pinning is not holding the instant and every "
                         "exact dwell assertion in this file still races: %r"
                         % (seen,))


class RebuildFiresOnChangeNotOnAClockTest(LrApiBase):
    """THE WEB PROCESS' OWN PROJECTION LOOP, which nothing bounded.

    MEASURED on the live server: 100 percent of a core since start, 1.36 GB
    resident, and a four second `strace -c` showing 44 vfork and 440 execve —
    about eleven subprocess spawns a second, continuously, on a box whose
    per-toolcall hooks have a two second budget.

    THE FLOOR WAS NOT THE BUG AND RAISING IT WOULD NOT HAVE HELPED. `_cached_swr`
    asks only how OLD the last body is; the projection costs MORE than the floor
    (measured 9.6 to 40s against a 30s floor), so by the time one rebuild stored
    its body the entry was already due again and the next poll started another.
    The loop is therefore continuous at ANY floor shorter than the build. What
    bounds it is asking a different question: has anything this body is computed
    FROM actually moved? Measured over 60s on the live box, the answer was no —
    the dispatch ledger had last been written twenty minutes earlier.
    """

    def reset(self, key="fp"):
        for state in (web_cache._qstate, web_cache._qinflight,
                      web_cache._qcold, web_cache._qfresh,
                      web_cache._qverified, web_cache._qcapkick):
            state.pop(key, None)
        return key

    def burst(self, key, builds, mark, n=12, age=None, unchanged_max=600):
        """`n` requests against an entry already past the floor, each one
        carrying the same fingerprint."""
        for _ in range(n):
            web_cache._qstate[key] = (time.time() - (age or 45),
                                      {"loops": ["cached"]})
            web_cache._cached_swr(
                key, 30, 600, lambda: builds.append(1) or {"loops": ["fresh"]},
                fingerprint=lambda: mark[0], unchanged_max=unchanged_max)
            _join_swr(key)
        return builds

    def test_a_request_burst_below_the_floor_triggers_NO_rebuild(self):
        """THE CURE, AND ITS OWN POSITIVE CONTROL IN ONE ARM.

        The first stale request RECORDS the fingerprint and rebuilds — nothing
        may be skipped on a body whose inputs were never compared. Every later
        request over the same unmoved inputs is served from the cache, however
        old the entry gets, up to the cap.

        LOAD-BEARING MUTATION: delete the `_qfresh.get(key) == mark` clause in
        `_cached_swr`.
          -> AssertionError: 12 rebuilds for 12 polls over unmoved inputs
        """
        key = self.reset()
        builds, mark = [], ["identity-A"]
        self.burst(key, builds, mark, n=12)
        # THE CONTROL IS THE FIRST ONE: a run that rebuilt ZERO times would
        # satisfy "no storm" for the wrong reason entirely — a build that never
        # happens is not a cache that works.
        self.assertEqual(len(builds), 1,
                         "12 polls over unmoved inputs cost %d rebuilds"
                         % len(builds))

    def test_a_MOVED_input_rebuilds_on_the_very_next_poll(self):
        """THE OTHER HALF, and the one that says this is a cache and not a pin.
        A fingerprint that differs from the recorded one rebuilds immediately —
        it does not wait for the cap, and it does not wait for the floor to
        lapse a second time."""
        key = self.reset()
        builds, mark = [], ["identity-A"]
        self.burst(key, builds, mark, n=5)
        self.assertEqual(len(builds), 1)
        mark[0] = "identity-B"
        self.burst(key, builds, mark, n=1)
        self.assertEqual(len(builds), 2,
                         "the ledger moved and the projection did not rebuild")

    def test_an_UNMOVED_fingerprint_still_expires_at_the_cap(self):
        """NO MATCH MAY HOLD THE BOARD FOREVER, and this is the bound that
        makes the fingerprint safe to have at all.

        The killed design — an mtime memo inside `_lr_build` — had no bound: a
        `chmod 000` moves neither mtime nor size, so it served the last healthy
        board indefinitely over a ledger nobody could read. The cap answers
        that class WITHOUT having to enumerate every input: whatever this
        fingerprint failed to consider (a trunk that fast-forwards writes
        nothing to any ledger) is picked up within one window.

        LOAD-BEARING MUTATION: drop the `age < unchanged_max` clause.
          -> AssertionError: an unmoved fingerprint held the board past its cap
        """
        key = self.reset()
        builds, mark = [], ["identity-A"]
        self.burst(key, builds, mark, n=3, age=45, unchanged_max=300)
        self.assertEqual(len(builds), 1)
        self.burst(key, builds, mark, n=1, age=301, unchanged_max=300)
        self.assertEqual(len(builds), 2,
                         "an unmoved fingerprint held the board past its cap")

    def test_a_caller_with_no_fingerprint_keeps_the_OLD_behaviour(self):
        """Every other key on this shared cache — ready, creds, burn, rooms —
        supplies neither argument and must be untouched."""
        key = self.reset()
        builds = []
        for _ in range(3):
            web_cache._qstate[key] = (time.time() - 45, {"loops": ["cached"]})
            web_cache._cached_swr(key, 30, 600,
                                  lambda: builds.append(1) or {"loops": ["x"]})
            _join_swr(key)
        self.assertEqual(len(builds), 3,
                         "a caller that supplies no fingerprint had its "
                         "rebuild skipped")

    def test_a_fingerprint_that_CANNOT_BE_TAKEN_rebuilds(self):
        """None is NO IDENTITY, never a matching one. A reader that cannot say
        what it read must rebuild — the direction that costs work rather than
        the one that pins a body."""
        key = self.reset()
        builds = []

        def boom():
            raise OSError("the ledger directory went away")

        for _ in range(3):
            web_cache._qstate[key] = (time.time() - 45, {"loops": ["cached"]})
            web_cache._cached_swr(key, 30, 600,
                                  lambda: builds.append(1) or {"loops": ["x"]},
                                  fingerprint=boom, unchanged_max=600)
            _join_swr(key)
        self.assertEqual(len(builds), 3,
                         "a fingerprint that raised was treated as a match")


    def test_a_confirmed_poll_stamps_the_reading_and_a_moved_one_does_not(self):  # noqa: VACUOUS_ASSERTION — verified_at is asserted NOT None (and >= before) on the confirmed poll between the two absences, on the same key through the same call
        """THE READING'S AGE IS THE FINGERPRINT'S, NOT THE BUILD'S. A poll whose
        fingerprint matches the one the served body was built under is a read
        of every input, and `verified_at` says when. Measured on the owner's
        console before this: the cap plus a rebuild aged a body to 370s against
        the cards' 180s bound, so every land-pipeline band read STALE for most
        of each cycle over a ledger nobody had written to.

        LOAD-BEARING MUTATION: drop the `_qverified[key] = ...` line.
          -> AssertionError: a confirmed poll left no reading
        """
        key = self.reset()
        builds, mark = [], ["identity-A"]
        self.burst(key, builds, mark, n=1)          # records the mark, rebuilds
        self.assertEqual(len(builds), 1)
        self.assertIsNone(web_cache.verified_at(key),
                          "a rebuild's own body is not a confirmation")
        before = time.time()
        web_cache._qstate[key] = (before - 45, {"loops": ["cached"]})
        web_cache._cached_swr(key, 30, 600, lambda: builds.append(1),
                              fingerprint=lambda: mark[0], unchanged_max=600)
        stamp = web_cache.verified_at(key)
        self.assertIsNotNone(stamp, "a confirmed poll left no reading")
        self.assertGreaterEqual(stamp, before)
        self.assertEqual(len(builds), 1)
        # THE CONTROL: a MOVED fingerprint is not a confirmation of the body
        # it is served with, and the rebuild it kicks clears the reading.
        mark[0] = "identity-B"
        web_cache._qstate[key] = (time.time() - 45, {"loops": ["cached"]})
        web_cache._cached_swr(key, 30, 600,
                              lambda: builds.append(1) or {"loops": ["fresh"]},
                              fingerprint=lambda: mark[0], unchanged_max=600)
        _join_swr(key)
        self.assertEqual(len(builds), 2)
        self.assertIsNone(web_cache.verified_at(key),
                          "a moved input left the old reading standing")

    def test_a_rebuild_in_flight_is_not_confirmed_by_the_mark_that_kicked_it(self):  # noqa: VACUOUS_ASSERTION — the positive control follows on the same observable: the same poll once the rebuild has stored reads NOT None
        """`_qfresh` records the mark AT THE DECISION, so while the rebuild runs
        the served body predates it. A poll that matches that mark has
        confirmed the NEW inputs, not the OLD body; stamping it would age a
        body that missed a write from the write's own fingerprint."""
        key = self.reset()
        gate, started = threading.Event(), threading.Event()

        def slow():
            started.set()
            gate.wait(10)
            return {"loops": ["fresh"]}
        mark = ["identity-A"]
        web_cache._qstate[key] = (time.time() - 45, {"loops": ["cached"]})
        web_cache._cached_swr(key, 30, 600, slow,
                              fingerprint=lambda: mark[0], unchanged_max=600)
        self.assertTrue(started.wait(5))
        # the same mark, the rebuild still running: served, NOT confirmed
        web_cache._qstate[key] = (time.time() - 45, {"loops": ["cached"]})
        web_cache._cached_swr(key, 30, 600, slow,
                              fingerprint=lambda: mark[0], unchanged_max=600)
        self.assertIsNone(web_cache.verified_at(key),
                          "a body built before the mark was confirmed by it")
        gate.set()
        _join_swr(key)
        # POSITIVE CONTROL on the same call: once the rebuild has stored, the
        # very same poll IS a confirmation.
        with web_cache._qlock:
            ent = web_cache._qstate[key]
        web_cache._cached_swr(key, 0, 600, slow,
                              fingerprint=lambda: mark[0], unchanged_max=600)
        self.assertIsNotNone(web_cache.verified_at(key))
        # ...and a stamp is bound to the entry it confirmed: replace the entry
        # and the old reading no longer speaks for the new body.
        web_cache._qstate[key] = (ent[0] - 1, {"loops": ["restored"]})
        self.assertIsNone(web_cache.verified_at(key))

    def test_a_CAP_kicked_rebuild_keeps_confirming_and_a_MOVED_one_does_not(self):
        """WHY THE REBUILD WAS KICKED DECIDES WHETHER THE BODY IS CONFIRMED.

        A rebuild the CAP kicked under an unchanged mark is replacing a body
        that was built under that very fingerprint, so nothing it could have
        missed has been written and a matching poll has read every input. A
        rebuild a MOVED mark kicked is replacing a body that genuinely
        predates a write, and no poll may confirm that one.

        LOAD-BEARING MUTATION: drop the `_qcapkick.get(key) is True` clause.
          -> AssertionError: a cap-kicked rebuild blanked the reading
        """
        key = self.reset()
        mark = ["identity-A"]
        cap_gate, cap_started = threading.Event(), threading.Event()
        moved_gate, moved_started = threading.Event(), threading.Event()

        def slow(gate, started):
            def go():
                started.set()
                gate.wait(10)
                return {"loops": ["fresh"]}
            return go

        # A body built under identity-A, then an entry past the cap carrying
        # the SAME identity: the rebuild this kicks is the cap's.
        self.burst(key, [], mark, n=1)
        web_cache._qstate[key] = (time.time() - 700, {"loops": ["cached"]})
        web_cache._cached_swr(key, 30, 6000, slow(cap_gate, cap_started),
                              fingerprint=lambda: mark[0], unchanged_max=600)
        self.assertTrue(cap_started.wait(5))
        self.assertIn(key, web_cache._qinflight)
        before = time.time()
        web_cache._qstate[key] = (before - 45, {"loops": ["cached"]})
        web_cache._cached_swr(key, 30, 6000, slow(cap_gate, cap_started),
                              fingerprint=lambda: mark[0], unchanged_max=600)
        stamp = web_cache.verified_at(key)
        self.assertIsNotNone(stamp, "a cap-kicked rebuild blanked the reading")
        self.assertGreaterEqual(stamp, before)
        cap_gate.set()
        _join_swr(key)
        # THE CONTROL, on the same key through the same call: the input moves,
        # the rebuild it kicks is a write's, and a poll carrying the write's
        # own fingerprint confirms the body that missed it of nothing.
        mark[0] = "identity-B"
        web_cache._qstate[key] = (time.time() - 45, {"loops": ["cached"]})
        web_cache._cached_swr(key, 30, 6000, slow(moved_gate, moved_started),
                              fingerprint=lambda: mark[0], unchanged_max=600)
        self.assertTrue(moved_started.wait(5))
        web_cache._qstate[key] = (time.time() - 45, {"loops": ["cached"]})
        web_cache._cached_swr(key, 30, 6000, slow(moved_gate, moved_started),
                              fingerprint=lambda: mark[0], unchanged_max=600)
        self.assertIsNone(web_cache.verified_at(key),
                          "a body that missed a write was confirmed by the "
                          "write's own fingerprint")
        moved_gate.set()
        _join_swr(key)


class LedgerFingerprintTest(LrApiBase):
    """THE IDENTITY ITSELF, AND THE HOLE A SHORTER TERM OPENS.

    A term made of mtime and size alone cannot see a `chmod 000`, which moves
    NEITHER — so a memo built on one serves the last healthy board, every row
    green and nothing on screen changed, over a ledger that can no longer be
    read at all. These arms exist so that the term cannot be shortened back."""

    def fingerprint(self):
        return web_land._lr_fingerprint()

    def test_a_CHMOD_000_ledger_changes_the_fingerprint(self):
        """THE EXACT DEFECT THAT KILLED THE FIRST MEMO, as an arm.

        LOAD-BEARING MUTATION: drop `st_mode` and `st_ctime_ns` from the term.
          -> AssertionError: chmod 000 left the fingerprint unchanged
        """
        model = web_land_model_mod()
        path = model._lr_ledger_paths()[0]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("")
        before = self.fingerprint()
        # POSITIVE CONTROL FIRST: the fingerprint must be a real, non-empty
        # value over a readable ledger, or every inequality below holds for the
        # wrong reason.
        self.assertTrue(before, "the fingerprint answered nothing at all")
        os.chmod(path, 0)
        try:
            self.assertNotEqual(before, self.fingerprint(),
                                "chmod 000 left the fingerprint unchanged — "
                                "the exact hole that killed the first mtime "
                                "memo")
        finally:
            # RESTORED HERE, NOT IN A CLEANUP. `doCleanups` runs AFTER
            # `tearDown`, which has already removed the whole temporary home,
            # so a chmod registered there raises FileNotFoundError and turns a
            # passing arm into an error about the fixture.
            os.chmod(path, 0o644)

    def test_a_VANISHED_ledger_changes_the_fingerprint(self):
        """A ledger that disappears is a CHANGED identity, not a missing one:
        the term names the errno so absence is a value and not a gap."""
        model = web_land_model_mod()
        path = model._lr_ledger_paths()[0]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("")
        before = self.fingerprint()
        self.assertTrue(before)
        os.unlink(path)
        after = self.fingerprint()
        self.assertTrue(after, "a vanished ledger answered no identity")
        self.assertNotEqual(before, after)

    def test_an_APPEND_changes_the_fingerprint(self):
        """The ordinary case, and the control for every arm above: a ledger
        write must move this, or the rebuild would never fire at all."""
        model = web_land_model_mod()
        path = model._lr_ledger_paths()[0]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"x": 1}\n')
        before = self.fingerprint()
        # UNCONDITIONAL CONTROLS: a real non-empty identity that is STABLE
        # across two readings of an unchanged world. Without the second, an
        # instrument that answered a fresh value every call would satisfy the
        # inequality below while measuring nothing about the append.
        self.assertTrue(before, "the fingerprint answered nothing at all")
        self.assertEqual(before, self.fingerprint(),
                         "the fingerprint moved with no write, so it cannot "
                         "say anything about one")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"x": 2}\n')
        self.assertNotEqual(before, self.fingerprint(),
                            "a ledger append did not move the fingerprint, so "
                            "the board would never rebuild")

    def test_the_cap_sits_BELOW_the_hard_ttl(self):  # noqa: VACUOUS_ASSERTION — an ordering pin over three module constants has no run-time observable to control; the behaviour it protects is driven in RebuildFiresOnChangeNotOnAClockTest, whose cap arm reddens when the order is broken
        """A match that could hold until ANCIENT would put the next rebuild on
        the BLOCKING path, where the owner's poll waits the whole projection
        out — the flap `_cached_swr` exists to close."""
        model = web_land_model_mod()
        self.assertLess(model._LR_UNCHANGED_MAX_S, model._LR_HARD_TTL_S)
        self.assertGreater(model._LR_UNCHANGED_MAX_S, model._LR_FLOOR_S)

    def test_the_floor_is_not_below_the_measured_fill(self):  # noqa: VACUOUS_ASSERTION — an ordering pin over module constants has no run-time observable of its own; what the order BUYS is driven unconditionally in test_the_floor_spaces_rebuilds_by_the_floor_plus_the_build below
        """A FLOOR UNDER THE FILL BOUNDS NOTHING, and the arithmetic is the
        whole argument. `_swr_rebuild` stamps an entry when the build
        COMPLETES, so over a ledger that keeps moving the rebuilder's duty
        cycle is fill / (floor + fill). Put the floor under the fill and the
        body is due again before its successor can exist: the only thing left
        spacing the rebuilds is how long they take, which is the permanent
        background core this constant exists to prevent.

        AND THE COLD WAIT BELONGS UNDER THE FLOOR for a different reason —
        it is a bound on ONE blocked reader, not on the cost, and a wait as
        long as the spacing would hold a phone through a whole cycle.

        THE CEILING IS THE RENDERER'S, AND IT IS THE HALF THAT WAS UNPINNED.
        The pipeline band calls `cardStale(read_age_s, 45)` and `cardBoundS`
        is `cadence * 4`, so the card refuses to print its totals past a 180s
        reading. The worst age this floor produces is floor + fill — the body
        stands until the floor, the rebuild is kicked there, and the previous
        body stands one more fill while it runs. Raise either term far enough
        and the owner's headline comes off the card with nothing in this file
        to say why, which is how a cost cure becomes a blank band."""
        model = web_land_model_mod()
        self.assertGreaterEqual(model._LR_FLOOR_S, model._LR_MEASURED_FILL_S)
        self.assertLess(model._LR_COLD_WAIT_S, model._LR_FLOOR_S)
        self.assertLess(model._LR_FLOOR_S + model._LR_MEASURED_FILL_S, 180)

    def test_the_floor_spaces_rebuilds_by_the_floor_plus_the_build(self):
        """THE OBSERVABLE THE TWO ORDERING PINS ABOVE ARE ABOUT: what a floor
        BUYS is the gap between one build finishing and the next starting, and
        nothing in this file measured it.

        The build below sleeps, the fingerprint moves on every poll (so the
        rebuild gate is open every time, which is the live fleet's case), and
        the poll loop is tight. Under a floor LONGER than the build, the
        second build must not have started; under a floor SHORTER than it, it
        must have. Both arms run, so neither can pass by the subject never
        running at all.

        LOAD-BEARING MUTATION: make `_cached_swr` ignore `ttl` and treat every
        entry as stale.
          -> AssertionError: a floor longer than the build did not space it
        """
        from helm import web_cache
        build = 0.40

        def make(counter, started):
            def fn():
                counter.append(1)
                started.set()
                time.sleep(build)
                return {"loops": ["built"]}
            return fn

        def builds_within(key, floor, window):
            counter, started = [], threading.Event()
            fn = make(counter, started)
            mark = [0]

            def moving():
                mark[0] += 1
                return mark[0]

            web_cache._qstate[key] = (time.time() - 10000, {"loops": ["old"]})
            try:
                deadline = time.time() + window
                while time.time() < deadline:
                    web_cache._cached_swr(key, floor, 100000, fn,
                                          fingerprint=moving,
                                          unchanged_max=99999)
                    time.sleep(0.02)
                _join_swr(key)
                return len(counter)
            finally:
                # THE PROBE OWNS ITS KEY AND LEAVES NOTHING BEHIND. These are
                # module-level dicts shared by every test in this file, and a
                # key left in `_qinflight` is one another test can block on.
                for table in (web_cache._qstate, web_cache._qinflight,
                              web_cache._qfresh, web_cache._qcold,
                              web_cache._qcapkick, web_cache._qverified,
                              web_cache._qdated):
                    table.pop(key, None)
                web_cache._qrestored.discard(key)

        # POSITIVE CONTROL, UNCONDITIONAL AND FIRST: a floor well under the
        # build leaves the build itself as the only spacing, so a window of
        # three build-lengths must fit more than one. Without this arm the
        # assertion below would also pass over a `_cached_swr` that never
        # rebuilt at all.
        window = build * 3
        hot = builds_within("floor-probe-hot", 0.01, window)
        self.assertGreater(hot, 1,
                           "no floor at all produced %d build(s) in %.2fs, so "
                           "this probe cannot say anything about a floor"
                           % (hot, window))
        cold = builds_within("floor-probe-spaced", window + build, window)
        self.assertEqual(cold, 1,
                         "a floor longer than the build did not space it: %d "
                         "builds in %.2fs" % (cold, window))


class ClientDisconnectIsOneLineTest(unittest.TestCase):
    """70 TRACEBACKS IN TEN MINUTES, and they were their own cost.

    MEASURED in the live server's log: every one a `BrokenPipeError` raised
    from `_send`'s `wfile.write`, escaping into socketserver's default
    `handle_error`, which prints the full stack plus two rule lines per event.
    The console's own polls produce them — a card aborts its fetch at its
    deadline, or a tab closes mid-response — so the log the owner reads for
    real faults was almost entirely this, and the formatting is work a process
    already pinned at a full core was doing on top of everything else.

    THE ROUTING IS NOT ASSUMED. The live log's text is socketserver's own
    `handle_error` output, verbatim ("Exception occurred during processing of
    request from ..."), which is the evidence that an escaping write error
    reaches exactly this method."""

    def handled(self, exc):
        """Run `_Server.handle_error` inside a REAL raise of `exc` and return
        what it wrote to stderr. The exception is raised rather than
        constructed because `handle_error` reads `sys.exc_info()`; handing it a
        quiet process would measure a branch that never fires in production."""
        srv = web.make_server(0)
        self.addCleanup(srv.server_close)
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            try:
                raise exc
            except BaseException:
                srv.handle_error(("sock",), ("127.0.0.1", 41234))
        return buf.getvalue()

    def test_a_broken_pipe_is_ONE_LINE_and_no_stack(self):
        """LOAD-BEARING MUTATION: delete `_Server.handle_error`.
          -> AssertionError: a client disconnect printed a traceback"""
        out = self.handled(BrokenPipeError(32, "Broken pipe"))
        self.assertNotIn("Traceback", out,
                         "a client disconnect printed a traceback")
        self.assertEqual(len(out.strip().splitlines()), 1,
                         "a client disconnect printed %d lines: %r"
                         % (len(out.strip().splitlines()), out))
        self.assertIn("BrokenPipeError", out,
                      "the one line does not name what happened")
        self.assertIn("127.0.0.1", out,
                      "the one line does not name who went away")

    def test_a_connection_RESET_is_the_same_one_line(self):
        """The browser's other spelling of the same event."""
        out = self.handled(ConnectionResetError(104, "Connection reset by peer"))
        self.assertNotIn("Traceback", out)
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_a_REAL_fault_still_prints_its_whole_stack(self):
        """THE CONTROL, and it is the point of not simply swallowing here.
        This is the only place a handler fault is reported at all, so anything
        that is not a disconnect must keep everything socketserver gave it."""
        out = self.handled(ValueError("a real handler defect"))
        self.assertIn("Traceback", out,
                      "a real handler fault lost its stack — the quiet branch "
                      "is catching more than a disconnect")
        self.assertIn("ValueError", out)
        self.assertGreater(len(out.strip().splitlines()), 3)


def web_land_model_mod():
    from helm import web_land_model
    return web_land_model


def _join_swr(key, timeout=10):
    """Wait for whatever background rebuild this key may have kicked, so a
    request count is taken after the work it names has finished."""
    ev = web_cache._qinflight.get(key)
    if ev is not None:
        ev.wait(timeout)


class BuildingBandTest(WorkBase):
    """WHAT IS BUILDING — the band the owner's 13:26 reading asked for.

    HIS OBSERVABLE FIRST. He opened this card over a fleet with three live
    builds on it and read "0 in flight", then asked whether that could be
    true while work was plainly ongoing. Both halves were true: the card
    counts LAND REQUESTS, and a land request is filed at the END of a build. So
    these arms are about the population the card had no words for — a lane room
    with commits on it and a live claim, before anything is filed.

    AGAINST A REAL TREE, NOT A LIST OF DICTS. `WorkBase` mints an actual git
    repo, `helm work claim` mints actual rooms with actual leases, and the
    commits below are actual commits, so the predicate under test is measured
    against the same three readings it takes in production — the worktree
    registry, the claims table and ahead/behind against the trunk ref. A
    fixture that handed `_lr_building` pre-shaped rows would agree with
    whatever the function did and prove nothing about any of them."""

    def building(self, filed=()):
        return web_land._lr_building(set(filed), root=self.root)

    def commits(self, path, n):
        for i in range(n):
            with open(os.path.join(path, "w%d" % i), "w") as f:
                f.write("work %d\n" % i)
            self.assertEqual(_sh(path, "git", "add", "-A").returncode, 0)
            r = _sh(path, "git", "commit", "-q", "-m", "w%d" % i)
            self.assertEqual(r.returncode, 0, r.stderr)

    def lane(self, name, commits=0):
        """A claimed room, optionally with commits on its branch -> its path."""
        rc, _out, err = self.work("claim", name, "--seat", "s1")
        self.assertEqual(rc, 0, err)
        path = work.lane_path(self.root, name)
        if commits:
            self.commits(path, commits)
        return path

    def test_a_lane_with_three_commits_and_a_lease_renders_in_BUILDING(self):
        """THE ARM THE TASK NAMES, and the whole point of the band."""
        self.lane("live-build", commits=3)
        got = self.building()
        # MUST-HIT FIRST: the reading really happened. A refusal here would
        # otherwise reach the assertions below as an empty, confident board.
        self.assertIsNone(got["unavailable"],
                          "MUST-HIT: the lane rooms were not read at all, so "
                          "nothing below is about a measured population")
        self.assertEqual([r["lane"] for r in got["rows"]], ["live-build"])
        self.assertEqual(got["total"], 1)
        row = got["rows"][0]
        self.assertEqual(row["ahead"], 3)
        self.assertEqual(row["holder"], "s1")
        # the lease reading is the one `helm work list` prints, and it is a
        # REMAINING time: the claim record carries an expiry and a last-renewed
        # stamp, and no instant for when the lease was first taken.
        self.assertIsInstance(row["lease_remaining_s"], int)
        self.assertGreater(row["lease_remaining_s"], 0)

    def test_a_lane_sitting_at_trunk_is_NOT_building(self):
        """THE CONTROL. Without it every assertion above is satisfied by a
        function that lists every claimed room — which is `helm work list`,
        not a statement about work in progress."""
        self.lane("just-claimed")          # a room, a lease, no commits
        ahead = self.lane("live-build", commits=2)
        self.assertTrue(os.path.isdir(ahead))
        got = self.building()
        self.assertIsNone(got["unavailable"])
        # POSITIVE CONTROL ON THE SAME READ: the ahead lane IS listed, so the
        # absence below is a classification and not an empty board.
        self.assertEqual([r["lane"] for r in got["rows"]], ["live-build"])
        self.assertEqual(got["total"], 1)
        self.assertEqual(got["unmeasured"], 0,
                         "a lane at trunk was counted as unmeasurable rather "
                         "than as measured-and-not-building")

    def test_a_FILED_lane_leaves_building_and_enters_in_flight(self):
        """THE PARTITION. The two numbers sit beside each other on the owner's
        home band, so a lane counted in both is a fleet that looks twice as
        busy as it is — and the reason this band exists at all is that he
        cannot check its arithmetic against anything."""
        self.lane("filed-lane", commits=3)
        self.lane("unfiled-lane", commits=1)
        before = self.building()
        self.assertEqual(sorted(r["lane"] for r in before["rows"]),
                         ["filed-lane", "unfiled-lane"])
        after = self.building(filed=["filed-lane"])
        self.assertEqual([r["lane"] for r in after["rows"]], ["unfiled-lane"])
        self.assertEqual(after["total"], 1)

    def test_a_lane_that_landed_by_PATCH_IDENTITY_is_not_building(self):
        """A REBASED LAND leaves the lane AHEAD of the trunk by object id
        while every commit's content is on it, so `ahead` alone kept calling it
        building. The band now reads the verdict `helm work list` prints
        beside the row (`work.lanes_landed`) and leaves it out."""
        def commit(path, name):
            with open(os.path.join(path, name), "w") as f:
                f.write("%s — its own content\n" % name)
            self.assertEqual(_sh(path, "git", "add", "-A").returncode, 0)
            r = _sh(path, "git", "commit", "-q", "-m", name)
            self.assertEqual(r.returncode, 0, r.stderr)
            return _sh(path, "git", "rev-parse", "HEAD").stdout.strip()
        tip = commit(self.lane("rebased-land"), "rebased.txt")
        commit(self.lane("live-build"), "live.txt")
        # THE TRUNK MOVES FIRST, so the pick lands on a different parent and
        # mints a different object — the rebased-land shape. Picked onto the
        # lane's own parent in the same second, git mints the SAME object and
        # the lane is simply an ancestor.
        with open(os.path.join(self.root, "trunk-moved.txt"), "w") as f:
            f.write("an unrelated land\n")
        for argv in (("add", "-A"), ("commit", "-q", "-m", "unrelated land")):
            r = subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", *argv],
                cwd=self.root, capture_output=True, text=True, timeout=30,
                env=dict(os.environ, HELM_WORK_INTEGRATOR="1"))
            self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t",
             "cherry-pick", tip], cwd=self.root, capture_output=True,
            text=True, timeout=30,
            env=dict(os.environ, HELM_WORK_INTEGRATOR="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = {row["lane"]: row for row in work.list_rows(self.root)}
        # MUST-HIT: the lane really is still ahead by object id, and really is
        # landed by content — the two readings `ahead` alone conflates.
        self.assertEqual(rows["rebased-land"]["ahead"], "1")
        self.assertEqual(rows["rebased-land"]["landed"]["state"],
                         work.LANE_LANDED, rows["rebased-land"])
        self.assertIn("patch identity", rows["rebased-land"]["landed"]["proof"])
        got = self.building()
        self.assertIsNone(got["unavailable"])
        # POSITIVE CONTROL on the same read: the unlanded lane IS building
        self.assertEqual([r["lane"] for r in got["rows"]], ["live-build"])
        self.assertEqual(got["total"], 1)

    def test_a_lane_landed_by_PATCH_IDENTITY_under_a_DIRTY_room_is_still_building(self):  # noqa: VACUOUS_ASSERTION — the band's rows are asserted EQUAL to ["dirty-land"], a positive; the loop only pins that both fixtures are landed and ahead
        """THE CLI KEEPS IT, SO THE BAND DOES. `helm work list` prints a
        landed row whose room is dirty as DIRTY and `helm lr foldcheck` keeps
        it; uncommitted work is in flight whatever the trunk carries."""
        env = dict(os.environ, HELM_WORK_INTEGRATOR="1")

        def git(cwd, *argv):
            r = subprocess.run(["git", "-c", "user.name=t", "-c",
                                "user.email=t@t", *argv], cwd=cwd,
                               capture_output=True, text=True, timeout=30,
                               env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout.strip()
        tips = {}
        for name in ("clean-land", "dirty-land"):
            path = self.lane(name)
            with open(os.path.join(path, name + ".txt"), "w") as f:
                f.write("%s — its own content\n" % name)
            git(path, "add", "-A")
            git(path, "commit", "-q", "-m", name)
            tips[name] = git(path, "rev-parse", "HEAD")
        with open(os.path.join(self.root, "trunk-moved.txt"), "w") as f:
            f.write("an unrelated land\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "unrelated land")
        for name in ("clean-land", "dirty-land"):
            git(self.root, "cherry-pick", tips[name])
        with open(os.path.join(work.lane_path(self.root, "dirty-land"),
                               "wip.txt"), "w") as f:
            f.write("uncommitted\n")
        rows = {row["lane"]: row for row in work.list_rows(self.root)}
        # MUST-HIT: both landed by content and ahead by object id; only one
        # room is dirty
        for name in ("clean-land", "dirty-land"):
            self.assertEqual(rows[name]["landed"]["state"], work.LANE_LANDED,
                             rows[name])
            self.assertEqual(rows[name]["ahead"], "1")
        self.assertEqual((rows["clean-land"]["dirty"],
                          rows["dirty-land"]["dirty"]), (False, True))
        got = self.building()
        self.assertIsNone(got["unavailable"])
        self.assertEqual([r["lane"] for r in got["rows"]], ["dirty-land"])

    def test_a_room_with_NO_lease_is_not_building(self):
        """A lane room whose claim expired or was released is not somebody's
        live work — it is a room. `helm work list` prints it under its own
        holderless heading and offers to reclaim it."""
        self.room("holderless")            # minted git-only, never at the desk
        self.commits(work.lane_path(self.root, "holderless"), 2)
        self.lane("held", commits=1)
        got = self.building()
        self.assertEqual([r["lane"] for r in got["rows"]], ["held"],
                         "a room with commits and no lease was reported as "
                         "somebody's live build")

    def test_an_unreadable_tree_answers_UNKNOWN_and_never_zero(self):
        """The band's own law, and the reason it is a second source at all: it
        must be able to say "I could not read this" in words that cannot be
        mistaken for "nobody is building"."""
        self.lane("live-build", commits=2)
        with mock.patch.object(work, "list_rows",
                               side_effect=OSError("planted")):
            got = self.building()
        self.assertEqual(got["rows"], [])
        self.assertEqual(got["total"], 0)
        self.assertIn("UNKNOWN", got["unavailable"] or "")
        self.assertIn("OSError", got["unavailable"] or "")

    def test_the_default_root_is_the_checkout_THIS_helm_came_from(self):
        """`helm web` is a long-running server and its cwd is not a promise.
        The resolution is the one `ready.signal_checkout` takes for the same
        reason, and it must ANSWER rather than raise — the assertion is that
        the call returns an envelope, refusing in words if it must."""
        got = web_land._lr_building(set())
        self.assertEqual(got["source"], "helm work list")
        self.assertIsInstance(got["rows"], list)
        self.assertTrue(got["unavailable"] is None
                        or "UNKNOWN" in got["unavailable"])


class ARestoredBodyIsDatedNotAncientTest(unittest.TestCase):
    """THE FIRST /api/lr OF A SERVER LIFE, which is the one the owner sees.

    MEASURED against a running server: run1 200 in 56.07s, run2 0.64s, run3
    0.66s, run4 0.22s. The first call of a life blocks; every call after it is
    warm. That is why the fault reads as intermittent, why a reload
    appears to cure it, and why it survived every arm this file already has —
    all of those run against a cache that has already been filled.

    THE REGIME IS THE DEFECT. `persist_load` ages a body whose input moved
    while the server was down past `ttl` ON PURPOSE, so it lands in STALE:
    served while a rebuild runs, with its age stated in the open. But the age
    it carries is its REAL age, so a server down longer than `hard_ttl` gets
    back an entry that is already ANCIENT — the one regime with no ceiling —
    and the bounded cold-start answer cannot cover it either, because restoring
    a body makes the cache non-EMPTY and that branch is reachable only from
    EMPTY. The bounded answer built for this exact symptom is skipped precisely
    when the symptom occurs.

    WHAT MAY NOT BE BOUGHT WITH IT. A confident stale board is worse than the
    timeout it replaces: the owner can tell a timeout from a working board and
    cannot tell a frozen board from a live one. So these arms assert the bound
    AND the disclosure — the served entry keeps its true timestamp, and no
    fingerprint match may stamp it as a reading taken now."""

    TTL = 30
    HARD = 120

    def setUp(self):
        saved = tempfile.mkdtemp(prefix="helm-t2878-")
        self.addCleanup(shutil.rmtree, saved, True)
        # THE REAL WRITER, A REDIRECTED SHELF. The envelope under test is
        # produced by `_persist_store` itself rather than hand-written here, so
        # these arms read what the server actually saves; only where it sleeps
        # moves, and it moves off the developer's own home.
        path = mock.patch.object(
            web_cache, "_persist_path",
            lambda key: os.path.join(saved, "%s.json" % key))
        path.start()
        self.addCleanup(path.stop)

    def forget(self, key):
        for holder in (web_cache._qstate, web_cache._qdated, web_cache._qfresh,
                       web_cache._qcapkick, web_cache._qverified,
                       web_cache._qcold):
            holder.pop(key, None)
        web_cache._qrestored.discard(key)

    def settle(self, key, timeout=30):
        """Wait out whatever rebuild this key kicked, so a background store
        cannot land after the fixture has been torn down."""
        pending = web_cache._qinflight.get(key)
        if pending is not None:
            pending.wait(timeout)

    def dated_on_disk(self, key, probe="RESTORED"):
        """A body from a PREVIOUS server life, ten hard-ttls old, whose input
        has moved since. Both halves are the ordinary case on a live fleet: a
        restart older than ten minutes, and any dispatch write while the server
        was down."""
        self.forget(key)
        self.addCleanup(self.forget, key)
        stored_ts = time.time() - 10 * self.HARD
        web_cache._persist_store(key, stored_ts, {"loops": [], "probe": probe},
                                 "W1")
        return stored_ts

    def test_the_first_read_of_a_server_life_answers_DATED_not_a_blocking_build(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control runs FIRST and on the same clock: the identical gated build over an EMPTY key must still be holding its caller after a second, which is what proves this arm can see a blocked read at all
        released = threading.Event()

        def gated_build():
            released.wait(30)          # the real projection costs 22-56s
            return {"loops": [], "probe": "REBUILT"}

        # POSITIVE CONTROL FIRST, ON THE SAME CLOCK AND THE SAME FUNCTION.
        # Over a key with NOTHING saved, this identical gated build must HOLD
        # its caller. Without that reading, the fast answer measured below is
        # equally consistent with a build that was simply cheap, and the arm
        # would pass over the very defect it exists to catch.
        control = "t2878_empty"
        self.forget(control)
        self.addCleanup(self.forget, control)
        held = {}

        def call_control():
            began = time.time()
            held["body"] = web_cache._cached_swr(control, self.TTL, self.HARD,
                                                 gated_build)
            held["elapsed"] = time.time() - began

        caller = threading.Thread(target=call_control, daemon=True)
        caller.start()
        caller.join(1.0)
        self.assertTrue(caller.is_alive(),
                        "an EMPTY cache answered while its build was still "
                        "gated, so this arm cannot see a held caller at all "
                        "and the measurement below would prove nothing")
        released.set()
        caller.join(30)
        self.assertFalse(caller.is_alive(), "the control caller never returned")
        self.assertEqual(held["body"].get("probe"), "REBUILT",
                         "the control did not answer from its own build")
        self.assertGreaterEqual(held["elapsed"], 1.0,
                                "the control returned without waiting for the "
                                "build it was blocked on")

        # THE ARM.
        key = "t2878_dated"
        stored_ts = self.dated_on_disk(key)
        gate = threading.Event()
        self.addCleanup(gate.set)

        def slow_rebuild():
            gate.wait(30)
            return {"loops": [], "probe": "REBUILT"}

        began = time.time()
        served = web_cache._cached_swr(key, self.TTL, self.HARD, slow_rebuild,
                                       snapshot=web_cache._ConstSnapshot("W2"))
        elapsed = time.time() - began
        self.assertEqual(served.get("probe"), "RESTORED",
                         "the first read of a server life waited out the whole "
                         "foreground build instead of serving the saved body")
        self.assertLess(elapsed, 1.0,
                        "the first read took %.2fs: a restored body born past "
                        "the hard ttl fell through to the unbounded blocking "
                        "path, which is the 56.07s the owner measured"
                        % elapsed)
        # DATED *WHILE REBUILDING* — the second half of the regime's contract.
        self.assertIn(key, web_cache._qinflight,
                      "the dated body was served with no rebuild behind it")
        # AND THE BOUND IS NOT BOUGHT WITH A CLAMP. Admitting this body by
        # moving its timestamp up under the hard ttl reaches the same regime
        # while telling the card that a twenty-minute-old reading is nine
        # minutes old. The entry keeps its true stamp, so `read_age_s`
        # discloses what it really is.
        self.assertEqual(web_cache._qstate[key][0], stored_ts,
                         "the restored entry's timestamp was moved: the card "
                         "would state an age this body does not have")
        gate.set()
        self.settle(key)

    def test_no_fingerprint_match_may_certify_a_dated_body_as_read_just_now(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the final block: a body this life DID build, polled the same three times with the same unmoved fingerprint, must come back CONFIRMED through the same `verified_at` the absence above is asserted on
        """`verified_at` is what the card stamps `read_age_s` from, so a stamp
        over a restored body would report a twenty-minute-old board as a
        reading taken seconds ago — the confident stale board, arriving through
        the very regime that is supposed to disclose the age. The mark a dated
        entry is compared against was first taken on THIS life by the poll that
        adopted the body, so an unmoved fingerprint says nothing about a body
        built before this server started."""
        key = "t2878_confirm"
        stored_ts = self.dated_on_disk(key)
        gate = threading.Event()
        self.addCleanup(gate.set)

        def slow_rebuild():
            gate.wait(30)
            return {"loops": [], "probe": "REBUILT"}

        served = None
        for _ in range(3):             # poll 1 kicks, poll 2 records the
            served = web_cache._cached_swr(    # cap-kick reason, poll 3 would
                key, self.TTL, self.HARD, slow_rebuild,    # confirm on it
                snapshot=web_cache._ConstSnapshot("W2"),
                fingerprint=lambda: "M1", unchanged_max=300)
        self.assertEqual(served.get("probe"), "RESTORED")
        self.assertIsNone(
            web_cache.verified_at(key),
            "a matching fingerprint certified a body this server life never "
            "built: the card would render a %.0fs-old board as current"
            % (time.time() - stored_ts))
        gate.set()
        self.settle(key)

        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE, through the
        # same three polls: a body this life DID build is confirmed by the same
        # unmoved fingerprint, so the None above is a discrimination rather
        # than a stamp that never fires.
        live = "t2878_live"
        self.forget(live)
        self.addCleanup(self.forget, live)
        build = lambda: {"loops": [], "probe": "BUILT"}      # noqa: E731
        for _ in range(3):
            web_cache._cached_swr(live, 0, self.HARD, build,
                                  fingerprint=lambda: "M1", unchanged_max=300)
            self.settle(live)
        self.assertIsNotNone(web_cache.verified_at(live),
                             "the confirmation stamp never fires at all")
