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
            "MELD_CHAT_NODE_URL")


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

    def test_cmd_launch_exports_derived_project_room(self):
        with mock.patch.object(launch.seats, "derive_home_room",
                               return_value="helm"), \
                mock.patch.object(launch.seats, "join") as join, \
                mock.patch.object(launch.os, "execvpe") as execvpe:
            launch.cmd_launch(["--no-install"])
        join.assert_called_once_with(
            cwd=os.getcwd(), seat=launch.stable_seat(), room="helm",
            room_explicit=False, room_source="derived")
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
            room_explicit=False, room_source="derived")
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

    def test_stable_seat_sanitized(self):
        s = launch.stable_seat("/tmp/My Proj!x")
        self.assertNotIn(" ", s)
        self.assertNotIn("!", s)
        self.assertTrue(s.endswith("My-Proj-x"))
        self.assertLessEqual(len(s), 64)


if __name__ == "__main__":
    unittest.main()
