"""helm.landtask — the land step: a landed task is LANDED, owing a
seen-working check, or narrows at land time (task/3746, helm/observed.py).

THE GAP. A land put a lane's work on trunk and left its task open. In one
night about nine landed rows were closed or discharged by hand, and `helm
task close-candidates` listed none of seven open rows whose lanes had landed
their whole ask. The owner: "god why dont we ever finish anything we start".

WHICH TASK A LAND SERVES (`resolve`). The car join (`taskkey.car_key`): the
task the chain's first row records and the car's own lane record (`helm work claim
--task`), which must agree, else the one literal `task/N` or `task-N` the
chain's first row names. `review_door.pair_key` still names the chain's pair
room by its first row, but a car whose own proof differs cannot close that
chain's task, findings, or sibling rows: what the land served is UNKNOWN,
and it is said. Else the OPEN task a trailing
`-<N>` in the lane's name names (`lane_number`): task/3693 landed as
train482 and stayed open, because no row carried `--task` although its lane
ended in 3693. The number rule is bounded as a lane's part marker is (three
to six digits, no leading zero), so `-3529a`, `-3533p2` and `-3301-r3` are
not it, and `canary-seeded-red-0926` is a date; and only an OPEN task is
named, so a number that reaches a closed row names nothing.

WHOLE OR PART (`run`). A lane records that it carries its task's WHOLE ask
with `--whole` (`helm work claim --task task/N --whole`, or `helm dispatch
send|add --task task/N --whole` on a chain's first row; `whole_of`). At the
land such a task is LANDED, not closed (helm/observed.py): it stays open and
owes a seen-working check by one named seat, "landed whole in LAND N
<sha12> (lane L at <tip12>): owes a seen-working check by @X", and closes
when `helm task observed` records what was seen working. A hand land tells
that seat once in the task's room; auto-land's announcement carries the
line. A task with open sub-tasks at any depth (`tasks.story_facts`) stays
open and a comment names them. Every other land comments the one question on the
task, "landed LAND N <sha12> (lane L at <tip12>): is the whole ask done?
close it, narrow its title, or file the remainder with --continues
task/<story root>", where task/<story root> is the head of the served
task's chain (the task itself when it is a root), so a part's remainder
files as a story sibling and the landed task can close.
and asks it in the task's room (`review_door.task_room`, the `<project>-<N>`
meld), addressed to the lane's author. The task stays open and is a close
candidate (helm/taskhygiene.py reads the comment back through
`parse_comment`). A land never closes a task its lane did not say it
carries whole: closing at land without that re-opened two tasks in one
night (task/3626).

ONCE. Every write is keyed by what landed, never by when: the comment by its
exact text, the room's question by `land-question:<task>:<lane>:<tip12>`
(`chat.post`'s event id), and the close by the task's own status. A step run
again, by a retried tick or a second hand verb, writes nothing twice.

EVERY LAND, NOT ONLY AUTO-LAND'S (`hand`). A land recorded by hand runs the
same step: `helm lr close --reason landed` for the row it closed, `helm lr
foldcheck --apply` for each held source-clean row it closed, and `helm lr
land` once trunk carries the reviewed tip. It also closes the open
findings its chain's FIX verdicts filed ("cured in a hand land at
<sha12>"), which before only the auto-land LAND step closed. Auto-land runs the fold and the closes
as children of the installed helm, before and after it takes the LAND
number; it tells each child (DEFER_ENV) that its own LAND step runs the land
step, with the number, so no hand-labelled step runs first.

THE CHAIN'S OTHER ROWS ARE DISCHARGED, AND A LANE'S UNCHAINED ROW IS SAID
(`chain_facts`, `_rows`). A land left build rows and hand-backs of the chain
it carried CARRIED-open, and a person discharged five in one night. Every
other live row of the landed chain is offered, in order, to the close doors
whose proof fits it (`_doors`): `landed` for an approve or a build row when
the land declared its delivery, `discharged` for a row that never got a
verdict, then `carried`. Each door re-proves the row on its own terms; the
step adds no authority, and a row every door refuses stays open and is named
on the land line and on the task. A live row of the same lane with NO chain
link to the landed row is never closed: a lane is a label, and the discharge
door's same-lane tier was withdrawn for exactly that reason. It is FLAGGED
on the land line and commented on its task, for a person to close by its
own door.
"""
import os
import re

from . import taskkey, tasks

#: A lane's trailing task number: `-<N>` at its end, three to six digits
#: with no leading zero (the digits `taskhygiene._LANE_PART` bounds a part
#: marker by).
_LANE_NUMBER = re.compile(r"-([1-9][0-9]{2,5})\Z")


def lane_number(lane, known=None):
    """The OPEN `task/N` a lane's trailing `-<N>` names, or None. `known` is
    a task ledger snapshot; without one the ledger is read strictly, and an
    unreadable ledger names nothing."""
    hit = _LANE_NUMBER.search(taskkey.lane_name(lane))
    if not hit:
        return None
    tid, _why = taskkey.open_task("task/" + hit.group(1), known)
    return tid


def resolve(row, current=None, known=None):
    """(task or None, why) — the task the landed dispatch `row` serves: the
    one join over its chain and lane record, else the open task its lane's
    trailing number names (see the module doc). `why` says why there is
    none: no task named, or the join's UNKNOWN reason."""
    if not isinstance(row, dict):
        return None, "the landed row cannot be read"
    key = taskkey.car_key(row, current)
    if key.task:
        return key.task, None
    if key.why != taskkey.NO_TASK:
        return None, key.why
    first = taskkey.first_row(row, current) or {}
    for lane in (row.get("lane"), first.get("lane")):
        tid = lane_number(lane, known) if lane else None
        if tid:
            return tid, None
    return None, taskkey.NO_TASK


def whole_of(task, row, current=None):
    """Did the lane of the landed `row` record that it carries the WHOLE ask
    of `task`: `task_whole` on the chain's first row beside that task, or
    the lane branch's `helmWhole` record naming it?"""
    if not task or not isinstance(row, dict):
        return False
    first = taskkey.first_row(row, current) or {}
    if first.get("task") == task and first.get("task_whole") is True:
        return True
    repo = first.get("repo_id") or first.get("repo_root") \
        or row.get("repo_id") or row.get("repo_root")
    wholes, why = taskkey.lane_wholes(repo if isinstance(repo, str) else None)
    if why:
        return False
    return any(task in (wholes.get(taskkey.lane_name(lane)) or ())
               for lane in (row.get("lane"), first.get("lane")) if lane)


#: Set on the verbs auto-land runs as children: its LAND step runs the land
#: step itself, with the LAND number, so the hand path leaves it.
DEFER_ENV = "HELM_LAND_STEP_BY"
AUTO_LAND = "auto-land"

# What the step did with the task, one word each. LANDED: a whole land's
# task owes a seen-working check (helm/observed.py); a land closes no task.
LANDED, STAYS_OPEN, ASKED = "landed", "stays-open", "asked"
ALREADY_CLOSED, NO_TASK, NOT_IN_LEDGER = ("already-closed", "no-task",
                                          "not-in-ledger")
QUESTION = ("is the whole ask done? close it, narrow its title, or file the "
            "remainder with --continues %s")


def words(land):
    """The land in words: "LAND 537 <sha12>", or "by hand at <sha12>" for
    a land no LAND number counted."""
    sha = str(land.get("sha") or "?")[:12]
    return "%s %s" % (land["label"], sha) if land.get("label") \
        else "by hand at %s" % sha


def _whole_words(land):
    return "in " + words(land) if land.get("label") else words(land)


def _at(land):
    return "(lane %s at %s)" % (land.get("lane") or "?",
                                str(land.get("tip") or "?")[:12])


def question(land, known=None):
    """The one question a land that did not carry the whole ask comments on
    its task. `--continues` names the task's story root when the task is a
    part of a bigger ask (`tasks.story_facts`: a root other than itself), so
    the remainder files as a story sibling and the landed task can close; a
    root task keeps `--continues <itself>`. `known` is a task ledger snapshot;
    without one the ledger is read strictly, and an unreadable ledger names
    the task itself."""
    task = land["task"]
    if known is None:
        known, bad = tasks.snapshot(strict=True)
        known = known or {}
    root = (tasks.story_facts(known).get(task) or {}).get(
        "story_root") or task
    return "landed %s %s: %s" % (words(land), _at(land),
                                  QUESTION % root)


def stays_open(land, kids):
    """What a whole land comments on a task it cannot close, naming the
    open sub-tasks that keep it open."""
    return "landed whole %s %s: not closed, because its %s %s %s open" % (
        _whole_words(land), _at(land),
        "sub-task" if len(kids) == 1 else "sub-tasks", ", ".join(kids),
        "is" if len(kids) == 1 else "are")


def owes(land, rec):
    """What a whole land comments on the task it leaves owing a check."""
    from . import observed
    return "landed whole %s %s: %s" % (_whole_words(land), _at(land),
                                       observed.note(rec, land["task"]))


_LAND = (r"(?:(?:in )?(LAND \d+) ([0-9a-f]{7,40})|by hand at "
         r"([0-9a-f]{7,40}))")
_ASKED = re.compile(r"^landed %s \(lane (\S+) at (\S+)\): is the whole ask "
                    r"done\?" % _LAND)
_STAYS = re.compile(r"^landed whole %s \(lane (\S+) at (\S+)\): (?:not "
                    r"closed, because |owes a seen-working check by @)"
                    % _LAND)


def parse_comment(text):
    """{land, sha, lane, tip, whole} for a comment the land step wrote (a
    question, or a whole land's stays-open or owes-a-check note), else
    None. `land` is the
    LAND label, None for a hand land."""
    text = str(text or "")
    for pattern, whole in ((_ASKED, False), (_STAYS, True)):
        hit = pattern.match(text)
        if hit:
            label, sha, hand, lane, tip = hit.groups()
            return {"land": label, "sha": sha or hand, "lane": lane,
                    "tip": tip, "whole": whole}
    return None


def _report(land):
    return {"task": land.get("task"), "why": land.get("why"),
            "lane": land.get("lane"), "row": land.get("row"),
            "action": None, "reason": None, "room": None, "asked": False,
            "check_owner": None, "check_role": None, "restart": None,
            "open_children": [], "findings_closed": [], "findings_errors": [],
            "discharged": [], "kept_open": [], "flagged": [], "errors": []}


def _commented(task_row, text):
    return any(isinstance(c, dict) and c.get("text") == text
               for c in tasks.comments_of(task_row))


def _comment(task, text, land, report, known=None):
    """Comment `text` on `task` once."""
    row = (known or {}).get(task)
    if row is None:
        row = tasks.get(task)
    if row is None or _commented(row, text):
        return
    _row, err = tasks.comment(task, text, by=land.get("by"))
    if err:
        report["errors"].append("%s was not commented: %s" % (task, err))


def _open_below(known, task):
    """The open rows below `task` at any depth, board order."""
    facts = tasks.story_facts(known)
    return [r["id"] for r in tasks.board_order(
        [r for r in (facts.get(task) or {}).get("below") or ()
         if r.get("status") in tasks.OPEN_STATUSES])]


def _ask(land, report, post, known=None):
    """Ask the question in the task's room, addressed to the lane's author,
    once (`post(room, text, key)` -> None or why it did not post)."""
    from . import review_door
    task = land["task"]
    room = review_door.task_room(land.get("scope") or "local", task)
    report["room"] = room
    author = land.get("author")
    text = "%s%s %s" % ("@%s " % author if author else "", task,
                        question(land, known))
    key = "land-question:%s:%s:%s" % (task, land.get("lane") or "?",
                                      str(land.get("tip") or "?")[:12])
    try:
        err = (post or _post)(room, text, key)
    except Exception as exc:                            # noqa: BLE001
        err = "%s: %s" % (type(exc).__name__, exc)
    if err:
        report["errors"].append("the question was not asked in %s: %s"
                                % (room, err))
    else:
        report["asked"] = True


def _post(room, text, key):
    """The question into `room` as this process's seat, keyed so a second
    run returns the first row (`chat.post` event_id)."""
    from . import chat
    got = chat.post(text, room=room, sign=False, event_id=key)
    return None if isinstance(got, dict) and got.get("id") \
        else "the room took no row"


def _landed(land, row, report, post):
    """A whole land's task owes a seen-working check (helm/observed.py):
    stamp LANDED with its one check owner, comment it once, and tell the
    owner once. Auto-land's announcement carries the land line, which
    @mentions the owner; a hand land asks it in the task's room."""
    from . import observed
    task = land["task"]
    rec, err = observed.stamp(task, row, {
        "label": land.get("label"), "sha": land.get("sha"),
        "lane": land.get("lane"), "tip": land.get("tip"),
        "restart": land.get("restart")})
    if err:
        report["errors"].append("%s was not stamped LANDED: %s; the step "
                                "run again stamps it" % (task, err))
        return
    report["action"] = LANDED
    report["check_owner"], report["check_role"] = rec.get("owner"), \
        rec.get("role")
    report["restart"] = rec.get("restart")
    text = owes(land, rec)
    _comment(task, text, land, report)
    if land.get("by") == AUTO_LAND:
        return
    from . import review_door
    room = review_door.task_room(land.get("scope") or "local", task)
    report["room"] = room
    try:
        err = (post or _post)(room, "@%s %s %s" % (
            rec.get("owner") or "?", task, text),
            "land-observe:%s:%s" % (task, str(rec.get("sha") or "?")[:12]))
    except Exception as exc:                            # noqa: BLE001
        err = "%s: %s" % (type(exc).__name__, exc)
    if err:
        report["errors"].append("the check owner was not told in %s: %s"
                                % (room, err))
    else:
        report["asked"] = True


def _task_step(land, report, post):
    """Stamp a whole land's task LANDED, owing a seen-working check, or
    comment and ask the one question."""
    task = land.get("task")
    if not task:
        report["action"] = NO_TASK
        report["why"] = land.get("why") or taskkey.NO_TASK
        return
    known, bad = tasks.snapshot(strict=True)
    if bad:
        report["errors"].append("the task ledger could not be read (%s), so "
                                "%s was neither closed nor asked"
                                % (bad, task))
        return
    row = known.get(task)
    if not isinstance(row, dict):
        report["action"] = NOT_IN_LEDGER
        return
    if row.get("status") not in tasks.OPEN_STATUSES:
        report["action"] = ALREADY_CLOSED
        return
    if land.get("whole"):
        kids = _open_below(known, task)
        if not kids:
            _landed(land, row, report, post)
            return
        report["action"], report["open_children"] = STAYS_OPEN, kids
        _comment(task, stays_open(land, kids), land, report, known)
        return
    report["action"] = ASKED
    _comment(task, question(land, known), land, report, known)
    _ask(land, report, post, known)


def _findings(land, report, close_findings):
    """Close the open findings the landed chain filed, "cured in <land>"
    (helm/review_findings.py), never an id in `keep`: a task a live lane
    serves by record or number, or a finding whose naming tip is not proven
    strictly earlier than the LAND. What it could not close is in
    `findings_errors`."""
    if not land.get("chain"):
        # A READ THAT FAILED IS SAID (task/3862): nothing of the chain is
        # closed, and the land says why, never a silent nothing.
        if land.get("findings_unread"):
            report["findings_errors"].append("%s; nothing was closed"
                                             % land["findings_unread"])
        return
    from . import review_findings
    label = land.get("label") or "a hand land at %s" % str(
        land.get("sha") or "?")[:12]
    keep = set(land.get("keep") or ()) | ({land["task"]} if land.get("task")
                                          else set())
    closed, errors = (close_findings or review_findings.close_landed)(
        land["chain"], label, keep)
    report["findings_closed"] = list(closed or ())
    report["findings_errors"] = list(errors or ())


def _live(row):
    """Is dispatch `row` still owed: open, held, or a verdict not yet
    closed, and not retired?"""
    from . import dispatches, query
    return isinstance(row, dict) \
        and str(row.get("status") or "") in ("open", "held", "verdict") \
        and not dispatches._close_retired_by(row) \
        and not query.query_is_retired_admin(row)


def chain_facts(row, current):
    """{chain_rows, lane_rows} — the OTHER live rows of the landed `row`'s
    chain ({id, kind, status, polarity}), and the live rows of its lane in
    its repository with no chain link to it ({id, task})."""
    from . import review_findings
    chain = review_findings.chain_of(row)
    rid, lane, repo = row.get("id"), row.get("lane"), row.get("repo_id")
    chain_rows, lane_rows = [], []
    for other in (current or {}).values():
        if not _live(other) or other.get("id") == rid:
            continue
        if chain and review_findings.chain_of(other) == chain:
            chain_rows.append({k: other.get(k) for k in
                               ("id", "kind", "status", "polarity")})
        elif lane and other.get("lane") == lane \
                and other.get("repo_id") == repo:
            task, _why = resolve(other, current)
            lane_rows.append({"id": other["id"], "task": task})
    return {"chain_rows": chain_rows, "lane_rows": lane_rows}


def _doors(row, live):
    """The close doors a chain row is offered, in order: `landed` needs the
    delivery the land declared (`live` None: none was)."""
    polarity = str(row.get("polarity") or "").lower()
    if row.get("status") == "verdict":
        doors = ["landed", "carried"] if polarity == "approve" \
            else ["carried"]
    elif row.get("kind") == "build":
        doors = ["landed", "discharged", "carried"]
    else:
        doors = ["discharged", "carried"]
    return [d for d in doors if d != "landed" or live is not None]


def _close_row(rid, reason, evidence, live=None, restart=None):
    """None, or why the door refused: `helm lr close <rid> --reason
    <reason>` in this process, its own sweep off (the land's own close ran
    that)."""
    from . import landreq
    kw = {"live": bool(live), "needs_restart": restart} \
        if reason == "landed" else {}
    _out, err = landreq.close(rid, reason, evidence=evidence, fan_out=False,
                              **kw)
    return err


def kept_open(land, rid, why):
    """What the task says of a chain row every door refused."""
    return "row %s of the landed chain is still open after %s: %s" % (
        str(rid)[:12], words(land), why)


def flagged(land, rid):
    """What the task says of a same-lane row with no chain link."""
    return ("row %s (lane %s) is still open after %s: it has no chain link "
            "to the landed row %s, so the land did not close it; close it "
            "by its own door if the land carried its work"
            % (str(rid)[:12], land.get("lane") or "?", words(land),
               str(land.get("row") or "?")[:12]))


def _rows(land, report, close_row):
    """Offer each live row of the landed chain to its doors; flag each
    same-lane row with no chain link."""
    closer = close_row or _close_row
    evidence = "%s: its chain landed through row %s at %s" % (
        words(land), str(land.get("row") or "?")[:12],
        str(land.get("tip") or "?")[:12])
    skip = set(land.get("skip") or ())
    for row in land.get("chain_rows") or ():
        if row["id"] in skip:
            continue
        why = "no close door fits a %s row" % (row.get("status") or "?")
        for door in _doors(row, land.get("live")):
            err = closer(row["id"], door, evidence, live=land.get("live"),
                         restart=land.get("restart"))
            if not err:
                report["discharged"].append((row["id"], door))
                break
            why = "%s: %s" % (door, err)
        else:
            report["kept_open"].append((row["id"], why))
            if land.get("task"):
                _comment(land["task"], kept_open(land, row["id"], why), land,
                         report)
    for row in land.get("lane_rows") or ():
        task = row.get("task") or land.get("task")
        report["flagged"].append((row["id"], task))
        if task:
            _comment(task, flagged(land, row["id"]), land, report)


def run(land, close_row=None, post=None, close_findings=None):
    """The land step for one landed row -> its report (see the module doc).
    `land`: {task, why, whole, lane, tip, row, author, label, sha, scope,
    chain, findings_unread, step_unread, chain_rows, lane_rows, skip, live,
    restart, by}; `findings_unread`/`step_unread` say a read that failed
    before the step, and the report says them. The chain's
    findings close first, so the ones the land cured never hold a whole task
    open; the chain's other rows come last. `post(room, text, key)` asks the
    room, `close_findings(chain, label, keep)` closes the findings and
    `close_row(rid, door, evidence, live, restart)` asks one door. Never
    raises: the land is on trunk whatever this says, and what it could not
    do is in `errors` (the findings' in `findings_errors`)."""
    if not land.get("task") and land.get("why") \
            and land["why"] != taskkey.NO_TASK:
        # A saved compose (or a hand land) can carry stale chain facts. Task
        # UNKNOWN never gives them authority to close another task's findings
        # or sibling rows; the already landed tip is still reported.
        land = dict(land, chain=None, chain_rows=[], lane_rows=[],
                    findings_unread=("the car's task is UNKNOWN (%s); chain "
                                     "findings stay open" % land["why"]),
                    step_unread=("the car's task is UNKNOWN; chain rows stay "
                                 "open"))
    report = _report(land)
    if land.get("step_unread"):
        report["errors"].append(land["step_unread"])
    for step in (lambda: _findings(land, report, close_findings),
                 lambda: _task_step(land, report, post),
                 lambda: _rows(land, report, close_row)):
        try:
            step()
        except Exception as exc:                        # noqa: BLE001
            report["errors"].append("the land step failed (%s: %s)"
                                    % (type(exc).__name__, exc))
    return report


def of_row(rid, label, sha, live=None, restart=None, current=None):
    """(land, err) — the land of dispatch row `rid`, read from the dispatch
    ledger as it stands."""
    from . import dispatches, review_door, review_findings
    if current is None:
        current, bad = dispatches.snapshot()
        if bad:
            return None, ("the dispatch ledger could not be read (%s), so the "
                          "land step did not run" % bad)
    row = (current or {}).get(rid)
    if not isinstance(row, dict):
        return None, ("row %s is not in the dispatch ledger, so the land step "
                      "did not run" % str(rid)[:12])
    task, why = resolve(row, current)
    tip = row.get("reviewed_tip") or row.get("source_clean_tip") \
        or row.get("tip")
    # THE FINDINGS THE LAND ANSWERS, as the auto-land LAND step reads them
    # (`autoland.Ops.close_findings`): a finding whose naming tip is not
    # proven earlier than this tip, and every task a live lane serves by
    # record or number, stay open. A read that fails or raises closes
    # nothing of the chain, and the step says why (task/3862).
    unknown = not task and why != taskkey.NO_TASK
    chain = None if unknown else review_findings.chain_of(row)
    repo = row.get("repo_root")
    kept, named = [], set()
    unread = ("the car's task is UNKNOWN (%s); its chain findings stay open"
              % why) if unknown else None
    if not unknown:
        try:
            kept, why_kept = review_findings.named_at(
                chain, tip, current,
                repo if isinstance(repo, str) and repo else None)
            named, why_named = review_findings.lane_tasks(row, current)
            if chain is not None and (why_kept or why_named):
                unread = "the land could not read them: %s" % (why_kept
                                                                or why_named)
        except Exception as exc:                        # noqa: BLE001
            unread = "the land could not read them (%s: %s)" % (
                type(exc).__name__, exc)
    return dict(({"chain_rows": [], "lane_rows": []} if unknown else
                 chain_facts(row, current)), **{
        "task": task, "why": why, "whole": whole_of(task, row, current),
        "lane": row.get("lane"), "tip": tip, "row": row["id"],
        "author": dispatches.custodian_of(row) or None, "label": label,
        "sha": sha, "scope": review_door.pair_scope(row, current),
        "chain": None if unread else chain, "findings_unread": unread,
        "step_unread": ("the car's task is UNKNOWN; its chain's other rows "
                        "stay open") if unknown else None,
        "keep": sorted(set(kept) | set(named)), "by": "land-step",
        "live": live, "restart": restart}), None


def hand(rid, verb, sha, live=None, restart=None, close_row=None, post=None,
         close_findings=None):
    """The land step for row `rid`, landed by hand through `verb` at trunk
    `sha` -> its report (`lines` renders it). `live`/`restart` are the
    delivery the land declared, None when it declared none. Under DEFER_ENV
    the report is `deferred` and nothing is written."""
    if os.environ.get(DEFER_ENV) == AUTO_LAND:
        report = _report({"row": rid})
        report["deferred"] = verb
        return report
    try:
        land, err = of_row(rid, None, sha, live=live, restart=restart)
    except Exception as exc:                            # noqa: BLE001
        # THE STEP NEVER RAISES OUT OF A HAND VERB: the land it follows is
        # already recorded, so a read that raised is said, not a traceback.
        land, err = None, ("the land step could not read row %s (%s: %s), "
                           "so it did not run" % (str(rid)[:12],
                                                  type(exc).__name__, exc))
    if err:
        report = _report({"row": rid})
        report["errors"].append(err)
        return report
    return run(land, close_row=close_row, post=post,
               close_findings=close_findings)


def of_car(car, label, head, owed=(), keep=()):
    """The land of an auto-land car, from what its compose recorded; `keep`
    is the ids no finding close of this LAND may take."""
    return {"task": car.get("task"),
            "why": car.get("task_unknown") or (None if car.get("task")
                                               else taskkey.NO_TASK),
            "whole": bool(car.get("whole")), "lane": car.get("lane"),
            "tip": car.get("tip"), "row": car.get("id"),
            "author": car.get("author"), "label": label, "sha": head,
            "scope": car.get("scope"), "chain": car.get("chain"),
            "findings_unread": car.get("findings_unread"),
            "step_unread": car.get("step_unread"),
            "keep": sorted(keep or ()),
            "chain_rows": car.get("chain_rows") or [],
            "lane_rows": car.get("lane_rows") or [],
            "skip": car.get("closed_with") or [], "by": "auto-land",
            "live": not owed, "restart": "; ".join(owed) or None}


def lines(report):
    """What the step did, one line each, for a verb's output and the land
    line."""
    task, act = report.get("task"), report.get("action")
    out = []
    if report.get("deferred"):
        return ["auto-land's LAND step runs the land step, with the LAND "
                "number"]
    if act == LANDED:
        from . import observed
        out.append("%s LANDED — %s" % (task, observed.note(
            {"owner": report.get("check_owner"),
             "restart": report.get("restart")}, task)))
    elif act == STAYS_OPEN:
        out.append("%s stays open: landed whole, but its open sub-tasks %s "
                   "stay open under it"
                   % (task, ", ".join(report.get("open_children") or ())))
    elif act == ASKED:
        out.append("%s asked %s: is the whole ask done?" % (
            task, "in %s" % report["room"] if report.get("asked")
            else "on the task (the room was not reached)"))
    elif act == ALREADY_CLOSED:
        out.append("%s was already closed: nothing was closed or asked"
                   % task)
    elif act == NOT_IN_LEDGER:
        out.append("%s is not in the task ledger: nothing was closed or "
                   "asked" % task)
    elif act == NO_TASK:
        out.append("lane %s: no task (%s): nothing was closed or asked"
                   % (report.get("lane") or "?",
                      report.get("why") or taskkey.NO_TASK))
    out.extend("finding %s closed: %s" % tuple(pair)
               for pair in report.get("findings_closed") or ())
    out.extend("discharged row %s (%s)" % (str(rid)[:12], door)
               for rid, door in report.get("discharged") or ())
    out.extend("NOT discharged row %s: %s" % (str(rid)[:12], why)
               for rid, why in report.get("kept_open") or ())
    out.extend("FLAGGED row %s (lane %s): no chain link to the landed row, "
               "so it stays open%s" % (str(rid)[:12],
                                       report.get("lane") or "?",
                                       "; said on %s" % task if task else "")
               for rid, task in report.get("flagged") or ())
    out.extend("NOT DONE — the chain's findings: %s" % e
               for e in report.get("findings_errors") or ())
    out.extend("NOT DONE — %s" % e for e in report.get("errors") or ())
    return out
