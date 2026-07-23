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

from helm import chat, doctor, home, pk, whoami


def levels(results, level):
    return [msg for l, msg in results if l == level]


class DoctorBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.helm_home = os.path.join(self.tmp.name, "helm-home")
        scan = os.path.join(self.tmp.name, "scan-root")
        os.makedirs(scan)  # a live source for the registry projection row
        self.envp = mock.patch.dict(os.environ, {
            "HELM_HOME": self.helm_home,
            "MC_HOME": os.path.join(self.tmp.name, "mc-home"),
            "HELM_CACHE_DIR": os.path.join(self.tmp.name, "cache"),
            "HELM_SCAN_ROOTS": scan,
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

    def test_lexicon_dead_vocabulary_flags_spaced_kind_only(self):
        # a space-separated multi-word kind with no keywords is DEAD symptom
        # vocabulary (the comma gate cannot rescue it); a comma CSV kind is
        # rescued by the legacy fallback and a clean entry is silent
        d = os.path.join(home.global_dir(), "lexicon")
        os.makedirs(d)
        lex = lambda term, kind: (
            "---\nname: lex-%s\ndescription: \"lexicon: %s = def\"\n"
            "metadata:\n  node_type: memory\n  type: lexicon\n"
            "  term: %s\n  scope: global\n  kind: %s\n"
            "  definition: def of %s\n---\n" % (term, term, term, kind, term))
        for term, kind in (("cli-proxy", "cli-proxy proxy codex kimi seat"),
                           ("rescued", "snowflake, avalanche"),
                           ("clean", "phrase")):
            pk.atomic_write(os.path.join(d, "lex-%s.md" % term), lex(term, kind))
        empty = os.path.join(self.tmp.name, "adopted-empty")
        os.makedirs(empty)
        with mock.patch.dict(os.environ, {"HELM_ADOPTED_DIR": empty}):
            res = doctor.check_lexicon_dead_vocabulary()
        self.assertEqual([lvl for lvl, _ in res], [doctor.WARN])
        self.assertIn("lexicon 'cli-proxy'", res[0][1])
        self.assertIn("helm store add lexicon \"cli-proxy | def of cli-proxy | "
                      "phrase | cli-proxy, proxy, codex, kimi, seat\"", res[0][1])

    def test_know_your_user_empty_warns_then_ok(self):
        results = doctor.check_know_your_user()
        self.assertTrue(any("know-your-user leg is empty" in m and "helm interview" in m
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

    def test_chat_sign_failure_is_a_loud_operator_warning(self):
        st = {"mode": "degraded", "profile": "seat-a",
              "reason": "send failed", "first_failure": "first",
              "last_failure": "last", "last_age_s": 9,
              "failure_count": 2,
              "remediation": "repair balance, then retry"}
        from helm import cell
        with mock.patch.object(chat, "node_url", return_value="http://node"), \
             mock.patch.object(chat, "node_head", return_value={"chain_index": 7}), \
             mock.patch.object(chat, "transport_status", return_value=st), \
             mock.patch.object(chat, "cells_path",
                               return_value=os.path.join(self.tmp.name, "none")), \
             mock.patch.object(cell, "bin_status", return_value={
                 "configured": True, "usable": True, "state": "ready",
                 "reason": "signer ready"}):
            results = doctor.check_chat_node()
        warning = "\n".join(levels(results, doctor.WARN))
        self.assertIn("DEGRADED profile 'seat-a': send failed", warning)
        self.assertIn("first first, last last (9s ago)", warning)
        self.assertIn("remediation: repair balance, then retry", warning)
        # The persistent incident must not disappear behind a CURRENT node-down
        # early return; doctor reports both facts until signed recovery clears it.
        with mock.patch.object(chat, "node_url", return_value="http://node"), \
             mock.patch.object(chat, "node_head", return_value=None), \
             mock.patch.object(chat, "transport_status", return_value=st):
            down = "\n".join(levels(doctor.check_chat_node(), doctor.WARN))
        self.assertIn("DEGRADED profile 'seat-a'", down)
        self.assertIn("UNREACHABLE", down)
        with mock.patch.object(chat, "node_url", return_value=None), \
             mock.patch.object(chat, "transport_status", return_value=st):
            disabled = doctor.check_chat_node()
        self.assertTrue(any(lvl == doctor.WARN and "DEGRADED" in msg
                            for lvl, msg in disabled))
        self.assertTrue(any(lvl == doctor.OK and "disabled" in msg
                            for lvl, msg in disabled))

    def test_ready_unproven_signing_is_explicit(self):
        st = {"mode": "ready", "state": "READY",
              "label": "ready (unproven)",
              "detail": "no committed signing receipt observed for this profile"}
        from helm import cell
        with mock.patch.object(chat, "node_url", return_value="http://node"), \
             mock.patch.object(chat, "node_head", return_value={"chain_index": 7}), \
             mock.patch.object(chat, "transport_status", return_value=st), \
             mock.patch.object(chat, "cells_path",
                               return_value=os.path.join(self.tmp.name, "none")), \
             mock.patch.object(cell, "bin_status", return_value={
                 "configured": True, "usable": True, "state": "ready",
                 "reason": "signer ready"}):
            warning = "\n".join(levels(doctor.check_chat_node(), doctor.WARN))
        self.assertIn("ready (unproven)", warning)
        self.assertIn("no committed signing receipt", warning)

    def test_configured_missing_signer_is_unavailable_not_unset(self):
        missing = os.path.join(self.tmp.name, "deleted-signer")
        with mock.patch.dict(os.environ, {
                "HELM_CELL_BIN": missing,
                "HELM_CHAT_NODE_URL": "http://node"}), \
             mock.patch.object(chat, "node_head",
                               return_value={"chain_index": 7}), \
             mock.patch.object(chat, "cells_path",
                               return_value=os.path.join(self.tmp.name, "none")):
            results = doctor.check_chat_node()
        warning = "\n".join(levels(results, doctor.WARN))
        self.assertIn("DEGRADED profile", warning)
        self.assertIn("signer path does not exist", warning)
        self.assertNotIn("HELM_CELL_BIN unset", warning)
        self.assertNotIn(missing, warning)


class TestProjectionRegistry(DoctorBase):
    """check_projection_registry: laws 2+3 enforced read-only — undeclared
    rebuild/source FAILs, an orphaned projection FAILs, declared staleness
    WARNs, squatters WARN; a clean scaffolded estate is one OK row."""

    def _row(self, **kw):
        base = {"name": "probe", "kind": "projection", "root": "home",
                "globs": ("probe.json",), "source": "the probe source",
                "sources": (os.path.join(self.tmp.name, "scan-root"),),
                "rebuild": "helm probe", "fresh_days": None, "mutable": False}
        base.update(kw)
        return base

    def _check(self, row):
        from helm import registry
        with mock.patch.object(registry, "projections", lambda: (row,)):
            return doctor.check_projection_registry()

    def test_green_scaffold_is_single_ok(self):
        home.scaffold_global()
        res = doctor.check_projection_registry()
        self.assertEqual([lvl for lvl, _ in res], [doctor.OK])
        self.assertIn("0 squatters", res[0][1])

    def test_undeclared_rebuild_and_source_fail(self):
        for kw in ({"rebuild": None}, {"sources": ()}):
            res = self._check(self._row(**kw))
            fails = levels(res, doctor.FAIL)
            self.assertTrue(any("undeclared" in m and "gitignored" in m
                                for m in fails), kw)

    def test_orphaned_projection_fails(self):
        home.scaffold_global()
        pk.atomic_write(os.path.join(self.helm_home, "probe.json"), "{}")
        res = self._check(self._row(
            sources=(os.path.join(self.tmp.name, "no-such-source"),)))
        self.assertTrue(any("ORPHANED" in m and "only truth" in m
                            for m in levels(res, doctor.FAIL)))

    def test_present_source_is_ok_and_absent_projection_skips_orphan_check(self):
        home.scaffold_global()
        # no probe.json on disk: a gone source is NOT an orphan (nothing to lose)
        res = self._check(self._row(
            sources=(os.path.join(self.tmp.name, "no-such-source"),)))
        self.assertEqual(levels(res, doctor.FAIL), [])
        pk.atomic_write(os.path.join(self.helm_home, "probe.json"), "{}")
        res = self._check(self._row())  # source exists -> healthy
        self.assertEqual(levels(res, doctor.FAIL), [])

    def test_stale_projection_warns(self):
        home.scaffold_global()
        p = os.path.join(self.helm_home, "probe.json")
        pk.atomic_write(p, "{}")
        os.utime(p, (0, 0))  # epoch: decades past any horizon
        res = self._check(self._row(fresh_days=30))
        self.assertTrue(any("stale" in m and "helm probe" in m
                            for m in levels(res, doctor.WARN)))

    def test_mutable_row_fails(self):
        res = self._check(self._row(mutable=True))
        self.assertTrue(any("read-only-as-truth" in m
                            for m in levels(res, doctor.FAIL)))

    def test_squatters_warn_with_paths(self):
        home.scaffold_global()
        state = os.path.join(home.global_dir(), ".state")
        pk.atomic_write(os.path.join(state, "mystery.bin"), "?")
        res = doctor.check_projection_registry()
        warns = levels(res, doctor.WARN)
        self.assertTrue(any("SQUATTER" in m and "mystery.bin" in m
                            and "helm projections" in m for m in warns))
        self.assertIn("1 squatter", levels(res, doctor.OK)[0])

    def test_survey_trouble_is_warn_not_crash(self):
        from helm import registry
        with mock.patch.object(registry, "projection_survey",
                               side_effect=OSError("boom")):
            res = doctor.check_projection_registry()
        self.assertEqual(res[0][0], doctor.WARN)
        self.assertIn("unreadable", res[0][1])


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
                                  lambda: [(doctor.OK, "cv stub")]), \
                mock.patch.object(doctor, "check_cred_families",
                                  lambda: [(doctor.OK, "families stub")]):
            rc, out = self.run_doctor()
        self.assertEqual(rc, 1)  # the broken symlink FAIL
        self.assertIn("FAIL", out)
        self.assertIn("broken symlink", out)
        self.assertIn("repo moved or deleted", out)
        self.assertIn("adoption conflict", out)
        self.assertIn("drain --sweep-dups", out)
        self.assertIn("know-your-user leg is empty", out)
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
                                  lambda: [(doctor.OK, "cv stub")]), \
                mock.patch.object(doctor, "check_cred_families",
                                  lambda: [(doctor.OK, "families stub")]):
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

    def test_version_probe_failure_warns_not_silent(self):
        # the sentinel must not fail QUIET exactly when currency is unknowable
        import shutil as sh
        import subprocess as sp
        with mock.patch.object(sh, "which", lambda t: "/usr/bin/" + t), \
                mock.patch.object(sp, "run", side_effect=OSError("boom")), \
                mock.patch.object(doctor, "PHYSICS_PROBED", {"claude": "2.1.207"}):
            res = doctor.check_physics_currency()
        self.assertEqual(res[0][0], doctor.WARN)
        self.assertIn("UNKNOWN", res[0][1])

    def test_git_present_is_ok(self):
        import shutil as sh
        with mock.patch.object(sh, "which", lambda t: "/usr/bin/git"):
            res = doctor.check_git()
        self.assertEqual(res, [(doctor.OK, "git on PATH (/usr/bin/git)")])

    def test_git_absent_warns_with_distro_command_and_jj_note(self):
        import shutil as sh
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix="os-release") as f:
            f.write('NAME="Debian GNU/Linux"\nID=debian\n')
            f.flush()
            hint = doctor._git_install_hint(os_release=f.name, platform="linux")
        self.assertEqual(hint, "sudo apt install git")
        with mock.patch.object(sh, "which", lambda t: None), \
                mock.patch.object(doctor, "_git_install_hint",
                                  lambda: "sudo apt install git"):
            res = doctor.check_git()
        self.assertEqual(res[0][0], doctor.WARN)
        self.assertIn("sudo apt install git", res[0][1])
        self.assertIn("jj/jujutsu", res[0][1])
        self.assertIn("degrade", res[0][1])

    def test_git_install_hint_distro_table(self):
        import tempfile
        cases = (("ID=ubuntu\nID_LIKE=debian\n", "sudo apt install git"),
                 ("ID=fedora\n", "sudo dnf install git"),
                 ("ID=arch\n", "sudo pacman -S git"),
                 ('ID="opensuse-tumbleweed"\nID_LIKE="suse"\n', "sudo zypper install git"),
                 ("ID=alpine\n", "sudo apk add git"),
                 ("ID=plan9\n", "install git via your distro's package manager"))
        for text, want in cases:
            with tempfile.NamedTemporaryFile("w") as f:
                f.write(text)
                f.flush()
                self.assertEqual(doctor._git_install_hint(os_release=f.name,
                                                          platform="linux"), want, text)
        self.assertEqual(doctor._git_install_hint(platform="darwin"),
                         "xcode-select --install")
        self.assertEqual(doctor._git_install_hint(os_release="/no/such",
                                                  platform="linux"),
                         "install git via your distro's package manager")

    def test_authored_non_object_entry_fails_not_crashes(self):
        # a parseable file with a null entry must FAIL, never raise —
        # hermetic: never touch the live helm home
        import tempfile
        with tempfile.TemporaryDirectory(prefix="helm-doctor-auth-") as tmp, \
                mock.patch.dict(os.environ, {"HELM_HOME": tmp}):
            os.makedirs(os.path.dirname(doctor.home.authored_path()), exist_ok=True)
            pk.write_json(doctor.home.authored_path(),
                          {"version": 1, "projects": {"proj": None}})
            res = doctor.check_authored()
        self.assertEqual(res[0][0], doctor.FAIL)
        self.assertIn("proj", res[0][1])


if __name__ == "__main__":
    unittest.main()
