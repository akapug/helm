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
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from helm import dispatches, eventledger, landreq

ENV_KEYS = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NODE_URL",
            "HELM_CHAT_NAME", "HELM_CELL_BIN", "HELM_CELL_PROFILE",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")


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

        dispatches.mark_verdict(row["id"], self.side, "review-post-9")
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
        dispatches.mark_verdict(row["id"], self.side, "ok")
        self.commit("more-trunk-work")             # trunk moves, side does not
        self.assertEqual(landreq.get(row["id"])[0]["state"], "READY")

    def test_merged_local_is_not_landed_until_the_push_is_observed(self):
        self.add_origin()                          # origin/main == b
        row = self.dispatch(ref=self.side)
        dispatches.mark_verdict(row["id"], self.side, "ok")
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
            repo=self.repo, key="k1", sign=False)
        self.assertIsNone(why)
        self.assertTrue(sent)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["author"], "integrator")   # HELM_CHAT_NAME seat
        self.assertEqual(lr["reviewer"], "codex-3")
        self.assertEqual(lr["state"], "AWAITING_REVIEW")   # delivery observed


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

    def test_ready_stalls_past_the_land_threshold(self):
        row = self.dispatch()
        dispatches.mark_verdict(row["id"], self.side, "ok")
        self.age(row["id"], landreq.LAND_STALL_S["READY"] + 60)
        stalled = landreq.stalls()[0]
        self.assertEqual([lr["state"] for lr in stalled], ["READY"])

    def test_landed_and_young_loops_never_show_as_stalled(self):
        landed = self.dispatch(ref=self.side)
        dispatches.mark_verdict(landed["id"], self.side, "ok")
        self.git("merge", "--no-edit", "-q", "side")
        self.age(landed["id"], 10 ** 6)            # ancient but terminal
        young = self.dispatch(ref=self.b, lane="lane/young")
        dispatches.mark_verdict(young["id"], self.b, "ok")   # b IS on trunk
        self.assertEqual(landreq.stalls()[0], [])
        # the ancient landed loop is terminal, and b landed immediately.
        self.assertEqual(landreq.get(landed["id"])[0]["state"], "LANDED")
        self.assertEqual(landreq.get(young["id"])[0]["state"], "LANDED")

    def test_verdict_without_repo_binding_is_unobservable_never_a_false_stall(self):
        # The live-ledger bug this guards: an old verdict'd row with no repo_id
        # cannot have its landing observed, so it must NOT be asserted READY +
        # STALLED (it may well have landed a day ago). Unobservable, not a gap.
        old = "2026-07-01T00:00:00Z"
        legacy = {"id": "4f65d90d", "ts": old, "recipient": "codex-orch",
                  "lane": "work-adopt", "ref": self.a, "tip": self.a,
                  "note": None, "deadline_s": 60, "source": "old",
                  "status": "verdict", "verdict_ref": "CLEAR; landed as merge x",
                  "reviewed_tip": self.a, "last_updated": old}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), legacy))
        lr = landreq.get("4f65d90d")[0]
        self.assertEqual(lr["state"], "READY")
        self.assertFalse(lr["observable"])
        self.assertFalse(lr["stalled"])           # aged far past, still no gap
        self.assertEqual(landreq.stalls()[0], [])
        self.assertIn("landing unobservable", run(["list", "--all"])[1])
        self.assertIn("UNOBSERVABLE", run(["show", "4f65d90d"])[1])

    def test_stalls_are_ordered_longest_first(self):
        older = self.dispatch(lane="lane/older")
        dispatches.mark_verdict(older["id"], self.side, "ok")
        self.age(older["id"], 7200)
        newer = self.dispatch(ref=self.side, lane="lane/newer")
        dispatches.mark_verdict(newer["id"], self.side, "ok")
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
        dispatches.mark_verdict(row["id"], self.side, "ok")
        self.git("merge", "--no-edit", "-q", "side")
        default, _ = landreq.loops()
        self.assertEqual(default, [])              # terminal, not in flight
        every, _ = landreq.loops(include_landed=True)
        self.assertEqual([lr["id"] for lr in every], [row["id"]])


class BoardSectionTest(LandReqBase):
    def test_board_section_exposes_loops_and_the_stalled_subset(self):
        stuck = self.dispatch(lane="lane/stuck")
        dispatches.mark_verdict(stuck["id"], self.side, "ok")
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


class CmdTest(LandReqBase):
    def test_cli_list_show_stalls_round_trip(self):
        row = self.dispatch(lane="lane/round")
        dispatches.mark_verdict(row["id"], self.side, "ok")
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

    def test_bad_usage_is_rc2_without_traceback(self):
        for args in ([], ["bogus"], ["list", "--wat"], ["stalls", "x"],
                     ["show"]):
            rc, out, err = run(args)
            self.assertEqual(rc, 2, args)
            self.assertNotIn("Traceback", out + err)

    def test_cli_verb_is_wired(self):
        from helm import cli
        self.assertIn("lr", cli.VERBS)


if __name__ == "__main__":
    unittest.main()
