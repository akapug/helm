"""Hermetic model tests for helm.configs internals — the recognition gate,
edit-safety classification, TOML validation, backup listing, home-scope
enumeration, and tree walking that the web contract tests reach only
through HTTP.

configs.py freezes CWD_ROOTS/HOME_ROOTS at import, so this module patches the
module attributes directly (the test_web_configs pattern) and restores them —
the real ~/dev, ~/.claude, ~/.cache are never touched.
"""
import json
import os
import shutil
import stat
import tempfile
import unittest

from helm import configs


class ConfigsModelInternalsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-cfgmodel-")
        j = lambda *p: os.path.join(self.tmp, *p)
        self.cwdroot = j("dev")
        self.home = j("claude-home")
        self.codexhome = j("codex-home")
        os.makedirs(self.cwdroot)
        os.makedirs(self.home)
        os.makedirs(self.codexhome)
        self._orig = {k: getattr(configs, k) for k in
                      ("CWD_ROOTS", "HOME_ROOTS", "BACKUP_DIR", "MANAGED_DIRS")}
        configs.CWD_ROOTS = [self.cwdroot]
        configs.HOME_ROOTS = [self.home, self.codexhome]
        configs.BACKUP_DIR = j("backups")
        configs.MANAGED_DIRS = (j("managed"),)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(configs, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, path, content, mode=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        if mode is not None:
            os.chmod(path, mode)
        return path

    # -- _is_recognized_config: the single gate -----------------------------
    def test_recognized_project_files_at_any_depth(self):
        deep = os.path.join(self.cwdroot, "a", "b", "proj")
        p = self._write(os.path.join(deep, ".mcp.json"), "{}")
        self.assertTrue(configs._is_recognized_config(os.path.realpath(p)))
        self.assertTrue(configs._is_recognized_config(
            os.path.join(deep, ".claude", "settings.local.json")))
        self.assertTrue(configs._is_recognized_config(os.path.join(deep, "AGENTS.md")))

    def test_recognized_rules_pattern_and_rejections(self):
        proj = os.path.join(self.cwdroot, "proj")
        ok = os.path.join(proj, ".claude", "rules", "style.md")
        self.assertTrue(configs._is_recognized_config(ok))
        # extension alone is NOT enough
        self.assertFalse(configs._is_recognized_config(os.path.join(proj, "settings.json")))
        self.assertFalse(configs._is_recognized_config(os.path.join(proj, "package.json")))
        # credential stores are denied even under a home root
        self.assertFalse(configs._is_recognized_config(os.path.join(self.home, ".credentials.json")))
        self.assertFalse(configs._is_recognized_config(os.path.join(self.codexhome, "auth.json")))
        # a rules path that is not *.md, and a NESTED rules dir, don't match
        self.assertFalse(configs._is_recognized_config(
            os.path.join(proj, ".claude", "rules", "style.txt")))
        self.assertFalse(configs._is_recognized_config(
            os.path.join(proj, ".claude", "rules", "sub", "style.md")))

    def test_home_files_must_sit_directly_in_a_home_root(self):
        self.assertTrue(configs._is_recognized_config(os.path.join(self.home, "settings.json")))
        self.assertTrue(configs._is_recognized_config(os.path.join(self.codexhome, "config.toml")))
        # same basename, one level down: not a home-scope file
        self.assertFalse(configs._is_recognized_config(
            os.path.join(self.home, "nested", "settings.json")))

    # -- classify_path: editable iff recognized + in-roots + not managed ----
    def test_classify_denies_credentials_and_outsiders(self):
        _, ed, reason = configs.classify_path(os.path.join(self.home, ".credentials.json"))
        self.assertFalse(ed)
        self.assertIn("credential", reason)
        _, ed, reason = configs.classify_path("/etc/passwd")
        self.assertFalse(ed)
        self.assertIn("not a recognized", reason)
        _, ed, reason = configs.classify_path(os.path.join(self.tmp, "elsewhere", "CLAUDE.md"))
        self.assertFalse(ed)
        self.assertIn("outside the allowlisted", reason)

    def test_classify_plugin_tree_is_read_only(self):
        # a recognized-shaped file inside a home's plugins/ tree
        p = self._write(os.path.join(self.home, "plugins", "CLAUDE.md"), "# plug")
        _, ed, reason = configs.classify_path(p)
        self.assertFalse(ed)
        self.assertIn("plugin/managed", reason)
        # but a project's OWN plugins/ dir is legit
        proj = self._write(os.path.join(self.cwdroot, "mine", "plugins", "CLAUDE.md"), "# x")
        _, ed, _ = configs.classify_path(proj)
        self.assertTrue(ed)

    def test_classify_create_if_absent_requires_living_parent(self):
        proj = os.path.join(self.cwdroot, "newproj")
        os.makedirs(proj)
        _, ed, _ = configs.classify_path(os.path.join(proj, "CLAUDE.md"))
        self.assertTrue(ed, "absent file, living parent: create-if-absent")
        _, ed, reason = configs.classify_path(
            os.path.join(self.cwdroot, "gone", "deeper", "CLAUDE.md"))
        self.assertFalse(ed)
        self.assertIn("parent directory", reason)

    # -- _ext_type ----------------------------------------------------------
    def test_ext_type_mapping(self):
        self.assertEqual(configs._ext_type("x/.mcp.json"), "json")
        self.assertEqual(configs._ext_type("x/settings.json"), "json")
        self.assertEqual(configs._ext_type("x/config.toml"), "toml")
        self.assertEqual(configs._ext_type("x/CLAUDE.md"), "md")
        self.assertEqual(configs._ext_type("x/Makefile"), "other")

    # -- _validate: TOML actually parsed (py3.11+ tomllib present here) -----
    def test_validate_toml_good_and_bad(self):
        if configs.tomllib is None:
            self.skipTest("tomllib unavailable")
        ok, err = configs._validate("toml", 'model = "gpt-5"\n')
        self.assertTrue(ok)
        ok, err = configs._validate("toml", "model = = =\n")
        self.assertFalse(ok)
        self.assertIn("invalid TOML", err)
        ok, err = configs._validate("md", "anything goes\n")
        self.assertTrue(ok)

    def test_write_file_toml_invalid_leaves_file_untouched(self):
        if configs.tomllib is None:
            self.skipTest("tomllib unavailable")
        p = self._write(os.path.join(self.codexhome, "config.toml"), 'model = "a"\n')
        res = configs.write_file(p, "model = =\n")
        self.assertIn("invalid TOML", res["error"])
        with open(p) as f:
            self.assertEqual(f.read(), 'model = "a"\n')

    # -- backups: list_backups reads sidecars, restore re-validates ---------
    def test_list_backups_shape_and_orig_from_sidecar(self):
        p = self._write(os.path.join(self.home, "settings.json"), '{"a": 1}')
        configs.write_file(p, '{"a": 2}')
        configs.write_file(p, '{"a": 3}')
        bs = [b for b in configs.list_backups() if b["orig"] == os.path.realpath(p)]
        self.assertEqual(len(bs), 2, bs)
        for b in bs:
            self.assertTrue(b["backup"].startswith(os.path.realpath(configs.BACKUP_DIR)))
            self.assertGreater(b["size"], 0)
            self.assertIn("at", b)
        # .orig sidecars never appear as backups themselves
        self.assertFalse(any(b["backup"].endswith(".orig") for b in bs))

    def test_restore_of_deleted_target_recreates_it(self):
        p = self._write(os.path.join(self.home, "settings.json"), '{"v": 1}')
        configs.write_file(p, '{"v": 2}')
        mine = [b for b in configs.list_backups() if b["orig"] == os.path.realpath(p)][0]
        os.unlink(p)
        res = configs.restore(mine["backup"])
        self.assertNotIn("error", res)
        with open(p) as f:
            self.assertEqual(json.load(f), {"v": 1})

    def test_backup_of_create_returns_none_and_second_write_backs_up(self):
        p = os.path.join(self.home, "settings.json")
        res = configs.write_file(p, '{"n": 1}')
        self.assertIsNone(res["backup"])
        res2 = configs.write_file(p, '{"n": 2}')
        self.assertTrue(res2["backup"])

    # -- homes_configs: the top of the cascade ------------------------------
    def test_homes_configs_enumerates_home_scope_only(self):
        self._write(os.path.join(self.home, "settings.json"), "{}")
        self._write(os.path.join(self.home, ".claude.json"), "{}")
        self._write(os.path.join(self.home, "auth.json"), "{}")  # never listed
        self._write(os.path.join(self.codexhome, "config.toml"), 'model = "x"\n')
        out = {h["home"]: h for h in configs.homes_configs()}
        rels = {f["rel"] for f in out[os.path.basename(self.home)]["files"]}
        self.assertEqual(rels, {"settings.json", ".claude.json"})
        self.assertEqual(out[os.path.basename(self.codexhome)]["provider"], "codex")

    # -- tree: skip-dirs never walked, depth bounded -------------------------
    def test_find_config_dirs_skips_noise_and_respects_depth(self):
        self._write(os.path.join(self.cwdroot, "ok", "CLAUDE.md"), "# ok")
        for noise in ("node_modules", ".git", "target", ".venv"):
            self._write(os.path.join(self.cwdroot, "noisy", noise, "CLAUDE.md"), "# no")
        deep = self.cwdroot
        for i in range(8):
            deep = os.path.join(deep, "d%d" % i)
        self._write(os.path.join(deep, "CLAUDE.md"), "# too deep")
        hits = configs._find_config_dirs(self.cwdroot)
        self.assertIn(os.path.realpath(os.path.join(self.cwdroot, "ok")), hits)
        self.assertFalse(any("node_modules" in h or ".git" in h or "target" in h
                             or ".venv" in h for h in hits))
        self.assertNotIn(os.path.realpath(deep), hits)

    def test_project_files_at_includes_dir_entries_and_rules(self):
        proj = os.path.join(self.cwdroot, "p")
        self._write(os.path.join(proj, "CLAUDE.md"), "# p")
        self._write(os.path.join(proj, ".claude", "rules", "a.md"), "r")
        os.makedirs(os.path.join(proj, ".claude", "skills", "learn"))
        files = {f["rel"]: f for f in configs._project_files_at(os.path.realpath(proj))}
        self.assertIn(os.path.join(".claude", "rules", "a.md"), files)
        skills = files[".claude/skills/"]
        self.assertEqual(skills["entries"], ["learn"])
        self.assertFalse(skills["editable"])

    # -- write_file mode discipline beyond the cred-home case ---------------
    def test_new_project_config_is_world_readable_not_owner_only(self):
        proj = os.path.join(self.cwdroot, "projx")
        os.makedirs(proj)
        p = os.path.join(proj, "CLAUDE.md")
        res = configs.write_file(p, "# hello\n")
        self.assertNotIn("error", res)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o644)

    def test_write_creates_missing_parent_and_says_so(self):
        p = os.path.join(self.cwdroot, "made", ".claude", "settings.json")
        os.makedirs(os.path.join(self.cwdroot, "made"))
        res = configs.write_file(p, "{}")
        self.assertNotIn("error", res)
        self.assertTrue(res["created_parent"])

    # -- entry_op edge shapes -------------------------------------------------
    def test_entry_op_rejects_non_object_root_and_bad_kinds(self):
        p = self._write(os.path.join(self.home, "settings.json"), "[1,2]")
        res = configs.entry_op("add", p, "mcp", "x", {"command": "/bin/x"})
        self.assertIn("not a JSON object", res["error"])
        p2 = self._write(os.path.join(self.home, "settings.json"), '{"mcpServers": [1]}')
        res = configs.entry_op("add", p2, "mcp", "x", {"command": "/bin/x"})
        self.assertIn("mcpServers is not an object", res["error"])
        p3 = self._write(os.path.join(self.home, "settings.json"), '{"mcpServers": {}}')
        res = configs.entry_op("enable", p3, "mcp", "x")
        self.assertIn("unsupported action", res["error"])
        res = configs.entry_op("add", p3, "hooks", "x")
        self.assertIn("unsupported kind", res["error"])

    def test_entry_op_mcp_add_remove_round_trip_validates(self):
        p = os.path.join(self.cwdroot, "eproj", ".mcp.json")
        os.makedirs(os.path.dirname(p))
        res = configs.entry_op("add", p, "mcp", "cv", {"command": "/bin/cv"})
        self.assertNotIn("error", res)
        with open(p) as f:
            self.assertIn("cv", json.load(f)["mcpServers"])
        res = configs.entry_op("remove", p, "mcp", "cv")
        with open(p) as f:
            self.assertEqual(json.load(f)["mcpServers"], {})


if __name__ == "__main__":
    unittest.main()
