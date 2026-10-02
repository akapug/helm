"""helm.observed — a land is done when someone named has seen it working.

THE GAP. A whole land closed its task at land (helm/landtask.py), so "done"
meant "on trunk". It did not mean live, and it did not mean anyone had used
it. The owner: "just making it live doesn't close the loop, verifying results
by dogfooding does". Done is live AND observed.

LANDED. A land that carries its task's whole ask no longer closes it. The
land step stamps a `landed` record on the task and names ONE check owner
(`owner_for`), and the task stays open, reading "landed — owes a
seen-working check by @X". The record says which land, which lane and tip,
and the restart the land still owes (a lead-posture land is not LIVE until
the leads relaunch, helm/autoland.py RESTART_RULES).

THE CHECK OWNER is the seat that filed the task (its `source`, the
requester). When that seat is dark (seat_usability.availability reads the
measured wall), a longtail seat (a team role that is not lead), or unknown
(no filer recorded, or not on the roster), the check goes to the task's
project lead, then to the integrator. The integrator is the last owner: a
check always has one.

TOLD ONCE. The auto-land announcement carries the land step's line, which
@mentions the owner with the exact command. A hand land posts the same line
to the task's room once (helm/landtask.py). While a seat owes checks, its
own stop whisper says "owes N seen-working checks: <ids>" with the command
(`stop_candidate`, a rung in helm/seats_work_offer.py), once per owed set,
and the fleet brief's ORDER lines name every owing seat (helm/brief.py).
There is no nag loop.

OBSERVED. `helm task observed <id> --evidence "<what was seen working,
where>"` closes the task. The evidence is free text and required. Any seat
may record it, and the record names who did.

THE PILE. A check older than CHECK_S moves ONCE to the next fit seat in the
owner chain, with one @mention, and stays there (`sweep`, run by the
auto-land tick). `helm doctor` counts the landed-not-observed tasks and the
oldest one's age. Nothing here holds a land or the train.

INSTALLED IS NOT LIVE, OUTSIDE A LAND TOO (task/3717). A binary installed
over a running one (a `~/.local/bin` tool a seat runs as its MCP server)
reaches no seat until that seat relaunches: the kernel reads the old exe as
"<path> (deleted)". `replaced` names each such process whose parent declares
a seat, so the doctor line beside the pile says which seats run which
replaced binary.
"""
import hashlib
import os
import time

from . import home, pk, tasks

#: How long a check may wait before it moves to the fallback owner, once.
CHECK_S = 24 * 3600

REQUESTER, LEAD, INTEGRATOR = "requester", "lead", "integrator"
ROLES = (REQUESTER, LEAD, INTEGRATOR)

#: What the observed verb asks for, in the words every surface prints.
EVIDENCE_HINT = "<what was seen working, where>"


def command(tid):
    """The exact command that records a check for `tid`."""
    return 'helm task observed %s --evidence "%s"' % (tid, EVIDENCE_HINT)


# -- the seams the owner rule reads (each is one existing read) -------------

def _roster():
    """The roster's seat names, or None when the roster cannot be read (an
    unread roster proves no seat unknown)."""
    from . import seats_roster
    snap, failed = seats_roster.roster_checked()
    return None if failed else set(snap or ())


def _dark(seat):
    """The availability text when `seat` is behind a measured wall, else
    None (seat_usability.availability, the one read)."""
    from . import seat_usability
    rec = seat_usability.availability(seat)
    return rec.get("text") if seat_usability.availability_walled(rec) \
        else None


def _role(seat):
    """The team role a whole teams read gives `seat`, or None."""
    from . import teams
    return teams.settled_role(seat)


def _lead(project):
    """(seat, None) for the project's one lead, else (None, why)."""
    from . import seatevents
    return seatevents._lead(project)


def _integrator():
    """The integrator seat, never None (loud when it had to default)."""
    from . import seats_integrator
    return seats_integrator.integrator_seat_or_default()


def _safe(fn, *args):
    """fn(*args), or None when the read raised: a read that fails proves
    nothing against a seat."""
    try:
        return fn(*args)
    except Exception:                                   # noqa: BLE001
        return None


def unfit(seat, roster):
    """None when `seat` can own a check, else why not, in words."""
    if not seat:
        return "unknown: none recorded"
    if roster is not None and seat not in roster:
        return "unknown: %s is not on the roster" % seat
    role = _safe(_role, seat)
    if role and role != "lead":
        return "a longtail seat: %s's team role is %s" % (seat, role)
    dark = _safe(_dark, seat)
    if dark:
        return "dark: %s is %s" % (seat, dark)
    return None


def _chain(row):
    """(role, lazy read -> (seat, why there is none)) in fallback order:
    the lead and the integrator are read only when an earlier seat does
    not fit."""
    return ((REQUESTER, lambda: (row.get("source"),
                                 "the task records no filer")),
            (LEAD, lambda: _safe(_lead, tasks.project_of_row(row))
             or (None, "its lead could not be read")),
            (INTEGRATOR, lambda: (_safe(_integrator), "unresolved")))


def owner_for(row, after=None):
    """(seat, role, passed) — the first fit seat of the owner chain, after
    role `after` when given; the integrator when no earlier seat fits.
    `passed` says why each earlier seat was passed over. (None, None, passed)
    when nothing is left after `after`."""
    chain = _chain(row)
    if after in ROLES:
        chain = chain[ROLES.index(after) + 1:]
    roster, passed = _safe(_roster), []
    for role, read in chain:
        seat, none = read()
        if role == INTEGRATOR and seat:
            return seat, role, passed
        why = unfit(seat, roster) if seat else "%s: %s" % (role, none)
        if not why:
            return seat, role, passed
        passed.append(why if not seat else "%s %s" % (role, why))
    return None, None, passed


# -- the record ----------------------------------------------------------------

def owes(row):
    """The open `landed` record `row` owes a check on, else None."""
    rec = (row or {}).get("landed")
    if not isinstance(rec, dict) or (row or {}).get("observed") \
            or row.get("status") not in tasks.OPEN_STATUSES:
        return None
    return rec


def _land_words(land):
    sha = str(land.get("sha") or "?")[:12]
    return "%s %s" % (land["label"], sha) if land.get("label") \
        else "by hand at %s" % sha


def stamp(tid, row, land, now=None):
    """(record, err): stamp `landed` on task `tid` (read as `row`) for
    `land` {label, sha, lane, tip, restart}. A record for the same land is
    left as it is and returned: the step run again writes nothing."""
    was = (row or {}).get("landed")
    if isinstance(was, dict) and was.get("sha") == land.get("sha"):
        return was, None
    seat, role, passed = owner_for(row)
    rec = {"land": land.get("label"), "sha": land.get("sha"),
           "lane": land.get("lane"), "tip": land.get("tip"),
           "ts": time.time() if now is None else now,
           "owner": seat, "role": role, "passed": passed,
           "restart": land.get("restart") or None, "moved": None}
    got, err = tasks.update(tid, expect=row, landed=rec)
    if got is tasks.SKIPPED:
        return None, "%s moved while the land read it" % tid
    return (None, err) if err else (rec, None)


def note(rec, tid):
    """What the task and the land line say of a check owed."""
    return "owes a seen-working check by @%s: `%s`%s" % (
        rec.get("owner") or "?", command(tid),
        "; owed before it is LIVE: %s" % rec["restart"]
        if rec.get("restart") else "")


def show_line(row):
    """The `helm task show` line for a check owed or recorded, or None."""
    rec, seen = (row or {}).get("landed"), (row or {}).get("observed")
    if isinstance(seen, dict):
        return "seen working (by @%s): %s" % (seen.get("by") or "?",
                                              seen.get("evidence"))
    if owes(row):
        return "landed — %s%s" % (note(rec, row["id"]), (
            " (moved from @%s after %dh)" % (rec["moved"]["from"],
                                             CHECK_S // 3600)
            if isinstance(rec.get("moved"), dict) else ""))
    return None


# -- observed ------------------------------------------------------------------

def observe(token, evidence, by, now=None):
    """(row, err): record what was seen working on the task `token` owes a
    check on, and close it. `evidence` is free text and required; `by` is
    the recording seat."""
    evidence = " ".join(str(evidence or "").split())
    if not evidence:
        return None, ("--evidence is required: %s" % EVIDENCE_HINT)
    row = tasks.get(token)
    if not row:
        return None, "%s does not exist" % token
    rec = owes(row)
    if not rec:
        return None, ("%s owes no seen-working check (%s): only a whole land "
                      "stamps one; close it with `helm task close %s "
                      "<reason>`" % (row["id"], "it is %s" % row["status"]
                                     if row.get("status") not in
                                     tasks.OPEN_STATUSES else
                                     "no land stamped it", row["id"]))
    seen = {"evidence": evidence, "by": by or None,
            "ts": time.time() if now is None else now,
            "owner": rec.get("owner"), "land": rec.get("land"),
            "sha": rec.get("sha")}
    reason = "seen working: %s (recorded by @%s; landed %s)" % (
        evidence, by or "?", _land_words({"label": rec.get("land"),
                                          "sha": rec.get("sha")}))
    got, err = tasks.update(row["id"], expect=row, status="closed",
                            closed_reason=reason, observed=seen,
                            open_children=tasks.OPEN_CHILDREN_REFUSE)
    if got is tasks.SKIPPED:
        return None, "%s moved while it was read; run it again" % row["id"]
    return (None, err) if err else (got, None)


# -- what is owed --------------------------------------------------------------

def owed(rows=None):
    """{seat: [task ids]} for every open task owing a check, board order."""
    if rows is None:
        rows = tasks.rows().values()
    out = {}
    for row in tasks.board_order(r for r in rows if owes(r)):
        out.setdefault(row["landed"].get("owner") or "?", []).append(
            row["id"])
    return out


def line(n, ids, cap=tasks.LEVER_NAMED):
    """"owes N seen-working checks: <ids>", the first `cap` ids named."""
    return "owes %d seen-working check%s: %s" % (n, "" if n == 1 else "s",
                                                  tasks.named(ids, cap))


#: How many owed ids the stop whisper names before "+N more".
WHISPER_NAMED = 3


def _memo_path():
    return os.path.join(home.global_dir(), ".state", "observed-owed.json")


def owed_by(seat):
    """[task ids] `seat` owes checks on, board order; [] when none, None
    when the task ledger cannot be read.

    THE STOP PATH'S READ, SO IT IS CHEAP WHEN NOTHING MOVED. The whole
    owed map is memoised beside the ledger's identity
    (eventledger.ledger_identity: one open and one fstat). A stop on an
    unchanged ledger reads that identity and one small JSON file; only a
    changed ledger pays one full read, and that read refreshes the memo for
    every seat. The memo is written only when the identity is the same
    before and after the read, so it always describes the bytes it came
    from."""
    from . import eventledger
    path = tasks.ledger_path()
    ident = eventledger.ledger_identity(path)
    key = list(ident) if ident else None
    memo = pk.read_json(_memo_path(), {}) if key else {}
    if isinstance(memo, dict) and key and memo.get("id") == key \
            and isinstance(memo.get("owed"), dict):
        return list(memo["owed"].get(seat) or ())
    rows, unavailable = tasks.snapshot()
    if unavailable:
        return None
    got = owed(rows.values())
    if key and eventledger.ledger_identity(path) == ident:
        try:
            pk.write_json(_memo_path(), {"id": key, "owed": got})
        except OSError:
            pass
    return list(got.get(seat) or ())


def stop_candidate(seat):
    """('observed:K:<ids digest>', line) for the stop whisper while `seat`
    owes K checks, else None. The fingerprint carries the owed set, so the
    whisper latch tells the seat once per set and again when it changes. A
    read that fails is silence, never a raise (the whisper law)."""
    from . import projscope, record
    try:
        ids = owed_by(seat) if seat else None
    except projscope.Expired:
        raise
    except Exception as _swallowed:
        record.swallow("observed.stop_candidate", _swallowed)
        return None
    if not ids:
        return None
    return ("observed:%d:%s" % (len(ids), hashlib.sha1(
        ",".join(ids).encode("utf-8")).hexdigest()[:12]),
        "%s — see each working, then `helm task observed <id> --evidence "
        '"..."`' % line(len(ids), ids, WHISPER_NAMED))


def pile(now=None, rows=None):
    """{n, over, oldest_s}: landed-not-observed tasks, how many are past
    CHECK_S, and the oldest one's age in seconds (None when there are
    none)."""
    now = time.time() if now is None else now
    if rows is None:
        rows = tasks.rows().values()
    ages = [now - float(r["landed"].get("ts") or now) for r in rows
            if owes(r)]
    return {"n": len(ages), "over": sum(a >= CHECK_S for a in ages),
            "oldest_s": max(ages) if ages else None}


def sweep(post, now=None, rows=None):
    """Move each check older than CHECK_S to its fallback owner, once, with
    one @mention through `post(text)`. -> [{task, from, to, posted}] for
    the checks moved. A check already moved, or with no other fit seat left
    in its chain, stays where it is."""
    now = time.time() if now is None else now
    if rows is None:
        rows = tasks.rows().values()
    moved = []
    for row in tasks.board_order(r for r in rows if owes(r)):
        rec = row["landed"]
        age = now - float(rec.get("ts") or now)
        if age < CHECK_S or rec.get("moved"):
            continue
        seat, role, passed = owner_for(row, after=rec.get("role"))
        if not seat or seat == rec.get("owner"):
            continue
        new = dict(rec, owner=seat, role=role, passed=passed,
                   moved={"from": rec.get("owner"), "to": seat, "ts": now})
        got, err = tasks.update(row["id"], expect=row, landed=new)
        if got is tasks.SKIPPED or err:
            continue
        text = ("@%s %s landed %s %dh ago and @%s has recorded no "
                "seen-working check; it is yours now, and it stays with you: "
                "`%s`" % (seat, row["id"], _land_words(
                    {"label": rec.get("land"), "sha": rec.get("sha")}),
                    age // 3600, rec.get("owner") or "?", command(row["id"])))
        err = post(text)
        moved.append({"task": row["id"], "from": rec.get("owner"),
                      "to": seat, "posted": not err})
    return moved


# -- installed is not live (task/3717) -----------------------------------------

_DELETED = " (deleted)"
_SEAT_KEY = b"HELM_CHAT_NAME="


def _ppid(root, pid):
    """The parent pid /proc/<pid>/stat records, or None."""
    try:
        with open(os.path.join(root, pid, "stat"), "rb") as fh:
            tail = fh.read().rsplit(b")", 1)[1].split()
        return tail[1].decode("ascii")
    except (OSError, IndexError, UnicodeDecodeError):
        return None


def _seat_of(root, pid):
    """The one HELM_CHAT_NAME entry of /proc/<pid>/environ, or None. Only
    that entry is read out; no other value leaves this function."""
    try:
        with open(os.path.join(root, pid, "environ"), "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    for item in raw.split(b"\0"):
        if item.startswith(_SEAT_KEY):
            return item[len(_SEAT_KEY):].decode("utf-8", "replace") or None
    return None


def replaced(proc=None):
    """[(binary, seat, pid)] for each process the kernel says runs a deleted
    exe and whose parent declares a seat (HELM_CHAT_NAME): installed, not
    live until that seat relaunches."""
    from . import procid
    root = procid.proc_root(proc)
    out = []
    try:
        pids = [p for p in os.listdir(root) if p.isdigit()]
    except OSError:
        return out
    for pid in pids:
        try:
            exe = os.readlink(os.path.join(root, pid, "exe"))
        except OSError:
            continue
        if not exe.endswith(_DELETED):
            continue
        parent = _ppid(root, pid)
        seat = _seat_of(root, parent) if parent else None
        if seat:
            out.append((exe[:-len(_DELETED)], seat, int(pid)))
    return sorted(out)


def replaced_line(found):
    """The doctor's one line for `replaced()`, or None when there is none."""
    if not found:
        return None
    from .seats_common import _seat_label
    by = {}
    for binary, seat, _pid in found:
        by.setdefault(binary, set()).add(_seat_label(seat))
    return ("%d seat process%s a replaced binary: %s, live on their next "
            "relaunch (task/3717)" % (
                len(found), " runs" if len(found) == 1 else "es run",
                "; ".join("%s in %s" % (b, ", ".join(sorted(s)))
                          for b, s in sorted(by.items()))))
