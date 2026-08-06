#!/usr/bin/env python3
"""The per-toolcall steer: fires at the act, once, and never holds a boundary.

The failure it exists for: an agent Wrote five durable lessons into a private
memory dir while `/learn` — loaded and correct — said "This routes to HELM's
store". Turn-level injection had already delivered the rule. The write happened
anyway, and nothing could notice, because record.py reduced every write to a
basename before any watcher saw it.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import record, toolwhisper  # noqa: E402


class ToolWhisperBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-toolwhisper-")
        self.prior = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.sid = "sess-tw"
        os.makedirs(record.session_dir(self.sid), exist_ok=True)

    def tearDown(self):
        if self.prior is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self.prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    def log_edit(self, path):
        with open(os.path.join(record.session_dir(self.sid),
                               "edit-paths.log"), "a") as f:
            f.write(path + "\n")


class ClassifyTest(unittest.TestCase):
    """Pure, no I/O — the part that must never be the thing that fails."""

    def test_a_private_memory_dir_is_caught(self):
        for p in ("~/.claude-homes/a-home/projects/x/memory/lesson.md",
                  "/u/someone/.claude-homes/a-b/projects/p/memory/n.md",
                  "~/.claude/projects/y/memory/deep/n.md"):
            with self.subTest(p=p):
                self.assertIsNotNone(toolwhisper.classify(p), p)

    def test_ordinary_writes_are_SILENT(self):
        """The bar for a per-toolcall nudge is high: almost every write must
        pass unremarked or the channel becomes noise and gets ignored."""
        for p in ("~/dev/example/helm/helm/seat.py",
                  "~/dev/example/helm/tests/test_seat.py",
                  "~/dev/example/helm/docs/HOOKS.md",
                  "/tmp/scratch/notes.md",
                  "~/.helm/_global/premises/prior-x.md",
                  "~/memory-lane/photo.md"):
            with self.subTest(p=p):
                self.assertIsNone(toolwhisper.classify(p), p)


class WhisperTest(ToolWhisperBase):
    def test_it_fires_on_a_memoryhole_write(self):
        p = "~/.claude-homes/a-home/projects/p/memory/never-clear.md"
        text, rid = toolwhisper.whisper_for(self.sid, p)
        self.assertIsNotNone(text)
        self.assertEqual(rid, "memoryhole-write")
        self.assertIn("helm store add", text,
                      "a steer must name the RIGHT move, not just the wrong one")
        self.assertIn("resolve", text, "and the proof step /learn requires")

    def test_it_LATCHES_one_per_session(self):
        """A refactor across twenty files in one directory costs ONE line."""
        p = "~/.claude-homes/a-home/projects/p/memory/a.md"
        self.assertIsNotNone(toolwhisper.whisper_for(self.sid, p)[0])
        for nxt in ("b.md", "c.md", "d.md"):
            again = toolwhisper.whisper_for(
                self.sid, "~/.claude-homes/a-home/projects/p/memory/" + nxt)[0]
            self.assertIsNone(again, "the latch leaked on " + nxt)

    def test_an_ordinary_write_never_fires(self):
        self.assertIsNone(
            toolwhisper.whisper_for(self.sid, "~/dev/example/helm/helm/pk.py")[0])

    def test_no_session_and_no_path_are_silent_not_errors(self):
        self.assertIsNone(toolwhisper.whisper_for(None, "x")[0])
        self.assertIsNone(toolwhisper.whisper_for(self.sid, None)[0])
        self.assertIsNone(toolwhisper.whisper_for(self.sid, "")[0])


class HookPathTest(ToolWhisperBase):
    """for_hook reads what record.py wrote on the SAME boundary."""

    def test_it_reads_the_last_edit_destination(self):
        self.log_edit("~/dev/example/helm/helm/seat.py")
        self.log_edit("~/.claude-homes/a-home/projects/p/memory/lesson.md")
        self.assertIsNotNone(toolwhisper.for_hook(self.sid))

    def test_the_LAST_edit_decides_not_an_earlier_one(self):
        self.log_edit("~/.claude-homes/a-home/projects/p/memory/old.md")
        self.log_edit("~/dev/example/helm/helm/seat.py")
        self.assertIsNone(toolwhisper.for_hook(self.sid),
                          "an earlier memory write re-fired on a later ordinary one")

    def test_no_log_at_all_is_silent(self):
        self.assertIsNone(toolwhisper.for_hook("sess-with-no-log"))

    def test_an_unreadable_latch_is_silent_never_an_exception(self):
        """FAIL-OPEN is the load-bearing law: a steer that can raise inside a
        hook is worse than no steer at all."""
        self.log_edit("~/.claude-homes/a-home/projects/p/memory/x.md")
        lp = toolwhisper._latch_path(self.sid)
        os.makedirs(os.path.dirname(lp), exist_ok=True)
        os.makedirs(lp, exist_ok=True)          # a DIRECTORY where a file goes
        try:
            self.assertIsNone(toolwhisper.for_hook(self.sid))
        finally:
            os.rmdir(lp)


if __name__ == "__main__":
    unittest.main()


class HelmeseRegisterRuleTest(ToolWhisperBase):
    """The second whisper rule (#216): an edit to the helmese SEED REGISTER.

    WHY IT IS WORTH A WHISPER. The register is versioned AND digested because a
    decoder that meets an unknown version REFUSES to interpret rather than
    guessing with a newer table. An entry changed without a VERSION bump ships
    a register that READS as the old version and is not, and nothing at read
    time can tell. The bump instruction lives in a comment above VERSION —
    exactly the place nobody looks while editing the table below it."""

    REGISTER = "helm/helmese.py"

    def test_it_matches_the_register_by_path(self):
        self.assertTrue(toolwhisper.RULES, "no whisper rules: nothing can match")
        for path in (self.REGISTER, "~/dev/example/helm/" + self.REGISTER,
                     "/u/someone/src/helm/" + self.REGISTER):
            rule = toolwhisper.classify(path)
            self.assertIsNotNone(rule, path)
            self.assertEqual(rule["id"], "helmese-register-edit", path)

    def test_it_does_NOT_match_neighbours_or_lookalikes(self):  # noqa: VACUOUS_ASSERTION — an unconditional assertIsNotNone on the register path runs first, so a None below means no rule matched rather than a dead classifier.
        # MUST-HIT CONTROL first: the classifier is alive on this run, so a
        # None below means "no rule matched" and not "classify is broken".
        self.assertIsNotNone(toolwhisper.classify(self.REGISTER))
        for path in ("tests/test_helmese.py", "helm/clarity/rules.py",
                     "helm/helmese_notes.py", "docs/helmese.py.md"):
            self.assertIsNone(toolwhisper.classify(path), path)

    def test_the_original_rule_still_fires(self):
        """Adding a rule must not shadow the one that was already earned."""
        rule = toolwhisper.classify("~/.claude/projects/x/memory/a.md")
        self.assertIsNotNone(rule)
        self.assertEqual(rule["id"], "memoryhole-write")

    def test_the_whisper_NAMES_THE_RIGHT_MOVE_with_its_command(self):
        """A negation alone is re-readable as permission, so every whisper must
        carry the positive move and the exact command that proves it."""
        text, rid = toolwhisper.whisper_for(self.sid, self.REGISTER)
        self.assertEqual(rid, "helmese-register-edit")
        self.assertIn("helmese.VERSION", text)
        self.assertIn("helmese.stamp()", text)

    def test_it_LATCHES_like_every_other_rule(self):
        """A seat editing the register twice must not be nagged twice."""
        first, _ = toolwhisper.whisper_for(self.sid, self.REGISTER)
        self.assertTrue(first)
        second, _ = toolwhisper.whisper_for(self.sid, self.REGISTER)
        self.assertIsNone(second)

    def test_every_rule_in_the_table_carries_an_id_and_a_say(self):
        self.assertGreaterEqual(len(toolwhisper.RULES), 2)
        ids = [r["id"] for r in toolwhisper.RULES]
        self.assertEqual(len(ids), len(set(ids)), "duplicate whisper rule id")
        for r in toolwhisper.RULES:
            self.assertTrue(r["say"].strip(), r["id"])
