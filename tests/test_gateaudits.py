#!/usr/bin/env python3
"""helm gate audits: the tree-wide audit list, printed instead of typed."""
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-gateaudits-", var="HELM_HOME")

from helm import cli, gate, gateaudits, gateslice  # noqa: E402

_TREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ask(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(["gate", "audits"] + argv)
    return rc, out.getvalue(), err.getvalue()


class TheListIsTheTreesOwn(unittest.TestCase):

    def test_every_listed_audit_is_a_file_in_this_tree(self):
        self.assertGreater(len(gateaudits.AUDITS), 20)
        self.assertEqual(gateaudits.missing(_TREE), [])

    def test_the_list_and_the_registries_page_name_the_same_enumerators(self):
        """Two lists of one fact drift unless something holds them. The page
        is read from its own heading to the paragraph after the list, so a
        name added anywhere else on the page does not satisfy this arm."""
        with open(os.path.join(_TREE, "docs", "MODULE_REGISTRIES.md"),
                  encoding="utf-8") as fh:
            page = fh.read()
        # THE ANCHOR MUST NOT CARRY THE COUNT. The heading spells the number
        # in English and was also the `page.index` argument, so the count
        # lived in FOUR places: this literal, the heading prose, the page's
        # own list, and ENUMERATORS. Curating correctly -- adding an audit
        # and updating the heading to match -- then made this arm raise
        # ValueError on a missing anchor instead of failing with a readable
        # diff, which is the drift this lane exists to end. Measured by
        # renaming the heading and watching the index throw.
        #
        # Anchored on the SENTENCE under the heading instead: it states the
        # section's purpose and carries no number, so the page and the list
        # grow together and this arm keeps reporting a real comparison.
        start = page.index("The test modules that enumerate the package")
        stop = page.index("`tests/test_wiring.py`", start)
        named = re.findall(r"`(test_[a-z_0-9]+)`", page[start:stop])
        # UNCONDITIONAL FLOOR FIRST, and it is not decoration. Deriving the
        # count from ENUMERATORS removes the old literal's second job: a
        # hardcoded 22 also asserted the set was NON-EMPTY, so an anchor that
        # matched nothing reddened. Two derived comparisons alone both pass
        # when both sides are empty, which is the tautology this file exists
        # to prevent elsewhere. The floor restores that guarantee without
        # putting the count back.
        self.assertTrue(named, "the page slice named no modules")
        self.assertTrue(gateaudits.ENUMERATORS, "the list is empty")
        self.assertEqual(sorted(named), sorted(gateaudits.ENUMERATORS))
        # The count is DERIVED, never retyped: one place for the number.
        self.assertEqual(len(named), len(gateaudits.ENUMERATORS), named)

    def test_no_audit_is_listed_twice(self):
        self.assertEqual(len(set(gateaudits.AUDITS)), len(gateaudits.AUDITS))


class TheCommandIsPasteable(unittest.TestCase):

    def test_one_line_naming_the_repo_and_every_audit(self):
        rc, out, _err = _ask(["--repo", _TREE])
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.strip().splitlines()), 1, out)
        self.assertTrue(out.startswith(
            "fab test --repo %s -- python3 -m unittest " % _TREE), out[:120])
        for name in gateaudits.AUDITS:
            self.assertIn(" tests.%s" % name, out)

    def test_the_lanes_own_modules_ride_once_each_in_any_spelling(self):
        rc, out, _err = _ask(["--repo", _TREE, "--", "tests.test_gateaudits",
                              "test_gateaudits", "tests/test_gateaudits.py",
                              "tests.test_wiring"])
        self.assertEqual(rc, 0)
        # WHOLE TOKENS: `tests.test_gateaudits_drift` is a listed audit, and a
        # substring count reads it as a second `tests.test_gateaudits`.
        self.assertEqual(out.split().count("tests.test_gateaudits"), 1, out)
        self.assertEqual(out.split().count("tests.test_wiring"), 1, out)
        self.assertTrue(out.strip().endswith("tests.test_gateaudits"), out[-80:])

    def test_json_carries_the_same_command(self):
        rc, out, _err = _ask(["--repo", _TREE, "--json"])
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertEqual(doc["command"], gateaudits.command(_TREE))
        self.assertEqual(doc["modules"], gateaudits.modules())

    def test_a_tree_missing_a_listed_audit_prints_NOTHING_and_fails(self):
        """CONTROL: the real tree, through the same door, prints the line."""
        self.assertEqual(_ask(["--repo", _TREE])[0], 0)
        bare = tempfile.mkdtemp(prefix="helm-test-gateaudits-bare-")
        self.addCleanup(shutil.rmtree, bare, ignore_errors=True)
        os.makedirs(os.path.join(bare, "tests"))
        rc, out, err = _ask(["--repo", bare])
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("names an audit the tree does not have", err)

    def test_a_stray_flag_is_refused_before_anything_prints(self):
        rc, out, err = _ask(["--bogus"])
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("unknown arg '--bogus'", err)


class TheLineRunsAsSlicesWhenTheSlicedPathIsEnabled(unittest.TestCase):
    """The printed line runs the audits through the sliced focused runner
    when the land door admits a sliced receipt, and serial otherwise; stderr
    names which and why, and --serial asks for serial by name."""

    SLICED_HEAD = ("fab test --slots fit --cores %d --repo %s -- env "
                   "HELM_GATESLICE_LEAKS=fail HELM_GATE_SUITE_CAP=1 python3 "
                   "helm/gateslice.py --modules ")

    def enabled(self):
        patch = mock.patch.object(gate, "sliced_land_refusal",
                                  return_value=None)
        patch.start()
        self.addCleanup(patch.stop)

    def test_an_enabled_path_prints_the_sliced_runner_and_says_so(self):
        self.enabled()
        rc, out, err = _ask(["--repo", _TREE])
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(out.strip().splitlines()), 1, out)
        self.assertTrue(out.startswith(self.SLICED_HEAD % (
            gate.SLICE_SLOTS * gate._CORES_PER_SUITE, _TREE)), out[:200])
        for name in gateaudits.AUDITS:
            self.assertIn(" tests.%s" % name, out)
        self.assertIn("helm gate audits: SLICED — the sliced path is enabled: "
                      "%d modules run as slices" % len(gateaudits.AUDITS), err)

    def test_serial_by_name_even_when_the_path_is_enabled(self):
        self.enabled()
        rc, out, err = _ask(["--repo", _TREE, "--serial"])
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.startswith(
            "fab test --repo %s -- python3 -m unittest " % _TREE), out[:120])
        self.assertIn("helm gate audits: SERIAL — --serial asked", err)

    def test_a_path_the_canary_does_not_stand_for_prints_serial_and_why(self):
        rc, out, err = _ask(["--repo", _TREE])
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.startswith(
            "fab test --repo %s -- python3 -m unittest " % _TREE), out[:120])
        self.assertIn("helm gate audits: SERIAL — the sliced path is not "
                      "enabled", err)
        self.assertIn(gate.sliced_land_refusal(), err)

    def test_json_names_the_mode_and_its_reason(self):
        self.enabled()
        rc, out, _err = _ask(["--repo", _TREE, "--json"])
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertEqual((doc["mode"], doc["command"]),
                         (gateaudits.SLICED, gateaudits.command(
                             _TREE, (), gateaudits.SLICED)))
        self.assertIn("sliced path is enabled", doc["mode_reason"])

    def _tree(self, runner=True, modules_scope=True):
        root = tempfile.mkdtemp(prefix="helm-test-gateaudits-tree-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        os.makedirs(os.path.join(root, "tests"))
        os.makedirs(os.path.join(root, "helm"))
        with open(os.path.join(root, "tests", "__init__.py"), "w"):
            pass
        for name in gateaudits.AUDITS:
            with open(os.path.join(root, "tests", name + ".py"), "w") as fh:
                fh.write("import unittest\nclass Audit(unittest.TestCase):\n"
                         "    def test_audit(self):\n        pass\n")
        for rel in gate.SLICE_RUNNER_FILES if runner else ():
            shutil.copy(os.path.join(_TREE, *rel.split("/")),
                        os.path.join(root, *rel.split("/")))
        if runner and not modules_scope:
            path = os.path.join(root, *gate.SLICE_RUNNER.split("/"))
            with open(path) as fh:
                text = fh.read()
            with open(path, "w") as fh:
                fh.write(text.replace('MODULES_FLAG = "--modules"',
                                      'MODULES_FLAG = None'))
        return root

    def test_a_tree_that_cannot_run_the_list_as_slices_prints_serial(self):
        self.enabled()
        for kw, needle in (({"runner": False},
                            "does not ship the slice runner"),
                           ({"modules_scope": False},
                            "predates its --modules scope")):
            with self.subTest(**kw):
                root = self._tree(**kw)
                rc, out, err = _ask(["--repo", root])
                self.assertEqual(rc, 0, err)
                self.assertIn("-- python3 -m unittest ", out)
                self.assertIn(needle, err)

    def test_the_printed_sliced_line_runs_every_audit(self):
        """What is pasted is what runs: the argv after `--` in the printed
        line, executed in a tree that ships the runner, runs every listed
        module as slices and exits 0."""
        self.enabled()
        root = self._tree()
        rc, out, err = _ask(["--repo", root])
        self.assertEqual(rc, 0, err)
        argv = shlex.split(out.strip().split(" -- ", 1)[1])
        self.assertEqual(argv[0], "env")
        env = dict(os.environ)
        for key in ("HELM_GATESLICE_EVIDENCE", "HELM_GATESLICE_KEEP",
                    "HELM_GATESLICE_TIMINGS", "HELM_GATESLICE_WORKERS"):
            env.pop(key, None)
        argv = [sys.executable if a == "python3" else a for a in argv]
        proc = subprocess.run(argv, cwd=root, env=env, capture_output=True,
                              text=True, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn(gateslice.DIAGNOSTIC_MARKER, proc.stderr)
        self.assertIn("Ran %d tests" % len(gateaudits.AUDITS), proc.stderr)
        self.assertNotIn("gateslice leak", proc.stderr)


#: The checks a lane-on-lane composition made relevant through 09-25 while
#: the standing list did not guarantee them. Named here, not read from
#: `gateaudits`, so a list that loses one reddens instead of agreeing with
#: itself. The receipts and cure commits that name each are in
#: helm/gateaudits.py beside `COMPOSITION_CHECKS`.
_COMPOSITION_CHECKS = ("test_world_literals", "test_assertion_hygiene",
                       "test_no_private_names", "test_delivery_truth",
                       "test_chat_reply", "test_trailer_rung",
                       "test_burnflags")

#: Modules that went red at a train gate in the same window and read only
#: their own fixtures, so no other lane's change can redden them. Listing one
#: would cost every lane its runtime and catch nothing.
_FIXTURE_ONLY = {
    "test_work": "its occupant census reads a planted process table "
                 "(cd44ca76bd4), so only the host could redden it",
    "test_stale_claim": "its census and its who rung read a fixture /proc "
                        "(0783500d336), so only the host could redden it",
}

_PASSING = ("import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_t(self):\n"
            "        self.assertTrue(True)\n")


class TheCompositionChecksRide(unittest.TestCase):

    def test_each_check_that_refused_another_lanes_change_is_listed(self):  # noqa: VACUOUS_ASSERTION — every case is asserted POSITIVELY (assertIn) over a non-empty literal tuple
        for name in _COMPOSITION_CHECKS:
            self.assertIn(name, gateaudits.AUDITS,
                          "a check that went red only at a train gate is "
                          "missing from the lane's audits, so the next lane "
                          "that breaks it goes green alone")

    def test_a_module_that_reads_only_its_own_fixtures_is_not_listed(self):
        # POSITIVE CONTROL on the same list: a module that also went red at a
        # train gate in the window, and does read the tree, is listed.
        self.assertIn("test_scratch", gateaudits.AUDITS)
        for name, why in sorted(_FIXTURE_ONLY.items()):
            self.assertTrue(os.path.isfile(
                os.path.join(_TREE, "tests", name + ".py")), name)
            self.assertNotIn(name, gateaudits.AUDITS, why)


class ALaneClashIsRedInTheLanesOwnAudits(unittest.TestCase):
    """A lane that ships an address at a registrable domain (the clash that
    reddened train170 and train257) must go red in the command `helm gate
    audits` prints for that lane, not first at the train. The lane tree
    carries a passing stub for every listed audit and the REAL
    test_world_literals, and the printed module list runs in it."""

    def _lane(self, planted):
        root = tempfile.mkdtemp(prefix="helm-test-gateaudits-lane-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        files = {"tests/__init__.py": "", "helm/lane.py": "X = 1\n"}
        files.update(("tests/%s.py" % n, _PASSING) for n in gateaudits.AUDITS)
        with open(os.path.join(_TREE, "tests", "test_world_literals.py"),
                  encoding="utf-8") as fh:
            files["tests/test_world_literals.py"] = fh.read()
        if planted:
            files["helm/lane.py"] += "OWNER = 'lane@registrable-name.org'\n"
        for rel, text in files.items():
            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        for argv in (("init", "-q"), ("add", "-A")):
            subprocess.run(("git",) + argv, cwd=root, check=True,
                           capture_output=True)
        return root

    def _audit_run(self, root):
        """The printed module list, run in `root` -> (modules, child)."""
        rc, out, err = _ask(["--repo", root])
        self.assertEqual(rc, 0, err)
        modules = out.split(" -m unittest ", 1)[1].split()
        # `-m` puts `root` first on the child's path, so `tests` is the lane's.
        child = subprocess.run((sys.executable, "-m", "unittest") +
                               tuple(modules), cwd=root, text=True,
                               capture_output=True)
        return modules, child

    def test_a_lane_that_ships_a_registrable_address_is_red(self):
        modules, child = self._audit_run(self._lane(planted=True))
        self.assertIn("tests.test_world_literals", modules,
                      "the lane's audit command does not run the check that "
                      "refused this clash at the train")
        self.assertNotEqual(child.returncode, 0, child.stderr[-2000:])
        self.assertIn("lane@registrable-name.org", child.stderr)

    def test_the_same_lane_without_the_address_is_green(self):
        """CONTROL: the stubs and the real check pass on the unplanted tree,
        so the red above is the address and not the fixture."""
        _modules, child = self._audit_run(self._lane(planted=False))
        self.assertEqual(child.returncode, 0, child.stderr[-2000:])
        self.assertIn("OK", child.stderr)

    def test_the_red_comes_from_the_list_carrying_the_check(self):
        """CONTROL on the condition: with the check off the list, the same
        planted lane runs green, which is how every lane went green alone."""
        without = tuple(n for n in gateaudits.AUDITS
                        if n != "test_world_literals")
        with mock.patch.object(gateaudits, "AUDITS", without):
            modules, child = self._audit_run(self._lane(planted=True))
        self.assertIn("tests.test_wiring", modules)     # the list still ran
        self.assertNotIn("tests.test_world_literals", modules)
        self.assertEqual(child.returncode, 0, child.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
