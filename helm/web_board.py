"""The BURN BOARD's one read — GET /api/board (task/2975).

WHAT IT ANSWERS. The owner asked for the console's separate parts — the
project lights, the per-family burn flags, the task backlog, the seats, the
land pipeline — to become "a single, elegant dashboard ... from which to
manage which projects are active at any given time". This endpoint is that
composition and nothing more. It ADDS NO DATA SOURCE: every number in it is
read by a function another door already calls (`registry.lights`,
`get_flags` over `burnflags.cached_snapshot`, `tasks.snapshot`, the roster
report behind `/api/chat/roster`, and the `/api/lr` body whose `scheduler` is
`scheduler.project`). A board that grew its own reading would be a second
answer free to disagree with the command line.

TWO AXES, READ IN THIS ORDER. A project's light is AUTHORIZATION: whether the
owner wants spending on it at all. A family's burn colour is SUPPLY: how much
the credentials behind a family can carry. Setting a project green is
choosing it active, and it outranks any credential flag. So the board joins
the families a project's seats spend onto that project's row, and the page
draws the light first.

EACH SECTION CARRIES ITS OWN CLOCK. `measured_at` is when the SOURCE was
read, not when this body was built, and `age_s`/`stale` are resolved at
RESPONSE time against the section's own `limit_s`. A section past its limit
is marked stale and the page draws it as STALE, never as fresh. A section
that could not be read at all carries `unavailable` with the reason, and its
numbers are absent rather than zero: "nothing to report" and "could not
look" never share a value on this wire.

THE LEGS ARE KEPT, THE LIGHTS ARE NOT. The joins cost a roster report, a
task ledger read, a land-pipeline body and a trunk read per project, so each
leg keeps its reading for the window `/api/flags` uses (`_legs`). The lights
are the owner's own writes and cost one registry read, so they are read on
every response: a light he sets shows on the next read of this board.

THE LAND PIPELINE PROJECTS ONE PROJECT. `/api/lr` is scoped to the project
this helm serves (`withheld.scope`), so lanes, owner holds and waits are
joined onto THAT project only. Every other project's row carries no such key,
which the page reads as "not read here", never as "no lanes".

THE LAST LAND IS READ OFF EACH PROJECT'S OWN TRUNK, NOT OFF THE PIPELINE. The
integrator's trains land by a fast-forward push and file no land-request row,
so the pipeline's newest close ran hours behind main. Every project's
registered checkout is asked instead, see `_trunk_tip`. The same read counts
the week's lands, one first-parent commit each.

LANES ARE COUNTED, NOT PROMISED. The headline is "lanes: R running of M
possible". R is every live worktree claim across every project, one per lane
however many seats hold it. M is the seats able to take work right now — up,
not paused, not walled, on a family whose credit is not RED — one each. M is a
count of seats, not a guarantee of M lanes, and the page says so.

QUIET IS DECIDED HERE, AT RESPONSE TIME. A project nobody touched for thirty
days, with nothing open and no light set in thirty days, is folded on the page
(`_quiet`). The rule lives on the server because the repository badges need
it too: a folded project's repositories are never sent to gh.
"""
import os
import re
import sys
import threading
import time

from . import gitfacts, registry, repofacts
from .web_cache import _drop, _read_behind
from .web_land import _api_lr
from .web_quota import get_flags
from .web_roster import _ROSTER_REP_CACHE, _roster_cached

_web = sys.modules.get(__package__ + ".web")
if _web is not None:
    globals().update({name: value for name, value in vars(_web).items()
                      if not (name.startswith("__") and name.endswith("__"))})


BOARD_TTL_S = 120

# HOW OLD A JOINED SECTION MAY BE AND STILL BE DRAWN. Five cache windows: the
# cache holds a join for one window at most, so a section older than five of
# them has stopped being refreshed, whatever the reason. The flag section is
# not bound by this number; it keeps the snapshot's own bound.
SECTION_LIMIT_S = 5 * BOARD_TTL_S

# WHAT THE EXPANDED ROW SHOWS, bounded so a busy project stays a pane.
TOP_TASKS = 5
TOP_WAITS = 6
WAIT_ROWS = 4
TOP_LANES = 8
KANBAN_ROWS = 40

# THE WEEK a project's progress is counted over, and the quiet rule's month.
WEEK_S = 7 * 86400
QUIET_S = 30 * 86400

# A worktree claim's resource: `worktree:<project>:<lane>`.
_WORKTREE = re.compile(r"^worktree:([^:]+):(.+)$")

# THE FLAG FIELDS THE PAGE READS: the chip's hover and the flag card's rows.
_FLAG_KEYS = ("colour", "cause", "axis", "provenance", "expires_at")

_SECTIONS = (("lights", "helm projects state"), ("flags", "helm burn"),
             ("tasks", "helm task list"), ("seats", "helm chat seats"),
             ("lands", "helm lr list"), ("trunk", "git for-each-ref"))


def _section(source, measured_at, limit_s, unavailable=None, **extra):
    rec = {"source": source, "measured_at": measured_at, "limit_s": limit_s,
           "unavailable": unavailable}
    rec.update(extra)
    return rec


def _aged(sec, now):
    """A copy of one section with `age_s` and `stale` resolved at `now`.

    A readable section with no clock is STALE: a reading that cannot say when
    it was taken has not shown that it is current."""
    out = dict(sec)
    at = out.get("measured_at")
    if out.get("unavailable") or out.get("loading"):
        out["age_s"], out["stale"] = None, False
    elif not isinstance(at, (int, float)):
        out["age_s"], out["stale"] = None, True
    else:
        out["age_s"] = max(0, int(now - at))
        limit = out.get("limit_s")
        out["stale"] = limit is not None and out["age_s"] > limit
    return out


def _projects():
    """{registry key: record} the joins attribute to; None when unreadable."""
    try:
        return dict(registry.load(strict=True).get("projects") or {})
    except (OSError, ValueError):
        return None


# THE TRUNK, per project. `main` first, `master` for the repositories trunked
# on it; the remote-tracking ref beside the local one, because the local ref
# can lead the remote in the moment between a land and its push.
_TRUNK_REFS = ("refs/heads/main", "refs/remotes/origin/main",
               "refs/heads/master", "refs/remotes/origin/master")
_PUSHED_REFS = ("refs/remotes/origin/main", "refs/remotes/origin/master")

# WHAT A TRUNK READ COST LAST TIME IT WAS ASKED, keyed by the repository's
# common git dir. The stamp is the files the answer depends on — the four refs,
# `packed-refs` and the push logs — read with `stat`, so an unmoved trunk is
# answered with no git spawn at all and a moved one is asked once. Bounded,
# because a test run mints a repository per arm inside one process.
_TRUNK_MEMO = {}
_TRUNK_LOCK = threading.Lock()
_TRUNK_MEMO_CAP = 1024


def _trunk_stamp(common):
    names = _TRUNK_REFS + ("packed-refs",) + tuple(
        os.path.join("logs", ref) for ref in _PUSHED_REFS)
    stamp = []
    for name in names:
        try:
            st = os.stat(os.path.join(common, name))
        except OSError:
            stamp.append((name, None))
            continue
        stamp.append((name, st.st_ino, st.st_size, st.st_mtime_ns))
    return tuple(stamp)


def _pushed_at(common, ref, sha):
    """The instant THIS checkout pushed `sha` to `ref`, or None.

    A train is committed, gated and then pushed, so the commit's clock is when
    it was MADE and the push is when it LANDED. The remote-tracking ref's own
    log records the push as `update by push` with the pusher's clock; a move
    written by a fetch or a pull is when this checkout HEARD of the commit, not
    when it landed, and is never taken. The last entry must name the tip, or
    it describes some earlier move."""
    try:
        with open(os.path.join(common, "logs", ref), "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 4096))
            tail = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    lines = tail.splitlines()
    if not lines:
        return None
    head, _tab, message = lines[-1].partition("\t")
    fields = head.split()
    if len(fields) < 4 or fields[1] != sha \
            or not message.startswith("update by push"):
        return None
    try:
        return int(fields[-2])
    except ValueError:
        return None


def _trunk_tip(path):
    """({at, how, sha, ref} or {unavailable: why}, the week's land times or
    None) for a checkout's trunk.

    `at` is the instant this checkout pushed the tip when its ref log says it
    did, else the tip commit's own committer time. The land times are the
    first-parent commits of the last week (`_land_times`), None when they
    could not be read. ONE or TWO git spawns when the trunk moved, NONE when
    it did not (`_TRUNK_MEMO`). A path that is not itself a checkout root is
    refused rather than handed to git, which would walk up and answer for
    whatever repository encloses it."""
    if not path or not os.path.exists(os.path.join(path, ".git")):
        return {"unavailable": "no git checkout at the registered path"}, None
    common = gitfacts._common_dir(path)
    if not common:
        return {"unavailable": "the checkout's git directory cannot be "
                "read"}, None
    stamp = _trunk_stamp(common)
    with _TRUNK_LOCK:
        hit = _TRUNK_MEMO.get(common)
    if hit and hit[0] == stamp:
        return hit[1], hit[2]
    from . import vcs
    rc, out, _err = vcs.backend(path).text(
        path, "for-each-ref", "--sort=-committerdate", "--count=1",
        "--format=%(committerdate:unix) %(objectname) %(refname)",
        *_TRUNK_REFS, timeout=10)
    if rc != 0:
        # UNREADABLE IS NEVER MEMOISED: a transient failure must not become a
        # standing answer.
        return {"unavailable": "git could not read the trunk (rc %d)" % rc}, \
            None
    fields = out.split()
    times = None
    if len(fields) != 3 or not fields[0].isdigit():
        rec = {"unavailable": "no main or master branch"}
    else:
        committed, sha, ref = int(fields[0]), fields[1], fields[2]
        pushed = [t for t in (_pushed_at(common, r, sha) for r in _PUSHED_REFS)
                  if t is not None]
        rec = {"at": max(pushed) if pushed else committed,
               "how": "push" if pushed else "commit", "sha": sha[:12],
               "ref": ref.replace("refs/heads/", "").replace("refs/remotes/",
                                                             "")}
        # A TIP OLDER THAN THE WEEK HAS NO LAND IN IT, so the log is not
        # walked. The times memoised are every land since a week before THIS
        # read, which holds every land any later read's week can contain:
        # an unmoved trunk only ages its lands out, it never ages one in.
        since = int(time.time()) - WEEK_S
        times = [] if committed < since else _land_times(path, sha, since)
    if times is None and "at" in rec:
        return rec, None                # unreadable is never memoised
    with _TRUNK_LOCK:
        _TRUNK_MEMO[common] = (stamp, rec, times)
        while len(_TRUNK_MEMO) > _TRUNK_MEMO_CAP:
            _TRUNK_MEMO.pop(next(iter(_TRUNK_MEMO)))
    return rec, times


def _land_times(path, sha, since):
    """The committer times of the first-parent commits on the trunk at `sha`
    since `since`, or None when git could not say. One land is one
    first-parent commit: a train's merge, or a commit made on main itself —
    never the lane commits a merge brought in."""
    from . import vcs
    rc, out, _err = vcs.backend(path).text(
        path, "log", "--first-parent", "--format=%ct",
        "--since=" + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since)),
        sha, timeout=10)
    if rc != 0:
        return None
    return [int(t) for t in out.split() if t.isdigit()]


def _trunk_join(projects):
    """(section, {project: {last_land, lands7, repos}}) for every row the
    board renders: the registry's projects, retired ones excepted. `lands7`
    is None when the trunk could not be read, never 0. `repos` is the
    checkout's remotes (`repofacts.remotes`), None with `repos_unavailable`
    when there is no checkout to ask."""
    read_at = time.time()
    if projects is None:
        return _section("git for-each-ref", None, SECTION_LIMIT_S,
                        unavailable="the registry could not be read"), {}
    out = {}
    cut = read_at - WEEK_S
    for key, rec in projects.items():
        if not isinstance(rec, dict) or rec.get("retired"):
            continue
        land, times = _trunk_tip(rec.get("path"))
        repos, why = repofacts.remotes(rec.get("path"))
        out[key] = {"last_land": land, "repos": repos,
                    "lands7": None if times is None
                    else sum(1 for t in times if t >= cut)}
        if repos is None:
            out[key]["repos_unavailable"] = why
    return _section("git for-each-ref", read_at, SECTION_LIMIT_S), out


def _flags_section():
    """The burn flags, through `/api/flags`'s own cached read."""
    try:
        got = get_flags()
    except Exception as exc:                # noqa: BLE001 — named, never zero
        return _section("helm burn", None, None, families={}, overall=None,
                        unavailable="the burn-flag read raised (%s)"
                        % type(exc).__name__)
    if not got.get("measured"):
        return _section("helm burn", None, got.get("bound_s"), families={},
                        overall=None,
                        unavailable=got.get("why") or "no fresh burn-flag "
                        "snapshot")
    fams = {fam: {k: fl.get(k) for k in _FLAG_KEYS}
            for fam, fl in (got.get("families") or {}).items()
            if isinstance(fl, dict)}
    return _section("helm burn", got.get("measured_at"), got.get("bound_s"),
                    families=fams, overall=got.get("overall"))


def _closings():
    """(closed_at, born_closed, accept): an `accept` for `tasks.snapshot` that
    watches every event go by, and the two things it learns — when each row
    last CLOSED, and which rows were born closed. A row filed already closed
    is a record of history (a tombstone), not work, so it is neither opened
    nor closed in any week."""
    closed_at, born = {}, set()

    def accept(row, prior):
        rid = str(row.get("id"))
        status = row.get("status")
        if prior is None and status == "closed":
            born.add(rid)
        if status == "closed" and (prior or {}).get("status") != "closed":
            closed_at[rid] = row.get("last_updated") or row.get("ts")
        elif status != "closed":
            closed_at.pop(rid, None)
        return True
    return closed_at, born, accept


def _tasks_join(keys):
    """(section, {project: {open, in_progress, top}}, {project: {opened7,
    closed7}}) from ONE read of the task ledger. The week's flow is counted
    off the ledger's events, so a row closed and then commented on is dated
    by its close, not by the comment."""
    from . import tasks
    read_at = time.time()
    closed_at, born, accept = _closings()
    rows, unavailable = tasks.snapshot(accept=accept)
    if unavailable:
        return _section("helm task list", None, SECTION_LIMIT_S,
                        unavailable=str(unavailable)), {}, {}
    cut = read_at - WEEK_S
    flow = {}
    for rid, row in rows.items():
        if rid in born:
            continue
        key = tasks.project_of_row(row)
        if key is None or (keys is not None and key not in keys):
            continue
        rec = flow.setdefault(key, {"opened7": 0, "closed7": 0})
        ts = row.get("ts")
        rec["opened7"] += isinstance(ts, (int, float)) and ts >= cut
        at = closed_at.get(rid)
        rec["closed7"] += row.get("status") == "closed" \
            and isinstance(at, (int, float)) and at >= cut
    live = [r for r in rows.values() if r.get("status") in tasks.OPEN_STATUSES]
    out, unscoped, unplaced = {}, 0, 0
    for row in tasks.board_order(live):
        key = tasks.project_of_row(row)
        if key is None:
            unscoped += 1
            continue
        if keys is not None and key not in keys:
            unplaced += 1
            continue
        rec = out.setdefault(key, {"open": 0, "in_progress": 0, "top": []})
        rec["open"] += 1
        rec["in_progress"] += row.get("status") == "in_progress"
        if len(rec["top"]) < TOP_TASKS:
            prio = row.get("priority")
            rec["top"].append({
                "id": str(row.get("id") or ""),
                "title": str(row.get("title") or ""),
                "priority": prio if prio in tasks.PRIORITIES else None,
                "status": str(row.get("status") or "")})
    return _section("helm task list", read_at, SECTION_LIMIT_S,
                    unscoped=unscoped,
                    unplaced=unplaced if keys is not None else None), out, flow


def _burn_family(row):
    """The burn-flag family a roster seat spends, or None.

    THE SAME TWO DOORS ROUTING READS. A self-verified launch family first,
    translated from the harness word (`claude`) to the credential word the
    flags use (`anthropic`) through `route.FROM_ALIASES`, which already holds
    that mapping; then `seat.family_for`, which parses a numbered seat name
    and reads the spawn register. Unverified runtime is display evidence, not
    authority, so it is never believed."""
    from . import route
    runtime = row.get("runtime") if isinstance(row.get("runtime"), dict) \
        else {}
    if row.get("runtime_verified") is True:
        spelled = str(runtime.get("family") or "").lower()
        if spelled:
            return route.FROM_ALIASES.get(spelled, spelled)
    try:
        from . import seat
        family, err = seat.family_for(str(row.get("seat") or ""))
    except Exception:          # noqa: BLE001 — a chip, not a verdict
        return None
    # THE NAME DOOR ANSWERS THE HARNESS WORD TOO (`<project>-claude` ->
    # `claude`), so it is translated the same way: read raw, a RED anthropic
    # flag would miss the seat and count it able to work.
    return None if err or not family \
        else route.FROM_ALIASES.get(str(family).lower(), family)


def _running(claims):
    """{project: {lane: {holders}}} from the roster's worktree claims. A claim
    whose holder is proven dead (`stale`) is not a running lane; one whose
    holder cannot be proven either way still holds its lane and counts."""
    out = {}
    for c in claims or ():
        if not isinstance(c, dict) or c.get("liveness") == "stale" \
                or c.get("stale"):
            continue
        m = _WORKTREE.match(str(c.get("resource") or ""))
        if not m:
            continue
        holder = str(c.get("holder_id") or c.get("holder") or "?")
        out.setdefault(m.group(1), {}).setdefault(m.group(2), set()).add(holder)
    return out


def _claims_landed(projects, key, lanes):
    """({lane: {state, proof, tip}}, {lane: dirty}) for one project's claimed
    lanes — IS THE WORK UNDER EACH LEASE ALREADY ON THE TRUNK, and for the
    lanes whose work is, IS THEIR ROOM DIRTY. A land releases no lease, so a
    lane stays claimed after it lands, and a claim read alone draws a landed
    lane as BUILDING — work in flight that is already in history.

    NOT A SECOND READING. It asks `work.lanes_landed`, the producer
    `helm work list` renders from, against the project's registered checkout,
    so the board and the command line cannot disagree about one lane; that
    producer keeps each answer by a stat stamp of its refs, so a board rebuilt
    over an unmoved repository costs no git call for it. A project with no
    checkout answers nothing, and a read that raised says so per lane rather
    than passing the lanes off as plain running work.

    DIRT IS ASKED OF THE LANDED LANES ONLY, through `work.rooms_dirty` (the
    `dirty` `list_rows` carries, from the same reader): one `git status` per
    landed held room, never one per claim. A landed lane under a DIRTY room is
    the one the CLI prints as DIRTY and `helm lr foldcheck` keeps, and a board
    that dropped the flag would draw it as done. Nothing stamps a room's
    working tree, so this read is not memoised; the board's own section
    cache is its bound. A lane not asked, or whose read raised, has no key."""
    rec = (projects or {}).get(key)
    path = rec.get("path") if isinstance(rec, dict) else None
    if not lanes or not path:
        return {}, {}
    from . import work
    try:
        got = work.lanes_landed(path, lanes)
    except Exception as exc:        # noqa: BLE001 — named, never a verdict
        return {lane: {"state": "unknown", "tip": None,
                       "proof": "the landedness read raised (%s)"
                       % type(exc).__name__} for lane in lanes}, {}
    landed = [lane for lane, v in got.items()
              if v.get("state") == work.LANE_LANDED]
    try:
        dirty = work.rooms_dirty(path, landed) if landed else {}
    except Exception:               # noqa: BLE001 — unread is null, never clean
        dirty = {}
    return {lane: {"state": v.get("state"), "proof": v.get("proof"),
                   "tip": (v.get("tip") or "")[:12] or None}
            for lane, v in got.items()}, dirty


def _usable(seats, flags):
    """The seats that could take work now: up (fresh or quiet), not an
    ephemeral agent or the owner, not paused, not walled, and not on a family
    whose burn flag is RED. A family the fold mints no flag for is not RED,
    and neither is one whose flags could not be read. M counts these, one
    each; a claimless one is also a lane at work (`_seat_lanes`)."""
    red = {fam for fam, fl in (flags.get("families") or {}).items()
           if isinstance(fl, dict) and fl.get("colour") == "RED"}
    return [row for row in seats or ()
            if isinstance(row, dict) and not row.get("ephemeral")
            and not row.get("owner")
            and row.get("presence") in ("fresh", "quiet")
            and row.get("availability") != "UNAVAILABLE"
            and not row.get("beacon_paused")
            and _burn_family(row) not in red]


def _seat_lanes(usable, running, projects):
    """({project: [seat]}, [unscoped seat]) — the usable seats that hold no
    lane claim, each ONE lane at work on the project its cwd resolves to.

    THE SAME DERIVATION HELM SCOPES WITH (`inject._ledger.project_for_cwd`:
    the deepest registered path, a lane worktree beside its repo belonging to
    that repo's project). A seat holding a counted claim is counted by that
    claim and never again here. A cwd no project claims is not guessed at:
    the seat is counted nowhere and named."""
    from .inject._ledger import project_for_cwd
    holding = {h for lanes in running.values() for held in lanes.values()
               for h in held}
    keys = {str((rec if isinstance(rec, dict) else {}).get("name") or key): key
            for key, rec in projects.items()}
    placed, unscoped = {}, []
    for row in usable:
        seat = str(row.get("seat") or "")
        if not seat or seat in holding:
            continue
        key = keys.get(project_for_cwd(row.get("cwd"), projects=projects))
        if key is None:
            unscoped.append(seat)
        else:
            placed.setdefault(key, []).append(seat)
    return placed, sorted(unscoped)


def _roster_read(reader, room="main"):
    """(when it was built, report): the cached roster report the chat roster
    serves, asked through `reader` — the roster leg of `_board_build`."""
    rep = reader(room)
    hit = _ROSTER_REP_CACHE.get(room)
    return (hit[0] if hit else time.time()), rep


def _seats_join(projects, flags, measured_at, rep):
    """(section, {project: {seats, families, running}}, fleet) from the
    roster report `rep`. `fleet` is {running, possible, claimed, seats,
    offboard, unscoped}: R, M, R's two halves, the claimed lanes on projects
    the board has no row for (so the shares add up to R), and the seats at
    work where no project claims the directory. It is None when the roster
    did not validate; R's seat half, `offboard` and `unscoped` are None when
    the registry could not be read.

    A seat SPENDS when it is not absent, so a family chip is minted only from
    present seats; absent seats stay listed in the detail. Each chip carries
    its family's flag colour and cause from `flags`, or None when the fold
    mints no flag for that family — carried, never dropped."""
    keys = None if projects is None else set(projects)
    out, unplaced = {}, 0
    for row in rep.get("seats") or ():
        if not isinstance(row, dict) or row.get("ephemeral") \
                or row.get("owner"):
            continue
        key = str(row.get("project") or "").strip()
        if not key:
            continue
        if keys is not None and key not in keys:
            unplaced += 1
            continue
        rec = out.setdefault(key, {"seats": [], "families": []})
        rec["seats"].append({"seat": str(row.get("seat") or ""),
                             "presence": row.get("presence"),
                             "family": _burn_family(row)})
        # THE PROJECT'S NEWEST BEAT. The registry's own `last_seen` is only as
        # fresh as the last `helm sync`, which can be a week old while seats
        # work on the project every minute; a seat's beat is read live.
        beat = row.get("last_seen")
        if isinstance(beat, (int, float)) and beat > rec.get("active_at", 0):
            rec["active_at"] = beat
    known = flags.get("families") or {}
    for rec in out.values():
        spending = {}
        for s in rec["seats"]:
            if s["presence"] != "absent" and s["family"]:
                spending.setdefault(s["family"], []).append(s["seat"])
        rec["families"] = [
            {"family": fam, "colour": (known.get(fam) or {}).get("colour"),
             "cause": (known.get(fam) or {}).get("cause"), "seats": names}
            for fam, names in sorted(spending.items())]
    running = _running(rep.get("claims"))
    usable = _usable(rep.get("seats"), flags)
    at_work, unscoped = ({}, None) if projects is None \
        else _seat_lanes(usable, running, projects)
    for key in set(running) | set(at_work):
        if keys is not None and key not in keys:
            continue
        lanes = running.get(key) or {}
        landed, dirty = _claims_landed(projects, key, lanes)
        out.setdefault(key, {"seats": [], "families": []})["running"] = [
            {"lane": lane, "kind": "claim", "seats": sorted(lanes[lane]),
             "landed": landed.get(lane), "dirty": dirty.get(lane)}
            for lane in sorted(lanes)] + [
            {"lane": None, "kind": "seat", "seats": [seat]}
            for seat in sorted(at_work.get(key) or ())]
    why = "the roster did not validate" if rep.get("roster_failed") else None
    claimed = sum(len(lanes) for lanes in running.values())
    # A CLAIM ON A PROJECT THE BOARD HAS NO ROW FOR still runs, so it counts
    # in R and in its own share: the per-project shares plus this one add up
    # to the headline.
    offboard = None if keys is None else sum(
        len(lanes) for key, lanes in running.items() if key not in keys)
    seated = None if projects is None \
        else sum(len(v) for v in at_work.values())
    # HOW MANY OF R ARE LANDED WITH THE LEASE STILL HELD, counted off the
    # SAME running[] entries the kanban draws from, so the headline and the
    # columns cannot disagree. R keeps counting them: a held lease is a live
    # claim until its holder releases it, and the annotation says which.
    landed_held = None if projects is None else sum(
        1 for rec in out.values() for r in rec.get("running") or ()
        if (r.get("landed") or {}).get("state") == "landed")
    fleet = None if why else {
        "running": None if seated is None else claimed + seated,
        "possible": len(usable), "claimed": claimed, "seats": seated,
        "offboard": offboard, "unscoped": unscoped, "landed": landed_held}
    return _section("helm chat seats", measured_at, SECTION_LIMIT_S,
                    unavailable=why,
                    unplaced=unplaced if keys is not None else None), out, fleet


def _kanban_card(c):
    """One land-request card as the kanban draws it — the SAME four fields
    for this project's pipeline and every other project's. `trunk_contains_tip`
    is the pipeline's own tri-state (True only when trunk PROVABLY holds the
    row's pinned tip with no verdict recorded), carried so the page draws that
    row as landed rather than as waiting for a review of work in history."""
    return {"id": str(c.get("id") or ""), "lane": str(c.get("lane") or ""),
            "state": str(c.get("state") or ""),
            "trunk_contains_tip": c.get("trunk_contains_tip")}


def _lands_join(reader):
    """(section, {scope: {lanes, owner, waits}}) off `/api/lr`, asked through
    `reader`. A pipeline still warming after a restart is STILL BEING READ,
    and asked for again rather than served as an answer (`_legs`)."""
    source = "helm lr list"
    body = reader({})[0]
    if not isinstance(body, dict):
        return _section(source, None, SECTION_LIMIT_S, scope=None,
                        unavailable="the land pipeline gave no body"), {}
    now = time.time()
    scope = (body.get("withheld") or {}).get("scope")
    if body.get("warming"):
        return _section(source, None, SECTION_LIMIT_S, scope=scope,
                        loading=True, retry=True), {}
    why = body.get("unavailable")
    if not why and not scope:
        why = "the land pipeline names no project scope"
    read_age = body.get("read_age_s")
    at = now - read_age if isinstance(read_age, (int, float)) else None
    if why:
        return _section(source, None, SECTION_LIMIT_S, scope=scope,
                        unavailable=str(why)), {}
    filed = [c for c in body.get("loops") or ()
             if isinstance(c, dict) and not c.get("honored")]
    building = body.get("building") or {}
    model = body.get("scheduler") or {}
    lands = body.get("recent_lands") or {}
    rec = {
        "lanes": {"in_flight": len(filed),
                  "filed": [str(c.get("lane") or "")
                            for c in filed][:TOP_LANES],
                  "building": (None if building.get("unavailable")
                               else building.get("total")),
                  "building_lanes": [str(r.get("lane") or "")
                                     for r in building.get("rows") or ()
                                     if isinstance(r, dict)][:TOP_LANES],
                  # THE KANBAN'S CARDS: every loop in flight with the state
                  # the pipeline gave it, for the page to put in a column
                  "loops": [_kanban_card(c) for c in filed][:KANBAN_ROWS]},
        "landed": None if lands.get("unavailable") else [
            {"lane": r.get("lane"), "task": r.get("task"),
             "age_s": r.get("age_s")}
            for r in (lands.get("rows") or ())[:TOP_LANES]
            if isinstance(r, dict)],
        "owner": {"holds": (None if model.get("unavailable")
                            else model.get("owner_hold_count"))},
        "waits": [] if model.get("unavailable") else [
            {"label": g.get("label"), "count": g.get("count"),
             "oldest_age_s": g.get("oldest_age_s"),
             "rows": [{"plain_title": r.get("plain_title"),
                       "stage_class": r.get("stage_class"),
                       "age_s": r.get("age_s")}
                      for r in (g.get("rows") or ())[:WAIT_ROWS]
                      if isinstance(r, dict)]}
            for g in (model.get("groups") or ())[:TOP_WAITS]
            if isinstance(g, dict)],
    }
    return _section(source, at, SECTION_LIMIT_S, scope=scope), {scope: rec}


# EVERY OTHER PROJECT'S PIPELINE: `/api/lr?all_projects=1`, read on its own
# daemon thread and never waited on. It is a whole second projection — as slow
# as the first on a cold start — so the board answers with what the last
# completed read said, or LOADING before there is one, and each board response
# asks again. The read itself is `_api_lr`'s own serve-stale-while-revalidate
# cache, so asking again costs a cache hit until its inputs move.
_FLEET_LOCK = threading.Lock()
_FLEET = {"thread": None, "done": None}
_FLEET_SOURCE = "helm lr list --all-projects"


def _fleet_kick():
    """Start the all-projects read unless one is already running. The reader is
    bound HERE, on the caller's thread, so the thread asks the same door this
    response saw."""
    reader = _api_lr
    with _FLEET_LOCK:
        running = _FLEET["thread"]
        if running is not None and running.is_alive():
            return

        def read():
            try:
                done = (time.time(), reader({"all_projects": ["1"]})[0], None)
            except Exception as exc:        # noqa: BLE001 — named, never empty
                done = (time.time(), None, "the all-projects read raised (%s)"
                        % type(exc).__name__)
            with _FLEET_LOCK:
                _FLEET["done"] = done
        worker = threading.Thread(target=read, name="board-lr-all",
                                  daemon=True)
        _FLEET["thread"] = worker
    worker.start()


def _fleet_wait(timeout):
    """True once no all-projects read is running (tests, shutdown)."""
    with _FLEET_LOCK:
        worker = _FLEET["thread"]
    if worker is not None:
        worker.join(timeout)
    return worker is None or not worker.is_alive()


def _fleet_reset():
    """Forget the last all-projects read, after the running one finishes."""
    _fleet_wait(10)
    with _FLEET_LOCK:
        _FLEET.update(thread=None, done=None)


def _fleet_now(keys, now):
    """(section, {project: {loops, landed}}) for every project in `keys` but
    the one the scoped pipeline already projects, off the last completed
    all-projects read. LOADING before one has completed or while it warms;
    UNAVAILABLE, with the reason, when it failed. A project the read reached
    and found nothing for gets two empty lists — that is an answer. A row whose
    project is unresolved, or is no project on this board, is counted in
    `unplaced` and never guessed onto a row."""
    _fleet_kick()
    with _FLEET_LOCK:
        done = _FLEET["done"]
    if done is None:
        return _section(_FLEET_SOURCE, None, SECTION_LIMIT_S, loading=True), {}
    read_at, body, why = done
    if why or not isinstance(body, dict):
        return _section(_FLEET_SOURCE, None, SECTION_LIMIT_S,
                        unavailable=why or "the all-projects read gave no "
                        "body"), {}
    if body.get("warming"):
        return _section(_FLEET_SOURCE, None, SECTION_LIMIT_S, loading=True), {}
    if body.get("unavailable"):
        return _section(_FLEET_SOURCE, None, SECTION_LIMIT_S,
                        unavailable=str(body["unavailable"])), {}
    scope = (body.get("withheld") or {}).get("scope")
    lands = body.get("recent_lands") or {}
    lands_read = isinstance(lands, dict) and not lands.get("unavailable")
    out = {key: {"loops": [], "landed": [] if lands_read else None}
           for key in keys if key != scope}
    unplaced = 0
    aged = max(0, now - read_at)

    def home(row):
        project = row.get("foreign_project")
        return out.get(project) if project else None

    for card in body.get("loops") or ():
        if not isinstance(card, dict) or card.get("honored"):
            continue
        if not card.get("foreign_project") and not card.get(
                "project_unresolved"):
            continue                        # the scope's own row
        rec = home(card)
        if rec is None:
            unplaced += 1
        elif len(rec["loops"]) < KANBAN_ROWS:
            rec["loops"].append(_kanban_card(card))
    for row in (lands.get("rows") or ()) if lands_read else ():
        if not isinstance(row, dict) or "foreign_project" not in row:
            continue
        if not row.get("foreign_project") and not row.get(
                "project_unresolved"):
            continue
        rec = home(row)
        if rec is None:
            unplaced += 1
        elif len(rec["landed"]) < TOP_LANES:
            age = row.get("age_s")
            rec["landed"].append({
                "lane": row.get("lane"), "task": row.get("task"),
                "age_s": age + int(aged) if isinstance(age, (int, float))
                else None})
    read_age = body.get("read_age_s")
    measured_at = read_at - read_age if isinstance(read_age, (int, float)) \
        else None
    # THE LANDS LIST IS CAPPED FLEET-WIDE (the newest few), so a project with
    # none in it may still have landed: the section says how much was read,
    # and the page draws "none among the newest" rather than a quiet zero.
    shown = len(lands.get("rows") or ()) if lands_read else None
    total = lands.get("total") if lands_read else None
    partial = {"shown": shown, "total": total} if isinstance(total, int) \
        and shown is not None and total > shown else None
    return _section(_FLEET_SOURCE, measured_at, SECTION_LIMIT_S, scope=scope,
                    unplaced=unplaced, landed_partial=partial), out


# THE BOARD'S LEGS, READ AT ONCE, EACH UNDER ITS OWN BUDGET: the first read
# after a restart took 70s against the page's 45s, its legs read in turn. A leg
# past its budget is STILL BEING READ (`loading`, no numbers, `retry`) while
# its read goes on (`web_cache._read_behind`); a reading is served BOARD_TTL_S,
# then re-read behind itself, and never past SECTION_LIMIT_S.
_LEGS = ("flags", "tasks", "seats", "lands", "trunk")
_LEG_BUDGET_S = dict.fromkeys(_LEGS, 3.0)
# what a leg that raised says, and what its section carries with no reading
_LEG_RAISED = {"flags": "the burn-flag read raised (%s)",
               "tasks": "the task-ledger read raised (%s)",
               "seats": "the roster could not be read (%s)",
               "lands": "the land-pipeline read raised (%s)",
               "trunk": "the trunk read raised (%s)"}
_LEG_BLANK = {"flags": {"families": {}, "overall": None},
              "lands": {"scope": None}}
_STILL = "still being read"


def _blank(name, **state):
    """A leg's section with no reading: its source, no clock, `state`."""
    sec = _section(dict(_SECTIONS)[name], None, SECTION_LIMIT_S,
                   **_LEG_BLANK.get(name, {}))
    sec.update(state)
    return sec


def _unkept(got):
    """Why a leg's answer is not a reading to keep, or None: a section that
    could not be read or is still being read, or a roster report that did
    not validate. The next board read asks again."""
    if isinstance(got[0], dict):
        return got[0].get("unavailable") or (_STILL if got[0].get("retry")
                                             else None)
    return "the roster did not validate" if got[1].get("roster_failed") \
        else None


def _legs(fns):
    """({leg: its answer}, {leg: the section it carries instead}), each leg
    waited for until its own budget from one start. A leg whose read raised,
    or whose read behind its served reading failed, is UNAVAILABLE: that
    reading is held back with its age, never drawn as current."""
    start = time.monotonic()
    held = {name: _read_behind("board:" + name, BOARD_TTL_S, SECTION_LIMIT_S,
                               fn, _unkept) for name, fn in fns.items()}
    answers, absent = {}, {}
    for name, (thread, box) in held.items():
        if thread is not None:
            thread.join(max(0.0, start + _LEG_BUDGET_S[name]
                            - time.monotonic()))
        if thread is not None and thread.is_alive():
            absent[name] = _blank(name, loading=True, retry=True)
            continue                    # the box is read only once it ended
        fail = box.get("failed")
        why = fail and (fail[2] if fail[1] == "refused"
                        else _LEG_RAISED[name] % fail[2])
        if "got" not in box:
            absent[name] = _blank(name, unavailable=why)
        elif fail and why != _STILL:
            age = int(time.time() - box["at"])
            ago = "%dm" % (age // 60) if age >= 120 else "%ds" % age
            absent[name] = _blank(name, unavailable="%s; its last reading, "
                                  "%s old, is not drawn" % (why, ago))
        else:
            answers[name] = box["got"]
    return answers, absent


def _forget():
    """Drop every leg's reading once no read of it runs (tests)."""
    for name in _LEGS:
        _drop("board:" + name)


def _read(sec):
    """Did this section's leg answer: neither unreadable nor still read?"""
    return not (sec.get("unavailable") or sec.get("loading"))


def _board_build():
    """Every join, each section stamped with its own read (`_legs`). The
    readers are bound HERE, on the caller's thread, so a leg that outlives
    this build still asks the door this build saw."""
    projects = _projects()
    keys = None if projects is None else set(projects)
    roster, lr = _roster_cached, _api_lr
    got, absent = _legs({"flags": lambda: (_flags_section(),),
                         "tasks": lambda: _tasks_join(keys),
                         "seats": lambda: _roster_read(roster),
                         "lands": lambda: _lands_join(lr),
                         "trunk": lambda: _trunk_join(projects)})
    (flags,) = got.get("flags") or (absent.get("flags"),)
    if flags.get("loading"):
        # THE SEATS JOIN READS THE FLAGS: which seats can take work (none on
        # a RED family), so M, the seats at work and every chip's colour. It
        # is still being read until they are.
        got.pop("seats", None)
        absent.setdefault("seats", _blank("seats", loading=True, retry=True))
    tasks_sec, by_tasks, flow = got.get("tasks") or (absent.get("tasks"),
                                                     {}, {})
    seats_sec, by_seats, fleet = (
        _seats_join(projects, flags, *got["seats"]) if "seats" in got
        else (absent["seats"], {}, None))
    lands_sec, by_lands = got.get("lands") or (absent.get("lands"), {})
    trunk_sec, by_trunk = got.get("trunk") or (absent.get("trunk"), {})
    joins = {}
    for key, rec in by_tasks.items():
        joins.setdefault(key, {})["tasks"] = rec
    for key, rec in by_seats.items():
        joins.setdefault(key, {}).update(rec)
    for key, rec in by_lands.items():
        joins.setdefault(key, {}).update(rec)
    if _read(seats_sec):
        # A READABLE ROSTER ANSWERS FOR EVERY PROJECT ON THE BOARD: one with
        # open work and nobody seated says so in two empty lists.
        for rec in joins.values():
            rec.setdefault("seats", [])
            rec.setdefault("families", [])
    # THE LAST LAND RIDES EVERY RENDERED ROW, added after the defaulting
    # above so a project joined by nothing else does not grow empty seat
    # lists it was never read for.
    for key, rec in by_trunk.items():
        rec = dict(rec)
        lands7 = rec.pop("lands7")
        week = flow.get(key) or {}
        joins.setdefault(key, {}).update(rec, progress={
            "lands7": lands7,
            "opened7": week.get("opened7", 0) if _read(tasks_sec) else None,
            "closed7": week.get("closed7", 0) if _read(tasks_sec) else None})
    return {"sections": {"flags": flags, "tasks": tasks_sec,
                         "seats": seats_sec, "lands": lands_sec,
                         "trunk": trunk_sec},
            "joins": joins, "fleet": fleet}


def _lights_now(now):
    """(section, green count, lights, records) from the registry, read at
    response time; the last three are None when it could not be read."""
    try:
        reg = registry.load(strict=True)
        lights = registry.lights(reg)
    except (OSError, ValueError) as exc:
        return _section("helm projects state", None, None,
                        unavailable=str(exc) or "registry unreadable"), \
            None, None, None
    green = sum(1 for lit in lights.values()
                if lit.get("authored") and lit.get("colour") == "green")
    return _section("helm projects state", now, None,
                    projects=len(lights)), green, lights, \
        dict(reg.get("projects") or {})


def _quiet(rec, join, lit, tasks_ok, now):
    """Is this project QUIET: nothing seen for thirty days, nothing open, no
    lane running, and no light set in thirty days? All of them, or it stays
    up.

    Activity is the newer of the registry's last session and the newest seat
    beat. The backlog must have been READ as zero: a count nobody could read,
    or one past its bound, is not zero. A light with no recorded time keeps
    the row up, because its age is unknown, not old."""
    if not tasks_ok or not isinstance(rec, dict) or not isinstance(lit, dict):
        return False
    if (join.get("tasks") or {}).get("open") or join.get("running"):
        return False
    seen = max(rec.get("last_seen") or 0, join.get("active_at") or 0)
    if seen > now - QUIET_S:
        return False
    return not lit.get("authored") or (isinstance(lit.get("ts"), (int, float))
                                       and lit["ts"] <= now - QUIET_S)


def _visible(joins, quiet, now):
    """Each repository row with its visibility, on copies. A project on show
    has its GitHub repositories asked of gh (in the background, once a day);
    a quiet one is answered from what is already cached and never asked. A
    remote that is not on GitHub is unknown, and says why."""
    ask, keep = set(), set()
    for key, rec in joins.items():
        for r in rec.get("repos") or ():
            if r.get("slug"):
                (keep if quiet.get(key) else ask).add(r["slug"])
    vis = dict(repofacts.visibility(sorted(keep), now=now, ask=False))
    vis.update(repofacts.visibility(sorted(ask), now=now, ask=True))
    none = {"visibility": "unknown", "why": "not a GitHub remote",
            "checked_at": None, "stale": False}
    return {key: [dict(r, **(vis.get(r["slug"]) if r.get("slug") else none))
                  for r in rec["repos"]]
            for key, rec in joins.items() if rec.get("repos")}


def _stamped(rec, now, **fresh):
    """One project's join with its last land aged at `now` and the
    response-time fields in `fresh`, on a copy: the cached body is shared by
    every response."""
    out = dict(rec, **fresh)
    land = rec.get("last_land")
    if land:
        at = land.get("at")
        out["last_land"] = dict(
            land, age_s=max(0, int(now - at)) if isinstance(at, (int, float))
            else None)
    return out


def _api_board():
    """GET /api/board — the burn board's one read. Never a 500: a join that
    cannot be built answers with every section named UNAVAILABLE."""
    try:
        built = _board_build()
    except Exception as exc:                # noqa: BLE001 — named, never empty
        why = "the board could not be built (%s)" % type(exc).__name__
        built = {"sections": {name: _section(src, None, None, unavailable=why)
                              for name, src in _SECTIONS if name != "lights"},
                 "joins": {}, "fleet": None}
    now = time.time()
    lights_sec, green, lights, records = _lights_now(now)
    sections = {name: _aged(sec, now)
                for name, sec in built["sections"].items()}
    sections["lights"] = _aged(lights_sec, now)
    tasks_ok = _read(sections["tasks"]) and not sections["tasks"]["stale"]
    joins = built["joins"]
    quiet = {key: _quiet((records or {}).get(key), rec,
                         (lights or {}).get(key), tasks_ok, now)
             for key, rec in joins.items()}
    repos = _visible(joins, quiet, now)
    fleet_sec, pipeline = _fleet_now(set(joins), now)
    sections["fleet"] = _aged(fleet_sec, now)
    overall = sections["flags"].get("overall") or {}
    fleet = built.get("fleet") or {}
    return {"generated_at": now,
            "headline": {"lanes": {k: fleet.get(k) for k in (
                "running", "possible", "claimed", "seats", "offboard", "landed",
                "unscoped")},
                         "colour": overall.get("colour"),
                         "family": overall.get("family"),
                         "green": green},
            "sections": sections,
            "projects": {key: _stamped(rec, now, quiet=quiet[key],
                                       **({"repos": repos[key]}
                                          if key in repos else {}),
                                       **({"pipeline": pipeline[key]}
                                          if key in pipeline else {}))
                         for key, rec in joins.items()}}
