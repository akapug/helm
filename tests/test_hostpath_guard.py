#!/usr/bin/env python3
"""The pre-push host-path guard — refuse public pushes carrying host paths.

NO REAL HOST PATHS IN FIXTURES — every test path uses /home/test-user/,
/Users/dev/, or similar generic names. The test data lives in these fixtures
deliberately; the guard's regex IS expected to match them (they ARE host-path
shapes, by design).
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from helm import hostpath_guard


_PAT = hostpath_guard._HOSTPATH_RE
_ZERO = "0000000000000000000000000000000000000000"


class PatternTest(unittest.TestCase):
    """The regex — shape only, never a literal path."""

    def test_home_paths_match(self):
        self.assertTrue(_PAT.search("/home/alice/projects/foo"))
        self.assertTrue(_PAT.search("/home/bob-dev_2/.config/app"))
        self.assertTrue(_PAT.search("/Users/carol/Documents/report"))

    def test_non_host_paths_do_not_match(self):
        self.assertIsNone(_PAT.search("/tmp/build"))
        self.assertIsNone(_PAT.search("/etc/config"))
        self.assertIsNone(_PAT.search("/usr/local/bin"))
        self.assertIsNone(_PAT.search("/var/log"))
        self.assertIsNone(_PAT.search("/opt/app"))

    def test_home_without_username_does_not_match(self):
        self.assertIsNone(_PAT.search("/home/"))


class TestGitRepo(unittest.TestCase):
    """Git-backed tests — commit content and scan via ls-tree."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-hp-")
        os.environ["HELM_HOSTPATH_SKIP"] = "0"
        subprocess.run(["git", "-C", self.tmp, "init", "-q"], check=True)
        subprocess.run(["git", "-C", self.tmp, "config", "user.email",
                        "test@example.com"], check=True)
        subprocess.run(["git", "-C", self.tmp, "config", "user.name",
                        "Test"], check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, relpath, content):
        path = os.path.join(self.tmp, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def _commit(self, msg):
        subprocess.run(["git", "-C", self.tmp, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.tmp, "commit", "-q", "-m", msg],
                       check=True)
        return subprocess.run(["git", "-C", self.tmp, "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()

    def test_host_path_in_content_is_detected(self):
        """The incident shape: a tracked file carries a host path in its
        content — the new-tree scanner catches it even in the first commit."""
        self._write("tests/config.py",
                    'HOME = "/home/test-user/.config/app"\n')
        new = self._commit("add host path")
        hits, _notes = hostpath_guard.scan_push(
            self.tmp, _ZERO, new, "origin")
        self.assertTrue(hits, "host path in content MUST be detected")
        self.assertIn("config.py", hits[0][0])

    def test_clean_content_passes(self):
        self._write("src/main.py", 'print("hello world")\n')
        new = self._commit("clean commit")
        hits, _notes = hostpath_guard.scan_push(
            self.tmp, _ZERO, new, "origin")
        self.assertEqual(hits, [])

    def test_MUST_NOT_HIT_paths_like_tmp_or_etc(self):
        self._write("src/log.py", 'OUT = "/tmp/build/output.log"\n'
                    'CFG = "/etc/myapp/config"\n'
                    'BIN = "/usr/local/bin/tool"\n')
        new = self._commit("non-host paths")
        hits, _notes = hostpath_guard.scan_push(
            self.tmp, _ZERO, new, "origin")
        self.assertEqual(hits, [], "non-host paths must not trigger guard")

    def test_multiple_host_paths_all_detected(self):
        self._write("a.py", 'P = "/home/alice/src/main.py"\n')
        self._write("b.py", 'Q = "/Users/bob/Library/Logs/app"\n')
        new = self._commit("two host paths")
        hits, _notes = hostpath_guard.scan_push(
            self.tmp, _ZERO, new, "origin")
        self.assertEqual(len(hits), 2)

    def test_direct_scan_ignores_skip_env(self):
        """scan_push() ignores HELM_HOSTPATH_SKIP — only main() checks it."""
        self._write("leak.py", 'H = "/home/test-user/secret"\n')
        new = self._commit("host path commit")
        with mock.patch.dict(os.environ, {"HELM_HOSTPATH_SKIP": "1"}):
            hits, _ = hostpath_guard.scan_push(
                self.tmp, _ZERO, new, "origin")
        self.assertTrue(hits, "scan_push must find hits regardless of SKIP")


class RemoteVisibilityTest(unittest.TestCase):
    """Public/private detection — UNKNOWN = public (fail safe)."""

    def test_unknown_remote_is_public(self):
        self.assertTrue(hostpath_guard._is_public(None))
        self.assertTrue(hostpath_guard._is_public(""))
        self.assertTrue(hostpath_guard._is_public(
            "git@unknown-host:owner/repo"))

    def test_non_github_remote_is_public(self):
        """gitlab, self-hosted — cannot determine = public."""
        self.assertTrue(hostpath_guard._is_public(
            "git@gitlab.com:owner/repo.git"))

    def test_github_private_is_not_public(self):
        with mock.patch.object(hostpath_guard.subprocess, "run") as mr:
            mr.return_value.returncode = 0
            mr.return_value.stdout = json.dumps({"isPrivate": True})
            self.assertFalse(hostpath_guard._is_public(
                "git@github.com:owner/private-repo.git"))

    def test_github_public_is_public(self):
        with mock.patch.object(hostpath_guard.subprocess, "run") as mr:
            mr.return_value.returncode = 0
            mr.return_value.stdout = json.dumps({"isPrivate": False})
            self.assertTrue(hostpath_guard._is_public(
                "https://github.com/owner/public-repo"))

    def test_gh_unavailable_treats_as_public(self):
        with mock.patch.object(hostpath_guard.subprocess, "run") as mr:
            mr.side_effect = OSError("gh not found")
            self.assertTrue(hostpath_guard._is_public(
                "git@github.com:owner/repo.git"))


class CliTest(unittest.TestCase):
    """The pre-push entry point — stdin handling."""

    def test_bad_flag_is_usage(self):
        rc = hostpath_guard.main(["--not-a-flag"])
        self.assertEqual(rc, 2)

    def test_empty_stdin_is_nothing_to_push_and_allows(self):
        """git feeds the pre-push hook ZERO ref lines on an up-to-date push —
        empty stdin means nothing-to-push, not a scan failure."""
        err = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("")):
            with contextlib.redirect_stderr(err):
                rc = hostpath_guard.main(["--pre-push"])
        self.assertEqual(rc, 0)
        self.assertIn("nothing to push", err.getvalue())

    def test_malformed_nonempty_stdin_still_refuses(self):
        """Control: NON-empty input that cannot be parsed refuses loudly —
        the honest-empty allowance must not swallow real parse failures."""
        err = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("one-lone-token\n")):
            with contextlib.redirect_stderr(err):
                rc = hostpath_guard.main(["--pre-push"])
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", err.getvalue())

    def test_whitespace_only_stdin_refuses(self):
        """Whitespace-only bytes are NON-empty malformed input, not an
        up-to-date push — git never emits them, so a stripped-to-empty
        read must refuse rather than launder into nothing-to-push."""
        err = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("  \n\t\n")):
            with contextlib.redirect_stderr(err):
                rc = hostpath_guard.main(["--pre-push"])
        self.assertEqual(rc, 2)
        self.assertIn("whitespace", err.getvalue())

    def test_three_field_line_is_malformed(self):
        """git pre-push feeds FOUR fields per ref; fewer is a broken feeder."""
        err = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(
                "refs/heads/main %s refs/heads/main\n" % ("a" * 40))):
            with contextlib.redirect_stderr(err):
                rc = hostpath_guard.main(["--pre-push"])
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", err.getvalue())

    def test_non_hex_REMOTE_sha_is_malformed(self):
        """Both sha fields are validated. scan_push ignores the remote sha
        today, so this is not a content bypass — but accepting malformed
        protocol input because the current consumer does not read it is the
        fail-open the chain refuses."""
        err = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(
                "refs/heads/main %s refs/heads/main not-a-sha\n" % ("a" * 40))):
            with contextlib.redirect_stderr(err):
                rc = hostpath_guard.main(["--pre-push"])
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", err.getvalue())

    def test_an_interior_blank_line_makes_the_whole_input_malformed(self):
        """git emits one four-field record per ref and never a blank line.
        Filtering a blank one would let it ride along with valid records —
        so the SAME bytes that refuse alone must refuse in company."""
        err = io.StringIO()
        body = ("refs/heads/a %s refs/heads/a %s\n"
                "   \n"
                "refs/heads/b %s refs/heads/b %s\n"
                % ("a" * 40, _ZERO, "b" * 40, _ZERO))
        with mock.patch("sys.stdin", io.StringIO(body)):
            with contextlib.redirect_stderr(err):
                rc = hostpath_guard.main(["--pre-push"])
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", err.getvalue())

    def test_a_trailing_newline_alone_is_not_a_blank_record(self):
        """Control for the line above: the ORDINARY terminal newline must
        still parse, or every real push would refuse."""
        seen = []

        def fake_scan(root, old, new, remote, remote_url=None):
            seen.append(new)
            return [], []

        with mock.patch("sys.stdin", io.StringIO(
                "refs/heads/main %s refs/heads/main %s\n" % ("a" * 40, _ZERO))):
            with mock.patch.object(hostpath_guard, "scan_push", fake_scan):
                with contextlib.redirect_stderr(io.StringIO()):
                    rc = hostpath_guard.main(["--pre-push"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen, ["a" * 40])

    def test_unreadable_stdin_refuses_WITH_a_diagnostic(self):
        """A read we could not perform is not an up-to-date push. rc 2 was
        already right; the operator was told nothing about why."""
        err = io.StringIO()
        broken = mock.Mock()
        broken.read.side_effect = OSError("Bad file descriptor")
        with mock.patch("sys.stdin", broken):
            with contextlib.redirect_stderr(err):
                rc = hostpath_guard.main(["--pre-push"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot read pre-push stdin", err.getvalue())
        self.assertIn("Bad file descriptor", err.getvalue())

    def test_sha256_repo_deletion_ref_is_recognised(self):
        """A SHA-256 repository feeds 64-hex shas; an all-zero 64-hex local
        sha is a deletion exactly as the 40-hex one is."""
        err = io.StringIO()

        def fail_scan(root, old, new, remote):  # pragma: no cover
            raise AssertionError("deletion ref must not be scanned")

        with mock.patch("sys.stdin", io.StringIO(
                "(delete) %s refs/heads/gone %s\n" % ("0" * 64, "c" * 64))):
            with mock.patch.object(hostpath_guard, "scan_push", fail_scan):
                with contextlib.redirect_stderr(err):
                    rc = hostpath_guard.main(["--pre-push"])
        self.assertEqual(rc, 0)
        self.assertIn("deletion", err.getvalue())

    def test_sha256_repo_normal_push_is_scanned(self):
        """Must-hit control beside the deletion arm: a real 64-hex sha is
        scanned, not mistaken for a deletion."""
        seen = []

        def fake_scan(root, old, new, remote, remote_url=None):
            seen.append(new)
            return [], []

        with mock.patch("sys.stdin", io.StringIO(
                "refs/heads/main %s refs/heads/main %s\n"
                % ("d" * 64, "0" * 64))):
            with mock.patch.object(hostpath_guard, "scan_push", fake_scan):
                with contextlib.redirect_stderr(io.StringIO()):
                    rc = hostpath_guard.main(["--pre-push"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen, ["d" * 64])

    def test_non_hex_local_sha_is_malformed(self):
        err = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(
                "refs/heads/main not-a-sha refs/heads/main %s\n" % _ZERO)):
            with contextlib.redirect_stderr(err):
                rc = hostpath_guard.main(["--pre-push"])
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", err.getvalue())

    def test_stdin_with_shas_and_fake_remote(self):
        """Without a real git repo, ls-tree fails -> rc=2 (fail-closed)."""
        with mock.patch("sys.stdin", io.StringIO(
                "refs/heads/main %s refs/heads/main %s\n"
                % ("a" * 40, _ZERO))):
            with mock.patch.object(hostpath_guard, "_remote_url",
                                   return_value="git@unknown:repo"):
                rc = hostpath_guard.main(["--pre-push"])
        # No git repo at cwd -> RuntimeError -> 2
        self.assertEqual(rc, 2)

    def test_every_ref_line_is_scanned_not_just_the_first(self):
        """A push of N refs must scan every local sha — a violation on the
        SECOND line is exactly what a first-line-only parse silently ships."""
        seen = []

        def fake_scan(root, old, new, remote, remote_url=None):
            seen.append(new)
            return [], []

        body = ("refs/heads/a %s refs/heads/a %s\n"
                "refs/heads/b %s refs/heads/b %s\n"
                % ("a" * 40, _ZERO, "b" * 40, _ZERO))
        with mock.patch("sys.stdin", io.StringIO(body)):
            with mock.patch.object(hostpath_guard, "scan_push", fake_scan):
                with contextlib.redirect_stderr(io.StringIO()):
                    rc = hostpath_guard.main(["--pre-push"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen, ["a" * 40, "b" * 40])

    def test_same_sha_on_two_refs_scans_once(self):
        seen = []

        def fake_scan(root, old, new, remote, remote_url=None):
            seen.append(new)
            return [], []

        body = ("refs/heads/a %s refs/heads/a %s\n"
                "refs/tags/v1 %s refs/tags/v1 %s\n"
                % ("a" * 40, _ZERO, "a" * 40, _ZERO))
        with mock.patch("sys.stdin", io.StringIO(body)):
            with mock.patch.object(hostpath_guard, "scan_push", fake_scan):
                with contextlib.redirect_stderr(io.StringIO()):
                    rc = hostpath_guard.main(["--pre-push"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen, ["a" * 40])

    def test_deletion_only_push_allows_without_scanning(self):
        """Deleting a remote ref pushes NO content: local sha is all zeros,
        there is no tree to scan, and the push must not be refused for it."""
        err = io.StringIO()

        def fail_scan(root, old, new, remote):  # pragma: no cover
            raise AssertionError("deletion ref must not be scanned")

        with mock.patch("sys.stdin", io.StringIO(
                "(delete) %s refs/heads/gone %s\n" % (_ZERO, "c" * 40))):
            with mock.patch.object(hostpath_guard, "scan_push", fail_scan):
                with contextlib.redirect_stderr(err):
                    rc = hostpath_guard.main(["--pre-push"])
        self.assertEqual(rc, 0)
        self.assertIn("deletion", err.getvalue())

    def test_deletion_mixed_with_real_ref_scans_only_the_real_one(self):
        seen = []

        def fake_scan(root, old, new, remote, remote_url=None):
            seen.append(new)
            return [], []

        body = ("(delete) %s refs/heads/gone %s\n"
                "refs/heads/main %s refs/heads/main %s\n"
                % (_ZERO, "c" * 40, "d" * 40, _ZERO))
        with mock.patch("sys.stdin", io.StringIO(body)):
            with mock.patch.object(hostpath_guard, "scan_push", fake_scan):
                with contextlib.redirect_stderr(io.StringIO()):
                    rc = hostpath_guard.main(["--pre-push"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen, ["d" * 40])


if __name__ == "__main__":
    unittest.main()


class PushIdentityFromGitArgvTest(unittest.TestCase):
    """v2 — the scanner scans the push git DESCRIBED, not the one it guessed.

    Git hands pre-push `<remote> <url>` as $1 $2. The v1 wrapper dropped
    both, so every push was visibility-checked against `origin`. Measured
    2026-08-06: a push to a second remote printed "remote 'origin' is
    private — host-path scan skipped"; both remotes were private that day,
    and against a public second remote the guard would have skipped the one
    push it exists to refuse.
    """

    _REF = ("refs/heads/main " + "a" * 40 + " refs/heads/main " + "b" * 40
            + "\n")

    def _run(self, argv, env=None):
        err = io.StringIO()
        patches = [mock.patch("sys.stdin", io.StringIO(self._REF))]
        if env is not None:
            patches.append(mock.patch.dict(os.environ, env, clear=False))
        with contextlib.ExitStack() as st:
            for p in patches:
                st.enter_context(p)
            st.enter_context(contextlib.redirect_stderr(err))
            rc = hostpath_guard.main(argv)
        return rc, err.getvalue()

    def test_argv_url_is_the_identity_and_name_resolution_never_runs(self):
        """When git supplies the URL, resolving by name is not merely
        redundant — it is the BUG (answering about a different remote). The
        resolver is patched to explode so a regression cannot pass by
        resolving to a conveniently-right answer."""
        with mock.patch.object(hostpath_guard, "_remote_url",
                               side_effect=AssertionError(
                                   "name resolution must not run when git "
                                   "supplied the URL")):
            with mock.patch.object(hostpath_guard, "_is_public",
                                   return_value=False) as vis:
                rc, err = self._run(
                    ["--pre-push", "aspublic",
                     "git@github.com:example/staging.git"])
        self.assertEqual(rc, 0)
        vis.assert_called_once_with("git@github.com:example/staging.git")
        # The note names the remote GIT named, not origin.
        self.assertIn("remote 'aspublic' is private", err)
        self.assertNotIn("'origin'", err)

    def test_argv_outranks_the_env_override(self):
        """The env var exists for invocations with no argv to trust. It must
        not outrank what git measured about THIS push."""
        with mock.patch.object(hostpath_guard, "_remote_url",
                               side_effect=AssertionError("no resolution")):
            with mock.patch.object(hostpath_guard, "_is_public",
                                   return_value=False):
                rc, err = self._run(
                    ["--pre-push", "aspublic", "git@github.com:e/s.git"],
                    env={"HELM_HOSTPATH_REMOTE": "somewhere-else"})
        self.assertEqual(rc, 0)
        self.assertIn("'aspublic'", err)
        self.assertNotIn("somewhere-else", err)

    def test_bare_pre_push_keeps_the_legacy_origin_fallback(self):
        """A v1 wrapper or a hand-run supplies no argv; the origin default
        (and the env override) must keep working — with name resolution."""
        with mock.patch.object(hostpath_guard, "_remote_url",
                               return_value="git@github.com:e/priv.git") as res:
            with mock.patch.object(hostpath_guard, "_is_public",
                                   return_value=False):
                rc, err = self._run(["--pre-push"])
        self.assertEqual(rc, 0)
        res.assert_called_once()
        self.assertEqual(res.call_args[0][1], "origin")
        self.assertIn("'origin' is private", err)

    def test_a_public_argv_url_is_scanned_not_skipped(self):
        """MUST-HIT for the whole class: the same push that skips on a
        private URL SCANS on a public one — proving the visibility answer
        drives the scan rather than decorating it. The tree walk is stubbed
        empty so no fixture repo is needed; the note says scanned-0-files."""
        with mock.patch.object(hostpath_guard, "_is_public",
                               return_value=True):
            with mock.patch.object(hostpath_guard, "_new_tree_blobs",
                                   return_value=[]):
                rc, err = self._run(
                    ["--pre-push", "aspublic", "https://github.com/e/s"])
        self.assertEqual(rc, 0)
        self.assertIn("no files in outgoing push to scan", err)

    def test_four_args_is_still_usage(self):
        rc, err = self._run(["--pre-push", "a", "b", "c"])
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)

    def test_the_installed_wrapper_forwards_gits_arguments(self):
        """The template is the artifact that RUNS. Without `"$@"` on the
        scanner exec line every scanner-side fix above is dead code — v1's
        exact failure. Pinned on the template string itself."""
        from helm.work import _guard
        exec_line = [ln for ln in _guard.HOSTPATH_PUSH_HOOK.splitlines()
                     if ln.startswith("exec python3")]
        self.assertEqual(len(exec_line), 1)
        self.assertIn('--pre-push "$@"', exec_line[0])
