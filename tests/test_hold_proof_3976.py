"""Door admission reads a source-clean holder's proof frozen at the hold."""
import io
import os
from unittest import mock

from helm import autoland, dispatches, dispatches_tier, home, landreq, proxywatch, seats, store
from tests import test_dispatches as _dispatch
from tests import test_source_clean_landed as _source_clean
from tests._runtime_proof import runtime_proof


class HoldProof3976(_dispatch.DispatchBase):
    SEAT = "reviewer-3976"
    SESSION = "session-hold-proof-3976"

    def setUp(self):
        super().setUp()
        self.proof = runtime_proof(session=self.SESSION)
        runtime, why = proxywatch._proxy_proof_runtime(self.proof)
        self.assertIsNone(why, why)
        self.family = runtime["family"]
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": self.SEAT}):
            seats.write_roster(self.SEAT, session=self.SESSION,
                               presence_beat=False)
        stamped, why = seats.stamp_proxy_runtime(self.SESSION, runtime,
                                                   self.proof)
        self.assertIsNone(why, why)
        self.assertEqual(stamped["proxy_proof"], self.proof)
        self.policy(["family:" + self.family])

    def policy(self, members):
        return store.write_prior({
            "id": "approval-3976", "statement": "Reviewers of a door must be admitted.",
            "confidence": 1.0, "stated_ts": "2026-07-29T00:00:00Z",
            "source": "human", "policy_kind": "approval-tier",
            "policy_members": members, "policy_reason": "owner approval rule"},
            root_dir=os.path.join(home.global_dir(), "premises"))

    def hold(self):
        row = self.add(recipient=self.SEAT, lane="lane/safety-door-3976",
                       ref=self.side)
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=(self.SEAT, None)), \
                mock.patch.object(home, "session_id",
                                  return_value=self.SESSION):
            held, why = dispatches.mark_hold(
                row["id"], "source clean, awaiting the land gate; fab Ran 5 tests OK",
                source_clean_tip=self.side)
        return row, held, why

    def car(self, row):
        replay, why = dispatches.snapshot()
        self.assertIsNone(why, why)
        projected, why = landreq.project()
        self.assertIsNone(why, why)
        self.assertEqual(replay[row["id"]]["status"], "held")
        self.assertEqual(projected[row["id"]]["source_clean_tip"], self.side)
        return {"id": row["id"], "lane": row["lane"], "tip": self.side,
                "basis": "source-clean", "lr": projected[row["id"]]}

    def test_proven_proxy_holder_remains_admitted_after_its_upstream_walls(self):
        with mock.patch.object(proxywatch, "proxy_runtime_snapshot",
                               return_value=(self.family, self.proof, None)):
            row, held, why = self.hold()
        self.assertIsNone(why, why)
        self.assertEqual(held["source_clean_tip"], self.side)
        car = self.car(row)
        with mock.patch.object(proxywatch, "proxy_runtime_snapshot",
                               return_value=(self.family, self.proof, None)):
            before = autoland.Ops().car_facts(self.repo, car)
        self.assertIn("guard", before["doors"])
        self.assertEqual(before["admit"], (True, None), before)
        with mock.patch.object(proxywatch, "proxy_runtime_snapshot",
                               return_value=(None, None, "upstream walled")) as live:
            facts = autoland.Ops().car_facts(self.repo, car)
        live.assert_not_called()
        self.assertEqual(facts["reader"], self.SEAT)
        self.assertEqual(facts["admit"], (True, None))
        self.assertEqual(landreq.source_clean_car(car["lr"]), (self.side, None))

    def test_unproven_holder_can_hold_but_door_car_is_refused(self):
        with mock.patch.object(proxywatch, "proxy_runtime_snapshot",
                               return_value=(None, None, "upstream walled")):
            row, held, why = self.hold()
        self.assertIsNone(why, why)
        self.assertEqual(held["source_clean_tip"], self.side)
        self.assertNotIn("hold_approval", held)
        car = self.car(row)
        with mock.patch.object(proxywatch, "proxy_runtime_snapshot",
                               return_value=(None, None, "upstream walled")):
            facts = autoland.Ops().car_facts(self.repo, car)
        self.assertIn("guard", facts["doors"])
        self.assertFalse(facts["admit"][0], facts)
        self.assertIn("no proven approval-tier holder at the hold",
                      facts["admit"][1])
        tip, refusal = landreq.source_clean_car(car["lr"])
        self.assertIsNone(tip)
        self.assertIn("no proven approval-tier holder at the hold", refusal)
        tick = autoland._Tick(self.repo, False, autoland.Ops(), io.StringIO())
        with mock.patch.object(proxywatch, "proxy_runtime_snapshot",
                               return_value=(None, None, "upstream walled")):
            allowed, barred = tick.admitted([car])
        self.assertEqual(allowed, [])
        self.assertEqual(len(barred), 1)
        self.assertEqual(barred[0][0]["id"], row["id"])

    def test_unknown_seat_cannot_record_a_source_clean_hold(self):
        row = self.add(recipient=self.SEAT, lane="lane/safety-door-3976",
                       ref=self.side)
        with mock.patch.object(home, "chat_name", return_value=None), \
                mock.patch.object(home, "session_id",
                                  return_value="session-unknown-3976"):
            held, why = dispatches.mark_hold(
                row["id"], "source clean but seat unknown; fab Ran 5 tests OK",
                source_clean_tip=self.side)
        self.assertIsNone(held)
        self.assertIn("family floor", why)
        replay, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        self.assertEqual(replay[row["id"]]["status"], "open")
        self.assertNotIn("source_clean_tip", replay[row["id"]])

    def test_owner_demotion_after_proven_hold_refuses_final_admission(self):
        with mock.patch.object(proxywatch, "proxy_runtime_snapshot",
                               return_value=(self.family, self.proof, None)):
            row, held, why = self.hold()
        self.assertIsNone(why, why)
        self.assertIn("hold_approval", held)
        car = self.car(row)
        with mock.patch.object(proxywatch, "proxy_runtime_snapshot",
                               return_value=(None, None, "upstream walled")):
            before = autoland.Ops().car_facts(self.repo, car)
        self.assertEqual(before["admit"], (True, None), before)
        self.policy(["seat:some-other-reviewer"])
        stream = io.StringIO()
        ops = autoland.Ops()
        tick = autoland._Tick(self.repo, False, ops, stream)
        state = {"train": "train-3976", "name": "train-3976",
                 "cars": [{"id": row["id"], "lane": row["lane"],
                           "tip": self.side, "task": None}],
                 "state": autoland.LANDING, "step": None}
        with mock.patch.object(ops, "plan", return_value=(
                {"cars": [car], "ejections_unknown": None}, None)), \
                mock.patch.object(tick, "still_ready", return_value=([car], [])), \
                mock.patch.object(proxywatch, "proxy_runtime_snapshot",
                                  return_value=(None, None, "upstream walled")):
            result = tick.ready_at_the_push(state, self.side)
        self.assertEqual(result, 0)
        self.assertEqual(state["step"], "verified")
        self.assertIn("not admitted", stream.getvalue())
        self.assertIn("current approval tier", stream.getvalue())
        replay, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        self.assertEqual(replay[row["id"]]["status"], "held")

    def test_owner_demotion_after_recorded_push_does_not_strand_the_fold(self):
        with mock.patch.object(proxywatch, "proxy_runtime_snapshot",
                               return_value=(self.family, self.proof, None)):
            row, held, why = self.hold()
        self.assertIsNone(why, why)
        self.assertIn("hold_approval", held)
        car = self.car(row)
        ops = autoland.Ops()
        tick = autoland._Tick(self.repo, True, ops, io.StringIO())
        admitted, barred = tick.admitted([car])
        self.assertEqual(barred, [])
        self.assertEqual([c["id"] for c in admitted], [row["id"]])
        train = {"v": 1, "train": "train-3976", "name": "train-3976",
                 "state": autoland.LANDING, "step": "pushing", "head": self.side,
                 "trunk": self.c, "cars": admitted, "history": [], "posted": []}
        tick.save(train, "ready to push", create=True)
        with mock.patch.object(ops, "plan", return_value=(
                {"cars": [car], "ejections_unknown": None}, None)), \
                mock.patch.object(tick, "still_ready",
                                  return_value=([car], [])):
            self.assertIsNone(tick.ready_at_the_push(train, self.side))
        # Model the push record the tick writes after the successful last word.
        # A tip merely on trunk, without this active pushed train, is not the
        # authority being exercised here.
        self.git("merge", "--no-ff", "-q", "-X", "theirs", "side")
        head = self.git("rev-parse", "HEAD")
        self.git("config", "remote.origin.url", "fixture://trunk")
        self.git("update-ref", "refs/remotes/origin/main", head)
        train.update(head=head, step="pushed", pushed_ts=tick.now)
        tick.save(train, "pushed %s" % head[:12])
        self.assertEqual(autoland.read_train(self.repo, train["train"])["step"],
                         "pushed")
        receipt = _source_clean.SourceCleanBase.receipt(self, head)
        _source_clean.SourceCleanBase.append_receipt(self, receipt)
        info = dispatches._repo_info(self.repo)
        train["receipt"] = {"id": receipt["id"]}
        train["push_admission"] = {
            "v": 2, "repo": info["repo"], "repo_id": info["repo_id"],
            "head": head, "gate": receipt["id"],
            "target": "refs/heads/main at fixture://trunk", "remote": "origin",
            "cars": [{"id": row["id"], "lane": row["lane"],
                      "tip": self.side, "hold_ts": held["hold_ts"],
                      "hold_actor": held["hold_actor"],
                      "anchor": held["hold_approval"]["anchor"]}]}
        # The final DOOR can appear after composition; the push snapshot, not
        # the original car's stale door list, is the admission authority.
        train["cars"][0]["doors"] = []
        tick.save(train, "successful train-issued push proof")
        token = "gate:" + receipt["id"]
        before, err = landreq.source_clean_landings(self.repo, head, token)
        self.assertIsNone(err, err)
        self.assertEqual([(e["id"], e["verdict"]) for e in before],
                         [(row["id"], "QUALIFIES")], before)
        with mock.patch("helm.registry.load",
                        side_effect=OSError("synthetic registry unavailable")):
            unreadable, err = landreq.source_clean_landings(
                self.repo, head, token, apply=True)
        self.assertIsNone(err, err)
        self.assertEqual([(e["id"], e["verdict"]) for e in unreadable],
                         [(row["id"], "REFUSED")], unreadable)

        self.policy(["seat:some-other-reviewer"])
        pushed = train["push_admission"]
        # A push to A does not license closure after the same remote name is
        # repointed to B, even when B has the identical trunk commit locally.
        self.git("config", "remote.origin.url", "fixture://other-trunk")
        borrowed, err = landreq.source_clean_landings(
            self.repo, head, token, apply=True)
        self.assertIsNone(err, err)
        self.assertEqual([(e["id"], e["verdict"]) for e in borrowed],
                         [(row["id"], "REFUSED")], borrowed)
        self.git("config", "remote.origin.url", "fixture://trunk")
        self.git("config", "remote.origin.pushurl", "fixture://other-trunk")
        diverted, err = landreq.source_clean_landings(
            self.repo, head, token, apply=True)
        self.assertIsNone(err, err)
        self.assertEqual([(e["id"], e["verdict"]) for e in diverted],
                         [(row["id"], "REFUSED")], diverted)
        self.git("config", "--unset", "remote.origin.pushurl")
        self.git("config", "--add", "remote.origin.pushurl", "fixture://trunk")
        self.git("config", "--add", "remote.origin.pushurl", "fixture://other-trunk")
        mirrored, err = landreq.source_clean_landings(
            self.repo, head, token, apply=True)
        self.assertIsNone(err, err)
        self.assertEqual([(e["id"], e["verdict"]) for e in mirrored],
                         [(row["id"], "REFUSED")], mirrored)
        self.git("config", "--unset-all", "remote.origin.pushurl")
        for damaged in (None, dict(pushed, head=self.side),
                        dict(pushed, gate="f" * 16),
                        dict(pushed, remote="mirror"),
                        dict(pushed, target="refs/heads/other at fixture://trunk"),
                        dict(pushed, repo_id="/other/repo/.git"),
                        dict(pushed, cars=[]),
                        dict(pushed, cars=[dict(pushed["cars"][0],
                                                anchor="a" * 64)])):
            train["push_admission"] = damaged
            tick.save(train, "damaged push proof refuses closure")
            refused, err = landreq.source_clean_landings(
                self.repo, head, token, apply=True)
            self.assertIsNone(err, err)
            self.assertEqual([(e["id"], e["verdict"]) for e in refused],
                             [(row["id"], "REFUSED")], refused)
        train["push_admission"] = pushed
        train["step"] = "pushing"
        tick.save(train, "not yet a recorded successful push")
        unpushed, err = landreq.source_clean_landings(
            self.repo, head, token, apply=True)
        self.assertIsNone(err, err)
        self.assertEqual([(e["id"], e["verdict"]) for e in unpushed],
                         [(row["id"], "REFUSED")], unpushed)
        train["step"] = "pushed"
        tick.save(train, "successful push proof restored")
        local, err = landreq.close(
            row["id"], "source-clean-landed", repo=self.repo,
            trunk="refs/heads/" + self.main, gate=token, dry_run=True)
        self.assertIsNone(local)
        self.assertIn("current approval tier", err)
        after, err = landreq.source_clean_landings(
            self.repo, head, token, apply=True)
        self.assertIsNone(err, err)
        self.assertEqual([(e["id"], e["verdict"]) for e in after],
                         [(row["id"], "CLOSED")], after)
        replay, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        self.assertEqual(replay[row["id"]]["close_reason"],
                         "source-clean-landed")

    def test_non_origin_successful_push_closes_on_upstream_not_local_main(self):  # noqa: VACUOUS_ASSERTION — the same landing closes on upstream after three refusal controls
        if self.main != "main":
            self.git("branch", "-m", "main")
        self.git("config", "helm.trunkRef", "refs/heads/main")
        self.git("config", "helm.trunkRemote", "upstream")
        self.git("config", "helm.trunkUrl", "fixture://trunk")
        self.git("config", "remote.upstream.url", "fixture://trunk")
        self.assertEqual(self.git("remote"), "upstream")  # no origin
        with mock.patch.object(proxywatch, "proxy_runtime_snapshot",
                               return_value=(self.family, self.proof, None)):
            row, held, why = self.hold()
        self.assertIsNone(why, why)
        tick = autoland._Tick(self.repo, True, autoland.Ops(), io.StringIO())
        admitted, barred = tick.admitted([self.car(row)])
        self.assertEqual(barred, [])
        self.assertEqual([c["id"] for c in admitted], [row["id"]])
        self.git("merge", "--no-ff", "-q", "-X", "theirs", "side")
        head = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/upstream/main", head)
        self.git("reset", "--hard", self.c)  # local main did not receive the push
        train = {"v": 1, "train": "train-upstream-3976", "name": "train-upstream-3976",
                 "state": autoland.LANDING, "step": "pushed", "head": head,
                 "trunk": self.c, "cars": admitted, "history": [], "posted": [],
                 "pushed_ts": tick.now}
        tick.save(train, "recorded upstream push", create=True)
        receipt = _source_clean.SourceCleanBase.receipt(self, head)
        _source_clean.SourceCleanBase.append_receipt(self, receipt)
        info = dispatches._repo_info(self.repo)
        train["receipt"] = {"id": receipt["id"]}
        train["push_admission"] = {
            "v": 2, "repo": info["repo"], "repo_id": info["repo_id"],
            "head": head, "gate": receipt["id"],
            "target": "refs/heads/main at fixture://trunk", "remote": "upstream",
            "cars": [{"id": row["id"], "lane": row["lane"],
                      "tip": self.side, "hold_ts": held["hold_ts"],
                      "hold_actor": held["hold_actor"],
                      "anchor": held["hold_approval"]["anchor"]}]}
        tick.save(train, "successful upstream push proof")
        self.policy(["seat:some-other-reviewer"])
        token = "gate:" + receipt["id"]
        local, refusal = landreq.close(
            row["id"], "source-clean-landed", repo=self.repo,
            trunk="refs/heads/main", gate=token, dry_run=True)
        self.assertIsNone(local)
        self.assertIn("current approval tier", refusal)
        self.git("config", "helm.trunkRemote", "mirror")
        self.git("config", "remote.mirror.url", "fixture://trunk")
        missing, err = landreq.source_clean_landings(
            self.repo, head, token, apply=True)
        self.assertIsNone(err, err)
        self.assertEqual([(e["id"], e["verdict"]) for e in missing],
                         [(row["id"], "REFUSED")], missing)
        self.git("update-ref", "refs/remotes/mirror/main", head)
        other, err = landreq.source_clean_landings(
            self.repo, head, token, apply=True)
        self.assertIsNone(err, err)
        self.assertEqual([(e["id"], e["verdict"]) for e in other],
                         [(row["id"], "REFUSED")], other)
        self.git("config", "helm.trunkRemote", "upstream")
        closed, err = landreq.source_clean_landings(
            self.repo, head, token, apply=True)
        self.assertIsNone(err, err)
        self.assertEqual([(e["id"], e["verdict"]) for e in closed],
                         [(row["id"], "CLOSED")], closed)
        self.assertIn("refs/remotes/upstream/main", closed[0]["why"])

    def test_manual_land_after_demotion_cannot_borrow_final_admission(self):
        with mock.patch.object(proxywatch, "proxy_runtime_snapshot",
                               return_value=(self.family, self.proof, None)):
            row, held, why = self.hold()
        self.assertIsNone(why, why)
        self.assertIn("hold_approval", held)
        self.git("merge", "--no-ff", "-q", "-X", "theirs", "side")
        head = self.git("rev-parse", "HEAD")
        receipt = _source_clean.SourceCleanBase.receipt(self, head)
        _source_clean.SourceCleanBase.append_receipt(self, receipt)
        self.policy(["seat:some-other-reviewer"])
        rows, err = landreq.source_clean_landings(
            self.repo, head, "gate:" + receipt["id"], apply=True)
        self.assertIsNone(err, err)
        self.assertEqual([(e["id"], e["verdict"]) for e in rows],
                         [(row["id"], "REFUSED")], rows)
        replay, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        self.assertEqual(replay[row["id"]]["status"], "held")

    def test_current_policy_uses_held_checkout_not_common_git_directory(self):
        with mock.patch.object(proxywatch, "proxy_runtime_snapshot",
                               return_value=(self.family, self.proof, None)):
            row, held, why = self.hold()
        self.assertIsNone(why, why)
        self.assertIn("hold_approval", held)
        replay, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        checked = []
        admitted_at_hold = held["hold_approval"]["policy"]
        demoted = dict(admitted_at_hold,
                       policy_members=["seat:some-other-reviewer"])

        def policy(path):
            checked.append(path)
            return (demoted if path == self.repo else admitted_at_hold), None

        car = self.car(row)
        with mock.patch.object(dispatches, "_hold_policy", side_effect=policy):
            admitted, reason = dispatches_tier.hold_approval(
                replay[row["id"]], self.repo + "/.git")
            facts = autoland.Ops().car_facts(self.repo + "/.git", car)
            tip, compose_reason = landreq.source_clean_car(car["lr"])
        self.assertFalse(admitted, reason)
        self.assertFalse(facts["admit"][0], facts)
        self.assertIsNone(tip)
        self.assertIn("current approval tier", compose_reason)
        self.assertEqual(checked, [self.repo] * 3)
        self.assertIn("current approval tier", reason)

    def test_boolean_proof_version_cannot_replay_as_version_one(self):
        with mock.patch.object(proxywatch, "proxy_runtime_snapshot",
                               return_value=(self.family, self.proof, None)):
            row, held, why = self.hold()
        self.assertIsNone(why, why)
        self.assertIn("hold_approval", held)
        replay, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        malformed = dict(replay[row["id"]])
        proof = dict(malformed["hold_approval"])
        proof["v"] = True
        proof["anchor"] = dispatches._proof_anchor(
            "source-clean-holder-v1", {k: v for k, v in proof.items()
                                       if k != "anchor"})
        malformed["hold_approval"] = proof
        self.assertFalse(dispatches_tier.hold_approval(
            malformed, self.repo, current=False)[0])

    def test_unreadable_registry_at_hold_cannot_borrow_later_policy(self):
        with mock.patch("helm.registry.load",
                        side_effect=OSError("synthetic registry unavailable")):
            row, held, why = self.hold()
        self.assertIsNone(why, why)
        self.assertNotIn("hold_approval", held)
        facts = autoland.Ops().car_facts(self.repo, self.car(row))
        self.assertIn("guard", facts["doors"])
        self.assertFalse(facts["admit"][0], facts)
        self.assertIn("no proven approval-tier holder at the hold",
                      facts["admit"][1])

    def test_unreadable_project_registry_cannot_fall_back_to_global_owner_policy(self):
        with mock.patch.object(proxywatch, "proxy_runtime_snapshot",
                               return_value=(self.family, self.proof, None)):
            row, held, why = self.hold()
        self.assertIsNone(why, why)
        self.assertIn("hold_approval", held)
        car = self.car(row)
        with mock.patch.object(proxywatch, "proxy_runtime_snapshot",
                               return_value=(None, None, "upstream walled")):
            before = autoland.Ops().car_facts(self.repo, car)
        self.assertEqual(before["admit"], (True, None), before)
        with mock.patch("helm.registry.load",
                        side_effect=OSError("synthetic registry unavailable")) as read, \
                mock.patch.object(proxywatch, "proxy_runtime_snapshot",
                                  return_value=(None, None, "upstream walled")):
            facts = autoland.Ops().car_facts(self.repo, car)
        self.assertIn("guard", facts["doors"])
        self.assertFalse(facts["admit"][0], facts)
        self.assertIn("current approval tier", facts["admit"][1])
        read.assert_any_call(strict=True)


class NativeHoldWindow4055(_dispatch.DispatchBase):
    """A NATIVE seat's hold made while its subagents run (task/4055).

    A subagent acts in its seat's name, so `native_turn_model` answers
    UNKNOWN when one named another model in the ten minutes before the hold.
    The ambiguity matters only when it could change admission: every model
    the seat's family, each admitted by the family rule, records a v2 proof
    naming the seat's own answer; any other shape records none, and the fold
    refuses the door."""
    SEAT = "reviewer-4055"
    SESSION = "session-hold-window-4055"
    ANSWER = "claude-opus-5-5"

    def setUp(self):
        import time
        super().setUp()
        self.now = time.time()
        claude = os.path.join(self.tmp, "claude")
        # One key, restored by itself: a patch.dict stopped in a cleanup
        # would restore its snapshot AFTER DispatchBase.tearDown, putting the
        # base's whole test environment back into the process.
        prior = os.environ.get("HELM_CLAUDE_DIR")
        os.environ["HELM_CLAUDE_DIR"] = claude
        self.addCleanup(
            lambda: os.environ.pop("HELM_CLAUDE_DIR", None) if prior is None
            else os.environ.__setitem__("HELM_CLAUDE_DIR", prior))
        self.projects = os.path.join(claude, "projects", "proj")
        os.makedirs(self.projects)
        # The seat answered on one model every few minutes up to the hold.
        with open(os.path.join(self.projects, self.SESSION + ".jsonl"), "w",
                  encoding="utf-8") as handle:
            handle.writelines(self.line(self.now - ago, self.ANSWER)
                              for ago in (900, 600, 300, 60))
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": self.SEAT}):
            seats.write_roster(self.SEAT, session=self.SESSION,
                               presence_beat=False)
        self.evidence = {
            "v": 5, "identity": self.SEAT, "roster_identity": self.SEAT,
            "runtime": {"agent_harness": "claude", "family": "claude",
                        "backend": "native"},
            "runtime_verified": True, "session": self.SESSION}
        anchor = dispatches._subsumed_family_anchor(self.evidence)
        native = mock.patch.object(
            dispatches, "_approval_identity_family_evidence",
            lambda recipient, session=None, require_exact_session=False:
            ({"claude"}, self.evidence, anchor, None))
        native.start()
        self.addCleanup(native.stop)
        self.policy(["family:claude"])

    policy = HoldProof3976.policy
    car = HoldProof3976.car

    def hold(self, lane="lane/safety-door-4055"):
        row = self.add(recipient=self.SEAT, lane=lane, ref=self.side)
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=(self.SEAT, None)), \
                mock.patch.object(home, "session_id",
                                  return_value=self.SESSION):
            held, why = dispatches.mark_hold(
                row["id"], "source clean, awaiting the land gate; fab Ran 5 tests OK",
                source_clean_tip=self.side)
        return row, held, why

    def line(self, epoch, model, **extra):
        import json
        import time
        return json.dumps(dict({
            "type": "assistant", "sessionId": self.SESSION,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                       time.gmtime(epoch)),
            "message": {"role": "assistant", "model": model}}, **extra)) + "\n"

    def workflow_agent(self, model, name="agent-w1.jsonl"):
        """A workflow agent of the seat, writing two minutes before the hold
        where the harness writes one: <session>/subagents/workflows/<run>/."""
        directory = os.path.join(self.projects, self.SESSION, "subagents",
                                 "workflows", "wf_4055")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.line(self.now - 120, model, isSidechain=True))
        os.utime(path, (self.now - 120, self.now - 120))
        return path

    def refused(self, row, held):
        """The hold stands with no proof, and the fold refuses its DOOR."""
        self.assertEqual(held["source_clean_tip"], self.side)
        self.assertNotIn("hold_approval", held)
        tip, refusal = landreq.source_clean_car(self.car(row)["lr"])
        self.assertIsNone(tip)
        self.assertIn("no proven approval-tier holder at the hold", refusal)

    def test_the_seat_alone_records_the_version_one_proof(self):
        row, held, why = self.hold()
        self.assertIsNone(why, why)
        proof = held["hold_approval"]
        self.assertEqual((proof["v"], proof["model"]), (1, self.ANSWER))
        self.assertNotIn("window_models", proof)
        self.assertEqual(landreq.source_clean_car(self.car(row)["lr"]),
                         (self.side, None))

    def test_a_same_family_workflow_agent_records_a_window_proof(self):
        self.workflow_agent("claude-opus-4-8")
        row, held, why = self.hold()
        self.assertIsNone(why, why)
        proof = held["hold_approval"]
        self.assertEqual(proof["v"], 2)
        self.assertEqual(proof["model"], self.ANSWER)
        self.assertEqual(proof["window_models"],
                         ["claude-opus-4-8", self.ANSWER])
        car = self.car(row)
        # The fold accepts it: compose admission, the lr car and the replay.
        self.assertEqual(landreq.source_clean_car(car["lr"]),
                         (self.side, None))
        facts = autoland.Ops().car_facts(self.repo, car)
        self.assertIn("guard", facts["doors"])
        self.assertEqual(facts["admit"], (True, None), facts)
        replay, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        self.assertEqual(replay[row["id"]]["hold_approval"], proof)
        self.assertEqual(dispatches_tier.hold_approval(
            replay[row["id"]], self.repo, current=False), (True, None))

    def fresh_read(self, row, model=None, run=None):
        """Append the seat's fresh-context CONCUR of the held tip: the read
        the verdict verb records once its run passed the checks on disk, and
        the read its auto-hold then rests on."""
        current, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        before = current[row["id"]]
        event = {"v": 4, "event": dispatches.ADVISORY_READ_EVENT,
                 "seq": before["seq"] + 1, "id": row["id"],
                 "ts": dispatches.pk.now_ts(), "reviewed_tip": self.side,
                 "verdict_ref": "fresh-context read clean; fab Ran 5 tests OK",
                 "polarity": "concur",
                 "reviewer_model": model or self.ANSWER,
                 "reviewer_run": run or "a" + "5" * 16,
                 "author_model": self.ANSWER,
                 "author_model_source": "declared", "recorded_by": self.SEAT,
                 "reviewer_family": "claude", "independence": "fresh-context"}
        out, why = dispatches._ledger_write(
            lambda txn: (event, None) if txn.append(event)
            else (None, "ledger unwritable"))
        self.assertIsNone(why, why)
        current, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        reads = current[row["id"]].get("advisory_reads") or ()
        self.assertEqual([r["reviewer_run"] for r in reads],
                         [event["reviewer_run"]], "the read did not replay")
        return current[row["id"]]

    def hold_on_run(self, lane="lane/safety-door-4220", **read):
        row = self.add(recipient=self.SEAT, lane=lane, ref=self.side)
        self.fresh_read(row, **read)
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=(self.SEAT, None)), \
                mock.patch.object(home, "session_id",
                                  return_value=self.SESSION):
            held, why = dispatches.mark_hold(
                row["id"], "SOURCE-CLEAN: fresh-context run read it clean",
                source_clean_tip=self.side)
        return row, held, why

    def test_a_hold_on_a_verified_opus_run_is_proven_despite_a_sonnet_window(self):
        """task/4220. The seat ran an unrelated Sonnet build subagent in the
        window; its hold rests on a verified fresh-context Opus read run, so
        the proof is the run's (v3), and the door admits it."""
        self.workflow_agent("claude-sonnet-5")
        row, held, why = self.hold_on_run()
        self.assertIsNone(why, why)
        proof = held.get("hold_approval")
        self.assertIsInstance(proof, dict, held)
        self.assertEqual((proof["v"], proof["model"], proof["run"],
                          proof["family"]),
                         (3, self.ANSWER, "a" + "5" * 16, "claude"))
        self.assertNotIn("window_models", proof)
        car = self.car(row)
        self.assertEqual(landreq.source_clean_car(car["lr"]),
                         (self.side, None))
        facts = autoland.Ops().car_facts(self.repo, car)
        self.assertIn("guard", facts["doors"])
        self.assertEqual(facts["admit"], (True, None), facts)
        replay, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        self.assertEqual(replay[row["id"]]["hold_approval"], proof)
        self.assertEqual(dispatches_tier.hold_approval(
            replay[row["id"]], self.repo, current=False), (True, None))
        self.assertEqual(dispatches_tier.hold_approval(
            replay[row["id"]], self.repo), (True, None))

    def test_a_plain_hold_in_the_same_sonnet_window_stays_unproven(self):  # noqa: VACUOUS_ASSERTION — `refused` asserts the fold's positive refusal sentence on the same car, and the run-rested hold in the same window is asserted proven first
        self.workflow_agent("claude-sonnet-5")
        # POSITIVE CONTROL: the same window, a hold resting on a run.
        _row, held, why = self.hold_on_run(lane="lane/safety-door-4220-run")
        self.assertIsNone(why, why)
        self.assertEqual(held["hold_approval"]["v"], 3)
        row, held, why = self.hold(lane="lane/safety-door-4220-plain")
        self.assertIsNone(why, why)
        self.refused(row, held)

    def test_a_run_whose_model_is_input_only_or_another_family_mints_nothing(self):  # noqa: VACUOUS_ASSERTION — each literal model is asserted to mint None, and the untampered row is asserted to mint a v3 proof through the same call first
        """Replay never carries a Sonnet fresh-context read, so the minting
        call is driven with the row as it would read: the run path never
        admits an input-only or other-family model, and the Sonnet window
        then refuses the seat's own rule too."""
        self.workflow_agent("claude-sonnet-5")
        row = self.add(recipient=self.SEAT, lane="lane/safety-door-4220-c",
                       ref=self.side)
        good = self.fresh_read(row)

        def mint(model):
            reads = [dict(r, reviewer_model=model)
                     for r in good["advisory_reads"]]
            with mock.patch.object(home, "session_id",
                                   return_value=self.SESSION):
                return dispatches_tier.record_hold_approval(
                    dict(good, advisory_reads=reads), self.SEAT, self.side,
                    dispatches.pk.now_ts())

        # POSITIVE CONTROL: the Opus run mints.
        self.assertEqual(mint(self.ANSWER)["v"], 3)
        for model in ("claude-sonnet-5", "claude-haiku-4-5", "gpt-5.5",
                      "claude-foo"):
            with self.subTest(model=model):
                self.assertIsNone(mint(model))

    def test_a_run_proof_is_judged_against_the_run_on_the_row(self):  # noqa: VACUOUS_ASSERTION — the untampered v3 proof judges (True, None) through the same call first
        self.workflow_agent("claude-sonnet-5")
        row, held, why = self.hold_on_run()
        self.assertIsNone(why, why)
        replay, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        good = replay[row["id"]]
        self.assertEqual(dispatches_tier.hold_approval(
            good, self.repo, current=False), (True, None))

        def judged(row=good, **change):
            return dispatches_tier.hold_approval(
                self.reanchored(row, **change), self.repo, current=False)[0]

        # Another run, or none, is not the run the hold rests on.
        self.assertFalse(judged(run="a" + "6" * 16))
        self.assertFalse(judged(run=""))
        # The model must be the run's, and a reviewing model of the family.
        for model in ("claude-opus-4-8", "claude-sonnet-5", "gpt-5.5"):
            with self.subTest(model=model):
                self.assertFalse(judged(model=model))
        # The row must still carry the read the proof names.
        self.assertFalse(judged(row=dict(good, advisory_reads=())))
        self.assertFalse(judged(row=dict(good, advisory_reads=tuple(
            dict(r, recorded_by="another-seat")
            for r in good["advisory_reads"]))))
        self.assertFalse(judged(row=dict(good, advisory_reads=tuple(
            dict(r, polarity="fix") for r in good["advisory_reads"]))))
        # A version that never had `run` refuses a proof that carries one.
        for version in (1, 2):
            with self.subTest(version=version):
                self.assertFalse(judged(v=version))
        # A later per-model policy that drops the run's model demotes it.
        self.policy(["model:claude-opus-4-8"])
        ok, why = dispatches_tier.hold_approval(good, self.repo)
        self.assertFalse(ok)
        self.assertIn("current approval tier", why)

    def test_another_family_in_the_window_records_no_proof(self):  # noqa: VACUOUS_ASSERTION — `refused` asserts the fold's positive refusal sentence on the same car, and test_a_same_family_workflow_agent_records_a_window_proof is this fixture's admitted control
        for model in ("gpt-6.1-sol", "gpt-5.5"):
            with self.subTest(model=model):
                path = self.workflow_agent(model)
                row, held, why = self.hold(lane="lane/safety-door-4055-" + model)
                self.assertIsNone(why, why)
                self.refused(row, held)
                os.remove(path)

    def test_an_input_only_model_in_the_window_records_no_proof(self):  # noqa: VACUOUS_ASSERTION — `refused` asserts the fold's positive refusal sentence on the same car, and test_a_same_family_workflow_agent_records_a_window_proof is this fixture's admitted control
        self.workflow_agent("claude-sonnet-5")
        row, held, why = self.hold()
        self.assertIsNone(why, why)
        self.refused(row, held)

    def test_a_per_model_rule_with_disagreeing_candidates_records_no_proof(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the first hold in the body: the same policy records a v1 proof for the seat alone
        self.policy(["model:claude-opus-5-5", "model:claude-opus-4-8"])
        # POSITIVE CONTROL: the same policy admits the seat alone.
        row, held, why = self.hold()
        self.assertIsNone(why, why)
        self.assertEqual(held["hold_approval"]["v"], 1)
        self.workflow_agent("claude-opus-4-8")
        row, held, why = self.hold(lane="lane/safety-door-4055-window")
        self.assertIsNone(why, why)
        self.refused(row, held)

    def test_an_unreadable_subagent_transcript_records_no_proof(self):  # noqa: VACUOUS_ASSERTION — `refused` asserts the fold's positive refusal sentence on the same car, and test_a_same_family_workflow_agent_records_a_window_proof is this fixture's admitted control
        from helm import native_turn
        path = self.workflow_agent("claude-opus-4-8")
        real = open

        def refuse(name, *rest, **kw):
            if name == path:
                raise PermissionError(13, "denied", name)
            return real(name, *rest, **kw)

        with mock.patch.object(native_turn, "open", refuse, create=True):
            row, held, why = self.hold()
        self.assertIsNone(why, why)
        self.refused(row, held)

    def test_a_window_proof_is_judged_as_strictly_as_the_first(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the first assertion: the untampered v2 proof judges (True, None) through the same call
        self.workflow_agent("claude-opus-4-8")
        row, held, why = self.hold()
        self.assertIsNone(why, why)
        replay, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        good = replay[row["id"]]
        self.assertEqual(dispatches_tier.hold_approval(
            good, self.repo, current=False), (True, None))

        def judged(**change):
            proof = dict(good["hold_approval"], **change)
            proof = {k: v for k, v in proof.items() if v is not None}
            proof["anchor"] = dispatches._proof_anchor(
                "source-clean-holder-v%d" % proof["v"],
                {k: v for k, v in proof.items() if k != "anchor"})
            return dispatches_tier.hold_approval(
                dict(good, hold_approval=proof), self.repo, current=False)

        # Relabelled as version one, it keeps a key version one never had.
        self.assertFalse(judged(v=1)[0])
        # A version two that dropped its window is not a version two.
        self.assertFalse(judged(window_models=None)[0])
        for window in ([self.ANSWER], [self.ANSWER, "claude-opus-4-8"],
                       ["claude-opus-4-8", "claude-opus-5"],
                       ["claude-opus-4-8", "gpt-6.1-sol", self.ANSWER],
                       ["claude-sonnet-5", self.ANSWER]):
            with self.subTest(window=window):
                self.assertFalse(judged(window_models=window)[0])
        # A window behind a stamped runtime was never read off a transcript.
        stamped = dict(self.evidence, runtime=dict(
            self.evidence["runtime"], model=self.ANSWER))
        self.assertFalse(judged(authority=stamped)[0])
        # A later per-model policy demotes it at the current read.
        self.assertEqual(dispatches_tier.hold_approval(good, self.repo),
                         (True, None))
        self.policy(["model:claude-opus-5-5", "model:claude-opus-4-8"])
        ok, why = dispatches_tier.hold_approval(good, self.repo)
        self.assertFalse(ok)
        self.assertIn("current approval tier", why)

    def native_as(self, harness):
        """Bind the seat's family evidence to a runtime labelled `harness`,
        its own anchor recomputed so the evidence stays valid by itself."""
        evidence = dict(self.evidence, runtime=dict(
            self.evidence["runtime"], agent_harness=harness))
        anchor = dispatches._subsumed_family_anchor(evidence)
        native = mock.patch.object(
            dispatches, "_approval_identity_family_evidence",
            lambda recipient, session=None, require_exact_session=False:
            ({"claude"}, evidence, anchor, None))
        native.start()
        self.addCleanup(native.stop)
        return evidence

    def reanchored(self, good, **change):
        """`good` with its hold proof changed and its anchor recomputed."""
        proof = dict(good["hold_approval"], **change)
        proof["anchor"] = dispatches._proof_anchor(
            "source-clean-holder-v%d" % proof["v"],
            {k: v for k, v in proof.items() if k != "anchor"})
        return dict(good, hold_approval=proof)

    def test_a_non_claude_harness_cannot_claim_a_claude_transcript_window(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the first hold in the body: the same transcript and window under the claude harness records a v2 proof; `refused` then asserts the fold's positive refusal sentence
        self.workflow_agent("claude-opus-4-8")
        # POSITIVE CONTROL: the claude harness records the window proof.
        row, held, why = self.hold()
        self.assertIsNone(why, why)
        self.assertEqual(held["hold_approval"]["v"], 2)
        # The same transcript and window, under a runtime labelled pi.
        self.native_as("pi")
        row, held, why = self.hold(lane="lane/safety-door-4055-pi")
        self.assertIsNone(why, why)
        self.refused(row, held)

    def test_relabelled_harness_cannot_replay_a_window_proof(self):
        self.workflow_agent("claude-opus-4-8")
        row, held, why = self.hold()
        self.assertIsNone(why, why)
        replay, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        good = replay[row["id"]]
        # POSITIVE CONTROL: the untampered proof judges ok.
        self.assertEqual(dispatches_tier.hold_approval(
            good, self.repo, current=False), (True, None))
        pi = dict(self.evidence, runtime=dict(
            self.evidence["runtime"], agent_harness="pi"))
        self.assertIsNone(dispatches._family_evidence_error(
            pi, self.SEAT, "claude", dispatches._subsumed_family_anchor(pi)))
        self.assertEqual(dispatches_tier.hold_approval(
            self.reanchored(good, authority=pi), self.repo, current=False),
            (False, "hold approval window is malformed"))

    def test_an_uncatalogued_claude_spelling_in_the_window_records_no_proof(self):  # noqa: VACUOUS_ASSERTION — `refused` asserts the fold's positive refusal sentence on the same car, and test_a_measured_workflow_id_and_a_catalogued_id_record_a_window_proof is this fixture's admitted control
        for model in ("claude-foo", "claude-sonnetfuture"):
            with self.subTest(model=model):
                path = self.workflow_agent(model)
                row, held, why = self.hold(
                    lane="lane/safety-door-4055-" + model)
                self.assertIsNone(why, why)
                self.refused(row, held)
                os.remove(path)

    def test_an_uncatalogued_claude_spelling_cannot_replay_a_window_proof(self):
        self.workflow_agent("claude-opus-4-8")
        row, held, why = self.hold()
        self.assertIsNone(why, why)
        replay, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        good = replay[row["id"]]
        # POSITIVE CONTROL: the measured id judges ok through the same call.
        self.assertEqual(dispatches_tier.hold_approval(
            good, self.repo, current=False), (True, None))
        for model in ("claude-foo", "claude-sonnetfuture"):
            with self.subTest(model=model):
                window = sorted({model, self.ANSWER})
                self.assertEqual(dispatches_tier.hold_approval(
                    self.reanchored(good, window_models=window), self.repo,
                    current=False),
                    (False, "holder was not admitted at the hold"))

    def test_a_measured_workflow_id_and_a_catalogued_id_record_a_window_proof(self):  # noqa: VACUOUS_ASSERTION — the loop is over a literal two-id tuple, so every arm runs, and each asserts a recorded v2 proof
        for model in ("claude-opus-4-8", "claude-opus-5"):
            with self.subTest(model=model):
                path = self.workflow_agent(model)
                row, held, why = self.hold(
                    lane="lane/safety-door-4055-" + model)
                self.assertIsNone(why, why)
                proof = held["hold_approval"]
                self.assertEqual((proof["v"], proof["model"]),
                                 (2, self.ANSWER))
                self.assertEqual(proof["window_models"],
                                 sorted({model, self.ANSWER}))
                self.assertEqual(landreq.source_clean_car(
                    self.car(row)["lr"]), (self.side, None))
                os.remove(path)

    def test_the_window_family_table_is_measured_ids_and_the_catalog(self):  # noqa: VACUOUS_ASSERTION — each loop is over a literal non-empty tuple, so every arm runs; the first loop is the positive control for the None arms
        from helm import verdict_tier
        self.assertEqual(verdict_tier.NATIVE_WINDOW_MODELS,
                         frozenset({"claude-opus-4-8"}))
        for model in ("claude-opus-4-8", "claude-opus-4-8[1m]",
                      "claude-opus-5", "claude-opus-5-5", "opus"):
            with self.subTest(model=model):
                self.assertEqual(
                    verdict_tier.window_family(model, "claude"), "claude")
        for model in ("claude-foo", "claude-sonnetfuture", "claude-opus-4-9",
                      "claude-"):
            with self.subTest(model=model):
                self.assertIsNone(verdict_tier.window_family(model, "claude"))
        # The table speaks for a claude seat only.
        self.assertIsNone(verdict_tier.window_family("claude-opus-4-8",
                                                     "codex"))
