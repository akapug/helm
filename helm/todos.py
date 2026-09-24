#!/usr/bin/env python3
"""helm todos — the seat todo mirror: what each agent in the fleet is actually
working on, right now, readable without asking it.

The bridge is DIGEST + PULL, never a firehose (decision-spirit #23, the
attention budget). Todo state churns on nearly every turn, so:

  * PULL-FIRST is the primary surface. The recorder captures the seat's
    CURRENT todo list into per-session state; `helm todos` / `helm todos
    --all` / `/api/todos` / the roster row read it ON DEMAND. Nobody pays
    attention for a todo they did not ask about.
  * PUSH is a rate-capped exception. A chat line lands only on a transition
    a teammate would ACT on — a task finished, or an idle seat picking up a
    new in-progress task — collapsed to the latest state, at most once per
    MIN_POST_S per seat, and never at all when nothing materially changed.
  * The post is a ROOM line from the seat, NEVER an @mention and NEVER a DM.
    Mentions and DMs pierce mute and wake seats; a todo update must never
    wake the fleet. Every '@' is stripped from the posted text so a todo
    that merely CONTAINS "@name" cannot become a mention by accident.

Capture is harness-shaped, plural on purpose:
  * `TodoWrite`   — tool_input.todos, the whole list every call (also
                    tolerated as a JSON-encoded STRING, the 2.1.193 shape).
  * `TaskCreate` / `TaskUpdate` (and the legacy `TaskUpdateTODO`) — the
    Task* family the live fleet actually emits: create appends, update
    patches by id, status `deleted` tombstones. The id of a create lives
    only in the tool_result string ("Task #7 created successfully: …"), so
    it is parsed from there when present and synthesized when not.

State, beside the recorder's own, under the SESSION dir
<helm home>/_global/.state/reflex-state/<session_id>/todos.json:
    {"v":1,"ts","src","items":[{"id","text","status"}],
     "post":{"fp","ts"}}   <- the rate-cap ledger, same file, one write

Laws: stdlib only; the capture path is one JSON read + one atomic write, no
subprocess, no network; bounded (ITEM_CAP items, TEXT_CAP chars each); and
FAIL-CLOSED TOTAL — any trouble skips the mirror silently, never the seat's
tool call.
"""
import errno
import fcntl
import hashlib
import json
import os
import re
import stat as statmod
import sys
import tempfile
import time
import unicodedata

from . import fsops, home, openflags, pk

TODO_TOOL = "TodoWrite"
TASK_TOOLS = ("TaskCreate", "TaskUpdate", "TaskUpdateTODO")
TOOLS = (TODO_TOOL,) + TASK_TOOLS

ITEM_CAP = 64           # items kept per seat (a list longer than this is noise)
TEXT_CAP = 120          # chars per item text
STATE = "todos.json"
MIN_POST_S = 300        # per-seat floor between mirror posts (the rate cap)
ACTIVE = "in_progress"
DONE = "completed"
GONE = ("deleted", "cancelled")
STATUSES = ("pending", ACTIVE, DONE) + GONE

_CREATED_RE = re.compile(r"task\s*#?(\d+)", re.I)


# ── normalization ───────────────────────────────────────────────────────────

def _text(d):
    """The one human line for an item, across every field name the harnesses
    use (claude's todos carry `content`; the Task family carries `subject`)."""
    for k in ("subject", "content", "text", "title", "task", "activeForm",
              "description"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())[:TEXT_CAP]
    return ""


def _status(d):
    v = str(d.get("status") or d.get("state") or "").strip().lower()
    return v if v in STATUSES else "pending"


def _items(raw):
    """A todos array (list, or the legacy JSON-encoded string) -> normalized
    [{id,text,status}], deleted rows dropped, capped. Anything unusable
    answers None — the caller then leaves prior state untouched."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if not isinstance(raw, list):
        return None
    out = []
    for i, d in enumerate(raw):
        if not isinstance(d, dict):
            continue
        t = _text(d)
        if not t:
            continue
        st = _status(d)
        if st in GONE:
            continue
        out.append({"id": str(d.get("id") or d.get("taskId") or i + 1),
                    "text": t, "status": st})
    return out[:ITEM_CAP]


def _task_id(tin, resp):
    """A Task* event's item id: the input's own id when it has one (update),
    else the ordinal the tool_result reports (create — the id lives ONLY in
    that string), else None (the caller synthesizes)."""
    for k in ("taskId", "task_id", "id"):
        v = tin.get(k)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v).strip()
    text = resp if isinstance(resp, str) else \
        json.dumps(resp) if resp is not None else ""
    m = _CREATED_RE.search(text or "")
    return m.group(1) if m else None


def _apply_task(items, tool, tin, resp):
    """Task family -> the patched item list. Create appends; update patches
    the named row in place (unknown id = append, a mid-session join is not a
    reason to lose the row); a `deleted`/`cancelled` status removes it.

    Every row is COPIED, never aliased: the caller keeps the pre-event list to
    diff against, and a shallow `list(items)` let an in-place status patch
    mutate that snapshot too — so `_meaningful` compared the new state against
    itself and no Task* transition ever posted (caught dogfooding the fleet
    view: a codex seat that picked up a task stayed silent)."""
    items = [dict(i) for i in items]
    tid = _task_id(tin, resp)
    st = _status(tin) if (tin.get("status") or tin.get("state")) else None
    txt = _text(tin)
    if tid is None:
        tid = str(len(items) + 1)
    if st in GONE:
        return [it for it in items if it["id"] != tid]
    for it in items:
        if it["id"] == tid:
            if txt:
                it["text"] = txt
            if st:
                it["status"] = st
            return items
    if not (txt or st):
        return items
    items.append({"id": tid, "text": txt, "status": st or "pending"})
    return items[-ITEM_CAP:]


# ── digest ──────────────────────────────────────────────────────────────────

def _scrub(s):
    """seats._scrub's reader-side label defense, local copy (importing seats
    here would cycle): strip C0/C1 controls, format chars (incl. bidi
    overrides), line/paragraph separators — a todo whose text carries
    \\x1b[2J must not reshape the terminal `helm chat seats` prints its
    task cell into. Capture's whitespace-collapse does NOT strip these
    (str.split() only splits on whitespace), so the read seam must."""
    return "".join(ch for ch in s if ch == "\t"
                   or unicodedata.category(ch) not in ("Cc", "Cf", "Zl", "Zp"))


def _clip(s, cap):
    """Byte-budget clip on a codepoint boundary (seats._clip's law, local
    copy — importing seats here would cycle)."""
    enc = s.encode("utf-8")
    if len(enc) <= cap:
        return s
    end = cap
    while end > 0 and (enc[end] & 0xC0) == 0x80:
        end -= 1
    return enc[:end].decode("utf-8", errors="ignore") + "…"


def _lbl(s, cap=80):
    """Launder a roster-borne KEY for a DISPLAY sink: the seat name and the
    project ride the fleet table's first columns AND the /api/todos JSON, and
    the seat key is the unvalidated HELM_CHAT_NAME join seam — a hostile one
    (\\x1b[2J screen-clear, bidi override) must not reshape the operator's
    terminal or the panel. Mirrors _scrub's law for the todo TEXT, extended to
    the key/project (the two roster-borne strings _row emits raw before this).
    None/empty pass through untouched."""
    return _clip(_scrub(str(s)).strip(), cap) if s else s


def digest(items):
    """The pull surface's one-line summary of a list:
    {"active","done","total","fp"}. `fp` is the MATERIAL fingerprint — the
    in-progress line plus the done/total counts. Reordering, re-wording a
    pending item, or any other churn leaves it unchanged, which is exactly
    what makes 'nothing materially changed' cheap to detect.

    Rows that are not well-formed dicts are DROPPED, not trusted: this reads
    a file a hand-edit or a truncated write can reach, and every consumer of
    the digest (CLI, roster, web) has to survive it. `active` leaves here
    SCRUBBED for the same reason — every surface (CLI task cell, fleet
    table, web panel) renders it as a one-line label."""
    items = [i for i in (items or []) if isinstance(i, dict)] \
        if isinstance(items, list) else []
    active = next((_scrub(str(i.get("text") or "")) for i in items
                   if i.get("status") == ACTIVE), None)
    done = sum(1 for i in items if i.get("status") == DONE)
    return {"active": active, "done": done, "total": len(items),
            "fp": "%s|%d|%d" % (active or "", done, len(items))}


def _meaningful(old, new):
    """Is this a transition a TEAMMATE would act on? Two only: a task got
    FINISHED, or the seat picked up a DIFFERENT in-progress task (idle ->
    working included). Everything else — a new pending item, a re-word, a
    reorder, a list that merely grew — is the seat's own bookkeeping."""
    if new["fp"] == old["fp"]:
        return False
    if new["done"] > old["done"]:
        return True
    return bool(new["active"]) and new["active"] != old["active"]


# ── capture (the recorder's hot path) ───────────────────────────────────────

def state_path(sid):
    from . import record
    return os.path.join(record.session_dir(sid), STATE)


def state(sid):
    """One session's mirrored todo state, {} when absent — the read seam
    every consumer (CLI, roster, web) uses instead of hardcoding the path.

    A file that parses to something OTHER than an object answers {}: a
    hand-edit or a truncated race must degrade to 'no mirror yet', never to
    an AttributeError. Without the coercion a `todos.json` holding a bare
    list killed the session's mirror PERMANENTLY (capture raised before its
    own write, so the bad file could never be replaced) and crashed
    `helm todos --all` for the whole fleet."""
    st = pk.read_json(state_path(sid), {})
    return st if isinstance(st, dict) else {}


def capture(event, sid, tool, tin, resp):
    """Mirror one todo/task tool event. Returns the new state dict, or None
    when the event carried nothing usable (prior state untouched). FAIL-CLOSED
    is the CALLER's contract too — record() swallows everything.

    One session can emit parallel tool events. Atomic replace prevents a torn
    file but does NOT serialize read/modify/write: without this per-session
    flock, every writer could read the same pre-post ledger and each append a
    chat row. Hold the lock through the rate-cap reservation + append; unrelated
    sessions remain fully parallel, and process death releases the lock."""
    path = state_path(sid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a+") as lock:
        import fcntl
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        prior = state(sid)
        old = [i for i in (prior.get("items") or []) if isinstance(i, dict)]
        if tool == TODO_TOOL or "todos" in tin:
            items = _items(tin.get("todos"))
            if items is None:
                return None
        else:
            items = _apply_task(old, tool, tin, resp)
        st = {"v": 1, "ts": int(time.time()), "src": tool, "items": items}
        if prior.get("post"):
            st["post"] = prior["post"]
        pk.write_json(path, st)
        _maybe_post(sid, st, digest(old), digest(items))
        return st


def post_enabled():
    return home.env("TODO_POST") != "0"


def _maybe_post(sid, st, old, new):
    """The rate-capped, meaningful-transition-only room line. Never an
    @mention, never a DM, and never a WAKE: the row rides `ambient` so
    seats.deliverable() drops it before every wake rule. Stripping '@' alone
    was not enough — the home-room rule hands EVERY plain row in a team
    channel to every seat homed there, so on a `helm launch --room team-x`
    fleet a todo transition woke the whole team (found adversarially; that
    is precisely the beacon noise class the owner had removed).

    Fail-closed: a chat rail that is down, slow, or absent leaves the MIRROR
    intact and the tool call untouched."""
    try:
        if not (post_enabled() and _meaningful(old, new)):
            return
        last = st.get("post") or {}
        now = time.time()
        if new["fp"] == last.get("fp"):
            return                       # collapsed: already told them this
        if now - float(last.get("ts") or 0) < MIN_POST_S:
            return                       # rate cap: at most one per window
        from . import chat, seats
        seat = seats.seat_for_session(sid)
        if not seat:
            return          # an unseated session has no one to speak as
        line = "todo · %s (%d/%d done)" % (
            ("now: " + new["active"]) if new["active"]
            else "no task in progress", new["done"], new["total"])
        # '@' stripped: a todo that merely CONTAINS "@someone" must not
        # become a mention (mentions pierce mute and WAKE seats).
        line = line.replace("@", "")
        st["post"] = {"fp": new["fp"], "ts": int(now)}
        pk.write_json(state_path(sid), st)
        chat.post(line, who=seat,   # room derives (env seam then cwd project)
                  sign=False,            # sign=False: no node probe on a hook
                  ambient=True)          # ambient=True: renders, wakes nobody
    except Exception:
        pass


# ── pull (the fleet-coordination read path) ─────────────────────────────────

def _age(secs):
    secs = max(0, int(secs))
    if secs < 120:
        return "%ds" % secs
    if secs < 7200:
        return "%dm" % (secs // 60)
    if secs < 172800:
        return "%dh" % (secs // 3600)
    return "%dd" % (secs // 86400)


def _ts(st):
    """A mirror file's timestamp as an int, 0 when it is missing or not a
    number — every ordering/age read goes through here so a hand-written
    "yesterday" cannot ValueError its way out of a read-only surface."""
    try:
        return int(float((st or {}).get("ts") or 0))
    except (TypeError, ValueError):
        return 0


def for_seat(row):
    """A roster row -> its freshest mirrored todo state ({} when none). A
    seat's co-named sessions all mirror separately; the newest wins."""
    sids = list(row.get("sessions") or [])
    if row.get("session") and row["session"] not in sids:
        sids.append(row["session"])
    best = {}
    for sid in sids:
        st = state(sid)
        if st and _ts(st) >= _ts(best):
            best = st
    return best


def seat_digest(row):
    """The roster surface's compact todo cell, or None when the seat has
    mirrored nothing. Cheap by construction (one small JSON per session)."""
    st = for_seat(row)
    if not st:
        return None
    d = digest(st.get("items"))
    return {"active": d["active"], "done": d["done"], "total": d["total"],
            "ts": _ts(st)}


ORPHAN_CAP = 25         # unclaimed sessions shown, freshest first


def _row(name, project, st, items):
    d = digest(st.get("items"))
    # the seat KEY and project are roster-borne DISPLAY strings — laundered
    # at this emit boundary so neither the fleet table nor /api/todos JSON
    # carries raw ESC/bidi (digest already scrubs the todo TEXT; this is the
    # same law extended to the key/project). The raw key still drove the
    # roster read upstream; only the emitted copy is laundered.
    r = {"seat": _lbl(name), "project": _lbl(project), "active": d["active"],
         "done": d["done"], "total": d["total"], "ts": _ts(st)}
    if items:
        # `helm todos --all --json` ships the FULL item list — a display sink
        # like every other. Launder EVERY string field of each item (the `text`
        # line renders in the panel/CLI) so a hostile todo cannot ride the JSON
        # wire raw. Field-agnostic: a new string field is laundered the moment
        # it appears, not a hand-maintained per-key list.
        r["items"] = [{k: (_lbl(v, 200) if isinstance(v, str) and v else v)
                       for k, v in i.items()}
                      for i in (st.get("items") or []) if isinstance(i, dict)]
    return r


def fleet(items=False):
    """{"seats":[…], "orphans":[…]} — every roster seat with its current
    task, plus mirrored sessions no seat claims (a session that never
    joined a room still did work worth seeing). Read-only, fail-open.

    `items` is OFF by default: the two live consumers (the fleet table and
    the web panel) render only seat/active/done/total/ts, and shipping every
    seat's full list made the 3-second `/api/todos` poll carry hundreds of
    kilobytes on a busy estate. `helm todos --all --json` asks for them.

    Orphans are the UNBOUNDED half — gc prunes reflex-state at 30 days, so a
    busy fleet leaves hundreds of stale session dirs behind — and are cut to
    the ORPHAN_CAP freshest, with the remainder reported as a count. A
    thousand rows of dead sessions is the attention tax this bridge exists
    to avoid, not a fleet view."""
    from . import record, seats
    rows, claimed = [], set()
    try:
        roster = seats.roster()
    except Exception:
        roster = {}
    for seat, row in sorted(roster.items()):
        if not isinstance(row, dict):
            continue
        for s in (row.get("sessions") or []) + [row.get("session")]:
            if s:
                claimed.add(record.session_key(s))
        rows.append(_row(seat, row.get("project"), for_seat(row), items))
    orphans = []
    try:
        names = os.listdir(record.state_root())
    except OSError:
        names = []
    for n in sorted(names):
        if n in claimed:
            continue
        st = pk.read_json(os.path.join(record.state_root(), n, STATE), {})
        st = st if isinstance(st, dict) else {}
        if not st.get("items"):
            continue
        orphans.append(_row("(session " + n[:8] + ")", None, st, items))
    orphans.sort(key=lambda r: -(r["ts"] or 0))
    hidden = max(0, len(orphans) - ORPHAN_CAP)
    return {"seats": rows, "orphans": orphans[:ORPHAN_CAP],
            "orphans_hidden": hidden, "now": int(time.time())}


def this_session():
    """The session id for a bare `helm todos` — the ambient harness env."""
    return home.session_id()


# ── CLI ─────────────────────────────────────────────────────────────────────

_USAGE = """usage: helm todos [--json]           this seat's mirrored todo list
       helm todos --all [--json]     every seat: who is working on what
       helm todos promote <id> [--owner S]
                                     file one personal row into the team ledger
       helm todos demote [--dry-run] [--owner S]
                                     drop personal rows the ledger says are
                                     finished or belong to another seat"""

_GLYPH = {ACTIVE: "▶", DONE: "✓", "pending": "·"}


def _print_all(rows, now, hidden=0):
    """The fleet table. Seats that have mirrored NOTHING collapse into one
    footer line — an estate is mostly idle seats, and a screen of '—' rows is
    exactly the attention tax this bridge exists to avoid. `hidden` counts the
    unclaimed sessions fleet() cut past ORPHAN_CAP, reported the same way."""
    live = [r for r in rows if r["total"]]
    quiet = len(rows) - len(live)
    if live:
        w = max(max(len(r["seat"]) for r in live), 4)
        print("  %-*s  %-40s %7s  %5s" % (w, "seat", "in-progress", "done", "age"))
        for r in live:
            print("  %-*s  %-40s %7s  %5s" % (
                w, r["seat"], (r["active"] or "—")[:40],
                "%d/%d" % (r["done"], r["total"]),
                _age(now - r["ts"]) if r.get("ts") else "—"))
    elif not quiet:
        print("  no seats yet — sessions join on their next start")
    if quiet:
        print("  (%d seat%s with no mirrored todos — they fill on the next "
              "TodoWrite/Task* call)" % (quiet, "s"[:quiet != 1]))
    if hidden:
        print("  (%d older unclaimed session%s hidden — the %d freshest show)"
              % (hidden, "s"[:hidden != 1], ORPHAN_CAP))


# ── THE LEDGER BRIDGE (task/327) ────────────────────────────────────────────
#
# The personal task store is the HARNESS's, not helm's: one JSON file per row
# under <claude home>/tasks/<session>/<id>.json, session-scoped, single-reader,
# and injected into the seat's context every time it is listed. helm's own
# todos.json above is a read-only WINDOW onto that world — it can show the
# rows and nothing can claim, comment, prioritise or close what it shows,
# which is the owner's exact phrase ("durably interact with") and the exact
# gap. These two functions are the only writers, and they are deliberately
# narrow.
#
# WHY THE STAMP AND NOT A TITLE MATCH. `promote` writes the ledger row id back
# INTO the personal row. Every later decision reads that stamp. Re-deriving
# the pairing by title would make the DESTRUCTIVE leg depend on string
# similarity between two independently-edited texts — a wrong match there
# deletes a seat's live working note, so an unstamped row is simply never
# touched. That is not a limitation to fix later; it is the safety property.
#
# WHY THIS IS SAFE TO DO AUTOMATICALLY (owner directive, task/327): 289 local
# rows measured on one seat, 96% pure duplicate, and 3 rows read "pending"
# locally while the ledger had them CLOSED — one of which re-dispatched work
# another seat had already finished hours earlier. The cost is not the tokens.
# It is that working the personal list re-does completed work.

PERSONAL = "tasks"
STAMP = "helm_row"          # the ledger row id, written into the personal row
TRASH = "promoted-trash"    # removed personal rows, kept verbatim
CLAIM_PREFIX = ".claiming-"  # a removal IN FLIGHT; recoverable, never a row
PENDING = "pending"          # a DRY RUN reached here; nothing was terminalized
SWEPT = "gone"               # the claim directory is provably no longer there.
                             # NOT `GONE`: that name is taken at line 63 by the
                             # terminal TASK STATUSES ("deleted", "cancelled"),
                             # and binding a string over that tuple turned
                             # `if st in GONE` from a membership test into a
                             # SUBSTRING test — which stayed syntactically
                             # valid, kept working for some inputs, and raised
                             # TypeError on None inside a capture path that
                             # swallows exceptions. Every claim test stayed
                             # green while the task mirror silently stopped
                             # being written.
UNRECORDED = "unrecorded"    # the claim is STILL THERE and nothing on disk
                             # says why — never report this as a residual
RESIDUAL = "residual"        # TERMINAL: bytes preserved in place because the
                             # claim could not be cleared without a handoff
                             # that can destroy them. Retained indefinitely,
                             # reported, and disposed of only by an operator —
                             # choosing when preserved user bytes may be
                             # destroyed is a human-facing policy, not an
                             # implementation default (a retention
                             # ruling, meld e:1786171017).
CLAIM_META = "claim.json"    # what the claim IS, so a crash is recoverable
# CLAIM_STALE_S was the age backstop for a lock that could not be TESTED. It
# is gone rather than orphaned: age never granted exclusion, and a constant
# whose only reader has been deleted is how the previous version of this
# decision rotted unnoticed.


def claude_home():
    """The harness config home this seat runs under. CLAUDE_CONFIG_DIR is the
    per-seat pin (helm/launch.py sets it); ~/.claude is the unpinned default."""
    return os.environ.get("CLAUDE_CONFIG_DIR") or \
        os.path.expanduser(os.path.join("~", ".claude"))


def personal_dir(sid):
    return os.path.join(claude_home(), PERSONAL, str(sid or ""))


def personal_rows(sid, collect_unreadable=None):
    """A DESCRIPTOR-OWNING WRAPPER around _personal_rows_at.

    Callers that hold no capability get one for the length of this call. A
    caller that already pinned the personal directory must use the `_at` form
    instead and pass it, so every phase of a sweep enumerates through the SAME
    inode (capability result A)."""
    d = personal_dir(sid)
    fd = _dir_fd(d)
    if fd is None:
        try:
            os.stat(d)
        except FileNotFoundError:
            return [], None          # an empty new session
        except OSError as e:
            return [], str(e)
        return [], "the task directory could not be opened safely"
    try:
        return _personal_rows_at(d, fd, collect_unreadable=collect_unreadable)
    finally:
        os.close(fd)


def _personal_rows_at(d, pdir_fd, collect_unreadable=None,
                      collect_ident=None):
    """([(path, row)], error) for one session's task files, id-sorted.

    ENUMERATED THROUGH A PINNED DIRECTORY. This used to listdir a PATHNAME and
    then open each member by joining that pathname again, so a rename of the
    session directory between the two — or between any two rows — moved the
    enumeration onto a different inode mid-sweep, and the sweep would then
    judge one directory's rows and act on another's (review item A).

    A missing directory is an empty new session; another directory-read error
    is CANNOT SEE and is returned to the caller. A file that will not parse is
    SKIPPED from the returned rows and never repaired. When
    `collect_unreadable` is provided its path is reported there: the bridge's
    whole job is to remove rows safely, and a row it cannot read is a row it
    cannot prove anything about."""
    out = []
    try:
        names = sorted(os.listdir(pdir_fd))
    except FileNotFoundError:
        return out, None
    except OSError as e:
        return out, str(e)
    for name in names:
        if not name.endswith(".json"):
            continue
        full = os.path.join(d, name)
        # OPENED THROUGH THE PINNED DIRECTORY, ONCE. pk.read_json re-resolves
        # the pathname, so the row that got read was not necessarily the
        # member this enumeration listed — and the (path, row) pair handed
        # back would then describe two different objects (result A). The
        # pathname is still returned because callers report it; what changed
        # is that it is no longer how the bytes were reached.
        rfd = _open_member(pdir_fd, name)
        row = ident = None
        if rfd is not None:
            try:
                row = _json_fd(rfd)
                ident = _member_id_of(rfd)
                if collect_ident is not None:
                    # THE IDENTITY OF THE OBJECT WE ACTUALLY READ, captured
                    # here because this is the only moment it is held. The
                    # report door downstream can then say "this path names the
                    # row I enumerated" rather than merely "this path is in
                    # the right directory" — parent-only passes a same-parent
                    # member replacement, which is the fourth time that exact
                    # distinction has come up on this lane.
                    collect_ident[name] = ident
            finally:
                os.close(rfd)
        if isinstance(row, dict):
            out.append((full, row))
        elif collect_unreadable is not None:
            # A FILE THAT WILL NOT PARSE IS A THIRD DECLINED-TO-UNDERSTAND
            # CAUSE, and the two stamp buckets do not cover it.
            # Skipping is still RIGHT — a row helm cannot read is a row it
            # cannot prove anything about — but skipping SILENTLY meant a
            # directory of nothing but broken JSON reported "0 row(s) still
            # yours and open", which reads as a clean list. The caller opts in
            # by passing a list; every other caller keeps the old
            # signature.
            #
            # NAMED `collect_unreadable`, NOT `unreadable`, AND THE DIFFERENCE
            # IS REAL: test_orcaadopt's consumer census enumerates every helm
            # function taking an `unreadable` PARAMETER, because that is the
            # process census "arriving pre-taken" and a consumer that drops it
            # asks 'is there a READABLE rival?' while answering 'is there a
            # rival?'. This parameter runs the OTHER WAY — it arrives EMPTY and
            # this function fills it. Borrowing the consumer's noun for a
            # producer's out-param would make a reader expect semantics this
            # has never had, and would have put a file census into a rung that
            # exists for the process one.
            # THE IDENTITY EXISTS EVEN THOUGH THE PARSE FAILED: we opened
            # the member, we just could not read a row out of it. Reporting
            # "location unknown" here would take the ONE fact an operator
            # needs — which file does not parse — away from the one report
            # whose entire job is naming it.
            collect_unreadable.append(_row_report(full, pdir_fd, ident))
    return out, None


def _write_personal(path, row):
    """Rewrite one personal row in place. Returns True on success — a failure
    is never fatal here (see the fail-open law at the top of this module)."""
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(row, fh, ensure_ascii=False, indent=1)
        return True
    except OSError:
        return False


def promote(sid, item_id, owner, path=None):
    """One personal row -> a ledger row, with the id stamped back. (row, err).

    ALREADY-PROMOTED IS NOT AN ERROR AND NOT A SECOND ROW: a re-run returns
    the existing ledger row. Promotion is the kind of thing a seat will
    retry after a crash, and a bridge that filed a duplicate on retry would
    reproduce the 96%-duplicate disease it exists to cure, in the durable
    store rather than the disposable one."""
    from . import tasks
    # STRICT: a corrupt complete row must poison this read. The
    # tolerant default folds it as known-empty, and known-empty is exactly
    # the answer that files a duplicate — the refusal below never fired on
    # real corrupt bytes until the read was made to confess.
    known, unavailable = tasks.snapshot(path, strict=True)
    if unavailable:
        # CANNOT SEE is not "no twin" — filing on an unreadable ledger is how
        # a duplicate gets created with no way to notice.
        return None, "task ledger unreadable (%s) — refusing to file" % unavailable
    if not str(owner or "").strip():
        return None, "promote needs an owner — an unowned ledger row is a " \
                     "list, not a ledger (tasks.add refuses it too)"
    unreadable = []
    rows, unavailable = personal_rows(sid, collect_unreadable=unreadable)
    if unavailable:
        return None, "personal task directory unreadable (%s) — refusing to " \
                     "file" % unavailable
    for full, row in rows:
        if str(row.get("id") or "") != str(item_id):
            continue
        stamped = str(row.get(STAMP) or "")
        if stamped:
            got = tasks.get(stamped, path=path)
            if got:
                return got, None
            # A stamp whose row is GONE is not a licence to file a second
            # one silently — say so and let the caller decide.
            return None, "personal row %s is stamped %s, which the ledger " \
                         "does not hold" % (item_id, stamped)
        title = str(row.get("subject") or row.get("text") or "").strip()
        if not title:
            return None, "personal row %s has no subject to file" % item_id
        note = str(row.get("description") or "").strip() or None
        # DURABLE IDENTITY ON THE LEDGER SIDE TOO. The first cut
        # filed the row and THEN stamped the personal file, so a crash or a
        # concurrent run between those two writes left a ledger row nobody
        # could find again — and the retry, seeing no stamp, filed a SECOND
        # one. A bridge whose retry duplicates rows in the durable store is
        # the disease it was built to cure, moved somewhere worse.
        #
        # The back-reference makes promote idempotent from EITHER side: the
        # ledger row cites the personal item, so a retry finds it by that
        # citation even when the stamp never landed. It is an exact token,
        # never a title match — the same rule the destructive leg follows.
        backref = "local:%s/%s" % (sid, item_id)
        for existing in known.values():
            if backref in (existing.get("refs") or []):
                if not stamped:
                    row[STAMP] = str(existing.get("id") or "")
                    _write_personal(full, row)
                return existing, None
        # ORIGIN IS DELIBERATELY UNSET. A promoted todo is filed BY an agent,
        # but the field answers "did the OWNER ask for this", and a personal
        # todo is exactly as likely to be the owner's request written down as
        # it is to be the agent's own idea. The bridge cannot witness which,
        # and inventing provenance is the failure the field exists to end.
        new, err = tasks.add(title, owner, note=note, refs=[backref],
                              path=path)
        if err:
            return None, err
        row[STAMP] = str(new.get("id") or "")
        if not _write_personal(full, row):
            # The LEDGER row exists and the mapping does not. Say it: a
            # silent success here is how the next demote pass sees an
            # unstamped duplicate and leaves it forever.
            return new, "filed %s but could NOT stamp it back onto %s — the " \
                        "mapping is lost, re-stamp by hand" % (new["id"], full)
        return new, None
    # COMPARED BY MEMBER NAME, not by pathname. `file` is stripped whenever
    # the sweep could not verify it, so a pathname comparison would silently
    # stop matching exactly when the directory had been renamed — and this
    # check exists to refuse treating CORRUPTION AS ABSENCE, which is the
    # worst moment to start missing (result A, gap 3).
    target = "%s.json" % item_id
    if any(r.get("name") == target for r in unreadable):
        return None, "personal row %s exists but is unreadable — refusing to " \
                     "treat corruption as absence" % item_id
    return None, "no personal row %s in session %s" % (item_id, sid)


def _terminal(row):
    return str(row.get("status") or "") == "closed"


def demote(sid, seat, path=None, apply=True):
    """Remove personal rows whose LEDGER twin is finished or someone else's.

    Returns a report dict; `apply=False` reports without touching anything.
    The personal list is an ACTIVE WORKING SET, so the two removal reasons
    are "the shared ledger says this is done" and "the shared ledger says
    this is not yours" — both facts the local copy cannot know on its own,
    which is why it goes stale and re-dispatches finished work.

    UNSTAMPED ROWS ARE NEVER TOUCHED (see the module note above), and neither
    is a row whose stamp does not resolve: an unreadable ledger must mean
    "cannot tell", never "not mine"."""
    from . import tasks
    # `removed` holds ONLY rows whose file is actually gone; a cancelled
    # removal lands in `failed`. `unparseable` and `orphaned` are the two
    # halves of what used to be one `unresolved` count — see below for why
    # they are different facts with different owners. Both carry row identity,
    # because a sweep that runs unattended every turn must let its operator
    # audit what it DECLINED TO UNDERSTAND, not only what it deleted.
    rep = {"removed": [], "would_remove": [], "failed": [], "kept": 0,
           "unstamped": 0, "unparseable": [], "orphaned": [], "unreadable": [],
           "trash": None, "unavailable": None, "personal_unavailable": None}
    # ONE ledger read for the whole sweep, and it REFUSES on an unreadable
    # one. Asking per-row would make every row answer "no twin" — a state
    # that is indistinguishable from an orphaned stamp and would report 200
    # orphans over a ledger that was simply unavailable for a moment. Nothing
    # is removed either way; the difference is whether the operator is told
    # the truth about why.
    # STRICT for the same reason: the tolerant read SKIPS a corrupt
    # complete row, so a corrupt ledger arrived here as ({}, None) — the
    # refusal below never fired, and every stamped row misfiled as ORPHANED.
    # Could-not-read narrated as ledger-moved-on sends the operator to clean
    # up rows when the fix is to restore a ledger.
    known, unavailable = tasks.snapshot(path, strict=True)
    if unavailable:
        rep["unavailable"] = str(unavailable)
        return rep
    # RECOVERY RUNS AFTER THE LEDGER REFUSAL AND BEFORE ENUMERATION, and the
    # ORDER OF THOSE TWO IS THE POINT (a semantic conflict the one
    # textual conflict hid during the rebase). Trunk's invariant is that a
    # corrupt or unreadable ledger REFUSES BEFORE ANY DESTRUCTIVE WORK — "no
    # row was judged" — and recovery mutates: it restores rows, publishes
    # undos and terminalises claims. Running it above the refusal meant a
    # sweep that reported touching nothing had already changed the disk.
    #
    # It still precedes ENUMERATION, which is what the original ordering was
    # for: a row restored from a crashed claim is seen by THIS sweep rather
    # than the next one. Recovery remains the ONLY consumer of `.claiming-`
    # directories — without it a crash between the claim and the undo leaves a
    # row on disk and invisible to every surface.
    # ONE PERSONAL-DIRECTORY CAPABILITY FOR THE WHOLE SWEEP
    # (capability result A). Recovery, enumeration and every removal each
    # resolved `personal_dir(sid)` for themselves, so a rename of the session
    # directory between any two of them moved a LATER phase onto a different
    # inode: recovery could restore into the old directory while the
    # enumeration read a replacement, and the removal then refused rows it had
    # just been handed. Three resolutions of one name is three chances for the
    # name to mean something else mid-sweep.
    #
    # Opened once here, owned by one finally, and passed down. The phases can
    # still disagree about the WORLD; they can no longer disagree about which
    # directory they are in.
    _pdir = personal_dir(sid)
    pdir_fd = _dir_fd(_pdir)
    if pdir_fd is None:
        try:
            os.stat(_pdir)
        except FileNotFoundError:
            pdir_fd = None           # an empty new session: nothing to sweep
        except OSError as e:
            rep["personal_unavailable"] = str(e)
            return rep
        else:
            rep["personal_unavailable"] = ("the task directory could not be "
                                           "opened safely")
            return rep
    # THE PREFLIGHT BELONGS BEFORE ANY MUTATION, ON EVERY PATH IN.
    # Only the public recover_claims ran it, and demote now calls the `_at`
    # form directly — so a missing member primitive let RECOVERY mutate and
    # then _trash_at refuse afterwards, which is the "already changed the disk
    # before refusing" ordering this sweep was restructured to prevent. My own
    # comment calling _demote_at's callee a wrapper was false the moment
    # demote became the other caller.
    try:
        gap = _capability_gap()
        if gap:
            # INSIDE THE TRY, so the finally below is still the ONE owner of
            # this descriptor. My first version closed it here and returned,
            # which is a second close path — the exact shape this lane keeps
            # removing, written by me while removing it.
            rep["personal_unavailable"] = gap
            rep["recovered"] = []
            return rep
        return _demote_at(sid, seat, known, rep, apply, _pdir, pdir_fd)
    finally:
        if pdir_fd is not None:
            os.close(pdir_fd)


def _demote_at(sid, seat, known, rep, apply, _pdir, pdir_fd):
    """The sweep, with the personal directory already pinned."""
    from . import tasks
    if pdir_fd is None:
        # THE SHAPE THE CALLER WAS PROMISED. Returning early without this left
        # `recovered` unset, which every previous version of demote had
        # established as a list — a consumer reading rep["recovered"] got a
        # KeyError on an empty session.
        rep["recovered"] = []
        return rep
    rep["recovered"] = _recover_claims_at(sid, apply, _pdir, pdir_fd)
    idents = {}
    rows, unavailable = _personal_rows_at(
        _pdir, pdir_fd, collect_unreadable=rep["unreadable"],
        collect_ident=idents)
    if unavailable:
        rep["personal_unavailable"] = str(unavailable)
        return rep
    for full, row in rows:
        stamped = str(row.get(STAMP) or "")
        if not stamped:
            rep["unstamped"] += 1
            continue
        # UNPARSEABLE IS NOT ABSENT, AND MY `or ""` COLLAPSED THEM
        # (mutation-proved: relax the accident this
        # depended on and a VALID ROW IS DELETED). normalize_id returns None
        # to mean "this string is not an id at all" — a different fact from
        # "no such row" — and coercing it to "" handed a falsy key to
        # known.get(). That happened to be safe ONLY because eventledger
        # rejects falsy ids two modules away: an accident, untested, and one
        # refactor from silently deleting rows in a store helm does not own.
        #
        # This is also the reported "malformed replay can delete valid rows",
        # which I traced and could NOT construct. I was looking for a path
        # through my own code; the safety was living somewhere else.
        tid = tasks.normalize_id(stamped)
        if tid is None:
            # cannot even read the stamp — the strongest possible "cannot
            # tell", and cannot-tell has always meant KEEP on this leg.
            #
            # ITS OWN BUCKET, because it is its own FACT.
            # An UNPARSEABLE stamp means a WRITER BUG in a store helm does not
            # own; an ORPHANED one means the LEDGER MOVED ON. Different owners,
            # different fixes, and they shared a counter — so demote honoured
            # "UNPARSEABLE is not ABSENT" in the DECISION and collapsed it in
            # the REPORT, which is the half an operator reads.
            rep["unparseable"].append(
                _row_report(full, pdir_fd, idents.get(os.path.basename(full)),
                            id=row.get("id"), stamp=stamped))
            continue
        twin = known.get(tid)
        if not twin:
            rep["orphaned"].append(
                _row_report(full, pdir_fd, idents.get(os.path.basename(full)),
                            id=row.get("id"), row=tid))
            continue
        owner = str(twin.get("owner") or "")
        why = None
        if _terminal(twin):
            why = "ledger row %s is closed" % twin["id"]
        elif owner and seat and owner.casefold() != seat.casefold():
            why = "ledger row %s is owned by %s" % (twin["id"], owner)
        if why is None:
            rep["kept"] += 1
            continue
        # ONE ENTRY CANNOT SERVE THREE BRANCHES, because they make three
        # different claims about the disk and are true at three different
        # MOMENTS. This was built once, before _trash_at ran, with
        # gone_ok=True — so a dry run inherited "the member may be absent"
        # when it must still be present, and a rename DURING the removal left
        # both the failure and the success reporting a path verified before
        # any of it happened. Each branch builds its own, now, against the
        # disk as it stands when that branch is taken.
        ident = idents.get(os.path.basename(full))

        def _entry(gone_ok):
            return _row_report(full, pdir_fd, ident, gone_ok=gone_ok,
                               id=row.get("id"), row=twin["id"], why=why)

        if not apply:
            # A DRY RUN REMOVES NOTHING, SO `removed` MUST BE EMPTY (T1
            # — and it falsifies the contract I claimed one commit ago).
            # That commit said `removed` means removed BY CONSTRUCTION; it was
            # true only on the apply path. A dry run appended anyway, so
            # `--dry-run --json` published removed:[row] about a file still on
            # disk and the summary said "1 dropped" under a per-row line
            # reading "would drop". Naming a contract does not establish it —
            # the sentence has to be true on EVERY path the function has.
            # A DRY RUN TOUCHED NOTHING, so its member must still be
            # exactly there: gone_ok would accept an absence it cannot have
            # caused.
            rep["would_remove"].append(_entry(False))
            continue
        if apply:
            err = _trash_at(sid, full, row, rep, pdir_fd, ident)
            if err:
                # THE REPORT MUST NOT SAY A THING THE FILESYSTEM DOES NOT.
                # Appending to `removed` FIRST and letting _trash stamp a `failed`
                # key onto the row it had just added would, on the one path built
                # never to lose a row, keep the row in `removed` while its FILE
                # STILL EXISTS. _trash's own docstring says a failure CANCELS the
                # removal: true of the disk, false of the report. Any consumer
                # doing len(rep["removed"]) over-reported deletions. Now
                # `removed` means removed, by construction rather than by the
                # caller remembering to filter.
                # A CANCELLED REMOVAL LEFT THE ROW WHERE IT WAS, so its
                # member must still be exactly there — built NOW, after
                # _trash_at ran, because a rename during the removal would
                # otherwise be reported through a path verified before it.
                failed = _entry(False)
                failed["failed"] = err
                rep["failed"].append(failed)
                continue
        # THE SUCCESS CASE IS THE ONE WHERE THE MEMBER IS INTENTIONALLY GONE,
        # so it is the only branch that may say so — and its parent binding is
        # taken now, against the directory as it stands after the removal.
        rep["removed"].append(_entry(True))
    return rep


class _PutBackDone(Exception):
    """The restore succeeded; skip the remaining strategies."""


def _trash(sid, full, row, rep, judged=None):
    """A DESCRIPTOR-OWNING WRAPPER around _trash_at, for callers holding none.

    A sweep that already pinned the personal directory must use _trash_at and
    pass it, so the removal acts through the SAME inode the enumeration read
    (capability result A)."""
    fd = _dir_fd(os.path.dirname(full) or ".")
    if fd is None:
        return ("the row's own directory could not be opened safely, so "
                "nothing was removed")
    try:
        if judged is None:
            # THE WRAPPER ACQUIRES WHAT THE TRANSACTION REQUIRES, exactly as
            # it already acquires the directory. A caller with no identity in
            # hand is not a reason to skip the check; it is a reason to
            # establish one here, through the descriptor we just opened.
            try:
                st = os.stat(os.path.basename(full), dir_fd=fd,
                             follow_symlinks=False)
                judged = (st.st_dev, st.st_ino)
            except OSError as e:
                return ("the row could not be identified before the claim "
                        "(%s), so nothing was removed" % e)
        return _trash_at(sid, full, row, rep, fd, judged)
    finally:
        os.close(fd)


def _trash_at(sid, full, row, rep, pdir_fd, judged=None):
    """Keep the bytes before removing the file. A destructive sweep that runs
    unattended every turn needs an undo that does not depend on the harness
    having a backup, and `promoted-trash` is one flat directory a human can
    read. Any failure here CANCELS the removal — losing a row is the outcome
    this whole leg is built to avoid.

    -> None on success, the error STRING on failure. It used to reach back
    into `rep["removed"][-1]` and stamp a key onto the caller's last append,
    which is what let a cancelled removal stay counted as a removal. Returning
    the outcome lets the caller decide which bucket the row belongs in, and a
    caller that ignores the return value can no longer mis-file it silently."""
    from . import record
    # PREFLIGHT BEFORE ANY MUTATION (seam 4). Nothing has been
    # touched at this point, so a refusal here leaves the row exactly where it
    # is — which is already this function's contract: a string means the
    # removal was CANCELLED.
    gap = _capability_gap()
    if gap:
        return "nothing was removed: %s" % gap
    # THE CLAIM AND THE UNDO ARE ON DIFFERENT FILESYSTEMS IN THE GENERAL CASE:
    # `personal_dir` is rooted at `claude_home()`, the undo at
    # `record.state_root()`. So the CLAIM is taken beside the row, where
    # rename() is atomic, and the UNDO is reached afterwards by a COPY.
    #
    # THE CLAIM IS MINTED BEFORE THE UNDO, BECAUSE THE UNDO DESCENDS FROM IT
    # (census, meld e:1786171017). The undo was named
    # <trash>/<basename(row)> — a name chosen before the claim existed and
    # REUSABLE FOREVER — published with replacing primitives. Measured
    # 2026-08-08, two opposite harms from that one root: same-filesystem, a
    # second removal of the same row name DESTROYED the first transaction's
    # undo, which was the last copy because the first removal had already
    # succeeded; cross-filesystem, it silently RESURRECTED a row whose removal
    # had succeeded, because the overwritten undo stopped matching the digest
    # the claim recorded and recovery fell through to its restore branch. Both
    # transactions reported success in both directions.
    #
    # BOTH DIRECTORIES ARE MINTED, AND THAT IS THE POINT. `mkdtemp` makes a
    # name unique among what EXISTS IN THAT DIRECTORY RIGHT NOW — not unique
    # across time. Nothing sweeps the trash root, so a claim id can be minted,
    # cleared from beside the row, and minted again later while its OLD undo
    # directory is still sitting there. Deriving the undo from the claim id
    # alone would re-collide on exactly that reuse. So the undo directory is
    # created EXCLUSIVELY here: the pair is jointly unique by construction, and
    # a reused id becomes a retry rather than a silent overwrite.
    # THE WHOLE-OBJECT PREFLIGHT, BEFORE ANY MUTATION (the shape
    # behind findings 5 and 6). Every capability this transaction will use is
    # established here or the transaction REFUSES having touched nothing.
    # Checking them one at a time as each is first needed is what left an
    # unsupported host with a claim directory, an undo directory and a
    # manifest while reporting that nothing was removed.
    gap = _capability_gap()
    if gap:
        return ("the row was NOT removed: %s" % gap)
    # AND THE SUBSTRATE IS EXERCISED, NOT ASSUMED (item 8). The
    # census proves libc exports the symbol; it says nothing about whether
    # this kernel implements it or this filesystem accepts the flag. A host
    # answering ENOSYS, or a filesystem answering EINVAL to NOREPLACE, passed
    # the old preflight and was discovered by the FIRST REAL CALL — after the
    # claim directory, the undo directory and the manifest already existed.
    # The probe runs on the row's own directory, which is the substrate the
    # move will actually use.
    unsupported = fsops.supports_noreplace(pdir_fd)
    if unsupported:
        return ("the row was NOT removed: %s" % unsupported)
    trash_root = os.path.join(record.session_dir(sid), TRASH)
    qdir = dest = None
    for _attempt in range(8):
        try:
            # MINTED THROUGH THE PINNED DIRECTORY, NOT BY PATHNAME
            # (capability result B). mkdtemp(dir=<pathname>) creates inside
            # whatever holds that name AT THAT MOMENT, so a swap between the
            # enumeration and the removal put the claim — and therefore the
            # row — into the REPLACEMENT directory. This lane's own decoy arm
            # caught it: the row was read through the pin and then written by
            # name, which is the seam A closes on one side and B on the other.
            #
            # mkdirat is inherently exclusive: EEXIST is the answer when the
            # name is taken, so the uniqueness needs no separate check and no
            # window between checking and creating. The name is random rather
            # than sequential so concurrent minters do not converge on the
            # same next candidate.
            qname = CLAIM_PREFIX + os.urandom(6).hex()
            os.mkdir(qname, 0o700, dir_fd=pdir_fd)
            qdir = os.path.join(os.path.dirname(full), qname)
        except FileExistsError:
            continue                       # a taken name costs an attempt
        except OSError as e:
            return ("the row could not be claimed for removal, so nothing was "
                    "removed: %s" % e)
        try:
            dest = os.path.join(trash_root, os.path.basename(qdir))
            # EVERY ENTRY THIS CREATES, not just the leaf's parent. On a
            # first removal this is a six-level chain; afterwards it syncs
            # nothing but trash_root. FileExistsError on the leaf still means
            # the claim id is in use and we retry — that is the loop's job.
            _makedirs_durable(dest)
            break
        except FileExistsError:
            _discard_fresh_claim(qdir)
            qdir = dest = None
        except OSError as e:
            _discard_fresh_claim(qdir)
            return ("the undo directory could not be made (%s), so nothing "
                    "was removed" % e)
    if qdir is None:
        return ("the row could not be claimed for removal: eight claim ids in "
                "a row were already taken in the undo directory, so nothing "
                "was removed")
    claim_id = os.path.basename(qdir)
    keep = os.path.join(dest, os.path.basename(full))
    quarantine = os.path.join(qdir, os.path.basename(full))
    meta_path = os.path.join(qdir, CLAIM_META)

    # THREE DESCRIPTORS, OPENED ONCE, CLOSED BY ONE OWNER (seam 4).
    # Every member operation below addresses a bare NAME relative to one of
    # these, so a pathname is never re-resolved after the decision that used
    # it: the claim and undo directories were minted by THIS call, so a
    # descriptor taken now refers to the inodes we created and cannot be
    # swapped underneath us. The row's own directory is not ours, which is
    # exactly why its entries are reached through a descriptor rather than by
    # walking the path again at each step.
    payload_fd = None                  # held open through the PUBLISH
    claim_lock = None                  # the claim's OWNER identity (item 2)
    # NOT REOPENED: the caller pinned the row's directory and owns it. This
    # resolved dirname(full) for itself, which is a THIRD resolution of the
    # same name in one sweep — the window A exists to close.
    # OPENED THROUGH THE PIN, BY THE NAME WE JUST MINTED (capability result B). A
    # path-based open here would re-resolve the whole chain and could land in
    # a replacement directory even though the mkdir went to the right one —
    # creating through a capability and then opening by name gives back
    # exactly the window the capability was taken to close.
    # O_DIRECTORY is optional HERE: _adopted_shape immediately lists the opened
    # descriptor and refuses a non-directory, so no directory claim depends on
    # the flag being present.
    qdir_fd = _open_member(pdir_fd, os.path.basename(qdir),
                           openflags.verified_directory(os.O_RDONLY))
    if qdir_fd is not None and _adopted_shape(qdir_fd) is not None:
        os.close(qdir_fd)
        qdir_fd = None
    dest_fd = _dir_fd(dest)
    if dest_fd is not None and _adopted_shape(dest_fd) is not None:
        # VALIDATED THE SAME WAY qdir IS (finding 3). dest was
        # minted, opened, and then trusted — the only capability in this
        # transaction with no check on it at all. Replace the fresh undo
        # directory with a POPULATED one that happens not to hold the row's
        # basename and the removal publishes the undo beside a stranger's
        # bytes, while the directory we actually made stays empty.
        #
        # "It cannot have contents, we just made it" is the same sentence that
        # was wrong about qdir. Emptiness is what makes adoption harmless, and
        # it is cheap to establish rather than assume.
        os.close(dest_fd)
        dest_fd = None
    if qdir_fd is not None:
        # THE LIVE PATH TAKES THE CLAIM'S OWNER LOCK TOO (review item 2).
        # It held only its own claim.json inode, so recovery could take the
        # directory and a replacement manifest and act on the same claim
        # concurrently. This directory was minted by this call, so the lock is
        # uncontended here — what it does is make any LATER owner, recovery
        # included, wait rather than proceed in parallel.
        claim_lock, _lock_why = _lock_claim_dir(qdir_fd)
        if claim_lock is None:
            os.close(qdir_fd)
            qdir_fd = None
    if qdir_fd is None or dest_fd is None:
        # PARTIAL ACQUISITION IS PAST THE ANCHOR (second immutable
        # refuter). This cleaned up by NAME — `_discard_fresh_claim(qdir)` and
        # `os.rmdir(dest)` — which contradicts the law `_abandon` states
        # twelve lines below: once ANY descriptor exists, the names no longer
        # identify the inodes we hold. Acquire qdir_fd, let a rename-and-reuse
        # put an empty replacement at that name, fail on dest_fd, and this
        # removed the STRANGER's directory. rmdir refuses a non-empty one, so
        # the kernel covers the loud case and leaves exactly the quiet one.
        #
        # So nothing is cleaned here. Both directories are empty and were
        # minted by this call, so recovery meets them, decides `empty`, and
        # disposes of them under a held lock — the same path that already
        # exists for a claim interrupted one instruction later. A leaked empty
        # directory is a tidiness cost; removing a stranger's is data loss.
        # PDIR IS BORROWED — the caller owns it. Only what this call minted
        # gets closed here.
        for _fd in (qdir_fd, dest_fd):
            if _fd is not None:
                os.close(_fd)
        return ("the claim's directories could not be opened safely, so "
                "nothing was removed; the empty claim at %s is left for "
                "recovery rather than removed by a name that may no longer "
                "be ours" % qdir)

    def _abandon():
        """Give back a claim that never took anything, BY NAME AND ONLY WHILE
        THAT IS STILL HONEST.

        Both directories, because both were minted: leaving the empty undo dir
        behind would burn its claim id forever, since the mint loop reads an
        existing undo dir as "this id is taken".

        BUT ONLY BEFORE THE ANCHOR. Once descriptors exist, the
        NAMES no longer identify the inodes we hold — a rename-and-reuse in
        between means these rmdirs would remove a replacement's directories
        while ours keep their contents at a path nothing reports. rmdir on a
        non-empty directory fails, so the kernel refuses the worst case for
        us; what it cannot refuse is a replacement that is also empty. After
        the anchor there is nothing to give back anyway — the claim has taken
        the row — so this simply must not run there, and every post-anchor
        caller uses the terminal state instead."""
        if pdir_fd is not None:
            return                     # anchored: names are no longer identity
        # NEITHER DIRECTORY IS DELETED, for the one reason that applies to
        # both: "empty by construction" is a statement about the moment we
        # made it, and the rmdir happens at a later one. Between them the name
        # can lead somewhere else, and the kernel's non-empty refusal only
        # covers the loud half — an empty replacement is removed silently.
        # Recovery disposes of what we abandon, under a lock.
        _discard_fresh_claim(qdir)

    # A CLAIM THAT CANNOT BE LOCKED IS NOT TAKEN. A
    # swallowed failed flock would leave a live claim that
    # recovery could not tell from an abandoned one — and recovery acting on a
    # live claim is the row-stealing case the lock exists to prevent. Refusing
    # here costs one un-swept row; proceeding unlocked costs a row.
    # THE OWNER'S TRY STARTS HERE, NOT AFTER THE MANIFEST OPENS. The three
    # directory descriptors are already open at this point, so any return
    # BEFORE this line leaked all three — measured at exactly 3 per fault
    # across 20 injected fdopen failures by the arm that went looking. A
    # capability's lifetime has to begin where the capability does.
    held = None
    try:
        try:
            # "w+" NOT "w": the terminal transition READS BACK through this
            # same descriptor, and a write-only handle cannot confirm what it
            # wrote. A confirmation that cannot read is not a confirmation.
            # O_EXCL, NOT O_TRUNC (architecture result C). A
            # freshly minted claim directory cannot already hold a manifest,
            # so O_EXCL makes that an ERROR rather than a silent overwrite —
            # and O_TRUNC on a name that unexpectedly exists destroys the only
            # record of whatever it was. O_APPEND makes every later write
            # land at the end no matter what any seek did.
            _mfd = _open_member(qdir_fd, CLAIM_META,
                                os.O_RDWR | os.O_CREAT | os.O_EXCL
                                | os.O_APPEND)
            if _mfd is None:
                raise OSError("the claim manifest could not be opened safely")
            if not _regular_fd(_mfd):
                os.close(_mfd)
                raise OSError("the claim manifest is not an ordinary file")
            try:
                # UNBUFFERED BINARY: the frame boundary is the durability
                # boundary, and a buffer would let a "written" frame sit in
                # user space across the fsync that is supposed to commit it.
                held = os.fdopen(_mfd, "rb+", buffering=0)
            except BaseException:
                # os.fdopen TAKES OWNERSHIP ONLY ON SUCCESS. Between the open
                # and the wrapper existing the raw descriptor has no owner at
                # all, so a fault there leaks it (reproduced).
                os.close(_mfd)
                raise
        except OSError as e:
            _abandon()
            return ("the claim could not be recorded, so nothing was "
                    "removed: %s" % e)
        try:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            _abandon()
            return ("the claim could not be locked, so nothing was removed: "
                    "%s" % e)

        def _write_meta(digest=None):
            """The claim's description, fsynced — file AND directory. Syncing
            the file alone leaves the DIRECTORY ENTRY unsynced, so a crash can
            lose the only record of what the claim held while the payload sits
            beside it."""
            # NO TRUNCATE. See _append_manifest: the previous complete
            # snapshot survives until a new complete one exists.
            # NO ABSOLUTE PATH IS AUTHORITY HERE (a review ruling, meld
            # e:1786171017). The previous manifest stored `original` and `undo`
            # as absolute paths and recovery used them directly — so a
            # hand-edited or corrupt claim.json pointed the sweep anywhere on
            # disk, and a moved HELM_HOME would have silently relocated an old
            # claim instead of surfacing it. A persisted root is stale
            # configuration wearing the costume of identity.
            #
            # What is bound instead is what cannot be derived: the sid, the
            # claim id, the row's own BASENAME, its logical identity, and the
            # payload digest. Recovery derives every root from the sid its
            # scanner already holds, and REFUSES anything that does not sit
            # inside that namespace. `row` and `payload` are the same string by
            # construction today; both are written so recovery can CHECK that
            # rather than assume it, and a future divergence is caught instead
            # of inherited.
            why = _append_manifest(
                held, {"claim_id": claim_id, "sid": str(sid),
                       "row": os.path.basename(full),
                       "payload": os.path.basename(full),
                       "identity": list(_identity(row) or ()),
                       "payload_sha256": digest or ""},
                qdir_fd, first=digest is None)
            if why is not None:
                raise OSError(why)

        try:
            _write_meta()
        except OSError as e:
            _abandon()
            return ("the claim could not be recorded, so nothing was removed: "
                    "%s" % e)

        # CLAIMING IS THE REMOVAL. After this rename nothing can substitute the
        # object under judgement, and there is no second unlink for a race.
        # NO-CLOBBER, VIA renameat2(RENAME_NOREPLACE) (capability result B). Both
        # ends are capabilities, so the only thing left that could destroy a
        # stranger's bytes is the rename ITSELF: os.rename is destructive BY
        # SPECIFICATION — POSIX says it replaces the destination atomically —
        # so if a claim directory we adopted already held a member at this
        # name, the move would silently overwrite it. A check-then-rename
        # cannot fix that; the check has to be inside the syscall.
        #
        # A kernel that cannot promise no-clobber is a REFUSAL, never a
        # fallback to plain rename: falling back would reintroduce exactly the
        # clobber this exists to prevent, on the systems least able to detect
        # it.
        # THE ROW MOVED MUST BE THE ROW JUDGED (finding 1). The
        # enumeration's identity was carried only into the REPORT, so a swap
        # after the judgement — same id, same stamp, different inode — was
        # moved and removed although nothing had ever decided about it. The
        # existing identity re-read compares the row's LOGICAL fields, which
        # is exactly what such a swap satisfies.
        #
        # Checked through the pinned parent, immediately before the move, and
        # a mismatch CANCELS: this leg's whole contract is that a row it did
        # not judge is a row it does not touch.
        # AN UNESTABLISHED FACT REFUSES; IT DOES NOT SKIP THE CHECK. My own
        # dogfood found this: I cured "the judged identity never reaches the
        # mutation" by threading it as `judged=None` behind
        # `if judged is not None:`, so the one case where nothing is known got
        # no check at all. Probe on the gated tip: force the identity capture
        # to None, swap the row for a different inode after the judgement, and
        # the stranger's row was REMOVED and reported as a success — the
        # original defect, verbatim, reached through its own cure.
        #
        # An optional ERROR may default to None, because absence means no
        # error. An optional FACT may not, because absence means we cannot
        # tell — and cannot-tell has meant KEEP on this leg since the first
        # round of this lane.
        if judged is None:
            _abandon()
            return ("the row this sweep judged could not be identified, so "
                    "nothing was removed — an unprovable identity keeps the "
                    "file")
        try:
            now = os.stat(os.path.basename(full), dir_fd=pdir_fd,
                          follow_symlinks=False)
        except OSError as e:
            _abandon()
            return ("the row could not be re-checked before the claim "
                    "(%s), so nothing was removed" % e)
        if (now.st_dev, now.st_ino) != tuple(judged):
            _abandon()
            return ("the file at that path is no longer the row this "
                    "sweep judged — something replaced it after the "
                    "decision, so nothing was removed")
        try:
            if not fsops.have_renameat2():
                _abandon()
                return ("this system cannot move a row without risking "
                        "overwriting something at the destination, so nothing "
                        "was removed")
            err = fsops.rename_noreplace(pdir_fd, os.path.basename(full),
                                         qdir_fd,
                                         os.path.basename(quarantine))
            if err is not None:
                raise err
        except FileExistsError:
            # something already occupies the claim's member name: we adopted a
            # directory that was not empty, or a racer got there first. Either
            # way the stranger keeps its bytes.
            _abandon()
            return ("the claim already holds a file at that name, so the row "
                    "was NOT moved and nothing of anyone else's was "
                    "overwritten")
        except FileNotFoundError:
            _abandon()
            return "GONE — something else removed this file before the sweep did"
        except OSError as e:
            _abandon()
            return ("the row could not be claimed for removal, so nothing was "
                    "removed: %s" % e)
        # A SWALLOWED SYNC UNDER DURABILITY PROSE IS THE PROSE LYING
        # Passing on OSError would contradict the comments
        # above, which promise the claim survives a crash. It cannot promise that
        # if the directory entry naming the claim was never synced, so the
        # failure is REPORTED and the row is put back rather than left in a
        # state the code claims is durable and is not.
        # ORDER: THE DESTINATION BEFORE THE SOURCE (cumulative
        # refute). The claim rename created an entry in qdir and removed one
        # from the row's directory. Syncing the SOURCE first can make the
        # removal durable while the entry naming where the bytes went is not —
        # a crash there loses the only pointer to a file that no longer exists
        # at its own name. Destination first is the only order in which a
        # crash is always recoverable.
        fsync_failed = None
        try:
            # THE PINNED INODES, NOT THEIR NAMES. A directory
            # swapped after the anchor makes the WRONG inode durable, which
            # is the same re-resolution as any other — durability included.
            os.fsync(qdir_fd)
            os.fsync(pdir_fd)
        except OSError as e:
            fsync_failed = e

        def _put_back(why):
            """Undo the claim, and say what happened when it cannot be undone.

            `os.link` REFUSES when the destination exists, which is the ATOMIC
            form of "restore only if nothing took our place"."""
            # RESTORE THE JUDGED OBJECT, NOT THE NAME ("_put_back
            # also links the mutable name"). Linking basename(quarantine) puts
            # back whatever that name points at NOW — measured: with the
            # payload swapped after the judgement, a cancelled removal
            # restored the ATTACKER's bytes into the operator's row list. The
            # publish binds the descriptor we judged.
            #
            # If that inode can no longer be linked — its only name was taken,
            # so nlink is 0 and /proc/self/fd reads as deleted — a copy of the
            # judged bytes is the ONLY way to return what was claimed. That is
            # the one place a copy is right rather than a shortcut: there is
            # no inode left to share.
            try:
                if payload_fd is None:
                    # NO DESCRIPTOR MEANS NOTHING WAS CAPTURED — NOT THE
                    # IDENTITY AND NOT THE BYTES (second immutable
                    # refuter). Two versions of this branch have now tried to
                    # be helpful and both were wrong. The first linked
                    # `quarantine` BY NAME, which puts back whatever occupies
                    # that name now — reproduced: with the payload swapped
                    # between the open attempt and this line, a refusal
                    # hard-linked the REPLACEMENT into the operator's row
                    # list. The second wrote `claimed`, which is assigned only
                    # AFTER a successful open, so an unopenable payload raised
                    # UnboundLocalError here — after the row had already left
                    # its normal path.
                    #
                    # There is no third clever thing to do. Nothing was read,
                    # so nothing can be restored or reproduced; the pinned
                    # claim is left exactly as it stands and the caller is
                    # told the location is unresolved. Refusing to act is the
                    # only honest move available, and it keeps the bytes.
                    return ("%s; the claimed object could not be opened, so "
                            "nothing was read and nothing was put back — the "
                            "claim is left intact and its location is "
                            "unresolved" % why)
                e = _link_fd(payload_fd, os.path.basename(full), pdir_fd)
                if e is None:
                    raise _PutBackDone
                if isinstance(e, FileExistsError):
                    raise e
                if claimed is None:
                    # NO CAPTURED REGULAR BYTES, SO NOTHING TO WRITE. Writing
                    # here would materialise an EMPTY regular row at the
                    # operator's path while reporting that nothing was
                    # removed — inventing data is worse than the loss it is
                    # pretending to cover.
                    return ("%s; the claimed object yielded no readable "
                            "contents, so nothing was written back and the "
                            "claim is left intact with its location "
                            "unresolved" % why)
                fd = os.open(os.path.basename(full),
                             os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600,
                             dir_fd=pdir_fd)
                with _wrap_fd(fd, "w+b") as out:
                    short = _write_all_verified(out, claimed)
                if short is not None:
                    return ("%s; and the copy put back at its path is not the "
                            "claimed bytes (%s), so it is not reported as "
                            "them" % (why, short))
                raise _PutBackDone
            except _PutBackDone:
                pass
            except FileExistsError:
                # THE DOUBLE RACE, AND IT WAS DATA LOSS WITH A LYING REPORT.
                # A writer holds the row's path AND the judged
                # payload's name was swapped away, so: the link fails, the
                # O_EXCL restore fails, and the old text said the claimed
                # bytes are "preserved at <quarantine>" — a name that now
                # holds the ATTACKER's file. The judged inode has no name left
                # and dies the moment payload_fd closes. The report was the
                # only record of where the bytes were, and it pointed at the
                # wrong ones.
                #
                # So when the judged inode has no name, MATERIALISE it
                # somewhere it can be found: the claim's own undo directory is
                # empty at this point (the publish never happened) and is
                # bound to this transaction by the manifest. Then report THAT
                # path, never quarantine.
                try:
                    orphaned = os.fstat(payload_fd).st_nlink == 0
                except OSError:
                    orphaned = True
                if not orphaned:
                    return ("%s; a writer has since installed a different "
                            "file at that path, so the bytes helm claimed are "
                            "preserved at %s rather than overwriting it"
                            % (why, _say_where(quarantine, qdir_fd,
                                               _member_id_of(payload_fd),
                                               claim_id)))
                if claimed is None:
                    # NOTHING WAS CAPTURED, so there is nothing to copy — and
                    # this branch would have called len(None). Same law as the
                    # fallback one level up: a nonregular member is never read,
                    # so no bytes exist to preserve and inventing an empty
                    # file here would be the fabrication that law exists to
                    # prevent. (Found by this leg's own double-race arm once
                    # BOTH halves of the composition were supplied — with only
                    # the writer-holds-the-path half, `orphaned` is False and
                    # this branch is unreachable.)
                    return ("%s; a writer holds that path AND the claimed "
                            "object was replaced, and nothing readable was "
                            "captured from it — the claim is left intact with "
                            "its location unresolved" % why)
                try:
                    # O_RDWR, because the copy is READ BACK before it is
                    # reported. Opening O_WRONLY and wrapping it "w+b" fails
                    # with EBADF the moment the verification reads — which is
                    # what this leg's own double-race arm caught, and the
                    # failure text then said the bytes "are lost", on the path
                    # whose entire job is not losing them.
                    fd2 = os.open(os.path.basename(full),
                                  os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600,
                                  dir_fd=dest_fd)
                    with _wrap_fd(fd2, "w+b") as out2:
                        # WRITE-ALL, then VERIFY THROUGH THIS DESCRIPTOR. The
                        # same short-write gap already cured in
                        # _retain_from_fd lived here too: one os.write call
                        # read as the whole buffer, and the only check was the
                        # inode — so a truncated copy passed as the judged
                        # bytes. Identity answers "which object";
                        # only the digest answers "which contents".
                        short = _write_all_verified(out2, claimed)
                        # CAPTURED FROM THE WRITER, not re-read by name: the
                        # sentence below claims THESE bytes are at that name.
                        undo_written = _member_id_of(out2.fileno())
                    if short is not None:
                        return ("%s; a writer holds that path AND the copy of "
                                "the claimed bytes is not those bytes (%s), "
                                "so nothing is reported as preserved"
                                % (why, short))
                    os.fsync(dest_fd)
                except OSError as e2:
                    return ("%s; a writer holds that path AND the claimed "
                            "object was replaced, and the bytes could not be "
                            "written anywhere (%s) — they are lost unless "
                            "this process still holds them" % (why, e2))
                return ("%s; a writer has since installed a different file at "
                        "that path AND the claimed object was replaced, so "
                        "the bytes helm judged are preserved at %s"
                        % (why, _say_where(keep, dest_fd, undo_written,
                                           claim_id)))
            except OSError as e:
                return ("%s; and it could not be put back (%s), so it is "
                        "preserved at %s"
                        % (why, e, _say_where(quarantine, qdir_fd,
                                              _member_id_of(payload_fd),
                                              claim_id)))
            # THE RESTORE MUST BE DURABLE BEFORE THE CLAIM IS TERMINALIZED
            # (cumulative refute). Marking the claim terminal while
            # the entry that put the row back is still unsynced records a
            # decision the disk has not made.
            try:
                os.fsync(pdir_fd)
            except OSError as e:
                return ("%s; the row was put back but that could not be made "
                        "durable (%s), so its claim is left standing at %s "
                        "rather than closed on an unsynced restore"
                        % (why, e, _say_where(qdir, pdir_fd,
                                             _member_id_of(qdir_fd),
                                             claim_id)))
            # AND THE SWEEP RESULT IS NOT DISCARDED. UNRECORDED here means the
            # claim is still standing with no decision written, which the
            # caller must hear about — it was silently dropped.
            state = _sweep_claim(qdir, "the row was put back", held,
                                 pdir_fd, qdir_fd)
            if state == UNRECORDED:
                return ("%s; the row is back, but this claim recorded no "
                        "decision and will be seen again at %s"
                        % (why, _say_where(qdir, pdir_fd,
                                           _member_id_of(qdir_fd), claim_id)))
            return why

        # THE PAYLOAD'S DIGEST IS RECORDED ONLY NOW, because only now is the
        # object OURS and unable to change. It is what binds "completed" to
        # THIS transaction: recovery calls a claim completed when the undo
        # holds these exact bytes, never merely when something with the same
        # logical identity happens to sit at the undo path. An adversarial
        # state — a stale undo carrying identity A, a claimed payload B, and
        # metadata naming A — makes recovery sweep B when no digest is
        # recorded (D1/D3). A crash BEFORE this write reads as not-completed,
        # which is correct: the copy had not happened yet.
        # ONE RESOLUTION OF THE PAYLOAD, AND EVERY QUESTION ASKED OF THE FD.
        # Regularity, bytes, digest and identity all came from
        # separate re-resolutions of `quarantine` — four chances for the name
        # to mean something else, on the object this whole leg exists to
        # judge. The claim directory is ours and pinned, so the payload is
        # opened O_NOFOLLOW relative to it exactly once and nothing below
        # touches the pathname again.
        # BOUND BEFORE ANYTHING CAN READ IT. `claimed` is a free variable in
        # _put_back, resolved at CALL time, and three _put_back call sites sit
        # above the read loop that used to be its only assignment — so a
        # payload that opened but was not regular, or whose digest failed,
        # reached `out.write(claimed)` with the name unbound and raised
        # UnboundLocalError AFTER the row had left its normal path.
        # Curing the one branch a refuter happened to reach would leave the
        # other two, so the name is bound where no caller can predate it. An
        # None means NOTHING WAS CAPTURED, which is not the same as capturing
        # an empty file — and b"" conflated them. Binding it to b""
        # cured the UnboundLocalError and bought a worse bug: a nonregular
        # member (a directory opens fine) fails the fd-link and then the
        # fallback FABRICATED an empty regular row at the operator's path
        # while the report said NOT removed. A sentinel that cannot be
        # mistaken for data is the difference.
        claimed = None
        payload_fd = _open_member(qdir_fd, os.path.basename(full))
        if payload_fd is None:
            return _put_back("the claimed object could not be opened safely, "
                             "so nothing was removed")
        try:
            if not _regular_fd(payload_fd):
                return _put_back("the claimed object is not an ordinary file, "
                                 "so it was NOT removed")
            claimed_digest = _sha256_fd(payload_fd)
            if claimed_digest is None:
                return _put_back("the claim could not be bound to its "
                                 "payload, so nothing was removed")
            os.lseek(payload_fd, 0, os.SEEK_SET)
            claimed = b""          # now it means "captured, and so far empty"
            while True:
                chunk = os.read(payload_fd, 1 << 16)
                if not chunk:
                    break
                claimed += chunk
            try:
                _write_meta(claimed_digest)
            except OSError as e:
                return _put_back("the claim could not be bound to its payload "
                                 "(%s), so nothing was removed" % e)
            if fsync_failed is not None:
                return _put_back("the claim could not be made durable (%s), "
                                 "so it was NOT removed" % fsync_failed)
            current = _json_fd(payload_fd)
        except BaseException:
            # NO CLOSE HERE. The owner's finally already covers every path out
            # of this function, so closing here made it exact-TWICE — EBADF at
            # best, and once the number is recycled, a close of somebody
            # else's file (reproduced). Exactly one place closes
            # each descriptor, and that place is the owner.
            raise
        # NOT CLOSED HERE. The descriptor stays open through the PUBLISH, so
        # the object proven regular, hashed and identity-checked above is the
        # very inode that gets published — closing it here and linking the
        # name afterwards is the window a review named. The owner's finally
        # closes it.
        if not isinstance(current, dict):
            return _put_back("the file changed under the sweep and no longer "
                             "parses, so it was NOT removed")
        here, there = _identity(current), _identity(row)
        # CANNOT-TELL MEANS KEEP, AND `!=` ALONE COULD NOT SAY THAT:
        # `_identity` returns None for a row with no id, and `None != None` is
        # FALSE, so two UNIDENTIFIABLE rows compared EQUAL and the removal went
        # ahead on the leg built to prevent that.
        if here is None or there is None:
            return _put_back(
                "the sweep cannot prove which row this file holds — %s carries "
                "no id — so it was NOT removed"
                % ("the file on disk" if here is None
                   else "the row this decision was made about"))
        if here != there:
            # ONE-ELEMENT TUPLE: `_identity` returns a tuple and `%` spreads
            # one across multiple conversions.
            what = "%s stamped %s" % here
            return _put_back(
                "the file changed under the sweep — it now holds %s, not the "
                "row this decision was made about, so it was NOT removed"
                % (what,))

        # OWNED AND PROVEN. The undo normally receives the OBJECT ITSELF by
        # rename; only a cross-filesystem undo falls back to a copy, and that
        # branch keeps the original rather than deleting it.
        # The undo directory was minted with the claim, exclusively, so there
        # is nothing to create here. It used to be made at this point with
        # exist_ok=True — which was correct while the undo was a SHARED
        # directory and is exactly the permission a per-claim one must not
        # have: exist_ok on a name that is supposed to be unique turns the
        # collision you want to hear about into silence.
        # THE PUBLISH MOVES NOTHING (a review ruling, meld e:1786171017).
        #
        # Round 7 made the undo a RENAME so the object itself became the undo
        # rather than a snapshot of its bytes, which fixed destroying a live
        # inode. But a rename is still a HANDOFF: it clobbers, and the payload
        # stops existing at the name the claim owns. With terminal residuals
        # now the ordinary outcome there is no reason to hand anything off at
        # all. The claim keeps its payload; the undo gets a SECOND NAME for
        # the same inode. `os.link` REFUSES an existing destination, so
        # no-replace is a property of the primitive rather than of a check.
        #
        # dir_fd would NOT have given this. It fixes which DIRECTORY resolves,
        # not rename's clobber — swapping the old rename for a dir_fd rename
        # and calling it no-replace was the mistake a review caught.
        # (the regularity question was asked of the payload's own descriptor
        # above, in the same resolution as its digest — the lstat-on-a-path
        # version here was a SECOND resolution and therefore TOCTOU)
        cross_fs = False
        err = _link_fd(payload_fd, os.path.basename(keep), dest_fd)
        if err is not None:
            # The link is attempted from the DESCRIPTOR, so a failure here is
            # about the destination or the filesystem boundary — never about
            # the source having changed, because the source is an inode we
            # hold open rather than a name that can be re-pointed.
            if isinstance(err, FileExistsError):
                return _put_back("the undo path is already occupied, which "
                                 "cannot happen for a freshly minted claim "
                                 "directory, so nothing was removed")
            if err.errno != errno.EXDEV:
                # ONLY a filesystem boundary justifies the copy. Everything
                # else — a full disk, a read-only mount — CANCELS, and
                # treating them all as EXDEV turned a cancellation into a
                # successful removal.
                return _put_back("the undo could not be placed (%s), so "
                                 "nothing was removed" % err)
            # CROSS-FILESYSTEM: a link cannot span devices, so the undo is a
            # fresh file created O_EXCL — exclusive by the OPEN, not by a
            # check — and the payload is still KEPT. Here the two are distinct
            # inodes, and the payload is the ONLY one a descriptor opened
            # before the claim can still write to, which is precisely why it
            # is not disposable.
            cross_fs = True
            try:
                fd = os.open(os.path.basename(keep),
                             os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600,
                             dir_fd=dest_fd)
                with _wrap_fd(fd, "w+b") as out:
                    short = _write_all_verified(out, claimed)
                if short is not None:
                    # THE UNDO IS THE ONLY BACKUP OF A ROW ABOUT TO LEAVE ITS
                    # PATH. A short write here produced a truncated undo that
                    # the removal then proceeded on, so the row went away and
                    # what stood in for it was not it.
                    return _put_back("the undo copy is not the claimed bytes "
                                     "(%s), so nothing was removed" % short)
            except OSError as e2:
                return _put_back("the undo copy could not be written (%s), so "
                                 "nothing was removed" % e2)
        # DURABILITY BEFORE ANY SOURCE-STATE TRANSITION (seam 5).
        # This used to swallow the directory sync and then sweep, which is the
        # prose lying: a claim cannot be called durably removed if the entry
        # naming its undo was never synced. A sync failure is now a REFUSAL,
        # and the row goes back.
        try:
            os.fsync(dest_fd)
        except OSError as e:
            return _put_back("the undo could not be made durable (%s), so "
                             "nothing was removed" % e)

        # THE PAYLOAD STAYS. Only now, with the undo durable, does the claim
        # take its terminal state.
        why = ("the row was removed; its payload is preserved in place as "
               "this claim's retained backup")
        state = _sweep_claim(qdir, why, held, pdir_fd, qdir_fd)
        rep["trash"] = trash_root
        # (the undo's location is reported below, once it has been verified
        # through the descriptor that owns it — a joined string here was the
        # defect)
        # ENOUGH EVIDENCE FOR THE CLI TO SAY "removed; retained backup"
        # (a review ruling): same-filesystem the payload and the undo are one
        # inode with two names and cost a dirent; cross-filesystem they are
        # two inodes and the backup is a real second copy. An operator
        # deciding what to dispose of needs to be able to tell those apart.
        try:
            st = os.fstat(payload_fd)   # the descriptor we judged and published
            ino, nlink = st.st_ino, st.st_nlink
        except OSError:
            ino = nlink = None
        # THE LIVE PATH REPORTS VERIFIED LOCATIONS, NOT JOINED STRINGS
        # (second immutable refuter (2)). Recovery had a verifier
        # and this leg did not: `quarantine` and `keep` are built by joining a
        # directory pathname to a member name, so a rename-and-reuse of qdir
        # or dest between the publish and this line sent the operator to a
        # location that holds someone else's file — the same defect the
        # recovery door exists to close, on the path that runs EVERY time a
        # row is removed.
        #
        # Both are checked through the descriptors we still hold: the payload
        # against the inode we judged, and the undo against its own member.
        # Same-filesystem the undo IS that inode under a second name;
        # cross-filesystem it is a separate copy, so it answers to its own
        # identity and digest rather than borrowing the payload's.
        payload_at = _verified_path(quarantine, qdir_fd,
                                    (st.st_dev, st.st_ino)
                                    if ino is not None else None)
        # THE EXPECTATION CANNOT COME FROM THE THING BEING CHECKED. My first
        # version opened the current member at dest/name, took undo_id from
        # THAT descriptor, and then verified the path against it — which
        # always passes, so a rename-over after the publish made a stranger's
        # bytes report as the undo. A verifier that derives its own
        # expectation is not a verifier; it is a formality with the shape of
        # one, and it is the same defect this lane keeps removing, one layer
        # in and wearing my own cure as a costume.
        #
        # The expectation comes from what was PUBLISHED. Same-filesystem the
        # undo is the payload's inode under a second name, so it answers to
        # the identity we judged. Cross-filesystem it is a distinct copy, so
        # it answers to the DIGEST we wrote — compared against the claim's own
        # claimed_digest, which was computed from the payload descriptor
        # before any of this.
        undo_at, undo_digest = None, None
        ufd = _open_member(dest_fd, os.path.basename(full))
        if ufd is not None:
            try:
                if cross_fs:
                    undo_digest = _sha256_fd(ufd)
                    if undo_digest is not None \
                            and undo_digest == claimed_digest:
                        undo_at = _verified_path(keep, dest_fd,
                                                 _member_id_of(ufd))
                elif ino is not None:
                    undo_at = _verified_path(keep, dest_fd,
                                             (st.st_dev, st.st_ino))
            except OSError:
                undo_at = None
            finally:
                os.close(ufd)
        entry = {"claim_id": claim_id, "row": os.path.basename(full),
                 "payload": payload_at, "undo": undo_at, "state": state,
                 "why": why, "inode": ino, "nlink": nlink,
                 "same_inode": not cross_fs}
        if payload_at is None:
            entry["payload_unknown"] = True
        if undo_at is None:
            entry["undo_unknown"] = True
        if undo_digest is not None:
            entry["undo_sha256"] = undo_digest
        rep.setdefault("residuals", []).append(entry)
        rep.setdefault("undos", []).append(
            {"row": os.path.basename(full), "undo": undo_at,
             "undo_unknown": undo_at is None})
        if cross_fs:
            rep.setdefault("preserved", []).append(
                {"row": os.path.basename(full), "undo": undo_at,
                 "object": payload_at})
        return None
    finally:
        # ONE OWNER CLOSES ALL OF THEM. No callee closes a borrowed
        # descriptor — a capability that can be closed by whoever it is
        # passed to is not a capability, it is a liability with a lifetime
        # nobody owns.
        if held is not None:
            held.close()
        # PDIR IS BORROWED and is NOT in this list: the caller pinned it and
        # closes it. Closing a capability that was passed to you is the
        # exact-twice defect cured elsewhere in this lane, and after the A
        # split it would shut the descriptor the enumeration is still using
        # for the REST of the sweep.
        for _fd in (claim_lock, qdir_fd, dest_fd, payload_fd):
            if _fd is None:
                continue
            try:
                os.close(_fd)
            except OSError:
                pass


def _identity(row):
    """The pair that says WHICH row a personal file holds: its own id and the
    ledger row it is stamped with. Both, because either alone can repeat — an
    id is unique only within a session dir, and one ledger row can be stamped
    on more than one personal row. -> None when the row carries no id at all,
    or carries an EMPTY one.

    NONE MEANS "I CANNOT SAY", AND THE CALLER MUST TREAT IT AS SUCH. This
    docstring used to claim None "never compares equal to anything, including
    another None" — a statement about Python that is simply false, since
    `None == None`. `_trash` guarded its unlink with a bare `!=` on this
    function's output and so proceeded whenever BOTH sides were unidentifiable,
    which is the one case the guard exists for. Measured 2026-08-07: a live row
    destroyed through the guard that was built to save it.

    The line was load-bearing in the worst way — an auditor reading it stopped,
    because it said the None case was handled. A comparison cannot express
    "unknown"; only the caller can, and `_trash` now refuses on either side
    being None before it compares at all."""
    if not isinstance(row, dict):
        return None
    rid = row.get("id")
    if rid in (None, ""):
        return None
    return (str(rid), str(row.get(STAMP) or ""))


# THE CAPABILITIES EVERY MEMBER OPERATION IN THIS MODULE DEPENDS ON.
# Named as DATA rather than assumed as a platform fact, which is what makes
# the refusal path testable: a test removes one entry and the preflight must
# refuse (meld e:1786171017). I had argued this branch was
# unreachable on Linux and therefore either dead-if-written or a reason to
# refuse at import; both were wrong, and the framing was the error — a
# capability check over patchable sets is neither dead nor fatal.
# EVERY PRIMITIVE THE TRANSACTION ACTUALLY CALLS, not the ones I remembered.
# `mkdir` was missing, so the census passed on a platform without
# it and the mkdirat that mints the claim then raised NotImplementedError from
# inside the mutation — a preflight that says "safe to proceed" and is wrong
# is worse than no preflight, because it is the thing the caller trusted.
#
# This list is derived from the call sites rather than recalled; there is an
# arm that walks _trash_at and _recover_claims_at for dir_fd= keywords and
# fails if any names a primitive absent here.
# DERIVED FROM THE CALL SITES, IN BOTH DIRECTIONS (item 11). The
# list was hand-maintained and I checked it only for OMISSIONS — my own scan
# printed "os.* called with a dir_fd: link, mkdir, open, stat" against a
# declared list containing rename, unlink and rmdir, and I asked whether
# anything was MISSING without ever asking whether anything was SURPLUS. A
# one-directional check on a two-directional question.
#
# The surplus is not harmless: requiring descriptor forms this module never
# calls REFUSES the whole sweep on hosts that are perfectly capable of running
# it. A capability census that over-claims strands rows exactly as surely as
# one that under-claims, and it does it on the machines least able to spare
# the loss.
_NEED_DIR_FD = ("link", "mkdir", "open", "stat")
_NEED_FLAGS = ("O_DIRECTORY", "O_NOFOLLOW")


def _capability_gap():
    """What this platform lacks for safe member operations, or "".

    A NAME, not a boolean: the refusal has to say WHICH primitive is missing,
    because "cannot operate safely here" is not something an operator can act
    on and the whole point of refusing is that they can."""
    # BY NAME, NOT BY OBJECT IDENTITY. Resolving getattr(os, "link") and
    # testing membership couples this to the identity of that object, so ANY
    # caller or test that wraps os.link sees a capability refusal instead of
    # its wrapper — including the idempotence arm, whose whole method is
    # hooking link and unlink to prove neither is called. The question this
    # asks is whether the PLATFORM's link supports dir_fd, which is a property
    # of the name in the support set, and patching the set (the mutation test
    # a review specified) still removes the name.
    have = {getattr(f, "__name__", "") for f in os.supports_dir_fd}
    missing = [n for n in _NEED_DIR_FD if n not in have]
    if not any(getattr(f, "__name__", "") == "listdir" for f in os.supports_fd):
        missing.append("listdir(fd)")
    missing += [f for f in _NEED_FLAGS if not hasattr(os, f)]
    # /proc/self/fd is how an OPEN DESCRIPTOR is published without a copy.
    # Named here so its absence REFUSES rather than silently degrading to a
    # name-based link, which is the window this whole seam is about.
    if not os.path.isdir("/proc/self/fd"):
        missing.append("/proc/self/fd")
    # NO-CLOBBER MOVE, CENSUSED WITH EVERYTHING ELSE. This was
    # checked deep inside the transaction, AFTER the claim directory, the undo
    # directory and the manifest had all been created — so an unsupported host
    # reported "nothing was removed" and left three artifacts behind. A
    # capability the transaction cannot proceed without belongs in the same
    # preflight as the rest, where refusing costs nothing.
    if not fsops.have_renameat2():
        missing.append("renameat2(RENAME_NOREPLACE)")
    if not missing:
        return ""
    return ("this platform cannot address directory members safely — %s "
            "%s no directory-descriptor form, so every member operation "
            "would have to re-resolve a pathname that can be replaced "
            "between the check and the act"
            % (", ".join(missing), "has" if len(missing) == 1 else "have"))


def _link_fd(fd, dst_name, dst_dir_fd):
    """Publish the OPEN DESCRIPTOR's inode under `dst_name`. -> True on success.

    THE OBJECT JUDGED IS THE OBJECT PUBLISHED (the root finding).
    os.link publishes a NAME, so fstat-ing and hashing an open payload and
    then linking its name re-resolves — the member can be swapped in between,
    and everything proven about the descriptor is proven about a file that is
    no longer there. Python does not expose AT_EMPTY_PATH (measured: False),
    but /proc/self/fd/N names the descriptor's inode and os.link follows that
    symlink when asked to.

    MEASURED AGAINST THE ACTUAL ATTACK: open the payload, rename another file
    over its name, then link through /proc/self/fd/N — the link published the
    descriptor's bytes while the name held the attacker's. Same inode, nlink 2.

    A copy would also close the window, and costs one full duplicate of every
    removed row; this keeps one-inode-two-names, which is what makes a
    retained backup a directory entry instead.

    -> None on success, else the OSError. THE ERROR IS THE RETURN VALUE, not a
    False: the caller has to tell EXDEV (publish the other way) from anything
    else (cancel the removal), and collapsing every failure into one boolean
    made a full disk look like a filesystem boundary and silently converted a
    cancellation into a successful copy."""
    try:
        os.link("/proc/self/fd/%d" % fd, dst_name, dst_dir_fd=dst_dir_fd,
                follow_symlinks=True)
        return None
    except OSError as e:
        return e


def _retain_names(row_name):
    """An UNBOUNDED supply of no-clobber candidates.

    A fixed list of eight was a silent loss condition: eight occupied names
    cannot authorise closing a nameless judged inode, and that is exactly what
    exhausting the list did. Collisions must cost another attempt,
    never the bytes, so this yields until the CALLER hits a failure that is
    not EEXIST — a real error gets surfaced, a taken name never does."""
    yield row_name
    n = 0
    while True:
        # RANDOM, NOT SEQUENTIAL. A counter makes every concurrent rescue
        # of the same row collide on the same next name, which is the one
        # case where retries do not converge.
        yield "%s.retained-%s" % (row_name, os.urandom(6).hex())
        n += 1


def _retain_from_fd(payload_fd, qdir_fd, row_name):
    """Keep the held bytes reachable. -> (basename, same_inode, why).

    `why` is None on success. THE LAST NAME CAN BE TAKEN WHILE WE HOLD THE
    BYTES: a rename-over removes the only link to the inode this claim judged,
    the descriptor alone keeps it alive, and closing that descriptor destroys
    it. An honest "location unknown" is true and useless — the operator is
    told we cannot say where the bytes are, and then they stop existing.

    Linking a NAMELESS inode is not possible here, measured rather than
    assumed: linkat through /proc/self/fd/N resolves that PATH, so once the
    last name is gone it dangles with ENOENT, and re-linking the descriptor
    needs AT_EMPTY_PATH, which this build does not expose and which wants
    CAP_DAC_READ_SEARCH regardless. So we link while a name remains and COPY
    when none does — a different inode with the same content, reported as
    such."""
    for candidate in _retain_names(row_name):
        err = _link_fd(payload_fd, candidate, qdir_fd)
        if err is None:
            try:
                os.fsync(qdir_fd)
            except OSError as e:
                # DURABILITY BEFORE THE REPORT, here as everywhere else in
                # this leg. A retained location that the disk has not
                # committed to is a promise this process cannot keep across a
                # crash, and saying "your bytes are safe at X" is the one
                # sentence that must never be provisional.
                return candidate, True, "the retained link is not durable (%s)" % e
            return candidate, True, None
        if not isinstance(err, FileExistsError):
            break
    # THE COPY PATH. Nothing below cleans up by NAME on failure: a partial
    # member left behind is recoverable, and an unlink by re-resolved name is
    # the conditional-delete POSIX does not provide — a replacement between
    # the close and the unlink means we delete a stranger's file (the same
    # shape this whole lane exists to remove).
    want = _sha256_fd(payload_fd)
    for candidate in _retain_names(row_name):
        try:
            dst = os.open(candidate, os.O_RDWR | os.O_CREAT | os.O_EXCL,
                          0o600, dir_fd=qdir_fd)
        except FileExistsError:
            continue
        except OSError as e:
            return None, False, "no retained name could be created (%s)" % e
        try:
            off = 0
            while True:
                block = fsops.pread(payload_fd, 1 << 16, off)
                if not block:
                    break
                # WRITE-ALL. os.write may write fewer bytes than it was given,
                # and treating one call as the whole block produced a silently
                # TRUNCATED copy while the digest was computed from the
                # ORIGINAL descriptor — a report that verified the wrong
                # object.
                sent = 0
                while sent < len(block):
                    sent += os.write(dst, block[sent:])
                off += len(block)
            os.fsync(dst)
            # VERIFIED THROUGH ITS OWN DESCRIPTOR, not through the source's.
            # The claim is about the COPY, so the copy is what gets read back.
            got = _sha256_fd(dst)
            size = os.fstat(dst).st_size
        except OSError as e:
            os.close(dst)
            return candidate, False, ("the retained copy is incomplete (%s) "
                                      "and was left in place" % e)
        os.close(dst)
        if want is None or got != want or size != off:
            return candidate, False, ("the retained copy does not match the "
                                      "bytes it was made from and was left "
                                      "in place")
        try:
            os.fsync(qdir_fd)
        except OSError as e:
            return candidate, False, "the retained copy is not durable (%s)" % e
        return candidate, False, None
    return None, False, "no retained name could be created"


def _verify_reported_paths(out, pdir_fd, qdir_fd, payload_fd=None,
                           row_name=None):
    """Strip any reported path that the pinned directories no longer own.

    ONE DOOR, NOT TEN (a stale-report finding, and my own third
    encounter with this shape). Ten different outcomes emitted `preserved` or
    `occupied` by joining a pathname to a member name, each correct when
    written and each stale the moment the claim is renamed away. Fixing them
    one at a time leaves the eleventh to be written next week, so the rule
    lives where every outcome passes instead: a path survives only while its
    parent is still the directory we acted through.

    A precise wrong path sends an operator to the one place the bytes are not,
    which is worse than telling them we cannot say — unknown path over a lie."""
    if not isinstance(out, dict):
        return out
    expect = out.pop("_member_id", None)
    # `claim` IS A PATH TOO (production refuter E). The door verified
    # the two locations an operator reads for BYTES and left the one they read
    # for the CLAIM itself unchecked — and every fallback renderer in this
    # module falls back to `claim` when `preserved` is unknown, which is
    # exactly the case the door creates. So the stale name it removed from one
    # field reappeared in the other, on the same line, at the same moment.
    #
    # It is a DIRECTORY, so its parent is the personal dir and its expectation
    # is the claim directory we actually acted through. A claim that cannot be
    # verified is named by its ID, which is stable and still actionable.
    claim = out.get("claim")
    if claim and qdir_fd is not None:
        out.setdefault("claim_id", os.path.basename(claim))
        if _verified_path(claim, pdir_fd, _member_id_of(qdir_fd)) is None:
            out["claim"] = None
            out["claim_unknown"] = True
    for key, pinned in (("preserved", qdir_fd), ("occupied", pdir_fd)):
        path = out.get(key)
        if not path:
            continue
        if _verified_path(path, pinned, expect if key == "preserved"
                          else ANY_MEMBER) is None:
            # A RESCUE BEATS AN HONEST SHRUG, and this is the only frame that
            # can perform one: `preserved` names OUR bytes, and we are still
            # holding them. Re-name the descriptor into the pinned
            # claim and the report becomes true again — verified through the
            # same door, never assumed because the link returned success.
            kept, same, rescue_why = None, False, None
            if key == "preserved" and payload_fd is not None and row_name:
                kept, same, rescue_why = _retain_from_fd(payload_fd, qdir_fd,
                                                         row_name)
            if kept is not None and rescue_why is None:
                try:
                    qpath = os.path.join(os.path.dirname(path), kept)
                except (TypeError, AttributeError):
                    qpath = None
                # A COPY IS NOT THE INODE, so it is not verified as one.
                # The link case still answers to identity; the copy answers to
                # its digest, and the outcome says which — an operator holding
                # a "retained" path deserves to know whether it is the object
                # helm judged or a faithful reproduction of it.
                want = expect if same else ANY_MEMBER
                if qpath and _verified_path(qpath, pinned, want) is not None:
                    out[key] = qpath
                    out["retained_rescued"] = "inode" if same else "copy"
                    if not same:
                        out["retained_sha256"] = _sha256_fd(payload_fd)
                    continue
            out[key] = None
            out[key + "_unknown"] = True
            if rescue_why is not None:
                # THE RESCUE IS REPORTED WHETHER OR NOT IT WORKED. A partial
                # or unsynced retained member is left in place on purpose —
                # bytes over cleanup — so the operator is told where it is and
                # what is wrong with it rather than finding an unexplained
                # file.
                out["retained_partial"] = kept
                out["retained_why"] = rescue_why
            try:
                st = os.fstat(pinned)
                out.setdefault("claim_inode", [st.st_dev, st.st_ino])
            except (OSError, TypeError):
                pass
    return out


def _write_all_verified(fh, data):
    """Write every byte, then prove it by reading the file back. -> None|why.

    THE SAME DEFECT AT FOUR SITES, so it becomes one function rather than four
    repairs (it was found at each in turn). os.write and file.write may
    write FEWER bytes than they were given; treating one call as the whole
    buffer silently produces a truncated file, and every one of these sites
    then reported that file as the preserved copy of the judged bytes. A
    digest taken from the SOURCE descriptor cannot catch it — only reading
    back what actually landed can, which is why the verification is part of
    the writer instead of a step a caller can forget."""
    sent = 0
    while sent < len(data):
        wrote = fh.write(data[sent:])
        if not wrote:
            return "the copy stopped accepting bytes after %d of %d" % (
                sent, len(data))
        sent += wrote
    fh.flush()
    os.fsync(fh.fileno())
    try:
        fh.seek(0)
        back = fh.read()
    except (OSError, ValueError) as e:
        return "the copy could not be read back to verify it (%s)" % e
    if back != data:
        return "the copy came back %d bytes, not %d" % (len(back), len(data))
    return None


RS = b"\x1e"                 # record separator: a frame STARTS here
FRAME_END = b"\n"           # ...and is COMMITTED by this


class ManifestCorrupt(Exception):
    """A frame was COMPLETED and is unreadable — fail closed, never guess."""


def _frames(blob):
    """Every complete snapshot in `blob`, oldest first. -> list of dicts.

    THE MANIFEST IS APPEND-ONLY BECAUSE TRUNCATE HAS NO SAFE MOMENT (per
    architecture result C). Every state transition used to seek(0)+truncate()
    and rewrite in place, so a crash between the truncate and the fsync left
    the claim's ONLY record empty or half-written — and a claim whose manifest
    cannot be parsed is invisible to every surface while its payload sits
    beside it. There is no ordering of truncate-then-write that avoids this;
    the cure is to never destroy the old record, so the previous complete
    snapshot survives until a new complete one exists.

    A frame is RS + compact JSON + LF. The LF is the COMMIT: a tail without
    one is a write that was interrupted, and it is ignored — that is the crash
    this format exists to survive. A frame that HAS its LF and still does not
    parse is committed corruption, which is a different fact and must not be
    silently skipped, because skipping it would silently promote an older
    state over a newer one.

    A legacy manifest is a single bare JSON object with no framing, and it is
    read as the first snapshot so claims written before this format are not
    orphaned by it."""
    out = []
    if not blob:
        return out
    head, sep, rest = blob.partition(RS)
    if head.strip():
        # LEGACY PREFIX: one bare object, pre-framing.
        try:
            legacy = json.loads(head.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ManifestCorrupt("the legacy manifest prefix is unreadable")
        if not isinstance(legacy, dict):
            # DICT-STRICT. A completed frame holding a scalar or a list parses
            # fine and is not a snapshot; accepting it would put a non-record
            # into the chain and let `null` or `[]` read as the claim's state.
            raise ManifestCorrupt("the legacy manifest prefix is not a record")
        out.append(legacy)
    if not sep:
        return out
    chunks = rest.split(RS)
    for i, chunk in enumerate(chunks):
        if not chunk.endswith(FRAME_END):
            # A TORN TAIL CONTAINS NO COMMIT BYTE AT ALL (C2). A
            # final chunk that HAS an LF somewhere inside it but does not END
            # with one is a COMPLETE frame followed by something else — and
            # ignoring it as "torn" served the SUPERSEDED state while a
            # committed newer one sat right there in the file. Whatever that
            # trailing matter is, it is not a write this format made, so the
            # honest answer is to refuse rather than to pick a state.
            if FRAME_END in chunk:
                raise ManifestCorrupt(
                    "a completed manifest frame is followed by bytes this "
                    "format did not write")
            # AN INCOMPLETE FRAME IS ONLY A TAIL IF NOTHING FOLLOWS IT.
            # A retry after a crash appends a FRESH frame past the
            # torn one, so `old + RS+partial + RS+complete` is a normal file —
            # and stopping at the partial would serve the SUPERSEDED state
            # while a complete newer one sat right behind it. Skip it and keep
            # scanning; only the last chunk can be the interrupted write.
            if i == len(chunks) - 1:
                break          # TORN TAIL: the crash this format survives
            continue           # a torn frame a retry already wrote past
        try:
            snap = json.loads(chunk[:-1].decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ManifestCorrupt("a completed manifest frame is unreadable")
        if not isinstance(snap, dict):
            raise ManifestCorrupt("a completed manifest frame is not a record")
        out.append(snap)
    return out


def _lock_claim_dir(qdir_fd):
    """An exclusive lock on the CLAIM DIRECTORY. -> (fd, None) | (None, why).

    ONE OWNER IDENTITY FOR ONE LOGICAL CLAIM (item 2). Recovery
    locked the directory while the live removal locked only its own
    claim.json inode — two different owners for the same claim. Rename that
    manifest aside, install a byte-identical replacement, and recovery takes
    the directory and the REPLACEMENT manifest while the live transaction
    carries on under the old inode: both believe they hold the claim, and
    reproduced, recovery restores while live publishes.

    A member can always be replaced; the directory cannot be, without the
    pinned parent changing. So the directory is the identity, and both paths
    take THIS lock — which is the only way the exclusion means anything."""
    # HELD AND UNLOCKABLE ARE DIFFERENT ANSWERS, and my first version of this
    # helper returned None for both — collapsing a distinction this lane had
    # already established, inside the cure for a different collapse. A claim
    # another sweep owns is expected and silent; a claim we could not lock AT
    # ALL is unresolved and must be REPORTED, because an unresolved claim
    # nobody mentions is the silently-absent row the whole leg exists to
    # prevent.
    try:
        # "." beneath a pinned directory fd is already a directory identity;
        # O_DIRECTORY is a fast kernel assertion, not the source of that fact.
        fd = os.open(".", openflags.verified_directory(os.O_RDONLY),
                     dir_fd=qdir_fd)
    except OSError:
        return None, "unlockable"
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None, "held"
    except OSError:
        os.close(fd)
        return None, "unlockable"
    return fd, None


class _ClaimHandle:
    """The manifest handle AND the claim lock, with one lifetime.

    The lock is held on the claim DIRECTORY and the record is a member of it,
    so a caller holds two capabilities that must be released together. Closing
    the file while the directory lock lived on would keep the claim held by a
    descriptor nobody has a reference to."""

    def __init__(self, fh, lockfd):
        self._fh = fh
        self._lockfd = lockfd

    def close(self):
        try:
            self._fh.close()
        finally:
            if self._lockfd is not None:
                try:
                    os.close(self._lockfd)   # releases the flock
                finally:
                    self._lockfd = None

    def __getattr__(self, name):
        return getattr(self._fh, name)


def _private_record(fh, qdir_fd):
    """Why this manifest is not PRIVATE TRANSACTION STORAGE, or None.

    TWO THINGS THE FLOCK DOES NOT ESTABLISH (findings 14 and 15):

    (1) A LOCK ON AN INODE IS NOT A LOCK ON THE CLAIM. flock serializes access
    to the object it was taken on, and the claim's identity is the DIRECTORY —
    so replacing claim.json with a byte-copy at the same name gives a second
    actor a different inode to lock, and both proceed believing they hold the
    claim. Verifying the binding AFTER a write is too late: by then the other
    actor has already acted. The held inode must BE the member at CLAIM_META,
    checked before anything is written.

    (2) A MULTIPLY-LINKED RECORD IS NOT PRIVATE. A claim.json hard-linked from
    somewhere else has nlink > 1, and appending this transaction's state to it
    changes bytes that belong to whatever else holds that link. O_EXCL creates
    a fresh file with one link; anything else is somebody's shared file."""
    try:
        mine = os.fstat(fh.fileno())
    except OSError as e:
        return "the manifest could not be examined (%s)" % e
    if mine.st_nlink != 1:
        return ("the manifest has %d links, so it is shared with something "
                "outside this transaction and writing to it would change "
                "bytes that are not ours" % mine.st_nlink)
    try:
        there = os.stat(CLAIM_META, dir_fd=qdir_fd, follow_symlinks=False)
    except OSError as e:
        return "the manifest is not reachable at its own name (%s)" % e
    if (mine.st_dev, mine.st_ino) != (there.st_dev, there.st_ino):
        return ("the manifest we hold is no longer the one at its name, so "
                "this lock does not serialize the claim")
    return None


def _read_manifest(fh):
    """The latest complete snapshot on a HELD descriptor, or None."""
    fh.seek(0)
    frames = _frames(fh.read())
    return frames[-1] if frames else None


def _append_manifest(fh, snapshot, qdir_fd, first=False):
    """Append a full snapshot and prove it landed. -> None | why.

    FULL SNAPSHOTS, NOT DELTAS: a reader must never have to replay a chain to
    know the current state, and the latest complete frame is the whole answer.

    The directory is synced only when this call CREATED the entry; later
    appends change the file, not its name, so paying for a directory sync on
    every transition would be a cost with no fact behind it."""
    body = RS + json.dumps(snapshot, separators=(",", ":")).encode("utf-8")

    def _all(buf):
        sent = 0
        while sent < len(buf):
            wrote = fh.write(buf[sent:])
            if not wrote:
                raise OSError(errno.EIO, "the manifest stopped accepting bytes")
            sent += wrote
        fh.flush()
        os.fsync(fh.fileno())

    # RE-ESTABLISHED BEFORE EVERY WRITE. Holding the lock since the last
    # check is not evidence that the binding still holds: the member can be
    # replaced under a lock taken on the old inode, which is exactly what
    # finding 14 reproduces.
    why = _private_record(fh, qdir_fd)
    if why is not None:
        return why
    try:
        fh.seek(0, os.SEEK_END)
        # TWO WRITES, TWO SYNCS — THE COMMIT BYTE GOES LAST AND ALONE
        # (review item C1). The format's whole claim is that the closing LF is
        # the commit, so that every crash reads as the old state or the new
        # one. Writing RS+JSON+LF in ONE call and syncing once does not
        # establish that: nothing orders the persistence of bytes inside a
        # single write, so the LF can reach the platter while the body behind
        # it has not — and the reader then sees a COMPLETE frame whose
        # contents are partly whatever was in that block before. That is the
        # committed-corruption case, manufactured by the writer.
        #
        # The body is made durable first. Only then does the byte that MEANS
        # "durable" get written, and it is made durable itself. A crash
        # between the two leaves a torn tail, which is the case the reader
        # already handles by ignoring it.
        _all(body)
        _all(FRAME_END)
        if first:
            os.fsync(qdir_fd)
    except OSError as e:
        return "the claim could not be recorded (%s)" % e
    # PRIVACY IS BOUND ACROSS THE MUTATION, NOT SAMPLED BEFORE IT
    # (item 3). _private_record checks nlink == 1 and then writes; a link
    # created at the first write() lands the append on an inode that is now
    # shared, and the call reported SUCCESS while an external file's bytes
    # changed. Reproduced: append_success, link count 2, external bytes
    # modified.
    #
    # Nothing can PREVENT a hard link — anyone with access can make one at any
    # instant — so the property is not exclusivity, it is HONESTY: if the
    # record stopped being private at any point across the write, this
    # transition did not happen on private storage and must not be reported as
    # though it did. The bytes are already shared by then; what we control is
    # whether we claim a clean write over them.
    why = _private_record(fh, qdir_fd)
    if why is not None:
        return ("the manifest stopped being private during the write (%s), "
                "so this transition is not recorded as durable" % why)
    # READ BACK THROUGH THE SAME DESCRIPTOR. A write that returned is not a
    # write that is there.
    try:
        back = _read_manifest(fh)
    except ManifestCorrupt as e:
        return str(e)
    except OSError as e:
        return "the claim could not be read back (%s)" % e
    if back != snapshot:
        return "the manifest read back as something else"
    # AND THE NAME STILL LEADS TO THE INODE WE WROTE. A rename-and-reuse
    # between the open and here means the record we just made durable is in a
    # directory nobody will look in.
    try:
        mine = os.fstat(fh.fileno())
        there = os.stat(CLAIM_META, dir_fd=qdir_fd, follow_symlinks=False)
    except OSError as e:
        return "the manifest's own name could not be checked (%s)" % e
    if (mine.st_dev, mine.st_ino) != (there.st_dev, there.st_ino):
        return "the manifest we wrote is no longer the one at its name"
    return None


def _wrap_fd(fd, mode):
    """os.fdopen, but the raw descriptor is never orphaned by a failed wrap.

    A `with os.fdopen(fd, ...) as f:` transfers ownership to the file object —
    but ONLY once fdopen returns. If it raises, the context never begins, no
    __exit__ ever runs, and the raw fd is leaked with nothing left holding a
    reference to it (three copy sites had this shape). The window is
    small and the consequence is not: a recovery pass over many claims
    exhausts the table, and the failure then looks like something else
    entirely.

    Exactly one close on the failure path, and none on the success path —
    the caller's `with` owns it from there."""
    try:
        return os.fdopen(fd, mode)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _adopted_shape(dfd):
    """Why this directory is NOT the empty one we minted, or None if it is.

    ONE CHECK FOR EVERY DIRECTORY THIS TRANSACTION ADOPTS. mkdirat proves
    nothing was there when we created it; it cannot prove the thing we then
    OPENED is that same object, because a same-UID substitution in between
    ignores any lease we could invent. What CAN be established through the
    opened descriptor is that it is EMPTY — and an empty directory is one
    where adoption is provably harmless, since every member below is created
    O_EXCL, the row moves in no-clobber, and nothing is ever cleaned by name.
    A replacement WITH CONTENTS is somebody else's and gets refused.

    It exists as a function because it was written for the claim directory and
    NOT for the undo directory, and that asymmetry was a finding — the undo
    directory was the one capability here with no check on it at all."""
    try:
        here = os.listdir(dfd)
    except OSError as e:
        return "it could not be read (%s)" % e
    return "it already holds %d entr%s" % (
        len(here), "y" if len(here) == 1 else "ies") if here else None


ANY_MEMBER = object()   # "some object at this name" IS the whole claim


def _reported_row_path(full, pdir_fd, expect=None, gone_ok=False):
    """The row's path if the pinned parent still owns that name, else None.

    THE SWEEP'S REPORTS NEEDED THE SAME DOOR AS RECOVERY'S (review item A gap
    3). `unparseable`, `orphaned`, `would_remove`, `removed` and `failed` all
    carried the raw `full` they were enumerated with, so after a rename of the
    session directory every one of them named a path inside the REPLACEMENT —
    the operator is told which files were touched and handed a stranger's.

    A removed row is the special case: its member is INTENTIONALLY gone, so
    only the parent binding can be checked, and that is exactly the question
    worth asking — is this pathname still inside the directory we swept?"""
    if pdir_fd is None or not full:
        return None
    # MEMBER FIRST, PARENT BINDING LAST — and I wrote this door with the OLD
    # ordering days after curing exactly that inversion in _verified_path.
    # Asking the pathname question first lets a rename between the
    # two probes check the member through the PINNED directory while the
    # pathname being returned resolves to the replacement: both probes pass
    # and the returned string names a stranger. The last thing established has
    # to be the thing the return value asserts.
    #
    # `gone_ok` is the REMOVED case and only that one: its member is
    # intentionally absent, so the parent binding is the whole question left.
    try:
        st = os.stat(os.path.basename(full), dir_fd=pdir_fd,
                     follow_symlinks=False)
    except FileNotFoundError:
        if not gone_ok:
            return None
        st = None
    except OSError:
        return None
    if st is not None:
        if expect is None:
            if not gone_ok:
                return None
        elif (st.st_dev, st.st_ino) != tuple(expect):
            return None
    try:
        par = os.stat(os.path.dirname(full) or ".", follow_symlinks=False)
        pin = os.fstat(pdir_fd)
    except OSError:
        return None
    return full if (par.st_dev, par.st_ino) == (pin.st_dev, pin.st_ino) \
        else None


def _row_report(full, pdir_fd, expect=None, gone_ok=False, **extra):
    """A report entry whose path is verified, or names itself unknown."""
    where = _reported_row_path(full, pdir_fd, expect, gone_ok)
    out = dict(extra)
    out["file"] = where
    out["name"] = os.path.basename(full or "")
    if where is None:
        out["file_unknown"] = True
    return out


def _say_where(path, pinned_fd, expect, claim_id):
    """A location safe to put in a sentence an operator will act on.

    THE REFUSAL TEXTS BYPASSED THE SUCCESS PATH'S VERIFIER. Four
    of them interpolated `quarantine`, `keep` or `qdir` — raw joins of a
    directory pathname to a member name — so a rename-and-reuse made the
    refusal name a stranger's file, on the exact path where the operator is
    already being told something went wrong and is most likely to go looking.

    Fixing four sentences leaves the fifth to be written next week, so the
    rule lives in one place: verified path, or a phrase that names the claim
    and admits it cannot say."""
    ok = _verified_path(path, pinned_fd, expect)
    if ok is not None:
        return ok
    return ("an unverified location in claim %s (that directory was renamed "
            "or replaced, so helm will not name a path)" % claim_id)


def _verified_path(path, pinned_fd, expect=None):
    """`path` if its final parent still IS the pinned directory, else None.

    A REPORT MUST NAME WHERE THE BYTES ARE, NOT WHERE THEY WERE PLANNED TO BE.
    Every `preserved` string in this module was built by joining
    the claim's pathname to a member name, at a moment that may be long past:
    rename the pinned claim aside and the report cheerfully names a path that
    holds nothing, while the bytes sit in the moved directory. The operator is
    then hunting the one place they are not.

    Comparing (st_dev, st_ino) of the path's parent against the descriptor we
    actually acted through answers whether the name still means the inode. On
    mismatch the caller reports identity instead of a location — unknown path
    over a lie."""
    if pinned_fd is None:
        return None
    # AND THE MEMBER MUST STILL BE THERE. Verifying only the PARENT
    # answers "is this the right directory" and not "are the bytes at this
    # name" — the member can be renamed or removed inside a directory that is
    # still ours, and the report would name it anyway. Half a verification
    # reads as a whole one to anyone downstream.
    # AND THE MEMBER MUST BE THE ONE WE MEAN. Verifying the PARENT answers
    # "is this the right directory"; verifying the member EXISTS answers "is
    # something there"; neither answers what the report actually claims, which
    # is "are THESE bytes at that name". `expect` is (st_dev, st_ino) taken
    # from the descriptor we held. Without an expectation a precise path is
    # unrepresentable, so this returns None rather than a name it cannot stand
    # behind.
    # THE MEMBER FIRST, THE PARENT BINDING LAST (production
    # refuter). The parent check answers "does this pathname still name the
    # directory I acted through", and it was asked FIRST — so a rename between
    # the two probes let the member be checked through the PINNED (old)
    # directory while the pathname being returned resolved to the REPLACEMENT.
    # Both probes passed and the returned string named a stranger.
    #
    # The order that closes it is the one where the last thing established is
    # the thing the return value asserts: check the member through the pinned
    # descriptor, then bind the pathname to that same descriptor, and return
    # only if the binding still holds. A rename after that final check makes
    # the answer stale, which no ordering can prevent — but it can no longer
    # make it WRONG-AT-THE-TIME, which is what a two-probe inversion did.
    try:
        st = os.stat(os.path.basename(path), dir_fd=pinned_fd,
                     follow_symlinks=False)
    except OSError:
        return None
    # WHAT EACH REPORT CLAIMS IS NOT THE SAME CLAIM. `preserved` names BYTES
    # ("the ones helm holds are here"), so only an inode match can stand
    # behind it. `occupied` names a PATH ("a writer holds this"), and that is
    # true of whatever object is there — demanding an inode we never held
    # would strip a TRUE statement. ANY_MEMBER says existence is the whole
    # claim; None says the claim needs an identity we do not have, and an
    # unrepresentable precise path is dropped rather than guessed.
    if expect is not ANY_MEMBER:
        if expect is None:
            return None
        if (st.st_dev, st.st_ino) != tuple(expect):
            return None
    # THE BINDING, LAST. Everything above was asked of the descriptor; this is
    # the only question about the PATHNAME, and it is what the returned string
    # actually claims.
    try:
        par = os.stat(os.path.dirname(path) or ".", follow_symlinks=False)
        pin = os.fstat(pinned_fd)
    except OSError:
        return None
    if (par.st_dev, par.st_ino) != (pin.st_dev, pin.st_ino):
        return None
    return path


def _absent(dir_fd, name):
    """Is `name` DEFINITELY not there, asked without following a link?

    The distinction this exists for: a failed open can mean absent, or can
    mean present-and-unopenable, and treating the second as the first is how
    a completed removal gets resurrected."""
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        return False
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _open_member(dir_fd, name, flags=None):
    """Open a member of a PINNED directory, O_NOFOLLOW, or None.

    ONE RESOLUTION, AND EVERYTHING AFTER IT COMES FROM THE DESCRIPTOR. The
    point is not that O_NOFOLLOW refuses a link — it is that the name is
    resolved exactly ONCE and every later question (is it regular, what are
    its bytes, what does it parse to) is asked of the fd rather than of the
    name again. lstat-then-open is two resolutions with a window between them,
    which is the check-then-act shape this module keeps removing; I
    reintroduced it inside the commit that claimed to close the symlink hole."""
    if flags is None:
        flags = os.O_RDONLY
    try:
        return os.open(name, openflags.flags(flags, "O_NOFOLLOW"),
                       dir_fd=dir_fd)
    except OSError:
        return None


def _regular_fd(fd):
    """Is this OPEN descriptor an ordinary file? Asked of the fd, so there is
    no second resolution to race."""
    try:
        return statmod.S_ISREG(os.fstat(fd).st_mode)
    except OSError:
        return False


def _sha256_fd(fd):
    """The digest of an OPEN descriptor, read from position 0."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        h = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _json_fd(fd):
    """Parse an OPEN descriptor as JSON, or None. Same descriptor the
    regularity and digest questions were asked of."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = b""
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            raw += chunk
        return json.loads(raw.decode("utf-8")) if raw.strip() else None
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _dir_fd(path):
    """An O_DIRECTORY|O_NOFOLLOW descriptor on `path`, or None.

    O_NOFOLLOW because the point of anchoring to a descriptor is defeated if
    the anchor itself was reached through a link someone else controls."""
    try:
        return os.open(path, openflags.flags(
            os.O_RDONLY, "O_DIRECTORY", "O_NOFOLLOW"))
    except OSError:
        return None


def _makedirs_durable(path):
    """Create `path`, and make every directory entry THIS CALL creates durable.

    `os.makedirs` creates intermediates, so syncing the leaf's parent persists
    the leaf's entry and leaves every ancestor entry unsynced. MEASURED on a
    first-ever removal in a fresh home: SIX levels created (_global, .state,
    reflex-state, the session dir, promoted-trash, the claim dir) and one
    synced — a crash after the row was removed could lose the whole chain,
    with the row already gone from its own name (a review named the window;
    the count is what made a one-level fix insufficient).

    The missing ancestors are collected BEFORE the create and each one's
    PARENT is synced outermost-first, so an entry is durable before anything
    below it is relied on. When the tree already exists this syncs nothing,
    which is the ordinary case and must not pay for the first one."""
    # NO SYMLINK MAY STAND IN THIS CHAIN (finding 4). os.path.isdir
    # FOLLOWS links and os.makedirs walks the pathname, so a promoted-trash
    # symlinked at an external directory made a removal SUCCEED while
    # publishing its undo outside the namespace every recovery root is derived
    # from — the bytes are then unreachable by the only code that knows to
    # look for them, and the report says the removal completed.
    #
    # An existing component that is a link is a REFUSAL, not something to
    # resolve: this transaction derives its whole namespace from the sid, and
    # a link is a claim that the namespace is somewhere else, made by someone
    # who is not the sid.
    # CREATED THROUGH DESCRIPTORS, NOT VALIDATED THEN CREATED BY PATH
    # (item 5). A version that lstat'd every component and
    # then called os.makedirs on the PATHNAME — two resolutions with a window
    # between them, so replacing a validated ancestor with a symlink after the
    # last lstat made the create follow the new link and build the undo
    # outside the namespace. Their probe did exactly that and _makedirs_durable
    # returned success.
    #
    # lstat-then-path-create cannot deliver this property at any ordering: the
    # check and the act address the name twice. So the chain is WALKED with
    # O_NOFOLLOW|O_DIRECTORY opens from a root we hold, and each level is
    # created relative to its parent's DESCRIPTOR. A link substituted at any
    # point is refused by the open that would have followed it, because
    # O_NOFOLLOW is the check and the act in one operation.
    parts, p = [], path
    while True:
        head, tail = os.path.split(p)
        if not tail:
            break
        parts.append(tail)
        if head == p:
            break
        p = head
    parts.reverse()
    root = p if p else os.sep
    made = []
    try:
        # `root` is the filesystem root produced by the split above: it cannot
        # be a symlink or non-directory. These flags are kernel assertions only.
        cur = os.open(root, openflags.filesystem_root(os.O_RDONLY))
    except OSError as e:
        raise OSError(e.errno or errno.EACCES,
                      "the undo path's root %s could not be opened safely "
                      "(%s)" % (root, e))
    try:
        if parts:
            flags = openflags.flags(os.O_RDONLY, "O_DIRECTORY", "O_NOFOLLOW")
        for i, name in enumerate(parts):
            created = False
            try:
                os.mkdir(name, 0o700, dir_fd=cur)
                created = True
            except FileExistsError:
                pass
            except OSError:
                raise
            try:
                nxt = os.open(name, flags, dir_fd=cur)
            except OSError as e:
                # ELOOP here IS the symlink refusal: O_NOFOLLOW makes the
                # check and the act one operation, so there is no window.
                raise OSError(e.errno or errno.ELOOP,
                              "a symlink or non-directory stands in the undo "
                              "path at %s (%s), so the undo would be "
                              "published outside this session's namespace"
                              % (os.sep.join(parts[:i + 1]), e))
            if created:
                # THE PARENT'S ENTRY, synced outermost-first — an entry is
                # durable before anything beneath it is relied on.
                #
                # AND THE FAILURE IS LOUD. My descriptor-relative rewrite
                # wrapped this in `except OSError: pass`, which silently
                # undid a law this lane established several rounds ago: a
                # directory whose durability fails must CANCEL the removal,
                # because a row taken away on an undo the disk has not
                # committed to is the loss this whole leg prevents. The
                # undo-root arm caught it by going quiet — it reported "the
                # removal did not refuse" about a removal nothing had
                # interfered with.
                made.append(cur)
                os.fsync(cur)
            if cur is not None and cur not in made:
                os.close(cur)
            cur = nxt
    finally:
        for fd in made:
            try:
                os.close(fd)
            except OSError:
                pass
        if cur is not None:
            try:
                os.close(cur)
            except OSError:
                pass
    return


def _fsync_dir(d):
    """Sync a DIRECTORY ENTRY. Syncing a file does not sync the entry that
    names it, so a crash can lose the only record of a claim while its payload
    sits beside it."""
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mark_residual(why, held, qdir_fd):
    """Transition this claim's manifest to its TERMINAL state, in place and
    durably. -> True when the record is on disk.

    EVERY STEP GOES THROUGH THE LOCKED DESCRIPTOR WHEN THERE IS ONE
    (seam-4 addendum, naming a defect in the commit that claimed to close it).
    This used to reopen claim.json with open(path, "w") while the caller held
    a SEPARATE descriptor on it under flock. I had moved the PARSE onto the
    locked fd and left the WRITE going through the pathname — so the
    durability this function adds was anchored to a name the lock does not
    cover, and a replacement of that name between the lock and the write would
    have been invisible. Fixing half a seam and describing it as closed is
    worse than not touching it, because the comment then vouches for it.

    `held` is an O_RDWR file object on claim.json, already locked, and
    `qdir_fd` is the claim's own directory. Read, APPEND, fsync and read back
    all use them — nothing here truncates, so the previous complete record
    outlives every attempt to replace it. Both are REQUIRED by the signature — this used to
    say it "refuses" without them, which described a guard that has since
    become unreachable, and a sentence promising a check that cannot run is
    worse than no sentence at all."""
    if held is None:
        # NO LOCKED HANDLE, NO RECORD. The terminal state is written THROUGH
        # the locked manifest — that is what makes it exclusive — so without
        # one there is nothing to write it on, and the honest answer is
        # "not recorded" rather than a crash. The old body reached
        # held.seek(0) inside an OSError guard, which an AttributeError walks
        # straight past.
        return False
    try:
        meta = _read_manifest(held)
    except (OSError, ManifestCorrupt):
        # COMMITTED CORRUPTION IS NOT A BLANK SLATE. Writing a fresh terminal
        # snapshot on top of an unreadable one would bury the evidence under a
        # decision nobody can check.
        return False
    if not isinstance(meta, dict):
        return False
    meta = dict(meta)
    meta["state"] = RESIDUAL
    meta["residual_why"] = str(why)
    # A FULL SNAPSHOT, APPENDED. The previous complete record survives until
    # this one is complete, so a crash anywhere in here leaves a claim that is
    # still readable and still decidable — which is the whole point of the
    # format (architecture result C). _append_manifest fsyncs the
    # file, reads the record BACK through this same descriptor, and confirms
    # the name still leads to this inode; its None means a reader of the
    # locked object would now find the terminal state.
    return _append_manifest(held, meta, qdir_fd) is None


def _discard_fresh_claim(qdir):
    """ABANDON a claim directory this call minted and never used. Removes
    NOTHING — the name is kept for recovery to dispose of under a lock.

    PRE-ANCHOR ONLY, and it NEVER UNLINKS A MEMBER. Emptying
    the directory first is indefensible once anything can appear
    inside: the callers run before descriptors exist, so the name is all we
    have, and unlinking members of a name that may have been reused deletes
    somebody else's contents. An rmdir on an empty directory is safe because
    it FAILS when the directory is not empty — the kernel does the check that
    we cannot do ourselves.

    So if anything is in there, it stays, and the directory is left for
    recovery to decide. A leftover directory is a thing an operator can read;
    a deleted replacement is not."""
    # IT DOES NOT DELETE ANY MORE, and the reasoning is the ruling that
    # already took delete-by-name out of _sweep_claim — it just was not
    # carried back here, because this path is "pre-anchor" and that felt like
    # a different situation. It is not (finding 2). Pre-anchor only
    # means WE have not opened it yet; it says nothing about whether the NAME
    # still leads to the directory we made. Rename the parent, install a
    # same-named empty replacement, and this rmdir removed the stranger's.
    #
    # POSIX has no conditional rmdir-by-inode, so there is no version of this
    # that checks harder. An empty directory we minted and abandoned is a
    # tidiness cost; recovery meets it, decides `empty`, and disposes of it
    # under a held lock — the path that already exists for a claim interrupted
    # one instruction later. Bytes over cleanup, pre-anchor included.
    return


def _sweep_claim(qdir, why, held, pdir_fd, qdir_fd):
    """TERMINALIZE the pinned claim. -> RESIDUAL | UNRECORDED. Never removes.

    IT NO LONGER DELETES ANYTHING (a structural review ruling). Once a claim
    directory has been anchored there is no safe way to delete it BY NAME:
    POSIX offers no conditional rmdir-by-inode, so a stat-then-rmdir on
    parent+basename only narrows the window in which the name can have been
    renamed away and its basename reused — it cannot close it. The previous
    version listed the PINNED directory and then removed by NAME, so a
    rename-and-reuse between those two steps deleted a REPLACEMENT claim's
    contents while the original kept its bytes at a path nothing reported.

    So the claim is retained and its manifest records why. That is not a
    concession: with the publish preserving the payload, a retained claim was
    already the ordinary outcome of every removal, and cleanup was the only
    remaining reason to touch a name after anchoring it. Bytes over cleanup.

    Disposal belongs to the retention surface and, ultimately, to an operator
    — which is where the retention ruling put it."""
    return RESIDUAL if _mark_residual(why, held, qdir_fd) else UNRECORDED


def _claim_lock(qdir_fd):
    """An OPEN, LOCKED handle on this claim, or None when someone holds it.

    TWO THINGS THIS DELIBERATELY DOES NOT DO. It never
    CREATES the metadata: the previous version opened with append mode, so
    merely ASKING whether a claim was live minted an empty claim.json and made
    a metadata-less claim permanently unreadable — the probe manufactured the
    condition it was reporting. And it does not release: the caller HOLDS this
    handle for the whole recovery, because a lock that is dropped between the
    check and the act is the check-then-act shape this module keeps removing.

    -> (handle, why). A non-None HANDLE IS the acquired state, so success
    carries no label: `why` is None there, and on failure says which case
    this is — "held" (another sweep owns it: expected, quiet, not ours to
    touch) or "unlockable" (we could not open or test the lock at all).

    The contract used to name a third value, "taken", on the success path.
    No caller read it — the sole consumer inspects `why` only when the handle
    is None — so a mutation flipping it to "held" left every control green.
    A value that cannot be observed cannot be tested, and the cure is to not
    have it rather than to write an arm that pins a label to nothing.

    ITS OWN PREVIOUS LINE NAMED ALL THREE AND RETURNED ONE VALUE FOR THEM:
    "None when the claim is held, unlockable, or has no metadata to lock."
    That is the cannot-tell collapse admitting itself in prose (its
    sixth instance in this lane). A claim genuinely held by a live sweep is
    skipped silently and correctly; a claim we could not even ask about must
    be REPORTED, because unresolved-and-unreported is the state this whole
    leg exists to prevent."""
    try:
        # O_RDWR | O_NOFOLLOW: the holder of this lock is the one that
        # writes the terminal state through it (seam-4 addendum),
        # and O_NOFOLLOW refuses a claim.json that has become a symlink —
        # a dir_fd bounds WHICH directory resolves, it does not make a
        # symlinked member valid.
        # A MEMBER OF THE PINNED CLAIM. The descriptor is REQUIRED by the
        # signature, so there is no capless shape to guard against — the
        # None-branch that used to sit here was unreachable the moment every
        # caller passed an anchor, and an unreachable guard is a second API
        # shape pretending to be a safety net.
        # O_APPEND, matching the creator: every write to this manifest is an
        # append of a full snapshot, and O_APPEND makes that true regardless
        # of where any reader left the offset (result C).
        fd = os.open(CLAIM_META,
                     openflags.flags(os.O_RDWR | os.O_APPEND, "O_NOFOLLOW"),
                     dir_fd=qdir_fd)
    except OSError:
        return None, "unlockable"
    try:
        # UNBUFFERED BINARY, matching the creator: a buffered text handle
        # would let a frame sit in user space across the fsync meant to
        # commit it, and would decode bytes the framing owns.
        fh = os.fdopen(fd, "rb+", buffering=0)
    except BaseException:
        os.close(fd)               # unowned until the wrapper exists
        # AND IT DOES NOT PROPAGATE. recover_claims promises it never raises —
        # a failing pass must not take the sweep with it — and this raised
        # straight through it (a probe arm found the leak; the escape came
        # with it). A claim that cannot be locked is one we do not act on,
        # which is already this function's contract for every other reason it
        # can fail.
        return None, "unlockable"
    # THE LOCK GOES ON THE CLAIM DIRECTORY, NOT ON A MEMBER OF IT
    # (finding 14). flock serializes access to the inode it is taken on, and
    # any MEMBER can be replaced — so a byte-copy of claim.json at the same
    # name hands a second actor a different inode to lock, both locks succeed,
    # and both actors proceed believing they hold the claim. Binding the held
    # inode to the name afterwards only tells the loser what happened.
    #
    # The claim's identity IS the directory: it cannot be swapped at its name
    # without the pinned parent changing, and the parent is held. A fresh
    # descriptor on that directory is taken through the pin, so two actors get
    # independent descriptors on the SAME inode and flock actually conflicts —
    # measured, both directions.
    lockfd, lock_why = _lock_claim_dir(qdir_fd)
    if lockfd is None:
        fh.close()
        return None, lock_why
    # ONE OBJECT OWNS BOTH, so the lock's lifetime cannot drift from the
    # handle's. Stapling the descriptor onto the file object as an attribute
    # happens to work on this build and would leak it on close, which is the
    # same class of fragility as assigning to fh.write.
    fh = _ClaimHandle(fh, lockfd)
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None, "held"    # genuinely held by a running sweep
    except OSError:
        # AGE IS NOT EXCLUSION (production refuter). The lock could
        # not be TESTED — a filesystem without flock — and this used the
        # claim's mtime to decide it was abandoned, then fell through and
        # returned the handle WITH NO LOCK HELD. Two recoveries meeting the
        # same stale claim both pass that test, both get a handle, and both
        # act. An hour of quiet is evidence about the PAST; it grants no
        # exclusion for the next instruction, and the whole point of this
        # handle is that its holder may mutate the claim.
        #
        # The honest answer is the one this module keeps arriving at: we could
        # not ask, so we do not act, and we SAY SO. `unlockable` is reported
        # rather than skipped, so a row on a lock-less mount is visible and
        # an operator can act on it — it is not silently stranded, which was
        # the real fear behind the age backstop. Restoring automatic recovery
        # there needs an exclusion primitive that works without flock (an
        # O_EXCL member would do it); age cannot stand in for one.
        fh.close()
        return None, "unlockable"
    # THE HANDLE IS THE ACQUIRED STATE; THE REASON EXISTS ONLY ON FAILURE.
    # A success label nobody reads is a value a mutation can collapse for
    # free — flipping "taken" to "held" left every control green, because the
    # sole consumer only inspects `why` when the handle is None (row
    # 111). Removing the value removes the collapse: there is no success
    # string left to be wrong about.
    # THE BINDING IS ESTABLISHED HERE, BEFORE THE HANDLE IS HANDED OUT
    # (findings 14/15). A caller that receives this handle proceeds to mutate
    # on the strength of it, so every precondition it relies on has to hold
    # BEFORE it leaves this function — checking afterwards only tells the
    # loser of a race what happened.
    why = _private_record(fh, qdir_fd)
    if why is not None:
        fh.close()
        return None, "unlockable"
    return fh, None


def recover_claims(sid, apply=True):
    """Finish or undo the claims a CRASHED sweep left behind -> [outcome dict].

    A claim is a removal in flight: the row is no longer at its own pathname
    and not yet in the undo. Kill the process there and the only copy sits in
    a hidden `.claiming-` directory `personal_rows` never descends into — the
    bytes survive and the row is INVISIBLE to every surface, the same
    silently-absent outcome the identity guard exists to prevent, arriving
    through a crash instead of a race (measured).

    COMPLETION IS BOUND TO CONTENT, NOT TO A PATH. The undo path is reusable
    across transactions, so "the undo holds a row with this identity" cannot
    mean "the undo holds THIS row": their adversarial state — a stale undo
    carrying identity A, a claimed payload B, metadata naming A — made an
    earlier version call it completed and sweep B, which is data loss through
    the recovery written to prevent data loss. A claim is completed only when
    the undo holds the payload's own DIGEST, recorded while the claim was
    owned and unable to change.

    `apply=False` DECIDES WITHOUT TOUCHING ANYTHING. Recovery used to run
    unconditionally, so `--dry-run` mutated the disk — the worst defect in
    that commit, shipped inside a feature about not losing rows.

    Never raises: a recovery pass that fails must not take the sweep with it."""
    from . import record
    out = []
    d = personal_dir(sid)
    gap = _capability_gap()
    if gap:
        return [{"claim": d, "outcome": "refused", "why": gap}]
    fd = _dir_fd(d)
    if fd is None:
        # UNAVAILABLE IS NOT EMPTY (item 6). Returning [] says "this
        # session has no crashed claims", which is a POSITIVE finding about
        # the disk — and it was returned for "I could not look". A caller
        # cannot tell those apart, and the whole point of recovery is that a
        # claim nobody can see is the failure mode.
        try:
            os.stat(d)
        except FileNotFoundError:
            return []                  # genuinely nothing: no directory yet
        except OSError as e:
            return [{"claim": d, "outcome": "unreadable",
                     "preserved_unknown": True,
                     "why": "this session's task directory could not be "
                            "examined (%s), so whether it holds crashed "
                            "claims cannot be decided" % e}]
        return [{"claim": d, "outcome": "unreadable",
                 "preserved_unknown": True,
                 "why": "this session's task directory could not be opened "
                        "safely, so whether it holds crashed claims cannot "
                        "be decided"}]
    try:
        return _recover_claims_at(sid, apply, d, fd)
    finally:
        os.close(fd)


def _recover_claims_at(sid, apply, d, pdir_fd):
    """Recovery, with the personal directory already pinned by the caller.

    A DESCRIPTOR-OWNING WRAPPER LIVES ABOVE THIS (capability result
    A). A sweep that recovers, enumerates and removes must do all three
    through the SAME inode, and it could not while each phase resolved
    `personal_dir(sid)` for itself."""
    out = []
    from . import record
    # THE PREFLIGHT LIVES IN THE WRAPPER, so a caller that already holds a
    # capability is not asked about capabilities twice.
    # THE OWNER OPENS EVERYTHING AND CLOSES EVERYTHING (seam 4).
    # This whole scan was path-based: listdir on a name, isdir following a
    # link, _claim_lock re-opening by path, and every later question resolving
    # again. Recovery reads directories it did NOT create, so it is the side
    # where a replaced name matters most — and it was the side with no pinning
    # at all.
    # NOT REOPENED. The caller pinned it and owns it; taking a second
    # descriptor here would be a capability the callee minted for itself and
    # would defeat the whole point of threading one (capability result A).
    # THE TRASH ROOT IS THE SCANNER'S TO OWN. _recover_one opened
    # and closed it per claim, which is a capability the callee minted for
    # itself — the same lifetime problem as _sweep_claim opening its own, and
    # it re-resolved the root on every iteration.
    _troot = os.path.join(record.session_dir(sid), TRASH)
    troot_fd = _dir_fd(_troot)
    # THE ROOT'S ABSENCE IS NOT EVIDENCE (row 86). A `troot_absent` derived
    # here and allowed to authorise a restore failed in two forms —
    # first with os.path.exists, which FOLLOWS symlinks and read a dangling
    # link as absent, and then with lstat, which was honest about the name
    # and still answered the wrong QUESTION. A trash root can be MOVED, and
    # then "absent" and "the undo is somewhere else" are the same
    # observation. Only an ENOENT on the MEMBER, beneath an undo directory
    # held open, proves an undo does not exist; every coarser absence is
    # UNDECIDABLE. Fixing the symlink hole in that check left the check
    # itself unjustified, which is what row 86 is about.
    try:
        try:
            names = sorted(os.listdir(pdir_fd))
        except OSError as e:
            # SAME RULE ONE LEVEL IN. This collapsed any enumeration failure
            # into the empty result already accumulated, so a directory that
            # raised EIO reported exactly what a clean empty one reports.
            out.append({"claim": d, "outcome": "unreadable",
                        "preserved_unknown": True,
                        "why": "the claims in this session could not be "
                               "enumerated (%s), so whether any are "
                               "outstanding cannot be decided" % e})
            return out
        for n in names:
            if not n.startswith(CLAIM_PREFIX):
                continue
            qdir = os.path.join(d, n)
            # OPENED, NOT TESTED. `isdir(qdir)` follows links and then the
            # code opened the name again anyway; O_DIRECTORY|O_NOFOLLOW makes
            # "is it a directory we may descend into" and "give me that
            # directory" the SAME act.
            # _adopted_shape below proves the descriptor is a directory before
            # any member operation; O_DIRECTORY is an optional early refusal.
            qdir_fd = _open_member(
                pdir_fd, n, openflags.verified_directory(os.O_RDONLY))
            if qdir_fd is None:
                # A CLAIM WE CANNOT OPEN IS INVISIBLE EVERYWHERE ELSE
                # (production refuter). `personal_rows` ignores
                # claim directories by design, so a crashed claim that has
                # been moved, replaced or made unopenable appears in no
                # surface at all — the bare `continue` was the last place it
                # could have been mentioned, and it said nothing. That is the
                # silently-absent row this leg exists to prevent, reached by a
                # different road.
                #
                # No location is named because none can be verified; the claim
                # ID is what an operator can act on.
                # THE PATH GOES THROUGH THE DOOR LIKE EVERY OTHER ONE
                # (item 7). This built `qdir` from the enumerated
                # pathname and returned it directly, so a personal directory
                # renamed and its name reused made the report resolve to a
                # REPLACEMENT claim — the exact stale-name defect the door
                # exists for, in the one branch that skipped it. The claim ID
                # is stable and always reported; the PATH is named only when
                # the pinned parent still owns it.
                out.append({"claim": _reported_row_path(qdir, pdir_fd,
                                                        gone_ok=True),
                            "claim_unknown": _reported_row_path(
                                qdir, pdir_fd, gone_ok=True) is None,
                            "claim_id": n,
                            "outcome": "unreadable",
                            "preserved_unknown": True,
                            "why": "this claim directory could not be opened, "
                                   "so what it holds cannot be decided or "
                                   "located"})
                continue
            try:
                lock, why = _claim_lock(qdir_fd)
                if lock is None:
                    # HELD IS SILENCE; UNLOCKABLE IS A REPORT. A claim another
                    # sweep owns is expected and none of our business, so it
                    # is skipped without a word. A claim we could not open or
                    # test is UNRESOLVED, and an unresolved claim nobody
                    # mentions is exactly the silently-absent row this leg
                    # exists to prevent. NOTHING is created to find out —
                    # see `_claim_lock`.
                    if why != "held":
                        out.append({"claim": qdir, "outcome": "unreadable",
                                    "why": "this claim could not be locked or "
                                           "tested, so whether it is live "
                                           "cannot be decided"})
                    continue
                try:
                    # NOT VERIFIED HERE ANY MORE — _recover_one does it while
                    # it still holds the payload descriptor, which is the only
                    # window in which an unverifiable location can be rescued
                    # rather than merely reported.
                    out.append(_recover_one(sid, qdir, apply, lock,
                                            pdir_fd, qdir_fd, troot_fd))
                finally:
                    lock.close()
            finally:
                os.close(qdir_fd)
    finally:
        # PDIR IS NOT CLOSED HERE. The caller pinned it and owns it; closing
        # a BORROWED capability is the exact-twice defect this lane cured one
        # function over, and it produced EBADF on the first run after the
        # split (capability result A).
        if troot_fd is not None:
            os.close(troot_fd)
    return [o for o in out if o]


def _is_plain_name(name):
    """A single path COMPONENT, and nothing that can leave its directory.

    `os.path.basename` is not this check: it is a transform, and a caller who
    reaches for it has already accepted whatever the input was. This REFUSES,
    which is the difference between bounding a hostile value and quietly
    rewriting it."""
    return bool(name) and name not in (".", "..") and os.sep not in name \
        and (os.altsep or os.sep) not in name and "\0" not in name


def _preserved_outcome(qdir, outcome, path, pinned_fd, why, row_name,
                       member_id=None):
    """An outcome whose `preserved` is VERIFIED, or which says it cannot be.

    The path is emitted only while its parent still is the pinned directory;
    otherwise the claim is identified by id and inode and the location is
    reported as unknown, because a precise wrong path is worse than an honest
    absence of one."""
    ok = _verified_path(path, pinned_fd, member_id)
    out = {"claim": qdir, "outcome": outcome, "row": row_name, "why": why}
    if ok:
        out["preserved"] = ok
    else:
        out["preserved"] = None
        out["preserved_unknown"] = True
        try:
            st = os.fstat(pinned_fd)
            out["claim_inode"] = [st.st_dev, st.st_ino]
        except (OSError, TypeError):
            pass
        out["why"] = (why + " — and its location cannot be reported: the "
                      "claim's name no longer resolves to the directory this "
                      "decision was made in")
    return out


def _bound_row(pdir_fd, row_name, payload_fd):
    """Is the entry at `row_name` the very inode `payload_fd` holds?
    True / False (a different object is there) / None (cannot tell)."""
    try:
        a = os.fstat(payload_fd)
        b = os.stat(row_name, dir_fd=pdir_fd, follow_symlinks=False)
    except OSError:
        return None
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _restored_outcome(qdir, row_name, row, state, pdir_fd, payload_fd,
                      base="restored"):
    """One shape for every restore report, so the three sweep states cannot be
    collapsed by a caller writing its own conditional.

    SWEPT is done. RESIDUAL is terminal and the payload is the retained backup.
    UNRECORDED is NEITHER — the claim is still there and nothing on disk says
    why, so it must read as retryable rather than as a residual an operator
    can dispose of.

    THE INODE IS BOUND HERE, AFTER THE MUTATION, FOR EVERY BRANCH
    (rows 102-103). Both restore branches — the fresh `_link_fd` publish and
    the already-restored shortcut — proved their identity BEFORE `_sweep_claim`
    and then reported `restored` on the strength of that stale proof. A
    concurrent rename between the proof and the terminalization installs a
    different inode carrying the SAME logical id and stamp, and the only check
    left downstream compares logical identity, so it passes: the report says
    the row is back while the live path names a stranger and the restored
    inode is elsewhere. Curing only the branch a probe happened to hit would
    leave the other one live, so the binding is taken at the one door both
    branches already pass through.

    A mismatch is a REPORT fact, not a licence to touch the newcomer."""
    bound = _bound_row(pdir_fd, row_name, payload_fd)
    if bound is False:
        base = "restored-replaced"
    elif bound is None:
        base = "restored-unverified"
    if state == SWEPT:
        return {"claim": qdir, "outcome": base, "row": row}
    # PENDING IS ITS OWN SUFFIX. An else-branch that means UNRECORDED would
    # label a dry run's untouched claim "recorded no decision", which is a
    # statement about a write that was never attempted — and UNRECORDED is the
    # retryable, act-on-me state, so a read-only pass would have manufactured
    # work (refuter D).
    suffix = ("-" + PENDING if state == PENDING else
              "-" + RESIDUAL if state == RESIDUAL else "-" + UNRECORDED)
    return {"claim": qdir, "outcome": base + suffix, "row": row,
            "preserved": os.path.join(qdir, row_name),
            "_member_id": _member_id_of(payload_fd)}


def _member_id_of(fd):
    try:
        st = os.fstat(fd)
        return (st.st_dev, st.st_ino)
    except OSError:
        return None


def _recover_one(sid, qdir, apply, held, pdir_fd, qdir_fd, troot_fd):
    """One claim, decided under a HELD lock. -> outcome dict or None.

    THE SID IS THE AUTHORITY; THE MANIFEST IS THE SUSPECT (a review ruling,
    meld e:1786171017). Every root is DERIVED from the sid the scanner already
    holds, and the manifest is checked AGAINST that namespace rather than
    consulted for it. The previous version stored `original` and `undo` as
    absolute paths and used them directly, so a corrupt or hand-edited
    claim.json aimed this function anywhere on disk, and a moved HELM_HOME
    would have silently relocated an old claim instead of surfacing it. A
    persisted root is stale configuration wearing the costume of identity.

    A claim whose manifest names another session, another claim id, or a row
    that is not a plain component is REFUSED rather than repaired. That
    includes every claim written before this manifest shape: those are an
    explicit migration case, and inventing a resolution for them is exactly
    the manifest-controlled path resolution this refuses."""
    # (no `from . import record` here — the owner resolves no paths; the
    #  import moved with the code that used it, and a leftover import is
    #  a claim that this function still reaches for the session root)
    # PARSED FROM THE LOCKED DESCRIPTOR (seam 4). Reopening
    # claim.json by pathname here would decide on bytes the lock does not
    # cover — the lock and the manifest have to be one object or the lock
    # is decoration.
    corrupt = None
    try:
        meta = _read_manifest(held)
    except ManifestCorrupt as e:
        # A COMPLETED FRAME THAT DOES NOT PARSE IS A DIFFERENT FACT from a
        # torn tail, and it must not be skipped in favour of an older frame:
        # doing so would silently promote a superseded state — the exact
        # not-decidable-but-decided shape this module keeps removing. Fail
        # closed and say which it is (architecture result C).
        meta, corrupt = None, str(e)
    except OSError:
        meta = None
    if not isinstance(meta, dict):
        # A claim that cannot say what it held is not one this can act on, and
        # removing it would be the unprovable deletion the module refuses.
        return {"claim": qdir, "outcome": "unreadable",
                "why": corrupt or "this claim's manifest holds no complete "
                                  "record of what it was"}
    claim_id = os.path.basename(qdir)
    row_name = str(meta.get("row") or "")
    if not _is_plain_name(row_name):
        return {"claim": qdir, "outcome": "unreadable",
                "why": "the manifest does not name a row, or names one that "
                       "is not a plain filename (pre-namespace claim, or "
                       "tampered)"}
    for field, mine, theirs in (("claim", claim_id, str(meta.get("claim_id") or "")),
                                ("session", str(sid), str(meta.get("sid") or "")),
                                ("payload", row_name, str(meta.get("payload") or ""))):
        if mine != theirs:
            return {"claim": qdir, "outcome": "foreign", "field": field,
                    "expected": mine, "found": theirs}
    if meta.get("state") == RESIDUAL:
        # TERMINAL, AND RECOVERY IS IDEMPOTENT ON IT. A residual is a decision
        # already taken and durably recorded: its bytes are preserved in place
        # awaiting an operator. Re-deciding it every pass would either churn
        # the same claim forever or, worse, reach the disposal it exists to
        # avoid. It is REPORTED, never acted on.
        # THE RETAINED LOCATION IS EARNED, NOT ASSERTED. This is
        # the one report an operator acts on — it is where the bytes went —
        # so a path here must be verifiable, and the outer door will strip
        # any path that arrives without an identity to compare against. The
        # member is opened through the PINNED claim and its identity captured
        # from that descriptor, so the reported location is bound to the
        # object rather than composed from two strings.
        #
        # A member that is absent or cannot be opened yields no expectation,
        # and that becomes an explicit unknown — never a guessed path.
        rfd = _open_member(qdir_fd, row_name)
        out = {"claim": qdir, "outcome": RESIDUAL, "row": row_name,
               "preserved": os.path.join(qdir, row_name),
               "why": str(meta.get("residual_why") or "")}
        if rfd is None:
            out.pop("preserved")
            out["preserved_unknown"] = ("the retained payload could not be "
                                        "opened through its own claim")
            return out
        # THE DOOR RUNS HERE, WHILE rfd IS HELD (production
        # refuter). This captured an identity and then RETURNED — around the
        # very door the comment above it invokes — so a rename-and-reuse
        # emitted a stale `preserved`, and worse, the close then dropped the
        # last name of bytes nobody had rescued. This is the terminal report
        # an operator acts on; it is the last surface that should be exempt
        # from the rule.
        try:
            out["_member_id"] = _member_id_of(rfd)
            return _verify_reported_paths(out, pdir_fd, qdir_fd, rfd,
                                          row_name)
        finally:
            os.close(rfd)
    original = os.path.join(personal_dir(sid), row_name)
    payload = os.path.join(qdir, row_name)
    # ONE RESOLUTION, HELD OPEN FOR EVERY QUESTION BELOW. Existence,
    # regularity and the digest were three separate re-resolutions of the same
    # name, on a directory recovery did not create — so each was a fresh
    # chance for the name to mean something else, and the lstat-then-link pair
    # was outright TOCTOU.
    payload_fd = _open_member(qdir_fd, row_name)
    if payload_fd is None:
        # (the not-there / cannot-open decision lives in the body)
        pass
    try:
        # THE REPORT IS VERIFIED WHILE THE DESCRIPTOR IS STILL OURS (per
        # immutable refuter on 6b2a). The door used to run in the scanner
        # loop, one frame out — AFTER this function's finally had closed
        # payload_fd. A rename-OVER of the member in that interval takes the
        # held inode's LAST NAME, and the door then honestly reported the
        # location as unknown while the judged bytes ceased to exist at the
        # close: an open descriptor is the only thing keeping a nameless inode
        # alive. An honest report about destroyed data is still destroyed
        # data, and this leg exists to not lose bytes.
        #
        # Inside the lifetime the same discovery has a cure available: the
        # descriptor can still be given a name.
        return _verify_reported_paths(
            _recover_one_open(sid, qdir, apply, held, pdir_fd, qdir_fd,
                              troot_fd, meta, claim_id, row_name,
                              original, payload, payload_fd),
            pdir_fd, qdir_fd, payload_fd, row_name)
    finally:
        # ONE OWNER, ONE CLOSE. Every early return after the open — completed,
        # inconsistent, same-inode restored — left this descriptor open, so a
        # recovery pass over many claims exhausted the process's file
        # descriptors. A capability opened in a function must be
        # closed by that function, on every path out of it.
        if payload_fd is not None:
            try:
                os.close(payload_fd)
            except OSError:
                pass


def _recover_one_open(sid, qdir, apply, held, pdir_fd, qdir_fd, troot_fd,
                      meta, claim_id, row_name, original, payload,
                      payload_fd):
    """The decision, with the payload already open. Split out so exactly one
    place closes that descriptor — see the owner's finally above."""
    from . import record
    if payload_fd is None:
        # CANNOT-OPEN IS NOT NOT-THERE, and O_NOFOLLOW makes that distinction
        # load-bearing: it fails with ELOOP on a symlink, so reading None as
        # "the claim held no payload" would SWEEP a claim whose payload is a
        # planted link — destroying the thing the refusal exists to preserve.
        # That is the cannot-tell-becomes-nothing fail-open this module keeps
        # curing, and I reintroduced it inside the fix for the TOCTOU in this
        # very check. The name is asked about WITHOUT following, so something
        # sitting there is reported and preserved; only a genuinely absent
        # name is empty.
        there = True
        try:
            os.stat(row_name, dir_fd=qdir_fd, follow_symlinks=False)
        except FileNotFoundError:
            there = False
        except OSError:
            there = True
        if there:
            # NO DESCRIPTOR, SO NO EXACT IDENTITY — and that is the honest
            # answer, not a gap to paper over. This branch is
            # reached precisely because the payload could NOT be opened, so
            # there is nothing to compare a member against. Carrying an
            # expectation here was an UnboundLocalError waiting on the first
            # planted symlink; carrying a GUESSED one would be worse. The
            # absent key routes this through the unknown path, which says
            # "something is preserved, its exact location is unverified".
            return {"claim": qdir, "outcome": "unreadable",
                    "preserved": payload,
                    "why": "the claim's payload is not an ordinary file (a "
                           "link or a device sits at that name), so recovery "
                           "cannot act on it"}
        # AND THE SWEEP'S ANSWER IS NOT DISCARDED HERE EITHER. This called
        # _sweep_claim and threw the result away, then said `empty` — i.e.
        # "cleared" — even when the sweep RETAINED the claim or could not
        # record a decision at all. Since preserve-in-place, retaining is the
        # normal outcome, so the common case was being reported as the one
        # thing it was not.
        state = _sweep_claim(qdir, "the claim held no payload", held,
                             pdir_fd, qdir_fd) if apply else PENDING
        out = {"claim": qdir, "outcome": "empty", "state": state}
        if state != SWEPT:
            out["outcome"] = "empty-" + state
        return out
    # THE PAYLOAD MUST BE AN ORDINARY FILE, and this is the case a directory
    # descriptor cannot cover (seam 4). Recovery reads a claim
    # directory it did not create — a crashed sweep's, or a hand-edited one —
    # so a symlink planted at the payload name is inside the claim, reached by
    # the correct descriptor, and pointing anywhere. Restoring it would
    # publish a pointer into the operator's row list; hashing it would prove
    # completion against a target nobody claimed. Preserved and reported, not
    # acted on: the bytes are not ours to judge and not ours to destroy.
    # THE MEMBER'S IDENTITY, CAPTURED WHILE WE HOLD IT (a third pass
    # on this check). Every outcome reporting `preserved` claims THESE bytes
    # are at that name. The door could only check that SOMETHING was there:
    # rename 1.json inside the very same claim, drop a stranger's file at
    # 1.json, and the parent check passed, the member stat passed, and the
    # precise false path still emitted.
    try:
        _pst = os.fstat(payload_fd)
        payload_id = (_pst.st_dev, _pst.st_ino)
    except OSError:
        payload_id = None
    if not _regular_fd(payload_fd):
        # ASKED OF THE OPEN DESCRIPTOR. The name was resolved once, above, and
        # this is the same object every later step uses — no window between
        # deciding it is a file and acting on it.
        return _preserved_outcome(qdir, "unreadable", payload, qdir_fd,
                                  "the claim's payload is not an ordinary "
                                  "file (a link or a device sits at that "
                                  "name), so recovery cannot act on it",
                                  row_name, payload_id)
    want = str(meta.get("payload_sha256") or "")
    want_id = tuple(str(x) for x in (meta.get("identity") or ()))
    got = _sha256_fd(payload_fd)
    # THE DESCRIPTOR OUTLIVES THE DIGEST — the restore below publishes it, and
    # closing it here to link the payload's name is the same re-resolution one
    # function over. It is closed by the OWNER, on every path out.
    if got is None:
        return {"claim": qdir, "outcome": "unreadable", "preserved": payload,
                "_member_id": payload_id}
    undo = os.path.join(record.session_dir(sid), TRASH, claim_id, row_name)
    # `and undo` used to guard this: the undo was a manifest FIELD and could be
    # missing. It is now DERIVED and always a string, so that clause could only
    # ever be true — the same orphaned-condition class as the CLAIM_STALE_S
    # backstop that survived its only reader earlier in this lane. Dropped
    # rather than left reading as a live check.
    # THE UNDO IS A MEMBER OF THIS CLAIM'S OWN UNDO DIRECTORY, opened
    # O_DIRECTORY|O_NOFOLLOW from the trash root and then the member from
    # THAT — so `completed` is proven against the bound object rather than
    # against whatever currently answers to a re-resolved path.
    # CANNOT-READ-THE-UNDO IS NOT NO-UNDO (REPRODUCED). Every one of
    # these opens returned None into the same `undo_digest = None`, which the
    # completed test then read as "the undo does not match" — so a claim whose
    # removal HAD completed fell through to the restore branch and the row
    # came back. A resurrection caused by a descriptor that could not be
    # opened. That is the fourth time in this lane that cannot-tell silently
    # became nothing, and this one arrives through the cure for the first
    # three.
    #
    # So the three answers are kept apart: the digest, a definite ABSENCE, and
    # an inability to look. Only a definite absence may be treated as "no
    # undo"; not being able to look REFUSES and leaves the claim standing.
    # THE STATE MACHINE (row 86), and it is deliberately narrow:
    #   trash root missing / moved / symlinked / unopenable  -> UNDECIDABLE
    #   this claim's undo dir missing / moved / symlinked / unopenable -> UNDECIDABLE
    #   member ENOENT beneath an ALREADY-OPEN pinned undo dir -> no undo, restore
    # Only the last one PROVES anything. Every coarser absence is equally
    # consistent with the undo having been moved out from under us, and a
    # restore on that evidence resurrects a completed removal.
    undo_digest = None
    undo_unknown = False
    if troot_fd is None:
        undo_unknown = True
    else:
        # The first dir-relative member operation below refuses a non-directory;
        # O_DIRECTORY only moves that same refusal earlier when available.
        udir_fd = _open_member(
            troot_fd, claim_id,
            openflags.verified_directory(os.O_RDONLY))
        if udir_fd is None:
            undo_unknown = True
        else:
            try:
                ufd = _open_member(udir_fd, row_name)
                if ufd is None:
                    # the ONLY absence that proves anything: this member is
                    # not present in a directory we are holding open.
                    undo_unknown = not _absent(udir_fd, row_name)
                else:
                    try:
                        if _regular_fd(ufd):
                            undo_digest = _sha256_fd(ufd)
                            undo_unknown = undo_digest is None
                        else:
                            undo_unknown = True
                    finally:
                        os.close(ufd)
            finally:
                os.close(udir_fd)      # ours; the ROOT is borrowed and stays
    if undo_unknown:
        return {"claim": qdir, "outcome": "undecidable",
                "row": row_name, "preserved": payload,
                "_member_id": payload_id,
                "why": "this claim's undo could not be read, so whether the "
                       "removal completed cannot be decided — the claim is "
                       "left standing rather than guessed either way"}
    if want and want == got and undo_digest == want:
        # COMPLETED DURABLY, proven by the payload's own bytes rather than by a
        # name. Restoring here would resurrect a row already removed.
        #
        # BUT THE PAYLOAD IS STILL A MUTABLE INODE, AND RECOVERY WAS UNLINKING
        # IT (measured). A writer holding an fd from before
        # the claim can write between this digest match and the unlink: the
        # sweep then reports completed, DELETES those bytes, and leaves the undo
        # holding the OLD ones. That is snapshot-then-delete — the exact shape
        # round 7 outlawed in the live path — and recovery kept doing it while
        # `_trash` was cured. A green suite proved nothing about this; only
        # their probe did.
        #
        # So the inode is never unlinked. It is MOVED beside the undo, where a
        # late write lands in a file that still exists and is named; when it
        # cannot be moved it is LEFT WHERE IT IS. Both answers preserve bytes;
        # only unlinking destroys them.
        #
        # AND THERE IS NOTHING TO RETIRE (a review ruling, meld
        # e:1786171017). `_retire_payload` is DELETED, not converted. It only
        # existed because the undo used to be a MOVE, which left the payload
        # as a leftover duplicate wanting tidy-up; round 8 made that tidy-up
        # non-destructive by linking it to a `.retired-` name instead of
        # unlinking it. Under link-and-preserve the payload is not a leftover
        # at all: same-filesystem it is one of two owned names for the very
        # inode the undo IS, and cross-filesystem it is the ONLY inode that
        # can still receive a late write through a descriptor opened before
        # the claim. In neither case is it disposable clutter, and the last
        # link-then-unlink handoff in this module goes with it.
        #
        # So the completed branch records the terminal state and stops.
        if apply:
            why = ("the removal completed durably; the payload is preserved "
                   "in place as this claim's retained backup")
            if not _mark_residual(why, held, qdir_fd):
                return {"claim": qdir, "outcome": "completed-unrecorded",
                        "row": row_name, "preserved": payload,
                "_member_id": payload_id,
                        "why": "the removal completed durably, but its residual "
                               "record could not be written, so recovery will "
                               "meet this claim again"}
            return {"claim": qdir, "outcome": "completed-residual",
                    "row": row_name, "preserved": payload,
                "_member_id": payload_id, "why": why}
        return {"claim": qdir, "outcome": "completed", "row": row_name}
    if want and want != got:
        # The metadata and the payload disagree, so this claim cannot be
        # decided at all. Preserve and report; never sweep an unexplained
        # payload (D5).
        return {"claim": qdir, "outcome": "inconsistent", "preserved": payload,
                "_member_id": payload_id}
    try:
        # SAMENESS IS AN INODE QUESTION, so it is asked of the descriptor we
        # hold and of the row's entry in the PINNED parent — samefile takes
        # two paths and resolves both again.
        try:
            a = os.fstat(payload_fd)
            b = os.stat(row_name, dir_fd=pdir_fd, follow_symlinks=False)
            same = (a.st_ino, a.st_dev) == (b.st_ino, b.st_dev)
        except OSError:
            same = False
    except OSError:
        same = False
    if same:
        # A CRASH BETWEEN THE RESTORE HARDLINK AND THE CLEANUP. The row is
        # already back and these are ONE INODE, so this is an idempotent
        # completion of the restore, not a contest with a newcomer — calling
        # it contested would report a conflict that does not exist.
        # A DRY RUN MUST NOT SYNTHESISE A STATE IT DID NOT ACHIEVE
        # (production refuter D). `else SWEPT` made a read-only pass report
        # plain `restored`, which reads as "done, claim gone" — and hid the
        # claim that is still standing, the one thing a dry run exists to show
        # you. PENDING is the truth: the row is back, nothing has been
        # terminalized, and the claim is still there to be dealt with.
        state = _sweep_claim(qdir, "the row was restored from this claim",
                             held, pdir_fd, qdir_fd) if apply else PENDING
        # ALL THREE STATES REACH THE REPORT (cumulative refute).
        # This compared to SWEPT and kept a BOOLEAN, which collapsed RESIDUAL
        # and UNRECORDED into one "not swept" — so a claim whose terminal
        # state was never written was reported as `restored-residual`, i.e.
        # terminal. UNRECORDED is the retryable one; calling it terminal is
        # how a claim nobody will revisit gets created.
        return _restored_outcome(qdir, row_name, os.path.basename(original),
                                 state, pdir_fd, payload_fd)
    if not apply:
        occupied = True
        try:
            os.stat(row_name, dir_fd=pdir_fd, follow_symlinks=False)
        except FileNotFoundError:
            occupied = False
        except OSError:
            occupied = True
        return {"claim": qdir, "outcome": ("contested" if occupied
                                           else "restorable"),
                "preserved": payload,
                "_member_id": payload_id, "occupied": original}
    link_err = _link_fd(payload_fd, row_name, pdir_fd)
    try:
        if link_err is not None:
            raise link_err
    except FileExistsError:
        # A writer took the path while the claim was orphaned. Both kept, both
        # named: losing a row is what this leg prevents, and that covers the
        # newcomer.
        return {"claim": qdir, "outcome": "contested",
                "preserved": payload,
                "_member_id": payload_id, "occupied": original}
    except OSError as e:
        return {"claim": qdir, "outcome": "unrecoverable",
                "preserved": payload,
                "_member_id": payload_id, "why": str(e)}
    try:
        os.fsync(pdir_fd)          # THE PINNED PARENT, not its path
    except OSError as e:
        # SWALLOWED, THEN SWEPT AND RECORDED (cumulative refute).
        # The restore is not durable, so terminalizing the claim on top of it
        # writes a decision the disk has not made — and the claim is the only
        # thing that would bring the row back after a crash. Report and leave
        # it standing.
        return {"claim": qdir, "outcome": "restored-undurable",
                "row": os.path.basename(original), "preserved": payload,
                "_member_id": payload_id,
                "why": "the row was put back but that could not be made "
                       "durable (%s), so this claim is left standing" % e}
    state = _sweep_claim(qdir, "the row was restored from this claim", held,
                         pdir_fd, qdir_fd)
    # WHAT CAME BACK IS NAMED, NOT ASSUMED (a successor blocker). The
    # metadata's identity is the row the sweep DECIDED about, read before the
    # claim; the payload is what was actually at that pathname when the claim
    # took it, and an already-open writer can have mutated that inode in
    # between. Restoring the bytes is right either way — they belong at that
    # path — but reporting them as the recorded row when they are not is the
    # report lying about the disk, which is the defect this leg keeps curing.
    # READ FROM A PINNED MEMBER, not by re-resolving the row's path after the
    # link that put it there.
    have = None
    ofd = _open_member(pdir_fd, row_name)      # pdir_fd is required, not optional
    if ofd is not None:
        try:
            have = _identity(_json_fd(ofd))
        finally:
            os.close(ofd)
    # THE RESIDUAL TRAVELS ON BOTH OUTCOMES (step-3 refutation).
    # Whether the restored bytes matched the recorded identity and whether the
    # claim directory survives are INDEPENDENT facts, and the caller needs
    # both — reporting only the first is how a claim nobody owns goes unseen.
    if want_id and have != want_id:
        out = _restored_outcome(qdir, row_name, os.path.basename(original),
                                state, pdir_fd, payload_fd,
                                base="restored-unexpected")
        out["recorded"], out["found"] = list(want_id), list(have or ())
        return out
    return _restored_outcome(qdir, row_name, os.path.basename(original),
                             state, pdir_fd, payload_fd)


def _cmd_bridge(args):
    """`helm todos promote|demote`. Separate from cmd_todos's read path on
    purpose: these two WRITE, and the read verbs must stay unable to."""
    verb, rest = args[0], args[1:]
    as_json = "--json" in rest
    rest = [a for a in rest if a != "--json"]
    sid = this_session()
    if not sid:
        print("helm todos: no session in this environment — the personal "
              "list is session-scoped, so there is nothing to bridge",
              file=sys.stderr)
        return 2
    # THE SEAT NAME IS AN INPUT, NOT AN AMBIENT FACT — found by dogfooding
    # this on the seat that wrote it. `home.chat_name()` reads HELM_CHAT_NAME,
    # and a pane can be live and working with that variable absent from the
    # shell it hands to a subprocess (this session is one: its own resume-turn
    # notice says "declares no seat name"). promote then refused with no way
    # forward, and demote was WORSE — it silently skipped the
    # owned-by-another-seat rule and reported a clean sweep, because its
    # comparison needs a seat to compare against. A safety rule that
    # evaporates when an env var is missing is the failure mode this leg
    # exists to prevent, so both verbs now take --owner and demote REFUSES
    # rather than degrading.
    seat = None
    parsed = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a != "--owner":
            parsed.append(a)
            i += 1
            continue
        if seat is not None or i + 1 >= len(rest) or rest[i + 1].startswith("-"):
            print(_USAGE, file=sys.stderr)
            return 2
        seat = rest[i + 1]
        i += 2
    rest = parsed
    seat = str(seat or home.chat_name() or "").strip()
    if not seat:
        print("helm todos: cannot tell which seat this is — HELM_CHAT_NAME is "
              "unset in this shell. Pass --owner <seat>.\n"
              "  promote would file a row nobody is accountable to; demote "
              "would silently keep every row another seat owns, and report "
              "that as a clean list.", file=sys.stderr)
        return 2
    if verb == "promote":
        ids = [a for a in rest if not a.startswith("-")]
        if len(ids) != 1 or len(ids) != len(rest):
            print(_USAGE, file=sys.stderr)
            return 2
        row, err = promote(sid, ids[0], seat)
        if err and not row:
            print("helm todos: %s" % err, file=sys.stderr)
            return 1
        if as_json:
            print(json.dumps({"row": row, "warning": err}, indent=1,
                             ensure_ascii=False))
        else:
            print("helm todos: %s -> %s  %s"
                  % (ids[0], row["id"], row.get("title") or ""))
            if err:                       # filed, but the mapping is lost
                print("helm todos: WARNING %s" % err, file=sys.stderr)
        return 0
    dry = "--dry-run" in rest
    if [a for a in rest if a != "--dry-run"]:
        print(_USAGE, file=sys.stderr)
        return 2
    rep = demote(sid, seat, apply=not dry)
    if as_json:
        print(json.dumps(rep, indent=1, ensure_ascii=False))
        # rc 1 on a refusal even though the report printed: a wrapper that
        # checks the exit code must not read a sweep that judged NOTHING as
        # a sweep that found nothing to judge.
        return 1 if rep["unavailable"] or rep["personal_unavailable"] else 0
    if rep["unavailable"]:
        # A VALUE COMPUTED AND NEVER RENDERED IS ITS OWN BUG CLASS (the
        # `unreadable` loop below learned this first). demote set this field
        # and the text path fell through to "nothing to drop — 0 row(s) still
        # yours and open": a confident clean answer about a ledger nobody
        # could read, with rc 0 under it.
        print("helm todos: task ledger unreadable (%s) — the sweep REFUSED, "
              "no row was judged" % rep["unavailable"], file=sys.stderr)
        return 1
    if rep["personal_unavailable"]:
        print("helm todos: personal task directory unreadable (%s) — the "
              "sweep REFUSED, no row was judged"
              % rep["personal_unavailable"], file=sys.stderr)
        return 1
    # THE TWO CANNOT-TELL POPULATIONS ARE NAMED APART AND CARRY THEIR ROWS.
    # An UNPARSEABLE stamp is a WRITER BUG in a store helm does not own; an
    # ORPHANED one means the LEDGER MOVED ON. They shared a counter and a
    # nameless one, so a sweep running unattended every turn let its operator
    # audit what it DELETED but not what it SILENTLY DECLINED TO UNDERSTAND.
    # RECOVERY IS REPORTED FIRST BECAUSE IT RAN FIRST, and every outcome that
    # leaves bytes somewhere unusual NAMES THE PATH. A report that says
    # "preserved" without saying WHERE is the same silently-absent row this
    # whole leg exists to prevent, one layer up.
    # THE OUTCOME VOCABULARY IS DECOMPOSED, NOT ENUMERATED. Adding
    # `-residual` variants as five more elif branches is how the surface fell
    # through in the first place: a successful restore reported
    # `restored-residual`, matched nothing, and printed CANNOT DECIDE at the
    # operator — a working recovery reading to a human as an undecidable
    # failure. Splitting the suffix off means a new base outcome is the ONLY
    # thing that can reach the fallback, and retention is reported once, in
    # its own block, with a count and exact paths (a review ruling).
    retained = []
    unrecorded = []
    pending = []
    for r in rep.get("recovered") or []:
        what = r.get("row") or r.get("claim")
        outcome = r.get("outcome") or ""
        if outcome == RESIDUAL:
            # RETAINED STATE, NOT A NEW ACTION. A terminal residual is a
            # decision already taken; re-announcing it every pass as though
            # something just happened is the noise that makes an operator stop
            # reading the one surface that tells them where their bytes are.
            retained.append(r)
            continue
        # THREE PARALLEL SUFFIXES, ONE TABLE. Two of them were an if/else
        # ladder and adding a third by hand produced a knot on the first try;
        # three same-shaped cases are a data structure, not more branches.
        #
        # They mean genuinely different things and must not be merged:
        # RESIDUAL is a decision already taken and the payload is the retained
        # backup; UNRECORDED is RETRYABLE — the claim stands and nothing on
        # disk says why, so filing it with the residuals would invite an
        # operator to dispose of an undecided claim; PENDING means a DRY RUN
        # reached it and terminalized nothing, so reporting either of the
        # others would claim a write that was never attempted.
        for suffix, bucket in (("-" + RESIDUAL, retained),
                               ("-" + UNRECORDED, unrecorded),
                               ("-" + PENDING, pending)):
            head, marker, _tail = outcome.partition(suffix)
            if marker:
                bucket.append(r)
                outcome = head
                break
        if outcome == "completed-unrecorded":
            print("  removed %s, BUT ITS CLAIM COULD NOT BE CLEARED OR "
                  "RECORDED — the bytes are safe at %s and recovery will meet "
                  "this claim again until it is dealt with"
                  % (what, r.get("preserved")))
        elif outcome == "restored":
            print("  RECOVERED %s — a crashed sweep had claimed it; it is back "
                  "in your list" % what)
        elif outcome == "completed":
            print("  finished a crashed removal of %s — the undo already held "
                  "those exact bytes, so nothing was put back" % what)
        elif outcome == "contested":
            print("  KEPT BOTH at %s — a writer holds that path, so the bytes "
                  "helm had claimed stay at %s"
                  % (r.get("occupied"), r.get("preserved")))
        elif outcome == "restorable":
            print("  would recover a crashed claim for %s — its bytes are at %s"
                  % (r.get("occupied") or what, r.get("preserved")))
        elif outcome == "restored-unexpected":
            print("  RECOVERED %s, BUT IT IS NOT THE ROW THE SWEEP DECIDED "
                  "ABOUT — recorded %s, found %s. The bytes are back where "
                  "they belong; the mismatch is reported rather than assumed "
                  "away" % (what, r.get("recorded"), r.get("found")))
        elif outcome == "restored-replaced":
            # THE OWNER IS TOLD THE TRUTH, NOT THE HAPPY VERSION. The bytes
            # went back and then something else took that name, so "recovered"
            # would point at a file that is not the one recovered.
            print("  PUT %s BACK, BUT SOMETHING ELSE NOW HOLDS THAT NAME — the "
                  "recovered bytes are the ones helm had claimed and are kept "
                  "at %s; nothing of yours was overwritten"
                  % (what, r.get("preserved") or r.get("claim")))
        elif outcome == "restored-unverified":
            print("  put %s back, BUT COULD NOT RE-READ THAT PATH TO CONFIRM "
                  "IT — the claimed bytes are kept at %s until it can be "
                  "checked" % (what, r.get("preserved") or r.get("claim")))
        elif outcome == "empty":
            print("  cleared an empty crashed claim at %s"
                  % (r.get("claim") or "claim %s (its directory was renamed "
                     "or replaced, so helm will not name a path)"
                     % r.get("claim_id", "?")))
        else:
            where = r.get("preserved") or r.get("claim")
            print("  CANNOT DECIDE a crashed claim %s (%s) — its bytes are "
                  "left where they are%s"
                  % (r.get("claim") or r.get("claim_id", "?"), outcome,
                     ", at %s" % where if where else
                     " (its directory was renamed or replaced, so helm will "
                     "not name a path)"))
    # THE LIVE REMOVAL PATH'S RESIDUALS WERE RENDERED NOWHERE (found by a
    # cumulative refute). rep["residuals"] is written by every successful
    # _trash — which, since the publish links and preserves, is EVERY removal
    # — and no surface consumed it, so the operator was told a row was removed
    # and never told where its retained backup is. That is the same
    # silently-absent shape this leg exists to prevent, one layer up.
    for r in rep.get("residuals") or []:
        # A LOCATION THE CODE COULD NOT VERIFY MUST NOT REACH THE OPERATOR AS
        # A PATH. Binding these reports to their descriptors made `payload`
        # None whenever the claim directory was renamed under us — and this
        # renderer would have printed "the bytes are at None", which is worse
        # than the stale path it replaced: at least a stale path looks like a
        # path. The unverified case gets its own sentence.
        where = r.get("payload")
        if not where:
            print("  removed %s — ITS RETAINED BACKUP COULD NOT BE LOCATED "
                  "(the claim directory was renamed or replaced under the "
                  "sweep). The bytes were not deleted; helm will not guess "
                  "where they are. Claim id: %s"
                  % (r.get("row"), r.get("claim_id")))
        elif r.get("state") == UNRECORDED:
            print("  removed %s, BUT ITS CLAIM RECORDED NO DECISION — the "
                  "bytes are at %s and this claim will be seen again"
                  % (r.get("row"), where))
        else:
            print("  removed %s — retained backup at %s (%s)"
                  % (r.get("row"), where,
                     "same inode as the undo, one directory entry"
                     if r.get("same_inode") else
                     "a separate copy: the undo is on another filesystem"))
    if unrecorded:
        print("  %d claim(s) recorded NO decision — retryable, not retained, "
              "and recovery will meet them again:" % len(unrecorded))
        for r in unrecorded:
            where = r.get("preserved") or r.get("claim")
            print("      %s %s" % (r.get("row") or r.get("claim")
                                   or r.get("claim_id", "?"),
                                   "at %s" % where if where else
                                   "(location unverified — claim %s)"
                                   % r.get("claim_id", "?")))
    if pending:
        # A DRY RUN'S OWN LINE. These claims are still standing and nothing
        # was written; saying so is the whole point of a dry run, and folding
        # them into the residual or unrecorded counts would report writes that
        # never happened (refuter D).
        print("  %d claim(s) WOULD be dealt with — nothing was changed:"
              % len(pending))
        for r in pending:
            print("      %s (claim %s)"
                  % (r.get("row") or "a row",
                     r.get("claim_id") or os.path.basename(r.get("claim")
                                                           or "?")))
    if retained:
        # COUNT AND EXACT PATHS, IN THE SAME RUN (a review ruling). With
        # preserve-in-place this is the NORMAL disposal path, not a
        # diagnostic, so it is the line that tells an operator what the sweep
        # actually left them.
        print("  %d retained backup(s) — the removals succeeded and these "
              "bytes are kept until you dispose of them:" % len(retained))
        for r in retained:
            where = r.get("preserved") or r.get("claim")
            print("      %s %s" % (r.get("row") or r.get("claim")
                                   or r.get("claim_id", "?"),
                                   "at %s" % where if where else
                                   "(location unverified — claim %s)"
                                   % r.get("claim_id", "?")))
    for r in rep.get("preserved") or []:
        # THE SAME NONE-LEAK AS THE RESIDUAL LINE, one loop down and missed on
        # the first pass. Binding these locations made both fields
        # droppable, and this printed "undo at None ... object ... None"
        # whenever the claim or undo directory had been reused. An honest
        # unknown, or a path — never the word None.
        undo = r.get("undo")
        obj = r.get("object")
        if undo and obj:
            print("  removed %s — its undo at %s is a COPY (different "
                  "filesystem), so the object itself is preserved at %s "
                  "rather than deleted while a writer could still reach it"
                  % (r.get("row"), undo, obj))
        else:
            print("  removed %s — its undo is a COPY (different filesystem) "
                  "and the object was preserved rather than deleted, but %s "
                  "could not be verified (the directory was renamed or "
                  "replaced), so helm will not name %s"
                  % (r.get("row"),
                     "neither location" if not undo and not obj
                     else "the undo's location" if not undo
                     else "the object's location",
                     "them" if not undo and not obj else "it"))
    for r in rep["unparseable"]:
        print("  kept %s — stamp %r does not parse, so the ledger cannot be "
              "asked about it (a writer wrote it wrong)"
              % (r["id"], r["stamp"]))
    for r in rep["orphaned"]:
        print("  kept %s — stamp names %s, which the ledger does not hold "
              "(the ledger moved on)" % (r["id"], r["row"]))
    for r in rep["unreadable"]:
        # A VALUE COMPUTED AND NEVER RENDERED IS ITS OWN BUG CLASS.
        print("  kept %s — the file does not parse at all, so no row could "
              "be read from it (a writer wrote it wrong)%s"
              % (r.get("name") or "a row",
                 "" if r.get("file") else
                 " [its directory was renamed or replaced, so helm will not "
                 "name a path]"))
    for r in rep["would_remove"]:
        print("  would drop %s — %s" % (r["id"], r["why"]))
    declined = (
        (rep["unstamped"], "never promoted"),
        (len(rep["unparseable"]), "with an unreadable stamp"),
        (len(rep["orphaned"]), "stamped but not in the ledger"),
        (len(rep["unreadable"]), "file(s) that do not parse"),
    )
    declined_text = "".join(", %d %s" % item for item in declined if item[0])
    if not rep["removed"] and not rep["failed"] and not rep["would_remove"]:
        # THE POPULATION IS SAID EVEN WHEN NOTHING MOVED. "nothing to drop"
        # over a list of 200 unstamped rows reads as "your list is clean",
        # which is the opposite of true — they are unreachable, not absent.
        print("helm todos: nothing to drop — %d row(s) still yours and open%s"
              % (rep["kept"], declined_text))
        return 0
    for r in rep["removed"]:
        print("  dropped %s — %s" % (r["id"], r["why"]))
    for r in rep["failed"]:
        # KEPT, not dropped: a trash failure CANCELS the removal, so the file
        # is still there and saying "dropped (FAILED)" described a state the
        # filesystem was not in.
        print("  KEPT %s — %s, but the undo copy failed so the removal was "
              "cancelled: %s" % (r["id"], r["why"], r["failed"]))
    print("helm todos: %d %s, %d row(s) still yours and open%s%s%s"
          % (len(rep["would_remove"]) if dry else len(rep["removed"]),
             "would drop" if dry else "dropped", rep["kept"], declined_text,
             ", %d removal(s) CANCELLED" % len(rep["failed"])
             if rep["failed"] else "",
             "  (originals kept in %s)" % rep["trash"] if rep["trash"] else ""))
    return 0


def cmd_todos(args):
    """todos [--all] [--json] — the seat todo mirror: what each agent is
    working on right now (pull-only; the recorder fills it)."""
    args = list(args or [])
    if args and args[0] in ("-h", "--help"):
        print(_USAGE)
        return 0
    # THE TWO WRITERS (task/327). Read verbs stay below and unchanged.
    if args and args[0] in ("promote", "demote"):
        return _cmd_bridge(args)
    unknown = [a for a in args if a not in ("--all", "--json")]
    if unknown:
        print(_USAGE, file=sys.stderr)
        return 2
    as_json = "--json" in args
    if "--all" in args:
        # --json is the DETAIL surface (it carries every item); the table
        # reads only the digest, so it never pays for the lists.
        rep = fleet(items=as_json)
        if as_json:
            print(json.dumps(rep, indent=2, ensure_ascii=False))
            return 0
        _print_all(rep["seats"] + rep["orphans"], rep["now"],
                   rep.get("orphans_hidden") or 0)
        return 0
    sid = this_session()
    st = state(sid) if sid else {}
    if as_json:
        print(json.dumps({"session": sid, **(st or {})}, indent=2,
                         ensure_ascii=False))
        return 0
    if not sid:
        print("  no session in this environment — `helm todos --all` reads "
              "the fleet")
        return 0
    # A CRASHED CLAIM IS INVISIBLE TO THIS LIST BY CONSTRUCTION: the row is no
    # longer at its pathname, so nothing this surface reads can see it. That is
    # exactly why the READ path has to say so (D6) — recovery lived
    # only in `demote`, so a session that never swept again left the row held
    # and reported NOWHERE A READER LOOKS.
    #
    # THIS HALF DECIDES AND TOUCHES NOTHING. apply=False, so it names what is
    # held and where the bytes are; `helm todos demote` is what puts them back.
    # A read verb that repaired state would be the mutating dry run all over
    # again, one surface over.
    # `undecidable` BELONGS IN THIS LIST. It is exactly the state
    # where the undo root is missing, moved, symlinked or unopenable — the
    # loudest thing recovery can say — and the filter omitted it, so the one
    # outcome an operator most needs to see was the one surface that never
    # mentioned it. Silence about an unresolved claim is the defect this whole
    # leg exists to prevent, and it had reached the owner's own verb.
    held = [r for r in recover_claims(sid, apply=False)
            if r.get("outcome") in ("restorable", "contested", "inconsistent",
                                    "unreadable", "unrecoverable",
                                    "undecidable")]
    for r in held:
        # A PATH OR AN HONEST UNKNOWN — never a stale one. `preserved` is now
        # stripped when it cannot be verified, so falling back to the claim
        # pathname would reintroduce exactly the stale name the door removed.
        where = r.get("preserved")
        if where:
            told = "its bytes are at %s" % where
        else:
            told = ("its bytes are held by claim %s, whose location could not "
                    "be verified" % os.path.basename(r.get("claim") or "?"))
        print("  HELD BY A CRASHED SWEEP: %s — %s%s"
              % (r.get("row") or "a row", told,
                 " (a writer now holds the original path)"
                 if r.get("outcome") == "contested" else
                 " (its undo could not be read, so whether the removal "
                 "finished cannot be decided)"
                 if r.get("outcome") == "undecidable" else ""))
    if held:
        print("  these are NOT lost — run `helm todos demote` to put them back")
    items = [i for i in (st.get("items") or []) if isinstance(i, dict)] \
        if isinstance(st.get("items"), list) else []
    if not items:
        print("  no todos mirrored for this session yet")
        return 0
    d = digest(items)
    print("  %d/%d done · mirrored %s ago" % (
        d["done"], d["total"], _age(time.time() - _ts(st))))
    for it in items:
        print("  %s %s" % (_GLYPH.get(it.get("status"), "·"),
                           it.get("text") or ""))
    return 0
