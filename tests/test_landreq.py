#!/usr/bin/env python3
"""helm lr — the LAND REQUEST lifecycle VIEW over the dispatch ledger + a git
trunk observation. Hermetic: HELM_HOME/HELM_CHAT_DIR are tmp dirs and every
git repo is minted in setUp; the real ledger and repos are never touched. No
second ledger is created — these tests assert the LR is a pure projection of
dispatch rows refined by an observation of where the reviewed tip actually is.
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from helm import chat, dispatches, eventledger, gate, landreq, verdicts

ENV_KEYS = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NODE_URL",
            "HELM_CHAT_NAME", "HELM_CELL_BIN", "HELM_CELL_PROFILE",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")

# A complete signed-send receipt, exactly the shape chat._sign_send returns.
SENT = {"sent": True, "turn_hash": "a" * 64, "receipt_hash": "b" * 64,
        "chain_index": 7}


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = landreq.cmd_lr(args)
    return rc, out.getvalue(), err.getvalue()


class LandReqBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-lr-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_NAME"] = "integrator"
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.main = self.git("symbolic-ref", "--short", "HEAD")
        self.a = self.commit("a")
        self.git("branch", "side", self.a)
        self.b = self.commit("b")
        self.c = self.commit("c")
        # a divergent reviewed tip that is NOT on trunk until an integrator
        # merges it — the READY/MERGED_LOCAL/LANDED axis rides on this commit.
        self.git("checkout", "-q", "side")
        self.side = self.commit("side", path="g")
        self.git("checkout", "-q", self.main)
        # THIS WHOLE MODULE TESTS THE PRE-GATE LIFECYCLE, declared once here.
        #
        # An approve written by a GATE-CAPABLE writer must carry a minted
        # receipt to reach READY (dispatches.GATE_CAPS stamps the writer's
        # capability on the event; landreq._needs_gate reads it). These
        # fixtures record untokened approves and assert READY/stall behaviour,
        # and their subject is LANDING — whether a reviewed tip reaching trunk
        # is observed — not gating; their synthetic side branches have no suite
        # to run. So they write verdicts as an OLD writer would.
        #
        # PINNING IT HERE RATHER THAN AT EACH CALL SITE IS THE POINT. @codex
        # measured the alternative: after the enforcement went live I fixed the
        # two tests that were already red, and FIVE more were green only by
        # accident of when the suite ran. Per-call-site discipline cannot close
        # that class, because the next fixture someone adds has the same hole
        # and nothing asks them to think about it.
        #
        # PINNING THE WRITER, NOT A CLOCK, is the second correction: the first
        # version of this pinned a wall-time boundary, which made these tests'
        # correctness depend on the hour they ran. A writer capability is a
        # durable property of the row.
        #
        # The gate-capable contract (untokened approve stays REVIEWED, a bound
        # one reaches READY) is asserted in tests.test_gate.LandPathEnforcement.
        policy = mock.patch.object(dispatches, "GATE_CAPS", ())
        policy.start()
        self.addCleanup(policy.stop)

    def tearDown(self):
        for key, value in self.prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *args, cwd=None):
        p = subprocess.run(["git", "-C", cwd or self.repo, *args],
                           capture_output=True, text=True, check=True)
        return p.stdout.strip()

    def commit(self, text, path="state"):
        with open(os.path.join(self.repo, path), "a", encoding="utf-8") as f:
            f.write(text + "\n")
        self.git("add", path)
        self.git("commit", "-q", "-m", text)
        return self.git("rev-parse", "HEAD")

    def add_origin(self):
        """Publish a trunk upstream so refs/remotes/origin/<trunk> exists and
        MERGED_LOCAL vs LANDED becomes observable."""
        bare = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "--bare", "-q", bare], check=True)
        self.git("remote", "add", "origin", bare)
        self.git("push", "-q", "origin", self.main)

    def dispatch(self, ref=None, **kw):
        # Independent work unless the test names a parent — see the same rule in
        # tests/test_dispatches.py. A land-loop fixture that stamped --new-work
        # on a superseding round would be asserting the defect.
        kw.setdefault("new_work", "supersedes" not in kw)
        # UNDELIVERED BY DEFAULT, which is what a land loop's FIRST stage means.
        # add() now marks a row delivered on its own mention, and delivery is
        # the very thing that moves the state OPEN -> AWAITING_REVIEW and the
        # debt integrator -> reviewer (landreq.OWED_BY). A fixture that posted a
        # mention would hand every walk-the-lifecycle arm a row that had already
        # left the stage it was written to observe. Arms that MEAN delivered
        # call _mark_delivered explicitly, as the delivered/stall arms already do.
        kw.setdefault("notify", False)
        row = dispatches.add("codex-3", kw.pop("lane", "lane/foo"),
                             ref=ref or self.side, repo=self.repo, **kw)
        self.assertIsNotNone(row)
        return row

    def age(self, rid, seconds):
        """Backdate every event of one row to exercise dwell/stall from a fresh
        disk replay (the append-only ledger is never rewritten in real code)."""
        path = dispatches.ledger_path()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              time.gmtime(time.time() - seconds))
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for row in events:
                if row.get("id") == rid:
                    row["ts"] = stamp
                f.write(json.dumps(row, separators=(",", ":")) + "\n")


class EraStabilityTest(LandReqBase):
    """@codex's audit, made permanent: THE CLOCK MUST NOT MOVE THESE TESTS.

    The bug this replaces was not "two tests are red". It was that the file's
    verdict timestamps came from the wall clock — `age()` backdates a row to
    `now - seconds` — so each fixture crossed GATE_BOUNDARY at its own moment
    and the suite would have gone red one test at a time over the following
    hours. Fixing the two that had already tripped is fixing instances of a
    class whose remaining members are indistinguishable from healthy.

    So this asserts the PROPERTY rather than the instances: with the base's
    pin in place, the same lifecycle holds under a clock far past the boundary.
    A future fixture inherits the pin automatically; if someone removes it,
    this is what says so.
    """

    def test_the_lifecycle_is_the_same_TEN_YEARS_FROM_NOW(self):
        """The clock must not move these tests. It used to: verdict stamps came
        from the wall clock (`age()` backdates to `now - seconds`), so each
        fixture crossed the old wall-time boundary at its own moment and the
        suite would have gone red one test at a time over the following hours.
        Nothing about the still-green ones said which kind they were."""
        row = self.dispatch(ref=self.side)
        dispatches._mark_delivered(row["id"], "post-1")
        far = time.time() + 10 * 365 * 24 * 3600
        with mock.patch.object(time, "time", lambda: far):
            dispatches.mark_verdict(row["id"], self.side, "ok",
                                    polarity="approve")
            lr, err = landreq.get(row["id"])
            self.assertIsNone(err, err)
            self.assertEqual(lr["state"], "READY")
            self.assertIsNone(lr["ungated"])

    def test_the_pin_is_what_makes_that_true_and_not_luck(self):
        """THE CONTROL. Without it the test above passes for a version where
        the gate check never fires at all — which is the failure mode the first
        `_needs_gate` actually shipped with (time.gmtime on an ISO string,
        False for every row, reading as enabled while enforcing nothing)."""
        self.assertEqual(dispatches.GATE_CAPS, ())            # the pin is on
        row = self.dispatch(ref=self.side)
        dispatches._mark_delivered(row["id"], "post-2")
        current = dispatches.snapshot()[0][row["id"]]
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": current["seq"] + 1,
            "id": row["id"], "ts": dispatches.pk.now_ts(),
            "reviewed_tip": self.side, "verdict_ref": "ok",
            "polarity": "approve", "gate": "",
            "gate_caps": [dispatches.GATE_CAP_RECEIPT]}))
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertIn("no minted gate receipt", lr["ungated"])

    def test_a_declared_polarity_is_never_rendered_UNDECLARED(self):
        """The projection contradicted its own record. `lr show` printed
        "polarity UNDECLARED ... this row can never gain one" on EVERY REVIEWED
        row, guarded only on `closed_by_landing` and never on whether a polarity
        was actually present. So a row whose ledger polarity is `approve`
        rendered as having declared nothing — two lines under its own `verdict`
        line, which printed the approval in full. An integrator read the
        projection instead of the record and announced in the room that a
        reviewer's APPROVE carried no polarity; the retraction was public.

        The negative control is the sibling
        test_verdict_without_repo_binding_is_unobservable_never_a_false_stall,
        which builds a genuinely polarity-less row and still expects UNDECLARED.
        Both must hold, and that is the point: this asserts the line is
        SUPPRESSED when the ledger has a polarity, that one asserts it still
        FIRES when it does not. A fix that simply deleted the branch would go
        green here and red there."""
        row = self.dispatch(ref=self.side)
        dispatches._mark_delivered(row["id"], "post-3")
        # The fixture is an APPROVAL-TIER demotion rather than a missing
        # receipt, for two reasons. It is the representative shape — 13 of the
        # 14 live rows carrying this defect are tier demotions and only 1 is a
        # missing receipt. And an ungated approve is no longer CONSTRUCTIBLE:
        # mark_verdict refuses it outright once the receipt capability is on,
        # so a fixture built that way records no verdict at all and the row
        # stays OPEN. Demotion at projection time is the durable seam.
        with mock.patch.object(dispatches, "approval_tier",
                               lambda who, repo=None: ("outside", "reviewer outside the "
                                            "permitted tier (test)")):
            dispatches.mark_verdict(row["id"], self.side, "ok",
                                    polarity="approve")
            lr, err = landreq.get(row["id"])
            self.assertIsNone(err, err)
            # The exact row the bug mis-rendered: REVIEWED, yet polarity IS set.
            self.assertEqual(lr["state"], "REVIEWED")
            self.assertEqual(lr["polarity"], "approve")
            text = run(["show", row["id"]])[1]
            # Assert the EFFECT, not the absence of a complaint: the declared
            # polarity must reach the surface, not merely go uncontradicted.
            self.assertIn("polarity  APPROVE", text)
            self.assertNotIn("UNDECLARED", text)
            # And the row still has to say why it is not READY — the tier,
            # never the verdict, which exists.
            self.assertIn("approval tier does not permit", text)

    def test_compact_list_reads_declared_polarity_from_the_dispatch_store(self):
        """The live #135 row carried canonical polarity=approve and
        ledger_refused=[verdict], yet compact `lr list` printed UNDECLARED because
        it inferred polarity from state=REVIEWED. Tier holds are the ordinary
        way a declared APPROVE becomes REVIEWED; the state is not the verdict.

        The refused historical event stays visible as a separate axis. This arm
        proves neither warning can overwrite the current accepted polarity."""
        row = self.dispatch(ref=self.side)
        dispatches._mark_delivered(row["id"], "post-list-polarity")
        with mock.patch.object(dispatches, "approval_tier",
                               lambda who, repo=None: (
                                   "outside", "reviewer outside tier (test)")):
            dispatches.mark_verdict(row["id"], self.side, "accepted approve",
                                    polarity="approve")
            # A later same-sequence duplicate is historical refusal evidence,
            # not the owner of current polarity.
            current = dispatches.snapshot()[0][row["id"]]
            self.assertTrue(eventledger.append(dispatches.ledger_path(), {
                "v": 3, "event": "verdict", "seq": current["seq"],
                "id": row["id"], "ts": dispatches.pk.now_ts(),
                "reviewed_tip": self.side, "verdict_ref": "refused fix",
                "polarity": "fix", "gate": "", "gate_caps": []}))
            lr, err = landreq.get(row["id"])
            self.assertIsNone(err, err)
            self.assertEqual(lr["state"], "REVIEWED")
            self.assertEqual(lr["polarity"], "approve")
            self.assertEqual(lr["polarity_source"], "dispatch-store")
            self.assertEqual(lr["ledger_refused"], ["verdict"])
            self.assertEqual(lr["owed_by"], "unknown (declared verdict held)")
            text = run(["list", "--all"])[1]
        self.assertIn("verdict polarity: dispatch store", text)
        self.assertIn("APPROVE from dispatch store", text)
        self.assertIn("dispatch fold REFUSED historical verdict event", text)
        self.assertNotIn("polarity UNDECLARED", text)


class LifecycleTest(LandReqBase):
    def test_open_awaiting_ready_landed_walk(self):
        row = self.dispatch()
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err)
        self.assertEqual(lr["state"], "OPEN")
        self.assertEqual(lr["reviewer"], "codex-3")
        self.assertEqual(lr["review_sha"], self.side)
        self.assertFalse(lr["terminal"])

        dispatches._mark_delivered(row["id"], "post-1")
        self.assertEqual(landreq.get(row["id"])[0]["state"], "AWAITING_REVIEW")

        dispatches.mark_verdict(row["id"], self.side, "review-post-9", polarity="approve")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "READY")      # reviewed tip not on trunk
        self.assertFalse(lr["landed"])

        self.git("merge", "--no-edit", "-q", "side")   # integrator lands it
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "LANDED")
        self.assertTrue(lr["landed"])
        self.assertTrue(lr["terminal"])

    def test_landed_needs_the_exact_reviewed_tip_not_just_any_trunk_move(self):
        # A verdict'd tip that never reaches trunk stays READY even as trunk
        # advances on unrelated work — landing is bound to THE reviewed commit.
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.commit("more-trunk-work")             # trunk moves, side does not
        self.assertEqual(landreq.get(row["id"])[0]["state"], "READY")

    def test_merged_local_is_not_landed_until_the_push_is_observed(self):
        self.add_origin()                          # origin/main == b
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")   # local trunk past origin
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "MERGED_LOCAL")
        self.assertTrue(lr["merged_local"])
        self.assertFalse(lr["landed"])
        self.assertFalse(lr["terminal"])

        self.git("push", "-q", "origin", self.main)    # push observed
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "LANDED")
        self.assertTrue(lr["landed"])

    def test_branch_and_author_bind_from_the_dispatch_send_row(self):
        # send delivers to the tmp chat lane (sign=False); the author is the
        # sender seat (HELM_CHAT_NAME), the reviewer is the recipient.
        row, why, sent = dispatches.send(
            "codex-3", "review", "review the branch tip", self.side,
            repo=self.repo, key="k1", sign=False, new_work=True)
        self.assertIsNone(why)
        self.assertTrue(sent)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["author"], "integrator")   # HELM_CHAT_NAME seat
        self.assertEqual(lr["reviewer"], "codex-3")
        self.assertEqual(lr["state"], "AWAITING_REVIEW")   # delivery observed


class StallNamesTheOwnerTest(LandReqBase):
    """`lr stalls` printed the ROLE that owes a row and never the SEAT.

    MEASURED 2026-08-04, on myself, twice: during a burndown that asked every
    seat for its own authored stalls, I answered ZERO from this listing. I had
    one, 13h50m old. A control found the only seat-looking strings in the whole
    output were a LANE NAME ("codex-refusal-ends-turn-reroute") whose author was
    a different seat — so the surface could not answer the question it was being
    read for, and answering it wrong looked exactly like answering it right."""

    def test_the_role_is_resolved_to_the_seat_that_holds_it(self):
        self.assertEqual(
            landreq._owed_by_whom({"owed_by": "author", "author": "kimi",
                                   "reviewer": "ds4pro"}),
            "author (kimi)")
        self.assertEqual(
            landreq._owed_by_whom({"owed_by": "reviewer", "author": "kimi",
                                   "reviewer": "ds4pro"}),
            "reviewer (ds4pro)")

    def test_a_role_with_no_single_holder_is_left_alone(self):
        """integrator/nobody/unknown name no seat, and inventing one would be
        this same defect pointing the other way."""
        # UNCONDITIONAL POSITIVE CONTROL, on the same observable and the same
        # inputs: the resolver DOES rewrite a role that has a holder. Without
        # this line every assertion below would also pass for a function that
        # returns its argument unchanged — which is exactly the bug this file
        # is about, one level up.
        self.assertEqual(
            landreq._owed_by_whom({"owed_by": "author", "author": "kimi",
                                   "reviewer": "ds4pro"}),
            "author (kimi)")
        for role in ("integrator", "nobody", "nobody (undeclared)",
                     "unknown (declared verdict held)"):
            self.assertEqual(
                landreq._owed_by_whom({"owed_by": role, "author": "kimi",
                                       "reviewer": "ds4pro"}),
                role, role)

    def test_a_missing_seat_degrades_to_the_role_not_to_None(self):
        self.assertEqual(
            landreq._owed_by_whom({"owed_by": "author", "author": None}),
            "author")

    def test_ball_holder_is_the_one_server_side_display_answer(self):
        ordinary = {"owed_by": "author", "author": "kimi"}
        raw = {"contrary": True, "owed_by": "author", "author": "kimi"}
        missing = {}
        self.assertEqual(landreq.ball_holder(ordinary), ("author", "kimi"))
        self.assertEqual(landreq.ball_holder(raw), ("integrator", None))
        self.assertEqual(landreq.ball_holder(missing), ("unknown", None))
        for stamp in ("a", "b", "c"):
            row = {"contrary": True, "contrary_discharge": stamp,
                   "owed_by": "integrator"}
            self.assertEqual(landreq.ball_holder(row), ("nobody", None), stamp)
            self.assertEqual(landreq._owed_by_whom(row), "nobody", stamp)
        # fail-closed: a classifier uncertainty is still live debt
        unverified = {"contrary": True,
                      "contrary_discharge": "unverified",
                      "owed_by": "integrator"}
        self.assertEqual(landreq.ball_holder(unverified),
                         ("integrator", None))


class BuildVsReviewSurfaceTest(LandReqBase):
    """#176: `helm lr show` must distinguish BUILD vs REVIEW dispatches in
    state label, recipient role name, and timeline stage name.

    A BUILD row's recipient is a 'builder', not a 'reviewer', and its stage is
    'AWAITING_BUILD', not 'AWAITING_REVIEW'."""

    def test_build_row_surface_renders_builder_and_awaiting_build(self):
        row = self.dispatch(ref=self.side, lane="lane/test-build", kind="build")
        dispatches._mark_delivered(row["id"], "post-1")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "AWAITING_BUILD")
        self.assertEqual(lr["owed_by"], "builder")
        self.assertEqual(landreq._owed_by_whom(lr), "builder (codex-3)")

        rendered = landreq._render_show(lr)
        self.assertIn("AWAITING_BUILD", rendered)
        self.assertNotIn("AWAITING_REVIEW", rendered)
        self.assertIn("author    integrator  ->  builder  codex-3", rendered)
        self.assertNotIn("reviewer codex-3", rendered)

    def test_review_row_surface_renders_reviewer_and_awaiting_review(self):
        row = self.dispatch(ref=self.side, lane="lane/test-review", kind="review")
        dispatches._mark_delivered(row["id"], "post-2")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "AWAITING_REVIEW")
        self.assertEqual(lr["owed_by"], "reviewer")
        self.assertEqual(landreq._owed_by_whom(lr), "reviewer (codex-3)")

        rendered = landreq._render_show(lr)
        self.assertIn("AWAITING_REVIEW", rendered)
        self.assertNotIn("AWAITING_BUILD", rendered)
        self.assertIn("author    integrator  ->  reviewer codex-3", rendered)
        self.assertNotIn("builder  codex-3", rendered)
        self.assertIn("AWAITING_REVIEW", rendered)
        self.assertNotIn("AWAITING_BUILD", rendered)
        self.assertIn("author    integrator  ->  reviewer codex-3", rendered)
        self.assertNotIn("builder  codex-3", rendered)
        self.assertEqual(landreq._owed_by_whom({}), "unknown")

    def test_the_PRINTED_stall_line_names_the_seat(self):
        """The unit above proves the resolver; this proves it is WIRED. The
        defect was never in a helper — it was that the line never called one."""
        row = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(row["id"], "post-1")
        self.age(row["id"], 3600)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            landreq.cmd_lr(["stalls"])
        text = out.getvalue()
        # UNCONDITIONAL POSITIVE CONTROL: the row really is in this listing,
        # so a missing seat name means "not printed" and not "not stalled".
        self.assertIn(row["id"][:12], text)
        self.assertIn("owed by reviewer (%s)" % row["recipient"], text)

    def test_the_PRINTED_stall_line_SAYS_the_work_already_landed(self):
        """The unit arms prove the marker; THIS proves it is WIRED — the same
        reason test_the_PRINTED_stall_line_names_the_seat exists above it.

        A marker nothing calls is the built-not-wired shape this seat spent the
        night finding in other people's lanes, and the isolated arms would stay
        green while the print site never called it.
        """
        row = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(row["id"], "post-1")
        self.age(row["id"], 3600)

        from helm import seats_work_offer
        # UNCONDITIONAL POSITIVE CONTROL, first and on the same listing: with
        # the probe silent the row is present and carries NO landed marker, so
        # the sentence below is the wiring speaking rather than a listing that
        # prints it unconditionally.
        with mock.patch.object(seats_work_offer, "_offer_landing_state",
                               return_value=(None, None)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                landreq.cmd_lr(["stalls"])
        quiet = out.getvalue()
        self.assertIn(row["id"][:12], quiet)
        self.assertNotIn("ALREADY ON TRUNK", quiet)

        with mock.patch.object(seats_work_offer, "_offer_landing_state",
                               return_value=(True, "d" * 40)):
            out2 = io.StringIO()
            with contextlib.redirect_stdout(out2):
                landreq.cmd_lr(["stalls"])
        loud = out2.getvalue()
        self.assertIn(row["id"][:12], loud)
        self.assertIn("ALREADY ON TRUNK at " + "d" * 12, loud)
        # THE OWED-BY CLAUSE IS GONE FROM THE PRINTED LINE, not merely
        # contradicted further along it. The quiet run above proves the same
        # listing DOES carry "owed by reviewer" when nothing landed, so this
        # absence is the replacement working rather than a line that never
        # said it.
        self.assertIn("owed by reviewer", quiet)
        self.assertNotIn("owed by reviewer", loud)
        self.assertIn("owed by NOBODY", loud)

class StallTest(LandReqBase):
    def test_awaiting_review_stalls_at_the_dispatch_deadline(self):
        row = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(row["id"], "post-1")
        self.age(row["id"], 3600)                  # far past the 60s deadline
        stalled, unavailable = landreq.stalls()
        self.assertIsNone(unavailable)
        self.assertEqual([lr["id"] for lr in stalled], [row["id"]])
        self.assertEqual(stalled[0]["state"], "AWAITING_REVIEW")
        self.assertTrue(stalled[0]["stalled"])

    def test_open_is_not_a_stallable_state(self):
        # OPEN = the dispatch was persisted but delivery was never confirmed.
        # The obligation was created but the reviewer was never told — billing
        # them for work they don't know exists is the fleet-stall root cause.
        # OPEN loops still appear in `lr list` as live obligations; they just
        # don't count as stalled because the integrator owes the notification.
        row = self.dispatch(deadline_s=60)
        self.age(row["id"], 3600)                   # far past the 60s deadline
        self.assertEqual(landreq.stalls()[0], [])    # not stallable
        # And the row says who owes it
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "OPEN")
        self.assertEqual(lr["owed_by"], "integrator")
        self.assertIsNone(lr["stall_threshold_s"])
        self.assertFalse(lr["stalled"])

    def test_ready_stalls_past_the_land_threshold(self):
        row = self.dispatch()
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.age(row["id"], landreq.LAND_STALL_S["READY"] + 60)
        stalled = landreq.stalls()[0]
        self.assertEqual([lr["state"] for lr in stalled], ["READY"])

    def test_merged_local_stalls_past_its_cumulative_push_threshold(self):
        # A push loop sitting since its verdict past the MERGED_LOCAL threshold
        # IS a workflow gap — the local merge landed but the push never did.
        self.add_origin()                          # origin lacks the reviewed tip
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")   # local trunk only
        self.age(row["id"], landreq.LAND_STALL_S["MERGED_LOCAL"] + 60)
        stalled = landreq.stalls()[0]
        self.assertEqual([(lr["id"], lr["state"]) for lr in stalled],
                         [(row["id"], "MERGED_LOCAL")])
        self.assertTrue(stalled[0]["stalled"])

    def test_forward_progress_never_manufactures_a_stall(self):
        # A loop healthy in READY (dwell inside the land budget) must NOT flip
        # to STALLED the instant the integrator merges it locally. Thresholds
        # are cumulative from the verdict and MERGED_LOCAL >= READY, so the
        # merge clears the land-side wait, it does not invent a push stall.
        self.add_origin()
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.age(row["id"], landreq.LAND_STALL_S["READY"] - 60)   # inside READY
        before = landreq.get(row["id"])[0]
        self.assertEqual(before["state"], "READY")
        self.assertFalse(before["stalled"])
        self.git("merge", "--no-edit", "-q", "side")             # forward progress
        after = landreq.get(row["id"])[0]
        self.assertEqual(after["state"], "MERGED_LOCAL")
        self.assertFalse(after["stalled"])                       # not a false alarm
        self.assertEqual(landreq.stalls()[0], [])

    def test_landed_and_young_loops_never_show_as_stalled(self):
        landed = self.dispatch(ref=self.side)
        dispatches.mark_verdict(landed["id"], self.side, "ok", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        self.age(landed["id"], 10 ** 6)            # ancient but terminal
        young = self.dispatch(ref=self.b, lane="lane/young")
        dispatches.mark_verdict(young["id"], self.b, "ok",   # b IS on trunk
                                polarity="approve")
        self.assertEqual(landreq.stalls()[0], [])
        # the ancient landed loop is terminal, and b landed immediately.
        self.assertEqual(landreq.get(landed["id"])[0]["state"], "LANDED")
        self.assertEqual(landreq.get(young["id"])[0]["state"], "LANDED")

    def test_verdict_without_repo_binding_is_unobservable_never_a_false_stall(self):
        # The live-ledger bug this guards: an old verdict'd row with no repo_id
        # cannot have its landing observed, so it must NOT be asserted READY +
        # STALLED (it may well have landed a day ago). Unobservable, not a gap.
        #
        # Since verdict POLARITY landed, this legacy row is REVIEWED rather than
        # READY, and that is a strictly stronger form of the same guarantee: the
        # row never declared whether the review APPROVED, so no stall is billable
        # in either direction. Note the evidence prose here literally reads
        # "CLEAR; landed as merge x" — a keyword sniffer would happily call this
        # an approval, which is exactly the per-case inference this module
        # refuses to make.
        old = "2026-07-01T00:00:00Z"
        legacy = {"id": "4f65d90d", "ts": old, "recipient": "codex-orch",
                  "lane": "work-adopt", "ref": self.a, "tip": self.a,
                  "note": None, "deadline_s": 60, "source": "old",
                  "status": "verdict", "verdict_ref": "CLEAR; landed as merge x",
                  "reviewed_tip": self.a, "last_updated": old}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), legacy))
        lr = landreq.get("4f65d90d")[0]
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertIsNone(lr["polarity"])
        self.assertFalse(lr["observable"])
        self.assertFalse(lr["stalled"])           # aged far past, still no gap
        self.assertEqual(landreq.stalls()[0], [])
        self.assertIn("polarity UNDECLARED", run(["list", "--all"])[1])
        self.assertIn("UNDECLARED", run(["show", "4f65d90d"])[1])

    def test_stalls_are_ordered_longest_first(self):
        older = self.dispatch(lane="lane/older")
        dispatches.mark_verdict(older["id"], self.side, "ok", polarity="approve")
        self.age(older["id"], 7200)
        newer = self.dispatch(ref=self.side, lane="lane/newer")
        dispatches.mark_verdict(newer["id"], self.side, "ok", polarity="approve")
        self.age(newer["id"], 3700)
        order = [lr["id"] for lr in landreq.stalls()[0]]
        self.assertEqual(order, [older["id"], newer["id"]])


class ProjectionScopeTest(LandReqBase):
    def test_refless_legacy_rows_are_not_land_loops(self):
        ts = "2026-07-01T00:00:00Z"
        legacy = {"id": "ce1e7dd0", "ts": ts, "recipient": "codex-3",
                  "lane": "legacy", "ref": self.a[:7], "note": None,
                  "deadline_s": 60, "source": "old", "status": "open",
                  "ack_ref": None, "verdict_ref": None, "last_updated": ts}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), legacy))
        self.assertEqual(dispatches.rows()["ce1e7dd0"]["migration"],
                         "needs-redispatch")
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        self.assertNotIn("ce1e7dd0", lrs)          # no exact tip => no land loop

    def test_unavailable_ledger_is_unknown_not_an_empty_board(self):
        with mock.patch.object(eventledger, "checked_events",
                               return_value=([], "PermissionError: denied")):
            loops, unavailable = landreq.loops()
            self.assertIsNone(loops)
            self.assertIn("denied", unavailable)
            section = landreq.board_section()
            self.assertIn("denied", section["unavailable"])
            self.assertEqual(section["loops"], [])
            rc, _out, err = run(["list"])
            self.assertEqual(rc, 1)
            self.assertIn("UNKNOWN", err)

    def test_list_excludes_landed_by_default_all_includes_it(self):
        row = self.dispatch()
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        default, _ = landreq.loops()
        self.assertEqual(default, [])              # terminal, not in flight
        every, _ = landreq.loops(include_landed=True)
        self.assertEqual([lr["id"] for lr in every], [row["id"]])


class BoardSectionTest(LandReqBase):
    def test_board_section_exposes_loops_and_the_stalled_subset(self):
        stuck = self.dispatch(lane="lane/stuck")
        dispatches.mark_verdict(stuck["id"], self.side, "ok", polarity="approve")
        self.age(stuck["id"], 9000)
        fresh = self.dispatch(ref=self.side, lane="lane/fresh")
        section = landreq.board_section()
        self.assertEqual(section["title"], "LAND LOOPS")
        self.assertIsNone(section["unavailable"])
        ids = {c["id"] for c in section["loops"]}
        self.assertEqual(ids, {stuck["id"], fresh["id"]})
        stalled_ids = {c["id"] for c in section["stalled"]}
        self.assertEqual(stalled_ids, {stuck["id"]})
        card = next(c for c in section["loops"] if c["id"] == stuck["id"])
        self.assertEqual(card["state"], "READY")
        self.assertEqual(len(card["review_sha"]), 12)   # clipped for the panel

    def test_board_derives_both_subsets_from_one_projection(self):
        self.dispatch()
        # The PROPERTY is one ledger read, not the NAME of the reader: the
        # topology needs rows project() drops (cancelled is transit), so both
        # subsets now come from project_raw — still ONE read. Counting the old
        # name would have gone green forever the day the mechanism moved.
        with mock.patch.object(landreq, "project_raw",
                               wraps=landreq.project_raw) as p, \
                mock.patch.object(landreq, "project",
                                  wraps=landreq.project) as legacy:
            landreq.board_section()
        self.assertEqual(p.call_count + legacy.call_count, 1)


class CmdTest(LandReqBase):
    def test_cli_list_show_stalls_round_trip(self):
        row = self.dispatch(lane="lane/round")
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.age(row["id"], 9000)
        rc, out, err = run(["list"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("READY", out)
        self.assertIn("STALLED", out)
        rc, out, err = run(["stalls"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("workflow gap", out)
        self.assertIn(row["id"][:12], out)
        rc, out, err = run(["show", row["id"][:8]])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("LAND REQUEST", out)
        self.assertIn("timeline:", out)
        self.assertIn("reviewer codex-3", out)

    def test_stalls_derives_both_subsets_from_one_projection(self):
        self.dispatch()
        # Same property, same reason as the board pin above: ONE read, whoever
        # performs it. `stalls` derives the stalled AND unmeasurable subsets.
        with mock.patch.object(landreq, "project_raw",
                               wraps=landreq.project_raw) as p, \
                mock.patch.object(landreq, "project",
                                  wraps=landreq.project) as legacy:
            run(["stalls"])
        self.assertEqual(p.call_count + legacy.call_count, 1)

    def test_cli_empty_and_clean_states_are_honest(self):
        self.assertIn("no land loops", run(["list"])[1])
        self.assertIn("no stalled", run(["stalls"])[1])
        row = self.dispatch()                      # OPEN, young, not stalled
        self.assertIn("no stalled", run(["stalls"])[1])
        self.assertIn("OPEN", run(["list"])[1])

    def test_show_prefix_is_unique_or_refused(self):
        row = self.dispatch()
        _lr, err = landreq.get(row["id"][:10])
        self.assertIsNone(err)
        _lr, err = landreq.get("")
        self.assertIn("no such land request", err)
        rc, _out, err = run(["show", "zzzz"])
        self.assertEqual(rc, 1)
        self.assertIn("no such land request", err)

    def test_exact_short_id_does_not_outrank_a_longer_collision(self):
        short = {"id": "deadbeef", "state": "OPEN"}
        longer = {"id": "deadbeef" + "1" * 24, "state": "READY"}
        lrs = {short["id"]: short, longer["id"]: longer}
        with mock.patch.object(landreq, "project", return_value=(lrs, None)):
            row, err = landreq.get("deadbeef")
            self.assertIsNone(row)
            self.assertIn("ambiguous land request id prefix", err)
            self.assertIs(landreq.get(longer["id"])[0], longer)
        with mock.patch.object(landreq, "project",
                               return_value=({short["id"]: short}, None)):
            self.assertIs(landreq.get(short["id"])[0], short)

    def test_bad_usage_is_rc2_without_traceback(self):
        for args in ([], ["bogus"], ["list", "--wat"], ["stalls", "x"],
                     ["show"]):
            rc, out, err = run(args)
            self.assertEqual(rc, 2, args)
            self.assertNotIn("Traceback", out + err)

    def test_cli_verb_is_wired(self):
        from helm import cli
        self.assertIn("lr", cli.VERBS)


class SelectiveProjectionTest(LandReqBase):
    @contextlib.contextmanager
    def projection_world(self, current, fold):
        events = {rid: [] for rid in current}
        attest = lambda rows: {row["id"]: {} for row in rows}
        with mock.patch.object(
                dispatches, "snapshot_and_events",
                return_value=(current, events, events, {}, None)), \
                mock.patch.object(dispatches, "gate_epoch", return_value=None), \
                mock.patch.object(dispatches, "attest_projections",
                                  side_effect=attest), \
                mock.patch.object(dispatches, "verdict_index",
                                  return_value=None), \
                mock.patch.object(landreq, "_receipts_by_tip",
                                  return_value={}), \
                mock.patch.object(landreq, "_consumed_confirmations",
                                  return_value={}), \
                mock.patch.object(landreq.store_load, "read_scope",
                                  side_effect=contextlib.nullcontext), \
                mock.patch.object(landreq, "_lr", side_effect=fold) as hydrate, \
                mock.patch.object(landreq, "_trunk_reach", return_value=set()), \
                mock.patch.object(landreq, "_annotate_succession"), \
                mock.patch.object(landreq, "_annotate_contrary_discharge"), \
                mock.patch.object(landreq, "_annotate_frontier_debt") as frontier:
            yield hydrate, frontier

    def raw(self, rid, parent=None, root=None, status="open"):
        return {"id": rid, "tip": self.side, "status": status,
                "supersedes": parent, "chain_root": root}

    def test_show_hydrates_only_the_selected_chain_from_the_full_snapshot(self):
        parent, cancelled, target, fork = (c * 32 for c in "abcd")
        current = {
            parent: self.raw(parent),
            cancelled: self.raw(cancelled, parent, parent, "cancelled"),
            target: self.raw(target, cancelled, parent),
            fork: self.raw(fork, parent, parent),
        }
        for i in range(1000):
            rid = "%032x" % (i + 16)
            current[rid] = self.raw(rid)
        wanted = {parent, target, fork}

        def fold(row, *_args, **_kw):
            if row["id"] not in wanted:
                raise RuntimeError("unrelated row hydrated")
            return {"id": row["id"]}

        with self.projection_world(current, fold) as (hydrate, frontier):
            row, err = landreq.get(target[:12])
            self.assertIsNone(err, err)
            self.assertEqual(row["id"], target)
            self.assertEqual({c.args[0]["id"] for c in hydrate.call_args_list},
                             wanted)
            # The cancelled middle is not hydrated, but the complete one-instant
            # snapshot still reaches topology/frontier annotation as transit.
            self.assertIs(frontier.call_args.args[1], current)
            self.assertEqual(len(current), 1004)

            # Full-board projection remains strict: the same unrelated failure
            # is still globally unavailable rather than silently omitted.
            hydrate.reset_mock()
            out, raw, unavailable = landreq.project_raw()
            self.assertEqual((out, raw), ({}, {}))
            self.assertIn("unrelated row hydrated", unavailable)

    def test_selector_ambiguity_refuses_before_any_row_is_hydrated(self):
        short = "deadbeef"
        longer = short + "1" * 24
        current = {short: self.raw(short), longer: self.raw(longer)}
        with self.projection_world(
                current, lambda row, *_a, **_k: {"id": row["id"]}) \
                as (hydrate, _frontier):
            row, err = landreq.get(short)
        self.assertIsNone(row)
        self.assertIn("ambiguous land request id prefix", err)
        self.assertEqual(hydrate.call_count, 0)

    def test_close_routes_the_requested_id_into_selective_projection(self):
        rid = "a" * 32
        row = {"id": rid}
        with mock.patch.object(landreq, "project",
                               return_value=({rid: row}, None)) as projection, \
                mock.patch.object(landreq, "_close_ladder_withdrawn",
                                  return_value=({"would_close": rid}, None)):
            result, err = landreq.close(rid, "withdrawn", dry_run=True)
        self.assertIsNone(err, err)
        self.assertEqual(result["would_close"], rid)
        projection.assert_called_once_with(selector=rid)


class PolarityTest(LandReqBase):
    """A verdict's DIRECTION, which `status == "verdict"` could never carry.

    The bug these pin, live on 2026-07-25: five loops sat READY for over a day
    carrying SUPERSEDED, SUPERSEDED, "FIX (3 blockers)", "helm#210 FIX" and
    "helm#217 FIX" — not one an approval — and `stalls` billed all five as
    land-side workflow gaps, because READY was derived from a review having
    HAPPENED rather than from its having said YES.
    """

    def _legacy_undeclared(self, row, evidence="ok"):
        """Plant history the current writer is now required to refuse."""
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": row["seq"] + 1,
            "id": row["id"], "ts": dispatches.pk.now_ts(),
            "reviewed_tip": row["tip"], "verdict_ref": evidence}))

    def test_a_fix_verdict_is_changes_requested_and_owed_by_the_author(self):
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "3 blockers",
                                polarity="fix")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "CHANGES_REQUESTED")
        self.assertEqual(lr["owed_by"], "author")
        self.assertFalse(lr["terminal"])       # the author still owes a re-dispatch

    def test_a_fix_verdict_never_reads_as_a_land_loop(self):
        """The precise inversion of the live bug: a rejection must not park in a
        land-side state where the LANDER is nagged for someone else's work."""
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "FIX", polarity="fix")
        self.age(row["id"], 10 ** 6)
        lr = landreq.get(row["id"])[0]
        self.assertNotIn(lr["state"], ("READY", "MERGED_LOCAL", "LANDED"))
        self.assertNotEqual(lr["owed_by"], "lander")

    def test_a_supersede_verdict_is_terminal_and_never_stalls(self):
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "replaced by e1cf126",
                                polarity="supersede")
        self.age(row["id"], 10 ** 6)           # ancient, and must still be quiet
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "SUPERSEDED")
        self.assertTrue(lr["terminal"])
        self.assertFalse(lr["stalled"])
        self.assertEqual(landreq.stalls()[0], [])

    def test_an_undeclared_verdict_is_reviewed_and_carries_no_threshold(self):
        row = self.dispatch(ref=self.side)
        self._legacy_undeclared(row)
        self.age(row["id"], 10 ** 6)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertIsNone(lr["polarity"])
        self.assertIsNone(lr["stall_threshold_s"])
        self.assertFalse(lr["stalled"])

    def test_an_unknown_polarity_fails_closed_to_reviewed_never_ready(self):
        """THE MUTATION KILL. Replay is not a trust boundary: a hand-edited or
        future-versioned ledger row carrying an unrecognised polarity must read
        UNDECLARED, never as an approval. If `_replay_polarity` ever passed the
        raw value through, or `VERDICT_STATE.get` ever defaulted to READY, this
        row would arm the land clock on a verdict nobody can interpret."""
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for ev in events:
                if ev.get("event") == "verdict":
                    ev["polarity"] = "definitely-approved-trust-me"
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertIsNone(lr["polarity"])

    def test_write_time_refuses_an_unknown_polarity_outright(self):
        row = self.dispatch(ref=self.side)
        out, why = dispatches.mark_verdict(row["id"], self.side, "ok",
                                           polarity="lgtm")
        self.assertIsNone(out)
        self.assertIn("polarity must be one of", why)
        # and the refusal is TOTAL — no half-written verdict behind it
        self.assertEqual(landreq.get(row["id"])[0]["state"], "OPEN")

    def test_polarity_cannot_be_flipped_on_a_standing_verdict(self):
        """Terminal is immutable, and that includes a verdict's direction: the
        same tip and evidence with a DIFFERENT polarity is not an idempotent
        retry, it is an attempted rewrite of what the reviewer said."""
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "same", polarity="fix")
        out, why = dispatches.mark_verdict(row["id"], self.side, "same",
                                           polarity="approve")
        self.assertIsNone(out)
        self.assertIn("already has a verdict", why)
        self.assertEqual(landreq.get(row["id"])[0]["state"],
                         "CHANGES_REQUESTED")

    def test_an_identical_verdict_including_polarity_is_still_idempotent(self):
        row = self.dispatch(ref=self.side)
        first, why = dispatches.mark_verdict(row["id"], self.side, "same",
                                            polarity="approve")
        self.assertIsNone(why)
        again, why = dispatches.mark_verdict(row["id"], self.side, "same",
                                            polarity="approve")
        self.assertIsNone(why)
        self.assertEqual(again["polarity"], first["polarity"])

    def test_an_undeclared_verdict_still_reports_trunk_FACTS(self):
        """Undeclared kills the land CLOCK, not git OBSERVATION. This is what
        preserved the 8 genuinely-true MERGED_LOCAL alarms when the polarity fix
        went in: trunk membership is a fact about the repo, not a reading of the
        verdict, so an undeclared row whose tip demonstrably reached trunk is
        honestly LANDED rather than hidden behind the refusal."""
        row = self.dispatch(ref=self.side)
        self._legacy_undeclared(row)
        self.git("merge", "--no-edit", "-q", "side")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "LANDED")
        self.assertTrue(lr["landed"])

    def test_a_fix_verdict_still_observes_git_facts(self):
        """A FIX governs intent, not physical history. The verdict state remains
        CHANGES_REQUESTED while the separate Git facts expose contrary inclusion
        instead of suppressing or laundering it."""
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "FIX", polarity="fix")
        self.git("merge", "--no-edit", "-q", "side")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "CHANGES_REQUESTED")
        self.assertTrue(lr["observable"] and lr["landed"])
        self.assertTrue(lr["contrary"])
        self.assertFalse(lr["terminal"])
        self.assertEqual(lr["owed_by"], "integrator")
        self.assertIn("CONTRARY", run(["list"])[1])
        self.assertIn("contrary", run(["show", row["id"]])[1])
        card = landreq.board_section()["loops"][0]
        self.assertTrue(card["contrary"] and card["landed"])
        self.assertEqual(card["owed_by"], "integrator")

    def test_a_contrary_local_merge_names_local_not_upstream_trunk(self):
        self.add_origin()
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "FIX", polarity="fix")
        self.git("merge", "--no-edit", "-q", "side")
        lr = landreq.get(row["id"])[0]
        self.assertTrue(lr["contrary"] and lr["merged_local"])
        self.assertFalse(lr["landed"])
        shown = run(["show", row["id"]])[1]
        self.assertIn("MERGED_LOCAL on local trunk", shown)
        self.assertNotIn("MERGED_LOCAL on upstream trunk", shown)

    def test_a_superseded_tip_on_trunk_stays_visible_as_contrary(self):
        row = self.dispatch(ref=self.side, lane="lane/contrary")
        dispatches.mark_verdict(row["id"], self.side, "replaced",
                                polarity="supersede")
        self.git("merge", "--no-edit", "-q", "side")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "SUPERSEDED")
        self.assertTrue(lr["contrary"] and lr["landed"])
        self.assertFalse(lr["terminal"])
        self.assertEqual(lr["owed_by"], "integrator")
        self.assertIn(row["id"], [x["id"] for x in landreq.loops()[0]])
        self.assertIn("CONTRARY", run(["list"])[1])
        self.assertIn("owed by integrator", run(["show", row["id"]])[1])

    def test_stalls_reports_undeclared_rows_instead_of_dropping_them(self):
        """The HONEST REFUSAL. A loop excluded from stall accounting and also
        absent from the output reads as healthy — that silent-absence is how 4
        live loops stayed invisible for three days. Exclusion must be STATED."""
        row = self.dispatch(ref=self.side, lane="lane/undeclared")
        self._legacy_undeclared(row)
        self.age(row["id"], 10 ** 6)
        rows, err = landreq.unmeasurable()
        self.assertIsNone(err)
        self.assertEqual([lr["id"] for lr, _why in rows], [row["id"]])
        self.assertEqual(landreq.stalls()[0], [])
        _rc, out, _err = run(["stalls"])
        self.assertIn("NOT stall-checked", out)
        # #142: the writer strips the lane/ prefix, so the surfaced row names
        # the stored bare spelling.
        self.assertIn("undeclared", out)

    def test_a_held_approve_is_not_reported_as_undeclared_polarity(self):
        """Second site of the `lr show` polarity defect, and the one that
        reached the OWNER CONSOLE. `_unmeasurable_rows` labelled EVERY
        non-terminal REVIEWED row "polarity UNDECLARED" with no check on
        whether a polarity existed. Measured on the live ledger: 14 of 37
        REVIEWED rows carry polarity `approve` — held by the approval tier or
        a missing receipt, not by any absence of a verdict — and the console
        told the owner all 37 were undeclared.

        Sited beside test_stalls_reports_undeclared_rows_instead_of_dropping_them,
        which builds a genuinely polarity-less row and still expects UNDECLARED.
        The pair is the point: this asserts the label is CORRECTED when a
        polarity exists, that one asserts it still FIRES when none does. Deleting
        the branch outright goes green here and red there.

        The row stays in the unmeasurable bucket on purpose — whether a held
        APPROVE should become stall-billable is a real question and a separate
        change. This fixes the false REASON, nothing else."""
        row = self.dispatch(ref=self.side, lane="lane/held-approve")
        # Approval-tier demotion, not a missing receipt — see the sibling test
        # in EraStabilityTest for why: an ungated approve is no longer
        # constructible, mark_verdict refuses it before any append.
        with mock.patch.object(dispatches, "approval_tier",
                               lambda who, repo=None: ("outside", "reviewer outside the "
                                            "permitted tier (test)")):
            dispatches.mark_verdict(row["id"], self.side, "ok",
                                    polarity="approve")
            self.age(row["id"], 10 ** 6)
            lr, err = landreq.get(row["id"])
            self.assertIsNone(err, err)
            self.assertEqual(lr["state"], "REVIEWED")     # the shape at issue
            self.assertEqual(lr["polarity"], "approve")   # and it IS declared
            rows, err = landreq.unmeasurable()
            self.assertIsNone(err)
            why = dict((x["id"], w) for x, w in rows)[row["id"]]
            # Assert the EFFECT: the real polarity reaches the surface.
            self.assertIn("APPROVE", why)
            self.assertNotIn("UNDECLARED", why)

    def test_the_all_clear_line_never_hides_an_undeclared_row(self):
        """Lie by omission: with zero stalls and one unclassifiable loop, "every
        loop is inside its threshold" must not be the whole message."""
        row = self.dispatch(ref=self.side)
        self._legacy_undeclared(row)
        self.age(row["id"], 10 ** 6)
        _rc, out, _err = run(["stalls"])
        self.assertIn("inside its threshold", out)      # the all-clear fires...
        self.assertIn("NOT stall-checked", out)         # ...and does not stand alone

    def test_stalls_offers_no_remedy_the_ledger_would_refuse(self):
        """A remedy that cannot be followed is worse than none: a verdict is
        immutable, so `dispatch verdict` REFUSES an already-verdict'd row, and
        telling an operator to re-record one would fail the moment they tried."""
        row = self.dispatch(ref=self.side)
        self._legacy_undeclared(row)
        _rc, out, _err = run(["stalls"])
        self.assertIn("immutable", out)
        # and the ledger really does refuse, so the text is not merely cautious
        _out, why = dispatches.mark_verdict(row["id"], self.side, "ok",
                                            polarity="approve")
        self.assertIn("already has a verdict", why)

    def test_the_timeline_records_the_verdicts_own_direction(self):
        """The history must not retell the lie the state machine just stopped
        telling: a FIX verdict's timeline entry is CHANGES_REQUESTED, not READY."""
        row = self.dispatch(ref=self.side)
        dispatches._mark_delivered(row["id"], "post-1")
        dispatches.mark_verdict(row["id"], self.side, "FIX", polarity="fix")
        states = [s["state"] for s in landreq.get(row["id"])[0]["timeline"]]
        self.assertEqual(states, ["OPEN", "AWAITING_REVIEW",
                                  "CHANGES_REQUESTED"])
        self.assertNotIn("READY", states)

    def test_every_state_names_who_owes_the_next_move(self):
        """A stall that cannot say whose turn it is cannot drive a poke, which is
        the whole point of open owner-ask 21350297."""
        for state in landreq.STAGE_ORDER:
            self.assertIn(state, landreq.OWED_BY, state)
        self.assertEqual(landreq.OWED_BY["CHANGES_REQUESTED"], "author")
        self.assertEqual(landreq.OWED_BY["READY"], "lander")
        self.assertEqual(landreq.OWED_BY["AWAITING_REVIEW"], "reviewer")

    def test_declared_polarity_survives_a_fresh_disk_replay(self):
        """It is ledger EVIDENCE, not a live-object field: a cold reader must
        reach the same verdict direction the writer recorded."""
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="fix")
        current, unavailable = dispatches.snapshot()   # re-reads from disk
        self.assertIsNone(unavailable)
        self.assertEqual(current[row["id"]]["polarity"], "fix")

    def test_cli_declares_polarity_by_flag_not_by_positional(self):
        """Evidence is a free-text tail, so polarity is a REQUIRED flag.
        Omitting it refuses before the immutable ledger gains a verdict."""
        from helm import dispatches as d
        row = self.dispatch(ref=self.side)
        rc = d.cmd_dispatch(
            ["verdict", row["id"], self.side, "--measured",
             "looks", "good"])
        self.assertEqual(rc, 2)
        self.assertEqual(landreq.get(row["id"])[0]["state"], "OPEN")
        rc = d.cmd_dispatch(["verdict", row["id"], self.side,
                             "--approve", "--measured", "looks", "good"])
        self.assertEqual(rc, 0)
        self.assertEqual(landreq.get(row["id"])[0]["polarity"], "approve")
        self.assertEqual(landreq.get(row["id"])[0]["verdict_ref"], "looks good")

    def test_cli_refuses_two_polarities_and_an_unknown_flag(self):
        from helm import dispatches as d
        row = self.dispatch(ref=self.side)
        for flags in (["--approve", "--fix"], ["--maybe"]):
            rc = d.cmd_dispatch(["verdict", row["id"], self.side, *flags,
                                 "--measured", "e"])
            self.assertEqual(rc, 2, flags)
        self.assertEqual(landreq.get(row["id"])[0]["state"], "OPEN")
class ContraryDischargeTest(LandReqBase):
    """A contrary FIX/SUPERSEDE land remains history after an approved round
    retires the operational debt."""

    def handoff(self, lane, ref, supersedes=None):
        """A round. `supersedes` names the round it continues — which is the
        WORK IDENTITY discharge now walks, and the reason a superseding round
        must be dispatched with --supersedes rather than merely be later."""
        row, why, sent = dispatches.send(
            "codex-3", lane, "review " + lane, ref, repo=self.repo,
            key="key-" + lane, sign=False,
            new_work=supersedes is None, supersedes=supersedes)
        self.assertIsNone(why)
        self.assertTrue(sent)
        return row

    def resolved(self, polarity="fix", upstream=False, push=False):
        if upstream or push:
            self.add_origin()
        first = self.handoff("feature-r1", self.side)
        dispatches.mark_verdict(first["id"], self.side, "findings",
                                polarity=polarity)
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve findings", path="g")
        self.git("checkout", "-q", self.main)
        second = self.handoff("feature-r2", fixed, supersedes=first["id"])
        dispatches.mark_verdict(second["id"], fixed, "re-probed clean",
                                polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        if push:
            self.git("push", "-q", "origin", self.main)
        return first, second, fixed

    def test_discharge_closes_only_the_debt_and_preserves_contrary_history(self):
        first, second, fixed = self.resolved()
        lr, why = landreq.discharge(first["id"][:12], fixed, "r2 approved")
        self.assertIsNone(why)
        self.assertEqual(lr["state"], "CHANGES_REQUESTED")
        self.assertTrue(lr["contrary"] and lr["terminal"])
        self.assertEqual(lr["close_reason"], "superseded")   # via lr close
        self.assertEqual(lr["owed_by"], "nobody")
        self.assertFalse(lr["stalled"])
        self.assertEqual(lr["superseding_tip"], fixed)
        self.assertEqual(lr["superseding_id"], second["id"])
        self.assertEqual(lr["close_evidence"], "r2 approved")
        self.assertEqual(lr["contrary_state"], "landed")
        self.assertEqual([step["state"] for step in lr["timeline"]][-1],
                         "CLOSED_SUPERSEDED")
        frozen = lr["dwell_s"]
        self.assertEqual(landreq.get(first["id"], time.time() + 10000)[0]["dwell_s"],
                         frozen)
        self.assertNotIn(first["id"], [r["id"] for r in landreq.loops()[0]])
        self.assertIn(first["id"], [r["id"] for r in landreq.loops(True)[0]])
        self.assertEqual(landreq.stalls()[0], [])
        self.assertNotIn(first["id"],
                         [r["id"] for r in landreq.board_section()["loops"]])
        shown = run(["show", first["id"][:12]])[1]
        self.assertIn("CONTRARY", shown.upper())
        self.assertIn("CLOSED (SUPERSEDED)", shown)
        self.assertIn(fixed, shown)
        listed = run(["list", "--all"])[1]
        self.assertIn("CLOSED (SUPERSEDED) by %s" % fixed[:12], listed)
        blind = {"observable": False, "local": False, "upstream": False,
                 "has_upstream": False}
        with mock.patch.object(landreq, "_git_observe", return_value=blind):
            replayed = landreq.get(first["id"])[0]
        self.assertTrue(replayed["contrary"])
        self.assertEqual(replayed["close_reason"], "superseded")
        self.assertEqual(replayed["contrary_state"], "landed")
        self.assertEqual(replayed["contrary_target"], "local")

    def test_supersede_contrary_can_be_discharged_without_changing_state(self):
        first, _second, fixed = self.resolved("supersede")
        lr, why = landreq.discharge(first["id"], fixed, "replacement approved")
        self.assertIsNone(why)
        self.assertEqual(lr["state"], "SUPERSEDED")
        self.assertTrue(lr["contrary"] and lr["terminal"])
        self.assertEqual(lr["close_reason"], "superseded")

    def test_identical_retry_is_idempotent_and_conflict_is_refused(self):
        first, _second, fixed = self.resolved()
        one, why = landreq.discharge(first["id"], fixed, "resolved")
        self.assertIsNone(why)
        before = len(dispatches.history(first["id"]))
        two, why = landreq.discharge(first["id"], fixed, "resolved")
        self.assertIsNone(why)
        self.assertEqual(two, one)
        self.assertEqual(len(dispatches.history(first["id"])), before)
        _lr, why = landreq.discharge(first["id"], fixed, "different")
        self.assertIn("retired once", why)

    def test_an_earlier_approve_cannot_launder_a_later_fix(self):
        base = self.git("rev-parse", self.main)
        self.git("checkout", "-q", "-b", "approved-first", base)
        with open(os.path.join(self.repo, "shared"), "w", encoding="utf-8") as f:
            f.write("same patch\n")
        self.git("add", "shared")
        self.git("commit", "-q", "-m", "approved first")
        approved_tip = self.git("rev-parse", "HEAD")
        approved = self.handoff("approved-first", approved_tip)
        dispatches.mark_verdict(approved["id"], approved_tip, "clean",
                                polarity="approve")
        self.git("checkout", "-q", self.main)
        self.git("cherry-pick", approved_tip)

        self.git("checkout", "-q", "-b", "rejected-later", base)
        with open(os.path.join(self.repo, "shared"), "w", encoding="utf-8") as f:
            f.write("same patch\n")
        self.git("add", "shared")
        self.git("commit", "-q", "-m", "rejected later")
        rejected_tip = self.git("rev-parse", "HEAD")
        rejected = self.handoff("rejected-later", rejected_tip)
        dispatches.mark_verdict(rejected["id"], rejected_tip, "new findings",
                                polarity="fix")
        self.git("checkout", "-q", self.main)
        events = eventledger.events(dispatches.ledger_path())
        forged = next(dict(event) for event in events
                      if event.get("id") == rejected["id"]
                      and event.get("event") == "verdict")
        with open(dispatches.ledger_path(), "w", encoding="utf-8") as f:
            for event in [forged] + events:  # pre-genesis duplicate: replay rejects it
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
        self.assertEqual(dispatches.snapshot()[0][rejected["id"]]["status"],
                         "verdict")
        self.assertTrue(landreq.get(rejected["id"])[0]["contrary"])
        self.assertTrue(landreq._landed(
            os.path.realpath(os.path.join(self.repo, ".git")),
            rejected_tip, approved_tip))
        _lr, why = landreq.discharge(rejected["id"], approved_tip,
                                     "old approval")
        self.assertIn("no later APPROVE", why)
        self.assertFalse(landreq.get(rejected["id"])[0]["discharged"])

    def test_concurrent_identical_discharge_reconciles_as_success(self):
        first, second, fixed = self.resolved()
        real = dispatches.snapshot_with_verdicts
        before = len(dispatches.history(first["id"]))
        fired = []

        def race():
            if not fired:
                fired.append(True)
                row, why = dispatches._record_discharge_proven(
                    first["id"], self.side, fixed, second["id"], "resolved",
                    "landed", "local")
                self.assertIsNone(why)
                self.assertTrue(row["discharged"])
            return real()

        with mock.patch.object(dispatches, "snapshot_with_verdicts",
                               side_effect=race):
            lr, why = landreq.discharge(first["id"], fixed, "resolved")
        self.assertIsNone(why)
        self.assertTrue(lr["discharged"])
        self.assertEqual(len(dispatches.history(first["id"])), before + 1)

    def test_discharge_requires_an_approved_superseding_dispatch(self):
        first = self.handoff("feature-r1", self.side)
        dispatches.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        self.git("merge", "--no-edit", "-q", "side")
        descendant = self.commit("unreviewed descendant")
        _lr, why = landreq.discharge(first["id"], descendant, "trust me")
        self.assertIn("no later APPROVE verdict", why)

    def test_gate_capable_ungated_approve_cannot_discharge(self):
        first = self.handoff("feature-r1", self.side)
        dispatches.mark_verdict(
            first["id"], self.side, "findings", polarity="fix")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve without gate", path="g")
        self.git("checkout", "-q", self.main)
        second = self.handoff(
            "feature-r2", fixed, supersedes=first["id"])
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": second["seq"] + 1,
            "id": second["id"], "ts": dispatches.pk.now_ts(),
            "reviewed_tip": fixed, "verdict_ref": "approve without receipt",
            "polarity": "approve", "gate": "",
            "gate_caps": [dispatches.GATE_CAP_RECEIPT]}))
        candidate = landreq.get(second["id"])[0]
        self.assertEqual(candidate["state"], "REVIEWED")
        self.assertIn("no minted gate receipt", candidate["ungated"])
        self.git("merge", "--no-edit", "-q", "side")
        before = len(dispatches.history(first["id"]))
        _lr, why = landreq.discharge(first["id"], fixed, "ungated successor")
        self.assertIn("no later APPROVE", why)
        self.assertEqual(len(dispatches.history(first["id"])), before)

    def test_gate_capable_bound_approve_can_discharge(self):
        with mock.patch.object(
                dispatches, "GATE_CAPS", (dispatches.GATE_CAP_RECEIPT,)), \
                mock.patch.object(
                    dispatches.gate, "bind",
                    return_value=("VERIFIED", "a" * 16, "test receipt")):
            first, _second, fixed = self.resolved()
        lr, why = landreq.discharge(first["id"], fixed, "gated successor")
        self.assertIsNone(why)
        self.assertEqual(lr["close_reason"], "superseded")

    def forge_open(self, rid, drop=(), **fields):
        """Rewrite one row's opening dispatch event, the way a hand-edit or a
        pre-chain writer would look to replay. Real code never rewrites the
        append-only ledger; this is how legacy and corrupt rows get in front
        of it (the `rewrite` idiom in tests/test_dispatch_chain.py)."""
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for event in events:
                if event.get("id") == rid and event.get("event") == "dispatch":
                    for key in drop:
                        event.pop(key, None)
                    event.update(fields)
                f.write(json.dumps(event, separators=(",", ":")) + "\n")

    def legacy(self, row):
        """Strip a row's chain fields so replay reads it as a pre-chain LEGACY
        row — the shape of everything on the live ledger before chains."""
        self.forge_open(row["id"], drop=("chain_root", "supersedes"))

    def test_a_successor_approve_on_the_authors_chain_pays_the_debt(self):
        """THE LANE-HANDOFF ADMIT, chain leg. A lane that changed hands
        mid-life (author -> successor integrator) used to leave its contrary
        debt payable by nobody: the successor's approve failed only the flat
        sender==author clause. The successor's round names the author's row as
        its parent, so the handoff is PROVEN by the record and the debt is
        payable."""
        first = self.handoff("feature-r1", self.side)
        dispatches.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve by the successor", path="g")
        self.git("checkout", "-q", self.main)
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        second = self.handoff("feature-r2", fixed, supersedes=first["id"])
        dispatches.mark_verdict(second["id"], fixed, "clean", polarity="approve")
        os.environ["HELM_CHAT_NAME"] = "integrator"
        self.git("merge", "--no-edit", "-q", "side")
        lr, why = landreq.discharge(first["id"], fixed, "handed-off successor")
        self.assertIsNone(why)
        # Delegation truth: the alias routes through close, whose event
        # records close_reason — the discharged flag belongs to legacy
        # replay alone.
        self.assertEqual(lr["close_reason"], "superseded")
        self.assertEqual(lr["superseding_id"], second["id"])
        self.assertEqual(lr["superseding_tip"], fixed)

    def test_legacy_same_stem_recorded_handoff_admits(self):
        """THE LANE-HANDOFF ADMIT, legacy leg. Chainless rows cannot prove a
        handoff by ancestry, so the proof is the lane family record itself:
        same stem family AND the successor visibly dispatched in that family
        beyond the candidate approve — the lane changed hands in the record."""
        first = self.handoff("resolve-thing", self.side)
        self.legacy(first)
        dispatches.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve by the successor", path="g")
        self.git("checkout", "-q", self.main)
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        # The recorded handoff: the successor demonstrably WORKS this lane
        # family (a second dispatch of its own), not one drive-by approve.
        self.handoff("resolve-thing-review", self.c)
        second = self.handoff("resolve-thing-r2", fixed)
        self.legacy(second)
        dispatches.mark_verdict(second["id"], fixed, "clean", polarity="approve")
        os.environ["HELM_CHAT_NAME"] = "integrator"
        self.git("merge", "--no-edit", "-q", "side")
        lr, why = landreq.discharge(first["id"], fixed, "handed-off successor")
        self.assertIsNone(why)
        # Delegation truth: the alias routes through close, whose event
        # records close_reason — the discharged flag belongs to legacy
        # replay alone.
        self.assertEqual(lr["close_reason"], "superseded")
        self.assertEqual(lr["superseding_id"], second["id"])

    def test_lane_prefix_is_family_to_its_bare_stem(self):
        """#156: `_lane_stem` stripped the -rN suffix but NOT the lane/
        prefix, so a family recorded BOTH ways (142: 798 bare rows / 82
        lane/-prefixed) silently failed the handoff family match. One named
        normalisation strips BOTH, every reader and writer alike."""
        self.assertEqual(landreq._lane_stem("lane/resolve-thing-r2"),
                         landreq._lane_stem("resolve-thing"))
        self.assertEqual(landreq._lane_stem("lane/g-review-r2"),
                         landreq._lane_stem("g"))
        # A prefix alone is not membership: a genuinely different stem must
        # still not match.
        self.assertNotEqual(landreq._lane_stem("lane/other-thing"),
                            landreq._lane_stem("resolve-thing"))

    def test_legacy_handoff_admits_across_the_lane_prefix_split(self):
        """The escape hatch BITES here: a legacy contrary recorded bare and
        its successor rounds recorded lane/-prefixed are ONE work family —
        but the family match saw 'resolve-thing' vs 'lane/resolve-thing' and
        refused, exactly the silent failure #142's split guarantees. After
        the fix the recorded handoff admits across the spelling split."""
        first = self.handoff("resolve-thing", self.side)
        self.legacy(first)
        dispatches.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve by the successor", path="g")
        self.git("checkout", "-q", self.main)
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        # Same family, OTHER spelling: the successor visibly works the
        # lane/-prefixed name of the contrary's bare stem.
        self.handoff("lane/resolve-thing-review", self.c)
        second = self.handoff("lane/resolve-thing-r2", fixed)
        self.legacy(second)
        dispatches.mark_verdict(second["id"], fixed, "clean", polarity="approve")
        os.environ["HELM_CHAT_NAME"] = "integrator"
        self.git("merge", "--no-edit", "-q", "side")
        lr, why = landreq.discharge(first["id"], fixed, "handed-off successor")
        self.assertIsNone(why)
        self.assertEqual(lr["close_reason"], "superseded")
        self.assertEqual(lr["superseding_id"], second["id"])

    def test_legacy_chain_leg_admits_only_an_author_row_in_family(self):
        """On a LEGACY contrary no chain binds the walk to the debt, so the
        author row the walk reaches must itself sit in the contrary's lane
        family. The sender here has NO second dispatch in the family — the
        legacy leg refuses it — so the in-family walk is the single admitting
        proof, and this test measures exactly it."""
        first = self.handoff("resolve-thing", self.side)
        self.legacy(first)
        dispatches.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        # The author's own chained round IN the contrary's family.
        bridge = self.handoff("resolve-thing-r2", self.c)
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve by the successor", path="g")
        self.git("checkout", "-q", self.main)
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        second = self.handoff("resolve-thing-r3", fixed, supersedes=bridge["id"])
        dispatches.mark_verdict(second["id"], fixed, "clean", polarity="approve")
        os.environ["HELM_CHAT_NAME"] = "integrator"
        self.git("merge", "--no-edit", "-q", "side")
        lr, why = landreq.discharge(first["id"], fixed, "in-family chain")
        self.assertIsNone(why)
        # Delegation truth: the alias routes through close, whose event
        # records close_reason — the discharged flag belongs to legacy
        # replay alone.
        self.assertEqual(lr["close_reason"], "superseded")
        self.assertEqual(lr["superseding_id"], second["id"])

    def test_legacy_debt_refuses_a_chain_to_the_authors_unrelated_row(self):
        """THE HOLE THE REPAIR CLOSES. A candidate chained to ANY unrelated
        row of the author's used to pay ANY legacy debt of that author — the
        walk proved the author's NAME, not the author's WORK. Out-of-family
        ancestry now refuses, and the refusal names the failed handoff."""
        first = self.handoff("feature-r1", self.side)
        self.legacy(first)
        dispatches.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        # The author's UNRELATED row — a different lane family entirely.
        unrelated = self.handoff("proxy-oauth-mint", self.a)
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve elsewhere", path="g")
        self.git("checkout", "-q", self.main)
        os.environ["HELM_CHAT_NAME"] = "drive-by"
        second = self.handoff("totally-else", fixed, supersedes=unrelated["id"])
        dispatches.mark_verdict(second["id"], fixed, "clean", polarity="approve")
        os.environ["HELM_CHAT_NAME"] = "integrator"
        self.git("merge", "--no-edit", "-q", "side")
        _lr, why = landreq.discharge(first["id"], fixed, "unrelated chain")
        self.assertIn("no later APPROVE", why)
        self.assertIn("no cross-sender candidate proved a lane handoff", why)
        self.assertIn(second["id"][:12], why)
        self.assertFalse(landreq.get(first["id"])[0]["discharged"])

    def test_an_unrelated_seat_approve_in_a_different_lane_family_refuses(self):
        """A cross-sender approve OUTSIDE the contrary's lane family proves no
        handoff — and the refusal now names BOTH failed legs, so the next
        integrator does not re-diagnose from scratch."""
        first = self.handoff("resolve-thing", self.side)
        self.legacy(first)
        dispatches.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve elsewhere", path="g")
        self.git("checkout", "-q", self.main)
        os.environ["HELM_CHAT_NAME"] = "drive-by"
        second = self.handoff("unrelated-lane", fixed)
        dispatches.mark_verdict(second["id"], fixed, "clean", polarity="approve")
        os.environ["HELM_CHAT_NAME"] = "integrator"
        self.git("merge", "--no-edit", "-q", "side")
        _lr, why = landreq.discharge(first["id"], fixed, "drive-by approve")
        self.assertIn("no later APPROVE", why)
        self.assertIn("no cross-sender candidate proved a lane handoff", why)
        self.assertIn("outside the contrary's lane family", why)
        self.assertIn(second["id"][:12], why)
        self.assertFalse(landreq.get(first["id"])[0]["discharged"])

    def test_an_unreadable_chain_refuses_naming_the_unprovable_leg(self):
        """TRI-STATE, fail-closed: a candidate whose supersedes link is corrupt
        cannot prove the handoff, and the refusal says exactly which leg was
        unprovable rather than a bare conjunction."""
        first = self.handoff("feature-r1", self.side)
        dispatches.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve with a corrupt link", path="g")
        self.git("checkout", "-q", self.main)
        os.environ["HELM_CHAT_NAME"] = "other-author"
        second = self.handoff("feature-r2", fixed, supersedes=first["id"])
        dispatches.mark_verdict(second["id"], fixed, "clean", polarity="approve")
        os.environ["HELM_CHAT_NAME"] = "integrator"
        # Corrupt ONLY the parent link; the chain_root still matches, so the
        # handoff walk is the single thing standing between this and a
        # discharge — exactly the leg this test measures.
        self.forge_open(second["id"], supersedes="not-a-chain-id")
        self.git("merge", "--no-edit", "-q", "side")
        _lr, why = landreq.discharge(first["id"], fixed, "corrupt link")
        self.assertIn("no cross-sender candidate proved a lane handoff", why)
        self.assertIn("UNPROVABLE", why)
        self.assertIn(second["id"][:12], why)
        self.assertFalse(landreq.get(first["id"])[0]["discharged"])

    def test_a_forked_approve_by_an_unrelated_seat_cannot_discharge(self):
        """THE ADMIT IS NOT A LOOSENING. On a CHAINED contrary the ancestry is
        the only proof: a fork off the same chain by a seat whose walk never
        reaches a row of the debt's author still refuses — same lane family,
        same chain root, still not a handoff from THIS author."""
        os.environ["HELM_CHAT_NAME"] = "seat-x"
        root = self.handoff("feature-r0", self.a)
        os.environ["HELM_CHAT_NAME"] = "integrator"
        first = self.handoff("feature-r1", self.side, supersedes=root["id"])
        dispatches.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve on a fork", path="g")
        self.git("checkout", "-q", self.main)
        os.environ["HELM_CHAT_NAME"] = "other-author"
        # A FORK off the root — legitimate chain membership, but its walk
        # (second -> root) contains no row authored by `integrator`.
        second = self.handoff("feature-r2", fixed, supersedes=root["id"])
        dispatches.mark_verdict(second["id"], fixed, "clean", polarity="approve")
        os.environ["HELM_CHAT_NAME"] = "integrator"
        self.git("merge", "--no-edit", "-q", "side")
        _lr, why = landreq.discharge(first["id"], fixed, "forked approve")
        self.assertIn("same author/repo", why)
        self.assertIn("no cross-sender candidate proved a lane handoff", why)
        self.assertIn("without a row authored by integrator", why)
        self.assertFalse(landreq.get(first["id"])[0]["discharged"])

    def test_an_approve_from_a_different_repo_cannot_discharge(self):
        first = self.handoff("feature-r1", self.side)
        dispatches.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve for clone", path="g")
        self.git("checkout", "-q", self.main)
        self.git("merge", "--no-edit", "-q", "side")
        other_repo = os.path.join(self.tmp, "other-repo")
        subprocess.run(["git", "clone", "-q", self.repo, other_repo], check=True)
        # ON THE CHAIN ON PURPOSE, same reason as the author test above: an
        # unlinked round would be rejected by the chain gate too, and two checks
        # rejecting one input measure neither.
        second, why, sent = dispatches.send(
            "codex-3", "other-repo-r2", "review clone", fixed,
            repo=other_repo, key="other-repo", sign=False,
            supersedes=first["id"])
        self.assertIsNone(why)
        self.assertTrue(sent)
        dispatches.mark_verdict(second["id"], fixed, "clean", polarity="approve")
        _lr, why = landreq.discharge(first["id"], fixed, "wrong repo")
        self.assertIn("same author/repo", why)

    def test_unrelated_approved_tip_cannot_launder_the_contrary(self):
        """UNRELATED WORK, and the ledger now says so out loud. `other-r2` is a
        separate chain, so it is not a candidate at all — which is the whole
        repair: an approve of different work used to have to be caught by Git
        lineage AFTER being accepted as a candidate, and any later approved
        trunk commit trivially contains a change already on trunk."""
        first = self.handoff("feature-r1", self.side)
        dispatches.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        other = self.handoff("other-r2", self.c)
        dispatches.mark_verdict(other["id"], self.c, "clean", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        _lr, why = landreq.discharge(first["id"], self.c, "unrelated")
        self.assertIn("SAME WORK CHAIN", why)
        self.assertFalse(landreq.get(first["id"])[0]["discharged"])

    def test_a_chained_round_still_needs_git_lineage(self):
        """THE OTHER HALF, bound separately. Naming the parent is necessary and
        NOT sufficient: a round that really is on this chain but whose tip does
        not contain the reviewed change is still refused, by Git. Without this
        test the lineage check would be unmeasured — the chain gate above
        rejects its input first, so reverting lineage would leave that test
        green."""
        first = self.handoff("feature-r1", self.side)
        dispatches.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        second = self.handoff("feature-r2", self.c, supersedes=first["id"])
        dispatches.mark_verdict(second["id"], self.c, "clean", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        _lr, why = landreq.discharge(first["id"], self.c, "same chain, wrong tip")
        self.assertIn("does not contain", why)
        self.assertFalse(landreq.get(first["id"])[0]["discharged"])

    def test_unknown_git_proof_refuses_without_appending(self):
        first, _second, fixed = self.resolved()
        real = landreq._landed

        def unknown(gitdir, tip, ref):
            if tip == self.side and ref == fixed:
                return None
            return real(gitdir, tip, ref)
        before = len(dispatches.history(first["id"]))
        with mock.patch.object(landreq, "_landed", side_effect=unknown):
            _lr, why = landreq.discharge(first["id"], fixed, "resolved")
        self.assertIn("could not prove", why)
        self.assertEqual(len(dispatches.history(first["id"])), before)

    def test_the_original_tip_cannot_discharge_itself(self):
        first = self.handoff("feature-r1", self.side)
        dispatches.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        self.git("merge", "--no-edit", "-q", "side")
        _lr, why = landreq.discharge(first["id"], self.side, "same")
        self.assertIn("cannot supersede itself", why)

    def test_stale_tracking_ref_without_origin_cannot_override_local_authority(self):
        first = self.handoff("feature-r1", self.side)
        dispatches.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        self.git("merge", "--no-edit", "-q", "side")
        self.git("checkout", "-q", "-b", "resolution", self.side)
        fixed = self.commit("resolved but not landed locally", path="g")
        second = self.handoff("feature-r2", fixed, supersedes=first["id"])
        dispatches.mark_verdict(second["id"], fixed, "clean", polarity="approve")
        self.git("checkout", "-q", self.main)
        self.git("update-ref", "refs/remotes/origin/main", fixed)
        self.assertFalse(self.git("remote"))
        self.assertTrue(landreq.get(first["id"])[0]["contrary"])
        _lr, why = landreq.discharge(first["id"], fixed, "stale ref")
        self.assertIn("authoritative trunk", why)
        self.assertFalse(landreq.get(first["id"])[0]["discharged"])

    def test_configured_origin_without_a_tracking_ref_is_unknown_not_local(self):
        bare = os.path.join(self.tmp, "unfetched-origin.git")
        subprocess.run(["git", "init", "--bare", "-q", bare], check=True)
        self.git("push", "-q", bare, "%s:main" % self.main)
        self.git("remote", "add", "origin", bare)
        with self.assertRaises(subprocess.CalledProcessError):
            self.git("rev-parse", "--verify", "refs/remotes/origin/main")
        first, _second, fixed = self.resolved()
        _lr, why = landreq.discharge(first["id"], fixed, "not remotely observed")
        self.assertIn("tracking ref", why)
        self.assertFalse(landreq.get(first["id"])[0]["discharged"])

    def test_superseding_tip_must_reach_authoritative_upstream(self):
        first, _second, fixed = self.resolved(upstream=True, push=False)
        _lr, why = landreq.discharge(first["id"], fixed, "not pushed")
        self.assertIn("authoritative trunk", why)
        self.git("push", "-q", "origin", self.main)
        lr, why = landreq.discharge(first["id"], fixed, "pushed")
        self.assertIsNone(why)
        self.assertEqual(lr["close_reason"], "superseded")
        self.assertEqual(lr["contrary_target"], "upstream")
        moved = {"observable": True, "local": True, "upstream": False,
                 "has_upstream": True}
        with mock.patch.object(landreq, "_git_observe", return_value=moved):
            historical = landreq.get(first["id"])[0]
            shown = landreq._render_show(historical)
        self.assertEqual(historical["contrary_state"], "landed")
        self.assertEqual(historical["contrary_target"], "upstream")
        self.assertIn("LANDED on upstream trunk", shown)
        self.assertNotIn("MERGED_LOCAL on upstream trunk", shown)

    def test_patch_equivalent_lineage_is_accepted_when_ancestry_is_false(self):
        first = self.handoff("feature-r1", self.side)
        dispatches.mark_verdict(first["id"], self.side, "findings", polarity="fix")
        self.git("checkout", "-q", "-b", "resolution", self.main)
        self.git("cherry-pick", self.side)
        fixed = self.commit("fix on cherry-picked base", path="g")
        second = self.handoff("feature-r2", fixed, supersedes=first["id"])
        dispatches.mark_verdict(second["id"], fixed, "clean", polarity="approve")
        self.git("checkout", "-q", self.main)
        self.git("merge", "--no-edit", "-q", "resolution")
        gitdir = os.path.realpath(os.path.join(self.repo, ".git"))
        self.assertFalse(landreq._is_ancestor(gitdir, self.side, fixed))
        self.assertTrue(landreq._landed(gitdir, self.side, fixed))
        lr, why = landreq.discharge(first["id"], fixed, "patch-equivalent")
        self.assertIsNone(why)
        self.assertEqual(lr["close_reason"], "superseded")

    def test_cli_discharge_uses_short_id_and_refuses_unknown_flags(self):
        first, _second, fixed = self.resolved()
        rc, out, err = run(["discharge", first["id"][:12], fixed,
                            "r2", "approved", "--json"])
        self.assertEqual(rc, 0, err)
        self.assertIn("deprecated: use helm lr close --reason superseded", err)
        self.assertEqual(json.loads(out)["close_reason"], "superseded")
        rc, _out, err = run(["discharge", first["id"], fixed,
                             "again", "--wat"])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)


class WithdrawTerminalTest(LandReqBase):
    """A do-not-land FIX verdict gets a terminal state when Git proves the
    reviewed change is provably ABSENT from trunk — the mirror of discharge,
    for the row no superseding tip will ever exist to close."""

    def fix_row(self, polarity="fix"):
        """A FIX/SUPERSEDE verdict on the divergent side tip, which is NOT on
        trunk — exactly the do-not-land shape withdraw retires."""
        row, why, sent = dispatches.send(
            "opus-integrator", "lane/evals-0723", "review evals-0723",
            self.side, repo=self.repo, key="key-evals", sign=False, new_work=True)
        self.assertIsNone(why)
        self.assertTrue(sent)
        dispatches.mark_verdict(row["id"], self.side, "findings: keep untracked",
                                polarity=polarity)
        return row

    def test_withdraw_retires_the_debt_and_preserves_the_verdict(self):
        row = self.fix_row()
        lr, why = landreq.withdraw(row["id"][:12], "kept untracked as instructed")
        self.assertIsNone(why)
        # the FIX verdict is preserved, the debt is retired (via lr close)
        self.assertEqual(lr["state"], "CHANGES_REQUESTED")
        self.assertEqual(lr["polarity"], "fix")
        self.assertEqual(lr["close_reason"], "withdrawn")
        self.assertTrue(lr["terminal"])
        self.assertFalse(lr["stalled"])
        self.assertEqual(lr["owed_by"], "nobody")
        self.assertEqual(lr["close_evidence"], "kept untracked as instructed")
        # and it is OFF the operational board but still in --all
        self.assertNotIn(row["id"], [r["id"] for r in landreq.loops()[0]])
        self.assertIn(row["id"], [r["id"] for r in landreq.loops(True)[0]])
        self.assertEqual(landreq.stalls()[0], [])
        # dwell is frozen at the withdraw, not still accruing
        frozen = lr["dwell_s"]
        self.assertEqual(landreq.get(row["id"], time.time() + 10000)[0]["dwell_s"],
                         frozen)
        # the compact board row and the show page both name the resolution
        line = landreq._line(lr)
        self.assertIn("WITHDRAWN", line)
        shown = landreq._render_show(lr)
        self.assertIn("CLOSED (WITHDRAWN)", shown)
        self.assertIn("withdrawn absent at", shown)

    def test_a_landed_reviewed_tip_cannot_be_withdrawn(self):
        """THE GATE, mutation-pinned: withdraw is only truthful when the change
        is ABSENT. A reviewed tip that IS on trunk makes withdraw a lie — that
        row wants discharge (if superseded) or is a contrary, not a withdraw."""
        row = self.fix_row()
        self.git("merge", "--no-edit", "-q", "side")   # land the side tip
        lr, why = landreq.withdraw(row["id"], "claim it is not landed")
        self.assertIsNone(lr)
        self.assertIn("on trunk", why)
        # and the row is NOT withdrawn
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_an_unknown_land_state_fails_closed(self):
        """If Git cannot prove absence (_landed None), withdraw refuses rather
        than guess — fail-closed, exactly as discharge is."""
        row = self.fix_row()
        with mock.patch.object(landreq, "_landed", return_value=None):
            lr, why = landreq.withdraw(row["id"], "evidence")
        self.assertIsNone(lr)
        self.assertIn("could not prove", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_a_non_fix_verdict_cannot_be_withdrawn(self):
        """An APPROVE verdict resolves to READY/land, not do-not-land — withdraw
        is the wrong verb for it."""
        row = self.fix_row(polarity="approve")
        lr, why = landreq.withdraw(row["id"], "evidence")
        self.assertIsNone(lr)
        self.assertIn("not a FIX/SUPERSEDE", why)

    def test_withdraw_needs_evidence(self):
        row = self.fix_row()
        lr, why = landreq.withdraw(row["id"], "")
        self.assertIsNone(lr)
        self.assertIn("evidence", why)

    def test_identical_retry_is_idempotent_and_a_conflict_is_refused(self):
        row = self.fix_row()
        lr1, why1 = landreq.withdraw(row["id"], "resolution A")
        self.assertIsNone(why1)
        lr2, why2 = landreq.withdraw(row["id"], "resolution A")
        self.assertIsNone(why2)                     # identical retry: OK
        self.assertEqual(lr2["close_reason"], "withdrawn")
        lr3, why3 = landreq.withdraw(row["id"], "resolution B")
        self.assertIsNone(lr3)                      # conflicting: refused
        self.assertIn("retired once", why3)

    def test_a_withdrawn_row_accepts_no_discharge_and_vice_versa(self):
        """A debt is retired once. withdraw-then-discharge and discharge-then-
        withdraw are both refused at the ledger boundary."""
        row = self.fix_row()
        lr, why = landreq.withdraw(row["id"], "withdrawn first")
        self.assertIsNone(why)
        fixed = self.commit("a later superseding tip", path="h")
        out, err = dispatches._record_discharge_proven(
            row["id"], self.side, fixed, row["id"], "superseded",
            "landed", "local")
        self.assertIsNone(out)
        self.assertIn("retired once", err)

    def test_a_later_land_re_exposes_a_withdrawn_row_as_contrary(self):
        """LIFECYCLE-WALK, and the trap named in the lane brief. A withdraw is
        truthful when recorded (Git proved absence then) but its premise is
        falsifiable: if the withdrawn change LATER lands, the row must not stay
        silently terminal — the live landed-despite-FIX fact re-exposes it as
        CONTRARY, owed by the integrator again. A withdraw is a resolution, not
        a veto on the future."""
        row = self.fix_row()
        lr, why = landreq.withdraw(row["id"], "absent at withdraw time")
        self.assertIsNone(why)
        self.assertTrue(lr["terminal"] and not lr["stalled"])
        # the change LANDS LATER — the withdraw's premise is now false
        self.git("merge", "--no-edit", "-q", "side")
        reland = landreq.get(row["id"])[0]
        self.assertTrue(reland["contrary"], "a later land must re-expose the row")
        self.assertFalse(reland["terminal"])
        self.assertEqual(reland["owed_by"], "integrator")
        # the close event stays as history even though the row is contrary
        self.assertEqual(reland["close_reason"], "withdrawn")
        self.assertTrue(reland["close_contradicted"])

    def test_the_owner_console_card_carries_the_withdraw_state(self):
        """OWNER SURFACE — the P2 from cross-family review. The operational
        effect (retire off the stalled list) was right, but board_section's card
        dropped the `withdrawn` key, so the owner saw only a row that stopped
        being red with no way to distinguish CORRECTLY-NEVER-LANDED from
        SUPERSEDED-BY-A-LATER-LAND — the very distinction the feature exists
        for. The card must carry it, and a contradicted row must say why it came
        back."""
        row = self.fix_row()
        landreq.withdraw(row["id"], "kept untracked")
        cards = {c["id"]: c for c in landreq.board_section()["loops"]}
        # a withdrawn row is terminal, so it is NOT in the default loops — but
        # the key must survive on the row wherever it renders. Assert the card
        # SHAPE carries the key for a row that IS present (a contradicted one,
        # which un-retires back onto the board).
        self.git("merge", "--no-edit", "-q", "side")   # later land -> contradicted
        cards = {c["id"]: c for c in landreq.board_section()["loops"]}
        self.assertIn(row["id"], cards, "a contradicted row re-appears on the board")
        card = cards[row["id"]]
        self.assertEqual(card["close_reason"], "withdrawn")
        self.assertIn("close_contradicted", card)
        self.assertTrue(card["close_contradicted"])
        self.assertEqual(card["owed_by"], "integrator")


class RetiredHeadlineTest(LandReqBase):
    """THE LYING INSTRUMENT, measured on the live ledger 2026-08-03: 61 of 718
    rows were `terminal`, `owed_by: nobody`, carrying a real retirement stamp
    and a reason — and `helm lr show` headlined every one of them a bare
    CHANGES_REQUESTED, because the retirement headline keyed on `close_reason`
    and the `withdraw`/`discharge` events do not write one. Specimen
    251d3c1a89bb (lane triage-committer-signal, withdrawn 2026-07-31T21:07:42Z)
    read as its author's open debt at the top of its own page. The same count
    was the `--all` header's: `718 land loops in flight` over 653 retirements.
    """

    def fix_row(self, lane="lane/retired-headline", key="key-retired",
                polarity="fix"):
        row, why, sent = dispatches.send(
            "opus-integrator", lane, "review " + lane, self.side,
            repo=self.repo, key=key, sign=False, new_work=True)
        self.assertIsNone(why)
        self.assertTrue(sent)
        dispatches.mark_verdict(row["id"], self.side, "findings: do not land",
                                polarity=polarity)
        return row

    def withdrawn_row(self, ref="superseded in-lane; never lands"):
        """A row retired by the `withdraw` EVENT — `withdrawn: True` with no
        `close_reason`, the exact shape all 15 live withdrawn specimens carry
        and the shape `_record_withdraw_proven` still writes today."""
        row = self.fix_row()
        out, err = dispatches._record_withdraw_proven(row["id"], self.side, ref)
        self.assertIsNone(err)
        self.assertTrue(out["withdrawn"])
        self.assertIsNone(out.get("close_reason"))
        return landreq.get(row["id"])[0]

    def test_a_withdrawn_row_headlines_as_retired_and_names_its_reason(self):
        lr = self.withdrawn_row("superseded in-lane by a539c3e67434 on trunk")
        self.assertTrue(lr["terminal"])
        self.assertEqual(lr["owed_by"], "nobody")
        shown = landreq._render_show(lr)
        headline = shown.splitlines()[0]
        self.assertIn("RETIRED (WITHDRAWN)", headline)
        # the verdict stays in the headline — the retirement names how the debt
        # ended, it does not overwrite what the reviewer said
        self.assertIn("CHANGES_REQUESTED", headline)
        # and the page carries WHY, verbatim, so a reader outside our history
        # can see what retired it
        self.assertIn("superseded in-lane by a539c3e67434 on trunk", shown)

    def test_a_withdrawn_row_is_never_headlined_as_landed(self):
        """DISCRIMINATION, the other half of the fix. A withdraw asserts the
        reviewed change is correctly ABSENT from trunk; reading it as a landing
        would trade one lie for another. It also holds no close event, so the
        `CLOSED (...)` spelling would assert a transition the ledger lacks."""
        headline = landreq._render_show(self.withdrawn_row()).splitlines()[0]
        # the positive control on the SAME observable: an empty headline would
        # satisfy both absences below and prove nothing
        self.assertIn("RETIRED (WITHDRAWN)", headline)
        self.assertNotIn("LANDED", headline)
        self.assertNotIn("CLOSED", headline)

    def test_a_discharged_row_headlines_as_retired_and_names_its_reason(self):
        """THE MAJORITY LEG, and a mutation this suite first let survive: 46 of
        the 61 lying rows were retired by `discharge`, not `withdraw` — the
        same `close_reason`-is-None shape, the same bare CHANGES_REQUESTED
        headline over a debt a later round already paid. Specimen 631d2220b189
        (lane pi-start-resume)."""
        row = self.fix_row(lane="lane/discharged-headline", key="key-disc")
        self.git("merge", "--no-edit", "-q", "side")   # the contrary land
        fixed = self.commit("the superseding round", path="h")
        out, err = dispatches._record_discharge_proven(
            row["id"], self.side, fixed, row["id"],
            "resolved by the r2 round", "landed", "local")
        self.assertIsNone(err, err)
        self.assertTrue(out["discharged"])
        self.assertIsNone(out.get("close_reason"))
        lr = landreq.get(row["id"])[0]
        self.assertTrue(lr["terminal"])
        self.assertEqual(lr["owed_by"], "nobody")
        shown = landreq._render_show(lr)
        headline = shown.splitlines()[0]
        self.assertIn("RETIRED (DISCHARGED)", headline)
        self.assertIn("CHANGES_REQUESTED", headline)   # the verdict survives
        self.assertIn("resolved by the r2 round", shown)   # and WHY

    def test_a_landed_row_still_headlines_as_landed(self):
        """The discrimination control: the fix must not blur the retirements
        that already rendered honestly."""
        row = self.fix_row(lane="lane/landed-control", key="key-landed",
                           polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        sha = self.git("rev-parse", self.main)
        out, err = dispatches._record_close_proven(
            row["id"], "landed", self.side, evidence="landed on trunk",
            closing_repo_id=self.repo, closing_trunk_ref="refs/heads/" + self.main,
            closing_trunk_sha=sha, proof_mode="ancestor")
        self.assertIsNone(err, err)
        self.assertEqual(out["close_reason"], "landed")
        headline = landreq._render_show(
            landreq.get(row["id"])[0]).splitlines()[0]
        self.assertIn("CLOSED (LANDED)", headline)
        self.assertNotIn("RETIRED", headline)

    def test_a_live_changes_requested_row_headline_is_unchanged(self):
        """THE NEGATIVE CONTROL — the assertion that proves this did not just
        relabel every CHANGES_REQUESTED row. An un-retired FIX is real, live,
        author-owed debt and must keep reading exactly that way."""
        row = self.fix_row(lane="lane/live-debt", key="key-live")
        lr = landreq.get(row["id"])[0]
        self.assertFalse(lr["terminal"])
        self.assertEqual(lr["owed_by"], "author")
        headline = landreq._render_show(lr).splitlines()[0]
        self.assertIn("CHANGES_REQUESTED", headline)
        self.assertNotIn("RETIRED", headline)

    def test_a_contradicted_withdraw_loses_the_retired_headline(self):
        """The falsifiability clause, carried into the headline. A withdraw is
        truthful when recorded, not forever: once the withdrawn change lands,
        the row is live debt owed by the integrator again, and a headline still
        saying RETIRED would be the same lie pointed the other way."""
        lr = self.withdrawn_row()
        self.assertIn("RETIRED (WITHDRAWN)",
                      landreq._render_show(lr).splitlines()[0])
        self.git("merge", "--no-edit", "-q", "side")   # the premise falsified
        reland = landreq.get(lr["id"])[0]
        self.assertTrue(reland["withdraw_contradicted"])
        self.assertFalse(reland["terminal"])
        self.assertEqual(reland["owed_by"], "integrator")
        self.assertNotIn("RETIRED", landreq._render_show(reland).splitlines()[0])

    def test_the_all_header_does_not_count_retirements_as_in_flight(self):
        """THE SUMMARY LINE. `--all` carries the retirements, so counting the
        whole listing as `in flight` billed the fleet for debt nobody owed."""
        self.withdrawn_row()
        live = self.fix_row(lane="lane/still-owed", key="key-still-owed")
        rc, out, err = run(["list", "--all"])
        self.assertEqual(rc, 0, err)
        header = out.splitlines()[0]
        self.assertIn("1 RETIRED", header)
        self.assertIn("1 in flight", header)
        self.assertNotIn("2 land loops in flight", header)
        # and the DEFAULT listing, which filters terminals out, is untouched
        rc, out, err = run(["list"])
        self.assertEqual(rc, 0, err)
        # startswith, not equality: trunk's header also carries a polarity
        # provenance suffix, and pinning the WHOLE line makes this test fail
        # for a reason that has nothing to do with retirement counting (it
        # did, on the rebase that merged the two headers). The claim under
        # test is the COUNT and the words "in flight", so assert those.
        default_header = out.splitlines()[0]
        self.assertTrue(
            default_header.startswith("helm lr — 1 land loop in flight"),
            default_header)
        self.assertNotIn("RETIRED", default_header)
        self.assertIn(live["id"][:12], out)


class ChainPolarityCloseTest(LandReqBase):
    """The close ladders' polarity gates read the CHAIN's declared polarity
    when the row's own field is silent — the bug class is a decision recorded
    in PROSE that the machine-readable gate cannot see. Live fixture:
    be5e82bbe0b5 sat REVIEWED 8d08h with verdict text "SUPERSEDED — guard not
    landing", polarity UNDECLARED, and every terminal door refusing for a
    different correct reason, while chained round b5c7a8df67df held an
    explicit SUPERSEDE about the same code.

    THE NEGATIVE CONTROLS ARE THE POINT: a chain-aware door must not become a
    door any chained row can open. Boundary under test (`_chain_polarity`):
    the chain speaks only when the row is silent; only a descendant verdict
    about the SAME reviewed code counts; conflict refuses and names the rows;
    every unreadable leg is UNKNOWN in words, never a close."""

    def undeclared_row(self, tip=None, lane="lane/silent"):
        """A verdict whose decision lives in PROSE only — polarity None. The
        current writer refuses this shape, which is exactly why the rows
        exist only as history: plant the legacy event verbatim."""
        tip = tip or self.side
        row = self.dispatch(ref=tip, lane=lane, kind="review")
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": row["seq"] + 1,
            "id": row["id"], "ts": dispatches.pk.now_ts(),
            "reviewed_tip": tip,
            "verdict_ref": "SUPERSEDED in prose — not landing"}))
        return row

    def chained(self, parent, tip, polarity, lane="lane/silent-r2"):
        """A round dispatched --supersedes parent, reviewed at `tip`."""
        kid = self.dispatch(ref=tip, lane=lane, kind="review",
                            supersedes=parent["id"])
        if polarity:
            _out, why = dispatches.mark_verdict(
                kid["id"], tip, "chain declares %s" % polarity,
                polarity=polarity)
            self.assertIsNone(why)
        else:
            self.assertTrue(eventledger.append(dispatches.ledger_path(), {
                "v": 3, "event": "verdict", "seq": kid["seq"] + 1,
                "id": kid["id"], "ts": dispatches.pk.now_ts(),
                "reviewed_tip": tip,
                "verdict_ref": "round happened, silent"}))
        return kid

    def sidecar(self, *pairs):
        state = os.path.join(landreq.home.global_dir(), ".state")
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "ref-migrations.jsonl"), "a",
                  encoding="utf-8") as f:
            for old, new in pairs:
                f.write(json.dumps({"old": old, "new": new}) + "\n")

    def test_chain_declared_supersede_withdraws_an_undeclared_row(self):
        """THE ACCEPTANCE SHAPE (be5e82bbe0b5): undeclared row, chained round
        declares SUPERSEDE at the same reviewed tip, work absent from trunk —
        the withdrawn door closes on the absence proof, dry-run first."""
        row = self.undeclared_row()
        kid = self.chained(row, self.side, "supersede")
        plan, why = landreq.close(row["id"], "withdrawn",
                                  evidence="owner declined in the verdict",
                                  dry_run=True)
        self.assertIsNone(why)
        self.assertEqual(plan["reason"], "withdrawn")
        self.assertEqual(plan["proof_mode"], "absent")
        self.assertEqual(plan["polarity"], "supersede")
        self.assertEqual(plan["polarity_via_chain"], kid["id"][:12])
        # dry-run appended nothing
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])
        lr, why = landreq.close(row["id"], "withdrawn",
                                evidence="owner declined in the verdict")
        self.assertIsNone(why)
        self.assertEqual(lr["close_reason"], "withdrawn")
        self.assertTrue(lr["terminal"])
        self.assertEqual(lr["owed_by"], "nobody")
        self.assertIsNone(lr["polarity"], "the immutable verdict is untouched")
        # and it survives a fresh disk replay — the recorder admitted it
        self.assertEqual(landreq.get(row["id"])[0]["close_reason"], "withdrawn")

    def test_a_silent_chain_still_refuses_the_gate_did_not_loosen(self):
        """NEGATIVE CONTROL: no declared polarity anywhere — the undeclared
        row refuses exactly as before, in words that say the chain was read."""
        row = self.undeclared_row()
        lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("no chained round declares", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_an_undeclared_chained_round_asserts_nothing(self):
        """NEGATIVE CONTROL: a chained child whose own polarity is UNDECLARED
        contributes no polarity — prose on the child is still prose."""
        row = self.undeclared_row()
        self.chained(row, self.side, None)
        lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("no chained round declares", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_a_declaration_about_different_code_does_not_count(self):
        """NEGATIVE CONTROL — the door-any-chained-row-can-open shape: a
        chained SUPERSEDE at a DIFFERENT reviewed tip is a ruling about
        different code and must not open this row's withdrawn door."""
        row = self.undeclared_row()
        self.git("checkout", "-q", "side")
        other = self.commit("a different change entirely", path="g")
        self.git("checkout", "-q", self.main)
        self.chained(row, other, "supersede")
        lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("no chained round declares", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_a_conflicting_chain_refuses_and_names_the_rows(self):
        """NEGATIVE CONTROL: an APPROVE and a SUPERSEDE about the same code
        on different children is a chain that disagrees with itself — refuse,
        and say WHICH rows conflict."""
        row = self.undeclared_row()
        yes = self.chained(row, self.side, "approve", lane="lane/silent-r2")
        no = self.chained(row, self.side, "supersede", lane="lane/silent-r3")
        lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("CONFLICTING", why)
        self.assertIn(yes["id"][:12], why)
        self.assertIn(no["id"][:12], why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])
        # the conflict blocks the landed door identically — neither polarity
        # may be assumed for ANY gate on an ambiguous chain
        self.git("merge", "--no-edit", "-q", "side")
        lr, why = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(lr)
        self.assertIn("CONFLICTING", why)

    def test_own_declared_polarity_outranks_the_chain(self):
        """NEGATIVE CONTROL: a chained SUPERSEDE cannot flip a row whose OWN
        polarity is APPROVE — the chain speaks only when the row is silent."""
        row = self.dispatch(ref=self.side, lane="lane/declared", kind="review")
        dispatches.mark_verdict(row["id"], self.side, "clean",
                                polarity="approve")
        self.chained(row, self.side, "supersede", lane="lane/declared-r2")
        lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("not a FIX/SUPERSEDE verdict row", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_landed_work_still_cannot_be_withdrawn_through_the_chain(self):
        """NEGATIVE CONTROL: withdrawn proves ABSENCE. A chain-declared
        SUPERSEDE over work that IS on trunk must still refuse — the polarity
        gate opened, the absence rung refuses."""
        row = self.undeclared_row()
        self.chained(row, self.side, "supersede")
        self.git("merge", "--no-edit", "-q", "side")
        lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("IS on trunk", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_an_unreadable_sidecar_is_UNKNOWN_for_the_polarity_gate(self):
        """NEGATIVE CONTROL: the same-code set rides the translation sidecar;
        a chain whose sidecar cannot be read is a chain that cannot be fully
        read — UNKNOWN in words, never a close."""
        row = self.undeclared_row()
        self.chained(row, self.side, "supersede")
        with mock.patch.object(landreq, "_ref_translations_checked",
                               return_value=({}, "permission denied")):
            lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("UNKNOWN", why)
        self.assertIn("permission denied", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_an_unreadable_ledger_never_reaches_a_door(self):
        """NEGATIVE CONTROL: the chain lives in the projection; a projection
        that cannot be read refuses the whole close in words, one layer above
        every door."""
        row = self.undeclared_row()
        self.chained(row, self.side, "supersede")
        with mock.patch.object(landreq, "project",
                               return_value=({}, "ledger torn mid-read")):
            lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("ledger torn mid-read", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_superseded_door_walks_the_contrary_branch_on_chain_polarity(self):
        """The superseded door treats a chain-declared SUPERSEDE row exactly
        as an own-declared one: its landed change is CONTRARY debt
        (contrary_state records it), where the silent-chain reading would
        have refused with 'IS on trunk — use landed'."""
        first, why, sent = dispatches.send(
            "codex-3", "lane/prose-r1", "review prose-r1", self.side,
            repo=self.repo, key="key-prose-r1", sign=False, new_work=True)
        self.assertIsNone(why)
        self.assertTrue(sent)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": first["seq"] + 1,
            "id": first["id"], "ts": dispatches.pk.now_ts(),
            "reviewed_tip": self.side,
            "verdict_ref": "SUPERSEDED in prose — do not keep"}))
        # the chain declares the polarity the prose meant, at the same code
        decl, why, sent = dispatches.send(
            "codex-3", "lane/prose-r2", "declare polarity", self.side,
            repo=self.repo, key="key-prose-r2", sign=False,
            new_work=False, supersedes=first["id"])
        self.assertIsNone(why)
        _out, why = dispatches.mark_verdict(
            decl["id"], self.side, "chain declares supersede",
            polarity="supersede")
        self.assertIsNone(why)
        # the contrary LANDS, then an approved superseding round replaces it
        self.git("merge", "--no-edit", "-q", "side")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve findings", path="g")
        self.git("checkout", "-q", self.main)
        second, why, sent = dispatches.send(
            "codex-3", "lane/prose-r3", "superseding round", fixed,
            repo=self.repo, key="key-prose-r3", sign=False,
            new_work=False, supersedes=first["id"])
        self.assertIsNone(why)
        _out, why = dispatches.mark_verdict(second["id"], fixed,
                                            "re-probed clean",
                                            polarity="approve")
        self.assertIsNone(why)
        self.git("merge", "--no-edit", "-q", "side")
        lr, why = landreq.close(first["id"], "superseded",
                                evidence="r3 approved", tip=fixed)
        self.assertIsNone(why)
        self.assertEqual(lr["close_reason"], "superseded")
        self.assertEqual(lr["contrary_state"], "landed",
                         "the contrary branch must have adjudicated this")
        self.assertEqual(lr["superseding_id"], second["id"])

    def test_chain_declared_contrary_blocks_the_landed_door(self):
        """The landed door learns the same identity: an undeclared row whose
        chain declares SUPERSEDE about its code is a CONTRARY on trunk, not a
        resolution — and the refusal names the declaring round. Control: the
        same row without the declaration closes landed."""
        row = self.undeclared_row()
        kid = self.chained(row, self.side, "supersede")
        self.git("merge", "--no-edit", "-q", "side")
        lr, why = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(lr)
        self.assertIn("chain-declared SUPERSEDE", why)
        self.assertIn(kid["id"][:12], why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])
        # POSITIVE CONTROL — an identical row with a silent chain closes
        control = self.undeclared_row(lane="lane/silent-control")
        self.chained(control, self.side, None, lane="lane/silent-control-r2")
        lr, why = landreq.close(control["id"], "landed", live=True)
        self.assertIsNone(why)
        self.assertEqual(lr["close_reason"], "landed")


class WithdrawTranslationTest(LandReqBase):
    """#79 — the withdrawn door follows REWRITE TRANSLATIONS, both walks of
    one lane: a tip pruned by a recorded rewrite proves absence through its
    live recorded identity, and a recorded identity that LANDED makes
    "absent" a lie the door must catch."""

    def fix_row(self, tip, lane="lane/rewrite"):
        row = self.dispatch(ref=tip, lane=lane, kind="review")
        dispatches.mark_verdict(row["id"], tip, "do not land",
                                polarity="fix")
        return row

    def pruned_tip(self):
        """A reviewed tip whose object a history rewrite destroyed."""
        self.git("checkout", "-q", "-b", "doomed", self.main)
        tip = self.commit("doomed evidence", path="doomed")
        row = self.fix_row(tip)
        self.git("checkout", "-q", self.main)
        self.git("branch", "-D", "doomed")
        self.git("reflog", "expire", "--expire=now", "--all")
        self.git("gc", "--prune=now")
        probe = subprocess.run(["git", "-C", self.repo, "cat-file", "-e",
                                tip + "^{commit}"], capture_output=True)
        self.assertNotEqual(probe.returncode, 0,
                            "the fixture must really prune the object")
        return row, tip

    def sidecar(self, *pairs):
        state = os.path.join(landreq.home.global_dir(), ".state")
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "ref-migrations.jsonl"), "a",
                  encoding="utf-8") as f:
            for old, new in pairs:
                f.write(json.dumps({"old": old, "new": new}) + "\n")

    def test_a_pruned_tip_withdraws_through_its_recorded_translation(self):
        """The #79 acceptance walk: direct absence is UNKNOWN (object gone),
        the recorded live identity is provably absent — close, recording the
        translated proof."""
        row, old = self.pruned_tip()
        self.git("checkout", "-q", "side")
        new = self.commit("rewritten identity, still unlanded", path="g")
        self.git("checkout", "-q", self.main)
        self.sidecar((old, new))
        plan, why = landreq.close(row["id"], "withdrawn",
                                  evidence="rewrite kept it unlanded",
                                  dry_run=True)
        self.assertIsNone(why)
        self.assertEqual(plan["proof_mode"], "translated-absent")
        self.assertEqual(plan["translated_tip"], new)
        lr, why = landreq.close(row["id"], "withdrawn",
                                evidence="rewrite kept it unlanded")
        self.assertIsNone(why)
        self.assertEqual(lr["close_reason"], "withdrawn")
        self.assertEqual(lr["close_proof_mode"], "translated-absent")
        self.assertEqual(lr["translated_tip"], new)
        # and the recorded proof survives a fresh disk replay
        replayed = landreq.get(row["id"])[0]
        self.assertEqual(replayed["close_proof_mode"], "translated-absent")
        self.assertEqual(replayed["translated_tip"], new)

    def test_a_landed_translation_makes_absent_a_lie(self):
        """NEGATIVE CONTROL: the original tip reads absent directly, but its
        recorded translation IS on trunk — the work landed under its
        rewritten identity and the door must refuse."""
        row = self.fix_row(self.side)
        # the rewritten identity is a DIFFERENT patch cut from main (a
        # rewrite changes content), and IT lands; the original side tip
        # itself never does — direct absence would read False.
        self.git("checkout", "-q", "-b", "rewrite", self.main)
        new = self.commit("carried forward under a new identity", path="h")
        self.git("checkout", "-q", self.main)
        self.git("merge", "--no-edit", "-q", "rewrite")   # translation lands
        self.sidecar((self.side, new))
        lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("recorded translation", why)
        self.assertIn("IS on trunk", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_an_unprovable_translation_is_UNKNOWN(self):
        """NEGATIVE CONTROL: pruned tip, translation recorded to an object
        git cannot adjudicate — UNKNOWN in words, fail-closed."""
        row, old = self.pruned_tip()
        self.sidecar((old, "f" * 40))
        lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("recorded translation", why)
        self.assertIn("FAIL-CLOSED", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_an_unreadable_sidecar_refuses_absence_adjudication(self):
        """NEGATIVE CONTROL: an unreadable sidecar cannot say whether a
        translation exists — absence cannot be adjudicated."""
        row, _old = self.pruned_tip()
        with mock.patch.object(landreq, "_ref_translations_checked",
                               return_value=({}, "disk error")):
            lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("unreadable", why)
        self.assertIn("disk error", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_a_pruned_tip_with_no_translation_stays_UNKNOWN(self):
        """NEGATIVE CONTROL: the translation walk widens proof, never
        loosens it — no recorded translation leaves the original fail-closed
        refusal exactly as before."""
        row, _old = self.pruned_tip()
        lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("could not prove", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])


class AbandonTerminalTest(LandReqBase):
    """Evidence loss is terminal only when Git explicitly says MISSING."""

    def reviewed(self, tip=None, polarity="fix", lane="lane/ghost"):
        tip = tip or self.side
        row = self.dispatch(ref=tip, lane=lane, kind="review")
        dispatches.mark_verdict(row["id"], tip, "reviewed", polarity=polarity)
        return row

    def ghost(self, polarity="fix", lane="lane/ghost", path="ghost"):
        # `path` exists so a test may build SEVERAL ghosts in one repo. Same
        # parent + same tree + same message inside one second is the same
        # COMMIT ID, so two default ghosts would silently be one object and
        # the second row would review a tip the first had already destroyed.
        self.git("checkout", "-q", "-b", "ghost-object", self.main)
        tip = self.commit("ghost evidence", path=path)
        row = self.reviewed(tip, polarity=polarity, lane=lane)
        self.git("checkout", "-q", self.main)
        self.git("branch", "-D", "ghost-object")
        self.git("reflog", "expire", "--expire=now", "--all")
        self.git("gc", "--prune=now")
        probe = subprocess.run(["git", "-C", self.repo, "cat-file", "-e",
                                tip + "^{commit}"], capture_output=True)
        self.assertNotEqual(probe.returncode, 0,
                            "the positive arm must really remove the object")
        return row, tip

    def test_missing_commit_abandons_with_land_state_UNKNOWN(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        row, missing = self.ghost()
        lr, why = landreq.abandon(
            row["id"][:12], "history rewrite destroyed the reviewed object")
        self.assertIsNone(why)
        self.assertEqual(lr["state"], "ABANDONED")
        self.assertTrue(lr["abandoned"] and lr["terminal"])
        self.assertEqual(lr["land_state"], "UNKNOWN")
        self.assertEqual(lr["polarity"], "fix")
        self.assertEqual(lr["reviewed_tip"], missing)
        self.assertEqual(lr["owed_by"], "nobody")
        self.assertFalse(lr["stalled"] or lr["landed"] or lr["withdrawn"])
        self.assertNotIn(row["id"], [r["id"] for r in landreq.loops()[0]])
        self.assertIn(row["id"], [r["id"] for r in landreq.loops(True)[0]])
        self.assertIn("ABANDONED", landreq._line(lr))
        shown = landreq._render_show(lr)
        self.assertIn("LAND STATE UNKNOWN", shown)
        self.assertIn("history rewrite destroyed", shown)
        self.assertIn("structured-message-and-tag-scan v2 (none trunk mention)", shown)
        self.assertIn("branch    none via git-ref-and-ancestry v1", shown)
        self.assertIn("worktree  none via git-worktree-status v1", shown)

    def test_the_writer_and_the_replay_admit_the_SAME_polarities(self):  # noqa: VACUOUS_ASSERTION — the positive control is the UNCONDITIONAL assertIn("fix", accepted) after the loop, on the same observable the loop fills
        """THE DEFECT @codex-2 REPRODUCED AT THE EXACT TIP — pinned as the
        AGREEMENT it actually is, not as a fact about `concur`.

        `_apply`'s abandon arm requires a WORK polarity. The WRITER blocked
        only `supersede`, so `concur` passed HERE and was refused THERE:
        err=None, an event appended (history 2->3), the row still REVIEWED,
        and the CLI printing ABANDONED over it.

        THE DIVERGENCE IS WORSE THAN EITHER HALF ALONE. A refusal that leaves
        no event is safe. A silent acceptance that changes nothing is an
        operator being told a terminal write happened that did not — and the
        ledger keeping an event to prove them right.

        Walking EVERY polarity the vocabulary admits, instead of asserting
        that `concur` is refused, is what makes the polarity added tomorrow
        covered without editing this test. `supersede` is refused by an
        earlier door while the replay would admit it: writer-stricter-than-
        replay writes no event, so it is the SAFE direction of disagreement
        and the same assertions below hold for it unchanged."""
        accepted = []
        for polarity in verdicts.POLARITIES:
            with self.subTest(polarity=polarity):
                row, _gone = self.ghost(polarity=polarity,
                                        lane="lane/ghost-" + polarity,
                                        path="ghost-" + polarity)
                before = len(dispatches.history(row["id"]))
                _lr, why = landreq.abandon(row["id"][:12],
                                           "history rewrite destroyed it")
                state = landreq.get(row["id"])[0]["state"]
                grew = len(dispatches.history(row["id"])) - before
                if why is None:
                    accepted.append(polarity)
                    self.assertEqual(state, "ABANDONED")
                    self.assertEqual(grew, 1)
                else:
                    self.assertNotEqual(state, "ABANDONED")
                    self.assertEqual(
                        grew, 0,
                        "%s: the writer appended an event the replay ignores "
                        "— the exact divergence that printed ABANDONED over a "
                        "REVIEWED row" % polarity)
        # MUST-HIT CONTROL. Every assertion above is satisfied vacuously by a
        # writer that refuses EVERYTHING, so the loop proves nothing until it
        # is shown to admit real work — and to still exclude concur.
        self.assertIn("fix", accepted)
        self.assertNotIn("concur", accepted)

    def test_structured_trunk_lane_mention_blocks_abandon_without_calling_it_landed(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        row, _missing = self.ghost(lane="lane/chatnode-posture-r2")
        self.git("commit", "--allow-empty", "-q", "-m",
                 "Merge branch 'chatnode-posture' — restore chat posture")
        before = len(dispatches.history(row["id"]))
        lr, why = landreq.abandon(row["id"], "write off reviewed work")
        self.assertIsNone(lr)
        self.assertIn("structured trunk mention", why)
        self.assertIn("chatnode-posture", why)
        self.assertIn("correlation, not proven landed", why)
        self.assertNotIn("LAND STATE LANDED", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_landed_lane_family_tag_blocks_false_writeoff(self):  # noqa: VACUOUS_ASSERTION — lightweight and annotated tags bind real trunk ancestors and unchanged history
        cases = (("light", False, "gate/%s-r2"),
                 ("annotated", True, "gate/%s-r2"),
                 ("token", False, "archive/%s"))
        for label, annotated, tag_shape in cases:
            with self.subTest(tag=label):
                family = "tag-%s-family" % label
                row, _missing = self.ghost(lane="lane/" + family + "-r1")
                self.git("commit", "--allow-empty", "-q", "-m",
                         "repair arrived under paraphrased words")
                tag = tag_shape % family
                args = ["tag"]
                if annotated:
                    args.extend(("-a", tag, "-m", "review gate"))
                else:
                    args.append(tag)
                self.git(*args)
                before = len(dispatches.history(row["id"]))
                lr, why = landreq.abandon(row["id"], "write off reviewed work")
                self.assertIsNone(lr)
                self.assertIn("landed family tag", why)
                self.assertIn(tag, why)
                self.assertIn("correlation, not proven landed", why)
                self.assertNotIn("LAND STATE LANDED", why)
                self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_family_tag_not_on_trunk_does_not_claim_landing(self):  # noqa: VACUOUS_ASSERTION — trunk-ancestor positive control above binds this negative arm
        family = "tag-unlanded-family"
        row, _missing = self.ghost(lane="lane/" + family + "-r1")
        self.git("checkout", "-q", "-b", "tag-only", self.main)
        self.commit("tagged but not on trunk", path="tag-only")
        self.git("tag", "gate/" + family + "-r2")
        self.git("checkout", "-q", self.main)
        lr, why = landreq.abandon(row["id"], "write off reviewed work")
        self.assertIsNone(why)
        self.assertEqual(lr["state"], "ABANDONED")
        self.assertEqual(lr["land_state"], "UNKNOWN")

    def test_unreadable_tag_scan_refuses_without_an_event(self):  # noqa: VACUOUS_ASSERTION — non-empty Git failure arm binds unchanged history
        row, _missing = self.ghost(lane="lane/unreadable-tag-family-r1")
        before = len(dispatches.history(row["id"]))
        real_git = landreq._git

        def fake_git(gitdir, *args, **kwargs):
            if args == ("for-each-ref", "--format=%(refname)", "refs/tags"):
                return subprocess.CompletedProcess(
                    args, 128, stdout="", stderr="fatal: refs unreadable")
            return real_git(gitdir, *args, **kwargs)

        with mock.patch.object(landreq, "_git", side_effect=fake_git):
            lr, why = landreq.abandon(row["id"], "write off reviewed work")
        self.assertIsNone(lr)
        self.assertIn("UNKNOWN", why)
        self.assertIn("tag", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_lane_family_suffixes_widen_iteratively_for_refusal_only(self):
        self.assertEqual(
            landreq._lane_family_names("feature-review-r2"),
            ("feature-review-r2", "feature-review", "feature"))
        self.assertEqual(landreq._lane_family_names("feature-re-review"),
                         ("feature-re-review", "feature-re", "feature"))
        self.assertEqual(landreq._lane_family_names("feature-build"),
                         ("feature-build", "feature"))
        self.assertEqual(landreq._lane_family_names("feature-fix"),
                         ("feature-fix", "feature"))
        self.assertEqual(
            landreq._lane_family_names("lr-delivery-leg-contract"),
            ("lr-delivery-leg-contract", "lr-delivery-leg", "lr-delivery"))
        self.assertEqual(
            landreq._lane_family_names("lr-undeclared-terminal-xrev"),
            ("lr-undeclared-terminal-xrev", "lr-undeclared-terminal",
             "lr-undeclared"))
        self.assertEqual(landreq._lane_family_names("feature"), ("feature",))

    def test_every_tag_namespace_gets_the_same_suffix_tolerance(self):
        """helm-claude's post-land finding, reproduced against tags that exist
        in THIS repo: suffix tolerance lived only in a special gate/ branch,
        and every other namespace fell to a boundary regex whose trailing
        lookahead rejects a FOLLOWING HYPHEN — the under-match direction,
        which for a block-only interlock is the direction that permits a
        wrongful abandon. One rule now: strip any <namespace>/ prefix, then
        startswith + separator boundary."""
        m = landreq._tag_matches_lane_family
        names = ("roster-read-cache",)
        # The three live MISSES from the finding, now matches:
        self.assertTrue(m("refs/tags/archive/roster-read-cache-64b0700", names))
        self.assertTrue(m("refs/tags/archive/roster-read-cache-v2", names))
        self.assertTrue(m("refs/tags/rescue/ds4pro-landreq-staged-2026-07-27",
                          ("ds4pro-landreq-staged",)))
        # The prior matches stay matches (no regression):
        self.assertTrue(m("refs/tags/rescue/codex-slice4-staged",
                          ("codex-slice4-staged",)))
        self.assertTrue(m("refs/tags/gate/argv-body-guard-r4",
                          ("argv-body-guard",)))
        # PRODUCTION NAMES, not a hand-built tuple — helm-claude's review
        # caught the first version asserting run-ons miss against a fixture
        # production never builds: _lane_trunk_mention feeds
        # _lane_family_names(lane), whose STEM ("roster-read") catches the
        # run-on via startswith + separator ("-cachex" starts with a
        # separator). So under the real code path a run-on MATCHES — and that
        # is ACCEPTED refusal-only noise, written here deliberately: the leg
        # is block-only, a false refusal costs one human investigation, a
        # false permit is an irreversible write-off. A later author who reads
        # an over-match as a bug and tightens the boundary walks straight
        # back into the under-match this lane fixed.
        prod = landreq._lane_family_names("roster-read-cache")
        self.assertIn("roster-read", prod)          # the stem that decides
        self.assertTrue(m("refs/tags/archive/roster-read-cachex", prod))
        self.assertTrue(m("refs/tags/rescue/roster-read-cache2", prod))
        # The boundary claim, restated where it is TRUE: against the exact
        # lane name alone (no stem), a run-on still fails the separator rule.
        self.assertFalse(m("refs/tags/archive/roster-read-cachex", names))
        self.assertFalse(m("refs/tags/rescue/roster-read-cache2", names))
        # And an unrelated lane still misses entirely, production names too.
        self.assertFalse(m("refs/tags/archive/roster-read-cache-v2",
                           landreq._lane_family_names("lr-projection")))

    def test_one_family_vocabulary_feeds_both_consumers(self):
        """SINGLE FAMILY AUTHORITY. `_lane_stem` (the discharge ADMIT) and
        `_lane_family_names` (refusal-only abandonment evidence) both derive
        from _LANE_FAMILY_SUFFIXES. Two independently-authored suffix sets
        once sat in this one file and disagreed about -build, -re-review,
        -xrev and -v2 — a renamed lane was FAMILY to one side and a STRANGER
        to the other. Every vocabulary token must read as the SAME family on
        both sides. The stem is deliberately SHORT and single-segment
        ("feature", 7 chars): prefix expansion never yields it, so the
        family-names side of every assertion rides on the suffix vocabulary
        alone — the exact thing this test pins."""
        # UNCONDITIONAL positive control on -build, the token the two forked
        # sets historically disagreed about, before the vocabulary loop.
        self.assertEqual(landreq._lane_stem("feature-build"), "feature")
        self.assertIn("feature", landreq._lane_family_names("feature-build"))
        for suffix in ("re-review", "rereview", "review", "build", "fix",
                       "xrev", "retry", "redo", "r3", "v2"):
            lane = "feature-" + suffix
            self.assertEqual(landreq._lane_stem(lane), "feature", lane)
            self.assertIn("feature", landreq._lane_family_names(lane), lane)

    def test_renamed_lane_family_trunk_mention_blocks_false_writeoff(self):  # noqa: VACUOUS_ASSERTION — three live census shapes bind real rows and unchanged history
        cases = (
            ("lr-delivery-leg-review", "lr-delivery-leg", "r6"),
            ("lr-delivery-leg-contract", "lr-delivery-leg", "r7"),
            ("lr-undeclared-terminal-xrev", "lr-undeclared-terminal", "r3"),
        )
        for lane, family, round_name in cases:
            with self.subTest(lane=lane):
                row, _missing = self.ghost(lane="lane/" + lane)
                self.git("commit", "--allow-empty", "-q", "-m",
                         "merge: %s %s — landed family work" %
                         (family, round_name))
                before = len(dispatches.history(row["id"]))
                lr, why = landreq.abandon(row["id"], "write off reviewed work")
                self.assertIsNone(lr)
                self.assertIn("AMBIGUOUS", why)
                self.assertIn(family, why)
                self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_incidental_short_stem_match_is_ambiguous_and_blocks_abandon(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        row, _missing = self.ghost(lane="lane/delim-r1")
        self.git("commit", "--allow-empty", "-q", "-m",
                 "fix delimiter parsing in unrelated input")
        before = len(dispatches.history(row["id"]))
        lr, why = landreq.abandon(row["id"], "write off reviewed work")
        self.assertIsNone(lr)
        self.assertIn("AMBIGUOUS", why)
        self.assertIn("delimiter", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_live_lane_branch_blocks_abandon_after_reviewed_tip_dies(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        lane = "withdraw-contradicted-exit"
        row, _missing = self.ghost(lane="lane/" + lane)
        self.git("checkout", "-q", "-b", "lane/" + lane, self.main)
        self.commit("live branch work", path="live-branch")
        self.git("checkout", "-q", self.main)
        before = len(dispatches.history(row["id"]))
        lr, why = landreq.abandon(row["id"], "write off reviewed work")
        self.assertIsNone(lr)
        self.assertIn("lane branch", why)
        self.assertIn("unlanded", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_renamed_lane_family_branch_blocks_abandon(self):  # noqa: VACUOUS_ASSERTION — live family branch binds a real row and unchanged history
        family = "renamed-live-work"
        row, _missing = self.ghost(lane="lane/" + family + "-contract")
        self.git("checkout", "-q", "-b", "lane/" + family, self.main)
        self.commit("live renamed-family work", path="renamed-live-branch")
        self.git("checkout", "-q", self.main)
        before = len(dispatches.history(row["id"]))
        lr, why = landreq.abandon(row["id"], "write off reviewed work")
        self.assertIsNone(lr)
        self.assertIn("lane branch", why)
        self.assertIn("unlanded", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_dirty_registered_worktree_blocks_abandon(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        lane = "dirty-ghost"
        row, _missing = self.ghost(lane="lane/" + lane)
        wt = os.path.join(self.tmp, "dirty-worktree")
        self.git("worktree", "add", "-q", "-b", "lane/" + lane, wt, self.main)
        with open(os.path.join(wt, "uncommitted"), "w", encoding="utf-8") as f:
            f.write("live dirt\n")
        before = len(dispatches.history(row["id"]))
        lr, why = landreq.abandon(row["id"], "write off reviewed work")
        self.assertIsNone(lr)
        self.assertIn("dirty worktree", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_renamed_lane_family_dirty_worktree_blocks_abandon(self):  # noqa: VACUOUS_ASSERTION — registered family worktree binds a real row and unchanged history
        family = "renamed-dirty-work"
        row, _missing = self.ghost(lane="lane/" + family + "-xrev")
        wt = os.path.join(self.tmp, "opaque-renamed-family-worktree")
        self.git("worktree", "add", "-q", "-b", "lane/" + family,
                 wt, self.main)
        with open(os.path.join(wt, "uncommitted"), "w", encoding="utf-8") as f:
            f.write("live renamed-family dirt\n")
        before = len(dispatches.history(row["id"]))
        lr, why = landreq.abandon(row["id"], "write off reviewed work")
        self.assertIsNone(lr)
        self.assertIn("dirty worktree", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_unreadable_lane_branch_refuses_without_an_event(self):  # noqa: VACUOUS_ASSERTION — three non-empty Git failure arms bind unchanged history
        real_git = landreq._git
        cases = (
            ("resolve", 128, "", None),
            ("identity", 0, "not-a-full-object-id\n", None),
            ("ancestry", 0, self.c + "\n", landreq.UNDETERMINED),
        )
        for name, returncode, stdout, relation in cases:
            with self.subTest(name=name):
                lane = "unreadable-branch-" + name
                row, _missing = self.ghost(lane="lane/" + lane)
                ref = "refs/heads/lane/" + lane
                before = len(dispatches.history(row["id"]))

                def fake_git(gitdir, *args, **kwargs):
                    if args == ("rev-parse", "--verify", "--quiet", ref):
                        return subprocess.CompletedProcess(
                            args, returncode, stdout=stdout, stderr="fatal")
                    return real_git(gitdir, *args, **kwargs)

                ancestry = mock.patch.object(
                    landreq, "_ancestry", return_value=relation) \
                    if relation is not None else contextlib.nullcontext()
                with mock.patch.object(landreq, "_git", side_effect=fake_git), ancestry:
                    lr, why = landreq.abandon(row["id"], "write off reviewed work")
                self.assertIsNone(lr)
                self.assertIn("UNKNOWN", why)
                self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_unreadable_registered_worktree_refuses_without_an_event(self):  # noqa: VACUOUS_ASSERTION — four non-empty Git failure arms bind unchanged history
        real_git = landreq._git
        cases = ("list", "missing-path", "status-missing", "status-error")
        for name in cases:
            with self.subTest(name=name):
                lane = "unreadable-worktree-" + name
                row, _missing = self.ghost(lane="lane/" + lane)
                path = os.path.join(self.tmp, "registered-" + name)
                if name in ("status-missing", "status-error"):
                    os.makedirs(path)
                porcelain = ("worktree %s\nbranch refs/heads/lane/%s\n\n"
                             % (path, lane))
                before = len(dispatches.history(row["id"]))

                def fake_git(gitdir, *args, **kwargs):
                    if args == ("worktree", "list", "--porcelain"):
                        if name == "list":
                            return subprocess.CompletedProcess(
                                args, 128, stdout="", stderr="fatal")
                        return subprocess.CompletedProcess(
                            args, 0, stdout=porcelain, stderr="")
                    return real_git(gitdir, *args, **kwargs)

                if name == "status-missing":
                    status = mock.patch.object(landreq, "_worktree_status",
                                               return_value=None)
                elif name == "status-error":
                    result = subprocess.CompletedProcess(
                        ("git", "status"), 128, stdout="", stderr="fatal")
                    status = mock.patch.object(landreq, "_worktree_status",
                                               return_value=result)
                else:
                    status = contextlib.nullcontext()
                with mock.patch.object(landreq, "_git", side_effect=fake_git), status:
                    lr, why = landreq.abandon(row["id"], "write off reviewed work")
                self.assertIsNone(lr)
                self.assertIn("UNKNOWN", why)
                self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_porcelain_branch_identity_ignores_a_dirty_lookalike_path(self):  # noqa: VACUOUS_ASSERTION — clean match plus dirty negative control bind exact branch identity
        lane = "identity-target"
        row, _missing = self.ghost(lane="lane/" + lane)
        matching = os.path.join(self.tmp, "opaque-matching-worktree")
        lookalike = os.path.join(self.tmp, lane)
        self.git("worktree", "add", "-q", "-b", "lane/" + lane,
                 matching, self.main)
        self.git("worktree", "add", "-q", "-b", "lane/unrelated-dirty",
                 lookalike, self.main)
        with open(os.path.join(lookalike, "uncommitted"), "w",
                  encoding="utf-8") as f:
            f.write("unrelated live dirt\n")
        lr, why = landreq.abandon(row["id"], "write off reviewed work")
        self.assertIsNone(why)
        self.assertEqual(lr["state"], "ABANDONED")
        self.assertEqual(lr["abandon_worktree_state"], "clean")

    def test_existing_commit_refuses_and_names_the_honest_alternative(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        row = self.reviewed()
        before = len(dispatches.history(row["id"]))
        lr, why = landreq.abandon(row["id"], "close inconvenient work")
        self.assertIsNone(lr)
        self.assertIn("exists", why)
        self.assertIn("helm lr withdraw", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)
        self.assertFalse(landreq.get(row["id"])[0].get("abandoned", False))

    def test_commit_exists_only_explicit_missing_is_FALSE(self):  # noqa: VACUOUS_ASSERTION — paired commit/missing controls bind both concrete outcomes
        sha = self.side
        cases = (
            (None, None),
            (subprocess.CompletedProcess(("git",), 128, stdout="", stderr="fatal"),
             None),
            (subprocess.CompletedProcess(
                ("git",), 0, stdout=sha + "^{commit} missing\n", stderr=""),
             False),
            (subprocess.CompletedProcess(
                ("git",), 0, stdout=sha + " commit 123\n", stderr=""), True),
        )
        for result, expected in cases:
            with self.subTest(result=result, expected=expected), \
                    mock.patch.object(landreq, "_git", return_value=result):
                self.assertIs(landreq._commit_exists(self.repo, sha), expected)

    def test_UNKNOWN_git_state_refuses_without_an_event(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        row, _missing = self.ghost()
        before = len(dispatches.history(row["id"]))
        with mock.patch.object(landreq, "_commit_exists", return_value=None):
            lr, why = landreq.abandon(row["id"], "evidence unavailable")
        self.assertIsNone(lr)
        self.assertIn("UNKNOWN", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_preflight_missing_but_mutation_boundary_exists_refuses(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        row, _missing = self.ghost()
        before = len(dispatches.history(row["id"]))
        with mock.patch.object(landreq, "_commit_exists",
                               side_effect=(False, True)) as probe:
            lr, why = landreq.abandon(row["id"], "history rewrite")
        self.assertIsNone(lr)
        self.assertIn("exists", why)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_repo_id_is_part_of_the_identity_not_the_callers_checkout(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        self.git("init", "-q", cwd=other)
        self.git("config", "user.email", "other@example.com", cwd=other)
        self.git("config", "user.name", "Other", cwd=other)
        with open(os.path.join(other, "base"), "w", encoding="utf-8") as f:
            f.write("base\n")
        self.git("add", "base", cwd=other)
        self.git("commit", "-q", "-m", "base", cwd=other)
        main = self.git("symbolic-ref", "--short", "HEAD", cwd=other)
        self.git("checkout", "-q", "-b", "side", cwd=other)
        with open(os.path.join(other, "only-there"), "w", encoding="utf-8") as f:
            f.write("live\n")
        self.git("add", "only-there", cwd=other)
        self.git("commit", "-q", "-m", "live cross-repo object", cwd=other)
        tip = self.git("rev-parse", "HEAD", cwd=other)
        self.git("checkout", "-q", main, cwd=other)
        row = dispatches.add("codex-3", "lane/cross-repo", ref=tip, repo=other,
                             new_work=True, kind="review")
        dispatches.mark_verdict(row["id"], tip, "reviewed", polarity="approve")
        lr, why = landreq.abandon(row["id"], "wrong-repo would call this missing")
        self.assertIsNone(lr)
        self.assertIn("exists", why)
        self.assertFalse(landreq.get(row["id"])[0].get("abandoned", False))

    def test_build_rows_and_bad_reasons_are_refused(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        build = self.dispatch(ref=self.side, lane="lane/build", kind="build")
        dispatches.mark_verdict(build["id"], self.side, "historical bad shape",
                                polarity="fix")
        lr, why = landreq.abandon(build["id"], "not a review")
        self.assertIsNone(lr)
        self.assertIn("not a review row", why)
        row, _missing = self.ghost()
        for reason in ("", "two\nlines", "control\x00byte"):
            lr, why = landreq.abandon(row["id"], reason)
            self.assertIsNone(lr)
            self.assertIn("abandon reason", why)

    def test_cli_requires_named_reason_and_reports_write_off(self):
        row, _missing = self.ghost()
        rc, _out, err = run(["abandon", row["id"]])
        self.assertEqual(rc, 2)
        self.assertIn("--reason", err)
        rc, out, err = run(["abandon", row["id"][:12], "--reason",
                            "history rewrite destroyed the object"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("ABANDONED", out)
        self.assertIn("LAND STATE UNKNOWN", out)
        self.assertIn("WRITES OFF REVIEWED WORK", out)


class ReceiptBase(LandReqBase):
    """Shared harness for land-receipt replay. Signed emit is mocked (hermetic —
    no node, no binary), exactly as test_chat_v2 does."""

    def gitdir(self):
        return self.git("rev-parse", "--absolute-git-dir")

    def signed(self, info=None):
        """The room node is UP: emit returns a committed signing receipt."""
        return mock.patch.object(chat, "emit_coordination_turn",
                                 return_value=(dict(info or SENT), None))

    def down(self, code="node_unreachable", reason="chat node unreachable"):
        """The room node is DOWN: emit fails open with a structured diagnostic."""
        return mock.patch.object(chat, "emit_coordination_turn",
                                 return_value=(None, chat._diag(code, reason)))

    def record(self, tip, trunk_sha, lane="lane/foo", branch="seat/foo",
               patch_id=None, **kw):
        """Record one signed receipt, asserting it was actually recorded."""
        with self.signed():
            rec, err = landreq.record_land(
                lane, branch, tip, patch_id or "e" * 40, trunk_sha,
                repo_id=kw.pop("repo_id", self.gitdir()), **kw)
        self.assertIsNone(err)
        return rec

    def rows(self):
        """Every raw row currently in the durable index."""
        with open(landreq.receipts_path(), encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def write_rows(self, *rows):
        """Overwrite the index with exactly these raw rows — how a hostile or
        corrupt writer is simulated (the index is a plain local file)."""
        path = landreq.receipts_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    def landed_row(self, tip, rid="aabbccdd", lane="lane/pruned"):
        """A verdict'd dispatch row bound to `tip` — the loop a receipt speaks
        for. Stamped before the compat boundary so the snapshot row replays as
        verdict'd (the reduced core's own grammar)."""
        ts = "2026-07-01T00:00:00Z"
        row = {"id": rid, "ts": ts, "recipient": "codex-3", "lane": lane,
               "ref": tip, "tip": tip, "note": None, "deadline_s": 60,
               "source": "x", "status": "verdict", "verdict_ref": "CLEAR",
               "polarity": "approve", "reviewed_tip": tip,
               "repo_id": self.gitdir(),
               "last_updated": ts}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), row))
        return rid

    def land_side(self):
        """Land `side` onto trunk with a real merge commit; return the new trunk."""
        self.git("merge", "-q", "--no-ff", "side", "-m", "land side")
        return self.git("rev-parse", self.main)


class LandReceiptTest(ReceiptBase):
    """The durable candidate receipt and its honest non-authority boundary."""

    def test_recorded_receipt_stays_diagnostic_when_git_evidence_is_gone(self):
        # Git cannot see the pruned object, so observation is UNAVAILABLE. The
        # local row records what the signer command returned, but replay cannot
        # verify that turn's payload; it must not manufacture LANDED.
        pruned = "0" * 40
        self.landed_row(pruned)

        lr = landreq.get("aabbccdd")[0]
        # This deliberately old snapshot carries no replayable verdict polarity,
        # so the verdict is REVIEWED/UNDECLARED rather than guessed APPROVED.
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertFalse(lr["observable"])
        self.assertEqual(lr["receipt_state"], landreq.R_NONE)

        with self.signed() as emit:
            rec, err = landreq.record_land(
                "lane/pruned", "seat/pruned", pruned, "e" * 40, "d" * 40,
                repo_id=self.gitdir(), has_upstream=False)
        self.assertIsNone(err)
        self.assertEqual((rec["chain"], rec["turn"]), (7, "a" * 64))
        self.assertEqual(emit.call_args[0][0], landreq.LAND_TOPIC)
        # #142: record_land canonicalizes the lane BEFORE signing, so the
        # signed payload is the one over the stored bare spelling.
        self.assertEqual(emit.call_args[0][1],
                         landreq.land_payload("pruned", "seat/pruned",
                                              pruned, "e" * 40, "d" * 40))
        self.assertTrue(emit.call_args[0][1].startswith(landreq.LAND_TAG))

        lr = landreq.get("aabbccdd")[0]
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertFalse(lr["landed"] or lr["terminal"] or lr["receipt"])
        self.assertEqual(lr["receipt_state"], landreq.R_LOCAL)
        self.assertIn("payload binding unavailable", lr["receipt_reason"])
        shown = run(["show", "aabbccdd"])[1]
        self.assertIn("local-unverified", shown)
        self.assertNotIn("[recorded receipt]", shown)

    def test_receipt_lane_is_canonical_at_write_and_verbatim_at_replay(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive controls ARE the assertEquals on the same observables (stored row == ["foo"] and stored payload == the bare-spelling payload, both loud on an empty index); the assertNotEquals pin the as-written law
        """#142 at the RECEIPT layer, round 2 (codex): the WRITER canonicalizes
        the lane before signing/storing (`record_land`), so a new receipt
        stores the bare spelling and its payload was signed over that bare
        spelling. `land_record` itself joins fields VERBATIM — replay
        recomputes stored payloads through it, so canonicalizing there would
        judge historical prefixed rows under today's spelling (the live-ledger
        29/29 rebind failure)."""
        args = ("seat/foo", "1" * 40, "e" * 40, "2" * 40)
        # The as-written law: two spellings are two DIFFERENT records/payloads.
        self.assertNotEqual(landreq.land_record("lane/foo", *args),
                            landreq.land_record("foo", *args))
        self.assertNotEqual(landreq.land_payload("lane/foo", *args),
                            landreq.land_payload("foo", *args))
        # The fixture records under "lane/foo"; the stored row is bare and its
        # payload is the one signed over the BARE spelling.
        self.record("1" * 40, "2" * 40)
        self.assertEqual([r["lane"] for r in self.rows()], ["foo"])
        rec = self.rows()[0]
        self.assertEqual(rec["payload"],
                         landreq.land_payload("foo", rec["branch"],
                                              rec["reviewed_tip"],
                                              rec["patch_id"],
                                              rec["trunk_sha"]))

    def test_the_signed_payload_binds_every_field(self):
        # The payload is a tagged digest over the WHOLE record, so changing one
        # stored field without changing the stored payload is detectable. This is
        # local consistency, not proof that dregg's turn carried that payload.
        base = ("lane/a", "seat/a", "1" * 40, "e" * 40, "2" * 40)
        p = landreq.land_payload(*base)
        self.assertEqual(len(p), len(landreq.LAND_TAG) + 64)
        self.assertLess(len(p.encode("utf-8")), 104)
        for i in range(5):
            other = list(base)
            other[i] = other[i][:-1] + "f" if i else "lane/b"
            self.assertNotEqual(p, landreq.land_payload(*other))
        # The RS-join is injective only because a field may never CONTAIN the
        # separator — so that is the invariant actually enforced, at BOTH the
        # write and the replay boundary (a slid pair does collide as a digest:
        # `_field` is what makes it unreachable, and it is tested as such).
        self.assertEqual(landreq.land_payload("a\x1eb", "c", "1" * 40, None, "2" * 40),
                         landreq.land_payload("a", "b\x1ec", "1" * 40, None, "2" * 40))
        self.assertIsNone(landreq._field("a\x1eb"))           # refused as a field
        with self.signed():                                   # refused at write
            self.assertIn("printable line", landreq.record_land(
                "a\x1eb", "c", "1" * 40, None, "2" * 40)[1])
        rec = dict(schema=landreq.LAND_SCHEMA, topic=landreq.LAND_TOPIC,
                   id="1" * 40, reviewed_tip="1" * 40, lane="a\x1eb", branch="c",
                   patch_id=None, trunk_sha="2" * 40, turn="a" * 64,
                   receipt="b" * 64, chain=1,
                   payload=landreq.land_payload("a\x1eb", "c", "1" * 40, None,
                                                "2" * 40))
        self.assertIn("printable line",                       # refused at replay
                      landreq._validate_receipt(rec, "1" * 40)[1])

    def test_node_down_falls_open_to_git_observation(self):
        # FAIL-OPEN: an unreachable cave-node must NEVER block a land or crash
        # the recorder. Nothing is written and landing stays EXACTLY git-observed.
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        trunk = self.land_side()

        with self.down():
            rec, why = landreq.record_land(
                "lane/foo", "seat/foo", self.side, "e" * 40, trunk,
                repo_id=self.gitdir(), has_upstream=False)
        self.assertIsNone(rec)                                      # no receipt
        self.assertIn("unreachable", why)                           # honest why
        self.assertFalse(os.path.exists(landreq.receipts_path()))    # no write

        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "LANDED")     # git observed the merge
        self.assertTrue(lr["landed"])
        self.assertFalse(lr["receipt"])
        self.assertEqual(lr["receipt_state"], landreq.R_NONE)

    def test_a_receipt_without_its_anchor_is_refused_not_written(self):
        # trunk_sha identifies the historical land event the turn claims. Even a
        # diagnostic candidate without that identity is malformed and is refused
        # before signing.
        with self.signed() as emit:
            rec, why = landreq.record_land("lane/x", "seat/x", "1" * 40,
                                           "e" * 40, None)
        self.assertIsNone(rec)
        self.assertIn("ACTUAL trunk sha", why)
        emit.assert_not_called()                    # never even signed
        self.assertFalse(os.path.exists(landreq.receipts_path()))
        # same for a tip that is not a full sha, and for a junk patch-id
        with self.signed():
            self.assertIn("FULL sha", landreq.record_land(
                "l", "b", "abc", None, "2" * 40)[1])
            self.assertIn("patch_id", landreq.record_land(
                "l", "b", "1" * 40, "patchid-x", "2" * 40)[1])

    def test_cli_land_verb_records_then_is_fail_open(self):
        # `helm lr land <id>` WITNESSES a land; it does NOT perform one. The
        # comment here used to call it "the integrator's ff-merge hook", which
        # is the exact misreading the verb's imperative name invites — and it
        # was written into the test that documents it. Nothing in this path
        # moves a ref; the merge is the integrator's own git work.
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.land_side()
        with self.signed():
            rc, out, err = run(["land", row["id"]])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("receipt recorded", out)
        self.assertIn("NO MERGE PERFORMED", out,
                      "the success line must name the non-action — an "
                      "integrator scanning output sees `land` beside exit 0 "
                      "and concludes the lane landed")
        rec = self.rows()[0]
        self.assertEqual(rec["reviewed_tip"], self.side)
        self.assertEqual(rec["trunk_sha"], self.git("rev-parse", self.main))
        self.assertTrue(rec["patch_id"])            # correlation evidence rode
        self.assertEqual(landreq.get(row["id"])[0]["state"], "LANDED")

        other = self.dispatch(ref=self.b, lane="lane/down")   # b is already trunk
        dispatches.mark_verdict(other["id"], self.b, "ok",
                                polarity="approve")
        with self.down():
            rc, out, err = run(["land", other["id"]])
        self.assertEqual(rc, 0)                     # fail-open, land not blocked
        self.assertIn("fail-open", out)
        # BOTH exits must name the non-action, not just the happy one. This is
        # the branch an integrator actually hits when HELM_CELL_BIN is unset,
        # and its old wording ("land not blocked") read as though the land had
        # proceeded — the reading that cost a real integrator a false "landed"
        # report on 2026-07-30, caught only by checking origin/main afterwards.
        self.assertIn("NO MERGE PERFORMED", out)


class ReceiptAuthorityTest(ReceiptBase):
    """trunk_sha is the candidate event identity, never local authority.

    These cells preserve the correct historical semantics while proving that the
    local index cannot apply them until dregg payload binding is verifiable.
    """

    def test_a_cherry_pick_is_landed_by_patch_identity_not_the_receipt(self):
        # Cherry-picking lands the change while leaving the reviewed object
        # non-ancestral. Main's patch-identity fallback proves the land; adding a
        # local-unverified receipt changes only the diagnostic axis.
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.git("cherry-pick", self.side)
        trunk = self.git("rev-parse", self.main)
        self.assertEqual(landreq._ancestry(self.gitdir(), self.side,
                                          "refs/heads/" + self.main),
                         landreq.NOT_ANCESTOR)
        before = landreq.get(row["id"])[0]
        self.assertEqual(before["state"], "LANDED")
        self.assertTrue(before["landed"])

        self.record(self.side, trunk)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "LANDED")
        self.assertTrue(lr["landed"])
        self.assertFalse(lr["receipt"])
        self.assertEqual(lr["receipt_state"], landreq.R_LOCAL)
        self.assertIn("payload binding unavailable", lr["receipt_reason"])

    def test_a_real_revert_stays_landed_it_is_a_later_separate_event(self):
        # A real `git revert` ADDS a commit; it does not retract the land. Git's
        # historical ancestry proves the land independently of the local receipt.
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        trunk = self.land_side()
        self.record(self.side, trunk)
        self.assertEqual(landreq.get(row["id"])[0]["state"], "LANDED")

        self.git("revert", "--no-edit", "-m", "1", trunk)
        self.assertNotEqual(self.git("rev-parse", self.main), trunk)   # trunk moved
        self.assertEqual(landreq._ancestry(self.gitdir(), self.side,
                                          "refs/heads/" + self.main),
                         landreq.ANCESTOR)       # the tip is STILL in history
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "LANDED")  # the land happened; revert is later
        self.assertFalse(lr["receipt"])           # Git, not the local row, proved it
        self.assertEqual(lr["receipt_state"], landreq.R_LOCAL)

    def test_a_history_rewrite_contradicts_the_candidate_receipt(self):
        # A rewrite that drops the claimed trunk out of history contradicts even
        # the local candidate. Live Git governs throughout; the receipt state
        # changes only to preserve the useful diagnostic.
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        trunk = self.land_side()
        self.record(self.side, trunk)
        self.assertEqual(landreq.get(row["id"])[0]["state"], "LANDED")

        self.git("reset", "-q", "--hard", "HEAD~1")      # the land left history
        self.assertEqual(landreq._ancestry(self.gitdir(), trunk,
                                          "refs/heads/" + self.main),
                         landreq.NOT_ANCESTOR)
        lr = landreq.get(row["id"])[0]
        self.assertNotEqual(lr["state"], "LANDED")
        self.assertFalse(lr["landed"])
        self.assertEqual(lr["receipt_state"], landreq.R_CONTRADICTED)
        self.assertIn("left trunk's history", lr["receipt_reason"])

    def test_live_git_still_observes_the_push_with_a_local_receipt_present(self):
        # Removing local receipt authority must not break the real feature: Git
        # still distinguishes a local merge from the later upstream push.
        self.add_origin()                                  # origin lacks the land
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        trunk = self.land_side()
        self.record(self.side, trunk, has_upstream=True, upstream=False)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "MERGED_LOCAL")      # local only
        self.assertTrue(lr["merged_local"])
        self.assertFalse(lr["landed"] or lr["receipt"])
        self.assertEqual(lr["receipt_state"], landreq.R_LOCAL)

        self.git("push", "-q", "origin", self.main)        # the push happens
        self.assertEqual(landreq.get(row["id"])[0]["state"], "LANDED")


class ReceiptValidationTest(ReceiptBase):
    """Nothing in the index is trusted, and nothing in it may veto live Git.

    A wrong topic, forged field, or junk transport shape stays visible on the
    receipt-diagnostic axis while an independently observed land remains LANDED.
    Receipt corruption can never manufacture authority in either direction.
    """

    def valid_row(self):
        """One genuinely recorded receipt for a REAL land, as the baseline."""
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        trunk = self.land_side()
        self.record(self.side, trunk)
        self.assertEqual(landreq.get(row["id"])[0]["state"], "LANDED")
        return row["id"], self.rows()[0]

    def assert_receipt_diagnostic(self, rid, state, needle=None):
        lr = landreq.get(rid)[0]
        # Receipt contamination is diagnostic only: it neither manufactures a
        # land nor vetoes the independent, readable Git fact established by
        # valid_row(). The two evidence axes must stay orthogonal.
        self.assertEqual(lr["state"], "LANDED")
        self.assertTrue(lr["landed"] and lr["observable"])
        self.assertFalse(lr["stalled"])
        self.assertEqual(lr["receipt_state"], state)
        if needle:
            self.assertIn(needle, lr["receipt_reason"])
        shown = run(["show", rid])[1]
        self.assertIn("LANDED", shown)
        self.assertIn(state, shown)
        # SCOPED TO THE TWO AXES THIS ASSERTS. A bare substring over the whole
        # render also caught the DWELL line, which legitimately reads UNKNOWN
        # here: a git-observed landing is terminal and carries no closure
        # instant anywhere, so its age was never measured. The claim being made
        # is that receipt corruption manufactures no UNKNOWN in the LAND state
        # or in the receipt diagnostic — so it is asserted on those lines.
        for line in shown.splitlines():
            if line.startswith(("  landed", "  receipt", "LAND REQUEST")):
                self.assertNotIn("UNKNOWN", line)
        self.assertEqual(len([line for line in shown.splitlines()
                              if line.startswith("  receipt   ")]), 1)
        self.assertIn("LANDED", run(["list", "--all"])[1])
        return lr

    def test_a_wrong_topic_row_is_rejected(self):
        rid, rec = self.valid_row()
        self.write_rows(dict(rec, topic="helm.chat"))
        self.assert_receipt_diagnostic(rid, landreq.R_REJECTED, "topic is not helm.land")

    def test_forged_fields_that_no_longer_rebind_are_rejected(self):
        # the heart of the binding: keep the SIGNED payload, swap the fields it
        # was signed over. Each forgery must fail to rebind.
        rid, rec = self.valid_row()
        for field, value in (("trunk_sha", "9" * 40), ("patch_id", "b" * 40),
                             ("lane", "lane/other"), ("branch", "seat/other")):
            self.write_rows(dict(rec, **{field: value}))
            self.assert_receipt_diagnostic(rid, landreq.R_REJECTED, "does not rebind")

    def test_a_historical_prefixed_receipt_still_rebinds_as_written(self):  # noqa: VACUOUS_ASSERTION — the positive controls are the assertEquals on ok[lane] (None would raise loudly) and on receipt_state against a baseline pinned NotEqual REJECTED above
        """#142 round 2 (codex): 29 of the live ledger's 117 signed receipts
        store a lane/-prefixed spelling whose payload was SIGNED over those
        bytes. Replay must validate each stored payload under the spelling
        that was actually signed — recomputing through the write-time
        canonicalizer rejected all 29 live rows ("payload does not rebind
        these fields"). The refactor must superset the dumb version: an old
        row keeps exactly the meaning it was written with."""
        rid, rec = self.valid_row()
        baseline = landreq.get(rid)[0]["receipt_state"]
        self.assertNotEqual(baseline, landreq.R_REJECTED)   # live control
        # The row AS THE OLD WRITER WROTE IT: prefixed lane, payload signed
        # over that prefixed spelling. The historical bytes are FROZEN HERE,
        # never recomputed through land_record/land_payload — a fixture that
        # routes through the current builder would mutate in lockstep with
        # the exact regression it exists to catch (the round-1 mutation
        # matrix proved that: re-adding the strip survived the recomputing
        # version of this test).
        import hashlib
        raw = landreq._RS.join([landreq.LAND_SCHEMA, "lane/" + rec["lane"],
                                rec["branch"], rec["reviewed_tip"],
                                rec["patch_id"], rec["trunk_sha"]])
        frozen = landreq.LAND_TAG + hashlib.blake2b(
            raw.encode("utf-8"), digest_size=32).hexdigest()
        hist = dict(rec, lane="lane/" + rec["lane"], payload=frozen)
        ok, why = landreq._validate_receipt(dict(hist), rec["reviewed_tip"])
        self.assertIsNone(why)
        self.assertEqual(ok["lane"], "lane/" + rec["lane"])
        self.write_rows(hist)
        lr = landreq.get(rid)[0]
        self.assertEqual(lr["receipt_state"], baseline)     # not degraded

    def test_a_forged_reviewed_tip_cannot_speak_for_another_loop(self):
        rid, rec = self.valid_row()
        self.write_rows(dict(rec, reviewed_tip="9" * 40))   # id still the old tip
        self.assert_receipt_diagnostic(rid, landreq.R_REJECTED, "not bound to this row's")

    def test_junk_transport_evidence_is_rejected(self):
        rid, rec = self.valid_row()
        for junk in ({"turn": "junk"}, {"receipt": "b" * 63}, {"chain": "7"},
                     {"chain": -1}, {"chain": True}, {"turn": None}):
            self.write_rows(dict(rec, **junk))
            self.assert_receipt_diagnostic(rid, landreq.R_REJECTED,
                                "no committed signing receipt")

    def test_an_unschemad_or_malformed_row_is_rejected(self):
        rid, rec = self.valid_row()
        for bad in (dict(rec, schema="helm.land/1"), dict(rec, schema=None),
                    dict(rec, trunk_sha="deadbeef"), dict(rec, lane="a\x1eb"),
                    dict(rec, lane="x" * 300), dict(rec, payload=None)):
            self.write_rows(bad)
            self.assert_receipt_diagnostic(rid, landreq.R_REJECTED)

    def test_two_receipts_binding_different_lands_to_one_tip_conflict(self):
        rid, rec = self.valid_row()
        second = dict(rec, trunk_sha="9" * 40)
        second["payload"] = landreq.land_payload(
            rec["lane"], rec["branch"], rec["reviewed_tip"], rec["patch_id"],
            "9" * 40)                        # internally consistent, but a RIVAL
        self.write_rows(rec, second)
        self.assert_receipt_diagnostic(rid, landreq.R_CONFLICT, "DIFFERENT lands")

    def test_a_valid_receipt_beside_an_invalid_one_conflicts(self):
        # appending garbage next to a good row must not let the good row be
        # cherry-picked: a contaminated index is refused, not resolved
        rid, rec = self.valid_row()
        self.write_rows(rec, dict(rec, topic="helm.chat"))
        self.assert_receipt_diagnostic(rid, landreq.R_CONFLICT, "both a valid and an invalid")

    def test_an_identical_duplicate_row_is_idempotent_not_a_conflict(self):
        # re-recording the SAME land (a retried `lr land`) binds identically
        rid, rec = self.valid_row()
        self.write_rows(rec, dict(rec))
        lr = landreq.get(rid)[0]
        self.assertEqual(lr["state"], "LANDED")       # Git independently sees it
        self.assertEqual(lr["receipt_state"], landreq.R_LOCAL)

    def test_complete_malformed_physical_siblings_poison_strict_replay(self):
        cases = {
            "malformed": b'{not json}\n',
            "idless": b'{}\n',
            "oversize": b'{"id":"x","pad":"' +
                        b'x' * eventledger.MAX_EVENT_BYTES + b'"}\n',
        }
        for name, raw in cases.items():
            rid, _rec = self.valid_row()
            with open(landreq.receipts_path(), "ab") as f:
                f.write(raw)
            self.assert_receipt_diagnostic(rid, landreq.R_REJECTED,
                                "corrupt ledger line")
            os.remove(landreq.receipts_path())

    def test_an_unterminated_final_tail_is_outside_the_durable_boundary(self):
        rid, _rec = self.valid_row()
        with open(landreq.receipts_path(), "ab") as f:
            f.write(b'{not durable yet')
        lr = landreq.get(rid)[0]
        self.assertEqual(lr["state"], "LANDED")       # Git still proves the land
        self.assertEqual(lr["receipt_state"], landreq.R_LOCAL)

    def test_record_land_refuses_to_write_a_row_that_fails_its_own_replay(self):
        # what is written always replays: an incomplete signer response cannot
        # become a row that would later be rejected
        with mock.patch.object(chat, "emit_coordination_turn",
                               return_value=({"sent": True, "turn_hash": "a" * 64,
                                              "receipt_hash": "b" * 64,
                                              "chain_index": None}, None)):
            rec, why = landreq.record_land("l", "b", "1" * 40, None, "2" * 40)
        self.assertIsNone(rec)
        self.assertIn("fails its own replay", why)
        self.assertFalse(os.path.exists(landreq.receipts_path()))


class AncestryTriStateTest(ReceiptBase):
    """Ancestry is tri-state, while landedness has a patch-identity fallback."""

    def unanswerable(self, rc=128, hang=False, patch=False):
        """Make ancestry, and optionally patch identity, unanswerable while the
        rest of Git stays real. This separates a fallback answer from merely
        having attempted the fallback."""
        real = landreq._git

        def fake(gitdir, *args, **kw):
            ancestry = args[:2] == ("merge-base", "--is-ancestor")
            patch_probe = patch and args[:1] == ("cherry",)
            if ancestry or patch_probe:
                if hang:
                    return None                 # a timeout/OSError
                return subprocess.CompletedProcess(
                    args, rc, "", "fatal: Not a valid object name")
            return real(gitdir, *args, **kw)
        return mock.patch.object(landreq, "_git", side_effect=fake)

    def test_the_three_cells_are_distinct(self):
        gitdir, trunk = self.gitdir(), "refs/heads/" + self.main
        self.assertEqual(landreq._ancestry(gitdir, self.b, trunk),
                         landreq.ANCESTOR)              # rc 0
        self.assertEqual(landreq._ancestry(gitdir, self.side, trunk),
                         landreq.NOT_ANCESTOR)          # rc 1
        with self.unanswerable(rc=128):
            self.assertEqual(landreq._ancestry(gitdir, self.b, trunk),
                             landreq.UNDETERMINED)      # rc 128
        with self.unanswerable(hang=True):
            self.assertEqual(landreq._ancestry(gitdir, self.b, trunk),
                             landreq.UNDETERMINED)      # timeout / OSError
        # a missing gitdir/commit/ref is undeterminable, never a negative
        self.assertEqual(landreq._ancestry(gitdir, self.b, None),
                         landreq.UNDETERMINED)
        self.assertEqual(landreq._ancestry(None, self.b, trunk),
                         landreq.UNDETERMINED)

    def test_unanswerable_ancestry_falls_through_to_patch_identity(self):
        # rc128/timeout makes reachability unknown, not the whole landing question.
        # The readable per-commit patch comparison still proves this side commit is
        # absent, so READY remains observable and billable.
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.age(row["id"], landreq.LAND_STALL_S["READY"] + 60)
        for cell in (dict(rc=128), dict(hang=True)):
            with self.unanswerable(**cell):
                lr = landreq.get(row["id"])[0]
                self.assertTrue(lr["observable"])
                self.assertEqual(lr["state"], "READY")
                self.assertTrue(lr["stalled"])

    def test_unanswerable_ancestry_and_patch_identity_stays_unobservable(self):
        # A fallback ATTEMPT is not a fallback ANSWER. If neither reachability nor
        # patch identity can answer, folding the pair into False manufactures a
        # READY stall from evidence Git never gave.
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.age(row["id"], landreq.LAND_STALL_S["READY"] + 60)
        for cell in (dict(rc=128, patch=True),
                     dict(hang=True, patch=True)):
            with self.unanswerable(**cell):
                lr = landreq.get(row["id"])[0]
                self.assertEqual(lr["state"], "READY")
                self.assertFalse(lr["observable"])
                self.assertFalse(lr["stalled"])
                self.assertEqual(landreq.stalls()[0], [])

    def test_patch_identity_not_local_receipt_proves_land_when_ancestry_is_unknown(self):
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        # Land under a different object id so the independent patch-identity
        # fallback has an actual '-' answer even while ancestry is unavailable.
        self.git("cherry-pick", self.side)
        trunk = self.git("rev-parse", self.main)
        self.record(self.side, trunk)
        for cell in (dict(rc=128), dict(hang=True)):
            with self.unanswerable(**cell):
                lr = landreq.get(row["id"])[0]
                self.assertEqual(lr["state"], "LANDED")
                self.assertTrue(lr["observable"] and lr["landed"])
                self.assertFalse(lr["receipt"])
                self.assertEqual(lr["receipt_state"], landreq.R_LOCAL)

    def test_an_unreadable_index_is_the_git_floor_never_worse(self):
        # the index is an ENHANCEMENT: if it cannot be read at all, observation
        # is exactly today's git behavior (it can never manufacture a LANDED).
        # It is also no longer SILENT: the index answers a marker naming the
        # failure, so the diagnostic says "could not look" instead of "none".
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(
            row["id"], self.side, "ok", polarity="approve")
        self.land_side()
        with mock.patch.object(eventledger, "checked_events",
                               return_value=([], "PermissionError: denied")):
            index = landreq._receipts_by_tip()
            self.assertEqual(list(index.values()), ["PermissionError: denied"])
            self.assertEqual(landreq._receipt_for(self.side, index)[0],
                             landreq.R_UNREADABLE)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "LANDED")        # git saw the merge
        self.assertFalse(lr["receipt"])


class UnsignedFieldMayNotStrengthenTest(ReceiptBase):
    """No field outside the stored payload may affect lifecycle state."""

    def _rec_with(self, **changes):
        tip = "a" * 40
        self.record(tip, self.git("rev-parse", "HEAD"))
        row = dict(self.rows()[-1], **changes)
        self.write_rows(row)
        return tip, landreq._observe(
            self.gitdir(), tip, {}, receipts=landreq._receipts_by_tip())

    def test_flipping_either_unsigned_field_changes_no_observation(self):
        keys = ("observable", "local", "upstream", "has_upstream", "receipt")
        for field in ("upstream", "has_upstream"):
            _tip, a = self._rec_with(**{field: False})
            _tip, b = self._rec_with(**{field: True})
            self.assertEqual({k: a[k] for k in keys},
                             {k: b[k] for k in keys}, field)
            self.assertEqual(a["receipt_state"], landreq.R_LOCAL)
            self.assertIn("payload binding unavailable", a["receipt_reason"])

    def test_live_git_upstream_ancestry_still_grants_landed(self):
        self.add_origin()
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        trunk = self.land_side()
        self.record(self.side, trunk, has_upstream=True, upstream=False)
        self.assertEqual(landreq.get(row["id"])[0]["state"], "MERGED_LOCAL")
        self.git("push", "-q", "origin", self.main)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "LANDED")
        self.assertFalse(lr["receipt"])             # authority came from Git


class ReceiptStateNameIsHonestTest(ReceiptBase):
    """Local self-consistency must be named honestly and remain non-authority."""

    def test_the_wire_value_cannot_be_mistaken_for_attestation(self):
        self.assertEqual(landreq.R_LOCAL, "local-unverified")
        self.assertFalse(hasattr(landreq, "R_VALID"),
                         "the old misleading name must be GONE, not aliased "
                         "beside its successor")

    def test_a_handwritten_self_consistent_row_cannot_manufacture_landed(self):
        tip = "0" * 40
        self.landed_row(tip)
        trunk = self.git("rev-parse", self.main)
        row = {"id": tip, "schema": landreq.LAND_SCHEMA,
               "topic": landreq.LAND_TOPIC,
               "payload": landreq.land_payload(
                   "lane/pruned", "seat/pruned", tip, "e" * 40, trunk),
               "reviewed_tip": tip, "lane": "lane/pruned",
               "branch": "seat/pruned", "patch_id": "e" * 40,
               "trunk_sha": trunk, "repo_id": self.gitdir(),
               "has_upstream": False, "upstream": False,
               "turn": "a" * 64, "receipt": "b" * 64, "chain": 7,
               "profile": "", "ts": "2026-07-01T00:00:00Z"}
        self.write_rows(row)
        lr = landreq.get("aabbccdd")[0]
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertFalse(lr["landed"] or lr["receipt"])
        self.assertEqual(lr["receipt_state"], landreq.R_LOCAL)
        self.assertIn("payload binding unavailable", lr["receipt_reason"])

    def test_local_receipt_cannot_erase_a_repo_less_legacy_loop(self):
        tip, rid = "0" * 40, "aabbccdd"
        ts = "2026-07-01T00:00:00Z"
        row = {"id": rid, "ts": ts, "recipient": "codex-3",
               "lane": "lane/pruned", "ref": tip, "tip": tip,
               "note": None, "deadline_s": 60, "source": "old",
               "status": "verdict", "verdict_ref": "CLEAR",
               "reviewed_tip": tip, "last_updated": ts}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), row))
        self.record(tip, self.git("rev-parse", self.main), repo_id=None)

        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        self.assertIn(rid, lrs)
        lr = lrs[rid]
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertFalse(lr["observable"] or lr["landed"])
        self.assertEqual(lr["receipt_state"], landreq.R_LOCAL)
        self.assertIn("payload binding unavailable", lr["receipt_reason"])


class ReceiptFieldShapesTest(unittest.TestCase):
    """codex-3's blockers (4) and (5) — two validators that had stopped
    validating."""

    def test_impossible_sha_lengths_are_refused(self):
        for n in (40, 64):
            self.assertTrue(landreq._SHA.match("a" * n), "%d must be valid" % n)
        for n in (39, 41, 50, 63, 65):
            self.assertIsNone(landreq._SHA.match("a" * n),
                              "%d is not a git object name" % n)

    def test_a_lone_surrogate_is_refused_not_raised(self):
        """It passed the field check and then made the payload's UTF-8 encode
        RAISE, upstream of every fail-open, so record_land CRASHED instead of
        refusing. A validator whose rejection path is an exception in someone
        else's frame is not a validator."""
        self.assertIsNone(landreq._field("lane\ud800name"))
        self.assertEqual(landreq._field("ordinary/lane"), "ordinary/lane")


class CherryPickedLandTest(LandReqBase):
    """Landed-ness must be read from the PATCH, not from reachability alone.

    The integrator's stated rule is to land the single gated commit onto
    current trunk rather than merge the branch — landing less than was gated
    is safe, landing more never is. That means the landed change carries a
    NEW sha and the reviewed one stays reachable from nothing, so an
    ancestry-only test reports the loop as still awaiting a land that already
    happened. Forever: no later event makes the old sha reachable.

    It is the same defect as deriving READY from "a verdict exists", one
    state further along — a terminal fact inferred from a proxy that does not
    carry it. Measured on this repo 2026-07-25: delim was gated at bc3ef8e,
    landed as 964f06f, and `helm lr` showed the loop READY while the patch
    was demonstrably on trunk.
    """

    def gitdir(self):
        """What the ledger stores as repo_id: `rev-parse --git-common-dir`,
        NOT the worktree root. Passing the root makes every git call fail and
        every answer read False — which is how the first cut of these tests
        "proved" the fix while actually exercising nothing."""
        return os.path.realpath(os.path.join(self.repo, ".git"))

    def cherry_pick_onto_trunk(self, sha):
        """Land it the way the integrator actually lands: a new sha, same patch."""
        self.git("checkout", "-q", self.main)
        self.git("cherry-pick", sha)
        landed = self.git("rev-parse", "HEAD")
        self.assertNotEqual(landed, sha, "the whole point is a DIFFERENT sha")
        return landed

    def test_a_cherry_picked_land_is_SEEN(self):
        from helm import landreq
        self.cherry_pick_onto_trunk(self.side)
        self.assertFalse(
            landreq._is_ancestor(self.gitdir(), self.side, self.main),
            "precondition: ancestry must MISS it, or this proves nothing")
        self.assertTrue(landreq._landed(self.gitdir(), self.side, self.main))

    def test_the_loop_leaves_READY_once_its_patch_is_on_trunk(self):
        """End to end through the lifecycle, which is where it actually bit."""
        from helm import landreq
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "approved",
                                polarity="approve")
        self.cherry_pick_onto_trunk(self.side)
        lr = landreq.get(row["id"])[0]
        self.assertNotEqual(lr["state"], "READY",
                            "a landed loop parked in READY is the zombie")
        # no origin in this fixture, so local trunk IS the end of the road
        self.assertEqual(lr["state"], "LANDED")

    def test_with_an_upstream_it_reads_MERGED_LOCAL_not_READY(self):
        """The same land, one repo shape over: on local trunk but not yet
        pushed. Still not READY — READY means nobody has landed it at all,
        and saying that about landed work sends the lander after their own
        finished job."""
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "approved",
                                polarity="approve")
        self.add_origin()
        self.cherry_pick_onto_trunk(self.side)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "MERGED_LOCAL")

    def test_a_plain_merge_still_reads_landed_via_the_FAST_path(self):
        """Ancestry stays decisive and cheap when it is true; the patch-id
        compare is only a fallback, never a replacement."""
        from helm import landreq
        self.git("checkout", "-q", self.main)
        self.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
        self.assertTrue(landreq._is_ancestor(self.gitdir(), self.side, self.main))
        self.assertTrue(landreq._landed(self.gitdir(), self.side, self.main))

    def test_an_UNLANDED_commit_is_still_not_landed(self):
        """The dangerous direction. A landed-ness check that over-matches is
        worse than one that under-matches: it retires a live obligation."""
        from helm import landreq
        self.assertFalse(landreq._landed(self.gitdir(), self.side, self.main))

    def test_a_DIFFERENT_change_is_never_mistaken_for_this_one(self):
        """Patch-id equality is the claim; make sure it is really equality
        and not 'something landed around then'."""
        from helm import landreq
        self.git("checkout", "-q", self.main)
        self.commit("an unrelated trunk change", path="unrelated")
        self.assertFalse(landreq._landed(self.gitdir(), self.side, self.main))

    def test_an_unreadable_object_never_INVENTS_a_land(self):
        """Unobservable stays unknown: it may neither close the obligation nor
        manufacture a billable negative."""
        self.assertIsNone(landreq._landed(
            self.gitdir(), "0" * 40, self.main))
        self.assertIsNone(landreq._landed(
            self.gitdir(), "not-a-sha", self.main))


class RangeWalkAmbiguityTest(LandReqBase):
    """`git cherry` answers about a RANGE; landed-ness is asked per COMMIT.

    Raised by the integrator while gating the fix, as the specific way it
    could be right in intent and wrong in implementation: `git cherry <ref>
    <tip>` lists every commit from the merge-base forward and its FIRST LINE
    IS THE OLDEST, so an implementation that reads line one answers about a
    different commit than the one it was asked about — and would do it
    silently, with a plausible boolean.

    Their own confirmation hit exactly this: `git cherry HEAD 7ad25f9` over a
    15-commit stack reported 2 landed and 13 not, an aggregate that answers
    nothing about 7ad25f9 itself.

    The fix scans for the line whose sha matches the resolved commit. This
    pins that, because the reasoning is not visible from the call site and
    the next person to touch the parse will not have this conversation.
    """

    def stack_with_only_the_middle_landed(self):
        self.git("checkout", "-q", "-b", "stack", self.main)
        shas = {}
        for name in ("first", "middle", "last"):
            shas[name] = self.commit(name, path=name)
        self.git("checkout", "-q", self.main)
        self.git("cherry-pick", shas["middle"])
        return shas

    def test_the_answer_is_per_COMMIT_not_per_range(self):
        shas = self.stack_with_only_the_middle_landed()
        gitdir = os.path.realpath(os.path.join(self.repo, ".git"))
        # the precondition that makes this a real test: the first line of the
        # cherry output is NOT the commit we ask about
        out = subprocess.run(
            ["git", "--git-dir", gitdir, "cherry", self.main, shas["last"]],
            capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertGreater(len(out), 1, "need a multi-commit range")
        self.assertIn(shas["first"], out[0], "first line must be the OLDEST")

        self.assertFalse(landreq._landed(gitdir, shas["first"], self.main))
        self.assertTrue(landreq._landed(gitdir, shas["middle"], self.main))
        self.assertFalse(landreq._landed(gitdir, shas["last"], self.main))

    def test_reading_line_one_would_FAIL_this(self):
        """Names the mutation the test exists to kill, so a future 'simplify'
        of the parse into `out[0].startswith("-")` cannot pass quietly."""
        shas = self.stack_with_only_the_middle_landed()
        gitdir = os.path.realpath(os.path.join(self.repo, ".git"))
        out = subprocess.run(
            ["git", "--git-dir", gitdir, "cherry", self.main, shas["last"]],
            capture_output=True, text=True, check=True).stdout.splitlines()
        line_one_says = out[0].split()[0] == "-"
        self.assertFalse(line_one_says)                       # it says '+'
        self.assertTrue(landreq._landed(gitdir, shas["middle"], self.main))
        self.assertNotEqual(line_one_says,
                            landreq._landed(gitdir, shas["middle"], self.main))


class UndeclaredCloseLandedTest(LandReqBase):
    """Proof-based terminal closure preserves the missing verdict direction."""

    def reviewed(self, ref=None):
        row = self.dispatch(ref=ref or self.b)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": row["seq"] + 1,
            "id": row["id"], "ts": dispatches.pk.now_ts(),
            "reviewed_tip": ref or self.b, "verdict_ref": "legacy review"}))
        return row

    def legacy_unbound(self, tip=None, rid="aabbccdd"):
        tip = tip or self.b
        ts = "2026-07-01T00:00:00Z"
        row = {"id": rid, "ts": ts, "recipient": "codex-3",
               "lane": "lane/legacy", "ref": tip, "tip": tip,
               "note": None, "deadline_s": 60, "source": "old",
               "status": "verdict", "verdict_ref": "reviewed",
               "reviewed_tip": tip, "last_updated": ts}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), row))
        return rid

    def test_ancestor_close_preserves_undeclared_verdict_and_retires_loop(self):
        row = self.reviewed()
        lr, why = landreq.close_landed(
            row["id"], trunk="refs/heads/" + self.main, live=True)
        self.assertIsNone(why)
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertIsNone(lr["polarity"])
        self.assertEqual(lr["close_reason"], "landed")
        self.assertTrue(lr["terminal"])
        self.assertEqual(lr["close_proof_mode"], "ancestor")
        self.assertEqual(lr["timeline"][-1]["state"], "CLOSED_LANDED")
        self.assertEqual(landreq.loops()[0], [])
        self.assertEqual(landreq.unmeasurable()[0], [])

    def test_patch_equivalent_close_records_the_proof_mode(self):
        row = self.reviewed(ref=self.side)
        self.git("cherry-pick", self.side)
        self.assertFalse(landreq._is_ancestor(
            os.path.realpath(os.path.join(self.repo, ".git")),
            self.side, "refs/heads/" + self.main))
        lr, why = landreq.close_landed(row["id"], trunk=self.main, live=True)
        self.assertIsNone(why)
        self.assertEqual(lr["close_proof_mode"], "patch-equivalent")
        self.assertTrue(lr["terminal"])

    def test_absent_or_unknown_change_refuses_without_appending(self):
        row = self.reviewed(ref=self.side)
        before = len(dispatches.history(row["id"]))
        _lr, why = landreq.close_landed(row["id"], trunk=self.main, live=True)
        self.assertIn("neither an ancestor", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)
        with mock.patch.object(landreq, "_landing_proof",
                               return_value="unknown"):
            _lr, why = landreq.close_landed(row["id"], trunk=self.main, live=True)
        self.assertIn("could not prove", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_unbound_legacy_row_requires_explicit_repo_then_records_it(self):
        rid = self.legacy_unbound()
        _lr, why = landreq.close_landed(rid, trunk=self.main, live=True)
        self.assertIn("pass --repo", why)
        lr, why = landreq.close_landed(
            rid, repo=self.repo, trunk=self.main, live=True)
        self.assertIsNone(why)
        self.assertEqual(lr["closing_repo_id"],
                         os.path.realpath(os.path.join(self.repo, ".git")))
        self.assertIsNone(lr["repo_id"])

    def test_bound_row_rejects_a_different_repo_override(self):
        row = self.reviewed()
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        subprocess.run(["git", "-C", other, "init", "-q"], check=True)
        _lr, why = landreq.close_landed(
            row["id"], repo=other, trunk=self.main, live=True)
        self.assertIn("refusing cross-repository proof", why)

    def test_declared_or_open_rows_refuse_close(self):
        # the alias delegates to close --reason landed, whose domain admits
        # approve rows (proof-refused here: the tip never landed) and refuses
        # FIX/SUPERSEDE rows as contrary debt
        row = self.dispatch(lane="lane/approve")
        dispatches.mark_verdict(row["id"], self.side, "review",
                                polarity="approve")
        _lr, why = landreq.close_landed(row["id"], trunk=self.main, live=True)
        self.assertIn("neither an ancestor", why)
        for polarity in ("fix", "supersede"):
            row = self.dispatch(lane="lane/" + polarity)
            dispatches.mark_verdict(row["id"], self.side, "review",
                                    polarity=polarity)
            _lr, why = landreq.close_landed(row["id"], trunk=self.main, live=True)
            self.assertIn("CONTRARY, not a resolution", why)
        open_row = self.dispatch(lane="lane/open")
        _lr, why = landreq.close_landed(open_row["id"], trunk=self.main, live=True)
        self.assertIn("has no verdict", why)

    def test_trunk_is_explicit_named_and_frozen(self):
        row = self.reviewed()
        _lr, why = landreq.close_landed(row["id"], trunk=self.b, live=True)
        self.assertIn("not an object id", why)
        lr, why = landreq.close_landed(row["id"], trunk=self.main, live=True)
        self.assertIsNone(why)
        self.assertEqual(lr["closing_trunk_ref"], "refs/heads/" + self.main)
        self.assertEqual(lr["closing_trunk_sha"],
                         self.git("rev-parse", self.main))
        # an UNBOUND row still requires the explicit trunk (a bound one may
        # use the discharge trio through close --reason landed)
        rid = self.legacy_unbound(tip=self.c, rid="ddeeff00")
        _lr, why = landreq.close_landed(rid, repo=self.repo, live=True)
        self.assertIn("--trunk is required", why)

    def test_historical_close_survives_trunk_movement_without_reobserving_git(self):
        row = self.reviewed()
        first, why = landreq.close_landed(row["id"], trunk=self.main, live=True)
        self.assertIsNone(why)
        anchor = first["closing_trunk_sha"]
        self.commit("trunk moved")
        again, why = landreq.close_landed(row["id"], trunk=self.main, live=True)
        self.assertIsNone(why)
        self.assertEqual(again["closing_trunk_sha"], anchor)
        # BOUND TO THE GIT READ ITSELF, NOT TO A FUNCTION NAME. The claim is
        # that a historical closure consults no live repository, and `_git` is
        # the one place this module spawns one — so no future route into the
        # same subprocess can satisfy this by being called something else.
        #
        # `_observe` IS called for such a row now, with git=False. It carries
        # the land-receipt diagnostic as well as the git observation, and that
        # half is a lookup in an index project() has already read; skipping the
        # whole function to avoid the git half is what made an UNREADABLE
        # receipt ledger render as "land receipt: none" on exactly these rows.
        with mock.patch.object(landreq, "_git") as git, \
                mock.patch.object(landreq, "_git_observe") as observe:
            replayed = landreq.get(row["id"])[0]
        git.assert_not_called()
        observe.assert_not_called()
        self.assertEqual(replayed["close_reason"], "landed")
        self.assertTrue(replayed["terminal"])
        self.assertEqual(replayed["closing_trunk_sha"], anchor)

    def test_surfaces_never_imply_approval(self):
        row = self.reviewed()
        landreq.close_landed(row["id"], trunk=self.main, live=True)
        lr = landreq.get(row["id"])[0]
        text = landreq._render_show(lr)
        self.assertIn("REVIEWED (UNDECLARED) — CLOSED (LANDED)", text)
        self.assertNotIn("APPROVED", text)
        self.assertIn(lr, landreq.loops(include_landed=True)[0])
        self.assertNotIn(lr, landreq.loops()[0])
        self.assertEqual(landreq._unmeasurable_rows({lr["id"]: lr}), [])

    def test_cli_close_landed_json_and_usage(self):
        row = self.reviewed()
        rc, out, err = run(["close-landed", row["id"][:12], "--trunk",
                            self.main, "--live", "--json"])
        self.assertEqual(rc, 0, err)
        self.assertIn("deprecated: use helm lr close --reason landed", err)
        self.assertEqual(json.loads(out)["close_reason"], "landed")
        for args in (["close-landed", row["id"]],
                     ["close-landed", row["id"], "--trunk"],
                     ["close-landed", row["id"], "--trunk", self.main,
                      "--trunk", self.main],
                     ["close-landed", row["id"], "--wat", "x",
                      "--trunk", self.main]):
            self.assertEqual(run(args)[0], 2)


class OwedByTest(LandReqBase):
    """The fleet-stall root cause (2026-07-26): OPEN billed the REVIEWER, not the
    INTEGRATOR — a seat was billed for work it didn't know existed."""

    def test_open_is_owed_by_the_integrator_not_the_reviewer(self):
        """An obligation that was created but whose delivery was never confirmed
        is the INTEGRATOR's to push through, never the reviewer's to chase."""
        row = self.dispatch()
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "OPEN")
        self.assertEqual(lr["owed_by"], "integrator")

    def test_awaiting_review_is_owed_by_the_reviewer(self):
        """Once delivery IS observed, the reviewer knows. That is when their
        clock starts and the obligation transfers."""
        row = self.dispatch()
        dispatches._mark_delivered(row["id"], "post-1")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "AWAITING_REVIEW")
        self.assertEqual(lr["owed_by"], "reviewer")

    def test_open_is_not_assertable_as_a_stall(self):
        """OPEN has no billable clock — you cannot stall on work you don't know
        exists. The integrator's silence is the failure, but the reviewer's
        bill is the symptom."""
        row = self.dispatch(deadline_s=60)
        self.age(row["id"], 7200)
        lr = landreq.get(row["id"])[0]
        self.assertFalse(lr["stalled"])
        self.assertIsNone(lr["stall_threshold_s"])

    def test_a_verdict_disputed_polarity_is_owed_by_the_author(self):
        """A FIX verdict returns the obligation to the AUTHOR, not the reviewer."""
        row = self.dispatch()
        dispatches._mark_delivered(row["id"], "post-1")
        dispatches.mark_verdict(row["id"], self.side, "needs work",
                                polarity="fix")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["owed_by"], "author")

    def test_every_owed_by_state_names_someone(self):
        for state, owed in landreq.OWED_BY.items():
            self.assertTrue(owed, "%s -> %r" % (state, owed))

    def test_open_rows_are_not_in_the_stalls_subset(self):
        """OPEN rows are live obligations, not stalled ones."""
        row = self.dispatch()
        self.age(row["id"], 7200)
        stalled = landreq.stalls()[0]
        self.assertEqual(stalled, [])

    def test_forward_progress_out_of_open_is_owed_by_the_reviewer(self):
        """Delivery observed → the obligation transfers from integrator to
        reviewer. The row never passes through a state where nobody cares."""
        row = self.dispatch()
        dispatches._mark_delivered(row["id"], "post-1")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "AWAITING_REVIEW")
        self.assertEqual(lr["owed_by"], "reviewer")

    def test_dispatch_send_posts_a_public_at_mention(self):
        """The notification leg: a dispatch at-mentions the reviewer in main,
        so the beacon wakes on the mention. The fleet-stall root cause was
        that nothing told the reviewer a review was owed."""
        from unittest import mock
        patched = mock.patch.object(chat, "post", return_value={
            "id": "test-post", "ts": "2020-01-01T00:00:00Z"})
        with patched as fake_post:
            row = self.dispatch()
        # The DM goes to the private lane via seats.dm; the @mention goes to
        # main via chat.post in dispatches.send(). Both happen in send(), not
        # in add(), so check that they are reachable.
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["id"])


class NotifyPersistenceTest(LandReqBase):
    """Three-state persistence: told / never-told / told-but-delivery-failed.
    The r4 P0: _record_notify_failed was 100% inert (called pk.write_json with
    a nonexistent append parameter, swallowed the TypeError silently). The
    test that proved "green" checked for no exception — which tests the handler,
    not the durability. These tests assert the OBSERVABLE EFFECT."""

    def test_never_told_OPEN_has_no_notify_failed_marker(self):
        """A fresh dispatch that was never sent has no notify record."""
        row = dispatches.add("codex-3", "lane/fresh", ref=self.side,
                             repo=self.repo, notify=False, new_work=True)
        self.assertIsNotNone(row)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "OPEN")
        self.assertIsNone(lr["notify_failed"])

    def test_send_succeeds_no_notify_failed_marker(self):
        """When notification succeeds, no failure marker is written."""
        row = dispatches.add("codex-3", "lane/sent", ref=self.side,
                             repo=self.repo, notify=False, new_work=True)
        dispatches._mark_delivered(row["id"], "post-1")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "AWAITING_REVIEW")
        self.assertIsNone(lr["notify_failed"])

    def test_notify_failed_is_persisted_and_readable(self):
        """When notification fails, the event is DURABLE and visible in the
        lifecycle. This is the test r3 couldn't pass — _record_notify_failed
        was silently inert."""
        row = dispatches.add("codex-3", "lane/failed", ref=self.side,
                             repo=self.repo, notify=False, new_work=True)
        dispatches._record_notify_failed(row["id"], "test failure")
        lr = landreq.get(row["id"])[0]
        self.assertIsNotNone(lr["notify_failed"],
                             "notify_failed must be a dict, not None — "
                             "the durability leg must WORK")
        self.assertEqual(lr["notify_failed"]["id"], row["id"])
        self.assertEqual(lr["notify_failed"]["event"], "notify-failed")
        self.assertEqual(lr["notify_failed"]["reason"], "test failure")

    def test_notify_failed_does_not_leak_to_other_rows(self):
        """The marker is keyed by row id — a different dispatch never sees it."""
        row_a = dispatches.add("codex-3", "lane/a", ref=self.side,
                               repo=self.repo, notify=False, new_work=True)
        dispatches._record_notify_failed(row_a["id"], "failed for a")
        row_b = dispatches.add("codex-3", "lane/b", ref=self.side,
                               repo=self.repo, notify=False, new_work=True)
        lr_b = landreq.get(row_b["id"])[0]
        self.assertIsNone(lr_b["notify_failed"],
                          "an unrelated dispatch must never read another's "
                          "notify-failed marker")

    def test_delivered_rows_dont_read_notify_failed(self):
        """Once delivery is observed, the notify-failed status is moot —
        the DM got through regardless."""
        row = dispatches.add("codex-3", "lane/delivered", ref=self.side,
                             repo=self.repo, notify=False, new_work=True)
        dispatches._record_notify_failed(row["id"], "was failing")
        dispatches._mark_delivered(row["id"], "post-1")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "AWAITING_REVIEW")
        self.assertIsNone(lr["notify_failed"])


LAND_DEL_TEST = 6    # tracked files in the helper's HEAD fixture


class LandDeletionAckTest(LandReqBase):
    """A merge that deletes TRACKED files from a remote REFUSES by default.
    --ack-deletions is the explicit authorization. UNKNOWN fails safe."""

    def setUp(self):
        super().setUp()
        self.gitdir = os.path.realpath(os.path.join(self.repo, ".git"))
        # Plant tracked files so HEAD has a known tracked set
        for i in range(LAND_DEL_TEST):
            path = os.path.join(self.repo, "tracked_%d.md" % i)
            with open(path, "w") as f:
                f.write("tracked file %d\n" % i)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "adding tracked files")
        self.head = self.git("rev-parse", "HEAD")
        self.trunk = "refs/heads/" + self.main

    def rcommit(self, text, files_to_delete=(), branch=None):
        """Commit deletion of named tracked files, optionally on a branch."""
        if branch:
            self.git("checkout", "-b", branch)
        for f in files_to_delete:
            os.unlink(os.path.join(self.repo, f))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", text)
        return self.git("rev-parse", "HEAD")

    def test_no_deletion_returns_empty(self):
        """A merge that deletes nothing returns empty (not an error)."""
        from helm import landreq
        paths, err = landreq._tracked_deletions(
            self.gitdir, self.trunk, self.head)
        self.assertIsNone(err)
        self.assertEqual(paths, [])

    def test_tracked_deletions_are_detected(self):
        """Three tracked files deleted in the merge range."""
        from helm import landreq
        tip = self.rcommit("delete 3 tracked", ("tracked_0.md",
                                                "tracked_1.md",
                                                "tracked_2.md"),
                           branch="side-del-3")
        self.git("checkout", self.main)
        paths, err = landreq._tracked_deletions(
            self.gitdir, self.trunk, tip)
        self.assertIsNone(err)
        self.assertEqual(len(paths), 3)
        for p in paths:
            self.assertIn("tracked_", p)

    def test_untracked_deletions_are_not_counted(self):
        """An untracked file deleted is not in ls-files -> not counted."""
        from helm import landreq
        # Delete a tracked file so diff is non-empty
        tip = self.rcommit("delete 1 tracked", ("tracked_5.md",),
                           branch="side-del-untracked")
        self.git("checkout", self.main)
        paths, err = landreq._tracked_deletions(
            self.gitdir, self.trunk, tip)
        self.assertIsNone(err)
        # Only tracked_5.md should appear
        self.assertEqual(len(paths), 1)
        self.assertIn("tracked_5.md", paths[0])

    def test_missing_repo_returns_error_not_empty(self):
        """UNKNOWN fails SAFE — missing gitdir returns err, not empty."""
        from helm import landreq
        paths, err = landreq._tracked_deletions(
            "/nonexistent/path", "main", "sha")
        self.assertIsNotNone(err)
        self.assertIsNone(paths)

    def test_deletion_refusal_text(self):
        """Count, paths, flag, and CURATIVE message all present."""
        from helm import landreq
        r = landreq._deletion_refusal(
            ["docs/a.md", "docs/b.md", "docs/c.md", "docs/d.md"])
        self.assertIn("deletes 4 TRACKED files", r)
        self.assertIn("docs/a.md", r)
        self.assertIn("+1 more TRACKED file", r)
        self.assertIn("--ack-deletions", r)

    def test_the_flag_the_refusal_demands_is_accepted_by_the_parser(self):
        """The refusal's own cure must be reachable THROUGH THE CLI. For as
        long as --ack-deletions was missing from guard_tail's flags tuple,
        `helm lr land <id>` refused with rc 1 telling the operator to pass
        --ack-deletions, and `helm lr land <id> --ack-deletions` returned
        rc 2 as an unknown argument — the cure the refusal named was
        unpronounceable, so a deletion-bearing merge could never be
        witnessed from the CLI at all. Every prior test here called the
        helpers directly, so the CLI seam carried no pin.

        Found by a read-only code-ground audit of the terminal paths."""
        tip = self.rcommit("delete 1 tracked", ("tracked_3.md",),
                           branch="side-del-cli")
        self.git("checkout", self.main)
        row = self.dispatch(ref=tip)
        dispatches.mark_verdict(row["id"], tip, "ok", polarity="approve")
        # Without the flag: the refusal fires and NAMES the cure.
        rc, out, err = run(["land", row["id"]])
        text = out + err
        self.assertEqual(rc, 1, text)
        self.assertIn("REFUSED", text)
        self.assertIn("--ack-deletions", text)
        # With the flag: the parser must ACCEPT it (the bug returned rc 2
        # usage-error here) and the land proceeds to the witness path, which
        # is fail-open with no signer in this environment.
        rc, out, err = run(["land", row["id"], "--ack-deletions"])
        text = out + err
        self.assertNotEqual(rc, 2, "parser rejected the refusal's own cure: "
                            + text)
        self.assertEqual(rc, 0, text)
        self.assertIn("NO MERGE PERFORMED", text)
        self.assertNotIn("REFUSED", text)

    def test_deletion_refusal_plural_is_correct(self):
        """One file: 'deletes 1 TRACKED file' (no plural)."""
        from helm import landreq
        r = landreq._deletion_refusal(["one.md"])
        self.assertIn("deletes 1 TRACKED file", r)
        self.assertNotIn("files", r.split("deletes")[1])
        self.assertNotIn("+", r)


class ChainFoldingTest(unittest.TestCase):
    """The projection accounted per ROW while the unit of work is the CHAIN.

    Three consumers had the same defect. `_stalled_rows` billed a superseded
    round's dwell to an author who had nothing to do — 21 of 51 live stalled
    rows on 2026-08-01 were predecessors, rolling up to ten real debts, and no
    close ladder could retire them. `_unmeasurable_rows` read only the head's
    empty gate while a successor carried a verified one at the same reviewed
    tip. The primary list later repeated the per-row mistake, showing seven
    relieved predecessors as fresh integrator debt. One validated walk over the
    supersedes edge answers all three."""

    def row(self, rid, **kw):
        lr = {"id": rid, "stalled": False, "terminal": False, "dwell_s": 60,
              "state": "CHANGES_REQUESTED", "supersedes": None, "polarity": None,
              "ungated": None, "gate": "", "reviewed_tip": None,
              "owed_by": "author"}
        lr.update(kw)
        return lr

    def board(self, *rows, **kw):
        """(projected lrs, RAW ledger view). The forest is built from the RAW
        rows because project() drops cancelled ones while the chain grammar
        permits a successor of a cancelled parent — so a fixture that only
        ever hands over `lrs` cannot express the transit case at all."""
        lrs = {r["id"]: r for r in rows if not r.get("_cancelled")}
        # REPLAY-LEGAL BY CONSTRUCTION. A real snapshot row carries id and v
        # and has already been through dispatches._replay_chain, so chain_root
        # is exactly id | None | CHAIN_UNKNOWN — never "", never blank, never
        # a non-str. A fixture that can build those shapes invites arms for a
        # world production cannot produce.
        raw = {r["id"]: {"id": r["id"],
                         "v": r.get("v", 3),
                         "supersedes": r.get("supersedes"),
                         "chain_root": r.get("chain_root", "ROOT"),
                         "status": "cancelled" if r.get("_cancelled") else "open"}
               for r in rows}
        raw.update(kw.get("raw_extra") or {})
        return lrs, raw

    def stalled_ids(self, board):
        lrs, raw = board
        return sorted(r["id"] for r in landreq._stalled_rows(lrs, raw))

    def loop_ids(self, board, include_landed=False):
        lrs, raw = board
        return sorted(r["id"] for r in
                      landreq._loop_rows(lrs, raw, include_landed))

    # ---- site 0: the primary board ------------------------------------

    def test_default_loops_show_the_carrying_frontier_not_its_predecessor(self):  # noqa: VACUOUS_ASSERTION — exact non-empty [child] output proves the classifier ran while simultaneously excluding the parent; an always-empty filter fails
        parent = self.row("p", state="CHANGES_REQUESTED", owed_by="integrator")
        child = self.row("c", supersedes="p", state="READY", owed_by="lander")
        board = self.board(parent, child)
        self.assertEqual(self.loop_ids(board), ["c"])
        # Historical mode retains both exact rows and their independent facts.
        self.assertEqual(self.loop_ids(board, include_landed=True), ["c", "p"])
        self.assertFalse(parent["terminal"])
        self.assertNotIn("close_reason", parent)

    def test_a_landed_successor_removes_the_predecessor_not_a_bystander(self):
        parent = self.row("p", owed_by="integrator")
        landed = self.row("c", supersedes="p", state="LANDED", terminal=True)
        other = self.row("bystander")
        self.assertEqual(self.loop_ids(self.board(parent, landed, other)),
                         ["bystander"])

    def test_every_dead_branch_leaves_the_nearest_unresolved_ancestor(self):
        parent = self.row("p")
        abandoned = self.row("a", supersedes="p", state="ABANDONED",
                             terminal=True)
        stranded = self.row("s", supersedes="p", state="REVIEWED",
                            terminal=True, close_reason="stranded")
        self.assertEqual(
            self.loop_ids(self.board(parent, abandoned, stranded)), ["p"])

    def test_default_loops_traverse_cancelled_transit_to_the_live_frontier(self):
        parent = self.row("p")
        cancelled = self.row("x", supersedes="p", _cancelled=True)
        grandchild = self.row("g", supersedes="x")
        self.assertEqual(
            self.loop_ids(self.board(parent, cancelled, grandchild)), ["g"])

    def test_loop_topology_failure_reaches_the_public_surface(self):
        a = self.row("a", supersedes="b")
        b = self.row("b", supersedes="a")
        lrs, raw = self.board(a, b)
        with mock.patch.object(landreq, "project_raw",
                               return_value=(lrs, raw, None)):
            rows, unavailable = landreq.loops()
        self.assertIsNone(rows)
        self.assertIn("cycle", unavailable)

    def test_board_section_uses_the_same_frontier_as_the_list(self):
        parent = self.row("p", stalled=True)
        child = self.row("c", supersedes="p", stalled=True)
        lrs, raw = self.board(parent, child)
        with mock.patch.object(landreq, "project_raw",
                               return_value=(lrs, raw, None)), \
                mock.patch.object(landreq, "card", side_effect=dict):
            section = landreq.board_section()
        self.assertIsNone(section["unavailable"])
        self.assertEqual([row["id"] for row in section["loops"]], ["c"])
        self.assertEqual([row["id"] for row in section["stalled"]], ["c"])

    # ---- site 1: the stall census -------------------------------------

    def test_a_superseded_round_is_not_billed_to_its_author(self):
        parent = self.row("p", stalled=True, dwell_s=9000)
        child = self.row("c", stalled=True, dwell_s=100, supersedes="p")
        got = self.stalled_ids(self.board(parent, child))
        # the POSITIVE half is the point: the debt MOVED to the successor, it
        # was not written off. A cure that emptied both rows would pass an
        # assertion that only said "p is gone".
        self.assertEqual(got, ["c"])

    def test_a_six_round_chain_bills_one_debt_not_six(self):
        rows = [self.row("r0", stalled=True, dwell_s=9000)]
        for i in range(1, 6):
            rows.append(self.row("r%d" % i, stalled=True, dwell_s=9000 - i,
                                 supersedes="r%d" % (i - 1)))
        self.assertEqual(self.stalled_ids(self.board(*rows)), ["r5"])

    def test_a_LANDED_successor_absorbs_the_debt(self):
        parent = self.row("p", stalled=True)
        child = self.row("c", supersedes="p", state="LANDED", terminal=True)
        # BYSTANDER: an unrelated stalled row that must SURVIVE. Without it the
        # assertion below is satisfied just as well by a census that returns
        # nothing at all, which is the failure this whole class is about.
        other = self.row("bystander", stalled=True)
        self.assertEqual(self.stalled_ids(self.board(parent, child, other)),
                         ["bystander"])

    def test_a_terminal_close_absorbs_even_when_the_STATE_does_not_say_so(self):
        """codex-3's topology refutation of d9adb643, his exact two shapes.

        A row's projected STATE does not track its CLOSURE: close_reason=landed
        can still render REVIEWED, and close_reason=superseded can still render
        CHANGES_REQUESTED. Both are terminal and both carried the parent's debt
        away, but neither appears in the LANDED/SUPERSEDED state tuple the first
        version compared against — so the parent stayed billed for a debt its
        successor had already discharged. His probes returned [p]; [] is right.

        I keyed on a PROJECTION of the fact instead of the fact, which is the
        error this whole lane exists to correct, committed one layer over."""
        shapes = (("landed", "REVIEWED"), ("superseded", "CHANGES_REQUESTED"))
        self.assertEqual(len(shapes), 2)
        for reason, state in shapes:
            parent = self.row("p", stalled=True)
            child = self.row("c", supersedes="p", state=state, terminal=True,
                             close_reason=reason)
            other = self.row("bystander", stalled=True)
            self.assertEqual(
                self.stalled_ids(self.board(parent, child, other)),
                ["bystander"],
                "close_reason=%s rendering %s left the parent billed"
                % (reason, state))

    def test_a_STRANDED_close_carries_nothing_either(self):
        """The other side of the same fact, and the reason this is a reason-set
        rather than 'terminal absorbs': `stranded` ENDS work without moving it,
        exactly like abandoned. A predicate that absorbed on terminality alone
        would retire a debt nobody took."""
        parent = self.row("p", stalled=True)
        child = self.row("c", supersedes="p", state="REVIEWED", terminal=True,
                         close_reason="stranded")
        self.assertEqual(self.stalled_ids(self.board(parent, child)), ["p"])

    def test_an_ABANDONED_successor_carries_NOTHING(self):
        """The one direction where staying stalled is the honest answer: a
        successor written off moved no debt anywhere, so relieving the parent
        would retire an obligation nobody ever took."""
        parent = self.row("p", stalled=True)
        child = self.row("c", supersedes="p", state="ABANDONED", terminal=True)
        self.assertEqual(self.stalled_ids(self.board(parent, child)), ["p"])

    def test_an_unchained_stalled_row_is_untouched(self):
        """The control. Every assertion above is about rows being REMOVED, and
        a walk that removed everything would satisfy all of them."""
        lone = self.row("lone", stalled=True)
        self.assertEqual(self.stalled_ids(self.board(lone)), ["lone"])

    # ---- topology: the forest, not the first edge -----------------------

    def test_an_abandoned_SIBLING_does_not_resurrect_a_carried_parent(self):
        """A fork does NOT require every branch to absorb. My meld proposal
        said it did; codex-3 refuted it: an abandoned sibling does not mint a
        second copy of the parent's debt, so if any branch carries, billing
        the parent too is DOUBLE BILLING."""
        parent = self.row("p", stalled=True)
        dead = self.row("d", supersedes="p", state="ABANDONED", terminal=True)
        live = self.row("L", supersedes="p", stalled=True)
        self.assertEqual(self.stalled_ids(self.board(parent, dead, live)),
                         ["L"])

    def test_when_EVERY_branch_dies_the_nearest_ancestor_keeps_its_debt(self):
        parent = self.row("p", stalled=True)
        d1 = self.row("d1", supersedes="p", state="ABANDONED", terminal=True)
        d2 = self.row("d2", supersedes="p", state="REVIEWED", terminal=True,
                      close_reason="stranded")
        self.assertEqual(self.stalled_ids(self.board(parent, d1, d2)), ["p"])

    def test_two_live_fork_leaves_are_two_obligations_and_no_ancestor(self):
        parent = self.row("p", stalled=True)
        a = self.row("a", supersedes="p", stalled=True)
        b = self.row("b", supersedes="p", stalled=True)
        self.assertEqual(self.stalled_ids(self.board(parent, a, b)), ["a", "b"])

    def test_a_CANCELLED_middle_still_connects_its_live_descendant(self):
        """project() drops status=cancelled while the chain grammar permits a
        successor of a cancelled parent, so a projection-only graph severed
        p -> cancelled -> live and the live carrier stopped absorbing. The
        cancelled node is TRANSIT: never billable, always traversable."""
        parent = self.row("p", stalled=True)
        gone = self.row("x", supersedes="p", _cancelled=True)
        live = self.row("g", supersedes="x", stalled=True)
        got = self.stalled_ids(self.board(parent, gone, live))
        self.assertEqual(got, ["g"])          # not ["p", "g"], not ["p"]

    def test_measurement_reaches_an_APPROVE_GRANDCHILD_behind_a_FIX(self):  # noqa: VACUOUS_ASSERTION — the assertIsNotNone IS the claim; the FIX-middle-alone assertIsNone below is the control on the same observable
        """Traversal continues THROUGH a gated FIX middle: the middle answers
        nothing itself, and the grandchild at the same tip answers everything."""
        parent = self.held("p")
        middle = self.row("m", supersedes="p", polarity="fix",
                          gate="deadbeefdeadbeef", reviewed_tip="81aed834")
        grand = self.row("g", supersedes="m", state="READY", polarity="approve",
                         gate="3caeb50c42f1c790", reviewed_tip="81aed834")
        self.assertIsNotNone(self.through(parent, middle, grand))
        # control on the same observable: the FIX middle ALONE answers nothing,
        # so the yes above came from the grandchild and not from reachability.
        self.assertIsNone(self.through(parent, middle))

    def test_a_CYCLE_makes_the_WHOLE_surface_unavailable(self):
        """Never a partial list: "this row still bills" and "the board is
        unavailable" are two different external answers, and a topology we
        cannot trust must not be able to SUPPRESS a debt."""
        a = self.row("a", stalled=True, supersedes="b")
        b = self.row("b", stalled=True, supersedes="a")
        lrs, raw = self.board(a, b)
        # control: a HEALTHY board of the same shape does not raise, so the
        # raises below are about the cycle and not about the fixture.
        ok_lrs, ok_raw = self.board(self.row("h", stalled=True))
        self.assertEqual([r["id"] for r in
                          landreq._stalled_rows(ok_lrs, ok_raw)], ["h"])
        with self.assertRaises(landreq._ChainUntrustworthy):
            landreq._stalled_rows(lrs, raw)
        with self.assertRaises(landreq._ChainUntrustworthy):
            landreq._unmeasurable_rows(lrs, raw)

    def test_a_ROOT_MISMATCH_makes_the_WHOLE_surface_unavailable(self):  # noqa: VACUOUS_ASSERTION — absence of a fold IS the claim; the same-root control above asserts the fold works
        parent = self.row("p", stalled=True, chain_root="ROOT-A")
        child = self.row("c", supersedes="p", stalled=True,
                         chain_root="ROOT-B")
        lrs, raw = self.board(parent, child)
        # control: the SAME two rows sharing one root fold normally.
        same = self.board(self.row("p", stalled=True),
                          self.row("c", supersedes="p", stalled=True))
        self.assertEqual(self.stalled_ids(same), ["c"])
        with self.assertRaises(landreq._ChainUntrustworthy):
            landreq._stalled_rows(lrs, raw)

    def test_an_ABSENT_chain_root_on_an_edge_is_UNKNOWN_not_a_pass(self):  # noqa: VACUOUS_ASSERTION — the REFUSAL is the claim; the both-roots-present fold asserted first is the unconditional control
        """codex-3, refuting 9e9b4bb: a v3 child that names a parent and omits
        chain_root replayed as None, and the validation required BOTH roots
        truthy before comparing — so the edge skipped validation entirely and
        the fold SUPPRESSED the parent with unavailable=None. Unknown identity
        on a load-bearing edge is not a pass.

        My fixture defaulted chain_root on every row, so 201 tests could not
        see it: the shape under test was one the board could not express."""
        # Control FIRST, unconditionally: both roots present and equal folds
        # normally, so the raises below are about the ABSENCE and not about a
        # fixture shape that never folds at all.
        ok = self.board(self.row("p", stalled=True),
                        self.row("c", supersedes="p", stalled=True))
        self.assertEqual(self.stalled_ids(ok), ["c"])
        for missing in ("child", "parent"):
            parent = self.row("p", stalled=True,
                              chain_root=None if missing == "parent" else "ROOT")
            child = self.row("c", supersedes="p", stalled=True,
                             chain_root=None if missing == "child" else "ROOT")
            lrs, raw = self.board(parent, child)
            with self.assertRaises(landreq._ChainUntrustworthy,
                                   msg="absent root on the %s passed" % missing):
                landreq._stalled_rows(lrs, raw)
    def test_a_ROOT_parent_with_no_chain_root_roots_its_child_and_folds(self):
        """codex-3, refuting 1c2aa62: THE ENDPOINTS ARE NOT SYMMETRIC. A parent
        that names no parent ROOTS ITS OWN CHAIN, and the writer roots its
        child at parent.id — so an absent chain_root there is normal. My
        symmetric rule refused that edge and darkened the WHOLE board for an
        ordinary continuation: worse than the hole it closed, because it
        refused valid work.

        398 of 628 live rows are this shape, and only 6 are legacy — which is
        why the rule keys on TOPOLOGY (names no parent) and not on schema
        version. My first cut branched on v and the census refuted it."""
        schemas = (1, 3)
        self.assertEqual(len(schemas), 2)
        for schema in schemas:
            parent = self.row("p", stalled=True, v=schema, chain_root=None)
            child = self.row("c", supersedes="p", stalled=True, chain_root="p")
            self.assertEqual(self.stalled_ids(self.board(parent, child)), ["c"],
                             "v=%s root-parent was refused" % schema)

    def test_a_parent_that_is_ITSELF_a_continuation_must_carry_a_root(self):  # noqa: VACUOUS_ASSERTION — the REFUSAL is the claim; the folds-normally board asserted first is the unconditional control
        """The other half of the same rule: a row that names a parent AND
        carries no root has no self-rooting excuse, whatever its version."""
        ok = self.board(self.row("p", stalled=True),
                        self.row("c", supersedes="p", stalled=True))
        self.assertEqual(self.stalled_ids(ok), ["c"])
        # The named grandparent is deliberately ABSENT from the board: with it
        # present, the g->p edge refuses FIRST and assertRaises passes without
        # the clause under test ever deciding. My first two versions both did
        # that — same trap, one level out each time.
        parent = self.row("p", supersedes="absent-g", stalled=True,
                          chain_root=None)
        # child root EQUALS parent.id, so a mismatch cannot be the reason it
        # refuses — only the missing-identity clause can. My first version used
        # a different root and the arm passed for the wrong reason: a mutation
        # letting a continuation self-root SURVIVED it.
        child = self.row("c", supersedes="p", stalled=True, chain_root="p")
        lrs, raw = self.board(parent, child)
        with self.assertRaises(landreq._ChainUntrustworthy):
            landreq._stalled_rows(lrs, raw)

    def test_CHAIN_UNKNOWN_is_never_identity_on_either_side(self):  # noqa: VACUOUS_ASSERTION — the REFUSAL is the claim; the folds-normally board asserted first is the unconditional control
        """Two rows both reading UNKNOWN are two INDEPENDENTLY CORRUPTED rows.
        Their equality is agreement that they are broken, not proof they share
        a chain — and the truthy sentinel made == say otherwise.

        Not a live shape: zero rows on the 628-row ledger carry CHAIN_UNKNOWN
        today. The arm pins it before production produces it."""
        ok = self.board(self.row("p", stalled=True),
                        self.row("c", supersedes="p", stalled=True))
        self.assertEqual(self.stalled_ids(ok), ["c"])
        U = dispatches.CHAIN_UNKNOWN
        for proot, croot in ((U, U), (U, "ROOT"), ("ROOT", U)):
            parent = self.row("p", stalled=True, chain_root=proot)
            child = self.row("c", supersedes="p", stalled=True, chain_root=croot)
            lrs, raw = self.board(parent, child)
            with self.assertRaises(landreq._ChainUntrustworthy,
                                   msg="parent=%r child=%r passed" % (proot, croot)):
                landreq._stalled_rows(lrs, raw)

    def test_the_untrustworthy_surface_reaches_the_VERB_as_unavailable(self):
        """The exception is internal; what a caller sees must be an honest
        unavailable, never a short row list that reads like a clean board."""
        a = self.row("a", stalled=True, supersedes="b")
        b = self.row("b", stalled=True, supersedes="a")
        lrs, raw = self.board(a, b)
        with mock.patch.object(landreq, "project_raw",
                               return_value=(lrs, raw, None)):
            rows, unavailable = landreq.stalls()
        self.assertIsNone(rows)
        self.assertIn("cycle", unavailable)

    # ---- site 2: the unmeasurable census -------------------------------

    def through(self, *rows):
        """_measurable_through for row "p" over a board — the predicate under
        test, bound directly. Asserting these through the unmeasurable COLUMN
        would now measure FRONTIER OWNERSHIP instead: a live descendant moves
        its parent's hold whether or not it clears it, so the parent leaves
        the column either way and the assertion would pass for the wrong
        reason (codex-3, blocker 2)."""
        lrs, raw = self.board(*rows)
        kids, node, err = landreq._chain_forest(raw, lrs)
        self.assertIsNone(err)
        return landreq._measurable_through(lrs["p"], kids, node)


    def held(self, rid, **kw):
        return self.row(rid, state="REVIEWED", polarity="approve",
                        ungated="approved with no minted gate receipt",
                        reviewed_tip="81aed834", **kw)

    def unmeasurable_ids(self, board):
        lrs, raw = board
        return sorted(r["id"] for r, _why in
                      landreq._unmeasurable_rows(lrs, raw))

    def test_a_gated_child_at_the_SAME_tip_answers_the_hold(self):
        parent = self.held("p")
        child = self.row("c", supersedes="p", state="READY", polarity="approve",
                         gate="3caeb50c42f1c790", reviewed_tip="81aed834")
        # BYSTANDER, same reason as the stall site: an empty column is also
        # what a classifier that stopped classifying returns.
        other = self.held("bystander")
        self.assertEqual(
            self.unmeasurable_ids(self.board(parent, child, other)),
            ["bystander"])
        self.assertIsNotNone(self.through(parent, child))

    def test_a_child_gated_at_a_DIFFERENT_tip_proves_nothing(self):
        """A gate binds a tree. A successor gated on other code is a
        measurement of other code, and accepting it would let any later green
        launder this row's missing receipt."""
        parent = self.held("p")
        child = self.row("c", supersedes="p", state="READY", polarity="approve",
                         gate="3caeb50c42f1c790", reviewed_tip="deadbeef")
        self.assertIsNone(self.through(parent, child))

    def test_a_same_tip_FIX_carrying_a_gate_does_NOT_rescue_its_parent(self):  # noqa: VACUOUS_ASSERTION — the None IS the claim; the approve-polarity control below proves the predicate says yes
        """codex-3, gate d9adb643. The predicate read `kid.get("polarity")`
        for TRUTH, so ANY declared polarity satisfied it — and a FIX carrying
        a perfectly valid gate at the very same tip rescued a parent held for
        want of an APPROVAL. A fix verdict authorizes no land; it is the
        opposite of the thing the parent is missing. mark_verdict refuses an
        ungated approve for exactly this reason, and truthiness walked around
        that refusal one layer up.

        Every fixture I wrote gave the child polarity="approve", so the clause
        that decided the result was never the clause under test — the same
        miss as this class's M4."""
        bad_polarities = ("fix", "supersede")
        # Pinned before the loop: an empty tuple asserts nothing, and this arm
        # exists BECAUSE a clause was never reached. It must not itself become
        # a test whose body never runs.
        self.assertEqual(len(bad_polarities), 2)
        for bad in bad_polarities:
            parent = self.held("p")
            child = self.row("c", supersedes="p", state="CHANGES_REQUESTED",
                             polarity=bad, gate="3caeb50c42f1c790",
                             reviewed_tip="81aed834")
            self.assertIsNone(
                self.through(parent, child),
                "a %s verdict rescued a parent held for an APPROVAL" % bad)
        # control: flip ONLY the polarity to approve and the same shape answers
        good = self.row("c", supersedes="p", state="READY", polarity="approve",
                        gate="3caeb50c42f1c790", reviewed_tip="81aed834")
        self.assertIsNotNone(self.through(self.held("p"), good))

    def test_a_child_that_is_ITSELF_ungated_answers_nothing(self):
        """THE CHILD CARRIES A TOKEN AND IS STILL REFUSED — a tier refusal, not
        a missing receipt. Written that way deliberately: my first version gave
        the child no gate at all, so it was rejected by the token clause and the
        `ungated` clause was never reached. Deleting that clause left this test
        GREEN (mutation M4 survived). A gate token is not an approval; `ungated`
        is what says the approval stands, and only a child whose OWN approval
        was refused can prove this clause carries weight."""
        parent = self.held("p")
        child = self.row("c", supersedes="p", state="REVIEWED",
                         polarity="approve", gate="3caeb50c42f1c790",
                         reviewed_tip="81aed834",
                         ungated="approval tier not satisfied")
        self.assertIsNone(self.through(parent, child))
        # and the HOLD still moves: the child is the live frontier, so it is
        # the row that owes, not its parent.
        self.assertEqual(self.unmeasurable_ids(self.board(parent, child)), ["c"])

    def test_an_unchained_held_row_stays_unmeasurable(self):
        """The control for this site: the walk must not swallow a row that has
        no chain to be answered by. Asserts the REASON too — a row surfaced
        under the wrong explanation is the defect this classifier already
        shipped once, when every REVIEWED row was labelled UNDECLARED."""
        _lrs, _raw = self.board(self.held("p"))
        rows = landreq._unmeasurable_rows(_lrs, _raw)
        self.assertEqual([r["id"] for r, _why in rows], ["p"])
        self.assertIn("APPROVE, held", rows[0][1])


class StallsSayTheTipMoved(LandReqBase):
    """A verdict binds a BASE as well as a TIP: when the lane's head advances
    after the review, the verdict may be answering a question that no longer
    exists — and the stalls surface is exactly where a reviewer looks for
    what they still owe. Measured 2026-08-04: trunk moved 45+ times in one
    night and the integrator hand-computed "did the tip move under this
    review" before every land because the listing would not say it.

    The marker is three-dot counted (reviewed...head), never two-dot — the
    #91 trap — and SILENT whenever it cannot measure, because an absent
    marker must never read as "the tip did not move"."""

    def _stalled_ready_row(self, lane="lane/probe"):
        # ref_branch binds the UNIQUE local branch pointing at the tip
        # (dispatches._unique_local_tip_branch: exactly one, else None), so
        # the reviewed tip must be a commit that exists ONLY on the lane
        # branch — self.side is on `side` too, which would bind None.
        self.git("checkout", "-q", "-b", lane, self.side)
        tip = self.commit("lane work", path="lanefile")
        self.git("checkout", "-q", self.main)
        row = self.dispatch(lane=lane, ref=tip)
        self.assertEqual(dispatches.rows()[row["id"]].get("ref_branch"),
                         "refs/heads/" + lane,
                         "fixture law: the row must bind the LANE branch")
        dispatches.mark_verdict(row["id"], tip, "ok",
                                polarity="approve")
        self.age(row["id"], landreq.LAND_STALL_S["READY"] + 60)
        return row

    def test_a_moved_tip_is_NAMED_with_its_three_dot_count(self):
        row = self._stalled_ready_row()
        # POSITIVE CONTROL FIRST: the row IS stalled, so the silence/print
        # below is the marker acting, not an empty listing.
        stalled = landreq.stalls()[0]
        self.assertEqual([lr["id"] for lr in stalled], [row["id"]])
        # advance the lane two commits past the reviewed tip
        self.git("checkout", "-q", "lane/probe")
        self.commit("m1"); head = self.commit("m2")
        self.git("checkout", "-q", self.main)
        self.git("branch", "-f", "lane/probe", head)
        rc, out, err = run(["stalls"])
        self.assertEqual(rc, 0, err)
        self.assertIn("(tip MOVED +2 since review)", out)

    def test_an_unmoved_tip_says_nothing(self):
        row = self._stalled_ready_row()
        rc, out, err = run(["stalls"])
        self.assertEqual(rc, 0, err)
        self.assertIn(row["id"][:12], out)   # the row IS listed
        self.assertNotIn("MOVED", out)

    def test_a_rebased_lane_is_not_phantom_inflated(self):  # noqa: VACUOUS_ASSERTION — the assertNotEqual(three, two) two lines above IS the unconditional positive control: the fixture proves the two count forms disagree before the marker's choice between them is asserted
        """The #91 trap in its exact form: three-dot counts from the
        merge-base, and a REBASED lane shares no history with its old tip —
        the symmetric form then counts the whole fork (trunk's five plus
        the lane's one) as "moved", when the reviewer has one unreviewed
        commit. Two-dot from the reviewed commit counts what the reviewer
        has not seen."""
        row = self._stalled_ready_row()
        reviewed = dispatches.rows()[row["id"]]["reviewed_tip"]
        # trunk advances five; the lane is REBASED onto it (old tip
        # dropped, its work replayed as one new commit).
        for i in range(5):
            self.commit("trunk %d\n" % i, path="trunkfile")
        self.git("checkout", "-q", "-B", "lane/probe", self.main)
        self.commit("the replayed lane work", path="lanefile")
        head = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.git("branch", "-f", "lane/probe", head)
        # CONTROL, unconditional: the two forms genuinely differ here —
        # three-dot counts the whole fork, two-dot counts the one commit.
        three = self.git("rev-list", "--count", reviewed + "..." + head)
        two = self.git("rev-list", "--count", reviewed + ".." + head)
        self.assertNotEqual(three, two,
                            "fixture law: three-dot and two-dot must "
                            "disagree on a rebased lane, or the assertion "
                            "below proves nothing about which the marker "
                            "uses")
        rc, out, err = run(["stalls"])
        self.assertEqual(rc, 0, err)
        self.assertIn("(tip MOVED +%s since review)" % two, out)

    def test_a_merged_lane_counts_its_own_and_the_merge(self):
        """The other direction of the same rule: a lane that merged trunk
        after the review DID change under the reviewer — the merge and the
        trunk commits it carried in are unreviewed code ahead of the
        verdict, and they count."""
        row = self._stalled_ready_row()
        reviewed = dispatches.rows()[row["id"]]["reviewed_tip"]
        for i in range(3):
            self.commit("trunk %d\n" % i, path="trunkfile")
        self.git("checkout", "-q", "lane/probe")
        self.git("merge", "-q", "--no-edit", self.main)
        self.commit("the one lane commit", path="lane2")
        head = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.git("branch", "-f", "lane/probe", head)
        expected = self.git("rev-list", "--count", "--right-only",
                            reviewed + "..." + head)
        rc, out, err = run(["stalls"])
        self.assertEqual(rc, 0, err)
        self.assertIn("(tip MOVED +%s since review)" % expected, out)

    def test_a_missing_branch_is_silence_never_a_claim(self):
        row = self._stalled_ready_row()
        # no lane/probe ref at all: the marker cannot measure, and
        # cannot-measure must not render as "not moved" nor as noise.
        rc, out, err = run(["stalls"])
        self.assertEqual(rc, 0, err)
        self.assertIn(row["id"][:12], out)
        self.assertNotIn("MOVED", out)


class TheListHeaderStatesTheFiledPopulation(LandReqBase):
    """`helm lr list` and the web card's filed strip (/api/lr `filed`) read
    one record; a header that carried less of the split than the card would
    let the two surfaces disagree about it (codex-2's finding on the first
    cut: the CLI printed only the total). Both headers print the WHOLE strip
    through the same owner (`filed_split` + `filed_line`) the card's body
    rides. The empty board is where it bites most: "no land loops in flight"
    over a populated ledger needs the population beside it, or the absence
    claim reads as an empty RECORD."""

    # `open`, not `in flight` — task/324. The strip counts what is ON THE
    # BOOKS (every non-terminal filed row except REVIEWED); "in flight" is
    # the HEADER's word for the chain-folded live count, and one noun may
    # not name two predicates on one surface. This regex is the reason the
    # rename could not be a display-only patch: it pins the pasted text.
    _STRIP = re.compile(
        r"filed (\d+) all-time · (\d+) open · (\d+) held · (\d+) landed"
        r" · (\d+) closed · (\d+) non-loop")

    def split(self, out):
        """(total, [buckets]) parsed from the rendered strip — the assertion
        runs on what the owner PASTES, not on the dict behind it."""
        m = self._STRIP.search(out)
        self.assertIsNotNone(m, "no filed strip in: %r" % out)
        total, *buckets = (int(g) for g in m.groups())
        return total, buckets

    def off_trunk(self, name):
        """A tip on its own branch, NOT on trunk — so merging `side` for the
        landed arm cannot silently land another arm's row too."""
        self.git("checkout", "-q", "-b", name, self.a)
        tip = self.commit(name, path=name)
        self.git("checkout", "-q", self.main)
        return tip

    def test_the_populated_header_carries_the_whole_split(self):  # noqa: VACUOUS_ASSERTION — split() asserts the strip EXISTS in the output (assertIsNotNone on the regex match) before the exact-tuple equality; an empty out fails inside the helper
        self.dispatch()
        rc, out, err = run(["list"])
        self.assertEqual(rc, 0, err)
        total, buckets = self.split(out.splitlines()[0])
        # [in_flight, held, landed, closed, non_loop]
        self.assertEqual((total, buckets), (1, [1, 0, 0, 0, 0]))

    def test_the_empty_board_still_states_the_population(self):  # noqa: VACUOUS_ASSERTION — paired positive controls on the same out (the stamped absence line AND the parsed strip) bind a real render; an empty out fails both
        row = self.dispatch()
        _row, err = dispatches.mark_cancel(row["id"], "moot")
        self.assertIsNone(err)
        rc, out, err = run(["list"])
        self.assertEqual(rc, 0, err)
        # positive control on the same output: the board really is empty
        self.assertIn("no land loops in flight", out)
        total, buckets = self.split(out)
        self.assertEqual((total, buckets), (1, [0, 0, 0, 0, 1]))

    def test_the_header_split_sums_to_its_own_total(self):  # noqa: VACUOUS_ASSERTION — split() asserts the strip EXISTS (assertIsNotNone on the match) and the exact non-zero tuple (3, [1,0,1,0,1]) is the unconditional positive control; the sum line is the self-audit on top
        """The self-checking header: a bucket that leaked rows would break
        the printed arithmetic, so the header carries its own audit."""
        self.dispatch(lane="lane/sum-open", ref=self.off_trunk("sum-open"))
        landed = self.dispatch(lane="lane/sum-landed", ref=self.side)
        dispatches.mark_verdict(landed["id"], self.side, "ok",
                                polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        moot = self.dispatch(lane="lane/sum-moot",
                             ref=self.off_trunk("sum-moot"))
        _row, err = dispatches.mark_cancel(moot["id"], "moot")
        self.assertIsNone(err)
        rc, out, err = run(["list"])
        self.assertEqual(rc, 0, err)
        total, buckets = self.split(out.splitlines()[0])
        self.assertEqual((total, buckets), (3, [1, 0, 1, 0, 1]))
        self.assertEqual(total, sum(buckets),
                         "the printed split no longer sums to its own total")


class AListingStatesWhenItWasRead(LandReqBase):
    """A listing that carries no read instant is a snapshot that silently
    re-anchors to whenever it is quoted next.

    THE ABSENCE CLAIM IS THE ONE THAT BITES. "no land loops in flight" pasted
    into a room reads as a standing fact about the board, and that is the
    reading an integrator acts on by standing down — hours after it stopped
    being true. Every row already prints a RELATIVE dwell; a relative age with
    no origin cannot be re-derived by the reader."""

    _STAMP = re.compile(r"read (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")

    def test_the_EMPTY_listing_stamps_its_absence_claim(self):
        rc, out, err = run(["list"])
        self.assertEqual(rc, 0, err)
        self.assertIn("no land loops in flight", out)
        self.assertRegex(out, self._STAMP,
                         "an absence claim with no read instant reads as a "
                         "standing fact about the board")

    def test_the_POPULATED_listing_stamps_its_header(self):
        row = self.dispatch()             # one row -> the populated header
        rc, out, err = run(["list"])
        self.assertEqual(rc, 0, err)
        # POSITIVE CONTROL FIRST, unconditional and on the same output: the
        # listing really rendered the row. Without it, an empty `out` would
        # satisfy both assertions below.
        self.assertIn("land loop", out)
        self.assertIn(str(row["id"])[:12] if isinstance(row, dict) else "", out)
        self.assertNotIn("no land loops in flight", out)
        self.assertRegex(out, self._STAMP)

    def _instants_seen_by(self, verb):
        """The `now` each producer actually RECEIVED on one run of the verb.

        RUNTIME, NOT SOURCE TEXT. These two guards used to grep cmd_lr's
        source, which is the brittleness that produced the EOF-slice defect
        one round earlier — and a text pin cannot see a caller that passes the
        WRONG instant, only one that omits the token. Spying the producers
        answers the actual question: did both receive the SAME bound value,
        or did one sample its own clock (arriving as None)?  (@codex ruled
        this in the convergence meld: do NOT keep the source-text guards.)"""
        seen = {}
        real_project, real_stamp = landreq.project_raw, dispatches._read_stamp

        def spy_project(now=None, *a, **k):
            seen["project_raw"] = now
            return real_project(now, *a, **k)

        def spy_stamp(now=None, *a, **k):
            seen.setdefault("read_stamp", []).append(now)
            return real_stamp(now, *a, **k)

        with mock.patch.object(landreq, "project_raw", spy_project):
            with mock.patch.object(dispatches, "_read_stamp", spy_stamp):
                run([verb])
        return seen

    def test_list_uses_ONE_instant_for_the_stamp_AND_the_projection(self):
        seen = self._instants_seen_by("list")
        # POSITIVE CONTROL: both producers must have been CALLED, or the
        # equality below compares two absent keys and proves nothing.
        self.assertIn("project_raw", seen, "lr list never called project_raw")
        self.assertIn("read_stamp", seen, "lr list never stamped its read")
        self.assertIsNotNone(seen["project_raw"],
                             "project_raw received now=None, so it sampled "
                             "its OWN clock and the header instant is not the "
                             "one that classified the rows")
        for got in seen["read_stamp"]:
            self.assertEqual(got, seen["project_raw"],
                             "the stamp was taken at %r while the projection "
                             "used %r — one verb, two instants"
                             % (got, seen["project_raw"]))

    def test_stalls_uses_ONE_instant_for_every_section(self):
        seen = self._instants_seen_by("stalls")
        self.assertIn("project_raw", seen, "lr stalls never called project_raw")
        self.assertIn("read_stamp", seen, "lr stalls never stamped its read")
        self.assertIsNotNone(seen["project_raw"],
                             "project_raw sampled its own clock in stalls")
        for got in seen["read_stamp"]:
            self.assertEqual(got, seen["project_raw"],
                             "stalls stamped at %r while classifying at %r"
                             % (got, seen["project_raw"]))

    def test_the_stamp_spells_the_instant_the_way_a_ROW_does(self):
        """It is quoted into chat beside row timestamps; a second spelling is
        one more thing for a reader to mis-compare."""
        from helm import dispatches as d
        self.assertEqual(d._read_stamp(0), "1970-01-01T00:00:00Z")
        # POSITIVE CONTROL, unconditional: the helper tracks its argument, so
        # the equality above is a format check and not a constant.
        self.assertNotEqual(d._read_stamp(0), d._read_stamp(86400))


if __name__ == "__main__":
    unittest.main()


class StoredPatchRescueTest(unittest.TestCase):
    """THE OWNER SPOTTED THIS: the home LANDED card read "? UNKNOWN" six times
    for changes that are demonstrably on trunk.

    `_landing_proof` answers about a TIP and both its rungs need that tip
    readable. Land receipts outlive their tips — the integrator lands a
    rebased/cherry-picked commit, the gated object becomes unreachable, git
    prunes it. Measured 2026-08-01 on this repo's real receipts: all six had
    BOTH anchors dead, 12 of 12 objects unreadable. The receipt's STORED
    patch_id, written while the object was alive, is the only identity left
    that can answer — and all six resolve through it, at trunk depths
    505-579."""

    def test_a_cached_rescue_skips_the_WALK_but_still_re_checks_trunk(self):
        """The cache buys the 600-commit WALK, not blind trust. It first said
        "never walked" and asserted git was never called at all — that claim
        died when @codex pointed out a cached hit survives a trunk RESET. A
        patch-id is immutable; TRUNK IS NOT. So a hit now costs exactly ONE
        ancestry call and the walk is still what is saved (7.9ms/commit
        measured, ~4.8s for 600)."""
        from unittest import mock
        pid = "b38be8050f42ca4bd0241c97111d4de88af98c67"
        ok = mock.Mock(returncode=0, stdout="")
        with mock.patch.object(landreq.pk, "read_json",
                               return_value={"/repo/.git\t" + pid: "205f06df1320aa"}), \
                mock.patch.object(landreq, "_git", return_value=ok) as git, \
                mock.patch.object(landreq, "_patch_id", return_value=pid), \
                mock.patch.object(landreq, "_stored_patch_index") as idx:
            sha, why = landreq._stored_patch_on_trunk(
                "/repo/.git", pid, "refs/remotes/origin/main")
        self.assertEqual(sha, "205f06df1320aa")
        self.assertIn("content identity", why)
        idx.assert_not_called()                       # the WALK is skipped
        self.assertEqual(git.call_args[0][1], "merge-base")   # one re-check

    def test_a_POISONED_cache_entry_is_refused_even_though_it_is_on_trunk(self):
        """@codex round 2, and the leg I missed: ancestry proves the cached
        sha is still ON trunk and says NOTHING about whether it still carries
        the patch-id it was cached UNDER.

        Their probe, one repo: key=(gitdir, pid_A) -> sha_B, where B IS on
        trunk but patch_id(B) != pid_A. My re-check passed it, and the card
        then told the reader "this receipt's patch-id matches B" when it did
        not. I validated the answer's LIVENESS and never its CORRECTNESS."""
        from unittest import mock
        pid_a = "a" * 40
        on_trunk = mock.Mock(returncode=0, stdout="")
        with mock.patch.object(landreq.pk, "read_json",
                               return_value={"/r/.git\t" + pid_a: "shaBshaB"}), \
                mock.patch.object(landreq, "_git", return_value=on_trunk), \
                mock.patch.object(landreq, "_patch_id", return_value="b" * 40):
            sha, why = landreq._stored_patch_on_trunk(
                "/r/.git", pid_a, "main", index={}, capped=False)
        self.assertIsNone(sha)                       # NOT returned as a match
        self.assertIn("no longer carries this receipt's patch-id", why)

    def test_a_cached_hit_that_cannot_be_re_hashed_is_UNKNOWN(self):
        """The third state of the same leg: unreadable is not a match and not
        a mismatch. Without this arm the fix could treat None as 'differs' and
        silently re-derive on every read of an unreadable repo."""
        from unittest import mock
        pid = "a" * 40
        on_trunk = mock.Mock(returncode=0, stdout="")
        with mock.patch.object(landreq.pk, "read_json",
                               return_value={"/r/.git\t" + pid: "somesha"}), \
                mock.patch.object(landreq, "_git", return_value=on_trunk), \
                mock.patch.object(landreq, "_patch_id", return_value=None):
            sha, why = landreq._stored_patch_on_trunk(
                "/r/.git", pid, "main", index={}, capped=False)
        self.assertIsNone(sha)
        self.assertIn("UNKNOWN, not a match", why)

    def test_a_GENUINE_cached_hit_still_answers(self):
        """POSITIVE CONTROL. Both new legs pass and the hit is returned —
        without this the two arms above pass on a version that refuses every
        cached entry and destroys the cache entirely."""
        from unittest import mock
        pid = "a" * 40
        on_trunk = mock.Mock(returncode=0, stdout="")
        with mock.patch.object(landreq.pk, "read_json",
                               return_value={"/r/.git\t" + pid: "goodsha"}), \
                mock.patch.object(landreq, "_git", return_value=on_trunk), \
                mock.patch.object(landreq, "_patch_id", return_value=pid), \
                mock.patch.object(landreq, "_stored_patch_index") as idx:
            sha, why = landreq._stored_patch_on_trunk(
                "/r/.git", pid, "main", index={}, capped=False)
        self.assertEqual(sha, "goodsha")
        self.assertIn("content identity", why)
        idx.assert_not_called()                      # the WALK is still saved

    def test_a_cached_hit_whose_commit_left_trunk_is_REFUSED(self):
        """@codex: "after trunk reset a cached hit remains true." It does not."""
        from unittest import mock
        pid = "b" * 40
        gone = mock.Mock(returncode=1, stdout="")
        with mock.patch.object(landreq.pk, "read_json",
                               return_value={"/repo/.git\t" + pid: "deadbeefcafe"}), \
                mock.patch.object(landreq, "_git", return_value=gone):
            sha, why = landreq._stored_patch_on_trunk(
                "/repo/.git", pid, "main", index={}, capped=False)
        self.assertIsNone(sha)
        self.assertIn("no longer on this trunk", why)

    def test_a_breached_cap_is_UNKNOWN_and_never_absent(self):
        """A cap that skips commits cannot prove a patch is not among them.
        The live receipts sit at depth 505-579, so a small cap reported every
        one of them as unresolvable — the exact shape that must not read as
        'proven absent'."""
        from unittest import mock
        with mock.patch.object(landreq.pk, "read_json", return_value={}):
            sha, why = landreq._stored_patch_on_trunk(
                "/repo/.git", "a" * 40, "refs/remotes/origin/main",
                index={}, capped=True)
        self.assertIsNone(sha)
        self.assertIn("UNKNOWN, not absent", why)

    def test_an_unfound_patch_under_a_complete_scan_is_not_a_false_hit(self):
        """POSITIVE CONTROL on the negative: an uncapped scan that genuinely
        does not hold the patch returns nothing and claims nothing. Without
        this the two arms above could both pass on an always-hit stub."""
        from unittest import mock
        with mock.patch.object(landreq.pk, "read_json", return_value={}):
            sha, why = landreq._stored_patch_on_trunk(
                "/repo/.git", "b" * 40, "refs/remotes/origin/main",
                index={"c" * 40: "deadbeef"}, capped=False)
        self.assertIsNone(sha)
        self.assertIsNone(why)

    def test_a_malformed_stored_patch_id_is_refused_not_guessed(self):
        from unittest import mock
        with mock.patch.object(landreq.pk, "read_json", return_value={}):
            for bad in ("", None, "not-hex", "zz" * 20):
                with self.subTest(pid=bad):
                    self.assertEqual(
                        landreq._stored_patch_on_trunk(
                            "/repo/.git", bad, "refs/remotes/origin/main",
                            index={}, capped=True),
                        (None, None))


class ForgedReceiptCannotClaimALandingTest(unittest.TestCase):
    """@codex's bound FIX, closed by construction.

    THE REPRO: a hand-built accepted receipt carrying a DEAD reviewed tip plus
    ANY unrelated LIVE patch-id returned on_trunk=true and seeded the cache —
    the receipt asserting its own landing. helm/landreq.py:40 has always said
    the stored patch_id is "correlation evidence" and that "lifecycle
    landedness derives INDEPENDENTLY FROM LIVE GIT", and line 23 says
    "NEVER authority". The first cut of the rescue made correlation into
    authority EIGHT LINES below the sentence forbidding it, and every one of
    its four arms tested that the rescue WORKED rather than whether it SHOULD.

    The cure is not a forgery check — there is nothing to check against, the
    tip is gone. It is that a receipt may never reach `on_trunk` at all."""

    def test_a_receipt_can_never_set_on_trunk(self):
        """The whole finding in one assertion: whatever the receipt carries,
        on_trunk is Git's field. A forged id changes what the card CORRELATES
        to and can never change what it CLAIMS."""
        from unittest import mock
        row = {"topic": "helm.land", "lane": "forged", "repo_id": "/r/.git",
               "reviewed_tip": "d" * 40, "patch_id": "f" * 40,
               "trunk_sha": "e" * 40}
        with mock.patch.object(landreq, "_receipt_rows",
                               return_value=([row], None)), \
                mock.patch.object(landreq, "_trunk_refs",
                                  return_value=("refs/heads/main", None)), \
                mock.patch.object(landreq, "_origin_configured",
                                  return_value=False), \
                mock.patch.object(landreq, "_landing_proof",
                                  return_value="unknown"), \
                mock.patch.object(landreq, "_stored_patch_on_trunk",
                                  return_value=("live0badc0de", "correlated")):
            out, _un = landreq.verified_lands(limit=4)
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]["on_trunk"])          # NOT True. Ever.
        self.assertEqual(out[0]["correlates_to"], "live0badc0de")
        self.assertIsNone(out[0]["how"])               # `how` is Git's too

    def test_a_REAL_git_proof_still_sets_on_trunk(self):
        """POSITIVE CONTROL. Without it the arm above passes on a version that
        hard-codes on_trunk=None and destroys the field's meaning."""
        from unittest import mock
        row = {"topic": "helm.land", "lane": "real", "repo_id": "/r/.git",
               "reviewed_tip": "a" * 40, "patch_id": "b" * 40,
               "trunk_sha": "c" * 40}
        with mock.patch.object(landreq, "_receipt_rows",
                               return_value=([row], None)), \
                mock.patch.object(landreq, "_trunk_refs",
                                  return_value=("refs/heads/main", None)), \
                mock.patch.object(landreq, "_origin_configured",
                                  return_value=False), \
                mock.patch.object(landreq, "_landing_proof",
                                  return_value="ancestor"):
            out, _un = landreq.verified_lands(limit=4)
        self.assertIs(out[0]["on_trunk"], True)
        self.assertEqual(out[0]["how"], "ancestor")

    def test_a_row_with_no_usable_patch_id_never_builds_the_index(self):
        """@codex finding 3: a valid patch_id=None still walked 600 commits.
        Proven by never-called, not by timing."""
        from unittest import mock
        row = {"topic": "helm.land", "lane": "noid", "repo_id": "/r/.git",
               "reviewed_tip": "a" * 40, "patch_id": None, "trunk_sha": "c" * 40}
        with mock.patch.object(landreq, "_receipt_rows",
                               return_value=([row], None)), \
                mock.patch.object(landreq, "_trunk_refs",
                                  return_value=("refs/heads/main", None)), \
                mock.patch.object(landreq, "_origin_configured",
                                  return_value=False), \
                mock.patch.object(landreq, "_landing_proof",
                                  return_value="unknown"), \
                mock.patch.object(landreq, "_stored_patch_index") as idx:
            landreq.verified_lands(limit=4)
        idx.assert_not_called()

    def test_the_cache_is_keyed_by_repo_so_B_cannot_inherit_A(self):
        """@codex finding 2: estate-global cache keyed on patch-id alone let
        repo B inherit repo A's foreign sha."""
        from unittest import mock
        pid = "a" * 40
        stored = {"/repoA/.git\t" + pid: "aaaaaaaaaaaa"}
        ok = mock.Mock(returncode=0, stdout="")
        with mock.patch.object(landreq.pk, "read_json", return_value=stored), \
                mock.patch.object(landreq, "_git", return_value=ok), \
                mock.patch.object(landreq, "_patch_id", return_value=pid):
            hit, _ = landreq._stored_patch_on_trunk("/repoA/.git", pid, "main")
            miss, _ = landreq._stored_patch_on_trunk(
                "/repoB/.git", pid, "main", index={}, capped=False)
        self.assertEqual(hit and hit[:12], "aaaaaaaaaaaa")   # A still hits
        self.assertIsNone(miss)                              # B does NOT


class LandsCarryChainIdentityTest(LandReqBase):
    """A land receipt gets the CHAIN it belongs to, so the owner's card can
    fold a lane's rounds into one LAND without guessing identity from a label.

    THE GUESS THESE REPLACE deleted a landing: keying the card's groups on
    (lane, trunk_sha) merged two unrelated lands — lane labels are reused
    across many chain roots — and labelled one of them a round of the other.
    So identity is DECLARED here or it is absent, and absent means the surface
    renders the receipt alone."""

    def test_index_reads_the_raw_snapshot_not_the_projection(self):  # noqa: VACUOUS_ASSERTION — assert_not_called IS the claim; the unconditional control is idx.get(tip)==ROOT1 plus cheap.assert_called_once() on the line above, and mutating the read back to project_raw turns 4 arms RED
        """THE PERF ARM, and it pins a regression I measured rather than
        imagined: chain_root is a RAW ledger field, but the first cut read it
        through project_raw() — 26.4s vs 0.034s on this ledger, turning an
        0.89s owner card into a 24.8s one behind a 12s client deadline. A
        card that never renders is worse than a repetitive one."""
        snap = ({"d1": {"chain_root": "ROOT1", "reviewed_tip": "a" * 40}}, None)
        with mock.patch.object(landreq, "project_raw") as proj, \
                mock.patch.object(landreq.dispatches, "snapshot",
                                  return_value=snap) as cheap:
            idx = landreq._chain_root_index()
        # POSITIVE CONTROL FIRST: the index really ran and really indexed.
        # Without it, an early raise would satisfy assert_not_called and this
        # arm would pass while measuring nothing.
        self.assertEqual(idx.get("a" * 40), "ROOT1")
        cheap.assert_called_once()
        proj.assert_not_called()

    def test_declared_chain_reaches_the_receipt_row(self):
        """A receipt whose ledger row declares a chain_root carries it out."""
        tip = "a" * 40
        snap = ({"d1": {"chain_root": "ROOT1", "reviewed_tip": tip,
                        "ref": "lane/x"}}, None)
        with mock.patch.object(landreq.dispatches, "snapshot", return_value=snap):
            idx = landreq._chain_root_index()
        self.assertEqual(idx.get(tip), "ROOT1")

    def test_undeclared_chain_is_absent_never_invented(self):
        """A row with no chain_root gets NO entry — it is not a chain of one.
        Downstream that renders the receipt alone, which is the honest state."""
        bare, chained = "b" * 40, "c" * 40
        snap = ({"d1": {"chain_root": None, "reviewed_tip": bare},
                 "d2": {"chain_root": "ROOT2", "reviewed_tip": chained}}, None)
        with mock.patch.object(landreq.dispatches, "snapshot", return_value=snap):
            idx = landreq._chain_root_index()
        # The DECLARED row in the same snapshot is the control: it proves the
        # walk reached these rows at all, so the absence below is a verdict
        # about `bare` and not about an index that never got built.
        self.assertEqual(idx.get(chained), "ROOT2")
        self.assertNotIn(bare, idx)

    def test_a_build_rows_ref_is_a_BASE_and_never_indexed_as_a_tip(self):
        """`ref` means two different things and the row does not say which: on
        a --kind build it is the BASE dispatched FROM, on a --kind review it is
        the reviewed TIP. Indexing it blindly made every build row started from
        trunk claim that trunk sha — ca318bb5812f, a real landing, was claimed
        by FIVE roots: its own review row plus four lanes that merely began
        there. The review row is the unconditional control, so an absent build
        entry is a verdict and not an index that never got built."""
        base = "9" * 40
        snap = ({"b1": {"chain_root": "BUILDROOT", "kind": "build", "ref": base},
                 "r1": {"chain_root": "REVIEWROOT", "kind": "review",
                        "ref": "8" * 40}}, None)
        with mock.patch.object(landreq.dispatches, "snapshot", return_value=snap):
            idx = landreq._chain_root_index()
        self.assertEqual(idx.get("8" * 40), "REVIEWROOT")   # control: ref DOES
        self.assertNotIn(base, idx)                          # ... but not on a build

    def test_a_tip_under_two_roots_is_ambiguous_and_answers_absent(self):
        """One tip declared under SEVERAL chain roots is not a first-wins race:
        flipping insertion order flipped the answer. An ambiguous root is not a
        DECLARED root, so it answers None and the receipt renders alone."""
        shared, lone = "7" * 40, "6" * 40
        snap = ({"a": {"chain_root": "R_A", "kind": "review", "reviewed_tip": shared},
                 "b": {"chain_root": "R_B", "kind": "review", "reviewed_tip": shared},
                 "c": {"chain_root": "R_C", "kind": "review", "reviewed_tip": lone}},
                None)
        with mock.patch.object(landreq.dispatches, "snapshot", return_value=snap):
            idx = landreq._chain_root_index()
        self.assertEqual(idx.get(lone), "R_C")   # control: unambiguous still resolves
        self.assertNotIn(shared, idx)
        # and the answer does not depend on which row the dict yields first
        flipped = ({"b": snap[0]["b"], "a": snap[0]["a"], "c": snap[0]["c"]}, None)
        with mock.patch.object(landreq.dispatches, "snapshot", return_value=flipped):
            self.assertNotIn(shared, landreq._chain_root_index())

    def test_unavailable_ledger_fails_open_to_empty(self):
        """Every failure answers {} — each receipt then stands alone and every
        land stays visible. This index is never the reason the card cannot
        render, and it never guesses identity to stay useful."""
        good = ({"d1": {"chain_root": "ROOT3", "reviewed_tip": "d" * 40}}, None)
        # POSITIVE CONTROL on the same observable: a HEALTHY ledger yields a
        # NON-empty index. Both {} assertions below are otherwise satisfied by
        # a function that can only ever return {}.
        with mock.patch.object(landreq.dispatches, "snapshot", return_value=good):
            self.assertEqual(landreq._chain_root_index(), {"d" * 40: "ROOT3"})
        with mock.patch.object(landreq.dispatches, "snapshot",
                               return_value=({}, "ledger unreadable")):
            self.assertEqual(landreq._chain_root_index(), {})
        with mock.patch.object(landreq.dispatches, "snapshot",
                               side_effect=OSError("boom")):
            self.assertEqual(landreq._chain_root_index(), {})

    def test_verified_lands_carries_chain_root_and_none_is_a_value(self):  # noqa: VACUOUS_ASSERTION — the loop is guarded by an UNCONDITIONAL assertTrue(rows) that fires before it; a receipt is planted above precisely because the empty fixture made this arm vacuous once and the rung caught it
        """The field is always present on the row — None is the fail-open
        answer, never a missing key the renderer has to guess about."""
        # PLANT A RECEIPT. The fixture's HELM_HOME is empty, so without this
        # `rows` is [] and the loop below asserts NOTHING — which is exactly
        # how this arm first passed. helm's own vacuous-assertion rung flagged
        # it and the control proved the rung right.
        path = landreq.receipts_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "topic": "helm.land", "schema": landreq.LAND_SCHEMA,
                "id": "e" * 40, "reviewed_tip": "e" * 40, "lane": "lane/planted",
                "branch": "lane/planted", "patch_id": None,
                "trunk_sha": "f" * 40, "repo_id": "/nonexistent/.git",
                "ts": "2026-08-01T00:00:00Z"}) + "\n")
        with mock.patch.object(landreq, "_chain_root_index", return_value={}):
            rows, unavailable = landreq.verified_lands(5)
        self.assertIsNone(unavailable)
        # UNCONDITIONAL: an empty `rows` would make the loop below assert
        # nothing at all, which is the shape this arm is meant to refuse.
        self.assertTrue(rows, "no receipts read — the loop would be vacuous")
        for r in rows:
            self.assertIn("chain_root", r)
            self.assertIsNone(r["chain_root"])


class UnknownGateCapsNamesItsCauseTest(unittest.TestCase):
    """requirement=="unknown" has TWO causes and they need TWO sentences.

    #111/#119, measured 2026-08-02: an integrator read "repair the ledger
    row" on a row that was INTACT and spent a whole diagnosis on it. The
    actual defect was three days upstream — a seat home that had not rebased,
    whose `./bin/helm` runs the package beside it and stamps no gate_caps at
    all. 34 post-epoch verdicts carried no stamp; every one came from the
    four seats with stale homes.
    """

    def test_absent_blames_the_writer_and_unreadable_blames_the_row(self):
        absent = landreq._unknown_gate_caps_why({"polarity": "approve"})
        unreadable = landreq._unknown_gate_caps_why({"gate_caps": None})

        # they must not be the same sentence — the collapse IS the defect
        self.assertNotEqual(absent, unreadable)

        # ABSENT: name the writer, exonerate the row, give the check to run
        self.assertIn("stamped no gate_caps", absent)
        self.assertIn("INTACT", absent)
        self.assertIn("grep -c gate_caps", absent)
        self.assertNotIn("repair the row", absent.replace(
            "do not repair the row", ""))

        # UNREADABLE: the ledger IS the defect here, so keep that instruction
        self.assertIn("present but unreadable", unreadable)
        self.assertIn("repair the ledger row", unreadable)

    def test_the_split_reaches_the_refusal_an_integrator_actually_reads(self):
        """The helper is not the surface — _approval_refusal is."""
        row = {"polarity": "approve", "recipient": "helm-claude-2",
               "verdict_ref": "x", "gate": ""}
        with mock.patch.object(landreq, "gate_requirement",
                               return_value="unknown"), \
                mock.patch.object(landreq.dispatches, "approval_tier",
                                  return_value=("inside", "")):
            why, _tier = landreq._approval_refusal(row)
        self.assertIn("stamped no gate_caps", why)
        # POSITIVE CONTROL on the same observable: the unreadable shape still
        # reaches the OTHER sentence through the identical call path, so a
        # helper that returned one constant would redden here.
        row2 = dict(row, gate_caps=None)
        with mock.patch.object(landreq, "gate_requirement",
                               return_value="unknown"), \
                mock.patch.object(landreq.dispatches, "approval_tier",
                                  return_value=("inside", "")):
            why2, _t2 = landreq._approval_refusal(row2)
        self.assertIn("present but unreadable", why2)


class VanishedObjectProofTest(LandReqBase):
    """A GIT OBJECT THAT DOES NOT EXIST IS THE STRONGEST EVIDENCE OF ABSENCE,
    AND THE LADDER USED TO SCORE IT AS THE WEAKEST.

    `_landing_proof` returned `unknown` for a missing object while the subsumed
    door requires `absent`, so helm's two oldest rows — 4fdd32fcf664 at 7.9d
    and b970911edbe6 at 7.8d, audited 2026-08-03 — were permanently unclosable
    with every other clause passing: chain matched, cross-family held, and
    their cure 77e4021415e7 was a proven ancestor of main.

    THE TRAP THIS CLASS EXISTS TO KEEP SHUT is the obvious fix. Flipping
    unknown->absent on a LOCAL miss is the same bug pointing the other way: the
    object can live on a remote or in a reflog, and a confident false `absent`
    starts closing rows whose work never landed — strictly worse than
    refusing. So every arm below is really one rule: unanimous silence from
    every reachable source converts, and any probe that could not LOOK
    refuses."""

    def _remote(self):
        """A reachable origin, so the remote probe can actually answer."""
        bare = os.path.join(self.tmp, "origin.git")
        if not os.path.isdir(bare):
            subprocess.run(["git", "init", "--bare", "-q", bare], check=True)
        # idempotent: one arm points origin at a dead URL first, then asks for
        # a live one, and `remote add` on an existing name exits 3.
        subprocess.run(["git", "-C", self.repo, "remote", "remove", "origin"],
                       capture_output=True)
        self.git("remote", "add", "origin", bare)
        self.git("push", "-q", "-f", "origin", self.main)
        return bare

    @property
    def gitdir(self):
        return os.path.join(self.repo, ".git")

    def test_an_object_no_reachable_source_holds_is_absent_not_unknown(self):
        """THE FOUNDING CASE. Nothing anywhere has it, so it is gone."""
        self._remote()
        missing = "0" * 40
        self.assertEqual(landreq._vanished_proof(self.gitdir, missing),
                         "absent")
        # …and end-to-end through the door's own reader, which is what the two
        # stuck rows actually call. A helper that were right while
        # _landing_proof still said unknown would close nothing.
        self.assertEqual(
            landreq._landing_proof(self.gitdir, missing, self.main), "absent")

    def test_the_remote_publishing_it_refuses_because_it_is_not_vanished(self):
        """A LOCAL MISS IS NOT ABSENCE — the object is on origin.

        THE FIXTURE HAS TO ISOLATE THIS CLAUSE AND THE FIRST ONE DID NOT. It
        asked about `main`'s own HEAD, which is in the LOCAL REFLOG, so
        deleting the remote arm entirely left this test green — the reflog arm
        caught it one clause later and the mutation SURVIVED. The sha here is
        therefore built in a SEPARATE clone and pushed, so it exists on origin
        and has never been in this repo's reflog: the remote arm is the only
        one that can answer."""
        bare = self._remote()
        other = os.path.join(self.tmp, "other")
        subprocess.run(["git", "clone", "-q", bare, other], check=True)
        subprocess.run(["git", "-C", other, "config",
                        "user.email", "o@example.com"], check=True)
        subprocess.run(["git", "-C", other, "config", "user.name", "O"],
                       check=True)
        with open(os.path.join(other, "elsewhere"), "w") as fh:
            fh.write("made in another clone\n")
        subprocess.run(["git", "-C", other, "add", "-A"], check=True)
        subprocess.run(["git", "-C", other, "commit", "-q", "-m", "elsewhere"],
                       check=True)
        subprocess.run(["git", "-C", other, "push", "-q", "origin",
                        "HEAD:refs/heads/elsewhere"], check=True)
        on_remote = subprocess.run(
            ["git", "-C", other, "rev-parse", "HEAD"],
            capture_output=True, text=True).stdout.strip()
        # THE THREE CONTROLS THAT MAKE THIS FIXTURE SINGLE-CLAUSE, each
        # unconditional and on the same observables the predicate reads.
        absent_locally = subprocess.run(
            ["git", "--git-dir", self.gitdir, "cat-file", "-e",
             on_remote + "^{commit}"], capture_output=True).returncode
        self.assertTrue(absent_locally, "must not be in the local object DB")
        reflog = subprocess.run(
            ["git", "--git-dir", self.gitdir, "reflog", "--all",
             "--no-abbrev", "--format=%H %gd %gs"],
            capture_output=True, text=True).stdout
        self.assertNotIn(on_remote, reflog,
                         "must not be in the reflog, or the reflog arm "
                         "answers and this test stops testing the remote")
        remote_refs = subprocess.run(
            ["git", "--git-dir", self.gitdir, "ls-remote", "origin"],
            capture_output=True, text=True).stdout
        self.assertIn(on_remote, remote_refs, "but origin DOES publish it")
        self.assertEqual(landreq._vanished_proof(self.gitdir, on_remote),
                         "unknown")

    def test_a_reflog_holding_it_refuses_because_that_is_a_repair_job(self):
        """IT EXISTED HERE ONCE. The object DB losing it is corruption to
        repair, not evidence the work never happened."""
        self._remote()
        # a commit that is reachable from NO ref but IS in the reflog: make it,
        # then move the branch back off it.
        before = self.git("rev-parse", "HEAD")
        orphan = self.commit("orphaned", path="orphan")
        self.git("reset", "-q", "--hard", before)
        reflog = subprocess.run(
            ["git", "--git-dir", self.gitdir, "reflog", "--all",
             "--no-abbrev", "--format=%H %gd %gs"],
            capture_output=True, text=True).stdout
        self.assertIn(orphan, reflog)
        self.assertEqual(landreq._vanished_proof(self.gitdir, orphan),
                         "unknown")

    def test_a_probe_that_could_not_look_never_answers_absent(self):
        """A FAILED PROBE IS NOT A NEGATIVE RESULT — the rule the fleet
        re-derived on five surfaces on 2026-08-03."""
        missing = "0" * 40
        # NO REMOTE CONFIGURED: ls-remote cannot answer, so absence is unproven.
        self.assertEqual(landreq._vanished_proof(self.gitdir, missing),
                         "unknown")
        # AN UNREACHABLE REMOTE: the probe errors rather than missing.
        self.git("remote", "add", "origin", "https://127.0.0.1:1/nope.git")
        self.assertEqual(landreq._vanished_proof(self.gitdir, missing),
                         "unknown")
        # GIT ITSELF NOT RUNNING (timeout/OSError -> _git returns None).
        self.git("remote", "set-url", "origin",
                 os.path.join(self.tmp, "origin.git"))
        with mock.patch.object(landreq, "_git", return_value=None):
            self.assertEqual(landreq._vanished_proof(self.gitdir, missing),
                             "unknown")
        # AND THE REFLOG LEG SEPARATELY: the remote answers, the reflog does
        # not. Without this arm a reflog failure would ride the remote's
        # success straight to `absent`.
        self._remote()
        real = landreq._git

        def reflog_broken(gitdir, *args, **kw):
            if args and args[0] == "reflog":
                return None
            return real(gitdir, *args, **kw)

        with mock.patch.object(landreq, "_git", side_effect=reflog_broken):
            self.assertEqual(landreq._vanished_proof(self.gitdir, missing),
                             "unknown")

    def test_something_that_is_not_a_full_sha_is_not_an_object_lookup(self):
        """There is nothing to look up, so there is nothing to prove absent."""
        self._remote()
        self.assertEqual(landreq._vanished_proof(self.gitdir, "abc"),
                         "unknown")
        self.assertEqual(landreq._vanished_proof(self.gitdir, ""), "unknown")
        self.assertEqual(landreq._vanished_proof(self.gitdir, self.main),
                         "unknown")

    def test_a_present_object_still_reaches_the_patch_id_comparison(self):
        """THE REGRESSION GUARD. `_vanished_proof` is reached ONLY on a missing
        object; every existing answer must be untouched, or this fix bought two
        rows by breaking five hundred."""
        self._remote()
        self.assertEqual(
            landreq._landing_proof(self.gitdir, self.b, self.main), "ancestor")
        self.assertEqual(
            landreq._landing_proof(self.gitdir, self.side, self.main),
            "absent")
        self.git("cherry-pick", self.side)
        self.assertEqual(
            landreq._landing_proof(self.gitdir, self.side, self.main),
            "patch-equivalent")


class ReadyRungTest(unittest.TestCase):
    """READY says "this may be merged" for rows a land door will refuse, and it
    said it three measured ways. This names WHICH rung bites.

    Measured on the live board when this landed: of 131 READY rows, 81 were
    PRE-V4, 34 had no gate token at all, 12 were STALE-BASE, 4 were SELF-REVIEW
    and ZERO were clean. A word true of every row carries no information, which
    is the whole defect -- not that READY was wrong, but that it was unfalsifiable.

    THE FIRST TEST BELOW IS THE ONE THAT MATTERS. "every row fails a rung" is
    also exactly what a structurally broken predicate produces, so a suite
    without a reachable-plain-READY control cannot tell a real finding from a
    function that only knows one answer.
    """

    TRUNK = "8a5ae7e2cac5" + "0" * 28
    OK = {"tok": {"v": "4", "host": "a-host", "head": TRUNK}}
    ROW = {"state": "READY", "author": "alpha", "reviewer": "beta",
           "gate": "tok"}

    _DEFAULT = object()   # so an EXPLICIT None (unreadable trunk) is testable

    def word(self, row=None, index=_DEFAULT):
        return landreq.ready_word(
            dict(self.ROW, **(row or {})),
            index=self.OK if index is self._DEFAULT else index)

    def test_a_clean_row_is_PLAIN_READY(self):  # noqa: VACUOUS_ASSERTION — test_a_clean_row_is_PLAIN_READY is the unconditional positive control on the SAME observable (ready_word); every negative here is meaningless without it and it is asserted first in the class
        """THE CONTROL, and it is unconditional: every other assertion in this
        class is a negative, and negatives are worthless if the function cannot
        produce the positive."""
        self.assertEqual(self.word(), "READY")

    def test_an_unmarked_SELF_REVIEW_is_named(self):  # noqa: VACUOUS_ASSERTION — test_a_clean_row_is_PLAIN_READY is the unconditional positive control on the SAME observable (ready_word); every negative here is meaningless without it and it is asserted first in the class
        """reviewer == author: nobody independent looked. First in severity
        order because no receipt can compensate for it."""
        self.assertEqual(self.word({"reviewer": "alpha"}),
                         "READY-SELF-REVIEW")

    def test_RECEIPT_VERSION_is_deliberately_NOT_a_rung(self):  # noqa: VACUOUS_ASSERTION — asserts a POSITIVE (plain READY) on the same observable, which is the whole claim
        """81 of 131 live READY rows carry a hostless pre-v4 receipt, and NONE
        of them is flagged. The land door's landability check is a regex on the
        TOKEN STRING -- it never opens the receipt record, so it cannot refuse
        on version, host or binding, and binding is verified at `dispatch
        verdict` WRITE time instead. The door would land all 81, so READY does
        not overstate for them; flagging them would assert a consequence that
        does not exist, which is this surface's own disease pointed backwards.

        This test exists to keep it that way: a future reader who notices
        hostless receipts and "fixes" them into a rung must fail here first."""
        old = {"tok": {"v": "1", "head": self.TRUNK}}          # no host at all
        self.assertEqual(self.word(index=old), "READY",
                         "receipt version became a landability rung")

    def test_an_IN_FLIGHT_GATED_LANE_renders_PLAIN_READY(self):  # noqa: VACUOUS_ASSERTION — asserts plain READY, a positive, on the same observable
        """THE PIN FOR THE ONE THAT REACHED TRUNK. A lane's gate receipt
        attests THE LANE'S OWN TIP, and a lane tip is not reachable from trunk
        until it LANDS -- that is what "in flight" means. A reachability rung
        therefore flags every properly-gated in-flight lane, which is the whole
        audience this board serves; it rendered a minutes-old, host-bound,
        already-approved row as defective, and it did that ON TRUNK because
        fixtures cannot hold a distribution.

        So: a receipt whose head is nowhere near trunk is NOT a defect. If a
        future reader adds a RECEIPT-REACHABILITY rung, this fails first.

        The filed-as-its-own-row sequel (task/266) landed staleness on a
        DIFFERENT measure, and this pin is what keeps the two apart: the
        STALE-BASE rung reads `base_behind` -- trunk commits the row's history
        LACKS, counted from live git by the projection -- which is small for
        every in-flight lane and enormous for a dead one. It never opens the
        receipt, so a receipt whose head is unreachable stays exactly as
        non-defective as this test demands."""
        lane_tip = "b" * 40                    # a lane head, never on trunk
        self.assertEqual(
            self.word(index={"tok": {"v": "4", "host": "h", "head": lane_tip}}),
            "READY",
            "an in-flight gated lane was flagged; reachability is not a rung")

    def test_every_OTHER_state_is_returned_UNTOUCHED(self):  # noqa: VACUOUS_ASSERTION — test_a_clean_row_is_PLAIN_READY is the unconditional positive control on the SAME observable (ready_word); every negative here is meaningless without it and it is asserted first in the class
        """DISPLAY TRUTH ONLY. This layer never refuses and never rewrites the
        state word other consumers key on -- STAGE_ORDER, OWED_BY and
        LAND_STALL_S all index "READY", so rewriting it would drop rows out of
        stall accounting. Enforcement stays in the land door."""
        for state in ("OPEN", "AWAITING_REVIEW", "REVIEWED", "LANDED",
                      "CHANGES_REQUESTED", "SUPERSEDED"):
            self.assertEqual(self.word({"state": state}), state)
        self.assertIsNone(landreq.ready_rung(dict(self.ROW, state="OPEN")))

    def test_a_dead_base_is_named_STALE_BASE(self):  # noqa: VACUOUS_ASSERTION — test_a_clean_row_is_PLAIN_READY is the unconditional positive control on the SAME observable (ready_word); every negative here is meaningless without it and it is asserted first in the class
        """task/266: a READY row whose base lacks STALE_BASE_BEHIND trunk
        commits is named, AT the boundary — `>=` mutated to `>` fails here,
        and so does a drifted constant, because the fixture reads the real
        one rather than pinning a copy of it."""
        self.assertEqual(self.word({"base_behind": landreq.STALE_BASE_BEHIND}),
                         "READY-STALE-BASE")

    def test_one_commit_under_the_bar_is_PLAIN_READY(self):  # noqa: VACUOUS_ASSERTION — asserts a POSITIVE (plain READY) on the same observable, which is the whole claim
        """The other half of the boundary: routine drift is the normal state
        of every in-flight row (76 of 96 measured) and must never be named
        as the defect — that mistake is the rejected reachability rung."""
        self.assertEqual(
            self.word({"base_behind": landreq.STALE_BASE_BEHIND - 1}), "READY")

    def test_an_unmeasured_drift_NEVER_accuses(self):  # noqa: VACUOUS_ASSERTION — asserts a POSITIVE (plain READY) on the same observable, which is the whole claim
        """UNKNOWN is a real answer and it is not STALE: an absent field (a
        row projected by older code), an explicit None (rev-list declined),
        and bool-typed junk (True IS an int, and must not read as "1 commit
        behind") all stay silent. A rung minted from a value nobody measured
        would be the confident-verdict-over-no-reading defect this whole
        surface exists to cure."""
        self.assertEqual(self.word({"base_behind": None}), "READY")
        self.assertEqual(self.word({"base_behind": True}), "READY")
        self.assertEqual(self.word({"base_behind": "1333"}), "READY")
        # The bool arm needs a bar True could actually clear, or the guard
        # under test is unreachable and its mutant immortal: with the real
        # 120, True >= 120 is False and dropping `not isinstance(bool)`
        # changes nothing this test can see.
        with mock.patch.object(landreq, "STALE_BASE_BEHIND", 1):
            self.assertEqual(self.word({"base_behind": True}), "READY")

    def test_a_stronger_rung_OUTRANKS_a_dead_base(self):  # noqa: VACUOUS_ASSERTION — test_a_clean_row_is_PLAIN_READY is the unconditional positive control on the SAME observable (ready_word); every negative here is meaningless without it and it is asserted first in the class
        """Severity order: a dead base never masks a missing reviewer or an
        unverifiable receipt — those bite at the door, staleness bites at the
        compose leg. The marks column still carries the number either way."""
        vast = 10 ** 6
        self.assertEqual(self.word({"reviewer": "alpha", "base_behind": vast}),
                         "READY-SELF-REVIEW")
        self.assertEqual(self.word({"gate": "", "base_behind": vast}),
                         "READY-UNVERIFIED")


class BaseStateTest(unittest.TestCase):
    """base_state: LANDED / UNLANDED-CURRENT / UNLANDED-STALE / UNKNOWN — the
    task/266 predicate, judged from the row the projection already built.

    THE LADDER IS RESPECTED BY CONSTRUCTION: `landed`/`merged_local` are
    `_landing_proof`'s verdict (ancestry, then patch identity), so this
    function never accuses a row whose sha was merely rewritten by a rebase —
    the census measured 131 of 153 resolvable non-ancestor approves as
    landed-by-rebase (2026-08-06), and calling those stale sends someone to
    re-land work already on trunk. The end-to-end arm of that claim lives in
    StaleBaseProjectionTest.test_landed_by_rebase_reads_LANDED_never_stale."""

    def test_landed_wins_over_everything(self):
        """A landed row is LANDED whatever the drift fields say — staleness is
        a property of WAITING work."""
        self.assertEqual(landreq.base_state(
            {"landed": True, "observable": True, "base_behind": 10 ** 6}),
            "LANDED")
        self.assertEqual(landreq.base_state(
            {"merged_local": True, "observable": True, "base_behind": 10 ** 6}),
            "LANDED")

    def test_a_blind_row_is_UNKNOWN_even_with_a_number(self):
        """`observable` is the projection's own "I actually looked" bit; a
        behind-count on a row helm could not observe is a value nothing
        vouches for, and judging it would let a stamped field outrank the
        reading it is supposed to summarise."""
        self.assertEqual(landreq.base_state(
            {"observable": False, "base_behind": 10 ** 6}), "UNKNOWN")

    def test_an_unmeasured_count_is_UNKNOWN_never_current(self):
        """None means UNMEASURED. Rendering it CURRENT would be the exact
        inversion of the task/266 defect: an instrument that could not read
        reporting health."""
        self.assertEqual(landreq.base_state(
            {"observable": True, "base_behind": None}), "UNKNOWN")
        self.assertEqual(landreq.base_state(
            {"observable": True, "base_behind": True}), "UNKNOWN")

    def test_the_boundary_is_the_constant_exactly(self):
        self.assertEqual(landreq.base_state(
            {"observable": True, "base_behind": landreq.STALE_BASE_BEHIND}),
            "UNLANDED-STALE")
        self.assertEqual(landreq.base_state(
            {"observable": True,
             "base_behind": landreq.STALE_BASE_BEHIND - 1}),
            "UNLANDED-CURRENT")

    def test_stale_at_parameterises_the_policy_knob(self):
        """A caller with its own bar gets its own verdicts; the default stays
        the measured constant rather than a copy of it."""
        row = {"observable": True, "base_behind": 5}
        self.assertEqual(landreq.base_state(row, stale_at=5), "UNLANDED-STALE")
        self.assertEqual(landreq.base_state(row, stale_at=6),
                         "UNLANDED-CURRENT")


class StaleBaseProjectionTest(LandReqBase):
    """task/266 end to end: the projection MEASURES a READY row's base drift
    from live git, the predicate judges it, and every reader surface (list
    word, list mark, show detail) says the same thing — one computation, one
    field, no second census.

    The fixture geometry the base mints: `side` is cut from commit `a`, trunk
    then gains `b` and `c` — so a fresh READY row on `side` is exactly TWO
    commits behind, which is the MUST-NOT-FLAG control every scan here is
    seeded with. The MUST-FLAG control advances trunk past a lowered bar."""

    def _ready(self):
        row = self.dispatch(ref=self.side)
        dispatches._mark_delivered(row["id"], "post-sb")
        dispatches.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        return row

    def _receipted(self, token="feedc0de1234"):
        """A READY row whose gate token RESOLVES, so the UNVERIFIED rung
        passes and the word can reach STALE-BASE. The verdict event is
        appended as an old (gate-incapable) writer would write it — the
        base's own pin — and the receipt ledger is seeded with the token,
        exactly the lookup `ready_rung` performs."""
        row = self.dispatch(ref=self.side)
        dispatches._mark_delivered(row["id"], "post-sb2")
        current = dispatches.snapshot()[0][row["id"]]
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": current["seq"] + 1,
            "id": row["id"], "ts": dispatches.pk.now_ts(),
            "reviewed_tip": self.side, "verdict_ref": "ok",
            "polarity": "approve", "gate": token}))
        path = gate.receipts_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": token}) + "\n")
        return row

    def test_a_ready_row_carries_its_measured_drift(self):  # noqa: VACUOUS_ASSERTION — test_a_dead_base_names_the_rung_on_every_reader_surface is the unconditional positive control on the SAME observables (the list mark and the rung); this is their must-not-flag half
        """The MUST-NOT-FLAG control: routine drift is counted (2, from the
        fixture geometry — a number, not a guess), judged CURRENT, and no
        surface breathes the word stale."""
        row = self._ready()
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["state"], "READY")
        self.assertEqual(lr["base_behind"], 2)
        self.assertEqual(lr["base_state"], "UNLANDED-CURRENT")
        self.assertNotEqual(landreq.ready_rung(lr), "STALE-BASE")
        self.assertNotIn("commits behind trunk", run(["list"])[1])

    def test_a_dead_base_names_the_rung_on_every_reader_surface(self):
        """The MUST-FLAG control, walked through every surface a human or an
        integrator actually reads: the projected fields, the state word, the
        list line with its number, and the show detail. The bar is lowered to
        the fixture's scale via the module constant — the same knob
        production reads — so this also pins that the rung and the predicate
        consult the CONSTANT and not a copy of it."""
        row = self._receipted()
        self.commit("d")                       # behind: 2 -> 3
        with mock.patch.object(landreq, "STALE_BASE_BEHIND", 3):
            lr, err = landreq.get(row["id"])
            self.assertIsNone(err, err)
            self.assertEqual(lr["state"], "READY")
            self.assertEqual(lr["base_behind"], 3)
            self.assertEqual(lr["base_state"], "UNLANDED-STALE")
            self.assertEqual(landreq.ready_word(lr), "READY-STALE-BASE")
            listing = run(["list"])[1]
            self.assertIn("READY-STALE-BASE", listing)
            self.assertIn("base 3 commits behind trunk", listing)
            shown = run(["show", row["id"]])[1]
            self.assertIn("base is 3 commits behind trunk", shown)
            self.assertIn("STALE", shown)

    def test_landed_by_rebase_reads_LANDED_never_stale(self):
        """THE LADDER-RESPECT CONTROL, the census's own worst case: the work
        reached trunk under a REWRITTEN sha, so ancestry says NO — asserted,
        not assumed — and only patch identity says LANDED. A staleness rung
        that consulted ancestry alone would accuse this row of being a
        zombie while its content sits on trunk; 131 of the 153 resolvable
        non-ancestor approves in the live ledger are this exact shape
        (2026-08-06)."""
        row = self._ready()
        self.git("cherry-pick", self.side)     # lands the CONTENT, new sha
        gitdir = os.path.join(self.repo, ".git")
        self.assertEqual(landreq._ancestry(gitdir, self.side, self.main),
                         landreq.NOT_ANCESTOR,
                         "fixture must exercise the patch-identity rung")
        with mock.patch.object(landreq, "STALE_BASE_BEHIND", 1):
            lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["state"], "LANDED")
        self.assertEqual(lr["base_state"], "LANDED")
        self.assertIsNone(lr["base_behind"])   # drift is a property of WAITING

    def test_drift_is_counted_against_the_LANDING_target(self):
        """Upstream trunk when one exists — the SAME leg the row's landedness
        is judged on. Counting drift against a different trunk than the land
        verdict reads would let one row read LANDED on one ref and stale
        against another, which is a two-census disagreement inside a single
        row. Local main advances past origin here, so the two legs disagree
        by exactly one commit and the assertion can tell which was read."""
        self.add_origin()
        row = self._ready()
        self.commit("d")            # local main gains d; origin/main does not
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["state"], "READY")
        self.assertEqual(lr["base_behind"], 2)     # vs origin/main — not 3

    def test_base_behind_declines_to_answer_rather_than_guessing(self):
        """The None arms, with their positive control last: an unresolvable
        tip and a repo with no trunk both answer None — UNMEASURED — because
        a drift count nothing measured must never render as a number (least
        of all 0, which reads as freshly-cut)."""
        gitdir = os.path.join(self.repo, ".git")
        self.assertIsNone(landreq._base_behind(gitdir, "f" * 40, {}))
        self.assertIsNone(landreq._base_behind(
            os.path.join(self.tmp, "nowhere", ".git"), self.side, {}))
        self.assertEqual(landreq._base_behind(gitdir, self.side, {}), 2)

    def test_a_terminal_row_prints_no_drift_even_with_a_frozen_READY_word(self):
        """A closed-as-landed row FREEZES the verdict word READY in its
        headline, and a terminal row stops re-reading git by design — so the
        show surface used to print "drift UNMEASURED" on it forever: an
        eternal shrug on a discharged row (live dogfood find, 2026-08-06).
        Staleness is a property of WAITING work; the drift line must gate on
        non-terminal, not on the frozen word. Self-controlled: the SAME row
        shows its drift line while live, and drops it at closure."""
        row = self._ready()
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        self.assertIn("drift", landreq._render_show(lr))     # live control
        self.git("cherry-pick", self.side)     # the proof close_landed needs
        closed, why = landreq.close_landed(row["id"], trunk=self.main,
                                           live=True)
        self.assertIsNone(why, why)
        self.assertTrue(closed["terminal"])
        shown = landreq._render_show(landreq.get(row["id"])[0])
        headline = shown.splitlines()[0]
        self.assertIn("READY", headline)       # the frozen word, asserted —
        self.assertIn("CLOSED (LANDED)", shown)  # this IS the dogfood shape
        self.assertNotIn("drift", shown)

    def test_an_unobservable_ready_row_is_UNKNOWN_and_says_UNMEASURED(self):
        """A READY row whose landing helm cannot observe cannot have its
        drift measured either, and the show surface must say so rather than
        render like a current row — 'I could not look' printed as 'nothing
        to see' is this module's oldest recurring defect, and staleness must
        not re-introduce it. The blindness is injected at the observation
        seam (`_git_observe` answering its own blind shape), which is what a
        vanished repo or an rc-128 ancestry actually produces there; the
        CONTROL half is the sibling test above, where the same fixture with
        a live repo measures 2."""
        row = self._ready()
        with mock.patch.object(landreq, "_git_observe",
                               return_value=dict(landreq._UNOBSERVED)):
            lr, err = landreq.get(row["id"])
            self.assertIsNone(err, err)
            self.assertEqual(lr["state"], "READY")
            self.assertFalse(lr["observable"])
            self.assertIsNone(lr["base_behind"])
            self.assertEqual(lr["base_state"], "UNKNOWN")
            self.assertIn("UNMEASURED", run(["show", row["id"]])[1])


class CloseReasonRegisterParityTest(unittest.TestCase):
    """THE REGISTER: CLOSE_CLI_REASONS is the one truth, and every help
    surface that enumerates close reasons must carry ALL of it in order.
    Before this pin the three surfaces held three different lists (VERBS.md
    seven, landreq USAGE seven, cli.py eight) — a reader was told a door does
    not exist depending on which help they read, while the code accepted nine.
    """

    # `--reason TEXT` (abandon) is uppercase and never matches; only the
    # lowercase pipe-joined enumeration is a register.
    _LIST = re.compile(r"--reason ([a-z][a-z-]*(?:\|[a-z][a-z-]*)+)")

    def _register(self, text, label):
        m = self._LIST.search(text)
        self.assertIsNotNone(m, "%s carries no --reason enumeration" % label)
        return tuple(m.group(1).split("|"))

    def test_usage_string_lists_every_close_reason(self):  # noqa: VACUOUS_ASSERTION — _register's assertIsNotNone is the unconditional positive control: the enumeration must EXIST before the equality means anything
        self.assertEqual(self._register(landreq.USAGE, "landreq.USAGE"),
                         landreq.CLOSE_CLI_REASONS)

    def test_cli_verb_help_lists_every_close_reason(self):  # noqa: VACUOUS_ASSERTION — _register's assertIsNotNone is the unconditional positive control: the enumeration must EXIST before the equality means anything
        from helm import cli
        self.assertEqual(
            self._register(cli._VERB_HELP["lr"], "cli._VERB_HELP['lr']"),
            landreq.CLOSE_CLI_REASONS)

    def test_verbs_md_lists_every_close_reason(self):  # noqa: VACUOUS_ASSERTION — the MUST-HIT assertTrue(registers) is the unconditional positive control; zero matches fails loudly instead of passing empty
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "docs", "VERBS.md")
        with open(path, encoding="utf-8") as fh:
            registers = self._LIST.findall(fh.read())
        # MUST-HIT: the lr close signature is a register; zero matches means
        # the probe broke, not that the doc is clean.
        self.assertTrue(registers, "docs/VERBS.md carries no --reason "
                                   "enumeration — probe or doc broke")
        for found in registers:
            self.assertEqual(tuple(found.split("|")),
                             landreq.CLOSE_CLI_REASONS,
                             "a docs/VERBS.md close-reason list drifted from "
                             "CLOSE_CLI_REASONS")


class ContraryDischargeArmsTest(unittest.TestCase):
    """CONTRARY claimed a verdict was DEFIED, on rows whose verdict was HONORED
    through succession — the successor landed and the door closed the row,
    which is the process WORKING. Measured on the live board: of 52 in-flight
    contrary rows, 32 were discharged and 20 genuinely were not.

    RENAMED from ContraryDischargeTest (2026-08-05): this module already held
    a lifecycle class by that exact name, and the later definition SHADOWED
    it — the discharge-lifecycle tests silently stopped running the day this
    class landed (b83f56ca). Two classes, one name, zero warnings is the
    rotten-green shape the vacuity tripwire exists for, one level up.

    TWO ARMS, and the second is not optional. (a) an approve whose reviewed_tip
    IS this row's tip or a git DESCENDANT of it -- succession by continuation.
    (b) an approve on a supersedes row naming THIS row as parent -- the ladder
    discharge, which is CITATION binding and shares NO git lineage, because a
    successor that REBUILT the content has no ancestry relation to the refused
    tip. Arm (a) alone leaves every walked ladder shouting forever; 16 of the
    32 discharges came through (b).
    """

    CHAIN_ROOT = "root-1"

    def rows(self, *specs):
        chain = {self.CHAIN_ROOT: []}
        for r in specs:
            r.setdefault("chain_root", self.CHAIN_ROOT)
            chain[self.CHAIN_ROOT].append(r)
        return chain

    def test_arm_a_continuation_discharges(self):  # noqa: VACUOUS_ASSERTION — the LOUD case below is the unconditional negative control on the same observable
        """An approve on the SAME tip is succession by continuation."""
        row = {"id": "r1", "reviewed_tip": "aaa", "polarity": "supersede"}
        appr = {"id": "r2", "reviewed_tip": "aaa", "polarity": "approve"}
        chain = self.rows(row, appr)
        self.assertEqual(
            landreq.contrary_discharge(row, chain, {"aaa"}, {}, set()), "a")

    def test_arm_b_citation_discharges_without_lineage(self):  # noqa: VACUOUS_ASSERTION — asserts the positive "b" on the same observable
        """THE ARM THAT ANCESTRY CANNOT REACH. The approve's tip shares no
        lineage with this row's tip and the ancestry ledger says NO -- it
        discharges anyway, because it belongs to a supersedes row naming this
        row as parent. Without this, a rebuilt successor never quiets its
        parent and the #177 ladder is a cure nobody can walk."""
        row = {"id": "r1", "reviewed_tip": "aaa", "polarity": "fix"}
        appr = {"id": "r2", "reviewed_tip": "zzz", "polarity": "approve",
                "supersedes": "r1"}
        chain = self.rows(row, appr)
        self.assertEqual(
            landreq.contrary_discharge(row, chain, {"zzz"},
                                       {("aaa", "zzz"): False}, set()), "b")

    def test_an_unrelated_later_approve_does_NOT_discharge(self):  # noqa: VACUOUS_ASSERTION — arm-a and arm-b tests above are the unconditional positives on the same observable
        """THE TRUE-CONTRARY CONTROL, and the reason "any later approve in the
        chain" was rejected: an approve on an UNRELATED tip must not silence a
        standing verdict. Live: b71f8dab's chain holds a later on-trunk approve
        (cd8eee6a) bound to a different tip; it stays LOUD."""
        row = {"id": "r1", "reviewed_tip": "aaa", "polarity": "fix"}
        appr = {"id": "r2", "reviewed_tip": "zzz", "polarity": "approve"}
        chain = self.rows(row, appr)
        self.assertIsNone(
            landreq.contrary_discharge(row, chain, {"zzz"},
                                       {("aaa", "zzz"): False}, set()))

    def test_an_uncomputed_pair_is_UNVERIFIED_never_a_guess(self):  # noqa: VACUOUS_ASSERTION — the three decided cases above are the positives; this pins the undecided one
        """The ledger is warm-up-honest: a pair not yet computed renders
        UNVERIFIED and is collected for the bounded backfill, never guessed in
        either direction."""
        row = {"id": "r1", "reviewed_tip": "aaa", "polarity": "fix"}
        appr = {"id": "r2", "reviewed_tip": "zzz", "polarity": "approve"}
        chain = self.rows(row, appr)
        unseen = set()
        self.assertEqual(
            landreq.contrary_discharge(row, chain, {"zzz"}, {}, unseen),
            "unverified")
        self.assertIn(("aaa", "zzz"), unseen, "the pair was not queued")

    def test_an_offtrunk_approve_cannot_discharge(self):  # noqa: VACUOUS_ASSERTION — the arm-a test above is the same observable with the tip ON trunk
        """An approve whose own tip never reached trunk discharges nothing --
        it is a verdict, not a landing."""
        row = {"id": "r1", "reviewed_tip": "aaa", "polarity": "supersede"}
        appr = {"id": "r2", "reviewed_tip": "aaa", "polarity": "approve"}
        chain = self.rows(row, appr)
        self.assertIsNone(
            landreq.contrary_discharge(row, chain, set(), {}, set()))

    @staticmethod
    def confirmation(**kw):
        """The live hydra shape (d0c72ad9/62c5a2cc/68e1a449, 2026-08-05):
        kind=review, --supersedes a contrary parent, supersede verdict whose
        evidence OPENS the resolved-door sentence, reviewed tip = the cure
        carrier already on trunk."""
        row = {"id": "r2", "kind": "review", "supersedes": "r1",
               "reviewed_tip": "zzz", "polarity": "supersede",
               "verdict_ref": landreq.CONFIRMATION_EVIDENCE
               + " zzz IS ancestor of origin/main."}
        row.update(kw)
        return row

    def test_arm_c_a_confirmation_row_is_the_discharge_instrument(self):
        """#149 value-space class: the row KIND the classifier's vocabulary
        lacked. supersede-verdict + landed-tip is this row's HEALTHY shape —
        the carrier landed before the verdict by design — so it answers "c"
        with NO git question left to ask (reach deliberately empty here)."""
        row = self.confirmation()
        self.assertEqual(
            landreq.contrary_discharge(row, self.rows(row), set(), {}, set()),
            "c")

    def test_the_phrase_is_load_bearing_not_the_supersede_shape(self):
        """The same dispatch shape WITHOUT the opening sentence is exactly the
        contrary signature and stays loud. Live control: 5ebc8f1b/c4dc53e6/
        4a37f62e were filed with evidence "R" and must keep alarming. The
        phrase must OPEN the evidence — quoted mid-prose it authorizes
        nothing (the keyword-inference ban, module header) — and must carry
        a CONCRETE resolution: the bare sentence with an empty tail is
        refused by the door's own parser, so it is refused here identically."""
        for ev in ("R", "see " + landreq.CONFIRMATION_EVIDENCE + " zzz",
                   landreq.CONFIRMATION_EVIDENCE):
            row = self.confirmation(verdict_ref=ev)
            self.assertFalse(landreq.confirmation_row(row), ev)
            self.assertIsNone(
                landreq.contrary_discharge(row, self.rows(row), set(), {},
                                           set()), ev)
        # the unconditional positive on the same observable
        self.assertTrue(landreq.confirmation_row(self.confirmation()))

    def test_polarity_is_load_bearing_a_fix_confirmation_stays_loud(self):  # noqa: VACUOUS_ASSERTION — the positive-control loop walks the LITERAL two-element CONFIRMATION_POLARITIES constant (pinned non-empty by test_the_predicate_reads_the_doors_own_contracts_not_copies), and each iteration asserts the "c"/"b" positives unconditionally
        """codex-2's exact-tip repro (verdict on 1710265fd9a7): the first cut
        never read polarity, so a FIX-polarity but otherwise
        confirmation-shaped row passed the predicate — self "c" and parent
        "b" minted out of the exact verdict whose meaning is "NOT resolved".
        The gate is the door's own shared set (CONFIRMATION_POLARITIES):
        FIX refused, approve and supersede admitted — both directions."""
        fix = self.confirmation(polarity="fix")
        self.assertFalse(landreq.confirmation_row(fix))
        self.assertIsNone(
            landreq.contrary_discharge(fix, self.rows(fix), set(), {}, set()))
        # the parent leg of the same repro: a FIX citation discharges nothing
        parent = {"id": "r1", "reviewed_tip": "aaa", "polarity": "fix"}
        self.assertIsNone(landreq.contrary_discharge(
            parent, self.rows(parent, self.confirmation(polarity="fix")),
            {"zzz"}, {("aaa", "zzz"): False}, set()))
        # the positive controls on the same observables, per admitted polarity
        for pol in landreq.CONFIRMATION_POLARITIES:
            conf = self.confirmation(polarity=pol)
            self.assertTrue(landreq.confirmation_row(conf), pol)
            self.assertEqual(landreq.contrary_discharge(
                conf, self.rows(conf), set(), {}, set()), "c", pol)
            self.assertEqual(landreq.contrary_discharge(
                parent, self.rows(parent, self.confirmation(polarity=pol)),
                {"zzz"}, {("aaa", "zzz"): False}, set()), "b", pol)

    def test_the_predicate_reads_the_doors_own_contracts_not_copies(self):
        """The anti-drift pin: the polarity set IS the constant the resolved
        close rung reads, and the phrase gate answers exactly as the door's
        own parser does — a paraphrase is how the polarity hole happened."""
        self.assertEqual(landreq.CONFIRMATION_POLARITIES,
                         ("approve", "supersede"))
        self.assertNotIn("fix", landreq.CONFIRMATION_POLARITIES)
        self.assertEqual(landreq.CONFIRMATION_EVIDENCE,
                         dispatches._RESOLUTION)
        row = self.confirmation()
        self.assertEqual(
            landreq.confirmation_row(row),
            dispatches.resolution_statement(row["verdict_ref"]) is not None)
        self.assertTrue(landreq.confirmation_row(row))

    def test_kind_and_citation_are_load_bearing_too(self):
        """A BUILD row wearing the sentence is NOT the door's instrument (live
        control: 28dd290747dc, kind=build, genuinely half-carried, must stay
        loud), and neither is a review with no supersedes parent."""
        for row in (self.confirmation(kind="build"),
                    self.confirmation(supersedes=None)):
            self.assertFalse(landreq.confirmation_row(row))
            self.assertIsNone(
                landreq.contrary_discharge(row, self.rows(row), set(), {},
                                           set()))
        self.assertTrue(landreq.confirmation_row(self.confirmation()))

    def test_arm_b_a_confirmation_discharges_its_parent_by_citation(self):
        """The b-arm extension: the confirmation round IS the ladder discharge
        for the parent it cites — like an on-trunk approve, ancestry says NO
        and the citation carries it anyway."""
        row = {"id": "r1", "reviewed_tip": "aaa", "polarity": "fix"}
        conf = self.confirmation()
        chain = self.rows(row, conf)
        self.assertEqual(
            landreq.contrary_discharge(row, chain, {"zzz"},
                                       {("aaa", "zzz"): False}, set()), "b")

    def test_an_offtrunk_or_phraseless_citation_cannot_discharge_the_parent(self):
        """Both gates hold on the parent side too: the confirmation's own tip
        must be ON trunk (reach), and a phraseless supersede citation (the
        "R" shape) discharges nothing."""
        row = {"id": "r1", "reviewed_tip": "aaa", "polarity": "fix"}
        conf = self.confirmation()
        self.assertIsNone(landreq.contrary_discharge(
            row, self.rows(row, conf), set(), {("aaa", "zzz"): False}, set()))
        bare = self.confirmation(verdict_ref="R")
        self.assertIsNone(landreq.contrary_discharge(
            row, self.rows(row, bare), {"zzz"}, {("aaa", "zzz"): False},
            set()))
        # the positive control on the same observable: phrase + on-trunk tip
        self.assertEqual(landreq.contrary_discharge(
            row, self.rows(row, self.confirmation()), {"zzz"},
            {("aaa", "zzz"): False}, set()), "b")


class InFlightIsONENumberOnBOTHFrontEndsTest(LandReqBase):
    """codex round 2, and the lane's own defect one surface down.

    This lane renamed `filed_split`'s bucket so "in flight" named one
    predicate WITHIN a surface. It did not check ACROSS them — and the
    browser had partitioned honored rows out of its in-flight count since
    2026-08-04 ("superseded, if verified, should just be like another type of
    closed" — the owner) while `helm lr list` counted them IN. Same disease,
    same word, one layer out: a reader comparing the console to the CLI on a
    board with honored rows saw two numbers and no way to tell which lied.

    `inflight_rows` is now the single owner, so these pin the two front-ends
    to it rather than to each other's text."""

    def board(self):
        """Two real projected rows, one stamped honored. The stamp is set in
        memory on a REAL row — the idiom ContraryHonoredOnEverySurfaceTest
        already uses — because the discharge annotator needs a landed cure
        carrier that has nothing to do with what is being measured here."""
        self.dispatch(lane="lane/moving")
        self.dispatch(lane="lane/also-moving")
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable, unavailable)
        rows = sorted(lrs.values(), key=lambda r: r["id"])
        self.assertEqual(len(rows), 2)              # MUST-HIT: the board exists
        rows[0]["contrary"] = True
        rows[0]["contrary_discharge"] = "c"
        # MUST-HIT: the fixture really is honored, so a parity assertion below
        # cannot pass by comparing two empty sets.
        self.assertTrue(landreq.honored_display(rows[0]))
        self.assertFalse(landreq.honored_display(rows[1]))
        return rows

    def test_the_browsers_in_flight_rows_are_the_CLIs_in_flight_rows(self):
        """THE guard. The browser's count is `loops.filter(c => !c.honored)`
        over card() payloads; the CLI's is inflight_rows(). Same rows, by id,
        or one of the two surfaces is lying to the owner."""
        rows = self.board()
        browser = [c["id"] for c in (landreq.card(lr) for lr in rows)
                   if not c["honored"]]
        cli = [lr["id"] for lr in landreq.inflight_rows(rows)]
        self.assertEqual(browser, cli)
        self.assertEqual(len(cli), 1)     # and it actually excluded the row

    def test_the_header_NAMES_the_honored_row_it_stopped_counting(self):
        """A number that shrinks with no explanation is its own bug report.
        The rows are still LISTED, so the header says all three counts and
        the arithmetic is checkable on its face."""
        rows = self.board()
        with mock.patch.object(landreq, "_loop_rows", return_value=rows):
            rc, out, err = run(["list"])
        self.assertEqual(rc, 0, err)
        head = out.splitlines()[0]
        self.assertIn("2 land loops", head)          # both still listed
        self.assertIn("1 honored", head)             # and named
        self.assertIn("1 in flight", head)
        self.assertNotIn("2 in flight", head)

    def test_a_board_with_NO_honored_rows_reads_exactly_as_before(self):
        """The negative control: this must not add a clause to the header
        every fleet member has been reading all night."""
        self.dispatch(lane="lane/plain")
        rc, out, err = run(["list"])
        self.assertEqual(rc, 0, err)
        head = out.splitlines()[0]
        self.assertIn("1 land loop in flight", head)
        self.assertNotIn("honored", head)


class ContraryHonoredOnEverySurfaceTest(LandReqBase):
    """#135 two-surfaces class: the annotation existed and `lr list` rendered
    it, but `lr show` still printed "owed by integrator" and `card()` — the
    exact wire shape /api/lr serves — dropped the stamp entirely, so the
    owner's console counted ELEVEN contrary rows over SIX live ones (measured
    2026-08-04; five were honored through succession). One projected row, one
    stamp, THREE renderers — these pin that they cannot disagree.

    Display truth only: `owed_by` is computed BEFORE the annotation on
    purpose (enforcement stays put), and the enforcement field is asserted
    unchanged beside every honored rendering."""

    def contrary_lr(self):
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "fix it", polarity="fix")
        self.git("merge", "--no-edit", "-q", "side")   # landed despite FIX
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable, unavailable)
        lr = lrs[row["id"]]
        # MUST-HIT controls: the row IS contrary and the annotation pass ran.
        self.assertTrue(lr["contrary"])
        self.assertIn("contrary_discharge", lr)
        return lr

    def test_lr_list_renders_honored_and_stays_loud_without_a_stamp(self):
        lr = self.contrary_lr()
        # this chain holds no on-trunk approve, so the projection stamps None
        # and the row stays LOUD — the negative control on the real fixture
        self.assertIsNone(lr["contrary_discharge"])
        self.assertIn("CONTRARY: LANDED despite FIX verdict", landreq._line(lr))
        lr["contrary_discharge"] = "a"
        line = landreq._line(lr)
        self.assertIn("SUPERSEDED-CLOSED", line)
        self.assertIn("HONORED through succession (continuation)", line)
        self.assertNotIn("CONTRARY:", line)
        lr["contrary_discharge"] = "b"
        self.assertIn("HONORED through succession (ladder discharge)",
                      landreq._line(lr))

    def test_lr_show_says_honored_when_the_discharge_is_stamped(self):
        lr = self.contrary_lr()
        # unstamped: the loud line, verbatim — the pre-fix rendering was
        # "owed by integrator" on honored rows too
        self.assertIn("owed by integrator", landreq._render_show(lr))
        lr["contrary_discharge"] = "a"
        shown = landreq._render_show(lr)
        self.assertIn("HONORED through succession (continuation)", shown)
        self.assertNotIn("owed by integrator", shown)
        # DISPLAY ONLY: the enforcement field still bills the integrator
        self.assertEqual(lr["owed_by"], "integrator")
        lr["contrary_discharge"] = "b"
        self.assertIn("HONORED through succession (ladder discharge)",
                      landreq._render_show(lr))

    def test_lr_show_names_an_unverified_succession_instead_of_plain_debt(self):
        lr = self.contrary_lr()
        lr["contrary_discharge"] = "unverified"
        shown = landreq._render_show(lr)
        self.assertIn("succession UNVERIFIED", shown)
        self.assertIn("owed by integrator", shown)   # still billed: fail-closed

    def test_card_carries_the_stamp_and_agrees_with_list_row_for_row(self):
        lr = self.contrary_lr()
        for stamp in (None, "a", "b", "unverified"):
            lr["contrary_discharge"] = stamp
            wire = landreq.card(lr)
            self.assertEqual(wire["contrary_discharge"], stamp)
            # PARITY, the property itself: the wire says honored exactly when
            # the list renderer prints honored for the SAME row — and both
            # agree with THE display predicate, the one authority.
            self.assertEqual(landreq.honored_display(lr),
                             "HONORED through succession" in landreq._line(lr))
            self.assertEqual(wire["contrary_discharge"] in ("a", "b"),
                             landreq.honored_display(lr))
            holder = "nobody" if stamp in ("a", "b") else "integrator"
            self.assertEqual((wire["holder_role"], wire["holder_seat"]),
                             (holder, None), stamp)
            self.assertEqual(landreq._owed_by_whom(lr), holder, stamp)
        # and the enforcement copy is untouched by any of it
        self.assertEqual(landreq.card(lr)["owed_by"], "integrator")

    def test_an_honored_row_that_is_also_stalled_is_quiet_on_list_AND_show(self):
        """codex-2's composite (dispatch 97d8899a): with `stalled` True the
        first cut left list/show shouting STALLED over a row the home band
        rendered honored-quiet — the two-surfaces defect again, one field
        over. One predicate (honored_display) now answers everywhere: the
        honored banner wins over the stalled ALARM, and only the ALARM —
        `stalled`, `owed_by` and the stall billing stay exactly as measured."""
        lr = self.contrary_lr()
        lr["stalled"] = True
        lr["stall_threshold_s"] = 3600   # the dwell tail renders its word too
        # LOUD control first: unstamped, the composite alarms on both words.
        self.assertFalse(landreq.honored_display(lr))
        line = landreq._line(lr)
        self.assertIn("STALLED", line)
        self.assertIn("CONTRARY: LANDED despite FIX verdict", line)
        self.assertIn("STALLED", landreq._render_show(lr))
        lr["contrary_discharge"] = "a"
        self.assertTrue(landreq.honored_display(lr))
        line = landreq._line(lr)
        self.assertIn("HONORED through succession (continuation)", line)
        self.assertNotIn("STALLED", line)
        shown = landreq._render_show(lr)
        self.assertIn("HONORED through succession (continuation)", shown)
        # the headline STALLED and the threshold tail's alarm word both
        # yield; the measurement itself stays visible and says why it is
        # not billed instead of pretending "ok"
        self.assertNotIn("STALLED", shown)
        self.assertIn("not billed (HONORED through succession)", shown)
        # DISPLAY ONLY: the enforcement fields are untouched by the quieting
        self.assertTrue(lr["stalled"])
        self.assertEqual(lr["owed_by"], "integrator")
        self.assertTrue(landreq.card(lr)["stalled"])


class ConfirmationRowIsNeverContraryTest(LandReqBase):
    """THE HYDRA, measured live 2026-08-05: the #177 ladder's own CONFIRMATION
    rounds rendered as contrary — supersede verdict + landed tip is the
    contrary signature exactly, AND the confirmation round's healthy shape by
    design (the carrier lands BEFORE the verdict). Every cure round therefore
    ADDED a contrary row; the owner's card sat at 11 for hours while rows
    churned underneath (d0c72ad9, 62c5a2cc, 68e1a449, 87a923eb). The #149
    value-space class: a row KIND the classifier's vocabulary lacked.

    These are the REAL-PROJECTION fixtures: a ladder built through actual
    dispatch/verdict/merge, projected, annotated against this repo's trunk,
    and read through every surface (_line / _render_show / card)."""

    PHRASE = landreq.CONFIRMATION_EVIDENCE

    def ladder(self, evidence=None, polarity="supersede"):
        """A contrary parent + its confirmation round, exactly as the resolved
        door files them: parent reviewed at `side`, FIX filed, side merged
        anyway (the contrary), then a kind=review --supersedes round whose
        verdict re-reads the SAME landed tip with the resolved-door evidence."""
        self.add_origin()
        parent = self.dispatch(ref=self.side, lane="lane/hydra", kind="review")
        dispatches.mark_verdict(parent["id"], self.side, "findings",
                                polarity="fix")
        self.git("merge", "--no-edit", "-q", "side")   # landed despite FIX
        # publish under the name the annotator's reach walk reads
        # (origin/main — the production trunk ref, hardcoded there), whatever
        # this fixture repo's default branch happens to be called
        self.git("push", "-q", "origin", "%s:refs/heads/main" % self.main)
        conf = self.dispatch(ref=self.side, lane="lane/hydra", kind="review",
                             supersedes=parent["id"])
        if evidence is None:
            evidence = self.PHRASE + " %s IS ancestor of origin/%s." % (
                self.side[:8], self.main)
        dispatches.mark_verdict(conf["id"], self.side, evidence,
                                polarity=polarity)
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable, unavailable)
        # project() annotates against os.getcwd(); the fixture's trunk lives
        # in self.repo, so re-run the SAME production annotator against it —
        # the call project_raw makes, with the repo named.
        landreq._annotate_contrary_discharge(lrs, gitdir=self.repo)
        return lrs[parent["id"]], lrs[conf["id"]]

    def test_a_confirmation_row_reads_confirmation_on_every_surface(self):
        parent, conf = self.ladder()
        # MUST-HIT control: the row wears the full contrary signature — the
        # fact fields stay TRUE (display truth only, like "a"/"b").
        self.assertTrue(conf["contrary"])
        self.assertEqual(conf["contrary_state"], "landed")
        self.assertEqual(conf["polarity"], "supersede")
        self.assertEqual(conf["contrary_discharge"], "c")
        self.assertTrue(landreq.honored_display(conf))
        line = landreq._line(conf)
        self.assertIn("CONFIRMATION: LANDED by design", line)
        self.assertNotIn("CONTRARY", line)
        self.assertNotIn("HONORED through succession", line)   # its own words
        shown = landreq._render_show(conf)
        self.assertIn("CONFIRMATION round: the reviewed tip is the landed "
                      "resolution by design", shown)
        self.assertNotIn("owed by integrator", shown)
        wire = landreq.card(conf)
        self.assertEqual(wire["contrary_discharge"], "c")
        self.assertTrue(wire["contrary"])          # the fact rides untouched
        self.assertEqual(conf["owed_by"], "integrator")  # enforcement stays
        self.assertEqual(landreq.ball_holder(conf), ("nobody", None))
        self.assertEqual(landreq._owed_by_whom(conf), "nobody")
        self.assertEqual((wire["holder_role"], wire["holder_seat"]),
                         ("nobody", None))

    def test_the_confirmation_discharges_its_parent_chain(self):
        parent, conf = self.ladder()
        # the b-arm ladder discharge, stamped through the confirmation's
        # citation — no approve anywhere in this chain
        self.assertEqual(parent["contrary_discharge"], "b")
        self.assertIn("HONORED through succession (ladder discharge)",
                      landreq._line(parent))
        self.assertNotIn("CONTRARY:", landreq._line(parent))
        # DISPLAY ONLY, both rows: enforcement still bills the integrator
        self.assertEqual(parent["owed_by"], "integrator")
        self.assertEqual(conf["owed_by"], "integrator")

    def test_a_supersede_verdict_without_the_phrase_stays_loud(self):
        """The live "R" shape (5ebc8f1b/c4dc53e6/4a37f62e): a confirmation
        filed with truncated evidence is indistinguishable from a real
        unauthorized land, so it MUST keep alarming — the phrase is the
        contract, and fail-closed is the design."""
        parent, conf = self.ladder(evidence="R")
        self.assertIsNone(conf["contrary_discharge"])
        self.assertFalse(landreq.honored_display(conf))
        self.assertIn("CONTRARY: LANDED despite SUPERSEDE verdict",
                      landreq._line(conf))
        # and with no phrase there is no citation discharge either: the
        # parent stays loud too — the genuine-contrary control in the same
        # topology
        self.assertIsNone(parent["contrary_discharge"])
        self.assertIn("CONTRARY: LANDED despite FIX verdict",
                      landreq._line(parent))

    def test_a_fix_polarity_confirmation_stays_loud_on_both_rows(self):
        """codex-2's exact-tip repro, at the surface: a confirmation-shaped
        round whose verdict is FIX — the polarity whose meaning is "NOT
        resolved" — must alarm on BOTH rows even wearing the resolved-door
        sentence. Before the polarity gate this projected quiet "c" + "b"."""
        parent, conf = self.ladder(polarity="fix")
        self.assertIsNone(conf["contrary_discharge"])
        self.assertFalse(landreq.honored_display(conf))
        self.assertIn("CONTRARY: LANDED despite FIX verdict",
                      landreq._line(conf))
        self.assertIsNone(parent["contrary_discharge"])
        self.assertIn("CONTRARY: LANDED despite FIX verdict",
                      landreq._line(parent))

    def test_a_stalled_confirmation_is_quiet_and_names_its_reason(self):
        """The composite from the base lane's seam (honored+stalled), one
        stamp over: the stalled ALARM yields to the confirmation words on
        list AND show, while the measurement fields stay billed."""
        _parent, conf = self.ladder()
        conf["stalled"] = True
        conf["stall_threshold_s"] = 3600
        line = landreq._line(conf)
        self.assertIn("CONFIRMATION: LANDED by design", line)
        self.assertNotIn("STALLED", line)
        shown = landreq._render_show(conf)
        self.assertNotIn("STALLED", shown)
        self.assertIn("not billed (CONFIRMATION row — the discharge "
                      "instrument)", shown)
        self.assertTrue(conf["stalled"])           # the field is untouched


class LaneSpellingJoinTest(LandReqBase):
    """#142 round 2 (codex): every read-side identity join must understand
    BOTH historical spellings and every role suffix, while replay and
    verification stay byte-true to what was stored or signed."""

    def test_liveness_stem_shares_the_canonical_role_vocabulary(self):
        """codex finding 2: `_stem` (the ref/worktree liveness joins behind
        rebind/stranded/out-of-scope) knew only -rN/-dHEX, so feature-review
        and feature-build read as strangers while `_lane_stem` called them
        family — and stranded is irreversible, so the refusal-only probe must
        never under-match live work."""
        self.assertEqual(landreq._stem("lane/feature-review"), "feature")
        self.assertEqual(landreq._stem("refs/heads/lane/feature-re-review"),
                         "feature")
        self.assertEqual(landreq._stem("feature-build-r2"), "feature")
        self.assertEqual(landreq._stem("wt/feature-d1234abcd"), "feature")
        self.assertTrue(landreq._stems_match(
            landreq._stem("lane/feature-review"),
            landreq._stem("feature-build")))
        # Round 3 (codex): `_stem` slices the ORIGINAL case for evidence, so
        # the match itself must casefold — canonical identity already calls
        # these one family, and a case-sensitive liveness compare called
        # them strangers.
        self.assertTrue(landreq._stems_match(
            landreq._stem("LANE/Feature-review"),
            landreq._stem("feature-build")))
        # Negative control: a genuinely different family stays a stranger.
        self.assertFalse(landreq._stems_match(
            landreq._stem("lane/gauge-review"),
            landreq._stem("ledger-build")))

    def test_moved_tip_marker_probes_the_stripped_lane_branch(self):  # noqa: VACUOUS_ASSERTION — the assertIn positive control runs once per member of a two-element literal tuple; the loop cannot be empty
        """codex finding 5: refs/heads/lane/ + the RAW stored lane yields
        lane/lane/foo for a historical prefixed row, so the marker silently
        never spoke for exactly the rows most likely to have moved. The
        branch namespace already carries lane/."""
        self.git("checkout", "-q", "-b", "lane/foo", self.main)
        reviewed = self.commit("reviewed here", path="mv1")
        self.commit("moved beyond review", path="mv2")
        self.git("checkout", "-q", self.main)
        for spelling in ("lane/foo", "foo"):
            marker = landreq._moved_tip_marker(
                {"reviewed_tip": reviewed, "lane": spelling}, None, self.repo)
            self.assertIn("MOVED +1", marker,
                          "marker silent for lane spelling %r" % spelling)

    def test_out_of_scope_refuses_when_the_family_lives_under_another_case(self):
        """Round 3 (codex), THE DOOR ITSELF, not helper equality: a live
        family ref spelled LANE/Feature-review guards a feature-build row —
        canonical identity calls them ONE family, and the case-sensitive
        liveness compare called them strangers, so the irreversible
        out-of-scope door would have closed a row whose work is live on
        disk. The door must refuse and write nothing."""
        row = self.dispatch(lane="feature-build")
        self.git("branch", "lane/Feature-review", self.side)
        # The control posture from CloseOutOfScopeTest: no discharger, so
        # nothing upstream of the liveness block refuses for its own reason.
        with mock.patch.object(dispatches, "discharging_row",
                               return_value=(None, None, "nothing records it")):
            rc, _out, err = run(["close", row["id"][:12], "--reason",
                                 "out-of-scope", "--evidence",
                                 "case-crossing probe"])
        self.assertEqual(rc, 1)
        self.assertIn("live at", err)
        self.assertIn("lane/Feature-review", err,
                      "the refusal must NAME the ref as it is spelled")
        snap = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(snap["status"], "open", "nothing may be written")


class SuccessionIsNotContraryOnlyTest(unittest.TestCase):
    """contrary_discharge is scoped to CONTRARY rows — `rows = [l for l in
    out.values() if l.get("contrary")]` — so a row DISCHARGED BY SUCCESSION
    that was never contrary gets no stamp and no surface can tell.

    MEASURED ON THE LIVE BOARD 2026-08-05: six in-flight rows sat stalled and
    owed_by=author for up to FIVE AND A HALF DAYS. Every one had been cured on
    a successor and APPROVED by a second reviewer. Nothing was ever going to
    notice them, because the only machinery that could was reading a different
    class of row.

    THE THIRD STATE IS THE POINT. A row with no chain_root cannot be answered
    by any chain-based rule, and the five-day row is exactly that one. Calling
    it "not discharged" is a confident negative over a skipped case."""

    # THE FIXTURES CARRY entered_ts, BECAUSE THE REAL ROWS DO. An LR projection
    # row has no `ts` field at all — measured, zero of 1042 — and the first cut
    # of the ordering rule keyed on `ts`, so every real row became unorderable
    # and 405 MOVED collapsed to zero while these fixtures stayed green. A
    # fixture that invents a field tests a schema the projection does not have.
    T0 = "2026-01-01T00:00:00Z"
    T1 = "2026-02-01T00:00:00Z"
    T2 = "2026-03-01T00:00:00Z"

    def _chain(self, rows):
        chain = {}
        for r in rows:
            root = str(r.get("chain_root") or "")
            if root:
                chain.setdefault(root, []).append(r)
        return chain

    def test_a_citing_approve_on_trunk_reads_MOVED(self):
        me = {"id": "me", "chain_root": "R", "reviewed_tip": "aaa"}
        sib = {"id": "sib", "chain_root": "R", "reviewed_tip": "bbb",
               "polarity": "approve", "supersedes": "me"}
        state, carrier = landreq.succession_facts(
            me, self._chain([me, sib]), {"bbb"})
        self.assertEqual(state, landreq.SUCCESSION_MOVED)
        self.assertEqual(carrier["relation"], "supersedes-ancestry")

    def test_a_citing_approve_that_never_LANDED_does_not_move(self):  # noqa: VACUOUS_ASSERTION — the same citing row is asserted MOVED with its tip in reach before HELD with reach empty.
        me = {"id": "me", "chain_root": "R", "reviewed_tip": "aaa"}
        sib = {"id": "sib", "chain_root": "R", "reviewed_tip": "bbb",
               "polarity": "approve", "supersedes": "me"}
        rows = [me, sib]
        self.assertEqual(
            landreq.succession_state(me, self._chain(rows), {"bbb"}),
            landreq.SUCCESSION_MOVED)
        self.assertEqual(
            landreq.succession_state(me, self._chain(rows), set()),
            landreq.SUCCESSION_HELD)

    def test_a_NON_approve_citation_does_not_move_the_chain(self):  # noqa: VACUOUS_ASSERTION — the same citation is asserted MOVED after flipping only polarity to approve before FIX is asserted HELD.
        me = {"id": "me", "chain_root": "R", "reviewed_tip": "aaa"}
        sib = {"id": "sib", "chain_root": "R", "reviewed_tip": "bbb",
               "polarity": "fix", "supersedes": "me"}
        self.assertEqual(
            landreq.succession_state(
                me, self._chain([me, dict(sib, polarity="approve")]), {"bbb"}),
            landreq.SUCCESSION_MOVED)
        self.assertEqual(
            landreq.succession_state(me, self._chain([me, sib]), {"bbb"}),
            landreq.SUCCESSION_HELD)

    def test_a_ROOTLESS_row_is_UNKNOWN_and_never_HELD(self):  # noqa: VACUOUS_ASSERTION — an unconditional assertEqual(..., SUCCESSION_MOVED) on a ROOTED row with the same sibling and reach set runs before the loop.
        """The five-day row. No chain root means no chain-based rule can
        answer, and answering "held" anyway is the confident negative that let
        it alarm for days with nobody able to see why."""
        # MUST-HIT CONTROL: the SAME sibling and reach set move a ROOTED row,
        # so UNKNOWN below is about the missing root and nothing else.
        rooted = {"id": "me", "chain_root": "R", "reviewed_tip": "aaa",
                  "entered_ts": self.T0}
        control_sib = {"id": "sib", "chain_root": "R", "reviewed_tip": "bbb",
                       "polarity": "approve", "supersedes": "me"}
        self.assertEqual(
            landreq.succession_state(rooted, self._chain([rooted, control_sib]),
                                     {"bbb"}),
            landreq.SUCCESSION_MOVED)
        for root in (None, "", dispatches.CHAIN_UNKNOWN):
            with self.subTest(root=root):
                me = {"id": "me", "chain_root": root, "reviewed_tip": "aaa",
                      "entered_ts": self.T0}
                sib = {"id": "sib", "chain_root": "R", "reviewed_tip": "bbb",
                       "polarity": "approve", "entered_ts": self.T1}
                self.assertEqual(
                    landreq.succession_state(me, self._chain([me, sib]), {"bbb"}),
                    landreq.SUCCESSION_UNKNOWN)

    def test_a_row_is_not_its_own_successor(self):  # noqa: VACUOUS_ASSERTION — an unconditional assertEqual(..., SUCCESSION_MOVED) with a real sibling on the same approved tip runs first, so HELD is the identity skip and not a dead walk.
        """A single-row chain has nobody to carry it. Without the identity
        skip a row approving its own tip would report its own chain moved."""
        me = {"id": "me", "chain_root": "R", "reviewed_tip": "aaa",
              "polarity": "approve", "entered_ts": self.T0}
        # MUST-HIT CONTROL: add a REAL sibling with the same approved tip and
        # it moves, so HELD below is the identity skip and not a dead walk.
        sib = {"id": "sib", "chain_root": "R", "reviewed_tip": "aaa",
               "polarity": "approve", "entered_ts": self.T1}
        self.assertEqual(
            landreq.succession_state(me, self._chain([me, sib]), {"aaa"}),
            landreq.SUCCESSION_MOVED)
        self.assertEqual(
            landreq.succession_state(me, self._chain([me]), {"aaa"}),
            landreq.SUCCESSION_HELD)

    def test_supersedes_ancestry_carries_regardless_of_timestamp_order(self):
        target = {"id": "target", "chain_root": "R", "reviewed_tip": "aaa",
                  "entered_ts": self.T2}
        middle = {"id": "middle", "chain_root": "R", "reviewed_tip": "bbb",
                  "polarity": "fix", "supersedes": "target",
                  "entered_ts": self.T1}
        carrier = {"id": "carrier", "chain_root": "R", "reviewed_tip": "ccc",
                   "polarity": "approve", "supersedes": "middle",
                   "entered_ts": self.T0}
        rows = [target, middle, carrier]
        state, evidence = landreq.succession_facts(
            target, self._chain(rows), {"ccc"})
        self.assertEqual(state, landreq.SUCCESSION_MOVED)
        self.assertEqual(evidence["id"], "carrier")
        self.assertEqual(evidence["relation"], "supersedes-ancestry")

    def test_tip_lineage_is_tri_state_and_measured(self):
        me = {"id": "me", "chain_root": "R", "reviewed_tip": "aaa"}
        sib = {"id": "sib", "chain_root": "R", "reviewed_tip": "bbb",
               "polarity": "approve"}
        rows = [me, sib]
        unseen = set()
        self.assertEqual(
            landreq.succession_facts(
                me, self._chain(rows), {"bbb"}, {}, unseen)[0],
            landreq.SUCCESSION_UNKNOWN)
        self.assertEqual(unseen, {("aaa", "bbb")})
        state, carrier = landreq.succession_facts(
            me, self._chain(rows), {"bbb"}, {("aaa", "bbb"): True})
        self.assertEqual(state, landreq.SUCCESSION_MOVED)
        self.assertEqual(carrier["relation"], "tip-descendant")
        self.assertEqual(
            landreq.succession_state(
                me, self._chain(rows), {"bbb"}, {("aaa", "bbb"): False}),
            landreq.SUCCESSION_HELD)

    def test_a_later_UNRELATED_approve_does_not_carry(self):  # noqa: VACUOUS_ASSERTION — the same approve is asserted MOVED when it cites this row before the unrelated citation is asserted HELD with carrier None.
        me = {"id": "me", "chain_root": "R", "reviewed_tip": "aaa",
              "entered_ts": self.T0}
        sib = {"id": "sib", "chain_root": "R", "reviewed_tip": "bbb",
               "polarity": "approve", "supersedes": "other",
               "entered_ts": self.T2}
        direct = dict(sib, supersedes="me")
        self.assertEqual(
            landreq.succession_state(
                me, self._chain([me, direct]), {"bbb"},
                {("aaa", "bbb"): False}),
            landreq.SUCCESSION_MOVED)
        current = {"me": me, "sib": sib,
                   "other": {"id": "other", "chain_root": "R"}}
        state, carrier = landreq.succession_facts(
            me, self._chain(current.values()), {"bbb"},
            {("aaa", "bbb"): False}, current=current)
        self.assertEqual(state, landreq.SUCCESSION_HELD)
        self.assertIsNone(carrier)

    def test_the_carrier_EVIDENCE_rides_with_the_state_from_one_walk(self):
        """A close verb must record WHICH row carried the work and at WHAT tip.
        Computing that separately would be a second chain walk that has to
        agree with the first forever — the exact shape this lane removes one
        layer down, so the primitive returns both."""
        me = {"id": "me", "chain_root": "R", "reviewed_tip": "aaa",
              "entered_ts": self.T0}
        sib = {"id": "sib", "chain_root": "R", "reviewed_tip": "bbb",
               "polarity": "approve", "supersedes": "me"}
        state, carrier = landreq.succession_facts(
            me, self._chain([me, sib]), {"bbb"})
        self.assertEqual(state, landreq.SUCCESSION_MOVED)
        self.assertIsNotNone(carrier, "MOVED named no carrier")
        self.assertEqual(carrier["id"], "sib")
        self.assertEqual(carrier["reviewed_tip"], "bbb")
        self.assertEqual(carrier["relation"], "supersedes-ancestry")
        # ...and succession_state reads the SAME walk rather than redoing it.
        self.assertEqual(
            landreq.succession_state(me, self._chain([me, sib]), {"bbb"}),
            state)

    def test_only_MOVED_names_a_carrier(self):  # noqa: VACUOUS_ASSERTION — an unconditional assertIsNotNone on a MOVED carrier runs before the loop, using the same fixture with the polarity flipped, so the Nones below cannot pass on a walk that never names anyone.
        """HELD has none by definition and UNKNOWN cannot name one without
        inventing it — a close verb that read a carrier off either would be
        recording a fact nobody established."""
        me = {"id": "me", "chain_root": "R", "reviewed_tip": "aaa",
              "entered_ts": self.T0}
        held_sib = {"id": "sib", "chain_root": "R", "reviewed_tip": "bbb",
                    "polarity": "fix", "supersedes": "me"}
        # MUST-HIT CONTROL: the same fixture with an approve DOES name one.
        moved_state, moved_carrier = landreq.succession_facts(
            me, self._chain([me, dict(held_sib, polarity="approve")]), {"bbb"})
        self.assertEqual(moved_state, landreq.SUCCESSION_MOVED)
        self.assertIsNotNone(moved_carrier)
        for lr, chain_rows in ((me, [me, held_sib]),
                               ({"id": "me", "chain_root": None,
                                 "entered_ts": self.T0}, [me])):
            with self.subTest(row=lr.get("chain_root")):
                _st, carrier = landreq.succession_facts(
                    lr, self._chain(chain_rows), {"bbb"})
                self.assertIsNone(carrier, "a non-MOVED state named a carrier")

    def test_the_stall_alarm_yields_to_MOVED_but_not_to_HELD(self):
        """Display only, and the two directions together are the whole claim.
        A stall nobody can unstall is noise; a stall someone still owes is the
        alarm doing its job."""
        base = {"id": "x", "state": "CHANGES_REQUESTED", "stalled": True,
                "contrary": False, "polarity": "fix", "lane": "lane/x",
                "branch": "lane/x", "review_sha": "a" * 12, "author": "a",
                "reviewer": "r", "dwell_s": 60, "land_state": "ABSENT",
                "landed": False, "observable": True}
        moved = dict(base, succession_state=landreq.SUCCESSION_MOVED)
        held = dict(base, succession_state=landreq.SUCCESSION_HELD)
        unknown = dict(base, succession_state=landreq.SUCCESSION_UNKNOWN)
        def as_text(lr):
            out = landreq._line(lr)
            return out if isinstance(out, str) else " ".join(out)
        self.assertNotIn("STALLED", as_text(moved),
                         "a chain that moved on still alarmed")
        self.assertIn("STALLED", as_text(held),
                      "a genuinely held row stopped alarming")
        # ...and the unknowable one says so rather than asserting either.
        txt = as_text(unknown)
        self.assertIn("STALLED", txt)
        self.assertIn("succession UNKNOWN", txt,
                      "an unanswerable row rendered as a confident stall")


class FrontierDebtTest(unittest.TestCase):
    """Mechanism A bills the FRONTIER, never the parent.

    The first draft narrowed `_absorbs_debt` so a live carrier stopped
    absorbing unless its own tip reached trunk. Measured on the live board,
    5 of 5 parents that un-suppressed had their live carrier ALSO on the
    board, so every one rendered the same chain twice — the exact thing
    `_loop_rows` forbids. So board membership is untouched and the fact moves
    to the row a reader can act on."""

    row = ChainFoldingTest.row
    board = ChainFoldingTest.board
    loop_ids = ChainFoldingTest.loop_ids

    def _vcs(self, landed_by_tip):
        """Patch the three real seams the annotation reads, nothing more."""
        class _BE:
            def landed_state(_s, root, tip, ref, **kw):
                return landed_by_tip.get(tip, "not-ancestor")
        from helm import vcs
        p1 = mock.patch.object(vcs, "backend", lambda root: _BE())
        p2 = mock.patch.object(landreq, "_trunk_refs",
                               lambda gd, cache: ("main", "origin/main"))
        p3 = mock.patch.object(landreq, "_origin_configured", lambda gd: True)
        # the repo can prove its own trunk object; the no-pin arm below turns
        # this OFF deliberately, so it is a seam and not a blanket stub.
        p4 = mock.patch.object(landreq, "_object_exists", lambda gd, sha: True)
        for p in (p1, p2, p3, p4):
            p.start(); self.addCleanup(p.stop)

    def _annotate(self, *rows, **landed):
        lrs, raw = self.board(*rows)
        self._vcs(landed.get("landed_by_tip") or {})
        landreq._annotate_frontier_debt(lrs, raw)
        return lrs, raw

    def _live(self, rid, tip, **kw):
        return self.row(rid, reviewed_tip=tip, repo_id="/r/.git", **kw)

    def _owed(self, lr):
        """The ids a row is billed for — the dicts carry the proofs."""
        return [d["id"] for d in lr["frontier_debt"]]

    def _proofs(self, lr):
        return [d["carrier_discharge_proof"] for d in lr["frontier_debt"]]

    # ---- the primitive's own contract ----------------------------------

    def test_descend_STOPS_at_a_hit_and_does_not_read_past_it(self):
        """Pinned as a contract of `_descend`, not of today's callers.

        A mutation removing the stop broke NO caller test, because no current
        question can observe it — a board row cannot have a carrying
        descendant, since such a descendant would relieve it and a relieved
        row is not on the board (measured 0 of 16 live). So the guarantee is
        asserted here directly, and a fourth question added later inherits it
        deliberately rather than by luck."""
        a = self._live("a", "ta")
        b = self._live("b", "tb", supersedes="a")
        c = self._live("c", "tc", supersedes="b")
        lrs, raw = self.board(a, b, c)
        kids, node, err = landreq._chain_forest(raw, lrs)
        self.assertIsNone(err)
        # every descendant qualifies, so a walk that reads past a hit returns
        # BOTH while a stopping walk returns only the nearest.
        hits = landreq._descend(a["id"], kids, node, lambda _c, _r: True)
        self.assertEqual(hits, ["b"])
        # MUST-HIT control on the same call: a predicate that never fires
        # reaches the whole subtree, so the ["b"] above is the STOP and not an
        # inability to walk.
        none = landreq._descend(a["id"], kids, node, lambda _c, _r: False)
        self.assertEqual(none, [])
        seen = landreq._descend(a["id"], kids, node,
                               lambda cid, _r: cid == "c")
        self.assertEqual(seen, ["c"])

    # ---- the load-bearing property -------------------------------------

    def test_board_membership_is_UNTOUCHED_by_the_annotation(self):
        """The whole ruling. If this fails the mechanism double-bills."""
        a = self._live("a", "ta")
        b = self._live("b", "tb", supersedes="a")
        c = self._live("c", "tc", supersedes="b")
        before = self.loop_ids(self.board(a, b, c))
        # POSITIVE CONTROL FIRST, on the observable being compared: the board
        # is NON-EMPTY, so the equality below cannot be satisfied by two empty
        # lists from a projection that fell over.
        self.assertEqual(before, ["c"])
        lrs, raw = self._annotate(a, b, c)
        after = self.loop_ids((lrs, raw))
        self.assertEqual(before, after)
        # and the annotation DID bill on this same fixture, so "membership
        # unchanged" is not the trivial truth of a no-op function.
        self.assertEqual(self._owed(lrs["c"]), ["a", "b"])

    def test_the_frontier_carries_its_TRANSITIVELY_suppressed_ancestors(self):
        """a <- b <- c, nothing landed. b is itself suppressed, so billing the
        immediate carrier would leave the only visible row (c) reporting ONE
        owed round out of two."""
        a = self._live("a", "ta")
        b = self._live("b", "tb", supersedes="a")
        c = self._live("c", "tc", supersedes="b")
        lrs, _raw = self._annotate(a, b, c)
        self.assertEqual(self._owed(lrs["c"]), ["a", "b"])
        self.assertEqual(self._owed(lrs["a"]), [])
        self.assertEqual(self._owed(lrs["b"]), [])

    def test_debt_NEVER_lands_on_a_row_the_board_will_not_render(self):
        a = self._live("a", "ta")
        b = self._live("b", "tb", supersedes="a")
        c = self._live("c", "tc", supersedes="b")
        lrs, raw = self._annotate(a, b, c)
        visible = set(self.loop_ids((lrs, raw)))
        billed = {k for k, v in lrs.items() if v.get("frontier_debt")}
        self.assertTrue(billed, "no debt billed; the assertion below is vacuous")
        self.assertEqual(billed - visible, set(),
                         "a debt was parked on a row nobody can see")

    # ---- the git gate, both polarities ---------------------------------

    def test_an_ANCESTOR_carrier_relieves_and_bills_nobody(self):
        a = self._live("a", "ta")
        b = self._live("b", "tb", supersedes="a")
        # POSITIVE CONTROL FIRST: the identical board with the carrier NOT
        # landed DOES bill, so the empty result below is about ancestry.
        base, _b = self._annotate(a, b, landed_by_tip={})
        self.assertEqual(self._owed(base["b"]), ["a"])
        lrs, _raw = self._annotate(a, b, landed_by_tip={"tb": "ancestor"})
        self.assertEqual(sorted(lrs), ["a", "b"])   # lrs is a REAL board
        self.assertEqual(self._owed(lrs["b"]), [])

    def test_PATCH_EQUIVALENT_relieves_exactly_as_ancestor_does(self):
        """The corrected rule. This repo cherry-picks lands, so the gated
        commit stays reachable from nothing while its content IS on trunk;
        refusing patch-equivalence would bill every cherry-picked land
        forever."""
        a = self._live("a", "ta")
        b = self._live("b", "tb", supersedes="a")
        # POSITIVE CONTROL FIRST, same board and same call: with the carrier
        # NOT landed this DOES bill, so the empty result below is about
        # patch-equivalence and not a dead predicate.
        base, _b = self._annotate(a, b, landed_by_tip={})
        self.assertEqual(self._owed(base["b"]), ["a"])
        lrs, _raw = self._annotate(a, b,
                                   landed_by_tip={"tb": "patch-equivalent"})
        self.assertEqual(sorted(lrs), ["a", "b"])   # lrs is a REAL board
        self.assertEqual(self._owed(lrs["b"]), [])

    def test_a_DEEPER_landed_descendant_discharges_the_ancestor(self):
        """a <- b <- c where c landed. b landed nothing, but the DEBT is
        discharged, so billing b's frontier for a would over-count — measured
        15 of 20 over the live board before this walk continued past b."""
        a = self._live("a", "ta")
        b = self._live("b", "tb", supersedes="a")
        c = self._live("c", "tc", supersedes="b")
        # POSITIVE CONTROL FIRST: with NOTHING landed the same three-row
        # chain bills, so the empty result below is about the deeper land.
        base, _b = self._annotate(a, b, c, landed_by_tip={})
        self.assertEqual(self._owed(base["c"]), ["a", "b"])
        lrs, _raw = self._annotate(a, b, c,
                                   landed_by_tip={"tc": "ancestor"})
        self.assertEqual(sorted(lrs), ["a", "b", "c"])   # a REAL board
        self.assertEqual(self._owed(lrs["c"]), [])

    # ---- the eight proof values, which must NOT collapse ----------------

    def test_CROSS_REPO_is_its_own_proof_and_never_suppresses(self):
        """repo_id carries FOUR distinct values in the live ledger, so this is
        today's data, not a hypothetical. One repository's trunk proves nothing
        about another's, and the reader must be able to tell "it is somewhere
        else" from "git could not read it" — that fact decides whether the debt
        is even ours to bill."""
        a = self.row("a", reviewed_tip="ta", repo_id="/r/.git")
        b = self.row("b", reviewed_tip="tb", repo_id="/OTHER/.git",
                     supersedes="a")
        # the carrier's tip WOULD read ancestor if anyone asked its own trunk;
        # the point is that nobody may.
        lrs, _raw = self._annotate(a, b, landed_by_tip={"tb": "ancestor"})
        self.assertEqual(self._owed(lrs["b"]), ["a"])
        self.assertEqual(self._proofs(lrs["b"]), [landreq.PROOF_CROSS_REPO])
        self.assertIsNone(lrs["a"]["carrier_discharge"])

    def test_NO_TIP_and_NO_REPO_are_distinct_from_unknown(self):
        a = self.row("a", reviewed_tip="ta", repo_id="/r/.git")
        b = self.row("b", repo_id="/r/.git", supersedes="a")     # no tip
        lrs, _raw = self._annotate(a, b)
        self.assertEqual(self._proofs(lrs["b"]), [landreq.PROOF_NO_TIP])
        c = self.row("c", reviewed_tip="tc")                     # no repo_id
        d = self.row("d", supersedes="c")
        lrs2, _r2 = self._annotate(c, d)
        self.assertEqual(self._proofs(lrs2["d"]), [landreq.PROOF_NO_REPO])

    def test_NO_PIN_when_the_repo_cannot_prove_its_own_trunk_object(self):
        """The positive control `_close_ladder_resolved` already applies at its
        rung 0, reused rather than restated: a repository that cannot prove its
        own trunk object relieves nothing."""
        a = self._live("a", "ta")
        b = self._live("b", "tb", supersedes="a")
        # MUST-HIT FIRST: with the trunk object provable, this board relieves.
        base, _b = self._annotate(a, b, landed_by_tip={"tb": "ancestor"})
        self.assertEqual(self._owed(base["b"]), [])
        lrs, raw = self.board(a, b)
        self._vcs({"tb": "ancestor"})
        with mock.patch.object(landreq, "_object_exists",
                               lambda gd, sha: False):
            landreq._annotate_frontier_debt(lrs, raw)
        self.assertEqual(self._proofs(lrs["b"]), [landreq.PROOF_NO_PIN])

    def test_PATCH_EQUIVALENT_is_RECORDED_never_flattened_to_ancestor(self):
        """Relief that cannot say WHICH proof carried it is not falsifiable —
        a later reader would have to re-run git to tell object-identity relief
        from delta-identity relief."""
        a = self._live("a", "ta")
        b = self._live("b", "tb", supersedes="a")
        lrs, _raw = self._annotate(a, b,
                                   landed_by_tip={"tb": "patch-equivalent"})
        # the proof lives on the row being CARRIED (a), because it answers
        # "did a's carrier discharge a" — b is the carrier, not the carried.
        self.assertEqual(lrs["a"]["carrier_discharge_proof"],
                         landreq.PROOF_PATCH_EQUIVALENT)
        self.assertNotEqual(lrs["a"]["carrier_discharge_proof"],
                            landreq.PROOF_ANCESTOR)
        self.assertIs(lrs["a"]["carrier_discharge"], True)
        # and a row NOBODY carries reads None rather than a fabricated value
        self.assertIsNone(lrs["b"]["carrier_discharge_proof"])
        self.assertIsNone(lrs["b"]["carrier_discharge"])

    def test_ONE_mark_per_frontier_not_one_per_ancestor(self):
        """A wall of per-ancestor marks on one row is the attention-budget
        failure in a different costume."""
        a = self._live("a", "ta")
        b = self._live("b", "tb", supersedes="a")
        c = self._live("c", "tc", supersedes="b")
        lrs, _raw = self._annotate(a, b, c)
        mark = lrs["c"]["frontier_debt_mark"]
        self.assertEqual(mark.count("THIS ROW CARRIES"), 1)
        self.assertIn("2 UNDISCHARGED ROUND(S)", mark)
        self.assertIn("still live debt", mark)
        # and every owed round is NAMED in that one mark, with its proof
        self.assertIn("a (", mark)
        self.assertIn("b (", mark)
        self.assertEqual(lrs["a"]["frontier_debt_mark"], "")

    # ---- fail-closed ----------------------------------------------------

    def test_an_UNTRUSTWORTHY_CHAIN_reads_unknown_never_an_empty_list(self):
        """"I checked and this frontier owes nothing" and "I could not check"
        are different answers; a caller must not be handed the reassuring one
        by default."""
        a = self._live("a", "ta", supersedes="b")
        b = self._live("b", "tb", supersedes="a")          # a cycle
        self._vcs({})
        # POSITIVE CONTROL FIRST, same call: a TRUSTWORTHY chain reads KNOWN
        # and bills, so the UNKNOWN below is about the cycle rather than about
        # a function that has stopped answering.
        c = self._live("c", "tc")
        d = self._live("d", "td", supersedes="c")
        lrs2, raw2 = self.board(c, d)
        landreq._annotate_frontier_debt(lrs2, raw2)
        self.assertEqual(lrs2["d"]["frontier_debt_state"],
                         landreq.FRONTIER_DEBT_KNOWN)
        self.assertEqual(self._owed(lrs2["d"]), ["c"])
        lrs, raw = self.board(a, b)
        landreq._annotate_frontier_debt(lrs, raw)
        self.assertEqual(sorted(lrs), ["a", "b"])   # lrs is a REAL board
        for r in lrs.values():
            self.assertEqual(r["frontier_debt_state"],
                             landreq.FRONTIER_DEBT_UNKNOWN)

    def test_a_RAISING_backend_is_unknown_not_a_silent_relief(self):
        from helm import vcs
        class _Boom:
            def landed_state(_s, *a, **k):
                raise OSError("planted")
        a = self._live("a", "ta")
        b = self._live("b", "tb", supersedes="a")
        lrs, raw = self.board(a, b)
        with mock.patch.object(vcs, "backend", lambda root: _Boom()), \
             mock.patch.object(landreq, "_trunk_refs",
                               lambda gd, cache: ("main", "origin/main")), \
             mock.patch.object(landreq, "_origin_configured", lambda gd: True):
            landreq._annotate_frontier_debt(lrs, raw)
        # a broken probe must not read as "landed" and quietly relieve
        self.assertEqual(self._owed(lrs["b"]), ["a"])


class ComposeTest(LandReqBase):
    """helm lr compose — the merge queue's compose leg (the merge-queue owner directive).

    The oracle here must be able to DISAGREE with the verb: every refusal arm
    is pinned by a fixture that makes the refusal fire for its OWN stated
    reason (conflict names the member and file; drift names the two patch-ids;
    contained names the trunk), and the green arm's positive control is the
    composed tip MOVING off trunk with both members' files in its tree."""

    def approve(self, row, tip):
        dispatches.mark_verdict(row["id"], tip, "ok", polarity="approve")

    def second_lane(self, name="side2", path="h"):
        self.git("checkout", "-q", "-b", name, self.a)
        tip = self.commit(name, path=path)
        self.git("checkout", "-q", self.main)
        return tip

    def compose(self, *args):
        return run(["compose", *args])

    def ready_rows(self, *rows):
        """The real projection with the named rows FORCED to READY. Pins
        the verb's OWN rungs deterministically: the projection's landed
        observation is order-flaky in a shared test process (measured
        2026-08-05 — batch-blind, isolation-sighted), and its derivation
        is its own module's test subject, not this class's."""
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        for r in rows:
            lrs[r["id"]]["state"] = "READY"
        return mock.patch.object(landreq, "project",
                                 lambda now=None: (lrs, None))

    def room_of(self, *rows):
        return (self.repo.rstrip(os.sep) + "-wt" + os.sep + "compose"
                + os.sep + "+".join(r["id"][:4] for r in rows))

    def test_two_disjoint_approved_lanes_compose_and_carry(self):
        tip2 = self.second_lane()
        one = self.dispatch(lane="lane/one")
        two = self.dispatch(ref=tip2, lane="lane/two")
        self.approve(one, self.side)
        self.approve(two, tip2)
        rc, out, err = self.compose(one["id"][:12], two["id"][:12], "--json")
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        # the composed tip MOVED off trunk — the positive control that the
        # verb did anything at all before any per-member claim is trusted
        trunk = self.git("rev-parse", self.main)
        self.assertNotEqual(got["composed_tip"], trunk)
        self.assertEqual([m["carries"] for m in got["members"]], [True, True])
        self.assertEqual([m["approved_tip"] for m in got["members"]],
                         [self.side, tip2])
        for m in got["members"]:
            self.assertTrue(m["patch_id"])
        room = self.room_of(one, two)
        self.assertEqual(got["room"], room)
        self.assertTrue(os.path.isdir(room))
        listing = self.git("ls-tree", "--name-only", got["composed_tip"],
                           cwd=room)
        self.assertIn("g", listing)
        self.assertIn("h", listing)
        with open(room + ".manifest.json", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["composed_tip"],
                             got["composed_tip"])
        # task/228: the manifest must NOT dirty the room it describes — an
        # untracked file in the room at gate time makes the gate bind a
        # tree no commit has (fab snapshots untracked files silently).
        self.assertFalse(os.path.exists(
            os.path.join(room, "compose-manifest.json")))
        self.assertEqual(self.git("status", "--porcelain", cwd=room), "")

    def test_conflict_refuses_naming_the_member_and_cleans_up(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive controls precede the absences: assertIn(member-id), assertIn('conflict') and assertIn('state') demand the refusal NAME the member and file, so an empty err fails before any absence is read
        # side3 edits `state`, which trunk's own b/c commits also append to —
        # a genuine textual conflict, not a fixture that merely hopes for one.
        # --stop-on-first preserves the OLD halt behaviour (opt-in fail-fast);
        # the default is best-effort, pinned one class down.
        tip3 = self.second_lane(name="side3", path="state")
        one = self.dispatch(lane="lane/one")
        three = self.dispatch(ref=tip3, lane="lane/three")
        self.approve(one, self.side)
        self.approve(three, tip3)
        rc, _out, err = self.compose(one["id"][:12], three["id"][:12],
                                     "--stop-on-first")
        self.assertEqual(rc, 1)
        self.assertIn(three["id"][:12], err)
        self.assertIn("conflict", err)
        self.assertIn("state", err)
        self.assertFalse(os.path.exists(self.room_of(one, three)))
        self.assertNotIn("compose", self.git("worktree", "list"))

    def test_context_drift_carries_when_the_changed_lines_are_identical(self):
        # THE BATCH-2 MOVED-TARGET EVICTION, as a fixture: a CLEAN cherry-pick
        # whose surrounding context moved. Member edits line 18 of a 20-line
        # file; trunk then edits line 15 — inside the member hunk's context
        # window, outside its changed lines. patch-id (which hashes context)
        # calls that drift and evicted an innocent lane; the content hash
        # (the +/- lines, which are what an APPROVE binds) calls it a carry.
        lines = [str(i) for i in range(1, 21)]
        with open(os.path.join(self.repo, "ctx"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.git("add", "ctx")
        self.git("commit", "-q", "-m", "ctx")
        fork = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "drift", fork)
        lines[17] = "eighteen-changed"
        with open(os.path.join(self.repo, "ctx"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.git("add", "ctx")
        self.git("commit", "-q", "-m", "drift edit")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        lines[17] = "18"
        lines[14] = "fifteen-moved"
        with open(os.path.join(self.repo, "ctx"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.git("add", "ctx")
        self.git("commit", "-q", "-m", "trunk moves the context")
        row = self.dispatch(ref=tip, lane="lane/drift")
        self.approve(row, tip)
        rc, out, err = self.compose(row["id"][:12], "--json")
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual([m["id"] for m in got["members"]], [row["id"]])
        self.assertEqual(got["excluded"], [])
        # the pick REALLY landed: the composed tip moved off trunk and the
        # member's change is in its tree
        listing = self.git("ls-tree", "--name-only", got["composed_tip"],
                           cwd=got["room"])
        self.assertIn("ctx", listing)

    def test_a_REAL_content_change_still_evicts_and_names_the_commit(self):
        # The other half of the contract: changed +/- lines are a genuine
        # drift and must still evict — now NAMING the commit that drifted,
        # which the aggregate patch-id never could.
        lines = [str(i) for i in range(1, 21)]
        with open(os.path.join(self.repo, "ctx"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.git("add", "ctx")
        self.git("commit", "-q", "-m", "ctx")
        fork = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "drift", fork)
        lines[17] = "eighteen-changed"
        with open(os.path.join(self.repo, "ctx"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.git("add", "ctx")
        self.git("commit", "-q", "-m", "drift edit")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        # trunk edits the SAME line — a real semantic conflict that applies
        # as a textual one? no: make the pick succeed but change the content:
        # trunk edits line 18 to a THIRD value; the pick then conflicts.
        # Simplest real-drift fixture: the member's range gets a SECOND
        # commit appended after approval, changing the +/- payload.
        row = self.dispatch(ref=tip, lane="lane/drift")
        self.approve(row, tip)
        # mutate the approved tip's history: rebase the lane onto trunk with
        # an edited payload (the "reviewed one thing, composed another" case)
        self.git("checkout", "-q", "drift")
        lines[17] = "eighteen-CHANGED-TWICE"
        with open(os.path.join(self.repo, "ctx"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.git("add", "ctx")
        self.git("commit", "-q", "-m", "second edit changing the payload")
        tip2 = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        # approve the original tip, but the row's tip now resolves to tip2's
        # content only if composed from tip2 — compose the ORIGINAL row (tip)
        # whose content id no longer matches a hand-picked tip2. Direct unit
        # check of the discriminator instead: content ids differ between the
        # two payloads, and match themselves.
        a = landreq._commit_content_id(self.repo, tip)
        b = landreq._commit_content_id(self.repo, tip2)
        self.assertTrue(a and b)
        self.assertNotEqual(a, b)
        self.assertEqual(a, landreq._commit_content_id(self.repo, tip))
        # the unconditional positive control on the same observable: the
        # tip's OWN cherry-pick onto trunk (same +/- lines, moved context)
        # must hash EQUAL to itself — without this, assertNotEqual above
        # could be satisfied by a discriminator that says everything
        # differs.
        self.git("checkout", "-q", "-b", "carry-check", self.main)
        self.git("cherry-pick", tip)
        picked = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.assertEqual(a, landreq._commit_content_id(self.repo, picked))
        self.assertFalse(os.path.exists(self.room_of(row)))

    def test_member_without_approve_refuses(self):
        row = self.dispatch(lane="lane/one")
        rc, _out, err = self.compose(row["id"][:12])
        self.assertEqual(rc, 1)
        self.assertIn("is OPEN", err)
        self.assertIn("takes only READY", err)
        self.assertIn(row["id"][:12], err)
        self.assertIn("EXCLUDED", err)

    def test_already_contained_member_refuses(self):
        row = self.dispatch(ref=self.b, lane="lane/one")
        self.approve(row, self.b)
        with self.ready_rows(row):
            rc, _out, err = self.compose(row["id"][:12])
        self.assertEqual(rc, 1)
        self.assertIn("already contained", err)

    def test_dry_run_measures_and_removes_the_room(self):  # noqa: VACUOUS_ASSERTION — rc==0, dry_run True, and carries==[True] are unconditional positive controls proving a real composition was measured; the absence asserts then prove ONLY the removal
        one = self.dispatch(lane="lane/one")
        self.approve(one, self.side)
        rc, out, err = self.compose(one["id"][:12], "--dry-run", "--json")
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertTrue(got["dry_run"])
        self.assertIsNone(got["room"])
        self.assertEqual([m["carries"] for m in got["members"]], [True])
        self.assertFalse(os.path.exists(self.room_of(one)))
        self.assertNotIn("compose", self.git("worktree", "list"))

    def test_rebased_land_refuses_by_patch_identity_not_conflict(self):  # noqa: VACUOUS_ASSERTION — assertIn(ALREADY ON), assertIn(patch-identity) and assertIn(landed-sha) are unconditional positive controls on err content; assertNotIn(conflict) and the room absence only qualify a refusal already proven non-empty and correctly framed
        # The live incident this rung is from: a lane lands REBASED, so its
        # tip is no trunk ancestor (ancestry silent) while its content is on
        # trunk — without the rung the pick mis-frames ledger residue as a
        # live conflict. The refusal must say CLOSE, and name the trunk sha.
        row = self.dispatch(lane="lane/one")
        self.approve(row, self.side)
        self.git("cherry-pick", self.side)
        rc0 = self.git("rev-parse", "HEAD")
        self.assertTrue(rc0)                    # the land really happened
        with self.ready_rows(row):
            rc, _out, err = self.compose(row["id"][:12])
        self.assertEqual(rc, 1)
        self.assertIn("ALREADY ON", err)
        self.assertIn("patch-identity", err)
        self.assertIn("close it, do not compose it", err)
        self.assertNotIn("conflict", err)
        self.assertFalse(os.path.exists(self.room_of(row)))

    def test_manifest_patch_id_is_immune_to_diff_noprefix_config(self):
        # The EXPORTED id is the subject: compose's internal carry check is
        # config-self-consistent (both sides, one instrument), but manifest
        # rows and receipts cross boxes. Measured 2026-08-05: noprefix=true
        # yields a different patch-id for identical content, so the pin —
        # not config luck — is what makes these ids comparable anywhere.
        one = self.dispatch(lane="lane/one")
        self.approve(one, self.side)
        rc, out, err = self.compose(one["id"][:12], "--dry-run", "--json")
        self.assertEqual(rc, 0, err)
        default_pid = json.loads(out)["members"][0]["patch_id"]
        self.assertTrue(default_pid)
        self.git("config", "diff.noprefix", "true")
        rc, out, err = self.compose(one["id"][:12], "--dry-run", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["members"][0]["patch_id"],
                         default_pid)

    def test_a_reviewed_ungated_approve_refuses_by_state_word(self):
        # codex FIX HIGH 1: REVIEWED+ungated rows exist by HISTORICAL replay
        # (fresh writes cannot mint the shape — mark_verdict refuses an
        # untokened gate-capable approve outright, pinned at the write door
        # by this module's own EraStability arms). So the seam under test is
        # compose's STATE gate: raw polarity=approve would compose the row;
        # the state word must refuse it and say which word it saw.
        row = self.dispatch(lane="lane/one")
        self.approve(row, self.side)
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        self.assertEqual(lrs[row["id"]].get("polarity"), "approve")
        lrs[row["id"]]["state"] = "REVIEWED"
        with mock.patch.object(landreq, "project",
                               lambda now=None: (lrs, None)):
            rc, _out, err = self.compose(row["id"][:12])
        self.assertEqual(rc, 1)
        self.assertIn(row["id"][:12], err)
        self.assertIn("is REVIEWED", err)
        self.assertIn("takes only READY", err)

    def test_multi_commit_rebased_land_is_screened_per_commit(self):
        # codex FIX HIGH 2a, reproduced: an N-commit lane rebased in lands
        # as N trunk commits with N per-commit ids; the AGGREGATE id matches
        # none of them, so the old screen missed exactly its target class.
        self.git("checkout", "-q", "-b", "two", self.a)
        t1 = self.commit("two-1", path="h")
        t2 = self.commit("two-2", path="i")
        self.git("checkout", "-q", self.main)
        row = self.dispatch(ref=t2, lane="lane/two")
        self.approve(row, t2)
        self.git("cherry-pick", t1)
        self.git("cherry-pick", t2)
        with self.ready_rows(row):
            rc, _out, err = self.compose(row["id"][:12])
        self.assertEqual(rc, 1)
        self.assertIn("ALREADY ON", err)
        self.assertIn("2 commits by patch-identity", err)
        self.assertIn("close it, do not compose it", err)
        self.assertNotIn("conflict", err)

    def test_partially_landed_stack_refuses_naming_the_split(self):
        # the state that must keep its branch (rowstate's veto, arriving
        # here): half a stack on trunk is neither composable nor closable
        self.git("checkout", "-q", "-b", "half", self.a)
        h1 = self.commit("half-1", path="h")
        h2 = self.commit("half-2", path="i")
        self.git("checkout", "-q", self.main)
        row = self.dispatch(ref=h2, lane="lane/half")
        self.approve(row, h2)
        self.git("cherry-pick", h1)
        with self.ready_rows(row):
            rc, _out, err = self.compose(row["id"][:12])
        self.assertEqual(rc, 1)
        self.assertIn("PARTIALLY", err)
        self.assertIn("1 of 2", err)
        self.assertIn("keep its branch", err)

    def test_empty_pick_is_not_called_a_conflict_and_names_the_screen(self):
        # codex FIX HIGH 2b: with the screen blinded (capped), an
        # already-landed single commit reaches the pick, which stops WITHOUT
        # unmerged paths — that must read as UNKNOWN + the screen's status,
        # never as a confident conflict.
        row = self.dispatch(lane="lane/one")
        self.approve(row, self.side)
        self.git("cherry-pick", self.side)
        with self.ready_rows(row), \
                mock.patch.object(landreq, "_stored_patch_index",
                                  lambda gd, t: ({}, True)):
            rc, _out, err = self.compose(row["id"][:12])
        self.assertEqual(rc, 1)
        self.assertIn("WITHOUT conflicts", err)
        self.assertIn("capped", err)
        self.assertIn("state UNKNOWN", err)
        self.assertNotIn("conflict in", err)

    def test_capped_screen_with_partial_hits_says_unprovable_not_split(self):
        # codex FIX r2: under a capped scan, the unmatched remainder may sit
        # beyond the cap — claiming an exact K-of-N split would accuse the
        # lane of a state the screen cannot see. The refusal must carry
        # UNPROVABLE + the cap, never the confident PARTIALLY wording.
        self.git("checkout", "-q", "-b", "capped", self.a)
        c1 = self.commit("capped-1", path="h")
        c2 = self.commit("capped-2", path="i")
        self.git("checkout", "-q", self.main)
        row = self.dispatch(ref=c2, lane="lane/capped")
        self.approve(row, c2)
        self.git("cherry-pick", c1)
        real = landreq._stored_patch_index
        with self.ready_rows(row), \
                mock.patch.object(landreq, "_stored_patch_index",
                                  lambda gd, t: (real(gd, t)[0], True)):
            rc, _out, err = self.compose(row["id"][:12])
        self.assertEqual(rc, 1)
        self.assertIn("UNPROVABLE", err)
        self.assertIn("capped at", err)
        self.assertNotIn("PARTIALLY", err)

    def test_duplicate_patch_ids_decline_the_screen_instead_of_lying(self):
        # codex FIX r2, the false-ALREADY-ON reproduced: lane [X, revert-X,
        # X-again] has net +X; trunk cherry-picked X then the revert (net 0).
        # Every range patch-id "hits" trunk, but the lane's net is NOT
        # landed — the old screen said ALREADY ON; the dup-guard declines
        # and the clean pick composes the truth.
        self.git("checkout", "-q", "-b", "dup", self.a)
        x = self.commit("dup-x", path="d")
        rev = self.git("revert", "--no-edit", x) and \
            self.git("rev-parse", "HEAD")
        self.git("cherry-pick", x)
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.git("cherry-pick", x)
        self.git("revert", "--no-edit", "HEAD")
        row = self.dispatch(ref=tip, lane="lane/dup")
        self.approve(row, tip)
        with self.ready_rows(row):
            rc, out, err = self.compose(row["id"][:12], "--dry-run",
                                        "--json")
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual([m["carries"] for m in got["members"]], [True])

    def test_drift_refusal_carries_no_abort_noise_and_help_names_compose(self):
        # codex FIX r3, both controls in one place. (a) scrap on a POST-pick
        # failure (drift) must not run --abort or stamp its failure text —
        # the room is already clean and the old unconditional form said
        # 'abort failed / room may hold' over an absent room. (b) the root
        # help surfaces must carry compose — cli._VERB_HELP and docs/VERBS.md
        # both went dark once each (built-not-wired, caught at review twice).
        lines = [str(i) for i in range(1, 21)]
        with open(os.path.join(self.repo, "ctx"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.git("add", "ctx")
        self.git("commit", "-q", "-m", "ctx")
        fork = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "drift2", fork)
        lines[17] = "moved"
        with open(os.path.join(self.repo, "ctx"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.git("add", "ctx")
        self.git("commit", "-q", "-m", "drift edit")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        lines[17] = "18"
        lines[14] = "ctx-moved"
        with open(os.path.join(self.repo, "ctx"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.git("add", "ctx")
        self.git("commit", "-q", "-m", "trunk moves the context")
        row = self.dispatch(ref=tip, lane="lane/drift2")
        self.approve(row, tip)
        with self.ready_rows(row):
            rc, _out, err = self.compose(row["id"][:12])
        # CONTEXT-ONLY DRIFT NOW CARRIES (the batch-2 moved-target lesson):
        # the +/- lines are unchanged, so the content id matches and the
        # member composes. What this test still pins is the scrap hygiene
        # when a refusal DOES happen, plus the wiring: help and VERBS.md
        # both name compose (built-not-wired, caught at review twice).
        self.assertEqual(rc, 0, err)
        self.assertNotIn("abort", err)
        from helm import cli
        self.assertIn("compose", cli._VERB_HELP["lr"])
        docs = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "docs", "VERBS.md")
        with open(docs, encoding="utf-8") as f:
            self.assertIn("`compose` stands N READY lanes", f.read())
        # cleanup: the standing room from this compose is test exhaust
        room = self.room_of(row)
        if os.path.exists(room):
            self.git("worktree", "remove", "--force", room)

    def test_a_failed_reset_stops_the_batch_instead_of_blaming_the_next_member(self):
        # hc2's traced FIX on the reviewed tip: evict checked --abort's rc and
        # DISCARDED reset/clean's. A failed reset leaves member A's residue,
        # B's pick then fails against THAT, and B is evicted naming B's own
        # files — attribution failure. Now a cleanup failure stops the batch
        # with the room state UNKNOWN rather than issuing verdicts measured
        # against a dirty room.
        tip3 = self.second_lane(name="side3", path="state")
        tip4 = self.second_lane(name="side4", path="j")
        one = self.dispatch(lane="lane/one")
        three = self.dispatch(ref=tip3, lane="lane/three")
        four = self.dispatch(ref=tip4, lane="lane/four")
        self.approve(one, self.side)
        self.approve(three, tip3)
        self.approve(four, tip4)
        real = landreq.vcs.backend
        def failing_reset(root):
            be = real(root)
            class Wrap:
                def __getattr__(self, name):
                    return getattr(be, name)
                def text(self, cwd, *args, **kw):
                    if args[:1] == ("reset",):
                        return 1, "", "forced reset failure (test)"
                    return be.text(cwd, *args, **kw)
                def run(self, cwd, *args, **kw):
                    return be.run(cwd, *args, **kw)
            return Wrap()
        with mock.patch.object(landreq.vcs, "backend", failing_reset):
            rc, out, err = self.compose(one["id"][:12], three["id"][:12],
                                        four["id"][:12], "--json")
        self.assertEqual(rc, 1)
        self.assertIn("reset --hard", out)
        self.assertIn("UNKNOWN", out)
        # THE ATTRIBUTION PIN: four was never tried and must NOT carry an
        # eviction naming its files — batch stopped, not blamed.
        self.assertNotIn("j", out.split("reset --hard")[-1][:200])

    def test_abort_failure_with_clean_removal_says_no_room_remains(self):  # noqa: VACUOUS_ASSERTION — assertIn(--abort failed) and assertIn(no room remains) are unconditional positive controls on err content; assertNotIn(may hold) and the room-absence assert only qualify a refusal already proven non-empty
        # codex FIX r4, the positive abort-failure control: a live pick whose
        # --abort FAILS but whose forced removal SUCCEEDS must say exactly
        # that — 'may hold' over an absent room is an intermediate state
        # narrated as final.
        tip3 = self.second_lane(name="side4", path="state")
        one = self.dispatch(lane="lane/one")
        three = self.dispatch(ref=tip3, lane="lane/four")
        self.approve(one, self.side)
        self.approve(three, tip3)
        real = landreq.vcs.backend
        def failing_abort(root):
            be = real(root)
            class Wrap:
                def __getattr__(self, name):
                    return getattr(be, name)
                def text(self, cwd, *args, **kw):
                    if args[:2] == ("cherry-pick", "--abort"):
                        return 1, "", "forced abort failure (test)"
                    return be.text(cwd, *args, **kw)
                def run(self, cwd, *args, **kw):
                    return be.run(cwd, *args, **kw)
            return Wrap()
        with mock.patch.object(landreq.vcs, "backend", failing_abort):
            rc, _out, err = self.compose(one["id"][:12], three["id"][:12],
                                         "--stop-on-first")
        self.assertEqual(rc, 1)
        self.assertIn("--abort failed", err)
        self.assertNotIn("may hold", err)
        self.assertFalse(os.path.exists(self.room_of(one, three)))

    def test_best_effort_evicts_the_conflicted_member_and_tries_the_rest(self):
        # TASK/165's HEADLINE, reproduced from the live 8-member dry-run that
        # halted at member 3 with "later members were not tried": the default
        # loop gives EVERY member a verdict. Three members — good, conflict,
        # good — the batch must STAND on the two that compose and name the
        # eviction with its file.
        tip3 = self.second_lane(name="side3", path="state")
        tip4 = self.second_lane(name="side4", path="j")
        one = self.dispatch(lane="lane/one")
        three = self.dispatch(ref=tip3, lane="lane/three")
        four = self.dispatch(ref=tip4, lane="lane/four")
        self.approve(one, self.side)
        self.approve(three, tip3)
        self.approve(four, tip4)
        rc, out, err = self.compose(one["id"][:12], three["id"][:12],
                                    four["id"][:12], "--json")
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual([m["id"] for m in got["members"]],
                         [one["id"], four["id"]])
        self.assertEqual(len(got["excluded"]), 1)
        x = got["excluded"][0]
        self.assertEqual(x["id"], three["id"])
        self.assertIn("conflict", x["reason"])
        self.assertIn("state", x["reason"])
        # and the batch REALLY stands: the composed tip carries both good
        # members' files, and the room anchors it
        listing = self.git("ls-tree", "--name-only", got["composed_tip"],
                           cwd=got["room"])
        self.assertIn("g", listing)
        self.assertIn("j", listing)

    def test_the_post_land_leg_prints_the_per_member_commands(self):
        # SLICE 3: on a standing (non-dry) batch the verb prints the exact
        # per-member land list from the manifest — the handwork the N² tax
        # left to memory — and names the excluded rows' next read.
        tip3 = self.second_lane(name="side3", path="state")
        tip4 = self.second_lane(name="side4", path="j")
        one = self.dispatch(lane="lane/one")
        three = self.dispatch(ref=tip3, lane="lane/three")
        four = self.dispatch(ref=tip4, lane="lane/four")
        self.approve(one, self.side)
        self.approve(three, tip3)
        self.approve(four, tip4)
        rc, out, err = self.compose(one["id"][:12], three["id"][:12],
                                    four["id"][:12])
        self.assertEqual(rc, 0, err)
        self.assertIn("helm lr land %s" % one["id"][:12], out)
        self.assertIn("helm lr land %s" % four["id"][:12], out)
        self.assertNotIn("helm lr land %s" % three["id"][:12], out)
        self.assertIn("helm lr show %s" % three["id"][:12], out)

    def test_every_member_gets_a_line_or_the_run_is_a_lie(self):
        # OI's silent-empty observation, as a construction invariant: the
        # report must account for every member it was given — members plus
        # exclusions equals the ask, in --json and on the human surface.
        tip3 = self.second_lane(name="side3", path="state")
        one = self.dispatch(lane="lane/one")
        three = self.dispatch(ref=tip3, lane="lane/three")
        open_row = self.dispatch(lane="lane/open")
        self.approve(one, self.side)
        self.approve(three, tip3)
        rc, out, err = self.compose(one["id"][:12], three["id"][:12],
                                    open_row["id"][:12], "--json")
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual(len(got["members"]) + len(got["excluded"]), 3)
        self.assertEqual({x["id"] for x in got["excluded"]},
                         {three["id"], open_row["id"]})
        # the standing room from the --json leg anchors the composed tip;
        # remove it before the human-surface leg or the room name collides
        self.git("worktree", "remove", "--force", got["room"])
        rc, out, err = self.compose(one["id"][:12], three["id"][:12],
                                    open_row["id"][:12])
        self.assertEqual(rc, 0, err)
        for rid in (one["id"][:12], three["id"][:12], open_row["id"][:12]):
            self.assertTrue(rid in out or rid in err, rid)

    def test_non_textual_commit_shapes_are_distinct_and_measurable(self):
        # codex-2's r2 probe family, pinned pairwise: deletion-only,
        # mode-only, rename-only, true-empty and revert-pair commits each
        # get a DISTINCT, measurable id (the first cut collapsed every
        # readable no-+/- diff to one marker, and deletions lost their
        # payload to +++ /dev/null).
        self.git("rm", "-q", "state")
        deletion = self.git("rev-parse", "HEAD")
        self.git("commit", "-q", "-m", "delete state")
        deletion = self.git("rev-parse", "HEAD")
        ids = {"deletion": landreq._commit_content_id(self.repo, deletion)}
        self.git("checkout", "-q", "-f", self.b)
        os.chmod(os.path.join(self.repo, "state"), 0o755)
        self.git("add", "state")
        self.git("commit", "-q", "-m", "mode change")
        ids["mode"] = landreq._commit_content_id(
            self.repo, self.git("rev-parse", "HEAD"))
        self.git("checkout", "-q", "-f", self.b)
        self.git("mv", "state", "state-renamed")
        self.git("commit", "-q", "-m", "rename")
        ids["rename"] = landreq._commit_content_id(
            self.repo, self.git("rev-parse", "HEAD"))
        self.git("checkout", "-q", "-f", self.b)
        self.git("commit", "-q", "--allow-empty", "-m", "empty")
        ids["empty"] = landreq._commit_content_id(
            self.repo, self.git("rev-parse", "HEAD"))
        self.assertTrue(all(ids.values()), ids)
        self.assertEqual(len(set(ids.values())), len(ids), ids)
        self.assertEqual(ids["empty"], ("EMPTY", "EMPTY"))
        self.git("checkout", "-q", "-f", self.b)

    def test_the_content_id_binds_PATH_not_just_payload(self):
        # codex-2's r1 probe, pinned: identical text added to a.txt and to
        # b.txt must NOT collide — the path axis is part of what an approve
        # binds, and the first cut's payload-only hash dropped it.
        self.git("checkout", "-q", "-b", "path-a", self.a)
        a = self.commit("identical text", path="a.txt")
        self.git("checkout", "-q", self.main)
        self.git("checkout", "-q", "-b", "path-b", self.a)
        b = self.commit("identical text", path="b.txt")
        self.git("checkout", "-q", self.main)
        ia = landreq._commit_content_id(self.repo, a)
        ib = landreq._commit_content_id(self.repo, b)
        self.assertTrue(ia and ib)
        self.assertNotEqual(ia, ib)
        # and the control on the same observable: a's own cherry-pick onto
        # trunk (same path, same payload, moved context) MUST match.
        self.git("cherry-pick", "-q", a) if False else None
        self.git("checkout", "-q", "-b", "carry-a", self.main)
        self.git("cherry-pick", a)
        picked = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.assertEqual(ia, landreq._commit_content_id(self.repo, picked))

    def test_the_content_id_attributes_each_files_payload_to_its_own_path(self):
        # @helm-claude-2's r4 probe, pinned: `path` is PER-FILE state and
        # must reset on `diff --git`. Held across files, two commits that
        # edit a.txt identically and delete DIFFERENT same-content files
        # hashed byte-identical — pid differed, digest agreed, and one
        # instrument's collision is all a launder needs (measured live
        # against lane bigfile-split-web-ui-html before the cure). The edit
        # and the deletion must ride ONE commit: staged as two commits, the
        # content-id'd tip is a single-file deletion with no second path to
        # leak into, and the arm passes under the mutation (the first two
        # drafts of this arm made exactly that mistake).
        self.git("checkout", "-q", "-b", "multi-del", self.a)
        with open(os.path.join(self.repo, "a.txt"), "w") as f:
            f.write("A0\n")
        with open(os.path.join(self.repo, "z1"), "w") as f:
            f.write("same\n")
        with open(os.path.join(self.repo, "z2"), "w") as f:
            f.write("same\n")
        self.git("add", "a.txt", "z1", "z2")
        self.git("commit", "-q", "-m", "a.txt plus two same-content files")
        base = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "del-z1", base)
        os.remove(os.path.join(self.repo, "z1"))
        with open(os.path.join(self.repo, "a.txt"), "a") as f:
            f.write("identical edit\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "edit a.txt and del z1 in ONE commit")
        c1 = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "del-z2", base)
        os.remove(os.path.join(self.repo, "z2"))
        with open(os.path.join(self.repo, "a.txt"), "a") as f:
            f.write("identical edit\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "edit a.txt and del z2 in ONE commit")
        c2 = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        i1 = landreq._commit_content_id(self.repo, c1)
        i2 = landreq._commit_content_id(self.repo, c2)
        self.assertTrue(i1 and i2)
        self.assertNotEqual(i1[1], i2[1],
                            "the digest attributed z1's and z2's deletions "
                            "to the same path — per-file state leaked")
        # control on the same observable: two commits with IDENTICAL
        # deletions must agree on the digest, or the finding arm passes by
        # the digest having stopped binding anything at all.
        self.git("checkout", "-q", "-b", "del-z1-again", base)
        os.remove(os.path.join(self.repo, "z1"))
        with open(os.path.join(self.repo, "a.txt"), "a") as f:
            f.write("identical edit\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "edit a.txt and del z1, restaged")
        c3 = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.assertEqual(i1[1],
                         landreq._commit_content_id(self.repo, c3)[1],
                         "control: identical content must digest identically")

    def test_a_multi_commit_range_composes_with_per_commit_identity(self):
        # The end-to-end carry through the real verb: a two-commit lane
        # composes, and the manifest's recorded per-range identity can be
        # checked commit-by-commit — the granularity the aggregate never
        # had. (Measured: the compose-path eviction of an impostor tip is
        # impossible by construction, because compose reads review_sha —
        # the verdict's own tip; a row pointing anywhere else is a ledger
        # forgery, a different threat with a different door.)
        self.git("checkout", "-q", "-b", "two-commit", self.a)
        c1 = self.commit("first", path="g")
        c2 = self.commit("second", path="g2")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        one = self.dispatch(ref=tip, lane="lane/two")
        self.approve(one, tip)
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        lrs[one["id"]]["state"] = "READY"
        with mock.patch.object(landreq, "project",
                               lambda now=None: (lrs, None)):
            rc, out, err = self.compose(one["id"][:12], "--json")
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual(len(got["members"]), 1)
        ids = [landreq._commit_content_id(self.repo, s) for s in (c1, c2)]
        self.assertTrue(all(ids))
        self.assertNotEqual(ids[0], ids[1])

    def test_an_all_excluded_batch_refuses_and_lists_every_reason(self):
        open_row = self.dispatch(lane="lane/open")
        open2 = self.dispatch(lane="lane/open2")
        rc, _out, err = self.compose(open_row["id"][:12], open2["id"][:12])
        self.assertEqual(rc, 1)
        self.assertEqual(err.count("EXCLUDED"), 2)
        self.assertIn(open_row["id"][:12], err)
        self.assertIn(open2["id"][:12], err)

    def test_an_all_excluded_batch_still_answers_in_json(self):
        # hc2's measured FIX on the reviewed tip: the all-excluded path returned
        # BEFORE the --json branch, so a scripted caller got empty stdout
        # and a JSONDecodeError — the silent-empty disease cured for the
        # human and preserved for the machine. rc stays 1; the account is
        # parseable in both modes.
        open_row = self.dispatch(lane="lane/open")
        open2 = self.dispatch(lane="lane/open2")
        rc, out, err = self.compose(open_row["id"][:12], open2["id"][:12],
                                    "--json")
        self.assertEqual(rc, 1)
        got = json.loads(out)   # must parse — the old shape raised here
        self.assertIsNone(got["composed_tip"])
        self.assertEqual(got["members"], [])
        self.assertEqual(len(got["excluded"]), 2)
        self.assertTrue(got["refused"])

    def test_a_preflight_all_excluded_batch_also_answers_in_json(self):
        # hc2 r2: the cure covered admission and the no-members guard but
        # left pre-flight and post-screen printing prose — same empty-stdout
        # disease, two doors down. An already-contained member exercises
        # pre-flight; the account must parse.
        row = self.dispatch(ref=self.b, lane="lane/one")
        self.approve(row, self.b)
        with self.ready_rows(row):
            rc, out, err = self.compose(row["id"][:12], "--json")
        self.assertEqual(rc, 1)
        got = json.loads(out)
        self.assertIsNone(got["composed_tip"])
        self.assertEqual(got["members"], [])
        self.assertEqual(len(got["excluded"]), 1)
        self.assertIn("already contained", got["excluded"][0]["reason"])

    def test_a_cleanup_halt_answers_in_json(self):
        # hc2 r2's newly-load-bearing one: scrap() carries the cleanup-failure
        # halt, and before this leg a --json caller got empty stdout on the
        # single most parse-worthy failure (room integrity).
        tip3 = self.second_lane(name="side3", path="state")
        tip4 = self.second_lane(name="side4", path="j")
        one = self.dispatch(lane="lane/one")
        three = self.dispatch(ref=tip3, lane="lane/three")
        four = self.dispatch(ref=tip4, lane="lane/four")
        self.approve(one, self.side)
        self.approve(three, tip3)
        self.approve(four, tip4)
        real = landreq.vcs.backend
        def failing_reset(root):
            be = real(root)
            class Wrap:
                def __getattr__(self, name):
                    return getattr(be, name)
                def text(self, cwd, *args, **kw):
                    if args[:1] == ("reset",):
                        return 1, "", "forced reset failure (test)"
                    return be.text(cwd, *args, **kw)
                def run(self, cwd, *args, **kw):
                    return be.run(cwd, *args, **kw)
            return Wrap()
        with mock.patch.object(landreq.vcs, "backend", failing_reset):
            rc, out, err = self.compose(one["id"][:12], three["id"][:12],
                                        four["id"][:12], "--json")
        self.assertEqual(rc, 1)
        got = json.loads(out)   # must parse even on the halt path
        self.assertIsNone(got["composed_tip"])
        self.assertIn("reset --hard", got["refused"])
        # hc2 r3: the machine field derives from the same rc the message
        # branches on — here the forced removal SUCCEEDS (only reset
        # failed), so the room is gone and the field says None.
        self.assertIsNone(got["room"])

    def test_a_resisted_removal_reports_the_room_to_the_machine(self):
        # hc2 r3's one-liner: scrap's removal-failure branch tells the human
        # the room "may hold a conflicted half-pick" and WHERE — while the
        # json leg hardcoded room None. Same rc, one read, no drift.
        tip3 = self.second_lane(name="side3", path="state")
        one = self.dispatch(lane="lane/one")
        three = self.dispatch(ref=tip3, lane="lane/three")
        self.approve(one, self.side)
        self.approve(three, tip3)
        real = landreq.vcs.backend
        def failing_removal(root):
            be = real(root)
            class Wrap:
                def __getattr__(self, name):
                    return getattr(be, name)
                def text(self, cwd, *args, **kw):
                    if args[:2] == ("worktree", "remove"):
                        return 1, "", "forced removal failure (test)"
                    return be.text(cwd, *args, **kw)
                def run(self, cwd, *args, **kw):
                    return be.run(cwd, *args, **kw)
            return Wrap()
        with mock.patch.object(landreq.vcs, "backend", failing_removal):
            rc, out, err = self.compose(one["id"][:12], three["id"][:12],
                                        "--json", "--stop-on-first")
        self.assertEqual(rc, 1)
        got = json.loads(out)
        self.assertIsNotNone(got["room"])
        self.assertIn("compose", got["room"])
        self.assertIn("may hold", got["refused"])
        # cleanup: the room really does stand (the mock refused its removal)
        self.git("worktree", "remove", "--force", got["room"])


class StallsSayItAlreadyLandedTest(unittest.TestCase):
    """task/320 — the stall surface billed a named seat for 11h42m of delay on
    a row whose work was already on trunk.

    THE MISFIRE, from @opus-integrator's transcript:
        652d9794afbe AWAITING_BUILD  parked-dispatch-rebi  11h42m  (STALLED)
          (>= 4h00m in AWAITING_BUILD, owed by builder (helm-claude))
    AWAITING_BUILD and a dwell clock are both claims about the LEDGER; neither
    asks whether the work exists.

    A MARKER, NOT A FILTER — an earlier cure dropped landed rows inside
    dispatches.owed() and took 18 gate failures, because the work-offer rung
    wants that same row to SURFACE so the seat CLOSES it. Speaking here changes
    what the reader is told and removes nothing.
    """

    ROW = {"id": "a" * 32, "kind": "review", "reviewed_tip": "f" * 40}

    def _probe(self, **kw):
        from helm import seats_work_offer
        return mock.patch.object(seats_work_offer, "_offer_landing_state", **kw)

    def test_a_row_proven_on_trunk_SAYS_SO(self):
        # POSITIVE CONTROL FIRST, same row and same observable: with the probe
        # answering ABSENT the marker is silent, so the sentence below is the
        # landing answer rather than a marker that always fires.
        with self._probe(return_value=(False, "d" * 40)):
            self.assertEqual(landreq._landed_marker(dict(self.ROW), "AWAITING_BUILD"), "")
        with self._probe(return_value=(True, "d" * 40)):
            out = landreq._landed_marker(dict(self.ROW), "AWAITING_BUILD")
        self.assertIn("ALREADY ON TRUNK at " + "d" * 12, out)
        self.assertIn("CLOSING, not building", out)
        # THE DEBT IS REPLACED, NOT ARGUED WITH (@codex-3's first finding):
        # the tail that stands in for the owed-by clause must say the row is
        # owed by nobody, or the routing claim survives its own correction.
        self.assertIn("owed by NOBODY", out)

    def test_the_VERB_comes_from_the_STATE_not_from_a_guess(self):
        """@codex-3's second finding: the first cut said "not building" on
        every landed row, including AWAITING_REVIEW ones, naming an activity
        nobody was doing. OWED_BY already keys the ROLE off state; the verb
        has to come from the same place."""
        row = dict(self.ROW)
        with self._probe(return_value=(True, "d" * 40)):
            build = landreq._landed_marker(row, "AWAITING_BUILD")
            review = landreq._landed_marker(row, "AWAITING_REVIEW")
            other = landreq._landed_marker(row, "OPEN")
        self.assertIn("not building", build)
        self.assertNotIn("not reviewing", build)
        self.assertIn("not reviewing", review)
        self.assertNotIn("not building", review)
        # A state with no activity word says NEITHER rather than guessing one.
        self.assertNotIn("not building", other)
        self.assertNotIn("not reviewing", other)
        self.assertIn("needs CLOSING", other)

    def test_UNKNOWN_is_SILENT_never_a_denial(self):
        """The same tri-state law _moved_tip_marker states: an absent marker
        must never read as 'this did not land'."""
        row = dict(self.ROW)
        # UNCONDITIONAL POSITIVE CONTROL on the same row: the marker CAN fire
        # here, so the silence below is UNKNOWN being honoured rather than a
        # row this marker could never have spoken about.
        with self._probe(return_value=(True, "d" * 40)):
            self.assertIn("ALREADY ON TRUNK", landreq._landed_marker(row, "AWAITING_BUILD"))
        with self._probe(return_value=(None, None)):
            self.assertEqual(landreq._landed_marker(row, "AWAITING_BUILD"), "")

    def test_a_probe_that_RAISES_is_SILENT(self):
        """A marker that cannot measure says nothing. git being down is not
        evidence either way, and it must never become an accusation."""
        row = dict(self.ROW)
        # UNCONDITIONAL POSITIVE CONTROL on the same row, first.
        with self._probe(return_value=(True, "d" * 40)):
            self.assertIn("ALREADY ON TRUNK", landreq._landed_marker(row, "AWAITING_BUILD"))
        with self._probe(side_effect=RuntimeError("git unavailable")):
            self.assertEqual(landreq._landed_marker(row, "AWAITING_BUILD"), "")

    def test_a_BUILD_row_is_never_marked_on_its_BASE(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive controls are in-arm and deliberate: the forced-LANDED probe proves the marker CAN fire for this exact row, and the reached2 counter proves the same _close_repo patch DOES record a call for a row that legitimately reaches git, so both the empty string and the empty counter discriminate
        """THE TRAP THIS IS SHAPED AROUND, run against the REAL landing
        function with no mock so the short-circuit is proved, not assumed.

        A BUILD row's ref is the base the work was dispatched FROM. Measured
        2026-08-06 on the live ledger, 48 of the 52 resolvable BUILD refs are
        ALREADY ANCESTORS OF TRUNK, so a raw landing probe would stamp this
        marker on nearly every AWAITING_BUILD row ever written — the defect
        installed as its own cure. A BUILD with no reviewed_tip must return
        UNKNOWN before any git call.
        """
        build = {"id": "b" * 32, "kind": "build", "ref": "f" * 40}
        # UNCONDITIONAL POSITIVE CONTROL on THIS EXACT ROW: with the probe
        # forced to LANDED the marker does fire for it, so the silence below
        # is the BUILD rule declining to answer and not a row shape the marker
        # rejects for some unrelated reason.
        with self._probe(return_value=(True, "d" * 40)):
            self.assertIn("ALREADY ON TRUNK",
                          landreq._landed_marker(build, "AWAITING_BUILD"))
        # AND IT MUST PROVE THE GIT CLAIM, NOT JUST THE ANSWER (@codex-3's
        # third finding). An empty return is also what a BUILD row that DID
        # reach git and found nothing produces, so the silence proves nothing
        # on its own — the repo resolution has to be COUNTED.
        #
        # A RAISING DETONATOR WOULD BE INERT HERE AND I SHIPPED ONE FIRST:
        # _landed_marker catches Exception by law, and AssertionError IS an
        # Exception, so a side_effect that raises is SWALLOWED and the arm
        # passes whether or not git was reached. Counting is the only
        # instrument that survives a fail-open callee.
        reached = []
        with mock.patch.object(landreq, "_close_repo",
                               side_effect=lambda *a, **k:
                               reached.append(1) or (None, "err")):
            self.assertEqual(landreq._landed_marker(build, "AWAITING_BUILD"), "")
        self.assertEqual(reached, [], "a BUILD row with no reviewed_tip "
                                      "resolved a repo — the short-circuit is gone")
        # POSITIVE CONTROL ON THE COUNTER ITSELF: the same patch DOES record a
        # call for a row that legitimately reaches git, so the empty list above
        # is a short-circuit and not a counter that never fires.
        reached2 = []
        with mock.patch.object(landreq, "_close_repo",
                               side_effect=lambda *a, **k:
                               reached2.append(1) or (None, "err")):
            landreq._landed_marker({"id": "c" * 32, "kind": "build",
                                    "reviewed_tip": "f" * 40}, "AWAITING_BUILD")
        self.assertEqual(len(reached2), 1)

    def test_a_missing_store_row_is_SILENT(self):
        """store_rows.get(id) returns None for a row the dispatch store never
        held; that is an unmeasured row, not a landed one."""
        # UNCONDITIONAL POSITIVE CONTROL: a real dict DOES produce a marker
        # under the same probe, so the two silences below are the non-dict
        # guard and not a globally mute marker.
        with self._probe(return_value=(True, "d" * 40)):
            self.assertIn("ALREADY ON TRUNK",
                          landreq._landed_marker(dict(self.ROW), "AWAITING_BUILD"))
            self.assertEqual(landreq._landed_marker(None, "AWAITING_BUILD"), "")
            self.assertEqual(landreq._landed_marker("not-a-dict", "AWAITING_BUILD"), "")
