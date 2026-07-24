"""helm.web configs-editor surface — hermetic contract tests (slice C).

The /api/configs/* + /api/physics-diff contracts over configs.py/physics.py,
plus the uniform mutation hardening: EVERY POST demands the per-process bearer
(web.MUTATION_TOKEN) and answers 403 without it.

Hermetic per the test_configs pattern, adapted for suite runs: configs.py
freezes its roots from env at import (another test module may have imported it
first), so this class patches configs.CWD_ROOTS / HOME_ROOTS / BACKUP_DIR
module attributes directly and restores them — the real ~/dev, ~/.claude and
~/.cache are never touched. The catalog seam is stubbed so the tree endpoint
never scans real transcripts.
"""
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

from helm import configs, transcripts, web  # noqa: E402


class TestWebConfigs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-webcfg-")
        j = lambda *p: os.path.join(cls.tmp, *p)
        cls.root = j("dev")
        cls.proj = j("dev", "proj")
        cls.homedir = j("claude-home")
        cls.sess_cwd = j("dev", "sesscwd")   # only reachable via the catalog seam
        for d in (cls.proj, cls.homedir, cls.sess_cwd):
            os.makedirs(d)
        cls.mcpjson = os.path.join(cls.proj, ".mcp.json")
        with open(cls.mcpjson, "w") as f:
            json.dump({"mcpServers": {"planted": {"command": "/bin/echo"}}}, f)
        cls.claudemd = os.path.join(cls.proj, "CLAUDE.md")
        with open(cls.claudemd, "w") as f:
            f.write("# planted memory v1\n")
        with open(os.path.join(cls.sess_cwd, "CLAUDE.md"), "w") as f:
            f.write("# live-session cwd\n")
        with open(os.path.join(cls.homedir, "settings.json"), "w") as f:
            json.dump({"model": "opus"}, f)
        os.makedirs(os.path.join(cls.homedir, "commands"))
        cls.command = os.path.join(cls.homedir, "commands", "owner.md")
        with open(cls.command, "w") as f:
            f.write("# owner command\n")
        # repoint the frozen module roots (import-order-proof) + backups
        cls._cfg = {k: getattr(configs, k) for k in
                    ("CWD_ROOTS", "HOME_ROOTS", "BACKUP_DIR")}
        configs.CWD_ROOTS = [cls.root]
        configs.HOME_ROOTS = [cls.homedir]
        configs.BACKUP_DIR = j("config-backups")
        # the tree endpoint folds catalog cwds in — stub the seam, never scan
        cls._get_catalog = transcripts.get_catalog
        transcripts.get_catalog = lambda refresh=False: {
            "rows": [{"cwd": cls.sess_cwd}], "stats": {}, "scanned_at": 0}
        cls.srv = web.make_server(0)  # ephemeral port
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)
        transcripts.get_catalog = cls._get_catalog
        for k, v in cls._cfg.items():
            setattr(configs, k, v)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- plumbing ----------------------------------------------------------
    def req(self, path, payload=None, token=True, raw=False):
        """(status, body) — 4xx/5xx returned, not raised. payload -> POST;
        token=False drops the bearer (the CSRF-shaped request)."""
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if data and token:
            headers["Authorization"] = "Bearer " + web.MUTATION_TOKEN
        r = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                body = resp.read()
                return resp.status, (body if raw else json.loads(body or b"null"))
        except urllib.error.HTTPError as e:
            with e:
                body = e.read()
                return e.code, (body if raw else json.loads(body or b"null"))

    def _tree_nodes(self, node, acc):
        acc.append(node)
        for c in node["children"]:
            self._tree_nodes(c, acc)
        return acc

    def revision(self, path):
        q = urllib.parse.urlencode({"path": path})
        status, data = self.req("/api/configs/file?" + q)
        self.assertEqual(status, 200, data)
        return data["revision"]

    # -- GET /api/configs/tree ---------------------------------------------
    def test_tree_shape_and_catalog_cwd_fold(self):
        status, d = self.req("/api/configs/tree")
        self.assertEqual(status, 200)
        for key in ("roots", "count", "config_roots", "home_roots"):
            self.assertIn(key, d, "/api/configs/tree shape: missing %s" % key)
        self.assertEqual(d["config_roots"], [os.path.realpath(self.root)])
        nodes = []
        for r in d["roots"]:
            self._tree_nodes(r, nodes)
        by_path = {n["path"]: n for n in nodes}
        proj = by_path.get(os.path.realpath(self.proj))
        self.assertIsNotNone(proj, "planted project dir must appear in the tree")
        self.assertEqual({f["rel"] for f in proj["files"]}, {".mcp.json", "CLAUDE.md"})
        for f in proj["files"]:
            for key in ("rel", "path", "type", "harness", "kind", "editable", "reason"):
                self.assertIn(key, f)
        # the stubbed catalog cwd is folded in even without a scan hit ancestor
        self.assertIn(os.path.realpath(self.sess_cwd), by_path)
        status, denied = self.req("/api/configs/tree?root=/etc")
        self.assertEqual(status, 400)
        self.assertEqual(denied["code"], "refused")
        self.assertNotIn("/etc", denied["error"])

    # -- GET /api/configs/homes --------------------------------------------
    def test_homes_lists_home_scope_files(self):
        status, d = self.req("/api/configs/homes")
        self.assertEqual(status, 200)
        self.assertIsInstance(d, list)
        hm = next((h for h in d if h["path"] == self.homedir), None)
        self.assertIsNotNone(hm, "planted home must appear")
        self.assertEqual(hm["provider"], "claude")
        rels = {f["rel"] for f in hm["files"]}
        self.assertIn("settings.json", rels)
        self.assertIn("commands/owner.md", rels)

    # -- GET /api/configs/file ---------------------------------------------
    def test_file_get_content_and_refusals(self):
        q = urllib.parse.urlencode({"path": self.claudemd})
        status, d = self.req("/api/configs/file?" + q)
        self.assertEqual(status, 200)
        self.assertIn("planted memory", d["content"])
        self.assertTrue(d["editable"])
        # contract: a refused read answers 200 + error field + empty content
        status, d = self.req("/api/configs/file?path=/etc/passwd")
        self.assertEqual(status, 200)
        self.assertIn("error", d)
        self.assertEqual(d["content"], "")
        status, d = self.req("/api/configs/file")
        self.assertEqual(status, 400)

    # -- GET /api/configs/resolve ------------------------------------------
    def test_resolve_cascade(self):
        q = urllib.parse.urlencode({"home": self.homedir, "cwd": self.proj})
        status, d = self.req("/api/configs/resolve?" + q)
        self.assertEqual(status, 200)
        self.assertEqual(d["harness"], "claude")
        self.assertIn("planted", [m["name"] for m in d["mcpServers"]])
        self.assertIn(os.path.abspath(self.claudemd),
                      d["memory"]["projectClaudeMdChain"])
        status, d = self.req("/api/configs/resolve")
        self.assertEqual(status, 400)
        self.assertIn("error", d)

    # -- POST /api/configs/file (token + backup + refusals) ----------------
    def test_file_post_requires_token(self):
        status, d = self.req("/api/configs/file",
                             {"path": self.claudemd, "content": "# evil\n"},
                             token=False)
        self.assertEqual(status, 403)
        self.assertIn("error", d)
        with open(self.claudemd) as f:
            self.assertIn("planted memory", f.read(), "a 403 must not write")

    def test_file_post_writes_with_backup_then_restores(self):
        status, d = self.req("/api/configs/file",
                             {"path": self.claudemd, "content": "# edited v2\n",
                              "revision": self.revision(self.claudemd)})
        self.assertEqual(status, 200, d)
        self.assertTrue(d["ok"])
        self.assertTrue(d["backup"].startswith(configs.BACKUP_DIR))
        with open(self.claudemd) as f:
            self.assertEqual(f.read(), "# edited v2\n")
        # backups list carries the origin; restore round-trips to v1
        status, bs = self.req("/api/configs/backups")
        self.assertEqual(status, 200)
        mine = [b for b in bs if b["orig"] == os.path.realpath(self.claudemd)]
        self.assertTrue(mine, "backup of the pre-edit file must be listed")
        for key in ("backup", "orig", "size", "at"):
            self.assertIn(key, mine[0])
        status, d = self.req("/api/configs/restore", {"backup": mine[0]["backup"]})
        self.assertEqual(status, 200, d)
        with open(self.claudemd) as f:
            self.assertIn("planted memory", f.read())

    def test_file_post_requires_revision_and_returns_conflict(self):
        status, d = self.req("/api/configs/file",
                             {"path": self.command, "content": "# blind overwrite\n"})
        self.assertEqual(status, 400)
        self.assertEqual(d["code"], "revision")
        rev = self.revision(self.command)
        with open(self.command, "w") as f:
            f.write("# concurrent owner edit\n")
        status, d = self.req("/api/configs/file",
                             {"path": self.command, "content": "# stale UI edit\n",
                              "revision": rev})
        self.assertEqual(status, 409, d)
        self.assertEqual(d["code"], "conflict")
        with open(self.command) as f:
            self.assertEqual(f.read(), "# concurrent owner edit\n")

    def test_file_post_refuses_bad_paths_and_bad_json(self):
        status, d = self.req("/api/configs/file",
                             {"path": "/etc/passwd", "content": "x"})
        self.assertEqual(status, 400)
        self.assertIn("error", d)
        status, d = self.req("/api/configs/file",
                             {"path": self.mcpjson, "content": "{not json",
                              "revision": self.revision(self.mcpjson)})
        self.assertEqual(status, 400)
        self.assertIn("invalid JSON", d["error"])
        with open(self.mcpjson) as f:
            self.assertIn("planted", f.read(), "a refused write must not land")

    # -- POST /api/configs/entry -------------------------------------------
    def test_entry_op_adds_mcp_server(self):
        status, d = self.req("/api/configs/entry",
                             {"action": "add", "path": self.mcpjson, "kind": "mcp",
                              "name": "added", "value": {"command": "/bin/true"}})
        self.assertEqual(status, 200, d)
        with open(self.mcpjson) as f:
            servers = json.load(f)["mcpServers"]
        self.assertIn("added", servers)
        self.assertIn("planted", servers)
        status, d = self.req("/api/configs/entry",
                             {"action": "add", "path": self.mcpjson,
                              "kind": "hooks", "name": "x"})
        self.assertEqual(status, 400)
        status, d = self.req("/api/configs/entry",
                             {"action": "remove", "path": self.mcpjson,
                              "kind": "mcp", "name": "added"})
        self.assertEqual(status, 200, d)
        with open(self.mcpjson) as f:
            self.assertNotIn("added", json.load(f)["mcpServers"])

    def test_restore_refuses_unknown_backup(self):
        status, d = self.req("/api/configs/restore", {"backup": "/etc/passwd"})
        self.assertEqual(status, 400)
        self.assertIn("error", d)

    # -- physics + physics-diff --------------------------------------------
    def test_physics_and_diff(self):
        status, d = self.req("/api/physics?home=" +
                             urllib.parse.quote(self.homedir))
        self.assertEqual(status, 200)
        self.assertEqual(d["harness"], "claude")
        q = urllib.parse.urlencode({"a": self.homedir, "b": self.homedir})
        status, d = self.req("/api/physics-diff?" + q)
        self.assertEqual(status, 200)
        for key in ("harness", "a", "b", "added_in_b", "removed_in_b",
                    "changed", "identical"):
            self.assertIn(key, d, "physics_diff shape: missing %s" % key)
        self.assertTrue(d["identical"], "a home diffed against itself: %r" % d)
        status, d = self.req("/api/physics-diff?a=" +
                             urllib.parse.quote(self.homedir))
        self.assertEqual(status, 400)
        self.assertIn("error", d)

    # -- uniform mutation hardening ----------------------------------------
    def test_every_post_endpoint_403s_without_token(self):
        for ep in sorted(web.POST_API):
            status, d = self.req(ep, {}, token=False)
            self.assertEqual(status, 403, (ep, d))
            self.assertIn("error", d)
        # a wrong token is as good as none
        url = "http://127.0.0.1:%d/api/configs/file" % self.port
        r = urllib.request.Request(url, data=b"{}", headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer not-the-token"})
        try:
            with urllib.request.urlopen(r, timeout=10):
                self.fail("wrong token must not pass")
        except urllib.error.HTTPError as e:
            with e:
                self.assertEqual(e.code, 403)

    # -- the UI carries the view + the token -------------------------------
    def test_ui_configs_markup_and_token_injection(self):
        status, body = self.req("/", raw=True)
        self.assertEqual(status, 200)
        body = body.decode("utf-8")
        for marker in ('id="cfgbar"', 'id="cfgFilter"', 'data-ch="claude"',
                       'data-ch="codex"', 'id="cfgReload"', 'id="cfgsplit"',
                       'id="cfgtree"', 'id="cfgdetail"'):
            self.assertIn(marker, body, "configs view markup missing: %s" % marker)
        for fn in ("cfgInit", "cfgSelectCwd", "cfgResolve", "cfgSelectHome",
                   "cfgEdit", "cfgAddMcp", "cfgLoadBackups", "cfgPhysDiff"):
            self.assertIn("function " + fn, body, "configs JS missing: %s" % fn)
        self.assertNotIn("__HELM_TOKEN__", body,
                         "the token marker must be substituted at serve time")
        self.assertIn(web.MUTATION_TOKEN, body)
        self.assertNotIn("slice C", body, "the placeholder is gone")


if __name__ == "__main__":
    unittest.main()
