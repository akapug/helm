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
   lrCardHTML/lrRowHTML/lrNav source lifted verbatim out of the assembled web UI
   under node, following tests/test_web_chat_client_runtime.py. A python mirror
   of JS rots silently; this cannot.

Hermetic: the fixture is tests/test_landreq.py's LandReqBase (tmp HELM_HOME,
tmp chat dir, a git repo minted per test), so the real ledger and repos are
never touched.
"""
import json
import os
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

from helm import dispatches, eventledger, landreq, web, web_ui_loader  # noqa: E402
from tests.test_landreq import LandReqBase, ReceiptBase, run  # noqa: E402
from tests.test_web_chat_client_runtime import _extract_fn  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The verification axis. A rename on either of these does not break a build and
# does not raise — it just makes every row take the "absent" branch forever.
GATE_FIELDS = ("gate", "ungated")
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
               "holder_role", "holder_seat", "close_reason", "close_evidence",
               "receipt_state", "timeline") \
    + GATE_FIELDS


# Direct `dispatches.add` calls below pass `new_work=True` for the same reason
# LandReqBase.dispatch does: since the work-chain landed, a row must declare
# whether it is new work or supersedes a parent, and every fixture here mints an
# independent lane. Stamping --new-work on a superseding round would assert the
# defect that contract exists to prevent.
class LrApiBase(LandReqBase):
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
        # The writer strips the lane/ prefix at mint, so the card
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
        dispatches.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
        self.git("merge", "--no-edit", "-q", "side")   # landed despite FIX
        card = self.one(self.lr())
        self.assertTrue(card["contrary"])
        self.assertEqual(card["contrary_state"], "landed")
        self.assertEqual(card["polarity"], "fix")
        # The DISCHARGE stamp rides the same wire (the two-surfaces class:
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
        the right guard while the twin existed — measured live, the two
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
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
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
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
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
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
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
    """The owner's ask, verbatim: "why don't the pipeline readings
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

    def test_the_filed_split_partitions_the_whole_ledger(self):  # noqa: VACUOUS_ASSERTION — the exact five-bucket non-empty dict equality is the unconditional positive control; an empty board, a failed read, or a leaked arm all fail it
        # one row per bucket, each on its own tip so no arm leaks into another
        self.dispatch(lane="lane/filed-open", ref=self.off_trunk("open-tip"))
        held = self.dispatch(lane="lane/filed-held",
                             ref=self.off_trunk("held-tip"))
        self.legacy_undeclared(held, "review-post-7")     # REVIEWED — held
        landed = self.dispatch(lane="lane/filed-landed", ref=self.side)
        dispatches.mark_verdict(landed["id"], self.side, "ok",
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
        self.assertEqual(body["filed"],
                         {"total": 5, "open": 1, "held": 1, "landed": 1,
                          "closed": 1, "non_loop": 1, "in_flight": 1})
        f = body["filed"]
        self.assertEqual(f["total"], f["open"] + f["held"] + f["landed"]
                         + f["closed"] + f["non_loop"],
                         "the split no longer partitions the ledger")
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
                               return_value=([], "PermissionError: denied")):
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
                               return_value=([], "PermissionError: denied")):
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
                               return_value=([], "PermissionError: denied")):
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
    """A cross-family review's finding. `project()` reads the dispatch ledger TWICE — once
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
        return mock.patch.object(eventledger, "checked_events", flaky)

    def test_a_dispatch_ledger_that_cannot_be_read_is_reported_UNKNOWN(self):
        row = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(row["id"], "post-1")
        self.age(row["id"], 3600)
        healthy = self.lr()
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
                               return_value=([], "OSError: EIO")):
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
    """A cross-family review, one layer before the projection.

    `strict=True` validates the PHYSICAL shape of a ledger line — complete
    JSON, an object, a non-empty id — and says nothing about whether those
    events amount to an obligation. The repro is one well-formed row: a
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
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        current, held, _taken, _order, unavailable = dispatches.snapshot_and_events()
        self.assertIsNone(unavailable)
        self.assertEqual(sorted(held), sorted(current))
        self.assertIsNone(self.lr()["unavailable"])


class AnAppendCannotLandBetweenTwoReadsTest(LrApiBase):
    """A cross-family audit, and the ONE defect on this card that is not the
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
        real = eventledger.checked_events
        fired = []

        def racing(path, strict=False):
            out = real(path, strict=strict)
            if path == dispatches.ledger_path() and not fired:
                fired.append(True)       # never re-enter from the append itself
                dispatches._mark_delivered(row["id"], "post-1")
            return out
        return mock.patch.object(eventledger, "checked_events", racing), fired

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
        real = eventledger.checked_events
        seen = []

        def counting(path, strict=False):
            seen.append(path)
            return real(path, strict=strict)
        with mock.patch.object(eventledger, "checked_events", counting):
            landreq.project()
        self.assertEqual(seen.count(dispatches.ledger_path()), 1, seen)


class OneReadIsNotYetOneRuleTest(LrApiBase):
    """A cross-family review of the shared-read fix, and the useful
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
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
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
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
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
        dispatches.mark_verdict(row["id"], self.side, "the accepted one",
                                polarity="approve")
        return row

    def test_a_REFUSED_event_cannot_supply_the_ACCEPTED_one_s_stamp(self):
        """A cross-family follow-up, and the step the first answer was short by.
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
        # as a whole-suite gate flake ("2099" inside a freshly minted sha)
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
        dispatches.mark_verdict(row["id"], self.side, "canonical", "approve")
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
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
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
    """A cross-family audit's finding — the same class one level lower down.

    `project()` wrapped each row in `except Exception: continue`, so a row it
    could not build was dropped from the board with nothing said: the snapshot
    still held it, `unavailable` stayed None, and the card printed "0 in
    flight … the dispatch ledger READ cleanly" over a ledger with a live land
    loop on it. One junk row emptied the board.

    The trigger was a `repo_id` that is not a path. It reaches git as itself —
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
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
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
    """A cross-family review's finding, both halves.

    The window asked "when did this row enter its current state", which for a
    land loop is the VERDICT. An approve from three days ago whose change
    merges NOW leaves the in-flight list at that instant and was also outside a
    verdict-dated 24h window, so it fell off the card completely while the
    footer went on claiming nothing had closed."""

    def approved_days_ago_then_merged_now(self, days=3):
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
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
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        body = self.lr()
        self.assertEqual([c["id"] for c in body["closed_recent"]], [row["id"]])
        self.assertEqual(body["closed_unknown_when"], 0)
        self.assertEqual(body["closed_total"], 1)

    def test_ABANDONED_is_closed_recent_with_land_state_UNKNOWN(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        row = self.dispatch(ref=self.side, kind="review")
        dispatches.mark_verdict(row["id"], self.side, "reviewed", polarity="fix")
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
        dispatches.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
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
        dispatches.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
        landreq.withdraw(row["id"], "abandoned in favour of the verdict")
        self.corrupt_one_events_ts(row["id"], "close")
        return row

    def test_a_retirement_stamp_that_is_not_a_stamp_is_never_copied_out(self):
        """A cross-family audit's finding. The retirement ts was parsed for
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
        dispatches.mark_verdict(other["id"], landing, "ok", polarity="approve")
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
            dispatches.mark_verdict(row["id"], sha, "ok", polarity="approve")
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
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.edit_event(row["id"], "verdict", ts="not-a-stamp")
        lr = landreq.get(row["id"])[0]
        step = self.step(lr, "READY")
        self.assertIsNone(step["ts"])
        self.assertTrue(step["ts_unreadable"])
        self.assertNotIn("not-a-stamp", json.dumps(lr["timeline"]))
        # and the terminal stops printing it too
        self.assertIn("READY            (the ledger's stamp here is NOT A "
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
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        lr = landreq.get(row["id"])[0]
        step = self.step(lr, "LANDED")
        self.assertIsNone(step["ts"])
        self.assertTrue(step["observed"])
        self.assertIn("LANDED           (git-observed, no ledger stamp)",
                      landreq._render_show(lr))


class ARefusalAndABindingAreBothTrueAtOnceTest(LrApiBase):
    """A cross-family audit's finding, at the seam that MAKES the pair.

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
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.assertEqual(self.one(self.lr())["state"], "READY")   # control
        self.unreadable_caps_on_a_bound_verdict(row["id"])
        card = self.one(self.lr())
        self.assertEqual(card["gate"], "0c7f3a91ab")
        self.assertIn("gate_caps", card["ungated"])
        # and the refusal is REAL: an unreadable capability stamp may not
        # authorize the one state that says this may be merged
        self.assertEqual(card["state"], "REVIEWED")


class AnUnreadableReceiptLedgerIsNotAnAbsentOneTest(LrApiBase):
    """A cross-family review's finding. `_receipts_by_tip` suppressed every non-corruption
    I/O failure into `{}`, so `_receipt_for` found no row, answered R_NONE, and
    the card printed "land receipt none". A PermissionError read as PROOF OF
    ABSENCE.

    Only the RECEIPT read is broken here. The dispatch ledger stays readable and
    git stays authoritative, because the point is that the lifecycle is
    unaffected and only the DIAGNOSTIC was false."""

    def landed_row(self):
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
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
    """A cross-family audit's finding — the missing-value shape at the one
    place it bills the owner for time.

    A verdict event whose `ts` the ledger does not carry collapsed to None, and
    None means "there was no verdict event" everywhere downstream. Two lies
    followed from the one collapse:

    1. `post_verdict = verdict_ts or delivered_ts or open_ts` fell through to
       the DELIVERY instant. The exact repro: APPROVE at an unknown time,
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
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.age(row["id"], self.HOURS)     # delivery AND verdict, 2h50 back
        if drop:
            self.edit_event(row["id"], "verdict", drop_ts=True)
        return row

    def test_the_control_the_same_row_WITH_a_stamp_is_dated_and_STALLS(self):
        """First, that the fixture really does produce the confident reading —
        otherwise the test below passes over a row that was never dateable."""
        self.approve_then_lose_the_verdict_stamp(drop=False)
        card = self.one(self.lr())
        self.assertEqual(card["state"], "READY")
        self.assertTrue(card["dwell_known"])
        self.assertEqual(card["dwell_s"], self.HOURS)
        self.assertTrue(card["stalled"])          # past the 1h land budget

    def test_an_APPROVE_helm_cannot_date_is_not_dated_from_the_DELIVERY(self):
        self.approve_then_lose_the_verdict_stamp()
        card = self.one(self.lr())
        self.assertEqual(card["state"], "READY")  # the verdict still governs
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
        self.assertEqual(states, ["OPEN", "AWAITING_REVIEW", "READY"])
        step = [s for s in lr["timeline"] if s["state"] == "READY"][0]
        self.assertIsNone(step["ts"])
        # …and it says WHICH kind of missing it is: there is no stamp here, so
        # neither "the stamp is not a timestamp" nor "git observed this".
        self.assertFalse(step.get("ts_unreadable"))
        self.assertFalse(step.get("observed"))
        self.assertIn("READY            (no stamp on the ledger event)",
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
        dispatches.mark_verdict(row["id"], self.side, "superseded",
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
    """A cross-family audit's finding. The window had one side.

    `now - ts <= 86400` is satisfied by every instant in the FUTURE as well —
    the subtraction goes negative and sails under the ceiling — so a row whose
    record says it closed in 2099 was counted, confidently, as "closed in the
    last 24h", printed at the top of the list (the sort is newest-first), and
    changed the exact total the footer reports. A closure that has not happened
    yet is not recent; it is a broken clock or a broken record."""

    FUTURE = "2099-01-01T00:00:00Z"

    def withdrawn_with(self, stamp):
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
        landreq.withdraw(row["id"], "abandoned in favour of the verdict")
        self.age(row["id"], 3 * 86400)      # the verdict is OUTSIDE the window
        if stamp:
            self.edit_event(row["id"], "close", ts=stamp)
        return row

    def test_the_control_a_row_withdrawn_NOW_is_counted(self):
        """The fixture must be able to produce a counted row, or the assertion
        below is measuring an empty board."""
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
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
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.edit_event(row["id"], "verdict", ts=self.FUTURE)
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
        """A cross-family review is right that this one was left short. The window
        ruled the 2099 instant unusable and the ROW still reached the card
        carrying `closed_ts: "2099-01-01T00:00:00Z"`, which the panel printed
        as the closure instant — so the surface both showed the impossible date
        and, via its entry lower bound, acted on it. Absurd is not the same as
        harmless: the row is on the board, and the date beside it is a value
        nothing measured."""
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
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
        dispatches.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
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
        dispatches.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
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
        dispatches.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
        landreq.withdraw(row["id"], "abandoned in favour of the verdict")
        self.edit_event(row["id"], "close", ts=self.FUTURE)
        lr = landreq.get(row["id"])[0]
        self.assertIn("closure stamp IMPOSSIBLE", landreq._line(lr))
        self.assertIn("dated in the FUTURE", landreq._render_show(lr))

    def test_a_retirement_with_NO_stamp_at_all_is_undateable_too(self):
        """A cross-family review's third finding: `retired and retire_ts` walked past the no-stamp
        case entirely, so a withdraw event recorded without a ts left the row
        terminal with dwell_known TRUE and 259200s GROWING every draw. The flag
        is what asserts the retirement; the stamp only says when."""
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
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
        """A cross-family review's second blocker, the one named as left-unfixed.

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
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
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
        dispatches.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
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
        self.age(row["id"], 3600)
        now = time.time()
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
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
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
        # rows mint with the lane/ prefix stripped, so every projection
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

    def test_the_floor_never_outlives_the_read_it_reports(self):
        """The freshness header is what makes the floor honest — a body served
        out of the cache must say how old it is, not pretend it is now."""
        self.dispatch()
        first = self.lr()
        self.assertEqual(first["read_age_s"], 0)
        with mock.patch.object(web.time, "time",
                               mock.Mock(return_value=time.time() + 12)):
            held, _status = web.QUERY_API["/api/lr"]({})   # no forget
        self.assertGreaterEqual(held["read_age_s"], 12)


class CardRuntimeTest(unittest.TestCase):
    """The CLIENT leg: the real renderer source out of the assembled web UI, run.

    A server field nothing renders is the same as no field at all — the lesson
    the stale-banner tests already encode. These run lrCardHTML/lrNav verbatim
    under node so the assertion is about what the OWNER SEES. Requires node;
    skipped (not failed) where node is unavailable, like any optional
    toolchain."""

    EXTRACT = ["lrDwell", "lrAgo", "lrHonored", "lrMarks", "lrGate",
               "lrRowHTML", "lrKbRank", "lrKbKey", "lrKanbanHTML", "lrIsBoard",
               "lrBall", "lrInflightMoving", "lrInflightRowHTML",
               "lrInflightHTML", "lrCardHTML", "lrNav"]

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        cls.src = web_ui_loader.read_text()
        # `esc` is a const arrow, not a function declaration — lifted by its own
        # line so the harness escapes exactly the way the page does.
        line = [ln for ln in cls.src.splitlines() if ln.startswith("const esc = ")]
        assert len(line) == 1, "assembled web UI's esc definition moved"
        fns = "\n\n".join([line[0]]
                          + [_extract_fn(cls.src, n) for n in cls.EXTRACT])
        cls.tmp = tempfile.mkdtemp(prefix="helm-lr-runtime-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(fns + """

const cases = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
const out = {};
for (const name of Object.keys(cases)) {
  // a case may wrap its board to pick the view ({__d: board, __view: "list"})
  // or ask for ONE row's markup ({__row: row, __reason: r}) — the byte-parity
  // probe between the two views. A bare board (every pre-kanban case) renders
  // exactly as the page's default caller does: lrCardHTML with no view.
  const c = cases[name];
  if (c && c.__row !== undefined) {
    out[name] = {html: lrRowHTML(c.__row, c.__reason ?? null)};
    continue;
  }
  if (c && c.__inflight !== undefined) {
    out[name] = {html: lrInflightHTML(c.__inflight)};
    continue;
  }
  const d = c && c.__d !== undefined ? c.__d : c;
  out[name] = {html: c && c.__view !== undefined ? lrCardHTML(d, c.__view)
                                                 : lrCardHTML(d),
               nav: lrNav(d)};
}
process.stdout.write(JSON.stringify(out));
""")
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def render(self, **cases):
        path = os.path.join(self.tmp, "cases.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cases, f)
        p = subprocess.run([self.node, self.path, path],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    @staticmethod
    def row(**kw):
        base = {"id": "0123456789ab", "state": "READY", "lane": "lane/example",
                "branch": "lane/example", "review_sha": "f7f8047ba21c",
                "review_sha_full": "f7f8047ba21c" + "0" * 28,
                "author": "author-seat", "reviewer": "reviewer-seat",
                "kind": "review", "polarity": "approve",
                "polarity_source": "dispatch-store",
                "attest_source": "attest-sidecar", "attest_state": "attested",
                "attest_detail": "attested (signed turn, room main)",
                "dwell_s": 2460, "entered_ts": None,
                # a row whose entry stamp WAS readable and which the record
                # cannot date the closure of — the ordinary case for both
                "dwell_known": True, "closed_ts": None,
                "closed_ts_unreadable": False, "closed_ts_impossible": False,
                "ledger_refused": [],
                "stalled": False, "observable": True, "land_state": "ABSENT",
                "landed": False,
                "merged_local": False, "has_upstream": False, "contrary": False,
                "contrary_state": None, "contrary_discharge": None,
                "discharged": False,
                "superseding_tip": None, "withdrawn": False,
                "withdraw_contradicted": False, "abandoned": False,
                "abandon_reason": None, "abandon_ts": None,
                "abandon_object_state": None, "abandon_proof_mode": None,
                "abandon_proof_version": None,
                "abandon_trunk_mention_state": None,
                "abandon_trunk_mention_proof_mode": None,
                "abandon_trunk_mention_proof_version": None,
                "abandon_branch_state": None,
                "abandon_branch_proof_mode": None,
                "abandon_branch_proof_version": None,
                "abandon_worktree_state": None,
                "abandon_worktree_proof_mode": None,
                "abandon_worktree_proof_version": None,
                "abandon_land_state": None,
                "closed_by_landing": False,
                "landing_trunk_sha": None, "owed_by": "integrator",
                "holder_role": "integrator", "holder_seat": None,
                "receipt_state": "none", "timeline": [], "gate": "",
                "ungated": None}
        base.update(kw)
        # DERIVED, never hand-typed. `honored` rides the wire now, and a
        # fixture that set it by hand could describe a payload the server
        # never sends — the exact drift that having two implementations of
        # this predicate created in the first place. A test may still override
        # it explicitly to exercise an old server (absent field, fail-closed).
        base.setdefault("honored", landreq.honored_display(base))
        return base

    @classmethod
    def board(cls, rows, **kw):
        d = {"read_age_s": 3, "ledger_age_s": 40, "unavailable": None,
             "receipts_skipped": None, "loops": rows, "stalled_ids": [],
             "unmeasurable": [], "closed_recent": [], "closed_unknown_when": 0}
        d.update(kw)
        # The server always sends the PRE-CAP total; a fixture that omits it is
        # asking for the "no count" branch and must say so explicitly.
        d.setdefault("closed_total", len(d["closed_recent"]))
        # Same rule for the filed split: a rendered board always carries one;
        # a fixture that wants the "no split" branch pops it explicitly.
        d.setdefault("filed", {"total": len(d["loops"]),
                               "open": len(d["loops"]), "held": 0,
                               "landed": 0, "closed": 0, "non_loop": 0})
        return d

    # ── the property that matters most ──────────────────────────────────
    def test_an_unreadable_ledger_RENDERS_the_unknown_strip(self):
        out = self.render(dead={"unavailable": "PermissionError: denied",
                                "loops": [], "stalled_ids": [],
                                "unmeasurable": [], "closed_recent": []})
        html = out["dead"]["html"]
        self.assertIn("DISPATCH LEDGER UNREADABLE — pipeline UNKNOWN", html)
        self.assertIn("PermissionError: denied", html)     # the reason, verbatim
        self.assertIn("helm lr list", html)                # where to go instead

    def test_the_unknown_strip_shows_no_rows_and_claims_no_count(self):
        """The failure this replaces: a comforting "0 in flight" over a board
        nobody read."""
        out = self.render(dead={"unavailable": "denied", "loops": [],
                                "stalled_ids": [], "unmeasurable": [],
                                "closed_recent": []})
        html = out["dead"]["html"]
        self.assertNotIn("lrrow", html)
        self.assertNotIn("0 in flight", html)
        self.assertNotIn("no land loops in flight", html)
        self.assertIn("UNKNOWN", html)

    # ── the FILED strip: the all-time population behind the board ────────
    def test_the_filed_strip_renders_the_population(self):
        out = self.render(pop=self.board([], filed={
            "total": 542, "open": 51, "held": 3, "landed": 81,
            "closed": 379, "non_loop": 28}))
        html = out["pop"]["html"]
        self.assertIn("filed 542 all-time", html)
        self.assertIn("51 open", html)
        self.assertIn("3 held", html)
        self.assertIn("81 landed", html)
        self.assertIn("379 closed", html)
        self.assertIn("28 non-loop", html)

    def test_a_PRE_RENAME_servers_split_still_renders_its_number(self):
        """VERSION SKEW, not a broken payload (independent review). The console is served
        from disk and hot-reloads; the SERVER process can be older than it.
        `in_flight` was renamed `open` in this lane, so a fresh UI against a
        not-yet-restarted server received a perfectly well-formed split and
        printed "? open" — which reads as "the server sent garbage" and sends
        the reader to debug the wrong thing. The legacy key is read under the
        new name; every other term is untouched."""
        out = self.render(pop=self.board([], filed={
            "total": 542, "in_flight": 51, "held": 3, "landed": 81,
            "closed": 379, "non_loop": 28}))
        html = out["pop"]["html"]
        self.assertIn("51 open", html)
        self.assertNotIn("? open", html)
        self.assertIn("filed 542 all-time", html)

    def test_a_GENUINELY_absent_term_is_still_said_not_invented(self):
        """The control that keeps the fallback from becoming a lie: reading a
        legacy key is not the same as tolerating a missing one. A term that
        arrived under NO name still renders "?", never 0 — the rule the strip
        was built on and which this fallback must not erode."""
        out = self.render(pop=self.board([], filed={
            "total": 542, "held": 3, "landed": 81,
            "closed": 379, "non_loop": 28}))
        html = out["pop"]["html"]
        self.assertIn("? open", html)
        self.assertNotIn("0 open", html)

    def test_the_strip_tooltip_DEFINES_the_word_it_actually_prints(self):
        """The third site the rename missed. The strip says `open` while its
        own hover text still opened "in flight = every non-terminal loop" —
        defining the renamed bucket by the very word the rename removed from
        it, on the same element. A tooltip is a surface."""
        out = self.render(pop=self.board([], filed={
            "total": 5, "open": 1, "held": 1, "landed": 1,
            "closed": 1, "non_loop": 1}))
        html = out["pop"]["html"]
        self.assertIn("open = every non-terminal loop", html)
        self.assertNotIn("in flight = every non-terminal loop", html)

    def test_the_card_strip_is_byte_identical_to_the_CLI_strip(self):
        """One shape on every surface: the card's JS builds the strip and
        `helm lr list` prints landreq.filed_line — a number the owner pastes
        from either surface must read identically on the other. This runs the
        REAL renderer against the REAL python formatter for the same dict."""
        f = {"total": 542, "open": 51, "held": 3, "landed": 81,
             "closed": 379, "non_loop": 28}
        out = self.render(pop=self.board([], filed=f))
        self.assertIn(landreq.filed_line(f), out["pop"]["html"])

    def test_ONE_SURFACE_NEVER_PRINTS_TWO_PREDICATES_AS_IN_FLIGHT(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control asserts the card DOES print the phrase once (a card that rendered nothing would otherwise pass a count-of-at-most-one)
        """The THIRD instance of one class in one day.

        The disease: two different predicates rendered under one noun on one
        page, so a reader cannot tell which is lying — and neither is. The
        instances, all measured: (1) the home glance list vs its own header,
        caught by the owner ("it's still on the homepage and that
        is crazy to me") and cured at 00-core.js:994-999; (2) filed_split's
        raw non-terminal count vs _loop_rows' chain-folded one, which the
        owner read as 32 and 277 on ONE PAGE, 8.6x apart; (3) whatever comes
        next, which is what this test exists to stop.

        THE RULE, and it is deliberately about the WORD not the number: the
        phrase "in flight" belongs to exactly ONE predicate — `_loop_rows`,
        chain-folded and honored-subtracted, i.e. work actually MOVING. Any
        other count on the same surface must say what it actually is. The
        filed split says `open`, because it counts what is on the books.

        A count that is honest in a docstring and ambiguous on the surface is
        not honest — that is precisely how this shipped twice."""
        out = self.render(one=self.board([], filed={
            "total": 9, "open": 4, "held": 1, "landed": 2,
            "closed": 1, "non_loop": 1}))
        html = out["one"]["html"]
        # THE PREDICATE IS "A NUMBER LABELLED in flight", NOT THE PHRASE.
        # My first cut counted the raw string and got 3 — because the card
        # also says "0 in flight — pipeline read cleanly", "nothing is in
        # flight", and a tooltip. Counting prose answers a narrower question
        # than the one that matters; the disease is two NUMBERS under one
        # noun, so that is what this counts.
        import re as _re
        text = _re.sub(r"<[^>]+>", " ", html)
        labelled = _re.findall(r"\d+\s+in flight", text)
        # POSITIVE CONTROL, unconditional: the card really does label the
        # folded count this way, so the count-of-one below cannot pass by the
        # card rendering nothing at all.
        self.assertTrue(labelled, "the card must label the folded count")
        self.assertEqual(len(labelled), 1,
                         "two counts under one noun is the disease; "
                         "the second one must say what it actually is: %r"
                         % (labelled,))
        # and the filed strip must carry the honest word instead
        self.assertIn("4 open", html)
        self.assertNotIn("4 in flight", html)

    def test_an_absent_filed_split_is_SAID_never_rendered_as_zero(self):
        """An older server (or a pre-field cached body surviving a half-live
        deploy) sends no split; the strip must say so, not count."""
        b = self.board([])
        b.pop("filed")
        out = self.render(old=b)
        html = out["old"]["html"]
        self.assertIn("filed total UNKNOWN", html)
        self.assertNotIn("filed 0", html)

    def test_a_half_shaped_split_keeps_its_known_terms(self):
        out = self.render(half=self.board([], filed={"total": 7,
                                                     "open": 7}))
        html = out["half"]["html"]
        self.assertIn("filed 7 all-time", html)
        self.assertIn("7 open", html)
        self.assertIn("? held", html)          # unknown per-term, never 0

    def test_the_unknown_strip_never_carries_a_filed_count(self):
        """A lying body: unavailable AND a filed dict. The unavailable branch
        owns the render — a count beside "pipeline UNKNOWN" would be two
        verdicts about one read."""
        out = self.render(dead={"unavailable": "denied",
                                "filed": {"total": 9, "open": 9,
                                          "held": 0, "landed": 0,
                                          "closed": 0, "non_loop": 0},
                                "loops": [], "stalled_ids": [],
                                "unmeasurable": [], "closed_recent": []})
        html = out["dead"]["html"]
        self.assertNotIn("filed 9", html)
        self.assertIn("UNKNOWN", html)

    def test_an_honest_empty_board_says_the_read_SUCCEEDED(self):
        """"nothing in flight" and "this did not load" must never be
        confusable, so the empty state asserts the read, in words."""
        out = self.render(empty=self.board([]))
        html = out["empty"]["html"]
        self.assertIn("no land loops in flight", html)
        self.assertIn("READ cleanly", html)
        self.assertNotIn("UNREADABLE", html)

    def test_the_nav_badge_carries_UNKNOWN_off_the_home_tab(self):
        out = self.render(dead={"unavailable": "denied", "loops": []},
                          calm=self.board([self.row()]),
                          loud=self.board([self.row(stalled=True)]))
        self.assertEqual(out["dead"]["nav"]["n"], "?")     # never a zero
        self.assertIn("UNKNOWN", out["dead"]["nav"]["title"])
        self.assertEqual(out["calm"]["nav"]["n"], 0)       # routine never badges
        self.assertEqual(out["loud"]["nav"]["n"], 1)

    # ── the verification axis ───────────────────────────────────────────
    def test_a_row_with_no_gate_receipt_SAYS_SO_behind_the_disclosure(self):
        """The verification axis still reaches him — it stopped SHOUTING.

        Owner ruling: the headline chip had exactly two
        reachable outcomes and both were negative, so it was an alarm on 100%
        of cards. The FACT is unchanged and unmoved from the record; only its
        rung changed. Renamed from ...renders_UNVERIFIED because the old name
        asserts the old surface, and a test whose name lies is worse than one
        that fails."""
        out = self.render(b=self.board([self.row()]))
        html = out["b"]["html"]
        headline, expand = html.split('<div class="lrx"', 1)
        self.assertIn("nothing has gate-checked this row at all", expand)
        self.assertNotIn("UNVERIFIED", headline)
        # POSITIVE CONTROL on the same surface: the headline is NOT empty of
        # verification-adjacent signal by accident — a delivered-report row
        # still prints GATE N/A there, because that one says something true
        na = self.render(b=self.board([self.row(
            close_reason="delivered-report")]))["b"]["html"]
        self.assertIn("GATE N/A", na.split('<div class="lrx"', 1)[0])

    def test_delivered_report_renders_refs_and_explicitly_no_land_or_gate_claim(self):
        row = self.row(
            state="DELIVERED_REPORT", kind="build", polarity=None,
            base_sha="b" * 40, observable=False, land_state="NOT_CLAIMED",
            owed_by="nobody",
            close_reason="delivered-report",
            artifact_ref="artifact:reports/audit.json#abc123",
            report_ref="deadbeef0042",
            close_evidence="artifact handed off", closed_ts="2026-08-03T12:00:00Z",
            dwell_known=True)
        out = self.render(b=self.board([], closed_recent=[row]))
        html = out["b"]["html"]
        self.assertIn("DELIVERED_REPORT", html)
        self.assertIn("artifact:reports/audit.json#abc123", html)
        self.assertIn("deadbeef0042", html)
        self.assertIn("NO LAND CLAIM", html)
        self.assertIn("GATE N/A", html)
        self.assertIn("verdict polarity</b> N/A", html)
        self.assertIn("base sha", html)
        self.assertIn("b" * 40, html)
        self.assertIn("reference syntax only", html)
        self.assertIn("existence is not checked", html)
        self.assertIn("no reviewed-tip, verdict, gate, or Git landing claim", html)
        self.assertNotIn("UNVERIFIED", html)
        self.assertNotIn("reviewed sha", html)

    def test_a_row_carrying_a_gate_id_does_not_claim_to_have_read_it(self):
        """Phase 1 does not join the receipt. A claim that implied it had would
        be exactly the unbacked claim this card exists to catch.

        THE CLAIM MOVED, THE RULE DID NOT (owner ruling):
        the headline chip that used to carry "receipt NOT READ here" is quiet
        now, because it was negative on 100% of cards. The expand still says
        it in words AND still carries the token, so this arm asserts the same
        invariant one disclosure deeper."""
        out = self.render(b=self.board([self.row(gate="0c7f3a91ab")]))
        html = out["b"]["html"]
        self.assertIn("gate:0c7f3a91ab", html)
        self.assertIn("has not read the gate receipt bound to this row", html)
        self.assertNotIn("UNVERIFIED", html)
        # and the not-read admission is BEHIND the disclosure, not in the
        # headline — the demotion is the ruling, so it gets pinned too
        headline = html.split('<div class="lrx"', 1)[0]
        self.assertNotIn("gate receipt", headline)

    def test_a_bound_gate_no_longer_hides_why_READY_was_refused(self):
        """A cross-family audit's finding. A row can carry BOTH a gate id
        and a fail-closed `ungated` reason — a bound receipt whose gate_caps
        stamp is unreadable is refused READY — and the verification chip tested
        `gate` FIRST, so the owner read "receipt NOT READ here" and never
        learned the record had refused to call the row READY at all.

        Both facts now render, because they answer different questions: what
        is bound to this row, and why it may not land."""
        out = self.render(b=self.board([self.row(
            state="REVIEWED", gate="0c7f3a91ab",
            ungated="the verdict's gate_caps field is present but unreadable")]))
        html = out["b"]["html"]
        self.assertIn("gate:0c7f3a91ab", html)              # the binding
        # the record's own sentence, escaped exactly as the page escapes it
        self.assertIn("NOT READY — the verdict&#39;s gate_caps field is "
                      "present but unreadable", html)       # the refusal
        self.assertIn("REVIEWED", html)

    def test_the_refusal_reaches_him_with_no_gate_bound_either(self):
        """The other half of the same slot: an ordinary approve with no minted
        receipt is refused too, and used to be legible only because nothing
        else was competing for the chip."""
        out = self.render(
            refused=self.board([self.row(
                state="REVIEWED",
                ungated="approved with no minted gate receipt — run `helm gate run`")]),
            fine=self.board([self.row()]))
        self.assertIn("NOT READY — approved with no minted gate receipt",
                      out["refused"]["html"])
        self.assertNotIn("NOT READY", out["fine"]["html"])

    def test_the_refusal_is_a_line_of_its_own_not_a_pill(self):
        """Pinned by DOM position, and it is a design constraint, not a taste:
        the reason is a sentence, the chip is a pill that ellipsizes beside the
        sha at 390px, and `.lrmark` is the full-width, word-breaking row this
        card already uses for everything it needs him to actually read."""
        out = self.render(b=self.board([self.row(
            gate="0c7f3a91ab", ungated="approved with no minted gate receipt")]))
        html = out["b"]["html"]
        self.assertIn('<div class="lrmark mut">NOT READY — ', html)
        self.assertNotIn('class="lrg pend" title="the record', html)

    # ── colour is a class, never a mood ─────────────────────────────────
    def test_only_an_observed_landing_is_green_and_alarms_outrank_it(self):
        out = self.render(
            green=self.board([self.row(state="LANDED", landed=True)]),
            plain=self.board([self.row()]),
            both=self.board([self.row(state="LANDED", landed=True, contrary=True,
                                      contrary_state="landed", polarity="fix")]))
        self.assertIn('class="lrrow landed"', out["green"]["html"])
        self.assertIn('class="lrrow "', out["plain"]["html"])
        self.assertIn('class="lrrow alarm"', out["both"]["html"])
        self.assertNotIn("lrrow landed", out["both"]["html"])

    def test_a_contrary_row_prints_the_fact_the_verdict_and_the_glyph(self):
        out = self.render(b=self.board([self.row(
            state="CHANGES_REQUESTED", polarity="fix", contrary=True,
            contrary_state="merged-local")]))
        html = out["b"]["html"]
        self.assertIn("CONTRARY: MERGED_LOCAL despite FIX verdict", html)
        self.assertIn("⚠", html)

    def test_an_unread_contrary_state_says_so_instead_of_guessing(self):
        out = self.render(b=self.board([self.row(contrary=True, polarity="fix")]))
        self.assertIn("CONTRARY: STATE UNREAD", out["b"]["html"])

    # ── an honored contrary is the process WORKING, not an alarm ─────────
    def test_an_honored_contrary_renders_the_succession_not_the_alarm(self):
        """#135 two-surfaces: `lr list` said HONORED while this card shouted
        CONTRARY over the same row and billed the integrator. The discharge
        stamp now rides the wire, and the card prints lr list's words."""
        out = self.render(b=self.board([self.row(
            state="CHANGES_REQUESTED", polarity="fix", contrary=True,
            contrary_state="landed", contrary_discharge="a")]))
        html = out["b"]["html"]
        self.assertIn("SUPERSEDED-CLOSED: LANDED, and the FIX verdict was "
                      "HONORED through succession (continuation)", html)
        self.assertNotIn("CONTRARY:", html)
        self.assertNotIn("owed by the integrator", html)
        # not an alarm: no glyph, no alarm row class, and no contrary count
        self.assertNotIn('class="lrrow alarm"', html)
        self.assertNotIn("⚠", html)
        self.assertIn("1 honored", html)
        self.assertNotIn("1 contrary", html)
        # the badge does not ring for a row whose verdict was honored
        self.assertEqual(out["b"]["nav"]["n"], 0)

    def test_a_ladder_discharge_names_its_arm(self):
        out = self.render(b=self.board([self.row(
            state="CHANGES_REQUESTED", polarity="supersede", contrary=True,
            contrary_state="landed", contrary_discharge="b")]))
        self.assertIn("HONORED through succession (ladder discharge)",
                      out["b"]["html"])
        self.assertNotIn("CONTRARY:", out["b"]["html"])

    def test_a_confirmation_row_renders_confirmation_never_contrary(self):
        """The hydra (a value-space class, measured live): the succession
        ladder's own confirmation rounds — supersede verdict + landed tip,
        which is their healthy shape BY DESIGN — rendered CONTRARY on the
        owner's card, so every cure round ADDED a contrary row. A "c" stamp
        renders its own quiet words, counts under `honored`, and never rings
        the badge; the genuine contrary beside it stays loud — the must-stay
        control on the same board."""
        out = self.render(b=self.board([
            self.row(id="a" * 12, state="SUPERSEDED", polarity="supersede",
                     contrary=True, contrary_state="landed",
                     contrary_discharge="c"),
            self.row(id="b" * 12, state="SUPERSEDED", polarity="supersede",
                     contrary=True, contrary_state="landed")]))
        html = out["b"]["html"]
        self.assertIn("CONFIRMATION: LANDED by design — the resolution "
                      "verified on trunk; the discharge instrument, never "
                      "a debt", html)
        self.assertIn("CONTRARY: LANDED despite SUPERSEDE verdict", html)
        self.assertIn("1 contrary", html)
        self.assertIn("1 honored", html)
        # the badge rings ONLY for the genuine one
        self.assertEqual(out["b"]["nav"]["n"], 1)

    def test_a_confirmation_row_and_the_cli_print_the_same_words(self):  # noqa: VACUOUS_ASSERTION — the loop walks a LITERAL two-surface tuple (can never be empty), and its assertIn("CONFIRMATION: LANDED by design") rows are unconditional positive controls: a renderer that draws nothing fails the first iteration
        """PARITY for the new kind, end to end: the SAME row through
        landreq.card() into the REAL browser renderer and through
        landreq._line — both print the confirmation words, neither alarms."""
        lr = self.row(state="SUPERSEDED", polarity="supersede", contrary=True,
                      contrary_state="landed", contrary_discharge="c")
        line = landreq._line(lr)
        html = self.render(b=self.board([landreq.card(lr)]))["b"]["html"]
        for text in (line, html):
            self.assertIn("CONFIRMATION: LANDED by design", text)
            self.assertNotIn("CONTRARY", text)
            self.assertNotIn("HONORED through succession", text)

    def test_honored_and_live_contrary_are_counted_separately_never_merged(self):
        """The header said "11 contrary" over 6 live + 5 honored (measured
        live). Each is its own count, and the honored row stays VISIBLE
        with its own words — excluded from the alarm, never silently dropped.
        (Where it is visible moved later the same day: a discharged row is
        counted CLOSED and renders in the closed strip, pinned by
        test_a_discharged_row_leaves_the_board_column_for_the_closed_strip.)"""
        out = self.render(b=self.board([
            self.row(id="a" * 12, state="CHANGES_REQUESTED", polarity="fix",
                     contrary=True, contrary_state="landed",
                     contrary_discharge="a"),
            self.row(id="b" * 12, state="CHANGES_REQUESTED", polarity="fix",
                     contrary=True, contrary_state="landed")]))
        html = out["b"]["html"]
        self.assertIn("1 contrary", html)
        self.assertIn("1 honored", html)
        self.assertIn("HONORED through succession (continuation)", html)
        self.assertIn("CONTRARY: LANDED despite FIX verdict", html)
        # the badge counts ONLY the live one
        self.assertEqual(out["b"]["nav"]["n"], 1)

    def test_an_unverified_discharge_stays_loud_and_says_why(self):
        """An uncomputed ancestry pair is never guessed quiet: the row stays
        in the contrary count, rings the badge, and names the uncertainty."""
        out = self.render(b=self.board([self.row(
            state="CHANGES_REQUESTED", polarity="fix", contrary=True,
            contrary_state="landed", contrary_discharge="unverified")]))
        html = out["b"]["html"]
        self.assertIn("CONTRARY? LANDED despite FIX verdict — succession "
                      "UNVERIFIED", html)
        self.assertIn("1 contrary", html)
        self.assertNotIn("honored", html)
        self.assertEqual(out["b"]["nav"]["n"], 1)
        self.assertIn('class="lrrow alarm"', html)

    def test_an_old_server_row_without_the_stamp_stays_loud(self):
        """Fail-closed: a wire row that never carried contrary_discharge (the
        exact pre-fix shape) renders the alarm, not the friendlier word."""
        row = self.row(state="CHANGES_REQUESTED", polarity="fix",
                       contrary=True, contrary_state="landed")
        del row["contrary_discharge"]
        out = self.render(b=self.board([row]))
        self.assertIn("CONTRARY: LANDED despite FIX verdict", out["b"]["html"])
        self.assertEqual(out["b"]["nav"]["n"], 1)

    def test_an_honored_row_that_is_also_stalled_is_quiet_on_every_count(self):
        """codex-2's blocker on the first cut: the composite honored+stalled
        row was quiet on the home band and STALLED-loud here — the alarm
        mark, the ⚠, "1 stalled · 1 honored" in the header, and badge 1.
        One predicate (lrHonored) now answers for every surface: honored
        wins over the stalled ALARM, while `stalled` itself rides the wire
        untouched."""
        row = self.row(state="CHANGES_REQUESTED", polarity="fix",
                       contrary=True, contrary_state="landed",
                       contrary_discharge="a", stalled=True)
        out = self.render(b=self.board([row], stalled_ids=[row["id"]]))
        html = out["b"]["html"]
        self.assertIn("HONORED through succession (continuation)", html)
        self.assertNotIn("STALLED past its stage threshold", html)
        self.assertNotIn('class="lrrow alarm"', html)
        self.assertNotIn("⚠", html)
        self.assertIn("0 stalled", html)
        self.assertIn("1 honored", html)
        self.assertNotIn("1 stalled", html)
        self.assertEqual(out["b"]["nav"]["n"], 0)
        # THE LOUD CONTROL on the same composite: strip the stamp and the row
        # alarms on all of them at once — stalled mark, contrary mark, header
        # terms, badge.
        # RE-DERIVE, do not just strip the stamp. The classification rides the
        # wire on its own field now, so mutating only `contrary_discharge`
        # builds a payload the server cannot produce (no stamp, still honored)
        # and the row would stay quiet for a reason no production read has.
        # Recomputing through the SAME predicate the server uses is what keeps
        # this control honest.
        loud = dict(row, contrary_discharge=None)
        loud["honored"] = landreq.honored_display(loud)
        out = self.render(b=self.board([loud], stalled_ids=[loud["id"]]))
        html = out["b"]["html"]
        self.assertIn("CONTRARY: LANDED despite FIX verdict", html)
        self.assertIn("STALLED past its stage threshold", html)
        self.assertIn("1 stalled", html)
        self.assertIn("1 contrary", html)
        self.assertNotIn("honored</span>", html)
        self.assertEqual(out["b"]["nav"]["n"], 1)

    def test_the_card_and_the_cli_cannot_read_one_honored_row_differently(self):
        """PARITY, pinned end to end: the SAME projected row goes through
        landreq.card() (the exact wire shape) into the REAL browser renderer,
        and through landreq._line (the exact `lr list` renderer). If card()
        ever drops the stamp again, the renderer prints CONTRARY here while
        _line prints HONORED, and this fails."""
        lr = self.row(state="CHANGES_REQUESTED", polarity="fix", contrary=True,
                      contrary_state="landed", contrary_discharge="a")
        wire = landreq.card(lr)
        self.assertEqual(wire["contrary_discharge"], "a")
        line = landreq._line(lr)
        self.assertIn("HONORED through succession (continuation)", line)
        self.assertNotIn("CONTRARY:", line)
        html = self.render(b=self.board([wire]))["b"]["html"]
        self.assertIn("HONORED through succession (continuation)", html)
        self.assertNotIn("CONTRARY:", html)
        # the LOUD control through the same pair of renderers: strip the
        # stamp and both surfaces alarm again, together.
        loud = dict(lr, contrary_discharge=None)
        self.assertIn("CONTRARY: LANDED despite FIX verdict",
                      landreq._line(loud))
        self.assertIn("CONTRARY: LANDED despite FIX verdict",
                      self.render(b=self.board([landreq.card(loud)]))["b"]["html"])

    def test_the_composite_honored_stalled_row_reads_equal_on_every_surface(self):  # noqa: VACUOUS_ASSERTION — the loop walks a LITERAL five-stamp tuple (can never be empty), and its "a"/"b"/"c" rows are the unconditional positive controls: assertEqual(...quiet-word in text, True) demands the banner rendered on both surfaces, so a renderer that draws nothing fails the very first iteration
        """THE SEAM ROW of codex-2's re-review: honored AND stalled at once.
        The same dict goes through landreq.honored_display (the python
        authority), landreq._line, landreq.card() and the browser renderer +
        badge — every reader must give the SAME verdict: quiet with the
        stamp ("a"/"b" honored, "c" the confirmation instrument, each in its
        own words), alarm-loud without it. Two readers of one datum may not
        disagree, in either direction."""
        lr = self.row(state="CHANGES_REQUESTED", polarity="fix", contrary=True,
                      contrary_state="landed", contrary_discharge="a",
                      stalled=True)
        for stamp, quiet in ((("a"), True), ("b", True), ("c", True),
                             ("unverified", False), (None, False)):
            lr["contrary_discharge"] = stamp
            self.assertEqual(landreq.honored_display(lr), quiet, stamp)
            line = landreq._line(lr)
            wire = landreq.card(lr)
            self.assertEqual(wire["contrary_discharge"], stamp)
            self.assertTrue(wire["stalled"], stamp)     # the FIELD still rides
            out = self.render(b=self.board([wire], stalled_ids=[wire["id"]]))
            html = out["b"]["html"]
            word = "CONFIRMATION:" if stamp == "c" \
                else "HONORED through succession"
            for surface, text in (("cli", line), ("web", html)):
                self.assertEqual(word in text, quiet, (stamp, surface))
                self.assertEqual("STALLED" not in text, quiet,
                                 (stamp, surface))
            self.assertEqual(out["b"]["nav"]["n"], 0 if quiet else 1, stamp)

    def test_a_stalled_row_alarms_and_names_who_owes_it(self):
        out = self.render(b=self.board([self.row(stalled=True, owed_by="reviewer")],
                                       stalled_ids=["0123456789ab"]))
        html = out["b"]["html"]
        self.assertIn("STALLED past its stage threshold — owed by reviewer", html)
        self.assertIn("1 stalled", html)

    def test_an_unmeasurable_row_is_annotated_not_left_looking_healthy(self):
        out = self.render(b=self.board(
            [self.row(state="REVIEWED", polarity=None)],
            unmeasurable=[{"id": "0123456789ab", "reason": "polarity UNDECLARED"}]))
        html = out["b"]["html"]
        self.assertIn("NOT MEASURABLE: polarity UNDECLARED", html)
        self.assertIn("UNDECLARED", html)
        self.assertIn("1 unmeasurable", html)

    def test_the_freshness_header_says_unknown_rather_than_zero(self):
        out = self.render(b=self.board([], read_age_s=12, ledger_age_s=None))
        html = out["b"]["html"]
        self.assertIn("ledger read 12s ago", html)
        self.assertIn("newest ledger write unknown", html)

    def test_ABANDONED_renders_UNKNOWN_without_landing_or_withdrawal_claims(self):
        ghost = self.row(state="ABANDONED", abandoned=True,
                         abandon_reason="rewrite <destroyed> evidence",
                         abandon_land_state="UNKNOWN", land_state="UNKNOWN",
                         observable=False, landed=False, merged_local=False,
                         owed_by="nobody", polarity="fix")
        out = self.render(b=self.board([], closed_recent=[ghost]))
        html = out["b"]["html"]
        self.assertIn("ABANDONED — LAND STATE UNKNOWN", html)
        self.assertIn("reviewed work written off", html)
        self.assertIn("rewrite &lt;destroyed&gt; evidence", html)
        self.assertIn("LAND STATE UNKNOWN — commit evidence was destroyed", html)
        self.assertNotIn("not on trunk", html)
        self.assertNotIn('class="lrrow landed"', html)
        self.assertNotIn("WITHDRAWN", html)

    def test_the_closed_footer_never_calls_a_withdrawn_lane_landed(self):
        """TERMINAL covers SUPERSEDED, withdrawn and closed-by-landing rows, so
        the footer claims only that they CLOSED and each row prints its own
        state."""
        out = self.render(b=self.board([], closed_recent=[
            self.row(state="CHANGES_REQUESTED", withdrawn=True, polarity="fix")]))
        html = out["b"]["html"]
        self.assertIn("closed in the last 24h: 1", html)
        self.assertNotIn("landed in the last 24h", html)
        self.assertIn("CHANGES_REQUESTED", html)

    # ── nothing has answered yet is its own state ───────────────────────
    def test_the_first_paint_says_NOT_READ_YET_rather_than_drawing_nothing(self):
        """@codex's finding 3. Before this the section was an empty
        <section id="lrsec"> until a response arrived, and a hung /api/lr left
        it that way forever — pixel-identical to a healthy quiet board."""
        out = self.render(waiting={"pending": True})
        html = out["waiting"]["html"]
        self.assertIn("READING THE LAND PIPELINE", html)
        self.assertIn("no read has COMPLETED", html)
        self.assertIn("NOT READ YET", html)
        self.assertNotIn("lrrow", html)
        self.assertNotIn("0 in flight", html)
        self.assertNotIn("nothing closed in the last 24h", html)

    def test_pending_and_unreadable_do_not_render_the_same(self):
        """"nothing has answered" and "the ledger is unreadable" are different
        facts and the owner acts differently on each."""
        out = self.render(waiting={"pending": True},
                          dead={"unavailable": "PermissionError: denied",
                                "loops": []})
        self.assertIn("lrpending", out["waiting"]["html"])
        self.assertNotIn("lrunknown", out["waiting"]["html"])
        self.assertIn("lrunknown", out["dead"]["html"])
        self.assertNotIn("lrpending", out["dead"]["html"])
        self.assertNotIn("UNREADABLE", out["waiting"]["html"])

    def test_the_nav_badge_is_a_question_mark_before_the_first_answer(self):
        """A zero on the badge is a COUNT, and the count of a board nobody has
        read yet does not exist."""
        out = self.render(waiting={"pending": True})
        self.assertEqual(out["waiting"]["nav"]["n"], "?")
        self.assertIn("has not been read yet", out["waiting"]["nav"]["title"])

    # ── the closed footer never edits its own number ────────────────────
    def test_a_capped_closed_list_reports_the_TOTAL_and_names_the_drop(self):
        """13 closed lanes rendered as the number 12, because the footer printed
        the length of the list it had already truncated."""
        rows = [self.row(id="row%02d" % i) for i in range(12)]
        out = self.render(b=self.board([], closed_recent=rows, closed_total=13))
        html = out["b"]["html"]
        self.assertIn("closed in the last 24h: 13", html)
        self.assertIn("showing the newest 12", html)
        self.assertIn("1 not shown", html)

    def test_an_uncapped_closed_list_says_nothing_about_dropping(self):
        out = self.render(b=self.board([], closed_recent=[self.row()],
                                       closed_total=1))
        html = out["b"]["html"]
        self.assertIn("closed in the last 24h: 1", html)
        self.assertNotIn("not shown", html)

    def test_a_missing_total_is_UNKNOWN_and_never_the_list_length(self):
        """The absent-field law applied to a count: if the server did not send
        the total, the card may not promote the length of what it received."""
        d = self.board([], closed_recent=[self.row(), self.row()])
        d.pop("closed_total")
        out = self.render(b=d)
        html = out["b"]["html"]
        self.assertIn("TOTAL UNKNOWN", html)
        self.assertNotIn("closed in the last 24h: 2 ", html)

    def test_rows_closed_at_an_unknown_time_are_counted_never_dropped(self):
        """The lie this replaces: "nothing closed in the last 24h" printed over
        an approve from Tuesday whose change merged a minute ago."""
        out = self.render(b=self.board([], closed_unknown_when=3))
        html = out["b"]["html"]
        self.assertIn("3 further rows are CLOSED at an UNKNOWN time", html)
        self.assertIn("cannot say whether they closed in the last 24h", html)
        self.assertNotIn("nothing closed in the last 24h", html)

    def test_the_footer_may_still_say_nothing_closed_when_it_KNOWS_that(self):
        """The claim is not forbidden — it is earned. With no closed rows and no
        undateable ones, "nothing closed" is a fact the record supports."""
        out = self.render(b=self.board([self.row()]))
        self.assertIn("nothing closed in the last 24h", out["b"]["html"])

    def test_a_closed_row_with_no_closure_stamp_says_so_when_expanded(self):
        out = self.render(b=self.board([], closed_recent=[self.row(closed_ts=None)],
                                       closed_total=1))
        html = out["b"]["html"]
        self.assertIn("WHEN this closed is UNKNOWN", html)
        self.assertIn("git records no moment for the merge", html)

    def test_an_unreadable_closure_stamp_marks_the_row_without_a_tap(self):
        """@codex's SECOND audit, finding 2, at the surface. The row-level
        mark, which is all the owner sees on a phone until he taps — and a
        corrupt stamp is a REPAIR, not the routine absence a git-observed
        landing has."""
        out = self.render(
            bad=self.board([], closed_total=1, closed_recent=[
                self.row(closed_ts=None, closed_ts_unreadable=True)]),
            plain=self.board([], closed_total=1, closed_recent=[
                self.row(closed_ts=None)]))
        self.assertIn("CLOSURE STAMP UNREADABLE", out["bad"]["html"])
        self.assertIn("WHEN it closed is UNKNOWN", out["bad"]["html"])
        self.assertNotIn("CLOSURE STAMP UNREADABLE", out["plain"]["html"])

    def test_the_closure_line_itself_stops_printing_the_junk_stamp(self):
        """Asserted through its own DOM position so the mark above cannot
        stand in for it: `closed not-a-stamp` was the exact rendering, and a
        phrase both of them contain would leave this one unmeasured."""
        out = self.render(
            bad=self.board([], closed_total=1, closed_recent=[
                self.row(closed_ts=None, closed_ts_unreadable=True)]),
            absent=self.board([], closed_total=1, closed_recent=[
                self.row(closed_ts=None)]))
        self.assertIn("closed</b> the ledger's own retirement stamp",
                      out["bad"]["html"])
        self.assertNotIn("git records no moment", out["bad"]["html"])
        # …and the ordinary undated landing keeps its OWN sentence
        self.assertIn("closed</b> no closure stamp", out["absent"]["html"])

    def test_an_in_flight_row_is_not_asked_when_it_closed(self):
        """The closure line belongs to the closed footer; on a live row it would
        be a question with no meaning and a permanent UNKNOWN beside it."""
        out = self.render(b=self.board([self.row()]))
        self.assertNotIn("WHEN this closed is UNKNOWN", out["b"]["html"])

    # ── I could not look, versus there is nothing there ─────────────────
    def test_an_unreadable_receipt_ledger_marks_the_row_without_a_tap(self):
        """The row-level mark: visible on the collapsed row, which is all the
        owner sees on a phone until he taps."""
        out = self.render(
            unread=self.board([self.row(receipt_state="unreadable")]),
            absent=self.board([self.row(receipt_state="none")]))
        unread, absent = out["unread"]["html"], out["absent"]["html"]
        self.assertIn("LAND RECEIPT LEDGER UNREADABLE", unread)
        self.assertIn("UNKNOWN, not absent", unread)
        self.assertNotIn("UNREADABLE", absent)          # absence stays quiet

    def test_the_expanded_panel_line_itself_stops_saying_none(self):
        """@codex's exact reproduction was this line: "land receipt none" over a
        PermissionError. Asserted through its own DOM position so the row mark
        above cannot stand in for it — a phrase both of them contain would leave
        this rendering unmeasured."""
        out = self.render(
            unread=self.board([self.row(receipt_state="unreadable")]),
            absent=self.board([self.row(receipt_state="none")]))
        self.assertIn("land receipt</b> LEDGER UNREADABLE", out["unread"]["html"])
        self.assertIn("land-receipt index", out["unread"]["html"])
        self.assertIn("land receipt</b> none", out["absent"]["html"])

    def test_a_200_whose_SCHEMA_regressed_is_UNKNOWN_not_an_empty_board(self):
        """@codex-3's fifth. `lrCardHTML({})` rendered the happiest state this
        card has — "0 in flight", "the dispatch ledger READ cleanly", "nothing
        closed in the last 24h" — over a response carrying no board at all. A
        successful HTTP answer the card cannot read is an UNREAD pipeline, the
        same fact as an unreadable ledger arriving through a different door."""
        out = self.render(empty_object={},
                          wrong_type={"loops": "oops", "stalled_ids": [],
                                      "unmeasurable": [], "closed_recent": []},
                          renamed={"land_loops": [], "stalled_ids": [],
                                   "unmeasurable": [], "closed_recent": []})
        for name in ("empty_object", "wrong_type", "renamed"):
            html = out[name]["html"]
            self.assertIn("ANSWER NOT RECOGNISED — pipeline UNKNOWN", html, name)
            self.assertIn("NOT an empty pipeline", html, name)
            self.assertNotIn("READ cleanly", html, name)
            self.assertNotIn("0 in flight", html, name)
            self.assertNotIn("nothing closed in the last 24h", html, name)

    def test_the_badge_over_an_unrecognised_answer_is_not_a_zero(self):
        """Bound separately from the body: the badge is what reaches him from
        another tab, and a recogniser only the card consulted would leave the
        nav saying 0 over a strip saying UNKNOWN."""
        out = self.render(broken={}, fine=self.board([self.row()]))
        self.assertEqual(out["broken"]["nav"]["n"], "?")
        self.assertIn("cannot read", out["broken"]["nav"]["title"])
        self.assertEqual(out["fine"]["nav"]["n"], 0)     # a real board still counts

    def test_a_board_missing_only_its_TOTAL_is_still_a_board(self):
        """The recogniser may not swallow the degradations this card renders
        honestly on purpose: `closed_total` absent has its own "TOTAL UNKNOWN"
        branch, and demanding it here would turn a designed honest answer into
        an outage."""
        d = self.board([self.row()], closed_recent=[self.row()])
        d.pop("closed_total")
        out = self.render(b=d)
        self.assertIn("TOTAL UNKNOWN", out["b"]["html"])
        self.assertNotIn("ANSWER NOT RECOGNISED", out["b"]["html"])

    def test_an_impossible_closure_stamp_renders_as_its_own_state(self):
        out = self.render(
            impossible=self.board([], closed_total=1, closed_recent=[
                self.row(closed_ts=None, closed_ts_impossible=True)]),
            unreadable=self.board([], closed_total=1, closed_recent=[
                self.row(closed_ts=None, closed_ts_unreadable=True)]))
        bad = out["impossible"]["html"]
        self.assertIn("CLOSURE STAMP IMPOSSIBLE", bad)
        self.assertIn("closed</b> the ledger dates this row's closure in the FUTURE",
                      bad)
        # the two repairs are different instructions and must not swap
        self.assertNotIn("NOT A TIMESTAMP", bad)
        self.assertNotIn("CLOSURE STAMP IMPOSSIBLE", out["unreadable"]["html"])

    def test_a_refused_ledger_row_is_marked_on_the_collapsed_row(self):
        out = self.render(
            refused=self.board([self.row(state="OPEN",
                                         ledger_refused=["verdict"])]),
            clean=self.board([self.row()]))
        html = out["refused"]["html"]
        self.assertIn("LEDGER ROW REFUSED", html)
        self.assertIn("historical verdict event its fold refused", html)
        self.assertIn("current fields come only from accepted dispatch state", html)
        self.assertNotIn("LEDGER ROW REFUSED", out["clean"]["html"])

    def test_attest_mismatch_is_visible_without_overwriting_polarity(self):
        out = self.render(mismatch=self.board([self.row(
            state="REVIEWED", polarity="approve", attest_state="unverifiable",
            attest_detail="attest intent binding mismatch")]))
        html = out["mismatch"]["html"]
        self.assertIn("ATTEST UNVERIFIABLE", html)
        self.assertIn("attest intent binding mismatch", html)
        self.assertIn("verdict polarity</b> APPROVE", html)
        self.assertIn("from dispatch store", html)
        self.assertIn("verdict attest</b> UNVERIFIABLE", html)
        self.assertIn("from attest sidecar", html)

    def test_a_row_that_carries_NO_receipt_state_does_not_print_none(self):
        """The THIRD rendering of "unreadable receipt storage" as "there is no
        receipt", and the only one living in the browser: `c.receipt_state ||
        "none"` made the card answer for a field it was never sent. The server
        no longer defaults it, so this is the branch that must not re-invent
        the same word one layer further out."""
        out = self.render(
            missing=self.board([self.row(receipt_state=None)]),
            absent=self.board([self.row(receipt_state="none")]))
        missing = out["missing"]["html"]
        self.assertIn("land receipt</b> NOT SENT", missing)
        self.assertIn("no receipt state at all", missing)
        self.assertNotIn("land receipt</b> none", missing)
        # …and a row that really was told "none" still says none
        self.assertIn("land receipt</b> none", out["absent"]["html"])

    def test_the_undated_closure_footer_does_not_blame_git_for_all_of_them(self):
        """"git observed the landing and the record carries no closure stamp"
        was printed over EVERY undateable closure — including a stamp that is
        not a timestamp and one dated in the future, which the window now sends
        here too. The count was honest and the explanation was not."""
        out = self.render(b=self.board([], closed_unknown_when=2))
        html = out["b"]["html"]
        self.assertIn("2 further rows are CLOSED at an UNKNOWN time", html)
        self.assertIn("no usable closure instant", html)
        self.assertIn("dated in the future", html)
        self.assertNotIn("git observed the landing and the record carries no", html)

    def test_a_dwell_that_was_never_measured_renders_as_a_question_mark(self):
        """dwell_s is 0 when the entry stamp could not be read, and 0m is the
        most flattering possible reading of the oldest possible row."""
        out = self.render(unknown=self.board([self.row(dwell_known=False,
                                                       dwell_s=0)]),
                          known=self.board([self.row(dwell_known=True,
                                                     dwell_s=0)]))
        self.assertIn('<span class="lrdwell">?</span>', out["unknown"]["html"])
        self.assertNotIn("0m", out["unknown"]["html"])
        self.assertIn('<span class="lrdwell">0m</span>', out["known"]["html"])

    def test_an_unmeasured_dwell_says_why_in_words(self):
        out = self.render(b=self.board([self.row(dwell_known=False)]))
        html = out["b"]["html"]
        self.assertIn("DWELL UNKNOWN", html)
        self.assertIn("age was never measured", html)

    def test_an_unobserved_row_does_not_claim_the_change_is_off_trunk(self):
        """helm reads trunk only once a verdict exists, so an OPEN row's
        landed/merged_local are both false because NOBODY LOOKED. "not on
        trunk" there is a git fact this card never obtained."""
        out = self.render(
            blind=self.board([self.row(state="OPEN", observable=False)]),
            seen=self.board([self.row(state="READY", observable=True)]))
        self.assertIn("NOT OBSERVED", out["blind"]["html"])
        self.assertIn("trunk position is UNKNOWN", out["blind"]["html"])
        self.assertIn("<b>trunk</b> not on trunk", out["seen"]["html"])
        self.assertNotIn("NOT OBSERVED", out["seen"]["html"])

    def test_the_timeline_distinguishes_its_three_kinds_of_missing_stamp(self):
        """The eleventh defect at the surface. One rendering answered for all
        three: a corrupt stamp printed verbatim (`READY not-a-stamp`), and any
        absent one explained as "(git-observed…)" — which is true of the
        landing step alone and is a git reading an OPEN row never got."""
        out = self.render(b=self.board([self.row(timeline=[
            {"state": "OPEN", "ts": None},
            {"state": "AWAITING_REVIEW", "ts": "2026-07-30T00:00:00Z"},
            {"state": "READY", "ts": None, "ts_unreadable": True},
            {"state": "LANDED", "ts": None, "observed": True}])]))
        html = out["b"]["html"]
        self.assertIn("OPEN (no stamp on the ledger event)", html)
        self.assertIn("AWAITING_REVIEW 2026-07-30T00:00:00Z", html)
        self.assertIn("READY (the ledger's stamp here is NOT A TIMESTAMP)",
                      html)
        self.assertIn("LANDED (git-observed, no ledger stamp)", html)

    def test_the_timeline_names_the_FOURTH_kind_of_missing_stamp(self):
        """A stamp that parses and is dated in the future. Its own sentence,
        because "not a timestamp" sends the reader to re-read something that
        reads perfectly."""
        out = self.render(b=self.board([self.row(timeline=[
            {"state": "OPEN", "ts": "2026-07-30T00:00:00Z"},
            {"state": "WITHDRAWN", "ts": None, "ts_impossible": True}])]))
        html = out["b"]["html"]
        self.assertIn("WITHDRAWN (the ledger's stamp here is dated in the FUTURE)",
                      html)
        self.assertNotIn("WITHDRAWN (the ledger's stamp here is NOT A TIMESTAMP)",
                         html)
        self.assertNotIn("2099", html)

    def test_an_unstamped_step_is_not_called_git_observed(self):
        """Pinned as a NEGATIVE, because the failure was a rendering that was
        RIGHT for one step and applied to all of them."""
        out = self.render(b=self.board([self.row(
            timeline=[{"state": "OPEN", "ts": None}])]))
        self.assertNotIn("git-observed", out["b"]["html"])

    def test_a_hostile_lane_name_cannot_inject_markup(self):
        out = self.render(b=self.board([self.row(lane="<img src=x onerror=1>")]))
        html = out["b"]["html"]
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    # ── the compact HOME in-flight projection (owner task #236) ───────────
    def test_HOME_inflight_distinguishes_unread_unknown_and_measured_zero(self):
        out = self.render(
            pending={"__inflight": {"pending": True}},
            warming={"__inflight": {"warming": True}},
            down={"__inflight": {"unavailable": "PermissionError: denied"}},
            malformed={"__inflight": {}},
            empty={"__inflight": self.board([])})
        self.assertIn("not read yet", out["pending"]["html"])
        self.assertIn("reading…", out["warming"]["html"])
        self.assertNotIn("UNKNOWN", out["warming"]["html"])
        self.assertIn("UNKNOWN", out["down"]["html"])
        self.assertIn("PermissionError: denied", out["down"]["html"])
        self.assertIn("UNKNOWN", out["malformed"]["html"])
        self.assertNotIn("0 in flight", out["malformed"]["html"])
        self.assertIn("0 in flight — pipeline read cleanly", out["empty"]["html"])
        for name in out:
            self.assertIn('href="#ledger"', out[name]["html"], name)

    def test_HOME_inflight_drops_honored_rows_the_ledger_board_already_drops(self):
        """OWNER-OBSERVED 2026-08-05: home rendered 14 rows of which 10 were
        DISCHARGED, under a header whose own count said 5. The ledger board
        already partitions on lrHonored — "the board's SUPERSEDED column holds
        only undischarged rows" — and this glance list did not, so one card gave
        two answers to "what is in flight" and the owner read the longer one.

        Owner, on seeing them gone from the ledger and still on home: "it's still
        on the homepage and that is crazy to me ... i think you will find me
        addressing those first as contraries but always as superseded items, at
        least 20 times in the last 24h."

        The MUST-HIT control is the moving lane's own name: an assertion that
        only counted rows would pass on a render that dropped EVERYTHING, which
        is the failure this test would otherwise invite."""
        moving = self.row(id="a" * 12, lane="still-moving", state="AWAITING_REVIEW")
        done1 = self.row(id="b" * 12, lane="already-discharged-one",
                         state="SUPERSEDED", honored=True)
        done2 = self.row(id="c" * 12, lane="already-discharged-two",
                         state="SUPERSEDED", honored=True)
        out = self.render(
            mixed={"__inflight": self.board([moving, done1, done2])},
            all_done={"__inflight": self.board([done1, done2])})
        html = out["mixed"]["html"]
        # MUST-HIT: the one moving lane RENDERS — without this, "the discharged
        # lanes are absent" would also pass on an empty card.
        self.assertIn("still-moving", html)
        self.assertNotIn("already-discharged-one", html)
        self.assertNotIn("already-discharged-two", html)
        # a board of nothing BUT discharged rows is zero in flight, not a list
        self.assertIn("0 in flight — pipeline read cleanly",
                      out["all_done"]["html"])

    def test_HOME_inflight_formats_the_server_selected_ball_holder(self):
        unknown = self.row(id="e" * 12, lane="unknown", state="OPEN")
        unknown.pop("holder_role")
        rows = [
            self.row(id="a" * 12, lane="build", state="AWAITING_BUILD",
                     owed_by="builder", holder_role="builder",
                     holder_seat="build-seat"),
            self.row(id="b" * 12, lane="review", state="AWAITING_REVIEW",
                     owed_by="reviewer", holder_role="reviewer",
                     holder_seat="review-seat"),
            self.row(id="c" * 12, lane="changes", state="CHANGES_REQUESTED",
                     owed_by="author", holder_role="author",
                     holder_seat="author-seat"),
            self.row(id="d" * 12, lane="ready", state="READY",
                     owed_by="lander", holder_role="lander"),
            unknown]
        html = self.render(x={"__inflight": self.board(rows)})["x"]["html"]
        for text in ("builder @build-seat", "reviewer @review-seat",
                     "author @author-seat", "lander (seat not recorded)",
                     "holder UNKNOWN"):
            self.assertIn(text, html)
        self.assertEqual(html.count('<a class="difrow'), 5)
        self.assertEqual(html.count('href="#ledger"'), 5)

    def test_HOME_inflight_never_rederives_confirmation_holder_in_the_browser(self):
        rows = [
            self.row(id="a" * 12, lane="raw", contrary=True,
                     contrary_state="landed", contrary_discharge=None,
                     owed_by="integrator", holder_role="integrator"),
            self.row(id="b" * 12, lane="continuation", contrary=True,
                     contrary_state="landed", contrary_discharge="a",
                     owed_by="integrator", holder_role="nobody"),
            self.row(id="c" * 12, lane="ladder", contrary=True,
                     contrary_state="landed", contrary_discharge="b",
                     owed_by="integrator", holder_role="nobody"),
            self.row(id="d" * 12, lane="confirmation", contrary=True,
                     contrary_state="landed", contrary_discharge="c",
                     owed_by="integrator", holder_role="nobody"),
            # Deliberately contradictory wire body: this is the mutation guard.
            # If the browser re-derives the holder instead of reading the named
            # server answer, this control changes text.
            #
            # CHANGED 2026-08-05, and it changed DIRECTION, not strength. It
            # used to be an HONORED row (discharge "c") carrying an
            # `integrator` holder, guarding `lrHonored(c) -> nobody`. Home now
            # drops honored rows — the owner saw 10 discharged rows of 14 under
            # a header counting 5 — so an honored row never reaches lrBall on
            # this surface and that control could no longer move. This is its
            # mirror over the OTHER half of ball_holder: `contrary && !honored
            # -> integrator`. It is a raw contrary, so it renders, and the
            # server names `nobody` — an answer no re-derivation from
            # contrary/lrHonored/owed_by can produce. Restore ANY browser-side
            # derivation and this row reads "integrator (seat not recorded)".
            self.row(id="e" * 12, lane="server-wins", contrary=True,
                     contrary_state="landed", contrary_discharge=None,
                     owed_by="integrator", holder_role="nobody")]
        out = self.render(home={"__inflight": self.board(rows)},
                          ledger=self.board(rows))
        html, card = out["home"]["html"], out["ledger"]["html"]
        # title + visible text carry the same answer on the raw contrary.
        self.assertEqual(html.count("integrator (seat not recorded)"), 2)
        # …and the contradiction row prints the server's word, once as visible
        # text (its title carries the same answer, hence 2 for bare "nobody").
        self.assertEqual(html.count(">nobody</span>"), 1)
        self.assertEqual(html.count("nobody"), 2)
        # WHY the counts above are not the old 4/3: the three honored rows are
        # off this surface now. Named, so a future drop of the whole list can
        # never be mistaken for this change.
        for lane in ("continuation", "ladder", "confirmation"):
            self.assertNotIn(lane, html)
        # The rows did not vanish from the RECORD — only from the glance list.
        # lrBall has no reader on the ledger (it renders `owed by` in the
        # expanded panel instead), so their holder projection is not asserted
        # here: there is no holder text on this surface to assert.
        for lane in (">continuation<", ">ladder<", ">confirmation<"):
            self.assertIn(lane, card)
        self.assertIn("SUPERSEDED-CLOSED: LANDED, and the APPROVE verdict was "
                      "HONORED through succession (continuation)", card)
        self.assertIn("CONFIRMATION: LANDED by design", card)
        # LOAD-BEARING contradiction: the real confirmation still carries the
        # enforcement role. Only the server-projected holder may quiet it.
        self.assertEqual(rows[3]["owed_by"], "integrator")
        self.assertEqual(rows[3]["holder_role"], "nobody")

    def test_HOME_inflight_puts_moving_first_without_reordering_its_peers(self):
        rows = [
            self.row(id="a" * 12, lane="contrary-first", state="REVIEWED",
                     contrary=True, contrary_state="landed"),
            self.row(id="b" * 12, lane="moving-one", state="AWAITING_BUILD"),
            self.row(id="c" * 12, lane="stalled-last", state="AWAITING_REVIEW",
                     stalled=True),
            self.row(id="d" * 12, lane="moving-two", state="CHANGES_REQUESTED")]
        html = self.render(x={"__inflight": self.board(rows)})["x"]["html"]
        self.assertLess(html.index("moving-one"), html.index("moving-two"))
        self.assertLess(html.index("moving-two"), html.index("contrary-first"))
        self.assertLess(html.index("contrary-first"), html.index("stalled-last"))
        self.assertEqual(html.count('data-moving="true"'), 2)

    def test_HOME_inflight_reuses_honored_contrary_and_stalled_marks(self):
        rows = [
            self.row(id="a" * 12, lane="honored", contrary=True,
                     contrary_state="landed", contrary_discharge="a", stalled=True),
            self.row(id="b" * 12, lane="raw-contrary", contrary=True,
                     contrary_state="landed"),
            self.row(id="c" * 12, lane="stalled", stalled=True),
            self.row(id="d" * 12, lane="<img src=x onerror=1>")]
        out = self.render(home={"__inflight": self.board(rows)},
                          ledger=self.board(rows))
        html, card = out["home"]["html"], out["ledger"]["html"]
        # CHANGED 2026-08-05 BY OWNER OBSERVATION, and the previous assertion
        # here was `assertIn('class="difrow honored"')` — home DID render
        # honored rows, deliberately, marked with an honored class. The owner
        # saw 10 discharged rows of 14 under a header counting 5 and said: "it's
        # still on the homepage and that is crazy to me ... i think you will
        # find me addressing those first as contraries but always as superseded
        # items, at least 20 times in the last 24h."
        # So home now DROPS them and the ledger keeps them. The mark itself is
        # not gone — it moved to the surface that still shows the row.
        self.assertNotIn("honored", html)
        self.assertIn("SUPERSEDED-CLOSED", card)
        # the LIVE alarms still render on home, unchanged
        self.assertIn("CONTRARY: LANDED", html)
        self.assertIn("STALLED past its stage threshold", html)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    # ── the kanban board (owner 2026-08-03: board by default, list one tap away)
    def test_kanban_is_the_default_and_carries_one_column_per_state(self):
        """lrCardHTML with NO view argument — how every pre-kanban caller and
        test invokes it — draws the BOARD: one column per state string the
        rows actually carry, and never the flat wall."""
        out = self.render(b=self.board([
            self.row(id="a" * 12, state="OPEN"),
            self.row(id="b" * 12, state="AWAITING_REVIEW"),
            self.row(id="c" * 12, state="AWAITING_REVIEW", lane="lane/second"),
            self.row(id="d" * 12, state="READY")]))
        html = out["b"]["html"]
        self.assertIn('class="lrkb"', html)
        self.assertEqual(html.count('class="lrkcol'), 3)  # OPEN·AWAITING_REVIEW·READY
        self.assertNotIn('<div class="lrrows">', html)    # the flat wall is the OTHER view
        for rid in ("a" * 12, "b" * 12, "c" * 12, "d" * 12):
            self.assertIn('data-id="%s"' % rid, html)     # grouping drops no row

    def test_the_columns_read_left_to_right_in_pipeline_order(self):
        """The rank mirrors landreq.STAGE_ORDER's direction, whatever order
        the server sent the rows in."""
        out = self.render(b=self.board([
            self.row(id="a" * 12, state="MERGED_LOCAL"),
            self.row(id="b" * 12, state="OPEN"),
            self.row(id="c" * 12, state="READY")]))
        html = out["b"]["html"]
        self.assertIn('class="lrkb"', html)       # positive control: a board
        self.assertIn(">OPEN<", html)             # …with every column drawn
        self.assertIn(">READY<", html)
        self.assertIn(">MERGED_LOCAL<", html)
        self.assertLess(html.index(">OPEN<"), html.index(">READY<"))
        self.assertLess(html.index(">READY<"), html.index(">MERGED_LOCAL<"))

    def test_a_state_the_board_has_never_met_still_gets_a_column(self):
        """Columns are DERIVED from the rows. A state this client has never
        heard of must appear — last, never dropped: a dropped column is
        dropped rows, which is the one thing this card exists to prevent."""
        out = self.render(b=self.board([
            self.row(id="a" * 12, state="OPEN"),
            self.row(id="b" * 12, state="SOMETHING_NEW")]))
        html = out["b"]["html"]
        self.assertIn(">SOMETHING_NEW<", html)
        self.assertLess(html.index(">OPEN<"), html.index(">SOMETHING_NEW<"))

    def test_READY_rung_variants_keep_their_own_column_beside_READY(self):
        """READY-SELF-REVIEW is the record refusing plain READY — the rung is
        the point, so it must not be laundered into the READY column, and it
        must sort with READY rather than falling off the end."""
        out = self.render(b=self.board([
            self.row(id="a" * 12, state="READY-SELF-REVIEW"),
            self.row(id="b" * 12, state="MERGED_LOCAL")]))
        html = out["b"]["html"]
        self.assertIn(">READY-SELF-REVIEW<", html)
        self.assertLess(html.index(">READY-SELF-REVIEW<"),
                        html.index(">MERGED_LOCAL<"))

    def test_alarms_stay_loud_on_the_board_card_and_column_both(self):
        """CONTRARY/STALLED must survive the re-arrangement: the card keeps
        its alarm class and marks, the column it sits in is edged and counted,
        and the header's per-fact counts change not at all."""
        out = self.render(b=self.board(
            [self.row(id="a" * 12, state="READY", stalled=True),
             self.row(id="b" * 12, state="READY")],
            stalled_ids=["a" * 12]))
        html = out["b"]["html"]
        self.assertIn('class="lrrow alarm"', html)
        self.assertIn("STALLED past its stage threshold", html)
        self.assertIn('class="lrkcol alarmed"', html)
        self.assertIn("⚠1", html)                    # the column's ⚠1
        self.assertIn("1 stalled", html)                  # the header count stays

    def test_an_unmeasurable_rows_annotation_survives_the_board(self):
        """The unmeasurable map is keyed by id; the board must thread each
        row's reason through to the same mark the list prints."""
        out = self.render(b=self.board(
            [self.row(id="a" * 12, state="REVIEWED", polarity=None)],
            unmeasurable=[{"id": "a" * 12,
                           "reason": "verdict polarity undeclared"}]))
        self.assertIn("NOT MEASURABLE: verdict polarity undeclared",
                      out["b"]["html"])

    def test_the_list_view_is_byte_identical_row_markup_under_a_flat_wall(self):
        """The toggle's contract: the SAME rows through the SAME renderer.
        Each row's lrRowHTML output must appear verbatim in BOTH views —
        only the grouping differs — and the list view is the exact pre-board
        flat wall."""
        rows = [self.row(id="a" * 12, state="OPEN"),
                self.row(id="b" * 12, state="READY", contrary=True,
                         contrary_state="landed", landed=True, polarity="fix")]
        out = self.render(
            kb={"__d": self.board(rows), "__view": "kanban"},
            li={"__d": self.board(rows), "__view": "list"},
            r0={"__row": rows[0]}, r1={"__row": rows[1]})
        kb, li = out["kb"]["html"], out["li"]["html"]
        self.assertIn('<div class="lrrows">', li)
        self.assertNotIn("lrkb", li)
        for r in (out["r0"]["html"], out["r1"]["html"]):
            self.assertIn(r, kb)
            self.assertIn(r, li)

    def test_both_views_offer_the_toggle_with_the_active_choice_lit(self):
        rows = [self.row()]
        out = self.render(kb={"__d": self.board(rows), "__view": "kanban"},
                          li={"__d": self.board(rows), "__view": "list"})
        self.assertIn('data-lrview="list"', out["kb"]["html"])
        self.assertIn('class="lrvopt on" data-lrview="kanban"', out["kb"]["html"])
        self.assertIn('class="lrvopt on" data-lrview="list"', out["li"]["html"])

    def test_the_UNKNOWN_strips_render_no_view_toggle(self):
        """A control that re-arranges rows is a lie on a strip that has none
        — and its presence would dress an unread pipeline as a working one."""
        out = self.render(p={"pending": True},
                          u={"unavailable": "denied", "loops": [],
                             "stalled_ids": [], "unmeasurable": [],
                             "closed_recent": []})
        self.assertIn("READING THE LAND PIPELINE", out["p"]["html"])
        self.assertNotIn("lrvtog", out["p"]["html"])
        self.assertIn("DISPATCH LEDGER UNREADABLE", out["u"]["html"])
        self.assertNotIn("lrvtog", out["u"]["html"])

    # ── a VERIFIED superseded row is another kind of CLOSED ──────────────
    # Owner 2026-08-04, over a screenshot of a 10-card SUPERSEDED column:
    # "i feel like superseded, if verified, should just be like another type
    # of closed. are we overcomplicating this? -- somewhere on the kanban list
    # needs to link to a list of closed items". Measured on the live board the
    # same day: 10 SUPERSEDED cards, of which 9 were discharged (4 honored
    # through succession, 5 confirmation rounds) and ONE was the genuine
    # contrary. These pin BOTH halves — the placement, and the door.

    @staticmethod
    def columns(html):
        """JUST THE BOARD COLUMNS — the kanban strip, cut before the closed
        footer. A row that "left the board" must be absent HERE: asserting
        over the whole card would pass while the row sat in the closed strip
        below, which is exactly where it is supposed to be."""
        return html.split('<div class="lrkb">')[1].split(
            '<button type="button" class="lrfoot"')[0]

    @staticmethod
    def closed_strip(html):
        """Everything from the closed box onward (the box, then the
        unknown-when footer). Rows nest <div>s, so this cuts at the top of
        the strip rather than trying to find its end."""
        return html.split('id="lrclosed" hidden>', 1)[1]

    def discharged_board(self):
        """One SUPERSEDED column, three rows, one of each kind: honored
        through succession, a confirmation round, and the GENUINE contrary
        that must not move."""
        return self.board([
            self.row(id="a" * 12, state="SUPERSEDED", polarity="supersede",
                     contrary=True, contrary_state="landed",
                     contrary_discharge="a", lane="lane/honored"),
            self.row(id="c" * 12, state="SUPERSEDED", polarity="supersede",
                     contrary=True, contrary_state="landed",
                     contrary_discharge="c", lane="lane/confirmation"),
            self.row(id="b" * 12, state="SUPERSEDED", polarity="supersede",
                     contrary=True, contrary_state="landed",
                     lane="lane/genuine-contrary")])

    def test_a_discharged_row_leaves_the_board_column_for_the_closed_strip(self):
        """THE OWNER'S COMPLAINT, as a placement assertion. The honored and
        confirmation rows must not be cards in the SUPERSEDED column, and
        must be findable in the closed strip — while the MUST-HIT control,
        the genuine contrary, STAYS. Both directions in one render, so a
        renderer that drew an empty board would fail the control."""
        out = self.render(b=self.discharged_board())
        html = out["b"]["html"]
        cols, strip = self.columns(html), self.closed_strip(html)
        # the control FIRST: the undischarged contrary is a card on the board
        self.assertIn('data-id="%s"' % ("b" * 12), cols)
        self.assertIn("CONTRARY: LANDED despite SUPERSEDE verdict", cols)
        # …and the two discharged rows are NOT
        self.assertNotIn('data-id="%s"' % ("a" * 12), cols)
        self.assertNotIn('data-id="%s"' % ("c" * 12), cols)
        # …they are in the closed strip, and the RECORD KEEPS THE DISTINCTION:
        # each still prints its own discharge line, never a plain retirement
        self.assertIn('data-id="%s"' % ("a" * 12), strip)
        self.assertIn('data-id="%s"' % ("c" * 12), strip)
        self.assertIn("SUPERSEDED-CLOSED: LANDED, and the SUPERSEDE verdict "
                      "was HONORED through succession (continuation)", strip)
        self.assertIn("CONFIRMATION: LANDED by design", strip)
        # the column is left holding exactly the one undischarged row
        self.assertEqual(cols.count('class="lrrow '), 1)
        self.assertIn('in this state">1</span>', cols)

    def test_the_board_carries_the_door_to_the_closed_list(self):
        """The second half of the ask: the board must LINK to the closed
        list, and it reuses the ONE closed strip rather than growing a second
        one — the column chip and the footer carry the same data-lrclosed
        toggle over the same #lrclosed box."""
        out = self.render(b=self.discharged_board())
        html = out["b"]["html"]
        cols = self.columns(html)
        self.assertIn('<button type="button" class="lrkfoot" data-lrclosed="1"',
                      cols)
        self.assertNotIn('class="lrkfoot" data-lrclosed="1" role="button"', cols)
        self.assertIn("2 discharged — closed · tap to show</button>", cols)
        # the card's own closed footer is the same NATIVE door, and says what
        # joined the closed accounting
        self.assertIn('<button type="button" class="lrfoot" data-lrclosed="1">',
                      html)
        self.assertNotIn('<div class="lrfoot" data-lrclosed="1">', html)
        self.assertIn("2 honored (verified superseded/confirmation — closed, "
                      "not in flight)", html)
        # ONE box, ONE list: the strip is not duplicated per column
        self.assertEqual(html.count('id="lrclosed"'), 1)

    def test_a_footer_only_closed_list_has_ONE_native_keyboard_door(self):
        """Without a discharged column the footer is the only route to CLOSED.

        A clickable div cannot receive focus and native Enter/Space never fires
        its onclick, so the owner would have no keyboard route to the list."""
        out = self.render(b=self.board(
            [self.row(id="a" * 12)],
            closed_recent=[self.row(id="c" * 12, state="CLOSED")]))
        html = out["b"]["html"]
        self.assertNotIn("lrkfoot", html)  # no column door: footer is the control
        self.assertEqual(html.count('data-lrclosed="1"'), 1)
        self.assertIn('<button type="button" class="lrfoot" data-lrclosed="1">',
                      html)
        self.assertNotIn('<div class="lrfoot" data-lrclosed="1">', html)

    def test_the_counts_line_counts_a_discharged_row_as_closed_not_in_flight(self):
        """The accounting moves with the placement: 3 rows, 2 discharged, and
        the header reads ONE in flight and ONE contrary — the number beside
        the column, not a parallel one. Read off the COUNTS SPAN, because the
        filed strip beside it legitimately says "3 in flight": that is the
        server's all-time population, and the two are different questions."""
        out = self.render(b=self.discharged_board())
        html = out["b"]["html"]
        counts = html.split('<span class="lrcounts">')[1].split("</span>")[0]
        self.assertIn("1 in flight", counts)
        self.assertIn("1 contrary", counts)
        self.assertNotIn("3 in flight", counts)
        self.assertNotIn("honored", counts)   # it is not an in-flight term
        self.assertEqual(out["b"]["nav"]["n"], 1)     # only the genuine one rings

    def test_a_column_whose_every_row_discharged_keeps_the_door(self):
        """A state with no undischarged rows left must not VANISH from the
        board: the column stays, card-less, carrying the door to where its
        rows went. A dropped column is a dropped trace — 9 rows disappearing
        with nothing on the board to say where."""
        out = self.render(b=self.board([
            self.row(id="a" * 12, state="SUPERSEDED", polarity="supersede",
                     contrary=True, contrary_state="landed",
                     contrary_discharge="a"),
            self.row(id="d" * 12, state="READY")]))
        html = out["b"]["html"]
        cols = self.columns(html)
        self.assertIn('data-id="%s"' % ("d" * 12), cols)   # control: READY drew
        self.assertIn(">SUPERSEDED<", cols)                # the column survives
        self.assertNotIn('data-id="%s"' % ("a" * 12), cols)
        self.assertIn("1 discharged — closed · tap to show", cols)
        self.assertIn('data-id="%s"' % ("a" * 12), self.closed_strip(html))

    def test_the_discharged_card_is_byte_identical_in_the_closed_strip(self):
        """The row does not become a different row by moving: the SAME
        lrRowHTML markup the board would have drawn — unmeasurable reason
        threaded and all — appears verbatim under the closed strip. It is
        drawn WITHOUT the closed-stamp panel, because the ledger has not
        retired it: "when did this close" is not its question."""
        r = self.row(id="a" * 12, state="SUPERSEDED", polarity="supersede",
                     contrary=True, contrary_state="landed",
                     contrary_discharge="a")
        out = self.render(
            b={"__d": self.board([r], unmeasurable=[
                {"id": "a" * 12, "reason": "verdict polarity undeclared"}])},
            r0={"__row": r, "__reason": "verdict polarity undeclared"})
        self.assertIn(out["r0"]["html"], self.closed_strip(out["b"]["html"]))
        self.assertIn("NOT MEASURABLE: verdict polarity undeclared",
                      out["b"]["html"])

    def test_a_discharged_unmeasurable_row_keeps_its_reason_not_the_live_count(self):
        """Moving a row to closed must move every live-accounting term with it.
        The full reason map still feeds the closed row, but its id no longer
        increments the header's unmeasurable count."""
        r = self.row(id="a" * 12, state="SUPERSEDED", polarity="supersede",
                     contrary=True, contrary_state="landed",
                     contrary_discharge="a")
        out = self.render(b=self.board([r], unmeasurable=[
            {"id": "a" * 12, "reason": "verdict polarity undeclared"}]))
        html = out["b"]["html"]
        counts = html.split('<span class="lrcounts">')[1].split("</span>")[0]
        self.assertIn("0 in flight", counts)
        self.assertNotIn("unmeasurable", counts)
        self.assertIn("NOT MEASURABLE: verdict polarity undeclared",
                      self.closed_strip(html))

    def test_an_unverified_superseded_row_is_never_moved_to_closed(self):
        """FAIL-CLOSED, the whole point of the predicate: an UNVERIFIED
        succession and a row with no stamp at all are both undischarged
        work. They stay on the board, in the column, in the alarm — beside
        the discharged control that proves the move happens at all."""
        out = self.render(b=self.board([
            self.row(id="u" * 12, state="SUPERSEDED", polarity="supersede",
                     contrary=True, contrary_state="landed",
                     contrary_discharge="unverified"),
            self.row(id="a" * 12, state="SUPERSEDED", polarity="supersede",
                     contrary=True, contrary_state="landed",
                     contrary_discharge="a")]))
        cols = self.columns(out["b"]["html"])
        self.assertIn('data-id="%s"' % ("u" * 12), cols)
        self.assertIn("succession UNVERIFIED", cols)
        self.assertNotIn('data-id="%s"' % ("a" * 12), cols)   # the control moved
        self.assertIn("1 in flight", out["b"]["html"])
        self.assertEqual(out["b"]["nav"]["n"], 1)

    def test_the_list_view_moves_the_same_rows_to_the_same_strip(self):
        """One partition, both views: the list wall and the board agree about
        which rows are work, or the toggle changes what is closed."""
        out = self.render(li={"__d": self.discharged_board(), "__view": "list"})
        html = out["li"]["html"]
        wall = html.split('<div class="lrrows">')[1].split(
            '<button type="button" class="lrfoot"')[0]
        self.assertIn('data-id="%s"' % ("b" * 12), wall)      # control: it drew
        self.assertNotIn('data-id="%s"' % ("a" * 12), wall)
        self.assertNotIn('data-id="%s"' % ("c" * 12), wall)
        self.assertIn('data-id="%s"' % ("a" * 12), self.closed_strip(html))
        self.assertIn('<button type="button" class="lrfoot" data-lrclosed="1">',
                      html)


class AHungReadIsAnAnswerTest(unittest.TestCase):
    """@codex's finding 3, run rather than reasoned about.

    `pollLr` used `fetch` with no deadline, so a hung `/api/lr` never rejected,
    the UNKNOWN branch never ran, and the card sat on its first paint — an empty
    `#lrsec` with the nav badge at 0 — for as long as the socket stayed open.
    Blank-and-zero is how "nothing outstanding" looks.

    This drives the REAL `j`/`lrShow`/`pollLr` out of assembled web UI against a
    fetch that accepts the request and never answers, and asserts what the
    element and the badge actually hold. A structural assertion (`the source
    mentions AbortController`) would pass against a timeout wired to nothing."""

    HUNG = """
const fs = require("fs");
const el = {innerHTML: ""};
const $ = s => (s === "#lrsec" ? el : null);
const $$ = () => [];
const LR_FETCH_MS = 300;            // the real constant, shortened for the probe
let NAV_LR = "?", NAV_LR_TITLE = "";
let LR_LAST = null;
const renderNav = () => {};
const lrWire = () => {};
const lrViewPref = () => "kanban";
const lrCardHTML = d => (d.pending ? "PENDING" : "UNAVAILABLE:" + d.unavailable);
const lrNav = d => (d.pending ? {n: "?", title: "not read yet"}
                              : {n: "?", title: "unknown"});
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
const first = {dom: el.innerHTML, nav: NAV_LR};
pollLr();
setTimeout(() => {
  process.stdout.write(JSON.stringify(
    {first, aborted, dom: el.innerHTML, nav: NAV_LR}));
  process.exit(0);
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

    def test_the_element_is_never_empty_and_the_badge_is_never_zero(self):
        """The exact reproduction: first DOM blank, badge 0."""
        self.assertEqual(self.out["first"]["dom"], "PENDING")
        self.assertEqual(self.out["first"]["nav"], "?")

    def test_a_hung_read_becomes_an_UNKNOWN_instead_of_staying_pending(self):
        """Without a deadline this stays "PENDING" forever, which is the bug:
        the card shows a state it can never leave."""
        self.assertTrue(self.out["aborted"], "the read carried no deadline")
        self.assertTrue(self.out["dom"].startswith("UNAVAILABLE:"),
                        self.out["dom"])
        self.assertIn("did not answer", self.out["dom"])
        self.assertIn("timed out after", self.out["dom"])
        self.assertEqual(self.out["nav"], "?")


class TheConsoleRendersItTest(unittest.TestCase):
    """The wiring leg. A card the page never mounts, polls or badges is a
    server field nothing renders — which is this project's recurring defect,
    not a hypothetical one."""

    def setUp(self):
        self.ui = web_ui_loader.read_text()

    def test_there_is_an_element_a_renderer_and_a_poll(self):
        self.assertIn('id="lrsec"', self.ui)
        self.assertIn("function lrCardHTML(", self.ui)
        self.assertIn('$("#lrsec").innerHTML = lrCardHTML(d, lrViewPref())', self.ui)
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

    def test_the_wall_is_the_ledgers_FIRST_tier_and_only_lives_there(self):
        """Owner 2026-07-28: the collapsed home copy was still "duplication of
        UI" — "the ledger represents it all". So the wall's element lives
        INSIDE #view-ledger, ahead of the signed tier, and exactly once: a
        second mount would give pollLr's by-id render two candidate homes and
        the owner two walls to disagree with each other."""
        self.assertEqual(self.ui.count('<section id="lrsec">'), 1)
        self.assertNotIn("lrfold", self.ui)  # noqa: VACUOUS_ASSERTION — the fold is deliberately DELETED page-wide; the count-of-one above is the positive control on the surviving element
        i = self.ui.index('<section id="lrsec">')
        self.assertGreater(i, self.ui.index('id="view-ledger"'))
        self.assertLess(i, self.ui.index('id="tiersigned"'))
        # ...and the pipeline tier leads the tier stack
        self.assertLess(self.ui.index('id="tierpipeline"'),
                        self.ui.index('id="tiersigned"'))

    def test_the_home_glance_row_carries_the_jump_to_the_wall(self):
        """The home tab keeps the one-line pipeline glance; with the detail on
        another tab, every branch of that row — pending, UNKNOWN, board —
        must carry the way there, or the glance is a dead end on a phone."""
        src = _extract_fn(self.ui, "dashLr")
        self.assertIn("data-goledger", src)
        self.assertEqual(src.count("+ go"), 3,
                         "pending, UNKNOWN and board branches each append the jump")
        self.assertIn('showView("ledger")', src)

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
        self.assertNotIn("view-helm", _extract_fn(self.ui, "pollLr"))
        self.assertNotIn("document.hidden", _extract_fn(self.ui, "pollLr"))
        # …and the schedule is a TOP-LEVEL statement (column zero), never one
        # reached only from inside a tab-visibility branch.
        self.assertIn("\nsetInterval(pollLr, LR_POLL_MS);\n", self.ui)

    def test_the_ledger_tab_gets_the_alarm_badge(self):
        """The badge points where the wall lives — the ledger tab — and its
        title says so; a badge on a tab that no longer holds the rows sends
        him to a page that cannot explain it."""
        self.assertIn('navBadge("ledger", NAV_LR', self.ui)
        self.assertNotIn('navBadge("helm", NAV_LR', self.ui)  # noqa: VACUOUS_ASSERTION — two badges off one count would disagree someday; the assertIn above is the positive control on the same call
        self.assertIn("on the ledger tab", _extract_fn(self.ui, "lrNav"))
        self.assertIn('navBadge("chat", NAV_CHAT', self.ui)   # unchanged

    def test_the_view_choice_is_stored_and_kanban_wins_when_unset(self):
        """lrViewPref: only the stored word "list" picks the list — an absent,
        ancient or mangled value all mean the DEFAULT, kanban — and the toggle
        stores only an explicit choice, never the default on its behalf."""
        src = _extract_fn(self.ui, "lrViewPref")
        self.assertIn('localStorage.getItem("helm.lrview")', src)
        self.assertIn('=== "list" ? "list" : "kanban"', src)
        self.assertIn('localStorage.setItem("helm.lrview", v)',
                      _extract_fn(self.ui, "lrWire"))

    def test_the_toggle_redraws_from_the_answer_in_hand_not_a_new_fetch(self):
        """A toggle that costs a poll is a 45s toggle; one that fetches is a
        second reader of /api/lr with its own failure modes."""
        src = _extract_fn(self.ui, "lrWire")
        self.assertIn("lrShow(LR_LAST)", src)
        self.assertNotIn("fetch(", src)
        self.assertNotIn('j("/api/lr"', src)

    def test_every_closed_door_opens_the_one_closed_box(self):
        """The owner asked the kanban to LINK to the closed list, so the board
        columns grew a chip beside the card footer — and a chip nothing wires
        is a control that does nothing. The handler selects ALL data-lrclosed
        elements ($$, not $): with the single-element `$` the column chip was
        dead markup and only the footer worked."""
        wire = _extract_fn(self.ui, "lrWire")
        self.assertIn('$$("#lrsec [data-lrclosed]")', wire)
        self.assertIn('$("#lrclosed")', wire)
        self.assertIn("box.hidden = !box.hidden", wire)
        # …and the board emits a native keyboard-operable control, not a div
        # whose role claims behavior the DOM does not provide.
        board = _extract_fn(self.ui, "lrKanbanHTML")
        self.assertIn('<button type="button" class="lrkfoot" data-lrclosed="1"',
                      board)
        self.assertNotIn('class="lrkfoot" data-lrclosed="1" role="button"', board)
        card = _extract_fn(self.ui, "lrCardHTML")
        self.assertIn('<button type="button" class="lrfoot" data-lrclosed="1"',
                      card)
        self.assertNotIn('<div class="lrfoot" data-lrclosed="1"', card)
        self.assertIn(".lrkcol .lrkfoot:focus-visible", self.ui)
        self.assertIn("#lrsec button.lrfoot:focus-visible", self.ui)

    def test_the_board_and_the_list_share_one_row_renderer(self):
        """The parity guarantee is structural: lrKanbanHTML draws its cards
        with lrRowHTML — the SAME function the list maps over — so the two
        views cannot drift apart row-wise. The byte-level probe lives in
        CardRuntimeTest; this pins the mechanism that makes it hold."""
        self.assertIn("lrRowHTML(c, unmeas.get(",
                      _extract_fn(self.ui, "lrKanbanHTML"))

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
            # after that does A's finally run — codex's meld shape; the prior
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
        # bare sleep (codex's meld shape — the sleep raced A's finally and
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


class RecentLandsRideTheSamePipelineBodyTest(LrApiBase, ReceiptBase):
    """The dashboard's RECENT LANDS row (0.2 council item 4 — the one claim on
    the page the owner can verify end-to-end), DERIVED FROM TRUNK and riding
    INSIDE the /api/lr cached body. One 30s-floored build — a second polling
    endpoint is the /api/chat starvation class — and its OWN `unavailable`,
    because trunk, the dispatch ledger and the receipt index all fail
    independently and folding those flags would render a readable half as
    UNKNOWN.

    THIS CLASS USED TO ASSERT THE OPPOSITE SOURCE, and the swap is the fix.
    The rows came from `land-receipts.jsonl`, whose only writer is the manual
    verb `helm lr land`, so the card could only show a land somebody had
    additionally remembered to witness — and the verb fails open, so forgetting
    was silent. Measured on the morning this changed: trunk carried 55 folds
    since the newest receipt and the card showed six rows, the newest sixteen
    hours old. No agent is in the read path now."""

    def test_a_trunk_with_no_folds_is_a_clean_zero_not_an_unknown(self):
        """"No lands" is a claim only a SUCCESSFUL read may make — and on a
        repo whose trunk carries no fold commit it may make it.

        THE CONTROL IS THE SECOND HALF, and it runs unconditionally: the same
        binding is read again after one fold exists and reports it. Without it
        the empty list above would be equally true of a reader that can never
        see anything, which is the one explanation that must be excluded."""
        self.dispatch(lane="lane/dash-alpha")
        rl = self.lr()["recent_lands"]
        self.assertEqual(rl["window_s"], 86400)      # the reading HAPPENED
        self.assertEqual(rl["rows"], [])
        self.assertIsNone(rl["unavailable"])
        self.assertEqual(rl["window_total"], 0)
        self.land_side()
        self.fold("dash-lane-control", self.side)
        rl = self.lr()["recent_lands"]
        self.assertEqual(len(rl["rows"]), 1, rl)
        self.assertEqual(rl["rows"][0]["lane"], "dash-lane-control")

    def test_a_fold_on_trunk_reaches_the_card_with_no_verb_run(self):
        """THE OWNER'S OBSERVATION, as an arm. Nothing records anything: the
        integrator merges, and the land is on the card. The row's ✓ is git's
        answer at READ time, and age_s is stamped at RESPONSE time like
        read_age_s so the browser's clock stays out of it."""
        self.land_side()
        fold = self.fold("dash-lane-one", self.side,
                         " (dispatch aabbccdd0011, fixture-reviewer APPROVE)")
        rl = self.lr()["recent_lands"]
        self.assertIsNone(rl["unavailable"])
        self.assertEqual(len(rl["rows"]), 1, rl)
        row = rl["rows"][0]
        self.assertEqual(row["lane"], "dash-lane-one")
        self.assertEqual(row["reviewed_tip"], self.side)
        self.assertEqual(row["fold_sha"], fold)
        self.assertIs(row["on_trunk"], True)
        self.assertEqual(row["how"], "ancestor")
        self.assertIsInstance(row["age_s"], int)
        self.assertGreaterEqual(row["age_s"], 0)
        # THE MUST-HIT CONTROL FOR THIS WHOLE FILE'S CLAIM: no receipt was
        # written, and the land is here anyway. Under the old source this
        # assertion could not have passed at all.
        self.assertIs(row["witnessed"], False)

    def test_a_lane_label_with_spaces_still_parses_to_lane_and_tip(self):
        """23 of this repo's 145 real folds carry spaces in the label, so the
        grammar's anchor is the first ` at <object name>`, not the first
        space. The control is the arm above, which has no spaces."""
        self.land_side()
        self.fold("dash lane names live state", self.side, " (dispatch abcd)")
        row = self.lr()["recent_lands"]["rows"][0]
        self.assertEqual(row["lane"], "dash lane names live state")
        self.assertEqual(row["reviewed_tip"], self.side)

    def test_a_recorded_receipt_is_a_BADGE_on_a_row_git_already_produced(self):
        """The receipt's real job. It changes `witnessed` and nothing else —
        the row is here either way. Paired with the unsigned arm above, this is
        the positive control proving the badge can answer both ways."""
        trunk = self.land_side()
        self.record(self.side, trunk)
        self.fold("dash-lane-signed", self.side)
        rows = self.lr()["recent_lands"]["rows"]
        self.assertEqual(len(rows), 1, rows)
        self.assertIs(rows[0]["witnessed"], True)

    def test_an_unreadable_receipt_index_hides_no_land_it_only_unknowns(self):
        """THE INVERSION, PINNED. A receipt index that cannot be read used to
        empty this card, because it WAS the card. Now it can only turn the
        badge to UNKNOWN: the lands stay, because trunk still carries them.
        The wire must survive it too — the receipt reader's failure markers are
        module-private OBJECT keys, and json.dumps raises on a non-string key,
        which would take down the whole /api/lr body."""
        self.land_side()
        self.fold("dash-lane-two", self.side)
        self.unreadable_receipts()
        body = self.lr()
        rl = body["recent_lands"]
        self.assertEqual(len(rl["rows"]), 1, rl)
        self.assertIsNone(rl["unavailable"])
        self.assertIsNone(rl["rows"][0]["witnessed"])
        json.dumps(body)                     # the wire must survive the failure
        # ...and the failure is CONTAINED: the dispatch-ledger half still read.
        self.assertIsNone(body["unavailable"])

    def test_an_unreadable_trunk_is_UNKNOWN_and_never_zero_lands(self):
        """The direction that IS fatal to this leg. No repository to read means
        what landed is UNKNOWN — a named refusal, never an empty happy list."""
        with mock.patch.object(dispatches, "_repo_info", return_value=None):
            rl = self.lr()["recent_lands"]
        self.assertEqual(rl["rows"], [])
        self.assertIsInstance(rl["unavailable"], str)
        self.assertIn("UNKNOWN", rl["unavailable"])
        self.assertIsNone(rl["window_total"])

    def test_the_cap_may_not_edit_the_window_count(self):
        """A cap is a display budget. The closed-lanes footer learned this by
        printing its own truncated length as a total; the lands card is told
        the window count separately so it can say "6 of N"."""
        self.land_side()
        for i in range(web._LR_LANDS_CAP + 2):
            self.fold("dash-lane-%d" % i, self.side)
        rl = self.lr()["recent_lands"]
        # The newest fold names itself, unconditionally: the counts below are
        # only meaningful over a list that actually carries the lands.
        self.assertIn("dash-lane-", rl["rows"][0]["lane"])
        self.assertEqual(len(rl["rows"]), web._LR_LANDS_CAP)
        self.assertEqual(rl["window_total"], web._LR_LANDS_CAP + 2)
        self.assertEqual(rl["window_s"], web._LR_LANDS_WINDOW_S)

    def test_a_non_fold_commit_on_trunk_is_not_a_land(self):
        """The grammar's negative control. Trunk carries plenty of commits that
        are not landings; only the integrator's fold subject is one — and the
        second half proves this reader sees that one, so the empty reading is
        about the GRAMMAR and not about a reader that never matches."""
        self.commit("an ordinary trunk commit")
        self.git("commit", "-q", "--allow-empty", "-m",
                 "integrate: dash-lane at %s onto trunk" % self.side[:8])
        rl = self.lr()["recent_lands"]
        self.assertEqual(rl["window_s"], 86400)      # the reading HAPPENED
        self.assertEqual(rl["rows"], [])
        self.assertEqual(rl["window_total"], 0)
        self.fold("dash-lane-real", self.side)
        rl = self.lr()["recent_lands"]
        self.assertEqual([r["lane"] for r in rl["rows"]], ["dash-lane-real"])

    def test_a_fold_whose_reviewed_tip_is_gone_is_still_a_visible_land(self):
        """A land is on the card because trunk carries the FOLD. Whether the
        reviewed tip can still be re-derived is a separate, weaker fact — and
        when it cannot, the row says UNKNOWN and stays."""
        self.fold("dash-lane-dead", "0" * 40)
        rows = self.lr()["recent_lands"]["rows"]
        self.assertEqual(len(rows), 1, rows)
        self.assertIsNone(rows[0]["on_trunk"])
        self.assertEqual(rows[0]["lane"], "dash-lane-dead")

    @unittest.skipIf(os.geteuid() == 0, "root reads through any mode bits")
    def test_a_dead_dispatch_ledger_does_not_erase_the_lands_reading(self):
        """The other direction of independence: pipeline UNKNOWN, recent lands
        still answered. Real chmod, nothing patched — a mock on the shared
        eventledger reader would knock out both halves and prove nothing."""
        self.land_side()
        self.fold("dash-lane-three", self.side)
        path = dispatches.ledger_path()
        self.dispatch(lane="lane/dash-beta")   # the ledger exists to chmod
        os.chmod(path, 0o000)
        try:
            body = self.lr()
        finally:
            os.chmod(path, 0o600)
        self.assertIsInstance(body["unavailable"], str)   # pipeline UNKNOWN
        self.assertIsNone(body["recent_lands"]["unavailable"])
        self.assertEqual(len(body["recent_lands"]["rows"]), 1)

    def test_the_native_chain_summary_rides_the_same_body(self):
        """The dashboard RECORD row's verification half. A tmp home has no
        chain records: count 0 with unavailable None — an empty chain READ
        CLEANLY, which is a different fact from an unreadable one."""
        nc = self.lr()["native_chain"]
        self.assertEqual(nc["count"], 0)
        self.assertIsNone(nc["unavailable"])
        self.assertIn("verified", nc)
        self.assertIn("head_index", nc)


class TheDashboardIsWiredTest(unittest.TestCase):
    """The wiring leg for the home dashboard band (owner 2026-07-28: "much
    more of an above-the-fold dashboard of the system and its components").
    Same rationale as TheConsoleRendersItTest: a renderer nothing mounts or
    feeds is a server field nothing renders."""

    def setUp(self):
        self.ui = web_ui_loader.read_text()

    def test_the_band_exists_and_home_holds_no_second_pipeline_copy(self):
        self.assertIn('<section id="dash">', self.ui)
        # the wall is NOT on the home tab any more — the collapsed copy was
        # the duplication the owner objected to; home ends at fleet notes
        home = self.ui.split('id="view-helm"', 1)[1].split('id="view-quota"', 1)[0]
        self.assertIn('id="dash"', home)      # positive control: the slice IS the home tab
        self.assertNotIn('id="lrsec"', home)  # noqa: VACUOUS_ASSERTION — the wall must be ABSENT from home; its presence on the ledger is pinned by test_the_wall_is_the_ledgers_FIRST_tier
        self.assertNotIn("lrfold", home)

    def test_every_cell_boots_saying_not_read_yet(self):
        """ZERO vs UNREAD is the band's whole law: before any poll answers,
        every cell must carry words, not an empty element a reader skims as
        a quiet zero."""
        band = self.ui.split('<section id="dash">', 1)[1].split("</section>", 1)[0]
        self.assertGreaterEqual(band.count("not read yet"), 6)

    def test_inflight_sits_between_pipeline_and_landed(self):  # noqa: VACUOUS_ASSERTION — all three unconditional index() calls prove each id exists before comparing their order
        band = self.ui.split('<section id="dash">', 1)[1].split("</section>", 1)[0]
        self.assertLess(band.index('id="dpipe"'), band.index('id="dinflight"'))
        self.assertLess(band.index('id="dinflight"'), band.index('id="dlands"'))

    def test_the_lr_renderer_feeds_the_band(self):
        self.assertIn("function dashLr(", self.ui)
        self.assertIn("dashLr(d)", _extract_fn(self.ui, "lrShow"))
        dash = _extract_fn(self.ui, "dashLr")
        self.assertIn('$("#dinflight")', dash)
        self.assertIn("lrInflightHTML(d)", dash)
        self.assertIn("lrInflightWire(inflight)", dash)
        # the fold (and its summary line) is gone with the home copy
        self.assertNotIn("lrfoldsum", self.ui)  # noqa: VACUOUS_ASSERTION — intentional page-wide deletion; the dashLr(d) assertIn above is the positive control on the surviving renderer

    def test_the_presence_poll_feeds_the_fleet_row(self):
        self.assertIn("function dashFleet(", self.ui)
        self.assertIn("dashFleet(list)", _extract_fn(self.ui, "chatPresence"))

    def test_the_dregg_strip_is_the_dashboards_record_row_not_a_second_copy(self):
        """The owner explicitly dislikes duplicated UI: ONE signing pulse,
        living inside the band. Exactly one element carries the id, and it is
        inside the dashboard section."""
        self.assertEqual(self.ui.count('id="dreggstrip"'), 1)
        band = self.ui.split('<section id="dash">', 1)[1].split("</section>", 1)[0]
        self.assertIn('id="dreggstrip"', band)

    def test_UNKNOWN_branches_exist_for_every_lr_fed_cell(self):
        """Each cell renders a named UNKNOWN/NOT SENT branch — the degrade
        path the hard rules demand — rather than throwing or going blank."""
        for fn in ("dashLands", "dashChain", "dashOwner", "lrInflightHTML"):
            src = _extract_fn(self.ui, fn)
            self.assertTrue("UNKNOWN" in src or "NOT SENT" in src, fn)
        self.assertIn("UNKNOWN", _extract_fn(self.ui, "dashLr"))

    def test_each_inflight_line_is_a_native_ledger_link_with_one_upgrade(self):
        row = _extract_fn(self.ui, "lrInflightRowHTML")
        wire = _extract_fn(self.ui, "lrInflightWire")
        self.assertIn('href="#ledger"', row)
        self.assertIn("data-goinflight", row)
        self.assertIn('showView("ledger")', wire)
        self.assertIn("preventDefault()", wire)
        self.assertNotIn("clipboard", wire)
        self.assertNotIn("hidden", wire)

    def test_a_row_that_git_cannot_answer_renders_the_word_UNKNOWN(self):
        """Today EVERY receipt answers UNKNOWN (the recorded commits were
        local merges git has since collected) — the measured truth the band
        must show as a word, never as a blank or a zero."""
        src = _extract_fn(self.ui, "dashLandRow")
        self.assertIn("? UNKNOWN", src)
        # tri-state, tested strictly: only true/false may claim an answer
        self.assertIn("on_trunk === true", src)
        self.assertIn("on_trunk === false", src)


class DashInflightWireRuntimeTest(unittest.TestCase):
    """Run the HOME line's real click upgrader: the native anchor preserves
    focus/URL semantics and JS switches the existing ledger tab."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        cls.tmp = tempfile.mkdtemp(prefix="helm-dashinflight-wire-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write("let shown = null, prevented = false, selector = null;\n"
                    "const showView = v => { shown = v; };\n"
                    "const link = {onclick: null};\n"
                    "const root = {querySelectorAll: s => { selector = s; return [link]; }};\n\n"
                    + _extract_fn(src, "lrInflightWire") + "\n\n"
                    + "lrInflightWire(root);\n"
                    "link.onclick({preventDefault: () => { prevented = true; }});\n"
                    "process.stdout.write(JSON.stringify({shown, prevented, selector}));\n")
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def test_activation_opens_ledger_without_a_second_detail_behavior(self):  # noqa: VACUOUS_ASSERTION — the runtime returns the queried link and selector before the navigation assertions
        p = subprocess.run([self.node, self.path], capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        got = json.loads(p.stdout)
        self.assertEqual(got, {"shown": "ledger", "prevented": True,
                               "selector": "[data-goinflight]"})


class DashPipelineBandRuntimeTest(unittest.TestCase):
    """The home band's pipeline row, run under node — the owner-visible surface
    that still called every open ledger row in flight after the ledger card had
    moved discharged rows to closed. The headline and bar partition LIVE work
    only; honored rows remain visible in the adjacent closed accounting."""

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
            f.write("""const found = {}, inflightLinks = [];
let shown = null, prevented = false;
const element = sel => {
  let html = "";
  return {
    get innerHTML() { return html; },
    set innerHTML(value) {
      html = value;
      if (sel === "#dinflight") {
        inflightLinks.length = 0;
        const n = (String(value).match(/data-goinflight/g) || []).length;
        for (let i = 0; i < n; i++) inflightLinks.push({onclick: null});
      }
    },
    querySelectorAll: query => query === "[data-goinflight]" ? inflightLinks : []
  };
};
const $ = sel => found[sel] || (found[sel] = element(sel));
const dashChain = () => {}, dashLands = () => {}, dashOwner = () => {};
const showView = view => { shown = view; };
let DASH_FLEET_TS = 0, DASH_FLEET_HTML = "";
""" + line[0] + "\n\n"
                    + "\n\n".join(_extract_fn(src, n)
                                  for n in ("lrDwell", "lrAgo", "lrHonored",
                                            "lrMarks", "lrIsBoard", "lrBall",
                                            "lrInflightMoving", "lrInflightRowHTML",
                                            "lrInflightHTML", "lrInflightWire",
                                            "dashLr"))
                    + """

const d = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
dashLr(d);
const link = inflightLinks[0] || null;
if (link && link.onclick)
  link.onclick({preventDefault: () => { prevented = true; }});
process.stdout.write(JSON.stringify({
  pipe: found["#dpipe"].innerHTML,
  inflight: found["#dinflight"].innerHTML,
  linkCount: inflightLinks.length,
  wired: !!(link && link.onclick),
  shown,
  prevented
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
        d.update(kw)
        path = os.path.join(self.tmp, "board.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f)
        p = subprocess.run([self.node, self.path, path],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def band(self, loops, **kw):
        return self.run_band(loops, **kw)["pipe"]

    @staticmethod
    def row(**kw):
        base = {"id": "0123456789ab", "contrary": False,
                "contrary_discharge": None, "stalled": False}
        base.update(kw)
        # DERIVED, never hand-typed. `honored` rides the wire now, and a
        # fixture that set it by hand could describe a payload the server
        # never sends — the exact drift that having two implementations of
        # this predicate created in the first place. A test may still override
        # it explicitly to exercise an old server (absent field, fail-closed).
        base.setdefault("honored", landreq.honored_display(base))
        return base

    def test_dashLr_wires_the_rendered_inflight_link_to_the_ledger(self):
        out = self.run_band([self.row()])
        self.assertIn('href="#ledger"', out["inflight"])
        self.assertEqual(out["linkCount"], 1)
        self.assertTrue(out["wired"])
        self.assertEqual(out["shown"], "ledger")
        self.assertTrue(out["prevented"])

    def test_an_honored_row_joins_closed_not_the_live_headline_or_bar(self):
        pipe = self.band([
            self.row(id="a" * 12, contrary=True, contrary_discharge="a",
                     stalled=True),   # its successor landed: closed, not live
            self.row(id="b" * 12, contrary=True),
            self.row(id="c" * 12)])
        self.assertIn('<span class="dnum">2</span> in flight', pipe)
        self.assertIn('<span class="dbad"><span class="dnum">1</span> contrary', pipe)
        self.assertIn('<span class="dnum">0</span> stalled', pipe)
        self.assertIn('class="dctr"', pipe)
        self.assertNotIn('class="dhon"', pipe)
        self.assertNotIn('</span> honored</span>', pipe)
        self.assertIn("0 closed 24h · 1 honored closed", pipe)

    def test_honored_only_reads_zero_in_flight_and_one_honored_closed(self):
        pipe = self.band([self.row(contrary=True, contrary_discharge="b")])
        self.assertIn('<span class="dnum">0</span> in flight', pipe)
        self.assertIn('<span class="dmut"><span class="dnum">0</span> contrary', pipe)
        self.assertIn("0 closed 24h · 1 honored closed", pipe)
        self.assertNotIn('class="dctr"', pipe)
        self.assertNotIn('class="dhon"', pipe)

    def test_a_confirmation_row_is_closed_while_the_genuine_contrary_stays_live(self):
        """A "c"-stamped confirmation round leaves the live partition through
        lrHonored, while the genuine contrary beside it keeps the alarm segment
        and headline — the must-stay control."""
        pipe = self.band([
            self.row(id="a" * 12, contrary=True, contrary_discharge="c",
                     stalled=True),   # landed-by-design: closed, not live
            self.row(id="b" * 12, contrary=True)])
        self.assertIn('<span class="dnum">1</span> in flight', pipe)
        self.assertIn('<span class="dbad"><span class="dnum">1</span> contrary', pipe)
        self.assertIn('<span class="dnum">0</span> stalled', pipe)
        self.assertIn("0 closed 24h · 1 honored closed", pipe)
        self.assertIn('class="dctr"', pipe)
        self.assertNotIn('class="dhon"', pipe)

    def test_a_discharged_unmeasurable_row_is_not_in_the_live_bar(self):
        pipe = self.band(
            [self.row(id="a" * 12, contrary=True, contrary_discharge="a")],
            unmeasurable=[{"id": "a" * 12,
                           "reason": "verdict polarity undeclared"}])
        self.assertIn('<span class="dnum">0</span> in flight', pipe)
        self.assertIn('<span class="dnum">0</span> unmeasurable', pipe)
        self.assertNotIn('class="dunm"', pipe)
        self.assertIn("0 closed 24h · 1 honored closed", pipe)

    def test_unverified_and_unstamped_both_stay_in_the_alarm(self):
        pipe = self.band([
            self.row(id="a" * 12, contrary=True,
                     contrary_discharge="unverified"),
            self.row(id="b" * 12, contrary=True)])
        self.assertIn('<span class="dbad"><span class="dnum">2</span> contrary', pipe)
        # no honored CHIP and no honored bar segment (the partition title
        # naming the honored class is static text and always present)
        self.assertNotIn("</span> honored", pipe)
        self.assertNotIn('class="dhon"', pipe)

    def test_a_board_without_honored_rows_adds_no_closed_honored_term(self):
        pipe = self.band([self.row(contrary=True)])
        self.assertNotIn("honored closed", pipe)
        self.assertNotIn('class="dhon"', pipe)
        self.assertIn('<span class="dbad"><span class="dnum">1</span> contrary', pipe)


class DashLandGroupingRuntimeTest(unittest.TestCase):
    """The LANDED card folds a chain's rounds into ONE land — run under node,
    so the assertion is about what the OWNER SEES, not about a python mirror.

    THESE ARMS EXIST BECAUSE THEIR ABSENCE WAS A BLOCKING FINDING. The first
    cut of the grouping shipped with eleven assertions and a mutation run that
    lived in /tmp: a story about a run that happened, unable to stop the bug
    coming back by so much as one line. @codex refused it on exactly that, and
    separately proved the false collapse these pin against — lane labels are
    reused across 2-10 distinct chain roots in the live ledger, so a
    (lane, trunk_sha) key merged two unrelated lands and labelled one of them
    "1 earlier round". Grouping now demands chain_root and FAILS OPEN without
    it, and the arm that matters most is the one asserting it does not merge."""

    EXTRACT = ["dashLandWitness", "dashLandRow", "dashLandVerdictKey",
               "dashLandPrimary", "dashLandGroups", "dashLandGroup"]

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        line = [ln for ln in src.splitlines() if ln.startswith("const esc = ")]
        assert len(line) == 1, "assembled web UI's esc definition moved"
        cls.tmp = tempfile.mkdtemp(prefix="helm-dashland-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write("const lrAgo = s => String(s) + \"s\";\n" + line[0] + "\n\n"
                    + "\n\n".join(_extract_fn(src, n) for n in cls.EXTRACT)
                    + """

const rows = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
const groups = dashLandGroups(rows);
process.stdout.write(JSON.stringify({
  groups: groups.length,
  html: groups.map(dashLandGroup).join(""),
  primaries: groups.map(g => dashLandPrimary(g).reviewed_tip || null),
  sizes: groups.map(g => g.length)
}));
""")
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def render(self, rows):
        path = os.path.join(self.tmp, "rows.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f)
        p = subprocess.run([self.node, self.path, path],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    @staticmethod
    def receipt(**kw):
        base = {"lane": "lane/example", "trunk_sha": "t" * 40,
                "reviewed_tip": "a" * 40, "trunk_ref": "refs/remotes/origin/main",
                "on_trunk": True, "how": "ancestor", "correlates_to": None,
                "correlation_why": None, "rescue_why": None,
                "ts": "2026-08-01T00:00:00Z", "age_s": 60}
        base.update(kw)
        # DERIVED, never hand-typed. `honored` rides the wire now, and a
        # fixture that set it by hand could describe a payload the server
        # never sends — the exact drift that having two implementations of
        # this predicate created in the first place. A test may still override
        # it explicitly to exercise an old server (absent field, fail-closed).
        base.setdefault("honored", landreq.honored_display(base))
        return base

    def test_distinct_chains_same_lane_and_trunk_never_merge(self):
        """@codex's exact refutation. Two lands that share a lane LABEL and a
        batch trunk but come from DIFFERENT chain roots are two lands. Merging
        them deletes a landing from the owner's view and mislabels it as a
        round of the other — the one direction the design forbids."""
        out = self.render([
            self.receipt(reviewed_tip="a" * 40, chain_root="R1"),
            self.receipt(reviewed_tip="b" * 40, chain_root="R2"),
        ])
        self.assertEqual(out["groups"], 2,
                         "distinct chain roots collapsed into one land")
        self.assertNotIn("earlier round", out["html"],
                         "an unrelated land was labelled a round of another")

    def test_no_chain_root_fails_open_to_separate_rows(self):
        """The default today: no receipt carries chain_root. Absent identity
        must render every receipt separately — repetitive, never merged.
        Repetition is a cost; invisibility is a lie."""
        out = self.render([
            self.receipt(reviewed_tip="a" * 40),
            self.receipt(reviewed_tip="b" * 40),
        ])
        self.assertEqual(out["groups"], 2, "grouped without proven identity")
        self.assertNotIn("<details", out["html"])

    def test_same_chain_rounds_collapse_to_one_land(self):
        """The feature itself: three receipts of one chain are ONE land, and
        the two non-landing rounds fold underneath."""
        out = self.render([
            self.receipt(reviewed_tip="c" * 40, trunk_sha="c" * 40, chain_root="R1"),
            self.receipt(reviewed_tip="a" * 40, chain_root="R1"),
            self.receipt(reviewed_tip="b" * 40, chain_root="R1"),
        ])
        self.assertEqual(out["groups"], 1)
        self.assertEqual(out["sizes"], [3])
        self.assertIn("2 earlier rounds", out["html"])
        self.assertIn("<details><summary", out["html"],
                      "agreeing rounds must collapse, not open")

    def test_primary_is_the_tip_that_equals_trunk_not_the_newest(self):
        """Which receipt speaks for the land is EVIDENTIARY: the round whose
        reviewed tip IS the trunk sha literally is the commit that landed.
        Newest-wins is a guess about recency dressed as an answer."""
        out = self.render([
            self.receipt(reviewed_tip="a" * 40, chain_root="R1", ts="2026-08-01T09:00:00Z"),
            self.receipt(reviewed_tip="t" * 40, chain_root="R1", ts="2026-08-01T01:00:00Z"),
        ])
        self.assertEqual(out["primaries"], ["t" * 40],
                         "the landing round must speak, not the newest")

    def test_disagreeing_rounds_open_and_say_so(self):
        """A summary that hides the deciding fact is worse than the flat list
        it replaced. Conflict is promoted into the summary and the group opens
        itself; only agreement is allowed to collapse quietly."""
        out = self.render([
            self.receipt(reviewed_tip="t" * 40, chain_root="R1", on_trunk=True),
            self.receipt(reviewed_tip="b" * 40, chain_root="R1", on_trunk=False),
        ])
        self.assertEqual(out["groups"], 1)
        self.assertIn("THEY DISAGREE", out["html"])
        self.assertIn("<details open>", out["html"])
        self.assertIn("dlsplit", out["html"])

    def test_correlates_and_unknown_are_a_disagreement_not_a_pair(self):
        """`≈ correlates` (the receipt's own evidence) and `? UNKNOWN` (nothing
        answers) are DIFFERENT answers. Folding them together would hide that
        one round has evidence and another has none."""
        out = self.render([
            self.receipt(reviewed_tip="a" * 40, chain_root="R1", on_trunk=None,
                         correlates_to="d" * 40, correlation_why="content identity"),
            self.receipt(reviewed_tip="b" * 40, chain_root="R1", on_trunk=None),
        ])
        self.assertIn("THEY DISAGREE", out["html"])

    def test_grouping_changes_what_is_shown_never_what_is_counted(self):
        """Every receipt still renders. Grouping is a VIEW over the record and
        must never drop a row from it."""
        rows = [self.receipt(reviewed_tip=c * 40, chain_root="R1") for c in "abc"]
        out = self.render(rows)
        self.assertEqual(out["html"].count('class="dland"'), 3,
                         "a receipt vanished from the card")

    def test_alluk_banner_reads_raw_receipts_not_group_primaries(self):
        """dashLands computes allUnk over rl.rows — the whole record — so the
        sentence keeps claiming something true about every receipt even when
        the view folds them."""
        src = _extract_fn(web_ui_loader.read_text(), "dashLands")
        self.assertIn("rl.rows.every", src,
                      "allUnk must read every receipt, not the group primaries")

    def test_an_unsigned_land_renders_as_a_visible_row_saying_unsigned(self):
        """THE WHOLE POINT OF THE REBUILD, as a string the owner can read. A
        land with no receipt is a ROW that says `unsigned`, never an absence.
        Asserting the rendered text, not the absence of a complaint."""
        out = self.render([self.receipt(witnessed=False)])
        self.assertEqual(out["html"].count('class="dland"'), 1, out["html"])
        self.assertIn(">unsigned<", out["html"])
        self.assertIn("trunk carries the fold", out["html"])

    def test_a_signed_land_renders_the_same_row_with_the_other_badge(self):
        """The positive control for the arm above: same row, other answer. If
        both arms rendered the same string the pair would prove nothing."""
        signed = self.render([self.receipt(witnessed=True)])["html"]
        unsigned = self.render([self.receipt(witnessed=False)])["html"]
        self.assertIn(">signed<", signed)
        self.assertNotIn(">unsigned<", signed)
        self.assertNotEqual(signed, unsigned)

    def test_an_unknown_signature_is_neither_signed_nor_unsigned(self):
        """An unreadable receipt index must not be rendered as an accusation.
        `witnessed: null` is UNKNOWN and reads as UNKNOWN."""
        html = self.render([self.receipt(witnessed=None)])["html"]
        self.assertIn(">signed ?<", html)
        self.assertIn("could not be read", html)


class DashLandsCardRuntimeTest(unittest.TestCase):
    """The LANDED card as a whole, run under node, because the owner reads the
    CARD and not a row. The sentences under the rows are claims about the
    record — how many lands the window holds, and whether the reviewed content
    behind them can still be re-derived — and a claim rendered wrong is the
    exact failure this card keeps having."""

    EXTRACT = ["dashLandWitness", "dashLandRow", "dashLandVerdictKey",
               "dashLandPrimary", "dashLandGroups", "dashLandGroup", "dashLands"]

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        line = [ln for ln in src.splitlines() if ln.startswith("const esc = ")]
        assert len(line) == 1, "assembled web UI's esc definition moved"
        cls.tmp = tempfile.mkdtemp(prefix="helm-dashlands-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            # One inert #dlands cell so the REAL dashLands runs unmodified and
            # its innerHTML IS the assertion — no python mirror of the string.
            f.write("const lrAgo = s => String(s) + \"s\";\n" + line[0] + """
let html = "";
const cell = {get innerHTML() { return html; },
              set innerHTML(v) { html = v; }};
const $ = sel => (sel === "#dlands" ? cell : null);

"""
                    + "\n\n".join(_extract_fn(src, n) for n in cls.EXTRACT)
                    + """

dashLands(JSON.parse(require("fs").readFileSync(process.argv[2], "utf8")), false);
process.stdout.write(html);
""")
            chk = subprocess.run([cls.node, "--check", cls.path],
                                 capture_output=True, text=True)
            assert chk.returncode == 0, "node --check failed:\n" + chk.stderr

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def render(self, **rl):
        path = os.path.join(self.tmp, "rl.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rl, f)
        p = subprocess.run([self.node, self.path, path],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    @staticmethod
    def row(**kw):
        base = {"lane": "dash-lane", "reviewed_tip": "a" * 40,
                "fold_sha": "f" * 40, "trunk_sha": None,
                "trunk_ref": "refs/remotes/origin/main", "on_trunk": True,
                "how": "ancestor", "witnessed": False, "correlates_to": None,
                "correlation_why": None, "rescue_why": None, "chain_root": None,
                "ts": "2026-08-05T00:00:00Z", "age_s": 60}
        base.update(kw)
        return base

    def test_a_missing_signature_never_removes_a_land_from_the_card(self):
        """THE LAW THE WHOLE REBUILD RESTS ON, pinned where it can actually be
        broken. The row-level arm could not see this: it renders groups
        directly and never runs dashLands, so a filter INSIDE the card survived
        it. Here the unsigned land must survive the card itself."""
        html = self.render(rows=[self.row(lane="dash-signed", witnessed=True),
                                 self.row(lane="dash-unsigned", witnessed=False),
                                 self.row(lane="dash-unknown", witnessed=None)],
                           window_total=3, window_s=86400, unavailable=None)
        self.assertEqual(html.count('class="dland"'), 3, html)
        for lane in ("dash-signed", "dash-unsigned", "dash-unknown"):
            self.assertIn(">" + lane + "</span>", html)

    def test_the_card_says_how_many_lands_the_window_holds(self):
        """THE OWNER'S SENTENCE. Six rows and 76 lands today is not "six
        lands" — the cap is a display budget and the card says so."""
        html = self.render(rows=[self.row(lane="dash-lane-%d" % i)
                                 for i in range(6)],
                           window_total=76, window_s=86400, unavailable=None)
        self.assertIn("newest 6 of ", html)
        self.assertIn(">76</span> lands in the last 24h", html)

    def test_a_window_the_card_already_shows_whole_adds_no_sentence(self):
        """The control for the arm above. When nothing is truncated there is
        nothing to disclose, and "and 0 more" would be noise."""
        html = self.render(rows=[self.row()], window_total=1, window_s=86400,
                           unavailable=None)
        self.assertNotIn("lands in the last", html)
        self.assertIn('class="dland"', html)      # the row still rendered

    def test_a_server_that_sends_no_window_count_invents_none(self):
        """A number the server did not send may not be manufactured from the
        row count — that is the truncation-as-total bug, re-entered from the
        browser side. The row assertion is the unconditional control: the card
        DID render, so the missing sentence is a refusal and not a blank card."""
        html = self.render(rows=[self.row()], unavailable=None)
        self.assertIn('class="dland"', html)
        self.assertNotIn("lands in the last", html)

    def test_a_window_count_that_is_not_a_number_is_refused_not_coerced(self):
        """THIS ARM EXISTS BECAUSE ITS ABSENCE SURVIVED A MUTANT. Deleting the
        `typeof` guard left the arm above green: an ABSENT count subtracts to
        NaN, and NaN > 0 is false, so the card was safe by accident rather than
        by rule. A count that is PRESENT and not a number is the case that
        separates them — JavaScript would coerce "8" into arithmetic and print
        a total the server never measured."""
        html = self.render(rows=[self.row()], window_total="8", window_s=86400,
                           unavailable=None)
        self.assertIn('class="dland"', html)
        self.assertNotIn("lands in the last", html)

    def test_an_empty_trunk_reading_names_trunk_not_the_receipt_index(self):
        """The zero sentence has to describe the source that answered. It said
        "the receipt index read cleanly and holds none" while the receipt index
        had stopped being the source."""
        html = self.render(rows=[], window_total=0, window_s=86400,
                           unavailable=None)
        self.assertIn("trunk read cleanly and carries no fold commits", html)
        self.assertNotIn("receipt index", html)

    def test_all_unknown_says_the_lands_are_real_and_the_tips_are_gone(self):
        """The most important sentence on the card when it fires. Under the old
        source an all-UNKNOWN card meant "the landings cannot be re-derived";
        now the landings are not in question at all — each row IS a trunk
        commit — and only the reviewed tips are unrecoverable."""
        html = self.render(rows=[self.row(on_trunk=None, how=None),
                                 self.row(on_trunk=None, how=None)],
                           window_total=2, window_s=86400, unavailable=None)
        self.assertIn("these lands ARE on trunk", html)
        self.assertIn("the reviewed tips they name are gone", html)

    def test_one_proven_row_silences_the_all_unknown_sentence(self):
        """The control: the sentence claims something about EVERY row, so one
        row git could prove must retract it. Both rows must still RENDER — the
        sentence is retracted, never the lands."""
        html = self.render(rows=[self.row(on_trunk=None, how=None), self.row()],
                           window_total=2, window_s=86400, unavailable=None)
        self.assertEqual(html.count('class="dland"'), 2, html)
        self.assertNotIn("these lands ARE on trunk", html)

    def test_an_unavailable_reading_is_UNKNOWN_and_never_an_empty_card(self):
        """A trunk that could not be read is a named refusal, not "no lands"."""
        html = self.render(rows=[], window_total=None,
                           unavailable="git could not read trunk — UNKNOWN")
        self.assertIn("UNKNOWN", html)
        self.assertIn("git could not read trunk", html)
        self.assertNotIn("carries no fold commits", html)


class TheGateChipGoesQuietTest(CardRuntimeTest):
    """OWNER RULING, decision d2f490d0, 2026-08-06: "the chip goes quiet when
    there is nothing to report ... gate state moves behind the card's expand
    with the rest of the detail."

    He was asked because it was a decision about what he wants to see, not a
    style bug. The measurement that made it a question: lrGate had exactly two
    reachable outcomes on a live board — "receipt NOT READ here" and
    "UNVERIFIED" — so it was negative on 100% of cards, and a badge every card
    wears is a badge the eye stops reading. That was the remaining half of
    task/333's alarm saturation."""

    def test_a_bound_receipt_leaves_the_headline_and_KEEPS_ITS_TOKEN(self):
        html = self.render(r={"__row": self.row(gate="abcdef0123456789")})["r"]["html"]
        headline, expand = html.split('<div class="lrx"', 1)
        self.assertNotIn('class="lrg', headline)
        self.assertNotIn("receipt NOT READ here", headline)
        # DEMOTION, NOT DELETION — and the token is the one thing a reader
        # needs to go check the receipt, so it must survive the move
        self.assertIn("gate:abcdef0123", expand)
        self.assertIn("has not read the gate receipt", expand)

    def test_no_receipt_is_quiet_too_and_the_expand_still_says_so(self):
        html = self.render(r={"__row": self.row()})["r"]["html"]
        headline, expand = html.split('<div class="lrx"', 1)
        self.assertNotIn('class="lrg', headline)
        self.assertNotIn("UNVERIFIED", headline)
        self.assertIn("nothing has gate-checked this row at all", expand)

    def test_GATE_N_A_STILL_PRINTS_because_it_is_not_bad_news(self):
        """THE CONTROL, and the whole reason the other two arms mean anything.
        The rule is that the chip prints only when it has something to SAY, not
        that lrGate was emptied — so the one outcome that is a true, narrow
        statement rather than an accusation has to survive. Without this arm, a
        build that deleted lrGate outright would pass both arms above."""
        html = self.render(
            r={"__row": self.row(close_reason="delivered-report")})["r"]["html"]
        headline = html.split('<div class="lrx"', 1)[0]
        self.assertIn('class="lrg"', headline)
        self.assertIn("GATE N/A", headline)


class TheOwnerCanReadTheCardTest(CardRuntimeTest):
    """task/333, and it exists because @codex's review said the cure had NO
    REGRESSION TEST: all 86 renderer arms passed unchanged at the parent, so
    the clipped name and the UNDECLARED headline could regrow silently and the
    only evidence they were fixed was a browser measurement nobody re-runs.

    The owner-visible defect was arithmetic, not taste: on a 236px card the
    name was the only flex child that could shrink, so it took 100% of the
    squeeze and 32 of 33 titles clipped — five of them to 3px. The cure moves
    the name OUT of that competition into its own block. These arms bind the
    MARKUP that makes that possible; the geometry itself is CSS and is pinned
    in the stylesheet arm below."""

    def test_the_name_is_its_own_block_and_never_a_flex_sibling(self):
        LANE = "task-offer-wire-into-the-rung"
        html = self.render(r={"__row": self.row(lane=LANE)})["r"]["html"]
        # POSITIVE CONTROL FIRST, on the same markup: the row rendered at all.
        self.assertIn('class="lrrow', html)
        self.assertIn('<div class="lrtitle">' + LANE + "</div>", html)
        # THE REGRESSION ITSELF. .lrlane was a flex sibling of the state badge,
        # the dwell and the warn glyph, and it was the ONLY one that could
        # shrink. Re-emitting it puts the name back in that fight.
        self.assertNotIn('class="lrlane"', html)
        # and the name arrives WHOLE — an ellipsis in the markup would mean the
        # renderer truncated, which is a different bug from CSS clipping
        self.assertNotIn("…", html.split('class="lrl1"')[0])

    def test_UNDECLARED_stays_out_of_the_headline_and_a_real_polarity_leads(self):
        """The alarm-saturation half. UNDECLARED printed on every undecided row
        while the expand already spelled the same fact out, so it read as an
        accusation on the NORMAL state of a row awaiting review."""
        # POSITIVE CONTROL, UNCONDITIONAL, ON THE SAME OBSERVABLE: a DECLARED
        # polarity still renders its chip. Without this, "no lrpol" would also
        # be what a build that dropped the chip entirely produces.
        declared = self.render(
            r={"__row": self.row(polarity="approve")})["r"]["html"]
        self.assertIn('class="lrpol"', declared)
        # the chip prints the polarity UPPERCASED, which is also why the
        # suppression compares String(pol).toUpperCase() rather than the raw
        self.assertIn('<span class="lrpol">APPROVE</span>', declared)

        # THE STRING CASE IS THE ONE THAT DISCRIMINATES (@codex FIX). A row
        # whose polarity is None renders no chip because `pol &&` is falsy —
        # that path passes on a build with no suppression at all, so on its own
        # it proves nothing. The literal word, in BOTH cases, is what exercises
        # String(pol).toUpperCase().
        for value in ("UNDECLARED", "undeclared", "Undeclared"):
            undeclared = self.render(
                r={"__row": self.row(polarity=value)})["r"]["html"]
            headline, expand = undeclared.split('<div class="lrx"', 1)
            self.assertNotIn('class="lrpol"', headline, value)
            self.assertNotIn(value.upper(), headline, value)
            # the expand NORMALISES to upper, so the fact survives the
            # suppression in exactly one spelling whatever arrived
            self.assertIn("UNDECLARED", expand, value)
        # AND the production shape codex named: an absent polarity. Asserted
        # SEPARATELY and after the discriminating cases, so it documents the
        # null path rather than standing in for them.
        nulled = self.render(r={"__row": self.row(polarity=None)})["r"]["html"]
        self.assertNotIn('class="lrpol"', nulled.split('<div class="lrx"', 1)[0])
        # AND THE FACT IS NOT DELETED, ONLY DEMOTED — this is the whole claim.
        # Suppressing the chip would be a lie if the expand lost it too; the
        # rule is headlines-click-to-detail, not headlines-only. This assertion
        # is what separates the cure from silently dropping a field.
        self.assertIn("UNDECLARED", expand)

    def test_the_stylesheet_still_gives_the_name_its_own_line(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the five-shape hostile loop directly above the real assertion: it drives the SAME scan+comparison and requires each shape to be CAUGHT, so a broken scan or comparison fails there before the absence below can pass. The analyzer cannot credit it because the control is an assertTrue(any(...)) over a loop rather than a literal comparison
        """The markup arms above cannot see CSS, and the defect LIVED in CSS —
        one rule with flex:1 1 auto. This pins the two properties the cure
        depends on and the absence of the rule it replaced, so a stylesheet
        edit that re-creates the squeeze fails here rather than on his screen."""
        css = web_ui_loader.read_text()
        self.assertIn(".lrrow .lrtitle{", css)
        title = css.split(".lrrow .lrtitle{", 1)[1].split("}", 1)[0]
        # WRAP, never ellipsize: a lane name is the row's identity and half of
        # one identifies nothing.
        self.assertIn("overflow-wrap:anywhere", title)
        # EVERY RULE THAT CAN MATCH .lrtitle, NOT JUST THE FIRST (@codex FIX).
        # Checking one block let a LATER rule restore clipping and still pass —
        # and in CSS the later rule is the one that wins, so the arm was blind
        # in exactly the direction that matters.
        pattern = r"([^{}]*\.lrtitle[^{}]*)\{([^}]*)\}"
        # POSITIVE CONTROL ON THE SAME OBSERVABLE, unconditional and inline:
        # the scan+comparison CATCHES a clipping rule when one exists, in
        # every shape I could think of to hide one — inside @media, in a
        # multi-selector list, and with whitespace in the declaration. Without
        # this, "no ellipsis found" is also what a broken scan returns.
        for hostile in (
                "@media (max-width:680px){.lrrow .lrtitle{text-overflow:ellipsis}}",
                ".foo,.lrrow .lrtitle{text-overflow:ellipsis}",
                ".lrrow .lrtitle{text-overflow : ellipsis}",
                ".lrrow .lrtitle{\n  text-overflow:\n    ellipsis;\n}",
                ".lrrow .lrtitle{white-space : nowrap}",
                ".lrrow .lrtitle{TEXT-OVERFLOW:ELLIPSIS}"):
            hit = re.findall(pattern, hostile)
            self.assertTrue(hit, hostile)
            self.assertTrue(
                any("text-overflow:ellipsis" in "".join(b.split()).lower()
                    or "white-space:nowrap" in "".join(b.split()).lower()
                    for _s, b in hit), hostile)
        blocks = re.findall(pattern, css)
        self.assertTrue(blocks, "the scan found no .lrtitle rule at all")
        for sel, body in blocks:
            # WHITESPACE-INSENSITIVE, and this is a measured hole rather than
            # caution: `text-overflow : ellipsis` and the newline-formatted
            # spelling BOTH slipped an exact-token check, so three of six
            # shapes I tested passed a test that exists to refuse them. The
            # scan itself is fine — it finds .lrtitle rules inside @media and
            # in multi-selector lists, also measured — so the fix belongs on
            # the comparison, not the pattern.
            # CASE-INSENSITIVE TOO — CSS IS, AND MY COMPARISON WAS NOT
            # (@kimi, measured): TEXT-OVERFLOW:ELLIPSIS is valid CSS that
            # clips exactly as the lowercase spelling does, and it passed
            # all five hostile entries AND this assertion. Whitespace was
            # only half the normalisation the comparison owed.
            flat = "".join(body.split()).lower()
            self.assertNotIn("text-overflow:ellipsis", flat, sel)
            self.assertNotIn("white-space:nowrap", flat, sel)
        # the badges may wrap instead of squeezing the row on a narrow card
        self.assertIn(".lrrow .lrl1{flex-wrap:wrap}", css)
        # the rule that caused it is GONE, not merely unused: a stylesheet rule
        # nothing can match is the same stale-state defect as a dead code path
        self.assertNotIn(".lrrow .lrlane{", css)


if __name__ == "__main__":
    unittest.main()
