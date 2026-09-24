#!/usr/bin/env python3
"""Disposable-repository tests for scripts/mutation_matrix.py."""

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


SCRIPT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "scripts", "mutation_matrix.py"
))


_DEFAULT_SUBJECT = "VALUE = 1\nUNUSED = 1\n"
_DEFAULT_SUITE = """
    import unittest
    import subject

    class SubjectTest(unittest.TestCase):
        def test_value(self):
            self.assertEqual(subject.VALUE, 1)
"""


class MutationMatrixRepo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-mutation-matrix-")
        self.root = os.path.join(self.tmp, "repo")
        os.makedirs(self.root)
        self.env = os.environ.copy()
        self.env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        self.env["GIT_CONFIG_SYSTEM"] = "/dev/null"
        self.env["PYTHONUNBUFFERED"] = "1"
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "matrix@example.test")
        self.git("config", "user.name", "Matrix Test")
        self.git("config", "commit.gpgsign", "false")
        self.write("subject.py", _DEFAULT_SUBJECT)
        self.write("suite.py", textwrap.dedent(_DEFAULT_SUITE).lstrip())
        self.commit_files("baseline", "subject.py", "suite.py")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sh(self, *args, env=None, timeout=45):
        merged = dict(self.env, **(env or {}))
        return subprocess.run(
            args,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=merged,
        )

    def git(self, *args, check=True):
        proc = self.sh("git", *args)
        if check:
            self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def write(self, relative, body):
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)

    def commit_files(self, name, *paths):
        self.git("add", "--", *paths)
        self.git("commit", "-q", "-m", name)

    def commit_suite(self, body, name="suite fixture"):
        self.write("suite.py", textwrap.dedent(body).lstrip())
        self.commit_files(name, "suite.py")

    def mutation(self, name="value-two", find="VALUE = 1", replace="VALUE = 2",
                 path="subject.py"):
        return {
            "name": name,
            "path": path,
            "find": find,
            "replace": replace,
        }

    def command(self, *args):
        return [sys.executable, "-m", "unittest"] + list(args or ("suite.py",))

    def spec(self, mutations, command=None, timeout_seconds=10):
        data = {
            "command": command or self.command(),
            "timeout_seconds": timeout_seconds,
            "mutations": mutations,
        }
        path = os.path.join(self.root, "matrix.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        return path

    def run_matrix(self, mutations, command=None, env=None, timeout_seconds=10,
                   process_timeout=45):
        path = self.spec(mutations, command=command,
                         timeout_seconds=timeout_seconds)
        return self.sh(sys.executable, SCRIPT, path, env=env,
                       timeout=process_timeout)

    def head(self):
        return self.git("rev-parse", "HEAD").stdout.strip()

    def head_ref(self):
        proc = self.git("symbolic-ref", "-q", "HEAD", check=False)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def index_tag(self, path="subject.py"):
        return self.git("ls-files", "-v", "--", path).stdout[:1]

    def assert_target_clean(self, path="subject.py"):
        for args in (("diff", "--quiet", "--cached", "--", path),
                     ("diff", "--quiet", "--", path)):
            proc = self.git(*args, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.index_tag(path), "H")
        committed = self.git("show", "HEAD:" + path).stdout
        with open(os.path.join(self.root, path), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), committed)

    def assert_matrix_error(self, proc, context, *diagnostics):
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("MATRIX-ERROR " + context, proc.stdout)
        for diagnostic in diagnostics:
            self.assertIn(diagnostic, proc.stdout)
        self.assertNotIn("ALL-KILLED", proc.stdout)

    def test_killed_mutant_restores_target_and_reruns_control(self):
        proc = self.run_matrix([self.mutation()])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("BASELINE GREEN (1 tests)", proc.stdout)
        self.assertIn("KILLED value-two (1 tests)", proc.stdout)
        self.assertIn("CONTROL GREEN (1 tests)", proc.stdout)
        self.assertIn("ALL-KILLED 1/1", proc.stdout)
        self.assertNotIn("MATRIX-ERROR", proc.stdout)
        self.assert_target_clean()

    def test_inert_mutant_survives_without_all_killed_summary(self):
        mutation = self.mutation("unused-two", "UNUSED = 1", "UNUSED = 2")
        proc = self.run_matrix([mutation])
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("SURVIVED unused-two (1 tests)", proc.stdout)
        self.assertIn("SUMMARY 0 KILLED, 1 SURVIVED", proc.stdout)
        self.assertNotIn("ALL-KILLED", proc.stdout)
        self.assertNotIn("MATRIX-ERROR", proc.stdout)
        self.assert_target_clean()

    def test_refuses_staged_target_left_by_a_refused_commit(self):
        hook = os.path.join(self.root, ".git", "hooks", "pre-commit")
        with open(hook, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nexit 1\n")
        os.chmod(hook, 0o755)
        before = self.head()
        self.write("subject.py", "VALUE = 9\nUNUSED = 1\n")
        self.git("add", "subject.py")
        refused = self.git("commit", "-m", "must refuse", check=False)
        self.assertNotEqual(refused.returncode, 0)
        staged_before = self.git("show", ":subject.py").stdout

        proc = self.run_matrix([self.mutation()])

        self.assert_matrix_error(proc, "preflight", "dirty in the index")
        self.assertNotIn("BASELINE GREEN", proc.stdout)
        self.assertEqual(self.head(), before)
        self.assertEqual(self.git("show", ":subject.py").stdout, staged_before)

    def test_index_trap_is_restored_from_captured_head(self):
        self.commit_suite("""
            import subprocess
            import unittest
            import subject

            class SubjectTest(unittest.TestCase):
                def test_value(self):
                    if subject.VALUE == 2:
                        subprocess.run(["git", "add", "--", "subject.py"],
                                       check=True)
                    self.assertEqual(subject.VALUE, 1)
        """, "stage mutant before failure")
        before = self.head()

        proc = self.run_matrix([self.mutation()])

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("KILLED value-two", proc.stdout)
        self.assertIn("ALL-KILLED 1/1", proc.stdout)
        self.assertEqual(self.head(), before)
        self.assert_target_clean()

    def test_noop_git_restore_is_matrix_error_not_killed(self):
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        fake_dir = os.path.join(self.tmp, "fake-bin")
        os.makedirs(fake_dir)
        log = os.path.join(self.tmp, "restore.log")
        fake = os.path.join(fake_dir, "git")
        with open(fake, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/bin/sh\n"
                "if [ \"$1\" = restore ]; then\n"
                "  printf '%%s\\n' \"$*\" >> %s\n"
                "  exit 0\n"
                "fi\n"
                "exec %s \"$@\"\n" % (shlex.quote(log), shlex.quote(real_git))
            )
        os.chmod(fake, 0o755)
        before = self.head()
        env = {"PATH": fake_dir + os.pathsep + self.env.get("PATH", "")}

        proc = self.run_matrix([self.mutation()], env=env)

        self.assert_matrix_error(proc, "value-two", "worktree diff remains")
        self.assertNotIn("KILLED value-two", proc.stdout)
        with open(log, encoding="utf-8") as handle:
            restore = handle.read()
        self.assertIn("--source=" + before, restore)
        self.assertIn("--staged --worktree --", restore)
        self.assert_target_clean()

    def test_commit_movement_inside_disposable_checkout_is_matrix_error(self):
        self.commit_suite("""
            import subprocess
            import unittest
            import subject

            class SubjectTest(unittest.TestCase):
                def test_value(self):
                    if subject.VALUE == 2:
                        subprocess.run(["git", "add", "--", "subject.py"],
                                       check=True)
                        subprocess.run([
                            "git", "-c", "user.email=matrix@example.test",
                            "-c", "user.name=Matrix Test", "commit", "-q",
                            "-m", "mutant moved HEAD"
                        ], check=True)
                    self.assertEqual(subject.VALUE, 1)
        """, "move isolated HEAD from mutant")
        before = self.head()

        proc = self.run_matrix([self.mutation()])

        self.assert_matrix_error(proc, "value-two", "HEAD identity changed")
        self.assertNotIn("KILLED value-two", proc.stdout)
        self.assertEqual(self.head(), before)
        self.assertEqual(self.head_ref(), "refs/heads/main")
        self.assert_target_clean()

    def test_same_commit_caller_detachment_is_detected(self):
        self.commit_suite("""
            import subprocess
            import unittest
            import subject

            CALLER = %r

            class SubjectTest(unittest.TestCase):
                def test_value(self):
                    if subject.VALUE == 2:
                        subprocess.run([
                            "git", "-C", CALLER, "checkout", "--detach", "-q",
                            "HEAD"
                        ], check=True)
                    self.assertEqual(subject.VALUE, 1)
        """ % self.root, "detach caller at same commit")
        before = self.head()
        try:
            proc = self.run_matrix([self.mutation()])
            self.assert_matrix_error(proc, "value-two", "HEAD identity changed")
            self.assertIn("refs/heads/main@" + before, proc.stdout)
            self.assertIsNone(self.head_ref())
        finally:
            if self.head_ref() is None:
                self.git("switch", "-q", "main")
        self.assert_target_clean()

    def test_semantic_index_flags_are_matrix_errors(self):
        for option, expected in (("--assume-unchanged", "h"),
                                 ("--skip-worktree", "S")):
            with self.subTest(option=option):
                self.commit_suite("""
                    import subprocess
                    import unittest
                    import subject

                    OPTION = %r

                    class SubjectTest(unittest.TestCase):
                        def test_value(self):
                            if subject.VALUE == 2:
                                subprocess.run([
                                    "git", "update-index", OPTION, "subject.py"
                                ], check=True)
                            self.assertEqual(subject.VALUE, 1)
                """ % option, "semantic flag " + option)
                proc = self.run_matrix([self.mutation()])
                self.assert_matrix_error(proc, "value-two",
                                         "semantic index flag changed H -> " + expected)
                self.assertNotIn("KILLED value-two", proc.stdout)
                self.assert_target_clean()

    def test_cleanup_restores_every_declared_target(self):
        self.write("other.py", "OTHER = 1\n")
        self.commit_suite("""
            import subprocess
            import unittest
            import other
            import subject

            class SubjectTest(unittest.TestCase):
                def test_values(self):
                    if subject.VALUE == 2:
                        with open("other.py", "w", encoding="utf-8") as handle:
                            handle.write("OTHER = 9\\n")
                        subprocess.run(["git", "add", "--", "other.py"],
                                       check=True)
                    self.assertEqual(subject.VALUE, 1)
                    self.assertEqual(other.OTHER, 1)
        """, "cross-target contamination")
        self.commit_files("add second target", "other.py")
        mutations = [
            self.mutation(),
            self.mutation("other-two", "OTHER = 1", "OTHER = 2", "other.py"),
        ]

        proc = self.run_matrix(mutations)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("KILLED value-two", proc.stdout)
        self.assertIn("KILLED other-two", proc.stdout)
        self.assertIn("ALL-KILLED 2/2", proc.stdout)
        self.assert_target_clean()
        self.assert_target_clean("other.py")

    def test_sigint_cleans_disposable_mutant_before_matrix_error(self):
        marker = os.path.join(self.tmp, "mutant-started")
        self.commit_suite("""
            import time
            import unittest
            import subject

            MARKER = %r

            class SubjectTest(unittest.TestCase):
                def test_value(self):
                    if subject.VALUE == 2:
                        with open(MARKER, "w", encoding="utf-8") as handle:
                            handle.write("started")
                        time.sleep(30)
                    self.assertEqual(subject.VALUE, 1)
        """ % marker, "interrupt mutant")
        spec = self.spec([self.mutation()], timeout_seconds=40)
        interruptible_exec = textwrap.dedent("""
            import os
            import signal
            import sys

            signal.signal(signal.SIGINT, signal.SIG_DFL)
            if hasattr(signal, "pthread_sigmask"):
                signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})
            os.execv(sys.executable, [sys.executable] + sys.argv[1:])
        """).lstrip()
        proc = subprocess.Popen(
            [sys.executable, "-c", interruptible_exec, SCRIPT, spec],
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        try:
            deadline = time.time() + 15
            while not os.path.exists(marker) and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(os.path.exists(marker), "mutant never started")
            proc.send_signal(signal.SIGINT)
            out, err = proc.communicate(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertEqual(proc.returncode, 2, out + err)
        self.assertIn("MATRIX-ERROR value-two", out)
        self.assertIn("test command interrupted", out)
        self.assertNotIn("KILLED value-two", out)
        self.assertNotIn("ALL-KILLED", out)
        self.assert_target_clean()

    def test_zero_and_multiple_anchors_are_preflight_errors(self):
        cases = (
            ("missing", "NOT_PRESENT = 1", "occurs 0 times"),
            ("multiple", " = 1", "occurs 2 times"),
        )
        for name, anchor, diagnostic in cases:
            with self.subTest(name=name):
                proc = self.run_matrix([
                    self.mutation(name, anchor, " = 7")
                ])
                self.assert_matrix_error(proc, "preflight", diagnostic)
                self.assertNotIn("BASELINE GREEN", proc.stdout)
                self.assert_target_clean()

    def test_count_drift_is_matrix_error_and_final_control_stays_green(self):
        self.commit_suite("""
            import unittest
            import subject

            if subject.VALUE == 1:
                class SubjectTest(unittest.TestCase):
                    def test_one(self):
                        self.assertTrue(True)
            else:
                class SubjectTest(unittest.TestCase):
                    def test_one(self):
                        self.assertTrue(True)
                    def test_two(self):
                        self.assertTrue(True)
        """, "count drift fixture")

        proc = self.run_matrix([self.mutation()])

        self.assert_matrix_error(proc, "value-two", "test count changed from 1 to 2")
        self.assertIn("CONTROL GREEN (1 tests)", proc.stdout)
        self.assertNotIn("KILLED value-two", proc.stdout)
        self.assertNotIn("SURVIVED value-two", proc.stdout)
        self.assert_target_clean()

    def test_target_is_clean_before_killed_result_is_emitted(self):
        count_path = os.path.join(self.tmp, "run-count")
        self.commit_suite("""
            import time
            import unittest
            import subject

            COUNT_PATH = %r
            try:
                with open(COUNT_PATH, encoding="utf-8") as handle:
                    count = int(handle.read()) + 1
            except FileNotFoundError:
                count = 1
            with open(COUNT_PATH, "w", encoding="utf-8") as handle:
                handle.write(str(count))
            if count == 3 and subject.VALUE == 1:
                time.sleep(2)

            class SubjectTest(unittest.TestCase):
                def test_value(self):
                    self.assertEqual(subject.VALUE, 1)
        """ % count_path, "pause final control")
        spec = self.spec([self.mutation()])
        proc = subprocess.Popen(
            [sys.executable, SCRIPT, spec],
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        seen = []
        try:
            while True:
                line = proc.stdout.readline()
                self.assertTrue(line, "runner exited before emitting KILLED")
                seen.append(line)
                if line.startswith("KILLED value-two"):
                    break
            self.assertIsNone(proc.poll(), "final control did not keep runner alive")
            self.assert_target_clean()
            out, err = proc.communicate(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        output = "".join(seen) + out
        self.assertEqual(proc.returncode, 0, output + err)
        self.assertIn("CONTROL GREEN (1 tests)", output)
        self.assertIn("ALL-KILLED 1/1", output)

    def test_repo_local_invocation_state_cannot_false_kill_inert_mutant(self):
        self.commit_suite("""
            import unittest
            import subject

            try:
                with open(".matrix-run-count", encoding="utf-8") as handle:
                    count = int(handle.read()) + 1
            except FileNotFoundError:
                count = 1
            with open(".matrix-run-count", "w", encoding="utf-8") as handle:
                handle.write(str(count))

            class SubjectTest(unittest.TestCase):
                def test_not_second_shared_run(self):
                    self.assertNotEqual(count, 2)
                    self.assertEqual(subject.VALUE, 1)
        """, "false kill witness")
        mutation = self.mutation("unused-two", "UNUSED = 1", "UNUSED = 2")

        proc = self.run_matrix([mutation])

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("SURVIVED unused-two", proc.stdout)
        self.assertNotIn("ALL-KILLED", proc.stdout)
        self.assert_target_clean()

    def test_repo_local_invocation_state_cannot_false_survive_killable_mutant(self):
        self.commit_suite("""
            import unittest
            import subject

            try:
                with open(".matrix-run-count", encoding="utf-8") as handle:
                    count = int(handle.read()) + 1
            except FileNotFoundError:
                count = 1
            with open(".matrix-run-count", "w", encoding="utf-8") as handle:
                handle.write(str(count))

            class SubjectTest(unittest.TestCase):
                def test_value_except_on_second_shared_run(self):
                    if count != 2:
                        self.assertEqual(subject.VALUE, 1)
        """, "false survivor witness")

        proc = self.run_matrix([self.mutation()])

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("KILLED value-two", proc.stdout)
        self.assertIn("ALL-KILLED 1/1", proc.stdout)
        self.assert_target_clean()

    def test_footer_shaped_text_cannot_spoof_unittest_count(self):
        self.commit_suite("""
            print("Ran 9 tests in 0.001s")
            print("OK")
        """, "footer spoof fixture")

        proc = self.run_matrix([self.mutation()])

        self.assert_matrix_error(proc, "baseline", "ran zero tests")
        self.assertNotIn("BASELINE GREEN", proc.stdout)
        self.assert_target_clean()

    def test_sys_argv_cannot_forge_failed_receipt_for_inert_mutant(self):
        self.commit_suite("""
            import json
            import sys
            import unittest
            import subject

            if subject.UNUSED == 2 and len(sys.argv) >= 3:
                receipt, nonce = sys.argv[1:3]
                with open(receipt, "w", encoding="utf-8") as handle:
                    json.dump({
                        "nonce": nonce,
                        "status": "FAILED",
                        "count": 1,
                        "import_failed": False,
                    }, handle)

            class SubjectTest(unittest.TestCase):
                def test_value(self):
                    self.assertEqual(subject.VALUE, 1)
        """, "forged failed receipt witness")
        mutation = self.mutation("unused-two", "UNUSED = 1", "UNUSED = 2")

        proc = self.run_matrix([mutation])

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("SURVIVED unused-two", proc.stdout)
        self.assertNotIn("KILLED unused-two", proc.stdout)
        self.assert_target_clean()

    def test_module_globals_cannot_forge_anonymous_receipt(self):
        self.commit_suite("""
            import __main__
            import json
            import os
            import unittest
            import subject

            if subject.UNUSED == 2 and hasattr(__main__, "receipt_fd"):
                payload = json.dumps({
                    "status": "FAILED",
                    "count": 1,
                    "import_failed": False,
                }, sort_keys=True).encode("utf-8")
                os.write(__main__.receipt_fd, payload)
                os.close(__main__.receipt_fd)

            class SubjectTest(unittest.TestCase):
                def test_value(self):
                    self.assertEqual(subject.VALUE, 1)
        """, "module-global receipt forgery witness")
        mutation = self.mutation("unused-two", "UNUSED = 1", "UNUSED = 2")

        proc = self.run_matrix([mutation])

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("SURVIVED unused-two", proc.stdout)
        self.assertNotIn("KILLED unused-two", proc.stdout)
        self.assertNotIn("ALL-KILLED", proc.stdout)
        self.assert_target_clean()

    def test_sys_argv_cannot_forge_ok_receipt_then_exit(self):
        self.commit_suite("""
            import json
            import os
            import sys
            import unittest
            import subject

            if subject.VALUE == 2 and len(sys.argv) >= 3:
                receipt, nonce = sys.argv[1:3]
                with open(receipt, "w", encoding="utf-8") as handle:
                    json.dump({
                        "nonce": nonce,
                        "status": "OK",
                        "count": 1,
                        "import_failed": False,
                    }, handle)
                os._exit(0)

            class SubjectTest(unittest.TestCase):
                def test_value(self):
                    self.assertEqual(subject.VALUE, 1)
        """, "forged ok receipt witness")

        proc = self.run_matrix([self.mutation()])

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("KILLED value-two", proc.stdout)
        self.assertIn("ALL-KILLED 1/1", proc.stdout)
        self.assertNotIn("SURVIVED value-two", proc.stdout)
        self.assert_target_clean()

    def test_arbitrary_non_unittest_command_is_refused(self):
        proc = self.run_matrix(
            [self.mutation()], command=[sys.executable, "suite.py"]
        )
        self.assert_matrix_error(proc, "preflight",
                                 "command must be PYTHON -m unittest")
        self.assertNotIn("BASELINE GREEN", proc.stdout)

    def test_import_failure_is_matrix_error_even_with_same_count(self):
        self.write("test_subject.py", textwrap.dedent("""
            import unittest
            import subject

            class SubjectTest(unittest.TestCase):
                def test_value(self):
                    self.assertEqual(subject.VALUE, 1)
        """).lstrip())
        self.commit_files("discovery fixture", "test_subject.py")
        command = self.command("discover", "-s", ".", "-p", "test_subject.py")
        mutation = self.mutation("syntax-error", "VALUE = 1", "VALUE = (")

        proc = self.run_matrix([mutation], command=command)

        self.assert_matrix_error(proc, "syntax-error", "collection/import failed")
        self.assertIn("CONTROL GREEN (1 tests)", proc.stdout)
        self.assertNotIn("KILLED syntax-error", proc.stdout)
        self.assert_target_clean()

    def test_zero_test_baseline_is_matrix_error(self):
        self.commit_suite("import unittest\n", "zero test fixture")

        proc = self.run_matrix([self.mutation()])

        self.assert_matrix_error(proc, "baseline", "ran zero tests")
        self.assertNotIn("BASELINE GREEN", proc.stdout)
        self.assert_target_clean()

    def test_nonfinite_and_extreme_timeouts_are_preflight_matrix_errors(self):
        for timeout in (float("nan"), float("inf"), float("-inf"),
                        1e308, 10 ** 400):
            with self.subTest(timeout=timeout):
                proc = self.run_matrix([self.mutation()],
                                       timeout_seconds=timeout)
                self.assert_matrix_error(proc, "preflight",
                                         "timeout_seconds must be")
                self.assertNotIn("Traceback", proc.stdout + proc.stderr)

    def test_finite_timeout_is_matrix_error_and_cleans_mutant(self):
        self.commit_suite("""
            import time
            import unittest
            import subject

            class SubjectTest(unittest.TestCase):
                def test_value(self):
                    if subject.VALUE == 2:
                        time.sleep(5)
                    self.assertEqual(subject.VALUE, 1)
        """, "timeout fixture")

        proc = self.run_matrix([self.mutation()], timeout_seconds=1)

        self.assert_matrix_error(proc, "value-two", "timed out")
        self.assertIn("CONTROL GREEN (1 tests)", proc.stdout)
        self.assert_target_clean()

    def test_forked_child_cannot_hold_receipt_past_deadline(self):
        marker = os.path.join(self.tmp, "forked-child-marker")
        self.commit_suite("""
            import os
            import time
            import unittest
            import subject

            MARKER = %r

            if subject.VALUE == 2:
                child = os.fork()
                if child == 0:
                    os.close(1)
                    os.close(2)
                    time.sleep(1.5)
                    with open(MARKER, "w", encoding="utf-8") as handle:
                        handle.write("escaped")
                    os._exit(0)

            class SubjectTest(unittest.TestCase):
                def test_value(self):
                    self.assertEqual(subject.VALUE, 1)
        """ % marker, "forked receipt holder witness")

        started = time.monotonic()
        proc = self.run_matrix([self.mutation()], timeout_seconds=0.5,
                               process_timeout=10)
        elapsed = time.monotonic() - started

        self.assert_matrix_error(proc, "value-two",
                                 "left descendant processes running")
        self.assertLess(elapsed, 3.0, proc.stdout + proc.stderr)
        self.assertNotIn("KILLED value-two", proc.stdout)
        self.assertNotIn("SURVIVED value-two", proc.stdout)
        time.sleep(1.7)
        self.assertFalse(os.path.exists(marker), "forked child escaped cleanup")
        self.assert_target_clean()

    def test_command_not_found_is_baseline_matrix_error(self):
        command = ["definitely-not-a-python", "-m", "unittest", "suite.py"]
        proc = self.run_matrix([self.mutation()], command=command)
        self.assert_matrix_error(proc, "baseline", "could not run")
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        self.assert_target_clean()

    def test_missing_private_receipt_is_matrix_error(self):
        self.commit_suite("""
            import os
            import unittest
            import subject

            if subject.VALUE == 2:
                os._exit(7)

            class SubjectTest(unittest.TestCase):
                def test_value(self):
                    self.assertEqual(subject.VALUE, 1)
        """, "abnormal exit fixture")

        proc = self.run_matrix([self.mutation()])

        self.assert_matrix_error(proc, "value-two", "receipt missing/unreadable")
        self.assertIn("CONTROL GREEN (1 tests)", proc.stdout)
        self.assertNotIn("KILLED value-two", proc.stdout)
        self.assert_target_clean()

    def test_receipt_then_sigkill_is_matrix_error_not_killed(self):
        self.commit_suite("""
            import atexit
            import os
            import signal
            import unittest
            import subject

            if subject.VALUE == 2:
                atexit.register(
                    lambda: os.kill(os.getpid(), signal.SIGKILL)
                )

            class SubjectTest(unittest.TestCase):
                def test_value(self):
                    self.assertEqual(subject.VALUE, 1)
        """, "receipt then signal witness")

        proc = self.run_matrix([self.mutation()])

        self.assert_matrix_error(proc, "value-two",
                                 "test exit -9 disagrees with unittest status FAILED")
        self.assertIn("CONTROL GREEN (1 tests)", proc.stdout)
        self.assertNotIn("KILLED value-two", proc.stdout)
        self.assert_target_clean()

    def test_final_control_failure_prevents_all_killed_summary(self):
        count_path = os.path.join(self.tmp, "external-run-count")
        self.commit_suite("""
            import unittest
            import subject

            COUNT_PATH = %r
            try:
                with open(COUNT_PATH, encoding="utf-8") as handle:
                    count = int(handle.read()) + 1
            except FileNotFoundError:
                count = 1
            with open(COUNT_PATH, "w", encoding="utf-8") as handle:
                handle.write(str(count))

            class SubjectTest(unittest.TestCase):
                def test_value(self):
                    if count == 3:
                        self.fail("final control failed")
                    self.assertEqual(subject.VALUE, 1)
        """ % count_path, "final control failure fixture")

        proc = self.run_matrix([self.mutation()])

        self.assert_matrix_error(proc, "final-control", "green control failed")
        self.assertIn("KILLED value-two", proc.stdout)
        self.assert_target_clean()

    def test_mutation_name_cannot_inject_result_lines(self):
        for name in ("inert\rALL-KILLED 99/99", "inert\x1b[2J", "inert line"):
            with self.subTest(name=repr(name)):
                mutation = self.mutation(name, "UNUSED = 1", "UNUSED = 2")
                proc = self.run_matrix([mutation])
                self.assert_matrix_error(proc, "preflight",
                                         "printable-ASCII one-line name")
                self.assertNotIn("ALL-KILLED 99/99", proc.stdout)
                self.assertNotIn("Traceback", proc.stdout + proc.stderr)
                self.assert_target_clean()

    def test_unpaired_surrogates_are_preflight_matrix_errors(self):
        cases = (
            self.mutation("surrogate-find", chr(0xD800), "VALUE = 2"),
            self.mutation("surrogate-replace", "VALUE = 1", chr(0xD800)),
        )
        for mutation in cases:
            with self.subTest(name=mutation["name"]):
                proc = self.run_matrix([mutation])
                self.assert_matrix_error(proc, "preflight", "valid UTF-8 text")
                self.assertNotIn("UnicodeEncodeError", proc.stderr)
                self.assertNotIn("Traceback", proc.stdout + proc.stderr)
                self.assert_target_clean()

    def test_path_surrogates_and_controls_are_safe_preflight_errors(self):
        cases = (
            ("path-high-surrogate", "bad" + chr(0xD800) + ".py", None),
            ("path-low-surrogate", "bad" + chr(0xDC80) + ".py", None),
            ("path-escape", "bad\x1b[2J.py", "\x1b"),
            ("path-control", "bad\x07bell.py", "\x07"),
        )
        for name, path, raw_control in cases:
            with self.subTest(name=name):
                proc = self.run_matrix([self.mutation(name=name, path=path)])
                self.assert_matrix_error(proc, "preflight", "path must")
                output = proc.stdout + proc.stderr
                self.assertNotIn("UnicodeEncodeError", output)
                self.assertNotIn("Traceback", output)
                if raw_control is not None:
                    self.assertNotIn(raw_control, output)
                self.assert_target_clean()

    def test_byte_identical_replacement_is_matrix_error(self):
        proc = self.run_matrix([
            self.mutation("no-byte-change", "VALUE = 1", "VALUE = 1")
        ])
        self.assert_matrix_error(proc, "preflight", "replacement is byte-identical")
        self.assert_target_clean()


if __name__ == "__main__":
    unittest.main()
