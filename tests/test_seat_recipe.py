#!/usr/bin/env python3
"""helm/seat_recipe.py — a seat's LAUNCH RECIPE and the exact resume (task/3695).

Hermetic: HELM_HOME is a tmp tree (the resume live set lives under it), /proc
is a planted tree behind HELM_PROC, and transcripts are written here. The
resume arms themselves live beside the verbs they drive
(tests/test_seat_spawn.py ExactResumeTest, tests/test_orcaadopt.py
AdoptedExactResumeTest); these pin the readers those arms stand on.
"""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests import _tmphome  # noqa: F401 — plants the tmp config roots first

from helm import (accounts, homes, seat, seat_launch_assets, seat_recipe,
                  seat_resume_all)
from tests import _launchrecipe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SID = "55555555-5555-4555-8555-555555555555"
SID2 = "66666666-6666-4666-8666-666666666666"
BOOT = "77777777-7777-4777-8777-777777777777"


def _lines(rows):
    return "".join(json.dumps(r) + "\n" for r in rows)


def _catalog_line():
    """A system line helm's own catalog declares: the one text a recipe
    keeps for --append-system-prompt (free text there is withheld)."""
    return next(f["system_line"] for f in seat.FAMILIES.values()
                if f.get("system_line"))


class ArgvFactsTest(unittest.TestCase):

    def test_every_flag_a_launch_can_carry_is_read(self):
        f = seat_recipe.argv_facts([
            "--disallowedTools", "EnterPlanMode", "Agent(fork)",
            "--dangerously-skip-permissions", "--model", "gpt-6-sol",
            "--effort", "high", "--settings", '{"ultracode":true}',
            "--append-system-prompt", _catalog_line()])
        self.assertEqual(f["disallowed_tools"], ["EnterPlanMode", "Agent(fork)"])
        self.assertEqual(f["permission"], "bypassPermissions")
        self.assertEqual(f["model"], "gpt-6-sol")
        self.assertEqual(f["effort_level"], "high")
        self.assertTrue(f["ultracode"])
        self.assertEqual(f["append_system_prompt"], _catalog_line())
        self.assertEqual(f["withheld"], [])

    def test_an_absent_flag_is_known_only_where_only_a_flag_can_set_it(self):  # noqa: VACUOUS_ASSERTION — the absent flags ARE the contract; test_every_flag_a_launch_can_carry_is_read drives the same reader with every flag present
        """No --disallowedTools IS no denied tools; no --model is NOT a
        model (a settings file may name one), so it reads None."""
        f = seat_recipe.argv_facts(["--resume", SID])
        self.assertEqual(f["disallowed_tools"], [])
        self.assertIsNone(f["append_system_prompt"])
        self.assertIsNone(f["model"])
        self.assertIsNone(f["permission"])

    def test_a_value_outside_the_vocabulary_is_withheld_by_field_name(self):
        """Every valued flag the reader takes, set to a word outside its
        vocabulary: the field is named, its value is gone, and a later
        in-vocabulary occurrence of the flag does not bring it back.
        MUTATION: keep the value of any one flag — that arm's value is
        returned and its field is not named."""
        f = seat_recipe.argv_facts([
            "--model", "two words", "--permission-mode", "warp",
            "--effort", "turbo", "--disallowedTools", "Bash(curl x:*)",
            "--append-system-prompt", "a synthetic private instruction",
            "--model", "gpt-6-sol"])
        self.assertEqual(f["withheld"], ["model", "permission", "effort",
                                         "disallowed_tools",
                                         "append_system_prompt"])
        self.assertEqual((f["model"], f["permission"], f["effort_level"],
                          f["disallowed_tools"], f["append_system_prompt"]),
                         (None, None, None, [], None))
        self.assertEqual(seat_recipe.recipe_argv([
            "--model", "gpt-6-sol", "--effort", "turbo"]),
            ["--model", "gpt-6-sol"])

    def test_the_manual_mode_is_spelled_the_way_the_cli_takes_it(self):
        self.assertEqual(seat_recipe.argv_facts(
            ["--permission-mode=default"])["permission"], "manual")
        self.assertEqual(seat_recipe.permission_words("plan"),
                         ["--permission-mode", "plan"])
        self.assertEqual(seat_recipe.permission_words("bypassPermissions"),
                         ["--dangerously-skip-permissions"])

    def test_session_flags_never_ride_a_recipe(self):
        self.assertEqual(seat_recipe.strip_session(
            ["--model", "m", "--resume", SID, "--continue", "--session-id=x"]),
            ["--model", "m"])


class LaunchCaptureTest(unittest.TestCase):

    def test_a_minted_launch_line_reads_back_as_its_recipe(self):
        """The same parser reads launch.sh and the line helm would mint, so a
        re-mint that carries a recipe is checked on the line itself."""
        line = seat.launch_line("codex", "gpt-5.6-sol", seat="codex")
        cap = seat_recipe.launch_capture(line)
        a = seat_recipe.argv_facts(cap["argv"])
        e = seat_recipe.env_facts(cap["env"])
        self.assertEqual(a["model"], "gpt-5.6-sol")
        self.assertEqual(a["permission"], "bypassPermissions")
        self.assertIn("EnterPlanMode", a["disallowed_tools"])
        self.assertEqual(e["window"], 320000)
        self.assertEqual(e["subagent"], "gpt-5.6-sol")
        self.assertEqual(e["family"], "codex")
        self.assertEqual(e["identity_env"]["HELM_CHAT_NAME"], "codex")
        self.assertTrue(e["config_dir"].endswith(os.path.join("codex",
                                                              "claude")))

    def test_a_recipe_outranks_the_catalog_on_the_line(self):
        line = seat.launch_line("codex", None, seat="codex", recipe={
            "model": "gpt-6-sol", "window": 320000, "subagent": "gpt-5.6-sol",
            "disallowed_tools": ["EnterPlanMode"],
            "append_system_prompt": _catalog_line()})
        cap = seat_recipe.launch_capture(line)
        a = seat_recipe.argv_facts(cap["argv"])
        e = seat_recipe.env_facts(cap["env"])
        self.assertEqual(a["model"], "gpt-6-sol")
        self.assertEqual(e["window"], 320000)       # the catalog says 220000
        self.assertEqual(e["subagent"], "gpt-5.6-sol")
        self.assertEqual(a["disallowed_tools"], ["EnterPlanMode"])
        self.assertEqual(a["append_system_prompt"], _catalog_line())

    def test_without_a_recipe_the_line_is_the_one_it_always_was(self):
        line = seat.launch_line("codex", None, seat="codex")
        self.assertIn("--model %s" % seat.FAMILIES["codex"]["model"], line)
        self.assertEqual(line, seat.launch_line("codex", None, seat="codex",
                                                recipe=None))


class TranscriptFactsTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-recipe-tx-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "t.jsonl")

    def _write(self, rows):
        with open(self.path, "w") as f:
            f.write(_lines(rows))
        return seat_recipe.transcript_facts(self.path)

    @staticmethod
    def _att(model):
        return {"type": "attachment",
                "attachment": {"type": "model", "identity": {"modelId": model}}}

    @staticmethod
    def _reply(model, effort="xhigh"):
        return {"type": "assistant", "effort": effort, "version": "2.1.282",
                "message": {"model": model, "role": "assistant"}}

    @staticmethod
    def _cmd(args=""):
        return {"type": "user", "message": {"role": "user", "content": (
            "<command-name>/model</command-name>\n<command-message>model"
            "</command-message>\n<command-args>%s</command-args>" % args)}}

    def test_the_model_attachment_names_the_exact_model(self):
        """The 1M variant rides the attachment and not the reply."""
        f = self._write([self._att("claude-opus-5-5[1m]"),
                         self._reply("claude-opus-5-5"),
                         {"type": "permission-mode",
                          "permissionMode": "bypassPermissions"}])
        self.assertEqual(f["model"], "claude-opus-5-5[1m]")
        self.assertEqual(f["model_source"], "model attachment")
        self.assertEqual(f["permission"], "bypassPermissions")
        self.assertEqual(f["effort_level"], "xhigh")
        self.assertEqual(f["harness_version"], "2.1.282")

    def test_a_1m_attachment_older_than_the_first_tail_window_is_found(self):
        """A reply names the model WITHOUT its 1M suffix, so a model read
        from a reply is not settled while an attachment may lie further
        back: the read widens until it finds one. (The windows are shrunk
        here; the real first window is 8 MiB, about two hours of a large
        session.) MUTATION: stop at the first window holding any model and
        a permission mode — the resume passes the 200k variant."""
        filler = [{"type": "user", "message": {"role": "user",
                                               "content": "x" * 200}}] * 40
        with mock.patch.object(seat_recipe, "TAIL_WINDOWS", (2048, 1 << 20)):
            f = self._write([self._att("claude-opus-5-5[1m]")] + filler
                            + [{"type": "permission-mode",
                                "permissionMode": "bypassPermissions"},
                               self._reply("claude-opus-5-5")])
        self.assertEqual(f["model"], "claude-opus-5-5[1m]")
        self.assertEqual(f["model_source"], "model attachment")

    def test_a_reply_alone_still_names_the_model(self):
        """The control: with no attachment in the whole file, the reply's
        model stands, after the widest window."""
        filler = [{"type": "user", "message": {"role": "user",
                                               "content": "x" * 200}}] * 40
        with mock.patch.object(seat_recipe, "TAIL_WINDOWS", (2048, 1 << 20)):
            f = self._write(filler + [{"type": "permission-mode",
                                       "permissionMode": "bypassPermissions"},
                                      self._reply("claude-opus-5-5")])
        self.assertEqual(f["model"], "claude-opus-5-5")
        self.assertEqual(f["model_source"], "last reply")

    def test_a_model_command_after_the_attachment_moves_the_model(self):
        """The measured helm-codex case: `/model gpt-6-sol` after the last
        attachment. MUTATION: take the last attachment regardless — the
        recipe names the model the seat left."""
        f = self._write([self._att("gpt-5.6-sol"), self._reply("gpt-5.6-sol"),
                         self._cmd("gpt-6-sol"), self._reply("gpt-6-sol")])
        self.assertEqual(f["model"], "gpt-6-sol")
        self.assertEqual(f["model_source"], "reply after /model")

    def test_an_interactive_model_command_with_no_reply_yet_names_nothing(self):
        f = self._write([self._att("gpt-5.6-sol"), self._reply("gpt-5.6-sol"),
                         self._cmd("")])
        self.assertNotIn("model", f)
        self.assertTrue(f["replies"])

    def test_a_model_argument_that_is_not_one_model_id_names_nothing(self):
        """A /model argument is typed by a person: free text there is never
        read as the model (it would be printed as one), exactly as an empty
        argument is not."""
        f = self._write([self._att("gpt-5.6-sol"), self._reply("gpt-5.6-sol"),
                         self._cmd("a synthetic private note")])
        self.assertNotIn("model", f)
        self.assertTrue(f["replies"])
        f = self._write([self._att("gpt-5.6-sol"), self._reply("gpt-5.6-sol"),
                         self._cmd("gpt-6-sol")])
        self.assertEqual(f["model"], "gpt-6-sol")
        self.assertEqual(f["model_source"], "/model argument")

    def test_a_reply_after_a_1m_model_command_keeps_the_suffix(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a literal, non-empty tuple, so every positive assertion in it runs
        """A transcript from a Claude Code that writes no model attachment
        after /model: the reply after `/model <id>[1m]` names the id WITHOUT
        its suffix, and the command's own argument carries it, in the id or
        the alias form. MUTATION: take the reply alone — the resume passes
        the 200k id under 'resuming EXACTLY'."""
        for picked in ("claude-opus-5-5[1m]", "opus[1m]"):
            with self.subTest(picked=picked):
                f = self._write([self._att("claude-fable-5-1"),
                                 self._reply("claude-fable-5-1"),
                                 self._cmd(picked),
                                 self._reply("claude-opus-5-5")])
                self.assertEqual(f["model"], "claude-opus-5-5[1m]")
                self.assertTrue(f["model_source"].startswith(
                    "reply after /model"), f)

    def test_a_reply_after_a_200k_model_command_names_the_200k_model(self):
        """The control: a /model argument with no suffix adds none."""
        f = self._write([self._att("claude-fable-5-1"),
                         self._reply("claude-fable-5-1"),
                         self._cmd("claude-opus-5-5"),
                         self._reply("claude-opus-5-5")])
        self.assertEqual(f["model"], "claude-opus-5-5")
        self.assertEqual(f["model_source"], "reply after /model")

    def test_an_empty_menu_pick_on_a_claude_id_names_no_model(self):
        """`/model` with no argument is a menu pick, and a reply after it
        drops the 1M suffix the pick may have chosen: on a Claude id the
        model it runs is unknown. MUTATION: take the reply — the 200k id
        rides the resume while the seat ran the 1M one."""
        f = self._write([self._att("claude-fable-5-1"),
                         self._reply("claude-fable-5-1"),
                         self._cmd(""), self._reply("claude-opus-5-5")])
        self.assertNotIn("model", f)
        self.assertIn("menu", f["model_unknown"])

    def test_an_empty_menu_pick_on_another_familys_id_is_its_reply(self):
        """The control: an id with no 1M variant is named by the reply."""
        f = self._write([self._att("gpt-5.6-sol"), self._reply("gpt-5.6-sol"),
                         self._cmd(""), self._reply("gpt-6-sol")])
        self.assertEqual(f["model"], "gpt-6-sol")
        self.assertEqual(f["model_source"], "reply after /model")

    def test_a_typed_alias_answered_by_a_claude_id_is_unknown(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a literal, non-empty tuple, so every positive assertion in it runs
        """`/model default` or `/model opus` can select the 1M model (Claude
        Code prints "Opus 5 (1M context)" for it), and the reply after it
        shows the id without the suffix: like a menu pick, the model is
        unknown. MUTATION: take the reply for any typed argument — the
        resume passes the 200k id under 'resuming EXACTLY'."""
        for typed in ("default", "opus"):
            with self.subTest(typed=typed):
                f = self._write([self._att("claude-fable-5-1"),
                                 self._reply("claude-fable-5-1"),
                                 self._cmd(typed),
                                 self._reply("claude-opus-5-5")])
                self.assertNotIn("model", f)
                self.assertIn(typed, f["model_unknown"])

    def test_sidechain_and_synthetic_rows_never_name_the_model(self):
        f = self._write([self._att("claude-opus-5-5[1m]"),
                         dict(self._att("claude-haiku"), isSidechain=True),
                         self._reply("<synthetic>")])
        self.assertEqual(f["model"], "claude-opus-5-5[1m]")

    def test_ultracode_follows_its_last_enter_or_exit(self):
        enter = {"type": "attachment",
                 "attachment": {"type": "ultra_effort_enter"}}
        leave = {"type": "attachment",
                 "attachment": {"type": "ultra_effort_exit"}}
        self.assertTrue(self._write([enter])["ultracode"])
        self.assertFalse(self._write([enter, leave])["ultracode"])

    def test_the_tail_read_drops_the_record_it_cut(self):
        """A tail window starts mid-record; that fragment is not JSON and
        must not be the reason a field goes missing."""
        rows = [self._att("gpt-6-sol")] + [
            {"type": "user", "message": {"content": "x" * 200}}] * 50 + [
            self._reply("gpt-6-sol"),
            {"type": "permission-mode", "permissionMode": "plan"}]
        with open(self.path, "w") as f:
            f.write(_lines(rows))
        with mock.patch.object(seat_recipe, "TAIL_WINDOWS", (2000, 1 << 20)):
            got = seat_recipe.transcript_facts(self.path)
        self.assertEqual(got["model"], "gpt-6-sol")
        self.assertEqual(got["permission"], "plan")

    def test_a_missing_transcript_states_nothing(self):  # noqa: VACUOUS_ASSERTION — an absent file stating nothing IS the contract; every sibling arm reads facts from a present one
        self.assertEqual(seat_recipe.transcript_facts(
            os.path.join(self.tmp, "absent.jsonl")), {})


class ProcFixture(unittest.TestCase):
    """A planted /proc (HELM_PROC), a tmp HELM_HOME, and a tmp root for the
    credential-home registry (homes.ROOTS), so a registered credhome is a
    directory this fixture makes and no real home is read."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-recipe-proc-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "HELM_PROC": os.path.join(self.tmp, "proc"),
            "HELM_HOME": os.path.join(self.tmp, "helm")})
        env.start()
        self.addCleanup(env.stop)
        self.homes_root = os.path.join(self.tmp, "claude-homes")
        roots = mock.patch.dict(homes.ROOTS, {"claude": self.homes_root})
        roots.start()
        self.addCleanup(roots.stop)

    def _proc(self, pid, argv, environ, start="100"):
        """`start` is the birth stamp /proc/<pid>/stat field 22 carries: two
        incarnations of one pid differ there and nowhere else."""
        d = os.path.join(self.tmp, "proc", str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"\0".join(w.encode() for w in argv) + b"\0")
        with open(os.path.join(d, "environ"), "wb") as f:
            f.write(b"\0".join(("%s=%s" % kv).encode()
                               for kv in environ.items()) + b"\0")
        with open(os.path.join(d, "stat"), "w") as f:
            f.write("%d (claude) S %s %s 0 0\n" % (pid, " ".join(["0"] * 18),
                                                   start))
        return pid


class CaptureTest(ProcFixture):

    def test_a_capture_keeps_no_secret(self):
        """The bearer and every name outside CAPTURE_ENV are dropped at the
        read. MUTATION: keep the whole environ — the token lands in the live
        set on disk."""
        pid = self._proc(4242, ["/opt/claude/versions/2.1.282", "--model",
                                "gpt-6-sol", "--resume", SID],
                         {"ANTHROPIC_AUTH_TOKEN": "sk-planted-secret",
                          "HELM_CHAT_NAME": "seat-under-test", "PATH": "/bin",
                          "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "320000"})
        cap = seat_recipe.capture_process(pid)
        self.assertEqual(cap["argv"], ["--model", "gpt-6-sol"])
        self.assertEqual(cap["env"], {"HELM_CHAT_NAME": "seat-under-test",
                                      "CLAUDE_CODE_MAX_CONTEXT_TOKENS":
                                          "320000"})
        self.assertNotIn("sk-planted-secret", json.dumps(cap))
        # the binary's PATH is never kept, only the version its layout names
        self.assertNotIn("binary", cap)
        self.assertEqual(cap["version"], "2.1.282")

    def test_a_capture_keeps_no_secret_its_argv_carries(self):
        """An argv helm did not mint (an adopted lead's, typed by a person or
        another launcher) can carry a secret: a --settings env block, an
        --mcp-config header. The capture keeps only the recipe's own words,
        which read back as the same recipe. MUTATION: keep the argv whole —
        both planted secrets land in the live set on disk and ride forward
        from record to record."""
        words = ["--model", "opus", "--settings", json.dumps({
            "ultracode": True,
            "env": {"ANTHROPIC_AUTH_TOKEN": "sk-planted-settings"}}),
            "--mcp-config", json.dumps({"mcpServers": {"gh": {"headers": {
                "Authorization": "Bearer sk-planted-mcp"}}}}),
            "--effort", "high", "--disallowedTools", "EnterPlanMode",
            "--permission-mode", "plan",
            "--append-system-prompt", _catalog_line()]
        pid = self._proc(4646, ["/opt/claude/versions/2.1.282"] + words
                         + ["--resume", SID], {})
        cap = seat_recipe.capture_process(pid)
        self.assertNotIn("sk-planted", json.dumps(cap))
        self.assertEqual(seat_recipe.argv_facts(cap["argv"]),
                         seat_recipe.argv_facts(words))
        self.assertTrue(seat_recipe.argv_facts(cap["argv"])["ultracode"])

    def test_a_reused_pid_is_another_process(self):
        """The carry keeps a process's sessions only for the SAME process —
        its pid AND its birth stamp. A spawned seat's pid comes from its
        register pin as a bare int, so the stamp is read, never assumed.
        MUTATION: take the stamp off the pid object alone — a bare pid has
        none, two incarnations of one pid compare equal, and the old session
        is credited to a process that never held it."""
        pid = self._proc(4747, ["/opt/claude", "--model", "gpt-5.6-sol"], {},
                         start="100")
        seat_resume_all.record_live_set(
            [], True, False, BOOT, 1, recipes=seat_recipe.carried(
                seat_recipe.capture_seats([("lead", SID, [pid])])))
        self._proc(4747, ["/opt/claude", "--model", "gpt-6-sol"], {},
                   start="200")
        seat_resume_all.record_live_set(
            [], True, False, BOOT, 1, recipes=seat_recipe.carried(
                seat_recipe.capture_seats([("lead", SID2, [pid])])))
        rec, _path = seat_recipe.captured("lead", SID2)
        self.assertEqual(rec["argv"], ["--model", "gpt-6-sol"])
        self.assertEqual(rec["sessions"], [SID2])
        self.assertEqual(seat_recipe.captured("lead", SID), (None, None))

    def test_an_unreadable_process_captures_nothing(self):  # noqa: VACUOUS_ASSERTION — None for an unread pid IS the contract; test_a_capture_keeps_no_secret reads a planted one on the same reader
        self.assertIsNone(seat_recipe.capture_process(999999))

    def test_the_sweep_capture_rides_the_live_set_and_carries_forward(self):  # noqa: VACUOUS_ASSERTION — the empty population is the contract beside the recipes; the argv read back from the same file is the positive control
        """A LIVE seat's capture lands in the live set's `recipes`; the next
        pass, with the seat gone, still carries it; the same process seen on
        a new session (a /clear) keeps both sessions. MUTATION: write only
        this pass's captures — the dead seat's recipe is gone exactly when a
        resume needs it."""
        pid = self._proc(4343, ["/opt/claude", "--model", "gpt-6-sol"],
                         {"HELM_CHAT_NAME": "lead"})
        fresh = seat_recipe.capture_seats([("lead", SID, [pid])])
        line, failed = seat_resume_all.record_live_set(
            [], True, False, BOOT, 1, recipes=seat_recipe.carried(fresh))
        self.assertFalse(failed, line)
        rec, _path = seat_recipe.captured("lead", SID)
        self.assertEqual(rec["argv"], ["--model", "gpt-6-sol"])
        # the next pass: the seat is not live, nothing fresh
        seat_resume_all.record_live_set(
            [], True, False, BOOT, 1, recipes=seat_recipe.carried({}))
        self.assertEqual(seat_recipe.captured("lead", SID)[0]["argv"],
                         ["--model", "gpt-6-sol"])
        # the same process on a new session keeps the old one too
        fresh = seat_recipe.capture_seats([("lead", SID2, [pid])])
        seat_resume_all.record_live_set(
            [], True, False, BOOT, 1, recipes=seat_recipe.carried(fresh))
        self.assertIsNotNone(seat_recipe.captured("lead", SID)[0])
        self.assertIsNotNone(seat_recipe.captured("lead", SID2)[0])
        # and the population it rides beside is untouched
        with open(os.path.join(seat_resume_all.live_set_dir(),
                               BOOT + ".json")) as f:
            self.assertEqual(json.load(f)["seats"], {})

    def _captured_at(self, when, session=SID):
        return seat_recipe.capture_record(
            ["--model", "claude-opus-5-5"], {"HELM_CHAT_NAME": "lead"},
            pid=4800, start="100", sessions=[session], captured=when)

    def test_a_pass_never_writes_an_older_capture_over_a_newer_one(self):  # noqa: VACUOUS_ASSERTION — the only absence is the write's own failure flag; the capture read back after the writes is the unconditional positive control
        """Two sweep passes can run at once (the timer and a hand pass).
        One that read the recipes map before the other wrote it must not
        write its older capture of a seat over the newer one. MUTATION:
        write each pass's map whole — the later writer's stale capture
        replaces the one the other pass just took."""
        newer, older = "2026-09-30T12:05:00Z", "2026-09-30T12:00:00Z"
        for recipes in ({"lead": self._captured_at(newer)},   # landed first
                        {"lead": self._captured_at(older)}):  # read before it
            line, failed = seat_resume_all.record_live_set(
                [], True, False, BOOT, 1, recipes=recipes)
            self.assertFalse(failed, line)
        rec, _path = seat_recipe.captured("lead", SID)
        self.assertEqual(rec["captured"], newer)

    def test_the_pass_that_loses_keeps_the_session_it_saw(self):  # noqa: VACUOUS_ASSERTION — the only absence is the write's own failure flag; the capture read back after the writes is the unconditional positive control
        """The same process on two sessions, each pass seeing one (a /clear
        between them): the older pass's write keeps the newer capture AND
        the session only the older one saw. MUTATION: keep the newer record
        alone — the session the older pass saw reads uncaptured."""
        newer, older = "2026-09-30T12:05:00Z", "2026-09-30T12:00:00Z"
        for recipes in ({"lead": self._captured_at(newer, SID2)},
                        {"lead": self._captured_at(older, SID)}):
            line, failed = seat_resume_all.record_live_set(
                [], True, False, BOOT, 1, recipes=recipes)
            self.assertFalse(failed, line)
        self.assertEqual(seat_recipe.captured("lead", SID2)[0]["captured"],
                         newer)
        self.assertEqual(seat_recipe.captured("lead", SID)[0]["captured"],
                         newer)

    def test_a_newer_capture_replaces_an_older_one(self):  # noqa: VACUOUS_ASSERTION — the only absence is the write's own failure flag; the capture read back after the writes is the unconditional positive control
        """The control: in order, the later capture is kept."""
        for when in ("2026-09-30T12:00:00Z", "2026-09-30T12:05:00Z"):
            line, failed = seat_resume_all.record_live_set(
                [], True, False, BOOT, 1,
                recipes={"lead": self._captured_at(when)})
            self.assertFalse(failed, line)
        rec, _path = seat_recipe.captured("lead", SID)
        self.assertEqual(rec["captured"], "2026-09-30T12:05:00Z")

    def test_a_capture_of_another_session_never_speaks_for_this_one(self):
        pid = self._proc(4444, ["/opt/claude", "--model", "gpt-6-sol"], {})
        fresh = seat_recipe.capture_seats([("lead", SID, [pid])])
        seat_resume_all.record_live_set(
            [], True, False, BOOT, 1, recipes=seat_recipe.carried(fresh))
        self.assertIsNotNone(seat_recipe.captured("lead", SID)[0])
        self.assertEqual(seat_recipe.captured("lead", SID2), (None, None))

    def test_a_dry_run_records_no_recipe(self):
        line, _ = seat_resume_all.record_live_set(
            [], False, False, BOOT, 1, recipes={"lead": {}})
        self.assertIn("DRY RUN", line)
        self.assertFalse(os.path.exists(os.path.join(
            seat_resume_all.live_set_dir(), BOOT + ".json")))


class ReadTest(ProcFixture):
    """The per-field precedence: the transcript for what moves while a
    process runs, then the live process, the capture, the last launch."""

    def _transcript(self, rows):
        path = os.path.join(self.tmp, SID + ".jsonl")
        with open(path, "w") as f:
            f.write(_lines(rows))
        return path

    def test_the_transcript_outranks_the_argv_it_was_started_with(self):
        """meta-claude's measured shape: started `--model fable`, moved by a
        /model; the model it RUNS is the transcript's."""
        pid = self._proc(5151, ["/opt/claude", "--dangerously-skip-permissions",
                                "--model", "fable"],
                         {"HELM_CHAT_NAME": "lead"})
        tx = self._transcript([
            {"type": "attachment", "attachment": {
                "type": "model", "identity": {"modelId": "claude-opus-5-5[1m]"}}},
            {"type": "assistant", "effort": "xhigh",
             "message": {"model": "claude-opus-5-5"}},
            {"type": "permission-mode", "permissionMode": "acceptEdits"}])
        r = seat_recipe.read("claude", "lead", SID, transcript=tx, cwd=self.tmp,
                             pid=pid)
        self.assertEqual(r.fields["model"], "claude-opus-5-5[1m]")
        self.assertEqual(r.fields["permission"], "acceptEdits")
        self.assertEqual(r.fields["effort"],
                         {"level": "xhigh", "ultracode": False})
        self.assertEqual(r.fields["config_dir"], seat_recipe.default_home())
        self.assertEqual(r.missing, [])
        self.assertEqual(seat_recipe.verdict(r), "resumes exactly")

    def test_a_reply_alone_keeps_the_1m_suffix_its_launch_names(self):
        """A reply names the model WITHOUT its 1M suffix, so a transcript
        whose only model record is a reply (a /clear's session, or an
        attachment past the widest tail) must not strip the suffix the
        launch it is read with names. MUTATION: let the reply outrank the
        launch outright — the exact resume passes the base id and the seat
        loses its 1M window under 'resuming EXACTLY'."""
        tx = self._transcript([
            {"type": "assistant", "effort": "xhigh",
             "message": {"model": "claude-opus-5-5"}},
            {"type": "permission-mode", "permissionMode": "acceptEdits"}])
        for launched, runs in (("claude-opus-5-5[1m]", "claude-opus-5-5[1m]"),
                               ("claude-opus-5-5", "claude-opus-5-5"),
                               ("fable", "claude-opus-5-5")):
            with self.subTest(launched=launched):
                pid = self._proc(5152, ["/opt/claude", "--model", launched],
                                 {"HELM_CHAT_NAME": "lead"})
                r = seat_recipe.read("claude", "lead", SID, transcript=tx,
                                     cwd=self.tmp, pid=pid)
                self.assertEqual(r.fields.get("model"), runs, r.notes)
                self.assertEqual(r.missing, [], r.notes)

    def test_a_reply_alone_keeps_the_1m_suffix_its_alias_launch_names(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a literal, non-empty tuple, so every positive assertion in it runs
        """`--model opus[1m]` launches the 1M variant of the opus id a reply
        names, so the reply alone keeps that suffix too; an alias of another
        family does not. MUTATION: match the exact id only — an alias launch
        loses its 1M window under 'resuming EXACTLY'."""
        tx = self._transcript([
            {"type": "assistant", "effort": "xhigh",
             "message": {"model": "claude-opus-5-5"}},
            {"type": "permission-mode", "permissionMode": "acceptEdits"}])
        for launched, runs in (("opus[1m]", "claude-opus-5-5[1m]"),
                               ("sonnet[1m]", "claude-opus-5-5")):
            with self.subTest(launched=launched):
                pid = self._proc(5153, ["/opt/claude", "--model", launched],
                                 {"HELM_CHAT_NAME": "lead"})
                r = seat_recipe.read("claude", "lead", SID, transcript=tx,
                                     cwd=self.tmp, pid=pid)
                self.assertEqual(r.fields.get("model"), runs, r.notes)

    def test_an_empty_menu_pick_leaves_the_model_unknown_whatever_ran(self):
        """A lead whose last model move was an empty menu pick on a Claude
        id: the model is unknown by name, and no launch source (the live
        process's --model is the model it STARTED on) fills it. MUTATION:
        let the launch's model fill the gap — the resume passes a model the
        seat left."""
        tx = self._transcript([
            {"type": "attachment", "attachment": {
                "type": "model", "identity": {"modelId": "claude-fable-5-1"}}},
            {"type": "user", "message": {"role": "user", "content": (
                "<command-name>/model</command-name>\n<command-message>model"
                "</command-message>\n<command-args></command-args>")}},
            {"type": "assistant", "effort": "xhigh",
             "message": {"model": "claude-opus-5-5"}},
            {"type": "permission-mode", "permissionMode": "acceptEdits"}])
        pid = self._proc(5154, ["/opt/claude", "--model", "fable"],
                         {"HELM_CHAT_NAME": "lead"})
        r = seat_recipe.read("claude", "lead", SID, transcript=tx,
                             cwd=self.tmp, pid=pid)
        self.assertNotIn("model", r.fields, r.notes)
        self.assertIn("model", r.missing)
        self.assertTrue(any("menu" in n for n in r.notes), r.notes)

    def test_an_unknown_model_has_its_own_label_in_the_recipe(self):
        """`seat recipe` names why the model is unknown in its own words,
        never as a value outside its allow-list, which it is not.
        MUTATION: keep the unknown among the withheld fields — the recipe
        says the transcript set a value it never kept."""
        tx = self._transcript([
            {"type": "user", "message": {"role": "user", "content": (
                "<command-name>/model</command-name>\n<command-message>model"
                "</command-message>\n<command-args></command-args>")}},
            {"type": "assistant", "effort": "xhigh",
             "message": {"model": "claude-opus-5-5"}},
            {"type": "permission-mode", "permissionMode": "acceptEdits"}])
        r = seat_recipe.read("claude", "lead", SID, transcript=tx,
                             cwd=self.tmp)
        line = [l for l in seat_recipe.render(
            "lead", (r, {}, None, seat_recipe.CLAUDE_FIELDS))
            if l.strip().startswith("model ")]
        self.assertEqual(len(line), 1, line)
        self.assertIn("UNKNOWN", line[0])
        self.assertIn("menu pick", line[0])
        self.assertNotIn("allow-list", line[0])

    def test_with_no_argv_source_a_lead_is_saved_only(self):
        """The Saved-only answer names exactly what no source knows."""
        tx = self._transcript([
            {"type": "attachment", "attachment": {
                "type": "model", "identity": {"modelId": "claude-opus-5-5"}}},
            {"type": "assistant", "effort": "high",
             "message": {"model": "claude-opus-5-5"}},
            {"type": "permission-mode", "permissionMode": "default"}])
        home = os.path.join(self.homes_root, "lead")        # a credhome
        os.makedirs(home)
        r = seat_recipe.read("claude", "lead", SID, transcript=tx, cwd=self.tmp,
                             credhome=home)
        self.assertEqual(r.missing, ["disallowed_tools", "append_system_prompt",
                                     "identity_env"])
        self.assertEqual(r.fields["permission"], "manual")
        self.assertEqual(seat_recipe.verdict(r),
                         "saved only: disallowed_tools, append_system_prompt, "
                         "identity_env unknown")


class SessionStartRecordTest(ProcFixture):
    """The launch record is written by helm's SessionStart hook, the one
    place every real launch of a session passes, with the capture of the
    process that started (CLAUDE_PID, which Claude Code gives its hooks)."""

    ARGV = ["/opt/claude/versions/2.1.285", "--model", "claude-opus-5-5[1m]",
            "--permission-mode", "acceptEdits", "--disallowedTools", "WebFetch"]

    def _home(self, name="home-a"):
        where = os.path.join(self.homes_root, name)
        os.makedirs(where, exist_ok=True)
        return os.path.realpath(where)

    def _start(self, source="resume", pid=6161, home=None, session=SID,
               plant=True, argv=None):
        """The hook's own call: the payload Claude Code hands it, and the
        environment it runs in. `plant` False: the process could not be
        read."""
        home = home or self._home()
        if plant:
            self._proc(pid, argv or self.ARGV,
                       {"HELM_CHAT_NAME": "lead", "CLAUDE_CONFIG_DIR": home})
        return seat_recipe.record_session_start(
            {"hook_event_name": "SessionStart", "source": source,
             "session_id": session, "cwd": self.tmp,
             "transcript_path": os.path.join(self.tmp, session + ".jsonl")},
            environ={"CLAUDE_PID": str(pid), "CLAUDE_CODE_SESSION_ID": session,
                     "CLAUDE_CONFIG_DIR": home, "HELM_CHAT_NAME": "lead"})

    def _rows(self):
        from helm import eventledger, home
        rows, unread = eventledger.checked_events(
            os.path.join(home.global_dir(), "seat-launches.jsonl"))
        self.assertIsNone(unread)
        return rows

    def _transcript(self):
        path = os.path.join(self.tmp, SID + ".jsonl")
        with open(path, "w") as f:
            f.write(_lines([
                {"type": "attachment", "attachment": {"type": "model",
                 "identity": {"modelId": "claude-opus-5-5[1m]"}}},
                {"type": "assistant", "effort": "xhigh",
                 "message": {"model": "claude-opus-5-5"}},
                {"type": "permission-mode", "permissionMode": "acceptEdits"}]))
        return path

    def _sweep_capture(self, pid=4242):
        """What the reboot sweep kept of an EARLIER process of the session."""
        line, failed = seat_resume_all.record_live_set(
            [], True, False, BOOT, 1, recipes={
                "lead": seat_recipe.capture_record(
                    ["--model", "fable", "--disallowedTools", "Bash"],
                    {"HELM_CHAT_NAME": "lead"}, pid=pid, start="100",
                    sessions=[SID], captured="2026-09-29T00:00:00Z")})
        self.assertFalse(failed, line)

    def test_each_process_start_records_its_launch_with_its_capture(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a literal, non-empty tuple, so every positive assertion in it runs
        """startup, resume (--resume, --continue, an in-session /resume,
        Orca's relaunch, a hand `claude --resume`) and fork each start a
        process on the session: one row, naming the session, the seat, the
        home as a path, and the capture of that process. MUTATION: no row
        from the hook — a launch that names no session id leaves the older
        capture binding, and the payer moves back unprinted."""
        home = self._home()
        for n, source in enumerate(("startup", "resume", "fork")):
            with self.subTest(source=source):
                self.assertIsNone(self._start(source=source, home=home))
                row = self._rows()[-1]
                self.assertEqual(len(self._rows()), n + 1)
                self.assertEqual(row["session"], SID)
                self.assertEqual(row["source"], source)
                self.assertEqual(row["seat"], "lead")
                self.assertEqual(row["home"], home)
                self.assertEqual(row["model"], "claude-opus-5-5[1m]")
                self.assertEqual((row["capture"]["pid"],
                                  row["capture"]["start"]), (6161, "100"))
                self.assertIn("--disallowedTools", row["capture"]["argv"])

    def test_compact_and_an_unread_clear_record_nothing(self):  # noqa: VACUOUS_ASSERTION — no row is the contract for a source that starts no process; the sibling arms on the same fixture record one row per starting source and per captured clear
        """The control: compact keeps the session in the same process, and a
        clear whose process did not read has no capture to carry, so neither
        writes a row (a capture-less clear row would unbind the capture the
        sweep keeps of that same process)."""
        self.assertIsNone(self._start(source="compact"))
        self.assertIsNone(self._start(source="clear", plant=False, pid=6999))
        self.assertEqual(self._rows(), [])

    def test_a_clear_records_the_same_processs_capture(self):  # noqa: VACUOUS_ASSERTION — the one row, its source and its capture pid are the unconditional positive controls
        """/clear moves the process onto a new session id: its row carries
        that same process's capture, so the cleared session resumes
        exactly. MUTATION: skip clear — the cleared session is uncaptured
        until a sweep sees it."""
        self.assertIsNone(self._start(source="clear"))
        rows = self._rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["source"], "clear")
        self.assertEqual(rows[0]["capture"]["pid"], 6161)

    def test_a_print_mode_helper_writes_no_row(self):  # noqa: VACUOUS_ASSERTION — no row is the contract for a `claude -p` helper; test_each_process_start_records_its_launch_with_its_capture records one on the same fixture
        """A `claude -p --continue` (or `-p --resume <sid>`) helper run in
        the seat's cwd starts the seat's session in a helper process that is
        not the seat: it writes no row, the rule the sweep applies
        (`_one_interactive`). MUTATION: record it — the seat's next resume
        binds the helper's recipe."""
        for flag in ("-p", "--print"):
            self.assertIsNone(self._start(argv=self.ARGV + [flag,
                                                            "--continue"]))
        self.assertEqual(self._rows(), [])

    def test_the_newest_session_start_row_is_the_capture(self):
        """The row IS the freshest capture: a resume of the session binds
        it, over the sweep's capture of an earlier process. MUTATION: bind
        the sweep's capture first — the resume restores the denied tools
        and model an earlier process started with."""
        self._sweep_capture()
        self._start()
        r = seat_recipe.read("claude", "lead", SID,
                             transcript=self._transcript(), cwd=self.tmp)
        self.assertEqual(r.missing, [], r.notes)
        self.assertEqual(r.fields["disallowed_tools"], ["WebFetch"])
        self.assertIn("pid 6161", r.sources["disallowed_tools"])
        self.assertEqual(r.fields["config_dir"], self._home())

    def test_a_row_whose_process_did_not_read_binds_no_capture(self):
        """A launch recorded without a readable process is still the last
        launch: the sweep's older capture binds nothing, and the note says
        why. MUTATION: skip a row with no capture — the older capture binds
        over a launch it does not describe."""
        self._sweep_capture()
        self._start(plant=False)
        r = seat_recipe.read("claude", "lead", SID,
                             transcript=self._transcript(), cwd=self.tmp)
        self.assertIn("disallowed_tools", r.missing)
        self.assertTrue(any("SessionStart resume" in n for n in r.notes),
                        r.notes)

    def test_a_refused_append_leaves_a_marker_that_binds_nothing(self):
        """The record fails closed: an append it refuses leaves a marker for
        the session, read as an unread record, so no capture binds its next
        resume, and the note names the marker. The session itself starts
        either way. MUTATION: a WARNING alone — the next resume binds the
        pre-launch capture silently."""
        from helm import home
        os.makedirs(os.path.join(home.global_dir(), "seat-launches.jsonl"))
        self._sweep_capture()
        why = self._start()
        self.assertIsNotNone(why)
        marker = os.path.join(home.global_dir(), "seat-launches.unrecorded",
                              SID)
        self.assertTrue(os.path.exists(marker), why)
        os.rmdir(os.path.join(home.global_dir(), "seat-launches.jsonl"))
        r = seat_recipe.read("claude", "lead", SID,
                             transcript=self._transcript(), cwd=self.tmp)
        self.assertIn("disallowed_tools", r.missing)
        self.assertTrue(any(marker in n for n in r.notes), r.notes)

    def test_a_held_record_lock_is_waited_on_boundedly(self):  # noqa: VACUOUS_ASSERTION — the returned reason, the bounded wait and the marker on disk are the unconditional positive controls
        """A wedged lock holder must not hang a session start: the hook
        waits a bounded time, then leaves the marker. MUTATION: wait on the
        lock with no timeout — every session start on the host hangs."""
        import fcntl
        from helm import home
        path = os.path.join(home.global_dir(), "seat-launches.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".lock", "a") as held, \
                mock.patch.object(seat_recipe, "LAUNCH_LOCK_WAIT_S", 0.05):
            fcntl.flock(held, fcntl.LOCK_EX)
            why = self._start()
        self.assertIn("not taken within", why)
        self.assertTrue(os.path.exists(os.path.join(
            home.global_dir(), "seat-launches.unrecorded", SID)))


class FreeTextIsNeverKeptTest(ProcFixture):
    """An adopted lead's argv is not under helm's token law: a person or
    another launcher wrote it, so --append-system-prompt, or a freeform
    --disallowedTools word, can carry a secret or personal data. A recipe
    keeps only a closed vocabulary (model ids, permission modes, effort
    levels, tool names helm knows, the ultracode setting); a value outside
    it is never persisted and never printed, and its field is recorded as
    unknown BY NAME, so the exact resume refuses naming the field and
    `--defaults` prints the field, never the value. Both planted values are
    synthetic."""

    SEAT = "lead"
    PROMPT = "synthetic-fake-token-in-a-prompt-7f3a91"
    TOOL = "synthetic-fake-token-as-a-tool-c41d07"
    MODEL = "claude-opus-5-5[1m]"

    def _lead(self, pid=4848):
        """A running lead: its transcript says what it runs now, its argv
        carries both planted values, and its environ names its home."""
        # under helm's own home: a config dir must lie under a known root
        self.home = os.path.join(self.homes_root, "lead")   # a credhome
        os.makedirs(self.home, exist_ok=True)
        self.transcript = os.path.join(self.tmp, SID + ".jsonl")
        with open(self.transcript, "w") as f:
            f.write(_lines([
                {"type": "permission-mode",
                 "permissionMode": "bypassPermissions"},
                {"type": "attachment", "attachment": {
                    "type": "model", "identity": {"modelId": self.MODEL}}},
                {"type": "assistant", "effort": "xhigh",
                 "message": {"model": "claude-opus-5-5"}}]))
        self.row = {"i": SID, "h": "claude", "cwd": self.tmp,
                    "p": self.transcript}
        return self._proc(pid, [
            "/opt/claude/versions/2.1.282", "--model", self.MODEL,
            "--effort", "xhigh", "--dangerously-skip-permissions",
            "--disallowedTools", "EnterPlanMode", self.TOOL,
            "--append-system-prompt", self.PROMPT, "--resume", SID],
            {"HELM_CHAT_NAME": self.SEAT, "HELM_CELL_PROFILE": "p-lead",
             "DREGG_PROFILE": "p-lead", "CLAUDE_CONFIG_DIR": self.home})

    def _sweep(self, pid):
        """One `seat resume --all --apply` pass that finds the lead LIVE."""
        line, failed = seat_resume_all.record_live_set(
            [], True, False, BOOT, 1, recipes=seat_recipe.carried(
                seat_recipe.capture_seats([(self.SEAT, SID, [pid])])))
        self.assertFalse(failed, line)
        with open(os.path.join(seat_resume_all.live_set_dir(),
                               BOOT + ".json")) as f:
            return f.read()

    def _leaked(self, text):
        return [s for s in (self.PROMPT, self.TOOL) if s in text]

    def test_the_live_set_on_disk_keeps_no_free_text(self):
        """MUTATION: `recipe_argv` spells back every value `argv_facts` read
        (the reviewer's reproduction) — both planted values land in the live
        set on disk and ride forward from record to record."""
        disk = self._sweep(self._lead())
        self.assertIn(self.MODEL, disk)          # the record IS on disk
        self.assertEqual(self._leaked(disk), [])
        rec, _path = seat_recipe.captured(self.SEAT, SID)
        self.assertEqual(sorted(rec["withheld"]),
                         ["append_system_prompt", "disallowed_tools"])
        # the positive control on the same record: the vocabulary is kept
        self.assertEqual(seat_recipe.argv_facts(rec["argv"])["model"],
                         self.MODEL)

    def test_the_exact_resume_refuses_by_field_name_and_prints_no_value(self):  # noqa: VACUOUS_ASSERTION — the empty relaunch words ARE a refusal's contract, and the named fields and change lines are this arm's positive controls; test_the_vocabulary_still_round_trips drives adopted_plan to non-empty words
        """MUTATION: read the capture's words as the whole recipe — a field
        whose value was dropped reads as ABSENT, the lead resumes 'exactly'
        without its instructions, and the relaunch words carry the value."""
        self._sweep(self._lead())
        plan = seat_recipe.adopted_plan(self.SEAT, dict(self.row), self.home)
        self.assertIsNotNone(plan.refusal, plan.lines)
        for field in ("append_system_prompt", "disallowed_tools"):
            self.assertIn(field, plan.refusal)
        self.assertEqual(plan.extra, ())
        chose = seat_recipe.adopted_plan(self.SEAT, dict(self.row), self.home,
                                         defaults=True)
        self.assertIsNone(chose.refusal)
        said = "\n".join(chose.lines)
        self.assertRegex(said, r"append_system_prompt\s+UNKNOWN -> \(none\)")
        self.assertRegex(said, r"disallowed_tools\s+UNKNOWN -> \(none\)")
        printed = "\n".join([plan.refusal] + plan.lines + chose.lines
                            + list(plan.extra) + list(chose.extra))
        self.assertIn("append_system_prompt", printed)
        self.assertEqual(self._leaked(printed), [])

    def test_seat_recipe_prints_no_value_of_a_running_lead(self):
        """The read-only verb reads the LIVE process of a running lead, which
        no capture filtered. MUTATION: cut only the capture — the verb prints
        both planted values, in its table and in --json."""
        pid = self._lead()
        from helm import orcaadopt, sessions
        printed, rcs = [], []
        with mock.patch.object(seat_recipe, "view",
                               lambda name: seat_recipe._adopted_view(name)), \
                mock.patch.object(orcaadopt, "newest_session_row",
                                  return_value=(dict(self.row), None)), \
                mock.patch.object(sessions, "credhome_for",
                                  return_value=self.home), \
                mock.patch.object(seat_recipe, "live_pid", return_value=pid), \
                mock.patch.object(sessions, "live_sids",
                                  return_value={SID: pid}):
            for argv in ([self.SEAT], [self.SEAT, "--json"]):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(out):
                    rcs.append(seat_recipe.cmd_recipe(argv))
                printed.append(out.getvalue())
        self.assertEqual(self._leaked("\n".join(printed)), [])
        self.assertEqual(rcs, [1, 1], printed)
        self.assertIn("saved only: disallowed_tools, append_system_prompt "
                      "unknown", printed[0])
        self.assertRegex(printed[0], r"model\s+claude-opus-5-5\[1m\]")

    def test_a_record_carried_forward_is_cut_again(self):
        """A record an earlier build wrote whole is carried pass to pass;
        carrying it must not write its free text again. MUTATION: carry the
        prior map as it is read — both values are rewritten every pass."""
        d = seat_resume_all.live_set_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "old-boot.json"), "w") as f:
            json.dump({"v": 1, "boot_id": "old-boot", "btime": 1,
                       "written": "x", "seats": {}, "recipes": {self.SEAT: {
                           "binary": "/opt/claude/versions/2.1.282",
                           "argv": ["--model", self.MODEL,
                                    "--disallowedTools", self.TOOL,
                                    "--append-system-prompt", self.PROMPT],
                           "env": {"HELM_CHAT_NAME": self.SEAT},
                           "pid": 1, "start": "1", "sessions": [SID],
                           "captured": "x"}}}, f)
        seat_resume_all.record_live_set(
            [], True, False, BOOT, 1, recipes=seat_recipe.carried({}))
        with open(os.path.join(d, BOOT + ".json")) as f:
            disk = f.read()
        self.assertIn(self.MODEL, disk)          # the record WAS carried
        self.assertEqual(self._leaked(disk), [])
        rec = json.loads(disk)["recipes"][self.SEAT]
        self.assertEqual(rec["argv"], ["--model", self.MODEL])
        self.assertEqual(sorted(rec["withheld"]),
                         ["append_system_prompt", "disallowed_tools"])
        self.assertEqual(rec["sessions"], [SID])

    def test_the_vocabulary_still_round_trips(self):
        """The positive control: a known tool name (and helm's own deny
        rule), a model id, a mode, an effort level, the ultracode setting and
        helm's own catalog system line are all kept, and the lead resumes
        exactly on them with no transcript at all, except ultracode: no
        resume restores it (owner ruling: every agent starts at high effort,
        and ultracode is not special), and the plan says so."""
        line = next(f["system_line"] for f in seat.FAMILIES.values()
                    if f.get("system_line"))
        # under helm's own home: a config dir must lie under a known root
        self.home = os.path.join(self.homes_root, "lead")   # a credhome
        os.makedirs(self.home)
        pid = self._proc(4949, [
            "/opt/claude/versions/2.1.282", "--model", self.MODEL,
            "--effort", "high", "--permission-mode", "plan",
            "--settings", '{"ultracode":true}',
            "--disallowedTools", "EnterPlanMode", "Agent(fork)",
            "--append-system-prompt", line, "--resume", SID],
            {"HELM_CHAT_NAME": self.SEAT, "HELM_CELL_PROFILE": "p-lead",
             "DREGG_PROFILE": "p-lead", "CLAUDE_CONFIG_DIR": self.home})
        self._sweep(pid)
        rec, _path = seat_recipe.captured(self.SEAT, SID)
        self.assertFalse(rec.get("withheld"))
        a = seat_recipe.argv_facts(rec["argv"])
        self.assertEqual(a["model"], self.MODEL)
        self.assertEqual(a["effort_level"], "high")
        self.assertEqual(a["permission"], "plan")
        self.assertTrue(a["ultracode"])
        self.assertEqual(a["disallowed_tools"], ["EnterPlanMode",
                                                 "Agent(fork)"])
        self.assertEqual(a["append_system_prompt"], line)
        r = seat_recipe.read("claude", self.SEAT, SID, cwd=self.tmp,
                             credhome=self.home)
        self.assertEqual(r.missing, [])
        self.assertEqual(r.fields["effort"], {"level": "high",
                                              "ultracode": True})
        plan = seat_recipe.adopted_plan(
            self.SEAT, {"i": SID, "h": "claude", "cwd": self.tmp}, self.home)
        self.assertIsNone(plan.refusal, plan.lines)
        words = list(plan.extra)
        self.assertEqual(words[words.index("--effort") + 1], "high")
        self.assertNotIn("--settings", words)
        self.assertIn("ultracode: not restored", "\n".join(plan.lines))
        self.assertEqual(words[words.index("--disallowedTools") + 1:
                               words.index("--disallowedTools") + 3],
                         ["EnterPlanMode", "Agent(fork)"])
        self.assertEqual(words[words.index("--append-system-prompt") + 1],
                         line)


class EveryCapturedFieldIsAllowListedTest(ProcFixture):
    """The WHOLE capture, key by key (task/3695). Every value a capture
    persists passes the allow-list for its field; a value outside it is
    never persisted, carried or printed, and its field is recorded as
    withheld BY NAME, so the exact resume refuses naming the field and
    `--defaults` resumes. One arm per field plants a synthetic marker where
    an adopted lead's process could carry it; every marker carries FRAG,
    and FRAG is looked for on disk, in the record carried to the next pass,
    and on every printed surface."""

    SEAT = "lead"
    MODEL = "claude-opus-5-5[1m]"
    FRAG = "5e1b77"
    MARK = "synthetic-marker-5e1b77"        # seat-name shaped, in no list
    SPACED = "synthetic marker 5e1b77"      # outside the seat-name rule too

    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.homes_root, "lead")   # a credhome
        os.makedirs(self.home)
        self.row = {"i": SID, "h": "claude", "cwd": self.tmp}

    def _argv(self, binary="/opt/claude/versions/2.1.282", model=None,
              effort="high", mode="plan", tool="EnterPlanMode", prompt=None):
        """A lead's argv with every value in its list, one replaced."""
        return ([binary, "--model", model or self.MODEL, "--effort", effort,
                 "--permission-mode", mode, "--settings", '{"ultracode":false}',
                 "--disallowedTools", tool]
                + (["--append-system-prompt", prompt] if prompt else [])
                + ["--resume", SID])

    def _env(self, **over):
        env = {"HELM_CHAT_NAME": self.SEAT, "HELM_CELL_PROFILE": "p-lead",
               "DREGG_PROFILE": "p-lead", "CLAUDE_CONFIG_DIR": self.home,
               "HELM_MODEL_FAMILY": "codex",
               "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "320000",
               "CLAUDE_CODE_SUBAGENT_MODEL": "gpt-6-sol"}
        env.update(over)
        return env

    def _write(self, recipes):
        seat_resume_all.record_live_set([], True, False, BOOT, 1,
                                        recipes=recipes)
        with open(os.path.join(seat_resume_all.live_set_dir(),
                               BOOT + ".json")) as f:
            return f.read()

    def _passes(self, pid):
        """(the live set one pass writes, the one the next pass writes with
        the seat gone: its record CARRIED)."""
        disk = self._write(seat_recipe.carried(
            seat_recipe.capture_seats([(self.SEAT, SID, [pid])])))
        return disk, self._write(seat_recipe.carried({}))

    def _printed_claude(self, pid, field, refuses=True):
        """Every surface an adopted lead's recipe prints: the exact resume
        (whose refusal names `field`, when the field is one a resume needs),
        `--defaults`, and `seat recipe` as a table and as --json, on the live
        process and on the capture alone."""
        plan = seat_recipe.adopted_plan(self.SEAT, dict(self.row), self.home)
        chose = seat_recipe.adopted_plan(self.SEAT, dict(self.row), self.home,
                                         defaults=True)
        self.assertIsNone(chose.refusal)
        if refuses:
            self.assertIsNotNone(plan.refusal, plan.lines)
            self.assertIn(field, plan.refusal)
            self.assertEqual(plan.extra, ())
            self.assertRegex("\n".join(chose.lines),
                             r"%s\s+UNKNOWN -> " % field)
        out = [plan.refusal or ""] + plan.lines + list(plan.extra) \
            + chose.lines + list(chose.extra)
        from helm import orcaadopt, sessions
        for live in (pid, None):
            with mock.patch.object(seat_recipe, "view", lambda name: (
                    seat_recipe._adopted_view(name))), \
                    mock.patch.object(orcaadopt, "newest_session_row",
                                      return_value=(dict(self.row), None)), \
                    mock.patch.object(sessions, "credhome_for",
                                      return_value=self.home), \
                    mock.patch.object(seat_recipe, "live_pid",
                                      return_value=live), \
                    mock.patch.object(sessions, "live_sids",
                                      return_value={SID: live}):
                for argv in ([self.SEAT], [self.SEAT, "--json"]):
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf), \
                            contextlib.redirect_stderr(buf):
                        seat_recipe.cmd_recipe(argv)
                    out.append(buf.getvalue())
        return "\n".join(out)

    def _printed_proxy(self, pid, field):
        """A proxy seat's recipe on the same process: the refusal text its
        resume prints (`_incomplete`), `seat recipe`'s table and --json."""
        out = []
        for live in (pid, None):
            r = seat_recipe.read("proxy", self.SEAT, SID, pid=live,
                                 role="worker")
            self.assertIn(field, r.missing)
            self.assertIn(field, r.withheld)
            refusal = seat_recipe._incomplete(r, self.SEAT)
            self.assertIn(field, refusal)
            out += [refusal, json.dumps(r.as_json(), default=str)]
            out += seat_recipe.render(self.SEAT, (r, {}, live,
                                                  seat_recipe.PROXY_FIELDS))
        return "\n".join(out)

    def _arm(self, field, argv=None, env=None, proxy=False):
        pid = self._proc(4900, argv or self._argv(), env or self._env())
        disk, carried = self._passes(pid)
        self.assertIn(self.SEAT, json.loads(disk)["recipes"])
        printed = (self._printed_proxy if proxy else self._printed_claude)(
            pid, field)
        for where, text in (("the live set", disk),
                            ("the carried record", carried),
                            ("a printed surface", printed)):
            self.assertFalse(self.FRAG in text, "the marker reached %s" % where)
        for text in (disk, carried):
            rec = json.loads(text)["recipes"][self.SEAT]
            self.assertIn(field, rec["withheld"])

    # --------------------------------------------------- one arm per field

    def test_model(self):  # noqa: VACUOUS_ASSERTION — the assertions live in _arm, which drives this field's marker through every surface
        """MUTATION: check the model's SHAPE — the marker is one token of id
        characters and persists as the model."""
        self._arm("model", argv=self._argv(model=self.MARK))

    def test_permission(self):  # noqa: VACUOUS_ASSERTION — the assertions live in _arm, which drives this field's marker through every surface
        self._arm("permission", argv=self._argv(mode=self.MARK))

    def test_effort(self):  # noqa: VACUOUS_ASSERTION — the assertions live in _arm, which drives this field's marker through every surface
        self._arm("effort", argv=self._argv(effort=self.MARK))

    def test_disallowed_tools(self):  # noqa: VACUOUS_ASSERTION — the assertions live in _arm, which drives this field's marker through every surface
        self._arm("disallowed_tools", argv=self._argv(tool=self.MARK))

    def test_append_system_prompt(self):  # noqa: VACUOUS_ASSERTION — the assertions live in _arm, which drives this field's marker through every surface
        self._arm("append_system_prompt", argv=self._argv(prompt=self.MARK))

    def test_config_dir(self):  # noqa: VACUOUS_ASSERTION — the assertions live in _arm, which drives this field's marker through every surface
        """MUTATION: keep any CLAUDE_CONFIG_DIR — a path outside every home
        root (a person's own directory) persists."""
        self._arm("config_dir", env=self._env(
            CLAUDE_CONFIG_DIR="/srv/%s/claude" % self.MARK))

    def test_identity_env(self):  # noqa: VACUOUS_ASSERTION — the assertions live in _arm, which drives this field's marker through every surface
        self._arm("identity_env", env=self._env(HELM_CELL_PROFILE=self.SPACED))

    def test_family(self):  # noqa: VACUOUS_ASSERTION — the assertions live in _arm, which drives this field's marker through every surface
        self._arm("family", env=self._env(HELM_MODEL_FAMILY=self.MARK),
                  proxy=True)

    def test_window(self):  # noqa: VACUOUS_ASSERTION — the assertions live in _arm, which drives this field's marker through every surface
        self._arm("window", env=self._env(
            CLAUDE_CODE_MAX_CONTEXT_TOKENS=self.MARK), proxy=True)

    def test_subagent(self):  # noqa: VACUOUS_ASSERTION — the assertions live in _arm, which drives this field's marker through every surface
        self._arm("subagent", env=self._env(
            CLAUDE_CODE_SUBAGENT_MODEL=self.MARK), proxy=True)

    def test_binary(self):
        """The binary is kept only as the VERSION its versions layout names,
        never as a path. MUTATION: keep the path when its layout checks out
        — the marker directory above `versions/` persists."""
        for binary, version in (
                ("/opt/%s/claude/versions/2.1.282" % self.MARK, "2.1.282"),
                ("/opt/%s/launcher" % self.MARK, None)):
            with self.subTest(binary=binary):
                pid = self._proc(4901, self._argv(binary=binary), self._env())
                for text in self._passes(pid):
                    self.assertFalse(self.FRAG in text, "the path persisted")
                    rec = json.loads(text)["recipes"][self.SEAT]
                    self.assertEqual(rec.get("version"), version)

    def test_a_marker_inside_a_version_shape(self):
        """A versions-layout binary whose last part only LOOKS like a version:
        a prerelease suffix carries the marker. Only a plain N.N.N is kept;
        anything else withholds the harness version by name. MUTATION: admit
        a semver prerelease or build suffix — the marker persists as the
        version and `seat recipe` prints it."""
        for suffix in ("synthetic.marker.5e1b77", self.MARK):
            with self.subTest(suffix=suffix):
                binary = "/opt/claude/versions/2.1.282-%s" % suffix
                pid = self._proc(4904, self._argv(binary=binary), self._env())
                for text in self._passes(pid):
                    self.assertFalse(self.FRAG in text, "the suffix persisted")
                    rec = json.loads(text)["recipes"][self.SEAT]
                    self.assertNotIn("version", rec)
                    self.assertIn("harness_version", rec["withheld"])
                r = seat_recipe.read("claude", self.SEAT, SID, cwd=self.tmp,
                                     pid=pid, credhome=self.home)
                self.assertNotIn("harness_version", r.info)
                self.assertIn("harness_version", r.withheld)
                self.assertEqual(r.missing, [])   # a recorded fact never refuses
                shown = "\n".join(seat_recipe.render(self.SEAT, (
                    r, {}, pid, seat_recipe.CLAUDE_FIELDS))) + json.dumps(
                        r.as_json(), default=str)
                self.assertFalse(self.FRAG in shown, "the suffix was printed")
                self.assertIn("harness_version", shown)

    def test_a_marker_child_of_a_home_root(self):
        """A config dir is kept only when it IS a home helm knows, never
        because it lies under one: a child named by the marker, under the
        credhome root or under helm's own home, is withheld by name and its
        path is never written or printed. MUTATION: accept any path under a
        root — the marker child persists as the config dir."""
        for path in (os.path.join(self.homes_root, self.MARK),
                     os.path.join(self.tmp, "helm", self.MARK),
                     os.path.join(self.home, self.MARK)):
            with self.subTest(path=path):
                self._arm("config_dir", env=self._env(CLAUDE_CONFIG_DIR=path))

    def test_the_rest_of_a_record_is_allow_listed_too(self):
        """pid, birth stamp, sessions, the capture time and the seat key are
        the record's own facts, not the process's words, but a record an
        earlier build wrote whole is carried: each passes its list, and a key
        with no list is dropped. MUTATION: carry them as read — the marker in
        every one of them is rewritten each pass."""
        d = seat_resume_all.live_set_dir()
        os.makedirs(d, exist_ok=True)
        rec = {"version": "2.1.282", "argv": ["--model", self.MODEL],
               "env": {"HELM_CHAT_NAME": self.SEAT}, "withheld": [],
               "pid": self.MARK, "start": self.MARK,
               "sessions": [SID, self.MARK], "captured": self.MARK,
               "note": self.MARK}
        with open(os.path.join(d, "old-boot.json"), "w") as f:
            json.dump({"v": 1, "boot_id": "old-boot", "btime": 1,
                       "written": "x", "seats": {}, "recipes": {
                           self.SEAT: rec, self.SPACED: dict(rec)}}, f)
        disk = self._write(seat_recipe.carried({}))
        self.assertFalse(self.FRAG in disk, "a record fact was carried whole")
        kept = json.loads(disk)["recipes"]
        self.assertEqual(sorted(kept), [self.SEAT])
        self.assertEqual(kept[self.SEAT]["sessions"], [SID])
        self.assertNotIn("note", kept[self.SEAT])

    # --------------------------------- the transcript's facts, both ways

    def _transcript(self, model="claude-opus-5-5[1m]", effort="xhigh",
                    mode="bypassPermissions", version="2.1.282",
                    account="aaaaaaaa-0000-4000-8000-000000000001"):
        """A synthetic transcript for the planted lead's session. read() ranks
        its facts over the checked argv, so each is checked where the recipe
        is assembled, whatever its source."""
        path = os.path.join(self.tmp, SID + ".jsonl")
        with open(path, "w") as f:
            f.write(_lines([
                {"type": "permission-mode", "permissionMode": mode},
                {"type": "attachment", "attachment": {
                    "type": "model", "identity": {"modelId": model}}},
                {"type": "assistant", "effort": effort, "version": version,
                 "message": {"model": model}},
                {"type": "bridge-session", "ownerAccountUuid": account}]))
        self.row["p"] = path
        pid = self._proc(4905, self._argv(), self._env())
        self._passes(pid)
        return pid

    def test_a_transcript_model_the_catalog_does_not_know_is_never_printed(self):
        """MUTATION: rank the transcript's model over the checked argv without
        checking it — the marker is printed as the model the lead 'ran
        exactly as'."""
        pid = self._transcript(model=self.MARK)
        printed = self._printed_claude(pid, "model")
        self.assertFalse(self.FRAG in printed, "the transcript's model printed")
        self.assertIn("a model the catalog does not know", printed)

    def test_a_transcript_effort_outside_its_list_is_never_printed(self):
        """MUTATION: check the effort only against the flag's list at relaunch
        — the refusal names the marker as the effort it ran at."""
        pid = self._transcript(effort=self.MARK)
        self.assertFalse(self.FRAG in self._printed_claude(pid, "effort"),
                         "the transcript's effort printed")

    def test_a_transcript_permission_outside_its_list_is_never_printed(self):
        pid = self._transcript(mode=self.MARK)
        self.assertFalse(self.FRAG in self._printed_claude(pid, "permission"),
                         "the transcript's permission mode printed")

    def test_a_transcript_version_or_account_outside_its_list(self):  # noqa: VACUOUS_ASSERTION — the recorded-only fields never refuse; the field named as withheld is each subtest's positive control
        """Recorded-only facts never refuse a resume, but a marker in them is
        never printed either: the field is withheld by name."""
        for field, kw in (("harness_version", {"version": self.MARK}),
                          ("account", {"account": self.FRAG + "-synthetic"})):
            with self.subTest(field=field):
                pid = self._transcript(**kw)
                printed = self._printed_claude(pid, field, refuses=False)
                self.assertFalse(self.FRAG in printed, "%s printed" % field)
                r = seat_recipe.read("claude", self.SEAT, SID,
                                     transcript=self.row["p"], cwd=self.tmp,
                                     credhome=self.home)
                self.assertIn(field, r.withheld)

    def test_a_retired_claude_model_id_is_still_named(self):
        """The other way: a real retired id the catalog no longer lists, in
        the vendor's own grammar, is still the model the lead resumes on and
        is named. MUTATION: allow only catalogued models — the lead can never
        resume exactly on the model it ran."""
        pid = self._transcript(model="claude-opus-4-1")
        plan = seat_recipe.adopted_plan(self.SEAT, dict(self.row), self.home)
        self.assertIsNone(plan.refusal, plan.lines)
        self.assertIn("model claude-opus-4-1", "\n".join(plan.lines))
        self.assertEqual(list(plan.extra)[:2], ["--model", "claude-opus-4-1"])
        r = seat_recipe.read("claude", self.SEAT, SID, transcript=self.row["p"],
                             cwd=self.tmp, pid=pid, credhome=self.home)
        self.assertRegex("\n".join(seat_recipe.render(self.SEAT, (
            r, {}, pid, seat_recipe.CLAUDE_FIELDS))),
            r"model\s+claude-opus-4-1\s+\[transcript")

    # ------------------------------------------------- the positive control

    def test_every_listed_value_round_trips_and_resumes_exactly(self):
        """A catalog model, a real versions path, a mode and an effort claude
        takes, a known tool, a home under a known root, seat-shaped names, a
        family, a window and a catalogued subagent: all kept, nothing
        withheld, and the lead resumes exactly with no transcript at all."""
        pid = self._proc(4902, self._argv(), self._env())
        disk, carried = self._passes(pid)
        for text in (disk, carried):
            rec = json.loads(text)["recipes"][self.SEAT]
            self.assertFalse(rec.get("withheld"), rec)
            self.assertEqual(rec["env"], self._env())
        plan = seat_recipe.adopted_plan(self.SEAT, dict(self.row), self.home)
        self.assertIsNone(plan.refusal, plan.lines)
        self.assertEqual(list(plan.extra), [
            "--model", self.MODEL, "--permission-mode", "plan",
            "--effort", "high", "--disallowedTools", "EnterPlanMode"])
        r = seat_recipe.read("claude", self.SEAT, SID, cwd=self.tmp,
                             credhome=self.home)
        self.assertEqual(r.info["harness_version"], "2.1.282")
        p = seat_recipe.read("proxy", self.SEAT, SID, role="worker")
        self.assertEqual((p.fields["family"], p.fields["window"],
                          p.fields["subagent"]), ("codex", 320000,
                                                  "gpt-6-sol"))

    # -------------------------------------------------- the completeness arm

    def test_every_key_a_capture_writes_has_an_allow_list(self):
        """Enumerate what a capture WRITES — its keys, its environment names,
        its argv flags — and require an allow-list for each, one that
        refuses the marker. A future key, name or flag written without a list
        fails here, and the writer itself refuses a key it has no list for."""
        pid = self._proc(4903, self._argv(prompt=_catalog_line()), self._env())
        rec = json.loads(self._passes(pid)[0])["recipes"][self.SEAT]
        keys = seat_recipe.CAPTURE_KEYS
        self.assertEqual(sorted(set(rec) - set(keys)), [])
        self.assertEqual(sorted(set(rec["env"]) - set(seat_recipe.ENV_KINDS)),
                         [])
        self.assertEqual(sorted(seat_recipe.CAPTURE_ENV),
                         sorted(seat_recipe.ENV_KINDS))
        flags = [w for w in rec["argv"] if w.startswith("--")]
        self.assertEqual(sorted(set(flags) - set(seat_recipe.ARGV_KINDS)), [])
        self.assertEqual(len(flags), 6, rec["argv"])   # every flag was written
        words, flag = rec["argv"], None
        for word in words:
            if word in seat_recipe.ARGV_KINDS:
                flag = word
            else:
                self.assertTrue(seat_recipe.ARGV_KINDS[flag](word),
                                (flag, word))
        for key, keep in keys.items():
            self.assertFalse(keep(self.MARK), key)
        for name, (field, ok) in seat_recipe.ENV_KINDS.items():
            self.assertFalse(ok(self.SPACED), name)
            self.assertIn(field, seat_recipe.VOCAB_FIELDS)
        for flag, ok in seat_recipe.ARGV_KINDS.items():
            if flag != seat_recipe.SKIP_FLAG:
                self.assertFalse(ok(self.MARK), flag)
        with self.assertRaises(KeyError):
            seat_recipe._record(unlisted=self.MARK)
        # and the ASSEMBLED recipe: every field but cwd, and every recorded
        # fact, has a check read() applies whatever the source
        checked = set(seat_recipe.RECIPE_CHECKS)
        self.assertEqual(sorted(set(seat_recipe.PROXY_FIELDS) - checked),
                         ["cwd"])
        self.assertEqual(sorted(set(seat_recipe.INFO_FIELDS) - checked), [])
        for name, ok in seat_recipe.RECIPE_CHECKS.items():
            self.assertFalse(ok(self.MARK), name)


class ResumeSurfacesTest(ProcFixture):
    """What a resume prints and keeps beside the recipe: a refusal offers
    the flag and never a command, a capture keeps the account its home held
    as an opaque key, a flag the recipe does not restore is named and never
    its value, and a renamed proxy seat's identity follows the roster."""

    SEAT = "lead"
    MODEL = "claude-opus-5-5[1m]"
    FRAG = "5e1b77"

    def _home(self, email=None):
        home = os.path.join(self.homes_root, "lead")        # a credhome
        os.makedirs(home, exist_ok=True)
        if email:
            with open(os.path.join(home, ".claude.json"), "w") as f:
                json.dump({"oauthAccount": {
                    "emailAddress": email,
                    "accountUuid": "aaaaaaaa-0000-4000-8000-000000000001"}}, f)
        return home

    def _sweep(self, pid):
        line, failed = seat_resume_all.record_live_set(
            [], True, False, BOOT, 1, recipes=seat_recipe.carried(
                seat_recipe.capture_seats([(self.SEAT, SID, [pid])])))
        self.assertFalse(failed, line)
        with open(os.path.join(seat_resume_all.live_set_dir(),
                               BOOT + ".json")) as f:
            return f.read()

    def test_a_refusal_offers_the_flag_and_never_a_command(self):
        """A refusal names fields, sources and the `--defaults` flag; it
        never hands the operator a command line to type. MUTATION: spell
        the whole `helm seat resume <seat> --defaults` into the refusal."""
        r = seat_recipe.read("claude", "seat-a", SID, cwd=self.tmp)
        self.assertTrue(r.missing)
        texts = [seat_recipe._incomplete(r, "seat-a"),
                 seat_recipe.adopted_plan(
                     "seat-a", {"i": SID, "h": "codex", "cwd": self.tmp},
                     None).refusal]
        for text in texts:
            self.assertIn("--defaults", text)
            self.assertNotIn("`", text)
            self.assertNotIn("helm seat resume", text)

    def test_a_capture_keeps_the_account_its_home_held_as_a_key(self):
        """The account check compares one kind of id on both sides: the
        measured key of the account email the process's home held when it
        was captured, beside the key its home holds at resume. MUTATION:
        capture nothing of the account — the check has no side to compare
        with, and the transcript's bridge-session id is another kind."""
        email = "lead-%s@example.com" % self.FRAG
        home = self._home(email)
        pid = self._proc(4800, ["/opt/claude/versions/2.1.282", "--model",
                                self.MODEL, "--resume", SID],
                         {"HELM_CHAT_NAME": self.SEAT,
                          "CLAUDE_CONFIG_DIR": home})
        disk = self._sweep(pid)
        rec = json.loads(disk)["recipes"][self.SEAT]
        self.assertEqual(rec.get("account"), accounts.measured_key(email))
        # the control: the key is kept, the address never is
        self.assertFalse(self.FRAG in disk, "the account email persisted")

    def test_a_flag_outside_the_recipe_is_named_never_its_value(self):
        """An adopted lead's argv can carry options the recipe does not
        restore. The resume names each by its NAME, from claude's own list,
        and counts any other option-shaped word; no value is ever kept or
        printed. MUTATION: drop them silently — the lead resumes without its
        MCP config and extra directory and nothing says so."""
        home = self._home()
        pid = self._proc(4950, [
            "/opt/claude/versions/2.1.282", "--model", self.MODEL,
            "--effort", "high", "--dangerously-skip-permissions",
            "--settings", '{"ultracode":false}',
            "--mcp-config", "/srv/mcp-%s.json" % self.FRAG,
            "--add-dir", "/srv/dir-%s" % self.FRAG, "--strict-mcp-config",
            "--flag-%s" % self.FRAG, "value-%s" % self.FRAG, "--resume", SID],
            {"HELM_CHAT_NAME": self.SEAT, "HELM_CELL_PROFILE": "p-lead",
             "DREGG_PROFILE": "p-lead", "CLAUDE_CONFIG_DIR": home})
        disk = self._sweep(pid)
        row = {"i": SID, "h": "claude", "cwd": self.tmp}
        plan = seat_recipe.adopted_plan(self.SEAT, dict(row), home)
        self.assertIsNone(plan.refusal, plan.lines)
        text = "\n".join(plan.lines)
        self.assertIn("not restored: --mcp-config, --add-dir, "
                      "--strict-mcp-config, and 1 more option outside "
                      "claude's own list", text)
        chose = seat_recipe.adopted_plan(self.SEAT, dict(row), home,
                                         defaults=True)
        printed = "\n".join([text] + chose.lines + list(plan.extra)
                            + list(chose.extra))
        self.assertIn("not restored: --mcp-config", "\n".join(chose.lines))
        # the control: no value, and no unlisted option name, is kept or shown
        self.assertFalse(self.FRAG in printed, "a value was printed")
        self.assertFalse(self.FRAG in disk, "a value persisted")

    def _renamed_plan(self):
        """A captured codex proxy seat whose roster identity is now seat-b."""
        d = seat._instance_dir("codex", "codex")
        os.makedirs(d, exist_ok=True)
        text = _launchrecipe.launch_sh("codex", "codex")
        with open(os.path.join(d, "launch.sh"), "w") as f:
            f.write(text)
        ran = seat_recipe.launch_capture(text)
        line, failed = seat_resume_all.record_live_set(
            [], True, False, BOOT, 1, recipes={
                "codex": seat_recipe.capture_record(
                    ran["argv"], ran["env"], pid=4700, start="1",
                    sessions=[SID], captured="2026-09-29T00:00:00Z")})
        self.assertFalse(failed, line)
        with mock.patch.object(seat_launch_assets, "_launch_endpoint",
                               return_value=_launchrecipe.FIXTURE_PORT):
            return seat_recipe.proxy_plan(
                "codex", "codex", d, SID, self.tmp, {}, "worker", None, None,
                False, "seat-b", None)

    def test_a_renamed_proxy_seat_resumes_under_the_roster_name(self):
        """The control for the documented rule: the exact resume carries the
        roster's new name and says so, and refuses nothing for it."""
        plan = self._renamed_plan()
        self.assertIsNone(plan.refusal, plan.lines)
        text = "\n".join(plan.lines)
        self.assertIn("identity follows the roster: HELM_CHAT_NAME=codex", text)
        self.assertIn("-> HELM_CHAT_NAME=seat-b", text)

    def test_ultracode_notice_does_not_claim_an_explicit_low_effort_is_high(self):
        note = seat_recipe.ultracode_line({"effort": {"level": "low",
                                                 "ultracode": True}})
        self.assertIn("ultracode: not restored", note)
        self.assertNotIn("high effort", note)

    def test_a_lead_that_ran_with_ultracode_resumes_exactly_without_it(self):  # noqa: VACUOUS_ASSERTION — the absent refusal is paired with unconditional positives on the same plan: its EXACTLY and not-restored lines and its exact extra words
        """A proxy lead whose captured launch carried the retired lead
        default (`--settings {"ultracode":true}`) resumes EXACTLY: the plan
        neither restores ultracode nor refuses over it, and says so (owner
        ruling: every agent starts at high effort, and ultracode is not
        special). MUTATION: the lead role's ultracode back on the launch
        line, and the plan carries it without the line; or refuse the effort
        the launch line can no longer carry, and no lead resumes exactly."""
        d = seat._instance_dir("codex", "codex")
        os.makedirs(d, exist_ok=True)
        text = _launchrecipe.launch_sh("codex", "codex")
        with open(os.path.join(d, "launch.sh"), "w") as f:
            f.write(text)
        ran = seat_recipe.launch_capture(text)
        line, failed = seat_resume_all.record_live_set(
            [], True, False, BOOT, 1, recipes={
                "codex": seat_recipe.capture_record(
                    ran["argv"] + ["--effort", "high",
                                   "--settings", '{"ultracode":true}'],
                    ran["env"], pid=4700, start="1", sessions=[SID],
                    captured="2026-09-29T00:00:00Z")})
        self.assertFalse(failed, line)
        with mock.patch.object(seat_launch_assets, "_launch_endpoint",
                               return_value=_launchrecipe.FIXTURE_PORT):
            plan = seat_recipe.proxy_plan(
                "codex", "codex", d, SID, self.tmp, {}, "lead", None, None,
                False, "codex", None)
        self.assertIsNone(plan.refusal, plan.lines)
        self.assertIs(plan.recipe.fields["effort"]["ultracode"], True)
        said = "\n".join(plan.lines)
        self.assertIn("resuming EXACTLY as it ran", said)
        self.assertIn("ultracode: not restored", said)
        self.assertEqual(list(plan.extra), ["--effort", "high"])

    def _two_sessions(self):
        """A codex proxy seat whose register pins its LIVE process (planted,
        its birth stamp matching) to session SID2; SID is an older session
        of the same seat. -> (seat dir, register)."""
        d = seat._instance_dir("codex", "codex")
        proj = os.path.join(d, "claude", "projects", "-p")
        os.makedirs(proj, exist_ok=True)
        text = _launchrecipe.launch_sh("codex", "codex")
        with open(os.path.join(d, "launch.sh"), "w") as f:
            f.write(text)
        fam = seat.FAMILIES["codex"]
        from helm.seat_catalog import family_catalogued_models
        older = next(m for m in family_catalogued_models(fam)
                     if m != fam["model"])
        for sid, model in ((SID, older), (SID2, fam["model"])):
            with open(os.path.join(proj, sid + ".jsonl"), "w") as f:
                f.write(_lines([
                    {"type": "permission-mode", "sessionId": sid,
                     "permissionMode": "bypassPermissions"},
                    {"type": "attachment", "sessionId": sid, "attachment": {
                        "type": "model", "identity": {"modelId": model}}},
                    {"type": "assistant", "sessionId": sid, "effort": "high",
                     "message": {"model": model}}]))
        ran = seat_recipe.launch_capture(text)
        self._proc(4600, ["/opt/claude/versions/2.1.282"] + ran["argv"]
                   + ["--resume", SID2], ran["env"], start="100")
        return d, {"seat": "codex", "session": SID2, "session_pid": 4600,
                   "session_pid_identity": "proc:100"}

    def _plan(self, d, sid, register):
        with mock.patch.object(seat_launch_assets, "_launch_endpoint",
                               return_value=_launchrecipe.FIXTURE_PORT):
            return seat_recipe.proxy_plan(
                "codex", "codex", d, sid, self.tmp, register, "worker", None,
                None, False, "codex", None)

    def test_the_live_process_binds_only_the_session_it_holds(self):  # noqa: VACUOUS_ASSERTION — the refusal naming the transcript's model is the positive control; the empty relaunch words are the refusal's contract, and the sibling arm resumes the live session exactly
        """`--session OLD` while the seat's live process holds NEW: the live
        process is NEW's launch, so it binds nothing for OLD, and OLD's
        transcript beside launch.sh is not one launch's recipe. MUTATION:
        read the live process for any session — OLD resumes EXACTLY on the
        process NEW runs."""
        d, register = self._two_sessions()
        plan = self._plan(d, SID, register)
        self.assertIsNotNone(plan.refusal, plan.lines)
        self.assertIn("model [transcript", plan.refusal)
        self.assertEqual(plan.extra, ())

    def test_the_live_process_binds_the_session_it_holds(self):
        """The control: the same live process resumes NEW exactly."""
        d, register = self._two_sessions()
        plan = self._plan(d, SID2, register)
        self.assertIsNone(plan.refusal, plan.lines)
        self.assertIn("resuming EXACTLY as it ran", "\n".join(plan.lines))

    def test_identity_follows_the_roster_is_documented(self):
        """docs/VERBS.md states the one field an exact resume takes from
        the roster rather than from the launch it ran. MUTATION: leave it to
        the code alone — an operator reads "exactly as it ran" and finds the
        seat back under another name."""
        with open(os.path.join(ROOT, "docs", "VERBS.md"),
                  encoding="utf-8") as f:
            doc = f.read()
        self.assertIn("**Identity follows the roster.**", doc)
        self.assertIn("`identity follows the roster: <recorded> -> <roster>`",
                      doc)



class AdoptedLeadPostureTest(ProcFixture):
    """An orca-adopted claude seat whose RECORDED role is lead (task/4137)
    resumes with what `helm launch --seat S --role lead` gives a lead: the
    lead window pair and the HELM_SEAT_ROLE=lead marker in its env, and the
    lead-lean --settings words before --resume. A worker's plan, an
    undeclared seat's and a corrupt declaration's are what they always
    were. The declaration is planted under this fixture's tmp HELM_HOME."""

    SEAT = "lead"
    MODEL = "claude-opus-5-5[1m]"

    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.homes_root, "lead")   # a credhome
        os.makedirs(self.home)
        self.row = {"i": SID, "h": "claude", "cwd": self.tmp}
        pid = self._proc(4970, [
            "/opt/claude/versions/2.1.282", "--model", self.MODEL,
            "--effort", "high", "--permission-mode", "plan",
            "--settings", '{"ultracode":false}',
            "--disallowedTools", "EnterPlanMode", "--resume", SID],
            {"HELM_CHAT_NAME": self.SEAT, "HELM_CELL_PROFILE": "p-lead",
             "DREGG_PROFILE": "p-lead", "CLAUDE_CONFIG_DIR": self.home})
        line, failed = seat_resume_all.record_live_set(
            [], True, False, BOOT, 1, recipes=seat_recipe.carried(
                seat_recipe.capture_seats([(self.SEAT, SID, [pid])])))
        self.assertFalse(failed, line)

    WORDS = ["--model", "claude-opus-5-5[1m]", "--permission-mode", "plan",
             "--effort", "high", "--disallowedTools", "EnterPlanMode"]

    def _declaration(self):
        from helm import seat_role
        return os.path.join(seat_role._declaration_dir(self.SEAT),
                            seat_role.DECLARATION)

    def _plant(self, text):
        path = self._declaration()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def _plans(self):
        """(the exact plan, the --defaults plan), each as the triple a
        resume acts on: its lines, its env and its words."""
        return tuple((plan.refusal, plan.lines, plan.env, plan.extra)
                     for plan in (
            seat_recipe.adopted_plan(self.SEAT, dict(self.row), self.home),
            seat_recipe.adopted_plan(self.SEAT, dict(self.row), self.home,
                                     defaults=True)))

    def test_an_adopted_lead_resumes_with_the_lead_posture(self):  # noqa: VACUOUS_ASSERTION — the per-branch subtests are paired with unconditional positives after the loop: each plan's exact words and the readout's lead line
        """Both branches carry the marker, the window pair and the lean
        settings, each value the one `helm launch --role lead` puts on its
        child (launch.build_env and launch._lead_lean_args, read here, never
        restated). MUTATION: drop the posture from either branch — the
        resumed lead runs at CC's default window, unmarked, on its full
        settings, and this arm fails."""
        from helm import launch, seat_catalog, seat_role
        self.assertIsNone(seat_role.declare_role(self.SEAT, "lead"))
        self.assertEqual(seat_role.recorded_role(self.SEAT), "lead")
        built = launch.build_env({}, self.SEAT, role="lead",
                                 allocate_scratch=False)
        lean = launch._lead_lean_args(self.SEAT, "lead")
        self.assertEqual(lean[0], "--settings")
        keys = ("HELM_SEAT_ROLE",) + tuple(seat_catalog.WINDOW_VARS)
        exact, chose = self._plans()
        for name, (refusal, lines, env, extra) in (("exact", exact),
                                                   ("defaults", chose)):
            with self.subTest(branch=name):
                self.assertIsNone(refusal, lines)
                self.assertEqual(env["HELM_SEAT_ROLE"], "lead")
                self.assertEqual({k: env.get(k) for k in keys},
                                 {k: built[k] for k in keys})
                self.assertEqual(env["HELM_CHAT_NAME"], self.SEAT)
                self.assertEqual(list(extra[-2:]), lean)
                self.assertTrue(os.path.isfile(extra[-1]))
                self.assertIn("lead posture", "\n".join(lines))
        self.assertEqual(list(exact[3]), self.WORDS + lean)
        self.assertEqual(list(chose[3]), lean)
        # the readout says so, and writes nothing to say it
        r = seat_recipe.read("claude", self.SEAT, SID, cwd=self.tmp,
                             credhome=self.home)
        os.unlink(extra[-1])
        out = "\n".join(seat_recipe.render(self.SEAT, (
            r, {}, None, seat_recipe.CLAUDE_FIELDS)))
        self.assertRegex(out, r"lead posture .*HELM_SEAT_ROLE=lead, window "
                              r"%s" % built["CLAUDE_CODE_AUTO_COMPACT_WINDOW"])
        self.assertFalse(os.path.exists(extra[-1]),
                         "the read-only readout wrote the lean profile")

    def test_an_adopted_worker_resumes_as_it_always_did(self):  # noqa: VACUOUS_ASSERTION — the absent posture is paired with unconditional positives on the same plans: the exact recipe words and the bare identity env
        """An undeclared seat and a seat declared worker plan identically,
        with no posture word anywhere. MUTATION: apply the posture to every
        adopted seat — a worker comes back marked lead."""
        from helm import seat_role
        undeclared = self._plans()
        self.assertIsNone(seat_role.declare_role(self.SEAT, "worker"))
        self.assertEqual(self._plans(), undeclared)
        exact, chose = undeclared
        self.assertIsNone(exact[0], exact[1])
        self.assertEqual(list(exact[3]), self.WORDS)
        self.assertEqual(chose[3], ())
        self.assertEqual(chose[2], {"HELM_CHAT_NAME": self.SEAT})
        for _refusal, lines, env, extra in undeclared:
            self.assertNotIn("HELM_SEAT_ROLE", env)
            self.assertNotIn("CLAUDE_CODE_AUTO_COMPACT_WINDOW", env)
            self.assertNotIn("--settings", extra)
            self.assertNotIn("lead posture", "\n".join(lines))

    def test_a_corrupt_or_unreadable_declaration_reads_worker(self):  # noqa: VACUOUS_ASSERTION — every arm compares to a worker plan whose refusal-free exact words are asserted unconditionally first
        """A declaration that does not parse, one naming another seat, and a
        role reader that raises all plan as a worker. MUTATION: read the
        role off the file's text, or let the reader's error through — a
        broken file promotes the seat or the resume dies."""
        from helm import seat_role
        worker = self._plans()
        # the positive control: the plan compared against is a real resume
        self.assertIsNone(worker[0][0], worker[0][1])
        self.assertEqual(list(worker[0][3]), self.WORDS)
        for text in ('{"v": 1, "seat": "lead", "role": "lead"',
                     json.dumps({"v": 1, "seat": "other", "role": "lead"}),
                     json.dumps({"v": 1, "seat": "lead", "role": "boss"})):
            with self.subTest(text=text):
                self._plant(text)
                self.assertEqual(self._plans(), worker)
        self._plant(json.dumps({"v": 1, "seat": "lead", "role": "lead"}))
        with mock.patch.object(seat_role, "recorded_role",
                               side_effect=RuntimeError("unreadable")):
            self.assertEqual(self._plans(), worker)

    def test_a_lead_on_another_harness_resumes_as_before(self):
        """The non-claude adopted path has no claude launch to carry the
        posture: its --defaults plan is the bare one, declaration or not.
        MUTATION: add the claude flags there — a codex resume gets
        --settings it does not take."""
        from helm import seat_role
        self.assertIsNone(seat_role.declare_role(self.SEAT, "lead"))
        row = {"i": SID, "h": "codex", "cwd": self.tmp}
        plan = seat_recipe.adopted_plan(self.SEAT, row, None, defaults=True)
        self.assertIsNone(plan.refusal)
        self.assertEqual(plan.env, {"HELM_CHAT_NAME": self.SEAT})
        self.assertEqual(plan.extra, ())
        self.assertIsNotNone(seat_recipe.adopted_plan(self.SEAT, row,
                                                      None).refusal)


if __name__ == "__main__":
    unittest.main()
