#!/usr/bin/env python3
"""Is this git tree helm's OWN source, or a project repo helm is being USED on?

helm was always meant to help agent teams build ANY project. The team building
helm dogfoods it on helm itself, and that left assumptions that the cwd is the
helm checkout. A team USING helm must never need to know how helm is made;
wherever a verb, a default or a warning assumes the helm checkout, that is the
bug — that is the owner's framing for this whole class.

TWO SURFACES ASKED THE SAME QUESTION AND BOTH ANSWERED IT WRONG:

  * `work install-guard` gave every repo that declared nothing the RAIL —
    helm-the-repo's own shared-checkout law: lane discipline, the attribution
    trailer, citation and public-prose rungs, process an adopter's project
    never agreed to. Its note even said so out loud: "profile undeclared
    (rail by default)".
  * `cli.which_helm_warning` — the maker-only "you ran helm@X from tree@Y"
    surface — called any tree holding a `bin/helm` ENTRY SCRIPT a helm
    checkout. A project repo that ships a one-line `bin/helm` wrapper (the
    natural thing for an adopter to do) therefore got a warning about helm's
    internals on every command.

THE PREDICATE IS THE PACKAGE ROOT, NOT AN ENTRY SCRIPT AND NOT A PATH. A tree
is helm's source when the package a helm invocation imports lives at its root:
`helm/__init__.py`. That file IS the package — it is what `import helm` binds —
and nothing else in the tree can stand in for it. `bin/helm` alone matches a
wrapper that only LAUNCHES helm, which is exactly the false positive above.

A SECOND MODULE IN THE CONJUNCTION SILENCED THE HAZARD MID-REFACTOR. The first
shape required `helm/cli.py` as well, on the reasoning that two files are a
stronger signature than one. They are, and that is the defect: a helm worktree
in which `cli.py` is deleted, moved or renamed — a refactor in flight, and the
tree whose import would FAIL — stopped being recognised as helm source, so the
maker warning went quiet in exactly the tree whose code cannot run. The tree
that still holds the package root is still a tree whose helm code a maker is
editing, whatever the state of the modules under it. The narrower reading of
`__init__.py` alone — that it matches any project whose own root package is
named `helm` — is a tree that really does carry a `helm` package a helm
invocation could import instead, which is what this predicate reports.

WHEN THE FILESYSTEM REFUSES TO ANSWER, THIS RAISES rather than saying False.
"Not there" and "cannot look" are different facts and the callers want opposite
defaults from them: the maker warning speaks (advisory, and missing a real
mismatch is the worse failure), the guard default takes the narrow project legs
(arming helm's own process in an adopter's repo is the worse failure). A
predicate that folded both into False would have made that choice for them.

WHY NOT "THE SAME GIT COMMON DIR AS THE RUNNING BINARY'S CHECKOUT". That was
the first shape considered for the warning, and it is too narrow in a way that
silences the hazard the warning exists for: two SEPARATE clones of helm (or a
fork beside the canonical checkout) have different common dirs, and running
clone A's binary while standing in clone B is precisely the maker mistake —
`tests/test_cli_tree_warning.TreeWarningTest.test_warns_and_names_BOTH_trees_when_they_differ`
pins that case. A worktree of the helm repo holds the package at its root and
so answers True here anyway, with zero subprocesses; a worktree of a project
repo answers False. It is also a hard requirement that this costs stats only:
it sits in front of EVERY helm invocation, and the git-subprocess version of
the tree warning was measured at 15.1ms per command against 0.03ms for the
stat path.

A tree that vendors helm's package at its own root is, by this predicate,
helm source — and that is the honest answer: such a tree really does carry
code a helm invocation could have imported instead.

AN IDENTITY IS NOT ALWAYS SPELLED AS A CHECKOUT ROOT. helm's own stores name a
repository by its GITDIR — `dispatches._repo_info` records the absolute
`--git-common-dir` as `repo_id`, and a land close hands that spelling straight
to the guard's drift reader. Classifying `<root>/.git` by looking for
`<root>/.git/helm/__init__.py` answers False for helm's OWN checkout, which
made an armed rail read as leak drift and falsely refused a `--live` close.
`checkout_root` is the one normalization both the predicate and the guard ask,
so no caller has to know which spelling it holds.

AND THE SPELLING IS RESOLVED BY GIT, NEVER BY THE PATH'S SHAPE. A gitdir's
shape does not determine its tree, so matching path shapes — `<root>/.git`,
`<root>/.git/worktrees/<name>` — answers about the wrong tree for two supported
identities:

  * A SEPARATE GITDIR (`git clone --separate-git-dir`, or `GIT_DIR` outside the
    tree) was returned unchanged, so the package root was looked for inside the
    METADATA directory. helm's own source answered "project repo" through that
    spelling, and `stale_guard_hooks` rendered the leak hook set against an
    armed rail: a false STALE at the composed `pre-commit` and a refused
    `--live` close. `tests/test_dispatches.py` builds exactly this repository
    shape, so it is a supported input and not a hypothetical.
  * A LINKED WORKTREE'S PRIVATE GITDIR named the MAIN checkout. The linked
    worktree's own root is what git records for it (`<private gitdir>/gitdir`
    holds the path of that worktree's `.git` file), and the two trees disagree
    whenever they hold different content — a sparse main beside a populated
    lane is the ordinary case here.

So `checkout_root` asks git: `rev-parse --show-toplevel` answers for any path
INSIDE a working tree, and for a gitdir every candidate root is CONFIRMED by
running `rev-parse --git-dir --git-common-dir` FROM that candidate and matching
the identity back. A candidate that does not confirm is not used — an identity
git records no working tree for (a bare mirror, a separate gitdir) is returned
unchanged rather than guessed at, and two separate clones stay two
repositories because nothing here is derived from a sibling path.

THE QUESTIONS GO THROUGH helm's GIT SEAM (`helm/vcs.py`), not through a spawn
of this module's own. The seam already expresses both verbs this module asks and
the per-call env overlay the scrub below needs, so there is nothing here that
`DirectSpawnAuditTest` has to carry as migration debt.

THE ENVIRONMENT IS SCRUBBED BEFORE EVERY ONE OF THOSE QUESTIONS. `GIT_DIR`,
`GIT_COMMON_DIR` and `GIT_WORK_TREE` in the ambient environment override
`git -C <path>` — MEASURED: with `GIT_DIR` exported, `git -C <other tree>
rev-parse --git-dir` prints the EXPORTED gitdir. helm runs inside hooks, gates
and land closes, all of which export those, so an unscrubbed question resolves
the wrong git directory and the answer is about whatever repository the caller's
environment last named.

AND THE COST STAYS WHERE IT WAS. The git questions are asked ONLY for a path
that does not carry `.git` at it — never for a checkout root or a linked
worktree root, which is every path the maker tree warning classifies. That
surface still costs stats alone.

AND AN IDENTITY WHOSE CHECKOUT CANNOT BE REACHED IS UNKNOWN — NEVER A READING
OF WHAT IT TRACKS. Answering an unresolved identity with `cat-file -e
HEAD:helm/__init__.py` swaps the question for a different one, and the two have
different answers: a tree whose HEAD does not carry the package root while the
WORKING TREE does (restored, or never committed — the ordinary mid-move state
DAMAGED is about) reads `rail` through its checkout and `leak` through its
metadata spelling, and the rail it has armed then compares STALE. See
`repository_state`: what is reachable is reached through git, and what is not
answers UNKNOWN so the caller decides — the guard refuses to derive a law from
it rather than letting a leak profile speak for a rail.

A PACKAGE ROOT MISSING FROM A TREE THAT STILL CARRIES HELM'S MODULES IS A THIRD
STATE, NOT "NOT HELM". Delete or move `helm/__init__.py` in a real helm
worktree and the modules under it — `cli.py`, `seat.py` and the rest — are
still there: that is a maker's tree mid-move, and the tree whose own
`import helm` FAILS, so an invocation of ANOTHER tree's binary from it is
exactly what the warning exists to say out loud. Folding that into "not helm
source" SILENCED the warning in the one tree that could not have served the
command. `source_state` reports the three states and each caller reads the one
it needs: the warning speaks for DAMAGED as well as SOURCE, the guard's profile
default arms the rail for SOURCE alone (arming helm's own process in an adopter
repo stays the worse failure).

BUT THE DIRECTORY NAME `helm` IS NOT PROVENANCE. "Any `.py` under `helm/`" made
DAMAGED out of an adopter's own `helm/Chart.yaml` + `helm/render.py` — a chart
and its renderer, no `bin/helm`, no helm CLI in the tree — so the maker-only
warning fired there and advised a `./bin/helm` that does not exist, which is
the task/2442 failure with a new door. DAMAGED therefore asks for helm-source
provenance (`_helm_provenance`): this project's `bin/helm` entry script AND two
or more of the names under `helm/` being modules of the RUNNING package — the
reference derived from the code that is executing, never a list of names written
down here to go stale. A project repo carrying no
`helm/` package directory at all is still OTHER in one `listdir`, so the common
adopter path costs no more than it did.
"""
import os
import stat

SOURCE = "source"        # the package root is here: this tree IS helm's source
DAMAGED = "damaged"      # the package root is gone from a tree still holding
                         # HELM'S OWN modules — helm source mid-move
OTHER = "other"          # no helm package here: a project repo helm is used on
UNKNOWN = "unknown"      # a repository identity that names no working tree
                         # anybody can reach: there is nothing to classify,
                         # and what the repository TRACKS is not a substitute
                         # for what a checkout holds (see `repository_state`)


class UnreachableCheckout(OSError):
    """The identity given names no working tree that can be reached.

    An OSError SUBCLASS on purpose. "Cannot look" is already this module's
    channel for an answer that can be neither read nor ruled out, and each
    caller has already chosen its direction for it — the maker warning speaks,
    the guard's profile default takes the narrow legs. An exception outside
    that hierarchy would have crashed the surface that runs in FRONT of every
    helm invocation. The caller that must distinguish this case from a
    stat that failed — the guard, which refuses to derive a law rather than
    inventing one — catches this type first; see `helm/work/_guard.py`."""


# HOW MANY OF HELM'S OWN MODULE NAMES A TREE MUST SHARE before its `helm/`
# directory counts as helm's package with the root missing. TWO, because one
# ordinary name — a `cli.py` — is a name any project's own package can hold,
# while two of them together is helm's package and not a coincidence. A real
# maker's tree mid-move shares dozens: DAMAGED is one file removed from a
# checkout, not a stripped tree. The names themselves are never transcribed
# here — see `_helm_provenance`.
_SOURCE_MODULE_QUORUM = 2

# GIT'S OWN OVERRIDES WIN OVER `-C`, so they are removed before every question
# this module asks. See the module docstring: helm runs inside hooks and gates
# that export these, and an unscrubbed question answers about the wrong
# repository entirely.
_GIT_ENV_OVERRIDES = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE",
                      "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                      "GIT_ALTERNATE_OBJECT_DIRECTORIES")


def _git_env():
    """The per-call ENV OVERLAY that carries the scrub to the seam.

    `helm/vcs.py` overlays a mapping onto the ambient environment for one call
    and a `None` value there means REMOVE, which is exactly the scrub this
    module needs and the reason it does not have to spawn git itself: HOME,
    PATH and GIT_CONFIG_* survive (replacing the environment wholesale is how
    git reads go subtly wrong on a differently configured box) while the
    repository-selecting variables are gone for the one question."""
    return {name: None for name in _GIT_ENV_OVERRIDES}


def _git(cwd, args):
    """(rc, stdout lines) for one git question, asked FROM `cwd` THROUGH THE
    SEAM (`helm/vcs.py`) with the scrub above as the call's env overlay.

    THE SEAM IS WHERE HELM SPELLS GIT. `tests/test_vcs.DirectSpawnAuditTest`
    pins the modules that still spawn git directly, and a classifier whose
    whole question is `rev-parse` and `cat-file` has no reason to be one of
    them: the seam already expresses both, including the per-call overlay that
    UNSETS a variable, which is the only part of this question a direct spawn
    answers that the seam does not.

    ANY FAILURE TO ASK AT ALL IS A NONZERO rc AND NO LINES — a classification
    never crashes. That covers a missing or broken git (the seam answers rc -1
    for spawn trouble) and, because the seam's calls are METERED, a projection
    whose budget is spent while this runs: this module sits in front of every
    helm invocation and must not be the thing that raises there.

    `helm.vcs` is imported HERE, not at module scope: the common path through
    this module costs stats alone (see the module docstring) and must not pay
    an import it never uses."""
    from . import vcs
    try:
        rc, out, _err = vcs.backend(cwd).text(cwd, *args, timeout=10,
                                              env=_git_env())
    except Exception:               # the seam fails open for this classifier
        return 1, []
    return rc, out.splitlines()


def _same_path(a, b):
    return bool(a) and bool(b) and os.path.realpath(a) == os.path.realpath(b)


def _confirms(candidate, gitdir):
    """True when GIT, asked from `candidate`, names `gitdir` as this working
    tree's git directory. The confirmation is the whole point: a candidate root
    derived from a path is a guess until the checkout itself agrees."""
    rc, lines = _git(candidate, ["rev-parse", "--path-format=absolute",
                                 "--git-dir", "--git-common-dir"])
    if rc != 0 or len(lines) < 2:
        return False
    return _same_path(lines[0], gitdir) or _same_path(lines[1], gitdir)


def checkout_root(path):
    """The CHECKOUT ROOT that a supported repository identity denotes.

    Several spellings reach helm's repository readers and all of them mean one
    working tree: the checkout root itself, a linked worktree's root, a path
    INSIDE either, and either one's GITDIR (which is what `--git-common-dir`
    prints and what the dispatch store keeps as `repo_id`). A classification
    that reads files RELATIVE TO the identity must be handed the root, or it
    asks its question of a directory inside `.git`.

    THE FAST PATH IS STATS ONLY. A path carrying `.git` at it IS a root — a
    directory in the main checkout, a file in a linked worktree — and that is
    every path the maker tree warning classifies, so that surface still costs
    no subprocess.

    ANY OTHER SPELLING IS RESOLVED BY GIT, NOT BY THE PATH'S SHAPE, and every
    candidate root is CONFIRMED from the candidate itself (see the module
    docstring for the two identities shape-matching got wrong). An identity git
    records no working tree for — a bare mirror, a separate gitdir — is returned
    UNCHANGED, so a caller's classification is exactly as conservative as it was
    rather than answering about some other directory. SEPARATE CLONES STAY
    SEPARATE: nothing here is derived from a sibling path, so one repository's
    gitdir never maps onto another repository's root, which is the hazard the
    maker tree warning exists to catch."""
    if not path:
        return path
    p = os.path.abspath(os.path.expanduser(path))
    if os.path.lexists(os.path.join(p, ".git")):
        return p
    return _root_git_names(p) or p


def _root_git_names(p):
    """The working-tree root git names for `p`, or None when it names none.

    Two questions, in this order, because the first answers for the common case
    without the repository having to be inspected: a path inside a working tree
    has a `--show-toplevel`, and only a path that is (or is inside) a GITDIR
    does not."""
    rc, lines = _git(p, ["rev-parse", "--path-format=absolute",
                         "--show-toplevel"])
    if rc == 0 and lines:
        return lines[0]
    rc, lines = _git(p, ["rev-parse", "--path-format=absolute",
                         "--git-dir", "--git-common-dir"])
    if rc != 0 or len(lines) < 2:
        return None                 # not a repository at all
    gitdir, common = lines[0], lines[1]
    if not _same_path(gitdir, common):
        # A LINKED WORKTREE'S PRIVATE GITDIR, and git records that worktree's
        # own root in it: `<private gitdir>/gitdir` holds the path of the
        # worktree's `.git` FILE, whose directory is the root. The main
        # checkout is NOT the answer here — the two trees hold different
        # content, which is what a lane beside a shared checkout IS.
        #
        # THE POINTER IS A FILESYSTEM PATH, SO IT IS BYTES. Read in TEXT mode
        # it raised UnicodeDecodeError — not an OSError, so the catch below
        # missed it — for a perfectly valid path: one ancestor directory
        # carrying a non-UTF8 byte is enough, and the lane basename beside it
        # is ordinary. MEASURED with git 2.53.0: `git worktree add` under an
        # ancestor holding byte 0xff writes that byte into
        # `<private gitdir>/gitdir`, and a text-mode read of it raises. The
        # resolution then died out of a classifier that must never crash, and
        # the guard's drift reader reported UNKNOWN instead of comparing the
        # hooks it could have read. `os.fsdecode` is the same
        # filesystem-byte-safe handling `tests/test_vcs.py` already pins for
        # the seam's own worktree parser (`parse_worktree_records`), and it
        # round-trips back to git through the seam as the same bytes.
        try:
            with open(os.path.join(gitdir, "gitdir"), "rb") as f:
                pointer = os.fsdecode(f.read().rstrip(b"\r\n"))
        except OSError:
            return None
        # Only the line terminator is stripped, above: a trailing or leading
        # SPACE is part of a valid path, and `str.strip()` would have eaten it.
        linked = os.path.dirname(pointer)
        return linked if linked and _confirms(linked, gitdir) else None
    # A MAIN CHECKOUT'S GITDIR is `<root>/.git`, and that parent is a candidate
    # to be confirmed — never assumed. A separate gitdir fails the
    # confirmation and that is the correct outcome: git records no working tree
    # for it, so there is nothing to resolve and nothing to guess.
    if os.path.basename(gitdir) != ".git":
        return None
    candidate = os.path.dirname(gitdir)
    return candidate if _confirms(candidate, gitdir) else None


def _helm_provenance(root, names):
    """Is the `helm/` directory in this tree HELM'S OWN package?

    THE PACKAGE ROOT IS THE ONLY MARKER THAT NEEDS NO CORROBORATION, and when
    it is gone the DIRECTORY NAME `helm` IS NOT EVIDENCE OF ANYTHING. "Any
    `.py` file under `helm/`" was the first shape of the DAMAGED
    discriminator, and `helm` is one of the most-taken directory names there
    is: an ADOPTER's repo holding `helm/Chart.yaml` beside `helm/render.py` —
    a Kubernetes chart and the script that renders it, no `bin/helm`, no helm
    CLI anywhere — was called a damaged helm checkout, so the maker-only
    wrong-tree warning fired on every command there and advised a `./bin/helm`
    that does not exist. That surface is exactly what task/2442 exists to keep
    out of an adopter's repo, and the old predicate was silent about this tree.

    SO DAMAGED ASKS FOR HELM-SOURCE PROVENANCE: the entry script this project
    really ships at `bin/helm`, AND at least `_SOURCE_MODULE_QUORUM` of the
    module names under `helm/` being names of THE RUNNING PACKAGE'S OWN
    MODULES.

    THE REFERENCE IS DERIVED, NEVER TRANSCRIBED. A list of names written down
    here would be a guess about what helm's source looks like, and a stale one
    the first time a module is renamed; the package this code is executing from
    IS helm's module list, it costs one `listdir` on the DAMAGED candidate path
    alone, and it cannot drift from the thing it describes. Only `.py` names
    count on both sides: `__pycache__` is a name any project that ran a script
    in its own `helm/` directory would share, and it says nothing about whose
    package this is.

    Raises OSError exactly where `source_state` does — an unreadable entry
    script, or a running package whose own directory cannot be listed, is
    "cannot look" and never "not helm": each caller's declared direction for
    that (the warning speaks, the guard takes the narrow legs) is the answer,
    not a silent False that would put the maker warning back to sleep in a
    damaged tree."""
    try:
        entry = os.stat(os.path.join(root, "bin", "helm"))
    except (FileNotFoundError, NotADirectoryError):
        return False
    if not stat.S_ISREG(entry.st_mode):
        return False
    running = os.path.dirname(os.path.abspath(__file__))
    own = {name for name in os.listdir(running) if name.endswith(".py")}
    shared = own & {name for name in names if name.endswith(".py")}
    return len(shared) >= _SOURCE_MODULE_QUORUM


def source_state(root):
    """SOURCE, DAMAGED or OTHER for one CHECKOUT ROOT — stats only, no git.

    SOURCE is the package root: `helm/__init__.py` IS the package, it is what
    `import helm` binds, and nothing else in the tree stands in for it.

    DAMAGED is the third state, and folding it into "not helm" silences the
    maker warning in the one tree that could not have served the command: the
    package root gone from a tree that still carries HELM'S OWN modules.
    Delete or move `helm/__init__.py` in a real helm worktree and `cli.py`,
    `seat.py` and the rest are still under `helm/` — a refactor in flight,
    whose own `import helm` FAILS, which is precisely why a successful
    invocation of ANOTHER tree's binary there has to be said out loud. The
    modules have to be HELM'S, though, or the directory name alone convicts an
    adopter's own `helm/` of being a maker's tree: `_helm_provenance` is that
    discriminator and it is asked only here.

    Raises OSError when the answer can be neither read nor ruled out — an
    unreadable directory, a path that is not one. Each caller chooses the
    direction it fails in; see the module docstring for which and why."""
    pkg = os.path.join(root, "helm")
    try:
        if stat.S_ISREG(os.stat(os.path.join(pkg, "__init__.py")).st_mode):
            return SOURCE
    except (FileNotFoundError, NotADirectoryError):
        pass                        # the package ROOT is not there — which of
                                    # the other two states is the next question
    try:
        names = os.listdir(pkg)
    except (FileNotFoundError, NotADirectoryError):
        return OTHER                # no `helm/` package directory whatsoever:
                                    # a project repo helm is merely used on
    if not any(name.endswith(".py") for name in names):
        return OTHER                # a directory with no module in it is
                                    # evidence of nothing
    return DAMAGED if _helm_provenance(root, names) else OTHER


def repository_state(root):
    """`source_state` for whatever spelling of a repository identity `root` is,
    and UNKNOWN for an identity whose CHECKOUT cannot be reached at all.

    THE CHECKOUT IS THE SUBJECT, AND THE TRACKED HISTORY IS NOT A STAND-IN FOR
    IT. Answering an unresolved identity with `cat-file -e
    HEAD:helm/__init__.py` — what the repository TRACKS — answers a different
    question, and the two have different answers: a genuine helm tree whose HEAD
    does not carry the package root while its WORKING TREE does (it was
    restored, or never committed, which is the ordinary mid-move state this
    module's DAMAGED case is about) reads `rail` through its checkout and `leak`
    through its metadata spelling, and the armed rail then compares STALE. A
    verdict derived from the wrong artifact is worse than no verdict, because
    nothing downstream can tell it apart from a real one.

    WHERE THE CHECKOUT IS REACHED. `checkout_root` asks git, and git answers
    for every identity it records a working tree for — including a separate
    gitdir that carries `core.worktree`, MEASURED with git 2.53.0:
    `rev-parse --show-toplevel` asked FROM such a gitdir prints the worktree,
    which is the first question `_root_git_names` asks. A linked worktree is
    reached through its own `<private gitdir>/gitdir` pointer there too.

    AND WHERE IT CANNOT BE. `git clone --separate-git-dir` and
    `git init --separate-git-dir` leave `core.worktree` UNSET (MEASURED, git
    2.53.0): the link is ONE-DIRECTIONAL — the checkout holds a `.git` FILE
    naming the gitdir and the gitdir records nothing pointing back, the same
    measurement `helm/obligation._root_for_repo` carries for `repo_id`. A bare
    mirror has no tree at all. For those there is no tree to classify, so this
    answers UNKNOWN and every caller decides what to do about it — the guard
    refuses to derive a law from it rather than arming one, which is the
    difference between "I do not know" and a leak profile speaking for a rail.

    Returns OTHER for None or an empty path so every caller can pass an
    unresolved root. Raises OSError exactly where `source_state` does."""
    if not root:
        return OTHER
    resolved = checkout_root(root)
    state = source_state(resolved)
    if state != OTHER or os.path.lexists(os.path.join(resolved, ".git")):
        return state                # a real checkout: its DISK is the answer
    return UNKNOWN


def _bool_state(root):
    """`repository_state`, with UNKNOWN raised instead of returned.

    THE TWO BOOLEAN PREDICATES CANNOT CARRY A THIRD ANSWER, and folding one in
    is how an unknown identity becomes a verdict: False from either of them is
    read as "a project repo helm is merely used on", which arms the leak legs
    and renders the leak hook set against whatever is actually installed. The
    raise is an OSError subclass so that both callers keep the direction they
    already declared for "cannot look" — see `UnreachableCheckout`."""
    state = repository_state(root)
    if state == UNKNOWN:
        raise UnreachableCheckout(
            "%s names no working tree git can reach, so whether it is helm's "
            "own source cannot be read from a checkout" % (root,))
    return state


def is_helm_source_tree(root):
    """True when `root` is a checkout of helm ITSELF (see the module
    docstring), False for a project repo helm is merely used on.

    THE NARROW READING, for the caller whose worse failure is a false True:
    the guard's profile default arms helm's own shared-checkout rail on this
    answer, and arming it in an adopter's repo is the failure that matters, so
    a DAMAGED tree — package root gone, modules still there — answers False
    here and `is_helm_tree_a_maker_edits` is what the maker warning asks.

    AN IDENTITY WITH NO REACHABLE CHECKOUT RAISES `UnreachableCheckout` rather
    than answering False, because False here IS a verdict: it arms the leak
    legs and tells a drift reader that an armed rail is stale. See
    `repository_state` for what cannot be reached and why."""
    return _bool_state(root) == SOURCE


def is_helm_tree_a_maker_edits(root):
    """True when `root` holds helm's own code in ANY state a maker leaves it
    in — the package root present, or gone from a tree still carrying the
    package's modules.

    THE WIDE READING, for the caller whose worse failure is a false False: the
    maker-only tree warning is advisory, it changes no exit code, and the
    failure it exists to prevent is a dogfood that PASSES against another
    tree's code. A tree mid-move is the likeliest place for that to happen and
    the likeliest place for the old predicate to go quiet.

    Raises `UnreachableCheckout` for an identity naming no reachable checkout,
    which `cli._is_helm_checkout` catches as the "cannot look" case it already
    speaks for. The warning is only ever asked about a working tree it is
    standing in, so that is a contract, not a live path."""
    return _bool_state(root) in (SOURCE, DAMAGED)
