"""helm.taskhygiene — the task ledger's self-cleaning, inside the stale sweep.

THE GAP (task/3451, the owner's ask: "we have hundreds to manage"). The ledger
holds ~1,400 open rows and the dominant stale class is LANDED BUT NOT CLOSED:
the work rode a train onto trunk while its task stayed open. One morning 3 of
5 P0s were already superseded or landed; another project closed 8 finished
rows by hand. Nothing joined the train log to the ledger, so every one of
those rows needed a person to notice.

FOUR PASSES, ONE PLAN, and the plan is the same object in a dry run and an
--apply, so what the dry run prints is exactly what --apply would do:

  1. LANDED-WORK LINK, AND A MARK, NEVER A CLOSE. The task a merge served
     is a FACT fixed when it merged: the line's own task ("trainNNN: merge
     lane <lane> at <tip> (task/N, ...)", RECORDED); "no task" and "task
     UNKNOWN: <why>" link nothing; a line that says neither (a hand-composed
     or older merge) is INFERRED from a literal task-N in its lane's name
     only, flagged as inferred on its receipt, never from a trailing number
     and never from TODAY's lane records (a lane name can be reused for
     another task). Today's records can only VETO an inference: a lane that
     now records a different task, or records that cannot be read, link
     nothing and write nothing. A link annotates task/N with the land sha,
     and the row reads "landed, whole ask not yet re-read"
     (`taskkey.landed_state`, task/3643 and task/3653): NO LAND CLOSES ANY
     ROW, because a land is not a re-read of the whole ask. The owner closes
     it after that read, in one word (`helm task confirm-close <id>`). A P0
     or an owner-asked row's holder is DMed once to do so, after a quiet
     window (HELM_TASK_CLOSE_QUIET_H, default 72 h) with no open children, no
     motion newer than the land and no trunk commit reverting it. A train
     that named only a PART of the task ("task/N part A", "item 14", "change
     1 of 3", a lane ending -Na or -NpK) annotates and is no candidate. A
     stood-down row is skipped by every pass.
  2. FRESHNESS CONTRACT by priority: P0 motion within 48 h, P1 within 7 d, P2
     within 30 d. Motion is anything a seat did on the row (a comment, a
     note, a claim, a status change — read from the row's own event history,
     so a later receipt never hides it), a commit naming the task on trunk or
     on any lane branch, a live work lease on a lane named for it, a dispatch
     row naming it, or a land naming it. The first breach pings the owner
     seat once through chat; the second unassigns the row into the triage
     queue (open, unowned). P3 and unranked rows carry no contract.
  A SWEEP THAT CANNOT READ A MOTION SOURCE ACTS ON NOTHING IT COULD HIDE. An
  unreadable trunk log, lane log, dispatch ledger, claims table or lane
  record can hide the very motion that makes a ping, an unassign or a
  confirm ask wrong, so none of the three is planned while any source is
  unavailable; the rows it cannot judge count UNKNOWN, never stale.
  3. DUPLICATE / SUPERSEDE detection by title overlap plus shared refs, as a
     PROPOSED list. Nothing is ever merged.
  4. RECEIPTS. Every automatic act leaves a comment on the task that starts
     with tasks.SWEEP_RECEIPT_MARK, and nothing is ever deleted.

THE CANDIDATE LIST IS THE LAND, NOT A SIDE FILE (task/3746). A row is a
close candidate, marked landed, because a full land names it: its own
comments carry the sweep's LANDED receipt, or the land step's question or
whole-land note (helm/landtask.py), or trunk carries a train merge that
names it and no receipt was written yet. The last is the one that listed
nothing: seven open rows' lanes had landed their whole ask while `helm task
close-candidates` found none, because a row was a candidate only once a
`helm stale sweep --apply` had written its receipt, and the scheduled sweep
runs dry. A candidate names its LAND (the land step's, else the land log's
number for its train) and whether its lane carried the whole ask. A row
was pinged because its own comments say so. There is no second store to
drift from the ledger, and every state this module acts on is readable in
`helm task show`. The receipts and the land step's comments are never motion
(a machine saying a row landed or is stale must not make it fresh), and
tasks.noted_epoch skips the receipts for the same reason.
"""
import os
import re
import sys
import time

from . import eventledger, taskkey, tasks

BOT = "stale-bot"
MARK = tasks.SWEEP_RECEIPT_MARK

FRESHNESS_S = {"P0": 48 * 3600, "P1": 7 * 86400, "P2": 30 * 86400}
QUIET_H_DEFAULT = 72
LAND_SCAN_DEFAULT = 5000     # trunk commits read for trains and task motion
PING_CAP_DEFAULT = 20        # owner pings per sweep; the rest ride the next
SUPERSEDE_OVERLAP = 0.4      # title overlap that, WITH a shared ref, proposes

ANNOTATE, ANNOTATE_PART = "annotate-landed", "annotate-part"
PING, UNASSIGN = "ping-owner", "unassign-to-triage"
CONFIRM_ASK = "ask-confirm"
# the acts a missing motion source can make wrong: none is planned blind
_BLIND_REFUSED = (PING, UNASSIGN, CONFIRM_ASK)
# where a train merge's task comes from: its own line, or its lane's name
RECORDED, INFERRED = "recorded", "inferred"

# A train merge's subject. The parenthetical is free prose; only its FIRST
# token is read as the car's task, because later mentions are other rows
# ("change 2 cut to task/3552", "verdict machinery moved to task/3558").
# A recomposed train keeps its number and takes a letter (train555b): it is
# the same kind of land, so its merges are read like any other train's.
_TRAIN = re.compile(r"^(train\d+[a-z]?): merge lane (\S+)"
                    r"(?: at ([0-9a-f]{7,40}))?(?:\s+\((.*))?$")
_HEAD_TASK = re.compile(r"^\s*task[/-](\d{1,6})\b", re.IGNORECASE)
# a lane that marks a PART of the task it already carries: -3529a (part a),
# -3533p2 (part 2); -3301-r3 is a third attempt at the whole task, not a
# part. Never a join: which task a lane serves is `taskkey.join`'s answer.
_LANE_PART = re.compile(r"-(\d{3,6})([a-z]|p\d+)(?:-r\d+)?$")
_PARTIAL = re.compile(r"\b(?:parts?|items?|half|phase|slice|step|stage|"
                      r"scope[- ]cut|follow[- ]?up|l\d+[a-z]?|"
                      r"change\s+\d+\s+of\s+\d+|\d+\s*/\s*\d+)\b",
                      re.IGNORECASE)
# "(no task, P?, ...)": the train says it carries no task, so the lane's
# trailing number is not borrowed for it (drift-probe-tmp-3578 is a probe,
# not task/3578)
_NO_TASK = re.compile(r"^\s*no[- ]task\b", re.IGNORECASE)
# "(task UNKNOWN: <why>, ...)": auto-land's join could not decide
# (`autoland._task_words`), so the line links nothing
_UNKNOWN_TASK = re.compile(r"^\s*task UNKNOWN\b")
# git's own revert body: a revert of a land blocks that land's link and close
# whatever the revert's subject says
_REVERTS = re.compile(r"This reverts commit ([0-9a-f]{7,40})")

_M = re.escape(MARK)
_R_LANDED = re.compile(r"^" + _M + r" LANDED ([0-9a-f]{7,40}) by (\S+) at "
                       r"(\S+)")
_R_PART = re.compile(r"^" + _M + r" LANDED-PART ([0-9a-f]{7,40}) ")
_R_STALE = re.compile(r"^" + _M + r" STALE-([12]) ")
_R_ASKED = re.compile(r"^" + _M + r" CONFIRM-ASKED ([0-9a-f]{7,40}) ")


def _env_int(name, default):
    """A positive integer knob, or its default — a typo never disables it."""
    try:
        got = int(str(os.environ.get(name) or "").strip())
    except ValueError:
        return default
    return got if got > 0 else default


def quiet_s():
    return _env_int("HELM_TASK_CLOSE_QUIET_H", QUIET_H_DEFAULT) * 3600


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _dur(s):
    s = max(0, int(s))
    return "%dd" % (s // 86400) if s >= 86400 else "%dh" % (s // 3600)


def _tid(num):
    return tasks.normalize_id(num) or "task/%s" % num


# ---------------------------------------------------------------------------
# reading trunk: trains and commits
# ---------------------------------------------------------------------------

def parse_train(subject):
    """One train merge subject -> {train, lane, tip, task, basis, partial},
    or None. Pure: the line is the fact, read with nothing else.

    `task` is the car's own task: the parenthetical's FIRST token when it is
    a task id (basis RECORDED), else None when it says "no task" or "task
    UNKNOWN", else the one task a literal task-N in the lane's name names
    (basis INFERRED), else None. `partial` is True when that first clause
    or the lane's part marker (-Na, -NpK on the task's own number) says the
    train carried only part of it — such a land annotates and is no
    candidate."""
    m = _TRAIN.match((subject or "").strip())
    if not m:
        return None
    train, lane, tip, prose = m.group(1), m.group(2), m.group(3), m.group(4)
    prose = (prose or "").rstrip(")")
    head = _HEAD_TASK.match(prose)
    task, basis, partial = None, None, False
    if head:
        task, basis = _tid(head.group(1)), RECORDED
        clause = re.split(r"[,;:)]", prose[head.end():], maxsplit=1)[0]
        partial = bool(_PARTIAL.search(clause))
    elif not _NO_TASK.match(prose) and not _UNKNOWN_TASK.match(prose):
        task = taskkey.join(lane=lane, lanes=False).task
        basis = INFERRED if task else None
    part = _LANE_PART.search(lane)
    if part and task == _tid(part.group(1)):
        partial = True
    return {"train": train, "lane": lane, "tip": tip, "task": task,
            "basis": basis, "partial": partial}


def land_link(train, lanes, lanes_bad=None):
    """(task, why) — the task a parsed train merge links, or (None, why).

    A RECORDED task is the fact and stands whatever the lane records say
    today. An INFERRED one stands only when today's records cannot say
    otherwise: records that cannot be read (`lanes_bad`, the read's
    error), or a lane that now records a different task, link nothing and
    name why, so the sweep never writes a guessed receipt."""
    if not train or not train.get("task"):
        return None, None
    if train.get("basis") == RECORDED:
        return train["task"], None
    lane = taskkey.lane_name(train["lane"])
    if lanes_bad:
        return None, ("%s's task is inferred from lane %s's name, and the "
                      "lane records cannot be read (%s)"
                      % (train["train"], lane, lanes_bad))
    have = (lanes or {}).get(lane)
    if have and have != {train["task"]}:
        return None, ("%s's task is inferred from lane %s's name as %s, and "
                      "the lane now records %s" % (
                          train["train"], lane, train["task"],
                          ", ".join(sorted(have))))
    return train["task"], None


def _named(text, lane=None, lanes=None):
    """Every task id `text` cites, and the task `lane` serves by the one join
    (`taskkey.join` over `lanes`, a `taskkey.lane_records` map) -> set."""
    out = {_tid(n) for n in tasks._CITED.findall(text or "")}
    if lane:
        task = taskkey.join(lane=lane, lanes=lanes or False).task
        if task:
            out.add(task)
    return out


def trunk_commits(root, trunk=None, limit=None):
    """([commit], unavailable) — the newest `limit` commits on trunk.

    Each commit: {sha, ct, subject, train (parse_train or None), named,
    reverts (the shas its body says it reverts)}. History is read as it was
    written: nothing here consults today's lane records. All
    commits count as motion for the tasks they name; only train merges link a
    land. Read through the vcs seam, trunk as `vcs.trunk_ref` resolves it
    (origin/<base> when present): only what the fleet can read is landed."""
    from . import vcs
    if not root:
        return [], "no repository to read trunk from"
    try:
        be = vcs.backend(root)
        ref = trunk or be.trunk_ref(root)
        rc, out, err = be.text(
            root, "log", ref, "-n",
            str(limit or _env_int("HELM_TASK_LAND_SCAN", LAND_SCAN_DEFAULT)),
            "--format=%H%x1f%ct%x1f%B%x1e", "--", timeout=120)
    except Exception as e:                      # noqa: BLE001 — reported
        return [], "trunk log unreadable (%s)" % e
    if rc != 0:
        return [], "trunk log unreadable (%s)" % (err or "rc %s" % rc)
    commits = []
    for rec in out.split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) != 3 or not parts[0].strip():
            continue
        sha, ct, body = parts[0].strip(), parts[1], parts[2]
        subject = body.strip().split("\n", 1)[0]
        train = parse_train(subject)
        commits.append({"sha": sha, "ct": float(ct or 0), "subject": subject,
                        "train": train, "named": _named(body),
                        "reverts": set(_REVERTS.findall(body))})
    return commits, None


def lane_commits(root, trunk=None, limit=None, lanes=None):
    """({task id: (epoch, what)}, unavailable) — commits on any local or
    remote branch that trunk does not carry, as motion.

    Work in progress lives on lane branches long before a train carries it,
    and a P0 being committed to every few hours is not stale because trunk
    has not seen it yet. A commit counts for the tasks its message names and
    for the task its branch serves by the one join (`--source` names the ref
    each commit was reached through; `lanes` is the repository's
    `taskkey.lane_records` map)."""
    from . import vcs
    if not root:
        return {}, "no repository to read lane branches from"
    try:
        be = vcs.backend(root)
        ref = trunk or be.trunk_ref(root)
        rc, out, err = be.text(
            root, "log", "--branches", "--remotes", "^" + ref, "--source",
            "-n",
            str(limit or _env_int("HELM_TASK_LAND_SCAN", LAND_SCAN_DEFAULT)),
            "--format=%H%x1f%ct%x1f%S%x1f%B%x1e", "--", timeout=120)
    except Exception as e:                      # noqa: BLE001 — reported
        return {}, "lane log unreadable (%s)" % e
    if rc != 0:
        return {}, "lane log unreadable (%s)" % (err or "rc %s" % rc)
    motion = {}
    for rec in out.split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) != 4 or not parts[0].strip():
            continue
        sha, ct, src, body = parts[0].strip(), float(parts[1] or 0), \
            parts[2].strip(), parts[3]
        for tid in _named(body, src, lanes):
            if ct > motion.get(tid, (0.0,))[0]:
                motion[tid] = (ct, "lane commit %s on %s" % (sha[:12], src))
    return motion, None


def lease_motion(now, lanes=None, project=None, repo_id=None):
    """({task id: (now, what)}, unavailable) — live `helm work claim` leases
    whose lane serves a task by the one join (`taskkey.join`: the lane's
    record in `lanes`, else a literal task-N in its name). With `project`,
    only that project's leases count at all: another project's lease, even
    on a lane named task-N, is that project's work, and counting it here
    would hide this project's stale row (task/3643). THE KEY IS THE ROOT'S
    BASENAME, so two repositories of one name share it; a lease therefore
    counts only as `dispatches.progress_state` counts one: the live GRANT
    records `repo_id`, this repository's git dir (`_grant_binds_repo`), AND
    the lane's room binds to it (`_lane_claim_binds`, the room's own gitdir
    pointer), both read off `dispatches.live_claims`. A room outlives its
    grant, so the room alone is never enough; a grant with no recorded
    repository, or no `repo_id`, is not this project's motion.

    A lease is a seat's standing statement that it is holding the lane
    (`worktree:<project>:<lane>` in the claims table, the resource
    work._lanes.resource spells), so a task whose lane is leased is being
    worked NOW. Read through the strict claims reader first, as
    landreq._cached_leases does: the tolerant table answers an unreadable
    file as empty, and an empty table here would read as no work at all."""
    from . import dispatches
    try:
        from . import seats_claims
        bad = seats_claims.claims_unavailable()
        grants = {} if bad else dispatches.live_claims() or {}
    except Exception as e:                      # noqa: BLE001 — reported
        bad = "%s" % type(e).__name__
    if bad:
        return {}, "claims table unreadable (%s)" % bad
    motion = {}
    for res, grant in sorted(grants.items()):
        parts = str(res).split(":", 2)
        if len(parts) != 3 or parts[0] != "worktree" \
                or not isinstance(grant, dict):
            continue
        if project and (parts[1] != project or not repo_id
                        or not dispatches._grant_binds_repo(repo_id, grant)
                        or not dispatches._lane_claim_binds(repo_id,
                                                            parts[2])):
            continue
        for tid in _named(parts[2], parts[2], lanes):
            motion[tid] = (now, "live lease %s (holder %s)" % (
                res, grant.get("holder") or "unknown"))
    return motion, None


def dispatch_motion(path=None):
    """({task id: newest epoch a dispatch row naming it OPENED}, unavailable).

    A row's opening is the motion; its later events (verdicts, closes) are
    the row finishing, and a close written after a land must not read as
    fresh work that holds the task open. A row names what its own text
    says, its recorded `task` included; today's lane records are not read
    for old rows."""
    if path is None:
        from . import dispatches
        path = dispatches.ledger_path()
    events, unavailable = eventledger.checked_events(path)
    if unavailable:
        return {}, "dispatch ledger unreadable (%s)" % unavailable
    seen, out = set(), {}
    for ev in events:
        rid = str(ev.get("id") or "")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        at = tasks.stamp_epoch(ev.get("ts"))
        if at is None:
            continue
        text = " ".join(str(v) for v in ev.values() if isinstance(v, str))
        for tid in _named(text, ev.get("lane")):
            out[tid] = max(out.get(tid, 0.0), at)
    return out, None


# ---------------------------------------------------------------------------
# reading one row: receipts, motion, children
# ---------------------------------------------------------------------------

def _receipts(row):
    """The row's own sweep receipts, oldest first -> [(ts, text)]."""
    out = []
    for c in tasks.comments_of(row):
        if tasks.is_sweep_receipt(c):
            at = tasks.stamp_epoch(c.get("ts"))
            out.append((at or 0.0, str(c.get("text") or "")))
    return out


def _linked_shas(receipts):
    shas = set()
    for _at, text in receipts:
        m = _R_LANDED.match(text) or _R_PART.match(text)
        if m:
            shas.add(m.group(1))
    return shas


def _latest_land(receipts):
    """The newest FULL land receipt -> {sha, train, land_at, noted_at}."""
    for at, text in reversed(receipts):
        m = _R_LANDED.match(text)
        if m:
            return {"sha": m.group(1), "train": m.group(2),
                    "land_at": tasks.stamp_epoch(m.group(3)) or at,
                    "noted_at": at}
    return None


def _machine_note(c):
    """Is comment `c` a machine's record: the sweep's receipt, the friction
    autopilot's count, or the land step's question or whole-land note?"""
    from . import frictionpilot, landtask
    return tasks.is_sweep_receipt(c) or frictionpilot.is_receipt(c) or (
        isinstance(c, dict) and landtask.parse_comment(c.get("text"))
        is not None)


def _land_steps(row):
    """The land step's own comments on the row, oldest first ->
    [(ts, {land, sha, lane, tip, whole})] (helm/landtask.py)."""
    from . import landtask
    out = []
    for c in tasks.comments_of(row):
        got = landtask.parse_comment(c.get("text")) \
            if isinstance(c, dict) else None
        if got:
            out.append((tasks.stamp_epoch(c.get("ts")) or 0.0, got))
    return out


def _bot_event(row, prior):
    """Did this ledger event only carry a machine's act? A sweep receipt or
    a land step comment appended, or the stale-bot's unassign — never
    motion."""
    before = [c for c in prior.get("comments") or () if isinstance(c, dict)]
    new = [c for c in row.get("comments") or ()
           if isinstance(c, dict) and c not in before]
    if new and all(_machine_note(c) for c in new):
        return True
    rel = row.get("released")
    return isinstance(rel, dict) and rel.get("by") == BOT \
        and rel != prior.get("released")


def _event_epoch(row, prior):
    """The epoch a seat's write to the row happened, or None for a filing,
    a sweep receipt, or an event that stamped no new time."""
    if prior is None or _bot_event(row, prior):
        return None
    ats = [tasks.stamp_epoch(row.get(k))
           for k in ("last_updated", "custody_updated")
           if row.get(k) != prior.get(k)]
    ats = [a for a in ats if a]
    return max(ats) if ats else None


def motion_of(row, commit_motion, dispatch_at, history):
    """(epoch, what) — the newest motion on the row, receipts excluded.

    THE ROW'S OWN EVENT HISTORY, NOT ITS NEWEST last_updated. Every write
    re-appends the whole row, so the ledger keeps each note edit, claim and
    status change with its own stamp; `history` is the newest such stamp that
    was not one of this module's receipts. Reading last_updated alone lost a
    note written between a land and its link: the LANDED receipt bumped the
    field past it, and the row auto-closed over an owner saying the land was
    only half the work. `commit_motion` already folds trunk, lane-branch and
    lease motion."""
    tid = row.get("id")
    best = (tasks.filed_epoch(row) or 0.0, "filed")
    for c in tasks.comments_of(row):
        if not _machine_note(c):
            at = tasks.stamp_epoch(c.get("ts"))
            if at and at > best[0]:
                best = (at, "comment")
    at = history.get(tid)
    if at and at > best[0]:
        best = (at, "row edit")
    got = commit_motion.get(tid)
    if got and got[0] > best[0]:
        best = got
    at = dispatch_at.get(tid)
    if at and at > best[0]:
        best = (at, "dispatch row")
    return best


def _children(rows_):
    """{id: [the open rows below it, at ANY depth]} over the story roll-up
    (`tasks.story_facts`, task/3746): an open grandchild under a closed
    child still holds its story's root, as it does `helm task close`."""
    out = {}
    for tid, facts in tasks.story_facts(rows_).items():
        kids = [r.get("id") for r in facts.get("below") or ()
                if r.get("status") in tasks.OPEN_STATUSES]
        if kids:
            out[tid] = kids
    return out


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------

def _root(repo):
    if repo:
        return repo
    from . import work
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return work.find_root(here) or here


def _reverted(sha, reverted):
    """Is land `sha` (full or a 12-char prefix) named by any revert?"""
    return next((r for r in reverted
                 if r.startswith(sha) or sha.startswith(r)), None)


def _land_numbers(root):
    """{train: "LAND N"} from the land log, or {} when it cannot be read: a
    candidate's LAND is a label, never a condition."""
    try:
        from . import autoland
        lands, _why = autoland.land_log(root)
    except Exception:                           # noqa: BLE001 — a label only
        return {}
    return {str(line.get("train")): "LAND %d" % n
            for n, line in sorted(lands.items()) if line.get("train")}


def _needs_word(row):
    """Why this row is never closed by the quiet window, or None."""
    if row.get("priority") == "P0":
        return "a P0"
    if tasks.origin_of(row) == "owner":
        return "owner-asked"
    return None


def plan(now=None, path=None, repo=None, trunk=None, dispatch_path=None,
         commits=None, duplicates=True):
    """Decide every task action this sweep would take -> the report dict.

    {actions, candidates, duplicates, counts, unavailable}. Pure: it reads
    the ledger, trunk, the lane branches, the claims table and the dispatch
    ledger, and writes nothing."""
    now = time.time() if now is None else now
    unavailable = []
    history = {}

    def _fold(row, prior):
        # one read of the ledger yields both the rows and each row's newest
        # seat-made write (motion_of)
        at = _event_epoch(row, prior)
        rid = str(row.get("id"))
        if at and at > history.get(rid, 0.0):
            history[rid] = at
        return True

    snap, bad = tasks.snapshot(path, strict=True, accept=_fold)
    if bad:
        return {"actions": [], "candidates": [], "duplicates": [],
                "counts": {}, "unavailable": ["task ledger: %s" % bad]}
    root = _root(repo)
    # THE ONE JOIN'S STORED KEYS (task/3643): a lane record helm cannot read
    # may name the task a lease serves or veto an inferred land, so it
    # blinds the sweep like any other motion source.
    lanes, lanes_bad = taskkey.lane_records(root)
    if lanes_bad:
        unavailable.append("lane records unreadable (%s)" % lanes_bad)
    if commits is None:
        commits, bad = trunk_commits(root, trunk)
        if bad:
            unavailable.append(bad)
    lane_at, bad = lane_commits(root, trunk, lanes=lanes)
    if bad:
        unavailable.append(bad)
    from . import dispatches
    from .work import _lanes
    repo_id = (dispatches._repo_info(root) or {}).get("repo_id")
    if not repo_id:
        unavailable.append("the repository's git dir could not be read, so "
                           "no lease can be bound to it")
    lease_at, bad = lease_motion(now, lanes=lanes,
                                 project=_lanes.project_token(root),
                                 repo_id=repo_id)
    if bad:
        unavailable.append(bad)
    dispatch_at, bad = dispatch_motion(dispatch_path)
    if bad:
        unavailable.append(bad)
    commit_motion, lands, reverted, refused = {}, {}, set(), []
    for extra in (lane_at, lease_at):
        for tid, got in extra.items():
            if got[0] > commit_motion.get(tid, (0.0,))[0]:
                commit_motion[tid] = got
    for c in commits:
        reverted.update(c.get("reverts") or ())
        what = ("land " + c["train"]["train"]) if c["train"] else "commit"
        for tid in c["named"]:
            if c["ct"] > commit_motion.get(tid, (0.0,))[0]:
                commit_motion[tid] = (c["ct"], what + " " + c["sha"][:12])
        task, why = land_link(c["train"], lanes, lanes_bad)
        if task:
            lands.setdefault(task, []).append(c)
        elif why:
            refused.append(why)
    blind = "motion UNKNOWN: %s" % "; ".join(unavailable) \
        if unavailable else None
    children = _children(snap)
    numbers = _land_numbers(root)
    quiet = quiet_s()
    actions, candidates = [], []
    counts = {"close_candidates": 0, "landed_unread": 0,
              "links_refused": len(refused), "stale": 0, "warned": 0,
              "triage": 0, "unknown": 0, "stood_down": 0,
              "by_priority": {p: {"open": 0, "stale": 0, "warned": 0,
                                  "triage": 0, "unknown": 0}
                              for p in FRESHNESS_S},
              "no_contract": 0}
    for tid in sorted(snap, key=lambda k: tasks.sort_key(snap[k])):
        row = snap[tid]
        if row.get("status") not in tasks.OPEN_STATUSES:
            continue
        if tasks.standdown_blocks_offer(row, now):
            # deliberately parked: no link, no close, no contract
            counts["stood_down"] += 1
            continue
        receipts = _receipts(row)
        linked = _linked_shas(receipts)
        new_full, unreceipted = False, None
        for c in sorted(lands.get(tid, ()), key=lambda c: c["ct"]):
            if c["sha"][:12] in linked or c["sha"] in linked:
                continue
            if _reverted(c["sha"], reverted):
                continue       # a land trunk backed out links nothing
            tr = c["train"]
            kind = ANNOTATE_PART if tr["partial"] else ANNOTATE
            new_full = new_full or not tr["partial"]
            if not tr["partial"]:
                unreceipted = {"sha": c["sha"][:12], "train": tr["train"],
                               "land_at": c["ct"], "noted_at": c["ct"]}
            actions.append({"kind": kind, "id": tid, "sha": c["sha"][:12],
                            "train": tr["train"], "lane": tr["lane"],
                            "land_at": c["ct"], "text": _land_text(
                                kind, tid, c["sha"][:12], tr, c["ct"])})
        receipted = _latest_land(receipts)
        steps = _land_steps(row)
        step_at, step = steps[-1] if steps else (None, None)
        # A FULL LAND NAMES THE ROW (task/3746): the sweep's receipt, else
        # a trunk land no receipt records yet, else the land step's own
        # comment. Only a receipted land is ever asked of its holder.
        land = receipted or unreceipted or ({
            "sha": step["sha"], "train": None, "land_at": step_at,
            "noted_at": step_at} if step else None)
        mot_at, mot_what = motion_of(row, commit_motion, dispatch_at, history)
        if land:
            counts["close_candidates"] += 1
            blockers = []
            kids = children.get(tid) or []
            if kids:
                blockers.append("open children %s" % ", ".join(sorted(kids)))
            # motion NEWER THAN THE LAND: the land commit itself and the
            # lane's commits under it are the land, not motion after it, and
            # a row FILED after its land (a late record of landed work) has
            # had no motion at all
            if mot_at > land["land_at"] + 1 and mot_what != "filed":
                blockers.append("motion after the land (%s %s)"
                                % (mot_what, _iso(mot_at)))
            back = _reverted(land["sha"], reverted)
            if back:
                blockers.append("the land was reverted (%s)" % back[:12])
            if blind:
                blockers.append(blind)
            waited = now - land["noted_at"]
            word = _needs_word(row)
            # THE MARK, NEVER A CLOSE (task/3643, task/3653): landed work is
            # not a re-read of the whole ask, so the row reads LANDED_UNREAD
            # and its owner closes it after that read
            state = taskkey.landed_state(row, (land,))
            counts["landed_unread"] += 1 if state else 0
            cand = {"id": tid, "title": row.get("title"),
                    "owner": tasks.owner_of(row), "sha": land["sha"],
                    "train": land["train"], "noted_at": land["noted_at"],
                    "quiet_left_s": max(0.0, quiet - waited),
                    "blockers": blockers, "state": state,
                    "needs_confirm": word,
                    "land": (step or {}).get("land")
                    or numbers.get(str(land["train"])),
                    "whole": step["whole"] if step else None,
                    "receipted": bool(receipted),
                    # a whole land's seen-working check (helm/observed.py)
                    "check": _check_note(row)}
            candidates.append(cand)
            if blockers or waited < quiet or not word or not receipted \
                    or cand["check"]:
                continue
            # A P0 OR AN OWNER-ASKED ROW'S HOLDER IS DMed once per land to
            # re-read and confirm; the row stays a candidate for
            # `helm task confirm-close`
            asked = {m.group(1) for _a, t in receipts
                     for m in (_R_ASKED.match(t),) if m}
            owner = cand["owner"]
            if owner and land["sha"] not in asked:
                actions.append({
                    "kind": CONFIRM_ASK, "id": tid, "owner": owner,
                    "sha": land["sha"], "train": land["train"],
                    "text": "%s CONFIRM-ASKED %s by %s — %s row; @%s asked "
                            "to re-read the whole ask and confirm with "
                            "`helm task confirm-close %s`]" % (
                                MARK, land["sha"], land["train"], word,
                                owner, tid)})
            continue
        if new_full:
            continue       # a candidate from this sweep on: not a freshness case
        _freshness(row, now, mot_at, mot_what, receipts, actions, counts,
                   blind)
    return {"actions": actions, "candidates": candidates,
            "duplicates": propose_duplicates(snap) if duplicates else [],
            "counts": counts, "links_refused": refused,
            "unavailable": unavailable}


def _check_note(row):
    """The seen-working check `row` owes, in words, or None."""
    from . import observed
    owed = observed.owes(row)
    return observed.note(owed, row["id"]) if owed else None


def _land_text(kind, tid, sha, tr, ct):
    lane = "lane %s%s" % (tr["lane"], "; the task inferred from the lane's "
                          "name, not recorded at the land"
                          if tr.get("basis") == INFERRED else "")
    if kind == ANNOTATE_PART:
        return ("%s LANDED-PART %s by %s at %s (%s) — the train named "
                "only part of this task, so it stays open]"
                % (MARK, sha, tr["train"], _iso(ct), lane))
    return ("%s LANDED %s by %s at %s (%s) — %s: its owner re-reads the "
            "whole ask and closes it with `helm task confirm-close %s`; no "
            "land closes a task]"
            % (MARK, sha, tr["train"], _iso(ct), lane,
               taskkey.LANDED_UNREAD, tid))


def _freshness(row, now, mot_at, mot_what, receipts, actions, counts,
               blind=None):
    tid, prio = row.get("id"), row.get("priority")
    window = FRESHNESS_S.get(prio)
    if window is None:
        counts["no_contract"] += 1
        return
    bucket = counts["by_priority"][prio]
    bucket["open"] += 1
    if now - mot_at < window:
        return
    if blind:
        # the motion that would make this row fresh may be in the source
        # nobody could read: UNKNOWN, never stale, and nothing is sent
        counts["unknown"] += 1
        bucket["unknown"] += 1
        return
    owner = tasks.owner_of(row)
    after = [(at, int(m.group(1))) for at, text in receipts
             for m in (_R_STALE.match(text),) if m and at > mot_at]
    level = max([lv for _at, lv in after] or [0])
    warned_at = max([at for at, lv in after if lv == 1] or [0.0])
    counts["stale"] += 1
    bucket["stale"] += 1
    idle = "no motion for %s (contract %s; last: %s %s)" % (
        _dur(now - mot_at), _dur(window), mot_what, _iso(mot_at))
    if not owner or level >= 2:
        counts["triage"] += 1
        bucket["triage"] += 1
        return
    if level == 0:
        actions.append({"kind": PING, "id": tid, "owner": owner,
                        "priority": prio, "breach_at": mot_at + window,
                        "text": "%s STALE-1 %s %s; pinged @%s]"
                                % (MARK, prio, idle, owner)})
        return
    counts["warned"] += 1
    bucket["warned"] += 1
    if now - warned_at >= window:
        actions.append({"kind": UNASSIGN, "id": tid, "owner": owner,
                        "priority": prio, "row": row,
                        "text": "%s STALE-2 %s %s; second breach, unassigned "
                                "from @%s into the triage queue]"
                                % (MARK, prio, idle, owner)})


def propose_duplicates(snap):
    """[{id, of, overlap, shared}] — open rows that may repeat an older one.

    PROPOSED, NEVER MERGED. A pair is listed when the titles overlap past the
    add door's own duplicate threshold, or share a ref and overlap past
    SUPERSEDE_OVERLAP; the newer row is named as the possible duplicate of
    the older. Rows in different projects are never compared."""
    live = [r for r in snap.values() if r.get("status") in tasks.OPEN_STATUSES]
    toks, by_word = {}, {}
    for r in live:
        t = tasks.title_tokens(r.get("title"))
        if t:
            toks[r["id"]] = t
            for w in t:
                by_word.setdefault(w, []).append(r["id"])
    rowmap = {r["id"]: r for r in live}
    out, seen = [], set()
    for tid, mine in toks.items():
        peers = {p for w in mine for p in by_word[w] if p != tid}
        for other in peers:
            pair = tuple(sorted((tid, other)))
            if pair in seen:
                continue
            seen.add(pair)
            a, b = rowmap[pair[0]], rowmap[pair[1]]
            if tasks.project_of_row(a) != tasks.project_of_row(b):
                continue
            theirs = toks[other]
            overlap = len(mine & theirs) / float(len(mine | theirs))
            shared = sorted(set(a.get("refs") or ()) & set(b.get("refs") or ()))
            if overlap >= tasks.DUP_OVERLAP or (
                    shared and overlap >= SUPERSEDE_OVERLAP):
                older, newer = sorted(
                    (a, b), key=lambda r: tasks.filed_epoch(r) or 0.0)
                out.append({"id": newer["id"], "of": older["id"],
                            "overlap": round(overlap, 2), "shared": shared})
    out.sort(key=lambda d: (-d["overlap"], d["id"]))
    return out


# ---------------------------------------------------------------------------
# acting
# ---------------------------------------------------------------------------

def _chat_ping(owner, text, key):
    """The existing chat post path: one DM to the owner seat, keyed so a
    retried breach can never post twice."""
    from . import chat
    try:
        got = chat.post(text, who=BOT, sign=False, dm=owner, dm_display=owner,
                        event_id=key)
    except Exception as e:                      # noqa: BLE001 — reported
        print("helm stale sweep: task ping to @%s failed: %s" % (owner, e),
              file=sys.stderr)
        return False
    return isinstance(got, dict) and bool(got.get("id"))


def sweep(apply=False, notify=None, now=None, path=None, repo=None,
          trunk=None, dispatch_path=None, commits=None):
    """plan() and, with apply=True, act on it -> the report plus
    {applied, failed, deferred}. Dry run is the default and writes nothing.

    Each act writes its receipt comment; a decision write (close, unassign)
    is guarded by the row the plan judged, so a row a seat touched between
    the read and the write is skipped, never overwritten. Pings are capped
    per sweep (HELM_TASK_PING_CAP); the rest wait for the next sweep."""
    rep = plan(now=now, path=path, repo=repo, trunk=trunk,
               dispatch_path=dispatch_path, commits=commits)
    rep.update({"applied": [], "failed": [], "deferred": [], "apply": apply})
    if apply:
        _act(rep, notify, path)
    rep["actions"] = [_public(a) for a in rep["actions"]]
    rep["deferred"] = [_public(a) for a in rep["deferred"]]
    return rep


def _public(act):
    """An action without the judged row it carries for the guarded write."""
    return {k: v for k, v in act.items() if k != "row"}


def _act(rep, notify, path):
    """Take each planned action in order; each leaves its receipt comment."""
    notify = notify or _chat_ping
    cap = _env_int("HELM_TASK_PING_CAP", PING_CAP_DEFAULT)
    pinged = 0
    for act in rep["actions"]:
        kind, tid = act["kind"], act["id"]
        err = None
        if kind in _BLIND_REFUSED and rep.get("unavailable"):
            # plan() plans none of these blind; this is the writer's own
            # refusal, so no later edit to the plan can act without sight
            err = "a motion source is unavailable — nothing was sent"
        elif kind in (ANNOTATE, ANNOTATE_PART):
            _row, err = tasks.comment(tid, act["text"], by=BOT, path=path)
        elif kind == PING:
            if pinged >= cap:
                rep["deferred"].append(act)
                continue
            pinged += 1
            key = "task-fresh:%s:%d" % (tid, int(act["breach_at"]))
            msg = ("%s is %s and has had no motion past its freshness "
                   "contract — comment on it, close it, or release it; a "
                   "second breach unassigns it into triage" % (
                       tid, act["priority"]))
            if not notify(act["owner"], msg, key):
                err = "ping to @%s was not delivered" % act["owner"]
            else:
                _row, err = tasks.comment(tid, act["text"], by=BOT, path=path)
        elif kind == CONFIRM_ASK:
            key = "task-confirm:%s:%s" % (tid, act["sha"])
            msg = ("%s landed whole on trunk at %s (%s) and is %s: re-read "
                   "the whole ask, then confirm with `helm task "
                   "confirm-close %s`, or comment why it stays open" % (
                       tid, act["sha"], act["train"],
                       "a P0" if "a P0" in act["text"] else "owner-asked",
                       tid))
            if not notify(act["owner"], msg, key):
                err = "confirm ask to @%s was not delivered" % act["owner"]
            else:
                _row, err = tasks.comment(tid, act["text"], by=BOT, path=path)
        elif kind == UNASSIGN:
            got, err = tasks.release_stale(tid, act["text"], act["row"],
                                           path=path)
            if got is tasks.SKIPPED:
                err = err or "row moved"
            elif not err:
                _row, err = tasks.comment(tid, act["text"], by=BOT, path=path)
        (rep["failed"] if err else rep["applied"]).append(
            dict(_public(act), **({"error": err} if err else {})))


def close_candidates(**kw):
    """The candidate list — plan()'s `candidates`, nothing written."""
    rep = plan(duplicates=False, **kw)
    return rep["candidates"], rep["unavailable"]


def confirm_close(token, by, path=None, plan_lands=False,
                  open_children=None):
    """The owner's one word -> (row, error): close a CURRENT close candidate
    with its land in the reason. A candidate's land is its full-land
    receipt, else the land step's comment, else (`plan_lands`, the CLI's
    reading) the trunk land the candidate list names it by. Refused for a
    row no full land names, for a row another seat holds, and, as `helm task
    close` is (task/3746), for a row with open sub-tasks unless
    `open_children` is tasks.OPEN_CHILDREN_STAY, which records them."""
    row = tasks.get(token, path=path)
    if not row:
        return None, "%s does not exist" % token
    if row.get("status") not in tasks.OPEN_STATUSES:
        return None, "%s is already closed" % row["id"]
    land = _latest_land(_receipts(row))
    steps = _land_steps(row)
    if not land and steps:
        step = steps[-1][1]
        land = {"sha": step["sha"], "train": step["land"] or "a hand land"}
    if not land and plan_lands:
        got = {c["id"]: c for c in close_candidates(path=path)[0]}
        land = got.get(row["id"])
    if not land:
        return None, ("%s is not a close candidate: no train naming the "
                      "whole task is linked to it (a part-land never is). "
                      "Close it with `helm task close %s <reason>`"
                      % (row["id"], row["id"]))
    # A WHOLE LAND'S TASK CLOSES ON A SEEN-WORKING CHECK (helm/observed.py),
    # never on a confirm with no evidence.
    from . import observed
    owed = observed.owes(row)
    if owed:
        return None, ("%s landed whole and %s; it closes with that check, "
                      "not with confirm-close" % (row["id"],
                                                  observed.note(owed,
                                                                row["id"])))
    holder = tasks.owner_of(row)
    if holder and not tasks.held_by(row, by):
        return None, ("%s is held by %s — the owner confirms its close, "
                      "not %s" % (row["id"], holder, by))
    return tasks.close(row["id"], "landed on trunk at %s (%s); close "
                       "confirmed by @%s" % (land["sha"], land["train"], by),
                       path=path, expect=row,
                       open_children=open_children
                       or tasks.OPEN_CHILDREN_REFUSE)


def freshness_counts(**kw):
    """The sweep's counts, as an API no surface reads yet ->
    {close_candidates, stale, warned, triage, unknown, stood_down,
    by_priority, no_contract}. A row a blind sweep cannot judge counts
    `unknown`, never `stale`. Each call reads the git logs and the dispatch
    ledger, so a polling caller caches it."""
    return plan(duplicates=False, **kw)["counts"]


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def render(rep):
    """The sweep's task section as printable lines."""
    apply = rep.get("apply")
    lines = []
    for u in rep.get("unavailable") or ():
        lines.append("task hygiene: source UNAVAILABLE — %s" % u)
    done = rep.get("applied") if apply else rep.get("actions")
    verb = "did" if apply else "would"
    for act in done or ():
        lines.append("  %s %-18s %-10s %s" % (
            verb, act["kind"], act["id"], act["text"][len(MARK):].strip(" ]")))
    for act in rep.get("failed") or ():
        lines.append("  FAILED %-18s %-10s %s" % (
            act["kind"], act["id"], act.get("error")))
    for d in rep.get("duplicates") or ():
        lines.append("  proposed: %s may duplicate %s (title overlap %.2f%s)"
                     % (d["id"], d["of"], d["overlap"],
                        "; shared refs " + ", ".join(d["shared"])
                        if d["shared"] else ""))
    c = rep.get("counts") or {}
    for why in rep.get("links_refused") or ():
        lines.append("  no link: %s" % why)
    lines.append(
        "task hygiene: %d action(s) %s, %d close candidate(s) (%d %s), %d "
        "stale (%d warned, %d in triage), %d duplicate proposal(s)%s%s"
        % (len(done or ()), "taken" if apply else "planned (dry run — "
           "--apply acts)", c.get("close_candidates", 0),
           c.get("landed_unread", 0), taskkey.LANDED_UNREAD,
           c.get("stale", 0),
           c.get("warned", 0), c.get("triage", 0),
           len(rep.get("duplicates") or ()),
           ", %d FAILED" % len(rep["failed"]) if rep.get("failed") else "",
           ", %d ping(s) deferred to the next sweep" % len(rep["deferred"])
           if rep.get("deferred") else ""))
    return lines
