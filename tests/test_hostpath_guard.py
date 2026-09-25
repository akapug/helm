#!/usr/bin/env python3
"""The pre-push host-path guard — refuse public pushes carrying host paths.

NO REAL HOST PATHS IN FIXTURES — every test path uses /home/test-user/,
/Users/dev/, or similar generic names. The test data lives in these fixtures
deliberately; the guard's regex IS expected to match them (they ARE host-path
shapes, by design).
"""
import ast
import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-hp-", var="HELM_HOME")

from helm import hostpath_guard, wiring, work  # noqa: E402
from helm.work import _guard  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_PAT = hostpath_guard._HOSTPATH_RE
_ZERO = "0000000000000000000000000000000000000000"
# `_visibility` answers for a mocked door: (state, why), why only on UNKNOWN.
_PRIVATE = (hostpath_guard.PRIVATE, None)
_PUBLIC = (hostpath_guard.PUBLIC, None)


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
        skip = mock.patch.dict(os.environ, {"HELM_HOSTPATH_SKIP": "0"})
        skip.start()
        self.addCleanup(skip.stop)
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
    """The visibility answer is PRIVATE, PUBLIC or UNKNOWN with its cause.
    Only PRIVATE skips; the caller scans UNKNOWN as if public (fail safe)."""

    def setUp(self):
        # The retry pause is observed, never slept.
        self.pause = mock.patch.object(hostpath_guard, "time").start()
        self.addCleanup(mock.patch.stopall)

    def test_a_remote_gh_cannot_be_asked_about_is_unknown_without_asking(self):  # noqa: VACUOUS_ASSERTION — the absent pause is read beside five equalities to non-empty UNKNOWN answers, and test_gh_unavailable_is_unknown_after_one_retry is the pause's positive control
        """No URL, a URL not on GitHub, or no owner/repo: asking again cannot
        change the answer, so gh never runs and nothing is retried."""
        cases = {None: "no remote URL", "": "no remote URL",
                 "git@unknown-host:owner/repo": "not a GitHub URL",
                 "git@gitlab.com:owner/repo.git": "not a GitHub URL",
                 "https://github.com/owner": "no owner/repo in the GitHub URL"}
        with mock.patch.object(hostpath_guard.subprocess, "run",
                               side_effect=AssertionError("gh ran")):
            for url, why in cases.items():
                with self.subTest(url=url):
                    self.assertEqual(hostpath_guard._visibility(url),
                                     (hostpath_guard.UNKNOWN, why))
        self.pause.sleep.assert_not_called()

    def test_github_private_is_private(self):
        with mock.patch.object(hostpath_guard.subprocess, "run") as mr:
            mr.return_value.returncode = 0
            mr.return_value.stdout = json.dumps({"isPrivate": True})
            self.assertEqual(hostpath_guard._visibility(
                "git@github.com:owner/private-repo.git"), _PRIVATE)
        self.assertEqual(mr.call_args[0][0], ("gh", "repo", "view",
                                              "owner/private-repo", "--json",
                                              "isPrivate"))

    def test_github_public_is_public(self):  # noqa: VACUOUS_ASSERTION — the absent pause is read beside an equality to the PUBLIC answer and a call count of 1; test_gh_unavailable_is_unknown_after_one_retry is the pause's positive control
        with mock.patch.object(hostpath_guard.subprocess, "run") as mr:
            mr.return_value.returncode = 0
            mr.return_value.stdout = json.dumps({"isPrivate": False})
            self.assertEqual(hostpath_guard._visibility(
                "https://github.com/owner/public-repo"), _PUBLIC)
        self.assertEqual(mr.call_count, 1)
        self.pause.sleep.assert_not_called()

    def test_gh_unavailable_is_unknown_after_one_retry(self):
        with mock.patch.object(hostpath_guard.subprocess, "run") as mr:
            mr.side_effect = OSError("gh not found")
            self.assertEqual(hostpath_guard._visibility(
                "git@github.com:owner/repo.git"),
                (hostpath_guard.UNKNOWN, "cannot run gh (OSError), twice"))
        self.assertEqual(mr.call_count, 2)
        self.pause.sleep.assert_called_once_with(
            hostpath_guard._GH_RETRY_PAUSE)

    def test_an_answer_that_is_not_a_boolean_is_unknown_never_private(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a literal tuple of four answers, each an equality to a non-empty UNKNOWN answer
        """The old read took a missing isPrivate, and a string "false", as
        PRIVATE and skipped the scan on an answer gh never gave."""
        for out in ("{}", '{"isPrivate": "false"}', '{"isPrivate": null}',
                    "[true]"):
            with self.subTest(out=out), \
                    mock.patch.object(hostpath_guard.subprocess, "run") as mr:
                mr.return_value.returncode = 0
                mr.return_value.stdout = out
                self.assertEqual(
                    hostpath_guard._visibility("git@github.com:o/r.git"),
                    (hostpath_guard.UNKNOWN,
                     "gh repo view gave no isPrivate true/false, twice"))


# A stand-in `gh`: each call appends its argv to <plan>.calls and plays the
# next step of the JSON plan (the last step repeats). A step may sleep, print
# to stdout or stderr, and exit with an rc.
_GH_STUB = r"""
import json, os, sys, time
plan = os.environ["HP_GH_PLAN"]
with open(plan) as f:
    steps = json.load(f)
with open(plan + ".calls", "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
with open(plan + ".calls") as f:
    n = len(f.readlines())
step = steps[min(n, len(steps)) - 1]
time.sleep(step.get("sleep", 0))
sys.stdout.write(step.get("stdout", ""))
sys.stderr.write(step.get("stderr", ""))
sys.exit(step.get("rc", 0))
"""


class StubbedGhRefusalLineTest(unittest.TestCase):
    """task/3073, MEASURED: a push of main to a PRIVATE repository was
    refused with "must not be pushed to a PUBLIC remote", and `gh repo view`
    read isPrivate:true 409 ms later. The guard could not say which gh
    failure it had turned into PUBLIC.

    Every arm runs the real pre-push entry against a stub `gh` that is the
    only program on PATH, so the real gh is never reached. The push carries
    one host path (the scan's git reads are stubbed; the pattern match is
    real), so each arm ends on the line that decides it: the private skip, or
    the refusal's last line, which names the visibility answer."""

    URL = "git@github.com:example/leaky.git"
    ARGV = ["repo", "view", "example/leaky", "--json", "isPrivate"]
    SKIP = "[helm hostpath] remote 'origin' is private — host-path scan skipped"
    HEAD = "[helm hostpath] REFUSED: 1 host-path match in outgoing push"
    LAST = ("[helm hostpath] these match a host filesystem-path pattern and "
            "must not be pushed to %s")
    PUBLIC_LINE = LAST % "a PUBLIC remote"
    UNKNOWN_LINE = LAST % "'origin': visibility UNKNOWN (%s), treated as public"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-hp-gh-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bindir = os.path.join(self.tmp, "bin")
        os.mkdir(self.bindir)
        self.plan = os.path.join(self.tmp, "plan.json")

    def install_gh(self):
        gh = os.path.join(self.bindir, "gh")
        with open(gh, "w") as f:
            f.write("#!%s\n%s" % (sys.executable, _GH_STUB))
        os.chmod(gh, 0o755)

    def push(self, *steps, timeout=None):
        """-> (rc, stderr lines, gh argvs, the pause mock)."""
        with open(self.plan, "w") as f:
            json.dump(list(steps), f)
        blob = ("b" * 40, "leak.py", 64)
        err = io.StringIO()
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.dict(os.environ, {
                "PATH": self.bindir, "HP_GH_PLAN": self.plan}))
            st.enter_context(mock.patch(
                "sys.stdin", io.StringIO("refs/heads/main %s refs/heads/main "
                                         "%s\n" % ("a" * 40, _ZERO))))
            st.enter_context(mock.patch.object(
                hostpath_guard, "_remote_config",
                return_value=[("remote.origin.url", self.URL)]))
            st.enter_context(mock.patch.object(
                hostpath_guard, "_advertised_base", return_value=([], None)))
            st.enter_context(mock.patch.object(
                hostpath_guard, "_outgoing_blobs",
                return_value=([blob], None)))
            st.enter_context(mock.patch.object(
                hostpath_guard, "_read_blobs", return_value=iter(
                    [(blob[0], b'H = "/home/test-user/secret"\n')])))
            pause = st.enter_context(mock.patch.object(hostpath_guard,
                                                       "time"))
            if timeout is not None:
                st.enter_context(mock.patch.object(
                    hostpath_guard, "_GH_TIMEOUT", timeout))
            st.enter_context(contextlib.redirect_stderr(err))
            rc = hostpath_guard.main(["--pre-push", "origin", self.URL])
        calls = self.plan + ".calls"
        argvs = []
        if os.path.exists(calls):
            with open(calls) as f:
                argvs = [json.loads(line) for line in f]
        return rc, err.getvalue().splitlines(), argvs, pause.sleep

    def assert_refused_unknown(self, rc, lines, why):
        self.assertEqual(rc, 1, lines)
        self.assertIn(self.HEAD, lines)
        self.assertEqual(lines[-2], self.UNKNOWN_LINE % why)
        self.assertNotIn(self.PUBLIC_LINE, lines)

    def test_private_skips_the_scan(self):
        self.install_gh()
        rc, lines, argvs, pause = self.push({"stdout": '{"isPrivate":true}'})
        self.assertEqual((rc, lines), (0, [self.SKIP]))
        self.assertEqual(argvs, [self.ARGV])
        pause.assert_not_called()

    def test_public_refuses_and_says_public(self):
        self.install_gh()
        rc, lines, argvs, pause = self.push({"stdout": '{"isPrivate":false}'})
        self.assertEqual(rc, 1, lines)
        self.assertIn(self.HEAD, lines)
        self.assertEqual(lines[-2], self.PUBLIC_LINE)
        self.assertEqual(argvs, [self.ARGV])
        pause.assert_not_called()

    def test_a_nonzero_gh_exit_refuses_as_unknown_and_never_prints_gh(self):
        self.install_gh()
        rc, lines, argvs, pause = self.push(
            {"rc": 1, "stderr": "HTTP 502: Bad Gateway (example/leaky)\n"})
        self.assert_refused_unknown(rc, lines, "gh repo view exited 1, twice")
        self.assertEqual(argvs, [self.ARGV, self.ARGV])
        pause.assert_called_once_with(hostpath_guard._GH_RETRY_PAUSE)
        self.assertFalse([ln for ln in lines if "502" in ln or
                          "example/leaky" in ln], lines)

    def test_a_gh_timeout_refuses_as_unknown(self):
        self.install_gh()
        rc, lines, argvs, pause = self.push({"sleep": 30}, timeout=0.5)
        self.assert_refused_unknown(
            rc, lines, "gh repo view timed out after 0.5s, twice")
        self.assertEqual(argvs, [self.ARGV, self.ARGV])
        pause.assert_called_once_with(hostpath_guard._GH_RETRY_PAUSE)

    def test_no_gh_on_path_refuses_as_unknown(self):
        """No stub installed: PATH holds nothing, so running gh is an
        OSError."""
        rc, lines, argvs, pause = self.push({"rc": 0})
        self.assert_refused_unknown(
            rc, lines, "cannot run gh (FileNotFoundError), twice")
        self.assertEqual(argvs, [])
        pause.assert_called_once_with(hostpath_guard._GH_RETRY_PAUSE)

    def test_output_that_is_not_json_refuses_as_unknown(self):
        self.install_gh()
        rc, lines, argvs, pause = self.push({"stdout": "isPrivate: true\n"})
        self.assert_refused_unknown(rc, lines, "gh repo view printed no JSON, "
                                               "twice")
        self.assertEqual(argvs, [self.ARGV, self.ARGV])

    def test_two_different_failures_are_both_named(self):
        self.install_gh()
        rc, lines, argvs, _pause = self.push({"rc": 4}, {"sleep": 30},
                                             timeout=0.5)
        self.assert_refused_unknown(
            rc, lines, "gh repo view exited 4, then gh repo view timed out "
                       "after 0.5s")
        self.assertEqual(len(argvs), 2)

    def test_the_measured_push_a_failed_first_probe_whose_retry_answers(self):
        """The measured push replayed: the first probe fails, the retry reads
        isPrivate:true, and the push goes through as PRIVATE."""
        self.install_gh()
        rc, lines, argvs, pause = self.push(
            {"rc": 1}, {"stdout": '{"isPrivate":true}'})
        self.assertEqual((rc, lines), (0, [self.SKIP]))
        self.assertEqual(argvs, [self.ARGV, self.ARGV])
        pause.assert_called_once_with(hostpath_guard._GH_RETRY_PAUSE)

    def test_a_failed_first_probe_whose_retry_says_public_refuses_as_public(
            self):
        self.install_gh()
        rc, lines, argvs, _pause = self.push(
            {"rc": 1}, {"stdout": '{"isPrivate":false}'})
        self.assertEqual(rc, 1, lines)
        self.assertEqual(lines[-2], self.PUBLIC_LINE)
        self.assertEqual(len(argvs), 2)


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
        """Both sha fields are validated. The base comes from the
        destination's advertisement, not from this field, but malformed
        protocol input means a broken feeder and nothing past it is read."""
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

    def test_unreadable_stdin_refuses_WITH_a_diagnostic(self):  # noqa: VACUOUS_ASSERTION — the absent error text is read against the fixed reason pinned in the same stderr
        """A read we could not perform is not an up-to-date push, and the
        operator is told so in fixed words. The error's own text is not
        printed: it can carry whatever the reader held."""
        err = io.StringIO()
        broken = mock.Mock()
        broken.read.side_effect = OSError("Bad file descriptor")
        with mock.patch("sys.stdin", broken):
            with contextlib.redirect_stderr(err):
                rc = hostpath_guard.main(["--pre-push"])
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED: unreadable pre-push input", err.getvalue())
        self.assertNotIn("Bad file descriptor", err.getvalue())

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

    def test_a_refusal_counts_in_whole_words(self):
        """`"es"[:n != 1]` slices ONE character, so 153 hits printed as
        "153 host-path matche" (measured on a clone of the dregg fork)."""
        five = [("f%d.py" % i, "/home/test-user/") for i in range(5)]
        err = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(
                "refs/heads/main %s refs/heads/main %s\n" % ("a" * 40, _ZERO))):
            with mock.patch.object(hostpath_guard, "scan_push",
                                   return_value=(five, [])):
                with contextlib.redirect_stderr(err):
                    rc = hostpath_guard.main(["--pre-push"])
        self.assertEqual(rc, 1)
        self.assertIn("REFUSED: 5 host-path matches in outgoing push — +2 "
                      "more matches\n", err.getvalue())

    def test_stdin_with_shas_and_fake_remote(self):
        """A pushed sha no repository has: rev-list fails -> rc=2."""
        with mock.patch("sys.stdin", io.StringIO(
                "refs/heads/main %s refs/heads/main %s\n"
                % ("a" * 40, _ZERO))):
            with mock.patch.object(hostpath_guard, "_remote_url",
                                   return_value="git@unknown:repo"):
                rc = hostpath_guard.main(["--pre-push"])
        # rev-list cannot walk the sha -> RuntimeError -> 2
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
        # The label names a remote only when its url is configured; these
        # arms are about which name git passed, not about this checkout.
        config = [("remote.aspublic.url", "git@github.com:example/staging.git"),
                  ("remote.origin.url", "git@github.com:e/priv.git")]
        patches = [mock.patch("sys.stdin", io.StringIO(self._REF)),
                   mock.patch.object(hostpath_guard, "_remote_config",
                                     return_value=config)]
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
            with mock.patch.object(hostpath_guard, "_visibility",
                                   return_value=_PRIVATE) as vis:
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
            with mock.patch.object(hostpath_guard, "_visibility",
                                   return_value=_PRIVATE):
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
            with mock.patch.object(hostpath_guard, "_visibility",
                                   return_value=_PRIVATE):
                rc, err = self._run(["--pre-push"])
        self.assertEqual(rc, 0)
        res.assert_called_once()
        self.assertEqual(res.call_args[0][1], "origin")
        self.assertIn("'origin' is private", err)

    def test_a_public_argv_url_is_scanned_not_skipped(self):
        """MUST-HIT for the whole class: the same push that skips on a
        private URL SCANS on a public one — proving the visibility answer
        drives the scan rather than decorating it. The object walk is stubbed
        empty so no fixture repo is needed; the note says scanned-0-files."""
        with mock.patch.object(hostpath_guard, "_visibility",
                               return_value=_PUBLIC):
            with mock.patch.object(hostpath_guard, "_outgoing_blobs",
                                   return_value=([], None)), \
                    mock.patch.object(hostpath_guard, "_advertised_base",
                                      return_value=([], None)):
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
        scanner line every scanner-side fix above is dead code — v1's exact
        failure. Pinned on the template string itself."""
        scanner_line = [ln for ln in _guard.HOSTPATH_PUSH_HOOK.splitlines()
                        if 'python3 "$scanner"' in ln]
        self.assertEqual(len(scanner_line), 1)
        self.assertIn('--pre-push "$@"', scanner_line[0])


HOST_PATH_BLOB = 'HOME = "/home/test-user/.config/app"\n'


def proven_push():
    """The argv of a plain `git push origin main`. A direct call has no
    `git push` above it, so without this its destination is never proven."""
    return mock.patch.object(hostpath_guard, "_push_argv",
                             return_value=["git", "push", "origin", "main"])

# THE PRE-PUSH INSTALLED ON LIVE REPOS BEFORE v4, trimmed to its executable
# body: the user hook runs on the hook's own stdin, then the scanner is
# exec'd on whatever stdin is left.
V2_PRE_PUSH = """#!/bin/sh
# helm work managed hook: pre-push v2
# Existing executable hook, when present, runs first with the original args.
scanner=%(scanner)s
user_hook=%(user_hook)s

if [ -x "$user_hook" ]; then
  "$user_hook" "$@" || exit $?
fi
[ "$HELM_HOSTPATH_SKIP" = "1" ] && exit 0
if [ ! -f "$scanner" ]; then
  exit 0
fi
exec python3 "$scanner" --pre-push "$@"
"""


class _PushSandbox(unittest.TestCase):
    """A real `git push` through the pre-push hook the real installer writes.

    Every arm runs in a sandbox: a temp repo, a bare remote on a local path
    (a non-GitHub URL reads as public, so the scan runs), HOME, HELM_HOME and
    git config all temp, and a `helm` on PATH that does nothing, so the
    refusal counter the hook carries reaches no estate. The remote holds a
    clean seed before the hook is installed.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-hp-push-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bin_dir = os.path.join(self.tmp, "bin")
        home = os.path.join(self.tmp, "home")
        os.makedirs(bin_dir)
        os.makedirs(home)
        with open(os.path.join(bin_dir, "helm"), "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(bin_dir, "helm"), 0o755)
        needles = os.path.join(self.tmp, "needles.txt")
        with open(needles, "w") as f:
            f.write("zz-synthetic-hostpath-needle\n")
        env = mock.patch.dict(os.environ, {
            "HOME": home,
            "HELM_HOME": os.path.join(self.tmp, "helm"),
            "HELM_CHAT_DIR": os.path.join(self.tmp, "chat"),
            "HELM_CHAT_NODE_URL": "", "HELM_CHAT_LOG": "0",
            "HELM_LANDLOCK": "0", "HELM_PRIVATE_NEEDLES": needles,
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "PATH": bin_dir + os.pathsep + os.environ.get("PATH", ""),
            "PYTHONPATH": REPO})
        env.start()
        self.addCleanup(env.stop)
        # popped INSIDE the patch, so stopping it restores whatever was set
        os.environ.pop("HELM_HOSTPATH_SKIP", None)
        self.remote = os.path.join(self.tmp, "remote.git")
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        r = self.git(self.tmp, "init", "-q", "--bare", self.remote)
        self.assertEqual(r.returncode, 0, r.stderr)
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.email", "t@example.com"),
                     ("config", "user.name", "t"),
                     ("remote", "add", "origin", self.remote)):
            r = self.git(self.root, *args)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.write("README", "seed\n")
        self.seed = self.commit("seed")
        # THE REMOTE HOLDS THE SEED BEFORE ANY HOOK EXISTS, so every ref line
        # an arm expects carries a real remote sha, not the zero sha.
        r = self.git(self.root, "push", "-q", "origin", "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        rc, _out, err = self.cli("install-guard", "--apply", "--profile", "leak")
        self.assertEqual(rc, 0, err)
        self.hook = _guard.hook_path(self.root, "pre-push")
        self.user = self.hook + ".helm-user"
        self.marker = os.path.join(self.tmp, "user-hook-stdin")

    def git(self, cwd, *args):
        return subprocess.run(("git",) + args, cwd=cwd, capture_output=True,
                              text=True, timeout=120)

    def cli(self, *args):
        """The shipped `helm work` dispatcher, never install_guard directly."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = work.cmd_work(list(args) + ["--repo", self.root])
        return rc, out.getvalue(), err.getvalue()

    def write(self, rel, content):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def commit(self, msg):
        """--no-verify: the leak profile's pre-commit is not the subject, and
        a refusal there would fail these arms for a reason they are not about."""
        self.assertEqual(self.git(self.root, "add", "-A").returncode, 0)
        r = self.git(self.root, "commit", "-q", "--no-verify", "-m", msg)
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.git(self.root, "rev-parse", "HEAD").stdout.strip()

    def push(self, refspec="main"):
        return self.git(self.root, "push", "origin", refspec)

    def remote_sha(self, ref="refs/heads/main"):
        return self.git(self.remote, "rev-parse", "--verify", "-q",
                        ref).stdout.strip()

    def ref_line(self, local_sha):
        return "refs/heads/main %s refs/heads/main %s\n" % (local_sha, self.seed)


class ManagedPrePushReadsStdinOnceTest(_PushSandbox):
    """END TO END: the managed pre-push with a user hook composed in that
    READS stdin.

    Git writes the outgoing ref lines to pre-push ONCE. A user hook that
    consumes them (`git lfs pre-push` reads to the end) left the scanner an
    empty stdin, which is the protocol for nothing-to-push, so a host path
    went out unscanned.
    """

    def install_stdin_eating_user_hook(self):
        """The git-lfs shape: read stdin to the end, keep what it read."""
        with open(self.user, "w") as f:
            f.write("#!/bin/sh\ncat > %s\n" % shlex.quote(self.marker))
        os.chmod(self.user, 0o755)

    def marker_text(self):
        with open(self.marker, encoding="utf-8") as f:
            return f.read()

    def test_a_user_hook_that_reads_stdin_no_longer_blinds_the_scan(self):  # noqa: VACUOUS_ASSERTION — the equalities pin non-empty values, the seed sha and git's ref line
        self.install_stdin_eating_user_hook()
        self.write("tests/config.py", HOST_PATH_BLOB)
        head = self.commit("host path")
        r = self.push()
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("[helm hostpath] REFUSED: 1 host-path match", r.stderr)
        self.assertNotIn("nothing to push", r.stderr)
        self.assertEqual(self.remote_sha(), self.seed,
                         "a refused push must leave the remote where it was")
        self.assertEqual(self.marker_text(), self.ref_line(head),
                         "the user hook must still get git's exact ref lines")

    def test_a_clean_push_passes_and_the_user_hook_gets_its_ref_line(self):  # noqa: VACUOUS_ASSERTION — the equalities pin non-empty values, the pushed sha and git's ref line
        """The control for the arm above: the refusal is about the content,
        and on the allow path the user hook still reads the refs it uploads."""
        self.install_stdin_eating_user_hook()
        self.write("docs/clean.md", "nothing host-shaped here\n")
        head = self.commit("clean")
        r = self.push()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.remote_sha(), head)
        self.assertEqual(self.marker_text(), self.ref_line(head))

    def test_an_up_to_date_push_stays_empty_for_both_readers(self):  # noqa: VACUOUS_ASSERTION — the empty marker IS the subject; the file existing proves the user hook ran
        self.install_stdin_eating_user_hook()
        r = self.push()
        self.assertEqual(r.returncode, 0, r.stderr)
        # the scanner ran and read zero bytes
        self.assertIn("nothing to push", r.stderr)
        # the user hook ran (its file exists) and read zero bytes
        self.assertEqual(self.marker_text(), "")

    def test_with_no_user_hook_the_scan_still_reads_the_push(self):  # noqa: VACUOUS_ASSERTION — the remote is pinned to the non-empty seed sha beside the refusal text
        self.assertFalse(os.path.lexists(self.user))
        self.write("tests/config.py", HOST_PATH_BLOB)
        self.commit("host path")
        r = self.push()
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("[helm hostpath] REFUSED: 1 host-path match", r.stderr)
        self.assertEqual(self.remote_sha(), self.seed)

    def test_a_stdin_the_hook_cannot_read_refuses_before_any_reader(self):  # noqa: VACUOUS_ASSERTION — the absent marker is read against the marker the readable control wrote
        """Git never hands pre-push an unreadable stdin, so the installed
        hook is run directly: a directory as stdin makes the read fail."""
        self.install_stdin_eating_user_hook()
        argv = [self.hook, "origin", self.remote]
        r = subprocess.run(argv, cwd=self.root, input="", capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.marker_text(), "",
                         "control: a readable empty stdin reaches the user hook")
        os.unlink(self.marker)
        fd = os.open(self.tmp, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        r = subprocess.run(argv, cwd=self.root, stdin=fd, capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("[helm hostpath] REFUSED: cannot read git's pre-push "
                      "ref lines", r.stderr)
        self.assertFalse(os.path.exists(self.marker),
                         "no reader may run on a stdin the hook could not read")

    def test_install_guard_upgrades_a_v2_pre_push_and_keeps_the_user_hook(self):  # noqa: VACUOUS_ASSERTION — each equality pins a non-empty sha, ref line or byte string
        self.install_stdin_eating_user_hook()
        with open(self.user, "rb") as f:
            user_bytes = f.read()
        scanner = next(p for p in _guard._scanner_assets(self.root, "leak")
                       if p.endswith("/hostpath_guard.py"))
        v2 = V2_PRE_PUSH % {"scanner": shlex.quote(scanner),
                            "user_hook": shlex.quote(self.user)}
        with open(self.hook, "w") as f:
            f.write(v2)
        os.chmod(self.hook, 0o755)
        self.write("tests/config.py", HOST_PATH_BLOB)
        head = self.commit("host path")
        # CONTROL: the v2 hook reproduces the fail-open v4 closes. The user
        # hook eats stdin and the host path goes out unscanned.
        r = self.push("HEAD:refs/heads/under-v2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nothing to push", r.stderr)
        self.assertEqual(self.remote_sha("refs/heads/under-v2"), head)
        stale = _guard.stale_guard_hooks(self.root)
        self.assertEqual([(s, n) for s, n, _w in stale], [("STALE", "pre-push")])
        # THE DRY RUN SHOWS THE UPGRADE AND WRITES NOTHING
        rc, out, err = self.cli("install-guard", "--profile", "leak")
        self.assertEqual(rc, 0, err)
        self.assertIn("# DRIFT %s — installed CONTENT differs" % self.hook, out)
        self.assertIn("# helm work managed hook: pre-push v4", out)
        self.assertNotIn("preserves existing hook as %s" % self.user, out)
        with open(self.hook) as f:
            self.assertEqual(f.read(), v2)
        rc, _out, err = self.cli("install-guard", "--apply", "--profile", "leak")
        self.assertEqual(rc, 0, err)
        with open(self.hook) as f:
            self.assertEqual(f.read().splitlines()[1],
                             "# helm work managed hook: pre-push v4")
        with open(self.user, "rb") as f:
            self.assertEqual(f.read(), user_bytes,
                             "the user's own hook is kept byte for byte")
        self.assertTrue(os.access(self.user, os.X_OK))
        self.assertFalse(os.path.lexists(self.hook + _guard.RETIRED_HOOK_SUFFIX),
                         "a managed hook is rewritten, never demoted")
        self.assertEqual(_guard.stale_guard_hooks(self.root), [])
        os.unlink(self.marker)
        # The v2 push PUBLISHED the first host path: the remote holds it on
        # under-v2, and the scan reads only what a push adds. So the push
        # that proves the v4 hook reads the refs carries a NEW host path.
        self.write("tests/second.py", 'P = "/Users/dev/Library/app"\n')
        head = self.commit("a second host path")
        r = self.push()
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("[helm hostpath] REFUSED: 1 host-path match", r.stderr)
        self.assertIn("tests/second.py", r.stderr)
        self.assertEqual(self.remote_sha(), self.seed)
        self.assertEqual(self.marker_text(), self.ref_line(head))

    def test_the_wiring_census_credits_the_installed_hook_and_no_other_feeder(self):  # noqa: VACUOUS_ASSERTION — the MISSING row is read against the WIRED row the installed hook earns
        hooks = os.path.dirname(self.hook)
        units = os.path.join(self.tmp, "no-units")
        os.makedirs(units)
        got = wiring.actuator_census(hook_dir=hooks, unit_dir=units,
                                     crontab="", settings_paths=[])
        self.assertIn("hostpath-pre-push", got["wired"])
        self.assertNotIn("hostpath-pre-push", got["missing"])
        with open(self.hook) as f:
            text = f.read()
        fed = 'helm_push_refs | python3 "$scanner"'
        self.assertEqual(text.count(fed), 1,
                         "the installed scanner line is fed by the hook's feeder")
        blinded = text.replace(fed, 'true | python3 "$scanner"')
        bare = os.path.join(self.tmp, "blinded-hooks")
        os.makedirs(bare)
        with open(os.path.join(bare, "pre-push"), "w") as f:
            f.write(blinded)
        os.chmod(os.path.join(bare, "pre-push"), 0o755)
        got = wiring.actuator_census(hook_dir=bare, unit_dir=units,
                                     crontab="", settings_paths=[])
        self.assertIn("hostpath-pre-push", got["missing"])


class PushReadsOnlyWhatTheRemoteLacksTest(_PushSandbox):
    """The scan reads the blobs a push ADDS to the remote, not the tree.

    MEASURED on the dregg fork: a whole-tree scan REFUSED a push of two
    commits that added no host path, on 322 host paths in upstream files the
    remote already held, after about 17 minutes. Every arm pushes through the
    installed hook to a remote that already holds a host path, published
    here with --no-verify as the fixture, the way the upstream history
    reached the fork. The base is what the destination advertises;
    DestinationAdvertisementTest holds the arms where that cannot be read.
    """

    def publish_host_path(self):
        """The remote already holds a host path. -> the published sha."""
        self.write("vendor/upstream.md", HOST_PATH_BLOB)
        head = self.commit("upstream history with a host path")
        r = self.git(self.root, "push", "-q", "--no-verify", "origin", "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.remote_sha(), head)
        return head

    def clean_commit(self, name="docs/clean.md"):
        self.write(name, "nothing host-shaped here\n")
        return self.commit("clean")

    def test_a_clean_commit_on_public_host_paths_is_allowed(self):  # noqa: VACUOUS_ASSERTION — the remote is pinned to the non-empty pushed sha
        self.publish_host_path()
        head = self.clean_commit()
        r = self.push()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.remote_sha(), head)
        self.assertIn("1 blob(s) scanned, the ones 'origin' does not "
                      "already have — no host-path matches", r.stderr)
        self.assertNotIn("full scan", r.stderr)

    def test_a_commit_adding_a_host_path_on_top_is_refused(self):  # noqa: VACUOUS_ASSERTION — the absent old path is read in the same stderr that names the new one
        published = self.publish_host_path()
        self.write("src/new.py", 'P = "/Users/dev/Library/app"\n')
        self.commit("a new host path")
        r = self.push()
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("[helm hostpath] REFUSED: 1 host-path match", r.stderr)
        self.assertIn("src/new.py: /Users/dev/", r.stderr)
        self.assertNotIn("vendor/upstream.md", r.stderr,
                         "the published host path is not this push's")
        self.assertEqual(self.remote_sha(), published)

    def test_a_new_branch_on_a_published_base_reads_only_its_objects(self):  # noqa: VACUOUS_ASSERTION — the new remote branch is pinned to the non-empty pushed sha
        self.publish_host_path()
        self.assertEqual(self.git(self.root, "checkout", "-q", "-b",
                                  "feature").returncode, 0)
        head = self.clean_commit()
        r = self.push("feature")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.remote_sha("refs/heads/feature"), head)
        self.assertNotIn("full scan", r.stderr)

    def test_no_tracking_ref_is_needed(self):  # noqa: VACUOUS_ASSERTION — the empty tracking-ref list is read beside the pinned non-empty pushed sha
        """Every tracking ref is deleted: the destination's advertisement
        alone says it holds the published commit."""
        self.publish_host_path()
        r = self.git(self.root, "update-ref", "-d", "refs/remotes/origin/main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.git(self.root, "for-each-ref",
                                  "refs/remotes/origin").stdout, "")
        head = self.clean_commit()
        r = self.push()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.remote_sha(), head)
        self.assertNotIn("full scan", r.stderr)

    def test_a_first_push_to_an_empty_remote_reads_the_history(self):  # noqa: VACUOUS_ASSERTION — the empty remote's ref list is read against the named hit and the pinned non-empty sha on the other remote
        """The host path is only in HISTORY: the tip deletes it. A first
        push uploads that history, so it is read. The same commits to the
        remote that already holds them go out, which is the control."""
        self.publish_host_path()
        os.unlink(os.path.join(self.root, "vendor", "upstream.md"))
        head = self.clean_commit()
        r = self.push()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.remote_sha(), head)
        empty = os.path.join(self.tmp, "empty.git")
        self.assertEqual(self.git(self.tmp, "init", "-q", "--bare",
                                  empty).returncode, 0)
        self.assertEqual(self.git(self.root, "remote", "add", "second",
                                  empty).returncode, 0)
        r = self.git(self.root, "push", "second", "main")
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("full scan: 'second': the destination advertises no "
                      "branch or tag (a first push)", r.stderr)
        self.assertIn("[helm hostpath] REFUSED: 1 host-path match", r.stderr)
        self.assertIn("vendor/upstream.md: /home/test-user/", r.stderr)
        self.assertEqual(self.git(empty, "for-each-ref").stdout, "")

    def test_a_push_by_url_is_narrowed_by_that_urls_advertisement(self):  # noqa: VACUOUS_ASSERTION — the remote is pinned to the non-empty pushed sha
        """`git push <url>` hands the hook the URL as the remote name. The
        advertisement comes from the URL, so no remote entry is needed."""
        self.publish_host_path()
        head = self.clean_commit()
        r = self.git(self.root, "push", self.remote, "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.remote_sha(), head)
        self.assertNotIn("full scan", r.stderr)

    def test_a_broken_tracking_ref_is_never_read(self):  # noqa: VACUOUS_ASSERTION — the remote is pinned to the non-empty pushed sha
        self.publish_host_path()
        with open(os.path.join(self.root, ".git", "refs", "remotes", "origin",
                               "broken"), "w") as f:
            f.write("not a sha\n")
        head = self.clean_commit()
        r = self.push()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.remote_sha(), head)
        self.assertNotIn("full scan", r.stderr)

    def test_a_hand_run_without_gits_url_reads_everything(self):
        """No URL from git means nothing to ask for an advertisement."""
        published = self.publish_host_path()
        head = self.clean_commit()
        hits, notes = hostpath_guard.scan_push(self.root, published, head,
                                               "origin")
        self.assertEqual(hits, [("vendor/upstream.md", "/home/test-user/")])
        self.assertIn("did not pass this push's URL", notes[0][1])
        with proven_push():
            hits, notes = hostpath_guard.scan_push(
                self.root, published, head, "origin", remote_url=self.remote)
        self.assertEqual(hits, [])
        self.assertEqual(notes, [("note", "1 blob(s) scanned, the ones "
                                  "'origin' does not already have — no "
                                  "host-path matches")])


class DestinationAdvertisementTest(_PushSandbox):
    """The base is what the push DESTINATION advertises, never a tracking
    ref: a tracking ref records what some fetch URL held, and a set-url, a
    pushurl or a pushInsteadOf sends the push elsewhere while it stays.

    MEASURED: a host-path blob published to origin's old URL, `git remote
    set-url origin` to a fresh empty repository, and a clean commit narrowed
    by refs/remotes/origin/main read 1 blob and sent the old blob to the
    empty repository unread. Every arm starts with origin holding a host
    path, published with --no-verify as the fixture.
    """

    def setUp(self):
        super().setUp()
        self.write("vendor/upstream.md", HOST_PATH_BLOB)
        self.published = self.commit("upstream history with a host path")
        r = self.git(self.root, "push", "-q", "--no-verify", "origin", "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.remote_sha(), self.published)

    def config(self, *args):
        r = self.git(self.root, "config", *args)
        self.assertEqual(r.returncode, 0, r.stderr)

    def fresh_remote(self):
        path = os.path.join(self.tmp, "fresh.git")
        r = self.git(self.tmp, "init", "-q", "--bare", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        return path

    def clean_commit(self):
        self.write("docs/clean.md", "nothing host-shaped here\n")
        return self.commit("clean")

    def assert_first_push_refused(self, r, fresh):
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("full scan: 'origin': the destination advertises no "
                      "branch or tag (a first push)", r.stderr)
        self.assertIn("[helm hostpath] REFUSED: 1 host-path match", r.stderr)
        self.assertIn("vendor/upstream.md: /home/test-user/", r.stderr)
        self.assertEqual(self.git(fresh, "for-each-ref").stdout, "",
                         "the fresh repository must receive nothing")

    def scan(self, head, url, old=_ZERO):
        with proven_push():
            return hostpath_guard.scan_push(self.root, old, head, "origin",
                                            remote_url=url)

    def test_set_url_to_a_fresh_repository_reads_everything(self):  # noqa: VACUOUS_ASSERTION — the fresh repository's empty ref list is read beside the kept non-empty tracking ref
        fresh = self.fresh_remote()
        r = self.git(self.root, "remote", "set-url", "origin", fresh)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.git(self.root, "rev-parse",
                                  "refs/remotes/origin/main").stdout.strip(),
                         self.published, "the stale tracking ref is kept")
        self.clean_commit()
        self.assert_first_push_refused(self.push(), fresh)

    def assert_unproven_refused(self, r, fresh, cause):
        """A pushurl or pushInsteadOf is not proven, so the scan reads
        everything before any query; PushDestinationProofTest holds the rest."""
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("full scan: 'origin': push destination not proven to be "
                      "the queried one (%s)" % cause, r.stderr)
        self.assertIn("vendor/upstream.md: /home/test-user/", r.stderr)
        self.assertEqual(self.git(fresh, "for-each-ref").stdout, "",
                         "the fresh repository must receive nothing")

    def test_a_pushurl_to_a_fresh_repository_reads_everything(self):  # noqa: VACUOUS_ASSERTION — the fresh repository's empty ref list is read beside the named refusal
        fresh = self.fresh_remote()
        self.config("remote.origin.pushurl", fresh)
        self.clean_commit()
        self.assert_unproven_refused(self.push(), fresh,
                                     "a pushurl that differs from url")

    def test_a_push_insteadof_to_a_fresh_repository_reads_everything(self):  # noqa: VACUOUS_ASSERTION — the fresh repository's empty ref list is read beside the named refusal
        fresh = self.fresh_remote()
        self.config("url.%s.pushInsteadOf" % fresh, self.remote)
        self.clean_commit()
        self.assert_unproven_refused(self.push(), fresh,
                                     "a pushInsteadOf rewrite")

    def test_a_ref_line_sha_the_destination_does_not_advertise_is_ignored(self):  # noqa: VACUOUS_ASSERTION — the empty hits are the control, read against the named hit that follows on the same call
        """The ref line names the published sha; the destination is empty,
        so the sha is not the destination's statement and is not a base."""
        head = self.clean_commit()
        hits, _notes = self.scan(head, self.remote, old=self.published)
        self.assertEqual(hits, [], "control: an advertised sha narrows")
        hits, notes = self.scan(head, self.fresh_remote(), old=self.published)
        self.assertEqual(hits, [("vendor/upstream.md", "/home/test-user/")])
        self.assertIn("full scan: 'origin': the destination advertises no "
                      "branch", notes[0][1])

    def test_an_advertised_sha_this_repo_lacks_is_left_out(self):  # noqa: VACUOUS_ASSERTION — the remote is pinned to the non-empty pushed sha
        """The destination also advertises a commit pushed from another
        clone. rev-list cannot walk it, so it is left out of the base and
        the published commit still narrows the scan."""
        other = os.path.join(self.tmp, "other")
        r = self.git(self.tmp, "clone", "-q", "-b", "main", self.remote, other)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(other, "elsewhere.md"), "w") as f:
            f.write("pushed from another clone\n")
        for args in (("add", "-A"),
                     ("-c", "user.email=o@example.com", "-c", "user.name=o",
                      "commit", "-q", "-m", "elsewhere"),
                     ("push", "-q", "origin", "HEAD:refs/heads/other")):
            r = self.git(other, *args)
            self.assertEqual(r.returncode, 0, r.stderr)
        lacked = self.git(other, "rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(self.git(self.root, "cat-file", "-e",
                                     lacked).returncode, 0)
        head = self.clean_commit()
        r = self.push()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.remote_sha(), head)
        self.assertNotIn("full scan", r.stderr)

    def test_ls_remote_that_fails_reads_everything(self):
        head = self.clean_commit()
        nowhere = os.path.join(self.tmp, "nowhere.git")
        hits, notes = self.scan(head, nowhere)
        self.assertEqual(hits, [("vendor/upstream.md", "/home/test-user/")])
        self.assertIn("full scan: 'origin': not reachable", notes[0][1])
        self.assertNotIn(nowhere, notes[0][1])

    def stub_ls_remote(self, result):
        real = hostpath_guard._git

        def git(root, *args, **kw):
            if args[:1] == ("ls-remote",):
                if isinstance(result, BaseException):
                    raise result
                return result
            return real(root, *args, **kw)

        return mock.patch.object(hostpath_guard, "_git", git)

    def test_ls_remote_that_times_out_reads_everything(self):  # noqa: VACUOUS_ASSERTION — the empty hits are the control, read against the named hit that follows on the same call
        head = self.clean_commit()
        hits, _notes = self.scan(head, self.remote)
        self.assertEqual(hits, [], "control: an answering ls-remote narrows")
        with self.stub_ls_remote(subprocess.TimeoutExpired("git", 30)):
            hits, notes = self.scan(head, self.remote)
        self.assertEqual(hits, [("vendor/upstream.md", "/home/test-user/")])
        self.assertIn("full scan: 'origin': ls-remote timed out", notes[0][1])

    def test_an_advertisement_it_cannot_parse_reads_everything(self):
        head = self.clean_commit()
        with self.stub_ls_remote((0, "%s\trefs/heads/main\nnot a ref line\n"
                                  % self.published, "")):
            hits, notes = self.scan(head, self.remote)
        self.assertEqual(hits, [("vendor/upstream.md", "/home/test-user/")])
        self.assertIn("full scan: 'origin': ls-remote returned no parseable "
                      "refs", notes[0][1])
        self.assertNotIn("not a ref line", notes[0][1])

# SYNTHETIC credentials only: a push URL can carry a real one. Every
# userinfo spelling the diagnostic must never echo: a plain password with a
# query token, an apostrophe, every RFC 3986 sub-delim with percent-encoded
# bytes, an "@" in the password as %40, and the scp user:password@host: form.
CRED_URLS = (
    "https://person:synthetic-secret@example.invalid/demo?token=synthetic-tok",
    "https://per'son:syn'thetic-secret@example.invalid/demo",
    "https://us!$&'()*+,;=er:pa!$&'()*+,;=ss%2Fsynthetic-secret"
    "@example.invalid/demo",
    "https://person:synthetic%40secret@example.invalid/demo#frag-synthetic",
    "person:synthetic-secret@example.invalid:demo.git",
)
CRED_URL = CRED_URLS[0]
# Every distinctive piece of those secrets, in every spelling: usernames,
# passwords (raw and percent-decoded), the token, the fragment, the host and
# the path. None of them may appear in any output or return value.
CRED_BYTES = (
    "person", "per'son", "us!$&'()*+,;=er", "pa!$&'()*+,;=ss", "synthetic",
    "thetic-secret", "secret", "%40", "%2F", "token", "frag-", "example",
    ".invalid", "demo",
)


def echoing_stderr(url):
    """A git stderr that repeats the URL raw, percent-decoded and
    percent-encoded, and the password alone."""
    from urllib.parse import quote, unquote
    return ("fatal: unable to access '%s/': Could not resolve host\n"
            "fatal: '%s' via %s: synthetic-secret\n"
            % (url, unquote(url), quote(url, safe="")))


class PushUrlCredentialsNeverPrintTest(_PushSandbox):
    """A push URL can carry a credential in any spelling, and git echoes the
    URL in its errors. No diagnostic contains the push URL or git's stderr in
    ANY form: it names the push by the remote NAME git passed, or "a URL
    remote" when that name is a URL, and gives a fixed reason. Every arm
    checks every spelling of every secret in stderr AND in the return values,
    with a positive control that the name and the reason ARE there, so an
    empty output cannot pass. The destination query is stubbed, so no arm
    reaches the network."""

    def setUp(self):
        super().setUp()
        self.write("vendor/upstream.md", HOST_PATH_BLOB)
        self.commit("history with a host path")
        r = self.git(self.root, "push", "-q", "--no-verify", "origin", "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.write("docs/clean.md", "nothing host-shaped here\n")
        self.head = self.commit("clean")

    def assert_no_credential(self, *texts):
        text = "\n".join(str(t) for t in texts)
        for piece in CRED_BYTES:
            self.assertNotIn(piece, text)

    def ls_remote(self, result):
        real = hostpath_guard._git

        def git(root, *args, **kw):
            if args[:1] == ("ls-remote",):
                if isinstance(result, BaseException):
                    raise result
                if result == "real":
                    return real(root, "ls-remote", "--heads", "--tags",
                                self.remote, feed="")
                return result
            return real(root, *args, **kw)

        return mock.patch.object(hostpath_guard, "_git", git)

    def main(self, name, url):
        """The pre-push entry with git's argv: `git push <url>` passes the
        URL as the name too; a configured remote passes its NAME."""
        err = io.StringIO()
        here = os.getcwd()
        os.chdir(self.root)
        try:
            with mock.patch("sys.stdin", io.StringIO(
                    "refs/heads/main %s refs/heads/main %s\n"
                    % (self.head, _ZERO))), proven_push():
                with contextlib.redirect_stderr(err):
                    rc = hostpath_guard.main(["--pre-push", name, url])
        finally:
            os.chdir(here)
        return rc, err.getvalue()

    def every_path(self, outcome, reason):
        """Run `outcome(url)`, one ls-remote result that repeats the URL, for
        every spelling, as a URL push and as a configured remote holding
        that URL. -> nothing; asserts."""
        for url in CRED_URLS:
            for name, label in ((url, "a URL remote"), ("origin", "'origin'")):
                with self.subTest(url=url, name=label), \
                        self.ls_remote(outcome(url)):
                    rc, err = self.main(name, url)
                    with proven_push():
                        base, why = hostpath_guard._advertised_base(
                            self.root, name, url,
                            hostpath_guard._remote_config(self.root))
                        hits, notes = hostpath_guard.scan_push(
                            self.root, _ZERO, self.head, name, remote_url=url)
                    self.assertEqual(rc, 1, err)
                    self.assertIn("full scan: %s: %s" % (label, reason), err)
                    self.assertIn("[helm hostpath] REFUSED: 1 host-path match",
                                  err)
                    self.assertEqual((base, why), ([], reason))
                    self.assertTrue(hits)
                    self.assert_no_credential(err, why, notes)

    def test_a_failed_query_that_echoes_the_url_prints_no_credential(self):  # noqa: VACUOUS_ASSERTION — every absent byte is read against the name and reason pinned in the same text
        self.every_path(lambda url: (128, "", echoing_stderr(url)),
                        "not reachable")

    def test_a_timed_out_query_prints_no_credential(self):  # noqa: VACUOUS_ASSERTION — every absent byte is read against the name and reason pinned in the same text
        self.every_path(lambda url: subprocess.TimeoutExpired(
            ("git", "ls-remote", "--heads", "--tags", url), 30),
            "ls-remote timed out")

    def test_a_query_exiting_without_a_marker_prints_only_its_code(self):  # noqa: VACUOUS_ASSERTION — every absent byte is read against the name and reason pinned in the same text
        self.every_path(lambda url: (2, "", "error: %s synthetic-secret\n"
                                     % url), "ls-remote exited 2")

    def test_a_query_printing_the_url_as_a_ref_prints_no_credential(self):  # noqa: VACUOUS_ASSERTION — every absent byte is read against the name and reason pinned in the same text
        self.every_path(lambda url: (0, "%s\n" % url, ""),
                        "ls-remote returned no parseable refs")

    def test_the_allowed_path_names_the_remote_without_its_credential(self):  # noqa: VACUOUS_ASSERTION — every absent byte is read against the name pinned in the same note
        for url in CRED_URLS:
            for name, label in ((url, "a URL remote"), ("origin", "'origin'")):
                with self.subTest(url=url, name=label), \
                        self.ls_remote("real"), proven_push():
                    rc, err = self.main(name, url)
                    hits, notes = hostpath_guard.scan_push(
                        self.root, _ZERO, self.head, name, remote_url=url)
                    self.assertEqual(rc, 0, err)
                    self.assertEqual(hits, [])
                    self.assertIn("1 blob(s) scanned, the ones %s does not "
                                  "already have" % label, err)
                    self.assert_no_credential(err, notes)

    def test_the_private_skip_names_the_remote_without_its_credential(self):  # noqa: VACUOUS_ASSERTION — every absent byte is read against the name pinned in the same note
        for url in CRED_URLS:
            for name, label in ((url, "a URL remote"), ("origin", "'origin'")):
                with self.subTest(url=url, name=label), \
                        mock.patch.object(hostpath_guard, "_visibility",
                                          return_value=_PRIVATE):
                    rc, err = self.main(name, url)
                    _hits, notes = hostpath_guard.scan_push(
                        self.root, _ZERO, self.head, name, remote_url=url)
                    self.assertEqual(rc, 0, err)
                    self.assertIn("remote %s is private" % label, err)
                    self.assert_no_credential(err, notes)

    def test_the_label_is_the_name_or_a_url_remote(self):  # noqa: VACUOUS_ASSERTION — every case is an equality against a non-empty expected string
        """A NAME prints only when `remote.<name>.url` is configured."""
        config = [("remote.origin.url", "/srv/a.git"),
                  ("remote.fork-2.pub.url", "/srv/b.git"),
                  ("remote.nourl.pushurl", "/srv/c.git")]
        cases = dict.fromkeys(CRED_URLS, "a URL remote")
        cases.update({
            "origin": "'origin'",
            "fork-2.pub": "'fork-2.pub'",
            "notaremote": "a URL remote",
            "nourl": "a URL remote",
            "/synthetic/host/path": "a URL remote",
            "../synthetic-host-path.git": "a URL remote",
            "git@github.com:akapug/dregg.git": "a URL remote",
            "example.invalid:demo.git": "a URL remote",
            "file:///srv/demo.git": "a URL remote",
            "": "a URL remote",
            None: "a URL remote",
        })
        for name, want in cases.items():
            self.assertEqual(hostpath_guard._remote_label(config, name), want,
                             name)
        self.assertEqual(hostpath_guard._remote_label(None, "origin"),
                         "a URL remote", "an unreadable config names nothing")

    def test_a_scan_that_times_out_names_the_verb_not_the_command(self):  # noqa: VACUOUS_ASSERTION — every absent byte is read against the verb pinned in the same text
        stall = subprocess.TimeoutExpired(("git", "rev-list", CRED_URL), 60)
        got = hostpath_guard._scan_failure(stall)
        self.assertEqual(got, "git rev-list timed out")
        self.assert_no_credential(got)
        for exc in (RuntimeError(CRED_URL), ValueError(CRED_URL)):
            self.assertEqual(hostpath_guard._scan_failure(exc),
                             "internal error (%s)" % type(exc).__name__)


class PushDestinationProofTest(_PushSandbox):
    """The advertised base holds only when the push provably goes where
    ls-remote looked. MEASURED: `remote.origin.receivepack` wrote the pack into
    an empty repository B while the URL named A; the scan excluded A's
    objects, the push exited 0 and B received the old host-path blob. Every
    arm here starts with A (origin) holding a host path and a clean commit on
    top; where the destination is not proven, the push must read the old blob
    and REFUSE. Real pushes run the real /proc walk to the `git push`."""

    NOT_PROVEN = ("full scan: 'origin': push destination not proven to be "
                  "the queried one (%s)")

    def setUp(self):
        super().setUp()
        self.write("vendor/upstream.md", HOST_PATH_BLOB)
        self.published = self.commit("history with a host path")
        r = self.git(self.root, "push", "-q", "--no-verify", "origin", "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.write("docs/clean.md", "nothing host-shaped here\n")
        self.head = self.commit("clean")

    def repo(self, name, clone=False):
        """A second bare repository B: empty, or a clone of A holding the
        published commit."""
        path = os.path.join(self.tmp, name)
        args = (("clone", "-q", "--bare", self.remote, path) if clone
                else ("init", "-q", "--bare", path))
        r = self.git(self.tmp, *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return path

    def config(self, *args):
        r = self.git(self.root, "config", *args)
        self.assertEqual(r.returncode, 0, r.stderr)

    def refs(self, path):
        return self.git(path, "for-each-ref", "--format=%(objectname)").stdout

    def assert_old_blob_read_and_refused(self, r, cause):
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn(self.NOT_PROVEN % cause, r.stderr)
        self.assertIn("[helm hostpath] REFUSED: 1 host-path match", r.stderr)
        self.assertIn("vendor/upstream.md: /home/test-user/", r.stderr)
        self.assertEqual(self.remote_sha(), self.published)

    def test_a_proven_push_still_excludes_what_the_destination_holds(self):  # noqa: VACUOUS_ASSERTION — the remote is pinned to the non-empty pushed sha
        """Positive control: a plain push reads its own argv from /proc,
        proves the destination, and reads only the new blob."""
        r = self.push()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.remote_sha(), self.head)
        self.assertIn("1 blob(s) scanned, the ones 'origin' does not already "
                      "have — no host-path matches", r.stderr)
        self.assertNotIn("full scan", r.stderr)

    def test_a_configured_receive_pack_into_an_empty_repository_is_refused(self):  # noqa: VACUOUS_ASSERTION — the empty or unchanged B is read beside the named refusal and A pinned to the non-empty published sha
        b = self.repo("b.git")
        self.config("remote.origin.receivepack",
                    "git receive-pack %s #" % shlex.quote(b))
        r = self.push()
        self.assert_old_blob_read_and_refused(r, "a custom receive-pack")
        self.assertEqual(self.refs(b), "", "B must receive nothing")

    def test_a_receive_pack_in_the_push_argv_is_refused(self):  # noqa: VACUOUS_ASSERTION — the empty or unchanged B is read beside the named refusal and A pinned to the non-empty published sha
        b = self.repo("b.git")
        r = self.git(self.root, "push", "--receive-pack=git receive-pack %s #"
                     % shlex.quote(b), "origin", "main")
        self.assert_old_blob_read_and_refused(
            r, "a receive-pack in the push argv")
        self.assertEqual(self.refs(b), "", "B must receive nothing")

    def test_a_pushurl_that_differs_is_refused(self):  # noqa: VACUOUS_ASSERTION — the empty or unchanged B is read beside the named refusal and A pinned to the non-empty published sha
        """B holding A's history is where the old code narrowed by B's own
        advertisement; an empty B is the first-push cell."""
        for clone in (True, False):
            with self.subTest(b_holds_a=clone):
                b = self.repo("pushurl-%s.git" % clone, clone=clone)
                before = self.refs(b)
                self.config("remote.origin.pushurl", b)
                r = self.push()
                self.git(self.root, "config", "--unset", "remote.origin.pushurl")
                self.assertNotEqual(r.returncode, 0, r.stderr)
                self.assertIn(self.NOT_PROVEN
                              % "a pushurl that differs from url", r.stderr)
                self.assertIn("vendor/upstream.md: /home/test-user/", r.stderr)
                self.assertEqual(self.refs(b), before)

    def test_a_push_insteadof_rewrite_is_refused(self):  # noqa: VACUOUS_ASSERTION — the empty or unchanged B is read beside the named refusal and A pinned to the non-empty published sha
        for clone in (True, False):
            with self.subTest(b_holds_a=clone):
                b = self.repo("rewrite-%s.git" % clone, clone=clone)
                before = self.refs(b)
                self.config("url.%s.pushInsteadOf" % b, self.remote)
                r = self.push()
                self.git(self.root, "config", "--unset",
                         "url.%s.pushInsteadOf" % b)
                self.assertNotEqual(r.returncode, 0, r.stderr)
                self.assertIn(self.NOT_PROVEN % "a pushInsteadOf rewrite",
                              r.stderr)
                self.assertIn("vendor/upstream.md: /home/test-user/", r.stderr)
                self.assertEqual(self.refs(b), before)

    def main(self, argv, stdin=None, git=None):
        err = io.StringIO()
        here = os.getcwd()
        os.chdir(self.root)
        patches = [mock.patch("sys.stdin", io.StringIO(
            stdin or "refs/heads/main %s refs/heads/main %s\n"
            % (self.head, self.published)))]
        if git is not None:
            patches.append(mock.patch.object(hostpath_guard, "_git", git))
        try:
            with contextlib.ExitStack() as st:
                for p in patches:
                    st.enter_context(p)
                st.enter_context(contextlib.redirect_stderr(err))
                rc = hostpath_guard.main(argv)
        finally:
            os.chdir(here)
        return rc, err.getvalue()

    def test_an_unreadable_push_argv_is_refused(self):
        with proven_push():
            rc, err = self.main(["--pre-push", "origin", self.remote])
        self.assertEqual(rc, 0, "control: a readable argv narrows\n" + err)
        with mock.patch.object(hostpath_guard, "_push_argv",
                               return_value=None):
            rc, err = self.main(["--pre-push", "origin", self.remote])
        self.assertEqual(rc, 1, err)
        self.assertIn(self.NOT_PROVEN % "push argv unreadable", err)
        self.assertIn("vendor/upstream.md: /home/test-user/", err)

    def test_the_push_argv_is_read_from_the_git_push_above_the_hook(self):
        proc = os.path.join(self.tmp, "proc")
        chain = {30: (b"python3\0scanner\0--pre-push\0", 20),
                 20: (b"/bin/sh\0.git/hooks/pre-push\0origin\0", 10),
                 10: (b"git\0push\0--receive-pack=x\0origin\0main\0", 5),
                 5: (b"git\0log\0", 1)}
        for pid, (cmdline, ppid) in chain.items():
            os.makedirs(os.path.join(proc, str(pid)))
            with open(os.path.join(proc, str(pid), "cmdline"), "wb") as f:
                f.write(cmdline)
            with open(os.path.join(proc, str(pid), "status"), "w") as f:
                f.write("Name:\tx\nPPid:\t%d\n" % ppid)
        argv = hostpath_guard._push_argv(pid=30, proc=proc)
        self.assertEqual(argv, ["git", "push", "--receive-pack=x", "origin",
                                "main"])
        self.assertEqual(hostpath_guard._argv_moves_the_push(argv),
                         "a receive-pack in the push argv")
        self.assertIsNone(hostpath_guard._push_argv(pid=5, proc=proc),
                          "a git that is not a push is not the push")
        os.unlink(os.path.join(proc, "20", "status"))
        self.assertIsNone(hostpath_guard._push_argv(pid=30, proc=proc),
                          "a chain that cannot be read proves nothing")
        for argv, want in (
                (["git", "push", "origin", "main"], None),
                (["git", "push", "--exec", "x", "origin"],
                 "a receive-pack in the push argv"),
                (["git", "push", "--rece=x", "origin"],
                 "a receive-pack in the push argv"),
                (["git", "-c", "Remote.origin.PUSHURL=x", "push"],
                 "a config override in the push argv"),
                (["git", "--config-env=url.x.pushInsteadOf=V", "push"],
                 "a config override in the push argv"),
                (["git", "push", "--recurse-submodules=no", "origin"], None)):
            self.assertEqual(hostpath_guard._argv_moves_the_push(argv), want,
                             argv)

    def test_a_path_remote_prints_no_byte_of_the_path(self):  # noqa: VACUOUS_ASSERTION — the absent path bytes are read against the label and reason pinned in the same lines
        """`git push <path>` and `git push file://<path>`: the name git
        passes is the path, and no remote is configured under it."""
        for spelling in ("%s", "file://%s"):
            with self.subTest(spelling=spelling):
                path = os.path.join(self.tmp, "synthetic-host-path",
                                    "%d.git" % len(spelling))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                self.assertEqual(self.git(self.tmp, "init", "-q", "--bare",
                                          path).returncode, 0)
                r = self.git(self.root, "push", spelling % path, "main")
                ours = "\n".join(ln for ln in r.stderr.splitlines()
                                 if ln.startswith("[helm hostpath]"))
                self.assertNotEqual(r.returncode, 0, r.stderr)
                self.assertIn("full scan: a URL remote: the destination "
                              "advertises no branch or tag (a first push)",
                              ours)
                for piece in ("synthetic-host-path", self.tmp, path):
                    self.assertNotIn(piece, ours)

    def test_unexpected_object_output_prints_none_of_it(self):  # noqa: VACUOUS_ASSERTION — the absent secret is read against the fixed reason pinned in the same text
        real = hostpath_guard._git
        secret = "synthetic-secret-listing %s" % CRED_URL

        def junk(verb):
            def git(root, *args, **kw):
                if args[:1] == ("ls-remote",):
                    return real(root, "ls-remote", "--heads", "--tags",
                                self.remote, feed="")
                if args[:2] == verb:
                    out = (secret + "\n").encode() if kw.get("binary") else (
                        secret + "\n")
                    return 0, out, out
                if args[:1] == ("cat-file",) and verb == ("fail",):
                    return 128, b"", (secret + "\n").encode()
                return real(root, *args, **kw)
            return git

        cases = ((("cat-file", "--batch-check=%(objectname) %(objecttype) "
                   "%(objectsize)"), "unreadable object listing"),
                 (("cat-file", "--batch"), "unreadable object listing"),
                 (("fail",), "object listing failed (exited 128)"))
        shapes = (("origin", self.remote), (self.remote, self.remote),
                  (CRED_URL, CRED_URL))
        for verb, reason in cases:
            for name, url in shapes:
                with self.subTest(verb=verb[-1], name=name[:12]), \
                        proven_push():
                    rc, err = self.main(["--pre-push", name, url],
                                        git=junk(verb))
                    self.assertEqual(rc, 2, err)
                    self.assertIn("[helm hostpath] REFUSED: push scan failed "
                                  "— %s" % reason, err)
                    for piece in ("synthetic", "example.invalid", self.tmp):
                        self.assertNotIn(piece, err)

    def test_malformed_pre_push_input_prints_none_of_it(self):  # noqa: VACUOUS_ASSERTION — the absent secret is read against the fixed words pinned in the same text
        good = "refs/heads/main %s refs/heads/main %s\n" % ("a" * 40, _ZERO)
        cases = (
            ("refs/heads/main synthetic-secret refs/heads/main %s\n" % _ZERO,
             "line 1: the local sha is not a sha"),
            ("refs/heads/main %s refs/heads/main synthetic-secret\n"
             % ("a" * 40), "line 1: the remote sha is not a sha"),
            ("synthetic-secret %s three\n" % CRED_URL, "line 1: 3 field(s), not 4"),
            (good + "refs/heads/x synthetic-secret refs/heads/x %s\n" % _ZERO,
             "line 2: the local sha is not a sha"),
        )
        for stdin, words in cases:
            for name, url in (("origin", self.remote),
                              (self.remote, self.remote),
                              (CRED_URL, CRED_URL)):
                with self.subTest(words=words, name=name[:12]):
                    rc, err = self.main(["--pre-push", name, url], stdin=stdin)
                    self.assertEqual(rc, 2, err)
                    self.assertIn("[helm hostpath] REFUSED: malformed "
                                  "pre-push input — " + words, err)
                    for piece in ("synthetic", "example.invalid", "refs/",
                                  self.tmp):
                        self.assertNotIn(piece, err)


SENTINEL = "hostpath-sentinel-9f3"
SENTINEL_URL = "https://%s@example.invalid/%s" % (SENTINEL, SENTINEL)
GH_SENTINEL_URL = "https://github.com/e/%s" % SENTINEL


class SentinelBoom(Exception):
    """What a planted boundary raises; its message is the sentinel."""


def boom(kind):
    """Raise a planted failure: the sentinel as a message, the ValueError a
    malformed size raises in int(), or an OSError carrying the sentinel."""
    if kind == "int":
        int(SENTINEL)
    if kind == "oserror":
        raise OSError(SENTINEL)
    raise SentinelBoom(SENTINEL)


def guard_boundaries():
    """Every place the guard takes an external input, from an AST walk of its
    source: each `_git` call keyed by its leading literal arguments, each
    subprocess call keyed by its program, and each `open`, `int`,
    `json.loads` and `sys.stdin.read`."""
    with open(hostpath_guard.__file__, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    keys = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func)
        if name == "_git":
            lead = []
            for arg in node.args[1:]:
                if not (isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)):
                    break
                lead.append(arg.value)
            keys.add(("_git",) + tuple(lead))
        elif name.startswith("subprocess."):
            argv = node.args[0] if node.args else None
            argv = argv.left if isinstance(argv, ast.BinOp) else argv
            first = (argv.elts[0] if isinstance(argv, ast.Tuple) and argv.elts
                     else None)
            keys.add((name, first.value if isinstance(first, ast.Constant)
                      else None))
        elif name in ("open", "int", "json.loads", "sys.stdin.read"):
            keys.add((name,))
    return keys


# Each boundary and the push that reaches it: "named" is a proven push to
# the configured origin with a GitHub URL (so gh runs), "handrun" a bare
# --pre-push (so the remote is resolved by name and the walk is full).
BOUNDARY_FORMS = {
    ("_git", "cat-file", "--batch"): "named",
    ("_git", "cat-file",
     "--batch-check=%(objectname) %(objecttype) %(objectsize)"): "named",
    ("_git", "cat-file", "--batch-check=%(objectname) %(objecttype)"): "named",
    ("_git", "config", "-z", "--get-regexp", "^(remote|url)\\."): "named",
    ("_git", "ls-remote", "--upload-pack=git-upload-pack", "--heads",
     "--tags"): "named",
    ("_git", "remote", "get-url"): "handrun",
    ("_git", "rev-list", "--objects"): "handrun",
    ("_git", "rev-list", "--objects", "--stdin"): "named",
    ("int",): "named",
    ("json.loads",): "named",
    ("open",): "named",
    ("subprocess.run", "gh"): "named",
    ("subprocess.run", "git"): "named",
    ("sys.stdin.read",): "named",
}
GIT_KEYS = [k for k in BOUNDARY_FORMS if k[0] == "_git"]
FORMS = {"named": ["--pre-push", "origin", GH_SENTINEL_URL],
         "url": ["--pre-push", SENTINEL_URL, SENTINEL_URL],
         "handrun": ["--pre-push"]}


class GuardInputsNeverPrintTest(_PushSandbox):
    """No input the guard reads ever reaches its output. The sentinel is
    planted in every external input, each git call's stdout and stderr, gh's,
    pre-push stdin, the remote name and URL, the /proc argv and the
    environment, and every door is driven. Each run records which inputs
    actually carried the sentinel INTO the guard, the must-hit control, so a
    door whose input never arrives cannot pass vacuously. The history holds
    a published host path, and the pushed commit adds another whose blob
    carries the sentinel beside the match."""

    def setUp(self):
        super().setUp()
        self.write("vendor/upstream.md", HOST_PATH_BLOB)
        self.published = self.commit("history with a host path")
        r = self.git(self.root, "push", "-q", "--no-verify", "origin", "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.write("src/new.py", 'P = "/Users/dev/Library/app"  # %s\n'
                   % SENTINEL)
        self.head = self.commit("a new host path beside the sentinel")
        self.empty = os.path.join(self.tmp, "empty.git")
        r = self.git(self.tmp, "init", "-q", "--bare", self.empty)
        self.assertEqual(r.returncode, 0, r.stderr)

    @staticmethod
    def plant(key, out, plant):
        """-> `out` with the sentinel planted for `key`, or None."""
        text = isinstance(out, str)
        tag = SENTINEL if text else SENTINEL.encode()
        nl = "\n" if text else b"\n"
        if key[1] == "config":
            # In VALUES: a configured remote NAME is what the label prints by
            # design, so a remote named after the sentinel would print it.
            return out + ("remote.origin.fetch\n+refs/heads/*:refs/%s/*\0"
                          "url.%s.insteadof\n%s-prefix\0"
                          % (SENTINEL, SENTINEL, SENTINEL))
        if key[1] == "remote" or key[1] == "rev-list":
            return out + nl + tag + nl
        if key == GIT_KEYS[2]:                      # the existence check
            return out + "%s missing\n" % SENTINEL
        if key[1] == "ls-remote" and "advert" in plant:
            return out + tag + nl
        if key == GIT_KEYS[1] and "listing" in plant:
            return out + tag + nl
        if key == GIT_KEYS[1] and "size-listing" in plant:
            lines = out.split(nl)                   # the first BLOB's size
            at = next(i for i, ln in enumerate(lines)
                      if ln.split()[1:2] == [b"blob"])
            lines[at] = b" ".join(lines[at].split()[:2] + [tag])
            return nl.join(lines)
        if key == GIT_KEYS[0] and "size-header" in plant:
            head, _sep, rest = out.partition(nl)
            return b" ".join(head.split()[:2] + [tag]) + nl + rest
        return None

    def drive(self, form, stdin=None, raise_at=None, kind="message",
              plant=(), destination=None, env=None, direct=False):
        """One guarded run with the sentinel in every input it reaches.
        -> (rc, stdout + stderr, seen, direct). `seen` names each input that
        carried the sentinel into the guard; `direct` is what scan_push
        returned or raised, called under the same inputs, as text, with the
        raised exception's chained context."""
        seen, argv = set(), FORMS[form]
        real_git, real_run = hostpath_guard._git, subprocess.run
        real_label = hostpath_guard._remote_label
        dest = destination or self.remote

        def key_of(args):
            match = [k for k in GIT_KEYS if args[:len(k) - 1] == k[1:]]
            return max(match, key=len) if match else ("_git",) + args[:1]

        def raising(key):
            if raise_at == key:
                seen.add("raised")
                boom(kind)

        def git(root, *args, **kw):
            key = key_of(args)
            raising(key)
            if args[0] == "ls-remote":
                if SENTINEL in args[-1]:
                    seen.add("url")
                args = args[:-1] + (dest,)
            if args[0] == "remote" and SENTINEL in args[-1]:
                seen.add("env")
            rc, out, err = real_git(root, *args, **kw)
            text = isinstance(out, str)
            err = (err or ("" if text else b"")) + (
                "\n%s\n" % SENTINEL if text else b"\n" + SENTINEL.encode())
            seen.add("stderr:" + args[0])
            if not text and key == GIT_KEYS[0] and SENTINEL.encode() in out:
                seen.add("blob")
            planted = self.plant(key, out, plant)
            if planted is not None:
                out = planted
                seen.add("stdout:" + args[0])
            return rc, out, err

        def run(cmd, *a, **kw):
            if cmd[0] in ("git", "gh"):
                raising(("subprocess.run", cmd[0]))
            if cmd[0] == "gh":
                seen.add("gh")
                return subprocess.CompletedProcess(cmd, 0, SENTINEL, SENTINEL)
            return real_run(cmd, *a, **kw)

        def label(config, name):
            if name and SENTINEL in name:
                seen.add("name")
            return real_label(config, name)

        class Stdin:
            def read(self_):
                raising(("sys.stdin.read",))
                body = stdin if stdin is not None else (
                    "refs/heads/%s %s refs/heads/%s %s\n"
                    % (SENTINEL, self.head, SENTINEL, self.published))
                if SENTINEL in body:
                    seen.add("stdin")
                return body

        def proc_argv(*_a, **_k):
            seen.add("proc")
            return ["git", "push", SENTINEL, "main"]

        def raiser(key):
            return lambda *_a, **_k: raising(key)

        out, err, here = io.StringIO(), io.StringIO(), os.getcwd()
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.dict(os.environ, env or {}))
            if not env:
                os.environ.pop("HELM_HOSTPATH_REMOTE", None)
            st.enter_context(mock.patch.object(hostpath_guard, "_git", git))
            st.enter_context(mock.patch.object(subprocess, "run", run))
            st.enter_context(mock.patch.object(hostpath_guard,
                                               "_remote_label", label))
            st.enter_context(mock.patch("sys.stdin", Stdin()))
            if raise_at == ("open",):
                st.enter_context(mock.patch.object(
                    hostpath_guard, "open", create=True,
                    side_effect=raiser(("open",))))
            else:
                st.enter_context(mock.patch.object(
                    hostpath_guard, "_push_argv", proc_argv))
            if raise_at == ("int",):
                st.enter_context(mock.patch.object(
                    hostpath_guard, "int", create=True,
                    side_effect=raiser(("int",))))
            if raise_at == ("json.loads",):
                st.enter_context(mock.patch.object(
                    hostpath_guard.json, "loads",
                    side_effect=raiser(("json.loads",))))
            os.chdir(self.root)
            st.callback(os.chdir, here)
            st.enter_context(contextlib.redirect_stdout(out))
            st.enter_context(contextlib.redirect_stderr(err))
            rc = hostpath_guard.main(list(argv))
            got = None
            if direct:
                name = argv[1] if len(argv) > 1 else "origin"
                try:
                    got = repr(hostpath_guard.scan_push(
                        self.root, _ZERO, self.head, name,
                        remote_url=argv[2] if len(argv) > 2 else None))
                except hostpath_guard._ScanError as exc:
                    got = "%s %r %r %r" % (exc, exc, exc.__context__,
                                          exc.__cause__)
                    self.assertIsNone(exc.__context__)
                    self.assertIsNone(exc.__cause__)
        return rc, out.getvalue() + err.getvalue(), seen, got

    def test_every_boundary_in_the_module_is_armed(self):  # noqa: VACUOUS_ASSERTION — a set equality against the non-empty table of forms
        """A new subprocess call, `_git` call, `open`, `int`, `json.loads`
        or stdin read without an entry in BOUNDARY_FORMS fails here."""
        self.assertEqual(guard_boundaries(), set(BOUNDARY_FORMS))

    def test_a_raise_at_every_boundary_refuses_in_fixed_words(self):  # noqa: VACUOUS_ASSERTION — the absent sentinel is read against the refusal and the must-hit raise pinned in the same run
        for key, form in sorted(BOUNDARY_FORMS.items(), key=repr):
            for kind in ("message", "int"):
                with self.subTest(boundary=key, kind=kind):
                    rc, text, seen, got = self.drive(
                        form, raise_at=key, kind=kind,
                        direct=key != ("sys.stdin.read",))
                    self.assertIn("raised", seen, "must-hit: the boundary "
                                  "was never reached")
                    self.assertNotEqual(rc, 0, text)
                    self.assertIn("[helm hostpath] REFUSED", text)
                    self.assertNotIn(SENTINEL, text)
                    self.assertNotIn(SENTINEL, got or "")

    def test_the_sentinel_in_every_input_never_prints(self):  # noqa: VACUOUS_ASSERTION — the absent sentinel is read against the door's fixed text and the must-hit inputs pinned in the same run
        """door, form, overrides, rc, fixed text, inputs that must be hit."""
        scan = ("stdin", "proc")
        doors = (
            ("refusal", "named", {}, 1,
             "[helm hostpath] REFUSED: 1 host-path match",
             scan + ("url", "gh", "stderr:config", "stdout:config",
                     "stderr:ls-remote", "stderr:cat-file", "stdout:cat-file",
                     "stderr:rev-list", "stdout:rev-list", "blob")),
            ("scan failure", "named", {"plant": ("listing",)}, 2,
             "REFUSED: push scan failed — unreadable object listing",
             scan + ("url", "gh", "stdout:cat-file", "stdout:rev-list")),
            ("stdin parse", "url",
             {"stdin": "refs/heads/%s %s refs/heads/x %s\n"
              % (SENTINEL, SENTINEL, _ZERO)}, 2,
             "REFUSED: malformed pre-push input — line 1: the local sha "
             "is not a sha", ("stdin",)),
            ("first push", "url", {"destination": self.empty}, 1,
             "full scan: a URL remote: the destination advertises no "
             "branch or tag (a first push)",
             scan + ("name", "url", "stderr:ls-remote", "stdout:rev-list",
                     "blob")),
            ("unparseable advertisement", "url", {"plant": ("advert",)}, 1,
             "full scan: a URL remote: ls-remote returned no parseable refs",
             scan + ("name", "url", "stdout:ls-remote", "blob")),
            ("malformed size, batch-check line", "named",
             {"plant": ("size-listing",)}, 2,
             "REFUSED: push scan failed — unreadable object listing",
             scan + ("url", "stdout:cat-file")),
            ("malformed size, batch header", "named",
             {"plant": ("size-header",)}, 2,
             "REFUSED: push scan failed — unreadable object listing",
             scan + ("url", "stdout:cat-file")),
            ("unreadable stdin", "named",
             {"raise_at": ("sys.stdin.read",), "kind": "oserror"}, 2,
             "REFUSED: unreadable pre-push input", ("raised",)),
            ("raised at the top level", "named",
             {"raise_at": ("sys.stdin.read",)}, 2,
             "REFUSED: internal error (SentinelBoom)", ("raised",)),
            ("raised inside the scan", "named",
             {"raise_at": GIT_KEYS[3]}, 2,
             "REFUSED: push scan failed — internal error (SentinelBoom)",
             ("raised", "stdin")),
            ("environment", "handrun",
             {"env": {"HELM_HOSTPATH_REMOTE": SENTINEL}}, 1,
             "full scan: a URL remote: git did not pass this push's URL",
             ("stdin", "env", "name", "stderr:remote", "stdout:rev-list",
              "blob")),
        )
        for door, form, over, rc_want, fixed, must in doors:
            with self.subTest(door=door):
                rc, text, seen, _got = self.drive(form, **over)
                self.assertEqual(sorted(set(must) - seen), [],
                                 "must-hit: these inputs never carried the "
                                 "sentinel in")
                self.assertEqual(rc, rc_want, text)
                self.assertIn(fixed, text)
                self.assertNotIn(SENTINEL, text)


if __name__ == "__main__":
    unittest.main()
