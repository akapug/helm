#!/usr/bin/env python3
"""A lane writes its change note in changes/, never in CHANGELOG.md.

THE CLASS. When each lane adds a bullet at the top of CHANGELOG.md's
`## Unreleased`, any two lanes in one auto-land train conflict there at
compose and one car drops: a hand rebase, a refused retip and a new row for
each. So a change note is its own file, `changes/<lane>.md`, and the release
tool's fold (`scripts/release/release.py <version> --fold`) is the one writer
of notes into CHANGELOG.md.

THE GUARD asks who wrote each line that is still under `## Unreleased`. A
diff against a trunk ref would need that ref, and a plain `fab test` ships
the tree and its history but no trunk ref, so there the diff would have no
base. The rule came into the tree with the commit that added
changes/README.md. A line under `## Unreleased` whose commit (git blame) has
that commit in its history, or that is not committed yet, was written by
someone who had the rule, and it is red, named with its line number. The
pointer line (`release.POINTER`) is exempt. So:
  * trunk as it stands passes: each bullet there was written before the rule;
  * the fold passes: it leaves only the pointer under `## Unreleased`;
  * a lane cut before the rule and merged as it was passes, because its
    bullet was written without the rule. Rebased onto a trunk that has the
    rule, the same bullet is red, and the cure is to move it to changes/.
A tree whose history has no changes/README.md has no rule yet.

THE SHAPE. The fold takes each `changes/<name>.md` directly under changes/,
and folds its text into a bullet list. Anything else there (another suffix,
a subdirectory, an empty note, a note that does not open with a bullet)
would be left out or folded as prose, so each is red here.

The live arms judge this tree. The fixture arms drive the same two functions
over a planted history: each outcome above once, the real `--fold` included.
"""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "scripts", "release", "release.py")

#: The release tool's module: the pointer line, the section reader and the
#: note paths come from the one file the fold runs, never from a copy here.
_spec = importlib.util.spec_from_file_location("_release_tool_notes", TOOL)
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

#: The commit git blame names for a line the working tree has and HEAD does not.
UNCOMMITTED = "0" * 40
#: A git blame --porcelain header: the commit, its line, this file's line.
_HEADER = re.compile(r"^([0-9a-f]{40}) \d+ (\d+)(?: \d+)?$", re.M)
CURE = ("write the note as changes/<lane>.md and take it out of "
        "CHANGELOG.md: a lane never edits CHANGELOG.md, and only "
        "`scripts/release/release.py <version> --fold` writes notes there")


def _git(root, *args):
    env = {k: v for k, v in os.environ.items()
           if k not in release._GIT_SELECTION_ENV}
    return subprocess.run(("git", "-C", root) + args, capture_output=True,
                          text=True, env=env, timeout=120)


def _out(root, *args):
    p = _git(root, *args)
    if p.returncode != 0:
        raise AssertionError("git %s: %s" % (" ".join(args), p.stderr.strip()))
    return p.stdout


def rule_commits(root):
    """The commits in HEAD's history that added changes/README.md."""
    return _out(root, "log", "--format=%H", "--diff-filter=A", "HEAD", "--",
                release.CHANGES_README).split()


def unreleased_lines(text):
    """{line number: line} for each line under a `## Unreleased` heading that
    is not blank and not the pointer line."""
    out, inside = {}, False
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("## "):
            inside = (line[3:].split() or [""])[0] == release.UNRELEASED
        elif inside and line.strip() and line.strip() != release.POINTER:
            out[number] = line
    return out


def _ranges(numbers):
    """`-L a,b` arguments covering `numbers`, one per run of consecutive ones."""
    out, run = [], []
    for n in sorted(numbers):
        if run and n != run[-1] + 1:
            out += ["-L", "%d,%d" % (run[0], run[-1])]
            run = []
        run.append(n)
    return out + (["-L", "%d,%d" % (run[0], run[-1])] if run else [])


def written_under_the_rule(root):
    """[(line number, line)] of the lines under `## Unreleased` in root's
    working CHANGELOG.md that were written by a commit that had the rule, or
    are not committed yet; [] when the rule is not in HEAD's history."""
    with open(os.path.join(root, release.CHANGELOG), encoding="utf-8") as fh:
        lines = unreleased_lines(fh.read())
    rules = rule_commits(root)
    if not rules or not lines:
        return []
    blame = _out(root, "blame", "--porcelain", *_ranges(lines), "--",
                 release.CHANGELOG)
    by = {int(line): sha for sha, line in _HEADER.findall(blame)}
    after = {UNCOMMITTED: True}
    for sha in set(by.values()) - {UNCOMMITTED}:
        after[sha] = False
        for rule in rules:
            p = _git(root, "merge-base", "--is-ancestor", rule, sha)
            if p.returncode not in (0, 1):
                raise AssertionError("cannot tell whether %s has %s in its "
                                     "history: %s" % (sha, rule,
                                                      p.stderr.strip()))
            after[sha] = after[sha] or p.returncode == 0
    return [(n, lines[n]) for n in sorted(lines) if after[by[n]]]


def misshapen(root):
    """[(path, why)] for each entry under changes/ the fold would leave out
    or fold as prose; README.md is not a note."""
    base = os.path.join(root, release.CHANGES)
    if not os.path.isfile(os.path.join(root, release.CHANGES_README)):
        return [(release.CHANGES_README, "missing: it says how to write a "
                 "change note")]
    out = []
    for name in sorted(os.listdir(base)):
        rel = release.CHANGES + name
        path = os.path.join(base, name)
        if rel == release.CHANGES_README:
            continue
        if not name.endswith(".md") or not os.path.isfile(path):
            out.append((rel, "the fold takes only a file named <lane>.md "
                             "directly under changes/"))
            continue
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
        if not text.startswith("- "):
            out.append((rel, "a change note is one or more markdown bullets, "
                             "each opening with '- '"))
    return out


def _say(found):
    return "\n  ".join("CHANGELOG.md:%d: %s" % (n, line.strip())
                       for n, line in found)


class TheTreeHoldsTheRule(unittest.TestCase):

    def test_no_line_under_unreleased_was_written_under_the_rule(self):  # noqa: VACUOUS_ASSERTION — no named line is the audit's contract; the armed-rule assertTrue and the pointer assertIn are unconditional positive controls, and the planted arms drive the same function red
        """This tree's own history carries the rule, and CHANGELOG.md still
        has its '## Unreleased' pointer, so the arm below judged something."""
        self.assertTrue(rule_commits(ROOT), "no commit in this tree's history "
                        "added %s, so the rule is not armed"
                        % release.CHANGES_README)
        with open(os.path.join(ROOT, release.CHANGELOG), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("## %s\n%s\n" % (release.UNRELEASED, release.POINTER),
                      text)
        found = written_under_the_rule(ROOT)
        self.assertEqual(found, [], "a lane edited CHANGELOG.md's '## "
                         "Unreleased': " + CURE + ":\n  " + _say(found))

    def test_every_entry_under_changes_is_a_note_the_fold_takes(self):  # noqa: VACUOUS_ASSERTION — a clean changes/ is the audit's contract; the planted shapes arm drives the same misshapen() red on five entries and a missing README
        found = misshapen(ROOT)
        self.assertEqual(found, [], "\n  ".join("%s: %s" % f for f in found))


class Planted(unittest.TestCase):
    """A history with bullets under '## Unreleased' written before the rule,
    then the commit that brings the rule (changes/README.md and the pointer
    line), then whatever each arm writes on top."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-change-notes-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "repo")
        os.makedirs(self.root)
        for args in (("init", "-q", "-b", "main", "--template="),
                     ("config", "user.email", "t@example.invalid"),
                     ("config", "user.name", "t"),
                     ("config", "commit.gpgSign", "false")):
            _out(self.root, *args)
        self.write("helm/__init__.py", '__version__ = "9.9.1"\n')
        self.write(release.CHANGELOG, "# Changelog\n\n## %s\n\n- old one\n"
                   "- old two\n\n## 9.9.0 — 2026-09-01\n\nChanges since "
                   "9.8.0.\n\n- the first\n" % release.UNRELEASED)
        self.before = self.commit("before the rule")
        self.edit(lambda t: t.replace(
            "## %s\n" % release.UNRELEASED,
            "## %s\n%s\n" % (release.UNRELEASED, release.POINTER)))
        self.write(release.CHANGES_README, "# Change notes\n\nOne file each.\n")
        self.rule = self.commit("the rule")

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def text(self):
        with open(os.path.join(self.root, release.CHANGELOG),
                  encoding="utf-8") as fh:
            return fh.read()

    def edit(self, change):
        self.write(release.CHANGELOG, change(self.text()))

    def commit(self, message):
        _out(self.root, "add", "-A")
        _out(self.root, "commit", "-q", "-m", message)
        return _out(self.root, "rev-parse", "HEAD").strip()

    def add_bullet(self, bullet):
        self.edit(lambda t: t.replace(release.POINTER + "\n\n",
                                      release.POINTER + "\n\n%s\n" % bullet, 1))

    def test_trunk_as_it_stands_passes_with_the_rule_armed(self):
        self.assertEqual(rule_commits(self.root), [self.rule])
        self.assertEqual(unreleased_lines(self.text()),
                         {6: "- old one", 7: "- old two"})
        self.assertEqual(written_under_the_rule(self.root), [])

    def test_a_lane_that_adds_a_bullet_after_the_rule_is_named(self):
        self.add_bullet("- a lane's bullet")
        self.commit("a lane")
        self.assertEqual(written_under_the_rule(self.root),
                         [(6, "- a lane's bullet")])

    def test_a_lane_that_rewords_an_old_bullet_is_named(self):
        self.edit(lambda t: t.replace("- old two", "- old two, reworded"))
        self.commit("a lane")
        self.assertEqual(written_under_the_rule(self.root),
                         [(7, "- old two, reworded")])

    def test_an_uncommitted_bullet_is_named(self):
        self.add_bullet("- not committed yet")
        self.assertEqual(written_under_the_rule(self.root),
                         [(6, "- not committed yet")])

    def test_before_the_rule_nothing_is_judged(self):  # noqa: VACUOUS_ASSERTION — nothing judged is the contract without the rule; the after-rule arms drive the same function red on the same edit
        """A history without changes/README.md has no rule: the same edit
        that is named after the rule passes here."""
        _out(self.root, "reset", "-q", "--hard", self.before)
        self.edit(lambda t: t.replace("## %s\n\n" % release.UNRELEASED,
                                      "## %s\n\n- before the rule\n"
                                      % release.UNRELEASED))
        self.commit("a lane before the rule")
        self.assertEqual(rule_commits(self.root), [])
        self.assertEqual(written_under_the_rule(self.root), [])

    def test_a_lane_cut_before_the_rule_passes_until_it_is_rebased(self):
        """Its bullet was written without the rule, so merged as it was it
        passes; rebased onto the rule, the same bullet is named."""
        _out(self.root, "checkout", "-q", "-b", "old-lane", self.before)
        self.edit(lambda t: t.replace("- old one", "- an old lane\n- old one"))
        self.commit("a lane cut before the rule")
        _out(self.root, "checkout", "-q", "main")
        _out(self.root, "merge", "-q", "--no-edit", "old-lane")
        self.assertEqual(written_under_the_rule(self.root), [])
        _out(self.root, "reset", "-q", "--hard", self.rule)
        _out(self.root, "checkout", "-q", "old-lane")
        _out(self.root, "rebase", "-q", "main")
        self.assertEqual(written_under_the_rule(self.root),
                         [(6, "- an old lane")])

    def test_the_release_fold_passes(self):  # noqa: VACUOUS_ASSERTION — only the pointer left under '## Unreleased' is the fold's contract; rc 0, the commit subject and the section equality pin the same fold positively
        """The real `--fold`, on notes under '## Unreleased' and in changes/:
        it leaves only the pointer there, and a note file it folded is gone."""
        self.write("changes/a-lane.md", "- a lane's note\n")
        self.commit("a lane's note")
        needles = os.path.join(self.tmp, "needles.txt")
        with open(needles, "w", encoding="utf-8") as fh:
            fh.write("")
        env = {k: v for k, v in os.environ.items()
               if k not in release._GIT_SELECTION_ENV}
        env["HELM_PRIVATE_NEEDLES"] = needles
        p = subprocess.run((sys.executable, TOOL, "9.9.2", "--fold",
                            "--source", self.root), capture_output=True,
                           text=True, env=env, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(_out(self.root, "log", "-1", "--format=%s").strip(),
                         "release 9.9.2: fold the change notes into "
                         "CHANGELOG.md")
        text = self.text()
        self.assertEqual(unreleased_lines(text), {})
        self.assertEqual(release.changelog_section(text, "9.9.2"),
                         "Changes since 9.9.0.\n\n- old one\n- old two\n"
                         "- a lane's note")
        self.assertFalse(os.path.exists(os.path.join(self.root, "changes",
                                                     "a-lane.md")))
        self.assertEqual(written_under_the_rule(self.root), [])
        self.assertEqual(misshapen(self.root), [])

    def test_every_shape_the_fold_would_skip_or_fold_as_prose_is_named(self):
        self.write("changes/good.md", "- a note\n")
        self.write("changes/prose.md", "A note that is not a bullet.\n")
        self.write("changes/empty.md", "\n")
        self.write("changes/other.txt", "- a note\n")
        self.write("changes/sub/deep.md", "- a note\n")
        self.assertEqual([path for path, _why in misshapen(self.root)],
                         ["changes/empty.md", "changes/other.txt",
                          "changes/prose.md", "changes/sub"])
        os.remove(os.path.join(self.root, release.CHANGES_README))
        self.assertEqual(misshapen(self.root),
                         [(release.CHANGES_README, "missing: it says how to "
                           "write a change note")])


if __name__ == "__main__":
    unittest.main()
