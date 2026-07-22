#!/usr/bin/env python3
"""docs/NEW_AGENT_GUIDE.md — the one onboarding artifact every new seat is
steered to, and the RSH wiring that steers them: the `helm chat join` banner
ends with a pointer at the guide. Source-driven both ways — the banner's
pointer must name a file that exists (deleting/moving the guide fails the
suite), and the banner must actually carry it (dropping the pointer fails
the suite). Hermetic: HELM_HOME/HELM_CHAT_DIR point at tmp dirs."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import seats  # noqa: E402

ENV_KEYS = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NAME", "HELM_CHAT_ROOM",
            "HELM_CHAT_ROOM_SOURCE", "HELM_CHAT_NODE_URL", "HELM_ADOPTED_DIR",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUIDE = os.path.join(REPO, "docs", "NEW_AGENT_GUIDE.md")


class NewAgentGuideTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-guide-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_guide_exists_where_the_banner_points(self):
        # one constant, both truths: the banner interpolates GUIDE_PATH, and
        # GUIDE_PATH must be a real file in this checkout
        self.assertEqual(seats.GUIDE_PATH, GUIDE)
        self.assertTrue(os.path.isfile(seats.GUIDE_PATH),
                        "banner points at a missing guide: %s" % seats.GUIDE_PATH)

    def test_join_banner_carries_the_guide_pointer(self):
        _seat, line = seats.join(seat="newbie", cwd=self.tmp)
        self.assertIn(seats.GUIDE_PATH, line)
        self.assertIn("NEW_AGENT_GUIDE.md", line)

    def test_guide_covers_the_load_bearing_verbs(self):
        # the guide is real onboarding, not a doc dump: every first-10-minutes
        # claim is an executable verb — pin the load-bearing ones so a rewrite
        # cannot silently drop a leg
        text = open(GUIDE, encoding="utf-8").read()
        for needle in ("helm chat wait --seat", "helm chat post",
                       "helm work claim", "helm store resolve", "helm fleet",
                       "helm session ls", "Monitor", "ToolSearch"):
            self.assertIn(needle, text, "guide lost its %r leg" % needle)


if __name__ == "__main__":
    unittest.main()
