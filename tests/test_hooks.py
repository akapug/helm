#!/usr/bin/env python3
"""helm hooks tests — the self-closing inject installer. HERMETIC BY LAW:
homes.ROOTS/DEFAULTS, configs.HOME_ROOTS and configs.BACKUP_DIR all point at
tmp dirs (the test_homes patching pattern), and HELM_HOME is a tmp dir so the
seat estate (seat_homes reads <helm_home>/_global/seats) never touches the live
~/.helm — a real install against the live ~/.claude or a live seat never runs
here."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import configs, doctor, homes, hooks  # noqa: E402


class HooksBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-hooks-")
        j = lambda *p: os.path.join(self.tmp, *p)
        self._homes_orig = {k: getattr(homes, k) for k in ("ROOTS", "DEFAULTS")}
        homes.ROOTS = {"claude": j("claude-homes"), "codex": j("codex-homes")}
        homes.DEFAULTS = {"claude": j("default-claude"), "codex": j("default-codex")}
        os.makedirs(homes.ROOTS["claude"])
        os.makedirs(homes.DEFAULTS["claude"])
        self._cfg_orig = (configs.HOME_ROOTS, configs.BACKUP_DIR)
        configs.HOME_ROOTS = [homes.DEFAULTS["claude"]]
        configs.BACKUP_DIR = j("backups")
        # HELM_HOME hermetic: seat_homes() globs <helm_home>/_global/seats — a
        # tmp home keeps the live seats out of every hooks test.
        self._helm_home_prior = os.environ.get("HELM_HOME")
        self.helm_home = j("helm-home")
        os.environ["HELM_HOME"] = self.helm_home
        self.seats_root = os.path.join(self.helm_home, "_global", "seats")

    def tearDown(self):
        for k, v in self._homes_orig.items():
            setattr(homes, k, v)
        configs.HOME_ROOTS, configs.BACKUP_DIR = self._cfg_orig
        if self._helm_home_prior is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self._helm_home_prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mk_seat(self, family, settings=None):
        """A fake seat config dir (<helm_home>/_global/seats/<family>/claude),
        registered as a home root so the gated write path accepts it — the
        live-seat analogue of mk_home."""
        d = os.path.join(self.seats_root, family, "claude")
        os.makedirs(d, exist_ok=True)
        configs.HOME_ROOTS.append(d)
        if settings is not None:
            with open(os.path.join(d, "settings.json"), "w") as f:
                json.dump(settings, f, indent=2)
        return d

    def mk_home(self, name, settings=None):
        d = os.path.join(homes.ROOTS["claude"], name)
        os.makedirs(d, exist_ok=True)
        configs.HOME_ROOTS.append(d)
        if settings is not None:
            with open(os.path.join(d, "settings.json"), "w") as f:
                json.dump(settings, f, indent=2)
        return d

    def read_settings(self, d):
        with open(os.path.join(d, "settings.json"), encoding="utf-8") as f:
            return json.load(f)

    def run_hooks(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = hooks.cmd_hooks(list(args))
        return rc, out.getvalue(), err.getvalue()


class CommandTest(HooksBase):
    def test_generated_command_shape_and_resolvable_helm(self):
        cmd = hooks.hook_command()
        self.assertIn("inject --hook-json", cmd)
        self.assertTrue(cmd.startswith("timeout "), cmd)   # never hold a turn
        self.assertTrue(cmd.endswith("|| true"), cmd)      # never block a turn
        hb = hooks.helm_bin()
        self.assertIn(hb, cmd)
        self.assertTrue(os.path.isfile(hb) and os.access(hb, os.X_OK),
                        "generated command must point at this checkout's bin/helm")
        self.assertEqual(hooks._helm_of(cmd), hb)
        self.assertTrue(hooks._resolvable(cmd))
        self.assertTrue(hooks._fail_open(cmd))


class InstallTest(HooksBase):
    def test_install_covers_every_home_and_is_idempotent(self):
        a = self.mk_home("a-user-dev")
        rc, out, err = self.run_hooks(["install"])
        self.assertEqual(rc, 0, err)
        self.assertIn("2 of 2 claude homes covered", out)
        for d in (a, homes.DEFAULTS["claude"]):
            cmds = hooks._hook_cmds(self.read_settings(d))
            self.assertEqual(cmds, [hooks.hook_command()])
        before = self.read_settings(a)
        rc, out, _ = self.run_hooks(["install"])   # re-install detects up-to-date
        self.assertEqual(rc, 0)
        self.assertIn("ok", out)
        self.assertIn("hook up to date", out)
        self.assertEqual(self.read_settings(a), before)
        self.assertEqual(len(hooks._hook_cmds(self.read_settings(a))), 1)

    def test_merge_preserves_foreign_hooks_and_settings_keys(self):
        d = self.mk_home("a-user-dev", settings={
            "model": "opus",
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                          {"type": "command", "command": "echo pre"}]}],
                      "UserPromptSubmit": [{"hooks": [
                          {"type": "command", "command": "echo other"}]}]}})
        action, _ = hooks.install_home(d)
        self.assertEqual(action, "add")
        got = self.read_settings(d)
        self.assertEqual(got["model"], "opus")
        self.assertEqual(got["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "echo pre")
        cmds = hooks._hook_cmds(got)
        self.assertIn("echo other", cmds)
        self.assertIn(hooks.hook_command(), cmds)
        self.assertEqual(len(cmds), 2)

    def test_stale_helm_entry_updated_in_place_never_doubled(self):
        d = self.mk_home("a-user-dev", settings={
            "hooks": {"UserPromptSubmit": [{"hooks": [{
                "type": "command",
                "command": "jq -r .prompt | /old/path/helm inject --project x"}]}]}})
        action, _ = hooks.install_home(d)
        self.assertEqual(action, "update")
        self.assertEqual(hooks._hook_cmds(self.read_settings(d)),
                         [hooks.hook_command()])

    def test_dry_prints_diff_writes_nothing(self):
        d = self.mk_home("a-user-dev")
        rc, out, _ = self.run_hooks(["install", "--dry"])
        self.assertEqual(rc, 0)
        self.assertIn("dry — nothing written", out)
        self.assertIn("inject --hook-json", out)   # the would-be entry, as a diff
        self.assertIn("+", out)
        self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))
        self.assertFalse(os.path.exists(
            os.path.join(homes.DEFAULTS["claude"], "settings.json")))

    def test_unparseable_settings_refused_untouched(self):
        d = self.mk_home("a-user-dev")
        with open(os.path.join(d, "settings.json"), "w") as f:
            f.write("not json{")
        action, detail = hooks.install_home(d)
        self.assertEqual(action, "fail")
        self.assertIn("refusing to touch", detail)
        with open(os.path.join(d, "settings.json")) as f:
            self.assertEqual(f.read(), "not json{")

    def test_write_failure_leaves_original_and_exits_1(self):
        self.mk_home("a-user-dev", settings={"model": "opus"})
        with mock.patch.object(configs, "write_file",
                               return_value={"error": "disk full"}):
            rc, out, _ = self.run_hooks(["install"])
        self.assertEqual(rc, 1)
        self.assertIn("fail", out)
        self.assertEqual(
            self.read_settings(os.path.join(homes.ROOTS["claude"], "a-user-dev")),
            {"model": "opus"})

    def test_post_write_corruption_restores_backup(self):
        d = self.mk_home("a-user-dev", settings={"model": "opus"})
        real = configs.write_file
        torn = []

        def torn_write(path, content):
            res = real(path, content)          # legit backup + atomic write...
            if not torn:                       # ...then the install's write lands torn
                torn.append(1)
                with open(path, "w") as f:
                    f.write("{torn")
            return res

        with mock.patch.object(configs, "write_file", torn_write):
            action, detail = hooks.install_home(d)
        self.assertEqual(action, "fail")
        self.assertIn("backup restored", detail)
        self.assertEqual(self.read_settings(d), {"model": "opus"})

    def test_home_flag_narrows_unknown_home_errors(self):
        a = self.mk_home("a-user-dev")
        rc, _, _ = self.run_hooks(["install", "--home", "a-user-dev"])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(a, "settings.json")))
        self.assertFalse(os.path.exists(
            os.path.join(homes.DEFAULTS["claude"], "settings.json")))
        rc, _, err = self.run_hooks(["install", "--home", "no-such"])
        self.assertEqual(rc, 1)
        self.assertIn("unknown claude home", err)

    def test_codex_reports_recipe_pending_writes_nothing(self):
        rc, out, _ = self.run_hooks(["install", "--harness", "codex"])
        self.assertEqual(rc, 0)
        self.assertIn("recipe pending", out)
        self.assertFalse(os.path.exists(
            os.path.join(homes.DEFAULTS["claude"], "settings.json")))

    def test_default_home_symlinked_onto_named_home_deduped(self):
        a = self.mk_home("a-user-dev")
        os.rmdir(homes.DEFAULTS["claude"])
        os.symlink(a, homes.DEFAULTS["claude"])
        self.assertEqual(hooks.claude_homes(), [("a-user-dev", os.path.realpath(a))])


class StatusTest(HooksBase):
    def test_status_table_and_coverage(self):
        a = self.mk_home("a-user-dev")
        hooks.install_home(a)
        self.mk_home("hand-wired", settings={
            "hooks": {"UserPromptSubmit": [{"hooks": [{
                "type": "command",
                "command": "/nonexistent/helm inject --hook-json"}]}]}})
        self.mk_home("bare")
        rows = {r["home"]: r for r in hooks.status_rows()}
        self.assertEqual(len(rows), 4)  # 3 named + default
        good = rows["a-user-dev"]
        self.assertTrue(good["hook"] and good["resolvable"] and good["fail_open"])
        hand = rows["hand-wired"]
        self.assertTrue(hand["hook"])
        self.assertFalse(hand["resolvable"])   # helm path does not exist
        self.assertFalse(hand["fail_open"])    # no `|| true` guard
        self.assertFalse(rows["bare"]["hook"])
        self.assertEqual(hooks.coverage(), (1, 4))
        rc, out, _ = self.run_hooks(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("inject coverage: 1 of 4 claude homes", out)
        self.assertIn("`helm hooks install` closes the gap", out)
        self.assertIn("codex: recipe pending", out)


class DeliveryLaneTest(HooksBase):
    def test_install_wires_all_events(self):
        d = self.mk_home("a-user-dev")
        action, _ = hooks.install_home(d)
        self.assertEqual(action, "add")
        got = self.read_settings(d)
        for spec in hooks.SPECS:
            self.assertIn(hooks.spec_command(spec),
                          hooks._hook_cmds(got, spec["event"]))
        # the events that take a matcher get the wildcard
        self.assertEqual(got["hooks"]["PostToolUse"][0]["matcher"], "*")
        self.assertEqual(got["hooks"]["SessionStart"][0]["matcher"], "*")
        # the continuity lane fires on EVERY trigger — no matcher key at all
        self.assertNotIn("matcher", got["hooks"]["PreCompact"][0])
        self.assertNotIn("matcher", got["hooks"]["SessionEnd"][0])
        self.assertEqual(hooks.install_home(d), ("ok", "hook up to date"))

    def test_continuity_specs_wire_handoff_check(self):
        """PreCompact + SessionEnd both carry `handoff check --hook-json` — the
        one command whose --hook-json path captures the now-snapshot AND nags."""
        names = {s["name"]: s for s in hooks.SPECS}
        for name, event in (("handoff-precompact", "PreCompact"),
                            ("handoff-sessionend", "SessionEnd")):
            spec = names[name]
            self.assertEqual(spec["event"], event)
            self.assertEqual(spec["args"], "handoff check --hook-json")
            self.assertIsNone(spec["matcher"])
            self.assertTrue(hooks.spec_command(spec).endswith(
                "handoff check --hook-json || true"))

    def test_status_reports_continuity_lane(self):
        d = self.mk_home("a-user-dev")
        hooks.install_home(d)
        rows = {r["home"]: r for r in hooks.status_rows()}
        self.assertTrue(rows["a-user-dev"]["handoff-precompact"])
        self.assertTrue(rows["a-user-dev"]["handoff-sessionend"])
        self.assertFalse(rows["(default-claude)"]["handoff-precompact"])
        rc, out, _ = self.run_hooks(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("handoff", out)               # the column header
        self.assertIn("continuity lane", out)       # the gap summary line

    def test_record_posttooluse_hook_coexists_untouched(self):
        rec = "timeout 10 /x/bin/helm record --hook-json || true"
        d = self.mk_home("a-user-dev", settings={
            "hooks": {"PostToolUse": [{"matcher": "*", "hooks": [
                {"type": "command", "command": rec}]}]}})
        action, _ = hooks.install_home(d)
        self.assertEqual(action, "add")
        cmds = hooks._hook_cmds(self.read_settings(d), "PostToolUse")
        self.assertIn(rec, cmds)   # record's leg survives byte-identical
        self.assertEqual(len(cmds), 2)

    def test_status_reports_delivery_lanes(self):
        d = self.mk_home("a-user-dev")
        hooks.install_home(d)
        rows = {r["home"]: r for r in hooks.status_rows()}
        self.assertTrue(rows["a-user-dev"]["deliver"])
        self.assertTrue(rows["a-user-dev"]["join"])
        self.assertTrue(rows["a-user-dev"]["stop-guard"])   # the idle gate
        self.assertFalse(rows["(default-claude)"]["deliver"])

    def test_wrong_matcher_on_exclusive_group_repaired_in_place(self):
        """Codex B3's exact reproduction: the exact deliver command under a
        Bash-pinned group misses most tool boundaries — status must call it
        NOT live, and install must repair the matcher."""
        deliver = next(s for s in hooks.SPECS if s["name"] == "deliver")
        d = self.mk_home("a-user-dev", settings={
            "hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": hooks.spec_command(deliver)}]}]}})
        rows = {r["home"]: r for r in hooks.status_rows()}
        self.assertFalse(rows["a-user-dev"]["deliver"])   # stale ≠ coverage
        action, _ = hooks.install_home(d)
        self.assertEqual(action, "update")
        got = self.read_settings(d)
        groups = got["hooks"]["PostToolUse"]
        self.assertEqual(len(groups), 1)                  # repaired, not doubled
        self.assertEqual(groups[0]["matcher"], "*")
        rows = {r["home"]: r for r in hooks.status_rows()}
        self.assertTrue(rows["a-user-dev"]["deliver"])
        self.assertEqual(hooks.install_home(d), ("ok", "hook up to date"))

    def test_wrong_matcher_with_foreign_cotenant_relocates_ours_only(self):
        deliver = next(s for s in hooks.SPECS if s["name"] == "deliver")
        rec = "timeout 10 /x/bin/helm record --hook-json || true"
        d = self.mk_home("a-user-dev", settings={
            "hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": rec},
                {"type": "command", "command": hooks.spec_command(deliver)}]}]}})
        action, _ = hooks.install_home(d)
        self.assertEqual(action, "update")
        groups = self.read_settings(d)["hooks"]["PostToolUse"]
        bash = [g for g in groups if g.get("matcher") == "Bash"]
        self.assertEqual(len(bash), 1)                    # foreign group survives…
        self.assertEqual([h["command"] for h in bash[0]["hooks"]], [rec])
        wild = [g for g in groups if g.get("matcher") == "*"]
        self.assertTrue(any(hooks.spec_command(deliver) == h["command"]
                            for g in wild for h in g["hooks"]))
        rows = {r["home"]: r for r in hooks.status_rows()}
        self.assertTrue(rows["a-user-dev"]["deliver"])

    def test_wrong_type_same_command_not_covered_until_repaired(self):
        """Final gate delta: the exact command+matcher under a FOREIGN type
        (type:'http') must not read as coverage — the harness would not run
        it as a command hook. Install repairs the type; only then live."""
        deliver = next(s for s in hooks.SPECS if s["name"] == "deliver")
        d = self.mk_home("a-user-dev", settings={
            "hooks": {"PostToolUse": [{"matcher": "*", "hooks": [
                {"type": "http", "command": hooks.spec_command(deliver)}]}]}})
        rows = {r["home"]: r for r in hooks.status_rows()}
        self.assertFalse(rows["a-user-dev"]["deliver"])
        action, _ = hooks.install_home(d)
        self.assertEqual(action, "update")
        got = self.read_settings(d)["hooks"]["PostToolUse"]
        self.assertEqual(len(got), 1)                    # repaired in place
        self.assertEqual(got[0]["hooks"][0]["type"], "command")
        rows = {r["home"]: r for r in hooks.status_rows()}
        self.assertTrue(rows["a-user-dev"]["deliver"])

    def test_missing_matcher_on_owned_delivery_group_repaired(self):
        join = next(s for s in hooks.SPECS if s["name"] == "join")
        d = self.mk_home("a-user-dev", settings={
            "hooks": {"SessionStart": [{"hooks": [       # no matcher key at all
                {"type": "command", "command": hooks.spec_command(join)}]}]}})
        rows = {r["home"]: r for r in hooks.status_rows()}
        self.assertFalse(rows["a-user-dev"]["join"])
        action, _ = hooks.install_home(d)
        self.assertEqual(action, "update")
        got = self.read_settings(d)["hooks"]["SessionStart"]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["matcher"], "*")


class DoctorCoverageTest(HooksBase):
    def test_doctor_coverage_warn_then_ok(self):
        self.mk_home("a-user-dev")
        res = doctor.check_inject_coverage()
        self.assertEqual(res[0][0], doctor.WARN)
        self.assertIn("inject coverage: 0 of 2 claude homes", res[0][1])
        self.assertIn("helm hooks install", res[0][1])
        rc, _, _ = self.run_hooks(["install"])
        self.assertEqual(rc, 0)
        self.assertEqual(doctor.check_inject_coverage(),
                         [(doctor.OK, "inject coverage: 2 of 2 claude homes")])


class SeatCoverageTest(HooksBase):
    """Seats (multimodel CLAUDE_CONFIG_DIRs) get the delivery lane, not inject —
    same merge-preserving / backup→validate→atomic / idempotent laws as homes."""

    def test_seat_homes_discovers_seat_config_dirs(self):
        c = self.mk_seat("codex")
        self.mk_seat("kimi")
        found = dict(hooks.seat_homes())
        self.assertEqual(set(found), {"codex", "kimi"})
        self.assertEqual(found["codex"], os.path.realpath(c))

    def test_install_wires_delivery_lane_without_dropping_foreign(self):
        foreign = "echo seat-local-hook"          # a pre-existing foreign hook
        d = self.mk_seat("codex", settings={
            "model": "gpt-5.6-sol",
            "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
                {"type": "command", "command": foreign}]}]}})
        deliver = next(s for s in hooks.SPECS if s["name"] == "deliver")
        join = next(s for s in hooks.SPECS if s["name"] == "join")
        stop = next(s for s in hooks.SPECS if s["name"] == "stop-guard")
        action, _ = hooks.install_home(d, specs=hooks.DELIVERY_SPECS)
        self.assertEqual(action, "add")
        got = self.read_settings(d)
        # the seat's own settings + foreign hook survive byte-identical
        self.assertEqual(got["model"], "gpt-5.6-sol")
        self.assertEqual(got["hooks"]["PreToolUse"][0]["hooks"][0]["command"], foreign)
        # the WHOLE delivery lane is present under the right events + matcher
        self.assertIn(hooks.spec_command(deliver), hooks._hook_cmds(got, "PostToolUse"))
        self.assertIn(hooks.spec_command(join), hooks._hook_cmds(got, "SessionStart"))
        self.assertIn(hooks.spec_command(stop), hooks._hook_cmds(got, "Stop"))
        self.assertEqual(got["hooks"]["PostToolUse"][-1]["matcher"], "*")
        self.assertEqual(got["hooks"]["SessionStart"][-1]["matcher"], "*")
        self.assertNotIn("matcher", got["hooks"]["Stop"][-1])  # Stop takes none
        # inject is NOT a seat concern — the delivery lane only
        self.assertEqual(hooks._hook_cmds(got, "UserPromptSubmit"), [])
        # idempotent: a second install detects up-to-date, writes nothing new
        self.assertEqual(hooks.install_home(d, specs=hooks.DELIVERY_SPECS),
                         ("ok", "hook up to date"))
        self.assertEqual(len(hooks._hook_cmds(got, "PostToolUse")), 1)

    def test_seat_coverage_and_status_surface_the_gap(self):
        covered = self.mk_seat("codex")
        hooks.install_home(covered, specs=hooks.DELIVERY_SPECS)
        self.mk_seat("kimi")                       # bare — no delivery hooks
        rows = {r["seat"]: r for r in hooks.seat_status_rows()}
        self.assertTrue(rows["codex"]["deliver"] and rows["codex"]["join"]
                        and rows["codex"]["stop-guard"])
        self.assertFalse(rows["kimi"]["deliver"])
        self.assertEqual(hooks.seat_coverage(), (1, 2))
        rc, out, _ = self.run_hooks(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("seats (fleet delivery", out)
        self.assertIn("stop", out)              # the Stop column is surfaced
        self.assertIn("seat delivery: 1 of 2 seats", out)

    def test_full_install_covers_every_seat_and_reports(self):
        self.mk_seat("codex")
        self.mk_seat("kimi", settings={"model": "kimi-k3"})  # foreign key present
        rc, out, err = self.run_hooks(["install"])
        self.assertEqual(rc, 0, err)
        self.assertIn("seats (fleet delivery", out)
        self.assertIn("2 of 2 seats covered", out)
        for fam in ("codex", "kimi"):
            got = self.read_settings(os.path.join(self.seats_root, fam, "claude"))
            self.assertIn("chat deliver --hook-json",
                          " ".join(hooks._hook_cmds(got, "PostToolUse")))
            self.assertIn("chat join --hook-json",
                          " ".join(hooks._hook_cmds(got, "SessionStart")))
        self.assertEqual(self.read_settings(
            os.path.join(self.seats_root, "kimi", "claude"))["model"], "kimi-k3")

    def test_home_narrowed_install_leaves_seats_untouched(self):
        self.mk_home("a-user-dev")
        self.mk_seat("codex")
        rc, out, _ = self.run_hooks(["install", "--home", "a-user-dev"])
        self.assertEqual(rc, 0)
        # a --home-scoped run never reaches the seats
        self.assertFalse(os.path.exists(os.path.join(
            self.seats_root, "codex", "claude", "settings.json")))
        self.assertEqual(hooks.seat_coverage(), (0, 1))


if __name__ == "__main__":
    unittest.main()
