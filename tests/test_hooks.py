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
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# THE CHECKOUT, for the arms that EXECUTE the shipped wrapper. `bin/helm-hook`
# is a tracked file and these arms run THAT file rather than a paraphrase of
# it, so a change to the script that breaks an arm is red here.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# THE LADDER ITSELF. The rc-case arms used to be rendered INTO every hook
# command, so an arm that asked about them read the command string. They live
# in this tracked file now — the command is the short invocation of it — so an
# arm about the ARMS reads the file and an arm about the INSTALLED COMMAND
# reads the command. Conflating the two is what a text search over the command
# would now do silently, passing on a ladder that had lost the arm.
LADDER = open(os.path.join(ROOT, "bin", "helm-hook"), encoding="utf-8").read()
# THE ARMS WITHOUT THE PROSE. The script explains WHY a code is excluded, so a
# `assertNotIn("137")` over the whole file is satisfied by nothing and refuses
# the explanation instead of the arm.
LADDER_CODE = "\n".join(line for line in LADDER.splitlines()
                        if not line.lstrip().startswith("#"))

from tests._tmphome import pin_suite_guard  # noqa: E402
from helm import (beacons, configs, doctor, homes, hooks, seat,  # noqa: E402
                  seats, vcs)


# DERIVED, NEVER TRANSCRIBED (review on 3402c442a1fd). Five classes below each
# copied this string. A copy keeps passing after SPECS renames the verb, and a
# marker fixture that drifts from SPECS is not a weaker test — it is a test of
# a command nobody runs. A first probe of this lane reported three
# phantom false owners because it INVENTED a single-word token; real markers
# are multi-word and take the other branch of _own_hit entirely, so the whole
# finding evaporated under a real one. Deriving here makes both failures
# impossible: the value cannot go stale and cannot be invented.
def _spec_marker(name):
    return [s for s in hooks.SPECS if s["name"] == name][0]["args"]


_DELIVER_MARKER = _spec_marker("deliver")


class HooksBase(unittest.TestCase):
    def wrapper_beside(self, binpath):
        """Put the SHIPPED bin/helm-hook next to `binpath`, and return binpath.

        The generated command names TWO files — the wrapper and the helm it
        wraps — and `hooks.wrapper_bin` derives the first from the second. A
        stub helm that arrives without its sibling makes the command's FIRST
        word nonexistent, so the arm measures the shell's own 127 instead of
        the ladder it meant to drive. `binpath` itself is deliberately NOT
        created: the missing-guard arms need it absent.
        """
        d = os.path.dirname(binpath)
        os.makedirs(d, exist_ok=True)
        shutil.copy2(os.path.join(ROOT, "bin", hooks.HOOK_WRAPPER),
                     os.path.join(d, hooks.HOOK_WRAPPER))
        return binpath

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
        # HELM_SCRATCH_GC hermetic for a reason the other three are not: this
        # module DRIVES THE STOP HOOK, and the stop hook silently runs the
        # scratch reaper against the REAL /tmp/claude-* tree. A tmp HELM_HOME
        # does not fence that — the reaper reads its own root — so without
        # this a full-suite run would delete live dead-session scratch as a
        # side effect of testing hook installation. tests/test_scratch.py's
        # host-mutation tripwire enforces exactly this pairing across every
        # module that touches the Stop hook.
        # HELM_SUITE_GUARD hermetic for the EXTERNAL spec: the suite guard is
        # the one required executable helm does not ship, so an unpinned run
        # would resolve it off the HOST's PATH — present on the owner's box,
        # absent in CI, and the contract under test would differ between them.
        # A fixture executable makes `resolved_specs` deterministic everywhere.
        self._env_prior = {k: os.environ.get(k)
                           for k in ("HELM_HOME", "HELM_PROC", "HELM_CHAT_DIR",
                                     "HELM_SCRATCH_GC")}
        self.helm_home = j("helm-home")
        os.environ["HELM_HOME"] = self.helm_home
        os.environ["HELM_PROC"] = j("proc")        # empty ⇒ no panes found
        os.environ["HELM_CHAT_DIR"] = j("chat")
        os.environ["HELM_SCRATCH_GC"] = "0"
        self.suite_guard = pin_suite_guard(self, self.tmp)
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
        # registered like mk_home/mk_seat: configs' gated write requires the
        # path under a known root, so an in-process install_project against an
        # unregistered dir is REFUSED and never reaches the code under test.
        configs.HOME_ROOTS.append(d)
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
        """A deliberately narrowed caller omits `inject`, so an owned inject
        entry is merged by nobody on this pass — the residue the room repair
        must still see. It is repointed at the shared checkout, and the report
        names it."""
        d = self.mk_seat("family-x", {"hooks": {"UserPromptSubmit": [{"hooks": [
            {"type": "command",
             "command": "timeout 10 %s/bin/helm inject --hook-json || true"
                        % self.POISON_ROOM}]}]}})
        narrowed = tuple(s for s in hooks.SEAT_SPECS if s["name"] != "inject")
        action, detail = hooks.install_home(d, specs=narrowed)
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

        Uses the same deliberately narrowed install as the residue test above:
        a spec the install DOES carry gets its whole command regenerated, which
        would erase the argument before the rail ever saw it."""
        cmd = ("timeout 10 %s inject --hook-json --data-dir %s/state || true"
               % (hooks.helm_bin(), self.POISON_ROOM))
        d = self.mk_seat("family-y", {"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": cmd}]}]}})
        narrowed = tuple(s for s in hooks.SEAT_SPECS if s["name"] != "inject")
        action, detail = hooks.install_home(d, specs=narrowed)
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
        # DERIVED from the generator, never transcribed: this was a hardcoded
        # one-line fail-open command, and its equality with hook_command()
        # below held only while that was the shape the generator emitted. The
        # moment the advisory branch grew an rc-case, the fixture pinned a
        # command no caller writes any more. Swapping the room into the REAL
        # command also makes this a live test of the rail: the generated text
        # names the helm binary TWICE (the invocation, and the rc-127 alarm's
        # "nothing executable at ..."), so a repair that repointed only the
        # first would leave the alarm citing a deleted room.
        parked = record.hook_command().replace(hooks.helm_bin(),
                                               self.POISON_ROOM + "/bin/helm")
        self.assertIn(self.POISON_ROOM, parked)          # the swap really took
        self.assertNotIn(hooks.helm_bin(), parked)       # ...everywhere
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
        hooks.install_home(d, specs=hooks.SEAT_SPECS)         # settle
        sp = os.path.join(d, "settings.json")
        with open(sp, encoding="utf-8") as f:
            settled = f.read()
        # CONTROL: a genuinely unchanged install says ok AND writes nothing.
        action, _detail = hooks.install_home(d, specs=hooks.SEAT_SPECS)
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
        action, detail = hooks.install_home(d, specs=hooks.SEAT_SPECS)
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


class HookCurrencyTest(HooksBase):
    def _home(self, spec, mutate=None):
        entry = hooks._canonical_entry(spec)
        if mutate is not None:
            entry["hooks"][0]["command"] = mutate(
                entry["hooks"][0]["command"])
        return {"hooks": {spec["event"]: [entry]}}

    def test_drifted_rendering_is_reported(self):
        spec = next(s for s in hooks.SPECS if s["name"] == "stop-guard")
        with mock.patch.object(hooks, "helm_bin", return_value="/b/helm"):
            clean = self._home(spec)
            # AN OLDER RENDERING OF A WORKING LADDER, which is exactly what
            # an estate carries after the ladder moved into bin/helm-hook:
            # the inline rc-case every home was installed with.
            drift = self._home(spec, lambda command: hooks._gate_command_v1(
                spec, hooks._executed(command)[0]))
            self.assertNotEqual(
                clean, drift,
                "MUST-HIT: the fixture did not alter the canonical rendering")
            self.assertEqual(
                (hooks.stale_specs(clean, (spec,)),
                 hooks.stale_specs(drift, (spec,))),
                ([], ["stop-guard"]))

    def test_absent_hook_is_not_reported_as_drifted(self):
        spec = next(s for s in hooks.SPECS if s["name"] == "stop-guard")
        with mock.patch.object(hooks, "helm_bin", return_value="/b/helm"):
            self.assertEqual(
                (hooks.stale_specs({}, (spec,)),
                 hooks.stale_specs(self._home(spec), (spec,))),
                ([], []))

    def test_currency_and_unrunnable_are_different_facts(self):
        rows = hooks.status_rows()
        self.assertTrue(rows, "MUST-HIT: no home rows were inspected")
        self.assertIn("drifted", rows[0])
        self.assertIn("stale", rows[0])
        self.assertIsNot(rows[0]["drifted"], rows[0]["stale"])

    def test_resolution_failure_is_unknown_not_clean(self):
        with mock.patch.object(hooks, "resolved_specs",
                               side_effect=OSError("probe failed")):
            self.assertIsNone(hooks.stale_specs({}))

    def test_unknown_currency_does_not_count_as_covered(self):
        """UNKNOWN is still not coverage — not knowing must never round up.

        A DRIFTED ENTRY IS COVERED, and the reason is a measurement rather
        than a taste. Answering 0 for `drifted=["inject"]` makes a whole estate
        report `inject coverage 0 of 7` the first time the ladder template
        changes — seven homes whose inject hook fires on every turn, counted as
        an outage, with the one true row (an out-of-date rendering that works)
        buried under seven copies of itself. Coverage is PRESENCE; the vintage
        has its own row here and its own doctor rung.
        """
        base = {"hook": True, "resolvable": True, "fail_open": True}
        self.assertEqual(
            (hooks.inject_covered([dict(base, drifted=[])]),
             hooks.inject_covered([dict(base, drifted=["inject"])]),
             hooks.inject_covered([dict(base, drifted=None)]),
             hooks.inject_covered([dict(base)])),
            (1, 1, 0, 0))
        # THE FACT IS NOT LOST, it moved: the same row that now counts as
        # covered is the row `drifted_rows` reports, so loosening the count
        # could not make the re-render silent.
        self.assertEqual(
            len(hooks.drifted_rows([dict(base, drifted=["inject"])])), 1)
        self.assertEqual(len(hooks.drifted_rows([dict(base, drifted=[])])), 0)

    def _drift(self, home, names):
        """Age the installed rendering of `names` in `home`, in place.

        THE LEAF IS IDENTIFIED THE WAY PRODUCTION IDENTIFIES IT — by the helm
        subcommand it runs — and never by a substring of its text. A blunter
        match rewrote a SIBLING spec's leaf that happened to share an event and
        a budget, which made that sibling genuinely unrecognisable rather than
        merely older, and the arm then measured an absence it had created."""
        from helm import envtidy
        settings = self.read_settings(home)
        touched = []
        for name in names:
            spec = next(s for s in hooks.SPECS if s["name"] == name)
            for group in settings["hooks"].get(spec["event"], []):
                for leaf in group.get("hooks", []):
                    command = leaf.get("command") or ""
                    if envtidy._helm_args(command) != spec["args"]:
                        continue
                    # THE VINTAGE THE ESTATE ACTUALLY CARRIES: the inline
                    # rc-case ladder, rendered by the frozen renderer for this
                    # spec's kind. A working hook at an older spelling is the
                    # state these counts must not report as an outage.
                    frozen = (hooks._gate_command_v1 if spec.get("gate")
                              else hooks._advisory_command_v2)
                    aged = frozen(spec, hooks._executed(command)[0])
                    self.assertIsNotNone(aged, name)
                    leaf["command"] = aged
                    touched.append(name)
        self.assertEqual(sorted(touched), sorted(names),
                         "MUST-HIT: a spec named here has no standalone leaf "
                         "in this home, so nothing was aged")
        with open(os.path.join(home, "settings.json"), "w") as output:
            json.dump(settings, output)

    def _census(self, out):
        return [line for line in out.splitlines()
                if "coverage (this lane only)" in line
                or line.startswith(("home guard contract:", "delivery lane",
                                    "continuity lane"))]

    def test_status_reports_drift_without_lowering_any_lane_coverage(self):  # noqa: VACUOUS_ASSERTION — `assertTrue(census)` before the comparison is unconditional and on the same observable, so an empty census fails rather than comparing two absences; and the drift line asserted after it is a PRESENCE
        """PRESENCE IS NOT CURRENCY, at the surface the owner reads.

        MEASURED, which is why the arm is a BEFORE/AFTER on one home rather
        than a fixed string: on this host a template change made all seven
        credential homes report `inject coverage 0 of 7`, `delivery lane 0 of
        7` and `continuity lane 0 of 7` while every one of those hooks was
        firing on every tool call — and the table above the counts printed
        `deliver yes` for the same seven files.
        """
        home = self.mk_home("drifted")
        action, detail = hooks.install_home(home)
        self.assertIn(action, ("add", "update"), detail)

        rc, before, err = self.run_hooks(["status"])
        self.assertEqual(rc, 0, err)
        census = self._census(before)
        self.assertTrue(census, "MUST-HIT: no census lines were printed at "
                                "all, so the comparison below is two empties")
        self.assertNotIn("OUT-OF-DATE", before,
                         "MUST-HIT: the freshly installed home already reads "
                         "as drifted, so the drift below is not this arm's")

        self._drift(home, ("stop-guard", "inject"))
        rc, after, err = self.run_hooks(["status"])
        self.assertEqual(rc, 0, err)
        # EVERY COUNT IS THE SAME NUMBER IT WAS. That is the whole claim, and
        # it holds for inject, the guard contract and both lanes at once.
        self.assertEqual(self._census(after), census,
                         "an older rendering of a working ladder lowered a "
                         "coverage count")
        # AND THE FACT IS LOUDER, not gone: ONE row naming the count and the
        # homes. A template change drifts every home at once, so a per-home
        # line is the same sentence N times and buries the single action all N
        # of them ask for.
        self.assertIn("1 of 2 homes run an OUT-OF-DATE rendering of 2 spec(s) "
                      "(inject, stop-guard)", after)
        self.assertIn("drifted homes: drifted", after)
        self.assertNotIn("home drifted carries a STALE guard entry", after)
        self.assertNotIn("home drifted is MISSING", after,
                         "one installed drifted guard was reported twice as "
                         "both absent and out of date")

    def test_unreadable_settings_report_currency_unknown(self):
        home = self.mk_home("unreadable")
        with open(os.path.join(home, "settings.json"), "w") as output:
            output.write("{")
        broken = self.mk_home("broken-link")
        os.symlink(os.path.join(self.tmp, "missing-target"),
                   os.path.join(broken, "settings.json"))
        rc, out, err = self.run_hooks(["status"])
        self.assertEqual(rc, 0, err)
        self.assertIn(
            "home unreadable: currency UNKNOWN — its settings could not be read",
            out)
        self.assertIn(
            "home broken-link: currency UNKNOWN — its settings could not be read",
            out)
        self.assertIn(
            "home unreadable is MISSING", out,
            "an unreadable config lost the established missing-contract alarm")
        self.assertIn(
            "home broken-link is MISSING", out,
            "a broken-link config lost the established missing-contract alarm")

    def test_no_drift_lowers_coverage_and_an_absence_still_does(self):
        """PRESENCE IS NOT CURRENCY, at the one door every count reads.

        `missing_names` subtracts the drifted specs from the absent ones — a
        subtraction the MISSING *row* already performed while every COUNT built
        on the raw list did not, so one table printed `deliver yes` beside
        `delivery lane 0 of 7` about the same seven files.
        """
        base = {"hook": True, "resolvable": True, "fail_open": True}
        self.assertEqual(
            (hooks.inject_covered([dict(base, drifted=[])]),
             hooks.inject_covered([dict(base, drifted=["stop-guard"])]),
             hooks.inject_covered([dict(base, drifted=["inject"])])),
            (1, 1, 1),
            "a vintage difference was counted as an absent hook")
        # A GENUINE ABSENCE IS STILL AN ABSENCE — the control that shows this
        # loosened presence and not the whole predicate.
        drifted = {"missing": ["join"], "stale": [], "drifted": ["join"]}
        absent = {"missing": ["join"], "stale": [], "drifted": []}
        unrunnable = {"missing": [], "stale": ["join"], "drifted": []}
        self.assertEqual(
            (hooks.missing_names(drifted), hooks.missing_names(absent)),
            ([], ["join"]))
        self.assertEqual(hooks.lane_live(drifted, "join"), True)
        self.assertEqual(hooks.lane_live(absent, "join"), False)
        self.assertEqual(
            (hooks.covered_count([drifted]), hooks.covered_count([absent]),
             hooks.covered_count([unrunnable])),
            (1, 0, 0),
            "STALE and DRIFTED are opposite failures and only one is absence")

    def test_canonical_inject_with_stale_room_stays_covered(self):
        spec = next(s for s in hooks.SPECS if s["name"] == "inject")
        with mock.patch.object(hooks, "helm_bin", return_value="/b/helm"):
            settings = self._home(spec)
            other = next(s for s in hooks.SPECS
                         if s["name"] == "stop-guard")
            room = hooks._canonical_entry(other)
            command = room["hooks"][0]["command"]
            room["hooks"][0]["command"] = command.replace(
                "/b/helm", "/synthetic/proj-wt/lane-a/bin/helm", 1)
            settings["hooks"][other["event"]] = [room]
            _merged, actions = hooks._merge_all(settings, (spec,))
            self.assertEqual(
                actions["rooms"], "update",
                "MUST-HIT: the fixture did not trigger lane-room repair")
            self.assertEqual(
                hooks.stale_specs(settings, (spec,)), [],
                "lane-room repair was mislabeled as hook-rendering drift")

    def test_canonical_inject_with_stale_default_stays_covered(self):
        """An unrelated estate default is not drift in the inject hook.

        `_merge_all` owns both domains. Treating every update it reports as a
        stale hook makes a canonical inject entry read OUT-OF-DATE and lowers
        the explicitly inject-only coverage count because a scalar default is
        stale. Currency must be scoped to resolved hook specs, and inject
        coverage to the inject spec inside that result.
        """
        spec = next(s for s in hooks.SPECS if s["name"] == "inject")
        with mock.patch.object(hooks, "helm_bin", return_value="/b/helm"):
            settings = self._home(spec)
            settings["workflowSizeGuideline"] = "large"
            drifted = hooks.stale_specs(settings, (spec,))
        self.assertEqual(
            drifted, [],
            "a stale scalar default was mislabeled as hook-rendering drift")
        row = {"hook": True, "resolvable": True, "fail_open": True,
               "drifted": drifted}
        self.assertEqual(
            hooks.inject_covered([row]), 1,
            "canonical inject was excluded from inject-only coverage by an "
            "unrelated stale default")


class StopGuardBudgetTest(HooksBase):
    """THE BUDGET MUST OUTLIVE THE WORK, or the guard is decorative.

    MEASURED 2026-09-09 on the owner's box, one real stop with four held
    leases and a 3,186-row dispatch ledger: 7.70s before the `_binding_rows`
    memo, 6.27s after. Three back-to-back runs of the exact hook command
    returned rc=2 — the BLOCK code — at 8.7s, 8.1s and 5.9s against a 5s
    budget. So the guard had a live refusal to make every single time and its
    own clock killed it first; the stop was announced ALLOWED and UNCHECKED.

    THIS ARM IS NOT A TRANSCRIPTION OF THE CONSTANT. It pins the budget to the
    MEASUREMENT that justifies it, so shrinking the budget back toward the
    measured cost fails here with the number that says why.
    """

    # AN OBSERVED SAMPLE MAXIMUM, NOT A BOUND, and the distinction is the
    # whole reason this constant is named the way it is. Three runs is a
    # sample; the true worst case is unknown and larger. A review of
    # this: "must not turn finite sampled maximum into guaranteed bound".
    #
    # WORKLOAD AND ENVIRONMENT THE SAMPLE CAME FROM, because a timing with no
    # environment attached is not evidence of anything: the owner's laptop,
    # 2026-09-09, one seat holding four worktree leases, a 3,186-row dispatch
    # ledger and a ~120-row bindings ledger, three back-to-back runs of the
    # exact hook command (8.7s, 8.1s, 5.9s) plus one cProfile run at 7.70s.
    #
    # SERVICE OBJECTIVE: the budget must leave room for the cost to grow with
    # the ledgers between one re-derivation and the next, so the margin is
    # demanded as a MULTIPLE of the observed sample rather than a fixed slack.
    # EXHAUSTION BEHAVIOUR when the objective is finally breached is unchanged
    # and deliberate: the guard fails OPEN and says so (pinned below).
    #
    # THIS ARM DOES NOT MEASURE ANYTHING. It compares a RECORDED observation
    # to a CONFIGURED budget — a policy relationship, deterministic and
    # environment-free. The timing lives here as documentation of where the
    # number came from, never as an oracle a test run re-derives.
    OBSERVED_SAMPLE_MAX_S = 8.7
    REQUIRED_MARGIN = 2

    def spec(self):
        return next(s for s in hooks.SPECS if s["name"] == "stop-guard")

    def test_the_budget_exceeds_the_measured_cost_with_headroom(self):
        spec = self.spec()
        budget = spec["timeout"]
        # THE POSITIVE CONTROL, on the observable the thresholds below judge:
        # the number must actually reach the shell. A budget compared only
        # against constants is satisfied by a spec nothing renders.
        with mock.patch.object(hooks, "helm_bin", return_value="/b/helm"):
            # `_wrapper_prefix` fixes FIVE operands before the child, so the
            # budget is words[4] by contract rather than by position luck.
            self.assertEqual(shlex.split(hooks.spec_command(spec))[4],
                             str(budget),
                             "MUST-HIT: the budget never reaches the rendered "
                             "command, so these thresholds judge a dead number")
        self.assertGreater(
            budget, self.OBSERVED_SAMPLE_MAX_S,
            "the stop-guard budget is at or below a run we have actually "
            "OBSERVED, so a guard with a live refusal to make gets killed "
            "before it can make it — a safety rung failing OPEN")
        # The dominant remaining cost is the dispatch fold and it is O(rows)
        # on a ledger that grows daily, so a budget sized to the sample is the
        # same mistake as the 5 this replaces. The multiple is the service
        # objective: room for the cost to grow before anyone must re-derive.
        self.assertGreaterEqual(
            budget, self.REQUIRED_MARGIN * self.OBSERVED_SAMPLE_MAX_S,
            "the budget leaves less than the required margin over the "
            "observed sample, and the cost grows with the ledger")

    def test_the_harness_deadline_outlives_the_shell_budget(self):
        """A grace that does not exceed the inner budget makes the rc-124 arm
        dead code: Claude kills the wrapper before `timeout` can speak."""
        spec = self.spec()
        outer = hooks._gate_outer_timeout(spec)
        # THE POSITIVE CONTROL: the grace is only real if the installed entry
        # carries it, and `.get` returning None on BOTH sides would satisfy an
        # equality check while proving the opposite. Assert a live number
        # first, then that it is the one written.
        with mock.patch.object(hooks, "helm_bin", return_value="/b/helm"):
            entry = hooks._canonical_entry(spec)
        written = entry["hooks"][0].get("timeout")
        self.assertIsInstance(written, int)
        self.assertGreater(written, 0,
                           "MUST-HIT: the hook entry carries no harness "
                           "deadline, so the comparison below is inert")
        self.assertEqual(written, outer,
                         "the entry's deadline is not the derived one")
        self.assertGreater(outer, spec["timeout"],
                           "the harness can kill the wrapper before its own "
                           "timeout fires, so rc 124 can never be reported")

    def test_the_harness_grace_is_named_not_inlined(self):
        """The grace must exceed the budget or the rc-124 arm is dead code:
        Claude kills the wrapper before `timeout` can speak. `GRACE_S` exists
        so that relationship is assertable rather than an inline 5."""
        spec = self.spec()
        self.assertGreater(hooks.GRACE_S, 0)
        self.assertEqual(hooks._gate_outer_timeout(spec),
                         spec["timeout"] + hooks.GRACE_S)
        self.assertGreater(hooks._gate_outer_timeout(spec), spec["timeout"],
                           "the harness can kill the wrapper before its own "
                           "timeout fires, so rc 124 can never be reported")

    def test_only_124_may_claim_a_timeout(self):
        """A CODE ANYONE CAN PRODUCE CANNOT IDENTIFY AN EVENT THIS WRAPPER OWNS.

        A `124|137` arm was here while the wrapper escalated to SIGKILL, and
        A probe measured why it is wrong: 137 is 128+SIGKILL and belongs to
        every kill there is — the OOM killer, an operator's `kill -9`, a
        self-inflicted one, measured returning 137 BEFORE the deadline. So the
        arm reported "THE GUARD TIMED OUT" for kills that had nothing to do
        with the deadline: a false FAILURE report traded for a false TIMEOUT
        report.

        This arm exists to keep 137 OUT of the timeout arm until an
        escalation this wrapper can prove it owns is built, which is a
        different lane. It fails if anyone re-adds the alternation.
        """
        self.assertIn("124)", LADDER_CODE,
                      "MUST-HIT: no timeout arm at all, so this asserts "
                      "nothing about which codes may claim one")
        self.assertNotIn("137", LADDER_CODE,
                         "137 is SIGKILL, not a timeout — an OOM kill would "
                         "be reported as one")
        timed_out = LADDER_CODE.split("124)", 1)[1].split(";;", 1)[0]
        self.assertIn("alarm", timed_out)
        self.assertIn("TIMED OUT", LADDER_CODE)

    def test_the_timeout_still_fails_open(self):
        """THE LAW THIS CHANGE DOES NOT TOUCH, pinned here so the next reader
        does not mistake a bigger budget for a changed failure direction.
        Settled after a measured fleet-wide outage: a guard that can wedge
        every seat's turn end is worse than one that misses a row. rc 124
        still exits 0 and still says the stop went unchecked."""
        self.assertIn("124)", LADDER_CODE,
                      "MUST-HIT: no timeout arm in the shipped ladder, so "
                      "this asserts nothing")
        after = LADDER_CODE.split("124)", 1)[1].split(";;", 1)[0]
        self.assertNotIn("exit 2", after,
                         "a timeout now BLOCKS — that inverts the fail-open "
                         "law, which is a separate decision from the budget")
        self.assertIn("exit 2", LADDER_CODE,
                      "the deliberate refusal no longer reaches the harness")
        self.assertEqual(
            shlex.split(hooks.spec_command(self.spec()))[1], "gate",
            "the stop guard stopped being rendered as a gate, so the arm "
            "that propagates rc 2 is never reached")

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
            binpath = os.path.join(self.tmp, "stub", "helm")
            os.makedirs(os.path.dirname(binpath), exist_ok=True)
            with open(binpath, "w") as f:
                f.write("#!/bin/sh\n%s\n" % body)
            os.chmod(binpath, 0o755)
        # THE SHIPPED SCRIPT, BESIDE THE STUB. These arms execute
        # bin/helm-hook for real; without it the command's first word does not
        # exist and every arm measures the shell's 127 instead of the ladder.
        self.wrapper_beside(binpath)
        # A PRIVATE SUPPRESSION WINDOW PER RUN. The rendered 124 arm speaks
        # once per class per window and counts the rest (hooks.hookalarm), so
        # two arms sharing a directory would make the second one read as a
        # guard that said nothing — a leaked channel wearing the costume of a
        # missing diagnostic. Every call gets its own directory, which is what
        # keeps each arm a measurement of the LADDER rather than of the order
        # the arms happened to run in.
        window = tempfile.mkdtemp(prefix="alarmwin-", dir=self.tmp)
        return subprocess.run(["sh", "-c", self.render(binpath)],
                              capture_output=True, text=True,
                              env=dict(os.environ,
                                       HELM_HOOK_ALARM_DIR=window))

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
        """A finding, and it invalidated every arm above as a proof of
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
        self.assertIn("TIMED OUT at 2s", doc["systemMessage"],
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

    def test_a_control_character_in_the_path_still_leaves_valid_JSON(self):
        """THE PROPERTY `json.dumps` MADE AND A TWO-RULE ESCAPER DID NOT.

        The inline ladder built its envelope with `json.dumps`: valid JSON
        however the text was spelled. The wrapper's escaper covered backslash
        and quote — every character a path was EXPECTED to contain. A tab or a
        newline is legal in a path on every filesystem helm runs on, and a raw
        control character inside a JSON string is a parse error, so the one
        channel the harness reads on exit 0 would carry nothing at all on the
        hook that is already reporting a broken estate.

        EVERY C0 CHARACTER, not the three that are easy: the escaper walks
        bytes and the assertion is a round trip, so a rule that covers `\\t`
        and misses `\\v` is red here rather than on the day a path has one.
        """
        # UNCONDITIONAL POSITIVE CONTROL, ON EVERY OBSERVABLE THE LOOP READS,
        # because a control that covers one of three assertions leaves the
        # other two satisfiable by a channel that is empty for its own
        # reasons. Same command shape, ordinary path: it reaches stdout, it
        # parses, it carries the path back, and its two copies of the one
        # sentence agree — so every failure below is about the CONTROL
        # CHARACTER and nothing else.
        clean = os.path.join(self.tmp, "room", "bin", "helm")
        p = self.run_proc(binpath=clean)
        self.assertTrue(p.stdout.strip())
        self.assertEqual(p.returncode, 0)
        doc = json.loads(p.stdout)
        self.assertIn(clean, doc["systemMessage"])
        self.assertEqual(doc["hookSpecificOutput"]["additionalContext"],
                         doc["systemMessage"])
        for label, bad in (("tab", "\t"), ("newline", "\n"),
                           ("carriage-return", "\r"), ("vertical-tab", "\v"),
                           ("bell", "\a"), ("form-feed", "\f"),
                           ("escape", "\x1b"), ("delete", "\x7f")):
            with self.subTest(control=label):
                nasty = os.path.join(self.tmp, "ro%som" % bad, "bin", "helm")
                p = self.run_proc(binpath=nasty)
                self.assertEqual(p.returncode, 0, "fail-open law unchanged")  # noqa: VACUOUS_ASSERTION — rc 0 IS the fail-open law, so this claim is absence-shaped by nature; its unconditional twin is the control above, which drives the SAME run_proc on an ordinary path and asserts rc 0 with non-empty stdout. A control must call run_proc a second time, so it can never share this loop's call site.
                self.assertTrue(p.stdout.strip(),
                                "the alarm never reached stdout at all")
                doc = json.loads(p.stdout)      # the assertion IS the parse
                self.assertIn(nasty, doc["systemMessage"],
                              "the path did not survive the escaper intact")
                self.assertEqual(doc["hookSpecificOutput"]["additionalContext"],  # noqa: VACUOUS_ASSERTION — two absences would also be equal, which is exactly why the control above asserts this same equality on a document already proven non-empty and to carry its own path.
                                 doc["systemMessage"],
                                 "the two copies of one sentence drifted")

    def test_a_killed_guard_still_says_it_timed_out(self):
        rc, err = self.run_gate("sleep 30")
        self.assertEqual(rc, 0)
        self.assertIn("[helm stop-guard] TIMED OUT at 2s", err)
        self.assertIn("this stop is UNCHECKED", err,
                      "the trimmed sentence dropped the one fact that must "
                      "survive it")

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
        words = shlex.split(cmd)
        self.assertIn("inject --hook-json", cmd)
        # NEVER HOLD A TURN: the budget is the wrapper's fourth operand and
        # the wrapper is the first word, so nothing runs before it.
        self.assertEqual(words[0], hooks.wrapper_bin(), cmd)
        self.assertEqual(words[4], str(hooks.TIMEOUT_S), cmd)
        # never block a turn — the CONTRACT, not the idiom it used to be
        # spelled with. The `lane` kind is what decides it inside the wrapper;
        # only `gate` has an arm that propagates rc 2.
        self.assertEqual(words[1], "lane", cmd)
        self.assertNotIn("2) exit 2", cmd)                 # and never blocks
        hb = hooks.helm_bin()
        self.assertIn(hb, cmd)
        self.assertTrue(os.path.isfile(hb) and os.access(hb, os.X_OK),
                        "generated command must point at this checkout's bin/helm")
        self.assertEqual(hooks._helm_of(cmd), hb)
        self.assertTrue(hooks._resolvable(cmd))
        self.assertTrue(hooks._fail_open(cmd))


class InstallTest(HooksBase):
    def test_install_covers_every_home_and_is_idempotent(self):
        a = self.mk_home("a-user-example")
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
        d = self.mk_home("a-user-example", settings={
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
        d = self.mk_home("a-user-example", settings={
            "hooks": {"UserPromptSubmit": [{"hooks": [{
                "type": "command",
                "command": "jq -r .prompt | /old/path/helm inject --project x"}]}]}})
        action, _ = hooks.install_home(d)
        self.assertEqual(action, "update")
        self.assertEqual(hooks._hook_cmds(self.read_settings(d)),
                         [hooks.hook_command()])

    def test_dry_prints_diff_writes_nothing(self):
        d = self.mk_home("a-user-example")
        rc, out, _ = self.run_hooks(["install", "--dry"])
        self.assertEqual(rc, 0)
        self.assertIn("dry — nothing written", out)
        self.assertIn("inject --hook-json", out)   # the would-be entry, as a diff
        self.assertIn("+", out)
        self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))
        self.assertFalse(os.path.exists(
            os.path.join(homes.DEFAULTS["claude"], "settings.json")))

    def test_unparseable_settings_refused_untouched(self):
        d = self.mk_home("a-user-example")
        with open(os.path.join(d, "settings.json"), "w") as f:
            f.write("not json{")
        action, detail = hooks.install_home(d)
        self.assertEqual(action, "fail")
        self.assertIn("refusing to touch", detail)
        with open(os.path.join(d, "settings.json")) as f:
            self.assertEqual(f.read(), "not json{")

    def test_write_failure_leaves_original_and_exits_1(self):
        self.mk_home("a-user-example", settings={"model": "opus"})
        with mock.patch.object(configs, "write_file",
                               return_value={"error": "disk full"}):
            rc, out, _ = self.run_hooks(["install"])
        self.assertEqual(rc, 1)
        self.assertIn("fail", out)
        self.assertEqual(
            self.read_settings(os.path.join(homes.ROOTS["claude"], "a-user-example")),
            {"model": "opus"})

    def test_post_commit_foreign_write_is_preserved_and_retried(self):
        d = self.mk_home("a-user-example", settings={"model": "opus"})
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
        a = self.mk_home("a-user-example")
        rc, _, _ = self.run_hooks(["install", "--home", "a-user-example"])
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
        a = self.mk_home("a-user-example")
        os.rmdir(homes.DEFAULTS["claude"])
        os.symlink(a, homes.DEFAULTS["claude"])
        self.assertEqual(hooks.claude_homes(), [("a-user-example", os.path.realpath(a))])


class StatusTest(HooksBase):
    def test_status_table_and_coverage(self):
        a = self.mk_home("a-user-example")
        hooks.install_home(a)
        self.mk_home("hand-wired", settings={
            "hooks": {"UserPromptSubmit": [{"hooks": [{
                "type": "command",
                "command": "/nonexistent/helm inject --hook-json"}]}]}})
        self.mk_home("bare")
        rows = {r["home"]: r for r in hooks.status_rows()}
        self.assertEqual(len(rows), 4)  # 3 named + default
        good = rows["a-user-example"]
        self.assertTrue(good["hook"] and good["resolvable"] and good["fail_open"])
        hand = rows["hand-wired"]
        self.assertTrue(hand["hook"])
        self.assertFalse(hand["resolvable"])   # helm path does not exist
        self.assertFalse(hand["fail_open"])    # no `|| true` guard
        self.assertFalse(rows["bare"]["hook"])
        self.assertEqual(hooks.coverage(), (1, 4))
        rc, out, _ = self.run_hooks(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("inject coverage (this lane only): 1 of 4 claude homes", out)
        # the NARROW lane count is no longer the only home claim: the
        # guard contract sits beside it and names what is missing
        self.assertIn("home guard contract: 1 of 4 homes", out)
        self.assertIn("home bare is MISSING", out)
        self.assertIn("suite-guard", out)
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
        d = self.mk_home("a-user-example")
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

    def test_stop_guard_generator_keeps_its_checked_verdict_budget(self):
        """DERIVED, NOT TRANSCRIBED. This arm used to spell 5 and 10 as
        literals and was named for them, so it pinned a number rather than the
        RELATIONSHIP it exists to protect: whatever the spec's budget is, the
        rendered command must carry it and the installed entry must carry the
        derived harness grace. Transcribing the constant made the arm fail on
        a deliberate re-derivation of the budget while proving nothing about
        the generator."""
        spec = next(s for s in hooks.SPECS if s["name"] == "stop-guard")
        budget = spec["timeout"]
        self.assertGreater(budget, 0)                 # control: a real budget
        command = hooks.spec_command(spec)
        self.assertTrue(command.startswith(
            hooks._wrapper_prefix(spec, "gate", "stop") + " "), command)
        d = self.mk_home("checked-stop")
        action, detail = hooks.install_home(d)
        self.assertNotEqual(action, "fail", detail)
        got = self.read_settings(d)
        self.assertIn(command, hooks._hook_cmds(got, "Stop"))
        installed = got["hooks"]["Stop"][0]["hooks"][0]
        self.assertEqual(installed["timeout"],
                         hooks._gate_outer_timeout(spec))
        installed.pop("timeout")
        self.assertFalse(hooks._lane_live(got, spec))
        with open(os.path.join(d, "settings.json"), "w") as f:
            json.dump(got, f)
        rc, why = hooks.preflight(d, specs=(spec,), apply=False)
        self.assertEqual(rc, 1)
        self.assertIn("stop-guard", why)
        row = next(r for r in hooks.home_gap_rows() if r["path"] == d)
        self.assertIn("stop-guard", row["missing"])
        self.assertEqual(hooks.install_home(d)[0], "update")
        repaired_settings = self.read_settings(d)
        repaired = repaired_settings["hooks"]["Stop"][0]["hooks"][0]
        self.assertEqual(repaired["timeout"],
                         hooks._gate_outer_timeout(spec))
        self.assertTrue(hooks._lane_live(repaired_settings, spec))

    def test_gate_timeout_message_names_the_events_own_stake(self):
        """ONE template renders every gate spec, and the hardcoded 'this
        stop' told the argv-guard's PreToolUse reader a STOP had been allowed
        when what actually went unchecked was a tool call. The noun comes
        from the spec's event; an event outside the table renders as
        '<Event> event' — vague but never wrong."""
        names = {s["name"]: s for s in hooks.SPECS}
        stop_cmd = hooks.spec_command(names["stop-guard"])
        argv_cmd = hooks.spec_command(names["argv-guard"])
        # THE NOUN IS AN OPERAND NOW, and the sentence that consumes it lives
        # in bin/helm-hook. Both halves are asserted: the operand here, and the
        # rendered sentence by executing the script (GateFailureVisibilityTest
        # and tests/test_hook_wrapper.py, which drives BOTH nouns).
        self.assertEqual(shlex.split(stop_cmd)[5], "stop")
        self.assertEqual(shlex.split(argv_cmd)[5], "tool call")
        self.assertNotIn("this stop", argv_cmd)
        self.assertIn("this $what is ALLOWED and UNCHECKED", LADDER_CODE,
                      "MUST-HIT: the sentence that consumes the noun is gone")
        fake = {"name": "x-gate", "event": "SessionStart", "args": "chat x",
                "timeout": 2, "gate": True}
        self.assertEqual(shlex.split(hooks.spec_command(fake))[5],
                         "SessionStart event")

    def test_continuity_specs_wire_handoff_check(self):
        """PreCompact + SessionEnd both carry `handoff check --hook-json` — the
        one command whose --hook-json path captures the now-snapshot AND nags."""
        names = {s["name"]: s for s in hooks.SPECS}
        # UNCONDITIONAL MIRROR, outside the loop: the gate ladder DOES carry
        # the blocking arm, so "not in" below is a fact about advisory specs
        # rather than a string that never appears in any command.
        self.assertEqual(
            shlex.split(hooks.spec_command(names["stop-guard"]))[1], "gate")
        for name, event in (("handoff-precompact", "PreCompact"),
                            ("handoff-sessionend", "SessionEnd")):
            spec = names[name]
            self.assertEqual(spec["event"], event)
            self.assertEqual(spec["args"], "handoff check --hook-json")
            self.assertIsNone(spec["matcher"])
            cmd = hooks.spec_command(spec)
            self.assertIn("handoff check --hook-json", cmd)
            self.assertEqual(shlex.split(cmd)[1], "lane")     # never blocks
            self.assertTrue(hooks._fail_open(cmd))            # ...never gates

    def test_status_reports_continuity_lane(self):
        d = self.mk_home("a-user-example")
        hooks.install_home(d)
        rows = {r["home"]: r for r in hooks.status_rows()}
        self.assertTrue(rows["a-user-example"]["handoff-precompact"])
        self.assertTrue(rows["a-user-example"]["handoff-sessionend"])
        self.assertFalse(rows["(default-claude)"]["handoff-precompact"])
        rc, out, _ = self.run_hooks(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("handoff", out)               # the column header
        self.assertIn("continuity lane", out)       # the gap summary line

    def test_exact_installed_recorder_combines_when_delivery_is_requested(self):
        from helm import posttool, record
        rec = "timeout 10 /x/bin/helm record --hook-json || true"
        d = self.mk_home("a-user-example", settings={
            "hooks": {"PostToolUse": [{"matcher": "*", "hooks": [
                {"type": "command", "command": rec}]}]}})
        action, detail = hooks.install_home(d)
        self.assertEqual(action, "update", detail)
        got = self.read_settings(d)
        self.assertEqual(len(hooks._hook_cmds(got, "PostToolUse")), 1)
        self.assertEqual(posttool.covered_members(got, executable=hooks.helm_bin()),
                         {"record", "deliver"})
        self.assertTrue(record._leg_live(got, record.HOOK_EVENT))

    def test_status_reports_delivery_lanes(self):
        d = self.mk_home("a-user-example")
        hooks.install_home(d)
        rows = {r["home"]: r for r in hooks.status_rows()}
        self.assertTrue(rows["a-user-example"]["deliver"])
        self.assertTrue(rows["a-user-example"]["join"])
        self.assertTrue(rows["a-user-example"]["stop-guard"])   # the idle gate
        self.assertFalse(rows["(default-claude)"]["deliver"])

    def test_wrong_matcher_on_exclusive_group_repaired_in_place(self):
        """Codex B3's exact reproduction: the exact deliver command under a
        Bash-pinned group misses most tool boundaries — status must call it
        NOT live, and install must repair the matcher."""
        deliver = next(s for s in hooks.SPECS if s["name"] == "deliver")
        d = self.mk_home("a-user-example", settings={
            "hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": hooks.spec_command(deliver)}]}]}})
        rows = {r["home"]: r for r in hooks.status_rows()}
        self.assertFalse(rows["a-user-example"]["deliver"])   # stale ≠ coverage
        action, _ = hooks.install_home(d)
        self.assertEqual(action, "update")
        got = self.read_settings(d)
        groups = got["hooks"]["PostToolUse"]
        self.assertEqual(len(groups), 1)                  # repaired, not doubled
        self.assertEqual(groups[0]["matcher"], "*")
        rows = {r["home"]: r for r in hooks.status_rows()}
        self.assertTrue(rows["a-user-example"]["deliver"])
        self.assertEqual(hooks.install_home(d), ("ok", "hook up to date"))

    def test_wrong_matcher_with_foreign_cotenant_relocates_ours_only(self):
        deliver = next(s for s in hooks.SPECS if s["name"] == "deliver")
        rec = "timeout 10 /x/bin/helm record --hook-json || true"
        d = self.mk_home("a-user-example", settings={
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
        self.assertTrue(rows["a-user-example"]["deliver"])


    def test_tool_specific_pair_repair_stays_standalone(self):
        deliver = next(s for s in hooks.SPECS if s["name"] == "deliver")
        rec = "timeout 10 /x/bin/helm record --hook-json || true"
        d = self.mk_home("a-user-example", settings={
            "hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": rec},
                {"type": "command", "command": hooks.spec_command(deliver)}]}]}})
        before = self.read_settings(d)
        action, detail = hooks.install_home(d)
        self.assertEqual(action, "update", detail)
        from helm import posttool
        got = self.read_settings(d)
        self.assertEqual(posttool.covered_members(got, executable=hooks.helm_bin()), set())
        self.assertEqual(got["hooks"]["PostToolUse"][0]["hooks"],
                         before["hooks"]["PostToolUse"][0]["hooks"][:1])
        self.assertEqual(got["hooks"]["PostToolUse"][0]["matcher"], "Bash")
        self.assertEqual(len(hooks._hook_cmds(got, "PostToolUse")), 2)
        self.assertEqual(hooks.install_home(d)[0], "ok")
        self.assertEqual(self.read_settings(d), got)

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
        d = self.mk_home("a-user-example", settings={
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
        self.assertTrue(rows["a-user-example"]["deliver"])

    def test_wrong_type_same_command_not_covered_until_repaired(self):
        """Final gate delta: the exact command+matcher under a FOREIGN type
        (type:'http') must not read as coverage — the harness would not run
        it as a command hook. Install repairs the type; only then live."""
        deliver = next(s for s in hooks.SPECS if s["name"] == "deliver")
        d = self.mk_home("a-user-example", settings={
            "hooks": {"PostToolUse": [{"matcher": "*", "hooks": [
                {"type": "http", "command": hooks.spec_command(deliver)}]}]}})
        rows = {r["home"]: r for r in hooks.status_rows()}
        self.assertFalse(rows["a-user-example"]["deliver"])
        action, _ = hooks.install_home(d)
        self.assertEqual(action, "update")
        got = self.read_settings(d)["hooks"]["PostToolUse"]
        self.assertEqual(len(got), 1)                    # repaired in place
        self.assertEqual(got[0]["hooks"][0]["type"], "command")
        rows = {r["home"]: r for r in hooks.status_rows()}
        self.assertTrue(rows["a-user-example"]["deliver"])

    def test_missing_matcher_on_owned_delivery_group_repaired(self):
        join = next(s for s in hooks.SPECS if s["name"] == "join")
        d = self.mk_home("a-user-example", settings={
            "hooks": {"SessionStart": [{"hooks": [       # no matcher key at all
                {"type": "command", "command": hooks.spec_command(join)}]}]}})
        rows = {r["home"]: r for r in hooks.status_rows()}
        self.assertFalse(rows["a-user-example"]["join"])
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
        d = self.mk_home("a-user-example", settings={
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
        action, _ = hooks.install_home(d, specs=hooks.SEAT_SPECS)
        self.assertEqual(action, "add")
        got = self.read_settings(d)
        self.assertEqual(got["model"], "kimi-k3")
        self.assertEqual(got["permissions"]["allow"], list(hooks.PERMIT_RULES))
        self.assertEqual(hooks.install_home(d, specs=hooks.SEAT_SPECS),
                         ("ok", "hook up to date"))

    def test_permits_alone_missing_still_triggers_an_install_write(self):
        """A home whose hooks are current but whose allow rules are absent is
        NOT up to date — the exact live gap (hooks installed before this fix)."""
        d = self.mk_home("a-user-example")
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
        d = self.mk_home("a-user-example", settings={"permissions": "nope"})
        action, detail = hooks.install_home(d)
        self.assertEqual(action, "fail")
        self.assertIn("permissions", detail)
        self.assertEqual(self.read_settings(d), {"permissions": "nope"})

    def test_status_surfaces_the_permit_gap(self):
        d = self.mk_home("a-user-example")
        hooks.install_home(d)
        rows = {r["home"]: r for r in hooks.status_rows()}
        self.assertTrue(rows["a-user-example"]["permits"])
        self.assertFalse(rows["(default-claude)"]["permits"])
        rc, out, _ = self.run_hooks(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("beacon permit", out)

    def test_status_gives_an_UNPROVEN_LAUNCH_its_own_row(self):
        """Q2 OF THE GUARD-SET MELD, ANSWERED BY WIRING RATHER THAN ASKING.

        `unproven_launches` existed with only its test as a reader — the
        built-not-wired shape this repo files rows about. The obvious home was
        WRONG and that is the interesting part: a doctor RUNG cannot hold it,
        because that rung's contract forbids OK rows, so a permanent WARN about
        a guard that demonstrably runs is a false alarm invented to cure a
        silent claim, and an alarm that fires on the healthy majority is one
        nobody reads.

        STATUS IS A REPORT, NOT A RUNG. It already prints MISSING and STALE per
        home without either being a failure; UNPROVEN is the third word in that
        vocabulary and the one the old code said "ok" about.

        THE ARM CARRIES BOTH DISPOSITIONS THROUGH THE SAME CALL, because on a
        healthy host the real answer is empty — so an arm that only ran the
        real one would assert nothing at all."""
        d = self.mk_home("a-user-example")
        hooks.install_home(d)

        # MUST-MISS FIRST, unconditional: with nothing unproven the line is
        # ABSENT. This is also the honest state of this host, so it is the
        # control that keeps the must-hit below about the injection.
        with mock.patch.object(hooks, "unproven_launches", return_value=[]):
            rc, quiet, _e = self.run_hooks(["status"])
        self.assertEqual(rc, 0)
        self.assertNotIn("UNPROVEN", quiet)

        spec = {"name": "suite-guard", "external": "fab-suite-pretooluse"}
        with mock.patch.object(
                hooks, "unproven_launches",
                return_value=[(spec, hooks.LAUNCH_UNJUDGED,
                               "its #! runs python3 through env")]):
            rc2, loud, _e2 = self.run_hooks(["status"])
        self.assertEqual(rc2, 0, "an UNPROVEN row is a REPORT, never a failure "
                                 "— status must still exit 0")
        self.assertIn("suite-guard", loud)
        self.assertIn("UNPROVEN", loud)
        self.assertIn("its #! runs python3 through env", loud,
                      "the row must carry WHY, or it is an alarm with no "
                      "action attached")
        self.assertIn("unproven is not broken", loud,
                      "and it must say what UNPROVEN does NOT mean, because "
                      "the whole risk of this row is being read as a fault")



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
                    d, specs=hooks.SEAT_SPECS)
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
        hooks.install_home(d, specs=hooks.SEAT_SPECS)
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
        hooks.install_home(d, specs=hooks.SEAT_SPECS)
        self.assertEqual(hooks.install_home(d, specs=hooks.SEAT_SPECS),
                         ("ok", "hook up to date"))
        got = self.read_settings(d)
        got.pop("workflowSizeGuideline")            # someone pruned ours
        with open(os.path.join(d, "settings.json"), "w") as f:
            json.dump(got, f)
        action, _ = hooks.install_home(d, specs=hooks.SEAT_SPECS)
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


class MemoryBaseTest(HooksBase):
    """A credential home whose `projects` is a SYMLINK to the shared tree gets
    MEMORY_BASE_ENV rendered into settings env, naming the real parent of that
    tree. Without it Claude Code sees the resolved memory path under a
    `.claude` segment, refuses it as a memory write, and the seat stops on a
    permission prompt. A home whose `projects` is a real dir gets nothing."""

    def mk_linked(self, name, settings=None, target_name="projects"):
        """(home, base): a home whose `projects` links into a shared tree
        shaped like the default one, <base>/.claude/<target_name>."""
        d = self.mk_home(name, settings=settings)
        shared = os.path.join(self.tmp, "shared", ".claude", target_name)
        os.makedirs(shared, exist_ok=True)
        os.symlink(shared, os.path.join(d, "projects"))
        return d, os.path.dirname(os.path.realpath(shared))

    def env_of(self, d):
        return self.read_settings(d).get("env")

    def test_linked_home_gains_the_base_and_keeps_every_owner_key(self):
        d, base = self.mk_linked("a-user-example", settings={
            "env": {"OWNER_VAR": "1"},
            "permissions": {"allow": ["Read"],
                            "additionalDirectories": ["/srv/owner-dir"]}})
        action, _ = hooks.install_home(d)
        self.assertEqual(action, "add")
        got = self.read_settings(d)
        self.assertEqual(got["env"], {"OWNER_VAR": "1",
                                      hooks.MEMORY_BASE_ENV: base})
        self.assertEqual(got["permissions"]["additionalDirectories"],
                         ["/srv/owner-dir"])
        self.assertTrue(base.endswith(os.path.join("shared", ".claude")))
        # re-render is a no-op: nothing written, nothing duplicated
        self.assertEqual(hooks.install_home(d), ("ok", "hook up to date"))
        self.assertEqual(self.read_settings(d), got)

    def test_real_projects_dir_gets_nothing(self):
        d = self.mk_home("a-user-example", settings={"model": "opus"})
        os.makedirs(os.path.join(d, "projects"))
        self.assertEqual(hooks.install_home(d)[0], "add")
        got = self.read_settings(d)
        self.assertIn("permissions", got)       # the install did write
        self.assertNotIn("env", got)
        self.assertIsNone(hooks.memory_base(d))

    def test_link_to_a_dir_not_named_projects_gets_nothing(self):
        d, _base = self.mk_linked("a-user-example", target_name="elsewhere")
        self.assertEqual(hooks.install_home(d)[0], "add")
        self.assertIsNone(hooks.memory_base(d))
        got = self.read_settings(d)
        self.assertIn("permissions", got)       # the install did write
        self.assertNotIn("env", got)

    def test_drifted_value_is_rewritten(self):
        d, base = self.mk_linked("a-user-example", settings={
            "env": {hooks.MEMORY_BASE_ENV: "/wrong", "OWNER_VAR": "1"}})
        action, _ = hooks.install_home(d)
        self.assertEqual(action, "update")
        self.assertEqual(self.env_of(d),
                         {hooks.MEMORY_BASE_ENV: base, "OWNER_VAR": "1"})

    def test_a_home_that_no_longer_needs_it_has_it_removed(self):
        d, _base = self.mk_linked("a-user-example", settings={"env": {"OWNER_VAR": "1"}})
        hooks.install_home(d)
        os.unlink(os.path.join(d, "projects"))
        os.makedirs(os.path.join(d, "projects"))
        action, _ = hooks.install_home(d)
        self.assertEqual(action, "update")
        self.assertEqual(self.env_of(d), {"OWNER_VAR": "1"})

    def test_broken_env_shape_refused_untouched(self):
        d, _base = self.mk_linked("a-user-example", settings={"env": "nope"})
        action, detail = hooks.install_home(d)
        self.assertEqual(action, "fail")
        self.assertIn("env", detail)
        self.assertEqual(self.read_settings(d), {"env": "nope"})

    def test_doctor_and_status_name_the_home_until_install_renders_it(self):  # noqa: VACUOUS_ASSERTION — the same doctor and status observables are first asserted to NAME the key, then asserted clean
        d, base = self.mk_linked("a-user-example")
        for _name, path in hooks.claude_homes():
            if path != os.path.realpath(d):
                hooks.install_home(path)
        # every guard present, only the memory base missing
        hooks.install_home(d)
        got = self.read_settings(d)
        del got["env"]
        with open(os.path.join(d, "settings.json"), "w") as f:
            json.dump(got, f, indent=2)
        warns = [m for lvl, m in doctor.check_guard_contract()
                 if lvl == doctor.WARN]
        mine = [m for m in warns if hooks.MEMORY_BASE_ENV in m]
        self.assertEqual(len(mine), 1, warns)
        self.assertIn("a-user-example", mine[0])
        self.assertIn(base, mine[0])
        _rc, out, _ = self.run_hooks(["status"])
        self.assertIn(hooks.MEMORY_BASE_ENV, out)
        hooks.install_home(d)
        self.assertEqual(doctor.check_guard_contract(), [])
        _rc, out, _ = self.run_hooks(["status"])
        self.assertNotIn(hooks.MEMORY_BASE_ENV, out)

    def test_project_install_never_renders_it(self):
        p = self.mk_project("a-proj")
        shared = os.path.join(self.tmp, "shared", ".claude", "projects")
        os.makedirs(shared)
        os.symlink(shared, os.path.join(p, ".claude", "projects"))
        hooks.install_project(p)
        with open(hooks.project_settings_path(p), encoding="utf-8") as f:
            got = json.load(f)
        self.assertIn("permissions", got)       # the install did write
        self.assertNotIn("env", got)


class DoctorCoverageTest(HooksBase):
    def test_doctor_coverage_warn_then_ok(self):
        self.mk_home("a-user-example")
        res = doctor.check_inject_coverage()
        self.assertEqual(res[0][0], doctor.WARN)
        self.assertIn("inject coverage: 0 of 2 claude homes", res[0][1])
        self.assertIn("helm hooks install", res[0][1])
        rc, _, _ = self.run_hooks(["install"])
        self.assertEqual(rc, 0)
        self.assertEqual(doctor.check_inject_coverage(),
                         [(doctor.OK, "inject coverage: 2 of 2 claude homes")])


class SeatGuardAuditTest(HooksBase):
    """task/1006 half 2: doctor asserts, PER CONFIG — seats AND homes — that
    every required guard is present, a missing one a LOUD row, never a silent
    gap.

    The defect this replaces was not a wrong count, it was an unaskable
    question: `hooks status` said "10 of 10 seats covered (full hook contract)"
    on a morning two seats carried no suite guard, because nothing in helm
    named that guard. So the audit and the contract read ONE list here."""

    def _install_seat(self, family):
        d = self.mk_seat(family)
        self.assertEqual(hooks.install_home(d, specs=hooks.SEAT_SPECS)[0], "add")
        return d

    def _install_homes(self):
        """Every credential home wired, so a home gap in an arm below is the
        one the arm PUT there. HooksBase always has the default claude home,
        which is itself a launch surface the audit must cover."""
        for _name, path in hooks.claude_homes():
            hooks.install_home(path)

    def _clean(self):
        """The audit over a fully-wired estate — the baseline every arm starts
        from, asserted rather than assumed.

        EMPTY, not OK: this rung is findings-only. A healthy estate spends no
        line of the operator's attention, and an OK here would be a positive
        claim made over a contract `resolved_specs` may have shortened."""
        rows = doctor.check_guard_contract()
        self.assertEqual(rows, [], rows)

    def _warns(self):
        return [m for lvl, m in doctor.check_guard_contract()
                if lvl == doctor.WARN]

    def _drop(self, d, spec_name):
        """Remove exactly ONE spec's entry from a config — the single-guard
        hole a fresh mint or a hand-edit leaves behind."""
        spec = next(s for s in hooks.SPECS if s["name"] == spec_name)
        got = self.read_settings(d)
        cmd = hooks.spec_command(spec)
        for g in got["hooks"][spec["event"]]:
            g["hooks"] = [h for h in g["hooks"] if h.get("command") != cmd]
        got["hooks"][spec["event"]] = [g for g in got["hooks"][spec["event"]]
                                       if g["hooks"]]
        with open(os.path.join(d, "settings.json"), "w") as f:
            json.dump(got, f, indent=2)

    def test_doctor_names_the_seat_and_the_missing_guard(self):
        self._install_homes()
        self._install_seat("codex")
        self._install_seat("kimi")
        self._clean()                      # must-hit: clean before the hole
        self._drop(os.path.join(self.seats_root, "kimi", "claude"),
                   "suite-guard")
        warns = self._warns()
        self.assertEqual(len(warns), 1, warns)
        # the row must carry BOTH halves: WHICH config, and WHICH guard. A row
        # saying only "a seat is incomplete" sends its reader to diff 10 files.
        self.assertIn("kimi", warns[0])
        self.assertIn("suite-guard", warns[0])
        self.assertIn("helm hooks install", warns[0])
        self.assertNotIn("codex", warns[0])   # no smearing onto a fine seat

    def test_a_healthy_estate_makes_this_rung_emit_nothing(self):
        """FINDINGS-ONLY: fully resolved plus every config complete produces the
        EMPTY LIST — this specialized rung may print OK under no condition.

        The must-hit is its other half, in the same arm so neither can rot
        alone: an UNHEALTHY estate still emits its rows. Without that, a rung
        that returned [] unconditionally — the exact way to break this — would
        satisfy the silence half perfectly."""
        self._install_homes()
        self._install_seat("codex")
        self._install_seat("kimi")
        self.assertEqual(doctor.check_guard_contract(), [])
        # MUST-HIT: the same rung is not simply mute
        self._drop(os.path.join(self.seats_root, "kimi", "claude"),
                   "suite-guard")
        rows = doctor.check_guard_contract()
        self.assertTrue(rows, "a rung that is silent about a REAL gap is not "
                              "findings-only, it is blind")
        self.assertNotIn(doctor.OK, [lvl for lvl, _m in rows], rows)

    def test_a_home_missing_a_guard_is_reported(self):
        """FINDING 3: the first cut audited seats only. A credential home is a
        full launch surface — the owner's own ~/.claude is one — so a guard
        missing there is exactly as unguarded, and was invisible."""
        self._install_homes()
        self._install_seat("codex")
        self._clean()
        d = homes.DEFAULTS["claude"]
        self._drop(d, "suite-guard")
        warns = self._warns()
        self.assertTrue(any("home" in m and "suite-guard" in m for m in warns),
                        warns)
        self.assertTrue(any(d in m for m in warns), warns)

    def test_every_required_guard_is_audited_not_just_the_suite_guard(self):
        """The rung reads the SPEC LIST, so it covers guards added later — the
        property that keeps this from becoming another hand-kept enumeration."""
        self._install_homes()
        d = self._install_seat("codex")
        guards = [s["name"] for s in hooks.resolved_specs(hooks.SEAT_SPECS)
                  if s.get("gate")]
        # UNCONDITIONAL CONTROLS, so a loop that never runs cannot pass as
        # proof: the estate really does carry three gates, and the intact
        # estate really does read clean before any of them is dropped.
        self.assertEqual(sorted(guards),
                         ["argv-guard", "stop-guard", "suite-guard"])
        self._clean()
        for name in guards:
            with self.subTest(guard=name):
                hooks.install_home(d, specs=hooks.SEAT_SPECS)   # restore
                self._drop(d, name)
                self.assertTrue(any(name in m and "codex" in m
                                    for m in self._warns()), name)

    def test_an_external_guard_is_a_preserved_stray_to_envtidy(self):
        """The other half of the envtidy exclusion, asserted rather than
        assumed: envtidy must PRESERVE the suite-guard entry, not read it as
        drift to rewrite. `_helm_args` answering None is what routes it here."""
        from helm import envtidy
        spec = next(s for s in hooks.SPECS if s.get("external"))
        cmd = hooks.spec_command(spec)
        self.assertIsNone(envtidy._helm_args(cmd))
        d = self.mk_seat("codex", settings={
            "hooks": {spec["event"]: [{"matcher": spec["matcher"],
                                       "hooks": [{"type": "command",
                                                  "command": cmd}]}]}})
        self.assertIn(cmd, envtidy.plan_hooks_home("seat:codex", d)["strays"])

    # ---- FINDING 1: the pin is a PATH, and a relative one names nothing ----

    def test_a_relative_pin_is_refused_not_resolved(self):
        """A relative pin resolves against whatever cwd the asking process has,
        so a seat and a doctor run would disagree about the SAME config. It
        must be refused loudly, never silently resolved."""
        with open(os.path.join(self.tmp, "rel-guard"), "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(self.tmp, "rel-guard"), 0o755)
        os.environ["HELM_SUITE_GUARD"] = "rel-guard"
        spec = next(s for s in hooks.SPECS if s.get("external"))
        # MUST-HIT: the very same file, named absolutely, DOES resolve — so a
        # None below is the relativeness being refused, not a broken fixture.
        os.environ["HELM_SUITE_GUARD"] = os.path.join(self.tmp, "rel-guard")
        self.assertIsNotNone(hooks.external_bin(spec))
        os.environ["HELM_SUITE_GUARD"] = "rel-guard"
        self.assertIsNone(hooks.external_bin(spec))
        self.assertEqual(hooks.external_status(spec)[1], "relative-pin")
        msg = hooks.external_gap_message(spec)
        self.assertIn("RELATIVE", msg)
        self.assertIn("absolute", msg)
        self.assertTrue(any("RELATIVE" in m for m in self._warns()))

    def test_a_stale_pin_does_not_read_as_installed(self):
        """FINDING 1, second half. A pin that named a real executable when the
        hook was written and names nothing now leaves the config carrying a
        rendered command for it. That entry is a CLAIM, not coverage."""
        d = self._install_seat("codex")
        self._install_homes()
        self._clean()
        spec = next(s for s in hooks.SPECS if s.get("external"))
        self.assertIn(spec["name"], hooks._owned_specs(self.read_settings(d)))
        os.remove(self.suite_guard)              # the guard goes away AFTER install
        self.assertEqual(hooks.external_status(spec)[1], "pin-dead")
        warns = self._warns()
        self.assertTrue(any("STALE" in m and "codex" in m for m in warns), warns)
        # and it must NOT be reported as merely missing — install cannot fix it
        self.assertFalse(any("codex is MISSING" in m for m in warns), warns)

    def test_unresolvable_guard_is_its_own_loud_row(self):
        """A guard executable absent from the HOST is not a config gap: no
        install could close it, so it must not masquerade as one."""
        os.environ["HELM_SUITE_GUARD"] = os.path.join(self.tmp, "not-here")
        self._install_homes()
        self._install_seat("codex")
        warns = self._warns()
        self.assertTrue(any("suite-guard" in m and "NOT an executable" in m
                            for m in warns), warns)
        # every guard helm CAN install is installed, so no config is reported
        # as a missing-guard config
        self.assertFalse(any("is MISSING" in m for m in warns), warns)

    # ---- the MELD fallback: implicit, real, and now refused ----------------

    def test_a_live_meld_pin_does_not_resolve(self):
        """`home.env` falls back HELM_X -> MELD_X, so MELD_SUITE_GUARD — a name
        this code never writes — was ACCEPTED today. An external guard is a
        security boundary; its pin has exactly one spelling."""
        spec = next(s for s in hooks.SPECS if s.get("external"))
        legacy = os.path.join(self.tmp, "meld-guard")
        with open(legacy, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(legacy, 0o755)
        del os.environ["HELM_SUITE_GUARD"]
        os.environ["MELD_SUITE_GUARD"] = legacy
        # a LAMBDA, not addCleanup(os.environ.pop, ...): the env-hygiene
        # scanner reads restores syntactically and sees a bare method
        # REFERENCE as no restore at all — see the row filed on that gap
        self.addCleanup(lambda: os.environ.pop("MELD_SUITE_GUARD", None))
        # MUST-HIT: the very same file under the HELM_ name DOES resolve, so
        # the None below is the legacy name being refused, not a bad fixture.
        os.environ["HELM_SUITE_GUARD"] = legacy
        self.assertEqual(hooks.external_status(spec), (legacy, "ok"))
        del os.environ["HELM_SUITE_GUARD"]
        self.assertIsNone(hooks.external_bin(spec))
        self.assertEqual(hooks.external_status(spec)[1], "absent")

    def test_a_dead_meld_pin_does_not_mask_a_valid_path_guard(self):
        """The nastier half: a DEAD legacy value used to win over a perfectly
        good PATH resolution, turning a healthy host into an unguarded one on
        the strength of a name nobody wrote."""
        spec = next(s for s in hooks.SPECS if s.get("external"))
        binned = os.path.join(self.tmp, "bin")
        os.makedirs(binned)
        real = os.path.join(binned, spec["external"])
        with open(real, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(real, 0o755)
        del os.environ["HELM_SUITE_GUARD"]
        os.environ["MELD_SUITE_GUARD"] = os.path.join(self.tmp, "long-gone")
        # a LAMBDA, not addCleanup(os.environ.pop, ...): the env-hygiene
        # scanner reads restores syntactically and sees a bare method
        # REFERENCE as no restore at all — see the row filed on that gap
        self.addCleanup(lambda: os.environ.pop("MELD_SUITE_GUARD", None))
        prior = os.environ.get("PATH")
        os.environ["PATH"] = binned + os.pathsep + (prior or "")
        self.addCleanup(os.environ.__setitem__, "PATH", prior or "")
        # PATH wins, because the legacy name is not read at all
        self.assertEqual(hooks.external_status(spec), (real, "ok"))

    # ---- FINDING 2: the repair verb must not report a repair it did not do --

    def test_install_is_loud_and_not_full_when_a_guard_is_unresolvable(self):
        """`hooks install` used to write every resolvable spec, print
        "N of N covered (full hook contract)" and exit 0 on a host where a
        REQUIRED guard resolved to nothing. An operator who runs the repair
        verb and reads success must not have to run doctor to learn it did not
        repair."""
        self.mk_seat("codex")
        # MUST-HIT: with the guard resolvable the very same run IS clean and
        # DOES claim the full contract — so the failure below is the gap, not
        # a fixture that cannot succeed.
        rc, out, err = self.run_hooks(["install"])
        self.assertEqual(rc, 0, err)
        self.assertIn("full hook contract", out)
        self.assertNotIn("SHORTENED", out)
        os.environ["HELM_SUITE_GUARD"] = os.path.join(self.tmp, "not-here")
        rc, out, err = self.run_hooks(["install"])
        self.assertNotEqual(rc, 0, "an install that could not deliver the "
                                   "contract reported success")
        self.assertIn("suite-guard", err)          # loud, on the error channel
        self.assertIn("SHORTENED", out)            # and the claim is withdrawn
        self.assertNotIn("(full hook contract)", out)

    # ---- every DOOR propagates the shared result, none claims full --------

    def _dead_pin(self):
        os.environ["HELM_SUITE_GUARD"] = os.path.join(self.tmp, "long-gone")

    def test_the_shared_result_carries_the_shortened_state(self):
        """ONE result shape for BOTH installers — the alternative was four
        caller-local patches, which is how the estate got two lists to begin
        with. Every door below reads `.shortened` off the value it already
        has."""
        d = self.mk_seat("codex")
        proj = self.mk_project("p", settings={})
        # MUST-HIT: resolvable => not shortened, on both installers
        self.assertFalse(hooks.install_home(d, specs=hooks.SEAT_SPECS).shortened)
        self.assertFalse(hooks.install_project(proj).shortened)
        self._dead_pin()
        home_res = hooks.install_home(self.mk_seat("kimi"),
                                      specs=hooks.SEAT_SPECS)
        proj_res = hooks.install_project(self.mk_project("q", settings={}))
        # UNCONDITIONAL positive assertions on the flagged observable, outside
        # the loop: a loop that never ran would otherwise carry the whole proof.
        self.assertTrue(home_res.shortened)
        self.assertTrue(proj_res.shortened)
        for res in (home_res, proj_res):
            self.assertTrue(res.shortened)
            self.assertEqual(res.missing_names, "suite-guard")
            # even a door that only ever prints `detail` cannot call it clean
            self.assertIn("SHORTENED", res[1])
            self.assertEqual(len(res), 2, "the (action, detail) shape must "
                                          "survive for every existing caller")

    def test_the_direct_home_door_is_shortened_not_clean(self):
        """A DIRECT install_home — the door `launch --install` and every other
        non-CLI caller uses — must carry the state, not just the estate CLI."""
        self._dead_pin()
        res = hooks.install_home(self.mk_home("a-user-example"))
        self.assertTrue(res.shortened)
        self.assertNotIn("suite-guard", " ".join(
            hooks._all_hook_cmds(self.read_settings(
                os.path.join(homes.ROOTS["claude"], "a-user-example")))))

    def test_the_project_door_is_loud_and_nonzero(self):
        """Measured before this cure: rc0, no warning, settings omit the guard."""
        proj = self.mk_project("p", settings={})
        rc, _out, _err = self.run_hooks(["install", "--project", proj])
        self.assertEqual(rc, 0)                       # must-hit: it CAN pass
        self._dead_pin()
        rc, _out, err = self.run_hooks(["install", "--project", proj])
        self.assertNotEqual(rc, 0, "a project install that omitted a required "
                                   "guard reported success")
        self.assertIn("SHORTENED", err)
        self.assertIn("suite-guard", err)

    def test_status_counts_and_table_agree_across_a_racing_estate(self):
        """ONE SNAPSHOT: the table and every count beside it must describe the
        SAME moment.

        `hooks status` printed its table from one `status_rows()` scan and its
        counts from a SECOND scan inside `coverage()`. On a live estate the two
        raced, and a probe saw a count over a population the table never listed
        — "guard 1 of 2" with no MISSING row, each half true and the pair
        incoherent.

        This does not count calls. It MUTATES THE ESTATE between the two former
        reads — a home disappears after the first — and then requires the
        printed denominators to equal the number of rows actually tabled. One
        read is unaffected by the mutation; two reads cannot agree."""
        self.mk_home("a-user-example")
        self.mk_home("b-user-example")
        for _n, p in hooks.claude_homes():
            hooks.install_home(p)
        real, seen = hooks.status_rows, {"n": 0}

        def racing():
            rows = real()
            seen["n"] += 1
            if seen["n"] == 1:          # the estate changes between reads
                shutil.rmtree(os.path.join(homes.ROOTS["claude"], "b-user-example"),
                              ignore_errors=True)
            return rows

        with mock.patch.object(hooks, "status_rows", side_effect=racing):
            rc, out, _err = self.run_hooks(["status"])
        self.assertEqual(rc, 0)
        tabled = [ln for ln in out.splitlines()[2:]
                  if ln.startswith("  ") and not ln.startswith("  home ")]
        self.assertGreaterEqual(len(tabled), 2, out)  # must-hit: table was built
        # THE DENOMINATOR IS DERIVED FROM THE TABLE, not hardcoded: what must
        # hold is that every count describes the population actually printed,
        # whatever that population turned out to be.
        n = len(tabled)
        self.assertIn("inject coverage (this lane only): %d of %d" % (n, n), out)
        self.assertIn("home guard contract: %d of %d homes" % (n, n), out)

    # ---- FINDING 4: the claim and the audit read ONE list --------------

    def test_status_seat_count_comes_from_the_audited_set(self):
        """`hooks status` recomputed its coverage inline over raw SPECS while
        the audit measured the RESOLVED set — two lists answering one question,
        which is how "10 of 10 seats covered" got printed over two unguarded
        seats. A seat missing a guard must make STATUS say so."""
        d = self._install_seat("codex")
        self._install_seat("kimi")
        rc, out, _err = self.run_hooks(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("seat hooks: 2 of 2 seats", out)     # must-hit
        self._drop(d, "suite-guard")
        rc, out, _err = self.run_hooks(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("seat hooks: 1 of 2 seats", out)
        # and the printed count IS the audited one, not a parallel reading
        rows, _u = hooks.seat_gap_rows()
        self.assertEqual(hooks.covered_count(rows), 1)
        self.assertEqual(hooks.seat_coverage()[0], 1)

    def test_status_and_audit_agree_when_a_guard_is_unresolvable(self):
        """THE CASE THE TWO LISTS ACTUALLY DIVERGE ON, and the one a
        both-lists-agree fixture cannot see: while every required guard
        resolves, raw SPECS and `resolved_specs` are the same tuple, so a
        status that recomputes from the raw list still prints the right number
        and the drift is invisible. It appears only once a spec is dropped from
        the resolved set — the reader that asks the raw list then counts a
        lane that was deliberately never written, and calls a wired seat
        uncovered. Status must report what the audit reports, always."""
        # UNRESOLVABLE BEFORE THE INSTALL, deliberately: the guard is then
        # never written, so these configs are not STALE (they claim nothing) —
        # they are correctly complete over the shortened contract. That is the
        # only state in which the raw and resolved lists give different
        # answers, and so the only state that can catch the drift.
        os.environ["HELM_SUITE_GUARD"] = os.path.join(self.tmp, "not-here")
        self._install_seat("codex")
        self._install_seat("kimi")
        rows, _u2 = hooks.seat_gap_rows()
        self.assertEqual([r["stale"] for r in rows], [[], []])
        # the audit's reading: every guard this host CAN install is installed
        self.assertEqual(hooks.seat_coverage()[0], 2)
        rc, out, _err = self.run_hooks(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("seat hooks: 2 of 2 seats", out)
        self.assertNotIn("seat hooks: 0 of 2 seats", out)
        # and the host-level gap is still SAID, on this surface too — the
        # count being honest is not the same as the estate being guarded
        self.assertIn("suite-guard", out)

    # ---- FINDING 5: OK is a claim, and an unresolved guard falsifies it ----

    def test_no_ok_while_a_required_guard_resolves_to_nothing(self):
        """Every config can be complete over the guards this host CAN install
        and the estate still be unguarded: `resolved_specs` SHORTENED the
        contract the count was taken over. OK there is finding 2 in a doctor
        hat — a clean word earned by not asking the question."""
        self._install_homes()
        self._install_seat("codex")
        self._clean()                    # must-hit: silence IS reachable here
        os.environ["HELM_SUITE_GUARD"] = os.path.join(self.tmp, "not-here")
        rows = doctor.check_guard_contract()
        self.assertNotIn(doctor.OK, [lvl for lvl, _m in rows], rows)
        self.assertTrue(any("SHORTENED contract" in m
                            for _lvl, m in rows), rows)


class SuiteGuardIsOptionalUntilConfiguredTest(HooksBase):
    """helm does not ship the local-suite guard's executable, so a host that
    never installed one is not a broken host: every other hook installs, the
    guard reads "not configured (optional)", and nothing refuses. CONFIGURED
    means an executable on PATH or HELM_SUITE_GUARD set; from then on it is
    required exactly as before, and a pin that stops resolving is SHORTENED.
    Each arm pairs the optional reading with the required one it must keep."""

    def spec(self):
        return next(s for s in hooks.SPECS if s["name"] == "suite-guard")

    def _warns(self):
        return [m for lvl, m in doctor.check_guard_contract()
                if lvl == doctor.WARN]

    def unconfigure(self):
        """No pin, and a PATH holding no guard, asserted rather than assumed:
        a build node that carries the executable would otherwise run the
        configured case under this arm's name."""
        prior_pin = os.environ.pop("HELM_SUITE_GUARD", None)
        self.addCleanup(lambda: prior_pin is None or os.environ.__setitem__(
            "HELM_SUITE_GUARD", prior_pin))
        empty = os.path.join(self.tmp, "empty-bin")
        os.makedirs(empty, exist_ok=True)
        prior_path = os.environ.get("PATH")
        os.environ["PATH"] = os.pathsep.join((empty, "/usr/bin", "/bin"))
        self.addCleanup(os.environ.__setitem__, "PATH", prior_path or "")
        self.assertEqual(hooks.external_status(self.spec()), (None, "absent"))

    def test_an_unconfigured_guard_installs_everything_else_and_passes(self):
        self.unconfigure()
        d = self.mk_seat("codex")
        rc, out, err = self.run_hooks(["install"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("SHORTENED", out + err)
        self.assertIn("suite-guard: not configured (optional)", out)
        self.assertIn("(full hook contract)", out)
        row = hooks._gap_row("codex", d, hooks.SEAT_SPECS)
        self.assertEqual(row["missing"], [])
        self.assertNotIn("suite-guard", row)
        self.assertEqual(hooks.unresolved_externals(), [])
        self.assertEqual([s["name"] for s, _m in hooks.unconfigured_optionals()],
                         ["suite-guard"])
        self.assertFalse(hooks.install_home(self.mk_seat("kimi"),
                                            specs=hooks.SEAT_SPECS).shortened)
        self.assertEqual(hooks.preflight(d), (0, ""))
        self.assertEqual(self._warns(), [])
        rc, out, _err = self.run_hooks(["status"])
        self.assertIn("suite-guard: not configured (optional)", out)

    def test_a_guard_on_PATH_is_configured_and_installed(self):
        self.unconfigure()
        binned = os.path.join(self.tmp, "guard-bin")
        os.makedirs(binned)
        exe = os.path.join(binned, hooks.SUITE_GUARD_BIN)
        with open(exe, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(exe, 0o755)
        os.environ["PATH"] = binned + os.pathsep + os.environ["PATH"]
        self.assertEqual(hooks.external_status(self.spec()), (exe, "ok"))
        d = self.mk_seat("codex")
        rc, out, err = self.run_hooks(["install"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("not configured", out + err)
        self.assertTrue(hooks._gap_row("codex", d, hooks.SEAT_SPECS)["suite-guard"])
        self.assertEqual(hooks.unconfigured_optionals(), [])

    def test_a_guard_only_on_a_RELATIVE_PATH_entry_is_configured(self):
        """`which` finds it, so the operator configured it; the canonical
        resolver refuses a relative answer, so it is a required gap, never
        the optional state."""
        self.unconfigure()
        rel = os.path.join(self.tmp, "relbin")
        os.makedirs(rel)
        exe = os.path.join(rel, hooks.SUITE_GUARD_BIN)
        with open(exe, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(exe, 0o755)
        os.environ["PATH"] = os.path.relpath(rel, os.getcwd())
        self.assertEqual(hooks.external_status(self.spec()), (None, "absent"))
        self.assertFalse(hooks.optional_unconfigured(self.spec()))
        self.assertEqual([s["name"] for s, _w, _m in hooks.unresolved_externals()],
                         ["suite-guard"])
        self.assertEqual(hooks.unconfigured_optionals(), [])

    def test_a_configured_guard_that_stops_resolving_is_still_required(self):
        d = self.mk_seat("codex")
        self.assertTrue(hooks.install_home(d, specs=hooks.SEAT_SPECS)[0])
        self.assertEqual(hooks.unconfigured_optionals(), [])
        os.environ["HELM_SUITE_GUARD"] = os.path.join(self.tmp, "long-gone")
        self.assertEqual([s["name"] for s, _w, _m in hooks.unresolved_externals()],
                         ["suite-guard"])
        self.assertEqual(hooks.unconfigured_optionals(), [])
        rc, out, err = self.run_hooks(["install"])
        self.assertNotEqual(rc, 0)
        self.assertIn("SHORTENED", out)
        self.assertNotIn("not configured (optional)", out + err)
        self.assertEqual(hooks.preflight(d)[0], 1)


class SeatCoverageTest(HooksBase):
    """Seats (multimodel CLAUDE_CONFIG_DIRs) get the same full hook contract as
    homes under the same merge-preserving / CAS / idempotence laws."""

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
        no full hook contract must appear as an UNCOVERED row — and the gated
        write must then REACH it (deliberately not registered in HOME_ROOTS:
        _is_seat_home has to recognize the instance shape on its own)."""
        fam = self.mk_seat("codex")
        hooks.install_home(fam, specs=hooks.SEAT_SPECS)
        inst = os.path.join(self.seats_root, "codex", "instances",
                            "codex-2", "claude")
        os.makedirs(inst)                   # bare: no settings.json at all
        rows = {r["seat"]: r for r in hooks.seat_status_rows()[0]}
        self.assertIn("codex-2", rows,
                      "the nested instance is OUTSIDE the census")
        self.assertFalse(rows["codex-2"]["deliver"])
        self.assertEqual(hooks.seat_coverage(), (1, 2, []))
        action, detail = hooks.install_home(inst, specs=hooks.SEAT_SPECS)
        self.assertEqual(action, "add", detail)
        self.assertEqual(hooks.seat_coverage(), (2, 2, []))
        got = self.read_settings(inst)
        for name in ("inject", "handoff-precompact", "handoff-sessionend"):
            s = next(s for s in hooks.SEAT_SPECS if s["name"] == name)
            self.assertIn(hooks.spec_command(s),
                          hooks._hook_cmds(got, s["event"]))

    def test_install_wires_delivery_lane_without_dropping_foreign(self):
        foreign = "echo seat-local-hook"          # a pre-existing foreign hook
        d = self.mk_seat("codex", settings={
            "model": "gpt-5.6-sol",
            "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
                {"type": "command", "command": foreign}]}]}})
        by_name = {s["name"]: s for s in hooks.SEAT_SPECS}
        action, _ = hooks.install_home(d, specs=hooks.SEAT_SPECS)
        self.assertEqual(action, "add")
        got = self.read_settings(d)
        # the seat's own settings + foreign hook survive byte-identical
        self.assertEqual(got["model"], "gpt-5.6-sol")
        self.assertEqual(got["hooks"]["PreToolUse"][0]["hooks"][0]["command"], foreign)
        # The whole contract is present, including the producer/consumer pair
        # that the old delivery-only policy split across different surfaces.
        for s in hooks.SEAT_SPECS:
            self.assertIn(hooks.spec_command(s),
                          hooks._hook_cmds(got, s["event"]), s["name"])
        self.assertEqual(got["hooks"]["PostToolUse"][-1]["matcher"], "*")
        self.assertEqual(got["hooks"]["SubagentStop"][-1]["matcher"], "*")
        self.assertEqual(got["hooks"]["SessionStart"][-1]["matcher"], "*")
        for name in ("inject", "stop-guard", "handoff-precompact",
                     "handoff-sessionend"):
            event = by_name[name]["event"]
            mine = next(g for g in got["hooks"][event]
                        if hooks.spec_command(by_name[name]) in
                        [h["command"] for h in g["hooks"]])
            self.assertNotIn("matcher", mine, name)
        # idempotent: a second install detects up-to-date, writes nothing new
        self.assertEqual(hooks.install_home(d, specs=hooks.SEAT_SPECS),
                         ("ok", "hook up to date"))
        self.assertEqual(len(hooks._hook_cmds(got, "PostToolUse")), 1)

    def test_seat_coverage_and_status_surface_the_gap(self):
        covered = self.mk_seat("codex")
        hooks.install_home(covered, specs=hooks.SEAT_SPECS)
        self.mk_seat("kimi")                       # bare — no seat hooks
        rows = {r["seat"]: r for r in hooks.seat_status_rows()[0]}
        self.assertTrue(all(rows["codex"][s["name"]]
                            for s in hooks.SEAT_SPECS))
        self.assertFalse(rows["kimi"]["inject"])
        self.assertFalse(rows["kimi"]["handoff-precompact"])
        self.assertFalse(rows["kimi"]["handoff-sessionend"])
        self.assertEqual(hooks.seat_coverage(), (1, 2, []))
        rc, out, _ = self.run_hooks(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("seats (full hook contract", out)
        self.assertIn("inject", out)
        self.assertIn("handoff", out)
        self.assertIn("seat hooks: 1 of 2 seats", out)

    def test_full_install_covers_every_seat_and_reports(self):
        self.mk_seat("codex")
        self.mk_seat("kimi", settings={"model": "kimi-k3"})  # foreign key present
        rc, out, err = self.run_hooks(["install"])
        self.assertEqual(rc, 0, err)
        self.assertIn("seats (full hook contract", out)
        self.assertIn("2 of 2 seats covered (full hook contract)", out)
        for fam in ("codex", "kimi"):
            got = self.read_settings(os.path.join(self.seats_root, fam, "claude"))
            for s in hooks.SEAT_SPECS:
                self.assertIn(hooks.spec_command(s),
                              hooks._hook_cmds(got, s["event"]),
                              "%s missing from %s" % (s["name"], fam))
        self.assertEqual(self.read_settings(
            os.path.join(self.seats_root, "kimi", "claude"))["model"], "kimi-k3")

    def test_home_narrowed_install_leaves_seats_untouched(self):
        self.mk_home("a-user-example")
        self.mk_seat("codex")
        rc, out, _ = self.run_hooks(["install", "--home", "a-user-example"])
        self.assertEqual(rc, 0)
        # a --home-scoped run never reaches the seats
        self.assertFalse(os.path.exists(os.path.join(
            self.seats_root, "codex", "claude", "settings.json")))
        self.assertEqual(hooks.seat_coverage(), (0, 1, []))


class SeatCensusCompletenessTest(HooksBase):
    """task/331's disease one layer down (a review's probe): the WIDENED walk
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
        hooks.install_home(fam, specs=hooks.SEAT_SPECS)
        with self._locked(inst):
            rc, out, _ = self.run_hooks(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("seat hooks: 1 of 1 seats", out)
        self.assertIn("seat census INCOMPLETE", out)
        self.assertIn(inst, out)

    def test_install_wires_the_visible_and_fails_loud_on_the_unread(self):
        """UNKNOWN-and-report, never abort: the seats the walk CAN see still
        get their full hook contract (a hard-fail would deny the working fleet
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
    """task/331's disease one layer OUT (a review's second probe, at
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
        now carries the full hook contract to the seats it can see."""
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
        action, detail = hooks.install_home(d, specs=hooks.SEAT_SPECS)
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
        # always has is not testing production. A review called this a real
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
    # scanned /proc itself. The integrator ruled it a per-case handler —
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
    # PRODUCER CONTRACT (producer layer, not another
    # consumer). running_panes' docstring promised "every live claude-harness
    # PTY of THIS uid; other-uid entries skipped" and the implementation never
    # looked at st_uid. The promise held only INCIDENTALLY: another user's
    # environ is EACCES, and both reads used to sit in one try with a bare
    # `continue`. The cure for the vanishing-pane bug removed that continue —
    # and converted an incidental guarantee into a FALSE CLAIM in the same
    # stroke. A foreign claude then entered OUR snapshot as OUR blind pane.
    def test_another_users_claude_pane_is_NOT_in_our_census(self):
        """OWNERSHIP IS PROVEN, NEVER INFERRED FROM A READ FAILURE.

        The cross-UID positive control a review asked for by name. A real
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
        it re-enters as our blind pane, which is exactly the taint a review
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

        A review found it. A new branch is a new place for the class to live
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
        """THE PROMISE THE INTEGRATOR BOUND ACCEPTANCE ON, made structural.

        NOT A LIVENESS GATE, which is what this arm used to call it and what
        the name said until a review pointed at the prose. Liveness is the
        smaller half. The seam enforces INCARNATION COHERENCE for every row —
        that all the reads describe ONE process, so the row is true at SOME
        instant — and OWNERSHIP REPROOF for uncertainty rows only. A gate that
        merely asked "is it still alive" would pass a pid recycled to another
        process, which is the defect that made this the seam it is.

        A /proc scan is N reads with N-1 gaps and the process can exit in
        every one. We cured them one at a time — the ownership/environ gap,
        then (pid 7331) the gap BETWEEN the two detector reads — and
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
        """A ROW TRUE AT NO INSTANT — the pid-7441 repro.

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
        """The pid-7443 repro, and it defeats a bracket that is
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
        # pointing at the wrong instrument. A review caught it.
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
        SNAPSHOTS. A probe built the same-pid case: coverage UNKNOWN from
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
        (Converged in a convergence meld.)"""
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
        for its own scan. (The third consumer of this return value.)"""
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

        A review found this in the consumer I had explicitly flagged as
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
        self.mk_home("a-user-example")
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
        # always has is not testing production. A review called this a real
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

    ACTORS = {"seat-a": {"home_room": "helm"}, "seat-b": {"home_room": "helm"},
              "seat-c": {"home_room": "helm"}, "seat-d": {"home_room": "helm"},
              "seat-e": {"cwd": "/tmp/x"}}           # rostered, never joined

    def _signer_env(self):
        signer = os.path.join(self.tmp, "dregg-client-sign")
        with open(signer, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(signer, 0o755)
        return ("HELM_CELL_BIN=%s" % signer).encode()

    def _flagged(self, proc, roster=None, failed=False):
        """{seat: sign_reason} over the fake /proc, against a roster read
        that answers `roster` (or is judged `failed`)."""
        with mock.patch("helm.seats.roster_checked",
                        return_value=(roster if roster is not None
                                      else self.ACTORS, failed)):
            return {p["seat"]: p["sign_reason"]
                    for p in hooks.unsigned_panes(proc)}

    def test_a_pane_whose_profile_is_NOT_ITS_SEAT_is_flagged(self):
        """The signing gate refuses a fleet actor whose profile names someone
        else, so such a pane posts UNSIGNED as surely as one with no trio. The
        profile compared is the one the gate reads: HELM_CELL_PROFILE first."""
        bin_env = self._signer_env()
        proc = self.mk_proc(21, [b"claude"],
                            [b"HELM_CHAT_NAME=seat-a", bin_env,
                             b"HELM_CELL_PROFILE=owner-profile"])
        # the gate reads HELM_CELL_PROFILE before DREGG_PROFILE
        self.mk_proc(22, [b"claude"],
                     [b"HELM_CHAT_NAME=seat-c", bin_env,
                      b"HELM_CELL_PROFILE=owner-profile",
                      b"DREGG_PROFILE=seat-c"])
        # control: a pane signing as its own seat is not flagged
        self.mk_proc(23, [b"claude"],
                     [b"HELM_CHAT_NAME=seat-b", bin_env,
                      b"HELM_CELL_PROFILE=seat-b", b"DREGG_PROFILE=seat-b"])
        rows = self._flagged(proc)
        self.assertEqual(sorted(rows), ["seat-a", "seat-c"])
        self.assertIn("owner-profile", rows["seat-a"])
        self.assertIn("identity_conflict", rows["seat-a"])
        self.assertIn("identity_conflict", rows["seat-c"])

    def test_a_NON_ACTOR_pane_with_a_borrowed_profile_is_not_flagged(self):
        """The gate signs a named pane that is not a fleet actor as its
        ambient profile (rule 4), so the surface must not claim a refusal:
        a rostered row without home_room, and a name with no row at all."""
        bin_env = self._signer_env()
        proc = self.mk_proc(24, [b"claude"],
                            [b"HELM_CHAT_NAME=seat-e", bin_env,
                             b"HELM_CELL_PROFILE=owner-profile"])
        self.mk_proc(25, [b"claude"],
                     [b"HELM_CHAT_NAME=seat-f", bin_env,
                      b"HELM_CELL_PROFILE=owner-profile"])
        # positive control on the same scan: an ACTOR under that profile is
        self.mk_proc(21, [b"claude"],
                     [b"HELM_CHAT_NAME=seat-a", bin_env,
                      b"HELM_CELL_PROFILE=owner-profile"])
        self.assertEqual(sorted(self._flagged(proc)), ["seat-a"])

    def test_a_BLANK_profile_falls_through_as_it_does_in_the_gate(self):
        """cell.signer_profile skips a blank value; so must the scan, or a
        pane the gate signs as seat-d is reported under the profile ''."""
        bin_env = self._signer_env()
        proc = self.mk_proc(26, [b"claude"],
                            [b"HELM_CHAT_NAME=seat-d", bin_env,
                             b"HELM_CELL_PROFILE=  ", b"DREGG_PROFILE=seat-d"])
        self.mk_proc(21, [b"claude"],
                     [b"HELM_CHAT_NAME=seat-a", bin_env,
                      b"HELM_CELL_PROFILE=owner-profile"])
        scanned = {p["seat"]: p["signer_profile"]
                   for p in hooks.running_panes(proc)}
        self.assertEqual(scanned["seat-d"], "seat-d")
        self.assertEqual(sorted(self._flagged(proc)), ["seat-a"])

    def test_an_UNREADABLE_roster_flags_a_borrowed_profile(self):
        """While the roster cannot be read the gate refuses any profile that
        is not the pane's own seat, actor or not, and says why."""
        bin_env = self._signer_env()
        proc = self.mk_proc(24, [b"claude"],
                            [b"HELM_CHAT_NAME=seat-e", bin_env,
                             b"HELM_CELL_PROFILE=owner-profile"])
        # control: a pane signing as itself needs no roster
        self.mk_proc(23, [b"claude"],
                     [b"HELM_CHAT_NAME=seat-b", bin_env,
                      b"HELM_CELL_PROFILE=seat-b"])
        rows = self._flagged(proc, roster={}, failed=True)
        self.assertEqual(sorted(rows), ["seat-e"])
        self.assertIn("identity_unreadable", rows["seat-e"])

    OWNER_SID = "sid-seat-a-0000-0001"

    def _owner_export(self):
        """seat-a is a fleet actor whose session is OWNER_SID, and
        `owner-profile` is an owner name (pinned, never the runner's)."""
        rows = dict(self.ACTORS)
        rows["seat-a"] = {"home_room": "helm", "session": self.OWNER_SID,
                          "sessions": [self.OWNER_SID]}
        return rows

    def test_an_OWNER_EXPORT_pane_bound_to_its_row_is_INFO_not_a_refusal(self):
        """task/3049: the gate sets the owner's inherited profile aside for a
        seat whose session is bound to its row, so the pane SIGNS — as itself.
        Reporting it under "posting UNSIGNED — relaunch" would spend its
        context on nothing. RED before: flagged identity_conflict."""
        bin_env = self._signer_env()
        proc = self.mk_proc(31, [b"claude"],
                            [b"HELM_CHAT_NAME=seat-a", bin_env,
                             b"HELM_CELL_PROFILE=owner-profile",
                             ("CLAUDE_CODE_SESSION_ID=%s"
                              % self.OWNER_SID).encode()])
        # control on the same scan: an owner-export actor pane whose session
        # is NOT bound to its row keeps the refusal line
        self.mk_proc(32, [b"claude"],
                     [b"HELM_CHAT_NAME=seat-c", bin_env,
                      b"HELM_CELL_PROFILE=owner-profile",
                      b"CLAUDE_CODE_SESSION_ID=sid-elsewhere"])
        with mock.patch.dict(os.environ,
                             {"HELM_CHAT_OWNER_NAMES": "owner-profile"}), \
                mock.patch("helm.seats.roster_checked",
                           return_value=(self._owner_export(), False)), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            rows = {p["seat"]: p for p in hooks.unsigned_panes(proc)}
        self.assertTrue(rows["seat-a"].get("sign_info"))
        self.assertIn("signs as its own seat 'seat-a'",
                      rows["seat-a"]["sign_reason"])
        self.assertFalse(rows["seat-c"].get("sign_info"))
        self.assertIn("identity_conflict", rows["seat-c"]["sign_reason"])
        self.assertIn("session is not", rows["seat-c"]["sign_reason"])

    def test_a_CLAUDE_pane_is_bound_through_its_pid_keyed_session_record(self):
        """A claude pane's own environ carries NO session id (Claude Code sets
        it only for its children — measured on every live pane), so the scan
        reads the pid-keyed session record `sessions.live_sids` keeps."""
        bin_env = self._signer_env()
        proc = self.mk_proc(33, [b"claude"],
                            [b"HELM_CHAT_NAME=seat-a", bin_env,
                             b"HELM_CELL_PROFILE=owner-profile"])
        with mock.patch.dict(os.environ,
                             {"HELM_CHAT_OWNER_NAMES": "owner-profile"}), \
                mock.patch("helm.seats.roster_checked",
                           return_value=(self._owner_export(), False)), \
                mock.patch("helm.sessions.live_sids",
                           return_value={self.OWNER_SID: 33}):
            rows = hooks.unsigned_panes(proc)
            out = io.StringIO()
            with mock.patch.object(hooks, "running_panes",
                                   return_value=hooks.running_panes(proc)), \
                    mock.patch.object(hooks, "uncovered_panes",
                                      return_value=[]):
                hooks.surface_uncovered(out=out)
        self.assertEqual([(p["pid"], bool(p.get("sign_info"))) for p in rows],
                         [(33, True)])
        self.assertIn("SIGN AS THEIR OWN SEAT", out.getvalue())
        self.assertNotIn("posting UNSIGNED", out.getvalue())

    def test_unnamed_pane_not_judged_and_surface_prints(self):
        proc = self.mk_proc(15, [b"claude"], [b"TERM=xterm"])
        self.assertEqual(hooks.unsigned_panes(proc), [])
        self.mk_proc(16, [b"claude"], [b"HELM_CHAT_NAME=kimi"])
        out = io.StringIO()
        hooks.surface_uncovered(out=out)       # HELM_PROC = the fake tree
        self.assertIn("UNSIGNED", out.getvalue())
        self.assertIn("kimi", out.getvalue())
        self.assertIn("launch.sh", out.getvalue())


class FleetHookScopeTest(unittest.TestCase):
    """A fleet hook runs in helm's project and nowhere else.

    The hooks install PER CLAUDE HOME, not per project, so an unscoped fleet
    hook runs in every project on the machine — measured 2026-08-13, agents in
    two client projects were enrolled as helm seats by `chat join` and
    then fed helm DMs by `chat deliver`.

    THE GATE IS IN PYTHON, NOT IN THE GENERATED SHELL STRING, and two arms
    below pin that placement rather than the behaviour: a `case "$PWD"` prefix
    was tried first and ran BEFORE the fail-open alarms could arm (recreating
    the silent-guard shape of the 2026-08-04 outage) while also breaking
    `envtidy._helm_args`, so helm stopped recognising its own hooks.
    """
    FLEET = ("chat", ["deliver", "--hook-json"])

    def _at(self, project, verb=None, rest=None, expect_consulted=True):
        """hook_skips_here as if cwd resolved to `project`.

        `expect_consulted` is a POSITIVE CONTROL IN BOTH DIRECTIONS. True
        asserts the double actually bound — a patch that silently failed to
        bind would consult the REAL registry and every assertion here would be
        about this machine instead of the fixture. False asserts the opposite
        and is the stronger claim: the cheap paths (no --hook-json, or an
        unscoped verb) must SHORT-CIRCUIT before any registry read, so an
        unrelated project pays nothing for helm's hooks being installed."""
        verb, rest = (verb or self.FLEET[0]), (rest or list(self.FLEET[1]))
        seen = []

        def fake(path):
            seen.append(path)
            return "helm" if path == "/HELM/ROOT" else project

        with mock.patch.object(hooks, "helm_bin",
                               return_value="/HELM/ROOT/bin/helm"), \
                mock.patch("helm.inject._ledger.project_for_cwd", fake):
            out = hooks.hook_skips_here(verb, rest)
        if expect_consulted:
            self.assertTrue(seen, "project_for_cwd was never consulted — the "
                                  "double did not bind and this proves nothing")
        else:
            self.assertEqual(seen, [], "this path must answer WITHOUT reading "
                                       "the project registry at all")
        return out

    def test_a_fleet_hook_is_silent_outside_helms_project(self):
        self.assertTrue(self._at("sitka-inc"))
        self.assertTrue(self._at("bench-dev"))

    def test_a_fleet_hook_still_runs_inside_helm(self):
        # MUST-HIT. Without this the suite passes on a gate that is broken
        # everywhere, which is exactly how the first attempt looked green.
        self.assertFalse(self._at("helm"))

    def test_an_unregistered_cwd_is_not_helm(self):
        self.assertTrue(self._at(None))

    def test_an_unscoped_hook_runs_in_every_project(self):
        for verb, rest in (("inject", ["--hook-json"]),
                           ("chat", ["argv-guard", "--hook-json"]),
                           # continuity is PER-PROJECT utility, not fleet
                           # apparatus: handoff writes ~/.helm/<project>/journal
                           # and resume-turn reads it. Scoping these broke other
                           # projects' compaction continuity — pinned here.
                           ("handoff", ["check", "--hook-json"]),
                           ("seat", ["resume-turn", "--hook-json"]),
                           # task/2987: saguide scopes its own payload, so
                           # another project's subagent hears that project's
                           # reflex and nothing of helm's.
                           ("saguide", ["--hook-json"])):
            self.assertFalse(self._at("sitka-inc", verb, rest,
                                      expect_consulted=False),
                             "%s must never be scoped — the store/lexicon and "
                             "shell-substitution rungs serve every project"
                             % verb)

    # THE ORCA DOOR (task/2673 PART C): JOIN, and only join, runs for a pane
    # Orca opened, whatever its project. All four quadrants of
    # (Orca pane or not) x (helm cwd or not), plus a scoped non-join hook.
    JOIN = ("chat", ["join", "--hook-json"])

    def _orca(self, pane):
        env = {"ORCA_PANE_KEY": "pk-1"} if pane else {}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        if not pane:
            os.environ.pop("ORCA_PANE_KEY", None)

    def test_an_orca_pane_joins_outside_helm(self):
        self._orca(True)
        self.assertFalse(self._at("sitka-inc", *self.JOIN,
                                  expect_consulted=False))

    def test_an_orca_pane_joins_inside_helm(self):
        self._orca(True)
        self.assertFalse(self._at("helm", *self.JOIN, expect_consulted=False))

    def test_a_pane_orca_did_not_open_is_still_scoped_out(self):
        self._orca(False)
        self.assertTrue(self._at("sitka-inc", *self.JOIN))

    def test_a_pane_orca_did_not_open_still_joins_inside_helm(self):
        self._orca(False)
        self.assertFalse(self._at("helm", *self.JOIN))

    def test_the_orca_door_opens_join_only(self):
        self._orca(True)
        for verb, rest in (self.FLEET,
                           ("chat", ["stop-guard", "--hook-json"]),
                           ("chat", ["delegation-stop", "--hook-json"])):
            self.assertTrue(self._at("sitka-inc", verb, list(rest)),
                            "%s %s must stay scoped under Orca" % (verb, rest))

    def test_a_human_invocation_is_never_skipped(self):
        # --hook-json is the discriminator; `helm chat deliver` typed by hand
        # must behave identically wherever it runs.
        self.assertFalse(self._at("sitka-inc", "chat", ["deliver"],
                                  expect_consulted=False))

    def test_the_resolver_raising_fails_OPEN(self):
        def boom(path):
            raise RuntimeError("registry unreadable")
        with mock.patch.object(hooks, "helm_bin",
                               return_value="/HELM/ROOT/bin/helm"), \
                mock.patch("helm.inject._ledger.project_for_cwd", boom):
            self.assertFalse(hooks.hook_skips_here(*self.FLEET),
                             "cannot-tell must RUN the hook, never silence it")

    def test_no_scope_prefix_reaches_the_generated_command(self):
        # REGRESSION PIN, and the reason the gate is in Python. A shell prefix
        # here would run ahead of the fail-open alarms below.
        for spec in hooks.SPECS:
            cmd = hooks.spec_command(spec)
            self.assertTrue(cmd.startswith(shlex.quote(hooks.wrapper_bin())
                                           + " "),
                            "%s: a prefix before the wrapper suppresses the "
                            "fail-open alarm for a dead guard" % spec["name"])
            self.assertNotIn('case "$PWD"', cmd)

    def test_envtidy_can_still_identify_every_helm_hook(self):
        # REGRESSION PIN: the shell prefix made _helm_args return None, so the
        # estate census reported helm's own installed hooks as MISSING.
        #
        # EXTERNAL specs are excluded because they do not invoke helm at all —
        # `_helm_args` answering None for one is the CORRECT answer, not lost
        # identity, and envtidy's own consumer treats such a command as a stray
        # it PRESERVES (never rewrites, never deletes). The exclusion is by the
        # `external` key rather than by name so a second external guard cannot
        # quietly re-enter this pin.
        from helm import envtidy
        helm_specs = [s for s in hooks.SPECS if not s.get("external")]
        self.assertTrue(helm_specs)
        for spec in helm_specs:
            self.assertEqual(envtidy._helm_args(hooks.spec_command(spec)),
                             spec["args"], "identity lost for %s" % spec["name"])



class OwnershipIsTokenwiseTest(HooksBase):
    """Dispatch 70524837: marker ownership SUBSTRING-matched
    lookalike commands. Ownership is a license, not a label — status counts
    the entry as helm's and sync REWRITES it — so a foreign hook whose
    command merely CONTAINED a marker was helm's to edit."""

    def test_every_rendered_spec_command_is_still_ours(self):
        # MUST-HIT: the boundary cure may not orphan a single real entry —
        # these are the exact strings the installer writes. The unconditional
        # control first: an empty SPECS would turn the loop below into a
        # vacuous pass, and the inject form must hold with no loop at all.
        self.assertTrue(hooks._ours(hooks.hook_command()))
        self.assertGreater(len(hooks.SPECS), 0)
        for sp in hooks.SPECS:
            cmd = hooks.spec_command(sp)
            self.assertTrue(hooks._ours(cmd), cmd)

    def test_a_lookalike_containing_a_marker_is_not_ours(self):
        # The reported class is GLUING: a marker inside a longer word. One
        # unconditional pair first — the exact suffix-twin from the finding,
        # beside its real form — so this arm cannot pass on an empty loop.
        self.assertFalse(
            hooks._ours("timeout 3 /x/bin/fab-suite-pretooluse-old; rc=$?"))
        self.assertTrue(
            hooks._ours("timeout 3 /x/bin/fab-suite-pretooluse; rc=$?"))
        for cmd in ("/x/bin/evil-fab-suite-pretooluse --hook-json",
                    "run nothelm-inject --hook-json",
                    "echo helminject --hook-json"):
            self.assertFalse(hooks._ours(cmd), cmd)
        # AND THE WIDTH THAT STAYS, asserted so a later "tightening" cannot
        # remove it silently: the phrase token `inject --hook-json` is wide
        # BY DESIGN — it exists to recognise hand-wired helm inject commands
        # whatever the executable is spelled like (a path, a wrapper, a
        # rename), so any command carrying the phrase ON WORD BOUNDARIES is
        # ours even when the word before it is not "helm". The boundary cure
        # excludes gluing, never phrase context.
        self.assertTrue(hooks._ours("mytool inject --hook-json"))

    def test_the_absolute_path_call_form_is_ours(self):
        # A leading slash stays a boundary: rendered entries call the guard
        # by absolute path, and orphaning them would un-own every real
        # install in the estate.
        self.assertTrue(hooks._ours(
            "timeout 3 /home/x/.local/bin/fab-suite-pretooluse; rc=$?"))

    def test_owned_specs_does_not_claim_a_lookalike_entry(self):
        spec = next(s for s in hooks.SPECS if s.get("external"))
        entry = lambda cmd: {"hooks": {spec["event"]: [
            {"matcher": "Bash",
             "hooks": [{"type": "command", "command": cmd}]}]}}
        self.assertNotIn(
            spec["name"],
            hooks._owned_specs(entry("timeout 3 /x/fab-suite-pretooluse-old")))
        # MUST-HIT twin, same shape minus the suffix: the census still sees
        # the real entry, so the assertNotIn above measured the boundary.
        self.assertIn(
            spec["name"],
            hooks._owned_specs(entry("timeout 3 /x/fab-suite-pretooluse")))


class SidechainBeaconGuardInstallTest(HooksBase):
    """task/2542 door (1), the half that decides whether the guard ever runs.

    Before this, PreToolUse was installed for matcher `Bash` only, so a
    Monitor call reached no helm hook at all, and Monitor is the tool a seat
    arms its beacon with. These arms read the settings.json the REAL installer
    wrote into a temp config dir, never the SPECS constant: a spec that says
    Monitor and a file that does not is the gap this lane closes."""

    GUARD = "chat argv-guard --hook-json"

    def guard_matchers(self, d):
        groups = self.read_settings(d).get("hooks", {}).get("PreToolUse", [])
        return [g.get("matcher") for g in groups
                if any(self.GUARD in str(h.get("command"))
                       for h in g.get("hooks") or [])]

    @staticmethod
    def covers(matcher, tool):
        """Claude Code reads a matcher as a pattern over the tool name."""
        return bool(matcher) and re.fullmatch(matcher, tool) is not None

    def assert_guards_monitor(self, d):
        matchers = self.guard_matchers(d)
        self.assertEqual(len(matchers), 1, matchers)
        self.assertTrue(self.covers(matchers[0], "Bash"), matchers)
        self.assertTrue(self.covers(matchers[0], "Monitor"), matchers)
        # task/2566: a workflow file lands through Write/Edit too
        self.assertTrue(self.covers(matchers[0], "Write"), matchers)
        self.assertTrue(self.covers(matchers[0], "Edit"), matchers)
        # the agent-model rung: an Agent call reaches the guard, and the
        # Workflow tool, which spawns without an Agent call, does not
        self.assertTrue(self.covers(matchers[0], "Agent"), matchers)
        self.assertFalse(self.covers(matchers[0], "Workflow"), matchers)
        self.assertFalse(self.covers(matchers[0], "Read"), matchers)

    def test_hooks_install_writes_a_guard_that_fires_on_monitor(self):  # noqa: VACUOUS_ASSERTION — assert_guards_monitor requires exactly one produced guard group and a matcher that covers Bash and Monitor
        d = self.mk_home("a-user-example")
        rc, out, err = self.run_hooks(["install"])
        self.assertEqual(rc, 0, out + err)
        self.assert_guards_monitor(d)

    def test_a_seat_config_dir_gets_the_same_guard(self):
        d = self.mk_seat("codex")
        self.assertEqual(hooks.install_home(d, specs=hooks.SEAT_SPECS)[0], "add")
        self.assert_guards_monitor(d)

    def test_reinstall_upgrades_a_seat_whose_guard_matched_bash_only(self):
        """Every seat installed before this lane carries the Bash-only group.
        `helm hooks install` is the documented repair, so it must widen that
        group in place rather than report it up to date or add a second one."""
        spec = next(s for s in hooks.SEAT_SPECS if s["name"] == "argv-guard")
        old = hooks._canonical_entry(dict(spec, matcher="Bash"))
        d = self.mk_seat("codex", settings={"hooks": {"PreToolUse": [old]}})
        self.assertEqual(self.guard_matchers(d), ["Bash"])   # control
        hooks.install_home(d, specs=hooks.SEAT_SPECS)
        self.assert_guards_monitor(d)

    def test_a_config_whose_guard_omits_agent_is_flagged_and_then_widened(self):  # noqa: VACUOUS_ASSERTION — each absence after install is read against the unconditional assertIn on the same two audits over the same config before it
        """A matcher change reaches a config only when its hooks are rendered
        again, so every config installed before Agent joined carries the
        four-tool group. The audits must name it, or the estate reads covered
        while no Agent call reaches the guard; `helm hooks install` must then
        widen it in place."""
        spec = next(s for s in hooks.SEAT_SPECS if s["name"] == "argv-guard")
        four = "Bash|Monitor|Write|Edit"
        self.assertNotEqual(spec["matcher"], four)
        old = hooks._canonical_entry(dict(spec, matcher=four))
        d = self.mk_seat("codex", settings={"hooks": {"PreToolUse": [old]}})
        self.assertEqual(self.guard_matchers(d), [four])        # control
        self.assertIn("argv-guard",
                      hooks._gap_row("s", d, hooks.SEAT_SPECS)["missing"])
        self.assertIn("argv-guard",
                      hooks.stale_specs(self.read_settings(d),
                                        hooks.SEAT_SPECS))
        hooks.install_home(d, specs=hooks.SEAT_SPECS)
        self.assert_guards_monitor(d)
        self.assertNotIn("argv-guard",
                         hooks._gap_row("s", d, hooks.SEAT_SPECS)["missing"])
        self.assertNotIn("argv-guard",
                         hooks.stale_specs(self.read_settings(d),
                                           hooks.SEAT_SPECS))


class ExecutableIsNotLaunchableTest(HooksBase):
    """Dispatch 70524837 (a second-round finding underneath):
    a mode-0755 script whose `#!` interpreter is gone passed isfile + X_OK,
    read as OK, was provisioned everywhere — and the kernel refused the exec
    at the only moment that mattered, as a 127 the rc-2-only wrapper turned
    into ALLOW."""

    def _pin(self, body, mode=0o755, name="launch-guard"):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, mode)
        os.environ["HELM_SUITE_GUARD"] = p
        return p

    def test_a_0755_script_with_a_dead_interpreter_is_not_ok(self):
        spec = next(s for s in hooks.SPECS if s.get("external"))
        # MUST-HIT first: the same script under a live interpreter IS ok, so
        # the refusal below measures the interpreter check, not the fixture.
        self._pin("#!/bin/sh\nexit 0\n")
        self.assertEqual(hooks.external_status(spec)[1], "ok")
        self._pin("#!%s/no-such-interp\nexit 0\n" % self.tmp)
        self.assertEqual(hooks.external_status(spec),
                         (None, "interpreter-missing"))
        msg = hooks.external_gap_message(spec)
        self.assertIn("chmod is not the cure", msg)
        self.assertIn("no-such-interp", msg)

    def test_the_env_form_is_UNJUDGED_not_resolved_through_our_PATH(self):
        """`env` resolves its target against the PATH OF THE PROCESS THAT
        EXECS IT, and a hook runs under a PATH this code cannot see.

        Judging it with `which` here is wrong in BOTH directions: it reports
        interpreter-missing for a tool the hook context WOULD find, and
        reports ok for one it would not. Neither is evidence, so the honest
        answer is no verdict. This arm previously pinned the which-based
        judgement, which is the behaviour being overturned.
        """
        spec = next(s for s in hooks.SPECS if s.get("external"))
        self._pin("#!/usr/bin/env definitely-absent-tool-8f3a2c\nexit 0\n")
        # THIS ARM USED TO ASSERT "ok" AND THAT WAS THE DEFECT IT NOW PINS.
        # Unjudged is not a synonym for measured-fine: reporting it as ok made
        # a launch nobody could measure indistinguishable from one helm proved,
        # on the surface whose only job is to say whether the guard can run.
        self.assertEqual(hooks.external_status(spec)[1], hooks.LAUNCH_UNJUDGED,
                         "an env-form shebang was judged against OUR PATH, or "
                         "an unmeasured launch was reported as ok")
        # AND THE PATH SURVIVES. Refusing to resolve it would disarm every host
        # whose guard carries an ordinary env shebang, which is most of them.
        self.assertEqual(hooks.external_status(spec)[0],
                         os.environ["HELM_SUITE_GUARD"])
        # POSITIVE CONTROL on the same observable: an ABSOLUTE interpreter is
        # still judged, so 'ok' above is about env-form and not about a
        # verdict this code stopped producing altogether.
        self._pin("#!/definitely/absent/interp-8f3a2c\nexit 0\n")
        self.assertEqual(hooks.external_status(spec)[1], "interpreter-missing",
                         "an ABSOLUTE missing interpreter stopped being judged")

    def test_an_env_form_naming_NO_interpreter_is_still_a_gap(self):
        """MUST-MISS: unjudged is not silence. `#!/usr/bin/env` alone names
        nothing, and that is readable from the bytes without any PATH."""
        spec = next(s for s in hooks.SPECS if s.get("external"))
        self._pin("#!/usr/bin/env\nexit 0\n")
        self.assertEqual(hooks.external_status(spec)[1],
                         hooks.LAUNCH_SHEBANG_UNLAUNCHABLE)

    def test_no_shebang_is_UNJUDGED_and_no_longer_certified_ok(self):
        """THIS ARM USED TO ASSERT "ok" AND THAT WAS THE DEFECT.

        A first line that is not `#!` is still not a GAP — refusals ride a
        measured contradiction — but it is not a CLEARANCE either. The branch
        certified every non-script on the strength of its mode bits, which is
        the same over-claim the tri-state exists to end (a second
        addendum). A non-ELF file with no shebang is UNJUDGED: helm has no way
        to know what the kernel will do with it.
        """
        spec = next(s for s in hooks.SPECS if s.get("external"))
        self._pin("exit 0\n")
        self.assertEqual(hooks.external_status(spec)[1], hooks.LAUNCH_UNJUDGED)
        # AND THE PATH SURVIVES: unjudged is not a refusal, so install still
        # writes the entry and no host is disarmed by the honesty.
        self.assertEqual(hooks.external_status(spec)[0],
                         os.environ["HELM_SUITE_GUARD"])

    def test_mode_bits_off_still_reads_pin_dead_not_interpreter(self):
        # MUST-MISS for the new state: it may not swallow its neighbour — a
        # file that is not executable stays pin-dead, whatever its shebang
        # says, because chmod IS the cure there.
        spec = next(s for s in hooks.SPECS if s.get("external"))
        self._pin("#!%s/no-such-interp\nexit 0\n" % self.tmp, mode=0o644)
        self.assertEqual(hooks.external_status(spec)[1], "pin-dead")

    def test_a_present_but_unexecutable_interpreter_asks_for_chmod(self):
        """MUST-MISS on the guidance, not just the verdict: this case and
        interpreter-missing used to share one reason key AND one sentence, and
        that sentence told every reader "chmod is not the cure" — precisely
        wrong for the file whose only defect is its mode bits."""
        import tempfile
        d = tempfile.mkdtemp()
        interp = os.path.join(d, "interp")
        with open(interp, "wb") as f:
            f.write(b"#!/bin/sh\nexit 0\n")
        os.chmod(interp, 0o644)          # present, NOT executable
        spec = next(s for s in hooks.SPECS if s.get("external"))
        self._pin("#!%s\nexit 0\n" % interp)
        self.assertEqual(hooks.external_status(spec)[1],
                         hooks.LAUNCH_INTERPRETER_NOT_EXEC)
        msg = hooks.external_gap_message(spec)
        self.assertIn("chmod +x", msg)
        self.assertNotIn("chmod is not the cure", msg)
        # POSITIVE CONTROL on the same observable: chmod it and the same file
        # resolves, so the refusal above is about the mode bits and not about
        # a fixture this arm broke some other way.
        os.chmod(interp, 0o755)
        self.assertEqual(hooks.external_status(spec)[1], "ok")

    def test_an_interpreter_that_is_itself_a_broken_script_is_a_gap(self):
        """The kernel walks the #! chain and so must this. A two-hop break
        used to read as fine: hop one is executable, and nothing looked at
        what hop one's own first line names."""
        import tempfile
        d = tempfile.mkdtemp()
        mid = os.path.join(d, "mid")
        with open(mid, "wb") as f:
            f.write(b"#!%s/no-such-interp-9c1\nexit 0\n" % d.encode())
        os.chmod(mid, 0o755)             # executable, and cannot itself launch
        spec = next(s for s in hooks.SPECS if s.get("external"))
        self._pin("#!%s\nexit 0\n" % mid)
        self.assertEqual(hooks.external_status(spec)[1],
                         hooks.LAUNCH_INTERPRETER_MISSING)
        self.assertIn("no-such-interp-9c1", hooks.external_gap_message(spec))
        # POSITIVE CONTROL: repair hop two and the whole chain resolves.
        with open(mid, "wb") as f:
            f.write(b"#!/bin/sh\nexit 0\n")
        self.assertEqual(hooks.external_status(spec)[1], "ok")

    def test_env_itself_is_judged_even_though_its_target_is_not(self):
        """Two questions, and the old code asked neither. env's TARGET
        resolves against a PATH helm cannot see and stays unjudged — but env
        ITSELF is the file the kernel opens, and an absolute spelling of it is
        measurable. A missing /usr/bin/env fails the exec before any PATH
        lookup happens."""
        spec = next(s for s in hooks.SPECS if s.get("external"))
        self._pin("#!/definitely/absent/env python3\nexit 0\n")
        self.assertEqual(hooks.external_status(spec)[1],
                         hooks.LAUNCH_INTERPRETER_MISSING,
                         "an absent env binary was excused as unjudged")
        # POSITIVE CONTROL on the same observable: a REAL env keeps the target
        # unjudged, so the refusal above is about the launcher, not about the
        # env form having stopped being unjudged altogether.
        self._pin("#!/usr/bin/env definitely-absent-tool-8f3a2c\nexit 0\n")
        self.assertEqual(hooks.external_status(spec)[1], hooks.LAUNCH_UNJUDGED)

    def test_unproven_launches_reports_what_unresolved_externals_cannot(self):
        """An unjudged launch keeps its path, so `unresolved_externals` — which
        reports only path-is-None — is structurally blind to it. That blindness
        is the reason the promotion to "ok" stayed invisible."""
        spec = next(s for s in hooks.SPECS if s.get("external"))
        self._pin("#!/usr/bin/env definitely-absent-tool-8f3a2c\nexit 0\n")
        specs = (spec,)
        self.assertEqual(hooks.unresolved_externals(specs), [])
        rows = hooks.unproven_launches(specs)
        self.assertEqual([r[1] for r in rows], [hooks.LAUNCH_UNJUDGED])
        self.assertIn("UNPROVEN", rows[0][2])
        # MUST-MISS: a MEASURED launch must not appear in this list, or the
        # reporter is just enumerating every external spec.
        self._pin("#!/bin/sh\nexit 0\n")
        self.assertEqual(hooks.unproven_launches(specs), [])




class OwnershipIsAPositionTest(unittest.TestCase):
    """A marker names OUR entry only in COMMAND position.

    Ownership is a LICENSE, not a label: status counts an owned entry as ours
    and sync REWRITES it. Substring-with-word-boundaries claimed any command
    whose PROSE or ARGUMENTS merely contained the marker, so a foreign hook
    that echoed the guard's name became helm's to edit.
    """

    # A NEUTRAL path on purpose: this repository is public and an
    # operator home directory is not ours to publish. Position is the
    # property under test; the specific prefix is irrelevant to it.
    GUARD = "/opt/fleet/checkout/bin/helm"

    def test_a_command_in_head_position_is_ours(self):
        """UNCONDITIONAL POSITIVE CONTROL: every refusal below is meaningless
        unless ownership still fires on the entries helm actually writes."""
        self.assertTrue(hooks._own_hit(
            self.GUARD + " chat deliver --hook-json", self.GUARD))

    def test_a_prelude_does_not_move_the_command(self):  # noqa: VACUOUS_ASSERTION — test_a_command_in_head_position_is_ours is this class's unconditional positive control on the same observable; the subTest loop iterates a literal tuple and cannot be empty
        for cmd in (
                "timeout 5 " + self.GUARD + " record --hook-json",
                "env FOO=1 " + self.GUARD + " record",
                "HELM_X=1 " + self.GUARD + " record",
                "nice -n 19 " + self.GUARD + " record"):
            with self.subTest(cmd=cmd):
                self.assertTrue(hooks._own_hit(cmd, self.GUARD))

    def test_a_marker_in_PROSE_is_not_ours(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is test_a_command_in_head_position_is_ours, asserting the SAME predicate returns True for a real entry
        self.assertFalse(hooks._own_hit(
            "echo 'run " + self.GUARD + " yourself'", self.GUARD),
            "a command that merely MENTIONS the guard was claimed, and "
            "ownership is a license to rewrite it")

    def test_a_marker_as_an_ARGUMENT_is_not_ours(self):  # noqa: VACUOUS_ASSERTION — see test_a_command_in_head_position_is_ours, the positive face of this predicate
        self.assertFalse(hooks._own_hit(
            "/usr/bin/other --path " + self.GUARD, self.GUARD),
            "a foreign command taking the guard as an ARGUMENT was claimed")

    def test_a_PHRASE_marker_still_works_in_command_position(self):
        """Markers are sometimes phrases spanning the command and its args,
        so anchoring must pin the START and let the match run on."""
        self.assertTrue(hooks._own_hit(
            "timeout 2 " + self.GUARD + " chat argv-guard --hook-json",
            "helm chat argv-guard"))

    def test_a_PHRASE_marker_in_prose_is_not_ours(self):  # noqa: VACUOUS_ASSERTION — its positive face is test_a_PHRASE_marker_still_works_in_command_position, same marker, command position
        self.assertFalse(hooks._own_hit(
            "echo run helm chat argv-guard now", "helm chat argv-guard"))

    def test_each_SEGMENT_is_judged_as_its_own_command(self):
        """A segment runs its own command, so a later segment can be ours
        even when the first is not."""
        self.assertTrue(hooks._own_hit(
            "true; " + self.GUARD + " record", self.GUARD))


class TypedOwnershipIdentityTest(unittest.TestCase):
    """Ownership derives from PARSED IDENTITY: which program ran, and what
    subcommand it was given.

    Ownership is a license, not a label — status counts an owned entry as
    ours and sync REWRITES it — so destructive authority must never come
    from a marker appearing somewhere in a command string.
    """

    GUARD = "/opt/fleet/checkout/bin/helm"

    def test_a_rendered_entry_is_ours(self):
        """UNCONDITIONAL POSITIVE CONTROL: every refusal below is meaningless
        unless ownership still fires on what the installer writes."""
        self.assertTrue(hooks._own_hit(
            "timeout 10 " + self.GUARD + " inject --hook-json || true",
            "inject --hook-json"))

    def test_a_subcommand_phrase_claims_under_any_executable(self):
        """WIDTH KEPT BY RULING: a hand-wired command may spell the binary as
        a path, a wrapper or a rename, and the subcommand is our own coinage.
        """
        self.assertTrue(hooks._own_hit("mytool inject --hook-json",
                                       "inject --hook-json"))

    def test_a_binary_phrase_must_BE_the_executed_word(self):
        """The half of the finding that was always right: a foreign program
        taking `helm chat deliver` as ARGUMENTS is not ours to rewrite."""
        self.assertFalse(hooks._own_hit(
            "/usr/local/bin/otherguard helm chat deliver --x",
            "helm chat deliver"))
        # and its positive face, same marker, executed position
        self.assertTrue(hooks._own_hit(
            "timeout 2 " + self.GUARD + " chat deliver --x",
            "helm chat deliver"))

    def test_a_phrase_in_the_MIDDLE_of_arguments_is_not_ours(self):  # noqa: VACUOUS_ASSERTION — its positive face is test_a_rendered_entry_is_ours in the same class, the same marker at the first argument
        self.assertFalse(hooks._own_hit(
            self.GUARD + " chat post --body 'inject --hook-json'",
            "inject --hook-json"))

    def test_prose_about_a_guard_is_not_ours(self):  # noqa: VACUOUS_ASSERTION — positive face: test_a_subcommand_phrase_claims_under_any_executable, same marker in executed position
        self.assertFalse(hooks._own_hit("echo 'run inject --hook-json now'",
                                        "inject --hook-json"))

    def test_a_separator_inside_quotes_starts_no_command(self):  # noqa: VACUOUS_ASSERTION — positive face: test_a_rendered_entry_is_ours, an unquoted command with the same guard path
        self.assertFalse(hooks._own_hit(
            "echo 'a; " + self.GUARD + " record'", self.GUARD))

    def test_an_external_guard_is_owned_by_its_NAME(self):
        """External guards are reached by explicit identity, not by width."""
        self.assertTrue(hooks._own_hit(
            "timeout 3 /x/bin/fab-suite-pretooluse; rc=$?",
            "fab-suite-pretooluse"))
        for twin in ("timeout 3 /x/bin/fab-suite-pretooluse-old; rc=$?",
                     "/x/bin/evil-fab-suite-pretooluse --hook-json"):
            with self.subTest(twin=twin):
                self.assertFalse(hooks._own_hit(twin, "fab-suite-pretooluse"))


class EventIsPartOfOwnershipIdentityTest(unittest.TestCase):
    """A marker plus an EVENT is the identity; a marker alone is a family."""

    GUARD = "/opt/fleet/checkout/bin/helm"
    SHARED = "timeout 5 /opt/fleet/checkout/bin/helm handoff check --hook-json"

    def _owned(self, **events):
        return hooks._owned_specs(
            {"hooks": {e: [{"hooks": [{"command": c}]}]
                       for e, c in events.items()}})

    def test_a_shared_marker_registers_only_its_own_event(self):  # noqa: VACUOUS_ASSERTION — the assertion is an exact list equality against a NAMED spec, not an absence; its positive face is test_both_events_wired_registers_both
        """PreCompact and SessionEnd SHARE `handoff check --hook-json`, so a
        file carrying half the pair reported the whole pair as installed —
        and a repair that reads that answer believes nothing is missing."""
        self.assertEqual(["handoff-precompact"],
                         self._owned(PreCompact=self.SHARED))

    def test_both_events_wired_registers_both(self):  # noqa: VACUOUS_ASSERTION — this IS the class's unconditional positive control: an exact two-element list equality, which no inert scan can satisfy
        """UNCONDITIONAL POSITIVE CONTROL: scoping by event must not stop
        recognising the pair when the pair is genuinely there."""
        self.assertEqual(["handoff-precompact", "handoff-sessionend"],
                         self._owned(PreCompact=self.SHARED,
                                     SessionEnd=self.SHARED))

    def test_the_marker_under_a_FOREIGN_event_registers_nothing(self):  # noqa: VACUOUS_ASSERTION — its positive face is test_both_events_wired_registers_both above, the same call on the same marker under the events that own it
        self.assertEqual([], self._owned(Stop=self.SHARED))


class UnreadableIsUnknownNotOkTest(unittest.TestCase):
    """Launchability has three answers, and helm-could-not-look is one."""

    def test_an_unreadable_shebang_is_UNKNOWN(self):
        """Returning no-gap reported a file nobody could read as fit to run:
        absence of evidence rendered as evidence of health, on the one
        surface whose whole job is whether the guard can execute."""
        import tempfile
        d = tempfile.mkdtemp()
        unreadable = os.path.join(d, "isdir")
        os.mkdir(unreadable)
        verdict, key, detail = hooks._interpreter_launch(unreadable)
        self.assertEqual(verdict, hooks.LAUNCH_UNJUDGED,
                         "an unreadable guard read as launchable")
        self.assertEqual(key, hooks.LAUNCH_UNJUDGED)
        self.assertIn("UNKNOWN", detail)
        # AND IT IS NOT A GAP EITHER. `_interpreter_gap` collapses UNJUDGED to
        # None by design, which is exactly why nothing that decides whether a
        # guard is provisioned may call it — pinned here so that coercion can
        # never quietly come back as the contract.
        self.assertIsNone(hooks._interpreter_gap(unreadable))

    def test_a_readable_healthy_shebang_is_still_no_gap(self):  # noqa: VACUOUS_ASSERTION — this IS the positive control for test_an_unreadable_shebang_is_UNKNOWN beside it: it proves the function does not answer UNKNOWN for everything
        """UNCONDITIONAL POSITIVE CONTROL on the same observable: the
        function must not answer UNKNOWN for everything."""
        import tempfile
        d = tempfile.mkdtemp()
        fine = os.path.join(d, "fine")
        with open(fine, "wb") as f:
            f.write(b"#!/bin/sh\nexit 0\n")
        os.chmod(fine, 0o755)
        self.assertIsNone(hooks._interpreter_gap(fine))
        self.assertEqual(hooks._interpreter_launch(fine)[0], hooks.LAUNCH_OK)


class OwnershipReadsCommandsNotProseTest(unittest.TestCase):
    """Ownership is a LICENSE — status counts an owned entry as ours and sync
    REWRITES it — so every way text can LOOK like a command without being one
    is a way for foreign prose to license a rewrite.

    THE MARKERS HERE ARE READ OUT OF SPECS, NOT INVENTED. My first pass at
    these arms used a marker I made up (`chat wait`), reported a failure, and
    nearly drove a change to satisfy an instrument measuring nothing real.
    Every phrase marker helm actually ships ends in `--hook-json`, our own
    coinage, and that is WHY the subcommand-anchored rule can afford to be
    executable-agnostic."""

    @staticmethod
    def _phrase_marker():
        for spec in hooks.SPECS:
            for tok in spec.get("own") or ():
                if " " in tok and tok.startswith("chat "):
                    return tok
        raise AssertionError("no phrase marker in SPECS — this arm is blind")

    def test_a_marker_inside_a_heredoc_body_is_data_not_a_command(self):
        """The one that matters most on this repo: helm's own verbs are
        invoked with quoted-delimiter heredocs constantly, so a dispatch body
        or chat post MENTIONING a guard command was read as executing it."""
        m = self._phrase_marker()
        body = "helm dispatch send a b <<'EOF'\nrun /opt/g/helm %s\nEOF" % m
        self.assertFalse(hooks._own_hit(body, m),
                         "a heredoc BODY licensed a rewrite")
        # MUST-HIT on the same observable: the identical command OUTSIDE a
        # heredoc still claims, so the False above is about the heredoc and
        # not about a marker that stopped matching anything.
        self.assertTrue(hooks._own_hit("/opt/g/helm %s" % m, m))

    def test_an_escaped_separator_starts_no_command(self):
        m = self._phrase_marker()
        self.assertFalse(hooks._own_hit(r"echo a\; /opt/g/helm " + m, m))

    def test_a_comment_starts_no_command(self):
        m = self._phrase_marker()
        self.assertFalse(hooks._own_hit("ls # ; /opt/g/helm " + m, m))
        # MUST-MISS on the boundary rule itself: `foo#bar` is ONE WORD, not a
        # comment, so a '#' that is not at a word boundary must not blind the
        # parser to the rest of the line.
        self.assertTrue(hooks._own_hit("a#b ; /opt/g/helm " + m, m))

    def test_a_marker_must_end_at_a_token_boundary(self):
        """A plain startswith has no end, so a marker claimed a LONGER
        subcommand nobody wrote."""
        m = self._phrase_marker()
        self.assertFalse(hooks._own_hit("/opt/g/helm %sx" % m, m))
        self.assertTrue(hooks._own_hit("/opt/g/helm %s --follow" % m, m))

    def test_every_declared_prelude_word_is_actually_stepped_over(self):
        """THE DECLARATION IS THE ORACLE, not a list I retype here. The set
        named six words and the code hand-checked three, so `exec`, `command`
        and `builtin` were never stepped over and every entry behind one read
        as UNOWNED. Deriving the cases from the regex means a word added to
        the set with no handler goes red instead of silently unowning."""
        m = self._phrase_marker()
        # DERIVED FROM THE TABLE, NEVER RETYPED. The literal list this arm
        # used to carry was itself a second declaration that could drift from
        # the code — the exact defect the arm exists to catch, one layer up,
        # and it reddened the moment the table legitimately changed.
        declared = sorted(hooks._PRELUDE)
        self.assertTrue(declared, "the prelude table is empty")
        for word in declared:
            self.assertTrue(hooks._PRELUDE_WORD.match(word),
                            "%s is in the table and not in the regex" % word)
        for word in declared:
            # DERIVED FROM THE ROW, not retyped: a prelude that takes N
            # operands needs N filler words before the program, or the walk
            # steps past the program and the arm measures the wrong token.
            arg = ({"nice": "-n 5 ", "env": "FOO=1 "}.get(word, "")
                   + "5 " * hooks._PRELUDE[word]["operands"])
            with self.subTest(prelude=word):
                self.assertTrue(
                    hooks._own_hit("%s %s/opt/g/helm %s" % (word, arg, m), m),
                    "%s is declared a prelude and was not stepped over" % word)

    def test_a_leading_assignment_must_be_a_shell_NAME(self):
        """Every word stepped over hands the executed position to the next
        one, which is the defect the closed prelude set exists to prevent —
        arriving through the other door."""
        m = self._phrase_marker()
        self.assertTrue(hooks._own_hit("FOO=1 /opt/g/helm " + m, m))
        self.assertFalse(hooks._ASSIGNMENT.match("a-b=1"))
        self.assertFalse(hooks._ASSIGNMENT.match("2=3"))
        self.assertFalse(hooks._ASSIGNMENT.match("=x"))
        self.assertTrue(hooks._ASSIGNMENT.match("_A9=x"))

    def test_a_pin_containing_a_space_survives_the_split(self):
        """str.split truncated an absolute path under a directory with a
        space, so the executed word came out wrong and the entry could never
        be owned. shlex answers the question the shell answers."""
        m = self._phrase_marker()
        self.assertTrue(hooks._own_hit('"/opt/my guard/helm" ' + m, m))

    def test_an_unbalanced_quote_yields_no_executed_word(self):
        """A string no shell would run must not produce a guess. MUST-MISS
        for the lossy fallback I considered and rejected."""
        self.assertEqual(hooks._executed('"/opt/g/helm chat'), ("", []))

    def test_a_foreign_executable_taking_our_phrase_as_arguments_is_not_ours(self):
        """BINARY-ANCHORED, and this is the destructive-authority case: the
        marker's head names a program we ship, so it must BE the executed
        word."""
        binmark = next(t for spec in hooks.SPECS
                       for t in (spec.get("own") or ())
                       if t.startswith("helm "))
        self.assertFalse(hooks._own_hit("/usr/bin/foreign " + binmark, binmark))
        self.assertTrue(hooks._own_hit(
            "/opt/g/helm " + binmark.split(" ", 1)[1], binmark))


class WrapperGrammarIsDataNotAGuessTest(unittest.TestCase):
    """Each prelude word has its OWN option grammar, and one "skip the
    dash-words" rule was wrong for every entry in a different way.

    THE DIRECTION MATTERS AND IT IS NOT WHAT IT LOOKS LIKE. Three of these
    were FALSE NEGATIVES — a valid hand-wired entry read as UNOWNED — and an
    unowned entry does not merely go unmaintained, it gets a canonical
    duplicate APPENDED beside it. The parser was quietly manufacturing second
    hooks. The fourth is the false-positive twin: `command -v` PRINTS a path
    and never runs it, so treating it as transparent claimed a command that
    does not execute."""

    MARKER = _DELIVER_MARKER

    def owned(self, cmd):
        return hooks._own_hit(cmd, self.MARKER)

    def test_every_wrapper_form_finds_the_real_command(self):
        # MEASURED BEFORE THE CURE, and each line is a bug that shipped:
        #   timeout -s TERM 5 cmd  -> executed word was "5"
        #   nice cmd               -> executed word was "chat" (ate the command)
        #   env -u FOO cmd         -> executed word was "FOO"
        for name, cmd in (
                ("timeout with an option operand",
                 "timeout -s TERM 5 /opt/g/helm " + self.MARKER),
                ("timeout with only a duration",
                 "timeout 5 /opt/g/helm " + self.MARKER),
                ("nice with NO numeric operand",
                 "nice /opt/g/helm " + self.MARKER),
                ("nice with one", "nice -n 5 /opt/g/helm " + self.MARKER),
                ("env unsetting a name",
                 "env -u FOO /opt/g/helm " + self.MARKER),
                ("env with assignments",
                 "env -i FOO=1 /opt/g/helm " + self.MARKER),
                ("exec renaming argv0",
                 "exec -a nm /opt/g/helm " + self.MARKER)):
            with self.subTest(form=name):
                self.assertTrue(self.owned(cmd),
                                "a valid entry read as UNOWNED, which appends "
                                "a duplicate hook beside it")

    def test_command_dash_v_PRINTS_and_therefore_owns_nothing(self):
        """MUST-MISS, and the twin of the arm above: transparency is about
        what RUNS, not about what a word looks like."""
        self.assertFalse(
            self.owned("command -v /opt/g/helm " + self.MARKER),
            "`command -v` prints a path; nothing was executed to own")
        # POSITIVE CONTROL on the same observable: without -v it DOES run.
        self.assertTrue(self.owned("command /opt/g/helm " + self.MARKER))

    def test_the_grammar_table_and_the_regex_cannot_drift(self):
        """The regex used to name six words while the code handled three. Both
        are derived from ONE table now, so a word added with no grammar cannot
        silently become a transparent wrapper."""
        for word in hooks._PRELUDE:
            with self.subTest(prelude=word):
                self.assertTrue(hooks._PRELUDE_WORD.match(word))
        # `command` IS in the table — it genuinely runs what follows — and its
        # printing forms are refused by not_transparent_opts instead. Removing
        # the word outright was my first cut and my OWN positive control
        # reddened on it, because that unowns the bare form too.
        self.assertIn("command", hooks._PRELUDE)
        self.assertIn("-v", hooks._PRELUDE["command"]["not_transparent_opts"])


class ExecutableIsNotLaunchableEvenWithoutAShebangTest(unittest.TestCase):
    """The no-shebang branch certified every non-script as OK on the strength
    of its mode bits — the same over-claim the tri-state exists to end, one
    branch away."""

    def test_an_ELF_whose_loader_is_missing_is_a_GAP(self):
        import shutil
        import tempfile
        real = shutil.which("true") or "/bin/true"
        # MUST-HIT FIRST, on a REAL binary: a healthy dynamic ELF is OK, so the
        # gap below is about the corrupted loader and not about a parser that
        # condemns everything.
        self.assertEqual(hooks._interpreter_launch(real)[0], hooks.LAUNCH_OK)
        d = tempfile.mkdtemp()
        broken = os.path.join(d, "broken")
        shutil.copy(real, broken)
        os.chmod(broken, 0o755)
        raw = bytearray(io.open(broken, "rb").read())
        for needle in (b"/lib64/ld-linux-x86-64.so.2", b"/lib/ld-linux"):
            at = raw.find(needle)
            if at >= 0:
                raw[at:at + len(needle)] = (
                    b"/nope/ld-missing.so" + b"\x00" * (len(needle) - 19))
                break
        else:
            self.skipTest("no PT_INTERP string found to corrupt on this host")
        io.open(broken, "wb").write(bytes(raw))
        verdict, key, detail = hooks._interpreter_launch(broken)
        self.assertEqual(verdict, hooks.LAUNCH_GAP)
        self.assertEqual(key, hooks.LAUNCH_INTERPRETER_MISSING)
        self.assertIn("ld-missing.so", detail)

    def test_the_KERNEL_is_the_authority_and_it_agrees(self):
        """Q4 OF THE MELD, AND THE ANSWER IS A DIFFERENT DISEASE THAN Q3'S.

        A review asked whether "tests derive the same grammar" reaches the
        launch arms the way it reached the wrapper arms. It does not reach them
        the SAME way: there is no knowledge table here to iterate, because
        `_interpreter_launch` implements kernel semantics directly rather than
        consulting a list I wrote. `hooks.LAUNCH_GAP` is an enum name, not a
        claim about the world, so asserting against it is not the Q3 defect.

        BUT THERE IS A REAL INDEPENDENCE GAP ONE STEP OVER, and this closes it.
        Every launch arm compares helm's verdict to MY BELIEF about what the
        kernel does with that file. Nothing has ever asked the kernel. So this
        arm builds the same three files and EXECS them, then requires the two
        answers to correspond:

            helm says OK   -> execve must actually succeed
            helm says GAP  -> execve must actually fail

        UNJUDGED is exempt in BOTH directions and that is the point of having
        it: helm declining to judge is honest whatever the kernel does, so the
        arm asserts only that an unjudged file was not called OK.

        The expectation here comes from execve, not from helm's model of it.
        If `_elf_launch` ever misreads a program header the same way in both
        the code and my head, this is the arm that goes red."""
        import shutil
        import tempfile

        def kernel_starts(path):
            """(started?, why) — from execve itself, nothing else."""
            try:
                subprocess.run([path], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=10)
                return True, "execve succeeded"
            except OSError as exc:
                return False, "execve refused: %s" % exc

        real = shutil.which("true") or "/bin/true"
        d = tempfile.mkdtemp()
        # MALFORMED ELF — THE HOLE IN THIS ARM'S OWN POPULATION. Four bytes of
        # magic and 60 NULs is executable, parses to no program headers, and
        # execve answers ENOEXEC. The old parser fell through to its static-ELF
        # return and certified it LAUNCH_OK. This arm existed precisely to
        # catch a verdict the kernel contradicts, and its case list did not
        # include the case: valid dynamic, corrupted loader, plain non-ELF —
        # and nothing that LOOKED like an ELF without being one.
        malformed = []
        for tag, blob in (("magic-and-nuls", b"\x7fELF" + b"\x00" * 60),
                          ("magic-only", b"\x7fELF"),
                          ("bad-ei-class", b"\x7fELF\x09\x01" + b"\x00" * 58)):
            m = os.path.join(d, tag)
            io.open(m, "wb").write(blob)
            os.chmod(m, 0o755)
            malformed.append(m)
        broken = os.path.join(d, "broken")
        shutil.copy(real, broken)
        os.chmod(broken, 0o755)
        raw = bytearray(io.open(broken, "rb").read())
        for needle in (b"/lib64/ld-linux-x86-64.so.2", b"/lib/ld-linux"):
            at = raw.find(needle)
            if at >= 0:
                raw[at:at + len(needle)] = (
                    b"/nope/ld-missing.so" + b"\x00" * (len(needle) - 19))
                break
        else:
            self.skipTest("no PT_INTERP string found to corrupt on this host")
        io.open(broken, "wb").write(bytes(raw))
        plain = os.path.join(d, "plain")
        io.open(plain, "wb").write(b"not an elf and no shebang\n")
        os.chmod(plain, 0o755)

        # ONE PASS, ONE TABLE, so the probe's must-hit and the correspondence
        # claim are read off the SAME rows rather than off two calls that
        # merely look alike.
        checks = []
        for path, kernel_must in ([(real, True), (broken, False), (plain, None)]
                                  + [(m, False) for m in malformed]):
            started, why = kernel_starts(path)
            checks.append((path, hooks._interpreter_launch(path)[0],
                           started, why, kernel_must))

        # MUST-HIT ON THE INSTRUMENT ITSELF, unconditional and first: the probe
        # has to report BOTH answers, or its agreement with helm below is the
        # agreement of a function that always says yes. `plain` is excluded
        # here because errno 8 is what a SHELL would fall back from, not what
        # this arm is refereeing.
        # STATED AS A PROPERTY, NOT AS A LIST. The first version pinned the
        # literal [True, False] and went red the moment the population grew by
        # three malformed binaries — a control that breaks when you ADD a case
        # is pinned to the case list rather than to the thing it measures. The
        # property is that the probe returns BOTH answers somewhere in the
        # refereed set.
        refereed = [c[2] for c in checks if c[4] is not None]
        self.assertIn(True, refereed,
                      "the exec probe never reported a STARTED, so its "
                      "agreement with helm is the agreement of a function "
                      "that always refuses: %s" % [c[3] for c in checks])
        self.assertIn(False, refereed,
                      "the exec probe never reported a REFUSED, so it "
                      "referees nothing: %s" % [c[3] for c in checks])
        self.assertEqual(refereed, [c[4] for c in checks if c[4] is not None],
                         "the kernel disagreed with what each case DECLARES "
                         "it should do: %s" % [c[3] for c in checks])

        disagreed = ["%s: helm says %s, %s" % (path, verdict, why)
                     for path, verdict, started, why, _must in checks
                     if (verdict == hooks.LAUNCH_OK and not started)
                     or (verdict == hooks.LAUNCH_GAP and started)]
        self.assertEqual(disagreed, [], "\n".join(disagreed))

    def test_a_non_ELF_without_a_shebang_is_UNJUDGED_not_ok(self):
        """Not recognising a format has never been evidence that it runs."""
        import tempfile
        d = tempfile.mkdtemp()
        plain = os.path.join(d, "plain")
        io.open(plain, "wb").write(b"not an elf and no shebang\n")
        os.chmod(plain, 0o755)
        self.assertEqual(hooks._interpreter_launch(plain)[0],
                         hooks.LAUNCH_UNJUDGED)


class AMarkerIsATokenSequenceNotASubstringTest(unittest.TestCase):
    """shlex recovered the argument boundaries and the old code rejoined them
    with spaces, throwing away the one thing shlex was introduced for."""

    MARKER = _DELIVER_MARKER

    def test_one_quoted_argument_of_prose_owns_nothing(self):
        self.assertFalse(
            hooks._own_hit('/opt/g/helm "%s"' % self.MARKER, self.MARKER),
            "a single quoted argument whose TEXT contains the marker "
            "licensed a rewrite")
        # MUST-HIT on the same observable: the real two-token invocation.
        self.assertTrue(
            hooks._own_hit("/opt/g/helm " + self.MARKER, self.MARKER))

    def test_a_marker_still_ends_at_a_token_boundary(self):
        self.assertFalse(
            hooks._own_hit("/opt/g/helm chat deliver --hook-jsonx",
                           self.MARKER))


class HeredocTerminatorsFollowTheShellNotConvenienceTest(unittest.TestCase):
    """`<<` needs the terminator to stand alone; `<<-` strips TABS ONLY.

    The first cut used line.strip() for both and defended it as the forgiving
    direction. It is the opposite: ending a body EARLY hands the rest of the
    body to the command parser, which is the licensing path this closes."""

    MARKER = _DELIVER_MARKER

    def test_an_indented_terminator_under_plain_heredoc_does_not_end_it(self):
        cmd = "a <<'EOF'\nrun x\n   EOF\n/opt/g/helm " + self.MARKER
        self.assertFalse(hooks._own_hit(cmd, self.MARKER),
                         "an indented EOF ended a << body the shell keeps "
                         "reading, so data was parsed as commands")
        # MUST-HIT: the SAME command with a correct terminator IS owned, so
        # the refusal above is about indentation and not about a parser that
        # swallows everything.
        ok = "a <<'EOF'\nrun x\nEOF\n/opt/g/helm " + self.MARKER
        self.assertTrue(hooks._own_hit(ok, self.MARKER))

    def test_dash_heredoc_strips_tabs_only(self):
        tab = "a <<-EOF\nrun x\n\tEOF\n/opt/g/helm " + self.MARKER
        self.assertTrue(hooks._own_hit(tab, self.MARKER))
        spaces = "a <<-EOF\nrun x\n  EOF\n/opt/g/helm " + self.MARKER
        self.assertFalse(hooks._own_hit(spaces, self.MARKER),
                         "<<- strips TABS, never spaces")


class TheGrammarIsCheckedAgainstTheSHELLNotAgainstOurTableTest(unittest.TestCase):
    """The sharpest review line on this lane: "Tests derive the same grammar."

    Every other wrapper arm in this file iterates `hooks._PRELUDE` — the table
    under test — so it is structurally incapable of catching that table being
    WRONG. It can only catch the code disagreeing with the table. That is the
    same discriminator a review gave me on absence-invariant within the hour
    ("both guard and fixture derive from the same lexical name model"), found
    independently on a different lane, which is why it is a class and not a
    coincidence.

    SO THIS CLASS TOUCHES `_PRELUDE` NOWHERE. Every case is a literal command
    string with an expected answer derived from what a SHELL would do, written
    out so a reader can check each one against `man bash` rather than against
    helm. If our table is missing a word, or gives one the wrong option
    grammar, an arm here goes red while every table-derived arm stays green.
    """

    MARKER = _DELIVER_MARKER

    # (command, owned?, why — in shell terms, not helm terms)
    CASES = (
        ("command -pv /opt/g/helm %s", False,
         "-pv is a CLUSTER: -p and -v. -v makes command PRINT a path and run "
         "nothing, so no command executed and there is nothing to own"),
        ("command -v -p /opt/g/helm %s", False,
         "same, spelled as separate flags"),
        ("command -p /opt/g/helm %s", True,
         "-p only resets PATH; the program still RUNS"),
        ("command /opt/g/helm %s -v", True,
         "this -v is an ARGUMENT TO THE PROGRAM, not to command — the wrapper's "
         "options end at the first non-option word"),
        ("command -- /opt/g/helm %s", True,
         "-- ends command's options; what follows is the program"),
        ("timeout -s TERM 5 /opt/g/helm %s", True,
         "-s takes an OPERAND (TERM), then a DURATION, then the program"),
        ("timeout 5 /opt/g/helm %s", True, "duration then program"),
        ("nice /opt/g/helm %s", True,
         "nice takes NO bare operand; the next word is already the program"),
        ("nice -n 5 /opt/g/helm %s", True, "-n takes its operand"),
        ("env -u FOO /opt/g/helm %s", True, "-u consumes FOO as its operand"),
        ("env -i FOO=1 /opt/g/helm %s", True, "assignments then program"),
        ("exec -a nm /opt/g/helm %s", True, "-a consumes its argv0 operand"),
        ("/usr/bin/foreign %s", True,
         "SUBCOMMAND-anchored: the marker's head is not a program we ship, so "
         "it claims from the first argument whatever the executable is"),
        # UNMODELLED GRAMMAR — the shell agrees with the abstention here, so
        # these belong in this class: in every one, NO command begins at the
        # guard, so a shell runs nothing to own.
        ("((x && /opt/g/helm %s))", False,
         "inside (( )) the && is arithmetic AND, not a command separator — "
         "nothing after it is a command, so the guard never runs"),
        ("[[ -n x && /opt/g/helm %s ]]", False,
         "inside [[ ]] the && is a conditional operator; same reason"),
        ("echo >| /opt/g/helm %s", False,
         ">| redirects stdout to a FILE whose name happens to be the guard's "
         "path — nothing executes it"),
        ("cat <<< /opt/g/helm %s", False,
         "<<< takes a WORD as stdin DATA, not a command"),
        ("eval -X /opt/g/helm %s", False,
         "eval takes NO options: bash prints 'invalid option' and exits, so "
         "the guard never runs"),
        ("eval /opt/g/helm %s", True,
         "plain eval IS transparent — it execs what follows"),
        ("eval -- /opt/g/helm %s", True, "-- is a terminator, not an option"),
        ("echo 'a; /opt/g/helm %s'", False, "single-quoted: one word of prose"),
        ('echo "a # /opt/g/helm %s"', False, "double-quoted: still one word"),
    )

    def test_every_case_matches_what_a_shell_would_do(self):
        wrong = []
        for template, expected, why in self.CASES:
            cmd = template % self.MARKER
            got = hooks._own_hit(cmd, self.MARKER)
            if got != expected:
                wrong.append("%r -> %s, expected %s (%s)"
                             % (cmd, got, expected, why))
        self.assertEqual(wrong, [], "\n".join(wrong))

    # A NAME MARKER IS THE SHARP INSTRUMENT FOR "WAS THIS WORD STEPPED OVER?".
    # The phrase marker above anchors at the first ARGUMENT, so it answers True
    # for several wrong readings of the same string; a name marker claims ONLY
    # from the executed position, so it flips the moment a word is wrongly
    # stepped over or wrongly left in place.
    NAME_MARKER = "fab-suite-pretooluse"

    # (command, owned?, why — in shell terms)
    RESERVED = (
        ("if [ -x /opt/g/x ]; then %s; fi", True,
         "`;` ends the condition, so the second segment LEADS with `then` — "
         "which is grammar and never occupies the executed position"),
        ("while true; do %s; done", True, "`do` is grammar, same as `then`"),
        ("until false; do %s; done", True, "`until` heads a command, `do` is grammar"),
        ("! %s", True, "`!` negates the exit status of the command after it"),
        # DROPPED, on the parser author's ruling, and its
        # reason is the distinction neither this table nor the parser's prose
        # recorded: a brace group may EXECUTE the guard, but OWNERSHIP licenses
        # WHOLE-COMMAND REPLACEMENT, which would destroy the user's braces. So
        # `{ %s; }` is correctly NOT owned, and the expectation was the defect.
        # Left as a comment rather than deleted because the next composer will
        # otherwise ask the same question: the table and the parser's prose
        # disagreed, and from outside the car either half looked like a
        # defensible one-line fix.
        ("time %s", True, "the reserved word `time` RUNS the command after it"),
        ("time -p %s", True, "-p is the reserved word's only option"),
        # MUST-MISS. Each of these goes red if the reserved-word set is widened
        # to the words that merely LOOK transparent.
        ("for %s in a b; do :; done", False,
         "`for` is followed by a NAME, never a command — stepping over it "
         "hands a loop variable the executed position it never had"),
        ("case %s in *) :;; esac", False, "`case` is followed by a WORD to match, not a command"),
        ("select %s in a b; do break; done", False, "`select` is `for` with a menu"),
        ("bash -c %s", False,
         "bash RUNS; the script is its ARGUMENT, so bash is the executed word"),
        ("python3 -m %s", False, "python3 RUNS; the module is its argument"),
        ("npx %s", False, "npx RUNS; it resolves and launches a package"),
    )

    COMMAND_BEGIN = (
        ("( %s )", True,
         "a subshell RUNS what is inside it; `(` is grammar and never the "
         "executed word"),
        ("coproc %s", True,
         "`coproc CMD` runs CMD in a coprocess; the keyword is not the "
         "program"),
        ("( timeout 5 %s )", True,
         "grammar and a prelude word compose — both are stepped over"),
        ("coproc CO %s", False,
         "BASH RUNS `CO` HERE, NOT THE GUARD. `coproc [NAME] command` only has "
         "a NAME slot before a COMPOUND command, so with a SIMPLE command the "
         "first word is the program and the second is its argument. My first "
         "cure read this as naming CO and executing the guard — which grants "
         "ownership to a guard that never runs, and a FALSE owner suppresses "
         "the hook that would have provisioned a real one. Worse than the miss "
         "it replaced (@helm-codex, round two)"),
        ("coproc CO { %s; }", True,
         "THE REAL NAMED FORM: a compound body IS the NAME slot's grammar, so "
         "CO is a label and the guard inside runs. `{` is already grammar; the "
         "body's trailing `;` is stripped, since shlex does not split on it "
         "and a basename compare against `guard;` can never match"),
        ("> /dev/null %s", True,
         "A LEADING REDIRECTION IS GRAMMAR. `> file cmd` RUNS cmd; treating "
         "the operator as the executed word reads a running guard as unowned, "
         "which is the false negative that earns a duplicate hook"),
        ("2>&1 %s", True,
         "THE `&` IN A REDIRECTION IS NOT A SEPARATOR. `_segments` split this "
         "into `2>` and `1 <guard>`, so the executed word came out as `1`. The "
         "defect was in the SPLITTER, not the word scanner — one function "
         "further out than every previous round of this review"),
        ("&> log %s", True,
         "`&>` REDIRECTS BOTH STREAMS TO A FILE, so it consumes the next word. "
         "I first classed it self-contained like `2>&1` and its TARGET became "
         "the executed word — the same false-owner shape, arriving through my "
         "own fix for the shape above it"),
        ("eval %s", True,
         "`eval cmd` concatenates its arguments and RUNS the result, so the "
         "word after it is executed exactly as with `exec`. A quoted "
         "`eval \"$X\"` is unresolvable by construction and is the opaque "
         "path's business, not this set's"),
        ("(( %s ))", False,
         "SPACED ARITHMETIC TOKENIZES AS THREE WORDS. shlex yields `((guard))` "
         "as ONE word and `(( guard ))` as `((`, `guard`, `))` — so a skip of "
         "a SINGLE token handled the unspaced form and handed the executed "
         "position to the word INSIDE the spaced one, falsely owning a program "
         "arithmetic never runs. The skip spans to the closing `))`"),
        ("(( 1 + 2 )); %s", True,
         "SEPARATOR REQUIRED, and my first version of this arm did not have "
         "one. `(( expr )) cmd` is a SYNTAX ERROR — bash -n rejects it — so "
         "asserting the guard is owned there codified ownership in a command "
         "no shell would run. With the `;` it is two commands, the second runs "
         "the guard, and the arm still bounds what it was written to bound: "
         "the arithmetic skip must not swallow what follows it"),
        ("((%s))", False,
         "ARITHMETIC EVALUATION RUNS NO COMMAND AT ALL. Stripping its parens "
         "the way a subshell's are stripped turned this into the bare word and "
         "granted ownership to a program that never executed — the same "
         "false-owner harm as the coproc case above, arriving through the "
         "punctuation door (@helm-codex, round two)"),
        ("(%s)", True,
         "the unspaced subshell RUNS exactly what the spaced one runs. shlex "
         "yields it as a single token, so the parenthesis is stripped as the "
         "punctuation it is rather than enumerated as a keyword"),
    )

    def test_a_COMMAND_BEGIN_form_the_census_could_not_see(self):
        """Executed positions are NOT ownership of an entire shell line.

        The old test asked _own_hit about a shell-execution table. Five true
        execution cases deliberately abstain at the rewrite boundary, so that
        conflation demanded removing a safety guard. The actual ownership
        inventory (_owned_specs) and installer share _own_hit; neither is an
        execution census. Exercise the existing word parser here, not a new
        ownership rule, and retain all thirteen literal shell-derived cases.
        """
        self.assertEqual(len(self.COMMAND_BEGIN), 13)
        expected = [want for _template, want, _why in self.COMMAND_BEGIN]
        observed = [self._sees_executed_guard(template % self.NAME_MARKER)
                    for template, _want, _why in self.COMMAND_BEGIN]
        self.assertEqual(observed, expected)
        # Unconditional positive and negative controls on the same accessor.
        self.assertTrue(self._sees_executed_guard(self.NAME_MARKER))
        self.assertFalse(self._sees_executed_guard("echo " + self.NAME_MARKER))

    def _sees_executed_guard(self, command):
        return any(os.path.basename(hooks._executed(segment)[0]) == self.NAME_MARKER
                   for segment in hooks._segments(command))

    def test_all_COMMAND_BEGIN_expectations_match_an_actual_shell(self):
        """Bash executes an inert scratch binary; no parser result is its oracle.

        The marker is a file, not stdout: coproc redirects its child's stdout.
        Waiting for the child makes the observation independent of scheduling.
        Arithmetic, quoted/argument words, and a coprocess name cannot create
        the marker unless the shell really invokes our scratch executable.
        """
        bash, timeout = shutil.which("bash"), shutil.which("timeout")
        if not bash or not timeout:
            self.skipTest("shell oracle requires bash and timeout")
        self.assertEqual(len(self.COMMAND_BEGIN), 13)
        with tempfile.TemporaryDirectory(prefix="helm-command-begin-") as tmp:
            binary = os.path.join(tmp, self.NAME_MARKER)
            marker = os.path.join(tmp, "executed")
            with open(binary, "w", encoding="utf-8") as stream:
                stream.write('#!/bin/sh\nprintf "executed\\n" >> "$HOOK_ORACLE"\n')
            os.chmod(binary, 0o700)
            env = dict(os.environ, PATH=tmp + os.pathsep + os.path.dirname(timeout)
                       + os.pathsep + os.defpath, HOOK_ORACLE=marker)
            # Do not inherit shell startup scripts or exported shell functions.
            env = {key: value for key, value in env.items()
                   if key not in ("BASH_ENV", "ENV") and not key.startswith("BASH_FUNC_")}
            observed = []
            for template, expected, why in self.COMMAND_BEGIN:
                command = template % self.NAME_MARKER
                with self.subTest(command=command):
                    parsed = subprocess.run([bash, "--noprofile", "--norc", "-n", "-c", command],
                                            cwd=tmp, env=env, capture_output=True, timeout=5)
                    self.assertEqual(parsed.returncode, 0, "invalid shell fixture: " + why)
                    subprocess.run([bash, "--noprofile", "--norc", "-c", command + "\nwait\n"],
                                   cwd=tmp, env=env, capture_output=True, timeout=5)
                    ran = os.path.exists(marker)
                    observed.append(ran)
                    self.assertEqual(ran, expected, why)
                    if ran:
                        with open(marker, encoding="utf-8") as stream:
                            self.assertEqual(stream.read(), "executed\n")
                        os.unlink(marker)
            self.assertEqual(observed, [want for _command, want, _why in self.COMMAND_BEGIN])
            self.assertIn(True, observed)
            self.assertIn(False, observed)

    def test_reserved_words_are_grammar_and_lookalikes_are_not(self):
        """MEASURED POPULATION, not an imagined one. A census of every hook
        command on this host — 387 strings across 166 settings.json files,
        third-party repos included — puts `if` SECOND by frequency (17) behind
        `timeout` (136). `_segments` splits on `;`, so a real `if ...; then
        <our hook>; fi` entry arrives with `then` in the leading position and
        used to read as the executed word. The cost of that false negative is
        stated in `_executed` itself: an unowned entry earns a canonical
        duplicate appended beside a working hand-wired hook.

        The must-misses are the half that matters. `for`/`in`/`case`/`select`
        are followed by names and word lists rather than commands, and
        `bash`/`python3`/`npx` genuinely ARE the program that runs — reading
        any of them as transparent is the exact hazard the closed prelude set
        exists to prevent, arriving through the door marked "keywords are
        obviously transparent"."""
        # ONE COLLECTOR CALL CARRIES BOTH THE CLAIM AND ITS CONTROL. An empty
        # result reads identically whether every case passed or no case ran, so
        # a case asserted with the WRONG answer ON PURPOSE rides along: the
        # collector must report exactly it, and nothing else. A silent
        # collector fails the assertIn, and a broken case fails the count.
        planted = ("bash -c %s", True,
                   "WRONG ON PURPOSE: bash is the program that RUNS, so this "
                   "must not be owned — it is here to prove the collector "
                   "can still see a mismatch at all")
        checked = self._reserved_mismatches(self.RESERVED + (planted,))
        self.assertIn("bash -c", "".join(checked),
                      "the collector did not report the case planted to fail, "
                      "so its silence about the others proves nothing: %r"
                      % (checked,))
        self.assertEqual(len(checked), 1,
                         "only the planted case may mismatch:\n%s"
                         % "\n".join(checked))

    def _reserved_mismatches(self, cases):
        out = []
        for template, expected, why in cases:
            cmd = template % self.NAME_MARKER
            got = hooks._own_hit(cmd, self.NAME_MARKER)
            if got != expected:
                out.append("%r -> %s, expected %s (%s)"
                           % (cmd, got, expected, why))
        return out

    def test_this_class_derives_nothing_from_the_table_under_test(self):
        """THE ARM THAT KEEPS THIS CLASS HONEST. If a future edit makes these
        cases read `_PRELUDE`, the class silently rejoins the population it was
        written to escape and stops being an independent check."""
        import ast
        import inspect
        import textwrap
        src = inspect.getsource(type(self))
        # ASK THE AST WHAT THIS CLASS *READS*, NOT WHAT ITS PROSE MENTIONS.
        # My first cut was `assertNotIn("_PRELUDE", src)` and it went red on
        # its own docstring — the paragraph EXPLAINING that the class must not
        # derive from the table contains the table's name, as does the list of
        # names being checked. That is a string scan standing in for a
        # semantic property, which is the fourth time today I have written one:
        # the guard must look at attribute ACCESS, not at text.
        tree = ast.parse(textwrap.dedent(src))
        read = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name) and n.value.id == "hooks"}
        # MUST-HIT: the walk has to have SEEN this class reach into hooks at
        # all, or an empty set agrees with every assertion below.
        self.assertIn("_own_hit", read,
                      "the AST walk found no hooks access — it is broken, so "
                      "its verdict about the table proves nothing")
        self.assertEqual(
            read & {"_PRELUDE", "_OWN_EXECUTABLES", "_ASSIGNMENT",
                    "_PRELUDE_WORD", "_KEYWORD", "SPECS"}, set(),
            "this class reads the very table it exists to check "
            "independently: %s" % sorted(read))


if __name__ == "__main__":
    unittest.main()


class AdvisoryAnnouncesItsFailure(HooksBase):
    """An advisory hook that could not run must SAY so — on both channels.

    `|| true` was the whole advisory branch, and it rewrote rc 124 and rc 127
    to success. A `timeout` KILL prints nothing, so a lane hook that never ran
    was indistinguishable from one that ran clean. docs/HOOKS.md records that
    exact outage for the GATES on 2026-08-04; the cure stopped at the three
    gates and left the other eight silent for the same reason. On 2026-08-27
    the fleet spent hours on seats that were simply unreachable.
    """

    def _drive(self, cmd, rc):
        """Run `cmd` with its helm replaced by a stub exiting `rc`, and the
        SHIPPED wrapper beside that stub.

        SUBSTITUTION BY OPERAND POSITION, not by regex over the whole string.
        `_wrapper_prefix` fixes five operands before the child, so words[6] is
        the program and words[0] is the wrapper; a pattern match over the text
        once ate a `;` and turned the ladder's own `rc=$?` into an argument,
        and the harness then reported a working wrapper as broken."""
        words = shlex.split(cmd)
        # MUST-HIT: the shape the substitution assumes is the shape helm
        # renders. A wrapper invocation this cannot read is a harness bug, not
        # a finding about the ladder.
        self.assertIn(words[1], ("gate", "lane", "posttool"), cmd)
        d = tempfile.mkdtemp(dir=self.tmp)
        stub = os.path.join(d, "helm")
        with open(stub, "w") as f:
            f.write("#!/bin/sh\nexit %d\n" % rc)
        os.chmod(stub, 0o755)
        self.wrapper_beside(stub)
        words[0] = os.path.join(d, hooks.HOOK_WRAPPER)
        words[6] = stub
        c = " ".join(shlex.quote(w) for w in words)
        # A PRIVATE SUPPRESSION WINDOW PER DRIVE. The 124 arm speaks once per
        # class per window across PROCESSES (hooks.hookalarm), and the in-process
        # test-runner seam cannot reach this sh child — it is a separate
        # process that imports no Python at all. Without a private directory,
        # the control drive inside `test_a_clean_advisory_hook_says_nothing`
        # silences the arm in the sibling test and the wrapper reads as a
        # channel that says nothing, which is the exact defect these arms
        # exist to catch. Measured: both arms went red on `inject` this way.
        p = subprocess.run(["bash", "-c", c], capture_output=True, text=True,
                           input="{}",
                           env=dict(os.environ,
                                    HELM_HOOK_ALARM_DIR=tempfile.mkdtemp()))
        return p.returncode, p.stdout.strip(), p.stderr.strip()

    def _advisory(self):
        """The advisory specs — asserted NON-EMPTY at the point of use.

        Every arm below loops over this list, and a loop over an empty list
        passes while measuring nothing. The count is derived from SPECS (never
        transcribed) but the floor is unconditional, so a refactor that empties
        or renames the population turns these arms RED instead of green-and-
        vacuous.
        """
        specs = [s for s in hooks.SPECS if not s.get("gate")]
        self.assertGreaterEqual(len(specs), 6, "advisory population vanished")
        self.assertTrue(any(s["name"] == "join" for s in specs),
                        "the seat-reachability hook is the case this exists for")
        return specs

    def test_every_advisory_spec_declares_what_its_failure_costs(self):
        """DERIVED, never transcribed: the arm reads the spec list, so a spec
        added later without a sentence fails here instead of shipping the
        generic fallback."""
        specs = self._advisory()
        # positive control: the table is real and its sentences are sentences,
        # so "nothing missing" cannot mean "nothing looked at".
        self.assertTrue(hooks._ADVISORY_LOST)
        for name, lost in hooks._ADVISORY_LOST.items():
            self.assertGreater(len(lost), 20, name)
        missing = [s["name"] for s in specs
                   if s["name"] not in hooks._ADVISORY_LOST]
        self.assertEqual(missing, [], "advisory specs with no consequence line")

    def test_a_timed_out_advisory_hook_names_what_was_lost_on_both_channels(self):
        import json
        seen = 0
        for spec in self._advisory():
            cmd = hooks.spec_command(spec)
            rc, out, err = self._drive(cmd, 124)
            self.assertEqual(rc, 0, "%s must never hold a turn" % spec["name"])
            lost = hooks._ADVISORY_LOST[spec["name"]]
            self.assertIn(lost, err, spec["name"])
            # stderr is the channel that does NOT count: "stderr from a hook
            # that exits 0 goes to the debug log only, never the transcript,
            # and Claude never sees it." The JSON is the one a reader gets.
            doc = json.loads(out)
            self.assertIn(lost, doc["systemMessage"])                 # the owner
            self.assertEqual(doc["hookSpecificOutput"]["hookEventName"],
                             spec["event"])   # a mismatched event is DISCARDED
            self.assertIn(lost,
                          doc["hookSpecificOutput"]["additionalContext"])  # Claude
            seen += 1
        self.assertEqual(seen, len(self._advisory()))   # the loop RAN

    def test_a_clean_advisory_hook_says_nothing(self):
        """The not-overzealous half. A wrapper that chatters on success is its
        own defect, and rc 0 must stay quiet on BOTH channels."""
        seen = 0
        for spec in self._advisory():
            # CONTROL FIRST, same command and same two channels: driven to 124
            # this wrapper DOES speak. So the emptiness asserted below is
            # silence on a working channel, not a dead one.
            _, loud_out, loud_err = self._drive(hooks.spec_command(spec), 124)
            self.assertTrue(loud_out and loud_err, spec["name"])
            rc, out, err = self._drive(hooks.spec_command(spec), 0)
            self.assertEqual((rc, out, err), (0, "", ""), spec["name"])
            seen += 1
        self.assertEqual(seen, len(self._advisory()))   # the loop RAN

    def test_an_advisory_rc2_announces_but_still_does_not_block(self):  # noqa: VACUOUS_ASSERTION — the unconditional mirror is the line driving gates[0] to rc 2 and asserting it returns 2: same observable (rc), same helper, opposite verdict, outside every loop. The rung cannot see it through _drive's indirection.
        """MUST-MISS: a gate propagates 2, an advisory spec must not — or every
        lane hook silently becomes a blocker. hookrun._stronger holds the same
        line for the in-process dispatcher."""
        # CONTROL, same observable (rc), mirror condition: a GATE driven to 2
        # really does return 2, so the 0 below is a decision about gate-ness
        # and not a dead rc channel.
        gates = [s for s in hooks.SPECS if s.get("gate")]
        self.assertTrue(gates)
        # UNCONDITIONAL — not inside the loop. A control that only runs when a
        # list happens to be non-empty is the same vacuity it is meant to rule
        # out, one level up.
        self.assertEqual(self._drive(hooks.spec_command(gates[0]), 2)[0], 2,
                         gates[0]["name"])
        for g in gates[1:]:
            self.assertEqual(self._drive(hooks.spec_command(g), 2)[0], 2, g["name"])
        seen = 0
        for spec in self._advisory():
            rc, _, err = self._drive(hooks.spec_command(spec), 2)
            self.assertEqual(rc, 0, "%s must not block on 2" % spec["name"])
            self.assertIn("FAILED rc=2", err, spec["name"])
            seen += 1
        self.assertEqual(seen, len(self._advisory()))   # the loop RAN

    def test_the_record_hook_inherits_the_same_wrapper(self):
        """record built its own copy of the template, which is why it needed a
        separate fix; it is the hook that fires on every tool call."""
        from helm import record
        cmd = record.hook_command()
        self.assertEqual(shlex.split(cmd)[1], "lane", cmd)
        self.assertNotIn("|| true", cmd)
        self.assertTrue(hooks._fail_open(cmd))
        # the deployed budget must not move as a side effect of delegating
        self.assertEqual(shlex.split(cmd)[4], str(hooks.TIMEOUT_S))

    def test_fail_open_reads_the_contract_not_the_idiom(self):
        gates = [s for s in hooks.SPECS if s.get("gate")]
        self.assertTrue(gates, "no gate to contrast against")   # the MIRROR case
        for spec in self._advisory():
            self.assertTrue(hooks._fail_open(hooks.spec_command(spec)),
                            spec["name"])
        for spec in gates:
            self.assertFalse(hooks._fail_open(hooks.spec_command(spec)),
                             "a gate blocks on purpose: %s" % spec["name"])


class AnAlarmMustBeReachable(HooksBase):
    """A wrapper that mints an rc-124 alarm needs an outer deadline that
    outlives its inner one, or the alarm is dead code.

    Claude's outer runner defaults to five seconds. The inner `timeout` is what
    fires rc 124, and rc 124 exists because a timeout KILL prints nothing — so
    a hook that never ran reads exactly like one that ran clean. If the outer
    deadline lands first, the shell dies before its own alarm can speak.

    The grace was introduced for GATES, correctly, when gates were the only
    specs that spoke. When the advisory branch grew the same ladder it inherited
    the alarms and NOT the grace, which left eight of ten advisory specs — every
    one at >= 5s, `inject` and `record` at 10s among them — with an arm that
    could never fire. Verified against the wrapper in isolation, unable to fire
    in the estate. This arm binds the two together so they cannot drift again.
    """

    def _alarm_minting(self):
        """hooks.SPECS only, plus record's DEPLOYED spec.

        record.HOOK_SPECS is NOT the installed shape: it declares inner 5 for
        the in-process dispatcher while the shell hook is generated at 10, and
        the recorder installs through record._merge_hook rather than
        hooks._canonical_entry. Feeding HOOK_SPECS here computed a grace for a
        budget nothing runs and proved an UNUSED representation — an exact
        review caught it. The installed recorder seam is asserted in
        tests/test_record.py against the entries install_home actually writes."""
        from helm import record
        specs = [s for s in list(hooks.SPECS) + [record.deployed_spec(e)
                                                 for e in record.HOOK_EVENTS]
                 if hooks._wrapper_kind(hooks.spec_command(s))
                 in ("gate", "lane")]
        self.assertGreaterEqual(len(specs), 10, "alarm-minting population lost")
        return specs

    def _named(self, want):
        """One spec BY NAME — the unconditional control every loop below needs.

        A loop over a list computed by the code under test cannot witness its
        own emptiness, and every arm here would pass over zero specs. Naming
        `inject` pins the worst case besides: the largest inner budget (10s),
        so it is the spec whose alarm the outer default kills first.
        """
        from helm import record
        for spec in list(hooks.SPECS) + list(record.HOOK_SPECS):
            if spec["name"] == want:
                return spec
        self.fail("spec %r is gone — this arm is measuring nothing" % want)

    def test_every_alarm_minting_spec_outlives_its_own_inner_timeout(self):
        inject = self._named("inject")
        self.assertGreater(hooks._gate_outer_timeout(inject), inject["timeout"])
        for spec in self._alarm_minting():
            outer = hooks._gate_outer_timeout(spec)
            self.assertIsNotNone(outer, spec["name"])
            self.assertGreater(
                outer, spec["timeout"],
                "%s: the outer deadline must outlive the inner timeout, or its "
                "rc-124 alarm can never fire" % spec["name"])

    def test_the_grace_is_not_reserved_for_gates(self):
        """MUST-MISS: the exact regression. A gate-only predicate passes the
        arm above for gates and fails it for advisory specs, so this names the
        advisory half directly rather than trusting the aggregate."""
        self.assertIsNotNone(hooks._gate_outer_timeout(self._named("inject")))
        advisory = [s for s in self._alarm_minting() if not s.get("gate")]
        self.assertTrue(advisory, "no advisory spec mints an alarm")
        for spec in advisory:
            self.assertIsNotNone(
                hooks._gate_outer_timeout(spec),
                "%s mints an alarm but was granted no outer grace" % spec["name"])

    def test_the_installed_entry_carries_that_deadline(self):
        """The value must reach the FILE, not merely the function — an outer
        deadline computed and never written is the same dead alarm."""
        pinned = self._named("inject")
        self.assertEqual(hooks._canonical_entry(pinned)["hooks"][0]["timeout"],
                         hooks._gate_outer_timeout(pinned))
        for spec in self._alarm_minting():
            entry = hooks._canonical_entry(spec)
            # the deadline rides on the HOOK, not the entry — reading it one
            # level too high returns None for every spec and reads exactly
            # like a missing deadline (it did, for me, first try)
            hook = entry["hooks"][0]
            self.assertEqual(hook.get("timeout"),
                             hooks._gate_outer_timeout(spec), spec["name"])
            self.assertGreater(hook["timeout"], spec["timeout"], spec["name"])
class ExecutedGuardDoesNotLicenseWholeLineRewriteTest(unittest.TestCase):
    """The five formerly conflated forms execute a guard but are not ours."""

    MARKER = "fab-suite-pretooluse"
    ABSTAIN = (
        ("( %s )", "subshell"),
        ("( timeout 5 %s )", "subshell"),
        ("coproc CO { %s; }", "brace group"),
        ("(( 1 + 2 )); %s", "arithmetic evaluation"),
        ("(%s)", "subshell"),
    )

    def _assert_seen_and_abstained(self):
        self.assertEqual(len(self.ABSTAIN), 5)
        observed = []
        for template, reason in self.ABSTAIN:
            command = template % self.MARKER
            segments, abstention = hooks._segments_ex(command)
            seen = any(os.path.basename(hooks._executed(segment)[0]) == self.MARKER
                       for segment in segments)
            observed.append((seen, hooks._own_hit(command, self.MARKER), abstention))
        self.assertEqual(observed, [(True, False, reason) for _template, reason in self.ABSTAIN])
        # Ownership must still see a plain entry; always-False is not a cure.
        self.assertTrue(hooks._own_hit(self.MARKER, self.MARKER))

    def test_seen_execution_and_rewrite_abstention_are_both_observed(self):
        self._assert_seen_and_abstained()

    def test_admitting_one_subshell_fails_the_same_paired_assertion(self):
        self._assert_seen_and_abstained()
        original = hooks._own_hit
        def wrongly_owned(command, marker):
            return command == "( %s )" % self.MARKER or original(command, marker)
        with mock.patch.object(hooks, "_own_hit", side_effect=wrongly_owned):
            with self.assertRaises(AssertionError):
                self._assert_seen_and_abstained()
        self._assert_seen_and_abstained()

    def test_installer_preserves_all_five_user_commands_and_updates_plain_owned_entry(self):
        spec = {"name": "synthetic-guard", "event": "PreToolUse", "matcher": "Bash",
                "own": (self.MARKER,), "timeout": 5}
        canonical = "/canonical/" + self.MARKER
        self.assertEqual(len(self.ABSTAIN), 5)
        with mock.patch.object(hooks, "spec_command", return_value=canonical):
            for template, _reason in self.ABSTAIN:
                command = template % self.MARKER
                original = {"type": "command", "command": command}
                out = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [dict(original)]}]}}
                self.assertEqual(hooks._merge_event(out, spec), "add")
                self.assertEqual(out["hooks"]["PreToolUse"][0]["hooks"], [original])
                all_commands = hooks._hook_cmds(out, "PreToolUse")
                self.assertEqual(all_commands, [command, canonical])
            # Same installer, real positive rewrite: an ordinary owned command
            # is canonicalized rather than left untouched with another copy.
            plain = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "/old/" + self.MARKER}]}]}}
            self.assertEqual(hooks._merge_event(plain, spec), "update")
            self.assertEqual(hooks._hook_cmds(plain, "PreToolUse"), [canonical])


class OwnershipAbstainsOnGrammarItCannotReadTest(unittest.TestCase):
    """Ownership is a LICENSE TO REWRITE, so an unreadable line must DISOWN.

    THIS CLASS EXISTS BECAUSE THE COST IS ASYMMETRIC, and the asymmetry is the
    whole argument. A false POSITIVE hands sync a line helm never parsed and it
    rewrites it. A false NEGATIVE appends a canonical duplicate beside a working
    hand-wired hook — the guard then runs twice, which is noisy and not
    destructive. So where the walk cannot read the grammar, it abstains.

    THE CASES BELOW ARE NOT SHELL TRUTH AND MUST NOT MOVE INTO
    TheGrammarIsCheckedAgainstTheSHELLNotAgainstOurTableTest. In each one a
    shell WOULD run the guard, so the honest expectation there would be True.
    These record a deliberate helm POLICY of abstaining, and mixing them into
    the shell-derived class would destroy the one property that makes it able
    to catch our table being wrong.

    MEASURED COST, not assumed: a census of every hook command in the
    settings.json files on this host — 266 strings, 102 of them mentioning helm
    as a must-hit control — found ZERO using any of these constructs. The
    abstention is therefore free on the live population. That census is
    NARROWER than the 166-file/387-string sweep cited elsewhere in this file,
    so read the zero as "none in 266 here", not as "none anywhere".
    """

    MARKER = _DELIVER_MARKER

    # (command, why a shell WOULD run the guard — and we abstain anyway)
    ABSTAIN = (
        # MEASURED on 3402c442a1fd: each of these RUNS the guard and also
        # carries the user's own commands, and sync replaces h["command"]
        # WHOLESALE, so owning one DELETES the user's work. main has no
        # ownership mechanism at all, which is what made this worse-than-main.
        ("a |& /opt/g/helm %s",
         "|& is a PIPE: the guard runs, and `a` is the user's"),
        ("{ a; /opt/g/helm %s; }",
         "brace group: the guard runs, the braces and `a` are the user's"),
        ("( a; /opt/g/helm %s )", "subshell: same"),
        (">| f; /opt/g/helm %s",
         "the ; really does separate; the guard really does run"),
        ("<> f; /opt/g/helm %s", "same"),
        ("((i++)); /opt/g/helm %s",
         "the arithmetic ends before the ;, and the guard runs after it"),
    )

    def _owned_among(self, templates):
        """The ONE collection expression both the assertion and its positive
        control run through — a control that used a different expression would
        prove the control works, not that THIS arm can see a failure."""
        out = []
        for template in templates:
            cmd = template % self.MARKER
            if hooks._own_hit(cmd, self.MARKER):
                out.append(cmd)
        return out

    def test_an_unreadable_line_is_disowned_even_when_the_guard_would_run(self):  # noqa: VACUOUS_ASSERTION — an unconditional positive control IS present (the line above the absence assertion, which fails if the collector cannot see an owned command). I do NOT know why the rung does not credit it, and I have stopped guessing: I measured three candidate causes and FALSIFIED all three — same-name rebinding, distinct names, and inlining the production call instead of the helper each leave the finding standing. An earlier version of this comment asserted the rebinding mechanism; that was my inference, not a measurement, and it is wrong. The property is verified by MUTATION instead: always-False _own_hit and an emptied _UNMODELLED are each caught here.
        # POSITIVE CONTROL, unconditional, on the SAME observable: the empty
        # list below means "nothing was owned", which is also what an empty
        # input or a broken collector produces. This proves the collector
        # reports when something IS owned, so the [] beneath it is a finding.
        owned = self._owned_among(("/opt/g/helm %s",))
        self.assertEqual(owned, ["/opt/g/helm " + self.MARKER],
                         "collector cannot see an owned command; the "
                         "assertion below would pass vacuously")
        self.assertTrue(self.ABSTAIN, "no cases: the arm below is vacuous")
        owned = self._owned_among([t for t, _why in self.ABSTAIN])
        self.assertEqual(owned, [], "unreadable lines were OWNED: %r" % (owned,))

    def test_the_abstention_does_NOT_swallow_ordinary_entries(self):  # noqa: VACUOUS_ASSERTION — an unconditional positive control IS present (the line above the absence assertion, which fails if the collector cannot see an owned command). I do NOT know why the rung does not credit it, and I have stopped guessing: I measured three candidate causes and FALSIFIED all three — same-name rebinding, distinct names, and inlining the production call instead of the helper each leave the finding standing. An earlier version of this comment asserted the rebinding mechanism; that was my inference, not a measurement, and it is wrong. The property is verified by MUTATION instead: always-False _own_hit and an emptied _UNMODELLED are each caught here.
        """The control that makes the arm above mean something.

        Without it, a mutation returning False from _own_hit unconditionally
        passes the abstention test perfectly — the failure mode is a guard that
        owns NOTHING, and every entry on the host earns a duplicate."""
        must_own = (
            "/opt/g/helm %s",
            "timeout 5 /opt/g/helm %s",
            "if [ -x /opt/g/helm ]; then /opt/g/helm %s; fi",
            # THE BRACE AND PAREN RULES ARE WORD-SHAPED, and these are the
            # cases a literal "{" / "(" match would wrongly disown. Every one
            # is an ordinary hook line.
            "echo ${HOME}; /opt/g/helm %s",
            "awk '{print}' f; /opt/g/helm %s",
            "X=$(date); /opt/g/helm %s",
        )
        def missing(cands):
            return [c % self.MARKER for c in cands
                    if not hooks._own_hit(c % self.MARKER, self.MARKER)]

        # POSITIVE CONTROL on the SAME expression: a command that is genuinely
        # NOT owned must show up in `missing`, or an empty result below means
        # nothing.
        missed = missing(("echo 'a; /opt/g/helm %s'",))
        self.assertEqual(missed, ["echo 'a; /opt/g/helm " + self.MARKER + "'"],
                         "the collector cannot see an unowned command")
        self.assertTrue(must_own, "no cases: the arm below is vacuous")
        missed = missing(must_own)
        self.assertEqual(missed, [], "abstention swallowed real entries: %r"
                         % (missed,))

    def test_command_substitution_STAYS_owned(self):
        """The ruling on 616376a64691, and the cure is an arm not a table row.

        `$( )` and the backquote form split INSIDE the substitution, so the
        guard survives into a later segment and the line comes out owned. That
        is the right answer reached by ACCIDENT, and my instinct was to make it
        deliberate by adding `$(` to _UNMODELLED. A review refused that and was
        right: command substitution is ORDINARY in hook lines in a way that
        `|&`, brace groups and subshells are not, so abstaining on it trades
        three exotic false owners for a routine class of LOST auto-management —
        and a lost owner appends a duplicate hook to every affected entry.

        So the accident is load-bearing and this arm is what makes it safe: if
        the splitter changes and the accident stops holding, this goes RED
        instead of ownership silently vanishing from every substitution line.
        """
        # UNCONDITIONAL FLOOR, outside the loop — every assertion below is
        # inside `for` + subTest, so an emptied tuple would leave this green.
        base = "echo $(date); /opt/g/helm " + self.MARKER
        self.assertTrue(hooks._own_hit(base, self.MARKER))
        self.assertIsNone(hooks._segments_ex(base)[1])
        for src in ("echo $(date); /opt/g/helm %s",
                    "echo `date`; /opt/g/helm %s",
                    "X=$(a; b); /opt/g/helm %s",
                    "echo ${HOME}; /opt/g/helm %s",
                    "/opt/g/helm %s $(date)"):
            cmd = src % self.MARKER
            with self.subTest(src=cmd):
                self.assertTrue(hooks._own_hit(cmd, self.MARKER),
                                "substitution line lost its owner: %r" % (cmd,))
                self.assertIsNone(hooks._segments_ex(cmd)[1],
                                  "substitution must not read as unmodelled")

    def test_each_construct_is_reported_by_name(self):
        """The walk must say WHICH construct it could not read.

        A bare boolean makes every future report of this class unactionable —
        the reader learns the line was disowned and not what to look at."""
        # NO assertIsNone CONTROL HERE, deliberately. Adding one made this
        # arm carry an ABSENCE assertion it never had, which then needs its own
        # positive control — circular. The None direction is the entire subject
        # of test_ordinary_commands_report_no_construct below, which carries a
        # non-None control of its own. Two arms, one direction each.
        # UNCONDITIONAL, OUTSIDE THE LOOP. Every assertion below sits inside
        # `for` + subTest, so emptying the case tuple would make this arm pass
        # while checking nothing. This one line is the arm's floor: it runs
        # whatever the table contains.
        self.assertEqual(hooks._segments_ex("((x))")[1], "arithmetic evaluation")
        for src, want in (("((x))", "arithmetic evaluation"),
                          ("[[ x ]]", "conditional expression"),
                          ("a <(b)", "process substitution"),
                          ("a <<< w", "here-string"),
                          ("a >| f", "clobbering redirection"),
                          ("a <> f", "read-write redirection"),
                          ("a |& b", "pipe-with-stderr"),
                          ("{ a; b; }", "brace group"),
                          ("( a; b )", "subshell")):
            with self.subTest(src=src):
                self.assertEqual(hooks._segments_ex(src)[1], want)

    def test_ordinary_commands_report_no_construct(self):
        # POSITIVE CONTROL: the same accessor must be able to answer NON-None,
        # otherwise a mutation returning None always makes this arm green.
        self.assertEqual(hooks._segments_ex("((x))")[1], "arithmetic evaluation")
        for src in ("a && b", "a; b", "echo 'a; b'", "a >& f", "a 2>&1 b",
                    "echo ${HOME}", "awk '{print}' f", "X=$(date)",
                    "a || b", "a | b"):
            with self.subTest(src=src):
                self.assertIsNone(hooks._segments_ex(src)[1])
