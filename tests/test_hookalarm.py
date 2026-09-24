"""The rate limit on the fail-open alarm — both halves of one file format.

WHAT THIS MODULE HAS TO PROVE is not that a counter counts. It is that the two
INDEPENDENT implementations of the same suppression — Python, for alarms helm
is alive to print, and POSIX sh, for the ones where helm has been killed by its
own `timeout` — agree about the directory, the window and the file. If they
disagree the state splits in half, every line prints twice, and nothing is
broken enough for anyone to notice. So the sh half is EXECUTED here against
the same directory the Python half reads back, rather than asserted as text.
"""
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from helm import hookalarm, hooks                            # noqa: E402


class HookAlarmBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="helm-hookalarm-test-")
        self._prior = {hookalarm.DIR_ENV: os.environ.get(hookalarm.DIR_ENV)}
        # These arms are ABOUT the door, so they name their own directory and
        # take the real mechanism — the test-runner seam is what every OTHER
        # module's arms rely on, and naming a directory is what overrides it.
        os.environ[hookalarm.DIR_ENV] = self.dir

    def tearDown(self):
        for k, v in self._prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class WindowTest(HookAlarmBase):
    def test_the_first_alarm_speaks_and_its_repeats_do_not(self):  # noqa: VACUOUS_ASSERTION — the assertEqual below is the unconditional positive control on the SAME observable: `line()` returning the whole text on this key, immediately before the None assertions on it
        text = "[helm argv-guard] TIMED OUT at 2s — this tool call is UNCHECKED"
        now = time.time()
        first = hookalarm.line("argv-guard", text, now=now)
        self.assertEqual(first, text,
                         "the first alarm of a window must say the whole "
                         "thing — a silent fail-open is the worse failure")
        for i in range(4):
            self.assertIsNone(hookalarm.line("argv-guard", text, now=now),
                              "repeat %d spoke inside the window" % i)

    def test_a_swallowed_count_reaches_the_next_line_that_speaks(self):
        """The counter is DEFERRED, never dropped. This is the half that makes
        the suppression honest: the owner still learns that four tool calls ran
        unchecked, one window later instead of four lines ago."""
        text = "[helm argv-guard] TIMED OUT at 2s — this tool call is UNCHECKED"
        t0 = time.time()
        self.assertIsNotNone(hookalarm.line("argv-guard", text, now=t0))
        for _ in range(4):
            hookalarm.line("argv-guard", text, now=t0)
        later = hookalarm.line("argv-guard", text,
                               now=t0 + (hookalarm.WINDOW_MIN + 1) * 60)
        self.assertIsNotNone(later, "the next window never spoke at all")
        self.assertIn("+4 suppressed", later,
                      "four swallowed alarms were counted and never reported")

    def test_a_reported_count_is_not_reported_twice(self):
        text = "x"
        t0 = time.time()
        hookalarm.line("deliver", text, now=t0)
        hookalarm.line("deliver", text, now=t0)
        step = (hookalarm.WINDOW_MIN + 1) * 60
        self.assertIn("+1 suppressed", hookalarm.line("deliver", text, now=t0 + step))
        self.assertEqual(hookalarm.line("deliver", text, now=t0 + 2 * step), text,
                         "the same swallowed alarm was counted in two windows")

    def test_classes_do_not_silence_each_other(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone in the middle is an unconditional positive control on the same observable (suppression IS happening here), and the walker cannot see it because it reads as an absence itself
        """The window is PER CLASS. Without the middle assertion this arm would
        pass on a module that suppresses nothing at all, which is the shape it
        is supposed to discriminate against."""
        now = time.time()
        self.assertIsNotNone(hookalarm.line("argv-guard", "a", now=now))
        self.assertIsNone(hookalarm.line("argv-guard", "a", now=now),
                          "MUST-HIT: nothing is being suppressed here, so the "
                          "assertion below cannot tell per-class from per-none")
        self.assertIsNotNone(hookalarm.line("deliver", "b", now=now),
                             "one hook's timeout muted a different hook's")

    def test_an_unwritable_state_dir_speaks_rather_than_goes_quiet(self):
        """FAIL-OPEN, in the direction that keeps the fact. Losing the counter
        costs a duplicate line; losing the line costs the unchecked-tool-call
        fact itself, which is the thing this module exists to preserve.

        A FILE where the directory should be, not a bad byte in the path: the
        first spelling of this arm put a NUL in the value and raised inside
        `os.environ.__setitem__`, so it measured the assignment and never
        reached the module at all."""
        blocked = os.path.join(self.dir, "a-file-not-a-dir")
        with open(blocked, "w") as f:
            f.write("x")
        os.environ[hookalarm.DIR_ENV] = os.path.join(blocked, "state")
        self.assertEqual(hookalarm.line("argv-guard", "text"), "text")
        self.assertEqual(hookalarm.line("argv-guard", "text"), "text",
                         "a broken counter must keep speaking, not latch")

    def test_a_key_that_is_not_a_bare_token_is_refused(self):
        """The key is spliced into a path INSIDE generated shell. A separator
        here writes outside the state dir, so this refuses rather than quotes."""
        # UNCONDITIONAL POSITIVE CONTROL, outside the loop: a real spec name
        # is accepted and renders. Without it, a `shell_suppressed` that raised
        # on EVERYTHING would pass every assertion below.
        self.assertIn("printf X", hookalarm.shell_suppressed("argv-guard",
                                                             "printf X"))
        for bad in ("../escape", "a/b", ".hidden", ""):
            with self.subTest(key=bad):
                with self.assertRaises(ValueError):
                    hookalarm.shell_suppressed(bad, "printf X")


class ShellAgreesWithPythonTest(HookAlarmBase):
    """THE TWO HALVES, RUN AGAINST ONE DIRECTORY.

    The sh ladder is what speaks when helm has been killed, so it cannot call
    any of this module's code. It is executed here for real and the Python
    half reads its state back — which is the only way to catch the two
    agreeing in prose and disagreeing in a path or a window token.
    """

    def run_sh(self, key="argv-guard", speaking="printf 'SPOKE\\n'"):
        frag = hookalarm.shell_suppressed(key, speaking)
        p = subprocess.run(["sh", "-c", frag], capture_output=True, text=True,
                           env=dict(os.environ, **{hookalarm.DIR_ENV: self.dir}))
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_the_shell_half_speaks_once_and_then_counts(self):
        self.assertEqual(self.run_sh(), "SPOKE\n",
                         "MUST-HIT: the fragment never printed at all, so the "
                         "silence below would prove nothing")
        for i in range(3):
            self.assertEqual(self.run_sh(), "", "sh repeat %d spoke" % i)

    def test_python_reads_the_count_the_shell_wrote(self):
        """The format is the contract. If sh wrote a different directory or a
        different window token, this reads zero and the arrears vanish.

        THE TWO HALVES DO NOT SHARE A KEY IN PRODUCTION — the sh ladder keys on
        the spec name, `hookrun` on 'handler-<name>', `posttoolrun` on
        'posttoolrun-<phase>'. This arm is therefore ABOUT THE FORMAT and says
        so: it is what proves each half can read back state the other half's
        implementation of the same format wrote, which is what a restart, a
        rename or a window change would break.
        """
        self.run_sh()
        for _ in range(3):
            self.run_sh()
        later = hookalarm.line("argv-guard", "text",
                               now=time.time() + (hookalarm.WINDOW_MIN + 1) * 60)
        self.assertIn("+3 suppressed", later,
                      "Python did not find the counter sh wrote — the two "
                      "halves disagree about the directory or the window")

    def test_the_shell_half_reports_the_count_it_wrote(self):
        """THE HALF THAT DROPPED THE COUNT. A timeout kills the helm process,
        so the two banners the owner actually read come from this ladder by
        construction — and it wrote a byte per repeat and drained nothing, so
        the arrears clause both this module and docs/HOOKS.md promise was
        false for exactly those lines. The counters also accumulated unread.
        """
        self.assertEqual(self.run_sh(), "SPOKE\n",
                         "MUST-HIT: the fragment never spoke, so an absent "
                         "clause below would prove nothing")
        for _ in range(3):
            self.run_sh()
        self._age_buckets("argv-guard", "20260916120")
        spoke = self.run_sh(speaking="printf 'SPOKE%s\\n' \"$ha\"")
        self.assertIn("+3 suppressed", spoke,
                      "the sh half swallowed three repeats and never reported "
                      "them — the arrears promise is false for the half that "
                      "prints when helm has been killed")
        self.assertEqual(
            sorted(n for n in os.listdir(self.dir)
                   if n.startswith("argv-guard.")), ["argv-guard." + self._now()],
            "the reported counters were left on disk to be reported twice")

    def test_both_halves_render_the_same_clause_from_the_same_state(self):
        """ONE FORMAT, TWO IMPLEMENTATIONS, and the clause is part of it.

        A count agreed and a stamp disagreed would be invisible: both lines
        look right on their own. So the two renderings are compared BYTE FOR
        BYTE against identical seeded state, one key each.
        """
        for key in ("argv-guard", "deliver"):
            self._seed(key, "20260916120", 3)
            self._seed(key, "20260916125", 2)
        shell = self.run_sh(speaking="printf '%s\\n' \"$ha\"").rstrip("\n")
        python = hookalarm.line("deliver", "")
        self.assertEqual(shell, " (+5 suppressed since 12:00Z)",
                         "MUST-HIT: the sh half rendered no clause at all, so "
                         "an equality below would compare two absences")
        self.assertEqual(python, shell,
                         "the two halves render the same state differently")

    def test_the_since_stamp_names_the_window_it_measured(self):
        """A fixed "in the last 10 min" is a bound the drain does not enforce:
        `_drain_older` sums every bucket it can see with no age limit, so a
        counter six hours old renders as ten minutes old and the error always
        overstates recency. The clause names the window it measured."""
        self._seed("deliver", "20260916123", 5)
        said = hookalarm.line("deliver", "x")
        self.assertIn("+5 suppressed", said,
                      "MUST-HIT: nothing was drained, so the stamp below is "
                      "not a reading of this seeded bucket")
        self.assertIn("since 12:30Z", said,
                      "the clause named a bound it did not measure")
        self.assertNotIn("10 min", said)

    def _now(self):
        return time.strftime("%Y%m%d%H%M", time.gmtime())[:-1]

    def _seed(self, key, bucket, n):
        with open(os.path.join(self.dir, "%s.%s" % (key, bucket)), "wb") as f:
            f.write(b"." * n)

    def _age_buckets(self, key, bucket):
        for name in sorted(os.listdir(self.dir)):
            if name.startswith(key + "."):
                os.rename(os.path.join(self.dir, name),
                          os.path.join(self.dir, "%s.%s" % (key, bucket)))

    def test_the_shell_half_speaks_when_python_has_not(self):  # noqa: VACUOUS_ASSERTION — the first line is an unconditional positive control on the same observable, run through the `run_sh` HELPER the walker does not follow: a fresh key makes the identical fragment print SPOKE
        """Direction two: sh must see Python's marker, not only the reverse."""
        # UNCONDITIONAL POSITIVE CONTROL on the same observable, on a key
        # nothing has touched: the fragment DOES print here. The silence
        # asserted below is therefore a marker being honoured, not a fragment
        # that prints nothing under this harness.
        self.assertEqual(self.run_sh(key="deliver"), "SPOKE\n")
        self.assertIsNotNone(hookalarm.line("argv-guard", "text"))
        self.assertEqual(self.run_sh(), "",
                         "sh spoke although Python had already spoken in this "
                         "window — the marker sh reads is not the one Python "
                         "writes")


class FailOpenUnderDashTest(unittest.TestCase):
    """THE ALARM MUST NOT BE ABLE TO BLOCK THE TOOL CALL IT IS REPORTING ON.

    rc 2 out of a PreToolUse or a Stop wrapper is BLOCK. The first cut of the
    124 arm created its marker with `: > "$hf"`, and `:` is a POSIX SPECIAL
    BUILT-IN: a redirection error on one makes a non-interactive shell exit
    immediately. Under /bin/sh — dash on a Debian box, which is what these
    wrappers actually run under — an alarm directory that could not be written
    therefore turned a hook TIMEOUT into rc 2 with no helm sentence at all: a
    refused tool call, or a turn the agent cannot end, from an internal failure
    of the diagnostic. Under bash the same ladder exited 0 and printed, which
    is why it was never seen.

    THE LADDER IS THE REAL ONE — the tracked bin/helm-hook, executed. Only the
    CHILD is swapped, for a process that is genuinely killed by its own
    `timeout`, so rc 124 arrives the way production produces it and every
    metadata operand is the one `hooks.spec_command` renders.
    """

    SHELLS = ("/bin/sh", "/bin/bash")

    def setUp(self):
        self.tmp = []

    def tearDown(self):
        for d in self.tmp:
            try:
                os.chmod(d, 0o700)
                shutil.rmtree(d)
            except OSError:
                pass

    def _mkdtemp(self, mode=None):
        d = tempfile.mkdtemp(prefix="helm-hookalarm-failopen-")
        self.tmp.append(d)
        if mode is not None:
            os.chmod(d, mode)
        return d

    def _ladder(self, name, child):
        """The SHIPPED wrapper invocation with only the CHILD replaced."""
        spec = next(s for s in hooks.SPECS if s["name"] == name)
        words = shlex.split(
            hooks.spec_command(spec, executable="/opt/helm/bin/helm"))
        self.assertEqual(words[6], "/opt/helm/bin/helm",
                         "MUST-HIT: the child this arm swaps is not the child "
                         "the renderer wrote, so the rest of the ladder is not "
                         "the shipped one either")
        # The wrapper is addressed at THIS CHECKOUT, because /opt/helm is a
        # path for rendering and not one that exists; every other operand is
        # the rendered one.
        words[0] = os.path.join(ROOT, "bin", hooks.HOOK_WRAPPER)
        return " ".join(shlex.quote(w) for w in words[:6]) + " " + child

    def _run(self, frag, shell, alarm_dir):
        p = subprocess.run([shell, "-c", frag], capture_output=True, text=True,
                           env=dict(os.environ,
                                    **{hookalarm.DIR_ENV: alarm_dir}))
        return p.returncode, p.stdout + p.stderr

    def _unusable_dirs(self):
        parent = self._mkdtemp(0o500)
        notdir = self._mkdtemp()
        with open(os.path.join(notdir, "afile"), "w") as f:
            f.write("x")
        return (("an alarm dir nothing can create", "/proc/helm-no-such-dir/s"),
                ("a read-only alarm dir", self._mkdtemp(0o500)),
                ("a missing alarm dir under an unwritable parent",
                 os.path.join(parent, "state")),
                ("a non-directory where the alarm dir should be",
                 os.path.join(notdir, "afile", "state")))

    def test_an_unusable_alarm_dir_still_exits_0_and_still_says_unchecked(self):  # noqa: VACUOUS_ASSERTION — `_ladder` asserts UNCONDITIONALLY, on every call, that the shipped renderer's base is the one being swapped; a ladder that was not the shipped one fails there rather than passing quietly here, and the usable-dir control below runs the identical fragment
        for name in ("argv-guard", "deliver"):
            frag = self._ladder(name, "timeout 0.1 /bin/sleep 5")
            for label, d in self._unusable_dirs():
                for shell in self.SHELLS:
                    with self.subTest(hook=name, dir=label, shell=shell):
                        rc, said = self._run(frag, shell, d)
                        self.assertEqual(rc, 0,
                                         "a broken ALARM turned a timeout into "
                                         "rc %d — on %s that BLOCKS" % (rc, name))
                        self.assertIn("[helm %s]" % name, said,
                                      "the hook was never named")
                        self.assertIn("TIMED OUT", said,
                                      "the line does not say why it could not "
                                      "be checked")

    def test_a_usable_alarm_dir_is_the_control_for_that(self):  # noqa: VACUOUS_ASSERTION — this arm IS the positive control for the one above: same ladder, one argument different, asserting the line PRESENT rather than any absence
        """CONTROL: the identical ladder, one argument different, speaks and
        exits 0 — so the arm above is discriminating the DIRECTORY and not a
        fragment that exits 0 and prints whatever happens."""
        for name in ("argv-guard", "deliver"):
            with self.subTest(hook=name):
                rc, said = self._run(self._ladder(name, "timeout 0.1 /bin/sleep 5"),
                                     "/bin/sh", self._mkdtemp())
                self.assertEqual(rc, 0)
                self.assertIn("[helm %s] TIMED OUT" % name, said)

    def test_a_real_refusal_is_still_the_only_rc_2(self):  # noqa: VACUOUS_ASSERTION — the advisory leg after the loop is unconditional and asserts a PRESENT line (`FAILED rc=2`) through the same `_ladder`/`_run` pair, so a harness that printed nothing at all fails there
        """THE POSITIVE CONTROL THE CURE COULD HAVE DESTROYED. A gate keeps its
        teeth: rc 2 from the guard itself still propagates, and only that."""
        gate = self._ladder("argv-guard", "sh -c 'exit 2'")
        for label, d in (("a usable alarm dir", self._mkdtemp()),
                         ("an unusable alarm dir", "/proc/helm-no-such-dir/s")):
            with self.subTest(alarm_dir=label):
                rc, said = self._run(gate, "/bin/sh", d)
                self.assertEqual(rc, 2, "the guard's own refusal was swallowed")
                self.assertEqual(said, "",
                                 "a refusal is not a timeout and must not "
                                 "print the alarm")
        advisory = self._ladder("deliver", "sh -c 'exit 2'")
        rc, said = self._run(advisory, "/bin/sh", self._mkdtemp())
        self.assertEqual(rc, 0, "an advisory spec must never become a blocker")
        self.assertIn("FAILED rc=2", said)


class SuiteSeamTest(HookAlarmBase):
    """A SUITE IS NOT A FLEET, and this class is the arm that would have caught
    the first seam. It was keyed on `HELM_GATE_SUITE_CAP` — true of the gate
    runner, false of a focused `fab test` — and 25 arms in sibling modules went
    red because each asserted a line an earlier arm had already spent."""

    def test_a_process_running_tests_with_no_named_dir_always_speaks(self):
        os.environ.pop(hookalarm.DIR_ENV, None)
        # CONTROL: this arm is worthless unless the condition it relies on is
        # actually true here — a suite that did not import unittest would make
        # the three assertions below pass for the wrong reason.
        self.assertTrue(hookalarm._under_a_test_runner(),
                        "MUST-HIT: unittest is not in sys.modules inside a "
                        "unittest run, so this arm measures nothing")
        for _ in range(3):
            self.assertEqual(hookalarm.speak("argv-guard"), (True, 0, ""))

    def test_a_named_dir_beats_the_test_runner_seam(self):
        os.environ[hookalarm.DIR_ENV] = self.dir
        self.assertEqual(hookalarm.speak("argv-guard")[0], True)
        self.assertEqual(hookalarm.speak("argv-guard")[0], False,
                         "the seam swallowed an explicitly named window")


if __name__ == "__main__":
    unittest.main()
