#!/usr/bin/env python3
"""A seat that OWES a dispatch row hears about it at every idle stop and on
every wake its beacon delivers.

THE SYMPTOM THIS PINS (measured; the seat is named generically). A
review row was dispatched to a seat while that seat was busy on another
review. The seat finished that review and then ended THREE turns, each woken
by a beacon event, with "Standing by", while the row sat PENDING VERDICT
addressed to it. The DISPATCHER's stop hook printed NEEDS CHECK-IN (OVERDUE)
the whole time, so the owed-row fact existed in helm. Nothing on the
RECIPIENT's path said it.

WHAT THE RECIPIENT'S PATH HAD ON TRUNK, measured by the first arm below. The
only surface was the auto-claim/offer whisper at the bottom of the stop ladder:
ONE line per stop, latched per (seat, session) on the row's fingerprint, and
with no start command in it. So the first idle stop mentioned the row once,
and every later idle turn ended unrefused with nothing about the row (the
second was told "inbox clean"). The beacon's wake line carried only the row
that woke it.

Every arm drives a REAL entry point: `helm chat stop-guard --hook-json` for the
stop and `helm chat wait --follow` for the wake. Rows are planted raw into the
ledger (the WorkOfferTest idiom in tests/test_seats.py), so no git is needed.
The stop facts are the resident's own computation (tests/_stopfacts.py).
"""
import json
import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_seats import SeatsBase  # noqa: E402

from helm import chat, dispatches, seats  # noqa: E402

SEAT, SID = "revseat", "s-owed-row"
SENDER = "lead-seat"
# The words both surfaces open their owed-row sentence with. An arm that
# asserts this is absent is asserting the SURFACE is absent, so every such arm
# also carries a positive control that the same words appear when owed.
MARK = "you OWE"


def _ts(age_s):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_s))


class _OwedWorld(SeatsBase):
    """One joined seat, raw dispatch rows addressed to it, and the two real
    entry points a recipient meets: its Stop hook and its beacon."""

    def setUp(self):
        super().setUp()
        # THE SCRATCH REAPER IS OFF, set by SeatsBase (HELM_SCRATCH_GC=0).
        # These arms drive the real Stop hook, and a live reaper would delete
        # real dead-session scratch; asserted, so a base that stops setting it
        # reddens here instead of mutating the host.
        self.assertEqual(os.environ.get("HELM_SCRATCH_GC"), "0")
        # THE SENDER IS A LIVE, ROSTERED SEAT, as it was in the incident. A
        # sender that names no seat makes the DISPATCHER's check-in rung show
        # the row to every stopping seat (`_mine_or_unprovable` keeps an
        # unattributable row rather than strand it), which is a different
        # surface speaking with a sender's words; the first cut of this
        # fixture measured exactly that and nothing about the recipient.
        seats.join(session="s-lead", seat=SENDER, cwd="/tmp/p")
        seats.join(session=SID, seat=SEAT, cwd="/tmp/p")

    def git(self, repo, *args):
        p = subprocess.run(["git", "-C", repo, "-c", "user.email=t@t",
                            "-c", "user.name=t", *args], capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def plant(self, rid, lane, age_s=3600, recipient=SEAT, supersedes=None,
              tip=None, repo_id=None):
        """One OPEN review row, delivered, sent by another seat. Returns the
        12-hex id the surfaces print."""
        ts = _ts(age_s)
        row = {"v": 3, "event": "dispatch", "seq": 0, "id": rid, "ts": ts,
               "recipient": recipient, "lane": lane, "tip": tip or "a" * 40,
               "ref": tip or "a" * 40, "deadline_s": 2700, "status": "open",
               "kind": "review", "sender": SENDER}
        if supersedes:
            # A continuation carries its parent only as the SAME WORK
            # (`dispatches._same_chain`): a rootless legacy parent's child
            # roots at the parent's id, which is what `add` writes.
            row["supersedes"] = supersedes
            row["chain_root"] = supersedes
        if repo_id:
            row["repo_id"] = repo_id
        done = {"v": 3, "event": "delivered", "seq": 1, "id": rid, "ts": ts,
                "delivery_ref": "dm-x"}
        path = dispatches.ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            for r in (row, done):
                f.write(json.dumps(r) + "\n")
        return rid[:12]

    def stop(self, continuing=False, transcript=None):
        """One Stop through the hook verb, as the harness drives it. A new
        idle turn arrives with no stop_hook_active; the re-stop inside the
        SAME turn after a refusal arrives with it set. `transcript` is the
        payload's transcript_path, where the harness keeps the session's
        Workflow runs and background agents."""
        payload = {"session_id": SID}
        if continuing:
            payload["stop_hook_active"] = True
        if transcript:
            payload["transcript_path"] = transcript
        return self.cmd("stop-guard", ["--hook-json", "--seat", SEAT],
                        stdin=json.dumps(payload).encode())

    def wake(self, *flags):
        """One `helm chat wait --follow` pass, the doorbell's ring by default
        or one line per row with `--per-row` in `flags`: every line it emits
        is one Monitor event, which is one wake."""
        rc, out, err = self.cmd("wait", ["--seat", SEAT, "--follow",
                                         "--timeout", "0.3", *flags])
        self.assertEqual(rc, 0, err)
        return [ln for ln in out.splitlines() if ln.strip()]

    def assertNames(self, text, id12, where):
        self.assertIn(MARK, text, "%s never named the owed row:\n%s"
                      % (where, text))
        self.assertIn("helm dispatch triage %s" % id12, text,
                      "%s named no start command for %s:\n%s"
                      % (where, id12, text))


class IdleSeatHearsItsOwedRowTest(_OwedWorld):

    def test_every_idle_stop_names_the_row_and_the_command_that_starts_it(self):  # noqa: VACUOUS_ASSERTION — the loop runs a literal three turns and each turn asserts rc 2 plus the MARK and the triage command
        """THE SYMPTOM. Three idle turns end in a row, each a fresh turn (no
        stop_hook_active). Each one must be refused naming the row and
        `helm dispatch triage <row>`; on trunk the first printed a latched
        whisper with no start command and the next two ended unrefused with
        nothing about the row."""
        rid = self.plant("99e8f7a88945aa11", "review the canary")
        for turn in (1, 2, 3):
            rc, _o, err = self.stop()
            self.assertEqual(rc, 2, "idle turn %d ended unrefused:\n%s"
                             % (turn, err))
            self.assertNames(err, rid, "idle turn %d" % turn)

    def test_the_re_stop_inside_the_same_turn_passes(self):  # noqa: VACUOUS_ASSERTION — the first stop's assertNames is the unconditional positive control on the same stop-guard stderr observable
        """No loop. The harness marks the stop that follows a refusal with
        stop_hook_active, and that stop must pass: the row was named once for
        this turn."""
        rid = self.plant("a1a1a1a1a1a1a1a1", "review the canary")
        rc, _o, err = self.stop()
        self.assertEqual(rc, 2, err)
        self.assertNames(err, rid, "the idle stop")
        rc, _o, err = self.stop(continuing=True)
        self.assertEqual(rc, 0, err)
        self.assertNotIn(MARK, err)

    def test_an_auto_claimed_row_names_its_triage_and_speaks_again_unleased(self):
        """The other first stop. When the row's reviewed tip is provably NOT
        on trunk, the whisper's auto-claim takes the row's lease on the first
        idle stop, and that lease makes the row WORKING. So the claim line is
        the one place that stop can say how to START, and it must. Once the
        lease is gone and the row is still owed, the next idle turn names it
        again."""
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        for name, branch in (("seed", None), ("change", "review-tip")):
            if branch:
                self.git(repo, "checkout", "-q", "-b", branch)
            else:
                self.git(repo, "init", "-q", "-b", "main")
            with open(os.path.join(repo, name), "w") as f:
                f.write(name + "\n")
            self.git(repo, "add", name)
            self.git(repo, "commit", "-q", "-m", name)
        tip = self.git(repo, "rev-parse", "HEAD")
        self.git(repo, "checkout", "-q", "main")
        rid = self.plant("a9a9a9a9a9a9a9a9", "review the canary", tip=tip,
                         repo_id=os.path.join(repo, ".git"))
        rc, _o, err = self.stop()
        self.assertEqual(rc, 2, err)
        self.assertIn("auto-claimed dispatch a9a9a9a9", err)
        self.assertIn("helm dispatch triage " + rid, err,
                      "the auto-claim named no start command:\n" + err)
        self.assertNotIn(MARK, err, "one stop named the same row twice")
        lease = seats._live_claims().get("dispatch:a9a9a9a9")
        self.assertIsInstance(lease, dict, "the auto-claim took no lease")
        ok, msg = seats.release("dispatch:a9a9a9a9", SEAT,
                                lease=lease.get("lease"), session=SID)
        self.assertTrue(ok, msg)
        rc, _o, err = self.stop()
        self.assertEqual(rc, 2, err)
        self.assertNames(err, rid, "the idle turn after the lease ended")

    def test_two_owed_rows_are_both_named_oldest_first(self):
        new = self.plant("b2b2b2b2b2b2b2b2", "the newer review", age_s=600)
        old = self.plant("c3c3c3c3c3c3c3c3", "the older review", age_s=7200)
        rc, _o, err = self.stop()
        self.assertEqual(rc, 2, err)
        self.assertNames(err, old, "the stop")
        self.assertNames(err, new, "the stop")
        self.assertLess(err.index("helm dispatch triage " + old),
                        err.index("helm dispatch triage " + new),
                        "the older row must be named first:\n" + err)


class WorkingOrClosedIsNotNaggedTest(_OwedWorld):

    def test_a_seat_working_the_owed_row_is_not_nagged(self):  # noqa: VACUOUS_ASSERTION — the pre-claim stop's assertNames on both rows is the unconditional positive control for the absence asserted after
        """WORKING is `dispatches.progress_state`: the seat holds the row's
        own lease (the claim the auto-claim rung and `helm chat claim
        dispatch:<id8>` mint). The lease rung keeps its own once-per-held-set
        cadence; this surface adds nothing on top of it. A HELD row (the
        recipient acknowledged it and paused it on a named dependency) is not
        owed at all, so it is not named either."""
        rid = self.plant("d4d4d4d4d4d4d4d4", "review the canary")
        held = self.plant("e5e5e5e5e5e5e5e5", "review the other one")
        rc, _o, err = self.stop()                      # positive control
        self.assertNames(err, rid, "the stop before the claim")
        self.assertNames(err, held, "the stop before the hold")
        ok, msg, _lease = seats.claim("dispatch:" + rid[:8], SEAT,
                                      session=SID)
        self.assertTrue(ok, msg)
        _row, why = dispatches.mark_hold(held, "waiting on the fab box")
        self.assertIsNone(why, why)
        for turn in (1, 2):
            rc, _o, err = self.stop()
            self.assertNotIn(MARK, err, "turn %d nagged a worked row:\n%s"
                             % (turn, err))
            self.assertNotIn("helm dispatch triage " + held, err)

    def test_a_closed_or_superseded_row_is_never_named_again(self):  # noqa: VACUOUS_ASSERTION — the pre-closure stop's assertNames on both rows and the wake's assertIn on its own text are the positive controls
        gone = self.plant("f6f6f6f6f6f6f6f6", "the cancelled review")
        moved = self.plant("a7a7a7a7a7a7a7a7", "the superseded review")
        rc, _o, err = self.stop()                      # positive control
        self.assertNames(err, gone, "the stop before closure")
        self.assertNames(err, moved, "the stop before supersession")
        _row, why = dispatches.mark_cancel(gone, "moot: the lane was dropped")
        self.assertIsNone(why, why)
        self.plant("b8b8b8b8b8b8b8b8", "the superseded review",
                   recipient="other-seat", supersedes="a7a7a7a7a7a7a7a7")
        for turn in (1, 2):
            rc, _o, err = self.stop()
            self.assertNotIn(MARK, err, "turn %d named a finished row:\n%s"
                             % (turn, err))
            self.assertNotIn("helm dispatch triage " + gone, err)
            self.assertNotIn("helm dispatch triage " + moved, err)
        chat.post("@%s unrelated ping" % SEAT, who="bob", room="main")
        lines = self.wake()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("unrelated ping", lines[0])
        self.assertNotIn(MARK, lines[0])

    def test_a_seat_that_owes_nothing_gets_nothing_and_no_wake(self):  # noqa: VACUOUS_ASSERTION — the closing owing wake's assertNames is the unconditional positive control on the same wake observable
        rc, _o, err = self.stop()
        self.assertEqual(rc, 0, err)
        self.assertNotIn(MARK, err)
        self.assertEqual(self.wake(), [], "a clean idle seat was woken")
        chat.post("@%s unrelated ping" % SEAT, who="bob", room="main")
        lines = self.wake()
        self.assertEqual(len(lines), 1, lines)       # the mention itself
        self.assertIn("unrelated ping", lines[0])
        self.assertNotIn(MARK, lines[0])
        # positive control: the same wake, owing, does carry the row
        rid = self.plant("c9c9c9c9c9c9c9c9", "review the canary")
        chat.post("@%s second ping" % SEAT, who="bob", room="main")
        lines = self.wake()
        self.assertEqual(len(lines), 1, lines)
        self.assertNames(lines[0], rid, "the wake")


class BusyElsewhereTest(_OwedWorld):

    def test_a_seat_busy_on_another_lease_hears_each_owed_set_once(self):  # noqa: VACUOUS_ASSERTION — the first busy stop's assertNames and the post-new-row assertNames bracket the latched absence
        """A seat holding a lease on OTHER work is not idle, so a row sent to
        it is named once per owed set and not at every turn end: it is told
        what is queued without being pulled off its work each turn. A new row
        changes the set, and the set is named again."""
        ok, msg, _l = seats.claim("worktree-x", SEAT, ttl=600, session=SID)
        self.assertTrue(ok, msg)
        first = self.plant("d1d1d1d1d1d1d1d1", "review the canary")
        rc, _o, err = self.stop()
        self.assertEqual(rc, 2, err)
        self.assertNames(err, first, "the first busy stop")
        rc, _o, err = self.stop()
        self.assertNotIn(MARK, err, "the same set re-nagged a busy seat")
        second = self.plant("e2e2e2e2e2e2e2e2", "review the next one",
                            age_s=60)
        rc, _o, err = self.stop()
        self.assertNames(err, first, "the stop after a new row")
        self.assertNames(err, second, "the stop after a new row")


class DelegatedWorkIsBusyTest(_OwedWorld):
    """A seat whose Workflow run or background agent is still running is not
    idle (task/3696).

    MEASURED on the integrator seat: the owed-row block fired three times on
    one row while a Workflow the recipient launched was reviewing it, and the
    only move that quieted it was taking the row's lease again, which marks
    nothing the Workflow was not already doing. Such a seat is BUSY, so it
    hears each owed set once, and a set it was already told at an idle turn
    is not said again. When the run ends the seat is idle again and the nag
    comes back at every idle turn.

    The harness keeps a session's delegates beside its transcript: a Workflow
    run's agents under `<session>/subagents/workflows/<run>/` and the run's
    end as `<session>/workflows/<run>.json`; a background agent's transcript
    under `<session>/subagents/`, and its end as the SubagentStop tombstone
    helm's delegation-stop hook writes."""

    def transcript(self):
        """The session's transcript path, laid out as the harness lays it
        out: `<dir>/<session>.jsonl` beside the folder `<dir>/<session>/`."""
        d = os.path.join(self.tmp, "projects", "a-project")
        os.makedirs(os.path.join(d, SID), exist_ok=True)
        path = os.path.join(d, SID + ".jsonl")
        open(path, "a").close()
        return path

    @staticmethod
    def write(path, text="{}\n"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def run_started(self, transcript, run="wf_0001-abc"):
        """A Workflow run with one agent writing its transcript."""
        folder = os.path.join(transcript[:-len(".jsonl")], "subagents",
                              "workflows", run)
        self.write(os.path.join(folder, "agent-a1f00d.jsonl"))
        return folder

    def run_ended(self, transcript, run="wf_0001-abc"):
        self.write(os.path.join(transcript[:-len(".jsonl")], "workflows",
                                run + ".json"), '{"status": "completed"}')

    def test_a_running_workflow_is_told_nothing_new_and_its_end_re_arms(self):  # noqa: VACUOUS_ASSERTION — the idle stops before and after the run assert rc 2 and the owed row by name on the same stderr observable the busy stops assert absent
        tr = self.transcript()
        rid = self.plant("a3a3a3a3a3a3a3a3", "review the canary")
        rc, _o, err = self.stop(transcript=tr)       # idle: told, as always
        self.assertEqual(rc, 2, err)
        self.assertNames(err, rid, "the idle stop before the run")
        self.run_started(tr)
        for turn in (1, 2):
            rc, _o, err = self.stop(transcript=tr)
            self.assertNotIn(MARK, err, "busy turn %d re-told a set the seat "
                             "already heard while its Workflow works it:\n%s"
                             % (turn, err))
        self.run_ended(tr)
        rc, _o, err = self.stop(transcript=tr)
        self.assertEqual(rc, 2, "the idle stop after the run ended unrefused:"
                         "\n" + err)
        self.assertNames(err, rid, "the idle stop after the run")

    def test_a_background_agent_is_busy_until_its_SubagentStop(self):  # noqa: VACUOUS_ASSERTION — the first busy stop and the stop after SubagentStop both assert the owed row by name on the same observable
        from helm import seats_delegation
        tr = self.transcript()
        self.write(os.path.join(tr[:-len(".jsonl")], "subagents",
                                "agent-a2b0c1.jsonl"))
        rid = self.plant("a4a4a4a4a4a4a4a4", "review the canary")
        rc, _o, err = self.stop(transcript=tr)       # busy, set not told yet
        self.assertEqual(rc, 2, err)
        self.assertNames(err, rid, "the first busy stop")
        rc, _o, err = self.stop(transcript=tr)
        self.assertNotIn(MARK, err, "the same set re-told a seat whose "
                         "background agent is still running:\n" + err)
        self.assertTrue(seats_delegation._mark_agent_stopped(SID, "a2b0c1"))
        rc, _o, err = self.stop(transcript=tr)
        self.assertEqual(rc, 2, err)
        self.assertNames(err, rid, "the idle stop after SubagentStop")

    def test_an_orphan_run_left_quiet_for_a_week_is_not_work(self):  # noqa: VACUOUS_ASSERTION — the loop runs a literal two turns and each asserts rc 2 and the owed row by name
        """THE TRUE ALARM STAYS. A run folder with no end file whose agents
        have written nothing for a week belongs to a session that died
        mid-run; it is not work, so the seat is idle and hears the row at
        every idle turn."""
        tr = self.transcript()
        folder = self.run_started(tr)
        old = time.time() - 7 * 86400
        for p in [os.path.join(folder, n) for n in os.listdir(folder)] \
                + [folder]:
            os.utime(p, (old, old))
        rid = self.plant("a5a5a5a5a5a5a5a5", "review the canary")
        for turn in (1, 2):
            rc, _o, err = self.stop(transcript=tr)
            self.assertEqual(rc, 2, "idle turn %d ended unrefused:\n%s"
                             % (turn, err))
            self.assertNames(err, rid, "idle turn %d" % turn)


class HoldingTheLeaseIsInProgressTest(_OwedWorld):
    """The lease the owed-row block tells a seat to take is the in-progress
    mark, so the lease rung must not answer it with an act (task/3696).

    MEASURED on the integrator seat: with the lease on an owed row held, a
    stop printed it as a line to act on ("act per line, then stop ...
    helm chat release ...") and, while the resident refolded, as "what this
    claim is owed is UNKNOWN [stop-facts ABSENT]". The owed-row rung asks
    for the lease and the lease rung refused the stop for holding it.

    A held `dispatch:` lease is now IN PROGRESS unless the resident's EXACT
    facts prove its row discharged (answered, cancelled, rebound, retired or
    carried), which is when releasing it is the act a stop owes; and a lease
    about to expire still refuses, as every lease line does."""

    def claim_row(self, rid, ttl=None):
        kw = {"ttl": ttl} if ttl else {}
        ok, msg, lease = seats.claim("dispatch:" + rid[:8], SEAT,
                                     session=SID, **kw)
        self.assertTrue(ok, msg)
        return lease

    def test_the_lease_on_a_row_still_owed_is_no_act_at_stop(self):  # noqa: VACUOUS_ASSERTION — the pre-claim stop asserts rc 2 and the owed row on the same observable, and the first stop after the claim must name the lease IN PROGRESS
        rid = self.plant("b1b1b1b1b1b1b1b1", "review the canary")
        rc, _o, err = self.stop()                    # positive control
        self.assertEqual(rc, 2, err)
        self.assertNames(err, rid, "the stop before the claim")
        self.claim_row(rid)
        rc, _o, err = self.stop()
        self.assertEqual(rc, 0, "the stop refused the in-progress mark it "
                         "asked for:\n" + err)
        self.assertIn("dispatch:" + rid[:8], err)
        self.assertIn("IN PROGRESS", err)
        rc, _o, err = self.stop()
        self.assertEqual(rc, 0, err)
        self.assertNotIn("IN PROGRESS", err, "an unchanged mark was printed "
                         "again at the next stop")
        self.assertNotIn(MARK, err)

    def test_absent_stop_facts_never_make_the_mark_a_block(self):  # noqa: VACUOUS_ASSERTION — the first stop asserts the IN PROGRESS line and its stop-facts note, the positive control for the reprint absence after
        rid = self.plant("b2b2b2b2b2b2b2b2", "review the canary")
        self.claim_row(rid)
        self.fresh_resident.off()       # no resident behind the stop
        rc, _o, err = self.stop()
        self.assertEqual(rc, 0, "a stop without stop facts refused the "
                         "in-progress mark:\n" + err)
        self.assertIn("IN PROGRESS", err)
        self.assertIn("stop-facts", err, "it must say why it could not rule")
        self.fresh_resident.start()     # the resident catches up: EXACT
        rc, _o, err = self.stop()
        self.assertEqual(rc, 0, err)
        self.assertNotIn("IN PROGRESS", err, "facts arriving reprinted an "
                         "unchanged mark")

    def test_a_lease_about_to_expire_on_a_row_still_owed_still_refuses(self):
        """KEPT: an expiring lease on owed work says so, as a refusal."""
        rid = self.plant("b3b3b3b3b3b3b3b3", "review the canary")
        self.claim_row(rid, ttl=60)
        rc, _o, err = self.stop()
        self.assertEqual(rc, 2, err)
        self.assertIn("dispatch:" + rid[:8], err)
        self.assertIn("EXPIRING", err)

    def test_a_lease_whose_row_was_answered_refuses_once_with_its_release(self):  # noqa: VACUOUS_ASSERTION — the stop after the cancel asserts rc 2 and the release command, the positive control for the quiet stop after it
        """KEPT: once EXACT facts prove the row discharged, the release is
        the act, and the stop refuses once naming it."""
        rid = self.plant("b4b4b4b4b4b4b4b4", "review the canary")
        self.claim_row(rid)
        self.stop()
        _row, why = dispatches.mark_cancel(rid, "moot: the lane was dropped")
        self.assertIsNone(why, why)
        rc, _o, err = self.stop()
        self.assertEqual(rc, 2, err)
        self.assertIn("helm chat release dispatch:" + rid[:8], err)
        self.assertIn("CANCELLED", err)
        rc, _o, err = self.stop()
        self.assertEqual(rc, 0, "an unchanged discharged lease refused "
                         "again:\n" + err)

    def test_a_lease_under_the_sessions_other_name_is_in_progress(self):
        """THE RULING IS THE SESSION'S, NOT ONE OF ITS NAMES'. The resident
        rules a claim for its holder and for the session's roster seat. A
        process that still answers to an older name takes the lease under
        that name while the roster, and so the dispatcher, knows the session
        as SEAT: the holder's ruling reads the row REBOUND to SEAT, and
        SEAT's reads it OWED. The row is this session's, so the lease is its
        in-progress mark; the stop refuses once every ruling made for the
        claim reads the row discharged."""
        rid = self.plant("b5b5b5b5b5b5b5b5", "review the canary")
        ok, msg, _lease = seats.claim("dispatch:" + rid[:8], SEAT + "-old",
                                      session=SID)
        self.assertTrue(ok, msg)
        rc, _o, err = self.stop()
        self.assertEqual(rc, 0, "a lease under the session's other name was "
                         "refused as if its row were rebound:\n" + err)
        self.assertIn("dispatch:" + rid[:8], err)
        self.assertIn("IN PROGRESS", err)
        # KEPT: once the row is discharged, every ruling reads it STALE.
        row, why = dispatches.mark_cancel(rid, "moot: the lane was dropped")
        self.assertIsNone(why, why)
        self.assertTrue(row, "the cancel returned no row")
        rc, _o, err = self.stop()
        self.assertEqual(rc, 2, err)
        self.assertIn("CANCELLED", err)


class TheWakeNamesTheOwedRowTest(_OwedWorld):

    def test_a_wake_while_owing_names_the_owed_row_on_the_same_line(self):
        """One Monitor line is one wake, so the owed row rides the SAME line
        as the event that woke the seat. A second line would be a second wake
        that nothing addressed."""
        rid = self.plant("f3f3f3f3f3f3f3f3", "review the canary")
        chat.post("@%s unrelated ping" % SEAT, who="bob", room="main")
        lines = self.wake()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("unrelated ping", lines[0])
        self.assertNames(lines[0], rid, "the wake")

    def test_a_ring_for_an_owing_seat_names_the_row_before_its_tail(self):
        """The beacon's default wake is the doorbell's ring, and its line
        ENDS with the fixed `(+N waiting — helm chat read ...)` tail that
        readers strip. So the owed clause sits between the lead and that
        tail: stripping the tail leaves the line ending on the owed row's
        start command, and the clause is said once."""
        from helm import beacon_doorbell
        rid = self.plant("f4f4f4f4f4f4f4f4", "review the canary")
        chat.post("@%s unrelated ping" % SEAT, who="bob", room="main")
        lines = self.wake()
        self.assertEqual(len(lines), 1, lines)
        line = lines[0]
        self.assertNames(line, rid, "the ring")
        self.assertEqual(line.count(MARK), 1, line)
        tail = beacon_doorbell._TAIL.search(line)
        self.assertIsNotNone(tail, "the ring does not end with its tail:\n"
                             + line)
        self.assertRegex(tail.group(0), r"doorbell: \d+ unread")
        self.assertLess(line.index("unrelated ping"), line.index(MARK), line)
        self.assertLess(line.index(MARK), tail.start(), line)
        self.assertTrue(line[:tail.start()].endswith(
            "helm dispatch triage " + rid), line)

    def test_a_per_row_wake_names_the_row_before_its_waiting_tail(self):  # noqa: VACUOUS_ASSERTION — len(lines) == 3 is asserted first, so both loops run over literal rows and the tail found on lines 1-2 is the positive control for its absence on line 3
        """`--per-row` streams one line per addressed row, and a line with
        rows behind it ENDS with the `(+N waiting — helm chat read)` tail
        delivery renders. The owed clause sits between the row and that tail,
        as it does on the ring, so the tail stays last and promptshape's strip
        removes exactly the tail and leaves the clause. The last row has
        nothing behind it, so its line has no tail and the clause ends it."""
        from helm import promptshape
        rid = self.plant("f5f5f5f5f5f5f5f5", "review the canary")
        for n in (1, 2, 3):
            chat.post("@%s ping %d" % (SEAT, n), who="bob", room="main")
        lines = self.wake("--per-row")
        self.assertEqual(len(lines), 3, lines)
        for n, line in enumerate(lines, 1):
            self.assertNames(line, rid, "per-row line %d" % n)
            self.assertEqual(line.count(MARK), 1, line)
            self.assertLess(line.index("ping %d" % n), line.index(MARK), line)
        for line, waiting in zip(lines, (2, 1)):
            tail = promptshape._WAKE_TAIL.search(line)
            self.assertIsNotNone(tail, "a line with rows behind it does not "
                                 "end with its tail:\n" + line)
            self.assertEqual(tail.group(0),
                             " (+%d waiting — helm chat read)" % waiting, line)
            self.assertLess(line.index(MARK), tail.start(), line)
            self.assertEqual(promptshape._WAKE_TAIL.sub("", line),
                             line[:tail.start()], line)
            self.assertTrue(line[:tail.start()].endswith(
                "helm dispatch triage " + rid), line)
        self.assertIsNone(promptshape._WAKE_TAIL.search(lines[2]), lines[2])
        self.assertTrue(lines[2].endswith("helm dispatch triage " + rid),
                        lines[2])


if __name__ == "__main__":
    unittest.main()
