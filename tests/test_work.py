#!/usr/bin/env python3
"""helm work — worktree lifecycle on the claims lane. Hermetic: HELM_HOME +
HELM_CHAT_DIR are tmp dirs, git global/system config nulled, every room is a
scratch repo minted in setUp — the real repo and its worktrees are never
touched (every `helm work` call pins --repo at the scratch root)."""
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

from helm import seats, work  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_OWNER_NAMES", "HELM_CELL_BIN", "HELM_ADOPTED_DIR",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "CLAUDECODE", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL",
            "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "HELM_WORK_INTEGRATOR")


def _sh(cwd, *args):
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True,
                          timeout=30)


class WorkBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-work-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_LOG"] = "0"
        os.environ["HELM_CHAT_OWNER_NAMES"] = "david"
        os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"
        os.environ["GIT_CONFIG_SYSTEM"] = "/dev/null"
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@t"),
                    ("git", "config", "user.name", "t")):
            self.assertEqual(_sh(self.root, *cmd).returncode, 0)
        with open(os.path.join(self.root, "README"), "w") as f:
            f.write("seed\n")
        _sh(self.root, "git", "add", "-A")
        r = _sh(self.root, "git", "commit", "-q", "-m", "seed")
        self.assertEqual(r.returncode, 0, r.stderr)

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def work(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = work.cmd_work(list(args) + ["--repo", self.root])
        return rc, out.getvalue(), err.getvalue()

    def room(self, lane, dirty=None):
        """Mint a lease-less room directly (git only, never the desk)."""
        path = work.lane_path(self.root, lane)
        r = _sh(self.root, "git", "worktree", "add", "-q", "-b",
                work.lane_branch(lane), path, "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        if dirty:
            with open(os.path.join(path, dirty), "w") as f:
                f.write("precious uncommitted bytes\n")
        return path


class ClaimTest(WorkBase):
    def test_claim_mints_room_lock_and_lease(self):
        rc, out, err = self.work("claim", "demo", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        path, branch, lease, ttl = out.strip().split("\t")
        self.assertEqual(path, work.lane_path(self.root, "demo"))
        self.assertEqual(branch, "lane/demo")
        self.assertEqual(int(ttl), work.DEFAULT_TTL)
        self.assertTrue(os.path.isdir(path))
        row = {w["path"]: w for w in work.worktrees(self.root)}[path]
        self.assertTrue(row["locked"])                    # do-not-disturb tag
        self.assertEqual(row["reason"], "lease:" + lease[:8])
        self.assertIn("worktree:proj:demo",
                      [c["resource"] for c in seats.claims_list()])
        # idempotent re-entry: the holder re-claims with the lease, same room
        rc2, out2, err2 = self.work("claim", "demo", "--seat", "s1",
                                    "--lease", lease)
        self.assertEqual(rc2, 0, err2)
        self.assertEqual(out2.strip().split("\t")[:3], [path, branch, lease])
        # a second seat without the capability is refused, holder named
        rc3, _out3, err3 = self.work("claim", "demo", "--seat", "s2")
        self.assertEqual(rc3, 1)
        self.assertIn("held by s1", err3)

    def test_claim_bad_lane_name(self):
        rc, _out, err = self.work("claim", "no/slashes")
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)


class ReleaseTest(WorkBase):
    def test_release_clean_removes_room_and_merged_branch(self):
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path, _branch, lease, _ttl = out.strip().split("\t")
        rc, out, err = self.work("release", "demo", "--seat", "s1",
                                 "--lease", lease)
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(seats.claims_list(), [])         # key surrendered
        self.assertFalse(work._has_branch(self.root, "lane/demo"))  # merged: -d

    def test_release_dirty_refuses_then_park_commits(self):
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path, _b, lease, _t = out.strip().split("\t")
        junk = os.path.join(path, "junk.txt")
        with open(junk, "w") as f:
            f.write("do not lose me\n")
        rc, _out, err = self.work("release", "demo", "--seat", "s1",
                                  "--lease", lease)
        self.assertEqual(rc, 1)                     # inspect before the key
        self.assertIn("--park", err)                # the two exits are named
        self.assertTrue(os.path.exists(junk))       # nothing touched
        self.assertEqual(len(seats.claims_list()), 1)   # key still in pocket
        rc, out, err = self.work("release", "demo", "--seat", "s1",
                                 "--lease", lease, "--park")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(path))
        show = _sh(self.root, "git", "show", "lane/demo:junk.txt")
        self.assertEqual(show.stdout, "do not lose me\n")   # lost-and-found
        self.assertIn("awaits integration", out)  # unmerged branch stays, told

    def test_release_occupied_room_keeps_room_and_lease(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path, _branch, lease, _ttl = out.strip().split("\t")
        proc = subprocess.Popen(["sleep", "30"], cwd=path)
        try:
            rc, _out, err = self.work("release", "demo", "--seat", "s1",
                                      "--lease", lease)
            self.assertEqual(rc, 1)
            self.assertIn("OCCUPIED", err)
            self.assertTrue(os.path.isdir(path))
            self.assertEqual(len(seats.claims_list()), 1)
            self.assertNotIn("(deleted)", os.readlink("/proc/%d/cwd" % proc.pid))
        finally:
            proc.terminate()
            proc.wait()

    def test_release_wrong_lease_removes_nothing(self):
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path = out.split("\t")[0]
        rc, _out, err = self.work("release", "demo", "--seat", "s1",
                                  "--lease", "beefbeefbeefbeef")
        self.assertEqual(rc, 1)                 # key surrender precedes removal
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(len(seats.claims_list()), 1)


class GcTest(WorkBase):
    def setUp(self):
        super().setUp()
        rc, out, _e = self.work("claim", "held", "--seat", "s1")
        self.assertEqual(rc, 0)
        self.held = out.split("\t")[0]                    # live lease
        self.locked = self.room("noturn")                 # out-of-band lock
        _sh(self.root, "git", "worktree", "lock", self.locked,
            "--reason", "owner says keep")
        self.clean = self.room("cleanmg")                 # clean + merged,
        _sh(self.root, "git", "worktree", "lock", self.clean,
            "--reason", "lease:deadbeef")                 # stale key tag
        self.dirty = self.room("messy", dirty="junk.txt")  # dirty, lease-less

    def test_dry_run_verdicts_touch_nothing(self):
        rc, out, _err = self.work("gc")
        self.assertEqual(rc, 0)
        for path in (self.held, self.locked, self.clean, self.dirty):
            self.assertTrue(os.path.isdir(path))
        self.assertRegex(out, r"KEEP\s+held\s+lease live")
        self.assertRegex(out, r"KEEP\s+noturn\s+locked out-of-band")
        self.assertRegex(out, r"REMOVE\s+cleanmg")
        self.assertRegex(out, r"RESCUE\s+messy")
        self.assertIn("dry-run", out)
        self.assertIn("never discarded", out)

    def test_apply_sweeps_and_rescues_never_discards(self):
        rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(self.held))      # guest in the room
        self.assertTrue(os.path.isdir(self.locked))    # do-not-disturb
        self.assertFalse(os.path.exists(self.clean))   # clean+merged swept
        self.assertFalse(work._has_branch(self.root, "lane/cleanmg"))
        self.assertFalse(os.path.exists(self.dirty))   # room swept, BYTES SAVED
        show = _sh(self.root, "git", "show", "lane/messy:junk.txt")
        self.assertEqual(show.stdout, "precious uncommitted bytes\n")
        self.assertTrue(work._has_branch(self.root, "lane/messy"))
        self.assertIn("rescued", out)

    def test_apply_never_removes_an_occupied_room(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        proc = subprocess.Popen(["sleep", "30"], cwd=self.clean)
        try:
            rc, out, err = self.work("gc", "--apply")
            self.assertEqual(rc, 0, err)
            self.assertIn("OCCUPIED", out)
            self.assertTrue(os.path.isdir(self.clean))
            self.assertNotIn("(deleted)", os.readlink("/proc/%d/cwd" % proc.pid))
        finally:
            proc.terminate()
            proc.wait()

    def test_enact_rechecks_occupancy_after_scan(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        row = next(r for r in work.gc_scan(self.root) if r["path"] == self.clean)
        self.assertEqual(row["verdict"], "remove")
        proc = subprocess.Popen(["sleep", "30"], cwd=self.clean)
        try:
            lines = work.gc_enact(self.root, row)
            self.assertTrue(any("OCCUPIED" in line for line in lines))
            self.assertTrue(os.path.isdir(self.clean))
        finally:
            proc.terminate()
            proc.wait()

    def test_gc_orphans_feeds_the_helm_gc_report_row(self):
        got = work.gc_orphans(self.root)
        self.assertEqual(sorted(got), sorted([self.clean, self.dirty]))


class GcUnmergedTest(WorkBase):
    def test_unmerged_clean_room_removed_branch_stays(self):
        path = self.room("aheadln")
        with open(os.path.join(path, "f.txt"), "w") as f:
            f.write("x\n")
        _sh(path, "git", "add", "-A")
        r = _sh(path, "git", "commit", "-q", "-m", "lane work")
        self.assertEqual(r.returncode, 0, r.stderr)
        rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(path))
        self.assertTrue(work._has_branch(self.root, "lane/aheadln"))
        self.assertIn("stays for the integrator", out)


class ListTest(WorkBase):
    def test_list_joins_registry_with_claims(self):
        rc, _out, err = self.work("claim", "webui", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.room("stray", dirty="j.txt")
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        row_w = next(ln for ln in out.splitlines() if "webui" in ln)
        self.assertRegex(row_w, r"webui\s+s1 \d+s\s+clean")
        row_s = next(ln for ln in out.splitlines() if "stray" in ln)
        self.assertRegex(row_s, r"stray\s+-\s+dirty")

    def test_list_empty(self):
        rc, out, _err = self.work("list")
        self.assertEqual(rc, 0)
        self.assertIn("no lane rooms", out)


class UnguardedRoomTest(WorkBase):
    """Only Claude Agent/Workflow isolation rooms are reported, with every
    uncertain liveness input retaining an explicit UNKNOWN state."""

    def foreign(self, name, dirty=None):
        path = os.path.join(self.root, ".claude", "worktrees", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        r = _sh(self.root, "git", "worktree", "add", "-q", "-b",
                "worktree-" + name, path, "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        if dirty:
            with open(os.path.join(path, dirty), "w") as f:
                f.write("a reviewer's uncommitted security fixes\n")
        return path

    @contextlib.contextmanager
    def workflow_evidence(self, path, status="completed", live=True):
        home = os.path.join(self.tmp, "claude-home")
        sid = "11111111-2222-4333-8444-555555555555"
        slug = self.root.replace(os.sep, "-").replace(".", "-")
        d = os.path.join(home, "projects", slug, sid, "workflows")
        os.makedirs(d, exist_ok=True)
        run = os.path.basename(path).rsplit("-", 1)[0]
        with open(os.path.join(d, run + ".json"), "w") as f:
            json.dump({"runId": run, "status": status}, f)
        sessions = {sid} if live else set()
        with mock.patch.object(work, "_claude_homes", return_value=[home]), \
                mock.patch.object(work, "_live_claude_sessions",
                                  return_value=sessions):
            yield

    @contextlib.contextmanager
    def direct_agent_evidence(self, path, terminal=False, live=True):
        home = os.path.join(self.tmp, "direct-home")
        sid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        slug = self.root.replace(os.sep, "-").replace(".", "-")
        d = os.path.join(home, "projects", slug, sid, "subagents")
        os.makedirs(d, exist_ok=True)
        room = os.path.basename(path)
        with open(os.path.join(d, room + ".meta.json"), "w") as f:
            json.dump({"worktreePath": path}, f)
        msg = {"type": "assistant", "message": {
            "stop_reason": "end_turn" if terminal else "tool_use",
            "content": [{"type": "text", "text": "done"}] if terminal
            else [{"type": "tool_use", "name": "Bash"}]}}
        with open(os.path.join(d, room + ".jsonl"), "w") as f:
            f.write(json.dumps(msg) + "\n")
        sessions = {sid} if live else set()
        with mock.patch.object(work, "_claude_homes", return_value=[home]), \
                mock.patch.object(work, "_live_claude_sessions",
                                  return_value=sessions):
            yield

    def test_agent_worktree_is_reported_with_stable_identity(self):
        path = self.foreign("wf_dead00-1")
        with self.workflow_evidence(path):
            rows = work.unguarded_rows(self.root)
            self.assertEqual([r["id"] for r in rows], ["wf_dead00-1"])
            rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("UNGUARDED CLEAN", out)
        self.assertIn("id=wf_dead00-1", out)
        self.assertIn("never authorizes cleanup", out)

    def test_main_lanes_and_arbitrary_worktrees_are_not_unguarded(self):
        rc, _out, err = self.work("claim", "mine", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.room("stray")
        normal = os.path.join(self.tmp, "ordinary-review")
        r = _sh(self.root, "git", "worktree", "add", "-q", "-b",
                "ordinary-review", normal, "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(work.unguarded_rows(self.root), [])

    def test_recent_write_is_advisory_not_ownership_claim(self):
        path = self.foreign("wf_busy00-2", dirty="cred.py")
        self.assertEqual(work._occupants(path), [])
        with self.workflow_evidence(path):
            row = work.unguarded_rows(self.root)[0]
            rc, out, err = self.work("list")
        self.assertTrue(row["dirty"])
        self.assertLessEqual(row["wrote_ago"], work.RECENT_WRITE_SECONDS)
        self.assertEqual(rc, 0, err)
        self.assertIn("RECENT-WRITE", out)
        self.assertIn("advisory, not ownership proof", out)
        self.assertNotIn("assume live", out)

    def test_clean_but_live_workflow_is_live_with_parent_cwd_elsewhere(self):
        path = self.foreign("wf_live000-3")
        self.assertEqual(work._occupants(path), [])
        with self.workflow_evidence(path, status="running", live=True):
            row = work.unguarded_rows(self.root)[0]
            rc, out, _err = self.work("list")
        self.assertFalse(row["dirty"])
        self.assertEqual(row["harness"], "live")
        self.assertIn("LIVE-HARNESS", out)
        self.assertIn("Workflow active", out)

    def test_running_metadata_without_live_parent_is_unknown(self):
        path = self.foreign("wf_crashed0-4")
        with self.workflow_evidence(path, status="running", live=False):
            rc, out, _err = self.work("list")
        self.assertIn("UNGUARDED UNKNOWN", out)
        self.assertIn("harness metadata incomplete", out)

    def test_clean_direct_agent_is_live_until_its_final_text_end_turn(self):
        path = self.foreign("agent-deadbeef1234")
        with self.direct_agent_evidence(path, terminal=False, live=True):
            self.assertEqual(work.unguarded_rows(self.root)[0]["harness"], "live")
        with self.direct_agent_evidence(path, terminal=True, live=True):
            self.assertEqual(work.unguarded_rows(self.root)[0]["harness"],
                             "inactive")

    def test_terminal_clean_room_is_clean_but_not_declared_unowned(self):
        path = self.foreign("wf_idle000-5")
        with self.workflow_evidence(path):
            row = work.unguarded_rows(self.root)[0]
            rc, out, _err = self.work("list")
        self.assertIsNone(row["wrote_ago"])
        self.assertEqual(row["harness"], "inactive")
        self.assertIn("UNGUARDED CLEAN", out)
        self.assertIn("not proof that no writer will resume", out)

    def test_stale_dirty_write_remains_dirty_without_live_claim(self):
        path = self.foreign("wf_stale00-6", dirty="half.py")
        old = time.time() - 7200
        os.utime(os.path.join(path, "half.py"), (old, old))
        with self.workflow_evidence(path):
            row = work.unguarded_rows(self.root)[0]
            rc, out, _err = self.work("list")
        self.assertGreaterEqual(row["wrote_ago"], 7000)
        self.assertIn("UNGUARDED DIRTY", out)
        self.assertNotIn("LIVE-HARNESS", out)

    def test_missing_harness_metadata_fails_unknown(self):
        self.foreign("wf_orphan00-7")
        with mock.patch.object(work, "_claude_homes", return_value=[]), \
                mock.patch.object(work, "_live_claude_sessions", return_value=set()):
            rc, out, _err = self.work("list")
        self.assertIn("UNGUARDED UNKNOWN", out)
        self.assertIn("no matching harness metadata", out)

    def test_symlink_escape_is_reported_unknown_without_git_status(self):
        target = os.path.join(self.tmp, "outside")
        os.makedirs(target)
        path = os.path.join(self.root, ".claude", "worktrees", "wf_escape00-8")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(target, path)
        records = [{"path": path, "branch": "refs/heads/worktree-wf_escape00-8",
                    "locked": False, "reason": ""}]
        with mock.patch.object(work, "_room_status") as status_call:
            rows, errors = work.unguarded_inventory(self.root, registered=records)
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["harness"], "unknown")
        self.assertIn("symlink", rows[0]["hard_unknown"])
        status_call.assert_not_called()

    def test_foreign_repo_replacement_is_not_inspected_as_same_repo(self):
        path = os.path.join(self.root, ".claude", "worktrees", "wf_foreign0-9")
        os.makedirs(path)
        self.assertEqual(_sh(path, "git", "init", "-q").returncode, 0)
        records = [{"path": path, "branch": "refs/heads/worktree-wf_foreign0-9",
                    "locked": False, "reason": ""}]
        with mock.patch.object(work, "_room_status") as status_call:
            rows, _errors = work.unguarded_inventory(self.root, registered=records)
        self.assertIn("not a regular worktree link", rows[0]["hard_unknown"])
        status_call.assert_not_called()

    def test_symlinked_container_escape_is_unknown(self):
        outside = os.path.join(self.tmp, "escaped-claude")
        os.makedirs(os.path.join(outside, "worktrees", "wf_parent00-1"))
        os.symlink(outside, os.path.join(self.root, ".claude"))
        path = os.path.join(self.root, ".claude", "worktrees", "wf_parent00-1")
        records = [{"path": path, "branch": "refs/heads/worktree-wf_parent00-1",
                    "locked": False, "reason": ""}]
        with mock.patch.object(work, "_room_status") as status_call:
            rows, _errors = work.unguarded_inventory(self.root, registered=records)
        self.assertIn("container escapes", rows[0]["hard_unknown"])
        status_call.assert_not_called()


class PorcelainSafetyTest(WorkBase):
    def test_worktree_registry_is_nul_and_byte_safe(self):
        weird = os.fsencode(self.root) + b"/.claude/worktrees/agent-deadbeef\\\n\xff"
        raw = (b"worktree " + weird + b"\0HEAD abc\0branch refs/heads/x\0"
               b"locked because\0\0")
        with mock.patch.object(work, "_git_bytes", return_value=(0, raw, b"")):
            rows, error = work._worktree_records(self.root)
        self.assertIsNone(error)
        self.assertEqual(os.fsencode(rows[0]["path"]), weird)
        self.assertEqual(rows[0]["branch"], "refs/heads/x")
        self.assertEqual(rows[0]["reason"], "because")

    def test_worktree_registry_failure_and_truncation_are_not_empty_success(self):
        with mock.patch.object(work, "_git_bytes", return_value=(1, b"", b"boom")):
            self.assertEqual(work._worktree_records(self.root), ([], "boom"))
        with mock.patch.object(work, "_git_bytes",
                               return_value=(0, b"worktree /tmp/no-nul", b"")):
            rows, error = work._worktree_records(self.root)
        self.assertEqual(rows, [])
        self.assertIn("truncated", error)

    def test_porcelain_v2_parser_consumes_two_path_records_exactly(self):
        ordinary = b"1 .M N... 100644 100644 100644 a b line\\name\n\xff"
        rename = b"2 R. N... 100644 100644 100644 a b R100 new name"
        copy = b"2 C. N... 100644 100644 100644 a b C075 copy name"
        unmerged = b"u UU N... 100644 100644 100644 100644 a b c conflict"
        raw = (ordinary + b"\0" + rename + b"\0old name\0" + copy +
               b"\0source name\0" + unmerged + b"\0? untracked dir/file\0")
        got = work._status_entries(raw)
        self.assertEqual([p for p, _sub in got], [
            b"line\\name\n\xff", b"new name", b"copy name", b"conflict",
            b"untracked dir/file"])

    def test_human_porcelain_and_truncated_records_fail_closed(self):
        with self.assertRaises(ValueError):
            work._status_entries(b" M human path\0")
        with self.assertRaises(ValueError):
            work._status_entries(b"? no terminator")
        with self.assertRaises(ValueError):
            work._status_entries(
                b"2 R. N... 100644 100644 100644 a b R100 new\0\0")

    def test_weird_untracked_names_and_untracked_directory_are_timed(self):
        path = self.room("weirdst")
        rel = b"dir/space newline\nback\\slash-\xff"
        os.makedirs(os.path.join(path, "dir"))
        fd = os.open(os.path.join(os.fsencode(path), rel),
                     os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        row = work._room_status(path)
        self.assertTrue(row["dirty"])
        self.assertIsNone(row["unknown"])
        self.assertIsNotNone(row["wrote_ago"])

    def test_deletion_and_dirty_submodule_are_timestamp_unknown(self):
        path = self.room("deleted")
        os.remove(os.path.join(path, "README"))
        row = work._room_status(path)
        self.assertTrue(row["dirty"])
        self.assertIn("deleted", row["unknown"])
        record = (b"1 .M S.M. 160000 160000 160000 a b sub\0")
        fake_stat = os.stat(path)
        with mock.patch.object(work, "_git_bytes", return_value=(0, record, b"")), \
                mock.patch.object(work.os, "lstat", return_value=fake_stat):
            row = work._room_status(path)
        self.assertIn("submodule", row["unknown"])

    def test_symlink_mtime_is_not_target_mtime_and_future_clock_is_advisory(self):
        path = self.room("symlink")
        target = os.path.join(self.tmp, "future-target")
        with open(target, "w") as f:
            f.write("outside")
        future = time.time() + 3600
        os.utime(target, (future, future))
        link = os.path.join(path, "link")
        os.symlink(target, link)
        old = time.time() - 3600
        os.utime(link, (old, old), follow_symlinks=False)
        row = work._room_status(path, now=time.time())
        self.assertGreater(row["wrote_ago"], 3000)
        os.utime(link, (future, future), follow_symlinks=False)
        row = work._room_status(path, now=time.time())
        self.assertEqual(row["wrote_ago"], 0)
        self.assertTrue(row["clock_skew"])

    def test_ignored_files_are_excluded_and_status_failure_is_unknown(self):
        path = self.room("ignored")
        with open(os.path.join(path, ".gitignore"), "w") as f:
            f.write("ignored/\n")
        _sh(path, "git", "add", ".gitignore")
        _sh(path, "git", "commit", "-q", "-m", "ignore")
        os.makedirs(os.path.join(path, "ignored"))
        with open(os.path.join(path, "ignored", "secret"), "w") as f:
            f.write("ignored")
        self.assertFalse(work._room_status(path)["dirty"])
        with mock.patch.object(work, "_git_bytes",
                               return_value=(-1, b"", b"timeout")):
            row = work._room_status(path)
        self.assertTrue(row["dirty"])
        self.assertIn("status failed", row["unknown"])


class GuardTest(WorkBase):
    def test_install_guard_dry_then_apply_then_heal(self):
        hook = work.hook_path(self.root)
        rc, out, _err = self.work("install-guard")
        self.assertEqual(rc, 0)
        self.assertIn("HELM_WORK_INTEGRATOR", out)
        self.assertIn("DRY", out)
        self.assertFalse(os.path.exists(hook))          # print, never run
        rc, out, _err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0)
        self.assertTrue(os.access(hook, os.X_OK))
        # the motivating failure: `checkout -b` in the shared checkout heals —
        # pointer back to main, the created branch SURVIVES, message is loud
        r = _sh(self.root, "git", "checkout", "-b", "oops")
        self.assertEqual(r.returncode, 0, r.stderr)
        head = _sh(self.root, "git", "symbolic-ref", "--short", "HEAD")
        self.assertEqual(head.stdout.strip(), "main")
        self.assertTrue(work._has_branch(self.root, "oops"))
        self.assertIn("integrator", r.stderr)
        # worktree add must NOT trip the guard (lane rooms are unguarded)
        wt = self.room("quiet")
        self.assertEqual(
            _sh(wt, "git", "symbolic-ref", "--short", "HEAD").stdout.strip(),
            "lane/quiet")
        # the escape hatch: the integrator's own env skips the heal
        env = dict(os.environ, HELM_WORK_INTEGRATOR="1")
        r = subprocess.run(["git", "checkout", "-q", "-b", "intg"],
                           cwd=self.root, capture_output=True, text=True,
                           timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        head = _sh(self.root, "git", "symbolic-ref", "--short", "HEAD")
        self.assertEqual(head.stdout.strip(), "intg")

    def test_install_guard_refuses_foreign_hook(self):
        hook = work.hook_path(self.root)
        os.makedirs(os.path.dirname(hook), exist_ok=True)
        with open(hook, "w") as f:
            f.write("#!/bin/sh\necho mine\n")
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 1)
        self.assertIn("not overwriting", err)
        with open(hook) as f:
            self.assertIn("echo mine", f.read())


class WiringTest(unittest.TestCase):
    def test_cli_and_gc_wiring(self):
        from helm import cli, gc
        self.assertIn("work", cli.VERBS)
        self.assertIn("work", cli._VERB_HELP)
        rows = [p for p in gc.POLICIES if p["stream"] == "work-worktrees"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cls"], "state")   # report-only, never reaped
        self.assertEqual(rows[0]["act"], "report")


if __name__ == "__main__":
    unittest.main()
