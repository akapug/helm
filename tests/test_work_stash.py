#!/usr/bin/env python3
"""helm work stash — address a stash by MESSAGE, never by position.

The incident these pin (2026-07-24, shared-tree #5): `stash@{0}` is a POSITION
in a stack. A redundant council WIP was verified and dropped — correct on its
own terms — and from that instant `stash@{0}` resolved to a DIFFERENT agent's
cv-autocompact entry, which then kept getting applied into MAIN, leaving
dangling conflict states with no MERGE_HEAD that the integrator unpicked by
hand more than once.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-stash-", var="HELM_HOME")

from helm.work import _stash  # noqa: E402


def _run(root, *args):
    subprocess.run(["git"] + list(args), cwd=root, check=True,
                   capture_output=True)


class StashByMessageTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="helm-test-stash-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        _run(self.root, "init", "-q", "-b", "main")
        _run(self.root, "config", "user.email", "t@t")
        _run(self.root, "config", "user.name", "t")
        with open(os.path.join(self.root, "f.txt"), "w") as fh:
            fh.write("base\n")
        _run(self.root, "add", "-A")
        _run(self.root, "commit", "-qm", "base")
        for tag in ("alpha-work", "beta-work"):
            with open(os.path.join(self.root, "f.txt"), "w") as fh:
                fh.write(tag + "\n")
            _run(self.root, "stash", "push", "-m", tag)

    def test_a_positional_reference_is_refused_with_the_reason(self):
        for bad in ("stash@{0}", "stash@{ 1 }", "@{2}"):
            ref, _msg, err = _stash.resolve(self.root, bad)
            self.assertIsNone(ref, bad)
            self.assertIn("POSITION", err)
            self.assertIn("MESSAGE", err)          # says what to do instead

    def test_a_message_names_the_same_stash_after_the_stack_shifts(self):
        """THE INCIDENT: the position moves, the message does not."""
        ref0, msg0, err = _stash.resolve(self.root, "alpha-work")
        self.assertIsNone(err)
        self.assertEqual(ref0, "stash@{1}")        # alpha is the OLDER entry
        # drop the newer entry — every later position shifts down by one
        _run(self.root, "stash", "drop", "stash@{0}")
        ref1, msg1, err = _stash.resolve(self.root, "alpha-work")
        self.assertIsNone(err)
        self.assertEqual(ref1, "stash@{0}")        # the POSITION moved...
        self.assertEqual(msg0, msg1)               # ...the MESSAGE did not
        self.assertNotEqual(ref0, ref1)            # which is the whole bug

    def test_ambiguity_refuses_rather_than_guessing(self):
        ref, _msg, err = _stash.resolve(self.root, "work")   # matches both
        self.assertIsNone(ref)
        self.assertIn("AMBIGUOUS", err)
        self.assertIn("alpha-work", err)           # names both candidates
        self.assertIn("beta-work", err)

    def test_a_miss_is_a_refusal_not_a_default(self):
        ref, _msg, err = _stash.resolve(self.root, "no-such-stash")
        self.assertIsNone(ref)
        self.assertIn("no stash message contains", err)

    def test_an_empty_needle_refuses(self):
        for empty in ("", "   ", None):
            ref, _msg, err = _stash.resolve(self.root, empty)
            self.assertIsNone(ref)
            self.assertIn("MESSAGE", err)

    def test_act_applies_the_named_stash_and_echoes_which(self):
        lines, err = _stash.act(self.root, "apply", "alpha-work")
        self.assertIsNone(err)
        self.assertIn("alpha-work", lines[0])      # the operator sees the hit
        with open(os.path.join(self.root, "f.txt")) as fh:
            self.assertEqual(fh.read().strip(), "alpha-work")

    def test_act_refuses_a_positional_before_touching_git(self):
        before = _stash._entries(self.root)
        lines, err = _stash.act(self.root, "drop", "stash@{0}")
        self.assertIsNone(lines)
        self.assertIn("POSITION", err)
        self.assertEqual(_stash._entries(self.root), before)   # nothing moved

    def test_unreadable_stack_is_UNKNOWN_not_empty(self):
        ref, _msg, err = _stash.resolve("/definitely/not/a/repo", "anything")
        self.assertIsNone(ref)
        self.assertIn("UNKNOWN", err)

    def test_list_says_UNKNOWN_when_it_cannot_read(self):
        self.assertIn("UNKNOWN",
                      "\n".join(_stash.list_lines("/definitely/not/a/repo")))


if __name__ == "__main__":
    unittest.main()
