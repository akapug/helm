#!/usr/bin/env python3
"""docs/NEW_AGENT_GUIDE.md — the one onboarding artifact every new seat is
steered to, and the RSH wiring that steers them: the `helm chat join` banner
ends with a pointer at the guide. Source-driven both ways — the banner's
pointer must name a file that exists (deleting/moving the guide fails the
suite), and the banner must actually carry it (dropping the pointer fails
the suite). Hermetic: HELM_HOME/HELM_CHAT_DIR point at tmp dirs."""
import os
import re
import shutil
import subprocess
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
HELM = os.path.join(REPO, "bin", "helm")

# Probe-of-source runners: a guide claim gated on grepping/reading repo files
# is stale-by-construction — rc 2 from any non-repo cwd, and it tests a source
# substring, not runtime capability (the `grep -q per-instance helm/seat.py`
# class). Operational claims must be helm verbs.
SOURCE_PROBE_RUNNERS = {"grep", "egrep", "fgrep", "rg", "cat", "sed", "awk",
                        "head", "tail", "ls", "find", "test", "python",
                        "python3"}


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
                       "helm who", "helm session ls", "helm seat launch",
                       "--room", "Monitor", "ToolSearch"):
            self.assertIn(needle, text, "guide lost its %r leg" % needle)

    def test_guide_claims_are_helm_probes_not_source_greps(self):
        # the finding-1 class, structurally: a backticked command that greps or
        # reads repo files only works from the checkout root and pins a source
        # substring, not a runtime capability — the guide may not carry one
        text = open(GUIDE, encoding="utf-8").read()
        for span in re.findall(r"`([^`\n]+)`", text):
            toks = span.split()
            first = toks[0] if toks else ""
            self.assertNotIn(
                first, SOURCE_PROBE_RUNNERS,
                "guide gates on a source probe %r — cwd-dependent and "
                "stale-by-construction; state it as a helm verb" % span)

    def test_guide_helm_commands_exist_from_a_neutral_cwd(self):
        # every `helm <verb> ...` claim must resolve against the REAL CLI from
        # a NEUTRAL cwd (tmpdir): `helm <verb> --help` exits 0 for a known
        # verb and 2 for an unknown one, so a dropped/renamed verb fails here
        # and no claim can quietly depend on being run from the checkout.
        # Future-tense claims are allowed ONLY with the structural marker: the
        # span immediately followed by "(lane/<lane>)" — `helm fleet`
        # (lane/fleet-truth-verb) — which names where the verb lands.
        text = open(GUIDE, encoding="utf-8").read()
        verbs = set()
        for m in re.finditer(r"`(helm [^`\n]+)`", text):
            toks = m.group(1).split()
            if len(toks) < 2 or toks[1].startswith("<"):
                continue  # `helm <verb> --help` is the probe idiom itself
            if text[m.end():].lstrip()[:6] == "(lane/":
                continue  # marked unlanded — present tense would be a lie
            verbs.add(toks[1])
        self.assertTrue(verbs, "guide carries no helm commands to verify")
        neutral = tempfile.mkdtemp(prefix="helm-guide-neutral-")
        try:
            for verb in sorted(verbs):
                p = subprocess.run(
                    [sys.executable, HELM, verb, "--help"], cwd=neutral,
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(
                    p.returncode, 0,
                    "guide names `helm %s` but the CLI refuses it from a "
                    "neutral cwd (rc %d): %s"
                    % (verb, p.returncode, p.stderr.strip()))
        finally:
            shutil.rmtree(neutral, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
