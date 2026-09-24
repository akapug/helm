"""helm.web configs-editor surface — hermetic contract tests (slice C).

The predecessor tool's /api/configs/* + /api/physics-diff contracts over configs.py/physics.py,
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
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm.configs import _io  # noqa: E402
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
        # shutdown() waits one serve_forever poll (stdlib 0.5s)
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      kwargs={"poll_interval": 0.01}, daemon=True)
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

    def setUp(self):
        # configs.tree() is cached for a minute now; a test that plants a config
        # file and then GETs the tree must not be served the scan that ran
        # before it existed.
        configs.clear_tree_cache()
        self.addCleanup(configs.clear_tree_cache)

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
            self.assertIn(key, d, "predecessor /api/configs/tree shape: missing %s" % key)
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

    def test_tree_is_cached_and_the_rescan_button_bypasses_it(self):
        """The owner's hang: every GET walked the whole cwd root. Now the tab
        open takes the cache and only ↻ (which the page sends as ?rescan=1)
        pays for a walk."""
        with mock.patch.object(configs, "_find_config_dirs",
                               wraps=configs._find_config_dirs) as walk:
            status, first = self.req("/api/configs/tree")
            self.assertEqual(status, 200)
            self.assertEqual(walk.call_count, 1, "the spy must see the real walk")
            status, second = self.req("/api/configs/tree")
            self.assertEqual(status, 200)
            self.assertEqual(walk.call_count, 1, "a repeat GET re-walked the tree")
            self.assertEqual(first, second)
            status, rescanned = self.req("/api/configs/tree?rescan=1")
            self.assertEqual(status, 200)
            self.assertEqual(walk.call_count, 2, "?rescan=1 must re-walk")
            self.assertEqual(rescanned["count"], first["count"])
            # a refusal is a verdict about the ARGUMENT and is never stored
            status, denied = self.req("/api/configs/tree?root=/etc")
            self.assertEqual(status, 400)
            status, denied2 = self.req("/api/configs/tree?root=/etc")
            self.assertEqual(status, 400)
            self.assertEqual(denied2["code"], "refused")
            self.assertEqual(walk.call_count, 2, "a refusal must not reach the walk")

    def _paths(self, body):
        nodes = []
        for r in body["roots"]:
            self._tree_nodes(r, nodes)
        return {n["path"] for n in nodes}

    def _plant_behind_helms_back(self, name):
        """A new config dir written to DISK, never through helm's write doors —
        so only an invalidation of the WALK can put it on the next cached GET."""
        d = os.path.join(self.root, name)
        os.makedirs(d, exist_ok=True)
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with open(os.path.join(d, "CLAUDE.md"), "w") as f:
            f.write("# planted on disk, not through the API\n")
        return os.path.realpath(d)

    def _probe_spy(self):
        """Counts `_project_files_at` — one call per directory whose file rows
        the payload is being rebuilt from. Zero of them is a payload served
        from cache; more than zero is a payload rebuilt."""
        return mock.patch.object(configs, "_project_files_at",
                                 wraps=configs._project_files_at)

    def _walk_spy(self):
        return mock.patch.object(configs, "_find_config_dirs",
                                 wraps=configs._find_config_dirs)

    def test_a_save_to_a_listed_file_rebuilds_the_payload_and_keeps_the_walk(self):
        """A WRITE DROPS WHAT IT CAN HAVE CHANGED AND NOTHING MORE.

        Saving over a file the tree already lists cannot change WHICH
        directories hold configs, so it must not cost a walk of the owner's cwd
        root — doing that once per save is the hang this lane exists to cure,
        re-introduced one keystroke at a time. It must still cost a rebuild of
        the payload, which is what carries that file's row.

        The planted directory is the control for the walk half: it is on disk
        and the surface must still not show it, which is only true if the walk
        really was kept.
        """
        with open(self.claudemd) as f:
            before = f.read()
        def restore():
            with open(self.claudemd, "w") as f:
                f.write(before)
        self.addCleanup(restore)

        status, first = self.req("/api/configs/tree")
        self.assertEqual(status, 200)
        self.assertIn(os.path.realpath(self.proj), self._paths(first))
        planted = self._plant_behind_helms_back("behind-a-plain-save")
        with self._probe_spy() as probe:
            status, cached = self.req("/api/configs/tree")
            self.assertEqual(status, 200)
            self.assertEqual(probe.call_count, 0,
                             "this GET rebuilt, so the assertions below are about "
                             "nothing")
            # POSITIVE POLE on the same spy, unconditional: a GET that DOES
            # rebuild is counted, so the zero above is a measurement.
            configs.invalidate_built()
            status, _ = self.req("/api/configs/tree")
            self.assertEqual(status, 200)
            self.assertGreater(probe.call_count, 0, "the probe spy counted nothing")
        self.assertNotIn(planted, self._paths(cached))

        status, saved = self.req("/api/configs/file",
                                 {"path": self.claudemd, "content": "# saved v2\n",
                                  "revision": self.revision(self.claudemd)})
        self.assertEqual(status, 200, saved)
        self.assertFalse(saved["created"], "this fixture file already existed")
        with self._probe_spy() as probe, self._walk_spy() as walk:
            status, after = self.req("/api/configs/tree")
            self.assertEqual(status, 200)
            self.assertGreater(probe.call_count, 0,
                               "the save left the owner's tab on a payload built "
                               "before it")
            self.assertEqual(walk.call_count, 0,
                             "a save to an already-listed file re-walked the cwd root")
            # POSITIVE POLE on the walk spy, unconditional: ?rescan=1 is counted,
            # so the zero above is a measurement and not a spy that sees nothing.
            status, _ = self.req("/api/configs/tree?rescan=1")
            self.assertEqual(status, 200)
            self.assertEqual(walk.call_count, 1, "the walk spy counted nothing")
        self.assertNotIn(planted, self._paths(after),
                         "the walk was dropped by a write that cannot have changed it")

    def test_a_write_that_CREATES_a_file_drops_the_walk_as_well(self):
        """A create is the one write that can put a directory on the surface.

        Three doors write — file, entry, restore — and each one reaches
        `configs.write_file`, so each one reports `created`. Each is exercised
        against its own control: a directory planted on disk behind helm's back
        must be invisible to the GET before the write and visible to the one
        after, which is only possible if the cache was in front of the endpoint
        and the write dropped it.
        """
        with open(self.claudemd) as f:
            claudemd_before = f.read()
        with open(self.mcpjson) as f:
            mcpjson_before = f.read()

        def restore_fixture():
            with open(self.claudemd, "w") as f:
                f.write(claudemd_before)
            with open(self.mcpjson, "w") as f:
                f.write(mcpjson_before)
        self.addCleanup(restore_fixture)

        # the restore door needs a backup, and MAKING one is itself a write —
        # so it is made up front, before any of the controls below are taken.
        status, saved = self.req("/api/configs/file",
                                 {"path": self.claudemd, "content": "# edited for restore\n",
                                  "revision": self.revision(self.claudemd)})
        self.assertEqual(status, 200, saved)
        os.unlink(self.claudemd)          # so the restore CREATES rather than updates
        new_parent = os.path.join(self.root, "written-through-the-api")
        os.makedirs(new_parent, exist_ok=True)
        self.addCleanup(shutil.rmtree, new_parent, ignore_errors=True)
        fresh_mcp = os.path.join(self.root, "entry-through-the-api")
        os.makedirs(fresh_mcp, exist_ok=True)
        self.addCleanup(shutil.rmtree, fresh_mcp, ignore_errors=True)

        # POSITIVE POLE, unconditional and outside the loop: _paths over a live
        # tree GET really does find a planted config dir. Without it every
        # "not in" below could be reading an empty answer.
        status, shape = self.req("/api/configs/tree")
        self.assertEqual(status, 200)
        self.assertIn(os.path.realpath(self.proj), self._paths(shape))

        doors = (
            ("/api/configs/file", {"path": os.path.join(new_parent, "CLAUDE.md"),
                                   "content": "# created through the API\n",
                                   "revision": "missing"}),
            ("/api/configs/entry", {"action": "add",
                                    "path": os.path.join(fresh_mcp, ".mcp.json"),
                                    "kind": "mcp", "name": "added-by-the-arm",
                                    "value": {"command": "/bin/echo"}}),
            ("/api/configs/restore", {"backup": saved["backup"]}),
        )
        for i, (door, payload) in enumerate(doors):
            with self.subTest(door=door):
                configs.clear_tree_cache()
                status, _first = self.req("/api/configs/tree")
                self.assertEqual(status, 200)
                planted = self._plant_behind_helms_back("behind-helms-back-%d" % i)
                status, cached = self.req("/api/configs/tree")
                self.assertEqual(status, 200)
                self.assertNotIn(planted, self._paths(cached),
                                 "this GET was not served from the cache, so the "
                                 "assertion below would hold with no invalidation")
                status, wrote = self.req(door, payload)
                self.assertEqual(status, 200, wrote)
                self.assertTrue(wrote["created"],
                                "this door was supposed to CREATE: %r" % (wrote,))
                status, after = self.req("/api/configs/tree")
                self.assertEqual(status, 200)
                self.assertIn(planted, self._paths(after),
                              "%s left the owner's tab on a stale scan" % door)

    def test_a_refused_write_drops_neither_cache(self):
        """A write that changed nothing invalidates nothing. Re-walking on every
        rejected revision would let one client with a stale revision keep the
        cache permanently cold — which is the hang this lane exists to cure."""
        status, shape = self.req("/api/configs/tree")
        self.assertEqual(status, 200)
        self.assertIn(os.path.realpath(self.proj), self._paths(shape))
        planted = self._plant_behind_helms_back("behind-a-refusal")
        status, refused = self.req("/api/configs/file",
                                   {"path": "/etc/passwd", "content": "x",
                                    "revision": "missing"})
        self.assertEqual(status, 400, refused)
        with self._probe_spy() as probe, self._walk_spy() as walk:
            status, after = self.req("/api/configs/tree")
            self.assertEqual(status, 200)
            self.assertEqual(walk.call_count, 0, "a refused write dropped the walk")
            self.assertEqual(probe.call_count, 0,
                             "a refused write dropped the built payload")
            # POSITIVE POLES on both spies, unconditional: ?rescan=1 walks AND
            # rebuilds, and both counters move, so the two zeros above are
            # measurements taken with live instruments.
            status, _ = self.req("/api/configs/tree?rescan=1")
            self.assertEqual(status, 200)
            self.assertEqual(walk.call_count, 1, "the walk spy counted nothing")
            self.assertGreater(probe.call_count, 0, "the probe spy counted nothing")
        self.assertNotIn(planted, self._paths(after))

    def test_the_prewarm_thread_primes_the_scan_and_swallows_its_own_failures(self):  # noqa: VACUOUS_ASSERTION — the unchanged walk count after the first GET is read from the SAME spy that, two assertions earlier, is unconditionally asserted to have counted the prewarm's own walk; the payload that GET returns is then asserted to CONTAIN the fixture project.
        """The owner's FIRST tab open should be warm too.

        It hangs off the serve path (`cmd_web`), never off `make_server`, so no
        suite walks his real cwd root by starting a server; this arm calls it
        directly, which is the only way to join the thread it returns.
        """
        from helm import web_server
        configs.clear_tree_cache()
        with self._walk_spy() as walk:
            t = web_server._prewarm_configs()
            self.assertIsNotNone(t, "no thread was started")
            t.join(30)
            self.assertFalse(t.is_alive())
            # POSITIVE POLE, unconditional and on the SAME spy the zero below is
            # read from: the prewarm really walked.
            primed = walk.call_count
            self.assertGreater(primed, 0, "the prewarm walked nothing")
            status, body = self.req("/api/configs/tree")
            self.assertEqual(status, 200)
            self.assertEqual(walk.call_count, primed,
                             "the first GET after the prewarm walked anyway")
        self.assertIn(os.path.realpath(self.proj), self._paths(body))
        # A FAILING WARM IS A SLOW TAB, NEVER A SERVER THAT DID NOT START. The
        # thread hook is what makes "swallowed" observable: a thread whose body
        # raises FINISHES either way, so `is_alive()` alone cannot tell a caught
        # exception from an escaped one — only whether anything reached the
        # interpreter's last-resort handler can.
        escaped = []
        prior_hook = threading.excepthook
        threading.excepthook = escaped.append
        self.addCleanup(setattr, threading, "excepthook", prior_hook)
        with mock.patch.object(configs, "tree",
                               side_effect=OSError("the disk went away")) as broke:
            t = web_server._prewarm_configs()
            self.assertIsNotNone(t)
            t.join(30)
            self.assertFalse(t.is_alive(), "the prewarm thread never finished")
            self.assertEqual(broke.call_count, 1,
                             "the prewarm never reached the scan it is meant to warm")
        self.assertEqual([type(a.exc_value).__name__ for a in escaped], [],
                         "the prewarm let an exception escape its own thread")
        status, still = self.req("/api/configs/tree")
        self.assertEqual(status, 200)
        self.assertIn(os.path.realpath(self.proj), self._paths(still))

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
        # the predecessor's contract: a refused read answers 200 + error field + empty content
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

    # -- the FOURTH copy of the substring guess (on de0baa7) -----------------
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

        These are the exact repros. They are pinned against the ENDPOINT
        rather than harness_for, because harness_for was already correct on
        de0baa7 and the endpoint was still wrong — a unit test on the helper
        would have stayed green through the whole bug.
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

    # -- opt-in Config observation; never part of roster polling ------------
    def test_config_injection_endpoint_exposes_explicit_states(self):
        from unittest import mock
        body = {"state": "partial", "target": {
            "session": {"value": "session-v2", "sources": ["inject-v2"]},
            "cwd": {"value": self.proj, "sources": ["roster-current"]},
            "harness": {"value": "claude", "sources": ["catalog-root"]},
            "config_home": {"value": None, "sources": []}},
                "missing": ["config_home"], "conflicts": [],
                "samples": {"v2_utf8": {"count": 1, "rendered_bytes": 12},
                            "v1_approx": {"count": 2, "approx_characters": 17}}}
        with mock.patch("helm.injection_config.view", return_value=body) as view:
            status, got = self.req("/api/config/injection?seat=seat-a&session=session-v2")
        self.assertEqual((status, got), (200, body))
        view.assert_called_once_with("seat-a", "session-v2")

    def test_config_injection_backend_failure_is_explicit_unavailable(self):
        from unittest import mock
        with mock.patch("helm.injection_config.view",
                        side_effect=OSError("ledger unreadable")):
            status, body = self.req(
                "/api/config/injection?seat=seat-a&session=session-v2")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "unknown")
        self.assertEqual(body["unavailable"], ["observation"])
        self.assertEqual(set(body["target"]), {
            "seat", "session", "cwd", "harness", "transcript_home",
            "config_home"})
        self.assertTrue(all(field == {"value": None, "sources": []}
                            for field in body["target"].values()))

    def test_presence_endpoints_never_reach_config_or_transcript_readers(self):
        from unittest import mock
        with mock.patch("helm.injection_config.view") as injection, \
                mock.patch("helm.transcripts.get_catalog") as catalog, \
                mock.patch("helm.configs.tree") as config_tree:
            for endpoint in ("/api/todos", "/api/chat/roster"):
                status, _body = self.req(endpoint)
                self.assertEqual(status, 200)
        injection.assert_not_called()
        catalog.assert_not_called()
        config_tree.assert_not_called()

    # -- the UI carries the view + the token -------------------------------
    def test_ui_configs_markup_and_token_injection(self):
        status, body = self.req("/", raw=True)
        self.assertEqual(status, 200)
        body = body.decode("utf-8")
        for marker in ('id="cfgbar"', 'id="cfgFilter"', 'data-ch="claude"',
                       'data-ch="codex"', 'id="cfgReload"', 'id="cfgsplit"',
                       'id="cfgtree"', 'id="cfgdetail"'):
            self.assertIn(marker, body, "configs view markup missing: %s" % marker)
        for fn in ("cfgInit", "cfgInjection", "cfgSelectCwd", "cfgResolve",
                   "cfgSelectHome", "cfgEdit", "cfgAddMcp", "cfgLoadBackups",
                   "cfgPhysDiff", "seatToConfig"):
            self.assertIn("function " + fn, body, "configs JS missing: %s" % fn)
        self.assertIn('class="spcfg"', body,
                      "roster popup must carry the Config deep link")
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
        """Why a review refused a generic nearest-enclosing lookup.

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
                # class 1 — a mkdtemp suffix decided it
                "/tmp/helm-test-webcfg-a1pi9z0q/claude-home",
                "/tmp/helm-test-webcfg-codexy77/claude-home",
                "/var/pipeline/claude-home",
                # class 2 — an unrelated dotted ANCESTOR captured it
                "/tmp/.pi-cache/u/.claude",
                "/tmp/.claude-scratch/x/.pi/agent",
                "/tmp/.codex-tmp/u/.claude",
                # class 3 — the leaf matcher stripped the dot class 1 refused
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


class BackupListingIsMemoisedOnTheDirectoryTest(unittest.TestCase):
    """THE DEFECT, measured on the owner's box: 6,948 entries under
    BACKUP_DIR, and `list_backups` lstats each leaf, reads and json-parses its
    sidecar and resolves the origin path — 1.65s fastest, 2.22s median, 2.52s
    slowest over six calls, with NOTHING holding the answer. The editor calls
    it on every file open and again after every save and every restore, so the
    owner paid the whole walk each time.

    THE MEMO IS KEYED ON THE DIRECTORY'S OWN IDENTITY, NOT ON A CLOCK, and
    that is what these arms pin. A ttl here would have to be guessed against
    the walk, and a ttl guessed SHORTER than its fill can never serve warm —
    the shape this tree shipped twice. A fingerprint cannot fail that way, and
    it gives read-your-own-writes with no invalidation hook to forget.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-bk-")
        # THE ORIGIN HAS TO BE A REAL RECOGNIZED CONFIG. `_backup_meta` drops
        # any leaf whose `orig` does not resolve through `_candidate` and read
        # back, so a fixture whose sidecars point at an unrecognized path
        # produces an EMPTY listing — and every arm below would then be green
        # over a walk that found nothing. The roots are repointed for the same
        # reason the class above repoints them.
        self._saved = {k: getattr(configs, k) for k in
                       ("BACKUP_DIR", "CWD_ROOTS", "HOME_ROOTS")}
        self.proj = os.path.join(self.tmp, "proj")
        os.makedirs(self.proj)
        configs.CWD_ROOTS = [self.tmp]
        configs.HOME_ROOTS = [self.tmp]
        configs.BACKUP_DIR = os.path.join(self.tmp, "config-backups")
        os.makedirs(configs.BACKUP_DIR)
        self.orig = os.path.join(self.proj, "CLAUDE.md")
        with open(self.orig, "w") as f:
            f.write("# a real origin\n")
        _io._BACKUPS_CACHE.clear()

    def tearDown(self):
        _io._BACKUPS_CACHE.clear()
        for k, v in self._saved.items():
            setattr(configs, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plant(self, name):
        """One backup leaf and the sidecar that makes it readable."""
        bp = os.path.join(configs.BACKUP_DIR, name)
        with open(bp, "w") as f:
            f.write("# an older revision\n")
        with open(bp + ".orig", "w") as f:
            json.dump({"orig": os.path.realpath(self.orig),
                       "at": "2026-01-01 00:00:00"}, f)
        return bp

    def test_the_memo_answers_exactly_what_the_walk_answers(self):
        self._plant("bk-1")
        self._plant("bk-2")
        walked = _io._list_backups_uncached()
        # MUST-HIT: the fixture produced real rows, so the equality below is
        # not two empty lists agreeing.
        self.assertEqual(2, len(walked), walked)
        self.assertEqual(json.dumps(walked),
                         json.dumps(configs.list_backups()),
                         "the memo answers something the walk does not")

    def test_the_second_call_does_not_re_walk(self):
        self._plant("bk-1")
        calls = []
        real = _io._list_backups_uncached

        def counted():
            calls.append(1)
            return real()

        _io._list_backups_uncached = counted
        try:
            first = configs.list_backups()
            second = configs.list_backups()
        finally:
            _io._list_backups_uncached = real
        # THE MUST-HIT is the first call: the walk MUST have run once, or
        # "did not run twice" would be true of a route that never ran at all.
        self.assertEqual(1, len(calls),
                         "the walk ran %d times for two calls" % len(calls))
        self.assertEqual(1, len(first), first)
        self.assertEqual(json.dumps(first), json.dumps(second))

    def test_a_NEW_backup_is_visible_on_the_very_next_call(self):
        """READ-YOUR-OWN-WRITES, WITH NO HOOK TO FORGET. The owner saves a
        config, the save mints a backup, and the listing beside his editor
        must already show it. A clock-keyed memo would owe an invalidation
        call at every writer; the directory's own mtime owes none."""
        self._plant("bk-1")
        self.assertEqual(1, len(configs.list_backups()))   # warms the memo
        self._plant("bk-2")
        after = configs.list_backups()
        self.assertEqual(2, len(after),
                         "a backup written after the memo warmed is invisible "
                         "to the listing beside the editor")
        # THE CONTROL on the same observable: the fingerprint is what moved,
        # so the rebuild above is the directory being seen and not a memo that
        # simply never holds anything.
        fp = _io._backups_fingerprint()
        self.assertIsNotNone(fp, "the backup directory could not be stat'd, "
                                 "so no arm here proves a fingerprint works")
        self._plant("bk-3")
        self.assertNotEqual(fp, _io._backups_fingerprint(),
                            "a new backup did not move the directory's "
                            "identity, so the memo cannot see writes")

    def test_a_caller_that_edits_its_rows_cannot_edit_the_memo(self):
        self._plant("bk-1")
        first = configs.list_backups()
        first[0]["orig"] = "/tmp/not-the-real-origin"
        self.assertNotEqual("/tmp/not-the-real-origin",
                            configs.list_backups()[0]["orig"],
                            "a caller's edit reached the next caller's rows")

    def test_an_UNREADABLE_directory_is_never_memoised_as_an_empty_listing(self):
        """EMPTY AND UNREADABLE MUST NOT SHARE A VALUE. The walk answers []
        for both, and stamping that under a fingerprint would freeze "no
        backups" over a directory that merely could not be stat'd."""
        self._plant("bk-1")
        # THE UNCONDITIONAL POSITIVE CONTROL, on the SAME observable the
        # absence below is asserted over. `_BACKUPS_CACHE` starts empty, so
        # "the unreadable key is not in it" is equally true of a memo that
        # never stores anything at all — including one this fixture simply
        # failed to exercise. A READABLE directory must land a key in that
        # same dict first, or the assertNotIn at the end proves nothing.
        readable = str(configs.BACKUP_DIR)
        self.assertEqual(1, len(configs.list_backups()))
        self.assertIn(readable, _io._BACKUPS_CACHE,
                      "a readable directory was not memoised at all, so no "
                      "arm here can tell an unreadable one apart from it")
        configs.BACKUP_DIR = os.path.join(self.tmp, "does-not-exist")
        self.assertIsNone(_io._backups_fingerprint())
        self.assertEqual([], configs.list_backups())
        self.assertNotIn(str(configs.BACKUP_DIR), _io._BACKUPS_CACHE,
                         "an unreadable directory was cached as empty")
        # AND THE READABLE ENTRY IS STILL STANDING, so the absence above is
        # the unreadable key being refused and not the whole memo being
        # dropped by the failed call.
        self.assertIn(readable, _io._BACKUPS_CACHE)


if __name__ == "__main__":
    unittest.main()
