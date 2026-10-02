#!/usr/bin/env python3
"""The cloud BUILD lane and the standing-session transport (task/3517), end to
end through helm/remote_relay.py: a real dispatch ledger, a real project
repository, a LOCAL bare repository standing in for the private push
repository, and NO NETWORK. `remote_session.RUN` stands in for claude and gh:
a delivery to a standing session is recorded with the session it named, and
the drop is a list of comments this test writes. The cloud sessions' own work
(a pushed build branch, a report comment) is done here with real git.

Every hop is asserted on the LEDGER or in the repositories, never only in what
the relay said: the delivery into a standing session, the refusal of a seat
with none, the hand-back fetched into a local lane branch, the review minted
for a session other than the builder's, the verdict recorded from the
reviewer's report, and the FIX that goes back to the builder as a cure.
"""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helm import (burnflags, dispatches, remote_relay,  # noqa: E402
                  remote_session as rs)
from tests._tmphome import dispatch_home, pin_live_seats  # noqa: E402
from tests.test_remote_relay import (EMAIL, SEAT, RelayBase,  # noqa: E402
                                     run_git)

EMAIL_B = "builder-two@example.com"
SID_A = "session_01StandingAaaa"
SID_B = "session_01StandingBbbb"
IDENTITY = "Build Owner <builder@example.com>"


class StandingBase(RelayBase):
    """Two accounts, each with its own home and its own standing session."""

    def setUp(self):
        self.to_sessions = []
        self.answers = {}
        super().setUp()

    def configure(self, model="opus", doors=(), seats_extra=None,
                  transport="branch", push_url=None, push_repo=None,
                  standing=(SID_A, SID_B), seat_transport=None,
                  effort="high", review_seat=None,
                  bound=("owner/drop", "owner/drop")):
        self.claude_home_b = os.path.join(self.tmp, "claude-home-b")
        os.makedirs(self.claude_home_b, exist_ok=True)
        with open(os.path.join(self.claude_home_b, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"emailAddress": EMAIL_B},
                       "projects": {}}, f)
        with open(os.path.join(self.claude_home, ".credentials.json")) as f:
            creds = f.read()
        with open(os.path.join(self.claude_home_b, ".credentials.json"),
                  "w") as f:
            f.write(creds)
        super().configure(model=model, doors=doors, seats_extra=seats_extra,
                          transport=transport, push_url=push_url,
                          push_repo=push_repo)
        path = os.path.join(os.environ["HELM_HOME"], "_global", rs.CONFIG)
        with open(path) as f:
            cfg = json.load(f)
        cfg["projects"]["fixture"]["build_author"] = IDENTITY
        seat = cfg["seats"][SEAT]
        seat.pop("transport", None)
        if seat_transport:
            seat["transport"] = seat_transport
        if effort:
            seat["effort"] = effort
        accounts = [{"email": EMAIL, "home": self.claude_home},
                    {"email": EMAIL_B, "home": self.claude_home_b}]
        for account, sid, repo in zip(accounts, standing, bound):
            if sid:
                account["standing_session"] = sid
            # the repository the owner attached the session to in the web UI
            if sid and repo:
                account["standing_repo"] = repo
        seat["accounts"] = accounts
        if review_seat:
            cfg["seats"][review_seat] = dict(seat)
            seat["review_seat"] = review_seat
        with open(path, "w") as f:
            json.dump(cfg, f)

    def fake_run(self, argv, cwd=None, env=None, timeout=None):
        if argv[0] == "fake-claude" and "--cloud" in argv and "-p" in argv:
            sid = argv[argv.index("--cloud") + 1]
            self.calls.append({"argv": list(argv), "cwd": cwd, "env": env})
            self.to_sessions.append((sid, argv[argv.index("-p") + 1],
                                     (env or {}).get("CLAUDE_CONFIG_DIR")))
            return self.answers.get(sid, (0, '{"ok": true}', ""))
        return super().fake_run(argv, cwd=cwd, env=env, timeout=timeout)

    # -- the cloud sessions' side, done with real git --------------------

    def build_row(self, lane="feat-x", brief="Build the parser.", tip=None):
        with dispatch_home(self.repo):
            row, why, _posted = dispatches.send(
                SEAT, lane, brief, tip or self.base, repo=self.repo,
                kind="build", sign=False, new_work=True, task=self.task)
        self.assertIsNotNone(row, why)
        return row

    def names(self, row):
        return rs.build_names("cloudrev/", remote_relay.label_of(row))

    def builder_pushes(self, label, text="built\n", message="the build",
                       ident=("Build Owner", "builder@example.com")):
        """Act as the builder: clone the push repository, start from the
        build's start branch, commit as the owner, push the -build branch."""
        names = rs.build_names("cloudrev/", label)
        work = os.path.join(self.tmp, "builder-%d" % len(os.listdir(self.tmp)))
        subprocess.run(["git", "clone", "-q", "-b", names["start"],
                        self.drop_repo, work], check=True, capture_output=True)
        with open(os.path.join(work, "parser.py"), "a") as f:
            f.write(text)
        run_git(work, "add", "-A")
        run_git(work, "-c", "user.name=" + ident[0], "-c",
                "user.email=" + ident[1],
                "commit", "-q", "-m", message)
        run_git(work, "push", "-q", "origin",
                "HEAD:refs/heads/" + names["build"])
        return run_git(work, "rev-parse", "HEAD")

    def handback_comment(self, label, tip, kind="BUILD", cid=31,
                         branch=None):
        body = "%s %s\nBranch: %s\nTip: %s\nModel: claude-opus-5-5\n" \
               "Effort: high\n\nDone." % (
                   kind, label,
                   branch or rs.build_names("cloudrev/", label)["build"], tip)
        self.comments.append({"id": cid, "body": body,
                              "html_url": "https://github.com/owner/drop/"
                                          "issues/5#issuecomment-%d" % cid,
                              "user": {"login": "owner"}})

    def ledger_rows(self, **match):
        current, why = dispatches.snapshot()
        self.assertIsNone(why)
        return [r for r in current.values()
                if all(r.get(k) == v for k, v in match.items())]

    def handed_back(self):
        """A build row whose builder pushed: the tick fetches it back."""
        row = self.build_row()
        self.assertIn("delivered", self.acted(row))
        label = remote_relay.label_of(row)
        built = self.builder_pushes(label)
        action = self.acted(row)
        self.assertIn("handed back", action)
        review = [r for r in self.ledger_rows(kind="review")
                  if r.get("supersedes") == row["id"]]
        self.assertEqual(len(review), 1)
        return row, label, built, review[0]


class StandingTransportTest(StandingBase):

    def test_a_review_row_is_delivered_into_the_standing_session(self):
        row, _why = self.send(brief="Review the lane; watch the parser.")
        self.assertIn("delivered", self.acted(row))
        self.assertFalse([c for c in self.calls if c["argv"][0] == "script"],
                         "a standing seat must never run a CLI launch")
        self.assertEqual([s for s, _m, _h in self.to_sessions], [SID_A])
        _sid, task, home = self.to_sessions[0]
        self.assertEqual(home, self.claude_home)
        branch = "cloudrev/" + remote_relay.label_of(row)
        for want in ("watch the parser", "git fetch origin %s" % branch,
                     "model opus", "effort high", "say so",
                     "CLOUD REVIEW %s %s" % (remote_relay.label_of(row),
                                             self.tip[:12])):
            self.assertIn(want, task)
        self.assertEqual(run_git(self.drop_repo, "rev-parse", branch), self.tip)
        launch = self.journal("launch")[0]
        self.assertEqual((launch["standing_sid"], launch["account"],
                          launch["effort"]), (SID_A, EMAIL, "high"))
        # the report is read and recorded exactly as a launched session's
        self.report(row)
        self.assertIn("recorded APPROVE as hold", self.acted(row))
        self.assert_no_receipt_hold(row["id"])

    def test_a_seat_with_no_standing_session_is_refused_plainly(self):
        self.configure(standing=())
        row, _why = self.send()
        action = self.acted(row)
        self.assertIn("no standing session", action)
        self.assertEqual(self.row(row["id"])["status"], "held")
        self.assertIn("no standing session",
                      self.row(row["id"]).get("hold_reason") or action)
        self.assertEqual(self.to_sessions, [])
        self.assertFalse([c for c in self.calls if c["argv"][0] == "script"],
                         "no fallback to the CLI launch")
        self.assertEqual(self.drop_branches(), [])

    def test_the_cli_launch_is_an_explicit_opt_in_and_carries_the_effort(self):
        self.configure(standing=(), seat_transport="cli")
        row, _why = self.send()
        self.assertIn("launched", self.acted(row))
        script = [c for c in self.calls if c["argv"][0] == "script"][0]
        command = script["argv"][3]
        self.assertLess(command.index("--effort high"), command.index("--cloud"))
        self.assertEqual(self.to_sessions, [])

    def test_a_standing_session_not_bound_to_the_push_repo_is_refused(self):
        """CURE3 F2: a standing session pushes to whatever repository the web
        UI attached, and the relay's PRIVATE check never sees that push. So an
        account's `standing_repo` must name the project's push_repo, or its
        session is not used; with none bound the row is held naming both."""
        for bound, named in ((("someone/public", "someone/public"),
                              "someone/public"),
                             ((None, None), "names no standing_repo")):
            with self.subTest(bound=bound):
                self.configure(bound=bound)
                row, _why = self.send(lane="lane-%s" % named[:6].strip())
                action = self.acted(row)
                self.assertIn("held", action)
                held = self.row(row["id"])
                self.assertEqual(held["status"], "held")
                reason = held.get("hold_reason") or ""
                self.assertIn(named, reason)
                self.assertIn("owner/drop", reason)
                self.assertEqual(self.to_sessions, [])

    def test_one_bound_session_serves_while_the_other_is_not(self):
        self.configure(bound=("someone/public", "owner/drop"))
        row, _why = self.send()
        self.assertIn("delivered", self.acted(row))
        self.assertEqual([s for s, _t, _h in self.to_sessions], [SID_B])

    def test_every_standing_task_pushes_nothing_unless_origin_is_the_push_repo(self):
        """CURE3 F2, the session's side: the task tells it to read its origin
        first and push nothing unless it names the push repository."""
        row, _why = self.send()
        self.assertIn("delivered", self.acted(row))
        build = self.build_row()
        self.assertIn("delivered", self.acted(build))
        for _sid, task, _home in self.to_sessions:
            self.assertIn("git remote get-url origin", task)
            self.assertIn("unless it names owner/drop, push nothing", task)

    def test_an_archived_standing_session_is_recorded_and_never_used_again(self):
        self.answers[SID_A] = (1, '{"ok": false, "error": "Session is '
                                  'archived"}', "")
        row, _why = self.send()
        self.assertIn("archived", self.acted(row))
        gone = self.journal("standing-archived")
        self.assertEqual([(e["account"], e["sid"]) for e in gone],
                         [(EMAIL, SID_A)])
        self.assertIn("delivered", self.acted(row))
        self.assertEqual([s for s, _m, _h in self.to_sessions], [SID_A, SID_B])
        second, _why = self.send(lane="lane-two")
        self.acted(second)
        self.assertEqual(self.to_sessions[-1][0], SID_B)
        self.assertEqual([s for s, _m, _h in self.to_sessions].count(SID_A), 1)
        self.assertEqual(len(self.journal("standing-archived")), 1)
        self.assertEqual(remote_relay.status()["standing"],
                         {EMAIL: {"sid": SID_A, "archived": True},
                          EMAIL_B: {"sid": SID_B, "archived": False}})


class BuildLaneTest(StandingBase):

    def test_a_build_row_delivers_its_brief_with_the_standard_preamble(self):
        row = self.build_row()
        dry = remote_relay.tick(dry=True)
        self.assertIn("would deliver", dict(dry["actions"])[row["id"]])
        self.assertEqual(self.to_sessions, [])
        self.assertIn("delivered", self.acted(row))
        names = self.names(row)
        self.assertEqual(self.drop_branches(), [names["start"]])
        self.assertEqual(run_git(self.drop_repo, "rev-parse", names["start"]),
                         self.base)
        sid, task, _home = self.to_sessions[0]
        self.assertEqual(sid, SID_A)
        for want in ("Build the parser.", IDENTITY, "Co-Authored-By",
                     "git push origin HEAD:refs/heads/" + names["build"],
                     "BUILD " + remote_relay.label_of(row), "model opus",
                     "effort high", "gh issue comment 5 -R owner/drop"):
            self.assertIn(want, task)
        self.assertIn("waiting", self.acted(row))
        self.assertEqual(len(self.to_sessions), 1)

    def test_the_pushed_build_is_fetched_into_a_local_lane_and_handed_back(self):
        row, label, built, review = self.handed_back()
        lane_ref = "refs/heads/lane/" + label
        self.assertEqual(run_git(self.repo, "rev-parse", lane_ref), built)
        self.assertEqual(self.drop_branches(), [],
                         "the build's remote branches are deleted after fetch")
        held = self.row(row["id"])
        self.assertEqual(held["status"], "held")
        self.assertIn(built[:12], held.get("hold_reason") or "")
        self.assertEqual((review["tip"], review["kind"], review["lane"]),
                         (built, "review", row["lane"]))
        brief, _absent, _problem = dispatches.brief_of(review)
        for want in ("fresh subagent", "file:line", "APPROVE or FIX",
                     "neighbours"):
            self.assertIn(want, brief)
        back = self.journal("handback")[0]
        self.assertEqual((back["row"], back["tip"], back["ref"],
                          back["review_row"], back["builder"]),
                         (row["id"], built, lane_ref, review["id"], SID_A))
        # nothing was pushed anywhere but the private push repository
        self.assertEqual(run_git(self.repo, "rev-parse", self.main), self.base)

    def test_a_report_whose_branch_is_missing_is_unmeasured_never_done(self):
        row = self.build_row()
        self.acted(row)
        label = remote_relay.label_of(row)
        self.handback_comment(label, "c" * 40)
        action = self.acted(row)
        self.assertIn("UNMEASURED", action)
        self.assertEqual(self.row(row["id"])["status"], "open")
        self.assertEqual(self.journal("handback"), [])
        unmeasured = self.journal("build-unmeasured")
        self.assertEqual(len(unmeasured), 1)
        self.assertEqual(unmeasured[0]["tip"], "c" * 40)
        self.assertFalse([r for r in self.ledger_rows(kind="review")
                          if r.get("supersedes") == row["id"]])

    def test_the_review_goes_to_a_session_other_than_the_builders(self):
        _row, _label, built, review = self.handed_back()
        self.assertIn("delivered", self.acted(review))
        sid, task, home = self.to_sessions[-1]
        self.assertEqual((sid, home), (SID_B, self.claude_home_b))
        self.assertIn(built[:12], task)

    def test_with_only_the_builders_session_it_serves_the_review_fresh(self):
        """Owner canon: the builder's own standing session may serve the
        review, because the brief hands the read to one fresh-context
        subagent that holds none of the build's working context."""
        self.configure(standing=(SID_A,))
        _row, _label, _built, review = self.handed_back()
        self.assertIn("delivered", self.acted(review))
        self.assertEqual([s for s, _m, _h in self.to_sessions], [SID_A, SID_A])
        self.assertIn("holds none of the build's working context",
                      self.to_sessions[-1][1])

    def test_a_re_sent_review_still_prefers_a_session_other_than_the_builder(self):
        """A review row that supersedes the relay's review walks the chain
        back to the build, so it too prefers a session that did not build
        the lane, even when the builder's account is listed first."""
        row, _label, built, review = self.handed_back()
        again, why = self.send(lane=row["lane"], tip=built,
                               supersedes=review["id"])
        self.assertIsNotNone(again, why)
        self.assertIn("delivered", self.acted(again))
        self.assertEqual(self.to_sessions[-1][0], SID_B)

    def test_a_build_with_a_foreign_author_is_unmeasured_and_never_reviewed(self):
        row = self.build_row()
        self.assertIn("delivered", self.acted(row))
        label = remote_relay.label_of(row)
        built = self.builder_pushes(label, ident=("Someone Else",
                                                  "else@example.com"))
        action = self.acted(row)
        self.assertIn("UNMEASURED", action)
        self.assertIn("not authored", action)
        self.assertEqual(self.row(row["id"])["status"], "held")
        self.assertEqual(self.journal("handback"), [])
        self.assertFalse([r for r in self.ledger_rows(kind="review")
                          if r.get("supersedes") == row["id"]])
        bad = self.journal("build-unmeasured")
        self.assertEqual([(e["tip"], e["record"]) for e in bad],
                         [(built, "UNMEASURED")])

    def test_refused_standing_deliveries_hold_the_row(self):
        """A delivery refusal starts no session, so the launch cap never saw
        it: after MAX_STANDING_REFUSALS_PER_ROW the row is held."""
        for sid in (SID_A, SID_B):
            self.answers[sid] = (1, '{"ok": false, "error": "boom"}', "")
        row, _why = self.send()
        for _n in range(remote_relay.MAX_STANDING_REFUSALS_PER_ROW):
            self.assertIn("failed", self.acted(row))
        self.assertIn("held", self.acted(row) or "held")
        held = self.row(row["id"])
        self.assertEqual(held["status"], "held")
        self.assertIn("refused", held.get("hold_reason") or "")
        tries = len(self.to_sessions)
        self.acted(row)
        self.assertEqual(len(self.to_sessions), tries, "a held row kept trying")

    def test_an_archived_builder_that_pushed_is_handed_back_not_relaunched(self):
        row = self.build_row()
        self.assertIn("delivered", self.acted(row))
        label = remote_relay.label_of(row)
        built = self.builder_pushes(label)
        launch = self.journal("launch")[0]
        rs.append({"event": "archived", "row": row["id"], "sid": launch["sid"],
                   "error": "session is archived"})
        action = self.acted(row)
        self.assertIn("handed back", action)
        self.assertEqual(run_git(self.repo, "rev-parse",
                                 "refs/heads/lane/" + label), built)
        self.assertEqual(len(self.journal("launch")), 1,
                         "an archived builder that pushed was relaunched")

    def test_a_superseding_review_on_the_builders_session_carries_the_fresh_read(self):
        """CURE2 F1a: the fresh-context clause rides EVERY standing review
        task, not only the relay-minted brief. With the other session
        archived, the review lands on the builder's own session; a review
        that supersedes it is a re-read there and still says who must read
        it, and a new review of the same tip does too. Each launch says it
        is the builder's session (F1b)."""
        row, _label, built, review = self.handed_back()
        rs.append({"event": "standing-archived", "account": EMAIL_B,
                   "sid": SID_B})
        self.assertIn("delivered", self.acted(review))
        again, why = self.send(lane=row["lane"], tip=built,
                               brief="Review it again, hard on the parser.",
                               supersedes=review["id"])
        self.assertIsNotNone(again, why)
        self.assertIn("re-read", self.acted(again))
        sid, task, _home = self.to_sessions[-1]
        self.assertEqual(sid, SID_A)
        self.assertIn("RE-READ", task)
        self.assertIn("holds none of the build's working context", task)
        other, why = self.send(lane=row["lane"] + "-again", tip=built,
                               brief="Review it again, hard on the parser.")
        self.assertIsNotNone(other, why)
        self.assertIn("delivered", self.acted(other))
        sid, task, _home = self.to_sessions[-1]
        self.assertEqual(sid, SID_A)
        self.assertIn("holds none of the build's working context", task)
        self.assertIn("Review it again, hard on the parser.", task)
        launch = {e["row"]: e.get("builder_session")
                  for e in self.journal("launch")}
        self.assertEqual((launch[review["id"]], launch[other["id"]]),
                         (True, True))

    def test_a_review_on_another_session_is_not_marked_the_builders(self):
        _row, _label, _built, review = self.handed_back()
        self.assertIn("delivered", self.acted(review))
        launch = [e for e in self.journal("launch") if e["row"] == review["id"]]
        self.assertEqual([e.get("builder_session") for e in launch], [False])

    def test_the_builders_session_read_is_named_in_the_verdicts_evidence(self):
        """CURE2 F1b: a verdict read on the builder's own session says so on
        the ledger, so the reader of the verdict can weigh it."""
        self.configure(standing=(SID_A,))
        _row, _label, _built, review = self.handed_back()
        self.assertIn("delivered", self.acted(review))
        self.report(review, verdict="FIX", account=EMAIL, findings=(
            "[BLOCKING] [MEASURED] parser drops a token / parser.py:1 / "
            "a -> b / ran it / keep it",))
        self.assertIn("fix-verdict", self.acted(review))
        after = self.row(review["id"])
        said = json.dumps(after)
        self.assertIn("builder's own session", said)

    def test_a_build_that_fails_the_door_gets_one_correction(self):
        """CURE2 F2: a verify failure tells the builder why, once."""
        row = self.build_row()
        self.assertIn("delivered", self.acted(row))
        label = remote_relay.label_of(row)
        self.builder_pushes(label, ident=("Someone Else", "else@example.com"))
        before = len(self.to_sessions)
        self.assertIn("UNMEASURED", self.acted(row))
        sent = self.to_sessions[before:]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], SID_A)
        self.assertIn("CORRECTION label=" + label, sent[0][1])
        self.assertIn("not authored", sent[0][1])
        self.acted(row)
        self.assertEqual(len(self.to_sessions), before + 1,
                         "the correction was sent twice")

    def test_refused_checkouts_count_toward_the_refusal_cap(self):
        """CURE2 F3: a checkout refusal starts no session either, so it
        counts toward the same cap; the daily credit refusal does not."""
        row, _why = self.send()
        with mock.patch.object(rs, "build_branch_checkout",
                               return_value=(None, "checkout boom")):
            for _n in range(remote_relay.MAX_STANDING_REFUSALS_PER_ROW):
                self.assertIn("checkout boom", self.acted(row))
            self.acted(row)
        held = self.row(row["id"])
        self.assertEqual(held["status"], "held")
        self.assertIn("checkout boom", held.get("hold_reason") or "")
        self.assertEqual(self.to_sessions, [])

    def test_a_build_whose_message_names_a_model_is_unmeasured(self):
        row = self.build_row()
        self.assertIn("delivered", self.acted(row))
        label = remote_relay.label_of(row)
        self.builder_pushes(label, message="the build\n\nModel: claude-opus-5-5")
        action = self.acted(row)
        self.assertIn("UNMEASURED", action)
        self.assertIn("AI authoring line", action)
        self.assertEqual(self.journal("handback"), [])

    def test_an_unparseable_review_report_is_unmeasured_never_a_verdict(self):
        _row, _label, _built, review = self.handed_back()
        self.acted(review)
        self.report(review, verdict="FIX", findings=(),
                    account=EMAIL_B)                 # a FIX with none
        action = self.acted(review)
        self.assertIn("UNMEASURED", action)
        after = self.row(review["id"])
        self.assertEqual(after["status"], "open")
        self.assertIsNone(after.get("polarity"))
        bad = self.journal("report-malformed")
        self.assertEqual([e.get("record") for e in bad], ["UNMEASURED"])
        self.assertEqual(self.journal("recorded"), [])

    def test_a_fix_round_trips_to_the_builder_and_back_to_review(self):
        row, label, built, review = self.handed_back()
        self.acted(review)
        self.report(review, verdict="FIX", account=EMAIL_B, findings=(
            "[BLOCKING] [MEASURED] parser drops a token / parser.py:1 / "
            "a -> b / ran it / keep it",))
        self.assertIn("fix-verdict", self.acted(review))
        self.assertEqual(self.row(review["id"])["polarity"], "fix")
        cures = [r for r in self.ledger_rows(kind="build", recipient=SEAT)
                 if r.get("supersedes") == review["id"]]
        self.assertEqual(len(cures), 1)
        cure = cures[0]
        self.assertEqual(cure["tip"], built)
        self.assertIn("delivered", self.acted(cure))
        sid, task, _home = self.to_sessions[-1]
        self.assertEqual(sid, SID_A, "the cure goes to the builder's session")
        names = rs.build_names("cloudrev/", label)
        self.assertIn("CURE " + label, task)
        self.assertIn("git push origin HEAD:refs/heads/" + names["build"], task)
        self.assertEqual(run_git(self.drop_repo, "rev-parse", names["start"]),
                         built)
        cured = self.builder_pushes(label, text="cured\n", message="the cure")
        self.assertIn("handed back", self.acted(cure))
        self.assertEqual(run_git(self.repo, "rev-parse",
                                 "refs/heads/lane/" + label), cured)
        again = [r for r in self.ledger_rows(kind="review", recipient=SEAT)
                 if r.get("supersedes") == cure["id"]]
        self.assertEqual(len(again), 1)
        self.acted(again[0])
        self.assertEqual(self.to_sessions[-1][0], SID_B)

    def test_an_approve_under_the_builders_own_seat_is_not_source_clean(self):
        """The ledger records the build seat as a chain author, so a review
        under the same seat name is the author's own read: its APPROVE is an
        ordinary hold that names the refusal, never a source-clean one."""
        _row, _label, _built, review = self.handed_back()
        self.assertEqual(review["recipient"], SEAT)
        self.acted(review)
        self.report(review, account=EMAIL_B)
        self.assertIn("recorded APPROVE as hold", self.acted(review))
        held = self.row(review["id"])
        self.assertEqual(held["status"], "held")
        self.assertIn("LANE AUTHOR", held.get("hold_reason") or "")
        self.assertIsNone(held.get("source_clean_tip"))

    def test_an_approve_holds_and_leaves_the_lane_alone(self):
        self.configure(review_seat="cloud-opus-review")
        _row, label, built, review = self.handed_back()
        self.assertEqual(review["recipient"], "cloud-opus-review")
        self.assertIn("delivered", self.acted(review))
        self.assertEqual(self.to_sessions[-1][0], SID_B)
        self.report(review, account=EMAIL_B)
        self.assertIn("recorded APPROVE as hold", self.acted(review))
        self.assert_no_receipt_hold(review["id"])
        self.assertFalse([r for r in self.ledger_rows(kind="build")
                          if r.get("supersedes") == review["id"]])
        self.assertEqual(run_git(self.repo, "rev-parse", self.main), self.base)
        self.assertEqual(run_git(self.repo, "rev-parse",
                                 "refs/heads/lane/" + label), built)


class AiLinesTest(unittest.TestCase):
    """CURE2 P3: the build door refuses what BUILD_PROTOCOL forbids, a model
    id and an AI link as well as an authoring trailer."""

    def setUp(self):
        pin_live_seats(self)

    def test_a_model_id_or_an_ai_link_is_an_ai_line(self):
        for line in ("Model: claude-opus-5-5", "model: opus",
                     "https://claude.ai/code/session_01AbcDef",
                     "claude.ai/code/session_01AbcDef",
                     "Built on claude-sonnet-5-5 at high effort.",
                     "Reviewed with gpt-6-sol.",
                     "see https://chatgpt.com/c/123"):
            self.assertTrue(rs.ai_lines("the build\n\n" + line), line)

    def test_plain_prose_is_not(self):
        for line in ("Fix the parser (task/3517).", "the gpt-oss path stays",
                     "Model the ledger as events.", "claude-home-b is a dir"):
            self.assertEqual(rs.ai_lines(line), [], line)


class PacingTest(StandingBase):

    def test_a_scarce_pool_lifts_the_auto_spread_to_the_ceiling(self):
        env = {"HELM_REMOTE_DAILY_BUDGET_USD": "auto",
               "HELM_REMOTE_SCARCE_CEILING_USD": "0"}
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(burnflags, "family_flag",
                                   return_value={"colour": "ORANGE"}):
                row, _why = self.send()
                self.assertIn("deferred", self.acted(row))
                self.assertIn("ORANGE", self.acted(row))
        self.assertEqual(self.to_sessions, [])


if __name__ == "__main__":
    unittest.main()
