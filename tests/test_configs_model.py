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
from unittest import mock

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
                      ("CWD_ROOTS", "HOME_ROOTS", "HOME_ROOT_HARNESS",
                       "BACKUP_DIR", "MANAGED_DIRS")}
        configs.CWD_ROOTS = [self.cwdroot]
        configs.HOME_ROOTS = [self.home, self.codexhome]
        # harness_for is a lookup, so a synthetic root DECLARES its harness.
        # These two used to get their answer from the directory names
        # `claude-home` / `codex-home` — i.e. from the guesser this lane
        # deleted. The dirs keep their readable names; the fact now comes from
        # the fixture, which is the only place that actually knows it.
        configs.HOME_ROOT_HARNESS = {os.path.realpath(self.home): "claude",
                                     os.path.realpath(self.codexhome): "codex"}
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

    # -- bounded JSON transform CAS -----------------------------------------
    def _owned_transform(self, data):
        data["helmOwned"] = True
        return data, {"owned": True}

    def _owned_verify(self, candidate, before, metadata):
        expected = dict(before, helmOwned=True)
        return metadata == {"owned": True} and candidate == expected

    def test_transform_passes_exact_read_revision_and_retries_two_conflicts(self):
        p = self._write(os.path.join(self.home, "settings.json"), '{"foreign0": 0}\n')
        real = configs.write_file
        seen = []

        def race(path, content, expected_revision=None):
            current = configs.read_file(path)
            seen.append((expected_revision, current["revision"]))
            if len(seen) < 3:
                data = json.loads(current["content"])
                data["foreign%d" % len(seen)] = len(seen)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                return {"error": "conflict", "code": "conflict"}
            return real(path, content, expected_revision=expected_revision)

        with mock.patch.object(configs, "write_file", side_effect=race):
            out = configs.transform_json_file(
                p, self._owned_transform, verify=self._owned_verify)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["attempts"], 3)
        self.assertTrue(all(expected == actual for expected, actual in seen))
        with open(p, encoding="utf-8") as f:
            got = json.load(f)
        self.assertEqual(got, {"foreign0": 0, "foreign1": 1,
                               "foreign2": 2, "helmOwned": True})

    def test_transform_stops_at_three_conflicts_and_keeps_latest_foreign_bytes(self):
        p = self._write(os.path.join(self.home, "settings.json"), '{}\n')
        calls = []

        def always_race(path, _content, expected_revision=None):
            current = configs.read_file(path)
            calls.append(expected_revision)
            data = json.loads(current["content"])
            data["foreign%d" % len(calls)] = len(calls)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, sort_keys=True)
            return {"error": "conflict", "code": "conflict"}

        with mock.patch.object(configs, "write_file", side_effect=always_race):
            out = configs.transform_json_file(
                p, self._owned_transform, verify=self._owned_verify)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "conflict")
        self.assertEqual(out["attempts"], 3)
        self.assertEqual(len(calls), 3)
        with open(p, encoding="utf-8") as f:
            got = json.load(f)
        self.assertEqual(got, {"foreign1": 1, "foreign2": 2, "foreign3": 3})
        self.assertNotIn("helmOwned", got)

    def test_transform_missing_file_create_race_rederives_from_foreign_file(self):
        p = os.path.join(self.home, "settings.json")
        real = configs.write_file
        calls = []

        def create_race(path, content, expected_revision=None):
            calls.append(expected_revision)
            if len(calls) == 1:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"foreign": "created"}, f)
                return {"error": "conflict", "code": "conflict"}
            return real(path, content, expected_revision=expected_revision)

        with mock.patch.object(configs, "write_file", side_effect=create_race):
            out = configs.transform_json_file(
                p, self._owned_transform, verify=self._owned_verify)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["attempts"], 2)
        with open(p, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"foreign": "created", "helmOwned": True})

    def test_transform_preserves_post_commit_foreign_write_and_retries(self):
        p = self._write(os.path.join(self.home, "settings.json"), '{"base": 1}\n')
        real = configs.write_file
        wrote = []

        def foreign_after(path, content, expected_revision=None):
            out = real(path, content, expected_revision=expected_revision)
            if out.get("ok") and not wrote:
                wrote.append(1)
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                data["foreignAfter"] = True
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
            return out

        with mock.patch.object(configs, "write_file", side_effect=foreign_after):
            out = configs.transform_json_file(
                p, self._owned_transform, verify=self._owned_verify)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["attempts"], 2)
        self.assertTrue(out["wrote"])
        with open(p, encoding="utf-8") as f:
            self.assertEqual(json.load(f),
                             {"base": 1, "helmOwned": True, "foreignAfter": True})

    def test_transform_non_conflict_failure_is_not_retried(self):
        p = self._write(os.path.join(self.home, "settings.json"), '{"base": 1}\n')
        failure = {"error": "disk full", "code": "stage"}
        with mock.patch.object(configs, "write_file", return_value=failure) as write:
            out = configs.transform_json_file(
                p, self._owned_transform, verify=self._owned_verify)
        self.assertFalse(out["ok"])
        self.assertEqual(out["attempts"], 1)
        self.assertEqual(write.call_count, 1)
        with open(p, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"base": 1})

    def test_transform_semantic_noop_preserves_exact_formatting(self):
        raw = '{\n    "helmOwned" : true\n}\n'
        p = self._write(os.path.join(self.home, "settings.json"), raw)
        with mock.patch.object(configs, "write_file") as write:
            out = configs.transform_json_file(
                p, self._owned_transform, verify=self._owned_verify)
        self.assertTrue(out["ok"])
        self.assertFalse(out["wrote"])
        self.assertEqual(out["before"], raw)
        self.assertEqual(out["after"], raw)
        self.assertFalse(write.called)

    def test_transform_keeps_committed_metadata_separate_from_intent_and_winner(self):
        cases = {
            "clean": (["add"], "add", 1),
            "precommit-update": (["update"], "update", 2),
            "postcommit-noop": (["add"], "ok", 2),
            "multiple-commits": (["add", "update"], "ok", 3),
            "competitor-installed": ([], "ok", 2),
            "dry": ([], "add", 1),
            "failed-write": ([], None, 1),
            "failed-after-commit": (["add"], None, 2),
        }
        for mode, (committed, winning, attempts) in cases.items():
            with self.subTest(mode=mode):
                p = self._write(os.path.join(self.home, "settings.json"), '{}\n')
                writes, successes = [], []
                real = configs.write_file

                def transform(data):
                    action = "add" if "owned" not in data else "ok" if data["owned"] else "update"
                    metadata = {"domain_action": action, "foreign": data.get("foreign", 0)}
                    data["owned"] = True
                    return data, metadata

                def race(path, content, expected_revision=None):
                    writes.append(expected_revision)
                    if mode in ("precommit-update", "competitor-installed") and len(writes) == 1:
                        self._write(path, json.dumps({"owned": mode == "competitor-installed", "foreign": 1}))
                        return {"code": "conflict", "error": "synthetic competitor"}
                    if mode == "failed-write" or (mode == "failed-after-commit" and len(writes) == 2):
                        return {"code": "write", "error": "synthetic failure"}
                    saved = real(path, content, expected_revision=expected_revision)
                    if saved.get("ok"):
                        successes.append(True)
                        if mode in ("postcommit-noop", "multiple-commits", "failed-after-commit"):
                            data = json.loads(content)
                            data["foreign"] = len(successes)
                            if mode in ("multiple-commits", "failed-after-commit") and len(successes) == 1:
                                data["owned"] = False
                            self._write(path, json.dumps(data))
                    return saved

                with mock.patch.object(configs, "write_file", side_effect=race):
                    out = configs.transform_json_file(p, transform, dry_run=mode == "dry")
                self.assertEqual(out["attempts"], attempts)
                self.assertEqual(len(successes), len(committed))
                if winning is None:
                    self.assertFalse(out["ok"])
                    self.assertEqual(out["action"], "fail")
                    self.assertNotIn("wrote", out)
                    self.assertNotIn("committed_metadata", out)
                    self.assertFalse(json.loads(configs.read_file(p)["content"]).get("owned", False))
                    continue
                self.assertTrue(out["ok"], out)
                self.assertEqual(out["initial_metadata"]["domain_action"], "add")
                self.assertEqual(out["metadata"]["domain_action"], winning)
                self.assertEqual([m["domain_action"] for m in out["committed_metadata"]], committed)
                self.assertEqual(out["wrote"], bool(committed))
                if mode == "multiple-commits":
                    self.assertEqual([m["foreign"] for m in out["committed_metadata"]], [0, 1])
                    self.assertEqual(out["metadata"]["foreign"], 2)
                if mode == "dry":
                    self.assertEqual(writes, [])
                    self.assertEqual(configs.read_file(p)["content"], '{}\n')

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

    def test_tree_refuses_outside_and_symlink_scan_roots(self):
        outside = os.path.join(self.tmp, "outside-tree")
        self._write(os.path.join(outside, "CLAUDE.md"), "# unrelated\n")
        denied = configs.tree(outside)
        self.assertEqual(denied["code"], "refused")
        self.assertEqual(denied["roots"], [])
        alias = os.path.join(self.cwdroot, "tree-alias")
        os.symlink(outside, alias)
        denied = configs.tree(alias)
        self.assertEqual(denied["code"], "refused")

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

    # -- command/rule discovery + adversarial file shapes --------------------
    def test_home_subdirs_are_enumerated_deduped_and_filtered(self):
        command = self._write(os.path.join(self.home, "commands", "sp ace-☃.md"), "# hi\n")
        rule = self._write(os.path.join(self.home, "rules", "owner.rules"), "allow\n", 0o400)
        self._write(os.path.join(self.home, "commands", ".hidden.md"), "secret\n")
        self._write(os.path.join(self.home, "commands", "state.json"), '{"token":"no"}')
        outside = self._write(os.path.join(self.tmp, "outside.md"), "outside\n")
        os.symlink(outside, os.path.join(self.home, "commands", "alias.md"))
        os.mkfifo(os.path.join(self.home, "rules", "device.rules"))
        alias = os.path.join(self.tmp, "home-alias")
        os.symlink(self.home, alias)
        rule_alias_home = os.path.join(self.tmp, "rule-alias-home")
        os.makedirs(rule_alias_home)
        os.symlink(os.path.join(self.home, "rules"), os.path.join(rule_alias_home, "rules"))
        configs.HOME_ROOTS = [self.home, alias, rule_alias_home]

        out = configs.homes_configs()
        self.assertEqual(len(out), 1, out)
        self.assertEqual(out[0]["path"], os.path.realpath(self.home))
        self.assertIn("id", out[0])
        rels = {f["rel"]: f for f in out[0]["files"]}
        self.assertIn("commands/sp ace-☃.md", rels)
        self.assertTrue(rels["commands/sp ace-☃.md"]["editable"])
        self.assertIn("rules/owner.rules", rels)
        self.assertFalse(rels["rules/owner.rules"]["editable"])
        self.assertIn("owner read-only", rels["rules/owner.rules"]["reason"])
        self.assertNotIn("commands/.hidden.md", rels)
        self.assertNotIn("commands/state.json", rels)
        self.assertNotIn("commands/alias.md", rels)
        self.assertNotIn("rules/device.rules", rels)
        self.assertEqual(rels["commands/sp ace-☃.md"]["path"], command)
        self.assertEqual(rels["rules/owner.rules"]["path"], rule)

    def test_non_utf8_large_symlink_directory_and_fifo_are_safe(self):
        bad = os.path.join(self.home, "commands", "bad.md")
        os.makedirs(os.path.dirname(bad), exist_ok=True)
        with open(bad, "wb") as f:
            f.write(b"\xff\xfe")
        got = configs.read_file(bad)
        self.assertEqual(got["code"], "encoding")
        self.assertEqual(got["content"], "")

        large = os.path.join(self.home, "commands", "large.md")
        with open(large, "wb") as f:
            f.truncate(configs._MAX_CONFIG_BYTES + 1)
        _, editable, reason = configs.classify_path(large)
        self.assertFalse(editable)
        self.assertIn("size limit", reason)
        self.assertEqual(configs.read_file(large)["code"], "too-large")

        outside = self._write(os.path.join(self.tmp, "outside-secret.md"), "do not leak\n")
        os.makedirs(os.path.join(self.home, "rules"), exist_ok=True)
        link = os.path.join(self.home, "commands", "link.md")
        os.symlink(outside, link)
        for p in (link, os.path.join(self.home, "commands", "dir.md"),
                  os.path.join(self.home, "rules", "pipe.rules")):
            if p.endswith("dir.md"):
                os.mkdir(p)
            elif p.endswith("pipe.rules"):
                os.mkfifo(p)
            res = configs.write_file(p, "replacement\n")
            self.assertEqual(res["code"], "refused", (p, res))
        with open(outside) as f:
            self.assertEqual(f.read(), "do not leak\n")

    def test_parent_symlink_escape_is_denied_without_path_leak(self):
        outside = os.path.join(self.tmp, "outside-home")
        os.makedirs(os.path.join(outside, "commands"))
        alias = os.path.join(self.home, "alias")
        os.symlink(outside, alias)
        p = os.path.join(alias, "commands", "escape.md")
        res = configs.write_file(p, "x\n")
        self.assertEqual(res["code"], "refused")
        self.assertNotIn(p, res["error"])
        denied = configs.read_file("/etc/unique-owner-secret")
        self.assertEqual(denied["path"], "")
        self.assertNotIn("/etc/unique-owner-secret", denied["error"])

    def test_stale_deleted_renamed_and_replaced_files_conflict(self):
        p = self._write(os.path.join(self.home, "commands", "race.md"), "one\n")
        rev = configs.read_file(p)["revision"]
        with open(p, "w") as f:
            f.write("external\n")
        res = configs.write_file(p, "mine\n", expected_revision=rev)
        self.assertEqual(res["code"], "conflict")
        with open(p) as f:
            self.assertEqual(f.read(), "external\n")

        rev = configs.read_file(p)["revision"]
        renamed = p + ".old"
        os.rename(p, renamed)
        res = configs.write_file(p, "mine\n", expected_revision=rev)
        self.assertEqual(res["code"], "conflict")
        self.assertFalse(os.path.exists(p), "a stale save must not recreate a renamed file")
        os.rename(renamed, p)

        rev = configs.read_file(p)["revision"]
        os.unlink(p)
        res = configs.write_file(p, "mine\n", expected_revision=rev)
        self.assertEqual(res["code"], "conflict")
        self.assertFalse(os.path.exists(p), "a stale save must not recreate a deleted file")

        d = os.path.join(self.home, "commands")
        p = self._write(os.path.join(d, "parent-race.md"), "before\n")
        rev = configs.read_file(p)["revision"]
        moved = d + ".moved"
        os.rename(d, moved)
        res = configs.write_file(p, "mine\n", expected_revision=rev)
        self.assertEqual(res["code"], "conflict")
        self.assertFalse(os.path.exists(d), "a stale save must not recreate a renamed parent")

    def test_write_preserves_bom_crlf_mode_and_ownership(self):
        p = os.path.join(self.home, "commands", "windows.md")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"\xef\xbb\xbfline one\r\nline two\r\n")
        os.chmod(p, 0o640)
        before = os.stat(p)
        got = configs.read_file(p)
        self.assertEqual((got["encoding"], got["newline"]), ("utf-8-sig", "crlf"))
        res = configs.write_file(p, "changed\nagain\n", expected_revision=got["revision"])
        self.assertNotIn("error", res, res)
        after = os.stat(p)
        self.assertEqual(stat.S_IMODE(after.st_mode), 0o640)
        self.assertEqual((after.st_uid, after.st_gid), (before.st_uid, before.st_gid))
        with open(p, "rb") as f:
            self.assertEqual(f.read(), b"\xef\xbb\xbfchanged\r\nagain\r\n")
        second = configs.write_file(p, "second\n", expected_revision=res["revision"])
        self.assertNotIn("error", second, second)
        restored = configs.restore(res["backup"])
        self.assertNotIn("error", restored, restored)
        with open(p, "rb") as f:
            self.assertEqual(f.read(), b"\xef\xbb\xbfline one\r\nline two\r\n")

    def test_final_exchange_race_is_detected_and_external_file_wins(self):
        p = self._write(os.path.join(self.home, "commands", "exchange.md"), "before\n")
        got = configs.read_file(p)
        real_rename = configs._renameat2
        fired = False

        def race(dfd, old, new, flags):
            nonlocal fired
            if flags == configs._RENAME_EXCHANGE and not fired:
                fired = True
                leaf = ".external-replacement"
                fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dfd)
                os.write(fd, b"external\n")
                os.close(fd)
                os.replace(leaf, new, src_dir_fd=dfd, dst_dir_fd=dfd)
            return real_rename(dfd, old, new, flags)

        with mock.patch.object(configs, "_renameat2", race):
            res = configs.write_file(p, "mine\n", expected_revision=got["revision"])
        self.assertEqual(res["code"], "conflict", res)
        with open(p) as f:
            self.assertEqual(f.read(), "external\n")

    def test_post_exchange_fsync_failure_rolls_back(self):
        p = self._write(os.path.join(self.home, "commands", "rollback.md"), "before\n")
        got = configs.read_file(p)
        real_rename, real_fsync = configs._renameat2, os.fsync
        armed = failed = False
        parent_id = (os.stat(os.path.dirname(p)).st_dev, os.stat(os.path.dirname(p)).st_ino)

        def rename(dfd, old, new, flags):
            nonlocal armed
            out = real_rename(dfd, old, new, flags)
            if flags == configs._RENAME_EXCHANGE and not armed:
                armed = True
            return out

        def fsync(fd):
            nonlocal failed
            st = os.fstat(fd)
            if armed and not failed and stat.S_ISDIR(st.st_mode) \
                    and (st.st_dev, st.st_ino) == parent_id:
                failed = True
                raise OSError("injected durability failure")
            return real_fsync(fd)

        with mock.patch.object(configs, "_renameat2", rename), mock.patch("os.fsync", fsync):
            res = configs.write_file(p, "mine\n", expected_revision=got["revision"])
        self.assertEqual(res["code"], "durability", res)
        with open(p) as f:
            self.assertEqual(f.read(), "before\n")

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
