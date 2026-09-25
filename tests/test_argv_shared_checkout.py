#!/usr/bin/env python3
"""Two argv-guard rungs that read WHERE a command runs and WHO runs it.

THE SHARED-CHECKOUT RUNG (task/3057). A `git stash pop` meant for a lane ran
in helm's shared checkout and left a conflict marker in a module every helm
verb imports, so every verb and hook in the fleet raised SyntaxError until a
person cleaned the tree. Two facts made it easy: git's stash is ONE list for
every worktree of a repository, and the Bash tool puts a seat back in the
shared checkout after every call. So a working-tree git verb whose effective
directory is the shared primary checkout of a registered repository is
refused, and the refusal spells the same verb with `git -C <lane>`.

THE DELEGATE RUNG. A subagent a seat delegated a read to wrote ledger
verdicts beyond its brief, one of them an APPROVE, which the ledger never
takes back. task/1388 built a rung for it here, and it was folded into the
ONE delegate rung, task/3060's sidechain authority rung (helm/chat.py,
helm/delegate_grant.py): a tool call whose payload carries agent_id may not
run a verdict-class write unless its seat granted it. That rung's own arms,
task/1388's among them, are in tests/test_delegate_authority.py; the matrix
here keeps it as rung B for the axis only this file has, that it decides on
who runs a command and never on the directory.

THE MATRIX IS THE TASK'S OWN: both rungs x (main seat, subagent) x eight
states, one subTest per cell, through the shipped entry `cmd_argv_guard`. The
rig is real: a registered repository with a linked lane, an unregistered one
with a lane, and a registered one with no lane, all made with git.
"""
import contextlib
import io
import json
import os as _os
import shutil
import subprocess
import sys as _sys
import tempfile
import unittest
from unittest import mock

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-sharedtree-", var="HELM_HOME")

from helm import chat, home, pk  # noqa: E402

RIG = {}
_SAVED = {}


def _git(where, *args):
    p = subprocess.run(["git", "-C", where] + list(args),
                       capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError("git %s: %s" % (" ".join(args), p.stderr))


def _repo(root, lane=None, rail=True):
    """A git repository at `root` with one commit, and a linked worktree at
    `<root>-wt/<lane>` when a lane is named. `rail` declares the guard
    profile that puts a repository under the shared-checkout rail."""
    _os.makedirs(root)
    _git(root, "init", "-q", "-b", "main", ".")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    if rail:
        _git(root, "config", "helm.guard.profile", "rail")
    with open(_os.path.join(root, "a.txt"), "w") as f:
        f.write("base\n")
    _git(root, "add", "-A")
    _git(root, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "base")
    if lane:
        path = _os.path.join(root + "-wt", lane)
        _git(root, "worktree", "add", "-q", "-b", "lane/" + lane, path)
        return path
    return None


def setUpModule():
    tmp = _os.path.realpath(tempfile.mkdtemp(prefix="helm-test-sharedtree-"))
    RIG["tmp"] = tmp
    RIG["shared"] = _os.path.join(tmp, "shared")
    RIG["lane"] = _repo(RIG["shared"], "lane")
    RIG["other"] = _os.path.join(tmp, "other")
    RIG["other_lane"] = _repo(RIG["other"], "lane")
    RIG["solo"] = _os.path.join(tmp, "solo")
    _repo(RIG["solo"])
    RIG["project"] = _os.path.join(tmp, "project")
    RIG["project_lane"] = _repo(RIG["project"], "lane", rail=False)
    RIG["sub"] = _os.path.join(RIG["shared"], "helm", "work")
    _os.makedirs(RIG["sub"])
    RIG["link"] = _os.path.join(tmp, "shared-link")
    _os.symlink(RIG["shared"], RIG["link"])
    RIG["gone"] = _os.path.join(tmp, "no-such-dir", "x")
    path = home.registry_path()
    _os.makedirs(_os.path.dirname(path), exist_ok=True)
    if _os.path.exists(path):
        with open(path) as f:
            _SAVED["registry"] = f.read()
    pk.write_json(path, {"version": 1, "projects": {
        "rigshared": {"name": "rigshared", "path": RIG["shared"]},
        "rigsolo": {"name": "rigsolo", "path": RIG["solo"]},
        "rigproject": {"name": "rigproject", "path": RIG["project"]}}})


def tearDownModule():
    path = home.registry_path()
    if "registry" in _SAVED:
        with open(path, "w") as f:
            f.write(_SAVED.pop("registry"))
    elif _os.path.exists(path):
        _os.remove(path)
    shutil.rmtree(RIG.get("tmp") or "/nonexistent", ignore_errors=True)


class _Env(unittest.TestCase):
    """No integrator declaration leaks in from the runner, and the pass
    path's steer latches land in a directory this test owns."""

    def setUp(self):
        chat_dir = tempfile.mkdtemp(prefix="helm-test-sharedtree-chat-")
        self.addCleanup(shutil.rmtree, chat_dir, True)
        env = mock.patch.dict(_os.environ, {"HELM_CHAT_DIR": chat_dir})
        env.start()
        self.addCleanup(env.stop)
        _os.environ.pop("HELM_WORK_INTEGRATOR", None)

    def hook(self, command, cwd=None, agent_id=None, tool="Bash"):
        """(rc, stdout+stderr) of the shipped PreToolUse entry."""
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool,
                   "session_id": "sess-sharedtree",
                   "tool_use_id": "toolu_sharedtree",
                   "tool_input": {"command": command}}
        if cwd is not None:
            payload["cwd"] = cwd
        if agent_id is not None:
            payload["agent_id"] = agent_id
            payload["agent_type"] = "general-purpose"
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(_sys, "stdin",
                               io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = chat.cmd_argv_guard([])
        return rc, out.getvalue() + err.getvalue()


TREE_ACT = "git stash pop"
LEDGER_ACT = ("helm dispatch verdict d-17 TIP --approve --measured "
              "gate:tok evidence")


def _states():
    """(state, cwd, command for rung A, command for rung B). Rung B reads a
    directory only for a relative HELM_HOME, which none of these spells, so
    its command carries the state's own prefix before the act and the state
    changes nothing it decides."""
    shared, lane, other = RIG["shared"], RIG["lane"], RIG["other"]
    return (
        ("shared checkout cwd", shared, TREE_ACT, LEDGER_ACT),
        ("lane worktree cwd", lane, TREE_ACT, LEDGER_ACT),
        ("cd-into-lane prefix", shared, "cd %s && %s" % (lane, TREE_ACT),
         "cd %s && %s" % (lane, LEDGER_ACT)),
        ("git -C lane", shared, "git -C %s stash pop" % lane,
         "git -C %s log -1 && %s" % (lane, LEDGER_ACT)),
        ("git -C shared", lane, "git -C %s stash pop" % shared,
         "git -C %s log -1 && %s" % (shared, LEDGER_ACT)),
        ("HELM_WORK_INTEGRATOR=1", shared,
         "HELM_WORK_INTEGRATOR=1 " + TREE_ACT,
         "HELM_WORK_INTEGRATOR=1 " + LEDGER_ACT),
        ("non-helm repo", other, TREE_ACT, LEDGER_ACT),
        ("unreadable cwd", RIG["gone"], TREE_ACT, LEDGER_ACT),
    )


# rung A refuses in exactly these states, for the seat and a subagent alike
_TREE_REFUSES = frozenset(("shared checkout cwd", "git -C shared"))


class SurfaceByStateMatrixTest(_Env):
    """Both rungs x (main seat, subagent) x the eight states. Rung A decides
    on the directory the git verb runs in and never on who runs it; rung B
    decides on who runs it and never on the directory."""

    def test_every_cell(self):  # noqa: VACUOUS_ASSERTION — every cell of a finite matrix executes, and the matrix holds both refusing and passing cells on the same entry
        cells = 0
        for state, cwd, tree_cmd, ledger_cmd in _states():
            for actor, agent in (("main seat", None), ("subagent", "a1b2")):
                for rung, command, refuse in (
                        ("A", tree_cmd, state in _TREE_REFUSES),
                        ("B", ledger_cmd, agent is not None)):
                    with self.subTest(rung=rung, actor=actor, state=state):
                        cells += 1
                        rc, out = self.hook(command, cwd, agent)
                        if refuse:
                            self.assertEqual(rc, 2, out)
                            self.assertIn("[helm argv-guard] BLOCKED", out)
                            self.assertIn(
                                "shared checkout" if rung == "A"
                                else "--reviewer-run", out)
                        else:
                            self.assertEqual(rc, 0, out)
                            self.assertNotIn("BLOCKED", out)
        self.assertEqual(cells, 32)

    def test_the_integrator_declared_in_the_environment(self):
        """The seat's own environment clears rung A, and never rung B: a
        subagent inherits its seat's environment whole, so a declaration
        there would open the ledger to every reader it delegates to."""
        rc, out = self.hook(TREE_ACT, RIG["shared"])
        self.assertEqual(rc, 2, out)
        with mock.patch.dict(_os.environ, {"HELM_WORK_INTEGRATOR": "1"}):
            rc, out = self.hook(TREE_ACT, RIG["shared"])
            self.assertEqual((rc, "BLOCKED" in out), (0, False), out)
            rc, out = self.hook(TREE_ACT, RIG["shared"], "a1b2")
            self.assertEqual((rc, "BLOCKED" in out), (0, False), out)
            rc, out = self.hook(LEDGER_ACT, RIG["shared"], "a1b2")
            self.assertEqual(rc, 2, out)
            rc, out = self.hook(LEDGER_ACT, RIG["shared"])
            self.assertEqual((rc, "BLOCKED" in out), (0, False), out)

    def test_monitor_runs_the_same_shell(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts rc 2 and a refusal
        for rung, command, agent in (("A", TREE_ACT, None),
                                     ("B", LEDGER_ACT, "a1b2")):
            with self.subTest(rung=rung):
                rc, out = self.hook(command, RIG["shared"], agent,
                                    tool="Monitor")
                self.assertEqual(rc, 2, out)
                self.assertIn("BLOCKED", out)


class SharedCheckoutRungTest(_Env):
    """`shared_checkout_refusal` over the verbs, the directory arithmetic
    and the text that is not a command."""

    def refused(self, command, cwd=None):
        return chat.shared_checkout_refusal(command, cwd or RIG["shared"])

    def test_every_working_tree_verb_is_refused_in_the_shared_checkout(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        for command in (
                "git stash pop", "git stash apply", "git stash drop",
                "git stash apply --index", "git stash branch fix",
                "git checkout -- a.txt", "git checkout main",
                "git checkout HEAD~1 a.txt", "git checkout .",
                "git restore a.txt", "git restore --staged a.txt",
                "git reset --hard", "git reset --hard origin/main",
                "git reset --merge", "git reset --keep HEAD~1",
                "git merge lane/x", "git merge --no-ff lane/x",
                "git merge --abort", "git rebase main", "git rebase --continue",
                "git cherry-pick abc", "git revert HEAD", "git am < p.mbox",
                "git switch lane/x", "git switch -c new", "git clean -fd",
                "git clean -fdx", "git pull", "git pull --rebase"):
            with self.subTest(command=command):
                hit = self.refused(command)
                self.assertIsNotNone(hit, command)
                self.assertEqual(hit[0], RIG["shared"])

    def test_reads_and_the_listed_safe_verbs_pass(self):  # noqa: VACUOUS_ASSERTION — the first assertion is the refusal on the same rung in the same directory
        self.assertIsNotNone(self.refused("git stash pop"))
        for command in (
                "git status", "git status --short", "git log --oneline -5",
                "git diff", "git diff HEAD~1", "git show HEAD",
                "git fetch", "git fetch origin", "git pull --ff-only",
                "git pull --ff-only origin main", "git merge --ff-only origin/main",
                "git worktree add ../shared-wt/x -b lane/x",
                "git worktree remove ../shared-wt/x", "git worktree list",
                "git branch -a", "git branch --show-current",
                "git rev-parse HEAD", "git merge-base HEAD main",
                "git stash list", "git stash show -p", "git stash",
                "git stash push -m wip", "git reset", "git reset --soft HEAD~1",
                "git reset HEAD a.txt", "git clean -n", "git clean -nd",
                "git clean --dry-run -d", "git checkout", "git checkout -h",
                "git stash pop --help", "git cherry-pick -h"):
            with self.subTest(command=command):
                self.assertIsNone(self.refused(command), command)

    def test_the_effective_directory(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and the refusing and passing arms share the rung and the rig
        shared, lane, sub = RIG["shared"], RIG["lane"], RIG["sub"]
        rel_lane = _os.path.relpath(lane, shared)
        rel_shared = _os.path.relpath(shared, lane)
        for command, cwd, refuse in (
                (TREE_ACT, sub, True),                      # below the top
                (TREE_ACT, RIG["link"], True),              # a symlink to it
                ("cd %s && %s" % (shared, TREE_ACT), lane, True),
                ("cd %s; %s" % (rel_shared, TREE_ACT), lane, True),
                ("cd %s\n%s" % (lane, TREE_ACT), shared, False),
                ("cd %s && %s" % (rel_lane, TREE_ACT), shared, False),
                ("git -C %s stash pop" % rel_lane, shared, False),
                ("git -C %s stash pop" % rel_shared, lane, True),
                ("git -C %s -C %s stash pop" % (RIG["tmp"], "shared"),
                 lane, True),
                ("git -C %s -C %s stash pop" % (RIG["tmp"], "shared-wt/lane"),
                 shared, False),
                ("(cd %s && %s); %s" % (lane, TREE_ACT, TREE_ACT),
                 shared, True),                             # the scope closes
                ("(cd %s && %s)" % (lane, TREE_ACT), shared, False),
                ("cd %s | cat; %s" % (lane, TREE_ACT), shared, True),
                ("pushd %s && %s" % (lane, TREE_ACT), shared, False),
                # a cd after || runs only when the one before it failed, so
                # the directory after it is not settled
                ("cd %s 2>/dev/null || cd %s\n%s" % (lane, shared, TREE_ACT),
                 shared, False),
                ("cd %s || exit 1\n%s" % (shared, TREE_ACT), lane, True),
                ("cd %s && git -C %s stash pop" % (lane, shared), lane, True),
                ("sudo -u me git stash pop", shared, True),
                ("timeout 60 /usr/bin/git -c core.x=y stash pop", shared, True),
                ("bash -c 'git stash pop'", shared, True),
                ("bash -c 'cd %s && git stash pop'" % lane, shared, False),
                ("x=$(git stash pop)", shared, True),
                ("echo \"$(cd %s && git stash pop)\"" % lane, shared, False),
                ("eval git stash pop", shared, True),
                ("bash <<'EOF'\ngit stash pop\nEOF", shared, True),
                ("cat <<'EOF'\ngit stash pop\nEOF", shared, False),
                (TREE_ACT, RIG["solo"], False),     # registered, no lane
                (TREE_ACT, RIG["project"], False),  # registered, no rail
                (TREE_ACT, RIG["other_lane"], False),
                ("cd %s && %s" % (RIG["other"], TREE_ACT), shared, False)):
            with self.subTest(command=command, cwd=cwd):
                hit = chat.shared_checkout_refusal(command, cwd)
                if refuse:
                    self.assertIsNotNone(hit, command)
                    self.assertEqual(hit[0], shared)
                else:
                    self.assertIsNone(hit, command)

    def test_only_a_repository_under_the_rail_is_a_shared_checkout(self):
        """A registered project with lanes whose repository declares no rail
        is worked in its own checkout by its lead: helm's shared-checkout law
        is opt-in there, as the guard profiles are. The same repository
        refuses once it declares the rail, and passes again with the leak
        profile."""
        project = RIG["project"]
        self.addCleanup(subprocess.run, ["git", "-C", project, "config",
                                         "--unset", "helm.guard.profile"],
                        capture_output=True)
        _git(project, "config", "helm.guard.profile", "rail")
        self.assertEqual(chat.shared_checkout_refusal(TREE_ACT, project),
                         (project, "stash pop"))
        _git(project, "config", "helm.guard.profile", "leak")
        self.assertIsNone(chat.shared_checkout_refusal(TREE_ACT, project))
        _git(project, "config", "--unset", "helm.guard.profile")
        self.assertIsNone(chat.shared_checkout_refusal(TREE_ACT, project))

    def test_the_rail_key_is_the_guard_profiles_own(self):
        """The rung asks git for the key the guard records, and a profile
        name the guard knows: the two may not drift apart."""
        from helm.work import _guard
        self.assertEqual(chat._RAIL_KEY, _guard.PROFILE_KEY)
        self.assertIn(chat._RAIL, _guard.GUARD_PROFILES)

    def test_a_home_relative_directory(self):
        """`~` and `$HOME` spell the directory bash expands them to."""
        tmp = RIG["tmp"]
        with mock.patch.dict(_os.environ, {"HOME": tmp}):
            self.assertIsNotNone(chat.shared_checkout_refusal(
                "cd ~/shared && git stash pop", RIG["lane"]))
            self.assertIsNotNone(chat.shared_checkout_refusal(
                "git -C \"$HOME/shared\" stash pop", RIG["lane"]))
            self.assertIsNone(chat.shared_checkout_refusal(
                "cd ~/shared-wt/lane && git stash pop", RIG["shared"]))

    def test_what_the_text_does_not_settle_passes(self):  # noqa: VACUOUS_ASSERTION — the first assertion is the refusal on the same rung in the same directory
        """A directory the text does not name, a repository chosen by
        --git-dir or GIT_DIR, and text the shell reader cannot settle all
        pass: a guard on every Bash call misses an exotic edge rather than
        block work it cannot read."""
        self.assertIsNotNone(self.refused(TREE_ACT))
        for command in (
                "git -C \"$d\" stash pop", "cd \"$d\" && git stash pop",
                "cd - && git stash pop", "popd && git stash pop",
                "for d in a b; do git -C $d stash pop; done",
                "env -C %s git stash pop" % RIG["lane"],
                "git --git-dir=%s/.git stash pop" % RIG["shared"],
                "git --work-tree=/x stash pop",
                "GIT_DIR=/x/.git git stash pop",
                "case $x in a) git stash pop;; esac",
                "echo 'git stash pop'", "git commit -m 'git stash pop'",
                "grep -n 'git stash pop' notes.md",
                "helm chat post --room r <<'EOF'\ngit stash pop\nEOF"):
            with self.subTest(command=command):
                self.assertIsNone(self.refused(command), command)

    def test_an_unreadable_cwd_passes(self):  # noqa: VACUOUS_ASSERTION — the first assertion is the refusal on the same rung and command
        self.assertIsNotNone(chat.shared_checkout_refusal(
            TREE_ACT, RIG["shared"]))
        for cwd in (RIG["gone"], None, "", "relative/dir", 42,
                    _os.path.join(RIG["shared"], "a.txt")):
            with self.subTest(cwd=cwd):
                self.assertIsNone(chat.shared_checkout_refusal(TREE_ACT, cwd))
        if _os.geteuid() == 0:
            return                   # root reads a mode-000 directory anyway
        locked = _os.path.join(RIG["shared"], "locked")
        _os.makedirs(locked)
        _os.chmod(locked, 0)
        self.addCleanup(_os.rmdir, locked)
        self.addCleanup(_os.chmod, locked, 0o700)
        self.assertIsNone(chat.shared_checkout_refusal(TREE_ACT, locked))

    def test_a_defect_in_the_rung_fails_open_and_leaves_the_later_rungs(self):
        """The rung stands before the substitution guard, so a defect that
        escaped it would pass the command the substitution guard refuses."""
        hazard = 'git commit -m "the `date` phrase"'
        rc, out = self.hook(hazard, RIG["lane"])
        self.assertEqual(rc, 2, out)
        self.assertIsNotNone(self.refused(TREE_ACT))
        with mock.patch.object(chat, "_commands_run",
                               side_effect=RuntimeError("defect")):
            self.assertIsNone(self.refused(TREE_ACT))
            rc, out = self.hook(hazard, RIG["lane"])
            self.assertEqual(rc, 2, out)
            self.assertIn("backtick", out)

    def test_the_refusal_spells_the_lane_route(self):
        rc, out = self.hook("git stash pop --index", RIG["shared"])
        self.assertEqual(rc, 2, out)
        self.assertIn(RIG["shared"] + ", the shared checkout", out)
        self.assertIn("git -C %s-wt/<lane> stash pop --index" % RIG["shared"],
                      out)
        self.assertIn("git stash is one list for every worktree", out)
        self.assertIn("HELM_WORK_INTEGRATOR=1", out)


class EntryImportTest(unittest.TestCase):
    """THE HOOK BUDGET, through the real entry script: rung A reads the
    registry only for a working-tree verb whose directory is a primary
    checkout with lanes, so the verb run in a lane loads nothing more than
    `ls` does. (The delegate rung's arm is in test_delegate_authority.)"""

    ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))

    def run_hook(self, command, cwd, agent_id=None):
        probe = (
            "import json, sys, runpy\n"
            "sys.argv = ['helm', 'chat', 'argv-guard', '--hook-json']\n"
            "rc = 0\n"
            "try:\n"
            "    runpy.run_path(%r, run_name='__main__')\n"
            "except SystemExit as e:\n"
            "    rc = e.code or 0\n"
            "sys.stderr.write('PROBE ' + json.dumps({'rc': rc, 'mods': "
            "sorted(sys.modules)}) + '\\n')\n"
            % _os.path.join(self.ROOT, "bin", "helm"))
        payload = {"tool_name": "Bash", "session_id": "sharedtree-arm",
                   "cwd": cwd, "tool_input": {"command": command}}
        if agent_id:
            payload["agent_id"] = agent_id
        env = dict(_os.environ, HELM_NO_TREE_WARNING="1")
        env.pop("HELM_WORK_INTEGRATOR", None)
        p = subprocess.run([_sys.executable, "-c", probe],
                           input=json.dumps(payload), capture_output=True,
                           text=True, env=env)
        line = [l for l in p.stderr.splitlines() if l.startswith("PROBE ")]
        self.assertTrue(line, "the probe never reported: " + p.stderr[-800:])
        report = json.loads(line[-1][len("PROBE "):])
        return report["rc"], set(report["mods"]), p.stderr

    def test_the_registry_is_read_only_for_a_candidate(self):
        rc, refused, err = self.run_hook(TREE_ACT, RIG["shared"])
        self.assertEqual(rc, 2, err[-600:])
        self.assertIn("helm.registry", refused)
        rc, passed, err = self.run_hook("git -C %s stash pop" % RIG["lane"],
                                        RIG["shared"])
        self.assertEqual(rc, 0, err[-600:])
        self.assertIn("helm.chat", passed)
        self.assertNotIn("helm.registry", passed)
        rc, plain, err = self.run_hook("ls -la", RIG["shared"])
        self.assertEqual(rc, 0, err[-600:])
        self.assertEqual(sorted(passed - plain), [])


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
