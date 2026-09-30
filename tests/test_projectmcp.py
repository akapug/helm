"""A seat home approves its project's `.mcp.json` servers (task/2698,
task/2670): Claude Code runs a project server only once
`projects[<key>].enabledMcpjsonServers` in the config dir's `.claude.json`
names it, and a seat has nobody to answer the approval prompt. Hermetic:
HELM_HOME, the claude homes root, the default home and every project live in
a tmp dir; no real ~/.claude or ~/.helm is read or written."""
import contextlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from tests._tmphome import home as _tmp_home  # noqa: E402,F401 — plants the tmp config roots first
from helm import homes, launch, projectmcp

SERVERS = {"chrome-devtools": {"command": "npx", "args": ["chrome-devtools-mcp"]},
           "playwriter": {"command": "npx", "args": ["playwriter"]}}


class ProjectMcpBase(unittest.TestCase):
    """A tmp estate: HELM_HOME, a claude homes root and a default home, the
    Orca sync off, instructions distribution off, and a project checkout with
    a `.mcp.json`. Holds no `test_*` method."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="helm-test-projmcp-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": self.j("helm"), "HELM_CHAT_DIR": self.j("chat"),
            "HELM_CHAT_NODE_URL": "", "ORCA_USER_DATA_PATH": self.j("orca"),
            "HELM_CREDHOME_ORCA_SYNC": "0",
            "HELM_INSTRUCTIONS_CANONICAL": "off",
            "HELM_SKILLS_CANONICAL": self.j("no-hub")})
        env.start()
        self.addCleanup(env.stop)
        for k in ("CLAUDE_CONFIG_DIR", "HELM_CHAT_NAME", "MELD_CHAT_NAME",
                  "HELM_CHAT_ROOM", "MELD_CHAT_ROOM", "MELD_HOME",
                  "MELD_INSTRUCTIONS_CANONICAL", "MELD_SKILLS_CANONICAL"):
            os.environ.pop(k, None)
        for p in (mock.patch.dict(homes.ROOTS, {"claude": self.j("claude-homes")}),
                  mock.patch.dict(homes.DEFAULTS,
                                  {"claude": self.j("default-claude")})):
            p.start()
            self.addCleanup(p.stop)
        os.makedirs(homes.ROOTS["claude"])
        os.makedirs(homes.DEFAULTS["claude"])
        # THE FLEET: no live claude on any home unless an arm plants one
        # (orcaadopt.claude_processes, helm's own process census, task/2698)
        self.fleet = ([], [])
        census = mock.patch("helm.orcaadopt.claude_processes",
                            side_effect=lambda: self.fleet)
        census.start()
        self.addCleanup(census.stop)
        self.registered = []
        self.repo = self.project("repo")
        self.register(self.repo)

    def j(self, *p):
        return os.path.join(self.tmp, *p)

    def register(self, path):
        """Name `path` in the helm project registry, as `helm sync` does."""
        from helm import home
        self.registered.append(path)
        os.makedirs(home.global_dir(), exist_ok=True)
        with open(home.registry_path(), "w") as f:
            json.dump({"version": 1, "projects": {
                os.path.basename(x): {"name": os.path.basename(x), "path": x}
                for x in self.registered}}, f)

    def project(self, *where, body=None):
        d = self.j(*where)
        os.makedirs(os.path.join(d, ".git"))
        with open(os.path.join(d, ".mcp.json"), "w") as f:
            f.write(json.dumps({"mcpServers": SERVERS}) if body is None else body)
        return d

    def home(self, name, state):
        """A credhome whose `.claude.json` is written the way Claude Code
        writes it: 2-space indent, non-ASCII kept, no trailing newline, 0600."""
        d = os.path.join(homes.ROOTS["claude"], name)
        os.makedirs(d)
        path = os.path.join(d, ".claude.json")
        if state is not None:
            with open(path, "w", encoding="utf-8") as f:
                f.write(state if isinstance(state, str)
                        else json.dumps(state, indent=2, ensure_ascii=False))
            os.chmod(path, 0o600)
        return d

    @staticmethod
    def raw(home):
        with open(os.path.join(home, ".claude.json"), "rb") as f:
            return f.read()

    def launch_in(self, cwd, name):
        """-> (rc, stderr, exec mock) for `helm launch --no-install --home
        NAME` run from `cwd`."""
        here = os.getcwd()
        os.chdir(cwd)
        self.addCleanup(os.chdir, here)
        err = io.StringIO()
        with mock.patch.object(launch.seats, "join"), \
                mock.patch.object(launch.seat_launch_owner, "exec_attached",
                                  return_value=0) as ex, \
                contextlib.redirect_stderr(err):
            rc = launch.cmd_launch(["--no-install", "--seat", "s1", "--home", name])
        os.chdir(here)
        return rc, err.getvalue(), ex


class LaunchApprovesProjectServersTest(ProjectMcpBase):

    STATE = {"oauthAccount": {"emailAddress": "seat@user.example"},
             "theme": "dark", "displayName": "Zoë",
             "projects": {}}

    def test_a_seat_launched_in_its_project_gets_the_servers_approved_and_a_relaunch_changes_nothing(self):
        """ARM 1. The home's state is Claude Code's shape; after the launch it
        is that same shape (key order, indent, no trailing newline, non-ASCII
        kept, mode 0600) with the approval added under the project key. The
        second launch writes nothing and says nothing about it."""
        home = self.home("seat-home", self.STATE)
        rc, err, ex = self.launch_in(self.repo, "seat-home")
        self.assertEqual(rc, 0, err)
        ex.assert_called_once()
        want = json.loads(json.dumps(self.STATE))
        want["projects"][self.repo] = {
            "enabledMcpjsonServers": ["chrome-devtools", "playwriter"]}
        self.assertEqual(self.raw(home).decode("utf-8"),
                         json.dumps(want, indent=2, ensure_ascii=False))
        self.assertEqual(stat.S_IMODE(os.stat(
            os.path.join(home, ".claude.json")).st_mode), 0o600)
        self.assertIn("project MCP servers approved", err)
        before = self.raw(home)
        rc, err, ex = self.launch_in(self.repo, "seat-home")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.raw(home), before)
        self.assertNotIn("project MCP", err)

    def test_the_approval_is_additive_and_a_rejected_server_stays_rejected(self):
        state = {"projects": {self.repo: {
            "hasTrustDialogAccepted": True,
            "enabledMcpjsonServers": ["mine"],
            "disabledMcpjsonServers": ["playwriter"]}}}
        home = self.home("keep-home", state)
        lines = projectmcp.reconcile(home, self.repo)
        got = json.loads(self.raw(home))["projects"][self.repo]
        self.assertEqual(got["enabledMcpjsonServers"], ["mine", "chrome-devtools"])
        self.assertEqual(got["disabledMcpjsonServers"], ["playwriter"])
        self.assertEqual(list(got), ["hasTrustDialogAccepted",
                                     "enabledMcpjsonServers",
                                     "disabledMcpjsonServers"])
        self.assertEqual(len(lines), 1, lines)

    def test_a_subdirectory_and_a_linked_worktree_approve_every_key_claude_code_resolves(self):
        """Claude Code reads .mcp.json from the cwd upward and keys the
        approval on the cwd, the git top level or the canonical root by
        version: every one of them gets it."""
        sub = self.j("repo", "pkg")
        os.makedirs(sub)
        home = self.home("sub-home", {})
        projectmcp.reconcile(home, sub)
        projects = json.loads(self.raw(home))["projects"]
        self.assertEqual(sorted(projects), sorted([sub, self.repo]))
        # a linked worktree: its .git file names a gitdir whose commondir is
        # the main checkout's .git
        gitdir = self.j("repo", ".git", "worktrees", "lane")
        os.makedirs(gitdir)
        with open(os.path.join(gitdir, "commondir"), "w") as f:
            f.write("../..\n")
        lane = self.j("lane")
        os.makedirs(lane)
        with open(os.path.join(lane, ".git"), "w") as f:
            f.write("gitdir: %s\n" % gitdir)
        shutil.copy(os.path.join(self.repo, ".mcp.json"), lane)
        self.assertEqual(projectmcp.resolve(lane), (lane, [lane, self.repo], False))


class AboveTheGitRootTest(ProjectMcpBase):

    def test_a_seat_launched_above_its_git_root_approves_nothing_and_is_told_where(self):
        """ARM 2. A launch approves only the LAUNCHED cwd's own project. The
        cwd here is the repository's parent, so its one child root is a
        project nobody launched there, and nothing is approved; Claude Code
        would not load that root's servers from here anyway. The launch still
        names the root to launch from, which is where they are approved."""
        home = self.home("above-home", {"projects": {}})
        rc, err, ex = self.launch_in(self.tmp, "above-home")
        self.assertEqual(rc, 0, err)
        ex.assert_called_once()
        self.assertEqual(json.loads(self.raw(home))["projects"], {},
                         "a launch above the root approved the child project")
        self.assertIn("is ABOVE the git root %s" % self.repo, err)
        self.assertIn("launch from %s" % self.repo, err)

    def test_two_child_repositories_are_never_guessed_between(self):
        self.project("other")
        home = self.home("two-home", {"projects": {}})
        self.assertEqual(projectmcp.reconcile(home, self.tmp), [])
        self.assertEqual(json.loads(self.raw(home)), {"projects": {}})


class DegradeTest(ProjectMcpBase):

    def test_a_malformed_mcp_json_is_one_line_and_the_launch_proceeds(self):
        """ARM 5. The .mcp.json does not parse: one line names it, nothing
        is approved, the state file is byte-identical, and claude is exec'd."""
        bad = self.project("bad", body="{not json")
        home = self.home("bad-home", {"projects": {}})
        before = self.raw(home)
        rc, err, ex = self.launch_in(bad, "bad-home")
        self.assertEqual(rc, 0, err)
        ex.assert_called_once()
        self.assertEqual(self.raw(home), before)
        hit = [l for l in err.splitlines() if ".mcp.json" in l]
        self.assertEqual(len(hit), 1, err)
        self.assertIn("unreadable", hit[0])
        self.assertNotIn("Traceback", err)

    def test_an_unreadable_state_file_is_left_untouched_and_the_launch_proceeds(self):
        home = self.home("torn-home", "{torn")
        rc, err, ex = self.launch_in(self.repo, "torn-home")
        self.assertEqual(rc, 0, err)
        ex.assert_called_once()
        self.assertEqual(self.raw(home), b"{torn")
        self.assertIn("project MCP servers NOT approved", err)
        self.assertNotIn("Traceback", err)

    def test_a_symlinked_state_file_is_never_cut(self):
        target = self.j("elsewhere.json")
        with open(target, "w") as f:
            f.write("{}")
        home = self.home("link-home", None)
        os.symlink(target, os.path.join(home, ".claude.json"))
        lines = projectmcp.reconcile(home, self.repo)
        self.assertTrue(os.path.islink(os.path.join(home, ".claude.json")))
        self.assertIn("symlink", " ".join(lines))


class SeatMintApprovesTest(ProjectMcpBase):

    def test_a_minted_seat_dir_is_approved_for_its_workdir(self):
        """The proxy-seat door: _seat_full_agent approves the workdir's
        project servers in the seat's own config dir."""
        from helm import envtidy, seat, seat_launch_assets  # noqa: F401 — seat seeds the impl modules
        cdir = self.j("seat", "claude")
        os.makedirs(cdir)
        err = io.StringIO()
        with mock.patch.object(envtidy, "canonical_mcps", return_value={}), \
                contextlib.redirect_stderr(err):
            seat_launch_assets._seat_full_agent(cdir, "seat:codex", self.repo)
        with open(os.path.join(cdir, ".claude.json")) as f:
            got = json.load(f)
        self.assertEqual(got["projects"][self.repo]["enabledMcpjsonServers"],
                         ["chrome-devtools", "playwriter"], err.getvalue())


if __name__ == "__main__":
    unittest.main()


class CompareAndSwapTest(ProjectMcpBase):
    """CURE (task/2698): a running Claude Code rewrites its own state file.
    Every write here is a compare-and-swap: the file read is re-stated just
    before the replace, a changed one is re-read and the change recomputed,
    and after CAS_TRIES a file that keeps changing is refused, untouched."""

    def churn(self, path, times):
        """A writer that changes `path` in the window before our replace,
        `times` times, each time adding its own key."""
        n = {"i": 0}

        def write(_path):
            if n["i"] >= times:
                return
            n["i"] += 1
            with open(path, encoding="utf-8") as f:
                body = json.load(f)
            body["theirs%d" % n["i"]] = n["i"]
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(body, indent=2))
        return write

    def test_an_approval_retries_over_a_concurrent_write_and_keeps_its_key(self):
        home = self.home("race-home", {"projects": {}})
        path = os.path.join(home, ".claude.json")
        with mock.patch.object(projectmcp, "_before_replace",
                               side_effect=self.churn(path, 1)):
            lines = projectmcp.reconcile(home, self.repo)
        got = json.loads(self.raw(home))
        self.assertEqual(got["theirs1"], 1, "the concurrent write was lost")
        self.assertEqual(got["projects"][self.repo]["enabledMcpjsonServers"],
                         ["chrome-devtools", "playwriter"])
        self.assertIn("approved", " ".join(lines))

    def test_a_write_during_the_backup_is_kept(self):
        """The backup is taken BEFORE the last check that the file is
        unchanged, so a session's write that lands while the backup copies is
        seen by that check and kept, never replaced away."""
        home = self.home("backup-home", {"projects": {}})
        path = os.path.join(home, ".claude.json")
        calls = []

        def backup_while_they_write(p):
            calls.append(p)
            if len(calls) == 1:
                body = json.loads(self.raw(home))
                body["theirs"] = 1
                with open(path, "w") as f:
                    json.dump(body, f)

        def change(body):
            body.setdefault("projects", {})["x"] = {"ok": True}
            return True

        _r, wrote = projectmcp.update_state(path, change,
                                            before_write=backup_while_they_write)
        got = json.loads(self.raw(home))
        self.assertTrue(wrote)
        self.assertEqual(got.get("theirs"), 1, "a write during the backup was lost")
        self.assertEqual(got["projects"]["x"], {"ok": True})

    def test_persistent_churn_refuses_loudly_and_writes_nothing(self):
        home = self.home("churn-home", {"projects": {}})
        path = os.path.join(home, ".claude.json")
        with mock.patch.object(projectmcp, "_before_replace",
                               side_effect=self.churn(path, 99)):
            lines = projectmcp.reconcile(home, self.repo)
        got = json.loads(self.raw(home))
        self.assertEqual(got["theirs%d" % projectmcp.CAS_TRIES],
                         projectmcp.CAS_TRIES)
        self.assertNotIn(self.repo, got["projects"], "a churning file was written")
        said = " ".join(lines)
        self.assertIn("NOT approved", said)
        self.assertIn(home, said)
        self.assertIn("changed", said)

    def test_the_user_scope_provision_retries_and_refuses_the_same_way(self):
        with open(os.path.join(homes.DEFAULTS["claude"], ".claude.json"),
                  "w") as f:
            json.dump({"mcpServers": {"cv": {"command": "cv"}}}, f)
        home = self.home("user-home", {"mcpServers": {}})
        path = os.path.join(home, ".claude.json")
        with mock.patch.object(projectmcp, "_before_replace",
                               side_effect=self.churn(path, 1)):
            verdict, _detail = homes._mcp_provision(home)
        got = json.loads(self.raw(home))
        self.assertEqual(verdict, "linked")
        self.assertEqual((got["theirs1"], list(got["mcpServers"])), (1, ["cv"]))
        home2 = self.home("user-home-2", {"mcpServers": {}})
        path2 = os.path.join(home2, ".claude.json")
        with mock.patch.object(projectmcp, "_before_replace",
                               side_effect=self.churn(path2, 99)):
            verdict, detail = homes._mcp_provision(home2)
        self.assertEqual(verdict, "error")
        self.assertIn("changed", detail)
        self.assertEqual(json.loads(self.raw(home2))["mcpServers"], {})


class TrustTest(ProjectMcpBase):
    """CURE (task/2698): a launch approves only its own project's servers,
    the backfill only trusted projects, and a rejected name never."""

    def test_a_launch_never_approves_an_ancestors_servers_above_the_git_root(self):
        with open(self.j(".mcp.json"), "w") as f:       # above the repo
            json.dump({"mcpServers": {"ancestor-tool": {"command": "x"}}}, f)
        home = self.home("anc-home", {})
        projectmcp.reconcile(home, self.repo)
        projects = json.loads(self.raw(home))["projects"]
        self.assertEqual(projects[self.repo]["enabledMcpjsonServers"],
                         ["chrome-devtools", "playwriter"])
        self.assertNotIn("ancestor-tool", json.dumps(projects))

    def test_the_backfill_skips_a_project_the_home_never_trusted(self):
        other = self.project("other")
        home = self.home("fill-home", {"projects": {
            self.repo: {"hasTrustDialogAccepted": True},
            other: {"hasTrustDialogAccepted": False}}})
        homes._prov_project_mcp(home)
        projects = json.loads(self.raw(home))["projects"]
        self.assertIn("enabledMcpjsonServers", projects[self.repo])
        self.assertEqual(projects[other], {"hasTrustDialogAccepted": False})

    def test_a_disabled_name_stays_disabled_under_every_key_and_path(self):
        sub = self.j("repo", "pkg")
        os.makedirs(sub)
        home = self.home("dis-home", {"projects": {
            self.repo: {"hasTrustDialogAccepted": True,
                        "disabledMcpjsonServers": ["playwriter"]}}})
        projectmcp.reconcile(home, sub)                 # launch path
        homes._prov_project_mcp(home)                   # backfill path
        projects = json.loads(self.raw(home))["projects"]
        for key in (sub, self.repo):
            self.assertNotIn("playwriter",
                             projects[key].get("enabledMcpjsonServers", []), key)
        self.assertNotIn("enableAllProjectMcpServers", json.dumps(projects))
        self.assertNotIn("hasTrustDialogAccepted", json.dumps(projects[sub]))



class RegisteredAndHeldTest(ProjectMcpBase):
    """CURE (task/2698, the reconciled bar): a launch approves only a
    REGISTERED helm project's servers, and no write touches a home a live
    Claude Code holds, since it rewrites the whole file from memory."""

    def test_an_unregistered_cwd_is_not_approved(self):
        stray = self.project("stray")
        home = self.home("stray-home", {"projects": {}})
        before = self.raw(home)
        lines = projectmcp.reconcile(home, stray)
        self.assertEqual(self.raw(home), before)
        self.assertIn("not a registered helm project", " ".join(lines))

    def test_a_linked_worktree_of_a_registered_checkout_is_approved(self):
        gitdir = self.j("repo", ".git", "worktrees", "lane")
        os.makedirs(gitdir)
        with open(os.path.join(gitdir, "commondir"), "w") as f:
            f.write("../..\n")
        lane = self.j("lane")
        os.makedirs(lane)
        with open(os.path.join(lane, ".git"), "w") as f:
            f.write("gitdir: %s\n" % gitdir)
        shutil.copy(os.path.join(self.repo, ".mcp.json"), lane)
        home = self.home("lane-home", {})
        projectmcp.reconcile(home, lane)
        self.assertEqual(sorted(json.loads(self.raw(home))["projects"]),
                         sorted([lane, self.repo]))

    def plant_holder(self, home, pid=4242):
        """A live claude whose environ names `home` as its config dir."""
        self.fleet = ([{"pid": pid, "seat": "busy-seat"}], [])
        env = {"CLAUDE_CONFIG_DIR": home}
        p = mock.patch("helm.beacons.proc_env",
                       side_effect=lambda q: env if q == pid else None)
        p.start()
        self.addCleanup(p.stop)

    def test_a_held_home_is_not_written_and_the_line_names_the_holder(self):
        home = self.home("held-home", {"projects": {}})
        before = self.raw(home)
        self.plant_holder(home)
        lines = projectmcp.reconcile(home, self.repo)
        self.assertEqual(self.raw(home), before)
        said = " ".join(lines)
        self.assertIn("pid 4242 (seat busy-seat) runs on it", said)
        self.assertIn("helm homes provision --apply", said)
        with open(os.path.join(homes.DEFAULTS["claude"], ".claude.json"),
                  "w") as f:
            json.dump({"mcpServers": {"cv": {"command": "cv"}}}, f)
        verdict, detail = homes._mcp_provision(home)
        self.assertEqual(verdict, "held")
        self.assertIn("pid 4242", detail)
        notes, _ = homes._prov_project_mcp(
            self.home("held-2", {"projects": {self.repo: {
                "hasTrustDialogAccepted": True}}}))
        self.assertEqual(self.raw(home), before)

    def test_a_held_home_with_nothing_to_add_says_nothing(self):
        home = self.home("quiet-home", {})
        projectmcp.reconcile(home, self.repo)
        self.plant_holder(home)
        self.assertEqual(projectmcp.reconcile(home, self.repo), [])

    def test_an_unreadable_census_counts_as_held(self):
        home = self.home("blind-home", {"projects": {}})
        before = self.raw(home)
        self.fleet = ([], [5151])
        lines = projectmcp.reconcile(home, self.repo)
        self.assertEqual(self.raw(home), before)
        self.assertIn("claude pid 5151 could not be read", " ".join(lines))
        with mock.patch("helm.orcaadopt.claude_processes",
                        side_effect=OSError("no /proc")):
            self.assertIn("census could not be read",
                          projectmcp.holder(home))
