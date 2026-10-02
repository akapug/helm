#!/usr/bin/env python3
"""A landed SOURCE-CLEAN hold closes on the land, and only as ruled.

THE STATE (task/3053). A reviewer who reads a delta and finds nothing cannot
mint an APPROVE, because an approve binds a verified whole-suite token that
only the land gate produces. So the reviewer holds `--source-clean <tip>` and
the row moves to the integrator. The land then gates and ships the tip, and
nothing could close the row: `close --reason landed` refuses a row with no
verdict, the reviewer is not woken after the gate, and the integrator is often
the lane's author. Every source-clean land left an open row on the owner board.

THE RULING THESE ARMS PIN. `close --reason source-clean-landed` closes such a
row only when ALL THREE hold, and names every one that does not:
  1. the hold was recorded by the row's RECIPIENT, who wrote no lane round;
  2. the hold's tip is an ANCESTOR of trunk (a rebased or patch-identical
     copy refuses) and DESCENDS from the row's dispatched tip;
  3. a VERIFIED whole-suite receipt passed on a commit whose history
     contains the tip.
The close records the hold AND the token as its evidence and mints NO
approve. `helm lr foldcheck <head> --gate G` lists every held source-clean row
against that head, and `--apply` closes exactly the ones that qualify.

AND THE CLOCK (task/2695). A source-clean hold moved the debt to the
integrator and started no clock, so such a row could never be late. It is now
billed to the integrator from `hold_ts`, against a gate turnaround — never
from dispatch time and never on the reviewer's threshold — unless the hold
records no holder, which only its reviewer's re-hold can clear.

AND THE HOLD DOOR (the task/3053 read). A `--source-clean` hold binds its
holder through the corroborated identity an APPROVE binds, is refused for any
hand but the recipient's, and is refused at a tip outside the dispatched
ref's history.

AND THE CARS (the author's rulings, round 4). `helm train` takes a qualifying
hold as a car at its held tip and `lr compose` takes none, because a
cherry-picked copy could never close on ancestry; an unanswered FIX on the
held tip keeps the car out by name; a stamped hold whose holder rung refuses
is the reviewer's on every billing surface; the author refusal is typed; the
read side builds the contributor join once; and the NO HOLDER backlog is one
counted train line naming the verb that lists it.

Every refusal arm here is paired with an ADMITTING control in the same fixture
and against the same door, so no refusal can be a door that refuses
everything, and every refusal asserts the EFFECT (nothing appended, the row
still held) rather than only the absence of a complaint.
"""
import contextlib
import io
import json
import os
import time
import unittest
from unittest import mock

from helm import (autoland, dispatches, dispatches_close, dispatches_tier,
                  eventledger, foldcheck, gate, home, landreq, landreq_cli,
                  landreq_close, landwindow,
                  rowstate, seats, store)
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_landwindow as _lw
from tests import test_lr_close as _close
from tests.test_landreq import run as _lr_run


# THIS MODULE DOES NOT READ HOST LIVENESS, made here because a module fixture
# runs only for the module that declares it: every dispatch write would
# otherwise walk the host's process table for a liveness no arm asserts on.
# Through the shared helper, whose module cleanup holds the patcher: a module
# global holding it would stay re-bound after the run, and
# tests/test_hold_actor_backfill.py holds this module, so another unit could
# read it.
def setUpModule():
    from tests._tmphome import pin_live_seats
    pin_live_seats()


REASON = "source-clean-landed"
READER = "seat-reader"          # every row's recipient, unless an arm says so
OTHER = "seat-other"            # a seat that is neither recipient nor author
# THE AUTHOR'S RULING, round 4 (task/3053): `lr compose` cherry-picks, and
# `source-clean-landed` closes on ANCESTRY only, so compose names every
# source-clean candidate with this sentence and admits none.
COMPOSE_REFUSAL = ("a source-clean car rides only `helm train`, which merges "
                   "its held tip; lr compose cherry-picks and its copy could "
                   "never close source-clean-landed")


def _no_holder_line(n):
    """THE ONE LINE `helm train` folds its NO HOLDER exclusions into (the
    author's ruling 6, round 4), for `n` rows."""
    if n == 1:
        return ("  EXCLUDED — 1 held source-clean row carries NO HOLDER and "
                "cannot ride until its recipient re-holds; list it: "
                "`helm dispatch list --no-holder`")
    return ("  EXCLUDED — %d held source-clean rows carry NO HOLDER and "
            "cannot ride until each recipient re-holds; list them: "
            "`helm dispatch list --no-holder`" % n)


def _stamp(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class SourceCleanBase(_close.CloseBase):
    """Rows built through the real doors, holds stamped by a declared seat,
    and receipts minted in the real receipt grammar.

    The fixture repository (LandReqBase) has `a -> b -> c` on trunk and a
    divergent `side` tip off `a`, so `self.b` is ON trunk and `self.side` is
    not. The fixture process itself declares `integrator`, which is the
    SENDER of every row — so the integrator is a lane author here, exactly as
    it so often is on the live board."""

    READER_SESSION = "source-clean-reader-session"

    def setUp(self):
        super().setUp()
        # A real exact-session native runtime and a real owner policy are the
        # hold-time authority; an actor-name mock alone no longer proves a
        # source-clean DOOR holder. Keep legacy planted holds unproven.
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": READER}):
            seats.write_roster(
                READER, session=self.READER_SESSION,
                runtime={"family": "claude", "agent_harness": "claude",
                         "backend": "native", "model": "claude-opus-5-5"},
                presence_beat=False)
        prior = store.write_prior({
            "id": "source-clean-approval-tier",
            "statement": "Claude-family fixture readers may approve doors.",
            "confidence": 1.0, "stated_ts": "2026-07-29T00:00:00Z",
            "source": "human", "policy_kind": "approval-tier",
            "policy_members": ["family:claude"],
            "policy_reason": "owner approval for fixture reviewers"},
            root_dir=os.path.join(home.global_dir(), "premises"))
        self.assertTrue(os.path.isfile(prior))

    def dispatch(self, ref=None, **kw):
        kw.setdefault("new_work", "supersedes" not in kw)
        kw.setdefault("notify", False)
        if kw["new_work"]:
            kw.setdefault("task", self.review_task["id"])
        row = dispatches.add(READER, kw.pop("lane", "lane/foo"),
                             ref=ref or self.side, repo=self.repo, **kw)
        self.assertIsNotNone(row)
        return row

    def row(self, tip, lane, recipient=READER, supersedes=None):
        row, why = dispatches.add(
            recipient, lane, ref=tip, repo=self.repo, kind="review",
            notify=False, new_work=supersedes is None, supersedes=supersedes,
            force=True, _reason=True,
            task=self.review_task["id"] if supersedes is None else None)
        self.assertIsNone(why, why)
        return row

    def hold(self, row, tip, actor=READER,
             reason="SOURCE-CLEAN: read clean; fab Ran 5 tests in 0.1s OK"):
        """Hold `row` source-clean at `tip` THROUGH THE DOOR, as a process
        whose CORROBORATED identity is `actor` — the answer
        `dispatches._acting_author` gives, which is what the hold door binds
        and what the APPROVE a source-clean land stands in for binds too."""
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=(actor, None)), \
                mock.patch.object(home, "session_id",
                                  return_value=self.READER_SESSION):
            out, why = dispatches.mark_hold(row["id"], reason,
                                            source_clean_tip=tip)
        self.assertIsNone(why, why)
        self.assertEqual(out["status"], "held")
        if actor == READER:
            self.assertIn("hold_approval", self.state(row["id"]),
                          "the positive hold lacks frozen holder authority")
        return out

    def held(self, tip, lane, actor=READER, recipient=READER):
        row = self.row(tip, lane, recipient=recipient)
        self.hold(row, tip, actor=actor)
        return row

    def planted(self, row, tip, actor=None, ts=None,
                reason="SOURCE-CLEAN: read clean", proven=False):
        """A source-clean hold appended the way one written BEFORE the hold
        door bound its holder and its lineage reads -> the folded row.

        The door now refuses every such shape, so these arms reach the CLOSE
        door and the projection the only way the live ledger does: ~31 held
        source-clean rows carry no `hold_actor` at all. `actor` None records
        no holder; other planted rows remain unproven unless the lineage-only
        refusal explicitly requests real hold-time authority."""
        state = self.state(row["id"])
        event = {"v": 3, "event": "hold", "seq": int(state["seq"]) + 1,
                 "id": row["id"], "ts": ts or dispatches.pk.now_ts(),
                 "reason": reason, "owner_gated": False,
                 "source_clean_tip": tip}
        if actor:
            event["hold_actor"] = actor
        if proven:
            # Only the unrelated-tip arm needs a proven holder while keeping
            # its intentionally pre-lineage-door hold; other planted rows
            # remain legacy/unproven, with their old refusal intact.
            self.assertEqual(actor, READER)
            with mock.patch.object(home, "session_id",
                                   return_value=self.READER_SESSION):
                event["hold_approval"] = dispatches_tier.record_hold_approval(
                    state, actor, tip, event["ts"])
            self.assertIsNotNone(event["hold_approval"])
        self.assertTrue(eventledger.append(dispatches.ledger_path(), event))
        folded = self.state(row["id"])
        self.assertEqual(folded["status"], "held")
        self.assertEqual(folded.get("source_clean_tip"), tip)
        return folded

    def held_before(self, tip, lane, actor=None, recipient=READER):
        row = self.row(tip, lane, recipient=recipient)
        self.planted(row, tip, actor=actor)
        return row

    def receipt(self, rev, **over):
        """A receipt the REAL minting grammar would produce — the row shape
        tests/test_foldcheck.py mints, id computed by the function
        `gate.receipts()` recomputes."""
        head = self.git("rev-parse", rev)
        tree = self.git("rev-parse", "%s^{tree}" % rev)
        # ONE INSTANT PER RECEIPT. A v4 id binds the stamp, head and tree but
        # not the suite flag, so two receipts for one commit minted inside one
        # second can share an id — and then the store answers for the wrong
        # one. Each mint steps back one second from ONE origin fixed per test:
        # stepping back from a fresh clock read collides whenever the clock
        # ticks between two mints, which a fab run caught.
        self._mints = getattr(self, "_mints", 0) + 1
        self._origin = getattr(self, "_origin", None) or int(time.time())
        row = {"v": 4, "event": "gate",
               "ts": _stamp(self._origin - self._mints),
               "repo_id": "/fixture/wt/train-abc12345",
               "head": head, "tree": tree, "dirty": False,
               "head_after": head, "tree_after": tree, "dirty_after": False,
               "interpreter": {"name": "cpython", "version": "3.12.3",
                               "language": "3.12.3",
                               "executable": "/usr/bin/python3"},
               "host": {"node": "fixture-node", "system": "Linux",
                        "release": "6.8.0", "id": "ab" * 8},
               "argv": ["/usr/bin/python3", "-m", "unittest", "discover",
                        "-s", "tests", "-t", "."],
               "suite": True, "label": "train", "rc": 0, "wall": 12.5,
               "status": "OK", "ran": 100, "skipped": 1, "detail": "",
               "elapsed": 12.0, "failures": [], "failures_unreadable": False,
               "base_check": None}
        row.update(over)
        row["id"] = gate._receipt_id(row)
        return row

    def append_receipt(self, row):
        path = gate.receipts_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def mint(self, rev, **over):
        """Append a real receipt for `rev` -> gate:<id>, read back first so an
        UNKNOWN downstream is about the door and never about the fixture."""
        row = self.receipt(rev, **over)
        self.append_receipt(row)
        rows, unavailable, skipped = gate.receipts()
        self.assertIsNone(unavailable, unavailable)
        self.assertEqual(skipped, 0, "the fixture receipt failed its own "
                                     "integrity recompute")
        self.assertIn(row["id"], [str(r.get("id")) for r in rows])
        return "gate:" + row["id"]

    def history(self):
        return len(list(eventledger.events(dispatches.ledger_path())))

    def events_of(self, rid, kind):
        return [e for e in eventledger.events(dispatches.ledger_path())
                if e.get("id") == rid and e.get("event") == kind]

    def state(self, rid):
        rows, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable, unavailable)
        return rows[rid]

    def close(self, row, gate_ref, **kw):
        return landreq.close(row["id"], REASON, repo=self.repo, gate=gate_ref,
                             **kw)

    def control(self, gate_ref):
        """THE UNCONDITIONAL POSITIVE CONTROL: a row meeting all three
        conditions REHEARSES CLEAN through the same door in the same fixture,
        so no refusal below can be a door that refuses everything."""
        ok = self.held(self.b, "lane/sc-control")
        out, err = self.close(ok, gate_ref, dry_run=True)
        self.assertIsNone(err, "the control row did not qualify: %s" % err)
        self.assertTrue(out.get("would_append"), out)

    def refused(self, row, gate_ref, condition, needle):
        """Assert `row` refuses by NAME — its condition and a phrase from the
        measurement — and that the refusal changed nothing."""
        before = self.history()
        out, err = self.close(row, gate_ref)
        self.assertIsNone(out)
        self.assertIn(condition, err or "")
        self.assertIn(needle, err or "")
        self.assertEqual(self.history(), before,
                         "a refused source-clean close APPENDED an event")
        self.assertEqual(self.state(row["id"])["status"], "held")
        return err


class TheDoorClosesTest(SourceCleanBase):
    """THE ARMS THAT DRIVE A REAL CLOSE. A dry run cannot reach the
    persistence table, the replay arm or the retry identity, so these read the
    PERSISTED event back off the ledger."""

    def test_all_three_holding_CLOSES_with_the_hold_and_the_token_as_evidence(self):  # noqa: VACUOUS_ASSERTION — the ledger count is asserted to grow by exactly one and every persisted evidence field is read back by value; the empty verdict list is believed only beside that positive
        row = self.held(self.b, "lane/sc-ok")
        hold_ts = self.state(row["id"])["hold_ts"]
        token = self.mint(self.c)
        # THE STATE THIS DOOR EXISTS FOR, measured on the same row first: the
        # landed door refuses a row with no verdict, which is the whole of
        # task/3053's "nobody can close it".
        _o, landed_err = landreq.close(row["id"], "landed", repo=self.repo,
                                       live=True)
        self.assertIsNotNone(landed_err)
        before = self.history()
        out, err = self.close(row, token)
        self.assertIsNone(err, err)
        self.assertEqual(self.history(), before + 1,
                         "the close appended nothing, or more than one event")
        event = self.close_event(row["id"])
        self.assertEqual(event["close_reason"], REASON)
        self.assertEqual(event["reviewed_tip"], self.b)
        self.assertEqual(event["close_proof_mode"], "ancestor")
        # THE HOLD HALF
        self.assertEqual(event["source_clean_hold_actor"], READER)
        self.assertEqual(event["source_clean_hold_ts"], hold_ts)
        # THE GATE HALF — the receipt's own commit and tree, not the tip's
        self.assertEqual("gate:" + event["source_clean_gate"], token)
        self.assertEqual(event["source_clean_gate_head"], self.c)
        self.assertEqual(event["source_clean_gate_tree"],
                         self.git("rev-parse", "%s^{tree}" % self.c))
        self.assertEqual(event["source_clean_anchor"],
                         dispatches_close._source_clean_anchor(event))
        self.assertIn("no APPROVE minted", event["close_evidence"])
        # AND NO APPROVE WAS MINTED: no verdict event, polarity still None.
        self.assertEqual(self.events_of(row["id"], "verdict"), [])
        folded = self.state(row["id"])
        self.assertEqual(folded["status"], "closed")
        self.assertEqual(folded["close_reason"], REASON)
        self.assertIsNone(folded.get("polarity"))
        lr, lerr = landreq.get(row["id"])
        self.assertIsNone(lerr, lerr)
        self.assertTrue(lr["terminal"])
        self.assertEqual(lr["owed_by"], "nobody")
        self.assertEqual(lr["source_clean_gate"], event["source_clean_gate"])

    def test_an_identical_retry_reconciles_instead_of_refusing(self):  # noqa: VACUOUS_ASSERTION — `after_first` is read from the ledger after a real close, so the unchanged count is anchored to a counter the first write is proven to move
        row = self.held(self.b, "lane/sc-retry")
        token = self.mint(self.c)
        _out, err = self.close(row, token)
        self.assertIsNone(err, err)
        after_first = self.history()
        _again, err = self.close(row, token)
        self.assertIsNone(err, "an honest retry was refused: %s" % err)
        self.assertEqual(self.history(), after_first,
                         "the retry appended a second close event")

    def test_it_is_registered_everywhere_a_reason_must_be(self):
        self.assertIn(REASON, landreq.CLOSE_CLI_REASONS)
        self.assertIn(REASON, dispatches.CLOSE_REASONS)
        self.assertEqual(dispatches._CLOSE_POLARITY[REASON], (None,))
        self.assertIn("source_clean_gate", dispatches._CLOSE_STATE_FIELDS[REASON])
        self.assertIn("source_clean_hold_actor",
                      dispatches._CLOSE_STATE_FIELDS[REASON])
        self.assertIn(REASON, landreq.REPO_TRUNK_REASONS)
        self.assertEqual(dispatches.CLOSE_EXACT_PROOF_MODE[REASON], "ancestor")
        self.assertIn(REASON, dispatches.DISCHARGING_CLOSE)
        self.assertIn(REASON, landreq._DEBT_ABSORBING_CLOSE)
        self.assertIn("source_clean_gate_head", landreq._REF_FIELDS)
        # THE TERMINAL WORD: the row's own clean-read tip on trunk is LANDED;
        # MUST-HIT: the table does not say LANDED for everything.
        self.assertEqual(rowstate._CLOSE_TERMINAL[REASON], rowstate.LANDED)
        self.assertEqual(rowstate._CLOSE_TERMINAL["discharged"],
                         rowstate.SUPERSEDED)


class EachConditionRefusesAloneTest(SourceCleanBase):
    """EACH CONDITION FAILING ALONE REFUSES, BY NAME. Every row here meets the
    other two conditions, and the refusal must name the failing one and NOT
    the others — a refusal that named all three would prove nothing about
    which condition the door actually checked."""

    def alone(self, err, condition):
        for other in landreq_close.SOURCE_CLEAN_CONDITIONS:
            if other != condition:
                self.assertNotIn(other, err, "a condition that holds was "
                                             "named as failing: %s" % err)

    # -- condition 1: the holder -----------------------------------------
    def test_a_hold_recorded_by_a_LANE_AUTHOR_refuses(self):  # noqa: VACUOUS_ASSERTION — the claim is asserted inside `refused()` — the named condition, a phrase from the measurement, an UNCHANGED ledger count and the row still HELD — and `control()` first rehearses an admitting row through the same door in the same fixture, so a door that refuses everything fails there
        """THE RECIPIENT WROTE AN EARLIER ROUND OF THIS LANE, so it is the
        right hand for the hold and the wrong one for independence."""
        token = self.mint(self.c)
        self.control(token)
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": READER}):
            first = self.row(self.side, "lane/sc-author", recipient=OTHER)
        self.assertEqual(first["sender"], READER,
                         "the fixture did not make the recipient an author")
        second = self.row(self.b, "lane/sc-author", supersedes=first["id"])
        # PLANTED: the hold door refuses a lane author now (task/3053 round
        # 3), so this is the hold written before that door learned it — the
        # shape the close must still refuse on its own.
        self.planted(second, self.b, actor=READER)
        err = self.refused(second, token, "condition 1 (holder)",
                           "LANE AUTHOR")
        self.alone(err, "condition 1 (holder)")

    def test_a_hold_recorded_by_the_lanes_SENDER_refuses(self):  # noqa: VACUOUS_ASSERTION — the claim is asserted inside `refused()` — the named condition, a phrase from the measurement, an UNCHANGED ledger count and the row still HELD — and `control()` first rehearses an admitting row through the same door in the same fixture, so a door that refuses everything fails there
        """The integrator, who sent the row, holds it itself — the shape the
        ruling exists to refuse, because the integrator is so often the
        author. The hold door refuses it now, so it is PLANTED as a hold
        written before that door: the close must refuse it on its own."""
        token = self.mint(self.c)
        self.control(token)
        row = self.held_before(self.b, "lane/sc-sender", actor="integrator")
        err = self.refused(row, token, "condition 1 (holder)",
                           "not by the row's recipient")
        self.alone(err, "condition 1 (holder)")

    def test_a_hold_by_a_seat_that_is_not_the_recipient_refuses(self):  # noqa: VACUOUS_ASSERTION — the claim is asserted inside `refused()` — the named condition, a phrase from the measurement, an UNCHANGED ledger count and the row still HELD — and `control()` first rehearses an admitting row through the same door in the same fixture, so a door that refuses everything fails there
        token = self.mint(self.c)
        self.control(token)
        row = self.held_before(self.b, "lane/sc-stranger", actor=OTHER)
        err = self.refused(row, token, "condition 1 (holder)",
                           "not by the row's recipient")
        self.alone(err, "condition 1 (holder)")

    def test_a_hold_that_records_NO_holder_refuses_and_names_the_cure(self):  # noqa: VACUOUS_ASSERTION — the claim is asserted inside `refused()` — the named condition, a phrase from the measurement, an UNCHANGED ledger count and the row still HELD — and `control()` first rehearses an admitting row through the same door in the same fixture, so a door that refuses everything fails there
        """ABSENT IS NOT A PASS: a legacy hold's silence is never read as the
        recipient."""
        token = self.mint(self.c)
        self.control(token)
        row = self.held_before(self.b, "lane/sc-legacy", actor=None)
        self.assertNotIn("hold_actor", self.state(row["id"]))
        err = self.refused(row, token, "condition 1 (holder)", "NO HOLDER")
        self.assertIn("helm dispatch release", err)
        self.alone(err, "condition 1 (holder)")

    # -- condition 2: ancestry -------------------------------------------
    def test_a_tip_NOT_on_trunk_refuses(self):  # noqa: VACUOUS_ASSERTION — the claim is asserted inside `refused()` — the named condition, a phrase from the measurement, an UNCHANGED ledger count and the row still HELD — and `control()` first rehearses an admitting row through the same door in the same fixture, so a door that refuses everything fails there
        token = self.mint(self.c)
        self.control(token)
        side_gate = self.mint(self.side)      # a gate that DOES contain it
        row = self.held(self.side, "lane/sc-off-trunk")
        err = self.refused(row, side_gate, "condition 2 (ancestry)",
                           "is NOT on")
        self.alone(err, "condition 2 (ancestry)")

    def test_a_REBASED_or_patch_identical_copy_on_trunk_refuses(self):  # noqa: VACUOUS_ASSERTION — the claim is asserted inside `refused()` — the named condition, a phrase from the measurement, an UNCHANGED ledger count and the row still HELD — and `control()` first rehearses an admitting row through the same door in the same fixture, so a door that refuses everything fails there
        """Trunk carries the same delta under another sha; the tip the
        reviewer read clean never landed."""
        self.git("cherry-pick", self.side)
        token = self.mint(self.git("rev-parse", "HEAD"))
        self.control(token)
        side_gate = self.mint(self.side)
        row = self.held(self.side, "lane/sc-rebased")
        err = self.refused(row, side_gate, "condition 2 (ancestry)",
                           "PATCH IDENTITY")
        self.alone(err, "condition 2 (ancestry)")

    def test_a_hold_on_an_UNRELATED_trunk_commit_refuses_at_the_close_door(self):  # noqa: VACUOUS_ASSERTION — the claim is asserted inside `refused()` — the named condition, a phrase from the measurement, an UNCHANGED ledger count and the row still HELD — and `control()` first rehearses an admitting row through the same door in the same fixture, so a door that refuses everything fails there
        """THE HELD TIP IS NOT BOUND TO THE ROW'S WORK BY ANYTHING BUT THIS.
        The row was dispatched at `side`, which never landed; its hold names
        `b`, a commit already on trunk that the whole-suite gate contains. The
        hold's own tip satisfies ancestry and the gate trivially, so without
        a lineage rung the row would close LANDED on work that never landed.
        Planted as a hold made before the hold door learned this rung, which
        is the population the close must still refuse on its own."""
        token = self.mint(self.c)
        self.control(token)
        row = self.row(self.side, "lane/sc-unrelated-close")
        self.planted(row, self.b, actor=READER, proven=True)
        err = self.refused(row, token, "condition 2 (ancestry)",
                           "does not descend from the dispatched ref")
        self.assertIn(self.b[:12], err)
        self.assertIn(self.side[:12], err)
        # THE CURE IS ONE THIS ROW CAN TAKE: `retip` re-points an OPEN row
        # only, and this row is HELD, so the door names the release, the
        # retip and the re-hold, in that order.
        rid12 = row["id"][:12]
        self.assertIn("helm dispatch release %s" % rid12, err)
        self.assertIn("helm dispatch retip %s" % rid12, err)
        self.assertLess(err.index("helm dispatch release %s" % rid12),
                        err.index("helm dispatch retip %s" % rid12))
        self.assertIn("--source-clean", err[err.index("helm dispatch retip"):])
        self.alone(err, "condition 2 (ancestry)")

    # -- condition 3: the gate -------------------------------------------
    def test_a_MISSING_gate_refuses(self):  # noqa: VACUOUS_ASSERTION — the claim is asserted inside `refused()` — the named condition, a phrase from the measurement, an UNCHANGED ledger count and the row still HELD — and `control()` first rehearses an admitting row through the same door in the same fixture, so a door that refuses everything fails there
        self.control(self.mint(self.c))
        row = self.held(self.b, "lane/sc-no-gate")
        err = self.refused(row, None, "condition 3 (gate)",
                           "no gate receipt named")
        self.alone(err, "condition 3 (gate)")

    def test_a_FAILED_gate_refuses(self):  # noqa: VACUOUS_ASSERTION — the claim is asserted inside `refused()` — the named condition, a phrase from the measurement, an UNCHANGED ledger count and the row still HELD — and `control()` first rehearses an admitting row through the same door in the same fixture, so a door that refuses everything fails there
        self.control(self.mint(self.c))
        failed = self.mint(self.c, status="FAIL", rc=1)
        row = self.held(self.b, "lane/sc-red-gate")
        err = self.refused(row, failed, "condition 3 (gate)", "not OK")
        self.alone(err, "condition 3 (gate)")

    def test_an_UNVERIFIED_gate_refuses(self):  # noqa: VACUOUS_ASSERTION — the claim is asserted inside `refused()` — the named condition, a phrase from the measurement, an UNCHANGED ledger count and the row still HELD — and `control()` first rehearses an admitting row through the same door in the same fixture, so a door that refuses everything fails there
        """Two shapes: a token no store holds, and a receipt edited after it
        was minted, which `gate.receipts()` drops because its id no longer
        recomputes from its content."""
        self.control(self.mint(self.c))
        row = self.held(self.b, "lane/sc-unverified")
        self.refused(row, "gate:" + "0" * 16, "condition 3 (gate)",
                     "no VERIFIED receipt")
        tampered = self.receipt(self.c)
        tampered["ran"] = tampered["ran"] + 1          # content moved, id not
        self.append_receipt(tampered)
        err = self.refused(row, "gate:" + tampered["id"],
                           "condition 3 (gate)", "no VERIFIED receipt")
        self.alone(err, "condition 3 (gate)")

    def test_a_gate_whose_commit_does_NOT_CONTAIN_the_tip_refuses(self):  # noqa: VACUOUS_ASSERTION — the claim is asserted inside `refused()` — the named condition, a phrase from the measurement, an UNCHANGED ledger count and the row still HELD — and `control()` first rehearses an admitting row through the same door in the same fixture, so a door that refuses everything fails there
        self.control(self.mint(self.c))
        before_tip = self.mint(self.a)                 # a precedes b
        row = self.held(self.b, "lane/sc-not-containing")
        err = self.refused(row, before_tip, "condition 3 (gate)",
                           "NOT in the history")
        self.alone(err, "condition 3 (gate)")

    def test_a_FOCUSED_not_whole_suite_gate_refuses(self):  # noqa: VACUOUS_ASSERTION — the claim is asserted inside `refused()` — the named condition, a phrase from the measurement, an UNCHANGED ledger count and the row still HELD — and `control()` first rehearses an admitting row through the same door in the same fixture, so a door that refuses everything fails there
        self.control(self.mint(self.c))
        focused = self.mint(self.c, suite=False)
        row = self.held(self.b, "lane/sc-focused")
        err = self.refused(row, focused, "condition 3 (gate)",
                           "not a whole-suite run")
        self.alone(err, "condition 3 (gate)")

    def test_every_failing_condition_is_named_not_only_the_first(self):  # noqa: VACUOUS_ASSERTION — the three assertIn checks on the refusal are unconditional positives on the same observable the unchanged count describes
        """One line per row in the foldcheck listing has to carry the whole
        answer, or the operator discovers the second failure a round later."""
        row = self.held_before(self.side, "lane/sc-all-three", actor=OTHER)
        before = self.history()
        _out, err = self.close(row, None)
        for condition in landreq_close.SOURCE_CLEAN_CONDITIONS:
            self.assertIn(condition, err or "")
        self.assertEqual(self.history(), before)

    def test_a_verdict_row_is_refused_and_told_it_carries_no_hold(self):
        token = self.mint(self.c)
        self.control(token)
        row = self.verdict_row(polarity="approve", ref=self.b,
                               lane="lane/sc-verdict", recipient=READER)
        _out, err = self.close(row, token)
        self.assertIn("carries no source-clean hold", err or "")


class TheReplayTest(SourceCleanBase):
    """THE LEDGER HALF: replay accepts the writer's event, and the reducer
    refuses a forged one missing either piece of evidence."""

    def forged_for(self, real, row):
        """The real writer's event re-aimed at another qualifying `row`, with
        that row's hold and a re-sealed anchor — the best forgery there is."""
        state = self.state(row["id"])
        event = dict(real, id=row["id"], seq=int(state["seq"]) + 1,
                     ts=dispatches.pk.now_ts(), reviewed_tip=self.b,
                     source_clean_hold_actor=state["hold_actor"],
                     source_clean_hold_ts=state["hold_ts"])
        event.pop("close_actor", None)
        event["source_clean_anchor"] = dispatches_close._source_clean_anchor(event)
        return event

    def test_replay_ACCEPTS_the_real_close(self):  # noqa: VACUOUS_ASSERTION — every assertion is a positive equality on the re-folded row; the assertIsNone on err is a precondition
        row = self.held(self.b, "lane/sc-replay")
        _out, err = self.close(row, self.mint(self.c))
        self.assertIsNone(err, err)
        # A FRESH SNAPSHOT RE-FOLDS THE LEDGER FROM ITS BYTES, so this is the
        # replay arm judging the event the writer appended.
        folded = self.state(row["id"])
        self.assertEqual(folded["status"], "closed")
        self.assertEqual(folded["close_reason"], REASON)
        self.assertEqual(folded["source_clean_hold_actor"], READER)

    def test_the_reducer_REFUSES_a_forgery_missing_either_piece(self):  # noqa: VACUOUS_ASSERTION — the complete event is asserted to BIND through the same validator and then to CLOSE the row at the fold, which is the positive control for every refusal before it
        token = self.mint(self.c)
        done = self.held(self.b, "lane/sc-template")
        _out, err = self.close(done, token)
        self.assertIsNone(err, err)
        real = self.close_event(done["id"])
        victim = self.held(self.b, "lane/sc-victim")
        complete = self.forged_for(real, victim)
        rows, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable, unavailable)
        state = rows[victim["id"]]
        # POSITIVE CONTROL: the complete event binds through the same
        # validator, so each refusal below is the missing piece and nothing
        # else.
        self.assertIsNone(dispatches._close_event_error(
            dict(complete), state, current=rows))
        no_hold = dict(complete)
        no_hold.pop("source_clean_hold_actor")
        no_gate = dict(complete)
        no_gate.pop("source_clean_gate")
        for name, forged in (("hold", no_hold), ("gate", no_gate)):
            with self.subTest(missing=name):
                why = dispatches._close_event_error(forged, state, current=rows)
                self.assertIsNotNone(why)
                self.assertIn("missing=", why)
                # AND AT THE FOLD: appended, it leaves the row HELD.
                self.assertTrue(eventledger.append(dispatches.ledger_path(),
                                                   forged))
                self.assertEqual(self.state(victim["id"])["status"], "held")
        # A SWAPPED HALF, re-sealed nowhere: the old anchor no longer binds.
        swapped = dict(complete, source_clean_gate="f" * 16)
        self.assertIn("anchor does not bind",
                      dispatches._close_event_error(swapped, state,
                                                    current=rows) or "")
        # AND THE CONTROL AT THE FOLD: the complete event closes the row.
        self.assertTrue(eventledger.append(dispatches.ledger_path(), complete))
        self.assertEqual(self.state(victim["id"])["status"], "closed")

    def test_the_reducer_refuses_a_close_whose_holder_is_a_lane_author(self):  # noqa: VACUOUS_ASSERTION — the assertIn on the refusal text is an unconditional positive; the template close before it is proven real by its own assertIsNone and close_event read
        """Replay re-derives condition 1 from the ledger it is folding, so a
        hand-written event cannot close an author-held row even with every
        field present and the anchor re-sealed."""
        token = self.mint(self.c)
        done = self.held(self.b, "lane/sc-template-2")
        _out, err = self.close(done, token)
        self.assertIsNone(err, err)
        real = self.close_event(done["id"])
        # PLANTED: the hold door refuses an author's source-clean hold.
        authored = self.held_before(self.b, "lane/sc-authored",
                                    actor="integrator", recipient="integrator")
        forged = self.forged_for(real, authored)
        rows, _u = dispatches.snapshot()
        why = dispatches._close_event_error(forged, rows[authored["id"]],
                                            current=rows)
        self.assertIn("LANE AUTHOR", why or "")


class TheReplayJoinIsKeyedTest(SourceCleanBase):
    """NIT (d) of the task/3053 read: a source-clean close's replay runs the
    chain-contributor join INSIDE the checkpointed fold, so every
    environment read that join takes must reach the fold's recorder — or a
    checkpoint caches an answer another checkout would not give."""

    def test_the_join_records_its_realpaths_and_its_home_read(self):
        from helm import foldckpt
        home = os.path.join(self.tmp, "home-repo.git")
        bound = os.path.join(self.tmp, "bound-repo.git")
        rows = {"r1": {"id": "r1", "repo_id": bound, "sender": "seat-x"},
                "r2": {"id": "r2", "sender": "seat-y"}}     # a legacy row
        with mock.patch.object(dispatches, "home_repo_id",
                               return_value=(home, None)), \
                foldckpt.recording() as rec:
            chains, broken = landreq._contributor_chains(rows, {})
        self.assertEqual(broken, set())
        real_bound, real_home = os.path.realpath(bound), os.path.realpath(home)
        self.assertIn((real_bound, "r1"), chains)     # the join did run
        self.assertIn((real_home, "r2"), chains)
        self.assertEqual(rec.realpaths.get(bound), real_bound)
        self.assertEqual(rec.realpaths.get(home), real_home)
        self.assertEqual(rec.homes, [home],
                         "the legacy row's home read never reached the key")


class TheHoldStampsItsHolderTest(SourceCleanBase):
    """Condition 1 needs a hand the ledger records. The hold stamps it."""

    def test_the_hold_event_records_the_declared_seat_and_release_clears_it(self):  # noqa: VACUOUS_ASSERTION — the stamp is asserted PRESENT by value on the event and the row before the release is asserted to remove it
        row = self.held(self.b, "lane/sc-stamp")
        self.assertEqual(self.events_of(row["id"], "hold")[-1]["hold_actor"],
                         READER)
        self.assertEqual(self.state(row["id"])["hold_actor"], READER)
        out, why = dispatches.mark_release(row["id"])
        self.assertIsNone(why, why)
        self.assertNotIn("hold_actor", out)
        self.assertNotIn("hold_actor", self.state(row["id"]))

    def test_an_ORDINARY_hold_under_an_uncorroborated_identity_holds_and_stamps_nobody(self):  # noqa: VACUOUS_ASSERTION — the held status is asserted positively, and the corroborated `seated` hold in the same fixture is the unconditional control: its event's hold_actor is asserted EQUAL to the seat
        """ORDINARY HOLDS KEEP THEIR BEHAVIOUR and stamp only a corroborated
        hand: a process whose identity the caller-identity law refuses still
        holds its row, and the ledger records no holder rather than the name
        it merely declared. The SAME refusal stops a source-clean hold."""
        refusal = ("refusing to hold this row under a DISPUTED identity: "
                   "this process declares 'x' but session 00000000 is "
                   "rostered to 'y'")
        row = self.row(self.b, "lane/sc-unseated")
        clean = self.row(self.b, "lane/sc-unseated-clean")
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=(None, refusal)):
            out, why = dispatches.mark_hold(row["id"], "waiting on a build box")
            refused, cwhy = dispatches.mark_hold(
                clean["id"], "SOURCE-CLEAN: read clean",
                source_clean_tip=self.b)
        self.assertIsNone(why, why)
        self.assertEqual(out["status"], "held")
        self.assertNotIn("hold_actor", self.events_of(row["id"], "hold")[-1])
        self.assertNotIn("hold_actor", self.state(row["id"]))
        self.assertIsNone(refused)
        self.assertIn("DISPUTED identity", cwhy or "")
        self.assertEqual(self.events_of(clean["id"], "hold"), [])
        # MUST-HIT on the same observable: a corroborated seat IS stamped.
        seated = self.held(self.b, "lane/sc-seated")
        self.assertEqual(self.events_of(seated["id"], "hold")[-1]["hold_actor"],
                         READER)

    def test_the_hold_actor_is_the_hand_the_activity_registry_reads(self):
        self.assertEqual(dispatches.LEDGER_EVENT_ACTORS["hold"], ("hold_actor",))


class TheHoldDoorBindsItsRecipientTest(SourceCleanBase):
    """FIX (a) of the task/3053 read: the hold stamp is the authority the land
    closes on, so it is resolved as the APPROVE it stands in for resolves its
    recorder — through `dispatches._acting_author`, which refuses a declared
    name the roster disputes — and a source-clean hold is refused at the door
    unless that corroborated hand is the row's recipient."""

    SID = "aaaabbbb-1111-4222-8333-444455556666"

    def test_a_declared_name_the_roster_disputes_cannot_hold_source_clean(self):  # noqa: VACUOUS_ASSERTION — the refusal text is a positive; the same row, env and door then HOLD under an agreeing roster and the stamp is asserted EQUAL to the seat, so the unchanged count is a discrimination
        """AN INHERITED HELM_CHAT_NAME, at the hold door. The author's
        pane carries the reader's HELM_CHAT_NAME while the roster binds its
        session to the author. An approve from that pane is refused as a
        DISPUTED identity; a source-clean hold from it would have been stamped
        with the reader's name and then closed the row on the author's own
        read. It is refused, and nothing is written."""
        from helm import seats
        row = self.row(self.b, "lane/sc-disputed")
        before = self.history()
        env = {"HELM_CHAT_NAME": READER, "CLAUDE_CODE_SESSION_ID": self.SID}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(seats, "seat_for_session",
                                  return_value="integrator"):
            out, why = dispatches.mark_hold(row["id"], "SOURCE-CLEAN: read "
                                            "clean", source_clean_tip=self.b)
        self.assertIsNone(out)
        self.assertIn("DISPUTED identity", why or "")
        self.assertEqual(self.history(), before,
                         "a disputed identity's source-clean hold APPENDED")
        self.assertEqual(self.state(row["id"])["status"], "open")
        # CONTROL, SAME ENV AND ROW: the roster agreeing with the declared
        # name holds it and stamps that name, so the refusal above is the
        # dispute's doing and not a door that refuses every hold.
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(seats, "seat_for_session",
                                  return_value=READER):
            out, why = dispatches.mark_hold(row["id"], "SOURCE-CLEAN: read "
                                            "clean; fab Ran 5 tests OK",
                                            source_clean_tip=self.b)
        self.assertIsNone(why, why)
        self.assertEqual(self.state(row["id"])["hold_actor"], READER)

    def test_a_source_clean_hold_by_anyone_but_the_recipient_is_refused_at_the_door(self):  # noqa: VACUOUS_ASSERTION — each subTest asserts the refusal names the recipient; after the loop the recipient holds the SAME row through the SAME door unconditionally and its stamp is asserted by value
        """A stranger, and the lane's own sender: neither is the reader the
        row was sent to, and the refusal NAMES that reader."""
        row = self.row(self.b, "lane/sc-door-stranger")
        before = self.history()
        for actor in (OTHER, "integrator"):
            with self.subTest(actor=actor):
                with mock.patch.object(dispatches, "_acting_author",
                                       return_value=(actor, None)):
                    out, why = dispatches.mark_hold(
                        row["id"], "SOURCE-CLEAN: read clean",
                        source_clean_tip=self.b)
                self.assertIsNone(out)
                self.assertIn("@" + READER, why or "")
                self.assertIn("recipient", why or "")
        self.assertEqual(self.history(), before)
        self.assertEqual(self.state(row["id"])["status"], "open")
        # CONTROL: the recipient holds the same row through the same door.
        self.hold(row, self.b)
        self.assertEqual(self.state(row["id"])["hold_actor"], READER)


class TheHeldTipDescendsFromTheDispatchedRefTest(SourceCleanBase):
    """FIX (b) of the task/3053 read: a hold at any commit that resolves in
    the repository satisfied the land's ancestry and gate conditions on its
    own tip, so a hold on an unrelated trunk commit closed a row whose work
    never landed. The held tip must DESCEND from the row's dispatched tip."""

    def test_a_hold_on_an_UNRELATED_trunk_commit_refuses_at_the_hold_door(self):  # noqa: VACUOUS_ASSERTION — the refusal names both shas positively; the same row then holds at a descendant cure tip through the same door and the stored tip is asserted EQUAL to it
        row = self.row(self.side, "lane/sc-unrelated-door")
        before = self.history()
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=(READER, None)):
            out, why = dispatches.mark_hold(row["id"], "SOURCE-CLEAN: read "
                                            "clean", source_clean_tip=self.b)
        self.assertIsNone(out)
        self.assertIn("does not descend from the dispatched ref", why or "")
        self.assertIn(self.b[:12], why)
        self.assertIn(self.side[:12], why)
        # AT THE HOLD DOOR THE ROW IS OPEN, so `retip` alone is the cure and
        # the sentence stays as it was: no release is owed.
        self.assertIn("helm dispatch retip %s" % row["id"][:12], why)
        self.assertNotIn("helm dispatch release", why)
        self.assertEqual(self.history(), before)
        self.assertEqual(self.state(row["id"])["status"], "open")
        # CONTROL: a CURE ROUND moves the tip past the dispatched ref, which
        # is the ordinary case, and that descendant is admitted.
        self.git("checkout", "-q", "side")
        cure = self.commit("cure", path="cure")
        self.git("checkout", "-q", self.main)
        self.hold(row, cure)
        self.assertEqual(self.state(row["id"])["source_clean_tip"], cure)


class ALegacyUnstampedHoldIsTheReviewersTest(SourceCleanBase):
    """FIX (f) of the task/3053 read: every live source-clean hold predates
    the stamp, so none records a holder, and the only move that clears one is
    its RECIPIENT's release and re-hold. Billing the integrator for it, and
    telling the integrator to run a close that refuses, is the wrong party
    and the wrong door."""

    def test_an_unstamped_hold_on_trunk_is_owed_by_the_REVIEWER_and_names_the_rehold(self):  # noqa: VACUOUS_ASSERTION — the stamped row in the same fixture is the unconditional control: owed by the integrator and its mark names the close, so each absence on the legacy line is a discrimination
        aged = _stamp(time.time() - landreq.SOURCE_CLEAN_GATE_S - 120)
        legacy = self.row(self.b, "lane/sc-legacy-bill")
        self.planted(legacy, self.b, actor=None, ts=aged)
        stamped = self.row(self.b, "lane/sc-stamped-bill")
        self.planted(stamped, self.b, actor=READER, ts=aged)
        lr_legacy, err = landreq.get(legacy["id"])
        self.assertIsNone(err, err)
        lr_stamped, err = landreq.get(stamped["id"])
        self.assertIsNone(err, err)
        # THE STATE THE READ NAMED: on trunk, late, unstamped.
        self.assertIs(lr_legacy["trunk_contains_tip"], True)
        self.assertTrue(lr_legacy["stalled"],
                        "an unstamped hold's debt stopped being billed")
        self.assertEqual(lr_legacy["owed_by"], "reviewer")
        self.assertTrue(landreq._owed_by_whom(lr_legacy).startswith("reviewer"),
                        landreq._owed_by_whom(lr_legacy))
        line = landreq._line(lr_legacy)
        self.assertIn("helm dispatch release %s" % legacy["id"][:12], line)
        self.assertIn("--source-clean %s" % self.b[:12], line)
        self.assertNotIn("source-clean-landed", line)
        self.assertNotIn("foldcheck", line)
        # CONTROL: a stamped hold in the same state is the integrator's, and
        # its mark names the close it owes.
        self.assertTrue(lr_stamped["stalled"])
        self.assertEqual(lr_stamped["owed_by"], "integrator")
        self.assertIn("source-clean-landed", landreq._line(lr_stamped))
        # THE DISPATCH STATE WORD answers the same question and must agree:
        # an unstamped hold is not handed to the integrator there either.
        word = dispatches._base_label(self.state(legacy["id"]))
        self.assertIn("NO HOLDER", word)
        self.assertIn("REVIEWER", word)
        self.assertNotIn("INTEGRATOR", word)
        self.assertIn("ON THE INTEGRATOR",
                      dispatches._base_label(self.state(stamped["id"])))

    def test_the_hold_nudge_names_the_rehold_for_an_unstamped_hold(self):
        """The nudge a hold sends reaches an unstamped row only through an
        idempotent re-run of a legacy hold — and there it must name the
        re-hold to the recipient, never a close that will refuse. NIT (c)
        rides the control: the stamped nudge says what the gate must be."""
        from helm import dispatches_cli
        calls = []
        legacy = self.row(self.b, "lane/sc-legacy-nudge")
        legacy_state = self.planted(legacy, self.b, actor=None)
        stamped = self.held(self.b, "lane/sc-stamped-nudge")
        with mock.patch.object(dispatches, "_nudge",
                               lambda to, body, ctx: calls.append(
                                   (to, body, ctx))):
            dispatches_cli._hold_holder_nudge(legacy_state)
            dispatches_cli._hold_holder_nudge(self.state(stamped["id"]))
        self.assertEqual(len(calls), 2, calls)
        to, body, _ctx = calls[0]
        self.assertEqual(to, READER, "the re-hold is the recipient's to make")
        self.assertIn("helm dispatch release %s" % legacy["id"][:12], body)
        self.assertIn("--source-clean %s" % self.b[:12], body)
        self.assertNotIn("foldcheck", body)
        self.assertNotIn("source-clean-landed", body)
        # CONTROL: the stamped hold's nudge names the close, and states the
        # gate as what the door checks — not as the land's own receipt.
        self.assertIn("foldcheck", calls[1][1])
        self.assertIn("a verified whole-suite receipt on a commit containing "
                      "the tip", calls[1][1])


class FoldcheckListsThenClosesTest(SourceCleanBase):
    """`helm lr foldcheck <head> --gate G` lists every held source-clean row
    against a COMPOSED head, and `--apply` closes EXACTLY the qualifying ones
    — before the land and again after it."""

    def foldcheck(self, head, token, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = landreq.cmd_lr(["foldcheck", head, "--gate", token, "--repo",
                                 self.repo, "--no-fetch", *extra])
        return rc, out.getvalue(), err.getvalue()

    def foldstage(self, head, token, cars):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = landreq_cli._print_source_clean_landings(
                self.repo, head, token, apply=True, train_car_ids=set(cars))
        return rc, out.getvalue()

    def line(self, text, rid):
        found = [l for l in text.splitlines() if rid[:12] in l]
        self.assertTrue(found, "row %s is not in the listing:\n%s"
                        % (rid[:12], text))
        return found[0]

    def test_it_lists_then_closes_exactly_the_qualifying_rows(self):  # noqa: VACUOUS_ASSERTION — each apply is asserted to grow the ledger by exactly one and to report CLOSED for the named row; the unchanged counts and HELD rows are measured beside those positives
        ok = self.held(self.b, "lane/fc-ok")
        # Both refused at the hold door now, so both are the holds written
        # before it — the rows a sweep over the live ledger actually meets.
        by_author = self.held_before(self.b, "lane/fc-author",
                                     actor="integrator")
        legacy = self.held_before(self.b, "lane/fc-legacy", actor=None)
        on_side = self.held(self.side, "lane/fc-side")
        self.git("checkout", "-q", "-b", "elsewhere", self.a)
        other_tip = self.commit("elsewhere", path="elsewhere")
        self.git("checkout", "-q", self.main)
        elsewhere = self.held(other_tip, "lane/fc-elsewhere")
        # THE COMPOSED HEAD: trunk plus the side lane, gated as one tree.
        self.git("checkout", "-q", "-b", "train", self.main)
        self.git("merge", "--no-ff", "--no-edit", "-q", "side")
        head = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        token = self.mint(head)

        before = self.history()
        _rc, text, _err = self.foldcheck(head, token)
        self.assertEqual(self.history(), before, "the DRY listing appended")
        self.assertIn("QUALIFIES", self.line(text, ok["id"]))
        self.assertIn("condition 1 (holder)", self.line(text, by_author["id"]))
        self.assertIn("NO HOLDER", self.line(text, legacy["id"]))
        # in the head, not yet on trunk: the land has not happened
        side_line = self.line(text, on_side["id"])
        self.assertIn("REFUSED", side_line)
        self.assertIn("condition 2 (ancestry)", side_line)
        self.assertIn("NOT-IN-HEAD", self.line(text, elsewhere["id"]))

        rc, text, _err = self.foldcheck(head, token, "--apply")
        self.assertIn("CLOSED", self.line(text, ok["id"]))
        self.assertEqual(self.history(), before + 1,
                         "--apply closed something other than the one row "
                         "that qualified")
        self.assertEqual(self.state(ok["id"])["close_reason"], REASON)
        for row in (by_author, legacy, on_side, elsewhere):
            self.assertEqual(self.state(row["id"])["status"], "held")

        # THE LAND: trunk fast-forwards to the gated head, and the row whose
        # tip only the head carried now qualifies — and is the only new close.
        self.git("merge", "--ff-only", "-q", "train")
        rc, text, _err = self.foldcheck(head, token, "--apply")
        self.assertIn("CLOSED", self.line(text, on_side["id"]))
        self.assertNotIn(ok["id"][:12], text,
                         "a row already closed was listed again")
        self.assertEqual(self.history(), before + 2)
        for row in (by_author, legacy, elsewhere):
            self.assertEqual(self.state(row["id"])["status"], "held")

    def test_train_scope_reports_foreign_refusal_but_stops_on_own_failure(self):
        own = self.held(self.b, "lane/fc-own")
        foreign = self.held_before(self.b, "lane/fc-foreign", actor=None)
        token = self.mint(self.b)
        rc, text = self.foldstage(self.b, token, [own["id"]])
        self.assertEqual(rc, 0, text)
        self.assertIn("CLOSED", self.line(text, own["id"]))
        self.assertIn("REPORTED", self.line(text, foreign["id"]))
        self.assertIn("REFUSED:", self.line(text, foreign["id"]))
        self.assertEqual(self.state(foreign["id"])["status"], "held")
        rc, text = self.foldstage(self.b, token, [foreign["id"]])
        self.assertEqual(rc, 0, text)
        self.assertIn("REFUSED", self.line(text, foreign["id"]))
        self.assertNotIn("REPORTED", self.line(text, foreign["id"]))

    def test_own_hold_outside_the_head_stops_but_foreign_hold_does_not(self):
        own = self.held(self.side, "lane/fc-outside-head")
        token = self.mint(self.b)
        rc, text = self.foldstage(self.b, token, [])
        self.assertEqual(rc, 0, text)
        self.assertIn("NOT-IN-HEAD", self.line(text, own["id"]))
        rc, text = self.foldstage(self.b, token, [own["id"]])
        self.assertEqual(rc, 1, text)
        self.assertIn("REFUSED", self.line(text, own["id"]))
        self.assertIn("NOT-IN-HEAD", self.line(text, own["id"]))
        self.assertEqual(self.state(own["id"])["status"], "held")

    def test_a_prior_source_clean_close_is_proven_by_its_exact_row_and_tip(self):
        own = self.held(self.b, "lane/fc-prior-close")
        token = self.mint(self.b)
        rc, text = self.foldstage(self.b, token, [own["id"]])
        self.assertEqual(rc, 0, text)
        self.assertIn("CLOSED", self.line(text, own["id"]))
        ops = autoland.Ops()
        self.assertTrue(ops.source_clean_closed(
            self.repo, own["id"], self.b, self.b))
        self.assertFalse(ops.source_clean_closed(
            self.repo, own["id"], self.b, self.side))
        self.assertFalse(ops.source_clean_closed(
            self.repo, own["id"], self.side, self.b))

    def test_foldcheck_cli_passes_exact_car_ids_to_the_sweep(self):
        own = self.held(self.b, "lane/fc-cli-own")
        foreign = self.held_before(self.b, "lane/fc-cli-foreign", actor=None)
        token = self.mint(self.b)
        rungs = [foldcheck.Rung(name, foldcheck.PASS, "proved") for name in
                 ("tip-exists", "tree-vs-gate", "ff-able", "head-clean",
                  "origin-has-it")]
        with mock.patch.object(foldcheck, "check", return_value=rungs), \
                mock.patch.object(landreq_cli, "_fold_proven", return_value=0), \
                mock.patch.object(landreq_cli, "_print_landed_leases"), \
                mock.patch("helm.postland.take"):
            rc, text, err = self.foldcheck(
                self.b, token, "--apply", "--train-cars", own["id"])
        self.assertEqual(rc, 0, (text, err))
        self.assertIn("CLOSED", self.line(text, own["id"]))
        self.assertIn("REPORTED", self.line(text, foreign["id"]))
        self.assertEqual(self.state(own["id"])["status"], "closed")
        self.assertEqual(self.state(foreign["id"])["status"], "held")

    def test_a_foreign_failed_write_does_not_mask_an_own_failed_write(self):
        own, foreign = "a" * 32, "b" * 32
        entries = [{"id": rid, "tip": self.b, "holder": "reader-seat",
                    "verdict": "FAILED", "why": "write refused"}
                   for rid in (own, foreign)]
        with mock.patch.object(landreq, "source_clean_landings",
                               return_value=(entries, None)):
            rc, text = self.foldstage(
                self.b, "gate:" + "c" * 16, ["c" * 32])
            self.assertEqual(rc, 0, text)
            self.assertIn("REPORTED", self.line(text, own))
            self.assertIn("REPORTED", self.line(text, foreign))
            rc, text = self.foldstage(
                self.b, "gate:" + "c" * 16, [own])
            self.assertEqual(rc, 1, text)
            self.assertIn("FAILED", self.line(text, own))
            self.assertIn("REPORTED", self.line(text, foreign))
            rc, text = self.foldstage(
                self.b, "gate:" + "c" * 16, [foreign])
            self.assertEqual(rc, 1, text)
            self.assertIn("REPORTED", self.line(text, own))
            self.assertIn("FAILED", self.line(text, foreign))

    def test_train_car_option_refuses_abbreviated_and_duplicate_ids(self):
        row = self.held(self.b, "lane/fc-option")
        before = self.history()
        for ids in (row["id"][:12], row["id"] + "," + row["id"], ""):
            rc, text, err = self.foldcheck(
                self.b, "gate:" + "c" * 16, "--apply", "--train-cars", ids)
            self.assertEqual(rc, 2, (text, err))
            self.assertIn("distinct full dispatch row IDs", err)
        self.assertEqual(self.history(), before)
        self.assertEqual(self.state(row["id"])["status"], "held")

    def test_the_cli_close_verb_requires_its_gate_and_keeps_it_to_itself(self):
        row = self.held(self.b, "lane/cli-gate")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = landreq.cmd_lr(["close", row["id"], "--reason", REASON])
        self.assertEqual(rc, 2)
        self.assertIn("requires --gate", err.getvalue())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = landreq.cmd_lr(["close", row["id"], "--reason", "withdrawn",
                                 "--evidence", "x", "--gate", "gate:" + "a" * 16])
        self.assertEqual(rc, 2)
        self.assertIn("--gate belongs to --reason " + REASON, err.getvalue())


class TheIntegratorsClockTest(SourceCleanBase):
    """task/2695: a source-clean hold is billed to the integrator from
    `hold_ts`, on a gate turnaround — not from dispatch, not on the
    reviewer's threshold."""

    def held_at(self, lane, held_ago, dispatched_ago=None, tip=None):
        """A row dispatched `dispatched_ago` seconds ago (default: now) and
        held source-clean at `tip` (default: `self.b`, already on trunk)
        `held_ago` seconds ago, written as the ledger would hold them."""
        now = time.time()
        tip = tip or self.b
        row = self.row(tip, lane)
        if dispatched_ago is not None:
            path = dispatches.ledger_path()
            events = eventledger.events(path)
            with open(path, "w", encoding="utf-8") as fh:
                for event in events:
                    if event.get("id") == row["id"] \
                            and event.get("event") == "dispatch":
                        event["ts"] = _stamp(now - dispatched_ago)
                    fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        state = self.state(row["id"])
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "hold", "seq": int(state["seq"]) + 1,
            "id": row["id"], "ts": _stamp(now - held_ago),
            "reason": "SOURCE-CLEAN: read clean", "owner_gated": False,
            "source_clean_tip": tip, "hold_actor": READER}))
        self.assertEqual(self.state(row["id"])["status"], "held")
        return row

    def lr(self, row):
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        return lr

    def test_a_hold_older_than_the_gate_turnaround_is_LATE_on_the_integrator(self):  # noqa: VACUOUS_ASSERTION — every assertion inside the loop is a positive (True, equality, >=) and the loop runs over a literal two-tuple
        """BOTH SIDES OF THE LAND. Before it (the tip is off trunk) the
        integrator owes the gate; after it (the tip is on trunk) it owes the
        close — the row task/3053 found sitting open — and a contained row's
        stall is NOT cleared the way an unreviewed contained row's is."""
        for lane, tip in (("lane/clock-late-off-trunk", self.side),
                          ("lane/clock-late-on-trunk", self.b)):
            with self.subTest(tip=lane):
                late = self.lr(self.held_at(
                    lane, landreq.SOURCE_CLEAN_GATE_S + 120, tip=tip))
                self.assertTrue(late["stalled"], "a source-clean hold older "
                                "than one gate turnaround is never late")
                self.assertEqual(late["owed_by"], "integrator")
                self.assertEqual(late["stall_threshold_s"],
                                 landreq.SOURCE_CLEAN_GATE_S)
                self.assertGreaterEqual(late["hold_age_s"],
                                        landreq.SOURCE_CLEAN_GATE_S)

    def test_lr_stalls_bills_a_hold_on_trunk_to_its_holder_never_to_nobody(self):
        """THE SAME QUESTION ON THE STALL LISTING (task/3053, the author's
        ruling on the seam). `lr stalls` replaces the owed-by clause with
        "ALREADY ON TRUNK ... owed by NOBODY" for a row whose tip trunk holds,
        which is right for ledger debris and wrong for a source-clean hold:
        the land did not settle it and the integrator owes its close. The
        line keeps its holder and prints the one sentence naming the move."""
        row = self.held_at("lane/clock-stalls-on-trunk",
                           landreq.SOURCE_CLEAN_GATE_S + 120, tip=self.b)
        self.assertIs(self.lr(row)["trunk_contains_tip"], True)
        rc, out, err = _lr_run(["stalls"])
        self.assertEqual(rc, 0, err)
        # the STALLED listing's line, which carries the stall clause; the
        # same row also prints under NOT stall-checked, without one
        line = [ln for ln in out.splitlines()
                if row["id"][:12] in ln and "(>= " in ln]
        self.assertEqual(len(line), 1, out + err)
        self.assertIn("owed by integrator", line[0])
        self.assertIn("on main · SOURCE-CLEAN close owed by the integrator",
                      line[0])
        self.assertNotIn("owed by NOBODY", line[0])

    def test_the_clock_runs_from_the_HOLD_and_not_from_dispatch(self):  # noqa: VACUOUS_ASSERTION — the late control in the same fixture is asserted stalled, so the fresh row's False is a discrimination
        """Dispatched ten hours ago, held a minute ago: one minute is owed."""
        fresh = self.lr(self.held_at("lane/clock-fresh", 60,
                                     dispatched_ago=10 * 3600))
        self.assertFalse(fresh["stalled"])
        self.assertLess(fresh["hold_age_s"], landreq.SOURCE_CLEAN_GATE_S)
        # MUST-HIT, same fixture: the late control really is late.
        late = self.lr(self.held_at("lane/clock-control",
                                    landreq.SOURCE_CLEAN_GATE_S + 120))
        self.assertTrue(late["stalled"])

    def test_stalebot_explains_the_stall_by_the_HOLD_clock(self):
        """NIT (e): the sweep that chases the owed seat printed the stage
        dwell and the stage name for a row whose `stalled` was tripped by the
        HOLD's age — a different clock, and a stage the review had left."""
        from helm import stalebot
        late = self.lr(self.held_at("lane/clock-stalebot",
                                    landreq.SOURCE_CLEAN_GATE_S + 120))
        self.assertTrue(late["stalled"])
        # CONTROL: an ordinary stalled loop keeps its stage explanation.
        plain = {"id": "b2" * 8, "state": "REVIEW", "dwell_s": 7200,
                 "owed_by": "reviewer", "reviewer": OTHER}
        with mock.patch.object(stalebot.landreq, "stalls",
                               return_value=([late, plain], None)), \
                mock.patch.object(stalebot.dispatches, "snapshot",
                                  return_value=({}, None)), \
                mock.patch.object(stalebot.dispatches, "owed", return_value=[]), \
                mock.patch.object(stalebot.tasks, "open_rows", return_value=[]):
            items, _unavailable, _nd = stalebot.collect(time.time())
        byid = {item["id"]: item for item in items}
        item = byid[late["id"]]
        self.assertIn("since the source-clean hold", item["why_aged"])
        self.assertNotIn("stage threshold", item["why_aged"])
        self.assertEqual(item["age_s"], late["hold_age_s"])
        self.assertIn("stage threshold", byid[plain["id"]]["why_aged"])

    def test_the_threshold_is_the_gate_turnaround_NOT_the_reviewers(self):  # noqa: VACUOUS_ASSERTION — the threshold is asserted EQUAL to SOURCE_CLEAN_GATE_S and the hold age strictly past the reviewer's deadline, so the False stall is measured on a row the old clock would bill
        """Held an hour: past the reviewer's 45-minute read, inside one gate
        turnaround — so not late, and the threshold says which clock."""
        row = self.held_at("lane/clock-between", 3600)
        lr = self.lr(row)
        self.assertGreater(3600, int(self.state(row["id"])["deadline_s"]))
        self.assertFalse(lr["stalled"])
        self.assertGreater(landreq.SOURCE_CLEAN_GATE_S, 3600)
        self.assertEqual(lr["stall_threshold_s"], landreq.SOURCE_CLEAN_GATE_S)



class TheHoldDoorRefusesALaneAuthorTest(SourceCleanBase):
    """THE INTEGRATOR'S RULING (task/3053, 22:01Z): a `--source-clean` hold is
    REFUSED at the door when its corroborated recipient is a LANE AUTHOR —
    it wrote a round of the chain — through the chain-contributor join the
    close ladder's condition 1 reads, never a second reading of authorship.
    Before this the door admitted such a hold and the close refused it
    later, so the row sat on the integrator's plate with a claim no land
    could ever close on."""

    def chain(self, lane, first_sender, tip=None):
        """(first, second): round one sent by `first_sender` to OTHER at
        `tip` (`self.side` unless named), and its successor sent by the
        fixture's integrator to READER at `self.b`."""
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": first_sender}):
            first = self.row(tip or self.side, lane, recipient=OTHER)
        self.assertEqual(first["sender"], first_sender)
        second = self.row(self.b, lane, supersedes=first["id"])
        self.assertEqual(second["recipient"], READER)
        return first, second

    def test_a_recipient_who_wrote_an_earlier_round_is_refused_at_the_hold_door(self):  # noqa: VACUOUS_ASSERTION — the refusal names the author positively and the unchanged count is paired with the control: the same chain shape, one fact apart (round one sent by the integrator), holds through the same door and its stamp is asserted by value
        _first, second = self.chain("lane/sc-door-author", READER)
        before = self.history()
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=(READER, None)):
            out, why = dispatches.mark_hold(second["id"], "SOURCE-CLEAN: "
                                            "read clean",
                                            source_clean_tip=self.b)
        self.assertIsNone(out)
        self.assertIn("LANE AUTHOR", why or "")
        self.assertIn(READER, why)
        self.assertIn("--source-clean refused", why)
        self.assertIn("Ask a seat that wrote none of this lane to read it",
                      why)
        self.assertEqual(self.history(), before,
                         "a lane author's source-clean hold APPENDED")
        self.assertEqual(self.state(second["id"])["status"], "open")
        # CONTROL: the SAME chain shape, one fact apart — the integrator sent
        # round one, so the recipient wrote nothing — holds through the door.
        # Round one is at a tip READER never brought: tip provenance is
        # ledger-wide (task/3356 F3), so a round at `side` would name READER,
        # its first bringer above, in this chain too.
        _cfirst, csecond = self.chain("lane/sc-door-nonauthor", "integrator",
                                      tip=self.c)
        self.hold(csecond, self.b)
        self.assertEqual(self.state(csecond["id"])["hold_actor"], READER)
        self.assertEqual(self.state(csecond["id"])["source_clean_tip"],
                         self.b)

    def test_the_door_asks_the_join_the_close_ladder_asks(self):
        """ONE AUTHORSHIP READING: the hold door reaches the same
        chain-contributor join (`landreq._contributor_chains`) through the
        same rule (`source_clean_author_error`) that condition 1 reads."""
        _first, second = self.chain("lane/sc-door-join", READER)
        with mock.patch.object(landreq, "_contributor_chains",
                               wraps=landreq._contributor_chains) as join, \
                mock.patch.object(landreq, "source_clean_author_error",
                                  wraps=landreq.source_clean_author_error) \
                as rule, \
                mock.patch.object(dispatches, "_acting_author",
                                  return_value=(READER, None)):
            _out, why = dispatches.mark_hold(second["id"], "SOURCE-CLEAN: "
                                             "read clean",
                                             source_clean_tip=self.b)
        self.assertIn("LANE AUTHOR", why or "")
        self.assertEqual(rule.call_count, 1)
        self.assertEqual(rule.call_args[0][1], READER)
        self.assertEqual(join.call_count, 1)

    def rewrite_founding(self, rid, **fields):
        """Overwrite fields on one row's founding event, the way a hand-edit
        or a forged ledger would (tests/test_dispatch_chain.py's `rewrite`):
        the one way a row whose chain replays UNKNOWN reaches the door."""
        path = dispatches.ledger_path()
        events = list(eventledger.events(path))
        with open(path, "w", encoding="utf-8") as fh:
            for event in events:
                if event.get("id") == rid and event.get("event") == "dispatch":
                    event.update(fields)
                fh.write(json.dumps(event, separators=(",", ":")) + "\n")

    def test_an_UNREADABLE_chain_refuses_without_sending_the_reader_to_another_seat(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted positively by its words and its kind, and the LANE AUTHOR control through the same door carries the tail and its own kind, so the missing tail is a discrimination
        """RULING 4 (the reader's LOW, round 3): `source_clean_author_error`
        TYPES its refusal, and the door appends "ask a seat that wrote none of
        this lane" only to the LANE AUTHOR shape. An unreadable chain is no
        seat's to fix, so sending the reader to another seat is a door that
        cannot open."""
        row = self.row(self.b, "lane/sc-door-unreadable")
        self.rewrite_founding(row["id"], chain_root="not-a-hex-id")
        state = self.state(row["id"])
        self.assertEqual(state["chain_root"], dispatches.CHAIN_UNKNOWN)
        before = self.history()
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=(READER, None)):
            out, why = dispatches.mark_hold(row["id"], "SOURCE-CLEAN: read "
                                            "clean", source_clean_tip=self.b)
        self.assertIsNone(out)
        self.assertIn("--source-clean refused", why or "")
        self.assertIn("could not be read", why)
        self.assertNotIn("wrote none of this lane", why)
        self.assertEqual(self.history(), before)
        rows, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable, unavailable)
        self.assertEqual(
            landreq.source_clean_author_error(state, READER, rows).kind,
            landreq.SourceCleanRefusal.UNREADABLE)
        # CONTROL, the LANE AUTHOR shape through the same door: the tail, and
        # its own kind.
        _first, second = self.chain("lane/sc-door-typed-author", READER)
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=(READER, None)):
            out, why = dispatches.mark_hold(second["id"], "SOURCE-CLEAN: read "
                                            "clean", source_clean_tip=self.b)
        self.assertIsNone(out)
        self.assertIn("LANE AUTHOR", why or "")
        self.assertIn("Ask a seat that wrote none of this lane to read it",
                      why)
        rows, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable, unavailable)
        self.assertEqual(
            landreq.source_clean_author_error(self.state(second["id"]),
                                              READER, rows).kind,
            landreq.SourceCleanRefusal.LANE_AUTHOR)


class TheHolderRungBillsEverySurfaceTest(SourceCleanBase):
    """RULING 3 (the reader's LOW, round 3): a STAMPED source-clean hold whose
    holder fails `source_clean_holder_error` — the hand is not the
    recipient's, or it is a LANE AUTHOR's — is closable by no land and
    excluded by `helm train`, so it is billed to the REVIEWER with the
    release and re-hold door, exactly as a hold with NO HOLDER is. Every
    surface that bills it or names its door asks that predicate: the
    projection once (`_lr`), and the surfaces built on the projection read
    its answer rather than reading the hold a second time."""

    def rows(self):
        """((row, needle) refused by the holder rung, ...), control — all
        three planted as holds written before the door bound its holder,
        aged past the gate turnaround, at a tip trunk carries."""
        aged = _stamp(time.time() - landreq.SOURCE_CLEAN_GATE_S - 120)
        stranger = self.row(self.b, "lane/sc-bill-stranger")
        self.planted(stranger, self.b, actor=OTHER, ts=aged)
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": READER}):
            first = self.row(self.side, "lane/sc-bill-author",
                             recipient=OTHER)
        authored = self.row(self.b, "lane/sc-bill-author",
                            supersedes=first["id"])
        self.planted(authored, self.b, actor=READER, ts=aged)
        control = self.row(self.b, "lane/sc-bill-control")
        self.planted(control, self.b, actor=READER, ts=aged)
        return ((stranger, "not by the row's recipient"),
                (authored, "LANE AUTHOR")), control

    def surfaces(self, row):
        """(lr, {surface: text}, (sweep terminal, sweep door), the sweep's
        terminal for the dispatch row alone)."""
        from helm import obligation, stalebot
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        state = self.state(row["id"])
        term, ev, door = stalebot.classify_dispatch(state, lr, repo=self.repo,
                                                    trunk=self.main)
        bare = stalebot.classify_dispatch(state, None, repo=self.repo,
                                          trunk=self.main)[0]
        text = {"lr list": landreq._line(lr),
                "lr show": landreq._render_show(lr),
                "obligation": obligation.obligation_of(lr)["what"],
                "stale sweep": "%s / %s" % (ev, door),
                "dispatch word": dispatches._base_label(state)}
        return lr, text, (term, door), bare

    def test_a_stamped_hold_its_holder_rung_refuses_is_the_REVIEWERS_on_every_surface(self):  # noqa: VACUOUS_ASSERTION — the control, stamped by its recipient in the same fixture, is asserted integrator-owed on the same surfaces with the close door named, so each absence on the refused rows is a discrimination
        from helm import stalebot
        refused, control = self.rows()
        for row, needle in refused:
            with self.subTest(needle=needle):
                lr, text, (term, door), bare = self.surfaces(row)
                self.assertEqual(lr["owed_by"], "reviewer")
                self.assertTrue(
                    landreq._owed_by_whom(lr).startswith("reviewer"),
                    landreq._owed_by_whom(lr))
                release = "helm dispatch release %s" % row["id"][:12]
                for name in ("lr list", "lr show", "obligation",
                             "stale sweep"):
                    self.assertIn(release, text[name], name)
                    self.assertIn("--source-clean %s" % self.b[:12],
                                  text[name], name)
                    self.assertIn(needle, text[name], name)
                    self.assertNotIn("foldcheck", text[name], name)
                    self.assertNotIn("helm train", text[name], name)
                self.assertNotIn("waiting on your review", text["obligation"])
                self.assertIn("REVIEWER", text["dispatch word"])
                self.assertNotIn("INTEGRATOR", text["dispatch word"])
                self.assertEqual(term, stalebot.SOURCE_CLEAN_REHOLD)
                self.assertIn(release, door)
                self.assertEqual(bare, stalebot.SOURCE_CLEAN_REHOLD)
        # CONTROL: the recipient's own stamped hold is the integrator's, and
        # every surface names the close it owes.
        lr, text, (term, door), bare = self.surfaces(control)
        self.assertEqual(lr["owed_by"], "integrator")
        self.assertIn("source-clean-landed", text["lr list"])
        self.assertIn("on the integrator", text["lr show"])
        self.assertIn("helm lr foldcheck", text["obligation"])
        self.assertIn("ON THE INTEGRATOR", text["dispatch word"])
        self.assertEqual(term, stalebot.SOURCE_CLEAN_CLOSE)
        self.assertIn("helm lr foldcheck", door)
        self.assertEqual(bare, stalebot.SOURCE_CLEAN_CLOSE)

    def test_the_projection_asks_the_predicate_once_and_its_surfaces_never_again(self):  # noqa: VACUOUS_ASSERTION — the predicate is asserted CALLED for the row by the projection and once more by the dispatch word, so the unchanged count across the projection's surfaces is measured on a live spy
        """ONE PREDICATE CALL AND NO SECOND READING: `_lr` asks the holder
        rung once and carries its answer; `lr list`, `lr show`, the
        obligation sentence and the stale sweep read that answer. The
        dispatch state word, which is built on the dispatch row and never
        the projection, asks the predicate itself — once."""
        from helm import obligation, stalebot
        refused, _control = self.rows()
        row = refused[0][0]
        with mock.patch.object(landreq, "source_clean_holder_error",
                               wraps=landreq.source_clean_holder_error) as rung:
            lr, err = landreq.get(row["id"])
            self.assertIsNone(err, err)
            self.assertTrue([c for c in rung.call_args_list
                             if (c.args[0] or {}).get("id") == row["id"]],
                            "the projection never asked the holder rung")
            asked = rung.call_count
            state = self.state(row["id"])
            landreq._line(lr)
            landreq._render_show(lr)
            obligation._what(lr, "reviewer")
            stalebot._source_clean_door(state, lr, repo=self.repo,
                                        trunk=self.main)
            self.assertEqual(rung.call_count, asked,
                             "a surface built on the projection read the "
                             "hold a second time")
            self.assertIn("REVIEWER", dispatches._base_label(state))
            self.assertEqual(rung.call_count, asked + 1)


class SourceCleanCarsTest(SourceCleanBase, _lw.TrainBase):
    """F3 (task/3053; moved here from task/3039's land-gate-once-doors, "lr
    compose takes a source-clean hold"): `helm train` admits a HELD
    source-clean row as a car on ONE predicate, `landreq.source_clean_car`:
    the hold names a holder, the holder is the row's recipient, the recipient
    wrote no round of the lane, no unanswered FIX or SUPERSEDE from another
    row stands on the held tip, the held tip descends from the dispatched
    ref, and the row is not terminal. The car rides at the HELD tip, and
    every other held source-clean row stays out by name.

    `helm lr compose` asks the same predicate and ADMITS NONE (the author's
    ruling, round 4): it cherry-picks, and `source-clean-landed` closes on
    ancestry only, so a picked copy could never close.

    The fixture is the source-clean fixture with `helm train`'s own door
    spies bound by reference (`tests.test_landwindow.TrainBase`), so the
    train arms drive the shipped verb and door."""

    foldcheck = FoldcheckListsThenClosesTest.foldcheck
    line = FoldcheckListsThenClosesTest.line

    def car(self, name):
        """(row, tip): a lane tip off trunk, dispatched to READER and held
        source-clean at that tip by READER through the door."""
        tip = self.lane(name, name)
        row = self.row(tip, "lane/" + name)
        self.hold(row, tip)
        return row, tip

    def plan(self):
        rc, text = self.train(apply=False)
        self.assertEqual(rc, 0, text)
        return text

    def compose(self, *rows):
        rc, out, err = _lr_run(["compose"] + [r["id"][:12] for r in rows]
                               + ["--dry-run", "--json"])
        return rc, json.loads(out), err

    def refused_by_compose(self, got, rows):
        """{id: reason} after asserting compose composed NONE of `rows` and
        named every one with the ruling's sentence."""
        member_ids = [m["id"] for m in got["members"]]
        why = {x["id"]: x["reason"] for x in got["excluded"]}
        for row in rows:
            self.assertNotIn(row["id"], member_ids)
            self.assertIn(COMPOSE_REFUSAL, why.get(row["id"], ""),
                          "compose did not name %s with the ruling: %r"
                          % (row["id"][:12], why.get(row["id"])))
        return why

    def listed(self, text, rid):
        """The ONE plan line for `rid`: its car line (`N. <id>`) or its
        `EXCLUDED <id>` line — never the close-loop line below the plan."""
        found = [l for l in text.splitlines() if rid[:12] in l and (
            l.strip().startswith("EXCLUDED ")
            or l.strip().split(".", 1)[0].isdigit())]
        self.assertEqual(len(found), 1, "row %s is listed %d times:\n%s"
                         % (rid[:12], len(found), text))
        return found[0]

    def dispatch_list(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = dispatches.cmd_dispatch(["list", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_a_stamped_recipient_non_author_hold_rides_helm_train_and_lr_compose_refuses_it(self):  # noqa: VACUOUS_ASSERTION — the car's train line is asserted positively by value (held tip, SOURCE-CLEAN) and the approve-ready row composes in the same compose call, so the source-clean row's absence from the members is a discrimination
        ready, ready_tip = self.ready("ready", "rdy")
        row, tip = self.car("sc-car")
        text = self.plan()
        self.assertIn("merge order, 2 cars (1 approve-ready, 1 "
                      "source-clean):", text)
        line = self.listed(text, row["id"])
        self.assertIn("held tip %s" % tip[:12], line)
        self.assertIn("SOURCE-CLEAN", line)
        self.assertNotIn("EXCLUDED", line)
        self.assertIn("reviewed tip %s" % ready_tip[:12],
                      self.listed(text, ready["id"]))
        self.assertIn("helm lr foldcheck", text)
        # LR COMPOSE REFUSES IT BY NAME, and still composes the approve-ready
        # row beside it: the refusal is about the car, not the call.
        rc, got, err = self.compose(ready, row)
        self.assertEqual(rc, 0, err)
        self.assertEqual([m["id"] for m in got["members"]], [ready["id"]])
        why = self.refused_by_compose(got, [row])
        self.assertEqual(sorted(why), [row["id"]])
        rc, text, err = _lr_run(["compose", ready["id"][:12], row["id"][:12],
                                 "--dry-run"])
        self.assertEqual(rc, 0, err)
        self.assertIn("EXCLUDED %s" % row["id"][:12], text)
        self.assertIn(COMPOSE_REFUSAL, text)
        self.assertNotIn("CHERRY-PICKED", text)
        # BOTH VERBS ASKED ONE PREDICATE, and this is its answer for the row:
        # the car helm train takes is the car compose turned away.
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(landreq.source_clean_car(lr), (tip, None))

    def test_a_held_source_clean_car_is_unchanged_beside_a_land_first_car(self):
        """task/4223's control: a row dispatched long ago and held
        source-clean still rides as that hold, at its held tip and with its
        own word; land-first takes only the open, unread row beside it."""
        tip = self.lane("sc-old", "sc-old")
        row = self.row(tip, "lane/sc-old")
        self.age(row["id"], landwindow.LAND_FIRST_WAIT_S + 60)
        self.hold(row, tip)
        unread_tip = self.lane("unread", "unread")
        unread = self.row(unread_tip, "lane/unread")
        self.age(unread["id"], landwindow.LAND_FIRST_WAIT_S + 60)
        text = self.plan()
        self.assertIn("merge order, 2 cars (0 approve-ready, 1 source-clean, "
                      "1 landed before review):", text)
        self.assertIn("held tip %s  SOURCE-CLEAN — held by @%s, no approve "
                      "owed" % (tip[:12], READER), self.listed(text,
                                                                row["id"]))
        self.assertIn("unread tip %s  UNREAD" % unread_tip[:12],
                      self.listed(text, unread["id"]))
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(landreq.source_clean_car(lr), (tip, None))

    def test_an_unstamped_an_authors_and_a_non_descending_hold_each_stay_out_by_name(self):  # noqa: VACUOUS_ASSERTION — the control car is asserted positively in the train, and each excluded row's line and reason are asserted by value
        control, ctip = self.car("sc-ok")
        legacy_tip = self.lane("sc-legacy", "sc-legacy")
        legacy = self.held_before(legacy_tip, "lane/sc-legacy", actor=None)
        author_tip = self.lane("sc-author", "sc-author")
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": READER}):
            first = self.row(author_tip, "lane/sc-author", recipient=OTHER)
        authored = self.row(author_tip, "lane/sc-author",
                            supersedes=first["id"])
        self.planted(authored, author_tip, actor=READER)
        from_tip = self.lane("sc-from", "sc-from")
        stray_tip = self.lane("sc-elsewhere", "sc-elsewhere")
        stray = self.row(from_tip, "lane/sc-stray")
        self.planted(stray, stray_tip, actor=READER)
        cases = ((legacy, "NO HOLDER"), (authored, "LANE AUTHOR"),
                 (stray, "does not descend from the dispatched ref"))
        text = self.plan()
        self.assertIn("merge order, 1 car (0 approve-ready, 1 "
                      "source-clean):", text)
        self.assertIn("held tip %s" % ctip[:12],
                      self.listed(text, control["id"]))
        for row, needle in cases[1:]:
            with self.subTest(train=needle):
                line = self.listed(text, row["id"])
                self.assertIn("EXCLUDED", line)
                self.assertIn(needle, line)
        # THE NO HOLDER ROW IS COUNTED, NOT LISTED (ruling 6).
        self.assertNotIn(legacy["id"][:12], text)
        self.assertIn(_no_holder_line(1), text.splitlines())
        # LR COMPOSE ADMITS NONE OF THEM, and each refused hold also carries
        # the reason `helm train` refuses it.
        rc, got, err = self.compose(control, legacy, authored, stray)
        self.assertEqual(rc, 1, err)
        self.assertEqual(got["members"], [])
        why = self.refused_by_compose(
            got, [control] + [r for r, _n in cases])
        for row, needle in cases:
            with self.subTest(compose=needle):
                self.assertIn(needle, why[row["id"]])

    def test_the_integrators_repro_an_undelivered_row_held_by_the_integrator_is_no_car(self):  # noqa: VACUOUS_ASSERTION — the control car rides the train in the same plan and is asserted by value; each refusal names its reason positively
        """The task/3039 QC FIX that took these cars off trunk: a row
        dispatched to another seat and never delivered, held source-clean by
        the INTEGRATOR at its own dispatched tip, composed as a car (rc 0)
        with no reviewer read at all."""
        control, _ctip = self.car("sc-repro-control")
        undelivered = self.row(self.side, "lane/sc-repro",
                               recipient="seat-c")
        before = self.history()
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=("integrator", None)):
            out, why = dispatches.mark_hold(undelivered["id"], "SOURCE-CLEAN",
                                            source_clean_tip=self.side)
        self.assertIsNone(out)
        self.assertIn("@seat-c", why or "")
        self.assertEqual(self.history(), before)
        # THE HOLD AS THE REPRO WROTE IT, before holds recorded a holder.
        self.planted(undelivered, self.side, actor=None)
        # AND AS THE INTEGRATOR'S STAMP WOULD RECORD IT.
        stamped = self.row(self.side, "lane/sc-repro-stamped",
                           recipient="seat-c")
        self.planted(stamped, self.side, actor="integrator")
        rc, got, err = self.compose(control, undelivered, stamped)
        self.assertEqual(rc, 1, err)
        self.assertEqual(got["members"], [])
        why = self.refused_by_compose(got, [control, undelivered, stamped])
        self.assertIn("NO HOLDER", why[undelivered["id"]])
        self.assertIn("not by the row's recipient", why[stamped["id"]])
        text = self.plan()
        self.assertNotIn(undelivered["id"][:12], text)
        self.assertIn(_no_holder_line(1), text.splitlines())
        self.assertIn("not by the row's recipient",
                      self.listed(text, stamped["id"]))
        self.assertIn("merge order, 1 car (0 approve-ready, 1 "
                      "source-clean):", text)

    def test_after_the_train_lands_foldcheck_apply_closes_the_car(self):  # noqa: VACUOUS_ASSERTION — the merge parent, the CLOSED line, the ledger growing by exactly one and the persisted close are all positives; the pre-land unchanged count is paired with them
        row, tip = self.car("sc-landing")
        rc, text = self.train(apply=True)
        self.assertEqual(rc, 0, text)
        room = self.room()
        # THE HELD TIP IS THE MERGE'S SECOND PARENT, by its exact sha.
        self.assertEqual([m[2] for m in self.merges(room)], [tip])
        self.assertEqual(len(self.spawns), 1, self.spawns)
        head = self.git("rev-parse", "HEAD", cwd=room)
        token = self.mint(head)
        # BEFORE THE LAND the car is in the gated head and not on trunk: the
        # sweep names the ancestry condition and closes nothing.
        before = self.history()
        _rc, out, _err = self.foldcheck(head, token, "--apply")
        self.assertIn("condition 2 (ancestry)", self.line(out, row["id"]))
        self.assertEqual(self.history(), before)
        # THE LAND: trunk fast-forwards to the gated head.
        self.git("merge", "--ff-only", "-q", head)
        _rc, out, _err = self.foldcheck(head, token, "--apply")
        self.assertIn("CLOSED", self.line(out, row["id"]))
        self.assertEqual(self.history(), before + 1)
        state = self.state(row["id"])
        self.assertEqual(state["close_reason"], REASON)
        self.assertEqual(state["reviewed_tip"], tip)
        self.assertEqual("gate:" + state["source_clean_gate"], token)

    def test_a_held_tip_already_on_trunk_is_excluded_by_train_naming_the_foldcheck_close(self):  # noqa: VACUOUS_ASSERTION — the train exclusion is asserted by value to carry the foldcheck close and compose's to carry the ruling; the control car in the sibling arms proves the same door admits
        """A hold at a tip trunk already carries is no car: `helm train`
        names the one close that row can take — `helm lr foldcheck` on a
        verified whole-suite receipt — never "close it", which no approve can
        do for a held row. `lr compose` refuses it as it refuses every
        source-clean candidate."""
        row = self.held(self.b, "lane/sc-on-trunk")
        text = self.plan()
        line = self.listed(text, row["id"])
        self.assertIn("EXCLUDED", line)
        self.assertIn("helm lr foldcheck", line)
        rc, got, err = self.compose(row)
        self.assertEqual(rc, 1, err)
        self.assertEqual(got["members"], [])
        self.refused_by_compose(got, [row])

    def test_an_unanswered_FIX_on_the_held_tip_keeps_the_car_out_naming_the_contesting_row(self):  # noqa: VACUOUS_ASSERTION — the SAME row rides the train by value before the FIX is recorded, so the exclusion after it is a discrimination on one fact
        """THE READER'S MEDIUM, round 3 (ruling 2): a held tip carrying an
        unanswered FIX or SUPERSEDE from ANOTHER row or chain is no car, by
        the join `ready_rung_why`'s CONTESTED rung asks for an approve row,
        and `helm train` names the contesting row as it names READY-CONTESTED.
        The reader's input: row A held source-clean at T by its stamped
        non-author recipient; a second chain's row B on T gets FIX."""
        row, tip = self.car("sc-contested")
        # CONTROL, the same row one fact apart: with no FIX on T it rides.
        text = self.plan()
        self.assertIn("held tip %s" % tip[:12], self.listed(text, row["id"]))
        self.assertNotIn("EXCLUDED", self.listed(text, row["id"]))
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(landreq.source_clean_car(lr), (tip, None))
        # THE SECOND CHAIN'S READ OF THE SAME TIP FINDS SOMETHING.
        leg = self.row(tip, "lane/sc-contested-leg", recipient=OTHER)
        self.assertNotEqual(leg.get("chain_root") or leg["id"],
                            row.get("chain_root") or row["id"])
        _verdict, err = self.mark_verdict(leg["id"], tip, "FIX: a finding",
                                          polarity="fix")
        self.assertIsNone(err, err)
        text = self.plan()
        line = self.listed(text, row["id"])
        self.assertIn("EXCLUDED", line)
        self.assertIn("CONTESTED", line)
        self.assertIn(leg["id"][:12], line)
        self.assertIn("merge order, 0 approve-ready rows:", text)
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        got_tip, why = landreq.source_clean_car(lr)
        self.assertIsNone(got_tip)
        self.assertIn(leg["id"][:12], why)
        self.assertEqual(getattr(why, "kind", None),
                         landreq.SourceCleanRefusal.CONTESTED)

    def test_the_read_side_builds_the_memoised_join_once_for_every_car(self):
        """RULING 5: a read-side caller of the predicate reads the memoised
        `chain_contributor_index()`, so three stamped cars cost ONE join, not
        one each. The hold door keeps its shared-fold join
        (`test_the_door_asks_the_join_the_close_ladder_asks`)."""
        cars = [self.car("sc-join-%d" % i) for i in range(3)]
        lrs = [landreq.get(row["id"])[0] for row, _tip in cars]
        landreq._CHAIN_CONTRIB_MEMO.clear()
        with mock.patch.object(landreq, "_contributor_chains",
                               wraps=landreq._contributor_chains) as join:
            got = [landreq.source_clean_car(lr) for lr in lrs]
        self.assertEqual(got, [(tip, None) for _row, tip in cars])
        self.assertEqual(join.call_count, 1)

    def test_the_NO_HOLDER_backlog_is_one_counted_line_naming_the_verb_that_lists_it(self):  # noqa: VACUOUS_ASSERTION — the control car and the stranger's own EXCLUDED line are asserted by value in the same plan, and the named verb's listing is asserted EQUAL to the three rows
        """RULING 6: the ~36 legacy unstamped holds are ONE counted line in
        `helm train`, naming a verb that lists them; every other exclusion
        keeps its own line."""
        control, ctip = self.car("sc-backlog-ok")
        legacy = []
        for i in range(3):
            tip = self.lane("sc-backlog-%d" % i, "sc-backlog-%d" % i)
            legacy.append(self.held_before(tip, "lane/sc-backlog-%d" % i,
                                           actor=None))
        stranger_tip = self.lane("sc-backlog-x", "sc-backlog-x")
        stranger = self.row(stranger_tip, "lane/sc-backlog-x")
        self.planted(stranger, stranger_tip, actor=OTHER)
        text = self.plan()
        self.assertIn("held tip %s" % ctip[:12],
                      self.listed(text, control["id"]))
        self.assertIn("not by the row's recipient",
                      self.listed(text, stranger["id"]))
        self.assertEqual([l for l in text.splitlines() if "NO HOLDER" in l],
                         [_no_holder_line(3)])
        for row in legacy:
            self.assertNotIn(row["id"][:12], text)
        # THE VERB IT NAMES LISTS EXACTLY THOSE ROWS.
        rc, out, err = self.dispatch_list("--no-holder", "--all-projects",
                                          "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(r["id"] for r in json.loads(out)),
                         sorted(r["id"] for r in legacy))


class TheSourceCleanDoorIsNamedDownstreamTest(SourceCleanBase):
    """The prior read's NITs (g, h): the stale sweep's classifier and the
    obligation sentence answer a source-clean row with ITS door — the re-hold
    when the hold records no holder, the train and the foldcheck close when
    it does — never `close --reason superseded` and never "waiting on your
    review"."""

    def classify(self, row):
        from helm import stalebot
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        return stalebot.classify_dispatch(self.state(row["id"]), lr,
                                          repo=self.repo, trunk=self.main)

    def test_the_stale_sweep_proposes_the_rehold_or_the_foldcheck_never_superseded(self):  # noqa: VACUOUS_ASSERTION — every terminal and door is asserted by value, and the open control row on the same trunk commit proposes SUPERSEDE, so each absence is a discrimination
        from helm import stalebot
        # CONTROL: an open review row at the same trunk commit is the path a
        # source-clean row took before this cure — supersede.
        plain = self.row(self.b, "lane/sb-plain")
        term, _ev, door = self.classify(plain)
        self.assertEqual(term, stalebot.SUPERSEDE)
        self.assertIn("--reason superseded", door)
        legacy = self.held_before(self.b, "lane/sb-legacy", actor=None)
        term, ev, door = self.classify(legacy)
        self.assertIn("helm dispatch release %s" % legacy["id"][:12], door)
        self.assertIn("--source-clean %s" % self.b[:12], door)
        self.assertIn("NO HOLDER", ev)
        self.assertNotIn("superseded", door)
        self.assertEqual(term, stalebot.SOURCE_CLEAN_REHOLD)
        landed = self.held(self.b, "lane/sb-landed")
        term, ev, door = self.classify(landed)
        self.assertIn("helm lr foldcheck", door)
        self.assertIn("--apply", door)
        self.assertNotIn("helm train", door)
        self.assertNotIn("superseded", door)
        self.assertEqual(term, stalebot.SOURCE_CLEAN_CLOSE)
        waiting = self.held(self.side, "lane/sb-waiting")
        term, ev, door = self.classify(waiting)
        self.assertIn("helm train", door)
        self.assertIn("helm lr foldcheck", door)
        self.assertNotIn("superseded", door)
        self.assertEqual(term, stalebot.SOURCE_CLEAN_CLOSE)

    def test_the_obligation_sentence_names_the_rehold_not_a_review(self):  # noqa: VACUOUS_ASSERTION — each sentence is asserted to carry its door positively, and the plain open row is the control that still reads as waiting on its review
        from helm import obligation
        legacy = self.held_before(self.b, "lane/ob-legacy", actor=None)
        got = obligation.obligation_of(landreq.get(legacy["id"])[0])
        self.assertEqual(got["owed_by"], "reviewer")
        self.assertIn("NO HOLDER", got["what"])
        self.assertIn("helm dispatch release %s" % legacy["id"][:12],
                      got["what"])
        self.assertIn("--source-clean %s" % self.b[:12], got["what"])
        self.assertNotIn("waiting on your review", got["what"])
        # CONTROL, THE SAME ROLE: an ordinary row's reviewer sentence still
        # says it waits on the review; only the source-clean fields differ.
        plain = self.row(self.side, "lane/ob-plain")
        plain_lr = landreq.get(plain["id"])[0]
        self.assertIsNone(plain_lr.get("source_clean_tip"))
        self.assertIn("waiting on your review",
                      obligation._what(plain_lr, "reviewer"))
        # AND A STAMPED HOLD IS THE INTEGRATOR'S TRAIN AND CLOSE, not an
        # "open and unrouted" lane.
        stamped = self.held(self.side, "lane/ob-stamped")
        owed = obligation.obligation_of(landreq.get(stamped["id"])[0])
        self.assertEqual(owed["owed_by"], "integrator")
        self.assertIn("helm train", owed["what"])
        self.assertIn("helm lr foldcheck", owed["what"])
        self.assertNotIn("unrouted", owed["what"])


class ANoHolderRowWhoseRecipientIsALaneAuthorTest(SourceCleanBase):
    """THE FABLE READ of task/3053, finding on (c): the holder rung answered
    NO HOLDER before it asked whether the RECIPIENT may re-hold at all, so
    for a legacy row whose recipient wrote a round of its chain the board,
    `lr list`, the obligation sentence and the stale sweep all named a door
    the hold door refuses by name — the recipient's own `--source-clean`
    re-hold, refused as LANE AUTHOR. Measured on the live ledger with the
    lane's code: 6 of the 50 held source-clean rows (kimi x4,
    simbi-inc-codex, meta-claude). The rung now asks the author rung of the
    recipient first, so the door it names is one that opens: a seat that
    wrote none of the lane reads it, and `helm train` lists the row on its
    own line rather than inside the NO HOLDER count."""

    def chain(self, lane, first_sender, tip=None):
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": first_sender}):
            first = self.row(tip or self.side, lane, recipient=OTHER)
        self.assertEqual(first["sender"], first_sender)
        second = self.row(self.b, lane, supersedes=first["id"])
        self.assertEqual(second["recipient"], READER)
        return first, second

    def test_the_rung_names_a_non_author_reader_never_the_authors_own_rehold(self):  # noqa: VACUOUS_ASSERTION — the control row of the same shape, one fact apart, is asserted by value to carry the NO HOLDER kind and the recipient's own re-hold door, so each absence on the author's row is a discrimination
        _first, second = self.chain("lane/sc-nh-author", READER)
        self.planted(second, self.b, actor=None)
        state = self.state(second["id"])
        why = landreq.source_clean_holder_error(state)
        self.assertEqual(getattr(why, "kind", None),
                         landreq.SourceCleanRefusal.LANE_AUTHOR, why)
        self.assertIn("NO HOLDER", why)
        self.assertIn("LANE AUTHOR", why)
        self.assertIn(READER, why)
        rehold = landreq.source_clean_rehold(state)
        self.assertEqual(rehold["kind"], landreq.SourceCleanRefusal.LANE_AUTHOR)
        self.assertIn("a seat that wrote none of this lane", rehold["door"])
        self.assertIn("helm dispatch rebind %s" % second["id"][:12],
                      rehold["why"])
        # NEVER the recipient's own re-hold as the whole door.
        own = ("helm dispatch release %s; helm dispatch hold %s <reason> "
               "--source-clean %s" % (second["id"][:12], second["id"][:12],
                                      self.b[:12]))
        self.assertNotEqual(rehold["door"], own)
        # THE SURFACES READ THE SAME ANSWER: the projection bills the
        # reviewer and its sentence names the non-author reader; the train
        # predicate carries the kind, so the row is not folded into the NO
        # HOLDER count.
        lr, err = landreq.get(second["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["owed_by"], "reviewer")
        self.assertEqual(lr["source_clean_rehold"]["kind"],
                         landreq.SourceCleanRefusal.LANE_AUTHOR)
        self.assertIn("a seat that wrote none of this lane",
                      landreq.source_clean_on_main(lr) or "")
        self.assertIn("a seat that wrote none of this lane", landreq._line(lr))
        _tip, car_why = landreq.source_clean_car(lr)
        self.assertEqual(getattr(car_why, "kind", None),
                         landreq.SourceCleanRefusal.LANE_AUTHOR, car_why)
        # THE CONTROL: the same chain shape, one fact apart — the integrator
        # sent round one, so the recipient wrote nothing — is NO HOLDER, and
        # its door is the recipient's own re-hold. Round one is at a tip
        # READER never brought: tip provenance is ledger-wide (task/3356 F3),
        # so a round at `side` would name READER, its first bringer above.
        _cfirst, csecond = self.chain("lane/sc-nh-nonauthor", "integrator",
                                      tip=self.c)
        self.planted(csecond, self.b, actor=None)
        cwhy = landreq.source_clean_holder_error(self.state(csecond["id"]))
        self.assertEqual(getattr(cwhy, "kind", None),
                         landreq.SourceCleanRefusal.NO_HOLDER, cwhy)
        crehold = landreq.source_clean_rehold(self.state(csecond["id"]))
        self.assertEqual(crehold["door"], (
            "helm dispatch release %s; helm dispatch hold %s <reason> "
            "--source-clean %s" % (csecond["id"][:12], csecond["id"][:12],
                                   self.b[:12])))


class ACopyOnTrunkNamesNoLandDoorTest(SourceCleanBase):
    """THE FABLE READ of task/3053, finding on (c)/(d): trunk carrying a
    PATCH-IDENTICAL COPY of the held tip marks the row `trunk_contains_tip`
    True (`_PROOF_DISCHARGE` admits ancestry or patch identity), but the
    source-clean close proves ancestry alone and refuses that row by name.
    The board's one sentence named the refusing doors for it — `foldcheck
    --apply` and `lr close --reason source-clean-landed` — while the stale
    sweep's classifier answered the SAME row "no land door closes this row".
    Live on the lane's own code: row b080150f6193 (proof patch-equivalent)
    once its recipient re-holds it. One row, two surfaces, two answers."""

    def test_the_board_and_the_stale_sweep_say_the_same_thing_about_a_copy(self):  # noqa: VACUOUS_ASSERTION — the ancestor control in the same fixture is asserted to name the foldcheck door and the close verb by value, so the copy row's absent doors are a discrimination on the proof word alone
        from helm import stalebot
        self.git("cherry-pick", self.side)          # a COPY of `side` on trunk
        copy = self.held(self.side, "lane/sc-copy-board")
        lr, err = landreq.get(copy["id"])
        self.assertIsNone(err, err)
        # THE PRECONDITION, MEASURED: the projection proves containment by
        # patch identity, not ancestry — the mark the seam reads.
        self.assertIs(lr["trunk_contains_tip"], True)
        self.assertEqual(lr["trunk_contains_proof"],
                         landreq.PROOF_PATCH_EQUIVALENT)
        self.assertIsNone(lr.get("source_clean_rehold"))
        said = landreq.source_clean_on_main(lr)
        self.assertTrue(said and said.startswith("on main · "), said)
        self.assertIn(landreq.SOURCE_CLEAN_COPY_ON_TRUNK, said)
        for door in ("helm lr foldcheck", "helm lr close", "--apply"):
            self.assertNotIn(door, said, said)
        # THE SWEEP PRINTS THE SAME CLAUSE and proposes no land door.
        term, ev, door = stalebot.classify_dispatch(
            self.state(copy["id"]), lr, repo=self.repo, trunk=self.main)
        self.assertEqual(term, stalebot.KEEP)
        self.assertIn(landreq.SOURCE_CLEAN_COPY_ON_TRUNK, ev)
        self.assertEqual(door, "")
        # AND `lr list`'s line carries it after ALREADY ON TRUNK, never the
        # close it cannot take.
        line = landreq._line(lr)
        self.assertIn("ALREADY ON TRUNK — " + said, line)
        self.assertNotIn("source-clean-landed --gate", line)
        # THE CONTROL: the same door on a held tip trunk holds by ANCESTRY
        # names the close, and the sweep names the foldcheck.
        landed = self.held(self.b, "lane/sc-ancestor-board")
        lr_b, err = landreq.get(landed["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr_b["trunk_contains_proof"], landreq.PROOF_ANCESTOR)
        said_b = landreq.source_clean_on_main(lr_b)
        self.assertIn("helm lr foldcheck <head> --gate gate:<id> --apply",
                      said_b)
        self.assertIn("helm lr close %s --reason source-clean-landed"
                      % landed["id"][:12], said_b)
        self.assertNotIn("COPY", said_b)
        term, _ev, door = stalebot.classify_dispatch(
            self.state(landed["id"]), lr_b, repo=self.repo, trunk=self.main)
        self.assertEqual(term, stalebot.SOURCE_CLEAN_CLOSE)
        self.assertIn("helm lr foldcheck", door)


    def test_a_copy_on_trunk_is_owed_by_its_reviewer(self):
        """THE AUTHOR'S RULING on the Fable read's open question: a held
        source-clean row whose tip trunk carries only as a patch-identical
        COPY is owed by its REVIEWER, because the one move left is a verdict
        from its recipient (SOURCE_CLEAN_COPY_ON_TRUNK says so) and every
        close door refuses a copy. Billing the integrator would chase the
        seat that cannot pay; the control on an ancestor-landed hold stays the
        integrator's."""
        self.git("cherry-pick", self.side)
        copy = self.held(self.side, "lane/sc-copy-owed")
        lr, err = landreq.get(copy["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["trunk_contains_proof"],
                         landreq.PROOF_PATCH_EQUIVALENT)
        self.assertEqual(lr["owed_by"], "reviewer")
        landed = self.held(self.b, "lane/sc-ancestor-owed")
        lr_b, err = landreq.get(landed["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr_b["trunk_contains_proof"], landreq.PROOF_ANCESTOR)
        self.assertEqual(lr_b["owed_by"], "integrator")


#: A passing fab line as a hold reason carries it (task/4103).
FAB_RAN = "fab Ran 63 tests in 4.2s OK"


class AHoldCarriesItsFabReceiptTest(SourceCleanBase):
    """A `--source-clean` hold names the fab run behind it (task/4103).

    MEASURED: seats held rows source-clean 1-3 minutes after dispatch with
    reasons that carried no fab line, and about nine landed through auto-land
    on those holds alone. A source-clean hold is now refused unless its
    reason carries a passing `Ran N` line or the fab LOG of a run on exactly
    the held tip -- or, for a hold over a standing CONCUR at that tip, the
    CONCUR's own evidence carries one. A hold on a row whose recorded read
    is a FIX is refused by its verdict's name. A hold with no claim is
    unchanged. Every refusal asserts the effect: nothing appended, the row
    not held."""

    def attempt(self, row, tip, reason):
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=(READER, None)), \
                mock.patch.object(home, "session_id",
                                  return_value=self.READER_SESSION):
            return dispatches.mark_hold(row["id"], reason,
                                        source_clean_tip=tip)

    def log(self, tip):
        """A fab LOG path in the shape fab writes: its job name carries the
        first 11 hex chars of the tip the run tested."""
        return ("LOG node-a:~/fab/logs/lane-hold-%s-1790920203-44782e44-"
                "2454861.log" % tip[:11])

    def test_a_hold_with_no_receipt_is_refused_and_names_the_shape(self):  # noqa: VACUOUS_ASSERTION — the same row is then held through the same door with a Ran line and asserted HELD at the tip, so the refusal is the missing receipt and not a door that refuses everything
        row = self.row(self.side, "lane/sc-receipt-none")
        before = self.history()
        out, why = self.attempt(row, self.side, "SOURCE-CLEAN: read clean")
        self.assertIsNone(out)
        self.assertIn("no fab receipt", str(why))
        self.assertIn("Ran N tests", str(why))
        self.assertIn(self.side[:11], str(why))
        self.assertIn("--source-clean %s" % self.side[:12], str(why))
        self.assertEqual(self.history(), before)
        self.assertEqual(self.state(row["id"])["status"], "open")
        out, why = self.attempt(row, self.side,
                                "SOURCE-CLEAN: read clean; " + FAB_RAN)
        self.assertIsNone(why, why)
        self.assertEqual(self.state(row["id"])["source_clean_tip"], self.side)

    def test_a_LOG_for_another_tip_is_refused(self):  # noqa: VACUOUS_ASSERTION — the matching-LOG arm below holds through the same door on the same fixture, and the refusal names both tips
        row = self.row(self.side, "lane/sc-receipt-foreign")
        before = self.history()
        out, why = self.attempt(row, self.side,
                                "SOURCE-CLEAN: read clean, " + self.log(self.b))
        self.assertIsNone(out)
        self.assertIn(self.b[:11], str(why))
        self.assertIn("another tip", str(why))
        self.assertEqual(self.history(), before)
        self.assertEqual(self.state(row["id"])["status"], "open")
        # AND ITS COUNT IS THAT RUN'S: a passing Ran line beside only a LOG
        # of another tip is a run on another tree, cited as this one's.
        out, why = self.attempt(row, self.side, "SOURCE-CLEAN: fab Ran 63 "
                                "tests OK, " + self.log(self.b))
        self.assertIsNone(out)
        self.assertIn("another tip", str(why))
        self.assertEqual(self.history(), before)

    def test_a_LOG_of_the_held_tip_is_accepted(self):  # noqa: VACUOUS_ASSERTION — the hold is read back from the fold by status and tip
        row = self.row(self.side, "lane/sc-receipt-log")
        out, why = self.attempt(row, self.side,
                                "SOURCE-CLEAN: read clean, " + self.log(self.side))
        self.assertIsNone(why, why)
        folded = self.state(row["id"])
        self.assertEqual((folded["status"], folded["source_clean_tip"]),
                         ("held", self.side))

    def test_a_passing_Ran_line_is_accepted_and_a_FAILED_one_is_not(self):  # noqa: VACUOUS_ASSERTION — the FAILED arm and the passing arm run through the same door on the same row, and the passing hold is read back from the fold
        row = self.row(self.side, "lane/sc-receipt-ran")
        before = self.history()
        out, why = self.attempt(row, self.side, "SOURCE-CLEAN: fab Ran 63 "
                                "tests in 4.2s FAILED (failures=1)")
        self.assertIsNone(out)
        self.assertIn("no fab receipt", str(why))
        self.assertEqual(self.history(), before)
        out, why = self.attempt(row, self.side, "SOURCE-CLEAN: read the "
                                "delta; Ran 63 tests in 4.2s ... OK")
        self.assertIsNone(why, why)
        self.assertEqual(self.state(row["id"])["status"], "held")

    def concur(self, lane, evidence):
        row = self.row(self.side, lane)
        out, why = self.mark_verdict(row["id"], self.side, evidence,
                                     polarity="concur")
        self.assertIsNone(why, why)
        self.assertEqual(out["status"], "verdict")
        return row

    def test_a_held_CONCUR_whose_evidence_carries_the_receipt_is_accepted(self):  # noqa: VACUOUS_ASSERTION — a CONCUR whose evidence carries none is refused through the same door first, so the admission is the CONCUR's receipt
        bare = self.concur("lane/sc-receipt-concur-bare", "read clean")
        before = self.history()
        out, why = self.attempt(bare, self.side, "SOURCE-CLEAN: the CONCUR")
        self.assertIsNone(out)
        self.assertIn("no fab receipt", str(why))
        self.assertEqual(self.history(), before)
        self.assertEqual(self.state(bare["id"])["status"], "verdict")
        row = self.concur("lane/sc-receipt-concur",
                          "read clean; " + self.log(self.side))
        out, why = self.attempt(row, self.side, "SOURCE-CLEAN: the CONCUR")
        self.assertIsNone(why, why)
        folded = self.state(row["id"])
        self.assertEqual((folded["status"], folded["source_clean_tip"],
                          folded.get("polarity")),
                         ("held", self.side, "concur"))

    def test_a_FIX_verdict_row_is_refused_by_its_verdict(self):  # noqa: VACUOUS_ASSERTION — the refusal names FIX, the ledger is unchanged and the row is still the FIX verdict
        row = self.row(self.side, "lane/sc-receipt-fix")
        out, why = self.mark_verdict(row["id"], self.side, "a finding",
                                     polarity="fix")
        self.assertIsNone(why, why)
        before = self.history()
        out, why = self.attempt(row, self.side,
                                "SOURCE-CLEAN: read clean; " + FAB_RAN)
        self.assertIsNone(out)
        self.assertIn("FIX verdict", str(why))
        self.assertIn("--supersedes %s" % row["id"][:12], str(why))
        self.assertEqual(self.history(), before)
        folded = self.state(row["id"])
        self.assertEqual((folded["status"], folded.get("polarity")),
                         ("verdict", "fix"))

    def test_a_plain_hold_needs_no_receipt(self):  # noqa: VACUOUS_ASSERTION — the hold is read back from the fold with no source-clean claim
        row = self.row(self.side, "lane/sc-receipt-plain")
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=(READER, None)):
            out, why = dispatches.mark_hold(row["id"], "waiting on a build box")
        self.assertIsNone(why, why)
        folded = self.state(row["id"])
        self.assertEqual(folded["status"], "held")
        self.assertNotIn("source_clean_tip", folded)


class AHoldAutoLandTakesWakesNobodyTest(SourceCleanBase, _lw.TrainBase):
    """task/4215: a source-clean hold DMed the integrator "the next move is
    YOURS: land <tip>" even when auto-land's switch was ON and its planner
    would take the car by itself, so every such hold cost a wake turn for a
    move nobody had to make. With the switch ON and the planner admitting
    the car, the hold posts one retained, NON-WAKING row instead and sends no
    DM. With the switch ON and the planner turning the car away, the DM is
    sent and names the planner's own reason. With the switch OFF the DM is
    today's, word for word."""

    car = SourceCleanCarsTest.car
    plan = SourceCleanCarsTest.plan

    def root_of(self, row):
        """The checkout the row is bound to, as the planner resolves it."""
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        gitdir, why = landreq_close._close_repo(lr, None)
        self.assertIsNone(why, why)
        return gitdir[:-5] if gitdir.endswith("/.git") else gitdir

    def nudge(self, row):
        """Run the hold's notice on the folded row -> (DMs, room posts), with
        auto-land's timer reported installed so only the switch decides."""
        from helm import chat, dispatches_cli
        dms, posts = [], []
        with mock.patch.object(dispatches, "_nudge",
                               lambda to, body, ctx: dms.append(
                                   (to, body, ctx))), \
                mock.patch.object(chat, "post",
                                  lambda text, **kw: posts.append(
                                      (text, kw)) or {"id": "x"}), \
                mock.patch.object(autoland, "timer_installed",
                                  return_value=True):
            dispatches_cli._hold_holder_nudge(self.state(row["id"]))
        return dms, posts

    def test_switch_on_and_an_admitted_car_sends_no_dm_and_posts_one_ambient_row(self):  # noqa: VACUOUS_ASSERTION — the empty DM list is paired with one ambient row asserted by value (room, class, sender, row id, held tip) and the plan listing the same car
        row, tip = self.car("sc-auto-take")
        # PRECONDITION: `helm train` itself lists this row as a car.
        self.assertIn("held tip %s" % tip[:12],
                      SourceCleanCarsTest.listed(self, self.plan(),
                                                 row["id"]))
        dms, posts = self.nudge(row)
        self.assertEqual(dms, [], "a car auto-land takes woke the integrator")
        self.assertEqual(len(posts), 1, posts)
        text, kw = posts[0]
        self.assertTrue(kw.get("ambient"), kw)
        self.assertEqual(kw.get("room"), autoland.ROOM)
        self.assertEqual(kw.get("who"), "dispatches")
        self.assertIn(row["id"][:12], text)
        self.assertIn(tip[:12], text)
        self.assertIn("auto-land", text.lower())
        self.assertNotIn("@", text)

    def test_switch_on_and_an_ejected_car_dms_the_planners_reason(self):  # noqa: VACUOUS_ASSERTION — the DM is asserted by value to carry the planner's ejection clause; the empty post list pairs with it
        row, tip = self.car("sc-auto-ejected")
        _row, err = landwindow.record_ejection(self.root_of(row), {
            "tip": tip, "train": "train77", "gate": "feedface",
            "tests": ["tests.test_x.T.test_y"]})
        self.assertIsNone(err, err)
        # PRECONDITION: the planner now excludes it, for that ejection.
        self.assertIn("ejected from train77",
                      SourceCleanCarsTest.listed(self, self.plan(),
                                                 row["id"]))
        dms, posts = self.nudge(row)
        self.assertEqual(posts, [])
        self.assertEqual(len(dms), 1, dms)
        _to, body, ctx = dms[0]
        self.assertIn("The next move is YOURS", body)
        self.assertIn("AUTO-LAND IS ON BUT WILL NOT TAKE THIS CAR", body)
        self.assertIn("ejected from train77", body)
        self.assertIn("auto-land will not take it", ctx)

    def test_switch_off_dms_as_before(self):  # noqa: VACUOUS_ASSERTION — the DM is asserted by value against the full pre-change text; the empty post list pairs with it
        row, tip = self.car("sc-auto-off")
        _control, why = autoland.pause(self.root_of(row), "tester", "drill")
        self.assertIsNone(why, why)
        lane = self.state(row["id"])["lane"]
        dms, posts = self.nudge(row)
        self.assertEqual(posts, [])
        self.assertEqual(len(dms), 1, dms)
        _to, body, ctx = dms[0]
        self.assertEqual(body, (
            "SOURCE-CLEAN HOLD on %s (%s) — @%s read the delta and found "
            "nothing, and cannot mint an approve because one binds a "
            "whole-suite token only your land gate produces. The next move is "
            "YOURS: land %s under a whole-suite gate, then `helm lr foldcheck "
            "<head> --gate gate:<id> --apply`, naming a verified whole-suite "
            "receipt on a commit containing the tip, closes this row "
            "(--reason source-clean-landed) — no approve is needed."
            % (row["id"][:12], lane, READER, tip[:12])))
        self.assertEqual(ctx, "%s: SOURCE-CLEAN at %s — review complete, "
                              "awaiting your whole-suite gate"
                         % (lane, tip[:12]))


if __name__ == "__main__":
    unittest.main()
