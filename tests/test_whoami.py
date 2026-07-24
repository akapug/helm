#!/usr/bin/env python3
"""whoami tests — hermetic: tempfile + HELM_HOME/HELM_PROFILE_SCAFFOLD env
overrides; never touches the real ~/.helm, ~/.claude."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

from helm import home, pk, whoami


class WhoamiBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.scaffold_path = os.path.join(self.tmp.name, "scaffold", "profile.json")
        self.envp = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp.name, "helm-home"),
            "HELM_PROFILE_SCAFFOLD": self.scaffold_path,
        })
        self.envp.start()
        os.environ.pop("MELD_HOME", None)
        # belt+braces: every write in these tests must land inside the tempdir
        self.assertTrue(home.helm_home().startswith(self.tmp.name))
        self.assertTrue(whoami.scaffold_profile_path().startswith(self.tmp.name))

    def tearDown(self):
        self.envp.stop()
        self.tmp.cleanup()

    def write_scaffold(self, profile, path=None):
        path = path or whoami.scaffold_profile_path()
        pk.write_json(path, profile)
        return path


class TestMergeScaffold(WhoamiBase):
    def test_imports_scaffold_content_and_bookkeeping(self):
        path = self.write_scaffold({"schema_version": 1, "technical_level": "expert",
                              "guidance": ["short replies"], "interview_status": "offered",
                              "updated_at": "2026-07-10T07:04:31Z"},
                             path=os.path.join(self.tmp.name, "ext-scaffold", "profile.json"))
        p = whoami.merge_scaffold(scaffold_path=path)
        self.assertEqual(p["schema_version"], whoami.SCHEMA_VERSION)
        self.assertEqual(p["technical_level"], "expert")
        self.assertEqual(p["guidance"], ["short replies"])
        self.assertEqual(p["interview_status"], "offered")
        self.assertEqual(p["source"], "merged-from-scaffold")
        self.assertTrue(os.path.exists(whoami.profile_path()))

    def test_idempotent_no_rewrite_on_second_run(self):
        path = self.write_scaffold({"technical_level": "technical",
                              "guidance": ["no emojis"], "interview_status": "offered"})
        first = whoami.merge_scaffold(scaffold_path=path)
        with open(whoami.profile_path()) as f:
            raw = f.read()
        second = whoami.merge_scaffold(scaffold_path=path)
        self.assertEqual(first, second)
        self.assertEqual(second["guidance"], ["no emojis"])  # no duplicate append
        with open(whoami.profile_path()) as f:
            self.assertEqual(f.read(), raw)  # untouched on no-change

    def test_empty_scaffold_fields_never_clobber_helm_content(self):
        whoami.save_profile({"schema_version": 2, "technical_level": "technical",
                             "guidance": ["batch deploys"], "interview_status": "done",
                             "updated_at": "", "source": "fresh"})
        path = self.write_scaffold({"technical_level": "", "guidance": [],
                              "interview_status": "offered"})
        p = whoami.merge_scaffold(scaffold_path=path)
        self.assertEqual(p["technical_level"], "technical")
        self.assertEqual(p["guidance"], ["batch deploys"])
        self.assertEqual(p["interview_status"], "done")  # offered never downgrades done
        # a no-op merge must NOT stamp the source: an empty scaffold once
        # clobbered a derived profile's provenance note with "merged-from-scaffold"
        self.assertEqual(p["source"], "fresh")

    def test_no_scaffold_profile_is_fresh(self):
        p = whoami.merge_scaffold(scaffold_path=os.path.join(self.tmp.name, "nope.json"))
        self.assertEqual(p["source"], "fresh")
        self.assertEqual(p["technical_level"], "")
        self.assertEqual(p["interview_status"], "")


class TestNotes(WhoamiBase):
    def test_add_supersede_and_ordering(self):
        a = whoami.add_note("first take on voice", topic="voice")
        b = whoami.add_note("newer take on voice", topic="voice", supersedes=a)
        c = whoami.add_note("no trailing whitespace", topic="pet_peeves")
        active = whoami.load_notes()
        names = [e["name"] for e in active]
        self.assertNotIn(a, names)  # superseded -> hidden
        self.assertIn(b, names)
        self.assertIn(c, names)
        self.assertEqual(names[0], c)  # newest-first
        every = whoami.load_notes(include_superseded=True)
        self.assertIn(a, [e["name"] for e in every])  # history stays on disk
        by_name = {e["name"]: e for e in active}
        self.assertEqual(by_name[b]["body"], "newer take on voice")
        self.assertEqual(by_name[b]["topic"], "voice")
        self.assertEqual(by_name[b]["supersedes"], a)
        self.assertEqual(by_name[b]["type"], "know-your-user")

    def test_supersedes_tolerates_md_suffix(self):
        a = whoami.add_note("v1", topic="defaults")
        whoami.add_note("v2", topic="defaults", supersedes=a + ".md")
        self.assertNotIn(a, [e["name"] for e in whoami.load_notes()])

    def test_empty_text_is_refused(self):
        self.assertIsNone(whoami.add_note("   "))
        self.assertEqual(whoami.load_notes(), [])


class TestWhoamiCmd(WhoamiBase):
    def run_cmd(self, fn, args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fn(args)
        return rc, buf.getvalue()

    def test_empty_points_at_interview(self):
        rc, out = self.run_cmd(whoami.cmd_whoami, [])
        self.assertEqual(rc, 0)
        self.assertIn("know-your-user leg is empty", out)
        self.assertIn("helm interview", out)

    def test_note_cli_and_render(self):
        rc, out = self.run_cmd(whoami.cmd_whoami,
                               ["note", "keep", "replies", "short", "--topic", "voice"])
        self.assertEqual(rc, 0)
        self.assertIn("noted", out)
        name = whoami.load_notes()[0]["name"]
        rc, out = self.run_cmd(whoami.cmd_whoami,
                               ["note", "even shorter", "--topic", "voice",
                                "--supersedes", name])
        self.assertEqual(rc, 0)
        active = whoami.load_notes()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["body"], "even shorter")
        rc, out = self.run_cmd(whoami.cmd_whoami, [])
        self.assertEqual(rc, 0)
        self.assertIn("even shorter", out)
        self.assertNotIn("keep replies short", out)

    def test_note_without_text_usage_error(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = whoami.cmd_whoami(["note", "--topic", "voice"])
        self.assertEqual(rc, 2)
        self.assertIn("usage", buf.getvalue())


class TestInterview(WhoamiBase):
    def run_cmd(self, args, tty, answers=None):
        buf = io.StringIO()
        stdin = mock.Mock()
        stdin.isatty.return_value = tty
        with mock.patch.object(sys, "stdin", stdin), \
                mock.patch("builtins.input", side_effect=answers or []), \
                contextlib.redirect_stdout(buf):
            rc = whoami.cmd_interview(args)
        return rc, buf.getvalue()

    def test_non_tty_prints_question_sheet(self):
        rc, out = self.run_cmd([], tty=False)
        self.assertEqual(rc, 0)
        for q in whoami.QUESTIONS:
            self.assertIn(q["key"], out)
            self.assertIn(q["question"], out)
        self.assertIn("helm whoami note", out)  # how to submit answers
        self.assertEqual(whoami.load_profile()["interview_status"], "offered")

    def test_questions_flag_forces_sheet_even_on_tty(self):
        rc, out = self.run_cmd(["--questions"], tty=True)
        self.assertEqual(rc, 0)
        self.assertIn(whoami.QUESTIONS[0]["question"], out)

    def test_interactive_captures_level_and_notes(self):
        answers = ["expert"] + [""] * (len(whoami.QUESTIONS) - 2) + ["kebab-case names"]
        rc, out = self.run_cmd([], tty=True, answers=answers)
        self.assertEqual(rc, 0)
        p = whoami.load_profile()
        self.assertEqual(p["technical_level"], "expert")
        self.assertEqual(p["interview_status"], "done")
        notes = whoami.load_notes()
        self.assertEqual(len(notes), 1)  # blank answers skipped, level is not a note
        self.assertEqual(notes[0]["topic"], whoami.QUESTIONS[-1]["key"])
        self.assertEqual(notes[0]["body"], "kebab-case names")

    def test_interactive_eof_stops_early_but_completes(self):
        rc, out = self.run_cmd([], tty=True, answers=["technical", EOFError()])
        self.assertEqual(rc, 0)
        p = whoami.load_profile()
        self.assertEqual(p["technical_level"], "technical")
        self.assertEqual(p["interview_status"], "done")

    def test_done_never_nags(self):
        whoami.save_profile({"schema_version": 2, "technical_level": "expert",
                             "guidance": [], "interview_status": "done",
                             "updated_at": "", "source": "fresh"})
        rc, out = self.run_cmd([], tty=True,
                               answers=[AssertionError("interview must not re-run")])
        self.assertEqual(rc, 0)
        self.assertIn("already done", out)
        self.assertIn("--redo", out)

    def test_redo_reruns_after_done(self):
        whoami.save_profile({"schema_version": 2, "technical_level": "expert",
                             "guidance": [], "interview_status": "done",
                             "updated_at": "", "source": "fresh"})
        answers = ["some-technical"] + [""] * (len(whoami.QUESTIONS) - 1)
        rc, out = self.run_cmd(["--redo"], tty=True, answers=answers)
        self.assertEqual(rc, 0)
        self.assertEqual(whoami.load_profile()["technical_level"], "some-technical")


class TestInterviewConfirm(WhoamiBase):
    """Populated profile -> interview by confirmation: drafts to keep/correct/drop."""
    run_cmd = TestInterview.run_cmd
    POP = {"schema_version": 2, "technical_level": "expert founder-operator",
           "guidance": ["headline first", "never punt", "batch deploys"],
           "interview_status": "offered", "updated_at": "",
           "source": "derived-from-corpus 2026-07-19"}

    def populate(self):
        whoami.save_profile(dict(self.POP, guidance=list(self.POP["guidance"])))

    def test_confirm_all_renders_drafts_and_transitions(self):
        self.populate()
        rc, out = self.run_cmd([], tty=True, answers=[""] * 5)  # 4 keeps + end add-new
        self.assertEqual(rc, 0)
        self.assertIn("draft", out)
        self.assertIn(self.POP["technical_level"], out)
        for g in self.POP["guidance"]:
            self.assertIn(g, out)
        self.assertNotIn(whoami.QUESTIONS[1]["question"], out)  # no blank questions
        p = whoami.load_profile()
        self.assertEqual(p["technical_level"], self.POP["technical_level"])
        self.assertEqual(p["guidance"], self.POP["guidance"])
        self.assertEqual(p["interview_status"], "done")
        self.assertTrue(p["source"].startswith("owner-confirmed 20"))
        self.assertTrue(p["updated_at"])
        raw = pk.read_json(whoami.profile_path())
        self.assertEqual(set(raw), {"schema_version", "technical_level", "guidance",
                                    "interview_status", "updated_at", "source"})
        self.assertEqual(raw["schema_version"], 2)

    def test_edit_drop_direct_correction_and_add(self):
        self.populate()
        rc, _ = self.run_cmd([], tty=True, answers=[
            "e", "expert",                 # level: edit -> rewrite
            "",                            # guidance[0]: keep
            "d",                           # guidance[1]: drop
            "batch deploys to slice ends", # guidance[2]: typed correction in-place
            "new: bias-loud", ""])         # add-new, then finish
        self.assertEqual(rc, 0)
        p = whoami.load_profile()
        self.assertEqual(p["technical_level"], "expert")
        self.assertEqual(p["guidance"], ["headline first", "batch deploys to slice ends",
                                         "new: bias-loud"])

    def test_eof_keeps_remaining_drafts_and_completes(self):
        self.populate()
        rc, _ = self.run_cmd([], tty=True, answers=["d", EOFError()])
        self.assertEqual(rc, 0)
        p = whoami.load_profile()
        self.assertEqual(p["technical_level"], "")  # dropped before ctrl-d
        self.assertEqual(p["guidance"], self.POP["guidance"])  # rest stand as drafted
        self.assertEqual(p["interview_status"], "done")

    def test_empty_profile_falls_back_to_blank_interview(self):
        answers = ["expert"] + [""] * (len(whoami.QUESTIONS) - 1)
        rc, out = self.run_cmd([], tty=True, answers=answers)
        self.assertEqual(rc, 0)
        self.assertIn(whoami.QUESTIONS[1]["question"], out)  # blank questions asked
        self.assertNotIn("draft", out)
        self.assertEqual(whoami.load_profile()["technical_level"], "expert")

    def test_non_tty_prints_drafts_and_writes_nothing(self):
        self.populate()
        with open(whoami.profile_path(), "rb") as f:
            before = f.read()
        rc, out = self.run_cmd([], tty=False)  # any input() would StopIteration
        self.assertEqual(rc, 0)
        for g in self.POP["guidance"]:
            self.assertIn(g, out)
        self.assertIn("nothing was written", out)
        with open(whoami.profile_path(), "rb") as f:
            self.assertEqual(f.read(), before)  # byte-identical: no fake confirmation
        self.assertEqual(whoami.load_profile()["interview_status"], "offered")


if __name__ == "__main__":
    unittest.main()
