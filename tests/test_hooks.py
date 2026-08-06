#!/usr/bin/env python3
"""helm hooks tests — the self-closing inject installer. HERMETIC BY LAW:
homes.ROOTS/DEFAULTS, configs.HOME_ROOTS and configs.BACKUP_DIR all point at
tmp dirs (the test_homes patching pattern), and HELM_HOME is a tmp dir so the
seat estate (seat_homes reads <helm_home>/_global/seats) never touches the live
~/.helm — a real install against the live ~/.claude or a live seat never runs
here."""
import contextlib
import errno
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import (beacons, configs, doctor, homes, hooks, seat,  # noqa: E402
                  seats, vcs)


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
        # tmp home keeps the live seats out of every hooks test. HELM_PROC +
        # HELM_CHAT_DIR hermetic too: the retrofit surface scans /proc and
        # reads the roster — tests must never touch the live ones.
        self._env_prior = {k: os.environ.get(k)
                           for k in ("HELM_HOME", "HELM_PROC", "HELM_CHAT_DIR")}
        self.helm_home = j("helm-home")
        os.environ["HELM_HOME"] = self.helm_home
        os.environ["HELM_PROC"] = j("proc")        # empty ⇒ no panes found
        os.environ["HELM_CHAT_DIR"] = j("chat")
        self.seats_root = os.path.join(self.helm_home, "_global", "seats")

    def tearDown(self):
        for k, v in self._homes_orig.items():
            setattr(homes, k, v)
        configs.HOME_ROOTS, configs.BACKUP_DIR = self._cfg_orig
        for k, v in self._env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
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

    def mk_project(self, name, settings=None):
        d = os.path.join(self.tmp, "projects", name)
        os.makedirs(os.path.join(d, ".claude"), exist_ok=True)
        if settings is not None:
            with open(os.path.join(d, ".claude", "settings.json"), "w") as f:
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


class SharedCheckoutTest(HooksBase):
    """helm_bin() must name the SHARED checkout no matter which checkout the
    running module was imported from.

    THE OUTAGE (2026-08-04, task/258): helm_bin() read
    `dirname(dirname(__file__))`, so a seat running a hook-touching verb from
    its lane room baked that room's bin/helm into every settings file the
    installer reaches — and _merge_event reaches every credential home and
    every seat config. The room was deleted when its lane closed. Eight
    settings files then named a path that did not exist, BOTH gate hooks among
    them, and a missing binary exits 127, which the old gate wrapper had no arm
    for: it fell through to `exit 0` = ALLOW, silently."""

    def mk_room(self):
        """(main_checkout, lane_room) — a synthetic repo plus a REAL linked git
        worktree in the `<repo>-wt/` sibling `helm work claim` mints. Real git,
        because the fix's whole claim is that git can fold a worktree back to
        the checkout that owns it."""
        main = os.path.realpath(os.path.join(self.tmp, "proj"))
        os.makedirs(os.path.join(main, "bin"))
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.path.join(self.tmp, "gitcfg"),
                   GIT_CONFIG_SYSTEM=os.devnull, GIT_AUTHOR_NAME="t",
                   GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@e")
        run = lambda *a: subprocess.run(("git",) + a, cwd=main, env=env,
                                        capture_output=True, check=True)
        run("init", "-q", "-b", "main")
        with open(os.path.join(main, "bin", "helm"), "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        run("add", "-A")
        run("commit", "-qm", "seed", "--no-verify")
        room = os.path.join(main + "-wt", "lane-a")
        run("worktree", "add", "-q", "-b", "lane/lane-a", room)
        return main, os.path.realpath(room)

    @contextlib.contextmanager
    def imported_from(self, checkout):
        """Run the block as if `helm.hooks` had been imported out of
        `<checkout>/helm/hooks.py` — the exact condition that poisoned the
        estate. The root memo is cleared on both edges so neither the live
        answer nor the fake one leaks across."""
        hooks._SHARED_ROOT.clear()
        try:
            with mock.patch.object(hooks, "__file__",
                                   os.path.join(checkout, "helm", "hooks.py")):
                yield
        finally:
            hooks._SHARED_ROOT.clear()

    def test_helm_bin_folds_a_lane_room_back_to_the_shared_checkout(self):
        """THE REGRESSION TEST. Confirmed RED against the pre-fix derivation:
        it returned <room>/bin/helm, so both assertions below failed."""
        main, room = self.mk_room()
        with self.imported_from(room):
            got = hooks.helm_bin()
        self.assertIn(os.sep + "bin" + os.sep + "helm", got)   # it resolved
        self.assertEqual(got, os.path.join(main, "bin", "helm"))
        self.assertNotIn("-wt" + os.sep, got)
        self.assertNotIn("lane-a", got)

    def test_the_baked_hook_command_never_names_a_room(self):
        """One level out from helm_bin: the string that actually reaches
        settings.json. Every spec, gate and advisory alike."""
        main, room = self.mk_room()
        with self.imported_from(room):
            cmds = [hooks.spec_command(s) for s in hooks.SPECS]
        self.assertEqual(len(cmds), len(hooks.SPECS))
        # MUST-HIT: the detector finds a room token when one is there. Without
        # this control an empty result below could just mean a blind detector.
        self.assertEqual(hooks.lane_room_tokens("x /p-wt/l/bin/helm"),
                         ["/p-wt/l/bin/helm"])
        blob = "\n".join(cmds)
        self.assertIn(os.path.join(main, "bin", "helm"), blob)
        self.assertIn("chat stop-guard --hook-json", blob)
        self.assertEqual(hooks.stale_room_tokens(blob), [])
        self.assertNotIn(room, blob)

    def mk_fab_room(self):
        """(mirror, room) — a worktree of a BARE mirror, under a `wt/` path.

        This is how the build fabric prepares every gate room, and it defeated
        the first version of this fix twice over: `--git-common-dir` answers
        `…/mirrors/helm.git`, whose basename is not `.git`, so automap's
        _git_root discarded the answer; and the `wt` path segment then made the
        room SHAPE reject the fallback. helm_bin raised, and because
        ProjectScopeTest evaluates helm_bin in a class body, tests/test_hooks.py
        failed to IMPORT — 44 errors on the whole-suite gate."""
        base = os.path.realpath(self.tmp)
        src = os.path.join(base, "src")
        os.makedirs(os.path.join(src, "bin"))
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.path.join(base, "gitcfg"),
                   GIT_CONFIG_SYSTEM=os.devnull, GIT_AUTHOR_NAME="t",
                   GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@e")
        run = lambda cwd, *a: subprocess.run(("git",) + a, cwd=cwd, env=env,
                                             capture_output=True, check=True)
        run(src, "init", "-q", "-b", "main")
        with open(os.path.join(src, "bin", "helm"), "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        run(src, "add", "-A")
        run(src, "commit", "-qm", "seed", "--no-verify")
        mirror = os.path.join(base, "mirrors", "helm.git")
        subprocess.run(["git", "clone", "-q", "--mirror", src, mirror],
                       env=env, capture_output=True, check=True)
        room = os.path.join(base, "wt", "lane-abc")
        run(mirror, "worktree", "add", "-q", "--detach", room, "main")
        return mirror, os.path.realpath(room)

    def test_a_worktree_of_a_bare_mirror_is_itself_canonical(self):
        """No main checkout exists to fold to, so the worktree IS the answer —
        and the room SHAPE must not overrule that measurement."""
        _mirror, room = self.mk_fab_room()
        hooks._SHARED_ROOT.clear()
        self.assertEqual(hooks._shared_root(room), room)
        self.assertTrue(hooks._in_lane_room(os.path.join(room, "bin", "helm")),
                        "control: the shape really does reject this path")
        with self.imported_from(room):
            got = hooks.helm_bin()          # must not raise
        self.assertEqual(got, os.path.join(room, "bin", "helm"))

    def test_a_real_checkout_under_a_wt_path_is_not_folded_to_its_parent(self):
        """The SILENT direction of the same defect. automap's _strip_worktree
        is a path-shape fold built for project attribution; applied here it
        returned the PARENT of a real checkout — a wrong helm_bin baked into
        every home with no error at all. The measurement must win."""
        base = os.path.realpath(self.tmp)
        checkout = os.path.join(base, "wt", "realcheckout")   # ORDINARY repo,
        os.makedirs(os.path.join(checkout, "bin"))            # merely under wt/
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.path.join(base, "gitcfg2"),
                   GIT_CONFIG_SYSTEM=os.devnull, GIT_AUTHOR_NAME="t",
                   GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@e")
        run = lambda *a: subprocess.run(("git",) + a, cwd=checkout, env=env,
                                        capture_output=True, check=True)
        run("init", "-q", "-b", "main")
        with open(os.path.join(checkout, "bin", "helm"), "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        run("add", "-A")
        run("commit", "-qm", "seed", "--no-verify")
        hooks._SHARED_ROOT.clear()
        from helm import automap
        from helm.work._lanes import find_root
        # control: the fixture really is the shape under discussion
        self.assertIn(os.sep + "wt" + os.sep, checkout)
        self.assertTrue(os.path.isdir(os.path.join(checkout, ".git")))
        # THE FOLD THAT MADE THIS WRONG, shown rather than described: the raw
        # measurement is right, and _strip_worktree is what breaks it.
        self.assertEqual(automap._git_root(checkout), checkout)
        self.assertEqual(find_root(checkout), os.path.dirname(
            os.path.dirname(checkout)), "the wrapper folded a real checkout")
        # helm_bin must follow the measurement, not the fold
        self.assertEqual(hooks._shared_root(checkout), checkout)
        with self.imported_from(checkout):
            self.assertEqual(hooks.helm_bin(),
                             os.path.join(checkout, "bin", "helm"))

    def test_shared_root_measures_rather_than_delegating_to_the_shape_fold(self):
        """REUSE OF THE PRIMITIVE, not of the wrapper. The shared seam is
        `vcs.backend().common_dir()` — `git rev-parse --git-common-dir`, which
        automap._git_root and work._lanes.find_root are both built on. This
        does NOT go through find_root: that adds automap's `_strip_worktree`
        path-shape fold on top of the measurement, and the two tests above show
        it wrong in both directions for this question.

        Patching the seam is the assertion — a second hand-rolled derivation
        would ignore the patch and still answer correctly, and two disagreeing
        answers to 'where is helm' is its own defect class (task/326)."""
        main, room = self.mk_room()
        hooks._SHARED_ROOT.clear()
        self.assertEqual(hooks._shared_root(room), main)
        hooks._SHARED_ROOT.clear()
        with mock.patch.object(vcs.GitVcs, "common_dir",
                               return_value="/sentinel/checkout/.git") as cd:
            self.assertEqual(hooks._shared_root(room), "/sentinel/checkout")
        self.assertEqual(cd.call_args[0][0], room)
        hooks._SHARED_ROOT.clear()

    def test_helm_bin_refuses_rather_than_guess_when_the_root_is_unreadable(self):  # noqa: VACUOUS_ASSERTION — the observable IS the raise, and no non-raising form of this call exists under these conditions; the two assertions below are specific (the exact refused path and the rc that made it fatal), and test_helm_bin_folds_a_lane_room_back_to_the_shared_checkout is the unconditional positive control that this same call returns normally when git answers
        """FAIL CLOSED AT THE SOURCE. With NO ANSWER AT ALL from git and the
        module sitting in a room, the honest result is a refusal — the guess is
        what reaches every home.

        The seam is what gets silenced, not a wrapper: `common_dir` returning
        None is the only condition that lets the shape decide. ANY answer,
        including a bare mirror, is trusted over the shape (the two tests
        above)."""
        _main, room = self.mk_room()
        with self.imported_from(room), \
                mock.patch.object(vcs.GitVcs, "common_dir", return_value=None):
            with self.assertRaises(hooks.HookPathError) as caught:
                hooks.helm_bin()
        msg = str(caught.exception)
        self.assertIn(os.path.join(room, "bin", "helm"), msg)
        self.assertIn("127", msg)
        # A FOOTGUN WITH A SELF-EXPLAINING ERROR IS A SUPPORT QUESTION; one
        # with a bare exception is an outage. `_in_lane_room` is a SHAPE test
        # that deliberately shares automap's fold with the resolver, so a real
        # checkout named `<x>-wt` lands here — the message must name the
        # directory, say which test rejected it, and give the exact
        # measurement that distinguishes the two cases.
        self.assertIn(room, msg)                       # the directory itself
        self.assertIn("SHAPE", msg)                    # which test rejected it
        self.assertIn("points OUTSIDE itself", msg)    # what that read means
        self.assertIn("IF THIS IS REALLY A MAIN CHECKOUT", msg)
        # THE WHOLE COMMAND, not just its distinctive flag. A message that
        # tells an operator to run a command must contain a command that runs:
        # mutating `rev-parse` to nonsense left every other assertion here
        # green (mutation M19), which is the difference between a support
        # answer and a second dead end.
        self.assertIn("git -C %s rev-parse --path-format=absolute "
                      "--git-common-dir" % room, msg)

    def test_in_lane_room_agrees_with_the_resolver_about_room_shape(self):
        """The guard's notion of a room IS automap's fold — one definition, so
        the guard and the resolver cannot drift apart."""
        self.assertTrue(hooks._in_lane_room("/a/proj-wt/lane-a/bin/helm"))
        self.assertFalse(hooks._in_lane_room("/a/proj/bin/helm"))
        for path, room in (("/a/proj-wt/lane-a/bin/helm", True),
                           ("/a/proj-worktrees/x/bin/helm", True),
                           ("/a/proj/worktrees/x/bin/helm", True),
                           ("/a/proj/bin/helm", False),
                           ("/a/projwt/bin/helm", False)):
            self.assertIs(hooks._in_lane_room(path), room, path)

    def test_lane_room_tokens_reads_tokens_not_substrings(self):
        """A room named in prose the hook ECHOES is not a room the hook RUNS."""
        self.assertEqual(
            hooks.lane_room_tokens("timeout 5 /a/proj/bin/helm chat x; "
                                   "echo 'see /a/proj-wt/lane-a' >&2"), [])
        self.assertEqual(
            hooks.lane_room_tokens("timeout 5 /a/proj-wt/lane-a/bin/helm chat x"),
            ["/a/proj-wt/lane-a/bin/helm"])


class LaneRoomRepairTest(HooksBase):
    """The rail at the POINT OF PERSISTENCE. It holds even when the derivation
    is right, because it is a different mechanism — and it REPAIRS rather than
    refuses.

    THE COUNTEREXAMPLE THAT SETTLED IT, measured on the live estate four hours
    after the outage: `hooks install` was run to clean four credential homes
    whose helm-OWNED gate entries named the deleted room, and it worked. A
    guard that hard-failed on any owned room path would have failed on exactly
    those four homes — refusing on the poison it was being run to remove —
    leaving both gates dead and the operator holding a refusal with no verb
    that cleans it."""

    POISON_ROOM = "/synthetic/proj-wt/lane-a"

    def test_install_repairs_an_owned_entry_the_spec_merge_cannot_reach(self):
        """A seat config carries the DELIVERY lane, so its `inject` entry is
        owned by helm but merged by nobody on this pass — the residue the spec
        merge structurally cannot see. It is repointed at the shared checkout,
        and the report names it."""
        d = self.mk_seat("family-x", {"hooks": {"UserPromptSubmit": [{"hooks": [
            {"type": "command",
             "command": "timeout 10 %s/bin/helm inject --hook-json || true"
                        % self.POISON_ROOM}]}]}})
        action, detail = hooks.install_home(d, specs=hooks.DELIVERY_SPECS)
        self.assertNotEqual(action, "fail", detail)
        self.assertIn("repointed at the shared checkout", detail)
        self.assertIn(self.POISON_ROOM + "/bin/helm", detail)
        got = self.read_settings(d)
        blob = "\n".join(hooks._all_hook_cmds(got))
        self.assertIn("inject --hook-json", blob)          # entry survived
        self.assertIn(hooks.helm_bin() + " inject", blob)  # repointed
        self.assertEqual(hooks.stale_room_tokens(blob), [])

    def test_BOTH_marker_bearing_entries_in_one_event_are_rewritten(self):
        """THE RETURN-ON-FIRST HOLE. _merge_event repaired the first entry
        carrying a spec's own-marker and returned, so a second one kept its
        dead path forever while the harness ran it — which also meant
        `hooks install`, the standard cure, could never clean that entry."""
        dead = "timeout 5 %s/bin/helm chat stop-guard --hook-json; exit 0"
        d = self.mk_home("home-one", {"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": dead % "/old"},
            {"type": "command", "command": dead % self.POISON_ROOM},
        ]}]}})
        action, detail = hooks.install_home(d)
        self.assertNotEqual(action, "fail", detail)
        got = self.read_settings(d)
        stops = hooks._hook_cmds(got, "Stop")
        self.assertEqual(len(stops), 2, "an entry was dropped, not rewritten")
        want = hooks.spec_command(
            next(s for s in hooks.SPECS if s["name"] == "stop-guard"))
        self.assertEqual(stops, [want, want],
                         "the second marker-bearing entry was left behind")
        self.assertEqual(hooks.stale_room_tokens("\n".join(stops)), [])

    def test_a_clean_home_still_installs(self):
        """The rail must not disturb the normal case — the assertion that keeps
        it from being a fleet-wide brick."""
        d = self.mk_home("home-two")
        action, _detail = hooks.install_home(d)
        self.assertIn(action, ("add", "update"))
        got = self.read_settings(d)
        blob = "\n".join(hooks._all_hook_cmds(got))
        self.assertIn("chat stop-guard --hook-json", blob)   # must-hit
        self.assertEqual(hooks.stale_room_tokens(blob), [])

    def test_a_foreign_hook_naming_a_room_is_named_but_never_touched(self):
        """Someone else's tool living in a worktree is their business. Scoping
        REPAIR to helm-owned commands is what keeps this from being a
        general-purpose rewrite of other people's settings — it is reported and
        left byte-identical."""
        foreign = "%s/bin/othertool --check" % self.POISON_ROOM
        d = self.mk_home("home-three", {"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": foreign}]}]}})
        action, detail = hooks.install_home(d)
        self.assertNotEqual(action, "fail", detail)
        self.assertIn("not helm's — LEFT UNTOUCHED", detail)
        self.assertIn(foreign.split()[0], detail)
        got = self.read_settings(d)
        self.assertIn(foreign, hooks._all_hook_cmds(got))

    def test_an_owned_room_path_that_is_not_a_helm_binary_is_left_and_named(self):
        """We know it is a room; we do NOT know what the right value would be.
        Guessing is worse than saying so, and hard-failing is worse than both.

        Rides the seat surface for the same reason as the residue test above: a
        spec the install DOES carry gets its whole command regenerated, which
        would erase the argument before the rail ever saw it."""
        cmd = ("timeout 10 %s inject --hook-json --data-dir %s/state || true"
               % (hooks.helm_bin(), self.POISON_ROOM))
        d = self.mk_seat("family-y", {"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": cmd}]}]}})
        action, detail = hooks.install_home(d, specs=hooks.DELIVERY_SPECS)
        self.assertNotEqual(action, "fail", detail)
        self.assertIn("no correct value is known", detail)
        self.assertIn(self.POISON_ROOM + "/state", detail)
        got = self.read_settings(d)
        self.assertIn(cmd, hooks._all_hook_cmds(got),
                      "an unrepairable entry must be left byte-identical")

    def test_the_recorder_merge_visits_every_entry_not_just_the_first(self):
        """record.py's _merge_hook carried the identical return-on-first hole.

        THE SECOND ENTRY DIFFERS BY ITS TIMEOUT, not by its path, and that is
        deliberate: a path-only difference is repairable by the room rail too,
        so the two mechanisms would mask each other and a mutation to either
        would survive. Only the MERGE can normalize a wrong timeout."""
        from helm import record
        d = self.mk_home("home-four", {"hooks": {record.HOOK_EVENTS[0]: [{
            "hooks": [
                {"type": "command",
                 "command": "timeout 10 /old/bin/helm record --hook-json || true"},
                {"type": "command",
                 "command": "timeout 99 /old/bin/helm record --hook-json || true"},
            ]}]}})
        action, detail = record.install_home(d)
        self.assertNotEqual(action, "fail", detail)
        got = self.read_settings(d)
        cmds = record._event_cmds(got, record.HOOK_EVENTS[0])
        self.assertEqual(len(cmds), 2, "an entry was dropped, not rewritten")
        self.assertEqual(cmds, [record.hook_command()] * 2,
                         "the second recorder entry was left behind")
        self.assertNotIn("timeout 99", "\n".join(cmds))

    def test_the_recorder_installer_carries_the_same_rail(self):
        """record.py composes its own command text and never calls
        spec_command — the 'a new caller bypasses the derivation' case.

        THE ROOM PATH SITS IN AN EVENT THE RECORDER DOES NOT MERGE. record
        merges HOOK_EVENTS only, so a record-owned entry parked on any other
        event is residue its merge structurally cannot reach — the only fixture
        that isolates the rail from the merge."""
        from helm import record
        parked = ("timeout 10 %s/bin/helm record --hook-json || true"
                  % self.POISON_ROOM)
        other = "SessionStart"
        self.assertNotIn(other, record.HOOK_EVENTS)      # control: really unmerged
        d = self.mk_home("home-six", {"hooks": {other: [
            {"hooks": [{"type": "command", "command": parked}]}]}})
        action, detail = record.install_home(d)
        self.assertNotEqual(action, "fail", detail)
        self.assertIn("repointed at the shared checkout", detail)
        self.assertIn(self.POISON_ROOM + "/bin/helm", detail)
        got = self.read_settings(d)
        cmds = record._event_cmds(got, other)
        self.assertEqual(cmds, [record.hook_command()])
        self.assertEqual(hooks.stale_room_tokens("\n".join(cmds)), [])

    def test_ok_is_reserved_for_no_bytes_written(self):
        """`ok` is a claim about DISK, not about intent.

        On 2026-08-04 `hooks install` printed `update … 0 failed` for four homes
        it had only PARTLY fixed, and the residue was found by re-running the
        DETECTION scan rather than by reading the verb's own summary. A path
        that rewrites bytes and reports `ok` is that disease in its purest
        form. So a room repair aggregates to `update` even when every spec was
        already current — and the next person filing that as a spurious update
        has to delete this test first."""
        d = self.mk_seat("family-z")
        hooks.install_home(d, specs=hooks.DELIVERY_SPECS)         # settle
        sp = os.path.join(d, "settings.json")
        with open(sp, encoding="utf-8") as f:
            settled = f.read()
        # CONTROL: a genuinely unchanged install says ok AND writes nothing.
        action, _detail = hooks.install_home(d, specs=hooks.DELIVERY_SPECS)
        self.assertEqual(action, "ok")
        with open(sp, encoding="utf-8") as f:
            self.assertEqual(f.read(), settled, "`ok` wrote bytes")
        # Now the ONLY pending change is a room repair, on an entry this
        # install does not merge — every DELIVERY spec stays current.
        doc = json.loads(settled)
        doc["hooks"]["UserPromptSubmit"] = [{"hooks": [{
            "type": "command",
            "command": "timeout 10 %s/bin/helm inject --hook-json || true"
                       % self.POISON_ROOM}]}]
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
        with open(sp, encoding="utf-8") as f:
            before = f.read()
        action, detail = hooks.install_home(d, specs=hooks.DELIVERY_SPECS)
        self.assertEqual(action, "update", detail)
        with open(sp, encoding="utf-8") as f:
            after = f.read()
        self.assertIn("inject --hook-json", after)     # must-hit: entry survived
        self.assertNotEqual(after, before, "reported a change it did not make")
        self.assertNotIn(self.POISON_ROOM, after)

    def test_the_recorder_ok_is_reserved_for_no_bytes_written_too(self):
        """The recorder needs its OWN arm for the `ok`-means-no-bytes law.

        Its sibling above starts from an empty home, so both recorder legs are
        ADDED and the action is `add` however the room arm behaves — the law is
        untestable there. Settling the legs first is what makes a room repair
        the only pending change, and only then does `ok` vs `update` discriminate.
        (Found by mutation: forcing the recorder's room arm to report `ok` was
        the one mutant that survived the first matrix.)"""
        from helm import record
        d = self.mk_home("home-seven")
        record.install_home(d)                                    # settle
        sp = os.path.join(d, "settings.json")
        with open(sp, encoding="utf-8") as f:
            settled = f.read()
        action, _detail = record.install_home(d)
        self.assertEqual(action, "ok")
        with open(sp, encoding="utf-8") as f:
            self.assertEqual(f.read(), settled, "`ok` wrote bytes")
        parked = "SessionStart"
        self.assertNotIn(parked, record.HOOK_EVENTS)   # control: really unmerged
        doc = json.loads(settled)
        doc["hooks"][parked] = [{"hooks": [{
            "type": "command",
            "command": "timeout 10 %s/bin/helm record --hook-json || true"
                       % self.POISON_ROOM}]}]
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
        with open(sp, encoding="utf-8") as f:
            before = f.read()
        action, detail = record.install_home(d)
        self.assertEqual(action, "update", detail)
        with open(sp, encoding="utf-8") as f:
            after = f.read()
        self.assertIn("record --hook-json", after)     # must-hit: entry survived
        self.assertNotEqual(after, before, "reported a change it did not make")
        self.assertNotIn(self.POISON_ROOM, after)

    def test_helm_living_under_a_wt_path_is_not_its_own_poison(self):
        """THE BUILD NODE'S SHAPE, as a local arm.

        fab checks every gate room out at `…/fab/wt/<lane>/`, so helm's OWN
        canonical bin/helm is room-SHAPED there. Counting it as poison made
        `_rooms_clean` reject helm's own answer, `verify` failed on EVERY
        install, and 51 tests went red on the node while every one of them
        passed here — because this box's helm is not under a `wt/` directory.
        The exemption existed in the repair loop and in neither of the two
        sibling sites that ask the same question, so the cure is one shared
        predicate and this arm, which does not depend on where helm lives."""
        fake = os.path.join(self.tmp, "fab", "wt", "lane-x", "bin", "helm")
        with mock.patch.object(hooks, "helm_bin", return_value=fake):
            # control: the SHAPE really does flag helm's own path here
            self.assertTrue(hooks._in_lane_room(fake))
            self.assertEqual(hooks.lane_room_tokens("timeout 5 %s chat x" % fake),
                             [fake])
            # …and the POISON predicate exempts it, at all three sites
            self.assertEqual(hooks.stale_room_tokens("timeout 5 %s chat x" % fake),
                             [])
            d = self.mk_home("home-eight")
            action, detail = hooks.install_home(d)
            self.assertIn(action, ("add", "update"), detail)
            again, detail2 = hooks.install_home(d)
            self.assertEqual(again, "ok", detail2)
        got = self.read_settings(d)
        self.assertIn(fake, "\n".join(hooks._all_hook_cmds(got)))

    def test_the_report_separates_the_three_outcomes(self):
        """A single hard fail hid which was which. The report is a pure
        function of the notes, so it can be asserted without a filesystem."""
        line = hooks.lane_room_report([
            ("repaired", "Stop", "/a/p-wt/l/bin/helm"),
            ("foreign", "Stop", "/a/p-wt/l/bin/othertool"),
            ("unrepairable", "PreToolUse", "/a/p-wt/l/state")])
        self.assertIn("repointed at the shared checkout: /a/p-wt/l/bin/helm", line)
        self.assertIn("not helm's — LEFT UNTOUCHED: /a/p-wt/l/bin/othertool", line)
        self.assertIn("no correct value is known", line)
        self.assertIn("/a/p-wt/l/state (PreToolUse)", line)
        self.assertIsNone(hooks.lane_room_report([]))

    def test_a_repair_that_does_not_take_is_the_one_fatal_arm(self):
        """The fail-closed backstop. Unreachable from any operator state — only
        a bug in the swap can trip it — so it is the only thing that raises."""
        settings = {"hooks": {"Stop": [{"hooks": [{
            "type": "command",
            "command": "timeout 5 %s/bin/helm chat stop-guard --hook-json"
                       % self.POISON_ROOM}]}]}}
        with mock.patch.object(hooks, "_swap_room_token",
                               side_effect=lambda cmd, *a: cmd):
            with self.assertRaises(hooks.HookPathError) as caught:
                hooks.repair_lane_room_commands(settings, "/tmp/s.json")
        self.assertIn("INTERNAL", str(caught.exception))
        self.assertIn(self.POISON_ROOM + "/bin/helm", str(caught.exception))


class GateFailureVisibilityTest(HooksBase):
    """A guard that COULD NOT RUN is a strictly worse state than one that timed
    out, and only the timeout was ever reported. The arm set is exhaustive now:
    0 passes, 2 blocks, everything else says what happened, and the fail-open
    law is untouched."""

    @property
    def SPEC(self):
        """THE REAL Stop gate spec, with only the timeout shortened so the
        kill arm does not cost 5s. Derived rather than hand-copied: a literal
        duplicate could drift from SPECS and keep asserting about a shape helm
        no longer generates."""
        base = next(s for s in hooks.SPECS if s["name"] == "stop-guard")
        self.assertTrue(base.get("gate"))          # control: still a gate
        return dict(base, timeout=2)

    def render(self, binpath):
        with mock.patch.object(hooks, "helm_bin", return_value=binpath):
            return hooks.spec_command(self.SPEC)

    def run_proc(self, body=None, binpath=None):
        """The WHOLE result, stdout included. `run_gate` keeps its two-value
        shape for the arms that only care about the subprocess's own
        complaint; the harness-visibility arms need the third channel,
        because stdout is the ONLY one the harness reads on exit 0."""
        if binpath is None:
            binpath = os.path.join(self.tmp, "stub-helm")
            with open(binpath, "w") as f:
                f.write("#!/bin/sh\n%s\n" % body)
            os.chmod(binpath, 0o755)
        return subprocess.run(["sh", "-c", self.render(binpath)],
                              capture_output=True, text=True)

    def run_gate(self, body=None, binpath=None):
        p = self.run_proc(body, binpath)
        return p.returncode, p.stderr

    def test_a_missing_guard_binary_says_so(self):
        """THE OUTAGE'S SILENT LEG, executed. `timeout` exits 127 for a binary
        that is not there; 127 is neither 2 nor 124, so the old wrapper fell
        straight through to `exit 0` and four credential homes ran unguarded
        with nothing said."""
        gone = os.path.join(self.tmp, "deleted-room", "bin", "helm")
        rc, err = self.run_gate(binpath=gone)
        self.assertEqual(rc, 0)                       # fail-open law unchanged
        self.assertIn("THE GUARD IS MISSING", err)
        self.assertIn(gone, err)
        self.assertIn("this stop is ALLOWED and UNCHECKED", err)
        self.assertIn("helm hooks install", err)

    def test_an_unknown_return_code_says_so(self):
        """The default arm exists so the NEXT unhandled code is loud by
        construction rather than after the next outage."""
        rc, err = self.run_gate("exit 1")
        self.assertEqual(rc, 0)
        self.assertIn("THE GUARD FAILED rc=1", err)
        self.assertIn("this stop is ALLOWED and UNCHECKED", err)
        for code in (3, 42):
            rc, err = self.run_gate("exit %d" % code)
            self.assertEqual(rc, 0, code)
            self.assertIn("THE GUARD FAILED rc=%d" % code, err)
            self.assertIn("this stop is ALLOWED and UNCHECKED", err)

    def test_a_refusal_still_reaches_the_harness_and_a_pass_is_silent(self):  # noqa: VACUOUS_ASSERTION — the ABSENCE of a message IS the contract on these two arms (a BLOCK and a clean pass must stay quiet, or the new legs would put a scary line under every ordinary turn), so no positive control on their `err` can exist; rc==2 and rc==0 on the same runs are the unconditional positive controls, and the loud run below proves this same stderr channel does carry text on this same command shape
        """The two arms that must not regress while the others gain a voice."""
        # CONTROL on the observable itself: `err` is a live channel on this
        # very command shape, so the two silences below are the product's
        # choice and not a stderr that never carries anything.
        _rc, loud = self.run_gate(binpath=os.path.join(self.tmp, "gone", "helm"))
        self.assertIn("ALLOWED and UNCHECKED", loud)
        rc, err = self.run_gate("exit 2")
        self.assertEqual(rc, 2)
        self.assertEqual(err, "")
        rc, err = self.run_gate("exit 0")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")

    def test_the_alarm_reaches_the_HARNESS_and_not_only_the_debug_log(self):
        """@codex's finding, and it invalidated every arm above as a proof of
        VISIBILITY. Those arms capture the subprocess's OWN stderr, which
        proves the text was written — not that anybody can read it. The
        Claude Code hook contract: "Stderr from a hook that exits 0 goes to
        the debug log only, never the transcript, and Claude never sees it."
        Every leg here exits 0 by the fail-open law, so all three warnings
        went into a void, including the one written to cure the 2026-08-04
        outage.

        The channel that IS read on exit 0 is JSON on stdout — "JSON output
        is only processed on exit 0" — so this arm parses it rather than
        substring-matching, and asserts BOTH readers: `systemMessage` for the
        owner and `hookSpecificOutput.additionalContext` for Claude."""
        event = self.SPEC["event"]

        def harness_doc(**kw):
            """The rc and the document the harness would ACT on. The parse
            and the subscripts are load-bearing: an unparseable or truncated
            document raises here rather than reducing to something an
            assertion could still be satisfied by."""
            p = self.run_proc(**kw)
            return p.returncode, json.loads(p.stdout)

        def routed(doc):
            """(event the harness routes on, owner text == agent text)."""
            hso = doc["hookSpecificOutput"]
            return hso["hookEventName"], hso["additionalContext"]

        # Each leg is checked for ITS OWN sentence, not merely for some
        # sentence — a timeout reported as a missing binary is a different
        # diagnosis and would send the owner to the wrong repair. The event
        # must be the SPEC's own because the harness ROUTES on it, and the
        # agent's copy must equal the owner's, since two hand-kept copies is
        # how a hardcoded "this stop" reached a PreToolUse reader.
        rc, doc = harness_doc(binpath=os.path.join(self.tmp, "deleted-room",
                                                   "bin", "helm"))
        self.assertIn("THE GUARD IS MISSING", doc["systemMessage"],
                      "a MISSING guard reached nobody")
        self.assertEqual(rc, 0, "fail-open law unchanged")
        self.assertEqual(routed(doc), (event, doc["systemMessage"]))

        rc, doc = harness_doc(body="sleep 30")
        self.assertIn("THE GUARD TIMED OUT", doc["systemMessage"],
                      "a TIMED-OUT guard reached nobody")
        self.assertEqual(rc, 0, "fail-open law unchanged")
        self.assertEqual(routed(doc), (event, doc["systemMessage"]))

        rc, doc = harness_doc(body="exit 42")
        self.assertIn("THE GUARD FAILED rc=42", doc["systemMessage"],
                      "an UNKNOWN-code guard reached nobody, or the shell's "
                      "rc never reached the owner-visible text")
        self.assertEqual(rc, 0, "fail-open law unchanged")
        self.assertEqual(routed(doc), (event, doc["systemMessage"]))

    def test_the_JSON_carries_the_REAL_return_code_and_stays_quiet_when_ok(self):
        """Two claims the arm above cannot make. First: `$rc` is spliced by
        the SHELL, so the JSON has to survive a value helm never saw at
        render time — a format bug here would ship a literal placeholder to
        the owner. Second, and this is the control: a clean pass and a BLOCK
        must emit NO JSON at all, or "the harness got JSON" stops
        discriminating and every assertion above is satisfied by a hook that
        shouts on every single turn."""
        p = self.run_proc("exit 42")
        self.assertIn("rc=42", json.loads(p.stdout)["systemMessage"],
                      "the shell's rc never reached the owner-visible text")

        # CONTROLS on the same observable, same command shape.
        p = self.run_proc("exit 0")
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""),
                         "a clean pass must stay silent on every channel")
        p = self.run_proc("exit 2")
        self.assertEqual((p.returncode, p.stdout, p.stderr), (2, "", ""),
                         "a BLOCK must reach the harness as rc 2 with no "
                         "JSON — stdout is IGNORED on exit 2, so emitting "
                         "there would drop the refusal's reason entirely")

    def test_a_hostile_checkout_path_cannot_break_the_JSON(self):
        """The path now rides inside a JSON string as well as a shell one, so
        it has two escapers to get past instead of one. A quote or backslash
        in a checkout path would produce a document the harness cannot parse
        — which fails exactly like silence, the state this whole class
        exists to end."""
        nasty = os.path.join(self.tmp, 'ro"om\\back$x`id`', "bin", "helm")
        p = self.run_proc(binpath=nasty)
        self.assertEqual(p.returncode, 0)
        self.assertTrue(p.stdout.strip(),
                        "the alarm must reach stdout at all — an unparseable "
                        "document and an absent one fail identically")
        doc = json.loads(p.stdout)              # the assertion IS the parse
        self.assertIn(nasty, doc["systemMessage"],
                      "the path must survive BOTH escapers intact")
        self.assertNotIn("uid=", doc["systemMessage"],
                         "`id` was executed — the path became shell SOURCE")

    def test_a_killed_guard_still_says_it_timed_out(self):
        rc, err = self.run_gate("sleep 30")
        self.assertEqual(rc, 0)
        self.assertIn("THE GUARD TIMED OUT after 2s", err)
        self.assertIn("this stop is ALLOWED and UNCHECKED", err)

    def test_the_path_is_shell_data_not_shell_source(self):
        """helm has watched a shell eat backticked content out of a message
        body three times in two days — the argv-guard exists for it. A checkout
        path is interpolated into a string the harness RUNS, so it is quoted on
        both legs: the argv and the diagnostic."""
        canary = os.path.join(self.tmp, "pwned")
        # CONTROL: the probe below can actually see this file when it is there.
        with open(canary, "w") as f:
            f.write("x")
        self.assertTrue(os.path.exists(canary))
        os.remove(canary)
        nasty = os.path.join(self.tmp, "ro`touch %s`om" % canary, "bin", "helm")
        rc, err = self.run_gate(binpath=nasty)
        self.assertEqual(rc, 0)
        self.assertIn("THE GUARD IS MISSING", err)
        self.assertFalse(os.path.exists(canary),
                         "the wrapper executed a path as shell source")


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

    def test_post_commit_foreign_write_is_preserved_and_retried(self):
        d = self.mk_home("a-user-dev", settings={"model": "opus"})
        real = configs.write_file
        raced = []

        def foreign_after_commit(path, content, expected_revision=None):
            res = real(path, content, expected_revision=expected_revision)
            if res.get("ok") and not raced:
                raced.append(1)
                with open(path, encoding="utf-8") as f:
                    foreign = json.load(f)
                foreign["external"] = {"won": True}
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(foreign, f, indent=2)
            return res

        with mock.patch.object(configs, "write_file", foreign_after_commit):
            action, detail = hooks.install_home(d)
        self.assertEqual(action, "add")
        self.assertIn("CAS attempts: 2", detail)
        got = self.read_settings(d)
        self.assertEqual(got["model"], "opus")
        self.assertEqual(got["external"], {"won": True})
        self.assertTrue(all(hooks._lane_live(got, s) for s in hooks.SPECS))

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


class ProjectScopeTest(HooksBase):
    ORPHAN = {
        "hooks": {"UserPromptSubmit": [{"hooks": [{
            "type": "command",
            "command": "jq -r .prompt 2>/dev/null | %s inject --project helm "
                       "2>/dev/null || true" % hooks.helm_bin(),
        }]}]},
    }

    def contexts(self, project, active_home):
        return ([{"root": project, "homes": {active_home}}], None)

    def scoped(self, project, active_home):
        return mock.patch.object(hooks, "_project_contexts",
                                 return_value=self.contexts(project, active_home))

    def test_live_orphan_positive_control_reports_cross_scope_duplicate(self):
        """The removed hook-A shape, reminted with this tree, is found in TEMP.

        This is the mandatory known-positive: a scanner that has only ever
        returned clean is not evidence that the live duplicate class is gone."""
        active = self.mk_home("active")
        hooks.install_home(active)
        project = self.mk_project("orphan", self.ORPHAN)
        with self.scoped(project, active):
            rows = hooks.project_scope_rows()
            self.assertEqual([r["status"] for r in rows], ["duplicate"])
            self.assertEqual(rows[0]["duplicates"][0]["specs"], ["inject"])
            rc, out, _ = self.run_hooks(["status"])
            self.assertEqual(rc, 0)
            self.assertIn("cross-scope DUPLICATE", out)
            self.assertIn(os.path.join(project, ".claude", "settings.json"), out)
            warns = doctor.check_hook_scopes()
            self.assertEqual(len(warns), 1)
            self.assertEqual(warns[0][0], doctor.WARN)
            self.assertIn("cross-scope DUPLICATE", warns[0][1])

    def test_clean_project_is_silent(self):
        active = self.mk_home("active")
        hooks.install_home(active)
        project = self.mk_project("clean", {
            "hooks": {"UserPromptSubmit": [{"hooks": [{
                "type": "command", "command": "echo foreign"}]}]}})
        with self.scoped(project, active):
            self.assertEqual(hooks.project_scope_rows(), [])
            rc, out, _ = self.run_hooks(["status"])
            self.assertEqual(rc, 0)
            self.assertNotIn("cross-scope DUPLICATE", out)
            self.assertEqual(doctor.check_hook_scopes(), [])

    def test_project_only_hook_is_reported_but_not_called_a_defect(self):
        active = self.mk_home("active")
        project = self.mk_project("manual", self.ORPHAN)
        with self.scoped(project, active):
            rows = hooks.project_scope_rows()
            self.assertEqual([r["status"] for r in rows], ["project-only"])
            rc, out, _ = self.run_hooks(["status"])
            self.assertEqual(rc, 0)
            self.assertIn("project hook only", out)
            self.assertNotIn("cross-scope DUPLICATE", out)
            self.assertEqual(doctor.check_hook_scopes(), [])

    def test_install_reports_duplicate_without_deleting_project_hook(self):
        active = self.mk_home("active")
        project = self.mk_project("orphan", self.ORPHAN)
        path = os.path.join(project, ".claude", "settings.json")
        with open(path, encoding="utf-8") as f:
            before = f.read()
        with self.scoped(project, active):
            rc, out, err = self.run_hooks(["install"])
        self.assertEqual(rc, 0, err)
        self.assertIn("cross-scope DUPLICATE", out)
        with open(path, encoding="utf-8") as f:
            after = f.read()
        self.assertEqual(after, before,
                         "install auto-deleted a deliberately-authored project hook")

    def test_wrong_shaped_settings_are_unknown_not_clean(self):
        active = self.mk_home("active", settings=[])
        project = self.mk_project("orphan", self.ORPHAN)
        with self.scoped(project, active):
            rows = hooks.project_scope_rows()
        self.assertEqual([r["status"] for r in rows], ["unknown"])
        self.assertIn("root is not an object", rows[0]["detail"])

        bad_project = self.mk_project("bad-project")
        with open(os.path.join(bad_project, ".claude", "settings.json"), "w") as f:
            json.dump([], f)
        with self.scoped(bad_project, active):
            rows = hooks.project_scope_rows()
        self.assertEqual([r["status"] for r in rows], ["unknown"])
        self.assertIn("root is not an object", rows[0]["detail"])

    def test_status_scans_projects_even_when_no_home_rows_exist(self):
        row = {"status": "unknown", "detail": "known-positive scan"}
        with mock.patch.object(hooks, "status_rows", return_value=[]), \
                mock.patch.object(hooks, "project_scope_rows", return_value=[row]):
            rc, out, _ = self.run_hooks(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("no claude homes found", out)
        self.assertIn("project hook scan UNKNOWN", out)

    def test_non_git_cwd_never_promotes_user_claude_dir_to_project_scope(self):
        home_dir = os.path.join(self.tmp, "user-home")
        cwd = os.path.join(home_dir, "work")
        os.makedirs(os.path.join(home_dir, ".claude"))
        os.makedirs(cwd)
        self.assertEqual(hooks._checkout_root(cwd), os.path.realpath(cwd))

    def test_checkout_root_preserves_linked_worktree_root(self):
        root = os.path.join(self.tmp, "linked")
        cwd = os.path.join(root, "nested")
        os.makedirs(cwd)
        with open(os.path.join(root, ".git"), "w") as f:
            f.write("gitdir: /tmp/example\n")
        self.assertEqual(hooks._checkout_root(cwd), os.path.realpath(root))

    def test_home_cwd_does_not_reclassify_user_settings_as_project_settings(self):
        user = os.path.join(self.tmp, "user")
        config = os.path.join(user, ".claude")
        os.makedirs(config)
        with open(os.path.join(config, "settings.json"), "w") as f:
            json.dump(self.ORPHAN, f)
        contexts = [{"root": user, "homes": set()}]
        with mock.patch.dict(homes.DEFAULTS, {"claude": config}):
            self.assertEqual(hooks.project_scope_rows(contexts), [])

    def test_contexts_scan_current_and_live_seats_not_absent_history(self):
        current = self.mk_project("current")
        live = self.mk_project("live")
        absent = self.mk_project("absent")
        active = self.mk_home("active")
        roster = {"live": {"cwd": live, "seen": "fresh"},
                  "gone": {"cwd": absent, "seen": "absent"}}
        with mock.patch.object(seats, "safe_cwd", return_value=current), \
                mock.patch.object(seats, "roster_checked", return_value=(roster, False)), \
                mock.patch.object(seats, "unverified_seats", return_value={}), \
                mock.patch.object(seats, "last_seen",
                                  side_effect=lambda _name, row: row["seen"]), \
                mock.patch.object(seats, "presence_with_identity",
                                  side_effect=lambda seen, _conflict: seen), \
                mock.patch.object(hooks, "running_panes", return_value=[
                    {"seat": "live", "config_dir": active}]), \
                mock.patch.object(hooks, "_checkout_root", side_effect=lambda p: p), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": active}):
            rows, unknown = hooks._project_contexts()
        self.assertIsNone(unknown)
        self.assertEqual({r["root"] for r in rows}, {current, live})
        self.assertNotIn(absent, {r["root"] for r in rows})

    def test_unreadable_roster_is_unknown_not_clean(self):
        current = self.mk_project("current")
        active = self.mk_home("active")
        with mock.patch.object(seats, "safe_cwd", return_value=current), \
                mock.patch.object(seats, "roster_checked", return_value=({}, True)), \
                mock.patch.object(hooks, "_checkout_root", side_effect=lambda p: p), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": active}):
            rows = hooks.project_scope_rows()
        self.assertEqual(rows[0]["status"], "unknown")
        self.assertIn("roster unreadable", rows[0]["detail"])


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

    def test_gate_timeout_message_names_the_events_own_stake(self):
        """ONE template renders every gate spec, and the hardcoded 'this
        stop' told the argv-guard's PreToolUse reader a STOP had been allowed
        when what actually went unchecked was a tool call. The noun comes
        from the spec's event; an event outside the table renders as
        '<Event> event' — vague but never wrong."""
        names = {s["name"]: s for s in hooks.SPECS}
        stop_cmd = hooks.spec_command(names["stop-guard"])
        argv_cmd = hooks.spec_command(names["argv-guard"])
        self.assertIn("this stop is ALLOWED and UNCHECKED", stop_cmd)
        self.assertIn("this tool call is ALLOWED and UNCHECKED", argv_cmd)
        self.assertNotIn("this stop", argv_cmd)
        fake = {"name": "x-gate", "event": "SessionStart", "args": "chat x",
                "timeout": 2, "gate": True}
        self.assertIn("this SessionStart event is ALLOWED and UNCHECKED",
                      hooks.spec_command(fake))

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

    def test_TWO_orphans_relocate_into_ONE_canonical_group(self):
        """N relocations must not become N duplicate entries.

        The single-orphan case above cannot see this: with one orphan, "append
        once" and "append per orphan" are the same program. Visiting every
        marker-bearing entry (rather than returning after the first) is what
        makes N > 1 reachable at all, so the arithmetic needs its own arm."""
        deliver = next(s for s in hooks.SPECS if s["name"] == "deliver")
        cmd = hooks.spec_command(deliver)
        foreign_a = "timeout 10 /x/bin/othertool --a"
        foreign_b = "timeout 10 /x/bin/othertool --b"
        d = self.mk_home("a-user-dev", settings={
            "hooks": {"PostToolUse": [
                {"matcher": "Bash", "hooks": [
                    {"type": "command", "command": foreign_a},
                    {"type": "command", "command": cmd}]},
                {"matcher": "Edit", "hooks": [
                    {"type": "command", "command": foreign_b},
                    {"type": "command", "command": cmd}]}]}})
        action, _ = hooks.install_home(d)
        self.assertEqual(action, "update")
        groups = self.read_settings(d)["hooks"]["PostToolUse"]
        ours = [h for g in groups for h in g.get("hooks") or []
                if h.get("command") == cmd]
        self.assertEqual(len(ours), 1,
                         "two orphans produced %d canonical entries" % len(ours))
        wild = [g for g in groups if g.get("matcher") == "*"]
        self.assertEqual(len(wild), 1, "one canonical group, not one per orphan")
        # both foreign groups survive byte-identical, minus only our entry
        self.assertEqual(
            [[h["command"] for h in g["hooks"]]
             for g in groups if g.get("matcher") in ("Bash", "Edit")],
            [[foreign_a], [foreign_b]])
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
        # NOT-DOUBLED is the claim, and it is asserted about the join COMMAND,
        # not about the length of the SessionStart list: helm now wires two
        # SessionStart specs (join + resume-turn), so a raw length of 1 would
        # pin the spec count instead of the repair. Exactly one group may carry
        # join's command, that group's matcher must be repaired to `*`, and the
        # list must hold exactly one group per SessionStart spec.
        starts = [s for s in hooks.SPECS if s["event"] == "SessionStart"]
        mine = [g for g in got if any(
            h.get("command") == hooks.spec_command(join) for h in g["hooks"])]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["matcher"], "*")
        self.assertEqual(len(got), len(starts))


class BeaconPermitTest(HooksBase):
    """G-beacon-autopermit: install merges the beacon allow rules into
    permissions.allow on every home AND seat — additive, idempotent, never
    dropping an existing entry — so a fresh session's mandatory
    `Monitor(helm chat wait … --follow)` first action never hangs on a human
    permission prompt."""

    def test_install_grants_beacon_permits_idempotently(self):
        d = self.mk_home("a-user-dev", settings={
            "model": "opus",
            "permissions": {"allow": ["Bash(git:*)"], "deny": ["WebFetch"],
                            "defaultMode": "acceptEdits"}})
        action, _ = hooks.install_home(d)
        self.assertEqual(action, "add")
        got = self.read_settings(d)
        allow = got["permissions"]["allow"]
        self.assertIn("Bash(git:*)", allow)              # nothing dropped
        for rule in hooks.PERMIT_RULES:
            self.assertIn(rule, allow)
        self.assertEqual(got["permissions"]["deny"], ["WebFetch"])
        self.assertEqual(got["permissions"]["defaultMode"], "acceptEdits")
        # idempotent: re-install adds nothing, duplicates nothing
        self.assertEqual(hooks.install_home(d), ("ok", "hook up to date"))
        allow2 = self.read_settings(d)["permissions"]["allow"]
        self.assertEqual(allow2, allow)
        self.assertEqual(len(allow2), len(set(allow2)))

    def test_seat_with_no_permissions_key_gains_one(self):
        d = self.mk_seat("kimi", settings={"model": "kimi-k3"})
        action, _ = hooks.install_home(d, specs=hooks.DELIVERY_SPECS)
        self.assertEqual(action, "add")
        got = self.read_settings(d)
        self.assertEqual(got["model"], "kimi-k3")
        self.assertEqual(got["permissions"]["allow"], list(hooks.PERMIT_RULES))
        self.assertEqual(hooks.install_home(d, specs=hooks.DELIVERY_SPECS),
                         ("ok", "hook up to date"))

    def test_permits_alone_missing_still_triggers_an_install_write(self):
        """A home whose hooks are current but whose allow rules are absent is
        NOT up to date — the exact live gap (hooks installed before this fix)."""
        d = self.mk_home("a-user-dev")
        hooks.install_home(d)
        got = self.read_settings(d)
        got["permissions"]["allow"] = ["Bash(git:*)"]   # someone pruned ours
        with open(os.path.join(d, "settings.json"), "w") as f:
            json.dump(got, f)
        action, _ = hooks.install_home(d)
        self.assertEqual(action, "add")
        allow = self.read_settings(d)["permissions"]["allow"]
        self.assertIn("Bash(git:*)", allow)
        for rule in hooks.PERMIT_RULES:
            self.assertIn(rule, allow)

    def test_broken_permissions_shape_refused_untouched(self):
        d = self.mk_home("a-user-dev", settings={"permissions": "nope"})
        action, detail = hooks.install_home(d)
        self.assertEqual(action, "fail")
        self.assertIn("permissions", detail)
        self.assertEqual(self.read_settings(d), {"permissions": "nope"})

    def test_status_surfaces_the_permit_gap(self):
        d = self.mk_home("a-user-dev")
        hooks.install_home(d)
        rows = {r["home"]: r for r in hooks.status_rows()}
        self.assertTrue(rows["a-user-dev"]["permits"])
        self.assertFalse(rows["(default-claude)"]["permits"])
        rc, out, _ = self.run_hooks(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("beacon permit", out)


class WorkflowDefaultsTest(HooksBase):
    """AS-PREVENTED (owner directive 2026-07-29: "WORKFLOWS ENABLED, DEFAULT SIZE
    SMALL, ON ALL LAUNCH SCRIPTS / ALL AGENTS"). install_home is the ONE codepath
    that spans BOTH launch surfaces — the orca-launched claude seats' credhomes
    (claude_homes) and the helm-minted family seats' config dirs (seat_homes) —
    so wiring the default there and asserting it here PER FAMILY means a future
    edit that drops it fails the suite. Default-small is the gap this wires;
    workflows-ENABLED is already universal via the GrowthBook feature cache
    (seat._FEATURE_CACHE_KEYS), asserted per family below so both halves are
    pinned. See the launch-surface diagnosis for why the two seed functions each
    reach only the family-seat half."""

    def _complete_cache(self):
        """A cache that satisfies seat._feature_cache_complete AND carries the
        workflows-enabled flag — the wholesale blob a fresh seat inherits."""
        feats = {"f%03d" % i: True
                 for i in range(seat._FEATURE_CACHE_MIN_FEATURES)}
        feats[seat._FEATURE_CACHE_GATE] = True
        feats["tengu_workflows_enabled"] = True
        return {"cachedGrowthBookFeatures": feats,
                "cachedExperimentFeatures": [],
                "cachedGrowthBookFeaturesAt": time.time() * 1000}

    def test_every_family_seat_gets_default_small_via_install_home(self):
        for family in sorted(seat.FAMILIES):
            with self.subTest(family=family):
                d = self.mk_seat(family)
                action, detail = hooks.install_home(
                    d, specs=hooks.DELIVERY_SPECS)
                self.assertNotEqual(action, "fail", detail)
                self.assertEqual(
                    self.read_settings(d).get("workflowSizeGuideline"), "small",
                    "family %s seat did not get default-small" % family)

    def test_default_small_overrides_a_drifted_medium(self):
        # a seat left at CC's medium default is CORRECTED, not preserved: the
        # estate owns this key (measured drift: a credhome sat at medium). Every
        # sibling settings key survives.
        d = self.mk_seat("codex", settings={"workflowSizeGuideline": "medium",
                                            "model": "gpt-5.6-sol"})
        hooks.install_home(d, specs=hooks.DELIVERY_SPECS)
        s = self.read_settings(d)
        self.assertEqual(s.get("workflowSizeGuideline"), "small")
        self.assertEqual(s.get("model"), "gpt-5.6-sol")

    def test_claude_credhome_also_gets_default_small(self):
        # claude seats launch via orca into a credhome, NOT a family launch_line;
        # the SAME one edit reaches them because install_home covers claude_homes.
        d = self.mk_home("a-user-gmail-com")
        hooks.install_home(d)                       # full SPECS (home surface)
        self.assertEqual(
            self.read_settings(d).get("workflowSizeGuideline"), "small")

    def test_default_is_idempotent_and_its_absence_triggers_a_write(self):
        d = self.mk_seat("kimi")
        hooks.install_home(d, specs=hooks.DELIVERY_SPECS)
        self.assertEqual(hooks.install_home(d, specs=hooks.DELIVERY_SPECS),
                         ("ok", "hook up to date"))
        got = self.read_settings(d)
        got.pop("workflowSizeGuideline")            # someone pruned ours
        with open(os.path.join(d, "settings.json"), "w") as f:
            json.dump(got, f)
        action, _ = hooks.install_home(d, specs=hooks.DELIVERY_SPECS)
        self.assertNotEqual(action, "ok")
        self.assertEqual(
            self.read_settings(d).get("workflowSizeGuideline"), "small")

    def test_every_family_inherits_workflows_enabled_feature(self):
        # the ENABLE half: ONE complete source cache, and every family's seed
        # inherits cachedGrowthBookFeatures wholesale (tengu_workflows_enabled
        # included) — the already-universal mechanism, proven to reach each
        # family, so this suite fails if that inheritance ever regresses.
        src = os.path.join(self.seats_root, "codex", "claude")
        os.makedirs(src, exist_ok=True)
        with open(os.path.join(src, ".claude.json"), "w") as f:
            json.dump(self._complete_cache(), f)
        for family in sorted(seat.FAMILIES):
            with self.subTest(family=family):
                dst = os.path.join(self.tmp, "dst-%s.json" % family)
                cache = seat._feature_cache_seed(family, dst)
                self.assertIsNotNone(cache, "family %s inherits no cache" % family)
                self.assertIs(cache["cachedGrowthBookFeatures"].get(
                    "tengu_workflows_enabled"), True,
                    "family %s did not inherit workflows-enabled" % family)


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
        rows, unread = hooks.seat_homes()
        found = dict(rows)
        self.assertEqual(set(found), {"codex", "kimi"})
        self.assertEqual(found["codex"], os.path.realpath(c))
        self.assertEqual(unread, [])   # a fully readable estate reports so

    def test_seat_homes_finds_nested_instance_config_dirs(self):
        """task/331: the census's original `*/claude` glob was ONE level deep,
        so a live slice-6 instance dir (seats/<family>/instances/<seat>/
        claude) sat entirely OUTSIDE a fleet-wide hook repair that then
        reported every seat covered. The census must see every minted depth,
        prune the insides of matched config dirs, and stop at its stated
        bound (SEAT_WALK_DEPTH) — each arm asserted here, so this test goes
        RED against the one-level glob and against a bound regression both."""
        fam = self.mk_seat("codex")
        inst = os.path.join(self.seats_root, "codex", "instances",
                            "codex-2", "claude")
        os.makedirs(inst)
        # distractors, every shape present on the real estate: a smoke dir
        # (config-adjacent, never a delivery target), an auth tree, and a
        # dir literally named `claude` INSIDE a config dir (prune arm)
        os.makedirs(os.path.join(self.seats_root, "codex", "smoke-claude"))
        os.makedirs(os.path.join(self.seats_root, "codex", "auth", "logs"))
        os.makedirs(os.path.join(fam, "plugins", "claude"))
        # headroom arm: one nesting generation deeper than today's estate
        # (depth 6) is IN the census, so a third minted shape lands inside
        # the instrument instead of outside it
        deep = os.path.join(self.seats_root, "fam2", "instances", "i1",
                            "instances", "i1a", "claude")
        os.makedirs(deep)
        # bound arm: depth 7 is OUT — the bound is a stated claim, not vibes
        os.makedirs(os.path.join(self.seats_root, "fam3", "a", "b", "c",
                                 "d", "e", "claude"))
        self.assertEqual(hooks.seat_homes(),
                         ([("codex", os.path.realpath(fam)),
                           ("codex-2", os.path.realpath(inst)),
                           ("i1a", os.path.realpath(deep))], []))

    def test_seat_coverage_counts_nested_instances(self):
        """The defect's owner-visible symptom: "N of N seats" printed while
        codex-2/codex-3 sat outside the denominator. A nested instance with
        no delivery lane must appear as an UNCOVERED row — and the gated
        write must then REACH it (deliberately not registered in HOME_ROOTS:
        _is_seat_home has to recognize the instance shape on its own)."""
        fam = self.mk_seat("codex")
        hooks.install_home(fam, specs=hooks.DELIVERY_SPECS)
        inst = os.path.join(self.seats_root, "codex", "instances",
                            "codex-2", "claude")
        os.makedirs(inst)                   # bare: no settings.json at all
        rows = {r["seat"]: r for r in hooks.seat_status_rows()[0]}
        self.assertIn("codex-2", rows,
                      "the nested instance is OUTSIDE the census")
        self.assertFalse(rows["codex-2"]["deliver"])
        self.assertEqual(hooks.seat_coverage(), (1, 2, []))
        action, detail = hooks.install_home(inst, specs=hooks.DELIVERY_SPECS)
        self.assertEqual(action, "add", detail)
        self.assertEqual(hooks.seat_coverage(), (2, 2, []))
        got = self.read_settings(inst)
        self.assertIn("chat deliver --hook-json",
                      " ".join(hooks._hook_cmds(got, "PostToolUse")))

    def test_install_wires_delivery_lane_without_dropping_foreign(self):
        foreign = "echo seat-local-hook"          # a pre-existing foreign hook
        d = self.mk_seat("codex", settings={
            "model": "gpt-5.6-sol",
            "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
                {"type": "command", "command": foreign}]}]}})
        deliver = next(s for s in hooks.SPECS if s["name"] == "deliver")
        agent_stop = next(s for s in hooks.SPECS
                          if s["name"] == "delegation-stop")
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
        self.assertIn(hooks.spec_command(agent_stop),
                      hooks._hook_cmds(got, "SubagentStop"))
        self.assertIn(hooks.spec_command(join), hooks._hook_cmds(got, "SessionStart"))
        self.assertIn(hooks.spec_command(stop), hooks._hook_cmds(got, "Stop"))
        self.assertEqual(got["hooks"]["PostToolUse"][-1]["matcher"], "*")
        self.assertEqual(got["hooks"]["SubagentStop"][-1]["matcher"], "*")
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
        rows = {r["seat"]: r for r in hooks.seat_status_rows()[0]}
        self.assertTrue(rows["codex"]["deliver"] and rows["codex"]["join"]
                        and rows["codex"]["stop-guard"])
        self.assertFalse(rows["kimi"]["deliver"])
        self.assertEqual(hooks.seat_coverage(), (1, 2, []))
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
        self.assertEqual(hooks.seat_coverage(), (0, 1, []))


class SeatCensusCompletenessTest(HooksBase):
    """task/331's disease one layer down (@codex-3's probe): the WIDENED walk
    caught every OSError and silently returned, so an unreadable instances/
    subtree was indistinguishable from no nested seats — seat_coverage could
    print healthy around a live invisible codex-2, the exact observable the
    row was filed for. The law is helm's own everywhere else (vcs.ancestry,
    _project_contexts, running_panes' UNCLASSIFIED rows): a read that FAILED
    is never a NO. Every fixture here is synthetic."""

    def _skip_if_root(self):
        if os.geteuid() == 0:
            self.skipTest("chmod does not bind euid 0 — this arm would be "
                          "vacuous as root (the subtree would read fine and "
                          "the assertions would test nothing)")

    @contextlib.contextmanager
    def _locked(self, path, mode=0):
        """chmod with a restore that outlives an assertion failure — a 000
        dir left behind makes tearDown's rmtree leave litter silently."""
        os.chmod(path, mode)
        try:
            yield
        finally:
            os.chmod(path, 0o755)

    def _nested(self, family="codex", seat="codex-2"):
        """<seats>/<family>/claude + <family>/instances/<seat>/claude — the
        probe's exact estate. Returns (family_dir, instances_dir)."""
        fam = self.mk_seat(family)
        inst = os.path.join(self.seats_root, family, "instances")
        os.makedirs(os.path.join(inst, seat, "claude"))
        return fam, inst

    def test_unreadable_subtree_is_surfaced_never_an_absence(self):
        """The probe itself: chmod 000 on instances/ must NOT yield a clean
        single-seat census. Positive control first — the open estate must
        show both seats, or the locked arm's zero proves nothing."""
        self._skip_if_root()
        fam, inst = self._nested()
        rows, unread = hooks.seat_homes()
        self.assertEqual([r[0] for r in rows], ["codex", "codex-2"])
        self.assertEqual(unread, [])
        with self._locked(inst):
            rows, unread = hooks.seat_homes()
            self.assertEqual([r[0] for r in rows], ["codex"])
            self.assertEqual([u[0] for u in unread], [inst],
                             "an unreadable subtree MUST land in unread — a "
                             "bare one-seat census is task/331 again")
            self.assertIn("PermissionError", unread[0][1])
            c, t, cunread = hooks.seat_coverage()
            self.assertEqual(t, 1)
            self.assertEqual([u[0] for u in cunread], [inst],
                             "seat_coverage dropped the census's completeness")

    def test_listable_but_unstatable_child_is_unread_not_a_non_dir(self):
        """The same drain through the STAT arm: os.path.isdir swallows
        OSError into False, so a child the walk can LIST but not STAT
        (parent r without x, 0o400) used to pass silently as 'not a dir'."""
        self._skip_if_root()
        fam, inst = self._nested()
        child = os.path.join(inst, "codex-2")
        with self._locked(inst, 0o400):
            rows, unread = hooks.seat_homes()
        self.assertEqual([r[0] for r in rows], ["codex"])
        self.assertEqual([u[0] for u in unread], [child])

    def test_raced_away_is_absence_but_eacces_is_not(self):
        """The errno split, deterministic (mocked listdir, so it also holds
        under root where chmod arms skip): a dir GONE when asked about
        (ENOENT) was never a seat — running_panes' 'gone' law — and that
        benign arm must be entered on exactly ENOENT/ENOTDIR, so it cannot
        absorb a permission failure."""
        fam, inst = self._nested()
        real_listdir = os.listdir

        def hitting(exc):
            def fake(d):
                if os.fspath(d) == inst:
                    raise exc
                return real_listdir(d)
            return fake

        gone = OSError(errno.ENOENT, "vanished mid-walk", inst)
        with mock.patch("os.listdir", hitting(gone)):
            rows, unread = hooks.seat_homes()
        self.assertEqual([r[0] for r in rows], ["codex"])
        self.assertEqual(unread, [], "a raced-away dir is a real absence")

        denied = OSError(errno.EACCES, "permission denied", inst)
        with mock.patch("os.listdir", hitting(denied)):
            rows, unread = hooks.seat_homes()
        self.assertEqual([r[0] for r in rows], ["codex"])
        self.assertEqual([u[0] for u in unread], [inst],
                         "the absence arm absorbed an EACCES")

    def test_missing_seats_root_is_a_real_absence(self):
        # no seats dir minted at all — an estate with no seats, not UNKNOWN
        self.assertEqual(hooks.seat_homes(), ([], []))

    def test_fully_unreadable_seats_root_still_screams(self):
        """Zero rows + unread root: behind the old `if srows:` gate the
        LOUDEST failure (nothing readable at all) was the QUIETEST surface —
        status printed no seat section whatsoever."""
        self._skip_if_root()
        self.mk_seat("codex")
        with self._locked(self.seats_root):
            rows, unread = hooks.seat_homes()
            self.assertEqual(rows, [])
            self.assertEqual([u[0] for u in unread], [self.seats_root])
            rc, out, _ = self.run_hooks(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("seat census INCOMPLETE", out)
        self.assertIn(self.seats_root, out)

    def test_status_never_prints_a_clean_count_over_an_unread_subtree(self):
        """The owner-visible symptom, pinned end to end: '1 of 1 seats' may
        still print (it is TRUE of the readable population) but never alone
        — the INCOMPLETE line with the unreadable path rides beside it."""
        self._skip_if_root()
        fam, inst = self._nested()
        hooks.install_home(fam, specs=hooks.DELIVERY_SPECS)
        with self._locked(inst):
            rc, out, _ = self.run_hooks(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("seat delivery: 1 of 1 seats", out)
        self.assertIn("seat census INCOMPLETE", out)
        self.assertIn(inst, out)

    def test_install_wires_the_visible_and_fails_loud_on_the_unread(self):
        """UNKNOWN-and-report, never abort: the seats the walk CAN see still
        get their delivery lane (a hard-fail would deny the working fleet
        service over one bad subtree), but the install exits 1 — a subtree
        it could not read is a population it could not wire, its own job
        failing — and says so on stderr with the path."""
        self._skip_if_root()
        fam, inst = self._nested()
        with self._locked(inst):
            rc, out, err = self.run_hooks(["install"])
        self.assertEqual(rc, 1)
        self.assertIn("seat census INCOMPLETE", err)
        self.assertIn(inst, err)
        self.assertIn("count is a floor", out)
        got = self.read_settings(fam)
        self.assertIn("chat deliver --hook-json",
                      " ".join(hooks._hook_cmds(got, "PostToolUse")))


class InstallCensusUnskippableTest(HooksBase):
    """task/331's disease one layer OUT (@codex-3's second probe, at
    a059b2f8): the walk was made honest, but install's CONSUMER could still
    exit before asking. A dry run's product is its REPORT, so its exit code
    must say whether the report is COMPLETE — a guard that exits before it
    measures is indistinguishable from a guard that measured and found
    nothing. Every arm here MOCKS the census (no chmod), so none goes
    vacuous under root. Every fixture is synthetic."""

    _DENIED = "PermissionError: [Errno 13] Permission denied"

    def test_a_dry_install_cannot_return_before_the_census(self):
        """Drain (1) verbatim: _select_homes -> [] and seat_homes -> unread
        used to return 0 BEFORE calling seat_homes (census_calls=0, stderr
        empty), so a seat-only estate was invisible to the one command whose
        job is wiring what it finds. The census is now unskippable: invoked,
        the unreadable path named on stderr, rc 1."""
        calls = {"n": 0}
        unread = os.path.join(self.seats_root, "codex", "instances")

        def unreadable_census():
            calls["n"] += 1
            return [], [(unread, self._DENIED)]

        with mock.patch.object(hooks, "_select_homes",
                               return_value=([], None)), \
                mock.patch.object(hooks, "seat_homes", unreadable_census):
            rc, out, err = self.run_hooks(["install", "--dry"])
        self.assertEqual(calls["n"], 1,
                         "census_calls=0 IS the drain — an rc-only check "
                         "could pass for the wrong reason")
        self.assertEqual(rc, 1,
                         "a dry report over an unreadable subtree is "
                         "INCOMPLETE and its rc must say so")
        self.assertIn("seat census INCOMPLETE", err)
        self.assertIn(unread, err)

    def test_a_dry_install_on_a_readable_empty_estate_still_asks(self):
        """The positive control that makes census_calls meaningful: rc 0 must
        mean MEASURED-complete, never unasked — the census runs even when
        there is nothing to find."""
        calls = {"n": 0}

        def empty_census():
            calls["n"] += 1
            return [], []

        with mock.patch.object(hooks, "_select_homes",
                               return_value=([], None)), \
                mock.patch.object(hooks, "seat_homes", empty_census):
            rc, out, err = self.run_hooks(["install", "--dry"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(calls["n"], 1, "the empty verdict was never asked for")
        self.assertEqual(err, "")
        self.assertIn("no claude homes found", out)

    def test_a_second_census_gone_unread_after_wiring_reaches_rc(self):
        """Drain (2) verbatim: the wet install's SECOND census
        (seat_coverage, after wiring) going unread printed `count is a floor`
        on stdout but returned 0 with stderr empty — the incompleteness was
        invisible at exit. First census complete (the visible seat keeps its
        lane), second unread: rc 1, exact path on stderr."""
        fam = self.mk_seat("codex")
        inst = os.path.join(self.seats_root, "codex", "instances")
        real, calls = hooks.seat_homes, {"n": 0}

        def racing_census():
            calls["n"] += 1
            if calls["n"] == 1:
                return real()
            return [], [(inst, self._DENIED)]

        with mock.patch.object(hooks, "seat_homes", racing_census):
            rc, out, err = self.run_hooks(["install"])
        self.assertEqual(calls["n"], 2, "both censuses must have run")
        self.assertEqual(rc, 1,
                         "an unread SECOND census must reach the exit code")
        self.assertIn("count is a floor", out)
        self.assertIn("seat census INCOMPLETE after wiring", err)
        self.assertIn(inst, err)
        # UNKNOWN-and-report, never abort: the seat the walk DID see is wired
        got = self.read_settings(fam)
        self.assertIn("chat deliver --hook-json",
                      " ".join(hooks._hook_cmds(got, "PostToolUse")))

    def test_a_seat_only_estate_is_wired_not_skipped(self):
        """The estate drain (1) hid: zero claude homes, one live seat family.
        The old early return exited 0 having wired NOTHING; a full install
        now carries the delivery lane to the seats it can see."""
        fam = self.mk_seat("codex")
        with mock.patch.object(hooks, "claude_homes", return_value=[]):
            rc, out, err = self.run_hooks(["install"])
        self.assertEqual(rc, 0, err)
        self.assertIn("no claude homes found", out)
        self.assertIn("1 of 1 seats covered", out)
        got = self.read_settings(fam)
        self.assertIn("chat deliver --hook-json",
                      " ".join(hooks._hook_cmds(got, "PostToolUse")))


class FreshSeatRecognitionTest(HooksBase):
    def test_seat_dir_minted_after_import_accepted_by_gated_write(self):
        """G-seatlaunch-installs' enabling fix: configs' HOME_ROOTS globs at
        import — a seat minted afterwards (helm seat add, same process) must
        STILL pass the write gate, or the born-wired install is refused."""
        d = os.path.join(self.seats_root, "newfam", "claude")
        os.makedirs(d)                      # deliberately NOT in HOME_ROOTS
        action, detail = hooks.install_home(d, specs=hooks.DELIVERY_SPECS)
        self.assertEqual(action, "add", detail)
        got = self.read_settings(d)
        self.assertIn("chat deliver --hook-json",
                      " ".join(hooks._hook_cmds(got, "PostToolUse")))


class UncoveredPanesTest(HooksBase):
    """The retrofit surface: running claude PTYs whose named identity has no
    live roster row — a joined-late idle pane can't self-heal; it must be
    surfaced for relaunch (the live kimi case)."""

    def mk_proc(self, pid, cmdline, env, starttime=100):
        d = os.path.join(self.tmp, "proc", str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"\0".join(cmdline) + b"\0")
        with open(os.path.join(d, "environ"), "wb") as f:
            f.write(b"\0".join(env) + b"\0")
        # A REAL /proc ENTRY CARRIES stat, AND WITHOUT IT THE INCARNATION
        # BRACKET IS INERT: proc_starttime returns None, the token compares
        # None to None, and every arm passes while exercising nothing of the
        # anti-pid-reuse guarantee. A fixture that omits what production
        # always has is not testing production. @codex-2 called this a real
        # hole and it was — I had declared it and shipped it once.
        with open(os.path.join(d, "stat"), "wb") as f:
            f.write(("%d (claude) S " % pid + "0 " * 18
                     + str(starttime)).encode())
        return os.path.join(self.tmp, "proc")

    def test_uncovered_pane_surfaced_then_covered_when_roster_fresh(self):
        proc = self.mk_proc(4242, [b"claude", b"--model", b"kimi-k3"],
                            [b"HELM_CHAT_NAME=kimi", b"TERM=xterm"])
        self.mk_proc(4243, [b"vim", b"notes.md"], [b"TERM=xterm"])  # not claude
        panes = hooks.running_panes(proc)
        self.assertEqual([(p["pid"], p["seat"]) for p in panes],
                         [(4242, "kimi")])
        rows = hooks.uncovered_panes(proc)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["seat"], "kimi")
        self.assertIn("never joined", rows[0]["reason"])
        out = io.StringIO()
        hooks.surface_uncovered(out=out)     # HELM_PROC points at the fake tree
        self.assertIn("kimi", out.getvalue())
        self.assertIn("relaunch", out.getvalue())
        from helm import seats
        seats.write_roster("kimi")           # the seat joins (fresh .seen)
        self.assertEqual(hooks.uncovered_panes(proc), [])

    def test_a_pane_whose_environ_is_unreadable_is_REPORTED_not_dropped(self):
        """THE SURFACE MUST NOT DELETE THE PANES IT CANNOT READ.

        running_panes opened cmdline and environ inside ONE try with a bare
        `continue`, so a pane whose environ is unreadable — another uid, a
        hardened proc, a process exiting mid-scan — VANISHED. This surface
        exists to name panes that can never self-heal, so a pane it drops is
        not merely uncounted: the operator reads a short clean list as "all
        covered", which is an absence claim the read never supported.

        beacons.agent_index already states the law in its own docstring — an
        unreadable pane "is recorded as declaring NOTHING, which is what it
        is" — and this second census simply did not follow it."""
        proc = self.mk_proc(5150, [b"claude"], [b"HELM_CHAT_NAME=alpha"])
        self.mk_proc(5151, [b"claude"], [b"HELM_CHAT_NAME=beta"])
        os.chmod(os.path.join(proc, "5151", "environ"), 0o000)

        panes = hooks.running_panes(proc)
        seen = {p["pid"]: p for p in panes}
        # POSITIVE CONTROL: the READABLE pane still resolves normally, so a
        # pass here cannot mean "the scan now returns everything as unknown".
        self.assertIn(5150, seen, "the readable pane disappeared")
        self.assertEqual(seen[5150]["seat"], "alpha")
        self.assertFalse(seen[5150]["environ_unreadable"])
        # THE DEFECT: this pid used to be absent entirely.
        self.assertIn(5151, seen,
                      "a claude pane with an unreadable environ was DROPPED "
                      "from the surface whose job is naming uncovered panes")
        self.assertTrue(seen[5151]["environ_unreadable"])
        self.assertIsNone(seen[5151]["seat"],
                          "an unreadable environ cannot yield a seat name")

    def test_an_instance_panes_config_dir_declares_its_seat(self):
        """task/331's live-pane arm: running_panes derived `family` only for
        a config dir exactly ONE level under the seats root, so an instance
        pane (CLAUDE_CONFIG_DIR=…/instances/codex-2/claude) carrying no
        HELM_CHAT_NAME stamp had seat=None AND family=None — dropped by every
        downstream `p["seat"] or p["family"]` join, invisible to the
        uncovered-pane surface whose whole job is naming such panes."""
        fam = os.path.join(self.seats_root, "codex", "claude")
        inst = os.path.join(self.seats_root, "codex", "instances",
                            "codex-2", "claude")
        os.makedirs(fam)
        os.makedirs(inst)
        proc = self.mk_proc(6001, [b"claude"],
                            [b"CLAUDE_CONFIG_DIR=" + inst.encode()])
        self.mk_proc(6002, [b"claude"],
                     [b"CLAUDE_CONFIG_DIR=" + fam.encode()])
        panes = {p["pid"]: p for p in hooks.running_panes(proc)}
        # POSITIVE CONTROL first: the family shape still classifies, so a
        # pass below cannot mean "everything now classifies as something"
        self.assertEqual(panes[6002]["family"], "codex")
        # THE DEFECT: this was None
        self.assertEqual(panes[6001]["family"], "codex-2")

    # ------------------------------------------------------------------
    # THE SPIRAL GUARD. Four review rounds on this lane, and rounds 3 and 4
    # were THE SAME MOVE TWICE: another consumer of running_panes() that
    # scanned /proc itself. @opus-integrator ruled it a per-case handler —
    # "a per-case handler can never be completed by adding cases, because
    # the next case is always outside the set you enumerated."
    #
    # Threading `panes=` into the three consumers we KNOW about would close
    # the set BY ENUMERATION, which is the shape that produced R3 and R4.
    # These two arms close it BY CONSTRUCTION: a new consumer that scans
    # unconditionally fails the AST arm, and a report that scans twice fails
    # the runtime arm. Neither requires anyone to remember this lane.
    #
    # Two independent scans of a LIVE /proc cannot agree about a process that
    # exits between them, so this is not a style rule — it is the only way
    # the four bucket semantics can be JOINTLY true rather than each true.
    _MAY_ORIGINATE_A_SCAN = {
        "surface_uncovered":
            "report entry point — takes THE scan and feeds every consumer",
        "_project_contexts":
            "report entry point for `helm doctor` (project_scope_rows), and "
            "accepts panes= so a larger report can hand it ITS scan instead",
    }

    @staticmethod
    def _scan_offenders(text, allow):
        """Every running_panes() call in `text` that ORIGINATES a scan
        without being allowed to. Returns [(function, lineno), ...]."""
        import ast
        tree = ast.parse(text)
        bad = []

        def scans(node):
            return [n for n in ast.walk(node)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name)
                    and n.func.id == "running_panes"]

        def guarded_call_ids(node):
            """Calls sitting in the `panes is None` branch of a default."""
            ok = set()
            for n in ast.walk(node):
                test = getattr(n, "test", None)
                if not isinstance(test, ast.Compare):
                    continue
                if not (isinstance(test.left, ast.Name)
                        and test.left.id == "panes"
                        and len(test.ops) == 1
                        and isinstance(test.ops[0], ast.Is)
                        and len(test.comparators) == 1
                        and isinstance(test.comparators[0], ast.Constant)
                        and test.comparators[0].value is None):
                    continue
                branch = getattr(n, "body", None)
                branch = branch if isinstance(branch, list) else [branch]
                for b in branch:
                    if b is not None:
                        ok.update(id(c) for c in scans(b))
            return ok

        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if fn.name == "running_panes":   # the def is not a call site
                continue
            calls = scans(fn)
            if not calls:
                continue
            args = fn.args
            takes = {a.arg for a in list(args.args) + list(args.kwonlyargs)}
            # TWO SEPARATE PROPERTIES, AND CONFLATING THEM LEFT A HOLE THE
            # MUTATION MATRIX FOUND. Being allowlisted grants permission to
            # ORIGINATE a scan; it does NOT excuse a function from HONORING
            # one it was handed. _project_contexts is allowlisted (it is the
            # entry point for `helm doctor`) AND accepts panes= so a larger
            # report can feed it — so reverting its body to an unconditional
            # running_panes() reintroduced R3 exactly, and the first version
            # of this guard skipped it on the allowlist and reported clean.
            #
            # Honouring is therefore checked for EVERY function that accepts
            # panes, allowlisted or not. The allowlist only exempts a
            # function from having to accept panes in the first place.
            if "panes" in takes:
                guarded = guarded_call_ids(fn)
                for c in calls:
                    if id(c) not in guarded:
                        bad.append((fn.name, c.lineno))
                continue
            if fn.name in allow:
                continue
            for c in calls:
                bad.append((fn.name, c.lineno))
        return bad

    def test_only_a_report_entry_point_may_ORIGINATE_a_proc_scan(self):
        """AST ARM — closes the consumer set by construction.

        Every running_panes() call in hooks.py must be EITHER inside an
        allowlisted report entry point, OR the `panes is None` default of a
        consumer that accepts a caller's snapshot. A new consumer that walks
        /proc on its own fails HERE, at the round it is written, instead of
        at the review round after it ships.

        ALLOWLIST, NOT BLOCKLIST. Enumerating forbidden shapes has to be
        widened per case and is silently wrong between widenings — which is
        the very defect this lane spiralled on, one layer up. An allowlist
        refuses by default: an unlisted function that scans is a failure
        until someone writes down why it may."""
        src = os.path.join(os.path.dirname(os.path.abspath(hooks.__file__)),
                           "hooks.py")
        with open(src, encoding="utf-8") as f:
            text = f.read()
        # the guard is worthless if it is reading the wrong file
        self.assertIn("def running_panes(", text, "read the wrong source")

        allow = self._MAY_ORIGINATE_A_SCAN

        # UNCONDITIONAL POSITIVE CONTROL, hoisted out of the loop below.
        # Every control I first wrote lived inside `for ... in controls`, so
        # an empty dict would have passed all of them and the guard would
        # have reported clean while checking nothing — the vacuous-assertion
        # rung caught it on this very commit. That is the same defect these
        # arms exist to prevent, one level up: a check whose domain can be
        # empty is not a check.
        # Bound to the SAME NAME the absence assertion below reads, because
        # a positive control only covers the observable it actually
        # constrains: proving the instrument fires on some OTHER expression
        # says nothing about `found`.
        found = self._scan_offenders("def unlisted(): return running_panes()",
                                     allow)
        self.assertTrue(
            found, "the guard does not even flag an unlisted unconditional scan")

        # POSITIVE CONTROLS — the guard must FAIL each of these, or its clean
        # read of the real file proves nothing. Every one is a shape a future
        # round could plausibly ship.
        controls = {
            "a brand-new consumer that scans unconditionally":
                "def brand_new(): return running_panes()",
            "a consumer that TAKES panes and then ignores it":
                "def sloppy(panes=None): return running_panes()",
            "a scan in the ELSE branch of the panes default":
                "def inverted(panes=None):\n"
                "    return panes if panes is None else running_panes()",
            "a panes check that is truthiness, not `is None` (an empty "
            "snapshot is a real answer and would rescan)":
                "def truthy(panes=None):\n"
                "    return running_panes() if not panes else panes",
            "a nested helper that scans on behalf of an allowlisted caller":
                "def helper(): return running_panes()",
            "a consumer taking panes under a DIFFERENT name":
                "def renamed(scan=None):\n"
                "    return running_panes() if scan is None else scan",
            "an ALLOWLISTED entry point that accepts panes and ignores it "
            "(the exact hole the mutation matrix found: allowlisting grants "
            "permission to ORIGINATE a scan, never to disregard one)":
                "def _project_contexts(panes=None):\n"
                "    return running_panes()",
        }
        self.assertEqual(  # noqa: VACUOUS_ASSERTION — mutation-proven
            len(controls), 7, "controls were dropped")
        for why, snippet in controls.items():
            found = self._scan_offenders(snippet, allow)
            self.assertTrue(found, "GUARD IS BLIND: it accepted %s" % why)

        # NEGATIVE CONTROLS — the sanctioned shapes must PASS, or the guard
        # merely rejects everything and its red on the real file is noise.
        for why, snippet in {
            "the sanctioned caller-snapshot default":
                "def good(panes=None):\n"
                "    return running_panes() if panes is None else panes",
            "the same default consumed by a for-loop":
                "def good_loop(panes=None):\n"
                "    for p in (running_panes() if panes is None else panes):\n"
                "        yield p",
            "an allowlisted entry point originating THE scan":
                "def surface_uncovered(out=None):\n"
                "    return running_panes()",
        }.items():
            found = self._scan_offenders(snippet, allow)
            # noqa: VACUOUS_ASSERTION — INTENTIONAL absence: a negative
            # control asserts the guard does NOT flag a sanctioned shape.
            # Non-vacuity is proven by mutation, not by a sibling assertion:
            # M3/M4 (a consumer ignoring its snapshot) kill this arm, and M2
            # (an allowlisted consumer ignoring it) killed it once the
            # originate/honour split landed. The unconditional positive on
            # `found` above fires on an unlisted scanner.
            self.assertEqual(found, [],
                             "GUARD IS OVER-BROAD: it rejected %s" % why)

        found = self._scan_offenders(text, allow)
        # noqa: VACUOUS_ASSERTION — INTENTIONAL absence: the real file must
        # hold no unsanctioned scanner. `found` carries an unconditional
        # positive above (an unlisted scan IS flagged), and mutation M2/M3/M4
        # each turn this green into red.
        self.assertEqual(found, [],
                         "these walk /proc on their own — either accept a "
                         "caller's snapshot as `panes`, or record in "
                         "_MAY_ORIGINATE_A_SCAN why this one must originate "
                         "its own: %r" % (found,))

    def test_a_consumer_handed_a_snapshot_does_not_go_back_to_proc(self):
        """BEHAVIOUR ARM — the dual of the AST arm, and the one that would
        have caught the hole on its own.

        The AST arm reads SHAPE: it asks whether a `panes is None` default
        wraps the call. This arm asks the question that actually matters —
        hand the consumer a snapshot and see whether it walks /proc anyway.
        A future consumer could satisfy the shape and still rescan (a helper
        call, a retry, a second read behind an early return); it cannot
        satisfy this.

        THE CONSUMER LIST IS DERIVED FROM THE SOURCE, not typed from memory.
        Anything in hooks.py that accepts `panes` must appear here with a
        driver, so a new consumer fails this arm on the day it is written
        rather than at the review round after it ships. Enumerating from
        memory is how R3 and R4 were both missed."""
        import ast
        src = os.path.join(os.path.dirname(os.path.abspath(hooks.__file__)),
                           "hooks.py")
        with open(src, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        accepts_panes = sorted(
            fn.name for fn in ast.walk(tree)
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            and "panes" in {a.arg for a in list(fn.args.args)
                            + list(fn.args.kwonlyargs)})

        proc = self.mk_proc(5150, [b"claude"], [b"HELM_CHAT_NAME=alpha"])
        os.environ["HELM_PROC"] = proc
        snapshot = hooks.running_panes()
        # POSITIVE CONTROL ON REAL DATA: consumers must be handed a NON-EMPTY
        # snapshot, or "it did not rescan" is satisfied by having nothing to
        # do. This is the input to every driver below.
        self.assertTrue(  # noqa: VACUOUS_ASSERTION — mutation-proven
            snapshot, "the fixture produced no panes to hand over")

        drivers = {
            "uncovered_panes": lambda s: hooks.uncovered_panes(panes=s),
            "unsigned_panes": lambda s: hooks.unsigned_panes(panes=s),
            "_project_contexts": lambda s: hooks._project_contexts(panes=s),
        }
        self.assertEqual(sorted(drivers), accepts_panes,
                         "every hooks.py function that accepts `panes` must "
                         "be driven here; %r accept it and %r are driven"
                         % (accepts_panes, sorted(drivers)))

        for name, drive in sorted(drivers.items()):
            calls = {"n": 0}
            real = hooks.running_panes

            def counting(p=None, _real=real, _c=calls):
                _c["n"] += 1
                return _real(p)

            with mock.patch.object(hooks, "running_panes", counting):
                drive(snapshot)
            # noqa: VACUOUS_ASSERTION — INTENTIONAL absence: ZERO rescans
            # is the property. The snapshot handed in is asserted non-empty
            # above, so this is not "nothing to do"; mutations M2/M3/M4 each
            # raise this count and kill the arm.
            self.assertEqual(calls["n"], 0,
                             "%s was handed a snapshot and walked /proc %d "
                             "more time(s) anyway; the caller's report now "
                             "mixes snapshots no matter what the caller does"
                             % (name, calls["n"]))

    def test_no_report_entry_point_scans_proc_TWICE(self):
        """RUNTIME ARM — the AST arm cannot see a report that calls two
        already-correct consumers WITHOUT handing its snapshot to either.
        That is precisely what surface_uncovered did for four rounds: both
        consumers accepted `panes`, and it passed neither.

        DRIVERS ARE DERIVED FROM THE ALLOWLIST, and the key-set assertion is
        the load-bearing part: adding an entry point to the allowlist without
        a driver FAILS here instead of silently skipping the new one. A guard
        that quietly covers a subset is the defect it is guarding against."""
        proc = self.mk_proc(4242, [b"claude"], [b"HELM_CHAT_NAME=alpha"])
        os.environ["HELM_PROC"] = proc

        def drive_surface():
            buf = io.StringIO()
            hooks.surface_uncovered(out=buf)
            return buf.getvalue()

        # Each driver RETURNS its real observable. Asserting only on the spy
        # counter would pass on a report that produced nothing at all — the
        # vacuous-assertion rung refuses a spy as a positive control for
        # exactly this reason, and it was right about this arm.
        drivers = {
            "surface_uncovered": drive_surface,
            "_project_contexts": hooks._project_contexts,
        }
        driven = sorted(drivers)
        # If drivers AND the allowlist were both empty this equality would
        # hold, the loop below would never run, and the arm would report
        # clean while guarding nothing. The rung flagged exactly this.
        self.assertTrue(driven, "no entry point is driven at all")
        self.assertEqual(driven, sorted(self._MAY_ORIGINATE_A_SCAN),
                         "every entry point allowed to originate a scan must "
                         "be DRIVEN here; an undriven one is unguarded")

        # UNCONDITIONAL POSITIVE CONTROL — same reason as the arm above: the
        # per-entry-point assertions live inside a loop, so an empty drivers
        # map would pass this test while driving nothing.
        solo = {"n": 0}
        _real = hooks.running_panes

        def solo_counting(p=None):
            solo["n"] += 1
            return _real(p)

        with mock.patch.object(hooks, "running_panes", solo_counting):
            report = drive_surface()
        # POSITIVE CONTROL ON REAL DATA, not on the spy: the live pane must
        # actually appear in the report. Without this the arm passes on an
        # empty report that scanned once and said nothing.
        self.assertIn("4242", report,
                      "the report never mentioned the live pane, so its scan "
                      "count constrains nothing")
        self.assertEqual(solo["n"], 1,
                         "surface_uncovered scanned /proc %d times"
                         % solo["n"])

        for name, drive in sorted(drivers.items()):
            calls = {"n": 0}
            real = hooks.running_panes

            def counting(p=None, _real=real, _c=calls):
                _c["n"] += 1
                return _real(p)

            with mock.patch.object(hooks, "running_panes", counting):
                observed = drive()
            self.assertTrue(observed,
                            "%s produced no observable at all; a scan count "
                            "over an empty result proves nothing" % name)
            # EXACTLY ONE, NOT `<= 1`: a driver that never reaches /proc at
            # all would satisfy "at most once" while guarding nothing.
            self.assertEqual(calls["n"], 1,
                             "%s scanned /proc %d times in ONE report; two "
                             "reads of a live /proc cannot agree about a "
                             "process that exits between them"
                             % (name, calls["n"]))

    # ------------------------------------------------------------------
    # PRODUCER CONTRACT (@codex-2, round five — producer layer, not another
    # consumer). running_panes' docstring promised "every live claude-harness
    # PTY of THIS uid; other-uid entries skipped" and the implementation never
    # looked at st_uid. The promise held only INCIDENTALLY: another user's
    # environ is EACCES, and both reads used to sit in one try with a bare
    # `continue`. The cure for the vanishing-pane bug removed that continue —
    # and converted an incidental guarantee into a FALSE CLAIM in the same
    # stroke. A foreign claude then entered OUR snapshot as OUR blind pane.
    def test_another_users_claude_pane_is_NOT_in_our_census(self):
        """OWNERSHIP IS PROVEN, NEVER INFERRED FROM A READ FAILURE.

        The cross-UID positive control @codex-2 asked for by name. A real
        multi-user box is not reproducible in a fixture, so the fixture stays
        honest and OUR uid moves: same pane, same bytes, one different answer
        to "is this ours".

        THE CONTROL IS THE WHOLE TEST. Asserting only that the foreign pane is
        absent would pass just as well if running_panes returned [] for every
        input, so the same pane MUST be present under our own uid first."""
        proc = self.mk_proc(6001, [b"claude"], [b"HELM_CHAT_NAME=theirs"])
        os.environ["HELM_PROC"] = proc

        ours = hooks.running_panes(proc)
        self.assertEqual([p["pid"] for p in ours], [6001],
                         "the fixture pane is not visible even as OUR OWN, so "
                         "its later absence would prove nothing")

        with mock.patch.object(os, "getuid", return_value=os.getuid() + 1):
            foreign = hooks.running_panes(proc)
        self.assertEqual(foreign, [],
                         "another user's claude entered our census; with an "
                         "unreadable environ it would post as OUR blind pane "
                         "and taint every coverage claim downstream")

    def test_a_process_that_exits_MID_SCAN_is_gone_not_a_blind_pane(self):
        """GONE AND BLIND ARE THE SAME OSError FROM HERE, and only evidence
        separates them.

        A process that exits between the ownership stat and the environ read
        raises exactly what a hardened /proc raises. Treating it as blind
        mints a permanent "coverage UNKNOWN — look, do NOT relaunch" row about
        a pid that no longer exists, and an operator cannot even go look.

        The exit is simulated where it really happens: the detector reads come
        first, so a side effect there lands between ownership and environ."""
        proc = self.mk_proc(6002, [b"claude"], [b"HELM_CHAT_NAME=leaver"])
        os.environ["HELM_PROC"] = proc
        base = os.path.join(proc, "6002")

        # CONTROL: alive, this pane IS reported.
        self.assertEqual([p["pid"] for p in hooks.running_panes(proc)], [6002])

        real_comm = beacons.proc_comm

        def exit_now(pid, proc_dir=None):
            if str(pid) == "6002":
                shutil.rmtree(base, ignore_errors=True)
            return real_comm(pid, proc_dir)

        with mock.patch.object(beacons, "proc_comm", exit_now):
            panes = hooks.running_panes(proc)
        self.assertEqual(panes, [],
                         "a departed process was reported as a live pane "
                         "whose identity could not be read")

    def test_a_pid_whose_OWNER_cannot_be_proven_is_excluded(self):
        """FAILS CLOSED ON OWNERSHIP, and the asymmetry is deliberate.

        Admitting a pane we cannot prove is ours taints every downstream
        certainty claim; excluding one can only lose a process whose /proc
        entry would not stat, which on Linux procfs means it is gone — the
        directory metadata is world-readable even when its contents are not.
        This is the ONE place in this file where a silent exclusion is right,
        and it is right because the alternative is the measured harm."""
        proc = self.mk_proc(6003, [b"claude"], [b"HELM_CHAT_NAME=opaque"])
        os.environ["HELM_PROC"] = proc
        base = os.path.join(proc, "6003")

        self.assertEqual([p["pid"] for p in hooks.running_panes(proc)], [6003],
                         "control: the pane is visible when its owner reads")

        real_stat = os.stat

        def blind_stat(path, *a, **kw):
            if str(path) == base:
                raise PermissionError(13, "Permission denied")
            return real_stat(path, *a, **kw)

        with mock.patch.object(os, "stat", blind_stat):
            panes = hooks.running_panes(proc)
        self.assertEqual(panes, [],
                         "a pid whose owner could not be established was "
                         "admitted to a census defined by ownership")

    def test_a_detector_that_could_not_READ_is_not_a_detector_that_said_no(self):
        """is_agent(None, None) IS FALSE AND MEANS NOTHING.

        When cmdline AND comm both refuse to be read, the detector returns the
        same False it returns for vim. One is a judgement, the other is a read
        that never happened — and collapsing them deleted the pid, which is an
        absence claim the read never supported: the exact defect this whole
        lane started on, one layer further up.

        The two are separated by EVIDENCE, not by the answer."""
        proc = self.mk_proc(6004, [b"claude"], [b"HELM_CHAT_NAME=mute"])
        os.environ["HELM_PROC"] = proc

        with mock.patch.object(beacons, "proc_argv", lambda *a, **k: None), \
             mock.patch.object(beacons, "proc_comm", lambda *a, **k: None):
            panes = hooks.running_panes(proc)
        self.assertEqual([p["pid"] for p in panes], [6004],
                         "an unclassifiable process of ours was dropped")
        row = panes[0]
        self.assertTrue(row["detect_unknown"], "not marked unclassified")
        self.assertTrue(row["environ_unreadable"],
                        "an unclassifiable pane must reach the consumers' "
                        "UNKNOWN buckets, not read as a named healthy pane")
        self.assertIsNone(row["seat"])

    def test_a_process_that_ANSWERED_and_is_not_claude_is_a_clean_no(self):
        """THE NEGATIVE CONTROL FOR THE ARM ABOVE, and without it that arm
        would be satisfied by carrying every process on the box.

        vim's cmdline reads fine and says vim. That is a judgement, so it
        leaves — and it must NOT arrive as an unclassified row."""
        proc = self.mk_proc(6005, [b"claude"], [b"HELM_CHAT_NAME=real"])
        self.mk_proc(6006, [b"vim", b"notes.md"], [b"TERM=xterm"])
        os.environ["HELM_PROC"] = proc

        panes = hooks.running_panes(proc)
        self.assertEqual([p["pid"] for p in panes], [6005],
                         "a process that answered and is not claude was "
                         "carried anyway")
        self.assertEqual([p for p in panes if p.get("detect_unknown")], [],
                         "a clean no was recorded as unclassified")

    def test_a_pane_that_answered_NO_read_says_so_instead_of_blaming_environ(self):
        """SAME BUCKET, SAME REMEDY, DIFFERENT DIAGNOSIS.

        Both an EACCES environ and a process that refused every read land in
        the UNKNOWN bucket, and that is correct — the remedy is identical
        (look, do not relaunch). But a BUCKET IS NOT A DIAGNOSIS. "environ
        unreadable" sends an operator to the environ; a pane whose cmdline and
        comm also refused is a process that answered NOTHING, and the thing to
        look at is the process. The row is the only place that difference can
        still be told, so it has to be told there.

        Caught by rendering the surface end-to-end rather than by a test: the
        fixture was green and the sentence was still wrong."""
        proc = self.mk_proc(7101, [b"claude"], [b"HELM_CHAT_NAME=mute"])
        os.environ["HELM_PROC"] = proc

        with mock.patch.object(beacons, "proc_argv", lambda *a, **k: None), \
             mock.patch.object(beacons, "proc_comm", lambda *a, **k: None):
            buf = io.StringIO()
            hooks.surface_uncovered(out=buf)
        mute = buf.getvalue()

        # CONTROL: the OTHER cause, same bucket, on the same surface — without
        # it, "says no-read" would pass on a build that said no-read for every
        # blind pane, which is the same conflation one direction over.
        #
        # mk_proc RETURNS ONE SHARED ROOT, so the first pane has to leave
        # before the second arrives. Leaving it in place made this arm fail
        # for a reason that had nothing to do with its claim: pid 7101, now
        # readable with no mocks, is an ordinary named pane off the roster and
        # correctly drew the DESTRUCTIVE header the last assertion forbids.
        shutil.rmtree(os.path.join(proc, "7101"), ignore_errors=True)
        proc2 = self.mk_proc(7102, [b"claude"], [b"HELM_CHAT_NAME=eacces"])
        os.chmod(os.path.join(proc2, "7102", "environ"), 0o000)
        os.environ["HELM_PROC"] = proc2
        buf2 = io.StringIO()
        hooks.surface_uncovered(out=buf2)
        eacces = buf2.getvalue()

        def section(text, header_fragment):
            """Only the rows under ONE header.

            ASSERTING ON THE WHOLE REPORT IS THE BUG THIS ARM IS ABOUT, one
            level up: coverage and signing each emit their own reason, so a
            claim about the COVERAGE wording is satisfied by the SIGNING row
            carrying it. Mutation proved it — collapsing the coverage reason
            back to a single string SURVIVED an assertIn over the full text."""
            keep, out = False, []
            for line in text.splitlines():
                if line.startswith("helm hooks:"):
                    keep = header_fragment in line
                elif keep:
                    out.append(line)
            return "\n".join(out)

        # UNCONDITIONAL POSITIVE CONTROL, hoisted out of the loop: every
        # check below lives inside it, so an empty surface tuple would pass
        # the whole arm while reading nothing.
        rows = section(mute, "COVERAGE COULD NOT BE READ")
        self.assertIn("7101", rows, "the pane is absent from the report")
        # ...and the same for the control's observable, because the NotIn
        # assertions below read `clean_rows`: a positive on `rows` says
        # nothing about it, and an empty slice satisfies any NotIn.
        clean_rows = section(eacces, "COVERAGE COULD NOT BE READ")
        self.assertIn("7102", clean_rows,
                      "the control pane is absent from the control report")

        for surface in ("COVERAGE COULD NOT BE READ", "SIGNING COULD NOT BE READ"):
            rows = section(mute, surface)
            self.assertTrue(rows, "%s produced no rows for the pane" % surface)
            self.assertIn("refused", rows,
                          "%s reported a process that refused EVERY read as "
                          "an environ problem, sending the operator to the "
                          "wrong place" % surface)
            clean_rows = section(eacces, surface)
            self.assertTrue(clean_rows, "%s produced no control rows" % surface)
            self.assertIn("environ unreadable", clean_rows,
                          "%s lost the EACCES diagnosis" % surface)
            self.assertNotIn(  # noqa: VACUOUS_ASSERTION — mutation-proven
                "refused", clean_rows,
                             "%s leaked the no-read wording onto a pane whose "
                             "cmdline and comm answered perfectly well"
                             % surface)

        # AND BOTH STAY NON-DESTRUCTIVE. The diagnosis may differ; the remedy
        # may not. A wording change that promoted either to a relaunch would
        # spend an operator's context on a guess.
        for label, text in (("no-read", mute), ("eacces", eacces)):
            for line in text.splitlines():
                if line.startswith("helm hooks:"):
                    self.assertIn("do NOT relaunch", line,
                                  "%s rendered under a destructive header: %s"
                                  % (label, line))

    def test_a_pid_RECYCLED_mid_scan_does_not_re_enter_as_our_blind_pane(self):
        """THE RE-CHECK RE-ASKS OWNERSHIP, NOT MERELY EXISTENCE.

        A bare exists() reopens the hole the st_uid check closes: if the pid is
        recycled between the ownership stat and the environ failure, exists()
        answers yes about a DIFFERENT process — possibly another user's — and
        it re-enters as our blind pane, which is exactly the taint @codex-2
        found. Rare, and rarity is not a property anything enforces.

        Simulated where it really happens: the detector reads sit between the
        two ownership questions, so a side effect there IS the recycle."""
        proc = self.mk_proc(7203, [b"claude"], [b"HELM_CHAT_NAME=recycled"])
        os.environ["HELM_PROC"] = proc
        env = os.path.join(proc, "7203", "environ")

        # CONTROL: unrecycled and unreadable, this pane IS reported blind —
        # without it, the absence below would pass on any always-empty scan.
        os.chmod(env, 0o000)
        blind = hooks.running_panes(proc)
        self.assertEqual([p["pid"] for p in blind], [7203],
                         "the blind pane is not reported even unrecycled")
        self.assertTrue(blind[0]["environ_unreadable"])

        ours, theirs = os.getuid(), os.getuid() + 1
        real_comm = beacons.proc_comm
        seen = {"uid": ours}

        def recycle(pid, proc_dir=None):
            if str(pid) == "7203":
                seen["uid"] = theirs      # the pid now belongs to someone else
            return real_comm(pid, proc_dir)

        real_stat = os.stat

        def owner_of(path, *a, **kw):
            st = real_stat(path, *a, **kw)
            if str(path) == os.path.join(proc, "7203"):
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev,
                                       st.st_nlink, seen["uid"], st.st_gid,
                                       st.st_size, int(st.st_atime),
                                       int(st.st_mtime), int(st.st_ctime)))
            return st

        with mock.patch.object(beacons, "proc_comm", recycle), \
             mock.patch.object(os, "stat", owner_of):
            panes = hooks.running_panes(proc)
        self.assertEqual(panes, [],
                         "a pid recycled to another user re-entered our "
                         "census as OUR blind pane")

    def test_a_pid_gone_BEFORE_or_BETWEEN_the_detector_reads_is_not_UNCLASSIFIED(self):
        """THE ARM THE CURE ITSELF INTRODUCED did not inherit the cure.

        The environ path re-asks ownership before calling a pane blind. The
        UNCLASSIFIED path appended IMMEDIATELY, so a process that exited
        between the ownership stat and the detector reads became a permanent
        "coverage UNKNOWN — look, do NOT relaunch" row about a pid that no
        longer exists. An operator cannot go look at a pid that is gone.

        @codex-2 found it. A new branch is a new place for the class to live
        and does not inherit its sibling's fix — which is the whole argument
        for `ours()` being one named predicate rather than an inline stat.

        THE HARDENED CONTROL IS THE OTHER HALF and it runs FIRST: a process
        that is still ours and merely unreadable MUST survive. Without it,
        deleting the UNCLASSIFIED branch entirely would satisfy the absence
        assertions below, and the cure would have quietly become the bug."""
        # CONTROL — still present, still ours, both reads refuse: CARRIED.
        proc = self.mk_proc(7330, [b"claude"], [b"HELM_CHAT_NAME=hardened"])
        os.environ["HELM_PROC"] = proc
        with mock.patch.object(beacons, "proc_argv", lambda *a, **k: None), \
             mock.patch.object(beacons, "proc_comm", lambda *a, **k: None):
            kept = hooks.running_panes(proc)
        self.assertEqual([p["pid"] for p in kept], [7330],
                         "a hardened but still-owned process was DROPPED — "
                         "the cure deleted the case it exists for")
        self.assertTrue(kept[0]["detect_unknown"])
        shutil.rmtree(os.path.join(proc, "7330"), ignore_errors=True)

        # EXIT BETWEEN THE TWO DETECTOR READS: proc_argv is what kills it.
        proc2 = self.mk_proc(7331, [b"claude"], [b"HELM_CHAT_NAME=leaver"])
        os.environ["HELM_PROC"] = proc2
        base = os.path.join(proc2, "7331")

        def exit_then_none(pid, proc_dir=None):
            if str(pid) == "7331":
                shutil.rmtree(base, ignore_errors=True)
            return None

        with mock.patch.object(beacons, "proc_argv", exit_then_none), \
             mock.patch.object(beacons, "proc_comm", lambda *a, **k: None):
            panes = hooks.running_panes(proc2)
        self.assertEqual(panes, [],
                         "a pid that exited between the ownership stat and "
                         "the detector reads was reported as an UNCLASSIFIED "
                         "live pane")

        # EXIT BEFORE BOTH READS, same requirement by a different route.
        proc3 = self.mk_proc(7332, [b"claude"], [b"HELM_CHAT_NAME=early"])
        os.environ["HELM_PROC"] = proc3
        base3 = os.path.join(proc3, "7332")
        real_stat = os.stat

        def stat_then_vanish(path, *a, **kw):
            st = real_stat(path, *a, **kw)
            if str(path) == base3:
                shutil.rmtree(base3, ignore_errors=True)
            return st

        with mock.patch.object(os, "stat", stat_then_vanish), \
             mock.patch.object(beacons, "proc_argv", lambda *a, **k: None), \
             mock.patch.object(beacons, "proc_comm", lambda *a, **k: None):
            panes3 = hooks.running_panes(proc3)
        self.assertEqual(panes3, [],
                         "a pid that vanished right after its ownership stat "
                         "was reported as an UNCLASSIFIED live pane")

    def test_no_row_leaves_the_scan_without_the_COHERENCE_seam(self):
        """THE PROMISE @opus-integrator BOUND ACCEPTANCE ON, made structural.

        NOT A LIVENESS GATE, which is what this arm used to call it and what
        the name said until @codex-2 pointed at the prose. Liveness is the
        smaller half. The seam enforces INCARNATION COHERENCE for every row —
        that all the reads describe ONE process, so the row is true at SOME
        instant — and OWNERSHIP REPROOF for uncertainty rows only. A gate that
        merely asked "is it still alive" would pass a pid recycled to another
        process, which is the defect that made this the seam it is.

        A /proc scan is N reads with N-1 gaps and the process can exit in
        every one. We cured them one at a time — the ownership/environ gap,
        then (@codex-2, pid 7331) the gap BETWEEN the two detector reads — and
        each cure fit the pair we were looking at. That converges only by
        exhausting the gaps.

        The race is not at the boundaries; it is between the evidence gathered
        and the row emitted, and there is exactly ONE of those. So one gate
        sits there, and this arm makes it impossible to route around: inside
        running_panes, a row may reach `out` ONLY through `emit`, and `emit`
        must ask `ours`. A read added anywhere above still precedes the gate,
        so a NEW boundary cannot reopen the race — and a new APPEND path,
        which is the other way to reopen it, fails here.

        The runtime arms prove the gate WORKS on today's boundaries; this
        proves no future boundary can bypass it. Neither alone is the promise."""
        import ast
        src = os.path.join(os.path.dirname(os.path.abspath(hooks.__file__)),
                           "hooks.py")
        with open(src, encoding="utf-8") as f:
            text = f.read()

        def offenders(text):
            """(function, lineno) for every append to the output list that is
            not inside the gate."""
            tree = ast.parse(text)
            scan = [n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "running_panes"]
            if not scan:
                return [("running_panes", 0)]     # missing is not passing
            scan = scan[0]
            gate = [n for n in ast.walk(scan)
                    if isinstance(n, ast.FunctionDef) and n.name == "emit"]
            gated = {id(c) for g in gate for c in ast.walk(g)}
            bad = []
            for n in ast.walk(scan):
                if (isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "append"
                        and isinstance(n.func.value, ast.Name)
                        and n.func.value.id == "out"
                        and id(n) not in gated):
                    bad.append(("running_panes", n.lineno))
            if not gate:
                bad.append(("emit", 0))            # no gate at all
                return bad
            calls = {c.func.id for g in gate for c in ast.walk(g)
                     if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
            attrs = {c.func.attr for g in gate for c in ast.walk(g)
                     if isinstance(c, ast.Call)
                     and isinstance(c.func, ast.Attribute)}
            # BOTH PROPERTIES, checked separately, because each was found by
            # a different repro and either alone leaves a live defect:
            # ownership reproof does not see a same-uid pid RECYCLE, and the
            # incarnation token does not make a failed read safe to publish.
            if "ours" not in calls:
                bad.append(("gate-does-not-reprove-ownership", gate[0].lineno))
            if "proc_starttime" not in attrs and "proc_starttime" not in calls:
                bad.append(("gate-does-not-check-incarnation", gate[0].lineno))
            return bad

        # POSITIVE CONTROLS — each is a plausible way a later change reopens
        # the race. Unconditional first, so an empty control map cannot pass
        # this test while checking nothing.
        naked = offenders(
            "def running_panes(proc=None):\n"
            "    out = []\n"
            "    def emit(base, row):\n"
            "        if ours(base):\n"
            "            out.append(row)\n"
            "    out.append({})\n"
            "    return out\n")
        self.assertTrue(naked, "an append OUTSIDE the gate was accepted")

        controls = {
            "a gate that no longer asks ours()":
                "def running_panes(proc=None):\n"
                "    out = []\n"
                "    def emit(pid, base, token, row):\n"
                "        if beacons.proc_starttime(pid) == token:\n"
                "            out.append(row)\n"
                "    return out\n",
            "a gate that reproves ownership but not incarnation — blind to a "
            "same-uid pid RECYCLE across successful reads":
                "def running_panes(proc=None):\n"
                "    out = []\n"
                "    def emit(pid, base, token, row):\n"
                "        if ours(base):\n"
                "            out.append(row)\n"
                "    return out\n",
            "no gate at all":
                "def running_panes(proc=None):\n"
                "    out = []\n"
                "    return out\n",
            "the producer renamed away entirely":
                "def something_else(proc=None):\n"
                "    return []\n",
        }
        self.assertEqual(len(controls), 4, "controls were dropped")
        for why, snippet in controls.items():
            self.assertTrue(offenders(snippet),
                            "GUARD IS BLIND: it accepted %s" % why)

        # NEGATIVE CONTROL — the sanctioned shape must pass, or a red on the
        # real file below is noise rather than a finding.
        ok = offenders(
            "def running_panes(proc=None):\n"
            "    out = []\n"
            "    def emit(pid, base, token, row):\n"
            "        if beacons.proc_starttime(pid) != token:\n"
            "            return\n"
            "        if row.get('environ_unreadable') and not ours(base):\n"
            "            return\n"
            "        out.append(row)\n"
            "    emit(1, '/proc/1', 7, {})\n"
            "    return out\n")
        self.assertEqual(ok, [],
                         "GUARD IS OVER-BROAD: it rejected the sanctioned "
                         "gate shape")

        found = offenders(text)
        # noqa: VACUOUS_ASSERTION — INTENTIONAL absence, mutation-proven: Z1
        # (incarnation check removed), Z2 (ownership reproof removed) and Z3
        # (a direct append outside the seam) each turn this red. The
        # unconditional `naked` positive above fires on an ungated append.
        self.assertEqual(  # noqa: VACUOUS_ASSERTION — Z1/Z2/Z3 kill this
                         found, [],
                         "a row can reach the output without the coherence "
                         "seam, so a process that exited or was recycled "
                         "mid-scan can still be reported: %r" % (found,))

    @staticmethod
    def _stat_bytes(pid, starttime, comm="claude"):
        """A /proc/<pid>/stat whose field 22 is `starttime`.

        proc_starttime parses after the LAST ')' and takes index 19, so the
        state char is index 0 and eighteen fillers sit between."""
        return ("%d (%s) S " % (pid, comm) + "0 " * 18 + str(starttime)).encode()

    def test_reads_that_ALL_SUCCEED_across_a_pid_recycle_emit_nothing(self):
        """A ROW TRUE AT NO INSTANT — @codex-2's pid-7441 repro.

        Every previous cure on this producer was about a read that FAILED.
        This one is about reads that all SUCCEED and describe two different
        processes: the kernel hands the pid to something else between them,
        so argv says claude and comm/environ say vim, and the row emitted is
        a claude pane wearing vim's identity. Ownership reproof cannot see it
        — the recycled process has the same uid — and neither can any
        failed-read seam, because nothing failed.

        The bracket is beacons.proc_starttime, helm's canonical anti-reuse
        token: read before the scan, checked at emit. This is COHERENCE, not
        freshness — it does not claim the process is still alive, only that
        the evidence is about ONE process."""
        proc = self.mk_proc(7441, [b"claude"], [b"HELM_CHAT_NAME=real-agent"])
        d = os.path.join(proc, "7441")
        with open(os.path.join(d, "stat"), "wb") as f:
            f.write(self._stat_bytes(7441, 100))
        with open(os.path.join(d, "comm"), "w") as f:
            f.write("claude\n")
        os.environ["HELM_PROC"] = proc

        # CONTROL FIRST: unrecycled, this pane IS emitted with its real name.
        kept = hooks.running_panes(proc)
        self.assertEqual([(p["pid"], p["seat"]) for p in kept],
                         [(7441, "real-agent")],
                         "the control pane is not emitted even unrecycled, so "
                         "its later absence would prove nothing")

        real_comm = beacons.proc_comm

        def recycle(pid, proc_dir=None):
            """The pid changes hands between proc_argv and proc_comm — and
            every read still SUCCEEDS."""
            if str(pid) == "7441":
                with open(os.path.join(d, "cmdline"), "wb") as f:
                    f.write(b"vim\0notes.md\0")
                with open(os.path.join(d, "comm"), "w") as f:
                    f.write("vim\n")
                with open(os.path.join(d, "environ"), "wb") as f:
                    f.write(b"HELM_CHAT_NAME=recycled-nonagent\0")
                with open(os.path.join(d, "stat"), "wb") as f:
                    f.write(self._stat_bytes(7441, 200, comm="vim"))
            return real_comm(pid, proc_dir)

        with mock.patch.object(beacons, "proc_comm", recycle):
            panes = hooks.running_panes(proc)

        # PROVE THE READS SUCCEEDED, or this passes for the boring reason that
        # something failed and an older cure caught it.
        self.assertIsNotNone(beacons.proc_argv(7441, proc),
                             "cmdline unreadable — wrong mechanism under test")
        self.assertIsNotNone(beacons.proc_comm(7441, proc),
                             "comm unreadable — wrong mechanism under test")

        # noqa: VACUOUS_ASSERTION — INTENTIONAL absence: emitting NOTHING is
        # the property. The control above proves the same pane IS emitted
        # unrecycled, and the two assertIsNotNone checks prove the reads
        # SUCCEEDED, so this is not an older cure firing. Mutations Z1 and Z4
        # both turn it red.
        self.assertEqual(  # noqa: VACUOUS_ASSERTION — Z1/Z4 kill this
                         panes, [],
                         "a row was emitted from reads that straddled a pid "
                         "recycle — claude by argv, vim by comm and environ, "
                         "true at no instant: %r" % (panes,))

    def test_a_pane_whose_incarnation_never_changes_is_still_emitted(self):
        """THE OVER-CORRECTION CONTROL for the arm above.

        Discarding every row would satisfy it. The bracket must reject only a
        CHANGED incarnation, so an ordinary pane whose token is stable across
        the whole scan has to survive — with its real identity intact."""
        proc = self.mk_proc(7442, [b"claude"], [b"HELM_CHAT_NAME=steady"])
        d = os.path.join(proc, "7442")
        with open(os.path.join(d, "stat"), "wb") as f:
            f.write(self._stat_bytes(7442, 4242))
        with open(os.path.join(d, "comm"), "w") as f:
            f.write("claude\n")
        os.environ["HELM_PROC"] = proc

        panes = hooks.running_panes(proc)
        self.assertEqual([(p["pid"], p["seat"]) for p in panes],
                         [(7442, "steady")],
                         "a stable pane was discarded by the incarnation "
                         "bracket, which would empty the census on any box "
                         "whose /proc actually carries stat")
        self.assertFalse(panes[0]["environ_unreadable"])

    def test_the_OWNERSHIP_proof_cannot_belong_to_another_incarnation(self):
        """@codex-2's pid-7443 repro, and it defeats a bracket that is
        truthful about everything except the proof that matters.

        ours(base) observes same-uid process A; A is replaced by a
        FOREIGN-uid claude B before that check returns; token, reads and
        re-token then all agree, coherently, about B. Every mechanism was
        honest and a foreign pane entered our census with a clean bill of
        health, because the ownership proof was taken OUTSIDE the bracket and
        described a different incarnation than the evidence did.

        The cure is ordering, not another check: the token is taken FIRST, so
        any replacement anywhere below fails the re-check."""
        proc = self.mk_proc(7443, [b"claude"], [b"HELM_CHAT_NAME=ours-A"],
                            starttime=100)
        os.environ["HELM_PROC"] = proc
        d = os.path.join(proc, "7443")

        # CONTROL: unswapped, this pane IS emitted under its own name.
        self.assertEqual([(p["pid"], p["seat"])
                          for p in hooks.running_panes(proc)],
                         [(7443, "ours-A")],
                         "control pane absent even unswapped")

        real_stat = os.stat
        swapped = {"done": False}

        def ours_then_theirs(path, *a, **kw):
            """The uid read answers about A, and B lands before it returns."""
            st = real_stat(path, *a, **kw)
            if str(path) == d and not swapped["done"]:
                swapped["done"] = True
                with open(os.path.join(d, "environ"), "wb") as f:
                    f.write(b"HELM_CHAT_NAME=foreign-new\0")
                with open(os.path.join(d, "stat"), "wb") as f:
                    f.write(("7443 (claude) S " + "0 " * 18 + "200").encode())
            return st

        with mock.patch.object(os, "stat", ours_then_theirs):
            panes = hooks.running_panes(proc)
        self.assertEqual(  # noqa: VACUOUS_ASSERTION — control above proves emission
            panes, [],
            "a FOREIGN process entered our census because the ownership "
            "proof described an earlier incarnation than the evidence: %r"
            % (panes,))

    def test_an_UNPROVABLE_incarnation_downgrades_instead_of_validating(self):
        """A MISSING PIN MUST NOT VALIDATE DERIVED FIELDS.

        None == None is not agreement, it is two failures, and production
        /proc can withhold stat. Every identity field is derived from reads
        this scan then cannot bind to one incarnation — so the row keeps only
        what the evidence supports: a process is here and we could not
        establish what it is.

        NEITHER SILENTLY DROPPED NOR SILENTLY TRUSTED. Dropping it is the
        founding defect of this whole lane (an absence claim the read never
        supported); trusting it publishes a seat name nothing pinned."""
        proc = self.mk_proc(7444, [b"claude"], [b"HELM_CHAT_NAME=unpinned"])
        os.environ["HELM_PROC"] = proc

        # CONTROL: WITH the pin, the identity is published.
        pinned = hooks.running_panes(proc)
        self.assertEqual([(p["pid"], p["seat"]) for p in pinned],
                         [(7444, "unpinned")],
                         "the pinned control never published its identity, so "
                         "the downgrade below would prove nothing")

        os.unlink(os.path.join(proc, "7444", "stat"))
        panes = hooks.running_panes(proc)
        self.assertEqual([p["pid"] for p in panes], [7444],
                         "an unpinnable process was DROPPED — an absence "
                         "claim the read never supported")
        self.assertIsNone(panes[0]["seat"],
                          "a seat name survived with nothing pinning it to "
                          "the process the name was read from")
        # ITS OWN CAUSE BIT. Reusing detect_unknown made both surfaces tell
        # an operator this process "refused both identity reads" when cmdline,
        # comm and environ had all answered perfectly — a false diagnosis
        # pointing at the wrong instrument. @codex-2 caught it.
        self.assertTrue(panes[0]["incarnation_unknown"])
        self.assertNotIn("detect_unknown", panes[0],
                         "the pin failure borrowed the DETECTOR's cause bit")
        self.assertTrue(panes[0]["environ_unreadable"])

    def test_the_UNPINNABLE_row_names_the_pin_on_BOTH_surfaces(self):
        """END-TO-END WORDING, coverage AND signing, because the cause bit
        only matters if it reaches the operator's line.

        The instrument that failed is the incarnation pin. cmdline, comm and
        environ may all have answered perfectly, so a row saying "refused
        both identity reads" sends someone to inspect reads that worked."""
        proc = self.mk_proc(7445, [b"claude"], [b"HELM_CHAT_NAME=unpinned2"])
        os.environ["HELM_PROC"] = proc
        os.unlink(os.path.join(proc, "7445", "stat"))

        buf = io.StringIO()
        hooks.surface_uncovered(out=buf)
        text = buf.getvalue()
        self.assertIn("7445", text, "the pane never reached the report")

        section, seen = None, {}
        for line in text.splitlines():
            if line.startswith("helm hooks:"):
                section = ("coverage" if "COVERAGE" in line else
                           "signing" if "SIGNING" in line else None)
                self.assertIn("do NOT relaunch", line,
                              "an unpinnable pane drew a DESTRUCTIVE header")
            elif section and "7445" in line:
                seen[section] = line

        self.assertEqual(sorted(seen), ["coverage", "signing"],
                         "the pane is missing from a surface: %r" % (seen,))
        for surface, line in seen.items():
            self.assertIn("pinned to one incarnation", line,
                          "%s does not name the instrument that failed: %s"
                          % (surface, line))
            self.assertNotIn("refused", line,
                             "%s blames reads that ANSWERED: %s"
                             % (surface, line))

    def test_ONE_report_never_gives_one_pid_OPPOSITE_actions(self):
        """THE COMPOSITION DEFECT: four correct buckets, one incoherent report.

        surface_uncovered called uncovered_panes() and unsigned_panes()
        separately and EACH walked /proc itself, so one report mixed TWO
        SNAPSHOTS. @codex-2 built the same-pid case: coverage UNKNOWN from
        scan 1, then "posting UNSIGNED — relaunch" WITH A SEAT NAME from scan
        2. One pid, two rows, OPPOSITE recommended actions, each individually
        correct about the snapshot it saw. Per-item correctness is never the
        standard; the composition is.

        This is the split-clock defect in another currency — two READS of one
        population where the report claims one observation.

        THE ASSERTION IS THE ACTION, NOT THE ROW COUNT. Two rows for one pid
        are FINE when they agree: coverage-unknown and signing-unknown are
        distinct facts with the same remedy. My first check counted rows and
        called the cured version broken."""
        proc = self.mk_proc(7777, [b"claude"], [b"HELM_CHAT_NAME=flipper"])
        env = os.path.join(proc, "7777", "environ")
        os.chmod(env, 0o000)
        os.environ["HELM_PROC"] = proc

        calls = {"n": 0}
        real = hooks.running_panes

        def flipping(p=None):
            calls["n"] += 1
            if calls["n"] == 2:          # readable by the time a 2nd scan runs
                os.chmod(env, 0o644)
            return real(p)

        with mock.patch.object(hooks, "running_panes", flipping):
            buf = io.StringIO()
            hooks.surface_uncovered(out=buf)
        text = buf.getvalue()

        # POSITIVE CONTROL: the report must actually mention the pane, or the
        # no-contradiction assertion below is satisfied by an empty report.
        self.assertIn("7777", text, "the pane is absent from the report")

        actions, header = [], None
        for line in text.splitlines():
            if line.startswith("helm hooks:"):
                header = ("DO-NOT-RELAUNCH" if "do NOT relaunch" in line
                          else "RELAUNCH")
            elif "7777" in line and header:
                actions.append(header)
        self.assertTrue(actions, "no row for the pane sat under any header")
        self.assertEqual(len(set(actions)), 1,
                         "ONE report gives pid 7777 opposite actions %r — it "
                         "read /proc twice and mixed two snapshots, so an "
                         "operator is told both to relaunch it and not to"
                         % (actions,))
        self.assertEqual(calls["n"], 1,
                         "the report scanned /proc %d times; one report is "
                         "one observation" % calls["n"])

    def test_a_blind_pane_is_signing_UNKNOWN_never_silently_healthy(self):
        """THE FOURTH CONSUMER, and the one I nearly shipped on REASONING.

        I wrote: unsigned_panes reads the SIGNER TRIO, not identity; a blind
        pane has no readable trio; therefore it belongs in NEITHER bucket.
        Every clause is true and the conclusion is wrong — "belongs in neither
        bucket" quietly means RENDERS AS CAN-SIGN, a claim the read never
        supported. Measured: a planted pane carrying a FULL trio behind an
        unreadable environ was reported by nothing at all.

        Four findings on this lane came from CONSTRUCTION and none from
        reasoning; this is the one where I had already written the wrong
        reasoning down and only caught it by building the case.
        (Converged with @codex-2 in a convergence meld.)"""
        proc = self.mk_proc(9001, [b"claude"], [b"HELM_CHAT_NAME=unsigned"])
        self.mk_proc(9002, [b"claude"], [b"HELM_CHAT_NAME=signed",
                                         b"HELM_CELL_BIN=/bin/true",
                                         b"DREGG_PROFILE=p"])
        self.mk_proc(9003, [b"claude"], [b"HELM_CHAT_NAME=blind",
                                         b"HELM_CELL_BIN=/bin/true",
                                         b"DREGG_PROFILE=p"])
        os.chmod(os.path.join(proc, "9003", "environ"), 0o000)

        got = {p["pid"]: p for p in hooks.unsigned_panes(proc)}
        # POSITIVE CONTROLS, both directions: a genuinely unsigned pane is
        # still reported, and a genuinely SIGNED one still is not. Without
        # them this passes for a function that reports everything.
        self.assertIn(9001, got, "the genuinely unsigned pane stopped being "
                                 "reported")
        self.assertIsNone(got[9001].get("sign_unknown"),
                          "a readable unsigned pane was labelled UNKNOWN")
        self.assertNotIn(9002, got, "a fully signed pane is being reported")

        self.assertIn(9003, got,
                      "the blind pane is reported by NOTHING — absent from "
                      "the unsigned bucket renders as can-sign, which the "
                      "read never supported")
        self.assertTrue(got[9003].get("sign_unknown"))
        self.assertIn("UNKNOWN", got[9003]["sign_reason"])

    def test_signing_UNKNOWN_is_not_rendered_as_a_relaunch_instruction(self):
        """Same split as the coverage bucket, same reason: "posting UNSIGNED —
        relaunch from their minted launch.sh" is a PROVEN claim with a
        DESTRUCTIVE remedy, and an unread pane has been proven nothing."""
        proc = self.mk_proc(9101, [b"claude"], [b"HELM_CHAT_NAME=unsigned"])
        self.mk_proc(9102, [b"claude"], [b"HELM_CHAT_NAME=blind",
                                         b"HELM_CELL_BIN=/bin/true",
                                         b"DREGG_PROFILE=p"])
        os.chmod(os.path.join(proc, "9102", "environ"), 0o000)
        os.environ["HELM_PROC"] = proc

        buf = io.StringIO()
        hooks.surface_uncovered(out=buf)
        text = buf.getvalue()
        # POSITIVE CONTROL: the proven-unsigned instruction still fires.
        self.assertIn("relaunch from their minted launch.sh", text)
        self.assertIn("9101", text)

        before = text.split("SIGNING COULD NOT BE READ")[0]
        self.assertNotIn("9102", before.split("posting UNSIGNED")[-1],
                         "the unread pane sits under the relaunch-to-sign "
                         "instruction — a destructive remedy on an unread fact")
        self.assertIn("9102", text, "the unread pane vanished entirely")

    def test_a_blind_pane_makes_the_project_census_report_INCOMPLETE(self):
        """THE THIRD CONSUMER, and the same fail-open each time.

        `_project_contexts` filtered `running_panes()` on `p["seat"]`, which a
        blind pane never has — its seat is None because the ENVIRON could not
        be read, not because the pane is unnamed, and the filter dropped both
        alike. Callers then received a COMPLETE-LOOKING context set:
        project_scope_rows can claim `project-only` from a census that never
        saw one of the live seats.

        The function already had the channel to say otherwise — it returns
        `unknown` the moment the ROSTER is unreadable. A blind pane is that
        same fact one level down. Contexts still ship; only the certainty is
        held back, which is the blast-radius rule beacons.agent_index states
        for its own scan. (@codex-2, third consumer of this return value.)"""
        proc = self.mk_proc(8080, [b"claude"], [b"HELM_CHAT_NAME=readable"])
        self.mk_proc(8081, [b"claude"], [b"HELM_CHAT_NAME=blind"])
        os.chmod(os.path.join(proc, "8081", "environ"), 0o000)
        os.environ["HELM_PROC"] = proc

        contexts, unknown = hooks._project_contexts()
        self.assertTrue(unknown,
                        "the census reports COMPLETE while a live pane went "
                        "unread — project_scope_rows can then claim "
                        "project-only from an incomplete scan")
        self.assertIn("incomplete", unknown)
        # POSITIVE CONTROL: the readable rows still ship. A fix that returned
        # unknown by discarding the census would pass the assertion above and
        # be strictly worse than the defect.
        self.assertIsInstance(contexts, list)

    def test_an_all_readable_census_reports_COMPLETE(self):
        """The other direction, without which the arm above is satisfied by a
        function that always says incomplete."""
        proc = self.mk_proc(8090, [b"claude"], [b"HELM_CHAT_NAME=readable"])
        os.environ["HELM_PROC"] = proc
        _contexts, unknown = hooks._project_contexts()
        self.assertIsNone(unknown,
                          "a fully readable scan reported %r — the census "
                          "cries wolf and its UNKNOWN stops meaning anything"
                          % (unknown,))
        # POSITIVE CONTROL ON THE SAME CALL, unconditional: make ONE pane
        # unreadable and the identical call must speak. Without it, a
        # _project_contexts that NEVER reports unknown satisfies the silence
        # above — which is the exact defect this pair exists to catch, passing
        # as its own cure.
        os.chmod(os.path.join(proc, "8090", "environ"), 0o000)
        _contexts, unknown = hooks._project_contexts()
        self.assertTrue(unknown,
                        "the same call stayed silent with a pane it could "
                        "not read, so the silence above measures nothing")

    def test_UNKNOWN_coverage_is_not_rendered_as_a_relaunch_instruction(self):
        """MAKING A PANE VISIBLE PUT IT UNDER THE WRONG HEADER.

        surface_uncovered printed every row of uncovered_panes beneath a
        DEFINITE "NOT receiving fleet chat — relaunch them", so the pane the
        previous fix stopped dropping arrived as `<invalid>` with an UNKNOWN
        reason under an instruction that asserts the opposite. The header
        claimed a fact the row denied, and the recommended action is the
        DESTRUCTIVE one — a relaunch discards the pane's context, so an
        operator following it spends real work on an unread fact.

        @codex-2 found this in the consumer I had explicitly flagged as
        unaudited when I dispatched the previous cure."""
        proc = self.mk_proc(7070, [b"claude"], [b"HELM_CHAT_NAME=epsilon"])
        self.mk_proc(7071, [b"claude"], [b"HELM_CHAT_NAME=zeta"])
        os.chmod(os.path.join(proc, "7071", "environ"), 0o000)
        os.environ["HELM_PROC"] = proc

        buf = io.StringIO()
        hooks.surface_uncovered(out=buf)
        text = buf.getvalue()

        # POSITIVE CONTROL: the DEFINITE header still fires for the pane we
        # genuinely proved uncovered, so a pass here cannot mean the relaunch
        # advice simply stopped being printed.
        self.assertIn("relaunch them", text)
        self.assertIn("epsilon", text)

        before_unknown = text.split("COULD NOT BE READ")[0]
        self.assertNotIn("7071", before_unknown,
                         "the unreadable pane is rendered under the DEFINITE "
                         "relaunch header — the operator is told to discard "
                         "the context of a pane whose coverage was never read")
        self.assertIn("do NOT relaunch", text)
        self.assertIn("7071", text,
                      "the unknown pane vanished again; it must be SHOWN, "
                      "just not under a relaunch instruction")

    def test_an_unreadable_pane_reads_UNKNOWN_coverage_never_covered(self):
        """`seat is None` means two different things and the filter conflated
        them: an UNNAMED HOME PANE (deliberately not judged here, its coverage
        rides `helm hooks status`) and a pane whose environ WE COULD NOT READ.
        The first is correctly skipped; the second must surface as UNKNOWN
        with a pid the operator can look at."""
        proc = self.mk_proc(6060, [b"claude"], [b"HELM_CHAT_NAME=gamma"])
        self.mk_proc(6061, [b"claude"], [b"HELM_CHAT_NAME=delta"])
        os.chmod(os.path.join(proc, "6061", "environ"), 0o000)

        rows = {r["pid"]: r for r in hooks.uncovered_panes(proc)}
        # POSITIVE CONTROL on the same call: the named pane still reports its
        # ordinary never-joined reason, so UNKNOWN is a decision about one
        # pane and not the surface giving up.
        self.assertIn(6060, rows, "the readable uncovered pane vanished")
        self.assertIn("never joined", rows[6060]["reason"])
        self.assertNotIn("UNKNOWN", rows[6060]["reason"])

        self.assertIn(6061, rows,
                      "the unreadable pane is absent from uncovered_panes, so "
                      "a shorter list silently reads as better coverage")
        self.assertTrue(rows[6061].get("coverage_unknown"))
        self.assertIn("UNKNOWN", rows[6061]["reason"])

    def test_seat_family_from_config_dir_and_stale_row(self):
        cdir = os.path.join(self.seats_root, "kimi", "claude")
        os.makedirs(cdir)
        proc = self.mk_proc(7, [b"claude"],
                            [b"CLAUDE_CONFIG_DIR=" + cdir.encode()])
        panes = hooks.running_panes(proc)
        self.assertEqual(panes[0]["family"], "kimi")   # named by its seat dir
        from helm import seats
        seats.write_roster("kimi")
        # honest presence (efbff44 line): write_roster no longer STAMPS a seen
        # file — a roster row names a seat, it does not witness one, and
        # conflating those was the phantom-presence bug. This test is about a
        # STALE seen row, so it plants the evidence itself instead of relying
        # on a side effect that (correctly) no longer happens.
        open(seats.seen_path("kimi"), "w").close()
        old = os.path.getmtime(seats.seen_path("kimi")) - 2000
        os.utime(seats.seen_path("kimi"), (old, old))
        rows = hooks.uncovered_panes(proc)
        self.assertEqual(len(rows), 1)
        self.assertIn("stale", rows[0]["reason"])

    def test_unnamed_home_pane_not_judged_and_missing_proc_fail_open(self):
        proc = self.mk_proc(9, [b"claude"], [b"TERM=xterm"])   # no name, no dir
        self.assertEqual(hooks.uncovered_panes(proc), [])
        self.assertEqual(hooks.running_panes(os.path.join(self.tmp, "nope")), [])

    def test_install_surfaces_uncovered_panes(self):
        self.mk_proc(4242, [b"claude"], [b"HELM_CHAT_NAME=kimi"])
        self.mk_home("a-user-dev")
        rc, out, _ = self.run_hooks(["install"])
        self.assertEqual(rc, 0)
        self.assertIn("NOT receiving fleet chat", out)
        self.assertIn("kimi", out)

    def test_hostile_pane_name_refused_without_retaining_its_laundered_alias(self):
        """A hostile HELM_CHAT_NAME is identified by process, never by the
        legitimate seat name its printable form would impersonate."""
        from helm import seats
        seats.write_roster("kimi")   # the real seat IS rostered
        proc = self.mk_proc(99, [b"claude"],
                            [b"HELM_CHAT_NAME=kimi\xe2\x80\x8d"])
        rows = hooks.uncovered_panes(proc)
        self.assertEqual(len(rows), 1, "a hostile-named pane still surfaces")
        self.assertTrue(rows[0]["identity_refused"])
        self.assertIn("identity refused", rows[0]["reason"])
        self.assertNotIn("kimi", rows[0]["reason"],
                         "the row must not retain the laundered alias")
        self.assertNotIn("never joined", rows[0]["reason"])

    def test_genuinely_unjoined_seat_still_gets_normal_never_joined_line(self):
        """MUST-HIT CONTROL: a pane with a LEGITIMATE seat token that
        genuinely never joined still gets the normal "never joined" line.
        The identity-refusal guard must NOT swallow the true case."""
        proc = self.mk_proc(99, [b"claude"],
                            [b"HELM_CHAT_NAME=genuine-unjoined-seat"])
        rows = hooks.uncovered_panes(proc)
        self.assertEqual(len(rows), 1)
        self.assertIn("never joined", rows[0]["reason"],
                      "a legitimately-unjoined seat must still get the "
                      "normal never-joined reason")
        self.assertNotIn("identity refused", rows[0]["reason"],
                         "must NOT refuse a legitimate seat token")
        self.assertFalse(rows[0].get("identity_refused"))

    def test_hostile_pane_surface_uses_pid_and_neutral_label_only(self):
        """Every section that prints the hostile pane uses a neutral label;
        the process PID is the only identity the operator can act on."""
        from helm import seats
        seats.write_roster("real-seat")
        self.mk_proc(99, [b"claude"],
                     [b"HELM_CHAT_NAME=real-seat\xe2\x80\x8d"])
        orig = hooks.running_panes
        hooks.running_panes = lambda proc=None: [{"pid": 99,
            "seat": "real-seat‍", "family": "",
            "config_dir": None, "signer_bin": None, "signer_profile": None}]
        try:
            out = io.StringIO()
            hooks.surface_uncovered(out=out)
            text = out.getvalue()
        finally:
            hooks.running_panes = orig
        self.assertIn("pid 99", text)
        self.assertIn("<invalid>", text)
        self.assertIn("identity refused", text)
        self.assertNotIn("real-seat", text,
                         "no section may retain the laundered alias")
        self.assertNotIn("never joined", text)
        self.assertNotIn("no roster row", text)


class UnsignedPanesTest(HooksBase):
    """No-silent-break for signing: a pane launched before launch_line
    carried the signing trio posts [unsigned] by configuration and cannot
    self-heal — surface it for relaunch beside the uncovered panes."""

    def mk_proc(self, pid, cmdline, env, starttime=100):
        d = os.path.join(self.tmp, "proc", str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"\0".join(cmdline) + b"\0")
        with open(os.path.join(d, "environ"), "wb") as f:
            f.write(b"\0".join(env) + b"\0")
        # A REAL /proc ENTRY CARRIES stat, AND WITHOUT IT THE INCARNATION
        # BRACKET IS INERT: proc_starttime returns None, the token compares
        # None to None, and every arm passes while exercising nothing of the
        # anti-pid-reuse guarantee. A fixture that omits what production
        # always has is not testing production. @codex-2 called this a real
        # hole and it was — I had declared it and shipped it once.
        with open(os.path.join(d, "stat"), "wb") as f:
            f.write(("%d (claude) S " % pid + "0 " * 18
                     + str(starttime)).encode())
        return os.path.join(self.tmp, "proc")

    def test_pane_without_signing_env_flagged(self):
        signer = os.path.join(self.tmp, "dregg-client-sign")
        with open(signer, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(signer, 0o755)
        proc = self.mk_proc(11, [b"claude"], [b"HELM_CHAT_NAME=oldkimi"])
        self.mk_proc(12, [b"claude"],
                     [b"HELM_CHAT_NAME=newkimi",
                      ("HELM_CELL_BIN=%s" % signer).encode(),
                      b"DREGG_PROFILE=newkimi"])
        rows = hooks.unsigned_panes(proc)
        self.assertEqual([(p["pid"], p["seat"]) for p in rows],
                         [(11, "oldkimi")])
        self.assertIn("no HELM_CELL_BIN", rows[0]["sign_reason"])

    def test_unexecutable_bin_and_missing_profile_flagged(self):
        signer = os.path.join(self.tmp, "signer")
        with open(signer, "w") as f:
            f.write("x")                       # 0644 — NOT executable
        proc = self.mk_proc(13, [b"claude"],
                            [b"HELM_CHAT_NAME=a",
                             ("HELM_CELL_BIN=%s" % signer).encode(),
                             b"DREGG_PROFILE=a"])
        self.mk_proc(14, [b"claude"],
                     [b"HELM_CHAT_NAME=b",
                      b"HELM_CELL_BIN=/bin/true"])   # bin ok, profile missing
        rows = {p["seat"]: p["sign_reason"] for p in hooks.unsigned_panes(proc)}
        self.assertEqual(rows["a"], "no HELM_CELL_BIN (pre-signing launch)")
        self.assertIn("PROFILE", rows["b"])

    def test_unnamed_pane_not_judged_and_surface_prints(self):
        proc = self.mk_proc(15, [b"claude"], [b"TERM=xterm"])
        self.assertEqual(hooks.unsigned_panes(proc), [])
        self.mk_proc(16, [b"claude"], [b"HELM_CHAT_NAME=kimi"])
        out = io.StringIO()
        hooks.surface_uncovered(out=out)       # HELM_PROC = the fake tree
        self.assertIn("UNSIGNED", out.getvalue())
        self.assertIn("kimi", out.getvalue())
        self.assertIn("launch.sh", out.getvalue())


if __name__ == "__main__":
    unittest.main()
