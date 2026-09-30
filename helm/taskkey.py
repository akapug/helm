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

THE JOIN (`join`): a stored key, else a literal `task/N` or `task-N` that
names exactly one task, else UNKNOWN. There is no trailing-number rule. Two
different stored keys are UNKNOWN, and the reason names both; two different
literals are UNKNOWN as ambiguous. With a task ledger snapshot (`known`), a
literal must name a row in it.

HISTORY IS A FACT, NEVER RE-JOINED. The task a land served is what was
recorded when it landed (the car's task in the train file, the chain's first
row); today's lane records never re-attribute an old land, since a lane name
can be reused for another task.

LANDED IS REPORTED, NEVER CLOSED. A task is not closed when its work lands:
closing at land without a re-read of the whole ask re-opened two tasks in one
night (task/3626). `lands_by_task` joins every car an auto-land train pushed
(DONE, or ABANDONED by force after its head was measured on trunk) to its
task, and `landed_state` names an open task with landed work LANDED_UNREAD.
The task sweep reports that mark (helm/taskhygiene.py); the task's owner
closes it after re-reading the whole ask.
"""
import collections
import os
import re

from . import tasks

LANE_KEY = "helmTask"
STORED, LITERAL = "stored", "literal"
LANDED_UNREAD = "landed, whole ask not yet re-read"
NO_TASK = "no task is recorded for this work or named by it"
# git prints the key canonically: section and variable lowercased, the
# subsection (the branch name) as written
_LANE_KEY_RE = re.compile(r"^branch\.lane/(.+)\.helmtask$")

Key = collections.namedtuple("Key", "task via why cited landed")
Key.__doc__ = """One answer of the join.

task    `task/N`, or None for UNKNOWN
via     STORED or LITERAL, or None
why     the reason the answer is UNKNOWN, else None
cited   every distinct task the literal evidence names, in order
landed  the task's lands from a `lands_by_task` index, () for none, or
        None when the caller did not ask"""

_COMMON = {}    # repository path -> its git common dir
_RECORDS = {}   # config path -> (stat stamp, {lane: frozenset(values)})

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_COMMON": (
        "a repository path's git common dir, which does not change while the "
        "path names that repository; each test's repository is a new path"),
    "_RECORDS": (
        "a config file's lane records, keyed by its path and re-read when its "
        "mtime, size or inode changes, so a stale entry is never served"),
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


def lane_records(repo):
    """({lane: frozenset(recorded values)}, why) — every lane record in the
    repository whose branch `lane/<lane>` exists. The config is read once per
    change of its file; the branches are listed on every call, because a
    record left by a deleted branch (an older helm's `update-ref -d`, a hand
    delete) must never reach a reused lane name."""
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
    hit = _RECORDS.get(path)
    if hit and hit[0] == stamp:
        records = hit[1]
    else:
        rc, out, err = _lanes._git(common, "config", "--file", path,
                                   "--get-regexp", _LANE_KEY_RE.pattern)
        if rc not in (0, 1):
            return {}, "%s could not be read (%s)" % (
                path, err or "rc %s" % rc)
        found = {}
        for line in out.splitlines() if rc == 0 else ():
            key, _, value = line.partition(" ")
            m = _LANE_KEY_RE.match(key)
            if m:
                found.setdefault(m.group(1), set()).add(value.strip())
        records = {lane: frozenset(values) for lane, values in found.items()}
        _RECORDS[path] = (stamp, records)
    if not records:
        return records, None
    rc, out, err = _lanes._git(common, "for-each-ref", "--format=%(refname)",
                               "refs/heads/lane/")
    if rc != 0:
        return {}, "the lane branches could not be listed (%s)" % (
            err or "rc %s" % rc)
    live = {ref[len("refs/heads/lane/"):] for ref in out.splitlines()}
    return {lane: v for lane, v in records.items() if lane in live}, None


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
    rc, _out, err = _lanes._git(
        common, "config", "--file", os.path.join(common, "config"),
        "--unset-all", "branch.lane/%s.%s" % (lane, LANE_KEY))
    if rc == 0:
        return True, None
    return False, None if rc == 5 else (
        "git config could not drop the stale record (%s)" % (err or rc))


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


def record_lane(repo, lane, task):
    """(written, why) — record `task` as the task lane `lane` serves.
    Recording the same task again writes nothing; a lane that records a
    different task is refused, because a lane serves one task."""
    lane = lane_name(lane)
    records, why = lane_records(repo)
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
        "branch.lane/%s.%s" % (lane, LANE_KEY), task)
    if rc != 0:
        return False, "git config refused the record (%s)" % (err or rc)
    return True, None


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
            return None, ("lane %s records its task (helm work claim --task), "
                          "and %s; new work needs a lane of its own"
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
