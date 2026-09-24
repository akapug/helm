#!/usr/bin/env python3
"""proxy-fork-watch: checker boundary, semantic latch, and daily timer."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-proxy-fork-watch-", var="HELM_HOME")

from helm import cli, proxy_fork_watch as watch  # noqa: E402


def row(branch="fork/a", status="CLEAN", branch_tip="aaa", upstream_tip="up1",
        conflicts=(), packages=(), targets=("./...",)):
    return {
        "schema": 1,
        "branch": branch,
        "branch_tip": branch_tip,
        "upstream_tip": upstream_tip,
        "status": status,
        "conflict_files": list(conflicts),
        "failing_packages": list(packages),
        "test_targets": list(targets),
    }


def jsonl(*rows):
    return "".join(json.dumps(r) + "\n" for r in rows)


def report(*rows, commit="check1", exit_code=0):
    rows = list(rows or (row(),))
    return {
        "overall": "CLEAN" if all(r["status"] == "CLEAN" for r in rows) else "DEBT",
        "checker_exit": exit_code,
        "checker_commit": commit,
        "upstream": rows[0]["upstream_tip"],
        "results": rows,
        "error": None,
    }


def error_report(category="missing-clone", detail="one host detail", commit=None):
    return {
        "overall": "ERROR", "checker_exit": None, "checker_commit": commit,
        "upstream": None, "results": [],
        "error": {"category": category, "detail": detail},
    }


class ParserTest(unittest.TestCase):
    def test_exit_zero_and_one_are_parseable_and_sorted(self):
        for rc in (0, 1):
            rows = watch.parse_checker(jsonl(row("z"), row("a")), rc)
            self.assertEqual([r["branch"] for r in rows], ["a", "z"])

    def test_only_exit_zero_and_one_carry_reports(self):
        with self.assertRaises(watch.WatchError) as cm:
            watch.parse_checker(jsonl(row()), 2)
        self.assertEqual(cm.exception.category, "checker-exit")

    def test_malformed_incomplete_blank_and_empty_jsonl_are_errors(self):
        bad = ("", json.dumps(row()), "{nope}\n", jsonl(row()) + "\n")
        for text in bad:
            with self.subTest(text=repr(text)), self.assertRaises(watch.WatchError) as cm:
                watch.parse_checker(text, 0)
            self.assertEqual(cm.exception.category, "malformed-jsonl")

    def test_schema_fields_types_and_status_are_validated(self):
        cases = []
        missing = row(); missing.pop("test_targets"); cases.append(missing)
        schema = row(); schema["schema"] = 2; cases.append(schema)
        status = row(); status["status"] = "BROKEN"; cases.append(status)
        arrays = row(); arrays["conflict_files"] = "x.go"; cases.append(arrays)
        control = row(branch="bad\nbranch"); cases.append(control)
        clean_conflict = row(conflicts=("x.go",)); cases.append(clean_conflict)
        empty_conflict = row(status="CONFLICT"); cases.append(empty_conflict)
        empty_failure = row(status="TEST-FAIL"); cases.append(empty_failure)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(watch.WatchError) as cm:
                watch.parse_checker(jsonl(value), 0)
            self.assertEqual(cm.exception.category, "malformed-jsonl")

    def test_duplicate_branch_and_mixed_upstream_have_stable_categories(self):
        with self.assertRaises(watch.WatchError) as cm:
            watch.parse_checker(jsonl(row("a"), row("a", branch_tip="bbb")), 1)
        self.assertEqual(cm.exception.category, "duplicate-branch")
        with self.assertRaises(watch.WatchError) as cm:
            watch.parse_checker(jsonl(row("a"), row("b", upstream_tip="up2")), 1)
        self.assertEqual(cm.exception.category, "mixed-upstream")


class CheckerBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-pfw-check-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_missing_clone_is_a_stable_error_report(self):
        with mock.patch.dict(os.environ, {"HELM_PROXY_FORK_DIR": os.path.join(self.tmp, "gone")}, clear=False):
            got = watch.check()
        self.assertEqual(got["overall"], "ERROR")
        self.assertEqual(got["error"]["category"], "missing-clone")

    def test_wrong_branch_is_a_stable_error_report(self):
        os.makedirs(os.path.join(self.tmp, "cmd", "helm-upstream-check"))
        done = SimpleNamespace(returncode=0, stdout=(
            "# branch.oid abc123\n# branch.head main\n"), stderr="")
        backend = SimpleNamespace(proc=mock.Mock(return_value=done))
        with mock.patch.dict(os.environ, {"HELM_PROXY_FORK_DIR": self.tmp}, clear=False), \
                mock.patch("helm.proxy_fork_watch.vcs.backend", return_value=backend):
            got = watch.check()
        self.assertEqual(got["error"]["category"], "wrong-branch")

    def test_clone_identity_reads_branch_and_commit_from_one_status_call(self):
        os.makedirs(os.path.join(self.tmp, "cmd", "helm-upstream-check"))
        done = SimpleNamespace(returncode=0, stdout=(
            "# branch.oid abc123\n# branch.head helm/upstream-tracking\n"), stderr="")
        backend = SimpleNamespace(proc=mock.Mock(return_value=done))
        with mock.patch("helm.proxy_fork_watch.vcs.backend", return_value=backend):
            path, commit = watch._inspect_clone(self.tmp)
        self.assertEqual((path, commit), (self.tmp, "abc123"))
        backend.proc.assert_called_once_with(
            self.tmp, "status", "--porcelain=v2", "--branch",
            "--untracked-files=no", timeout=watch.GIT_TIMEOUT_S)

    def test_checker_invocation_is_exact_nonshell_readonly_and_gowork_off(self):
        done = SimpleNamespace(returncode=0, stdout=jsonl(row()), stderr="")
        with mock.patch.object(watch, "_inspect_clone", return_value=(self.tmp, "abc123")), \
                mock.patch("helm.proxy_fork_watch.subprocess.run", return_value=done) as run:
            got = watch.check()
        self.assertEqual(got["overall"], "CLEAN")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["go", "run", "-mod=readonly", "./cmd/helm-upstream-check"])
        self.assertEqual(kwargs["cwd"], self.tmp)
        self.assertEqual(kwargs["env"]["GOWORK"], "off")
        self.assertEqual(kwargs["timeout"], watch.CHECK_TIMEOUT_S)
        self.assertNotIn("shell", kwargs)

    def test_timeout_and_malformed_stdout_become_error_reports(self):
        with mock.patch.object(watch, "_inspect_clone", return_value=(self.tmp, "abc123")), \
                mock.patch("helm.proxy_fork_watch.subprocess.run",
                           side_effect=subprocess.TimeoutExpired(watch.CHECKER, 1)):
            self.assertEqual(watch.check()["error"]["category"], "checker-timeout")
        done = SimpleNamespace(returncode=1, stdout="not-json\n", stderr="diagnostic sha cafe")
        with mock.patch.object(watch, "_inspect_clone", return_value=(self.tmp, "abc123")), \
                mock.patch("helm.proxy_fork_watch.subprocess.run", return_value=done):
            got = watch.check()
        self.assertEqual(got["error"]["category"], "malformed-jsonl")
        self.assertEqual(got["checker_exit"], 1)
        self.assertEqual(got["checker_commit"], "abc123")

    def test_invalid_checker_text_is_a_stable_malformed_report(self):
        bad = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
        with mock.patch.object(watch, "_inspect_clone", return_value=(self.tmp, "abc123")), \
                mock.patch("helm.proxy_fork_watch.subprocess.run", side_effect=bad):
            got = watch.check()
        self.assertEqual(got["error"]["category"], "malformed-jsonl")
        self.assertEqual(got["checker_commit"], "abc123")

    def test_proxy_dir_override_is_strict_with_no_meld_fallback(self):
        with mock.patch.dict(os.environ, {"MELD_PROXY_FORK_DIR": "/legacy"}, clear=False):
            os.environ.pop("HELM_PROXY_FORK_DIR", None)
            self.assertNotEqual(watch.clone_dir(), "/legacy")
        with mock.patch.dict(os.environ, {"HELM_PROXY_FORK_DIR": "/new",
                                          "MELD_PROXY_FORK_DIR": "/legacy"}, clear=False):
            self.assertEqual(watch.clone_dir(), "/new")


class PostTransactionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-pfw-state-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.old = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = self.tmp
        self.addCleanup(self._restore)

    def _restore(self):
        if self.old is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self.old

    def _post(self, reps, post_side_effect=None, force=False):
        if not isinstance(reps, (list, tuple)):
            reps = [reps]
        patcher = mock.patch.object(watch, "check", side_effect=list(reps))
        chatter = mock.patch("helm.chat.post", side_effect=post_side_effect)
        with patcher, chatter as post:
            values = [watch.post_once(force=force) for _ in reps]
        return values, post

    def test_first_clean_posts_then_unchanged_skips(self):
        r = report(row())
        values, post = self._post([r, r])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(values[0][1]["last_run"]["post"]["decision"], "FIRST")
        self.assertEqual(values[1][1]["last_run"]["post"], {
            "decision": "UNCHANGED", "outcome": "SKIPPED", "error": None})

    def test_semantic_change_and_recovery_each_post_once(self):
        clean = report(row())
        debt = report(row(status="CONFLICT", conflicts=("a.go",)), exit_code=1)
        _values, post = self._post([clean, debt, debt, clean, clean])
        self.assertEqual(post.call_count, 3)  # FIRST, debt CHANGED, recovery CHANGED
        reasons = [c.args[0].split()[1] for c in post.call_args_list]
        self.assertEqual(reasons, ["FIRST", "CHANGED", "CHANGED"])

    def test_error_posts_and_recovery_posts_once(self):
        err = error_report("checker-timeout", "40m elapsed")
        clean = report(row())
        _values, post = self._post([err, err, clean, clean])
        self.assertEqual(post.call_count, 2)
        self.assertIn("ERROR[checker-timeout]", post.call_args_list[0].args[0])

    def test_sha_target_and_error_detail_only_changes_do_not_post(self):
        a = report(row(branch_tip="a", upstream_tip="u1", targets=("./a",)), commit="c1")
        b = report(row(branch_tip="b", upstream_tip="u2", targets=("./b",)), commit="c2")
        _values, post = self._post([a, b])
        self.assertEqual(post.call_count, 1)
        os.remove(watch.state_path())
        e1 = error_report("checker-timeout", "elapsed at sha one", "c1")
        e2 = error_report("checker-timeout", "elapsed at sha two", "c2")
        _values, post = self._post([e1, e2])
        self.assertEqual(post.call_count, 1)

    def test_failed_post_does_not_consume_and_retries_next_cadence(self):
        r = report(row(status="TEST-FAIL", packages=("./pkg/x",)), exit_code=1)
        with mock.patch.object(watch, "check", return_value=r), \
                mock.patch("helm.chat.post", side_effect=RuntimeError("chat down")):
            _rep, state = watch.post_once()
        self.assertEqual(state["last_run"]["post"]["outcome"], "FAILED")
        self.assertNotIn("announced_fingerprint", state)
        with mock.patch.object(watch, "check", return_value=r), \
                mock.patch("helm.chat.post") as post:
            _rep, state = watch.post_once()
        self.assertEqual(post.call_count, 1)
        self.assertEqual(state["last_run"]["post"]["decision"], "FIRST")
        self.assertEqual(state["announced_fingerprint"], watch.fingerprint(r))

    def test_failed_forced_post_retries_on_the_next_ordinary_cadence(self):
        r = report(row())
        with mock.patch.object(watch, "check", return_value=r), \
                mock.patch("helm.chat.post"):
            watch.post_once()
        with mock.patch.object(watch, "check", return_value=r), \
                mock.patch("helm.chat.post", side_effect=RuntimeError("chat down")):
            _rep, state = watch.post_once(force=True)
        self.assertEqual(state["pending_decision"], "FORCED")
        with mock.patch.object(watch, "check", return_value=r), \
                mock.patch("helm.chat.post") as post:
            _rep, state = watch.post_once()
        self.assertEqual(post.call_count, 1)
        self.assertIn(" FORCED ", post.call_args.args[0])
        self.assertNotIn("pending_fingerprint", state)

    def test_force_posts_and_is_labeled_forced(self):
        r = report(row())
        _values, post = self._post([r, r], force=True)
        self.assertEqual(post.call_count, 2)
        self.assertTrue(all(c.args[0].startswith("proxy-fork-watch FORCED ")
                            for c in post.call_args_list))

    def test_last_run_updates_every_post_attempt_with_required_fields(self):
        r = report(row(status="CONFLICT", conflicts=("x.go",)), commit="checker-sha", exit_code=1)
        with mock.patch.object(watch, "check", return_value=r), \
                mock.patch("helm.chat.post"), \
                mock.patch("helm.proxy_fork_watch.pk.now_ts",
                           side_effect=["2026-01-01T00:00:00Z", "2026-01-01T00:00:02Z"]):
            _rep, state = watch.post_once()
        last = state["last_run"]
        self.assertEqual((last["started_at"], last["finished_at"]),
                         ("2026-01-01T00:00:00Z", "2026-01-01T00:00:02Z"))
        self.assertEqual(last["overall"], "DEBT")
        self.assertEqual(last["checker"], {
            "exit": 1, "commit": "checker-sha", "upstream": "up1"})
        self.assertEqual(last["results"], r["results"])
        self.assertIsNone(last["error_category"])
        with open(watch.state_path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f), state)
        self.assertFalse(any(n.endswith(".tmp") for n in os.listdir(os.path.dirname(watch.state_path()))))

    def test_fcntl_lock_precedes_checker_post_and_atomic_json_write(self):
        events = []
        r = report(row())
        with mock.patch("helm.proxy_fork_watch.fcntl.flock",
                        side_effect=lambda _fd, mode: events.append(("lock", mode))), \
                mock.patch.object(watch, "check", side_effect=lambda: events.append(("check", None)) or r), \
                mock.patch("helm.chat.post", side_effect=lambda *a, **k: events.append(("post", None))), \
                mock.patch("helm.proxy_fork_watch.pk.read_json", return_value={}), \
                mock.patch("helm.proxy_fork_watch.pk.write_json",
                           side_effect=lambda *a: events.append(("write", None))):
            watch.post_once()
        self.assertEqual([e[0] for e in events], ["lock", "check", "post", "write"])
        self.assertEqual(events[0][1], watch.fcntl.LOCK_EX)

    def test_chat_post_is_exactly_one_physical_line_room_helm_no_mention(self):
        r = report(row(branch="fork/@topic", status="CONFLICT",
                       conflicts=("x/@generated.go",)))
        _values, post = self._post(r)
        self.assertEqual(post.call_count, 1)
        text = post.call_args.args[0]
        self.assertNotIn("\n", text)
        self.assertNotIn("@", text)
        self.assertIn("FIRST", text)
        self.assertIn("state ", text)
        self.assertEqual(post.call_args.kwargs,
                         {"room": "helm", "who": "proxy-fork-watch"})


class TimerTest(unittest.TestCase):
    def test_units_use_durable_installed_cli_and_daily_bounded_idle_service(self):
        spath, service, tpath, timer = watch.timer_units()
        self.assertTrue(spath.endswith("/.config/systemd/user/helm-proxy-fork-watch.service"))
        self.assertTrue(tpath.endswith("/.config/systemd/user/helm-proxy-fork-watch.timer"))
        self.assertIn("Type=oneshot", service)
        self.assertIn("ExecStart=%h/.local/bin/helm proxy-fork-watch --post", service)
        self.assertIn("SuccessExitStatus=1", service)
        self.assertIn("TimeoutStartSec=45m", service)
        self.assertIn("Nice=10", service)
        self.assertIn("IOSchedulingClass=idle", service)
        self.assertIn("OnCalendar=daily", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("RandomizedDelaySec=30m", timer)
        self.assertNotIn("CLIProxyAPI", service + timer)
        self.assertNotIn("worktree", (service + timer).lower())
        self.assertNotIn("cli-proxy-api", (service + timer).lower())

    def test_installer_validates_before_atomic_writes_then_reloads_and_enables(self):
        done = SimpleNamespace(returncode=0, stdout="", stderr="")
        def which(name):
            return "/usr/bin/" + name
        with mock.patch.object(watch, "_inspect_clone", return_value=("/clone", "sha")) as inspect, \
                mock.patch("helm.proxy_fork_watch.shutil.which", side_effect=which), \
                mock.patch("helm.proxy_fork_watch.os.path.isfile", return_value=True), \
                mock.patch("helm.proxy_fork_watch.os.access", return_value=True), \
                mock.patch("helm.proxy_fork_watch.pk.atomic_write") as write, \
                mock.patch("helm.proxy_fork_watch.subprocess.run", return_value=done) as run:
            ok, _detail = watch.install_timer()
        self.assertTrue(ok)
        inspect.assert_called_once_with()
        self.assertEqual(write.call_count, 2)
        self.assertEqual([c.args[0] for c in run.call_args_list], [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now",
             "helm-proxy-fork-watch.timer"],
        ])

    def test_failed_validation_writes_no_units_or_systemd_calls(self):
        with mock.patch.object(watch, "_inspect_clone",
                               side_effect=watch.WatchError("wrong-branch", "main")), \
                mock.patch("helm.proxy_fork_watch.pk.atomic_write") as write, \
                mock.patch("helm.proxy_fork_watch.subprocess.run") as run:
            ok, detail = watch.install_timer()
        self.assertFalse(ok)
        self.assertIn("wrong-branch", detail)
        self.assertFalse(write.called)
        self.assertFalse(run.called)


class CliTest(unittest.TestCase):
    def test_help_and_root_listing_name_the_lazy_verb(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["proxy-fork-watch", "--help"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("proxy-fork-watch", out.getvalue())
        self.assertIn("--install-timer", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--help"]), 0)
        self.assertIn("proxy-fork-watch", out.getvalue())

    def test_default_and_json_are_read_only_no_state_or_post(self):
        tmp = tempfile.mkdtemp(prefix="helm-test-pfw-cli-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = tmp
        self.addCleanup(lambda: os.environ.pop("HELM_HOME", None)
                        if old is None else os.environ.__setitem__("HELM_HOME", old))
        for args in ([], ["--json"]):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(watch, "check", return_value=report(row())), \
                    mock.patch("helm.chat.post") as post, \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = watch.cmd_proxy_fork_watch(args)
            self.assertEqual(rc, 0, err.getvalue())
            self.assertFalse(post.called)
            self.assertFalse(os.path.exists(watch.state_path()))

    def test_unknown_flag_refuses_before_checker(self):
        with mock.patch.object(watch, "check") as check:
            rc = watch.cmd_proxy_fork_watch(["--pst"])
        self.assertEqual(rc, 2)
        self.assertFalse(check.called)


if __name__ == "__main__":
    unittest.main()
