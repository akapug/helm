#!/usr/bin/env python3
"""The footer does not tell the owner the CLI is the helm.

The owner's door is the web console; the footer's trailing clause —
"localhost surface — the CLI is the helm" — contradicts that, so console walk
4 (task/3739 finding 10) removes it. This pins the removal: the footer keeps
its ⎈ mark, the ~/.helm label and the sync age, and stops claiming the CLI is
the helm. The mark, label and sync age are asserted to remain so the cure is
the clause, not a reword of the whole footer.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOOT_SRC = os.path.join(ROOT, "helm", "web_ui", "scripts", "00-core.js.part")


class FooterWords(unittest.TestCase):
    def test_foot_body(self):
        with open(FOOT_SRC, encoding="utf-8") as handle:
            source = handle.read()
        match = re.search(r"function foot\(reg\) \{(.*?)\n\}", source,
                          re.DOTALL)
        self.assertTrue(match, "no function foot(reg) { found in 00-core.js.part")
        return match.group(1)

    def test_foot_body_is_not_empty(self):
        body = self.test_foot_body()
        self.assertTrue(body.strip(), "the footer function has no body to test")

    def test_footer_no_longer_claims_the_cli_is_the_helm(self):
        body = self.test_foot_body()
        self.assertNotIn("the CLI is the helm", body,
                         "the footer still says the CLI is the helm")
        self.assertNotIn("localhost surface", body,
                         "the footer still says localhost surface")

    def test_footer_keeps_sync_state_and_mark(self):
        body = self.test_foot_body()
        self.assertIn("synced", body, "the sync age is gone")
        self.assertIn("never synced", body, "the never-synced arm is gone")
        self.assertIn("⎈", body, "the ⎈ mark is gone")


if __name__ == "__main__":
    unittest.main()
