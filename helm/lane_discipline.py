#!/usr/bin/env python3
"""The lane-discipline rung — REFUSE a commit that ORIGINATES work on the
shared checkout's base branch, where no lane, no gate and no verdict ever saw
it.

WHY THE RUNG EXISTS (2026-08-03, measured, and the incident is a fleet seat's
own). Two commits went straight to local `main` in the shared checkout: no
claimed lane, no whole-suite gate, no cross-family verdict, no fold. Over the
preceding 40 commits on origin/main exactly ONE other commit had that shape,
and it was the same author's. The fleet's process is: claim a lane -> build in
its private worktree -> verify it with focused runs (`helm gate run --focus`,
the tests the change reaches, beside the tree-wide audits that
`helm gate audits` prints) -> a cross-family
reviewer reads it and, on a clean read, HOLDS it source-clean (`helm dispatch
hold --source-clean`), or, on a MECHANICAL finding, commits the cure in their
OWN worktree on a branch off the exact reviewed tip and records that tip on a
FIX verdict (`--patch-tip`) for the lane owner or integrator to rebase onto ->
the integrator composes the train, runs its ONE serial whole suite (the
land gate) on the tree that lands, the approve binds that suite's token, and
the integrator FOLDS it to main. A lane runs no whole suite of its own:
`helm gate run` refuses one in a lane room (task/3039). A lane may
therefore carry SEVERAL AUTHORS of either family; what keeps the families
independent is that the composed tip is re-read once by a reader who wrote
none of it, not a rule that one family may only look. A DESIGN finding is
never patched under review — it goes to a meld.

NOTE WHAT DID NOT CHANGE: every one of those authors writes INSIDE A LANE. A
reviewer's cure is a commit on a branch off the reviewed tip in the reviewer's
own worktree; it is never a commit on the shared checkout's base branch, which
is the exact act this rung refuses.

THE COST WAS NOT THE COMMIT, IT WAS THE INHERITANCE. Two OTHER seats then cut
their lanes from that local main and silently carried the unreviewed change.
Their diffs against origin/main showed it as THEIRS. The integrator named the
harm exactly: a verdict is a statement about a TREE, so anything riding in
that tree is inside the verdict whether the reviewer noticed it or not — an
APPROVE on either lane would have covered a commit nobody reviewed.

NOTHING STOPPED IT. helm guards in-flight gates, vacuous assertions,
never-track paths, conflict markers, host paths and argv substitution. The one
law with no instrument was the one enforced DOWNSTREAM, by the integrator's
attention. A convention holds until somebody decides their case is special,
and that author's reasons both felt sufficient at the time.

=== THE DISCRIMINATOR, AND WHY IT IS NOT IDENTITY ===

A BLANKET REFUSAL WOULD BE WORSE THAN NOTHING. Folds ARE commits to main and
they are the correct path; a rung that refuses them is overridden inside the
hour, and then the override is the habit. So the rung must separate a FOLD
from an AD-HOC COMMIT.

IT CANNOT ASK WHO. Every commit in this estate carries one git identity —
agent commits, owner commits and rescue commits are byte-identical in author
and committer, and blame inherits the same blindness (premise
`git-metadata-never-proves-who-wrote-it`). Anything keyed on identity here
does not merely fail, it fails while looking like it works.

IT CANNOT ASK WHAT THE MESSAGE SAYS. A pre-commit hook runs BEFORE the message
exists — there is no message to read, which settles it mechanically. And even
at `commit-msg` the shape `fold: <lane> at <sha> (... APPROVE gate:<tok>)` is
a string anyone can type; a rung keyed on it teaches typing it. That is the
convention that already failed, re-implemented as code.

SO IT ASKS *WHERE* AND *WHETHER THE CONTENT ALREADY EXISTS ELSEWHERE* — four
structural facts, all read from git, none of them assertable by the author:

  1. VENUE. `--git-dir` == `--git-common-dir` is the SHARED CHECKOUT; every
     lane worktree answers `<common>/worktrees/<room>` and leaves at the first
     probe. A seat working correctly never reaches clause 2, so the rung
     costs the fleet ONE `git rev-parse` per commit (both dirs come back from
     the one call) and can never refuse it.

  2. BRANCH. HEAD is the base branch (`main` when it exists, else the shared
     checkout's own HEAD — vcs.GitVcs.base_branch's rule, mirrored, because
     the ref-transaction hook bakes the same value). A detached or off-base
     shared checkout is the ref-guard's and post-checkout's business, not
     this rung's; narrow beats broad, because breadth is what gets a guard
     switched off.

  3. ORIGINATION. No integration is in progress. THIS IS THE FOLD DOOR and it
     is structural: `git merge` builds its commit through `pre-merge-commit`
     and never reaches a pre-commit rung at all (measured — a clean fold is
     admitted by never being seen), and a fold that STOPS on a conflict leaves
     MERGE_HEAD in the git dir, which this rung reads and admits. Same for
     CHERRY_PICK_HEAD, REVERT_HEAD and the rebase/sequencer directories: each
     one is git's own record that this commit REPLAYS content that already
     exists on another ref. Content that exists on another ref came from
     somewhere reviewable; content that exists nowhere else is ORIGINATION,
     and origination on trunk is the whole defect. An author cannot fake this
     without first putting the work on a ref — which is a lane.

  4. ESTATE. The shared checkout has at least one OTHER registered worktree.
     The measured harm REQUIRES an estate: a lane inherits main. A repo with
     one worktree has no lanes to poison and no "shared" checkout to speak
     of, and a rung that bricks `git commit` in every solo repo the moment
     `helm work install-guard` runs is a rung that gets uninstalled. Its
     residual hole is named in FALSE REFUSALS below rather than hidden.

FAIL-CLOSED ON UNKNOWN, LIKE conflict_marker AND landgate, and for landgate's
reason: what lands on a shared trunk is not recoverable by a retry. A venue
this rung cannot measure exits 2 and refuses. The one deliberate exception is
the ROOT commit (HEAD resolves to no commit at all): there is no trunk to
protect and no lane that could exist yet, so it is admitted — otherwise the
guard would refuse the first commit of every repo it is installed into.

=== THE ESCAPE HATCH, AND WHAT IT IS FOR ===

`HELM_WORK_INTEGRATOR=1` — the SAME declaration the reference-transaction and
post-checkout hooks already take, deliberately reused rather than invented: an
integrator holding the shared tree already exports it, and one declaration for
one role beats four. It is for the integrator's OWN direct commits to trunk —
a docs fix, an amend of a fold's message, a rescue — the commits that are
legitimately not folds. It is a DECLARATION, not a proof, and that is the
honest description: it says "I am acting as the integrator", the rung believes
it, and the whole cost of lying is that you have written down that you did.

`HELM_LANE_DISCIPLINE_SKIP=1` — the family-shaped one-commit skip (sibling to
HELM_CONFLICT_MARKER_SKIP / HELM_NEVER_TRACK_SKIP / HELM_HOSTPATH_SKIP),
checked by the hook so this rung's bypass disarms THIS rung and nothing else.

=== FALSE REFUSALS THIS RUNG CAN PRODUCE — NAMED, NOT HIDDEN ===

  * `git commit --amend` on trunk in the shared checkout, including amending a
    fold: the amend leaves no marker a hook can read, so it reads as
    origination. Costs the integrator one env var.
  * The owner committing by hand or from a GUI in the shared checkout. He is
    GUI-first and cannot always set an env var; his commit is refused with the
    hatch named in the message. This is the rung's real price and it was
    accepted knowingly — the alternative was keying on harness-session
    presence, which would have disarmed the rung on exactly the
    session-unbound seats the estate has already measured misbehaving.
  * A repo with no `main` branch guards whatever branch the shared checkout
    has out — that is base_branch's rule, not a bug, but in a repo adopted
    without lane discipline it will feel like one.

=== THE HOLE THIS RUNG DOES NOT CLOSE ===

An estate whose rooms have all been retired reads as a solo repo for as long
as that lasts (clause 4), and a lane claimed a minute later still inherits
whatever was committed during the gap. `helm work gc` closing the last room is
the realistic way to reach it. The rung is a narrower, not a proof.

Stdlib-only, no helm imports: the pre-commit hook executes an installed
SNAPSHOT of this file as `python3 <snapshot> --staged` with cwd at the
committing work tree's top — same canonical-scanner law as nevertrack, so a
lane cannot edit its own checked-out copy to neuter the rung for its own
commit, and the rung does not die with the tree it was installed from.
"""
import os
import subprocess
import sys

ADMIT = "admit"
REFUSE = "refuse"
UNKNOWN = "unknown"

# git's own records that the commit being built REPLAYS content already
# reachable from another ref. Paths are relative to the invoking --git-dir.
_INTEGRATION_MARKERS = (("MERGE_HEAD", "a merge (this is the fold door)"),
                        ("SQUASH_MSG", "a squash merge"),
                        ("CHERRY_PICK_HEAD", "a cherry-pick"),
                        ("REVERT_HEAD", "a revert"),
                        ("rebase-merge", "a rebase"),
                        ("rebase-apply", "a rebase"),
                        ("sequencer", "a sequencer run"))

INTEGRATOR_ENV = "HELM_WORK_INTEGRATOR"
SKIP_ENV = "HELM_LANE_DISCIPLINE_SKIP"


def _git(root, *args):
    """(rc, stdout, stderr), stripped. One helper, one spawn site — the rung
    runs as a plain script under the hook with no importable helm, so the vcs
    seam is not reachable from here (tests/test_vcs.py carries the reason)."""
    p = subprocess.run(("git",) + args, capture_output=True, cwd=root,
                       timeout=60, text=True)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def git_dirs(root):
    """(git_dir, common_dir) absolute, or (None, None) when git cannot say.

    Equal means THE SHARED CHECKOUT; a lane worktree answers
    `<common>/worktrees/<room>`. One spawn asks both, so the fleet's hot path
    — every commit in every lane — costs exactly one process."""
    rc, out, _err = _git(root, "rev-parse", "--path-format=absolute",
                         "--git-dir", "--git-common-dir")
    lines = out.splitlines()
    if rc != 0 or len(lines) != 2:
        return None, None
    return lines[0].strip(), lines[1].strip()


def base_branch(root):
    """The integration branch NAME, resolved exactly as vcs.GitVcs.base_branch
    does — `main` when the ref exists, else the shared checkout's own HEAD,
    else `main`. Mirrored rather than imported: the hook bakes this same value
    into its shell rungs, and the two must not drift."""
    if _git(root, "rev-parse", "--verify", "-q", "refs/heads/main")[0] == 0:
        return "main"
    # The SHARED checkout's (or a bare repository's) HEAD through the common
    # git dir, read live, shortened by git so a tag-shadowed branch keeps its
    # `heads/` qualifier — the same read and the same fallbacks as the seam.
    _git_dir, common = git_dirs(root)
    shared = ("--git-dir=" + common,) if common else ()
    rc, out, _err = _git(root, *shared, "symbolic-ref", "--short", "HEAD")
    return out if rc == 0 and out else "main"


def head_branch(root):
    """The short branch name HEAD points at, or None when detached. rc != 0
    from `symbolic-ref -q` is an ANSWER (detached), never a failure."""
    rc, out, _err = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    return out if rc == 0 and out else None


def has_root_commit(root):
    """Does HEAD resolve to a commit? False means this is the REPO'S FIRST
    commit — no trunk to protect, no lane that could exist yet."""
    return _git(root, "rev-parse", "--verify", "-q", "HEAD")[0] == 0


def integration_in_progress(git_dir):
    """The human name of the in-flight integration, or None.

    A fold that stops on a conflict is finished by `git commit`, which is the
    ONE way a real fold reaches a pre-commit rung — this is what admits it."""
    for name, label in _INTEGRATION_MARKERS:
        if os.path.exists(os.path.join(git_dir, name)):
            return label
    return None


def estate_size(root):
    """How many worktrees this repo has registered, or None when git cannot
    say. Counting is `--porcelain` because the human listing wraps."""
    rc, out, _err = _git(root, "worktree", "list", "--porcelain")
    if rc != 0:
        return None
    return sum(1 for line in out.splitlines() if line.startswith("worktree "))


def verdict(root, environ=None):
    """(state, detail) — ADMIT / REFUSE / UNKNOWN for the commit being built
    in the work tree at `root`. Pure enough to test directly: everything it
    reads is git's own state plus the declared environment."""
    env = os.environ if environ is None else environ
    if env.get(INTEGRATOR_ENV) == "1":
        return ADMIT, "%s=1 declares the integrator's own commit" % INTEGRATOR_ENV
    git_dir, common = git_dirs(root)
    if git_dir is None:
        return UNKNOWN, ("cannot identify the invoking git directory — the "
                         "venue is unmeasurable, so this refuses")
    if git_dir != common:
        return ADMIT, "a private lane worktree, which is where work belongs"
    base = base_branch(root)
    branch = head_branch(root)
    if branch is None:
        return ADMIT, ("the shared checkout is detached — HEAD departure is "
                       "the reference-transaction guard's law, not this one")
    if branch != base:
        return ADMIT, ("the shared checkout is on '%s', not the base branch "
                       "'%s'" % (branch, base))
    if not has_root_commit(root):
        return ADMIT, "the repository's root commit — there is no trunk yet"
    marker = integration_in_progress(git_dir)
    if marker:
        return ADMIT, ("%s is in progress, so this commit replays content that "
                       "already exists on another ref" % marker)
    rooms = estate_size(root)
    if rooms is None:
        return UNKNOWN, ("cannot read the worktree registry — the estate is "
                         "unmeasurable, so this refuses")
    if rooms < 2:
        return ADMIT, ("this repo has one worktree, so there is no shared "
                       "checkout and no lane that could inherit the commit")
    return REFUSE, ("originating work on '%s' in the shared checkout %s, with "
                    "%d rooms cut from it" % (base, root, rooms))


def _refusal(root, detail):
    """The refusal, verbatim. It states the failure MODE (not just the rule),
    names the exact command that fixes it, and documents both hatches and what
    each is FOR — the house voice its sibling rungs set."""
    base = base_branch(root)
    return [
        "REFUSED: %s" % detail,
        "no lane holds this change, so no gate ran on it and no cross-family "
        "verdict covers it — a reviewer's own cure is welcome, but it belongs "
        "on a branch off the tip they reviewed, in their own worktree, named "
        "on the verdict; this is the shared checkout's base branch. And that "
        "is not the worst of it: every lane cut "
        "from '%s' afterwards INHERITS the commit silently. Their diff against "
        "origin/%s shows YOUR change as THEIRS, and because a verdict is a "
        "statement about a TREE, an APPROVE on their lane covers your commit "
        "whether the reviewer noticed it or not. Measured 2026-08-03, twice."
        % (base, base),
        "move it into a lane — the change is not lost, `git stash -u` carries "
        "it whole:",
        "    git stash -u",
        "    helm work claim <lane-name> --seat <your-seat-name>",
        "    cd <the room it prints> && git stash pop",
        "A FOLD IS NOT AFFECTED BY THIS RUNG, and that is deliberate — folds "
        "are the one correct path onto '%s'. `git merge` builds its commit "
        "through pre-merge-commit and never reaches a pre-commit rung, and a "
        "fold that stops on a conflict carries MERGE_HEAD, which this rung "
        "reads and admits with no env at all." % base,
        "ESCAPE HATCH, for the INTEGRATOR'S OWN direct commits to trunk (a "
        "docs fix, an amend of a fold, a rescue) and not for a lane in a "
        "hurry — the same declaration the ref-transaction guard already "
        "takes:  %s=1 git commit ..." % INTEGRATOR_ENV,
        "one-commit skip for THIS rung alone: %s=1" % SKIP_ENV,
        "this rung cannot tell WHO you are — every commit in this estate "
        "carries one git identity — so it asks WHERE you are and WHETHER the "
        "content already exists on another ref. rung: helm/lane_discipline.py",
    ]


def main(argv=None):
    """The pre-commit entry: judge the venue of the commit being built at cwd.
    Exit 0 admits, 1 refuses, 2 when the venue itself cannot be measured (fail
    CLOSED — an unmeasured venue is not a cleared one)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv != ["--staged"]:
        sys.stderr.write("usage: lane_discipline.py --staged  (run by the "
                         "helm pre-commit guard; cwd must be inside the "
                         "repo)\n")
        return 2
    rc, root, _err = _git(os.getcwd(), "rev-parse", "--show-toplevel")
    if rc != 0 or not root:
        sys.stderr.write("[helm lane-discipline] REFUSED: not inside a git "
                         "work tree — the commit venue cannot be measured\n")
        return 2
    try:
        state, detail = verdict(root)
    except (OSError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write("[helm lane-discipline] REFUSED: venue check failed "
                         "— %s\n" % exc)
        return 2
    if state == ADMIT:
        return 0
    w = sys.stderr.write
    if state == UNKNOWN:
        w("[helm lane-discipline] REFUSED: %s\n" % detail)
        w("[helm lane-discipline] override: %s=1 (integrator) or %s=1 (this "
          "rung only)\n" % (INTEGRATOR_ENV, SKIP_ENV))
        return 2
    for line in _refusal(root, detail):
        w("[helm lane-discipline] %s\n" % line)
    return 1


if __name__ == "__main__":
    sys.exit(main())
