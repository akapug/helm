#!/usr/bin/env python3
"""Coordination verbs run TRUNK helm from any tree (task/3382).

Measured over 40.5 h of three local seats' transcripts: 812 helm calls
carried the maker banner "[helm] you ran .../helm@X from <lane>", it was the
FIRST line of 170 failures, it steered seats to `./bin/helm`, and up to 78
ledger writes then exited 0 from lane trees a median 11 commits behind main,
where every current reader drops them.

Every cell of the surface-by-state matrix is an arm here:

  coordination verb from a lane worktree   trunk code runs (a marker only the
                                           trunk copy carries), no banner
  coordination verb from the trunk         runs in place, no re-exec, no loop,
                                           and no banner from a lane's cwd
  dogfood verb from a lane worktree        the lane's code runs; the banner is
                                           kept where it fired before
  HELM_LANE_COORDINATION=1                 the lane's own coordination verb
                                           runs and says so in ONE line
  trunk declared, unusable, a ledger verb  REFUSED, nothing runs, the fix named,
                                           the text byte for byte as before
  trunk declared, unusable, chat           THIS tree's chat runs, and ONE line
                                           names the cause, the fix and the
                                           tree; rc and stdout are chat's own
                                           (the integrator's ruling: a refused
                                           chat is the fleet's one channel to
                                           coordinate the fix going down)
  exit code, stdout, stderr                trunk's own, byte for byte
  argv with spaces/quotes/bytes, stdin     preserved byte for byte

The END-TO-END arms build one real repository per class: a trunk checkout on
`main` and a linked lane worktree, each holding a full copy of the helm
package under test, with a different verb override appended to each copy's
`helm/cli.py`. The marker a run prints therefore names the tree whose code
ran, and nothing about the answer is inferred.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
from tests._tmphome import copy_live_tree  # noqa: E402
_tmp_home(prefix="helm-test-trunkroute-", var="HELM_HOME")

from helm import cli, trunkroute  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(cwd, *args, check=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-C", cwd] + list(args), check=check,
                          capture_output=True, text=True, env=env)


def _init(root):
    os.makedirs(root)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")


def _commit(root, msg):
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", msg)


#: The refusal a ledger verb printed before the chat fallback existed, as a
#: LITERAL: the ruling keeps it byte for byte, so it is pinned here and not
#: rebuilt with the function that renders it. `_refusal_text` fills it.
REFUSAL = ("[helm] REFUSED: the %s verb writes a shared ledger, so it runs "
           "trunk helm, and trunk helm cannot be used: %s. Nothing ran from "
           "%s, so nothing was written. Fix: %s. To run this tree's own code "
           "on purpose, set HELM_LANE_COORDINATION=1.")

#: The ledger verbs the ruling names; every other coordination verb but chat
#: is held to the same refusal by
#: `test_every_ledger_verb_keeps_its_refusal_byte_for_byte`.
LEDGER_VERBS = ("dispatch", "work", "handoff")


def _refusal_text(verb, where, why, fix):
    return REFUSAL % (verb, why, where, fix)


def _nowhere(trunk):
    """(why, fix) of the cause the ruling is about: the trunk checkout left
    its branch, so no worktree has it checked out."""
    return ("no worktree of this repository has the trunk branch main "
            "checked out",
            "check it out in the trunk checkout: git -C %s switch main" % trunk)


def _no_entry(trunk):
    """(why, fix) of a trunk checkout with no runnable bin/helm."""
    return ("the trunk checkout %s has no runnable bin/helm and helm package"
            % trunk,
            "restore them: git -C %s checkout -- bin/helm helm" % trunk)


#: How the fallback line opens, and what follows its cause and its fix.
FALLBACK_HEAD = "[helm] trunk helm cannot be used: "


def _fallback_cause(line):
    """(why, fix) as a fallback line states them."""
    rest = line[len(FALLBACK_HEAD):] if line.startswith(FALLBACK_HEAD) else ""
    why, _sep, rest = rest.partition(". Fix: ")
    fix, _sep, _tail = rest.partition(". The chat verb ran ")
    return why, fix


def _cause_of(refusal):
    """(why, fix) as a refusal line states them."""
    _head, _sep, rest = refusal.partition("cannot be used: ")
    why, _sep, rest = rest.partition(". Nothing ran from ")
    _tree, _sep, rest = rest.partition(", so nothing was written. Fix: ")
    fix, _sep, _tail = rest.partition(". To run this tree's own code")
    return why, fix


def _stub_helm(root):
    """The least a checkout needs to count as a runnable helm tree."""
    os.makedirs(os.path.join(root, "helm"), exist_ok=True)
    os.makedirs(os.path.join(root, "bin"), exist_ok=True)
    open(os.path.join(root, "helm", "__init__.py"), "w").close()
    entry = os.path.join(root, "bin", "helm")
    with open(entry, "w") as f:
        f.write("#!/usr/bin/env python3\n")
    os.chmod(entry, 0o755)


class _StubRepo(unittest.TestCase):
    """A trunk checkout on `main` and one linked lane worktree, with STUB
    trees: enough for `decide`, which reads git and the filesystem only."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="trunkroute-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.trunk = os.path.join(self.tmp, "trunk")
        _init(self.trunk)
        _stub_helm(self.trunk)
        _commit(self.trunk, "base")
        _git(self.trunk, "config", "helm.trunkRef", "refs/heads/main")
        self.lane = os.path.join(self.tmp, "lane")
        _git(self.trunk, "worktree", "add", "-q", "-b", "lane/x", self.lane)

    def decide(self, argv, where, env=None):
        return trunkroute.decide(argv, package_dir=os.path.join(where, "helm"),
                                 environ={} if env is None else env)


class DecideTest(_StubRepo):
    def test_a_coordination_verb_from_a_lane_routes_to_the_declared_trunk(self):
        route = self.decide(["dispatch", "verdict"], self.lane)
        self.assertEqual(route.action, trunkroute.TRUNK)
        self.assertEqual(route.trunk, self.trunk)

    def test_a_coordination_verb_in_the_trunk_runs_here(self):
        route = self.decide(["chat", "post"], self.trunk)
        self.assertEqual(route.action, trunkroute.HERE)
        self.assertEqual(route.trunk, self.trunk)

    def test_the_trunk_answers_from_its_own_head_without_the_worktree_list(self):
        with mock.patch.object(trunkroute, "_worktrees",
                               side_effect=AssertionError("listed")):
            route = self.decide(["chat"], self.trunk)
        self.assertEqual(route.action, trunkroute.HERE)

    def test_a_head_the_files_cannot_name_falls_back_to_the_list(self):
        """The fast path decides nothing on a miss (a reftable repository
        keeps a placeholder HEAD file, for one)."""
        with mock.patch.object(trunkroute, "_head_ref", return_value=None):
            self.assertEqual(self.decide(["chat"], self.trunk).action,
                             trunkroute.HERE)
            self.assertEqual(self.decide(["chat"], self.lane).action,
                             trunkroute.TRUNK)

    def test_head_ref_reads_a_checkout_and_a_linked_worktree(self):
        self.assertEqual(trunkroute._head_ref(self.trunk), "refs/heads/main")
        self.assertEqual(trunkroute._head_ref(self.lane), "refs/heads/lane/x")
        _git(self.trunk, "checkout", "-q", "--detach")
        self.assertIsNone(trunkroute._head_ref(self.trunk))
        self.assertIsNone(trunkroute._head_ref(os.path.join(self.tmp, "no")))

    def test_a_dogfood_verb_is_never_routed_and_asks_git_nothing(self):  # noqa: VACUOUS_ASSERTION — one exact equality over seven routes, and _git armed to raise
        with mock.patch.object(trunkroute, "_git",
                               side_effect=AssertionError("asked git")):
            got = [self.decide(argv, self.lane).action for argv in (
                ["gate", "run"], ["doctor"], ["--version"], [],
                ["help", "dispatch"], ["train"], ["owed"])]
        self.assertEqual(got, [trunkroute.PASS] * 7)

    def test_the_opt_in_runs_the_lane_and_names_both_trees(self):
        route = self.decide(["dispatch", "send"], self.lane,
                            {trunkroute.OPT_IN: "1"})
        self.assertEqual(route.action, trunkroute.OWN)
        self.assertIn(trunkroute.OPT_IN + "=1", route.line)
        self.assertIn(self.lane, route.line)
        self.assertIn(self.trunk, route.line)
        self.assertEqual(len(route.line.splitlines()), 1)

    def test_the_opt_in_in_the_trunk_says_nothing(self):
        route = self.decide(["chat"], self.trunk, {trunkroute.OPT_IN: "1"})
        self.assertEqual(route.action, trunkroute.HERE)

    def test_the_opt_in_is_exactly_1(self):
        route = self.decide(["chat"], self.lane, {trunkroute.OPT_IN: "yes"})
        self.assertEqual(route.action, trunkroute.TRUNK)

    def test_an_undeclared_repository_keeps_todays_behaviour(self):
        _git(self.trunk, "config", "--unset", "helm.trunkRef")
        self.assertEqual(self.decide(["chat"], self.lane).action,
                         trunkroute.PASS)

    def test_a_user_level_declaration_is_not_the_repositorys(self):
        """--local: one line in a user's ~/.gitconfig must not declare a
        trunk for every repository they touch."""
        _git(self.trunk, "config", "--unset", "helm.trunkRef")
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        with open(os.path.join(home, ".gitconfig"), "w") as f:
            f.write("[helm]\n\ttrunkRef = refs/heads/main\n")
        with mock.patch.dict(os.environ, {"HOME": home}):
            self.assertEqual(self.decide(["chat"], self.lane).action,
                             trunkroute.PASS)

    def test_installed_outside_a_checkout_is_not_routed(self):
        loose = os.path.join(self.tmp, "loose", "helm")
        os.makedirs(loose)
        with mock.patch.object(cli, "_tree_of", return_value=None):
            self.assertEqual(trunkroute.decide(["chat"], package_dir=loose,
                                               environ={}).action,
                             trunkroute.PASS)

    def refusal(self, route):
        """`route.line`, asserted to be one refusal line that names the fix
        and the opt-in."""
        self.assertEqual(route.action, trunkroute.REFUSE)
        self.assertTrue(route.line.startswith("[helm] REFUSED:"), route.line)
        self.assertIn("Fix:", route.line)
        self.assertIn(trunkroute.OPT_IN + "=1", route.line)
        self.assertEqual(len(route.line.splitlines()), 1)
        return route.line

    def fallback(self, route, where):
        """(why, fix) as the fallback line states them, asserted: chat runs
        `where`'s code and says so in ONE line that is not a refusal."""
        self.assertEqual(route.action, trunkroute.FALLBACK, route.line)
        self.assertEqual(route.tree, where)
        self.assertEqual(len(route.line.splitlines()), 1, route.line)
        self.assertTrue(route.line.startswith(FALLBACK_HEAD), route.line)
        self.assertIn("The chat verb ran this tree's own code instead (%s)"
                      % where, route.line)
        return _fallback_cause(route.line)

    def unusable(self, where):
        """(why, fix) of an unusable trunk, asserted from both halves of the
        ruling: every ledger verb is refused naming them, and chat falls
        back naming the SAME two in its one line."""
        refused = [_cause_of(self.refusal(self.decide([verb], where)))
                   for verb in LEDGER_VERBS]
        chat = self.fallback(self.decide(["chat", "post"], where), where)
        self.assertTrue(chat[0] and chat[1], chat)
        self.assertEqual(refused, [chat] * len(LEDGER_VERBS))
        return chat

    def test_no_worktree_on_the_trunk_branch_refuses_and_names_the_fix(self):
        _git(self.trunk, "checkout", "-q", "--detach")
        why, fix = self.unusable(self.lane)
        self.assertIn("switch main", fix)
        self.assertEqual((why, fix), _nowhere(self.trunk))

    def test_every_ledger_verb_keeps_its_refusal_byte_for_byte(self):
        """The ruling: every coordination verb but chat keeps today's
        refusal. Pinned as literal text for the cause the ruling names."""
        _git(self.trunk, "checkout", "-q", "--detach")
        why, fix = _nowhere(self.trunk)
        others = sorted(trunkroute.COORDINATION_VERBS - {"chat"})
        self.assertIn("dispatch", others)
        self.assertNotIn("chat", others)
        got = {verb: self.decide([verb], self.lane) for verb in others}
        self.assertIn("[helm] REFUSED: the dispatch verb",
                      got["dispatch"].line)
        self.assertEqual([(got[v].action, got[v].line) for v in others],
                         [(trunkroute.REFUSE,
                           _refusal_text(v, self.lane, why, fix))
                          for v in others])

    def test_a_missing_entry_script_keeps_its_refusal_byte_for_byte(self):
        os.unlink(os.path.join(self.trunk, "bin", "helm"))
        why, fix = _no_entry(self.trunk)
        lines = [self.decide([verb], self.lane).line for verb in LEDGER_VERBS]
        self.assertIn("no runnable bin/helm", lines[0])
        self.assertEqual(lines, [_refusal_text(verb, self.lane, why, fix)
                                 for verb in LEDGER_VERBS])

    def test_chat_from_a_lane_runs_the_lane_when_the_branch_is_nowhere(self):
        _git(self.trunk, "checkout", "-q", "--detach")
        why, fix = self.fallback(self.decide(["chat", "post", "x"], self.lane),
                                 self.lane)
        self.assertIn("switch main", fix)
        self.assertEqual((why, fix), _nowhere(self.trunk))

    def test_chat_runs_the_lane_when_trunk_has_no_entry_script(self):
        os.unlink(os.path.join(self.trunk, "bin", "helm"))
        why, fix = self.fallback(self.decide(["chat"], self.lane), self.lane)
        self.assertIn("no runnable bin/helm", why)
        self.assertEqual((why, fix), _no_entry(self.trunk))

    def test_a_usable_trunk_still_routes_chat_from_a_lane(self):
        route = self.decide(["chat", "post"], self.lane)
        self.assertEqual(route.action, trunkroute.TRUNK)
        self.assertEqual(route, trunkroute.Route(
            trunkroute.TRUNK, self.trunk, None, self.lane))

    def test_a_detached_trunk_refuses_from_the_trunk_too(self):
        """The shared checkout off its branch is not trunk either: a ledger
        verb run from it is refused, and chat run from it runs ITS code (the
        overnight case: `helm` on PATH is that checkout)."""
        _git(self.trunk, "checkout", "-q", "--detach")
        _why, fix = self.unusable(self.trunk)
        self.assertIn("switch main", fix)

    def test_a_declaration_with_two_values_refuses(self):
        _git(self.trunk, "config", "--add", "helm.trunkRef", "refs/heads/other")
        why, _fix = self.unusable(self.lane)
        self.assertIn("more than once", why)

    def test_an_empty_first_value_beside_a_real_one_is_still_two(self):
        """A blank line is a declared empty value; stripping git's output is
        how two conflicting declarations once read as one."""
        _git(self.trunk, "config", "--unset", "helm.trunkRef")
        _git(self.trunk, "config", "--add", "helm.trunkRef", "")
        _git(self.trunk, "config", "--add", "helm.trunkRef", "refs/heads/main")
        why, _fix = self.unusable(self.lane)
        self.assertIn("more than once", why)

    def test_an_empty_declaration_refuses(self):
        _git(self.trunk, "config", "helm.trunkRef", "")
        why, _fix = self.unusable(self.lane)
        self.assertIn("empty", why)

    def test_a_remote_tracking_declaration_refuses(self):
        _git(self.trunk, "config", "helm.trunkRef", "refs/remotes/origin/main")
        why, _fix = self.unusable(self.lane)
        self.assertIn("refs/remotes/origin/main", why)

    def test_a_trunk_without_its_entry_script_refuses(self):
        os.unlink(os.path.join(self.trunk, "bin", "helm"))
        why, _fix = self.unusable(self.lane)
        self.assertIn("bin/helm", why)
        self.assertIn(self.trunk, why)

    def test_an_entry_script_that_leads_out_of_the_trunk_refuses(self):
        other = os.path.join(self.tmp, "elsewhere-helm")
        with open(other, "w") as f:
            f.write("#!/usr/bin/env python3\n")
        os.chmod(other, 0o755)
        entry = os.path.join(self.trunk, "bin", "helm")
        os.unlink(entry)
        os.symlink(other, entry)
        why, _fix = self.unusable(self.lane)
        self.assertIn(self.trunk, why)

    def test_git_that_cannot_be_asked_is_not_undeclared(self):
        with mock.patch.object(trunkroute, "_git", return_value=(None, "")):
            why, _fix = self.unusable(self.lane)
        self.assertIn("could not read helm.trunkRef", why)

    def test_the_opt_in_runs_the_lane_when_trunk_is_unusable(self):
        """The opt-in is unchanged, for chat as for a ledger verb: its own
        line, not the fallback's."""
        _git(self.trunk, "checkout", "-q", "--detach")
        env = {trunkroute.OPT_IN: "1"}
        got = [self.decide([verb], self.lane, dict(env))
               for verb in ("chat", "dispatch")]
        self.assertIn("HELM_LANE_COORDINATION=1", got[0].line)
        self.assertEqual([(r.action, r.line) for r in got], [
            (trunkroute.OWN,
             "[helm] HELM_LANE_COORDINATION=1: the %s verb runs this tree's "
             "own code (%s); trunk helm cannot be used: %s."
             % (verb, self.lane, _nowhere(self.trunk)[0]))
            for verb in ("chat", "dispatch")])

    def test_the_hop_is_removed_for_every_verb(self):  # noqa: VACUOUS_ASSERTION — exact equality: each env keeps KEEP and loses only the hop
        left = []
        for argv in (["chat"], ["gate"], []):
            env = {trunkroute.HOP: self.trunk, "KEEP": "1"}
            self.decide(argv, self.lane, env)
            left.append(env)
        self.assertEqual(left, [{"KEEP": "1"}] * 3)

    def test_the_hop_names_this_tree_and_it_is_trunk_so_it_runs_here(self):
        env = {trunkroute.HOP: self.trunk}
        with mock.patch.object(trunkroute, "_worktrees",
                               side_effect=AssertionError("listed")):
            route = self.decide(["chat"], self.trunk, env)
        self.assertEqual(route.action, trunkroute.HERE)

    def test_a_hop_set_by_hand_to_the_lane_itself_is_no_bypass(self):
        line = self.refusal(self.decide(["chat"], self.lane,
                                        {trunkroute.HOP: self.lane}))
        self.assertIn("not trunk", line)

    def test_a_hop_into_a_trunk_that_stopped_being_trunk_refuses(self):
        """Declared away between the parent's look and the child's."""
        _git(self.trunk, "config", "--unset", "helm.trunkRef")
        line = self.refusal(self.decide(["chat"], self.trunk,
                                        {trunkroute.HOP: self.trunk}))
        self.assertIn("not trunk", line)

    def test_a_hop_into_a_trunk_that_left_its_branch_lets_only_chat_run(self):
        """The trunk checkout left main between the parent's look and the
        child's: trunk is declared and unusable in the child, so chat runs
        there and a ledger verb keeps the hop's refusal."""
        _git(self.trunk, "checkout", "-q", "--detach")
        lines = [self.refusal(self.decide([verb], self.trunk,
                                          {trunkroute.HOP: self.trunk}))
                 for verb in LEDGER_VERBS]
        self.assertIn("which is not trunk", lines[0])
        self.assertEqual([ln.count("which is not trunk") for ln in lines],
                         [1] * len(LEDGER_VERBS))
        why, fix = self.fallback(self.decide(
            ["chat"], self.trunk, {trunkroute.HOP: self.trunk}), self.trunk)
        self.assertIn("switch main", fix)
        self.assertEqual((why, fix), _nowhere(self.trunk))

    def test_a_hop_that_names_another_tree_refuses_rather_than_hop_again(self):
        """No loop, and no silent bypass: a HOP set by hand in a lane is not
        permission to run the lane."""
        line = self.refusal(self.decide(["chat"], self.lane,
                                        {trunkroute.HOP: self.trunk}))
        self.assertIn("reached %s, which is not trunk" % self.lane, line)
        self.assertIn(self.trunk, line)

    def test_a_second_worktree_on_the_trunk_branch_is_ambiguous(self):
        """git refuses this without --force; helm refuses to guess."""
        other = os.path.join(self.tmp, "other")
        _git(self.trunk, "worktree", "add", "-q", "-f", other, "main")
        why, _fix = self.unusable(self.lane)
        self.assertIn("2 worktrees", why)

    def test_a_worktree_whose_directory_is_gone_is_not_trunk(self):
        """A prunable worktree holding the branch is not a checkout."""
        _git(self.trunk, "checkout", "-q", "--detach")
        gone = os.path.join(self.tmp, "gone")
        _git(self.trunk, "worktree", "add", "-q", gone, "main")
        shutil.rmtree(gone)
        _why, fix = self.unusable(self.lane)
        self.assertIn("switch main", fix)


def _written(err):
    """What a mocked stderr was given."""
    return "".join(c.args[0] for c in err.write.call_args_list)


def _boom(*_a):
    raise FileNotFoundError("gone")


class EnterTest(_StubRepo):
    def setUp(self):
        super().setUp()
        # A fallback spends the door's once-per-process stale-tree line; this
        # process's own list is not the arm's to spend.
        said = mock.patch.object(cli, "_STALE_TREE_SAID", [])
        said.start()
        self.addCleanup(said.stop)

    def test_the_trunk_route_execs_trunk_bin_helm_with_argv_untouched(self):
        calls = []
        argv = ["dispatch", "send", "a b", "q\"u'o", "", "\udcff"]
        env = {"KEEP": "x"}
        rc, banner = trunkroute.enter(
            argv, package_dir=os.path.join(self.lane, "helm"), environ=env,
            execve=lambda *a: calls.append(a))
        self.assertEqual(len(calls), 1)
        exe, args, child = calls[0]
        self.assertEqual(args[-len(argv) - 1:],
                         [os.path.join(self.trunk, "bin", "helm")] + argv)
        self.assertEqual(exe, args[0])
        self.assertEqual(child[trunkroute.HOP], self.trunk)
        self.assertEqual(child["KEEP"], "x")
        self.assertEqual(env, {"KEEP": "x"})       # the caller's is untouched
        self.assertEqual((rc, banner), (trunkroute.REFUSED_RC, False))

    def test_the_trunk_runs_in_place_with_no_exec_and_no_banner(self):  # noqa: VACUOUS_ASSERTION — (None, False) IS the contract (run here, no banner); execve=self.fail fails the arm on any exec
        rc, banner = trunkroute.enter(
            ["chat"], package_dir=os.path.join(self.trunk, "helm"),
            environ={}, execve=self.fail)
        self.assertEqual((rc, banner), (None, False))

    def test_a_dogfood_verb_keeps_the_banner(self):  # noqa: VACUOUS_ASSERTION — (None, True) IS the contract (run here, keep the banner); execve=self.fail fails the arm on any exec
        rc, banner = trunkroute.enter(
            ["doctor"], package_dir=os.path.join(self.lane, "helm"),
            environ={}, execve=self.fail)
        self.assertEqual((rc, banner), (None, True))

    def test_a_refusal_prints_one_line_and_runs_nothing(self):
        _git(self.trunk, "checkout", "-q", "--detach")
        with mock.patch("sys.stderr") as err:
            rc, banner = trunkroute.enter(
                ["dispatch"], package_dir=os.path.join(self.lane, "helm"),
                environ={}, execve=self.fail)
        self.assertEqual((rc, banner), (trunkroute.REFUSED_RC, False))
        self.assertIn("[helm] REFUSED: the dispatch verb", _written(err))
        self.assertEqual(_written(err), _refusal_text(
            "dispatch", self.lane, *_nowhere(self.trunk)) + "\n")

    def test_chat_runs_here_with_one_line_and_no_stale_tree_line(self):
        """The door's tree-behind-trunk line would tell the reader to
        fast-forward or rebase THIS tree, the wrong fix when trunk left its
        branch, and a second line where the ruling allows one."""
        with open(os.path.join(self.trunk, "ahead"), "w") as f:
            f.write("ahead\n")
        _commit(self.trunk, "ahead")
        _git(self.trunk, "update-ref", "refs/remotes/origin/main",
             "refs/heads/main")
        _git(self.trunk, "checkout", "-q", "--detach")

        def stale():
            return cli.stale_tree_warning(
                cwd=self.lane, package_dir=os.path.join(self.lane, "helm"))
        # the control: this lane is behind, and the door's line speaks
        self.assertIn("behind origin/main", stale() or "")
        cli._STALE_TREE_SAID.clear()
        with mock.patch("sys.stderr") as err:
            rc, banner = trunkroute.enter(
                ["chat", "post"], package_dir=os.path.join(self.lane, "helm"),
                environ={}, execve=self.fail)
        self.assertEqual((rc, banner), (None, False))
        text = _written(err)
        self.assertEqual(text.count("\n"), 1, text)
        self.assertEqual(text, self.decide(["chat"], self.lane).line + "\n")
        for part in _nowhere(self.trunk) + (self.lane,):
            self.assertIn(part, text)
        self.assertIsNone(stale())

    def test_an_exec_that_fails_refuses_a_ledger_verb_by_name(self):
        with mock.patch("sys.stderr") as err:
            rc, banner = trunkroute.enter(
                ["dispatch", "send"],
                package_dir=os.path.join(self.lane, "helm"),
                environ={}, execve=_boom)
        self.assertEqual((rc, banner), (trunkroute.REFUSED_RC, False))
        entry = os.path.join(self.trunk, "bin", "helm")
        self.assertEqual(_written(err), _refusal_text(
            "dispatch", self.trunk,
            "starting %s failed (FileNotFoundError)"
            % trunkroute.exec_argv(self.trunk, ["dispatch"])[0],
            "check that %s runs" % entry) + "\n")

    def test_an_exec_that_fails_runs_chat_here_and_names_why(self):
        """A failed exec is a trunk that cannot be used: chat runs this tree
        with one line, and nothing else is written."""
        with mock.patch("sys.stderr") as err:
            rc, banner = trunkroute.enter(
                ["chat", "post"], package_dir=os.path.join(self.lane, "helm"),
                environ={}, execve=_boom)
        self.assertEqual((rc, banner), (None, False))
        text = _written(err)
        self.assertEqual(text.count("\n"), 1, text)
        self.assertFalse(text.startswith("[helm] REFUSED"), text)
        for part in ("trunk helm cannot be used", "(FileNotFoundError)",
                     "Fix: check that %s runs"
                     % os.path.join(self.trunk, "bin", "helm"), self.lane):
            self.assertIn(part, text)
        self.assertTrue(cli._STALE_TREE_SAID, "the stale line was not spent")


class ShapeTest(unittest.TestCase):
    def test_every_coordination_verb_is_a_real_verb(self):
        """A renamed verb must not silently fall out of the set."""
        self.assertEqual(sorted(trunkroute.COORDINATION_VERBS & set(cli.VERBS)),
                         sorted(trunkroute.COORDINATION_VERBS))
        self.assertGreater(len(trunkroute.COORDINATION_VERBS), 7)

    def test_the_brief_set_is_covered(self):  # noqa: VACUOUS_ASSERTION — exact equality: the intersection is the whole seven-verb brief set
        brief = {"dispatch", "chat", "work", "handoff", "task", "lr", "store"}
        self.assertEqual(brief & trunkroute.COORDINATION_VERBS, brief)

    def test_the_dogfood_verbs_stay_out(self):  # noqa: VACUOUS_ASSERTION — absence from the set IS the contract; the names are pinned real verbs so the check cannot pass on a typo
        """gate must run the gated tree's own code (gate._cross_tree_refusal)."""
        dogfood = {"gate", "train", "compose", "landgate", "doctor"}
        self.assertEqual(dogfood & set(cli.VERBS), dogfood)
        self.assertFalse(dogfood & trunkroute.COORDINATION_VERBS)

    def test_only_chat_runs_this_tree_when_trunk_cannot_be_used(self):
        """The integrator's ruling names chat alone: every other
        coordination verb writes a ledger that current readers fold."""
        self.assertEqual(sorted(trunkroute.FALLBACK_VERBS), ["chat"])
        self.assertIn("chat", trunkroute.COORDINATION_VERBS)

    def test_the_declaration_key_is_the_dispatch_folds(self):
        from helm import dispatches
        self.assertEqual(trunkroute.TRUNK_REF_KEY, dispatches._TRUNK_REF_KEY)

    def test_the_suite_plants_the_opt_in_under_this_name(self):
        """tests/__init__.py may not import helm, so it spells the name."""
        self.assertEqual(os.environ.get(trunkroute.OPT_IN), "1")

    def test_interpreter_flags(self):
        def flags(**kw):
            base = dict(isolated=0, ignore_environment=0, no_user_site=0,
                        no_site=0, dont_write_bytecode=0)
            base.update(kw)
            return types.SimpleNamespace(**base)
        f = trunkroute._interpreter_flags
        self.assertEqual(f(flags()), [])
        self.assertEqual(f(flags(no_site=1, dont_write_bytecode=1)),
                         ["-S", "-B"])
        self.assertEqual(f(flags(ignore_environment=1, no_user_site=1)),
                         ["-E", "-s"])
        self.assertEqual(f(flags(isolated=1, ignore_environment=1,
                                 no_user_site=1)), ["-I"])

    def test_exec_argv_uses_this_interpreter_else_the_entry(self):
        self.assertEqual(
            trunkroute.exec_argv("/t", ["chat"], executable="/py",
                                 flags=types.SimpleNamespace(
                                     isolated=0, ignore_environment=0,
                                     no_user_site=0, no_site=1,
                                     dont_write_bytecode=0)),
            ["/py", "-S", "/t/bin/helm", "chat"])
        self.assertEqual(trunkroute.exec_argv("/t", ["chat"], executable=""),
                         ["/t/bin/helm", "chat"])

    def test_only_the_process_entry_routes(self):
        """cli.main([...]) in process never execs: the hook dispatcher and
        every in-process test pass argv."""
        with mock.patch.object(trunkroute, "enter",
                               side_effect=AssertionError("routed")), \
                mock.patch.object(cli, "_main", return_value=0) as run:
            self.assertEqual(cli.main(["chat", "read"]), 0)
        run.assert_called_once()

    def test_the_process_entry_routes_and_passes_the_banner_on(self):
        with mock.patch.object(trunkroute, "enter",
                               return_value=(None, False)) as enter, \
                mock.patch.object(cli, "_main", return_value=0) as run, \
                mock.patch.object(sys, "argv", ["helm", "chat", "read"]):
            self.assertEqual(cli.main(), 0)
        enter.assert_called_once_with(["chat", "read"])
        run.assert_called_once_with(["chat", "read"], banner=False)

    def test_a_refused_route_is_the_exit_status(self):
        with mock.patch.object(trunkroute, "enter", return_value=(1, False)), \
                mock.patch.object(cli, "_main",
                                  side_effect=AssertionError("ran")), \
                mock.patch.object(sys, "argv", ["helm", "dispatch"]):
            self.assertEqual(cli.main(), 1)

    def test_an_installed_hook_entry_never_routes(self):  # noqa: VACUOUS_ASSERTION — enter is armed to raise, so reaching the handler's rc proves the route was never asked
        from helm import hooklatency
        with mock.patch.object(hooklatency, "entry_allowed",
                               return_value=False), \
                mock.patch.object(trunkroute, "enter",
                               side_effect=AssertionError("routed")), \
                mock.patch.object(cli, "_main", return_value=0), \
                mock.patch.object(sys, "argv",
                                  ["helm", "chat", "join", "--hook-json"]):
            self.assertEqual(cli.main(), 0)

    def test_no_banner_means_no_which_tree_line(self):
        with mock.patch.object(cli, "which_helm_warning",
                               return_value="[helm] you ran X") as warn, \
                mock.patch.object(cli, "stale_tree_warning",
                                  return_value=None) as stale, \
                mock.patch.dict(os.environ, {"HELM_NO_TREE_WARNING": "0"}), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr") as err:
            cli._main(["--version"], banner=False)
        warn.assert_not_called()
        stale.assert_called_once_with()          # the door still ran
        self.assertNotIn("you ran",
                         "".join(c.args[0] for c in err.write.call_args_list))


# ---------------------------------------------------------------------------
# end to end: real processes, real trees, the marker names who ran
# ---------------------------------------------------------------------------

_MARKER = '''

# ---- test fixture: this tree's marker (tests/test_trunkroute.py) ----
def _fixture_marker(args, _tag=%r):
    data = sys.stdin.buffer.read() if args[:1] == ["--stdin"] else b""
    sys.stderr.write(_tag + "-VERB-STDERR first line\\n")
    sys.stderr.flush()
    sys.stdout.write(_tag + "-MARKER " + repr(args) + " " + repr(data) + "\\n")
    return 7


VERBS["chat"] = _fixture_marker
VERBS["doctor"] = _fixture_marker
'''


def _copy_package(dest):
    copy_live_tree(os.path.join(REPO, "helm"), os.path.join(dest, "helm"))
    os.makedirs(os.path.join(dest, "bin"))
    shutil.copy2(os.path.join(REPO, "bin", "helm"),
                 os.path.join(dest, "bin", "helm"))
    with open(os.path.join(dest, ".gitignore"), "w") as f:
        f.write("__pycache__/\n")


def _mark(tree, tag):
    with open(os.path.join(tree, "helm", "cli.py"), "a") as f:
        f.write(_MARKER % tag)
    _commit(tree, "%s marker" % tag.lower())


class EndToEndTest(unittest.TestCase):
    """One repository for the class: `trunk` on main carries TRUNK's marker,
    the linked worktree `lane` carries LANE's, and neither carries the
    other's."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = os.path.realpath(tempfile.mkdtemp(prefix="trunkroute-e2e-"))
        cls.trunk = os.path.join(cls.tmp, "trunk")
        cls.lane = os.path.join(cls.tmp, "lane")
        try:
            _init(cls.trunk)
            _copy_package(cls.trunk)
            _commit(cls.trunk, "base")
            _git(cls.trunk, "config", "helm.trunkRef", "refs/heads/main")
            _git(cls.trunk, "worktree", "add", "-q", "-b", "lane/x", cls.lane)
            _mark(cls.lane, "LANE")
            _mark(cls.trunk, "TRUNK")
        except Exception:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("GIT_") and k not in (
                   trunkroute.OPT_IN, trunkroute.HOP, "HELM_NO_TREE_WARNING")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env.update(extra)
        return env

    def run_helm(self, argv, cwd, stdin=b"", env=None, via=None):
        cmd = via or [os.path.join(cwd, "bin", "helm")]
        return subprocess.run(cmd + argv, cwd=cwd, input=stdin,
                              capture_output=True, timeout=120,
                              env=env or self.env())

    # the cell the brief measured: the argv shape of a piped brief
    ARGV = [b"chat", b"--stdin", b"a b", b"q\"u'o te", b"", b"$HOME *",
            "é".encode(), b"\xff\xfe raw"]
    STDIN = b"BRIEF line one\n\x00\xff\xfe binary\r\nlast line no newline"

    def test_a_coordination_verb_from_a_lane_runs_trunks_code(self):
        p = self.run_helm(self.ARGV, self.lane, self.STDIN)
        self.assertIn(b"TRUNK-MARKER", p.stdout, p.stderr)
        self.assertNotIn(b"LANE-MARKER", p.stdout)
        self.assertNotIn(b"you ran", p.stderr)

    def test_the_trunk_run_is_byte_for_byte_what_trunk_prints_itself(self):
        """stdout, stderr and the exit status pass through exactly, and the
        verb's own first stderr line is still the first line."""
        routed = self.run_helm(self.ARGV, self.lane, self.STDIN)
        direct = self.run_helm(self.ARGV, self.lane, self.STDIN,
                               via=[os.path.join(self.trunk, "bin", "helm")])
        self.assertEqual(routed.returncode, 7)
        self.assertEqual((routed.returncode, routed.stdout, routed.stderr),
                         (direct.returncode, direct.stdout, direct.stderr))
        self.assertEqual(routed.stderr.splitlines()[0],
                         b"TRUNK-VERB-STDERR first line", routed.stderr)

    def test_argv_and_a_piped_brief_arrive_byte_for_byte(self):  # noqa: VACUOUS_ASSERTION — one exact equality on the whole stdout; the empty argument is part of the argv under test
        p = self.run_helm(self.ARGV, self.lane, self.STDIN)
        want = [os.fsdecode(a) for a in self.ARGV[1:]]
        self.assertEqual(p.stdout.decode("utf-8", "surrogateescape"),
                         "TRUNK-MARKER %r %r\n" % (want, self.STDIN))

    def test_python_dash_m_helm_from_a_lane_runs_trunk_too(self):
        p = self.run_helm(self.ARGV, self.lane, self.STDIN,
                          via=[sys.executable, "-m", "helm"])
        self.assertIn(b"TRUNK-MARKER", p.stdout, p.stderr)
        self.assertEqual(p.returncode, 7)

    def test_the_trunk_binary_from_a_lane_cwd_prints_no_banner(self):
        """The 812-call case: `helm` on PATH is trunk, the cwd is a lane."""
        p = self.run_helm([b"chat"], self.lane,
                          via=[os.path.join(self.trunk, "bin", "helm")])
        self.assertIn(b"TRUNK-MARKER", p.stdout)
        self.assertNotIn(b"you ran", p.stderr)
        self.assertEqual(p.stderr.splitlines()[0],
                         b"TRUNK-VERB-STDERR first line")

    def test_a_coordination_verb_in_the_trunk_runs_in_place(self):
        p = self.run_helm([b"chat"], self.trunk)
        self.assertEqual(p.returncode, 7)
        self.assertIn(b"TRUNK-MARKER", p.stdout)
        self.assertNotIn(b"you ran", p.stderr)

    def test_a_dogfood_verb_from_a_lane_runs_the_lanes_code(self):
        p = self.run_helm([b"doctor"], self.lane)
        self.assertIn(b"LANE-MARKER", p.stdout, p.stderr)
        self.assertNotIn(b"TRUNK-MARKER", p.stdout)

    def test_a_dogfood_verb_keeps_the_banner_where_it_fired(self):
        p = self.run_helm([b"doctor"], self.lane,
                          via=[os.path.join(self.trunk, "bin", "helm")])
        self.assertIn(b"TRUNK-MARKER", p.stdout)
        self.assertIn(b"you ran", p.stderr)

    def test_the_opt_in_runs_the_lanes_own_verb_and_says_so_once(self):
        p = self.run_helm([b"chat"], self.lane,
                          env=self.env(**{trunkroute.OPT_IN: "1"}))
        self.assertIn(b"LANE-MARKER", p.stdout, p.stderr)
        self.assertNotIn(b"TRUNK-MARKER", p.stdout)
        lines = p.stderr.splitlines()
        self.assertEqual(sum(trunkroute.OPT_IN.encode() in l for l in lines),
                         1, p.stderr)
        self.assertEqual(lines[1:], [b"LANE-VERB-STDERR first line"])
        self.assertNotIn(b"you ran", p.stderr)

    def test_the_opt_in_line_is_not_silenced_by_the_tree_warning_switch(self):
        p = self.run_helm([b"chat"], self.lane, env=self.env(
            **{trunkroute.OPT_IN: "1", "HELM_NO_TREE_WARNING": "1"}))
        self.assertIn(trunkroute.OPT_IN.encode(), p.stderr)

    def _detach_trunk(self):
        _git(self.trunk, "checkout", "-q", "--detach")
        self.addCleanup(_git, self.trunk, "checkout", "-q", "main")

    def test_an_unusable_trunk_refuses_a_ledger_verb_and_nothing_runs(self):
        """Byte for byte the refusal each ledger verb printed before chat
        could fall back: exit 1, no stdout, the one line."""
        self._detach_trunk()
        why, fix = _nowhere(self.trunk)
        got = [self.run_helm([verb.encode()], self.lane)
               for verb in LEDGER_VERBS]
        self.assertIn(b"[helm] REFUSED: the dispatch verb", got[0].stderr)
        self.assertEqual(
            [(p.returncode, p.stdout, p.stderr) for p in got],
            [(trunkroute.REFUSED_RC, b"",
              _refusal_text(verb, self.lane, why, fix).encode() + b"\n")
             for verb in LEDGER_VERBS])

    def fallback_line(self, p, tag):
        """The one line helm adds when chat falls back, asserted: rc and
        stdout are the tree's own chat, chat's own stderr follows the line,
        and nothing else is said (no refusal, banner or stale-tree line)."""
        self.assertEqual(p.returncode, 7, p.stderr)
        self.assertEqual(p.stdout, tag + b"-MARKER [] b''\n", p.stderr)
        lines = p.stderr.splitlines()
        self.assertEqual(lines[1:], [tag + b"-VERB-STDERR first line"],
                         p.stderr)
        self.assertTrue(lines[0].startswith(FALLBACK_HEAD.encode()), lines)
        for absent in (b"REFUSED", b"you ran", b"behind origin/main"):
            self.assertNotIn(absent, p.stderr)
        return lines[0]

    def test_an_unusable_trunk_runs_the_lanes_chat_with_one_line(self):
        """The branch checked out nowhere, from a lane that is behind
        origin/main: the lane's chat runs, and the one line is the only
        line helm adds (no tree-behind-trunk line beside it)."""
        self._detach_trunk()
        _git(self.trunk, "update-ref", "refs/remotes/origin/main",
             "refs/heads/main")
        self.addCleanup(_git, self.trunk, "update-ref", "-d",
                        "refs/remotes/origin/main")
        # the control: the door speaks for this lane on a verb it runs
        doctor = self.run_helm([b"doctor"], self.lane)
        self.assertIn(b"LANE-MARKER", doctor.stdout, doctor.stderr)
        self.assertIn(b"behind origin/main", doctor.stderr)
        line = self.fallback_line(self.run_helm([b"chat"], self.lane),
                                  b"LANE")
        why, fix = _nowhere(self.trunk)
        self.assertIn(why.encode(), line)
        self.assertIn(fix.encode(), line)
        self.assertIn(b"instead (%s)" % self.lane.encode(), line)

    def test_the_trunk_checkout_off_its_branch_still_runs_chat_from_path(self):
        """The overnight case: `helm` on PATH is the shared checkout, it
        left main, and a seat stands in its lane. That checkout's own chat
        runs, and the which-tree banner stays off."""
        self._detach_trunk()
        via = [os.path.join(self.trunk, "bin", "helm")]
        # the control: the banner fires for this binary from this cwd
        doctor = self.run_helm([b"doctor"], self.lane, via=via)
        self.assertIn(b"you ran", doctor.stderr)
        line = self.fallback_line(self.run_helm([b"chat"], self.lane, via=via),
                                  b"TRUNK")
        why, fix = _nowhere(self.trunk)
        self.assertIn(why.encode(), line)
        self.assertIn(fix.encode(), line)
        self.assertIn(b"instead (%s)" % self.trunk.encode(), line)

    def test_a_trunk_without_bin_helm_runs_chat_and_refuses_the_rest(self):
        entry = os.path.join(self.trunk, "bin", "helm")
        os.rename(entry, entry + ".away")
        self.addCleanup(os.rename, entry + ".away", entry)
        why, fix = _no_entry(self.trunk)
        line = self.fallback_line(self.run_helm([b"chat"], self.lane), b"LANE")
        self.assertIn(why.encode(), line)
        self.assertIn(fix.encode(), line)
        d = self.run_helm([b"dispatch"], self.lane)
        self.assertIn(b"no runnable bin/helm", d.stderr)
        self.assertEqual(
            (d.returncode, d.stdout, d.stderr),
            (trunkroute.REFUSED_RC, b"",
             _refusal_text("dispatch", self.lane, why, fix).encode() + b"\n"))


if __name__ == "__main__":
    unittest.main()
