"""Hermetic tests for helm.harness (the metaharness adapter seam) + the
`helm seat resume` verb. Every orca/herdr CLI call is a patched
subprocess.run; detect() gets a synthetic `which`; no real metaharness is
ever touched, no pane is ever spawned."""
import contextlib
import io
import json
import os
import shlex
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from helm import doctor, harness, seat


def _orca_reply(result, ok=True):
    return json.dumps({"id": "x", "ok": ok, "result": result})


def _herdr_reply(result):
    return json.dumps({"id": "cli:x", "result": result})


def _herdr_error(code="not_found", message="no such pane"):
    return json.dumps({"id": "cli:x", "error": {"code": code, "message": message}})


class FakeProc:
    def __init__(self, stdout, rc=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, rc, stderr


class OrcaAdapterTest(unittest.TestCase):
    def setUp(self):
        self.ad = harness.OrcaAdapter("/fake/bin/orca")

    def _patch(self, stdout, rc=0, stderr=""):
        return mock.patch.object(harness.subprocess, "run",
                                 return_value=FakeProc(stdout, rc, stderr))

    def test_spawn_builds_exact_command_and_parses_handle(self):
        with self._patch(_orca_reply({"terminal": {"handle": "term_abc"}})) as run:
            h = self.ad.spawn("/s/launch.sh --continue", title="codex", cwd="/w")
        self.assertEqual(h, "term_abc")
        self.assertEqual(run.call_args[0][0],
                         ["/fake/bin/orca", "terminal", "create",
                          "--worktree", "path:/w", "--title", "codex",
                          "--command", "/s/launch.sh --continue", "--json"])

    def test_spawn_omits_absent_title_and_cwd(self):
        with self._patch(_orca_reply({"terminal": {"handle": "t1"}})) as run:
            self.ad.spawn("true")
        self.assertEqual(run.call_args[0][0],
                         ["/fake/bin/orca", "terminal", "create",
                          "--command", "true", "--json"])

    def test_read_builds_exact_command_and_joins_tail(self):
        with self._patch(_orca_reply({"terminal": {
                "handle": "t1", "tail": ["line one", "line two"]}})) as run:
            text = self.ad.read("t1", limit=500, timeout=0.5)
        self.assertEqual(text, "line one\nline two")
        self.assertEqual(run.call_args[0][0],
                         ["/fake/bin/orca", "terminal", "read", "--terminal",
                          "t1", "--limit", "500", "--json"])
        self.assertEqual(run.call_args.kwargs["timeout"], 0.5)

    def test_send_with_and_without_enter(self):
        with self._patch(_orca_reply({})) as run:
            self.ad.send("t1", "hello")
            self.assertEqual(run.call_args[0][0],
                             ["/fake/bin/orca", "terminal", "send",
                              "--terminal", "t1", "--text", "hello",
                              "--enter", "--json"])
            self.ad.send("t1", "raw", enter=False)
            self.assertEqual(run.call_args[0][0],
                             ["/fake/bin/orca", "terminal", "send",
                              "--terminal", "t1", "--text", "raw", "--json"])
            self.ad.send("t1", "", enter=True)
            self.assertEqual(run.call_args[0][0],
                             ["/fake/bin/orca", "terminal", "send",
                              "--terminal", "t1", "--text", "", "--enter",
                              "--json"])

    def test_stop_builds_close(self):
        with self._patch(_orca_reply({})) as run:
            self.ad.stop("t1")
        self.assertEqual(run.call_args[0][0],
                         ["/fake/bin/orca", "terminal", "close",
                          "--terminal", "t1", "--json"])

    def test_list_parses_rows(self):
        with self._patch(_orca_reply({"terminals": [
                {"handle": "t1", "title": "codex", "connected": True,
                 "writable": True, "ptyId": "pty-1", "tabId": "tab-1",
                 "leafId": "leaf-1", "worktreeId": "wt-1",
                 "worktreePath": "/w"},
                {"handle": "t2", "connected": False}]})):
            rows = self.ad.list()
        self.assertEqual(rows, [
            {"handle": "t1", "title": "codex", "preview": "",
             "status": "connected", "writable": True, "pty_id": "pty-1",
             "tab_id": "tab-1", "leaf_id": "leaf-1",
             "worktree_id": "wt-1", "worktree": "/w", "orphaned": False,
             "last_output_at": None},
            {"handle": "t2", "title": "", "preview": "",
             "status": "disconnected", "writable": False, "pty_id": None,
             "tab_id": None, "leaf_id": None, "worktree_id": None,
             "worktree": None, "orphaned": False, "last_output_at": None}])

    def test_list_carries_orphaned(self):
        """The field that was DROPPED and broke every pane read after the orca
        .46 remint. A pane can be connected=True and writable=True while its PTY
        has no live renderer (orphaned=True), and reads against it return empty.
        _pane_row must carry it so _pane_live can stop answering True on it —
        there is no helm-side heuristic that distinguishes an orphaned PTY from
        an idle one, only orca's own statement."""
        with self._patch(_orca_reply({"terminals": [
                {"handle": "t1", "connected": True, "writable": True,
                 "orphaned": True},
                {"handle": "t2", "connected": True, "writable": True,
                 "orphaned": False}]})):
            rows = self.ad.list()
        self.assertTrue(rows[0]["orphaned"])
        self.assertFalse(rows[1]["orphaned"])

    def test_resolve_pane_uses_remint_stable_key(self):
        reply = {"terminal": {"handle": "new", "ptyId": "pty-1",
                              "tabId": "tab-1", "leafId": "leaf-1"}}
        with mock.patch.object(self.ad, "_runtime_call",
                               return_value=reply) as call:
            got = self.ad.resolve_pane("tab-old:leaf-old")
        call.assert_called_once_with(
            "terminal.resolvePane", {"paneKey": "tab-old:leaf-old"})
        self.assertEqual(got, {"handle": "new", "pty_id": "pty-1",
                               "tab_id": "tab-1", "leaf_id": "leaf-1"})

    def test_nonzero_rc_raises(self):
        with self._patch("", rc=1, stderr="boom"):
            with self.assertRaises(harness.HarnessError):
                self.ad.list()

    def test_ok_false_raises(self):
        with self._patch(_orca_reply({}, ok=False)):
            with self.assertRaises(harness.HarnessError):
                self.ad.stop("t1")

    def test_bad_json_raises(self):
        with self._patch("not json"):
            with self.assertRaises(harness.HarnessError):
                self.ad.list()

    def test_spawn_missing_handle_is_loud(self):
        with self._patch(_orca_reply({"terminal": {}})):
            with self.assertRaises(harness.HarnessError):
                self.ad.spawn("true")


class HerdrAdapterTest(unittest.TestCase):
    def setUp(self):
        self.ad = harness.HerdrAdapter("/fake/bin/herdr")

    def _patch(self, stdout, rc=0, stderr=""):
        return mock.patch.object(harness.subprocess, "run",
                                 return_value=FakeProc(stdout, rc, stderr))

    def test_spawn_builds_agent_start_and_parses_pane_id(self):
        reply = _herdr_reply({"type": "agent_started",
                              "agent": {"name": "codex", "pane_id": "w3:p9",
                                        "terminal_id": "term_ff"},
                              "argv": ["sh", "-lc", "cmd"]})
        with self._patch(reply) as run:
            h = self.ad.spawn("/s/launch.sh --continue", title="codex", cwd="/w")
        self.assertEqual(h, "w3:p9")
        self.assertEqual(run.call_args[0][0],
                         ["/fake/bin/herdr", "agent", "start", "codex",
                          "--cwd", "/w", "--no-focus", "--",
                          "sh", "-lc", "/s/launch.sh --continue"])

    def test_list_parses_pane_rows(self):
        reply = _herdr_reply({"type": "pane_list", "panes": [
            {"pane_id": "w3:p1", "label": "codex", "agent_status": "working"},
            {"pane_id": "w3:p2"}]})
        with self._patch(reply) as run:
            rows = self.ad.list()
        self.assertEqual(run.call_args[0][0], ["/fake/bin/herdr", "pane", "list"])
        self.assertEqual(rows, [
            {"handle": "w3:p1", "title": "codex", "status": "working"},
            {"handle": "w3:p2", "title": "", "status": "unknown"}])

    def test_read_builds_pane_read_and_returns_text(self):
        reply = _herdr_reply({"type": "pane_read",
                              "read": {"text": "tail text\nhere", "pane_id": "w3:p1"}})
        with self._patch(reply) as run:
            text = self.ad.read("w3:p1", limit=40, timeout=0.5)
        self.assertEqual(text, "tail text\nhere")
        self.assertEqual(run.call_args[0][0],
                         ["/fake/bin/herdr", "pane", "read", "w3:p1",
                          "--source", "recent", "--lines", "40"])
        self.assertEqual(run.call_args.kwargs["timeout"], 0.5)

    def test_send_enter_uses_pane_run_literal_uses_send_text(self):
        with self._patch(_herdr_reply({})) as run:
            self.ad.send("w3:p1", "make test")
            self.assertEqual(run.call_args[0][0],
                             ["/fake/bin/herdr", "pane", "run", "w3:p1", "make test"])
            self.ad.send("w3:p1", "y", enter=False)
            self.assertEqual(run.call_args[0][0],
                             ["/fake/bin/herdr", "pane", "send-text", "w3:p1", "y"])
            self.ad.send("w3:p1", "", enter=True)
            self.assertEqual(run.call_args[0][0],
                             ["/fake/bin/herdr", "pane", "run", "w3:p1", ""])

    def test_stop_builds_pane_close(self):
        with self._patch(_herdr_reply({})) as run:
            self.ad.stop("w3:p1")
        self.assertEqual(run.call_args[0][0],
                         ["/fake/bin/herdr", "pane", "close", "w3:p1"])

    def test_error_envelope_raises_with_message(self):
        with self._patch(_herdr_error(message="no such pane")):
            with self.assertRaisesRegex(harness.HarnessError, "no such pane"):
                self.ad.read("w9:p9")


class SeatHomeWorktreeTest(unittest.TestCase):
    """The ONE seat-home isolation method, implemented for all three
    metaharness cases. The floor runs against a throwaway git repo; the two
    CLI adapters are asserted at the `_run` seam so real git keeps working
    underneath them."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-home-wt-")
        self.repo = os.path.join(self.tmp, "proj")
        os.makedirs(self.repo)
        self._git("init", "-q", "-b", "main", ".")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        with open(os.path.join(self.repo, "a.txt"), "w") as f:
            f.write("hi\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "init")
        self.wt = os.path.join(self.tmp, "proj-wt", "seats", "codex")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *args, where=None):
        import subprocess
        return subprocess.run(["git", "-C", where or self.repo] + list(args),
                              capture_output=True, text=True)

    # --- the floor (headless / none / any adapter without the method) -------

    def test_path_and_branch_are_pure_deterministic_functions(self):
        self.assertEqual(harness.seat_worktree_path("/r/proj", "codex-2"),
                         os.path.join("/r/proj-wt", "seats", "codex-2"))
        self.assertEqual(harness.seat_worktree_path("/r/proj/", "codex"),
                         os.path.join("/r/proj-wt", "seats", "codex"))
        self.assertEqual(harness.seat_branch("codex-2"), "seat/codex-2")

    def test_native_creates_the_home_on_its_own_branch(self):
        path = harness.ensure_home_worktree("codex", self.repo)
        self.assertEqual(path, self.wt)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(self._git("rev-parse", "--abbrev-ref", "HEAD",
                                   where=path).stdout.strip(), "seat/codex")
        # NO worktree lock: a lane lock IS a task lease, a seat HOME is
        # long-lived and a lock would make prune/remove/gc refuse forever.
        self.assertNotIn("locked", self._git("worktree", "list").stdout)

    def test_reuse_is_idempotent_and_keeps_the_seat_bytes(self):
        path = harness.ensure_home_worktree("codex", self.repo)
        with open(os.path.join(path, "wip.txt"), "w") as f:
            f.write("mid-flight\n")
        again = harness.ensure_home_worktree("codex", self.repo)
        self.assertEqual(again, path)
        self.assertTrue(os.path.exists(os.path.join(path, "wip.txt")))
        rows = [l for l in self._git("worktree", "list").stdout.splitlines()
                if os.path.join("seats", "codex") in l]
        self.assertEqual(len(rows), 1)

    def test_stale_admin_record_is_pruned_and_the_home_reprovisioned(self):
        """A reseed/rm -rf leaves git's admin record behind; a plain re-add
        would fail. The record whose checkout is already gone is pruned first,
        which cannot touch a live sibling room."""
        path = harness.ensure_home_worktree("codex", self.repo)
        shutil.rmtree(path)
        again = harness.ensure_home_worktree("codex", self.repo)
        self.assertEqual(again, path)
        self.assertTrue(os.path.isdir(path))

    def test_unknown_bytes_at_the_home_path_are_refused_not_adopted(self):
        os.makedirs(self.wt)
        with open(os.path.join(self.wt, "someones-work.txt"), "w") as f:
            f.write("not ours\n")
        with self.assertRaisesRegex(harness.HarnessError, "not a registered"):
            harness.ensure_home_worktree("codex", self.repo)
        self.assertTrue(os.path.exists(os.path.join(self.wt,
                                                    "someones-work.txt")))

    def test_unsafe_seat_names_are_refused(self):
        for bad in ("", "..", "../escape", "-x", "a/b", " codex"):
            with self.assertRaises(harness.HarnessError):
                harness.ensure_home_worktree(bad, self.repo)
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "proj-wt")))

    def test_base_falls_back_when_the_named_base_does_not_exist(self):
        """A repo whose integration branch is not `main` still gets a home."""
        self._git("branch", "-m", "main", "trunk")
        path = harness.ensure_home_worktree("codex", self.repo)
        self.assertTrue(os.path.isdir(path))

    def test_find_repo_root_folds_a_worktree_to_the_main_checkout(self):
        path = harness.ensure_home_worktree("codex", self.repo)
        self.assertEqual(harness.find_repo_root(path), self.repo)
        self.assertEqual(harness.find_repo_root(self.repo), self.repo)
        self.assertIsNone(harness.find_repo_root(None))      # deleted cwd, no raise
        self.assertIsNone(harness.find_repo_root(self.tmp))  # outside any checkout

    # --- herdr: explicit --path + adopt (the no-orca-overfit proof) ---------

    def test_herdr_create_uses_helms_deterministic_path_and_guard_env(self):
        ad = harness.HerdrAdapter("/fake/bin/herdr")
        with mock.patch.object(ad, "_run", return_value={}) as run:
            path = ad.ensure_home_worktree("codex", self.repo)
        self.assertEqual(path, self.wt)
        listing, create = run.call_args_list[0], run.call_args_list[1]
        self.assertEqual(listing[0][0], ["worktree", "list", "--cwd",
                                         self.repo, "--json"])
        self.assertEqual(create[0][0], [
            "worktree", "create", "--cwd", self.repo,
            "--branch", "seat/codex", "--base", "main", "--path", self.wt,
            "--label", "helm seat codex", "--no-focus", "--json"])
        # the shared-tree ref-guard's sanctioned-creator bit, scoped to the call
        self.assertEqual(create[1]["env"], {"HELM_WORK_CLAIM": "1"})

    def test_herdr_reuse_short_circuits_on_its_own_registry(self):
        ad = harness.HerdrAdapter("/fake/bin/herdr")
        os.makedirs(self.wt)
        rows = {"worktrees": [{"path": self.wt, "branch": "seat/codex"}]}
        with mock.patch.object(ad, "_run", return_value=rows) as run:
            path = ad.ensure_home_worktree("codex", self.repo)
        self.assertEqual(path, self.wt)
        self.assertEqual(len(run.call_args_list), 1)   # list only, no create

    def test_herdr_create_failure_falls_back_to_native_then_adopts(self):
        """herdr does the git work in its DAEMON, whose env we cannot reach, so
        a ref-guard refusal must still leave the seat with its checkout — via
        the native floor plus herdr's real `worktree open --path` adopt."""
        ad = harness.HerdrAdapter("/fake/bin/herdr")
        calls = []

        def flaky(args, timeout=60, env=None):
            calls.append(list(args))
            if args[1] == "create":
                raise harness.HarnessError("refused by the shared-tree guard")
            return {}

        with mock.patch.object(ad, "_run", side_effect=flaky):
            path = ad.ensure_home_worktree("codex", self.repo)
        self.assertEqual(path, self.wt)
        self.assertTrue(os.path.isdir(path))           # native floor delivered
        self.assertEqual(calls[-1], ["worktree", "open", "--cwd", self.repo,
                                     "--path", self.wt, "--label",
                                     "helm seat codex", "--no-focus", "--json"])

    def test_herdr_adopt_failure_still_returns_a_working_checkout(self):
        ad = harness.HerdrAdapter("/fake/bin/herdr")
        with mock.patch.object(ad, "_run",
                               side_effect=harness.HarnessError("herdr down")):
            path = ad.ensure_home_worktree("codex", self.repo)
        self.assertEqual(path, self.wt)
        self.assertTrue(os.path.isdir(path))

    # --- orca: native path + fail-open RPC adoption (the CLI cannot adopt) ---

    def test_orca_uses_the_native_deterministic_path(self):
        """The checkout is helm-native and the orca board entry is attempted
        through the DAEMON RPC — `orca worktree set` could never do it, because
        `set` only selects a worktree orca already knows."""
        ad = harness.OrcaAdapter("/fake/bin/orca")
        with mock.patch.object(ad, "adopt_worktree",
                               return_value=(True, "adopted")) as adopt, \
                mock.patch.object(ad, "_run", return_value={}) as run:
            path = ad.ensure_home_worktree("codex", self.repo)
        self.assertEqual(path, self.wt)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(self._git("rev-parse", "--abbrev-ref", "HEAD",
                                   where=path).stdout.strip(), "seat/codex")
        adopt.assert_called_once_with(self.repo, self.wt)
        run.assert_not_called()   # no CLI leg is involved in adoption any more

    def test_orca_visibility_failure_is_fail_open(self):
        """Adoption is a nicety; the checkout is the contract. Every adoption
        failure mode (daemon down, folder-kind project, garbage reply) must cost
        the seat nothing."""
        ad = harness.OrcaAdapter("/fake/bin/orca")
        with mock.patch.object(ad, "rpc",
                               return_value=(None, "orca daemon down")):
            path = ad.ensure_home_worktree("codex", self.repo)
        self.assertEqual(path, self.wt)
        self.assertTrue(os.path.isdir(path))

    def test_orca_adopt_uses_the_selector_shape_the_daemon_accepts(self):
        """The wire shape is {"repo": "<selector>", "updates": {...}} — read from
        the installed app bundle, not guessed. A flat `repoId` (the shape a
        first guess produces) is answered `invalid_argument: Missing repo
        selector`, so pinning the accepted shape is the point of this test."""
        ad = harness.OrcaAdapter("/fake/bin/orca")
        calls = []

        def fake_rpc(method, params, **kw):
            calls.append((method, params))
            if method == "repo.list":
                return {"repos": [{"id": "r1", "path": self.repo,
                                   "kind": "git"}]}, None
            return {"repo": {}}, None

        with mock.patch.object(ad, "rpc", side_effect=fake_rpc):
            ok, detail = ad.adopt_worktree(self.repo, self.wt)
        self.assertTrue(ok, detail)
        self.assertEqual(calls[-1][0], "repo.update")
        self.assertEqual(calls[-1][1], {
            "repo": "id:r1",
            "updates": {"externalWorktreeVisibility": "show",
                        "importedExternalWorktreePaths": [self.wt]}})

    def test_orca_adopt_preserves_worktrees_adopted_before_us(self):
        """importedExternalWorktreePaths is READ-MODIFY-WRITTEN. A blind write
        would silently un-adopt every other worktree on the project."""
        ad = harness.OrcaAdapter("/fake/bin/orca")
        sent = {}

        def fake_rpc(method, params, **kw):
            if method == "repo.list":
                return {"repos": [{
                    "id": "r1", "path": self.repo, "kind": "git",
                    "importedExternalWorktreePaths": ["/other/lane"]}]}, None
            sent.update(params)
            return {"repo": {}}, None

        with mock.patch.object(ad, "rpc", side_effect=fake_rpc):
            ok, _ = ad.adopt_worktree(self.repo, self.wt)
        self.assertTrue(ok)
        self.assertEqual(sent["updates"]["importedExternalWorktreePaths"],
                         ["/other/lane", self.wt])

    def test_orca_adopt_is_idempotent_on_an_already_imported_path(self):
        ad = harness.OrcaAdapter("/fake/bin/orca")
        sent = {}

        def fake_rpc(method, params, **kw):
            if method == "repo.list":
                return {"repos": [{
                    "id": "r1", "path": self.repo, "kind": "git",
                    "importedExternalWorktreePaths": [self.wt]}]}, None
            sent.update(params)
            return {"repo": {}}, None

        with mock.patch.object(ad, "rpc", side_effect=fake_rpc):
            ad.adopt_worktree(self.repo, self.wt)
        self.assertEqual(sent["updates"]["importedExternalWorktreePaths"],
                         [self.wt])

    def test_orca_adopt_refuses_to_reclassify_a_folder_context(self):
        """MEASURED on this machine: orca holds helm as kind="folder", which has
        no externalWorktreeVisibility at all, so discovery never runs for it
        (git reported 102 helm worktrees; orca's worktree.list knew 1). The
        schema WOULD accept updates.kind="git", but that rearranges the owner's
        sidebar — helm reports it instead of doing it silently."""
        ad = harness.OrcaAdapter("/fake/bin/orca")
        calls = []

        def fake_rpc(method, params, **kw):
            calls.append(method)
            return {"repos": [{"id": "r1", "path": self.repo,
                               "kind": "folder"}]}, None

        with mock.patch.object(ad, "rpc", side_effect=fake_rpc):
            ok, detail = ad.adopt_worktree(self.repo, self.wt)
        self.assertFalse(ok)
        self.assertIn("folder", detail)
        self.assertIn("reclassified", detail)
        self.assertNotIn("repo.update", calls)   # nothing was mutated

    def test_orca_adopt_on_an_unregistered_repo_says_so(self):
        ad = harness.OrcaAdapter("/fake/bin/orca")
        with mock.patch.object(ad, "rpc", return_value=({"repos": []}, None)):
            ok, detail = ad.adopt_worktree(self.repo, self.wt)
        self.assertFalse(ok)
        self.assertIn("not have", detail)

    # --- the seam itself ---------------------------------------------------

    def test_all_three_cases_answer_the_same_deterministic_path(self):
        """The point of the seam: helm is metaharness-AGNOSTIC. Same seat, same
        repo, same answer — headless, herdr, orca."""
        expected = harness.seat_worktree_path(self.repo, "codex")
        floor = harness.ensure_home_worktree("codex", self.repo)
        herdr = harness.HerdrAdapter("/fake/bin/herdr")
        orca = harness.OrcaAdapter("/fake/bin/orca")
        with mock.patch.object(herdr, "_run", return_value={}), \
                mock.patch.object(orca, "_run", return_value={}):
            self.assertEqual(herdr.ensure_home_worktree("codex", self.repo),
                             expected)
            self.assertEqual(orca.ensure_home_worktree("codex", self.repo),
                             expected)
        self.assertEqual(floor, expected)

    def test_run_env_overlays_rather_than_replaces_the_environment(self):
        """The guard bit must ride WITHOUT stripping HOME/PATH from the CLI."""
        ad = harness.HerdrAdapter("/fake/bin/herdr")
        with mock.patch.object(harness.subprocess, "run",
                               return_value=FakeProc(_herdr_reply({}))) as run:
            ad._run(["worktree", "create"], env={"HELM_WORK_CLAIM": "1"})
        env = run.call_args[1]["env"]
        self.assertEqual(env["HELM_WORK_CLAIM"], "1")
        self.assertEqual(env.get("PATH"), os.environ.get("PATH"))
        with mock.patch.object(harness.subprocess, "run",
                               return_value=FakeProc(_herdr_reply({}))) as run:
            ad._run(["pane", "list"])
        self.assertIsNone(run.call_args[1]["env"])   # inherit when not asked


class DetectTest(unittest.TestCase):
    @staticmethod
    def _which(installed):
        return lambda b: installed.get(b)

    BOTH = {"orca": "/bin/orca", "herdr": "/bin/herdr"}

    def test_prefers_orca_by_default(self):
        ad = harness.detect(env={}, which=self._which(self.BOTH))
        self.assertIsInstance(ad, harness.OrcaAdapter)
        self.assertEqual(ad.path, "/bin/orca")

    def test_inside_herdr_session_prefers_herdr(self):
        ad = harness.detect(env={"HERDR_ENV": "1"}, which=self._which(self.BOTH))
        self.assertIsInstance(ad, harness.HerdrAdapter)

    def test_picks_whichever_is_installed(self):
        ad = harness.detect(env={}, which=self._which({"herdr": "/bin/herdr"}))
        self.assertIsInstance(ad, harness.HerdrAdapter)
        ad = harness.detect(env={}, which=self._which({"orca": "/bin/orca"}))
        self.assertIsInstance(ad, harness.OrcaAdapter)

    def test_none_installed_returns_none(self):
        self.assertIsNone(harness.detect(env={}, which=self._which({})))

    def test_env_override_wins(self):
        ad = harness.detect(env={"HELM_METAHARNESS": "herdr"},
                            which=self._which(self.BOTH))
        self.assertIsInstance(ad, harness.HerdrAdapter)
        self.assertIsNone(harness.detect(env={"HELM_METAHARNESS": "none"},
                                         which=self._which(self.BOTH)))

    def test_env_override_absent_binary_returns_none(self):
        self.assertIsNone(harness.detect(env={"HELM_METAHARNESS": "orca"},
                                         which=self._which({"herdr": "/bin/herdr"})))


class DoctorMetaharnessTest(unittest.TestCase):
    def test_detected_reports_ok_with_name_and_others(self):
        res = doctor.check_metaharness(
            detect=lambda: harness.OrcaAdapter("/bin/orca"),
            which=lambda b: {"herdr": "/bin/herdr"}.get(b))
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0][0], doctor.OK)
        self.assertIn("metaharness: orca (/bin/orca)", res[0][1])
        self.assertIn("also present: herdr", res[0][1])

    def test_none_warns_with_companion_recommendation(self):
        res = doctor.check_metaharness(detect=lambda: None, which=lambda b: None)
        self.assertEqual(res[0][0], doctor.WARN)
        self.assertIn("orca", res[0][1])
        self.assertIn("herdr", res[0][1])


# A pane whose composer is EMPTY — i.e. one that took its turn. Synthetic, but
# shaped exactly like a real frame, because `submit` proves delivery by reading
# the composer back and a double that returns "" models an UNREADABLE pane
# (UNKNOWN), not a working one.
ADVANCED_PANE = "\n".join(("─" * 40, "❯", "─" * 40,
                           "  opus-5 | ~/dev/example/repo",
                           "  ⏵⏵ bypass permissions on"))


class FakeAdapter(harness._CLIAdapter):
    """Records the uniform pane ops seat resume drives.

    Subclasses the real base so it inherits the REAL `submit` — the split send
    and the read-back verification — instead of a second implementation that
    could agree with a broken one.
    """
    name, path = "fake", "/bin/fake"

    def __init__(self, rows=()):
        self.rows, self.spawned, self.stopped, self.sent = list(rows), [], [], []

    def spawn(self, command, title=None, cwd=None):
        self.spawned.append((command, title, cwd))
        return "pane-1"

    def list(self):
        return list(self.rows)

    def read(self, handle, limit=3000, timeout=60):
        return ADVANCED_PANE

    def send(self, handle, text, enter=True):
        self.sent.append((handle, text, enter))

    def stop(self, handle):
        self.stopped.append(handle)


class SeatResumeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-resume-")
        self._env = {k: os.environ.get(k)
                     for k in ("HELM_HOME", "MELD_HOME",
                               "HELM_SPAWN_SEND_DELAY",
                               "HELM_SUBMIT_SETTLE_S")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ.pop("MELD_HOME", None)
        # resume now delivers the wake-path re-arm prompt with spawn's own
        # boot grace; a real 5s sleep per test is suite poison
        os.environ["HELM_SPAWN_SEND_DELAY"] = "0"
        # `submit` settles between typing and the bare Enter; a real settle per
        # test is the same suite poison as the boot grace above.
        os.environ["HELM_SUBMIT_SETTLE_S"] = "0"
        self.timer = mock.patch.object(seat, "_ensure_autocompact_timer")
        self.ensure_timer = self.timer.start()

    def tearDown(self):
        self.timer.stop()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mint(self, seat_name="codex", family="codex"):
        d = seat._instance_dir(family, seat_name)
        os.makedirs(d, exist_ok=True)
        launch = os.path.join(d, "launch.sh")
        with open(launch, "w") as f:
            f.write("#!/bin/sh\nexec env FAKE=1 claude \"$@\"\n")
        os.chmod(launch, 0o700)
        return d, launch

    def _record(self, d, handle="p9", harness_name="fake"):
        with open(os.path.join(d, "spawn.json"), "w") as f:
            json.dump({"v": 1, "seat": "codex", "harness": harness_name,
                       "handle": handle, "worktree": os.getcwd(),
                       "room": "main"}, f)

    def _resume(self, args, adapter):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(seat, "_write_launch_assets") as wla, \
                mock.patch.object(harness, "detect", return_value=adapter), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["resume"] + list(args))
        return rc, out.getvalue(), err.getvalue(), wla

    def test_resume_composes_continue_line_via_adapter(self):
        d, launch = self._mint()
        fake = FakeAdapter()
        rc, out, err, wla = self._resume(["codex"], fake)
        self.assertEqual(rc, 0, err)
        command, title, cwd = fake.spawned[0]
        self.assertEqual(command, "%s --continue" % shlex.quote(launch))
        self.assertEqual(title, "codex")
        self.assertEqual(cwd, os.getcwd())   # no session -> caller's cwd
        wla.assert_called_once()             # env refreshed to latest launch.sh
        self.assertIn("resumed codex via fake", out)
        self.assertIn("pane-1", out)
        with open(os.path.join(d, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["seat"], "codex")
        self.assertEqual(rec["harness"], "fake")
        self.assertEqual(rec["handle"], "pane-1")
        self.ensure_timer.assert_called_once()

    def test_concurrent_resumes_share_one_lifecycle_lock(self):
        self._mint()
        entered, release = threading.Event(), threading.Event()

        class SerialAdapter(FakeAdapter):
            def __init__(self):
                super().__init__()
                self.active = self.max_active = 0
                self.guard = threading.Lock()

            def spawn(self, command, title=None, cwd=None):
                with self.guard:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    n = len(self.spawned) + 1
                    handle = "pane-%d" % n
                    self.spawned.append((command, title, cwd))
                if n == 1:
                    entered.set()
                    release.wait(2)
                with self.guard:
                    self.rows.append({"handle": handle, "title": title or "",
                                      "status": "connected"})
                    self.active -= 1
                return handle

            def stop(self, handle):
                super().stop(handle)
                self.rows = [row for row in self.rows
                             if row.get("handle") != handle]

        fake = SerialAdapter()
        results = []

        def run():
            results.append(seat.cmd_seat(["resume", "codex"]))

        with mock.patch.object(seat, "_write_launch_assets"), \
                mock.patch.object(harness, "detect", return_value=fake), \
                mock.patch("builtins.print"):
            a, b = threading.Thread(target=run), threading.Thread(target=run)
            a.start()
            self.assertTrue(entered.wait(1))
            b.start()
            time.sleep(0.05)
            self.assertEqual(fake.max_active, 1)
            release.set()
            a.join(2)
            b.join(2)
        self.assertEqual(results, [0, 0])
        self.assertEqual(fake.max_active, 1)
        self.assertEqual(len(fake.rows), 1)
        d = seat._instance_dir("codex", "codex")
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["handle"], fake.rows[0]["handle"])

    def test_resume_uses_session_id_and_sniffed_cwd_when_resolvable(self):  # noqa: VACUOUS_ASSERTION — positive controls: the exact --resume command, the sniffed-cwd equality, and the registered sid
        d, launch = self._mint()
        sid = "0199aaaa-bbbb-cccc-dddd-eeeeffff0000"
        # the sniffed cwd must EXIST now (a recorded cwd that no longer
        # exists refuses instead of spawning somewhere stale), and
        # TEMP_ROOTS is patched so the fixture tree is not itself classed
        # throwaway (tests/test_resume_cwd.py's law)
        spot = os.path.join(self.tmp, "work", "spot")
        os.makedirs(spot)
        proj = os.path.join(d, "claude", "projects", "-work-spot")
        os.makedirs(proj)
        with open(os.path.join(proj, sid + ".jsonl"), "w") as f:
            f.write(json.dumps({"cwd": spot, "type": "user"}) + "\n")
        fake = FakeAdapter()
        from helm import seats
        with mock.patch.object(seats, "TEMP_ROOTS", ("/dev/shm",)):
            rc, out, err, _ = self._resume(["codex"], fake)
        self.assertEqual(rc, 0, err)
        command, _, cwd = fake.spawned[0]
        self.assertEqual(command, "%s --resume %s" % (shlex.quote(launch), sid))
        self.assertEqual(cwd, spot)
        self.assertIn("--resume", out)
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["session"], sid)

    def test_resume_stops_registered_pane_first(self):
        d, _ = self._mint()
        self._record(d)
        fake = FakeAdapter(rows=[{"handle": "p9", "title": "dynamic",
                                  "status": "idle"}])
        rc, out, err, _ = self._resume(["codex"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.stopped, ["p9"])

    def test_resume_refuses_title_only_decoy_without_stopping_registered_pane(self):
        d, _ = self._mint()
        self._record(d)
        fake = FakeAdapter(rows=[{"handle": "p9", "title": "dynamic"},
                                 {"handle": "p2", "title": "codex"}])
        rc, _, err, wla = self._resume(["codex"], fake)
        self.assertEqual(rc, 1)
        self.assertIn("identity-by-title", err)
        self.assertEqual(fake.stopped, [])
        self.assertEqual(fake.spawned, [])
        wla.assert_not_called()

    def test_resume_stops_registered_pane_before_reminting(self):
        """The re-mint rewrites launch.sh; a still-running stale pane's `sh`
        is reading that very file and the metaharness close is not
        process-synchronous — the stop MUST land before the rewrite."""
        d, _ = self._mint()
        self._record(d)
        fake = FakeAdapter(rows=[{"handle": "p9", "title": "dynamic",
                                  "status": "idle"}])
        order = []
        orig_wla = seat._write_launch_assets
        def spy_wla(*a, **k):
            order.append("remint")
        def spy_stop(h):
            order.append("stop")
            fake.stopped.append(h)
        fake.stop = spy_stop
        with mock.patch.object(seat, "_write_launch_assets",
                               side_effect=spy_wla) as wla, \
                mock.patch.object(harness, "detect", return_value=fake), \
                mock.patch("builtins.print"):
            rc = seat.cmd_seat(["resume", "codex"])
        self.assertEqual(rc, 0)
        self.assertEqual(order, ["stop", "remint"])  # stop strictly first

    def test_resume_never_closes_an_unregistered_same_title_pane(self):
        self._mint()
        fake = FakeAdapter(rows=[{"handle": "decoy", "title": "codex",
                                  "status": "idle"}])
        rc, _, err, wla = self._resume(["codex"], fake)
        self.assertEqual(rc, 1)
        self.assertEqual(fake.stopped, [])
        self.assertEqual(fake.spawned, [])
        wla.assert_not_called()
        self.assertIn("identity-by-title", err)

    def test_resume_register_failure_closes_new_pane(self):
        self._mint()
        fake = FakeAdapter()
        with mock.patch.object(seat, "_register_spawn", return_value=False):
            rc, _, _, _ = self._resume(["codex"], fake)
        self.assertEqual(rc, 1)
        self.assertEqual(fake.stopped, ["pane-1"])

    def test_launch_sh_written_atomically(self):
        """The re-minted launch.sh is a tmp+rename, never an O_TRUNC-in-place
        a running reader can catch half-written; mode stays 0700, no litter."""
        d, launch = self._mint()
        seat._write_launch_sh(launch, "#!/bin/sh\nexec env X=1 claude \"$@\"\n")
        with open(launch) as f:
            self.assertIn("claude", f.read())
        import stat as _st
        self.assertEqual(_st.S_IMODE(os.stat(launch).st_mode), 0o700)
        leftovers = [n for n in os.listdir(d) if n.startswith(".launch-")]
        self.assertEqual(leftovers, [])

    def test_resume_never_mints_a_nonexistent_seat(self):
        fake = FakeAdapter()
        rc, out, err, wla = self._resume(["codex"], fake)
        self.assertEqual(rc, 1)
        self.assertEqual(fake.spawned, [])
        wla.assert_not_called()
        self.assertIn("no codex seat minted", err)

    def test_resume_unknown_seat_is_usage_error(self):
        rc, out, err, _ = self._resume(["mystery"], FakeAdapter())
        self.assertEqual(rc, 2)
        self.assertIn("unknown seat", err)

    def test_resume_without_metaharness_prints_recommendation_and_paste(self):
        _, launch = self._mint()
        rc, out, err, _ = self._resume(["codex"], None)
        self.assertEqual(rc, 1)
        self.assertIn("orca", err)
        self.assertIn("herdr", err)
        self.assertIn("%s --continue" % shlex.quote(launch), err)

    def test_resume_instance_seat_uses_instance_dir(self):
        d, launch = self._mint(seat_name="codex-2")
        self.assertIn(os.path.join("instances", "codex-2"), launch)
        fake = FakeAdapter()
        rc, out, err, _ = self._resume(["codex-2"], fake)
        self.assertEqual(rc, 0, err)
        command, title, _ = fake.spawned[0]
        self.assertEqual(command, "%s --continue" % shlex.quote(launch))
        self.assertEqual(title, "codex-2")

    def test_resume_preserves_room_homing_across_the_remint(self):
        d, launch = self._mint()
        with open(launch, "w") as f:
            f.write("#!/bin/sh\nexec env HELM_CHAT_ROOM=team-z claude \"$@\"\n")
        rc, out, err, wla = self._resume(["codex"], FakeAdapter())
        self.assertEqual(rc, 0, err)
        self.assertEqual(wla.call_args[0], ("codex", d, "team-z", "codex"))
        self.assertIsNone(wla.call_args.kwargs["room_source"])

    def test_resume_preserves_derived_room_provenance(self):
        d, launch = self._mint()
        with open(launch, "w") as f:
            f.write("#!/bin/sh\nexec env HELM_CHAT_ROOM='project room' "
                    "HELM_CHAT_ROOM_SOURCE=derived claude \"$@\"\n")
        rc, out, err, wla = self._resume(["codex"], FakeAdapter())
        self.assertEqual(rc, 0, err)
        self.assertEqual(wla.call_args[0],
                         ("codex", d, "project room", "codex"))
        self.assertEqual(wla.call_args.kwargs["room_source"], "derived")

    def test_resume_command_never_carries_the_seat_token(self):
        """TOKEN LAW: the pane command is the launch.sh PATH — the expanded
        line (ANTHROPIC_AUTH_TOKEN=…) must never cross the adapter seam."""
        d, _ = self._mint()
        with open(os.path.join(d, "token"), "w") as f:
            f.write("super-secret-token\n")
        fake = FakeAdapter()
        rc, _, err, _ = self._resume(["codex"], fake)
        self.assertEqual(rc, 0, err)
        command, title, _ = fake.spawned[0]
        self.assertNotIn("super-secret-token", command)
        self.assertNotIn("ANTHROPIC", command)


if __name__ == "__main__":
    unittest.main()
