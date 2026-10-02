#!/usr/bin/env python3
"""End-to-end controls for the retired-name pre-commit rung."""
import ast
import collections
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
import tracemalloc
import unicodedata
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import retired_name_rung                                  # noqa: E402
from helm.work import _guard                                       # noqa: E402

RUNG = os.path.abspath(retired_name_rung.__file__)

MODULE = ('_USAGE = "helm chatnode ..."\n'
          "\n\n"
          "def render():\n"
          "    return _USAGE\n"
          "\n\n"
          "class Node:\n"
          "    def helper(self):\n"
          "        return 1\n")
CONSUMER = ("from helm import chatnode\n"
            "\n\n"
            "def test_synopsis():\n"
            "    assert chatnode._USAGE.startswith('helm')\n")
SKIPS = {"HELM_LANE_DISCIPLINE_SKIP": "1", "HELM_INFLIGHT_GATE_SKIP": "1",
         "HELM_NEVER_TRACK_SKIP": "1", "HELM_DOCREF_SKIP": "1",
         "HELM_SEATNAME_SKIP": "1", "HELM_WORLD_PROSE_SKIP": "1"}


class RungBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-retired-name-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "repo")
        os.makedirs(self.root)
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.email", "t@example.invalid"),
                     ("config", "user.name", "t"),
                     ("config", "--local", "helm.guard.profile", "rail")):
            self.assertEqual(self.git(*args).returncode, 0)
        self.stage("helm/chatnode.py", MODULE)
        self.stage("tests/test_synopsis.py", CONSUMER)
        self.assertEqual(self.git("commit", "-qm", "seed").returncode, 0)

    def git(self, *args, env=None):
        return subprocess.run(("git",) + args, cwd=self.root,
                              capture_output=True, text=True, timeout=90,
                              env=dict(os.environ, **(env or {})))

    def stage(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        r = self.git("add", "--", rel)
        self.assertEqual(r.returncode, 0, r.stderr)

    def rung(self):
        return subprocess.run((sys.executable, RUNG, "--staged"),
                              cwd=self.root, capture_output=True, text=True,
                              timeout=90)

    def retire_usage(self):
        """Stage the incident: the constant goes, a rendered function
        replaces it, and the consumer in another module is left alone."""
        self.stage("helm/chatnode.py",
                   MODULE.replace('_USAGE = "helm chatnode ..."\n', "")
                   .replace("    return _USAGE\n",
                            "    return 'helm chatnode ...'\n"))


class ParserTest(unittest.TestCase):
    def test_top_level_removals_are_retired_and_indented_ones_are_not(self):
        diff = ("--- a/helm/m.py\n+++ b/helm/m.py\n"
                "-_USAGE = 1\n-def gone():\n-    def method(self):\n"
                "-    local = 2\n-KEEP: int = 3\n+KEEP: int = 4\n"
                "-class Renamed:\n+class Renamed:\n")
        self.assertEqual([("helm/m.py", "_USAGE", "helm/m.py"),
                          ("helm/m.py", "gone", "helm/m.py")],
                         retired_name_rung.parse(diff))

    def test_a_deleted_file_retires_every_top_level_name(self):
        diff = "--- a/helm/gone.py\n+++ /dev/null\n-class Gone:\n-    x = 1\n"
        self.assertEqual([("helm/gone.py", "Gone", "helm/gone.py")],
                         retired_name_rung.parse(diff))

    def test_a_name_moved_to_another_file_is_still_retired_from_the_old_one(self):
        """TWO FILE PAIRS, not a rename: a.py keeps existing and loses the
        name, so the live path is a.py and the index is asked about it."""
        diff = ("--- a/helm/a.py\n+++ b/helm/a.py\n-def moved():\n"
                "--- a/helm/b.py\n+++ b/helm/b.py\n+def moved():\n")
        self.assertEqual([("helm/a.py", "moved", "helm/a.py")],
                         retired_name_rung.parse(diff))

    def test_a_RENAME_carries_the_path_the_file_now_has(self):
        """ONE pair whose two sides differ IS the rename, and the old path is
        gone from the index — asking about it can only ever answer nothing,
        so the live path travels with the retirement."""
        self.assertEqual(
            [("helm/old.py", "helper", "helm/new.py")],
            retired_name_rung.parse(
                "--- a/helm/old.py\n+++ b/helm/new.py\n-def helper():\n"))

    def test_non_python_files_retire_nothing(self):
        body = "-foo = bar\n-def x():\n"
        self.assertEqual([("docs/x.py", "foo", "docs/x.py"),
                          ("docs/x.py", "x", "docs/x.py")],
                         retired_name_rung.parse(
                             "--- a/docs/x.py\n+++ b/docs/x.py\n" + body),
                         "MUST-HIT: the same lines under a .py path retire "
                         "both names, or the empty answer below is free")
        self.assertEqual([], retired_name_rung.parse(
            "--- a/docs/x.md\n+++ b/docs/x.md\n" + body))


class OutsiderFindingsTest(RungBase):
    """The shapes an outsider read found, each measured before it was cured.

    Three of them are the SAME defect wearing different clothes: a
    line-oriented regex over text answering a question that belongs to
    Python's own grammar. The fourth is a path this rung could not even
    name."""

    def test_a_multiline_parenthesized_import_is_a_consumer(self):
        """`git grep` sees one line at a time, so no line of

            from helm.mod import (
                _USAGE,
            )

        carries `from`, `import` and the name together, and the consumer was
        invisible. Helm's own tree has dozens of imports in this style."""
        self.stage("tests/test_multi.py",
                   "from helm.chatnode import (\n    render,\n    _USAGE,\n)"
                   "\n\n\ndef test_x():\n    return _USAGE\n")
        self.assertEqual(self.git("commit", "-qm", "multi").returncode, 0)
        self.retire_usage()
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("tests/test_multi.py:", r.stderr)

    def test_a_deprecation_comment_or_docstring_is_not_a_consumer(self):
        """THE EXPENSIVE DIRECTION: a note about the retirement, written in
        the file that retires it, is the most likely sentence an author
        writes at that moment — and it refused the commit. A rung that
        refuses correct commits is a rung that gets switched off."""
        self.stage("helm/chatnode.py",
                   "# _USAGE was retired in favour of render\n"
                   "\n\ndef render():\n"
                   '    """Replaces _USAGE."""\n'
                   "    return 'helm chatnode ...'\n")
        self.stage("tests/test_synopsis.py",
                   CONSUMER.replace("chatnode._USAGE", "chatnode.render()"))
        r = self.rung()
        self.assertEqual(r.returncode, 0, r.stderr)
        # MUST-HIT on the same fixture: a real USE in that same file refuses.
        self.stage("helm/chatnode.py",
                   "# _USAGE was retired in favour of render\n"
                   "\n\ndef render():\n    return _USAGE\n")
        self.assertEqual(self.rung().returncode, 1,
                         "a surviving USE in the retiring file must refuse")

    def test_a_tuple_assignment_retires_every_name_it_binds(self):
        self.assertEqual(
            [("helm/m.py", "FOO", "helm/m.py"),
             ("helm/m.py", "BAR", "helm/m.py")],
            retired_name_rung.parse(
                "--- a/helm/m.py\n+++ b/helm/m.py\n-FOO, BAR = 1, 2\n"))

    def test_a_path_git_renders_awkwardly_is_still_read(self):
        """A path with a space arrives with a TRAILING TAB delimiter, and a
        path outside ASCII arrives C-quoted unless quoting is turned off at
        every git call. Either one silently dropped every retirement in that
        file — the failure direction a guard may not have."""
        self.assertEqual(
            [("helm/with space.py", "helper", "helm/with space.py")],
            retired_name_rung.parse(
                "--- a/helm/with space.py\t\n+++ b/helm/with space.py\t\n"
                "-def helper():\n"),
            "the tab delimits the path, it is not part of it")
        for rel in ("helm/with space.py", "helm/caf\u00e9.py"):
            self.stage(rel, "def helper():\n    return 1\n\n\n"
                            "def run():\n    return helper()\n")
            self.assertEqual(self.git("commit", "-qm", "awkward").returncode, 0)
            self.stage(rel, "def run():\n    return helper()\n")
            r = self.rung()
            self.assertEqual(r.returncode, 1,
                             "%s: the retirement was dropped silently: %r"
                             % (rel, r.stderr))
            self.assertEqual(self.git("commit", "-qm", "x", env={
                "HELM_RETIRED_NAME_SKIP": "1"}).returncode, 0)


CORRECTED = "corrected: "


class ReplacementParserTest(unittest.TestCase):
    """The replacement a refusal names is read from the same staged diff."""

    def test_a_rename_in_one_hunk_names_the_new_name(self):
        diff = ("--- a/helm/m.py\n+++ b/helm/m.py\n@@ -1 +1 @@\n"
                "-_USAGE = 1\n+_SYNOPSIS = 1\n")
        self.assertEqual({("helm/m.py", "_USAGE"): ("helm/m.py", "_SYNOPSIS")},
                         retired_name_rung.replacements(diff))

    def test_a_move_to_another_file_names_that_module(self):
        diff = ("--- a/helm/a.py\n+++ b/helm/a.py\n@@ -1 +0,0 @@\n"
                "-def moved():\n"
                "--- a/helm/b.py\n+++ b/helm/b.py\n@@ -0,0 +1 @@\n"
                "+def moved():\n")
        self.assertEqual({("helm/a.py", "moved"): ("helm/b.py", "moved")},
                         retired_name_rung.replacements(diff))

    def test_an_uneven_hunk_names_no_replacement(self):
        diff = ("--- a/helm/m.py\n+++ b/helm/m.py\n@@ -1,2 +1 @@\n"
                "-ONE = 1\n-TWO = 2\n+BOTH = 3\n")
        self.assertEqual({}, retired_name_rung.replacements(diff))
        self.assertEqual({("helm/m.py", "ONE"): ("helm/m.py", "UNO"),
                          ("helm/m.py", "TWO"): ("helm/m.py", "DOS")},
                         retired_name_rung.replacements(
                             "--- a/helm/m.py\n+++ b/helm/m.py\n"
                             "@@ -1,2 +1,2 @@\n-ONE = 1\n-TWO = 2\n"
                             "+UNO = 1\n+DOS = 2\n"),
                         "MUST-HIT: an even hunk pairs in order, or the "
                         "empty answer above is free")

    def test_a_name_changed_in_place_is_not_a_replacement(self):
        diff = ("--- a/helm/m.py\n+++ b/helm/m.py\n@@ -1,2 +1,2 @@\n"
                "-KEEP = 1\n-GONE = 2\n+KEEP = 3\n+NEW = 4\n")
        self.assertEqual({("helm/m.py", "GONE"): ("helm/m.py", "NEW")},
                         retired_name_rung.replacements(diff))


class CureNamesTheLivePathTest(unittest.TestCase):
    """A renamed file is read by its NEW module token, so the cure names it."""

    def test_a_renamed_file_names_the_module_its_readers_spell(self):
        diff = ("--- a/helm/chatnode.py\n+++ b/helm/node.py\n@@ -1 +0,0 @@\n"
                "-_USAGE = 1\n")
        out = io.StringIO()
        retired_name_rung.report(
            {("helm/chatnode.py", "_USAGE"): [
                ("tests/t.py", 4, "node._USAGE"),
                ("helm/node.py", 9, "return _USAGE")]}, out=out, diff=diff)
        self.assertEqual(
            ["corrected: tests/t.py:4: node._USAGE -> (this commit adds no "
             "replacement: rewrite this read, or keep _USAGE defined in "
             "helm/node.py)",
             "corrected: helm/node.py:9: _USAGE -> (this commit adds no "
             "replacement: rewrite this read, or keep _USAGE defined in "
             "helm/node.py)"],
            [l for l in out.getvalue().splitlines()
             if l.startswith(CORRECTED)])


class RefusalPrintsItsCureTest(RungBase):
    """Each read the rung refuses ends in a `corrected:` line naming the
    retired name and what replaces it."""

    def _corrected(self, err):
        return [l for l in err.splitlines() if l.startswith(CORRECTED)]

    def rename_usage(self):
        self.stage("helm/chatnode.py",
                   MODULE.replace('_USAGE = "helm chatnode ..."\n',
                                  '_SYNOPSIS = "helm chatnode ..."\n')
                   .replace("    return _USAGE\n", "    return _SYNOPSIS\n"))

    def test_a_rename_names_the_new_name_at_each_read(self):
        self.rename_usage()
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(
            ["corrected: tests/test_synopsis.py:5: chatnode._USAGE -> "
             "chatnode._SYNOPSIS"], self._corrected(r.stderr))

    def test_applying_the_corrected_line_clears_the_rung(self):  # noqa: VACUOUS_ASSERTION — the refusal on the same staged rename, asserted first, is the unconditional positive control
        self.rename_usage()
        self.assertEqual(self.rung().returncode, 1)
        self.stage("tests/test_synopsis.py",
                   CONSUMER.replace("chatnode._USAGE", "chatnode._SYNOPSIS"))
        r = self.rung()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual("", r.stderr)

    def test_a_stale_spelling_in_the_retiring_file_takes_the_bare_name(self):
        self.stage("helm/chatnode.py",
                   MODULE.replace('_USAGE = "helm chatnode ..."\n',
                                  '_SYNOPSIS = "helm chatnode ..."\n'))
        self.stage("tests/test_synopsis.py",
                   CONSUMER.replace("chatnode._USAGE", "chatnode._SYNOPSIS"))
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(["corrected: helm/chatnode.py:5: _USAGE -> _SYNOPSIS"],
                         self._corrected(r.stderr))

    def test_a_move_names_the_module_that_now_holds_it(self):
        self.stage("helm/chatnode.py",
                   MODULE.replace('_USAGE = "helm chatnode ..."\n', "")
                   .replace("    return _USAGE\n",
                            "    return 'helm chatnode ...'\n"))
        self.stage("helm/synopsis.py", '_USAGE = "helm chatnode ..."\n')
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(
            ["corrected: tests/test_synopsis.py:5: chatnode._USAGE -> "
             "synopsis._USAGE (import it from helm/synopsis.py)"],
            self._corrected(r.stderr))

    def test_no_replacement_in_the_commit_names_both_ways_out(self):
        self.retire_usage()
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(
            ["corrected: tests/test_synopsis.py:5: chatnode._USAGE -> "
             "(this commit adds no replacement: rewrite this read, or keep "
             "_USAGE defined in helm/chatnode.py)"],
            self._corrected(r.stderr))


class StagedScanTest(RungBase):
    def test_the_incident_is_refused_with_the_consumer_path_and_line(self):
        self.retire_usage()
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("[helm retired-name] REFUSED", r.stderr)
        self.assertIn("chatnode._USAGE (retired from helm/chatnode.py)",
                      r.stderr)
        self.assertIn("tests/test_synopsis.py:5:", r.stderr)
        self.assertIn("HELM_RETIRED_NAME_SKIP=1", r.stderr)

    def test_moving_the_consumer_in_the_same_commit_is_admitted(self):  # noqa: VACUOUS_ASSERTION — the refusal on the same staged retirement, asserted first, is the unconditional positive control
        self.retire_usage()
        self.assertEqual(self.rung().returncode, 1)
        self.stage("tests/test_synopsis.py",
                   CONSUMER.replace("chatnode._USAGE", "chatnode.render()"))
        r = self.rung()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual("", r.stderr)

    def test_a_double_spelled_as_a_string_beside_the_module_is_a_consumer(self):  # noqa: VACUOUS_ASSERTION — asserts a nonzero exit and an exact path:line substring of the refusal; nothing here asserts an absence
        self.stage("tests/test_synopsis.py",
                   "from unittest import mock\nfrom helm import chatnode\n"
                   "\n\ndef test_patch():\n"
                   "    with mock.patch.object(chatnode, '_USAGE', 'x'):\n"
                   "        pass\n")
        self.assertEqual(self.git("commit", "-qm", "double").returncode, 0)
        self.retire_usage()
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("tests/test_synopsis.py:6:", r.stderr)

    def test_a_stale_spelling_left_in_the_retiring_file_is_a_consumer(self):
        self.stage("helm/chatnode.py",
                   MODULE.replace('_USAGE = "helm chatnode ..."\n', ""))
        self.stage("tests/test_synopsis.py",
                   CONSUMER.replace("chatnode._USAGE", "chatnode.render()"))
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("helm/chatnode.py:", r.stderr)
        self.assertIn("return _USAGE", r.stderr)

    def test_a_retired_name_nobody_spells_is_simply_gone(self):  # noqa: VACUOUS_ASSERTION — the incident arm above refuses the same rung on the same fixture; this pins the opposite pole
        self.stage("helm/chatnode.py",
                   MODULE.replace("class Node:\n    def helper(self):\n"
                                  "        return 1\n", ""))
        r = self.rung()
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_an_indented_method_is_not_a_retirement(self):  # noqa: VACUOUS_ASSERTION — paired with the top-level refusal above; a method removal is outside the contract by design and the contract says so
        self.stage("tests/test_synopsis.py",
                   CONSUMER + "\n\ndef test_node():\n"
                   "    assert chatnode.Node().helper() == 1\n")
        self.assertEqual(self.git("commit", "-qm", "method").returncode, 0)
        self.stage("helm/chatnode.py",
                   MODULE.replace("    def helper(self):\n        return 1\n",
                                  "    pass\n"))
        self.assertEqual(self.rung().returncode, 0)

    def test_outside_a_work_tree_is_UNKNOWN_not_clean(self):
        r = subprocess.run((sys.executable, RUNG, "--staged"), cwd=self.tmp,
                           capture_output=True, text=True, timeout=90)
        self.assertEqual(r.returncode, 2)
        self.assertIn("not inside a git work tree", r.stderr)



class ADeclaredSatelliteOwnsTheNameTest(RungBase):
    """A name that MOVED to a declared satellite was never retired.

    The contract judges retirement per FILE: a name removed with no `+` line
    in THE SAME FILE adding it back. Splitting a module at the size ceiling
    removes names from one public file and defines them in a sibling, so
    every such split reads as a mass retirement while the names stay
    reachable.

    RELOCATION IS NOT THE PROPERTY, REACHABILITY IS. Exempting a move on
    relocation alone would also exempt a move that forgot to republish, where
    every `module.NAME` consumer really does dangle -- the exact shape this
    rung exists to catch, and the one a focused set cannot see. So the
    exemption is DECLARED: the retiring module carries a literal owner-names
    table naming the satellite and the names it owns, and the satellite must
    actually define each one at column zero. Both halves are read from the
    INDEX with no import.
    """

    OWNED = ('_OWNER_NAMES = (("chatnode_cli", ("_USAGE",)),)\n'
             "\n\n"
             "def render():\n"
             "    return 1\n")
    SATELLITE = '_USAGE = "helm chatnode ..."\n'

    def setUp(self):
        super().setUp()
        self.stage("helm/chatnode.py", MODULE)
        self.stage("tests/test_chatnode.py", CONSUMER)
        self.assertEqual(
            self.git("commit", "--no-verify", "-qm", "seed module").returncode,
            0)

    def test_a_DECLARED_and_DEFINED_move_is_not_a_retirement(self):  # noqa: VACUOUS_ASSERTION — the second staged scan, same rung and fixture with the declaration dropped, is an unconditional must-hit
        """The property. Red against a rung that judges per file."""
        self.stage("helm/chatnode.py", self.OWNED)
        self.stage("helm/chatnode_cli.py", self.SATELLITE)
        r = self.rung()
        self.assertEqual(r.returncode, 0,
                         "a declared, defined move was called a retirement:"
                         "\n%s" % r.stderr)
        # UNCONDITIONAL POSITIVE CONTROL on the same rung and fixture: drop
        # the declaration and the identical move refuses again, so the pass
        # above is the declaration doing work rather than a rung gone quiet.
        self.stage("helm/chatnode.py", "def render():\n    return 1\n")
        self.assertEqual(self.rung().returncode, 1,
                         "the rung cleared everything, so the exemption "
                         "above proved nothing")

    def test_a_move_that_DECLARES_but_does_not_DEFINE_refuses(self):
        """CONTROL. A table may not vouch for a name nobody defines, or the
        declaration becomes a way to silence the rung by typing."""
        self.stage("helm/chatnode.py", self.OWNED)
        self.stage("helm/chatnode_cli.py", "def unrelated():\n    return 1\n")
        r = self.rung()
        self.assertEqual(r.returncode, 1,
                         "a declaration vouched for a name the satellite "
                         "never defines")
        self.assertIn("_USAGE", r.stderr)

    def test_an_UNDECLARED_move_still_refuses(self):
        """CONTROL, and the shape the rung exists for: the names relocate and
        nothing declares them, so every consumer spelling the old module
        dangles."""
        self.stage("helm/chatnode.py",
                   "def render():\n    return 1\n")
        self.stage("helm/chatnode_cli.py", self.SATELLITE)
        r = self.rung()
        self.assertEqual(r.returncode, 1,
                         "an undeclared move was exempted")
        self.assertIn("_USAGE", r.stderr)

    def test_an_UNPARSEABLE_retiring_file_exempts_nothing(self):
        """UNKNOWN IS NOT CLEARANCE. A table this rung cannot read must not
        widen what passes, so the file falls back to being judged."""
        self.stage("helm/chatnode.py", self.OWNED + "def (:\n")
        self.stage("helm/chatnode_cli.py", self.SATELLITE)
        r = self.rung()
        self.assertEqual(r.returncode, 1,
                         "an unreadable owner-names table granted an "
                         "exemption it could not justify")

class InstalledHookTest(RungBase):
    def test_install_snapshots_and_invokes_the_refusing_rung(self):  # noqa: VACUOUS_ASSERTION — snapshot byte equality and the end-to-end commit refusal are unconditional positive controls
        rc, _lines = _guard.install_guard(self.root, apply=True)
        self.assertEqual(rc, 0)
        assets = _guard._scanner_assets(self.root)
        installed = next(p for p in assets
                         if p.endswith("/retired_name_rung.py"))
        with open(installed, "rb") as got, open(RUNG, "rb") as want:
            self.assertEqual(got.read(), want.read())
        with open(_guard.hook_path(self.root, "pre-commit")) as f:
            body = f.read()
        self.assertEqual(body.count('python3 "$retired_name" --staged'), 1)
        self.assertNotIn(RUNG, body)

        self.retire_usage()
        r = self.git("commit", "-m", "retire", env=SKIPS)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[helm retired-name] REFUSED", r.stderr)
        r = self.git("commit", "-m", "retire",
                     env=dict(SKIPS, HELM_RETIRED_NAME_SKIP="1"))
        self.assertEqual(r.returncode, 0, r.stderr)


class NfkcIdentifierLimitTest(RungBase):
    """PYTHON BINDS THE NFKC FORM AND `tokenize` REPORTS THE RAW ONE.

    A file may spell the retired name in a form whose BYTES differ while the
    interpreter binds the very name being retired. That file is a real
    consumer, so the TEST half must find it once the file reaches the test,
    and the PREFILTER half provably cannot -- which is why the limit is
    written down in `consumers` rather than implied.

    Found by probing my own lane's load-bearing claim after an outsider read
    approved it: the claim was "every NAME token is also a word-boundary text
    match", and it is false for exactly this population."""

    WIDE = "\uff32\uff25\uff34\uff29\uff32\uff25\uff24"      # fullwidth RETIRED

    def test_PYTHON_really_binds_the_ascii_name_so_the_case_is_not_invented(self):
        """The control on the premise itself. If this ever stops holding, the
        rest of this class is testing nothing."""
        ns = {}
        exec(self.WIDE + " = 1", ns)                     # noqa: S102
        self.assertIn("RETIRED", ns)

    def test_the_TEST_half_finds_an_nfkc_equivalent_token(self):
        names, _values = retired_name_rung._tokens(
            "from pkg.mod import %s\nprint(%s)\n" % (self.WIDE, self.WIDE))
        self.assertIn("RETIRED", names,
                      "tokens are NFKC-normalised so the test asks the "
                      "question Python asks")

    def test_the_RAW_spelling_is_kept_too_so_a_grep_still_agrees(self):
        names, _values = retired_name_rung._tokens("%s = 1\n" % self.WIDE)
        self.assertIn(self.WIDE, names)

    def test_an_ASCII_token_is_unaffected_so_the_change_is_not_broad(self):
        names, _values = retired_name_rung._tokens("RETIRED = 1\n")
        self.assertEqual({n for n in names if "RETIRE" in n}, {"RETIRED"})

    def test_the_PREFILTER_half_is_the_NAMED_LIMIT_and_still_misses(self):
        """Pins the limit so it cannot be quietly closed or quietly widened.

        If this ever starts finding the file, the prefilter changed -- read the
        limit in `consumers` before editing this expectation."""
        self.stage("pkg/__init__.py", "")
        self.stage("pkg/mod.py", "RETIRED = 1\n")
        self.stage("pkg/plain.py",
                   "from pkg.mod import RETIRED\nprint(RETIRED)\n")
        self.stage("pkg/wide.py",
                   "from pkg.mod import %s\nprint(%s)\n"
                   % (self.WIDE, self.WIDE))
        self.assertEqual(self.git("commit", "-qm", "wide").returncode, 0)
        hits, err = retired_name_rung.consumers(self.root, "pkg/mod.py", "RETIRED")
        self.assertIsNone(err)
        found = {h[0] for h in hits}
        self.assertIn("pkg/plain.py", found,
                      "the ASCII control must be found, or this arm proves "
                      "nothing about the wide one")
        self.assertNotIn("pkg/wide.py", found,
                         "the prefilter cannot search every NFKC-equivalent "
                         "spelling; this is the documented limit")


class PatchSiteIsStructuralTest(unittest.TestCase):
    """PROXIMITY IS EVIDENCE OF TYPING, NEVER OF REFERENCE.

    The string arm asks whether `name` is used as a STRING to reach into
    `mod` -- a patched double. Two cheaper instruments failed first and both
    failures are pinned here, because each one looks correct until measured.

    Found by gemini attacking the arm on a numbered brief."""

    MOD = "chat"

    def _src(self, body):
        return "from helm import chat\n" + body

    def test_IMPORTING_a_module_and_using_a_word_as_a_string_is_not_a_use(self):
        """The file-wide version refused a commit over this shape, measured on
        this repo's own tests/test_chat.py for four different names."""
        src = self._src('log("status")\nchat.other()\n')
        self.assertFalse(retired_name_rung._patch_site(src, self.MOD, "status"))

    def test_the_SAME_LINE_narrowing_is_not_enough_either(self):
        """The measurement that killed proximity outright: module and string
        on ONE line, and it is an argv."""
        src = self._src('chat.cmd_chat(["join", "--room", "main"])\n')
        self.assertFalse(retired_name_rung._patch_site(src, self.MOD, "join"))

    def test_a_REAL_patch_object_is_caught(self):
        src = self._src('mock.patch.object(chat, "status")\n')
        self.assertTrue(retired_name_rung._patch_site(src, self.MOD, "status"))

    def test_a_patch_object_SPLIT_ACROSS_LINES_is_caught(self):
        """The shape no line-oriented or same-line arm could ever see."""
        src = self._src('mock.patch.object(\n    chat,\n    "status",\n)\n')
        self.assertTrue(retired_name_rung._patch_site(src, self.MOD, "status"))

    def test_the_DOTTED_patch_spelling_is_caught(self):
        """The commonest form, and the bare-name comparison missed it: one
        string token carrying the whole path."""
        self.assertTrue(retired_name_rung._patch_site(
            'mock.patch("helm.chat.helper")\n', self.MOD, "helper"))

    def test_getattr_and_setattr_doubles_are_caught(self):
        for fn in ("getattr", "setattr", "delattr", "hasattr"):
            with self.subTest(fn):
                src = self._src('%s(chat, "status")\n' % fn)
                self.assertTrue(
                    retired_name_rung._patch_site(src, self.MOD, "status"))

    def test_a_dotted_string_naming_ANOTHER_module_is_not_a_hit(self):
        """The control on the dotted arm: it must read the module, not just
        the last segment."""
        self.assertFalse(retired_name_rung._patch_site(
            'mock.patch("helm.other.status")\n', self.MOD, "status"))

    def test_the_evidence_KIND_is_reported_so_the_caller_can_tell_them_apart(self):
        """The two hits above are not interchangeable one branch later, and a
        bare True cannot say which one was found."""
        self.assertEqual(
            retired_name_rung._patch_site(
                'mock.patch("helm.chat.helper")\n', self.MOD, "helper"),
            retired_name_rung.PATCH_DOTTED)
        self.assertEqual(
            retired_name_rung._patch_site(
                self._src('mock.patch.object(chat, "status")\n'),
                self.MOD, "status"),
            retired_name_rung.PATCH_STRUCTURAL)

    def test_a_DOTTED_hit_outranks_a_structural_one_in_the_same_file(self):
        """Order of appearance must not decide it: the walk sees the
        structural call first here, and the answer is still DOTTED, because
        the weaker one would make the door demand a token this file may not
        have."""
        src = self._src('mock.patch.object(chat, "status")\n'
                        'mock.patch("helm.chat.status")\n')
        self.assertEqual(retired_name_rung._patch_site(src, self.MOD, "status"),
                         retired_name_rung.PATCH_DOTTED)


class DottedPatchCarriesItsOwnModuleTest(RungBase):
    """THE DOOR ASKED A SELF-NAMING STRING TO NAME ITSELF TWICE.

    `_patch_site` has always read `mock.patch("helm.chatnode._USAGE")`
    correctly and its unit arm above has always been green. `consumers` then
    asked the same file for a `chatnode` NAME token -- which a file patching
    by dotted string alone does not have -- and dropped the hit one branch
    after proving it. The retirement shipped with a live consumer behind it.

    EVERY ARM HERE GOES THROUGH `consumers`, NOT THROUGH THE PREDICATE, which
    is the whole reason the unit arm could not see this: a green predicate and
    a broken door look identical from inside the predicate's own test."""

    def _double(self, target):
        self.stage("tests/test_double.py",
                   "from unittest import mock\n"
                   "\n\n"
                   "def test_it():\n"
                   "    with mock.patch('%s', 'x'):\n"
                   "        pass\n" % target)

    def _consumers(self):
        hits, err = retired_name_rung.consumers(
            self.root, "helm/chatnode.py", "_USAGE")
        self.assertIsNone(err)
        return {h[0] for h in hits}

    def test_a_dotted_patch_with_NO_module_token_is_still_a_consumer(self):
        self._double("helm.chatnode._USAGE")
        self.retire_usage()
        self.assertIn("tests/test_double.py", self._consumers())

    def test_a_dotted_patch_naming_ANOTHER_module_is_still_dropped(self):
        """The loosening is scoped to the module the symbol came from. Without
        this the exception would wave through every file holding any dotted
        string ending in the retired name."""
        self._double("helm.somewhere_else._USAGE")
        self.retire_usage()
        found = self._consumers()
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: the seeded consumer is
        # found unconditionally, so an empty answer cannot be mistaken for
        # a correct refusal.
        self.assertIn("tests/test_synopsis.py", found)
        self.assertNotIn("tests/test_double.py", found)

    def test_a_BARE_string_with_no_module_token_is_still_dropped(self):
        """The false-refusal tier this rung paid for twice stays refused: a
        file-wide text match over string literals is what the AST replaced,
        and loosening the dotted branch must not release it."""
        self.stage("tests/test_double.py",
                   "def test_it():\n"
                   "    log('_USAGE')\n")
        self.retire_usage()
        found = self._consumers()
        self.assertIn("tests/test_synopsis.py", found)   # positive control
        self.assertNotIn("tests/test_double.py", found)


class _PkgAFixture(RungBase):
    """pkg/a.py defines f and g, pkg/c.py defines its own f, and each arm
    commits a pkg/b.py and stages the retirement of `a.f`. No arms here."""

    A = "def f():\n    return 1\n\n\ndef g():\n    return 2\n"
    RETIRED = "def g():\n    return 2\n"

    def setUp(self):
        super().setUp()
        self.stage("pkg/__init__.py", "")
        self.stage("pkg/a.py", self.A)
        self.stage("pkg/c.py", "def f():\n    return 3\n")
        self.assertEqual(self.git("commit", "-qm", "pkg").returncode, 0)

    def retire_f(self, consumer):
        """Commit `consumer` as pkg/b.py, then stage the retirement of a.f."""
        self.stage("pkg/b.py", consumer)
        self.assertEqual(self.git("commit", "-qm", "b").returncode, 0)
        self.stage("pkg/a.py", self.RETIRED)
        return self.rung()

    def judge(self, src):
        """`retire_f(src)`, then undo the b commit so the next shape starts
        from the clean fixture."""
        r = self.retire_f(src)
        reset = self.git("reset", "-q", "--hard", "HEAD~1")
        self.assertEqual(reset.returncode, 0, reset.stderr)
        return r

    def assertRefusesB(self, r):
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("a.f (retired from pkg/a.py)", r.stderr)
        self.assertIn("pkg/b.py:", r.stderr)

    def assertRefusesAt(self, r, src, text):
        """`assertRefusesB`, naming the one line of `src` that is `text`."""
        self.assertRefusesB(r)
        line = src.splitlines().index(text) + 1
        self.assertIn("pkg/b.py:%d: %s" % (line, text), r.stderr)

    def assertEachRefuses(self, sources):
        """Each source, as pkg/b.py, refuses the retirement of a.f. The b
        commit is undone before asserting, so one red shape leaves the next
        one a clean fixture."""
        for src in sources:
            with self.subTest(src):
                r = self.retire_f(src)
                reset = self.git("reset", "-q", "--hard", "HEAD~1")
                self.assertEqual(reset.returncode, 0, reset.stderr)
                self.assertRefusesB(r)


class AModuleTokenIsNotABindingTest(_PkgAFixture):
    """ANOTHER FILE THAT NAMES THE MODULE IS NOT A CONSUMER OF EVERY NAME IT HOLDS.

    The rung asked two token questions of another file: does it hold the
    name, and does it hold the module. It never asked what the name is BOUND
    to. Replaying 8d6fd2216a8, which retires `cell.roster_path`, refused over
    helm/chat.py (`from .seats_common import roster_path`, and `from . import
    cell` 800 lines away) and three `seats.roster_path()` test sites, and
    the author had to commit with HELM_RETIRED_NAME_SKIP=1. Nothing in the
    tree spelled `cell.roster_path`.

    Every arm runs the installed rung end to end, and every clearance is
    paired with a refusal on the same fixture, so a rung that clears
    everything cannot pass."""

    def test_a_name_imported_from_ANOTHER_module_is_not_a_consumer(self):  # noqa: VACUOUS_ASSERTION — the second scan on the same fixture, one real a.f added, must refuse through assertRefusesB (rc 1 plus the exact refusal line)
        """The shape of chat.py. Red on trunk: `a` is a token in b.py."""
        r = self.retire_f("from pkg import a\nfrom pkg.c import f\n\n\n"
                          "def run():\n    return a.g(), f()\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("a.f (retired", r.stderr)
        # POSITIVE CONTROL, SAME FIXTURE AND OBSERVABLE: one real `a.f` in the
        # same file refuses, so the foreign import excuses only itself.
        self.stage("pkg/b.py", "from pkg import a\nfrom pkg.c import f\n\n\n"
                               "def run():\n    return a.f(), f()\n")
        self.assertRefusesB(self.rung())

    def test_a_receiver_bound_to_ANOTHER_module_is_not_a_consumer(self):  # noqa: VACUOUS_ASSERTION — the second scan on the same fixture, one real a.f added, must refuse through assertRefusesB (rc 1 plus the exact refusal line)
        """The shape of the three `seats.roster_path()` test sites."""
        r = self.retire_f("from pkg import a, c\n\n\n"
                          "def run():\n    return a.g(), c.f()\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("a.f (retired", r.stderr)
        self.stage("pkg/b.py", "from pkg import a, c\n\n\n"
                               "def run():\n    return a.f(), c.f()\n")
        self.assertRefusesB(self.rung())

    def test_from_the_module_import_name_still_refuses(self):  # noqa: VACUOUS_ASSERTION — assertRefusesB asserts rc 1 and the exact refusal and path; nothing here asserts an absence
        """CONTROL: the binding resolves TO the retiring module."""
        self.assertRefusesB(self.retire_f(
            "from pkg.a import f\n\n\ndef run():\n    return f()\n"))

    def test_module_dot_name_still_refuses(self):  # noqa: VACUOUS_ASSERTION — assertRefusesB asserts rc 1 and the exact refusal and path; nothing here asserts an absence
        """CONTROL: the incident this rung exists for."""
        self.assertRefusesB(self.retire_f(
            "from pkg import a\n\n\ndef run():\n    return a.f()\n"))

    def test_an_ALIAS_of_the_module_still_refuses(self):  # noqa: VACUOUS_ASSERTION — assertEachRefuses asserts rc 1 and the exact refusal and path per shape; nothing here asserts an absence
        """CONTROL. Trunk refused both spellings (measured: `a` is a token of
        the import), so resolving bindings may not lose them."""
        self.assertEachRefuses(
            imp + "\n\ndef run():\n    return z.f()\n"
            for imp in ("import pkg.a as z\n", "from pkg import a as z\n"))

    def test_a_STAR_import_from_the_module_still_refuses(self):  # noqa: VACUOUS_ASSERTION — assertEachRefuses asserts rc 1 and the exact refusal and path per shape; nothing here asserts an absence
        """CONTROL. A star import binds every public name of the module, so a
        bare `f()` after it may be `a.f`, and a foreign `from pkg.c import f`
        in the same file does not settle which one wins. Green on trunk."""
        self.assertEachRefuses((
            "from pkg.a import *\n\n\ndef run():\n    return f()\n",
            "from pkg.c import f\nfrom pkg.a import *\n\n\n"
            "def run():\n    return f()\n"))

    def test_a_CONDITIONAL_import_from_the_module_still_refuses(self):  # noqa: VACUOUS_ASSERTION — assertEachRefuses asserts rc 1 and the exact refusal and path per shape; nothing here asserts an absence
        """CONTROL. `from a import f` binds `a.f` wherever it stands: under
        `try:` with a foreign fallback, under `if TYPE_CHECKING:`, and in a
        function body beside a foreign import. Green on trunk."""
        self.assertEachRefuses((
            "try:\n    from pkg.a import f\nexcept ImportError:\n"
            "    from pkg.c import f\n\n\ndef run():\n    return f()\n",
            "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n"
            "    from pkg.a import f\n\n\ndef use():\n    return f\n",
            "from pkg.c import g\n\n\ndef run():\n"
            "    from pkg.a import f\n    return f(), g()\n"))

    def test_a_receiver_no_import_binds_still_refuses(self):  # noqa: VACUOUS_ASSERTION — assertRefusesB asserts rc 1 and the exact refusal and path; nothing here asserts an absence
        """CONTROL on the direction of the cure. A parameter can hold the
        module, so only a receiver an IMPORT binds elsewhere is resolved."""
        self.assertRefusesB(self.retire_f(
            "from pkg import a\n\n\ndef run(m):\n    return m.f()\n\n\n"
            "run(a)\n"))

    def test_the_8d6fd2216a8_replay_clears_seats_common_roster_path(self):  # noqa: VACUOUS_ASSERTION — the second scan on the same fixture, one real cellmod.roster_path added, must refuse naming cell.roster_path and the exact line
        """The incident, its spellings copied from 8d6fd2216a8~1 ("cell: pass
        through only the verbs the signer dispatches, and stop reading a
        roster nothing reads"). That commit is on an unlanded lane, so the
        fixture carries the shape rather than depend on the object."""
        self.stage("helm/__init__.py", "")
        cell = ("import os\n\n\ndef profile_name():\n    return 'default'\n"
                "\n\ndef roster_path():\n"
                "    return os.path.join(os.path.expanduser('~'), '.dregg',"
                " 'roster.toml')\n")
        self.stage("helm/cell.py", cell)
        self.stage("helm/seats_common.py",
                   "import os\n\n\ndef _flocked(path):\n"
                   "    return open(path, 'a')\n\n\ndef roster_path():\n"
                   "    return os.path.join('chat', '.roster.json')\n")
        self.stage("helm/seats.py",
                   "from .seats_common import (  # noqa: F401\n"
                   "    _flocked, roster_path,\n)\n")
        self.stage("helm/chat.py",
                   "def _dm_parent_fields(room, ref):\n"
                   "    from .seats_common import _flocked, roster_path\n\n"
                   "    with _flocked(roster_path() + \".lock\"):\n"
                   "        return room, ref\n\n\n"
                   "def _incident_state_unreadable(exc):\n"
                   "    from . import cell\n"
                   "    return {'profile': cell.profile_name(), 'r': str(exc)}\n")
        test_chat = ("from helm import cell as cellmod\n\n\n"
                     "def test_roster():\n"
                     "    from helm import seats\n"
                     "    with open(seats.roster_path(), \"w\","
                     " encoding=\"utf-8\") as f:\n"
                     "        f.write(cellmod.profile_name())\n")
        self.stage("tests/test_chat.py", test_chat)
        self.stage("tests/test_seat_availability_predicate.py",
                   "from helm import (seat, seats,  # noqa: F401\n"
                   "                  web_roster)\n\n\n"
                   "def test_cell():\n"
                   "    cell = 'UNKNOWN kimi'\n"
                   "    with open(seats.roster_path(), \"w\","
                   " encoding=\"utf-8\") as f:\n"
                   "        f.write(cell)\n")
        self.assertEqual(self.git("commit", "-qm", "incident").returncode, 0)
        self.stage("helm/cell.py", cell.split("\n\ndef roster_path")[0] + "\n")
        r = self.rung()
        self.assertNotIn("cell.roster_path", r.stderr)
        self.assertEqual(r.returncode, 0, r.stderr)
        # POSITIVE CONTROL: a real `cell.roster_path` through the alias the
        # test file already holds refuses, and names only that file.
        self.stage("tests/test_chat.py",
                   test_chat + "        f.write(cellmod.roster_path())\n")
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("cell.roster_path (retired from helm/cell.py)", r.stderr)
        self.assertIn("tests/test_chat.py:8:", r.stderr)
        self.assertNotIn("helm/chat.py:", r.stderr)
        self.assertNotIn("tests/test_seat_availability_predicate.py:",
                         r.stderr)


class AFilesOwnTopLevelDefIsABindingTest(_PkgAFixture):
    """ANOTHER FILE'S OWN DEF IS A BINDING, BY THE SHAPE-A TEST AND NO WIDER.

    8b9ecd16e00 (task/3060) retires `chat._ledger_write`. helm/dispatches.py
    imports `chat` inside many functions and has its own, unrelated
    top-level `def _ledger_write`; every bare `_ledger_write` there calls
    that def. The rung refused with 18 lines of dispatches.py, and the author
    had to commit with HELM_RETIRED_NAME_SKIP=1.

    The def was the first binder found to clear a bare spelling, and it was
    cleared by `still_defines` and no wider, so an assignment still refused
    (task/3418, `ARetiredNameIsKeyedToItsModuleTest`). A BARE spelling in
    another file is now never a read of the retiring module, whatever binds
    it there; only a spelling that reads the name FROM the module refuses:
    `a.f`, an alias of the module, a receiver no import binds, `from pkg.a
    import f`, a string reach. Every clearance is paired with a refusal on
    the same fixture."""

    OWN = ("from pkg import a\n\n\n"
           "def f():\n    return 0\n\n\n"
           "def run():\n    return a.g(), f()\n")

    def test_a_file_that_DEFINES_the_name_itself_is_not_a_consumer(self):  # noqa: VACUOUS_ASSERTION — the second scan on the same fixture, one real a.f beside the own def, must refuse through assertRefusesB (rc 1 plus the exact refusal line)
        """The 8b9ecd16e00 shape. Red on trunk: `a` is a token in b.py and
        no foreign import binds `f`."""
        r = self.retire_f(self.OWN)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("a.f (retired", r.stderr)
        # POSITIVE CONTROL, SAME FIXTURE AND OBSERVABLE: one `a.f` beside the
        # own def refuses, so the def clears bare spellings only.
        self.stage("pkg/b.py", self.OWN.replace("a.g(), f()", "a.f(), f()"))
        self.assertRefusesB(self.rung())

    def test_an_own_def_alone_or_an_async_def_or_class_clears(self):  # noqa: VACUOUS_ASSERTION — each shape is paired with the same source plus one real a.f, which must refuse through assertRefusesB
        """The def statement is itself a spelling of the name, so a file
        whose only use is the def holds the token and is still cleared. The
        paired refusal is one read of a.f, not a `del f`: a later `del`
        unbinds b's own f and reads nothing from a (task/3418)."""
        for own in ("def f():\n    return a.g()\n",
                    "async def f():\n    return a.g()\n",
                    "class f:\n    pass\n"):
            with self.subTest(own):
                src = "from pkg import a\n\n\n" + own
                r = self.judge(src)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertRefusesB(self.judge(src + "\n\nX = a.f\n"))

    def test_a_spelling_that_can_reach_the_module_still_refuses(self):  # noqa: VACUOUS_ASSERTION — assertEachRefuses asserts rc 1 and the exact refusal and path per shape; nothing here asserts an absence
        """CONTROL. Beside the own def: `a.f`, an alias of the module, a
        receiver no import binds, and `from pkg.a import f` in a function,
        where the import is the binding that wins."""
        own = "\n\n\ndef f():\n    return 0\n\n\n"
        self.assertEachRefuses((
            self.OWN.replace("a.g(), f()", "a.f(), f()"),
            "import pkg.a as z" + own + "def run():\n    return z.f(), f()\n",
            "from pkg import a as z" + own
            + "def run():\n    return z.f(), f()\n",
            "from pkg import a" + own
            + "def run(m):\n    return m.f(), f()\n\n\nrun(a)\n",
            "from pkg import a  # noqa: F401" + own
            + "def run():\n    from pkg.a import f\n    return f()\n"))

    def test_a_from_import_of_the_name_still_refuses(self):  # noqa: VACUOUS_ASSERTION — assertEachRefuses asserts rc 1 and the exact refusal and path per shape; nothing here asserts an absence
        """CONTROL. `from pkg.a import f` binds a's f, with or without an
        import of the module itself beside it."""
        run = "\n\ndef run():\n    return f()\n"
        self.assertEachRefuses((
            "from pkg import a  # noqa: F401\nfrom pkg.a import f\n\n" + run,
            "from pkg.a import f\n\n" + run))

    def test_any_other_binder_of_the_bare_name_is_admitted(self):  # noqa: VACUOUS_ASSERTION — each shape is paired with the same source plus one real a.f, which must refuse through assertRefusesB
        """task/3418. These shapes refused while the own-def clearance was
        `still_defines` and no wider, and the assignment is the incident:
        helm/dispatches.py binds its own `_FULL_TIP`. A bare `f` in b is b's
        own name whatever binds it -- an assignment from ANOTHER name of the
        module, a def under `if` or `try`, nothing at module scope (a method
        of the name binds none), a def a later `del` or `global` unbinds --
        and none of them reads a.f. Each is admitted, and one `a.f` beside
        it refuses. Red on trunk."""
        run = "\n\ndef run():\n    return f()\n"
        for src in (
                "from pkg import a\n\nf = a.g\n\n" + run,
                "from pkg import a\n\nif a:\n    def f():\n        return 0\n\n"
                + run,
                "from pkg import a  # noqa: F401\n\ntry:\n    def f():\n"
                "        return 0\nexcept NameError:\n    pass\n\n" + run,
                "from pkg import a  # noqa: F401\n\n\nclass C:\n"
                "    def f(self):\n        return 0\n\n" + run,
                self.OWN + "\n\ndel f\n",
                self.OWN + "\n\ndef drop():\n    global f\n    del f\n"):
            with self.subTest(src):
                r = self.judge(src)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertNotIn("a.f (retired", r.stderr)
                self.assertRefusesB(self.judge(src + "\n\nX = a.f\n"))

    def test_the_8b9ecd16e00_replay_clears_dispatches_own_ledger_write(self):  # noqa: VACUOUS_ASSERTION — the second scan on the same fixture, one real _chat._ledger_write added, must refuse naming chat._ledger_write and the exact line
        """The incident, its spellings copied from 8b9ecd16e00 ("argv-guard:
        one delegate rung; ..."). That commit is on an unlanded lane, so the
        fixture carries the shape rather than depend on the object."""
        self.stage("helm/__init__.py", "")
        chat = ("import os\n\n\ndef _default_post_room():\n"
                "    return os.environ.get('HELM_ROOM', 'main')\n\n\n"
                "def _ledger_write(group, act, args):\n"
                '    """Whether verb `act` of the helm verb group `group`, '
                'given `args`,\n    writes the ledger."""\n'
                "    return bool(args)\n")
        self.stage("helm/chat.py", chat)
        dispatches = (
            "import os\n\n"
            "#: How many tries a dispatch-ledger writer makes "
            "(`_ledger_write`).\nLEDGER_TRIES = 3\n\n\n"
            "def _ledger_write(body, path=None, tries=None):\n"
            '    """Run ONE dispatch-ledger write."""\n'
            "    return body(path)\n\n\n"
            "def notify(row, context):\n"
            "    from . import chat\n"
            "    return chat._default_post_room(), '@%s %s' % (row, context)"
            "\n\n\n"
            "def add(attempt, path):\n"
            "    return _ledger_write(attempt, path)\n\n\n"
            "def verdict_room(intent):\n"
            "    if not intent:\n"
            "        from . import chat as _chat\n"
            "        return (os.environ.get('HELM_VERDICT_ROOM')\n"
            "                or _chat._default_post_room())\n"
            "    return intent\n")
        self.stage("helm/dispatches.py", dispatches)
        self.assertEqual(self.git("commit", "-qm", "incident").returncode, 0)
        self.stage("helm/chat.py", chat.split("\n\ndef _ledger_write")[0] + "\n")
        r = self.rung()
        self.assertNotIn("chat._ledger_write", r.stderr)
        self.assertEqual(r.returncode, 0, r.stderr)
        # POSITIVE CONTROL: a real `chat._ledger_write` through the alias the
        # file already uses refuses, and names that line.
        use = "    return _chat._ledger_write('work', 'land', args)"
        self.stage("helm/dispatches.py",
                   dispatches + "\n\ndef census(args):\n"
                   "    from . import chat as _chat\n" + use + "\n")
        line = (dispatches + "\n\ndef census(args):\n").count("\n") + 2
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("chat._ledger_write (retired from helm/chat.py)",
                      r.stderr)
        self.assertIn("helm/dispatches.py:%d: %s" % (line, use), r.stderr)

    def test_a_string_reach_through_an_alias_beats_the_own_def(self):  # noqa: VACUOUS_ASSERTION — each reach is paired with the same source minus the string line, which must clear
        """A review of this lane: beside an own def, `getattr(c, "f")`,
        `c.__dict__["f"]` and `vars(c)["f"]` reach the retired module's f
        through an alias the attribute walk never sees, and cleared anyway."""
        own = "\n\n\ndef f():\n    return 0\n\n"
        for reach in ("import pkg.a as z" + own + "X = getattr(z, 'f')\n",
                      "from pkg import a as z" + own + "X = z.__dict__['f']\n",
                      "import pkg.a as z" + own + "X = vars(z)['f']\n",
                      "import pkg.a as z" + own + "ok = hasattr(z, 'f')\n",
                      # the NON-aliased shapes: the bare name of a
                      # from-import, and the dotted chain of a plain import
                      "from pkg import a" + own + "X = getattr(a, 'f')\n",
                      "from pkg import a" + own + "X = a.__dict__['f']\n",
                      "import pkg.a" + own + "X = getattr(pkg.a, 'f')\n",
                      "import pkg.a" + own + "X = pkg.a.__dict__['f']\n"):
            with self.subTest(reach):
                self.assertRefusesB(self.judge(reach))
        # the same files without the reach clear on the same fixture
        for plain in ("import pkg.a as z" + own,
                      "from pkg import a" + own,
                      "import pkg.a" + own):
            with self.subTest(plain):
                self.assertEqual(self.judge(plain).returncode, 0)


class ARetiredNameIsKeyedToItsModuleTest(RungBase):
    """A RETIRED NAME IS `M.NAME`, NEVER EVERY `NAME` IN THE TREE (task/3418).

    Deleting `findingspass._FULL_TIP` was refused with every bare
    `_FULL_TIP` in helm/dispatches.py, and earlier `findingspass._ID` with
    every bare `_ID` there. dispatches.py imports findingspass inside a
    function and binds its OWN `_FULL_TIP` and `_ID` with assignments, so no
    line of it reads either name from findingspass. The only bypass is an
    owner override, so builders kept the dead names instead.

    A BARE NAME IN ANOTHER MODULE IS THAT MODULE'S OWN BINDING. It reaches
    the retiring module only through a statement that spells the module --
    `from M import NAME`, `NAME = M.NAME`, `getattr(M, "NAME")` -- and each
    of those statements is read in its own right. So the reads of M are the
    evidence and a bare spelling never is. Every admission here is paired
    with a refusal on the same fixture, and every kept refusal is asked in a
    tree where another module still binds the same bare name."""

    FP = ("import re\n\n"
          "from . import dispatches\n\n"
          "_ID = re.compile(r'[0-9a-f]{8,64}')\n"
          "_FULL_TIP = re.compile(r'[0-9a-f]{40,64}')\n\n\n"
          "def stop_record(rid):\n"
          "    if not _ID.fullmatch(str(rid or '')):\n"
          "        return None\n"
          "    return dispatches.row(rid)\n\n\n"
          "def examine(tip):\n"
          "    if not _FULL_TIP.fullmatch(str(tip or '')):\n"
          "        return 'the row names no full tip to read'\n"
          "    return tip\n\n\n"
          "def reader():\n"
          "    return 'qwen'\n")
    #: The shapes of helm/dispatches.py at 9774674b853: its own `_ID`, TWO
    #: module-level bindings of `_FULL_TIP`, each read bare, and findingspass
    #: imported inside the one function that calls it.
    DISP = ("import re\n\n"
            "_ID = re.compile(r'[0-9a-f]{8,64}\\Z')\n"
            "_FULL_TIP = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\\Z')\n\n\n"
            "def row(rid):\n"
            "    return rid if _ID.fullmatch(rid) else None\n\n\n"
            "def _findings_readers():\n"
            "    from . import findingspass\n"
            "    return {'kimi', findingspass.reader()}\n\n\n"
            "def dispatched_tip(tip):\n"
            "    return tip if _FULL_TIP.fullmatch(tip) else None\n\n\n"
            "_FULL_TIP = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')\n\n\n"
            "def superseded(tip, rid):\n"
            "    return _FULL_TIP.fullmatch(tip) and _ID.fullmatch(rid)\n")
    #: The lines of DISP that spell each name as dispatches' own.
    OWN_LINES = {"_ID": (3, 8, 24), "_FULL_TIP": (4, 17, 20, 24)}
    PATTERNS = {"_ID": "r'[0-9a-f]{8,64}'",
                "_FULL_TIP": "r'[0-9a-f]{40,64}'"}

    def setUp(self):
        super().setUp()
        self.stage("helm/__init__.py", "")
        self.stage("helm/findingspass.py", self.FP)
        self.stage("helm/dispatches.py", self.DISP)
        self.assertEqual(self.git("commit", "-qm", "findings").returncode, 0)

    def retire(self, name):
        """Stage findingspass without `name`: its binding goes, and the one
        read of it becomes an inline match."""
        pat = self.PATTERNS[name]
        self.stage("helm/findingspass.py",
                   self.FP.replace("%s = re.compile(%s)\n" % (name, pat), "")
                   .replace("%s.fullmatch(" % name,
                            "re.fullmatch(%s, " % pat))

    def reads_it(self, name):
        """(DISP plus one real read of findingspass.`name`, its line, the
        line's text)."""
        read = "    return findingspass.%s.fullmatch(tip)" % name
        src = (self.DISP + "\n\ndef examine(tip):\n"
               "    from . import findingspass\n" + read + "\n")
        return src, src.count("\n"), read

    def assertRefuses(self, r, name, where, line, text):
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("findingspass.%s (retired from helm/findingspass.py)"
                      % name, r.stderr)
        self.assertIn("%s:%d: %s" % (where, line, text), r.stderr)

    def admitted_beside_its_own_binding(self, name):
        """The replay: `name` leaves findingspass and dispatches keeps its
        own. Admitted; then one real `findingspass.NAME` read refuses."""
        self.retire(name)
        r = self.rung()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("findingspass.%s" % name, r.stderr)
        # POSITIVE CONTROL, SAME FIXTURE AND OBSERVABLE: a read of the
        # retired name through the module, in the file that binds its own.
        src, line, read = self.reads_it(name)
        self.stage("helm/dispatches.py", src)
        self.assertRefuses(self.rung(), name, "helm/dispatches.py", line, read)

    def test_the_FULL_TIP_replay_is_admitted(self):  # noqa: VACUOUS_ASSERTION — the second scan on the same fixture, one real findingspass._FULL_TIP read added, must refuse naming its exact line
        """Red on trunk: 14 lines of the real dispatches.py were listed."""
        self.admitted_beside_its_own_binding("_FULL_TIP")

    def test_the_ID_replay_is_admitted(self):  # noqa: VACUOUS_ASSERTION — the second scan on the same fixture, one real findingspass._ID read added, must refuse naming its exact line
        """Red on trunk: 9 lines of the real dispatches.py were listed."""
        self.admitted_beside_its_own_binding("_ID")

    def test_the_refusal_names_the_read_not_the_files_own_bindings(self):  # noqa: VACUOUS_ASSERTION — assertRefuses asserts rc 1 and the read's exact path:line on the same stderr before each absence
        """Red on trunk: another file's refusal listed every line spelling
        the word, so the one real read sat among dispatches' own lines, and
        past the eighth it was cut to '... and N more'."""
        for name in ("_FULL_TIP", "_ID"):
            with self.subTest(name):
                self.retire(name)
                src, line, read = self.reads_it(name)
                self.stage("helm/dispatches.py", src)
                r = self.rung()
                reset = self.git("reset", "-q", "--hard", "HEAD")
                self.assertEqual(reset.returncode, 0, reset.stderr)
                self.assertRefuses(r, name, "helm/dispatches.py", line, read)
                for own in self.OWN_LINES[name]:
                    self.assertNotIn("helm/dispatches.py:%d:" % own, r.stderr)

    def test_a_bare_spelling_left_in_the_retiring_file_still_refuses(self):  # noqa: VACUOUS_ASSERTION — assertRefuses asserts rc 1 and the retiring file's exact path:line on the same stderr before the absence
        """CONTROL. Inside findingspass a bare `_FULL_TIP` IS findingspass's
        own, and dispatches binding the same word does not excuse it. The
        refusal names that line and no line of dispatches (red on trunk for
        the second half only)."""
        self.stage("helm/findingspass.py", self.FP.replace(
            "_FULL_TIP = re.compile(%s)\n" % self.PATTERNS["_FULL_TIP"], ""))
        r = self.rung()
        self.assertRefuses(r, "_FULL_TIP", "helm/findingspass.py", 15,
                           "    if not _FULL_TIP.fullmatch(str(tip or '')):")
        self.assertNotIn("helm/dispatches.py:", r.stderr)

    def test_a_from_import_of_the_retired_name_still_refuses(self):  # noqa: VACUOUS_ASSERTION — assertRefuses asserts rc 1, the refusal and the exact path:line per shape; nothing here asserts an absence
        """CONTROL. `from M import NAME` binds M's name wherever it stands:
        in a function of the file that binds its own, and as a
        parenthesized import in a test module."""
        in_disp = (self.DISP + "\n\ndef examine(tip):\n"
                   "    from .findingspass import _FULL_TIP as fp_tip\n"
                   "    return fp_tip.fullmatch(tip)\n")
        in_test = ("from helm.findingspass import (\n    examine,\n"
                   "    _FULL_TIP,\n)\n\n\ndef test_tip():\n"
                   "    assert _FULL_TIP.fullmatch('a' * 40) and examine\n")
        for where, src, line in (
                ("helm/dispatches.py", in_disp, in_disp.count("\n") - 1),
                ("tests/test_findingspass.py", in_test, 3)):
            with self.subTest(where):
                self.stage(where, src)
                self.retire("_FULL_TIP")
                r = self.rung()
                reset = self.git("reset", "-q", "--hard", "HEAD")
                self.assertEqual(reset.returncode, 0, reset.stderr)
                self.assertRefuses(r, "_FULL_TIP", where, line,
                                   src.splitlines()[line - 1])

    def test_a_patch_target_naming_it_still_refuses(self):  # noqa: VACUOUS_ASSERTION — each admitted target is followed on the same fixture by one naming findingspass, which must refuse naming its exact line
        """CONTROL. A dotted target names its own module, so it refuses with
        no `findingspass` token in the file, and `patch.object` refuses
        through the module. The same dotted target naming dispatches, whose
        `_FULL_TIP` is its own, is admitted on the same fixture."""
        dotted = ("from unittest import mock\n\n\n"
                  "def test_tip():\n"
                  "    with mock.patch('helm.%s._FULL_TIP', None):\n"
                  "        pass\n")
        by_object = ("from unittest import mock\n\n"
                     "from helm import findingspass\n\n\n"
                     "def test_tip():\n"
                     "    with mock.patch.object(findingspass, '_FULL_TIP',"
                     " None):\n"
                     "        pass\n")
        self.stage("tests/test_tip.py", dotted % "dispatches")
        self.retire("_FULL_TIP")
        r = self.rung()
        self.assertEqual(r.returncode, 0, r.stderr)
        for src, line in ((dotted % "findingspass", 5), (by_object, 7)):
            with self.subTest(src):
                self.stage("tests/test_tip.py", src)
                self.assertRefuses(self.rung(), "_FULL_TIP",
                                   "tests/test_tip.py", line,
                                   src.splitlines()[line - 1])

    def test_a_getattr_through_the_module_or_an_alias_still_refuses(self):  # noqa: VACUOUS_ASSERTION — the admitted getattr on dispatches is followed on the same fixture by each getattr on findingspass, which must refuse naming its exact line
        """CONTROL, and one shape trunk missed: `getattr(fp, '_FULL_TIP')`
        through `import helm.findingspass as fp`, in a file holding no
        `_FULL_TIP` NAME token, was dropped before the alias was asked.
        `getattr(dispatches, '_FULL_TIP')` is dispatches' own and admitted."""
        self.stage("tests/test_tip.py",
                   "from helm import dispatches\n\n\ndef test_tip():\n"
                   "    return getattr(dispatches, '_FULL_TIP')\n")
        self.retire("_FULL_TIP")
        r = self.rung()
        self.assertEqual(r.returncode, 0, r.stderr)
        for src in ("from helm import findingspass\n\n\ndef test_tip():\n"
                    "    return getattr(findingspass, '_FULL_TIP')\n",
                    "import helm.findingspass as fp\n\n\ndef test_tip():\n"
                    "    return getattr(fp, '_FULL_TIP')\n"):
            with self.subTest(src):
                self.stage("tests/test_tip.py", src)
                self.assertRefuses(self.rung(), "_FULL_TIP",
                                   "tests/test_tip.py", 5,
                                   src.splitlines()[4])

    def test_a_facade_that_publishes_it_from_the_module_still_refuses(self):  # noqa: VACUOUS_ASSERTION — the two admitted facades are followed on the same fixture by one naming findingspass beside the retiring file, which must refuse naming its exact line
        """A facade's `_OWNER_NAMES` entry ("findingspass", ("_FULL_TIP",))
        publishes findingspass's name as the facade's own -- the web_compat
        form, bound by the satellite's publish loop or by a module
        `__getattr__` -- so deleting the name breaks every reader of the
        facade. Red on trunk: the entry holds both words only as strings and
        the rung asked for NAME tokens. Keyed to the module: the same entry
        in a facade in another directory names ANOTHER findingspass, and an
        entry naming dispatches names dispatches' own name; both are
        admitted."""
        facade = ('_OWNER_NAMES = (("%s", ("_FULL_TIP",)),)\n\n\n'
                  "def __getattr__(name):\n"
                  "    for module, names in _OWNER_NAMES:\n"
                  "        if name in names:\n"
                  "            import importlib\n"
                  "            return getattr(importlib.import_module(\n"
                  "                '.' + module, __package__), name)\n"
                  "    raise AttributeError(name)\n")
        self.stage("tools/ledger.py", facade % "findingspass")
        self.stage("helm/ledger.py", facade % "dispatches")
        self.retire("_FULL_TIP")
        r = self.rung()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.stage("helm/ledger.py", facade % "findingspass")
        r = self.rung()
        self.assertRefuses(r, "_FULL_TIP", "helm/ledger.py", 1,
                           (facade % "findingspass").splitlines()[0])
        self.assertNotIn("tools/ledger.py:", r.stderr)

    def test_a_dynamic_reach_on_another_module_counts_nothing_here(self):  # noqa: VACUOUS_ASSERTION — the admitted reach on `re` is followed on the same fixture by the same reach on findingspass, which must refuse naming the string's exact line
        """task/3418 r2. A file that reaches a module by a name it computes
        reads every string spelling the retired name -- but only for THAT
        module. dispatches reaching `re` with the key '_FULL_TIP' beside
        its own bare `_FULL_TIP` is admitted; the same key reaching
        findingspass refuses at the string."""
        on_re = (self.DISP + "\n\ndef lookup(key='_FULL_TIP'):\n"
                 "    return getattr(re, key)\n")
        on_fp = (self.DISP + "\n\ndef lookup(key='_FULL_TIP'):\n"
                 "    from . import findingspass\n"
                 "    return getattr(findingspass, key)\n")
        self.stage("helm/dispatches.py", on_re)
        self.retire("_FULL_TIP")
        r = self.rung()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.stage("helm/dispatches.py", on_fp)
        text = "def lookup(key='_FULL_TIP'):"
        self.assertRefuses(self.rung(), "_FULL_TIP", "helm/dispatches.py",
                           on_fp.splitlines().index(text) + 1, text)


class ADynamicReachOnTheModuleReadsItsNameTest(_PkgAFixture):
    """A FILE THAT REACHES THE MODULE BY A NAME IT COMPUTES READS EVERY
    STRING AND KEYWORD SPELLING THE RETIRED NAME (task/3418, round 2).

    A non-author approval-tier read of the module key, measured on a
    git-archive of the lane tip: tests/test_gate_fifo.py has a helper
    `guard_reason(**patches)` that runs `mock.patch.object(gatechild, name,
    value)` for each keyword, and calls it with `_arm_parent_death=...` at
    three lines. Staging a rename of gatechild._arm_parent_death, trunk
    refused at those three lines, the tip admitted, and the composed train
    failed AttributeError: the tip followed no name through a variable. The
    same class, trunk refusing and the tip admitting: `patch.multiple(M,
    X=1)`, `patch.multiple("pkg.M", X=1)`, `globals().update(vars(M))` then
    a bare X, and attrgetter, getattr_static, __getattribute__ and
    vars(M).get with the name held in a variable.

    A DYNAMIC REACH on the module is one of the calls that read a module's
    name through a string -- getattr, setattr, delattr, hasattr, patch.object,
    getattr_static, __getattribute__, attrgetter, `vars(M).get`, `vars(M)[k]`
    -- with a name that is not a constant; `globals().update(vars(M))`; and
    `patch.multiple` on the module or its dotted string. In a file with one,
    every keyword argument named NAME and every string constant equal to
    NAME is a read. A reach on ANOTHER module counts nothing for this one,
    and a bare NAME is still the file's own binding, except after
    `globals().update(vars(M))`, which binds it from the module. Every
    admission is paired with a refusal on the same fixture, and every
    refusal names its line."""

    def assertEachRefusesAt(self, table):
        for src, text in table:
            with self.subTest(src):
                self.assertRefusesAt(self.judge(src), src, text)

    def assertAdmittedThenRefused(self, admitted, refused, text):
        """`admitted` clears as pkg/b.py; `refused`, the same file reaching
        pkg.a instead, refuses at the line `text`."""
        r = self.judge(admitted)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("a.f (retired", r.stderr)
        self.assertRefusesAt(self.judge(refused), refused, text)

    def test_a_helper_patching_the_module_for_each_keyword_refuses(self):  # noqa: VACUOUS_ASSERTION — assertAdmittedThenRefused pairs the admitted reach on pkg.c with the same helper on pkg.a, which must refuse naming the keyword's exact line
        """The gate_fifo shape on the pkg fixture: the keyword is the read.
        The same helper patching pkg.c is admitted."""
        src = ("from unittest import mock\n\n"
               "from pkg import a, c  # noqa: F401\n\n\n"
               "def patched(**patches):\n"
               "    return [mock.patch.object(%s, name, value)\n"
               "            for name, value in patches.items()]\n\n\n"
               "def test_it():\n"
               "    a.g()\n"
               "    return patched(\n"
               "        f=1)\n")
        self.assertAdmittedThenRefused(src % "c", src % "a", "        f=1)")

    def test_patch_multiple_on_the_module_or_its_dotted_string_refuses(self):  # noqa: VACUOUS_ASSERTION — assertRefusesAt asserts rc 1 and the exact path:line per shape; nothing here asserts an absence
        """The keyword of `patch.multiple` is the read, and a dotted target
        needs no `a` token in the file. `**{'f': 1}` reads through the
        string."""
        self.assertEachRefusesAt((
            ("from unittest import mock\n\nfrom pkg import a\n\n\n"
             "def test_it():\n    with mock.patch.multiple(a, f=1):\n"
             "        pass\n", "    with mock.patch.multiple(a, f=1):"),
            ("from unittest import mock\n\n\ndef test_it():\n"
             "    with mock.patch.multiple('pkg.a', f=1):\n        pass\n",
             "    with mock.patch.multiple('pkg.a', f=1):"),
            ("from unittest.mock import patch\n\nfrom pkg import a\n\n\n"
             "def test_it():\n    with patch.multiple(a, **{'f': 1}):\n"
             "        pass\n", "    with patch.multiple(a, **{'f': 1}):")))

    def test_patch_multiple_on_another_module_is_admitted(self):  # noqa: VACUOUS_ASSERTION — assertAdmittedThenRefused pairs each admitted target with the same call on pkg.a, which must refuse naming its exact line
        """CONTROL. pkg.c binds its own f, and a target inside the module
        (`pkg.a.Klass`) patches the class, not the module."""
        src = ("from unittest import mock\n\n"
               "from pkg import a, c  # noqa: F401\n\n\n"
               "def test_it():\n    with mock.patch.multiple(%s, f=1):\n"
               "        return a.g()\n")
        for other in ("c", "'pkg.c'", "'pkg.a.Klass'"):
            with self.subTest(other):
                self.assertAdmittedThenRefused(
                    src % other, src % "a",
                    "    with mock.patch.multiple(a, f=1):")

    def test_globals_updated_from_the_module_then_a_bare_name_refuses(self):  # noqa: VACUOUS_ASSERTION — assertAdmittedThenRefused pairs the admitted update from pkg.c with each update from pkg.a, which must refuse naming the bare read's exact line
        """`globals().update(vars(a))` is `from pkg.a import *` at run time,
        so the bare `f` after it is a's. The same update from pkg.c is
        admitted."""
        src = ("from pkg import a, c  # noqa: F401\n\n"
               "globals().update(%s)\n\n\n"
               "def run():\n    return f(), a.g()\n")
        for mine in ("vars(a)", "a.__dict__"):
            with self.subTest(mine):
                self.assertAdmittedThenRefused(
                    src % "vars(c)", src % mine, "    return f(), a.g()")

    def test_a_name_held_in_a_variable_refuses_where_it_is_spelled(self):  # noqa: VACUOUS_ASSERTION — assertAdmittedThenRefused pairs each admitted reach on pkg.c with the same reach on pkg.a, which must refuse naming the string's exact line
        """Every call that reads a module's name through a string, with the
        string bound to a variable first. The same reach on pkg.c is
        admitted."""
        head = ("import inspect  # noqa: F401\n"
                "from operator import attrgetter  # noqa: F401\n"
                "from unittest import mock  # noqa: F401\n\n"
                "from pkg import a, c  # noqa: F401\n\n"
                "NAME = 'f'\n\n\n"
                "def run():\n    a.g()\n    return %s\n")
        for reach in ("getattr(@, NAME)", "hasattr(@, NAME)",
                      "setattr(@, NAME, 1)", "delattr(@, NAME)",
                      "inspect.getattr_static(@, NAME)",
                      "@.__getattribute__(NAME)",
                      "object.__getattribute__(@, NAME)",
                      "vars(@).get(NAME)", "@.__dict__.get(NAME)",
                      "vars(@)[NAME]", "@.__dict__[NAME]",
                      "attrgetter(NAME)(@)",
                      "mock.patch.object(@, NAME, 1)"):
            with self.subTest(reach):
                self.assertAdmittedThenRefused(
                    head % reach.replace("@", "c"),
                    head % reach.replace("@", "a"), "NAME = 'f'")

    def test_a_constant_name_through_each_new_reacher_refuses(self):  # noqa: VACUOUS_ASSERTION — assertAdmittedThenRefused pairs each admitted spelling with the same reach naming 'f', which must refuse naming its exact line
        """attrgetter, getattr_static, __getattribute__ and `.get` on the
        module's dict read a constant name directly. `attrgetter('x.f')`
        reads a.x.f, and is admitted."""
        head = ("import inspect  # noqa: F401\n"
                "from operator import attrgetter  # noqa: F401\n\n"
                "from pkg import a\n\n\n"
                "def run():\n    return a.g(), %s\n")
        for reach in ("attrgetter('%s')(a)", "attrgetter('%s.x')(a)",
                      "inspect.getattr_static(a, '%s')",
                      "a.__getattribute__('%s')",
                      "object.__getattribute__(a, '%s')",
                      "vars(a).get('%s')", "a.__dict__.get('%s')"):
            with self.subTest(reach):
                refused = head % (reach % "f")
                self.assertAdmittedThenRefused(
                    head % (reach % "x.f"), refused,
                    refused.splitlines()[-1])

    def test_a_dynamic_reach_counts_strings_and_keywords_not_a_bare_binding(self):  # noqa: VACUOUS_ASSERTION — assertAdmittedThenRefused pairs the admitted file with the same file plus one call passing 'f', which must refuse naming its exact line
        """CONTROL on the width of the cure. b binds its own f and reaches
        a by a computed key: the bare `f` is b's, and admitted. One call
        passing the key 'f' refuses at that line."""
        src = ("from pkg import a\n\n\n"
               "def f():\n    return 0\n\n\n"
               "def run(key):\n    return getattr(a, key), f()\n")
        self.assertAdmittedThenRefused(src, src + "\n\nrun('f')\n",
                                       "run('f')")


class TheGateFifoReplayTest(RungBase):
    """The approval-tier replay (task/3418, round 2), its shapes copied from
    tests/test_gate_fifo.py:84-88, :183, :233 and :238 at c4cece950cc:
    `guard_reason(**patches)` patches gatechild for each keyword, and three
    calls pass `_arm_parent_death=`. A rename of gatechild._arm_parent_death
    refused on trunk at the three keyword lines and was admitted on the lane
    tip; the composed train then failed AttributeError, because
    `patch.object` without `create=True` needs the attribute to exist."""

    CHILD = ("import os\n\n\n"
             "def _arm_parent_death(parent, parent_start):\n"
             "    return bool(parent and parent_start)\n\n\n"
             "def _proc_rows():\n    return {}\n\n\n"
             "def _guard(argv):\n"
             "    if not _arm_parent_death(os.getppid(), 0):\n"
             "        return 125\n"
             "    return 0 if _proc_rows() and argv else 125\n")
    FIFO = ("import io\nimport sys\nimport unittest\n"
            "from unittest import mock\n\n"
            "from helm import gate, gatechild  # noqa: F401\n\n\n"
            "class GuardReasonTest(unittest.TestCase):\n"
            "    GUARD_ARGV = ['--']\n\n"
            "    def guard_reason(self, **patches):\n"
            "        child = io.StringIO()\n"
            "        managers = [mock.patch.object(sys, 'stderr', child)]\n"
            "        managers.extend(mock.patch.object(%s, name, value)\n"
            "                        for name, value in patches.items())\n"
            "        for manager in managers:\n"
            "            manager.__enter__()\n"
            "        self.assertEqual(gatechild._guard(self.GUARD_ARGV), 125)\n"
            "        return child.getvalue()\n\n"
            "    def test_reason(self):\n"
            "        self.guard_reason(\n"
            "            _arm_parent_death=mock.Mock(return_value=True),\n"
            "            _proc_rows=mock.Mock(return_value={}))\n\n"
            "    def test_distinct(self):\n"
            "        missing = self.guard_reason(\n"
            "            _arm_parent_death=mock.Mock(return_value=True),\n"
            "            _proc_rows=mock.Mock(return_value={}))\n"
            "        denied = self.guard_reason(\n"
            "            _arm_parent_death=mock.Mock(return_value=True))\n"
            "        self.assertNotEqual(missing, denied)\n")

    def setUp(self):
        super().setUp()
        self.stage("helm/__init__.py", "")
        self.stage("helm/gate.py", "def _guard_supervisor():\n    return None\n")
        self.stage("helm/gatechild.py", self.CHILD)
        self.stage("tests/test_gate_fifo.py", self.FIFO % "gatechild")
        self.assertEqual(self.git("commit", "-qm", "fifo").returncode, 0)
        self.stage("helm/gatechild.py", self.CHILD.replace(
            "_arm_parent_death", "_arm_death_signal"))

    def test_the_rename_refuses_at_each_keyword_line(self):  # noqa: VACUOUS_ASSERTION — asserts rc 1, the refusal and each of the three keyword lines by exact path:line
        """Red on the lane tip: admitted."""
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("gatechild._arm_parent_death (retired from "
                      "helm/gatechild.py)", r.stderr)
        lines = [(n, t) for n, t in enumerate(
            (self.FIFO % "gatechild").splitlines(), 1)
            if "_arm_parent_death=" in t]
        self.assertEqual(len(lines), 3)
        for n, t in lines:
            self.assertIn("tests/test_gate_fifo.py:%d: %s" % (n, t), r.stderr)

    def test_the_same_helper_patching_another_module_is_admitted(self):  # noqa: VACUOUS_ASSERTION — the sibling arm refuses the same rename on the same fixture with the helper patching gatechild
        """CONTROL. The helper patches `gate`, and the file still reads
        `gatechild._guard`: no keyword there reads gatechild's name."""
        self.stage("tests/test_gate_fifo.py", self.FIFO % "gate")
        r = self.rung()
        self.assertEqual(r.returncode, 0, r.stderr)


class UnparseableSourceIsNotCleanTest(RungBase):
    """A FILE THIS RUNG CANNOT READ IS NOT A FILE IT CLEARED.

    `_tokens` once answered empty sets for an unparseable source, and
    empty sets are indistinguishable from "holds nothing" -- so a consumer
    with a live use at the TOP and a syntax error at the BOTTOM was dropped
    in silence and the commit passed unchecked. A guard may not turn "I
    cannot read this" into "this is clean". Found by gemini."""

    def test_a_source_that_does_not_parse_answers_none(self):
        self.assertIsNone(retired_name_rung._tokens("x = [\n"))
        self.assertIsNotNone(retired_name_rung._tokens("x = 1\n"))

    def test_an_UNPARSEABLE_consumer_is_reported_not_dropped(self):
        self.stage("pkg/__init__.py", "")
        self.stage("pkg/mod.py", "RETIRED = 1\n")
        self.stage("pkg/broken.py",
                   "from pkg.mod import RETIRED\nprint(RETIRED)\nx = [\n")
        self.assertEqual(self.git("commit", "-qm", "broken").returncode, 0)
        hits, err = retired_name_rung.consumers(self.root, "pkg/mod.py",
                                                "RETIRED")
        self.assertIsNone(err)
        self.assertIn("pkg/broken.py", {h[0] for h in hits},
                      "an unreadable candidate must be reported, because "
                      "nothing here can rule it out")

    #: It tokenizes, and `ast.parse` raises MemoryError ("Parser stack
    #: overflowed"), which no parse site caught: the approval-tier read
    #: found it uncaught in `consumers` (task/3418, round 2).
    DEEP = "X = " + "-" * 100000 + "1\n"

    def test_a_parser_stack_overflow_is_unparsed_at_every_parse(self):
        self.assertIsNotNone(retired_name_rung._tokens(self.DEEP))
        with self.assertRaises(MemoryError):
            ast.parse(self.DEEP)
        source = retired_name_rung._Source
        self.assertTrue(retired_name_rung.still_defines(
            source("def f():\n    pass\n"), "f"))      # the control
        self.assertFalse(retired_name_rung.still_defines(
            source(self.DEEP + "def f():\n    pass\n"), "f"))
        self.assertIn("f", retired_name_rung._top_level_names(
            source("f = 1\n")))                         # the control
        self.assertEqual(set(), retired_name_rung._top_level_names(
            source(self.DEEP + "f = 1\n")))
        # No tree, so no `_OWNER_NAMES` declaration can be read from it.
        self.assertIsNone(source(self.DEEP).index)
        self.assertFalse(retired_name_rung._patch_site(
            "from helm import chat\n" + self.DEEP
            + "getattr(chat, 'f')\n", "chat", "f"))

    def test_a_consumer_the_parser_overflows_on_is_reported(self):  # noqa: VACUOUS_ASSERTION — asserts the exact (path, line, text) hit is present; nothing here asserts an absence
        """Red on the lane tip: MemoryError escaped `consumers`, and the
        rung died with a traceback instead of a refusal."""
        self.stage("pkg/__init__.py", "")
        self.stage("pkg/mod.py", "RETIRED = 1\n")
        self.stage("pkg/deep.py",
                   "from pkg import mod\n" + self.DEEP + "print(mod.RETIRED)\n")
        self.assertEqual(self.git("commit", "-qm", "deep").returncode, 0)
        hits, err = retired_name_rung.consumers(self.root, "pkg/mod.py",
                                                "RETIRED")
        self.assertIsNone(err)
        self.assertIn(("pkg/deep.py", 3, "print(mod.RETIRED)"), hits)


class _CellFixture(RungBase):
    """The helm/cell.py repository the merge arms share: the base defines
    MODE, profile_name and roster_path, tests/test_cell.py spells
    `cell.roster_path()`, and branch `trunk` starts there. No arms here."""

    CELL = ("MODE = 'base'\n\n\n"
            "def profile_name():\n    return 'default'\n\n\n"
            "def roster_path():\n    return 'roster.toml'\n")
    USES = ("from helm import cell\n\n\n"
            "def test_roster():\n    return cell.roster_path()\n")
    #: What trunk left behind when it retired `roster_path`: a receiver no
    #: import binds, beside a `cell` token. The rung reads it as a use (its
    #: docstring names this cost), so trunk's author committed with the
    #: override -- and every lane that merges trunk inherits the spelling.
    TRUNK_DEBT = ("from helm import cell\n\n\n"
                  "def test_roster(r):\n"
                  "    return cell.profile_name(), r.roster_path()\n")

    def setUp(self):
        super().setUp()
        self.stage("helm/cell.py", self.CELL)
        self.stage("tests/test_cell.py", self.USES)
        self.commit("cell")
        self.assertEqual(self.git("branch", "trunk").returncode, 0)

    def commit(self, msg, cwd=None):
        r = self.run_git(cwd or self.root, "commit", "-qm", msg)
        self.assertEqual(r.returncode, 0, r.stderr)

    def run_git(self, cwd, *args, env=None):
        return subprocess.run(("git",) + args, cwd=cwd, capture_output=True,
                              text=True, timeout=90,
                              env=dict(os.environ, **(env or {})))

    def write(self, cwd, rel, text):
        with open(os.path.join(cwd, rel), "w", encoding="utf-8") as f:
            f.write(text)
        r = self.run_git(cwd, "add", "--", rel)
        self.assertEqual(r.returncode, 0, r.stderr)

    def rung_in(self, cwd):
        return subprocess.run((sys.executable, RUNG, "--staged"), cwd=cwd,
                              capture_output=True, text=True, timeout=90)

    def trunk_retires_roster_path(self):
        """On trunk: MODE moves and `roster_path` goes, leaving the debt."""
        self.assertEqual(self.git("checkout", "-q", "trunk").returncode, 0)
        self.stage("helm/cell.py",
                   self.CELL.replace("'base'", "'trunk'")
                   .split("\n\n\ndef roster_path")[0] + "\n")
        self.stage("tests/test_cell.py", self.TRUNK_DEBT)
        self.commit("trunk retires cell.roster_path")
        self.assertEqual(self.git("checkout", "-q", "main").returncode, 0)

    def lane_moves_mode(self, cwd):
        self.write(cwd, "helm/cell.py", self.CELL.replace("'base'", "'lane'"))
        self.commit("lane moves MODE", cwd=cwd)

    def conflicted_merge(self, cwd, resolution):
        """`git merge trunk` stops on the MODE line; stage `resolution`."""
        r = self.run_git(cwd, "merge", "trunk")
        self.assertNotEqual(r.returncode, 0, "the fixture must conflict")
        self.assertIn("CONFLICT", r.stdout + r.stderr)
        self.write(cwd, "helm/cell.py", resolution)
        merge_head = self.run_git(cwd, "rev-parse", "-q", "--verify",
                                  "MERGE_HEAD")
        self.assertEqual(merge_head.returncode, 0, "the merge is in progress")

    def without_roster_path(self, mode):
        return (self.CELL.replace("'base'", "'%s'" % mode)
                .split("\n\n\ndef roster_path")[0] + "\n")


class WhatTheCommitItselfRetiresTest(_CellFixture):
    """THE TWO SHAPES THAT OPENED task/3025, and what the rung answers now.
    Every fixture is a real repository with real commits and merges.

    A. A NAME THE FILE STILL DEFINES IS NOT RETIRED. A module defined
    `_own_commits` twice at column zero and a commit renamed one copy to
    `_reflog_biography`. The diff carries `-def _own_commits` and no `+` line
    adds it back, while the file still defines the name with a def, so the
    rung clears it.

    B. A MERGE IS JUDGED AGAINST ITS FIRST PARENT, as every commit is.
    Merging trunk into a lane charges the lane with every retirement trunk
    made since the lane base, and the rung refuses it. Lanes compose in the
    train room with trunk as the first parent; HELM_RETIRED_NAME_SKIP=1 is
    the override for a merge that must be made the other way."""

    # 1 -----------------------------------------------------------------
    def test_1_an_ordinary_def_deletion_with_a_consumer_still_refuses(self):
        self.stage("helm/cell.py",
                   self.CELL.split("\n\n\ndef roster_path")[0] + "\n")
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("cell.roster_path (retired from helm/cell.py)", r.stderr)
        self.assertIn("tests/test_cell.py:5:", r.stderr)

    # 2 -----------------------------------------------------------------
    TWICE = ("def _own_commits():\n    return 1\n\n\n"
             "def run():\n    return _own_commits()\n\n\n"
             "def _own_commits():\n    return 2\n")

    def test_2_renaming_ONE_of_two_duplicate_defs_retires_nothing(self):  # noqa: VACUOUS_ASSERTION — the second scan on the same fixture, the surviving copy renamed too, must refuse naming walker._own_commits
        """Shape A. Red on the base: the second copy still defines the name."""
        self.stage("helm/walker.py", self.TWICE)
        self.stage("tests/test_walker.py",
                   "from helm import walker\n\n\n"
                   "def test_it():\n    return walker._own_commits()\n")
        self.commit("walker, with the name defined twice")
        once = self.TWICE.replace("def _own_commits():\n    return 1",
                                  "def _reflog_biography():\n    return 1")
        self.stage("helm/walker.py", once)
        r = self.rung()
        self.assertEqual(r.returncode, 0,
                         "the file still defines the name:\n%s" % r.stderr)
        # MUST-HIT ON THE SAME FIXTURE: rename the surviving copy too and the
        # name is gone from the file while both consumers still spell it.
        self.stage("helm/walker.py",
                   once.replace("\n\n\ndef _own_commits():\n    return 2\n",
                                "\n\n\ndef _own_walk():\n    return 2\n"))
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("walker._own_commits (retired from helm/walker.py)",
                      r.stderr)

    # 3 -----------------------------------------------------------------
    def test_3_renaming_the_ONLY_def_while_references_remain_refuses(self):
        """Shape A's negative: one definition, renamed, consumers left."""
        self.stage("helm/cell.py",
                   self.CELL.replace("def roster_path", "def roster_file"))
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("cell.roster_path (retired from helm/cell.py)", r.stderr)
        self.assertIn("tests/test_cell.py:5:", r.stderr)

    # 4 -----------------------------------------------------------------
    def test_4_merging_trunk_into_a_lane_is_charged_trunks_retirement(self):  # noqa: VACUOUS_ASSERTION — asserts rc 1, the exact refusal and trunk's own consumer line
        """Shape B refuses. Trunk is the SECOND parent, the staged diff is
        taken against the first, and trunk's `-def roster_path` is in it
        beside trunk's own remaining spelling."""
        self.trunk_retires_roster_path()
        self.lane_moves_mode(self.root)
        self.conflicted_merge(self.root, self.without_roster_path("merged"))
        first = self.run_git(self.root, "diff", "--cached", "-U0", "HEAD",
                             "--", "*.py")
        self.assertIn(("helm/cell.py", "roster_path", "helm/cell.py"),
                      retired_name_rung.parse(first.stdout))
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("cell.roster_path (retired from helm/cell.py)", r.stderr)
        self.assertIn("tests/test_cell.py:5:", r.stderr)

    def test_4c_shape_B_through_the_installed_hook_refuses_until_overridden(self):  # noqa: VACUOUS_ASSERTION — asserts the exact refusal lines, then a committed merge with three rev-list tokens
        """Shape B at the door a conflicted merge is concluded by: refused,
        and HELM_RETIRED_NAME_SKIP=1 commits it as a real two-parent merge."""
        self.trunk_retires_roster_path()
        self.lane_moves_mode(self.root)
        rc, _lines = _guard.install_guard(self.root, apply=True)
        self.assertEqual(rc, 0)
        self.conflicted_merge(self.root, self.without_roster_path("merged"))
        r = self.git("commit", "--no-edit", env=SKIPS)
        self.assertNotEqual(r.returncode, 0, "trunk's retirement was admitted")
        self.assertIn("[helm retired-name] REFUSED", r.stderr)
        self.assertIn("cell.roster_path (retired from helm/cell.py)", r.stderr)
        self.assertIn("tests/test_cell.py:5:", r.stderr)
        r = self.git("commit", "--no-edit",
                     env=dict(SKIPS, HELM_RETIRED_NAME_SKIP="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        parents = self.git("rev-list", "--parents", "-n", "1", "HEAD")
        self.assertEqual(3, len(parents.stdout.split()), parents.stdout)

    # 5 -----------------------------------------------------------------
    def test_5_a_resolution_that_drops_a_name_both_parents_keep_refuses(self):  # noqa: VACUOUS_ASSERTION — asserts the exact refusal lines, then a committed merge with three rev-list tokens
        """Both parents define `roster_path` and both still spell
        `cell.roster_path()`; the conflict resolution drops the def. The
        merge itself retires it, so it is judged. Through the installed
        hook, at the door a conflicted merge is concluded by."""
        self.assertEqual(self.git("checkout", "-q", "trunk").returncode, 0)
        self.stage("helm/cell.py", self.CELL.replace("'base'", "'trunk'"))
        self.commit("trunk moves MODE")
        self.assertEqual(self.git("checkout", "-q", "main").returncode, 0)
        self.lane_moves_mode(self.root)
        rc, _lines = _guard.install_guard(self.root, apply=True)
        self.assertEqual(rc, 0)
        self.conflicted_merge(
            self.root, self.CELL.replace("'base'", "'merged'")
            .split("\n\n\ndef roster_path")[0] + "\n")
        r = self.git("commit", "--no-edit", env=SKIPS)
        self.assertNotEqual(r.returncode, 0, "the merge's own retirement "
                            "was admitted")
        self.assertIn("[helm retired-name] REFUSED", r.stderr)
        self.assertIn("cell.roster_path (retired from helm/cell.py)", r.stderr)
        self.assertIn("tests/test_cell.py:5:", r.stderr)
        # 8: THE OVERRIDE STILL ADMITS IT, and the commit is a real merge.
        r = self.git("commit", "--no-edit",
                     env=dict(SKIPS, HELM_RETIRED_NAME_SKIP="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        parents = self.git("rev-list", "--parents", "-n", "1", "HEAD")
        self.assertEqual(3, len(parents.stdout.split()), parents.stdout)

    # 6 -----------------------------------------------------------------
    def test_6_a_merge_that_retires_nothing_passes(self):  # noqa: VACUOUS_ASSERTION — the second scan on the same in-progress merge, with the def dropped from the result, must refuse
        self.assertEqual(self.git("checkout", "-q", "trunk").returncode, 0)
        self.stage("helm/extra.py", "def extra():\n    return 1\n")
        self.commit("trunk adds a module")
        self.assertEqual(self.git("checkout", "-q", "main").returncode, 0)
        self.stage("helm/lane.py", "def lane():\n    return 1\n")
        self.commit("lane adds a module")
        r = self.git("merge", "--no-commit", "--no-ff", "trunk")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.rung()
        self.assertEqual(r.returncode, 0, r.stderr)
        # POSITIVE CONTROL: the same merge, resolved without the def.
        self.stage("helm/cell.py",
                   self.CELL.split("\n\n\ndef roster_path")[0] + "\n")
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("cell.roster_path (retired from helm/cell.py)", r.stderr)

    # 7 -----------------------------------------------------------------
    def test_7_a_commit_with_no_parent_is_judged_as_it_was(self):  # noqa: VACUOUS_ASSERTION — pins today's answer on a parentless commit; the must-hit arms above run the same rung on parented commits
        """Today an unborn HEAD diffs against the empty tree, so no `-` line
        exists and nothing is retired. A root commit, and an orphan branch
        whose index drops a def a staged consumer still spells."""
        fresh = os.path.join(self.tmp, "fresh")
        os.makedirs(os.path.join(fresh, "helm"))
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.email", "t@example.invalid"),
                     ("config", "user.name", "t")):
            self.assertEqual(self.run_git(fresh, *args).returncode, 0)
        self.write(fresh, "helm/cell.py", self.CELL)
        r = self.rung_in(fresh)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.git("checkout", "-q", "--orphan",
                                  "fresh").returncode, 0)
        self.stage("helm/cell.py",
                   self.CELL.split("\n\n\ndef roster_path")[0] + "\n")
        r = self.rung()
        self.assertEqual(r.returncode, 0, r.stderr)


class OnlyADefOrClassKeepsTheNameTest(RungBase):
    """SHAPE A, AND NOTHING WIDER. A retired name is cleared only when the
    file still has an unconditional top-level def, async def or class of it,
    with no module-scope `del` of it after that statement and no `global`
    or `nonlocal` of it anywhere in the file. Every other binder -- an
    assignment, an annotation, an import, a loop or with target -- is not
    counted, so the rung refuses as it did before the clearance existed.
    Each clearance here is paired with a refusal on the same fixture."""

    CONSUMER = ("from helm import knob\n\n\n"
                "def test_it():\n    return knob.LIMIT\n")

    def judge(self, before, after):
        """Commit `before` as helm/knob.py beside a consumer of knob.LIMIT,
        stage `after`, run the rung, then put the work tree back."""
        self.stage("helm/knob.py", before)
        self.stage("tests/test_knob.py", self.CONSUMER)
        r = self.git("commit", "-q", "--allow-empty", "-m", "knob")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.stage("helm/knob.py", after)
        r = self.rung()
        reset = self.git("reset", "-q", "--hard", "HEAD")
        self.assertEqual(reset.returncode, 0, reset.stderr)
        return r

    def assertRefuses(self, r):
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("knob.LIMIT (retired from helm/knob.py)", r.stderr)
        self.assertIn("tests/test_knob.py:5:", r.stderr)

    def test_F2_a_kept_duplicate_followed_by_a_top_level_del_refuses(self):  # noqa: VACUOUS_ASSERTION — assertRefuses asserts rc 1, the exact refusal and the consumer line
        """The review's reproduction: the copy that survives is deleted
        further down, so the module ends with LIMIT unbound."""
        self.assertRefuses(self.judge(
            "def LIMIT():\n    return 1\n\n\ndel LIMIT\n\n\n"
            "def LIMIT():\n    return 2\n",
            "def LIMIT():\n    return 1\n\n\ndel LIMIT\n"))

    def test_a_duplicate_kept_and_then_DELETED_refuses_where_shape_A_passes(self):  # noqa: VACUOUS_ASSERTION — the second scan on the same fixture, a del after the kept copy, must refuse through assertRefuses
        twice = "def LIMIT():\n    return 1\n\n\ndef LIMIT():\n    return 2\n"
        r = self.judge(twice, "def LIMIT():\n    return 1\n")
        self.assertEqual(r.returncode, 0,
                         "shape A: the kept copy defines LIMIT:\n%s" % r.stderr)
        self.assertRefuses(self.judge(
            twice, "def LIMIT():\n    return 1\n\n\ndel LIMIT\n"))

    def test_F2_a_bare_annotation_left_where_the_def_was_refuses(self):  # noqa: VACUOUS_ASSERTION — assertRefuses asserts rc 1, the exact refusal and the consumer line per shape
        """The review's second reproduction. `LIMIT: object` declares a
        type and binds nothing, whether the commit writes it in place of the
        def or it was already there."""
        for before in ("def LIMIT():\n    return 1\n",
                       "LIMIT: object\n\n\ndef LIMIT():\n    return 1\n"):
            with self.subTest(before):
                self.assertRefuses(self.judge(before, "LIMIT: object\n"))

    def test_a_surviving_ASYNC_def_or_CLASS_keeps_the_name(self):  # noqa: VACUOUS_ASSERTION — the second scan on each fixture, a trailing del added, must refuse through assertRefuses
        for survivor in ("async def LIMIT():\n    return 1\n",
                         "class LIMIT:\n    pass\n"):
            with self.subTest(survivor):
                before = survivor + "\n\ndef LIMIT():\n    return 2\n"
                r = self.judge(before, survivor)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertRefuses(
                    self.judge(before, survivor + "\n\ndel LIMIT\n"))

    def test_a_remaining_binder_that_is_not_a_def_or_class_refuses(self):  # noqa: VACUOUS_ASSERTION — assertRefuses asserts rc 1, the exact refusal and the consumer line per shape
        """The def goes and another binder of the name stays in the file,
        unchanged by the commit. Some of these leave `knob.LIMIT` resolving
        at runtime and some do not; the rung does not model which, and
        refuses every one. The re-export by import is the correct commit
        this costs, and the override exists for it."""
        self.stage("helm/knob_impl.py", "def LIMIT():\n    return 1\n")
        for binder in ("LIMIT = 1\n",
                       "LIMIT, OTHER = 1, 2\n",
                       "LIMIT: int = 1\n",
                       "from helm.knob_impl import LIMIT\n",
                       "from helm.knob_impl import *\n",
                       "import os as LIMIT\n",
                       "for LIMIT in (1,):\n    pass\n",
                       "with open(__file__) as LIMIT:\n    pass\n"):
            with self.subTest(binder):
                self.assertRefuses(self.judge(
                    binder + "\n\ndef LIMIT():\n    return 1\n", binder))

    def test_a_zero_trip_loop_left_where_the_assignment_was_refuses(self):  # noqa: VACUOUS_ASSERTION — assertRefuses asserts rc 1, the exact refusal and the consumer line
        """`for LIMIT in ():` never binds LIMIT. A loop target is not
        counted at all, so whether the loop runs is never asked."""
        self.assertRefuses(self.judge("LIMIT = 1\n",
                                      "for LIMIT in ():\n    pass\n"))

    def test_a_GLOBAL_of_the_name_anywhere_refuses(self):  # noqa: VACUOUS_ASSERTION — the first scan on the same fixture, with no global, must pass; each shape then refuses through assertRefuses
        """A class body that declares `global LIMIT` and deletes it unbinds
        the module's name while the module runs, and a function that does
        the same unbinds it when called. The rung does not model which
        runs, so a `global` of the name anywhere in the file refuses."""
        twice = "def LIMIT():\n    return 1\n\n\ndef LIMIT():\n    return 2\n"
        kept = "def LIMIT():\n    return 1\n"
        r = self.judge(twice, kept)
        self.assertEqual(r.returncode, 0, r.stderr)
        for tail in ("class C:\n    global LIMIT\n    del LIMIT\n",
                     "def drop():\n    global LIMIT\n    del LIMIT\n"):
            with self.subTest(tail):
                self.assertRefuses(self.judge(twice, kept + "\n\n" + tail))


class StillDefinedIsAnUnconditionalDefOrClassTest(unittest.TestCase):
    """The shape-A rule as a table over `still_defines`. True needs a def,
    async def or class statement directly in the module body, no `del` of
    the name in module scope after the last such statement (an
    `except ... as NAME` counts as one), and no `global`/`nonlocal` of the
    name anywhere. A `del` inside a def, or inside a class body without
    `global`, touches that scope's own name and does not count."""

    ROWS = (("def X():\n    pass\n", True),
            ("async def X():\n    pass\n", True),
            ("class X:\n    pass\n", True),
            ("@dec\ndef X():\n    pass\n", True),
            ("X = 1\n\n\ndef X():\n    pass\n", True),
            ("def X():\n    pass\n\n\nX = 2\n", True),
            ("def X():\n    pass\n\n\ndel X\n\n\ndef X():\n    pass\n", True),
            ("def X():\n    pass\n\n\ndef f():\n    del X\n", True),
            ("def X():\n    pass\n\n\nclass C:\n    X = 2\n    del X\n",
             True),
            ("def X():\n    pass\n\n\ndel X.attr, d[X]\n", True),
            ("X = 1\n", False),
            ("X, Y = 1, 2\n", False),
            ("X: int = 1\n", False),
            ("X: object\n", False),
            ("import X\n", False),
            ("import os as X\n", False),
            ("from os import X\n", False),
            ("from os import *\n", False),
            ("for X in ():\n    pass\n", False),
            ("for X in (1,):\n    pass\n", False),
            ("with open('f') as X:\n    pass\n", False),
            ("(X := 1)\n", False),
            ("type X = int\n", False),
            ("if c:\n    def X():\n        pass\n", False),
            ("try:\n    class X:\n        pass\nexcept E:\n    pass\n", False),
            ("def X():\n    pass\n\n\ndel X\n", False),
            ("def X():\n    pass\n\n\ndel Y, X\n", False),
            ("def X():\n    pass\n\n\nif c:\n    del X\n", False),
            ("def X():\n    pass\n\n\ntry:\n    pass\nexcept E as X:\n"
             "    pass\n", False),
            ("def X():\n    pass\n\n\nclass C:\n    global X\n    del X\n",
             False),
            ("def X():\n    pass\n\n\ndef f():\n    global X\n", False),
            ("global X\n\n\ndef X():\n    pass\n", False),
            ("def X():\n    pass\n\n\ndef f():\n    X = 1\n\n"
             "    def g():\n        nonlocal X\n", False),
            ("def X():\n    pass\n\n\nX = [\n", False))

    def test_each_statement_shape(self):  # noqa: VACUOUS_ASSERTION — assertIs against a table holding rows of both polarities; every True row is an unconditional positive control on the same predicate
        for src, defined in self.ROWS:
            with self.subTest(src):
                self.assertIs(retired_name_rung.still_defines(
                    retired_name_rung._Source(src), "X"), defined)


class AMergeIsJudgedAgainstItsFirstParentTest(_CellFixture):
    """A MERGE IS JUDGED AS EVERY COMMIT IS: its staged diff against HEAD,
    with no parent able to clear a name the result no longer defines. Each
    refusal here is a merge that a parent-clearance rule admitted with the
    staged tree broken (task/3025)."""

    LANE_USES = ("from helm import cell\n\n\n"
                 "def test_lane():\n    return cell.roster_path()\n")
    MOVED = ("from helm import cell\n\n\n"
             "def test_roster():\n    return cell.profile_name()\n")
    #: The consumer line kept byte for byte, under a local `cell` that
    #: shadows the module, so the same text is not the same reference.
    SHADOWED = ("import types\n\nfrom helm import cell  # noqa: F401\n\n\n"
                "def test_roster():\n"
                "    cell = types.SimpleNamespace(roster_path=str)\n"
                "    return cell.roster_path()\n")

    def trunk_retires_and_moves_the_consumer(self):
        """Trunk retires roster_path cleanly: its one consumer moves too."""
        self.assertEqual(self.git("checkout", "-q", "trunk").returncode, 0)
        self.stage("helm/cell.py", self.without_roster_path("trunk"))
        self.stage("tests/test_cell.py", self.MOVED)
        self.commit("trunk retires cell.roster_path and moves its consumer")
        self.assertEqual(self.git("checkout", "-q", "main").returncode, 0)

    def lane_adds_a_consumer(self):
        self.stage("tests/test_lane.py", self.LANE_USES)
        self.commit("lane adds a consumer of cell.roster_path")

    def merge_trunk_into_a_lane_commit(self):
        """Give the lane a commit of its own, then `git merge --no-commit
        --no-ff trunk`, which must stop with the merge in progress."""
        self.stage("helm/lane.py", "def lane():\n    return 1\n")
        self.commit("lane adds a module")
        r = self.git("merge", "--no-commit", "--no-ff", "trunk")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def assertRefusesRosterPath(self, r, where):
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("cell.roster_path (retired from helm/cell.py)", r.stderr)
        self.assertIn(where, r.stderr)

    def test_F1_a_parent_that_never_had_the_name_clears_nothing(self):  # noqa: VACUOUS_ASSERTION — asserts rc 1, the exact refusal and the consumer line
        """The review's reproduction: HEAD defines roster_path and holds a
        live consumer, the second parent has neither, and the resolution
        drops the def while keeping the consumer."""
        self.trunk_retires_and_moves_the_consumer()
        self.lane_adds_a_consumer()
        self.lane_moves_mode(self.root)
        self.conflicted_merge(self.root, self.without_roster_path("merged"))
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("cell.roster_path (retired from helm/cell.py)", r.stderr)
        self.assertIn("tests/test_lane.py:5:", r.stderr)

    def test_a_parent_that_defines_it_only_CONDITIONALLY_clears_nothing(self):  # noqa: VACUOUS_ASSERTION — assertRefusesRosterPath asserts rc 1, the exact refusal and the consumer line
        """Trunk moved `roster_path` under `if True:` and still spells
        `cell.roster_path()`; the resolution drops the definition and keeps
        the consumer. Trunk never lacked the name, so it clears nothing."""
        self.assertEqual(self.git("checkout", "-q", "trunk").returncode, 0)
        self.stage("helm/cell.py", self.without_roster_path("base")
                   + "\n\nif True:\n    def roster_path():\n"
                   "        return 'roster.toml'\n")
        self.commit("trunk defines roster_path under a condition")
        self.assertEqual(self.git("checkout", "-q", "main").returncode, 0)
        self.merge_trunk_into_a_lane_commit()
        self.write(self.root, "helm/cell.py", self.without_roster_path("base"))
        self.assertRefusesRosterPath(self.rung(), "tests/test_cell.py:5:")

    def test_a_parent_whose_identical_line_is_SHADOWED_clears_nothing(self):  # noqa: VACUOUS_ASSERTION — assertRefusesRosterPath asserts rc 1, the exact refusal and the consumer line
        """Trunk retired `roster_path` and kept `return cell.roster_path()`
        under a local `cell`; the resolution keeps that line and drops the
        shadow, so the line now reaches into the module."""
        self.assertEqual(self.git("checkout", "-q", "trunk").returncode, 0)
        self.stage("helm/cell.py", self.without_roster_path("base"))
        self.stage("tests/test_cell.py", self.SHADOWED)
        self.commit("trunk retires roster_path behind a local shadow")
        self.assertEqual(self.git("checkout", "-q", "main").returncode, 0)
        self.merge_trunk_into_a_lane_commit()
        self.write(self.root, "tests/test_cell.py", self.USES)
        self.assertRefusesRosterPath(self.rung(), "tests/test_cell.py:5:")

    def test_both_parents_lack_it_and_nothing_spells_it_passes(self):  # noqa: VACUOUS_ASSERTION — the second scan on the same merge, profile_name dropped from the resolution, must refuse naming it
        self.trunk_retires_and_moves_the_consumer()
        self.stage("helm/cell.py", self.without_roster_path("lane"))
        self.stage("tests/test_cell.py", self.MOVED)
        self.commit("lane retires cell.roster_path too")
        self.conflicted_merge(self.root, self.without_roster_path("merged"))
        r = self.rung()
        self.assertEqual(r.returncode, 0, r.stderr)
        # MUST-HIT ON THE SAME MERGE: profile_name, which both parents define
        # and MOVED spells, dropped by the resolution.
        self.write(self.root, "helm/cell.py", "MODE = 'merged'\n")
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("cell.profile_name (retired from helm/cell.py)",
                      r.stderr)
        self.assertNotIn("cell.roster_path", r.stderr)


R = retired_name_rung

# THE PER-PAIR PATH AS IT STOOD AT d7a57105d0c, kept here as the parity
# oracle for the index (task/3840) and nowhere in the rung. Each function
# walks the whole tree for ONE (module, name) pair -- the cost the index
# removes -- and its answers are the ones the index must keep. The node
# predicates (`_reach`, `_module_test`, ...) are the rung's own, so the
# oracle pins the walk and the filing, not a second copy of the grammar.


def _oracle_string_reads(tree, mod, name, to_mod):
    on_mod = R._module_test(mod, to_mod)
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and R._dotted_hit(node.value, mod,
                                                            name):
            found[node.lineno] = R.PATCH_DOTTED
            continue
        reach = R._reach(node, on_mod)
        keys = [k for k in reach[0] if R._spells(k, name, reach[1])] \
            if reach else []
        if isinstance(node, ast.Call) and R._spelling(node.func) in R._REACHERS:
            args = list(node.args) + [kw.value for kw in node.keywords]
            if any(map(on_mod, args)):
                keys.extend(a for a in args if R._spells(a, name))
        for key in keys:
            found.setdefault(key.lineno, R.PATCH_STRUCTURAL)
    return found


def _oracle_dynamic_reads(tree, mod, name, to_mod):
    on_mod = R._module_test(mod, to_mod)
    nodes = list(ast.walk(tree))
    star = any(R._globals_from(n, on_mod) for n in nodes)
    if not star and not any(R._multiple_on(n, mod, on_mod)
                            or R._computed(R._reach(n, on_mod))
                            for n in nodes):
        return set()
    at = {n.lineno for n in nodes
          if (isinstance(n, ast.keyword) and n.arg == name)
          or (isinstance(n, ast.Constant) and n.value == name)}
    if star:
        at.update(n.lineno for n in nodes
                  if isinstance(n, ast.Name) and n.id == name)
    return at


def _oracle_bindings(tree, mod):
    to_mod, to_other = {mod}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    last = a.name.rpartition(".")[2]
                    (to_mod if last == mod else to_other).add(a.asname)
                else:
                    (to_mod if a.name == mod else to_other).add(
                        a.name.partition(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                (to_mod if a.name == mod else to_other).add(a.asname or a.name)
    return to_mod, to_other


def _oracle_module_reads(tree, names, mod, name, cand, retiring):
    to_mod, to_other = _oracle_bindings(tree, mod)
    at = set(_oracle_string_reads(tree, mod, name, to_mod))
    at |= _oracle_dynamic_reads(tree, mod, name, to_mod)
    here = os.path.dirname(cand)
    at.update(line for token, owned, line in R._owner_pairs(tree)
              if owned == name and R._satellite_paths(here, token) & retiring)
    if mod not in names:
        return at
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            src = (node.module or "").rpartition(".")[2]
            if src == mod or not node.module:
                at.update(getattr(a, "lineno", node.lineno)
                          for a in node.names if a.name in (name, "*"))
        elif isinstance(node, ast.Attribute) and node.attr == name:
            recv = node.value
            if not (isinstance(recv, ast.Name) and recv.id in to_other
                    and recv.id not in to_mod):
                at.add(node.end_lineno)
    return at


class TheIndexAnswersWhatThePerPairWalkAnsweredTest(unittest.TestCase):
    """PARITY (task/3840): the per-scan index answers every (module, name)
    pair exactly as the per-pair walk above did, line for line and, for a
    string read, kind for kind.

    ONE `_Index` PER FILE SERVES EVERY PAIR, as it does in a scan, so a memo
    that carried one module's answer into another's shows here. The corpus
    is every string reach `_reach` reads, against five receivers, with
    constant and with computed keys; the import, attribute, dotted,
    patch.multiple, globals().update, keyword-helper, facade and
    own-binding shapes; and the rung's own source as a real-size file."""

    HEAD = ("import inspect\n"
            "import pkg.a as m\n"
            "from operator import attrgetter\n"
            "from unittest import mock\n"
            "from unittest.mock import patch\n"
            "from pkg import a, c\n"
            "from pkg.c import g\n\n"
            "NAME = 'f'\n\n\n"
            "def run(self, q, k):\n"
            "    return (\n")
    REACHES = ("getattr(@, %s)", "hasattr(@, %s)", "setattr(@, %s, 1)",
               "delattr(@, %s)", "inspect.getattr_static(@, %s)",
               "@.__getattribute__(%s)", "object.__getattribute__(@, %s)",
               "vars(@).get(%s)", "@.__dict__.get(%s)", "vars(@)[%s]",
               "@.__dict__[%s]", "attrgetter(%s)(@)",
               "attrgetter('y', %s)(@)", "mock.patch.object(@, %s, 1)",
               "mock.patch.object(target=@, attribute=%s)", "patch(@, %s)",
               "log(@, %s)", "getattr(%s, @)",
               "mock.patch.multiple(@, **{%s: 1})", "run(@, name=%s)",
               "@.x.__getattribute__(%s)")
    RECEIVERS = ("a", "c", "m", "pkg.a", "q")
    KEYS = (("'f'", "'f.x'", "'x.f'", "'g'", "'NAME'"),
            ("'f'", "'g'", "NAME", "k[0]"))
    OTHERS = (
        "from pkg.a import (\n    f,\n    g as gg,\n)\n"
        "from pkg.c import *\nfrom . import f, x\nfrom .a import NAME\n"
        "try:\n    from pkg.a import Klass\nexcept ImportError:\n"
        "    Klass = None\n"
        "import pkg.a\nimport pkg.c as a2\nfrom pkg import a as m, c as a\n"
        "\n\ndef run(r, pkg, self):\n"
        "    return (pkg.a.f, a.f, m.g, r.x, a2.NAME, c.Klass,\n"
        "            self.f, f().g, pkg.c.g)\n",
        "from unittest import mock\n\n\n"
        "@mock.patch('pkg.a.f')\n@mock.patch('helm.a.g', new=1)\n"
        "def test_it(x):\n"
        "    s = ['a.f', 'pkg.c.f', 'a.f.x', 'pkg.a', 'f', 'm.NAME',\n"
        "         'c.g', 'x.a.g']\n"
        "    return s, 'pkg.a.Klass', 'pkg.m.x'\n",
        "from unittest import mock\nfrom unittest.mock import patch\n\n"
        "from pkg import a, c\n\n\n"
        "def test_it():\n"
        "    with mock.patch.multiple(a, f=1, x=2):\n        pass\n"
        "    with mock.patch.multiple('pkg.c', g=1):\n        pass\n"
        "    with patch.multiple(target='pkg.a.Klass', NAME=1):\n"
        "        pass\n"
        "    with patch.multiple(c, **{'f': 1}):\n"
        "        return run(f=1, g=2), 'x'\n",
        "from pkg import a, c\n\n"
        "globals().update(vars(a))\nglobals().update(c.__dict__)\n\n\n"
        "def run():\n    return f(), g(), x, NAME(f=1), 'Klass'\n",
        "from unittest import mock\n\nfrom pkg import a, c  # noqa: F401\n"
        "\n\ndef patched(**patches):\n"
        "    return [mock.patch.object(a, name, value)\n"
        "            for name, value in patches.items()]\n\n\n"
        "def test_it():\n    c.g()\n"
        "    return patched(\n        f=1), patched(g=2, x=3)\n",
        "_OWNER_NAMES = (\n    ('a', ('f', 'g')),\n    ('c', ('f', 'x')),\n"
        "    ('pkg', ('NAME',)),\n    'bad',\n    ('a', 'f'),\n)\n",
        "from pkg import a\n\n\ndef f():\n    return a.keep()\n\n\n"
        "g = x = NAME = Klass = None\n",
        "import pkg.a as c\nfrom pkg import c as a\n\n\n"
        "def run():\n    return a.f, c.f, getattr(a, 'g'), getattr(c, 'g')\n",
        "def run(m):\n    global f\n    return m.f, m.g, vars(m)['x']\n")
    MODS = ("a", "c", "m", None)
    NAMES = ("f", "g", "NAME")
    #: A facade entry names a satellite beside the file, so the retiring
    #: set only changes an answer for the hand-written files.
    RETIRING = ({"pkg/a.py"}, {"pkg/c.py", "pkg/pkg/__init__.py"})

    def corpus(self):
        """[(label, source, retiring sets)] -- every generated and
        hand-written file."""
        out = []
        for reach in self.REACHES:
            for recv in self.RECEIVERS:
                for keys in self.KEYS:
                    body = "".join("        %s,\n"
                                   % (reach.replace("@", recv) % key)
                                   for key in keys)
                    out.append(("%s on %s %s" % (reach, recv, keys),
                                self.HEAD + body + "    )\n",
                                self.RETIRING[:1]))
        out.extend(("other %d" % i, src, self.RETIRING)
                   for i, src in enumerate(self.OTHERS))
        return out

    def compare(self, label, src, mods, names, retirings):
        """([mismatch], hits, misses) for every pair asked of one file."""
        tree = ast.parse(src)
        ix = R._Index(tree)
        tokens = R._tokens(src)[0]
        bad, hits, misses = [], 0, 0
        for mod in mods:
            to_mod = _oracle_bindings(tree, mod)[0]
            for name in names:
                pair = (label, mod, name)
                want = _oracle_string_reads(tree, mod, name, to_mod)
                got = R._string_reads(ix, mod, name, to_mod)
                if got != want:
                    bad.append(("string", pair, want, got))
                for retiring in retirings:
                    want = _oracle_module_reads(tree, tokens, mod, name,
                                                "pkg/b.py", retiring)
                    got = R._module_reads(ix, tokens, mod, name,
                                          "pkg/b.py", retiring)
                    if got != want:
                        bad.append(("module", pair, sorted(retiring),
                                    sorted(want), sorted(got)))
                    hits, misses = hits + bool(want), misses + (not want)
        return bad, hits, misses

    def test_every_pair_of_the_corpus_answers_as_the_walk_did(self):  # noqa: VACUOUS_ASSERTION — the empty mismatch list is paired with unconditional must-hits on both poles: over 200 pairs answer non-empty and over 200 answer empty
        bad, hits, misses = [], 0, 0
        for label, src, retirings in self.corpus():
            b, h, m = self.compare(label, src, self.MODS, self.NAMES,
                                   retirings)
            bad, hits, misses = bad + b, hits + h, misses + m
        self.assertEqual([], bad[:10], "%d mismatches" % len(bad))
        # MUST-HIT, BOTH POLES: a corpus of all-empty answers would agree
        # with any index, and so would one of all-full answers.
        self.assertGreater(hits, 200, (hits, misses))
        self.assertGreater(misses, 200, (hits, misses))

    def test_the_rungs_own_source_answers_as_the_walk_did(self):
        with open(RUNG, encoding="utf-8") as f:
            src = f.read()
        bad, hits, misses = self.compare(
            "retired_name_rung.py", src, ("ast", "os", None),
            ("parse", "walk", "path", "_reach"), ({"helm/ast.py"},))
        self.assertEqual([], bad[:10], "%d mismatches" % len(bad))
        # MUST-HIT: ast.parse, ast.walk and os.path are read in it, and
        # nothing of the None module is.
        self.assertGreaterEqual(hits, 3, (hits, misses))
        self.assertGreaterEqual(misses, 4, (hits, misses))


class EachFileIsReadOncePerScanTest(RungBase):
    """THE COST ARM (task/3840): one scan shows, parses and walks each file
    at most ONCE, however many (module, name) pairs ask about it.

    py-spy measured `retired_name_rung.py --staged` at 6+ minutes and 55%
    CPU on one 9-file commit, the stack ending in `_string_reads` ->
    `_reach`: the scan walked every candidate's whole tree several times
    for EACH retired name, so its cost was names x files x nodes. Counting
    calls cannot flake the way a timing can. The fixture retires five names
    from pkg/a.py and one from pkg/c.py, and every consumer is a candidate
    for most of the six; the recorded verdict is asserted first, so a scan
    that read less cannot pass by answering less."""

    B = ("from unittest import mock\n\n"
         "from pkg import a, c\n\n\n"
         "def run():\n"
         "    return a.f(), a.g(), a.h(), a.K, a.Klass, c.f(), c.g()\n\n\n"
         "def test_it():\n"
         "    with mock.patch.object(a, \"h\", 1):\n"
         "        return getattr(c, \"g\")\n")
    D = ("from pkg.a import (\n    K,\n    Klass,\n    f,\n)\n"
         "from pkg.c import g\n\n\n"
         "def test_d():\n    return K, Klass, f(), g()\n")
    E = ("from pkg import a\n\n\n"
         "def f():\n    return a.keep()\n\n\n"
         "g = h = K = Klass = None\n")
    KEPT_A = "def keep():\n    return 4\n"
    KEPT_C = "def f():\n    return 5\n"
    FILES = (("pkg/__init__.py", ""),
             ("pkg/a.py", "K = 1\n\n\ndef f():\n    return K\n\n\n"
                          "def g():\n    return 2\n\n\ndef h():\n"
                          "    return 3\n\n\nclass Klass:\n    pass\n\n\n"
                          + KEPT_A),
             ("pkg/c.py", KEPT_C + "\n\ndef g():\n    return 6\n"),
             ("pkg/b.py", B), ("tests/test_d.py", D), ("pkg/e.py", E))
    B7 = "    return a.f(), a.g(), a.h(), a.K, a.Klass, c.f(), c.g()"
    VERDICT = {
        ("pkg/a.py", "K"): [("pkg/b.py", 7, B7),
                            ("tests/test_d.py", 2, "    K,")],
        ("pkg/a.py", "f"): [("pkg/b.py", 7, B7),
                            ("tests/test_d.py", 4, "    f,")],
        ("pkg/a.py", "g"): [("pkg/b.py", 7, B7)],
        ("pkg/a.py", "h"): [("pkg/b.py", 7, B7),
                            ("pkg/b.py", 11,
                             "    with mock.patch.object(a, \"h\", 1):")],
        ("pkg/a.py", "Klass"): [("pkg/b.py", 7, B7),
                                ("tests/test_d.py", 3, "    Klass,")],
        ("pkg/c.py", "g"): [("pkg/b.py", 7, B7),
                            ("pkg/b.py", 12,
                             "        return getattr(c, \"g\")"),
                            ("tests/test_d.py", 6, "from pkg.c import g")]}

    def setUp(self):
        super().setUp()
        for rel, text in self.FILES:
            self.stage(rel, text)
        self.assertEqual(self.git("commit", "-qm", "pkg").returncode, 0)
        self.stage("pkg/a.py", self.KEPT_A)
        self.stage("pkg/c.py", self.KEPT_C)
        self.staged = dict(self.FILES, **{"pkg/a.py": self.KEPT_A,
                                          "pkg/c.py": self.KEPT_C})

    def test_the_scan_answers_the_recorded_verdict(self):  # noqa: VACUOUS_ASSERTION — asserts the exact non-empty verdict dict: six refused names and their path:line reads
        found, err = retired_name_rung.scan_staged(self.root)
        self.assertIsNone(err)
        self.assertEqual(self.VERDICT, found)

    def test_each_file_is_shown_parsed_and_walked_once(self):  # noqa: VACUOUS_ASSERTION — the exact non-empty verdict and the six pairs asked are asserted unconditionally before the bounds, and each consumer must appear in the parse count
        path_of = {text: rel for rel, text in self.staged.items()}
        real_parse, real_walk = ast.parse, ast.walk
        tree_of, parsed, walked, shown, asked = {}, [], [], [], []

        def parse(text, *args, **kwargs):
            # A LIST, NOT A DICT BY id(): a tree the scan drops frees its id
            # for the next parse, and counting by id would lose parses.
            tree = real_parse(text, *args, **kwargs)
            parsed.append(path_of.get(text, "?"))
            tree_of[id(tree)] = parsed[-1]
            return tree

        def walk(node):
            walked.append(tree_of.get(id(node), "a subtree"))
            return real_walk(node)

        def show(root, path):
            shown.append(path)
            return real_show(root, path)

        def git(root, *args):
            if args[0] == "grep":
                asked.append(args[-3])
            return real_git(root, *args)

        real_show = retired_name_rung._index_text
        real_git = retired_name_rung._git
        with mock.patch.object(ast, "parse", side_effect=parse), \
                mock.patch.object(ast, "walk", side_effect=walk), \
                mock.patch.object(retired_name_rung, "_index_text",
                                  side_effect=show), \
                mock.patch.object(retired_name_rung, "_git",
                                  side_effect=git):
            found, err = retired_name_rung.scan_staged(self.root)
        self.assertIsNone(err)
        self.assertEqual(self.VERDICT, found)
        counts = {"pairs asked": len(asked),
                  "parses": collections.Counter(parsed),
                  "walks": collections.Counter(walked),
                  "shows": collections.Counter(shown)}
        self.assertEqual(6, len(asked), counts)
        # MUST-HIT: every consumer was read at all, so the bound below is
        # about reading ONCE and not about reading nothing.
        for rel in ("pkg/b.py", "tests/test_d.py", "pkg/e.py"):
            self.assertIn(rel, counts["parses"], counts)
        for what in ("parses", "walks", "shows"):
            self.assertEqual({}, {rel: n for rel, n in counts[what].items()
                                  if n > 1}, counts)


def _oracle_names(text):
    """The rung's tokenizer at d7a57105d0c: every NAME token, raw and
    NFKC-normalised, or None for a source that does not tokenize."""
    names = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.NAME:
                names.add(tok.string)
                names.add(unicodedata.normalize("NFKC", tok.string))
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return None
    return names


def _oracle_reads(text, cand, mod, name, retiring):
    """One candidate's answer as `consumers` gave it at d7a57105d0c: the
    file tokenized, then ALWAYS parsed and asked the per-pair walk."""
    names = _oracle_names(text)
    every = [(cand, n, t) for n, t in R._lines_with(text, name)]
    if names is None:
        return every
    if cand in retiring:
        return every if name in names else []
    try:
        tree = ast.parse(text)
    except R._UNPARSED:
        return every if name in names and mod in names else []
    lines = R._source_lines(text)
    return [(cand, n, lines[n - 1].rstrip()) for n in sorted(
        _oracle_module_reads(tree, names, mod, name, cand, retiring))]


class TheNarrowingAnswersAsTheParseDidTest(unittest.TestCase):
    """PARITY FOR THE NARROWING (task/3840, round 2): a candidate that holds
    no spelling of the module is answered from its tokens and never parsed
    (`_may_read`), and every answer is the one the always-parse path gave.

    A TEXT GREP FOR THE MODULE COULD NOT PASS THIS. ADVERSARIAL holds the
    files it would drop while the parse refuses them: a module spelled
    across an implicit concatenation, through an escape, across a line
    continuation, inside an f-string, in fullwidth latin; a file that does
    not tokenize; and a facade token naming the old path of a rename."""

    ADVERSARIAL = (
        'from unittest import mock\n\n\n@mock.patch("pkg.m" "od.NAME")\n'
        'def test_it(m):\n    pass\n',
        'from unittest import mock\n\n\n@mock.patch("pkg.\\x6dod.NAME")\n'
        'def test_it(m):\n    pass\n',
        'from unittest import mock\n\n\n@mock.patch("pkg.m\\\nod.NAME")\n'
        'def test_it(m):\n    pass\n',
        'from unittest import mock\n\n\n@mock.patch(f"pkg.\\x6dod.NAME")\n'
        'def test_it(m):\n    pass\n',
        'x = f"{0}.mod.NAME"\n',
        'from unittest import mock\n\n\ndef test_it():\n'
        '    with mock.patch.multiple("pkg.m" "od", NAME=1):\n        pass\n',
        'from pkg import ｍｏｄ\n\n\n'
        'def run():\n    return ｍｏｄ.NAME\n',
        'NAME = [\n',
        'import mod\nprint(mod.NAME)\nx = = 1\n',
        '_OWNER_NAMES = (("o" "ld", ("NAME",)),)\n',
        'x = b"pkg.mod.NAME"\n',
        'def NAME():\n    return 1\n\n\nNAME()\n',
        '"""pkg.mod.NAME is gone."""\n',
        'x = r"pkg.mod.NAME\\d", rf"{x}.mod.NAME"\n')
    MODS = ("a", "c", "m", "mod", "zeta", None)
    NAMES = ("f", "g", "NAME")
    RETIRING = ({"pkg/a.py"}, {"pkg/c.py", "pkg/pkg/__init__.py"},
                {"pkg/old.py", "pkg/mod.py"})

    def test_every_candidate_answers_as_the_parse_did(self):  # noqa: VACUOUS_ASSERTION — the empty mismatch list is paired with unconditional must-hits: each of ten adversarial shapes refused, over 300 unparsed readings and over 200 non-empty answers
        parity = TheIndexAnswersWhatThePerPairWalkAnsweredTest()
        others = len(parity.OTHERS)
        files = [(label, src, retirings)
                 for label, src, retirings in parity.corpus()[:-others]]
        files += [(label, src, self.RETIRING)
                  for label, src, _r in parity.corpus()[-others:]]
        files += [("adversarial %d" % i, src, self.RETIRING)
                  for i, src in enumerate(self.ADVERSARIAL)]
        bad, hits, skipped, asked = [], collections.Counter(), 0, 0
        for label, text, retirings in files:
            for mod in self.MODS:
                src = R._Source(text)
                for name in self.NAMES:
                    for retiring in retirings:
                        for cand in ("pkg/b.py", sorted(retiring)[0]):
                            want = _oracle_reads(text, cand, mod, name,
                                                 retiring)
                            got = R._reads(src, cand, mod, name, retiring)
                            asked += 1
                            if got != want:
                                bad.append((label, mod, name, cand,
                                            sorted(retiring), want, got))
                            hits[label] += cand not in retiring and bool(
                                want)
                skipped += "index" not in vars(src)
        self.assertEqual([], bad[:10], "%d mismatches" % len(bad))
        # MUST-HIT, EACH ADVERSARIAL SHAPE THE MODULE GREP WOULD DROP: the
        # parse refuses it in a file that is not the retiring one, so the
        # narrowing had to refuse it too.
        for i in range(10):
            self.assertGreater(hits["adversarial %d" % i], 0, i)
        # AND THE NARROWING IS REAL: many (file, module) readings never
        # parsed, and many other-file answers were non-empty.
        self.assertGreater(skipped, 300, (skipped, asked))
        self.assertGreater(sum(hits.values()), 200, (hits, asked))


class AScanHoldsOneFileAtATimeTest(RungBase):
    """THE MEMORY ARM (task/3840, round 2): the scan's peak is ONE file's
    reading, not the tree's.

    Round 1 cached every candidate's text, tokens and tree for the scan's
    life. A fresh reader measured it on fab: retiring one `path` (1001
    candidates) peaked at 2293 MB against 89 MB on trunk, a retained file
    costing about 23x its size, times every seat committing at once. The
    fixture retires `a.f` read by twelve like-sized consumers; the peak of
    the scan is bounded against the peak of reading ONE of them the way the
    scan reads it. Holding all twelve is twelve times that."""

    N = 12

    def consumer(self, i):
        return "from pkg import a\n\n\n" + "".join(
            "def g%d_%d(x):\n    return [x, x + %d, {'k': (x, %d)}]\n\n\n"
            % (i, j, j, j) for j in range(300)) + \
            "def use():\n    return a.f()\n"

    def test_the_peak_is_one_file_not_the_tree(self):  # noqa: VACUOUS_ASSERTION — the exact twelve refused consumers are asserted before the bound
        self.stage("pkg/__init__.py", "")
        self.stage("pkg/a.py", "def f():\n    return 1\n\n\n"
                               "def keep():\n    return 2\n")
        for i in range(self.N):
            self.stage("pkg/c%d.py" % i, self.consumer(i))
        self.assertEqual(self.git("commit", "-qm", "pkg").returncode, 0)
        self.stage("pkg/a.py", "def keep():\n    return 2\n")
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            base = tracemalloc.get_traced_memory()[0]
            src = retired_name_rung._Source(self.consumer(0))
            self.assertIsNotNone(src.tokens)
            self.assertIsNotNone(src.index)
            self.assertTrue(src.lines)
            one = tracemalloc.get_traced_memory()[1] - base
            del src
            tracemalloc.reset_peak()
            base = tracemalloc.get_traced_memory()[0]
            found, err = retired_name_rung.scan_staged(self.root)
            peak = tracemalloc.get_traced_memory()[1] - base
        finally:
            tracemalloc.stop()
        self.assertIsNone(err)
        # MUST-HIT: every consumer was read, parsed and refused, so the
        # bound is on reading them all and not on reading none.
        hits = found[("pkg/a.py", "f")]
        self.assertEqual({"pkg/c%d.py" % i for i in range(self.N)},
                         {h[0] for h in hits})
        self.assertEqual({"    return a.f()"}, {h[2] for h in hits})
        self.assertLess(peak, 3 * one, "scan peak %d B, one file %d B"
                        % (peak, one))


class ADirectoryNameIsNotAModuleSpellingTest(RungBase):
    """ONLY A PACKAGE IS SPELLED BY ITS DIRECTORY (task/3840, round 3).

    Round 2 added the directory name of EVERY retiring path to the module's
    spellings, and `helm/x.py` then counted every file holding the string
    "helm" as one that may read it -- nearly every file in this tree. A
    fresh reader measured the parse-skip firing almost never: 1073 of 1073
    candidates parsed for a new module, 1001 for the rung's own file, 1014
    for landreq. A facade names a retiring file by its directory only when
    that file is the package itself (`pkg/__init__.py`), so only then is the
    directory a spelling. The fixture is that shape: twelve files in a
    package directory whose name is in a string in each of them, one real
    reader, and a facade naming a renamed package."""

    OTHER = ('"""A helm module; helm runs it."""\nHOME = "helm"\n\n\n'
             "def run():\n    return HOME\n")
    READER = "from helm import zzmod\n\n\ndef go():\n    return zzmod.run()\n"
    ZZMOD = "def run():\n    return 1\n\n\ndef keep():\n    return 2\n"
    KEPT = "def keep():\n    return 2\n"

    def setUp(self):
        super().setUp()
        self.files = {"helm/__init__.py": "", "helm/zzmod.py": self.ZZMOD,
                      "helm/reader.py": self.READER}
        self.files.update(("helm/c%d.py" % i, self.OTHER.replace(
            "HOME", "HOME%d" % i)) for i in range(12))
        for rel, text in self.files.items():
            self.stage(rel, text)
        self.assertEqual(self.git("commit", "-qm", "helm").returncode, 0)
        self.stage("helm/zzmod.py", self.KEPT)
        self.files["helm/zzmod.py"] = self.KEPT

    def test_a_directory_named_everywhere_sends_no_file_to_the_parse(self):  # noqa: VACUOUS_ASSERTION — the exact refusal of the one reader and the oracle's answer are asserted before the parse census
        real_parse, parsed = ast.parse, []
        path_of = {text: rel for rel, text in self.files.items()}

        def parse(text, *args, **kwargs):
            parsed.append(path_of.get(text, "?"))
            return real_parse(text, *args, **kwargs)

        with mock.patch.object(ast, "parse", side_effect=parse):
            found, err = retired_name_rung.scan_staged(self.root)
        self.assertIsNone(err)
        # THE ORACLE'S ANSWER: every candidate parsed and asked the per-pair
        # walk, in the grep's order.
        word = re.compile(r"\brun\b")
        want = [hit for rel in sorted(self.files)
                if word.search(self.files[rel])
                for hit in _oracle_reads(self.files[rel], rel, "zzmod",
                                         "run", {"helm/zzmod.py"})]
        self.assertEqual([("helm/reader.py", 5, "    return zzmod.run()")],
                         want)
        self.assertEqual({("helm/zzmod.py", "run"): want}, found)
        # The retiring file (still_defines) and the one file that spells
        # the module: the twelve that only say "helm" are answered from
        # their tokens.
        self.assertEqual({"helm/zzmod.py": 1, "helm/reader.py": 1},
                         dict(collections.Counter(parsed)))

    def test_a_renamed_package_is_still_spelled_by_its_directory(self):  # noqa: VACUOUS_ASSERTION — the oracle's exact one-line refusal is asserted first and the narrowing must equal it; the empty answer is the paired control
        """The one case the directory is a spelling: a facade beside the
        package names it by its directory, and a rename leaves that token
        on the OLD path only. The parse refuses; so must the narrowing."""
        text = '_OWNER_NAMES = (("pkg", ("NAME",)),)\n'
        retiring = {"helm/pkg/__init__.py", "helm/newpkg/__init__.py"}
        want = _oracle_reads(text, "helm/facade.py", "newpkg", "NAME",
                             retiring)
        self.assertEqual([("helm/facade.py", 1, text.rstrip())], want)
        self.assertEqual(want, retired_name_rung._reads(
            retired_name_rung._Source(text), "helm/facade.py", "newpkg",
            "NAME", retiring))
        # CONTROL: a plain module's directory is not a spelling, so the
        # same file beside a retiring helm/zzmod.py is not parsed at all.
        src = retired_name_rung._Source('X = "helm"\n' + text)
        self.assertEqual([], retired_name_rung._reads(
            src, "helm/facade.py", "zzmod", "NAME", {"helm/zzmod.py"}))
        self.assertNotIn("index", vars(src))


if __name__ == "__main__":
    unittest.main()
