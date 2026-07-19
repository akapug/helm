#!/usr/bin/env python3
"""doctor tests — hermetic: a synthetic HELM_HOME in a tempdir with a broken
symlink, a missing repo path, a stale memory_dir, an adoption conflict, and a
dup-prefix adopted store. Never touches the real ~/.helm, ~/.mc, ~/.claude."""
import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from helm import doctor, home, pk, whoami


def levels(results, level):
    return [msg for l, msg in results if l == level]


class DoctorBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.helm_home = os.path.join(self.tmp.name, "helm-home")
        self.envp = mock.patch.dict(os.environ, {
            "HELM_HOME": self.helm_home,
            "MC_HOME": os.path.join(self.tmp.name, "mc-home"),
        })
        self.envp.start()
        os.environ.pop("MELD_HOME", None)
        self.assertTrue(home.helm_home().startswith(self.tmp.name))

    def tearDown(self):
        self.envp.stop()
        self.tmp.cleanup()

    def seed_home(self):
        """The synthetic estate: one healthy project + one issue per check."""
        home.scaffold_global()
        repo = os.path.join(self.tmp.name, "repos", "good")
        os.makedirs(repo)
        for name in ("good", "gone-repo", "brokelink", "memstale", "mission-control"):
            if name != "brokelink":
                home.scaffold_project(name)
        os.symlink(os.path.join(self.tmp.name, "no-such-target"),
                   home.project_dir("brokelink"))
        rec = lambda name, path, mem=None: {
            "name": name, "path": path, "kind": "git", "status": "active",
            "sessions": {}, "memory_dir": mem}
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "good": rec("good", repo),
            "gone-repo": rec("gone-repo", os.path.join(self.tmp.name, "repos", "gone")),
            "brokelink": rec("brokelink", repo),
            "memstale": rec("memstale", repo, mem=os.path.join(self.tmp.name, "no-such-mem")),
            "mission-control": rec("mission-control", repo),
        }})

    def seed_adopted(self):
        d = os.path.join(self.tmp.name, "adopted")
        os.makedirs(d)
        for f in ("prem-x.md", "prior-x.md", "prior-y.md", "lex-z.md",
                  "heuristic-h.md", "random.md"):
            pk.atomic_write(os.path.join(d, f), "stub\n")
        return d


class TestChecks(DoctorBase):
    def test_home_missing_is_warn_not_fail(self):
        results = doctor.check_home()
        self.assertTrue(any("helm home missing" in m for m in levels(results, doctor.WARN)))
        self.assertEqual(levels(results, doctor.FAIL), [])

    def test_registry_garbled_is_fail(self):
        home.scaffold_global()
        pk.atomic_write(home.registry_path(), "not json{")
        results = doctor.check_home()
        self.assertTrue(any("does not parse" in m for m in levels(results, doctor.FAIL)))

    def test_registry_parses_reports_count(self):
        self.seed_home()
        results = doctor.check_home()
        self.assertTrue(any("5 projects" in m for m in levels(results, doctor.OK)))

    def test_authored_absent_is_silent(self):
        home.scaffold_global()
        self.assertEqual(doctor.check_authored(), [])

    def test_authored_garbled_is_fail(self):
        home.scaffold_global()
        pk.atomic_write(home.authored_path(), "not json{")
        results = doctor.check_authored()
        self.assertTrue(any("registry-authored" in m and "does not parse" in m
                            for m in levels(results, doctor.FAIL)))

    def test_authored_merge_counts(self):
        self.seed_home()
        good = os.path.join(self.tmp.name, "repos", "good")
        pk.write_json(home.authored_path(), {"version": 1, "projects": {
            "good": {"path": good, "notes": "n"},           # path-matched: live
            "ghost": {"path": "/gone/elsewhere", "notes": "x"},  # orphan: not live
            "vend": {"path": "/v", "external": True},       # external anchor: live
        }})
        ok = levels(doctor.check_authored(), doctor.OK)[0]
        self.assertIn("3 entries", ok)
        self.assertIn("2 live in merge", ok)

    def test_projects_broken_symlink_missing_path_stale_memory(self):
        self.seed_home()
        results = doctor.check_projects()
        fails, warns = levels(results, doctor.FAIL), levels(results, doctor.WARN)
        self.assertTrue(any("brokelink" in m and "broken symlink" in m for m in fails))
        self.assertTrue(any("gone-repo" in m and "repo moved or deleted" in m for m in warns))
        self.assertTrue(any("memstale" in m and "memory_dir" in m for m in warns))
        self.assertFalse(any("good:" in m for m in fails + warns))

    def test_adoption_conflict_real_dir_warns(self):
        self.seed_home()
        results = doctor.check_adoption()
        self.assertTrue(any("mission-control" in m and "adoption conflict" in m
                            for m in levels(results, doctor.WARN)))

    def test_adopted_store_counts_and_dup_warn(self):
        d = self.seed_adopted()
        results = doctor.check_adopted_store(adopted_dir=d)
        ok = levels(results, doctor.OK)[0]
        self.assertIn("prior=2", ok)
        self.assertIn("prem=1", ok)
        self.assertIn("lex=1", ok)
        self.assertIn("heuristic=1", ok)
        self.assertIn("other=1", ok)
        warn = levels(results, doctor.WARN)[0]
        self.assertIn("1 prem-/prior- same-slug duplicate", warn)
        self.assertIn("drain --sweep-dups", warn)
        self.assertIn("owner-gated", warn)

    def test_adopted_store_missing_warns(self):
        results = doctor.check_adopted_store(
            adopted_dir=os.path.join(self.tmp.name, "nope"))
        self.assertTrue(any("adopted store missing" in m for m in levels(results, doctor.WARN)))

    def test_know_your_user_empty_warns_then_ok(self):
        results = doctor.check_know_your_user()
        self.assertTrue(any("warmth leg is empty" in m and "helm interview" in m
                            for m in levels(results, doctor.WARN)))
        whoami.add_note("short replies", topic="voice")
        results = doctor.check_know_your_user()
        self.assertTrue(any("1 active note" in m for m in levels(results, doctor.OK)))

    def test_cv_present_and_missing(self):
        d = os.path.join(self.tmp.name, "cv")
        os.makedirs(d)
        self.assertEqual(doctor.check_cv(cv_dir=d)[0][0], doctor.OK)
        gone = doctor.check_cv(cv_dir=os.path.join(self.tmp.name, "no-cv"))
        self.assertEqual(gone[0][0], doctor.WARN)
        self.assertIn("recall plane offline", gone[0][1])

    def test_env_overrides_reported(self):
        msgs = levels(doctor.check_env(), doctor.OK)
        self.assertTrue(any("HELM_HOME=" + self.helm_home in m for m in msgs))


class TestCmdDoctor(DoctorBase):
    def run_doctor(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = doctor.cmd_doctor([])
        return rc, buf.getvalue()

    def snapshot(self, root):
        state = {}
        for dirpath, dirnames, filenames in os.walk(root):
            for f in filenames:
                p = os.path.join(dirpath, f)
                if os.path.islink(p):
                    state[p] = os.readlink(p)
                    continue
                with open(p, "rb") as fh:
                    state[p] = fh.read()
        return state

    def test_full_report_exit_1_on_fail_and_read_only(self):
        self.seed_home()
        adopted = self.seed_adopted()
        before = self.snapshot(self.tmp.name)
        with mock.patch.object(home, "adopted_memory_dir", lambda: adopted), \
                mock.patch.object(doctor, "check_cv",
                                  lambda: [(doctor.OK, "cv stub")]):
            rc, out = self.run_doctor()
        self.assertEqual(rc, 1)  # the broken symlink FAIL
        self.assertIn("FAIL", out)
        self.assertIn("broken symlink", out)
        self.assertIn("repo moved or deleted", out)
        self.assertIn("adoption conflict", out)
        self.assertIn("drain --sweep-dups", out)
        self.assertIn("warmth leg is empty", out)
        self.assertIn("helm doctor:", out)
        self.assertIn("1 fail", out)
        self.assertEqual(self.snapshot(self.tmp.name), before)  # READ-ONLY always

    def test_green_estate_exits_0(self):
        home.scaffold_global()
        repo = os.path.join(self.tmp.name, "repos", "solo")
        os.makedirs(repo)
        home.scaffold_project("solo")
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "solo": {"name": "solo", "path": repo, "kind": "git",
                     "status": "active", "sessions": {}, "memory_dir": None}}})
        adopted = os.path.join(self.tmp.name, "adopted-clean")
        os.makedirs(adopted)
        pk.atomic_write(os.path.join(adopted, "prior-only.md"), "stub\n")
        whoami.save_profile({"schema_version": 2, "technical_level": "expert",
                             "guidance": ["short replies"], "interview_status": "done",
                             "updated_at": "", "source": "fresh"})
        with mock.patch.object(home, "adopted_memory_dir", lambda: adopted), \
                mock.patch.object(doctor, "check_cv",
                                  lambda: [(doctor.OK, "cv stub")]):
            rc, out = self.run_doctor()
        self.assertEqual(rc, 0)
        self.assertIn("0 fail", out)
        self.assertNotIn("FAIL", out)


class PhysicsCurrencyTest(unittest.TestCase):
    def _run(self, version_out):
        import shutil as sh
        import subprocess as sp
        done = mock.Mock(returncode=0, stdout=version_out, stderr="")
        with mock.patch.object(sh, "which", lambda t: "/usr/bin/" + t), \
                mock.patch.object(sp, "run", return_value=done), \
                mock.patch.object(doctor, "PHYSICS_PROBED", {"claude": "2.1.207"}):
            return doctor.check_physics_currency()

    def test_matching_version_is_ok(self):
        res = self._run("2.1.207 (Claude Code)")
        self.assertEqual([lvl for lvl, _ in res], [doctor.OK])

    def test_drifted_version_warns_with_both_versions(self):
        res = self._run("2.1.215 (Claude Code)")
        self.assertEqual(res[0][0], doctor.WARN)
        self.assertIn("2.1.215", res[0][1])
        self.assertIn("2.1.207", res[0][1])

    def test_unparseable_version_warns(self):
        res = self._run("not a version at all")
        self.assertEqual(res[0][0], doctor.WARN)
        self.assertIn("unparseable", res[0][1])

    def test_missing_binary_is_silent(self):
        import shutil as sh
        with mock.patch.object(sh, "which", lambda t: None), \
                mock.patch.object(doctor, "PHYSICS_PROBED", {"claude": "2.1.207"}):
            self.assertEqual(doctor.check_physics_currency(), [])


if __name__ == "__main__":
    unittest.main()
