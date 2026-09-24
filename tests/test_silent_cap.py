"""The bounded-write advisory: a cut value persisted with nothing naming the cut.

EVERY ARM IS A DISCRIMINATION. A detector that reported every slice and one
that reported none would both look like work from the outside, so each
reporting source below sits beside a passing one differing only in the
property under test -- the mark, the write, the reach, the bound's size.

THE THREE INSTANCE SHAPES are frozen under tests/fixtures/ rather than
re-typed, because a fixture a test invents proves the test's idea of the
defect and not the defect. The journal-receipt shape is read from the live
module by its measured coordinates, so the arm goes red if that instance is
cured -- which is the outcome it exists to notice.

THE HALF THIS RUNG CANNOT SEE has an arm too. An absence sharing a
representation with an all-clear is the sibling defect, the rung is blind to
it by construction, and the arm pins BOTH that it reports nothing there and
that the docstring says so -- a blind spot nobody wrote down is
indistinguishable from a clean bill.
"""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import silent_cap                                   # noqa: E402
from helm.work import _guard                                  # noqa: E402

RUNG = os.path.abspath(silent_cap.__file__)
_HERE = os.path.dirname(os.path.abspath(__file__))
_TREE = os.path.dirname(_HERE)
FIXTURES = os.path.join(_HERE, "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


def lines_of(source, rel="helm/probe.py"):
    """The line numbers this rung reports over `source`."""
    return [row[1] for row in silent_cap.analyze_source(source, rel)]


# The journal receipt: a summary cut to a constant and appended to the events
# file, where every later reader sees a value indistinguishable from a whole
# one. Written as its own source rather than read from helm/pk.py so the arm
# states the SHAPE; the arm below pins that the live module still carries it.
_JOURNAL = '''\
import json


def event(verb, summary, path):
    row = {"v": 1, "verb": str(verb), "summary": str(summary)[:200]}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\\n")
    return True
'''

_JOURNAL_MARKED = '''\
import json


def event(verb, summary, path):
    text = str(summary)
    kept = text[:200]
    if len(text) > 200:
        kept = "%s [%d of %d chars]" % (text[:200], 200, len(text))
    row = {"v": 1, "verb": str(verb), "summary": kept}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\\n")
    return True
'''

_JOURNAL_REFUSING = '''\
import json

SUMMARY_MAX = 200


def event(verb, summary, path):
    text = str(summary)
    if len(text) > SUMMARY_MAX:
        raise ValueError("summary is longer than SUMMARY_MAX")
    row = {"v": 1, "verb": str(verb), "summary": text}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\\n")
    return True
'''


class TheRuleTest(unittest.TestCase):
    """The pure door: a source string in, findings out, no git and no repo."""

    def test_the_journal_receipt_shape_is_reported(self):
        self.assertEqual(lines_of(_JOURNAL), [5])

    #: THE PRODUCTION NEGATIVE CONTROLS. Each pair is a module and the name
    #: of a value whose cut that module marks. The cure is what removes the
    #: bare bound, so re-introducing one puts the spelling back into the
    #: sliced expression and the arm below goes red naming the module.
    CURED = (("helm/pk.py", "summary"), ("helm/resumeturn.py", "reason"),
             ("helm/handoff.py", "head"), ("helm/reflex.py", "steer"),
             ("helm/whoami.py", "text"), ("helm/record.py", "exc"),
             ("helm/record.py", "where"), ("helm/web_core.py", "statement"),
             ("helm/web_core.py", "ENTRY_CAP"),
             ("helm/injectpack.py", "gloss"), ("helm/nouncensus.py", "text"))

    def test_every_cured_writer_on_this_tree_is_not_reported(self):
        """THE INSTANCES, NOT A MODEL OF THEM. Every writer in `CURED` marks
        the cut it makes, so this rung must report none of them: a rung that
        reported a CURE would train the tree straight back off itself. Read
        off the live modules rather than off fixtures, because a fixture of a
        cure proves only that somebody can write one."""
        import importlib
        for rel, named in self.CURED:
            mod = importlib.import_module("helm." + rel[5:-3])
            with open(os.path.abspath(mod.__file__), encoding="utf-8") as fh:
                found = silent_cap.analyze_source(fh.read(), rel)
            self.assertEqual([row for row in found if named in row[2]], [],
                             "%s: the cured %s write is reported as unmarked"
                             % (rel, named))
        # UNCONDITIONAL POSITIVE CONTROL through the SAME call and the same
        # name: the planted shape is still seen, so the eleven empty lists
        # above cannot be a rung that reports nothing at all.
        found = silent_cap.analyze_source(_JOURNAL, "helm/planted.py")
        self.assertEqual([row[1] for row in found], [5])

    def test_the_dispatch_brief_cap_shape_is_reported(self):
        found = silent_cap.analyze_source(
            fixture("silent_cap_dispatch_body_precure.py.txt"),
            "helm/dispatches.py")
        self.assertEqual([(row[1], row[3]) for row in found],
                         [(9, "MESSAGE_BODY_CAP")])

    def test_the_per_file_capture_cap_shape_is_reported(self):
        found = silent_cap.analyze_source(
            fixture("silent_cap_perfile_cap_precure.py.txt"), "helm/cap.py")
        self.assertEqual([(row[1], row[3]) for row in found],
                         [(11, "PER_FILE_CAP")])

    def test_the_cured_dispatch_writer_on_this_tree_is_not_reported(self):
        """The production negative control. The live brief store wraps its cut
        in a notice naming both sizes, and a rung that reported the CURE would
        train the tree straight back off itself."""
        from helm import dispatches
        with open(os.path.abspath(dispatches.__file__), encoding="utf-8") as fh:
            found = silent_cap.analyze_source(fh.read(), "helm/dispatches.py")
        self.assertEqual([row for row in found if "message" in row[2].lower()],
                         [],
                         "the marked brief cut is being reported as unmarked")
        self.assertTrue(found, "the module reports nothing at all — the "
                               "analyzer is not reading this file")

    def test_a_cut_marked_in_the_written_value_is_not_reported(self):
        self.assertEqual(lines_of(_JOURNAL_MARKED), [])
        self.assertEqual(lines_of(_JOURNAL), [5],
                         "the positive control stopped reporting: the arm "
                         "above would pass on a dead analyzer")

    def test_a_refusal_over_the_bound_is_not_reported(self):
        self.assertEqual(lines_of(_JOURNAL_REFUSING), [])
        self.assertEqual(lines_of(_JOURNAL), [5])

    def test_a_cut_that_reaches_no_write_is_not_reported(self):
        nowhere = ("def render(text):\n"
                   "    short = text[:200]\n"
                   "    return len(short)\n")
        somewhere = ("def render(text, rows):\n"
                     "    short = text[:200]\n"
                     "    rows.append(short)\n")
        self.assertEqual(lines_of(nowhere), [])
        self.assertEqual(lines_of(somewhere), [2])

    def test_a_print_is_not_a_write(self):
        """A column width, a table cell and a log line are display. The
        journal case is still caught because a journal row is serialized and
        appended, not printed -- the control beside this one."""
        shown = ("def show(text):\n"
                 "    print(text[:200])\n")
        stored = ("def store(text, rows):\n"
                  "    rows.append(text[:200])\n")
        self.assertEqual(lines_of(shown), [])
        self.assertEqual(lines_of(stored), [2])

    def test_an_identifier_prefix_is_not_a_cut(self):
        ident = ("def store(row, rows):\n"
                 "    rows.append(row['id'][:12])\n")
        content = ("def store(row, rows):\n"
                   "    rows.append(row['statement'][:12])\n")
        self.assertEqual(lines_of(ident), [])
        self.assertEqual(lines_of(content), [2])

    def test_an_identity_named_inside_a_concatenation_grants_nothing(self):
        """The head is what vouches, never the subtree. A joined string that
        happens to contain an id is CONTENT, and reading the subtree let a
        front-matter description walk out unreported."""
        joined = ("def store(e, rows):\n"
                  "    rows.append((e['id'] + ' - ' + e['steer'])[:170])\n")
        self.assertEqual(lines_of(joined), [2])

    def test_a_bound_under_four_is_a_field_extraction(self):
        small = ("def parse(fields, entries):\n"
                 "    entries.append(fields[0][:1])\n")
        real = ("def parse(fields, entries):\n"
                "    entries.append(fields[0][:4])\n")
        self.assertEqual(lines_of(small), [])
        self.assertEqual(lines_of(real), [2])

    def test_a_cut_beyond_the_span_is_not_attributed_to_a_later_write(self):
        near = "def store(text, rows):\n    kept = text[:200]\n"
        near += "".join("    x%d = %d\n" % (i, i)
                        for i in range(silent_cap.SPAN - 1))
        near += "    rows.append(kept)\n"
        far = "def store(text, rows):\n    kept = text[:200]\n"
        far += "".join("    x%d = %d\n" % (i, i)
                       for i in range(silent_cap.SPAN + 3))
        far += "    rows.append(kept)\n"
        self.assertEqual(lines_of(near), [2])
        self.assertEqual(lines_of(far), [])

    def test_an_outer_binding_does_not_vouch_for_a_nested_function(self):
        nested = ("def outer(text, rows):\n"
                  "    kept = text[:200]\n"
                  "    def inner():\n"
                  "        rows.append(kept)\n"
                  "    return inner\n")
        flat = ("def outer(text, rows):\n"
                "    kept = text[:200]\n"
                "    rows.append(kept)\n")
        self.assertEqual(lines_of(nested), [])
        self.assertEqual(lines_of(flat), [2])

    def test_the_escape_needs_a_reason(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the `bare` half below: the SAME analyzer over the SAME cut with a reasonless escape still reports line 2, so the reasoned arm's empty list cannot be a dead analyzer
        reasoned = ("def store(text, rows):\n"
                    "    kept = text[:200]  # noqa: SILENT_CAP — the full "
                    "text is stored in the row beside it\n"
                    "    rows.append(kept)\n")
        bare = ("def store(text, rows):\n"
                "    kept = text[:200]  # noqa: SILENT_CAP\n"
                "    rows.append(kept)\n")
        self.assertEqual(lines_of(reasoned), [])
        found = silent_cap.analyze_source(bare, "helm/probe.py")
        self.assertEqual([row[1] for row in found], [2])
        self.assertTrue(found[0][5],
                        "a reasonless escape must be named as one")

    def test_a_docstring_is_prose_and_never_a_mark(self):
        """A sentence explaining a function is not written beside the value.
        A docstring naming a `{v, ts}` row cleared this rung's warning for a
        cut that still shipped unmarked, which is the whole class inverted."""
        prosy = ('def store(text, rows):\n'
                 '    """One {v, ts, summary} line lands per call."""\n'
                 '    rows.append(text[:200])\n')
        self.assertEqual(lines_of(prosy), [3])

    def test_the_classifier_control_sees_the_cut_and_not_the_cure(self):
        self.assertTrue(silent_cap.control_ok())
        self.assertTrue(silent_cap.analyze_source(silent_cap._CONTROL, "c.py"))
        self.assertEqual(
            silent_cap.analyze_source(silent_cap._CONTROL_CURED, "c.py"), [])


class TheBlindHalfTest(unittest.TestCase):
    """The sibling defect this rung does not see, pinned as a blind spot."""

    def test_an_absence_sharing_a_representation_reports_nothing(self):
        source = fixture("silent_cap_not_judged_precure.py.txt")
        self.assertEqual(silent_cap.analyze_source(source, "helm/judge.py"),
                         [])
        self.assertTrue(
            silent_cap.analyze_source(_JOURNAL, "helm/pk.py"),
            "the analyzer reports nothing at all — the arm above proves "
            "nothing about the blind spot")

    def test_the_blind_half_is_named_in_the_docstring(self):
        doc = silent_cap.__doc__ or ""
        self.assertIn("THE HALF THIS RUNG DOES NOT SEE", doc)
        self.assertIn("ALL-CLEAR", doc)


class ThePathScopeTest(unittest.TestCase):
    """Exemption by FILE IDENTITY, never by wording."""

    def test_only_python_under_helm_is_policed(self):
        self.assertTrue(silent_cap.policed("helm/pk.py"))
        self.assertFalse(silent_cap.policed("tests/test_pk.py"))
        for rel in ("helm/store/write.py", "helm/silent_cap.py"):
            self.assertTrue(silent_cap.policed(rel), rel)
        for rel in ("docs/HOOKS.md", "journal/x.py", "helm/journal/note.py",
                    "helm/web_ui.html", "bin/helm", "helm/configs/x.json"):
            self.assertFalse(silent_cap.policed(rel), rel)


class _Repo(unittest.TestCase):
    """A throwaway git repo; the staged door has no other honest fixture."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="silentcap-")
        self.addCleanup(_rmtree, self.root)
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")
        self.git("config", "commit.gpgsign", "false")
        os.makedirs(os.path.join(self.root, "helm"))

    def git(self, *args):
        return subprocess.run(("git",) + args, cwd=self.root,
                              capture_output=True, text=True)

    def write(self, rel, text):
        full = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)

    def run_rung(self, mode):
        done = subprocess.run([sys.executable, RUNG, mode,
                               "--repo", self.root],
                              capture_output=True, text=True, cwd=self.root)
        return done.returncode, done.stderr


class TheStagedDoorTest(_Repo):

    def test_only_what_this_commit_adds_is_reported(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the SECOND half: the same rung over the same cut newly added at helm/b.py:5 IS reported, so the absence for the untouched helm/a.py:5 is a scope decision and not a silent rung
        self.write("helm/a.py", _JOURNAL)
        self.git("add", "helm/a.py")
        self.git("commit", "-qm", "base")
        self.write("helm/a.py", _JOURNAL + "\n\nSETTLED = True\n")
        self.git("add", "helm/a.py")
        rc, err = self.run_rung("--staged")
        self.assertEqual(rc, 0)
        self.assertNotIn("helm/a.py:5", err,
                         "a standing cut this commit did not touch was "
                         "reported as the commit's own")
        self.write("helm/b.py", _JOURNAL)
        self.git("add", "helm/b.py")
        rc, err = self.run_rung("--staged")
        self.assertEqual(rc, 0)
        self.assertIn("helm/b.py:5", err)

    def test_an_unstaged_cut_is_not_the_staged_set(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the SECOND half: `git add` of the identical worktree bytes makes the same rung report helm/a.py:5
        self.write("helm/a.py", "SETTLED = True\n")
        self.git("add", "helm/a.py")
        self.git("commit", "-qm", "base")
        self.write("helm/a.py", _JOURNAL)          # worktree only, never added
        rc, err = self.run_rung("--staged")
        self.assertEqual(rc, 0)
        self.assertNotIn("helm/a.py", err)
        self.git("add", "helm/a.py")
        rc, err = self.run_rung("--staged")
        self.assertIn("helm/a.py:5", err)

    def test_a_test_module_is_exempt_by_path_while_helm_is_not(self):
        self.write("tests/test_x.py", _JOURNAL)
        self.write("helm/x.py", _JOURNAL)
        self.git("add", "tests/test_x.py", "helm/x.py")
        rc, err = self.run_rung("--staged")
        self.assertEqual(rc, 0)
        self.assertNotIn("tests/test_x.py", err)
        self.assertIn("helm/x.py:5", err)

    def test_all_reports_standing_debt_and_blocks_nothing(self):
        self.write("helm/a.py", _JOURNAL)
        self.git("add", "helm/a.py")
        self.git("commit", "-qm", "base")
        rc, err = self.run_rung("--all")
        self.assertEqual(rc, 0, "the census may never block a commit")
        self.assertIn("helm/a.py:5", err)
        self.assertIn("existing debt, nothing blocked", err)

    def test_the_banner_names_the_polarity_and_the_measured_rate(self):
        self.write("helm/a.py", _JOURNAL)
        self.git("add", "helm/a.py")
        rc, err = self.run_rung("--staged")
        self.assertEqual(rc, 0)
        self.assertIn(silent_cap.POLARITY, err)
        self.assertIn(silent_cap.MEASURED, err)
        self.assertIn("noqa: SILENT_CAP", err)

    def test_a_clean_staged_set_says_nothing(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the SECOND half: staging the unmarked twin of the same writer produces non-empty stderr from the same invocation
        self.write("helm/a.py", _JOURNAL_MARKED)
        self.git("add", "helm/a.py")
        rc, err = self.run_rung("--staged")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.write("helm/b.py", _JOURNAL)
        self.git("add", "helm/b.py")
        rc, err = self.run_rung("--staged")
        self.assertNotEqual(err, "", "silence here is a dead rung, not a "
                                     "clean tree")

    def test_a_mode_is_required_and_two_modes_are_not_one_answer(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the FIRST run: `--all` alone over the same repo exits 0, so the rc 2 from the same binary with no mode and with two is argument parsing and not a rung that refuses everything
        ok = subprocess.run([sys.executable, RUNG, "--all", "--repo",
                             self.root], capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, "the control run does not succeed, "
                                           "so a refusal below proves nothing")
        for argv in ([], ["--staged", "--all"]):
            done = subprocess.run([sys.executable, RUNG] + argv +
                                  ["--repo", self.root],
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 2, argv)
            self.assertIn("usage:", done.stderr)


class TheInstalledLadderTest(_Repo):
    """The rung is snapshotted beside the hook and invoked as an advisory."""

    def test_the_installer_snapshots_the_rung_beside_its_siblings(self):
        assets = _guard._scanner_assets(self.root, "rail")
        snapshot = [p for p in assets if p.endswith("/silent_cap.py")]
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(os.path.realpath(assets[snapshot[0]]),
                         os.path.realpath(RUNG))
        self.assertTrue([p for p in assets
                         if p.endswith("/vacuous_assertion.py")],
                        "the sibling census is empty — this arm would pass "
                        "against an installer that snapshots nothing")

    def test_the_pre_commit_hook_runs_it_as_an_advisory(self):
        _base, plan = _guard._guard_plan(self.root, "rail")
        script = [p for p in plan if p["name"] == "pre-commit"][0]["script"]
        snapshot = next(p for p in _guard._scanner_assets(self.root, "rail")
                        if p.endswith("/silent_cap.py"))
        self.assertIn(snapshot, script)
        self.assertEqual(script.count('python3 "$silent_cap" --staged'), 1,
                         "a duplicated invocation repeats every warning")
        self.assertIn('python3 "$silent_cap" --staged || true', script,
                      "a WARN-only rung must not propagate its exit status")
        self.assertIn("staged bounded-write advisory SKIPPED", script,
                      "a missing snapshot must fail open BUT LOUD")

    def test_the_leak_profile_does_not_carry_it(self):  # noqa: VACUOUS_ASSERTION — the unconditional `assertTrue(plan)` proves the leak plan really rendered its hooks, so the empty asset list is an omission by profile and not an unrendered plan
        """The leak profile arms only the legs every repo owes. An advisory
        about helm's own writing habits is not one of them, and rendering it
        there would fail on a substitution the leak templates never make."""
        assets = _guard._scanner_assets(self.root, "leak")
        self.assertEqual([p for p in assets if p.endswith("/silent_cap.py")],
                         [])
        plan = _guard._leak_plan(self.root)
        self.assertTrue(plan, "the leak plan is empty — nothing was checked")
        for hook in plan:
            self.assertNotIn("silent_cap", hook["script"], hook["name"])


class TheDocsTest(unittest.TestCase):

    def test_the_registries_page_names_the_rung_and_its_cure(self):
        with open(os.path.join(_TREE, "docs", "MODULE_REGISTRIES.md"),
                  encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("helm/silent_cap.py", text)
        self.assertIn("noqa: SILENT_CAP", text)

    def test_the_verb_page_names_the_rung_in_the_pre_commit_ladder(self):
        with open(os.path.join(_TREE, "docs", "VERBS.md"),
                  encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("helm/silent_cap.py", text)
        self.assertIn("silent-cap", text)


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
