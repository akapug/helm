"""THE ONE WORK READER (task/3643, slice 1): one card per piece of work, from
the to-do list to the land, BUILT ON the one pipeline reading (task/3631).

The owner's two pages, Backlog and Pipeline, count the same work two ways and
cannot meet: helm records no task on a work room. This module is the server
half of the one Work page (`GET /api/work`): it joins the to-do ledger, the
pipeline reading, the work rooms, the train files and the land log, and
returns ONE card per piece of work in ONE of five stages (To do, Building, In
review, Landing, Landed), with whose move it is, how long it has been there,
the stages it reached, how often it came back, how it landed, and the counts,
computed once here so no surface counts on its own.

NEVER A SECOND COUNTER. The pipeline rows are the rows the one reading counts:
`pipe_counts` (task/3631) sums the per-project tallies
(`web_board._kanban_tally`) over the cards `/api/board` sends, and it is the
ONLY pipeline count: the page's twin of it, `pipeCounts`, was retired with
the land board (task/3643, slice 6), and the Work page, the nav badge, Home's
tile and every project's row read this body instead. Every row that reading
returns (the live cards it counts and the rows its census folds onto count
lines) is TYPED exactly once, below, and the build asserts the partition.

A TYPED SETTLEMENT PER ROW, WITH ITS PROVENANCE (the design meld's ruling on
paperwork). `settle` answers `active` (somebody owes a move on the work),
`fix-owed` (the work is proved on trunk while a FIX verdict on it stood
unanswered: a debt, drawn in Building), or `record` (a PROVED discharge: a
successor proved to carry the row). ON TRUNK, A CENSUS FOLD AND A LATER
LANDED ROUND ARE EVIDENCE, NEVER A DISMISSAL: the FIX question is asked
first, so a landed, unanswered FIX is `fix-owed` even where the census folds
it off the frontier (the prototype asked "on trunk?" first and filed such a
row as a record); a later landed round of the same lane proves nothing about
it (the prototype's `chain_landed` took any); a landing the census cannot
place for THIS row is unknown, neither a land nor a debt; and a clean hold
whose work landed still owes its close or re-hold, a live move.

AGE NEVER RETIRES ANYTHING. Twenty-four hours with nobody's move is a STUCK
mark on the card (`view`); an undecided review stays in review and stuck
until a typed action closes it.

ONE CARD PER TASK, SEVERAL OWED MOVES. Within one chain only its frontier
round is live (the reading sends frontiers only). Across the independent rooms
of one task every live row, and every claimed room with no round sent, is an
ACTION with its own whose-move; the card's
stage is its PRIMARY action's, the least advanced one, a fix debt first, so a
landed FIX plus a newer review reads Building with the review as a secondary
action (task/3538's shape; the prototype drew it In review).

A SOURCE NOT READ IS NEVER A ZERO (task/3632). A PIPELINE stage count over a
scope with an unread or unreadable project, roster or land source is a floor
(`at_least`) or unknown (`None`), never a complete-looking number; To do over
a project whose pipeline was not read is the opposite, a CEILING (`at_most`):
its to-do items may be work already under way that the reading could not
see. The to-do list itself unread makes To do unknown. The scope names what
was not read.

THE JOIN IS THE ONE JOIN (`taskkey.join`, task/3703). Every piece of work
goes through `join_work`, which asks it and then checks the task's own
project: a stored key (the chain's first row's task, the lane's record in
the train project's repository, a train car's recorded task), else a lane
that cites exactly one task (`task/N` or `task-N`) that the to-do list
holds, of the work's own project, joins; anything else is UNKNOWN with its
reason, never a guess from a trailing number. A land is joined by what was
recorded at it, never by today's lane records. Every card says how it was
linked (`link`).

ONE SNAPSHOT PER REVISION. `snapshot` keys its build by a revision marker over
every input (`revision`: the pipeline reading's read stamps, the project
registry, the identities of the ledgers, and a five-minute tick for the
Landed window), builds once per change, and keeps the last two, so the
page's 45 s poll costs a board read and a stat. A card's crossings
(`card_events`) come from the SAME snapshot, computed when a drawer first
asks and kept on it; a drawer for a reading no longer kept is told so.

READ-ONLY on every source. No note, brief, reason or comment text is copied:
only ids, titles, seats, states, ranks and times.
"""
import calendar
import datetime
import hashlib
import os
import re
import threading
import time

STAGES = ("todo", "building", "review", "landing", "landed")
STAGE_I = {s: i for i, s in enumerate(STAGES)}
PIPELINE = ("building", "review", "landing")

ACTIVE, FIX_OWED, RECORD = "active", "fix-owed", "record"
SETTLEMENTS = (ACTIVE, FIX_OWED, RECORD)

DAY_S = 86400
#: nobody's move for longer than this is a STUCK mark, never a retirement
STUCK_S = DAY_S
#: a task handed back to the pool inside this window came back to To do
RELEASED_S = 7 * DAY_S
#: the Landed window
LANDED_S = DAY_S
#: the revision's clock step: the Landed window moves in steps of this size
TICK_S = 300
#: how many snapshots are kept, so a drawer asked for by revision reads the
#: snapshot its page was drawn from
KEEP = 2

#: THE OWNER'S CLOCK for the Today count. helm configures no owner zone (no
#: setting, registry field or environment name carries one), so the owner's
#: own zone is named here.
OWNER_ZONE = "America/Los_Angeles"

#: THE COLUMN A LIVE ROW'S STATE IS IN — the retired land board's
#: `KANBAN_OF`, with its "gate" column named as the stage it is.
STAGE_OF = {"OPEN": "review", "AWAITING_REVIEW": "review",
            "AWAITING_BUILD": "building", "REVIEWED": "review",
            "CHANGES_REQUESTED": "building", "READY": "landing",
            "MERGED_LOCAL": "landing"}

#: the words the reading's `holder` uses for a role no seat was named for
ROLES = frozenset(("integrator", "lander", "author", "reviewer", "builder",
                   "owner"))

#: the census reasons that PROVE a folded row's work reached trunk
_LANDED_REASONS = ("landed-by-ancestry", "landed-by-patch-id")
_ABANDONED_REASON = "abandoned-unreachable"
_MARKS = ("moving", "nonbillable", "stalled", "contrary")


# --------------------------------------------------------------- the join

def join_work(project, ledger, lane=None, row=None, current=None,
              lanes=None, recorded=(), unread=None):
    """{task, how, why}: THE ONE JOIN's answer (`taskkey.join`, task/3703)
    for one piece of work of `project`, CHECKED against the task's own
    project. A task of another project, or one with no project, is UNKNOWN
    (a contradictory or missing project mapping never joins).

    `lanes` is the lane records of the repository the work is in
    (`taskkey.lane_records`), or None where none apply: another project's
    work, or a land, which is joined by what was recorded at it. `unread` is
    why those records could not be read; the answer for work in a lane is
    then UNKNOWN, as the one join's own is, since a record may name another
    task."""
    from . import taskkey, tasks            # DEFERRED — the join, the ledger
    named = lane or (row or {}).get("lane")
    if unread and named:
        return {"task": None, "how": "unknown",
                "why": "the record of lane %s could not be read (%s)"
                       % (taskkey.lane_name(named), unread)}
    key = taskkey.join(lane=lane, row=row, current=current,
                       lanes=False if lanes is None else lanes,
                       recorded=recorded, known=ledger)
    if key.task is None:
        return {"task": None, "how": "unknown", "why": key.why}
    owner = tasks.project_of_row((ledger or {}).get(key.task) or {})
    if owner is None:
        return {"task": None, "how": "unknown",
                "why": "%s has no project, so the join cannot be checked"
                       % key.task}
    if project is not None and owner != project:
        return {"task": None, "how": "unknown",
                "why": "%s is %s's, and the work is %s's" % (key.task, owner,
                                                              project)}
    return {"task": key.task, "how": key.via, "why": None}


# ------------------------------------------- the one pipeline reading, served

def _sec(board, name):
    """The page's `boardSec`: one section, or None before the first read."""
    if isinstance(board, dict) and board.get("unavailable"):
        return {"unavailable": board["unavailable"]}
    sections = board.get("sections") if isinstance(board, dict) else None
    return (sections or {}).get(name) or None


def _sec_state(sec):
    """The page's `boardSecState`."""
    if not sec:
        return "unread"
    if sec.get("unavailable"):
        return "unknown"
    if sec.get("loading"):
        return "loading"
    return "stale" if sec.get("stale") else "ok"


def _pkey(key, rec):
    """The page's `pkey`: the light's key, else the name, else the key."""
    light = rec.get("light") if isinstance(rec, dict) else None
    return (light or {}).get("key") or (rec or {}).get("name") or key


def _mark(card):
    """The page's `lbMark`: the mark the server named, else the card's own."""
    return card.get("mark") or ("contrary" if card.get("contrary") else
                                "stalled" if card.get("stalled") else
                                "moving")


def _pipe(board, key):
    """(section, its state, the project's pipeline record) — the scoped
    project's from the lands leg, every other project's from the fleet read."""
    lands = _sec(board, "lands")
    mine = bool(lands) and lands.get("scope") == key
    sec = lands if mine else _sec(board, "fleet")
    rec = ((board or {}).get("projects") or {}).get(key) \
        if isinstance(board, dict) else None
    pipe = (rec or {}).get("lanes" if mine else "pipeline")
    return sec, _sec_state(sec), pipe if isinstance(pipe, dict) else None


def _sent(pipe):
    """Every live card one project's pipeline sent: the listed ones, the ones
    the server's cap cut, and the re-hold rows — the rows its tally counts."""
    rows = list(pipe.get("loops") or ()) + list(pipe.get("loops_cut") or ())
    rows += [r.get("lr") for r in ((pipe.get("rehold") or {}).get("rows")
                                   or ()) if isinstance(r, dict)]
    return [r for r in rows if isinstance(r, dict)]


def pipe_counts(projects, board, project=""):
    """THE ONE PIPELINE READING (task/3631), and the only one: every surface
    that prints a pipeline number reads this, through /api/work (task/3643).

    `projects` is the registry's {key: record} (a retired one is skipped);
    `project` "" is every project. The live rows are the per-project tallies
    the server counted (`web_board._kanban_tally`), summed; a project still
    being read is `unread`, one that could not be read `unknown` (the first
    reason in `why`), and neither is in any number. ->
    {scope, live, marks, alarm, holders, items, tallies, counted, unread,
    unknown, shown, why, age_s, stale, limit_s, read, complete}."""
    out = {"scope": project or "", "live": 0,
           "marks": dict.fromkeys(_MARKS, 0), "alarm": 0, "holders": {},
           "items": [], "tallies": {}, "counted": [], "unread": [],
           "unknown": [], "shown": [], "why": None, "age_s": None,
           "stale": False, "limit_s": None, "read": False,
           "complete": False}
    ageless = False
    for key, rec in (projects or {}).items():
        if isinstance(rec, dict) and rec.get("retired"):
            continue
        k = _pkey(key, rec)
        if project and k != project:
            continue
        sec, st, pipe = _pipe(board, k)
        if st == "unknown":
            out["unknown"].append(k)
            out["why"] = out["why"] or str(sec.get("unavailable"))
            continue
        if st not in ("ok", "stale") or pipe is None:
            out["unread"].append(k)
            continue
        sent = _sent(pipe)
        tally = pipe.get("tally")
        if not isinstance(tally, dict):
            # an older server sent no tally: counted off the cards it sent
            tally = {"live": 0, "marks": dict.fromkeys(_MARKS, 0),
                     "holders": {}}
            for c in sent:
                if not c.get("lane"):
                    continue
                tally["live"] += 1
                tally["marks"][_mark(c)] = tally["marks"].get(_mark(c), 0) + 1
                who = c.get("holder") or "unknown"
                tally["holders"][who] = tally["holders"].get(who, 0) + 1
            out["shown"].append([k, tally["live"]])
        for c in sent:
            age = c.get("age_s")
            out["items"].append({
                "project": k, "id": c.get("id"), "lane": c.get("lane"),
                "stage": {"READY": "gate", "MERGED_LOCAL": "gate"}.get(
                    c.get("state"), STAGE_OF.get(c.get("state"), "review")),
                "state": c.get("state"), "mark": _mark(c),
                "holder": c.get("holder") or "unknown",
                "age_s": age if isinstance(age, (int, float))
                and not isinstance(age, bool) else None,
                "task": c.get("task") or None, "tip": c.get("tip") or None})
        out["tallies"][k] = tally
        out["counted"].append(k)
        out["live"] += tally.get("live") or 0
        for m in _MARKS:
            out["marks"][m] += (tally.get("marks") or {}).get(m) or 0
        for who, n in (tally.get("holders") or {}).items():
            out["holders"][who] = out["holders"].get(who, 0) + n
        age = sec.get("age_s")
        if not isinstance(age, (int, float)) or isinstance(age, bool):
            ageless = True
        elif out["age_s"] is None or age > out["age_s"]:
            out["age_s"] = age
        if st == "stale":
            out["stale"] = True
            out["limit_s"] = sec.get("limit_s")
    if ageless:
        out["age_s"] = None
    out["alarm"] = out["marks"]["stalled"] + out["marks"]["contrary"]
    out["read"] = bool(out["counted"])
    out["complete"] = out["read"] and not out["unread"] \
        and not out["unknown"]
    return out


def pipe_rows(board, key):
    """[(card, fold class or None, fold line)] — EVERY row one project's
    reading returned: the live cards its tally counts, then each row its
    census folded onto a count line (on main with no verdict, off the live
    frontier, unplaceable, absorbed by a later round)."""
    _sec_, _st, pipe = _pipe(board, key)
    if pipe is None:
        return []
    out = [(c, None, None) for c in _sent(pipe)]
    lines = [pipe.get("on_main")] + list(pipe.get("collapsed") or ())
    for line in lines:
        if not isinstance(line, dict):
            continue
        for c in line.get("rows") or ():
            if isinstance(c, dict):
                out.append((c, line.get("class"), line))
    return out


# ------------------------------------------------------------ settlement

def _fix_stands(c):
    """Does a FIX (or SUPERSEDE) verdict stand on this row?"""
    return c.get("state") == "CHANGES_REQUESTED" \
        or c.get("polarity") in ("fix", "supersede")


def _carried(c, carried):
    """The land of the train that pushed THIS request at its own tip
    ({train, n, at, tip}), or None. `carried` is {request id: land} over the
    pushed trains (`build`). The row's own tips are the reviewed one (`tip`)
    and, for a SOURCE-CLEAN hold, the held one the train merges
    (`source_clean_tip`, which may be a cure round's later commit); a car at
    neither is another round of the lane, and proves nothing about this
    row."""
    got = (carried or {}).get(c.get("id"))
    if not got:
        return None
    theirs = str(got.get("tip") or "")
    mine = [t for t in (str(c.get("tip") or ""),
                        str(c.get("source_clean_tip") or "")) if t]
    if mine and theirs and not any(theirs.startswith(t) or t.startswith(theirs)
                                   for t in mine):
        return None
    return got


def _pushed_words(land):
    """"train483 pushed it as LAND 508": a train's land of one request."""
    return "%s pushed it%s" % (land["train"], " as LAND %s" % land["n"]
                               if land.get("n") else "")


def _landed_evidence(c, line, carried=None):
    """(landed, how) — whether this row's own work is known to be on trunk:
    True with how it is known; False with how it is known NOT to be (or no
    `how` when nothing says either); None, UNKNOWN, with why. A census line
    off the frontier proves a land only when every row on it landed: a line
    that mixes landed rows with abandoned ones, or names no reason, does not
    say which this row is. A train that pushed this very request at its tip
    (`carried`, `_carried`) is a land."""
    if c.get("contrary"):
        return True, "contrary: the work %s while the verdict stood (%s)" % (
            "landed" if c.get("contrary_state") == "landed"
            else "merged locally" if c.get("contrary_state") == "merged-local"
            else "reached trunk", c.get("contrary_provenance") or "unstated")
    if c.get("trunk_contains_tip") is True:
        return True, "trunk contains the row's tip"
    if carried:
        return True, _pushed_words(carried)
    if (line or {}).get("class") == "off_frontier":
        reasons = sorted((line.get("by_reason") or {}))
        if reasons and all(r in _LANDED_REASONS for r in reasons):
            return True, "the census placed it off the frontier as landed " \
                "(%s)" % ", ".join(reasons)
        if reasons == [_ABANDONED_REASON]:
            return False, "the census placed it off the frontier as " \
                "abandoned, not landed"
        return None, "unknown: the census placed it off the frontier on a " \
            "line that %s, so whether this row landed is not measured" % (
                "mixes %s" % ", ".join(reasons) if reasons
                else "names no reason")
    return False, None


def settle(c, fold=None, line=None, carried=None):
    """{type, why, evidence} — the row's TYPED settlement, with provenance.

    ONLY A PROVED DISCHARGE SETTLES. A row is a `record` when a successor
    was proved to carry it (`contrary_discharge` a/b, the row is the
    confirmation round itself, c, or the succession walk MOVED it onto a
    landed carrier), or when a train pushed THIS request at its own tip
    (`carried`, the train's land from `_carried`): its move, the train's,
    is made, whatever the pipeline reading still says of it (task/3723:
    walk 3 saw a request train483 had landed wait "for the train" 13
    minutes). Nothing else closes a row: a census fold (`fold`, its
    line's class), on trunk, a later landed round of the same lane and age
    are evidence, carried in `evidence`, never a settlement.

    THE FIX QUESTION IS ASKED FIRST. A FIX standing on work proved to be on
    trunk is `fix-owed`; a FIX whose landing is unknown or absent is
    `active`, the author's move, and an unknown landing says so and claims
    no land. A clean hold whose work landed owes a live close (the
    integrator's) or re-hold (its recipient's): `active`. Everything else
    is `active` too — whatever its fold or its age."""
    ev = {"state": c.get("state"), "fold": fold}
    discharge = c.get("contrary_discharge")
    succession = c.get("succession_state")
    if _fix_stands(c):
        ev["verdict"] = c.get("polarity") or "fix"
        if discharge in ("a", "b", "c"):
            ev["discharge"] = discharge
            return {"type": RECORD, "why": "fix answered by a proved "
                    "successor", "evidence": ev}
        if succession == "moved":
            ev["succession"] = succession
            return {"type": RECORD, "why": "a proved successor carried it",
                    "evidence": ev}
        landed, how = _landed_evidence(c, line, carried)
        if how:
            ev["landed"] = how
        if landed:
            ev.update(discharge=discharge or "none", succession=succession)
            return {"type": FIX_OWED, "why": "the work landed while the fix "
                    "stood unanswered", "evidence": ev}
        return {"type": ACTIVE, "why": "a fix is asked for", "evidence": ev}
    if succession == "moved":
        ev["succession"] = succession
        return {"type": RECORD, "why": "a proved successor carried it",
                "evidence": ev}
    ev["on_trunk"] = c.get("trunk_contains_tip")
    if c.get("owes_rehold") or c.get("source_clean_on_main"):
        ev["owed"] = c.get("source_clean_on_main")
        return {"type": ACTIVE, "why": "its clean hold's work landed; its "
                "%s is owed" % ("re-hold" if c.get("owes_rehold")
                                else "close"), "evidence": ev}
    if carried:
        ev.update(train=carried["train"], land=carried.get("n"),
                  landed=_pushed_words(carried))
        return {"type": RECORD, "why": "%s; the pipeline reading still "
                "shows its request open" % _pushed_words(carried),
                "evidence": ev}
    return {"type": ACTIVE, "why": "a move is owed on the work",
            "evidence": ev}


def _whose(c, stage, sub, train_project, project):
    """{kind, who, why}: whose move one row waits on, off the holder the
    reading counted it under (`web_board._holder`)."""
    holder = str(c.get("holder") or "unknown")
    if stage == "landing" and project == train_project \
            and sub not in ("close-owed", "rehold-owed"):
        return {"kind": "train", "who": None,
                "why": "the auto-land train takes it on its next tick"}
    if sub == "undecided":
        pol = c.get("polarity")
        why = {"concur": "the review said “agree” only, which "
                         "lets nothing land",
               "approve": "approved, but by a reader whose approval "
                          "cannot land it"}.get(pol, "the review said "
                                                     "neither yes nor no")
        return {"kind": "nobody", "who": None, "why": why}
    if holder in ("unknown", "nobody", ""):
        return {"kind": "nobody", "who": None,
                "why": "the pipeline names nobody (%s)"
                       % (c.get("owed_by") or "no holder")}
    if holder in ROLES:
        return {"kind": "role", "who": holder,
                "why": "the pipeline names the %s role, not a seat" % holder}
    return {"kind": "seat", "who": holder, "why": None}


def _row_stage(c, settlement):
    """(stage, sub) for one row that is not a record."""
    state = c.get("state")
    if settlement == FIX_OWED:
        return "building", "fix-owed"
    if state == "AWAITING_BUILD":
        return "building", "asked"
    if state == "CHANGES_REQUESTED" or _fix_stands(c):
        return "building", "fix"
    if c.get("owes_rehold"):
        return "landing", "rehold-owed"
    if c.get("source_clean_on_main"):
        return "landing", "close-owed"
    if state == "REVIEWED":
        return "review", "undecided"
    if state == "READY":
        return "landing", "approved"
    if state == "MERGED_LOCAL":
        return "landing", "merged"
    if state == "AWAITING_REVIEW" and c.get("owed_by") == "integrator":
        # a source-clean hold: the reviewer's structured claim that the
        # review is over, owed to the integrator's land gate
        return "landing", "clean"
    return STAGE_OF.get(state, "review"), "wait"


# ------------------------------------------------------------- the ledgers

def _epoch(ts):
    """An epoch for a stamp (a number, or ISO-8601 UTC with a Z), or None."""
    if isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        return float(calendar.timegm(time.strptime(str(ts),
                                                   "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return None


_TASK_KEEP = ("ts", "last_updated", "status", "owner", "priority", "origin",
              "project", "ranked_by", "ranked_at", "rank_action")


def _task_snap(row):
    """The fields of one task-ledger event a crossing is read off — no title,
    note or comment."""
    s = {k: row.get(k) for k in _TASK_KEEP}
    sd = row.get("standdown") if isinstance(row.get("standdown"), dict) \
        else None
    s["standdown"] = {"ts": sd.get("ts"), "until": sd.get("until"),
                      "by": sd.get("by")} if sd else None
    rl = row.get("released") if isinstance(row.get("released"), dict) \
        else None
    s["released"] = {"by": rl.get("by"), "ts": rl.get("ts")} if rl else None
    m = re.match(r"\s*LANDED\s+(train\d+[a-z]?)",
                 str(row.get("closed_reason") or ""))
    s["train"] = m.group(1) if m else None
    return s


def read_tasks(path=None):
    """{rows, history, unavailable}: ONE read of the to-do ledger — every
    row's latest state and every event it kept, in ledger order."""
    from . import tasks                     # DEFERRED — the ledger module
    history = {}

    def accept(row, _prior):
        history.setdefault(str(row.get("id")), []).append(_task_snap(row))
        return True
    rows, why = tasks.snapshot(path, accept=accept)
    return {"rows": rows if not why else {}, "history": history if not why
            else {}, "unavailable": str(why) if why else None}


def read_dispatch(path=None):
    """{rows, events, chains, unavailable}: ONE read of the dispatch ledger,
    compact — each round's id, time, kind, lane, chain, sender and
    recipient, and each event on it as [epoch, code, ...]."""
    from . import dispatches, eventledger   # DEFERRED — heavy
    got, why = eventledger.checked_events(path or dispatches.ledger_path())
    if why:
        return {"rows": {}, "events": {}, "chains": {},
                "unavailable": str(why)}
    rows, events, chains = {}, {}, {}
    codes = {"cancel": ["Y"], "retire": ["T"], "superseded": ["E"],
             "release": ["k"]}
    for e in got:
        rid = e.get("id")
        if not isinstance(rid, str) or not rid:
            continue
        kind = e.get("event") or "dispatch"
        at = _epoch(e.get("ts"))
        if kind == "dispatch":
            chain = e.get("chain_root") or rid
            # the fields a crossing is drawn from, and the ones the join
            # reads off a chain's first row (its lane, stored task and
            # repository) — never the note or brief
            rows[rid] = {"id": rid, "ts": at,
                         "kind": e.get("kind") or "review",
                         "sender": e.get("sender"),
                         "recipient": e.get("recipient"),
                         "lane": e.get("lane"), "chain": chain,
                         "chain_root": e.get("chain_root"),
                         "task": e.get("task"), "repo_id": e.get("repo_id")}
            chains.setdefault(chain, []).append(rid)
            continue
        if kind == "verdict":
            ev = ["V", e.get("polarity") or "undeclared",
                  bool(e.get("patch_tip"))]
        elif kind == "advisory-read":
            ev = ["Z", e.get("polarity") or "undeclared"]
        elif kind == "hold":
            ev = ["K", bool(e.get("source_clean_tip")),
                  bool(e.get("owner_gated"))]
        elif kind == "close":
            ev = ["L", e.get("close_reason")]
        elif kind in ("withdraw", "discharge", "verdict-retract"):
            ev = ["N", kind]
        elif kind == "custody":
            ev = ["X", e.get("custodian")]
        elif kind in codes:
            ev = list(codes[kind])
        else:
            continue
        events.setdefault(rid, []).append([at] + ev)
    for ids in chains.values():
        ids.sort(key=lambda i: rows[i]["ts"] or 0)
    return {"rows": rows, "events": events, "chains": chains,
            "unavailable": None}


def read_trains(root):
    """{trains, lands, ejections, unavailable}: the auto-land train archive,
    the land log and the ejection store of the project checked out at
    `root` — the only project whose lands helm records."""
    from . import autoland, eventledger, landwindow   # DEFERRED — heavy
    trains = []
    for d in autoland.archived(root):
        # A CAR IS A REQUEST AT A TIP (`landwindow`): its `id` is the land
        # request the train boarded and `tip` the commit it merged, so a
        # pushed train names exactly which request it landed (`_carried`)
        cars = [{"id": c.get("id"), "tip": c.get("tip"),
                 "lane": c.get("lane"), "task": c.get("task"),
                 "author": c.get("author"), "reader": c.get("reader")}
                for c in d.get("cars") or () if isinstance(c, dict)]
        trains.append({
            "name": d.get("name") or d.get("train"), "state": d.get("state"),
            "land": d.get("land"),
            "intent": _epoch(d.get("intent_ts") or d.get("created_ts")),
            "pushed": _epoch(d.get("pushed_ts")),
            "ended": _epoch(d.get("updated_ts") or d.get("created_ts")),
            "ran": (d.get("receipt") or {}).get("ran")
            if isinstance(d.get("receipt"), dict) else None,
            "cars": cars})
    trains.sort(key=lambda t: t["intent"] or 0)
    lands, lwhy = autoland.land_log(root)
    by_train = {row.get("train"): {"n": n, "ran": row.get("ran"),
                                   "at": _epoch(row.get("ts"))}
                for n, row in (lands or {}).items() if row.get("train")}
    ej, ewhy = eventledger.checked_events(landwindow.ejections_path(root))
    ejections = [{"lane": e.get("lane"), "train": e.get("train"),
                  "at": _epoch(e.get("ts"))} for e in ej or ()
                 if e.get("event") == landwindow.EJECTED and e.get("lane")]
    return {"trains": trains, "lands": by_train, "ejections": ejections,
            "unavailable": lwhy or ewhy}


# ------------------------------------------------------------ the build

def _card(cards, key, **seed):
    c = cards.get(key)
    if c is None:
        c = cards[key] = {"key": key, "task": None, "link": None,
                          "project": None, "actions": [], "records": [],
                          "lanes": [], "rooms": [], "land": None,
                          "chains": []}
    for k, v in seed.items():
        if v is not None and c.get(k) in (None, [], ""):
            c[k] = v
    return c


def _lane(s):
    """A lane's name for MATCHING: the leases a lane holds through review
    carry a `lane/` prefix its request does not (CURE2 3585, P2), so a room
    on `lane/a-rev-0` is the room of the request on `a-rev-0`. The raw name
    stays on the card for display (`lanes`)."""
    return s[5:] if isinstance(s, str) and s.startswith("lane/") else s


def _row_key(link, project, lane):
    return link["task"] or "lane:%s:%s" % (project, _lane(lane))


def _place(placed, project, lane, key):
    """Record that card `key` holds work of `lane` in `project`."""
    held = placed.setdefault((project, _lane(lane)), [])
    if key not in held:
        held.append(key)


def _room_card(placed, project, lane, key, link):
    """The card a room of `lane` rides: the room's own key when that card
    holds the lane's work, else the first card that does, else None. The
    room's key comes from the lane alone, since a room has no dispatch row;
    the card was keyed with the row, whose chain may record the task (the
    stored key) when the lane names none (console walk 4, P1 1). A room
    whose lane record names its task (`link`, stored) never rides ANOTHER
    task's card: a reused lane name's old land stays its own task's
    history (`taskkey`), so that room rides only a card naming no task."""
    held = placed.get((project, _lane(lane))) or ()
    if key in held:
        return key
    if link["how"] == "stored":
        held = [h for h in held if h.startswith("lane:")]
    return held[0] if held else None


def _serves(served, ti, ci, task):
    """Did car `ci` of train `ti` join `task` at its land (`served`, the
    task each car joined, per train, from `build`)? A card that names no
    task takes every car of its lane's name, as before."""
    return task is None or served is None or served[ti][ci] == task


def _land_of(trains, lands, lane, train_project, project, task=None,
             served=None):
    """The newest land of `lane` on this project's trains: {train, n, at,
    ran, author, reader, how}, or None. For a card of `task`, only a car
    that joined that task at its land (`_serves`): a lane name reused for
    another task never carries the old land onto it."""
    if project != train_project:
        return None
    for ti in range(len(trains) - 1, -1, -1):
        t = trains[ti]
        if t["state"] != "DONE" or not t["pushed"]:
            continue
        car = next((c for ci, c in enumerate(t["cars"])
                    if _lane(c["lane"]) == _lane(lane)
                    and _serves(served, ti, ci, task)), None)
        if car:
            log = lands.get(t["name"]) or {}
            return {"train": t["name"], "n": t["land"] or log.get("n"),
                    "at": t["pushed"], "ran": t["ran"] or log.get("ran"),
                    "author": car.get("author"), "reader": car.get("reader"),
                    "how": "train"}
    return None


def build(inp):
    """The snapshot: every card, every typed row, the reading, the scope.

    `inp`: {now, board (the `/api/board` answer), projects (the registry's
    {key: record}, or None unread), tasks (`read_tasks`), dispatch
    (`read_dispatch`), trains (`read_trains`), train_project, lanes (the
    lane records of the train project's repository, `taskkey.lane_records`,
    or None: none read), lanes_unavailable (why they could not be read),
    task_lands (`taskkey.lands_by_task`'s index, or None: not read)}. PURE:
    it reads nothing else, so the suite drives it with a planted world."""
    now = inp["now"]
    board = inp.get("board") or {}
    ledger = (inp.get("tasks") or {}).get("rows") or {}
    tasks_why = (inp.get("tasks") or {}).get("unavailable")
    disp = inp.get("dispatch") or {"rows": {}, "events": {}, "chains": {}}
    tr = inp.get("trains") or {"trains": [], "lands": {}, "ejections": []}
    trains, lands = tr.get("trains") or [], tr.get("lands") or {}
    train_project = inp.get("train_project")
    projects = inp.get("projects") or {}

    def link_of(proj, lane, row=None, land=None):
        # THE LANE RECORDS ARE THE TRAIN PROJECT'S (task/3703): the checkout
        # this helm serves keeps them, so they join its live work only. A
        # LAND (`land`, a train's car) is history, a fact (`taskkey`): it
        # joins by its car's recorded task and its chain's first row, else
        # its lane's literal, never today's lane records, as
        # `taskkey.lands_by_task` joins it
        mine = proj == train_project and land is None
        return join_work(proj, ledger, lane=lane, row=row,
                         current=disp["rows"],
                         lanes=inp.get("lanes") if mine else None,
                         unread=inp.get("lanes_unavailable") if mine
                         else None,
                         recorded=(land["task"],) if land and land.get("task")
                         else ())
    reading = pipe_counts(projects, board)
    gen = board.get("generated_at") if isinstance(
        board.get("generated_at"), (int, float)) else now
    cards, rows, records = {}, {}, []
    # EVERY REQUEST A TRAIN PUSHED, by its id: the newest push of it
    carried = {}
    for t in trains:
        if t["state"] != "DONE" or not t["pushed"] or t["pushed"] > now:
            continue
        for car in t["cars"]:
            if car.get("id") and (car["id"] not in carried
                                  or carried[car["id"]]["at"] < t["pushed"]):
                carried[car["id"]] = {"train": t["name"], "n": t["land"]
                                      or (lands.get(t["name"]) or {}).get("n"),
                                      "at": t["pushed"], "tip": car.get("tip"),
                                      "task": car.get("task")}
    # THE TASK EACH CAR JOINED AT ITS LAND, per train (`link_of`, history):
    # a card finds a train's car by its lane's NAME only when the car joined
    # the card's own task (`_serves`), so today's lane record never carries
    # a reused name's old land onto the task it records now
    served = [[link_of(train_project, c.get("lane"),
                       disp["rows"].get(c.get("id")), c)["task"]
               for c in t["cars"]] for t in trains]

    # ---- every row the one reading returned, typed once. A project whose
    # server counted more live rows than it sent (an older server's cap) is
    # PARTIAL: its cards are a floor, like a project not read.
    partial = []
    for proj in reading["counted"]:
        got = pipe_rows(board, proj)
        if sum(1 for _c, fold, _l in got if fold is None) \
                < (reading["tallies"][proj].get("live") or 0):
            partial.append(proj)
        for c, fold, line in got:
            rid = c.get("id")
            if not rid or rid in rows:
                continue
            s = settle(c, fold, line, _carried(c, carried)
                       if proj == train_project else None)
            # A REQUEST ITS TRAIN PUSHED IS A LAND, joined as history
            link = link_of(proj, c.get("lane"), disp["rows"].get(rid),
                           carried[rid] if s["evidence"].get("train")
                           else None)
            age = c.get("age_s")
            since = gen - age if isinstance(age, (int, float)) \
                and not isinstance(age, bool) else None
            row = {"id": rid, "lane": c.get("lane"), "project": proj,
                   "state": c.get("state"), "settlement": s,
                   "stalled": bool(c.get("stalled")), "since": since,
                   "chain": c.get("chain_root"), "task": link["task"],
                   "tip": c.get("tip")}
            if s["type"] == RECORD:
                row["whose"] = _whose(c, None, None, train_project, proj)
                rows[rid] = row
                records.append(row)
                if s["evidence"].get("train") \
                        and carried[rid]["at"] >= now - LANDED_S:
                    # A REQUEST ITS TRAIN PUSHED IN THE WINDOW is its work's
                    # land: the record draws its card (lanes and rounds, so
                    # the drawer tells where it has been), the train's land
                    # rides that card, and its room is placed on it
                    row["card"] = _row_key(link, proj, c.get("lane"))
                    card = _card(cards, row["card"], task=link["task"],
                                 link=link, project=proj)
                    if c.get("lane") and c.get("lane") not in card["lanes"]:
                        card["lanes"].append(c.get("lane"))
                    if row["chain"] and row["chain"] not in card["chains"]:
                        card["chains"].append(row["chain"])
                continue
            stage, sub = _row_stage(c, s["type"])
            row.update(stage=stage, sub=sub,
                       whose=_whose(c, stage, sub, train_project, proj),
                       author=c.get("author"), reviewer=c.get("reviewer"),
                       contrary=bool(c.get("contrary")),
                       on_trunk=c.get("trunk_contains_tip"))
            rows[rid] = row
            card = _card(cards, _row_key(link, proj, c.get("lane")),
                         task=link["task"], link=link, project=proj)
            card["actions"].append(row)
            if c.get("lane") and c.get("lane") not in card["lanes"]:
                card["lanes"].append(c.get("lane"))
            if row["chain"] and row["chain"] not in card["chains"]:
                card["chains"].append(row["chain"])
    for row in records:
        key = row["task"] or "lane:%s:%s" % (row["project"],
                                             _lane(row["lane"]))
        if key in cards:
            cards[key]["records"].append(row["id"])

    # ---- work rooms. A claimed room with no round sent is its seat's own
    # Building move (an action beside the card's other moves); a room whose
    # round the reading holds rides that card, its move the round's; a room
    # on landed work rides its card, or is a record when no card is drawn.
    # WHILE A PROJECT'S LAND REQUESTS ARE NOT READ, ITS ROOMS ARE NOT PLACED
    # (the board's own rule, task/3130): a room whose round nobody read is
    # not known to be unsent. WHILE THEY ARE STALE, A ROOM THE READING HOLDS
    # NO ROUND FOR IS NOT PLACED EITHER (task/3130's stale half): its first
    # round may have come after the reading, and it would be a false
    # "writing" card; that project is then PARTIAL, so Building says it is a
    # floor. A roster not read places no room at all, and Building says it
    # is a floor. A room is matched to its request by the lane's name
    # without its `lane/` prefix (`_lane`), and rides THE CARD THAT HOLDS
    # the request (`_room_card`), never a card its own key would draw. A
    # room whose lane holds no live round but LANDED work in the window (its
    # request's record, or its train's car: `done`) is that work going on:
    # it rides that card, and while it is not on trunk its seat's writing
    # move rides with it, never a second card beside the land.
    placed, done = {}, {}
    for c in cards.values():
        for a in c["actions"]:
            _place(placed, a["project"], a["lane"], c["key"])
    for r in records:
        if r.get("card"):
            _place(done, r["project"], r["lane"], r["card"])

    # ---- lands: every car a train pushed in the Landed window, placed
    # before the rooms so a room on its lane finds its card (`done`)
    cut = now - LANDED_S
    for t in trains:
        if t["state"] != "DONE" or not t["pushed"] or t["pushed"] < cut \
                or t["pushed"] > now:
            continue
        for car in t["cars"]:
            lane = car.get("lane")
            mine = rows.get(car.get("id")) if car.get("id") else None
            if mine is not None and mine.get("card"):
                # the request this car IS already drew its work's card
                card = cards[mine["card"]]
            else:
                link = link_of(train_project, lane,
                               disp["rows"].get(car.get("id")), car)
                card = _card(cards, _row_key(link, train_project, lane),
                             task=link["task"], link=link,
                             project=train_project)
            if lane and _lane(lane) not in map(_lane, card["lanes"]):
                card["lanes"].append(lane)
            if lane:
                _place(done, train_project, lane, card["key"])
            land = _land_of(trains, lands, lane, train_project, train_project,
                            card["task"], served)
            if land and (not card["land"] or land["at"] > card["land"]["at"]):
                card["land"] = land
    rooms_read = _sec_state(_sec(board, "seats")) in ("ok", "stale")
    extra, landed_rooms = [], []
    for proj, rec in ((board.get("projects") or {}).items()
                      if isinstance(board.get("projects"), dict)
                      and rooms_read else ()):
        if proj not in reading["counted"]:
            continue
        stale = _pipe(board, proj)[1] != "ok"
        for room in (rec or {}).get("running") or ():
            if not isinstance(room, dict) or room.get("kind") != "claim" \
                    or not room.get("lane"):
                continue          # a seat with no room is not a piece of work
            lane = room["lane"]
            proof = room.get("landed") or {}
            link = link_of(proj, lane)
            key = _row_key(link, proj, lane)
            info = {"lane": lane, "seats": list(room.get("seats") or ()),
                    "landed": proof.get("state") == "landed",
                    "proof": proof.get("proof"), "project": proj}
            held = _room_card(placed, proj, lane, key, link)
            if held:
                cards[held]["rooms"].append(info)
                continue
            if info["landed"]:
                landed_rooms.append((key, link, info))
                continue
            if stale:
                if proj not in partial:
                    partial.append(proj)
                continue
            held = _room_card(done, proj, lane, key, link)
            card = cards[held] if held else _card(
                cards, key, task=link["task"], link=link, project=proj)
            card["rooms"].append(info)
            if lane not in card["lanes"]:
                card["lanes"].append(lane)
            move = {"id": "room:%s:%s" % (proj, lane), "lane": lane,
                    "project": proj, "state": "CLAIMED", "stage": "building",
                    "sub": "writing", "since": None, "stalled": False,
                    "chain": None, "task": card["task"],
                    "whose": {"kind": "seat", "who": ", ".join(info["seats"]),
                              "why": None} if info["seats"] else
                    {"kind": "nobody", "who": None,
                     "why": "the claim names no seat"},
                    "settlement": {"type": ACTIVE, "why": "a claimed room "
                                   "with no round sent: its seat is writing",
                                   "evidence": {"state": "CLAIMED",
                                                "fold": None,
                                                "landed": proof.get("proof")}}}
            card["actions"].append(move)
            extra.append(move)

    # ---- the to-do list: every open task, once
    from . import tasks as _tasks           # DEFERRED — the ledger module
    for tid, t in ledger.items():
        if t.get("status") in _tasks.OPEN_STATUSES:
            _card(cards, tid, task=tid,
                  link={"how": "task", "why": None},
                  project=_tasks.project_of_row(t))

    # ---- a room on landed work: on the card that holds its lane's round or
    # land (`_room_card`, the same maps the live rooms ride), else on its own
    # key's card when one is drawn, else a record whose lease release its
    # seat owes
    for key, link, info in landed_rooms:
        held = _room_card(placed, info["project"], info["lane"], key, link) \
            or _room_card(done, info["project"], info["lane"], key, link) \
            or (key if key in cards else None)
        if held:
            cards[held]["rooms"].append(info)
            continue
        rec = {"id": "room:%s:%s" % (info["project"], info["lane"]),
               "lane": info["lane"], "project": info["project"],
               "state": "ROOM", "task": link["task"], "since": None,
               "stalled": False,
               "settlement": {"type": RECORD, "why": "the room's branch is "
                              "on trunk; only its lease is still held",
                              "evidence": {"landed": info["proof"]}},
               "whose": {"kind": "seat", "who": ", ".join(info["seats"]),
                         "why": "its lease release is owed"}}
        records.append(rec)
        extra.append(rec)

    for card in cards.values():
        _finish(card, ledger, inp, disp, trains, lands, train_project, served)
    seen = [r["id"] for c in cards.values() for r in c["actions"]
            if not str(r["id"]).startswith("room:")] \
        + [r["id"] for r in records if not str(r["id"]).startswith("room:")]
    if len(seen) != len(set(seen)) or set(seen) != set(rows):
        raise RuntimeError("the reading's rows were not typed exactly once")
    return {"built_at": now, "cards": cards, "records": records,
            "rows": rows, "reading": reading, "partial": partial,
            "rooms_read": rooms_read, "extra": extra,
            "train_project": train_project,
            "tasks_unavailable": tasks_why,
            "dispatch_unavailable": disp.get("unavailable"),
            "trains_unavailable": tr.get("unavailable"),
            "trains": trains, "lands": lands, "served": served,
            "ejections": tr.get("ejections") or [],
            "history": (inp.get("tasks") or {}).get("history") or {},
            "dispatch": disp, "events": {}, "zone": inp.get("zone")
            or OWNER_ZONE}


def _primary_key(a):
    return (STAGE_I[a["stage"]], a["sub"] != "fix-owed",
            not a.get("stalled"), a["since"] if a["since"] is not None
            else float("inf"))


def _finish(card, ledger, inp, disp, trains, lands, train_project,
            served=None):
    """Place one card: its stage, whose move, the stages it reached, its fix
    count, whether it came back, and its debt. A train's car is the card's
    by its lane's name only when it joined the card's task (`_serves`)."""
    now = inp["now"]
    t = ledger.get(card["task"]) if card["task"] else None
    from . import tasks as _tasks           # DEFERRED — the ledger module
    card["title"] = (t or {}).get("title") or re.sub(
        r"[-_]+", " ", (card["lanes"] or [card["key"].rsplit(":", 1)[-1]])[0]
    ).strip().capitalize()
    card["status"] = (t or {}).get("status")
    card["rank"] = (t or {}).get("priority") \
        if (t or {}).get("priority") in _tasks.PRIORITIES else None
    card["asked"] = (t or {}).get("origin") == "owner"
    card["owner"] = (_tasks.owner_of(t) or None) if t else None
    card["filed"] = _epoch((t or {}).get("ts"))
    card["paused"] = bool(t) and _tasks.standdown_blocks_offer(t, now)
    if card["project"] is None and t is not None:
        card["project"] = _tasks.project_of_row(t)
    actions = sorted(card["actions"], key=_primary_key)
    for i, a in enumerate(actions):
        a["primary"] = i == 0
    card["actions"] = actions

    # what the chains of its rounds recorded
    fixes, reached = 0, set()
    for chain in card["chains"]:
        for rid in disp.get("chains", {}).get(chain, ()):
            r = disp["rows"].get(rid) or {}
            reached.add(1)
            if r.get("kind") == "review":
                reached.add(2)
            for e in disp.get("events", {}).get(rid, ()):
                if e[1] == "V":
                    reached.add(2)
                    if e[2] == "fix":
                        fixes += 1
                    elif e[2] == "approve":
                        reached.add(3)
                elif e[1] == "K" and e[2]:
                    reached.add(3)
    for a in actions:
        reached.add(STAGE_I[a["stage"]])
        if a["state"] in ("REVIEWED", "CHANGES_REQUESTED"):
            reached.add(2)
        if a["sub"] in ("fix-owed", "close-owed", "rehold-owed") \
                or a.get("on_trunk") is True:
            reached.add(4)
        if a["sub"] == "fix" and not fixes:
            fixes = 1
    if card["rooms"]:
        reached.add(1)
    if any(r["landed"] for r in card["rooms"]):
        reached.add(4)
    for lane in card["lanes"]:
        for ti, tr in enumerate(trains):
            if card["project"] == train_project \
                    and any(_lane(c["lane"]) == _lane(lane)
                            and _serves(served, ti, ci, card["task"])
                            for ci, c in enumerate(tr["cars"])):
                reached.add(3)
    land = card["land"]
    if not land:
        for lane in card["lanes"]:
            got = _land_of(trains, lands, lane, train_project,
                           card["project"], card["task"], served)
            if got and (not land or got["at"] > land["at"]):
                land = got
        card["land"] = land
    if land:
        reached.update((3, 4))
    # THE LANDED-UNREAD STATE IS THE ONE JOIN'S (task/3703): the lands
    # `taskkey.lands_by_task` recorded for THIS task, read by
    # `taskkey.landed_state`, so an open task whose work landed says so
    # even when no lane of its card names the land. The card's own land,
    # found by a lane's name (which a reused name can carry), answers only
    # while that index is not read.
    from . import taskkey                   # DEFERRED — the one join
    index = inp.get("task_lands")
    unread = taskkey.landed_state(t, index.get(card["task"])
                                  if index is not None
                                  else (land,) if land else ())

    debt = next((a for a in actions if a["sub"] == "fix-owed"), None)
    card["debt"] = {"row": debt["id"], "since": debt["since"],
                    "author": debt.get("author"),
                    "reviewer": debt.get("reviewer"),
                    "landed": debt["settlement"]["evidence"].get("landed"),
                    "land": land} if debt else None
    if actions:
        p = actions[0]
        card.update(stage=p["stage"], sub=p["sub"], whose=p["whose"],
                    since=p["since"])
    elif land and land.get("at") and land["at"] >= now - LANDED_S:
        card.update(stage="landed", sub="open" if unread else "recent",
                    whose={"kind": "done", "who": None, "why": unread},
                    since=land["at"])
    else:
        released = _released(inp.get("tasks", {}).get("history", {}).get(
            card["task"]) or (), now)
        sub = "paused" if card["paused"] else \
            "claimed" if card["status"] == "in_progress" else \
            "released" if released and not card["owner"] else \
            "landed-open" if unread else None
        # TIME IN THIS STAGE: a card handed back has been in To do since it
        # came back, a paused one since its pause, never since it was filed
        pause = _epoch(((t or {}).get("standdown") or {}).get("ts")) \
            if isinstance((t or {}).get("standdown"), dict) else None
        since = (released or {}).get("at") if sub == "released" \
            else pause if sub == "paused" else None
        card.update(stage="todo", sub=sub,
                    whose={"kind": "seat", "who": card["owner"], "why": None}
                    if card["owner"] else {"kind": "unowned", "who": None,
                                           "why": None},
                    since=since or card["filed"])
        card["released"] = released if sub == "released" else None
    if card["task"]:
        reached.add(0)
    reached.add(STAGE_I[card["stage"]])
    card["reached"] = [STAGES[i] for i in sorted(reached)]
    card["fixes"] = fixes
    # THE HOLLOW DOTS: stages it reached and was sent back from — past its
    # stage, and held by none of its live moves (a secondary action further
    # on is live work, not a return)
    live = {STAGE_I[a["stage"]] for a in actions}
    card["hollow"] = [STAGES[i] for i in sorted(reached)
                      if i > STAGE_I[card["stage"]] and i not in live]
    card["came_back"] = bool(card["hollow"]) or bool(card.get("released"))
    card["stalled"] = bool(actions and actions[0].get("stalled"))


def _released(history, now):
    """{by, at} when the task was last handed back to the pool inside
    `RELEASED_S`, else None."""
    last = None
    prev = None
    for s in history:
        if prev is not None and prev.get("owner") and not s.get("owner"):
            rl = s.get("released") or {}
            last = {"by": rl.get("by") or prev.get("owner"),
                    "at": _epoch(rl.get("ts")) or _epoch(s.get("last_updated"))}
        prev = s
    if last and last["at"] and last["at"] > now - RELEASED_S:
        return last
    return None


# --------------------------------------------------------------- the view

def _stuck(card, now):
    """The STUCK mark, or None. Never a retirement: a fix owed on landed
    work, the reading's own stall alarm on the card's primary row, or nobody
    holding the next move for more than STUCK_S."""
    if card["stage"] in ("todo", "landed"):
        return None
    if card["sub"] == "fix-owed":
        return {"why": "fix-owed"}
    if card.get("stalled"):
        return {"why": "stalled"}
    since = card.get("since")
    if card["whose"]["kind"] in ("nobody", "role") and since is not None \
            and now - since > STUCK_S:
        return {"why": "no-move", "for_s": int(now - since)}
    return None


def _age(since, now):
    return max(0, int(now - since)) if since is not None else None


def _day(at, zone):
    try:
        from zoneinfo import ZoneInfo       # DEFERRED — stdlib, tz data
        return datetime.datetime.fromtimestamp(at, ZoneInfo(zone)).date()
    except Exception:                        # noqa: BLE001 — named below
        return None


#: the wire keys a card or row keeps even when empty
_WIRE_KEEP = frozenset(("key", "stage", "whose", "id", "settlement"))


def _compact(d):
    """`d` with every None, False and empty value left off the wire, so a
    card of the to-do list stays small; an absent key reads as the empty one
    it replaces."""
    return {k: v for k, v in d.items()
            if k in _WIRE_KEEP or v not in (None, False, [], {}, "")}


def _wire_row(r, now):
    out = {k: r.get(k) for k in ("id", "lane", "project", "state", "stage",
                                 "sub", "whose", "since", "settlement",
                                 "primary", "stalled", "task")
           if k in r}
    out["age_s"] = _age(r.get("since"), now)
    if isinstance(out.get("whose"), dict):
        out["whose"] = _compact(out["whose"])
    return _compact(out)


def _wire_card(card, now, stuck):
    """One card as the page receives it."""
    return _compact({
        "key": card["key"], "task": card["task"],
        "link": _compact(card["link"] or {"how": "unknown"}),
        "project": card["project"], "title": card["title"],
        "status": card["status"], "rank": card["rank"],
        "asked": card["asked"], "owner": card["owner"],
        "paused": card["paused"], "stage": card["stage"],
        "sub": card["sub"], "whose": _compact(card["whose"]),
        "since": card["since"], "age_s": _age(card["since"], now),
        "reached": card["reached"], "hollow": card["hollow"],
        "fixes": card["fixes"], "came_back": card["came_back"],
        "stuck": stuck, "land": card["land"], "debt": card["debt"],
        "rooms": card["rooms"], "released": card.get("released"),
        "lanes": card["lanes"],
        "actions": [_wire_row(a, now) for a in card["actions"]],
        "records": card["records"]})


def _trains_view(snap, now):
    """The train's own line for the Landing and Landed columns: the last
    run, the lands of the Landed window with the lanes each carried, and
    the window's abandoned, vetoed and thrown-off counts."""
    trains = snap["trains"]
    if not snap["train_project"]:
        return None
    cut = now - LANDED_S
    done = [t for t in trains if t["state"] == "DONE" and t["pushed"]
            and cut <= t["pushed"] <= now]
    last = trains[-1] if trains else None
    return {"project": snap["train_project"],
            "last": {"name": last["name"], "state": last["state"],
                     "land": last["land"], "ran": last["ran"],
                     "at": last["pushed"] or last["ended"]} if last else None,
            "recent": [{"name": t["name"], "land": t["land"],
                        "at": t["pushed"], "ran": t["ran"],
                        "lanes": [c["lane"] for c in t["cars"]]}
                       for t in reversed(done)],
            "day": {"done": len(done),
                    "abandoned": sum(1 for t in trains
                                     if t["state"] == "ABANDONED"
                                     and (t["ended"] or 0) >= cut),
                    "vetoed": sum(1 for t in trains if t["state"] == "VETOED"
                                  and (t["ended"] or 0) >= cut),
                    "thrown": sum(1 for e in snap["ejections"]
                                  if (e["at"] or 0) >= cut)}}


def view(snap, now):
    """The `/api/work` body for one snapshot at `now`: the cards, the
    records, the counts, the scope. Ages, the stuck mark and the Today count
    are read at `now`; everything else is the snapshot's.

    A STAGE COUNT OVER A PARTIAL SCOPE IS NEVER A COMPLETE-LOOKING NUMBER.
    With a project not read, each pipeline stage is a floor (`at_least`), or
    UNKNOWN when nothing was seen there, and To do is a ceiling (`at_most`)
    when a project not read has tasks in it: its work may be further on.

    LANDED IS MEASURED FOR THE TRAIN'S PROJECT ONLY (task/3723): helm records
    no land of a project that merges by hand, registered or with work, so
    over every project Landed is a floor (UNKNOWN when none landed), and
    `counts.landed` names the one project measured and how many are not."""
    reading = snap["reading"]
    zone = snap["zone"]
    tp = snap["train_project"]
    today = _day(now, zone)
    missing = reading["unread"] + reading["unknown"] + snap["partial"]
    cards_out, stages = [], dict.fromkeys(STAGES, 0)
    by_project, whose, stuck, back, asked, notask = {}, {}, 0, 0, 0, 0
    landed_today, todo_missing = 0, False
    for card in snap["cards"].values():
        st = _stuck(card, now)
        stages[card["stage"]] += 1
        p = card["project"] or "none"
        by_project.setdefault(p, dict.fromkeys(STAGES, 0))[card["stage"]] += 1
        todo_missing |= card["stage"] == "todo" and p in missing
        w = card["whose"]
        label = w["who"] if w["kind"] in ("seat", "role") and w["who"] \
            else w["kind"]
        whose[label] = whose.get(label, 0) + 1
        stuck += st is not None
        back += bool(card["came_back"])
        asked += bool(card["asked"])
        notask += card["task"] is None
        if card["stage"] == "landed" and card["land"] and today \
                and _day(card["land"]["at"], zone) == today:
            landed_today += 1
        cards_out.append(_wire_card(card, now, st))
    cards_out.sort(key=lambda c: (STAGE_I[c["stage"]], c["key"]))
    others = {p for p in list(by_project) + reading["counted"]
              + reading["unread"] + reading["unknown"]
              if p not in (tp, "none")}
    counts = {}
    for s in STAGES:
        n = stages[s]
        if s in PIPELINE and (missing or (s == "building"
                                          and not snap["rooms_read"])):
            counts[s] = {"n": n if n else None, "exact": False,
                         "bound": "at_least" if n else "unknown"}
        elif s == "todo" and snap["tasks_unavailable"]:
            counts[s] = {"n": None, "exact": False, "bound": "unknown"}
        elif s == "todo" and todo_missing:
            counts[s] = {"n": n, "exact": False, "bound": "at_most"}
        elif s == "landed" and (snap["trains_unavailable"] or others):
            counts[s] = {"n": n if n else None, "exact": False,
                         "bound": "at_least" if n else "unknown"}
        else:
            counts[s] = {"n": n, "exact": True, "bound": None}
    if not reading["read"]:
        for s in PIPELINE:
            counts[s] = {"n": None, "exact": False, "bound": "unknown"}
    settlements = dict.fromkeys(SETTLEMENTS, 0)
    for r in list(snap["rows"].values()) + snap["extra"]:
        settlements[r["settlement"]["type"]] += 1
    return {
        "rev": snap.get("rev"), "generated_at": now,
        "built_at": snap["built_at"], "build_s": snap.get("build_s"),
        "zone": zone,
        "scope": {"complete": reading["complete"] and not missing
                  and snap["rooms_read"] and not snap["tasks_unavailable"]
                  and not snap["trains_unavailable"]
                  and not snap["dispatch_unavailable"],
                  "rooms_read": snap["rooms_read"],
                  "unread": reading["unread"], "unknown": reading["unknown"],
                  "partial": snap["partial"],
                  "why": reading["why"],
                  "tasks_unavailable": snap["tasks_unavailable"],
                  "dispatch_unavailable": snap["dispatch_unavailable"],
                  "trains_unavailable": snap["trains_unavailable"]},
        "reading": dict({k: reading[k] for k in (
            "live", "marks", "alarm", "holders", "counted", "unread",
            "unknown", "why", "age_s", "stale", "limit_s", "read",
            "complete")}, tallies={k: t for k, t in reading["tallies"].items()
                                   if t.get("live")}),
        "counts": {
            "stages": counts, "cards": len(cards_out), "stuck": stuck,
            "came_back": back, "asked": asked, "no_task": notask,
            "whose": whose, "by_project": by_project,
            "settlement": settlements,
            "landed": {"window_s": LANDED_S,
                       "n": counts["landed"]["n"],
                       "today": landed_today if today
                       and not snap["trains_unavailable"] else None,
                       "unavailable": snap["trains_unavailable"],
                       "measured": [tp] if tp else [],
                       "unmeasured": sorted(p for p in by_project
                                            if p not in (tp, "none")),
                       "others": len(others)}},
        "trains": _trains_view(snap, now),
        "cards": cards_out,
        "records": [_wire_row(r, now) for r in snap["records"]],
    }


# ----------------------------------------------------- crossings, lazily

def _task_events(history):
    """Every change the to-do ledger kept for one task, as compact
    crossings [epoch, code, ...]: F written down, R ranked, A assigned, C
    claimed, H handed on, B handed back, D done, O reopened, s set down, P
    paused, Q resumed."""
    ev, prev = [], None
    for s in history:
        lu = _epoch(s.get("last_updated")) or _epoch(s.get("ts"))
        if prev is None:
            ev.append([_epoch(s.get("ts")) or lu, "F", s.get("origin")
                       or "agent", s.get("project")])
            if s.get("priority"):
                ev.append([_epoch(s.get("ranked_at")) or lu, "R", None,
                           s["priority"], s.get("ranked_by")])
            if s.get("owner"):
                ev.append([lu, "C" if s.get("status") == "in_progress"
                           else "A", s["owner"]])
            prev = s
            continue
        if s.get("priority") != prev.get("priority"):
            ev.append([_epoch(s.get("ranked_at")) or lu, "R",
                       prev.get("priority"), s.get("priority"),
                       s.get("ranked_by")])
        if s.get("owner") != prev.get("owner"):
            if not s.get("owner"):
                rl = s.get("released") or {}
                ev.append([_epoch(rl.get("ts")) or lu, "B", rl.get("by")
                           or prev.get("owner")])
            elif not prev.get("owner"):
                ev.append([lu, "C" if s.get("status") == "in_progress"
                           else "A", s["owner"]])
            else:
                ev.append([lu, "H", prev["owner"], s["owner"]])
        if s.get("status") != prev.get("status"):
            if s.get("status") == "closed":
                ev.append([lu, "D", s.get("train")])
            elif prev.get("status") == "closed":
                ev.append([lu, "O"])
            elif s.get("status") == "in_progress" \
                    and s.get("owner") == prev.get("owner"):
                ev.append([lu, "C", s.get("owner")])
            elif s.get("status") == "open" \
                    and s.get("owner") == prev.get("owner"):
                ev.append([lu, "s", s.get("owner")])
        if bool(s.get("standdown")) != bool(prev.get("standdown")):
            ev.append([_epoch((s.get("standdown") or {}).get("ts")) or lu,
                       "P", (s.get("standdown") or {}).get("until")]
                      if s.get("standdown") else [lu, "Q"])
        prev = s
    return ev


def card_events(snap, key):
    """{events, timed, gaps} — one card's crossings, oldest first, off the
    SAME snapshot its card was drawn from, computed on first ask and kept.
    A crossing helm keeps no time for is a gap ([None, ...]), never left
    out."""
    if key in snap["events"]:
        return snap["events"][key]
    card = snap["cards"].get(key)
    if card is None:
        return None
    ev = []
    if card["task"]:
        ev += _task_events(snap["history"].get(card["task"]) or ())
    disp = snap["dispatch"]
    n = 0
    for chain in card["chains"]:
        for rid in disp.get("chains", {}).get(chain, ()):
            r = disp["rows"].get(rid) or {}
            n += 1
            ev.append([r.get("ts"), "S", n, r.get("kind"), r.get("sender"),
                       r.get("recipient"), r.get("lane")])
            for e in disp.get("events", {}).get(rid, ()):
                ev.append([e[0], e[1], n] + e[2:])
    for room in card["rooms"]:
        ev.append([None, "W", room["lane"]])
    if card["project"] == snap["train_project"]:
        for lane in card["lanes"]:
            for ti, t in enumerate(snap["trains"]):
                if not any(_lane(c["lane"]) == _lane(lane)
                           and _serves(snap.get("served"), ti, ci,
                                       card["task"])
                           for ci, c in enumerate(t["cars"])):
                    continue
                ev.append([t["intent"], "J", t["name"]])
                if t["state"] == "DONE" and t["pushed"]:
                    ev.append([t["pushed"], "M", t["name"], t["land"],
                               t["ran"]])
                elif t["state"] == "VETOED":
                    ev.append([t["ended"], "tv", t["name"]])
                elif t["state"] == "ABANDONED":
                    ev.append([t["ended"], "ta", t["name"]])
            for e in snap["ejections"]:
                if _lane(e["lane"]) == _lane(lane):
                    ev.append([e["at"], "tr", e["train"]])
    if card["stage"] == "landed" and card["task"] is None:
        ev.append([None, "G", "notask"])
    # A LAND HELM SAW BUT KEEPS NO TRAIN OR TIME FOR (a fix debt's work found
    # on trunk, a room whose branch is there) is a gap, never left out
    if "landed" in (card.get("reached") or ()) \
            and not any(e[1] == "M" for e in ev):
        ev.append([None, "G", "landed"])
    ev.sort(key=lambda e: (e[0] is None, e[0] or 0))
    got = {"events": ev, "timed": sum(1 for e in ev if e[0] is not None),
           "gaps": sum(1 for e in ev if e[0] is None)}
    snap["events"][key] = got
    return got


# ------------------------------------------- the snapshot, one per change

class LiveSources:
    """Where the live snapshot reads: the console's own board reading, the
    registry, and the ledgers of the checkout this helm serves (`root`)."""

    def __init__(self, root=None):
        from . import web_ui_loader        # DEFERRED — the web package
        self.root = root or os.path.dirname(web_ui_loader.PACKAGE_DIR)

    def board(self):
        from . import web_board             # DEFERRED — the web board
        return web_board._api_board()

    def projects(self):
        from . import web_board             # DEFERRED — the web board
        return web_board._projects()

    def marks(self):
        """(the four sections `revision` marks, as a board, and the
        registry) off the board's own reads without its joins
        (`web_board._board_marks`): what a poll asks to find the kept
        revision, and never what a snapshot is built from."""
        from . import web_board             # DEFERRED — the web board
        return web_board._board_marks()

    def paths(self):
        """Every file `read` reads, for the revision marker: the ledgers, the
        train archive, the land log, the ejection store, and the git config
        of the checkout's repository, which holds its lane records
        (`taskkey.lane_records`). A repository git cannot name adds no
        path: its records are then unread, and naming it later is a new
        revision."""
        from . import autoland, dispatches, landwindow  # DEFERRED
        from . import taskkey, tasks                    # DEFERRED
        common = taskkey._common_dir(self.root)[0]
        return [tasks.ledger_path(), dispatches.ledger_path(),
                os.path.join(autoland.state_dir(self.root),
                             autoland.DONE_SUBDIR),
                autoland.land_log_path(self.root),
                landwindow.ejections_path(self.root)] \
            + ([os.path.join(common, "config")] if common else [])

    def fingerprint(self, paths):
        from . import web_cache             # DEFERRED — the web cache
        return web_cache._input_fingerprint(paths)

    def read(self, board, projects):
        from . import taskkey               # DEFERRED — the one join
        from .work import _lanes            # DEFERRED — the lanes module
        lands = _sec(board, "lands") or {}
        todo = read_tasks()
        # THE ONE JOIN'S STORED KEYS AND LANDS (task/3703): the lane records
        # of the checkout this helm serves, and every land joined to the
        # task recorded for it
        lanes, lanes_why = taskkey.lane_records(self.root)
        task_lands, lands_why = taskkey.lands_by_task(
            self.root, known=None if todo["unavailable"] else todo["rows"])
        return {"board": board, "projects": projects,
                "tasks": todo, "dispatch": read_dispatch(),
                "trains": read_trains(self.root),
                "lanes": lanes, "lanes_unavailable": lanes_why,
                "task_lands": None if lands_why else task_lands,
                "train_project": lands.get("scope")
                or _lanes.project_token(self.root)}


_LOCK = threading.Lock()
#: ONE BUILD AT A TIME: a poll arriving while a revision builds waits for it
#: and reads the snapshot it made, rather than building the same one again
_BUILD = threading.Lock()
_SNAPS = {}


def _registry_mark(projects):
    """The part of the project registry the reading reads: each project's
    key, the key the page counts it under, and whether it is retired. None
    when the registry could not be read."""
    if projects is None:
        return None
    return sorted((str(k), str(_pkey(k, rec)),
                   bool(isinstance(rec, dict) and rec.get("retired")))
                  for k, rec in projects.items())


def revision(board, projects, fingerprint, now):
    """THE REVISION MARKER — the identity of EVERY input a snapshot is built
    from, so equal markers mean one snapshot serves:

      * the board reading: each section the reader uses (the lands leg,
        whose scope also names the train's project; the fleet read; the
        roster's rooms; the task leg) by its read stamp, state and scope,
        and the board's own failure;
      * the project registry (`_registry_mark`): which projects are in
        scope, so a project added is counted, read or not, at once;
      * the ledger files (`fingerprint`, over `LiveSources.paths`): the task
        ledger, the dispatch ledger, the train archive, the land log, the
        ejection store, and the git config that holds the lane records the
        one join reads (`taskkey.lane_records`);
      * the Landed window's tick (`TICK_S`).

    An input added to `LiveSources.read` joins this marker in the same
    change, or a snapshot outlives the input it was built from."""
    marks = []
    for name in ("lands", "fleet", "seats", "tasks"):
        sec = _sec(board, name) or {}
        marks.append((name, sec.get("measured_at"), sec.get("scope"),
                      bool(sec.get("unavailable")), bool(sec.get("loading")),
                      bool(sec.get("stale"))))
    raw = repr((marks, (board or {}).get("unavailable"),
                _registry_mark(projects), fingerprint, int(now // TICK_S)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def kept(rev):
    """The snapshot kept under `rev`, or None when it is no longer kept."""
    with _LOCK:
        return _SNAPS.get(rev)


def snapshot(src=None, now=None):
    """The snapshot for the current revision, built once: a poll arriving
    while it builds waits for it rather than building it again.

    A POLL ASKS THE MARKS FIRST (task/3657): the four sections the revision
    reads and the registry, off the board's own reads without its joins
    (`LiveSources.marks`), which cost seconds on the owner's console and
    never reached a kept snapshot. A revision they name is answered as it
    is; any other reads the whole board, and the snapshot built from it is
    kept under THAT board's revision, never the marks'."""
    now = time.time() if now is None else now
    src = src or LiveSources()
    marks, projects = src.marks()
    fingerprint = src.fingerprint(src.paths())
    got = kept(revision(marks, projects, fingerprint, now))
    if got is not None:
        return got
    board = src.board()
    projects = src.projects()
    marker = revision(board, projects, fingerprint, now)
    with _BUILD:
        got = kept(marker)
        if got is not None:
            return got
        started = time.monotonic()
        inp = src.read(board, projects)
        inp["now"] = now
        snap = build(inp)
        snap["rev"] = marker
        snap["build_s"] = round(time.monotonic() - started, 3)
        with _LOCK:
            _SNAPS[marker] = snap
            while len(_SNAPS) > KEEP:
                _SNAPS.pop(next(iter(_SNAPS)))
    return snap


def forget():
    """Drop every kept snapshot (tests)."""
    with _LOCK:
        _SNAPS.clear()


def api(qs, src=None, now=None):
    """GET /api/work -> (body, status). No `key`: the page's body. `key`: one
    card with its crossings. `rev`: the reading the page was drawn from, and
    ONLY that reading — one no longer kept answers 410 with `gone` naming
    it, never another reading's card or crossings. Never a 500: a read that
    raised answers UNAVAILABLE with every count null."""
    def one(name):
        got = (qs or {}).get(name)
        return got[0] if isinstance(got, list) and got else got
    now = time.time() if now is None else now
    try:
        rev = one("rev")
        snap = kept(rev) if rev else snapshot(src, now=now)
        if snap is None:
            return {"gone": rev, "rev": None, "counts": None,
                    "unavailable": "the reading %s is no longer kept: "
                                   "reload the page to read the current one"
                                   % rev}, 410
        key = one("key")
        if not key:
            return view(snap, now), 200
        got = card_events(snap, key)
        if got is None:
            return {"unavailable": "no card %s in this reading" % key,
                    "rev": snap.get("rev")}, 404
        card = snap["cards"][key]
        return dict(got, rev=snap.get("rev"),
                    card=_wire_card(card, now, _stuck(card, now))), 200
    except Exception as exc:                # noqa: BLE001 — named, never empty
        return {"unavailable": "the work reader raised (%s)"
                % type(exc).__name__, "cards": [], "records": [],
                "counts": None, "rev": None}, 200
