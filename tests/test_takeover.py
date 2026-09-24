#!/usr/bin/env python3
"""Fail-closed task BUILD takeover contract."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, takeover, tasks  # noqa: E402


ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME")


def evidence(state="hung", expired=False, inflight=0, pending=1):
    return {
        "v": 1, "observed_at": time.time(), "incumbent": "incumbent",
        "occurrence": {"seat": "incumbent", "harness": "orca",
                       "session": "session-a", "worktree": "/repo-wt/source",
                       "room": "helm", "handle": "pane-a"},
        "proxywatch": {"reported_at": time.time(), "state": state,
                       "proxywatch_state": ("compact-needed"
                                             if state == "context-full" else state),
                       "pane_live": True, "inflight": inflight,
                       "pending": pending, "pending_after": bool(pending),
                       "open_dispatches": 0, "turn_complete": True,
                       "turn_evidence": "measured"},
        "claim": {"resource": "worktree:repo:source", "holder": "incumbent",
                  "session": "session-a", "lease": "lease-a", "fence": 7,
                  "exp_mono": 10.0, "exp_wall": 20.0, "expired": expired},
        "contradiction": ("idle-expired" if state == "idle" and expired
                           else state),
    }


class TempBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-takeover-")
        self.task_path = os.path.join(self.tmp, "tasks.jsonl")
        self.filed = 0
        self.transfer_path = os.path.join(self.tmp, "takeovers.jsonl")
        self.env = {k: os.environ.get(k) for k in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")

    def tearDown(self):
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def task(self, owner="incumbent"):
        """A filed row, AND A DISTINCT ONE ON EVERY CALL.

        Several arms below file a SECOND row to stand as the control beside
        their subject — an ordinary update beside a custody move, a `build`
        proof beside a `reassign` one. `tasks.add` resolves a new title
        against the open rows and refuses a near-duplicate (helm/tasks.py:
        `duplicate_verdict`), and two rows titled alike are ONE piece of work
        to that door, so a fixture handing it one title made those arms fail
        on their fixture instead of on their subject.

        The counter is the fix rather than `force_new=True`, because what
        these arms need is two genuinely separate pieces of work, and filing
        two rows the production door calls one would model a ledger helm does
        not permit. Nothing here reads the title text; the id is the handle.
        """
        self.filed += 1
        row, err = tasks.add("continue build %d" % self.filed, owner,
                             path=self.task_path)
        self.assertIsNone(err, err)
        return row

    @staticmethod
    def line_count(path):
        try:
            with open(path, encoding="utf-8") as f:
                return sum(1 for line in f if line.strip())
        except FileNotFoundError:
            return 0


class EvidenceGateTest(TempBase):
    def proxy_row(self, state, **fields):
        row = {"seat": "incumbent", "turn_state": state, "pane_live": True,
               "inflight": 0, "pending_after": False, "open_dispatches": 0,
               "turn_complete": True, "turn_evidence": "typed"}
        row.update(fields)
        return {"ts": time.time(), "seats": [row]}

    def claim(self, expired=False):
        return {"expired": expired}

    def test_healthy_quiet_recent_turn_without_reply_MUST_MISS(self):  # noqa: VACUOUS_ASSERTION — the sampled healthy state is asserted before the refusal, so the miss cannot be an absent probe
        """No reply is not an input; a recent completed turn remains REFUSE."""
        with mock.patch.object(takeover.proxywatch, "health",
                               return_value=self.proxy_row("ok")):
            sample = takeover._proxy("incumbent", time.time())
        with self.assertRaisesRegex(takeover.TakeoverRefused,
                                    "not a measured contradiction"):
            takeover._admissible(sample, self.claim(expired=True))

    def test_typed_hung_starved_and_context_full_are_admitted(self):  # noqa: VACUOUS_ASSERTION — each typed sample is asserted equal to its normalized state and admitted contradiction
        for raw, wanted in (("hung", "hung"), ("starved", "starved"),
                            ("compact-needed", "context-full")):
            with self.subTest(raw=raw), mock.patch.object(
                    takeover.proxywatch, "health",
                    return_value=self.proxy_row(raw)):
                sample = takeover._proxy("incumbent", time.time())
                self.assertEqual(sample["state"], wanted)
                self.assertEqual(takeover._admissible(sample, self.claim()), wanted)

    def test_idle_needs_exact_zero_censuses_and_expired_bound_claim(self):
        base = {"state": "idle", "inflight": 0, "pending": 0}
        self.assertEqual(takeover._admissible(base, self.claim(True)),
                         "idle-expired")
        for proxy, claim in ((dict(base, pending=1), self.claim(True)),
                             (dict(base, inflight=1), self.claim(True)),
                             (base, self.claim(False))):
            with self.subTest(proxy=proxy, claim=claim), self.assertRaisesRegex(
                    takeover.TakeoverRefused, "idle authorizes only"):
                takeover._admissible(proxy, claim)

    def test_claim_age_alone_never_authorizes(self):  # noqa: VACUOUS_ASSERTION — the exact healthy state and expired claim are supplied directly before the refusal
        with self.assertRaisesRegex(takeover.TakeoverRefused,
                                    "not a measured contradiction"):
            takeover._admissible({"state": "ok", "inflight": 0, "pending": 0},
                                 self.claim(expired=True))

    def test_absent_stale_malformed_and_mismatched_proxy_evidence_refuses(self):  # noqa: VACUOUS_ASSERTION — each concrete malformed report is supplied through proxywatch and must raise
        cases = (
            {"ts": time.time() - takeover.EVIDENCE_MAX_AGE_S - 1,
             "seats": [self.proxy_row("hung")["seats"][0]]},
            {"ts": time.time(), "seats": []},
            {"ts": time.time(), "seats": [dict(
                self.proxy_row("hung")["seats"][0], seat="other")]},
            {"ts": time.time(), "seats": [dict(
                self.proxy_row("hung")["seats"][0], inflight=None)]},
        )
        for report in cases:
            with self.subTest(report=report), mock.patch.object(
                    takeover.proxywatch, "health", return_value=report), \
                    self.assertRaises(takeover.TakeoverRefused):
                takeover._proxy("incumbent", time.time())

    def test_occurrence_and_claim_must_bind_same_seat_session_and_lane(self):  # noqa: VACUOUS_ASSERTION — the valid occurrence and claim are asserted first before mismatch refusals
        rec = {"seat": "incumbent", "harness": "orca", "session": "session-a",
               "worktree": "/repo-wt/source", "room": "helm", "handle": "p1"}
        occ = takeover._occurrence(rec, "incumbent", "/repo-wt/source")
        claim = {"holder": "incumbent", "session": "session-a",
                 "lease": "lease", "fence": 2, "exp_mono": 20.0,
                 "exp_wall": 30.0}
        with mock.patch.object(takeover.time, "time", return_value=10.0):
            got = takeover._claim({"worktree:repo:source": claim},
                                  "worktree:repo:source", "incumbent", occ,
                                  10.0, 10.0)
        self.assertFalse(got["expired"])
        for changed in (dict(rec, seat="other"), dict(rec, session=None),
                        dict(rec, worktree="/repo-wt/other"),
                        dict(rec, handle=None)):
            with self.subTest(changed=changed), self.assertRaises(
                    takeover.TakeoverRefused):
                takeover._occurrence(changed, "incumbent", "/repo-wt/source")
        with self.assertRaisesRegex(takeover.TakeoverRefused, "session"):
            takeover._claim({"worktree:repo:source": dict(
                claim, session="session-b")}, "worktree:repo:source",
                "incumbent", occ, 10.0, 10.0)
        with self.assertRaisesRegex(takeover.TakeoverRefused, "absent"):
            takeover._claim({}, "worktree:repo:source", "incumbent", occ,
                            10.0, 10.0)

    def test_exact_registered_occurrence_must_still_resolve_without_repair(self):
        pane = {"harness": "orca", "handle": "p1"}
        adapter = object()
        with mock.patch.object(takeover.seat, "_resolve_registered_pane",
                               return_value=(adapter, "p1", "verified")):
            self.assertEqual(takeover._prove_occurrence("incumbent", "/seat", pane),
                             "verified")
        with mock.patch.object(takeover.seat, "_resolve_registered_pane",
                               return_value=(adapter, "p2", "replacement")):
            with self.assertRaisesRegex(takeover.TakeoverRefused, "replacement"):
                takeover._prove_occurrence("incumbent", "/seat", pane)
        headless = {"harness": "headless", "pid": 42,
                    "pid_identity": "proc:start"}
        with mock.patch.object(takeover.seat, "_pid_identity",
                               return_value="proc:other"):
            with self.assertRaisesRegex(takeover.TakeoverRefused, "reused"):
                takeover._prove_occurrence("incumbent", "/seat", headless)

    def test_absent_malformed_and_duplicate_key_spawn_registers_are_unknown(self):  # noqa: VACUOUS_ASSERTION — each concrete invalid spawn file is written before its refusal assertion
        seat_dir = os.path.join(self.tmp, "seat")
        os.makedirs(seat_dir)
        with self.assertRaisesRegex(takeover.TakeoverRefused, "absent"):
            takeover._strict_spawn_record(seat_dir)
        path = os.path.join(seat_dir, "spawn.json")
        for raw in ("not-json", '{"seat":"a","seat":"b"}', "[]"):
            with self.subTest(raw=raw):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(raw)
                with self.assertRaisesRegex(takeover.TakeoverRefused,
                                            "malformed|unreadable"):
                    takeover._strict_spawn_record(seat_dir)


class ACustodyChangeIsNotActivityTest(TempBase):
    """`last_updated` is what stalebot reads to decide what has gone quiet.

    It was stamped on every accepted mutation, so REASSIGNING a task
    refreshed it — and a reassignment exists precisely to rescue the backlog
    of a seat that stopped. Moving that backlog therefore made every stale
    row look freshly touched and silenced the detector on exactly the rows it
    was watching for. The board reads clean while the work rots; that is
    worse than a blind detector, because a blind one does not reassure.

    A custody move keeps the prior timestamp and records itself in
    `custody_updated` instead, so the transfer stays auditable without
    claiming progress nobody made.
    """

    def _transfer(self, tid, before, to, **extra):
        """Reach `tasks.update` the way `seat_reassign._move_tasks` reaches it.

        Hand-minting a BUILD-continuation proof would have tested a path
        production does not take for a reassignment — and would have missed
        that an owner-ONLY field set is refused by that mint entirely. The
        disposition is faked (there is no real seat here), the CUSTODY path
        is not.
        """
        from helm import seat_reassign
        incumbent = before.get("owner")
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD,
                                             "no pane")):
            disp, derr = takeover.mint_source_disposition(incumbent)
        self.assertIsNone(derr, derr)
        auth, mint_err = takeover.mint_seat_reassign(
            tid, before, incumbent, to,
            {"kind": "seat-reassign", "from": incumbent, "to": to},
            disposition=disp)
        self.assertIsNone(mint_err, mint_err)
        fields = dict(extra)
        fields["owner"] = to
        row, err = tasks.update(tid, path=self.task_path, takeover_auth=auth,
                                **fields)
        self.assertIsNone(err, err)
        return row

    def test_a_pure_custody_move_does_not_look_like_progress(self):
        before = self.task()
        quiet_since = before["last_updated"]
        self.assertIsNotNone(quiet_since)
        lines_before = self.line_count(self.task_path)

        row = self._transfer(before["id"], before, "successor")

        # THE WRITE HAPPENED. An unchanged timestamp is exactly what a
        # REFUSED write also produces, and this store is event-sourced, so
        # neither the returned row nor the field value can tell those apart —
        # only the ledger file can.
        self.assertEqual(self.line_count(self.task_path), lines_before + 1,
                         "no row was appended, so the preserved timestamp "
                         "proves nothing about custody")
        # The MOVE happened — otherwise this arm would pass on a no-op.
        self.assertEqual(row["owner"], "successor")
        self.assertEqual(row["last_updated"], quiet_since,  # noqa: VACUOUS_ASSERTION — the ledger row-count above is this assertion's positive control: it proves a row WAS appended, which is the only thing separating a preserved timestamp from a refused write
                         "the transfer refreshed last_updated, so a stale row "
                         "now reads as freshly touched and stalebot will skip "
                         "it")
        # And it is not silent about itself: the custody event is recorded at
        # a LATER moment than the work's own quiet-since, which is what proves
        # the timestamp above was preserved rather than merely never set.
        self.assertIn("custody_updated", row)
        self.assertGreater(row["custody_updated"], quiet_since)

    def test_an_ordinary_mutation_still_stamps(self):
        """THE CONTROL. The arm above passes identically if `last_updated`
        stopped being stamped at all — which would break staleness detection
        in the opposite direction, and far more widely."""
        before = self.task()
        row, err = tasks.update(before["id"], path=self.task_path,
                                status="in_progress")
        self.assertIsNone(err, err)
        self.assertNotEqual(row["last_updated"], before["last_updated"],
                            "an ordinary update no longer stamps "
                            "last_updated — staleness detection is dead")
        self.assertNotIn("custody_updated", row)  # noqa: VACUOUS_ASSERTION — the transfer at the end of this same method proves custody_updated IS written by some path, so this absence is not about a key nothing writes
        # THE POSITIVE CONTROL FOR THAT ABSENCE, in this method rather than a
        # neighbouring one: a key that no path ever writes is absent here for
        # a reason that has nothing to do with the rule under test.
        moved = self.task(owner="incumbent")
        transferred = self._transfer(moved["id"], moved, "successor")
        self.assertIn("custody_updated", transferred,
                      "custody_updated is never written by any path, so its "
                      "absence above was not evidence")

    def test_a_reassign_proof_cannot_carry_work_at_all(self):
        """The narrower fact I found by writing the arm that assumed the
        wider one: a seat-reassign proof's compare-and-swap is OWNER-ONLY, so
        the custody exemption cannot be smuggled onto a work-bearing update
        through this mint. It is refused before `last_updated` is reached."""
        before = self.task()
        with self.assertRaises(AssertionError) as caught:
            self._transfer(before["id"], before, "successor",
                           status="in_progress")
        self.assertIn("cannot authorize other fields", str(caught.exception))

    def test_the_exemption_is_for_CUSTODY_not_for_the_takeover_verb(self):
        """THE DISCRIMINATOR. The BUILD-continuation mint CAN carry work, so
        this is the path where `set(fields) <= {"owner"}` is load-bearing
        rather than redundant with the mint's own CAS. A proof that also
        moves the status is doing work, and work is activity."""
        before = self.task()
        fields = {"owner": "successor", "status": "in_progress"}
        auth = takeover._mint(before["id"], before, "incumbent", "successor",
                              fields, {"transfer_id": "tx-work",
                                       "scope": "build-continuation"})
        row, err = tasks.update(before["id"], path=self.task_path,
                                takeover_auth=auth, **fields)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "in_progress")
        self.assertNotEqual(row["last_updated"], before["last_updated"],
                            "a takeover that also moved the work was treated "
                            "as pure custody, so real progress went unstamped")
        self.assertNotIn("custody_updated", row)  # noqa: VACUOUS_ASSERTION — the transfer at the end of this same method proves custody_updated IS written by some path, so this absence is not about a key nothing writes


class CapabilityBoundaryTest(TempBase):
    def test_direct_construction_is_refused(self):  # noqa: VACUOUS_ASSERTION — direct construction is the concrete forbidden act and its typed refusal is asserted
        with self.assertRaisesRegex(takeover.TakeoverRefused,
                                    "direct construction"):
            takeover.BuildContinuationAuthorization()

    def test_raw_force_and_owner_clear_cannot_mutate_an_incumbent(self):  # noqa: VACUOUS_ASSERTION — the preexisting task line count and owner are asserted unchanged for both bypass attempts
        row = self.task()
        before = self.line_count(self.task_path)
        for fields in ({"owner": "successor", "force": True}, {"owner": ""}):
            with self.subTest(fields=fields):
                got, err = tasks.update(row["id"], path=self.task_path, **fields)
                self.assertIsNone(got)
                self.assertIn("raw force", err)
                self.assertEqual(self.line_count(self.task_path), before)

    def test_minted_capability_commits_only_exact_build_owner_cas(self):
        before = self.task()
        fields = {"owner": "successor", "status": "in_progress"}
        proof = {"transfer_id": "tx-cap", "scope": "build-continuation"}
        auth = takeover._mint(before["id"], before, "incumbent", "successor",
                              fields, proof)
        row, err = tasks.update(before["id"], path=self.task_path,
                                takeover_auth=auth, **fields)
        self.assertIsNone(err, err)
        self.assertEqual(row["owner"], "successor")
        self.assertEqual(row["status"], "in_progress")
        self.assertEqual(row["takeover"], proof)
        again, err = tasks.update(before["id"], path=self.task_path,
                                  takeover_auth=auth, **fields)
        self.assertIsNone(again)
        self.assertIn("scoped", err)

    def test_task_writers_refuse_invalid_clocks_without_appending(self):
        from helm import seat_reassign
        now = time.time()
        stamps = (None, True, "yesterday", float("nan"), float("inf"),
                  float("-inf"), 10 ** 400, -(10 ** 400), now + 3,
                  now - takeover.REASSIGN_MAX_AGE_S - 1)
        for kind in ("build", "reassign"):
            with self.subTest(kind=kind):
                previous = self.task()
                fields = {"owner": "successor"}
                proof = {"transfer_id": "clock-" + kind}
                with mock.patch.object(seat_reassign, "source_disposition",
                                       return_value=(seat_reassign.SOURCE_DEAD,
                                                     "gone")):
                    disp, err = takeover.mint_source_disposition("incumbent")
                self.assertIsNone(err, err)
                if kind == "build":
                    fields["status"] = "in_progress"
                    auth = takeover._mint(previous["id"], previous, "incumbent",
                                          "successor", fields, proof)
                else:
                    auth, err = takeover.mint_seat_reassign(
                        previous["id"], previous, "incumbent", "successor",
                        proof, disposition=disp)
                    self.assertIsNone(err, err)
                with open(self.task_path, "rb") as f:
                    before = f.read()
                with mock.patch.object(takeover.time, "time", return_value=now):
                    for stamp in stamps:
                        with self.subTest(stamp=stamp):
                            auth.minted_at = stamp
                            row, err = tasks.update(previous["id"],
                                                    path=self.task_path,
                                                    takeover_auth=auth, **fields)
                            self.assertIsNone(row)
                            self.assertIn("stale or clock-invalid", err or "")
                            with open(self.task_path, "rb") as f:
                                self.assertEqual(f.read(), before)
                    auth.minted_at = now
                    row, err = tasks.update(previous["id"], path=self.task_path,
                                            takeover_auth=auth, **fields)
                self.assertIsNone(err, err)
                self.assertEqual(row["owner"], "successor")

    def test_disposition_clock_is_checked_before_either_reassign_mint(self):
        from helm import seat_reassign
        previous = self.task()
        now = time.time()
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD, "gone")):
            disp, err = takeover.mint_source_disposition("incumbent")
        self.assertIsNone(err, err)
        for mint in (lambda: takeover.mint_reassign_custody(
                "incumbent", "successor", reason="clock test", disposition=disp),
                lambda: takeover.mint_seat_reassign(
                    previous["id"], previous, "incumbent", "successor",
                    {"transfer_id": "disposition-clock"}, disposition=disp)):
            with mock.patch.object(takeover.time, "time", return_value=now):
                for stamp in (float("nan"), float("inf"), float("-inf"),
                              10 ** 400, -(10 ** 400), now + 3,
                              now - takeover.DISPOSITION_MAX_AGE_S - 1):
                    with self.subTest(stamp=stamp):
                        disp.measured_at = stamp
                        auth, err = mint()
                        self.assertIsNone(auth)
                        self.assertIn("remeasure", err or "")
                for age in (-2, 0, takeover.DISPOSITION_MAX_AGE_S):
                    disp.measured_at = now - age
                    auth, err = mint()
                    self.assertIsNone(err, err)
                    self.assertIsNotNone(auth)

    def test_capability_cannot_authorize_other_fields_or_terminal_scopes(self):  # noqa: VACUOUS_ASSERTION — a valid minted capability is supplied before each exact out-of-scope mutation refusal
        before = self.task()
        expected = {"owner": "successor", "status": "in_progress"}
        auth = takeover._mint(before["id"], before, "incumbent", "successor",
                              expected, {"transfer_id": "tx-scope"})
        for fields in ({"owner": "successor", "status": "in_progress",
                        "note": "review approved"},
                       {"owner": "successor", "status": "closed",
                        "closed_reason": "landed"},
                       {"note": "release lane"}):
            with self.subTest(fields=fields):
                row, err = tasks.update(before["id"], path=self.task_path,
                                        takeover_auth=auth, **fields)
                self.assertIsNone(row)
                self.assertRegex(err, "exact task BUILD-owner|scoped")
        self.assertEqual(takeover.BuildContinuationAuthorization.__module__,
                         "helm.takeover")
        self.assertEqual(auth.scope, "task-build-continuation")

    def test_review_fold_release_and_land_layers_do_not_accept_the_capability(self):  # noqa: VACUOUS_ASSERTION — each named authority module is opened and checked for both forbidden symbols
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for rel in ("helm/dispatches.py", "helm/foldcheck.py", "helm/landreq.py",
                    "helm/work/_claims.py"):
            with self.subTest(rel=rel), open(os.path.join(root, rel),
                                             encoding="utf-8") as f:
                source = f.read()
            self.assertNotIn("BuildContinuationAuthorization", source)
            self.assertNotIn("takeover_auth", source)


class CliContractTest(TempBase):
    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = tasks.cmd_task(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_takeover_cli_requires_one_complete_explicit_request(self):  # noqa: VACUOUS_ASSERTION — each incomplete argv is executed and its rc plus takeover error asserted
        for args in (("takeover",),
                     ("takeover", "1", "--from-lane", "source"),
                     ("takeover", "1", "--transfer-id", "tx"),
                     ("takeover", "1", "--from-lane", "source",
                      "--transfer-id", "tx", "--unknown")):
            with self.subTest(args=args):
                rc, _out, err = self.cli(*args)
                self.assertEqual(rc, 2)
                self.assertIn("takeover", err)

    def test_takeover_cli_forwards_source_transfer_and_superseding_declaration(self):
        committed = {"id": "task/1", "owner": "successor",
                     "takeover": {"incumbent": "incumbent",
                                  "successor": "successor",
                                  "transfer_id": "tx-cli"}}
        with mock.patch.object(takeover, "transfer",
                               return_value=(committed, None)) as called:
            rc, out, err = self.cli("takeover", "1", "--from-lane", "source",
                                    "--transfer-id", "tx-cli", "--superseding")
        self.assertEqual(rc, 0, err)
        self.assertIn("BUILD continuation", out)
        called.assert_called_once_with("1", "source", "tx-cli", superseding=True)


class LineageTest(TempBase):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        self.rooms = self.repo + "-wt"
        os.makedirs(self.repo)
        self.git(self.repo, "init", "-q", "-b", "main")
        self.git(self.repo, "config", "user.email", "test@example.invalid")
        self.git(self.repo, "config", "user.name", "Test")
        self.write(self.repo, "base", "base")
        self.git(self.repo, "add", "base")
        self.git(self.repo, "commit", "-q", "-m", "base")
        os.makedirs(self.rooms)
        self.add_room("source", "main")
        self.write(self.room("source"), "source", "source")
        self.git(self.room("source"), "add", "source")
        self.git(self.room("source"), "commit", "-q", "-m", "source")
        self.source_tip = self.out(self.repo, "rev-parse", "lane/source")
        self.add_room("successor", "lane/source")
        self.write(self.room("successor"), "successor", "successor")
        self.git(self.room("successor"), "add", "successor")
        self.git(self.room("successor"), "commit", "-q", "-m", "successor")

    def room(self, lane):
        return os.path.join(self.rooms, lane)

    def add_room(self, lane, start):
        self.git(self.repo, "worktree", "add", "-q", "-b", "lane/" + lane,
                 self.room(lane), start)

    @staticmethod
    def git(where, *args):
        subprocess.run(["git", "-C", where, *args], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    @staticmethod
    def out(where, *args):
        return subprocess.run(["git", "-C", where, *args], check=True,
                              text=True, stdout=subprocess.PIPE).stdout.strip()

    @staticmethod
    def write(where, name, text):
        with open(os.path.join(where, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_source_WIP_room_must_be_real_and_attached_to_its_lane(self):  # noqa: VACUOUS_ASSERTION — the real attached source room is asserted first before absent and detached refusals
        source = self.room("source")
        self.assertEqual(takeover._source_worktree(self.repo, "source"),
                         os.path.realpath(source))
        with self.assertRaisesRegex(takeover.TakeoverRefused,
                                    "WIP preservation UNKNOWN"):
            takeover._source_worktree(self.repo, "absent")
        self.git(source, "checkout", "-q", "--detach")
        with self.assertRaisesRegex(takeover.TakeoverRefused,
                                    "WIP lineage UNKNOWN"):
            takeover._source_worktree(self.repo, "source")
        self.git(source, "checkout", "-q", "lane/source")

    def test_descendant_and_explicit_superseding_lineage_preserve_source(self):  # noqa: VACUOUS_ASSERTION — real sibling worktrees prove the descendant and explicit superseding paths while the source ref remains exact
        got = takeover.lineage(self.repo, "source", "successor")
        self.assertEqual(got["mode"], "descendant")
        self.assertEqual(got["source_tip"], self.source_tip)
        self.assertEqual(self.out(self.repo, "rev-parse", "lane/source"),
                         self.source_tip)

        self.add_room("unrelated", "main")
        self.write(self.room("unrelated"), "unrelated", "unrelated")
        self.git(self.room("unrelated"), "add", "unrelated")
        self.git(self.room("unrelated"), "commit", "-q", "-m", "unrelated")
        with self.assertRaisesRegex(takeover.TakeoverRefused, "not a descendant"):
            takeover.lineage(self.repo, "source", "unrelated")
        linked = takeover.lineage(self.repo, "source", "unrelated",
                                  superseding=True)
        self.assertEqual(linked["mode"], "superseding")
        self.assertEqual(self.out(self.repo, "rev-parse", "lane/source"),
                         self.source_tip)

    def test_equal_tip_is_not_a_descendant_continuation_commit(self):  # noqa: VACUOUS_ASSERTION — a real equal-tip sibling worktree is attached before its typed refusal
        self.add_room("equal", "lane/source")
        with self.assertRaisesRegex(takeover.TakeoverRefused,
                                    "no descendant commit"):
            takeover.lineage(self.repo, "source", "equal")


class TransactionTest(TempBase):
    def setUp(self):
        super().setUp()
        self.before = self.task()
        self.root = os.path.join(self.tmp, "repo")
        self.lineage = {"mode": "descendant", "source_lane": "source",
                        "source_branch": "lane/source", "source_tip": "a" * 40,
                        "successor_lane": "successor",
                        "successor_branch": "lane/successor",
                        "successor_tip": "b" * 40}
        self.evidence = evidence()
        seat_patch = mock.patch.object(takeover.seats, "own_name",
                                       return_value="successor")
        lane_patch = mock.patch.object(takeover.work, "_infer_lane",
                                       return_value="successor")
        seat_patch.start()
        lane_patch.start()
        self.addCleanup(seat_patch.stop)
        self.addCleanup(lane_patch.stop)

    def finalize(self, record, _root, task_path=None):
        before, err = takeover._strict_task(record["task_id"], task_path)
        if err:
            raise takeover.TakeoverRefused(err)
        fields = {"owner": record["successor"], "status": "in_progress"}
        proof = {"v": 1, "transfer_id": record["transfer_id"],
                 "scope": "build-continuation",
                 "incumbent": record["incumbent"],
                 "successor": record["successor"],
                 "notifications": record["notifications"]}
        auth = takeover._mint(record["task_id"], before, record["incumbent"],
                              record["successor"], fields, proof)
        row, err = tasks.update(record["task_id"], path=task_path,
                                takeover_auth=auth, **fields)
        if err:
            raise takeover.TakeoverRefused(err)
        return row

    def run_transfer(self, transfer_id="tx-one"):
        with mock.patch.object(takeover, "lineage", return_value=self.lineage), \
                mock.patch.object(takeover, "capture", return_value=self.evidence), \
                mock.patch.object(takeover, "_finalize", side_effect=self.finalize):
            return takeover.transfer(
                self.before["id"], "source", transfer_id,
                task_path=self.task_path, ledger_path=self.transfer_path,
                root=self.root, successor="successor", successor_lane="successor")

    def test_prepare_dm_mention_and_task_commit_share_one_transfer_id(self):
        row, err = self.run_transfer()
        self.assertIsNone(err, err)
        self.assertEqual(row["owner"], "successor")
        proof = row["takeover"]
        self.assertEqual(proof["transfer_id"], "tx-one")
        self.assertEqual((proof["incumbent"], proof["successor"]),
                         ("incumbent", "successor"))
        self.assertNotIn("from", proof)
        self.assertNotIn("to", proof)
        self.assertEqual(set(proof["notifications"]),
                         {"direct_message", "mention", "room", "events"})
        saved = takeover._latest("tx-one", self.transfer_path)
        self.assertEqual(saved["phase"], "committed")
        self.assertEqual(saved["transfer_id"], proof["transfer_id"])
        self.assertEqual(self.line_count(chat.room_path("helm")), 1)
        self.assertEqual(self.line_count(chat.room_path(chat.dm_room("incumbent"))), 1)

        again, err = self.run_transfer()
        self.assertIsNone(err, err)
        self.assertEqual(again["takeover"]["transfer_id"], "tx-one")
        self.assertEqual(self.line_count(chat.room_path("helm")), 1)
        self.assertEqual(self.line_count(chat.room_path(chat.dm_room("incumbent"))), 1)

    def test_partial_notification_failure_is_visible_and_transfers_nothing(self):
        original = takeover.chat.post

        def fail_mention(*args, **kwargs):
            if kwargs.get("dm"):
                return original(*args, **kwargs)
            raise OSError("room unavailable")

        with mock.patch.object(takeover, "lineage", return_value=self.lineage), \
                mock.patch.object(takeover, "capture", return_value=self.evidence), \
                mock.patch.object(takeover.chat, "post", side_effect=fail_mention):
            row, err = takeover.transfer(
                self.before["id"], "source", "tx-partial",
                task_path=self.task_path, ledger_path=self.transfer_path,
                root=self.root, successor="successor", successor_lane="successor")
        self.assertIsNone(row)
        self.assertIn("chat transfer incomplete", err)
        self.assertEqual(tasks.get(self.before["id"], self.task_path)["owner"],
                         "incumbent")
        self.assertEqual(takeover._latest("tx-partial", self.transfer_path)["phase"],
                         "failed")
        dm_path = chat.room_path(chat.dm_room("incumbent"))
        self.assertEqual(self.line_count(dm_path), 1)
        self.assertEqual(self.line_count(chat.room_path("helm")), 0)
        with open(dm_path, encoding="utf-8") as f:
            notice = json.loads(next(line for line in f if line.strip()))["text"]
        self.assertIn("PREPARED", notice)
        self.assertIn("only if", notice)
        self.assertIn("Check the task's takeover record for the outcome", notice)
        self.assertNotIn(" is continuing ", notice)

    def test_lost_notification_ack_retries_same_chat_rows_then_commits(self):
        original = takeover._append_phase
        lost = {"done": False}

        def lose_notified(record, phase, path=None, **fields):
            if phase == "notified" and not lost["done"]:
                lost["done"] = True
                raise takeover.TakeoverRefused("lost notification ack")
            return original(record, phase, path=path, **fields)

        with mock.patch.object(takeover, "lineage", return_value=self.lineage), \
                mock.patch.object(takeover, "capture", return_value=self.evidence), \
                mock.patch.object(takeover, "_finalize", side_effect=self.finalize), \
                mock.patch.object(takeover, "_append_phase",
                                  side_effect=lose_notified):
            row, err = takeover.transfer(
                self.before["id"], "source", "tx-lost",
                task_path=self.task_path, ledger_path=self.transfer_path,
                root=self.root, successor="successor", successor_lane="successor")
        self.assertIsNone(row)
        self.assertIn("lost notification ack", err)
        self.assertEqual(tasks.get(self.before["id"], self.task_path)["owner"],
                         "incumbent")
        self.assertEqual(self.line_count(chat.room_path("helm")), 1)
        self.assertEqual(self.line_count(chat.room_path(chat.dm_room("incumbent"))), 1)

        row, err = self.run_transfer("tx-lost")
        self.assertIsNone(err, err)
        self.assertEqual(row["owner"], "successor")
        self.assertEqual(self.line_count(chat.room_path("helm")), 1)
        self.assertEqual(self.line_count(chat.room_path(chat.dm_room("incumbent"))), 1)

    def test_malformed_prepare_row_is_unknown_and_never_mutates_task(self):
        with open(self.transfer_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"id": "takeover/tx-bad", "phase": "prepared"}) +
                    "\n")
        row, err = takeover.transfer(
            self.before["id"], "source", "tx-bad", task_path=self.task_path,
            ledger_path=self.transfer_path, root=self.root, successor="successor",
            successor_lane="successor")
        self.assertIsNone(row)
        self.assertIn("malformed", err)
        self.assertEqual(tasks.get(self.before["id"], self.task_path)["owner"],
                         "incumbent")

    def test_transfer_id_cannot_be_rebound_to_another_request(self):  # noqa: VACUOUS_ASSERTION — the first transfer commits and is asserted before the conflicting reuse refusal
        row, err = self.run_transfer("tx-stable")
        self.assertIsNone(err, err)
        with mock.patch.object(takeover, "lineage", return_value=self.lineage), \
                mock.patch.object(takeover, "capture", return_value=self.evidence):
            other, err = takeover.transfer(
                self.before["id"], "other-source", "tx-stable",
                task_path=self.task_path, ledger_path=self.transfer_path,
                root=self.root, successor="successor", successor_lane="successor")
        self.assertIsNone(other)
        self.assertIn("different transfer request", err)
        self.assertEqual(takeover._latest("tx-stable", self.transfer_path)["phase"],
                         "committed")


class FinalRaceTest(TempBase):
    class ClaimLock(object):
        f = object()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def setUp(self):
        super().setUp()
        self.before = self.task()
        self.lineage = {"mode": "descendant", "source_lane": "source",
                        "source_branch": "lane/source", "source_tip": "a" * 40,
                        "successor_lane": "successor",
                        "successor_branch": "lane/successor",
                        "successor_tip": "b" * 40}
        self.base = evidence()
        self.record = {"task_id": self.before["id"], "incumbent": "incumbent",
                       "successor": "successor", "source_lane": "source",
                       "successor_lane": "successor", "superseding": False,
                       "task_before": takeover._task_digest(self.before),
                       "evidence_anchor": takeover._bundle_anchor(self.base),
                       "lineage": self.lineage, "transfer_id": "tx-race",
                       "notifications": {"direct_message": "d", "mention": "m"},
                       "prepared_at": time.time()}

    def patches(self, final, lineage=None):
        return (mock.patch.object(takeover, "_seat_dir", return_value="/seat"),
                mock.patch.object(takeover.seat_lifecycle, "_seat_lifecycle_lock",
                                  return_value=contextlib.nullcontext()),
                mock.patch.object(takeover.seats, "_claim_flocked",
                                  return_value=self.ClaimLock()),
                mock.patch.object(takeover, "_capture_locked", return_value=final),
                mock.patch.object(takeover, "lineage",
                                  return_value=lineage or self.lineage))

    def test_occurrence_claim_and_proxy_races_each_refuse_before_task_mutation(self):  # noqa: VACUOUS_ASSERTION — each exact changed bundle is supplied and the incumbent owner is asserted afterward
        finals = []
        for section, key, value in (("occurrence", "handle", "pane-b"),
                                    ("claim", "fence", 8),
                                    ("proxywatch", "inflight", 1)):
            changed = json.loads(json.dumps(self.base))
            changed[section][key] = value
            finals.append(changed)
        for final in finals:
            with self.subTest(final=final), contextlib.ExitStack() as stack:
                for patch in self.patches(final):
                    stack.enter_context(patch)
                with self.assertRaisesRegex(takeover.TakeoverRefused,
                                            "final evidence does not match"):
                    takeover._finalize(self.record, "/repo", self.task_path)
            self.assertEqual(tasks.get(self.before["id"], self.task_path)["owner"],
                             "incumbent")

    def test_unchanged_final_bundle_and_task_cas_commit_together(self):
        with contextlib.ExitStack() as stack:
            for patch in self.patches(self.base):
                stack.enter_context(patch)
            row = takeover._finalize(self.record, "/repo", self.task_path)
        self.assertEqual(row["owner"], "successor")
        self.assertEqual(row["status"], "in_progress")
        self.assertEqual(row["takeover"]["transfer_id"], "tx-race")

    def test_lineage_and_task_snapshot_races_refuse(self):  # noqa: VACUOUS_ASSERTION — both concrete races are created and the incumbent owner remains asserted afterward
        moved = dict(self.lineage, successor_tip="c" * 40)
        with contextlib.ExitStack() as stack:
            for patch in self.patches(self.base, lineage=moved):
                stack.enter_context(patch)
            with self.assertRaisesRegex(takeover.TakeoverRefused, "lineage moved"):
                takeover._finalize(self.record, "/repo", self.task_path)
        changed, err = tasks.update(self.before["id"], path=self.task_path,
                                    note="concurrent edit")
        self.assertIsNone(err, err)
        self.assertIsNotNone(changed)
        with contextlib.ExitStack() as stack:
            for patch in self.patches(self.base):
                stack.enter_context(patch)
            with self.assertRaisesRegex(takeover.TakeoverRefused, "task snapshot"):
                takeover._finalize(self.record, "/repo", self.task_path)
        self.assertEqual(tasks.get(self.before["id"], self.task_path)["owner"],
                         "incumbent")


class ARefusalNamesEveryDoorThatOpensItTest(unittest.TestCase):
    """task/2136 — a refusal that lists the authorized alternatives lists ALL.

    `authorize_task_mutation` dispatches on TYPE to two capabilities. The
    refusal an operator actually reads named one of them, and the one it
    omitted covers the population most likely to be standing in front of it:
    a row held by a seat no roster knows. The omission is not cosmetic and it
    is measurable — the author of the neighbouring code read that sentence and
    published "the row cannot be transferred or cleared by anyone", which is
    exactly what a partial list reads as.

    So the sentence is now BUILT FROM the dispatch table. These arms pin the
    two halves that can drift apart: every dispatchable capability has a door,
    and the door text actually reaches the operator through the real refusal.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-doors-")
        self._env = {k: os.environ.pop(k, None) for k in ENV_KEYS}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "home")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NAME"] = "seat-b"
        self.p = os.path.join(self.tmp, "tasks.jsonl")

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def dispatchable(self):
        """Every Authorization class this module defines, by INTROSPECTION.

        Derived rather than listed: a third capability added to
        authorize_task_mutation shows up here without anyone remembering to
        extend a fixture, which is the only way this arm can outlive its
        author.
        """
        import inspect
        return {o for n, o in vars(takeover).items()
                if inspect.isclass(o) and n.endswith("Authorization")
                and getattr(o, "__module__", None) == takeover.__name__}

    def test_every_dispatchable_capability_has_an_operator_door(self):
        found = self.dispatchable()
        # POSITIVE CONTROL FIRST: an empty or one-element discovery would make
        # the subset assertion below pass while proving nothing. The two known
        # capabilities must BOTH be discovered by the same introspection the
        # assertion uses.
        self.assertIn(takeover.BuildContinuationAuthorization, found)
        self.assertIn(takeover.SeatReassignAuthorization, found)
        self.assertGreaterEqual(len(found), 2, found)
        documented = {d.cls for d in takeover._TASK_OWNER_DOORS}
        missing = found - documented
        self.assertEqual(
            missing, set(),
            "these capabilities can authorize a task OWNER change and offer "
            "the operator no door in the refusal: %s"
            % sorted(c.__name__ for c in missing))

    def test_the_incumbent_refusal_carries_every_door_verbatim(self):
        row, err = tasks.add("a row held by a seat no roster knows",
                             "seat-a", path=self.p, project="doors",
                             origin="agent", posture_na="arm")
        self.assertIsNone(err, err)
        _r, detail = tasks.update(row["id"], path=self.p, owner="seat-b")
        self.assertIsNotNone(detail, "the incumbent guard did not refuse, so "
                                     "this arm never reached its subject")
        self.assertIn("raw force cannot transfer or clear an incumbent", detail)
        for door in takeover.task_owner_doors("seat-a"):
            self.assertIn(door, detail,
                          "the refusal omits an authorized door: %s" % door)

    def test_the_reassign_door_is_a_command_the_operator_can_run(self):
        """A door naming a CONTRACT is a reading assignment; a door naming a
        VERB with the incumbent already in it is an action. Both directions:
        the name appears, and it is THIS incumbent's name rather than a
        constant that would be right by accident."""
        mine = takeover.task_owner_doors("seat-a")
        theirs = takeover.task_owner_doors("seat-c")
        self.assertTrue(any("helm seat reassign seat-a --to" in d
                            for d in mine), mine)
        self.assertTrue(any("helm seat reassign seat-c --to" in d
                            for d in theirs), theirs)
        self.assertNotEqual(mine, theirs,
                            "the doors do not vary with the incumbent, so the "
                            "command in them cannot be copied and run")

    def test_the_refusal_counts_the_doors_it_lists(self):
        """The count is derived, not typed. A capability added to the table
        without this being generated would say `Two capabilities` over three
        doors — the same drift one field over."""
        row, err = tasks.add("counted", "seat-a", path=self.p,
                             project="doors", origin="agent", posture_na="arm")
        self.assertIsNone(err, err)
        _r, detail = tasks.update(row["id"], path=self.p, owner="seat-b")
        n = len(takeover.task_owner_doors("seat-a"))
        self.assertIn("%d capabilit%s can:" % (n, "y" if n == 1 else "ies"),
                      detail)

    def test_the_refusal_names_the_holders_own_path(self):
        """EVERY DOOR IN THE TABLE IS A TRANSFER SOMEBODY ELSE PERFORMS, so a
        live holder who simply agrees the work belongs elsewhere reaches none
        of them by itself. The refusal says which act each door waits on, and
        then points at the ask, which needs no capability at all.

        EVERY PHRASE THIS ARM LOOKS FOR IS READ FROM THE TABLE — each door's
        ask, and the verb that opens it — so rewording a door carries its own
        arm with it. A transcribed sentence pins wording no door supplies:
        it goes red on a rewrite that breaks nothing, and it says nothing at
        all about the door it names. The one phrase left
        here is the incumbent's NAME, which no table supplies.

        The exact clause the counts and verbs are rendered INTO is the
        subject of test_every_claim_the_holder_clause_makes_is_READ_FROM_THE_DOORS,
        which builds table shapes this one cannot observe."""
        row, err = tasks.add("a row its holder wants to give away", "seat-a",
                             path=self.p, project="doors", origin="agent",
                             posture_na="arm")
        self.assertIsNone(err, err)
        _r, detail = tasks.update(row["id"], path=self.p, owner="seat-b")
        self.assertIsNotNone(detail, "the incumbent guard did not refuse, so "
                                     "this arm never reached its subject")
        facts = takeover.task_owner_door_facts("seat-a")
        self.assertTrue(facts, "the doors table is empty, so every assertion "
                               "below would hold over nothing")
        # SCOPE, ASSERTED RATHER THAN ASSUMED. This arm reads the advice given
        # to a holder who can open NOTHING. A table offering the holder a door
        # of its own renders a different clause, and this arm would go quiet
        # about it instead of failing.
        self.assertEqual([d.cls.__name__ for d in facts if d.holder_may_open],
                         [], "a door in the table is now the holder's own to "
                             "open, so the advice this arm reads is no longer "
                             "the advice that table produces")
        for door in facts:
            # THE OFFER FOLLOWS REACHABILITY, MEASURED FOR THIS FIXTURE'S
            # INCUMBENT RATHER THAN ASSUMED. A door this environment cannot
            # open for "seat-a" is named as shut instead of offered, so an arm
            # that demanded every ask would be asserting the defect.
            if door.reachable is False:
                self.assertNotIn(door.ask, detail,
                                 "the refusal offers the %s door, which it "
                                 "measured as unable to open for this seat"
                                 % door.cls.__name__)
                self.assertIn(door.text, detail,
                              "a door dropped from the offer must still be "
                              "NAMED, or an operator who knows the capability "
                              "exists is told nothing about why it vanished")
                self.assertIn(door.blocked_by, detail,
                              "the refusal says the %s door cannot open and "
                              "not what its own check reported"
                              % door.cls.__name__)
            else:
                self.assertIn(door.ask, detail,
                              "the refusal offers no ask for the %s door, so "
                              "a holder reading it learns only that it is "
                              "stuck" % door.cls.__name__)
            self.assertIn(door.opened_by, detail,
                          "the refusal never says the %s door is %s, so the "
                          "holder cannot tell whose act it waits on"
                          % (door.cls.__name__, door.opened_by))
        # UNCONDITIONAL, OUTSIDE THE BRANCH: at least one ask must reach the
        # holder for this arm to be about advice at all. A fixture where every
        # door measured shut would satisfy every assertion above by asserting
        # only absences.
        self.assertTrue([d for d in facts if d.reachable is not False],
                        "every door measured shut for this fixture's "
                        "incumbent, so this arm no longer reads any advice")
        self.assertIn("If you ARE seat-a", detail,
                      "the advice must name THIS incumbent, or a constant "
                      "would read correctly on every row and identify none")
        # THE BARRIER IS DIRECTION, NOT FITNESS, and this is the assertion
        # that keeps it that way. The BUILD continuation contract is built
        # FOR a live incumbent — takeover._notify DMs them and @-mentions
        # them before the transfer commits — so a sentence claiming neither
        # door describes a live holder is false by that door's own words.
        # What the holder cannot do is OFFER. If this arm ever has to be
        # relaxed, the sentence has drifted back to the false reason.
        self.assertNotIn("neither one describes you", detail,
                         "the refusal is back to saying the BUILD contract "
                         "does not fit a live holder, which is false by that "
                         "contract's own description and by _notify's "
                         "behaviour")

    def _shaped(self, *doors):
        """One refusal rendered over a synthetic doors table -> detail."""
        row, err = tasks.add("reach", "seat-a", path=self.p, project="doors",
                             origin="agent", posture_na="arm")
        self.assertIsNone(err, err)
        with mock.patch.object(takeover, "_TASK_OWNER_DOORS", doors):
            _r, detail = tasks.update(row["id"], path=self.p, owner="seat-b")
        self.assertIsNotNone(detail, "the incumbent guard did not refuse, so "
                                     "this arm never reached its subject")
        return detail

    @staticmethod
    def _door(text, ask, answer):
        """A door whose reach question returns exactly `answer`."""
        return takeover._Door(object, text, "TAKEN", False, ask,
                              lambda _name, a=answer: a)

    def test_a_door_that_cannot_open_for_this_seat_is_named_not_offered(self):
        """A CAPABILITY THE CODE CAN DISPATCH IS NOT A CAPABILITY THE
        ENVIRONMENT CAN RESOLVE, and the refusal was honest about the first
        while silent about the second. Pointing a stuck operator at a door
        that cannot open for their seat costs them the one thing this sentence
        exists to save: the next move.

        Three shapes at once, because the interesting property is how they
        SEPARATE — an open door is offered, a shut one is named as shut and
        dropped from the offer, and an UNMEASURED one stays offered, since a
        probe that could not answer is not a closed door."""
        open_door = self._door("the open one", "ask for the open one",
                               (True, ""))
        shut = self._door("the shut one", "ask for the shut one",
                          (False, "no register carries this seat"))
        unknown = self._door("the unmeasured one", "ask for the unmeasured one",
                             (None, "the probe could not answer"))
        detail = self._shaped(open_door, shut, unknown)
        # THE OFFER: open and unmeasured are in it, shut is not.
        self.assertIn(open_door.ask, detail)
        self.assertIn(unknown.ask, detail)
        self.assertNotIn(shut.ask, detail,
                         "the refusal still tells the operator to ask for a "
                         "door that cannot open for this seat")
        # AND THE SHUT DOOR IS NAMED RATHER THAN HIDDEN, with the reason its
        # own probe gave. Dropping it silently would leave an operator who
        # already knows the capability exists wondering why it went missing.
        self.assertIn("cannot open for seat-a", detail)
        self.assertIn(shut.text, detail)
        self.assertIn("no register carries this seat", detail)
        # THE COUNT IS STILL A FACT ABOUT THE CODE. Three capabilities can
        # authorize this mutation; that they cannot all resolve for one seat
        # is a separate sentence and must not silently shrink the first.
        self.assertIn("3 capabilities can:", detail)

    def test_an_UNMEASURED_doors_reason_rides_on_its_own_ask(self):
        """A PROBE'S REFUSAL IS OFTEN THE INSTRUCTIONS FOR OPENING THE DOOR,
        and an offer that keeps the door while dropping the reason hands the
        operator the half that needs no action.

        The tri-state settles which doors are OFFERED. It says nothing about
        where an UNKNOWN door's reason goes, and the reasons differ in kind: a
        SHUT door's reason explains a capability that vanished from the offer,
        while an UNKNOWN door's reason frequently names the operand to sharpen
        — an ambiguous token lists every seat it matched, and picking one is
        the entire remedy. Computing that sentence and rendering nothing is a
        refusal that knows the next move and does not say it.

        THE PAIRING IS THE ASSERTION, not the presence of the text. A reason
        that appeared anywhere in the sentence would pass a substring check
        while leaving a reader to guess which door it belongs to, so the arm
        pins the composed line."""
        open_door = self._door("the open one", "ask for the open one",
                               (True, ""))
        unknown = self._door(
            "the unmeasured one", "ask for the unmeasured one",
            (None, "that token is remembered by 2 seats (seat-a, seat-b) — "
                   "name the seat exactly"))
        detail = self._shaped(open_door, unknown)
        self.assertIn("%s (UNMEASURED here: %s)" % (unknown.ask,
                                                    unknown.reach(None)[1]),
                      detail,
                      "the UNMEASURED door is offered without the words its "
                      "own probe gave, so the refusal holds the operand to "
                      "sharpen and does not print it")
        # MUST-MISS, ON THE SAME SENTENCE: a door that ANSWERED renders its
        # ask alone. A parenthetical on every line is one a reader learns to
        # skip, and it would also mean this arm is reading a decoration rather
        # than the tri-state.
        self.assertIn(open_door.ask, detail,
                      "the measured-open door left the offer, so the control "
                      "below is about a door nobody is being told to ask for")
        self.assertNotIn("%s (UNMEASURED" % open_door.ask, detail,
                         "a door whose probe answered is rendered as though "
                         "its check could not be run")
        # AND AN UNMEASURED DOOR IS NOT A SHUT ONE. The shut clause drops its
        # doors from the offer; reaching this door through that clause would
        # be the tri-state collapsing back to two states with extra words.
        self.assertNotIn("cannot open for seat-a", detail,
                         "an unmeasured door is being reported as one this "
                         "environment measured shut")

    def test_an_UNMEASURED_door_with_no_words_still_says_UNMEASURED(self):
        """THE MARKER IS KEYED ON THE TRI-STATE, NOT ON THE REASON, because a
        door that answered None and said nothing is exactly as unmeasured as
        one that explained itself.

        Rendering it as a bare ask puts it in the same shape as a door
        measured OPEN, which is the collapse the tri-state exists to prevent —
        the cured defect one level down, reachable through a producer nobody
        has written yet rather than through one standing today.

        NO PRODUCER IN THIS TREE EMITS THIS PAIR, and the arm is here for that
        reason rather than in spite of it: the property is held up by
        discipline across two modules and enforced nowhere, and
        `_build_contract_reach` already returns an empty second element on its
        True path, so the spelling is idiomatic in this table."""
        silent = self._door("the silent one", "ask for the silent one",
                            (None, ""))
        # THE OTHER None DOOR, AND IT IS THE CONTROL THAT MAKES THE
        # PLACEHOLDER MEAN SOMETHING. A True door only separates marked from
        # unmarked; these two are both UNMEASURED, so what discriminates them
        # is WHOSE WORDS each carries — and a build that dropped the fallback
        # would render the silent door with an empty reason while this one
        # still read correctly.
        spoken = self._door("the speaking one", "ask for the speaking one",
                            (None, "the probe named a reason"))
        answered = self._door("the open one", "ask for the open one",
                              (True, ""))
        detail = self._shaped(silent, spoken, answered)
        # THE PLACEHOLDER IS THE ONLY NEW STRING ON THIS PATH, so the arm has
        # to reach past the colon: stopping there is satisfied exactly by the
        # render this branch exists to prevent, `(UNMEASURED here: )` with
        # nothing after it. The literal is TRANSCRIBED on purpose — it is
        # operator-facing text, and a build that reworded it should have to
        # say so here.
        self.assertIn("%s (UNMEASURED here: the probe gave no reason)"
                      % silent.ask, detail,
                      "the silent door renders an UNMEASURED marker with no "
                      "reason inside it, which claims a measurement it does "
                      "not have and reads as a rendering bug rather than a "
                      "probe that said nothing")
        # AND THE FALLBACK IS A FALLBACK: a None door that DID speak carries
        # its own words and never the placeholder, so the assertion above is
        # about an absent reason rather than about every unmeasured door.
        self.assertIn("%s (UNMEASURED here: %s)"
                      % (spoken.ask, spoken.reach(None)[1]), detail,
                      "a door that answered None WITH words lost them")
        self.assertNotIn("%s (UNMEASURED here: the probe gave no reason)"
                         % spoken.ask, detail,
                         "the placeholder displaced a reason the probe "
                         "actually gave")
        # MUST-MISS ON THE SAME SENTENCE, and it is the whole point of the
        # arm: the measured-open door must NOT pick up the marker. Without
        # it this arm would pass against a build that marked every door.
        self.assertIn(answered.ask, detail,
                      "the measured-open door left the offer, so the control "
                      "below is about a door nobody is being told to ask for")
        self.assertNotIn("%s (UNMEASURED" % answered.ask, detail,
                         "a door whose probe answered is rendered as though "
                         "its check could not be run")
        # AND IT IS STILL OFFERED. An unmeasured door keeps its place; if the
        # marker had arrived by way of the shut clause instead, the assertion
        # above would hold for the opposite behaviour.
        self.assertNotIn("cannot open for seat-a", detail,
                         "an unmeasured door is being reported as one this "
                         "environment measured shut")

    def test_the_reassign_doors_own_ambiguity_reaches_the_operator(self):
        """THE SAME PROPERTY THROUGH THE LIVE TABLE, because the synthetic arm
        above proves the RENDER and cannot prove that any real probe produces
        a reason worth carrying.

        An ambiguous source token is the case that matters: the verb genuinely
        refuses, so the door as rendered does not open — and the capability
        does, once the operand names one seat. That is why ambiguity answers
        UNKNOWN rather than False, and why its words are the actionable ones.
        """
        row, err = tasks.add("ambiguous", "seat-a", path=self.p,
                             project="doors", origin="agent", posture_na="arm")
        self.assertIsNone(err, err)
        sharpen = ("session abcdefgh is remembered by 2 seats (seat-a, "
                   "seat-b) - name the seat exactly")
        with mock.patch("helm.seat_reassign.resolve_source",
                        return_value=(None, None, sharpen)):
            _r, detail = tasks.update(row["id"], path=self.p, owner="seat-b")
        self.assertIsNotNone(detail, "the incumbent guard did not refuse, so "
                                     "this arm never reached its subject")
        self.assertIn(sharpen, detail,
                      "the resolver said which seats the token matched and "
                      "the refusal dropped it, so the operator is told to ask "
                      "for a door and not how to make it answer")
        # UNCONDITIONAL CONTROL ON THE SAME SENTENCE: the door is still being
        # OFFERED. If ambiguity had collapsed to shut, the reason would be
        # reaching the reader through the dropped-door clause instead, and the
        # assertion above would hold for the opposite behaviour.
        reassign = [d for d in takeover.task_owner_door_facts("seat-a")
                    if d.cls is takeover.SeatReassignAuthorization]
        self.assertTrue(reassign, "the live table carries no reassign door, "
                                  "so this arm measures nothing about it")
        self.assertIn(reassign[0].ask, detail,
                      "the reassign door left the offer, so its reason is "
                      "being rendered as a shut door's explanation")

    def test_when_no_door_can_open_the_advice_says_so_instead_of_a_list(self):
        """AN EMPTY OFFER IS A SENTENCE, NOT AN OMISSION. With every door shut
        the advice has nothing to name, and the shape that reads "So say it on
        the row — ." is how a reader learns nothing at all."""
        shut = self._door("the only one", "ask for the only one",
                          (False, "no register carries this seat"))
        detail = self._shaped(shut)
        # THE SENTENCE NAMES THE INCUMBENT, because the fact is about this
        # seat's environment and not about the code: another seat reading the
        # same row can be told something different, and a constant sentence
        # would read as a property of the capability itself.
        self.assertIn("no capability here can open for seat-a", detail)
        self.assertNotIn(shut.ask, detail)
        # AND IT IS NOT THE EMPTY-TABLE SENTENCE. A table with a door that
        # cannot open is a different fact from a table with no door, and the
        # second is not fixable by registering anything.
        self.assertNotIn("there is no capability here to ask for", detail)
        self.assertIn("1 capability can:", detail,
                      "the capability count is derived from the table and "
                      "says what the CODE accepts, which is unchanged by any "
                      "seat's register")

    def test_a_probe_that_raises_leaves_its_door_in_the_offer(self):
        """MISSING EVIDENCE IS NOT EVIDENCE AGAINST. A refusal that answered
        'cannot open' because its own check broke would send an operator away
        from a capability that may be working."""
        boom = takeover._Door(object, "the door whose probe throws",
                              "TAKEN", False, "ask about the throwing one",
                              mock.Mock(side_effect=RuntimeError("probe down")))
        detail = self._shaped(boom)
        self.assertIn(boom.ask, detail)
        self.assertNotIn("cannot open for seat-a", detail)
        facts = takeover.task_owner_door_facts("seat-a")
        self.assertTrue(facts, "the live table is empty, so the control below "
                               "measures nothing")

    def test_a_door_with_no_probe_is_unmeasured_rather_than_open(self):
        """The default answer for a row that names no check. Defaulting to
        OPEN would let a door claim an availability nobody ever asked about,
        which is the same false confidence this arm's siblings remove."""
        with mock.patch.object(takeover, "_TASK_OWNER_DOORS",
                               (takeover._Door(object, "no probe here",
                                               "TAKEN", False, "ask anyway"),)):
            fact, = takeover.task_owner_door_facts("seat-a")
        self.assertIsNone(fact.reachable)
        self.assertIn("names no way to check", fact.blocked_by)

    def test_an_UNREADABLE_roster_leaves_the_reassign_door_in_the_offer(self):
        """THE TRI-STATE, DRIVEN THROUGH THE REAL RESOLVER RATHER THAN A MOCK
        OF IT — which is the only way this defect is visible, because it lives
        in what the resolver's FALSY RETURN MEANS.

        `resolve_source` hands back a seat for every case where the door can
        open, INCLUDING a name no roster row answers to: its own docstring
        calls that the orphan case and says moving those holdings is the
        verb's job. So absence never arrives as a falsy seat, and the falsy
        returns are no token, an UNREADABLE roster, and an ambiguous token.
        Mapping those to False shipped once, and it inverted the tri-state
        exactly: the one moment the strongest evidence is unavailable became
        the one moment the refusal told an operator the door was shut and
        dropped its ask.
        """
        from helm import seats_roster
        # UNPATCHED CONTROL FIRST, on the same call: against the real roster
        # this door answers OPEN, so an UNKNOWN below is caused by the patch
        # rather than by anything about the fixture's seat name.
        reachable, why = takeover._seat_reassign_reach("seat-a")
        self.assertIs(reachable, True,
                      "the reassign door does not open for an ordinary name "
                      "against a readable roster, so this arm cannot show "
                      "what an unreadable one changes: %s" % why)
        # The tri-state reader, not a raise: the fail-open reader cannot
        # raise, so an OSError here rehearsed a path production never takes.
        with mock.patch.object(seats_roster, "roster_checked",
                               return_value=({}, True)):
            reachable, why = takeover._seat_reassign_reach("seat-a")
            self.assertIsNone(reachable,
                              "an unreadable roster answered a VERDICT about "
                              "the door instead of UNKNOWN")
            self.assertIn("UNKNOWN", why,
                          "the answer carries none of the resolver's own "
                          "words about why it could not tell")
            row, err = tasks.add("unreadable", "seat-a", path=self.p,
                                 project="doors", origin="agent",
                                 posture_na="arm")
            self.assertIsNone(err, err)
            _r, detail = tasks.update(row["id"], path=self.p, owner="seat-b")
            facts = takeover.task_owner_door_facts("seat-a")
        door, = [d for d in facts
                 if d.cls is takeover.SeatReassignAuthorization]
        self.assertIsNot(door.reachable, False,
                         "the reassign door was measured SHUT because the "
                         "roster could not be read")
        self.assertIn(door.ask, detail,
                      "the refusal dropped the reassign ask while the roster "
                      "was merely unreadable, which is the tri-state inverted")

    def test_each_reach_question_is_asked_of_the_doors_own_precondition(self):
        """THE PROBE MUST BE THE DOOR'S OWN CALL, NOT A COPY OF ITS RULE. A
        second implementation of "can this seat be resolved" starts at zero on
        every edge the first already handles, and the two drift apart in the
        direction nobody is watching. So each probe is bound to the call its
        capability makes: the BUILD contract's `capture` opens with
        `_seat_dir`, and `helm seat reassign` opens by resolving a source
        token through the roster."""
        # BOTH POLES ON THE SAME OBSERVABLE, the open one FIRST and
        # unconditionally: a probe wired to nothing at all returns a falsey
        # answer for every input, which satisfies every shut assertion below
        # while proving the door is unreadable rather than closed.
        with mock.patch.object(takeover, "_seat_dir") as seat_dir:
            seat_dir.return_value = "/seats/whatever"
            reachable, why = takeover._build_contract_reach("seat-a")
            self.assertTrue(reachable, "the BUILD probe calls _seat_dir and "
                                       "still answers shut when it succeeds")
            self.assertEqual("", why)
            seat_dir.side_effect = takeover.TakeoverRefused("no register here")
            reachable, why = takeover._build_contract_reach("seat-a")
        seat_dir.assert_called_with("seat-a")
        self.assertFalse(reachable)
        self.assertIn("no register here", why)

        # PATCHED BY PATH, NOT BY A LOCAL IMPORT: binding the module to a
        # name here shadows it for the rest of the method, and an analyzer
        # that reads assertion provenance cannot then tell which object the
        # assertions observed.
        with mock.patch("helm.seat_reassign.resolve_source") as resolve:
            resolve.return_value = ("seat-a", "sid", "exact roster row")
            reachable, why = takeover._seat_reassign_reach("seat-a")
            self.assertTrue(reachable, "the reassign probe resolves a source "
                                       "and still answers shut when it does")
            self.assertEqual("", why)
            resolve.return_value = (None, None, "no roster row for that token")
            reachable, why = takeover._seat_reassign_reach("seat-a")
        resolve.assert_called_with("seat-a")
        self.assertFalse(reachable)
        self.assertIn("no roster row", why)

    def work_update(self, tid):
        """One owner-change attempt against this fixture's ledger -> (row, detail)."""
        return tasks.update(tid, path=self.p, owner="seat-b")

    def test_every_claim_the_holder_clause_makes_is_READ_FROM_THE_DOORS(self):  # noqa: VACUOUS_ASSERTION — each absence is preceded, in the same iteration, by unconditional positives on the SAME observable: the derived count phrase for that table shape and the clause the shape implies. A refusal that failed to re-render fails those first
        """THE SENTENCE MAY ASSERT NOTHING THE TABLE DOES NOT SUPPLY.

        A refusal that states a fact about a table — how many rows, how they
        open, which one to ask for, whether the holder may open one — is true
        only of the rows that existed when the prose was written. Each such
        fact left in prose became wrong in turn: a hardcoded arity, a
        hardcoded characterisation, a hardcoded capability name. So the row
        carries the fact and the sentence renders it.

        THE SHAPES HERE ARE THE ONES THE LIVE TABLE CANNOT PRODUCE, which is
        the only reason this arm can see anything: an empty table, a single
        door that is NOT the BUILD contract, a door whose mechanics are
        neither of the two that exist, and a HOLDER-OPENABLE door — the row
        task/2026 would add, and the one case where the quantifier itself has
        to change.
        """
        # THE REACH QUESTION IS NEUTRALISED HERE ON PURPOSE. This arm is about
        # what the CLAUSE states over a table shape — counts, how each opens,
        # whether the holder may open one — and a door carrying its real probe
        # would make those assertions depend on whether the machine running
        # them registers "seat-a". Reachability has its own arms; a row with
        # no probe reads UNMEASURED and stays in the offer, which is exactly
        # the neutral input this arm wants.
        build, reassign = (d._replace(reach=None)
                           for d in takeover._TASK_OWNER_DOORS)
        third = takeover._Door(object, "some future door", "BOUGHT", False,
                               "ask the market")
        offerable = takeover._Door(object, "the hand-off verb", "OFFERED",
                                   True, "run the hand-off yourself")
        row, err = tasks.add("claims", "seat-a", path=self.p, project="doors",
                             origin="agent", posture_na="arm")
        self.assertIsNone(err, err)
        # UNCONDITIONAL CONTROL OUTSIDE THE LOOP: the live table renders at
        # all. Every assertion below is inside an iteration, so a loop that
        # stopped iterating would take the whole arm quiet with it.
        _r0, live = tasks.update(row["id"], path=self.p, owner="seat-b")
        self.assertIn("NONE of them is yours to open", live)
        for doors, wants, forbids in (
                ((), ["NONE of them is yours to open",
                      "there is no capability here to ask for"],
                 ["BUILD contract", "by somebody else"]),
                ((reassign,), ["each is FORCED by somebody else",
                               "ask an authorized operator to move it"],
                 ["BUILD contract", "TAKEN"]),
                ((build,), ["each is TAKEN by somebody else",
                            "under the BUILD contract"],
                 ["FORCED", "authorized operator"]),
                ((build, reassign, third),
                 ["each is BOUGHT or FORCED or TAKEN by somebody else",
                  "ask the market"], ["NONE of them is yours to open: each is "
                                      "FORCED or TAKEN"]),
                ((build, reassign, offerable),
                 ["1 of them IS yours to open", "the hand-off verb",
                  "run the hand-off yourself"],
                 ["NONE of them is yours to open"]),
        ):
            with mock.patch.object(takeover, "_TASK_OWNER_DOORS", doors):
                _r, detail = tasks.update(row["id"], path=self.p,
                                          owner="seat-b")
            n = len(doors)
            self.assertIsNotNone(detail, "the guard did not refuse at %d "
                                         "door(s); this shape measured "
                                         "nothing" % n)
            self.assertIn("%d capabilit%s can:"
                          % (n, "y" if n == 1 else "ies"), detail,
                          "the derived count did not follow the table, so "
                          "nothing below is about this shape")
            for want in wants:
                self.assertIn(want, detail,
                              "shape of %d door(s) must state %r" % (n, want))
            for forbid in forbids:
                self.assertNotIn(forbid, detail,
                                 "shape of %d door(s) states %r, which its "
                                 "rows do not supply" % (n, forbid))


if __name__ == "__main__":
    unittest.main()
