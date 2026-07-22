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
import json
import os
import re
import sys
import time

from . import home, pk

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

def digest(items):
    """The pull surface's one-line summary of a list:
    {"active","done","total","fp"}. `fp` is the MATERIAL fingerprint — the
    in-progress line plus the done/total counts. Reordering, re-wording a
    pending item, or any other churn leaves it unchanged, which is exactly
    what makes 'nothing materially changed' cheap to detect.

    Rows that are not well-formed dicts are DROPPED, not trusted: this reads
    a file a hand-edit or a truncated write can reach, and every consumer of
    the digest (CLI, roster, web) has to survive it."""
    items = [i for i in (items or []) if isinstance(i, dict)] \
        if isinstance(items, list) else []
    active = next((str(i.get("text") or "") for i in items
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
    is the CALLER's contract too — record() swallows everything."""
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
    pk.write_json(state_path(sid), st)
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
        chat.post(line, room=home.env("CHAT_ROOM") or "main", who=seat,
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
    r = {"seat": name, "project": project, "active": d["active"],
         "done": d["done"], "total": d["total"], "ts": _ts(st)}
    if items:
        r["items"] = [i for i in (st.get("items") or []) if isinstance(i, dict)]
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
       helm todos --all [--json]     every seat: who is working on what"""

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


def cmd_todos(args):
    """todos [--all] [--json] — the seat todo mirror: what each agent is
    working on right now (pull-only; the recorder fills it)."""
    args = list(args or [])
    if args and args[0] in ("-h", "--help"):
        print(_USAGE)
        return 0
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
