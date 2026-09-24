#!/usr/bin/env python3
"""Registry-split tests — the projection (registry.json) / authored
(registry-authored.json) split. Hermetic: tmp HELM_HOME, observations injected,
scan roots pinned to an empty dir, the tmp-cwd noise filter stubbed so tmp
paths can register. The real ~/.helm is never read or written."""
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from helm import autocompact, automap, harnesses, home, pk, registry

EDGE = {"rel": "forked-from", "to": "beta", "note": "", "confirmed": True}


def _obs(cwd):
    return {"cwd": cwd, "harness": "claude", "sessions": 3,
            "last_seen": 1900000000.0, "refs": [], "days": {"2026-07-01"}}


# THE OBSERVATION, NOT THE SOURCE. Whether a read takes the registry write
# flock is a question about a running process, and reading `load` to see which
# context manager it opens answers a different one -- the reentrancy shelf in
# `_write_lock` means the same source line takes the lock or does not depending
# on who called it. So the arms ask a SEPARATE PROCESS to try the lock while
# the read is mid-flight: an independent open file description is the only
# party whose answer the loader cannot influence.
_PROBE = """
import fcntl, os, sys
os.makedirs(os.path.dirname(sys.argv[1]), exist_ok=True)
f = open(sys.argv[1], 'a')
try:
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    print('BUSY')
else:
    print('FREE')
"""


def _probe_write_flock():
    """'FREE' or 'BUSY' -- can another process take the registry write flock?"""
    path = os.path.join(home.global_dir(), ".state", "registry.lock")
    done = subprocess.run([sys.executable, "-c", _PROBE, path],
                          capture_output=True, text=True, timeout=60)
    return done.stdout.strip()


class RegistryBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="helm-registry-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        env = {k: os.environ.get(k) for k in ("HELM_HOME", "MELD_HOME", "HELM_SCAN_ROOTS")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ["HELM_SCAN_ROOTS"] = self._dir("scan-root")  # empty: no shelf tier
        os.environ.pop("MELD_HOME", None)

        def restore():
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        # hard guard: every write in these tests lands under tmp
        self.assertTrue(home.helm_home().startswith(self.tmp))
        noise = mock.patch.object(automap, "_is_noise", lambda cwd: False)
        noise.start()
        self.addCleanup(noise.stop)

    def _dir(self, *parts):
        p = os.path.join(self.tmp, *parts)
        os.makedirs(p, exist_ok=True)
        return p

    def _repo(self, *parts):
        p = self._dir(*parts)
        os.makedirs(os.path.join(p, ".git"), exist_ok=True)  # promotes without real git
        return p

    def _bytes(self, path):
        with open(path, "rb") as f:
            return f.read()


class ForgottenMembershipTest(RegistryBase):
    def gone(self, external=False):
        path = os.path.join(self.tmp, "gone", "alpha")
        rec = {"name": "alpha", "path": path, "kind": "git",
               "status": "active", "sessions": {}, "notes": "keep this",
               "edges": [dict(EDGE)]}
        if external:
            rec["external"] = True
        registry.save({"version": 1, "projects": {"alpha": rec}})
        return path

    def command(self, args):
        import contextlib
        import io
        from helm import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.cmd_projects(args)
        return rc, out.getvalue(), err.getvalue()

    def test_forget_is_explicit_archived_and_reversible_through_cli(self):
        path = self.gone()
        before = (self._bytes(home.registry_path()), self._bytes(home.authored_path()))
        rc, out, err = self.command(["forget", "alpha"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("dry-run", out)
        self.assertEqual((self._bytes(home.registry_path()),
                          self._bytes(home.authored_path())), before)
        self.assertIn("alpha", registry.load(strict=True)["projects"])
        self.assertEqual(self.command(["forget", "alpha", "--apply"])[0], 0)
        self.assertNotIn("alpha", registry.load(strict=True)["projects"])
        archive = registry._forgotten(registry._authored_load(strict=True))["alpha"]
        self.assertEqual(archive["record"]["path"], path)
        self.assertEqual(archive["record"]["notes"], "keep this")
        self.assertEqual(archive["record"]["edges"], [EDGE])
        self.assertEqual(self._bytes(home.registry_path()), before[0])
        self.assertIn("alpha  forgotten", self.command(["forgotten"])[1])
        frozen = self._bytes(home.authored_path())
        self.assertEqual(self.command(["restore", "alpha"])[0], 0)
        self.assertEqual(self._bytes(home.authored_path()), frozen)
        self.assertEqual(self.command(["restore", "alpha", "--apply"])[0], 0)
        self.assertEqual(registry.load(strict=True)["projects"]["alpha"]["notes"], "keep this")
        self.assertEqual(registry._forgotten(registry._authored_load(strict=True)), {})

    def test_moved_registration_stays_unknown_until_explicit_forget(self):
        from helm import foldcompose
        old = self._repo("old", "alpha")
        registry.save({"version": 1, "projects": {
            "alpha": {"name": "alpha", "path": old}}})
        self.assertEqual(foldcompose.project_state(old), ("registered", "alpha"))
        moved = os.path.join(self._dir("moved"), "alpha")
        os.rename(old, moved)
        self.assertTrue(os.path.isdir(os.path.join(moved, ".git")))
        self.assertFalse(os.path.lexists(old))
        self.assertEqual(foldcompose.project_state(moved), ("unknown", None))
        self.assertEqual(self.command(["forget", "alpha"])[0], 0)
        self.assertEqual(foldcompose.project_state(moved), ("unknown", None))
        self.assertEqual(self.command(["forget", "alpha", "--apply"])[0], 0)
        self.assertEqual(foldcompose.project_state(moved), ("unregistered", None))
        self.assertTrue(os.path.isdir(os.path.join(moved, ".git")))
        self.assertEqual(self.command(["restore", "alpha", "--apply"])[0], 0)
        self.assertEqual(foldcompose.project_state(moved), ("unknown", None))

    def test_gone_earlier_entry_blocks_positive_match_until_explicit_forget(self):
        from helm import foldcompose
        gone = self.gone()
        live = self._repo("live", "zeta")
        reg = registry.load(strict=True)
        reg["projects"]["zeta"] = {"name": "zeta", "path": live}
        registry.save(reg)
        self.assertFalse(os.path.lexists(gone))
        self.assertTrue(os.path.isdir(live))
        self.assertEqual(foldcompose.project_state(live), ("unknown", None))
        self.assertEqual(self.command(["forget", "alpha"])[0], 0)
        self.assertEqual(foldcompose.project_state(live), ("unknown", None))
        self.assertEqual(self.command(["forget", "alpha", "--apply"])[0], 0)
        self.assertEqual(foldcompose.project_state(live), ("registered", "zeta"))
        self.assertEqual(self.command(["restore", "alpha", "--apply"])[0], 0)
        self.assertEqual(foldcompose.project_state(live), ("unknown", None))

    def test_sync_cannot_resurrect_forgotten_observation_or_shelf(self):
        path = self.gone()
        self.assertIsNone(registry.forget("alpha", apply=True)[1])
        self._repo("gone", "alpha")
        other = self._repo("other", "beta")
        with mock.patch.object(automap, "scan_repos", return_value={path: "alpha"}):
            reg, report = registry.sync(observations=[_obs(path), _obs(other)])
        self.assertEqual(set(reg["projects"]), {"beta"})
        self.assertIn("beta", report["new"])
        self.assertEqual(registry._forgotten(registry._authored_load(strict=True))["alpha"]
                         ["record"]["path"], path)
        self.assertIsNone(registry.restore("alpha", apply=True)[1])
        self.assertEqual(registry.load(strict=True)["projects"]["alpha"]["path"], path)

    def test_forgotten_external_anchor_stays_out_and_name_is_reserved(self):
        self.gone(external=True)
        self.assertIsNone(registry.forget("alpha", apply=True)[1])
        self.assertNotIn("alpha", registry.load(strict=True)["projects"])
        newcomer = self._repo("new", "alpha")
        reg, report = registry.sync(observations=[_obs(newcomer)])
        self.assertEqual(len(report["new"]), 1)
        name = report["new"][0]
        self.assertNotEqual(name, "alpha")
        self.assertEqual(reg["projects"][name]["path"], newcomer)
        self.assertFalse(reg["projects"][name].get("edges"))
        self.assertIsNone(registry.restore("alpha", apply=True)[1])
        self.assertEqual(set(registry.load(strict=True)["projects"]), {"alpha", name})

    def test_present_unknown_and_malformed_are_not_forget_authority(self):
        import errno
        import pathlib
        path = self.gone()
        self._dir("gone", "alpha")
        row, err = registry.forget("alpha", apply=True)
        self.assertIsNone(row)
        self.assertIn("still exists", err)
        before = (self._bytes(home.registry_path()), self._bytes(home.authored_path()))
        for failure in (PermissionError(errno.EACCES, "denied"),
                        NotADirectoryError(errno.ENOTDIR, "not a directory"),
                        RuntimeError("symlink loop")):
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch.object(pathlib.Path, "resolve", side_effect=failure):
                row, err = registry.forget("alpha", apply=True)
                self.assertIsNone(row)
                self.assertIn("UNKNOWN", err)
        self.assertEqual((self._bytes(home.registry_path()),
                          self._bytes(home.authored_path())), before)
        auth = registry._authored_load(strict=True)
        auth["forgotten_projects"] = {"alpha": {"record": {"name": "alpha"}}}
        pk.write_json(home.authored_path(), auth)
        self.assertRaises(ValueError, registry.load, strict=True)
        self.assertEqual(self.command(["forget", "alpha", "--apply"])[0], 1)
        self.assertTrue(os.path.isdir(path))

    def test_restore_refuses_name_reassignment_and_unknown_flags(self):
        self.gone()
        self.assertIsNone(registry.forget("alpha", apply=True)[1])
        reg = pk.read_json(home.registry_path())
        reg["projects"]["alpha"]["path"] = self._dir("elsewhere", "alpha")
        pk.write_json(home.registry_path(), reg)
        before = (self._bytes(home.registry_path()), self._bytes(home.authored_path()))
        row, err = registry.restore("alpha", apply=True)
        self.assertIsNone(row)
        self.assertIn("different path", err)
        self.assertEqual(self.command(["forget", "alpha", "--force"])[0], 2)
        self.assertEqual(self.command(["restore"])[0], 2)
        self.assertEqual((self._bytes(home.registry_path()),
                          self._bytes(home.authored_path())), before)


    def test_restore_validates_the_whole_archive_before_any_write(self):
        path = self.gone()
        self.assertEqual(registry.load(strict=True)["projects"]["alpha"]["path"], path)
        self.assertIsNone(registry.forget("alpha", apply=True)[1])
        self.assertNotIn("alpha", registry.load(strict=True)["projects"])
        auth = registry._authored_load(strict=True)
        self.assertEqual(auth["forgotten_projects"]["alpha"]["record"]["path"], path)
        for field, value in (("cwds", "not-a-list"), ("cv_scope", [])):
            with self.subTest(field=field):
                bad = dict(auth, forgotten_projects={"alpha": dict(auth["forgotten_projects"]["alpha"])})
                bad["forgotten_projects"]["alpha"]["record"] = dict(
                    auth["forgotten_projects"]["alpha"]["record"], **{field: value})
                pk.write_json(home.authored_path(), bad)
                before = (self._bytes(home.registry_path()), self._bytes(home.authored_path()))
                self.assertRaises(ValueError, registry.restore, "alpha", apply=True)
                self.assertEqual((self._bytes(home.registry_path()),
                                  self._bytes(home.authored_path())), before)
        pk.write_json(home.authored_path(), auth)
        self.assertIsNone(registry.restore("alpha", apply=True)[1])
        restored = registry.load(strict=True)["projects"]["alpha"]
        self.assertEqual((restored["path"], restored["notes"], restored["edges"]),
                         (path, "keep this", [EDGE]))
        self.assertNotIn("alpha", registry._forgotten(registry._authored_load(strict=True)))

    def test_restore_refuses_different_name_at_same_path_including_external(self):
        path = self.gone()
        self.assertIsNone(registry.forget("alpha", apply=True)[1])
        for external in (False, True):
            with self.subTest(external=external):
                pk.write_json(home.registry_path(), {"version": 1, "projects": {}})
                auth = registry._authored_load(strict=True)
                auth["projects"].pop("beta", None)
                pk.write_json(home.authored_path(), auth)
                if external:
                    registry.add_external("beta", path)
                    pk.write_json(home.registry_path(), {"version": 1, "projects": {}})
                else:
                    registry.save({"version": 1, "projects": {"beta": {
                        "name": "beta", "path": path, "sessions": {}}}})
                self.assertEqual(registry.load(strict=True)["projects"]["beta"]["path"], path)
                before = (self._bytes(home.registry_path()), self._bytes(home.authored_path()))
                row, err = registry.restore("alpha", apply=True)
                self.assertIsNone(row)
                self.assertIn("different name", err)
                self.assertEqual((self._bytes(home.registry_path()),
                                  self._bytes(home.authored_path())), before)

    def test_legacy_inline_authorship_survives_forget_sync_restore_and_rebuild(self):
        path = os.path.join(self.tmp, "legacy", "alpha")
        pk.write_json(home.registry_path(), {"version": 1, "projects": {"alpha": {
            "name": "alpha", "path": path, "notes": "legacy note",
            "edges": [dict(EDGE)], "sessions": {}}}})
        pk.write_json(home.authored_path(), {"version": 1, "projects": {}})
        self.assertIsNone(registry.forget("alpha", apply=True)[1])
        registry.sync(observations=[])
        self.assertIsNone(registry.restore("alpha", apply=True)[1])
        authored = pk.read_json(home.authored_path())["projects"]["alpha"]
        self.assertEqual(authored["notes"], "legacy note")
        self.assertEqual(authored["edges"], [EDGE])
        self.assertNotIn("notes", pk.read_json(home.registry_path())["projects"]["alpha"])
        os.remove(home.registry_path())
        self._repo("legacy", "alpha")
        reg, _ = registry.sync(observations=[_obs(path)])
        self.assertEqual(reg["projects"]["alpha"]["notes"], "legacy note")
        self.assertEqual(reg["projects"]["alpha"]["edges"], [EDGE])

    def test_restore_interruption_keeps_archive_and_current_authorship_conflict_refuses(self):
        self.gone()
        self.assertIsNone(registry.forget("alpha", apply=True)[1])
        initial = (pk.read_json(home.registry_path()), pk.read_json(home.authored_path()))
        writer = pk.write_json
        for stop_at in (1, 2, 3):
            with self.subTest(stop_at=stop_at):
                writer(home.registry_path(), initial[0])
                writer(home.authored_path(), initial[1])
                calls = []
                def interrupt(path, row):
                    calls.append(path)
                    if len(calls) == stop_at:
                        raise OSError("injected interrupted restore")
                    return writer(path, row)
                with mock.patch.object(pk, "write_json", side_effect=interrupt):
                    self.assertRaises(OSError, registry.restore, "alpha", apply=True)
                self.assertEqual(len(calls), stop_at)
                self.assertIn("alpha", registry._forgotten(registry._authored_load(strict=True)))
                self.assertNotIn("alpha", registry.load(strict=True)["projects"])
        auth = registry._authored_load(strict=True)
        auth["projects"]["alpha"]["notes"] = "new operator edit"
        writer(home.authored_path(), auth)
        row, err = registry.restore("alpha", apply=True)
        self.assertIsNone(row)
        self.assertIn("conflict", err)
        self.assertEqual(registry._authored_load(strict=True)["projects"]["alpha"]
                         ["notes"], "new operator edit")

    def test_concurrent_save_cannot_erase_an_applied_forget(self):
        import threading
        self.gone()
        reg = registry.load(strict=True)
        captured, release, started, done = (threading.Event() for _ in range(4))
        errors = []
        writer = pk.write_json
        def delayed(path, row):
            if path == home.authored_path() and threading.current_thread().name == "registry-save":
                captured.set()
                if not release.wait(5):
                    raise RuntimeError("writer barrier was not released")
            return writer(path, row)
        def save():
            try:
                registry.save(reg)
            except BaseException as exc:
                errors.append(exc)
        def forget():
            started.set()
            try:
                row, err = registry.forget("alpha", apply=True)
                if err or row is None:
                    raise AssertionError(err)
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()
        with mock.patch.object(pk, "write_json", side_effect=delayed):
            first = threading.Thread(target=save, name="registry-save")
            second = threading.Thread(target=forget)
            first.start()
            try:
                self.assertTrue(captured.wait(5))
                second.start()
                self.assertTrue(started.wait(5))
                blocked = not done.wait(0.05)
            finally:
                release.set()
                first.join(5)
                if second.ident is not None:
                    second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(blocked, "forget crossed another registry writer's transaction")
        self.assertIn("alpha", registry._forgotten(registry._authored_load(strict=True)))
        self.assertNotIn("alpha", registry.load(strict=True)["projects"])

    def test_lock_failure_refuses_and_project_home_is_never_removed(self):
        path = self.gone()
        self.assertEqual(registry.load(strict=True)["projects"]["alpha"]["path"], path)
        project_home = self._dir("helm-home", "alpha")
        marker = os.path.join(project_home, "keep.json")
        pk.write_json(marker, {"unrebuildable": True})
        before = (self._bytes(home.registry_path()), self._bytes(home.authored_path()))
        with mock.patch.object(registry.fcntl, "flock", side_effect=OSError("lock unavailable")):
            self.assertRaises(OSError, registry.forget, "alpha", apply=True)
        self.assertEqual((self._bytes(home.registry_path()), self._bytes(home.authored_path())), before)
        self.assertIsNone(registry.forget("alpha", apply=True)[1])
        self.assertNotIn("alpha", registry.load(strict=True)["projects"])
        archive = registry._forgotten(registry._authored_load(strict=True))["alpha"]["record"]
        self.assertEqual((archive["path"], archive["notes"]), (path, "keep this"))
        self.assertEqual(pk.read_json(marker), {"unrebuildable": True})


class RepointMembershipTest(RegistryBase):
    gone = ForgottenMembershipTest.gone
    command = ForgottenMembershipTest.command

    def state(self):
        return self._bytes(home.registry_path()), self._bytes(home.authored_path())

    def binding(self):
        return registry._bindings(registry._authored_load(strict=True))["alpha"]

    def move(self, source, target=None, undo=False):
        args = ["repoint", "alpha", "--from", source]
        args += ["--undo"] if undo else ["--to", target]
        return self.command(args + ["--apply"])

    def test_directory_repoint_preserves_history_and_home_without_git(self):
        from helm import foldcompose
        source = self.gone()
        reg = registry.load(strict=True)
        reg["projects"]["alpha"].update(sessions={"claude": 7}, cwds=[source])
        registry.save(reg)
        target = self._dir("migrated", "alpha")
        self.assertFalse(os.path.lexists(os.path.join(target, ".git")))
        marker = os.path.join(self._dir("helm-home", "alpha"), "activation.json")
        pk.write_json(marker, {"evidence": "not revalidated by repoint"})
        activation = self._bytes(marker)
        before = self.state()
        self.assertEqual(foldcompose.project_state(target), ("unknown", None))
        self.assertEqual(self.command(["repoint", "alpha", "--from", source, "--to", target])[0], 0)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.move(source, target)[0], 0)
        self.assertEqual(self._bytes(home.registry_path()), before[0])
        rec = registry.load(strict=True)["projects"]["alpha"]
        self.assertEqual((rec["path"], rec["kind"], rec["sessions"]), (target, "dir", {}))
        self.assertEqual((rec["notes"], rec["edges"]), ("keep this", [EDGE]))
        self.assertEqual(self.binding()["transitions"][0]["record"]["sessions"], {"claude": 7})
        self.assertEqual(foldcompose.project_state(target), ("registered", "alpha"))
        self.assertEqual(self._bytes(marker), activation)
        self.assertEqual(self.move(target, undo=True)[0], 0)
        self.assertEqual(foldcompose.project_state(target), ("unknown", None))
        self.assertEqual(registry.load(strict=True)["projects"]["alpha"]["sessions"], {"claude": 7})
        self.assertEqual(self.move(source, undo=True)[0], 0)  # undo of undo is redo
        self.assertEqual(registry.load(strict=True)["projects"]["alpha"]["path"], target)

    def test_current_adoption_drives_sync_doctor_and_profile_lookup(self):
        from helm import doctor, whoami
        source = self.gone()
        target = self._dir("target")
        old_home = self._dir("old-adoption")
        new_home = self._dir("new-adoption")
        legacy_home = self._dir("legacy-adoption")
        for path in (old_home, new_home):
            pk.write_json(os.path.join(path, "user-profile", "profile.json"), {"source": path})
        reg = registry.load(strict=True)
        reg["projects"]["alpha"]["adopt"] = old_home
        registry.save(reg)
        self.assertEqual(registry.adopted_homes(), {"alpha": old_home})
        self.assertEqual(self.move(source, target)[0], 0)
        reg = registry.load(strict=True)
        reg["projects"]["alpha"]["adopt"] = new_home
        registry.save(reg)
        auth = registry._authored_load(strict=True)
        self.assertEqual(auth["projects"]["alpha"]["adopt"], old_home)
        self.assertEqual(auth["projects"][registry._qualified("alpha", target)]["adopt"], new_home)
        # Legacy config can precede discovery; binding support must not orphan it.
        auth["projects"]["legacy"] = {"adopt": legacy_home}
        pk.write_json(home.authored_path(), auth)
        self.assertEqual(registry.adopted_homes(), {"alpha": new_home, "legacy": legacy_home})
        project_home = home.project_dir("alpha")
        self.assertFalse(os.path.lexists(project_home))
        registry.sync(observations=[])
        self.assertTrue(os.path.islink(project_home))
        self.assertEqual(os.readlink(project_home), new_home)
        with mock.patch.dict(os.environ):
            os.environ.pop("HELM_PROFILE_HOME", None)
            self.assertEqual(whoami.scaffold_profile_path(),
                             os.path.join(new_home, "user-profile", "profile.json"))
        os.unlink(project_home)
        os.mkdir(project_home)
        findings = doctor.check_adoption()
        self.assertEqual(findings, [(doctor.WARN,
            "alpha: adoption conflict — helm dir is a real dir, expected symlink -> " + new_home)])
        self.assertEqual(self.binding()["transitions"][0]["record"]["adopt"], old_home)

    def test_corrupt_authorship_projects_and_show_refuse_through_cli(self):
        import contextlib
        import io
        from helm import cli
        self.gone()
        authored = self._bytes(home.authored_path())
        projection = self._bytes(home.registry_path())
        def invoke(args):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(cli, "which_helm_warning", return_value=None), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(args)
            return rc, out.getvalue(), err.getvalue()
        for args in (["projects"], ["show", "alpha"]):
            self.assertEqual(invoke(args)[0], 0)
        corrupt = b"{ unreadable authored binding authority"
        with open(home.authored_path(), "wb") as f:
            f.write(corrupt)
        for args in (["projects"], ["show", "alpha"]):
            rc, out, err = invoke(args)
            self.assertEqual(rc, 1)
            self.assertEqual(out, "")
            self.assertIn("registry UNKNOWN", err)
            self.assertNotIn("Traceback", err)
            self.assertEqual(self._bytes(home.authored_path()), corrupt)
            self.assertEqual(self._bytes(home.registry_path()), projection)
        backups = [name for name in os.listdir(os.path.dirname(home.authored_path()))
                   if name.startswith(os.path.basename(home.authored_path()) + ".corrupt-")]
        self.assertGreaterEqual(len(backups), 1)
        for name in backups:
            self.assertEqual(self._bytes(os.path.join(os.path.dirname(home.authored_path()), name)), corrupt)
        with open(home.authored_path(), "wb") as f:
            f.write(authored)
        for args in (["projects"], ["show", "alpha"]):
            self.assertEqual(invoke(args)[0], 0)

    def test_stale_and_missing_generation_saves_refuse_attempted_edits(self):
        import copy
        source = self.gone()
        target = self._dir("target")
        old = registry.load(strict=True)
        self.assertEqual(self.move(source, target)[0], 0)
        target_era = registry.load(strict=True)
        target_era["projects"]["alpha"]["notes"] = "latest operator note"
        target_era["projects"]["alpha"]["sessions"] = {"claude": 11}
        registry.save(target_era)
        self.assertEqual(self.move(target, undo=True)[0], 0)
        self.assertEqual(self.move(source, undo=True)[0], 0)
        current = registry.load(strict=True)
        self.assertNotEqual(current["projects"]["alpha"]["_binding_generation"],
                            target_era["projects"]["alpha"]["_binding_generation"])
        self.assertEqual(current["projects"]["alpha"]["sessions"], {"claude": 11})
        for stale in (old, target_era, copy.deepcopy(current)):
            if stale is not old and stale is not target_era:
                del stale["projects"]["alpha"]["_binding_generation"]
            stale["projects"]["alpha"]["notes"] = "attempted stale edit"
            self.assertEqual(stale["projects"]["alpha"]["notes"], "attempted stale edit")
            before = self.state()
            with self.assertRaisesRegex(ValueError, "stale project binding"):
                registry.save(stale)
            self.assertEqual(self.state(), before)
            self.assertEqual(registry.load(strict=True)["projects"]["alpha"]["notes"],
                             "latest operator note")
        current["projects"]["alpha"]["notes"] = "fresh explicit edit"
        registry.save(current)
        self.assertEqual(registry.load(strict=True)["projects"]["alpha"]["notes"], "fresh explicit edit")

    def test_sync_suppresses_abandoned_sources_and_rebuilds_external_binding(self):
        source = self.gone(external=True)
        target = self._dir("target")
        self.assertEqual(self.move(source, target)[0], 0)
        self._repo("gone", "alpha")
        beta = self._repo("other", "beta")
        with mock.patch.object(automap, "scan_repos", return_value={source: "alpha"}):
            reg, report = registry.sync(observations=[_obs(source), _obs(target), _obs(beta)])
        self.assertEqual(set(reg["projects"]), {"alpha", "beta"})
        self.assertEqual(reg["projects"]["alpha"]["path"], target)
        self.assertIn("beta", report["new"])
        self.assertEqual(reg["projects"]["alpha"]["sessions"], {"claude": 3})
        os.unlink(home.registry_path())
        rebuilt = registry.load(strict=True)["projects"]
        self.assertEqual(set(rebuilt), {"alpha"})
        self.assertEqual(rebuilt["alpha"]["path"], target)
        self.assertEqual(rebuilt["alpha"]["notes"], "keep this")
        self.assertEqual(self.move(target, undo=True)[0], 0)
        with mock.patch.object(automap, "scan_repos", return_value={target: "alpha"}):
            reg, _ = registry.sync(observations=[_obs(target), _obs(beta)])
        self.assertEqual(reg["projects"]["alpha"]["path"], source)
        self.assertEqual(set(reg["projects"]), {"alpha", "beta"})

    def test_undo_preserves_new_authorship_and_location_scoped_observations(self):
        source = self.gone()
        target = self._dir("target")
        self.assertEqual(self.move(source, target)[0], 0)
        reg = registry.load(strict=True)
        reg["projects"]["alpha"].update(notes="edited after repoint", sessions={"claude": 9}, cwds=[target])
        registry.save(reg)
        self.assertEqual(self.move(target, undo=True)[0], 0)
        restored = registry.load(strict=True)["projects"]["alpha"]
        self.assertEqual(restored["notes"], "edited after repoint")
        self.assertEqual(restored["sessions"], {})
        departed = self.binding()["transitions"][-1]["record"]
        self.assertEqual((departed["sessions"], departed["cwds"]), ({"claude": 9}, [target]))
        self.assertEqual(self.binding()["transitions"][0]["record"]["notes"], "keep this")
        self.assertEqual(self.move(source, undo=True)[0], 0)
        self.assertEqual(registry.load(strict=True)["projects"]["alpha"]["sessions"], {"claude": 9})

    def test_refusals_preserve_bytes_and_forgotten_membership_requires_restore(self):
        import errno
        import pathlib
        source = self.gone()
        target = self._dir("target")
        before = self.state()
        for args in (["repoint", "alpha"],
                     ["repoint", "alpha", "--from", source, "--to", target, "--undo"],
                     ["repoint", "alpha", "--from", source, "--to", target, "--force"]):
            self.assertEqual(self.command(args)[0], 2)
        self.assertEqual(self.move(source + "-wrong", target)[0], 1)
        self.assertEqual(self.move(source, target + "-missing")[0], 1)
        file_target = os.path.join(self.tmp, "file")
        with open(file_target, "w") as f:
            f.write("not a directory")
        self.assertEqual(self.move(source, file_target)[0], 1)
        for failure in (PermissionError(errno.EACCES, "denied"),
                        NotADirectoryError(errno.ENOTDIR, "not directory"), RuntimeError("loop")):
            with mock.patch.object(pathlib.Path, "resolve", side_effect=failure):
                self.assertEqual(self.move(source, target)[0], 1)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.move(source, target)[0], 0)
        os.rmdir(target)
        self.assertEqual(self.command(["forget", "alpha", "--apply"])[0], 0)
        before = self.state()
        self.assertEqual(self.move(target, undo=True)[0], 1)
        self.assertEqual(self.move(target, self._dir("third"))[0], 1)
        self.assertEqual(self.state(), before)
        self.assertNotIn("alpha", registry.load(strict=True)["projects"])
        self.assertEqual(self.command(["restore", "alpha", "--apply"])[0], 0)
        self.assertEqual(self.move(target, undo=True)[0], 0)
        self.assertEqual(len(self.binding()["transitions"]), 2)

    def test_target_alias_and_independent_authored_edit_conflicts_refuse(self):
        source = self.gone()
        target = self._dir("target")
        alias = os.path.join(self.tmp, "alias")
        os.symlink(target, alias)
        registry.add_external("beta", alias)
        before = self.state()
        self.assertEqual(self.move(source, target)[0], 1)
        self.assertEqual(self.state(), before)
        other = self._dir("free")
        self.assertEqual(self.move(source, other)[0], 0)
        self.assertEqual(registry.load(strict=True)["projects"]["alpha"]["path"], other)
        binding = self.binding()
        self.assertEqual(binding["path"], other)
        self.assertEqual(binding["transitions"][-1]["from"], source)
        auth = registry._authored_load(strict=True)
        auth["projects"]["alpha"]["notes"] = "independent old-location edit"
        pk.write_json(home.authored_path(), auth)
        before = self.state()
        rc, out, err = self.move(other, undo=True)
        self.assertEqual(rc, 1)
        self.assertEqual(err, "helm projects: destination authored fields conflict; repoint refused\n")
        self.assertEqual(out, "")
        self.assertEqual(self.state(), before)
        self.assertEqual(self.binding(), binding)
        current = registry.load(strict=True)["projects"]["alpha"]
        self.assertEqual((current["path"], current["notes"]), (other, "keep this"))
        self.assertEqual(registry._authored_load(strict=True)["projects"]["alpha"]["notes"],
                         "independent old-location edit")

    def test_atomic_authored_publication_interruption_and_lock_failure(self):
        source = self.gone()
        target = self._dir("target")
        before = self.state()
        writer = pk.write_json
        calls = []
        def interrupted(path, value):
            calls.append(path)
            raise OSError("before publication")
        with mock.patch.object(pk, "write_json", side_effect=interrupted):
            self.assertEqual(self.move(source, target)[0], 1)
        self.assertEqual(calls, [home.authored_path()])
        self.assertEqual(self.state(), before)
        self.assertEqual(registry.load(strict=True)["projects"]["alpha"]["path"], source)
        with mock.patch.object(registry.fcntl, "flock", side_effect=OSError("lock failure")):
            self.assertEqual(self.move(source, target)[0], 1)
        self.assertEqual(self.state(), before)
        def published_then_interrupted(path, value):
            writer(path, value)
            raise OSError("after publication")
        with mock.patch.object(pk, "write_json", side_effect=published_then_interrupted):
            self.assertEqual(self.move(source, target)[0], 1)
        self.assertEqual(registry.load(strict=True)["projects"]["alpha"]["path"], target)
        self.assertEqual(self._bytes(home.registry_path()), before[0])
        self.assertEqual(len(self.binding()["transitions"]), 1)
        self.assertEqual(self.move(source, target)[0], 1)  # stale retry cannot republish

    def test_imported_binding_shape_and_cross_entry_conflicts_fail_closed(self):
        import copy
        from helm import foldcompose
        source = self.gone()
        target = self._dir("target")
        self.assertEqual(self.move(source, target)[0], 0)
        valid = registry._authored_load(strict=True)
        self.assertEqual(foldcompose.project_state(target), ("registered", "alpha"))
        corruptions = []
        malformed = copy.deepcopy(valid)
        malformed["project_bindings"]["alpha"]["transitions"][0]["record"]["cwds"] = "bad"
        corruptions.append(malformed)
        conflict = copy.deepcopy(valid)
        conflict["projects"]["beta"] = {"path": target, "external": True}
        corruptions.append(conflict)
        chain = copy.deepcopy(valid)
        chain["project_bindings"]["alpha"]["path"] = source
        corruptions.append(chain)
        for candidate in corruptions:
            pk.write_json(home.authored_path(), candidate)
            before = self.state()
            for strict in (False, True):
                with self.assertRaises(ValueError):
                    registry.load(strict=strict)
            self.assertEqual(foldcompose.project_state(target), ("unknown", None))
            with self.assertRaises(ValueError):
                registry.sync(observations=[])
            self.assertEqual(self.state(), before)
        pk.write_json(home.authored_path(), valid)
        self.assertEqual(foldcompose.project_state(target), ("registered", "alpha"))

    def test_ship_import_validates_before_resync_and_preserves_invalid_authority(self):
        import contextlib
        import io
        from types import SimpleNamespace
        from helm import foldcompose, ship
        source = self.gone()
        target = self._dir("target")
        self.assertEqual(self.move(source, target)[0], 0)
        valid = registry._authored_load(strict=True)
        mutate = [False]
        pulls = []
        def git(_root, *args):
            if "pull" in args:
                pulls.append(args)
                if mutate[0]:
                    auth = registry._authored_load(strict=True)
                    auth["projects"]["beta"] = {"path": target, "external": True}
                    pk.write_json(home.authored_path(), auth)
            return SimpleNamespace(returncode=0, stdout="same-head\n", stderr="")
        with mock.patch.object(ship, "_is_repo", return_value=True), \
                mock.patch.object(ship, "_remote", return_value="hermetic"), \
                mock.patch.object(ship, "_branch", return_value="main"), \
                mock.patch.object(ship, "_git", side_effect=git), \
                mock.patch.object(ship, "_resync", return_value=(1, 0)) as resync, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ship.pull(), 0)
            self.assertEqual(resync.call_count, 1)
            mutate[0] = True
            self.assertEqual(ship.pull(), 1)
            self.assertEqual(resync.call_count, 1)
        self.assertEqual(len(pulls), 2)
        self.assertIn("beta", registry._authored_load(strict=True)["projects"])
        self.assertEqual(foldcompose.project_state(target), ("unknown", None))
        pk.write_json(home.authored_path(), valid)
        self.assertEqual(foldcompose.project_state(target), ("registered", "alpha"))


class TestSplit(RegistryBase):
    def test_save_splits_load_merges_shape_unchanged(self):
        path = self._dir("src", "alpha")
        rec = {"name": "alpha", "path": path, "kind": "git", "status": "active",
               "sessions": {}, "notes": "born of beta", "aliases": ["al"],
               "edges": [dict(EDGE)]}
        registry.save({"version": 1, "projects": {"alpha": dict(rec)}})
        raw = pk.read_json(home.registry_path())["projects"]["alpha"]
        for k in registry.AUTHORED_FIELDS:
            self.assertNotIn(k, raw)  # projection file carries nothing authored
        entry = pk.read_json(home.authored_path())["projects"]["alpha"]
        self.assertEqual(entry["edges"], rec["edges"])
        self.assertEqual(entry["path"], path)  # path-stamped for the collision guard
        merged = registry.load()["projects"]["alpha"]
        self.assertEqual(merged, rec)  # public API shape: exactly the pre-split record

    def test_partial_save_never_deletes_other_entries(self):
        registry.save({"version": 1, "projects": {
            "alpha": {"name": "alpha", "path": "/a", "notes": "keep me"}}})
        registry.save({"version": 1, "projects": {
            "beta": {"name": "beta", "path": "/b", "notes": "new"}}})
        entries = pk.read_json(home.authored_path())["projects"]
        self.assertEqual(entries["alpha"]["notes"], "keep me")
        self.assertEqual(entries["beta"]["notes"], "new")


class TestMigration(RegistryBase):
    def probe_during_load(self, strict=False):
        """Every answer the flock probe gives while one `registry.load` runs.

        The probe fires from inside `_apply_bindings`, which every pass of the
        merge calls after it has read both layers and before it writes either --
        the deepest point of the read, and the frame py-spy caught holding the
        flock when the fleet convoyed (task/2703). One answer per pass, so the
        LIST also says how many passes the load made.
        """
        seen = []
        real = registry._apply_bindings

        def spy(projects, auth):
            seen.append(_probe_write_flock())
            return real(projects, auth)

        with mock.patch.object(registry, "_apply_bindings", spy):
            reg = registry.load(strict=strict)
        return seen, reg

    def mixed_era(self):
        """A registry.json carrying an authored field inline -- the pre-split
        shape a migration exists to move."""
        path = self._dir("src", "alpha")
        pk.write_json(home.registry_path(), {"version": 1, "projects": {"alpha": {
            "name": "alpha", "path": path, "kind": "repo", "status": "active",
            "sessions": {}, "notes": "the inline note", "edges": [dict(EDGE)]}}})
        return path

    def test_an_ordinary_read_leaves_the_write_flock_free(self):
        """THE CURE FOR task/2703. A registry with nothing left to migrate --
        which is every registry after its first load -- is read with no flock
        held, so a page refresh in the owner's browser cannot queue every helm
        verb in every seat behind it.
        """
        path = self.mixed_era()
        registry.load()  # the one-time door runs and the migration lands
        self.assertEqual(pk.read_json(home.authored_path())["projects"]["alpha"]
                         ["notes"], "the inline note")
        seen, reg = self.probe_during_load()
        self.assertEqual(seen, ["FREE"])  # one pass, and it held nothing
        self.assertEqual(reg["projects"]["alpha"]["notes"], "the inline note")
        self.assertEqual(reg["projects"]["alpha"]["path"], path)
        # AND THE STRICT SPELLING KEEPS ITS OWN PROMISE, which is stronger: it
        # may not even create the sidecar the probe would have to open.
        seen, reg = self.probe_during_load(strict=True)
        self.assertEqual(seen, ["FREE"])
        self.assertEqual(reg["projects"]["alpha"]["edges"], [EDGE])

    def test_a_mixed_era_read_still_migrates_and_takes_the_lock_to_do_it(self):
        """THE PROPERTY THE CURE MAY NOT COST. The lock did not disappear -- it
        moved to the one load that actually writes. A mixed-era registry still
        ends up migrated, the write is still serialized against every other
        owner write door, and the unlocked first pass is the probe that decides
        which of the two it is.
        """
        path = self.mixed_era()
        seen, reg = self.probe_during_load()
        # FREE on the pass that only looked, BUSY on the pass that wrote: the
        # load re-reads under the flock rather than persisting bytes it read
        # before taking it.
        self.assertEqual(seen, ["FREE", "BUSY"])
        self.assertEqual(reg["projects"]["alpha"]["notes"], "the inline note")
        entry = pk.read_json(home.authored_path())["projects"]["alpha"]
        self.assertEqual(entry["notes"], "the inline note")
        self.assertEqual(entry["edges"], [EDGE])
        self.assertEqual(entry["path"], path)
        raw = pk.read_json(home.registry_path())["projects"]["alpha"]
        self.assertEqual(raw["path"], path)  # the projection keeps its own bytes
        for k in registry.AUTHORED_FIELDS:
            self.assertNotIn(k, raw)
        # AND THE NEXT READ IS FREE: one-time means one time.
        seen, _ = self.probe_during_load()
        self.assertEqual(seen, ["FREE"])

    def test_moves_once_idempotent_and_faithful(self):
        path = self._dir("src", "alpha")
        pk.write_json(home.registry_path(), {"version": 1, "projects": {"alpha": {
            "name": "alpha", "path": path, "kind": "git", "status": "active",
            "sessions": {}, "notes": "née melds — ünïcode", "retired": False,
            "edges": [dict(EDGE)]}}})
        self.assertFalse(os.path.exists(home.authored_path()))
        merged = registry.load()["projects"]["alpha"]
        self.assertEqual(merged["notes"], "née melds — ünïcode")
        self.assertEqual(merged["edges"], [EDGE])
        raw = pk.read_json(home.registry_path())["projects"]["alpha"]
        for k in registry.AUTHORED_FIELDS:
            self.assertNotIn(k, raw)
        entry = pk.read_json(home.authored_path())["projects"]["alpha"]
        self.assertEqual(entry["notes"], "née melds — ünïcode")
        self.assertIs(entry["retired"], False)  # falsy values move too, verbatim
        frozen = (self._bytes(home.registry_path()), self._bytes(home.authored_path()))
        registry.load()  # second load: byte-identical files, no re-migration
        self.assertEqual(
            (self._bytes(home.registry_path()), self._bytes(home.authored_path())), frozen)

    def test_never_clobbers_already_authored_values(self):
        path = self._dir("src", "alpha")
        pk.write_json(home.authored_path(), {"version": 1, "projects": {
            "alpha": {"path": path, "notes": "the live note"}}})
        pk.write_json(home.registry_path(), {"version": 1, "projects": {"alpha": {
            "name": "alpha", "path": path, "notes": "stale backup note",
            "edges": [dict(EDGE)]}}})
        merged = registry.load()["projects"]["alpha"]
        self.assertEqual(merged["notes"], "the live note")  # authored file outranks
        self.assertEqual(merged["edges"], [EDGE])           # new field still migrates

    def test_path_mismatch_never_grafts(self):
        # a same-name entry authored against another path: overlay + migration both refuse
        pk.write_json(home.authored_path(), {"version": 1, "projects": {
            "proj": {"path": "/somewhere/else/proj", "notes": "not yours",
                     "edges": [dict(EDGE)]}}})
        pk.write_json(home.registry_path(), {"version": 1, "projects": {"proj": {
            "name": "proj", "path": "/fresh/proj", "kind": "git", "sessions": {},
            "notes": "mine"}}})
        merged = registry.load()["projects"]["proj"]
        self.assertEqual(merged["notes"], "mine")
        self.assertNotIn("edges", merged)
        entry = pk.read_json(home.authored_path())["projects"]["proj"]
        self.assertEqual(entry["notes"], "not yours")  # foreign entry untouched


class TestSyncSurvival(RegistryBase):
    def test_authored_survives_projection_wipe_and_resync(self):
        repo = self._repo("src", "alpha")
        registry.sync(observations=[_obs(repo)])
        registry.add_edge("alpha", "forked-from", "beta", "the ancestry")
        registry.add_external("vendor", self._dir("src", "vendor"), "prior art")
        os.remove(home.registry_path())
        vendor = registry.load()["projects"]["vendor"]  # external anchor materializes
        self.assertTrue(vendor["external"])
        self.assertEqual(vendor["notes"], "prior art")
        reg, _ = registry.sync(observations=[_obs(repo)])
        self.assertEqual(reg["projects"]["alpha"]["edges"][0]["to"], "beta")
        self.assertTrue(reg["projects"]["vendor"]["external"])
        raw = pk.read_json(home.registry_path())["projects"]
        self.assertNotIn("edges", raw["alpha"])  # regenerated projection stays pure

    def test_collision_mints_fresh_name_cannot_inherit(self):
        a = self._repo("one", "proj")
        b = self._repo("two", "proj")
        registry.sync(observations=[_obs(a)])
        registry.add_edge("proj", "forked-from", "elder", "authored on the incumbent")
        reg, report = registry.sync(observations=[_obs(b)])
        by_path = {r["path"]: n for n, r in reg["projects"].items()}
        minted = by_path[b]
        self.assertEqual(by_path[a], "proj")
        self.assertNotEqual(minted, "proj")
        self.assertIn(minted, report["new"])
        self.assertFalse(reg["projects"][minted].get("edges"))
        self.assertEqual(reg["projects"]["proj"]["edges"][0]["to"], "elder")
        self.assertEqual(sorted(pk.read_json(home.authored_path())["projects"]), ["proj"])


class TestReviewHardening(RegistryBase):
    """Cross-family (codex-seat) review findings, 2026-07-19 — pinned."""

    def test_save_never_clobbers_path_mismatched_authored_entry(self):
        # incumbent authored at path A; a merged view carrying same-NAME
        # authored fields at path B must not delete A's entry on save
        pk.write_json(home.authored_path(), {"version": 1, "projects": {
            "proj": {"path": "/elsewhere/proj", "notes": "the unrebuildable note"}}})
        reg = {"version": 1, "projects": {
            "proj": {"name": "proj", "path": "/here/proj", "kind": "git",
                     "status": "active", "sessions": {},
                     "edges": [{"rel": "forked-from", "to": "elder"}]}}}
        registry.save(reg)
        auth = pk.read_json(home.authored_path())["projects"]
        self.assertEqual(auth["proj"]["notes"], "the unrebuildable note")  # survived
        q = registry._qualified("proj", "/here/proj")
        self.assertEqual(auth[q]["edges"][0]["to"], "elder")  # newcomer parked
        # and load() resolves the newcomer via the qualified key
        merged = registry.load()["projects"]["proj"]
        self.assertEqual(merged["edges"][0]["to"], "elder")

    def test_corrupt_authored_file_backed_up_before_any_save(self):
        os.makedirs(os.path.dirname(home.authored_path()), exist_ok=True)
        with open(home.authored_path(), "w") as f:
            f.write("{ this is not json")
        before = self._bytes(home.authored_path())
        with self.assertRaises(ValueError):
            registry.save({"version": 1, "projects": {}})
        self.assertEqual(self._bytes(home.authored_path()), before)
        siblings = [n for n in os.listdir(os.path.dirname(home.authored_path()))
                    if n.startswith(os.path.basename(home.authored_path()) + ".corrupt-")]
        self.assertEqual(len(siblings), 1)  # the garbled bytes survive for repair
        bak = os.path.join(os.path.dirname(home.authored_path()), siblings[0])
        with open(bak) as f:
            self.assertIn("this is not json", f.read())
        # Unknown bytes may contain location authority: backup is not permission
        # to erase it and publish an empty authored registry.
        with self.assertRaises(ValueError):
            registry.load(strict=True)

    def test_one_corruption_is_backed_up_once_however_often_it_is_read(self):
        os.makedirs(os.path.dirname(home.authored_path()), exist_ok=True)
        folder = os.path.dirname(home.authored_path())
        stem = os.path.basename(home.authored_path()) + ".corrupt-"
        copies = lambda: sorted(n for n in os.listdir(folder) if n.startswith(stem))
        with open(home.authored_path(), "w") as f:
            f.write("{ this is not json")
        stamps = iter("2000-01-01T00:00:%02dZ" % n for n in range(60))
        with mock.patch.object(registry.pk, "now_ts", lambda: next(stamps)):
            for _ in range(5):
                with self.assertRaises(registry.AuthoredUnreadable):
                    registry.authored_host()
        self.assertEqual(len(copies()), 1)
        # POSITIVE CONTROL: the listing can see a second copy, and a DIFFERENT
        # corruption still earns one, which is what the backup exists for.
        with open(home.authored_path(), "w") as f:
            f.write("{ nor is this")
        with self.assertRaises(registry.AuthoredUnreadable):
            registry.authored_host()
        self.assertEqual(len(copies()), 2)


class TestProjections(RegistryBase):
    """The projection registry (laws 2+3 as a manifest): well-formed rows,
    disk survey + squatter detection, the CLI surface, and the converge
    proof — a wiped projection rebuilds to the same estate from source."""

    def setUp(self):
        super().setUp()
        cache = self._dir("cache")
        envp = mock.patch.dict(os.environ, {"HELM_CACHE_DIR": cache})
        envp.start()
        self.addCleanup(envp.stop)
        self.cache = cache

    def test_manifest_well_formed(self):
        rows = registry.projections()
        names = [r["name"] for r in rows]
        self.assertEqual(len(names), len(set(names)))  # names are keys
        for r in rows:
            self.assertIn(r["kind"], registry.KINDS)
            self.assertIn(r["root"], ("home", "cache"))
            self.assertTrue(r["globs"])
            self.assertIs(r["mutable"], False)
            if r["kind"] == "projection":
                # the card's law: an undeclared rebuild/source cannot ship
                self.assertTrue(r["rebuild"], r["name"])
                self.assertTrue(r["sources"], r["name"])
                self.assertTrue(r["source"], r["name"])

    def test_manifest_scan_roots_match_the_producer(self):
        fresh = os.path.join(self.tmp, "fresh-home")
        cases = (
            ("", (os.path.join(fresh, "dev"),)),
            ("~/src::/srv/repos", (os.path.join(fresh, "src"), "/srv/repos")),
        )
        for raw, want in cases:
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {
                    "HOME": fresh,
                    "HELM_SCAN_ROOTS": raw,
                    "MELD_SCAN_ROOTS": "",
            }):
                self.assertEqual(tuple(automap._scan_roots()), want)
                row = next(r for r in registry.projections()
                           if r["name"] == "registry")
                self.assertTrue(all(root in row["sources"] for root in want))

    def test_manifest_declares_json_serializable_genesis(self):
        import json
        rows = registry.projections()
        by = {r["name"]: r for r in rows}
        self.assertEqual(by["registry"]["genesis"], {
            "json": {"version": 1, "projects": {}},
            "volatile_strings": ("generated_ts",),
        })
        self.assertEqual(by["syn-cache"]["genesis"], {
            "json": {}, "volatile_strings": (),
        })
        self.assertEqual(by["codex-cwd-cache"]["genesis"], {
            "json": {}, "volatile_strings": (),
        })
        self.assertEqual(
            [r["name"] for r in rows if r.get("genesis")],
            ["registry", "syn-cache", "codex-cwd-cache"],
        )
        json.dumps(rows)

    def test_survey_classifies_synced_estate_with_zero_squatters(self):
        repo = self._repo("src", "alpha")
        registry.sync(observations=[_obs(repo)])
        rows, squat = registry.projection_survey()
        by = {r["name"]: r for r in rows}
        self.assertIn("_global/registry.json", by["registry"]["files"])
        self.assertTrue(any(f.startswith("alpha/") for f in
                            by["project-registry"]["files"]))
        self.assertIn("_global/registry-authored.json",
                      by["registry-authored"]["files"])
        self.assertTrue(by["store-global"]["files"])  # the seeded reflex pack
        self.assertEqual(squat, {"home": [], "cache": []})

    def test_squatter_detected_in_both_roots(self):
        home.scaffold_global()
        state = os.path.join(home.global_dir(), ".state")
        os.makedirs(state, exist_ok=True)
        pk.atomic_write(os.path.join(state, "mystery.bin"), "?")
        pk.atomic_write(os.path.join(self.cache, "stray.json"), "{}")
        _rows, squat = registry.projection_survey()
        self.assertEqual(squat["home"], ["_global/.state/mystery.bin"])
        self.assertEqual(squat["cache"], ["stray.json"])

    def test_storage_matrix_artifact_lock_and_writer_are_declared_state(self):
        home.scaffold_global()
        state = os.path.join(home.global_dir(), ".state")
        os.makedirs(state, exist_ok=True)
        for name in ("storage-matrix.json", "storage-matrix.json.lock",
                     "storage-matrix.json.123.abc.tmp"):
            pk.atomic_write(os.path.join(state, name), "{}")
        rows, squat = registry.projection_survey()
        row = next(r for r in rows if r["name"] == "storage-matrix")
        self.assertEqual(row["kind"], "state")
        self.assertEqual(sorted(row["files"]), [
            "_global/.state/storage-matrix.json",
            "_global/.state/storage-matrix.json.123.abc.tmp",
            "_global/.state/storage-matrix.json.lock",
        ])
        self.assertEqual(squat, {"home": [], "cache": []})

    def test_autocompact_state_and_fixed_delivery_lock_are_declared(self):
        home.scaffold_global()
        state = os.path.join(home.global_dir(), ".state")
        os.makedirs(state, exist_ok=True)
        for name in ("autocompact.json", "autocompact.json.lock"):
            pk.atomic_write(os.path.join(state, name), "{}")
        delivery_lock = autocompact._refusal_delivery_lock()
        delivery_lock.close()
        rows, squat = registry.projection_survey()
        row = next(r for r in rows if r["name"] == "autocompact")
        self.assertEqual(row["kind"], "state")
        self.assertEqual(sorted(row["files"]), [
            "_global/.state/autocompact.json",
            "_global/.state/autocompact.json.delivery.lock",
            "_global/.state/autocompact.json.lock",
        ])
        self.assertEqual(squat, {"home": [], "cache": []})

    def test_chat_event_receipts_are_declared_state(self):
        home.scaffold_global()
        path = os.path.join(home.global_dir(), ".state",
                            "chat-event-receipts", "0123456789abcdef",
                            "main.jsonl.events.json")
        pk.atomic_write(path, "{}")
        rows, squat = registry.projection_survey()
        row = next(r for r in rows if r["name"] == "chat-event-receipts")
        self.assertEqual(row["kind"], "events")
        self.assertEqual(row["files"], [
            "_global/.state/chat-event-receipts/0123456789abcdef/"
            "main.jsonl.events.json",
        ])
        self.assertEqual(squat, {"home": [], "cache": []})

    def test_symlinks_never_crossed_or_flagged(self):
        # an adopted (symlinked) home is someone else's estate — its contents
        # are never classified, and the link itself is never a squatter
        home.scaffold_global()
        alien = self._dir("alien-estate")
        pk.atomic_write(os.path.join(alien, "junk.bin"), "?")
        os.symlink(alien, os.path.join(home.helm_home(), "adopted-proj"))
        _rows, squat = registry.projection_survey()
        self.assertEqual(squat["home"], [])

    def test_wipe_and_resync_converges(self):
        # THE overlay-not-store proof: the projection is regenerable from its
        # authoritative source — wipe registry.json + the mirror, re-sync,
        # same estate (doctor stays read-only; this test IS the converge check)
        repo = self._repo("src", "alpha")
        reg1, _ = registry.sync(observations=[_obs(repo)])
        want = {n: p["path"] for n, p in reg1["projects"].items()}
        mirror = os.path.join(home.project_dir("alpha"), "registry.json")
        self.assertTrue(os.path.exists(mirror))
        os.remove(home.registry_path())
        os.remove(mirror)
        reg2, _ = registry.sync(observations=[_obs(repo)])
        self.assertEqual({n: p["path"] for n, p in reg2["projects"].items()}, want)
        self.assertTrue(os.path.exists(mirror))

    def test_cmd_projections_table_and_json(self):
        import contextlib
        import io
        import json
        home.scaffold_global()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(registry.cmd_projections([]), 0)
        out = buf.getvalue()
        self.assertIn("helm projections", out)
        self.assertIn("rebuild: helm sync", out)
        self.assertIn("no unclassified squatters", out)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(registry.cmd_projections(["--json"]), 0)
        d = json.loads(buf.getvalue())
        self.assertEqual(len(d["rows"]), len(registry.projections()))
        self.assertEqual(d["squatters"], {"home": [], "cache": []})


class SyncEndToEndTest(RegistryBase):
    """The whole pipeline from disk: real claude/codex session files -> the
    harness scanners -> automap canonicalization -> registry.sync -> a
    materialized, correctly-typed project. Earlier sync tests inject synthetic
    observations; this one lays the transcripts down and reads them back."""

    def setUp(self):
        super().setUp()
        cache = self._dir("cache")
        prev = os.environ.get("HELM_CACHE_DIR")
        os.environ["HELM_CACHE_DIR"] = cache          # the codex sidecar lands here

        def restore():
            if prev is None:
                os.environ.pop("HELM_CACHE_DIR", None)
            else:
                os.environ["HELM_CACHE_DIR"] = prev
        self.addCleanup(restore)

    def _claude_session(self, projects_root, slug, cwd):
        d = os.path.join(projects_root, slug)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "sess.jsonl"), "w") as f:
            f.write('{"cwd": "%s", "gitBranch": "main"}\n' % cwd)

    def _codex_rollout(self, sessions_root, cwd):
        d = os.path.join(sessions_root, "2026", "07", "20")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "rollout-2026-07-20T10-00-00-abcd.jsonl")
        with open(p, "w") as f:
            f.write('{"payload": {"cwd": "%s"}}\n' % cwd)

    def _scan(self, projects_root, sessions_root):
        return (harnesses.claude_observations(claude_root=projects_root)
                + harnesses.codex_observations(roots=[sessions_root]))

    def test_disk_scan_to_sync_materializes_the_project(self):
        repo = self._repo("dev", "myproj")            # real dir + fake .git
        projects_root = self._dir("claude", "projects")
        sessions_root = self._dir("codex", "sessions")
        self._claude_session(projects_root, "-tmp-dev-myproj", repo)
        self._codex_rollout(sessions_root, repo)

        obs = self._scan(projects_root, sessions_root)
        # both harnesses observed the same cwd — two rows, one canonical root
        self.assertEqual({o["harness"] for o in obs}, {"claude", "codex"})

        reg, report = registry.sync(observations=obs)
        proj = next((p for p in reg["projects"].values() if p["path"] == repo), None)
        self.assertIsNotNone(proj, "the scanned project must register")
        self.assertEqual(proj["kind"], "git")
        self.assertIn(repo, proj["cwds"])
        self.assertEqual(set(proj["sessions"]), {"claude", "codex"})
        self.assertIn(proj["name"], report["new"])

        # the codex sidecar was written by the real scan (the 6k-file fast path)
        self.assertTrue(os.path.isfile(harnesses._codex_cache_path()))

    def test_resync_is_additive_and_idempotent(self):
        repo = self._repo("dev", "myproj")
        projects_root = self._dir("claude", "projects")
        sessions_root = self._dir("codex", "sessions")
        self._claude_session(projects_root, "-tmp-dev-myproj", repo)
        self._codex_rollout(sessions_root, repo)
        obs = self._scan(projects_root, sessions_root)

        reg1, _ = registry.sync(observations=obs)
        n1 = len(reg1["projects"])
        reg2, report2 = registry.sync(observations=self._scan(projects_root, sessions_root))
        self.assertEqual(len(reg2["projects"]), n1)     # no duplicate project
        name = next(p["name"] for p in reg2["projects"].values() if p["path"] == repo)
        self.assertIn(name, report2["updated"])         # re-seen, not re-created

    def test_second_scan_hits_the_codex_sidecar(self):
        repo = self._repo("dev", "myproj")
        sessions_root = self._dir("codex", "sessions")
        self._codex_rollout(sessions_root, repo)
        harnesses.codex_observations(roots=[sessions_root])   # populate the sidecar
        with mock.patch.object(harnesses, "_sniff_cwd",
                               side_effect=AssertionError("re-sniffed under sync")):
            obs = harnesses.codex_observations(roots=[sessions_root])
        self.assertEqual([o["cwd"] for o in obs], [repo])


class NeverMigratedFieldSurvivesEveryMembershipWriter(RegistryBase):
    """R2 (P1). A NEVER_MIGRATED field is EXECUTABLE — `gate` is the project's
    own suite command — so which LAYER it came from is part of whether helm may
    spawn it. `registry.json` is the projection: rebuildable by any re-scan,
    written by every discovery pass. The load migration and `save` were cured
    round one, and the class survived in the writers beside them, because each
    one reads the MERGED record and writes the AUTHORED file: `repoint` copied
    the merged value under the target's stamp, `forget` archived it into the
    authored file and `restore` then published it as a declaration the owner
    never wrote. Every writer is swept here, both poles each — a projection copy
    cannot enter, and a legitimately authored value cannot be lost.
    """

    FIELD = "gate"
    BLOCK = {"command": ["./run-suite.sh"], "protocol": "exit"}
    INLINE = {"command": ["true"], "protocol": "exit"}

    def setUp(self):
        super().setUp()
        # CONTROL ON THE PREMISE: this arm's subject is a field that is BOTH
        # authored and never migrated. If that ever stops being true the arms
        # below are about nothing, and they say so here rather than passing.
        self.assertIn(self.FIELD, registry.AUTHORED_FIELDS)
        self.assertIn(self.FIELD, registry.NEVER_MIGRATED_FIELDS)

    def plant(self, path, authored=None, inline=None):
        """A registered project at `path`, with the field in either layer."""
        rec = {"name": "alpha", "path": path, "kind": "git",
               "status": "active", "sessions": {}, "notes": "keep this"}
        if inline is not None:
            rec[self.FIELD] = inline
        pk.write_json(home.registry_path(),
                      {"version": 1, "projects": {"alpha": rec}})
        entry = {"path": path}
        if authored is not None:
            entry[self.FIELD] = authored
        pk.write_json(home.authored_path(),
                      {"version": 1, "projects": {"alpha": entry}})

    def authored_entry(self):
        return pk.read_json(home.authored_path())["projects"]

    def authored_holders(self):
        """Every authored KEY carrying the field — key-agnostic on purpose, since
        a repoint publishes under the target's path stamp rather than the plain
        name, and asserting on the plain name alone would read the wrong entry
        and pass for the wrong reason."""
        return sorted(key for key, entry in self.authored_entry().items()
                      if isinstance(entry, dict)
                      and entry.get(self.FIELD) is not None)

    def declarations(self):
        return [(rec["project"], rec["path"], rec["value"])
                for rec in registry.authored_declarations(self.FIELD)]

    def test_repoint_carries_an_authored_command_and_promotes_no_projection(self):  # noqa: VACUOUS_ASSERTION — the AUTHORED pole is the unconditional positive on the same observables and runs FIRST (the declaration follows the project to its new location), and the projection pole asserts the merged record CARRIES the block before asserting nothing was promoted, so an empty answer cannot come from a fixture that planted nothing
        source = os.path.join(self.tmp, "gone", "alpha")     # never created
        target = self._dir("moved", "alpha")
        self.plant(source, authored=self.BLOCK)
        # THE POSITIVE FIRST: an authored declaration follows the project to its
        # new location, or the cure below would just be a deletion.
        self.assertIsNone(registry.repoint("alpha", source, target,
                                           apply=True)[1])
        self.assertEqual(self.declarations(), [("alpha", target, self.BLOCK)])
        # The authored entry for the TARGET location holds it, whichever key the
        # repoint chose (the departed entry is retained for `undo`, which is why
        # the active record is the one `authored_declarations` answers with).
        self.assertEqual([entry[self.FIELD] for entry
                          in self.authored_entry().values()
                          if entry.get("path") == target], [self.BLOCK])
        # THE MUST-HIT, on an otherwise identical transition: the same block in
        # the PROJECTION only. This is the arm that goes red if repoint takes the
        # field off the merged record again.
        target2 = self._dir("moved-again", "alpha")
        self.plant(source, inline=self.INLINE)
        self.assertEqual(registry.load(strict=True)["projects"]["alpha"][
            self.FIELD], self.INLINE, "the merged record is where it was read")
        self.assertIsNone(registry.repoint("alpha", source, target2,
                                           apply=True)[1])
        self.assertEqual(self.declarations(), [])
        self.assertEqual(self.authored_holders(), [])
        # CONTROL. BLAST RADIUS: this arm. The rest of the authored record still
        # travels, so the absence above is this ONE field's provenance and not a
        # repoint that stopped carrying anything.
        self.assertEqual(registry.load(strict=True)["projects"]["alpha"][
            "notes"], "keep this")

    def test_forget_and_restore_carry_authorship_and_promote_no_projection(self):  # noqa: VACUOUS_ASSERTION — the AUTHORED pole runs FIRST as the unconditional positive (archive carries the block, restore republishes it), the projection pole asserts the merged record CARRIES it before asserting nothing was promoted, and the projection layer is then asserted positively to still hold it
        path = os.path.join(self.tmp, "gone", "alpha")        # gone: forget
        self.plant(path, authored=self.BLOCK)
        # THE POSITIVE FIRST: the owner's declaration survives the round trip.
        self.assertIsNone(registry.forget("alpha", apply=True)[1])
        archive = registry._forgotten(registry._authored_load(strict=True))
        self.assertEqual(archive["alpha"]["record"][self.FIELD], self.BLOCK)
        self.assertIsNone(registry.restore("alpha", apply=True)[1])
        self.assertEqual(self.declarations(), [("alpha", path, self.BLOCK)])
        # THE MUST-HIT, same two writers, same otherwise-identical record: the
        # block in the PROJECTION only. The archive must not become the smuggling
        # channel, and restore must not publish what it never owned.
        self.plant(path, inline=self.INLINE)
        self.assertEqual(registry.load(strict=True)["projects"]["alpha"][
            self.FIELD], self.INLINE, "the merged record is where it was read")
        self.assertIsNone(registry.forget("alpha", apply=True)[1])
        archive = registry._forgotten(registry._authored_load(strict=True))
        self.assertNotIn(self.FIELD, archive["alpha"]["record"])
        self.assertIsNone(registry.restore("alpha", apply=True)[1])
        self.assertEqual(self.declarations(), [])
        self.assertEqual(self.authored_holders(), [])
        # AND THE PROJECTION KEEPS ITS OWN BLOCK. A restore is a membership
        # transition, not a deletion of a layer it never owned: the naive cure
        # (strip the field from the archive) made the block VANISH on restore,
        # and the gate then said "you declared nothing" instead of "that is a
        # projection".
        self.assertEqual(
            pk.read_json(home.registry_path())["projects"]["alpha"][self.FIELD],
            self.INLINE)
        self.assertEqual(registry.declaration_provenance("alpha", path,
                                                         self.FIELD),
                         (self.INLINE, "projection"))
        # CONTROL. BLAST RADIUS: this arm. With NO block in either layer the same
        # round trip leaves the field absent from both, so the "projection"
        # answer above came from the planted block and not from a restore that
        # invents the field for every record it republishes.
        self.plant(path)
        self.assertIsNone(registry.forget("alpha", apply=True)[1])
        self.assertIsNone(registry.restore("alpha", apply=True)[1])
        self.assertEqual(registry.declaration_provenance("alpha", path,
                                                         self.FIELD),
                         (None, None))


    def _base_era_archive(self, name="alpha"):
        """The archive a PRE-NEVER-MIGRATED `forget` wrote, reconstructed from
        the base writer itself. Verbatim from `git show
        de8487fb92b1:helm/registry.py` — `_forget` line 510:

            row = {"record": dict(rec, name=name), "forgotten_at": pk.now_ts()}

        over `reg["projects"][name]`, which is the MERGED record. Two facts make
        it the hostile input: base `AUTHORED_FIELDS` did not contain `gate` (line
        83 of the same file), so its `restore` left the block in the projection
        and never treated it as authority; and the row carries NO provenance key
        of any kind, because the concept did not exist. Nothing about these bytes
        is forged — they are what the shipped writer of that era produced.
        """
        merged = registry.load(strict=True)["projects"][name]
        # THE PREMISE, ASSERTED: the merged record really does carry the field, or
        # this fixture is a hostile input that attacks nothing.
        self.assertEqual(merged[self.FIELD], self.INLINE)
        row = {"record": dict(merged, name=name), "forgotten_at": pk.now_ts()}
        self.assertNotIn(registry.ARCHIVED_AUTHORED_KEY, row)
        auth = pk.read_json(home.authored_path())
        auth.setdefault("forgotten_projects", {})[name] = row
        pk.write_json(home.authored_path(), auth)
        return row

    def test_a_base_era_archive_restores_into_the_projection_not_authority(self):  # noqa: VACUOUS_ASSERTION — every empty answer sits beside an unconditional positive on the same observable: declaration_provenance is asserted to equal (INLINE, "projection") and the projection file is asserted to still hold the block, the authored-conflict pole asserts the BLOCK survives as authority, and the CONTROL at the end forgets and restores through THIS writer and asserts the declaration IS republished
        """C2 (P1). `restore` copied every AUTHORED_FIELD off the archived record,
        and the archive is not a layer — it is a snapshot of the MERGED record. A
        base-era `forget` archived the projection's `gate` block inside that
        snapshot, legitimately, because the field was not authored then; today's
        restore read those same legitimate bytes as an executable declaration the
        owner never wrote, and published them into the authored file. Absence of
        a provenance mark is not a yes: an archive promotes a never-migrated field
        only where the ARCHIVE ITSELF records that the field was authored.
        """
        path = os.path.join(self.tmp, "gone", "alpha")       # gone: restorable
        self.plant(path, inline=self.INLINE)
        archive = self._base_era_archive()
        self.assertEqual(registry._archived_authored(archive), frozenset())
        self.assertIsNone(registry.restore("alpha", apply=True)[1])
        # THE PRODUCT LAW: nothing authored, and the block still reads as what it
        # is. This is the assertion that goes red without the cure — it published
        # ("alpha", path, INLINE) as a declaration.
        self.assertEqual(self.declarations(), [])
        self.assertEqual(self.authored_holders(), [])
        self.assertEqual(registry.declaration_provenance("alpha", path,
                                                         self.FIELD),
                         (self.INLINE, "projection"))
        self.assertEqual(
            pk.read_json(home.registry_path())["projects"]["alpha"][self.FIELD],
            self.INLINE)
        # AND THE ARCHIVE IS STILL THE ONLY COPY WHEN THE PROJECTION HAS BEEN
        # REBUILT WITHOUT IT, which is the case that makes "just drop the field"
        # wrong: the bytes go back to the projection they came from rather than
        # being deleted, so the gate goes on saying "that is a projection"
        # instead of "you declared nothing".
        self.plant(path, inline=self.INLINE)
        archive = self._base_era_archive()
        reg = pk.read_json(home.registry_path())
        del reg["projects"]["alpha"][self.FIELD]              # a re-scan rebuilt it
        pk.write_json(home.registry_path(), reg)
        self.assertIsNone(registry.restore("alpha", apply=True)[1])
        self.assertEqual(self.declarations(), [])
        self.assertEqual(registry.declaration_provenance("alpha", path,
                                                         self.FIELD),
                         (self.INLINE, "projection"))
        # AND AN AUTHORED DECLARATION IS NEVER OVERWRITTEN BY ARCHIVE BYTES. The
        # old reader put the archived value in `keep`, so this pair either
        # REFUSED the restore as a conflict or replaced the owner's command with
        # the projection's; the membership transition now completes and leaves
        # the authored value exactly where it was.
        self.plant(path, inline=self.INLINE)
        self._base_era_archive()                  # its record carries INLINE
        auth = pk.read_json(home.authored_path())
        auth["projects"]["alpha"][self.FIELD] = self.BLOCK    # the owner declares
        pk.write_json(home.authored_path(), auth)
        self.assertIsNone(registry.restore("alpha", apply=True)[1])
        self.assertEqual(self.declarations(), [("alpha", path, self.BLOCK)])
        self.assertEqual(registry.declaration_provenance("alpha", path,
                                                         self.FIELD),
                         (self.BLOCK, "authored"))
        self.assertEqual(
            pk.read_json(home.registry_path())["projects"]["alpha"][self.FIELD],
            self.INLINE)
        # CONTROL, SAME DOOR, SAME OBSERVABLE. BLAST RADIUS: this arm. An archive
        # THIS writer created records the provenance and its authored declaration
        # IS promoted — so the refusals above are the missing mark and not
        # "restore stopped carrying declarations".
        self.plant(path, authored=self.BLOCK)
        row, err = registry.forget("alpha", apply=True)
        self.assertIsNone(err, err)
        self.assertEqual(row[registry.ARCHIVED_AUTHORED_KEY], [self.FIELD])
        self.assertIsNone(registry.restore("alpha", apply=True)[1])
        self.assertEqual(self.declarations(), [("alpha", path, self.BLOCK)])

    def test_a_marked_field_the_archive_does_not_carry_is_malformed(self):
        """C2's fail-closed half. The mark is read by `restore` to promote an
        EXECUTABLE value, so a mark naming a field the archived record does not
        hold is a claim about nothing and the archive reader refuses it rather
        than letting restore interpret it. BLAST RADIUS: this arm — the positive
        control beside it is the same archive with the mark removed, which loads.
        """
        path = os.path.join(self.tmp, "gone", "alpha")
        self.plant(path, authored=self.BLOCK)
        self.assertIsNone(registry.forget("alpha", apply=True)[1])
        auth = pk.read_json(home.authored_path())
        row = auth["forgotten_projects"]["alpha"]
        del row["record"][self.FIELD]                  # the mark now names nothing
        pk.write_json(home.authored_path(), auth)
        with self.assertRaises(ValueError):
            registry._forgotten(registry._authored_load(strict=True))
        auth = pk.read_json(home.authored_path())
        del auth["forgotten_projects"]["alpha"][registry.ARCHIVED_AUTHORED_KEY]
        pk.write_json(home.authored_path(), auth)
        self.assertIn("alpha",
                      registry._forgotten(registry._authored_load(strict=True)))


class TheProjectLightSurvivesEveryMembershipWriter(
        NeverMigratedFieldSurvivesEveryMembershipWriter):
    """The light is never-migrated for `gate`'s reason one step removed: it
    executes nothing, but it decides whether work may START in a project, so a
    projection that could mint one would let a discovery pass switch a project
    off in the owner's name. Every membership writer the parent sweeps is swept
    again here with the light as the field — both poles each, inherited whole,
    so a writer added to that sweep covers this field the day it is written.
    """

    FIELD = "state"
    BLOCK = {"colour": "red", "reason": "frozen by the owner", "by": "owner", "ts": 1}
    INLINE = {"colour": "green", "reason": "a scan wrote this", "by": "", "ts": 1}


class TheResidencySurvivesEveryMembershipWriter(
        NeverMigratedFieldSurvivesEveryMembershipWriter):
    """Residency is never-migrated because it lets turn text LEAVE the LAN, and
    that cannot be recalled: a projection that could mint `may-leave-lan` would
    let a discovery pass publish a client's text in the owner's name. Every
    membership writer the parent sweeps is swept again with residency as the
    field, both poles each."""

    FIELD = "residency"
    BLOCK = {"value": "may-leave-lan", "reason": "non-client", "by": "owner", "ts": 1}
    INLINE = {"value": "may-leave-lan", "reason": "a scan wrote this", "by": "", "ts": 1}


class ProjectResidencyTest(RegistryBase):
    """Only an AUTHORED `may-leave-lan` opens the door; every other state is
    `lan-only`, and `why` names which one it was."""

    OPEN = {"value": "may-leave-lan", "reason": "non-client", "by": "owner", "ts": 1}

    def plant(self, authored=None, inline=None):
        path = self._dir("dev", "alpha")
        rec = {"name": "alpha", "path": path, "kind": "git", "status": "active",
               "sessions": {}}
        if inline is not None:
            rec["residency"] = inline
        pk.write_json(home.registry_path(), {"version": 1, "projects": {"alpha": rec}})
        entry = {"path": path}
        if authored is not None:
            entry["residency"] = authored
        pk.write_json(home.authored_path(), {"version": 1, "projects": {"alpha": entry}})
        return path

    def test_every_state_but_an_authored_open_value_is_lan_only(self):  # noqa: VACUOUS_ASSERTION — the authored may-leave-lan control closes the arm on the same reader and asserts the open value, so the lan-only answers cannot come from a reader that only ever says lan-only
        self.plant()
        for name, needle in ((None, "no project"), ("ghost", "not in the registry"),
                             ("alpha", "no residency field")):
            with self.subTest(name=name):
                r = registry.residency(name)
                self.assertEqual((r["value"], r["authored"]), ("lan-only", False))
                self.assertIn(needle, r["why"])
        for bad in ("may-leave-lan", {"value": "anywhere"}, {"reason": "x"}, ["may-leave-lan"]):
            with self.subTest(bad=bad):
                self.plant(authored=bad)
                self.assertEqual(registry.residency("alpha")["value"], "lan-only")
                self.assertIn("malformed", registry.residency("alpha")["why"])
        self.plant(authored=self.OPEN)
        r = registry.residency("alpha")
        self.assertEqual((r["value"], r["authored"], r["reason"]),
                         ("may-leave-lan", True, "non-client"))

    def test_a_projection_block_opens_nothing(self):
        self.plant(inline=self.OPEN)
        # THE PREMISE: the merged record really carries the block.
        self.assertEqual(registry.load()["projects"]["alpha"]["residency"], self.OPEN)
        self.assertEqual(registry.residency("alpha")["value"], "lan-only")
        self.plant(authored=self.OPEN)                         # the same block, authored
        self.assertEqual(registry.residency("alpha")["value"], "may-leave-lan")

    def test_an_unreadable_authored_layer_is_lan_only(self):
        self.plant(authored=self.OPEN)
        self.assertEqual(registry.residency("alpha")["value"], "may-leave-lan")
        with open(home.authored_path(), "w") as f:
            f.write("{garbled")
        r = registry.residency("alpha")
        self.assertEqual(r["value"], "lan-only")
        self.assertIn("registry unreadable", r["why"])

    def test_set_clear_dry_run_and_refusals(self):  # noqa: VACUOUS_ASSERTION — the applied set is asserted open on the same reader before the clear asserts it shut, and the dry run asserts the planned value it did not write
        self.plant()
        before = self._bytes(home.authored_path())
        row, err = registry.set_residency("alpha", "may-leave-lan", reason="non-client")
        self.assertIsNone(err)
        self.assertEqual(row["residency"]["value"], "may-leave-lan")   # it planned
        self.assertEqual(self._bytes(home.authored_path()), before)   # and wrote nothing
        row, err = registry.set_residency("alpha", "may-leave-lan", reason="non-client",
                                          by="owner", apply=True)
        self.assertIsNone(err)
        self.assertEqual(registry.residency("alpha")["value"], "may-leave-lan")
        registry.save(registry.load())                              # a save keeps it
        self.assertEqual(registry.residency("alpha")["value"], "may-leave-lan")
        self.assertNotIn("residency", pk.read_json(home.registry_path())["projects"]["alpha"])
        row, err = registry.set_residency("alpha", "clear", apply=True)
        self.assertIsNone(err)
        self.assertEqual(row["was"]["value"], "may-leave-lan")
        self.assertEqual(registry.residency("alpha")["value"], "lan-only")
        for name, value, why, needle in (
                ("alpha", "open", "r", "must be one of"),
                ("alpha", "may-leave-lan", None, "needs a reason"),
                ("alpha", "lan-only", "  ", "needs a reason"),
                ("nobody", "lan-only", "r", "unknown project 'nobody'")):
            with self.subTest(value=value, why=why):
                row, err = registry.set_residency(name, value, reason=why, apply=True)
                self.assertIsNone(row)
                self.assertIn(needle, err)


class ProjectLightTest(RegistryBase):
    """One light, two authors. The scan says what it SAW (`status`); a person
    says what they DECIDED (`state`), and the decision outranks the observation
    wherever the light is read. `light()` is that read, and it answers from the
    authored layer so a projection byte can never pass for the owner's word.
    """

    def plant(self, status="active", authored=None, inline=None):
        path = self._dir("dev", "alpha")
        rec = {"name": "alpha", "path": path, "kind": "git",
               "status": status, "sessions": {}}
        if inline is not None:
            rec["state"] = inline
        pk.write_json(home.registry_path(),
                      {"version": 1, "projects": {"alpha": rec}})
        entry = {"path": path}
        if authored is not None:
            entry["state"] = authored
        pk.write_json(home.authored_path(),
                      {"version": 1, "projects": {"alpha": entry}})
        return path

    def read(self):
        return registry.lights(registry.load())["alpha"]

    def test_an_unauthored_project_shows_what_the_scan_saw(self):  # noqa: VACUOUS_ASSERTION — one unconditional equality on the planted status and its provenance
        self.plant(status="dormant")
        lit = self.read()
        self.assertEqual((lit["colour"], lit["authored"]), ("dormant", False))

    def test_an_authored_colour_outranks_the_scan_in_both_directions(self):  # noqa: VACUOUS_ASSERTION — each direction reads the scan's value as its control and then the authored one, with setter and reason
        """Both directions, because only one would pass for a function that
        returned the scarier of the two: red over an active scan, and green
        over a dormant one."""
        for status, colour in (("active", "red"), ("dormant", "green")):
            with self.subTest(status=status, colour=colour):
                self.plant(status=status)
                self.assertEqual(self.read()["colour"], status)   # the control
                row, err = registry.state("alpha", colour, reason="because",
                                          by="owner", apply=True)
                self.assertIsNone(err)
                lit = self.read()
                self.assertEqual((lit["colour"], lit["authored"], lit["reason"],
                                  lit["by"]), (colour, True, "because", "owner"))

    def test_a_projection_block_is_not_a_light(self):
        """THE LAUNDERING ARM. The merged record CARRIES the inline block (the
        merge leaves a never-migrated field standing), so a read off the record
        would answer green-authored here."""
        self.plant(status="dormant",
                   inline={"colour": "green", "reason": "forged", "by": "x", "ts": 1})
        # The premise, or the arm is about nothing: the block really is there.
        self.assertEqual(registry.load()["projects"]["alpha"]["state"]["colour"],
                         "green")
        lit = self.read()
        self.assertEqual((lit["colour"], lit["authored"]), ("dormant", False))
        # And the same block in the AUTHORED layer is a light — so the refusal
        # above is about the layer, not about the block's shape.
        self.plant(status="dormant",
                   authored={"colour": "green", "reason": "real", "by": "owner", "ts": 1})
        self.assertEqual((self.read()["colour"], self.read()["authored"]),
                         ("green", True))

    def test_the_authored_light_survives_a_save(self):  # noqa: VACUOUS_ASSERTION — the authored colour is asserted present after the save; the absent key is the projection half of the same round-trip
        """A re-sync rebuilds the projection and must not take the light with
        it — and must not promote a projection block on the way through."""
        self.plant(status="active")
        registry.state("alpha", "orange", reason="one credential left", apply=True)
        registry.save(registry.load())
        self.assertEqual(self.read()["colour"], "orange")
        self.assertNotIn("state",
                         pk.read_json(home.registry_path())["projects"]["alpha"])

    def test_clear_hands_the_light_back_and_is_not_green(self):  # noqa: VACUOUS_ASSERTION — authored is asserted TRUE before the clear, and the clear's own row names the colour it removed
        self.plant(status="dormant")
        registry.state("alpha", "red", reason="frozen", apply=True)
        self.assertTrue(self.read()["authored"])                  # the control
        row, err = registry.state("alpha", "clear", apply=True)
        self.assertIsNone(err)
        self.assertEqual(row["was"]["colour"], "red")
        lit = self.read()
        self.assertEqual((lit["colour"], lit["authored"]), ("dormant", False))
        self.assertNotIn("state",
                         pk.read_json(home.authored_path())["projects"]["alpha"])

    def test_a_dry_run_writes_nothing(self):  # noqa: VACUOUS_ASSERTION — the planned row is asserted to carry the colour, so an unchanged file cannot come from a call that did nothing
        self.plant()
        before = self._bytes(home.authored_path())
        row, err = registry.state("alpha", "red", reason="frozen")
        self.assertIsNone(err)
        self.assertEqual(row["state"]["colour"], "red")      # it planned the write
        self.assertEqual(self._bytes(home.authored_path()), before)
        self.assertFalse(self.read()["authored"])

    def test_refusals_name_their_input_and_write_nothing(self):  # noqa: VACUOUS_ASSERTION — a literal five-tuple, each arm asserting its own sentence; ProjectLightTest's setting arms prove the same call writes
        self.plant()
        before = self._bytes(home.authored_path())
        for name, colour, why, needle in (
                ("alpha", "purple", "r", "colour must be one of"),
                ("alpha", "active", "r", "colour must be one of"),
                ("alpha", "red", None, "needs a reason"),
                ("alpha", "red", "  ", "needs a reason"),
                ("nobody", "red", "r", "unknown project 'nobody'")):
            with self.subTest(name=name, colour=colour, why=why):
                row, err = registry.state(name, colour, reason=why, apply=True)
                self.assertIsNone(row)
                self.assertIn(needle, err)
        self.assertEqual(self._bytes(home.authored_path()), before)

    def test_admits_grades_each_colour_for_new_work_and_for_continuations(self):  # noqa: VACUOUS_ASSERTION — a literal table, every row asserting both answers; refusals and admissions sit side by side on one observable
        path = self.plant(status="dormant")
        inside = os.path.join(path, "src", "deep")      # longest-prefix, not equality
        self.assertEqual(registry.admits(inside), (True, None, None))   # unauthored
        for colour, new, cont in (("green", True, True), ("yellow", True, True),
                                  ("orange", True, True), ("red", False, True)):
            with self.subTest(colour=colour):
                registry.state("alpha", colour, reason="because", apply=True)
                self.assertEqual(registry.admits(inside, new_work=True)[0], new)
                self.assertEqual(registry.admits(inside, new_work=False)[0], cont)
        ok, refusal, note = registry.admits(inside)
        self.assertIn("alpha is RED", refusal)
        self.assertIn("because", refusal)
        self.assertIn("helm projects state alpha", refusal)

    @staticmethod
    def _project_wording(family_sentence):
        """A burn-flag sentence said about a PROJECT: the family clause goes,
        and nothing else may differ."""
        return (family_sentence.replace(" on this family", " here")
                .replace(", prefer another family", ""))

    def test_the_four_words_mean_what_the_burn_flags_already_mean(self):
        """One vocabulary. A second grading of the same four words is how
        ORANGE came to freeze a project whose owner meant work-but-careful.
        WHOLE SENTENCES, COMPARED FOR EQUALITY: a substring check stays green
        while either table drifts, and passes on an empty string."""
        from helm import burnflags
        for colour, flag in (("green", burnflags.GREEN), ("yellow", burnflags.YELLOW),
                             ("orange", burnflags.ORANGE), ("red", burnflags.RED)):
            with self.subTest(colour=colour):
                want = self._project_wording(burnflags.BEHAVIOUR[flag]["say"])
                self.assertTrue(want.strip(), "the normaliser emptied the sentence")
                self.assertEqual(registry.LIGHT_SAYS[colour], want)
        # and only the word whose sentence says START NOTHING refuses anything
        self.assertEqual([c for c, (new, cont) in registry.LIGHT_ADMITS.items()
                          if not (new and cont)], ["red"])
        self.assertEqual(registry.LIGHT_ADMITS["red"], (False, True))

    def test_every_home_of_the_four_words_says_the_same_sentences(self):  # noqa: VACUOUS_ASSERTION — the presence of each exact sentence in the help, the card script and the docs is asserted positively beside the absent retired phrases
        """THE VOCABULARY HAS MORE HOMES THAN THE TWO TABLES: the `burn` verb's
        help (what an AGENT reads to learn the words), the card's tooltips (what
        the OWNER reads) and docs/VERBS.md. A correction that reaches some of
        them teaches two meanings at once, and the surface left behind was the
        one agents learn from."""
        from helm import burnflags, cli
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        read = lambda *parts: open(os.path.join(root, *parts), encoding="utf-8").read()
        help_text = cli._VERB_HELP["burn"]
        card = read("helm", "web_ui", "scripts", "00-core.js.part")
        docs = read("docs", "VERBS.md")
        for flag in (burnflags.GREEN, burnflags.YELLOW, burnflags.ORANGE, burnflags.RED):
            say = burnflags.BEHAVIOUR[flag]["say"]
            with self.subTest(sentence=say):
                self.assertIn(say, help_text)
        for colour, sentence in registry.LIGHT_SAYS.items():
            with self.subTest(colour=colour):
                # THE WHOLE SENTENCE. A slice pins the half that is easy to
                # match and leaves the half that carries the meaning free to go.
                self.assertIn(sentence, card)
                self.assertIn(sentence, docs)
        # THE RETIRED PHRASES, ANYWHERE IN WHAT SHIPS. A sixth copy written
        # tomorrow fails here instead of waiting for a reader to notice.
        retired = ("fan out freely", "speculative fan-out", "winding down")
        shipped = []
        for base in ("helm", "docs"):
            for dirpath, _dirs, files in os.walk(os.path.join(root, base)):
                if "__pycache__" in dirpath:
                    continue
                shipped += [os.path.join(dirpath, f) for f in files
                            if f.endswith((".py", ".md", ".part", ".html", ".js", ".css",
                                           ".txt", ".json"))]
        self.assertGreater(len(shipped), 100, "the walk found no tree to scan")
        offenders = [(os.path.relpath(path, root), phrase) for path in shipped
                     for phrase in retired
                     if phrase in open(path, encoding="utf-8", errors="replace").read()]
        self.assertEqual(offenders, [])

    def test_green_is_never_licence_to_speculate(self):
        """The owner's own correction: green opens lanes for built-up work."""
        for sentence in (registry.LIGHT_SAYS["green"],):
            self.assertIn("never speculative", sentence)
            self.assertNotIn("fan out", sentence)

    def test_admits_is_silent_on_green_and_outside_any_project(self):  # noqa: VACUOUS_ASSERTION — the yellow read on the same path is the positive control for the note being produced at all
        path = self.plant()
        registry.state("alpha", "yellow", reason="slow down", apply=True)
        self.assertIn("slow down", registry.admits(path)[2])
        registry.state("alpha", "green", reason="go", apply=True)
        self.assertEqual(registry.admits(path), (True, None, None))
        self.assertEqual(registry.admits(self._dir("elsewhere")), (True, None, None))

    def test_admits_never_lets_a_projection_block_refuse(self):
        path = self.plant(status="active",
                          inline={"colour": "red", "reason": "forged", "by": "x", "ts": 1})
        self.assertEqual(registry.admits(path), (True, None, None))
        self.plant(status="active",
                   authored={"colour": "red", "reason": "real", "by": "owner", "ts": 1})
        self.assertFalse(registry.admits(path)[0])       # the layer, not the shape

    def test_a_malformed_authored_block_falls_back_to_the_scan(self):  # noqa: VACUOUS_ASSERTION — each shape asserts the scan's planted value positively; test_a_projection_block_is_not_a_light proves a well-formed block IS read
        """A hand-edited file is the realistic source of this. An unknown
        colour must not reach a CSS class or a router as though it were one."""
        for bad in ("red", {"colour": "mauve"}, {"reason": "no colour"}, []):
            with self.subTest(bad=bad):
                self.plant(status="active", authored=bad)
                lit = self.read()
                self.assertEqual((lit["colour"], lit["authored"]), ("active", False))


class AComputedStampAndALegalNameCannotBeSpelledAlike(RegistryBase):
    """C3 (P2). `_qualified` stamped a second-location entry with `name@<8 hex>`,
    and `@` is LEGAL inside a project name — so a key and a stamp could be
    spelled identically and every reader had to guess which it held. Recomputing
    the stamp answers for helm's own keys and cannot answer for a real name the
    owner chose to be `alpha@<the sha1 prefix of alpha's own path>`: that name
    recomputes, so enumeration published `alpha` while every lookup and every
    withdrawal record used the whole label. Two spellings of one entry is what
    let a forgotten project keep an active declaration.

    THE CURE IS THE GRAMMAR, not a smarter decoder: the computed separator is one
    `_checked_value` REFUSES inside a name, so the two spellings are disjoint by
    construction and `_logical_name` decodes only the computed one.
    """

    FIELD = "gate"
    BLOCK = {"command": ["./run-suite.sh"], "protocol": "exit"}

    def plant(self, name, path, extra=None):
        rec = {"name": name, "path": path, "kind": "git", "status": "active",
               "sessions": {}}
        pk.write_json(home.registry_path(),
                      {"version": 1, "projects": {name: rec}})
        projects = {name: {"path": path, self.FIELD: self.BLOCK}}
        projects.update(extra or {})
        pk.write_json(home.authored_path(),
                      {"version": 1, "projects": projects})

    def declarations(self):
        return [(rec["project"], rec["path"], rec["value"])
                for rec in registry.authored_declarations(self.FIELD)]

    def test_a_legal_name_spelled_like_the_old_stamp_is_decoded_as_itself(self):
        """The answer for the reviewer's third input, and WHICH answer it is: the
        name is ADMITTED and decoded as ITSELF. It cannot be refused as a name,
        because a name of the form `alpha@<8 hex digits>` is an ordinary legal
        label, and refusing it would invent a grammar rule for a shape the
        decoder reads exactly; and it cannot decode to `alpha`, because the
        decoder reads only the separator a NAME may never contain. One spelling,
        at both doors.
        """
        path = os.path.join(self.tmp, "second")
        lookalike = "alpha" + registry._LEGACY_STAMP_SEP + registry._stamp(path)
        # CONTROL ON THE INPUT: this really is the old computed spelling, and the
        # registry really does admit it as a name — the arm is not about a
        # rejected input being reported.
        self.assertEqual(lookalike, registry._legacy_qualified("alpha", path))
        registry._checked_value({"projects": {lookalike: {"name": lookalike,
                                                          "path": path}}})
        self.assertEqual(registry._logical_name(lookalike, path), lookalike)
        self.plant(lookalike, path)
        # THE TWO DOORS AGREE, which is the whole property: what enumeration
        # publishes is what a lookup finds.
        self.assertEqual(self.declarations(), [(lookalike, path, self.BLOCK)])
        self.assertEqual(
            registry.declaration_provenance(lookalike, path, self.FIELD),
            (self.BLOCK, "authored"))
        # MUST-MISS on the same reader: an ORDINARY name carrying the legal
        # separator keeps all of itself too, and a GENUINE computed stamp still
        # decodes away. BLAST RADIUS: this trio — together they admit exactly one
        # reading of each key.
        self.assertEqual(registry._logical_name("team@adopter", path),
                         "team@adopter")
        stamped = registry._qualified("alpha", path)
        self.assertEqual(registry._logical_name(stamped, path), "alpha")
        # AND A STAMP WHOSE RECORDED PATH NO LONGER HASHES TO IT KEEPS ITS WHOLE
        # SPELLING: fail-closed, matching nothing, never a shortened label.
        self.assertEqual(registry._logical_name(stamped, "/somewhere/else"),
                         stamped)

    def test_the_computed_separator_is_refused_inside_a_project_name(self):
        """The grammar itself, which is what makes the pair above disjoint rather
        than merely distinguishable. BLAST RADIUS: this arm — the positive at the
        end is the same separator in a CORRECTLY stamped key, which is admitted,
        so this is not "the character is banned everywhere".
        """
        path = os.path.join(self.tmp, "second")
        for bad in ("alpha" + registry._STAMP_SEP + "beta",
                    "alpha" + registry._STAMP_SEP + "notahex",
                    "alpha" + registry._STAMP_SEP + "c0ffee1",
                    registry._STAMP_SEP + "alpha"):
            with self.subTest(name=bad):
                with self.assertRaises(ValueError):
                    registry._checked_value(
                        {"projects": {bad: {"name": bad, "path": path}}})
        stamped = registry._qualified("alpha", path)
        self.assertIn(registry._STAMP_SEP, stamped)
        registry._checked_value({"projects": {stamped: {"name": "alpha",
                                                        "path": path}}})

    def test_a_forgotten_project_whose_name_looks_like_a_stamp_goes_inactive(self):
        """C3's withdrawal intersection, and the reason the two spellings had to
        be made disjoint rather than merely documented. `forget` archives under
        the project's WHOLE name, while enumeration published the shortened one,
        so the inactive lookup missed its own archive and the declaration stayed
        armed for a path the owner had withdrawn — the exact hole the round-four
        cure closed for every ORDINARY name and left open for this subset.
        """
        path = os.path.join(self.tmp, "gone", "alpha")        # never created
        name = "alpha" + registry._LEGACY_STAMP_SEP + registry._stamp(path)
        self.plant(name, path)
        # THE STANDING POSITIVE FIRST: the declaration is active and enumerated
        # under the name the owner actually chose.
        self.assertEqual(self.declarations(), [(name, path, self.BLOCK)])
        self.assertIsNone(registry.forget(name, apply=True)[1])
        self.assertIn(name,
                      registry._forgotten(registry._authored_load(strict=True)))
        # THE PRODUCT LAW: a withdrawn membership declares nothing. Without the
        # cure the archive was keyed `alpha@<stamp>` and the filter looked under
        # `alpha`, so this returned the declaration.
        self.assertEqual(self.declarations(), [])
        # CONTROL, SAME OBSERVABLE. BLAST RADIUS: this arm. A RESTORE is the one
        # thing that re-arms it, so the absence above is the withdrawn membership
        # and not an entry this reader can no longer see at all.
        self.assertIsNone(registry.restore(name, apply=True)[1])
        self.assertEqual(self.declarations(), [(name, path, self.BLOCK)])


class AHistoricalPathStampIsDecodedOnlyWithProvenance(RegistryBase):
    """C3 (P1). Making the two spellings disjoint cured the legal-name collision
    and broke ORDINARY BASE-ERA MEMBERSHIP. The stamping producer that shipped
    before `_STAMP_SEP` wrote `name@<stamp>` for a repointed project's own CURRENT
    location, so on any host that had simply repointed a project the authored
    entry for the binding's current path was keyed in a spelling no lookup here
    could form: `_apply_bindings` raised "no matching authored path stamp" for an
    intact binding, every strict load raised with it, and `project_state` answers
    UNKNOWN for a strict-load failure — the whole registry unavailable on a host
    that had done nothing wrong.

    THE DECODE IS PROVENANCE AND NEVER RECOMPUTATION, which is what keeps the
    legal-name cure. An owner may legally name a project
    `alpha@<the 8 hex digits sha1 of alpha's own path>`; that name recomputes, so
    a recomputing decoder shortens it and publishes one spelling while every
    lookup and every withdrawal record keeps the other. So a historical key is
    decoded only where the authored file's OWN records — a departed-location
    archive or a repoint archive — show that producer had that (name, location) to
    stamp, and a full spelling with no such record stays a whole NAME.

    THE RE-SPELLING IS A WRITE DOOR. Readers answer correctly on the bytes as
    they stand, so nothing depends on the migration; `save` converges the spelling
    where the owner's authored file is already being rewritten, and no read
    mutates the unrebuildable layer.
    """

    FIELD = "gate"
    BLOCK = {"command": ["./run-suite.sh"], "protocol": "exit"}

    def _base_era_key(self, name, path):
        """The key the base writer's `_qualified` computes, verbatim from `git
        show de8487fb92b1:helm/registry.py`:

            return "%s@%s" % (name, hashlib.sha1((path or "").encode())
                                          .hexdigest()[:8])

        `_legacy_qualified` is this tree's own spelling of the same string, kept
        for the doors that must recognise a historical stamp. The equality here is
        the CONTROL that they are one value, so no key below is hand-typed and no
        fixture invents an input the shipped producer would not have written.
        """
        import hashlib
        want = "%s@%s" % (name,
                          hashlib.sha1((path or "").encode()).hexdigest()[:8])
        self.assertEqual(registry._legacy_qualified(name, path), want)
        return want

    def _base_era_repoints(self):
        """alpha repointed P0 -> P1 -> P2 by the SHIPPED membership writer, with
        every stamped authored key re-spelled in the grammar that era used.

        The binding, its transition chain, its per-location records, its
        generations and the projection are all `repoint`'s own output. The ONE
        reconstruction is the key SPELLING, and `_base_era_key` proves each is
        byte-identical to what that era's `_qualified` produced for the same
        (name, path) — the single character the grammar change moved.
        """
        p0 = os.path.join(self.tmp, "p0", "alpha")        # never created
        p1 = self._dir("p1", "alpha")
        p2 = self._dir("p2", "alpha")
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "alpha": {"name": "alpha", "path": p0, "kind": "git",
                      "status": "active", "sessions": {}}}})
        pk.write_json(home.authored_path(), {"version": 1, "projects": {
            "alpha": {"path": p0, self.FIELD: self.BLOCK}}})
        self.assertIsNone(registry.repoint("alpha", p0, p1, apply=True)[1])
        os.rmdir(p1)                  # P1 must be gone to repoint away from it
        self.assertIsNone(registry.repoint("alpha", p1, p2, apply=True)[1])
        auth = pk.read_json(home.authored_path())
        entries = auth["projects"]
        for key in sorted(entries):
            head = registry._stamped_head(key)
            if head is None:
                continue
            entry = entries.pop(key)
            entries[self._base_era_key(head, entry["path"])] = entry
        pk.write_json(home.authored_path(), auth)
        return p0, p1, p2

    def declarations(self):
        return [(rec["project"], rec["path"])
                for rec in registry.authored_declarations(self.FIELD)]

    def test_a_base_era_binding_applies_and_the_current_command_is_selected(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone is one half of a discriminating PAIR whose other half is the unconditional assertEqual on the SAME call one line below (the same lookup, handed the provenance table, returns the block), and three further unconditional positives follow on the same world: the loaded current path, the resolved plan argv, and the enumerated declaration
        """The availability half: an ordinary base-era repoint chain loads, its
        binding applies, the CURRENT location's declaration is selected and the
        departed one declares nothing. No gate field is involved in the breakage —
        this is `_apply_bindings` refusing a binding whose authored path stamp is
        spelled the way the writer that made it spelled things.
        """
        from helm import foldcompose, gate
        p0, p1, p2 = self._base_era_repoints()
        entries = pk.read_json(home.authored_path())["projects"]
        # CONTROL ON THE INPUT: the ONLY authored entry for the binding's current
        # location wears the historical spelling, which is the whole reason the
        # lookup found nothing for it.
        self.assertNotIn(registry._qualified("alpha", p2), entries)
        self.assertIn(self._base_era_key("alpha", p2), entries)
        # AND THE DISCRIMINATING PAIR, on the cured resolver itself. BLAST RADIUS:
        # these two calls. Asked WITHOUT the provenance table — the resolver as it
        # stood — the current binding's authored entry is invisible; asked with it,
        # the same call returns the same bytes.
        auth = registry._authored_load(strict=True)
        self.assertIsNone(registry._authored_for(entries, "alpha", p2))
        self.assertEqual(
            registry._authored_for(entries, "alpha", p2,
                                   registry._proven_legacy_stamps(auth))[self.FIELD],
            self.BLOCK)
        # THE BINDING APPLIES AND THE REGISTRY IS AVAILABLE AGAIN.
        reg = registry.load(strict=True)
        self.assertEqual(reg["projects"]["alpha"]["path"], p2)
        self.assertEqual(foldcompose.project_state(p2), ("registered", "alpha"))
        # THE CURRENT COMMAND IS SELECTED, at the door that spawns it.
        plan, err = gate.suite_command(p2)
        self.assertIsNone(err, err)
        self.assertEqual(plan["argv"], self.BLOCK["command"])
        # AND THE DEPARTED LOCATIONS DECLARE NOTHING: one record, for P2 only.
        self.assertEqual(self.declarations(), [("alpha", p2)])

    def test_a_departed_location_that_reappears_is_handed_no_command(self):  # noqa: VACUOUS_ASSERTION — the standing positive on the same reader runs FIRST (the CURRENT location resolves to the declared argv) and the CONTROL runs LAST on the same observable (a repoint undo makes this very location resolve again); the empty answer between them is the product law the arm exists to state
        """The other direction of the same decode. P1 is a location alpha has
        LEFT, and whatever reappears there may be an unrelated repository — so it
        gets no command, while the current location keeps its own. Without the
        strict load working at all, `_root_declaration` read the authored layer
        alone and exact-matched the retained P1 entry, which is a stale command
        selected for a tree nothing registered.
        """
        from helm import gate
        p0, p1, p2 = self._base_era_repoints()
        os.makedirs(p1)                 # something else lives here now
        # THE STANDING POSITIVE FIRST, on the same reader: the CURRENT location
        # does declare, so an empty answer for P1 is about P1.
        self.assertEqual(gate.suite_command(p2)[0]["argv"], self.BLOCK["command"])
        plan, err = gate.suite_command(p1)
        self.assertIsNone(plan, "a departed location was handed the old command")
        self.assertIn(gate.DECLARED_GATE_FIELD, err)
        self.assertNotIn("./run-suite.sh", err)
        # CONTROL, SAME OBSERVABLE. BLAST RADIUS: this arm. An UNDO makes P1 the
        # current location again and the command comes back, so the absence above
        # is the withdrawn membership and not an entry this reader cannot see.
        self.assertIsNone(registry.repoint("alpha", p2, undo=True, apply=True)[1])
        self.assertEqual(gate.suite_command(p1)[0]["argv"], self.BLOCK["command"])

    def test_a_full_at_spelling_with_no_provenance_stays_a_whole_name(self):
        """The legal-name pole, held beside the real old-producer output above. The
        SAME key is a declaring NAME when membership holds it whole and declares
        NOTHING when nothing does — and neither answer is `alpha`, because the
        decoder has no record to read it from. Enumerating it under its own full
        spelling with no membership published a `project` no withdrawal verb can
        reach, which is why the producer refuses it rather than a test asserting it.
        """
        path = self._dir("legit", "alpha")
        lookalike = self._base_era_key("alpha", path)
        # CONTROL ON THE INPUT: the registry admits this as a NAME, so the arm is
        # not about a rejected input being reported.
        registry._checked_value({"projects": {lookalike: {"name": lookalike,
                                                          "path": path}}})
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            lookalike: {"name": lookalike, "path": path, "kind": "git",
                        "status": "active", "sessions": {}}}})
        pk.write_json(home.authored_path(), {"version": 1, "projects": {
            lookalike: {"path": path, self.FIELD: self.BLOCK}}})
        # THE POSITIVE: registered under the WHOLE spelling, it declares, whole.
        self.assertEqual(self.declarations(), [(lookalike, path)])
        self.assertEqual(
            registry.declaration_provenance(lookalike, path, self.FIELD),
            (self.BLOCK, "authored"))
        # THE MUST-MISS, on ONE otherwise identical change: membership under
        # `alpha` instead of the whole spelling, everything else byte for byte.
        # Nothing decodes the key and nothing is registered under it, so it
        # declares nothing. BLAST RADIUS: this pair.
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "alpha": {"name": "alpha", "path": path, "kind": "git",
                      "status": "active", "sessions": {}}}})
        self.assertEqual(self.declarations(), [])
        # AND IT IS STILL A WHOLE NAME rather than a stamp for `alpha`: `alpha`
        # gets nothing either, at the lookup door as well as the enumerating one.
        self.assertEqual(registry._logical_name(lookalike, path), lookalike)
        self.assertEqual(registry.declaration_provenance("alpha", path,
                                                         self.FIELD),
                         (None, None))

    def test_a_save_re_spells_a_proven_historical_stamp_and_a_read_does_not(self):  # noqa: VACUOUS_ASSERTION — each absence is paired in the same breath with an unconditional positive on the same bytes: the historical key is gone AND the computed key holds the block and the path, the projection lacks the executable field AND keeps its own path and name, and the arm closes on two positive equalities (the plan still resolves, the declaration still enumerates)
        """The migration, and WHERE it lives. A read answers correctly on the
        bytes as they stand, so migrating from one would make `load` a writer of
        the unrebuildable layer for no gain; `save` is already rewriting that file.
        The declaration's bytes and the projection's identity survive the
        re-spelling, which is the whole point of doing it in the split writer.
        """
        from helm import gate
        p0, p1, p2 = self._base_era_repoints()
        legacy = self._base_era_key("alpha", p2)
        fresh = registry._qualified("alpha", p2)
        # CONTROL ON THE INPUT.
        self.assertIn(legacy, pk.read_json(home.authored_path())["projects"])
        reg = registry.load()
        self.assertIn(legacy, pk.read_json(home.authored_path())["projects"],
                      "an ordinary READ migrated the authored layer")
        registry.save(reg)
        after = pk.read_json(home.authored_path())["projects"]
        self.assertNotIn(legacy, after)
        self.assertEqual(after[fresh][self.FIELD], self.BLOCK)
        self.assertEqual(after[fresh]["path"], p2)
        # THE PROJECTION KEEPS ITS OWN BYTES: a re-spelling of an authored key is
        # not a projection edit, and the binding still applies over it.
        projected = pk.read_json(home.registry_path())["projects"]["alpha"]
        self.assertEqual(projected["path"], p2)
        self.assertEqual(projected["name"], "alpha")
        self.assertNotIn(self.FIELD, projected)
        self.assertEqual(registry.load(strict=True)["projects"]["alpha"]["path"],
                         p2)
        # AND THE COMMAND STILL RESOLVES AFTER THE MIGRATION, which is the
        # observable the re-spelling must not cost. BLAST RADIUS: this pair with
        # the assertion above — together they state that the key moved and the
        # answer did not.
        self.assertEqual(gate.suite_command(p2)[0]["argv"], self.BLOCK["command"])
        self.assertEqual(self.declarations(), [("alpha", p2)])


    # ------------------------------------- the round-eight writer arms

    def test_a_base_era_project_carries_its_gate_to_a_never_used_location(self):  # noqa: VACUOUS_ASSERTION — the one empty observable (the census after `forget`) is bracketed by unconditional positives on the SAME reader: the declaration is enumerated at P3 immediately before the withdrawal and again immediately after the restore, so the emptiness is the withdrawn membership and not a reader that stopped seeing this entry
        """F2 (P2). The READERS were handed the provenance table and the WRITERS
        were not, so `_authored_for` answered two different things about one
        authored file depending on which door asked. On a host that had ever
        repointed a project, the CURRENT location's entry wears the historical
        `name@<stamp>` spelling — visible to `_apply_bindings` and to
        `declaration_provenance`, invisible to `repoint`, `forget` and `restore`.
        Repointing such a project to a path it has never occupied therefore found
        no departed entry, carried no NEVER-MIGRATED field, and DROPPED the
        owner's active `gate` declaration: the merged keep excludes it, the
        departed lookup misses the old key, and the fresh slash entry has none.
        Returning to a previously used path masked it through the old slot; a
        never-used one does not.
        """
        from helm import gate
        p0, p1, p2 = self._base_era_repoints()
        p3 = self._dir("p3", "alpha")
        auth = registry._authored_load(strict=True)
        entries = auth["projects"]
        # CONTROL ON THE INPUT, on the lookup the writers omitted. BLAST RADIUS:
        # this pair. Asked as the writers asked, the source entry does not exist;
        # asked as the readers ask, it is right there carrying the declaration —
        # which is exactly the gate the repoint below must not lose.
        self.assertIsNone(registry._authored_for(entries, "alpha", p2))
        self.assertEqual(
            registry._authored_for(entries, "alpha", p2,
                                   registry._proven_legacy_stamps(auth))[self.FIELD],
            self.BLOCK)
        # THE STANDING POSITIVE FIRST: P2 is the current location and it declares.
        self.assertEqual(gate.suite_command(p2)[0]["argv"], self.BLOCK["command"])
        os.rmdir(p2)                  # P2 must be gone to repoint away from it
        self.assertIsNone(registry.repoint("alpha", p2, p3, apply=True)[1])
        # THE PRODUCT LAW: the declaration and its PROVENANCE moved with the
        # project. Authored, not projection — a `gate` that arrived as a
        # projection would be reported, never spawned.
        self.assertEqual(registry.load(strict=True)["projects"]["alpha"]["path"],
                         p3)
        self.assertEqual(
            registry.declaration_provenance("alpha", p3, self.FIELD),
            (self.BLOCK, "authored"))
        self.assertEqual(gate.suite_command(p3)[0]["argv"], self.BLOCK["command"])
        self.assertEqual(self.declarations(), [("alpha", p3)])
        # AND IT SURVIVES A WITHDRAWAL ROUND TRIP, which is the same omission in
        # `forget` and `restore`: without the table the archive is written with
        # no `authored_fields` mark, and `restore` reads that absence as
        # "provenance unknown" and demotes the owner's declaration to the
        # projection. The empty census between them is the withdrawal itself.
        os.rmdir(p3)
        self.assertIsNone(registry.forget("alpha", apply=True)[1])
        self.assertEqual(self.declarations(), [])
        self.assertIsNone(registry.restore("alpha", apply=True)[1])
        self.assertEqual(
            registry.declaration_provenance("alpha", p3, self.FIELD),
            (self.BLOCK, "authored"))
        self.assertEqual(self.declarations(), [("alpha", p3)])

    def test_a_historical_spelling_with_no_provenance_refuses_the_repoint(self):
        """F2's constraint, and the reason the writers were given the TABLE and
        not a recomputation. An owner may legally name a project
        `alpha@<the 8 hex digits sha1 of alpha's own path>`; recomputing the
        stamp shortens that name and publishes one spelling while every lookup
        keeps the other, which is the collision the disjoint grammar cured. So a
        key that WEARS the old grammar for this (name, location) and that the
        authored file's own records cannot account for is not this project's
        entry, and a writer that moved it anyway would be recomputing the stamp
        under another word. It refuses, and says which key and which location.
        """
        src = os.path.join(self.tmp, "src", "alpha")      # never created
        dst = self._dir("dst", "alpha")
        lookalike = self._base_era_key("alpha", src)
        # CONTROL ON THE INPUT: the registry admits this key, and nothing in the
        # authored file records the stamping that would decode it — so it is a
        # whole NAME to every reader, which is what makes moving it a recompute.
        registry._checked_value({"projects": {lookalike: {"name": lookalike,
                                                          "path": src}}})
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "alpha": {"name": "alpha", "path": src, "kind": "git",
                      "status": "active", "sessions": {}}}})
        pk.write_json(home.authored_path(), {"version": 1, "projects": {
            "alpha": {"path": src, self.FIELD: self.BLOCK},
            lookalike: {"path": src, self.FIELD: self.BLOCK}}})
        auth = registry._authored_load(strict=True)
        self.assertNotIn(lookalike, registry._proven_legacy_stamps(auth))
        row, err = registry.repoint("alpha", src, dst, apply=True)
        self.assertIsNone(row)
        self.assertIn(lookalike, err)
        self.assertIn("recomputing", err)
        # AND NOTHING WAS WRITTEN: the refusal is a refusal, not a partial move.
        self.assertEqual(registry.load(strict=True)["projects"]["alpha"]["path"],
                         src)
        # CONTROL, SAME OBSERVABLE, ONE CHANGE. BLAST RADIUS: this pair. With the
        # unaccountable key gone and everything else byte for byte, the identical
        # repoint succeeds and carries the declaration — so the refusal above is
        # that key and not a repoint this fixture could never have made.
        entries = pk.read_json(home.authored_path())
        entries["projects"].pop(lookalike)
        pk.write_json(home.authored_path(), entries)
        self.assertIsNone(registry.repoint("alpha", src, dst, apply=True)[1])
        self.assertEqual(
            registry.declaration_provenance("alpha", dst, self.FIELD),
            (self.BLOCK, "authored"))


    # ------------------------------------- the round-nine object arms

    def _lawful_lookalikes(self, src, dst):
        """Two UNRELATED projects whose lawful whole names are spelled exactly
        like alpha's historical stamp for `src` and for `dst`, each registered
        and declaring at its OWN location. `_base_era_key` is the control that
        each spelling is byte-identical to what the stamping producer would
        have written for (alpha, that location) — the shape the writer's old
        refusal keyed on."""
        q = self._dir("q", "lookalike-src")
        q2 = self._dir("q2", "lookalike-dst")
        for_src = self._base_era_key("alpha", src)
        for_dst = self._base_era_key("alpha", dst)
        other = {"command": ["./other.sh"], "protocol": "exit"}
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "alpha": {"name": "alpha", "path": src, "kind": "git",
                      "status": "active", "sessions": {}},
            for_src: {"name": for_src, "path": q, "kind": "git",
                      "status": "active", "sessions": {}},
            for_dst: {"name": for_dst, "path": q2, "kind": "git",
                      "status": "active", "sessions": {}}}})
        pk.write_json(home.authored_path(), {"version": 1, "projects": {
            "alpha": {"path": src, self.FIELD: self.BLOCK},
            for_src: {"path": q, self.FIELD: other},
            for_dst: {"path": q2, self.FIELD: other}}})
        return (for_src, q), (for_dst, q2), other

    def test_a_lawful_name_spelled_like_a_stamp_elsewhere_never_vetoes_a_move(self):
        """Round nine (A). The round-eight refusal keyed on the NAME GRAMMAR:
        any entry spelled `alpha@<stamp of the source or target>` that the file
        could not account for refused alpha's repoint — including an unrelated
        project whose lawful whole name happens to be that spelling, registered
        and declaring at its own location Q. A historical stamp key is a LOOKUP
        DOMAIN that maps to a record only through the recorded provenance of THAT
        record, never an identity of its own, so a lawful name at any other
        location can neither veto nor be captured by another project's move.
        The refusal stays for THIS project's own unaccountable record — an entry
        of that spelling recorded AT the move's own location — which is the
        control at the end.
        """
        from helm import gate
        src = os.path.join(self.tmp, "src", "alpha")       # never created
        dst = self._dir("dst", "alpha")
        (for_src, q), (for_dst, q2), other = self._lawful_lookalikes(src, dst)
        auth = registry._authored_load(strict=True)
        # CONTROL ON THE INPUT: nothing in the file accounts for either spelling
        # as alpha's stamp, and the writer's old refusal (`_legacy_qualified`
        # present, unproven) would have fired on both — so a success below is
        # the cure and not a fixture the old writer would also have passed.
        proven = registry._proven_legacy_stamps(auth)
        for key, where in ((for_src, src), (for_dst, dst)):
            self.assertEqual(key, registry._legacy_qualified("alpha", where))
            self.assertNotIn(key, proven)
            self.assertIn(key, auth["projects"])
        # THE STANDING POSITIVE FIRST: every one of the three declares, whole,
        # at its own location, before anything moves.
        self.assertEqual(sorted(self.declarations()),
                         sorted([("alpha", src), (for_dst, q2), (for_src, q)]))
        row, err = registry.repoint("alpha", src, dst, apply=True)
        self.assertIsNone(err, err)
        self.assertEqual(row["to"], dst)
        # THE PRODUCT LAW: alpha moved and kept its declaration AND its
        # provenance; the lookalikes kept theirs, whole, and were not captured
        # as alpha's departed record.
        self.assertEqual(registry.load(strict=True)["projects"]["alpha"]["path"],
                         dst)
        self.assertEqual(
            registry.declaration_provenance("alpha", dst, self.FIELD),
            (self.BLOCK, "authored"))
        self.assertEqual(gate.suite_command(dst)[0]["argv"],
                         self.BLOCK["command"])
        self.assertEqual(
            registry.declaration_provenance(for_src, q, self.FIELD),
            (other, "authored"))
        self.assertEqual(
            registry.declaration_provenance(for_dst, q2, self.FIELD),
            (other, "authored"))
        self.assertEqual(sorted(self.declarations()),
                         sorted([("alpha", dst), (for_dst, q2), (for_src, q)]))
        # NOT CAPTURED, at the resolver: now that alpha's binding proves
        # (alpha, src), the lookalike's spelling is in the provenance table —
        # and it is STILL not alpha's record at src, because the entry filed
        # under it records Q. BLAST RADIUS: this pair — the table says the key
        # decodes, the resolver says the record is not there.
        auth = registry._authored_load(strict=True)
        self.assertEqual(registry._proven_legacy_stamps(auth).get(for_src),
                         ("alpha", src))
        self.assertEqual(registry.authority_record(auth, "alpha", src)[0],
                         "alpha")
        self.assertEqual(registry.authority_record(auth, for_src, q)[0],
                         for_src)
        # CONTROL, SAME OBSERVABLE, ONE CHANGE. BLAST RADIUS: this arm. The SAME
        # spelling recorded AT the move's own source is this project's
        # unaccountable record, and the identical repoint refuses naming it — so
        # the successes above are the location the record carries and not a
        # writer that stopped refusing anything.
        src2 = os.path.join(self.tmp, "src2", "alpha")     # never created
        dst2 = self._dir("dst2", "alpha")
        own = self._base_era_key("alpha", src2)
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "alpha": {"name": "alpha", "path": src2, "kind": "git",
                      "status": "active", "sessions": {}}}})
        pk.write_json(home.authored_path(), {"version": 1, "projects": {
            "alpha": {"path": src2, self.FIELD: self.BLOCK},
            own: {"path": src2, self.FIELD: self.BLOCK}}})
        self.assertEqual(
            registry._unaccountable_stamp(registry._authored_load(strict=True),
                                          "alpha", src2), own)
        row, err = registry.repoint("alpha", src2, dst2, apply=True)
        self.assertIsNone(row)
        self.assertIn(own, err)
        self.assertIn("recomputing", err)
        self.assertEqual(registry.load(strict=True)["projects"]["alpha"]["path"],
                         src2)

    ANCESTOR_STAMPING_PRODUCER = "679cce8c7ea1587554d948986f4f5baf96d751a1"
    ROUND_EIGHT_PARENT = "eed8ef1420e5571242caa740cfa2a15ae91f85d3"
    ROUND_NINE_RESOLVER = "2aaaef4a039a5ec7df6951ca25824c7743b0a8ff"

    PRODUCER_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "fixtures", "registry_producers")
    PRODUCER_MARK = (b"# ---8<--- frozen body begins; every byte below is "
                     b"git show output\n")

    @classmethod
    def frozen_producer(cls, sha):
        """(declared commit, declared sha256, body bytes) of one frozen source.

        THE SOURCE IS A FILE IN THE TREE, NOT AN OBJECT IN THE HISTORY, and
        that is the whole contract. A producer fetched with `git show
        <pin>:helm/registry.py` is only available where the pin is: a
        shallow clone or a `git archive` holds HEAD and no other object, so
        on such a checkout the fetch fails before the property under test is
        reached. Two of the three pins are
        this lane's own unlanded commits and are on no trunk history at any
        depth, so no fetch depth reaches them either. Frozen bytes make the
        fixture self-contained; the header's digest, recomputed on every load
        here and asserted fixture-by-fixture in
        `test_the_frozen_producers_are_the_commits_their_headers_name`, is
        what keeps the provenance auditable — a hand-edited body cannot pass.

        `.py.txt`, under tests/fixtures/, is the house convention for frozen
        source (tests/fixtures/dispatch_apply_42b373c.py.txt is the precedent)
        and it is also what keeps these bytes out of every scanner that walks
        the tree for LIVE code: world_prose_guard._source_kind selects by
        extension and returns None for anything but .py/.js/.part, and
        seatname_guard scans .py and .json and reads everything else as
        out-of-scope narration. docref_guard is scoped to helm/ (POLICED) and
        hardcode.py exempts tests/ outright, so neither ever saw these paths.
        """
        path = os.path.join(cls.PRODUCER_ROOT, "registry-%s.py.txt" % sha)
        with open(path, "rb") as handle:
            raw = handle.read()
        head, mark, body = raw.partition(cls.PRODUCER_MARK)
        if not mark:
            raise AssertionError("%s carries no frozen-body marker" % path)
        fields = {}
        for line in head.decode("utf-8").splitlines():
            key, _, value = line.lstrip("#").partition(":")
            fields.setdefault(key.strip(), value.strip())
        return fields.get("commit"), fields.get("sha256"), body

    def _producer(self, sha, alias):
        """This module as a NAMED COMMIT shipped it, importable beside the tree's.

        Loaded under its own name INSIDE the `helm` package, so its relative
        imports resolve to the one `home`/`pk` this test's HELM_HOME is set for
        and two producer versions can write one store in one process. The pair
        this arm is about is written by TWO versions in sequence — a single
        module for every step does not produce it — so the fixture cannot be a
        hand-written file and cannot be one module's output either.

        The bytes come from the frozen fixture, whose commit and digest are
        checked HERE, on every load, so no arm can drive a body that is not the
        commit it names. See `frozen_producer` for why the history is gone.
        """
        commit, digest, source = self.frozen_producer(sha)
        self.assertEqual(commit, sha)
        self.assertEqual(hashlib.sha256(source).hexdigest(), digest)
        path = os.path.join(self._dir("producers"), alias + ".py")
        with open(path, "wb") as handle:
            handle.write(source)
        name = "helm." + alias
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        spec.loader.exec_module(module)
        return module

    # The verb each arm actually DRIVES on that producer. A digest proves the
    # bytes did not change; this proves they are a registry module and the
    # right one — a truncated or substituted body has no such definition.
    PRODUCER_VERB = {ANCESTOR_STAMPING_PRODUCER: "\ndef repoint(",
                     ROUND_EIGHT_PARENT: "\ndef repoint(",
                     ROUND_NINE_RESOLVER: "\ndef authority_record("}

    def test_the_frozen_producers_are_the_commits_their_headers_name(self):  # noqa: VACUOUS_ASSERTION — the one `assertNotEqual` is the second half of an UNCONDITIONAL pair at the end of the method: the line above it asserts the real body's sha256 EQUALS the header's declared digest on the same fixture, outside every loop, and the flipped-byte line asserts the same recomputation stops matching
        """PROVENANCE OF THE FROZEN SOURCE. The three producers the upgrade
        arms load are files in this tree, not objects in the history, because
        the shipped CI checks out at depth 1 and two of the three pins are
        this lane's own unlanded commits. A file can be edited where a commit
        cannot, so each fixture's header names its commit and a sha256 over
        the body below the marker, and this recomputes that digest from the
        bytes on disk. A hand-edited producer is caught HERE, before any
        upgrade arm can pass on bytes no version ever shipped.

        CONTROL, ON THE REAL BYTES, ONE CHANGE. The real body is taken, one
        byte is flipped, and the recomputation NO LONGER matches the declared
        digest — so the equality above is the file being intact and not a
        comparison that agrees with anything. BLAST RADIUS: this arm and the
        two upgrade arms that load these three files; nothing else in the
        suite reads tests/fixtures/registry_producers/.
        """
        pins = (self.ANCESTOR_STAMPING_PRODUCER, self.ROUND_EIGHT_PARENT,
                self.ROUND_NINE_RESOLVER)
        digests = []
        for sha in pins:
            commit, digest, body = self.frozen_producer(sha)
            # The NAME on disk, the commit the header CLAIMS, and the bytes
            # are one chain; break any link and the fixture stops being the
            # commit it is cited as.
            self.assertEqual(commit, sha)
            self.assertEqual(hashlib.sha256(body).hexdigest(), digest)
            self.assertIn(self.PRODUCER_VERB[sha], body.decode("utf-8"))
            digests.append(digest)
        # THREE DISTINCT PRODUCERS. The cure for the absent history was not
        # allowed to be one version standing in for three; no two of the
        # frozen bodies are the same bytes, so the upgrade sequence is still
        # written by the three writers it names.
        self.assertEqual(len(set(digests)), 3)
        # THE CONTROL, UNCONDITIONAL AND ON THE SAME OBSERVABLE. The real
        # body's digest matches the header, and the SAME body with one byte
        # changed does not — so the equalities above are the files being
        # intact and not a comparison that agrees with anything.
        _, declared, body = self.frozen_producer(self.ROUND_NINE_RESOLVER)
        self.assertEqual(hashlib.sha256(body).hexdigest(), declared)
        flipped = bytearray(body)
        flipped[-1] ^= 0x01
        self.assertNotEqual(hashlib.sha256(bytes(flipped)).hexdigest(),
                            declared)

    def _two_producer_pair(self):
        """One record for (alpha, P1) filed under BOTH spellings, published by
        two SHIPPED writers in the order a host upgrades through.

        The stamping ancestor moves alpha P0 -> P1 -> P2, writing `name@<stamp>`
        for each location it leaves and preserving those entries. P1 is then a
        repository again, and the round-eight parent undoes the last move: it
        selects P1, finds the plain name still occupied by P0's record, and so
        chooses the COMPUTED `alpha/<stamp of P1>` — publishing it beside the
        historical `alpha@<stamp of P1>` the ancestor had already written for
        the same location, with the same command and the same provenance.
        -> (parent module, P1, computed key, historical key).
        """
        old = self._producer(self.ANCESTOR_STAMPING_PRODUCER, "_r_stamping")
        parent = self._producer(self.ROUND_EIGHT_PARENT, "_r_parent")
        p0 = os.path.join(self.tmp, "pair0", "alpha")     # never created
        p1 = self._repo("pair1", "alpha")
        p2 = self._repo("pair2", "alpha")
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "alpha": {"name": "alpha", "path": p0, "kind": "git",
                      "status": "active", "sessions": {}}}})
        pk.write_json(home.authored_path(), {"version": 1, "projects": {
            "alpha": {"path": p0, self.FIELD: self.BLOCK}}})
        self.assertIsNone(old.repoint("alpha", p0, p1, apply=True)[1])
        shutil.rmtree(p1)             # P1 must be gone to repoint away from it
        self.assertIsNone(old.repoint("alpha", p1, p2, apply=True)[1])
        self._repo("pair1", "alpha")  # and a repository again to come back to
        self.assertIsNone(parent.repoint("alpha", p2, undo=True, apply=True)[1])
        return (parent, p1, registry._qualified("alpha", p1),
                self._base_era_key("alpha", p1))

    def test_one_record_under_two_spellings_survives_the_upgrade(self):  # noqa: VACUOUS_ASSERTION — every absent observable sits beside an unconditional positive on the same one: the historical key is gone from the saved layer AND the computed key is asserted to hold the block, the recorded path, the binding's current location and a resolvable plan; each `assertIsNone(err)` is the refusal half of a call whose `plan["argv"]` is asserted in the same breath; and the two refusals are `assertRaises` whose sentences are asserted to NAME both keys
        """R1 (P2). The UPGRADE. Two shipped producers between them publish one
        record under two keys for one location — the stamping era's
        `alpha@<stamp>` and a later undo's computed `alpha/<stamp>`, same
        command, same provenance — and the resolver that refused ANY two keys
        for a location refused those bytes: the strict load raised, so
        `project_state` answered UNKNOWN, the gate refused the suite and `save`
        reached the same refusal. Nothing was wrong with the file; both writers
        were lawful and neither could see the other's spelling.

        So the law is about CONTENT: two keys saying the same thing are one
        record and resolve; two keys saying different things are still refused,
        naming both. And the collapse is a WRITE — `save` folds the duplicate
        into the one slot, a read leaves the owner's bytes exactly as found.
        """
        from helm import foldcompose, gate
        parent, p1, computed, legacy = self._two_producer_pair()
        frozen = self._bytes(home.authored_path())
        entries = pk.read_json(home.authored_path())["projects"]
        # (a) THE PAIR IS REALLY THERE, on the frozen published bytes and before
        # any successor read: both spellings, one identical record, the declared
        # command in it.
        self.assertIn(computed, entries)
        self.assertIn(legacy, entries)
        self.assertEqual(entries[computed], entries[legacy])
        self.assertEqual(entries[computed][self.FIELD], self.BLOCK)
        self.assertEqual(entries[computed]["path"], p1)
        auth = registry._authored_load(strict=True)
        self.assertEqual(
            registry._authority_keys(auth["projects"], "alpha", p1,
                                     registry._proven_legacy_stamps(auth)),
            [computed, legacy])
        # CONTROL, AND IT IS THE REGRESSION ITSELF. BLAST RADIUS: this arm. The
        # round-nine resolver, loaded from its own commit, is handed these exact
        # bytes and REFUSES them — so every positive below is this cure and not
        # a fixture the code under review would also have passed.
        round_nine = self._producer(self.ROUND_NINE_RESOLVER, "_r_round_nine")
        with self.assertRaises(ValueError) as refused:
            round_nine.authority_record(
                round_nine._authored_load(strict=True), "alpha", p1)
        self.assertIn(computed, str(refused.exception))
        self.assertIn(legacy, str(refused.exception))
        # (b) AND THE PRODUCER THAT WROTE THEM READS THEM, which is what makes
        # this an upgrade regression: the parent resolves alpha at P1 with the
        # declared command, on the same bytes, and does not touch them.
        self.assertEqual(parent.declaration_provenance("alpha", p1, self.FIELD),
                         (self.BLOCK, "authored"))
        self.assertEqual(parent.load(strict=True)["projects"]["alpha"]["path"],
                         p1)
        self.assertEqual(self._bytes(home.authored_path()), frozen)
        # THE SUCCESSOR READS THE SAME BYTES: available, resolved, spawnable.
        self.assertEqual(
            registry.authority_record(registry._authored_load(strict=True),
                                      "alpha", p1)[0], computed)
        self.assertEqual(registry.declaration_provenance("alpha", p1, self.FIELD),
                         (self.BLOCK, "authored"))
        plan, err = gate.suite_command(p1)
        self.assertIsNone(err, err)
        self.assertEqual(plan["argv"], self.BLOCK["command"])
        self.assertEqual(registry.load(strict=True)["projects"]["alpha"]["path"],
                         p1)
        self.assertEqual(foldcompose.project_state(p1), ("registered", "alpha"))
        self.assertEqual(self.declarations(), [("alpha", p1)])
        # AND THE READ MUTATED NOTHING: the unrebuildable layer has one writer.
        self.assertEqual(self._bytes(home.authored_path()), frozen)
        # THE WRITE IS WHERE THE PAIR COLLAPSES: one key — the one the slot rule
        # chooses — carrying the declaration, with the binding, the current
        # location and the provenance intact.
        registry.save(registry.load())
        after = pk.read_json(home.authored_path())
        self.assertIn(computed, after["projects"])
        self.assertNotIn(legacy, after["projects"])
        self.assertEqual(after["projects"][computed][self.FIELD], self.BLOCK)
        self.assertEqual(after["projects"][computed]["path"], p1)
        self.assertEqual(after["project_bindings"]["alpha"]["path"], p1)
        self.assertEqual(registry.declaration_provenance("alpha", p1, self.FIELD),
                         (self.BLOCK, "authored"))
        self.assertEqual(gate.suite_command(p1)[0]["argv"], self.BLOCK["command"])
        self.assertEqual(registry.load(strict=True)["projects"]["alpha"]["path"],
                         p1)
        self.assertEqual(self.declarations(), [("alpha", p1)])
        # CONTROL, SAME BYTES, ONE CHANGE. BLAST RADIUS: this arm. The historical
        # duplicate of the SAME published file now holds a DIFFERENT command:
        # that is a disagreement about authority, not a spelling, and it still
        # refuses naming both keys — so the resolution above is the records being
        # equal and not a resolver that stopped refusing anything.
        with open(home.authored_path(), "wb") as handle:
            handle.write(frozen)
        hand = pk.read_json(home.authored_path())
        hand["projects"][legacy] = {"path": p1, self.FIELD: {
            "command": ["./other.sh"], "protocol": "exit"}}
        pk.write_json(home.authored_path(), hand)
        with self.assertRaises(ValueError) as caught:
            registry.authority_record(registry._authored_load(strict=True),
                                      "alpha", p1)
        self.assertIn(computed, str(caught.exception))
        self.assertIn(legacy, str(caught.exception))
        plan, err = gate.suite_command(p1)
        self.assertIsNone(plan)
        self.assertIn("could not be read", err)
        self.assertNotIn("./other.sh", err)
        self.assertEqual(foldcompose.project_state(p1), ("unknown", None))

    def test_two_keys_holding_different_records_refuse_naming_both(self):  # noqa: VACUOUS_ASSERTION — the `assertIsNone(plan)` is one half of a pair whose other half asserts the refusal text NAMES both keys and does not leak the losing command, and the CONTROL at the end removes one of the two on the same file and asserts the identical questions resolve to the block and spawn its argv
        """The resolver's ambiguity law, and it is about CONTENT. One (project,
        location) holds exactly one record; two keys that DISAGREE about it —
        here, two different commands — is refused at the ONE door, never
        resolved by domain order, with both keys in the sentence. The gate, one
        hop up, reads that refusal as an UNREADABLE registry and admits nothing.
        (Two keys holding the SAME record are one record and resolve; that is
        the arm above.) BLAST RADIUS: this arm — the control is the same file
        with one of the two removed, which resolves and spawns.
        """
        from helm import gate
        path = self._dir("twice", "alpha")
        stamped = registry._qualified("alpha", path)
        other = {"command": ["./other.sh"], "protocol": "exit"}
        # CONTROL ON THE INPUT: the two records DISAGREE, which is the whole
        # reason this file is refused rather than resolved.
        self.assertNotEqual(other, self.BLOCK)
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "alpha": {"name": "alpha", "path": path, "kind": "git",
                      "status": "active", "sessions": {}}}})
        pk.write_json(home.authored_path(), {"version": 1, "projects": {
            "alpha": {"path": path, self.FIELD: self.BLOCK},
            stamped: {"path": path, self.FIELD: other}}})
        auth = registry._authored_load(strict=True)
        with self.assertRaises(ValueError) as caught:
            registry.authority_record(auth, "alpha", path)
        self.assertIn("alpha", str(caught.exception))
        self.assertIn(stamped, str(caught.exception))
        with self.assertRaises(ValueError):
            registry.declaration_provenance("alpha", path, self.FIELD)
        with self.assertRaises(ValueError):
            registry.authored_declarations(self.FIELD)
        plan, err = gate.suite_command(path)
        self.assertIsNone(plan)
        self.assertIn("could not be read", err)
        self.assertNotIn("./other.sh", err)
        # CONTROL: one record, and the identical questions resolve to it.
        auth = pk.read_json(home.authored_path())
        del auth["projects"][stamped]
        pk.write_json(home.authored_path(), auth)
        self.assertEqual(
            registry.declaration_provenance("alpha", path, self.FIELD),
            (self.BLOCK, "authored"))
        self.assertEqual(gate.suite_command(path)[0]["argv"],
                         self.BLOCK["command"])

    def test_every_reader_and_writer_asks_the_one_resolver(self):
        """The resolver-identity arm. The property is that NO door resolves a
        record's identity on its own: every reader and every writer asks
        `authority_record`. So this SPIES THE REAL RESOLVER (wrapped, never
        replaced) and drives every listed reader and writer through the shipped
        verbs, asserting each one reached it for this project. A door that
        reimplements the lookup would leave its count at zero. BLAST RADIUS:
        this arm.
        """
        p0, p1, p2 = self._base_era_repoints()
        p3 = self._dir("p3", "alpha")
        seen = []
        real = registry.authority_record

        def spy(auth, name, path):
            seen.append((name, path))
            return real(auth, name, path)

        def drives(label, act):
            del seen[:]
            out = act()
            self.assertTrue(seen, "%s never asked the resolver" % label)
            self.assertIn("alpha", [name for name, _path in seen], label)
            return out

        with mock.patch.object(registry, "authority_record", spy):
            drives("declaration_provenance", lambda: self.assertEqual(
                registry.declaration_provenance("alpha", p2, self.FIELD),
                (self.BLOCK, "authored")))
            drives("authored_declarations",
                   lambda: self.assertEqual(self.declarations(),
                                            [("alpha", p2)]))
            drives("load (binding apply + overlay)", lambda: self.assertEqual(
                registry.load(strict=True)["projects"]["alpha"]["path"], p2))
            os.rmdir(p2)
            drives("repoint", lambda: self.assertIsNone(
                registry.repoint("alpha", p2, p3, apply=True)[1]))
            drives("save", lambda: registry.save(registry.load()))
            os.rmdir(p3)
            drives("forget", lambda: self.assertIsNone(
                registry.forget("alpha", apply=True)[1]))
            drives("restore", lambda: self.assertIsNone(
                registry.restore("alpha", apply=True)[1]))
        # AND THE ROUND TRIP THROUGH EVERY DOOR KEPT THE DECLARATION AUTHORED:
        # the spy wrapped the real call, so this is the shipped answer.
        self.assertEqual(
            registry.declaration_provenance("alpha", p3, self.FIELD),
            (self.BLOCK, "authored"))
        self.assertEqual(self.declarations(), [("alpha", p3)])


if __name__ == "__main__":
    unittest.main()
