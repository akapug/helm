#!/usr/bin/env python3
"""The built/wired/exercised ladder — and why it is code, not another premise.

THE RULE ALREADY EXISTED. `feature-and-rsh-must-both-be-wired` sits in the
store at confidence 1.00 and says, in the owner's own words, that unwired
features are THE reason the vibe-coding world hits 90%-done-never-100%.
`closed-is-the-full-loop` says done is a loop, not an arrow. Both were canon,
both were live, and on 2026-07-29 the owner asked "i wonder how we can /learn
to actual wire things when we build them" — which is the right question to ask
about a rule that has been true and ignored for a month.

A rule an agent must REMEMBER to apply is enforced by vigilance, and vigilance
does not scale past the surfaces someone happens to look at. So the lesson
became a graph instead of a sentence.

IT FOUND SOMETHING ON ITS FIRST REAL RUN, which is the argument in one line:
`helm/board.py` — "the integration board's ONE write path", built two days
earlier specifically to replace the ad-hoc `json.dump(board, open(p, "w"))`
that had already produced a lost update — had no verb, no importer, and no
caller. Its own docstring said `add_landed()` is the writer that prevents
hand-editing, while every writer was still hand-editing. No amount of care
would have surfaced that; one import graph did, in 40ms.

WHAT THIS SUITE MOSTLY PINS IS THE CENSUS NOT LYING. A first draft called 31
of 83 modules unreachable — including `doctor`, `web` and `landreq`, all run by
hand minutes earlier — because helm dispatches every verb through
`_lazy("<module>", "<fn>")`, a string-keyed lazy import that no import-statement
scan can see. A census with a 37% false-positive rate is not read twice, and
its silence then means nothing. So: the false-positive cases are tested as hard
as the true-positive one.
"""
import contextlib
import io
import json
import os
import shutil
import tempfile
import textwrap
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import wiring  # noqa: E402


class FakePackage(unittest.TestCase):
    """A tiny synthetic helm/ so the tests never depend on the real package's
    current shape — a census asserted against live code would fail the day
    someone lands a module, which teaches everyone to delete the test."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-wiring-")
        # The wiring memo and its lockfile live under HELM_HOME; without this
        # redirect the gate tests write into the operator's real estate.
        self._home_prior = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = os.path.join(self.d, "helm-home")

    def tearDown(self):
        if self._home_prior is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self._home_prior
        shutil.rmtree(self.d, ignore_errors=True)

    def mod(self, name, body=""):
        with open(os.path.join(self.d, name + ".py"), "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(body))

    def census(self):
        return wiring.census(root=self.d, with_events=False)


class ReachabilityTest(FakePackage):
    def test_a_module_nothing_imports_is_UNREACHABLE(self):
        """The silent case this exists for: it compiles, it has tests, and no
        execution path on earth reaches it."""
        self.mod("cli", "from . import used\n")
        self.mod("used")
        self.mod("orphan")
        self.assertEqual(self.census()["unreachable"], ["orphan"])

    def test_STRING_KEYED_DISPATCH_counts_as_wired(self):
        """helm's entire verb surface is `_lazy("mod", "fn")`. Missing it made
        the first draft report 31 of 83 modules dead, every one of them live."""
        self.mod("cli", '''
            def _lazy(module, fn):
                pass
            VERBS = {"drain": _lazy("drain", "cmd_drain")}
        ''')
        self.mod("drain")
        self.assertEqual(self.census()["unreachable"], [])

    def test_a_literal_import_module_counts_as_wired(self):
        self.mod("cli", '''
            import importlib
            def go():
                return importlib.import_module("helm.web")
        ''')
        self.mod("web")
        self.assertEqual(self.census()["unreachable"], [])

    def test_an_import_INSIDE_A_FUNCTION_counts(self):
        """helm defers imports constantly to keep CLI startup cheap. A
        top-level-only scan would call half the package dead."""
        self.mod("cli", '''
            def go():
                from . import late
        ''')
        self.mod("late")
        self.assertEqual(self.census()["unreachable"], [])

    def test_A_DEAD_CLUSTER_CANNOT_VOUCH_FOR_ITSELF(self):
        """Why this is REACHABILITY and not 'has an importer'. Two dead modules
        importing each other satisfy has-an-importer unanimously and are still
        dead. This is the case a simpler check gets confidently wrong."""
        self.mod("cli")
        self.mod("ghost_a", "from . import ghost_b\n")
        self.mod("ghost_b", "from . import ghost_a\n")
        self.assertEqual(self.census()["unreachable"], ["ghost_a", "ghost_b"])

    def test_a_module_named_only_in_a_DOCSTRING_is_not_wired(self):
        """The false-NEGATIVE guard. A regex scan counts a module named in
        prose, which would let a comment mentioning a dead module resurrect
        it — the census lying in the reassuring direction."""
        self.mod("cli", '"""We should really call orphan one day."""\n')
        self.mod("orphan")
        self.assertEqual(self.census()["unreachable"], ["orphan"])

    def test_ALLOWED_exempts_but_the_reason_is_the_price(self):
        self.mod("cli")
        self.mod("weird")
        with mock.patch.dict(wiring.ALLOWED, {"weird": "reached via a plugin"}):
            self.assertEqual(self.census()["unreachable"], [])

    def test_an_unparseable_module_does_not_crash_the_census(self):
        """A syntax error mid-edit must not take out the guard that runs on
        every stop."""
        self.mod("cli", "from . import ok\n")
        self.mod("ok")
        self.mod("broken", "def (:\n")
        self.assertIn("broken", self.census()["unreachable"])


class ActuatorCensusTest(unittest.TestCase):
    """Installed evidence, not a source-tree promise, owns EXTERNAL wiring."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-actuators-")
        self.hooks = os.path.join(self.tmp, "hooks")
        self.units = os.path.join(self.tmp, "systemd")
        os.makedirs(self.hooks)
        os.makedirs(self.units)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, path, body, mode=0o644):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(body))
        os.chmod(path, mode)

    def scan(self, **kw):
        return wiring.actuator_census(
            hook_dir=kw.pop("hook_dir", self.hooks), unit_dir=self.units,
            crontab=kw.pop("crontab", ""), settings_paths=kw.pop(
                "settings_paths", []), **kw)

    def test_UNINSTALLED_pre_push_is_named_while_pre_commit_is_proven(self):
        """MUST-HIT + MUST-NOT-HIT in one estate. This is the live defect:
        ALLOWED claimed both guards were wired while only one hook existed."""
        self.write(os.path.join(self.hooks, "pre-commit"), '''
            #!/bin/sh
            vacuous=/opt/helm/helm/vacuous_assertion.py
            python3 "$vacuous" --staged || true
            scanner=/opt/helm/helm/nevertrack.py
            exec python3 "$scanner" --staged
        ''', 0o755)
        got = self.scan()
        self.assertNotIn("never-track-pre-commit", got["missing"])
        self.assertNotIn("vacuous-assertion-pre-commit", got["missing"])
        self.assertIn("hostpath-pre-push", got["missing"])

    def test_the_owner_decision_flush_is_a_DECLARED_actuator(self):
        """task/232's retry edge, and it exists because I claimed a schedule I
        never measured. I wrote that flush_unreached "rides an EXISTING
        PERIODIC PASS ... already runs on a schedule"; @offbox-claude's
        base-interaction refute measured this box and found ZERO beacons
        timers, ZERO cron rows, ZERO hook references. The edge was in code and
        fired never — task/232's own dependency-on-noticing premise, un-cured
        one layer down at deployment.

        DECLARED here rather than detected by a new checker: check_actuator_wiring
        already asks exactly this question, so the cure is to make the census
        SEE the obligation."""
        # THE CASE: an estate with no beacons consumer at all reports it.
        got = self.scan()
        self.assertIn("owner-decision-flush", got["missing"])
        # AN INSTALLED-BUT-DISABLED TIMER IS STILL MISSING, which is the state
        # this box was actually in: beacons.ensure_timer exists and nobody ran
        # it. A unit file on disk is not a schedule.
        self.write(os.path.join(self.units, "helm-beacons.service"), """
            [Service]
            ExecStart=%h/.local/bin/helm beacons --post
        """)
        timer = os.path.join(self.units, "helm-beacons.timer")
        self.write(timer, """
            [Timer]
            OnUnitActiveSec=300
        """)
        self.assertIn("owner-decision-flush", self.scan()["missing"])
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: ENABLE it and it clears, so
        # "missing" above is the census discriminating rather than a name it
        # could never satisfy.
        wants = os.path.join(self.units, "timers.target.wants")
        os.makedirs(wants, exist_ok=True)
        os.symlink(timer, os.path.join(wants, "helm-beacons.timer"))
        again = self.scan()
        self.assertNotIn("owner-decision-flush", again["missing"])
        self.assertIn("owner-decision-flush", again["wired"])

    def test_inert_vacuity_tokens_cannot_impersonate_the_invocation(self):
        self.write(os.path.join(self.hooks, "pre-commit"), '''
            #!/bin/sh
            vacuous=/opt/helm/helm/vacuous_assertion.py
            scanner=/opt/helm/helm/nevertrack.py
            exec python3 "$scanner" --staged
        ''', 0o755)
        got = self.scan()
        self.assertIn("vacuous-assertion-pre-commit", got["missing"])
        self.assertNotIn("never-track-pre-commit", got["missing"])

    def test_a_comment_cannot_impersonate_an_actuator(self):
        self.write(os.path.join(self.hooks, "pre-push"), '''
            #!/bin/sh
            # exec python3 /opt/helm/helm/hostpath_guard.py --pre-push
            exit 0 # hostpath_guard.py --pre-push
        ''', 0o755)
        self.assertIn("hostpath-pre-push", self.scan()["missing"])

    def test_a_token_prefix_lookalike_is_not_the_declared_action(self):
        self.write(os.path.join(self.hooks, "pre-push"), '''
            #!/bin/sh
            exec python3 /opt/not-hostpath_guard.py --pre-push-later
        ''', 0o755)
        self.assertIn("hostpath-pre-push", self.scan()["missing"])

    def test_the_right_command_in_the_wrong_hook_does_not_count(self):
        self.write(os.path.join(self.hooks, "post-checkout"), '''
            #!/bin/sh
            exec python3 /opt/helm/helm/hostpath_guard.py --pre-push
        ''', 0o755)
        self.assertIn("hostpath-pre-push", self.scan()["missing"])

    def test_disabled_timer_is_missing_then_enabled_timer_proves_the_service(self):
        self.write(os.path.join(self.units, "helm-corpus.service"), '''
            [Service]
            ExecStart=%h/.local/bin/helm corpus backup
        ''')
        timer = os.path.join(self.units, "helm-corpus.timer")
        self.write(timer, '''
            [Timer]
            OnCalendar=daily
        ''')
        self.assertIn("corpus-backup", self.scan()["missing"])
        wants = os.path.join(self.units, "timers.target.wants")
        os.makedirs(wants)
        os.symlink(timer, os.path.join(wants, "helm-corpus.timer"))
        self.assertNotIn("corpus-backup", self.scan()["missing"])

    def test_comments_and_disabled_crontab_lines_do_not_satisfy_schedule(self):
        self.write(os.path.join(self.units, "helm-gc.service"), '''
            [Service]
            # `helm work gc --apply` is deliberately not this service.
            ExecStart=%h/.local/bin/helm gc --apply
        ''')
        self.assertIn("worktree-gc", self.scan(
            crontab="# * * * * * helm work gc --apply\n")["missing"])

    def test_active_crontab_and_json_consumers_are_real_surfaces(self):
        settings = os.path.join(self.tmp, "settings.json")
        self.write(settings, json.dumps({
            "hooks": {"Stop": [{"hooks": [
                {"type": "command", "command": "helm dispatch mix"}
            ]}]},
            "mcpServers": {"maintenance": {
                "command": "helm", "args": ["work", "gc", "--apply"]
            }},
        }))
        got = self.scan(
            crontab="7 * * * * helm corpus backup\n",
            settings_paths=[settings])
        self.assertNotIn("corpus-backup", got["missing"])
        self.assertNotIn("worktree-gc", got["missing"])
        self.assertNotIn("dispatch-mix", got["missing"])

    def test_a_swallowed_or_unreachable_conflict_invocation_is_not_wired(self):
        """A REFUSING rung's invocation must sit ON the refusal path. Token
        presence blessed `python3 "$conflict" --staged || true` (exit
        swallowed — it can never refuse) and `exit 0` above the true line
        (never reached). The positive control proves the real shape wires."""
        hook = os.path.join(self.hooks, "pre-commit")
        self.write(hook, '''
            #!/bin/sh
            conflict=/opt/helm/helm/conflict_marker.py
            python3 "$conflict" --staged || true
        ''', 0o755)
        self.assertIn("conflict-marker-pre-commit", self.scan()["missing"])
        self.write(hook, '''
            #!/bin/sh
            conflict=/opt/helm/helm/conflict_marker.py
            exit 0
            python3 "$conflict" --staged || exit $?
        ''', 0o755)
        self.assertIn("conflict-marker-pre-commit", self.scan()["missing"])
        self.write(hook, '''
            #!/bin/sh
            conflict=/opt/helm/helm/conflict_marker.py
            exit 0; python3 "$conflict" --staged || exit $?
        ''', 0o755)
        self.assertIn("conflict-marker-pre-commit", self.scan()["missing"])
        # the MUST-HIT: the composed template's exact shape, conditional
        # skip and all — a census that also refuses the REAL hook is dead
        self.write(hook, '''
            #!/bin/sh
            conflict=/opt/helm/helm/conflict_marker.py
            if [ "$HELM_CONFLICT_MARKER_SKIP" != "1" ]; then
              python3 "$conflict" --staged || exit $?
            fi
        ''', 0o755)
        got = self.scan()
        self.assertNotIn("conflict-marker-pre-commit", got["missing"])
        self.assertIn("conflict-marker-pre-commit", got["wired"])

    def test_hook_estate_absent_is_MISSING_unreadable_is_UNKNOWN(self):
        """Tri-state: an ABSENT hook dir honestly has no hooks (MISSING); a
        dir that cannot be READ was never measured (UNKNOWN, never missing —
        isdir/exists swallow EACCES into 'absent')."""
        got = self.scan(hook_dir=os.path.join(self.tmp, "no-such-dir"))
        self.assertIn("conflict-marker-pre-commit", got["missing"])
        self.assertNotIn("conflict-marker-pre-commit", got["unknown"])
        locked = os.path.join(self.tmp, "locked")
        os.makedirs(os.path.join(locked, "hooks"))
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        got = self.scan(hook_dir=os.path.join(locked, "hooks"))
        self.assertIn("conflict-marker-pre-commit", got["unknown"])
        self.assertIn("never-track-pre-commit", got["unknown"])
        self.assertNotIn("conflict-marker-pre-commit", got["missing"])
        self.assertNotIn("never-track-pre-commit", got["missing"])

    def test_dispatch_rebind_is_a_declared_obligation_not_a_quiet_verb(self):  # noqa: VACUOUS_ASSERTION — the loop iterates a literal 4-tuple (never empty) and the unconditional positive control (assertNotIn on the runnable shape) precedes it
        """codex FIX on 6ccc7347: rebind existed, only the CLI could call it,
        and the census never said so — quiet by OMISSION. The obligation row
        makes the missing scheduler MEASURED (MUST-HIT in an empty estate); a
        consumer that can really invoke the verb discharges it; a sibling
        dispatch verb cannot impersonate it; and — codex blocker 2 on
        6a8f9530 — a consumer whose command argparse REFUSES (no row id, no
        --to: exit 2 before the verb runs) is a sensor wired to nothing and
        must NOT discharge."""
        self.assertIn("dispatch-rebind", self.scan()["missing"])
        settings = os.path.join(self.tmp, "settings.json")
        self.write(settings, json.dumps({
            "hooks": {"Stop": [{"hooks": [
                {"type": "command", "command":
                 'helm dispatch rebind "$HELM_STALL_ROW"'
                 ' --to "$HELM_STALL_TO" --json'}
            ]}]},
        }))
        self.assertNotIn("dispatch-rebind",
                         self.scan(settings_paths=[settings])["missing"])
        # SIMPLE COMMAND ONLY (codex r6 closure root): six rounds proved that
        # judging ANY richer shell — segments, reachable lines, lexical
        # trackers — reduces case by case toward a reachability engine, and
        # every partial model over-credited somewhere (heredoc bodies,
        # continuations, quoted strings, uncalled function bodies, dead code
        # behind exit/exec/false&&). The contract: ONE line, ONE simple
        # command; ANY control operator, compound/function syntax, or
        # cross-line construct refuses outright. Trailing redirections are
        # the one syntax consumed. Under-credit is safe; false WIRED never.
        runnable = 'abcdef12 --to ds4pro --json'
        self.write(settings, json.dumps({
            "hooks": {"Stop": [{"hooks": [
                {"type": "command", "command":
                 "helm dispatch rebind " + runnable + " > /tmp/log 2>&1"}
            ]}]},
        }))
        self.assertNotIn("dispatch-rebind",
                         self.scan(settings_paths=[settings])["missing"],
                         "trailing redirections are syntax, not a refusal")
        for operator_bearing in (
                'helm dispatch rebind "$ROW" --to ds4pro --json || exit 1',
                "cd /x && helm dispatch rebind " + runnable,
                "helm dispatch rebind " + runnable + " ; true",
                "helm dispatch rebind abcdef12 --to ds4pro >"):
            self.write(settings, json.dumps({
                "hooks": {"Stop": [{"hooks": [
                    {"type": "command", "command": operator_bearing}
                ]}]},
            }))
            self.assertIn("dispatch-rebind",
                          self.scan(settings_paths=[settings])["missing"],
                          operator_bearing)
        for inert_multi in ("cat <<EOF\nhelm dispatch rebind " + runnable
                            + "\nEOF",
                            "printf x \\\nhelm dispatch rebind " + runnable,
                            "printf 'begin\nhelm dispatch rebind "
                            + runnable + "\nend'"):
            self.write(settings, json.dumps({
                "hooks": {"Stop": [{"hooks": [
                    {"type": "command", "command": inert_multi}
                ]}]},
            }))
            self.assertIn("dispatch-rebind",
                          self.scan(settings_paths=[settings])["missing"],
                          inert_multi)
        self.write(settings, json.dumps({
            "hooks": {"Stop": [{"hooks": [
                {"type": "command", "command":
                 "wrap() {\nhelm dispatch rebind " + runnable + "\n}"}
            ]}]},
        }))
        self.assertIn("dispatch-rebind",
                      self.scan(settings_paths=[settings])["missing"],
                      "an uncalled function definition's body is data")
        for multi_even_with_a_real_invocation in (
                "cat <<EOF\ninert body\nEOF\nhelm dispatch rebind "
                + runnable,
                "helm dispatch rebind " + runnable + " <<NOTE\n"
                "inert body\nNOTE"):
            self.write(settings, json.dumps({
                "hooks": {"Stop": [{"hooks": [
                    {"type": "command",
                     "command": multi_even_with_a_real_invocation}
                ]}]},
            }))
            self.assertIn("dispatch-rebind",
                          self.scan(settings_paths=[settings])["missing"],
                          "multi-line refuses OUTRIGHT — even around a real "
                          "invocation; under-credit is the safe direction")
        # COMMAND POSITION (codex r4, hermetic fake-helm proof): inert text
        # mentioning the verb is not an invocation — echo/printf bodies and
        # NAME="…" assignments census MISSING; wrappers, assignments-then-
        # command, path-spelled helm, and a second command segment all
        # census WIRED.
        for inert in ("echo helm dispatch rebind " + runnable,
                      'printf "helm dispatch rebind %s"' % runnable,
                      'CMD="helm dispatch rebind %s"' % runnable):
            self.write(settings, json.dumps({
                "hooks": {"Stop": [{"hooks": [
                    {"type": "command", "command": inert}
                ]}]},
            }))
            self.assertIn("dispatch-rebind",
                          self.scan(settings_paths=[settings])["missing"],
                          inert)
        # wrapper GRAMMAR, not a greedy strip (codex r7, /bin/sh rc127
        # proofs): assignments never follow exec/command, wrappers never
        # chain — shell refuses these; the census must too.
        for invalid_wrapper in ("exec FOO=1 helm dispatch rebind " + runnable,
                                "command FOO=1 helm dispatch rebind "
                                + runnable,
                                "env exec helm dispatch rebind " + runnable,
                                "env command helm dispatch rebind "
                                + runnable):
            self.write(settings, json.dumps({
                "hooks": {"Stop": [{"hooks": [
                    {"type": "command", "command": invalid_wrapper}
                ]}]},
            }))
            self.assertIn("dispatch-rebind",
                          self.scan(settings_paths=[settings])["missing"],
                          invalid_wrapper)
        for invokes in ("exec helm dispatch rebind " + runnable,
                        "env FOO=1 helm dispatch rebind " + runnable,
                        "FOO=1 ./bin/helm dispatch rebind " + runnable):
            self.write(settings, json.dumps({
                "hooks": {"Stop": [{"hooks": [
                    {"type": "command", "command": invokes}
                ]}]},
            }))
            self.assertNotIn("dispatch-rebind",
                             self.scan(settings_paths=[settings])["missing"],
                             invokes)
        got = self.scan(crontab="*/5 * * * * helm dispatch rebind "
                                + runnable + "\n")
        self.assertNotIn("dispatch-rebind", got["missing"],
                         "cron schedule fields are grammar, not argv")
        # TERMINAL PASS, three kind-specific routes (codex, meld
        # e:1785593989 — the set is CLOSED at these):
        # cron: an unescaped % ends the command; the suffix is stdin.
        got = self.scan(crontab="*/5 * * * * X=% helm dispatch rebind "
                                + runnable + "\n")
        self.assertIn("dispatch-rebind", got["missing"],
                      "cron percent ends the command — helm is stdin text")
        got = self.scan(crontab="*/5 * * * * helm dispatch rebind abcdef12"
                                " --to ds4pro --reason x\\%y --json\n")
        self.assertNotIn("dispatch-rebind", got["missing"],
                         "an ESCAPED percent stays literal inside a value")
        # systemd @: the second token is argv0, consumed by the executable.
        unit = os.path.join(self.units, "at.service")
        self.write(unit, "[Service]\nExecStart=@/usr/local/bin/helm "
                         "dispatch rebind " + runnable + "\n")
        wants = os.path.join(self.units, "default.target.wants")
        os.makedirs(wants, exist_ok=True)
        link = os.path.join(wants, "at.service")
        if not os.path.exists(link):
            os.symlink(unit, link)
        self.assertIn("dispatch-rebind", self.scan()["missing"],
                      "under @, 'dispatch' becomes argv0 and helm never "
                      "sees the verb")
        self.write(unit, "[Service]\nExecStart=@/usr/local/bin/helm helm "
                         "dispatch rebind " + runnable + "\n")
        self.assertNotIn("dispatch-rebind", self.scan()["missing"],
                         "an explicit argv0 token restores the real argv")
        # prefix chars combine in ANY order — `-@` must still find the @
        self.write(unit, "[Service]\nExecStart=-@/usr/local/bin/helm "
                         "dispatch rebind " + runnable + "\n")
        self.assertIn("dispatch-rebind", self.scan()["missing"],
                      "-@ still shifts argv0 — startswith missed it")
        self.write(unit, "[Service]\nExecStart=-@/usr/local/bin/helm helm "
                         "dispatch rebind " + runnable + "\n")
        self.assertNotIn("dispatch-rebind", self.scan()["missing"],
                         "-@ with explicit argv0 is a real invocation")
        os.unlink(link)
        os.unlink(unit)
        # MCP: the args contract is string[]; a coerced non-string row is
        # rejected whole, never repaired into valid-looking argv.
        self.write(settings, json.dumps({
            "mcpServers": {"bad": {"command": "helm",
                                   "args": ["dispatch", "rebind", 12345678,
                                            "--to", "ds4pro", "--json"]}},
        }))
        self.assertIn("dispatch-rebind",
                      self.scan(settings_paths=[settings])["missing"],
                      "a non-string MCP arg breaks the contract — the row "
                      "is rejected whole")
        got = self.scan(crontab="*/5 * * * * echo helm dispatch rebind "
                                + runnable + "\n")
        self.assertIn("dispatch-rebind", got["missing"])
        # every arm below measured rc2 against the live CLI before being
        # pinned (meld e:1785584307 — the four grammar shapes are codex's)
        for unusable in ("helm dispatch rebind --json",
                         "helm dispatch rebind abc123 --json",
                         "helm dispatch rebind --to ds4pro --json",
                         "helm dispatch rebind --reason repair --to ds4pro"
                         " --json",
                         "helm dispatch rebind --repo /tmp/r --to ds4pro"
                         " --json",
                         "helm dispatch rebind --to ds4pro || true",
                         "helm dispatch rebind --to one --to two --json",
                         "helm dispatch rebind abcdef12 --to ds4pro --bogus",
                         "helm dispatch rebind abcdef12 --to ds4pro --reason",
                         "helm dispatch rebind --reason one --reason two"
                         " --to ds4pro --json",
                         "helm dispatch mix"):
            self.write(settings, json.dumps({
                "hooks": {"Stop": [{"hooks": [
                    {"type": "command", "command": unusable}
                ]}]},
            }))
            self.assertIn("dispatch-rebind",
                          self.scan(settings_paths=[settings])["missing"],
                          unusable)

    def test_a_redirection_is_syntax_of_the_command_not_a_separator(self):
        """codex r5 (gate 80f95d7e): the splitter broke segments at any
        operator CHARACTER, so in `echo > helm …` the `>` handed helm a
        fresh segment and COMMAND POSITION — when the shell makes helm the
        FILENAME echo writes to. A redirection operator and its target are
        consumed as syntax of the CURRENT command: helm-as-target censuses
        MISSING, trailing redirections keep a real invocation WIRED, and a
        dangling operator (no target word) is a shell syntax error that
        invalidates its whole segment."""
        settings = os.path.join(self.tmp, "settings.json")
        self.write(settings, json.dumps({
            "hooks": {"Stop": [{"hooks": [
                {"type": "command", "command":
                 "echo > helm dispatch rebind abcdef12 --to ds4pro --json"}
            ]}]},
        }))
        self.assertIn("dispatch-rebind",
                      self.scan(settings_paths=[settings])["missing"],
                      "helm as echo's target filename censused as a consumer")
        self.write(settings, json.dumps({
            "hooks": {"Stop": [{"hooks": [
                {"type": "command", "command":
                 'helm dispatch rebind "$ROW" --to ds4pro --json'
                 ' > /tmp/log 2>&1'}
            ]}]},
        }))
        got = self.scan(settings_paths=[settings])
        self.assertNotIn("dispatch-rebind", got["missing"],
                         "trailing redirections are syntax, not a veto")
        self.assertIn("dispatch-rebind", got["wired"])
        self.write(settings, json.dumps({
            "hooks": {"Stop": [{"hooks": [
                {"type": "command", "command":
                 "helm dispatch rebind abcdef12 --to ds4pro >"}
            ]}]},
        }))
        self.assertIn("dispatch-rebind",
                      self.scan(settings_paths=[settings])["missing"],
                      "a dangling redirection can never run, so it can "
                      "never consume")

    def test_mcp_consumers_are_judged_as_structured_argv_not_shell(self):
        """An mcpServers entry IS argv — [command, *args] execed directly,
        no interpreter in between. Flattened to text it read as shell
        (codex r5): `{command: "echo", args: [">", "helm", …]}` censused
        WIRED for a verb the server would only ever print. argv[0] must be
        helm in command position; there is no shell, so nothing unwraps (an
        `env` command stays env — env-as-executable parsing is deliberately
        unimplemented, conservative under-credit) and nothing redirects."""
        settings = os.path.join(self.tmp, "settings.json")
        self.write(settings, json.dumps({
            "mcpServers": {"rebinder": {
                "command": "echo",
                "args": [">", "helm", "dispatch", "rebind", "abcdef12",
                         "--to", "ds4pro"]}},
        }))
        self.assertIn("dispatch-rebind",
                      self.scan(settings_paths=[settings])["missing"],
                      "echo's argument list censused as a helm invocation")
        self.write(settings, json.dumps({
            "mcpServers": {"rebinder": {
                "command": "env",
                "args": ["helm", "dispatch", "rebind", "abcdef12",
                         "--to", "ds4pro", "--json"]}},
        }))
        self.assertIn("dispatch-rebind",
                      self.scan(settings_paths=[settings])["missing"],
                      "mcp has no shell: an env command is not unwrapped")
        self.write(settings, json.dumps({
            "mcpServers": {"rebinder": {
                "command": "helm",
                "args": ["dispatch", "rebind", "abcdef12", "--to", "ds4pro",
                         "--json"]}},
        }))
        got = self.scan(settings_paths=[settings])
        self.assertNotIn("dispatch-rebind", got["missing"],
                         "structured helm argv is a real consumer")
        self.assertIn("dispatch-rebind", got["wired"])

    def test_systemd_ExecStart_is_direct_exec_argv_with_no_shell(self):
        """systemd execs ExecStart's first token itself — no shell ever
        runs, so a literal `&&` reaches echo as an ARGUMENT and chains
        nothing (codex r5: the shell reading handed helm command position
        inside a unit that would only ever echo). The executable token —
        after systemd's @-+! prefix chars — must itself be helm."""
        service = os.path.join(self.units, "helm-rebind.service")
        self.write(service, '''
            [Service]
            ExecStart=/bin/echo && helm dispatch rebind abcdef12 --to ds4pro
        ''')
        wants = os.path.join(self.units, "default.target.wants")
        os.makedirs(wants)
        os.symlink(service, os.path.join(wants, "helm-rebind.service"))
        self.assertIn("dispatch-rebind", self.scan()["missing"],
                      "an argv-literal && censused as a shell chain")
        self.write(service, '''
            [Service]
            ExecStart=/usr/local/bin/helm dispatch rebind abcdef12 --to ds4pro --json
        ''')
        got = self.scan()
        self.assertNotIn("dispatch-rebind", got["missing"],
                         "a direct-exec helm unit is a real consumer")
        self.assertIn("dispatch-rebind", got["wired"])

    def test_systemd_mutually_exclusive_privilege_prefixes_are_refused(self):  # noqa: VACUOUS_ASSERTION — fixed invalid-prefix loop plus unconditional wired control
        """systemd refuses `+` with `!` or `!!` before ExecStart can run.

        The census used to strip the whole prefix run and credit the remaining
        argv, reporting WIRED for a unit systemd-analyze marks fatal. Refuse
        that whole object while preserving each privilege mode on its own."""
        service = os.path.join(self.units, "helm-rebind.service")
        wants = os.path.join(self.units, "default.target.wants")
        os.makedirs(wants)
        os.symlink(service, os.path.join(wants, "helm-rebind.service"))
        command = ("/usr/local/bin/helm dispatch rebind abcdef12 "
                   "--to ds4pro --json")
        for prefixes in ("+!", "!+", "+!!", "!!+"):
            self.write(service, "[Service]\nExecStart=%s%s\n"
                       % (prefixes, command))
            self.assertIn("dispatch-rebind", self.scan()["missing"],
                          "%s is fatal to systemd, not a consumer" % prefixes)
        for prefix in ("+", "!", "!!"):
            self.write(service, "[Service]\nExecStart=%s%s\n"
                       % (prefix, command))
            got = self.scan()
            self.assertNotIn("dispatch-rebind", got["missing"],
                             "%s alone is a valid privilege mode" % prefix)
        self.write(service, "[Service]\nExecStart=+%s\n" % command)
        self.assertIn("dispatch-rebind", self.scan()["wired"],
                      "the refusal arm must not pass by rejecting every prefix")

    def test_every_nonintrinsic_ALLOWED_entry_has_a_checkable_obligation(self):
        got = self.scan(obligations={})
        self.assertEqual(got["invalid_allowed"],
                         # inflight_gate JOINS this class for the same reason
                         # as the other five: the pre-commit hook snapshots it
                         # and runs it as a subprocess, so no import reaches
                         # it. A REAL member, re-pinned deliberately — unlike
                         # a phantom entry, which would be fiction in a list
                         # that exists to be exhaustive.
                         ["conflict_marker", "hardcode", "hostpath_guard",
                          "inflight_gate", "lane_discipline", "nevertrack",
                          "vacuous_assertion"])

    def test_owner_report_folds_all_missing_actuators_into_one_class(self):
        got = self.scan()
        lines = wiring.actuator_report_lines(got)
        misses = [line for line in lines if "NO ACTUATOR" in line]
        self.assertEqual(len(misses), 1)
        self.assertIn("hostpath-pre-push", misses[0])
        self.assertIn("worktree-gc", misses[0])

    def test_cli_surface_names_missing_actuators(self):
        got = {"consumers": 0, "wired": {},
               "missing": ["hostpath-pre-push"],
               "unknown": [], "invalid_allowed": []}
        out = io.StringIO()
        with mock.patch.object(wiring, "actuator_census", return_value=got), \
                contextlib.redirect_stdout(out):
            rc = wiring.cmd_wiring([])
        self.assertEqual(rc, 1)
        self.assertIn("NO ACTUATOR", out.getvalue())
        self.assertIn("hostpath-pre-push", out.getvalue())


class GateTest(FakePackage):
    """The Stop-hook rung — scoped to what THIS tree added."""

    def _gate(self, added, unreachable):
        with mock.patch.object(wiring, "added_modules", return_value=added), \
                mock.patch.object(wiring, "census",
                                  return_value={"unreachable": unreachable}):
            return wiring.gate_lines(root=self.d, repo=self.d)

    def test_it_fires_on_a_module_THIS_TREE_added_and_left_dead(self):
        lines = self._gate(["orphan"], ["orphan"])
        self.assertTrue(lines)
        self.assertIn("orphan", "\n".join(lines))

    def test_it_stays_SILENT_on_pre_existing_debt(self):
        """A guard that blocks every seat on debt none of them created is a
        guard that gets switched off within the day. Scope is what keeps it
        alive long enough to matter."""
        self.assertEqual(self._gate([], ["someone_elses_orphan"]), [])

    def test_a_module_that_was_added_AND_wired_does_not_fire(self):
        self.assertEqual(self._gate(["fresh"], []), [])

    def test_it_IGNORES_pre_existing_debt_even_while_this_tree_added_something(self):
        """The case that actually pins the intersection, found by a mutation
        that did NOT bite. The two tests above are both satisfied by an early
        `if not added: return []`, so replacing `added & dead` with plain
        `dead` passed them unanimously — the scope guard was resting on a
        short-circuit and nothing exercised the line that does the work.

        Here the tree adds a WIRED module while the repo already carries an
        unrelated orphan. Correct: silent. Unscoped: it blocks this seat for
        someone else's debt, which is the failure that gets a guard disabled."""
        self.assertEqual(self._gate(["fresh_and_wired"], ["someone_elses_orphan"]),
                         [])

    def test_git_that_cannot_be_asked_leaves_the_gate_OPEN(self):
        """Tri-state, and it resolves toward open on purpose: this runs in a
        Stop hook, and a guard that blocks when it cannot measure would wedge
        every session on a host with no git."""
        with mock.patch.object(wiring, "_git", return_value=None):
            mods, note = wiring.unwired_additions(root=self.d, repo=self.d)
        self.assertEqual(mods, [])
        self.assertIn("could not be asked", note)

    def test_ANY_exception_leaves_the_gate_open_and_silent(self):
        """FAIL-OPEN TOTAL is the Stop hook's law — a hook that raises wedges
        every session on this host, which is strictly worse than the bug."""
        with mock.patch.object(wiring, "unwired_additions",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(wiring.gate_lines(root=self.d, repo=self.d), [])


class AddedModulesTest(unittest.TestCase):
    def test_only_helm_package_python_files_count(self):
        out = "helm/new.py\ntests/test_new.py\ndocs/NEW.md\nbin/helm\n"
        with mock.patch.object(wiring, "_git", return_value=out):
            self.assertEqual(wiring.added_modules("/repo"), ["new"])


    def test_a_new_SUBPACKAGE_is_seen(self):
        """The census names `helm/pkg/__init__.py` as module `pkg`, so mapping
        the path through the file-stem rule produced `__init__` and the two
        never intersected — every new subpackage was silently exempt. helm has
        six of them, so this is an ordinary shape, and it failed toward
        SILENCE, the direction nobody notices. Found by cross-review."""
        out = "helm/zzpkg/__init__.py\n"
        with mock.patch.object(wiring, "_git", return_value=out):
            self.assertEqual(wiring.added_modules("/repo"), ["zzpkg"])

    def test_a_plain_module_still_maps_to_its_stem(self):
        with mock.patch.object(wiring, "_git", return_value="helm/new.py\n"):
            self.assertEqual(wiring.added_modules("/repo"), ["new"])

    def test_a_new_SUBMODULE_is_named_the_way_the_census_names_it(self):
        """The third time this seam has failed toward silence, and the reason
        it keeps failing is that two functions each held their own opinion
        about what a file is called.

        `helm/pkg/new.py` was keyed by its bare stem `new` while the census had
        just started calling it `pkg.new`. The Stop gate intersects the two, so
        a newly added UNWIRED submodule evaded the guard entirely — which is
        exactly the escape the guard exists to close."""
        with mock.patch.object(wiring, "_git", return_value="helm/pkg/new.py\n"):
            self.assertEqual(wiring.added_modules("/repo"), ["pkg.new"])

    def test_the_gate_and_the_census_agree_on_every_shape(self):
        """The invariant behind all three failures: one resolver, one answer.
        A file's name at the WRITE path must equal its name in the graph."""
        mods = wiring.modules()
        root = os.path.dirname(wiring.__file__)
        for name, path in mods.items():
            with self.subTest(module=name):
                self.assertEqual(wiring.node_name(os.path.relpath(path, root)),
                                 name, "the resolver disagrees with modules()")
        # ...and the write path uses the same function on a git-shaped line.
        self.assertEqual(wiring.node_name("clarity/rules.py"), "clarity.rules")
        self.assertIsNone(wiring.node_name("NOTES.md"),
                          "a non-module must resolve to nothing")

    def test_no_git_answer_is_None_not_empty(self):
        """'nothing was added' and 'I could not look' must not render the
        same — the same law the rest of this module is about."""
        with mock.patch.object(wiring, "_git", return_value=None):
            self.assertIsNone(wiring.added_modules("/repo"))


class UnwiredMemoTest(unittest.TestCase):
    """The stop-path memo: the wiring census must not be paid on every stop,
    and must never answer stale.

    Owner ground truth 2026-07-31: 'still timing out, 3rd time ive mentioned
    this morning' — a module-adding lane in flight made every Stop hook pay
    ~1s of ast-walking (2.6s measured against the 5s ceiling). The memo keys
    on the added-set AND the source-graph content fingerprint: the census
    runs only when EITHER changes. codex-3's repro is the load-bearing case:
    an existing-file edit that unwires the same added module with NO
    added-set change must recompute — the first memo keyed on filenames
    alone returned the stale clean answer, which is the one lie this rung
    can tell.
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="helm-test-unwired-memo-")
        self._home = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")

    def tearDown(self):
        if self._home is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self._home
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _repo(self):
        """A scratch repo whose helm/ package mirrors the real one's shape:
        an __init__ plus modules, so the census graph is NONEMPTY — an empty
        graph makes every memo answer vacuously agree."""
        import subprocess
        repo = os.path.join(self.tmp, "proj")
        subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
        subprocess.run(["git", "-C", repo, "config", "user.email", "t@t"],
                       check=True)
        subprocess.run(["git", "-C", repo, "config", "user.name", "t"],
                       check=True)
        pkg = os.path.join(repo, "helm")
        os.makedirs(pkg)
        with open(os.path.join(pkg, "__init__.py"), "w") as f:
            f.write("")
        with open(os.path.join(pkg, "wiredmod.py"), "w") as f:
            f.write("from . import probe_unused  # noqa\n")
        with open(os.path.join(pkg, "probe_unused.py"), "w") as f:
            f.write("X = 1\n")
        with open(os.path.join(pkg, "__main__.py"), "w") as f:
            f.write("from . import wiredmod  # noqa\n")
        subprocess.run(["git", "-C", repo, "add", "."], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-qm", "base"], check=True)
        subprocess.run(["git", "-C", repo, "branch", "origin/main"], check=True)
        return repo

    def _add_probe(self, repo, wire=True):
        """Commit an added module, optionally WIRED through an existing file
        (the codex-3 repro: wired->unwired with NO added-set change)."""
        import subprocess
        pkg = os.path.join(repo, "helm")
        with open(os.path.join(pkg, "probe_new.py"), "w") as f:
            f.write("Y = 2\n")
        if wire:
            with open(os.path.join(pkg, "__main__.py"), "a") as f:
                f.write("from . import probe_new  # noqa\n")
        subprocess.run(["git", "-C", repo, "add", "."], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-qm", "add probe"],
                       check=True)

    def _unwire_probe(self, repo):
        import subprocess
        main = os.path.join(repo, "helm", "__main__.py")
        with open(main) as f:
            body = f.read()
        with open(main, "w") as f:
            f.write(body.replace("from . import probe_new  # noqa\n", ""))
        subprocess.run(["git", "-C", repo, "add", "."], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-qm", "unwire"],
                       check=True)

    def _pkg(self, repo):
        return os.path.join(repo, "helm")

    def test_a_wired_added_module_is_clean_then_UNWIRING_recomputes_to_a_find(self):
        """THE PRIMARY BLOCKER, codex-3's no-mock repro as a test. Same
        added-set, same repo/base: an existing-file import removed. The
        memo must recompute and FIND the module, never answer stale-clean."""
        repo = self._repo()
        self._add_probe(repo, wire=True)
        d1, _ = wiring.unwired_additions(root=self._pkg(repo), repo=repo)
        self.assertEqual(d1, [], "a wired added module reads clean first")
        self._unwire_probe(repo)
        d2, _ = wiring.unwired_additions(root=self._pkg(repo), repo=repo)
        self.assertIn("probe_new", d2,
                      "an existing-file edit left the memo stale-clean")

    def test_a_CHANGED_added_set_with_the_SAME_graph_recomputes(self):
        """The added-set arm alone: graph untouched, a NEW module added.
        Deleting added-set from the key would read the old memo's clean
        answer over the new module."""
        from unittest import mock
        repo = self._repo()
        self._add_probe(repo, wire=False)
        calls = []
        real = wiring.census
        def spy(*a, **kw):
            calls.append(1)
            return real(*a, **kw)
        with mock.patch.object(wiring, "census", spy):
            wiring.unwired_additions(root=self._pkg(repo), repo=repo)
            # same graph, DIFFERENT added-set: add a second module
            import subprocess
            with open(os.path.join(repo, "helm", "probe_two.py"), "w") as f:
                f.write("Z = 3\n")
            subprocess.run(["git", "-C", repo, "add", "."], check=True)
            subprocess.run(["git", "-C", repo, "commit", "-qm", "add two"],
                           check=True)
            wiring.unwired_additions(root=self._pkg(repo), repo=repo)
        self.assertEqual(len(calls), 2,
                         "a changed added-set read the stale memo")

    def test_the_second_stop_with_identical_inputs_does_not_recompute(self):
        """MEMO HIT arm — unchanged added-set AND unchanged graph: the
        census is not called twice. Mutation: drop the memo lookup -> the
        spy count grows -> this dies."""
        from unittest import mock
        repo = self._repo()
        self._add_probe(repo, wire=False)
        calls = []
        real = wiring.census
        def spy(*a, **kw):
            calls.append(1)
            return real(*a, **kw)
        with mock.patch.object(wiring, "census", spy):
            d1, _ = wiring.unwired_additions(root=self._pkg(repo), repo=repo)
            d2, _ = wiring.unwired_additions(root=self._pkg(repo), repo=repo)
        self.assertEqual(d1, d2)
        self.assertEqual(len(calls), 1,
                         "identical inputs paid the census twice")
        self.assertIn("probe_new", d1,
                      "the fixture must FIND something or the hit is vacuous")

    def test_a_CORRUPT_memo_recomputes_and_rewrites_valid(self):
        """Malformed bytes are never trusted: the rung recomputes, returns
        the SAME answer a fresh memo gives, and rewrites valid JSON."""
        repo = self._repo()
        self._add_probe(repo, wire=False)
        d1, _ = wiring.unwired_additions(root=self._pkg(repo), repo=repo)
        self.assertIn("probe_new", d1)
        path = wiring._unwired_memo_path()
        with open(path, "w") as f:
            f.write("{corrupt[")
        d2, _ = wiring.unwired_additions(root=self._pkg(repo), repo=repo)
        self.assertEqual(d1, d2, "a corrupt memo changed the answer")
        import json as _json
        with open(path, encoding="utf-8") as f:
            repaired = _json.load(f)
        self.assertEqual(repaired.get("v"), 1,
                         "the corrupt memo was never repaired")

    def test_a_source_change_DURING_the_census_writes_NOTHING(self):
        """codex-3's A->B repro, verbatim: census completes on wired A
        (clean), the import is removed (B) BEFORE it returns. Publishing
        that answer under EITHER key poisons it — A's key answers for a
        state already gone, B's for a state never measured. The strict
        rule: a measurement that straddles a state change is written
        NOWHERE, and the settled state then computes its own truth."""
        from unittest import mock
        repo = self._repo()
        self._add_probe(repo, wire=True)     # state A: wired, clean
        real_census = wiring.census
        def unwire_during_census(*a, **kw):
            out = real_census(*a, **kw)
            self._unwire_probe(repo)          # source moves to B mid-measure
            return out
        with mock.patch.object(wiring, "census", unwire_during_census):
            wiring.unwired_additions(root=self._pkg(repo), repo=repo)
        # the straddling answer was written NOWHERE
        self.assertFalse(os.path.exists(wiring._unwired_memo_path()),
                         "a straddled measurement was memoized")
        # and the settled state B now answers its OWN truth, not A's clean
        d, _ = wiring.unwired_additions(root=self._pkg(repo), repo=repo)
        self.assertIn("probe_new", d,
                      "state B inherited A's clean answer through the memo")

    def test_concurrent_first_misses_run_the_census_ONCE(self):
        """The fleet-timeout case: N stops racing the same cold key must not
        stack N census runs. The lockfile single-flights them: one computes,
        the rest read what it wrote. A census spy inside one process proves
        the count; concurrent first-misses from threads share the process,
        which is the strictest race the lock can see."""
        from unittest import mock
        import threading
        repo = self._repo()
        self._add_probe(repo, wire=False)
        calls = []
        real = wiring.census
        def spy(*a, **kw):
            calls.append(1)
            return real(*a, **kw)
        with mock.patch.object(wiring, "census", spy):
            results = []
            workers = [threading.Thread(
                target=lambda: results.append(
                    wiring.unwired_additions(root=self._pkg(repo), repo=repo)[0]))
                for _ in range(4)]
            for w in workers:
                w.start()
            for w in workers:
                w.join(10)
        self.assertEqual(len(calls), 1,
                         "concurrent first-misses ran %d censuses" % len(calls))
        self.assertTrue(all(r == results[0] for r in results))
        self.assertIn("probe_new", results[0])

    def test_the_memo_survives_ACROSS_PROCESSES(self):
        """Every real Stop is a fresh process; a module-global cache would
        pass in-process and fail in production. Subprocess miss, then
        subprocess hit, and the second must not recompute."""
        repo = self._repo()
        self._add_probe(repo, wire=False)
        env = dict(os.environ)
        script = (
            "import sys; sys.path.insert(0, %r);"
            "from helm import wiring;"
            "d,_ = wiring.unwired_additions(root=%r, repo=%r);"
            "print(','.join(d))") % (
                os.path.dirname(os.path.dirname(wiring.__file__)),
                self._pkg(repo), repo)
        import subprocess as sp
        first = sp.run(["python3", "-c", script], capture_output=True,
                       text=True, env=env)
        self.assertEqual(first.returncode, 0, first.stderr[-300:])
        # second process: memo read from disk; patch is impossible across
        # processes, so the proof is that the answer is identical and the
        # memo file names this key
        second = sp.run(["python3", "-c", script], capture_output=True,
                        text=True, env=env)
        self.assertEqual(first.stdout, second.stdout)
        import json as _json
        with open(wiring._unwired_memo_path(), encoding="utf-8") as f:
            memo = _json.load(f)
        self.assertEqual(memo["v"], 1)
        self.assertIn("probe_new", memo["dead"])

    def test_a_valid_shaped_memo_with_a_FORGED_clean_answer_recomputes(self):
        """codex-3's second probe, verbatim as a test: preserve v, key, repo,
        base, added; swap ONLY dead to []. A schema check that trusts shape
        returns the prohibited false clean. The payload digest binds the
        canonical content, so the forgery invalidates and recomputes."""
        repo = self._repo()
        self._add_probe(repo, wire=False)
        d1, _ = wiring.unwired_additions(root=self._pkg(repo), repo=repo)
        self.assertIn("probe_new", d1)
        import json as _json
        path = wiring._unwired_memo_path()
        with open(path, encoding="utf-8") as f:
            memo = _json.load(f)
        memo["dead"] = []                     # the forgery: valid shape, false clean
        with open(path, "w") as f:
            f.write(_json.dumps(memo))
        d2, _ = wiring.unwired_additions(root=self._pkg(repo), repo=repo)
        self.assertIn("probe_new", d2,
                      "a valid-shaped dead=[] forgery was trusted as clean")

    def test_a_schema_foreign_memo_recomputes(self):
        """A valid-JSON memo from another schema/version carries no
        authority — structured validation, not shape-agnostic truth."""
        repo = self._repo()
        self._add_probe(repo, wire=False)
        path = wiring._unwired_memo_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write('{"key": "anything", "dead": []}')
        d, _ = wiring.unwired_additions(root=self._pkg(repo), repo=repo)
        self.assertIn("probe_new", d,
                      "a schema-foreign memo's dead=[] was trusted")

    def test_a_WRONG_VERSION_with_the_right_key_recomputes(self):
        """The version gate alone: correct key, wrong v. Deleting the v
        check would let a future schema's stale answer read as current."""
        repo = self._repo()
        self._add_probe(repo, wire=False)
        d1, _ = wiring.unwired_additions(root=self._pkg(repo), repo=repo)
        import json as _json
        path = wiring._unwired_memo_path()
        with open(path, encoding="utf-8") as f:
            memo = _json.load(f)
        memo["v"] = 99
        with open(path, "w") as f:
            f.write(_json.dumps(memo))
        d2, _ = wiring.unwired_additions(root=self._pkg(repo), repo=repo)
        self.assertEqual(d1, d2)
        self.assertIn("probe_new", d2)


if __name__ == "__main__":
    unittest.main()


class PackageEdgesTest(unittest.TestCase):
    """A package contributes EVERY .py it contains, not just __init__.py.

    THE INCIDENT (2026-08-04, #216): `modules()` maps a package to its
    __init__.py, and `graph()` read imports from that one file. helm/clarity/
    keeps the rule table in rules.py, so `from .. import helmese` — an edge the
    clarity die walks on every owner-mode check — was invisible, and the census
    accused a wired module of being 'built, not wired'.

    A census that lies in the reassuring direction is worse than none. This one
    lied by accusing, which is the same defect wearing the opposite sign."""

    def test_a_package_submodules_edges_live_ON_THE_SUBMODULE(self):
        """The edge is real, and it belongs to the file that has it.

        An earlier version of this test asserted `helmese in g["clarity"]` —
        it encoded the rejected union model, where a package absorbs every
        file's imports. The edge must sit on clarity.rules, and reachability
        must flow through it rather than around it."""
        g = wiring.graph()
        self.assertIn("clarity", g, "clarity is not in the graph at all")
        self.assertIn("clarity.rules", g, "the submodule is not a node")
        self.assertIn("helmese", g["clarity.rules"],
                      "the submodule lost the import it actually has")
        self.assertNotIn("helmese", g["clarity"],
                         "the package absorbed its submodule's edge")
        self.assertIn("clarity.rules", g["clarity"],
                      "__init__ imports rules, so the edge must exist")

    def test_the_register_is_REACHABLE_from_the_entry_points(self):
        r = wiring.reachable(wiring.graph())
        self.assertIn("cli", r, "control: cli must be reachable")
        self.assertIn("helmese", r)

    def test_the_fix_did_not_simply_make_EVERYTHING_reachable(self):
        """The failure mode of a reachability fix is a census that now says
        yes to everything, which would be indistinguishable from a working one
        on the case that motivated it."""
        g = wiring.graph()
        reached = wiring.reachable(g)
        # MUST-HIT CONTROL: the walk actually reached things. An empty result
        # would make "every unreached module is declared" trivially true.
        self.assertGreater(len(reached), 10, "reachable() returned almost nothing")
        unreached = set(g) - reached
        for mod in unreached:
            self.assertIn(mod, wiring.ALLOWED,
                          "%s is unreachable and undeclared" % mod)
        fake = dict(g)
        fake["a-module-nothing-imports"] = set()
        self.assertNotIn("a-module-nothing-imports", wiring.reachable(fake),
                         "reachable() reports an orphan as reached")

    def test_a_DEAD_submodule_cannot_vouch_for_what_it_imports(self):
        """codex-3's negative control, verbatim from the review that caught it.

        The rejected model made a package's edges the UNION of every file it
        contains, so an UNIMPORTED submodule's imports read as reached. That
        laundering is worse than the hole it replaced: the census would then
        certify its own dead cluster."""
        tmp = tempfile.mkdtemp(prefix="helm-test-wiring-pkg-")
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "pkg"))
        for rel, body in (("cli.py", "from . import pkg\n"),
                          ("orphan.py", "x = 1\n"),
                          ("pkg/__init__.py", ""),
                          ("pkg/dead.py", "from .. import orphan\n")):
            with open(os.path.join(tmp, rel), "w") as fh:
                fh.write(body)
        g = wiring.graph(tmp)
        reached = wiring.reachable(g, entries=("cli",))
        # MUST-HIT CONTROL: the walk works on this fixture at all.
        self.assertIn("pkg", reached, "the package itself was not reached")
        self.assertNotIn("orphan", g["pkg"],
                         "a dead submodule's import landed on the package node")
        self.assertNotIn("orphan", reached,
                         "an UNIMPORTED submodule vouched for what it imports")
        self.assertNotIn("pkg.dead", reached, "nothing imports pkg.dead")
        # ...and the edge still EXISTS, on the node that actually has it.
        self.assertIn("orphan", g["pkg.dead"])

    def test_an_IMPORTED_submodule_does_carry_its_edges(self):
        """The other direction: the fix must not simply drop submodule edges,
        which would re-open the hole it was written to close."""
        tmp = tempfile.mkdtemp(prefix="helm-test-wiring-pkg2-")
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "pkg"))
        for rel, body in (("cli.py", "from . import pkg\n"),
                          ("target.py", "x = 1\n"),
                          ("pkg/__init__.py", "from .live import y\n"),
                          ("pkg/live.py", "from .. import target\n")):
            with open(os.path.join(tmp, rel), "w") as fh:
                fh.write(body)
        reached = wiring.reachable(wiring.graph(tmp), entries=("cli",))
        self.assertIn("pkg.live", reached)
        self.assertIn("target", reached,
                      "an imported submodule's edge was dropped")

    def test_tested_RESOLVES_dotted_identity(self):
        """The rungs must migrate node identity TOGETHER. graph() gained
        submodule nodes while tested() still collapsed every import to its
        first component, so 36 dotted nodes read as untested at once — and
        census() publishes live-minus-tested to the owner."""
        have = wiring.tested()
        self.assertTrue(have, "no modules read as tested at all")
        self.assertIn("clarity.rules", have,
                      "a directly-imported submodule reads as untested")
        self.assertIn("clarity", have, "control: the package is still credited")

    def test_a_package_a_test_NAMES_credits_what_it_REACHES(self):
        """A test saying `from helm import store` exercises store.cli without
        ever spelling it. Crediting the package's IMPORT CLOSURE keeps that
        honest; crediting the DIRECTORY would be the vouching the reachability
        rung rejects, so a dead file earns nothing."""
        tmp = tempfile.mkdtemp(prefix="helm-test-wiring-tested-")
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "pkg"))
        for rel, body in (("cli.py", "from . import pkg\n"),
                          ("pkg/__init__.py", "from .live import y\n"),
                          ("pkg/live.py", "x = 1\n"),
                          ("pkg/dead.py", "x = 1\n")):
            with open(os.path.join(tmp, rel), "w") as fh:
                fh.write(body)
        got = wiring._reached_within({"pkg"}, set(wiring.modules(tmp)), tmp)
        self.assertIn("pkg.live", got, "an imported submodule was not credited")
        self.assertNotIn("pkg.dead", got,
                         "a co-located dead file was credited by its package")

    def test_the_census_did_not_gain_false_untested_findings(self):
        """Making submodules nodes must not turn a 3-finding census into a
        24-finding one. Every submodule that landed in `untested` when the
        nodes appeared was reachable from a package a test names — all 21 of
        them — so publishing them would have been 21 false alarms."""
        c = wiring.census(with_events=False)
        self.assertGreater(c["modules"], 100, "control: the census ran")
        dotted_untested = [m for m in c["untested"] if "." in m]
        self.assertEqual(dotted_untested, [],
                         "submodules a named package reaches read as untested")

    def test_a_submodule_is_named_package_dot_module(self):
        mods = wiring.modules()
        self.assertIn("clarity.rules", mods, "the submodule is not a node")
        self.assertTrue(mods["clarity.rules"].endswith("clarity/rules.py"))
        self.assertIn("cli", mods, "control: a top-level module keeps its name")
