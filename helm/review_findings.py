"""helm.review_findings — a review's findings are sub-tasks of the task under
review, and its notes are comments on it (task/3742).

THE OWNER'S RULE: "like github issues and subissues, review
'rows' should just be subrows or comments or threads of some kind on existing
rows", and "we shouldnt do so at the expense of tracking needed work". So a
FIX verdict names each finding that is WORK with `--finding "<one line>"`,
and the verdict door files it as a task that continues the task the chain
serves; `--note "<one line>"` is an observation and becomes a comment on that
task, never a row. A new review therefore adds no top-level story.

ONE DOOR, ONE WRITE. The finding texts ride the verdict event itself, so the
verdict and the findings it names are recorded in one append: no verdict
stands without its findings, and a refusal records neither. The sub-task rows
are that record's projection into the task ledger, filed right after the
append. Filing is idempotent (one row per finding, keyed by the review row and
the text), so a filing the task ledger could not take is completed by running
the identical verdict again, which reconciles instead of re-recording. Every
check the task ledger makes is asked BEFORE the append (the parent, the
posture guard, a carried finding's chain), so a finding the ledger would
refuse refuses the verdict instead.

CARRY, DO NOT DUPLICATE. A later round naming the same issue passes
`--finding-carried task/N`: an open finding filed in this chain, under the
same task. The carried row takes a comment naming the round, and no second
row is filed.

WHO NAMES A FINDING. A row of the chain NAMES a finding when it carries an
UNRETRACTED FIX verdict that filed it or carried it, whatever that row's
later close: a FIX closed superseded, withdrawn, abandoned or discharged
still names what it read, because only a retraction withdraws what a
verdict said. Each closer below reads this one set.

CLOSE WHEN THE CHAIN ANSWERS IT, and never otherwise:
  held source-clean  a row of the chain is held source-clean at a tip that
                     strictly descends from the reviewed tip of every FIX that
                     named the finding (the one that filed it and each that
                     carried it): "cured, held source-clean at <tip12> (row
                     <id12>)". A hold at one of those tips, or at a tip git
                     cannot prove descends from all of them, closes nothing.
  LAND               the land of the chain: the auto-land LAND step,
                     "cured in LAND N", or a land recorded by hand (the land
                     step of helm/landtask.py), "cured in a hand land at
                     <sha12>". A finding a FIX named at the landed
                     tip, or at a tip that descends from it, stays open: the
                     LAND carries no cure for work read in a tree past it.
                     An unrelated tip likewise proves no cure; UNKNOWN
                     ancestry, absent landed tip or missing surviving FIX
                     namer refuses the close and says why. The set is
                     read at compose and again at LAND, so a FIX recorded
                     between them is seen.
  retracted          the last FIX that names it is retracted as reading
                     source-clean: "retracted (row <id12>)". While another
                     row of the chain still names it, it stays open; and a
                     retraction that reads fix, supersede or unknown closes
                     nothing and says so, for the next round to carry.
No closer closes a finding that any live lane of the repository records as
its task or serves by its open trailing task number (`lane_tasks`): that lane's
owner closes it (task/3643).

NO TASK. A legacy chain whose first row names no task cannot file findings:
refuse before the verdict append and again on filing replay. Work must be
attached to a task in the reviewed story, never a new top-level story.
"""
import re

FINDING_FLAG = "--finding"
CARRIED_FLAG = "--finding-carried"
NOTE_FLAG = "--note"
FLAGS = (FINDING_FLAG, CARRIED_FLAG, NOTE_FLAG)
#: A finding and a note are one line each, capped like a design finding.
TEXT_CAP = 256
#: At most this many of each on one verdict: a longer list is a document,
#: and one verdict event is bounded.
MAX_EACH = 20
#: The id a finding is checked under before the ledger mints its own.
_PLANNED = "task/planned-finding"
_ROW_ID = re.compile(r"[0-9a-f]{8,64}\Z")


def clean_args(findings=None, carried=None, notes=None):
    """({field: [value]}, err) — a verdict's review arguments, cleaned the one
    way the event stores them, with only the fields given. Each text is one
    printable line of at most TEXT_CAP characters, each carried id a task id,
    and none is given twice."""
    from . import dispatches, tasks
    out = {}
    for key, values, what in (("findings", findings, "finding"),
                              ("notes", notes, "note")):
        texts = []
        for value in values or ():
            if not isinstance(value, str):
                return None, "a %s is text, not %s" % (
                    what, type(value).__name__)
            text, err = dispatches._clean(value, what, TEXT_CAP)
            if err:
                return None, err
            if text in texts:
                return None, "the same %s is given twice: %s" % (what, text)
            texts.append(text)
        if len(texts) > MAX_EACH:
            return None, ("%d %ss on one verdict, over the %d it takes"
                          % (len(texts), what, MAX_EACH))
        if texts:
            out[key] = texts
    ids = []
    for value in carried or ():
        tid = tasks.normalize_id(value) if isinstance(value, str) else None
        if not tid:
            return None, ("--finding-carried %r is not a task id (task/N)"
                          % (value,))
        if tid in ids:
            return None, "--finding-carried %s is given twice" % tid
        ids.append(tid)
    if len(ids) > MAX_EACH:
        return None, ("%d carried findings on one verdict, over the %d it "
                      "takes" % (len(ids), MAX_EACH))
    if ids:
        out["findings_carried"] = ids
    return out, None


def named(review):
    """How many findings a verdict names: filed plus carried."""
    review = review or {}
    return len(review.get("findings") or ()) \
        + len(review.get("findings_carried") or ())


def derive_count(count, declared, review):
    """(count, err) — A FIX THAT NAMES ITS FINDINGS HAS COUNTED THEM: the
    count is the findings filed plus the ones carried. A given count that
    disagrees refuses, and so does one declared UNKNOWN beside them, because
    the count is known. A verdict that names none keeps the count it gave."""
    n = named(review)
    if not n:
        return count, None
    if "finding_count" in (declared or ()):
        return None, ("--finding-count UNKNOWN, but the count is known: %d "
                      "finding(s) named with %s/%s; drop --finding-count, it "
                      "is derived from them" % (n, FINDING_FLAG, CARRIED_FLAG))
    if count is not None and count != n:
        return None, ("--finding-count %s disagrees with the %d finding(s) "
                      "named (%s %d, %s %d); the count is derived from them, "
                      "so drop it or make it %d"
                      % (count, n, FINDING_FLAG,
                         len(review.get("findings") or ()), CARRIED_FLAG,
                         len(review.get("findings_carried") or ()), n))
    return n, None


def replayed(event):
    """The review fields a verdict event carries, as the reducer projects
    them: absent stays absent, and a malformed value is dropped rather than
    read as an answer."""
    out = {}
    for key in ("findings", "findings_carried", "notes"):
        value = event.get(key)
        if isinstance(value, list) and value \
                and all(isinstance(v, str) and v for v in value):
            out[key] = list(value)
    task = event.get("findings_task")
    if isinstance(task, str) and task:
        out["findings_task"] = task
    return out


def same(row, review):
    """Does the standing verdict record exactly these review arguments? A
    retry that names other findings is a different verdict, never a retry."""
    return all(list(row.get(key) or ()) == list(review.get(key) or ())
               for key in ("findings", "findings_carried", "notes"))


def chain_of(row):
    """The chain a row belongs to: its chain root, or its own id for a row
    that roots one or predates chains. None for an UNKNOWN chain, which no
    finding can be proven to be in."""
    from . import dispatches
    row = row or {}
    root = row.get("chain_root")
    if root == dispatches.CHAIN_UNKNOWN:
        return None
    value = root or row.get("id")
    return value if isinstance(value, str) and _ROW_ID.fullmatch(value) \
        else None


def chain_task(row, current, known):
    """(task or None, why) — the task the chain serves, by the one join the
    pair meld names its room with (`review_door.pair_key`: `taskkey.join`
    over the chain's FIRST row, its lane record unread), so a chain's
    findings and its room always name the same task. An unreadable first row
    or a task absent from the ledger is unknown, not proof of no task."""
    from . import dispatches, taskkey
    if current is None:
        current, bad = dispatches.snapshot()
        if bad:
            return None, ("the dispatch ledger could not be read (%s)" % bad)
    root = taskkey.first_row(row, current)
    if root is None:
        return None, ("the chain of row %s is UNKNOWN or unreadable"
                      % str((row or {}).get("id") or "?")[:12])
    # A caller can still hold the immutable pre-attach opener. Its effective
    # task is the validated chain-task projection in the CURRENT ledger fold,
    # not that old dict's absent `task` (nor a field forged onto its opener).
    # A missing current first row is UNKNOWN, not authority from that stale
    # caller dict, even if it claims a plausible attachment.
    root = current.get(root.get("id"))
    if root is None:
        return None, ("the chain of row %s is UNKNOWN or unreadable"
                      % str((row or {}).get("id") or "?")[:12])
    key = taskkey.join(row=root, current={}, lanes=False)
    if not key.task:
        return None, key.why or taskkey.NO_TASK
    if key.task not in (known or {}):
        return None, ("the chain names %s, which is not in the task ledger"
                      % key.task)
    return key.task, None


def _note(row, task, tip):
    """The note a filed finding carries: where it was found, and by whom."""
    where = ("found in review of %s: row %s" % (task, row["id"][:12])
             if task else "found in review row %s" % row["id"][:12])
    tail = "" if task else "; its chain names no task"
    return "%s at %s, read by @%s%s" % (
        where, str(tip or "?")[:12], row.get("recipient") or "?", tail)


def _owner(row):
    """The seat that must cure it: the lane's author, the row's custodian
    (`dispatches.custodian_of`). A name the task door would refuse as a
    placeholder files the finding unowned, for the offer rung to route."""
    from . import dispatches, tasks
    who = dispatches.custodian_of(row) or None
    return None if who is None or tasks._owner_placeholder_error(who) else who


def _project(row, task_row):
    """The task's project, or with no task the project of the chain's
    repository by the one mapper the write door uses."""
    from . import dispatches, tasks
    if task_row:
        return tasks.project_of_row(task_row)
    repo = row.get("repo_id")
    return dispatches._project_of(repo) if isinstance(repo, str) and repo \
        else None


def _carried_error(got, tid, chain, task):
    """Why `tid` cannot be carried by a round of `chain` serving `task`."""
    from . import tasks
    if not isinstance(got, dict):
        return "it is not in the task ledger"
    if not got.get("found_in"):
        return ("it is not a review finding; name new work with %s"
                % FINDING_FLAG)
    if chain is None or got.get("found_chain") != chain:
        return ("it was found in another chain (row %s), not this one"
                % str(got.get("found_in"))[:12])
    if got.get("status") not in tasks.OPEN_STATUSES:
        return ("it is %s; a carried finding is open work, and a new "
                "occurrence is a %s" % (got.get("status") or "of no status",
                                        FINDING_FLAG))
    if (got.get("continues") or None) != task:
        return ("it continues %s, not %s, the task this chain serves"
                % (got.get("continues") or "nothing", task or "nothing"))
    return None


def _open_in_chain(known, chain, text):
    """The id of an open finding of `chain` titled exactly `text`, or None:
    the same words named again in a later round are a carry."""
    from . import tasks
    return next((r["id"] for r in known.values() if isinstance(r, dict)
                 and r.get("found_chain") == chain and r.get("title") == text
                 and r.get("status") in tasks.OPEN_STATUSES), None)


def reported_work(row, titles):
    """Split an uncured report's titles into new work and open chain carries.

    Re-read a stale pre-verdict row to keep its standing fresh/carried split;
    filing a new task must not turn the same report into different work, and a
    carried task closing later must not turn the old verdict into new work.
    The verdict planner checks both against a fresh task snapshot before it
    appends; a task-ledger read failure cannot turn an existing carry into a
    duplicate new finding.
    """
    if not titles:
        return [], [], None
    from . import dispatches, tasks
    if row.get("status") != "verdict":
        current, bad = dispatches.snapshot()
        if bad:
            return None, None, ("the dispatch ledger could not be read (%s), "
                                "so the report's findings are UNKNOWN" % bad)
        row = current.get(row.get("id"), row)
    known, bad = tasks.snapshot(strict=True)
    if bad:
        return None, None, ("the task ledger could not be read (%s), so the "
                            "report's findings are UNKNOWN" % bad)
    chain = chain_of(row)
    standing = row.get("status") == "verdict" and row.get("polarity") == "fix"
    original = (row.get("findings") or ()) if standing else ()
    original_carried = (row.get("findings_carried") or ()) if standing else ()
    new, carried = [], []
    for title in titles:
        if title in original or any(
                isinstance(found, dict) and found.get("found_in") == row.get("id")
                and found.get("found_chain") == chain
                and found.get("title") == title for found in known.values()):
            new.append(title)
            continue
        tid = next((tid for tid in original_carried
                    if known.get(tid, {}).get("title") == title), None)
        if not tid:
            tid = _open_in_chain(known, chain, title) if chain else None
        (carried if tid else new).append(tid or title)
    return new, carried, None


def plan(row, current, polarity, review, advisory=False):
    """(event fields, err) — every check the task ledger would make of the
    findings, asked BEFORE the verdict is appended (see the module doc).
    {} when the verdict names no findings and no notes."""
    if not review:
        return {}, None
    if advisory:
        return None, ("%s, %s and %s ride a seat's own verdict: a model "
                      "run's advisory read files nothing; its recorder names "
                      "the findings on its own FIX" % FLAGS)
    if (review.get("findings") or review.get("findings_carried")) \
            and polarity != "fix":
        return None, ("%s and %s name the work a FIX hands back; %s hands "
                      "nothing back, so remaining work is `helm task add "
                      "<title> --continues <task>` and an observation is %s"
                      % (FINDING_FLAG, CARRIED_FLAG,
                         str(polarity or "this verdict").upper(), NOTE_FLAG))
    from . import posture, taskkey, tasks
    known, bad = tasks.snapshot(strict=True)
    if bad:
        return None, ("the task ledger could not be read (%s), so the "
                      "findings cannot be filed; nothing was recorded" % bad)
    task, why = chain_task(row, current, known)
    if why and why != taskkey.NO_TASK:
        return None, ("the findings cannot be filed (%s); nothing was "
                      "recorded" % why)
    if not task and (review.get("findings") or review.get("findings_carried")):
        return None, ("the findings cannot be filed: this review chain names "
                      "no task; give the next review --task task/N --part "
                      "(or --whole if it finishes the task), or helm work "
                      "claim <lane> --task task/N --part (or --whole); "
                      "nothing was recorded")
    if task and review.get("findings") and not _project(row, known.get(task)):
        return None, ("the findings cannot be filed: %s has no project; "
                      "home the reviewed task before recording work" % task)
    fields = dict(review)
    if task:
        fields["findings_task"] = task
        err = tasks._story_error(known, _PLANNED, task)
        if err:
            return None, "the findings cannot be filed under %s: %s" % (
                task, err)
        # NO OPEN CHILD IS BORN UNDER A CLOSED PARENT: a close refuses over
        # open sub-tasks (task/3742 slice a), and this door must not make
        # the same orphan from the other side.
        status = (known.get(task) or {}).get("status")
        if review.get("findings") and status not in tasks.OPEN_STATUSES:
            return None, ("%s is %s, so a finding filed under it would be "
                          "open work under a closed parent; reopen it "
                          "(`helm task update %s --status open`) if its "
                          "work goes on, or drop %s and file the work with "
                          "`helm task add`; nothing was recorded"
                          % (task, status or "of no status", task,
                             FINDING_FLAG))
    chain = chain_of(row)
    if review.get("findings") and chain is None:
        return None, ("row %s's chain is UNKNOWN, so a finding filed from it "
                      "could never be closed by its chain; nothing was "
                      "recorded" % row["id"][:12])
    for text in review.get("findings") or ():
        refused = posture.check("helm task add", text + "\n"
                                + _note(row, task, row.get("tip")))
        if refused:
            return None, "finding %r is refused by the task door: %s" % (
                text, refused)
        again = _open_in_chain(known, chain, text)
        if again:
            return None, ("finding %r is already open as %s in this chain: "
                          "name it with %s %s, not a second row"
                          % (text, again, CARRIED_FLAG, again))
    for tid in review.get("findings_carried") or ():
        err = _carried_error(known.get(tid), tid, chain, task)
        if err:
            return None, "%s %s: %s" % (CARRIED_FLAG, tid, err)
    return fields, None


def _commented(task_row, text):
    """Does `task_row` already carry a comment with exactly `text`?"""
    return any(isinstance(c, dict) and c.get("text") == text
               for c in (task_row or {}).get("comments") or ())


def file(row, current=None):
    """The review's findings and notes written to the task ledger, each once
    -> a report {task, no_task, filed, carried, noted, errors}, or None when
    the row names none. Never raises: the verdict is already durable, and a
    write that failed is said and completed by the same verdict run again."""
    if not any(row.get(k) for k in ("findings", "findings_carried", "notes")):
        return None
    report = {"task": row.get("findings_task"), "no_task": None, "filed": [],
              "carried": [], "noted": [], "errors": []}
    try:
        _file(row, current, report)
    except Exception as exc:                            # noqa: BLE001
        report["errors"].append("the task ledger write failed (%s: %s)"
                                % (type(exc).__name__, exc))
    return report


def _file(row, current, report):
    from . import taskkey, tasks
    known, bad = tasks.snapshot(strict=True)
    if bad:
        report["errors"].append("the task ledger could not be read (%s)"
                                % bad)
        return
    task, why = chain_task(row, current, known)
    if why and why != taskkey.NO_TASK:
        report["errors"].append("the findings cannot be filed (%s)" % why)
        return
    if task != report["task"]:
        report["errors"].append("the chain's task changed from %s to %s; "
                                "nothing was filed" % (report["task"] or
                                                       "none", task or "none"))
        return
    if not task and (row.get("findings") or row.get("findings_carried")):
        report["errors"].append("the review chain names no task; give the "
                                "next review --task task/N --part (or "
                                "--whole if it finishes the task)")
        return
    tip = row.get("reviewed_tip") or row.get("tip")
    rid, by = row["id"], row.get("recipient") or None
    mine = {r.get("title"): r for r in known.values()
            if isinstance(r, dict) and r.get("found_in") == rid}
    project = _project(row, known.get(task) if task else None)
    if row.get("findings") and not project:
        report["errors"].append("the reviewed task has no project; nothing "
                                "was filed")
        return
    for text in row.get("findings") or ():
        if text in mine:
            report["filed"].append((mine[text]["id"], text))
            continue
        # FORCE_NEW, WITH ITS REASON: a finding's identity is the review it
        # was found in, not its words, and a repeat within one chain is
        # CARRIED (`--finding-carried`), which files nothing.
        got, err = tasks.add(
            text, _owner(row), note=_note(row, task, tip), source=by,
            origin="agent", project=project, continues=task, force_new=True,
            found_in=rid, found_chain=chain_of(row))
        if err:
            report["errors"].append("finding %r was not filed: %s"
                                    % (text, err))
        else:
            report["filed"].append((got["id"], text))
    for tid in row.get("findings_carried") or ():
        said = ("carried: still open at %s, named again by the FIX on row %s"
                % (str(tip or "?")[:12], rid[:12]))
        got = known.get(tid) or {}
        if not _commented(got, said):
            _row, err = tasks.comment(tid, said, by=by)
            if err:
                report["errors"].append("carried %s was not noted: %s"
                                        % (tid, err))
                continue
        report["carried"].append((tid, got.get("title") or ""))
    for text in row.get("notes") or ():
        if not task:
            continue
        said = "review note (row %s at %s): %s" % (rid[:12],
                                                   str(tip or "?")[:12], text)
        if not _commented(known.get(task), said):
            _row, err = tasks.comment(task, said, by=by)
            if err:
                report["errors"].append("note %r was not commented on %s: %s"
                                        % (text, task, err))
                continue
            known[task] = _row
        report["noted"].append((task, text))


def with_report(row, current=None):
    """`row` with its filing report under `findings_report` (not a ledger
    field: the answer the verdict door says back)."""
    report = file(row, current)
    if report is None:
        return row
    out = dict(row)
    out["findings_report"] = report
    return out


def verdict_lines(row):
    """What the verdict door says back about the findings and notes."""
    report = row.get("findings_report")
    if not report:
        return []
    out = []
    task = report.get("task")
    for tid, text in report["filed"]:
        out.append("finding filed: %s (%s, owner @%s): %s" % (
            tid, "sub-task of %s" % task if task else "top-level",
            _owner(row) or "UNOWNED", text))
    for tid, title in report["carried"]:
        out.append("finding carried: %s (still open, no second row): %s"
                   % (tid, title))
    for tid, text in report["noted"]:
        out.append("note on %s (a comment, not a row): %s" % (tid, text))
    if not task:
        count = len(row.get("findings") or ())
        notes = len(row.get("notes") or ())
        out.append("no task: %s; %s%s" % (
            report.get("no_task") or "this chain names none",
            "its %d finding(s) are top-level rows found in row %s"
            % (count, row["id"][:12]) if count else "no finding is filed",
            "; its %d note(s) stay on this verdict row" % notes
            if notes else ""))
    out.extend("NOT FILED — %s; run the same verdict again to file it" % e
               for e in report["errors"])
    return out


def show_lines(lr):
    """`lr show`'s lines for the findings a row filed or carried and the
    notes it left, read from the dispatch row and the task ledger. Never
    raises: a page must render with the rest of what it knows."""
    try:
        from . import dispatches, tasks
        row = (dispatches.snapshot()[0] or {}).get(lr.get("id")) or {}
        if not any(row.get(k) for k in ("findings", "findings_carried",
                                        "notes")):
            return []
        known = tasks.snapshot()[0] or {}
    except Exception as exc:                            # noqa: BLE001
        return ["findings  UNREAD (%s)" % type(exc).__name__]
    mine = {r.get("title"): r for r in known.values()
            if isinstance(r, dict) and r.get("found_in") == row.get("id")}
    out = []
    for text in row.get("findings") or ():
        got = mine.get(text)
        out.append("finding   %s" % ("%-11s %-11s %s" % (
            got["id"], got.get("status") or "?", text) if got else
            "NOT FILED   %s" % text))
    for tid in row.get("findings_carried") or ():
        got = known.get(tid) or {}
        out.append("carried   %-11s %-11s %s" % (
            tid, got.get("status") or "?", got.get("title") or ""))
    for text in row.get("notes") or ():
        out.append("note      %s: %s" % (row.get("findings_task")
                                         or "no task", text))
    return out


def _open_findings(known, keep):
    """The open finding rows of the task ledger `keep(row)` selects."""
    from . import tasks
    return [r for r in known.values() if isinstance(r, dict)
            and r.get("found_in") and r.get("status") in tasks.OPEN_STATUSES
            and keep(r)]


def _close(rows_reasons):
    """Close each (row, reason) against the row as it was read ->
    (closed [(id, reason)], errors). A finding with open sub-tasks stays
    open, and its refusal is one of the errors: a closer never leaves open
    work under a closed parent (task/3742). A close the task ledger SKIPPED
    (the row moved since the read) is one of the errors too, never dropped:
    the finding stays open, and the closer says so."""
    from . import tasks
    closed, errors = [], []
    for got, reason in rows_reasons:
        out, err = tasks.close(got["id"], reason, expect=got,
                               open_children=tasks.OPEN_CHILDREN_REFUSE)
        if out is tasks.SKIPPED or err:
            errors.append("%s was not closed: %s" % (got["id"], err))
        else:
            closed.append((got["id"], reason))
    return closed, errors


def _read_open(keep):
    """(open finding rows, err) through the strict read a closer decides on."""
    from . import tasks
    known, bad = tasks.snapshot(strict=True)
    if bad:
        return None, "the task ledger could not be read (%s)" % bad
    return _open_findings(known, keep), None


def _naming_rows(finding, chain, current):
    """Every row of `chain` that NAMES `finding` (see the module doc): it
    carries an unretracted FIX verdict that filed or carried it, whatever
    that row's later close. The polarity is the test, not the status: only
    a verdict event projects a FIX, and a retraction projects it
    RETRACTED."""
    from . import dispatches
    return [r for r in (current or {}).values()
            if isinstance(r, dict) and chain_of(r) == chain
            and not r.get("verdict_retracted")
            and dispatches._replay_polarity(r.get("polarity")) == "fix"
            and (r.get("id") == finding.get("found_in")
                 or finding.get("id") in (r.get("findings_carried") or ()))]


def _naming_tips(finding, chain, current):
    """The reviewed tips of every FIX verdict of `chain` that filed or
    carried `finding`. EVERY ONE, never the newest: a carry says the finding
    still stood at its tip, and rounds stamped in one second have no order a
    clock can give."""
    return {str(r.get("reviewed_tip") or "")
            for r in _naming_rows(finding, chain, current)}


def lane_tasks(row, current):
    """({task id}, why) — protect every task any live repository lane serves:
    the chain's join, stored/cited ids, live lane records, and open numbered
    lane names via `landtask.lane_number` (the same fallback landtask.resolve
    uses). No closer owns another lane's task. An unreadable ledger, lane
    record, or branch listing closes nothing."""
    from . import taskkey, tasks
    first = taskkey.first_row(row, current)
    if first is None:
        return set(), ("the chain of row %s is UNKNOWN or unreadable"
                       % str((row or {}).get("id") or "?")[:12])
    repo = first.get("repo_id") or first.get("repo_root")
    records, why = taskkey.lane_records(repo if isinstance(repo, str)
                                        else None)
    if why:
        return set(), ("the lane records of %s could not be read (%s), so "
                       "which task a live lane serves is unknown"
                       % (repo, why))
    live, why = taskkey.live_lanes(repo if isinstance(repo, str) else None)
    if why:
        return set(), ("the live lanes of %s could not be read (%s), so "
                       "which task a live lane serves is unknown" % (repo, why))
    known, bad = tasks.snapshot(strict=True)
    if bad:
        return set(), "the task ledger could not be read (%s)" % bad
    from . import landtask
    key = taskkey.join(row=row, current=current, lanes=records)
    named = [key.task, first.get("task")] + list(key.cited or ())
    for values in records.values():
        named += sorted(values)
    named += [landtask.lane_number(lane, known) for lane in live
              if not records.get(lane)]
    return {tasks.normalize_id(v) for v in named
            if isinstance(v, str) and v} - {None, ""}, None


def close_on_hold(held, current):
    """(closed, errors) — the findings of `held`'s chain that its
    source-clean tip answers (see the module doc). The lane's own task is
    never one of them (`lane_tasks`): the hold that makes the lane landable
    does not close the task it serves."""
    tip = str(held.get("source_clean_tip") or "")
    chain = chain_of(held)
    repo = held.get("repo_root")
    if not tip or chain is None or not isinstance(repo, str) or not repo:
        return [], []
    try:
        own, why = lane_tasks(held, current)
        if why:
            return [], [why + "; nothing was closed"]
        found, err = _read_open(lambda r: r.get("found_chain") == chain
                                and r.get("id") not in own)
        if err:
            return [], [err]
        from . import vcs
        be = vcs.backend(repo)
        due = []
        for got in found:
            named_at = _naming_tips(got, chain, current)
            if not named_at or "" in named_at or tip in named_at \
                    or any(be.ancestry(repo, at, tip) != vcs.ANCESTOR
                           for at in named_at):
                continue
            due.append((got, "cured, held source-clean at %s (row %s)"
                        % (tip[:12], held["id"][:12])))
        return _close(due)
    except Exception as exc:                            # noqa: BLE001
        return [], ["the findings could not be closed (%s: %s)"
                    % (type(exc).__name__, exc)]


def named_at(chain, tip, current, root=None):
    """(ids, err) — keep a finding unless every naming FIX tip is PROVEN
    strictly before the landed tip. An equal or later naming tip stays open;
    unrelated trees also prove no cure. An absent landed tip or missing
    surviving FIX namer proves none either. UNKNOWN proofs are reported to
    compose or LAND so they close nothing of the chain. No chain means no
    closure work. Read at compose and again from a fresh fold at the LAND."""
    if not chain:
        return [], None
    found, err = _read_open(lambda r: r.get("found_chain") == chain)
    if err:
        return [], err
    if not tip:
        return sorted(got["id"] for got in found), (
            "the landed tip is UNKNOWN" if found else None)
    from . import vcs
    be = vcs.backend(root) if root else None
    kept, unknown, missing = [], [], []
    for got in found:
        tips = _naming_tips(got, chain, current)
        if not tips:
            kept.append(got["id"])
            missing.append(got["id"])
            continue
        for at in tips:
            if at == tip:
                kept.append(got["id"])
                break
            if not be or not at:
                unknown.append(got["id"])
                kept.append(got["id"])
                break
            before = be.ancestry(root, at, tip)
            if before == vcs.ANCESTOR:
                continue
            if before == vcs.UNKNOWN:
                unknown.append(got["id"])
                kept.append(got["id"])
                break
            after = be.ancestry(root, tip, at)
            if after == vcs.UNKNOWN:
                unknown.append(got["id"])
            # Both later and unrelated tips are unproved cures.
            kept.append(got["id"])
            break
    errors = []
    if missing:
        errors.append("no surviving FIX names open finding(s) %s"
                      % ", ".join(sorted(missing)))
    if unknown:
        errors.append("git ancestry could not prove the naming FIX tips for "
                      "%s relative to landed tip %s"
                      % (", ".join(sorted(set(unknown))), str(tip)[:12]))
    return sorted(set(kept)), "; ".join(errors) if errors else None


def close_landed(chain, land, keep=()):
    """(closed, errors) — every open finding of `chain`, closed by the LAND
    that landed it: `land` is the land's label ("LAND 384"). An id in `keep`
    is never one of them: a car's task (a land closes no car's task,
    task/3643), a task a live lane records (`lane_tasks`), and a finding
    named at the landed tip or past it (`named_at`). An unrelated tip is
    unproved; UNKNOWN ancestry, absent landed tip or missing surviving FIX
    namer refuses the close rather than curing it."""
    if not chain:
        return [], []
    keep = set(keep or ())
    try:
        found, err = _read_open(lambda r: r.get("found_chain") == chain
                                and r.get("id") not in keep)
        if err:
            return [], [err]
        return _close([(got, "cured in %s" % land) for got in found])
    except Exception as exc:                            # noqa: BLE001
        return [], ["the findings could not be closed (%s: %s)"
                    % (type(exc).__name__, exc)]


def close_retracted(row, current=None):
    """(closed, errors) — the open findings the retracted verdict on `row`
    filed or carried, each closed only when no other row of its chain
    names it (`_naming_rows`): that round read it still there, and the
    retraction does not answer it. So the last retraction of the FIXes that
    named a finding closes it, in whichever order they are retracted. The
    lane's own task is never one of them (`lane_tasks`), as on a hold: a
    retraction does not close the task the lane serves.

    ONLY A RETRACTION THAT READS SOURCE-CLEAN CLOSES (task/3862): one that
    reads fix, supersede or unknown says the work the FIX named may still
    stand, so its findings stay open, and the one said line (under `errors`)
    names them and how the next round carries them."""
    rid = str((row or {}).get("id") or "")
    if not rid:
        return [], []
    chain = chain_of(row)
    if chain is None:
        return [], ["the chain of row %s is UNKNOWN; nothing was closed"
                    % rid[:12]]
    carried = set((row or {}).get("findings_carried") or ())
    try:
        found, err = _read_open(lambda r: r.get("found_chain") == chain
                                and (r.get("found_in") == rid
                                     or r.get("id") in carried))
        if err:
            return [], [err]
        reads = (row or {}).get("retract_reads") or "unknown"
        if reads != "source-clean":
            return [], ["kept open: the retraction reads %s; carry them with "
                        "%s (%s)" % (reads, CARRIED_FLAG, ", ".join(
                            got["id"] for got in found))] if found else []
        if found and current is None:
            from . import dispatches
            current, unavailable = dispatches.snapshot()
            if unavailable:
                return [], ["the dispatch ledger could not be read (%s), so "
                            "whether a later round carried them is unknown"
                            % unavailable]
        if found:
            own, why = lane_tasks(row, current)
            if why:
                return [], [why + "; nothing was closed"]
            found = [got for got in found if got.get("id") not in own]
        return _close([(got, "retracted (row %s)" % rid[:12])
                       for got in found
                       if not any(r.get("id") != rid for r in _naming_rows(
                           got, got.get("found_chain"), current))])
    except Exception as exc:                            # noqa: BLE001
        return [], ["the findings could not be closed (%s: %s)"
                    % (type(exc).__name__, exc)]


def closed_lines(row):
    """What a hold or a retraction says back about the findings it closed."""
    out = ["finding %s closed: %s" % pair
           for pair in row.get("findings_closed") or ()]
    out += ["finding NOT closed — %s" % e
            for e in row.get("findings_close_errors") or ()]
    return out


def with_closed(row, closed, errors):
    """`row` with what a closer did, under keys the doors say back."""
    if not closed and not errors:
        return row
    out = dict(row)
    if closed:
        out["findings_closed"] = closed
    if errors:
        out["findings_close_errors"] = errors
    return out
