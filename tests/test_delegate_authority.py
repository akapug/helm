#!/usr/bin/env python3
"""A delegate may not write a verdict as its seat (task/3060, E1 + E2).

THE INCIDENT. A seat's delegated reader (an Agent-tool subagent sharing the
seat's session and HELM_CHAT_NAME) ran `helm dispatch verdict <id> <tip>
--approve` and minted an immutable APPROVE with zero findings that its brief
never authorized — the third delegate ledger write beyond a brief that day.
A second shape followed within the hour: a builder subagent ran `helm handoff
write` under its parent's session and replaced the parent's continuity file.

THE DISCRIMINATOR is `agent_id` on the PreToolUse payload: present inside a
subagent or a Workflow agent, absent in the main thread (helm/actors.py). The
argv-guard refuses a delegate's verdict-class write unless the seat's main
thread granted it (`helm delegate allow`).

ONE DELEGATE RUNG. task/1388 built a second rung from the same incident (a
subagent's dispatch or land-request ledger write). It was folded into this
one: its verbs are in delegate_grant.REFUSED, the spellings it let through
because they write nothing (usage, help, an lr dry run or census, a TEST
home) are read per invocation here, and its arms are TheFoldedLedgerRungTest
below. Some of its passes are now refusals on purpose: a HELM_HOME that
names the live home, or that the text does not settle, is not a test home,
and only the call's own prefix makes one. The shell reader does not model
shell state, so no export counts, and an option after `env` voids it.

Every arm drives the shipped hook entry (`chat.cmd_argv_guard` reading a real
payload on stdin), never a copy of its matcher, and every refusal arm is paired
with the control a wrong rung would also fail: the main thread, a delegate's
read, a delegate's `dispatch send`, and a delegate under a grant.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-delegate-", var="HELM_HOME")

from helm import chat  # noqa: E402

SESSION = "0f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0"
TIP = "5d" * 20
# TODAY'S SHAPE, verbatim in form: a delegate binding an approve to a row.
INCIDENT = ("helm dispatch verdict 0123456789ab %s --approve --measured "
            "'gate:0123456789abcdef clean read'" % TIP)
HANDOFF = "helm handoff write --session %s <<'EOF'\nRelay final report\nEOF" \
    % SESSION


def _grants():
    """The grant module, imported per call so a helm without it fails each
    arm on its own assertion instead of failing the whole module at import."""
    from helm import delegate_grant
    return delegate_grant


class GuardBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="helm-test-delegate-guard-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(tmp, "helm"),
            "HELM_CHAT_NAME": "seat-a",
            "HELM_CHAT_DIR": os.path.join(tmp, "chat"),
            "CLAUDE_CODE_SESSION_ID": SESSION})
        env.start()
        self.addCleanup(env.stop)

    def guard(self, command, agent_id="a1b2c3", tool="Bash", raw=None):
        """(rc, stdout, stderr) of the installed PreToolUse hook verb."""
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool,
                   "session_id": SESSION, "cwd": "/tmp/repo",
                   "tool_use_id": "toolu_1",
                   "tool_input": {"command": command}}
        if agent_id is not None:
            payload["agent_id"] = agent_id
            payload["agent_type"] = "general-purpose"
        out, err = io.StringIO(), io.StringIO()
        body = raw if raw is not None else json.dumps(payload)
        with mock.patch.object(sys, "stdin", io.StringIO(body)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chat.cmd_argv_guard([])
        return rc, out.getvalue(), err.getvalue()


class TodaysShapeIsRefusedTest(GuardBase):
    """E1, red first: the incident's command from a delegate exits 2."""

    def test_a_delegate_binding_an_approve_is_refused(self):  # noqa: VACUOUS_ASSERTION — the same command from the main thread is asserted to pass first, and the refusal text is asserted present
        # CONTROL FIRST: the same command from the MAIN THREAD runs.
        self.assertEqual(self.guard(INCIDENT, agent_id=None)[0], 0)
        rc, out, err = self.guard(INCIDENT)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("[helm argv-guard] BLOCKED", err)
        self.assertIn("`helm dispatch verdict`", err)
        self.assertIn("delegate", err)
        self.assertIn("Workflow run is never a seat", err)
        self.assertIn('helm delegate allow --verbs "dispatch verdict"', err)
        self.assertEqual(out, "", "a refusal rides stderr and nothing else")

    def test_a_Monitor_is_read_the_same_way(self):
        self.assertEqual(self.guard(INCIDENT, agent_id=None, tool="Monitor")[0],
                         0)
        self.assertEqual(self.guard(INCIDENT, tool="Monitor")[0], 2)

    def test_a_delegate_handoff_under_the_parents_session_is_refused(self):  # noqa: VACUOUS_ASSERTION — the parent's own handoff is asserted to pass on the same entry before the delegate's is refused
        """The second incident: a builder subagent replaced its parent's
        continuity file. The parent's own handoff still passes."""
        self.assertEqual(self.guard(HANDOFF, agent_id=None)[0], 0)
        rc, _out, err = self.guard(HANDOFF)
        self.assertEqual(rc, 2, err)
        self.assertIn("`helm handoff write`", err)


class TheRefusedTableTest(GuardBase):
    """E1: exactly the verdict-class writes, in every spelling a shell runs."""

    REFUSED = (
        "helm dispatch verdict 0123abcd %s --fix --measured x" % TIP,
        "helm dispatch retract 0123abcd --reason r --reads fix --measured",
        "helm dispatch hold 0123abcd --source-clean %s clean" % TIP,
        "helm dispatch release 0123abcd",
        "helm dispatch cancel 0123abcd moot",
        "helm dispatch rebind 0123abcd --to seat-b",
        "helm dispatch retip 0123abcd --ref %s --reason r" % TIP,
        "helm lr close 0123abcd --reason withdrawn --evidence e",
        "helm lr land 0123abcd",
        "helm lr expired --apply",
        "helm lr abandon 0123abcd --reason gone",
        "helm lr retire 0123abcd --reason author-unresolvable",
        "helm lr discharge 0123abcd %s e" % TIP,
        "helm lr withdraw 0123abcd e",
        "helm lr close-landed 0123abcd --trunk main",
        "helm store confirm some-id",
        "helm store supersede 2026-09-24 old new why",
        "helm store revise some-id a better statement",
        "helm store retire 2026-09-24 some-id why",
        "helm store reject some-id",
        "helm handoff write",
        'helm delegate allow --verbs "dispatch verdict"',
        "helm delegate revoke --all",
    )

    OPEN = (
        "helm dispatch send seat-b lane-x --ref %s --kind review --new-work"
        % TIP,
        "helm dispatch add seat-b lane-x --ref %s --kind review --new-work"
        % TIP,
        "helm dispatch list --mine",
        "helm dispatch triage 0123abcd",
        "helm dispatch list --json | grep verdict",
        "helm lr show 0123abcd",
        "helm lr list | grep close",
        "helm store get some-id",
        "helm work claim lane-x",
        "helm work release lane-x",
        "helm chat post --room r hello",
        "helm chat dm seat-b hello",
        "helm gate run --focus --plan",
        "helm handoff check",
        "helm delegate list",
        "git log --grep verdict",
    )

    def test_every_refused_verb_is_refused_to_a_delegate_only(self):  # noqa: VACUOUS_ASSERTION — each verb is asserted to PASS from the main thread before it is asserted refused
        for command in self.REFUSED:
            with self.subTest(command=command):
                self.assertEqual(self.guard(command, agent_id=None)[0], 0,
                                 "the main thread was refused")
                rc, _out, err = self.guard(command)
                self.assertEqual(rc, 2, err)
                self.assertIn("BLOCKED", err)

    def test_the_open_verbs_and_the_reads_pass_for_a_delegate(self):
        # POSITIVE CONTROL on the same entry and payload shape.
        self.assertEqual(self.guard(INCIDENT)[0], 2)
        for command in self.OPEN:
            with self.subTest(command=command):
                rc, _out, err = self.guard(command)
                self.assertEqual(rc, 0, err)

    def test_the_spellings_a_shell_assembles_are_the_verb(self):  # noqa: VACUOUS_ASSERTION — a delegate read is asserted to pass on the same entry before the spellings are refused
        self.assertEqual(self.guard("helm dispatch list --mine")[0], 0)
        for command in (
                "./bin/helm dispatch verdict 0123abcd %s --fix x" % TIP,
                "/opt/helm-wt/lane/bin/helm lr close 0123abcd --reason x",
                "python3 -m helm dispatch verdict 0123abcd %s --fix x" % TIP,
                "helm dis$(echo patch) verdict 0123abcd %s --fix x" % TIP,
                "helm dispatch ver\"$(echo dict)\" 0123abcd %s --fix x" % TIP,
                "helm store --project p confirm some-id",
                "cd /tmp && HELM_CHAT_NAME=seat-a helm lr retire x --reason y",
                "helm dispatch \\\n  verdict 0123abcd %s --fix x" % TIP):
            with self.subTest(command=command):
                self.assertEqual(self.guard(command)[0], 2)

    def test_a_garbled_payload_fails_open(self):  # noqa: VACUOUS_ASSERTION — a well-formed delegate payload is asserted REFUSED on the same entry first
        self.assertEqual(self.guard(INCIDENT)[0], 2)
        self.assertEqual(self.guard("", raw="{not json")[0], 0)
        self.assertEqual(self.guard("", raw=json.dumps(
            {"tool_name": "Bash", "agent_id": "x", "tool_input": None}))[0], 0)


class TheFoldedLedgerRungTest(GuardBase):
    """task/1388's rung, folded into this one: every write it refused is
    refused, every command it passed because it writes nothing still passes
    (a test home only as the call's own prefix),
    and what it read from the whole command is read per invocation."""

    # every ledger write task/1388 refused, in every spelling it read
    WRITES = (
        "helm dispatch verdict d-17 TIP --approve --measured gate:tok ev",
        "helm dispatch hold d-1 waiting on x",
        "helm dispatch release d-1",
        "helm dispatch cancel d-1 moot",
        "helm dispatch rebind d-1 --to s2",
        "helm dispatch retip d-1 --ref T --reason r",
        "helm lr close d-1 --reason landed",
        "helm lr land d-1",
        "helm lr retire d-1 --reason r",
        "helm lr retire --sweep --older-than 14d",
        "helm lr retire --off-frontier --apply",
        "helm lr discharge d-1 T ev",
        "helm lr withdraw d-1 ev",
        "helm lr close-landed d-1 --trunk main",
        "helm lr abandon d-1 --reason r",
        "helm lr expired --apply",
        "./bin/helm dispatch verdict d-1 T --fix e",
        "/home/x/helm/bin/helm lr close d-1",
        "~/dev/helm/bin/helm lr land d-1",
        "python3 -m helm dispatch hold d-1 r",
        "bash -c 'helm lr close d-1 --reason landed'",
        "cd /tmp && helm lr land d-1",
        "git log -1 && helm dispatch cancel d-1 r",
        "x=$(helm dispatch verdict d-1 T --fix e)",
        "evidence=$(cat <<'EOF'\nthe finding\nEOF\n)\n"
        "helm dispatch verdict d-1 T --fix \"$evidence\"",
        "case $x in a) helm dispatch verdict d-1 T;; esac",
    )

    # what task/1388 passed because it writes nothing: reads, dry runs, a
    # census without --apply, help, a verb given no arguments (its usage),
    # a literal test home, and programs that are not helm
    QUIET = (
        "helm chat read", "helm dispatch list --open --mine",
        "helm dispatch triage d-1", "helm dispatch briefs",
        "helm lr show d-1", "helm lr list", "helm lr stalls",
        "helm lr foldcheck T", "helm gate audits --repo . -- x",
        "fab test --repo . -- python3 -m unittest tests.x",
        "helm lr close d-1 --reason landed --dry-run",
        "helm lr retire --sweep --older-than 14d --dry-run",
        "helm lr retire --off-frontier", "helm lr expired",
        "helm dispatch verdict --help", "helm lr close -h",
        "./bin/helm lr close 2>&1 | head -80", "helm dispatch cancel",
        "./bin/helm dispatch verdict 2>&1 | head -20",
        "helm dispatch retract", "helm lr land --help",
        "HELM_HOME=/tmp/h helm lr close d-1 --reason landed",
        "env HELM_HOME=/tmp/sp/home ./bin/helm dispatch verdict d-1 T --fix e",
        "HELM_HOME=/tmp/s/home helm dispatch hold d-1 r",
        "kubectl-helm dispatch verdict", "helmet dispatch verdict",
    )

    def test_every_write_the_ledger_rung_refused_is_refused(self):  # noqa: VACUOUS_ASSERTION — each command is asserted to PASS from the main thread before it is asserted refused
        for command in self.WRITES:
            with self.subTest(command=command):
                self.assertEqual(self.guard(command, agent_id=None)[0], 0,
                                 "the main thread was refused")
                rc, _out, err = self.guard(command)
                self.assertEqual(rc, 2, err)
                self.assertIn("BLOCKED", err)

    def test_what_writes_nothing_passes(self):
        # POSITIVE CONTROL on the same entry and payload shape.
        self.assertEqual(self.guard(INCIDENT)[0], 2)
        for command in self.QUIET:
            with self.subTest(command=command):
                rc, _out, err = self.guard(command)
                self.assertEqual(rc, 0, err)

    def test_what_writes_nothing_is_read_per_invocation(self):  # noqa: VACUOUS_ASSERTION — each quiet spelling is asserted to PASS alone before the command that carries it beside a write is refused
        """The fold deleted the quote marks and the newlines, so a flag in
        a quoted value or on the next line is text beside the verb; the
        shell reader says which invocation it belongs to."""
        for quiet, command in (
                ("helm lr close d-1 --dry-run",
                 "helm lr close d-1 --reason 'not a --dry-run'"),
                ("helm lr close d-1 --dry-run",
                 "helm lr close d-1 --reason r\necho --dry-run"),
                ("helm lr close d-1 --dry-run",
                 "helm lr close d-1 --reason r # --dry-run"),
                ("helm lr close d-1 --dry-run",
                 "helm lr close x --dry-run; helm l$(echo r) close y"),
                ("helm dispatch cancel",
                 "helm dispatch cancel; helm dispatch cancel d-1 moot"),
                ("helm dispatch verdict --help",
                 "helm dispatch verdict d-1 T --fix e; echo --help"),
                ("helm lr expired", "helm lr expired --json --apply"),
                ("helm lr retire --off-frontier",
                 "helm lr retire --off-frontier; "
                 "helm lr retire d-1 --reason r")):
            with self.subTest(command=command):
                self.assertEqual(self.guard(quiet)[0], 0, quiet)
                rc, _out, err = self.guard(command)
                self.assertEqual(rc, 2, err)

    def test_a_test_home_passes_and_the_live_home_is_refused(self):  # noqa: VACUOUS_ASSERTION — a literal test home is asserted to PASS on the same entry before each live or unsettled home is refused
        """task/1388 let any HELM_HOME assignment through, so a delegate
        pointing it at the LIVE home wrote the live ledger unrefused. A test
        home is a literal path that resolves to neither the home helm
        resolves nor the default one."""
        write = " helm dispatch verdict d-1 T --fix e"
        self.assertEqual(self.guard("HELM_HOME=/tmp/h" + write)[0], 0)
        tmp = tempfile.mkdtemp(prefix="helm-test-delegate-home-")
        self.addCleanup(shutil.rmtree, tmp, True)
        link = os.path.join(tmp, "live-link")
        os.symlink(os.environ["HELM_HOME"], link)
        default = os.path.join(os.path.expanduser("~"), ".helm")
        for value in ("$HOME/.helm", "${HOME}/.helm", '"$HOME/.helm"',
                      "~/.helm", "~/.helm/", default, default + "/../.helm",
                      os.environ["HELM_HOME"], link,
                      "$SP/home", "$(mktemp -d)", "`mktemp -d`", "''"):
            with self.subTest(value=value):
                rc, _out, err = self.guard("HELM_HOME=%s%s" % (value, write))
                self.assertEqual(rc, 2, err)
        for command in (
                "HELM_HOME=" + write,
                "export HELM_HOME=/tmp/h; HELM_HOME=~/.helm" + write,
                "export HELM_HOME=~/.helm;" + write,
                "export HELM_HOME=/tmp/h; unset HELM_HOME;" + write,
                "export HELM_HOME=/tmp/h; env -u HELM_HOME" + write,
                "HELM_HOME=/tmp/h helm lr show d-1;" + write,
                "HELM_HOME=/tmp/h true &&" + write):
            with self.subTest(command=command):
                rc, _out, err = self.guard(command)
                self.assertEqual(rc, 2, err)
        # a relative home is joined to the directory the invocation runs in
        # (the payload's /tmp/repo, or where a cd left it)
        self.assertEqual(self.guard("HELM_HOME=.h" + write)[0], 0)
        self.assertEqual(self.guard(
            "cd %s && HELM_HOME=.helm%s" % (os.path.expanduser("~"),
                                            write))[0], 2)

    def test_an_option_before_the_program_voids_the_test_home_beside_it(self):  # noqa: VACUOUS_ASSERTION — the bare prefix and the bare env assignment are asserted to PASS on the same entry before each optioned spelling is refused
        """The reader folds `env -i HELM_HOME=v helm ...` into one call whose
        prefix holds both the assignment and the `-i`; taking the assignment
        first exempted exactly the spelling the contract names as no test
        home (a reviewer's fold read, task/3060). Any option word after `env`
        voids it now, and so does `sudo`: refusing `env -u OTHER` beside a
        test home is the accepted cost."""
        write = " helm dispatch verdict d-1 T --fix e"
        self.assertEqual(self.guard("HELM_HOME=/tmp/h" + write)[0], 0)
        self.assertEqual(self.guard("env HELM_HOME=/tmp/h" + write)[0], 0)
        for command in ("env -i HELM_HOME=/tmp/h" + write,
                        "env --ignore-environment HELM_HOME=/tmp/h" + write,
                        "env -u HELM_HOME HELM_HOME=/tmp/h" + write,
                        "env --unset HELM_HOME HELM_HOME=/tmp/h" + write,
                        "env -u HELM_CHAT_NAME HELM_HOME=/tmp/h" + write,
                        "sudo HELM_HOME=/tmp/h" + write):
            with self.subTest(command=command):
                rc, _out, err = self.guard(command)
                self.assertEqual(rc, 2, err)

    def test_an_export_inside_an_ended_subshell_reaches_nothing(self):  # noqa: VACUOUS_ASSERTION — the call's own prefix is asserted to PASS on the same entry before the export spellings are refused
        """The reader flattens `( ... )`, so a parenthesised `export
        HELM_HOME=v` read as still set for later invocations — exempting a
        write the real shell runs against the LIVE home (a reviewer's fold
        read, task/3060). No export makes a test home now, parenthesised or
        not; the call's own prefix does."""
        write = " helm dispatch verdict d-1 T --fix e"
        self.assertEqual(self.guard("HELM_HOME=/tmp/h" + write)[0], 0)
        for command in ("export HELM_HOME=/tmp/h;" + write,
                        "(export HELM_HOME=/tmp/h);" + write,
                        "(export HELM_HOME=/tmp/h;" + write + ")",
                        "(cd /tmp && export HELM_HOME=/tmp/h);" + write):
            with self.subTest(command=command):
                rc, _out, err = self.guard(command)
                self.assertEqual(rc, 2, err)

    def test_only_the_calls_own_prefix_makes_a_test_home(self):  # noqa: VACUOUS_ASSERTION — the own prefix, with and without a bare env, is asserted to PASS on the same entry before each hole is refused
        """The reader does not model shell state, and every spelling below
        reads an earlier statement's HELM_HOME, or an env option beside the
        call's own, as a test home the real shell does not keep. Each one
        exempted a write to the live ledger on some tip of this fold."""
        write = " helm dispatch verdict d-1 T --fix e"
        self.assertEqual(self.guard("HELM_HOME=/tmp/h" + write)[0], 0)
        self.assertEqual(self.guard("env HELM_HOME=/tmp/h" + write)[0], 0)
        self.assertEqual(self.guard(
            "export HELM_HOME=/tmp/h; HELM_HOME=/tmp/g" + write)[0], 0)
        for hole, command in (
                ("bash -c", "bash -c 'export HELM_HOME=/tmp/h';" + write),
                ("sh -c", "sh -c 'export HELM_HOME=/tmp/h';" + write),
                ("pipeline", "export HELM_HOME=/tmp/h | cat;" + write),
                ("background", "export HELM_HOME=/tmp/h &" + write),
                ("unset", "export HELM_HOME=/tmp/h; unset HELM_HOME;"
                 + write),
                ("export -n", "export HELM_HOME=/tmp/h; export -n HELM_HOME;"
                 + write),
                ("env --unset=", "export HELM_HOME=/tmp/h; "
                 "env --unset=HELM_HOME" + write),
                ("env -uX", "export HELM_HOME=/tmp/h; env -uHELM_HOME"
                 + write),
                ("env -uX own", "env -uHELM_HOME HELM_HOME=/tmp/h" + write),
                ("env --unset= own", "env --unset=HELM_HOME HELM_HOME=/tmp/h"
                 + write),
                ("env -", "env - HELM_HOME=/tmp/h" + write),
                ("export, env -", "export HELM_HOME=/tmp/h; env -" + write),
                ("env -C relative", "env -C /some/dir HELM_HOME=relative/path"
                 + write),
                ("env -C absolute", "env -C /some/dir HELM_HOME=/tmp/h"
                 + write),
                ("env -iv", "env -iv HELM_HOME=/tmp/h" + write),
                ("env -S", "env -S 'x' HELM_HOME=/tmp/h" + write),
                ("env -i after", "HELM_HOME=/tmp/h env -i" + write)):
            with self.subTest(hole=hole):
                rc, _out, err = self.guard(command)
                self.assertEqual(rc, 2, err)

    def test_writing_about_a_verb_passes(self):  # noqa: VACUOUS_ASSERTION — the same verb RUN is asserted refused on the same entry before each mention is asserted to pass
        """task/1388 read programs and passed these; the first fold of this
        rung read text and refused them. The rung reads what the shell runs
        now, so a mention in data passes again."""
        self.assertEqual(self.guard("helm lr land d-1")[0], 2)
        for mention in ("echo 'helm dispatch verdict d-1'",
                        "git commit -m 'helm lr close d-1'",
                        "grep -n 'helm dispatch verdict' notes.md",
                        "cat <<'EOF'\nhelm lr land d-1\nEOF"):
            with self.subTest(mention=mention):
                rc, _out, err = self.guard(mention)
                self.assertEqual(rc, 0, err)

    def test_a_defect_in_the_reader_lets_nothing_through(self):  # noqa: VACUOUS_ASSERTION — the dry run is asserted to PASS before the reader is broken
        """The reader only ever lets through what the fold found, so a
        defect in it refuses a quiet spelling and never passes a write."""
        self.assertEqual(self.guard("helm lr close d-1 --dry-run")[0], 0)
        with mock.patch.object(chat, "_commands_run",
                               side_effect=RuntimeError("defect")):
            self.assertEqual(self.guard("helm lr close d-1 --dry-run")[0], 2)
            self.assertEqual(self.guard(INCIDENT)[0], 2)
            self.assertEqual(self.guard("helm lr show d-1")[0], 0)

    def test_the_folded_verbs_name_the_route_and_take_a_grant(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted unconditionally first on the same entry, and the grant's id is asserted present in the admitted call's output
        rc, _out, err = self.guard("helm lr land d-1")
        self.assertEqual(rc, 2, err)
        self.assertIn("`helm lr land`", err)
        self.assertIn("--reviewer-model/--reviewer-run", err)
        self.assertIn('helm delegate allow --verbs "lr land"', err)
        grants = _grants()
        for verb in ("dispatch cancel", "dispatch rebind", "dispatch retip",
                     "lr land", "lr expired"):
            with self.subTest(verb=verb):
                self.assertEqual(grants.parse_verbs(verb), ([verb], None))
        grant, err = grants.allow(SESSION, ["dispatch cancel"],
                                  minted_by="seat-a")
        self.assertIsNone(err, err)
        rc, out, err = self.guard("helm dispatch cancel d-1 moot")
        self.assertEqual(rc, 0, err)
        self.assertIn("admitted under grant %s" % grant["id"], out)
        self.assertEqual(self.guard("helm lr land d-1")[0], 2)


class EntryImportTest(unittest.TestCase):
    """THE HOOK BUDGET, through the real entry script: the delegate rung
    imports no ledger module, for a refused write or a passed read."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def run_hook(self, command):
        probe = (
            "import json, sys, runpy\n"
            "sys.argv = ['helm', 'chat', 'argv-guard', '--hook-json']\n"
            "rc = 0\n"
            "try:\n"
            "    runpy.run_path(%r, run_name='__main__')\n"
            "except SystemExit as e:\n"
            "    rc = e.code or 0\n"
            "sys.stderr.write('PROBE ' + json.dumps({'rc': rc, 'mods': "
            "sorted(sys.modules)}) + '\\n')\n"
            % os.path.join(self.ROOT, "bin", "helm"))
        payload = {"tool_name": "Bash", "session_id": SESSION,
                   "cwd": "/tmp", "agent_id": "a1b2",
                   "tool_input": {"command": command}}
        tmp = tempfile.mkdtemp(prefix="helm-test-delegate-entry-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = dict(os.environ, HELM_NO_TREE_WARNING="1",
                   HELM_HOME=os.path.join(tmp, "helm"),
                   HELM_CHAT_DIR=os.path.join(tmp, "chat"))
        p = subprocess.run([sys.executable, "-c", probe],
                           input=json.dumps(payload), capture_output=True,
                           text=True, env=env)
        line = [l for l in p.stderr.splitlines() if l.startswith("PROBE ")]
        self.assertTrue(line, "the probe never reported: " + p.stderr[-800:])
        report = json.loads(line[-1][len("PROBE "):])
        return report["rc"], set(report["mods"]), p.stderr

    def test_the_rung_imports_no_ledger(self):
        rc, refused, err = self.run_hook(INCIDENT)
        self.assertEqual(rc, 2, err[-600:])
        self.assertIn("--reviewer-run", err)
        rc, passed, err = self.run_hook("helm lr close d-1 --dry-run")
        self.assertEqual(rc, 0, err[-600:])
        self.assertIn("helm.chat", passed)
        for heavy in ("helm.dispatches", "helm.landreq", "helm.registry"):
            with self.subTest(module=heavy):
                self.assertNotIn(heavy, refused)
                self.assertNotIn(heavy, passed)


class TheRungReadsWhatRunsTest(GuardBase):
    """The integrator's ruling (task/3060): the rung refuses a verb the shell
    RUNS, never one written as data. The falsifier (CL 85) is a spelling that
    executes a verb and passes; so every spelling below that executes one is
    refused, every census write-about passes, and what the reader cannot
    prove is data stays refused (unknown refuses)."""

    V = " dispatch verdict d-1 T --fix e"

    def executing(self):
        v = self.V
        return (
            # the program in every spelling a shell resolves
            "helm" + v, "./bin/helm" + v, "/opt/x/bin/helm" + v,
            "~/dev/helm/bin/helm" + v, "python3 -m helm" + v,
            "python3 bin/helm" + v, "$HELM" + v,
            # prefixes and wrappers
            "env HELM_CHAT_NAME=s ./bin/helm" + v, "env -u X helm" + v,
            "HELM_CHAT_NAME=s helm" + v, "sudo helm" + v,
            "timeout 60 helm" + v, "nohup helm" + v,
            "/usr/bin/time -f %e ./bin/helm lr close x --reason r",
            # shells, evaluators and substitutions
            "bash -c 'helm" + v + "'", 'sh -c "helm' + v + '"',
            "bash -lc 'cd /tmp && helm" + v + "'", "eval helm" + v,
            "eval 'helm" + v + "'", "x=$(helm" + v + ")",
            "echo $(helm" + v + ")", 'echo "$(helm' + v + ')"',
            "y=`helm" + v + "`", "a=$(b=$(helm" + v + "))",
            "case $x in a) helm" + v + ";; esac",
            # a heredoc a shell reads, quoted or not, or one built into -c
            "bash <<'EOF'\nhelm" + v + "\nEOF", "sh <<EOF\nhelm" + v + "\nEOF",
            "bash -s <<'EOF'\nhelm" + v + "\nEOF",
            'bash -c "$(cat <<\'EOF\'\nhelm' + v + '\nEOF\n)"',
            "echo 'helm" + v + "' | bash", "printf 'helm" + v + "\\n' | sh",
            # words assembled around an expansion, and option windows
            "helm dis$(echo patch) verdict d-1 T --fix e",
            'helm dispatch ver"$(echo dict)" d-1 T', "h$(echo elm)" + v,
            "helm dispatch --json verdict d-1 T --fix e",
            "helm lr --repo . close x", "helm store --project p confirm x",
            # programs that run their arguments
            "xargs helm" + v,
            "echo d-1 | xargs -I{} helm dispatch verdict {} T --fix e",
            "ssh host 'helm" + v + "'",
            "find . -name x -exec helm lr close {} \\;",
            "watch -n5 helm lr close x",
            # python that starts a process, and a script written then run
            "python3 -c \"import subprocess; subprocess.run('helm" + v
            + "', shell=True)\"",
            # a spawn hidden behind an indirect import: the module name is
            # quoted, and the text must still read as code (a review's delta
            # read: __import__("os").system was cut as data and passed)
            "python3 -c '__import__(\"os\").system(\"helm" + v + "\")'",
            "python3 -c 'import importlib; "
            "importlib.import_module(\"os\").system(\"helm" + v + "\")'",
            "python3 - <<'EOF'\nimport os\nos.system('helm" + v + "')\nEOF",
            # the allowlist arms (task/3071): every shape the denylist read
            # as data. The verb is shell-visible in each (the string the
            # spawn runs), so the rung refuses.
            "python3 -c 'from os import system; system(\"helm" + v + "\")'",
            "python3 -c 'import os as o; o.system(\"helm" + v + "\")'",
            "python3 -c 'import os; getattr(os, \"system\")(\"helm" + v
            + "\")'",
            "python3 -c 'exec(\"import os\\nos.system(\\\"helm" + v
            + "\\\")\")'",
            "python3 - <<'EOF'\nimport subprocess\nsubprocess.run('helm" + v
            + "', shell=True)\nEOF",
            "cat > /tmp/s.sh <<'EOF'\nhelm" + v + "\nEOF\nbash /tmp/s.sh",
            "cat <<'EOF' > s.sh\nhelm lr close x\nEOF\nchmod +x s.sh && ./s.sh",
            "cat <<'EOF' | tee run.sh\nhelm lr close x\nEOF\nsh run.sh",
            # a real run beside a quiet one, and a flag in a quoted value
            "helm lr close x --dry-run; helm lr close y --reason r",
            "helm lr close x --reason 'not --dry-run'",
            # the argv a python text carries as a LIST of constants
            # (task/3071): the fold never sees comma-joined words, so the
            # ast walk answers them
            "python3 -c 'import subprocess; subprocess.run([\"helm\",\"lr\","
            "\"close\",\"x\"])'",
            "python3 - <<'EOF'\nimport subprocess\nsubprocess.run(['helm',"
            "'dispatch','verdict','d-1','T','--fix','e'])\nEOF",
            "python3 -c 'import subprocess; subprocess.run([\"./bin/helm\","
            "\"lr\",\"close\",\"x\"])'",
            # the argv list with a VARIABLE arg after the verb (task/3071
            # cure): the row and the tip are names, the group and verb are
            # constants, and the leading-run read still finds the refused
            # pair. This is the incident's natural python port.
            "python3 -c 'import subprocess; row=\"d-1\"; subprocess.run("
            "[\"helm\",\"dispatch\",\"verdict\",row,\"T\",\"--approve\"])'",
            "python3 - <<'EOF'\nimport subprocess\nrow = open('r').read()\n"
            "subprocess.run(['helm','lr','close',row,'--reason','x'])\nEOF",
            # argv spelled as a CALL's positional args (task/3071): the
            # program name repeats (execlp) or the mode leads (spawnlp), and
            # the group and verb are constants after a constant helm word.
            "python3 -c 'import os; os.execlp(\"helm\",\"helm\",\"lr\","
            "\"close\",\"x\")'",
            "python3 -c 'import os; os.spawnlp(os.P_WAIT,\"helm\",\"helm\","
            "\"lr\",\"close\",\"x\")'",
            "python3 -c 'import asyncio; asyncio.run("
            "asyncio.create_subprocess_exec(\"helm\",\"lr\",\"close\",\"x\"))'",
            # a list led by the interpreter, and an option between group and
            # verb (the same window as a shell invocation)
            "python3 -c 'import subprocess; subprocess.run([\"python3\","
            "\"-m\",\"helm\",\"dispatch\",\"verdict\",\"d-1\",\"T\"])'",
            "python3 -c 'import subprocess; subprocess.run([\"helm\","
            "\"dispatch\",\"--json\",\"verdict\",\"d-1\"])'",
            # a python LIST inside a script the command writes then RUNS
            # (task/3071): the body is fed to cat, and _runs_what_it_wrote
            # proves python runs the file
            "cat > s.py <<'EOF'\nimport subprocess, sys\nsubprocess.run("
            "['helm','dispatch','verdict',sys.argv[1],'T','--approve'])"
            "\nEOF\npython3 s.py d-1",
            # RUNTIME-SUPPLIED ARGS (task/3071): the reader sees helm but
            # not its verb, and unknown refuses
            "echo 'dispatch verdict d-1 T --fix e' | xargs helm",
            "echo x | parallel helm",
            "eval $(pgrep -fa helm) dispatch verdict d-1 T --fix e",
            # helm INVOKED by xargs through a wrapper is the feed too
            # (task/3071 cure): env, sudo, and python -m helm all run it
            "echo 'lr close x' | xargs env helm",
            "echo 'lr close x' | xargs env -u X helm",
            "echo 'lr close x' | xargs sudo helm",
            "echo 'lr close x' | xargs python3 -m helm",
        )

    # THE CENSUS'S OWN WRITE-ABOUTS, masked and trimmed, three of them the
    # examples its report names
    ABOUT = (
        "git grep -n -e '^### `helm claims' -e '^### `helm dispatch retip' "
        "-e 'dispatch retip' docs/VERBS.md",
        'pgrep -f "bin/helm lr retire --off-frontier --apply" | head -5',
        "python3 -P - <<'EOF'\np = 'helm/store/write.py'\ns = open(p).read()"
        "\ns = s.replace('a', '`helm store supersede` this row to it')\nEOF",
        "cat > brief.txt <<'EOF'\nREVIEW: file helm dispatch verdict ID TIP "
        "--fix\nEOF\nhelm dispatch send r lane --ref T --kind review "
        "--new-work < brief.txt",
        "helm dispatch send r lane --ref T --kind review --new-work "
        "<<'BODY'\nthen file: helm dispatch verdict <row> $car --approve"
        "\nBODY",
        "git -c user.name=akapug -c user.email=a@b commit -q -F - <<'EOF'"
        "\nthe `helm store supersede` cure\nEOF",
        "/usr/bin/rg -n 'helm lr close' x",
        "printf '%s\\n' 'helm lr land d-1'",
        "python3 -c \"print('helm lr close x')\"",
        "ps -eo pid,args | /usr/bin/rg 'helm lr close'",
        'helm task add "cure: helm dispatch verdict --fix" --owner x',
        "pgrep -fa 'helm lr retire' | head -3",
        'helm chat post --room r "use helm dispatch retract"',
    )

    # what the reader cannot prove is data: refused, and the reason each is
    # named in the rung's comment
    UNPROVEN = (
        'until [ -z "$(pgrep -f \'helm lr close x\')" ]; do sleep 1; done',
        "awk '/helm lr close/' notes.md",
        'for v in "helm lr land" "helm lr show"; do echo "$v"; done',
        "tail -1 f | python3 -c 'import sys; print(\"helm lr close\")'",
        "grep x f | sed 's/helm lr close//'",
        "cat > brief.txt <<EOF\nfile helm dispatch verdict $ROW\nEOF",
        "python3 - <<'EOF'\nimport subprocess\ns = 'helm lr close x'\nEOF",
    )

    def test_the_allowlist_replaces_the_denylist(self):  # noqa: VACUOUS_ASSERTION — PLANT: each arm is asserted refused, then the denylist is restored and the same arms are asserted to PASS (the cut wrongly removes them)
        """task/3071's plant: restore `_SPAWNS` as python's data test (the
        denylist the allowlist replaced) and every allowlist arm passes —
        the denylist reads a spawn behind an indirect import or a from-
        import as data, and the data cut removes the verb."""
        v = self.V.replace(" dispatch verdict ", " lr close ")
        arms = (
            "python3 -c 'from os import system; system(\"helm" + v + "\")'",
            "python3 -c 'import os as o; o.system(\"helm" + v + "\")'",
            "python3 -c 'import os; getattr(os, \"system\")(\"helm" + v
            + "\")'",
            # exec of a built string is NOT in this plant: the string the
            # exec runs carries os.system as TEXT, which even the denylist
            # saw — it refused under both, so it proves nothing here. Its
            # arm is in the executing set.
        )
        for command in arms:
            with self.subTest(command=command):
                self.assertEqual(self.guard(command)[0], 2)

        # the denylist AS the allowlist's answer: inert exactly when the
        # spawn regex sees nothing
        with mock.patch.object(chat, "python_text_inert",
                               lambda src: not chat._SPAWNS.search(src)):
            for command in arms:
                with self.subTest(command=command):
                    self.assertEqual(self.guard(command)[0], 0,
                                     "the plant: the denylist must pass "
                                     "what the allowlist refuses")

    def test_the_argv_list_walk_and_the_runtime_unknown_are_load_bearing(self):  # noqa: VACUOUS_ASSERTION — PLANT: each arm is asserted refused, then each cure is stubbed out and its arms are asserted to PASS
        """One plant per cure (task/3071): with the ast list walk returning
        nothing, a comma-joined argv passes; with the runtime-fed read
        answering False, a helm fed its verb through a pipe passes."""
        v = self.V.replace(" dispatch verdict ", " lr close ")
        listed = ("python3 -c 'import subprocess; subprocess.run("
                  "[\"helm\",\"lr\",\"close\",\"x\"])'")
        fed = "echo 'dispatch verdict d-1 T --fix e' | xargs helm"
        self.assertEqual(self.guard(listed)[0], 2)
        self.assertEqual(self.guard(fed)[0], 2)
        with mock.patch.object(chat, "_python_argv_verbs",
                               lambda command: ()):
            self.assertEqual(self.guard(listed)[0], 0,
                             "the plant: without the walk the list passes")
        with mock.patch.object(chat, "_runtime_fed_helm",
                               lambda command: False):
            self.assertEqual(self.guard(fed)[0], 0,
                             "the plant: without the unknown the feed passes")

    def test_a_variable_arg_does_not_hide_a_spelled_list_verb(self):  # noqa: VACUOUS_ASSERTION — PLANT: the variable-arg incident is refused, then the walk is stubbed out and the same command PASSES; and a variable GROUP/VERB (genuinely unknown) must PASS so the rung is not too wide
        """task/3071 cure: a python argv list whose ROW and TIP are variables
        still spells its group and verb as constants, and the leading-run
        read finds the refused pair. The all-constant requirement it replaces
        let one name hide the whole invocation (the incident's natural port).
        The plant: stub the walk and the variable-arg incident passes. The
        control a too-wide rung fails: a variable GROUP or VERB is genuinely
        unknown and must pass — the walk judges only the constants it sees."""
        var_arg = ("python3 -c 'import subprocess; row=\"d-1\"; "
                   "subprocess.run([\"helm\",\"dispatch\",\"verdict\",row,"
                   "\"T\",\"--approve\"])'")
        self.assertEqual(self.guard(var_arg)[0], 2)
        with mock.patch.object(chat, "_python_argv_verbs",
                               lambda command: ()):
            self.assertEqual(self.guard(var_arg)[0], 0,
                             "the plant: without the walk the list passes")
        # a variable in the GROUP or VERB slot is not a spelled refused verb
        for unknown in (
                "python3 -c 'import subprocess; g=\"lr\"; subprocess.run("
                "[\"helm\",g,\"close\",\"x\"])'",
                "python3 -c 'import subprocess; v=\"close\"; subprocess.run("
                "[\"helm\",\"lr\",v,\"x\"])'",
                # a list read whose verb is not refused still passes
                "python3 -c 'import subprocess; r=\"d-1\"; subprocess.run("
                "[\"helm\",\"lr\",\"show\",r])'"):
            with self.subTest(command=unknown):
                self.assertEqual(self.guard(unknown)[0], 0, unknown)

    def test_call_args_and_written_scripts_and_the_option_window(self):  # noqa: VACUOUS_ASSERTION — PLANT: each generalized spelling is refused, then _argv_sequence_verbs is stubbed and the same spellings PASS; grep handed the verb string as ONE arg must PASS
        """task/3071 generalization: the argv the walk reads is a SEQUENCE —
        list/tuple elements or a call's positional args — so os.execlp, a
        list led by the interpreter, an option between group and verb, and a
        list inside a written-then-run script are all read by one rule. The
        plant: stub the sequence read and each passes. The control a too-wide
        rung fails: a whole `helm dispatch verdict` STRING handed to grep as
        one argument spells nothing (it is one word, not three)."""
        refused = (
            "python3 -c 'import os; os.execlp(\"helm\",\"helm\",\"lr\","
            "\"close\",\"x\")'",
            "python3 -c 'import subprocess; subprocess.run([\"python3\","
            "\"-m\",\"helm\",\"dispatch\",\"verdict\",\"d-1\",\"T\"])'",
            "python3 -c 'import subprocess; subprocess.run([\"helm\","
            "\"dispatch\",\"--json\",\"verdict\",\"d-1\"])'",
            "cat > s.py <<'EOF'\nimport subprocess, sys\nsubprocess.run("
            "['helm','dispatch','verdict',sys.argv[1],'T'])\nEOF\npython3 s.py",
        )
        for command in refused:
            with self.subTest(command=command):
                self.assertEqual(self.guard(command)[0], 2, command)
        with mock.patch.object(chat, "_argv_sequence_verbs",
                               lambda elts: []):
            for command in refused:
                with self.subTest(command=command):
                    self.assertEqual(self.guard(command)[0], 0,
                                     "the plant: without the sequence read it "
                                     "passes")
        # the walk itself: the verb as ONE multi-token element spells nothing
        # (it is one arg, a grep pattern), a run of single tokens spells the
        # invocation, and a variable group/verb is unknown
        import ast
        self.assertEqual(chat._argv_sequence_verbs(
            ast.parse("['grep', 'helm dispatch verdict', 'd']",
                      mode="eval").body.elts), [])
        self.assertIn("dispatch verdict", chat._argv_sequence_verbs(
            ast.parse("['helm', 'dispatch', 'verdict', row, 'T']",
                      mode="eval").body.elts))
        self.assertEqual(chat._argv_sequence_verbs(
            ast.parse("['helm', g, 'verdict']", mode="eval").body.elts), [])

    def test_xargs_judges_the_invoked_command_not_a_data_arg(self):  # noqa: VACUOUS_ASSERTION — PLANT: xargs INVOKING helm is refused, then _runtime_fed_helm is stubbed and it PASSES; helm handed to grep/wc as data must PASS
        """task/3071 cure: xargs/parallel feed the runtime unknown only when
        they INVOKE helm — bare, or through env/sudo/python -m helm. Helm
        handed to another program as a pattern or a filename is a read. The
        plant: stub the fed read and the bare feed passes."""
        fed = "echo 'lr close x' | xargs helm"
        self.assertEqual(self.guard(fed)[0], 2)
        with mock.patch.object(chat, "_runtime_fed_helm",
                               lambda command: False):
            self.assertEqual(self.guard(fed)[0], 0,
                             "the plant: without the unknown the feed passes")
        for reads in (
                "git ls-files | xargs grep -n helm",
                "ls | xargs wc -l helm",
                "git ls-files | xargs env grep -n helm",
                "echo helm.txt | xargs cat"):
            with self.subTest(command=reads):
                self.assertEqual(self.guard(reads)[0], 0, reads)

    def test_time_and_typing_are_inert(self):  # noqa: VACUOUS_ASSERTION — the inert-import read is asserted to PASS, then a non-inert import beside the same mention is asserted refused
        """task/3071: time and typing start nothing and write nothing, so a
        read that imports them and MENTIONS a verb literally is data, not a
        write-about refusal. A non-inert import beside the same mention still
        refuses — the allowlist, not the mention, is what lifts it."""
        for ok in (
                "python3 -c 'import time; print(time.time(), \"helm lr "
                "close\")'",
                "python3 -c 'from typing import List; print(\"helm dispatch "
                "verdict\")'"):
            with self.subTest(command=ok):
                self.assertEqual(self.guard(ok)[0], 0, ok)
        self.assertEqual(self.guard(
            "python3 -c 'import socket; print(\"helm lr close\")'")[0], 2)

    def test_every_executing_spelling_is_refused(self):  # noqa: VACUOUS_ASSERTION — a census write-about is asserted to PASS on the same entry before every executing spelling is asserted refused
        self.assertEqual(self.guard(self.ABOUT[0])[0], 0)
        spellings = self.executing()
        self.assertGreaterEqual(len(spellings), 45)
        for command in spellings:
            with self.subTest(command=command):
                rc, _out, err = self.guard(command)
                self.assertEqual(rc, 2, err)
                self.assertIn("BLOCKED", err)

    def test_every_census_write_about_passes(self):  # noqa: VACUOUS_ASSERTION — the incident is asserted refused on the same entry before each write-about is asserted to pass
        self.assertEqual(self.guard(INCIDENT)[0], 2)
        for command in self.ABOUT:
            with self.subTest(command=command):
                rc, _out, err = self.guard(command)
                self.assertEqual(rc, 0, err)

    def test_what_the_reader_cannot_prove_is_data_is_refused(self):  # noqa: VACUOUS_ASSERTION — the quoted cat brief is asserted to PASS on the same entry before each unproven spelling is refused
        self.assertEqual(self.guard(self.ABOUT[3])[0], 0)
        for command in self.UNPROVEN:
            with self.subTest(command=command):
                self.assertEqual(self.guard(command)[0], 2)

    def test_a_script_the_command_writes_is_data_until_it_runs(self):  # noqa: VACUOUS_ASSERTION — the written script is asserted to PASS unrun, and refused once a shell runs it, on the same entry
        body = "cat > s.sh <<'EOF'\nhelm lr close x --reason r\nEOF"
        self.assertEqual(self.guard(body)[0], 0)
        self.assertEqual(self.guard(body + "\nbash s.sh")[0], 2)
        self.assertEqual(self.guard(body + "\n./s.sh")[0], 2)
        self.assertEqual(self.guard(body + "\ncat s.sh")[0], 0)
        # a python script is python text: data unless it starts a process
        probe = "cat > p.py <<'EOF'\ncases = ['helm lr close x']\nEOF"
        self.assertEqual(self.guard(probe + "\npython3 p.py")[0], 0)
        spawn = ("cat > p.py <<'EOF'\nimport os\nos.system('helm lr close x')"
                 "\nEOF")
        self.assertEqual(self.guard(spawn + "\npython3 p.py")[0], 2)
        # and a spawn the denylist never saw (task/3071): the written
        # script's text is judged by the same allowlist as the -c text
        for body in ("import os as o\no.system('helm lr close x')",
                     "from os import system\nsystem('helm lr close x')"):
            with self.subTest(body=body):
                s = "cat > p.py <<'EOF'\n%s\nEOF\npython3 p.py" % body
                self.assertEqual(self.guard(s)[0], 2)

    def test_a_defect_in_the_data_cut_refuses_the_mention(self):  # noqa: VACUOUS_ASSERTION — the mention is asserted to PASS before the cut is broken
        """The cut only ever lets through what it proves; a defect in it
        reads the command whole, so it costs a refusal and never a pass."""
        self.assertEqual(self.guard(self.ABOUT[3])[0], 0)
        with mock.patch.object(chat, "_cut_data",
                               side_effect=RuntimeError("defect")):
            self.assertEqual(self.guard(self.ABOUT[3])[0], 2)
            self.assertEqual(self.guard(INCIDENT)[0], 2)


class TheGrantTest(GuardBase):
    """E2: `helm delegate allow` from the main thread admits its delegates."""

    def allow(self, verbs="dispatch verdict", ttl=None):
        grant, err = _grants().allow(SESSION, verbs.split(","),
                                     ttl_s=ttl, minted_by="seat-a")
        self.assertIsNone(err, err)
        return grant

    def test_a_granted_delegate_verdict_is_admitted_and_says_by_which_grant(self):
        self.assertEqual(self.guard(INCIDENT)[0], 2, "control: refused bare")
        grant = self.allow()
        rc, out, err = self.guard(INCIDENT)
        self.assertEqual(rc, 0, err)
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("admitted under grant %s" % grant["id"], ctx)
        self.assertIn("`helm dispatch verdict`", ctx)

    def test_a_grant_admits_exactly_its_verbs_on_its_session_until_it_ends(self):  # noqa: VACUOUS_ASSERTION — both granted verbs are asserted admitted by id before any absence is read
        grants = _grants()
        now = time.time()
        grant = self.allow("dispatch verdict,lr close")
        self.assertEqual(grants.admits(SESSION, "dispatch verdict")["id"],
                         grant["id"])
        self.assertEqual(grants.admits(SESSION, "lr close")["id"], grant["id"])
        self.assertIsNone(grants.admits(SESSION, "store confirm"))
        self.assertIsNone(grants.admits("another-session", "dispatch verdict"))
        self.assertIsNone(grants.admits(SESSION, "dispatch verdict",
                                        now=now + grants.TTL_DEFAULT_S + 1))
        # A command holding a granted and an ungranted verb is refused on the
        # ungranted one.
        rc, _out, err = self.guard(INCIDENT + " && helm store confirm x")
        self.assertEqual(rc, 2, err)
        self.assertIn("`helm store confirm`", err)
        gone, err = grants.revoke(SESSION, grant["id"])
        self.assertEqual((gone, err), ([grant["id"]], None))
        self.assertIsNone(grants.admits(SESSION, "dispatch verdict"))
        self.assertEqual(self.guard(INCIDENT)[0], 2)

    def test_no_grant_admits_minting_or_revoking_a_grant(self):
        grants = _grants()
        self.assertEqual(grants.parse_verbs("dispatch verdict")[0],
                         ["dispatch verdict"])
        for verb in ("delegate allow", "delegate revoke"):
            with self.subTest(verb=verb):
                self.assertIsNone(grants.parse_verbs(verb)[0])
        # A file that SAYS it grants one is data, and the door refuses it.
        self.allow()
        path = grants._path(SESSION)
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        doc["grants"][0]["verbs"].append("delegate allow")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        self.assertIsNotNone(grants.admits(SESSION, "dispatch verdict"))
        self.assertIsNone(grants.admits(SESSION, "delegate allow"))
        rc, _out, err = self.guard('helm delegate allow --verbs "lr close"')
        self.assertEqual(rc, 2, err)
        self.assertIn("seat's own act", err)

    def test_an_unreadable_grant_admits_nothing(self):
        grants = _grants()
        self.allow()
        self.assertIsNotNone(grants.admits(SESSION, "dispatch verdict"))
        with open(grants._path(SESSION), "w", encoding="utf-8") as f:
            f.write("{torn")
        self.assertIsNone(grants.admits(SESSION, "dispatch verdict"))
        self.assertEqual(self.guard(INCIDENT)[0], 2)


class TheDelegateVerbTest(GuardBase):
    """E2: the CLI door the seat's main thread uses."""

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = _grants().cmd_delegate(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_allow_list_revoke_round_trip(self):
        rc, out, err = self.cli("allow", "--verbs", "dispatch verdict",
                                "--ttl", "10m", "--note", "reader lane",
                                "--json")
        self.assertEqual((rc, err), (0, ""))
        grant = json.loads(out)
        self.assertEqual(grant["verbs"], ["dispatch verdict"])
        self.assertEqual(grant["minted_by"], "seat-a")
        rc, out, err = self.cli("list")
        self.assertEqual(rc, 0, err)
        self.assertIn(grant["id"], out)
        self.assertIn("reader lane", out)
        rc, out, err = self.cli("revoke", grant["id"])
        self.assertEqual(rc, 0, err)
        rc, out, err = self.cli("list")
        self.assertIn("no live grant", out)

    def test_every_malformed_call_refuses(self):  # noqa: VACUOUS_ASSERTION — a well-formed allow is asserted to succeed on the same entry first
        # CONTROL on the same entry: a well-formed allow succeeds.
        self.assertEqual(self.cli("allow", "--verbs", "lr close")[0], 0)
        for argv, rc, word in (
                (("allow",), 2, "names no verb"),
                (("allow", "--verbs", "dispatch send"), 2, "not a verb"),
                (("allow", "--verbs", "delegate allow"), 2, "seat's own act"),
                (("allow", "--verbs", "lr close", "--ttl", "48h"), 2,
                 "capped at 24h"),
                (("allow", "--verbs", "lr close", "--bogus", "x"), 2,
                 "unexpected"),
                (("revoke",), 2, "one grant id or --all"),
                (("frobnicate",), 2, "unknown subverb")):
            with self.subTest(argv=argv):
                got, _out, err = self.cli(*argv)
                self.assertEqual(got, rc, err)
                self.assertIn(word, err)

    def test_a_process_with_no_session_holds_no_grant(self):
        with mock.patch.dict(os.environ):
            for key in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
                        "CODEX_SESSION_ID"):
                os.environ.pop(key, None)
            rc, _out, err = self.cli("allow", "--verbs", "lr close")
        self.assertEqual(rc, 1)
        self.assertIn("has none", err)

    def test_the_verb_is_registered_and_documented(self):
        from helm import cli
        self.assertIn("delegate", cli.VERBS)
        for word in ("allow", "list", "revoke", "--verbs", "--ttl", "--note",
                     "--all", "handoff write"):
            self.assertIn(word, cli._VERB_HELP["delegate"])


if __name__ == "__main__":
    unittest.main()
