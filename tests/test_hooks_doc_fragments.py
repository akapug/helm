#!/usr/bin/env python3
"""task/3918: docs/HOOKS.md fragmented to one file per hook row.

THE CLASS. docs/HOOKS.md holds the hook estate table with 11 specs.
Every lane adding or changing a hook (argv-guard, saguide, stop-guard, etc.)
edits that table, so parallel lanes conflict at merge time. Each hook row
is fragmented into docs/hooks/<hook>.md, assembled at read time, with
every existing reader getting byte-identical output.

THE GUARD. In the style of tests/test_change_notes.py, written_under_the_rule
refuses any lane editing the monolith table in docs/HOOKS.md instead of
editing or adding its fragment under docs/hooks/.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from helm import hooks  # noqa: E402

HOOKS_MD = "docs/HOOKS.md"
HOOKS_DIR = "docs/hooks"
HOOKS_README = "docs/hooks/README.md"
POINTER = "<!-- docs/hooks/ holds hook rows; assembled at read time -->"

UNCOMMITTED = "0" * 40
_HEADER = re.compile(r"^([0-9a-f]{40}) \d+ (\d+)(?: \d+)?$", re.M)

CURE = ("write the hook row as docs/hooks/<hook>.md and take it out of "
        "docs/HOOKS.md: a lane never edits the monolith table, and only "
        "assemble_hooks_doc() writes rows there")


def _git(root, *args):
    return subprocess.run(("git", "-C", root) + args, capture_output=True,
                          text=True, timeout=120)


def _out(root, *args):
    p = _git(root, *args)
    if p.returncode != 0:
        raise AssertionError("git %s: %s" % (" ".join(args), p.stderr.strip()))
    return p.stdout


def rule_commits(root):
    """The commits in HEAD's history that added docs/hooks/README.md."""
    readme = os.path.join(HOOKS_DIR, "README.md")
    commits = _out(root, "log", "--format=%H", "--diff-filter=A", "HEAD", "--",
                   readme).split()
    if not commits and os.path.isfile(os.path.join(root, HOOKS_README)):
        commits = [UNCOMMITTED]
    return commits


def table_lines(text):
    """{line number: line} for each line inside the hook estate table in text."""
    out = {}
    lines = text.splitlines()
    header_idx = -1
    for idx, line in enumerate(lines):
        if line.replace(" ", "") == "|event|command|whatitcarries|":
            header_idx = idx
            break
    if header_idx == -1:
        return out
    for offset, line in enumerate(lines[header_idx + 2:], header_idx + 3):
        if not line.startswith("|") and not line.startswith("<!--"):
            break
        stripped = line.strip()
        if stripped and stripped != POINTER:
            out[offset] = line
    return out


def _ranges(numbers):
    """`-L a,b` arguments covering numbers, one per run of consecutive ones."""
    out, run = [], []
    for n in sorted(numbers):
        if run and n != run[-1] + 1:
            out += ["-L", "%d,%d" % (run[0], run[-1])]
            run = []
        run.append(n)
    return out + (["-L", "%d,%d" % (run[0], run[-1])] if run else [])


def written_under_the_rule(root):
    """[(line number, line)] of the lines in root's docs/HOOKS.md table
    that were written by a commit that had the rule, or are not committed yet;
    [] when the rule is not in HEAD's history."""
    target = os.path.join(root, HOOKS_MD)
    if not os.path.isfile(target):
        return []
    with open(target, encoding="utf-8") as fh:
        lines = table_lines(fh.read())
    rules = rule_commits(root)
    if not rules or not lines:
        return []
    blame = _out(root, "blame", "--porcelain", *_ranges(lines), "--", HOOKS_MD)
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
    return [(n, lines[n]) for n in sorted(lines) if after.get(by.get(n), False)]


def misshapen(root):
    """[(path, why)] for each entry under docs/hooks/ that is missing,
    empty or misshapen."""
    base = os.path.join(root, HOOKS_DIR)
    readme = os.path.join(root, HOOKS_README)
    if not os.path.isdir(base):
        return [(HOOKS_DIR, "missing: directory docs/hooks/ does not exist")]
    if not os.path.isfile(readme):
        return [(HOOKS_README, "missing: docs/hooks/README.md says how to write "
                               "hook fragments")]
    out = []
    expected_names = {s["name"] + ".md" for s in hooks.SPECS}
    for name in sorted(os.listdir(base)):
        rel = os.path.join(HOOKS_DIR, name)
        path = os.path.join(base, name)
        if name == "README.md":
            continue
        if not name.endswith(".md") or not os.path.isfile(path):
            out.append((rel, "docs/hooks/ takes only .md files directly under it"))
            continue
        if name not in expected_names:
            out.append((rel, "stray file: %r is not in hooks.SPECS" % name))
            continue
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
        if not text.startswith("|"):
            out.append((rel, "a hook fragment is a markdown table row starting with '|'"))
    for s in hooks.SPECS:
        name = s["name"] + ".md"
        rel = os.path.join(HOOKS_DIR, name)
        path = os.path.join(base, name)
        if not os.path.isfile(path):
            out.append((rel, "missing fragment for spec %r" % s["name"]))
    return out


def assemble_hooks_doc(root=None):
    """The assembled docs/HOOKS.md text with fragments substituted into POINTER."""
    root = root or ROOT
    path = os.path.join(root, HOOKS_MD)
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if POINTER not in text:
        return text
    rows = []
    base = os.path.join(root, HOOKS_DIR)
    for s in hooks.SPECS:
        fpath = os.path.join(base, s["name"] + ".md")
        if not os.path.isfile(fpath):
            return text
        with open(fpath, encoding="utf-8") as fh:
            row = fh.read().rstrip("\r\n")
        rows.append(row + "\n")
    return text.replace(POINTER + "\n", "".join(rows)).replace(POINTER, "".join(rows))


class TheTreeHoldsTheRule(unittest.TestCase):

    def test_assembled_doc_is_byte_identical_to_snapshot(self):
        """Assembling docs/HOOKS.md produces the exact byte-identical text of the snapshot."""
        snapshot_path = os.path.join(ROOT, "tests", "fixtures", "hooks_md_snapshot.md")
        self.assertTrue(os.path.isfile(snapshot_path), "snapshot missing")
        with open(snapshot_path, encoding="utf-8") as fh:
            snapshot = fh.read()
        assembled = assemble_hooks_doc(ROOT)
        self.assertEqual(assembled, snapshot)

    def test_every_spec_has_a_fragment_and_no_stray_files(self):  # noqa: VACUOUS_ASSERTION — a clean docs/hooks/ is the contract; misshapen() drives red on missing/stray/empty
        """Every spec in hooks.SPECS has a fragment under docs/hooks/ and no stray files exist."""
        found = misshapen(ROOT)
        self.assertEqual(found, [], "\n  ".join("%s: %s" % f for f in found))

    def test_monolith_table_has_pointer_and_no_raw_table_rows(self):  # noqa: VACUOUS_ASSERTION — asserts table_lines is empty when POINTER replaces the table
        """docs/HOOKS.md holds the POINTER line and no raw hook rows directly."""
        path = os.path.join(ROOT, HOOKS_MD)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(POINTER, text,
                      "docs/HOOKS.md missing pointer line: %r" % POINTER)
        lines = table_lines(text)
        self.assertEqual(lines, {}, "docs/HOOKS.md still contains raw table rows: %r" % lines)

    def test_no_table_line_was_written_under_the_rule(self):  # noqa: VACUOUS_ASSERTION — no line under the rule is the audit contract
        """This tree carries the rule, and no line in docs/HOOKS.md was written under the rule."""
        self.assertTrue(rule_commits(ROOT),
                        "no commit in this tree's history added %s, so the rule is not armed"
                        % HOOKS_README)
        found = written_under_the_rule(ROOT)
        self.assertEqual(found, [], "a lane edited docs/HOOKS.md's table: " + CURE)


class Planted(unittest.TestCase):
    """A planted git repo testing pre-rule, rule commit, and refusals."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-hooks-doc-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "repo")
        os.makedirs(self.root)
        for args in (("init", "-q", "-b", "main", "--template="),
                     ("config", "user.email", "t@example.invalid"),
                     ("config", "user.name", "t"),
                     ("config", "commit.gpgSign", "false")):
            _out(self.root, *args)
        os.makedirs(os.path.join(self.root, "docs"))
        self.write(HOOKS_MD,
                   "# Hooks\n\n| event | command | what it carries |\n"
                   "|---|---|---|\n"
                   "| `UserPromptSubmit` | `helm inject` | context |\n\n"
                   "- **Denies**: none\n")
        self.before = self.commit("before the rule")

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def commit(self, message):
        _out(self.root, "add", "-A")
        _out(self.root, "commit", "-q", "-m", message)
        return _out(self.root, "rev-parse", "HEAD").strip()

    def test_pre_rule_table_lines_are_accepted(self):  # noqa: VACUOUS_ASSERTION — asserts pre-rule history is exempt
        found = written_under_the_rule(self.root)
        self.assertEqual(found, [])

    def test_uncommitted_table_edit_after_rule_is_refused(self):
        # Introduce rule
        self.write(HOOKS_README, "# hook fragments\n")
        self.write(HOOKS_MD,
                   "# Hooks\n\n| event | command | what it carries |\n"
                   "|---|---|---|\n"
                   "%s\n\n"
                   "- **Denies**: none\n" % POINTER)
        self.commit("arm the rule")
        # Edit table in docs/HOOKS.md without committing
        self.write(HOOKS_MD,
                   "# Hooks\n\n| event | command | what it carries |\n"
                   "|---|---|---|\n"
                   "| `Stop` | `helm stop-guard` | stop |\n\n"
                   "- **Denies**: none\n")
        found = written_under_the_rule(self.root)
        self.assertGreater(len(found), 0)

    def test_committed_table_edit_after_rule_is_refused(self):
        # Introduce rule
        self.write(HOOKS_README, "# hook fragments\n")
        self.commit("arm the rule")
        # Commit edit to table in docs/HOOKS.md
        self.write(HOOKS_MD,
                   "# Hooks\n\n| event | command | what it carries |\n"
                   "|---|---|---|\n"
                   "| `Stop` | `helm stop-guard` | stop |\n\n"
                   "- **Denies**: none\n")
        self.commit("illegal table edit")
        found = written_under_the_rule(self.root)
        self.assertGreater(len(found), 0)


if __name__ == "__main__":
    unittest.main()
