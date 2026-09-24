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

from helm import dispatches, doctor, harness, proxywatch, seat


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

    def test_read_preserves_the_structured_composer_draft(self):
        """task/1944: screen rows can show an empty prompt while Orca's own
        current-input field holds the text. The structured field must win."""
        probe = "HELM_CC2166_COMPOSER_PROBE"
        with self._patch(_orca_reply({"terminal": {
                "handle": "t1", "tail": ["─" * 80, "❯", "─" * 80,
                                            "  ⏵⏵ bypass permissions on"],
                "draft": probe}})):
            text = self.ad.read("t1")
        self.assertIn("❯\xa0" + probe, text)
        self.assertIs(harness.composer_holds(text, probe), True,
                      "the non-empty structured draft was laundered into clear")

    def test_empty_structured_draft_stays_clear_despite_quoted_output(self):  # noqa: VACUOUS_ASSERTION — sibling non-empty structured-draft arm proves the same field is consumed; exact empty prompt + False pins this branch
        with self._patch(_orca_reply({"terminal": {
                "tail": ['● diagnostic says draft: "not composer input"'],
                "draft": ""}})):
            text = self.ad.read("t1")
        self.assertIs(harness.composer_holds(text, "not composer input"), False)
        self.assertEqual(harness.composer_body(harness._prompt_line(text)), "")

    def test_absent_structured_draft_preserves_the_old_text_frame(self):  # noqa: VACUOUS_ASSERTION — exact tail equality plus held=True positively prove the absent-field compatibility path ran
        tail = ["─" * 80, "❯\xa0old frame", "─" * 80,
                "  ⏵⏵ bypass permissions on"]
        with self._patch(_orca_reply({"terminal": {"tail": tail}})):
            text = self.ad.read("t1")
        self.assertEqual(text, "\n".join(tail))
        self.assertIs(harness.composer_holds(text, "old frame"), True)

    def test_multiline_structured_draft_never_earns_exact_enter_authority(self):  # noqa: VACUOUS_ASSERTION — None is paired with held=True on the same non-empty multiline field, proving only exact authority is withheld
        with self._patch(_orca_reply({"terminal": {
                "tail": ["❯", "─" * 80], "draft": "Helm line\nhuman line"}})):
            text = self.ad.read("t1")
        self.assertIsNone(harness.composer_exactly_holds(text, "Helm line\nhuman line"))
        self.assertIs(harness.composer_holds(text, "Helm line\nhuman line"), True)

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
             "incarnation_id": None,
             "worktree_id": "wt-1", "worktree": "/w", "orphaned": False,
             "last_output_at": None},
            {"handle": "t2", "title": "", "preview": "",
             "status": "disconnected", "writable": False, "pty_id": None,
             "tab_id": None, "leaf_id": None, "worktree_id": None,
             "incarnation_id": None,
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
    """SLICE 0 / LAYER 1 of the coordination substrate, seat isolation — the ONE
    isolation method, implemented for all three metaharness cases. The floor
    runs against a throwaway git repo; the two CLI adapters are asserted at the
    `_run` seam so real git keeps working underneath them."""

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
        the installed app bundle, not guessed. A flat `repoId` (the shape this
        slice was briefed with) is answered `invalid_argument: Missing repo
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
        self.typed = {}

    def spawn(self, command, title=None, cwd=None):
        self.spawned.append((command, title, cwd))
        return "pane-1"

    def list(self):
        return list(self.rows)

    def read(self, handle, limit=3000, timeout=60):
        text = self.typed.get(handle)
        if text is not None:
            return ADVANCED_PANE.replace("\n❯\n", "\n❯\xa0%s\n" % text)
        return ADVANCED_PANE

    def send(self, handle, text, enter=True):
        if enter:
            self.typed.pop(handle, None)
        else:
            self.typed[handle] = text
        self.sent.append((handle, text, enter))

    def stop(self, handle):
        self.typed.pop(handle, None)
        self.stopped.append(handle)
        self.rows = [row for row in self.rows
                     if row.get("handle") != handle]


class SeatResumeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-resume-")
        self._env = {k: os.environ.get(k)
                     for k in ("HELM_HOME", "MELD_HOME", "HELM_CHAT_NAME",
                               "HELM_SPAWN_SEND_DELAY",
                               "HELM_SUBMIT_SETTLE_S", "HELM_SEAT_ROLE")}
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
        self.registry = mock.patch.dict(harness.ADAPTERS, {"fake": FakeAdapter})
        self.registry.start()

    def tearDown(self):
        self.registry.stop()
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

    def _record(self, d, handle="p9", harness_name="fake", **extra):
        rec = {"v": 1, "seat": "codex", "harness": harness_name,
               "handle": handle, "session": "session-a",
               "worktree": os.getcwd(), "room": "main"}
        rec.update(extra)
        with open(os.path.join(d, "spawn.json"), "w") as f:
            json.dump(rec, f)

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
        self.assertEqual(
            fake.sent[0][1],
            "Run `helm seat boot-brief --rearm` and follow it.")
        self.ensure_timer.assert_called_once()

    def test_resume_preserves_recorded_lead_and_reasserts_ultracode(self):
        d, launch = self._mint()
        self._record(d, role="lead")
        fake = FakeAdapter(rows=[{"handle": "p9", "title": "dynamic",
                                 "status": "idle"}])
        rc, _, err, _ = self._resume(["codex"], fake)
        self.assertEqual(rc, 0, err)
        command, _, _ = fake.spawned[0]
        self.assertEqual(
            command, "env HELM_SEAT_ROLE=lead %s --settings %s --continue" % (
                shlex.quote(launch), shlex.quote('{"ultracode":true}')))
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["role"], "lead")
        self.assertEqual(
            fake.sent[0][1],
            "Run `helm seat boot-brief --rearm` and follow it.")

    def test_resume_can_explicitly_promote_and_demote(self):
        d, launch = self._mint()
        fake = FakeAdapter()
        rc, _, err, _ = self._resume(
            ["codex", "--role", "lead"], fake)
        self.assertEqual(rc, 0, err)
        self.assertIn("HELM_SEAT_ROLE=lead", fake.spawned[0][0])
        self.assertIn("--settings", fake.spawned[0][0])

        self._record(d, handle="pane-1", role="lead")
        fake = FakeAdapter(rows=[{"handle": "pane-1", "title": "dynamic",
                                 "status": "idle"}])
        rc, _, err, _ = self._resume(
            ["codex", "--role", "worker"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.spawned[0][0], "%s --continue" % shlex.quote(launch))
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["role"], "worker")

    def test_resume_rejects_unknown_role_before_reaping(self):
        d, _ = self._mint()
        self._record(d)
        fake = FakeAdapter(rows=[{"handle": "p9", "title": "dynamic",
                                 "status": "idle"}])
        rc, _, err, wla = self._resume(
            ["codex", "--role", "captain"], fake)
        self.assertEqual(rc, 2)
        self.assertIn("--role wants one of: worker, lead", err)
        self.assertEqual(fake.stopped, [])
        self.assertEqual(fake.spawned, [])
        wla.assert_not_called()

    def test_resume_refuses_a_corrupt_recorded_role_unless_overridden(self):
        d, launch = self._mint()
        self._record(d, role="captain")
        fake = FakeAdapter(rows=[{"handle": "p9", "title": "dynamic",
                                 "status": "idle"}])
        rc, _, err, wla = self._resume(["codex"], fake)
        self.assertEqual(rc, 2)
        self.assertIn("recorded spawn role is invalid", err)
        self.assertEqual(fake.stopped, [])
        wla.assert_not_called()

        rc, _, err, _ = self._resume(
            ["codex", "--role", "worker"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.spawned[0][0], "%s --continue" % shlex.quote(launch))

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
                mock.patch.object(seat, "_newest_seat_session",
                                  return_value=("session-a", self.tmp)), \
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
        # the sniffed cwd must EXIST now (row #155: a recorded cwd that no
        # longer exists refuses instead of spawning somewhere stale), and
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
        os.environ["HELM_CHAT_NAME"] = "operator-seat"
        with mock.patch.object(seats, "TEMP_ROOTS", ("/dev/shm",)):
            rc, out, err, _ = self._resume(["codex"], fake)
        self.assertEqual(rc, 0, err)
        command, _, cwd = fake.spawned[0]
        self.assertEqual(command, "%s --resume %s" % (shlex.quote(launch), sid))
        self.assertEqual(cwd, spot)
        self.assertIn("--resume", out)
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["session"], sid)
        row = seats.roster()["codex"]
        runtime, verified = seats.runtime_for_session(row, sid)
        self.assertTrue(verified)
        self.assertEqual(runtime, {"agent_harness": "claude",
                                   "family": "codex", "backend": "proxy"})
        entry = seats.runtime_entry_for_session(row, sid)
        self.assertEqual(entry["source"], "lifecycle")

        route = {"alias": "codex-wire", "provider": "openai",
                 "upstream_model": "gpt-live",
                 "base_url": "https://api.openai.test/v1"}
        observed = int(time.time())
        proof = {"v": 2, "session": sid, "agent_harness": "claude",
                 "agent_pid": 4101,
                 "agent_starttime": 701, "model": "codex-wire",
                 "local_base_url": "http://127.0.0.1:8360",
                 "proxy_pid": 4201, "proxy_identity": "proc:702",
                 "proxy_config": "/safe/config.yaml",
                 "config_sha256": "a" * 64, "route": route,
                 "observed_at": observed,
                 "canary": {"state": "HEALTHY", "status": 200}}
        base = {key: value for key, value in proof.items()
                if key not in ("observed_at", "canary")}
        shape = {"url": proof["local_base_url"], "token": "live-secret",
                 "model": proof["model"], "proof": base}
        configured = {
            "codex": {"mode": "proxy-key", "model": "codex-wire",
                      "provider": "openai", "upstream_model": "gpt-live",
                      "base_url": "https://api.openai.test/v1"}}
        state = os.path.join(self.tmp, "proxywatch.json")
        report = {"ts": observed, "seats": [], "upstream": {},
                  "proxy_runtime": {"self-label-is-not-authority": proof}}
        policy = {"id": "test-approval-tier",
                  "policy_reason": "measured Codex seats may approve",
                  "policy_members": ["family:codex"]}
        proxywatch._PROXY_AUTH_CANARIES.clear()
        with mock.patch.dict(seat.FAMILIES, configured, clear=True), \
                mock.patch.object(proxywatch, "_state_path",
                                  return_value=state), \
                mock.patch.object(proxywatch, "_proxy_runtime_shape",
                                  return_value=(shape, None)), \
                mock.patch.object(proxywatch, "proxy_runtime_canary",
                                  return_value=(proof, None)), \
                mock.patch("helm.store.load_certain_policy",
                           return_value=(policy, None)):
            self.assertTrue(proxywatch.record(report))
            tier, message = dispatches.approval_tier("codex")
        self.assertEqual(tier, "ok", message)
        entry = seats.runtime_entry_for_session(seats.roster()["codex"], sid)
        self.assertEqual(entry["source"], "proxywatch")
        self.assertEqual(entry["proxy_proof"], proof)

        lead = FakeAdapter(rows=[{"handle": "pane-1", "title": "dynamic",
                                  "status": "idle"}])
        with mock.patch.object(seats, "TEMP_ROOTS", ("/dev/shm",)):
            rc, _, err, _ = self._resume(
                ["codex", "--role", "lead"], lead)
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            lead.spawned[0][0],
            "env HELM_SEAT_ROLE=lead %s --settings %s --resume %s" % (
                shlex.quote(launch), shlex.quote('{"ultracode":true}'), sid))

    def test_resume_stops_registered_pane_first(self):
        d, _ = self._mint()
        self._record(d)
        fake = FakeAdapter(rows=[{"handle": "p9", "title": "dynamic",
                                  "status": "idle"}])
        rc, out, err, _ = self._resume(["codex"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.stopped, ["p9"])
        self.assertEqual(fake.spawned[0][0], "%s --continue" % shlex.quote(
            os.path.join(d, "launch.sh")))
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["role"], "worker")

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
            fake.rows = [row for row in fake.rows
                         if row.get("handle") != h]
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


class InstrumentFailureIsNotAnEstateFact(unittest.TestCase):
    """A CLI that never answered says NOTHING about the fleet.

    The 2026-08-27 incident: a wrapper race left `orca terminal list` refusing,
    helm rendered "pane inventory failed", and that read as "every seat is
    dead" for hours. The estate was never measured. These arms hold the two
    apart, including the input the classification MUST REJECT."""

    def _rc_failure(self, body):
        """A real _run() against a CLI that exits non-zero with `body`."""
        class _A(harness._CLIAdapter):
            name, bin = "orca", "orca"
        proc = mock.Mock(returncode=2, stdout="", stderr=body)
        with mock.patch("subprocess.run", return_value=proc):
            with self.assertRaises(harness.HarnessError) as caught:
                _A("orca")._run(["terminal", "list", "--json"])
        return caught.exception

    def test_a_CLI_that_failed_before_speaking_its_protocol_is_named(self):
        # Unconditional positive controls FIRST: a code compared against an
        # empty constant would match anything, and the raised error must
        # actually carry the CLI's own words or there is nothing to classify.
        self.assertTrue(harness.CLI_PROTOCOL_UNREACHED)
        self.assertIsInstance(harness.CLI_PROTOCOL_UNREACHED, str)
        exc = self._rc_failure("orca: bundle missing at /home/x (version=none)")
        self.assertIn("bundle missing", str(exc))
        self.assertEqual(exc.code, harness.CLI_PROTOCOL_UNREACHED)

    def test_MUST_MISS_a_real_coded_error_is_not_swallowed_by_the_new_code(self):
        """The input this classification has to REJECT.

        A CLI that DID speak its protocol and reported a real error must keep
        its own code. If this ever returns CLI_PROTOCOL_UNREACHED, the new
        classification has eaten every genuine coded failure — and the
        selector_not_found branch at the orca spawn seam goes with it."""
        exc = self._rc_failure(json.dumps(
            {"ok": False, "error": {"code": "selector_not_found",
                                    "message": "no such pane"}}))
        self.assertEqual(exc.code, "selector_not_found")
        self.assertNotEqual(exc.code, harness.CLI_PROTOCOL_UNREACHED)

    def test_the_operator_is_told_the_fleet_was_NOT_measured(self):
        from helm import seat_resume_all
        exc = self._rc_failure("orca: bundle missing at /home/x")
        rendered = seat_resume_all.inventory_failure(exc)
        self.assertIn("NOTHING about the fleet was measured", rendered)
        self.assertIn("no seat is known dead", rendered)

    def test_a_coded_failure_does_NOT_claim_the_fleet_went_unmeasured(self):
        """The renderer's own must-miss: the reassurance is not unconditional."""
        from helm import seat_resume_all
        exc = self._rc_failure(json.dumps(
            {"ok": False, "error": {"code": "selector_not_found", "message": "x"}}))
        rendered = seat_resume_all.inventory_failure(exc)
        self.assertNotIn("NOTHING about the fleet was measured", rendered)
        self.assertIn("pane inventory failed", rendered)


class TheRpcDeadlineIsWallClockNotPerRecv(unittest.TestCase):
    """A daemon that trickles keepalives must not hold a hook open forever.

    MEASURED by a hookprobe on its FIRST live run:
    inject caught at 9.02s asleep in wchan=unix_stream_data_wait, blocked
    reading the orca runtime socket.

    TWO defects, one deadline. (a) settimeout() bounds a SINGLE operation, so
    connect, sendall and each recv drew a FULL budget apiece — 3x the declared
    timeout, which is how a 5s RPC ceiling blew a 10s hook. (b) the reply loop
    `continue`s on every _keepalive, so a trickling daemon reset the effective
    clock each pass: every operation inside budget, total wall unbounded.
    """

    def _server(self, serve):
        """A listening AF_UNIX server with ONE teardown routine, registered
        IMMEDIATELY after the socket exists so an early failure runs the same
        path as a clean exit.

        Teardown order matters and is asserted: signal stop, close accepted
        clients, close the LISTENER (which wakes a blocked accept), join, and
        assert the thread is actually dead — a join that times out silently is
        the cleanup-that-does-nothing shape. rmtree last.
        """
        import socket as _s, shutil, threading, os as _os
        from tests import socket_dir
        d = socket_dir()
        path = _os.path.join(d, "sock")
        srv = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
        stop = threading.Event()
        clients = []
        state = {"thread": None}

        def teardown():
            """CLEANUP RUNS TO COMPLETION, THEN ASSERTS.

            The liveness assertion exists to stop a silent join timeout, but
            placed BEFORE rmtree it aborts the routine and leaks the very dir
            the routine exists to remove — a check that can skip the work it
            guards is the same defect as the silent join it replaced. So every
            release happens unconditionally in a finally, and the assertion is
            the LAST thing, after nothing is left to free."""
            alive = None
            try:
                stop.set()
                for c in clients:
                    try:
                        c.close()
                    except OSError:
                        pass
                # WAKE accept() EXPLICITLY, then close. Closing the
                # listener from another thread does NOT reliably return a
                # thread already blocked in accept() — a probe measured this
                # leaking REPRODUCIBLY, and the old comment here asserted the
                # opposite. A throwaway connect is what accept() is actually
                # waiting for, so it returns by the documented path instead of
                # by a race we hoped for.
                try:
                    waker = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
                    try:
                        waker.settimeout(1.0)
                        waker.connect(path)
                    finally:
                        waker.close()
                except OSError:
                    pass             # already closed, or never listened
                try:
                    srv.close()
                except OSError:
                    pass
                t = state["thread"]
                if t is not None:
                    t.join(3.0)
                    alive = t.is_alive()
            finally:
                shutil.rmtree(d, ignore_errors=True)
            self.assertNotEqual(alive, True,
                                "daemon thread survived teardown — a join "
                                "that times out leaves the socket live")

        self.addCleanup(teardown)    # registered BEFORE bind/listen
        srv.bind(path)
        srv.listen(1)
        t = threading.Thread(target=serve, args=(srv, stop, clients),
                             daemon=True)
        state["thread"] = t
        t.start()
        return path

    def _userdata(self, path):
        """A throwaway orca-runtime.json pointing at our fake daemon.

        RESTORED: a range edit that replaced _server/_daemon swallowed this
        method, which sits between them. It is CALLED four times and was
        DEFINED zero, so every arm using it raised AttributeError before its
        first assertion — the fifth arm this lane has broken by construction,
        and the first I shipped to review rather than caught myself. A range
        edit succeeds while doing something other than what I meant; an ast
        census over defined-vs-called is what sees it, and reading the diff is
        not."""
        import json as _j, os as _os, shutil, tempfile
        ud = tempfile.mkdtemp(prefix="helm-test-ud-")
        self.addCleanup(shutil.rmtree, ud, ignore_errors=True)
        with open(_os.path.join(ud, "orca-runtime.json"), "w") as f:
            _j.dump({"runtimeId": "rt-1", "authToken": "tok",
                     "transports": [{"kind": "unix", "endpoint": path}]}, f)
        return ud

    def _daemon(self, behaviour):
        """Accept one connection, read the request, hand it to `behaviour`."""
        def serve(srv, stop, clients):
            try:
                conn, _ = srv.accept()
                clients.append(conn)
                behaviour(conn, conn.recv(65536), stop)
            except OSError:
                pass
        return self._server(serve)

    def _slow_drain_then_silent(self, size=1_500_000, sip=0.01):
        """Drain the request SLOWLY, then go silent — and WITNESS both phases.

        `drained` is set only when the WHOLE FRAME has arrived — read to the
        NEWLINE the producer appends, never to a byte count. Counting to
        `size` was a false witness and a probe measured it: the wire carries
        the JSON envelope too (id, authToken, method, braces, newline), so at
        `size` bytes the client still had 170 unsent. `drained` then claimed
        "send COMPLETED" while proving only "the server read 1.5MB" — a weaker
        property wearing the stronger name. The frame terminator is the only
        boundary that means sendall() returned.
        """
        import threading, time as _t
        drained = threading.Event()
        w = {"drain_done": None}

        def serve(srv, stop, clients):
            try:
                conn, _ = srv.accept()
                clients.append(conn)
                buf = b""
                while b"\n" not in buf and not stop.is_set():
                    b = conn.recv(65536)
                    if not b:
                        break
                    buf += b
                    _t.sleep(sip)
                if b"\n" in buf:
                    w["drain_done"] = _t.perf_counter()
                    drained.set()          # WHOLE frame in; send is complete
                stop.wait(30)
            except OSError:
                pass
        path = self._server(serve)
        self._assert_send_blocks(size)
        return path, size, drained, w

    def _recv_spy(self, path):
        """Set an Event the first time THE CLIENT calls recv. Transparent.

        The server thread calls recv on the same class, so the spy must
        discriminate: an AF_UNIX socket that CONNECTED reports the server path
        from getpeername(), while an ACCEPTED socket reports empty string.
        Measured before this was written, not assumed. The spy delegates to the
        real recv and returns its value unchanged, so it observes without
        altering the behaviour it observes."""
        import socket as _s, threading
        entered = threading.Event()
        real = _s.socket.recv

        def spy(sock, *a, **k):
            try:
                if sock.getpeername() == path:
                    entered.set()
            except OSError:
                pass
            return real(sock, *a, **k)
        patcher = mock.patch.object(_s.socket, "recv", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return entered

    def _budget_spy(self, path, tick):
        """Record (clock, timeout) per phase ON A DETERMINISTIC CLOCK.

        THREE WITNESSES DIED HERE, each weaker than it looked, so the history
        is the documentation:
          1. a wall-clock CEILING — graded the scheduler, not the code.
          2. strictly DECREASING values — a per-phase implementation on
             a descending schedule resets its clock and still decreases.
             Measured [0.45, 0.4], passing.
          3. expiry convergence WITHIN A SLACK — again from review, and this one is
             subtle: _left() reads monotonic, then the patched settimeout
             enters this spy and reads monotonic AGAIN. Preemption between
             those two reads is scheduler-dependent, so any tolerance is a
             wall-clock bound wearing a new name.

        The cure is to remove the second read. time.monotonic is replaced by a
        counter that ADVANCES ONLY IN THIS SPY, after recording, so the value
        this spy stores is exactly the one _left() just used. A shared deadline
        then satisfies clock + timeout == THE SAME NUMBER every phase, compared
        with assertEqual and NO tolerance. Measured: real [0.5, 0.5]; flat
        per-phase [0.5, 0.7]; descending per-phase [0.45, 0.6].

        Real time still bounds the sockets — only the deadline arithmetic is
        deterministic — so an arm measuring its own wall duration must use
        perf_counter, which this does not touch.

        `tick` IS PER-ARM AND MUST BE CHOSEN, not defaulted. A review caught a
        fixed 0.2 truncating the trickle arm from 27 phases to 3: the fake
        clock reached the budget after three settimeout calls, so the arm
        stopped exercising a trickle at all while still passing. The trade is
        real and runs both ways. A SMALL tick preserves many phases but costs
        REAL seconds in any arm whose recv blocks for its whole remaining
        budget; a LARGE tick terminates fast but starves an arm that needs
        iterations. Measured on the trickle: tick 0.2 gives 3 phases in 0.020s
        real, tick 0.02 gives 25 phases in 0.463s real, and the expiry is
        exactly {0.5} either way — so shrinking it costs runtime, never
        precision.
        """
        import socket as _s, time as _t
        seen = []
        box = {"t": 0.0}
        real_settimeout = _s.socket.settimeout

        def fake_monotonic():
            return box["t"]

        def spy(sock, value):
            mine = False
            try:
                mine = sock.getpeername() == path
            except OSError:
                pass
            if mine and value is not None:
                seen.append((box["t"], value))
                box["t"] += tick
            return real_settimeout(sock, value)
        clock_patch = mock.patch.object(_t, "monotonic", fake_monotonic)
        spy_patch = mock.patch.object(_s.socket, "settimeout", spy)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        spy_patch.start()
        self.addCleanup(spy_patch.stop)
        return seen

    # TWO TICKS, because the arms have opposite needs and one value cannot
    # serve both. A DRAIN arm blocks in recv for its whole remaining budget, so
    # each extra phase costs that much REAL time — it wants a coarse tick and
    # only needs the two phases it has. A TRICKLE arm is fed keepalives every
    # 20ms, so phases are cheap and its whole point is that MANY re-arms still
    # share one deadline — it wants a fine tick. Exactness is unaffected either
    # way; only runtime and phase count change.
    _TICK_COARSE_S = 0.2
    _TICK_FINE_S = 0.02

    def assertBudgetIsShared(self, budgets):
        """ONE ABSOLUTE EXPIRY, compared EXACTLY — no tolerance anywhere.

        On the deterministic clock installed by _budget_spy, clock + timeout is
        the deadline the code computed, read at the instant it computed it. A
        shared deadline yields one number for every phase. A tolerance here
        would re-admit the scheduler dependence this witness exists to remove.
        """
        self.assertGreaterEqual(
            len(budgets), 2,
            "only %d timeout(s) were set, so no phase boundary was crossed and "
            "sharing cannot be observed at all" % len(budgets))
        expiries = {at + value for at, value in budgets}
        self.assertEqual(
            len(expiries), 1,
            "the phases derived %d DIFFERENT deadlines (%r) from timeouts %r, "
            "so each RE-DERIVED its own expiry instead of sharing one. Note a "
            "DESCENDING per-phase schedule passes a decreasing-values check "
            "while failing this one, which is why the assertion is on the sum"
            % (len(expiries), sorted(round(e, 6) for e in expiries),
               [round(v, 3) for _, v in budgets]))

    def _send_spy(self, path):
        """Witness whether the CLIENT blocked in sendall until its budget died.

        The must-miss arm used to infer "send exhausted the budget" from the
        server not having drained the whole frame. That is not the same claim:
        an incomplete drain is equally consistent with a merely SLOW server
        while the client sailed through sendall — a review's point, and it is the same
        error as inferring recv-entry from a server timestamp, one arm along.

        A raise out of the client's own sendall is the direct observation. The
        spy delegates and re-raises, changing nothing, and identifies the
        client socket by peer name exactly as _recv_spy does.
        """
        import socket as _s
        state = {"entered": False, "timed_out": False}
        real = _s.socket.sendall

        def spy(sock, *a, **k):
            mine = False
            try:
                mine = sock.getpeername() == path
            except OSError:
                pass
            if mine:
                state["entered"] = True
            try:
                return real(sock, *a, **k)
            except _s.timeout:
                if mine:
                    state["timed_out"] = True
                raise
        patcher = mock.patch.object(_s.socket, "sendall", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return state

    def _assert_send_blocks(self, size):
        """A precondition for the MUST-MISS arm only — NOT a sharing proof.

        This once carried the discrimination and cannot. A probe measured why:
        buffers < payload proves only that the frame cannot be resident without
        reads, and under CONCURRENT DRAINING it puts no lower bound on sendall.
        Sharing is proved by assertBudgetIsShared instead. What survives is
        narrow and still worth asserting — the send-exhausts-budget arm needs
        sendall to be capable of blocking at all, and on a box with buffers
        above the payload it never would.

        A review was right, and my refutation was the thing that was wrong: I argued the
        0.62 ceiling rejects a per-phase mutant "structurally, because recv
        caps at 65536 so 1.5MB needs >=23 sipped iterations". That measures
        the SERVER DRAIN, not the CLIENT SEND duration, and the two are only
        coupled while the payload EXCEEDS the socket buffers. On a box with
        buffers >= the payload, sendall returns at once, the mutant's send
        phase collapses to ~0, per-phase totals ~0.5s and PASSES the ceiling —
        the arm goes quietly vacuous. Measured here: SO_SNDBUF 212992 +
        SO_RCVBUF 212992 vs a 1500170-byte frame, and sendall with no reader
        blocked for a full 5s. That is an ENVIRONMENTAL precondition, so it is
        asserted rather than assumed, and a tuned box fails loudly instead."""
        import socket as _s
        a, b = _s.socketpair(_s.AF_UNIX, _s.SOCK_STREAM)
        try:
            room = (a.getsockopt(_s.SOL_SOCKET, _s.SO_SNDBUF)
                    + b.getsockopt(_s.SOL_SOCKET, _s.SO_RCVBUF))
        finally:
            a.close(); b.close()
        self.assertLess(room, size,
                        "socket buffers (%d) hold the whole %d-byte frame, so "
                        "sendall will NOT block and every shared-budget arm "
                        "here is vacuous — raise the payload above the buffers"
                        % (room, size))

    @staticmethod
    def _trickle(conn, _req, stop):
        """Keepalives forever, never an answer — the defect's own specimen.

        Ends at teardown via `stop` rather than spinning to process exit."""
        import json as _j
        while not stop.wait(0.02):
            try:
                conn.sendall((_j.dumps({"_keepalive": True}) + "\n").encode())
            except OSError:
                return

    def _call(self, path, timeout):
        """Drive the REAL adapter through its OWN accessors — _user_data_path
        is a staticmethod reading ORCA_USER_DATA_PATH, so the env var IS the
        supported seam. An earlier draft invented harness.Harness(), which does
        not exist and would have errored before its first assertion."""
        import os as _os
        from unittest import mock
        from helm import harness
        with mock.patch.dict(_os.environ,
                             {"ORCA_USER_DATA_PATH": self._userdata(path)}):
            _os.environ.pop("HELM_ORCA_RPC", None)     # never the kill-switch
            return harness.OrcaAdapter()._runtime_call(
                "terminal.list", {}, timeout=timeout)

    def test_a_keepalive_trickle_is_bounded_by_wall_clock_and_named(self):  # noqa: VACUOUS_ASSERTION — assertBudgetIsShared and the two assertIn on the raised text are unconditional positive controls on this same call; the valid-reply test below is the sibling control proving the deadline did not simply break the RPC. The elapsed check is now only a hang guard and is deliberately not load-bearing.
        """THE DEFECT'S OWN SPECIMEN: keepalives forever, never an answer."""
        import time as _t
        from helm import harness
        # PART ONE — PRODUCTION TIMING, REAL CLOCK, NOTHING PATCHED:
        # a deterministic clock proves arithmetic and exercises no production
        # timing at all, so the two must be separate calls rather than one call
        # doing both badly. This call is the only place the deadline meets real
        # time. The bound is deliberately BROAD — it detects a hang and grades
        # nothing else, because any tight ratio here grades the scheduler.
        # Pre-fix this ran forever; with only the recv loop bounded it still
        # reached ~3x on connect+send.
        real_path = self._daemon(self._trickle)
        started = _t.perf_counter()
        with self.assertRaises(harness.HarnessError) as cm:
            self._call(real_path, timeout=0.5)
        elapsed = _t.perf_counter() - started
        self.assertGreaterEqual(elapsed, 0.4, "must not return early")
        self.assertLess(elapsed, 5.0,
                        "%.2fs is a hang, not a bounded expiry" % elapsed)
        self.assertIn("deadline exceeded", str(cm.exception))
        self.assertIn("terminal.list", str(cm.exception))

        # PART TWO — THE ARITHMETIC, on its own call and its own daemon. A
        # trickle re-arms the socket on every keepalive, so a daemon able to
        # EXTEND the deadline would push each phase's expiry forward; one
        # instant shared across many reads is the property, and it holds on any
        # box. The phase-count assertion comes first because a clock tick too
        # coarse for the budget silently truncates the loop — measured, 27
        # phases down to 3, still passing.
        path = self._daemon(self._trickle)
        budgets = self._budget_spy(path, self._TICK_FINE_S)
        with self.assertRaises(harness.HarnessError):
            self._call(path, timeout=0.5)
        self.assertGreaterEqual(
            len(budgets), 10,
            "only %d phase(s) were observed, so the keepalives did not drive a "
            "re-arm loop and this is no longer a trickle test — check the clock "
            "tick against the budget" % len(budgets))
        self.assertBudgetIsShared(budgets)


    def test_send_and_recv_SHARE_one_budget_not_one_each(self):  # noqa: VACUOUS_ASSERTION — the elapsed window and the two assertIn on the raised text are unconditional positive controls on this same call; test_a_valid_reply_is_returned_green proves the RPC can still succeed, so this raise is a decision about the deadline and not an RPC that can only fail.
        """THE SHARING PROPERTY, which no earlier arm proved.

        The daemon drains the 1.5MB request slowly (~0.2s of a 0.5s budget)
        and then goes silent, so recv must live on the REMAINDER.

        The discrimination is proved by an ARM, not by this docstring:
        test_a_per_phase_implementation_is_REJECTED_by_the_budget_arithmetic runs the
        mutation and fails if the per-phase shape ever produces a spent-down
        budget.

        TWO WRONG VERSIONS OF THIS DOCSTRING ARE RECORDED so the reasoning is
        not repeated. The first claimed a 0.62s ceiling discriminates
        "STRUCTURALLY, because recv caps at 65536 so 1.5MB needs >=23 sipped
        iterations" — that measures SERVER DRAIN, not CLIENT SEND duration.
        The second kept the ceiling and merely ASSERTED buffers < payload as a
        precondition. A probe measured why that also fails: buffers < payload
        proves only that the frame cannot sit resident without reads, and under
        concurrent draining it puts NO lower bound on sendall. Any wall-time
        ceiling grades the scheduler.

        So sharing is no longer proved by the clock at all. assertBudgetIsShared
        reads the timeout VALUES handed to each phase: a shared deadline spends
        one budget DOWN, a per-phase one re-issues it. Host-independent."""
        import time as _t, os as _os
        from unittest import mock
        from helm import harness
        path, size, drained, w = self._slow_drain_then_silent()
        ud = self._userdata(path)
        entered_recv = self._recv_spy(path)
        budgets = self._budget_spy(path, self._TICK_COARSE_S)
        big = {"pad": "x" * size}
        started = _t.perf_counter()
        with mock.patch.dict(_os.environ, {"ORCA_USER_DATA_PATH": ud}):
            _os.environ.pop("HELM_ORCA_RPC", None)
            with self.assertRaises(harness.HarnessError) as cm:
                harness.OrcaAdapter()._runtime_call("terminal.list", big,
                                                    timeout=0.5)
        elapsed = _t.perf_counter() - started
        # PHASE WITNESSES FIRST, TIMING SECOND. Grading wall time without
        # knowing WHICH phase ran is how scheduler variance lets send eat the
        # whole budget while a fresh-per-op mutant stays green.
        self.assertTrue(drained.is_set(),
                        "the request must have been FULLY drained — otherwise "
                        "send never completed and this arm is timing an "
                        "unknown phase")
        # THE ERROR TEXT IS NOT A RECV-ENTRY WITNESS, and claiming it was is the
        # same defect this lane keeps growing. harness.py evaluates
        # _left("recv") on the line BEFORE conn.recv(), so an expiry raises
        # "at recv" WITHOUT recv ever being entered — measured. The
        # phase label proves which budget was consulted, never which call ran.
        # I had already built the client-side spy for fleet's sibling arm and
        # wired it to exactly one of the two callers.
        self.assertIn("at recv", str(cm.exception),
                      "the expiry must name the RECV phase — this pins WHICH "
                      "budget was consulted, not that recv ran")
        self.assertTrue(entered_recv.is_set(),
                        "the client never entered recv, so this expiry is the "
                        "post-send deadline CHECK rather than the shared-budget "
                        "property the arm claims")
        self.assertBudgetIsShared(budgets)
        # WALL TIME IS ONLY A HANG BOUND NOW. It no longer grades sharing —
        # that is assertBudgetIsShared's job, and it is host-independent.
        self.assertGreaterEqual(elapsed, 0.4, "the budget must be spent")
        self.assertLess(elapsed, 5.0,
                        "%.2fs is a hang, not a bounded expiry" % elapsed)
        self.assertIn("deadline exceeded", str(cm.exception))
        self.assertIn("terminal.list", str(cm.exception))

    def test_a_per_phase_implementation_is_REJECTED_by_the_budget_arithmetic(self):  # noqa: VACUOUS_ASSERTION — an unconditional assertEqual on a REAL reply through the MUTANT module runs first in this method, against a valid-reply daemon, so the assertRaises below is a decision about the deadline and not a module that can only fail; the heuristic cannot see the control across the two daemons, same blind spot the fleet sibling documents. The rejection is on BUDGET ARITHMETIC — the mutant re-issues a full budget so its values do not decrease — never on wall time.
        """THE PRESERVED MUTANT — a review asked for it, rightly.

        Every other arm here asserts what the CURRENT code does. None of them
        can tell you the witness would REJECT the shape it exists to reject,
        and that is the only property that makes the witness worth having. I
        had proved it in a throwaway probe and quoted the numbers in a commit
        message, which is precisely the move that leaves the artifact
        unchecked: the probe evaporates, the arm ships unproven.

        So the mutation runs HERE. The real harness source is rewritten so all
        three settimeout calls take a FRESH FULL budget — the per-phase shape —
        and its budgets must NOT decrease. If a future edit ever lets the
        per-phase shape spend one budget down, this arm goes red instead of the
        shared-budget arms going quietly vacuous. Nothing here grades wall time:
        A probe measured that any clock ceiling grades the scheduler, so the only
        time bound left in this method is a hang guard."""
        import os as _os, sys as _sys, time as _t, types
        from unittest import mock
        from helm import harness
        src = open(harness.__file__).read()          # DERIVED, never transcribed
        mutant = src
        for site in ('conn.settimeout(_left("connect"))',
                     'conn.settimeout(_left("send"))',
                     'conn.settimeout(_left("recv"))'):
            self.assertIn(site, src,
                          "the deadline seam moved; this mutation is stale "
                          "and would prove nothing: %s" % site)
            mutant = mutant.replace(site, 'conn.settimeout(timeout)')
        self.assertEqual(mutant.count('conn.settimeout(timeout)'), 3,
                         "the mutation did not apply to all three phases, so "
                         "a green here would be meaningless")

        mod = types.ModuleType("helm_harness_perphase_mutant")
        mod.__file__ = harness.__file__
        self.addCleanup(_sys.modules.pop, mod.__name__, None)
        _sys.modules[mod.__name__] = mod
        exec(compile(mutant, harness.__file__, "exec"), mod.__dict__)

        # POSITIVE CONTROL ON THE MUTANT ITSELF, unconditional and first. If
        # the exec above produced a broken module, EVERY call through it would
        # raise and the deadline assertion below would pass for entirely the
        # wrong reason — which is this lane's signature failure. So prove the
        # mutant can still SUCCEED before letting it prove anything by failing.
        import json as _j
        def answer(conn, req, _stop):
            rid = _j.loads(req.decode().strip())["id"]
            conn.sendall((_j.dumps({"id": rid, "ok": True,
                                    "result": {"terminals": ["t1"]}}) + "\n")
                         .encode())
        with mock.patch.dict(_os.environ,
                             {"ORCA_USER_DATA_PATH":
                              self._userdata(self._daemon(answer))}):
            _os.environ.pop("HELM_ORCA_RPC", None)
            alive = mod.OrcaAdapter()._runtime_call("terminal.list", {},
                                                    timeout=2.0)
        self.assertEqual(alive, {"terminals": ["t1"]},
                         "the MUTANT module cannot complete a valid RPC, so a "
                         "raise below would say nothing about the deadline")

        path, size, drained, w = self._slow_drain_then_silent()
        ud = self._userdata(path)
        budgets = self._budget_spy(path, self._TICK_COARSE_S)
        started = _t.perf_counter()
        with mock.patch.dict(_os.environ, {"ORCA_USER_DATA_PATH": ud}):
            _os.environ.pop("HELM_ORCA_RPC", None)
            with self.assertRaises(mod.HarnessError):
                mod.OrcaAdapter()._runtime_call("terminal.list",
                                                {"pad": "x" * size},
                                                timeout=0.5)
        elapsed = _t.perf_counter() - started
        self.assertTrue(drained.is_set(),
                        "the mutant never finished sending, so this measures "
                        "an unknown phase")
        # THE MUTANT IS REJECTED ON ARITHMETIC, NOT ON THE CLOCK. It re-issues
        # the FULL budget to each phase, so its values do not decrease. That
        # comparison holds on any box; the wall bound below only catches a hang.
        self.assertGreaterEqual(len(budgets), 2,
                                "the mutant never crossed a phase boundary")
        expiries = [at + v for at, v in budgets]
        self.assertNotEqual(
            len(set(expiries)), 1,
            "the per-phase mutant derived ONE deadline (%r), so "
            "assertBudgetIsShared cannot tell it from a shared budget and "
            "every arm resting on it is vacuous"
            % sorted(set(round(e, 6) for e in expiries)))
        self.assertLess(
            elapsed, 5.0,
            "the mutant took %.2fs, which is a hang rather than a bounded "
            "per-phase run" % elapsed)

    def test_a_DESCENDING_per_phase_schedule_is_still_rejected(self):  # noqa: VACUOUS_ASSERTION — an unconditional assertEqual on a REAL reply through the descending MUTANT module runs first in this method, so the assertRaises below is a decision about the deadline and not a module that can only fail; the spy is armed AFTER that control, so its settimeouts are excluded from the budgets under assertion.
        """THE COUNTEREXAMPLE THAT KILLED THE PREVIOUS WITNESS, kept as an arm.

        My first non-timing witness asserted the timeout VALUES strictly
        decrease. A review refuted it in one sentence: a per-phase implementation
        on a DESCENDING SCHEDULE resets its clock every phase and still
        decreases. It would have sailed through, and the arms would have been
        as vacuous as the wall-clock ceiling they replaced.

        Measured against exactly that mutant: values [0.45, 0.4] — strictly
        decreasing — with the phase deadlines 0.161s apart. So the property is
        the ABSOLUTE EXPIRY, and this arm exists so that whoever weakens
        assertBudgetIsShared back to a monotonicity check finds out here rather
        than in production. It asserts BOTH halves: the mutant does decrease
        (so the weak check would pass it) AND its expiries diverge (so the real
        check rejects it).
        """
        import os as _os, sys as _sys, time as _t, types
        from unittest import mock
        from helm import harness
        src = open(harness.__file__).read()
        mutant = (src
                  .replace('conn.settimeout(_left("connect"))',
                           'conn.settimeout(timeout)')
                  .replace('conn.settimeout(_left("send"))',
                           'conn.settimeout(timeout * 0.9)')
                  .replace('conn.settimeout(_left("recv"))',
                           'conn.settimeout(timeout * 0.8)'))
        self.assertEqual(
            mutant.count("conn.settimeout(timeout"), 3,
            "the descending mutation did not apply to all three phases, so a "
            "green here would be meaningless")
        mod = types.ModuleType("helm_harness_descending_mutant")
        mod.__file__ = harness.__file__
        self.addCleanup(_sys.modules.pop, mod.__name__, None)
        _sys.modules[mod.__name__] = mod
        exec(compile(mutant, harness.__file__, "exec"), mod.__dict__)

        # POSITIVE CONTROL ON THIS MUTANT, unconditional and first — the same
        # control the sibling mutant arm needed. A broken exec makes EVERY call
        # raise, and the assertRaises below would then pass for entirely the
        # wrong reason.
        import json as _j
        def answer(conn, req, _stop):
            rid = _j.loads(req.decode().strip())["id"]
            conn.sendall((_j.dumps({"id": rid, "ok": True,
                                    "result": {"terminals": ["t1"]}}) + "\n")
                         .encode())
        with mock.patch.dict(_os.environ,
                             {"ORCA_USER_DATA_PATH":
                              self._userdata(self._daemon(answer))}):
            _os.environ.pop("HELM_ORCA_RPC", None)
            alive = mod.OrcaAdapter()._runtime_call("terminal.list", {},
                                                    timeout=2.0)
        self.assertEqual(alive, {"terminals": ["t1"]},
                         "the descending mutant cannot complete a valid RPC, "
                         "so a raise below would say nothing about its budgets")

        path, size, drained, w = self._slow_drain_then_silent()
        ud = self._userdata(path)
        budgets = self._budget_spy(path, self._TICK_COARSE_S)
        with mock.patch.dict(_os.environ, {"ORCA_USER_DATA_PATH": ud}):
            _os.environ.pop("HELM_ORCA_RPC", None)
            with self.assertRaises(mod.HarnessError):
                mod.OrcaAdapter()._runtime_call("terminal.list",
                                                {"pad": "x" * size},
                                                timeout=0.5)
        self.assertGreaterEqual(len(budgets), 2,
                                "the mutant never crossed a phase boundary")
        values = [v for _, v in budgets]
        # HALF ONE: the weak witness WOULD have passed this.
        self.assertTrue(
            all(b < a for a, b in zip(values, values[1:])),
            "this mutant no longer produces a descending schedule (%r), so it "
            "is not the counterexample this arm exists to pin"
            % ([round(v, 3) for v in values],))
        # HALF TWO: the real witness rejects it anyway.
        expiries = [at + v for at, v in budgets]
        self.assertNotEqual(
            len(set(expiries)), 1,
            "a per-phase implementation derived ONE expiry (%r), which should "
            "be impossible — assertBudgetIsShared can no longer tell shared "
            "from per-phase and every arm resting on it is vacuous"
            % sorted(set(round(e, 6) for e in expiries)))

    def test_fleet_shares_one_budget_across_send_and_recv_too(self):  # noqa: VACUOUS_ASSERTION — the elapsed window is the unconditional control, and the sibling arm above proves fleet returns a real presence on a valid reply, so None here is a decision rather than a function that only returns None.
        """fleet's sibling needs its own send arm — a review, correctly:
        the harness arm says nothing about fleet's copy of the same loop."""
        import time as _t, os as _os
        from unittest import mock
        from helm import fleet
        path, size, drained, w = self._slow_drain_then_silent()
        ud = self._userdata(path)
        entered_recv = self._recv_spy(path)
        budgets = self._budget_spy(path, self._TICK_COARSE_S)
        started = _t.perf_counter()
        with mock.patch.dict(_os.environ, {}):
            out = fleet._orca_runtime_call(ud, "rt-1", "terminal.list",
                                           {"pad": "x" * size}, timeout=0.5)
        elapsed = _t.perf_counter() - started
        # fleet returns None with NO message, so it has no error text to
        # witness the phase — the drain Event is the explicit phase witness
        # a review asked for, and it must hold before any timing is graded.
        self.assertTrue(drained.is_set(),
                        "fleet's request must have been FULLY drained, or the "
                        "timing below is about an unknown phase")
        self.assertIsNone(out, "expiry is None here, per fleet's contract")
        # RECV-ENTRY, witnessed ON THE CLIENT — a review, twice, and right both
        # times. drained+None cannot tell a recv expiry from a post-send
        # _left<=0, because fleet returns None with no text. My first fix timed
        # the SERVER drain and inferred the client's state from it, which is the
        # same mistake one layer along: the client can be descheduled after
        # sendall, resume past the deadline, and return at the post-send _left
        # check having never called recv. Only the client can witness the
        # client, so recv is spied transparently (it delegates, changing
        # nothing) and the CLIENT socket is identified by its peer name — an
        # AF_UNIX connector reports the server path, an accepted socket reports
        # empty, verified before this was built.
        self.assertTrue(entered_recv.is_set(),
                        "fleet returned None without the client ever entering "
                        "recv — that is a post-send deadline check, not the "
                        "shared-budget property this arm claims to prove")
        self.assertBudgetIsShared(budgets)
        self.assertGreaterEqual(elapsed, 0.4)
        self.assertLess(elapsed, 5.0,
                        "%.2fs is a hang, not a bounded expiry" % elapsed)

    def test_a_valid_reply_is_returned_green(self):
        """MUST-HIT CONTROL, and it proves a VALID reply — not merely that
        some error other than the deadline was raised. The daemon ECHOES the
        request id, so this exercises the real success path; an id-mismatch
        control would have passed against an RPC that could never succeed."""
        import json as _j
        def answer(conn, req, _stop):
            rid = _j.loads(req.decode().strip())["id"]
            conn.sendall((_j.dumps({"_keepalive": True}) + "\n").encode())
            conn.sendall((_j.dumps({"id": rid, "ok": True,
                                    "result": {"terminals": ["t1"]}}) + "\n")
                         .encode())
        result = self._call(self._daemon(answer), timeout=2.0)
        self.assertEqual(result, {"terminals": ["t1"]})

    def test_a_send_that_eats_the_budget_never_reaches_recv(self):  # noqa: VACUOUS_ASSERTION — assertFalse on the recv Event is the MUST-MISS half of a pair; its must-hit sibling (test_fleet_shares_one_budget_across_send_and_recv_too) sets the same Event unconditionally on the same spy, so the witness is proven to report both values rather than being decorative.
        """THE MUST-MISS HALF, and I had already run this by hand.

        The recv witness in the sibling arm is a must-HIT: it asserts the client
        entered recv. On its own that proves nothing, because an Event that is
        set unconditionally — by a spy that fires on the wrong socket, or one
        left permanently set — leaves every arm green and reads exactly like a
        working witness. Only a case where the Event must stay CLEAR shows it
        discriminates.

        I proved this pair in a throwaway probe and shipped only the must-hit,
        which is the same habit that put a false docstring in this file: the
        thing I checked was not the thing I shipped. So the must-miss lives
        here now. Measured, both halves, one millisecond apart in elapsed:
            normal drain          out=None elapsed=0.502s entered-recv=True
            send eats the budget  out=None elapsed=0.503s entered-recv=False
        Same return, same duration, opposite witness — which is precisely the
        pair the arm could not tell apart before the spy existed."""
        import time as _t, os as _os
        from unittest import mock
        from helm import fleet
        # Drain 20x slower: the budget dies DURING send, so the client returns
        # at the post-send _left check and recv is never called.
        path, size, drained, w = self._slow_drain_then_silent(sip=0.20)
        ud = self._userdata(path)
        entered_recv = self._recv_spy(path)
        sent = self._send_spy(path)
        started = _t.perf_counter()
        with mock.patch.dict(_os.environ, {}):
            out = fleet._orca_runtime_call(ud, "rt-1", "terminal.list",
                                           {"pad": "x" * size}, timeout=0.5)
        elapsed = _t.perf_counter() - started
        self.assertIsNone(out, "fleet still reports expiry as None")
        # THE CLIENT MUST HAVE BLOCKED IN SEND, witnessed on the client. An
        # undrained server is NOT that claim: a slow server and a blocked
        # sendall produce the same partial drain, and only one of them is the
        # case this arm exists for.
        self.assertTrue(sent["entered"], "the client never reached sendall")
        self.assertTrue(
            sent["timed_out"],
            "sendall returned before the budget died, so the client did NOT "
            "block in send and this is not the send-exhausts-budget case — an "
            "undrained server alone would have let this arm pass anyway")
        self.assertFalse(
            drained.is_set(),
            "corroboration only: the server should not have seen the whole "
            "frame either")
        self.assertFalse(
            entered_recv.is_set(),
            "the client entered recv even though send consumed the budget — "
            "the recv witness fires unconditionally and its must-hit sibling "
            "is therefore decorative")
        # BOUNDED: the expiry must be the SEND deadline, not a hang. Without an
        # upper bound this arm would be satisfied by never finishing.
        self.assertGreaterEqual(elapsed, 0.4, "the budget must be spent")
        self.assertLess(elapsed, 5.0,
                        "%.2fs is a hang, not a bounded send expiry — this is "
                        "only a hang guard; sent[timed_out] above carries the "
                        "property" % elapsed)

    def test_fleet_sibling_is_bounded_and_returns_none_per_its_contract(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone is paired with an unconditional assertIsNotNone + assertEqual on the SAME call against a valid-reply daemon, later in this method; the presence control exists, the heuristic cannot see it across the two daemons.
        """fleet._orca_runtime_call carries the SAME defect and the OPPOSITE
        honest shape: its documented contract is "None on any uncertainty", so
        a deadline expiry returns None rather than raising. Copying the
        harness's raise into it would have broken its callers."""
        import time as _t, os as _os
        from unittest import mock
        from helm import fleet
        # PART ONE — PRODUCTION TIMING, REAL CLOCK, NOTHING PATCHED. Separate
        # from the arithmetic call below, per review: a deterministic clock
        # exercises no production timing, so one call cannot serve both. Broad
        # bound only — it detects a hang and grades nothing else.
        real_path = self._daemon(self._trickle)
        started = _t.perf_counter()
        with mock.patch.dict(_os.environ, {}):
            real_out = fleet._orca_runtime_call(
                self._userdata(real_path), "rt-1", "terminal.list", {},
                timeout=0.5)
        elapsed = _t.perf_counter() - started
        self.assertIsNone(real_out, "expiry must be None, not a raise, here")
        self.assertGreaterEqual(elapsed, 0.4, "must not return early")
        self.assertLess(elapsed, 5.0,
                        "%.2fs is a hang, not a bounded expiry" % elapsed)

        # PART TWO — THE ARITHMETIC, its own call and its own daemon.
        path = self._daemon(self._trickle)
        ud = self._userdata(path)
        budgets = self._budget_spy(path, self._TICK_FINE_S)
        with mock.patch.dict(_os.environ, {}):
            out = fleet._orca_runtime_call(ud, "rt-1", "terminal.list", {},
                                           timeout=0.5)
        self.assertIsNone(out, "expiry must be None here too")
        # SAME CURE AS ITS SIBLINGS. <0.9 against a 0.5s budget was a 1.8x wall
        # ratio grading the scheduler; a review ruled the class, not one arm, so
        # it applies here too. Boundedness is the shared EXPIRY.
        # THE TRICKLE MUST ACTUALLY HAVE TRICKLED. A review caught a fixed clock
        # tick truncating this arm from 27 phases to 3 — it kept passing while
        # no longer exercising a trickle at all. Asserting the phase COUNT is
        # what makes that visible: a re-arm loop proving one shared deadline
        # across many reads is the whole point, and three reads is not it.
        self.assertGreaterEqual(
            len(budgets), 10,
            "only %d phase(s) were observed, so the daemon's keepalives did "
            "not drive a re-arm loop and this arm is no longer a trickle test "
            "— check the clock tick against the budget" % len(budgets))
        self.assertBudgetIsShared(budgets)

        # POSITIVE CONTROL ON THE SAME OBSERVABLE — without it, a fleet that
        # returned None unconditionally would pass the assertion above. This
        # is finding (5) one arm over: an absence proves nothing until the
        # same call is shown able to produce a presence.
        import json as _j
        def answer(conn, req, _stop):
            rid = _j.loads(req.decode().strip())["id"]
            # fleet REQUIRES _meta.runtimeId to match; harness only checks it
            # when present. My first control omitted it, fleet correctly
            # returned None, and my own vacuity guard caught the control being
            # unable to produce a presence — the arm was right, the fixture
            # was wrong.
            conn.sendall((_j.dumps({"id": rid, "ok": True,
                                    "_meta": {"runtimeId": "rt-1"},
                                    "result": {"terminals": ["t1"]}}) + "\n")
                         .encode())
        ok = fleet._orca_runtime_call(
            self._userdata(self._daemon(answer)), "rt-1", "terminal.list", {},
            timeout=2.0)
        self.assertIsNotNone(ok, "the SAME call must be able to return a "
                                 "presence, or the assertIsNone above is a "
                                 "statement about a function that only ever "
                                 "returns None")
        self.assertEqual(ok, {"terminals": ["t1"]})
