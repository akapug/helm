"""The standing loop that carries every agent's HARNESS task list into the
fleet ledger.

WHY THIS EXISTS. A personal harness list is a free scratchpad — an agent
writes into it without ceremony, which is exactly what makes it useful and
exactly what makes it invisible. Work parked there is work no reader looks at,
and the owner asked for the ledger, singular, to be the place that answers
"who is doing what". So the lists keep their freedom and a LOOP carries them:
no agent has to notice, and nothing has to be migrated by hand.

ONE-WAY, ALWAYS. Personal list -> ledger. The ledger is authoritative once a
row is imported and NOTHING is ever written back. Two writers on one datum is
the defect this module would otherwise become: a mirror that also pushed would
have to decide whose edit wins, and there is no answer that is not a guess.

WHAT THE PRIOR PROCESS ACTUALLY SKIPPED, measured 2026-08-07 rather than
inherited. The 2026-08-05 corpus migration ran ONCE and its rows are stamped
`task-corpus-migration`. The row that commissioned this loop said the hole was
SKIPPED in_progress ITEMS. That is false, and the harness side is the only
side where it can be tested: the unmirrored backlog is 24 pending, 3 completed
and 2 in_progress, so if a status class were the hole that set would be mostly
in_progress. (Eleven migrated rows DO read in_progress — a row imported open
and claimed later looks identical, which is why the ledger side cannot settle
it.) The real hole is PROJECT SCOPE: every unmirrored session belongs to a
different project, and one of them holds tasks written a month BEFORE the
migration ran. A one-time drain scoped to one project skips everything that
lives somewhere else, forever, silently.

WHERE A FOREIGN PROJECT'S ROWS GO, ruled by the integrator: EVERYTHING lands
in the fleet ledger tagged `project:<cwd-slug>`, UNOWNED, and triage sorts —
because a tagged row in the ledger the owner actually reads beats an untagged
row in the "right" ledger nobody opens. The ROUTING DOOR is left open for a
project that runs its own helm scope; none do today, and the door has a
nameable producer rather than being speculative.

NOTHING IS SILENTLY SKIPPED. A task file that will not parse is REPORTED, not
passed over: a sweep whose whole job is to stop work disappearing must not
disappear work it could not read.

STATUS FLOWS TOO, STILL ONE WAY (task/1738). The first cut imported each item
once and never read it again, so a session that finished a step left its row
open forever: when measured, about 930 of ~1900 open ledger rows were mirror
rows. So every sweep re-reads the source item of every row it ever imported,
from the same walk that finds new items (no extra file reads):

  finished (completed, or an explicit `deleted`/`cancelled`) and the row is
  untouched since import -> the row is CLOSED through the ledger's normal
  close path, the reason naming the source;
  finished but a seat touched the row (claimed, owner, comment, edit, rank)
  -> the row stays open and gets ONE note from the mirror, never repeated;
  pending or in_progress -> nothing;
  gone (file or session) or unreadable -> nothing, because unknown is not
  done; unreadable is reported.

Nothing is written back to a harness list. The close is decided against the
row the ledger holds under its lock (`expect`), so a claim that lands between
this sweep's read and its write wins.
"""
import json
import os

from . import home, tasks, todos
from . import pk

TASKS_DIR = "tasks"          # <claude-home>/tasks/<session-id>/<task-id>.json
PROJECTS_DIR = "projects"    # <claude-home>/projects/<cwd-slug>/<sid>.jsonl
SOURCE = "harness-mirror"    # the tag every mirrored row carries
# THE DEDUP KEY LIVES IN TWO PLACES ON PURPOSE (measured hermetically).
# It rode only in `refs`, and `tasks.update(rid, refs=[...])` REPLACES the list
# wholesale — the update door does row.update(fields) — so one seat running
# `helm task update N --ref anything` deletes the key and the next sweep files
# a SECOND row for the same scratchpad item. Idempotence is a load-bearing
# claim of this module, and it was one ordinary edit away from a silent
# duplicate.
#
# So the key is ALSO written into `source`, which has no --source flag on the
# update door: a seat cannot reach it by any normal action. Both are read when
# deduping, so losing either one still dedups, and losing both takes two
# different deliberate edits rather than one routine one.
SOURCE_KEYED = "harness-mirror:"   # source carries the key: no CLI door to it
REF_PROJECT = "project:"     # refs entry: which project the session belongs to
REF_HARNESS = "harness:"     # refs entry: the STABLE DEDUP KEY
REF_STATUS = "harness-status:"   # the row's TRUE status upstream

# Harness vocabulary -> ledger vocabulary. `completed` becomes a CLOSED row
# rather than being dropped: a finished scratchpad item is history the ledger
# should hold, and dropping it would make the mirror lossy in the one
# direction nobody would notice.
#
# THE EXPLICIT TERMINAL STATUSES ARE todos.GONE, the vocabulary helm already
# parses (`deleted`, `cancelled`). They were missing here, so an unknown
# status fell to the `open` default and a cancelled item was imported as live
# work (task/1738).
STATUS = dict({"pending": "open", "in_progress": "in_progress",
               todos.DONE: "closed"}, **{g: "closed" for g in todos.GONE})
FINISHED = (todos.DONE,) + todos.GONE     # a source item that says "no more"
SOURCE_LIVE = ("pending", todos.ACTIVE)
MIRROR_ACTOR = "task-mirror"   # the `by` on the one note this loop writes
# AT MOST THIS MANY LEDGER WRITES PER SWEEP. Each close or note is one strict
# read of the whole ledger under its lock: 0.89s on a 36 MB ledger when
# measured, so the ~930-row backfill uncapped is a 14-minute tick holding the
# lock 930 times. 200 bounds a tick near three minutes; the rest are
# COUNTED as deferred and settle on the next sweeps.
WRITE_CAP = 200


def claude_homes():
    """Every harness config home whose task list this loop carries.

    `home.seat_claude_roots()` answers with PROJECTS roots, so the home is
    their parent and the task tree is a sibling — the seat homes are not a
    second store, they are the same shape under an isolated CLAUDE_CONFIG_DIR.
    """
    out, seen = [], set()
    # ONE DEFINITION OF THIS SEAT'S HARNESS HOME, and it is not this module's.
    # I re-implemented CLAUDE_CONFIG_DIR-or-~/.claude here and the hardcode
    # rung caught it: helm already answers this in `todos.claude_home()`,
    # documented as "the per-seat pin ... ~/.claude is the unpinned default".
    # A second copy is a second thing to keep true, and the first divergence
    # would be invisible — both would look right in isolation.
    for d in [todos.claude_home()] + [os.path.dirname(p)
                                      for p in home.seat_claude_roots()]:
        r = os.path.realpath(d)
        if r not in seen and os.path.isdir(os.path.join(r, TASKS_DIR)):
            seen.add(r)
            out.append(r)
    return out


def sessions(root):
    """[(session-id, task-dir)] for one harness home, id-sorted."""
    d = os.path.join(root, TASKS_DIR)
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    return [(n, os.path.join(d, n)) for n in names
            if os.path.isdir(os.path.join(d, n))]


def project_of(root, sid):
    """The cwd-slug whose transcripts hold this session, or None.

    The join is the transcript FILE, not a directory: a project dir contains
    `<session-id>.jsonl`. A session with no transcript anywhere routes
    NOWHERE, and that is a real state rather than an error — today's
    `probe-session` is exactly it — so the caller gets None and decides."""
    base = os.path.join(root, PROJECTS_DIR)
    try:
        slugs = sorted(os.listdir(base))
    except OSError:
        return None
    for slug in slugs:
        if os.path.exists(os.path.join(base, slug, "%s.jsonl" % sid)):
            return slug
    return None


WORKTREE_MARK = "-wt-"       # <repo>-wt/<lane> flattens into the cwd-slug
SUBAGENT_MARK = "--claude-worktree"   # a subagent's own worktree, same repo
LIVE = ("open", "in_progress")   # what the first pass carries (scope ruling)


def normalize_project(slug):
    """A cwd-slug is NOT a project, and treating it as one mis-tags the
    majority case.

    A seat that claims a lane MOVES INTO `<repo>-wt/<lane>`, so its transcripts
    land under a slug that reads like a separate project and is not one.
    Measured 2026-08-07: four of the top seven tags were helm lane worktrees —
    102, 93, 50 and 21 rows — so 266 rows would have been filed as if they
    belonged to four projects that do not exist. It mis-tags precisely the work
    seats do while doing lane work, which is most of it.

    The flattening is reversible because the marker survives it: a worktree
    path contains `-wt/`, which becomes `-wt-` in the slug, and everything from
    there on is the LANE name rather than the project."""
    if slug is None:
        # NONE MEANS UNROUTED AND MUST SURVIVE THIS FUNCTION. Coercing it to
        # "" here silently destroyed the caller's unrouted signal — nine rows
        # reported an empty project tag instead of being named as routing
        # nowhere, which is the same swallow this module refuses everywhere
        # else. Caught by reading the dry run rather than by a test.
        return None
    s = str(slug)
    for mark in (WORKTREE_MARK, SUBAGENT_MARK):
        i = s.find(mark)
        if i > 0:
            s = s[:i]
    return s


def read_tasks(task_dir):
    """([task dicts], [unreadable paths]) — never one at the expense of the
    other. A file that will not parse is HANDED BACK, because a loop whose
    purpose is to stop work vanishing must not vanish what it could not read."""
    rows, bad = [], []
    try:
        names = sorted(os.listdir(task_dir))
    except OSError:
        return rows, bad
    for n in names:
        if not n.endswith(".json") or n.startswith("."):
            continue
        p = os.path.join(task_dir, n)
        try:
            with pk.open_regular(p, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            bad.append(p)
            continue
        if isinstance(d, dict):
            rows.append(d)
        else:
            bad.append(p)
    return rows, bad


def harness_ref(root, sid, tid):
    """The STABLE DEDUP KEY carried in the mirrored row's refs.

    Seat home + session + task id, in that order. The session id alone is
    already unique, but the home is what makes the key legible to a human
    reading the row — and legibility is the point of putting provenance on the
    row rather than in a side table."""
    return "%s%s/%s/%s" % (REF_HARNESS, os.path.basename(root), sid, tid)


def mirrored(path=None):
    """([(dedup key, row)], {every key}, unavailable) from ONE strict read.

    The pairs are what the status leg settles; the key set is what the import
    leg dedups on. One read serves both, so the sweep costs no extra pass over
    the ledger. A row's key is its `source` key when present, since no CLI
    door reaches that field, else its first `harness:` ref — the only home the
    first cut of this loop wrote."""
    pairs, keys = [], set()
    rows, unavailable = tasks.snapshot(path, strict=True)
    if unavailable:
        return pairs, keys, str(unavailable)
    for row in (rows or {}).values():
        found = []
        src = str(row.get("source") or "")
        if src.startswith(SOURCE_KEYED):
            found.append(src[len(SOURCE_KEYED):])
        found.extend(str(r) for r in row.get("refs") or ()
                     if str(r).startswith(REF_HARNESS))
        keys.update(found)
        if found:
            pairs.append((found[0], row))
    return pairs, keys, None


def already_mirrored(path=None):
    """({dedup key} already imported, unavailable-reason or None).

    TWO VALUES, AND THE SECOND ONE IS WHY (task/2378). This read the ledger
    through `tasks.rows()`, which is `snapshot(path)[0]` — the unavailable
    channel dropped at the subscript. An unreadable ledger therefore produced
    an EMPTY dedup set, every ref missed, and `sweep` re-imported every
    harness task it had ever imported. That is the same false-zero class as
    the reassign manifest's, one notch worse: there the false zero reported
    nothing, here it WRITES, and `sweep` is a timer payload that runs
    unattended. An empty set and an unknown set are the same value and
    opposite instructions.

    READS BOTH HOMES. `refs` is where the key is legible to a human reading the
    row; `source` is where it survives an ordinary `helm task update --ref`,
    which replaces the refs list wholesale. Either alone dedups; requiring both
    to be lost is the point."""
    _pairs, keys, unavailable = mirrored(path)
    return (set(), unavailable) if unavailable else (keys, None)


def touches(row):
    """What anyone but this loop did to a mirrored row since import -> [why].

    Empty means untouched: open, no owner, no comment but the mirror's own,
    and `last_updated` still equal to the birth `ts` (every edit through the
    ledger's doors moves it; a custody move is caught by its own stamp). A row
    missing either stamp cannot be proven untouched, so it counts as touched.
    "updated" is named only when nothing more specific explains the edit."""
    out = []
    if row.get("status") == "in_progress":
        out.append("claimed")
    elif tasks.owner_of(row):
        out.append("owner set")
    if any(c.get("by") != MIRROR_ACTOR for c in row.get("comments") or ()):
        out.append("commented")
    if row.get("ranked_by"):
        out.append("ranked")
    if row.get("custody_updated") or row.get("takeover"):
        out.append("custody moved")
    ts, last = row.get("ts"), row.get("last_updated")
    if ts is None or last is None:
        out.append("edit history unknown")
    elif last != ts and not out:
        out.append("updated")
    return out


CLOSE_REASON = "source item %s in %s; closed by the task mirror"
NOTE = ("source item %s in %s. The task mirror left this row open because it "
        "was touched since import (%s); close it if the work is done.")


def _finished_report(write_cap=WRITE_CAP):
    return {"closed": [], "noted": [], "already_noted": 0, "live": 0,
            "vanished": 0, "unreadable": [], "unknown": [], "skipped": [],
            "refused": [], "deferred": 0, "write_cap": write_cap}


def settle(pairs, sources, torn, apply=True, path=None, write_cap=WRITE_CAP):
    """Bring each open mirrored row up to its source item's status -> report.

    `sources` maps a dedup key to the status its item holds NOW, from the
    sweep's own walk; `torn` holds the keys whose file would not parse. A key
    in neither is a source that is gone. Covers every row the ledger holds,
    not only this sweep's imports, which is what makes the first sweep a
    backfill."""
    out = _finished_report(write_cap)
    writes = 0
    for key, row in sorted(pairs, key=lambda kr: tasks.sort_key(kr[1])):
        if row.get("status") not in tasks.OPEN_STATUSES:
            continue
        rid = row.get("id")
        if key in torn:
            out["unreadable"].append(rid)
            continue
        if key not in sources:
            out["vanished"] += 1
            continue
        status = sources[key]
        if status in SOURCE_LIVE:
            out["live"] += 1
            continue
        if status not in FINISHED:
            out["unknown"].append({"row": rid, "ref": key, "status": status})
            continue
        if any(c.get("by") == MIRROR_ACTOR for c in row.get("comments") or ()):
            out["already_noted"] += 1
            continue
        where = key[len(REF_HARNESS):] if key.startswith(REF_HARNESS) else key
        why = touches(row)
        entry = {"row": rid, "ref": key, "status": status, "applied": False}
        if why:
            entry["touched"] = why
        bucket = out["noted" if why else "closed"]
        if not apply:
            bucket.append(entry)
            continue
        if writes >= write_cap:
            out["deferred"] += 1
            continue
        writes += 1
        if why:
            got, err = tasks.comment(rid, NOTE % (status, where, ", ".join(why)),
                                     by=MIRROR_ACTOR, path=path, expect=row)
        else:
            got, err = tasks.close(rid, CLOSE_REASON % (status, where),
                                   path=path, expect=row)
        if got is tasks.SKIPPED:
            out["skipped"].append({"row": rid, "why": err})
        elif err:
            out["refused"].append({"row": rid, "why": err})
        else:
            entry["applied"] = True
            bucket.append(entry)
    return out


def render_finished(fin, apply):
    """The status leg's lines for the verb -> [str]."""
    closed, noted = fin["closed"], fin["noted"]
    why = {}
    for n in noted:
        for w in n.get("touched") or ():
            why[w] = why.get(w, 0) + 1
    lines = ["  finished sources: %s %d untouched row(s); %s %d touched row(s)"
             " with one note each%s"
             % ("closed" if apply else "would close", len(closed),
                "noted" if apply else "would note", len(noted),
                " (%s)" % ", ".join("%s %d" % kv for kv in sorted(why.items()))
                if why else "")]
    if fin["already_noted"]:
        lines.append("  %d touched row(s) already noted — the note is written "
                     "once" % fin["already_noted"])
    if fin["live"]:
        lines.append("  %d mirrored row(s) whose source is still pending or "
                     "in_progress" % fin["live"])
    if fin["vanished"]:
        lines.append("  %d mirrored row(s) whose source is GONE — left open: "
                     "unknown is not done" % fin["vanished"])
    for rid in fin["unreadable"]:
        lines.append("  %s: its source file is UNREADABLE — left open, "
                     "reported" % rid)
    for u in fin["unknown"]:
        lines.append("  %s: source status %r is not one the mirror knows — "
                     "left open" % (u["row"], u["status"]))
    for s in fin["skipped"]:
        lines.append("  %s changed while this sweep ran — left for the next "
                     "one" % s["row"])
    for r in fin["refused"]:
        lines.append("  %s not settled — %s" % (r["row"], r["why"]))
    if fin["deferred"]:
        lines.append("  %d more deferred to the next sweep (at most %d writes "
                     "per sweep)" % (fin["deferred"], fin["write_cap"]))
    elif not apply and len(closed) + len(noted) > fin["write_cap"]:
        lines.append("  --apply writes at most %d per sweep; the rest settle "
                     "on later sweeps" % fin["write_cap"])
    return lines


def sweep(apply=True, path=None, live_only=True, write_cap=WRITE_CAP):
    """Carry every harness task the ledger has not seen, then settle every
    mirrored row whose source finished -> report dict.

    `apply=False` decides and writes NOTHING, so the loop can be inspected
    before it is trusted. That is not politeness: a sweep that mutates while
    being asked what it would do is the defect a dry run exists to rule out.
    """
    rep = {"homes": 0, "sessions": 0, "seen": 0, "imported": [], "already": 0,
           "unreadable": [], "unrouted": [], "refused": [],
           "closed_unmirrored": 0, "dedup_unreadable": None,
           "finished": _finished_report()}
    # THE DEDUP SOURCE IS A PRECONDITION, NOT AN INPUT. Without it every ref
    # misses and this loop re-files rows it already filed, so a sweep that
    # cannot read it must import NOTHING rather than import everything. It
    # returns EMPTY on that path too, so a caller reading only the set cannot
    # tell — which is exactly how this ran before.
    pairs, known, dedup_unreadable = mirrored(path)
    if dedup_unreadable:
        rep["dedup_unreadable"] = dedup_unreadable
        return rep
    sources, torn = {}, set()
    for root in claude_homes():
        rep["homes"] += 1
        for sid, task_dir in sessions(root):
            rep["sessions"] += 1
            rows, bad = read_tasks(task_dir)
            # UNPARSEABLE IS REPORTED BEFORE ANYTHING IS IMPORTED, so a run
            # that dies partway still leaves the operator the fact that a file
            # could not be read (integrator's ruling: a doctor finding, never
            # a silent skip).
            rep["unreadable"].extend(bad)
            torn.update(harness_ref(root, sid, os.path.basename(b)[:-5])
                        for b in bad)
            slug = normalize_project(project_of(root, sid))
            if slug is None:
                rep["unrouted"].append("%s/%s" % (os.path.basename(root), sid))
            for t in rows:
                rep["seen"] += 1
                tid = str(t.get("id") or "").strip()
                if tid:
                    sources[harness_ref(root, sid, tid)] = str(
                        t.get("status") or "").strip().lower()
                subject = str(t.get("subject") or "").strip()
                if not tid or not subject:
                    # A row that cannot name itself is not one this can file,
                    # and inventing an id would break the dedup key that makes
                    # the loop idempotent.
                    rep["refused"].append({"session": sid, "why": "no id or subject"})
                    continue
                ref = harness_ref(root, sid, tid)
                if ref in known:
                    rep["already"] += 1
                    continue
                status = STATUS.get(str(t.get("status") or ""), "open")
                if live_only and status not in LIVE:
                    # COUNTED, NEVER SILENTLY DROPPED (integrator's scope
                    # ruling). 446 of 628 harness tasks are finished
                    # scratchpad items, and importing them would bury a
                    # 403-row board under tombstones — the attention-budget
                    # leg of this loop's own design, violated at scale by the
                    # thing meant to serve it. So availability survives as a
                    # NAMED POINTER: the doctor line reports the count, and
                    # import-on-demand stays open because the dedup key is
                    # stable whenever someone asks for them.
                    rep["closed_unmirrored"] += 1
                    continue
                # AN UNOWNED in_progress ROW IS ONE THE LEDGER REFUSES, BY
                # DESIGN, and it is right to: "a backlog nobody is accountable
                # to is a list, not a ledger". The scope ruling asked for
                # open + in_progress imported UNOWNED, and those two cannot
                # both hold — the mirror cannot name an owner (claiming on a
                # seat's behalf is a lie) and must not drop the row.
                #
                # The ledger's own refusal names the way out: "file it `open`
                # and let the offer rung route it". So a live harness row
                # lands OPEN and its true harness status travels in refs, which
                # keeps the fact recoverable without asserting an accountability
                # that does not exist.
                filed = "open"
                refs = [ref, REF_PROJECT + (slug or "none"),
                        "%s%s" % (REF_STATUS, status)]
                note = str(t.get("description") or "").strip()
                if not apply:
                    rep["imported"].append({"ref": ref, "title": subject,
                                            "status": "open",
                                            "harness_status": status,
                                            "project": slug,
                                            "applied": False})
                    known.add(ref)
                    continue
                # force_new BECAUSE THIS SWEEP CARRIES ITS OWN DEDUP KEY AND
                # A STRONGER ONE. Every mirrored row is keyed by its harness
                # `ref` (the `known` set above and `SOURCE_KEYED + ref` on the
                # row), so this loop has already PROVEN each subject is not a
                # re-file of a row it mirrored before. `tasks.add`'s
                # title-similarity refusal answers a different question — a
                # human or agent typing a new title over a backlog they have
                # not read — and applying it here would redden a bulk import
                # over rows it can see are distinct. The bypass is explicit
                # and named rather than inherited from a door this leg
                # happens not to go through.
                row, err = tasks.add(
                    subject, "", note=note or None, refs=refs,
                    source=SOURCE_KEYED + ref, status=filed, path=path,
                    force_new=True)
                if err:
                    rep["refused"].append({"session": sid, "id": tid,
                                           "why": err})
                    continue
                known.add(ref)
                rep["imported"].append({"ref": ref, "title": subject,
                                        "status": "open",
                                        "harness_status": status,
                                        "project": slug,
                                        "row": row.get("id"), "applied": True})
    rep["finished"] = settle(pairs, sources, torn, apply=apply, path=path,
                             write_cap=write_cap)
    return rep


# ── cadence ─────────────────────────────────────────────────────────────────
#
# WHAT MAKES THE LOOP STANDING RATHER THAN CRANKABLE. A verb and a doctor rung
# make the sweep visible and audible; neither makes it RUN. The owner's ask was
# a complete loop — personal lists that flow to the ledger with no agent
# noticing — and a loop a human has to remember to turn is exactly the
# noticing this was built to remove.
#
# EXTERNAL CADENCE, NO DEMONS: the same shape autocompact and beacons use, so
# there is one way to schedule helm work rather than a new one per feature.
DEFAULT_INTERVAL_S = 900     # a scratchpad edit reaching the board within the
                             # quarter hour is soon enough; the sweep is reads
                             # plus an idempotent append, so cheaper is waste

_UNIT_SERVICE = """[Unit]
Description=helm harness-task mirror (one idempotent pass)

[Service]
Type=oneshot
WorkingDirectory=%(cwd)s
ExecStart=%(helm)s task mirror --apply
"""

_UNIT_TIMER = """[Unit]
Description=helm harness-task mirror cadence (external, no demons)

[Timer]
OnBootSec=%(interval)ss
OnUnitActiveSec=%(interval)ss

[Install]
WantedBy=timers.target
"""


def _timer_units(interval=DEFAULT_INTERVAL_S):
    # A persistent unit must never capture a DISPOSABLE WORKTREE's path: the
    # binary comes from the stable install, and the working directory is
    # DERIVED by folding a lane worktree back to the shared checkout. An
    # operator path baked into a tracked template is a never-track needle in
    # history, which is why neither of these is a literal.
    from . import work
    helm_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", "helm")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cwd = work.find_root(here) or here
    udir = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    return (os.path.join(udir, "helm-tasks-mirror.service"),
            _UNIT_SERVICE % {"helm": helm_bin, "cwd": cwd},
            os.path.join(udir, "helm-tasks-mirror.timer"),
            _UNIT_TIMER % {"interval": interval})


def ensure_timer(interval=DEFAULT_INTERVAL_S):
    """Install/refresh and enable the external cadence -> (ok, detail).

    Idempotent: writing the units and enabling again is the refresh path, so a
    changed interval or a moved checkout is repaired by re-running rather than
    by an operator noticing."""
    import shutil
    import subprocess
    from . import pk
    if interval < 1:
        return False, "interval must be at least 1 second"
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, ("systemctl unavailable; run `helm task mirror --apply` "
                       "from another scheduler")
    spath, service, tpath, timer = _timer_units(interval)
    try:
        os.makedirs(os.path.dirname(spath), exist_ok=True)
        pk.atomic_write(spath, service)
        pk.atomic_write(tpath, timer)
    except OSError as e:
        return False, "unit write failed: %s" % e
    for cmd in ([systemctl, "--user", "daemon-reload"],
                [systemctl, "--user", "enable", "--now",
                 "helm-tasks-mirror.timer"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return False, "%s failed: %s" % (
                " ".join(cmd), (r.stderr or r.stdout or "").strip()[:200])
    return True, "mirror cadence enabled every %ds (%s)" % (interval, tpath)
