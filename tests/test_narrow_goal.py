#!/usr/bin/env python3
"""The narrow-goal rung (helm/narrow_goal.py), driven through the real
PreToolUse door, `chat.cmd_argv_guard`, and proven both ways: a planted
build act with no answer is refused, and the look-alike beside it passes.

The rung asks the owner's question "am I optimizing on too narrow a goal?"
at the turn's FIRST build act, once, and passes free for a turn that already
wrote its `Wider goal:` line or a seat in an open meld for the task it builds.
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-narrowgoal-", var="HELM_HOME")

from helm import chat, narrow_goal, pk, record  # noqa: E402
from helm.seats_common import _seat_key  # noqa: E402

ANSWER = ("Wider goal: a seat asks before it builds · for: every lane · "
          "waits: the owner · exists: the premise · obvious: why not at plan?")


def _prompt(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _said(text):
    return {"type": "assistant", "message": {
        "role": "assistant", "content": [{"type": "text", "text": text}]}}


def _tool_result():
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}


class Isolated(unittest.TestCase):
    """A private helm home and chat dir, one open turn, one transcript."""

    ENV = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NAME", "MELD_CHAT_NAME",
           "HELM_NARROW_GOAL", "MELD_NARROW_GOAL")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-narrowgoal-")
        self._env = {k: os.environ.get(k) for k in self.ENV}
        for k in self.ENV:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "home")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.makedirs(os.environ["HELM_CHAT_DIR"])
        self.session = "s-narrow"
        self.transcript = os.path.join(self.tmp, self.session + ".jsonl")
        self.open_turn()
        self.write_transcript([_prompt("build the thing")])

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def open_turn(self):
        """A new turn edge, as record.turn_open writes it."""
        sd = record.session_dir(self.session)
        os.makedirs(sd, exist_ok=True)
        path = os.path.join(sd, "counters.json")
        c = pk.read_json(path, {}) or {}
        c["turn-opened-at"] = max(time.time(),
                                  float(c.get("turn-opened-at") or 0) + 1)
        pk.write_json(path, c)

    def write_transcript(self, records):
        with open(self.transcript, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")

    def hook(self, tool, tool_input, agent=None, cwd=None):
        """(rc, stderr) of one PreToolUse call through argv-guard."""
        payload = {"tool_name": tool, "session_id": self.session,
                   "tool_input": tool_input, "cwd": cwd or self.tmp,
                   "transcript_path": self.transcript,
                   "hook_event_name": "PreToolUse"}
        if agent:
            payload["agent_id"] = agent
        out, err = io.StringIO(), io.StringIO()
        stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload))
        try:
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = chat.cmd_argv_guard([])
        finally:
            sys.stdin = stdin
        return rc, err.getvalue()

    def edit(self, **kw):
        return self.hook("Edit", {"file_path": os.path.join(self.tmp, "m.py"),
                                  "old_string": "a", "new_string": "b"}, **kw)


class FirstBuildActTest(Isolated):

    def test_a_build_turn_without_the_line_is_refused_once_and_the_retry_passes(self):
        rc, err = self.edit()
        self.assertEqual(rc, 2)
        self.assertEqual(err.strip(), narrow_goal.LINE)
        self.assertEqual(self.edit(), (0, ""), "the retry must pass")
        rc, _err = self.hook("Bash", {"command": "cat > %s/x.sh <<'EOF'\n"
                                      "echo hi\nEOF" % self.tmp})
        self.assertEqual(rc, 0, "a later build act in the same turn passes")

    def test_the_next_turn_is_asked_again(self):
        self.assertEqual(self.edit()[0], 2)
        self.assertEqual(self.edit()[0], 0)
        self.open_turn()
        self.assertEqual(self.edit()[0], 2)

    def test_a_turn_whose_text_holds_the_line_passes(self):
        self.write_transcript([_prompt("build"), _said("Plan.\n" + ANSWER)])
        self.assertEqual(self.edit(), (0, ""), "the control: plain answer")
        for line in (ANSWER, "- **Wider goal:** ship it · for: x",
                     "> wider goal: lower case still answers"):
            self.open_turn()
            self.write_transcript([
                _prompt("build"), _said("Plan first.\n" + line),
                _tool_result(), _said("Now the edit.")])
            self.assertEqual(self.edit(), (0, ""), line)

    def test_the_line_must_be_in_THIS_turn(self):
        self.write_transcript([
            _prompt("earlier"), _said(ANSWER), _tool_result(),
            _prompt("build this now"), _said("On it.")])
        self.assertEqual(self.edit()[0], 2)

    def test_the_line_must_BEGIN_a_line_of_assistant_text(self):
        self.write_transcript([
            _prompt("build"),
            _said("I will skip the 'Wider goal:' line this time."),
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t", "name": "Write",
                 "input": {"content": "Wider goal: inside a tool input"}}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t",
                 "content": "Wider goal: inside a tool result"}]}}])
        self.assertEqual(self.edit()[0], 2)

    def test_every_build_act_kind_is_asked(self):
        self.assertEqual(self.hook("Workflow", {"script": "x"})[0], 2,
                         "the control: one build act, asked")
        acts = (
            ("Write", {"file_path": os.path.join(self.tmp, "n.py"),
                       "content": "x"}),
            ("NotebookEdit", {"notebook_path": os.path.join(self.tmp, "n.ipynb"),
                              "new_source": "x"}),
            ("Agent", {"description": "build", "prompt": "write the module",
                       "subagent_type": "general-purpose"}),
            ("Workflow", {"script": "agent('x')"}),
            ("Bash", {"command": "helm work claim narrow-goal-check"}),
            ("Bash", {"command": "helm dispatch send codex lane 'go'"}),
            ("Bash", {"command": "helm task add 'a row' --kind build"}),
            ("Bash", {"command": "echo '[Unit]' > ~/.config/systemd/user/x.service"}),
            ("Bash", {"command": "sed -i 's/a/b/' helm/x.py"}),
        )
        for tool, tin in acts:
            self.open_turn()
            self.assertEqual(self.hook(tool, tin)[0], 2, (tool, tin))


class NoBuildActTest(Isolated):

    READS = (
        ("Bash", {"command": "git grep -n narrow_goal -- helm"}),
        ("Bash", {"command": "ls -la 2>/dev/null | head"}),
        ("Bash", {"command": "python3 x.py > /dev/null 2>&1"}),
        ("Bash", {"command": "make 2>err.log >&2"}),
        ("Bash", {"command": "helm chat read --room helm"}),
        ("Bash", {"command": "helm task list"}),
        ("Bash", {"command": "echo 'a > b' && printf \"%s\" \"x>y\""}),
        ("Bash", {"command": "cp a /tmp/claude-1000/x/scratchpad/b"}),
        ("Bash", {"command": "git log --oneline > /tmp/log.txt"}),
        ("Bash", {"command": "cat <<'EOF' | wc -l\nline > file\nEOF"}),
        ("Monitor", {"command": "tail -f build.log", "description": "log"}),
        ("Agent", {"description": "find", "prompt": "where is x",
                   "subagent_type": "Explore"}),
        ("Read", {"file_path": "/etc/hosts"}),
    )

    def test_a_read_only_turn_never_refuses_and_spends_no_latch(self):
        for tool, tin in self.READS:
            self.assertEqual(self.hook(tool, tin), (0, ""), (tool, tin))
        self.assertEqual(self.edit()[0], 2,
                         "reads must not spend the turn's one question")

    def test_build_act_names_reads_None(self):
        self.assertEqual(narrow_goal.build_act("Edit", {}), "Edit",
                         "the control: a build act is named")
        for tool, tin in self.READS:
            self.assertIsNone(narrow_goal.build_act(tool, tin), (tool, tin))


class SwitchAndScopeTest(Isolated):

    def test_the_switch_off_passes(self):
        for off in ("0", "off", "no"):
            os.environ["HELM_NARROW_GOAL"] = off
            self.assertEqual(self.edit(), (0, ""), off)
        os.environ.pop("HELM_NARROW_GOAL")
        self.assertEqual(self.edit()[0], 2, "the control: switched on, asked")

    def test_a_subagent_call_passes(self):
        self.assertEqual(self.edit(agent="a1"), (0, ""))
        self.assertEqual(self.edit()[0], 2, "the subagent spent no latch")

    def test_no_recorded_turn_is_no_block(self):
        os.remove(os.path.join(record.session_dir(self.session),
                               "counters.json"))
        self.assertEqual(self.edit(), (0, ""))

    def test_an_unwritable_latch_is_no_block(self):
        sd = record.session_dir(self.session)
        os.makedirs(os.path.join(sd, narrow_goal.LATCH_FILE))
        self.assertEqual(self.edit(), (0, ""))
        self.assertEqual(self.edit(), (0, ""))


class LineTest(unittest.TestCase):

    def test_the_line_fits_the_cap(self):
        line = narrow_goal.LINE
        self.assertLess(len(line), 250)
        self.assertLessEqual(len(line), narrow_goal.CAP)
        self.assertNotIn("\n", line)
        self.assertIn("am I optimizing on too narrow a goal?", line)
        self.assertIn("'Wider goal: <goal> · for:", line)
        for field in ("waits:", "exists:", "obvious:", "meld"):
            self.assertIn(field, line)


class MeldTest(Isolated):
    """A meld is planning before building: a seat in an open meld for the
    task it builds passes without the line."""

    SEAT = "seat-narrow"

    def setUp(self):
        super().setUp()
        os.environ["HELM_CHAT_NAME"] = self.SEAT

    def meld(self, room, status="active", seat=None):
        seat = seat or self.SEAT
        pk.write_json(os.path.join(
            os.environ["HELM_CHAT_DIR"],
            "%s.meld.%s.json" % (pk.slug(room), _seat_key(seat))),
            {"room": room, "self": seat, "peer": "codex", "peers": ["codex"],
             "status": status, "epoch": int(time.time())})

    def send(self, task):
        return self.hook("Bash", {"command": "helm dispatch send codex lane "
                                  "'build %s' --ref x" % task})

    def test_an_open_meld_for_the_named_task_passes(self):
        self.assertEqual(self.send("task/4199")[0], 2, "the control: no meld")
        for status in ("active", "invited"):
            self.open_turn()
            self.meld("helm-4199", status)
            self.assertEqual(self.send("task/4199"), (0, ""), status)

    def test_a_meld_for_another_task_or_a_closed_one_does_not(self):
        self.meld("helm-4200")
        self.assertEqual(self.send("task/4199")[0], 2)
        self.open_turn()
        self.meld("helm-4199", "done")
        self.assertEqual(self.send("task/4199")[0], 2)
        self.open_turn()
        self.meld("helm-4199", seat="someone-else")
        self.assertEqual(self.send("task/4199")[0], 2)

    def test_the_lane_worktree_names_the_task(self):
        repo = os.path.join(self.tmp, "repo")
        run = lambda *a, cwd=repo: subprocess.run(  # noqa: E731
            ("git",) + a, cwd=cwd, check=True, capture_output=True)
        os.makedirs(repo)
        run("init", "-q", "-b", "main")
        run("-c", "user.email=t@x", "-c", "user.name=t", "commit", "-q",
            "--allow-empty", "-m", "root")
        wt = os.path.join(self.tmp, "repo-wt", "goal-lane")
        run("worktree", "add", "-q", "-b", "lane/goal-lane", wt)
        run("config", "branch.lane/goal-lane.helmTask", "task/4199")
        path = os.path.join(wt, "helm", "m.py")
        self.assertEqual(self.hook("Edit", {"file_path": path, "old_string": "a",
                                            "new_string": "b"})[0], 2,
                         "the control: no meld, asked")
        self.open_turn()
        self.meld("helm-4199")
        self.assertEqual(self.hook("Edit", {"file_path": path, "old_string": "a",
                                            "new_string": "b"}), (0, ""))


if __name__ == "__main__":
    unittest.main()
