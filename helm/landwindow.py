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
check it. This verb holds it by construction instead: every approve-ready car
carries its authorizing approve before the gate is spent.

AND A SOURCE-CLEAN CAR CARRIES ITS FINISHED READ INSTEAD (task/3053 F3). A
reviewer who read the delta and found nothing cannot mint an approve, because
an approve binds a whole-suite token only this gate produces, so it holds the
row `--source-clean <tip>`. That row rides as a car at the HELD tip when
`landreq.source_clean_car` admits it: the hold names a holder, the holder is
the row's recipient and wrote no round of the lane, no unanswered FIX or
SUPERSEDE from another row stands on the held tip (the CONTESTED rung an
approve row answers to), the held tip descends from the dispatched ref, and
the row is not terminal. Every other held source-clean row is listed as
excluded with the predicate's reason — except the rows whose hold records NO
HOLDER, a legacy backlog counted on ONE line that names the verb listing them.
The read is finished and found nothing, so the suite is not spent ahead of a
FIX. The merge keeps the held sha a parent, so after the gate passes and the
train lands, `helm lr foldcheck <head> --gate G --apply` closes each such car
as `source-clean-landed` on ancestry alone; no approve is minted.

THIS VERB IS THE ONLY ONE THAT TAKES THEM (the author's ruling 1, round 4).
`helm lr compose` asks the same predicate and refuses every source-clean
candidate: it cherry-picks, and `source-clean-landed` closes on ancestry
only, so a picked copy could never close.

AN EJECTED TIP STAYS OUT. `helm train blame --apply` (helm/trainblame.py)
ejects the car that broke a red train, and its row is still READY, so without
a record the next plan would merge the same tip into the next train and the
ejection would undo itself. The record is the EJECTION STORE: an append-only
JSONL under the project's `.state` (`ejections_path`), one `eject` event per
ejection bound to the car's EXACT TIP (the train, the red gate, the verdict,
the evidence line and the failing tests), and one `readmit` event per hand
clear, with who cleared it and why (`helm train readmit <tip> --reason R`,
for a blame that was wrong). No existing door fits: a verdicted dispatch
row takes no note (the findings-note admits open and held rows only).

  THE TIP, NOT THE LANE. The plan leaves out a car whose CURRENT tip has an
  ejection standing; a new tip on the same lane is a different sha and rides
  again with nothing to clear.

  AN UNREADABLE STORE GUESSES NEITHER WAY. It is read strictly, and a store
  with a malformed row, a row with no tip or an event this helm does not know
  is UNKNOWN: the plan keeps every car and marks its ejection UNKNOWN (so the
  dry run still says what it would do), and `--apply` refuses by name,
  because nothing can prove an ejected car is not in the train. A missing
  store is an empty one.

  AN EJECTION IS ORDERED AGAINST A PUSH (task/3265 races F2). `helm train
  auto`'s last word reads the plan once more and pushes under the READINESS
  LOCK (`readiness_lock`, the dispatch ledger's own lock, which every
  verdict, hold and retip writer takes). Every write to this store takes that
  lock first, then the store's own: an ejection lands before the last read
  (which then leaves its car out) or waits until the push is done, and is
  never written between the read and the push.
"""
import binascii
import getpass
import os
import re
import sys
import time

from . import (dispatches, eventledger, gatewindow, home, landorder,
               landreq, pk, rowworld, vcs)
from .work import _lanes

PROG = "helm train"
USAGE = ("usage: helm train [--repo PATH] [--trunk REF] [--name TRAIN] "
         "[--max-behind N] [--apply]\n"
         "       helm train blame <train-room> [--gate gate:<id>] [--apply] "
         "[--json]\n"
         "       helm train readmit <tip> --reason TEXT [--repo PATH]\n"
         "       helm train auto [--repo PATH] [--apply] [--status] "
         "[--pause [--reason TEXT] | --resume | --abandon [--force] "
         "[--drop ROW|LANE] --reason TEXT]\n"
         "       helm train veto <train> --reason TEXT [--repo PATH]")
READMIT_USAGE = "usage: helm train readmit <tip> --reason TEXT [--repo PATH]"
BOX = "compose"

# A merge that runs longer than this is a merge nobody is watching.
MERGE_TIMEOUT_S = 300

# How long `--apply` waits for auto-land's compose lock, which it holds only
# from its last flight check to its move to COMPOSING.
COMPOSE_LOCK_WAIT_S = 60

# EVERY MERGE CARRIES THIS, including the abort. See the module docstring.
NO_RERERE = ("-c", "rerere.enabled=false")

# AND EVERY MERGE IS THE REPOSITORY'S OWN, never the caller's identity: a car
# merge made from a seat's shell would otherwise carry whatever author that
# shell exports, so the four identity variables are removed (the seam's None)
# and the repository's configured author writes the merge. The integrator's
# sanction rides the call, the same bit the room's mint carries.
MERGE_ENV = {"GIT_AUTHOR_NAME": None, "GIT_AUTHOR_EMAIL": None,
             "GIT_COMMITTER_NAME": None, "GIT_COMMITTER_EMAIL": None,
             "HELM_WORK_INTEGRATOR": "1"}

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
# THE WORD A SOURCE-CLEAN CAR PRINTS in the READY word's column: no approve,
# a finished read (task/3053 F3).
SOURCE_CLEAN = "SOURCE-CLEAN"
#: The train name `car_admission` plans under: a notice reads no train
#: number from trunk, and no train is ever made under this name.
NOTICE_NAME = "admission"


# THE EJECTION STORE (see the module docstring): its file under the project's
# .state, its two events, and the word a car's ejection reads when the store
# cannot be read.
EJECTIONS = "train-ejections.jsonl"
EJECTED = "eject"
READMITTED = "readmit"
UNKNOWN = "UNKNOWN"
_TIP = re.compile(r"\A[0-9a-f]{40}\Z")
_TIP_TOKEN = re.compile(r"\A[0-9a-f]{4,40}\Z")


#: The one line `helm train` folds every NO HOLDER exclusion into (the
#: author's ruling 6, round 4), and the verb that lists those rows.
NO_HOLDER_LIST = "helm dispatch list --no-holder"

# LAND FIRST, REVIEW AFTER (task/4223). A review row nobody has read for this
# long rides the train at its dispatched tip without a review hold, when its
# lane is no door. The whole-suite gate still runs, the row stays OPEN, and
# its reader reads the landed merge: a FIX files a follow-on task, never a
# revert. LAND_FIRST is the car's basis, LAND_FIRST_WORD its plan word, and
# LAND_FIRST_MARK the words its merge body and its land line carry.
LAND_FIRST_WAIT_S = 1800
LAND_FIRST = "land-first"
LAND_FIRST_WORD = "UNREAD"
LAND_FIRST_MARK = "landed before review"
# THE TIP WORD each car basis prints, and the words its merge body writes
# before `tip` (`trainblame._CAR` reads them back).
_TIP_WORD = {"source-clean": "held", LAND_FIRST: "unread"}
_BODY_WORD = {"source-clean": "source-clean held",
              LAND_FIRST: LAND_FIRST_MARK + ", unread"}


def land_first_tip(lr, rows, now):
    """The dispatched tip a review row rides at before its review, or None.

    Only an OPEN review row qualifies, dispatched (or last re-tipped) at least
    LAND_FIRST_WAIT_S ago, on which no reader recorded anything: no verdict
    and no hold (the row is still open), no hold that was released, no
    advisory read, no findings note, nothing retired. A stamp that cannot be
    read or lies in the future is no age, so no car. Whether its lane is a
    door, and whether its tip is its branch's and off trunk, are `plan`'s,
    asked after the cheap ledger facts here."""
    row = (rows or {}).get(str(lr.get("id") or ""))
    if not isinstance(row, dict) or row.get("kind") != "review" \
            or row.get("status") != "open" or lr.get("terminal") \
            or landreq._retired_by(lr) or any(row.get(k) for k in (
                "release_ts", "advisory_reads", "findings_notes")):
        return None
    hops = row.get("retips") or ()
    at = landreq._epoch((hops[-1] if hops else row).get("ts"))
    if at is None or not LAND_FIRST_WAIT_S <= now - at:
        return None
    tip = str(row.get("tip") or "")
    return tip if _TIP.fullmatch(tip) and tip == lr.get("pinned_tip") \
        else None


def _land_first_reason(lr):
    """The clause beside a land-first car's word: what rides, and what its
    row owes after the land."""
    return ("no reader in %d min and no door: %s; the row stays open for "
            "its reader %s's post-land read, and a FIX files a follow-on "
            "task, never a revert" % (LAND_FIRST_WAIT_S // 60,
                                      LAND_FIRST_MARK,
                                      lr.get("reviewer") or "?"))


def _land_first_doors(lr):
    """Every door class of a land-first car's lane, ["unknown"] when its
    doors cannot be read: `review_door.lane_doors` is the authority."""
    from . import review_door
    rows, _verdicts, err = landreq._ledger_fold()
    row = (rows or {}).get(str(lr.get("id") or ""))
    if err or not isinstance(row, dict):
        return ["unknown"]
    try:
        return sorted({c for c, _e in review_door.lane_doors(row, rows)
                       ["doors"]})
    except Exception:  # noqa: BLE001 — unread doors could include a door
        return ["unknown"]


def _source_clean_reason(lr):
    """The clause beside a source-clean car's word: who read it clean, and
    that no approve is owed. The close its land owes is printed once, below
    the plan (`_source_clean_loop`)."""
    return "held by @%s, no approve owed" % (lr.get("hold_actor") or "?")


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


def ejections_path(root):
    """The project's ejection store: `<helm home>/<project>/.state/`, the
    project named as a lane claim names it (`_lanes.project_token`)."""
    return os.path.join(home.project_dir(_lanes.project_token(root)),
                        ".state", EJECTIONS)


def read_ejections(root):
    """(standing {tip: its eject record}, unavailable). A readmit after an
    eject clears that tip. Read STRICTLY: a malformed row, a row with no tip
    or an event this helm does not know makes the whole store UNKNOWN, and
    `unavailable` says why; a missing store is an empty one."""
    path = ejections_path(root)
    rows, unavailable = eventledger.checked_events(path, strict=True)
    if unavailable:
        return None, "the ejection store %s could not be read (%s)" % (
            path, unavailable)
    standing = {}
    for row in rows:
        tip = row.get("tip")
        if not isinstance(tip, str) or not _TIP.match(tip):
            return None, ("the ejection store %s holds a record with no tip"
                          % path)
        if row.get("event") == EJECTED:
            standing[tip] = row
        elif row.get("event") == READMITTED:
            standing.pop(tip, None)
        else:
            return None, ("the ejection store %s holds an event this helm "
                          "cannot read (%r)" % (path, row.get("event")))
    return standing, None


def readiness_lock(timeout=None):
    """THE READINESS LOCK: the dispatch ledger's own lock, which every
    verdict, hold, retip and withdrawal writer takes. `helm train auto`
    holds it from its last read of the plan through its push, and every
    ejection-store write takes it before the store's own lock, so a car's
    readiness never changes between that read and that push (the module
    docstring, task/3265 races F2). Every other writer of a land veto or of
    land authority takes it too (`landorder`, task/3265 races R2). It is let
    go by closing, never by unlocking, and it is reentrant on one thread
    (`landorder.locked`). Yields whether it was taken."""
    return landorder.locked(timeout=timeout)


def readiness_lock_path():
    """The file `readiness_lock` holds its flock on: the sibling
    `<ledger>.lock` `eventledger.locked` opens (a push inherits its
    descriptor, `autoland.Ops.push`)."""
    return landorder.path()


def _append_ejection(root, row):
    """(row, error): one event appended under the readiness lock, then
    the store's own lock, in that order and never the other."""
    path = ejections_path(root)
    row = dict(row, v=1, ts=pk.now_ts(),
               id=binascii.hexlify(os.urandom(8)).decode("ascii"))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)
    with readiness_lock() as ready:
        if not ready:
            return None, ("the readiness lock (the dispatch ledger's, which "
                          "a land holds from its last read through its push) "
                          "was not taken, so the ejection store %s is not "
                          "written" % path)
        with eventledger.locked(path) as held:
            if not held:
                return None, "the ejection store %s is locked" % path
            ok, why = eventledger.append_unlocked_checked(path, row)
    return (row, None) if ok else (None, why)


def record_ejection(root, record):
    """(row, error): one EJECT record, bound to `record["tip"]`."""
    if not _TIP.match(str(record.get("tip") or "")):
        return None, "an ejection records the car's full tip"
    return _append_ejection(root, dict(record, event=EJECTED))


# A FLAKE is not an ejection. `read_ejections` is strict, so a flake row in that
# store would make every `helm train` UNKNOWN, and a flake must never keep a
# car out. Its own store records the tree and the tests blame saw flake.
FLAKES = "flakes.jsonl"
FLAKED = "FLAKED"


def flakes_path(root):
    """The project's flake store, beside the ejection store."""
    return os.path.join(home.project_dir(_lanes.project_token(root)),
                        ".state", FLAKES)


def _flake_rows(root):
    """(rows, unavailable). The strict read `read_flakes` and `flake_counts`
    share. A missing store is an empty one; one malformed row makes the whole
    store UNKNOWN."""
    path = flakes_path(root)
    rows, unavailable = eventledger.checked_events(path, strict=True)
    if unavailable:
        return None, "the flake store %s could not be read (%s)" % (
            path, unavailable)
    for row in rows:
        if row.get("event") != FLAKED:
            return None, ("the flake store %s holds an event this helm cannot "
                          "read (%r)" % (path, row.get("event")))
        tree = row.get("tree")
        tests = row.get("tests")
        if not isinstance(tree, str) or not _TIP.match(tree) \
                or not isinstance(tests, list) \
                or any(not isinstance(t, str) or not t for t in tests):
            return None, ("the flake store %s holds a record with no tree or "
                          "no tests" % path)
    return rows, None


def read_flakes(root):
    """({tree: set(test ids)}, unavailable). Read STRICTLY, like the ejection
    store: a malformed row or an event this helm does not know makes the whole
    store UNKNOWN. A missing store is an empty one. Several FLAKED rows for one
    tree union their tests."""
    rows, unavailable = _flake_rows(root)
    if unavailable:
        return None, unavailable
    standing = {}
    for row in rows:
        standing.setdefault(row["tree"], set()).update(row["tests"])
    return standing, None


def flake_counts(root):
    """({tree: count of FLAKED rows}, unavailable). Read STRICTLY, like
    `read_flakes`: the same row is one count, however many tests it names. A
    missing store is an empty one."""
    rows, unavailable = _flake_rows(root)
    if unavailable:
        return None, unavailable
    counts = {}
    for row in rows:
        counts[row["tree"]] = counts.get(row["tree"], 0) + 1
    return counts, None


def record_flake(root, record):
    """(row, error): one FLAKED record for `record["tree"]`, naming the tests
    blame saw flake. The writer stamps the event; the caller does not."""
    tree = str(record.get("tree") or "")
    tests = record.get("tests")
    if not _TIP.match(tree):
        return None, "a flake records the red receipt's full tree"
    if not isinstance(tests, list) or not tests \
            or any(not isinstance(t, str) or not t for t in tests):
        return None, "a flake records the failing test ids"
    path = flakes_path(root)
    row = dict(record, event=FLAKED, tree=tree, tests=list(tests), v=1,
               ts=pk.now_ts(),
               id=binascii.hexlify(os.urandom(8)).decode("ascii"))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)
    with eventledger.locked(path) as held:
        if not held:
            return None, "the flake store %s is locked" % path
        ok, why = eventledger.append_unlocked_checked(path, row)
    return (row, None) if ok else (None, why)


def readmit(root, token, reason, by):
    """(row, error): clear the standing ejection of the one tip `token`
    names (a unique prefix), recording who cleared it and why."""
    token = str(token or "").strip().lower()
    if not _TIP_TOKEN.match(token):
        return None, "%r is not a tip (4 to 40 hex digits)" % token
    standing, why = read_ejections(root)
    if why:
        return None, why
    hits = [tip for tip in standing if tip.startswith(token)]
    if not hits:
        return None, ("no ejection stands on a tip %s: it was never ejected, "
                      "or it was readmitted already" % token)
    if len(hits) > 1:
        return None, "%s names %d ejected tips; give more of it" % (
            token, len(hits))
    ejected = standing[hits[0]]
    return _append_ejection(root, {
        "event": READMITTED, "tip": hits[0], "by": by, "reason": reason,
        "lr": ejected.get("lr"), "lane": ejected.get("lane"),
        "train": ejected.get("train"), "gate": ejected.get("gate"),
        "audits": ejected.get("audits")})


def ejection_reason(record):
    """The EXCLUDED line's clause for a car whose tip was ejected: from its
    red gate, from its train's red pre-gate audits (`audits`, the log
    `trainblame.audit_red` read), or from a hand `--drop` whose `reason`
    names why that car was kept out."""
    tests = [str(t) for t in record.get("tests") or ()]
    tip = _short(record.get("tip"))
    if record.get("audits"):
        where = "its pre-gate audits"
    elif record.get("gate"):
        where = "gate:%s" % record["gate"]
    elif record.get("reason"):
        where = "abandoned (%s)" % record["reason"]
    else:
        where = "gate:%s" % (record.get("gate") or "?")
    return ("ejected from %s (%s) at %s: %s%s; re-tip the lane or "
            "readmit this tip: `helm train readmit %s --reason ...`"
            % (record.get("train") or "?", where, tip,
               tests[0] if tests else "no failing test recorded",
               " (+%d more)" % (len(tests) - 1) if len(tests) > 1 else "",
               tip))


def approve_ready(lrs, identity, now=None):
    """(cars, excluded) for one project, cars in MERGE ORDER.

    A car is a LIVE row the projection calls READY, bound to this repository,
    with a reviewed tip — or a HELD source-clean row that
    `landreq.source_clean_car` admits, at its held tip — or an open review
    row nobody has read for LAND_FIRST_WAIT_S (`land_first_tip`), at its
    dispatched tip (`basis` names which).
    A row bound to ANOTHER repository is not this window's business and is
    not listed. A READY row with no binding at all is listed as excluded,
    because nothing places it here and merging it would make that claim for
    it; so is every held source-clean row the predicate refuses, with its
    reason.

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
    work in the order it was approved; a source-clean car is placed by the
    instant its hold was recorded. A row whose entry instant is unknown sorts
    last; the row id breaks ties so two runs agree.
    """
    cars, excluded, rows = [], [], None
    now = time.time() if now is None else now
    for lr in (lrs or {}).values():
        bound = lr.get("repo_id")
        if bound and bound != identity:
            continue
        # THE ONE PREDICATE, asked before the READY word because a held row
        # is never READY: (None, None) is a row it has nothing to say about.
        clean_tip, clean_why = landreq.source_clean_car(lr)
        if clean_tip or clean_why:
            car = {"id": lr.get("id") or "?", "lane": lr.get("lane") or "?",
                   "tip": clean_tip or lr.get("source_clean_tip") or "",
                   "entered": lr.get("hold_ts"), "lr": lr,
                   "basis": "source-clean"}
            if clean_why:
                # THE REFUSAL'S KIND RIDES WITH IT, so `render` can fold the
                # NO HOLDER backlog into one line without re-reading a hold.
                excluded.append(dict(car, why=clean_why,
                                     kind=getattr(clean_why, "kind", None)))
            else:
                cars.append(car)
            continue
        if not landreq.live_ready(lr):
            if bound:
                if rows is None:
                    # AN UNREAD LEDGER IS NO CAR: nothing shows the row unread.
                    fold, _verdicts, err = landreq._ledger_fold()
                    rows = {} if err else fold or {}
                tip = land_first_tip(lr, rows, now)
                if tip:
                    cars.append({"id": lr["id"], "lane": lr.get("lane") or "?",
                                 "tip": tip, "entered": lr.get("entered_ts"),
                                 "lr": lr, "basis": LAND_FIRST})
            continue
        car = {"id": lr.get("id") or "?", "lane": lr.get("lane") or "?",
               "tip": lr.get("reviewed_tip") or "",
               "entered": lr.get("entered_ts"), "lr": lr,
               "basis": "approved"}
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


def _bound_branch_tip(be, root, lr):
    """(sha, why). The tip of the branch this row bound at dispatch.

    The binding is ``ref_branch``, and only a ``refs/heads/`` ref is a
    branch. A missing binding, a ref that is not a local branch, or a ref
    git cannot read is UNKNOWN and the car does not ride. The lane label
    is not a branch name, so it is not consulted (task/3991)."""
    branch = (lr or {}).get("ref_branch")
    if not isinstance(branch, str) or not branch.startswith("refs/heads/") \
            or branch == "refs/heads/" or any(c.isspace() for c in branch):
        return None, ("the row bound no local branch, so whether its hold "
                      "is that branch's current tip is UNKNOWN and it is "
                      "not composed")
    rc, head, _err = be.text(
        root, "rev-parse", "--verify", "-q", branch + "^{commit}", env=_env())
    if rc != 0 or not head:
        return None, ("branch %s is not readable, so whether this hold is "
                      "its current tip is UNKNOWN and it is not composed"
                      % branch)
    return head, None


def plan(repo, trunk=None, name=None, project=None, max_behind=MAX_BEHIND,
         observe=True):
    """(plan, refusal) — everything this window would do, measured, with
    nothing minted or merged. The one write is the trunk authority's own
    bounded fetch of the declared ref (`trunk_authority`), the same one a
    build dispatch makes. `project` is the listing seam; its default is the real
    projection. `max_behind` is the drift cap (see the module docstring).
    `observe` False skips that fetch, and the plan's `authority` is None:
    only `car_admission` passes it, and it never renders or applies a plan."""
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
    authority = trunk_authority(root, identity) if observe else None
    ejected, ejections_unknown = read_ejections(root)
    lrs, unavailable = (project or landreq.project)()
    if unavailable:
        return None, "the land-request projection is unavailable: %s" \
            % unavailable
    ready, excluded = approve_ready(lrs, identity)
    cars, seen = [], {}
    for car in ready:
        tip = car["tip"]
        clean = car.get("basis") == "source-clean"
        first = car.get("basis") == LAND_FIRST
        # A SOURCE-CLEAN CAR HAS NO READY WORD TO READ: its admission is
        # `landreq.source_clean_car`, already asked, and its line says so.
        # Nor has a land-first car: nobody read it (`land_first_tip`).
        word = SOURCE_CLEAN if clean else LAND_FIRST_WORD if first \
            else landreq.ready_word(car["lr"])
        if word in DOOR_CAUTION:
            excluded.append(dict(car, why="is %s: %s. That is a door-caution "
                                          "rung of the READY word, and no "
                                          "land verb enforces it, so this "
                                          "verb does; `helm lr show %s` names "
                                          "why" % (word, DOOR_CAUTION[word],
                                                   _short(car["id"]))))
            continue
        reason = (_source_clean_reason(car["lr"]) if clean else
                  _land_first_reason(car["lr"]) if first else None)
        if word == UNVERIFIED:
            why, reason = _unverified(car["lr"])
            if why:
                excluded.append(dict(car, why=why))
                continue
        if ejected is not None and tip in ejected:
            # THE TIP, NOT THE LANE: a new tip on this lane rides again.
            excluded.append(dict(car, why=ejection_reason(ejected[tip])))
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
            excluded.append(dict(car, why=(
                "held tip %s is already on %s — `helm lr foldcheck %s --gate "
                "gate:<id> --apply` closes it on a verified whole-suite "
                "receipt containing it; do not compose it"
                % (_short(tip), ref, _short(sha))) if clean else
                "unread tip %s is already on %s with no verdict: its "
                "post-land read is owed by its reader %s; do not compose it"
                % (_short(tip), ref, car["lr"].get("reviewer") or "?")
                if first else
                "reviewed tip %s is already on %s — close it, do not compose "
                "it" % (_short(tip), ref)))
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
        # ONE CAR PER LANE. The car is the hold at the tip of the branch
        # the row bound. An older hold on that same branch is superseded
        # and is not composed (task/3991): merging both tips puts the
        # pre-rebase hold on the train, and that hold conflicts with trunk.
        branch_tip, branch_why = _bound_branch_tip(be, root, car.get("lr"))
        if branch_why:
            excluded.append(dict(car, why=branch_why))
            continue
        if tip != branch_tip:
            excluded.append(dict(car, why=(
                "superseded: branch %s's current tip is %s; this hold is %s "
                "and is not composed"
                % (car["lr"].get("ref_branch"), _short(branch_tip),
                   _short(tip)))))
            continue
        # A DOOR WAITS FOR ITS READ: only a lane `review_door.lane_doors`
        # finds no door in lands before review. Asked last, of the few cars
        # left, because it reads the lane's diff.
        doors = _land_first_doors(car["lr"]) if first else []
        if doors:
            excluded.append(dict(car, why=(
                "is unread and a DOOR (%s): a door lands only after its "
                "review" % ", ".join(doors))))
            continue
        seen[tip] = car["id"]
        # UNKNOWN, never a guess, when the store cannot be read: the car
        # stays listed and `--apply` refuses (`compose`).
        cars.append(dict(car, word=word, reason=reason, behind=drift,
                         ejection=UNKNOWN if ejections_unknown else None))
    if name is None:
        name, err = next_train(be, root, sha)
        if err:
            return None, err
    elif not _NAME.fullmatch(name):
        return None, ("train name %r is not a plain name (letters, digits, "
                      "dot, dash, underscore; it becomes a directory)" % name)
    return {"root": root, "identity": identity, "ref": ref, "trunk": sha,
            "authority": authority, "max_behind": max_behind,
            "ejections_unknown": ejections_unknown,
            "train": name, "room": _lanes.lane_path(root,
                                                    os.path.join(BOX, name)),
            "cars": cars, "excluded": excluded}, None


def car_admission(rid, project=None):
    """(root, tip, why) — would `helm train auto` take land-request row `rid`
    as a car now? THE PLANNER'S OWN ANSWER, never a second one: the row's
    projection is handed to `plan` itself, on the repository the row is bound
    to and with the default trunk and drift cap `helm train auto` plans with.
    So every rung `plan` asks (`landreq.source_clean_car`, the door-caution
    word, an ejected tip, a tip already on trunk, the drift cap, a superseded
    branch tip) is asked here exactly as the train asks it.

    A NOTICE IS NOT A PLAN, so two things are not done: no remote trunk
    authority is observed (that bounded fetch guards `--apply`, and a hold
    verb must not fetch), and no train name is read from trunk. The answer
    stands on the local trunk snapshot.

    `root` is None when the row's repository cannot be named. `tip` is the
    car's tip when the planner admits it; `why` is the planner's own
    exclusion otherwise. `project` is the listing seam, as for `plan`."""
    lrs, unavailable = (project or (
        lambda: landreq.project(selector=rid)))()
    if unavailable:
        return None, None, ("the land-request projection is unavailable: %s"
                            % unavailable)
    lr = next((r for r in (lrs or {}).values() if r.get("id") == rid), None)
    if lr is None:
        return None, None, ("the land-request projection carries no row %s"
                            % _short(rid))
    gitdir, why = landreq._close_repo(lr, None)
    if why:
        return None, None, why
    root = gitdir[:-5] if gitdir.endswith("/.git") else gitdir
    got, why = plan(root, name=NOTICE_NAME,
                    project=lambda: ({rid: lr}, None), observe=False)
    if why:
        return root, None, why
    for car in got["cars"]:
        if car["id"] == rid:
            return root, car["tip"], None
    for car in got["excluded"]:
        if car["id"] == rid:
            return root, None, str(car["why"])
    return root, None, "the planner does not list it as a car"


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
             "(--max-behind)" % (got["max_behind"], got["ref"])]
    if got.get("ejections_unknown"):
        lines.append("  ejections: UNKNOWN — %s; every car's ejection is "
                     "UNKNOWN and --apply refuses until the store reads"
                     % got["ejections_unknown"])
    lines.append(_order_header(got["cars"]))
    for i, car in enumerate(got["cars"], 1):
        lines.append("    %d. %s  lane %s  %s tip %s  %s%s  (%d behind)%s"
                     % (i, _short(car["id"]), car["lane"],
                        _TIP_WORD.get(car.get("basis"), "reviewed"),
                        _short(car["tip"]),
                        car["word"], " — %s" % car["reason"]
                        if car.get("reason") else "", car["behind"],
                        "  ejection %s" % car["ejection"]
                        if car.get("ejection") else ""))
    if not got["cars"]:
        lines.append("    (none)")
    unheld = 0
    for car in got["excluded"]:
        if car.get("kind") == landreq.SourceCleanRefusal.NO_HOLDER:
            unheld += 1
            continue
        lines.append("  EXCLUDED %s (lane %s): %s"
                     % (_short(car["id"]), car["lane"], car["why"]))
    if unheld:
        lines.append(_no_holder_line(unheld))
    return "\n".join(lines)


def _no_holder_line(n):
    """THE NO HOLDER BACKLOG AS ONE LINE (the author's ruling 6, round 4).

    Every source-clean hold written before holds stamped their holder records
    nobody — every held source-clean row on the live ledger, forty-odd
    when measured — and each is refused by the same rung for the same reason
    with the same cure: its recipient releases and re-holds it. One EXCLUDED line apiece buried the train's own plan under
    them, so they are counted here once, with the verb that lists them.
    Every other exclusion keeps its own line: each names a different cure."""
    if n == 1:
        return ("  EXCLUDED — 1 held source-clean row carries NO HOLDER and "
                "cannot ride until its recipient re-holds; list it: `%s`"
                % NO_HOLDER_LIST)
    return ("  EXCLUDED — %d held source-clean rows carry NO HOLDER and cannot "
            "ride until each recipient re-holds; list them: `%s`"
            % (n, NO_HOLDER_LIST))


def _order_header(cars):
    """The merge-order line. Unchanged for a train of approve-ready rows; a
    train carrying source-clean cars counts both kinds, because an
    approve-ready count that included them would claim an approve nobody
    wrote. A land-first car is counted as one too, for the same reason."""
    clean = sum(1 for car in cars if car.get("basis") == "source-clean")
    first = sum(1 for car in cars if car.get("basis") == LAND_FIRST)
    if not clean and not first:
        return "  merge order, %d approve-ready row%s:" % (
            len(cars), "" if len(cars) == 1 else "s")
    return "  merge order, %d car%s (%d approve-ready, %d source-clean%s):" % (
        len(cars), "" if len(cars) == 1 else "s", len(cars) - clean - first,
        clean, ", %d %s" % (first, LAND_FIRST_MARK) if first else "")


def _source_clean_loop(cars, out):
    """After the gate and the land, the close each source-clean car owes —
    printed, never run: the land is the integrator's, and so is this."""
    clean = [car for car in cars if car.get("basis") == "source-clean"]
    if clean:
        print("  source-clean car%s %s: once this train's whole-suite gate "
              "passes and the train lands, `helm lr foldcheck <landed head> "
              "--gate gate:<its receipt> --apply` closes %s as "
              "source-clean-landed (no approve is minted)"
              % ("" if len(clean) == 1 else "s",
                 ", ".join(_short(car["id"]) for car in clean),
                 "it" if len(clean) == 1 else "each"), file=out)


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

    A car's `detail`, when it has one, rides in the subject after the lane,
    in parentheses (`<train>: merge lane <lane> (<detail>)`), the shape the
    integrator's hand-written merges take and `trainblame` reads; the body's
    `land request` line is unchanged either way.
    """
    why = room_refusal(be, room, identity)
    if why:
        return STUCK, "the room assertion refused the merge: " + why, None
    env = dict(_env(), **MERGE_ENV)
    rc, before, err = be.text(room, "rev-parse", "--verify", "-q", "HEAD",
                              env=env)
    if rc != 0 or not before:
        return STUCK, "the room's HEAD is unreadable (%s)" % _first(err), None
    tip = car["tip"]
    if be.ancestry(room, tip, before) == vcs.ANCESTOR:
        return CONTAINED, "the room already holds %s" % _short(tip), before
    message = "%s: merge lane %s" % (train, car["lane"])
    detail = " ".join(str(car.get("detail") or "").split())
    if detail:
        message = "%s (%s)" % (message, detail)
    rc, out, err = be.text(
        room, *NO_RERERE, "merge", "--no-ff", "--no-edit", "--no-log",
        "-m", message, "-m", "land request %s, %s tip %s"
        % (car["id"], _BODY_WORD.get(car.get("basis"), "reviewed"), tip),
        tip, env=env,
        timeout=MERGE_TIMEOUT_S)
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
        _source_clean_loop(got["cars"], out)
        print("  dry run: nothing minted, merged or launched. `helm train "
              "--apply` composes this train and gates it through `helm gate "
              "window launch`.", file=out)
        return 0
    # ONE TRAIN IN FLIGHT AT A TIME, shared with `helm train auto`: while its
    # train is composing, gating, landing or STOPPED, a hand-composed train
    # would race it for the same cars, the same window and the same trunk
    # (a stopped train still holds its room and cars). The compose
    # lock is held from this check through the launch, so auto-land cannot
    # begin a compose between them (`autoland.compose_lock`).
    from . import autoland
    with autoland.compose_lock(got["root"], COMPOSE_LOCK_WAIT_S) as held:
        if not held:
            print("%s: REFUSED — another train is being composed (the compose "
                  "lock stayed held for %d s). Nothing was minted, merged or "
                  "launched." % (PROG, COMPOSE_LOCK_WAIT_S), file=out)
            return 1
        flying = autoland.flight_refusal(got["root"])
        if flying:
            print("%s: REFUSED — %s. Nothing was minted, merged or launched."
                  % (PROG, flying), file=out)
            return 1
        return _compose_applied(got, out, door)


def _compose_applied(got, out, door):
    """`compose --apply` past the flight check: the trunk authority, the
    ejection store and the cars, then the room. -> exit code."""
    stale = authority_refusal(got)
    if stale:
        print("%s: REFUSED — %s. Nothing was minted, merged or launched."
              % (PROG, stale), file=out)
        return 1
    if got.get("ejections_unknown"):
        print("%s: REFUSED — %s, so nothing can prove that no ejected car is "
              "in this train. Nothing was minted, merged or launched."
              % (PROG, got["ejections_unknown"]), file=out)
        return 1
    if not got["cars"]:
        print("%s: nothing is approve-ready or held source-clean in this "
              "project, so no room was minted and nothing was launched"
              % PROG, file=out)
        return 1
    return compose_room(got, out=out, door=door)


def mint_and_merge(got, out=None):
    """Mint `got`'s room on its trunk and merge every car in order, launching
    nothing. -> {"head", "merged", "refused", "stuck"}.

    THE COMPOSE HALF OF `compose_room`, apart so a caller can stand work
    between the merges and the gate: `helm train auto` (helm/autoland.py)
    runs the tree-wide audits on the composed room there, then launches the
    same door. `head` is the room's head after the last merge (the trunk when
    nothing merged); `merged` the cars merged, `refused` (car, cause) for
    each merge that was aborted and NOT resolved; `stuck` the sentence that
    stopped the train, None when it did not stop. `got` is `compose_room`'s."""
    out = out if out is not None else sys.stdout
    room, root = got["room"], got["root"]
    result = {"head": None, "merged": [], "refused": [], "stuck": None}
    if os.path.lexists(room):
        result["stuck"] = ("%s already exists. Its HEAD may be the only "
                           "anchor of an earlier train: land it or remove "
                           "it, or name another train with --name." % room)
        print("%s: REFUSED — %s" % (PROG, result["stuck"]), file=out)
        return result
    be = vcs.backend(root)
    os.makedirs(os.path.dirname(room), exist_ok=True)
    # THE SAME SANCTION `helm lr compose` HANDS THE REF GUARD: minting an
    # integration room from the shared checkout is the integrator's act, and
    # the bit is scoped to this one call, never set on the process.
    rc, _out, err = be.text(root, "worktree", "add", "--detach", room,
                            got["trunk"],
                            env=dict(_env(), HELM_WORK_INTEGRATOR="1"))
    if rc != 0:
        result["stuck"] = "cannot mint the room %s: %s" % (room, _first(err))
        print("%s: %s" % (PROG, result["stuck"]), file=out)
        return result
    result["head"] = got["trunk"]
    for car in got["cars"]:
        outcome, detail, head = merge_car(be, room, car, got["train"],
                                          identity=got["identity"])
        label = "%s (lane %s)" % (_short(car["id"]), car["lane"])
        if outcome == STUCK:
            result["stuck"] = "%s: %s" % (label, detail)
            print("  STOPPED at %s: %s.\n  The room %s is left exactly as it "
                  "stands, and nothing was launched." % (label, detail, room),
                  file=out)
            return result
        if outcome == REFUSED:
            result["refused"].append((car, detail))
            print("  REFUSED %s: %s. The merge was aborted and NOT resolved; "
                  "this lane owes a tip that merges onto %s."
                  % (label, detail, _short(got["trunk"])), file=out)
        elif outcome == CONTAINED:
            print("  CONTAINED %s: %s" % (label, detail), file=out)
        else:
            result["merged"].append(car)
            result["head"] = head
            print("  MERGED %s at %s: %s" % (label, _short(head), detail),
                  file=out)
    return result


def compose_room(got, out=None, door=None):
    """Mint `got`'s room on its trunk, merge every car in order, and launch
    the gate through the door. Returns the exit code `compose` documents.

    THE ONE COMPOSE PATH. `compose` reaches it after its plan and its trunk
    authority check; `helm train blame` (helm/trainblame.py) reaches it to
    compose a red train again without the car it ejected, so a train and its
    ejection are minted, merged and gated by the same code. `got` carries
    `root`, `identity`, `trunk` (the sha the room stands on), `train`, `room`
    and `cars` (each with `id`, `lane`, `tip` and its `basis`)."""
    out = out if out is not None else sys.stdout
    room = got["room"]
    composed = mint_and_merge(got, out=out)
    if composed["stuck"]:
        return 1
    merged, refused = composed["merged"], composed["refused"]
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
    _source_clean_loop(merged, out)
    return 1 if refused else 0


def cmd_train(args):
    """helm train — the landing window composes itself (dry run by default).
    `helm train blame <room>` names a red train's culprit (helm/trainblame.py);
    `helm train auto` drives a train from intent to land by itself, and `helm
    train veto <train>` stops one inside its window (helm/autoland.py).
    """
    from .cli import guard_tail
    if args and args[0] == "blame":
        from . import trainblame
        return trainblame.cmd(args[1:])
    if args and args[0] == "readmit":
        return _cmd_readmit(args[1:])
    if args and args[0] in ("auto", "veto"):
        from . import autoland
        return (autoland.cmd if args[0] == "auto"
                else autoland.cmd_veto)(args[1:])
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


def _cmd_readmit(args):
    """helm train readmit <tip> --reason TEXT [--repo PATH] — clear one
    standing ejection by hand, for a blame that was wrong. The record keeps
    who cleared it (the seat's HELM_CHAT_NAME, else the OS user) and why."""
    from .cli import guard_tail
    prog = "helm train readmit"
    args = list(args or ())
    if args and args[0] in ("-h", "--help"):
        print(READMIT_USAGE)
        return 0
    if not args or args[0].startswith("-"):
        print("%s: name the ejected tip (%s)" % (prog, READMIT_USAGE),
              file=sys.stderr)
        return 2
    token, rest = args[0], args[1:]
    rc = guard_tail(prog, rest, valued=("--reason", "--repo"),
                    usage=READMIT_USAGE)
    if rc is not None:
        return rc
    opts = {a: rest[i + 1] for i, a in enumerate(rest)
            if a in ("--reason", "--repo")}
    reason = " ".join(str(opts.get("--reason") or "").split())
    if not reason:
        print("%s: a readmit records why the ejection was wrong: --reason "
              "TEXT (%s)" % (prog, READMIT_USAGE), file=sys.stderr)
        return 2
    repo = opts.get("--repo") or os.getcwd()
    root = _lanes.find_root(repo)
    if not root:
        print("%s: %s is not inside a git repository" % (prog, repo),
              file=sys.stderr)
        return 1
    by = home.chat_name() or "user:%s" % getpass.getuser()
    row, why = readmit(root, token, reason, by)
    if why:
        print("%s: REFUSED — %s" % (prog, why), file=sys.stderr)
        return 1
    print("%s: %s rides again (it was ejected from %s, gate:%s); recorded "
          "by %s: %s" % (prog, _short(row["tip"]), row.get("train") or "?",
                         row.get("gate") or "?", by, reason))
    return 0
