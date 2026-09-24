"""Installed pair ownership through real writers and fake-home CAS only."""
from copy import deepcopy
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import helm
from helm import configs, envtidy, hooks, posttool, record

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_ROOM_REPAIR = hooks.repair_lane_room_commands
_STALE_ROOMS = hooks.stale_room_tokens


class InstalledPair(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="helm-posttool-install-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = str(self.root / "helm")
        for obj, name, value in (
            (hooks, "helm_bin", lambda: self.bin),
            (hooks, "repair_lane_room_commands", lambda *a, **kw: []),
            (hooks, "stale_room_tokens", lambda *a: []),
            (configs, "HOME_ROOTS", [str(self.home)]),
            (configs, "CWD_ROOTS", [str(self.root)]),
            (configs, "BACKUP_DIR", str(self.root / "backups")),
            (envtidy, "_load_hook_override", lambda: None),
        ):
            patch = mock.patch.object(obj, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        # THE SHIPPED LADDER, BESIDE THE STUB helm. `hooks.wrapper_bin`
        # derives bin/helm-hook from the helm it wraps, so the arms that
        # EXECUTE a rendered command need the real script here or they measure
        # the shell's own 127 instead of the wrapper.
        shutil.copy2(os.path.join(ROOT, "bin", hooks.HOOK_WRAPPER),
                     str(self.root / hooks.HOOK_WRAPPER))
        self.rec, self.deliver = posttool.installed_specs()

    def read(self):
        return json.loads((self.home / "settings.json").read_text())

    def write(self, settings):
        (self.home / "settings.json").write_text(json.dumps(settings))

    def pair(self):
        return {"hooks": {posttool.EVENT: [posttool.entry(executable=self.bin)]}}

    def assert_pair(self, settings):
        commands = record._event_cmds(settings)
        self.assertEqual(len(commands), 1, commands)
        self.assertEqual(posttool.covered_members(settings, executable=self.bin),
                         {"record", "deliver"})
        self.assertTrue(record._leg_live(settings, record.HOOK_EVENT))
        self.assertTrue(hooks._lane_live(settings, self.deliver))

    def test_installers_converge_both_orders_and_remain_idempotent(self):
        results = []
        for first in ("record", "deliver"):
            with self.subTest(first=first):
                self.write({"foreign": {"keep": [1, 2]}})
                install = {
                    "record": lambda: record.install_home(str(self.home)),
                    "deliver": lambda: hooks.install_home(str(self.home), specs=(self.deliver,)),
                }
                second = "deliver" if first == "record" else "record"
                self.assertNotEqual(install[first]()[0], "fail")
                one = self.read()
                self.assertEqual(len(record._event_cmds(one)), 1)
                self.assertEqual(posttool.covered_members(one, executable=self.bin), set())
                self.assertNotEqual(install[second]()[0], "fail")
                self.assert_pair(self.read())
                frozen = (self.home / "settings.json").read_bytes()
                self.assertEqual(install[first]()[0], "ok")
                self.assertEqual(install[second]()[0], "ok")
                self.assertEqual((self.home / "settings.json").read_bytes(), frozen)
                self.assertTrue(record._leg_live(self.read(), record.FAIL_EVENT))
                results.append(self.read())
        # Equivalent all-tool matcher spelling belongs to the existing group.
        for result in results:
            result["hooks"][posttool.EVENT][0].setdefault("matcher", "*")
        self.assertEqual(results[0], results[1])

    def test_delivery_only_seat_and_record_only_project_do_not_seed_sibling(self):
        seat, _, _ = envtidy._derive_hooks("seat:example", {})
        self.assertEqual(record._event_cmds(seat), [hooks.spec_command(self.deliver)])
        self.assertNotIn(record.FAIL_EVENT, seat["hooks"])
        (self.root / "project" / ".claude").mkdir(parents=True)
        result = hooks.install_project(str(self.root / "project"), specs=(self.rec,))
        self.assertNotEqual(result[0], "fail", result)
        path = Path(hooks.project_settings_path(str(self.root / "project")))
        project = json.loads(path.read_text())
        self.assertEqual(record._event_cmds(project), [record.hook_command()])
        self.assertNotIn(record.FAIL_EVENT, project["hooks"])
        self.assertEqual(posttool.covered_members(project, executable=self.bin), set())

    def test_exact_duplicate_blocks_convert_through_each_writer(self):
        foreign = {"type": "command", "command": "foreign-before", "keep": [1]}
        failure = {"hooks": [{"type": "command", "command": "foreign-failure"}]}
        for writer in ("hooks", "record", "sync"):
            with self.subTest(writer=writer):
                self.write({"keepRoot": [2], "hooks": {
                    posttool.EVENT: [{"hooks": [foreign,
                        *[hooks._canonical_entry(s)["hooks"][0]
                          for s in (self.rec, self.rec, self.deliver, self.deliver)]]}],
                    record.FAIL_EVENT: [failure]}})
                install = {
                    "hooks": lambda: hooks.install_home(str(self.home), specs=(self.rec, self.deliver)),
                    "record": lambda: record.install_home(str(self.home)),
                    "sync": lambda: envtidy.apply_hooks_home(
                        envtidy.plan_hooks_home("default-claude", str(self.home)),
                        str(self.root / "sync-backups")),
                }[writer]
                result = install()
                self.assertIn(result[0], ("add", "update", "applied"), result)
                got = self.read()
                self.assertEqual(posttool.covered_members(got, executable=self.bin),
                                 {"record", "deliver"})
                leaves = got["hooks"][posttool.EVENT][0]["hooks"]
                self.assertEqual(leaves[0], foreign)
                self.assertEqual(len(leaves), 2)
                self.assertEqual(got["keepRoot"], [2])
                self.assertEqual(got["hooks"][record.FAIL_EVENT][0], failure)
                frozen = (self.home / "settings.json").read_bytes()
                self.assertEqual(install()[0], "ok")
                self.assertEqual((self.home / "settings.json").read_bytes(), frozen)

    def test_narrow_repair_never_resurrects_standalones(self):
        original = self.pair()
        for specs in ((), (self.rec,), (self.deliver,)):
            got, actions = hooks._merge_all(original, specs)
            self.assert_pair(got)
            self.assertEqual(got["hooks"], original["hooks"])
            self.assertEqual(hooks.stale_specs(got, specs), [])
        got, action = record._merge_hook(original, record.hook_command())
        self.assertEqual((got, action), (original, "ok"))

    def test_sync_plans_and_cas_verifier_accept_pair_without_resurrection(self):
        self.write(self.pair())
        plan = envtidy.plan_hooks_home("default-claude", str(self.home))
        before = self.read()
        before["foreignAfterPlan"] = {"keep": True}
        self.write(before)
        verdict, detail = envtidy.apply_hooks_home(plan, str(self.root / "sync-backups"))
        self.assertEqual(verdict, "applied", detail)
        self.assert_pair(self.read())
        self.assertTrue(self.read()["foreignAfterPlan"]["keep"])
        self.assertTrue(record._leg_live(self.read(), record.FAIL_EVENT))
        self.assertEqual(envtidy.plan_hooks_home("default-claude", str(self.home))["verdict"], "ok")
        self.assertEqual(record.install_home(str(self.home))[0], "ok")

    def test_recorder_event_action_cannot_be_erased_by_failure_leg(self):
        original = {"hooks": {
            record.HOOK_EVENT: [hooks._canonical_entry(self.deliver)],
            record.FAIL_EVENT: [hooks._canonical_entry(record.deployed_spec(record.FAIL_EVENT))],
        }}
        want = tuple(t for t in envtidy.CANONICAL_HOOKS if t[2] == "record --hook-json")
        with mock.patch.object(envtidy, "hooks_for", return_value=("home", want)):
            out, actions, _ = envtidy._derive_hooks("default-claude", original)
        self.assert_pair(out)
        self.assertEqual(actions["record --hook-json"], "update")
        self.assertEqual(out["hooks"][record.FAIL_EVENT], original["hooks"][record.FAIL_EVENT])

    def test_exact_foreign_mixed_groups_preserve_execution_order_and_keys(self):
        left = {"type": "command", "command": "foreign-left", "extra": [1]}
        right = {"type": "command", "command": "foreign-right", "extra": {"x": 2}}
        original = {"unknownRoot": [1], "hooks": {posttool.EVENT: [{"hooks": [
            left, hooks._canonical_entry(self.rec)["hooks"][0],
            hooks._canonical_entry(self.deliver)["hooks"][0], right]}]}}
        out, _ = hooks._merge_all(original, (self.deliver,))
        leaves = out["hooks"][posttool.EVENT][0]["hooks"]
        self.assertEqual(leaves[0], left)
        self.assertEqual(leaves[-1], right)
        self.assertEqual(len(leaves), 3)
        self.assertEqual(out["unknownRoot"], [1])
        self.assertEqual(len(original["hooks"][posttool.EVENT][0]["hooks"]), 4)

    def test_ambiguous_foreign_and_custom_requests_refuse_before_legacy_repair(self):
        pair = self.pair()
        variants = []
        unknown = deepcopy(pair)
        unknown["hooks"][posttool.EVENT][0]["custom"] = {"preserve": True}
        variants.append((unknown, (self.deliver,)))
        for field, value in (("timeout", 3), ("matcher", "Bash"), ("name", "alias")):
            variants.append((pair, (dict(self.deliver, **{field: value}),)))
        for original, requested in variants:
            with self.subTest(requested=requested):
                frozen = deepcopy(original)
                with mock.patch.object(hooks, "_merge_event") as legacy:
                    with self.assertRaises(ValueError):
                        hooks._merge_all(original, requested)
                    legacy.assert_not_called()
                self.assertEqual(original, frozen)
        with self.assertRaises(ValueError):
            record._merge_hook(pair, "custom-record-command")

    def test_status_and_census_bind_native_contract(self):
        self.write(self.pair())
        self.assertNotEqual(record.install_home(str(self.home))[0], "fail")
        with mock.patch.object(hooks, "claude_homes", return_value=[("fake", str(self.home))]):
            self.assertTrue(record.status_rows()[0]["hook"])
        with mock.patch.object(envtidy, "home_mcps", return_value={"effective": []}), \
                mock.patch.object(envtidy, "canonical_mcps", return_value={}), \
                mock.patch.object(envtidy, "_worktree_census", return_value={}):
            row = envtidy.census(dirs=[("fake", str(self.home))])["homes"][0]
        self.assertNotIn("PostToolUse record --hook-json", row["missing"])
        self.assertNotIn("PostToolUse chat deliver --hook-json", row["missing"])
        for field, value in (("type", "prompt"), ("timeout", 23), ("command", "unknown")):
            damaged = self.pair()
            damaged["hooks"][posttool.EVENT][0]["hooks"][0][field] = value
            self.assertFalse(record._leg_live(damaged, record.HOOK_EVENT))
            self.assertFalse(hooks._lane_live(damaged, self.deliver))
        damaged = self.pair()
        damaged["hooks"][posttool.EVENT][0]["matcher"] = "Bash"
        self.assertFalse(record._leg_live(damaged, record.HOOK_EVENT))
        self.assertFalse(hooks._lane_live(damaged, self.deliver))

    def test_failure_leg_repairs_event_alarm_without_deleting_foreign_keys(self):
        historical = {"hooks": {record.FAIL_EVENT: [{"hooks": [
            {"type": "command", "command": record.hook_command(), "timeout": 15,
             "custom": {"keep": True}}]}]}}
        self.assertFalse(record._leg_live(historical, record.FAIL_EVENT))
        out, action = record._merge_hook(historical, record.hook_command(record.FAIL_EVENT), record.FAIL_EVENT)
        leaf = out["hooks"][record.FAIL_EVENT][0]["hooks"][0]
        self.assertEqual(action, "update")
        self.assertEqual(leaf["custom"], {"keep": True})
        self.assertEqual(leaf["command"], hooks.spec_command(record.deployed_spec(record.FAIL_EVENT)))
        self.assertTrue(record._leg_live(out, record.FAIL_EVENT))
        # THE EVENT IS AN OPERAND, and the harness ROUTES on it: a wrapper
        # that carried the wrong one would announce a PostToolUse failure to a
        # reader of the failure event. The envelope it becomes is asserted by
        # executing the command, at the end of the arm two below.
        self.assertEqual(shlex.split(leaf["command"])[3], record.FAIL_EVENT)

    def test_budget_and_registry_separation(self):
        self.assertEqual([s["timeout"] for s in record.HOOK_SPECS], [5, 5])
        self.assertEqual((self.rec["timeout"], self.deliver["timeout"]), (10, 2))
        self.assertEqual(posttool.preparation_budget(), 2)
        self.assertEqual(posttool.descriptor()["timeout"], 19)
        self.assertEqual(posttool.entry(executable=self.bin)["hooks"][0]["timeout"], 24)
        self.assertEqual([t[3] for t in envtidy.CANONICAL_HOOKS if t[2] == "record --hook-json"], [10, 10])
        self.assertNotIn(posttool.ARGS, [s["args"] for s in (*hooks.SPECS, *record.HOOK_SPECS)])
        # THE KIND IS THE CONTRACT. The runner's sentence and its
        # stderr-only channel live in bin/helm-hook under `posttool`; the
        # advisory kinds carry the two-channel envelope. Asserting the operand
        # here and the behaviour where it runs is what keeps a text search
        # from passing on a ladder that lost the distinction.
        command = hooks.spec_command(posttool.descriptor())
        self.assertEqual(shlex.split(command)[1], "posttool")
        self.assertEqual(shlex.split(hooks.spec_command(self.deliver))[1],
                         "lane")
        ladder = open(os.path.join(ROOT, "bin", hooks.HOOK_WRAPPER),
                      encoding="utf-8").read()
        self.assertIn("publication UNKNOWN", ladder)
        self.assertIn("hookSpecificOutput", ladder)

    def test_outer_failure_preserves_exact_child_bytes_including_partial_output(self):
        child = Path(self.bin)
        child.write_text("#!" + sys.executable + "\n" + '''
import os, signal
scenario = os.environ["SCENARIO"]
if scenario.startswith("full"):
    os.write(1, b'{"systemMessage":"synthetic"}')
elif scenario.startswith("partial"):
    os.write(1, b'{"systemMessage":')
if scenario.endswith("signal"):
    os.kill(os.getpid(), signal.SIGTERM)
if scenario.endswith("timeout"):
    signal.pause()
raise SystemExit(0 if scenario.endswith("ok") else 1)
''')
        child.chmod(0o700)
        # Short synthetic timeout, not an installed-budget measurement.
        cmd = hooks.spec_command(dict(posttool.descriptor(), timeout=1))
        for scenario in ("full-ok", "empty-ok", "empty-error", "full-error",
                         "partial-error", "full-signal", "full-timeout"):
            with self.subTest(scenario=scenario):
                expected = (b'{"systemMessage":"synthetic"}' if scenario.startswith("full")
                            else b'{"systemMessage":' if scenario.startswith("partial") else b"")
                proc = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True,
                                      timeout=4, env={"PATH": "/usr/bin:/bin", "SCENARIO": scenario})
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, expected)
                self.assertEqual(b"publication UNKNOWN" in proc.stderr,
                                 not scenario.endswith("ok"))
        failed = subprocess.run(["/bin/sh", "-c", record.hook_command(record.FAIL_EVENT)],
                                capture_output=True, timeout=4,
                                env={"PATH": "/usr/bin:/bin", "SCENARIO": "empty-error"})
        self.assertEqual(failed.returncode, 0)
        self.assertEqual(json.loads(failed.stdout)["hookSpecificOutput"]["hookEventName"],
                         record.FAIL_EVENT)

    def test_narrowed_unrequested_pair_repair_reports_actual_write(self):
        original, _ = hooks._merge_all(self.pair(), ())
        old = posttool.entry(executable=str(self.root / "old-helm"))
        original["hooks"][posttool.EVENT] = [old]
        out, actions = hooks._merge_all(original, ())
        self.assert_pair(out)
        self.assertEqual(hooks._agg(actions), "update")
        with mock.patch.object(envtidy, "hooks_for", return_value=("home", ())):
            synced, actions, _ = envtidy._derive_hooks("default-claude", original)
        self.assert_pair(synced)
        self.assertEqual(actions["posttool"], "update")
        self.assertFalse(hooks._lane_live(out, dict(self.deliver, scope="custom")))

    def protected_settings(self):
        leaf = {"type": "command", "timeout": 91, "custom": {"keep": ["λ"]},
                "command": "timeout 10 /old/bin/helm record --hook-json || true; printf RAW_SENTINEL"}
        return {"keepRoot": [7], "hooks": {posttool.EVENT: [{"hooks": [leaf]}],
            record.FAIL_EVENT: [hooks._canonical_entry(record.deployed_spec(record.FAIL_EVENT))]}}

    def writer(self, name):
        if name == "project":
            project = self.root / "project"
            (project / ".claude").mkdir(parents=True, exist_ok=True)
            return (Path(hooks.project_settings_path(str(project))),
                    lambda: hooks.install_project(str(project), specs=(self.rec, self.deliver)))
        writers = {
            "hooks": lambda: hooks.install_home(str(self.home), specs=(self.rec, self.deliver)),
            "record": lambda: record.install_home(str(self.home)),
            "sync": lambda: envtidy.apply_hooks_home(
                envtidy.plan_hooks_home("default-claude", str(self.home)),
                str(self.root / "sync-backups")),
        }
        return self.home / "settings.json", writers[name]

    def test_ignored_foreign_shapes_survive_writers_repeat_status_and_census(self):
        for group in (None, {"hooks": None}, {"hooks": "bad"}, {"hooks": ["bad"]}):
            for name in ("hooks", "project", "record", "sync"):
                with self.subTest(group=group, writer=name):
                    path, install = self.writer(name)
                    original = {"keepRoot": [3], "hooks": {posttool.EVENT: [group],
                        record.FAIL_EVENT: [hooks._canonical_entry(record.deployed_spec(record.FAIL_EVENT))]}}
                    path.write_text(json.dumps(original))
                    action, detail = install()
                    self.assertIn(action, ("add", "update", "applied"), detail)
                    got = json.loads(path.read_text())
                    self.assertEqual(got["hooks"][posttool.EVENT][0], group)
                    self.assertEqual(got["keepRoot"], [3])
                    self.assertTrue(record._leg_live(got, record.HOOK_EVENT))
                    if name != "record":
                        self.assertTrue(hooks._lane_live(got, self.deliver))
                    frozen = path.read_bytes()
                    self.assertEqual(install()[0], "ok")
                    self.assertEqual(path.read_bytes(), frozen)
                    home = str(path.parent)
                    with mock.patch.object(hooks, "claude_homes", return_value=[("fake", home)]):
                        # Home reports intentionally read settings.json, never
                        # the project's settings.local.json via a filename alias.
                        self.assertEqual(record.status_rows()[0]["hook"], name != "project")
                        hrow = hooks.status_rows()[0]
                        self.assertEqual(hrow["deliver"], name not in ("record", "project"))
                        # A full-spec drift plan can legitimately propose pair
                        # conversion after a recorder-only install.
                        self.assertEqual(hrow["drifted"], ["deliver"] if name == "record" else [])
                    if name == "project":
                        with mock.patch.object(envtidy.skillsync, "config_dirs", return_value=[]):
                            scopes = hooks.project_scope_rows(contexts=[{
                                "root": str(self.root / "project"), "homes": [str(self.root / "absent-home")]}])
                        self.assertEqual(len(scopes), 1)
                        self.assertEqual(scopes[0]["path"], str(path))
                        self.assertEqual(scopes[0]["status"], "project-only")
                        self.assertEqual(scopes[0]["specs"], ["deliver"])
                    with mock.patch.object(envtidy, "home_mcps", return_value={"effective": []}), \
                            mock.patch.object(envtidy, "canonical_mcps", return_value={}), \
                            mock.patch.object(envtidy, "_worktree_census", return_value={"note": "fake"}):
                        report = envtidy.census(dirs=[("fake", home)])
                    row = report["homes"][0]
                    self.assertEqual("PostToolUse record --hook-json" in row["missing"], name == "project")
                    self.assertEqual("PostToolUse chat deliver --hook-json" in row["missing"],
                                     name in ("record", "project"))
                    envtidy._print_census(report, out=io.StringIO())
                    self.assertEqual(path.read_bytes(), frozen)

    def test_project_scope_projects_only_healthy_composite_members(self):
        path, install = self.writer("project")
        path.write_text(json.dumps({}))
        self.assertNotEqual(install()[0], "fail")
        contexts = [{"root": str(self.root / "project"), "homes": [str(self.home)]}]
        with mock.patch.object(envtidy.skillsync, "config_dirs", return_value=[("fake", str(self.home))]):
            self.write({})
            row = hooks.project_scope_rows(contexts)[0]
            self.assertEqual((row["status"], row["specs"]), ("project-only", ["deliver"]))
            for home_settings, overlap in ((self.pair(), ["deliver"]),
                    ({"hooks": {posttool.EVENT: [hooks._canonical_entry(self.deliver)]}}, ["deliver"])):
                self.write(home_settings)
                row = hooks.project_scope_rows(contexts)[0]
                self.assertEqual(row["status"], "duplicate")
                self.assertEqual(row["duplicates"][0]["specs"], overlap)
            for field, value in (("timeout", 23), ("type", "prompt"), ("custom", True)):
                bad = self.pair()
                bad["hooks"][posttool.EVENT][0]["hooks"][0][field] = value
                path.write_text(json.dumps(bad))
                self.assertEqual(hooks.project_scope_rows(contexts), [])
                self.assertEqual(hooks._owned_specs(bad), [])
            for bad in ({"hooks": {record.FAIL_EVENT: self.pair()["hooks"][posttool.EVENT]}},
                        {"hooks": {posttool.EVENT: [dict(posttool.entry(executable=self.bin), matcher="Bash")]}}):
                path.write_text(json.dumps(bad))
                frozen = path.read_bytes()
                self.assertEqual(hooks.project_scope_rows(contexts), [])
                self.assertEqual(path.read_bytes(), frozen)
            path.write_text(json.dumps(self.pair()))
            self.write(bad)
            frozen = path.read_bytes(), (self.home / "settings.json").read_bytes()
            self.assertEqual(hooks.project_scope_rows(contexts)[0]["status"], "project-only")
            self.assertEqual((path.read_bytes(), (self.home / "settings.json").read_bytes()), frozen)
            path.unlink()
            self.write(self.pair())
            self.assertEqual(hooks.project_scope_rows(contexts), [])

    def test_event_nonlist_refuses_before_legacy_or_write_each_writer(self):
        for name in ("hooks", "project", "record", "sync"):
            with self.subTest(writer=name):
                path, install = self.writer(name)
                path.write_text(json.dumps({"hooks": {posttool.EVENT: {}}}))
                frozen = path.read_bytes()
                with mock.patch.object(posttool, "_scan", wraps=posttool._scan) as scan, \
                        mock.patch.object(hooks, "_merge_event") as legacy, \
                        mock.patch.object(configs, "write_file") as write:
                    action, detail = install()
                self.assertIn(action, ("fail", "FAIL"))
                self.assertIn("PostToolUse groups must be a list", detail)
                self.assertGreater(scan.call_count, 0)
                legacy.assert_not_called()
                write.assert_not_called()
                self.assertEqual(path.read_bytes(), frozen)
                self.assertEqual(install()[0], action)
                self.assertEqual(path.read_bytes(), frozen)
                with mock.patch.object(hooks, "claude_homes", return_value=[("fake", str(path.parent))]):
                    self.assertFalse(record.status_rows()[0]["hook"])
                    self.assertEqual(hooks.status_rows()[0]["drifted"], [] if name == "project" else None)
                if name == "project":
                    with mock.patch.object(envtidy.skillsync, "config_dirs", return_value=[]):
                        self.assertEqual(hooks.project_scope_rows(contexts=[{
                            "root": str(self.root / "project"), "homes": []}]), [])
                with mock.patch.object(envtidy, "home_mcps", return_value={"effective": []}), \
                        mock.patch.object(envtidy, "canonical_mcps", return_value={}), \
                        mock.patch.object(envtidy, "_worktree_census", return_value={"note": "fake"}):
                    report = envtidy.census(dirs=[("fake", str(path.parent))])
                self.assertIn("PostToolUse record --hook-json", report["homes"][0]["missing"])
                self.assertIn("PostToolUse chat deliver --hook-json", report["homes"][0]["missing"])
                envtidy._print_census(report, out=io.StringIO())

    def test_protected_leaf_partial_success_is_visible_and_idempotent_each_writer(self):
        for name in ("hooks", "project", "record", "sync"):
            with self.subTest(writer=name):
                path, install = self.writer(name)
                original = self.protected_settings()
                leaf = original["hooks"][posttool.EVENT][0]["hooks"][0]
                path.write_text(json.dumps(original))
                text, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(text), contextlib.redirect_stderr(errors):
                    action, detail = install()
                self.assertIn(action, ("add", "update", "applied"), detail)
                self.assertEqual((text.getvalue(), errors.getvalue()), ("", ""))
                got = json.loads(path.read_text())
                leaves = [h for g in got["hooks"][posttool.EVENT] for h in g["hooks"]]
                self.assertEqual(leaves[0], leaf)
                self.assertEqual(sum(posttool.recognize(h.get("command")) is not None for h in leaves), 1)
                self.assertEqual(got["hooks"][record.FAIL_EVENT], original["hooks"][record.FAIL_EVENT])
                self.assertEqual(got["keepRoot"], [7])
                self.assertTrue(record._leg_live(got, record.HOOK_EVENT))
                for message in (detail, install()[1]):
                    self.assertIn("REFUSED", message)
                    self.assertIn(str(path.parent), message)
                    self.assertIn("non-whole-PostToolUse-member", message)
                    self.assertIn("possible duplicate recorder execution", message)
                    self.assertNotIn("RAW_SENTINEL", message)
                frozen = path.read_bytes()
                self.assertEqual(install()[0], "ok")
                self.assertEqual(path.read_bytes(), frozen)
                self.assertTrue(posttool.refusals(json.loads(frozen)))

    def test_protected_leaf_reports_winning_CAS_snapshot_not_first_plan(self):
        for name in ("hooks", "record", "sync"):
            for change in ("add-protected", "remove-protected", "already-written"):
                with self.subTest(writer=name, conflict=change):
                    path, install = self.writer(name)
                    original = self.protected_settings() if change != "add-protected" else {}
                    path.write_text(json.dumps(original))
                    calls, real = [], configs.write_file
                    def conflict_once(target, content, expected_revision=None):
                        calls.append(expected_revision)
                        if len(calls) == 1:
                            winner = (json.loads(content) if change == "already-written" else
                                      self.protected_settings() if change == "add-protected" else {})
                            path.write_text(json.dumps(winner))
                            return {"error": "conflict", "code": "conflict"}
                        return real(target, content, expected_revision=expected_revision)
                    with mock.patch.object(configs, "write_file", side_effect=conflict_once):
                        action, detail = install()
                    self.assertNotIn(action, ("fail", "FAIL"), detail)
                    self.assertEqual("REFUSED" in detail, change != "remove-protected")
                    self.assertNotIn("RAW_SENTINEL", detail)
                    self.assertTrue(calls)
                    if change == "already-written":
                        self.assertEqual(action, "ok")
                        self.assertEqual(len(calls), 1)
                    else:
                        self.assertEqual(len(calls), 2)
                    got = json.loads(path.read_text())
                    self.assertEqual(bool(posttool.refusals(got)), change != "remove-protected")
                    self.assertTrue(record._leg_live(got, record.HOOK_EVENT))

    def test_committed_actions_and_winning_warnings_have_distinct_evidence(self):
        modes = ("postcommit-add-warning", "postcommit-remove-warning", "precommit-update",
                 "competitor-installed", "multiple-writes", "failed-after-commit", "dry")
        for name in ("hooks", "project", "record"):
            for mode in modes:
                with self.subTest(writer=name, mode=mode):
                    path, _unused = self.writer(name)
                    original = self.protected_settings() if mode == "postcommit-remove-warning" else {}
                    path.write_text(json.dumps(original))
                    protected_group = self.protected_settings()["hooks"][posttool.EVENT][0]
                    calls, successful, transactions = [], [], []
                    real_write, real_transform = configs.write_file, configs.transform_json_file

                    def observe(*args, **kwargs):
                        result = real_transform(*args, **kwargs)
                        transactions.append(result)
                        return result

                    def drift(data):
                        for group in data["hooks"][posttool.EVENT]:
                            for leaf in group["hooks"]:
                                if posttool.recognize(leaf.get("command")):
                                    leaf["timeout"] = 99
                                    return
                        self.fail("synthetic competitor missed canonical installed member")

                    def race(target, content, expected_revision=None):
                        calls.append(expected_revision)
                        data = json.loads(content)
                        if mode in ("precommit-update", "competitor-installed") and len(calls) == 1:
                            if mode == "precommit-update":
                                drift(data)
                            else:
                                data["hooks"][posttool.EVENT].insert(0, deepcopy(protected_group))
                            data["foreignWon"] = mode
                            path.write_text(json.dumps(data))
                            return {"error": "synthetic precommit competitor", "code": "conflict"}
                        if mode == "failed-after-commit" and len(calls) == 2:
                            return {"error": "SYNTHETIC_WRITE_FAILURE", "code": "write"}
                        saved = real_write(target, content, expected_revision=expected_revision)
                        if saved.get("ok"):
                            successful.append(True)
                            if len(successful) == 1 and mode in (
                                    "postcommit-add-warning", "postcommit-remove-warning",
                                    "multiple-writes", "failed-after-commit"):
                                if mode == "postcommit-add-warning":
                                    data["hooks"][posttool.EVENT].insert(0, deepcopy(protected_group))
                                elif mode == "postcommit-remove-warning":
                                    data["hooks"][posttool.EVENT].remove(protected_group)
                                else:
                                    drift(data)
                                data["foreignWon"] = mode
                                path.write_text(json.dumps(data))
                        return saved

                    with mock.patch.object(configs, "write_file", side_effect=race), \
                            mock.patch.object(configs, "transform_json_file", side_effect=observe):
                        if name == "record":
                            action, detail = record.install_home(str(path.parent), dry=mode == "dry")
                        elif name == "project":
                            action, detail = hooks.install_project(str(self.root / "project"),
                                specs=(self.deliver,), dry=mode == "dry")
                        else:
                            action, detail = hooks.install_home(str(path.parent),
                                specs=(self.deliver,), dry=mode == "dry")
                    expected = {"precommit-update": "update", "competitor-installed": "ok",
                                "multiple-writes": "add" if name == "record" else "update",
                                "failed-after-commit": "fail", "dry": "dry-add"}.get(mode, "add")
                    self.assertEqual(action, expected, detail)
                    self.assertEqual(len(transactions), 1)
                    result = transactions[0]
                    if mode == "failed-after-commit":
                        self.assertFalse(result["ok"])
                        self.assertIn("SYNTHETIC_WRITE_FAILURE", detail)
                        self.assertNotIn("canonical wiring", detail)
                        self.assertEqual(len(successful), 1)
                        continue
                    self.assertEqual(len(result["committed_metadata"]), len(successful))
                    self.assertEqual(result["wrote"], bool(successful))
                    self.assertEqual("REFUSED" in detail, mode in (
                        "postcommit-add-warning", "competitor-installed"))
                    self.assertNotIn("RAW_SENTINEL", detail)
                    if mode == "dry":
                        self.assertEqual(calls, [])
                        self.assertEqual(json.loads(path.read_text()), original)
                        continue
                    self.assertEqual(result["attempts"], 2)
                    got = json.loads(path.read_text())
                    self.assertEqual(got["foreignWon"], mode)
                    self.assertTrue(record._leg_live(got, record.HOOK_EVENT) if name == "record"
                                    else hooks._lane_live(got, self.deliver))
                    if mode.startswith("postcommit"):
                        self.assertIn("CAS attempts: 2", detail)
                        self.assertEqual(len(successful), 1)
                    if mode == "multiple-writes":
                        self.assertEqual(len(successful), 2)

    def test_mixed_orphan_writers_preserve_both_policies_through_CAS(self):
        want = tuple(t for t in envtidy.CANONICAL_HOOKS if t[2] == "chat deliver --hook-json")
        for name in ("hooks", "project", "sync"):
            for mode in ("clean", "pre-add", "pre-remove", "post-add", "post-remove",
                         "competitor", "fail"):
                with self.subTest(writer=name, race=mode):
                    path, _unused = self.writer(name)
                    ordinary = hooks._canonical_entry(self.deliver)["hooks"][0]
                    opaque = dict(ordinary, **{"async": True})
                    foreign = {"type": "command", "command": "foreign-retained", "keep": [1]}
                    groups = [{"matcher": matcher, "hooks": [deepcopy(foreign), deepcopy(leaf)]}
                              for matcher, leaf in zip(("Bash", "Edit"), (ordinary, opaque))]
                    protected = self.protected_settings()["hooks"][posttool.EVENT][0]
                    failure = self.protected_settings()["hooks"][record.FAIL_EVENT]
                    original = {"keepRoot": [9], "hooks": {
                        posttool.EVENT: groups + ([deepcopy(protected)] if mode.endswith("remove") else []),
                        record.FAIL_EVENT: failure}}
                    path.write_text(json.dumps(original))
                    before = path.read_bytes()
                    calls, successful, transactions = [], [], []
                    real_write, real_transform = configs.write_file, configs.transform_json_file

                    def observe(*args, **kwargs):
                        result = real_transform(*args, **kwargs)
                        transactions.append(result)
                        return result

                    def change(data):
                        if mode.endswith("remove"):
                            data["hooks"][posttool.EVENT].remove(protected)
                        else:
                            data["hooks"][posttool.EVENT].append(deepcopy(protected))
                        data["foreignWon"] = mode
                        return data

                    def race(target, content, expected_revision=None):
                        calls.append(expected_revision)
                        if mode == "fail":
                            return {"error": "SYNTHETIC_MIXED_WRITE_FAILURE", "code": "write"}
                        if len(calls) == 1 and (mode.startswith("pre") or mode == "competitor"):
                            data = json.loads(content) if mode == "competitor" else deepcopy(original)
                            path.write_text(json.dumps(change(data)))
                            return {"error": "synthetic competitor", "code": "conflict"}
                        saved = real_write(target, content, expected_revision=expected_revision)
                        if saved.get("ok"):
                            successful.append(True)
                            if len(successful) == 1 and mode.startswith("post"):
                                path.write_text(json.dumps(change(json.loads(content))))
                        return saved

                    def install():
                        if name == "project":
                            return hooks.install_project(str(self.root / "project"), specs=(self.deliver,))
                        if name == "sync":
                            return envtidy.apply_hooks_home(
                                envtidy.plan_hooks_home("default-claude", str(path.parent)),
                                str(self.root / "sync-backups"))
                        return hooks.install_home(str(path.parent), specs=(self.deliver,))

                    with mock.patch.object(envtidy, "hooks_for", return_value=("home", want)), \
                            mock.patch.object(configs, "write_file", side_effect=race), \
                            mock.patch.object(configs, "transform_json_file", side_effect=observe):
                        action, detail = install()
                    self.assertEqual(len(transactions), 1)
                    if mode == "fail":
                        self.assertIn(action, ("fail", "FAIL"))
                        self.assertEqual(len(calls), 1)
                        self.assertEqual(path.read_bytes(), before)
                        self.assertNotIn("canonical wiring", detail)
                        continue
                    self.assertEqual(action, "ok" if mode == "competitor" else
                                     "applied" if name == "sync" else "update", detail)
                    self.assertEqual(len(successful), 0 if mode == "competitor" else 1)
                    self.assertEqual(len(transactions[0]["committed_metadata"]), len(successful))
                    self.assertEqual(transactions[0]["attempts"], 1 if mode == "clean" else 2)
                    got = json.loads(path.read_text())
                    leaves = [h for g in got["hooks"][posttool.EVENT] for h in g["hooks"]]
                    self.assertEqual(sum(h == ordinary for h in leaves), 1)
                    self.assertEqual(sum(h == opaque for h in leaves), 1)
                    self.assertEqual(got["hooks"][posttool.EVENT][:2], [
                        {"matcher": matcher, "hooks": [foreign]} for matcher in ("Bash", "Edit")])
                    self.assertEqual(got["hooks"][record.FAIL_EVENT], failure)
                    self.assertEqual(got["keepRoot"], [9])
                    warning = mode in ("pre-add", "post-add", "competitor")
                    self.assertEqual("REFUSED" in detail, warning)
                    self.assertNotIn("RAW_SENTINEL", detail)
                    if mode != "clean":
                        self.assertEqual(got["foreignWon"], mode)
                    self.assertTrue(hooks._lane_live(got, self.deliver))
                    self.assertEqual(posttool.covered_members(got, executable=self.bin), set())
                    frozen = path.read_bytes()
                    with mock.patch.object(envtidy, "hooks_for", return_value=("home", want)):
                        again, note = install()
                    self.assertEqual(again, "ok", note)
                    self.assertEqual("REFUSED" in note, warning)
                    self.assertEqual(path.read_bytes(), frozen)

    def test_protected_leaf_failed_write_never_claims_canonical_installation(self):
        for name in ("hooks", "project", "record", "sync"):
            with self.subTest(writer=name):
                path, install = self.writer(name)
                path.write_text(json.dumps(self.protected_settings()))
                frozen = path.read_bytes()
                with mock.patch.object(configs, "write_file", return_value={
                        "error": "SYNTHETIC_WRITE_FAILURE", "code": "write"}) as write:
                    action, detail = install()
                self.assertIn(action, ("fail", "FAIL"))
                self.assertIn("SYNTHETIC_WRITE_FAILURE", detail)
                self.assertNotIn("canonical wiring", detail)
                self.assertEqual(write.call_count, 1)
                self.assertEqual(path.read_bytes(), frozen)
                self.assertFalse(record._leg_live(json.loads(frozen), record.HOOK_EVENT))

    def test_legacy_rail_independently_preserves_nonwhole_PostToolUse_leaf(self):
        # Request ONE member so planner seeding cannot bypass the standalone
        # legacy owner. The room rail remains stubbed by setUp independently.
        for spec in (self.rec, self.deliver):
            leaf = {"type": "command", "custom": ["keep"], "command":
                    "timeout %d /old/bin/helm %s || true; printf RAW_SENTINEL"
                    % (spec["timeout"], spec["args"])}
            self.write({"hooks": {posttool.EVENT: [{"hooks": [leaf]}]}})
            with mock.patch.object(hooks, "_merge_event", wraps=hooks._merge_event) as legacy:
                action, detail = hooks.install_home(str(self.home), specs=(spec,))
            self.assertNotEqual(action, "fail", detail)
            self.assertGreater(legacy.call_count, 0)
            got = self.read()
            self.assertEqual(got["hooks"][posttool.EVENT][0], {"hooks": [leaf]})
            self.assertTrue(hooks._lane_live(got, spec))
            self.assertEqual(posttool.covered_members(got, executable=self.bin), set())
            self.assertIn("REFUSED", detail)
            self.assertNotIn("RAW_SENTINEL", detail)
            frozen = (self.home / "settings.json").read_bytes()
            self.assertEqual(hooks.install_home(str(self.home), specs=(spec,))[0], "ok")
            self.assertEqual((self.home / "settings.json").read_bytes(), frozen)

    def test_room_rail_independently_preserves_nonwhole_PostToolUse_leaf(self):
        command = "timeout 2 /synthetic/proj-wt/lane-a/bin/helm chat deliver --hook-json || true; printf RAW_SENTINEL"
        leaf = {"type": "command", "command": command, "custom": ["keep"]}
        self.write({"hooks": {posttool.EVENT: [{"hooks": [leaf]}]}})
        with mock.patch.object(hooks, "repair_lane_room_commands", _ROOM_REPAIR), \
                mock.patch.object(hooks, "stale_room_tokens", _STALE_ROOMS), \
                mock.patch.object(hooks, "_merge_event", wraps=hooks._merge_event) as legacy:
            action, detail = hooks.install_home(str(self.home), specs=())
            self.assertNotEqual(action, "fail", detail)
            legacy.assert_not_called()
        self.assertEqual(self.read()["hooks"][posttool.EVENT], [{"hooks": [leaf]}])
        self.assertIn("REFUSED", detail)
        self.assertNotIn("RAW_SENTINEL", detail)

    def test_refusal_remains_in_structurally_covered_status_and_sync_plan(self):
        self.write(self.protected_settings())
        self.assertNotEqual(record.install_home(str(self.home))[0], "fail")
        with mock.patch.object(hooks, "claude_homes", return_value=[("fake", str(self.home))]):
            row = record.status_rows()[0]
        self.assertTrue(row["hook"])
        self.assertTrue(row["posttool_refusals"])
        self.assertIn("REFUSED", row["refusal_detail"])
        plan = envtidy.plan_hooks_home("default-claude", str(self.home))
        self.assertEqual(envtidy.apply_hooks_home(plan, str(self.root / "sync-backups"))[0], "applied")
        plan = envtidy.plan_hooks_home("default-claude", str(self.home))
        self.assertEqual(plan["verdict"], "ok")
        self.assertTrue(plan["refusals"])
        self.assertNotIn("RAW_SENTINEL", plan["refusal_detail"])
        with mock.patch.object(envtidy.skillsync, "config_dirs", return_value=[("default-claude", str(self.home))]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(envtidy.cmd_hooks_sync([]), 0)
        self.assertIn("REFUSED", output.getvalue())
        self.assertNotIn("RAW_SENTINEL", output.getvalue())

    def test_covered_hooks_status_and_census_render_sanitized_refusal(self):
        self.write(self.protected_settings())
        self.assertEqual(self.writer("sync")[1]()[0], "applied")
        frozen = (self.home / "settings.json").read_bytes()
        with mock.patch.object(hooks, "claude_homes", return_value=[("fake", str(self.home))]), \
                mock.patch.object(hooks, "project_scope_rows", return_value=[]), \
                mock.patch.object(hooks, "seat_status_rows", return_value=([], [])), \
                mock.patch.object(hooks, "unproven_launches", return_value=[]), \
                mock.patch.object(hooks, "unresolved_externals", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertTrue(hooks.status_rows()[0]["deliver"])
            self.assertEqual(hooks.cmd_hooks(["status"]), 0)
        with mock.patch.object(envtidy, "home_mcps", return_value={"effective": []}), \
                mock.patch.object(envtidy, "canonical_mcps", return_value={}), \
                mock.patch.object(envtidy, "_worktree_census", return_value={"note": "fake"}):
            report = envtidy.census(dirs=[("fake", str(self.home))])
        self.assertEqual(report["homes"][0]["missing"], [])
        rendered = io.StringIO()
        envtidy._print_census(report, out=rendered)
        for text in (output.getvalue(), rendered.getvalue()):
            self.assertIn("REFUSED", text)
            self.assertIn(str(self.home), text)
            self.assertIn("non-whole-PostToolUse-member", text)
            self.assertNotIn("RAW_SENTINEL", text)
        self.assertEqual((self.home / "settings.json").read_bytes(), frozen)

    def test_installed_parser_fixture_restores_preloaded_runtime(self):
        # Whole-suite collection imports this module before executing installer
        # tests. Exercise that order without ever running recorder/delivery I/O.
        from helm import posttoolrun
        with mock.patch.object(posttoolrun, "run", side_effect=AssertionError(
                "parser fixture escaped to real runtime")) as real_run:
            self.test_installed_parser_is_lazy_and_rejects_other_selectors()
            real_run.assert_not_called()
            self.assertIs(helm.posttoolrun, posttoolrun)
            self.assertIs(sys.modules["helm.posttoolrun"], posttoolrun)
            self.assertIs(posttoolrun.run, real_run)

    def test_installed_parser_is_lazy_and_rejects_other_selectors(self):
        runtime = types.ModuleType("helm.posttoolrun")
        runtime.run = mock.Mock(return_value=0)
        # Discovery can already bind the real runtime on the parent package.
        # Relative from-import consults that binding as well as sys.modules.
        with mock.patch.dict(sys.modules, {"helm.posttoolrun": runtime}), \
                mock.patch.object(helm, "posttoolrun", runtime, create=True), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(hooks.cmd_hooks(["run", "PostToolUse", "--installed", "--hook-json"]), 0)
            runtime.run.assert_called_once_with(payload=None)
            for args in (["PostToolUseFailure", "--hook-json"], ["PostToolUse"],
                         ["PostToolUse", "--hook-json", "--tool", "Bash"],
                         ["PostToolUse", "--hook-json", "--tool", ""]):
                self.assertEqual(hooks.cmd_hooks(["run", "--installed", *args]), 2)
            runtime.run.assert_called_once_with(payload=None)
