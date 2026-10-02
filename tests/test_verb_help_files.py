#!/usr/bin/env python3
"""One file per verb for the help text, not one shared dict literal.

THE CLASS. `helm/cli_help.py` held the help text of every verb in ONE dict
literal, `_VERB_HELP`. Every lane that adds or changes a verb edited that one
dict, so two lanes in one auto-land train conflicted on it and one car dropped:
a hand rebase, a refused retip and a new row for each. This is the same class
task/3845 cured for CHANGELOG.md with one file per change (`changes/<lane>.md`);
the cure here is one file per verb, `helm/help/<verb>.txt`, and `cli_help`
loads them back into the same `_VERB_HELP` name at import.

THE GUARD asks that `cli_help.py` hold no help-string dict literal again: a
new verb adds a file instead of editing a shared one. A help table is a
multi-entry string table; the module's only legitimate one is `_VERB_HELP`
(now loaded, never literal). The one remaining string dict, `_SPANS`, has two
one-character values and is a bracket table, not a help table, so the rule
fires on a dict literal with THREE or more string-valued entries — a number a
help table always clears and a bracket table never does.

THE SNAPSHOT is `tests/fixtures/verb_help_snapshot.json`, the 118 trunk values
captured once when this lane was cut. It is the source of truth the file and
loader are checked against, and it is static: a later lane that changes a
verb's help updates it in the same commit. The arms below each judge this
tree; none needs a planted history because the defect is the shape of one file.
"""
import ast
import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELP_DIR = os.path.join(ROOT, "helm", "help")
CLIPY = os.path.join(ROOT, "helm", "cli_help.py")
SNAPSHOT = os.path.join(ROOT, "tests", "fixtures", "verb_help_snapshot.json")

#: A dict literal with more string values than this is a help table, not a
#: bracket table: the module's `_SPANS` holds two one-character values, and a
#: per-verb help table clears this line by orders of magnitude.
HELP_TABLE_MIN = 3


def _snapshot():
    """The 118 trunk values, captured when this lane was cut."""
    with open(SNAPSHOT, encoding="utf-8") as fh:
        return json.load(fh)


def _loaded():
    """The help table as `cli_help` now builds it at import."""
    from helm import cli_help
    return cli_help._VERB_HELP


def _files():
    """(name, text) for every file under helm/help/, or None when absent."""
    if not os.path.isdir(HELP_DIR):
        return None
    out = []
    for name in sorted(os.listdir(HELP_DIR)):
        with open(os.path.join(HELP_DIR, name), encoding="utf-8") as fh:
            out.append((name, fh.read()))
    return out


def _dict_literals(path=CLIPY):
    """(lineno, n_string_values) for every dict literal in one source file."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            n = sum(1 for v in node.value.values
                    if isinstance(v, ast.Constant)
                    and isinstance(v.value, str))
            out.append((node.lineno, n))
    return out


class OneFilePerVerb(unittest.TestCase):
    """The shape of helm/help/: one file per verb, nothing else."""

    def test_one_file_per_verb_and_no_strays(self):
        """Each snapshot verb has a <verb>.txt and nothing else is there.

        Red while the dir is absent (the trunk state this lane cures)."""
        want = {name + ".txt" for name in _snapshot()}
        have = None
        rows = _files()
        if rows is not None:
            have = {name for name, _text in rows}
        self.assertIsNotNone(have, "helm/help/ is missing; one file per verb "
                                   "is the cure")
        self.assertEqual(sorted(have), sorted(want),
                         "a stray file or a missing verb in helm/help/")

    def test_no_file_is_empty(self):
        """A help file holding nothing is a verb with no help: red."""
        rows = _files()
        self.assertIsNotNone(rows, "helm/help/ is missing")
        empty = [name for name, text in rows if not text.strip()]
        self.assertEqual(empty, [])

    def test_each_file_holds_its_verb_exactly_plus_a_newline(self):
        """The file's bytes are the snapshot value plus ONE trailing newline.

        The snapshot, not the loader, is the source here: an arm that reads
        back the loader would agree with a loader that stripped the wrong
        number of newlines."""
        snap = _snapshot()
        rows = _files()
        self.assertIsNotNone(rows, "helm/help/ is missing")
        wrong = [name for name, text in rows
                 if name.endswith(".txt") and name[:-4] in snap
                 and text != snap[name[:-4]] + "\n"]
        self.assertEqual(wrong, [])


class TheLoaderKeepsTheTable(unittest.TestCase):
    """`cli_help._VERB_HELP` still holds the same table the files carry."""

    def test_the_loaded_dict_matches_the_trunk_snapshot(self):
        """Same keys and values as trunk. Positive control: passes on trunk
        (the literal equals the snapshot) and must pass after the loader."""
        self.assertTrue(_snapshot(), "the snapshot fixture must hold the "
                                      "trunk table, not be empty")
        self.assertEqual(_loaded(), _snapshot())


class NoHelpTableInCliHelp(unittest.TestCase):
    """The guard: cli_help.py holds no help-string dict literal again."""

    def test_no_dict_literal_is_a_help_table(self):
        """A dict literal with %d+ string values is a help table: it belongs
        in helm/help/, not in the module. The bracket table (_SPANS, two
        one-character values) is below the line and stays green."""
        lits = _dict_literals()
        self.assertTrue(lits, "the scanner must read cli_help.py's dict "
                              "literals, not find none")
        bad = [(line, n) for line, n in lits if n >= HELP_TABLE_MIN]
        self.assertEqual(
            bad, [],
            "cli_help.py holds a help-string dict literal again at line %r; "
            "a new verb adds a file in helm/help/, it does not re-add the "
            "shared table" % (bad,))


if __name__ == "__main__":
    unittest.main()
