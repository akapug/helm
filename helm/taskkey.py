"""helm.taskkey — the ONE join from a piece of work to the task it serves.

THE GAP (task/3643, slice 2 of the work surface). helm never recorded which
task a work room (a lane) or a dispatch chain was for, so each reader guessed
in its own way: the pair meld keyed its room on a literal `task/N` in the
chain's first row, auto-land's car task and train blame's task comment read
the same, the land board read the lane name alone, and the task sweep took a
lane's trailing number as its task, so `canary-seeded-red-0926` joined
task/926. Train merge lines said "no task" for lanes that plainly served one.

THE KEY IS STORED WHERE THE WORK'S IDENTITY ALREADY LIVES. No new ledger:

- A LANE is its branch, `lane/<lane>`. `helm work claim <lane> --task task/N`
  writes the branch's own git config key, `branch.lane/<lane>.helmTask`, in
  the repository's common config. Git moves the key with a branch rename and
  drops it with `branch -d`; helm's compare-and-delete of a lane ref drops it
  too (`vcs.Git.delete_branch`), and a record whose branch no longer exists
  is never read (`lane_records`), so a reused lane name starts clean. The
  lease is not a record: it lives on tmpfs and is deleted at release.
- A DISPATCH CHAIN is its first row. The first `helm dispatch send|add
  --new-work` writes `task` on that row: `--task task/N`, else the lane's
  record, else the one open task the lane names literally, else the one open
  task the brief names literally (`chain_task`).
- WHOLE OR PART (task/3746): `--whole` beside the task records that the
  lane carries the task's WHOLE ask, so its land closes the task
  (helm/landtask.py): `helm work claim --whole` writes
  `branch.lane/<lane>.helmWhole`, and `dispatch --new-work --whole` writes
  `task_whole` on the chain's first row. Without it a land asks the task's
  room whether the whole ask is done.

THE JOIN (`join`): a stored key, else a literal `task/N` or `task-N` that
names exactly one task, else UNKNOWN. There is no trailing-number rule. Two
different stored keys are UNKNOWN, and the reason names both; two different
literals are UNKNOWN as ambiguous. With a task ledger snapshot (`known`), a
literal must name a row in it.

HISTORY IS A FACT, NEVER RE-JOINED. The task a land served is what was
recorded when it landed (the car's task in the train file, the chain's first
row); today's lane records never re-attribute an old land, since a lane name
can be reused for another task.

LANDED IS REPORTED, AND CLOSES ONLY A WHOLE ASK. A land closes its task only
when its lane recorded that it carries the whole ask (`--whole`): closing at
land without that re-opened two tasks in one night (task/3626). Every other
land asks the task's room whether the whole ask is done (helm/landtask.py).
`lands_by_task` joins every car an auto-land train pushed (DONE, or ABANDONED
by force after its head was measured on trunk) to its task, and
`landed_state` names an open task with landed work LANDED_UNREAD. The task
sweep reports that mark (helm/taskhygiene.py); the task's owner closes it
after re-reading the whole ask.
"""
import collections
import os
import re
import time

from . import projscope, tasks

LANE_KEY = "helmTask"
#: The lane carries its task's WHOLE ask (task/3746): the value is the task.
WHOLE_KEY = "helmWhole"
STORED, LITERAL = "stored", "literal"
LANDED_UNREAD = "landed, whole ask not yet re-read"
NO_TASK = "no task is recorded for this work or named by it"
# git prints the key canonically: section and variable lowercased, the
# subsection (the branch name) as written
_LANE_KEY_RE = re.compile(r"^branch\.lane/(.+)\.helmtask$")
# A LANE THAT WENT AHEAD OF A BIGGER LEVER (task/3821): `helm work claim
# --task T --because R` records the tasks T skipped and the reason beside the
# lane's task, on the same branch, so they go with it. `--start-anyway R`
# records the started work it went ahead of (FINISH FIRST) the same way.
LEVER_KEYS = ("helmLeverSkipped", "helmLeverBecause")
START_KEYS = ("helmStartAnywayOver", "helmStartAnyway")
_EXTRA_KEY_RE = re.compile(
    r"^branch\.lane/(.+)\.(helmleverskipped|helmleverbecause"
    r"|helmstartanywayover|helmstartanyway)$")
_KEY_RES = {LANE_KEY: _LANE_KEY_RE,
            WHOLE_KEY: re.compile(r"^branch\.lane/(.+)\.helmwhole$")}

Key = collections.namedtuple("Key", "task via why cited landed")
Key.__doc__ = """One answer of the join.

task    `task/N`, or None for UNKNOWN
via     STORED or LITERAL, or None
why     the reason the answer is UNKNOWN, else None
cited   every distinct task the literal evidence names, in order
landed  the task's lands from a `lands_by_task` index, () for none, or
        None when the caller did not ask"""

_COMMON = {}    # repository path -> its git common dir
_RECORDS = {}   # (config path, key) -> (stat stamp, {lane: frozenset})

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_COMMON": (
        "a repository path's git common dir, which does not change while the "
        "path names that repository; each test's repository is a new path"),
    "_RECORDS": (
        "a config file's lane records, keyed by its path and the key read, "
        "and re-read when its mtime, size or inode changes, so a stale entry "
        "is never served"),
}


def lane_name(lane):
    """The bare lane of a lane, a `lane/<lane>` branch or a full ref."""
    name = str(lane or "").strip()
    if name.startswith("refs/heads/"):
        name = name[len("refs/heads/"):]
    elif name.startswith("refs/remotes/"):
        name = name[len("refs/remotes/"):].partition("/")[2]
    return name[len("lane/"):] if name.startswith("lane/") else name


def _common_dir(repo):
    """(common dir or None, why). None with no reason when the path does
    not exist: a repository this host cannot see holds no record here."""
    repo = str(repo or "")
    if not repo or not os.path.exists(repo):
        return None, None
    if repo in _COMMON:
        return _COMMON[repo], None
    from .work import _lanes
    rc, out, err = _lanes._git(repo, "rev-parse", "--path-format=absolute",
                               "--git-common-dir")
    if rc != 0 or not out:
        return None, "git could not name the repository at %s (%s)" % (
            repo, err or "rc %s" % rc)
    _COMMON[repo] = out.strip()
    return _COMMON[repo], None


def live_lanes(repo):
    """({lane names}, why) — branch names still under refs/heads/lane/.
    Unlike lane_records, includes lanes that never stored a helmTask key."""
    common, why = _common_dir(repo)
    if why or common is None:
        return set(), why
    from .work import _lanes
    rc, out, err = _lanes._git(common, "for-each-ref", "--format=%(refname)",
                               "refs/heads/lane/")
    if rc != 0:
        return set(), "the lane branches could not be listed (%s)" % (
            err or "rc %s" % rc)
    return {ref[len("refs/heads/lane/"):] for ref in out.splitlines()
            if ref.startswith("refs/heads/lane/")}, None


def lane_records(repo, key=LANE_KEY):
    """({lane: frozenset(recorded values)}, why) — every lane record of `key`
    in the repository whose branch `lane/<lane>` exists. The config is read
    once per change of its file; the branches are listed on every call,
    because a record left by a deleted branch (an older helm's `update-ref
    -d`, a hand delete) must never reach a reused lane name."""
    common, why = _common_dir(repo)
    if why or common is None:
        return {}, why
    path = os.path.join(common, "config")
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        return {}, "%s could not be read (%s)" % (path, exc)
    stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
    from .work import _lanes
    pattern = _KEY_RES[key]
    hit = _RECORDS.get((path, key))
    if hit and hit[0] == stamp:
        records = hit[1]
    else:
        rc, out, err = _lanes._git(common, "config", "--file", path,
                                   "--get-regexp", pattern.pattern)
        if rc not in (0, 1):
            return {}, "%s could not be read (%s)" % (
                path, err or "rc %s" % rc)
        found = {}
        for line in out.splitlines() if rc == 0 else ():
            name, _, value = line.partition(" ")
            m = pattern.match(name)
            if m:
                found.setdefault(m.group(1), set()).add(value.strip())
        records = {lane: frozenset(values) for lane, values in found.items()}
        _RECORDS[(path, key)] = (stamp, records)
    if not records:
        return records, None
    live, why = live_lanes(repo)
    if why:
        return {}, why
    return {lane: v for lane, v in records.items() if lane in live}, None


def lane_wholes(repo):
    """({lane: frozenset(tasks)}, why) — the lanes that record carrying the
    WHOLE ask of a task (`helm work claim --task task/N --whole`), read as
    `lane_records` reads the task key."""
    return lane_records(repo, WHOLE_KEY)


PRESENT, ABSENT = "present", "absent"


def lane_branch_state(common, lane):
    """(PRESENT or ABSENT, None), or (None, why) when the ref cannot be read.

    `git show-ref --exists` alone separates the three: rc 0 present, rc 2
    absent, and rc 1 a LOOKUP ERROR (a corrupt loose ref, a refs directory it
    cannot read). `--verify --quiet` and `rev-parse` answer rc 1 for all
    three, so neither may decide absence. Anything but rc 2 is never absent,
    because an absent branch is the one answer that licenses erasing its
    record."""
    from .work import _lanes
    ref = "refs/heads/lane/%s" % lane
    rc, _out, err = _lanes._git(common, "show-ref", "--exists", ref)
    if rc in (0, 2):
        return (PRESENT if rc == 0 else ABSENT), None
    return None, "the ref %s could not be read (%s)" % (
        ref, (err or "rc %s" % rc).strip()[:160])


def forget_lane(repo, lane):
    """(forgot, why) — drop any task record for a lane that has no branch.

    A record left by a deleted branch (a config its delete could not clean,
    or an older helm's `update-ref -d`) is stale by definition; `helm work
    claim` calls this before it cuts a fresh `lane/<lane>`, so the lane
    name's next use never inherits another task. A lane whose branch exists
    is left alone, and one whose ref cannot be read erases nothing and says
    why (the claim then refuses)."""
    lane = lane_name(lane)
    common, why = _common_dir(repo)
    if why or common is None:
        return False, why
    from .work import _lanes
    state, why = lane_branch_state(common, lane)
    if why:
        return False, why
    if state == PRESENT:
        return False, None
    forgot = False
    for key in (LANE_KEY, WHOLE_KEY) + LEVER_KEYS + START_KEYS:
        rc, _out, err = _lanes._git(
            common, "config", "--file", os.path.join(common, "config"),
            "--unset-all", "branch.lane/%s.%s" % (lane, key))
        if rc not in (0, 5):
            return forgot, ("git config could not drop the stale record (%s)"
                            % (err or rc))
        forgot = forgot or (rc == 0 and key in (LANE_KEY, WHOLE_KEY))
    return forgot, None


def open_task(token, known=None):
    """(task id, why) — the id of an OPEN task in the ledger, or a refusal
    that names it."""
    tid = tasks.normalize_id(token)
    if not tid:
        return None, "%r is not a task id (task/N)" % (token,)
    if known is None:
        known, bad = tasks.snapshot(strict=True)
        if bad:
            return None, ("the task ledger could not be read (%s), so %s is "
                          "unverified" % (bad, tid))
    row = (known or {}).get(tid)
    if not isinstance(row, dict):
        return None, "%s is not in the task ledger" % tid
    if row.get("status") not in tasks.OPEN_STATUSES:
        return None, "%s is %s, not open work" % (tid, row.get("status")
                                                 or "of no status")
    return tid, None


def record_lane(repo, lane, task, key=LANE_KEY):
    """(written, why) — record `task` as the task lane `lane` serves (or,
    with WHOLE_KEY, as the task whose whole ask it carries). Recording the
    same task again writes nothing; a lane that records a different task is
    refused, because a lane serves one task."""
    lane = lane_name(lane)
    records, why = lane_records(repo, key)
    if why:
        return False, ("the lane record could not be read (%s), so nothing "
                       "was recorded" % why)
    common, why = _common_dir(repo)
    if why or common is None:
        return False, why or "no repository at %s" % repo
    from .work import _lanes
    state, why = lane_branch_state(common, lane)
    if why:
        return False, "%s, so nothing was recorded" % why
    if state != PRESENT:
        return False, ("lane %s has no branch lane/%s, so there is nothing "
                       "to record its task on; claim the lane first"
                       % (lane, lane))
    have = records.get(lane) or frozenset()
    if have == {task}:
        return False, None
    if have:
        return False, ("lane %s already records %s; a lane serves one task, "
                       "so claim a new lane for %s"
                       % (lane, ", ".join(sorted(have)), task))
    rc, _out, err = _lanes._git(
        common, "config", "--file", os.path.join(common, "config"),
        "branch.lane/%s.%s" % (lane, key), task)
    if rc != 0:
        return False, "git config refused the record (%s)" % (err or rc)
    return True, None


def _record_keys(repo, lane, keys, values):
    """(written, why) — write `keys` = `values` on lane `lane`'s branch. The
    branch must exist; `record_lane` has written its task."""
    lane = lane_name(lane)
    common, why = _common_dir(repo)
    if why or common is None:
        return False, why or "no repository at %s" % repo
    from .work import _lanes
    for key, value in zip(keys, values):
        rc, _out, err = _lanes._git(
            common, "config", "--file", os.path.join(common, "config"),
            "branch.lane/%s.%s" % (lane, key), value)
        if rc != 0:
            return False, "git config refused the record (%s)" % (err or rc)
    return True, None


def record_lever(repo, lane, skipped, because):
    """(written, why) — record on lane `lane` the tasks its task went ahead
    of (`skipped`, ids) and the reason given (`because`, one line)."""
    return _record_keys(repo, lane, LEVER_KEYS, (" ".join(skipped), because))


def record_start(repo, lane, over, reason):
    """(written, why) — record on lane `lane` the started work it went ahead
    of (`over`, ids) and the reason given (`reason`, one line)."""
    return _record_keys(repo, lane, START_KEYS, (" ".join(over), reason))


def _lane_extras(repo):
    """({lane: {key: value}}, records, why) — the lever and start keys of
    every lane whose branch exists and whose task is recorded."""
    records, why = lane_records(repo)
    if why or not records:
        return {}, records, why
    common, why = _common_dir(repo)
    if why or common is None:
        return {}, records, why
    from .work import _lanes
    path = os.path.join(common, "config")
    rc, out, err = _lanes._git(common, "config", "--file", path,
                               "--get-regexp", _EXTRA_KEY_RE.pattern)
    if rc not in (0, 1):
        return {}, records, "%s could not be read (%s)" % (
            path, err or "rc %s" % rc)
    found = {}
    for line in out.splitlines() if rc == 0 else ():
        key, _, value = line.partition(" ")
        m = _EXTRA_KEY_RE.match(key)
        if m and records.get(m.group(1)):
            found.setdefault(m.group(1), {})[m.group(2)] = value.strip()
    return found, records, None


def lever_records(repo):
    """({lane: {"task", "skipped", "because"}}, why) — every lane whose
    branch exists, whose task is recorded, and which records a lever skip."""
    found, records, why = _lane_extras(repo)
    return {lane: {"task": sorted(records[lane])[0],
                   "skipped": got.get("helmleverskipped", "").split(),
                   "because": got.get("helmleverbecause") or None}
            for lane, got in found.items()
            if {"helmleverskipped", "helmleverbecause"} & set(got)}, why


def start_records(repo):
    """({lane: {"task", "over", "reason"}}, why) — every lane whose branch
    exists, whose task is recorded, and which went ahead of started work."""
    found, records, why = _lane_extras(repo)
    return {lane: {"task": sorted(records[lane])[0],
                   "over": got.get("helmstartanywayover", "").split(),
                   "reason": got.get("helmstartanyway") or None}
            for lane, got in found.items()
            if {"helmstartanywayover", "helmstartanyway"} & set(got)}, why


def pick_ledgers():
    """(dispatch snapshot, task snapshot, why) — ONE read of each ledger for
    one pick (task/3821): the leverage check, the finish-first census and
    its line all take these. `why` names the ledger that could not be read
    (the task ledger strictly, as a decision reads it), and then neither
    check runs; the snapshot it could not read is {}."""
    from . import dispatches
    current, bad = dispatches.snapshot()
    if bad:
        return {}, {}, "the dispatch ledger could not be read (%s)" % bad
    known, bad = tasks.snapshot(strict=True)
    if bad:
        return current or {}, {}, ("the task ledger could not be read (%s)"
                                   % bad)
    return current or {}, known, None


def live_tasks(repo, current=None):
    """(frozenset of task ids with live work, why) — a lane record in `repo`
    or an open dispatch chain (open or held) whose first row records the
    task. `current` is a dispatch snapshot, read when not given. A record or
    ledger that cannot be read answers why, and no set is claimed."""
    records, why = lane_records(repo)
    if why:
        return frozenset(), "the lane records could not be read (%s)" % why
    from . import dispatches, query
    if current is None:
        current, bad = dispatches.snapshot()
        if bad:
            return frozenset(), ("the dispatch ledger could not be read (%s)"
                                 % bad)
    live = {tasks.normalize_id(v) or v
            for values in records.values() for v in values}
    for row in (current or {}).values():
        if query.query_is_open(row):
            task = (first_row(row, current) or {}).get("task")
            if task:
                live.add(task)
    return frozenset(live), None


# FINISH FIRST (task/3821, owner: "why dont we ever finish anything we
# start"). A seat that starts new work while its started work sits unlanded
# is told, once per session and item, the oldest highest-leverage
# item, and the work goes ahead; --start-anyway records why, and with none the skip is
# recorded as tasks.NO_REASON. The integrator answers for the fleet.
FINISH_MARK = "FINISH FIRST"
FINISH_UNKNOWN_MARK = "FINISH FIRST CHECK UNKNOWN"
START_FLAG = "--start-anyway"
#: How long a started lane may go with no commit, room write or dispatch on it
#: before it counts as sitting unlanded: one default lane lease
#: (work._common.DEFAULT_TTL). Reading a lane and gating it take an hour
#: or two, so a shorter bound would stop a seat whose lane is only waiting on
#: a reader.
FINISH_FIRST_IDLE_S = 4 * 3600
#: How long a row may be held SOURCE-CLEAN before it counts as waiting to
#: land: an auto-land train lands a green hold within minutes, so a hold
#: older than an hour is one the land loop did not take by itself.
FINISH_FIRST_HELD_S = 3600

Finish = collections.namedtuple("Finish", "seat fleet items unknown")
Finish.__doc__ = """One answer of the finish-first census.

seat     the seat starting new work
fleet    True when that seat is the lander (the integrator), which answers
         for every seat's started work
items    ({id, kind, lane, task, idle_s, tip}, ...), the highest-leverage
         (the served task's rank) first and the oldest first within a rank;
         kind "lane" is a held lane, "held" a row held SOURCE-CLEAN
unknown  why the census could not run, else None"""


def _now():
    """The census clock."""
    return time.time()


def in_flight(repo, seat, lane=None, task=None, current=None, known=None):
    """Finish(seat, fleet, items, unknown) — `seat`'s started work in `repo`
    that sits unlanded, other than `lane` and `task` (the work now starting).

    A LANE is started work when its lease is live and held by `seat` (any
    holder, for the lander), its tip is not on trunk (`lanes_landed`
    UNLANDED; landed, unstarted and unreadable lanes are not counted), and
    nothing moved on it for FINISH_FIRST_IDLE_S: no commit, no write in its
    room, no dispatch row sent on it, and no lease grant or extension (the
    claim's `ts`, `lease_ts`): a lane adopted a minute ago is not 72h idle
    because its last commit is. A lane with an open or held dispatch
    row is WAITING on that row's reader or lander, not stalled, and is not
    counted. A ROW HELD SOURCE-CLEAN in `repo` for FINISH_FIRST_HELD_S is
    started work waiting to land; only the lander can land it, so only the
    lander's census counts it, and it stands in for its lane. A hold whose
    work reached trunk, or whose lane landed, is finished and not counted
    (`_hold_landed`); one whose landing cannot be read IS counted, since a
    false steer is one line and a silent skip hides a stall. The census
    spends ONE deadline (CENSUS_BUDGET_S from its start) on those landing
    proofs, whatever the number of holds; a hold reached after it is spent
    is counted unread. A read that fails answers `unknown` and counts
    nothing."""
    from . import query, seats_lander
    from .work import _gc, _lanes
    me = str(seat or "").casefold()
    lander, lwhy = seats_lander.lander_seat()
    fleet = bool(me) and not lwhy and str(lander or "").casefold() == me
    here = lane_name(lane) if lane else None
    now = _now()
    deadline = _monotonic() + CENSUS_BUDGET_S

    def unknown(why):
        return Finish(seat, fleet, (), why)
    if current is None:
        from . import dispatches
        current, bad = dispatches.snapshot()
        if bad:
            return unknown("the dispatch ledger could not be read (%s)" % bad)
    if known is None:
        known, bad = tasks.snapshot()
        if bad:
            return unknown("the task ledger could not be read (%s)" % bad)
    root = _lanes.find_root(repo) if repo and os.path.exists(repo) else None
    if not root:
        return Finish(seat, fleet, (), None)
    try:
        held = _gc.list_rows(root, gc=False, only_held=True)
    except Exception as exc:                  # noqa: BLE001 — said, not raised
        return unknown("the lane census could not be read (%s: %s)"
                       % (type(exc).__name__, exc))
    records, why = lane_records(root)
    if why:
        return unknown("the lane records could not be read (%s)" % why)
    common = os.path.realpath(_common_dir(root)[0] or root)
    moved, holds, waiting = {}, [], set()
    for row in (current or {}).values():
        name = lane_name(row.get("lane"))
        stamp = tasks.stamp_epoch(row.get("ts"))
        if name and stamp:
            moved[name] = max(moved.get(name, 0), stamp)
        if name and query.query_is_open(row):
            waiting.add(name)
        if row.get("status") == "held" and row.get("source_clean_tip") \
                and query.query_is_open(row) and os.path.realpath(
                    str(row.get("repo_id") or "")) == common:
            holds.append(row)
    items = {}
    for r in held:
        verdict = r.get("landed") or {}
        if r["lane"] == here or r["lane"] in waiting \
                or verdict.get("state") != _gc.LANE_UNLANDED \
                or not (fleet or str(r.get("holder") or "").casefold() == me):
            continue
        rc, out, _err = _lanes._git(root, "log", "-1", "--format=%ct",
                                    verdict.get("tip") or r["branch"])
        if rc != 0 or not out.strip().isdigit():
            continue
        stamps = [int(out.strip()), moved.get(r["lane"], 0),
                  tasks.stamp_epoch(r.get("lease_ts")) or 0]
        if r.get("dirty"):
            room = _lanes._room_status(r["path"])
            if room.get("wrote_ago") is None or room.get("clock_skew") \
                    or room.get("unknown"):
                continue
            stamps.append(now - room["wrote_ago"])
        served = sorted(records.get(r["lane"]) or ())[:1]
        idle = now - max(stamps)
        if idle >= FINISH_FIRST_IDLE_S and (not task or served != [task]):
            items[r["lane"]] = {"id": "lane/" + r["lane"], "kind": "lane",
                                "lane": r["lane"], "task": (served or [None])[0],
                                "idle_s": idle, "tip": verdict.get("tip")}
    holds = [row for row in holds if fleet
             and lane_name(row.get("lane")) != here
             and not (task and (first_row(row, current) or {}).get("task")
                      == task)
             and now - (tasks.stamp_epoch(row.get("hold_ts")) or now)
             >= FINISH_FIRST_HELD_S]
    lanes = _gc.lanes_landed(root, [lane_name(r.get("lane")) for r in holds])
    # ONE CENSUS DEADLINE: landreq's own derive budget is armed from it for
    # every proof below, and `_hold_landed` bounds its cherry by what is left.
    with projscope.scope():
        if holds:
            from . import landreq
            landreq.arm_derive_budget(max(0.0, deadline - _monotonic()))
        for row in holds:
            name = lane_name(row.get("lane"))
            served = (first_row(row, current) or {}).get("task")
            since = tasks.stamp_epoch(row.get("hold_ts"))
            if (lanes.get(name) or {}).get("state") == _gc.LANE_LANDED \
                    or _hold_landed(row, deadline):
                continue
            items[name] = {"id": "dispatch:" + str(row["id"])[:12],
                           "kind": "held", "lane": name, "task": served,
                           "idle_s": now - since,
                           "tip": row["source_clean_tip"]}
    return Finish(seat, fleet, tuple(sorted(items.values(), key=lambda i: (
        tasks._rank(known.get(i["task"]) or {}), -i["idle_s"]))), None)


#: The one deadline a finish-first census spends proving its holds landed:
#: every hold's landing proof and `git cherry` fallback share it, so N holds
#: cost one budget, not N.
CENSUS_BUDGET_S = 20


def _monotonic():
    """The census deadline's clock."""
    return time.monotonic()


def _hold_landed(row, deadline):
    """True only when the work a SOURCE-CLEAN hold names is PROVEN on its
    repository's trunk: landreq's one landing proof (ancestry, then patch
    identity) against the trunk its close would read, and, when that reads
    unknown (its patch index does not cover every trunk commit), one
    `git cherry` of that tip against the same pinned trunk, bounded by what
    is left of the census `deadline` (a `_monotonic` instant). A hold
    reached after the deadline is not asked. Anything that cannot be read
    is False, so the hold is counted."""
    from . import landreq
    if deadline - _monotonic() <= 0:
        return False
    try:
        gitdir, err = landreq._close_repo(row, None)
        if err:
            return False
        _ref, pinned, _target, err = landreq._close_trunk(row, gitdir, None)
        if err:
            return False
        proof = landreq._landing_proof(gitdir, row["source_clean_tip"], pinned)
    except Exception:                         # noqa: BLE001 — unread: counted
        return False
    if proof in ("ancestor", "patch-equivalent", "absent"):
        return proof != "absent"
    left = deadline - _monotonic()
    return left > 0 and bool(_cherry_landed(
        gitdir, pinned, row["source_clean_tip"], timeout=left))


def _cherry_landed(gitdir, trunk, tip, timeout=CENSUS_BUDGET_S):
    """True when every commit `tip` has beyond `trunk` has an equivalent
    patch on it (`git cherry` prints no "+" line), False when one does not,
    None when git cannot say within `timeout` seconds."""
    from .work import _lanes
    rc, out, _err = _lanes._git(gitdir, "cherry", trunk, tip,
                                timeout=timeout)
    if rc != 0:
        return None
    return not any(line.startswith("+") for line in out.splitlines())


def _span(seconds):
    return ("%dh" % (seconds // 3600) if seconds >= 3600
            else "%dm" % (seconds // 60))


def finish_line(finish, known=None):
    """The one line a door prints for `finish`, or None."""
    if finish.unknown:
        return ("%s: %s — the check did not run, so this goes ahead"
                % (FINISH_UNKNOWN_MARK, finish.unknown))
    if not finish.items:
        return None
    top = finish.items[0]
    rank = ((known or {}).get(top["task"]) or {}).get("priority")
    what = ("lane %s, " % top["lane"] if top["kind"] == "held" else "") + \
        (top["task"] or "no task") + (", %s" % rank if rank else "")
    state = ("held SOURCE-CLEAN at %s, waiting to land" % str(top["tip"])[:12]
             if top["kind"] == "held" else
             "unlanded, with no commit, write or dispatch")
    more = len(finish.items) - 1
    return ('%s: %s started work sits unlanded — %s (%s) %s for %s%s. Finish '
            'it first if you can; this goes ahead, recorded as "%s" (next '
            'time, say why with %s "<one line>").'
            % (FINISH_MARK, "the fleet's" if finish.fleet else "your",
               top["id"], what, state, _span(top["idle_s"]),
               "; %d more started item%s" % (more, "s"[:more != 1])
               if more else "", tasks.NO_REASON, START_FLAG))


#: Which steer lines were said, so each is said ONCE per context: keyed by
#: the harness session id (the acting seat when no session is set), the
#: check, the chosen task (the lane, for a row with no task) and the id the
#: line names first (`steer_lead`). Entries older than STEER_KEEP_S are
#: dropped before a lookup. The FINISH FIRST line does not name the chosen
#: task, so its key leaves the task out: one stalled item is said once per
#: context, not once for every new pick while it stays stalled.
STEER_STATE = "lever-steer.json"
STEER_KEEP_S = 7 * 86400


def steer_lead(result):
    """The id a check's steer names first: a Lever's first skipped task, a
    Finish's first unlanded item; "" when it found nothing."""
    found = getattr(result, "skipped", None) or getattr(result, "items", None)
    if not found:
        return ""
    lead = found[0]
    return str(lead.get("id") if isinstance(lead, dict) else lead[0])


def steer_once(seat, check, key, result=None):
    """True the first time the steer for (context, `check`, `key`, the id it
    names first) is asked for, and marks it said; False after, for
    STEER_KEEP_S. The named id is in the key (`steer_lead(result)`), so a new
    lever or a new unlanded item on a second pick of the same task speaks. An entry older
    than that is dropped BEFORE the lookup, so an aged one quiets nothing. A
    state that cannot be read or written says the line again rather than
    never. For the "finish" check `key` is not in the token: its line
    names the stalled item and not the pick, so a second pick of a different
    task while the same item is stalled says nothing new."""
    from . import home, pk
    path = os.path.join(home.global_dir(), ".state", STEER_STATE)
    token = "|".join(str(p) for p in (home.session_id() or seat, check,
                                       "" if check == "finish" else key,
                                       steer_lead(result)))
    now = _now()
    try:
        said = pk.read_json(path, {})
        said = {k: v for k, v in (said.items() if isinstance(said, dict)
                                  else ())
                if isinstance(v, (int, float)) and now - v < STEER_KEEP_S}
        if token in said:
            return False
        said[token] = now
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.write_json(path, said)
    except (OSError, ValueError):
        return True
    return True


def finish_over(finish):
    """The ids of `finish`'s items, in order."""
    return [i["id"] for i in finish.items]


def choice_comment(task, where, lever=None, start=None, by=None):
    """(written, why) — ONE comment on `task` saying what `where` (a lane or
    a dispatch row) took it ahead of and the reason the seat GAVE, so that
    reason outlives the lane's branch. `lever` and `start` are (ids, reason)
    or None. A choice with no reason given (`tasks.NO_REASON`) stays on the
    lane or the row and is not commented: a comment per pick with nothing
    to say is noise on the task. A task that already carries a comment from
    `where` gets no second one, so a retry writes nothing."""
    parts = []
    for got, flag in ((lever, tasks.LEVER_FLAG), (start, START_FLAG)):
        if got and got[1] != tasks.NO_REASON:
            parts.append('ahead of %s (%s): "%s"'
                         % (tasks.named(got[0]), flag, got[1]))
    if not task or not parts:
        return False, None
    head = "%s took %s " % (where, task)
    row = tasks.get(task) or {}
    if any(str(c.get("text") or "").startswith(head)
           for c in row.get("comments") or () if isinstance(c, dict)):
        return False, None
    _row, why = tasks.comment(task, head + "; ".join(parts), by=by)
    return why is None, why


def lane_choice(repo, lane, task):
    """None, or what lane `lane` recorded when it was claimed for `task`.

    A lane that records `task` was weighed at its claim (`helm work claim
    --task`), so a first dispatch on it is not weighed again. The dict holds
    the fields to copy onto that row: the skipped ids and the reason
    (`lever_skipped`, `lever_because`), the started work it went ahead of and
    why (`start_anyway_over`, `start_anyway`), and `reasons_from` naming the
    lane; it is empty when the claim went ahead of nothing. None when the
    lane does not record `task` or its records cannot be read, and the row
    is then weighed."""
    if not task:
        return None
    found, records, why = _lane_extras(repo)
    name = lane_name(lane)
    if why or task not in (records.get(name) or ()):
        return None
    got, out = found.get(name) or {}, {}
    if got.get("helmleverbecause"):
        out.update(lever_skipped=got.get("helmleverskipped", "").split(),
                   lever_because=got["helmleverbecause"])
    if got.get("helmstartanyway"):
        out.update(start_anyway_over=got.get("helmstartanywayover",
                                             "").split(),
                   start_anyway=got["helmstartanyway"])
    if out:
        out["reasons_from"] = "lane/" + name
    return out


def record_whole(repo, lane, task):
    """(written, why) — record that lane `lane` carries the WHOLE ask of
    `task` (task/3746), on the lane branch beside its task record."""
    return record_lane(repo, lane, task, key=WHOLE_KEY)


def first_row(row, current=None):
    """The chain's FIRST row, or None when it cannot be read. A row that
    roots its own chain, or predates chains, is its own first row."""
    from . import dispatches
    row = row or {}
    root_id = str(row.get("chain_root") or "")
    if not root_id or root_id == str(row.get("id") or ""):
        return row
    if root_id == dispatches.CHAIN_UNKNOWN:
        return None
    if current is None:
        current = dispatches.snapshot()[0] or {}
    return current.get(root_id)


def _cited(texts):
    nums = {int(n) for text in texts
            for n in tasks._CITED.findall(str(text or ""))}
    return tuple("task/%d" % n for n in sorted(nums))


def join(lane=None, row=None, current=None, repo=None, lanes=None,
         recorded=(), known=None, lands=None):
    """Key(task, via, why, cited, landed) — the task this work serves.

    lane      a lane, a `lane/<lane>` branch or a full ref
    row       any dispatch row of the chain; its FIRST row decides (a later
              round with a renamed lane stays with the chain), and supplies
              the lane and repository when they are not given
    current   the dispatch snapshot the first row is read from
    repo      the repository whose lane record is read
    lanes     a `lane_records` map already read, or False to read none
              (the pair meld names its room from the chain alone)
    recorded  task ids a record the caller holds names for this work (a
              train car's task, a merge subject's own task)
    known     a task ledger snapshot; a literal must name a row in it
    lands     a `lands_by_task` index, to fill `landed`

    Stored keys (the first row's `task`, the lane record, `recorded`) win
    over literals. A lane record that cannot be read makes the answer
    UNKNOWN: it may hold a different task."""
    stored, texts, lane_of = [], [], None
    if row is not None:
        first = first_row(row, current)
        if first is None:
            return Key(None, None, "the chain of row %s is UNKNOWN or "
                       "unreadable" % str(row.get("id") or "?")[:12], (),
                       None)
        if first.get("task"):
            stored.append(("the chain's first row", first["task"]))
        # THE APPEND-ONLY ATTACH (task/4000): a chain minted taskless gains
        # its task as a LATER ledger event on the first row, never as a
        # rewrite of the opener. Both are stored keys of that one row, so a
        # chain carrying both (which the fold refuses — an attach never
        # lands on a row that names a task) would read CONTRADICTORY here
        # rather than silently prefer one.
        if first.get("attached_task"):
            stored.append(("the chain's attached task",
                           first["attached_task"]))
        texts += [first.get("lane"), first.get("note")]
        lane_of = first.get("lane")
        repo = repo or first.get("repo_id") or first.get("repo_root")
    if lane is not None:
        texts.append(lane)
        lane_of = lane
    if lane_of and lanes is not False:
        if lanes is None:
            lanes, why = lane_records(repo)
            if why:
                return Key(None, None, "the record of lane %s could not be "
                           "read (%s)" % (lane_name(lane_of), why),
                           _cited(texts), None)
        stored += [("lane %s's record" % lane_name(lane_of), value)
                   for value in sorted(lanes.get(lane_name(lane_of), ()))]
    stored += [("the record", value) for value in recorded or () if value]
    cited = _cited(texts)
    by_task = {}
    for source, value in stored:
        tid = tasks.normalize_id(value)
        if tid is None:
            return Key(None, None, "%s holds %r, which is not a task id"
                       % (source, value), cited, None)
        by_task.setdefault(tid, source)
    if len(by_task) > 1:
        return Key(None, None, "contradictory: %s" % "; ".join(
            "%s records %s" % (source, tid)
            for tid, source in sorted(by_task.items())), cited, None)
    if by_task:
        tid = next(iter(by_task))
        return Key(tid, STORED, None, cited, _landed(tid, lands))
    if len(cited) > 1:
        return Key(None, None, "the work names %d tasks (%s), so which one "
                   "it serves is ambiguous" % (len(cited), ", ".join(cited)),
                   cited, None)
    if not cited:
        return Key(None, None, NO_TASK, cited, None)
    tid = cited[0]
    if known is not None and tid not in known:
        return Key(None, None, "the work names %s, which is not in the task "
                   "ledger" % tid, cited, None)
    return Key(tid, LITERAL, None, cited, _landed(tid, lands))


def car_key(row, current=None, lane=None, repo=None):
    """The landed/ejected row's chain task AND its own lane proof.

    A continuation's first row defines its chain, but a later car cannot
    borrow that task when its own lane or row proves another one. An unreadable
    or contradictory own record is UNKNOWN, never the chain's task.
    """
    chain = join(row=row, current=current)
    own = join(lane=lane or row.get("lane"),
               repo=repo or row.get("repo_root") or row.get("repo_id"),
               recorded=(row.get("task"),))
    if own.why and own.why != NO_TASK:
        return own
    if chain.why and chain.why != NO_TASK:
        return chain
    if chain.task and own.task and chain.task != own.task:
        return Key(None, None, "row %s's chain serves %s but car lane %s "
                   "proves %s" % (str(row.get("id") or "?")[:12],
                                  chain.task, lane_name(lane or row.get("lane")),
                                  own.task),
                   tuple(sorted(set(chain.cited + own.cited))), None)
    return own if own.task else chain


def _landed(tid, lands):
    return None if lands is None else tuple(lands.get(tid) or ())


def chain_task(explicit=None, new_work=False, lane=None, brief=None,
               repo=None, known=None):
    """(task or None, refusal or None) — the task a dispatch row records.

    Only a chain's FIRST row records one (`new_work`); a later round carries
    its chain's. EVERY SOURCE passes the same `open_task` check. `explicit`
    (`--task`) must name an OPEN task and agree with the lane's record.
    Without it the row takes the lane's record, and a record whose task is
    not open REFUSES by name: the lane served that task, new work in it is
    other work, and it needs a lane of its own. Else the one open task the
    lane names literally, else the one open task the brief names literally;
    a literal that is not open, or two named, records nothing and refuses
    nothing."""
    if explicit and not new_work:
        return None, ("--task names the task of a NEW chain (--new-work); a "
                      "--supersedes row keeps its chain's first row's task")
    if not new_work:
        return None, None
    records, why = lane_records(repo)
    have = records.get(lane_name(lane)) or frozenset() if not why \
        else frozenset()
    if explicit:
        tid, err = open_task(explicit, known)
        if err:
            return None, "--task: %s" % err
        if why:
            return None, ("--task: the record of lane %s could not be read "
                          "(%s), so whether it names another task is UNKNOWN"
                          % (lane_name(lane), why))
        if have and have != {tid}:
            return None, ("--task %s: lane %s records %s (helm work claim "
                          "--task); a lane serves one task"
                          % (tid, lane_name(lane), ", ".join(sorted(have))))
        return tid, None
    if len(have) == 1:
        tid, err = open_task(next(iter(have)), known)
        if err:
            return None, ("lane %s records its task (helm work claim --task "
                          "task/N --whole|--part), and %s; new work needs a "
                          "lane of its own"
                          % (lane_name(lane), err))
        return tid, None
    if have or why:
        return None, None
    for text in (lane, brief):
        cited = _cited([text])
        if len(cited) == 1:
            tid, err = open_task(cited[0], known)
            return (None if err else tid), None
        if cited:
            return None, None
    return None, None


def _pushed(st):
    """Did this archived auto-land train put its head on trunk? DONE did; an
    ABANDONED one did when its abandon was forced over a head it measured
    on the declared trunk (`autoland.abandon` records pushed "yes"). A head
    that only MAY be there is not counted."""
    from . import autoland
    if st.get("state") == autoland.DONE:
        return True
    gone = st.get("abandoned") or {}
    return st.get("state") == autoland.ABANDONED and gone.get("forced") \
        and gone.get("pushed") == "yes"


def lands_by_task(root, current=None, known=None):
    """({task: (land, ...)}, why) — every car an auto-land train pushed,
    joined to the task recorded for it: the car's task in the train file and
    its chain's first row, else a literal in its lane or first row. Today's
    lane records are never read (history is a fact), and a car whose merge
    said task UNKNOWN (`task_unknown`) proves no task. A land: {land, train,
    head, ts, lane, row, via, abandoned}."""
    from . import autoland, dispatches
    if current is None:
        current, bad = dispatches.snapshot()
        if bad:
            return {}, "the dispatch ledger could not be read (%s)" % bad
    out = {}
    for st in autoland.archived(root):
        if not _pushed(st):
            continue
        for car in st.get("cars") or ():
            if car.get("task_unknown"):
                continue        # the merge said task UNKNOWN: never re-joined
            row = (current or {}).get(car.get("id"))
            key = join(lane=car.get("lane"), row=row, current=current,
                       lanes=False, recorded=(car.get("task"),),
                       known=known)
            if key.task:
                out.setdefault(key.task, []).append({
                    "land": st.get("land"),
                    "train": st.get("name") or st.get("train"),
                    "head": st.get("head"), "ts": st.get("pushed_ts"),
                    "lane": car.get("lane"), "row": car.get("id"),
                    "via": key.via,
                    "abandoned": st.get("state") != autoland.DONE})
    return {tid: tuple(rows) for tid, rows in out.items()}, None


def row_tasks(rows, current):
    """{row id: Key} — the one join for many dispatch rows, each repository's
    lane records read once. A repository whose records cannot be read makes
    its rows' answers UNKNOWN with the reason (`join`)."""
    records = {}
    out = {}
    for rid, row in (rows or {}).items():
        first = first_row(row, current) or row or {}
        repo = first.get("repo_id") or first.get("repo_root")
        # a malformed binding (not a path) reads no record and never raises:
        # the land projection this feeds must keep the row on the board
        repo = repo if isinstance(repo, str) else None
        if repo not in records:
            records[repo] = lane_records(repo)
        lanes, why = records[repo]
        out[rid] = join(row=row, current=current, repo=repo,
                        lanes=None if why else lanes)
    return out


def landed_state(task_row, lands):
    """LANDED_UNREAD for an open task whose work has landed, else None."""
    row = task_row or {}
    if row.get("status") in tasks.OPEN_STATUSES and lands:
        return LANDED_UNREAD
    return None
