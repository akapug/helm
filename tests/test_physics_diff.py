"""Hermetic tests for the config-cascade / physics-diff layer — the paths the
web contract tests only smoke-shape: physics_diff's added/removed/changed
partitioning, the _VOLATILE exclusion (volatile per-seat keys must never
diff), _flatten's name/event keying, the claude .mcp.json walk-up shadowing
(nearest-wins), approval states, and configs._annotate_mcp_shadows' cross-tier
winner selection.

Everything plants tmp homes/cwds; MANAGED_DIRS is repointed away from the real
/etc; the real ~/.claude and /etc/claude-code are never touched.
"""
import json
import os
import shutil
import tempfile
import unittest

from helm import configs, physics


class PhysicsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-physics-")
        self._orig_managed = physics.MANAGED_DIRS
        physics.MANAGED_DIRS = (os.path.join(self.tmp, "no-managed-here"),)
        self._orig_home_roots = list(configs.HOME_ROOTS)

    def tearDown(self):
        physics.MANAGED_DIRS = self._orig_managed
        configs.HOME_ROOTS = self._orig_home_roots
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- planters ----------------------------------------------------------
    def _claude_home(self, name, settings=None, state=None):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        if settings is not None:
            with open(os.path.join(d, "settings.json"), "w") as f:
                json.dump(settings, f)
        if state is not None:
            with open(os.path.join(d, ".claude.json"), "w") as f:
                json.dump(state, f)
        return d

    def _codex_home(self, name, toml=""):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "config.toml"), "w") as f:
            f.write(toml)
        return d


class PhysicsDiffTest(PhysicsBase):
    def test_identical_homes_diff_empty(self):
        a = self._claude_home("ha", settings={"model": "opus"})
        b = self._claude_home("hb", settings={"model": "opus"})
        d = physics.physics_diff(a, b, "claude")
        self.assertTrue(d["identical"], d)
        self.assertEqual(d["added_in_b"], {})
        self.assertEqual(d["removed_in_b"], {})
        self.assertEqual(d["changed"], {})

    def test_changed_setting_reported_with_both_sides(self):
        a = self._claude_home("ha", settings={"model": "opus"})
        b = self._claude_home("hb", settings={"model": "sonnet"})
        d = physics.physics_diff(a, b, "claude")
        self.assertFalse(d["identical"])
        key = "settingsHighlights.model"
        self.assertEqual(d["changed"][key], {"a": "opus", "b": "sonnet"})
        self.assertEqual(d["added_in_b"], {})
        self.assertEqual(d["removed_in_b"], {})

    def test_added_and_removed_leaf_keys(self):
        # bools are shape-stable (always present in the report), so the
        # ADD/REMOVE signal lives in list-valued sections: hooks on one side.
        hook = {"SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": "/bin/echo hi"}]}]}
        a = self._claude_home("ha", settings={"model": "opus", "hooks": hook})
        b = self._claude_home("hb", settings={"model": "opus"})
        d = physics.physics_diff(a, b, "claude")
        self.assertTrue([k for k in d["removed_in_b"] if k.startswith("hooks[SessionStart]")],
                        d["removed_in_b"])
        d2 = physics.physics_diff(b, a, "claude")
        self.assertTrue([k for k in d2["added_in_b"] if k.startswith("hooks[SessionStart]")],
                        d2["added_in_b"])
        self.assertNotIn("settingsHighlights.model", d["changed"])

    def test_mcp_server_presence_diffs_by_name_key(self):
        a = self._claude_home("ha", state={"mcpServers": {"cv": {"command": "/bin/cv"}}})
        b = self._claude_home("hb", state={"mcpServers": {"db": {"command": "/bin/db"}}})
        d = physics.physics_diff(a, b, "claude")
        # _flatten keys list-of-dicts by their "name" — per-server, not per-index
        added = [k for k in d["added_in_b"] if k.startswith("mcpServers[db]")]
        removed = [k for k in d["removed_in_b"] if k.startswith("mcpServers[cv]")]
        self.assertTrue(added, d["added_in_b"])
        self.assertTrue(removed, d["removed_in_b"])

    def test_volatile_keys_never_diff(self):
        # home/cwd/stateFile/settingsLayers/warnings/launchSeams are per-seat
        # descriptors, not physics — two homes with IDENTICAL physics but
        # different paths must still diff clean.
        a = self._claude_home("ha", settings={"model": "opus"})
        b = self._claude_home("hb", settings={"model": "opus"})
        d = physics.physics_diff(a, b, "claude")
        for section in (d["added_in_b"], d["removed_in_b"], d["changed"]):
            for k in section:
                self.assertFalse(physics._VOLATILE.match(k),
                                 "volatile key leaked into diff: %s" % k)
        self.assertTrue(d["identical"])

    def test_cwd_axis_diffs_trust_and_memory(self):
        # with a cwd passed, the diff compares FULL seat physics — per-cwd
        # state (trust, local-scope MCPs, approvals) rides the home's
        # state file, keyed by the cwd the seat launches in.
        proj = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(proj, "sub"))
        with open(os.path.join(proj, "CLAUDE.md"), "w") as f:
            f.write("# memory\n")
        leaf = os.path.join(proj, "sub")
        a = self._claude_home("ha", state={
            "projects": {leaf: {"hasTrustDialogAccepted": True,
                                "mcpServers": {"local": {"command": "/bin/local"}}}}})
        b = self._claude_home("hb")  # never opened the cwd
        d = physics.physics_diff(a, b, "claude", cwd=leaf)
        self.assertFalse(d["identical"])
        # trust + the local-scope MCP server diff; the shared memory chain
        # (same cwd on both sides) stays equal.
        self.assertEqual(d["changed"]["trust.cwdKnown"], {"a": True, "b": False})
        self.assertEqual(d["changed"]["trust.hasTrustDialogAccepted"],
                         {"a": True, "b": None})
        self.assertTrue([k for k in d["removed_in_b"] if k.startswith("mcpServers[local]")],
                        d["removed_in_b"])
        self.assertEqual(d["added_in_b"], {})

    def test_codex_diff_model_and_mcp(self):
        a = self._codex_home("ca", 'model = "gpt-5"\n')
        b = self._codex_home("cb", 'model = "gpt-5-mini"\n[mcp_servers.cv]\ncommand = "/bin/cv"\n')
        d = physics.physics_diff(a, b, "codex")
        self.assertEqual(d["changed"]["settingsHighlights.model"],
                         {"a": "gpt-5", "b": "gpt-5-mini"})
        self.assertTrue([k for k in d["added_in_b"] if k.startswith("mcpServers[cv]")])


class ClaudeMcpWalkupTest(PhysicsBase):
    """The .mcp.json cascade: nearest ancestor wins, farther copies are
    shadowed, approvals ride the home's per-cwd state, and the .claude/.mcp.json
    stray warns."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "tree")
        self.leaf = os.path.join(self.root, "sub", "leaf")
        os.makedirs(self.leaf)

    def _mcpjson(self, dirpath, servers):
        with open(os.path.join(dirpath, ".mcp.json"), "w") as f:
            json.dump({"mcpServers": servers}, f)

    def test_nearest_mcpjson_wins_ancestor_shadowed(self):
        self._mcpjson(self.root, {"cv": {"command": "/bin/root-cv"}})
        self._mcpjson(os.path.join(self.root, "sub"), {"cv": {"command": "/bin/near-cv"}})
        home = self._claude_home("h", state={
            "projects": {self.leaf: {"hasTrustDialogAccepted": True,
                                     "enabledMcpjsonServers": ["cv"]}}})
        r = physics.physics_report(home, self.leaf, "claude")
        cvs = [m for m in r["mcpServers"] if m["name"] == "cv"]
        self.assertEqual(len(cvs), 2, r["mcpServers"])
        winner = next(m for m in cvs if not m.get("shadowed"))
        loser = next(m for m in cvs if m.get("shadowed"))
        self.assertEqual(winner["detail"], "near-cv")
        self.assertEqual(winner["approval"], "approved")
        self.assertEqual(loser["detail"], "root-cv")
        self.assertTrue(loser["shadowed_by"].endswith(os.path.join("sub", ".mcp.json")))

    def test_approval_states_disabled_pending_and_enable_all(self):
        self._mcpjson(self.root, {
            "ok": {"command": "/bin/ok"},
            "no": {"command": "/bin/no"},
            "maybe": {"command": "/bin/maybe"}})
        home = self._claude_home("h", state={
            "projects": {self.leaf: {"hasTrustDialogAccepted": True,
                                     "enabledMcpjsonServers": ["ok"],
                                     "disabledMcpjsonServers": ["no"]}}})
        r = physics.physics_report(home, self.leaf, "claude")
        by = {m["name"]: m for m in r["mcpServers"]}
        self.assertEqual(by["ok"]["approval"], "approved")
        self.assertEqual(by["no"]["approval"], "rejected")
        self.assertEqual(by["maybe"]["approval"], "pending-approval-prompt")
        self.assertTrue(any("not yet approved" in w and "maybe" in w
                            for w in r["warnings"]))
        # enableAllProjectMcpServers flips the pending state (and says so)
        home2 = self._claude_home("h2", settings={"enableAllProjectMcpServers": True},
                                  state={"projects": {self.leaf: {
                                      "hasTrustDialogAccepted": True,
                                      "enabledMcpjsonServers": ["ok"],
                                      "disabledMcpjsonServers": ["no"]}}})
        r2 = physics.physics_report(home2, self.leaf, "claude")
        by2 = {m["name"]: m for m in r2["mcpServers"]}
        self.assertEqual(by2["maybe"]["approval"],
                         "approved (enableAllProjectMcpServers)")
        self.assertEqual(by2["ok"]["approval"], "approved")  # explicit enable: no blanket tag
        self.assertEqual(by2["no"]["approval"], "rejected")  # disable wins over enable-all

    def test_stray_dotclaude_mcpjson_warns_dead_config(self):
        os.makedirs(os.path.join(self.leaf, ".claude"))
        self._mcpjson(os.path.join(self.leaf, ".claude"), {"x": {"command": "/bin/x"}})
        home = self._claude_home("h", state={"projects": {self.leaf: {}}})
        r = physics.physics_report(home, self.leaf, "claude")
        self.assertEqual([m for m in r["mcpServers"] if m["name"] == "x"], [],
                         "a .claude/.mcp.json must never resolve")
        self.assertTrue(any("dead config" in w for w in r["warnings"]))

    def test_malformed_mcpjson_warns_never_raises(self):
        with open(os.path.join(self.root, ".mcp.json"), "w") as f:
            f.write("{not json")
        home = self._claude_home("h", state={"projects": {self.leaf: {}}})
        r = physics.physics_report(home, self.leaf, "claude")
        self.assertTrue(any("malformed .mcp.json" in w for w in r["warnings"]))

    def test_mcp_shape_redacts_url_and_flags_secret_headers(self):
        home = self._claude_home("h", state={"mcpServers": {
            "web": {"url": "https://user:secret@api.example.com/v1/mcp?key=abc",
                    "headers": {"Authorization": "Bearer xyz", "X-Trace": "1"}}}})
        r = physics.physics_report(home, None, "claude")
        m = next(m for m in r["mcpServers"] if m["name"] == "web")
        self.assertEqual(m["detail"], "https://api.example.com")
        self.assertEqual(m["headerNames"], ["Authorization", "X-Trace"])
        self.assertTrue(m["hasSecretHeaders"])
        self.assertNotIn("secret", json.dumps(m))
        self.assertNotIn("abc", json.dumps(m))


class AnnotateMcpShadowsTest(PhysicsBase):
    """configs._annotate_mcp_shadows — the cross-tier winner pass that the web
    tests only exercise indirectly."""

    def _report(self, servers):
        return {"mcpServers": servers}

    def test_managed_beats_project_beats_user(self):
        rep = self._report([
            {"name": "cv", "source": "home-global"},
            {"name": "cv", "source": "project-mcpjson"},
            {"name": "cv", "source": "managed"},
        ])
        configs._annotate_mcp_shadows(rep)
        by = {(m["name"], m["source"]): m for m in rep["mcpServers"]}
        self.assertNotIn("shadowed", by[("cv", "managed")])
        self.assertTrue(by[("cv", "project-mcpjson")]["shadowed"])
        self.assertEqual(by[("cv", "project-mcpjson")]["shadowed_by"], "managed")
        self.assertTrue(by[("cv", "home-global")]["shadowed"])

    def test_local_scope_beats_project_scope(self):
        rep = self._report([
            {"name": "cv", "source": "project-mcpjson"},
            {"name": "cv", "source": "home-project-approval"},
        ])
        configs._annotate_mcp_shadows(rep)
        by = {m["source"]: m for m in rep["mcpServers"]}
        self.assertNotIn("shadowed", by["home-project-approval"])
        self.assertTrue(by["project-mcpjson"]["shadowed"])

    def test_already_shadowed_entries_never_win(self):
        # a physics-walkup shadow (nearest-wins within .mcp.json) ranks -2: it
        # can never become the cross-tier winner even against a lone rival.
        rep = self._report([
            {"name": "cv", "source": "project-mcpjson", "shadowed": True,
             "shadowed_by": "/nearer/.mcp.json"},
            {"name": "cv", "source": "home-global"},
        ])
        configs._annotate_mcp_shadows(rep)
        walkup = next(m for m in rep["mcpServers"] if m["source"] == "project-mcpjson")
        user = next(m for m in rep["mcpServers"] if m["source"] == "home-global")
        self.assertNotIn("shadowed", user, "walk-up shadow must not win the tier race")
        self.assertEqual(walkup["shadowed_by"], "/nearer/.mcp.json",
                         "the original walk-up attribution is preserved")

    def test_singletons_and_distinct_names_untouched(self):
        rep = self._report([
            {"name": "cv", "source": "home-global"},
            {"name": "db", "source": "plugin:dbtool"},
        ])
        configs._annotate_mcp_shadows(rep)
        for m in rep["mcpServers"]:
            self.assertNotIn("shadowed", m)

    def test_plugin_source_ranks_below_user_scope(self):
        rep = self._report([
            {"name": "cv", "source": "plugin:cvkit"},
            {"name": "cv", "source": "home-global"},
        ])
        configs._annotate_mcp_shadows(rep)
        by = {m["source"]: m for m in rep["mcpServers"]}
        self.assertNotIn("shadowed", by["home-global"])
        self.assertEqual(by["plugin:cvkit"]["shadowed_by"], "home-global")

    def test_end_to_end_via_configs_resolve(self):
        # the annotation is live on the resolve path: a project .mcp.json
        # server colliding with a user-scope server of the same name.
        proj = os.path.join(self.tmp, "proj2")
        os.makedirs(proj)
        with open(os.path.join(proj, ".mcp.json"), "w") as f:
            json.dump({"mcpServers": {"cv": {"command": "/bin/proj-cv"}}}, f)
        home = self._claude_home("h3", state={
            "mcpServers": {"cv": {"command": "/bin/user-cv"}},
            "projects": {proj: {"hasTrustDialogAccepted": True,
                                "enabledMcpjsonServers": ["cv"]}}})
        configs.HOME_ROOTS.append(home)
        r = configs.resolve(home, proj, "claude")
        cvs = [m for m in r["mcpServers"] if m["name"] == "cv"]
        self.assertEqual(len(cvs), 2)
        proj_m = next(m for m in cvs if m["source"] == "project-mcpjson")
        user_m = next(m for m in cvs if m["source"] == "home-global")
        self.assertNotIn("shadowed", proj_m)
        self.assertEqual(user_m["shadowed_by"], "project-mcpjson")


if __name__ == "__main__":
    unittest.main()
