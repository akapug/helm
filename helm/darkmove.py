"""The dark-seat mover: work owed by a seat nothing can reach moves to a live
seat, on the idle-dispatch tick (task/3587).

THE OWNER, VERBATIM: "jsut reassign anything like that from exhausted cred
agents to actiuve agents, no? that's what i wanthelm to be good at". Measured
the same night: kimi sat on a quota wall still owning six tasks, and nothing
moved work off a dark seat. `helm-seat-rebind.timer` rebinds pane identity,
and idle-dispatch only nags.

ONE JUDGEMENT OF DARK, each leg read from the reader that already owns it:

  FAMILY      `helm burn` reads the seat's family RED on MONEY or REACH: a
              vendor quota wall, an upstream outage, or a refusal on a seat's
              own turn (`turnwall`, which the fold reads).
  TURN-WALL   the seat's own last turn ended on a billing or credential
              refusal (`turnwall.seat_wall`), whatever its siblings read.
  NO ROUTE    the usability join measures the seat's pane GONE and no armed
              beacon: nothing can deliver a row to it or wake it.

A seat whose pane is live but whose beacon is not (DEAF) is NOT dark: the
beacons census already asks such a pane to re-arm, and `helm seat
resume-turn --nudge` types the same wake now, so it is reported once with
that route and its rows stay. A seat the remote relay drives is never judged
here: its liveness is its relay session (`idle_dispatch._relay_reading`), and
a row the relay cannot account for is UNKNOWN, which moves nothing.

A seat or family is CONFIRMED dark only once this mover has read it dark on
every tick for GRACE_S (HELM_DARK_MOVE_GRACE_S, default ten minutes), so one
bad reading moves nothing. The first sighting is latched in this mover's own
state file, and a reading that stops is dropped.

WHAT MOVES, from fields that already exist:

  a dispatch row  OPEN (not held, not answered: `dispatches.open_rows`) and
                  its recipient NOT visibly working it: `progress_state` is
                  IDLE, i.e. the recipient holds neither a claim on the row
                  nor the dispatched lane's lease; the recipient holds no
                  room of the lane's family (`rebind_room_fence`, the rooms a
                  rebind would leave fenced); and, for any row but a review,
                  no lane branch of that family carries commits that are on
                  neither trunk nor the dispatched ref. A WORKING row never
                  moves on its own; it moves only beside its started task.
  a task          `open` with an owner (`helm task claim` sets `in_progress`,
                  so an owned `open` row was assigned and never claimed), the
                  owner holding no live lease that names the task (`task-N`
                  in the lease), and no `lane/…task-N…` branch of this helm's
                  repository carrying commits not on trunk: UNSTARTED.
  started work    (task/3881's remainder, after task/4019 sat 19h as four
                  unreviewed commits on a walled kimi's lane) a task
                  `in_progress`, or `open` with a live lease or lane commits
                  of its own (`_task_holding`). Every room git's worktree
                  registry has checked out on one of its lanes is read
                  (`_task_rooms`). With NO uncommitted change in any of them
                  (`git status --porcelain`: tracked changes and untracked
                  files count, ignored files do not; a merge or rebase in
                  progress counts), it moves: the task's own leases
                  (`rebind_claim_holder(only=…)`, rolled back if the owner
                  move refuses), then its owner (the seat-reassign
                  capability; its status stays), then the dark seat's
                  WORKING dispatch rows on the same lanes (`dispatch
                  rebind`). The receiving seat gets ONE DM: the task, each
                  lane, its room and tip, and "continue from this tip:
                  commits on the lane are the dark seat's finished work;
                  review and land them, then finish the task". A DIRTY room
                  moves nothing: one decision row (below) @mentions the
                  integrator, the `build-lanes` steward, naming the seat,
                  the task, the room and the dirty paths. Started tasks move
                  first, P0 then P1 then the rest (`tasks._rank`).
                  The capability's LIVE veto stands, with no force: a seat
                  whose pane process runs but whose liveness reads WALLED or
                  BLOCKED_ON_QUOTA, its reset past RESET_WAIT_S, is not live
                  for work (`seat_reassign.source_disposition`), and the
                  task's record carries that classification and its reset.

A FACT THAT DOES NOT READ MOVES NOTHING: an unreadable claims ledger, lane
refs that git cannot list or count, a task of another project's repository,
or a project team that does not read keeps the work where it is.

WHERE IT GOES. A review goes to a measured-eligible reader `helm reviewers`
names (`reviewer_eligibility`), which already refuses every seat that wrote a
round of the chain (the builder, a cure's `patch_author`, the first bringer
of a tip it reads), the row's sender, a seat outside the approval tier and an
unusable seat. The review door needs one approval-tier read by a NON-AUTHOR,
so another family is not required. Any other row goes to one of
`seat_hold.live_seats`, and a task whose project has an AUTHORED team
(`teams.read`) only to a member of it. Among the candidates the one owing the
fewest open rows and tasks is taken, counting this tick's moves, ties in the
ladder's order. Either way a dark seat or a seat of a dark family is never
the target. The move itself is `dispatch rebind` (the cancel reason records
where the row went, and the successor carries the brief) or the seat-reassign
task capability, and one chat line says it.

WHAT CONFIRMS DARK (task/3881: hysteresis, never a hair trigger, because a
false positive moves live work off a working seat):

  FAMILY      the flag stays RED for GRACE_S. A wall whose known reset (the
              flag's `expires_at`) is within RESET_WAIT_S (an hour) is
              waited out and moves nothing. The clock starts at the family's
              #seats wall episode (`seatevents.opened`, task/3876) when that
              opened before this mover first saw it; an open episode with no
              RED flag moves nothing, so a lost closing edge cannot.
  TURN-WALL   GRACE_S; `turnwall` already reads a refusal that states a
              short reset as no wall.
  NO ROUTE    PANE_GONE_S (half an hour): a reboot or a relaunch clears a
              gone pane in minutes, and moving then costs the seat its work.

AUTOMATIC ON THE TICK (task/3881). The idle-dispatch timer's bare tick moves
work: the canon is that helm does this. local-names `dark-seat-mover` set to
"off" makes the tick report only, and `helm doctor` shows which. --apply and
--dry-run on the verb still decide for a hand-run pass.

EVERYTHING A CONFIRMED-DARK SEAT HOLDS ON THE DISPATCH LEDGER. A CARRIED
predecessor (an open or held row an open successor already carries) is
cancelled with a reason naming its carrier, never moved and never refused
(task/2488's defect: `seat reassign` counts it as a holding and `rebind`
refuses it). A HELD row is released and moved like an open one; a hold the
OWNER gates stays, since that gate is his, and is reported once.

WHERE IT GOES, BY ROLE. local-names `builder-seats` and `reviewer-seats`
declare the seats of each role (`ROLES`). A dark seat listed there hands a
build row or a task only to a live seat of its role; with none live, to the
steward of its dark leg's component (`seatevents.STEWARDS`), and the line
says so. A review still goes only to the reviewer ladder's eligible reader.

ONE #SEATS ROW PER DARK SEAT PER PASS (task/3876): the counts by kind and the
receiving seats, @mentioning the steward. The per-move #helm line is gone.

IT NEVER LOOPS. A row moves at most once per tick, and at most `move_cap()`
moves are made per tick (HELM_DARK_MOVE_MAX, default 5); the rest wait for
the next. Any read that fails is one honest line and no move.

ONE LINE PER SEAT PER DECISION, NEVER PER ROW (task/3881, after LAND 546
posted 137 "keeps its rows" lines in six minutes). A seat that keeps its
rows (DEAF), whose work has nowhere to go (STAYS), whose holds are the
owner's (GATED), or whose started work sits in a dirty room (DIRTY) is one
decision, said once in #seats through `seatevents`
with its steward mentioned, and never in the main room. The decision and
its cause are kept in this mover's state: the same decision on a later pass
says nothing, a changed one says it once, and a seat absent from a pass
keeps its record for HOLD_S so a one-pass flap is not news. A dry or quiet
pass prints its decisions and records none, so it never silences the
applying pass that follows.
"""
import hashlib
import json
import os
import re
import time

from . import home, pk

GRACE_ENV = "HELM_DARK_MOVE_GRACE_S"
GRACE_S = 10 * 60
PANE_ENV = "HELM_DARK_MOVE_PANE_S"
PANE_GONE_S = 30 * 60
RESET_WAIT_ENV = "HELM_DARK_MOVE_RESET_WAIT_S"
RESET_WAIT_S = 60 * 60
#: The local-names key that turns the automatic mover to report only.
SWITCH_KEY = "dark-seat-mover"
#: role -> the local-names key listing that role's seats.
ROLES = {"builder": "builder-seats", "reviewer": "reviewer-seats"}
MOVE_CAP_ENV = "HELM_DARK_MOVE_MAX"
MOVE_CAP = 5
_STATE = "dark_move.json"
#: How long a decision no pass repeats is remembered.
HOLD_S = 6 * 3600


def grace_s():
    """HELM_DARK_MOVE_GRACE_S, a whole number of seconds, else GRACE_S."""
    raw = (os.environ.get(GRACE_ENV) or "").strip()
    return int(raw) if raw.isdigit() else GRACE_S


def _secs(env, default):
    raw = (os.environ.get(env) or "").strip()
    return int(raw) if raw.isdigit() else default


def pane_gone_s():
    """HELM_DARK_MOVE_PANE_S, else PANE_GONE_S: how long a gone pane with no
    armed beacon is dark before its work moves."""
    return _secs(PANE_ENV, PANE_GONE_S)


def reset_wait_s():
    """HELM_DARK_MOVE_RESET_WAIT_S, else RESET_WAIT_S: a wall whose known
    reset is this close is waited out."""
    return _secs(RESET_WAIT_ENV, RESET_WAIT_S)


def automatic():
    """True unless local-names `dark-seat-mover` says "off"."""
    from . import localnames
    return (localnames.value(SWITCH_KEY) or "").strip().lower() != "off"


def switch_state():
    """(on, the sentence `helm doctor` prints)."""
    if automatic():
        return True, ("dark-seat mover: automatic — the idle-dispatch tick "
                      "moves a confirmed-dark seat's work (local-names %r: "
                      "\"off\" makes it report only)" % SWITCH_KEY)
    return False, ("dark-seat mover: OFF (local-names %r is \"off\") — the "
                   "tick only reports, so a dark seat keeps its work until "
                   "someone moves it" % SWITCH_KEY)


def _role_of(seat):
    """(role, {its seats casefolded}) for the first ROLES list naming
    `seat`, else (None, None)."""
    from . import localnames
    want = str(seat or "").casefold()
    for role, key in ROLES.items():
        names = {str(n).casefold() for n in localnames.words(key)}
        if want in names:
            return role, names
    return None, None


def move_cap():
    """HELM_DARK_MOVE_MAX, a whole positive number, else MOVE_CAP."""
    raw = (os.environ.get(MOVE_CAP_ENV) or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else MOVE_CAP


def _state_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", _STATE)


def _read_state():
    state = pk.read_json(_state_path(), {}) or {}
    if not isinstance(state, dict):
        state = {}
    # A DAMAGED ENTRY IS NO SIGHTING: it restarts that family's grace rather
    # than stopping the mover on every tick that follows.
    for key in ("dark_since",):
        value = state.get(key)
        state[key] = {k: v for k, v in value.items()
                      if isinstance(v, (int, float))
                      and not isinstance(v, bool)} \
            if isinstance(value, dict) else {}
    value = state.get("decisions")
    state["decisions"] = {k: v for k, v in value.items()
                          if isinstance(v, dict)
                          and isinstance(v.get("sig"), str)
                          and isinstance(v.get("seen"), (int, float))} \
        if isinstance(value, dict) else {}
    for legacy in ("reported", "reported_dry"):
        state.pop(legacy, None)
    return state


def dark_families(flags):
    """{family: cause} for each family whose flag reads RED on MONEY or
    REACH."""
    from . import burnflags
    out = {}
    for fam, flag in sorted((flags or {}).items()):
        axes = (flag or {}).get("axes") or {}
        if burnflags.RED in (axes.get("money"), axes.get("reach")):
            out[fam] = str((flag or {}).get("cause") or "no cause recorded")
    return out


def _least(names, load):
    """The name owing the least work, ties in the order given."""
    load = load or {}
    return min(enumerate(names),
               key=lambda p: (load.get(p[1].casefold(), 0), p[0]))[1]


def _reviewer(row, dark, eligibility, load=None, known=None):
    """(seat, None) or (None, why) — the measured-eligible approving reader
    of this review, outside a dark family and not itself walled, that owes
    the least. The ladder's chain rung has already refused every seat that
    wrote a round of the lane; no family filter is added here."""
    from . import turnwall
    report, err = eligibility(row["id"])
    if err or report is None:
        return None, "the reviewer ladder did not read (%s)" % (err or "none")
    if report.get("unreadable"):
        return None, "the reviewer ladder is partial (%s unreadable)" % (
            ", ".join(sorted(report["unreadable"])))
    fams = {r.get("seat"): r.get("family") for r in report.get("seats") or ()}
    picks = [name for name in report.get("measured_eligible") or ()
             if fams.get(name) not in dark and name != row.get("recipient")
             and (known is None or fams.get(name) in known)
             and not turnwall.seat_wall(name)]
    if picks:
        return _least(picks, load), None
    return None, "no measured-eligible approving reader outside a dark family"


def _team(row, what="task"):
    """(member names casefolded, or None for no limit; why-unread). A task's
    or a build row's project limits its targets only through a team the
    OWNER authored: `teams.read` says a proposal binds nothing. A task names
    its project; a dispatch row's is its repository's."""
    from . import dispatches, tasks, teams
    if what == "task":
        project = tasks.project_of_row(row)
    else:
        # THE ROW'S REPOSITORY NAMES ITS PROJECT, as a task's lanes are read
        # (`_task_gitdir`): the registry's name, else the repository's token.
        from .work import _lanes
        gitdir = str(row.get("repo_id") or "")
        try:
            project = dispatches._project_of(gitdir) or (_lanes.project_token(
                os.path.dirname(gitdir.rstrip(os.sep))) if gitdir else None)
        except Exception as exc:            # noqa: BLE001 — unread, no move
            return None, "the row's project did not resolve (%s)" % (
                exc.__class__.__name__)
    if not project:
        return None, None
    try:
        rec = teams.read(project)
    except Exception as exc:                # noqa: BLE001 — unread, no move
        return None, "project %s's team did not read (%s)" % (
            project, exc.__class__.__name__)
    if not (rec or {}).get("authored"):
        return None, None
    return {str(m.get("seat") or "").casefold()
            for m in rec.get("members") or () if isinstance(m, dict)}, None


def _target(seat, kind, row, dark, live, eligibility, load=None, what=None,
            known=None, steward=None):
    """(seat, None, note) or (None, why, None). `steward` is the dark
    seat's component steward: `(component, seat or None, why)`."""
    if kind == "review":
        return _reviewer(row, dark, eligibility, load, known) + (None,)
    # A BUILD ROW IS SCOPED LIKE A TASK (CURE2 F3): a helm row never lands on
    # a seat another project's authored team holds.
    team, why = _team(row, what)
    if why:
        return None, why, None
    picks = [name for name, fam in live
             if fam not in dark and name.casefold() != str(seat).casefold()
             and (team is None or name.casefold() in team)]
    role, members = _role_of(seat)
    if role is not None:
        # THE DECLARED ROLE BINDS (task/3881): a live seat of the dark seat's
        # role, else its component's steward, and the line says which.
        mine = [name for name in picks if name.casefold() in members]
        if mine:
            return _least(mine, load), None, None
        component, name, why = steward or (None, None, "no steward read")
        alive = {n.casefold(): n for n, fam in live if fam not in dark}
        if name and name.casefold() in alive \
                and name.casefold() != str(seat).casefold():
            return alive[name.casefold()], None, (
                "no live %s seat: to the %s steward" % (role, component))
        return None, ("no live %s seat, and the %s steward is not a live "
                      "seat (%s)" % (role, component, why or
                                     "@%s is not live" % name)), None
    if picks:
        return _least(picks, load), None, None
    return None, "no live seat outside a dark family%s" % (
        "" if team is None else " on its project's authored team"), None


def _lane_work(gitdir, match, exclude=()):
    """([(lane ref, commits, tip sha)], None) — the lane branches `match`
    names that carry commits on neither trunk nor any of `exclude`; (None,
    why) when git cannot list or count them, which is never "no work"."""
    from . import landreq, vcs
    gitdir = str(gitdir or "")
    root = os.path.dirname(gitdir.rstrip(os.sep)) if gitdir else ""
    if not root:
        return None, "no repository is recorded to read its lanes in"
    try:
        trunk = vcs.backend(root).trunk_ref(root)
    except Exception as exc:                # noqa: BLE001 — unread, no move
        return None, "trunk did not resolve (%s)" % exc.__class__.__name__
    out = landreq._git(gitdir, "for-each-ref",
                       "--format=%(objectname) %(refname:short)",
                       "refs/heads/lane/")
    if out is None or getattr(out, "returncode", 1) != 0:
        return None, "the lane refs did not read"
    found = []
    for line in (out.stdout or "").splitlines():
        sha, _, ref = line.strip().partition(" ")
        if not sha or not ref.startswith("lane/") or not match(ref[5:]):
            continue
        n = landreq._git(gitdir, "rev-list", "--count", sha, "^" + trunk,
                         *["^" + x for x in exclude if x])
        count = (n.stdout or "").strip() if n is not None \
            and getattr(n, "returncode", 1) == 0 else ""
        if not count.isdigit():
            return None, ("%s could not be counted against trunk, so whether "
                          "it carries work is unknown" % ref)
        if int(count):
            found.append((ref, int(count), sha))
    return found, None


def _work_text(found):
    return ", ".join("%s carries %d commit%s not on trunk"
                     % (ref, n, "" if n == 1 else "s") for ref, n, _ in found)


def _fence_text(fence):
    return "; ".join("@%s still holds the room %s%s" % (
        f.get("holder") or "?", f.get("lane") or "?",
        "" if f.get("remaining") is None else " for %ss more"
        % f.get("remaining")) for f in fence or () if isinstance(f, dict))


def _row_started(row, seat):
    """(why, None) when the row's room or lane shows it started, (None, why)
    when that did not read, (None, None) when neither does."""
    from . import dispatches, landreq
    fence = dispatches.rebind_room_fence(row, seat)
    if fence:
        return _fence_text(fence), None
    if row.get("kind") == "review":
        # THE REVIEWED LANE CARRIES THE BUILD'S COMMITS by construction; a
        # reader works in a room of its own, which the fence reads.
        return None, None
    lane = str(row.get("lane") or "").strip()
    if not lane:
        return None, None
    mine = landreq._stem(lane)
    found, why = _lane_work(
        row.get("repo_id"),
        lambda name: landreq._stems_match(mine, landreq._stem(name)),
        exclude=(row.get("tip"), row.get("ref")))
    if why:
        return None, why
    return (_work_text(found) or None), None


def _task_gitdir(row):
    """(this helm's gitdir, None) when the task's lanes live there, else
    (None, why): a task of another project's repository is not read here."""
    from . import dispatches, tasks
    from .work import _lanes
    gitdir, err = dispatches.home_repo_id()
    if err or not gitdir:
        return None, err or "this helm's repository did not resolve"
    project = tasks.project_of_row(row)
    if project:
        root = os.path.dirname(gitdir.rstrip(os.sep))
        names = {_lanes.project_token(root)}
        try:
            names.add(dispatches._project_of(root))
        except Exception:                   # noqa: BLE001 — the token stands
            pass
        if project not in names:
            return None, ("its project %s is not this helm's repository, so "
                          "its lanes are not read here" % project)
    return gitdir, None


def _recorded_lanes(row):
    """The lane stems a task records as its own (`lane:<name>` refs)."""
    from . import landreq
    out = set()
    for ref in (row or {}).get("refs") or ():
        ref = str(ref or "").strip()
        if ref.lower().startswith("lane:") and ref[5:].strip():
            out.add(landreq._stem(ref[5:].strip()))
    return out


def _task_matcher(tid, row):
    """(match, None) or (None, why): `match(text)` says whether a lease
    resource or a lane name is this task's. A name carries the task's number
    anywhere (`task-N`, `<slug>-N`, `<slug>-Nr`) or is a lane the task records
    (`lane:<name>`). Lanes are named `<slug>-N` far more often than `task-N`,
    so a number anywhere in the name counts; a false match reads unstarted
    work as started, and started work moves only out of a clean room."""
    from . import landreq, tasks
    m = tasks._CITED.search(str(tid))
    if not m:
        return None, "its id names no task number"
    num = re.compile(r"(?<![0-9])%d(?![0-9])" % int(m.group(1)))
    recorded = _recorded_lanes(row)

    def names(text):
        text = str(text or "")
        if num.search(text):
            return True
        tail = text.rsplit(":", 1)[-1]
        return bool(recorded) and any(
            landreq._stems_match(want, landreq._stem(tail))
            for want in recorded)
    return names, None


def _task_holding(tid, owner, row, claims):
    """(holding, None) or (None, why) — what a task's owner holds of it: the
    live leases it holds whose resource is the task's (`leases`, [(resource,
    the holder as stored)]), and the task's lane branches in this helm's
    repository carrying commits not on trunk (`lanes`, `_lane_work`'s rows).
    Either one is STARTED work. Any read that fails is why, never "none"."""
    names, why = _task_matcher(tid, row)
    if why:
        return None, why
    if claims is None:
        return None, "the claims ledger did not read"
    leases = []
    for res, claim in sorted(claims.items()):
        holder = str((claim or {}).get("holder") or "") \
            if isinstance(claim, dict) else ""
        if holder.casefold() == str(owner).casefold() and names(res):
            leases.append((res, holder))
    gitdir, why = _task_gitdir(row)
    if why:
        return None, why
    found, why = _lane_work(gitdir, names)
    if why:
        return None, why
    return {"leases": leases, "lanes": found, "gitdir": gitdir,
            "match": names}, None


def _room_dirt(path):
    """([uncommitted paths], None), [] for a clean room, or (None, why). The
    porcelain `work._lanes._room_status` reads, through its own parser:
    tracked changes and untracked files count, files the repository ignores
    do not. A merge, rebase or cherry-pick in progress is uncommitted too."""
    from .work import _lanes
    rc, out, err = _lanes._git_bytes(
        path, "status", "--porcelain=v2", "-z", "--untracked-files=all",
        "--ignore-submodules=none", timeout=5)
    if rc != 0:
        return None, "git status failed: %s" % (
            os.fsdecode(err or b"").strip() or "unknown error")
    try:
        entries = _lanes._status_entries(out)
    except ValueError as exc:
        return None, "an unsafe status record (%s)" % exc
    op = _lanes._operation_in_progress(path)
    if op is None:
        return None, "whether a git operation is in progress did not read"
    dirt = sorted({os.fsdecode(rel) for rel, _sub, _kind in entries})
    if dirt or op:
        return dirt + (["(a %s in progress)" % op] if op else []), None
    # Porcelain is clean after a detached commit, even when no branch reaches
    # that work. Moving its task strands the only copy in this room's HEAD.
    rc, head, err = _lanes._git(path, "rev-parse", "--abbrev-ref", "HEAD",
                                timeout=5)
    if rc != 0 or not head:
        return None, "whether the room HEAD is detached did not read: %s" % (
            err or "unknown error")
    if head == "HEAD":
        rc, lost, err = _lanes._git(path, "rev-list", "--max-count=1", "HEAD",
                                    "--not", "--branches", timeout=5)
        if rc != 0:
            return None, "detached HEAD reach did not read: %s" % (
                err or "unknown error")
        if lost:
            return ["detached HEAD has commits on no branch (%s)" % lost[:12]], None
    return [], None


def _room_lane(root, path, branch):
    """The lane a registered room is on, or "". A REBASE DETACHES HEAD, so
    git's registry names no branch for a room mid-rebase (or one checked out
    at a bare sha): its lane is then the branch the rebase is replaying
    (`rebase-merge/head-name`), else its place in the lane container
    (`_lanes.lane_path`). Reading only the branch skipped exactly the room
    holding the most uncommitted state, and its task moved out from under
    it."""
    from .work import _lanes
    want = "refs/heads/lane/"
    if branch:
        return branch[len(want):] if branch.startswith(want) else ""
    if not path:
        return ""
    rc, gitdir, _err = _lanes._git(path, "rev-parse", "--absolute-git-dir",
                                   timeout=5)
    for sub in ("rebase-merge", "rebase-apply") if rc == 0 and gitdir \
            else ():
        try:
            with open(os.path.join(gitdir, sub, "head-name")) as f:
                head = f.read().strip()
        except (OSError, TypeError, ValueError):
            continue
        if head.startswith(want):
            return head[len(want):]
    box = _lanes.lane_path(root, "")
    real = os.path.realpath(path)
    return os.path.relpath(real, os.path.realpath(box)) \
        if real.startswith(os.path.realpath(box) + os.sep) else ""


def _task_rooms(hold):
    """([(lane, room path, [dirty paths])], None) or (None, why): every
    checked-out room of a lane the task's holding names (its lane branches,
    with or without commits, and the lanes its leases name), read from git's
    own worktree registry. A lane with no room checked out holds nothing
    uncommitted: its work is its branch."""
    from .work import _lanes
    root = os.path.dirname(hold["gitdir"].rstrip(os.sep))
    token = _lanes.project_token(root)
    leased = set()
    for res, _holder in hold["leases"]:
        kind, _, rest = res.partition(":")
        project, _, lane = rest.partition(":")
        if kind == "worktree" and project == token and lane:
            leased.add(lane)
    recs, err = _lanes._worktree_records(root)
    if err or recs is None:
        return None, "the lane rooms did not read (%s)" % (err or "none")
    out = []
    for rec in recs:
        path = (rec or {}).get("path")
        lane = _room_lane(root, path, str((rec or {}).get("branch") or ""))
        if not lane:
            continue
        if not (hold["match"](lane) or lane in leased) or not path \
                or not os.path.isdir(path):
            continue
        dirt, why = _room_dirt(path)
        if why:
            return None, "the room %s did not read (%s)" % (path, why)
        out.append((lane, path, dirt))
    return out, None


def _dirty_text(tid, rooms, cap=8):
    """One decision reason: the task, each dirty room and its paths."""
    from . import seat_hold
    clean = seat_hold._text
    parts = []
    for lane, path, dirt in rooms:
        if dirt:
            shown = ", ".join(dirt[:cap]) + (
                " and %d more" % (len(dirt) - cap) if len(dirt) > cap else "")
            parts.append("%s in %s (lane/%s): %s" % (tid, path, lane, shown))
    return clean("; ".join(parts), 600)


def _family_of(seat_name):
    """The seat's family from its own register, or None (a native seat, or a
    name no register knows): the family `turnwall` reads its transcript
    under, never a second derivation."""
    from . import seat
    family, err = seat._seat_family(str(seat_name or ""))
    return None if err else family


def _row_of(rows, seat_name):
    want = str(seat_name or "").strip().casefold()
    for name, row in (rows or {}).items():
        if str(name).casefold() == want:
            return row or {}
    return {}


def run(apply=None, post=True, now=None, eligibility=None):
    """(lines, moves) — one pass. Never raises: a failed read is one line.
    `apply` None is the automatic tick: it applies unless the kill switch
    (`automatic`) is off."""
    from . import tickalarm
    if apply is None:
        apply = automatic()
    try:
        got = _run(apply, post, time.time() if now is None else now,
                   eligibility)
    except Exception as exc:                # noqa: BLE001 — never a traceback
        # A CATCH IS NOT THE END OF THE STORY (task/4189): this line alone
        # ran 944 passes in three days and nobody heard it.
        return (["dark-seat mover: stopped (%s: %s); nothing more moves this "
                 "tick" % (exc.__class__.__name__, exc)]
                + tickalarm.record("darkmove", tickalarm.error_of(exc)), [])
    tickalarm.record("darkmove")
    return got


class _Pass(object):
    """One tick's reads, taken once and shared by every row it judges."""

    def __init__(self, state, fams, rows, now):
        self.state, self.fams, self.rows, self.now = state, fams, rows, now
        self.seen = {}          # latch key -> first sighting, this tick
        self.bound = {}         # latch key -> the bound it must outlast
        self.relay = {}         # idle_dispatch._relay_reading's memo
        self.memo = {}          # seat -> its judgement
        self.leg = {}           # seat -> the component its darkness is in
        self.known = None       # the families the fresh burn read names
        self.resets = {}        # family -> its flag's known reset instant
        self.opened = {}        # seat-event episode key -> when it opened
        self.stewards = {}      # seat -> `steward`'s answer, read once

    def _confirm(self, key, cause, bound=None, reset=None):
        since = float(self.state["dark_since"].get(key, self.now))
        # THE SEAT EVENT DATES THE DARKNESS it reports (task/3876), and only
        # a darkness this pass read for itself: an episode alone is no leg.
        opened = self.opened.get(key)
        if isinstance(opened, (int, float)) and opened < since:
            since = float(opened)
        self.seen[key] = since
        self.bound[key] = grace_s() if bound is None else bound
        if isinstance(reset, (int, float)) \
                and 0 < reset - self.now <= reset_wait_s():
            return "reset", "%s; it waits for its reset at %s" % (
                cause, time.strftime("%H:%MZ", time.gmtime(reset)))
        if self.now - since >= self.bound[key]:
            return "dark", cause
        return "grace", cause

    def judge(self, seat, rid):
        """("dark"|"grace"|"reset"|"deaf"|None, cause) for the seat that
        owes row `rid` (a task passes None)."""
        from . import burnflags, idle_dispatch, turnwall
        try:
            relay = idle_dispatch._relay_reading(seat, rid, self.now,
                                                 self.relay)
        except Exception:                   # noqa: BLE001 — unread, no move
            return None, None
        if relay is not None:
            return None, None
        key = str(seat).casefold()
        if key in self.memo:
            return self.memo[key]
        fam = _family_of(seat)
        wall = turnwall.seat_wall(seat)
        row = _row_of(self.rows, seat)
        if fam in self.fams:
            self.leg[key] = "local-serving" \
                if fam in burnflags.local_families() else "credentials"
            out = self._confirm("family:%s" % fam, "%s is dark (%s)"
                                % (fam, self.fams[fam]),
                                reset=self.resets.get(fam))
        elif wall:
            self.leg[key] = "credentials"
            out = self._confirm("seat:%s" % key, turnwall.text(wall))

        elif row.get("pane") is False and row.get("reachable") is False:
            self.leg[key] = "project-seats"
            out = self._confirm("seat:%s" % key, (
                "no route: its pane is GONE and no beacon is armed (%s)"
                % (row.get("reachable_why") or "no live beacon")),
                bound=max(grace_s(), pane_gone_s()))
        elif row.get("reachable") is False and row.get("pane"):
            from .beacons import repair_argv
            out = ("deaf", "no armed beacon, but its pane is live: the "
                           "beacons census asks it to re-arm, and `%s` wakes "
                           "it now" % repair_argv(seat))
        else:
            out = (None, None)
        self.memo[key] = out
        return out

    def steward(self, seat):
        """(component, steward seat or None, why, project) for the dark
        seat, read once a pass."""
        from . import seatevents
        key = str(seat).casefold()
        if key not in self.stewards:
            component = self.leg.get(key, "credentials")
            project = None
            if component == "project-seats":
                project = seatevents.project_of([seat]).get(seat)
            name, why = seatevents.steward(component, project)
            self.stewards[key] = (component, name, why, project)
        return self.stewards[key]


def _run(apply, post, now, eligibility):
    from . import burnflags, eventledger, seat_hold
    mode = "apply" if apply else "dry-run"
    lines = []
    flags, _age = burnflags.cached_flags(now=now)
    if not flags:
        # A STALE BURN READING FAILS CLOSED (CURE2 F3): without it no target's
        # family is known to spend, and work is never moved on old evidence.
        return (lines + ["dark-seat mover: no fresh burn snapshot to read, "
                         "so no target's family is known to spend; nothing "
                         "moves this tick"], [])
    try:
        # ONE READ OF THE FLEET PER TICK, through the broken-seat door's own.
        rows = seat_hold.usability_rows()
        live = seat_hold.live_seats("", fleet=rows, fresh=True)
    except Exception as exc:                # noqa: BLE001 — no roster, no move
        return (lines + ["dark-seat mover: the roster did not read (%s), so "
                         "nothing moves" % exc.__class__.__name__], [])
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with eventledger.locked(path) as held:
        if not held:
            return (lines + ["dark-seat mover: its state lock could not be "
                             "taken, so nothing moves this tick"], [])
        state = _read_state()
        judged = _Pass(state, dark_families(flags), rows, now)
        judged.known = set(flags)
        judged.resets = {fam: (flag or {}).get("expires_at")
                         for fam, flag in flags.items()
                         if isinstance(flag, dict)}
        try:
            from . import seatevents
            judged.opened = seatevents.opened()
        except Exception:                   # noqa: BLE001 — no episode read
            judged.opened = {}
        more, moves = _move(judged, live, apply, post, now, eligibility,
                            mode)
        state["dark_since"] = judged.seen
        pk.write_json(path, state)
    lines.extend(more)
    if not more:
        grace = ["%s dark, inside the %ds %s" % (
            k, judged.bound.get(k, grace_s()),
            "pane bound" if judged.bound.get(k, 0) > grace_s() else "grace")
            for k, v in sorted(judged.seen.items())
            if now - float(v) < judged.bound.get(k, grace_s())]
        lines.append("dark-seat mover (%s): %s" % (mode, (
            "%s; nothing moves yet" % "; ".join(grace) if grace else
            "no seat that owes work is dark")))
    return lines, moves


def _held_rows(snap, judged):
    """(candidates, cancels, gated, other) over every OPEN or HELD row:
    for a recipient confirmed dark, [(row, cause, held)] to move,
    [(row, carrier, cause)] to cancel and [(row, cause)] owner-gated holds
    that stay; for any other recipient, [(row, verdict, cause)]."""
    from . import dispatches, query
    index = dispatches._successor_index(snap)
    cycles = dispatches._cycle_components(index)
    rows = sorted((r for r in snap.values() if isinstance(r, dict)
                   and (dispatches._open(r) or query.query_is_held(r))),
                  key=lambda r: (str(r.get("ts") or ""), str(r.get("id"))))
    cands, cancels, gated, other = [], [], [], []
    for row in rows:
        verdict, cause = judged.judge(row.get("recipient"), row["id"])
        if verdict != "dark":
            other.append((row, verdict, cause))
            continue
        carrier = dispatches.carrier(row, snap, index, cycles)
        held = query.query_is_held(row)
        if carrier is not None:
            cancels.append((row, carrier, cause))
        elif held and row.get("owner_gated") is True:
            gated.append((row, cause))
        else:
            cands.append((row, cause, held))
    return cands, cancels, gated, other


def _move(judged, live, apply, post, now, eligibility, mode):
    from . import dispatches, tasks
    state = judged.state
    # EVERY NAME AND CAUSE IS LAUNDERED WHERE IT IS FORMATTED: a roster key
    # or a ledger field can carry control bytes, and these lines reach a
    # terminal, the chat room and a cancel reason.
    from . import seat_hold
    label, clean = seat_hold._label, seat_hold._text
    lines, moves, seen, decided = [], [], set(), {}
    if eligibility is None:
        from . import reviewer_eligibility
        eligibility = reviewer_eligibility.eligibility

    def decide(seat, decision, cause, why=None, noun="dispatch row"):
        """Gather one row into its seat's DECISION. The line is said once
        for the whole seat (`_say_decisions`), never once per row: that
        per-row repeat posted 137 lines in six minutes (LAND 546)."""
        rec = decided.setdefault((str(seat).casefold(), decision), {
            "seat": seat, "decision": decision, "cause": cause, "why": [],
            "count": {}})
        if why and why not in rec["why"]:
            rec["why"].append(why)
        rec["count"][noun] = rec["count"].get(noun, 0) + 1

    def stays(what, rid, seat, why):
        lines.append("dark-seat mover: %s %s stays on @%s — %s"
                     % (what, label(str(rid)[:12]), label(seat), clean(why)))

    # PARKED: a dark seat's started dispatch rows. One moves only beside the
    # started task whose lane it names (`_move_started`), never alone.
    cands, started_work, parked = [], [], []
    snap, unavailable = dispatches.snapshot()
    claims = dispatches.live_claims()
    if unavailable:
        lines.append("dark-seat mover: the dispatch ledger did not read (%s), "
                     "so no row moves" % unavailable)
    elif claims is None:
        lines.append("dark-seat mover: the claims ledger did not read, so "
                     "whether a row has started is unknown and no row moves")
    else:
        rows, cancels, gated, other = _held_rows(snap or {}, judged)
        for row, verdict, cause in other:
            seat = row.get("recipient")
            if verdict == "deaf":
                decide(seat, "deaf", cause)
            elif verdict == "reset":
                stays("dispatch", row["id"], seat, cause)
        for row, carrier, cause in cancels:
            # A CARRIED PREDECESSOR IS CANCELLED, NEVER MOVED (task/2488's
            # defect): its successor already holds the work, so a rebind
            # would refuse and a move would fork it.
            seat = row.get("recipient")
            entry = {"what": "carried", "id": row["id"], "from": seat,
                     "source": seat, "to": None, "cause": cause,
                     "moved": False,
                     "carrier": carrier.get("id")}
            moves.append(entry)
            reason = ("dark-seat mover: @%s is dark (%s); carried by %s, so "
                      "this predecessor is cancelled, never moved"
                      % (label(seat), clean(cause, 100),
                         label(str(carrier.get("id"))[:12])))
            if not apply:
                lines.append("dark-seat mover (%s): would cancel carried "
                             "dispatch %s on @%s — carried by %s"
                             % (mode, label(row["id"][:12]), label(seat),
                                label(str(carrier.get("id"))[:12])))
                continue
            _row, err = dispatches.mark_cancel(row["id"], reason)
            if err:
                stays("carried dispatch", row["id"], seat,
                      "the cancel was refused: %s" % err)
                continue
            entry["moved"] = True
            lines.append(reason.replace("this predecessor", "dispatch %s"
                                        % label(row["id"][:12])))
        for row, cause in gated:
            decide(row.get("recipient"), "gated", cause,
                   why=clean(row.get("hold_reason") or "no reason", 120))
        for row, cause, held in rows:
            seat = row.get("recipient")
            started, why = dispatches.progress_state(row, live=claims)
            if started != dispatches.IDLE:
                if started == dispatches.WORKING:
                    parked.append((row, held, why))
                else:
                    stays("dispatch", row["id"], seat, why)
                continue
            started, unread = _row_started(row, seat)
            if started or unread:
                if started:
                    parked.append((row, held, started))
                else:
                    stays("dispatch", row["id"], seat,
                          "whether it started did not read (%s)" % unread)
                continue
            cands.append(("dispatch", row["id"], seat, cause,
                          row.get("kind"), row, held))
    trows, tunread = tasks.snapshot(strict=True)
    if tunread:
        lines.append("dark-seat mover: the task ledger did not read (%s), so "
                     "no task moves" % tunread)
    else:
        for tid, row in sorted((trows or {}).items()):
            owner = tasks.owner_of(row) if isinstance(row, dict) else ""
            if not owner or row.get("status") not in tasks.OPEN_STATUSES:
                continue
            verdict, cause = judged.judge(owner, None)
            if verdict == "reset":
                stays("task", tid, owner, cause)
            if verdict != "dark":
                continue
            hold, unread = _task_holding(tid, owner, row, claims)
            if unread:
                stays("task", tid, owner,
                      "whether it started did not read (%s)" % unread)
                continue
            if row.get("status") == "open" and not hold["leases"] \
                    and not hold["lanes"]:
                cands.append(("task", tid, owner, cause, "build", row, False))
                continue
            # STARTED WORK (task/3881's remainder): an in_progress task, a
            # lease or lane commits. It moves only out of a CLEAN room; a
            # dirty one is one ACT row to the integrator, and stays.
            rooms, unread = _task_rooms(hold)
            if unread:
                stays("started task", tid, owner, unread)
                continue
            if any(dirt for _lane, _path, dirt in rooms):
                decide(owner, "dirty", cause, why=_dirty_text(tid, rooms),
                       noun="started task")
                continue
            started_work.append((tasks._rank(row), tid, owner, cause, row,
                                 (hold, rooms)))
    # P0 FIRST, THEN P1, BY THE TASK'S PRIORITY: started work is the most
    # stranded, so it is taken before the unstarted rows under the cap.
    cands[:0] = [("started", tid, owner, cause, "build", row, held)
                 for _rank, tid, owner, cause, row, held
                 in sorted(started_work, key=lambda c: (c[0], c[1]))]
    dark = set(judged.fams)
    load = _load(snap if not unavailable else {},
                 trows if not tunread else {})
    cap, waiting = move_cap(), 0
    for what, rid, seat, cause, kind, row, held in cands:
        if rid in seen:
            continue
        seen.add(rid)
        if len(moves) - sum(1 for m in moves if m["what"] == "carried") \
                >= cap:
            waiting += 1
            continue
        started = what == "started"
        what = "task" if started else what
        to, why, note = _target(seat, kind, row, dark, live, eligibility,
                                load, what, judged.known,
                                judged.steward(seat)[:3])
        if not to:
            decide(seat, "stays", cause, why=clean(why),
                   noun="started task" if started else
                   "task" if what == "task" else "dispatch row")
            continue
        reason = "dark-seat mover: @%s — %s" % (label(seat), clean(cause, 160))
        entry = {"what": what, "id": rid, "from": seat, "source": seat,
                 "to": to, "cause": cause, "moved": False, "kind": kind,
                 "released": bool(held) and not started}
        load[to.casefold()] = load.get(to.casefold(), 0) + 1
        noted = " (%s)" % note if note else ""
        if started:
            entry["started"] = True
            _move_started(entry, row, held, parked, apply, reason, lines,
                          mode, noted)
            moves.append(entry)
            continue
        if not apply:
            lines.append("dark-seat mover (%s): would %smove %s %s @%s -> @%s "
                         "(%s)%s" % (mode, "release and " if held else "",
                                     what, label(rid[:12]), label(seat),
                                     label(to), clean(cause, 160), noted))
            moves.append(entry)
            continue
        fence, err = None, None
        if what == "dispatch":
            if held:
                # A HOLD ON A DARK SEAT IS NEVER KEPT FOREVER (task/3881):
                # released, then moved like any open row. The release records
                # the MOVER as its hand (task/4149): a deliberate release must
                # never read as an unattributed one, and the mover is a fixed
                # hand, not a seat, so it names itself rather than the seat
                # the process happens to run under.
                _row, err = dispatches.mark_release(rid, actor=SWITCH_KEY)
            if not err:
                err, fence = _rebind(rid, to, reason)
        else:
            err = _retask(rid, row, seat, to, reason)
        if err:
            lines.append("dark-seat mover: %s %s stays on @%s — the move to "
                         "@%s was refused: %s" % (what, label(rid[:12]),
                                                  label(seat), label(to),
                                                  clean(err)))
            moves.append(entry)
            continue
        entry["moved"] = True
        moves.append(entry)
        text = ("dark-seat mover: moved %s %s from @%s to @%s — %s%s"
                % (what, label(rid[:12]), label(seat), label(to), reason,
                   noted))
        if fence:
            # THE ROOM DID NOT MOVE WITH THE ROW (#203): the new seat is told,
            # here and in the #seats row.
            entry["note"] = ("NOTE %s, so @%s starts in a fresh room (`helm "
                             "work claim <lane>-r2`)"
                             % (clean(_fence_text(fence)), label(to)))
            text += "; " + entry["note"]
        lines.append(text)
    # A started dispatch is only parked while its task is judged. Saying
    # "stays" before that decision contradicted the same pass's carried move.
    # Refused mate rebinds already have their own precise stays line.
    accounted = {rid for m in moves if m.get("started")
                 for rid in m.get("accounted_rows", ())}
    for row, _held, why in parked:
        if row["id"] not in accounted:
            stays("dispatch", row["id"], row.get("recipient"), why)
    if waiting:
        lines.append("dark-seat mover: %d more wait for the next tick (at "
                     "most %d moves a tick, %s)" % (waiting, cap,
                                                    MOVE_CAP_ENV))
    if apply and post:
        note = _seat_rows(judged, moves, now)
        if note:
            lines.append("dark-seat mover: %s" % note)
    _say_decisions(judged, decided, apply and post, now, lines)
    return lines, moves


def _started_lanes(hold, rooms):
    """[(lane, tip sha or None, commits off trunk, room path or None)] for
    every lane a started task's holding names, in name order."""
    room = {lane: path for lane, path, _dirt in rooms}
    out = {ref[5:]: (sha, n) for ref, n, sha in hold["lanes"]}
    for res, _holder in hold["leases"]:
        if res.startswith("worktree:") and res.count(":") >= 2:
            out.setdefault(res.split(":", 2)[2], (None, 0))
    for lane in room:
        out.setdefault(lane, (None, 0))
    return [(lane, sha, n, room.get(lane))
            for lane, (sha, n) in sorted(out.items())]


def _handoff_text(entry, row, lanes):
    """The ONE addressed line the receiving seat gets: the task, each lane,
    its room and tip, and what to do with the commits it finds there."""
    from . import seat_hold
    label, clean = seat_hold._label, seat_hold._text
    where = []
    for lane, sha, n, room in lanes:
        where.append("lane/%s: room %s, tip %s%s" % (
            clean(lane, 120),
            clean(room, 240) if room else "not checked out (`helm work "
            "claim %s` opens it)" % clean(lane, 120),
            sha or "(no commit off trunk yet)",
            " (%d commit%s off trunk)" % (n, "" if n == 1 else "s")
            if n else ""))
    leases = entry.get("leases") or []
    return ("dark-seat mover: %s (%s, %s) moved to you from @%s, who is dark "
            "(%s). %s — continue from this tip: commits on the lane are the "
            "dark seat's finished work; review and land them, then finish the "
            "task.%s%s" % (
                label(entry["id"]), row.get("priority") or "unranked",
                clean(row.get("title") or "untitled", 120),
                label(entry["source"]), clean(entry["cause"], 160),
                "; ".join(where) or "no lane carries its work yet",
                " The lease%s %s %s yours now; `helm chat claims` shows the "
                "token." % ("" if len(leases) == 1 else "s",
                            ", ".join(clean(r, 160) for r in leases),
                            "is" if len(leases) == 1 else "are")
                if leases else "",
                " Its dispatch row%s %s moved with it." % (
                    "" if len(entry["rows"]) == 1 else "s",
                    ", ".join(label(r[:12]) for r in entry["rows"]))
                if entry.get("rows") else ""))


def _move_started(entry, row, held, parked, apply, reason, lines, mode,
                  noted):
    """Move one STARTED task off a confirmed-dark seat whose rooms are
    clean (task/3881's remainder), through the primitives `seat reassign`
    uses: the task's leases only (`rebind_claim_holder(only=)`), then its
    owner (`_retask`, the seat-reassign capability, whose LIVE veto stands),
    then the dark seat's started dispatch rows on the same lanes (`dispatch
    rebind`). A refused lease or owner move rolls back the leases already
    moved, and the task stays. The receiving seat gets one DM saying where
    to continue."""
    from . import dispatches, landreq, seat_hold, seats, seats_claims
    label, clean = seat_hold._label, seat_hold._text
    hold, rooms = held
    tid, seat, to = entry["id"], entry["source"], entry["to"]
    lanes = _started_lanes(hold, rooms)
    stems = [landreq._stem(lane) for lane, _sha, _n, _room in lanes]
    mates = [(r, h) for r, h, _why in parked
             if str(r.get("recipient") or "").casefold() == seat.casefold()
             and r.get("kind") != "review" and any(
                 landreq._stems_match(landreq._stem(str(r.get("lane") or "")),
                                      stem) for stem in stems)]
    entry.update(lanes=["lane/" + lane for lane, _s, _n, _r in lanes],
                 leases=[res for res, _holder in hold["leases"]], rows=[],
                 accounted_rows=[])
    with_text = ", ".join(
        ["lane/%s%s" % (lane, " at %s" % sha[:12] if sha else "")
         for lane, sha, _n, _r in lanes]
        + ["lease %s" % res for res in entry["leases"]]
        + ["dispatch %s" % r["id"][:12] for r, _h in mates])
    with_text = " with " + clean(with_text, 400) if with_text else ""
    if not apply:
        entry["accounted_rows"] = [r["id"] for r, _h in mates]
        lines.append("dark-seat mover (%s): would move started task %s @%s "
                     "-> @%s (%s)%s%s" % (mode, label(tid[:12]), label(seat),
                                         label(to), clean(entry["cause"], 160),
                                         with_text, noted))
        return
    err, done = None, []
    by_holder = {}
    for res, holder in hold["leases"]:
        by_holder.setdefault(holder, []).append(res)
    for holder, resources in sorted(by_holder.items()):
        ok, msg, manifest = seats_claims.rebind_claim_holder(
            holder, to, only=resources)
        if not ok:
            err = "its lease did not move: %s" % msg
            break
        done.append((holder, manifest))
    if not err:
        err = _retask(tid, row, seat, to, reason)
    if err:
        # A FAILED ROLLBACK IS SAID, NEVER DROPPED: the task stays on the
        # dark seat while the lease sits on the target, and only this line
        # and the move's record can tell anyone the two disagree.
        for holder, manifest in done:
            if not manifest:
                continue
            ok, msg, back = seats_claims.rollback_claim_holder(
                holder, to, manifest)
            restored = {(m.get("resource"), m.get("lease")) for m in back
                        if isinstance(m, dict)}
            missing = [m for m in manifest
                       if (m.get("resource"), m.get("lease")) not in restored]
            if missing:
                entry.setdefault("rollback", []).append(
                    "lease %s stays on @%s, not back on @%s: %s" % (
                        ", ".join(clean(m.get("resource") or "?", 160)
                                  for m in missing),
                        label(to), label(holder), clean(msg, 160)))
            elif not ok:
                entry.setdefault("rollback", []).append(
                    "rollback refused after all recorded leases returned to "
                    "@%s: %s" % (label(holder), clean(msg, 160)))
        lines.append("dark-seat mover: started task %s stays on @%s — the "
                     "move to @%s was refused: %s%s" % (
                         label(tid[:12]), label(seat), label(to), clean(err),
                         "; ROLLBACK FAILED — %s; the task's owner and its "
                         "lease now disagree, so move the lease back by hand"
                         % "; ".join(entry["rollback"])
                         if entry.get("rollback") else ""))
        return
    entry["moved"] = True
    entry["accounted_rows"] = [r["id"] for r, _h in mates]
    kept = []
    for mate, was_held in mates:
        why = None
        if was_held:
            _row, why = dispatches.mark_release(mate["id"])
        if not why:
            why, _fence = _rebind(mate["id"], to, reason)
        if why:
            kept.append("dispatch %s stays on @%s (%s)" % (
                label(mate["id"][:12]), label(seat), clean(why, 160)))
        else:
            entry["rows"].append(mate["id"])
    lines.append("dark-seat mover: moved started task %s from @%s to @%s — "
                 "%s%s%s" % (label(tid[:12]), label(seat), label(to), reason,
                             with_text, noted))
    lines.extend("dark-seat mover: " + k for k in kept)
    try:
        _sent, why = seats.dm(to, _handoff_text(entry, row, lanes),
                              who="idle-dispatch")
    except Exception as exc:                # noqa: BLE001 — said on the line
        why = exc.__class__.__name__
    if why:
        lines.append("dark-seat mover: the hand-off line to @%s did not send "
                     "(%s); the #seats row names the move" % (
                         label(to), clean(str(why), 160)))


def _decision_text(rec):
    from . import seat_hold
    label, clean = seat_hold._label, seat_hold._text
    seat, counted = label(rec["seat"]), _counted(rec["count"])
    if rec["decision"] == "deaf":
        return "dark-seat mover: @%s keeps its rows (%s) — %s" % (
            seat, counted, clean(rec["cause"]))
    if rec["decision"] == "dirty":
        return ("dark-seat mover: %s stay%s on @%s, which is dark (%s) — "
                "uncommitted changes in the room, so nothing moves: %s. "
                "Commit them on the lane or move them by hand; the next pass "
                "then moves the work" % (
                    counted, "s" if sum(rec["count"].values()) == 1 else "",
                    seat, clean(rec["cause"], 160), "; ".join(rec["why"])))
    if rec["decision"] == "gated":
        return ("dark-seat mover: %s: work stays on @%s — each hold is "
                "owner-gated (%s), and that gate is the owner's"
                % (counted, seat, "; ".join(rec["why"])))
    return "dark-seat mover: %s: work stays on @%s (%s) — %s" % (
        counted, seat, clean(rec["cause"], 160), "; ".join(rec["why"]))


def _decision_steward(judged, rec):
    """(component, project) for a decision's row: a deaf seat's is local
    serving for a local family, else its project's lead; a dark seat's is
    the component of the leg it went dark in. A dirty room's is the
    integrator's (`build-lanes`): the lane's commits are its to land."""
    from . import burnflags, seatevents
    if rec["decision"] == "dirty":
        return "build-lanes", None
    if rec["decision"] != "deaf":
        component, _name, _why, project = judged.steward(rec["seat"])
        return component, project
    if _family_of(rec["seat"]) in burnflags.local_families():
        return "local-serving", None
    return "project-seats", seatevents.project_of([rec["seat"]]).get(
        rec["seat"])


def _say_decisions(judged, decided, says, now, lines):
    """Say each seat's decision once. `says` False (a dry or quiet pass)
    prints every decision and records none."""
    from . import seatevents
    known = judged.state.setdefault("decisions", {})
    events = []
    for (key, decision), rec in sorted(decided.items()):
        text = _decision_text(rec)
        if not says:
            lines.append(text)
            continue
        # THE SIGNATURE IS THE DECISION AND ITS CAUSE, NEVER ITS COUNT: a
        # seat whose held rows grow by one keeps the same decision.
        sig = hashlib.sha256(json.dumps(
            [decision, rec["cause"] if decision == "deaf"
             else sorted(rec["why"])], sort_keys=True).encode(
                 "utf-8")).hexdigest()[:16]
        slot = "%s|%s" % (key, decision)
        if (known.get(slot) or {}).get("sig") == sig:
            known[slot]["seen"] = now
            continue
        known[slot] = {"sig": sig, "seen": now}
        lines.append(text)
        component, project = _decision_steward(judged, rec)
        events.append(seatevents.event(
            component, "dark-seat:%s" % slot, "decision",
            "%s|%.3f" % (sig, now), text, project=project))
    for slot in list(known):
        if now - known[slot]["seen"] > HOLD_S:
            known.pop(slot)
    if events:
        try:
            seatevents.announce(events, now=now)
        except Exception as exc:            # noqa: BLE001 — said on the line
            lines.append("dark-seat mover: the #seats row did not post (%s)"
                         % exc.__class__.__name__)


def _counted(counts):
    """{noun: n} -> "2 build rows and 1 task", in first-seen order."""
    parts = ["%d %s%s" % (n, noun, "" if n == 1 else "s")
             for noun, n in counts.items()]
    return parts[0] if len(parts) == 1 else \
        ", ".join(parts[:-1]) + " and " + parts[-1]


def _seat_rows(judged, moves, now):
    """ONE #seats row per dark seat this pass moved work off (task/3876,
    task/3881): the counts by kind, the receiving seats and the cancels,
    @mentioning the steward of its dark leg. -> None, or why it did not
    post."""
    from . import seat_hold, seatevents
    label = seat_hold._label
    done = {}
    for m in moves:
        if m["moved"]:
            done.setdefault(str(m["source"]), []).append(m)
    events = []
    for seat, ms in sorted(done.items()):
        by_to = {}
        for m in ms:
            if m["what"] == "carried":
                continue
            noun = "started task" if m.get("started") else "task" \
                if m["what"] == "task" else "%s row" % (
                    m.get("kind") or "dispatch")
            counts = by_to.setdefault(m["to"], {})
            counts[noun] = counts.get(noun, 0) + 1
        parts = ["moved %s to @%s" % (_counted(c), label(to))
                 for to, c in by_to.items()]
        cancelled = sum(1 for m in ms if m["what"] == "carried")
        released = sum(1 for m in ms if m.get("released"))
        if cancelled:
            parts.append("cancelled %d carried predecessor%s, each naming "
                         "its carrier" % (cancelled,
                                          "" if cancelled == 1 else "s"))
        if released:
            parts.append("%d of them released from a hold" % released)
        parts.extend(m["note"] for m in ms if m.get("note"))
        component, _name, _why, project = judged.steward(seat)
        text = "dark-seat mover: @%s is dark (%s): %s." % (
            label(seat), seat_hold._text(ms[0].get("cause") or "?", 120),
            "; ".join(parts))
        events.append(seatevents.event(
            component, "dark-move:%s" % seat, "moved", "%.3f" % now, text,
            project=project))
    if not events:
        return None
    try:
        got = seatevents.announce(events, now=now)
    except Exception as exc:                # noqa: BLE001 — said on the line
        return "the #seats row did not post (%s)" % exc.__class__.__name__
    return None if got.get("posted") or not events else \
        "the #seats row is owed and retried on the next seat event"


def _load(snap, trows):
    """{seat casefolded: open rows it owes + open or in-progress tasks it
    owns} — what balancing counts."""
    from . import dispatches, tasks
    load = {}
    for row in dispatches.open_rows(snap or {}):
        key = str(row.get("recipient") or "").casefold()
        load[key] = load.get(key, 0) + 1
    for row in (trows or {}).values():
        if isinstance(row, dict) and row.get("status") in ("open",
                                                          "in_progress"):
            key = tasks.owner_of(row).casefold()
            if key:
                load[key] = load.get(key, 0) + 1
    return load


def _rebind(rid, to, reason):
    """(None, fence) once `dispatch rebind` moved the row, else (refusal,
    None). The evidence gate is passed by `force` with the measured reason,
    as `seat reassign` passes it; every other rebind refusal still fires.
    The fence is the rooms the old seat still holds, which a rebind moves
    no more than the CLI's own rebind does."""
    from . import dispatches
    res, err = dispatches.rebind(rid, to, reason=reason, force=True)
    if res and not err:
        return None, res.get("room_fence") or []
    return (err or "rebind returned nothing"), None


def _retask(tid, row, seat, to, reason):
    """None once the task's owner moved, else the refusal. The seat-reassign
    capability, minted per task. A measurably LIVE owner refuses it: the
    move is reported and the task stays. A seat whose pane process runs but
    whose liveness reads WALLED or BLOCKED_ON_QUOTA, its reset past
    RESET_WAIT_S, is not live for work (`seat_reassign.source_disposition`),
    and the record carries that classification and its reset."""
    from . import takeover, tasks
    disp, err = takeover.mint_source_disposition(seat)
    if err:
        return err
    # NO FORCE (CURE2 F2): a measurably LIVE owner is the veto the
    # capability exists to keep, and a dark reading never overrides it.
    record = {"kind": "dark-seat-move", "from": seat, "to": to,
              "reason": reason,
              "source": {"state": disp.state, "why": disp.why}}
    auth, err = takeover.mint_seat_reassign(
        tid, row, seat, to, record, force=False, disposition=disp)
    if auth is None:
        return err
    _row, err = tasks.update(tid, takeover_auth=auth, owner=to)
    return err
