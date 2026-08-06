#!/usr/bin/env python3
"""helm launch — the metaharness seam. The exec leg is the one untested
line by design; everything else (arg split, env build, seat derivation,
roster pre-write) is pure and pinned here. Hermetic per the seats pattern."""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import launch, seats  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_ROOM",
            "MELD_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE",
            "MELD_CHAT_ROOM_SOURCE", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CELL_BIN", "MELD_CELL_BIN",
            "HELM_CELL_PROFILE", "MELD_AGENT_PROFILE", "DREGG_PROFILE")


class LaunchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-launch-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parse_splits_flags_from_claude_args(self):
        opts, rest = launch.parse_args(
            ["--seat", "alice", "--room", "dev", "--", "-p", "do the thing"])
        self.assertEqual(opts["seat"], "alice")
        self.assertEqual(opts["room"], "dev")
        self.assertEqual(rest, ["-p", "do the thing"])
        # first unknown token starts the pass-through, no `--` needed
        opts, rest = launch.parse_args(["--seat", "z", "-p", "hi", "--seat", "x"])
        self.assertEqual(opts["seat"], "z")
        self.assertEqual(rest, ["-p", "hi", "--seat", "x"])
        opts, rest = launch.parse_args(["--no-install"])
        self.assertFalse(opts["install"])
        self.assertEqual(rest, [])

    def test_build_env_sets_room_and_truthful_provenance(self):
        env = launch.build_env({"PATH": "/bin"}, "alice")
        self.assertEqual(env["HELM_CHAT_NAME"], "alice")
        self.assertEqual(env["HELM_AGENT_HARNESS"], "claude")
        self.assertEqual(env["HELM_MODEL_FAMILY"], "claude")
        self.assertEqual(env["HELM_MODEL_BACKEND"], "native")
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
        self.assertNotIn("HELM_CHAT_ROOM", env)
        env = launch.build_env({}, "alice", "/homes/h1", "team-a", "derived")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/homes/h1")
        self.assertEqual(env["HELM_CHAT_ROOM"], "team-a")
        self.assertEqual(env["HELM_CHAT_ROOM_SOURCE"], "derived")
        # Explicit main is meaningful: it prevents project derivation and homes
        # the child to main-only. It must clear a stale inherited derived marker.
        env = launch.build_env(
            {"HELM_CHAT_ROOM_SOURCE": "derived",
             "MELD_CHAT_ROOM": "stale", "MELD_CHAT_ROOM_SOURCE": "derived"},
            "alice", room="main")
        self.assertEqual(env["HELM_CHAT_ROOM"], "main")
        self.assertNotIn("HELM_CHAT_ROOM_SOURCE", env)
        self.assertNotIn("MELD_CHAT_ROOM", env)
        self.assertNotIn("MELD_CHAT_ROOM_SOURCE", env)

    def test_build_env_aligns_signer_identity_with_chat_identity(self):
        """A native launch used to set only HELM_CHAT_NAME. A clean parent made
        the child unsigned; a signed parent made it speak as alice while signing
        as the parent. The launch owner emits one aligned identity tuple."""
        from helm import seat
        env = launch.build_env({}, "alice")
        self.assertEqual(env["HELM_CELL_BIN"], seat.DREGG_SIGNER_DEFAULT)
        self.assertEqual(env["HELM_CELL_PROFILE"], "alice")
        self.assertEqual(env["DREGG_PROFILE"], "alice")
        self.assertEqual(env["GIT_AUTHOR_NAME"], "alice")
        self.assertEqual(env["GIT_COMMITTER_NAME"], "alice")

        env = launch.build_env(
            {"HELM_CELL_BIN": "/custom/signer",
             "HELM_CELL_PROFILE": "launcher",
             "DREGG_PROFILE": "launcher",
             "GIT_AUTHOR_NAME": "Operator Name",
             "GIT_COMMITTER_NAME": "Operator Name"}, "alice")
        self.assertEqual(env["HELM_CELL_BIN"], "/custom/signer")
        self.assertEqual(env["HELM_CELL_PROFILE"], "alice")
        self.assertEqual(env["DREGG_PROFILE"], "alice")
        self.assertEqual(env["GIT_AUTHOR_NAME"], "Operator Name")
        self.assertEqual(env["GIT_COMMITTER_NAME"], "Operator Name")

    def test_build_env_preserves_explicit_signer_kill_switches(self):
        env = launch.build_env({"HELM_CELL_BIN": ""}, "alice")
        self.assertEqual(env["HELM_CELL_BIN"], "")
        env = launch.build_env({"MELD_CELL_BIN": ""}, "alice")
        self.assertEqual(env["HELM_CELL_BIN"], "")
        env = launch.build_env({"MELD_CELL_BIN": "/legacy/signer"}, "alice")
        self.assertEqual(env["HELM_CELL_BIN"], "/legacy/signer")

    def test_cmd_launch_exports_derived_project_room(self):
        with mock.patch.object(launch.seats, "derive_home_room",
                               return_value="helm"), \
                mock.patch.object(launch.seats, "join") as join, \
                mock.patch.object(launch.os, "execvpe") as execvpe:
            launch.cmd_launch(["--no-install"])
        join.assert_called_once_with(
            cwd=os.getcwd(), seat=launch.stable_seat(), room="helm",
            room_explicit=False, room_source="derived",
            runtime={"agent_harness": "claude", "family": "claude",
                     "backend": "native"})
        env = execvpe.call_args[0][2]
        self.assertEqual(env["HELM_CHAT_ROOM"], "helm")
        self.assertEqual(env["HELM_CHAT_ROOM_SOURCE"], "derived")

    def test_legacy_room_alias_is_inherited_and_canonicalized(self):
        os.environ["MELD_CHAT_ROOM"] = "legacy-project"
        os.environ["MELD_CHAT_ROOM_SOURCE"] = "derived"
        with mock.patch.object(launch.seats, "join") as join, \
                mock.patch.object(launch.os, "execvpe") as execvpe:
            launch.cmd_launch(["--no-install"])
        join.assert_called_once_with(
            cwd=os.getcwd(), seat=launch.stable_seat(), room="legacy-project",
            room_explicit=False, room_source="derived",
            runtime={"agent_harness": "claude", "family": "claude",
                     "backend": "native"})
        env = execvpe.call_args[0][2]
        self.assertEqual(env["HELM_CHAT_ROOM"], "legacy-project")
        self.assertEqual(env["HELM_CHAT_ROOM_SOURCE"], "derived")
        self.assertNotIn("MELD_CHAT_ROOM", env)
        self.assertNotIn("MELD_CHAT_ROOM_SOURCE", env)

    def test_preferred_room_ignores_legacy_source_marker(self):
        os.environ["HELM_CHAT_ROOM"] = "explicit-new"
        os.environ["MELD_CHAT_ROOM_SOURCE"] = "derived"
        with mock.patch.object(launch.seats, "join") as join, \
                mock.patch.object(launch.os, "execvpe") as execvpe:
            launch.cmd_launch(["--no-install"])
        self.assertEqual(join.call_args.kwargs["room"], "explicit-new")
        self.assertTrue(join.call_args.kwargs["room_explicit"])
        self.assertIsNone(join.call_args.kwargs["room_source"])
        env = execvpe.call_args[0][2]
        self.assertEqual(env["HELM_CHAT_ROOM"], "explicit-new")
        self.assertNotIn("HELM_CHAT_ROOM_SOURCE", env)

    def test_cli_room_beats_derived_environment_marker(self):
        os.environ["HELM_CHAT_ROOM"] = "project-a"
        os.environ["HELM_CHAT_ROOM_SOURCE"] = "derived"
        with mock.patch.object(launch.seats, "join") as join, \
                mock.patch.object(launch.os, "execvpe") as execvpe:
            launch.cmd_launch(["--no-install", "--room", "main"])
        self.assertEqual(join.call_args.kwargs["room"], "main")
        self.assertTrue(join.call_args.kwargs["room_explicit"])
        self.assertIsNone(join.call_args.kwargs["room_source"])
        env = execvpe.call_args[0][2]
        self.assertEqual(env["HELM_CHAT_ROOM"], "main")
        self.assertNotIn("HELM_CHAT_ROOM_SOURCE", env)

    def test_build_env_strips_child_stamp(self):
        """child-stamp-kills-seat-persistence: `helm launch` run from inside a
        Claude session inherits the child stamp; the exec'd claude must start
        top-level or its transcript persistence is silently off."""
        from helm import seat
        base = {"PATH": "/bin"}
        base.update({v: "leaked" for v in seat.CHILD_STAMP_VARS})
        env = launch.build_env(base, "alice")
        for v in seat.CHILD_STAMP_VARS:
            self.assertNotIn(v, env)
        self.assertEqual(env["PATH"], "/bin")  # everything else rides through

    def test_build_env_strips_the_proxy_triple(self):
        """#107. Every non-claude family here runs behind a local proxy but
        INSIDE Claude Code's harness, so a proxied seat's shell carries
        ANTHROPIC_BASE_URL/AUTH_TOKEN/API_KEY. `helm launch` execs a seat that
        registers itself family=claude backend=NATIVE — inheriting those aims
        a native seat at a proxy fronting another vendor. It would look
        native, be billed native, and route elsewhere, and nothing downstream
        reports it.

        Values here are synthetic placeholders; only the NAMES are real."""
        from helm import seat
        base = {"PATH": "/bin", "HOME": "/tmp/synthetic-home"}
        base.update({v: "synthetic-placeholder" for v in seat.SCRUB_VARS})
        env = launch.build_env(base, "alice")
        # THE KEY SET, not "a wanted key is somewhere in it" — asserting
        # membership is how a half-scrubbed env passes.
        self.assertEqual(sorted(k for k in env if k in seat.SCRUB_VARS), [])
        self.assertEqual(env["PATH"], "/bin")       # everything else rides
        self.assertEqual(env["HOME"], "/tmp/synthetic-home")

    def test_build_env_is_unchanged_from_a_clean_shell(self):
        """LIFECYCLE, launch #2: nothing to strip is a NO-OP, never a mangled
        env. The positive control for the arm above — without it, a build_env
        that dropped everything would satisfy the emptiness there."""
        from helm import seat
        base = {"PATH": "/bin", "HOME": "/tmp/synthetic-home", "TERM": "xterm"}
        env = launch.build_env(base, "alice")
        # unconditional positives on the SAME env the absence is about
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["TERM"], "xterm")
        for k, v in base.items():
            self.assertEqual(env[k], v)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)   # absent, and stays absent

    def test_the_scrub_does_not_depend_on_the_proxy_being_up(self):
        """LIFECYCLE, launch #3: a seat whose proxy is DOWN still carries the
        variables — they are environment, not liveness. A scrub gated on
        reachability would pass every test on a healthy box and leak on the
        one day the proxy is dead, which is also the day someone relaunches."""
        from helm import seat
        base = {"PATH": "/bin"}
        # a base URL naming a port nothing is listening on: still inherited
        base.update({v: "http://127.0.0.1:1" for v in seat.SCRUB_VARS})
        env = launch.build_env(base, "alice")
        self.assertEqual(env["PATH"], "/bin")      # same env, positive
        self.assertEqual(sorted(k for k in env if k in seat.SCRUB_VARS), [])

    def test_build_env_does_not_mutate_the_caller_environment(self):  # noqa: VACUOUS_ASSERTION — the contract IS non-mutation, so the decisive assertion compares base to a pre-call snapshot and no positive control on that comparison can exist. Three unconditional positives on the SAME call already run first: the returned env is scrubbed, it still carries PATH, and base still carries every SCRUB_VAR.
        """`build_env(os.environ, ...)` is the real call. If the scrub edited
        in place it would strip the LAUNCHER's own proxy config as a side
        effect — breaking the very seat that ran the command."""
        from helm import seat
        base = {"PATH": "/bin"}
        base.update({v: "synthetic-placeholder" for v in seat.SCRUB_VARS})
        before = dict(base)
        env = launch.build_env(base, "alice")
        # the same call's OTHER channel: it really produced a scrubbed env,
        # so `base` being untouched is a copy and not a call that did nothing
        self.assertEqual(sorted(k for k in env if k in seat.SCRUB_VARS), [])
        self.assertEqual(env["PATH"], "/bin")
        # THE PROPERTY THAT MATTERS, stated positively: the caller STILL
        # CARRIES the proxy config. "base is unchanged" is the weaker way to
        # say it and reads as an absence; what breaks if this regresses is a
        # launching seat losing the vars its own proxy needs.
        self.assertEqual(sorted(k for k in base if k in seat.SCRUB_VARS),
                         sorted(seat.SCRUB_VARS))
        self.assertEqual(base, before)

    def test_cmd_launch_survives_a_deleted_cwd(self):
        """MED (fable delta pair @2b4d496, probe A6): the seat DEFAULT called
        stable_seat() — a bare os.getcwd() — BEFORE the safe_cwd resolver
        line, so `helm launch` from a pruned worktree with neither --seat nor
        HELM_CHAT_NAME died with FileNotFoundError instead of launching
        un-homed as the adjacent comment promises. safe_cwd is hoisted above
        the seat default; the seat falls to <host>-here, the room to main."""
        saved = os.path.dirname(os.path.abspath(__file__))
        self.addCleanup(os.chdir, saved)
        d = tempfile.mkdtemp(dir=self.tmp)
        os.chdir(d)
        os.rmdir(d)
        self.assertRaises(OSError, os.getcwd)   # the probe's precondition
        with mock.patch.object(launch.seats, "join") as join, \
                mock.patch.object(launch.os, "execvpe") as execvpe:
            rc = launch.cmd_launch(["--no-install"])
        self.assertIsNone(rc)                   # reached exec, no traceback
        self.assertTrue(execvpe.called)
        kw = join.call_args.kwargs
        self.assertIsNone(kw["cwd"])            # un-homed, not crashed
        self.assertEqual(kw["room"], "main")
        self.assertTrue(kw["seat"].endswith("-here"))

    def test_stable_seat_sanitized(self):
        s = launch.stable_seat("/tmp/My Proj!x")
        self.assertNotIn(" ", s)
        self.assertNotIn("!", s)
        self.assertTrue(s.endswith("My-Proj-x"))
        self.assertLessEqual(len(s), 64)


if __name__ == "__main__":
    unittest.main()
