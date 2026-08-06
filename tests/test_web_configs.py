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
        # The prefix DELIBERATELY contains "pi" and "codex". mkdtemp's random
        # suffix used to supply those letters by chance (~0.5% of runs), and a
        # naked `"pi" in home_path` then resolved this claude home as "pi" and
        # RED THE WHOLE SUITE — intermittent, so it never got pinned. Baking
        # the spoof into the fixture makes that bug fail 100% of runs instead
        # of 1 in 200: reintroduce a substring sniff and test_resolve_cascade
        # goes red immediately. Nothing else here depends on the prefix text.
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-webcfg-pi-codex-")
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
                    ("CWD_ROOTS", "HOME_ROOTS", "HOME_ROOT_HARNESS", "BACKUP_DIR")}
        configs.CWD_ROOTS = [cls.root]
        configs.HOME_ROOTS = [cls.homedir]
        # The fixture now DECLARES its harness instead of spelling it into a
        # tmpdir name and letting harness_for guess. That inversion is the
        # point of this lane: a synthetic home is claude because the test says
        # so, not because "claude-home" pattern-matched. A fixture that proved
        # the guesser worked was proving the wrong thing — and proved it
        # WRONGLY 0.497% of the time, when mkdtemp happened to emit a suffix
        # containing "pi" or "codex".
        configs.HOME_ROOT_HARNESS = {os.path.realpath(cls.homedir): "claude"}
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

    def test_resolve_REFUSES_an_unplaceable_harness_instead_of_500ing(self):
        """physics_report RAISES on an unknown harness, and this endpoint fed
        it two unvalidated values on an UNAUTHENTICATED GET.

        `harness=` was a raw query param nobody checked — ?harness=bogus was
        already a 500 before this lane existed. And harness_for used to answer
        "claude" for anything, so the None it now returns for an unplaceable
        home would have been a second route to the same crash. One screen
        against configs.HARNESSES closes both; both directions are pinned here
        because only the first was a pre-existing bug.
        """
        for qs, why in ((
                {"home": self.homedir, "harness": "bogus"}, "bogus harness="),
                ({"home": self.root}, "a home no tagged root places")):
            status, d = self.req("/api/configs/resolve?" + urllib.parse.urlencode(qs))
            self.assertEqual(status, 400, why)
            self.assertEqual(d.get("code"), "refused", why)
            self.assertIn("harness", d.get("error", ""), why)

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

    # -- the FOURTH copy of the substring guess (cross-family review) --------
    def _tag_home(self, rel, harness):
        """Plant a home under the class tmp and TAG it, restoring on teardown.

        The roots are CLASS-level fixture state here, so a test that appends
        without restoring leaks extra homes into every sibling test in this
        class — homes_configs would scan them and list-shape assertions would
        drift for reasons no one could see from the failing test.
        """
        if not hasattr(self, "_roots_saved"):
            self._roots_saved = True
            roots, table = configs.HOME_ROOTS, configs.HOME_ROOT_HARNESS
            self.addCleanup(setattr, configs, "HOME_ROOTS", roots)
            self.addCleanup(setattr, configs, "HOME_ROOT_HARNESS", table)
        home = os.path.join(self.tmp, "phys-est", rel)
        os.makedirs(home, exist_ok=True)
        configs.HOME_ROOT_HARNESS = dict(
            configs.HOME_ROOT_HARNESS, **{os.path.realpath(home): harness})
        configs.HOME_ROOTS = configs.HOME_ROOTS + [home]
        return home

    def test_physics_route_uses_the_LOOKUP_not_a_substring_guess(self):
        """/api/physics kept the ORIGINAL bug after three rounds fixed it elsewhere.

        The line was `"codex" if "/codex" in hp or "codex-homes" in hp else
        "claude"`, and web_ui.html calls this route with NO harness=, so the
        guess always decided — on the owner's own physics button. It had only
        TWO outcomes, so a pi home could never be answered at all.

        These are the review's exact repros. They are pinned against the
        ENDPOINT rather than harness_for, because harness_for was already
        correct at that tip and the endpoint was still wrong — a unit test on
        the helper would have stayed green through the whole bug.
        """
        for rel, harness, want in ((".claude", "claude", "claude"),
                                   (".codex", "codex", "codex"),
                                   (".pi/agent", "pi", "pi")):
            home = self._tag_home(rel, harness)
            status, d = self.req("/api/physics?home=" + urllib.parse.quote(home))
            self.assertEqual(status, 200, rel)
            self.assertEqual(d["harness"], want,
                             "%s must resolve %s, not a substring guess" % (rel, want))

    def test_physics_routes_REFUSE_a_bogus_harness_instead_of_500ing(self):
        """Both routes fed physics unvalidated values on an unauthenticated GET.

        physics_report and physics_diff RAISE on an unknown harness, so
        ?harness=bogus was an HTTP 500 on each — a pre-existing hole this lane
        closes with the same configs.HARNESSES screen used everywhere else.
        """
        for path, extra in (("/api/physics", {"home": self.homedir}),
                            ("/api/physics-diff",
                             {"a": self.homedir, "b": self.homedir})):
            qs = dict(extra, harness="bogus")
            status, d = self.req(path + "?" + urllib.parse.urlencode(qs))
            self.assertEqual(status, 400, path)
            self.assertEqual(d.get("code"), "refused", path)

    def test_physics_diff_REFUSES_a_pair_it_cannot_derive_ONE_harness_for(self):
        """`or "claude"` was not a default — it was a silent claim about BOTH homes.

        A diff takes one harness for two homes, so a codex/pi pair was answered
        as confidently as a matched one. Deriving is honest only when both
        agree; a mismatch is a question for the caller, not this layer.
        """
        cx = self._tag_home("diff-codex/.codex", "codex")
        pi = self._tag_home("diff-pi/.pi/agent", "pi")
        status, d = self.req("/api/physics-diff?" +
                             urllib.parse.urlencode({"a": cx, "b": pi}))
        self.assertEqual(status, 400, "a codex/pi pair has no single harness")
        self.assertEqual(d.get("code"), "refused")
        # …and a MATCHED pair still derives without the caller saying so.
        status, d = self.req("/api/physics-diff?" +
                             urllib.parse.urlencode({"a": cx, "b": cx}))
        self.assertEqual(status, 200)
        self.assertEqual(d["harness"], "codex")

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


class TestHarnessForTaggedLookup(unittest.TestCase):
    """harness_for is a LOOKUP against tagged roots — these tests changed with it.

    THE OLD TESTS ASSERTED A SPELLING RULE and that is why there were three
    rounds of them. Each round pinned the cases the author had thought of, went
    green, and a reviewer then produced a shape outside the enumeration:

      1. `"pi" in hp` over the whole path — a mkdtemp suffix decided the
         answer, reddening the FULL SUITE at a measured 0.497% of runs.
      2. a dotted-component rule that borrowed the leaf matcher — an unrelated
         dotted ANCESTOR captured it (`/tmp/.pi-cache/u/.claude` -> pi).
      3. the leaf matcher stripping a leading dot — whatever the exact dotted
         pass REFUSED walked back in through the fallback.

    A green suite certified all three (4916 passed, 0 failed on rule 2). It had
    to: every case tested was a shape the author chose, and the bug was always
    a shape they had not. So the fix was not a fourth rule — it was deleting
    the guess. HOME_ROOTS is built by globs that each KNOW their harness, and
    the harness is now carried from there instead of re-derived from spelling.

    What these tests assert therefore inverts. The adversarial paths above no
    longer resolve CORRECTLY — they resolve to None, an honest refusal, which
    is the only answer a lookup can give for a path nobody tagged. The class
    of bug is gone because the wrong answers became UNREPRESENTABLE, not
    because a cleverer matcher finally covered them.
    """

    def setUp(self):
        # the mkdtemp prefix still carries "pi" and "codex" on purpose: under
        # the old rules that suffix was load-bearing, and it must now be inert.
        self.tmp = tempfile.mkdtemp(prefix="helm-test-harness-pi-codex-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for rel in (".claude", ".codex", ".pi/agent",
                    ".claude-homes/acct", ".codex-homes/acct", ".pi-homes/acct",
                    ".codex-homes/claude", ".claude-homes/codex",
                    ".helm/_global/seats/gemini/claude",
                    ".helm/_global/seats/grok/pi"):
            os.makedirs(os.path.join(self.tmp, rel))
        for k in ("HOME", "_HELM_HOME", "HOME_ROOTS", "HOME_ROOT_HARNESS"):
            self.addCleanup(setattr, configs, k, getattr(configs, k))
        configs.HOME = self.tmp
        configs._HELM_HOME = os.path.join(self.tmp, ".helm")
        self.rebuild()

    def rebuild(self):
        """Repoint the tables THROUGH THE REAL BUILDER, never a hand-rolled dict.

        The first version of this fixture assembled an equivalent
        {realpath: harness} comprehension itself, and a mutation replacing the
        module's realpath keying with raw paths SURVIVED — the test had quietly
        substituted its own correct code for the code under test. Calling the
        builder is what puts _typed_home_roots AND its keying under test.
        """
        configs.HOME_ROOTS, configs.HOME_ROOT_HARNESS = \
            configs._common._home_root_tables()

    def j(self, *rel):
        return os.path.join(self.tmp, *rel)

    # -- MUST-HIT: the glob that minted a root is the one that answers for it --
    def test_every_tagged_root_resolves_to_the_glob_that_minted_it(self):
        for rel, want in ((".claude", "claude"), (".codex", "codex"),
                          (".pi/agent", "pi"),
                          (".claude-homes/acct", "claude"),
                          (".codex-homes/acct", "codex"),
                          (".pi-homes/acct", "pi"),
                          (".helm/_global/seats/gemini/claude", "claude"),
                          (".helm/_global/seats/grok/pi", "pi")):
            self.assertEqual(configs.harness_for(self.j(rel)), want, rel)

    def test_an_account_named_after_another_harness_takes_its_ROOTS_harness(self):
        """The property every previous round had to fight for, now free.

        An account directory may be named for a different harness than the home
        containing it. Under a spelling rule this needed an outranking pass and
        a scan direction; under a lookup the account name is never consulted at
        all, because ~/.codex-homes/claude was tagged `codex` by the glob.
        """
        self.assertEqual(configs.harness_for(self.j(".codex-homes/claude")), "codex")
        self.assertEqual(configs.harness_for(self.j(".claude-homes/codex")), "claude")

    def test_a_SYMLINKED_home_resolves_through_to_its_real_root(self):
        """The table is keyed by REALPATH, matching every gate that compares
        against HOME_ROOTS — a symlinked home must not read as unknown."""
        link = self.j("alias-home")
        os.symlink(self.j(".codex"), link)
        self.assertEqual(configs.harness_for(link), "codex")

    def test_a_root_that_IS_a_symlink_resolves_by_the_path_callers_ask_with(self):
        """The direction the previous test cannot reach, and the one that bites.

        Above, the LOOKUP argument is a symlink and the table entry is real, so
        raw-vs-realpath keying makes no difference. Here the ROOT ITSELF is the
        symlink — which is what homes_configs hands over, because it resolves
        `h = _real(raw)` before every call. A raw-keyed table holds the link
        path and is asked for the real one, so it misses, and the home silently
        loses its provider. Caught by a mutation that survived the test above.
        """
        real = os.path.join(self.tmp, "outside-the-estate")
        os.makedirs(real)
        link = self.j(".codex-homes", "linked-acct")
        os.symlink(real, link)
        self.rebuild()
        self.assertIn(link, configs.HOME_ROOTS, "the glob yields the SYMLINK path")
        self.assertEqual(configs.harness_for(os.path.realpath(link)), "codex")

    # -- the two shapes minted AFTER import (HOME_ROOTS globs exactly once) ----
    def test_an_account_home_minted_AFTER_import_still_resolves(self):
        """`helm homes prepare claude <email>` mints a home in the same process that then reads
        configs, so the import-time table cannot contain it. This is the same
        dynamic shape _resolve._allowed_home admits, computed per call."""
        fresh = self.j(".codex-homes", "minted-later")
        os.makedirs(fresh)
        self.assertNotIn(os.path.realpath(fresh), configs.HOME_ROOT_HARNESS)
        self.assertEqual(configs.harness_for(fresh), "codex")

    def test_a_seat_home_minted_AFTER_import_still_resolves(self):
        for rel, want in (("newfam/claude", "claude"), ("newfam/pi", "pi"),
                          ("codex/instances/codex-2/claude", "claude")):
            fresh = self.j(".helm/_global/seats", rel)
            os.makedirs(fresh)
            self.assertNotIn(os.path.realpath(fresh), configs.HOME_ROOT_HARNESS)
            self.assertEqual(configs.harness_for(fresh), want, rel)

    def test_only_a_DIRECT_child_of_an_account_root_resolves(self):
        """Why a generic nearest-enclosing lookup was refused in review.

        Nearest-enclosing would answer `codex` for anything under
        ~/.codex-homes at any depth. _allowed_home admits a DIRECT child and
        nothing deeper, so provenance must refuse exactly where authorization
        refuses — otherwise the two gates disagree about the same path.
        """
        deep = self.j(".codex-homes", "acct", "nested")
        os.makedirs(deep)
        self.assertIsNone(configs.harness_for(deep))

    def test_a_dir_merely_NAMED_claude_is_not_a_seat_home(self):
        """The seat rule reads a TAGGED root, not a terminal component.

        The leaf only answers when the WHOLE enclosing shape is the layout
        `helm seat add` writes. Drop that check and a directory called `claude`
        anywhere on the box becomes a claude home.
        """
        for rel in ("notseats/gemini/claude", ".helm/_global/claude",
                    ".helm/_global/seats/claude",
                    ".helm/_global/seats/fam/wrong/claude",
                    ".helm/_global/seats/fam/instances/s/deep/claude"):
            p = self.j(rel)
            os.makedirs(p, exist_ok=True)
            self.assertIsNone(configs.harness_for(p), rel)

    # -- MUST-NOT-HIT: every adversary of the three dead rules now REFUSES ----
    def test_the_three_dead_rules_adversaries_now_REFUSE_instead_of_guessing(self):
        """Each of these produced a CONFIDENT WRONG ANSWER under some round.

        None of them is a home. The old code answered "claude" for anything it
        could not place, which is precisely why the wrong answers hid: a
        default that is right most of the time makes the cases where it is
        wrong indistinguishable from the cases where it is right.
        """
        for path in (
                # round 1 — a mkdtemp suffix decided it
                "/tmp/helm-test-webcfg-a1pi9z0q/claude-home",
                "/tmp/helm-test-webcfg-codexy77/claude-home",
                "/var/pipeline/claude-home",
                # round 2 — an unrelated dotted ANCESTOR captured it
                "/tmp/.pi-cache/u/.claude",
                "/tmp/.claude-scratch/x/.pi/agent",
                "/tmp/.codex-tmp/u/.claude",
                # round 3 — the leaf matcher stripped the dot pass 1 refused
                "/tmp/x/.pi-cache",
                "/tmp/x/.codex-tmp",
                "/tmp/x/.claude-backup",
                # never homes under any rule
                "/tmp/x/codex-home", "/tmp/nothing/here", "/etc", ""):
            self.assertIsNone(configs.harness_for(path), path)

    def test_an_empty_path_refuses_even_when_the_PROCESS_CWD_is_a_home(self):
        """realpath("") is the PROCESS CWD, so the guard is not decoration.

        Without it, `home=` with no value resolves to whatever directory the
        server happens to be running in — and helm's own web server can be
        started from anywhere, including inside a config home. The empty case
        only LOOKED covered before: a mutation removing the guard survived,
        because the test process's cwd was the repo, which is not a home. The
        chdir is what makes the mutation bite.
        """
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.j(".codex"))
        self.assertIsNone(configs.harness_for(""))
        self.assertIsNone(configs.harness_for(None))

    def test_a_REAL_home_shape_outside_the_patched_estate_still_refuses(self):
        """The lookup is against THIS estate's tagged roots, not a shape family.

        `/home/u/.claude` is spelled exactly like a real claude home and every
        dead rule answered `claude` for it. It belongs to no estate the table
        knows, so the honest answer is None — and any reintroduced spelling
        fallback lights this test up immediately.
        """
        for path in ("/home/u/.claude", "/home/u/.codex", "/home/u/.pi/agent",
                     "/home/u/.claude-homes/acct"):
            self.assertIsNone(configs.harness_for(path), path)


if __name__ == "__main__":
    unittest.main()
