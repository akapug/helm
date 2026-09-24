#!/usr/bin/env python3
"""The hook wrapper is a SHIPPED FILE, and the timing trace is BUFFERED.

THE OWNER'S COMPLAINT, twice: "why is this stophook so fuggin long? are our
stophooks TRULY optimized for concision and usefulness each time they run?"
What a blocked stop printed in his terminal was ~4.9 kB, of which the one
sentence he needed was 900 characters and the rest was machinery: Claude Code
echoes a hook's WHOLE command string whenever that hook blocks or errors, so
every character of the inline rc-case ladder was owner-facing output, and the
rung timings streamed twenty-eight lines on a run that was never in danger.

THREE CURES, ONE PER SECTION BELOW:
  * the ladder moved into `bin/helm-hook`, so the echoed command is the short
    invocation of it. Its ARMS are unchanged and are executed here against
    stub children for rc 0, 2, a real `timeout` kill, 127 and an unknown code;
  * the trace buffers and speaks only once the ladder is in danger;
  * the character budgets, as numbers, so prose cannot creep back.

WHY THE ARMS EXECUTE RATHER THAN READ. An arm that greps the shipped script
for `124)` proves the text is present and nothing about what the shell does
with it — and the two are exactly what came apart the last time this ladder
was wrong (`: > "$hf"` is a POSIX special built-in; under dash a failed
redirection on one exits the shell at rc 2, which out of a Stop hook is a
BLOCK). Every arm below runs /bin/sh against the tracked file.
"""
import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import hookalarm, hooks, seats_stop_timing  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAPPER = os.path.join(ROOT, "bin", hooks.HOOK_WRAPPER)

# THE COMMAND'S OWN OVERHEAD, in characters, once the two absolute paths are
# taken out — they are decided by how deep the checkout sits and say nothing
# about this ladder. The inline gate ladder was 2,039 characters for the Stop
# gate, of which ~1,900 was the rc-case; the wrapper's whole argv is the five
# operands. The message half's budget lives beside the message, in
# tests/test_stop_lease_latch.py.
COMMAND_OVERHEAD_BUDGET = 80


_SCRATCH_GC = "HELM_SCRATCH_GC"
_SCRATCH_PRIOR = None


def setUpModule():
    """NEUTRALIZE THE SCRATCH REAPER FOR THE WHOLE MODULE.

    The stop-guard's silent-mechanical lane DELETES dead-session scratch under
    the real `/tmp/claude-*` harness estate, and CONTRIBUTING.md's law is that
    a test never touches a real harness store. `tests.test_scratch`'s tripwire
    is source-driven — it reads every module for a stop-guard invocation and
    fails the ones that do not set this — and this module named the Stop gate
    as a verb argument from its first commit while nothing here set it. The
    tripwire was RED on this lane and unseen, because no focused run included
    the module that holds it. It is set for the module rather than per class
    so a class added later inherits it instead of re-opening the hole.
    """
    global _SCRATCH_PRIOR
    _SCRATCH_PRIOR = os.environ.get(_SCRATCH_GC)
    os.environ[_SCRATCH_GC] = "0"


def tearDownModule():
    if _SCRATCH_PRIOR is None:
        os.environ.pop(_SCRATCH_GC, None)
    else:
        os.environ[_SCRATCH_GC] = _SCRATCH_PRIOR


def _stub(dirpath, body):
    """A stub child, with the SHIPPED wrapper beside it.

    `hooks.wrapper_bin` derives the wrapper from the helm it wraps, so a stub
    that arrives without its sibling makes the command's FIRST word nonexistent
    and every arm measures the shell's own 127 instead of the ladder.
    """
    os.makedirs(dirpath, exist_ok=True)
    child = os.path.join(dirpath, "helm")
    with open(child, "w") as f:
        f.write(body)
    os.chmod(child, 0o755)
    shutil.copy2(WRAPPER, os.path.join(dirpath, hooks.HOOK_WRAPPER))
    return child


class WrapperIsAShippedFileTest(unittest.TestCase):
    def test_the_wrapper_is_tracked_executable_and_valid_posix_sh(self):
        """A hook command whose first word is not there exits 127 before any
        line of the script runs, and the harness reads a non-2 code as ALLOW."""
        self.assertTrue(os.path.isfile(WRAPPER), WRAPPER)
        self.assertTrue(os.access(WRAPPER, os.X_OK),
                        "the wrapper is not executable, so every hook in the "
                        "estate would exit 126")
        tracked = subprocess.run(
            ["git", "-C", ROOT, "ls-files", "--error-unmatch",
             "bin/" + hooks.HOOK_WRAPPER],
            capture_output=True, text=True)
        self.assertEqual(tracked.returncode, 0,
                         "the wrapper is untracked, so a fresh checkout would "
                         "install hooks that cannot run: %s" % tracked.stderr)
        self.assertEqual(
            subprocess.run(["sh", "-n", WRAPPER],
                           capture_output=True, text=True).returncode, 0)

    def test_the_installed_command_is_the_short_invocation(self):
        """THE OWNER-FACING NUMBER. Claude Code echoes this string on every
        blocked stop; it was 2,039 characters for the Stop gate."""
        spec = next(s for s in hooks.SPECS if s["name"] == "stop-guard")
        cmd = hooks.spec_command(spec)
        words = shlex.split(cmd)
        self.assertEqual(words[0], hooks.wrapper_bin())
        self.assertEqual(words[1], "gate")
        self.assertEqual(words[2], "stop-guard")
        self.assertEqual(words[3], "Stop")
        self.assertEqual(words[4], str(spec["timeout"]))
        self.assertEqual(words[5], "stop")
        self.assertEqual(" ".join(words[6:]),
                         "%s %s" % (hooks.helm_bin(), spec["args"]))
        # POSITIVE CONTROL on the measurement: the paths themselves are most
        # of what is left, so the budget is stated against the command MINUS
        # them rather than against a number the checkout's depth decides.
        overhead = len(cmd) - len(words[0]) - len(words[6])
        self.assertLess(overhead, COMMAND_OVERHEAD_BUDGET, cmd)
        for text in ("rc=$?", "case ", "esac", "|| true", "printf"):
            self.assertNotIn(text, cmd,
                             "the ladder is back in the echoed command")

    def test_the_alarm_fragment_is_hookalarms_own_rendering(self):
        """ONE AUTHORITY FOR THE SUPPRESSION FORMAT. The state is shared with
        the Python half — same directory, same bucket token, same arrears
        clause — so a divergence here is two directories and every timeout line
        printed twice, which breaks nothing loudly."""
        text = open(WRAPPER, encoding="utf-8").read()
        self.assertIn(hookalarm.shell_suppressed("$name", 'say "$m$ha"'), text,
                      "bin/helm-hook's alarm body is no longer the one "
                      "hookalarm.shell_suppressed renders")


class TheArmsAreExecutedTest(unittest.TestCase):
    """rc 0, rc 2, a real `timeout` kill, rc 127 and an unknown code, run."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-hookwrapper-")
        self.window = os.path.join(self.tmp, "alarm")
        self.n = 0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_kind(self, kind, body, budget=5, name="stop-guard",
                 event="Stop", what="stop", child=None, window=None):
        """Run the SHIPPED wrapper over a stub child, and return the process.

        `child` names a path the caller owns (the missing-guard arm needs one
        that is NOT there); the wrapper is then read from THAT directory,
        because `wrapper_bin` derives it from the child's own dirname and a
        wrapper looked for anywhere else makes the arm measure the shell's 127
        rather than the ladder's.
        """
        if child is None:
            self.n += 1
            d = os.path.join(self.tmp, "k%d" % self.n)
            stub = _stub(d, body)
        else:
            stub, d = child, os.path.dirname(child)
        cmd = "%s %s %s %s %d %s %s" % (
            shlex.quote(os.path.join(d, hooks.HOOK_WRAPPER)), kind,
            shlex.quote(name), shlex.quote(event), budget, shlex.quote(what),
            shlex.quote(stub))
        p = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True,
                           env=dict(os.environ,
                                    HELM_HOOK_ALARM_DIR=(
                                        window or tempfile.mkdtemp(dir=self.tmp))))
        return p

    def test_rc0_passes_in_silence_on_both_channels(self):  # noqa: VACUOUS_ASSERTION — the loud arms below run the same wrapper on the same channels, so silence here is the product's choice rather than a channel that never carries anything
        for kind in ("gate", "lane"):
            with self.subTest(kind=kind):
                p = self.run_kind(kind, "#!/bin/sh\nexit 0\n")
                self.assertEqual(p.returncode, 0)
                self.assertEqual(p.stderr, "")
                self.assertEqual(p.stdout, "")

    def test_rc2_blocks_a_gate_and_only_announces_for_a_lane(self):
        gate = self.run_kind("gate", "#!/bin/sh\nexit 2\n")
        self.assertEqual(gate.returncode, 2, "the gate lost its teeth")
        self.assertEqual(gate.stderr, "")
        lane = self.run_kind("lane", "#!/bin/sh\nexit 2\n",
                             what="pending chat is deferred")
        self.assertEqual(lane.returncode, 0,
                         "an advisory rc 2 BLOCKED — every lane hook is now a "
                         "blocker")
        self.assertIn("FAILED rc=2", lane.stderr)

    def test_a_real_timeout_kill_says_the_event_went_unchecked(self):
        p = self.run_kind("gate", "#!/bin/sh\nsleep 30\n", budget=1)
        self.assertEqual(p.returncode, 0, "a timeout now BLOCKS")
        self.assertIn("TIMED OUT at 1s", p.stderr)
        self.assertIn("this stop is UNCHECKED", p.stderr)
        doc = json.loads(p.stdout)
        self.assertEqual(doc["hookSpecificOutput"]["hookEventName"], "Stop")
        self.assertEqual(doc["systemMessage"],
                         doc["hookSpecificOutput"]["additionalContext"])

    def test_a_missing_child_is_rc127_and_names_the_path(self):
        """THE DELETED LANE ROOM, ON BOTH OF HELM'S CHANNELS.

        This is the case the whole rc ladder was written for, and moving the
        ladder into a file must not cost it either channel. docs/HOOKS.md's own
        law is that stderr reaches the debug log while the exit-0 JSON is what
        the harness surfaces, so an arm that reads only stderr would pass over
        a wrapper that had gone silent where it counts. The WRAPPER's own
        absence is the state helm cannot speak for at all — measured in
        `test_a_missing_wrapper_fails_open_loudly_and_the_census_sees_it`,
        where the census speaks instead.
        """
        d = os.path.join(self.tmp, "deleted-room", "bin")
        os.makedirs(d)
        shutil.copy2(WRAPPER, os.path.join(d, hooks.HOOK_WRAPPER))
        gone = os.path.join(d, "helm")
        p = self.run_kind("gate", None, child=gone)
        self.assertEqual(p.returncode, 0, "fail-open law unchanged")
        self.assertIn("THE GUARD IS MISSING", p.stderr)
        self.assertIn(gone, p.stderr)
        self.assertIn("helm hooks install", p.stderr)
        # THE CHANNEL THE HARNESS ACTUALLY READS, parsed rather than matched:
        # an unparseable document fails exactly like an absent one.
        doc = json.loads(p.stdout)
        self.assertIn("THE GUARD IS MISSING", doc["systemMessage"])
        self.assertIn(gone, doc["systemMessage"])
        self.assertIn("helm hooks install", doc["systemMessage"])
        self.assertEqual(doc["hookSpecificOutput"]["hookEventName"], "Stop")
        self.assertEqual(doc["hookSpecificOutput"]["additionalContext"],
                         doc["systemMessage"])

    def test_an_unknown_code_is_loud_by_construction(self):
        for code in (1, 42):
            with self.subTest(rc=code):
                p = self.run_kind("gate", "#!/bin/sh\nexit %d\n" % code)
                self.assertEqual(p.returncode, 0)
                self.assertIn("THE GUARD FAILED rc=%d" % code, p.stderr)
                self.assertIn("this stop is ALLOWED and UNCHECKED", p.stderr)

    def test_the_noun_is_the_specs_own_and_not_the_templates(self):
        """One script renders every gate, and a hardcoded "this stop" told the
        argv-guard's PreToolUse reader a STOP had been allowed when what went
        unchecked was a tool call."""
        stop = self.run_kind("gate", "#!/bin/sh\nexit 7\n")
        tool = self.run_kind("gate", "#!/bin/sh\nexit 7\n", name="argv-guard",
                             event="PreToolUse", what="tool call")
        self.assertIn("this stop is ALLOWED", stop.stderr)
        self.assertIn("this tool call is ALLOWED", tool.stderr)
        self.assertNotIn("this stop", tool.stderr)
        self.assertEqual(
            json.loads(tool.stdout)["hookSpecificOutput"]["hookEventName"],
            "PreToolUse")

    def test_only_the_timeout_arm_is_rate_limited(self):
        """A timeout repeats per tool call with nothing new to say; MISSING and
        an unknown rc each name a broken estate that must be read every time."""
        os.makedirs(self.window)
        first = self.run_kind("lane", "#!/bin/sh\nsleep 30\n", budget=1,
                              name="deliver", event="PostToolUse",
                              what="pending chat is deferred", window=self.window)
        self.assertIn("TIMED OUT", first.stderr)      # positive control
        again = self.run_kind("lane", "#!/bin/sh\nsleep 30\n", budget=1,
                              name="deliver", event="PostToolUse",
                              what="pending chat is deferred", window=self.window)
        self.assertEqual(again.stderr, "", "the repeat was not suppressed")
        for _ in range(2):
            loud = self.run_kind("lane", "#!/bin/sh\nexit 9\n", name="deliver",
                                 event="PostToolUse",
                                 what="pending chat is deferred",
                                 window=self.window)
            self.assertIn("FAILED rc=9", loud.stderr,
                          "an unknown rc was rate-limited, so a broken estate "
                          "goes unread")

    def test_a_missing_wrapper_fails_open_loudly_and_the_census_sees_it(self):
        """THE ONE FAILURE THE WRAPPER CANNOT ANNOUNCE ITSELF. With the ladder
        in a file, a deleted file means the shell exits 127 before any line of
        it runs — non-zero and not 2, which the harness reads as ALLOW, but in
        the shell's words and not helm's. So the census MEASURES the file."""
        d = os.path.join(self.tmp, "no-wrapper")
        os.makedirs(d)
        stub = os.path.join(d, "helm")
        with open(stub, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(stub, 0o755)
        cmd = hooks.spec_command(
            next(s for s in hooks.SPECS if s["name"] == "stop-guard"),
            executable=stub)
        p = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 2,
                            "a missing wrapper BLOCKS the stop — the failure "
                            "direction inverts")
        self.assertNotEqual(p.returncode, 0,
                            "a missing wrapper is indistinguishable from a "
                            "clean allow, which is the silence this whole "
                            "ladder exists to end")
        self.assertIs(hooks._wrapper_ok(cmd), False,
                      "`helm hooks status` cannot see a missing wrapper")
        # THE CONTROL: with the wrapper present the same command runs clean and
        # the census says so, so the False above is about the FILE.
        shutil.copy2(WRAPPER, os.path.join(d, hooks.HOOK_WRAPPER))
        self.assertIs(hooks._wrapper_ok(cmd), True)
        self.assertEqual(
            subprocess.run(["sh", "-c", cmd],
                           capture_output=True).returncode, 0)


class TheCensusIsTheOnlyVOICEForAMissingLadderTest(unittest.TestCase):
    """ONE MEASUREMENT, EVERY SURFACE THAT REPORTS ON A CONFIG.

    A missing `bin/helm-hook` is a state no hook can announce — the shell
    exits 127 before any line of the ladder runs — so the only reader that can
    report it is one that MEASURES the file. The first cut of that measurement
    had exactly ONE reader (`helm hooks status`), and over the very same
    planted home `doctor` answered `inject coverage: 1 of 1 claude homes` with
    no mention of the ladder, while the seat rows carried no `wrapper` key at
    all — so an estate whose every hook exited 127 read as fully wired.

    These arms plant that state in a temp HOME and ask each surface directly.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-wrapcensus-")
        self.bin = os.path.join(self.tmp, "checkout", "bin")
        os.makedirs(self.bin)
        self.helm = os.path.join(self.bin, "helm")
        with open(self.helm, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(self.helm, 0o755)
        self.cfg = os.path.join(self.tmp, "home")
        os.makedirs(self.cfg)
        self.specs = [s for s in hooks.SPECS if not s.get("external")]
        settings = {"hooks": {}}
        for spec in self.specs:
            leaf = {"type": "command",
                    "command": hooks.spec_command(spec, executable=self.helm)}
            group = {"hooks": [leaf]}
            if spec["matcher"]:
                group["matcher"] = spec["matcher"]
            settings["hooks"].setdefault(spec["event"], []).append(group)
        with open(os.path.join(self.cfg, "settings.json"), "w") as f:
            json.dump(settings, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _wrapper(self, present):
        path = os.path.join(self.bin, hooks.HOOK_WRAPPER)
        if present:
            shutil.copy2(WRAPPER, path)
        elif os.path.exists(path):
            os.remove(path)

    def _row(self):
        return hooks._gap_row("planted", self.cfg, hooks.SPECS)

    def test_the_gap_row_carries_the_ladder_so_every_surface_inherits_it(self):  # noqa: VACUOUS_ASSERTION — the False and the True are read from the SAME planted config one line apart, so the restored-ladder read is the unconditional positive control on this exact observable
        """`_gap_row` is the ONE read both `home_gap_rows` and
        `seat_status_rows` derive from — the lane's own rollout writes the
        wrapper command into seat config dirs too, and neither surface
        measured the file. Putting it here is what makes that one change."""
        self._wrapper(False)
        self.assertIs(self._row()["wrapper"], False,
                      "the row cannot see a ladder that is not on disk")
        self._wrapper(True)                      # CONTROL on the same config
        self.assertIs(self._row()["wrapper"], True)
        # AND THE TWO CONSUMERS REALLY DO CARRY IT, by name: a row shape is
        # only a fix if the surfaces that render it inherit the key. Both
        # discovery walks are replaced by the planted config, so this arm
        # never reads the real estate.
        with mock.patch.object(hooks, "claude_homes",
                               return_value=[("planted", self.cfg)]), \
                mock.patch.object(hooks, "seat_homes",
                                  return_value=([("planted", self.cfg)], [])):
            for producer in (hooks.home_gap_rows, hooks.seat_status_rows):
                out = producer()
                rows = out[0] if isinstance(out, tuple) else out
                self.assertTrue(rows, "MUST-HIT: %s saw no config"
                                % producer.__name__)
                self.assertTrue(all("wrapper" in r for r in rows),
                                "%s drops the ladder measurement"
                                % producer.__name__)

    def test_an_inline_vintage_names_no_ladder_and_is_not_a_gap(self):  # noqa: VACUOUS_ASSERTION — the None IS the contract (an inline vintage names no wrapper), and the final unconditional assertIs(..., True) proves the same helper answers about a command that names one
        """None, not False. Every rendering helm installed before the wrapper
        carries no `helm-hook` at all, and reading "absent" as "broken" would
        turn the whole installed estate into a false alarm on the day this
        landed."""
        old = hooks._gate_command_v1(
            next(s for s in hooks.SPECS if s["name"] == "stop-guard"),
            self.helm)
        self.assertIsNotNone(old, "MUST-HIT: the frozen renderer produced none")
        self.assertIsNone(hooks._wrapper_state([old]))
        self.assertIsNone(hooks._wrapper_state([]))
        # CONTROL: the same helper DOES answer about a command that names one.
        self._wrapper(True)
        self.assertIs(hooks._wrapper_state(
            [hooks.spec_command(self.specs[0], executable=self.helm)]), True)

    def test_a_home_whose_ladder_is_gone_is_not_counted_as_covered(self):
        """`inject_covered` counted presence, resolvability and the fail-open
        contract — every one of which a home with no ladder still satisfies."""
        self._wrapper(True)
        rows = [self._row()]
        rows[0].update(hook=True, resolvable=True, fail_open=True, drifted=[])
        self.assertEqual(hooks.inject_covered(rows), 1)   # CONTROL
        rows[0]["wrapper"] = False
        self.assertEqual(hooks.inject_covered(rows), 0,
                         "a home whose every hook exits 127 read as covered")

    def test_the_two_counters_cannot_disagree_about_one_ladderless_config(self):
        """ONE SCREEN, TWO NUMBERS, OPPOSITE ANSWERS. `inject_covered`
        subtracted a ladder-less config and `covered_count` did not, so
        `hooks status` printed "seat hooks: 2 of 2 seats" directly under its
        own line naming those two seats as running nothing — and
        `seat_coverage`, which is the number a consumer takes WITHOUT the
        sentence beside it, said the seats were covered.

        A hand-built row carrying no `wrapper` key at all is a third answer
        and must stay untouched: every caller that never measured the file
        would otherwise start reading as a gap."""
        self._wrapper(True)
        covered = self._row()
        covered.update(missing=[], stale=[])
        self.assertEqual(hooks.covered_count([covered]), 1)      # CONTROL
        self.assertEqual(hooks.inject_covered(
            [dict(covered, hook=True, resolvable=True, fail_open=True,
                  drifted=[])]), 1)

        gone = dict(covered, wrapper=False)
        self.assertEqual(hooks.covered_count([gone]), 0,
                         "a seat that can fire no hook at all counted as "
                         "carrying the full contract")
        self.assertEqual(
            hooks.covered_count([gone]),
            hooks.inject_covered([dict(gone, hook=True, resolvable=True,
                                       fail_open=True, drifted=[])]),
            "the two counters still disagree about the same config")

        unmeasured = dict(covered)
        unmeasured.pop("wrapper")
        self.assertEqual(hooks.covered_count([unmeasured]), 1,
                         "a row that never measured the ladder now reads as "
                         "a gap, so every unmeasured caller became an alarm")

    def test_both_doctor_rungs_say_it_and_name_the_repair(self):  # noqa: ORPHANED_MOCK — the doubles are the INPUTS of doctor.check_inject_coverage and doctor.check_guard_contract, which this arm calls directly; the walker enters from hooks.wrapper_gone_message and so cannot see the cross-module consumers that actually read them
        """`helm hooks status` is hand-run; doctor is what a fleet reads. The
        planted home answered `[OK] inject coverage: 1 of 1 claude homes` with
        both rungs silent about the ladder."""
        from helm import doctor
        self._wrapper(False)
        rows = [self._row()]
        for kind in ("claude home", "seat"):
            msg = hooks.wrapper_gone_message(kind, rows)
            self.assertIn("planted", msg)
            self.assertIn(hooks.HOOK_WRAPPER, msg)
            self.assertIn("helm hooks install", msg,
                          "the line names no repair verb")
        with mock.patch.object(hooks, "status_rows", return_value=[
                dict(rows[0], home="planted", hook=True, resolvable=True,
                     fail_open=True, drifted=[])]):
            out = doctor.check_inject_coverage()
        self.assertTrue(any(hooks.HOOK_WRAPPER in m for _lvl, m in out),
                        "the inject rung is silent about a missing ladder: %r"
                        % (out,))
        self.assertTrue(any(lvl == doctor.WARN and "1 of 1" not in m
                            for lvl, m in out), out)
        with mock.patch.object(hooks, "seat_gap_rows",
                               return_value=([dict(rows[0], seat="planted")], [])), \
                mock.patch.object(hooks, "home_gap_rows", return_value=rows), \
                mock.patch.object(hooks, "unresolved_externals", return_value=[]):
            out = doctor.check_guard_contract()
        said = [m for lvl, m in out if hooks.HOOK_WRAPPER in m]
        self.assertEqual(len(said), 2,
                         "the guard-contract rung must say it for the SEAT "
                         "surface and the HOME surface: %r" % (out,))
        # CONTROL on the whole arm: with the ladder restored both rungs go
        # quiet about it, so the rows above are about the FILE.
        self._wrapper(True)
        rows = [self._row()]
        self.assertIsNone(hooks.wrapper_gone_message("claude home", rows))
        with mock.patch.object(hooks, "seat_gap_rows",
                               return_value=([dict(rows[0], seat="planted")], [])), \
                mock.patch.object(hooks, "home_gap_rows", return_value=rows), \
                mock.patch.object(hooks, "unresolved_externals", return_value=[]):
            out = doctor.check_guard_contract()
        self.assertEqual([m for _lvl, m in out if hooks.HOOK_WRAPPER in m], [])


class TheOldInlineLadderIsStaleTest(unittest.TestCase):
    """An estate full of inline ladders must UPDATE, never duplicate.

    `_own_hit` anchors a phrase marker at the executed word's FIRST ARGUMENT.
    The wrapper stands in front of that word, so without the `_PRELUDE` row
    that steps over it helm would read every hook it ever installed as foreign
    and append a canonical copy beside each one — two Stop gates, on every
    credential home in the estate.
    """

    def _spec(self, name):
        return next(s for s in hooks.SPECS if s["name"] == name)

    def test_an_installed_inline_ladder_reads_as_ours_and_is_replaced(self):
        for name, frozen in (("stop-guard", hooks._gate_command_v1),
                             ("inject", hooks._advisory_command_v2),
                             ("inject", hooks._advisory_command_v1)):
            spec = self._spec(name)
            old = frozen(spec, hooks.helm_bin())
            with self.subTest(name=name, vintage=frozen.__name__):
                self.assertIsNotNone(old, "MUST-HIT: the frozen renderer "
                                          "produces nothing for this spec")
                self.assertTrue(
                    any(hooks._own_hit(old, tok) for tok in spec["own"]),
                    "helm no longer recognises a ladder it wrote")
                settings = {"hooks": {spec["event"]: [
                    {"hooks": [{"type": "command", "command": old}]}]}}
                if spec["matcher"]:
                    settings["hooks"][spec["event"]][0]["matcher"] = \
                        spec["matcher"]
                self.assertEqual(hooks._merge_event(settings, spec), "update",
                                 "an inline ladder read as absent, so the "
                                 "canonical entry was ADDED beside it")
                leaves = [h for g in settings["hooks"][spec["event"]]
                          for h in g["hooks"]]
                self.assertEqual(len(leaves), 1, leaves)
                self.assertEqual(leaves[0]["command"], hooks.spec_command(spec))

    def test_every_vintage_helm_has_written_is_IN_the_history(self):
        """OWNERSHIP READS THE TUPLE, NOT THE RENDERERS BY NAME. The arm above
        calls `_gate_command_v1` directly, so it stays green if that renderer
        is dropped from `HISTORICAL_COMMANDS` — and the two readers that decide
        whether an INSTALLED wrapper is helm's (`cred._retired_guard_command`
        and `posttool.recognize`) both reconstruct from the TUPLE. A vintage
        left out of it reads as foreign: retirement refuses to remove helm's
        own writing, and the installed-pair planner refuses to convert it.
        Nothing breaks loudly; the estate just keeps wrappers helm believes it
        has replaced.

        DRIVEN FROM THE SPEC SET, so a kind added later with no frozen renderer
        is red here rather than at the next retirement."""
        from helm import posttool, record
        specs = (list(hooks.SPECS)
                 + [record.deployed_spec(e) for e in record.HOOK_EVENTS]
                 + [posttool.descriptor()])
        self.assertGreaterEqual(len(specs), 12, "the spec population vanished")
        for spec in specs:
            with self.subTest(name=spec["name"]):
                rendered = [c for c in (h(spec, hooks.helm_bin())
                                        for h in hooks.HISTORICAL_COMMANDS)
                            if c is not None]
                self.assertTrue(
                    rendered,
                    "%s has NO historical rendering, so an installed wrapper "
                    "for it reads as foreign to retirement and to the "
                    "installed-pair planner" % spec["name"])

    def test_the_new_command_is_owned_by_its_own_marker_and_no_others(self):
        for spec in hooks.SPECS:
            cmd = hooks.spec_command(spec)
            with self.subTest(name=spec["name"]):
                self.assertTrue(any(hooks._own_hit(cmd, t)
                                    for t in spec["own"]), cmd)
                # THE MIRROR: the wrapper is not a licence to claim a SIBLING's
                # entry. Ownership authorises a rewrite, so a marker that
                # claimed every wrapper invocation would let one spec overwrite
                # another's hook.
                for other in hooks.SPECS:
                    if other["name"] == spec["name"]:
                        continue
                    for tok in other["own"]:
                        if any(tok == t for t in spec["own"]):
                            continue
                        self.assertFalse(hooks._own_hit(cmd, tok),
                                         "%s claims %s's entry"
                                         % (other["name"], spec["name"]))


class TheTraceIsBufferedTest(unittest.TestCase):
    """Twenty-eight lines on a run that was never in danger, and zero now."""

    # The owner's own ladder, from the paste that opened this lane.
    OWNER_LADDER_S = 9.572

    def setUp(self):
        self.prior = os.environ.get(seats_stop_timing.STREAM_AFTER_ENV)
        os.environ.pop(seats_stop_timing.STREAM_AFTER_ENV, None)
        seats_stop_timing.reset()

    def tearDown(self):
        if self.prior is None:
            os.environ.pop(seats_stop_timing.STREAM_AFTER_ENV, None)
        else:
            os.environ[seats_stop_timing.STREAM_AFTER_ENV] = self.prior
        seats_stop_timing.reset()

    def test_the_threshold_outlives_the_run_the_owner_pasted(self):
        """DERIVED FROM THE LADDER'S OWN RESERVE, not a round number: earlier
        costs the owner lines on runs that were always going to finish, later
        risks the axe landing on an unflushed buffer."""
        from helm import seats_stop_budget
        after = seats_stop_timing.stream_after()
        self.assertGreater(
            after, self.OWNER_LADDER_S,
            "the 9.572s ladder the owner pasted would still print its trace")
        self.assertLess(after, seats_stop_budget.BUDGET_S,
                        "the threshold is at or past the ladder's own reserve, "
                        "so a doomed run flushes nothing")
        self.assertEqual(after, seats_stop_timing.STREAM_AFTER_FRACTION
                         * seats_stop_budget.BUDGET_S)

    def test_the_trace_state_is_one_cell_that_reset_mutates_never_rebinds(self):
        """THE HOLDER ONLY HELPS IF IT IS NEVER REBOUND.

        The five values here were five module globals, and every mover
        declared `global` and rebound one — the single write `seats.py`'s
        facade fanout cannot see, because `global X; X = v` in a sibling is a
        STORE_GLOBAL against this module's `__dict__` and never reaches the
        facade's `__setattr__`. tests/test_seats_split_contract.py asks that
        of every sibling structurally; this asks the behaviour the shape buys,
        which a `_STATE = _Trace()` inside `reset` would satisfy structurally
        and break here: a reference taken before the reset would stop tracking
        the live ladder.
        """
        held = seats_stop_timing._STATE
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            seats_stop_timing._emit("DONE rung elapsed=0.1s total=0.1s")
        self.assertTrue(held.buffer, "MUST-HIT: nothing was buffered at all")
        self.assertIsNotNone(held.origin)

        seats_stop_timing.reset()

        self.assertIs(seats_stop_timing._STATE, held,
                      "the holder was REBOUND, which is the five globals "
                      "again wearing one name")
        self.assertEqual(held.buffer, [],
                         "the cell a caller already holds did not take the "
                         "reset")
        self.assertIsNone(held.origin)

    def test_a_healthy_ladder_prints_nothing_and_keeps_everything(self):  # noqa: VACUOUS_ASSERTION — the 28-line buffer read one line below is the unconditional positive control on the SAME emitter: the lines were produced and retained, so the empty stderr is suppression rather than an emitter that never ran
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            for i in range(28):
                seats_stop_timing._emit("DONE rung%d elapsed=0.1s total=0.1s" % i)
        self.assertEqual(out.getvalue(), "",
                         "the trace spoke on a ladder that was never in danger")
        self.assertEqual(len(seats_stop_timing._STATE.buffer), 28,
                         "the lines were DROPPED rather than buffered, so a "
                         "doomed run would have nothing to flush")

    def test_crossing_the_threshold_flushes_the_buffer_and_then_streams(self):
        os.environ[seats_stop_timing.STREAM_AFTER_ENV] = "0.25"
        seats_stop_timing.reset()
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            seats_stop_timing._emit("BEGIN identity total=0.000s")
            seats_stop_timing._emit("DONE identity elapsed=0.0s total=0.0s")
            self.assertEqual(out.getvalue(), "")      # control: still quiet
            time.sleep(0.35)
            seats_stop_timing._emit("BEGIN claims total=0.4s")
            first = out.getvalue()
            seats_stop_timing._emit("DONE claims elapsed=0.1s total=0.5s")
            second = out.getvalue()
        self.assertIn("BEGIN identity", first,
                      "the buffered lines were lost, so the flush names only "
                      "the rung that happened to cross")
        self.assertIn("DONE identity", first)
        self.assertIn("BEGIN claims", first)
        self.assertIn("DONE claims", second[len(first):],
                      "the trace did not keep streaming after the crossing")

    def test_a_rung_that_stalls_silently_still_leaves_its_evidence(self):
        """THE CASE THE EMIT-POINT CHECK CANNOT SEE. A rung that hangs for the
        whole budget emits nothing, so a threshold consulted only when
        something is emitted would let exactly the doomed run die with an
        unflushed buffer — the hole is widest where the evidence matters most.
        """
        os.environ[seats_stop_timing.STREAM_AFTER_ENV] = "0.25"
        seats_stop_timing.reset()
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            seats_stop_timing._emit("BEGIN identity total=0.000s")
            self.assertEqual(out.getvalue(), "")      # control: still quiet
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not out.getvalue():
                time.sleep(0.05)
            said = out.getvalue()
        self.assertIn("BEGIN identity", said,
                      "a ladder that stalled silently past its threshold left "
                      "no evidence at all")

    def test_a_finished_ladder_stops_its_own_threshold_timer(self):
        """A TIMER OUTLIVES THE THING IT MEASURES UNLESS SOMEONE STOPS IT.

        The flush is scheduled at the ladder's FIRST line and fires on wall
        time, so a ladder that completed in milliseconds still had one
        pending: its whole buffer landed on stderr a whole threshold later,
        inside whatever was running by then. Measured before the cure — a
        ladder that finished in 0.002s printed seven buffered rung boundaries
        after a marker line saying nothing should follow.

        `reset` was the only cancel point and it runs at the NEXT ladder's
        construction, which is too late by exactly the interval that matters.
        """
        os.environ[seats_stop_timing.STREAM_AFTER_ENV] = "0.25"
        # THE POSITIVE CONTROL IS THE SAME LADDER WITHOUT ITS TERMINAL, on the
        # same observable: an UNFINISHED ladder's buffer DOES arrive after the
        # same wait, which is the whole reason the flush is scheduled at all.
        # Without this half, "nothing arrived" is satisfied by a timer that was
        # never armed, and the arm would pass over a deleted instrument.
        seats_stop_timing.reset()
        unfinished = io.StringIO()
        with contextlib.redirect_stderr(unfinished):
            seats_stop_timing.RungTiming()
            self.assertIsNotNone(seats_stop_timing._STATE.timer,
                                 "MUST-HIT: no threshold timer was armed")
            self.assertTrue(seats_stop_timing._STATE.buffer,
                            "MUST-HIT: nothing was buffered to flush")
            time.sleep(0.45)
        self.assertIn("BEGIN identity", unfinished.getvalue(),
                      "the trace never speaks at all, so the silence below "
                      "proves nothing about the cancel")

        seats_stop_timing.reset()
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            ladder = seats_stop_timing.RungTiming()
            ladder.finish("identity")
            time.sleep(0.45)                    # past the threshold, twice
        self.assertEqual(out.getvalue(), "",
                         "a ladder that ENDED dumped its buffered trace into "
                         "whatever ran next")

    def test_settle_states_the_window_it_closes_over(self):
        """THE TRADE IS CHOSEN, SO IT IS WRITTEN DOWN.

        Disarming at the terminal costs one case main had: a ladder that
        FINISHES fast and then blocks in PUBLICATION until the outer timeout
        kills it prints no trace at all, where the unbuffered trace streamed
        the whole thing. It is narrow — under `stopprobe.SLOW` no durable row
        exists either — and it is the price of buffering. Unwritten, the next
        reader cannot tell a deliberate trade from an oversight, and the
        module docstring's "a ladder that FINISHES has no such hole" reads as
        a claim that this case does not exist.

        BOTH HALVES, UNCONDITIONALLY: a docstring that names only the finish
        or only the block describes a case that is not the gap."""
        doc = " ".join((seats_stop_timing.settle.__doc__ or "").split()).lower()
        self.assertIn("disarm", doc,
                      "MUST-HIT: this is not settle's docstring at all")
        self.assertIn("finishes", doc,
                      "settle's docstring does not say the ladder FINISHES, "
                      "which is the half that makes the window narrow")
        self.assertIn("publication", doc,
                      "settle's docstring does not say where the run then "
                      "blocks, so the window it closes over is unstated")

    def test_the_OFF_value_silences_the_stream_at_every_elapsed_time(self):
        """THE SETTING `0` CANNOT EXPRESS. `0` asks for MORE stream; this is
        the other end of the same knob, and the LINES ARE STILL KEPT — only
        the time-keyed stream stops, so the end-of-ladder report is unchanged.
        """
        # THE POSITIVE CONTROL FIRST, same emitter, same observable: at `0`
        # this very line reaches stderr, so the silence below is suppression
        # and not an emitter that never ran.
        os.environ[seats_stop_timing.STREAM_AFTER_ENV] = "0"
        seats_stop_timing.reset()
        loud = io.StringIO()
        with contextlib.redirect_stderr(loud):
            seats_stop_timing._emit("BEGIN identity total=0.000s")
        self.assertIn("BEGIN identity", loud.getvalue(),
                      "MUST-HIT: the emitter never spoke at all")
        for raw in ("off", "OFF", " off "):
            with self.subTest(raw=raw):
                os.environ[seats_stop_timing.STREAM_AFTER_ENV] = raw
                seats_stop_timing.reset()
                out = io.StringIO()
                with contextlib.redirect_stderr(out):
                    seats_stop_timing._emit("BEGIN identity total=0.000s")
                    time.sleep(0.05)
                    seats_stop_timing._emit("DONE identity elapsed=0.0s")
                self.assertEqual(out.getvalue(), "",
                                 "%r: streamed %r" % (raw, out.getvalue()))
                self.assertEqual(len(seats_stop_timing._STATE.buffer), 2,
                                 "%r: the lines were DROPPED rather than "
                                 "buffered" % raw)

    def test_the_OFF_state_arms_no_flush_timer_to_fire_into_a_later_arm(self):  # noqa: VACUOUS_ASSERTION — the control block at the head of this method is unconditional and on the same emitter: the SAME unfinished ladder under a positive threshold does reach stderr after the same wait, so the emptiness here is the disarmed timer and not a silent emitter
        """THE TIMER IS THE HALF THAT CROSSES ARM BOUNDARIES. It fires on wall
        time into whatever `sys.stderr` names THEN, so a ladder one arm leaves
        unfinished flushes inside whichever later arm holds a redirect. An off
        state that only suppressed the emit-point check would still speak,
        later, somewhere else."""
        # THE CONTROL IS THE SAME UNFINISHED LADDER UNDER A POSITIVE
        # THRESHOLD, on its own capture: its buffer DOES arrive after the same
        # wait, which is the leak this state exists to close. Without it,
        # "nothing arrived" is satisfied by a timer that was never armed and
        # by an emitter that never ran.
        os.environ[seats_stop_timing.STREAM_AFTER_ENV] = "0.25"
        seats_stop_timing.reset()
        armed = io.StringIO()
        with contextlib.redirect_stderr(armed):
            seats_stop_timing.RungTiming()      # unfinished, on purpose
            self.assertIsNotNone(seats_stop_timing._STATE.timer,
                                 "MUST-HIT: a positive threshold armed no "
                                 "timer at all")
            time.sleep(0.45)
        self.assertIn("BEGIN identity", armed.getvalue(),
                      "MUST-HIT: an unfinished ladder past its threshold left "
                      "nothing on stderr, so the silence below proves nothing")
        seats_stop_timing.reset()               # cancel the control's timer

        os.environ[seats_stop_timing.STREAM_AFTER_ENV] = "off"
        seats_stop_timing.reset()
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            seats_stop_timing.RungTiming()      # unfinished, on purpose
            self.assertIsNone(seats_stop_timing._STATE.timer,
                              "a flush is scheduled, so this ladder can still "
                              "speak into whatever runs next")
            self.assertTrue(seats_stop_timing._STATE.buffer,
                            "nothing was buffered, so this ladder had no "
                            "trace to leak in the first place")
            time.sleep(0.45)                    # past the control's threshold
        self.assertEqual(out.getvalue(), "",
                         "the unfinished ladder spoke anyway")

    def test_OFF_is_decided_BEFORE_the_seconds_parser_and_not_by_it(self):  # noqa: VACUOUS_ASSERTION — the malformed value's assertEqual against the computed fraction is the unconditional positive control on the same resolver: stream_after DOES return a number for an unreadable value, so NEVER here is a decision and not an unresolved knob
        """A MALFORMED VALUE MEANS THE DEFAULT STANDS — `threshold_from_env`'s
        whole contract, and it is unchanged. So `off` can never be routed
        through that parser: a parser whose answer to everything it cannot
        read is the default cannot also carry a second meaning, and `off`
        would silently become the fraction."""
        from helm import seats_stop_budget
        os.environ[seats_stop_timing.STREAM_AFTER_ENV] = "not-a-number"
        seats_stop_timing.reset()
        self.assertEqual(seats_stop_timing.stream_after(),
                         seats_stop_timing.STREAM_AFTER_FRACTION
                         * seats_stop_budget.BUDGET_S)
        os.environ[seats_stop_timing.STREAM_AFTER_ENV] = "off"
        seats_stop_timing.reset()
        self.assertIs(seats_stop_timing.stream_after(),
                      seats_stop_timing.NEVER,
                      "off resolved to a NUMBER, which is the fraction "
                      "wearing the name of an off switch")

    def test_the_knob_streams_everything_and_a_malformed_one_is_ignored(self):
        for raw, quiet in (("0", False), ("not-a-number", True), ("-1", True)):
            with self.subTest(raw=raw):
                os.environ[seats_stop_timing.STREAM_AFTER_ENV] = raw
                seats_stop_timing.reset()
                out = io.StringIO()
                with contextlib.redirect_stderr(out):
                    seats_stop_timing._emit("BEGIN identity total=0.000s")
                self.assertEqual(out.getvalue() == "", quiet,
                                 "%r: streamed=%r" % (raw, out.getvalue()))



class TheHarnessDeclaresTheTraceOffTest(unittest.TestCase):
    """THE DECLARATION LIVES AT THE ONE DOOR BOTH RUNNERS LOAD.

    `tests/__init__.py`, not `tests/_tmphome.py`: the package __init__ is
    imported before any test in the package under unittest discovery AND under
    pytest, while _tmphome is imported by a minority of the modules — and the
    arm this protects is in whichever module happens to hold a
    `redirect_stderr` when a stray flush fires, which is not a set anyone
    enumerates.
    """

    def setUp(self):
        seats_stop_timing.reset()
        self.addCleanup(seats_stop_timing.reset)

    def test_this_process_resolves_the_trace_to_the_off_state(self):
        self.assertEqual(os.environ.get(seats_stop_timing.STREAM_AFTER_ENV),
                         seats_stop_timing.STREAM_OFF,
                         "the package door did not declare the value")
        self.assertIs(seats_stop_timing.stream_after(),
                      seats_stop_timing.NEVER,
                      "the value is exported but the module resolves it to a "
                      "threshold, so ladders still stream")

    def test_a_child_process_inherits_the_declaration(self):
        """THE DECLARATION IS IN `os.environ`, WHICH IS WHAT A CHILD COPIES,
        so an arm that drives a real `helm` through subprocess gets a quiet
        ladder without knowing the rule exists."""
        child = subprocess.run(
            (sys.executable, "-c",
             "import os;print(os.environ.get(%r, '<unset>'))"
             % seats_stop_timing.STREAM_AFTER_ENV),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(),
                         seats_stop_timing.STREAM_OFF)


if __name__ == "__main__":
    unittest.main()
