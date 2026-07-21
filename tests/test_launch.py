#!/usr/bin/env python3
"""helm launch — the metaharness seam. The exec leg is the one untested
line by design; everything else (arg split, env build, seat derivation,
roster pre-write) is pure and pinned here. Hermetic per the seats pattern."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import launch, seats  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_ROOM",
            "MELD_CHAT_ROOM", "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL")


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

    def test_build_env_sets_seat_optional_home_and_explicit_room(self):
        env = launch.build_env({"PATH": "/bin"}, "alice")
        self.assertEqual(env["HELM_CHAT_NAME"], "alice")
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
        self.assertNotIn("HELM_CHAT_ROOM", env)
        env = launch.build_env({}, "alice", "/homes/h1", "team-a")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/homes/h1")
        self.assertEqual(env["HELM_CHAT_ROOM"], "team-a")
        # The parser's implicit main default must not accidentally home every
        # legacy launch; un-homed seats retain the all-room compatibility lane.
        self.assertNotIn("HELM_CHAT_ROOM",
                         launch.build_env({}, "alice", room="main"))

    def test_stable_seat_sanitized(self):
        s = launch.stable_seat("/tmp/My Proj!x")
        self.assertNotIn(" ", s)
        self.assertNotIn("!", s)
        self.assertTrue(s.endswith("My-Proj-x"))
        self.assertLessEqual(len(s), 64)


if __name__ == "__main__":
    unittest.main()
