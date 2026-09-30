#!/usr/bin/env python3
"""The pre-push host-path guard — refuse public pushes carrying host paths.

NO REAL HOST PATHS IN FIXTURES — every test path uses /home/test-user/,
/Users/dev/, or similar generic names. The test data lives in these fixtures
deliberately; the guard's regex IS expected to match them (they ARE host-path
shapes, by design).
"""
import ast
import contextlib
import http.server
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
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

    def test_a_push_url_git_sends_elsewhere_is_scanned(self):  # noqa: VACUOUS_ASSERTION — the loop runs over the literal ten-row _ELSEWHERE, each row pinning the one hit EQUAL and the UNKNOWN destination note
        """task/3413, RED on abeca0b48ac for every shape: the push URL reads
        as github.com/owner/private, gh reads that PRIVATE, and the scan was
        skipped, while git sends the push elsewhere (`_ELSEWHERE`). The
        push's own argv is unreadable here, so no ls-remote runs: nothing
        reaches a network."""
        self._write("tests/config.py", 'HOME = "/home/test-user/app"\n')
        new = self._commit("add host path")
        with mock.patch.object(hostpath_guard, "_gh_visibility",
                               return_value=_PRIVATE), \
                mock.patch.object(hostpath_guard, "_push_argv",
                                  return_value=None):
            for url, reaches in _ELSEWHERE:
                with self.subTest(url=url, reaches=reaches):
                    hits, notes = hostpath_guard.scan_push(
                        self.tmp, _ZERO, new, "origin", remote_url=url)
                    self.assertEqual(hits, [("tests/config.py",
                                             "/home/test-user/")])
                    self.assertIn((hostpath_guard._DESTINATION,
                                   "a URL remote: visibility UNKNOWN (not a "
                                   "plain GitHub repository URL), treated as "
                                   "public"), notes)


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
                                              "github.com/owner/private-repo",
                                              "--json", "isPrivate"))

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

    @staticmethod
    def gh_by_host(argv, *_args, **_kwargs):
        """A stand-in gh that resolves the host as gh does (`HOST/OWNER/REPO`,
        else GH_HOST, else github.com) and reads github.com's repository as
        public and every other host's as private."""
        repo = argv[3]
        host = repo.split("/", 1)[0] if repo.count("/") >= 2 \
            else os.environ.get("GH_HOST") or "github.com"
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"isPrivate": host != "github.com"}),
            stderr="")

    def test_gh_host_cannot_answer_for_a_github_remote(self):  # noqa: VACUOUS_ASSERTION — both answers are asserted EQUAL to PUBLIC, the clean one first as the control
        """A bare `owner/repo` goes to whatever host GH_HOST names. MEASURED
        with gh 2.46: GH_HOST=bogus.invalid sent `akapug/helm` there, while
        `github.com/akapug/helm` was answered by github.com. An enterprise
        host holding a PRIVATE repository of the same name would skip the
        scan of a push to the PUBLIC one."""
        url = "git@github.com:owner/public-repo.git"
        with mock.patch.object(hostpath_guard.subprocess, "run",
                               side_effect=self.gh_by_host):
            clean = hostpath_guard._visibility(url)
            with mock.patch.dict(os.environ, {"GH_HOST": "ghe.example.com"}):
                steered = hostpath_guard._visibility(url)
        self.assertEqual(clean, _PUBLIC)            # the control
        self.assertEqual(steered, _PUBLIC)

    def test_only_github_com_itself_is_a_github_url(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a literal tuple of three URLs, each asserted EQUAL to a non-empty UNKNOWN answer
        """The host is read, not searched for: a host that merely ENDS in
        `github.com`, or a path that contains it, is not GitHub, and gh is
        never asked about the repository of the same name there."""
        cases = ("https://notgithub.com/owner/repo",
                 "git@notgithub.com:owner/repo.git",
                 "https://git.example/github.com/owner/repo")
        with mock.patch.object(hostpath_guard.subprocess, "run",
                               side_effect=AssertionError("gh ran")):
            for url in cases:
                with self.subTest(url=url):
                    self.assertEqual(hostpath_guard._visibility(url),
                                     (hostpath_guard.UNKNOWN,
                                      "not a GitHub URL"))

    def test_a_repository_name_holding_dot_git_is_asked_as_named(self):
        """Only a trailing `.git` is git's suffix. `owner/owner.github.io` is
        a Pages repository, and removing every `.git` asked gh about
        `owner/ownerhub.io`, another repository."""
        with mock.patch.object(hostpath_guard.subprocess, "run") as mr:
            mr.return_value.returncode = 0
            mr.return_value.stdout = json.dumps({"isPrivate": False})
            got = hostpath_guard._visibility(
                "https://github.com/owner/owner.github.io.git")
        self.assertEqual(got, _PUBLIC)
        self.assertEqual(mr.call_args[0][0][3],
                         "github.com/owner/owner.github.io")

    @staticmethod
    def gh_private(argv, *_args, **_kwargs):
        """A stand-in gh that reads every repository PRIVATE."""
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"isPrivate": True}), stderr="")

    def test_a_url_git_sends_elsewhere_is_unknown_without_asking(self):  # noqa: VACUOUS_ASSERTION — each shape is pinned EQUAL to the non-empty UNKNOWN answer beside the must-hit GitHub slug; the zero gh calls are read against test_a_plain_github_url_still_reads_its_slug, which asks
        """task/3413, RED on abeca0b48ac for every shape: each URL reads as
        github.com/owner/private, which gh reads PRIVATE, so the scan was
        skipped, while git sends the push to another host or another
        repository (`_ELSEWHERE`, each MEASURED). A URL that is not a plain
        GitHub repository URL is UNKNOWN, and gh is not asked."""
        with mock.patch.object(hostpath_guard.subprocess, "run",
                               side_effect=self.gh_private) as mr:
            for url, reaches in _ELSEWHERE:
                with self.subTest(url=url, reaches=reaches):
                    self.assertEqual(hostpath_guard._github_slug(url),
                                     ("owner/private", None),
                                     "must-hit: read as GitHub's")
                    self.assertEqual(hostpath_guard._visibility(url),
                                     (hostpath_guard.UNKNOWN,
                                      "not a plain GitHub repository URL"))
        self.assertEqual(mr.call_count, 0)

    def test_a_plain_github_url_still_reads_its_slug(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a literal tuple of four URLs, each asserted EQUAL to PRIVATE with the slug gh was asked
        """Control for the arm above: a plain https, ssh:// or scp URL is
        asked about as the owner/repo it names."""
        for url in ("https://github.com/owner/private",
                    "https://github.com/owner/private.git/",
                    "ssh://git@github.com/owner/private.git",
                    "git@github.com:owner/private.git"):
            with self.subTest(url=url), mock.patch.object(
                    hostpath_guard.subprocess, "run",
                    side_effect=self.gh_private) as mr:
                self.assertEqual(hostpath_guard._visibility(url), _PRIVATE)
                self.assertEqual(mr.call_args[0][0][3],
                                 "github.com/owner/private")


# task/3413: URLs `_github_slug` reads as github.com/owner/private while git
# reaches another host or repository, and where. MEASURED (git 2.53, libcurl
# 8.18) through a local logging proxy (http.proxy), a logging GIT_SSH_COMMAND
# and a local path, so nothing reached a network.
_ELSEWHERE = (
    ("https://evil.example#@github.com/owner/private",
     "CONNECT evil.example:443: a '#' ends the host for curl"),
    ("https://evil.example?@github.com/owner/private",
     "CONNECT evil.example:443: a '?' ends the host for curl"),
    ("https://evil.example:8443#@github.com/owner/private",
     "CONNECT evil.example:8443"),
    ("http://evil.example?@github.com/owner/private",
     "GET http://evil.example/?@github.com/owner/private/info/refs"
     "&service=git-receive-pack"),
    ("https://github.com/owner/private/../public",
     "https://github.com/owner/public: curl removes the dot segments"),
    ("https://github.com/owner/private/%2e%2e/public",
     "https://github.com/owner/public: and percent-encoded ones"),
    ("https://github.com/owner/private/./../public.git",
     "https://github.com/owner/public.git"),
    ("ssh://git@github.com/owner/private/../public.git",
     "git-receive-pack '/owner/private/../public.git' as written"),
    ("git@github.com:owner/private/../public.git",
     "git-receive-pack 'owner/private/../public.git' as written"),
    ("file://github.com/owner/private",
     "the local path /owner/private: file:// drops the host"),
)


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
    ARGV = ["repo", "view", "github.com/example/leaky", "--json",
            "isPrivate"]
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
        # -b main: the remote's HEAD names the branch the seed lands on, as
        # a real remote's does. A HEAD naming a branch that does not exist
        # advertises no default branch, and the shared-history rung, which
        # runs first in the same hook, refuses what that remote does not
        # already hold.
        r = self.git(self.tmp, "init", "-q", "--bare", "-b", "main",
                     self.remote)
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

    def git(self, cwd, *args, env=None):
        return subprocess.run(("git",) + args, cwd=cwd, capture_output=True,
                              text=True, timeout=120,
                              env=None if env is None
                              else dict(os.environ, **env))

    def cli(self, *args):
        """The shipped `helm work` dispatcher, never install_guard directly."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = work.cmd_work(list(args) + ["--repo", self.root])
        return rc, out.getvalue(), err.getvalue()

    def through_the_release_door(self):
        """The environment that passes the shared-history rung, which runs
        first in the same hook and refuses a steered push (a pushurl, a
        pushInsteadOf, a receive-pack) outright: the release marker naming
        the one commit these arms push. Behind it, the host-path scan is
        the rung under test, with the same steered proof to judge."""
        head = self.git(self.root, "rev-parse", "HEAD").stdout.strip()
        return {hostpath_guard.RELEASE_MARKER: head}

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

    def push(self, refspec="main", env=None):
        return self.git(self.root, "push", "origin", refspec, env=env)

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
        self.assertIn("# helm work managed hook: pre-push v5", out)
        self.assertNotIn("preserves existing hook as %s" % self.user, out)
        with open(self.hook) as f:
            self.assertEqual(f.read(), v2)
        rc, _out, err = self.cli("install-guard", "--apply", "--profile", "leak")
        self.assertEqual(rc, 0, err)
        with open(self.hook) as f:
            self.assertEqual(f.read().splitlines()[1],
                             "# helm work managed hook: pre-push v5")
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
        self.assert_unproven_refused(
            self.push(env=self.through_the_release_door()), fresh,
            "a pushurl that differs from url")

    def test_a_push_insteadof_to_a_fresh_repository_reads_everything(self):  # noqa: VACUOUS_ASSERTION — the fresh repository's empty ref list is read beside the named refusal
        fresh = self.fresh_remote()
        self.config("url.%s.pushInsteadOf" % fresh, self.remote)
        self.clean_commit()
        self.assert_unproven_refused(
            self.push(env=self.through_the_release_door()), fresh,
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
        self.assertIn("[helm shared-history] the release publish of %s: passed"
                      % self.head[:12], r.stderr)
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
        r = self.push(env=self.through_the_release_door())
        self.assert_old_blob_read_and_refused(r, "a custom receive-pack")
        self.assertEqual(self.refs(b), "", "B must receive nothing")

    def test_git_config_in_the_environment_hides_no_receive_pack(self):  # noqa: VACUOUS_ASSERTION — the empty B is read beside the named refusal and A pinned to the non-empty published sha
        """GIT_CONFIG is read by `git config` alone; git push, and every other
        git call, ignores it. Under GIT_CONFIG=/dev/null the config read here
        held no remote entry, so the push was proven by A's URL and the scan
        excluded A's objects while the configured receive-pack wrote the pack
        into B. The proof reads the config git push reads."""
        b = self.repo("b.git")
        self.config("remote.origin.receivepack",
                    "git receive-pack %s #" % shlex.quote(b))
        r = self.push(env=dict(self.through_the_release_door(),
                               GIT_CONFIG=os.devnull))
        self.assert_old_blob_read_and_refused(r, "a custom receive-pack")
        self.assertEqual(self.refs(b), "", "B must receive nothing")

    def test_a_receive_pack_in_the_push_argv_is_refused(self):  # noqa: VACUOUS_ASSERTION — the empty or unchanged B is read beside the named refusal and A pinned to the non-empty published sha
        b = self.repo("b.git")
        r = self.git(self.root, "push", "--receive-pack=git receive-pack %s #"
                     % shlex.quote(b), "origin", "main",
                     env=self.through_the_release_door())
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
                r = self.push(env=self.through_the_release_door())
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
                r = self.push(env=self.through_the_release_door())
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

    def test_configured_steering_is_named_before_an_unreadable_argv(self):  # noqa: VACUOUS_ASSERTION — every row of the literal seven-row table pins a non-empty cause with assertEqual
        """An argv that cannot be read hides only what the argv alone carries
        (a --receive-pack): the config, which a -c override reaches through
        GIT_CONFIG_PARAMETERS, is read first, and "push argv unreadable" is
        the cause only when it shows no steering."""
        url = "file:///srv/a.git"
        for extra, dest, want in (
                ([], url, "push argv unreadable"),
                ([("remote.origin.pushurl", "/srv/b.git")], url,
                 "a pushurl that differs from url"),
                ([("url./srv/b.git.pushinsteadof", url)], url,
                 "a pushInsteadOf rewrite"),
                ([("url./srv/b.git.insteadof", url)], url,
                 "an insteadOf rewrite of the push URL"),
                ([("remote.origin.receivepack", "x")], url,
                 "a custom receive-pack"),
                ([("remote.origin.vcs", "x")], url, "a remote helper"),
                ([], "ext::x", "a remote helper")):
            with self.subTest(extra=extra, url=dest):
                self.assertEqual(hostpath_guard._unproven(
                    [("remote.origin.url", url)] + extra, "origin", dest,
                    None), want)

    def test_a_pin_of_the_push_name_to_the_url_git_passed_is_proven(self):  # noqa: VACUOUS_ASSERTION — every row of the literal table pins its cause or its None with assertEqual, and the three None rows are controls for the refusing rows beside them
        """Auto-land pushes through a one-time name its environment maps to
        the vetted URL (helm/autoland.py `_pinned`): an insteadOf and a
        pushInsteadOf whose value is the whole name and whose base is the
        URL git passes as pre-push $2. MEASURED on train415: the rung read
        that pin as "a pushInsteadOf rewrite" and refused a fast-forward of
        trunk. The pin is proof (`_pin`); a pin to another URL, a second
        rule on the name, a shorter prefix of it, a remote section named by
        it, a rewrite of the URL itself, the identity rewrite of a URL
        pushed by URL, and an unreadable argv each still refuse."""
        url, other = "file:///srv/a.git", "file:///srv/b.git"
        name = "helm-autoland-push/" + "5" * 32

        def pin(base, *var):
            return [("url.%s.%s" % (base, v), name)
                    for v in var or ("insteadof", "pushinsteadof")]
        argv = ["git", "push", name, "%s:refs/heads/main" % ("f" * 40)]
        for config, pushed, dest, arg, want in (
                (pin(url), name, url, argv, None),
                (pin(url, "pushinsteadof"), name, url, argv, None),
                (pin(url, "insteadof"), name, url, argv, None),
                (pin(other), name, url, argv, "a pushInsteadOf rewrite"),
                (pin(url) + pin(other, "pushinsteadof"), name, url, argv,
                 "a pushInsteadOf rewrite"),
                (pin(url) + [("url.%s.pushinsteadof" % other,
                              "helm-autoland-push/")], name, url, argv,
                 "a pushInsteadOf rewrite"),
                (pin(url) + [("remote.%s.url" % name, url)], name, url, argv,
                 "a pushInsteadOf rewrite"),
                (pin(url) + [("remote.%s.pushurl" % name, other)], name, url,
                 argv, "a pushurl that differs from url"),
                (pin(url) + [("url.%s.insteadof" % other, "file:///srv/")],
                 name, url, argv, "an insteadOf rewrite of the push URL"),
                ([("url.%s.pushinsteadof" % url, url)], url, url,
                 ["git", "push", url, "main"], "a pushInsteadOf rewrite"),
                (pin(url) + [("remote.%s.url" % url, other)], name, url, argv,
                 "a remote is configured under the push URL's own name"),
                (pin(url) + [("url.%s.insteadof" % other,
                              hostpath_guard.QUERY_ALIAS + "0" * 32)], name,
                 url, argv, "a rewrite rule on the guard's own query names"),
                (pin(url) + [("url.%s.insteadof" % other,
                              "helm-hostpath-")], name, url, argv, None),
                (pin(url), name, url, None, "push argv unreadable")):
            with self.subTest(config=config, name=pushed, argv=arg):
                self.assertEqual(hostpath_guard._unproven(
                    config, pushed, dest, arg), want)

    def pinned_push(self, dest):
        """-> the pre-push outcome of auto-land's push of `self.head` to
        `dest`, through the one-time name the environment `autoland._pinned`
        builds maps to it, behind the release marker (the shared-history rung
        is not the rung under test here)."""
        from helm import autoland
        name, env = autoland._pinned(dict(os.environ), dest)
        return self.git(self.root, "push", name, "main:refs/heads/main",
                        env=dict(env, **self.through_the_release_door()))

    def test_a_remote_named_as_the_pinned_url_cannot_answer_for_it(self):  # noqa: VACUOUS_ASSERTION — the empty C is read beside the named refusal, and the control's narrowed pass is pinned on the same pinned push
        """HELM-CODEX'S DOOR READ OF THE PINNED PUSH (finding 1). Auto-land's
        push writes exactly the URL git passes as $2, here C, an empty
        repository; a remote section NAMED as C's URL string, pointing at A,
        answered `git ls-remote C` with A's refs, so the scan was narrowed by
        what A holds and C received A's old host-path blob unread. MEASURED
        on git 2.53 (RED before the query was pinned: the push exited 0 and
        C held the head). The push is refused as not proven and C receives
        nothing. The
        control: C a clone of A, with no such remote, is proven by the pin
        and narrowed by C's own advertisement. The URLs are file:// URLs:
        git ignores a remote section whose name begins with '/' (MEASURED,
        it warns "config remote shorthand cannot begin with '/'"), so a
        plain path cannot be named as a remote."""
        c = self.repo("c.git")
        self.config("remote.file://%s.url" % c, self.remote)
        r = self.pinned_push("file://" + c)
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("full scan: a URL remote: push destination not proven "
                      "to be the queried one (a remote is configured under "
                      "the push URL's own name)", r.stderr)
        self.assertIn("vendor/upstream.md: /home/test-user/", r.stderr)
        self.assertEqual(self.refs(c), "", "C must receive nothing")
        self.git(self.root, "config", "--unset", "remote.file://%s.url" % c)
        clone = self.repo("clone.git", clone=True)
        r = self.pinned_push("file://" + clone)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("1 blob(s) scanned, the ones a URL remote does not "
                      "already have — no host-path matches", r.stderr)
        self.assertEqual(self.git(clone, "rev-parse", "main").stdout.strip(),
                         self.head)

    def test_the_advertisement_is_read_at_exactly_the_url(self):  # noqa: VACUOUS_ASSERTION — the seed absent from the bare read is that plant's must-hit, and the pinned read of the same URL is pinned EQUAL to the non-empty [seed]
        """The one reader queries through a pin (`_pinned`), never the URL's
        own spelling, so no remote named as the URL and no rewrite whose
        value prefixes it can answer for it. MEASURED on git 2.53 (RED
        before the query was pinned): the remote named as C read A's main,
        and the rule on C's directory read C/c.git, which is no
        repository. C is a file:// URL: git ignores a remote section whose
        name begins with '/'."""
        path = self.repo("c.git")
        r = self.git(self.root, "push", "-q", "--no-verify", path,
                     "%s:refs/heads/main" % self.seed)
        self.assertEqual(r.returncode, 0, r.stderr)
        c = "file://" + path
        want = ([self.seed], None)
        self.assertEqual(hostpath_guard._advertisement(self.root, c), want,
                         "control")
        for key, value in (("remote.%s.url" % c, self.remote),
                           ("url.%s.insteadOf" % c,
                            "file://" + os.path.dirname(path))):
            with self.subTest(key=key.split(".")[0]):
                self.config(key, value)
                bare = self.git(self.root, "ls-remote", "--heads", c)
                self.assertNotIn(self.seed, bare.stdout,
                                 "must-hit: the plant steers a bare read")
                try:
                    self.assertEqual(
                        hostpath_guard._advertisement(self.root, c), want)
                finally:
                    self.git(self.root, "config", "--unset", key)

    def test_the_query_pin_is_appended_to_the_config_it_inherits(self):
        """The pin is one more command-line pair after those the caller's
        environment carries (auto-land's own push pin among them), so the
        query sees what the push sees. A count git would read otherwise
        (' 1' and '+1' read as 1) is never appended to: no pin, no query."""
        url = "file:///srv/a.git"
        name, env = hostpath_guard._pinned(url, {
            "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "a.b",
            "GIT_CONFIG_VALUE_0": "c", "GIT_CONFIG_KEY_1": "d.e",
            "GIT_CONFIG_VALUE_1": "f", "KEEP": "1"})
        self.assertTrue(name.startswith(hostpath_guard.QUERY_ALIAS), name)
        self.assertEqual(len(name), len(hostpath_guard.QUERY_ALIAS) + 32)
        self.assertEqual(env, {
            "GIT_CONFIG_COUNT": "3", "GIT_CONFIG_KEY_0": "a.b",
            "GIT_CONFIG_VALUE_0": "c", "GIT_CONFIG_KEY_1": "d.e",
            "GIT_CONFIG_VALUE_1": "f", "KEEP": "1",
            "GIT_CONFIG_KEY_2": "url.%s.insteadOf" % url,
            "GIT_CONFIG_VALUE_2": name})
        self.assertNotEqual(hostpath_guard._pinned(url, {})[0], name,
                            "a fresh name per query")
        for count in ("", "0"):
            with self.subTest(count=count):
                _name, env = hostpath_guard._pinned(
                    url, {"GIT_CONFIG_COUNT": count})
                self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        why = "the command-line config count cannot be read"
        for count in (" 1", "+1", "x"):
            with self.subTest(count=count):
                self.assertEqual(hostpath_guard._pinned(
                    url, {"GIT_CONFIG_COUNT": count}), (None, why))
        # and neither query runs without its pin
        with mock.patch.object(hostpath_guard, "_git") as git, \
                mock.patch.object(hostpath_guard, "_proof_failure",
                                  return_value=None), \
                mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "+1"}):
            self.assertEqual(hostpath_guard._advertisement(self.root, url),
                             (None, why))
            self.assertEqual(hostpath_guard._destination(
                self.root, "origin", url, []),
                (hostpath_guard.UNREADABLE, None, None, [], why))
        git.assert_not_called()

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


# A stand-in `gh` for a real push through the hook: it answers `repo view
# github.com/<slug> --json isPrivate` from a JSON map {slug: isPrivate} named
# by HP_GH_SLUGS, exits 1 for a slug the map lacks, and appends each call's
# arguments to <map>.calls.
_GH_BY_SLUG = r"""
import json, os, sys
plan = os.environ["HP_GH_SLUGS"]
with open(plan + ".calls", "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
with open(plan) as f:
    answers = json.load(f)
slug = sys.argv[3][len("github.com/"):] if len(sys.argv) > 3 else ""
if slug not in answers:
    sys.exit(1)
print(json.dumps({"isPrivate": answers[slug]}))
"""


class AlreadyPublicCommitsTest(_PushSandbox):
    """task/3395: a commit a PUBLIC remote already carries leaks nothing.

    MEASURED on the dregg fork: a branch based on the current upstream main
    could not be pushed to the public fork, because upstream's own public
    commits carry host paths, and the fork does not advertise them yet. The
    scan reads again, after a hit, without the tips another remote
    ADVERTISES NOW, when it holds a tracking ref here, this checkout credits
    it (its URL is set in the checkout's own config, and it has one), gh
    reads it PUBLIC, and nothing but that repository can answer its URL. The
    tracking ref itself vouches for nothing.

    The world is the dregg shape: `fork` and `upstream` are GitHub URLs, and
    GIT_SSH_COMMAND runs every connection against a bare repository under
    the sandbox, so nothing reaches a network. The `gh` on PATH answers from
    a map (`_GH_BY_SLUG`). Both hold the seed. Upstream's commit U adds a host
    path and is fetched; the lane is one clean commit on U. Every push runs
    through the installed hook, which hands the scanner git's own argv and
    ref lines."""

    FORK, UPSTREAM = "example-owner/fork", "example-owner/upstream"
    HIT = "vendor/upstream.md: /home/test-user/"

    def setUp(self):
        super().setUp()
        self.hosted = os.path.join(self.tmp, "hosted")
        bin_dir = os.path.join(self.tmp, "bin")
        ssh = os.path.join(bin_dir, "fake-ssh")
        with open(ssh, "w") as f:
            f.write('#!/bin/sh\ncd %s || exit 1\nexec sh -c "$2"\n'
                    % shlex.quote(self.hosted))
        os.chmod(ssh, 0o755)
        with open(os.path.join(bin_dir, "gh"), "w") as f:
            f.write("#!%s\n%s" % (sys.executable, _GH_BY_SLUG))
        os.chmod(os.path.join(bin_dir, "gh"), 0o755)
        self.slugs = os.path.join(self.tmp, "gh-slugs.json")
        self.answer(upstream=False)
        # set INSIDE the sandbox's patch, so stopping it removes them
        for key, value in (("GIT_SSH_COMMAND", ssh),
                           ("GIT_SSH_VARIANT", "simple"),
                           ("HP_GH_SLUGS", self.slugs)):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        for slug in (self.FORK, self.UPSTREAM):
            r = self.git(self.tmp, "init", "-q", "--bare", "-b", "main",
                         self.bare(slug))
            self.assertEqual(r.returncode, 0, r.stderr)
            name = slug.rsplit("/", 1)[1]
            for args in (("remote", "add", name, self.url(slug)),
                         ("push", "-q", "--no-verify", name, "main")):
                r = self.git(self.root, *args)
                self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.git(self.root, "checkout", "-q", "-b",
                                  "vendor").returncode, 0)
        self.write("vendor/upstream.md", HOST_PATH_BLOB)
        self.upstream = self.commit("upstream history with a host path")
        for args in (("push", "-q", "--no-verify", "upstream", "vendor:main"),
                     ("fetch", "-q", "upstream"),
                     ("checkout", "-q", "-b", "lane")):
            r = self.git(self.root, *args)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.git(self.root, "rev-parse",
                                  "refs/remotes/upstream/main").stdout.strip(),
                         self.upstream)
        self.write("docs/clean.md", "nothing host-shaped here\n")
        self.lane = self.commit("our clean commit on the upstream history")

    def bare(self, slug):
        return os.path.join(self.hosted, slug + ".git")

    @staticmethod
    def url(slug):
        return "git@github.com:%s.git" % slug

    def answer(self, upstream, also=()):
        """gh reads the fork and each slug in `also` PUBLIC, and upstream as
        given: False is public, True private, None no answer (gh exits
        1)."""
        plan = dict.fromkeys((self.FORK,) + tuple(also), False)
        if upstream is not None:
            plan[self.UPSTREAM] = upstream
        with open(self.slugs, "w") as f:
            json.dump(plan, f)

    def asked(self):
        """-> the gh calls, one line of arguments each."""
        calls = self.slugs + ".calls"
        if not os.path.exists(calls):
            return []
        with open(calls) as f:
            return f.read().splitlines()

    @staticmethod
    def view(slug):
        return "repo view github.com/%s --json isPrivate" % slug

    def to_fork(self, refspec="lane", env=None):
        return self.git(self.root, "push", "fork", refspec, env=env)

    def fork_ref(self, ref="refs/heads/lane"):
        return self.git(self.bare(self.FORK), "rev-parse", "--verify", "-q",
                        ref).stdout.strip()

    def drop_tracking(self, remote):
        """Delete every remote-tracking ref of `remote`, a symbolic HEAD
        (which a fetch may write) included."""
        refs = self.git(self.root, "for-each-ref", "--format=%(refname)",
                        "refs/remotes/%s/" % remote).stdout.split()
        self.assertTrue(refs, "must-hit: %s has tracking refs" % remote)
        for ref in refs:
            r = self.git(self.root, "update-ref", "--no-deref", "-d", ref)
            self.assertEqual(r.returncode, 0, r.stderr)

    def assert_refused_on_upstream(self, r):
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("[helm hostpath] REFUSED: 1 host-path match", r.stderr)
        self.assertIn(self.HIT, r.stderr)
        self.assertEqual(self.fork_ref(), "", "the fork must receive nothing")

    def test_a_lane_on_public_upstream_history_goes_to_the_fork(self):  # noqa: VACUOUS_ASSERTION — the fork's lane is pinned to the non-empty lane sha beside the exact gh calls
        """THE MEASURED PUSH: the host path is upstream's, upstream is
        PUBLIC and advertises U now, and the lane adds one clean commit. gh
        is asked about the fork once, for the destination, and about
        upstream once, and the note names the tip upstream advertised."""
        r = self.to_fork()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.fork_ref(), self.lane)
        self.assertIn("[helm hostpath] already public: 'upstream' advertises "
                      "%s now and gh reads it PUBLIC, so what that reaches "
                      "is not read again" % self.upstream[:12], r.stderr)
        self.assertIn("1 blob(s) scanned, the ones 'fork' does not already "
                      "have and no PUBLIC remote advertises", r.stderr)
        self.assertNotIn(self.HIT, r.stderr)
        self.assertEqual(self.asked(), [self.view(self.FORK),
                                        self.view(self.UPSTREAM)])

    def test_the_same_commit_behind_only_a_private_remote_is_refused(self):  # noqa: VACUOUS_ASSERTION — the empty fork ref is read beside the pinned refusal and the must-hit gh call
        """Control: upstream reads PRIVATE, so nothing says U is public."""
        self.answer(upstream=True)
        r = self.to_fork()
        self.assert_refused_on_upstream(r)
        self.assertIn(self.view(self.UPSTREAM), self.asked())

    def test_the_same_commit_behind_no_remote_is_refused(self):  # noqa: VACUOUS_ASSERTION — the empty fork ref is read beside the pinned refusal
        """Control: upstream is PUBLIC, but no remote-tracking ref reaches
        U: nothing was fetched that holds it."""
        self.drop_tracking("upstream")
        self.assert_refused_on_upstream(self.to_fork())

    def test_our_own_host_path_on_a_public_parent_is_refused(self):  # noqa: VACUOUS_ASSERTION — the absent upstream hit is read beside the pinned hit on our own file
        """Control: a host path the lane adds is new, whatever its parent.
        Only it is named: upstream's is already public."""
        self.write("src/new.py", 'P = "/Users/dev/Library/app"\n')
        self.commit("our own host path")
        r = self.to_fork()
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("[helm hostpath] REFUSED: 1 host-path match", r.stderr)
        self.assertIn("src/new.py: /Users/dev/", r.stderr)
        self.assertNotIn(self.HIT, r.stderr)
        self.assertEqual(self.fork_ref(), "")

    def test_a_visibility_gh_cannot_read_counts_as_not_public(self):  # noqa: VACUOUS_ASSERTION — the empty fork ref is read beside the pinned refusal and both must-hit gh calls
        """Control: gh has no answer for upstream (it exits 1, twice). The
        scan asks gh at push time; there is no cached answer to go stale."""
        self.answer(upstream=None)
        r = self.to_fork()
        self.assert_refused_on_upstream(r)
        self.assertIn("not counted as public: 'upstream': visibility UNKNOWN "
                      "(gh repo view exited 1, twice)", r.stderr)
        self.assertEqual(self.asked().count(self.view(self.UPSTREAM)), 2)

    def test_a_url_set_outside_the_checkout_is_not_credited(self):  # noqa: VACUOUS_ASSERTION — the empty fork ref is read beside the pinned refusal and the absent gh call
        """Control: upstream's URL comes from the global config, which the
        checkout does not own, so its refs vouch for nothing and gh is not
        asked about it (`credited_remotes`)."""
        glob = os.path.join(self.tmp, "global.gitconfig")
        with open(glob, "w") as f:
            f.write('[remote "upstream"]\n\turl = %s\n'
                    % self.url(self.UPSTREAM))
        r = self.git(self.root, "config", "--unset", "remote.upstream.url")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.to_fork(env={"GIT_CONFIG_GLOBAL": glob})
        self.assert_refused_on_upstream(r)
        self.assertIn("not counted as public: 'upstream': a URL for it is "
                      "set in the global config, outside this checkout's own",
                      r.stderr)
        self.assertNotIn(self.view(self.UPSTREAM), self.asked())

    def test_a_remote_that_is_not_on_github_is_not_asked(self):  # noqa: VACUOUS_ASSERTION — the empty fork ref is read beside the pinned refusal and the absent gh call
        """Control: upstream's URL is a path. gh cannot say anything about
        it, so its refs vouch for nothing."""
        r = self.git(self.root, "remote", "set-url", "upstream",
                     self.bare(self.UPSTREAM))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_refused_on_upstream(self.to_fork())
        self.assertNotIn(self.view(self.UPSTREAM), self.asked())

    def test_the_destinations_own_tracking_refs_vouch_for_nothing(self):  # noqa: VACUOUS_ASSERTION — the empty fork ref is read beside the pinned refusal and the kept non-empty tracking ref
        """Control, the measured set-url class: the fork's own tracking ref
        holds U (fetched from the URL it had before), the fork is PUBLIC,
        and it does not advertise U. Only its advertisement says what the
        destination holds; its tracking refs are a record of some fetch.
        The same holds for a second remote that names the fork's
        repository, in any case: it is the destination by another name, and
        gh would read its spelling PUBLIC."""
        self.drop_tracking("upstream")
        for remote in ("fork", "mirror"):
            with self.subTest(remote=remote):
                if remote == "mirror":
                    self.drop_tracking("fork")
                    self.answer(upstream=False, also=(self.FORK.upper(),))
                    r = self.git(self.root, "remote", "add", "mirror",
                                 self.url(self.FORK.upper()))
                    self.assertEqual(r.returncode, 0, r.stderr)
                r = self.git(self.root, "update-ref",
                             "refs/remotes/%s/old" % remote, self.upstream)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assert_refused_on_upstream(self.to_fork())

    def test_a_replace_ref_cannot_hide_a_host_path_from_the_scan(self):  # noqa: VACUOUS_ASSERTION — the unchanged origin main is read beside the pinned refusal
        """MEASURED on git 2.53: `git replace <leak> <stand-in>` made the
        scan's walk read the stand-in's tree while `git push` sent the leak's
        own objects, because pack-objects never honours a replacement. The
        scan reads the objects the ids name."""
        self.assertEqual(self.git(self.root, "checkout", "-q",
                                  "main").returncode, 0)
        self.write("src/leak.py", 'P = "/Users/dev/Library/app"\n')
        leak = self.commit("a host path")
        r = self.git(self.root, "commit-tree", "HEAD~1^{tree}", "-p",
                     "HEAD~1", "-m", "a stand-in without it")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.git(self.root, "replace", leak, r.stdout.strip())
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.push()
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("src/leak.py: /Users/dev/", r.stderr)
        self.assertEqual(self.remote_sha(), self.seed)

    def write_grafts(self, line):
        rel = self.git(self.root, "rev-parse", "--git-path",
                       "info/grafts").stdout.rstrip("\n")
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(line + "\n")

    def test_a_grafted_parent_the_push_sends_is_still_read(self):  # noqa: VACUOUS_ASSERTION — the unchanged origin main is read beside the pinned hit on the grafted file
        """Control for the arm above: pack-objects HONOURS a grafts file.
        MEASURED (git 2.53): a graft that adds a parent sent that parent's
        objects, and a walk with grafts off never read them. So the walk
        honours it too: a side commit no ref keeps, grafted on as a second
        parent, is read."""
        self.assertEqual(self.git(self.root, "checkout", "-q",
                                  "main").returncode, 0)
        self.write("src/grafted.py", 'P = "/Users/dev/Library/app"\n')
        side = self.commit("a side commit no ref will keep")
        r = self.git(self.root, "reset", "-q", "--hard", self.seed)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.write("docs/clean.md", "nothing host-shaped here\n")
        head = self.commit("clean, with the side commit grafted on")
        self.write_grafts("%s %s %s" % (head, self.seed, side))
        r = self.push()
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("src/grafted.py: /Users/dev/", r.stderr)
        self.assertEqual(self.remote_sha(), self.seed)

    def test_a_grafts_file_counts_nothing_as_public(self):  # noqa: VACUOUS_ASSERTION — the empty fork ref is read beside the pinned refusal and its reason
        """Control: a graft can make a private commit read as the parent of
        a public one, so while a grafts file is in effect no remote-tracking
        ref vouches for anything. This one only restates the lane's real
        parent, and the push is still refused."""
        self.write_grafts("%s %s" % (self.lane, self.upstream))
        r = self.to_fork()
        self.assert_refused_on_upstream(r)
        self.assertIn("no commit is counted as already public: a grafts file "
                      "rewrites which commits are parents here", r.stderr)

    def scan(self, fail=None):
        """-> (hits, notes, reached) for the lane pushed to the fork, called
        directly under the push's own proof. `fail` is (the leading git
        arguments, which call of them[, what that call gives instead]): an
        (rc, stdout, stderr) triple or an exception to raise, and a failed
        read, exit 128 with no output, when left out."""
        reached, calls, real = [], {}, hostpath_guard._git

        def git(root, *args, **kw):
            if fail and args[:len(fail[0])] == fail[0]:
                calls[fail[0]] = calls.get(fail[0], 0) + 1
                if calls[fail[0]] == fail[1]:
                    reached.append(args[0])
                    given = fail[2] if len(fail) > 2 else (128, "", "")
                    if isinstance(given, BaseException):
                        raise given
                    rc, out, err = given
                    return rc, (out.encode() if kw.get("binary") else out), err
            return real(root, *args, **kw)

        with proven_push(), mock.patch.object(hostpath_guard, "_git", git):
            hits, notes = hostpath_guard.scan_push(
                self.root, _ZERO, self.lane, "fork",
                remote_url=self.url(self.FORK))
        return hits, notes, reached

    def test_a_failed_git_read_counts_nothing_as_public(self):  # noqa: VACUOUS_ASSERTION — each failed read is read against the empty hits of the unfailed control and a must-hit reach
        """Control: a read of the public evidence that fails keeps every
        hit, and so does the second walk failing: rev-list then reads
        everything reachable from the pushed sha."""
        hits, _notes, _reached = self.scan()
        self.assertEqual(hits, [], "control: nothing fails, upstream is "
                                   "public, and the lane is clean")
        for fail in ((("for-each-ref",), 1),
                     (("config", "-z", "--show-scope"), 1),
                     (("rev-list", "--objects", "--stdin"), 2)):
            with self.subTest(fail=fail[0]):
                hits, notes, reached = self.scan(fail)
                self.assertEqual(reached, [fail[0][0]], "must-hit")
                self.assertEqual(hits, [("vendor/upstream.md",
                                         "/home/test-user/")])

    # ---- task/3395 P1 (helm-codex): a tracking ref is not evidence ----
    # The private repository `example-owner/private` holds X, a commit on the
    # seed that adds its own host path; no public repository holds X. Each
    # arm below moves the lane onto X, adds one clean commit, and puts X
    # where the scan once took a remote-tracking ref of a PUBLIC upstream as
    # proof. Only what upstream ADVERTISES NOW may count.

    PRIVATE = "example-owner/private"
    SECRET_HIT = "secret/notes.md: /home/private-user/"

    def private_lane(self):
        """-> X, held by the private repository alone; the lane is now one
        clean commit on X."""
        r = self.git(self.tmp, "init", "-q", "--bare", "-b", "main",
                     self.bare(self.PRIVATE))
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.git(self.root, "checkout", "-q", "-B", "secret", "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.write("secret/notes.md", 'KEY = "/home/private-user/.ssh/id"\n')
        x = self.commit("a private host path")
        r = self.git(self.root, "push", "-q", "--no-verify",
                     self.url(self.PRIVATE), "secret:main")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.git(self.root, "checkout", "-q", "-B", "lane", x)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.write("docs/clean.md", "nothing host-shaped here\n")
        self.lane = self.commit("our clean commit on the private history")
        return x

    def fresh_fork(self):
        """Delete the fork's lane, so a subtest after one that leaked is
        judged on its own push."""
        self.git(self.bare(self.FORK), "update-ref", "-d", "refs/heads/lane")
        self.assertEqual(self.fork_ref(), "")

    def tracking(self, ref="refs/remotes/upstream/main"):
        return self.git(self.root, "rev-parse", "--verify", "-q",
                        ref).stdout.strip()

    def assert_refused_on_private(self, r):
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("[helm hostpath] REFUSED: 1 host-path match", r.stderr)
        self.assertIn(self.SECRET_HIT, r.stderr)
        self.assertEqual(self.fork_ref(), "", "the fork must receive nothing")

    def test_a_url_changed_after_a_private_fetch_vouches_for_nothing(self):  # noqa: VACUOUS_ASSERTION — the empty fork ref is read beside the pinned refusal on the private file and the must-hit tracking ref
        """RED at 0863a03ab50 (helm-codex P1, worse than main): 'upstream'
        fetched the PRIVATE repository, then its URL was set to the PUBLIC
        upstream, which never held X. The tracking ref still records the
        private fetch; gh reads the URL PUBLIC; upstream advertises U, not
        X. The first publication of X to the public fork must be refused."""
        x = self.private_lane()
        for args in (("remote", "set-url", "upstream", self.url(self.PRIVATE)),
                     ("fetch", "-q", "upstream"),
                     ("remote", "set-url", "upstream",
                      self.url(self.UPSTREAM))):
            r = self.git(self.root, *args)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.tracking(), x, "must-hit: the stale ref is X")
        self.assert_refused_on_private(self.to_fork())
        self.assertIn(self.view(self.UPSTREAM), self.asked())

    def test_a_private_commit_written_into_a_public_namespace_vouches_for_nothing(self):  # noqa: VACUOUS_ASSERTION — the empty fork ref is read beside the pinned refusal and the must-hit ref in upstream's namespace
        """RED at 0863a03ab50: a fetch refspec that writes the private
        repository's branch under refs/remotes/upstream/, and a hand
        update-ref there, each put X in upstream's namespace. Upstream never
        advertised X, so neither vouches for it."""
        x = self.private_lane()
        stolen = "refs/remotes/upstream/stolen"
        writes = (("refspec", ("fetch", "-q", self.url(self.PRIVATE),
                               "+refs/heads/main:" + stolen)),
                  ("update-ref", ("update-ref", stolen, x)))
        for how, args in writes:
            with self.subTest(how=how):
                self.fresh_fork()
                r = self.git(self.root, *args)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.tracking(stolen), x, "must-hit")
                self.assert_refused_on_private(self.to_fork())
                r = self.git(self.root, "update-ref", "-d", stolen)
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_an_advertisement_that_cannot_be_read_counts_nothing(self):  # noqa: VACUOUS_ASSERTION — the absent sentinel is read against the pinned reason and hit in the same scan, beside a must-hit reach
        """Control (RED at 0863a03ab50, which reads no advertisement for
        upstream): upstream's ls-remote, the second in the scan (the fork's
        is the first), exits non-zero, times out, or prints a line that is
        not a ref. Each counts nothing, the hit stands, and no byte of what
        it printed reaches a note."""
        timeout = subprocess.TimeoutExpired(("git", "ls-remote", SENTINEL),
                                            30)
        cases = (("exits", (128, SENTINEL + "\n", SENTINEL),
                  "ls-remote exited 128"),
                 ("times out", timeout, "ls-remote timed out"),
                 ("prints no ref", (0, SENTINEL + "\n", SENTINEL),
                  "ls-remote returned no parseable refs"))
        for how, given, said in cases:
            with self.subTest(how=how):
                hits, notes, reached = self.scan(
                    (("ls-remote",), 2, given))
                self.assertEqual(reached, ["ls-remote"], "must-hit")
                self.assertEqual(hits, [("vendor/upstream.md",
                                         "/home/test-user/")])
                self.assertIn(("note", "not counted as public: 'upstream': "
                               "%s" % said), notes)
                self.assertNotIn(SENTINEL, repr(notes))

    def test_an_advertisement_without_the_commit_counts_nothing(self):  # noqa: VACUOUS_ASSERTION — the empty fork ref is read beside the pinned refusal and its reason
        """Control (RED at 0863a03ab50): the tracking ref still reaches U,
        but upstream does not advertise U now. It advertises a commit this
        repository does not have, and then nothing at all."""
        bare = self.bare(self.UPSTREAM)
        r = self.git(bare, "-c", "user.name=t", "-c", "user.email=t@example.com",
                     "commit-tree", "main^{tree}", "-m", "never fetched here")
        self.assertEqual(r.returncode, 0, r.stderr)
        cases = (("a commit not here", ("update-ref", "refs/heads/main",
                                        r.stdout.strip()),
                  "none of the 1 advertised object(s) is in this repo"),
                 ("nothing", ("update-ref", "-d", "refs/heads/main"),
                  "it advertises no branch or tag"))
        for how, args, said in cases:
            with self.subTest(advertises=how):
                self.fresh_fork()
                r = self.git(bare, *args)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.tracking(), self.upstream, "must-hit")
                r = self.to_fork()
                self.assert_refused_on_upstream(r)
                self.assertIn("not counted as public: 'upstream': %s" % said,
                              r.stderr)

    def test_a_url_another_repository_can_answer_counts_nothing(self):  # noqa: VACUOUS_ASSERTION — the empty fork ref is read beside the pinned refusal and the must-hit private advertisement
        """Control (RED at 0863a03ab50 for each shape): the advertisement
        counts only when the repository gh was asked about is the one that
        answers ls-remote on the same URL. An insteadOf rewrite, a remote
        configured under the URL's own name, and a path that climbs out of
        the repository each make `git ls-remote <upstream's URL>` here
        answer with the PRIVATE repository, which does advertise X."""
        x = self.private_lane()
        plain, climbing = (self.url(self.UPSTREAM),
                           "git@github.com:%s.git/../private.git"
                           % self.UPSTREAM)
        # the slug the climbing URL reads as, so gh would call it PUBLIC
        self.answer(upstream=False, also=(self.UPSTREAM + ".git",))
        shapes = (
            ("insteadOf", plain,
             ("config", "url.%s.insteadOf" % self.url(self.PRIVATE), plain),
             ("config", "--unset", "url.%s.insteadOf" % self.url(self.PRIVATE)),
             "an insteadOf rewrite applies to its URL"),
            ("a remote named as the URL", plain,
             ("config", "remote.%s.url" % plain, self.url(self.PRIVATE)),
             ("config", "--remove-section", "remote." + plain),
             "a remote is configured under its URL's own name"),
            ("a climbing path", climbing,
             ("remote", "set-url", "upstream", climbing),
             ("remote", "set-url", "upstream", plain),
             "its URL is not a plain GitHub repository URL"))
        for how, url, steer, undo, said in shapes:
            with self.subTest(how=how):
                self.fresh_fork()
                for args in (steer, ("update-ref",
                                     "refs/remotes/upstream/main", x)):
                    r = self.git(self.root, *args)
                    self.assertEqual(r.returncode, 0, r.stderr)
                r = self.git(self.root, "ls-remote", url)
                self.assertIn(x + "\trefs/heads/main", r.stdout,
                              "must-hit: another repository answers")
                r = self.to_fork()
                self.assert_refused_on_private(r)
                self.assertIn("not counted as public: 'upstream': %s" % said,
                              r.stderr)
                r = self.git(self.root, *undo)
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_an_http_redirect_is_not_followed(self):  # noqa: VACUOUS_ASSERTION — the read value is pinned beside the must-hit count of ls-remote reads
        """Control (RED at 0863a03ab50, which reads no advertisement for
        upstream): an HTTP redirect could let another repository answer
        upstream's URL, so upstream's ls-remote runs where git reads
        http.followRedirects as false, even under a push given
        `-c http.followRedirects=true`."""
        envs, real = [], hostpath_guard._git

        def git(root, *args, **kw):
            if args[:1] == ("ls-remote",):
                envs.append(kw.get("env"))
            return real(root, *args, **kw)

        ambient = {"GIT_CONFIG_PARAMETERS": "'http.followredirects=true'"}
        with proven_push(), mock.patch.object(hostpath_guard, "_git", git), \
                mock.patch.dict(os.environ, ambient):
            hits, _notes = hostpath_guard.scan_push(
                self.root, _ZERO, self.lane, "fork",
                remote_url=self.url(self.FORK))
        self.assertEqual(hits, [])
        self.assertEqual(len(envs), 2, "must-hit: the fork's, then upstream's")
        r = subprocess.run(("git", "config", "--get", "http.followRedirects"),
                           cwd=self.root, capture_output=True, text=True,
                           env=envs[1] or dict(os.environ, **ambient))
        self.assertEqual(r.stdout.strip(), "false", r.stderr)


class _Elsewhere(http.server.BaseHTTPRequestHandler):
    """A local smart-HTTP host that answers EVERY path with the one bare
    repository `served` names, through `git http-backend`: the host a URL
    with a `?` before its `@` really reaches, whatever follows the `?`."""

    served = None

    def _serve(self):
        service = ("git-receive-pack" if "git-receive-pack" in self.path
                   else "git-upload-pack")
        tail, query = (("/info/refs", "service=" + service)
                       if "/info/refs" in self.path else ("/" + service, ""))
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        env = dict(os.environ, GIT_HTTP_EXPORT_ALL="1",
                   GIT_PROJECT_ROOT=os.path.dirname(self.served),
                   PATH_INFO="/" + os.path.basename(self.served) + tail,
                   QUERY_STRING=query, REQUEST_METHOD=self.command,
                   REMOTE_ADDR="127.0.0.1", CONTENT_LENGTH=str(len(body)),
                   CONTENT_TYPE=self.headers.get("Content-Type", ""))
        for header in ("Git-Protocol", "Content-Encoding"):
            if self.headers.get(header):
                env["HTTP_" + header.upper().replace("-", "_")] = \
                    self.headers[header]
        out = subprocess.run(("git", "http-backend"), input=body, env=env,
                             capture_output=True, timeout=60).stdout
        head, _sep, payload = out.partition(b"\r\n\r\n")
        status, headers = 200, []
        for ln in head.decode("latin-1").splitlines():
            key, _colon, value = ln.partition(":")
            if key.lower() == "status":
                status = int(value.split()[0])
            elif key:
                headers.append((key, value.strip()))
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _serve

    def log_message(self, *_args):
        pass


class PushUrlReachingAnotherHostTest(_PushSandbox):
    """task/3413 END TO END: a push URL with a `?` before its `@` reads as
    github.com/example-owner/private, and git sends the push to the host
    before the `?`. MEASURED (git 2.53, libcurl 8.18, through a local logging
    proxy): `https://evil.example?@github.com/owner/private` opened CONNECT
    evil.example:443, and its http form sent GET
    http://evil.example/?@github.com/owner/private/info/refs&service=...

    Here that host is a local smart-HTTP server (`_Elsewhere`) serving
    `elsewhere.git`, which holds the seed, so the shared-history rung, which
    runs first in the same hook, passes the lane. The `gh` on PATH reads the
    slug PRIVATE; nothing reaches a network."""

    SLUG = "example-owner/private"

    def setUp(self):
        super().setUp()
        self.elsewhere = os.path.join(self.tmp, "served", "elsewhere.git")
        for cwd, args in ((self.tmp, ("init", "-q", "--bare", "-b", "main",
                                      self.elsewhere)),
                          (self.elsewhere, ("config", "http.receivepack",
                                            "true"))):
            r = self.git(cwd, *args)
            self.assertEqual(r.returncode, 0, r.stderr)
        handler = type("Served", (_Elsewhere,), {"served": self.elsewhere})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = "http://127.0.0.1:%d?@github.com/%s" % (
            server.server_address[1], self.SLUG)
        bin_dir = os.path.join(self.tmp, "bin")
        with open(os.path.join(bin_dir, "gh"), "w") as f:
            f.write("#!%s\n%s" % (sys.executable, _GH_BY_SLUG))
        os.chmod(os.path.join(bin_dir, "gh"), 0o755)
        self.slugs = os.path.join(self.tmp, "gh-slugs.json")
        with open(self.slugs, "w") as f:
            json.dump({self.SLUG: True}, f)
        # set INSIDE the sandbox's patch, so stopping it removes them; no
        # proxy may stand between git and the local host
        for key, value in (("HP_GH_SLUGS", self.slugs), ("NO_PROXY", "*"),
                           ("no_proxy", "*")):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        r = self.git(self.root, "push", "-q", "--no-verify", self.url, "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.lane_on_elsewhere("refs/heads/main"), self.seed,
                         "must-hit: the URL reaches the local host")

    def lane_on_elsewhere(self, ref="refs/heads/lane"):
        return self.git(self.elsewhere, "rev-parse", "--verify", "-q",
                        ref).stdout.strip()

    def asked(self):
        calls = self.slugs + ".calls"
        if not os.path.exists(calls):
            return []
        with open(calls) as f:
            return f.read().splitlines()

    def test_a_host_before_a_question_mark_is_scanned(self):  # noqa: VACUOUS_ASSERTION — the empty lane on the served host is read beside the pinned refusal and the setUp must-hit that the URL reaches that host
        """RED on abeca0b48ac: gh read github.com/example-owner/private
        PRIVATE, the scan was skipped, and the host path reached
        elsewhere.git. The URL is not a plain GitHub repository URL, so its
        visibility is UNKNOWN, gh is not asked, and the scan refuses it."""
        self.write("src/leak.py", HOST_PATH_BLOB)
        self.commit("a host path")
        r = self.git(self.root, "push", self.url, "main:refs/heads/lane")
        self.assertNotEqual(r.returncode, 0, r.stderr)
        ours = "\n".join(ln for ln in r.stderr.splitlines()
                         if ln.startswith("[helm"))
        self.assertIn("[helm hostpath] REFUSED: 1 host-path match", ours)
        self.assertIn("src/leak.py: /home/test-user/", ours)
        self.assertIn("a URL remote: visibility UNKNOWN (not a plain GitHub "
                      "repository URL), treated as public", ours)
        for piece in ("127.0.0.1", "github.com", "example-owner"):
            self.assertNotIn(piece, ours)
        self.assertEqual(self.asked(), [])
        self.assertEqual(self.lane_on_elsewhere(), "")


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
    # After a hit, what a PUBLIC remote already carries: which remotes hold a
    # remote-tracking ref, whether a grafts file is in effect, then the
    # scope of every remote URL. The sandbox's origin has a tracking ref, so
    # a named push reaches all three. A PUBLIC remote's own advertisement is
    # read through the ls-remote above (`_advertisement`, the one reader).
    ("_git", "for-each-ref", "--format=%(refname)", "refs/remotes/"): "named",
    ("_git", "rev-parse", "--git-path", "info/grafts"): "named",
    ("_git", "config", "-z", "--show-scope", "--get-regexp",
     r"^remote\..*\.url$"): "named",
    ("int",): "named",
    ("json.loads",): "named",
    ("open",): "named",
    ("subprocess.run", "gh"): "named",
    ("subprocess.run", "git"): "named",
    ("sys.stdin.read",): "named",
    # The shared-history rung, in the same file and behind the same doors:
    # "history" is a proven push of a descendant of the destination's main,
    # "history-orphan" of a commit that shares no history with it, and
    # "history-unreadable" a push whose destination ls-remote cannot read, so
    # the rung refuses it. The rung reads no remote-tracking ref: its only
    # for-each-ref is the host-path scan's, above.
    ("_git", "ls-remote", "--symref", "--upload-pack=git-upload-pack"):
        "history",
    ("_git", "merge-base"): "history",
    ("_git", "rev-list", "--max-parents=0", "--stdin"): "history",
    ("_git", "rev-list", "--stdin"): "history-orphan",
}
GIT_KEYS = [k for k in BOUNDARY_FORMS if k[0] == "_git"]
_HISTORY_ARGV = ["--shared-history", "origin", GH_SENTINEL_URL]
FORMS = {"named": ["--pre-push", "origin", GH_SENTINEL_URL],
         "url": ["--pre-push", SENTINEL_URL, SENTINEL_URL],
         "handrun": ["--pre-push"],
         "history": _HISTORY_ARGV,
         "history-orphan": _HISTORY_ARGV,
         "history-unreadable": _HISTORY_ARGV}


def prefix_of(form):
    """The prefix the rung a form drives refuses under."""
    return ("[helm shared-history]" if FORMS[form][0] == "--shared-history"
            else "[helm hostpath]")


class GuardInputsNeverPrintTest(_PushSandbox):
    """No input the guard reads ever reaches its output. The sentinel is
    planted in every external input, each git call's stdout and stderr, gh's,
    pre-push stdin, the remote name and URL, the /proc argv and the
    environment, and every door is driven. Each run records which inputs
    actually carried the sentinel INTO the guard, the must-hit control, so a
    door whose input never arrives cannot pass vacuously. The history holds
    a published host path, and the pushed commit adds another whose blob
    carries the sentinel beside the match. The shared-history rung is driven
    through the same doors: the remote's HEAD names its main, and an orphan
    commit over the same tree shares no history with it."""

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
        r = self.git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.git(self.root, "commit-tree", self.head + "^{tree}", "-m",
                     "an orphan over the same tree")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.orphan = r.stdout.strip()

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
        if key[1] == "merge-base":
            return out + tag + nl
        if key[1] == "for-each-ref" and "tracking" in plant:
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
        dest = destination or (os.path.join(self.tmp, "nowhere.git")
                               if form == "history-unreadable"
                               else self.remote)
        pushed = self.orphan if form == "history-orphan" else self.head

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
                # the URL reaches the query through its pin (`_pinned`):
                # the config key whose value is the name queried
                env = kw.get("env") or {}
                pins = {env.get("GIT_CONFIG_VALUE_" + k[len(
                    "GIT_CONFIG_KEY_"):]): v for k, v in env.items()
                        if k.startswith("GIT_CONFIG_KEY_")}
                if SENTINEL in pins.get(args[-1], ""):
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
                    % (SENTINEL, pushed, SENTINEL, self.published))
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
                url = argv[2] if len(argv) > 2 else None
                try:
                    got = repr(hostpath_guard.judge_push(
                        self.root, name, url, [pushed])
                        if argv[0] == "--shared-history" else
                        hostpath_guard.scan_push(
                            self.root, _ZERO, self.head, name,
                            remote_url=url))
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
                    self.assertIn("%s REFUSED" % prefix_of(form), text)
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
                     "stderr:rev-list", "stdout:rev-list", "blob",
                     "stderr:for-each-ref", "stderr:rev-parse")),
            ("unreadable tracking refs", "named", {"plant": ("tracking",)}, 1,
             "[helm hostpath] no commit is counted as already public: the "
             "remote-tracking refs cannot be read",
             scan + ("url", "gh", "stdout:for-each-ref", "blob")),
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
            ("shared history", "history", {}, 0,
             "[helm shared-history] 'origin': shares history with its "
             "default branch main, and carries no new root commit",
             scan + ("url", "stderr:config", "stdout:config",
                     "stderr:ls-remote", "stderr:cat-file", "stdout:cat-file",
                     "stderr:merge-base", "stdout:merge-base",
                     "stderr:rev-list", "stdout:rev-list")),
            ("disjoint history", "history-orphan", {}, 1,
             "[helm shared-history] REFUSED: this push shares no history "
             "with the default branch main of 'origin'",
             scan + ("url", "stderr:ls-remote", "stderr:cat-file",
                     "stdout:cat-file", "stderr:merge-base",
                     "stdout:merge-base", "stderr:rev-list",
                     "stdout:rev-list")),
            ("unreadable default branch", "history-unreadable", {}, 1,
             "[helm shared-history] REFUSED: the default branch of 'origin' "
             "cannot be read (not reachable), and this push carries 1 new "
             "root commit(s) that 'origin' is not known to hold",
             scan + ("url", "stderr:ls-remote", "stderr:rev-list",
                     "stdout:rev-list")),
            ("unparseable advertisement, history", "history",
             {"plant": ("advert",)}, 1,
             "[helm shared-history] REFUSED: the default branch of 'origin' "
             "cannot be read (ls-remote returned no parseable refs), and "
             "this push carries 1 new root commit(s) that 'origin' is not "
             "known to hold",
             scan + ("url", "stdout:ls-remote", "stderr:rev-list",
                     "stdout:rev-list")),
            ("empty destination, history", "history",
             {"destination": self.empty}, 1,
             "[helm shared-history] REFUSED: 'origin' has no branch yet (a "
             "brand-new repository), and its visibility is UNKNOWN (gh repo "
             "view printed no JSON, twice), treated as public",
             scan + ("url", "gh", "stderr:ls-remote")),
            ("history check failure", "history-orphan",
             {"raise_at": ("_git", "rev-list", "--stdin")}, 2,
             "[helm shared-history] REFUSED: history check failed — internal "
             "error (SentinelBoom)", ("raised", "stdin")),
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
