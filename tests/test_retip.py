#!/usr/bin/env python3
"""`helm dispatch retip` — re-pointing an OPEN row at a NEW TIP in place.

The mirror of `rebind`: that verb moves a row when the RECIPIENT went quiet,
this one when the BASE moved under a reviewer who is still there. Same row,
same recipient, same chain, one strict audit event, and every non-open row
refused because a verdict BINDS its tip.

MOVED WHOLE OUT OF `tests/test_dispatches.py` (task/2866), which stood 3,505
bytes under the never-track ceiling with more arms queued to land in it — and
which an arm of mine had already pushed PAST that ceiling once. No body was
rewritten on the way.

THE FIXTURE IS REUSED BY REFERENCE, NEVER COPIED. `DispatchBase` and `run` are
imported from the module these arms came from, so a change to the fixture
still reaches them and the two cannot drift. That is also why this class and
not its sibling: `RebindTest` owns the starvation rungs that
`BriefAndAuthorTravelWithTheObligationTest` deliberately BORROWS, so moving it
would either break that borrow or force those helpers out of the class that
defines them. `RetipTest` is borrowed from by nobody — measured, one
class-borrows-from-class edge in the whole file and it is not this one.
"""
import hashlib
import json
import os
import pathlib
import subprocess
from unittest import mock

from tests.test_dispatches import DispatchBase, run

from helm import dispatches, eventledger, home, landreq, vcs


class RetipTest(DispatchBase):
    """retip = re-point one OPEN row at a NEW TIP in place, one strict
    seq-ordered event — same row, same recipient, same chain. The mirror of
    rebind for the case where the BASE moved rather than the reviewer,
    measured six times in one day, once per land that moved trunk under an
    already-dispatched lane.

    DELIBERATELY NOT the cancel+add pair the first build of this verb was:
    a retip is not a ROUND, so it must not burn a chain hop, must not retire
    the row id the recipient is watching, and must not own a two-write crash
    window. One event, every refusal before it, and the seq-0 event keeps the
    original tip forever.

    EVERY ARM HERE MINTS ITS OWN ROW. The old verb's first exercise was run
    against a LIVE review row to see it refuse, and it did not refuse — it
    cancelled a real obligation. A destructive verb is exercised against a
    fixture or not at all."""

    def raw_events(self, rid, kind=None):
        return [e for e in eventledger.events(dispatches.ledger_path())
                if isinstance(e, dict) and e.get("id") == rid
                and (kind is None or e.get("event") == kind)]

    def reviewed_stack(self):
        self.git("checkout", "-q", "-b", "reviewed-stack", self.a)
        commits = []
        for i in range(2):
            name = "reviewed-%d.txt" % i
            with open(os.path.join(self.repo, name), "w",
                      encoding="utf-8") as f:
                f.write(name + "\n")
            self.git("add", name)
            self.git("commit", "-q", "-m", name)
            commits.append(self.git("rev-parse", "HEAD"))
        tip = commits[-1]
        self.git("checkout", "-q", self.main)
        return commits, tip

    def train_tip(self, branch, picks, flank=12):
        self.git("checkout", "-q", "-b", branch, self.a)
        for i in range(flank):
            name = "%s-before-%02d.txt" % (branch.replace("/", "-"), i)
            with open(os.path.join(self.repo, name), "w",
                      encoding="utf-8") as f:
                f.write(name + "\n")
            self.git("add", name)
            self.git("commit", "-q", "-m", name)
        for i, commit in enumerate(picks):
            if flank:
                self.git("cherry-pick", commit)
            else:
                self.git("cherry-pick", "--no-commit", commit)
                self.git("commit", "-q", "-m", "rewritten reviewed %d" % i)
        for i in range(flank):
            name = "%s-after-%02d.txt" % (branch.replace("/", "-"), i)
            with open(os.path.join(self.repo, name), "w",
                      encoding="utf-8") as f:
                f.write(name + "\n")
            self.git("add", name)
            self.git("commit", "-q", "-m", name)
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        return tip

    def historical_proof(self, event):
        """The round-2 `proof` recipe, inlined AS THE FORGER'S TOOL: blake2b-16
        over canonical JSON minus the proof field, domain retip-proof-v1. It
        lives only in this test file now — the round-3 cure is that computing
        it grants nothing, and these arms prove that by wielding it."""
        raw = json.dumps({k: v for k, v in event.items() if k != "proof"},
                         sort_keys=True, ensure_ascii=True,
                         separators=(",", ":"))
        return hashlib.blake2b(("retip-proof-v1\0" + raw).encode("utf-8"),
                               digest_size=16).hexdigest()

    def corrupt_supersedes(self, rid):
        """Malform one row's STORED supersedes on disk — the reviewer's
        unreadable-successor fixture. Real code never rewrites the append-only
        ledger; after this, `_replay_chain` reads the field as CHAIN_UNKNOWN.
        Every caller asserts that MUST-HIT itself before trusting a refusal."""
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for row in events:
                if row.get("id") == rid and row.get("event") == "dispatch":
                    row["supersedes"] = "not-a-chain!"
                f.write(json.dumps(row, separators=(",", ":")) + "\n")

    def test_retip_moves_the_ref_appends_the_event_and_keeps_history(self):
        """THE CONTRACT, all three halves on one move: the ref MOVES (same row
        id, new tip), the EVENT is appended (a strict v3 retip row on the
        ledger), and the OLD ref is PRESERVED — both in the projection's
        `retips` history and in the untouched seq-0 event. Chain identity does
        not move: no successor, no new chain hop, same root. BUILD kind,
        deliberately: a plain a->b trunk hop is a legitimate FORWARD base move
        for a build, while for a review it is different code — the first cut
        of this test rode the vacuous empty/empty "verified" (codex P1) by
        retipping a default-kind row between two trunk commits."""
        row = self.add(recipient="grok", kind="build")
        out, err = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["id"], row["id"], "retip must not mint a new row")
        self.assertEqual(out["tip"], self.b)
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(live["status"], "open")
        self.assertEqual(live["tip"], self.b)
        self.assertEqual([h["old_tip"] for h in live["retips"]], [self.a])
        self.assertEqual(live["retips"][0]["reason"], "trunk moved")
        self.assertEqual(live["retips"][0]["identity"], "verified",
                         "identity must survive a FRESH snapshot — a stamp "
                         "that lives only in the writer's return is transient")
        self.assertEqual(live.get("chain_root"), row["id"],
                         "a base move is not a round: the chain must not move")
        self.assertIsNone(live.get("supersedes"))
        events = self.raw_events(row["id"], "retip")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["old_tip"], self.a)
        self.assertEqual(events[0]["tip"], self.b)
        self.assertEqual(events[0]["identity"], "verified")
        seq0 = self.raw_events(row["id"], "dispatch")
        self.assertEqual([e["tip"] for e in seq0], [self.a],
                         "history is append-only: the original tip stays")

    def test_a_row_carrying_a_VERDICT_refuses_to_be_retipped(self):
        """A verdict BINDS the tip it was written against; re-pointing a
        verdicted row would retarget a reviewer's binding onto code they never
        read — the thing a review refused a countersign over. The status check
        IS the verdict check; this arm stops a later reader relaxing it as
        tidiness. And the binding runs BOTH directions: after a retip, a
        verdict against the OLD tip is refused as stale."""
        row = self.add(recipient="grok", kind="build")
        # CONTROL, unconditional and first: while OPEN the verb DOES move it.
        out, err = dispatches.retip(row["id"], self.b, reason="base moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        # The verdict must bind the CURRENT tip: the old one is stale now.
        _v, stale = dispatches.mark_verdict(row["id"], self.a, "reviewed", "fix")
        self.assertIn("stale", stale)
        # FIX polarity, not approve: an approve requires a verified gate token
        # and this arm is about the TIP BINDING, not gate machinery.
        _v, why = dispatches.mark_verdict(row["id"], self.b, "reviewed", "fix")
        self.assertIsNone(why, why)
        out2, err2 = dispatches.retip(row["id"], self.c, reason="try to move",
                                      repo=self.repo, notify=False)
        self.assertIsNone(out2)
        self.assertIn("only an OPEN row", err2)
        self.assertIn("verdict", err2)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["tip"], self.b)

    def test_a_CANCELLED_row_refuses_too(self):
        row = self.add(recipient="grok")
        _c, cerr = dispatches.mark_cancel(row["id"], "work moot")
        self.assertIsNone(cerr, cerr)
        out, err = dispatches.retip(row["id"], self.b, reason="x",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("only an OPEN row", err)

    def test_a_hand_appended_retip_event_after_a_verdict_is_INERT(self):
        """The replay arm enforces the same gate as the writer: a well-shaped
        retip event appended around the writer (hand edit, forged append)
        lands after the verdict and must not move the reviewed tip."""
        row = self.add(recipient="grok")
        _v, why = dispatches.mark_verdict(row["id"], self.a, "reviewed", "fix")
        self.assertIsNone(why, why)
        seq = dispatches.snapshot()[0][row["id"]]["seq"]
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "retip", "seq": seq + 1, "id": row["id"],
            "ts": dispatches.pk.now_ts(), "tip": self.b, "ref": self.b,
            "old_tip": self.a, "reason": "forged", "identity": "verified"}))
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(live["status"], "verdict")
        self.assertEqual(live["tip"], self.a)
        self.assertNotIn("retips", live)

    def test_a_retip_event_that_misnames_the_old_tip_is_INERT(self):
        """The event must NAME the tip it moves. Strict seq already orders
        events; binding each hop to its predecessor makes a spliced or
        out-of-context retip inert instead of silently applied."""
        row = self.add(recipient="grok")
        seq = dispatches.snapshot()[0][row["id"]]["seq"]
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "retip", "seq": seq + 1, "id": row["id"],
            "ts": dispatches.pk.now_ts(), "tip": self.b, "ref": self.b,
            "old_tip": self.c, "reason": "spliced", "identity": "verified"}))
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(live["tip"], self.a, "a hop from a tip the row is "
                         "not at must not apply")
        self.assertEqual(live["status"], "open")

    def test_retip_to_the_SAME_tip_refuses_rather_than_churning(self):
        row = self.add(recipient="grok")
        out, err = dispatches.retip(row["id"], self.a, reason="x",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("already names", err)

    def test_retip_without_a_reason_refuses(self):
        """The recipient is being asked to RE-READ. 'Why' is the difference
        between a rebase and a rewrite, and it travels on the event."""
        row = self.add(recipient="grok")
        out, err = dispatches.retip(row["id"], self.b, reason="  ",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("reason", err)

    def test_a_ref_THAT_DOES_NOT_RESOLVE_changes_NOTHING(self):
        """THE DESTRUCTIVE BUG of the verb's first build, kept as the pin: a
        typo'd ref must be indistinguishable from a no-op, never from a
        cancel. In the one-event design there is nothing to strand — but this
        arm still proves the refusal happens before the write."""
        row = self.add(recipient="grok")
        out, err = dispatches.retip(row["id"], "deadbeefdeadbeefdeadbeef",
                                    reason="typo", repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("does not resolve", err)
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual((live["status"], live["tip"]), ("open", self.a))
        # The MUST-HIT positive on the same read that proves the absence: the
        # seq-0 dispatch event is the ONLY event this row carries.
        self.assertEqual([e["event"] for e in self.raw_events(row["id"])],
                         ["dispatch"])

    def test_an_UNRELATED_commit_cannot_inherit_the_brief(self):
        """retip moves an obligation to a new BASE, never to different code,
        and the recipient's brief — written about the old sequence — cannot be
        silently pointed at another lane's.

        THE CANDIDATE MUST GENUINELY DIVERGE or this arm is not about
        unrelated code at all. The fixture it replaces pointed a row on trunk
        `a` at `side`, which DESCENDS from `a`: the refusal it drew was the
        one about stacking new work on a reviewed car, so the arm named an
        unrelated commit and measured a stacked one. Descent has its own arm
        now; this one pins the divergent door, and asserts the divergence
        before it asserts anything about the refusal."""
        reviewed = self._lane_tip(self.a, "brief-reviewed",
                                  body="reviewed work\n", path="reviewed.txt")
        unrelated = self._lane_tip(self.c, "brief-unrelated",
                                   body="someone else's work\n",
                                   path="unrelated.txt")
        state, _start, reviewed_n, train_n = \
            vcs.backend(self.repo).patch_sequence_containment(
                self.repo, reviewed, unrelated)
        self.assertEqual((state, reviewed_n, train_n),
                         (vcs.PATCH_SEQUENCE_ABSENT, 1, 3),
                         "the fixture's whole point: a DIVERGENT candidate, "
                         "with BOTH lengths real — a descendant collapses one "
                         "of them to zero and draws a different refusal")
        row = self.add(recipient="grok", kind="review", ref=reviewed)
        out, err = dispatches.retip(row["id"], unrelated,
                                    reason="wrong commit", repo=self.repo,
                                    notify=False)
        self.assertIsNone(out)
        self.assertIn("does not carry the work reviewed at", err)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["tip"], reviewed)

    def test_the_CONTROL_a_genuine_REBASE_is_verified_and_accepted(self):
        """The half that must NOT change, and the reason the identity is
        patch-id rather than sha: a rebase changes every sha and no patch. If
        this arm went red the check would have broken the exact case retip
        exists for while looking like it had tightened something."""
        def lane_tip(base, name):
            self.git("checkout", "-q", "-b", name, base)
            with open(os.path.join(self.repo, "lane.txt"), "w",
                      encoding="utf-8") as f:
                f.write("the lane's own work\n")
            self.git("add", "lane.txt")
            self.git("commit", "-q", "-m", "lane work")
            tip = self.git("rev-parse", "HEAD")
            self.git("checkout", "-q", self.main)
            self.git("clean", "-qfd")
            return tip

        old_tip = lane_tip(self.a, "lane-before")
        rebased = lane_tip(self.c, "lane-after")
        self.assertTrue(old_tip and rebased, "fixture must mint both tips")
        self.assertNotEqual(rebased, old_tip, "fixture must actually rebase")
        row = self.add(recipient="grok", kind="review", ref=old_tip)
        out, err = dispatches.retip(row["id"], rebased, reason="trunk moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["identity"], "verified")
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(live["tip"], rebased)
        self.assertEqual(live["retips"][0]["old_tip"], old_tip)

    def test_a_review_retip_finds_its_two_patch_car_inside_a_26_car_train(self):
        commits, reviewed = self.reviewed_stack()
        exact = self.train_tip("integration-exact", commits, flank=0)
        train = self.train_tip("integration-train", commits)
        exact_row = self.add(recipient="grok", kind="review", ref=reviewed)
        exact_out, exact_err = dispatches.retip(
            exact_row["id"], exact, reason="car rewritten",
            repo=self.repo, notify=False)
        self.assertIsNone(exact_err, exact_err)
        self.assertEqual(exact_out["identity"], "verified",
                         "the existing equal-length path must still agree")
        row = self.add(recipient="grok", kind="review", ref=reviewed)
        out, err = dispatches.retip(row["id"], train, reason="car composed",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["identity"], "verified")
        self.assertEqual(out["tip"], train)
        state = vcs.backend(self.repo).patch_sequence_containment(
            self.repo, reviewed, train)
        self.assertEqual(state, (vcs.PATCH_SEQUENCE_CONTAINED, 12, 2, 26))

    def test_a_review_retip_refuses_UNKNOWN_without_changing_the_obligation(self):
        commits, reviewed = self.reviewed_stack()
        candidate = self.train_tip("identity-retry", commits, flank=0)
        row = self.add(recipient="grok", kind="review", ref=reviewed)
        before = pathlib.Path(dispatches.ledger_path()).read_bytes()
        backend = vcs.backend(row["repo_id"])
        # Only the proof instrument is unavailable; the public writer, real
        # commits and ledger remain intact. A later successful retry proves
        # the same request was not stopped at an unrelated earlier guard.
        with mock.patch.object(type(backend), "patch_sequence_containment",
                               return_value=(vcs.PATCH_SEQUENCE_UNKNOWN,
                                             None, None, None)) as proof:
            out, err = dispatches.retip(
                row["id"], candidate, reason="same work on a new base",
                repo=self.repo, notify=False)
            proof.assert_called_once_with(row["repo_id"], reviewed, candidate)
        self.assertIsNone(out)
        self.assertIn("identity UNKNOWN", err)
        self.assertIn("patch-id sequence", err)
        self.assertIn("--supersedes " + row["id"][:12], err)
        # MISSING PROOF IS NOT A MISMATCH, and the guard names the three
        # sentences the review arm can ACTUALLY print. A negative keyed on a
        # phrase no refusal emits can never go red, so it reads as coverage
        # while covering nothing — these three are the live openers, and the
        # arm fails the moment UNKNOWN is rendered as any of them.
        self.assertNotIn("does not carry the work reviewed at", err)
        self.assertNotIn("DESCENDS from the reviewed tip", err)
        self.assertNotIn("is an ANCESTOR of the reviewed tip", err)
        self.assertEqual(pathlib.Path(dispatches.ledger_path()).read_bytes(),
                         before)
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual((live["status"], live["tip"]), ("open", reviewed))
        self.assertEqual([e["event"] for e in self.raw_events(row["id"])],
                         ["dispatch"])

        out, err = dispatches.retip(
            row["id"], candidate, reason="same work on a new base",
            repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        self.assertEqual((out["id"], out["tip"], out["identity"]),
                         (row["id"], candidate, "verified"))
        events = self.raw_events(row["id"], "retip")
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["old_tip"], events[0]["tip"],
                          events[0]["identity"]),
                         (reviewed, candidate, "verified"))

    def test_a_review_retip_refuses_a_train_missing_one_reviewed_patch(self):
        commits, reviewed = self.reviewed_stack()
        train = self.train_tip("integration-missing", commits[:1])
        row = self.add(recipient="grok", kind="review", ref=reviewed)
        out, err = dispatches.retip(row["id"], train,
                                    reason="incomplete composition",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("does not appear as one contiguous run", err)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["tip"], reviewed)

    def _stack(self, base, name, n):
        """An `n`-commit lane on `base`, every sha returned in order."""
        self.git("checkout", "-q", "-b", name, base)
        shas = []
        for i in range(n):
            path = "%s-%02d.txt" % (name, i)
            with open(os.path.join(self.repo, path), "w",
                      encoding="utf-8") as f:
                f.write(path + "\n")
            self.git("add", path)
            self.git("commit", "-q", "-m", path)
            shas.append(self.git("rev-parse", "HEAD"))
        self.git("checkout", "-q", self.main)
        self.git("clean", "-qfd")
        return shas

    def test_an_APPEND_refusal_names_the_NEW_TIP_and_what_it_ADDED(self):
        """A REFUSAL IS ROUTING INFORMATION, so it must name the input that
        FAILED and the value that input had. An append refuses through the
        EMPTY reviewed-sequence door, and the sentence that door printed
        described the MEASUREMENT instead: "the old 0-commit patch-id sequence
        is empty, so it identifies no reviewed work in the new 1-commit
        history". Both counts are lengths from the two tips' COMMON BASE,
        which for an append IS the reviewed tip — so the reviewed tip's own
        count collapses to zero while the tip is perfectly intact. Every
        reader who went to check the accused sequence found it fine and
        concluded the instrument was broken; the accusation belonged to the
        tip handed to --ref, which had stacked new commits on the reviewed
        car.

        THE FIXTURE MAKES THE COLLAPSE MAXIMALLY VISIBLE: the reviewed tip is
        3 commits off trunk and --ref is 5, so 0 and 2 are the only numbers in
        play and neither of the old sentence's is one a reader can reproduce.
        """
        lane = self._stack(self.a, "append-lane", 5)
        reviewed, appended_by_two, appended_by_one = lane[2], lane[4], lane[3]
        # THE PREMISE, AND IT IS THE TELL FOR THIS WHOLE CLASS: the subject
        # the old refusal accused is INDEPENDENTLY FINE. If this ever reads 0
        # the arm below would be asserting against a genuinely empty car.
        self.assertEqual(
            self.git("rev-list", "--count", self.a + ".." + reviewed), "3",
            "the reviewed tip carries three commits, so a sentence calling "
            "its sequence empty sends the reader to a healthy artifact")
        row = self.add(recipient="grok", kind="review", ref=reviewed)
        out, err = dispatches.retip(row["id"], appended_by_two,
                                    reason="lane grew", repo=self.repo,
                                    notify=False)
        self.assertIsNone(out)
        # THE FAILING INPUT, ITS VALUE, AND THE DOOR OUT — all unconditional,
        # and they are also the positive controls for the two absences below,
        # measured on this same `err`.
        self.assertIn("--ref " + appended_by_two[:12], err)
        self.assertIn("DESCENDS from the reviewed tip " + reviewed[:12], err)
        self.assertIn("adding 2 commit", err)
        self.assertIn("--supersedes " + row["id"][:12], err)
        self.assertNotIn("identifies no reviewed work", err,
                         "the reviewed work is intact; accusing it is the "
                         "defect this arm exists for")
        self.assertNotIn("0-commit", err,
                         "a count of zero here is an artifact of the base the "
                         "measurement chose, never a fact about either tip")
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["tip"], reviewed)
        # MUST-HIT: the count is DERIVED, not decoration. A one-commit append
        # on the SAME reviewed tip must move it, so a constant — or a number
        # read off the wrong side — fails one of these two arms.
        other = self.add(recipient="grok", kind="review", ref=reviewed)
        _out2, err2 = dispatches.retip(other["id"], appended_by_one,
                                       reason="lane grew less",
                                       repo=self.repo, notify=False)
        self.assertIn("adding 1 commit", err2)
        self.assertNotIn("adding 2 commit", err2)

    def test_a_TRUNCATING_retip_names_the_commits_it_would_DROP(self):
        """THE SIBLING ONE LINE OVER, failing the same way in the mirror
        direction — and curing the append while leaving this is how this class
        survives its own fix. When --ref is an ANCESTOR of the reviewed tip
        the common base IS --ref, so it is the TRAIN that collapses to zero
        and the sentence read "does not appear as one contiguous run in the
        new 0-commit history" about a tip carrying three commits. The reader
        it misroutes is the one who checks: --ref is fine, the reviewed
        sequence is fine, and the actual fault — that the move would shrink
        the dispatched work — is named nowhere."""
        lane = self._stack(self.a, "shrink-lane", 5)
        reviewed, shorter = lane[4], lane[2]
        # THE PREMISE: the tip the old sentence called a 0-commit history is
        # independently three commits long.
        self.assertEqual(
            self.git("rev-list", "--count", self.a + ".." + shorter), "3",
            "--ref carries three commits, so calling its history 0-commit "
            "describes the measurement's base and not the tip")
        row = self.add(recipient="grok", kind="review", ref=reviewed)
        out, err = dispatches.retip(row["id"], shorter, reason="rolled back",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("--ref " + shorter[:12], err)
        self.assertIn("is an ANCESTOR of the reviewed tip " + reviewed[:12],
                      err)
        self.assertIn("dropping the 2 commit", err)
        self.assertIn("--supersedes " + row["id"][:12], err)
        self.assertNotIn("0-commit", err,
                         "the zero is the base the measurement chose")
        self.assertNotIn("does not appear as one contiguous run", err,
                         "a truncation is not a scrambled car, and sending "
                         "its author to look for one costs the whole round")
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["tip"], reviewed)

    def test_a_DIVERGENT_refusal_names_the_BASE_its_counts_come_from(self):
        """THE THIRD FACE, and the one that survives as a count. With neither
        tip an ancestor of the other both numbers are real commit counts — but
        they are measured from the PAIR'S common base, which is the base of
        NEITHER tip. So a reader who measures --ref against the trunk it was
        actually cut from gets a different number for a refusal that is
        correct, which is the same wrong-layer trip by a smaller margin. The
        sentence names the base, and this arm pins the gap that makes naming
        it load-bearing."""
        reviewed = self._lane_tip(self.a, "diverge-reviewed",
                                  body="reviewed work\n", path="reviewed.txt")
        candidate = self._lane_tip(self.c, "diverge-candidate",
                                   body="someone else's work\n",
                                   path="unrelated.txt")
        base = self.git("merge-base", reviewed, candidate)
        row = self.add(recipient="grok", kind="review", ref=reviewed)
        out, err = dispatches.retip(row["id"], candidate, reason="wrong tip",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("does not carry the work reviewed at " + reviewed[:12],
                      err)
        self.assertIn("common base " + base[:12], err)
        # THE GAP, MEASURED: the sentence says three commits; --ref is ONE
        # commit off the trunk it was cut from. Both are true, and without
        # the base the reader cannot tell which question was asked.
        self.assertIn("3-commit history", err)
        self.assertEqual(
            self.git("rev-list", "--count", self.c + ".." + candidate), "1",
            "--ref is one commit off its OWN base while the refusal counts "
            "three from the pair's base — naming the base is what makes the "
            "number checkable")
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["tip"], reviewed)

    def setUp(self):
        super().setUp()
        # EVERY ROW IN THIS CLASS IS DISPATCHED UNDER A DECLARED AUTHORITY,
        # because that is the shape a non-FF BUILD retip requires and the
        # fixture would otherwise be testing only the legacy-UNKNOWN path. The
        # arms that need NO binding, or a broken one, say so themselves.
        self.git("config", "helm.trunkRef", "refs/heads/" + self.main)

    def _undeclare_authority(self):
        """Drop the declaration, so a row added after this carries no binding
        — the legacy shape, by construction rather than by age."""
        subprocess.run(["git", "-C", self.repo, "config", "--unset",
                        "helm.trunkRef"], capture_output=True, text=True)
        subprocess.run(["git", "-C", self.repo, "config", "--unset",
                        "helm.trunkRemote"], capture_output=True, text=True)

    def _ref_exists(self, ref):
        return subprocess.run(
            ["git", "-C", self.repo, "rev-parse", "--verify", "--quiet", ref],
            capture_output=True, text=True).returncode == 0

    def _is_ancestor(self, commit, ref):
        """git's own answer, not self.git — that helper is check=True and a
        genuine non-ancestor exits 1."""
        return subprocess.run(
            ["git", "-C", self.repo, "merge-base", "--is-ancestor",
             commit, ref], capture_output=True, text=True).returncode == 0

    def _lane_tip(self, base, name, body="the lane's own work\n",
                  path="lane.txt"):
        """A one-commit lane on `base`, left checked out nowhere."""
        self.git("checkout", "-q", "-b", name, base)
        with open(os.path.join(self.repo, path), "w", encoding="utf-8") as f:
            f.write(body)
        self.git("add", path)
        self.git("commit", "-q", "-m", "lane work on " + name)
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.git("clean", "-qfd")
        return tip

    def test_a_BUILD_base_moves_FORWARD_or_by_a_PROVEN_REBASE(self):
        """A build row's ref is a BASE, so a DESCENDANT verifies on ancestry
        alone. The half this arm gained: a non-descendant is no longer refused
        on sight. It falls through to the per-commit sequence test, and only
        genuinely DIFFERENT work is turned away."""
        row = self.add(recipient="grok", kind="build")
        out, err = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["identity"], "verified")
        sideways = self.add(recipient="grok", kind="build", ref=self.b)
        out2, err2 = dispatches.retip(sideways["id"], self.side,
                                      reason="hop lanes", repo=self.repo,
                                      notify=False)
        self.assertIsNone(out2)
        self.assertIn("does not descend", err2)
        self.assertIn("did not produce a readable tree", err2,
                      "the refusal must name WHICH test rejected them")

    def test_a_BUILD_row_accepts_a_PROVEN_REBASE_of_the_same_work(self):
        """THE DEFECT THIS LANE EXISTS FOR (task/1463). Rebasing onto the new
        trunk before gating is the standing fold procedure, and a rebase can
        NEVER produce a descendant — so ancestry-only made the one move every
        author must perform the one move the verb could not express, costing a
        cancel-and-re-cut per land."""
        old_tip = self._lane_tip(self.a, "build-lane-before")
        rebased = self._lane_tip(self.c, "build-lane-after")
        self.assertNotEqual(rebased, old_tip, "fixture must actually rebase")
        self.assertFalse(self._is_ancestor(old_tip, rebased),  # noqa: VACUOUS_ASSERTION — a FIXTURE precondition, and the negative IS the contract: this arm only reaches the branch it pins when the two tips are NOT in an ancestor relation
                         "fixture must be a REBASE, not a fast-forward")
        row = self.add(recipient="grok", kind="build", ref=old_tip)
        # UNCONDITIONAL POSITIVE CONTROL on the SAME observable, first: this
        # fixture CAN produce a refusal, so the assertIsNone below is a fact
        # about the rebase and not about a verb that never says no here.
        control = self.add(recipient="grok", kind="build", ref=old_tip)
        _c, control_err = dispatches.retip(control["id"], self.side,
                                           reason="unrelated work",
                                           repo=self.repo, notify=False)
        self.assertIsNotNone(control_err, "control: err must be reachable")
        out, err = dispatches.retip(row["id"], rebased, reason="trunk moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["tip"], rebased)
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(live["tip"], rebased,
                         "the move must survive a fresh snapshot")
        self.assertEqual(live["retips"][0]["identity"], "verified")
        self.assertEqual(live.get("chain_root"), row["id"],
                         "a rebase is not a round: the chain must not move")

    def test_two_BUILD_tips_that_both_sit_on_trunk_never_verify(self):
        """THE HAZARD THE ACCEPTING PATH IS DESIGNED AROUND, pinned so a later
        reader cannot relax it as symmetry with the review arm. There the two
        tips ARE the reviewed artifact, so their own patches can still decide.
        Here the tip is a BASE nobody reviewed: two empty ranges say only
        "both sit on trunk", and blessing that lets ANY trunk commit stand in
        for any other. The fixture merges `side` into trunk so both tips are
        trunk ancestors while neither descends from the other — the only shape
        that reaches this branch."""
        # -X ours: `side` and trunk both append to the fixture's one
        # `state` file, so a plain merge conflicts. The strategy picks a
        # content winner and changes nothing this arm measures — what it
        # needs is the PARENTAGE, asserted below.
        self.git("merge", "-q", "--no-ff", "-X", "ours", "side",
                 "-m", "merge side")
        self.assertEqual(len(self.git("rev-list", "--parents", "-1",
                                      "HEAD").split()) - 1, 2,
                         "the fixture needs a real two-parent merge")
        self.assertTrue(self._is_ancestor(self.side, "HEAD"),
                        "fixture must put BOTH tips on trunk")
        self.assertFalse(self._is_ancestor(self.c, self.side),  # noqa: VACUOUS_ASSERTION — same fixture precondition, covered by the assertTrue on the line above it
                         "and neither may descend from the other, or this "
                         "arm never reaches the branch it pins")
        # UNCONDITIONAL POSITIVE CONTROL on the SAME observable, first: a
        # legitimate forward move on this very fixture returns a row, so the
        # assertIsNone below means REFUSED and not "retip returns None here".
        control = self.add(recipient="grok", kind="build", ref=self.a)
        control_out, _e = dispatches.retip(control["id"], self.b,
                                           reason="trunk moved",
                                           repo=self.repo, notify=False)
        self.assertIsNotNone(control_out, "control: out must be reachable")
        row = self.add(recipient="grok", kind="build", ref=self.c)
        out, err = dispatches.retip(row["id"], self.side, reason="hop",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("did not produce a readable tree", err)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["tip"], self.c,
                         "a refused retip must leave the row where it was")

    def test_a_REBASE_that_DROPPED_a_commit_is_not_the_same_work(self):
        """Equal LENGTH is not the test and neither is equal content at the
        tip: a lane that lost a commit while being rebased carries a shorter
        sequence, and the recipient's brief was written about the longer one.
        Without this the accepting path would wave through a lane that
        silently shed work during the rebase."""
        self.git("checkout", "-q", "-b", "two-before", self.a)
        for name in ("first.txt", "second.txt"):
            with open(os.path.join(self.repo, name), "w",
                      encoding="utf-8") as f:
                f.write(name + "\n")
            self.git("add", name)
            self.git("commit", "-q", "-m", "add " + name)
        old_tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.git("clean", "-qfd")
        dropped = self._lane_tip(self.c, "two-after", body="second.txt\n",
                                 path="second.txt")
        row = self.add(recipient="grok", kind="build", ref=old_tip)
        out, err = dispatches.retip(row["id"], dropped, reason="rebased",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("produces tree", err,
                      "a dropped commit makes the replay conflict")
        self.assertNotIn("not the same work", err,
                         "and must NOT turn a structural fact into a verdict "
                         "about the work — see the merge arm, where the trees "
                         "are identical and the lengths still differ")

    def test_the_rebase_WARNING_reaches_the_recipient_and_a_ff_stays_quiet(self):
        """A PROVEN REBASE IS STILL A CHANGE THE RECIPIENT MUST SEE: the
        patches are identical and THE TREE UNDER THEM IS NOT, so work in
        progress against the old base may no longer apply. The stamp stays the
        two-value enum the fold accepts — widening it would make an older
        reader drop the hop in silence — so the fact travels in the
        notification. The fast-forward half is the must-stay-quiet control: if
        both moves shouted, the shout would carry no information."""
        old_tip = self._lane_tip(self.a, "warn-before")
        rebased = self._lane_tip(self.c, "warn-after")
        row = self.add(recipient="grok", kind="build", ref=old_tip)
        with mock.patch.object(dispatches, "_notify_public") as told:
            out, err = dispatches.retip(row["id"], rebased, reason="trunk",
                                        repo=self.repo, notify=True)
        self.assertIsNone(err, err)
        self.assertTrue(told.call_args, "the recipient must be told at all")
        said = told.call_args[0][1]
        self.assertIn("REBASED", said)
        self.assertIn("TREE UNDER IT IS NOT", said)

        forward = self.add(recipient="grok", kind="build", ref=self.a)
        with mock.patch.object(dispatches, "_notify_public") as told_ff:
            out2, err2 = dispatches.retip(forward["id"], self.b,
                                          reason="trunk moved",
                                          repo=self.repo, notify=True)
        self.assertIsNone(err2, err2)
        self.assertNotIn("REBASED", told_ff.call_args[0][1],
                         "a fast-forward replaces no tree and must not "
                         "borrow the rebase warning")

    def test_a_BACKWARD_base_with_identical_patches_is_REFUSED(self):
        """Measured: equal patch-ids prove the WORK is the same and
        say NOTHING about which trunk it sits on. Retipping from the rebased
        spelling BACK to the pre-rebase one has an identical sequence and
        hands the recipient an older tree under a `verified` stamp."""
        old_spelling = self._lane_tip(self.a, "back-before")
        new_spelling = self._lane_tip(self.c, "back-after")
        # CONTROL, unconditional, same observable: the FORWARD direction of
        # this very pair is accepted, so the refusal below is about DIRECTION.
        fwd = self.add(recipient="grok", kind="build", ref=old_spelling)
        fwd_out, fwd_err = dispatches.retip(fwd["id"], new_spelling,
                                            reason="forward",
                                            repo=self.repo, notify=False)
        self.assertIsNone(fwd_err, fwd_err)
        self.assertIsNotNone(fwd_out)
        row = self.add(recipient="grok", kind="build", ref=new_spelling)
        out, err = dispatches.retip(row["id"], old_spelling,
                                    reason="going backwards",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("produces tree", err)
        self.assertIn("produces tree", err,
                      "a backward move whose trunk content differs fails "
                      "the CONTENT check before direction is reached")

    def test_a_HISTORY_ONLY_REORDER_is_accepted_for_a_BUILD_row(self):
        """THE OTHER ARM THAT CHANGED SIDES, and the pair with the merge arm
        is the whole BUILD-vs-REVIEW distinction. Two commits swapped with a
        byte-identical FINAL STATE deliver the recipient exactly the same base,
        so a build row accepts. The ordered per-commit test refused it — right
        for a REVIEW row, whose artifact IS the history and where a reordered
        lane is genuinely not what the reviewer read, and wrong here. A reorder
        that CHANGES the final delta still refuses, which is the drop arm."""
        self.git("checkout", "-q", "-b", "order-before", self.a)
        for name in ("one.txt", "two.txt"):
            with open(os.path.join(self.repo, name), "w",
                      encoding="utf-8") as f:
                f.write(name + "\n")
            self.git("add", name)
            self.git("commit", "-q", "-m", "add " + name)
        first, second = (self.git("rev-parse", "HEAD~1"),
                         self.git("rev-parse", "HEAD"))
        old_tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.git("clean", "-qfd")
        self.git("checkout", "-q", "-b", "order-after", self.c)
        for commit in (second, first):
            self.git("cherry-pick", commit)
        reordered = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.git("clean", "-qfd")
        row = self.add(recipient="grok", kind="build", ref=old_tip)
        out, err = dispatches.retip(row["id"], reordered, reason="reordered",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["tip"], reordered)
        self.assertTrue(out["retips"][-1]["base_replaced"],
                        "the fork point moved, so the recipient is still told")

    def test_the_base_replacement_SURVIVES_a_notify_failure(self):  # noqa: VACUOUS_ASSERTION — assertRaises IS the contract here (the post must die AFTER the append); the persisted flag is asserted positively on the replayed row
        """The durability finding, reproduced: with the warning living
        only in the notification, a post that failed AFTER the append lost
        it permanently and an exact retry reconciled silently onto the
        committed write. The flag is on the EVENT now, so a fresh snapshot —
        the thing a later reader replays — still carries it."""
        old_tip = self._lane_tip(self.a, "durable-before")
        rebased = self._lane_tip(self.c, "durable-after")
        row = self.add(recipient="grok", kind="build", ref=old_tip)
        with mock.patch.object(dispatches, "_notify_public",
                               side_effect=RuntimeError("post died")):
            with self.assertRaises(RuntimeError):
                dispatches.retip(row["id"], rebased, reason="trunk moved",
                                 repo=self.repo, notify=True)
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(live["tip"], rebased,
                         "the append committed before the post died")
        self.assertTrue(live["retips"][-1]["base_replaced"],
                        "the replacement must survive the lost notification")

    def test_a_FAST_FORWARD_records_no_base_replacement(self):
        """The must-stay-quiet control for the flag: if a forward move also
        recorded a replacement, the flag would carry no information and every
        reader would learn to ignore it."""
        row = self.add(recipient="grok", kind="build")
        out, err = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        live = dispatches.snapshot()[0][row["id"]]
        # POSITIVE CONTROL on the same observable, unconditional: the row
        # really did move, so the False below is the FLAG being quiet rather
        # than a hop that never happened.
        self.assertEqual(live["tip"], self.b)
        self.assertEqual(len(live["retips"]), 1)
        self.assertFalse(live["retips"][-1]["base_replaced"])

    def test_a_WHITESPACE_ONLY_REINDENT_is_not_the_same_base(self):  # noqa: VACUOUS_ASSERTION — a refusal arm whose premise assertion is that patch-id CANNOT tell these apart, which is the measurement rather than an absence
        """`--stable` STRIPS WHITESPACE — that is most of how it
        survives a rebase — so a Python change indented two spaces and the
        same change indented four share ONE id. In this language that is a
        semantic difference, and a base is accepted on this comparison."""
        self.git("checkout", "-q", "-b", "ws-before", self.a)
        with open(os.path.join(self.repo, "m.py"), "w", encoding="utf-8") as f:
            f.write("def f():\n    return 1\n")
        self.git("add", "m.py")
        self.git("commit", "-q", "-m", "four spaces")
        old_tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.git("clean", "-qfd")
        self.git("checkout", "-q", "-b", "ws-after", self.c)
        with open(os.path.join(self.repo, "m.py"), "w", encoding="utf-8") as f:
            f.write("def f():\n  return 1\n")
        self.git("add", "m.py")
        self.git("commit", "-q", "-m", "two spaces")
        reindented = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.git("clean", "-qfd")
        from helm import landreq
        gitdir = os.path.join(self.repo, ".git")
        # THE PREMISE, kept and re-pointed: `--stable` patch-id cannot tell a
        # reindent from a no-op, which is why BUILD identity left patch-id
        # entirely rather than reaching for a stronger hash. The arm now proves
        # the REPLAY refuses it, and the patch-id equality below is the reason
        # the replay had to exist.
        self.assertEqual(landreq._patch_id(gitdir, old_tip),
                         landreq._patch_id(gitdir, reindented),
                         "the premise: patch-id cannot tell these apart")
        row = self.add(recipient="grok", kind="build", ref=old_tip)
        out, err = dispatches.retip(row["id"], reindented, reason="reindent",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("did not produce a readable tree", err,
                      "the REPLAY refuses a reindent; patch-id cannot")

    def test_an_UNANSWERABLE_ancestry_REFUSES_rather_than_proceeding(self):
        """The first cut returned `unverified` — proceed — when ancestry could
        not be answered, so the WEAKER signal was more permissive than a known
        non-descendant and "make ancestry unanswerable" was the way past the
        guard. Both now face the same evidence test."""
        row = self.add(recipient="grok", kind="build")
        # CONTROL first, same observable: this fixture DOES accept a move.
        control_out, control_err = dispatches.retip(
            row["id"], self.b, reason="trunk moved", repo=self.repo,
            notify=False)
        self.assertIsNone(control_err, control_err)
        self.assertIsNotNone(control_out)
        target = self.add(recipient="grok", kind="build", ref=self.b)
        from helm import landreq
        with mock.patch.object(landreq, "_ancestry",
                               return_value=landreq.UNDETERMINED):
            out, err = dispatches.retip(target["id"], self.side,
                                        reason="unanswerable",
                                        repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("UNANSWERABLE", err)
        self.assertNotIn("is NOT forward", err,
                         "an unanswerable ancestry must not be reported as "
                         "a proven backward move")

    def test_a_MERGED_TRUNK_lane_ACCEPTS_when_the_trees_are_IDENTICAL(self):  # noqa: VACUOUS_ASSERTION — a refusal arm whose point is that two IDENTICAL trees still refuse; the identical-tree assertEqual is the positive and the refusal text is asserted on err
        """THE ARM THAT CHANGED SIDES AT THE MELD, and it is the clearest
        statement of what a BUILD row's identity IS. A lane that MERGED TRUNK
        IN carries an extra first-parent patch the same work rebased and
        flattened does not, so the ordered per-commit spelling read 2 -> 1 and
        REFUSED — while the two trees are BYTE-IDENTICAL, which this fixture
        asserts before it asks anything else. What the recipient of a build row
        inherits is the FINAL DELTA over its fork point, not the lane's
        historical spelling, so identical trees deliver an identical base and
        this accepts (a review ruling; the previous refusal was mine and it
        was the task/1470 overclaim shape one branch further down)."""
        self.git("checkout", "-q", "-b", "merged-lane", self.a)
        with open(os.path.join(self.repo, "lane.txt"), "w",
                  encoding="utf-8") as f:
            f.write("the lane's own work\n")
        self.git("add", "lane.txt")
        self.git("commit", "-q", "-m", "lane work")
        lane_work = self.git("rev-parse", "HEAD")
        self.git("merge", "-q", "--no-ff", "-X", "ours", self.main,
                 "-m", "merge trunk in")
        merged = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "flattened", self.c)
        self.git("cherry-pick", lane_work)
        flattened = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.git("clean", "-qfd")
        self.assertEqual(self.git("rev-parse", merged + "^{tree}"),
                         self.git("rev-parse", flattened + "^{tree}"),
                         "the whole point: the trees must be IDENTICAL")
        row = self.add(recipient="grok", kind="build", ref=merged)
        out, err = dispatches.retip(row["id"], flattened, reason="flattened",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["tip"], flattened)

    def test_the_identity_cost_does_NOT_grow_with_lane_length(self):  # noqa: VACUOUS_ASSERTION — the per-size assertions are necessarily inside the loop over lane sizes; the unconditional control below it asserts the loop actually ran both sizes
        """THE COST MUST NOT GROW WITH LANE LENGTH, asserted as EQUALITY
        across two sizes rather than as a ceiling: a ceiling passes while the
        cost grows under it, and what the replay bought over the per-commit
        walk is that the count does not move at all. The walk it replaced was
        4N+10 — thirty git calls at five commits. The absolute number is
        deliberately NOT pinned here, because it moves with the authority
        observation and pinning it would make this arm fail for a reason that
        has nothing to do with lane length."""
        def lane(base, name, n):
            self.git("checkout", "-q", "-b", name, base)
            for i in range(n):
                path = "shared%d.txt" % i
                with open(os.path.join(self.repo, path), "w",
                          encoding="utf-8") as f:
                    f.write("shared %d\n" % i)
                self.git("add", path)
                self.git("commit", "-q", "-m", "c%d" % i)
            tip = self.git("rev-parse", "HEAD")
            self.git("checkout", "-q", self.main)
            self.git("clean", "-qfd")
            return tip

        from helm import landreq
        counts = {}
        for n in (1, 6):
            old_tip = lane(self.a, "cost-before-%d" % n, n)
            new_tip = lane(self.c, "cost-after-%d" % n, n)
            row = self.add(recipient="grok", kind="build", ref=old_tip)
            calls = []
            real = landreq._git
            def spy(gitdir, *args, **kwargs):
                calls.append(args[0] if args else "?")
                return real(gitdir, *args, **kwargs)
            with mock.patch.object(landreq, "_git", spy):
                ident, why, _how, _bound = dispatches._retip_identity(
                    row, new_tip)
            self.assertIsNone(why, why)
            self.assertEqual(ident, "verified",
                             "control: both sizes must be a PROVEN REBASE, or "
                             "an early refusal would flatten the count")
            counts[n] = len(calls)
        # UNCONDITIONAL, and it is the control the rung asked for: every
        # assertion above lives inside the loop, so without this a loop that
        # never ran would reach the comparison with an empty dict rather than
        # failing on the claim.
        self.assertEqual(sorted(counts), [1, 6], "both sizes must have run")
        self.assertEqual(counts[1], counts[6],
                         "the identity cost must not grow with lane length: "
                         "%r" % counts)

    def test_a_DUPLICATE_CONTEXT_edit_at_another_LOCATION_is_REFUSED(self):  # noqa: VACUOUS_ASSERTION — a refusal arm whose premise is that the two patch-ids MATCH while the trees DIFFER; both are asserted positively before the refusal
        """The repro, and the reason BUILD identity left patch-id
        entirely. A file carrying the same block WITH THE SAME SURROUNDING
        CONTEXT at two places gives `git patch-id` ONE id for an edit to either
        copy — under `--stable` and `--verbatim` alike — because patch-id
        ignores hunk locations by construction. The trees differ. A diff hash
        cannot bind LOCATION and no stronger hash fixes that, so the identity
        is a REPLAY whose emitted tree must equal the new tip's own.

        MY FIRST ATTEMPT AT THIS FIXTURE SAID THE FINDING DID NOT HOLD, which
        is why the context is spelled out twice below: with DIFFERENT
        neighbours around each block the diffs differ in their context lines
        and the ids come apart. The collision needs the whole region repeated.
        """
        region = ["ctxA", "ctxB", "ctxC", "TARGET", "ctxD", "ctxE", "ctxF"]
        path = os.path.join(self.repo, "dup.txt")
        self.git("checkout", "-q", "-b", "dup-base", self.a)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(region + ["---sep---"] + region) + "\n")
        self.git("add", "dup.txt")
        self.git("commit", "-q", "-m", "two identical regions")
        base = self.git("rev-parse", "HEAD")

        def edit(name, which):
            self.git("checkout", "-q", "-b", name, base)
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().split("\n")
            hits = [i for i, l in enumerate(lines) if l == "TARGET"]
            self.assertEqual(len(hits), 2, "fixture: two TARGETs")
            lines[hits[which]] = "EDITED"
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            self.git("add", "dup.txt")
            self.git("commit", "-q", "-m", "edit %d" % which)
            tip = self.git("rev-parse", "HEAD")
            self.git("checkout", "-q", self.main)
            self.git("clean", "-qfd")
            return tip

        first, second = edit("dup-first", 0), edit("dup-second", 1)
        from helm import landreq
        gitdir = os.path.join(self.repo, ".git")
        self.assertEqual(landreq._patch_id(gitdir, first),
                         landreq._patch_id(gitdir, second),
                         "THE PREMISE: the two patches hash the SAME")
        self.assertNotEqual(self.git("rev-parse", first + "^{tree}"),
                            self.git("rev-parse", second + "^{tree}"),
                            "and yet the two TREES differ")
        row = self.add(recipient="grok", kind="build", ref=first)
        out, err = dispatches.retip(row["id"], second, reason="same hash",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("produces tree", err)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["tip"], first,
                         "a refused retip leaves the row where it was")

    def test_a_HAND_RESOLVED_merge_with_a_DIFFERENT_result_is_REFUSED(self):  # noqa: VACUOUS_ASSERTION — a refusal arm; the unconditional positive is the assertNotEqual proving the hand resolution really made the trees differ
        """The other half of the merge pair. The merged-trunk arm accepts when
        the flattened spelling reproduces the same tree; this one carries a
        resolution that lives ONLY in the merge commit, so the replay onto the
        new base cannot reproduce it and the retip refuses. That is the case
        `git log --no-merges` would have silently accepted, which is why the
        identity is a replay rather than any walk over commits."""
        self.git("checkout", "-q", "-b", "hand-lane", self.a)
        with open(os.path.join(self.repo, "state"), "w",
                  encoding="utf-8") as f:
            f.write("lane's own line\n")
        self.git("add", "state")
        self.git("commit", "-q", "-m", "lane rewrites state")
        lane_work = self.git("rev-parse", "HEAD")
        # THE RESOLUTION IS A THIRD CONTENT, neither side's, so no replay of
        # the lane's commits onto the new base can reproduce it. -X ours and
        # -X theirs both FAILED to express this: they pick an existing side, so
        # the flattened spelling reproduced the same tree and the fixture's own
        # control caught it before the arm could pass vacuously.
        subprocess.run(["git", "-C", self.repo, "merge", "--no-ff",
                        "--no-commit", self.main], capture_output=True,
                       text=True)
        with open(os.path.join(self.repo, "state"), "w",
                  encoding="utf-8") as f:
            f.write("a resolution that is neither side\n")
        self.git("add", "state")
        self.git("commit", "-q", "-m", "merge trunk in, resolved by hand")
        merged = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "hand-flat", self.c)
        self.git("cherry-pick", "-X", "theirs", lane_work)
        flattened = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.git("clean", "-qfd")
        self.assertNotEqual(self.git("rev-parse", merged + "^{tree}"),
                            self.git("rev-parse", flattened + "^{tree}"),
                            "fixture: the resolution must make the trees DIFFER")
        row = self.add(recipient="grok", kind="build", ref=merged)
        out, err = dispatches.retip(row["id"], flattened, reason="flattened",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("did not produce a readable tree", err)

    def test_a_row_with_NO_authority_binding_REFUSES_a_non_FF_retip(self):  # noqa: VACUOUS_ASSERTION — a refusal arm; the unconditional positive is the bound control row accepted on the same fixture immediately before
        """THE LEGACY CASE, NAMED RATHER THAN DISCOVERED. Every row written
        before the binding carries none, and a non-FF BUILD retip on one
        refuses. That is a live behaviour change on rows already in the ledger
        and it is deliberate: compatibility is not authority. The measured
        reason is the tree-neutral case — content can be PROVEN while direction
        is UNKNOWN, and moving the row then re-points the recipient at an older
        base, which is the fail-open this predicate keeps curing."""
        old_tip = self._lane_tip(self.a, "unbound-before")
        rebased = self._lane_tip(self.c, "unbound-after")
        # CONTROL, unconditional and first: a BOUND row on this very fixture
        # accepts the same move, so the refusal below is about the binding.
        bound = self.add(recipient="grok", kind="build", ref=old_tip)
        out, err = dispatches.retip(bound["id"], rebased, reason="bound",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["tip"], rebased)
        self._undeclare_authority()
        legacy = self.add(recipient="grok", kind="build", ref=old_tip)
        self.assertNotIn("trunk_ref", legacy,
                         "fixture: the legacy row must carry NO binding")
        out2, err2 = dispatches.retip(legacy["id"], rebased, reason="legacy",
                                      repo=self.repo, notify=False)
        self.assertIsNone(out2)
        self.assertIn("NO TRUNK AUTHORITY BINDING", err2)
        self.assertIn("cancel", err2.lower(),
                      "and it must name the escape rather than only refusing")

    def test_a_CONFIGURED_EMPTY_authority_is_PRESERVED_not_read_as_LEGACY(self):  # noqa: VACUOUS_ASSERTION — both poles are refusals by contract; the arm's discriminator is that the two refusals differ, asserted in both directions on one fixture
        """A ROW CANNOT LIE ABOUT ITS OWN AGE TO EXCUSE A CONFIG IT READ.
        `if not ref` was true for BOTH the repository that declared nothing and
        the one that declared its trunk source blank, so the second was written
        as the legacy shape and the refusal told its author the row PREDATED
        authority binding — a claim about WHEN the row was written, drawn from
        a fact about the operator's config, and false. Worse than imprecise:
        that sentence sends them to re-dispatch, which mints an identical row
        against the same unreadable declaration.

        So the binding asks PRESENCE, not truthiness — the same distinction the
        parser had to learn, one layer out — and the projection carries the
        empty value through, which is measured here rather than assumed."""
        old_tip = self._lane_tip(self.a, "empty-decl-before")
        rebased = self._lane_tip(self.c, "empty-decl-after")
        self.git("config", "helm.trunkRef", "")
        empty = self.add(recipient="grok", kind="build", ref=old_tip)
        self.assertIn("trunk_ref", empty,
                      "fixture: the contradiction must REACH the row")
        self.assertEqual(empty["trunk_ref"], "")
        out, err = dispatches.retip(empty["id"], rebased, reason="empty decl",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("EMPTY", err)
        self.assertIn(dispatches._TRUNK_REF_KEY, err,
                      "the refusal must name the KEY, which is the thing to fix")
        # THE DISCRIMINATOR IS THE LEGACY BRANCH'S OWN CLAIM, not a phrase both
        # sentences happen to share. My first cut asserted "before the
        # binding" absent — and the declared-empty sentence SAYS that phrase,
        # in the clause explaining what it is NOT. The arm was right and the
        # needle was in both haystacks.
        self.assertNotIn("NO TRUNK AUTHORITY BINDING", err,
                         "a declared-empty row is not an UNBOUND one")
        # THE OTHER POLE, on this same fixture: the legacy row still earns the
        # age sentence. Either assertion alone passes if both branches collapse
        # back together, so both directions are asserted.
        self._undeclare_authority()
        legacy = self.add(recipient="grok", kind="build", ref=old_tip)
        out2, err2 = dispatches.retip(legacy["id"], rebased, reason="legacy",
                                      repo=self.repo, notify=False)
        self.assertIsNone(out2)
        self.assertIn("NO TRUNK AUTHORITY BINDING", err2)
        self.assertNotIn("EMPTY", err2)

    def test_a_DECLARED_authority_that_cannot_be_observed_REFUSES(self):  # noqa: VACUOUS_ASSERTION — a refusal arm whose positive control is the accepted move on the same pair before the declaration is broken
        """A CONTRADICTION, NOT A GAP, AND IT GETS ITS OWN SENTENCE. The
        repository said what it integrates against and we could not honour it.
        Both this and the never-declared case refuse — the typed distinction
        is for the reader, not for the outcome."""
        old_tip = self._lane_tip(self.a, "broken-before")
        rebased = self._lane_tip(self.c, "broken-after")
        row = self.add(recipient="grok", kind="build", ref=old_tip)
        self.assertEqual(row.get("trunk_ref"), "refs/heads/" + self.main,
                         "control: the row was dispatched WITH a binding")
        self.git("config", "helm.trunkRef", "refs/heads/nothing-here")
        out, err = dispatches.retip(row["id"], rebased, reason="broken",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("could not be honoured", err)
        self.assertIn("contradiction rather than a gap", err)

    def test_an_AUTHORITY_CHANGE_refuses_rather_than_re_binding(self):  # noqa: VACUOUS_ASSERTION — a refusal arm; the row's own recorded binding is asserted positively before the repository is re-declared
        """RE-BINDING SILENTLY WOULD LET A ROW'S BASE BE PROVEN AGAINST A
        TRUNK IT WAS NEVER DISPATCHED UNDER. An authority change is a
        migration and wants its own event, not a quiet re-point."""
        self.git("branch", "other-trunk", self.c)
        old_tip = self._lane_tip(self.a, "moved-before")
        rebased = self._lane_tip(self.c, "moved-after")
        row = self.add(recipient="grok", kind="build", ref=old_tip)
        self.assertEqual(row.get("trunk_ref"), "refs/heads/" + self.main)
        self.git("config", "helm.trunkRef", "refs/heads/other-trunk")
        out, err = dispatches.retip(row["id"], rebased, reason="moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("authority CHANGE is a migration", err)

    def test_a_repository_on_DEVELOP_works_because_it_DECLARES(self):  # noqa: VACUOUS_ASSERTION — the contract is that no error is raised in a non-main repository; the recorded binding is the unconditional positive
        """The reproducer that broke four discovery resolvers in a row is not
        a special case any more — it is the ordinary path. A repository whose
        integration branch is `develop` says so, and nothing has to guess."""
        self.git("branch", "-m", self.main, "develop")
        self.main = "develop"
        self.git("config", "helm.trunkRef", "refs/heads/develop")
        old_tip = self._lane_tip(self.a, "dev-before")
        rebased = self._lane_tip(self.c, "dev-after")
        row = self.add(recipient="grok", kind="build", ref=old_tip)
        self.assertEqual(row.get("trunk_ref"), "refs/heads/develop",
                         "the declaration is what the row records")
        out, err = dispatches.retip(row["id"], rebased, reason="trunk moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["tip"], rebased)

    def test_a_TREE_NEUTRAL_backward_move_is_refused_by_DIRECTION_alone(self):  # noqa: VACUOUS_ASSERTION — a refusal arm whose positive control is the forward move accepted on the same pair immediately before
        """THE ARM THE WHOLE DIRECTION CHECK EXISTS FOR, and the mutation
        matrix found it missing: dropping the forward-direction comparison
        killed NOTHING, because every other backward fixture fails the CONTENT
        replay first when trunk's delta is visible in the tree.

        Make trunk's movement TREE-NEUTRAL — an empty commit is the clean
        spelling, any commit the lane's tree does not see is the real one — and
        content matches in BOTH directions because there IS no content
        difference. Only the BASE is older. Nothing but the direction proof can
        refuse this, which is why it is the one case that pins it."""
        self.git("commit", "-q", "--allow-empty", "-m", "trunk moves, tree same")
        moved = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("rev-parse", self.c + "^{tree}"),
                         self.git("rev-parse", moved + "^{tree}"),
                         "fixture: trunk's movement must be TREE-NEUTRAL")
        older = self._lane_tip(self.c, "neutral-older")
        newer = self._lane_tip(moved, "neutral-newer")
        self.assertEqual(self.git("rev-parse", older + "^{tree}"),
                         self.git("rev-parse", newer + "^{tree}"),
                         "fixture: and the two lane spellings must MATCH")
        # CONTROL, unconditional and first: FORWARD is accepted, so the
        # refusal below is about DIRECTION and not about the fixture.
        fwd = self.add(recipient="grok", kind="build", ref=older)
        out, err = dispatches.retip(fwd["id"], newer, reason="forward",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["tip"], newer)
        back = self.add(recipient="grok", kind="build", ref=newer)
        out2, err2 = dispatches.retip(back["id"], older, reason="backward",
                                      repo=self.repo, notify=False)
        self.assertIsNone(out2)
        self.assertIn("not provably forward", err2)

    def test_the_binding_MOVES_with_the_row_on_every_hop(self):
        """Without this a SECOND retip proves its direction against the base of
        the ORIGINAL dispatch, so a lane rebased twice compares its third tip
        to its first base. The mutation matrix found no arm holding it."""
        first = self._lane_tip(self.a, "hop-one")
        second = self._lane_tip(self.c, "hop-two")
        row = self.add(recipient="grok", kind="build", ref=first)
        dispatched_base = row.get("base_sha")
        self.assertTrue(dispatched_base, "control: the row recorded a base")
        out, err = dispatches.retip(row["id"], second, reason="hop",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        moved_to = self.git("merge-base", second, "refs/heads/" + self.main)
        # BOTH PATHS, because they are two readers of one datum and only one of
        # them is exercised by a snapshot. The writer builds the returned row
        # from the event; replay rebuilds it from the ledger. The first cut of
        # this arm asserted only the REPLAYED row, so a mutation that removed
        # the WRITER's carry-forward killed nothing — the module's own law is
        # that the returned row must equal what a reader replays, and that law
        # is exactly what a single-path assertion cannot see.
        self.assertEqual(out.get("base_sha"), moved_to,
                         "the WRITER's returned row must carry the new base")
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(live["tip"], second, "control: the row moved")
        self.assertNotEqual(live.get("base_sha"), dispatched_base,
                            "the base must MOVE with the row, not stay at the "
                            "one the original dispatch recorded")
        self.assertEqual(live.get("base_sha"), moved_to,
                         "and REPLAY must agree with the writer, or the row a "
                         "reader sees diverges from the row its author got")

    def test_a_DECLARATION_that_cannot_be_observed_is_PERSISTED_not_erased(self):
        """A repository that DECLARED an authority must never be recorded in
        the same shape as one that never did — those two states owe the reader
        different sentences, and the refusal text is built from the
        difference."""
        gitdir = os.path.join(self.repo, ".git")
        bound = dispatches._authority_binding(gitdir, self.c)
        self.assertEqual(bound.get("trunk_ref"), "refs/heads/" + self.main,
                         "control: an observable declaration binds fully")
        self.assertTrue(bound.get("trunk_sha") and bound.get("base_sha"))
        self.git("config", "helm.trunkRef", "refs/heads/not-a-branch")
        broken = dispatches._authority_binding(gitdir, self.c)
        self.assertEqual(broken.get("trunk_ref"), "refs/heads/not-a-branch",
                         "the DECLARATION survives even though it failed")
        self.assertIsNone(broken.get("trunk_sha"),
                          "with no observation behind it")
        self._undeclare_authority()
        self.assertEqual(dispatches._authority_binding(gitdir, self.c), {},
                         "and never-declared is the EMPTY shape, which is how "
                         "the two stay distinguishable")

    def test_NEITHER_a_review_dispatch_NOR_an_FF_retip_touches_the_authority(self):  # noqa: VACUOUS_ASSERTION — the contract IS an empty call list; the unconditional positive is the BUILD dispatch asserted to touch the authority on the same fixture, which a second call cannot be linked to by the rung
        """TWO PATHS THAT MUST COST NOTHING, and both were paying.

        A REVIEW dispatch never consumes a stored binding — its identity
        observes the current authority for itself — so binding one would fetch
        and ls-remote for every review send, with the credential prompts,
        timeouts and offline failures a network call implies, to write fields
        nothing reads. A BUILD FF retip is answered by ancestry alone, and
        observing the authority ahead of it was measured at four git calls
        including two config reads before this arm existed.

        The control asserts against the AUTHORITY-TOUCHING verbs specifically
        rather than a total count, because a total moves with every unrelated
        change and would make this arm fail for reasons that are not its
        subject."""
        from helm import landreq
        touching = ("config", "fetch", "ls-remote")

        def calls_during(fn):
            seen = []
            real = landreq._git

            def spy(gitdir, *args, **kwargs):
                seen.append(args[0] if args else "?")
                return real(gitdir, *args, **kwargs)

            with mock.patch.object(landreq, "_git", spy):
                fn()
            return seen

        # CONTROL, unconditional and first: a BUILD dispatch DOES observe, so
        # an empty list below is the path being cheap rather than the spy
        # being blind.
        built = calls_during(
            lambda: self.add(recipient="grok", kind="build", ref=self.a))
        self.assertTrue([c for c in built if c in touching],
                        "control: a BUILD dispatch must observe the authority")
        reviewed = calls_during(
            lambda: self.add(recipient="grok", kind="review", ref=self.a))
        self.assertEqual([c for c in reviewed if c in touching], [],
                         "a REVIEW dispatch must not touch the authority")
        row = self.add(recipient="grok", kind="build", ref=self.a)
        ff = calls_during(
            lambda: dispatches.retip(row["id"], self.b, reason="ff",
                                     repo=self.repo, notify=False))
        self.assertEqual([c for c in ff if c in touching], [],
                         "an FF retip must not touch the authority")

    def test_a_GLOBAL_declaration_does_NOT_bind_this_repository(self):  # noqa: VACUOUS_ASSERTION — the contract IS that the seam sees nothing; the unconditional positive is git's own bare config --get returning the global line, which is necessarily a different call
        """An authority is a property of the REPOSITORY, not of whoever runs
        the command. A bare `git config --get` reads the whole stack, so one
        line in a user's ~/.gitconfig would silently declare a trunk for every
        repository they touch — and a wrong trunk is exactly the confidently
        wrong answer this design replaced four resolvers to avoid."""
        home = os.path.join(self.tmp, "fake-home")
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, ".gitconfig"), "w",
                  encoding="utf-8") as f:
            f.write("[helm]\n\ttrunkRef = refs/heads/from-the-global-config\n")
        gitdir = os.path.join(self.repo, ".git")
        prior = os.environ.get("HOME")
        prior_global = os.environ.get("GIT_CONFIG_GLOBAL")
        try:
            os.environ["HOME"] = home
            os.environ.pop("GIT_CONFIG_GLOBAL", None)
            # THE LOCAL DECLARATION GOES FIRST, or it wins the stack and the
            # control below reads it instead of the global line — which is
            # what happened on the first run of this arm.
            self._undeclare_authority()
            # CONTROL: git itself DOES see the global line, so a None below is
            # this seam scoping correctly rather than the fixture failing to
            # write one.
            seen = subprocess.run(
                ["git", "--git-dir", gitdir, "config", "--get",
                 "helm.trunkref"], capture_output=True, text=True,
                env=dict(os.environ)).stdout.strip()
            self.assertEqual(seen, "refs/heads/from-the-global-config",
                             "control: the global declaration must be visible "
                             "to a bare config --get")
            ref = dispatches._declared_authority(gitdir)[0]
            self.assertIsNone(ref,
                              "a GLOBAL declaration must not bind a "
                              "repository that declares nothing itself")
        finally:
            if prior is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = prior
            if prior_global is not None:
                os.environ["GIT_CONFIG_GLOBAL"] = prior_global

    def test_the_freshness_receipt_MOVES_with_the_row_not_only_the_sha(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is assertIsNone(err) on the accepting call; the unconditional positives are the receipt and instant asserted to equal THIS hop's forced values on both the writer and the replayed row
        """A receipt persisted at dispatch and left behind by every retip is
        WORSE than none: it is a currency claim about a reading that did not
        happen on this hop. The base moved and the receipt did not, so the row
        said `observed at T` while its base came from a later observation."""
        first = self._lane_tip(self.a, "receipt-one")
        second = self._lane_tip(self.c, "receipt-two")
        row = self.add(recipient="grok", kind="build", ref=first)
        self.assertEqual(row.get("trunk_receipt"), "local",
                         "control: the dispatch recorded HOW it observed")
        dispatched_at = row.get("trunk_observed_at")
        self.assertTrue(dispatched_at, "control: and WHEN")
        # THE HOP'S OBSERVATION IS MADE DISTINGUISHABLE ON PURPOSE. Two
        # weaker arms were written first and BOTH were vacuous: asserting the
        # field is PRESENT passes on inheritance, since writer and replay both
        # build their row by copying the previous one; and asserting the
        # INSTANT CHANGED passes only if the clock ticks, which at one-second
        # resolution it does not inside a test that runs in milliseconds. So
        # the observer is made to answer differently for this hop, and the row
        # must show THAT answer.
        from helm import vcs
        real_observe = vcs.observe_trunk_authority

        def hop_observe(root, source_ref, remote=None, timeout=20):
            sha, _receipt, failure = real_observe(root, source_ref, remote,
                                                  timeout)
            return sha, vcs.TRUNK_FETCHED, failure

        # AND THE INSTANT IS FORCED FORWARD, for the same reason the receipt
        # is forced to differ: at one-second resolution a dispatch and a retip
        # inside one test land on the SAME stamp, so "the instant changed" is
        # unobservable however true it is. A later clock makes this hop's
        # reading distinguishable from the one it inherited.
        later = "2027-01-01T00:00:00Z"
        with mock.patch.object(vcs, "observe_trunk_authority", hop_observe), \
                mock.patch.object(dispatches.pk, "now_ts",
                                  lambda *a, **k: later):
            out, err = dispatches.retip(row["id"], second, reason="hop",
                                        repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        # ASSERTING THE FIELD IS *PRESENT* CANNOT FAIL, and the mutation
        # matrix proved it: both the writer and replay build their row by
        # COPYING the previous one, so a receipt persisted at dispatch is
        # inherited whether or not this hop carries one forward. Removing the
        # carry-forward entirely left the arm green.
        #
        # THE INSTANT IS THE DISCRIMINATOR, because it necessarily CHANGES on
        # every observation. If the hop carried nothing forward, the row would
        # still be wearing the instant of the dispatch that created it — a
        # currency claim about a reading that did not happen on this hop.
        for where, seen in (("writer", out),
                            ("replay", dispatches.snapshot()[0][row["id"]])):
            self.assertEqual(seen.get("trunk_receipt"), vcs.TRUNK_FETCHED,
                             "%s: the row must carry THIS hop's receipt, not "
                             "the one inherited from the dispatch" % where)
            self.assertEqual(seen.get("trunk_observed_at"), later,
                             "%s: and THIS hop's instant, not %s"
                             % (where, dispatched_at))

    def test_a_DOUBLE_declaration_is_AMBIGUOUS_not_the_last_one(self):  # noqa: VACUOUS_ASSERTION — a refusal arm; the unconditional positive is the single-valued declaration resolving on the same fixture immediately before
        """`git config --get` returns the LAST value and says nothing about
        the first, so a file carrying the key twice reads as whichever git
        printed. A declaration that says two different things is AMBIGUOUS —
        picking one is exactly the arbitrary choice this design exists to stop
        making."""
        gitdir = os.path.join(self.repo, ".git")
        self.assertEqual(dispatches._declared_authority(gitdir)[0],
                         "refs/heads/" + self.main,
                         "control: one declaration resolves normally")
        self.git("config", "--add", "helm.trunkRef", "refs/heads/other-one")
        ref, _remote, sha, failure = dispatches._declared_authority(gitdir)[:4]
        self.assertIsNone(sha, "an ambiguous declaration cannot be observed")
        self.assertIn("more than once", failure)

    def test_an_EMPTY_and_a_REMOTE_declaration_CONFLICT_not_choose_the_remote(self):  # noqa: VACUOUS_ASSERTION — a refusal arm; its unconditional positive is the DOT-equivalence arm below, which resolves a two-entry declaration on this same parser
        """AN EMPTY VALUE IS A VALUE. Filtering blank lines out of `--get-all`
        before counting reopened the ambiguity hole one layer down: git prints
        `helm.trunkRemote` declared as `` and `origin` as `'\norigin\n'`, whose
        blank FIRST line is the empty declaration, and dropping it left one
        value and no conflict to find. Empty means THIS repository, so those
        two entries name a local and a remote authority at once."""
        gitdir = os.path.join(self.repo, ".git")
        self.git("config", "helm.trunkRemote", "")
        self.git("config", "--add", "helm.trunkRemote", "origin")
        ref, _remote, sha, failure = dispatches._declared_authority(gitdir)[:4]
        self.assertIsNone(sha, "a conflicted authority cannot be observed")
        self.assertIn("more than once", failure)

    def test_an_EMPTY_and_a_DOT_remote_are_ONE_local_authority(self):
        """The control that pins WHERE the canonicalization happens. Empty,
        `.` and absent are three spellings of this repository, so folding them
        BEFORE the uniqueness test is what makes the conflict above a conflict
        and this one agreement. Counting raw values first — the obvious cure to
        the dropped-empty bug — refuses this repository for changing nothing."""
        gitdir = os.path.join(self.repo, ".git")
        self.git("config", "helm.trunkRemote", "")
        self.git("config", "--add", "helm.trunkRemote", ".")
        ref, remote, sha, failure = dispatches._declared_authority(gitdir)[:4]
        self.assertIsNone(failure, failure)
        self.assertIsNone(remote, "both spellings store the LOCAL authority")
        self.assertEqual(ref, "refs/heads/" + self.main)
        self.assertTrue(dispatches._TIP.fullmatch(str(sha)), sha)

    def test_an_EMPTY_and_a_VALID_ref_CONFLICT_not_repair_to_the_valid_one(self):  # noqa: VACUOUS_ASSERTION — a refusal arm; the unconditional positive is setUp's single valid declaration, which the DOT-equivalence arm observes on this same parser
        """The same hole on the ref key, and the more dangerous half: dropping
        the empty entry SILENTLY REPAIRS a malformed declaration into a working
        one, so a repository whose config says two contradictory things about
        its trunk gets an answer instead of a refusal."""
        gitdir = os.path.join(self.repo, ".git")
        self.git("config", "--add", "helm.trunkRef", "")
        ref, _remote, sha, failure = dispatches._declared_authority(gitdir)[:4]
        self.assertIsNone(sha, "a conflicted authority cannot be observed")
        self.assertIn("more than once", failure)

    def test_a_PRESENT_but_EMPTY_ref_is_a_CONTRADICTION_not_an_ABSENT_one(self):  # noqa: VACUOUS_ASSERTION — a refusal arm; the unconditional positive is the ABSENT half asserted on the same fixture immediately after
        """PRESENCE IS THE RETURN CODE, never the emptiness of what was
        printed. A key git never had exits 1; a key present and empty exits 0
        and prints a blank line, and reading emptiness as absence turns a
        declaration nobody can read into the legacy no-declaration row — the
        one shape this design owes a different sentence."""
        gitdir = os.path.join(self.repo, ".git")
        self.git("config", "helm.trunkRef", "")
        ref, _remote, sha, failure = dispatches._declared_authority(gitdir)[:4]
        self.assertIsNone(sha, "an empty ref names no branch")
        self.assertIn("empty value", failure)
        self._undeclare_authority()
        self.assertEqual(dispatches._declared_authority(gitdir)[:4],
                         (None, None, None, None),
                         "an ABSENT key is the gap shape, and stays it")

    def test_DOT_and_ABSENT_are_the_SAME_local_authority(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is assertIsNone(err) on a retip that must SUCCEED; the unconditional positive is the tip equality on the same call's other channel
        """The contract says `helm.trunkRemote` absent OR `.` both mean THIS
        repository. Persisting one spelling while comparing the raw string made
        toggling between them read as an authority MIGRATION and refuse — a
        false refusal on a repository that changed nothing."""
        gitdir = os.path.join(self.repo, ".git")
        absent = dispatches._authority_binding(gitdir, self.c)
        self.git("config", "helm.trunkRemote", ".")
        dotted = dispatches._authority_binding(gitdir, self.c)
        self.assertEqual(absent.get("trunk_remote"), dotted.get("trunk_remote"),
                         "the two spellings must store ONE value")
        first = self._lane_tip(self.a, "dot-before")
        second = self._lane_tip(self.c, "dot-after")
        row = self.add(recipient="grok", kind="build", ref=first)
        self.git("config", "--unset", "helm.trunkRemote")
        out, err = dispatches.retip(row["id"], second, reason="dot to absent",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["tip"], second,
                         "toggling . to unset is not a migration")

    def test_an_OPEN_successor_blocks_retip_and_a_CLOSED_one_does_not(self):
        """THE COMPOSITION WITH THE DUPLICATE-SUCCESSOR LAW. A row whose OPEN
        successor already supersedes it has demonstrably handed its obligation
        to the child; re-pointing the parent would stand up a second live
        frontier for the same work — the precise duplicate the mint guard
        refuses. A CLOSED successor releases the block: the continuation
        ended and the still-open parent is again the one frontier."""
        row = self.add(recipient="grok", kind="build")
        child = self.add(recipient="grok", supersedes=row["id"])
        out, err = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("supersede", err)
        self.assertIn(child["id"][:12], err)
        _c, cerr = dispatches.mark_cancel(child["id"], "round abandoned")
        self.assertIsNone(cerr, cerr)
        out2, err2 = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                      repo=self.repo, notify=False)
        self.assertIsNone(err2, err2)
        self.assertEqual(out2["tip"], self.b)

    def test_the_chain_still_works_ON_TOP_of_a_retipped_row(self):
        """The other direction of composing: a retipped row is still a
        legitimate PARENT. A first successor minted --supersedes it links
        cleanly (inheriting the root), and a second live successor is refused
        by the duplicate-successor guard exactly as on an untouched row."""
        row = self.add(recipient="grok", kind="build")
        _o, err = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                   repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        child = self.add(recipient="grok", supersedes=row["id"], ref=self.b)
        live = dispatches.snapshot()[0]
        self.assertEqual(live[child["id"]].get("chain_root"), row["id"])
        self.assertEqual(live[row["id"]].get("superseded_by"), child["id"])
        dup, why = dispatches.add("grok", "lane-dup", ref=self.b,
                                  repo=self.repo, supersedes=row["id"],
                                  notify=False, _reason=True)
        self.assertIsNone(dup)
        self.assertIn("duplicate live work", why)

    def test_the_cli_arm_refuses_junk_before_writing_anything(self):
        row = self.add(recipient="grok")
        rc, _out, err = run(dispatches.cmd_dispatch, ["retip", row["id"][:12]])
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual((live["status"], live["tip"]), ("open", self.a),
                         "a usage refusal must write NOTHING")

    def test_the_cli_arm_moves_a_row_and_reports_the_hop(self):
        row = self.add(recipient="grok", kind="build")
        rc, out, _err = run(dispatches.cmd_dispatch,
                            ["retip", row["id"][:12], "--ref", self.b,
                             "--reason", "trunk moved", "--json"])
        self.assertEqual(rc, 0, _err)
        got = json.loads(out)
        self.assertEqual((got["id"], got["tip"]), (row["id"], self.b))
        self.assertEqual((got["retips"][0]["old_tip"], got["identity"]),
                         (self.a, "verified"))
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual((live["tip"], live["status"]), (self.b, "open"))

    def test_replay_enforces_the_successor_frontier_law_the_writer_does(self):
        """codex P1 on 10b316ca, reproduced verbatim: the writer refuses to
        retip a parent whose OPEN child supersedes it, but a hand-appended
        well-shaped retip at parent.seq+1 APPLIED at replay, because replay
        checked only status/seq/old_tip — replay accepted a transition the
        writer refuses. Now replay reads the successor frontier off the same
        one coherent projection the fold builds: the forged event is inert
        while the child lives, and the SAME-SHAPED event applies once the
        child is closed (the control that proves the refusal is the frontier,
        not the event shape). Identity is `unverified`, deliberately: a
        hand-appended hop that cannot claim the writer verified anything is
        the honest forgery shape, and it keeps this arm pinned to the
        frontier law alone."""
        row = self.add(recipient="grok", kind="build")
        child = self.add(recipient="grok", supersedes=row["id"])
        seq = dispatches.snapshot()[0][row["id"]]["seq"]
        forged = {"v": 3, "event": "retip", "seq": seq + 1, "id": row["id"],
                  "ts": dispatches.pk.now_ts(), "tip": self.b, "ref": self.b,
                  "old_tip": self.a, "reason": "adversarial",
                  "identity": "unverified"}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), forged))
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual((live["tip"], live.get("retips")), (self.a, None),
                         "an open successor is the one live frontier: the "
                         "parent's tip must not move at replay either")
        _c, cerr = dispatches.mark_cancel(child["id"], "round abandoned")
        self.assertIsNone(cerr, cerr)
        self.assertTrue(eventledger.append(dispatches.ledger_path(),
                                           dict(forged)))
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual([h["old_tip"] for h in live["retips"]], [self.a],
                         "the control: frontier clear, same event applies")
        self.assertEqual(live["tip"], self.b)

    def test_replay_requires_a_strict_identity_and_keeps_it_durable(self):
        """codex P1 on 10b316ca: replay neither validated event.identity nor
        copied it anywhere durable — a strict retip with identity OMITTED
        applied, and a verified stamp existed only in the writer's transient
        return value. Identity is now a strict accepted enum (omitted and
        junk are both inert) AND a durable hop field a fresh snapshot still
        carries. The verified half of durability is pinned by the happy-path
        arm; this one pins unverified."""
        row = self.add(recipient="grok", kind="build")
        seq = dispatches.snapshot()[0][row["id"]]["seq"]
        base = {"v": 3, "event": "retip", "seq": seq + 1, "id": row["id"],
                "ts": dispatches.pk.now_ts(), "tip": self.b, "ref": self.b,
                "old_tip": self.a, "reason": "adversarial"}
        self.assertTrue(eventledger.append(dispatches.ledger_path(),
                                           dict(base)))          # omitted
        self.assertTrue(eventledger.append(dispatches.ledger_path(),
                                           dict(base, identity="definitely")))
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual((live["tip"], live.get("retips")), (self.a, None),
                         "an event that cannot say whether identity was "
                         "verified has not earned application")
        self.assertTrue(eventledger.append(dispatches.ledger_path(),
                                           dict(base, identity="unverified")))
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(live["tip"], self.b)
        self.assertEqual([h["identity"] for h in live["retips"]],
                         ["unverified"],
                         "the stamp must survive a fresh snapshot ON the hop")

    def test_two_tips_already_on_trunk_can_never_verify_vacuously(self):
        """codex P1 on 10b316ca, reproduced verbatim: linear a->b->c with c on
        trunk — both post-merge-base sequences are [], and []==[] blessed
        a->b as verified although b carries a whole commit a does not. When
        both tips sit on trunk the reviewed patch itself decides, and the two
        differ, so this now REFUSES.

        AND THE REFUSAL SAYS WHICH TIP CARRIES THE EXTRA COMMIT, which is the
        sentence this docstring has always described and the code did not
        print: `b` descends from `a` by exactly one commit, so that is what it
        names now instead of calling the reviewed sequence empty."""
        row = self.add(recipient="grok", kind="review")
        out, err = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("DESCENDS from the reviewed tip " + self.a[:12], err)
        self.assertIn("adding 1 commit", err)
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual((live["status"], live["tip"]), ("open", self.a))

    def test_an_identical_retry_after_a_committed_write_reconciles(self):
        """codex P2 on 10b316ca: a committed retip whose RESPONSE was lost
        gets re-run, and the exact re-run used to refuse with 'already names'
        — the one sibling mutation that punished the retry pattern every
        other writer reconciles. The identical retry now returns the achieved
        row without another append (and the receipt proves it: one event,
        same seq); a DIFFERENT reason at the same tip is a genuine no-op and
        still refuses."""
        row = self.add(recipient="grok", kind="build")
        first, err = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                      repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        again, err2 = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                       repo=self.repo, notify=False)
        self.assertIsNone(err2, err2)
        self.assertEqual((again["id"], again["tip"], again["seq"]),
                         (row["id"], self.b, first["seq"]))
        self.assertEqual(again["identity"], "verified",
                         "the reconciled retry carries the hop's durable stamp")
        self.assertEqual(len(self.raw_events(row["id"], "retip")), 1,
                         "reconciling is a READ: no second event")
        other, err3 = dispatches.retip(row["id"], self.b, reason="different",
                                       repo=self.repo, notify=False)
        self.assertIsNone(other)
        self.assertIn("already names", err3)

    def test_an_UNREADABLE_successor_state_refuses_at_the_writer(self):
        """codex P1 on 75ca0f32, reproduced verbatim: the frontier was an
        equality against the parent id, and a not-closed child whose
        supersedes replays CHAIN_UNKNOWN satisfies no equality — so the one
        state that means 'this check could not look' read as 'no open
        successor' and the writer retipped a parent whose successor state was
        unreadable. UNKNOWN now REFUSES with a named reason and writes
        nothing; closing the unreadable row clears the frontier (the control
        that proves the refusal was the unreadable state, nothing else)."""
        row = self.add(recipient="grok", kind="build")
        child = self.add(recipient="grok", supersedes=row["id"])
        self.corrupt_supersedes(child["id"])
        self.assertEqual(
            dispatches.snapshot()[0][child["id"]].get("supersedes"),
            dispatches.CHAIN_UNKNOWN,
            "fixture MUST-HIT: the child must actually replay chain-UNKNOWN")
        before = self.raw_events(row["id"])
        self.assertEqual([e["event"] for e in before],
                         ["dispatch", "superseded"],
                         "fixture MUST-HIT: the parent enters carrying its "
                         "seq-0 row plus the mint-time annotation")
        out, err = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("UNREADABLE", err)
        self.assertIn(child["id"][:12], err,
                      "the refusal must NAME the unreadable row")
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual((live["status"], live["tip"]), ("open", self.a))
        self.assertEqual(self.raw_events(row["id"]), before,
                         "a refusal writes NOTHING")
        _c, cerr = dispatches.mark_cancel(child["id"], "round abandoned")
        self.assertIsNone(cerr, cerr)
        out2, err2 = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                      repo=self.repo, notify=False)
        self.assertIsNone(err2, err2)
        self.assertEqual(out2["tip"], self.b,
                         "the control: a CLOSED unreadable row no longer "
                         "occupies the frontier")

    def test_an_UNREADABLE_successor_state_is_inert_at_replay_too(self):
        """The replay half of the same P1: a hand-appended well-shaped retip
        past a child whose supersedes replays CHAIN_UNKNOWN must be inert —
        replay enforces what the writer refuses, and a frontier that could
        not be read never reads as clear. Identity is `unverified` to keep
        the frontier the only law in play (see the open-successor arm)."""
        row = self.add(recipient="grok", kind="build")
        child = self.add(recipient="grok", supersedes=row["id"])
        self.corrupt_supersedes(child["id"])
        self.assertEqual(
            dispatches.snapshot()[0][child["id"]].get("supersedes"),
            dispatches.CHAIN_UNKNOWN,
            "fixture MUST-HIT: the child must actually replay chain-UNKNOWN")
        seq = dispatches.snapshot()[0][row["id"]]["seq"]
        forged = {"v": 3, "event": "retip", "seq": seq + 1, "id": row["id"],
                  "ts": dispatches.pk.now_ts(), "tip": self.b, "ref": self.b,
                  "old_tip": self.a, "reason": "adversarial",
                  "identity": "unverified"}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), forged))
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual((live["tip"], live.get("retips")), (self.a, None),
                         "an unreadable frontier must refuse at replay: the "
                         "forged hop applied past a child whose successor "
                         "state could not be read")
        _c, cerr = dispatches.mark_cancel(child["id"], "round abandoned")
        self.assertIsNone(cerr, cerr)
        self.assertTrue(eventledger.append(dispatches.ledger_path(),
                                           dict(forged)))
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(live["tip"], self.b,
                         "the control: frontier readable again, same event "
                         "applies")

    def test_a_MISANCHORED_forge_dies_on_the_derived_tip_binding(self):  # noqa: VACUOUS_ASSERTION — the forge-inert absences are controlled: the writer's retip then moves the SAME row (tip positive) and the SAME event list yields the appended hop (retips/ev positives)
        """SCOPE CORRECTED after review measured this arm honestly.

        What this test proves is NARROWER than its old name claimed. It
        forges an old_tip the row was never at, so replay refuses it at the
        DERIVED-TIP BINDING — a guard that predates this lane. It says
        nothing about a forge that anchors correctly; see the arm below,
        which measures that one and finds it APPLIES.

        The self-hashed `proof` is still the right thing to have removed:
        an unkeyed recipe is recomputable by exactly the forger it would
        need to stop, so a better hash was never the cure. The control: the
        writer's real retip still moves the row afterwards, replays clean
        end-to-end, and the event it appends carries NO proof field."""
        row = self.add(recipient="grok", kind="build")
        seq = dispatches.snapshot()[0][row["id"]]["seq"]
        forged = {"v": 3, "event": "retip", "seq": seq + 1, "id": row["id"],
                  "ts": dispatches.pk.now_ts(), "tip": self.side,
                  "ref": self.side, "old_tip": self.c,
                  "reason": "unrelated work", "identity": "verified"}
        forged["proof"] = self.historical_proof(forged)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), forged))
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual((live["tip"], live.get("retips")), (self.a, None),
                         "the ledger is the witness: a forge that hashes its "
                         "own fields still cannot make the fold's derived "
                         "state say the row was at its old_tip")
        out, err = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        ev = self.raw_events(row["id"], "retip")[-1]
        self.assertEqual((ev["old_tip"], ev["tip"], ev["identity"]),
                         (self.a, self.b, "verified"),
                         "MUST-HIT: this is the writer's own appended event, "
                         "not the forge — the absence check below is about "
                         "ITS schema")
        self.assertNotIn("proof", ev,
                         "the writer stamps nothing the fold is pinned to "
                         "ignore")
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(
            (live["tip"], [h["identity"] for h in live["retips"]]),
            (self.b, ["verified"]),
            "the control: the legitimate retip replays clean end-to-end")

    def test_a_CORRECTLY_ANCHORED_forge_APPLIES_and_that_is_the_trust_boundary(self):
        """KNOWN ACCEPTED RISK, recorded so nobody re-derives it and nobody
        believes a defense we do not have. Review said the correctly
        anchored forge still applies; a probe confirmed it.

        An attacker who can APPEND TO THE LEDGER, and who anchors the hop at
        the row's true derived tip, moves the row to any tip it likes and has
        `identity: verified` recorded durably beside it. This test asserts
        that it APPLIES — it does not pretend otherwise.

        WHY NO REPLAY-SIDE ARM CAN CLOSE IT, which is the whole point: replay's
        only inputs ARE ledger events. A writer with append access and a forger
        with append access submit byte-identical evidence, so every check
        available to the fold sees one indistinguishable population. The
        removed `proof` field is the proof of that — an unkeyed recipe is
        recomputable by exactly the forger it would need to stop, and any
        replacement computed from event fields inherits the same defect. A
        stronger check here would be theatre: it would raise the cost of the
        forge by zero and raise our confidence by a lot, which is the worst
        possible trade.

        WHERE THE REAL CURE LIVES: upstream, at the WRITE boundary — dregg
        writer-identity signing, so an append carries a key the forger does
        not hold and the ledger can tell the two populations apart. Filed as
        task #241. Until that lands, ledger-append access is root here and
        this test is the honest record of it.

        THE CONTROL that keeps this from excusing everything: the MIS-anchored
        forge above still dies at the derived-tip binding. The boundary is
        exactly `can append AND anchors correctly`, not `can append`."""
        row = self.add(recipient="grok", kind="build")
        snap = dispatches.snapshot()[0][row["id"]]
        seq, derived_tip = snap["seq"], snap["tip"]
        self.assertEqual(derived_tip, self.a,
                         "fixture MUST-HIT: the row starts at its own tip, so "
                         "anchoring below is genuinely CORRECT, not accidental")
        forged = {"v": 3, "event": "retip", "seq": seq + 1, "id": row["id"],
                  "ts": dispatches.pk.now_ts(), "tip": self.side,
                  "ref": self.side, "old_tip": derived_tip,
                  "reason": "unrelated work", "identity": "verified"}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), forged))
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(live["tip"], self.side,
                         "MEASURED, NOT ASPIRATIONAL: the anchored forge moves "
                         "the row. If this ever starts failing, a real cure "
                         "landed — read #241 and rewrite this test rather than "
                         "deleting it")
        self.assertEqual([h["identity"] for h in live["retips"]], ["verified"],
                         "and the forger's own self-asserted stamp is what the "
                         "durable record now carries — the field is testimony, "
                         "never authority")

    def test_the_proof_field_is_inert_on_the_acceptance_path(self):
        """THE MUTATION PIN: nothing in the retip replay arm may
        read `proof`, in either direction. Three same-shaped events, bound
        to the row's true derived tip, differing ONLY in the proof field —
        absent, junk, and the historical recipe computed correctly — must
        produce IDENTICAL outcomes. Mutate replay to consult the field and
        one diverges: requiring a valid proof kills the absent/junk arms
        (the law this pin retires — its acceptance is the
        behavioral delta), honoring it as authority is killed by the
        misbound-forge arm above. The residual this pins as LAW: a bound
        verified stamp is the writer's TESTIMONY, recorded durably, not
        re-proven at fold — the forger who could exploit that holds append
        access and is outside every ledger-resident scheme."""
        exercised = []
        for variant in ("absent", "junk", "self-consistent"):
            row = self.add(recipient="grok", kind="build")
            seq = dispatches.snapshot()[0][row["id"]]["seq"]
            ev = {"v": 3, "event": "retip", "seq": seq + 1, "id": row["id"],
                  "ts": dispatches.pk.now_ts(), "tip": self.b, "ref": self.b,
                  "old_tip": self.a, "reason": "hand-appended",
                  "identity": "verified"}
            if variant == "junk":
                ev["proof"] = "deadbeef" * 4
            if variant == "self-consistent":
                ev["proof"] = self.historical_proof(ev)
            self.assertTrue(eventledger.append(dispatches.ledger_path(), ev))
            live = dispatches.snapshot()[0][row["id"]]
            self.assertEqual(
                (live["tip"], [h["identity"] for h in live["retips"]]),
                (self.b, ["verified"]),
                "outcome must not depend on the proof field (%s): replay "
                "reads its witness, never the hash" % variant)
            exercised.append(variant)
        self.assertEqual(exercised, ["absent", "junk", "self-consistent"],
                         "the unconditional control: all three variants ran "
                         "and applied — a skipped arm is a vacuous pin")

    def test_a_row_with_no_derivable_tip_anchors_no_retip(self):
        """The underivable half: a legacy needs-redispatch row has
        NO derivable current tip, and the old binding check string-matched
        absence against absence — '' == '' — so a hand-appended hop APPLIED
        to a row whose state replay could not witness. Underivable now
        anchors nothing: the writer refuses with the named reason (measured:
        it did NOT already refuse — None == None passed its under-lock tip
        re-check), and the same-shaped hop is inert at fold."""
        rid = "ab" * 12
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "id": rid, "recipient": "grok", "lane": "lane-legacy",
            "deadline_s": 3600, "status": "open"}))
        live = dispatches.snapshot()[0][rid]
        self.assertEqual((live["status"], live.get("tip")), ("open", None),
                         "fixture MUST-HIT: an OPEN legacy row with no "
                         "derivable tip")
        out, err = dispatches.retip(rid, self.b, reason="anchor it",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("derivable", err,
                      "the refusal must NAME the underivable anchor")
        self.assertIn("re-dispatch", err)
        forged = {"v": 3, "event": "retip", "seq": 1, "id": rid,
                  "ts": dispatches.pk.now_ts(), "tip": self.b, "ref": self.b,
                  "reason": "anchor it", "identity": "unverified"}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), forged))
        live = dispatches.snapshot()[0][rid]
        self.assertIsNone(live.get("tip"),
                          "underivable never reads as anchored: the hop must "
                          "not apply to a row whose tip the fold cannot "
                          "witness")
        self.assertNotIn("retips", live)

    def test_an_exact_retry_reconciles_even_after_the_row_went_terminal(self):
        """codex P2 on 75ca0f32, reproduced verbatim: reconciliation ran
        AFTER the OPEN-only gate, so a delayed retry of a COMMITTED retip
        failed the moment the row went terminal — the gate refused the exact
        retry it protects nothing from. Reconciliation is a READ of achieved
        state and now runs first: the retry returns the achieved row
        (terminality included), appends nothing, and everything that is NOT
        the exact retry still meets the gate."""
        row = self.add(recipient="grok", kind="build")
        first, err = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                      repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        _c, cerr = dispatches.mark_cancel(row["id"], "work moot")
        self.assertIsNone(cerr, cerr)
        again, err2 = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                       repo=self.repo, notify=False)
        self.assertIsNone(err2, "a delayed exact retry of a committed retip "
                          "must reconcile, not refuse: %s" % err2)
        self.assertEqual((again["id"], again["tip"]), (row["id"], self.b))
        self.assertEqual(again["identity"], "verified",
                         "the reconciled retry carries the hop's durable stamp")
        self.assertEqual(again["status"], "cancelled",
                         "the reconciled return is the ACHIEVED state, "
                         "terminality included — never a resurrected row")
        self.assertEqual(len(self.raw_events(row["id"], "retip")), 1,
                         "reconciling is a READ: no second event")
        other, err3 = dispatches.retip(row["id"], self.b, reason="different",
                                       repo=self.repo, notify=False)
        self.assertIsNone(other)
        self.assertIn("only an OPEN row", err3,
                      "the control: a non-exact request on a terminal row "
                      "still meets the gate")

    def test_a_retip_STALES_an_observed_delivery_so_the_recipient_is_re_told(self):
        """An `observed` delivery was earned by a message about the OLD tip.
        Leaving it observed across a retip tells every delivery surface the
        recipient has been told — about a sha that is no longer the row's.
        The row then stops re-surfacing at exactly the moment it has something
        new to say, and the recorded confirmation outlives the fact it
        confirms. Both the writer's return and a FRESH replay must read
        needs-confirmation; `delivery_ref` survives as the audit trail of what
        WAS confirmed."""
        row = self.add(recipient="grok", kind="build")
        _obs, derr = dispatches._mark_delivered(row["id"], "chat:old-tip-msg")
        self.assertIsNone(derr, derr)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["delivery"],
                         "observed",
                         "MUST-HIT: the fixture has to reach `observed` or "
                         "this arm proves nothing about staling it")
        out, err = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["delivery"], "needs-confirmation",
                         "the writer's return must not claim a delivery the "
                         "ledger will replay as stale")
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(live["delivery"], "needs-confirmation",
                         "the FOLD is the one that matters: a stale-only-in-"
                         "memory retraction is not recorded at all")
        self.assertEqual(live["delivery_ref"], "chat:old-tip-msg",
                         "only the CLAIM is retracted — the history of having "
                         "made it is the audit trail and stays")

    def test_a_retip_leaves_an_UNCONFIRMED_delivery_exactly_where_it_was(self):
        """The other half of the partition, so the staling is a RULE and not a
        blanket overwrite: a row that never reached `observed` has no claim to
        retract, and a retip must not invent delivery state for it."""
        row = self.add(recipient="grok", kind="build")
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["delivery"],
                         "needs-confirmation")
        out, err = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["delivery"], "needs-confirmation")
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(live["delivery"], "needs-confirmation")
        self.assertIsNone(live.get("delivery_ref"))
