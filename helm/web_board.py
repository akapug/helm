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

THE GATE IS READ OFF THE GATE WINDOW, NOT OFF THE PIPELINE (task/3129). A
train's whole-suite gate is recorded by its door (`gatewindow`) and no land
request says a train is being gated, so the kanban's gate column said "none"
while one ran for 46 minutes. `_gate_join` reads the record
`helm gate window show` reads, and a leased lane whose tip the running gate's
head carries is drawn ON THE GATE (`_claims_landed`).

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

from . import gitfacts, registry, repofacts, scheduler, web_land_model
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

# HOW OLD A LAND-PIPELINE SECTION MAY BE (task/3632). Its age is two ages
# added: how long ago its leg read the pipeline (served for up to
# SECTION_LIMIT_S while a read runs behind it) and how old the pipeline's
# own reading was then (served for up to its hard bound while it rebuilds,
# `web_land_model._LR_HARD_TTL_S`, and a body a restart restores is older
# still until the first rebuild replaces it). The leg's bound alone sat
# inside that sum, so a reading both caches were still serving and
# refreshing read STALE: "helm 24 → helm 0 marked STALE → helm 24" in ten
# minutes, and "other projects STALE" 48 s after a restart (console walk 2,
# finding 2). Past the sum, one of the two has stopped.
PIPELINE_LIMIT_S = SECTION_LIMIT_S + web_land_model._LR_HARD_TTL_S

# HOW LONG THE FIRST BOARD OF A SERVER LIFE WAITS FOR THE ALL-PROJECTS READ
# it kicks (task/3632): that read restores its body from disk in well under
# a second, and a board answered without it drew every other project as
# still being read.
FLEET_FIRST_WAIT_S = 3.0

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
_FLAG_KEYS = ("colour", "cause", "axis", "provenance", "money_provenance",
              "expires_at", "credits")

_SECTIONS = (("lights", "helm projects state"), ("flags", "helm burn"),
             ("tasks", "helm task list"), ("seats", "helm chat seats"),
             ("lands", "helm lr list"), ("trunk", "git for-each-ref"),
             ("gate", "helm gate window show"), ("teams", "helm team"))

# A TRAIN'S MERGE, as `helm train` writes it (landwindow: "<train>: merge
# lane <lane>"): the lanes a compose room carries are these merges between
# the trunk it stood on and its head.
_TRAIN_MERGE = re.compile(r"^(train\d+): merge lane (.+)$")


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

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_TRUNK_MEMO": (
        "keyed by common git dir and checked against the ref files' stat"),
}
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
    fams = {fam: dict({k: fl.get(k) for k in _FLAG_KEYS},
                      bills=_family_bills(fam))
            for fam, fl in (got.get("families") or {}).items()
            if isinstance(fl, dict)}
    return _section("helm burn", got.get("measured_at"), got.get("bound_s"),
                    families=fams, overall=got.get("overall"))


def _family_bills(family):
    """The account groups that BILL a model family, in route order: the seat
    catalog's one reading (`seat.billing_groups`), the same one the seeder
    mints the declared rows from (task/3461). [] for a family served from
    the operator's own GPUs, which has no bill; None where the catalog names
    no bill. The flags speak model families and the accounts table vendors;
    these groups are the join Fleet › credit draws both ways, and no group is
    guessed from a family's name."""
    from . import seat
    return seat.billing_groups(family)


def _teams_join():
    """(section, {project: {team}}) — each project's team, its shares turned
    into budgets and colours, its drift and its history (`teams.board_model`),
    joined onto the project's row (task/3156). The section carries what the
    card re-folds a draft with: the fleet's seats, each rate family's supply,
    the per-seat burn, each local family's lanes, the owner's sentence per
    colour, the roles and the approval tier.

    EVERY KEY THE MODEL CARRIES BESIDE ITS PROJECTS RIDES THE SECTION, so a
    reading `board_model` gains reaches the card without a second list here
    to forget it (the lanes did not, on the first live read)."""
    from . import teams
    read_at = time.time()
    model = teams.board_model(now=read_at)
    return _section("helm team", read_at, SECTION_LIMIT_S,
                    **{k: v for k, v in model.items() if k != "projects"}), \
        {key: {"team": rec} for key, rec in model["projects"].items()}


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
    """(section, {project: {open, in_progress, p0, p1, top}}, {project:
    {opened7, closed7}}) from ONE read of the task ledger. `p0` and `p1`
    count the open rows at each rank, for the project's line (task/3445). The week's flow is counted
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
        rec = out.setdefault(key, {"open": 0, "in_progress": 0, "p0": 0,
                                   "p1": 0, "top": []})
        rec["open"] += 1
        rec["in_progress"] += row.get("status") == "in_progress"
        prio = row.get("priority")
        rec["p0"] += prio == "P0"
        rec["p1"] += prio == "P1"
        if len(rec["top"]) < TOP_TASKS:
            rec["top"].append({
                "id": str(row.get("id") or ""),
                "title": str(row.get("title") or ""),
                "priority": prio if prio in tasks.PRIORITIES else None,
                "status": str(row.get("status") or "")})
    return _section("helm task list", read_at, SECTION_LIMIT_S,
                    unscoped=unscoped,
                    unplaced=unplaced if keys is not None else None), out, flow


def _burn_family(row):
    """The burn-flag family a roster seat spends, or None — asked of
    `seat_usability.burn_family`, the one door the signing-liveness read
    shares, so a seat's family has one answer on every surface."""
    from . import seat_usability
    return seat_usability.burn_family(row)


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


def _claims_landed(projects, key, lanes, gate=None, memo=None):
    """({lane: {state, proof, tip}}, {lane: dirty}, {lane: gate}) for one
    project's claimed
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
    cache is its bound. A lane not asked, or whose read raised, has no key.

    ON THE GATE (task/3130) is the third answer: {lane: gate} for each lane
    whose work is NOT on the trunk yet and whose tip is an ancestor of the
    running gate's room head (`gate`, from `_gate_heads`) — the lane rides a
    train that is being gated, so it is neither building nor under review.
    ONE `merge-base --is-ancestor` per such lane against that one head,
    through `memo`, which lives for one board build. A lane already on the
    trunk is on every later head too, so it is never asked; an UNKNOWN answer
    is not a verdict and leaves the lane where it was."""
    rec = (projects or {}).get(key)
    path = rec.get("path") if isinstance(rec, dict) else None
    if not lanes or not path:
        return {}, {}, {}
    from . import vcs, work
    try:
        got = work.lanes_landed(path, lanes)
    except Exception as exc:        # noqa: BLE001 — named, never a verdict
        return {lane: {"state": "unknown", "tip": None,
                       "proof": "the landedness read raised (%s)"
                       % type(exc).__name__} for lane in lanes}, {}, {}
    landed = [lane for lane, v in got.items()
              if v.get("state") == work.LANE_LANDED]
    try:
        dirty = work.rooms_dirty(path, landed) if landed else {}
    except Exception:               # noqa: BLE001 — unread is null, never clean
        dirty = {}
    on_gate, head = {}, (gate or {}).get("head")
    memo = {} if memo is None else memo
    for lane, v in got.items() if head else ():
        tip = v.get("tip")
        if v.get("state") != work.LANE_UNLANDED or not tip:
            continue
        ask = (path, tip, head)
        if ask not in memo:
            try:
                memo[ask] = vcs.backend(path).ancestry(path, tip, head)
            except Exception:       # noqa: BLE001 — unread is no verdict
                memo[ask] = None
        if memo[ask] == vcs.ANCESTOR:
            on_gate[lane] = {"head": head[:12], "label": gate.get("label")}
    return {lane: {"state": v.get("state"), "proof": v.get("proof"),
                   "tip": (v.get("tip") or "")[:12] or None}
            for lane, v in got.items()}, dirty, on_gate


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


def _seats_join(projects, flags, measured_at, rep, gates=None, memo=None):
    """(section, {project: {seats, families, running}}, fleet) from the
    roster report `rep`. `gates` is {project: the running gate's head and
    label} (`_gate_heads`); a claimed lane riding that gate's train carries
    it as `on_gate` (`_claims_landed`), with `memo` the one ancestry cache of
    this board build. `fleet` is {running, possible, claimed, seats,
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
        landed, dirty, on_gate = _claims_landed(
            projects, key, lanes, gate=(gates or {}).get(key), memo=memo)
        out.setdefault(key, {"seats": [], "families": []})["running"] = [
            {"lane": lane, "kind": "claim", "seats": sorted(lanes[lane]),
             "landed": landed.get(lane), "dirty": dirty.get(lane),
             "on_gate": on_gate.get(lane)}
            for lane in sorted(lanes)] + [
            {"lane": None, "kind": "seat", "seats": [seat]}
            for seat in sorted(at_work.get(key) or ())]
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
    sec = _seats_section(measured_at, rep,
                         unplaced if keys is not None else None)
    fleet = None if sec["unavailable"] else {
        "running": None if seated is None else claimed + seated,
        "possible": len(usable), "claimed": claimed, "seats": seated,
        "offboard": offboard, "unscoped": unscoped, "landed": landed_held}
    return sec, out, fleet


def _seats_section(measured_at, rep, unplaced=None):
    """The seats section off one roster report `rep` read at `measured_at`:
    UNAVAILABLE when the roster did not validate. `unplaced` is the join's
    count of seats on projects the board has no row for (`_seats_join`)."""
    return _section("helm chat seats", measured_at, SECTION_LIMIT_S,
                    unavailable="the roster did not validate"
                    if rep.get("roster_failed") else None, unplaced=unplaced)


def _kanban_card(c, age=None):
    """One land-request card as the kanban draws it — the SAME fields for this
    project's pipeline and every other project's. `trunk_contains_tip` is the
    pipeline's own tri-state (True only when trunk PROVABLY holds the row's
    work — for a BUILD, the work on its lane, never the base it was sent
    against); `on_main_unverdicted` is the one predicate's answer over it
    (`landreq.on_main_unverdicted`: that work with NO verdict recorded). The
    server counts every such card on the on-main line and never sends it as
    a card; the word rides the wire so a page reading an older server's
    cards folds exactly those, and never a row under a recorded verdict. A
    HELD source-clean row on trunk is not one either (task/3053): it is a
    live card whose `source_clean_on_main` names the close or the re-hold it
    owes.

    `owes_rehold` marks the one kind of such card whose move is its
    RECIPIENT's re-hold (the sentence's RE-HOLD branch, read off the same two
    fields `landreq.source_clean_on_main` reads): landed work nobody but that
    reviewer can move, which the kanban counts on one line (`_kanban_feed`).
    `age_s` is how long the card has waited, None when unknown.

    WHAT THE ONE LAND BOARD DRAWS ON A CARD (task/3585), copied off the same
    row and never looked up again: the task the pipeline joined (`_card_task`), the
    holder the next move waits on (`_holder`), the reviewed tip, the gate
    token, and the two marks the header strip counts, `contrary` and
    `stalled`. A build with no reviewed tip has none — its base is the trunk
    it was sent against, not its work.

    `stalled` IS THE ALARM, NOT THE MEASUREMENT: `landreq.stall_alarm`, the
    predicate `helm lr list` prints STALLED by, so a row a proved successor
    carried is quiet here exactly as it is there. And the row's own fields
    the card's marks and detail panel read (`_card_detail`) ride along, so
    the web shows the detail rather than naming a CLI verb for it."""
    from . import landreq                   # DEFERRED — landreq is heavy
    rehold = c.get("source_clean_rehold")
    return {"id": str(c.get("id") or ""), "lane": str(c.get("lane") or ""),
            "state": str(c.get("state") or ""),
            "task": _card_task(c), "holder": _holder(c),
            "tip": str(c.get("review_sha") or "")[:12] or None,
            "gate": str(c.get("gate") or ""),
            "contrary": bool(c.get("contrary")),
            "stalled": landreq.stall_alarm(c),
            "trunk_contains_tip": c.get("trunk_contains_tip"),
            "on_main_unverdicted": landreq.on_main_unverdicted(c),
            # a source-clean hold on trunk is a live card, and its note is
            # the one sentence naming the move it owes (task/3053)
            "source_clean_on_main": c.get("source_clean_on_main"),
            "owes_rehold": bool(c.get("source_clean_on_main"))
            and isinstance(rehold, dict) and bool(rehold),
            "age_s": age, **_card_detail(c)}


# WHAT THE ONE LAND BOARD'S MARKS AND DETAIL PANEL READ OFF A ROW (task/3585,
# owner rule 2: the web shows the detail, never a CLI verb for it): the fields
# the old wall's renderer read, copied off the row `landreq_cli.card` built.
_CARD_DETAIL = (
    "kind", "chain_root", "supersedes", "branch", "base_sha",
    "review_sha_full", "author", "reviewer", "polarity", "polarity_source",
    "attest_state", "attest_detail", "attest_source", "owed_by",
    "owed_seat_standing", "contrary_state", "contrary_provenance",
    "contrary_discharge", "succession_state", "succession_unknown_reason",
    "discharged", "superseding_tip", "withdraw_contradicted", "abandoned",
    "abandon_reason", "close_reason", "closed_by_landing", "receipt_state",
    "closed_ts_unreadable", "closed_ts_impossible", "ledger_refused",
    "advisory_lines", "dwell_known", "ungated", "observable", "observe_why",
    "landed", "merged_local", "has_upstream", "landing_trunk_sha",
    "timeline", "artifact_ref", "report_ref", "close_evidence",
    "delivered_report_correction", "cancel_reason",
    # the HELD tip a source-clean car rides at (`landreq.source_clean_car`),
    # which may descend from the reviewed tip `tip` names: the work reader
    # matches a pushed train's car to this row by it (`work_model._carried`)
    "source_clean_tip")
# the two whose False is a finding ("never measured", "not observed"), so it
# rides the wire where every other field's False is left off
_CARD_FALSE = frozenset(("dwell_known", "observable"))


def _card_detail(c):
    """The `_CARD_DETAIL` fields one row carries a value for: a quiet row's
    card stays small, and an absent field reads to the page exactly as the
    empty one it replaces."""
    return {k: c[k] for k in _CARD_DETAIL
            if c.get(k) or (k in _CARD_FALSE and c.get(k) is False)}


def _card_task(c):
    """The task the pipeline's one join gave the loop (`task` on the card,
    over the chain's first row and the lane's record, task/3643), None when
    it is UNKNOWN or names none; never re-derived from the label, so the
    board cannot contradict a stored key. A card from an older server that
    carries no `task` falls back to the label's literal (`_lane_task`)."""
    if "task" in c:
        return c.get("task") or None
    return _lane_task(c.get("lane"))


def _lane_task(lane):
    """`task/N` when the lane names exactly one task by the one join
    (`taskkey.join` over the lane's literal `task/N` or `task-N`), else None:
    two named is ambiguous, not the first of them, and a trailing number is
    never a task."""
    from . import taskkey                   # DEFERRED — the ledger module
    return taskkey.join(lane=lane, lanes=False).task


def _holder(c):
    """Who the next move waits on, in one word: the seat when the pipeline
    named one, else the first word of its role ("integrator", "nobody"),
    else "unknown" — the bucket the owed-by row always drew."""
    seat = c.get("holder_seat")
    if seat:
        return str(seat)
    return (str(c.get("holder_role") or "").split() or ["unknown"])[0]


def _kanban_tally(live, nonbillable=frozenset()):
    """{live, marks: {contrary, stalled, nonbillable, moving}, holders:
    {holder: n}} over
    EVERY live card of one project — the cards sent, the cards the cap cut and
    the re-hold line alike — so the one board's header strip counts the row
    set and never the cap (task/3585). One mark per row, contrary outranking
    stalled, the partition the in-flight row always drew."""
    marks = {"contrary": 0, "stalled": 0, "nonbillable": 0, "moving": 0}
    holders = {}
    for c in live:
        marks[_mark(c, nonbillable)] += 1
        who = c.get("holder") or "unknown"
        holders[who] = holders.get(who, 0) + 1
    return {"live": len(live), "marks": marks, "holders": holders}


def _mark(c, nonbillable=frozenset()):
    """The one mark a live card is counted under: `scheduler.mark`, the one
    the scheduler counts by too (task/3631), so the board's strip and the
    scheduler cannot count one row two ways."""
    return scheduler.mark(c, nonbillable)


def _nonbillable(body):
    """{id: why} for the nonbillable holds one /api/lr body lists — the ids
    the tally counts under their own mark, and the reason each card's NOT
    MEASURABLE line says."""
    return {str(u.get("id")): u.get("reason") for u in
            body.get("unmeasurable") or ()
            if isinstance(u, dict) and u.get("id")}


def _landed_card(r, age_s):
    """One landed row as the landed column draws it: lane, task, age, and the
    reviewed tip and gate receipt `recent_lands` carries (task/3585) — and,
    when the row carries them, the proof the closing ladder recorded
    (`on_trunk`, `how`, onto `trunk_ref` at `trunk_sha`), the closure stamp
    or that it was unreadable, and the chain the land's rounds share, so the
    column says how each land was proven and folds a chain's rounds."""
    out = {"lane": r.get("lane"), "task": r.get("task"), "age_s": age_s,
           "tip": str(r.get("reviewed_tip") or "")[:12] or None,
           "gate": str(r.get("gate") or "")}
    out.update({k: r[k] for k in ("on_trunk", "how", "trunk_ref", "ts",
                                  "ts_unreadable", "chain_root") if r.get(k)})
    if r.get("trunk_sha"):
        out["trunk_sha"] = str(r["trunk_sha"])[:12]
    return out


def _kanban_split(cards, read_age_s):
    """(live cards, on-main line or None, collapsed lines) for one project's
    in-flight requests — the kanban's half of the owner board's rule: LIVE
    obligations are drawn one card each, everything else is one line with its
    count, its oldest age and the command that lists it (task/2381).

    THE SAME JUDGEMENT AS THE WAITS, BY ONE CALL. `scheduler.collapse_class`
    decides every card here exactly as it decides the waits' rows: placed off
    the live frontier, unplaceable with its lane gone, or on main with no
    verdict recorded (`landreq.on_main_unverdicted`) — every card here is on
    the chain frontier, so none is superseded. The on-main line is drawn in
    the landed column rather than under the columns, and carries the `lanes`
    it counts so the page keeps placing them (a claim on one is not drawn as
    building); it is the scheduler's own line otherwise, the same words, count
    rule and command the waits draw. None when there is none, never a zero
    claim.

    EACH LINE CARRIES THE ROWS IT COUNTS (`rows`, the same cards a live row
    is sent as), so the one land board opens a count onto exactly those rows
    and their detail rather than naming the verb that lists them (task/3585,
    owner rule 2)."""
    live, folded, lanes, rows = [], [], [], {}
    for card in cards:
        try:
            age = int(card.get("dwell_s")) + int(read_age_s) \
                if card.get("dwell_known") is True else None
        except (TypeError, ValueError):
            age = None
        klass = scheduler.collapse_class(card)
        if klass == "on_main":
            lanes.append(str(card.get("lane") or ""))
        if klass:
            folded.append((klass, age, card.get("frontier")
                           if klass == "off_frontier" else None))
            rows.setdefault(klass, []).append(_kanban_card(card, age))
        else:
            live.append(_kanban_card(card, age))
    lines = [dict(line, rows=rows.get(line["class"], []))
             for line in scheduler.collapsed_lines(folded)]
    on_main = next((dict(line, lanes=lanes) for line in lines
                    if line["class"] == "on_main"), None)
    return live, on_main, [line for line in lines
                           if line["class"] != "on_main"]


# THE ROWS THE OWNER CAN MOVE NOW, drawn first (task/3130): a request under
# review, reviewed, or sent back, whose work is NOT already on main.
_ACTIONABLE = frozenset(("AWAITING_REVIEW", "REVIEWED", "CHANGES_REQUESTED"))
REHOLD_COMMAND = "helm lr list"


def _kanban_feed(live, nonbillable=frozenset()):
    """{loops, loops_more, rehold} — one project's live cards as the kanban
    is sent them, with every cap SAID (task/3130).

    ORDER: the actionable cards first (`_ACTIONABLE`, work not on main), then
    every other live card, each group in the pipeline's own order. The cards
    whose move is a recipient's RE-HOLD over landed work (`owes_rehold`) are
    not sent one each: they are ONE line, `rehold`, with their count, the
    oldest wait, the lanes it places and each lane's own sentence, and the
    command that lists them; None when there are none, never a zero claim.

    THE CAP IS KANBAN_ROWS cards, and what it cut is `loops_more`, {state:
    count}, so the page adds "N more" to the column each state is drawn in.
    Nothing is cut silently: loops + loops_more + rehold count every card.
    The cut cards themselves ride as `loops_cut`, so the Work page's reader
    (`work_model.pipe_rows`, behind `/api/work`) types every row the tally
    counts (task/3585, task/3643).

    `nonbillable` is the ids /api/lr lists as nonbillable holds, or a
    {id: why} (`_nonbillable`), whose reason each such card carries."""
    reasons = nonbillable if isinstance(nonbillable, dict) else {}
    for c in live:
        # each card names the mark the tally counts it under, so a filtered
        # board keeps exactly the cards its number counts
        c["mark"] = _mark(c, nonbillable)
        if reasons.get(c.get("id")):
            c["unmeasurable_reason"] = reasons[c["id"]]
    rehold = [c for c in live if c.get("owes_rehold")]
    rest = [c for c in live if not c.get("owes_rehold")]
    ordered = sorted(rest, key=lambda c: not (
        c.get("state") in _ACTIONABLE
        and c.get("trunk_contains_tip") is not True))      # stable
    more = {}
    for c in ordered[KANBAN_ROWS:]:
        more[c.get("state") or ""] = more.get(c.get("state") or "", 0) + 1
    ages = [c["age_s"] for c in rehold if isinstance(c.get("age_s"), int)]
    line = {"count": len(rehold), "command": REHOLD_COMMAND,
            "label": "landed lane%s owe%s a re-hold" % (
                ("", "s") if len(rehold) == 1 else ("s", "")),
            "oldest_age_s": max(ages) if ages else None,
            "lanes": [c["lane"] for c in rehold],
            # each row names the mark and holder the tally counts it under,
            # so a filtered board keeps exactly the rows its number counts
            # and the card itself (`lr`), so the board draws each row's
            # detail rather than a hover or a CLI verb (task/3585)
            "rows": [{"lane": c["lane"], "owed": c.get("source_clean_on_main"),
                      "holder": c.get("holder") or "unknown",
                      "mark": c["mark"], "lr": c}
                     for c in rehold]} if rehold else None
    return {"loops": ordered[:KANBAN_ROWS], "loops_more": more,
            "loops_cut": ordered[KANBAN_ROWS:],
            "rehold": line, "tally": _kanban_tally(live, nonbillable)}


def _more(total, shown):
    """How many a capped list left out: `total` less `shown` when the total
    was measured and exceeds it, else 0."""
    return total - shown if isinstance(total, int) \
        and not isinstance(total, bool) and total > shown else 0


def _building_detail(building):
    """{building_rows, building_unmeasured, building_unavailable} off one
    /api/lr `building` reading: each lane's holder, commits ahead, lease left
    and uncommitted edits (the newest TOP_LANES, as `building_lanes`), how
    many leased lanes git could not measure, and why the reading failed."""
    why = building.get("unavailable")
    return {"building_unavailable": str(why) if why else None,
            "building_unmeasured": None if why
            else building.get("unmeasured"),
            "building_rows": None if why else [
                {k: r.get(k) for k in ("lane", "holder", "ahead",
                                       "lease_remaining_s", "dirty")}
                for r in (building.get("rows") or ())[:TOP_LANES]
                if isinstance(r, dict)]}


def _lr_read_at(body):
    """When the land-pipeline reading `body` was taken: the instant `/api/lr`
    counts its `read_age_s` from (`read_at`), or None when the body does not
    say. THE READING'S OWN CLOCK, NEVER THE ASKER'S (task/3657): an unmoved
    pipeline answers every ask with one reading, and a section dated by its
    ask's own clock less that whole-second age moved its stamp on every ask,
    so the Work reader, whose revision marks the stamp, rebuilt every poll."""
    at = body.get("read_at")
    return at if isinstance(at, (int, float)) and not isinstance(at, bool) \
        else None


def _lands_join(reader):
    """(section, {scope: {lanes, owner, waits}}) off `/api/lr`, asked through
    `reader`. A pipeline still warming after a restart is STILL BEING READ,
    and asked for again rather than served as an answer (`_legs`)."""
    source = "helm lr list"
    body = reader({})[0]
    if not isinstance(body, dict):
        return _section(source, None, PIPELINE_LIMIT_S, scope=None,
                        unavailable="the land pipeline gave no body"), {}
    now = time.time()
    scope = (body.get("withheld") or {}).get("scope")
    if body.get("warming"):
        return _section(source, None, PIPELINE_LIMIT_S, scope=scope,
                        loading=True, retry=True), {}
    why = body.get("unavailable")
    if not why and not scope:
        why = "the land pipeline names no project scope"
    read_age = body.get("read_age_s")
    at = _lr_read_at(body)
    if why:
        return _section(source, None, PIPELINE_LIMIT_S, scope=scope,
                        unavailable=str(why)), {}
    filed = [c for c in body.get("loops") or ()
             if isinstance(c, dict) and not c.get("honored")]
    live, on_main, folded = _kanban_split(filed, read_age or 0)
    feed = _kanban_feed(live, _nonbillable(body))
    building = body.get("building") or {}
    model = body.get("scheduler") or {}
    groups = [g for g in model.get("groups") or () if isinstance(g, dict)]
    lands = body.get("recent_lands") or {}
    ahead = [str(r.get("lane") or "") for r in building.get("rows") or ()
             if isinstance(r, dict)]
    landed = None if lands.get("unavailable") else [
        _landed_card(r, r.get("age_s"))
        for r in (lands.get("rows") or ())[:TOP_LANES]
        if isinstance(r, dict)]
    rec = {
        "lanes": {"in_flight": len(live),
                  "filed": [c["lane"] for c in live][:TOP_LANES],
                  "building": (None if building.get("unavailable")
                               else building.get("total")),
                  "building_lanes": ahead[:TOP_LANES],
                  # WHAT THE BUILDING LIST LEFT OUT, counted against the
                  # total the pipeline measured, never a silent cap
                  "building_more": _more(None if building.get("unavailable")
                                         else building.get("total"),
                                         min(len(ahead), TOP_LANES)),
                  # EACH BUILDING LANE'S DETAIL, the four facts `helm work
                  # list` prints (task/3585), the lanes git could not
                  # measure, and a read that failed, named: a failed lane
                  # read is UNKNOWN on the board, never zero lanes
                  **_building_detail(building),
                  # THE KANBAN'S CARDS: every LIVE loop in flight with the
                  # state the pipeline gave it, actionable first, for the
                  # page to put in a column (`_kanban_feed`); what the cap
                  # cut is counted per state, the re-holds owed on landed
                  # work are one line, and the rest is counted on the two
                  # lines beside them
                  "loops": feed["loops"], "loops_more": feed["loops_more"],
                  "loops_cut": feed["loops_cut"],
                  "rehold": feed["rehold"], "tally": feed["tally"],
                  "on_main": on_main, "collapsed": folded},
        "landed": landed,
        # THE LANDS THE CARD DOES NOT LIST: the pipeline shows its newest few
        # and counts every one (`recent_lands.total`)
        "landed_more": None if landed is None
        else _more(lands.get("total"), len(landed)),
        "owner": {"holds": (None if model.get("unavailable")
                            else model.get("owner_hold_count"))},
        "waits": [] if model.get("unavailable") else [
            {"label": g.get("label"), "count": g.get("count"),
             "oldest_age_s": g.get("oldest_age_s"),
             "rows": [{"plain_title": r.get("plain_title"),
                       "stage_class": r.get("stage_class"),
                       "age_s": r.get("age_s"),
                       "source_clean_on_main": r.get("source_clean_on_main")}
                      for r in (g.get("rows") or ())[:WAIT_ROWS]
                      if isinstance(r, dict)]}
            for g in groups[:TOP_WAITS]],
        # THE GROUPS THE WAITS DO NOT LIST, counted and never cut silently;
        # each group's own `count` says how many rows its list left out
        "waits_more": 0 if model.get("unavailable")
        else _more(len(groups), TOP_WAITS),
        # WHAT THE WAITS DO NOT LIST, one line per class, from the scheduler
        # that decided it — the groups above hold live obligations only
        "waits_collapsed": [] if model.get("unavailable") else [
            {key: line.get(key) for key in ("class", "label", "count",
                                            "oldest_age_s", "command")}
            for line in model.get("collapsed") or ()
            if isinstance(line, dict)],
    }
    # `ages_at`: the instant every age in `rec` was stamped, so a response
    # served from this kept reading moves them on (`_reaged`)
    return _section(source, at, PIPELINE_LIMIT_S, scope=scope,
                    ages_at=now), {scope: rec}


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


def _fleet_reading():
    """(section, body, read_at) — the last completed all-projects read, after
    asking for the next one (`_fleet_kick`): the fleet section as it stands
    before any project is placed (its reading's clock and scope; LOADING
    before one has completed or while it warms; UNAVAILABLE, with the
    reason, when it failed), the body when it is a reading to place rows
    from (else None), and when that read completed. The ONE read of the
    fleet: `_fleet_now` places its rows, and `_board_marks` needs only the
    section."""
    _fleet_kick()
    with _FLEET_LOCK:
        done = _FLEET["done"]
    if done is None:
        # THE FIRST READ OF A SERVER LIFE IS WAITED FOR, a little (task/3632):
        # it restores the last body from disk, and without it the first board
        # after a restart drew every other project as still being read
        _fleet_wait(FLEET_FIRST_WAIT_S)
        with _FLEET_LOCK:
            done = _FLEET["done"]
    if done is None:
        return _section(_FLEET_SOURCE, None, PIPELINE_LIMIT_S,
                        loading=True), None, None
    read_at, body, why = done
    if why or not isinstance(body, dict):
        return _section(_FLEET_SOURCE, None, PIPELINE_LIMIT_S,
                        unavailable=why or "the all-projects read gave no "
                        "body"), None, None
    if body.get("warming"):
        return _section(_FLEET_SOURCE, None, PIPELINE_LIMIT_S,
                        loading=True), None, None
    if body.get("unavailable"):
        return _section(_FLEET_SOURCE, None, PIPELINE_LIMIT_S,
                        unavailable=str(body["unavailable"])), None, None
    return _section(_FLEET_SOURCE, _lr_read_at(body), PIPELINE_LIMIT_S,
                    scope=(body.get("withheld") or {}).get("scope")), \
        body, read_at


def _fleet_now(keys, now):
    """(section, {project: {loops, landed}}) for every project in `keys` but
    the one the scoped pipeline already projects, off the last completed
    all-projects read. LOADING before one has completed or while it warms;
    UNAVAILABLE, with the reason, when it failed. A project the read reached
    and found nothing for gets two empty lists — that is an answer. A row whose
    project is unresolved, or is no project on this board, is counted in
    `unplaced` and never guessed onto a row."""
    sec, body, read_at = _fleet_reading()
    if body is None:
        return sec, {}
    scope = sec["scope"]
    lands = body.get("recent_lands") or {}
    lands_read = isinstance(lands, dict) and not lands.get("unavailable")
    out = {key: {"loops": [], "landed": [] if lands_read else None,
                 "on_main": None, "collapsed": [], "loops_more": {},
                 "rehold": None, "tally": _kanban_tally([]), "landed_more": 0 if lands_read else None}
           for key in keys if key != scope}
    unplaced = 0
    aged = max(0, now - read_at)

    def home(row):
        project = row.get("foreign_project")
        return out.get(project) if project else None

    theirs = {}
    for card in body.get("loops") or ():
        if not isinstance(card, dict) or card.get("honored"):
            continue
        if not card.get("foreign_project") and not card.get(
                "project_unresolved"):
            continue                        # the scope's own row
        if home(card) is None:
            unplaced += 1
        else:
            theirs.setdefault(card["foreign_project"], []).append(card)
    # THE SAME SPLIT AS THIS PROJECT'S OWN KANBAN, per project, so the fleet
    # view and the row's own view of one project cannot disagree
    read_age = body.get("read_age_s")
    for project, cards in theirs.items():
        live, on_main, folded = _kanban_split(
            cards, (read_age if isinstance(read_age, (int, float)) else 0)
            + int(aged))
        out[project].update(_kanban_feed(live, _nonbillable(body)),
                            on_main=on_main,
                            collapsed=folded)
    for row in (lands.get("rows") or ()) if lands_read else ():
        if not isinstance(row, dict) or "foreign_project" not in row:
            continue
        if not row.get("foreign_project") and not row.get(
                "project_unresolved"):
            continue
        rec = home(row)
        if rec is None:
            unplaced += 1
        elif len(rec["landed"]) >= TOP_LANES:
            rec["landed_more"] += 1         # counted, never cut silently
        else:
            age = row.get("age_s")
            rec["landed"].append(_landed_card(
                row, age + int(aged) if isinstance(age, (int, float))
                else None))
    # THE LANDS LIST IS CAPPED FLEET-WIDE (the newest few), so a project with
    # none in it may still have landed: the section says how much was read,
    # and the page draws "none among the newest" rather than a quiet zero.
    shown = len(lands.get("rows") or ()) if lands_read else None
    total = lands.get("total") if lands_read else None
    partial = {"shown": shown, "total": total} if isinstance(total, int) \
        and shown is not None and total > shown else None
    sec.update(unplaced=unplaced, landed_partial=partial)
    return sec, out


# THE GATE WINDOW (task/3129): the whole-suite gate `helm gate window show`
# reports in flight, per project. The kanban's gate column read the land
# requests alone and said "none" while a window gate ran for 46 minutes: the
# door keeps its own record (`gatewindow`) and no request row says a train is
# being gated. So this leg reads THAT record, through the reader and the
# liveness ladder `show` uses, never a copy of either.
_GATE_SOURCE = dict(_SECTIONS)["gate"]
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


def _gate_join(projects, path=None, observe=None, inflight=None, now=None):
    """(section, {project: [gate card]}) — every live run in the gate-window
    store, on the project whose repository it gates, newest first.

    THE SAME READ AS `helm gate window show`: `gatewindow.read_runs_checked`
    over the store, then `gatewindow.live_runs`, which asks each run's own
    authority (or node) whether it still runs. An UNREADABLE store is
    UNAVAILABLE with its reason, never "no gate". An ABSENT store is a helm
    home that never launched one, and every project with a checkout gets an
    empty list: that is an answer.

    A run is placed by `gatewindow.project_id` — the identity the door wrote,
    the common git directory — asked of each registered checkout; a run whose
    repository no project claims is counted in `unplaced`. A host that could
    not be read keeps its runs listed, as `show` does, and each such card
    says that whether it runs is UNKNOWN. `observe`, `inflight` and `now` are
    the door's own seams."""
    from . import gatewindow
    clock = now or time.time
    read_at = clock()
    if projects is None:
        return _section(_GATE_SOURCE, None, SECTION_LIMIT_S,
                        unavailable="the registry could not be read, so no "
                        "gate can be placed on a project"), {}
    rows, why = gatewindow.read_runs_checked(path or gatewindow.runs_path())
    if why:
        return _section(_GATE_SOURCE, None, SECTION_LIMIT_S,
                        unavailable=why), {}
    live, _retired, unknown = gatewindow.live_runs(
        rows, inflight=inflight, observe=observe, now=clock)
    homes = {}
    for key, rec in projects.items():
        where = rec.get("path") if isinstance(rec, dict) else None
        if not where or rec.get("retired") \
                or not os.path.exists(os.path.join(where, ".git")):
            continue
        ident = gatewindow.project_id(where)
        if ident:
            homes.setdefault(ident, []).append(key)
    out = {key: [] for keys in homes.values() for key in keys}
    unplaced = 0
    for row in live:
        keys = homes.get(row.get("project"))
        if not keys:
            unplaced += 1
            continue
        card = _gate_card(row, unknown, read_at, projects[keys[0]]["path"])
        for key in keys:
            out[key].append(card)
    for cards in out.values():
        cards.sort(key=lambda c: (c["age_s"] is None, c["age_s"] or 0))
    return _section(_GATE_SOURCE, read_at, SECTION_LIMIT_S, unplaced=unplaced,
                    unknown_hosts=sorted(unknown)), out


def _gate_card(row, unknown, now, checkout):
    """One live gate run as the kanban draws it: its label, the node it runs
    on, how long it has run, the head it gates and the lanes its train
    carries (`_train_lanes`). `running` is "running" when its authority or
    node answered, "unknown" when its node could not be read, and "held" for
    a dispatch nobody can name, which the window holds without anyone able
    to ask whether it runs."""
    from . import gatewindow
    launched = gatewindow._row_ts(row)
    host = row.get("host")
    named = bool(host and (row.get("run_id") or (
        row.get("job_id") and row.get("generation"))))
    train, lanes, lanes_why = _train_lanes(row, checkout)
    if host in unknown:
        running, why = "unknown", "%s could not be read" % host
    elif not named:
        running, why = "held", ("its dispatch was never announced, so no node "
                                "can be asked whether it runs")
    else:
        running, why = "running", None
    return {"label": row.get("label") or train or None, "train": train,
            "host": host, "job": row.get("job_id") or row.get("run_id"),
            "age_s": None if launched is None
            else max(0, int(now - launched)),
            "head": row.get("head"), "trunk": row.get("trunk"),
            "room": row.get("room"), "running": running, "running_why": why,
            "lanes": lanes, "lanes_unavailable": lanes_why}


def _train_lanes(row, checkout):
    """(train, [{lane, tip}], None), or (None, None, why) — the lanes a gate's
    compose room carries: the train merges (`_TRAIN_MERGE`) on the first-parent
    line between the trunk the run stood on and the head it gates, oldest
    first, each with the lane tip it merged. ONE `git log`, in the room while
    it stands, else in the project's checkout, which shares its objects."""
    head, trunk = row.get("head"), row.get("trunk")
    for name, sha in (("head", head), ("trunk", trunk)):
        if not isinstance(sha, str) or not _SHA.match(sha):
            return None, None, "the record names no readable %s" % name
    room = row.get("room")
    where = room if isinstance(room, str) and os.path.exists(
        os.path.join(room, ".git")) else checkout
    from . import vcs
    rc, out, _err = vcs.backend(where).text(
        where, "log", "--first-parent", "--merges", "--format=%P%x09%s",
        "%s..%s" % (trunk, head), timeout=10)
    if rc != 0:
        return None, None, "git could not read the train (rc %s)" % rc
    train, lanes = None, []
    for line in reversed(out.splitlines()):
        parents, _tab, subject = line.partition("\t")
        hit = _TRAIN_MERGE.match(subject.strip())
        if not hit:
            continue
        tips = parents.split()
        train = train or hit.group(1)
        lanes.append({"lane": hit.group(2),
                      "tip": tips[1][:12] if len(tips) > 1 else None})
    return train, lanes, None


def _gate_heads(by_gate):
    """{project: {head, label}} — the one head each project's leased lanes
    are asked about: its newest run whose authority or node CONFIRMED it
    runs. A run whose node could not be read, or that nobody can name, puts
    no lane on the gate: a lane drawn as gated under a run that already
    ended is the misplacement this column exists to end."""
    out = {}
    for key, cards in (by_gate or {}).items():
        for card in cards:                  # newest first
            if card.get("running") == "running" and card.get("head"):
                out[key] = {"head": card["head"], "label": card.get("label")}
                break
    return out


# THE BOARD'S LEGS, READ AT ONCE, EACH UNDER ITS OWN BUDGET: the first read
# after a restart took 70s against the page's 45s, its legs read in turn. A leg
# past its budget is STILL BEING READ (`loading`, no numbers, `retry`) while
# its read goes on (`web_cache._read_behind`); the budget is the READ's, so a
# later board does not wait it out again. A reading is served BOARD_TTL_S,
# then re-read behind itself, and never past SECTION_LIMIT_S.
_LEGS = ("flags", "tasks", "seats", "lands", "trunk", "gate", "teams")
_LEG_BUDGET_S = dict.fromkeys(_LEGS, 3.0)
# what a leg that raised says, and what its section carries with no reading
_LEG_RAISED = {"flags": "the burn-flag read raised (%s)",
               "tasks": "the task-ledger read raised (%s)",
               "seats": "the roster could not be read (%s)",
               "lands": "the land-pipeline read raised (%s)",
               "trunk": "the trunk read raised (%s)",
               "gate": "the gate-window read raised (%s)",
               "teams": "the team read raised (%s)"}
_LEG_BLANK = {"flags": {"families": {}, "overall": None},
              "lands": {"scope": None},
              "teams": {"seats": [], "pace": {}, "burn": None, "say": {},
                        "roles": {}, "slots": None, "tier": None}}
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
    waited for until ITS READ's budget runs out, counted from when that read
    began. A read this build started is waited for its whole budget; one an
    earlier build started and already waited out is STILL BEING READ, and
    this build says so at once (task/3657: a second full wait on it cost
    every warm poll three seconds while the land projection rebuilt, and
    answered nothing new). A leg whose read raised, or whose read behind its
    served reading failed, is UNAVAILABLE: that reading is held back with
    its age, never drawn as current."""
    held = {name: _read_behind("board:" + name, BOARD_TTL_S, SECTION_LIMIT_S,
                               fn, _unkept) for name, fn in fns.items()}
    answers, absent = {}, {}
    for name, (thread, box) in held.items():
        if thread is not None:
            thread.join(max(0.0, box["began"] + _LEG_BUDGET_S[name]
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


def _board_legs():
    """(projects, flags, {leg: its answer}, {leg: the section it carries
    instead}) — the registry and every leg, each read under its own budget
    (`_legs`): the half of a board build that READS, shared by the build and
    by `_board_marks`. The readers are bound HERE, on the caller's thread, so
    a leg that outlives this build still asks the door this build saw."""
    projects = _projects()
    keys = None if projects is None else set(projects)
    roster, lr = _roster_cached, _api_lr
    got, absent = _legs({"flags": lambda: (_flags_section(),),
                         "tasks": lambda: _tasks_join(keys),
                         "seats": lambda: _roster_read(roster),
                         "lands": lambda: _lands_join(lr),
                         "trunk": lambda: _trunk_join(projects),
                         "gate": lambda: _gate_join(projects),
                         "teams": _teams_join})
    (flags,) = got.get("flags") or (absent.get("flags"),)
    if flags.get("loading"):
        # THE SEATS JOIN READS THE FLAGS: which seats can take work (none on
        # a RED family), so M, the seats at work and every chip's colour. It
        # is still being read until they are.
        got.pop("seats", None)
        absent.setdefault("seats", _blank("seats", loading=True, retry=True))
    return projects, flags, got, absent


def _board_marks():
    """({"sections": {lands, fleet, seats, tasks}}, projects) — the four
    sections the Work reader's revision marks (`work_model.revision`: each
    one's clock, scope and state), aged as `/api/board` ages them, and the
    registry they were read against, WITHOUT the joins (task/3657). A count
    only a join makes (`unplaced`, `landed_partial`) is not in them.

    THE SAME READS AS THE BOARD, NOT A SECOND READER: the legs through
    `_board_legs`, the seats section through `_seats_section`, the fleet
    through `_fleet_reading`, so a leg past its time is read again behind
    itself and the next all-projects read is asked for exactly as a board
    read does. What is left out is what cost a Work poll seconds on the
    owner's console and never reached a snapshot it was answered from: the
    roster join (`_claims_landed`), the lights, the repositories and each
    project's fleet rows. A revision these marks do not find is built from a
    whole board, under that board's own revision (`work_model.snapshot`)."""
    projects, _flags, got, absent = _board_legs()
    seats = got.get("seats")
    now = time.time()
    sections = {"lands": (got.get("lands") or (absent.get("lands"),))[0],
                "fleet": _fleet_reading()[0],
                "seats": _seats_section(*seats) if seats
                else absent["seats"],
                "tasks": (got.get("tasks") or (absent.get("tasks"),))[0]}
    return {"sections": {name: _aged(sec, now)
                         for name, sec in sections.items()}}, projects


def _board_build():
    """Every join, each section stamped with its own read (`_board_legs`)."""
    projects, flags, got, absent = _board_legs()
    tasks_sec, by_tasks, flow = got.get("tasks") or (absent.get("tasks"),
                                                     {}, {})
    gate_sec, by_gate = got.get("gate") or (absent.get("gate"), {})
    # ONE ANCESTRY CACHE FOR THIS BUILD: each leased lane is asked once
    # whether the running gate's head carries it (`_claims_landed`)
    seats_sec, by_seats, fleet = (
        _seats_join(projects, flags, *got["seats"],
                    gates=_gate_heads(by_gate) if _read(gate_sec) else None,
                    memo={}) if "seats" in got
        else (absent["seats"], {}, None))
    lands_sec, by_lands = got.get("lands") or (absent.get("lands"), {})
    trunk_sec, by_trunk = got.get("trunk") or (absent.get("trunk"), {})
    teams_sec, by_teams = got.get("teams") or (absent.get("teams"), {})
    joins = {}
    for key, rec in by_teams.items():
        joins.setdefault(key, {}).update(rec)
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
    # THE GATE RIDES EVERY ROW WITH A CHECKOUT, an empty list when no gate
    # runs there: the gate window was read, and it said so
    for key, cards in by_gate.items():
        joins.setdefault(key, {})["gate"] = cards
    return {"sections": {"flags": flags, "tasks": tasks_sec,
                         "seats": seats_sec, "lands": lands_sec,
                         "trunk": trunk_sec, "gate": gate_sec,
                         "teams": teams_sec},
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
    lane running, no gate running, and no light set in thirty days? All of
    them, or it stays up.

    Activity is the newer of the registry's last session and the newest seat
    beat. The backlog must have been READ as zero: a count nobody could read,
    or one past its bound, is not zero. A light with no recorded time keeps
    the row up, because its age is unknown, not old."""
    if not tasks_ok or not isinstance(rec, dict) or not isinstance(lit, dict):
        return False
    if (join.get("tasks") or {}).get("open") or join.get("running") \
            or join.get("gate"):
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


def _reaged(rec, shift):
    """This project's pipeline record with every age it carries moved on by
    `shift` seconds, on copies: the cached leg is shared by every response.

    THE LEG KEEPS ITS READING FOR MINUTES, AND ITS AGES WITH IT (task/3631,
    walk 2 finding 18). The land pipeline's cards are aged when the leg reads
    `/api/lr` (`_kanban_split`), and the leg is served for up to
    SECTION_LIMIT_S after that, so a card read "5m" on the board while the
    scheduler under it, aged at its own response, read "9m" for the same row.
    Every age here — each card's, each count line's oldest and its rows',
    each land's, each wait's — moves on by the time since the leg read it."""
    if not shift:
        return rec

    def add(v):
        return v + shift if isinstance(v, int) and not isinstance(v, bool) \
            else v

    def card(c):
        return dict(c, age_s=add(c.get("age_s"))) if isinstance(c, dict) \
            else c

    def row(r):
        if isinstance(r, dict) and isinstance(r.get("lr"), dict):
            return dict(r, lr=card(r["lr"]))
        return card(r)

    def line(ln):
        if not isinstance(ln, dict):
            return ln
        out = dict(ln, oldest_age_s=add(ln.get("oldest_age_s")))
        if isinstance(ln.get("rows"), list):
            out["rows"] = [row(r) for r in ln["rows"]]
        return out

    out = dict(rec)
    lanes = rec.get("lanes")
    if isinstance(lanes, dict):
        lanes = dict(lanes)
        for key in ("loops", "loops_cut"):
            if isinstance(lanes.get(key), list):
                lanes[key] = [card(c) for c in lanes[key]]
        for key in ("rehold", "on_main"):
            lanes[key] = line(lanes.get(key))
        if isinstance(lanes.get("collapsed"), list):
            lanes["collapsed"] = [line(c) for c in lanes["collapsed"]]
        out["lanes"] = lanes
    if isinstance(rec.get("landed"), list):
        out["landed"] = [card(r) for r in rec["landed"]]
    if isinstance(rec.get("waits"), list):
        out["waits"] = [dict(line(g), rows=[card(r) for r in g.get("rows")
                                            or ()])
                        if isinstance(g, dict) else g for g in rec["waits"]]
    if isinstance(rec.get("waits_collapsed"), list):
        out["waits_collapsed"] = [line(c) for c in rec["waits_collapsed"]]
    return out


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
    # THE LAND PIPELINE'S AGES ARE THIS RESPONSE'S (task/3631): moved on by
    # the time since its leg read them
    lands = built["sections"].get("lands") or {}
    if lands.get("scope") in joins \
            and isinstance(lands.get("ages_at"), (int, float)):
        joins = dict(joins)
        joins[lands["scope"]] = _reaged(
            joins[lands["scope"]], max(0, int(now - lands["ages_at"])))
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


def _api_work(qs):
    """GET /api/work — the Work page's one reader (task/3643): one card per
    piece of work over the to-do list and THIS board's pipeline reading,
    counted once on the server (`work_model`). No `key`: the page's body;
    `key` (and `rev`, the snapshot the page was drawn from): one card and
    its crossings. Never a 500."""
    from . import work_model                # DEFERRED — the ledgers' reader
    return work_model.api(qs)
