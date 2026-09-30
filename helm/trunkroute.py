#!/usr/bin/env python3
"""Coordination verbs run TRUNK helm, from any tree (task/3382).

THE DEFECT, MEASURED (a census of three local seats' transcripts, 40.5 h and
about 12,000 tool calls). 812 helm calls, 17-28 % per seat, carried the
maker banner "[helm] you ran .../helm@X from <lane>"
(`cli.which_helm_warning`).
In 170 failures it was the FIRST line, above the real error, and it tells the
reader to use `./bin/helm`. Seats did. Up to 78 ledger writes then exited 0
from lane trees a median 11 (max 397) commits behind main, and current readers
DROP such rows: an older fold does not advance a row's seq past events it has
no arm for, so the seq it writes reuses one already taken
(`dispatches.KNOWN_EVENT_KINDS`, `cli.stale_tree_warning`). Dispatch verdicts,
holds and hand-backs were lost without a word.

THE BANNER IS RIGHT FOR A DOGFOOD AND WRONG FOR A LEDGER WRITE. It exists so a
maker who runs another tree's binary knows the demo tested the wrong code. A
ledger write has the opposite need: it must come from the code every reader
folds with, which is trunk. So a COORDINATION verb (`COORDINATION_VERBS`)
reaching a helm process entry does this:

  * from the TRUNK checkout (the binary on PATH, `~/.local/bin/helm`): it runs
    in place, and the banner is not printed, wherever the cwd is. That removes
    the line from the 812 calls, and with it the advice that caused the stale
    writes.
  * from any OTHER tree of the same repository (`./bin/helm` in a lane or a
    seat home, `python3 -m helm` from one): the process replaces itself with
    trunk's `bin/helm` through `os.execve`. The pid, cwd, environment, stdin,
    stdout and stderr are the same objects, so a piped brief arrives byte for
    byte, a refusal's text is still the first line, and the exit status is
    trunk's own.
  * with `HELM_LANE_COORDINATION=1`: the tree's own code runs (a lane that
    changes dispatch itself must be able to drive it), and ONE line on stderr
    says so. `HELM_NO_TREE_WARNING` does not silence that line: the two
    variables answer different questions, as `gate._cross_tree_refusal`
    already rules for its own override.
  * when trunk is DECLARED but cannot be found or read: REFUSED, nothing runs,
    and the message names the fix. Running the tree's own code instead is
    exactly the silent stale write this module exists to end, and a refusal is
    loud, recoverable and names the opt-in for the case where it is wanted.
  * EXCEPT CHAT (`FALLBACK_VERBS`, the integrator's ruling): there the tree's
    own chat runs, and ONE line on stderr says trunk cannot be used, why, the
    fix, and which tree ran. If the shared checkout leaves its branch, a
    fleet-wide refusal of `helm chat` takes down the one channel seats use to
    coordinate the fix, and the one the owner and the integrator are reached
    by overnight. Chat from stale code has no measured loss; the ledger
    verbs' stale rows do, so they stay refused.

Every other verb keeps today's behaviour, banner included: running a lane's
own code is the point of a dogfood, and `helm gate` refuses a cross-tree run
outright (`gate._cross_tree_refusal`).

"TRUNK" IS THE REPOSITORY'S DECLARED AUTHORITY, NEVER A PATH. The repository
declares its trunk as `helm.trunkRef` (with `helm.trunkRemote`), the same
declaration the retip binding, the land window and `helm doctor`'s trunk
authority rung read (`dispatches._declared_authority`). The trunk CHECKOUT is
the one worktree of the running binary's repository that has that branch
checked out, found with `git worktree list`. Nothing here names a directory.
This reads the DECLARATION only; it never observes the remote, because a fetch
in front of a command is not acceptable, and the question here is "which
checkout", not "how fresh is it" (`cli.stale_tree_warning` answers that one).

A REPOSITORY THAT DECLARES NOTHING KEEPS TODAY'S BEHAVIOUR. An adopter's clone,
a contributor's fork and the fab node mirrors declare no trunk, and the
dispatch fold already reads "undeclared" as a gap and not as a contradiction.
There is nothing to route to, so nothing is routed and the banner is unchanged.

ONLY THE PROCESS ENTRY ROUTES. `cli.main` calls `enter` when it was invoked
with no argv (`bin/helm`, `python3 -m helm`); an in-process caller that passes
argv (the hook dispatcher, every test that calls `cli.main([...])`) never
re-execs, because replacing a process that called a function is never
correct. The installed hook entries (`cli._hook_selector`) are exempt as well:
the installed hook command names the checkout whose `bin/helm` it runs, and
they are latency-budgeted.

NO LOOP. The parent puts the trunk it chose in `HELM_TRUNK_REEXEC` for the one
exec; the child removes it from its environment before anything else, asks
the same question again, runs in place when it is that trunk, and refuses
when it is not. It never execs a second time, the verb's own children never
inherit the variable, and set by hand it is not a way around the route.
"""
import collections
import os
import sys

#: The verbs whose handlers WRITE a ledger that other seats fold. The first
#: seven are the brief's (task/3382); the rest write one of those stores or a
#: shared ledger of their own:
#:   dispatch  the dispatch ledger: sends, verdicts, holds, hand-backs
#:   chat      the chat bus: posts, DMs, acks, receipts, cursors, beacons
#:   work      work claims and lane leases
#:   handoff   the typed handoff journal; `now` is the same module's writer
#:   task      the task ledger
#:   lr        the land-request ledger (closes, backfills)
#:   store     the typed store; `premise` and `index` write the same store
#:   goal      the goals ledger
#:   asks, decide   the owner-asks and decision ledgers
#:   delegate  the delegation grants
#:   note      the fleet notes
#:   away, back     the owner's posture, read by every seat's wait surfaces
#: LEFT OUT ON PURPOSE: `gate` (a receipt must describe the tree whose helm
#: ran; `gate._cross_tree_refusal`), `train`/`compose`/`landgate` (the
#: integrator's land flow, run from trunk and bound to the declared authority
#: already), and the read-only projections (`owed`, `ready`, `board`,
#: `beacons`): a stale reader misreports but loses nothing, and the banner
#: still says which tree ran.
COORDINATION_VERBS = frozenset((
    "dispatch", "chat", "work", "handoff", "now", "task", "lr", "store",
    "premise", "index", "goal", "asks", "decide", "delegate", "note",
    "away", "back",
))

#: The coordination verbs that run THIS tree's own code when trunk is
#: declared and cannot be used, where every other one is refused (the
#: integrator's ruling on task/3382). Chat is how seats coordinate the fix
#: for a trunk checkout that left its branch, and how the owner and the
#: integrator are reached overnight, so refusing it fleet-wide takes the
#: channel down with the checkout; chat from stale code has no measured loss.
#: The ledger verbs keep the refusal: up to 78 stale rows were measured lost.
FALLBACK_VERBS = frozenset(("chat",))

#: The explicit opt-in: run THIS tree's own coordination verb.
OPT_IN = "HELM_LANE_COORDINATION"

#: The trunk the parent chose, for the child of ONE exec. Internal.
HOP = "HELM_TRUNK_REEXEC"

#: The declaration's keys, as `git config` reports them. The same keys
#: `dispatches._TRUNK_REF_KEY` names, pinned equal by a test rather than
#: imported: importing the dispatch module costs this path far more than the
#: whole route.
TRUNK_REF_KEY = "helm.trunkref"

#: The exit status of a refusal: the verb did not run.
REFUSED_RC = 1

PASS = "pass"      # not routed: today's behaviour, banner included
HERE = "here"      # this IS trunk: run in place, no banner
TRUNK = "trunk"    # exec trunk's bin/helm
OWN = "own"        # the opt-in: run this tree, say so in one line
REFUSE = "refuse"  # trunk is declared and cannot be used: run nothing
FALLBACK = "fallback"  # the same, for a FALLBACK_VERBS verb: run this tree


#: (action, trunk, line, tree). `trunk` is the trunk checkout when one is
#: known; `line` is what goes to stderr for OWN, REFUSE and FALLBACK; `tree`
#: is the checkout whose code would run here, on the TRUNK and FALLBACK
#: routes (a failed exec falls back to it).
Route = collections.namedtuple("Route", "action trunk line tree",
                               defaults=(None, None, None))


class _Undeclared(Exception):
    """The repository declares no trunk: there is nothing to route to."""


class _Unusable(Exception):
    """Trunk is declared and cannot be used. args: (why, fix)."""


def _git(tree, *args):
    """(rc, out) through helm's git seam, with the repository-selecting
    variables scrubbed (`selfrepo._git_env`): a helm run from inside a git
    hook inherits GIT_DIR, and this question is about the binary's own tree.
    `out` is decoded but NOT stripped: a blank first line is a declared empty
    value, and stripping it is how `dispatches._declared_authority` once read
    two conflicting declarations as one. rc is None when git could not be
    ASKED, which is never "absent"."""
    from . import selfrepo, vcs
    try:
        rc, out, _err = vcs.backend(tree).run(tree, *args, timeout=10,
                                              env=selfrepo._git_env())
    except Exception:            # noqa: BLE001 — a route never crashes
        return None, ""
    if rc is None or rc < 0:
        return None, ""
    return rc, os.fsdecode(out or b"")


def _declared_ref(tree):
    """The declared trunk source ref, or raises _Undeclared / _Unusable.

    --local and --get-all for the reasons `dispatches._declared_authority`
    gives: an authority is a property of the repository, not of a user's
    ~/.gitconfig, and a key declared twice with different values is
    ambiguous rather than whichever one git printed last."""
    rc, out = _git(tree, "config", "--local", "--get-all", TRUNK_REF_KEY)
    if rc == 1 and not out:
        raise _Undeclared()
    if rc != 0:
        raise _Unusable("git could not read helm.trunkRef in %s" % tree,
                        "check that %s is a readable git checkout" % tree)
    values = set(out.split("\n")[:-1] if out.endswith("\n")
                 else out.split("\n"))
    if len(values) > 1:
        raise _Unusable(
            "helm.trunkRef is declared more than once with different values "
            "(%s)" % ", ".join(sorted(values)),
            "keep one: git -C %s config --local --replace-all helm.trunkRef "
            "refs/heads/<branch>" % tree)
    ref = values.pop()
    from . import vcs
    bad = vcs.canonical_source_refusal(ref) if ref else (
        "helm.trunkRef is declared with an empty value")
    if bad:
        raise _Unusable(bad, "declare it: git -C %s config --local "
                             "helm.trunkRef refs/heads/<branch>" % tree)
    return ref


def _worktrees(tree):
    """[{path, branch, bare, prunable}] for the repository `tree` belongs to,
    the main worktree first (git lists it first). -z keeps a path with a line
    break in it one path."""
    rc, out = _git(tree, "worktree", "list", "--porcelain", "-z")
    if rc != 0:
        raise _Unusable("git could not list the worktrees of %s" % tree,
                        "check that %s is a readable git checkout" % tree)
    rows, row = [], None
    for field in out.split("\0"):
        if not field:
            row = None
            continue
        key, _sp, value = field.partition(" ")
        if key == "worktree":
            row = {"path": value, "branch": None, "bare": False,
                   "prunable": False}
            rows.append(row)
        elif row is not None and key == "branch":
            row["branch"] = value
        elif row is not None and key in ("bare", "prunable"):
            row[key] = True
    return rows


def _head_ref(tree):
    """The branch HEAD names in the checkout `tree`, read from its files, or
    None when it names none or cannot be read. The FAST PATH only: a None
    here decides nothing, it sends the question to `git worktree list`."""
    dot = os.path.join(tree, ".git")
    try:
        gitdir = dot
        if not os.path.isdir(dot):
            with open(dot, encoding="utf-8") as f:
                line = f.readline().strip()
            if not line.startswith("gitdir: "):
                return None
            gitdir = os.path.join(tree, line[len("gitdir: "):])
        with open(os.path.join(gitdir, "HEAD"), encoding="utf-8") as f:
            head = f.readline().strip()
    except (OSError, ValueError):
        return None
    return head[len("ref: "):] if head.startswith("ref: refs/heads/") else None


def trunk_checkout(tree):
    """The trunk checkout of the repository `tree` is a worktree of, as a
    realpath. Raises _Undeclared when the repository declares no trunk, and
    _Unusable(why, fix) when it declares one that cannot be used.

    THE TRUNK ITSELF PAYS ONE GIT CALL. A tree whose own HEAD names the
    declared branch holds it, and git lets one worktree hold a branch, so the
    worktree list (about 11 ms over 247 worktrees, measured) is read only
    when the answer is somewhere else."""
    tree = os.path.realpath(tree)
    ref = _declared_ref(tree)
    if _head_ref(tree) == ref:
        return tree
    rows = _worktrees(tree)
    holders = [os.path.realpath(r["path"]) for r in rows
               if r["branch"] == ref and not r["bare"] and not r["prunable"]
               and os.path.isdir(r["path"])]
    if tree in holders:
        return tree
    branch = ref[len("refs/heads/"):]
    if not holders:
        main = rows[0]["path"] if rows else tree
        raise _Unusable(
            "no worktree of this repository has the trunk branch %s checked "
            "out" % branch,
            "check it out in the trunk checkout: git -C %s switch %s"
            % (main, branch))
    if len(holders) > 1:
        raise _Unusable(
            "the trunk branch %s is checked out in %d worktrees (%s)"
            % (branch, len(holders), ", ".join(holders)),
            "keep %s checked out in one worktree only" % branch)
    trunk = holders[0]
    entry = os.path.join(trunk, "bin", "helm")
    real = os.path.realpath(entry)
    if not (os.path.isfile(real) and os.access(real, os.X_OK)
            and real.startswith(trunk + os.sep)
            and os.path.isfile(os.path.join(trunk, "helm", "__init__.py"))):
        raise _Unusable(
            "the trunk checkout %s has no runnable bin/helm and helm package"
            % trunk,
            "restore them: git -C %s checkout -- bin/helm helm" % trunk)
    return trunk


def _refusal(verb, tree, why, fix):
    return ("[helm] REFUSED: the %s verb writes a shared ledger, so it runs "
            "trunk helm, and trunk helm cannot be used: %s. Nothing ran from "
            "%s, so nothing was written. Fix: %s. To run this tree's own "
            "code on purpose, set %s=1." % (verb, why, tree, fix, OPT_IN))


def _fallback(verb, tree, why, fix):
    return ("[helm] trunk helm cannot be used: %s. Fix: %s. The %s verb ran "
            "this tree's own code instead (%s), so seats can still "
            "coordinate; every other coordination verb is refused until "
            "trunk is fixed." % (why, fix, verb, tree))


def decide(argv, package_dir=None, environ=None):
    """The Route for one process entry. Removes HOP from `environ` (default
    os.environ) whatever it decides, so the verb's children never inherit it.

    `package_dir` is the running helm package; its checkout is the tree whose
    code would run here."""
    env = os.environ if environ is None else environ
    hop = env.pop(HOP, None)
    verb = argv[0] if argv else None
    if verb not in COORDINATION_VERBS:
        return Route(PASS)
    from .cli import _tree_of
    package_dir = package_dir or os.path.dirname(os.path.abspath(__file__))
    tree = _tree_of(package_dir)
    if not tree:
        return Route(PASS)       # installed outside a checkout: no route
    tree = os.path.realpath(tree)
    try:
        trunk, unusable = trunk_checkout(tree), None
    except _Undeclared:
        if not hop:
            return Route(PASS)
        trunk, unusable = None, None     # declared away since the parent
    except _Unusable as exc:
        trunk, unusable = None, exc.args
    fallback = bool(unusable) and verb in FALLBACK_VERBS
    if hop:
        # The child of ONE re-exec. It runs only if it really is the trunk
        # its parent chose, and it never execs again: a HOP set by hand is
        # neither a loop nor a way around the route. Chat still falls back
        # when trunk became unusable in between, as it would from any tree.
        if trunk == tree == os.path.realpath(hop):
            return Route(HERE, tree)
        if not fallback:
            return Route(REFUSE, line=_refusal(
                verb, tree, "the re-exec meant for trunk %s reached %s, which "
                "is not trunk" % (hop, tree),
                "run it again with helm from PATH, and check where "
                "%s/bin/helm leads" % hop))
    elif unusable and env.get(OPT_IN) == "1":
        return Route(OWN, line=(
            "[helm] %s=1: the %s verb runs this tree's own code (%s); "
            "trunk helm cannot be used: %s." % (OPT_IN, verb, tree,
                                                unusable[0])))
    if fallback:
        return Route(FALLBACK, line=_fallback(verb, tree, *unusable),
                     tree=tree)
    if unusable:
        return Route(REFUSE, line=_refusal(verb, tree, *unusable))
    if trunk == tree:
        return Route(HERE, trunk)
    if env.get(OPT_IN) == "1":
        return Route(OWN, trunk, (
            "[helm] %s=1: the %s verb runs this tree's own code (%s), not "
            "trunk helm (%s); its ledger writes come from this tree."
            % (OPT_IN, verb, tree, trunk)))
    return Route(TRUNK, trunk, tree=tree)


def _interpreter_flags(flags=None):
    """The startup flags of this interpreter that change how helm starts,
    so trunk starts the same way (`-S` is the hook wrapper's floor)."""
    f = flags or sys.flags
    out = []
    if f.isolated:
        out.append("-I")
    else:
        if f.ignore_environment:
            out.append("-E")
        if f.no_user_site:
            out.append("-s")
    if f.no_site:
        out.append("-S")
    if f.dont_write_bytecode:
        out.append("-B")
    return out


def exec_argv(trunk, argv, executable=None, flags=None):
    """The argv that starts trunk's bin/helm with `argv` as its verb and
    tail. THIS interpreter, not the shebang's PATH lookup: the process that
    is running is already the interpreter the caller chose."""
    entry = os.path.join(trunk, "bin", "helm")
    exe = sys.executable if executable is None else executable
    if not exe:
        return [entry] + list(argv)
    return [exe] + _interpreter_flags(flags) + [entry] + list(argv)


def _run_here(line, tree):
    """(None, False) for a FALLBACK: print its one line and run this tree.

    The line already names the tree and the fix, so the door's tree-behind-
    trunk line (`cli.stale_tree_warning`, once per process) is spent here
    too. Its cure, fast-forward or rebase THIS tree, is the wrong fix when
    trunk has left its branch, and the ruling is one line."""
    print(line, file=sys.stderr)
    from . import cli
    cli._STALE_TREE_SAID.append(tree)
    return None, False


def enter(argv, package_dir=None, environ=None, execve=None):
    """(rc, banner) for `cli.main`'s process entry. rc None means run the
    verb here; banner False means the maker banner must not print. Never
    returns on a TRUNK route unless the exec itself failed."""
    env = os.environ if environ is None else environ
    route = decide(argv, package_dir, env)
    if route.action == PASS:
        return None, True
    if route.action == HERE:
        return None, False
    if route.action == OWN:
        print(route.line, file=sys.stderr)
        return None, False
    if route.action == REFUSE:
        print(route.line, file=sys.stderr)
        return REFUSED_RC, False
    if route.action == FALLBACK:
        return _run_here(route.line, route.tree)
    args = exec_argv(route.trunk, argv)
    child = dict(env)
    child[HOP] = route.trunk
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:        # noqa: BLE001 — a closed stream has nothing
            pass
    try:
        (execve or os.execve)(args[0], args, child)
    except OSError as exc:
        why = "starting %s failed (%s)" % (args[0], exc.__class__.__name__)
        fix = "check that %s runs" % os.path.join(route.trunk, "bin", "helm")
        if argv[0] in FALLBACK_VERBS:
            return _run_here(_fallback(argv[0], route.tree, why, fix),
                             route.tree)
        print(_refusal(argv[0], route.trunk, why, fix), file=sys.stderr)
        return REFUSED_RC, False
    return REFUSED_RC, False     # only an injected execve returns
