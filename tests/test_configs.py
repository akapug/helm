#!/usr/bin/env python3
"""helm.configs + the web configs/skills surface — hermetic contract tests.

Env is planted BEFORE helm.configs is imported (its roots freeze at import,
behavior-preserved from sesh): HELM_CONFIG_ROOTS -> a tmp cwd tree, HELM_HOME
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
os.environ.pop("SESH_CONFIG_ROOTS", None)
os.environ.setdefault("HELM_HOME", os.path.join(_TMP, "helm-home"))

with open(os.path.join(_PROJ, ".mcp.json"), "w") as f:
    json.dump({"mcpServers": {"planted": {"command": "/bin/echo", "args": ["hi"]}}}, f)
with open(os.path.join(_PROJ, "CLAUDE.md"), "w") as f:
    f.write("# planted memory\n")
with open(os.path.join(_HOMEDIR, "settings.json"), "w") as f:
    json.dump({"model": "opus"}, f)

from helm import configs, skills, web  # noqa: E402


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
            headers={"Content-Type": "application/json"})
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


if __name__ == "__main__":
    unittest.main()
