#!/usr/bin/env python3
"""`helm gate audits --wide`: the lane census, read from the tip's tree.

The audits import nothing they judge. A lane's own change is judged by the
modules that DO reach it, and those were chosen by hand before every gate.
`gateaudits.lane_census` reads them from the tip's committed tree, one reason
each, and the CLI prints what it answers. Every arm builds a fixture history
whose base and tip differ by a known change and reads the list the census
gives, so a missing rule shows as a missing module, never as a wrong count.

The two real-history arms read the lanes whose reds the census exists for:
train450's test_resumeturn names helm/harness.py, and train460's
test_seat_lineage_board imports test_web_lr inside a function.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-gateaudits-census-", var="HELM_HOME")

from helm import autoland, cli, gateaudits  # noqa: E402

_TREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PASSING = ("import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_t(self):\n"
            "        self.assertTrue(True)\n")


def _git(cwd, *args):
    proc = subprocess.run(("git",) + args, cwd=cwd, capture_output=True,
                          text=True)
    if proc.returncode != 0:
        raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def _ask(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(["gate", "audits"] + argv)
    return rc, out.getvalue(), err.getvalue()


#: The base tree: a passing stub for every audit and pre-gate audit, and one
#: module per rule, each a stand-in for the module the rule was built for.
BASE = {
    "tests/__init__.py": "",
    "helm/__init__.py": "",
    "helm/harness.py": "X = 1\n",
    "helm/quiet.py": "Q = 1\n",
    "docs/NOTES.md": "notes\n",
    "tests/test_harness.py": _PASSING,
    "tests/test_harness_split.py": _PASSING,
    "tests/test_resumeturn.py": _PASSING + (
        "\nPATHS = (\"helm/orcaadopt.py\", \"helm/harness.py\")\n"),
    "tests/test_web_lr.py": _PASSING + (
        "\nclass CardRuntimeBase(object):\n    pass\n"),
    "tests/test_seat_lineage_board.py": _PASSING + (
        "\n\ndef card():\n"
        "    from tests.test_web_lr import CardRuntimeBase\n"
        "    return CardRuntimeBase\n"),
    "tests/test_lineage_chain.py": (
        "from tests import (test_seat_lineage_board as board,  # noqa\n"
        "                   test_resumeturn)  # noqa\n") + _PASSING,
    "tests/test_bystander.py": _PASSING + (
        "\n# test_web_lr and test_seat_lineage_board, named in prose only\n"),
    "tests/test_quiet.py": _PASSING,
}
BASE.update(("tests/%s.py" % n, _PASSING) for n in
             gateaudits.AUDITS + autoland.PRE_GATE_AUDITS)

#: The change: a helm module and a test module, the shape of a lane.
CHANGE = {"helm/harness.py": "X = 2\n",
          "tests/test_web_lr.py": BASE["tests/test_web_lr.py"] + "# more\n"}


class _Repo(unittest.TestCase):

    def repo(self, change=CHANGE, base=BASE):
        """A repository whose `base` commit is BASE and whose HEAD adds
        `change` -> (root, base sha, tip sha)."""
        root = os.path.realpath(tempfile.mkdtemp(prefix="helm-census-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "user.name", "t")
        _git(root, "config", "user.email", "t@example.invalid")
        self.write(root, base)
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "base")
        trunk = _git(root, "rev-parse", "HEAD")
        self.write(root, change)
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "--allow-empty", "-m", "lane")
        return root, trunk, _git(root, "rev-parse", "HEAD")

    def write(self, root, files):
        for rel, text in files.items():
            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)

    def census(self, root, trunk, tip="HEAD"):
        census, why = gateaudits.lane_census(root, trunk, tip)
        self.assertIsNone(why, why)
        return census


class EachRuleSelectsItsModuleWithItsReason(_Repo):

    def test_every_rule_names_the_module_it_selected_and_why(self):
        root, trunk, tip = self.repo()
        census = self.census(root, trunk)
        self.assertEqual((census["base"], census["tip"]), (trunk, tip))
        self.assertEqual(census["touched"],
                         ["helm/harness.py", "tests/test_web_lr.py"])
        want = {
            "tests.test_web_lr": "touched tests/test_web_lr.py",
            "tests.test_harness": "stem of helm/harness.py",
            "tests.test_harness_split": "stem of helm/harness.py",
            "tests.test_seat_lineage_board": "imports tests.test_web_lr",
            "tests.test_lineage_chain":
                "imports tests.test_seat_lineage_board",
            "tests.test_resumeturn": "names helm/harness.py",
        }
        self.assertEqual({m: census["reasons"][m] for m in census["extra"]},
                         want)
        self.assertEqual(census["extra"], sorted(want))
        # CONTROL on the same list: a module that names the touched test
        # module only in prose, and one nothing reaches, are not selected.
        for name in ("tests.test_bystander", "tests.test_quiet"):
            self.assertNotIn(name, census["modules"])

    def test_the_audits_and_the_pre_gate_audits_lead_every_list(self):
        root, trunk, _tip = self.repo()
        census = self.census(root, trunk)
        lead = gateaudits.modules(autoland.PRE_GATE_AUDITS)
        self.assertEqual(census["modules"], lead + census["extra"])
        self.assertTrue(census["extra"], "the fixture change selects none")
        for name in lead:
            self.assertEqual(census["reasons"][name],
                             gateaudits.AUDIT if name[6:] in gateaudits.AUDITS
                             else gateaudits.PRE_GATE, name)
        self.assertIn("tests.test_stop_seam", lead)
        self.assertEqual(census["reasons"]["tests.test_stop_seam"],
                         gateaudits.PRE_GATE)

    def test_lane_modules_answers_the_same_list(self):
        root, trunk, _tip = self.repo()
        census = self.census(root, trunk)
        self.assertEqual(gateaudits.lane_modules(root, trunk, "HEAD"),
                         (census["modules"], census["reasons"], None))

    def test_any_file_under_a_helm_package_directory_selects_its_stem(self):
        """STEM OF, FOR A PACKAGE DIRECTORY: a changed file under
        helm/<stem>/ selects tests/test_<stem>*.py whatever its suffix, as
        the documented rule says. helm/web_ui/ ships no .py at all, only
        parts, and tests/test_web_ui_assembly.py is what a changed part
        reddens; nothing else in the census reaches it, since it names the
        part only below helm/web_ui/."""
        base = dict(BASE)
        base.update({
            "helm/web_ui/scripts/10-boxes.js.part": "var a = 1;\n",
            "helm/notes.txt": "n\n",
            "tests/test_web_ui_assembly.py": _PASSING + (
                "\nPARTS = (\"scripts/10-boxes.js.part\",)\n"),
            "tests/test_notes.py": _PASSING,
        })
        change = {"helm/web_ui/scripts/10-boxes.js.part": "var a = 2;\n",
                  "helm/notes.txt": "m\n"}
        root, trunk, _tip = self.repo(change=change, base=base)
        census = self.census(root, trunk)
        self.assertEqual(census["reasons"].get("tests.test_web_ui_assembly"),
                         "stem of helm/web_ui/scripts/10-boxes.js.part")
        # CONTROL: a top-level helm file that is not a module names no stem.
        self.assertIn("helm/notes.txt", census["touched"])
        self.assertNotIn("tests.test_notes", census["modules"])

    def test_a_module_several_rules_select_carries_the_first_reason(self):
        """touched, then stem, then imports, then names: test_harness is the
        stem module of helm/harness.py AND names it, and test_web_lr is
        touched AND is a stem module of nothing."""
        base = dict(BASE)
        base["tests/test_harness.py"] = _PASSING + "P = 'helm/harness.py'\n"
        root, trunk, _tip = self.repo(base=base)
        census = self.census(root, trunk)
        self.assertEqual(census["reasons"]["tests.test_harness"],
                         "stem of helm/harness.py")
        self.assertEqual(census["reasons"]["tests.test_web_lr"],
                         "touched tests/test_web_lr.py")

    def test_an_import_a_backslash_continues_is_read_whole(self):
        """A backslash carries an import statement onto the next line in
        three places: inside the name list, before `import`, and after a
        plain `import`. Each module here names the touched test_web_lr only
        on the continued line, so reading the first line alone misses it."""
        base = dict(BASE)
        base.update({
            "tests/test_cont_names.py": _PASSING + (
                "\n\ndef load():\n"
                "    from tests import test_quiet, \\\n"
                "        test_web_lr  # noqa\n"
                "    return test_web_lr\n"),
            "tests/test_cont_from.py": _PASSING + (
                "\nfrom tests.test_web_lr \\\n"
                "    import CardRuntimeBase  # noqa\n"),
            "tests/test_cont_plain.py": _PASSING + (
                "\nimport tests.test_quiet, \\\n"
                "    tests.test_web_lr  # noqa\n"),
        })
        root, trunk, _tip = self.repo(base=base)
        census = self.census(root, trunk)
        names = ("tests.test_cont_from", "tests.test_cont_names",
                 "tests.test_cont_plain")
        self.assertEqual({m: census["reasons"].get(m) for m in names},
                         dict.fromkeys(names, "imports tests.test_web_lr"))

    def test_a_deleted_test_module_seeds_its_importers_and_is_not_run(self):
        root, trunk, _tip = self.repo(change={})
        os.remove(os.path.join(root, "tests", "test_web_lr.py"))
        _git(root, "commit", "-qam", "drop")
        census = self.census(root, trunk)
        self.assertNotIn("tests.test_web_lr", census["modules"])
        self.assertEqual(census["reasons"]["tests.test_seat_lineage_board"],
                         "imports tests.test_web_lr")

    def test_a_change_nobody_names_adds_nothing(self):
        root, trunk, _tip = self.repo(change={"docs/NOTES.md": "more\n"})
        census = self.census(root, trunk)
        self.assertEqual(census["touched"], ["docs/NOTES.md"])
        self.assertEqual(census["extra"], [])
        self.assertEqual(census["modules"],
                         gateaudits.modules(autoland.PRE_GATE_AUDITS))

    def test_the_change_is_the_tips_own_past_the_merge_base(self):
        """A base that moved on after the lane branched adds nothing of its
        own: the change is read from the merge-base."""
        root, trunk, tip = self.repo()
        _git(root, "checkout", "-q", "-b", "later", trunk)
        self.write(root, {"helm/quiet.py": "Q = 2\n"})
        _git(root, "commit", "-qam", "trunk moved")
        census = self.census(root, "later", tip)
        self.assertEqual(census["merge_base"], trunk)
        self.assertNotIn("tests.test_quiet", census["modules"])
        self.assertIn("tests.test_harness", census["modules"])


class ACensusThatCannotBeReadIsRefusedNeverShortened(_Repo):

    def test_a_module_shaped_symlink_at_the_tip_is_refused(self):
        root, trunk, _tip = self.repo()
        os.symlink("test_web_lr.py", os.path.join(root, "tests",
                                                  "test_alias.py"))
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "alias")
        census, why = gateaudits.lane_census(root, trunk, "HEAD")
        self.assertIsNone(census)
        self.assertIn("tests/test_alias.py is a symlink", why)
        self.assertEqual(gateaudits.lane_modules(root, trunk, "HEAD")[:2],
                         (None, None))

    def test_a_tip_or_base_that_does_not_resolve_is_refused(self):
        root, trunk, _tip = self.repo()
        # CONTROL: the same repository, base and tip resolve.
        self.assertIsNotNone(gateaudits.lane_census(root, trunk, "HEAD")[0])
        census, why = gateaudits.lane_census(root, trunk, "no-such-ref")
        self.assertIsNone(census)
        self.assertIn("the tip no-such-ref is not a commit", why)
        census, why = gateaudits.lane_census(root, "no-such-ref", "HEAD")
        self.assertIsNone(census)
        self.assertIn("the base no-such-ref is not a commit", why)


class TheWideLineIsTheCensus(_Repo):

    def test_json_carries_base_tip_modules_and_reasons(self):
        root, trunk, tip = self.repo()
        rc, out, err = _ask(["--repo", root, "--wide", "--base", trunk,
                             "--tip", tip, "--json"])
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        census = self.census(root, trunk, tip)
        self.assertEqual((doc["base"], doc["tip"], doc["merge_base"]),
                         (trunk, tip, trunk))
        self.assertEqual(doc["modules"], census["modules"])
        self.assertEqual(doc["reasons"], census["reasons"])
        self.assertEqual(doc["reasons"]["tests.test_resumeturn"],
                         "names helm/harness.py")
        self.assertEqual(doc["command"], gateaudits.command(
            root, census["modules"], doc["mode"]))

    def test_the_line_names_every_module_and_stderr_counts_them(self):
        root, trunk, _tip = self.repo()
        rc, out, err = _ask(["--repo", root, "--wide", "--base", trunk])
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(out.strip().splitlines()), 1, out)
        census = self.census(root, trunk)
        self.assertEqual(out.split(" -m unittest ", 1)[1].split(),
                         census["modules"])
        self.assertIn("helm gate audits: SERIAL — ", err)
        self.assertIn("census: %d modules from 2 touched paths"
                      % len(census["extra"]), err)

    def test_the_tip_defaults_to_head_and_named_modules_ride_after(self):
        root, trunk, _tip = self.repo()
        rc, out, err = _ask(["--repo", root, "--wide", "--base", trunk,
                             "--json", "--", "test_quiet",
                             "tests.test_harness"])
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        census = self.census(root, trunk)
        self.assertIn("tests.test_harness", census["modules"])
        self.assertEqual(doc["modules"],
                         census["modules"] + ["tests.test_quiet"])
        self.assertEqual(doc["modules"].count("tests.test_harness"), 1)

    def test_a_census_that_cannot_be_read_prints_nothing_and_fails(self):
        root, trunk, _tip = self.repo()
        rc, out, err = _ask(["--repo", root, "--wide", "--base", trunk,
                             "--tip", "no-such-ref"])
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("the lane census cannot be read", err)

    def test_base_or_tip_without_wide_is_refused(self):
        rc, out, err = _ask(["--repo", _TREE, "--base", "HEAD"])
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("nothing reads them without it", err)
        rc, out, err = _ask(["--repo", _TREE, "--tip", "HEAD"])
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("nothing reads them without it", err)
        # CONTROL: the same line with --wide is read.
        rc, out, err = _ask(["--repo", _TREE, "--wide", "--base", "HEAD",
                             "--tip", "HEAD"])
        self.assertEqual(rc, 0, err)
        self.assertIn(" -m unittest ", out)

    def test_without_wide_the_output_is_the_audits_line_alone(self):
        rc, out, err = _ask(["--repo", _TREE])
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, gateaudits.command(
            _TREE, (), gateaudits.mode(_TREE)[0]) + "\n")
        self.assertEqual(err.count("\n"), 1, err)
        self.assertNotIn("census", err)
        rc, out, _err = _ask(["--repo", _TREE, "--json"])
        self.assertEqual(sorted(json.loads(out)),
                         ["command", "mode", "mode_reason", "modules",
                          "repo"])


class AFailureNamesItsModules(unittest.TestCase):

    def test_each_heading_names_its_listed_module_once_in_run_order(self):
        text = "\n".join((
            "FAIL: test_y (tests.test_resumeturn.T.test_y)",
            "ERROR: tests.test_gone (unittest.loader._FailedTest.tests."
            "test_gone)",
            "ERROR: setUpClass (tests.test_web_lr.Base)",
            "FAIL: test_z (tests.test_resumeturn.T.test_z)",
            "FAIL: test_w (tests.test_unlisted.T.test_w)",
            "  tests.test_quiet is only mentioned in a traceback line",
            "FAILED (failures=3, errors=2)"))
        names = ["tests.test_resumeturn", "tests.test_gone",
                 "tests.test_web_lr", "tests.test_quiet"]
        self.assertEqual(gateaudits.failing(text, names),
                         ["tests.test_resumeturn", "tests.test_gone",
                          "tests.test_web_lr"])

    def test_a_green_run_names_none(self):
        # CONTROL: the same module, red, is named.
        self.assertEqual(gateaudits.failing("FAIL: t (tests.test_x.T.t)\n",
                                            ["tests.test_x"]),
                         ["tests.test_x"])
        self.assertEqual(gateaudits.failing("Ran 4 tests in 0.1s\n\nOK\n",
                                            ["tests.test_x"]), [])


class TheLanesTheCensusWasBuiltFor(unittest.TestCase):
    """Real history: each lane's tip against the trunk its train merged it
    onto (the train merge's first parent)."""

    def lane(self, merge):
        have = subprocess.run(("git", "cat-file", "-e", merge + "^{commit}"),
                              cwd=_TREE, capture_output=True)
        if have.returncode != 0:
            self.skipTest("this clone does not carry %s" % merge)
        rc, out, err = _ask(["--repo", _TREE, "--wide", "--base",
                             merge + "^1", "--tip", merge + "^2", "--json"])
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def test_train450_lists_test_resumeturn_for_naming_the_harness(self):
        doc = self.lane("38c958626c8")
        self.assertIn("helm/harness.py", doc["touched"])
        self.assertEqual(doc["reasons"]["tests.test_resumeturn"],
                         "names helm/harness.py")

    def test_train460_lists_the_lineage_board_for_importing_test_web_lr(self):
        doc = self.lane("9022a40cf31")
        self.assertIn("tests/test_web_lr.py", doc["touched"])
        self.assertEqual(doc["reasons"]["tests.test_seat_lineage_board"],
                         "imports tests.test_web_lr")


if __name__ == "__main__":
    unittest.main()
