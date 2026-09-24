#!/usr/bin/env python3
"""End-to-end controls for the retired-name pre-commit rung."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

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
        names, _strings = retired_name_rung._names_and_strings(
            "from pkg.mod import %s\nprint(%s)\n" % (self.WIDE, self.WIDE))
        self.assertIn("RETIRED", names,
                      "tokens are NFKC-normalised so the test asks the "
                      "question Python asks")

    def test_the_RAW_spelling_is_kept_too_so_a_grep_still_agrees(self):
        names, _strings = retired_name_rung._names_and_strings("%s = 1\n" % self.WIDE)
        self.assertIn(self.WIDE, names)

    def test_an_ASCII_token_is_unaffected_so_the_change_is_not_broad(self):
        names, _strings = retired_name_rung._names_and_strings("RETIRED = 1\n")
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


class AModuleTokenIsNotABindingTest(RungBase):
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

    def assertRefusesB(self, r):
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("a.f (retired from pkg/a.py)", r.stderr)
        self.assertIn("pkg/b.py:", r.stderr)

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


class UnparseableSourceIsNotCleanTest(RungBase):
    """A FILE THIS RUNG CANNOT READ IS NOT A FILE IT CLEARED.

    `_names_and_strings` answers empty sets for an unparseable source, and
    empty sets are indistinguishable from "holds nothing" -- so a consumer
    with a live use at the TOP and a syntax error at the BOTTOM was dropped
    in silence and the commit passed unchecked. A guard may not turn "I
    cannot read this" into "this is clean". Found by gemini."""

    def test_a_source_that_does_not_parse_answers_false(self):
        self.assertFalse(
            retired_name_rung._source_parses("x = [\n"))
        self.assertTrue(retired_name_rung._source_parses("x = 1\n"))

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


if __name__ == "__main__":
    unittest.main()
