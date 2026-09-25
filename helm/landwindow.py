"""THE LANDING WINDOW COMPOSES ITSELF: one verb lists the approve-ready rows,
MERGES each reviewed tip into ONE detached room, and gates that room through
the landing-window door.

WHAT IT REPLACES. The integrator composed every landing train by hand: stand a
detached room on trunk, merge each approve-ready lane into it with a
"trainNNN: merge lane <name>" message, then launch one whole-suite gate through
`helm gate window launch` (helm/gatewindow.py). No verb produced those merges,
so the train was only as good as that session's memory of which rows were
ready, which order it used and whether it resolved a conflict on the way.

WHY MERGE, AND NOT THE CHERRY-PICK `helm lr compose` USES. A verdict binds the
reviewed tip BY SHA. A merge keeps that exact sha as a parent of the composed
head, so once the train lands the reviewed commit is an ANCESTOR of trunk, and
foldcheck's ancestry rung proves "the reviewed content landed" with one
`merge-base --is-ancestor`. A cherry-pick mints new shas, and the proof becomes
patch equivalence, which is one hop weaker and has to be re-derived. `helm lr
compose` keeps its cherry-pick and its patch-id re-measure on purpose for its
own callers; this module does not touch it.

WHAT IT REUSES, rather than rebuilds:

  * THE APPROVE-READY LISTING is the land-request projection's own READY word
    (`landreq.project`), the same word `helm lr compose` admits on. This module
    never re-judges a verdict; it reads the projection.
  * THE ROOM is the detached worktree under `<repo>-wt/compose/` that `helm lr
    compose` already stands, minted with the same integrator sanction, at a path
    `work._lanes.lane_path` derives.
  * THE GATE is `gatewindow.launch`, the door of task/2799. It refuses a second
    whole suite on one window, and that refusal comes back here unchanged. This
    module never dispatches a suite and never supersedes one.
  * THE TRUNK AUTHORITY is the repository's declared one (`helm.trunkRef`,
    `helm.trunkRemote`), observed through `vcs.observe_trunk_authority`. The
    plan stands on a LOCAL snapshot of trunk, so the dry run prints the
    remote's head beside it, and `--apply` refuses when the two differ or the
    remote's head is UNKNOWN: a train composed on a stale trunk is gated over
    a window that has already moved.

THE DRIFT CAP. A car whose reviewed tip is more than `--max-behind` commits
behind the trunk the train stands on (`rev-list --count <tip>..<trunk>`, 200 by
default) is skipped and named in the plan with its count: rebase it or close
it. Its review and its gate saw a trunk that many commits older, so the
composed room is the first place that work meets what trunk holds now, and
when it fails there, the whole train's gate fails with it. A count git cannot
give is UNKNOWN, and an UNKNOWN drift is skipped too, because nothing shows it
is under the cap. The cap has a floor of 10: a lower cap skips lanes cut only
a few landings ago, which is a typo and not a policy.

THE FOUR REFUSALS EACH MERGE CAN MEET, and what each leaves behind:

  THE ROOM IS NOT THE ROOM. Before every merge, git is asked which checkout the
  room path resolves to. A merge runs in whatever tree git discovers, and a room
  directory that has lost its `.git` pointer resolves UPWARD into an enclosing
  checkout. So the toplevel must be the room itself, a LINKED worktree of this
  project, on a detached HEAD. Anything else stops the train before the merge.

  A CONFLICT. The merge is aborted and never resolved, the row and its lane are
  named, and the other rows still compose. Nothing else is removed: the room
  keeps every merge that went in before it.

  RERERE. Every merge runs with `-c rerere.enabled=false`. With rerere on, a
  conflict is recorded, and the next time the same hunks meet, git applies the
  earlier resolution by itself, a resolution nobody reviewed on this tree.

  A MERGE THAT CANNOT BE UNDONE. If the abort fails or leaves residue, the state
  of the room is UNKNOWN. The train stops there, and nothing is launched over a
  tree nobody can describe.

THE FIRST APPROVE IS ALREADY IN. The fleet's rule is to gate a first-read tip
only after its first approve is in the ledger, because a FIX would make that
suite bind nothing. That rule is a stored heuristic
(gate-after-the-first-approve-on-a-first-read), and `gatewindow.launch` does not
check it. This verb holds it by construction instead: every car is READY, so
each one carries its authorizing approve before the gate is spent.
"""
import os
import re
import sys

from . import dispatches, gatewindow, landreq, rowworld, vcs
from .work import _lanes

PROG = "helm train"
USAGE = ("usage: helm train [--repo PATH] [--trunk REF] [--name TRAIN] "
         "[--max-behind N] [--apply]")
BOX = "compose"

# A merge that runs longer than this is a merge nobody is watching.
MERGE_TIMEOUT_S = 300

# EVERY MERGE CARRIES THIS, including the abort. See the module docstring.
NO_RERERE = ("-c", "rerere.enabled=false")

# How far back trunk's history is read for the last train number. The newest
# train merges come first, so the window only has to reach the latest one.
TRAIN_SCAN = 200
_TRAIN_SUBJECT = re.compile(r"\Atrain(\d+):")

# THE DRIFT CAP (see the module docstring): the default, and the floor below
# which a --max-behind value is refused. A count is at most nine digits, so a
# huge value parses as a number and never as an overflow.
MAX_BEHIND = 200
MAX_BEHIND_FLOOR = 10
_COUNT = re.compile(r"\A[0-9]{1,9}\Z")
_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

MERGED = "MERGED"
CONTAINED = "CONTAINED"
REFUSED = "REFUSED"
STUCK = "STUCK"

# THE READY WORD'S OWN DOOR-CAUTION RUNGS, each with `landreq.ready_rung`'s
# meaning for it: nobody independent looked, or an unanswered FIX stands on
# this tip. No land verb enforces them — `helm lr land` does not read the READY
# word — so this verb does, and excludes each such row by name. The other
# rungs stay in the train with their word printed: a STALE-BASE car is over
# the drift cap at its default and skipped by name, one a higher cap admits is
# refused by name at its merge if it no longer merges, and READY-UNVERIFIED is
# sorted by `_unverified` below.
DOOR_CAUTION = {
    "READY-SELF-REVIEW": "nobody independent looked",
    "READY-CONTESTED": "an unanswered FIX stands on this tip",
}
UNVERIFIED = "READY-UNVERIFIED"


def _env():
    """The overlay every git call here runs under: repository selection and
    config injection REMOVED. `git -C <room>` does not beat an ambient GIT_DIR,
    so without this a merge could land in whatever repository the calling
    process happened to be pointed at."""
    return vcs._authority_env()


def _first(text):
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return "no diagnostic"


def _short(sha):
    return (sha or "?")[:12]


def approve_ready(lrs, identity):
    """(cars, excluded) for one project, cars in MERGE ORDER.

    A car is a LIVE row the projection calls READY, bound to this repository,
    with a reviewed tip. A row bound to ANOTHER repository is not this window's
    business and is not listed. A READY row with no binding at all is listed as
    excluded, because nothing places it here and merging it would make that
    claim for it.

    LIVE IS HALF OF THE WORD, AND THE FIRST DRY RUN ON THE REAL LEDGER IS WHY.
    The projection keeps the stored state READY on a row that has since CLOSED
    (landed, discharged, retired) and marks it `terminal`. `helm lr list`
    asks `landreq.live_ready` (READY and not terminal) for exactly that
    reason, and so does this listing. Keyed on the state word alone, the first
    live dry run found 459 READY rows for helm, 457 of them already closed as
    landed. It offered 216 as cars, and 214 of those were closed rows that had
    landed rebased: their reviewed tips are no ancestors of trunk, so the
    train would have merged each old tip back in. Two were live.

    ORDER is the order the rows became READY, oldest first, so the train lands
    work in the order it was approved. A row whose entry instant is unknown
    sorts last; the row id breaks ties so two runs agree.
    """
    cars, excluded = [], []
    for lr in (lrs or {}).values():
        if not landreq.live_ready(lr):
            continue
        bound = lr.get("repo_id")
        if bound and bound != identity:
            continue
        car = {"id": lr.get("id") or "?", "lane": lr.get("lane") or "?",
               "tip": lr.get("reviewed_tip") or "",
               "entered": lr.get("entered_ts"), "lr": lr}
        if not bound:
            excluded.append(dict(car, why="carries no repository binding, so "
                                          "nothing places it in this project"))
        elif not car["tip"]:
            excluded.append(dict(car, why="has no reviewed tip to merge"))
        else:
            cars.append(car)
    cars.sort(key=lambda c: (c["entered"] is None, str(c["entered"] or ""),
                             c["id"]))
    return cars, excluded


def _unverified(lr):
    """(exclusion, reason) for a READY-UNVERIFIED row: the sentence that
    excludes it, or None and the rung's own reason for keeping its word.

    THE WORD FOLDS SEVERAL UNKNOWNS, and this train's gate answers only one of
    them. A receipt the ledger cannot speak for is what the whole-suite gate
    on the composed room measures again. A row helm could not observe has its
    tip and its ancestry measured by `plan` itself. But an unreadable
    contributor chain means nobody can say whether anyone independent looked,
    which is the SELF-REVIEW rung UNKNOWN, and no suite answers that. So that
    row is excluded, and every other one is kept with the rung's own reason
    beside its word."""
    independent, why = landreq.independent_review(lr)
    if independent is None:
        return ("is %s: independence UNKNOWN, which this train's gate cannot "
                "answer (%s)" % (UNVERIFIED, why)), None
    return None, landreq.ready_rung_why(lr)[1]


def trunk_authority(root, gitdir):
    """{ref, remote, sha, why} — the DECLARED trunk authority of the
    repository whose common git dir is `gitdir`, observed NOW. `sha` is None
    exactly when `why` says why.

    THE PLAN STANDS ON A LOCAL SNAPSHOT. `origin/main` says what the last fetch
    left behind, not what the remote holds, so a train composed on it can be
    gated over a trunk that has already moved. `vcs.observe_trunk_authority`
    is the seam that answers the remote's current head, and the declaration it
    is asked about (`helm.trunkRef`, `helm.trunkRemote`) is read by the one
    reader that already feeds it when a build is dispatched. A remote
    authority is observed with a bounded fetch of that one ref.

    NOTHING DECLARED IS UNKNOWN, NOT AGREEMENT. Without a declaration there is
    no remote to ask, and reading the snapshot as its own authority would pass
    exactly the stale trunk this check exists to catch.

    THE GIT DIR, NEVER THE CHECKOUT: the reader asks git with `--git-dir`, and
    a checkout path there reads every key as absent, which is the undeclared
    answer given to a repository that declared one."""
    ref, remote, sha, failure = dispatches._declared_authority(gitdir)[:4]
    if ref is None:
        failure = ("no trunk authority is declared here, so the remote this "
                   "trunk is a snapshot of cannot be asked; declare it: git -C "
                   "%s config helm.trunkRef refs/heads/<branch> (and "
                   "helm.trunkRemote <remote>)" % root)
    elif not sha and not failure:
        failure = "%s is declared but no sha was observed for it" % ref
    return {"ref": ref, "remote": remote, "sha": None if failure else sha,
            "why": failure}


def authority_refusal(got):
    """None when the trunk the plan stands on IS the observed authority, else
    the sentence `--apply` refuses with."""
    auth = got["authority"]
    if not auth["sha"]:
        return ("the trunk authority is UNKNOWN (%s), and UNKNOWN is not "
                "agreement" % auth["why"])
    if auth["sha"] != got["trunk"]:
        return ("this plan stands on %s at %s, but the trunk authority %s "
                "holds %s: fetch first%s, then run it again"
                % (got["ref"], _short(got["trunk"]), _authority_name(auth),
                   _short(auth["sha"]),
                   " (git fetch %s)" % auth["remote"] if auth["remote"]
                   else ""))
    return None


def _authority_name(auth):
    return "%s on %s" % (auth["ref"] or "(undeclared)",
                         auth["remote"] or "this repository")


def next_train(be, root, trunk):
    """(name, refusal) — one past the highest `trainN:` subject on trunk."""
    rc, out, err = be.text(root, "log", "-E", "--grep=^train[0-9]+:",
                           "--format=%s", "-n", str(TRAIN_SCAN), trunk,
                           env=_env())
    if rc != 0:
        return None, ("trunk's train history is unreadable (%s), so the next "
                      "train has no number; --name names it" % _first(err))
    numbers = [int(m.group(1)) for m in
               (_TRAIN_SUBJECT.match(line) for line in out.splitlines()) if m]
    return "train%d" % (max(numbers, default=0) + 1), None


def behind(be, root, tip, trunk):
    """How many commits of `trunk` the reviewed `tip` lacks, or None when git
    could not count them: `rev-list --count <tip>..<trunk>`, the measure
    `landreq._base_behind` takes for the READY-STALE-BASE rung, asked here of
    the exact trunk commit this train stands on."""
    rc, out, _err = be.text(root, "rev-list", "--count",
                            "%s..%s" % (tip, trunk), env=_env())
    return int(out) if rc == 0 and _COUNT.fullmatch(out) else None


def parse_max_behind(raw):
    """(n, refusal) for one --max-behind value: a whole number of commits, at
    least MAX_BEHIND_FLOOR."""
    if not _COUNT.fullmatch(raw or ""):
        return None, ("--max-behind takes a whole number of commits, not %r"
                      % raw)
    n = int(raw)
    if n < MAX_BEHIND_FLOOR:
        return None, ("--max-behind %d is under the floor of %d: a cap that "
                      "low skips lanes cut only a few landings ago"
                      % (n, MAX_BEHIND_FLOOR))
    return n, None


def plan(repo, trunk=None, name=None, project=None, max_behind=MAX_BEHIND):
    """(plan, refusal) — everything this window would do, measured, with
    nothing minted or merged. The one write is the trunk authority's own
    bounded fetch of the declared ref (`trunk_authority`), the same one a
    build dispatch makes. `project` is the listing seam; its default is the real
    projection. `max_behind` is the drift cap (see the module docstring)."""
    root = _lanes.find_root(repo)
    if not root:
        return None, "%s is not inside a git repository" % repo
    be = vcs.backend(root)
    identity, err = rowworld.repo_identity(root)
    if err:
        return None, err
    ref = trunk or be.trunk_ref(root)
    rc, sha, _err = be.text(root, "rev-parse", "--verify", "-q",
                            ref + "^{commit}", env=_env())
    if rc != 0 or not sha:
        return None, "trunk %s does not resolve in %s" % (ref, root)
    authority = trunk_authority(root, identity)
    lrs, unavailable = (project or landreq.project)()
    if unavailable:
        return None, "the land-request projection is unavailable: %s" \
            % unavailable
    ready, excluded = approve_ready(lrs, identity)
    cars, seen = [], {}
    for car in ready:
        tip = car["tip"]
        word = landreq.ready_word(car["lr"])
        if word in DOOR_CAUTION:
            excluded.append(dict(car, why="is %s: %s. That is a door-caution "
                                          "rung of the READY word, and no "
                                          "land verb enforces it, so this "
                                          "verb does; `helm lr show %s` names "
                                          "why" % (word, DOOR_CAUTION[word],
                                                   _short(car["id"]))))
            continue
        reason = None
        if word == UNVERIFIED:
            why, reason = _unverified(car["lr"])
            if why:
                excluded.append(dict(car, why=why))
                continue
        if tip in seen:
            excluded.append(dict(car, why="has the same reviewed tip as %s, "
                                          "whose merge carries it"
                                 % _short(seen[tip])))
            continue
        if be.text(root, "cat-file", "-e", tip + "^{commit}",
                   env=_env())[0] != 0:
            excluded.append(dict(car, why="reviewed tip %s is not readable "
                                          "here" % _short(tip)))
            continue
        state = be.ancestry(root, tip, sha)
        if state == vcs.ANCESTOR:
            excluded.append(dict(car, why="reviewed tip %s is already on %s — "
                                          "close it, do not compose it"
                                 % (_short(tip), ref)))
            continue
        if state != vcs.NOT_ANCESTOR:
            excluded.append(dict(car, why="git could not say whether %s is "
                                          "already on %s (ancestry UNKNOWN)"
                                 % (_short(tip), ref)))
            continue
        drift = behind(be, root, tip, sha)
        if drift is None:
            excluded.append(dict(car, why="git could not count how far %s is "
                                          "behind %s (drift UNKNOWN), so "
                                          "nothing shows it is under the "
                                          "drift cap of %d"
                                 % (_short(tip), ref, max_behind)))
            continue
        if drift > max_behind:
            excluded.append(dict(car, behind=drift,
                                 why="reviewed tip %s is %d commits behind %s "
                                     "at %s, over the drift cap of %d "
                                     "(--max-behind): rebase it or close it"
                                 % (_short(tip), drift, ref, _short(sha),
                                    max_behind)))
            continue
        seen[tip] = car["id"]
        cars.append(dict(car, word=word, reason=reason, behind=drift))
    if name is None:
        name, err = next_train(be, root, sha)
        if err:
            return None, err
    elif not _NAME.fullmatch(name):
        return None, ("train name %r is not a plain name (letters, digits, "
                      "dot, dash, underscore; it becomes a directory)" % name)
    return {"root": root, "identity": identity, "ref": ref, "trunk": sha,
            "authority": authority, "max_behind": max_behind,
            "train": name, "room": _lanes.lane_path(root,
                                                    os.path.join(BOX, name)),
            "cars": cars, "excluded": excluded}, None


def render(got):
    auth = got["authority"]
    lines = ["%s: %s over %s at %s" % (PROG, got["train"], got["ref"],
                                       _short(got["trunk"])),
             "  trunk authority: %s" % (
                 "%s at %s (%s the local snapshot)"
                 % (_authority_name(auth), _short(auth["sha"]),
                    "AGREES WITH" if auth["sha"] == got["trunk"]
                    else "DIFFERS FROM") if auth["sha"]
                 else "UNKNOWN — %s" % auth["why"]),
             "  room: %s (detached at %s)" % (got["room"],
                                              _short(got["trunk"])),
             "  drift cap: a car more than %d commits behind %s is skipped "
             "(--max-behind)" % (got["max_behind"], got["ref"]),
             "  merge order, %d approve-ready row%s:"
             % (len(got["cars"]), "" if len(got["cars"]) == 1 else "s")]
    for i, car in enumerate(got["cars"], 1):
        lines.append("    %d. %s  lane %s  reviewed tip %s  %s%s  (%d behind)"
                     % (i, _short(car["id"]), car["lane"], _short(car["tip"]),
                        car["word"], " — %s" % car["reason"]
                        if car.get("reason") else "", car["behind"]))
    if not got["cars"]:
        lines.append("    (none)")
    for car in got["excluded"]:
        lines.append("  EXCLUDED %s (lane %s): %s"
                     % (_short(car["id"]), car["lane"], car["why"]))
    return "\n".join(lines)


def room_refusal(be, room, identity=None):
    """None when `room` is a place a merge may run, else why it is not.

    Asked BEFORE EVERY MERGE, of git itself: the toplevel git resolves the path
    to must be the room, the room must be a linked worktree (never the shared
    checkout), it must belong to this project, and its HEAD must be detached.
    """
    env = _env()
    rc, out, err = be.text(room, "rev-parse", "--path-format=absolute",
                           "--show-toplevel", "--git-dir", "--git-common-dir",
                           env=env)
    found = out.splitlines() if rc == 0 else []
    if len(found) != 3:
        return "git cannot name the checkout at %s (%s)" % (room, _first(err))
    top, gitdir, common = (os.path.realpath(p) for p in found)
    if top != os.path.realpath(room):
        return ("git resolves %s to the checkout %s, so a merge there would "
                "land in THAT tree and not in the room" % (room, top))
    if gitdir == common:
        return "%s is a repository's own checkout, not a compose room" % room
    if identity and common != identity:
        return "%s belongs to %s, not to this project (%s)" % (room, common,
                                                              identity)
    rc, branch, _err = be.text(room, "symbolic-ref", "-q", "HEAD", env=env)
    if rc == 0:
        return ("%s has %s checked out; a compose room is DETACHED, and a "
                "merge here would move that branch" % (room, branch))
    return None


def _residue(be, room, before, env):
    """What a refused merge left behind, or "" when the room is exactly as it
    stood before that merge started."""
    rc, head, _err = be.text(room, "rev-parse", "--verify", "-q", "HEAD",
                             env=env)
    if rc != 0 or head != before:
        return "HEAD reads %s, not %s" % (_short(head) if head
                                          else "unreadable", _short(before))
    if be.text(room, "rev-parse", "-q", "--verify", "MERGE_HEAD",
               env=env)[0] == 0:
        return "a merge is still in progress (MERGE_HEAD stands)"
    rc, dirt, _err = be.text(room, "status", "--porcelain", env=env)
    if rc != 0:
        return "the room's status is unreadable"
    return "the room is not clean (%s)" % _first(dirt) if dirt else ""


def merge_car(be, room, car, train, identity=None):
    """(outcome, detail, head) for one car.

    MERGED: the room's HEAD is a new merge whose parents are the old head and
    EXACTLY the reviewed tip. CONTAINED: the room already holds the tip.
    REFUSED: the merge stopped, was aborted, and the room is exactly as it
    stood. STUCK: the room is not the room, or its state is unknown — the train
    stops there.
    """
    why = room_refusal(be, room, identity)
    if why:
        return STUCK, "the room assertion refused the merge: " + why, None
    env = _env()
    rc, before, err = be.text(room, "rev-parse", "--verify", "-q", "HEAD",
                              env=env)
    if rc != 0 or not before:
        return STUCK, "the room's HEAD is unreadable (%s)" % _first(err), None
    tip = car["tip"]
    if be.ancestry(room, tip, before) == vcs.ANCESTOR:
        return CONTAINED, "the room already holds %s" % _short(tip), before
    message = "%s: merge lane %s" % (train, car["lane"])
    rc, out, err = be.text(
        room, *NO_RERERE, "merge", "--no-ff", "--no-edit", "--no-log",
        "-m", message, "-m", "land request %s, reviewed tip %s"
        % (car["id"], tip), tip, env=env, timeout=MERGE_TIMEOUT_S)
    if rc == 0:
        rc, line, _err = be.text(room, "rev-list", "--parents", "-n", "1",
                                 "HEAD", env=env)
        got = line.split()
        if rc != 0 or got[1:] != [before, tip]:
            return STUCK, ("the merge reported success, but HEAD's parents "
                           "read %s, not %s then %s"
                           % (" ".join(_short(s) for s in got[1:]) or
                              "unreadable", _short(before), _short(tip))), None
        return MERGED, message, got[0]
    rc2, listing, _err = be.text(room, "diff", "--name-only", "-z",
                                 "--diff-filter=U", env=env)
    files = [f for f in listing.split("\0") if f] if rc2 == 0 else []
    cause = ("conflict in %s" % ", ".join(files) if files else
             "git stopped the merge without a conflict: %s"
             % _first(err or out))
    if be.text(room, "rev-parse", "-q", "--verify", "MERGE_HEAD",
               env=env)[0] == 0:
        rc, _out, err = be.text(room, *NO_RERERE, "merge", "--abort", env=env)
        if rc != 0:
            return STUCK, ("%s, and the abort FAILED (%s)"
                           % (cause, _first(err))), None
    left = _residue(be, room, before, env)
    if left:
        return STUCK, "%s, and after the abort %s" % (cause, left), None
    return REFUSED, cause, before


def compose(repo, trunk=None, name=None, apply=False, project=None, door=None,
            out=None, max_behind=MAX_BEHIND):
    """The verb. Returns the exit code.

    Dry run unless `apply`. With `apply`: mint the room, merge every car in
    order, then launch the gate through the door. `door` holds the door's own
    seams (the fab calls, the node and authority reads, the clock); an arm
    passes spies there so it observes the SHIPPED door, never a stand-in.

    EXIT: 0 every car merged and the door dispatched; the DOOR'S OWN CODE when
    the door refuses or fails (2, 3, 4), passed through untouched; 1 when a car
    was refused (the door still gated the others), when the train stopped,
    when the trunk the plan stands on is not the observed trunk authority (or
    that authority is UNKNOWN), or when there was nothing to compose.
    """
    out = out if out is not None else sys.stdout
    got, why = plan(repo, trunk=trunk, name=name, project=project,
                    max_behind=max_behind)
    if why:
        print("%s: REFUSED — %s" % (PROG, why), file=out)
        return 1
    print(render(got), file=out)
    if not apply:
        print("  dry run: nothing minted, merged or launched. `helm train "
              "--apply` composes this train and gates it through `helm gate "
              "window launch`.", file=out)
        return 0
    stale = authority_refusal(got)
    if stale:
        print("%s: REFUSED — %s. Nothing was minted, merged or launched."
              % (PROG, stale), file=out)
        return 1
    if not got["cars"]:
        print("%s: nothing is approve-ready in this project, so no room was "
              "minted and nothing was launched" % PROG, file=out)
        return 1
    room, root = got["room"], got["root"]
    if os.path.lexists(room):
        print("%s: REFUSED — %s already exists. Its HEAD may be the only "
              "anchor of an earlier train: land it or remove it, or name "
              "another train with --name." % (PROG, room), file=out)
        return 1
    be = vcs.backend(root)
    os.makedirs(os.path.dirname(room), exist_ok=True)
    # THE SAME SANCTION `helm lr compose` HANDS THE REF GUARD: minting an
    # integration room from the shared checkout is the integrator's act, and
    # the bit is scoped to this one call, never set on the process.
    rc, _out, err = be.text(root, "worktree", "add", "--detach", room,
                            got["trunk"],
                            env=dict(_env(), HELM_WORK_INTEGRATOR="1"))
    if rc != 0:
        print("%s: cannot mint the room %s: %s" % (PROG, room, _first(err)),
              file=out)
        return 1
    merged, refused = [], []
    for car in got["cars"]:
        outcome, detail, head = merge_car(be, room, car, got["train"],
                                          identity=got["identity"])
        label = "%s (lane %s)" % (_short(car["id"]), car["lane"])
        if outcome == STUCK:
            print("  STOPPED at %s: %s.\n  The room %s is left exactly as it "
                  "stands, and nothing was launched." % (label, detail, room),
                  file=out)
            return 1
        if outcome == REFUSED:
            refused.append(car)
            print("  REFUSED %s: %s. The merge was aborted and NOT resolved; "
                  "this lane owes a tip that merges onto %s."
                  % (label, detail, _short(got["trunk"])), file=out)
        elif outcome == CONTAINED:
            print("  CONTAINED %s: %s" % (label, detail), file=out)
        else:
            merged.append(car)
            print("  MERGED %s at %s: %s" % (label, _short(head), detail),
                  file=out)
    if not merged:
        print("%s: no car merged, so there is nothing to gate. The room %s "
              "stands at trunk %s; nothing was launched."
              % (PROG, room, _short(got["trunk"])), file=out)
        return 1
    rc, _request = gatewindow.launch(room, label=got["train"],
                                     trunk_ref=got["trunk"], out=out,
                                     **(door or {}))
    if rc:
        return rc
    return 1 if refused else 0


def cmd_train(args):
    """helm train — the landing window composes itself (dry run by default)."""
    from .cli import guard_tail
    valued = ("--repo", "--trunk", "--name", "--max-behind")
    rc = guard_tail(PROG, args, flags=("--apply",), valued=valued,
                    usage=USAGE)
    if rc is not None:
        return rc
    opts = {a: args[i + 1] for i, a in enumerate(args) if a in valued}
    cap, why = parse_max_behind(opts.get("--max-behind", str(MAX_BEHIND)))
    if why:
        print("%s: %s (%s)" % (PROG, why, USAGE), file=sys.stderr)
        return 2
    return compose(opts.get("--repo") or os.getcwd(),
                   trunk=opts.get("--trunk"), name=opts.get("--name"),
                   apply="--apply" in args, max_behind=cap)
