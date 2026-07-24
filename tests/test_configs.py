#!/usr/bin/env python3
"""helm.configs + the web configs/skills surface — hermetic contract tests.

Env is planted BEFORE helm.configs is imported (its roots freeze at import,
behavior-preserved): HELM_CONFIG_ROOTS -> a tmp cwd tree, HELM_HOME
-> a tmp helm home. The skills census is pointed at a tmp skill home by
patching skills._skill_homes (the test_skills pattern) and the web trash dir
at a tmp dir — the real ~/.claude / ~/.helm / ~/.cache are never touched.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="helm-test-configs-")
_ROOT = os.path.join(_TMP, "dev")
_PROJ = os.path.join(_ROOT, "proj")
_HOMEDIR = os.path.join(_TMP, "claude-home")     # a synthetic cred home
_SKILLS = os.path.join(_TMP, "skills")           # a synthetic skill home
os.makedirs(_PROJ)
os.makedirs(_HOMEDIR)
os.makedirs(_SKILLS)
os.environ["HELM_CONFIG_ROOTS"] = _ROOT
os.environ.setdefault("HELM_HOME", os.path.join(_TMP, "helm-home"))

with open(os.path.join(_PROJ, ".mcp.json"), "w") as f:
    json.dump({"mcpServers": {"planted": {"command": "/bin/echo", "args": ["hi"]}}}, f)
with open(os.path.join(_PROJ, "CLAUDE.md"), "w") as f:
    f.write("# planted memory\n")
with open(os.path.join(_HOMEDIR, "settings.json"), "w") as f:
    json.dump({"model": "opus"}, f)

from helm import configs, skills, web  # noqa: E402

# the synthetic cred home must be on the resolve allowlist (resolve refuses
# homes outside HOME_ROOTS — the audit's arbitrary-directory read gate)
configs.HOME_ROOTS.append(_HOMEDIR)


def _mk_skill(name, content="skill body"):
    d = os.path.join(_SKILLS, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w") as f:
        f.write(content)
    return d


_ALPHA = _mk_skill("alpha")
_BETA = _mk_skill("beta")


def _tree_nodes(node, acc):
    acc.append(node)
    for c in node["children"]:
        _tree_nodes(c, acc)
    return acc


class ConfigsModelTest(unittest.TestCase):
    def test_list_finds_planted_files(self):
        t = configs.tree()
        nodes = []
        for r in t["roots"]:
            _tree_nodes(r, nodes)
        proj = next((n for n in nodes if n["path"] == os.path.realpath(_PROJ)), None)
        self.assertIsNotNone(proj, "planted project dir must appear in the tree")
        rels = {f["rel"] for f in proj["files"]}
        self.assertIn(".mcp.json", rels)
        self.assertIn("CLAUDE.md", rels)

    def test_cascade_resolves(self):
        r = configs.resolve(_HOMEDIR, _PROJ, "claude")
        self.assertEqual(r["harness"], "claude")
        self.assertIn("planted", [m["name"] for m in r["mcpServers"]])
        self.assertIn(os.path.join(os.path.abspath(_PROJ), "CLAUDE.md"),
                      r["memory"]["projectClaudeMdChain"])
        self.assertIn("user", [l["layer"] for l in r["settingsLayers"]])

    def test_read_refuses_unrecognized(self):
        r = configs.read_file("/etc/passwd")
        self.assertIn("error", r)
        self.assertEqual(r["content"], "")

    def test_resolve_refuses_unlisted_home(self):
        # audit: resolve fed an arbitrary ?home= dir into physics_report,
        # returning that directory's settings/hooks/mcpServers on an
        # unauthenticated GET. Only recognized cred homes resolve.
        outside = os.path.join(_TMP, "not-a-home")
        os.makedirs(outside, exist_ok=True)
        with open(os.path.join(outside, "settings.json"), "w") as f:
            json.dump({"env": {"SECRET_NAME": "x"}}, f)
        r = configs.resolve(outside, _PROJ, "claude")
        self.assertEqual(r["code"], "refused")
        self.assertIn("not a recognized cred home", r["error"])
        self.assertNotIn(outside, r["error"])
        # the CLI surfaces the refusal as an error exit
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                configs.cmd_configs(["cascade", _PROJ, "--home", outside]), 1)

    def test_cmd_configs(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(configs.cmd_configs(["list"]), 0)
        self.assertIn(".mcp.json", out.getvalue())
        self.assertIn(os.path.realpath(_PROJ), out.getvalue())

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(
                configs.cmd_configs(["cascade", _PROJ, "--home", _HOMEDIR]), 0)
        cas = json.loads(out.getvalue())
        self.assertEqual(cas["harness"], "claude")
        self.assertIn("planted", [m["name"] for m in cas["mcpServers"]])

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(
                configs.cmd_configs(["show", os.path.join(_PROJ, "CLAUDE.md")]), 0)
        self.assertIn("# planted memory", out.getvalue())

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(configs.cmd_configs(["show", "/etc/passwd"]), 1)
            self.assertEqual(configs.cmd_configs(["bogus"]), 2)
            self.assertEqual(configs.cmd_configs(["cascade"]), 2)


class WriteModeTest(unittest.TestCase):
    """A cross-family review found: write_file replaced a 0600 settings.json with a
    0644 inode — an existing file must keep its exact mode; a new cred-home
    file is born owner-only."""

    def test_existing_mode_preserved_exactly(self):
        import stat as _st
        os.makedirs(_HOMEDIR, exist_ok=True)
        p = os.path.join(_HOMEDIR, "settings.json")
        with open(p, "w") as f:
            f.write("{}")
        os.chmod(p, 0o600)
        res = configs.write_file(p, '{"a": 1}')
        self.assertNotIn("error", res)
        self.assertEqual(_st.S_IMODE(os.stat(p).st_mode), 0o600)

    def test_new_cred_file_is_owner_only(self):
        import stat as _st
        os.makedirs(_HOMEDIR, exist_ok=True)
        p = os.path.join(_HOMEDIR, ".claude.json")
        if os.path.exists(p):
            os.unlink(p)
        res = configs.write_file(p, "{}")
        self.assertNotIn("error", res)
        self.assertEqual(_st.S_IMODE(os.stat(p).st_mode), 0o600)


class ConfigsWebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_homes = skills._skill_homes
        skills._skill_homes = lambda: ([_SKILLS], [])
        cls._orig_trash = web.TRASH_DIR
        web.TRASH_DIR = os.path.join(_TMP, "skills-trash")
        cls.srv = web.make_server(0)  # ephemeral port
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)
        skills._skill_homes = cls._orig_homes
        web.TRASH_DIR = cls._orig_trash
        shutil.rmtree(_TMP, ignore_errors=True)

    def get(self, path):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def post(self, path, obj):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        req = urllib.request.Request(
            url, data=json.dumps(obj).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + web.MUTATION_TOKEN})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_api_configs_lists_planted(self):
        status, d = self.get("/api/configs")
        self.assertEqual(status, 200)
        self.assertNotIn("unavailable", d)
        nodes = []
        for r in d["tree"]["roots"]:
            _tree_nodes(r, nodes)
        proj = next((n for n in nodes if n["path"] == os.path.realpath(_PROJ)), None)
        self.assertIsNotNone(proj)
        self.assertEqual({f["rel"] for f in proj["files"]}, {".mcp.json", "CLAUDE.md"})

    def test_api_configs_cascade(self):
        q = urllib.parse.urlencode({"cwd": _PROJ, "home": _HOMEDIR})
        status, d = self.get("/api/configs/cascade?" + q)
        self.assertEqual(status, 200)
        self.assertEqual(d["harness"], "claude")
        self.assertIn("planted", [m["name"] for m in d["mcpServers"]])
        status, d = self.get("/api/configs/cascade?harness=bogus")
        self.assertEqual(status, 400)
        self.assertIn("error", d)

    def test_api_cascade_refuses_unlisted_home(self):
        outside = os.path.join(_TMP, "not-a-home-web")
        os.makedirs(outside, exist_ok=True)
        q = urllib.parse.urlencode({"cwd": _PROJ, "home": outside})
        status, d = self.get("/api/configs/cascade?" + q)
        self.assertIn("error", d)
        self.assertIn("not a recognized cred home", d["error"])
        self.assertNotIn("mcpServers", d)

    def test_api_skills_census(self):
        status, d = self.get("/api/skills")
        self.assertEqual(status, 200)
        by_name = {s["name"]: s for s in d["skills"]}
        self.assertIn("alpha", by_name)
        self.assertTrue(by_name["alpha"]["has_manifest"])
        self.assertEqual(by_name["alpha"]["path"], _ALPHA)
        self.assertFalse(by_name["alpha"]["shadowed"])

    def test_skills_toggle_renames_and_restores(self):
        status, d = self.post("/api/skills/toggle", {"path": _ALPHA})
        self.assertEqual(status, 200, d)
        self.assertEqual(d["state"], "disabled")
        self.assertFalse(os.path.isdir(_ALPHA))
        self.assertTrue(os.path.isdir(_ALPHA + ".disabled"))
        _, sk = self.get("/api/skills")
        alpha = next(s for s in sk["skills"] if s["name"] == "alpha")
        self.assertTrue(alpha["disabled"])
        status, d = self.post("/api/skills/toggle", {"path": _ALPHA + ".disabled"})
        self.assertEqual(status, 200, d)
        self.assertEqual(d["state"], "enabled")
        self.assertTrue(os.path.isdir(_ALPHA))
        self.assertTrue(os.path.isfile(os.path.join(_ALPHA, "SKILL.md")))

    def test_skills_delete_moves_to_trash(self):
        doomed = _mk_skill("zeta", "doomed but archived")
        status, d = self.post("/api/skills/delete", {"path": doomed})
        self.assertEqual(status, 200, d)
        self.assertFalse(os.path.exists(doomed))
        self.assertTrue(d["trash"].startswith(web.TRASH_DIR))
        self.assertTrue(d["trash"].endswith("-zeta"))
        with open(os.path.join(d["trash"], "SKILL.md")) as f:
            self.assertEqual(f.read(), "doomed but archived")

    def test_post_rejects_outside_or_bad_paths(self):
        for payload in ({"path": _PROJ},          # a real dir, but not a skill home
                        {"path": "/etc"},
                        {"path": os.path.join(_SKILLS, "nonexistent")},
                        {"path": 42}, {}):
            for ep in ("/api/skills/toggle", "/api/skills/delete"):
                status, d = self.post(ep, payload)
                self.assertEqual(status, 400, (ep, payload, d))
                self.assertIn("error", d)
        self.assertTrue(os.path.isdir(_BETA), "a rejected POST must not act")
        status, d = self.post("/api/skills/nope", {"path": _BETA})
        self.assertEqual(status, 404)


class HomeSubdirConfigTest(unittest.TestCase):
    """The configs UI showed most files as uneditable. The bulk of files
    under the config homes are correctly unrecognized (credential stores,
    .bak copies, runtime state, plugin metadata). What was left was
    declarative human-authored config living ONE LEVEL DOWN in a home, which
    the gate had no pattern for."""

    def setUp(self):
        for d in ("commands", "rules"):
            os.makedirs(os.path.join(_HOMEDIR, d), exist_ok=True)

    def path(self, rel):
        p = os.path.join(_HOMEDIR, rel)
        with open(p, "w") as f:
            f.write("body\n")
        return os.path.realpath(p)

    def test_commands_and_rules_are_now_recognized(self):
        self.assertTrue(configs._is_recognized_config(self.path("commands/afk.md")))
        self.assertTrue(configs._is_recognized_config(self.path("rules/default.rules")))

    def test_classify_path_reports_them_editable_end_to_end(self):
        """The gate is one layer; what the UI actually consumes is
        classify_path's (type, editable, reason). Assert THAT, or the fix is
        proven only where nobody reads it."""
        for rel, want in (("commands/afk.md", "md"),
                          ("rules/default.rules", "text")):
            typ, editable, why = configs.classify_path(self.path(rel))
            self.assertTrue(editable, "%s not editable: %s" % (rel, why))
            self.assertEqual(typ, want, rel)

    def test_read_file_actually_serves_the_content(self):
        """Recognized must mean READABLE too — the gate governs both."""
        p = self.path("commands/afk.md")
        got = configs.read_file(p)
        # read_file always CARRIES an 'error' key; success is error IS None.
        self.assertIsNone(got.get("error"), got)
        self.assertIn("body", got.get("content", ""))
        self.assertTrue(got.get("editable"))
        self.assertEqual(got.get("reason"), "ok")

    def test_a_round_trip_edit_lands_and_is_backed_up(self):
        """backup -> validate -> atomic, on the newly-recognized shape."""
        p = self.path("commands/afk.md")
        res = configs.write_file(p, "# edited by the owner\n")
        self.assertNotIn("error", res, res)
        with open(p) as f:
            self.assertEqual(f.read(), "# edited by the owner\n")

    def test_the_subdir_must_sit_in_a_REAL_home_root(self):
        """A commands/foo.md anywhere else on disk must still fail the gate —
        the pattern is not a licence to read arbitrary markdown."""
        stray = os.path.join(_TMP, "not-a-home", "commands")
        os.makedirs(stray, exist_ok=True)
        p = os.path.join(stray, "afk.md")
        with open(p, "w") as f:
            f.write("x\n")
        self.assertFalse(configs._is_recognized_config(os.path.realpath(p)))

    def test_only_the_named_shapes_qualify(self):
        """commands/*.md and rules/*.rules — not every file dropped in them."""
        self.assertFalse(configs._is_recognized_config(self.path("commands/run.sh")))
        self.assertFalse(configs._is_recognized_config(self.path("rules/notes.md")))

    def test_executables_in_a_home_stay_UNEDITABLE(self):
        """A security boundary, not an oversight: the web UI writes the file
        and the next hook invocation RUNS it as the owner. A JSON config can
        only misconfigure; a .sh hook can do anything."""
        for rel in ("statusline.sh", "codex-hook.py"):
            p = os.path.join(_HOMEDIR, rel)
            with open(p, "w") as f:
                f.write("#!/bin/sh\necho hi\n")
            self.assertFalse(configs._is_recognized_config(os.path.realpath(p)),
                             "%s must NOT be editable through the config layer" % rel)

    def test_credential_stores_still_denied_under_the_new_pattern(self):
        """The deny list outranks every recognizer, including this one."""
        d = os.path.join(_HOMEDIR, "commands")
        os.makedirs(d, exist_ok=True)
        for name in (".credentials.json", "auth.json"):
            p = os.path.join(d, name)
            with open(p, "w") as f:
                f.write("{}")
            self.assertFalse(configs._is_recognized_config(os.path.realpath(p)))

    def test_a_rules_file_validates_as_text_not_other(self):
        self.assertEqual(configs._ext_type("/x/rules/default.rules"), "text")
        ok, err = configs._validate("text", "anything at all\n")
        self.assertTrue(ok, err)


if __name__ == "__main__":
    unittest.main()
