"""helm.taskhomes — every task row lives in exactly one project (task/3745).

ONE STORE, GLOBAL IDS, EVERY ROW IN ONE PROJECT. The ledger stays one file
with fleet-wide numbers, so a cited id means one row everywhere; what makes
each project a small ledger of its own is that every row names its project,
and each project's lead reads its own list and its own health line. A row
with no project is listed by no project's default `helm task list`, so no
lead burns it down.

REHOME gives a homeless row its project, from a PLAN FILE a person or a pass
wrote and a person read. The plan is JSON, in one of two shapes:

  nesting.json's shape   {"homes": {ID: ROW, ...},
                          "home_notes": {PROJECT: REASON, ...}, ...}
                         Other top-level keys (stories, reparent, notes) are
                         NOT read, and the dry run names them, so nobody
                         believes a reparent was applied.
  a bare mapping         {ID: ROW, ...}

ID is any spelling of a task id (56, "#56", "task/56"). ROW is a project
name, or an object {"project": NAME, "force": true|false, "from": NAME,
"reason": TEXT}. A row's own reason wins over its project's home_note.
`force` is the only way a row that already HAS a project is moved; without
it that row is refused by name. A forced row also names the project it
moves FROM, the one its author saw it in, and it is refused when the row is
anywhere else by the time it is judged: --apply judges a fresh snapshot,
not the dry run somebody read, and `from` is what ties the two together.

THE DRY RUN AND THE APPLY JUDGE THE SAME PLAN THE SAME WAY. Every plan row
gets one verdict against one snapshot of the ledger:

  home      the row is open and homeless (or forced), and the project is
            registered: --apply writes it
  already   the row already has exactly this project: nothing to do, which
            is what makes a re-run of an applied plan write nothing
  refused   not a task id, not in the ledger, closed, an unregistered
            project, already homed elsewhere without force, named twice, or
            a malformed plan row: never written, and named with its reason

--apply writes each `home` row as ONE project update through `tasks.update`
(the one task writer), attributed to the caller's admitted actor, and passes
the judged row as `expect`, so a row that moved after the plan was judged is
skipped rather than overwritten.

THE HEALTH LINE is what a project's lead reads about its own ledger, one line
per project: its open stories (counted the way `helm task list` counts them),
its open rows, the rows opened today and closed today, and the 7-day net
(opened minus closed). Opened is a row's filing stamp; closed is the stamp of
the event that closed it, read from the ledger's own event history in the
same one read. A row born closed is a tombstone and counts as neither.
TODAY IS THE HOST'S LOCAL DAY, from local midnight, the day `helm goal cycle`
counts from; the 7 days are today and the six local days before it. A row
counts under the project it has now.
"""
import json
import time

from . import tasks

NESTING = ("homes", "home_notes")
ROW_KEYS = ("project", "force", "from", "reason")
TITLE_CUT = 72


def read_plan(path):
    """(plan, err) — the plan file at `path`, parsed; exactly one is None.

    plan = {"shape": "nesting" | "bare", "ignored": [top-level keys not
    read], "rows": [entry, ...]} in the file's order. Each entry is {"key",
    "id", "project", "force", "reason", "bad"}: `id` is the normalized id or
    None, and `bad` says why a malformed row cannot be judged. A file that
    cannot be read, is not JSON, or has the wrong shape is refused whole."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, "cannot read the plan %s (%s: %s)" % (
            path, type(exc).__name__, exc)
    if not isinstance(doc, dict):
        return None, ("the plan %s is a JSON %s, not an object of {task id: "
                      "project}" % (path, type(doc).__name__))
    if isinstance(doc.get("homes"), dict):
        notes = doc.get("home_notes", {})
        if not isinstance(notes, dict):
            return None, ("the plan %s has a home_notes that is not an object "
                          "of {project: reason}" % path)
        shape, mapping = "nesting", doc["homes"]
        ignored = sorted(k for k in doc if k not in NESTING)
    else:
        shape, mapping, notes, ignored = "bare", doc, {}, []
    return {"shape": shape, "ignored": ignored,
            "rows": [_entry(k, v, notes) for k, v in mapping.items()]}, None


def _entry(key, value, notes):
    entry = {"key": key, "id": tasks.normalize_id(key), "project": None,
             "force": False, "moved_from": None, "reason": None,
             "bad": None}
    if isinstance(value, dict):
        extra = sorted(str(k) for k in value if k not in ROW_KEYS)
        force = value.get("force", False)
        was = value.get("from")
        reason = value.get("reason")
        if extra:
            entry["bad"] = ("unknown key(s) %s in the plan row — a row takes "
                            "%s" % (", ".join(extra), ", ".join(ROW_KEYS)))
        elif not isinstance(force, bool):
            entry["bad"] = "force must be true or false, not %r" % (force,)
        elif was is not None and not (isinstance(was, str) and was.strip()):
            entry["bad"] = "from must be a project name"
        elif reason is not None and not isinstance(reason, str):
            entry["bad"] = "reason must be text"
        entry.update(force=force is True, reason=reason,
                     moved_from=was.strip() if isinstance(was, str) else None)
        value = value.get("project")
    if isinstance(value, str) and value.strip():
        entry["project"] = value.strip()
    elif not entry["bad"]:
        entry["bad"] = ("the plan row names no project — a row is a project "
                        "name or {\"project\": NAME, \"force\": true|false, "
                        "\"reason\": TEXT}")
    if not entry["reason"] and isinstance(notes.get(entry["project"]), str):
        entry["reason"] = notes[entry["project"]]
    return entry


def judge(plan, snap, known):
    """Each plan row with its verdict against one ledger snapshot -> list.

    Every entry gains `verdict` ("home" | "already" | "refused"), `why`,
    `title`, `was` (the row's project now) and `row` (the judged snapshot
    row, handed to the writer as `expect`)."""
    out, seen = [], {}
    for e in plan["rows"]:
        j = dict(e, verdict="refused", why=None, title=None, was=None,
                 row=None)
        out.append(j)
        tid = e["id"]
        row = snap.get(tid) if tid else None
        if isinstance(row, dict):
            j.update(row=row, title=row.get("title") or "",
                     was=tasks.project_of_row(row))
        if not tid:
            j["why"] = "%r is not a task id" % (e["key"],)
        elif tid in seen:
            j["why"] = "named twice in the plan (also as %r)" % (seen[tid],)
        elif e["bad"]:
            j["why"] = e["bad"]
        elif e["project"] not in known:
            j["why"] = ("'%s' is not a registered project — `helm projects` "
                        "lists them" % e["project"])
        elif row is None:
            j["why"] = "not in the ledger"
        elif row.get("status") not in tasks.OPEN_STATUSES:
            j["why"] = ("%s — a closed row is history and is not rehomed"
                        % (row.get("status") or "no status"))
        elif j["was"] == e["project"]:
            j["verdict"] = "already"
        elif j["was"] and not e["force"]:
            j["why"] = ("already in project '%s' and the plan says '%s' — "
                        "give the row \"force\": true, \"from\": \"%s\" to "
                        "move it" % (j["was"], e["project"], j["was"]))
        elif j["was"] and not e["moved_from"]:
            j["why"] = ("a forced row names no \"from\" — give \"from\": "
                        "\"%s\", the project the row was read in, so a row "
                        "moved since then is refused rather than moved again"
                        % j["was"])
        elif e["moved_from"] and e["moved_from"] != j["was"]:
            j["why"] = ("is in '%s', not '%s' the plan moves it from — "
                        "somebody moved it since the plan was read; re-read "
                        "the dry run" % (j["was"] or "no project",
                                         e["moved_from"]))
        else:
            j["verdict"] = "home"
        if tid and tid not in seen:
            seen[tid] = e["key"]
    return out


def apply(judged, actor, path=None):
    """Write every `home` verdict as one project update -> judged, with each
    written row's `outcome` set to "homed", "skipped" or "failed" and `why`
    naming the reason for the last two."""
    for j in judged:
        if j["verdict"] != "home":
            continue
        row, err = tasks.update(j["id"], path=path, project=j["project"],
                                home_actor=actor, home_force=j["force"],
                                expect=j["row"])
        if row is tasks.SKIPPED:
            j.update(outcome="skipped", why=err)
        elif err:
            j.update(outcome="failed", why=err)
        else:
            j.update(outcome="homed")
    return judged


def _cut(text):
    text = " ".join(str(text or "").split())
    return text if len(text) <= TITLE_CUT else text[:TITLE_CUT - 3] + "..."


def header(path, plan):
    if plan["shape"] == "bare":
        return ("helm task rehome: plan %s — read as a bare {task id: "
                "project} mapping" % path)
    return ("helm task rehome: plan %s — read as nesting.json's shape: "
            "`homes` maps each task id to a project, `home_notes` gives a "
            "project's reason%s" % (path, "; not read: %s"
                                     % ", ".join(plan["ignored"])
                                     if plan["ignored"] else ""))


def line(j):
    """One plan row, as the dry run and the apply print it."""
    tid = j["id"] or str(j["key"])
    title = _cut(j["title"])
    if j["verdict"] == "refused":
        return "  %-11s REFUSED: %s%s" % (tid, j["why"],
                                          "  — %s" % title if title else "")
    if j["verdict"] == "already":
        return "  %-11s already in %s — nothing to do  %s" % (
            tid, j["project"], title)
    forced = "FORCED; " if j["was"] else ""
    why = ("reason: %s" % _cut(j["reason"]) if j["reason"]
           else "no reason in the plan")
    return "  %-11s %s -> %s  %s  (%s%s)" % (
        tid, j["was"] or "None", j["project"], title, forced, why)


def counts(judged):
    """{verdict or outcome: n} over the judged plan."""
    out = {}
    for j in judged:
        key = j.get("outcome") or j["verdict"]
        out[key] = out.get(key, 0) + 1
    return out


# ---------------------------------------------------------------------------
# the health line
# ---------------------------------------------------------------------------

HEALTH_KEYS = ("project", "open_stories", "open_rows", "opened_today",
               "closed_today", "net_7d")
NO_PROJECT_LABEL = "(no project)"


def _now():
    return time.time()


def health(path=None, now=None):
    """(report, err) — every project's health facts from ONE ledger read.

    report = {"today_since", "week_since", "tz", "lines": {project: line},
    "moved": {project: bool}}, where each line carries HEALTH_KEYS and the
    key None holds the rows with no project. `moved` says whether a row was
    opened or closed in the 7 days: a net of 0 can hide both, so it is kept
    beside the line rather than read back from it. `err` names an unreadable
    ledger."""
    born_open, closed_at = {}, {}

    def accept(row, prior):
        tid = str(row.get("id"))
        if prior is None:
            born_open[tid] = row.get("status") != "closed"
        if row.get("status") != "closed":
            closed_at.pop(tid, None)
        elif isinstance(prior, dict) and prior.get("status") != "closed":
            closed_at[tid] = tasks.stamp_epoch(row.get("last_updated"))
        return True

    snap, unavailable = tasks.snapshot(path, accept=accept)
    if unavailable:
        return None, "task ledger UNREADABLE (%s)" % unavailable
    from .goals import local_midnight
    now = _now() if now is None else now
    day = local_midnight(now)
    # Noon, six local days back, is inside that day whatever DST did in
    # between, so its midnight is the window's exact start.
    week = local_midnight(day - 6 * 86400 + 12 * 3600)
    facts = tasks.story_facts(snap)
    per = {}
    for tid, row in snap.items():
        if not isinstance(row, dict):
            continue
        got = per.setdefault(tasks.project_of_row(row), {
            "open": [], "opened_today": 0, "closed_today": 0,
            "opened_7d": 0, "closed_7d": 0})
        if row.get("status") in tasks.OPEN_STATUSES:
            got["open"].append(row)
        filed = tasks.filed_epoch(row) if born_open.get(tid) else None
        shut = closed_at.get(tid) if row.get("status") == "closed" else None
        for at, what in ((filed, "opened"), (shut, "closed")):
            if at is not None and at >= day:
                got[what + "_today"] += 1
            if at is not None and at >= week:
                got[what + "_7d"] += 1
    lines, moved = {}, {}
    for project, got in per.items():
        moved[project] = bool(got["opened_7d"] or got["closed_7d"])
        shown = tasks.board_order(got["open"])
        lines[project] = dict(zip(HEALTH_KEYS, (
            project, tasks.open_story_count(shown, snap, facts), len(shown),
            got["opened_today"], got["closed_today"],
            got["opened_7d"] - got["closed_7d"])))
    return {"today_since": day, "week_since": week,
            "tz": time.strftime("%Z", time.localtime(day)),
            "lines": lines, "moved": moved}, None


def _zero(project):
    return dict(zip(HEALTH_KEYS, (project, 0, 0, 0, 0, 0)))


def select(report, projects):
    """The lines for `projects` in that order; a project with no rows gets a
    line of zeros rather than no line."""
    return [report["lines"].get(p) or _zero(p) for p in projects]


def every_project(report, known):
    """Every registered project, then any other name a row carries, then the
    homeless rows. An unregistered name and the homeless line show only when
    they have an open row or a row opened or closed in the window."""
    live = [p for p, line in report["lines"].items()
            if line["open_rows"] or report["moved"].get(p)]
    extra = sorted(p for p in live if p is not None and p not in known)
    return list(known) + extra + ([None] if None in live else [])


def health_header(report):
    fmt = "%Y-%m-%d %H:%M %Z"
    return ("helm task health: today = since %s (this host's local time); "
            "7-day net = opened minus closed since %s"
            % (time.strftime(fmt, time.localtime(report["today_since"])),
               time.strftime(fmt, time.localtime(report["week_since"]))))


def health_line(line):
    """One project's line, the same facts --json carries under HEALTH_KEYS."""
    net = line["net_7d"]
    return "%-24s %d open %s, %d open %s; today %d opened, %d closed; " \
        "7-day net %s" % (
            line["project"] or NO_PROJECT_LABEL, line["open_stories"],
            "story" if line["open_stories"] == 1 else "stories",
            line["open_rows"], "row" if line["open_rows"] == 1 else "rows",
            line["opened_today"], line["closed_today"],
            "%+d" % net if net else "0")
