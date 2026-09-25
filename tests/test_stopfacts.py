#!/usr/bin/env python3
"""The stop facts: what the `helm web` resident writes, what a Stop reads, and
what every surface says when the facts are EXACT, STALE or ABSENT.

THE SURFACE x STATE MATRIX (task/3042 round 2). The law in every cell: only
positive proof exempts; UNKNOWN or stale never exempts and never waits. Each
cell names its arm (this file unless marked), or the reason it has none.

  claims rung (C)
    fresh ........... SurfaceByStateTest.test_stop_guard_EXACT_facts_exempt_the_gated_lane
    stale own lease . test_claims_rung_a_STALE_own_lease_answers_at_once_and_never_sleeps,
                      test_stop_guard_a_STALE_lease_keeps_the_block_and_says_why
    absent .......... test_stop_guard_NO_resident_keeps_the_block_and_names_it
    older code ...... test_stop_guard_OTHER_CODE_keeps_the_block_and_names_it
    mid-exec ........ test_claims_rung_a_resident_mid_exec_keeps_the_block
    two residents ... no arm of its own: a stop reads ONE file, and the lock
                      makes one writer of it (ResidentTest, below), so the
                      reading is the fresh cell's
    malformed row ... test_claims_rung_a_malformed_row_keeps_the_block_and_never_raises,
                      ReaderTest.test_a_row_of_the_wrong_shape_is_judged_never_raised_on
  seam rung (S) — tests/test_stop_seam.py ResidentSeamTest
    fresh / stale own lease (a HEAD move) / absent / older code
                      fresh_*, head_moved_*, absent_*, other_code_*
    mid-exec ........ refolding_*
    two residents ... as C: one file, one writer
    malformed row ... malformed_*
  whisper (W) — seats_work_offer's dispatch rung, fed `View.owed_pair`
    every state ..... test_whisper_reads_the_owed_frontier_only_from_EXACT_facts
                      (fresh, post-commit, an append naming an owed row,
                      absent, older code, mid-exec, malformed, and the
                      ledger's own fault); two residents as C
  doctor (D)
    fresh ........... test_doctor_OK_with_the_age
    stale own lease . test_doctor_stays_OK_when_a_held_lane_commits
    absent .......... test_doctor_FAIL_with_the_cure_when_ABSENT_or_other_code
    older code ...... test_doctor_names_a_resident_whose_loaded_code_is_older_than_the_tree
    mid-exec ........ test_doctor_WARNs_on_a_resident_mid_exec_and_FAILs_a_dead_one
    two residents ... tests/test_webserve.py (doctor.check_web_servers FAILs
                      more than one console); the snapshot has one writer
    malformed row ... test_doctor_reads_a_malformed_snapshot_without_raising
  the resident (R) — ResidentTest
    fresh ........... test_ONE_writer_and_the_loser_never_writes
    stale own lease . test_a_commit_in_a_held_lane_is_recomputed_on_the_next_poll
    absent .......... test_ONE_writer_and_the_loser_never_writes (its first
                      write is onto no file)
    older code ...... test_a_process_whose_code_was_replaced_does_not_write,
                      test_a_changed_tree_re_execs_ONCE_with_this_processs_own_argv,
                      test_the_poll_re_execs_before_it_kicks_a_refresh,
                      test_a_tree_that_does_not_import_is_never_exec_d_onto
    mid-exec ........ test_a_snapshot_write_in_flight_finishes_before_the_exec,
                      test_the_image_after_the_exec_marks_the_facts_refolding
    two residents ... test_ONE_writer_and_the_loser_never_writes,
                      test_a_second_resident_still_loses_the_lock_after_the_exec
    malformed row ... test_a_malformed_snapshot_on_disk_costs_a_recompute_not_a_wedge

NEVER WAITS, for every surface at once: ReaderTest.
test_nothing_in_the_reader_can_wait_on_state — the reader every rung reads
through names no sleep.

Every arm drives the SHIPPED reader and the SHIPPED resident: facts come from
`stopfacts_resident.compute`, files from `stopfacts_resident.write`, and the
stop from `helm chat stop-guard --hook-json`.
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

from tests.test_seats import SeatsBase
from tests._stopfacts import LaneWorld

from helm import (dispatches, doctor, pk, seats, stopfacts,
                  stopfacts_resident, web_cache)

# THE FIXTURE THAT PROTECTS THESE ARMS LIVES IN ANOTHER FILE, and two
# source-driven audits read THIS one (the scratch reaper and env hygiene each
# parse a module on its own). So the reaper is disabled HERE too, and put back.
_ENV_PRIOR = {}


def setUpModule():
    _ENV_PRIOR["HELM_SCRATCH_GC"] = os.environ.get("HELM_SCRATCH_GC")
    os.environ["HELM_SCRATCH_GC"] = "0"


def tearDownModule():
    for key, was in _ENV_PRIOR.items():
        if was is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = was


def _snapshot(**over):
    """A minimal well-formed snapshot for the reader's own arms."""
    snap = {"schema": stopfacts.SCHEMA, "written_at": time.time(),
            "policy": stopfacts.code_policy(),
            "ledger": {"path": "/nonexistent/ledger", "ino": None,
                       "size": None, "mtime_ns": None, "offset": 0,
                       "unavailable": None},
            "resident": {"pid": os.getpid(),
                         "starttime": stopfacts.own_starttime(),
                         "started_at": time.time(), "replaying_since": None},
            "leases": {}, "seats": {}, "fleet": {}, "rows": {}}
    snap.update(over)
    return snap


class ReaderTest(SeatsBase):
    """`stopfacts.View`: every witness, alone, and what it may do."""

    def setUp(self):
        super().setUp()
        self.fresh_resident.off()
        self.ledger = os.path.join(self.tmp, "ledger.jsonl")
        with open(self.ledger, "wb") as f:
            f.write(b'{"id": "aaaa1111", "lane": "lane-a"}\n')

    def write(self, snap):
        pk.atomic_write(stopfacts.path(), json.dumps(snap))

    def ledger_head(self):
        st = os.stat(self.ledger)
        return {"path": self.ledger, "ino": st.st_ino, "size": st.st_size,
                "mtime_ns": st.st_mtime_ns, "offset": st.st_size,
                "unavailable": None}

    def test_no_file_is_ABSENT_with_its_reason(self):
        view = stopfacts.read()
        facts, fresh = view.lease("worktree:p:l")
        self.assertIsNone(facts)
        self.assertEqual(fresh.verdict, stopfacts.ABSENT)
        self.assertIn("helm web", fresh.reason)

    def test_a_file_over_the_hook_read_bound_is_ABSENT_unread(self):
        os.makedirs(os.path.dirname(stopfacts.path()), exist_ok=True)
        with open(stopfacts.path(), "w") as f:
            f.write(" " * (stopfacts.MAX_BYTES + 1))
        view = stopfacts.read()
        self.assertIsNone(view.snap)
        self.assertIn("over the", view.absent)

    def test_an_unknown_schema_is_ABSENT(self):
        self.write(_snapshot(schema=stopfacts.SCHEMA + 1))
        self.assertIn("schema", stopfacts.read().absent)

    def test_facts_past_the_hard_bound_are_ABSENT(self):
        self.write(_snapshot(written_at=time.time()
                             - stopfacts.HARD_AGE_S - 5))
        view = stopfacts.read()
        _f, fresh = view.lease("x")
        self.assertEqual(fresh.verdict, stopfacts.ABSENT)
        self.assertIn("past the %ds bound" % stopfacts.HARD_AGE_S,
                      fresh.reason)

    def test_other_code_is_STALE_never_EXACT(self):
        self.write(_snapshot(policy="0" * 32, ledger=self.ledger_head(),
                             leases={"dispatch:aaaa1111": {"ids": []}}))
        _f, fresh = stopfacts.read().lease("dispatch:aaaa1111")
        self.assertEqual(fresh.verdict, stopfacts.STALE)
        self.assertIn("other code", fresh.reason)

    def test_an_append_goes_STALE_only_for_the_facts_it_names(self):
        self.write(_snapshot(ledger=self.ledger_head(), leases={
            "worktree:p:lane-a": {"stem": "lane-a", "ids": []},
            "worktree:p:lane-b": {"stem": "lane-b", "ids": []}},
            seats={"alice": {"ids": []}, "bob": {"ids": []}}))
        # POSITIVE CONTROL: before the append both are EXACT.
        view = stopfacts.read()
        self.assertTrue(view.lease("worktree:p:lane-a")[1].exact)
        self.assertTrue(view.seat("bob")[1].exact)
        with open(self.ledger, "ab") as f:
            f.write(b'{"id": "bbbb2222", "lane": "LANE-A", '
                    b'"sender": "bob"}\n')
        view = stopfacts.read()
        a, b = view.lease("worktree:p:lane-a"), view.lease("worktree:p:lane-b")
        self.assertEqual(a[1].verdict, stopfacts.STALE)
        self.assertIn("naming lane-a", a[1].reason)
        self.assertTrue(b[1].exact, b[1])
        self.assertEqual(view.seat("bob")[1].verdict, stopfacts.STALE)
        self.assertTrue(view.seat("alice")[1].exact)

    def test_a_replaced_or_oversized_ledger_moves_everything(self):
        head = self.ledger_head()
        self.write(_snapshot(ledger=dict(head, ino=head["ino"] + 1),
                             leases={"x": {"stem": "zzz", "ids": []}}))
        _f, fresh = stopfacts.read().lease("x")
        self.assertEqual(fresh.verdict, stopfacts.STALE)
        self.assertIn("replaced", fresh.reason)
        self.write(_snapshot(ledger=head,
                             leases={"x": {"stem": "zzz", "ids": []}}))
        with open(self.ledger, "ab") as f:
            f.write(b"x" * (stopfacts.TAIL_CAP + 10) + b"\n")
        _f, fresh = stopfacts.read().lease("x")
        self.assertEqual(fresh.verdict, stopfacts.STALE)
        self.assertIn("past the", fresh.reason)

    def test_a_lease_missing_from_the_facts_is_ABSENT_for_that_lease(self):
        self.write(_snapshot(ledger=self.ledger_head()))
        view = stopfacts.read()
        self.assertIsNone(view.absent, "control: the snapshot itself reads")
        _f, fresh = view.lease("worktree:p:new")
        self.assertEqual(fresh.verdict, stopfacts.ABSENT)
        self.assertIn("after the resident's last refresh", fresh.reason)

    def test_a_seat_the_ledger_never_names_gets_the_unnamed_answer(self):
        unnamed = {"ids": [],
                   "spiral": [None, "no dispatch row is authored by %r"
                              % stopfacts.UNNAMED]}
        self.write(_snapshot(ledger=self.ledger_head(),
                             fleet={"unnamed": unnamed, "owed": {}}))
        view = stopfacts.read()
        self.assertEqual(view.owed_pair("carol"), ({}, None),
                         "control: an empty, READABLE frontier")
        info, err = view.spiral("carol")
        self.assertIsNone(info)
        self.assertIn("'carol'", err)
        self.assertNotIn("unnamed-seat", err)

    def test_the_owed_frontier_is_refused_when_an_append_names_it(self):
        row = {"id": "aaaa1111", "recipient": "bob", "status": "open"}
        self.write(_snapshot(ledger=self.ledger_head(),
                             fleet={"owed": {"aaaa1111": row}}))
        snap, why = stopfacts.read().owed_pair("alice")
        self.assertIsNone(why)
        self.assertEqual(snap, {"aaaa1111": row})
        with open(self.ledger, "ab") as f:
            f.write(b'{"id": "zzzz9999", "lane": "x", "recipient": "dan"}\n')
        self.assertIsNone(stopfacts.read().owed_pair("alice")[1],
                          "an append naming neither the seat nor an owed row "
                          "can only have ADDED rows")
        self.assertIsNotNone(stopfacts.read().owed_pair("dan")[1])
        with open(self.ledger, "ab") as f:
            f.write(b'{"id": "aaaa1111", "event": "verdict"}\n')
        snap, why = stopfacts.read().owed_pair("alice")
        self.assertEqual(snap, {})
        self.assertIn("naming aaaa1111", why)

    def test_owed_of_the_owed_frontier_is_the_frontier(self):
        """THE PROPERTY `owed_pair` RESTS ON, measured on the real function: a
        superseded parent and its live successor fold to the successor alone,
        and folding that answer again changes nothing."""
        parent = {"id": "p" * 32, "status": "open", "kind": "review",
                  "lane": "l", "chain_root": "p" * 32}
        child = {"id": "c" * 32, "status": "open", "kind": "review",
                 "lane": "l", "supersedes": "p" * 32, "chain_root": "p" * 32}
        other = {"id": "o" * 32, "status": "open", "kind": "build",
                 "lane": "m", "chain_root": "o" * 32}
        full = {r["id"]: r for r in (parent, child, other)}
        once = {r["id"]: r for r in dispatches.owed(full)}
        self.assertNotIn(parent["id"], once, "control: the parent is carried")
        twice = {r["id"]: r for r in dispatches.owed(once)}
        self.assertEqual(sorted(twice), sorted(once))

    def test_the_resident_and_the_reader_share_ONE_naming_rule(self):
        tail = b'{"id": "c", "lane": "Stop-Facts-3042-review-r2"}\n'.lower()
        self.assertEqual(stopfacts.names(tail, ["stop-facts-3042"]),
                         "stop-facts-3042")
        self.assertIsNone(stopfacts.names(tail, ["other-lane"]))
        self.assertIsNone(stopfacts.names(tail, ["ab"]),
                          "a two-character key would match everything")
        self.assertIsNone(stopfacts.names(b"", ["stop-facts-3042"]))

    def test_a_row_of_the_wrong_shape_is_judged_never_raised_on(self):
        """The one file the reader reads is another process's. A row it
        cannot index is ABSENT or STALE — the block stands — and never a
        raise: a raise leaves the claims rung, and `publish_failure` then
        ALLOWS the stop UNCHECKED."""
        self.write(_snapshot(ledger=self.ledger_head(), resident="x",
                             leases={"worktree:p:l": "junk"},
                             seats={"alice": 3}, fleet=[]))
        view = stopfacts.read()
        self.assertIsNone(view.absent, "control: the header is well-formed")
        self.assertFalse(view.resident_alive())
        facts, fresh = view.lease("worktree:p:l")
        self.assertIsNone(facts)
        self.assertEqual(fresh.verdict, stopfacts.ABSENT)
        facts, fresh = view.seat("alice")
        self.assertIsNone(facts)
        self.assertEqual(fresh.verdict, stopfacts.ABSENT)
        self.assertEqual(view.owed_pair("alice")[0], {})
        self.assertIsNone(view.spiral("alice")[0])
        self.assertIsInstance(view.headline(), str)
        # A LEDGER HEADER OF THE WRONG SHAPE cannot be witnessed: STALE, and
        # keys of the wrong shape name nothing.
        self.write(_snapshot(ledger=[], leases={"worktree:p:l": {
            "computed_at": time.time(), "stem": "l", "ids": 5}}))
        facts, fresh = stopfacts.read().lease("worktree:p:l")
        self.assertIsNotNone(facts)
        self.assertEqual(fresh.verdict, stopfacts.STALE)
        self.assertIn("cannot be witnessed", fresh.reason)

    def test_the_ledgers_own_fault_outranks_a_missing_frontier(self):
        """A fold that raised leaves no frontier, and the pair then carries
        the LEDGER's reason: the whisper reads a `stop-facts` prefix as "the
        ledger is fine, the resident is behind" and falls silent
        (`_whisper_candidates`), the wrong silence for an unreadable ledger."""
        self.write(_snapshot(ledger=self.ledger_head(),
                             fleet={"unavailable": "the dispatch ledger raised "
                                                   "ValueError", "owed": None}))
        snap, why = stopfacts.read().owed_pair("alice")
        self.assertEqual(snap, {})
        self.assertEqual(why, "the dispatch ledger raised ValueError")
        self.assertFalse(why.startswith("stop-facts"))

    def test_nothing_in_the_reader_can_wait_on_state(self):  # noqa: VACUOUS_ASSERTION — the same walker finding seats_common's sleep, asserted first, is the unconditional positive control on this observable
        """FAIL-CLOSED RUNGS NEVER WAIT ON STATE (owner rule, task/3042). The
        reader every Stop rung reads the facts through names no sleep at all,
        so no rung can wait on a reading through it: the one bounded wait it
        had is deleted, not switched off."""
        import ast
        import inspect
        from helm import seats_common

        def sleeps(module):
            tree = ast.parse(inspect.getsource(module))
            return [n.lineno for n in ast.walk(tree)
                    if (isinstance(n, ast.Attribute) and n.attr == "sleep")
                    or (isinstance(n, ast.Name) and n.id == "sleep")]

        self.assertTrue(sleeps(seats_common),
                        "control: the walker finds a module's sleep")
        self.assertEqual(sleeps(stopfacts), [])


class LaneWitnessTest(LaneWorld, SeatsBase):
    """The HEAD and trunk witnesses, on a real lane worktree."""

    def setUp(self):
        super().setUp()
        self.build_lane_world()
        self.resident_writes()
        self.fresh_resident.off()

    def test_the_resident_computed_the_gate_with_the_guards_own_function(self):
        facts, fresh = stopfacts.read().lease(self.RES)
        self.assertTrue(fresh.exact, fresh)
        self.assertEqual(facts["room"], self.wt)
        self.assertEqual(facts["head"], self.head)
        self.assertEqual(facts["gate"][0], self.row["id"])
        self.assertEqual(facts["gate"][2], "pending")

    def test_a_commit_in_the_lane_moves_the_HEAD_witness(self):  # noqa: VACUOUS_ASSERTION — the EXACT reading before the commit is the unconditional control
        self.assertTrue(stopfacts.read().lease(self.RES)[1].exact)
        self.git("commit", "-q", "--allow-empty", "-m", "more", cwd=self.wt)
        _f, fresh = stopfacts.read().lease(self.RES)
        self.assertEqual(fresh.verdict, stopfacts.STALE)
        self.assertEqual(fresh.reason, "HEAD moved since")

    def test_a_moved_trunk_ages_the_advice_and_not_the_verdict(self):
        self.assertIsNone(stopfacts.read().lease(self.RES)[1].advice)
        self.git("commit", "-q", "--allow-empty", "-m", "trunk moves")
        _f, fresh = stopfacts.read().lease(self.RES)
        self.assertTrue(fresh.exact, fresh)
        self.assertEqual(fresh.advice, "trunk moved since")
        self.assertIn("trunk moved since", fresh.note())

    def test_a_proof_not_bound_to_the_witnessed_HEAD_is_dropped(self):
        """THE RESIDENT BINDS POSITIVE PROOF TO THE SHA IT WITNESSED. A gate
        answer whose row ref is not the witnessed HEAD is not recorded, so no
        reading of the facts can exempt on it."""
        with mock.patch.object(stopfacts_resident, "head",
                               return_value=("f" * 40, "ref: x\x1floose:ff")):
            facts = stopfacts_resident.lease_facts(
                self.RES, {"repo": self.common}, *dispatches.snapshot())
        self.assertIsNone(facts["gate"])
        control = stopfacts_resident.lease_facts(
            self.RES, {"repo": self.common}, *dispatches.snapshot())
        self.assertEqual(control["gate"][0], self.row["id"])


class ResidentTest(LaneWorld, SeatsBase):
    """The writer: one of it, incremental, and never on other code."""

    def setUp(self):
        super().setUp()
        self.build_lane_world()
        self.fresh_resident.off()

    def leg(self):
        return stopfacts_resident.Leg()

    def test_ONE_writer_and_the_loser_never_writes(self):
        first, second = self.leg(), self.leg()
        self.addCleanup(first.release)
        self.addCleanup(second.release)
        self.assertTrue(first.writer())
        self.assertFalse(second.writer())
        got = second.refresh()
        self.assertFalse(got["written"])
        self.assertFalse(os.path.exists(stopfacts.path()))
        self.assertTrue(first.refresh()["written"])
        self.assertTrue(os.path.exists(stopfacts.path()))
        self.assertEqual(os.stat(stopfacts.path()).st_mode & 0o777, 0o600)

    def test_a_process_whose_code_was_replaced_does_not_write(self):  # noqa: VACUOUS_ASSERTION — the same leg's written refresh with the policy restored is the unconditional control
        leg = self.leg()
        self.addCleanup(leg.release)
        with mock.patch.object(stopfacts_resident, "LOADED_POLICY", "0" * 32):
            got = leg.refresh()
        self.assertFalse(got["written"])
        self.assertIn("replaced on disk", got["why"])
        self.assertFalse(os.path.exists(stopfacts.path()))
        self.assertTrue(leg.refresh()["written"])

    def test_an_append_recomputes_only_the_facts_it_names(self):
        first = stopfacts_resident.compute()
        stopfacts_resident.write(first)
        self.plant(lane="another-lane")
        again = stopfacts_resident.compute(prior=first)
        self.assertFalse(again["recomputed"]["full"])
        self.assertIs(again["leases"][self.RES], first["leases"][self.RES],
                      "a lease the append never named was recomputed")
        self.plant(lane="lane-g-review-r2", kind="build")
        third = stopfacts_resident.compute(prior=again)
        self.assertIsNot(third["leases"][self.RES], again["leases"][self.RES],
                         "a lease the append named was kept")

    def test_changed_kicks_a_read_inside_the_ttl(self):
        calls = []
        key = "stop-facts-changed-test"
        self.addCleanup(web_cache._drop, key)
        held, _box = web_cache._read_behind(key, 60, 600,
                                            lambda: calls.append(1) or 1)
        held.join(5)
        self.assertEqual(calls, [1])
        _t, box = web_cache._read_behind(key, 60, 600,
                                         lambda: calls.append(2) or 2,
                                         changed=lambda: False)
        self.assertEqual(calls, [1], "an unmoved input re-read inside ttl")
        self.assertEqual(box["got"], 1)
        web_cache._read_behind(key, 60, 600, lambda: calls.append(3) or 3,
                               changed=lambda: True)
        deadline = time.time() + 5
        while 3 not in calls and time.time() < deadline:
            time.sleep(0.01)
        self.assertIn(3, calls, "a moved input did not kick a read")

    def test_the_leg_runs_only_on_the_console_port_unless_told(self):
        from helm.web_common import DEFAULT_PORT
        self.assertTrue(stopfacts_resident.enabled(DEFAULT_PORT))
        self.assertFalse(stopfacts_resident.enabled(DEFAULT_PORT + 48))
        with mock.patch.dict(os.environ, {"HELM_STOP_FACTS_LEG": "1"}):
            self.assertTrue(stopfacts_resident.enabled(DEFAULT_PORT + 48))
        with mock.patch.dict(os.environ, {"HELM_STOP_FACTS_LEG": "0"}):
            self.assertFalse(stopfacts_resident.enabled(DEFAULT_PORT))

    def test_the_mechanical_job_honours_its_kill_switch(self):
        from helm import scratch, store
        with mock.patch.object(scratch, "auto_gc") as gc, \
                mock.patch.object(store, "index_cap") as cap, \
                mock.patch.dict(os.environ, {"HELM_STOP_GUARD_INDEX": "0"}):
            got = stopfacts_resident.mechanical()
        self.assertIn("skipped", got)
        gc.assert_not_called()
        cap.assert_not_called()
        with mock.patch.object(scratch, "auto_gc", return_value=None) as gc, \
                mock.patch.object(store, "index_cap") as cap:
            stopfacts_resident.mechanical()
        gc.assert_called_once_with()
        cap.assert_called_once_with(apply=True)


    # -- the re-exec onto a changed tree (task/3042 ruling 1) ---------------

    def test_a_commit_in_a_held_lane_is_recomputed_on_the_next_poll(self):
        """STALE OWN LEASE, the resident's cell: the seat's own commit moves an
        input the leg witnesses, and the next refresh recomputes that lease
        alone; the reader then reads it EXACT at the new HEAD."""
        leg = self.leg()
        self.addCleanup(leg.release)
        self.assertTrue(leg.refresh()["written"])
        # THE FIRST REFRESH WITNESSED NO LANE (it had no facts yet to name
        # one), so the leg's next poll refreshes once more and records the
        # lane's HEAD; from then on only a move counts.
        self.assertTrue(leg.refresh()["written"])
        self.assertTrue(stopfacts.read().lease(self.RES)[1].exact)
        leg.last_full = time.time()      # inside the minute: only a move counts
        self.assertFalse(leg.changed(), "control: nothing moved")
        self.git("commit", "-q", "--allow-empty", "-m", "own", cwd=self.wt)
        new_head = self.git("rev-parse", "HEAD", cwd=self.wt)
        self.assertEqual(stopfacts.read().lease(self.RES)[1].verdict,
                         stopfacts.STALE)
        self.assertTrue(leg.changed())
        got = leg.refresh()
        self.assertTrue(got["written"], got)
        self.assertEqual(got["recomputed"]["leases"], 1, got)
        facts, fresh = stopfacts.read().lease(self.RES)
        self.assertTrue(fresh.exact, fresh)
        self.assertEqual(facts["head"], new_head)
        self.assertIsNone(facts["gate"], "the review row is at the old HEAD")

    def test_a_changed_tree_re_execs_ONCE_with_this_processs_own_argv(self):  # noqa: VACUOUS_ASSERTION — assert_called_once_with after the loop is the unconditional positive on the same double the control polls assert quiet
        """A digest change causes exactly one exec, on this interpreter with
        this process's own command line (so the same port) and, being
        `os.execv`, this process's own environment."""
        leg = self.leg()
        self.addCleanup(leg.release)
        self.assertTrue(leg.writer())
        polls = (0.0, 0.4, 0.8, 1.2, 1.6, 5.0, 9.0)
        with mock.patch.object(stopfacts_resident, "_preflight",
                               return_value=None), \
                mock.patch.object(stopfacts_resident.os, "execv") as execv:
            for now in polls:
                self.assertIsNone(leg.code_moved(now=now),
                                  "control: the tree is the code it loaded")
            execv.assert_not_called()
            with mock.patch.object(stopfacts_resident, "LOADED_POLICY",
                                   "0" * 32):
                for now in polls:
                    moved = leg.code_moved(now=now)
                    if moved:
                        leg.reexec(moved)
        argv = [sys.executable] + list(getattr(sys, "orig_argv", None)[1:]
                                       if getattr(sys, "orig_argv", None)
                                       else sys.argv)
        execv.assert_called_once_with(sys.executable, argv)
        self.assertIsNone(leg._fd, "the writer lock was not released first")

    def test_the_poll_re_execs_before_it_kicks_a_refresh(self):
        leg = self.leg()
        calls = []
        with mock.patch.object(leg, "code_moved", return_value="d" * 32), \
                mock.patch.object(leg, "reexec", side_effect=lambda d:
                                  calls.append(("reexec", d))), \
                mock.patch.object(web_cache, "_read_behind",
                                  side_effect=lambda key, *_a, **_k:
                                  calls.append(("read", key))):
            stopfacts_resident.tick(leg)
        self.assertEqual(calls[0], ("reexec", "d" * 32), calls)

    def test_a_tree_that_does_not_import_is_never_exec_d_onto(self):  # noqa: VACUOUS_ASSERTION — the real import check answering None and the refusal naming SyntaxError are unconditional positives on the same call
        """A BROKEN TREE LEAVES THE CONSOLE SERVING. The import check runs in
        a child interpreter; a refusal is kept per digest, so it is not
        retried until the tree moves again, and the refused write says why."""
        import subprocess
        self.assertIsNone(stopfacts_resident._preflight(),
                          "control: this tree imports in a child interpreter")
        leg = self.leg()
        self.addCleanup(leg.release)
        self.assertTrue(leg.writer())
        broken = subprocess.CompletedProcess(
            [], 1, b"", b"Traceback (most recent call last):\n"
                        b"SyntaxError: invalid syntax\n")
        with mock.patch.object(stopfacts_resident.subprocess, "run",
                               return_value=broken), \
                mock.patch.object(stopfacts_resident.os, "execv") as execv, \
                mock.patch.object(stopfacts_resident, "LOADED_POLICY",
                                  "0" * 32):
            self.assertIsNone(leg.code_moved(now=0.0))
            moved = leg.code_moved(now=2.0)
            why = leg.reexec(moved)
            again = leg.code_moved(now=4.0)
            refused = leg.refresh()
        execv.assert_not_called()
        self.assertIn("does not import (SyntaxError: invalid syntax)", why)
        self.assertIsNone(again, "a refused digest was retried")
        self.assertFalse(refused["written"])
        self.assertIn("cannot re-exec onto the new tree", refused["why"])
        self.assertIsNotNone(leg._fd, "a refused re-exec gave up the lock")

    def test_a_snapshot_write_in_flight_finishes_before_the_exec(self):  # noqa: VACUOUS_ASSERTION — the exec double IS called once after the write is released, and the recorded state is asserted equal to a populated dict. noqa: ORPHANED_MOCK — the import check is reached through leg.reexec on its own thread, which the walker does not follow
        """RESIDENT MID-EXEC: the exec waits out a write already running, and
        at the moment of the exec the snapshot is on disk and the writer lock
        is free for the new image."""
        import fcntl
        import threading
        leg = self.leg()
        self.addCleanup(leg.release)
        self.assertTrue(leg.writer())
        snap = stopfacts_resident.compute()
        entered, go = threading.Event(), threading.Event()
        real_write = stopfacts_resident.write

        def slow_write(body, p=None):
            entered.set()
            go.wait(10)
            return real_write(body, p)

        seen = {}

        def execv(_path, _argv):
            seen["written"] = os.path.exists(stopfacts.path())
            fd = os.open(stopfacts.lock_path(), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen["lock_free"] = True
            except OSError:
                seen["lock_free"] = False
            finally:
                os.close(fd)

        with mock.patch.object(stopfacts_resident, "write",
                               side_effect=slow_write), \
                mock.patch.object(stopfacts_resident, "_preflight",
                                  return_value=None), \
                mock.patch.object(stopfacts_resident.os, "execv",
                                  side_effect=execv) as ex:
            writing = threading.Thread(target=leg._publish,
                                       kwargs={"snapshot": snap})
            writing.start()
            self.assertTrue(entered.wait(10), "the write never started")
            execing = threading.Thread(target=leg.reexec, args=("d" * 32,))
            execing.start()
            execing.join(0.3)
            self.assertTrue(execing.is_alive(),
                            "the exec did not wait for the write in flight")
            ex.assert_not_called()
            go.set()
            writing.join(10)
            execing.join(10)
        ex.assert_called_once()
        self.assertEqual(seen, {"written": True, "lock_free": True})
        self.assertIsNone(stopfacts.read().absent)

    def test_the_image_after_the_exec_marks_the_facts_refolding(self):
        """RESIDENT MID-EXEC, the reader's side: the new image keeps the facts
        it found but marks them refolding, so nothing is exempted on them
        until its first write, which clears the mark."""
        first = self.leg()
        self.assertTrue(first.refresh()["written"])
        first.release()
        image = self.leg()
        self.addCleanup(image.release)
        image.announce()
        view = stopfacts.read()
        self.assertIn("refolding", view.absent)
        self.assertEqual(view.lease(self.RES)[1].verdict, stopfacts.ABSENT)
        self.assertTrue(image.refresh()["written"])
        self.assertIsNone(stopfacts.read().absent)
        self.assertTrue(stopfacts.read().lease(self.RES)[1].exact)

    def test_a_second_resident_still_loses_the_lock_after_the_exec(self):  # noqa: VACUOUS_ASSERTION — the exec double is asserted called and the new image's writer() and refresh() are asserted True
        first, second = self.leg(), self.leg()
        self.addCleanup(first.release)
        self.addCleanup(second.release)
        self.assertTrue(first.writer())
        self.assertFalse(second.writer())
        with mock.patch.object(stopfacts_resident, "_preflight",
                               return_value=None), \
                mock.patch.object(stopfacts_resident.os, "execv") as execv:
            first.reexec("d" * 32)
        execv.assert_called_once()
        image = self.leg()                    # the same process, re-exec'd
        self.addCleanup(image.release)
        self.assertTrue(image.writer(), "the old image left the lock held")
        self.assertFalse(second.writer())
        self.assertFalse(second.refresh()["written"])
        self.assertTrue(image.refresh()["written"])

    def test_a_malformed_snapshot_on_disk_costs_a_recompute_not_a_wedge(self):
        """MALFORMED ROW, the resident's cell: the snapshot a leg finds at
        start is another process's, and a table of the wrong shape in it
        must not raise on every refresh until the process is restarted."""
        good = stopfacts_resident.compute()
        pk.atomic_write(stopfacts.path(), json.dumps(dict(
            good, leases="junk", seats=3, fleet=[], ledger="junk",
            seam={"roots": "junk"})))
        leg = self.leg()
        self.addCleanup(leg.release)
        leg.announce()
        self.assertEqual(leg.last["leases"], "junk",
                         "control: the leg took the malformed facts")
        self.assertTrue(leg.refresh()["written"])
        self.assertTrue(leg.seam_refresh()["written"])
        self.assertTrue(stopfacts.read().lease(self.RES)[1].exact)


class SurfaceByStateTest(LaneWorld, SeatsBase):
    """The lane table's cells, driven through the shipped stop and doctor."""

    def setUp(self):
        super().setUp()
        # THE TWO RUNGS THIS TABLE IS NOT ABOUT: the wiring rung walks this
        # checkout's own package and the seam rung has no peer lane here.
        # Both are restored below, in this class, so the restore is visible
        # where the set is (SeatsBase's own restore of WIRING is not).
        self._wiring_prior = os.environ.get("HELM_STOP_GUARD_WIRING")
        self._seam_prior = os.environ.get("HELM_STOP_GUARD_SEAM")
        os.environ["HELM_STOP_GUARD_WIRING"] = "0"
        os.environ["HELM_STOP_GUARD_SEAM"] = "0"
        self.build_lane_world()

    def tearDown(self):
        if self._wiring_prior is None:
            os.environ.pop("HELM_STOP_GUARD_WIRING", None)
        else:
            os.environ["HELM_STOP_GUARD_WIRING"] = self._wiring_prior
        if self._seam_prior is None:
            os.environ.pop("HELM_STOP_GUARD_SEAM", None)
        else:
            os.environ["HELM_STOP_GUARD_SEAM"] = self._seam_prior
        super().tearDown()

    def doctor_row(self):
        rows = doctor.check_stop_facts()
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def test_stop_guard_EXACT_facts_exempt_the_gated_lane(self):
        rc, _o, err = self.stop()
        self.assertEqual(rc, 0, err)
        self.assertIn("lane in gate", err)
        self.assertIn("lease retained", err)

    def test_stop_guard_a_STALE_lease_keeps_the_block_and_says_why(self):
        self.resident_writes()
        self.fresh_resident.off()
        rc, _o, err = self.stop()
        self.assertEqual(rc, 0, "control: the written facts exempt: %s" % err)
        self.git("commit", "-q", "--allow-empty", "-m", "new work", cwd=self.wt)
        for name in os.listdir(os.environ["HELM_CHAT_DIR"]):
            if seats.LEASE_LATCH in name:
                os.unlink(os.path.join(os.environ["HELM_CHAT_DIR"], name))
        rc, _o, err = self.stop()
        self.assertEqual(rc, 2, err)
        self.assertIn(self.RES, err)
        self.assertNotIn("lane in gate", err)
        self.assertIn("HEAD moved since", err)
        self.assertIn("stop-facts as of", err)

    def test_stop_guard_NO_resident_keeps_the_block_and_names_it(self):
        self.fresh_resident.off()
        rc, _o, err = self.stop()
        self.assertEqual(rc, 2, err)
        self.assertIn(self.RES, err)
        self.assertNotIn("lane in gate", err)
        self.assertIn("stop-facts ABSENT", err)

    def test_stop_guard_OTHER_CODE_keeps_the_block_and_names_it(self):
        self.resident_writes(policy="0" * 32)
        self.fresh_resident.off()
        rc, _o, err = self.stop()
        self.assertEqual(rc, 2, err)
        self.assertNotIn("lane in gate", err)
        self.assertIn("computed by other code", err)

    def test_stop_guard_an_append_naming_the_lane_keeps_the_block(self):
        self.resident_writes()
        self.fresh_resident.off()
        dispatches.mark_hold(self.row["id"], "waiting on a dependency")
        rc, _o, err = self.stop()
        self.assertEqual(rc, 2, err)
        self.assertIn("naming", err)
        self.assertNotIn("lane in gate", err)

    def test_stop_guard_an_append_elsewhere_leaves_the_lane_EXACT(self):
        self.resident_writes()
        self.fresh_resident.off()
        self.plant(lane="some-other-lane", kind="build")
        rc, _o, err = self.stop()
        self.assertEqual(rc, 0, err)
        self.assertIn("lane in gate", err)

    def test_doctor_OK_with_the_age(self):
        self.resident_writes()
        level, text = self.doctor_row()
        self.assertEqual(level, doctor.OK, text)
        self.assertIn("stop facts: stop-facts as of", text)

    def test_doctor_WARN_when_the_resident_is_behind(self):
        self.resident_writes(written_at=time.time() - 120)
        self.fresh_resident.off()
        self.plant(lane="some-other-lane", kind="build")
        level, text = self.doctor_row()
        self.assertEqual(level, doctor.WARN, text)
        self.assertIn("behind the dispatch ledger", text)

    def test_doctor_FAIL_with_the_cure_when_ABSENT_or_other_code(self):
        from helm import webserve
        self.fresh_resident.off()
        # A HOME NO CONSOLE SERVES is told, not failed (the board rungs' law)
        # — and a console the process table shows but THIS home's registry
        # does not (another home's: a test's, a second install's) writes its
        # facts into that home, so it does not make this one's absence a FAIL.
        other = {"pid": 4242, "port": 8766, "registered": False}
        with mock.patch.object(webserve, "live", return_value={
                "servers": [other], "count": 1}):
            level, text = self.doctor_row()
        self.assertEqual(level, doctor.WARN, text)
        self.assertIn("no `helm web` serves this home", text)
        # A CONSOLE REGISTERED HERE THAT WRITES NOTHING is the resident broken.
        ours = dict(other, pid=4343, registered=True)
        with mock.patch.object(webserve, "live", return_value={
                "servers": [other, ours], "count": 2}):
            level, text = self.doctor_row()
        self.assertEqual(level, doctor.FAIL, text)
        self.assertIn("systemctl --user restart helm-web", text)
        # OTHER CODE: facts a resident on another checkout computed.
        self.resident_writes(policy="0" * 32, resident=dict(
            stopfacts_resident._resident(), code_root="/elsewhere/helm"))
        level, text = self.doctor_row()
        self.assertEqual(level, doctor.FAIL, text)
        self.assertIn("other code", text)
        self.assertIn("systemctl --user restart helm-web", text)


    # -- the rest of the surface x state matrix (task/3042 round 2) --------

    def claims(self):
        """One claims rung, alone, as the stop runs it."""
        from helm import seats_stop_claims
        blocks, warns = [], []
        seats_stop_claims.claims_rung(self.sid, "stop-facts-room", self.SEAT,
                                      blocks=blocks, warns=warns)
        return blocks, warns

    def unlatch(self):
        for name in os.listdir(os.environ["HELM_CHAT_DIR"]):
            if seats.LEASE_LATCH in name:
                os.unlink(os.path.join(os.environ["HELM_CHAT_DIR"], name))

    def test_claims_rung_a_STALE_own_lease_answers_at_once_and_never_sleeps(self):  # noqa: VACUOUS_ASSERTION — the held lease, its HEAD-moved reason and the snapshot age are asserted present on the same output the no-sleep double watched
        """RULING (task/3042): a fail-closed rung never waits on state. The
        seat's own commit a moment before its stop, with the resident that
        wrote the facts alive and behind — the exact case the deleted wait
        waited on — answers from ONE read with no sleep, holds the lease, and
        names the snapshot's age."""
        self.resident_writes()
        self.fresh_resident.off()
        blocks, warns = self.claims()
        self.assertIn("lane in gate", "\n".join(warns),
                      "control: the written facts exempt the gated lane")
        self.git("commit", "-q", "--allow-empty", "-m", "own", cwd=self.wt)
        self.unlatch()
        view = stopfacts.read()
        self.assertTrue(view.resident_alive(),
                        "control: the writer is alive, the case a wait took")
        self.assertEqual(view.lease(self.RES)[1].verdict, stopfacts.STALE)
        reads = []
        real = stopfacts.load
        with mock.patch.object(stopfacts, "load", side_effect=lambda p=None:
                               reads.append(p) or real(p)), \
                mock.patch("time.sleep") as slept:
            blocks, warns = self.claims()
        slept.assert_not_called()
        self.assertEqual(len(reads), 1, reads)
        held = "\n".join(blocks)
        self.assertIn(self.RES, held)
        self.assertNotIn("lane in gate", held + "\n".join(warns))
        self.assertIn("HEAD moved since", held)
        self.assertRegex(held, r"stop-facts as of \d+s")

    def test_claims_rung_a_resident_mid_exec_keeps_the_block(self):  # noqa: VACUOUS_ASSERTION — the held lease and the refolding reason are asserted present on the same output
        """RESIDENT MID-EXEC: the image after a re-exec marks the facts it
        found refolding; they are the older code's, and nothing is exempted
        on them."""
        self.resident_writes(policy="0" * 32)
        self.fresh_resident.off()
        image = stopfacts_resident.Leg()
        self.addCleanup(image.release)
        image.announce()
        with mock.patch("time.sleep") as slept:
            blocks, warns = self.claims()
        slept.assert_not_called()
        held = "\n".join(blocks)
        self.assertIn(self.RES, held)
        self.assertNotIn("lane in gate", held + "\n".join(warns))
        self.assertIn("refolding", held)

    def test_claims_rung_a_malformed_row_keeps_the_block_and_never_raises(self):  # noqa: VACUOUS_ASSERTION — the real three-part proof is asserted before the loop, and every subTest asserts the held lease and its reason present
        snap = self.resident_writes()
        self.fresh_resident.off()
        facts = snap["leases"][self.RES]
        self.assertEqual(len(facts["gate"]), 3, "control: a real proof")
        for label, row, says in (
                ("not a table", "junk", "stop-facts ABSENT"),
                ("fields of the wrong shape",
                 dict(facts, gate=facts["gate"][:1], findings="junk"),
                 "carry no reading of this room")):
            with self.subTest(label):
                self.unlatch()
                pk.atomic_write(stopfacts.path(), json.dumps(
                    dict(snap, leases={self.RES: row})))
                blocks, warns = self.claims()
                held = "\n".join(blocks)
                self.assertIn(self.RES, held)
                self.assertNotIn("lane in gate", held + "\n".join(warns))
                self.assertIn(says, held)

    def test_doctor_stays_OK_when_a_held_lane_commits(self):  # noqa: VACUOUS_ASSERTION — the STALE lease verdict is asserted first and the OK level is itself a positive reading
        """STALE OWN LEASE, doctor's cell: a lane HEAD move is the resident's
        next poll, not a fault, so doctor does not alarm on it."""
        self.resident_writes()
        self.fresh_resident.off()
        self.git("commit", "-q", "--allow-empty", "-m", "own", cwd=self.wt)
        self.assertEqual(stopfacts.read().lease(self.RES)[1].verdict,
                         stopfacts.STALE, "control: the lease is STALE")
        level, text = self.doctor_row()
        self.assertEqual(level, doctor.OK, text)

    def test_doctor_names_a_resident_whose_loaded_code_is_older_than_the_tree(self):
        now = time.time()
        res = dict(stopfacts_resident._resident(), started_at=now - 600)
        self.resident_writes(policy="0" * 32, resident=res)
        self.fresh_resident.off()
        with mock.patch.object(stopfacts, "source_newest",
                               return_value=now - 5):
            level, text = self.doctor_row()
        self.assertEqual(level, doctor.WARN, text)
        self.assertIn("resident pid %d loaded its code at" % os.getpid(), text)
        self.assertIn("older than this tree", text)
        self.assertIn("re-execs onto the tree", text)
        with mock.patch.object(stopfacts, "source_newest", return_value=(
                now - stopfacts_resident.REEXEC_WITHIN_S - 30)):
            level, text = self.doctor_row()
        self.assertEqual(level, doctor.FAIL, text)
        self.assertIn("older than this tree", text)
        self.assertIn("has not re-exec'd onto it", text)
        # A RESIDENT ON ANOTHER CHECKOUT is a different fault, said as such.
        self.resident_writes(policy="0" * 32,
                             resident=dict(res, code_root="/elsewhere/helm"))
        with mock.patch.object(stopfacts, "source_newest",
                               return_value=now - 5):
            level, text = self.doctor_row()
        self.assertEqual(level, doctor.FAIL, text)
        self.assertIn("runs /elsewhere/helm, not this tree", text)

    def test_doctor_WARNs_on_a_resident_mid_exec_and_FAILs_a_dead_one(self):
        self.resident_writes(policy="0" * 32)   # the code before the land
        self.fresh_resident.off()
        image = stopfacts_resident.Leg()
        self.addCleanup(image.release)
        image.announce()
        level, text = self.doctor_row()
        self.assertEqual(level, doctor.WARN, text)
        self.assertIn("refolding", text)
        snap, _why = stopfacts.load()
        snap["resident"] = dict(snap["resident"],
                                starttime=snap["resident"]["starttime"] + 1)
        pk.atomic_write(stopfacts.path(), json.dumps(snap))
        level, text = self.doctor_row()
        self.assertEqual(level, doctor.FAIL, text)
        self.assertIn("not running", text)

    def test_doctor_reads_a_malformed_snapshot_without_raising(self):
        snap = self.resident_writes()
        self.fresh_resident.off()
        pk.atomic_write(stopfacts.path(), json.dumps(
            dict(snap, leases=5, seats="junk")))
        level, text = self.doctor_row()
        self.assertEqual(level, doctor.OK, text)
        self.assertIn("0 lease(s), 0 seat(s)", text)

    def whisper(self, pair):
        """(what the whisper's dispatch rung was asked with, fingerprints)."""
        from helm import seats_work_offer
        real = seats_work_offer._dispatch_candidate
        with mock.patch.object(seats_work_offer, "_dispatch_candidate",
                               side_effect=real) as asked:
            fps = [c[0] for c in seats_work_offer._whisper_candidates(
                self.sid, self.SEAT, [], False, dispatch_snapshot=pair)]
        return [c.args[0] for c in asked.call_args_list], fps

    def test_whisper_reads_the_owed_frontier_only_from_EXACT_facts(self):  # noqa: VACUOUS_ASSERTION — the fresh, post-commit and ledger-fault cells assert the rung WAS asked with the pair, on the same double the quiet cells assert unasked
        """THE WHISPER'S ROW: only an EXACT frontier reaches its dispatch
        rung; every other state leaves the rung unasked and says nothing about
        the ledger (the claims footer and doctor name the resident) — while
        the ledger's OWN fault still reaches the seat as a ledger fault."""
        self.resident_writes()
        self.fresh_resident.off()
        pair = stopfacts.read().owed_pair(self.SEAT)
        self.assertIsNone(pair[1], pair)
        self.assertIn(self.row["id"], pair[0])
        self.assertEqual(self.whisper(pair)[0], [pair], "fresh")
        # POST-COMMIT: a lane HEAD move is not a ledger move.
        self.git("commit", "-q", "--allow-empty", "-m", "own", cwd=self.wt)
        pair = stopfacts.read().owed_pair(self.SEAT)
        self.assertEqual(self.whisper(pair)[0], [pair], "post-commit")

        def announce():
            self.resident_writes(policy="0" * 32)
            image = stopfacts_resident.Leg()
            image.announce()
            image.release()

        snap = self.resident_writes()
        for label, make in (
                ("an append naming an owed row",
                 lambda: dispatches.mark_hold(self.row["id"], "held")),
                ("absent", lambda: os.unlink(stopfacts.path())),
                ("older code", lambda: self.resident_writes(
                    policy="0" * 32)),
                ("resident mid-exec", announce),
                ("malformed", lambda: pk.atomic_write(
                    stopfacts.path(), json.dumps(dict(snap, fleet=[]))))):
            with self.subTest(label):
                self.resident_writes()
                make()
                pair = stopfacts.read().owed_pair(self.SEAT)
                self.assertTrue(str(pair[1]).startswith("stop-facts"), pair)
                asked, fps = self.whisper(pair)
                self.assertEqual(asked, [])
                self.assertNotIn("dispatch:ledger-unavailable", fps)
        pk.atomic_write(stopfacts.path(), json.dumps(dict(
            self.resident_writes(), fleet={"unavailable": "the dispatch "
                                           "ledger raised ValueError",
                                           "owed": None})))
        pair = stopfacts.read().owed_pair(self.SEAT)
        asked, fps = self.whisper(pair)
        self.assertEqual(asked, [pair], "the ledger's own fault")
        self.assertIn("dispatch:ledger-unavailable", fps)


class HookClockTest(SeatsBase):
    """HELM_HOOK_T0: the budget is measured from the wrapper's start. Hermetic
    through SeatsBase, because a dispatched handler and a published stop both
    write their records under the helm home."""

    def test_elapsed_is_read_against_proc_uptime(self):
        from helm import procage
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        got = procage.hook_elapsed({"HELM_HOOK_T0": "%.2f" % (up - 2.5)})
        self.assertIsNotNone(got)
        self.assertGreaterEqual(got, 2.4)
        self.assertLess(got, 60)

    def test_absent_future_or_ancient_starts_are_not_charged(self):  # noqa: VACUOUS_ASSERTION — the sibling arm's numeric reading through the same function is the unconditional control
        from helm import procage
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        for env in ({}, {"HELM_HOOK_T0": "junk"},
                    {"HELM_HOOK_T0": "%.2f" % (up + 100)},
                    {"HELM_HOOK_T0": "%.2f" % (up - 7200)}):
            with self.subTest(env=env):
                self.assertIsNone(procage.hook_elapsed(env))

    def test_the_wrapper_exports_the_start_to_its_child(self):
        import subprocess
        wrapper = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "bin", "helm-hook")
        r = subprocess.run(
            [wrapper, "lane", "t0-probe", "Stop", "5", "-", "/bin/sh", "-c",
             "printf '%s' \"$HELM_HOOK_T0\""],
            capture_output=True, text=True, timeout=30,
            env=dict(os.environ, HELM_HOOK_T0="stale"))
        self.assertEqual(r.returncode, 0, r.stderr)
        t0 = float(r.stdout)
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        self.assertLessEqual(t0, up)
        self.assertGreater(t0, up - 30)

    def test_the_handler_alarm_is_charged_from_the_hook_start(self):
        """A dispatch that begins after its whole budget was spent in startup
        is cut at once, and says UNCHECKED — not granted the full budget the
        outer `timeout` no longer has."""
        from helm import hookrun
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        spec = {"name": "sleepy", "event": "Stop", "args": "noop",
                "timeout": 3}
        seen = {}

        def main(_args):
            seen["alarm"] = __import__("signal").getitimer(
                __import__("signal").ITIMER_REAL)[0]
            return 0

        os.environ["HELM_HOOK_T0"] = "%.2f" % (up - 2.0)
        try:
            with mock.patch("helm.cli.main", side_effect=main), \
                    mock.patch.object(hookrun, "event_specs",
                                      return_value=[spec]):
                hookrun.run_event("Stop", payload="{}", specs=[spec])
        finally:
            os.environ.pop("HELM_HOOK_T0", None)
        self.assertGreater(seen["alarm"], 0)
        self.assertLess(seen["alarm"], 1.5,
                        "the alarm ignored the 2s already spent: %r" % seen)
        with mock.patch("helm.cli.main", side_effect=main), \
                mock.patch.object(hookrun, "event_specs",
                                  return_value=[spec]):
            hookrun.run_event("Stop", payload="{}", specs=[spec])
        self.assertGreater(seen["alarm"], 2.5,
                           "control: without a start the budget is whole")

    def test_the_stop_ladder_deadline_is_charged_from_the_hook_start(self):
        from helm import projscope, seats_cli, seats_stop_budget
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        seen = {}

        def guard(**_kw):
            seen["left"] = projscope.remaining()
            return [], []

        os.environ["HELM_HOOK_T0"] = "%.2f" % (up - 5.0)
        try:
            with mock.patch.object(seats_cli, "stop_guard",
                                   side_effect=guard):
                rc, _out, _err = self.cmd("stop-guard", ["--hook-json"],
                                          stdin=b"{}")
        finally:
            os.environ.pop("HELM_HOOK_T0", None)
        self.assertEqual(rc, 0)
        self.assertLess(seen["left"], seats_stop_budget.BUDGET_S - 4.5)
        self.assertGreater(seen["left"], seats_stop_budget.BUDGET_S - 7.0)
        with mock.patch.object(seats_cli, "stop_guard", side_effect=guard):
            self.cmd("stop-guard", ["--hook-json"], stdin=b"{}")
        self.assertGreater(seen["left"], seats_stop_budget.BUDGET_S - 1.0,
                           "control: without a start the ladder is whole")


if __name__ == "__main__":
    unittest.main()
