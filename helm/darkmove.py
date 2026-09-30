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

WHAT MOVES, and what "unstarted" means here, from fields that already exist:

  a dispatch row  OPEN (not held, not answered: `dispatches.open_rows`) and
                  its recipient NOT visibly working it: `progress_state` is
                  IDLE, i.e. the recipient holds neither a claim on the row
                  nor the dispatched lane's lease; the recipient holds no
                  room of the lane's family (`rebind_room_fence`, the rooms a
                  rebind would leave fenced); and, for any row but a review,
                  no lane branch of that family carries commits that are on
                  neither trunk nor the dispatched ref. WORKING never moves,
                  since its room may hold uncommitted work.
  a task          `open` with an owner (`helm task claim` sets `in_progress`,
                  so an owned `open` row was assigned and never claimed), the
                  owner holding no live lease that names the task (`task-N`
                  in the lease), and no `lane/…task-N…` branch of this helm's
                  repository carrying commits not on trunk.

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

IT NEVER LOOPS. A row moves at most once per tick, and at most `move_cap()`
moves are made per tick (HELM_DARK_MOVE_MAX, default 5); the rest wait for
the next. A row with no live eligible seat stays where it is and is reported
ONCE (latched until it moves or its seat comes back), latched apart for a
dry tick and an `--apply` tick, so a dry tick never silences the real
report. Any read that fails is one honest line and no move. DRY-RUN BY
DEFAULT: `--apply` writes.
"""
import os
import re
import time

from . import home, pk

GRACE_ENV = "HELM_DARK_MOVE_GRACE_S"
GRACE_S = 10 * 60
MOVE_CAP_ENV = "HELM_DARK_MOVE_MAX"
MOVE_CAP = 5
_STATE = "dark_move.json"
WHO = "idle-dispatch"
ROOM = "helm"


def grace_s():
    """HELM_DARK_MOVE_GRACE_S, a whole number of seconds, else GRACE_S."""
    raw = (os.environ.get(GRACE_ENV) or "").strip()
    return int(raw) if raw.isdigit() else GRACE_S


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
    for key in ("dark_since", "reported", "reported_dry"):
        value = state.get(key)
        state[key] = {k: v for k, v in value.items()
                      if isinstance(v, (int, float))
                      and not isinstance(v, bool)} \
            if isinstance(value, dict) else {}
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
            known=None):
    if kind == "review":
        return _reviewer(row, dark, eligibility, load, known)
    # A BUILD ROW IS SCOPED LIKE A TASK (CURE2 F3): a helm row never lands on
    # a seat another project's authored team holds.
    team, why = _team(row, what)
    if why:
        return None, why
    picks = [name for name, fam in live
             if fam not in dark and name.casefold() != str(seat).casefold()
             and (team is None or name.casefold() in team)]
    if picks:
        return _least(picks, load), None
    return None, "no live seat outside a dark family%s" % (
        "" if team is None else " on its project's authored team")


def _lane_work(gitdir, match, exclude=()):
    """([(lane ref, commits)], None) — the lane branches `match` names that
    carry commits on neither trunk nor any of `exclude`; (None, why) when
    git cannot list or count them, which is never "no work"."""
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
            found.append((ref, int(count)))
    return found, None


def _work_text(found):
    return ", ".join("%s carries %d commit%s not on trunk"
                     % (ref, n, "" if n == 1 else "s") for ref, n in found)


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


def _task_started(tid, owner, row, claims):
    """As `_row_started`, for a task: a live lease of its owner's, or a
    lane branch with commits not on trunk, whose name carries the task's
    number anywhere (`task-N`, `<slug>-N`, `<slug>-Nr`) or is a lane the task
    records (`lane:<name>`). Lanes are named `<slug>-N` far more often than
    `task-N`, so a number anywhere in the name counts; a false match keeps
    work where it is, which is the safe way to be wrong."""
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
    if claims is None:
        return None, "the claims ledger did not read"
    for res, claim in sorted(claims.items()):
        holder = str((claim or {}).get("holder") or "")
        if holder.casefold() == str(owner).casefold() and names(res):
            return "@%s holds the live lease %s" % (holder, res), None
    gitdir, why = _task_gitdir(row)
    if why:
        return None, why
    found, why = _lane_work(gitdir, names)
    if why:
        return None, why
    return (_work_text(found) or None), None


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


def _post(text):
    try:
        from . import chat
        chat.post(text, room=ROOM, who=WHO, sign=False)
        return None
    except Exception as exc:                # noqa: BLE001 — said on the line
        return "the chat line did not post (%s)" % exc.__class__.__name__


def run(apply=False, post=True, now=None, eligibility=None):
    """(lines, moves) — one pass. Never raises: a failed read is one line."""
    try:
        return _run(apply, post, time.time() if now is None else now,
                    eligibility)
    except Exception as exc:                # noqa: BLE001 — never a traceback
        return (["dark-seat mover: stopped (%s: %s); nothing more moves this "
                 "tick" % (exc.__class__.__name__, exc)], [])


class _Pass(object):
    """One tick's reads, taken once and shared by every row it judges."""

    def __init__(self, state, fams, rows, now):
        self.state, self.fams, self.rows, self.now = state, fams, rows, now
        self.seen = {}          # latch key -> first sighting, this tick
        self.relay = {}         # idle_dispatch._relay_reading's memo
        self.memo = {}          # seat -> its judgement
        self.known = None       # the families the fresh burn read names

    def _confirm(self, key, cause):
        since = self.state["dark_since"].get(key, self.now)
        self.seen[key] = since
        if self.now - float(since) >= grace_s():
            return "dark", cause
        return "grace", cause

    def judge(self, seat, rid):
        """("dark"|"grace"|"deaf"|None, cause) for the seat that owes row
        `rid` (a task passes None)."""
        from . import idle_dispatch, turnwall
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
            out = self._confirm("family:%s" % fam, "%s is dark (%s)"
                                % (fam, self.fams[fam]))
        elif wall:
            out = self._confirm("seat:%s" % key, turnwall.text(wall))
        elif row.get("pane") is False and row.get("reachable") is False:
            out = self._confirm("seat:%s" % key, (
                "no route: its pane is GONE and no beacon is armed (%s)"
                % (row.get("reachable_why") or "no live beacon")))
        elif row.get("reachable") is False and row.get("pane"):
            from .beacons import repair_argv
            out = ("deaf", "no armed beacon, but its pane is live: the "
                           "beacons census asks it to re-arm, and `%s` wakes "
                           "it now" % repair_argv(seat))
        else:
            out = (None, None)
        self.memo[key] = out
        return out


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
        more, moves = _move(judged, live, apply, post, now, eligibility,
                            mode)
        state["dark_since"] = judged.seen
        pk.write_json(path, state)
    lines.extend(more)
    if not more:
        grace = sorted(k for k, v in judged.seen.items()
                       if now - float(v) < grace_s())
        lines.append("dark-seat mover (%s): %s" % (mode, (
            "%s dark, inside the %ds grace; nothing moves yet"
            % (", ".join(grace), grace_s()) if grace else
            "no seat that owes unstarted work is dark")))
    return lines, moves


def _move(judged, live, apply, post, now, eligibility, mode):
    from . import dispatches, seat_hold, tasks
    state = judged.state
    # EVERY NAME AND CAUSE IS LAUNDERED WHERE IT IS FORMATTED: a roster key
    # or a ledger field can carry control bytes, and these lines reach a
    # terminal, the chat room and a cancel reason.
    label, clean = seat_hold._label, seat_hold._text
    lines, moves, seen, reported = [], [], set(), {}
    # A DRY TICK LATCHES APART: the shipped timer ticks dry, and a notice it
    # latched must still reach the room on the first --apply tick.
    latch = "reported" if apply else "reported_dry"
    if eligibility is None:
        from . import reviewer_eligibility
        eligibility = reviewer_eligibility.eligibility

    def once(key, text):
        """Report `text` the first tick `key` is seen, and hold the latch."""
        reported[key] = state[latch].get(key, now)
        if key in state[latch]:
            return
        lines.append(text)
        if apply and post:
            _post(text)

    def stays(what, rid, seat, why):
        lines.append("dark-seat mover: %s %s stays on @%s — %s"
                     % (what, label(str(rid)[:12]), label(seat), clean(why)))

    cands = []
    snap, unavailable = dispatches.snapshot()
    claims = dispatches.live_claims()
    if unavailable:
        lines.append("dark-seat mover: the dispatch ledger did not read (%s), "
                     "so no row moves" % unavailable)
    elif claims is None:
        lines.append("dark-seat mover: the claims ledger did not read, so "
                     "whether a row has started is unknown and no row moves")
    else:
        for row in dispatches.open_rows(snap):
            seat = row.get("recipient")
            verdict, cause = judged.judge(seat, row["id"])
            if verdict == "deaf":
                once("deaf:%s" % str(seat).casefold(),
                     "dark-seat mover: @%s keeps its rows — %s"
                     % (label(seat), clean(cause)))
            if verdict != "dark":
                continue
            started, why = dispatches.progress_state(row, live=claims)
            if started != dispatches.IDLE:
                stays("dispatch", row["id"], seat, why)
                continue
            started, unread = _row_started(row, seat)
            if started or unread:
                stays("dispatch", row["id"], seat, started or (
                    "whether it started did not read (%s)" % unread))
                continue
            cands.append(("dispatch", row["id"], seat, cause,
                          row.get("kind"), row))
    trows, tunread = tasks.snapshot(strict=True)
    if tunread:
        lines.append("dark-seat mover: the task ledger did not read (%s), so "
                     "no task moves" % tunread)
    else:
        for tid, row in sorted((trows or {}).items()):
            owner = tasks.owner_of(row) if isinstance(row, dict) else ""
            if not owner or row.get("status") != "open":
                continue
            verdict, cause = judged.judge(owner, None)
            if verdict != "dark":
                continue
            started, unread = _task_started(tid, owner, row, claims)
            if started or unread:
                stays("task", tid, owner, started or (
                    "whether it started did not read (%s)" % unread))
                continue
            cands.append(("task", tid, owner, cause, "build", row))
    dark = set(judged.fams)
    load = _load(snap if not unavailable else {},
                 trows if not tunread else {})
    cap, waiting = move_cap(), 0
    for what, rid, seat, cause, kind, row in cands:
        if rid in seen:
            continue
        seen.add(rid)
        if len(moves) >= cap:
            waiting += 1
            continue
        to, why = _target(seat, kind, row, dark, live, eligibility, load,
                          what, judged.known)
        if not to:
            once("%s:%s" % (what, rid),
                 "dark-seat mover: %s %s stays on @%s (%s) — %s"
                 % (what, label(rid[:12]), label(seat), clean(cause, 160),
                    clean(why)))
            continue
        reason = "dark-seat mover: @%s — %s" % (label(seat), clean(cause, 160))
        entry = {"what": what, "id": rid, "from": seat, "to": to,
                 "cause": cause, "moved": False}
        load[to.casefold()] = load.get(to.casefold(), 0) + 1
        if not apply:
            lines.append("dark-seat mover (%s): would move %s %s @%s -> @%s "
                         "(%s)" % (mode, what, label(rid[:12]), label(seat),
                                   label(to), clean(cause, 160)))
            moves.append(entry)
            continue
        fence = None
        if what == "dispatch":
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
        text = ("dark-seat mover: moved %s %s from @%s to @%s — %s"
                % (what, label(rid[:12]), label(seat), label(to), reason))
        if fence:
            # THE ROOM DID NOT MOVE WITH THE ROW (#203): the new seat is told.
            text += ("; NOTE %s, so @%s starts in a fresh room (`helm work "
                     "claim <lane>-r2`)" % (clean(_fence_text(fence)),
                                            label(to)))
        lines.append(text)
        if post:
            note = _post(text)
            if note:
                lines.append("dark-seat mover: %s" % note)
    if waiting:
        lines.append("dark-seat mover: %d more wait for the next tick (at "
                     "most %d moves a tick, %s)" % (waiting, cap,
                                                    MOVE_CAP_ENV))
    state[latch] = reported
    return lines, moves


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
    move is reported and the task stays."""
    from . import takeover, tasks
    disp, err = takeover.mint_source_disposition(seat)
    if err:
        return err
    # NO FORCE (CURE2 F2): a measurably LIVE owner is the veto the
    # capability exists to keep, and a dark reading never overrides it.
    auth, err = takeover.mint_seat_reassign(
        tid, row, seat, to, {"kind": "dark-seat-move", "from": seat,
                             "to": to, "reason": reason},
        force=False, disposition=disp)
    if auth is None:
        return err
    _row, err = tasks.update(tid, takeover_auth=auth, owner=to)
    return err
