#!/usr/bin/env python3
"""The friction ledger: a refusal is counted, and only a refusal.

Every arm reads rows the REAL producer wrote -- `friction.record`, the hook
dispatcher driving the shipped argv-guard, a real `git commit` against the
hook `helm work install-guard --apply` installs -- never a hand-built record.
Every absence is read against a presence on the same observable.
"""
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-friction-", var="HELM_HOME")

from helm import cli, friction, home, hookrun, reflex, work  # noqa: E402
from helm.work import _guard  # noqa: E402
# `seat` IS IMPORTED EXPLICITLY because the co-occurrence guard in
# tests/test_seat_facade_injection.py requires an accepted facade import
# beside any impl import. It asserts nothing about import order.
from helm import seat  # noqa: E402,F401
from helm import chat, pk, seats_runtime  # noqa: E402
from helm import web, web_ui_loader  # noqa: E402
from helm.seats_common import roster_path  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEAT = "fixture-seat"
# assembled from pieces so this FILE never carries the spelling the gate
# refuses: a seat reading or writing it would be refused by its own guard
REFUSED_COMMAND = " ".join(("gh", "work" + "flow", "run", "ci.yml"))


_SCRATCH_GC = "HELM_SCRATCH_GC"
_SCRATCH_PRIOR = None


def setUpModule():
    """The arms below name the Stop gate and drive real hook entries; the
    stop-guard's scratch reaper deletes under the real harness estate, and a
    test never touches a real harness store."""
    global _SCRATCH_PRIOR
    _SCRATCH_PRIOR = os.environ.get(_SCRATCH_GC)
    os.environ[_SCRATCH_GC] = "0"


def tearDownModule():
    if _SCRATCH_PRIOR is None:
        os.environ.pop(_SCRATCH_GC, None)
    else:
        os.environ[_SCRATCH_GC] = _SCRATCH_PRIOR


class LedgerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-friction-case-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "helm"),
            "HELM_CHAT_NAME": SEAT})
        env.start()
        self.addCleanup(env.stop)

    def rows(self):
        rows, unreadable = friction.read()
        self.assertIsNone(unreadable)
        return rows

    def raw(self):
        with open(friction.path(), "rb") as f:
            return f.read()

    def verb(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.VERBS["friction"](list(args))
        return rc, out.getvalue(), err.getvalue()


class RecordTest(LedgerBase):
    def test_a_refusal_is_one_row_of_tokens(self):
        self.assertTrue(friction.record("argv-guard", reason="PreToolUse:Bash",
                                        session="sess-1"))
        (row,) = self.rows()
        self.assertEqual(sorted(row),
                         ["guard", "id", "reason", "seat", "session", "ts"])
        self.assertEqual((row["guard"], row["seat"], row["session"], row["reason"]),
                         ("argv-guard", SEAT, "sess-1", "PreToolUse:Bash"))
        self.assertIsNotNone(friction._epoch(row["ts"]))

    def test_a_reason_that_is_not_a_token_is_dropped_never_stored(self):  # noqa: VACUOUS_ASSERTION — the absent secret is read against the two rows asserted present in the same ledger bytes one line above
        body = "curl -H 'Authorization: Bearer zz-fixture-secret' example.test"  # gitleaks:allow — a synthetic bearer value the test proves is dropped
        self.assertTrue(friction.record("argv-guard", reason=body))
        self.assertTrue(friction.record("argv-guard", reason="kept-token"))
        self.assertEqual([r["reason"] for r in self.rows()], [None, "kept-token"])
        self.assertNotIn(b"zz-fixture-secret", self.raw())

    def test_a_guard_that_is_not_a_token_counts_nothing(self):
        self.assertFalse(friction.record("not a token"))
        self.assertFalse(friction.record(None))
        self.assertTrue(friction.record("seat-name"))
        self.assertEqual([r["guard"] for r in self.rows()], ["seat-name"])

    def test_an_unwritable_ledger_returns_false_and_never_raises(self):
        self.assertTrue(friction.record("docref"), "control: the write works")
        state = os.path.dirname(friction.path())
        shutil.rmtree(state)
        with open(state, "w") as f:
            f.write("a file where the state directory belongs\n")
        self.assertIs(friction.record("docref"), False)

    def test_the_ledger_keeps_one_rotated_generation_and_reads_both(self):
        with mock.patch.object(friction, "MAX_BYTES", 1):
            for guard in ("first", "second", "third"):
                self.assertTrue(friction.record(guard))
        self.assertTrue(os.path.exists(friction.path() + ".1"))
        self.assertEqual([r["guard"] for r in self.rows()], ["second", "third"])


class ReportTest(LedgerBase):
    def seed(self):
        now = time.time()
        for guard, seat, age_days in (("argv-guard", SEAT, 0), ("argv-guard", SEAT, 1),
                                      ("argv-guard", "other-seat", 2),
                                      ("stop-guard", SEAT, 3),
                                      ("docref", SEAT, 30)):
            with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": seat}):
                self.assertTrue(friction.record(guard, now=now - age_days * 86400))
        return now

    def test_refusals_are_counted_per_guard_inside_the_window(self):
        now = self.seed()
        got = friction.report(now=now)
        self.assertEqual([(g["guard"], g["refusals"]) for g in got["guards"]],
                         [("argv-guard", 3), ("stop-guard", 1)])
        self.assertEqual(got["guards"][0]["seats"], {SEAT: 2, "other-seat": 1})
        self.assertEqual(got["total"], 4)
        # the row outside the window is IN the ledger: a wider window counts it
        wide = friction.report(days=60, now=now)
        self.assertIn(("docref", 1),
                      [(g["guard"], g["refusals"]) for g in wide["guards"]])

    def test_the_verb_prints_the_census_and_the_seat_split(self):
        self.seed()
        rc, out, _err = self.verb()
        self.assertEqual(rc, 0)
        self.assertIn("4 refusals in the last 7 days", out)
        self.assertRegex(out, r"\n\s+3\s+argv-guard\n")
        rc, out, _err = self.verb("--seat")
        self.assertIn("argv-guard  [%s 2, other-seat 1]" % SEAT, out)
        rc, out, _err = self.verb("--json", "--days", "60")
        self.assertEqual(json.loads(out)["total"], 5)

    def test_an_unreadable_ledger_says_UNREADABLE_and_an_absent_one_says_zero(self):
        rc, out, _err = self.verb()
        self.assertEqual((rc, out.splitlines()[0]),
                         (0, "helm friction — 0 refusals in the last 7 days"))
        os.makedirs(friction.path())        # a directory where the ledger belongs
        got = friction.report()
        self.assertTrue(got["unreadable"])
        self.assertIsNone(got["guards"])
        self.assertIsNone(got["total"])
        rc, out, err = self.verb()
        self.assertEqual(rc, 1)
        self.assertIn("UNREADABLE", err)
        self.assertNotIn("0 refusals", out + err)
        rc, out, _err = self.verb("--json")
        self.assertEqual(rc, 1)
        self.assertIsNone(json.loads(out)["total"])

    def test_the_verb_refuses_what_it_does_not_know(self):  # noqa: VACUOUS_ASSERTION — the refusals are read against the accepted `record` call and the row it lands, asserted by equality at the end
        for argv in (["--gaet"], ["record"], ["record", "a", "b"],
                     ["record", "a", "--nope"], ["--days", "zero"]):
            rc, _out, err = self.verb(*argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn("usage: helm friction", err)
        self.assertEqual(self.verb("record", "split-budget", "--reason",
                                   "pre-commit")[0], 0)
        self.assertEqual([(r["guard"], r["reason"]) for r in self.rows()],
                         [("split-budget", "pre-commit")])


class GateWriteSiteTest(LedgerBase):
    """The dispatcher's rc-2 escape, driven through the shipped argv-guard."""

    def dispatch(self, command, specs=None):
        payload = json.dumps({
            "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "session_id": "sess-gate", "cwd": self.tmp,
            "tool_use_id": "toolu_friction",
            "tool_input": {"command": command}})
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ):
            os.environ.pop("HELM_ALLOW_GITHUB_ACTIONS", None)
            return hookrun.run_event("PreToolUse", payload=payload, specs=specs)

    def test_a_gate_refusal_is_counted_and_carries_no_input(self):  # noqa: VACUOUS_ASSERTION — the absent input is read against the one row unpacked and compared by equality from the same ledger
        rc = self.dispatch(REFUSED_COMMAND + " --field token=zz-fixture-secret")
        self.assertEqual(rc, 2)
        (row,) = self.rows()
        self.assertEqual((row["guard"], row["seat"], row["session"], row["reason"]),
                         ("argv-guard", SEAT, "sess-gate", "PreToolUse:Bash"))
        self.assertNotIn(b"zz-fixture-secret", self.raw())
        self.assertNotIn(b"ci.yml", self.raw())

    def test_an_agent_call_naming_a_model_is_refused_through_the_dispatcher(self):  # noqa: VACUOUS_ASSERTION — the absent ledger is read against the one row the refusing dispatch writes to the same ledger right after it, unpacked and compared by equality
        """The merged dispatcher reads the SPECS matcher at run time, so an
        Agent payload reaches the argv-guard only because the matcher names
        Agent. Refused, it is counted as an Agent refusal; without a model
        the same door admits it and counts nothing."""
        def agent(tool_input):
            payload = json.dumps({
                "hook_event_name": "PreToolUse", "tool_name": "Agent",
                "session_id": "sess-gate", "cwd": self.tmp,
                "tool_use_id": "toolu_friction", "tool_input": tool_input})
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                return hookrun.run_event("PreToolUse", payload=payload)
        self.assertEqual(agent({"prompt": "p"}), 0)
        self.assertFalse(os.path.exists(friction.path()))
        self.assertEqual(agent({"prompt": "p", "model": "haiku"}), 2)
        (row,) = self.rows()
        self.assertEqual((row["guard"], row["reason"]),
                         ("argv-guard", "PreToolUse:Agent"))

    def test_an_allow_counts_nothing(self):  # noqa: ORPHANED_MOCK — hookrun reaches friction.record_gate through a function-local import the walker does not follow; the refusing dispatch below proves the double fires
        with mock.patch.object(friction, "record_gate") as counted:
            self.assertEqual(self.dispatch("git status --short"), 0)
            self.assertEqual(counted.call_count, 0)
            # the same door, refusing, reaches the ledger exactly once
            self.assertEqual(self.dispatch(REFUSED_COMMAND), 2)
            self.assertEqual(counted.call_count, 1)
        self.assertFalse(os.path.exists(friction.path()))

    def test_the_dispatcher_imports_the_ledger_only_on_the_refusal_path(self):
        """The no-refusal path pays nothing: the ledger module is named by an
        import in ONE place, inside the function only a refusal calls."""
        import ast
        with open(hookrun.__file__) as f:
            tree = ast.parse(f.read())
        owners = []
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.Module)):
                continue
            for node in ast.iter_child_nodes(fn):
                for sub in ([node] if isinstance(fn, ast.Module)
                            else ast.walk(node)):
                    if isinstance(sub, (ast.Import, ast.ImportFrom)) and any(
                            al.name.split(".")[-1] == "friction"
                            for al in sub.names):
                        owners.append(getattr(fn, "name", "<module>"))
        self.assertEqual(sorted(set(owners)), ["_count_refusal"])

    def test_an_advisory_handlers_rc_2_is_swallowed_and_not_counted(self):  # noqa: VACUOUS_ASSERTION — the same payload through the same spec with gate=True lands exactly one row, asserted below
        advisory = [{"name": "argv-guard", "event": "PreToolUse",
                     "args": "chat argv-guard --hook-json", "timeout": 2,
                     "matcher": None}]
        self.assertEqual(self.dispatch(REFUSED_COMMAND, specs=advisory), 0)
        self.assertFalse(os.path.exists(friction.path()))
        self.assertEqual(
            self.dispatch(REFUSED_COMMAND, specs=[dict(advisory[0], gate=True)]), 2)
        self.assertEqual(len(self.rows()), 1)

    def test_a_ledger_that_cannot_be_written_leaves_the_refusal_standing(self):
        self.assertEqual(self.dispatch(REFUSED_COMMAND), 2)
        self.assertEqual(len(self.rows()), 1, "control: this door writes")
        os.unlink(friction.path())
        os.makedirs(friction.path())
        self.assertEqual(self.dispatch(REFUSED_COMMAND), 2)


class WrapperWriteSiteTest(LedgerBase):
    """The shipped hook wrapper: the door every standalone gate's rc 2 leaves
    through, an external gate's included. Run under /bin/sh, as installed."""

    WRAPPER = os.path.join(REPO, "bin", "helm-hook")

    def wrapped(self, name, command, child=None, event="PreToolUse"):
        child = child or [os.path.join(REPO, "bin", "helm"), "chat",
                          "argv-guard", "--hook-json"]
        payload = json.dumps({
            "hook_event_name": event, "tool_name": "Bash",
            "session_id": "sess-wrapper", "cwd": self.tmp,
            "tool_input": {"command": command}})
        env = dict(os.environ, HELM_HOOK_ALARM_DIR=os.path.join(self.tmp, "alarm"))
        env.pop("HELM_ALLOW_GITHUB_ACTIONS", None)
        return subprocess.run(
            ["sh", self.WRAPPER, "gate", name, event, "20", "tool call"] + child,
            input=payload, capture_output=True, text=True, timeout=120, env=env)

    def settled(self, want, read):
        """Poll `read` until it returns `want` rows: the count is detached
        from the refusal, so it lands after the wrapper has already exited."""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            got = read()
            if len(got) >= want:
                return got
            time.sleep(0.05)
        return read()

    def ledger(self):
        return [(r["guard"], r["seat"], r["reason"])
                for r in (friction.read()[0] if os.path.exists(friction.path())
                          else [])]

    def test_a_standalone_gates_refusal_is_counted_and_an_allow_is_not(self):  # noqa: VACUOUS_ASSERTION — the allow runs FIRST and the refusal after it through the same wrapper, so the single row asserted at the end is both the presence and the proof that the allow added none
        self.assertEqual(self.wrapped("argv-guard", "git status --short").returncode, 0)
        p = self.wrapped("argv-guard", REFUSED_COMMAND + " --field t=zz-fixture-secret")
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertEqual(self.settled(1, self.ledger),
                         [("argv-guard", SEAT, "PreToolUse")])
        self.assertNotIn(b"zz-fixture-secret", self.raw())

    def test_the_merged_dispatcher_is_not_counted_a_second_time(self):
        """A stub `helm` that refuses as a gate child and logs what the
        wrapper then asks of it: nothing under a `dispatch-*` name."""
        bin_dir = os.path.join(self.tmp, "stubbin")
        os.makedirs(bin_dir)
        log = os.path.join(self.tmp, "asked.log")
        stub = os.path.join(bin_dir, "helm")
        with open(stub, "w") as f:
            f.write('#!/bin/sh\n[ "$1" = friction ] || exit 2\n'
                    'echo "$@" >> %s\n' % shlex.quote(log))
        os.chmod(stub, 0o755)
        wrapper = os.path.join(bin_dir, "helm-hook")
        shutil.copy2(self.WRAPPER, wrapper)

        def asked():
            if not os.path.exists(log):
                return []
            with open(log) as f:
                return f.read().splitlines()

        with mock.patch.object(self, "WRAPPER", wrapper):
            self.assertEqual(self.wrapped("dispatch-Stop", "x", child=[stub],
                                          event="Stop").returncode, 2)
            self.assertEqual(self.wrapped("stop-guard", "x", child=[stub],
                                          event="Stop").returncode, 2)
        self.assertEqual(self.settled(1, asked),
                         ["friction record stop-guard --reason Stop"])


class PostRefusalWriteSiteTest(LedgerBase):
    def test_a_padded_sha_refusal_is_counted(self):
        from helm import chat, shaguard
        with mock.patch.dict(os.environ, {
                "HELM_CHAT_DIR": os.path.join(self.tmp, "chat"),
                "HELM_CHAT_NODE_URL": "", "HELM_CHAT_LOG": "0"}), \
                mock.patch.object(shaguard, "refuse", return_value=True):
            with self.assertRaises(ValueError):
                chat.post("a body the patched guard refuses", who=SEAT,
                          room="main")
        self.assertEqual([(r["guard"], r["reason"]) for r in self.rows()],
                         [("shaguard", "padded-sha")])


class CommitHookWriteSiteTest(unittest.TestCase):
    """END TO END: a real `git commit` against the installed hook, with a
    `helm` on PATH that is this tree."""

    NEEDLE = "zz-synthetic-friction-needle"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-friction-hook-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "helm"), "w") as f:
            f.write('#!/bin/sh\nexec "%s" -m helm "$@"\n' % sys.executable)
        os.chmod(os.path.join(bin_dir, "helm"), 0o755)
        needles = os.path.join(self.tmp, "needles.txt")
        with open(needles, "w") as f:
            f.write(self.NEEDLE + "\n")
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "helm"),
            "HELM_CHAT_DIR": os.path.join(self.tmp, "chat"),
            "HELM_CHAT_NODE_URL": "", "HELM_CHAT_LOG": "0",
            "HELM_LANDLOCK": "0", "HELM_CHAT_NAME": SEAT,
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "HELM_PRIVATE_NEEDLES": needles,
            "PATH": bin_dir + os.pathsep + os.environ.get("PATH", ""),
            "PYTHONPATH": REPO})
        env.start()
        self.addCleanup(env.stop)
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@example.com"),
                    ("git", "config", "user.name", "t"),
                    ("git", "config", "--local", "helm.guard.profile", "rail")):
            self.assertEqual(self.sh(*cmd).returncode, 0)
        self.stage("README", "seed\n")
        self.assertEqual(self.commit().returncode, 0)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = work.cmd_work(["install-guard", "--apply", "--repo", self.root])
        self.assertEqual(rc, 0, err.getvalue())

    def sh(self, *args):
        return subprocess.run(list(args), cwd=self.root, capture_output=True,
                              text=True, timeout=120)

    def stage(self, rel, content):
        full = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        self.assertEqual(self.sh("git", "add", "--", rel).returncode, 0)

    def commit(self):
        # NO ATTRIBUTION TRAILER. This fixture carried one only to satisfy a
        # rung that REQUIRED the line; that rung now refuses it, so keeping it
        # would make every "admitted" commit here refuse for a reason this
        # class is not about, and the arm would read as a friction-counting
        # defect rather than as a stale fixture.
        return self.sh("git", "commit", "-q", "-m", "c")

    def counted(self):
        rows, unreadable = friction.read()
        self.assertIsNone(unreadable)
        return [(r["guard"], r["seat"], r["reason"]) for r in rows]

    def test_a_refused_commit_is_counted_under_the_rung_that_refused_it(self):  # noqa: VACUOUS_ASSERTION — the empty census after an admitted commit is read against the one-row census after the refused one
        self.stage("docs/clean.md", "nothing to refuse\n")
        r = self.commit()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.counted(), [], "an admitted commit counts nothing")
        block = "\n".join(["<" * 7 + " HEAD", "ours", "=" * 7, "theirs",
                           ">" * 7 + " lane/x", ""])
        self.stage("docs/block.md", block)
        r = self.commit()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[helm conflict-marker] REFUSED", r.stderr)
        self.assertEqual(self.counted(), [("conflict-marker", SEAT, "pre-commit")])

    def test_the_terminal_scanner_is_counted_and_carries_no_content(self):  # noqa: VACUOUS_ASSERTION — the absent needle is read against the never-track row asserted by equality from the same ledger
        self.stage("docs/leak.md", "carries %s in its body\n" % self.NEEDLE)
        r = self.commit()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[helm never-track]", r.stderr)
        self.assertEqual(self.counted(), [("never-track", SEAT, "pre-commit")])
        with open(friction.path(), "rb") as f:
            self.assertNotIn(self.NEEDLE.encode(), f.read())

    def test_a_user_hooks_failure_names_no_rung_and_is_not_counted(self):
        user = work.hook_path(self.root, "pre-commit") + ".helm-user"
        with open(user, "w") as f:
            f.write("#!/bin/sh\nexit 9\n")
        os.chmod(user, 0o755)
        self.stage("docs/clean.md", "nothing to refuse\n")
        self.assertNotEqual(self.commit().returncode, 0)
        self.assertEqual(self.counted(), [])
        os.unlink(user)
        self.stage("docs/leak.md", "carries %s in its body\n" % self.NEEDLE)
        self.assertNotEqual(self.commit().returncode, 0)
        self.assertEqual([g for g, _s, _r in self.counted()], ["never-track"],
                         "control: the same hook counts a rung's refusal")


class EveryRefusingRungNamesItselfTest(unittest.TestCase):
    # The pre-push scanner is fed the ref lines the hook read once, so its
    # refusing line starts with that feeder rather than with python3.
    REFUSING = re.compile(r'^ *(?:helm_push_refs \| )?python3 "\$\w+"[^\n]*\|\| '
                          r'(?:exit \$\?|helm_merge_refused \$\?)$', re.M)

    def test_every_refusing_rung_in_every_managed_hook_names_itself(self):  # noqa: VACUOUS_ASSERTION — the loop's reach is pinned by the unconditional floor on `seen` after it
        seen = 0
        for name in ("NEVER_TRACK_HOOK", "LEAK_PRECOMMIT_HOOK",
                     "MERGE_COMMIT_HOOK", "HOSTPATH_PUSH_HOOK",
                     "TRAILER_MSG_HOOK"):
            template = getattr(_guard, name)
            self.assertIn("trap helm_refusal_counted EXIT", template, name)
            self.assertNotIn("exec python3", template, name)
            lines = template.splitlines()
            for m in self.REFUSING.finditer(template):
                at = template[:m.start()].count("\n")
                self.assertRegex(lines[at - 1], r"^ *helm_rung=[\w-]+$",
                                 "%s: %s" % (name, lines[at]))
                seen += 1
        self.assertGreaterEqual(seen, 15)


class RefusalStreakReflexTest(LedgerBase):
    def setUp(self):
        super().setUp()
        home.scaffold_global()
        reflex.seed_defaults()

    def refuse(self, guard, times, seat=SEAT, age_s=0):
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": seat}):
            for _ in range(times):
                self.assertTrue(friction.record(guard, now=time.time() - age_s))

    def fired(self, session="sess-streak"):
        return [e for e in reflex.fire("", session=session)
                if e["id"] == "guard-friction"]

    def test_the_counter_is_the_worst_single_guard_for_this_seat_today(self):
        self.refuse("argv-guard", 3)
        self.refuse("stop-guard", 2)
        self.refuse("argv-guard", 4, seat="other-seat")
        self.refuse("argv-guard", 6, age_s=2 * 86400)
        self.assertEqual(friction.counters(),
                         {"refusal-streak": 3, "refusal-streak-subject": "argv-guard"})
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "quiet-seat"}):
            self.assertEqual(friction.counters(), {})

    def test_the_seeded_reflex_fires_once_at_the_dial_and_names_the_guard(self):  # noqa: VACUOUS_ASSERTION — silence under the dial and silence once latched bracket the one firing unpacked between them
        th = reflex.REFUSAL_STREAK_THRESHOLD
        self.refuse("argv-guard", th - 1)
        self.assertEqual(self.fired(), [])
        self.refuse("argv-guard", 1)
        (e,) = self.fired()
        self.assertIn("argv-guard refused this seat %dx in 24h" % th, e["steer"])
        for fact in ("pre-authorized", "30 minutes", "one delegated agent",
                     "fixing the TOOL", "resume"):
            self.assertIn(fact, e["steer"])
        self.refuse("argv-guard", 1)
        self.assertEqual(self.fired(), [], "latched: once per episode")

    def test_the_seeded_entry_carries_NO_threshold_of_its_own(self):
        """The precedence argument rests on this and nothing enforced it.

        `counter_spec` lets an entry's own `threshold` outrank the default,
        so an entry carrying one makes the CARD state a number the SEATS do
        not meet: measured on this tip, a hand-written threshold of 2 gives
        counter_spec 2 while dial() reports 5. That is tolerable only while
        the seeded entry carries none, which is true today and is the reason
        the divergence was judged acceptable.

        So the reason is pinned. Whoever adds a threshold here reddens this
        and has to decide what the card should say before shipping it.
        """
        entry = next(d for d in reflex.DEFAULT_PACK
                     if d["id"] == "guard-friction")
        self.assertNotIn("threshold", entry)
        # CONTROL on the same observable: an entry that DOES carry one is
        # read by counter_spec, so the assertion above is load-bearing and
        # not a statement about a field counter_spec ignores.
        self.assertEqual(
            reflex.counter_spec(dict(entry, threshold=2))[1], 2)

    def test_the_dial_is_the_one_constant(self):
        e = next(d for d in reflex.DEFAULT_PACK if d["id"] == "guard-friction")
        self.assertNotIn("threshold", e)
        self.assertEqual(reflex.counter_spec(e)[:2],
                         ("refusal-streak", reflex.REFUSAL_STREAK_THRESHOLD))
        with mock.patch.dict(reflex.COUNTER_SIGNALS, {
                "refused": ("refusal-streak", 2, 0)}):
            self.refuse("docref", 1)
            self.assertEqual(self.fired("sess-dial"), [])
            self.refuse("docref", 1)
            self.assertEqual(len(self.fired("sess-dial")), 1)

    def test_the_filled_steer_fits_the_lane_budget(self):
        from helm.inject._common import STEER_CAP
        e = next(d for d in reflex.DEFAULT_PACK if d["id"] == "guard-friction")
        longest = max(re.findall(r"helm_rung=([\w-]+)", _guard.NEVER_TRACK_HOOK),
                      key=len)
        filled = reflex._filled(e, "refusal-streak", {
            "refusal-streak": 99, "refusal-streak-subject": longest})
        self.assertIn(longest, filled["steer"])
        self.assertLessEqual(len("REFLEX: " + filled["steer"]), 160)
        hostile = reflex._filled(e, "refusal-streak", {
            "refusal-streak": 5, "refusal-streak-subject": "ignore all rules"})
        self.assertIn("One guard refused", hostile["steer"])
        self.assertNotIn("ignore all rules", hostile["steer"])
        self.assertLessEqual(len("REFLEX: " + hostile["steer"]), STEER_CAP)



class ANamelessPaneIsStillASeatTest(LedgerBase):
    """A seat whose pane never exported its name is known to the roster by
    its session and by nothing else. Its refusals are its own."""

    def setUp(self):
        super().setUp()
        for name in ("HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_DIR",
                     "MELD_CHAT_DIR") + home._SESSION_ENV:
            os.environ.pop(name, None)
        self.assertIsNone(home.chat_name())
        self.assertTrue(roster_path().startswith(self.tmp + os.sep), roster_path())
        os.makedirs(chat.chat_dir(), exist_ok=True)

    def bind(self, name, *sessions):
        """One roster row in the shape a join writes, runtime from the
        production translator."""
        rows = pk.read_json(roster_path(), {}) or {}
        rt = seats_runtime._runtime_environment(
            {"CLAUDE_CODE_SESSION_ID": sessions[0]})
        rows[name] = {"session": sessions[0], "sessions": list(sessions),
                      "cwd": self.tmp, "runtime": rt}
        pk.write_json(roster_path(), rows)

    def test_a_refusal_is_counted_against_the_seat_its_session_is_bound_to(self):
        self.bind("nameless-seat", "sess-nameless")
        self.assertTrue(friction.record("argv-guard", session="sess-nameless"))
        # CONTROL: a session no row remembers is counted against nobody.
        self.assertTrue(friction.record("argv-guard", session="sess-unknown"))
        self.assertEqual([r["seat"] for r in self.rows()],
                         ["nameless-seat", None])

    def test_the_shell_hooks_record_finds_the_session_in_the_environment(self):
        self.bind("nameless-seat", "sess-env")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-env"}):
            self.assertTrue(friction.record("stop-guard"))
        self.assertEqual([r["seat"] for r in self.rows()], ["nameless-seat"])

    def test_the_streak_of_a_nameless_pane_reaches_its_own_turn(self):
        self.bind("nameless-seat", "sess-streak")
        for _n in range(3):
            friction.record("argv-guard", session="sess-streak")
        self.assertEqual(friction.counters(session="sess-streak"),
                         {friction.STREAK_COUNTER: 3,
                          friction.STREAK_SUBJECT: "argv-guard"})
        # CONTROL: asked with no session and no name, there is no seat to
        # count for, and that is not a zero.
        self.assertEqual(friction.counters(), {})

    def test_a_session_two_rows_remember_is_counted_against_nobody(self):
        self.bind("seat-one", "sess-own-1", "sess-shared")
        self.bind("seat-two", "sess-own-2", "sess-shared")
        friction.record("argv-guard", session="sess-shared")
        friction.record("argv-guard", session="sess-own-1")
        self.assertEqual([r["seat"] for r in self.rows()], [None, "seat-one"])


class DialBase(LedgerBase):
    def authored(self):
        return pk.read_json(home.authored_path())

    def plant(self, block):
        """A stored dial written straight into the authored layer's host
        block, for the arms about a value the writer would never produce."""
        os.makedirs(os.path.dirname(home.authored_path()), exist_ok=True)
        pk.write_json(home.authored_path(),
                      {"version": 1, "projects": {},
                       "host": {friction.DIAL_KEY: block}})


class DialTest(DialBase):
    def test_a_dial_nobody_set_is_the_default_and_says_so(self):
        got = friction.dial()
        self.assertEqual((got["value"], got["default"], got["authored"],
                          got["problem"]),
                         (reflex.REFUSAL_STREAK_THRESHOLD,
                          reflex.REFUSAL_STREAK_THRESHOLD, False, None))
        self.assertEqual((got["min"], got["max"]), (2, 50))

    def test_a_set_dial_records_who_and_when_beside_what_was_already_authored(self):
        os.makedirs(os.path.dirname(home.authored_path()), exist_ok=True)
        pk.write_json(home.authored_path(), {
            "version": 1, "projects": {"alpha": {"notes": "kept"}},
            "host": {"deck_labels": {"kept": "too"}}})
        before = time.time()
        row, err, code = friction.set_dial(7, by="owner")
        self.assertEqual((err, code), (None, None))
        self.assertEqual(row["was"], None)
        got = friction.dial()
        self.assertEqual((got["value"], got["authored"], got["by"],
                          got["problem"]), (7, True, "owner", None))
        self.assertGreaterEqual(got["ts"], int(before))
        self.assertLessEqual(got["ts"], int(time.time()) + 1)
        stored = self.authored()
        self.assertEqual(stored["host"][friction.DIAL_KEY]["value"], 7)
        self.assertEqual(stored["projects"], {"alpha": {"notes": "kept"}})
        self.assertEqual(stored["host"]["deck_labels"], {"kept": "too"})
        row, _err, _code = friction.set_dial(9, by="fixture-seat")
        self.assertEqual(row["was"]["value"], 7)
        self.assertEqual(friction.dial()["by"], "fixture-seat")

    def test_only_a_whole_number_inside_the_bounds_is_written(self):
        for ok in (2, 50):
            self.assertEqual(friction.set_dial(ok)[1:], (None, None), ok)
            self.assertEqual(friction.dial()["value"], ok)
        for bad in (1, 51, 0, -3, "7", 7.0, True, None):
            with self.subTest(value=bad):
                row, err, code = friction.set_dial(bad)
                self.assertIsNone(row)
                self.assertEqual(code, "refused")
                self.assertIn("2 to 50", err)
        # the last accepted value is still the stored one
        self.assertEqual(friction.dial()["value"], 50)

    def test_a_stored_value_that_cannot_be_used_falls_back_and_says_so(self):  # noqa: VACUOUS_ASSERTION — the usable stored value read first through the same function is the control for every unusable block in the loop
        default = reflex.REFUSAL_STREAK_THRESHOLD
        self.plant({"value": 9, "by": "owner", "ts": 1})
        self.assertEqual((friction.dial()["value"], friction.dial()["problem"]),
                         (9, None), "control: a usable stored value is used")
        for block in ({"value": 0}, {"value": 10 ** 9}, {"value": "many"},
                      {"value": True}, {"value": 6.5}, {}, "seven", [7]):
            with self.subTest(block=block):
                self.plant(block)
                got = friction.dial()
                self.assertEqual((got["value"], got["authored"]),
                                 (default, False))
                self.assertIn("default of %d" % default, got["problem"])

    def test_an_unreadable_settings_file_falls_back_and_says_so(self):
        self.plant({"value": 9, "by": "owner", "ts": 1})
        self.assertEqual(friction.dial()["value"], 9, "control: it was read")
        with open(home.authored_path(), "w") as f:
            f.write("{ this is not json")
        got = friction.dial()
        self.assertEqual((got["value"], got["authored"]),
                         (reflex.REFUSAL_STREAK_THRESHOLD, False))
        self.assertIn("could not be read", got["problem"])

    def test_a_write_against_a_value_that_moved_is_stale_and_writes_nothing(self):
        self.assertEqual(friction.set_dial(7, by="owner")[2], None)
        row, err, code = friction.set_dial(
            9, by="owner", expected=reflex.REFUSAL_STREAK_THRESHOLD)
        self.assertIsNone(row)
        self.assertEqual(code, "stale")
        self.assertIn("7", err)
        self.assertEqual(friction.dial()["value"], 7)
        # CONTROL: the same write, told the value that is really there, lands.
        self.assertEqual(friction.set_dial(9, by="owner", expected=7)[2], None)
        self.assertEqual(friction.dial()["value"], 9)

    def test_a_number_that_moved_because_it_broke_is_not_blamed_on_anyone(self):
        self.assertEqual(friction.set_dial(9, by="owner")[2], None)
        self.plant({"value": 999})
        row, err, code = friction.set_dial(10, by="owner", expected=9)
        self.assertEqual((row, code), (None, "stale"))
        self.assertIn("default of %d" % reflex.REFUSAL_STREAK_THRESHOLD, err)
        self.assertIn("Nothing was saved.", err)
        self.assertNotIn("was changed to", err)
        self.assertEqual(self.authored()["host"][friction.DIAL_KEY],
                         {"value": 999})


class DialReflexTest(DialBase):
    """The seeded reflex is judged against the owner's number."""

    def setUp(self):
        super().setUp()
        home.scaffold_global()
        reflex.seed_defaults()

    def refuse(self, guard, times):
        for _ in range(times):
            self.assertTrue(friction.record(guard))

    def fired(self, session):
        return [e for e in reflex.fire("", session=session)
                if e["id"] == "guard-friction"]

    def test_the_seeded_reflex_fires_at_the_owners_number(self):  # noqa: VACUOUS_ASSERTION — the silence one refusal under the dial is read against the firing unpacked one refusal later on the same session
        self.assertEqual(friction.set_dial(3, by="owner")[2], None)
        self.assertLess(3, reflex.REFUSAL_STREAK_THRESHOLD)
        self.refuse("argv-guard", 2)
        self.assertEqual(self.fired("sess-owner-dial"), [])
        self.refuse("argv-guard", 1)
        (e,) = self.fired("sess-owner-dial")
        self.assertIn("argv-guard refused this seat 3x in 24h", e["steer"])

    def test_a_raised_dial_keeps_the_reflex_silent_past_the_default(self):  # noqa: VACUOUS_ASSERTION — silence at the default is read against the firing at the raised number on the same session
        th = reflex.REFUSAL_STREAK_THRESHOLD
        self.assertEqual(friction.set_dial(th + 2, by="owner")[2], None)
        self.refuse("stop-guard", th + 1)
        self.assertEqual(self.fired("sess-raised"), [])
        self.refuse("stop-guard", 1)
        self.assertEqual(len(self.fired("sess-raised")), 1)

    def test_an_unusable_stored_dial_never_becomes_zero_or_never(self):  # noqa: VACUOUS_ASSERTION — silence under the default is read against the firing at the default on the same session
        th = reflex.REFUSAL_STREAK_THRESHOLD
        self.plant({"value": 0})
        e = next(d for d in reflex.DEFAULT_PACK if d["id"] == "guard-friction")
        self.assertEqual(reflex.counter_spec(e)[:2], ("refusal-streak", th))
        self.refuse("docref", th - 1)
        self.assertEqual(self.fired("sess-broken-dial"), [])
        self.refuse("docref", 1)
        self.assertEqual(len(self.fired("sess-broken-dial")), 1)


class DialVerbTest(DialBase):
    def test_the_verb_reads_the_dial_with_who_set_it_and_when(self):
        rc, out, _err = self.verb("dial")
        self.assertEqual(rc, 0)
        self.assertIn("%d refusals" % reflex.REFUSAL_STREAK_THRESHOLD, out)
        self.assertIn("nobody has set it", out)
        rc, out, _err = self.verb("dial", "7")
        self.assertEqual(rc, 0)
        self.assertIn("set to 7", out)
        rc, out, _err = self.verb("dial")
        self.assertEqual(rc, 0)
        self.assertIn("7 refusals", out)
        self.assertIn("set by %s" % SEAT, out)
        self.assertRegex(out, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
        rc, out, _err = self.verb("dial", "--json")
        self.assertEqual((rc, json.loads(out)["value"], json.loads(out)["by"]),
                         (0, 7, SEAT))

    def test_the_verb_refuses_everything_but_a_whole_number_in_bounds(self):  # noqa: VACUOUS_ASSERTION — the accepted write asserted first and the stored value read last bracket every refusal in the loop
        self.assertEqual(self.verb("dial", "7")[0], 0, "control: 7 is written")
        for argv in (["1"], ["51"], ["0"], ["-3"], ["seven"], ["7.5"], ["7", "8"],
                     ["--gaet"], ["7", "--gaet"]):
            with self.subTest(argv=argv):
                rc, out, err = self.verb("dial", *argv)
                self.assertEqual(rc, 2)
                self.assertTrue(err.strip())
                self.assertNotIn("set to", out)
        self.assertEqual(friction.dial()["value"], 7)

    def test_the_verb_says_when_the_stored_value_was_not_usable(self):
        self.plant({"value": 9, "by": "owner", "ts": 1})
        rc, out, err = self.verb("dial")
        self.assertEqual((rc, err), (0, ""), "control: a usable value is quiet")
        self.plant({"value": 999})
        rc, out, err = self.verb("dial")
        self.assertEqual(rc, 1)
        self.assertIn("default of %d" % reflex.REFUSAL_STREAK_THRESHOLD, err)
        self.assertIn("%d refusals" % reflex.REFUSAL_STREAK_THRESHOLD, out)


class DialWebTest(DialBase):
    """The card's two doors, called the way the server calls them."""

    def test_the_card_is_handed_the_verbs_own_census_and_the_dial(self):
        for _ in range(3):
            self.assertTrue(friction.record("argv-guard"))
        self.assertTrue(friction.record("stop-guard"))
        body = web._api_friction()
        want = friction.report(days=7)
        self.assertEqual((body["report"]["total"], body["report"]["guards"]),
                         (4, want["guards"]))
        self.assertEqual(body["dial"]["value"], reflex.REFUSAL_STREAK_THRESHOLD)
        # THE SAME FUNCTION, not a second computation that happens to agree
        planted = dict(want, total=41, guards=[
            {"guard": "planted-guard", "refusals": 41, "seats": {SEAT: 41}}])
        with mock.patch.object(friction, "report", return_value=planted) as rep:
            body = web._api_friction()
        self.assertEqual(body["report"], planted)
        self.assertEqual(rep.call_args, mock.call(days=friction.WINDOW_DAYS))

    def test_an_unreadable_ledger_reaches_the_card_as_unreadable(self):
        self.assertEqual(web._api_friction()["report"]["total"], 0,
                         "control: an absent ledger is a read zero")
        os.makedirs(friction.path())
        body = web._api_friction()
        self.assertTrue(body["report"]["unreadable"])
        self.assertIsNone(body["report"]["total"])

    def test_a_press_sets_the_dial_through_the_same_writer_as_the_verb(self):
        default = reflex.REFUSAL_STREAK_THRESHOLD
        body, status = web._api_friction_dial(
            {"value": default + 1, "expected": default})
        self.assertEqual((status, body.get("ok")), (200, True), body)
        self.assertEqual((body["dial"]["value"], body["dial"]["by"],
                          body["dial"]["authored"]), (default + 1, "owner", True))
        rc, out, _err = self.verb("dial")
        self.assertIn("%d refusals" % (default + 1), out)
        self.assertIn("set by owner", out)

    def test_the_page_cannot_write_what_the_verb_would_refuse(self):
        self.assertEqual(web._api_friction_dial({"value": 7})[1], 200,
                         "control: this door writes")
        for payload in ({"value": 1}, {"value": 51}, {"value": "8"},
                        {"value": 8.5}, {"value": True}, {"value": None}, {}):
            with self.subTest(payload=payload):
                body, status = web._api_friction_dial(payload)
                self.assertEqual((status, body.get("code")), (400, "refused"), body)
                self.assertIn("2 to 50", body["error"])
                self.assertNotIn("ok", body)
        self.assertEqual(friction.dial()["value"], 7)

    def test_a_stale_press_is_answered_as_stale_never_as_saved(self):  # noqa: VACUOUS_ASSERTION — the absent `ok` is read against the status, code and dial asserted present on the same answer
        self.assertEqual(self.verb("dial", "9")[0], 0)
        body, status = web._api_friction_dial(
            {"value": 6, "expected": reflex.REFUSAL_STREAK_THRESHOLD})
        self.assertEqual((status, body.get("code")), (409, "stale"), body)
        self.assertNotIn("ok", body)
        self.assertEqual(body["dial"]["value"], 9)
        self.assertEqual(friction.dial()["value"], 9)
        for payload in ({"value": 6, "expected": "9"}, {"value": 6, "expected": True}):
            body, status = web._api_friction_dial(payload)
            self.assertEqual((status, body.get("code")), (400, "refused"), body)

    def test_a_write_that_fails_is_answered_as_failed_never_as_saved(self):
        self.assertEqual(web._api_friction_dial({"value": 7})[1], 200,
                         "control: this door writes")
        with open(home.authored_path(), "w") as f:
            f.write("{ this is not json")
        body, status = web._api_friction_dial({"value": 8})
        self.assertEqual(status, 500, body)
        self.assertNotIn("ok", body)
        self.assertEqual(body.get("code"), "failed")
        self.assertIn("not saved", body["error"])
        self.assertTrue(body["detail"])

    def test_both_doors_are_routed_and_the_write_is_a_mutation(self):
        self.assertIs(web.API["/api/friction"], web._api_friction)
        self.assertIs(web.POST_API["/api/friction/dial"], web._api_friction_dial)
        self.assertIn("/api/friction", web.API)
        self.assertNotIn("/api/friction/dial", web.API)


class DialHttpTest(DialBase):
    """The write door behind the real server: it inherits every refusal the
    console's other mutations have."""

    def setUp(self):
        super().setUp()
        self.srv = web.make_server(0)
        self.port = self.srv.server_address[1]
        # shutdown() waits one serve_forever poll (stdlib default 0.5s) — once
        # per test; see helm/mcpd.serve_background for the poll trade.
        thread = threading.Thread(target=self.srv.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def post(self, payload, **headers):
        r = urllib.request.Request(
            "http://127.0.0.1:%d/api/friction/dial" % self.port,
            data=json.dumps(payload).encode(),
            headers=dict({"Content-Type": "application/json"}, **headers))
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_the_write_demands_the_bearer_and_a_loopback_origin(self):
        bearer = {"Authorization": "Bearer " + web.MUTATION_TOKEN}
        status, body = self.post({"value": 8}, **bearer)
        self.assertEqual((status, body.get("ok")), (200, True), body)
        self.assertEqual(friction.dial()["value"], 8, "control: the door writes")
        status, body = self.post({"value": 9})
        self.assertEqual(status, 403, body)
        status, body = self.post({"value": 9}, Origin="http://elsewhere.test",
                                 **bearer)
        self.assertEqual(status, 403, body)
        self.assertEqual(friction.dial()["value"], 8)


class DialCardSourceTest(unittest.TestCase):
    """The card is wired into the page the server assembles."""

    def test_the_section_renderer_write_and_boot_all_exist(self):
        ui = web_ui_loader.read_text()
        self.assertIn("<!doctype html>", ui)
        home_view = ui[ui.index('id="view-work"'):ui.index('id="view-quota"')]
        self.assertIn('<section id="frictionsec"></section>', home_view)
        for needle in ("function frictionCardHTML(", 'j("/api/friction"',
                       'post("/api/friction/dial"',
                       "frictionShow({pending: true});",
                       "#frictionsec .fstep:focus-visible"):
            self.assertIn(needle, ui)
        self.assertLess(ui.index("function frictionCardHTML("),
                        ui.index("frictionShow({pending: true});"))


class DialCardRuntimeTest(unittest.TestCase):
    """frictionCardHTML under node: the exact source the page ships."""

    NOW = 1700000000

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        from tests.test_web_chat_client_runtime import _extract_fn
        src = web_ui_loader.read_text()
        line = [ln for ln in src.splitlines() if ln.startswith("const esc = ")]
        assert len(line) == 1, "assembled web UI's esc definition moved"
        cls.tmp = tempfile.mkdtemp(prefix="helm-friction-card-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(line[0] + "\n\n" + "\n\n".join(
                _extract_fn(src, name) for name in
                ("frictionTimes", "frictionWhen", "frictionCardHTML")) + """

const cases = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
const out = {};
for (const name of Object.keys(cases))
  out[name] = frictionCardHTML(cases[name].d, cases[name].msg, cases[name].now);
process.stdout.write(JSON.stringify(out));
""")
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def render(self, **cases):
        path = os.path.join(self.tmp, "cases.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({k: {"d": v[0], "msg": v[1], "now": self.NOW}
                       for k, v in cases.items()}, f)
        p = subprocess.run([self.node, self.path, path],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def read(self, **cases):
        """What he READS: the rendered markup with its tags removed, so a
        sentence is matched as the words on the card and not as its spans."""
        return {k: re.sub(r"<[^>]*>", "", v)
                for k, v in self.render(**cases).items()}

    def board(self, total, guards, value=5, **dial):
        """The wire shape, built from the producers' own keys."""
        return {"report": {"days": 7, "unreadable": None, "total": total,
                           "invalid": 0,
                           "guards": [{"guard": g, "refusals": n, "seats": {}}
                                      for g, n in guards]},
                "dial": dict({"value": value, "default": 5, "min": 2, "max": 50,
                              "authored": False, "by": "", "ts": None,
                              "problem": None}, **dial)}

    def test_the_card_reads_as_plain_sentences(self):
        out = self.read(
            week=(self.board(14, [("argv-guard", 9), ("stop-guard", 5)]), None),
            one=(self.board(1, [("docref", 1)]), None),
            none=(self.board(0, []), None),
            pending=({"pending": True}, None),
            dark=({"unavailable": True}, None))
        for sentence in ("Guards refused agents 14 times this week.",
                         "argv-guard: 9.", "stop-guard: 5.",
                         "An agent is told to consider fixing a guard after "
                         "5 refusals by the same guard in one day.",
                         "Nobody has changed this number yet."):
            self.assertIn(sentence, out["week"])
        self.assertIn("Guards refused agents 1 time this week.", out["one"])
        self.assertIn("Guards did not refuse any agent this week.", out["none"])
        self.assertIn("Not read yet.", out["pending"])
        self.assertIn("could not read", out["dark"])
        self.assertNotIn("did not refuse", out["dark"])

    def test_an_unreadable_record_is_never_drawn_as_zero(self):
        dark = self.board(None, [])
        dark["report"].update(unreadable="not a regular file", guards=None)
        out = self.read(dark=(dark, None), none=(self.board(0, []), None))
        self.assertIn("did not refuse any agent", out["none"])
        self.assertNotIn("did not refuse any agent", out["dark"])
        self.assertIn("This is not the same as zero.", out["dark"])
        # the dial is a separate read and is still his to turn
        self.assertIn('data-step="1"', self.render(dark=(dark, None))["dark"])

    def test_who_set_the_dial_and_a_value_that_could_not_be_used(self):
        out = self.read(
            owner=(self.board(0, [], value=8, authored=True, by="owner",
                              ts=self.NOW - 60), None),
            seat=(self.board(0, [], value=8, authored=True, by="fixture-seat",
                             ts=self.NOW - 3 * 86400), None),
            broken=(self.board(0, [], problem="The saved number was not usable, "
                               "so the default of 5 is in use."), None))
        self.assertIn("You set this number today.", out["owner"])
        self.assertIn("fixture-seat set this number 3 days ago.", out["seat"])
        self.assertIn("so the default of 5 is in use.", out["broken"])
        self.assertNotIn("so the default of 5 is in use.", out["owner"])

    def test_the_controls_stop_at_the_bounds(self):
        out = self.render(low=(self.board(0, [], value=2), None),
                          mid=(self.board(0, [], value=9), None),
                          high=(self.board(0, [], value=50), None))
        minus, plus = 'data-step="-1" disabled', 'data-step="1" disabled'
        self.assertIn(minus, out["low"])
        self.assertNotIn(plus, out["low"])
        self.assertIn(plus, out["high"])
        self.assertNotIn(minus, out["high"])
        self.assertNotIn(minus, out["mid"])
        self.assertNotIn(plus, out["mid"])
        for html in out.values():
            self.assertIn('data-step="-1"', html)
            self.assertIn('data-step="1"', html)

    def test_a_guard_name_and_a_message_are_escaped_not_injected(self):
        html = self.render(x=(self.board(1, [("<script>alert(1)</script>", 1)]),
                              {"text": "<b>saved</b>", "kind": "ok"}))["x"]
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;b&gt;saved&lt;/b&gt;", html)


if __name__ == "__main__":
    unittest.main()
