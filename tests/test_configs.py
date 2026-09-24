#!/usr/bin/env python3
"""helm.configs + the web configs/skills surface — hermetic contract tests.

Env is planted BEFORE helm.configs is imported (its roots freeze at import,
behavior-preserved from the predecessor): HELM_CONFIG_ROOTS -> a tmp cwd tree, HELM_HOME
-> a tmp helm home. The skills census is pointed at a tmp skill home by
patching skills._skill_homes (the test_skills pattern) and the web trash dir
at a tmp dir — the real ~/.claude / ~/.helm / ~/.cache are never touched.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="helm-test-configs-")
_ENV_KEYS = ("HELM_CONFIG_ROOTS", "HELM_HOME")
_ENV_PRIOR = {k: os.environ.get(k) for k in _ENV_KEYS}
# PLANT INTO THE ROOT THE FREEZE ALREADY USED. helm/configs/_common.py computes
# CWD_ROOTS at IMPORT time, so by the time this module runs the roots may already
# be frozen — they are, whenever any earlier test module reached helm.configs
# first. Making our own root and assigning the env below then plants fixtures
# somewhere the frozen CWD_ROOTS does not look, which is exactly why
# `pytest tests/test_envtidy.py tests/test_configs.py` failed three tests while
# this module passed alone. tests/conftest.py guarantees HELM_CONFIG_ROOTS is a
# tmp path before ANY import, so honouring it here is both correct and safe.
_ROOT = os.environ.get("HELM_CONFIG_ROOTS") or os.path.join(_TMP, "dev")
_PROJ = os.path.join(_ROOT, "proj")
_HOMEDIR = os.path.join(_TMP, "claude-home")     # a synthetic cred home
_SKILLS = os.path.join(_TMP, "skills")           # a synthetic skill home
os.makedirs(_PROJ, exist_ok=True)
os.makedirs(_HOMEDIR)
os.makedirs(_SKILLS)
os.environ["HELM_CONFIG_ROOTS"] = _ROOT   # a no-op when conftest set it
os.environ.setdefault("HELM_HOME", os.path.join(_TMP, "helm-home"))

with open(os.path.join(_PROJ, ".mcp.json"), "w") as f:
    json.dump({"mcpServers": {"planted": {"command": "/bin/echo", "args": ["hi"]}}}, f)
with open(os.path.join(_PROJ, "CLAUDE.md"), "w") as f:
    f.write("# planted memory\n")
with open(os.path.join(_HOMEDIR, "settings.json"), "w") as f:
    json.dump({"model": "opus"}, f)

from helm import configs, skills, web  # noqa: E402
from helm.configs._resolve import _CONFIG_SUBDIR  # noqa: E402

_CWD_ROOTS_PRIOR = list(configs.CWD_ROOTS)
_HOME_ROOTS_PRIOR = list(configs.HOME_ROOTS)

# unittest discovery can import helm.configs while walking the helm package,
# before it imports this test module. Mutate the shared frozen list in place so
# every configs submodule sees the planted root under either test runner.
configs.CWD_ROOTS[:] = [_ROOT]

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

    def test_pi_cascade_resolves(self):
        pi_home = os.path.join(_TMP, "pi-home")
        os.makedirs(pi_home, exist_ok=True)
        with open(os.path.join(pi_home, "settings.json"), "w") as f:
            json.dump({"defaultModel": "gemini-3.6-flash-high", "defaultProjectTrust": "always"}, f)
        pi_proj_dir = os.path.join(_ROOT, "pi_proj")
        pi_proj = os.path.join(pi_proj_dir, ".pi")
        os.makedirs(pi_proj, exist_ok=True)
        with open(os.path.join(pi_proj, "settings.json"), "w") as f:
            json.dump({"defaultModel": "claude-sonnet-5"}, f)
        configs.HOME_ROOTS.append(pi_home)
        self.addCleanup(configs.HOME_ROOTS.remove, pi_home)

        r = configs.resolve(pi_home, pi_proj_dir, "pi")
        self.assertEqual(r["harness"], "pi")
        self.assertEqual(r["settingsHighlights"]["defaultModel"], "claude-sonnet-5")
        self.assertEqual(r["settingsHighlights"]["defaultProjectTrust"], "always")

        # CLI --harness pi wiring test
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(
                configs.cmd_configs(["cascade", pi_proj_dir, "--harness", "pi", "--home", pi_home]), 0)
        cas = json.loads(out.getvalue())
        self.assertEqual(cas["harness"], "pi")
        self.assertEqual(cas["settingsHighlights"]["defaultModel"], "claude-sonnet-5")

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

        observed = {"state": "partial", "requested": {"seat": "seat-a",
                    "session": "session-a"}}
        out = io.StringIO()
        with mock.patch("helm.injection_config.view", return_value=observed) as view, \
                contextlib.redirect_stdout(out):
            self.assertEqual(configs.cmd_configs([
                "injection", "--seat", "seat-a", "--session", "session-a"]), 0)
        self.assertEqual(json.loads(out.getvalue()), observed)
        view.assert_called_once_with("seat-a", "session-a")

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(configs.cmd_configs(["show", "/etc/passwd"]), 1)
            self.assertEqual(configs.cmd_configs(["bogus"]), 2)
            self.assertEqual(configs.cmd_configs(["cascade"]), 2)
            self.assertEqual(configs.cmd_configs(["injection"]), 2)

    def test_injection_valued_flags_refuse_flags_as_values_and_duplicates(self):  # noqa: VACUOUS_ASSERTION — a valid invocation is asserted before every malformed tail, and the mocked model exploding proves each refusal occurs before work
        with mock.patch("helm.injection_config.view", return_value={"state": "unknown"}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(configs.cmd_configs([
                "injection", "--seat", "seat-a", "--session", "session-a"]), 0)
        bad = (
            (["injection", "--seat", "--session", "s"], "--seat wants a value"),
            (["injection", "--session", "--seat", "seat-a"],
             "--session wants a value"),
            (["injection", "--seat", "a", "--seat", "b"],
             "duplicate --seat"),
            (["injection", "--session", "a", "--session", "b"],
             "duplicate --session"),
        )
        for args, message in bad:
            with self.subTest(args=args), \
                    mock.patch("helm.injection_config.view",
                               side_effect=AssertionError("invalid tail reached model")), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(configs.cmd_configs(args), 2)
            self.assertIn(message, err.getvalue())


class WriteModeTest(unittest.TestCase):
    """codex-seat review HIGH: write_file replaced a 0600 settings.json with a
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
        # shutdown() waits one serve_forever poll (stdlib 0.5s)
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      kwargs={"poll_interval": 0.01}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)
        skills._skill_homes = cls._orig_homes
        web.TRASH_DIR = cls._orig_trash

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

    def test_api_configs_cascade_pi(self):
        pi_home = os.path.join(_TMP, "pi-home-api")
        os.makedirs(pi_home, exist_ok=True)
        with open(os.path.join(pi_home, "settings.json"), "w") as f:
            json.dump({"defaultModel": "gemini-3.6-flash-high"}, f)
        configs.HOME_ROOTS.append(pi_home)
        self.addCleanup(configs.HOME_ROOTS.remove, pi_home)

        q = urllib.parse.urlencode({"cwd": _PROJ, "home": pi_home, "harness": "pi"})
        status, d = self.get("/api/configs/cascade?" + q)
        self.assertEqual(status, 200)
        self.assertEqual(d["harness"], "pi")
        self.assertEqual(d["settingsHighlights"]["defaultModel"], "gemini-3.6-flash-high")

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

    def test_both_skills_doors_drop_the_cached_config_tree(self):
        """A skill directory is a row of the configs tree, entry count and all,
        and removing the last one takes its directory off the surface; a door
        that renames or trashes one without dropping the cache leaves the tab
        describing skills that are gone."""
        doomed = _mk_skill("theta", "here to be trashed")
        with mock.patch.object(configs, "clear_tree_cache") as cleared:
            status, d = self.post("/api/skills/toggle", {"path": _ALPHA})
            self.assertEqual(status, 200, d)
            self.assertEqual(cleared.call_count, 1)
            status, d = self.post("/api/skills/toggle",
                                  {"path": _ALPHA + ".disabled"})
            self.assertEqual(status, 200, d)
            self.assertEqual(cleared.call_count, 2)
            status, d = self.post("/api/skills/delete", {"path": doomed})
            self.assertEqual(status, 200, d)
            self.assertEqual(cleared.call_count, 3)
            # a REFUSED door changed nothing and drops nothing
            status, d = self.post("/api/skills/delete", {"path": doomed})
            self.assertNotEqual(status, 200, d)
            self.assertEqual(cleared.call_count, 3)

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
    """The owner reported the configs UI was 'mostly uneditable'. Measured: of
    500 files under the config homes, 457 were unrecognized — but nearly all
    of those SHOULD be (credential stores, .bak copies, runtime state, plugin
    metadata). What was left was declarative human-authored config living ONE
    LEVEL DOWN in a home, which the gate had no pattern for."""

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
        for rel in ("statusline.sh", "rigger-codex-hook.py"):
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


class ConfigTreeScanCostTest(unittest.TestCase):
    """The owner's "scanning config tree…" hang, and what the cure may not cost him.

    Measured on the owner's dev root: one GET /api/configs/tree walked ~79,000
    directories, probed every one of them, and rebuilt the whole payload —
    uncached, per request.

    THE SURFACE NEVER SHRINKS. An earlier cut of this lane bought speed by
    summarizing git linked worktrees to their root row, and paid for it in two
    lane-local .mcp.json files that left the owner's surface. Every cure kept
    here makes the SAME answer cheaper: probe only directories whose own
    listing says a config could be there, and serve a repeat request from a
    cache instead of recomputing an identical answer.

    THE FIXTURES ARE REAL GIT where a repository shape is the subject.
    `.git` pointer files are written by `git worktree add` and
    `git submodule add`, never by this file: a hand-written pointer would
    agree with whatever the code believed.
    """

    @classmethod
    def setUpClass(cls):
        if not shutil.which("git"):
            raise unittest.SkipTest("git not available")

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(dir=_ROOT, prefix="scancost-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        configs.clear_tree_cache()
        self.addCleanup(configs.clear_tree_cache)

    # -- fixtures ----------------------------------------------------------
    def _git(self, *args, cwd):
        """git with no user or system config and a fixed identity."""
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_CONFIG_SYSTEM=os.devnull, GIT_TERMINAL_PROMPT="0",
                   GIT_AUTHOR_NAME="helm test", GIT_AUTHOR_EMAIL="t@example.invalid",
                   GIT_COMMITTER_NAME="helm test", GIT_COMMITTER_EMAIL="t@example.invalid")
        run = subprocess.run(("git",) + args, cwd=cwd, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(run.returncode, 0,
                         "git %s: %s" % (" ".join(args), run.stdout.decode("utf-8", "replace")))
        return run

    def _write(self, *parts, body="# planted\n"):
        p = os.path.join(*parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(body)
        return os.path.realpath(p)

    def _checkout(self, path, files):
        """A real git checkout, one commit, with files committed into it."""
        os.makedirs(path, exist_ok=True)
        self._git("init", "-q", ".", cwd=path)
        for rel, body in files.items():
            self._write(path, *rel.split("/"), body=body)
        self._git("add", "-A", cwd=path)
        self._git("commit", "-qm", "init", cwd=path)
        return os.path.realpath(path)

    def _worktree(self, primary, path, branch="lane"):
        """A real linked worktree — git writes the `gitdir:` pointer file."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._git("worktree", "add", "-q", "-b", branch, os.path.abspath(path),
                  cwd=primary)
        return os.path.realpath(path)

    def _paths(self, out):
        nodes = []
        for r in out["roots"]:
            _tree_nodes(r, nodes)
        return {n["path"] for n in nodes}

    def _spy(self):
        return mock.patch.object(configs, "_find_config_dirs",
                                 wraps=configs._find_config_dirs)

    def _entry(self):
        """The one cache entry this test's calls created."""
        cache = configs._resolve._tree_cache
        self.assertEqual(len(cache), 1, "expected exactly one cached walk: %r" % (cache,))
        return next(iter(cache.values()))

    def _age_the_walk(self, entry, by):
        """Push this entry's walk stamp back — the state tuple is replaced
        whole, exactly as production replaces it."""
        dirs, at, gen = entry.state
        entry.state = (dirs, at - by, gen)

    def _reference(self, root, maxdepth=6):
        """(hits, directories examined) from the UNFILTERED walk.

        The property the candidate filter has to keep, written out
        independently: walk the same tree with the same depth bound and the
        same noise prune, and probe EVERY directory. Whatever this finds, the
        shipped walk must find — no more and no fewer.
        """
        root = os.path.realpath(root)
        hits, examined = set(), 0
        root_depth = root.rstrip("/").count("/")
        for dirpath, dirnames, _ in os.walk(root):
            examined += 1
            if configs._project_files_at(dirpath):
                hits.add(dirpath)
            if dirpath.rstrip("/").count("/") - root_depth >= maxdepth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if d not in configs._resolve._SKIP_DIRS
                           and not d.startswith(".worktree")]
        return hits, examined

    # -- half one: the walk answers the same set, more cheaply --------------
    def test_only_candidate_directories_are_probed(self):  # noqa: VACUOUS_ASSERTION — the set equality against the reference has two unconditional positive controls on the same observable: `hits` is asserted EQUAL to a three-element expected set, and `examined` is asserted greater than 180, so neither side can be the empty agreement.
        """os.walk already read the directory; the probe must not re-read it.

        `_project_files_at` is eight lstats and four scandirs. A directory
        whose own listing holds none of the recognized names cannot answer yes
        to any of them, and this fixture is mostly such directories — which is
        what the owner's dev root is.
        """
        for i in range(60):                       # noise: no recognized name
            d = os.path.join(self.dir, "noise%02d" % i, "src", "inner")
            os.makedirs(d, exist_ok=True)
            self._write(d, "main.py", body="print(1)\n")
            self._write(d, "README", body="nothing here\n")
        # the CWD each file makes a hit — which for `.claude/settings.json` is
        # the directory two levels up, not the one the file sits in.
        hits_wanted = {
            os.path.dirname(self._write(self.dir, "a", "CLAUDE.md")),
            os.path.dirname(self._write(self.dir, "b", ".mcp.json", body="{}\n")),
            os.path.join(self.dir, "c"),
        }
        self._write(self.dir, "c", ".claude", "settings.json", body="{}\n")
        # a CANDIDATE THAT IS NOT A HIT: the filter admits it on the name and
        # the prober is what says no. Without such a directory a "probes ==
        # hits" reading of the number below would also fit.
        near_miss = os.path.join(self.dir, "d", ".claude", "statsig")
        os.makedirs(near_miss, exist_ok=True)
        candidates = len(hits_wanted) + 1

        reference, examined = self._reference(self.dir)
        with mock.patch.object(configs, "_project_files_at",
                               wraps=configs._project_files_at) as probe:
            hits = configs._find_config_dirs(self.dir)
        self.assertEqual(hits, reference, "the cheap walk changed the answer")
        self.assertEqual(hits, hits_wanted)
        self.assertEqual(probe.call_count, candidates,
                         "every directory whose listing could not hold a config "
                         "was probed anyway")
        # THE CONTROL IS THE SAME NUMBER FROM THE OTHER SIDE: the unfiltered
        # walk really did have far more directories to offer, so the count
        # above is a saving and not an empty tree.
        self.assertGreater(examined, 180)
        self.assertLess(probe.call_count, examined / 10)

    def test_the_walk_finds_exactly_what_probing_every_directory_finds(self):
        """SET EQUALITY against the unfiltered walk, on the shapes that differ.

        A linked worktree, a submodule, noise directories, a directory at the
        depth bound and one past it, and a directory that is a candidate by
        name without being a hit. Dropped and added are each named, because
        "the sets differ" is not a report anyone can act on.
        """
        primary = self._checkout(os.path.join(self.dir, "proj"),
                                 {"CLAUDE.md": "# proj\n", "pkg/CLAUDE.md": "# pkg\n"})
        lane = self._worktree(primary, os.path.join(self.dir, "proj-wt", "lane-a"))
        self._write(lane, "CLAUDE.md", body="# this lane only\n")
        elsewhere = tempfile.mkdtemp(dir=_TMP, prefix="submodule-origin-")
        self.addCleanup(shutil.rmtree, elsewhere, ignore_errors=True)
        origin = self._checkout(os.path.join(elsewhere, "libx"),
                                {"inner/AGENTS.md": "# only copy\n"})
        sup = self._checkout(os.path.join(self.dir, "super"), {"CLAUDE.md": "# super\n"})
        self._git("-c", "protocol.file.allow=always", "submodule", "add", "-q",
                  origin, "mod", cwd=sup)
        self._git("commit", "-qm", "vendor libx", cwd=sup)
        for noise in ("node_modules", ".venv", "target", "__pycache__", ".worktrees"):
            self._write(self.dir, "noisy", noise, "CLAUDE.md", body="# skipped\n")
        deep = self.dir
        for i in range(8):                        # crosses the depth bound
            deep = os.path.join(deep, "d%d" % i)
            self._write(deep, "CLAUDE.md", body="# depth %d\n" % i)
        os.makedirs(os.path.join(self.dir, "candidate-only", ".claude"), exist_ok=True)

        reference, examined = self._reference(self.dir)
        hits = configs._find_config_dirs(self.dir)
        self.assertEqual(sorted(hits - reference), [], "directories ADDED by the filter")
        self.assertEqual(sorted(reference - hits), [], "directories DROPPED by the filter")
        # POSITIVE POLE: the set is a real, populated answer holding each shape
        # this fixture exists to cover, so the two empty differences above are
        # an agreement and not two empty sets agreeing. The bound is DERIVED —
        # the traversal has strictly more directories to offer than it finds.
        self.assertGreater(examined, len(reference))
        self.assertGreater(len(reference), 5)
        for wanted in (primary, os.path.join(primary, "pkg"), lane,
                       os.path.join(lane, "pkg"), sup,
                       os.path.join(sup, "mod", "inner")):
            self.assertIn(wanted, hits, wanted)

    def test_a_linked_worktree_and_a_submodule_are_BOTH_walked_and_listed(self):
        """THE OWNER'S SURFACE NEVER SHRINKS.

        A worktree's nested configs are largely the primary checkout's read a
        second time, and an earlier cut of this lane took that as licence to
        stop walking into one. It is true of TRACKED files and false of
        lane-local untracked ones, and the untracked ones are exactly the
        configs that exist nowhere else. Both shapes are walked in full; the
        submodule is here as the case that was never even arguable.
        """
        primary = self._checkout(os.path.join(self.dir, "proj"),
                                 {"CLAUDE.md": "# proj\n", "pkg/CLAUDE.md": "# pkg\n"})
        lane = self._worktree(primary, os.path.join(self.dir, "proj-wt", "lane-a"))
        self._write(lane, "lane-only", ".mcp.json", body="{}\n")
        elsewhere = tempfile.mkdtemp(dir=_TMP, prefix="submodule-origin-")
        self.addCleanup(shutil.rmtree, elsewhere, ignore_errors=True)
        origin = self._checkout(os.path.join(elsewhere, "libx"),
                                {"inner/AGENTS.md": "# only copy\n"})
        sup = self._checkout(os.path.join(self.dir, "super"), {"CLAUDE.md": "# super\n"})
        self._git("-c", "protocol.file.allow=always", "submodule", "add", "-q",
                  origin, "mod", cwd=sup)
        self._git("commit", "-qm", "vendor libx", cwd=sup)

        hits = configs._find_config_dirs(self.dir)
        self.assertIn(lane, hits, "a worktree root keeps its own row")
        self.assertIn(os.path.join(lane, "pkg"), hits,
                      "the worktree's nested config left the owner's surface")
        self.assertIn(os.path.join(lane, "lane-only"), hits,
                      "a config that exists ONLY in this lane left the surface")
        self.assertIn(os.path.join(sup, "mod", "inner"), hits,
                      "a submodule's configs exist nowhere else")
        # and they reach the payload the tab renders, not just the walk
        paths = self._paths(configs.tree(self.dir))
        for wanted in (lane, os.path.join(lane, "pkg"), os.path.join(lane, "lane-only"),
                       os.path.join(sup, "mod", "inner")):
            self.assertIn(wanted, paths, wanted)

    def test_the_rules_subtree_is_a_candidate_without_a_dotclaude_table_entry(self):
        """The candidate set has TWO legs, and one of them is easy to lose.

        `.claude/rules/*.md` and the skills/commands/agents counts are read by
        `_project_files_at` with no `_PROJECT_FILES` key naming them. The table
        happens to carry `.claude/CLAUDE.md` today, which supplies `.claude` by
        accident; take that away and the explicit `_CONFIG_SUBDIR` union is the
        only thing keeping a rules-only directory on the owner's surface.
        """
        planted = os.path.dirname(os.path.dirname(os.path.dirname(
            self._write(self.dir, "rules-only", ".claude", "rules", "house.md"))))
        table = {k: v for k, v in configs._resolve._PROJECT_FILES.items()
                 if not k.startswith(_CONFIG_SUBDIR + "/")}
        self.assertNotIn(_CONFIG_SUBDIR, {k.split("/", 1)[0] for k in table},
                         "this fixture no longer removes the accidental supplier")
        with mock.patch.object(configs._resolve, "_PROJECT_FILES", table):
            names = configs._resolve._candidate_names()
            # CONTROL on the whole path, not just the name set: the walk reads
            # the table LIVE, so under the same patch it still finds the
            # directory. A snapshot taken at import would not see this table.
            hits = configs._find_config_dirs(self.dir)
        self.assertIn(_CONFIG_SUBDIR, names,
                      "a rules-only directory would never even be probed")
        self.assertIn(planted, hits)

    def test_the_resolution_memo_cuts_the_reads_one_build_repeats(self):
        """The memo is a COST change, so it is measured as one.

        classify_path resolves the file's own parent, then every allowlisted
        root until one matches, then every home root's plugins dir — once per
        recognized file. Nothing about the ANSWER changes when those are
        remembered, which is the point and also why no assertion about the
        payload can see the memo at all. The lstat counter can.
        """
        for i in range(12):
            self._write(self.dir, "p%02d" % i, "CLAUDE.md")
            self._write(self.dir, "p%02d" % i, ".mcp.json", body="{}\n")
        configs.tree(self.dir)            # one walk, shared by both builds below

        @contextlib.contextmanager
        def _unmemoized():
            yield

        configs.invalidate_built()
        with mock.patch.object(configs._resolve, "resolving", _unmemoized), \
                self._counting_fs() as counts:
            plain = configs.tree(self.dir)
        plain_lstats = counts["lstat"]
        configs.invalidate_built()
        with self._counting_fs() as counts:
            memoed = configs.tree(self.dir)
        memoed_lstats = counts["lstat"]
        self.assertEqual(self._paths(plain), self._paths(memoed),
                         "the memo changed the answer, which it must never do")
        # POSITIVE POLE on the same observable: both answers are real, populated
        # trees, so the equality above is an agreement and not two empty sets.
        self.assertIn(os.path.join(self.dir, "p00"), self._paths(memoed))
        self.assertGreaterEqual(len(self._paths(memoed)), 13)
        self.assertGreater(plain_lstats, 0, "the counter never saw a build")
        self.assertLess(memoed_lstats, plain_lstats,
                        "the memo saved nothing: %d lstats memoized, %d without"
                        % (memoed_lstats, plain_lstats))

    def test_find_config_dirs_returns_the_bare_set_of_hits(self):
        """The trunk return shape, which every consumer already expects."""
        planted = os.path.dirname(self._write(self.dir, "ok", "CLAUDE.md"))
        hits = configs._find_config_dirs(self.dir)
        self.assertIsInstance(hits, set)
        self.assertIn(planted, hits)

    # -- half two: the walk cache -------------------------------------------
    def test_a_second_tree_inside_the_ttl_does_not_walk_again(self):
        planted = self._write(self.dir, "cached", "CLAUDE.md")
        planted = os.path.dirname(planted)
        with self._spy() as walk:
            first = configs.tree(self.dir)
            self.assertEqual(walk.call_count, 1, "the spy must see the real walk")
            second = configs.tree(self.dir)
            self.assertEqual(walk.call_count, 1, "the second GET re-walked the tree")
            configs.clear_tree_cache()
            third = configs.tree(self.dir)
            self.assertEqual(walk.call_count, 2, "a cleared cache must walk again")
        # the cached answer is a real scan in its own right — an equality check
        # alone would hold just as well over two empty shells.
        self.assertIn(planted, self._paths(first))
        self.assertIn(planted, self._paths(second))
        self.assertIn(planted, self._paths(third))
        self.assertIsNot(first, second)
        self.assertIsNot(first["roots"], second["roots"])

    def test_a_cached_payload_is_never_the_one_handed_out(self):
        """A caller that sorts or empties what it received must not be editing
        the next reader's answer, nor holding helm's own root lists.

        THE WARM ANSWER IS THE ONE AT RISK and the arm has to stay on it: a
        cold call builds a fresh object whatever the cache does with it
        afterwards, and anything that drops the cache in between — a rescan, a
        clear — retires the very object the mutation was supposed to reach.
        Both reads here are hits on one stored payload.
        """
        planted = os.path.dirname(self._write(self.dir, "cached", "CLAUDE.md"))
        configs.tree(self.dir)                    # the cold build that stores it
        second = configs.tree(self.dir)
        self.assertIn(planted, self._paths(second))
        second["roots"].clear()
        second["home_roots"].append("/dev/null")
        third = configs.tree(self.dir)
        self.assertIsNot(second, third, "the cache handed out its own payload")
        self.assertIn(planted, self._paths(third),
                      "a caller that emptied its own answer emptied the cache")
        self.assertNotIn("/dev/null", third["home_roots"])
        self.assertIsNot(third["home_roots"], configs.HOME_ROOTS)

    def test_rescan_bypasses_both_caches(self):
        with self._spy() as walk:
            configs.tree(self.dir)
            configs.tree(self.dir)
            self.assertEqual(walk.call_count, 1)
            configs.tree(self.dir, rescan=True)
            self.assertEqual(walk.call_count, 2, "↻ rescan must re-walk")
            # and the rescan REPLACES the entry rather than bypassing writes
            configs.tree(self.dir)
            self.assertEqual(walk.call_count, 2)
        # a rescan that walks must also rebuild: an out-of-band edit is what
        # the owner presses it for, and a stale built payload would swallow it.
        planted = os.path.dirname(self._write(self.dir, "typed-by-hand", "CLAUDE.md"))
        self.assertNotIn(planted, self._paths(configs.tree(self.dir)))
        self.assertIn(planted, self._paths(configs.tree(self.dir, rescan=True)))

    def test_home_roots_are_part_of_the_key_and_copied_out_of_the_answer(self):
        """HOME_ROOTS is in the payload AND is read by classify_path for every
        file the walk recognizes, so a rebind must not be answered from a walk
        taken under the old list — and the list that travels out must be a
        copy, or the caller is holding helm's own authorization list."""
        extra = os.path.realpath(tempfile.mkdtemp(dir=_TMP, prefix="home-rebind-"))
        self.addCleanup(shutil.rmtree, extra, ignore_errors=True)
        with self._spy() as walk:
            first = configs.tree(self.dir)
            self.assertEqual(walk.call_count, 1)
            configs.HOME_ROOTS.append(extra)
            self.addCleanup(configs.HOME_ROOTS.remove, extra)
            second = configs.tree(self.dir)
            self.assertEqual(walk.call_count, 2, "a HOME_ROOTS rebind was answered from cache")
        self.assertIn(extra, second["home_roots"])
        self.assertNotIn(extra, first["home_roots"],
                         "the earlier answer was holding the live HOME_ROOTS list")
        self.assertIsNot(second["home_roots"], configs.HOME_ROOTS)

    def test_extra_cwds_are_probes_on_top_of_the_walk_not_walk_cache_keys(self):
        """The production caller folds a MOVING set of live session cwds into
        every request. Keyed on by the WALK, that is a miss per request and a
        cache entry left behind per request — it both missed and accumulated."""
        with self._spy() as walk:
            for i in range(50):
                d = os.path.join(self.dir, "session%02d" % i)
                os.makedirs(d, exist_ok=True)
                configs.tree(self.dir, extra_cwds=[d])
            self.assertEqual(walk.call_count, 1,
                             "a moving extra_cwds set re-walked the tree")
        self.assertEqual(len(configs._resolve._tree_cache), 1,
                         "distinct extra_cwds left distinct walk entries behind")
        # CONTROL: they are still APPLIED. This cwd is planted AFTER the walk
        # that is now cached, so it can only reach the answer as a probe.
        late = os.path.dirname(self._write(self.dir, "late-session", "CLAUDE.md"))
        with self._spy() as walk:
            folded = configs.tree(self.dir, extra_cwds=[late])
            plain = configs.tree(self.dir)
            self.assertEqual(walk.call_count, 0, "the probe paid for a walk")
        self.assertIn(late, self._paths(folded))
        self.assertNotIn(late, self._paths(plain))

    def test_the_walk_cache_is_bounded(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive pole is the assertEqual(len(cache), cap) one line above the bound: a cache that stored nothing would satisfy `<= cap` for free, and this arm refuses that reading by requiring the cap to be REACHED before it is held.
        cap = configs._resolve._TREE_CACHE_MAX
        for i in range(cap + 8):
            d = os.path.join(self.dir, "root%02d" % i)
            os.makedirs(d, exist_ok=True)
            configs.tree(d)
        # POSITIVE POLE ON THE SAME OBSERVABLE, unconditional: a cache that
        # stored nothing at all would satisfy the bound below for free.
        self.assertEqual(len(configs._resolve._tree_cache), cap)
        self.assertLessEqual(len(configs._resolve._tree_cache), cap)

    def test_the_built_cache_is_bounded(self):  # noqa: VACUOUS_ASSERTION — the assertEqual(len(built), cap) immediately above the bound is the unconditional positive pole; an empty cache would satisfy `<= cap` without storing anything.
        cap = configs._resolve._BUILT_CACHE_MAX
        for i in range(cap + 8):
            d = os.path.join(self.dir, "probe%02d" % i)
            os.makedirs(d, exist_ok=True)
            configs.tree(self.dir, extra_cwds=[d])
        self.assertEqual(len(configs._resolve._built_cache), cap)
        self.assertLessEqual(len(configs._resolve._built_cache), cap)

    def test_an_expired_payload_is_dropped_not_kept_until_the_cap(self):
        """A payload past its TTL can never be served again. Held until sixteen
        newer keys displace it, it is dead memory in a long-lived server."""
        with mock.patch.object(configs._resolve, "_BUILT_TTL", 0.0):
            for i in range(3):
                d = os.path.join(self.dir, "aged%02d" % i)
                os.makedirs(d, exist_ok=True)
                configs.tree(self.dir, extra_cwds=[d])
            self.assertEqual(len(configs._resolve._built_cache), 1)

    def test_a_stale_read_is_served_at_once_and_refreshed_behind_it(self):  # noqa: VACUOUS_ASSERTION — the absent `planted` has two unconditional poles on the same observable: `early` IS in the same served tree one line above (so the answer is a real scan, not an empty one), and `planted` IS in `fresh` at the end (so the path is one this walk can find at all).
        """Past the TTL the reader gets the LAST GOOD walk immediately and one
        background thread replaces it. A tab open must never pay for a walk
        that only the next reader needs."""
        # WHICH THREAD WALKED is the property, and a call COUNT cannot see it:
        # on a fixture this small the refresh finishes before the next line
        # runs, so "one walk so far" is true of a blocking implementation too.
        early = os.path.dirname(self._write(self.dir, "early", "CLAUDE.md"))
        walked_on = []
        real = configs._find_config_dirs

        def record(root, *args, **kwargs):
            walked_on.append(threading.current_thread())
            return real(root, *args, **kwargs)

        here = threading.current_thread()
        with mock.patch.object(configs, "_find_config_dirs", side_effect=record):
            configs.tree(self.dir)
            self.assertEqual(walked_on, [here], "the first, cold read walks here")
            entry = self._entry()
            self._age_the_walk(entry, configs._resolve._TREE_TTL + 1.0)
            planted = os.path.dirname(self._write(self.dir, "late", "CLAUDE.md"))
            served = configs.tree(self.dir)
            # POSITIVE POLE FIRST, on the same observable: the served tree is a
            # real scan holding a real config dir, so the absence beside it is a
            # measurement and not an empty answer.
            self.assertIn(early, self._paths(served))
            self.assertNotIn(planted, self._paths(served),
                             "the reader waited for a fresh walk instead of taking "
                             "the last good one")
            self.assertIsNotNone(entry.thread, "nothing was spawned to refresh it")
            entry.thread.join(30)
            self.assertEqual(len(walked_on), 2, "no refresh ran behind the stale read")
            self.assertIsNot(walked_on[1], here, "the refresh ran on the caller's thread")
            fresh = configs.tree(self.dir)
            self.assertEqual(len(walked_on), 2, "the refreshed value was not stored")
        self.assertIn(planted, self._paths(fresh))

    def test_a_refresh_that_raises_neither_poisons_nor_wedges_the_entry(self):  # noqa: VACUOUS_ASSERTION — `entry.refreshing` being False is proven non-vacuous by the unconditional positive control below it: the second stale read spawns a refresh that really runs (walk.call_count == 1) and really lands (the planted dir appears), which is only possible if the flag had truly cleared. Asserting the flag TRUE mid-flight is not available: on a fixture this small the refresh can finish before the assertion runs.
        first = configs.tree(self.dir)
        entry = self._entry()
        self._age_the_walk(entry, configs._resolve._TREE_TTL + 1.0)
        with mock.patch.object(configs, "_find_config_dirs",
                               side_effect=OSError("the disk went away")):
            served = configs.tree(self.dir)
            self.assertIsNotNone(entry.thread)
            entry.thread.join(30)
        self.assertEqual(self._paths(served), self._paths(first),
                         "a walk that raised replaced the last good value")
        self.assertFalse(entry.refreshing, "a raising refresh left the entry wedged")
        # NOT WEDGED means the NEXT refresh still runs. Without the finally the
        # flag would stay set and this entry would serve the same stale answer
        # until the process restarted.
        self._age_the_walk(entry, configs._resolve._TREE_TTL + 1.0)
        planted = os.path.dirname(self._write(self.dir, "after-the-failure", "CLAUDE.md"))
        with self._spy() as walk:
            configs.tree(self.dir)
            self.assertIsNotNone(entry.thread)
            entry.thread.join(30)
            self.assertEqual(walk.call_count, 1)
        self.assertIn(planted, self._paths(configs.tree(self.dir)))

    def test_a_walk_of_one_root_does_not_block_a_reader_of_another(self):
        """Single-flight is a property of a KEY. One global lock across the walk
        made every reader wait on every other, so the owner's ↻ rescan of the
        dev root stalled every other pane in the house."""
        a = os.path.realpath(tempfile.mkdtemp(dir=_ROOT, prefix="scancost-a-"))
        b = os.path.realpath(tempfile.mkdtemp(dir=_ROOT, prefix="scancost-b-"))
        for d in (a, b):
            self.addCleanup(shutil.rmtree, d, ignore_errors=True)
            self._write(d, "CLAUDE.md")
        entered, release, done = (threading.Event() for _ in range(3))
        real = configs._find_config_dirs

        def slow(root, *args, **kwargs):
            if root == a:
                entered.set()
                release.wait(30)
            return real(root, *args, **kwargs)

        with mock.patch.object(configs, "_find_config_dirs", side_effect=slow):
            held = threading.Thread(target=configs.tree, args=(a,), daemon=True)
            held.start()
            self.assertTrue(entered.wait(30), "the slow walk never started")

            def read_b():
                configs.tree(b)
                done.set()

            other = threading.Thread(target=read_b, daemon=True)
            other.start()
            self.assertTrue(done.wait(30),
                            "a walk of one root blocked a reader of another")
            release.set()
            held.join(30)
            other.join(30)
        self.assertFalse(held.is_alive())

    def test_a_refusal_is_never_cached(self):
        """GUARD — passes on the pre-change code too, which had no cache at all.
        Kept because the cache makes it a live hazard: a stored refusal would
        keep answering "refused" for a root allowlisted a moment later, and
        only a server restart would clear it."""
        outside = tempfile.mkdtemp(dir=_TMP, prefix="not-a-config-root-")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        with open(os.path.join(outside, "CLAUDE.md"), "w") as f:
            f.write("# unrelated\n")
        denied = configs.tree(outside)
        self.assertEqual(denied["code"], "refused")
        configs.CWD_ROOTS.append(outside)
        self.addCleanup(configs.CWD_ROOTS.remove, outside)
        allowed = configs.tree(outside)
        self.assertNotIn("code", allowed, allowed)
        self.assertEqual(allowed["roots"][0]["path"], os.path.realpath(outside))

    def test_concurrent_callers_share_one_walk(self):
        """N web requests landing together are ONE walk — the point of
        single-flight. The barrier makes them overlap on purpose; without it
        each thread would walk its own tree."""
        start = threading.Barrier(4)
        out = []
        with self._spy() as walk:
            def go():
                start.wait(timeout=10)
                out.append(configs.tree(self.dir))
            threads = [threading.Thread(target=go) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            self.assertEqual(len(out), 4)
            self.assertEqual(walk.call_count, 1, "each concurrent GET walked its own tree")
        self.assertEqual(len({frozenset(self._paths(o)) for o in out}), 1)

    # -- half three: the built payload --------------------------------------
    def _counting_fs(self):
        """os.walk / os.scandir / os.lstat, each counted where the whole tree
        reads them. `_resolve` and `_classify` call them through the `os`
        module, so this sees the real calls rather than a stand-in."""
        counts = {}

        @contextlib.contextmanager
        def go():
            patches = []
            for name in ("walk", "scandir", "lstat"):
                counts[name] = 0
                real = getattr(os, name)

                def wrapper(*a, _n=name, _r=real, **kw):
                    counts[_n] += 1
                    return _r(*a, **kw)

                patches.append(mock.patch.object(os, name, wrapper))
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                yield counts

        return go()

    def _preamble_cost(self, root):
        """What `tree()` spends BEFORE either cache can answer: the verdict on
        the argument, then canonicalising the scan roots. MEASURED, not
        assumed, so the warm assertion below is a claim about the caches and
        not a guess about how many components a tmpdir path has."""
        with self._counting_fs() as counts:
            configs._resolve._refused_root(root)
            configs._resolve._scan_roots(root)
        return dict(counts)

    def test_a_warm_tree_reads_nothing_it_has_not_already_read(self):  # noqa: VACUOUS_ASSERTION — the warm zeros are read from the SAME three counters that, immediately above, are unconditionally asserted non-zero for the cold call (walk >= 1, scandir > 0, lstat > floor); `cold_counts` is a copy of that very dict, so a dead instrument cannot produce both readings.
        """The warm path is a dict lookup and a copy.

        The walk cache alone did not fix the owner's tab: every GET still
        re-ran `_project_files_at` and the whole `classify_path` cascade for
        every node in the tree, plus one probe per live session cwd folded in.
        Measured on the owner's box that was most of a second per request,
        rebuilding an answer identical to the last one.
        """
        self._write(self.dir, "one", "CLAUDE.md")
        self._write(self.dir, "two", ".mcp.json", body="{}\n")
        cwd = os.path.dirname(self._write(self.dir, "session", "CLAUDE.md"))
        floor = self._preamble_cost(self.dir)
        with self._counting_fs() as counts:
            cold = configs.tree(self.dir, extra_cwds=[cwd])
        cold_counts = dict(counts)
        # POSITIVE POLE FIRST, unconditional and on the SAME three counters: the
        # cold call really walks, really probes, and really reads past the floor.
        # Without it a counter that silently counted nothing would read as a
        # perfect cache.
        self.assertGreaterEqual(cold_counts["walk"], 1, "the counters never saw a walk")
        self.assertGreater(cold_counts["scandir"], 0, "the counters never saw a probe")
        self.assertGreater(cold_counts["lstat"], floor["lstat"])
        with self._counting_fs() as counts:
            warm = configs.tree(self.dir, extra_cwds=[cwd])
        self.assertEqual(counts["walk"], 0, "a warm GET re-walked the tree")
        self.assertEqual(counts["scandir"], 0, "a warm GET re-probed a directory")
        self.assertEqual(counts["lstat"], floor["lstat"],
                         "a warm GET touched the filesystem beyond resolving the "
                         "scan root it was handed")
        self.assertEqual(self._paths(warm), self._paths(cold))
        self.assertIn(cwd, self._paths(warm))
        # and dropping the payload alone puts the reads back WITHOUT a walk
        configs.invalidate_built()
        with self._counting_fs() as counts:
            rebuilt = configs.tree(self.dir, extra_cwds=[cwd])
        self.assertGreater(counts["lstat"], floor["lstat"] + 10,
                           "the payload was served from a cache that was dropped")
        self.assertEqual(counts["walk"], 0, "dropping the payload re-walked the tree")
        self.assertEqual(self._paths(rebuilt), self._paths(cold))

    def test_a_different_extra_cwd_set_is_a_different_payload(self):
        """The session cwds are not in the WALK key and must be in the BUILT
        one: two panes with different live sessions are two answers."""
        configs.tree(self.dir)          # the walk, taken before either exists
        a = os.path.dirname(self._write(self.dir, "sess-a", "CLAUDE.md"))
        b = os.path.dirname(self._write(self.dir, "sess-b", "CLAUDE.md"))
        with self._spy() as walk:
            only_a = configs.tree(self.dir, extra_cwds=[a])
            only_b = configs.tree(self.dir, extra_cwds=[b])
            self.assertEqual(walk.call_count, 0, "a probe set paid for a walk")
            # POSITIVE POLE LAST, on the same spy — last because a walk would
            # fold BOTH directories into the answer and destroy the subject.
            configs.tree(self.dir, rescan=True)
            self.assertEqual(walk.call_count, 1, "the spy never saw a walk")
        self.assertIn(a, self._paths(only_a))
        self.assertNotIn(b, self._paths(only_a))
        self.assertIn(b, self._paths(only_b))
        self.assertNotIn(a, self._paths(only_b))

    def test_session_cwds_arrive_from_a_one_shot_iterable_too(self):
        """The payload key is built by ITERATING the session cwds, so a caller
        that hands over a generator — the natural spelling for "the cwds of the
        live sessions" — must not have it drained before the build sees it. A
        probe set silently emptied folds in nothing and says nothing."""
        configs.tree(self.dir)                    # the walk, taken first
        late = os.path.dirname(self._write(self.dir, "generated-cwd", "CLAUDE.md"))
        folded = configs.tree(self.dir, extra_cwds=(c for c in [late]))
        self.assertIn(late, self._paths(folded),
                      "the probe set was consumed before the build read it")
        # CONTROL: the same directory really is absent without the probe, so the
        # presence above came from the iterable and not from the cached walk.
        self.assertNotIn(late, self._paths(configs.tree(self.dir)))

    def _rels(self, out, path):
        """The config file names one node of a payload carries."""
        nodes = []
        for r in out["roots"]:
            _tree_nodes(r, nodes)
        node = next((n for n in nodes if n["path"] == path), None)
        self.assertIsNotNone(node, "%s is not in this payload" % path)
        return sorted(f["rel"] for f in node["files"])

    def test_an_edit_outside_helms_doors_shows_up_when_the_payload_expires(self):
        """The built TTL is the bound on how long a file helm did not write
        stays invisible — and the walk is not what went stale, so the walk is
        not what gets paid for again."""
        seed = os.path.dirname(self._write(self.dir, "seed", "CLAUDE.md"))
        self.assertEqual(self._rels(configs.tree(self.dir), seed), ["CLAUDE.md"])
        self._write(self.dir, "seed", ".mcp.json", body="{}\n")
        self.assertEqual(self._rels(configs.tree(self.dir), seed), ["CLAUDE.md"],
                         "the payload was rebuilt inside its own TTL")
        with mock.patch.object(configs._resolve, "_BUILT_TTL", 0.0), self._spy() as walk:
            after = configs.tree(self.dir)
            self.assertEqual(walk.call_count, 0, "an expired payload re-walked the tree")
            # POSITIVE POLE on the same spy, unconditional: it counts a walk when
            # one happens, so the zero above is a measurement and not a dead spy.
            configs.tree(self.dir, rescan=True)
            self.assertEqual(walk.call_count, 1, "the spy never saw a walk")
        self.assertEqual(self._rels(after, seed), [".mcp.json", "CLAUDE.md"])

    def test_a_new_config_directory_needs_the_walk_not_the_payload(self):
        """The two caches hold different facts and expire on different terms.

        A file planted in a directory the walk never found cannot arrive by
        rebuilding the payload, however often that is done — which is why the
        write door that CREATES a file drops the walk, and why ↻ exists.
        """
        self._write(self.dir, "seed", "CLAUDE.md")
        configs.tree(self.dir)
        planted = os.path.dirname(self._write(self.dir, "typed-by-hand", "CLAUDE.md"))
        with mock.patch.object(configs._resolve, "_BUILT_TTL", 0.0), self._spy() as walk:
            after = configs.tree(self.dir)
            self.assertEqual(walk.call_count, 0, "an expired payload re-walked the tree")
            # POSITIVE POLE on the same spy AND on the same absence: the rescan
            # walks (the spy counts it) and the directory appears (it was findable
            # all along), so the two negatives above are both measurements.
            rescanned = configs.tree(self.dir, rescan=True)
            self.assertEqual(walk.call_count, 1, "the spy never saw a walk")
        self.assertNotIn(planted, self._paths(after))
        self.assertIn(planted, self._paths(rescanned))

    def test_invalidate_built_keeps_the_walk_and_clear_drops_it(self):
        """The two doors say different things and must do different things."""
        self._write(self.dir, "seed", "CLAUDE.md")
        configs.tree(self.dir)
        with self._spy() as walk:
            configs.invalidate_built()
            configs.tree(self.dir)
            self.assertEqual(walk.call_count, 0, "invalidate_built re-walked the tree")
            self.assertEqual(len(configs._resolve._built_cache), 1)
            configs.clear_tree_cache()
            self.assertEqual(configs._resolve._built_cache, {})
            configs.tree(self.dir)
            self.assertEqual(walk.call_count, 1, "clear_tree_cache kept the walk")

def tearDownModule():
    configs.CWD_ROOTS[:] = _CWD_ROOTS_PRIOR
    configs.HOME_ROOTS[:] = _HOME_ROOTS_PRIOR
    for k, v in _ENV_PRIOR.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(_TMP, ignore_errors=True)

    # THE POSTCONDITION LIVES WHERE THE OBLIGATION DOES. These four checks were
    # a SEPARATE MODULE (tests/test_configs_cleanup.py) that imported this one
    # as a fixture and asserted this teardown had run. That made it true only
    # when unittest happened to collect this module first: under
    # one-module-per-process the import plants _TMP and HELM_HOME, no test of
    # THIS module is collected, tearDownModule never fires, and the witness
    # failed on a restoration that was never owed.
    #
    # Asserting here costs the cross-module contract nothing and gains the
    # guarantee: this module cleans up after itself, checked in the same
    # process that did the borrowing. An AssertionError from a tearDownModule
    # is reported by unittest as a module-level error, so a regression is loud.
    assert {k: os.environ.get(k) for k in _ENV_KEYS} == _ENV_PRIOR, (
        "test_configs did not restore %s" % (_ENV_KEYS,))
    assert configs.CWD_ROOTS == _CWD_ROOTS_PRIOR, "CWD_ROOTS not restored"
    assert configs.HOME_ROOTS == _HOME_ROOTS_PRIOR, "HOME_ROOTS not restored"
    assert not os.path.exists(_TMP), "the module tmp tree survived teardown"


if __name__ == "__main__":
    unittest.main()
