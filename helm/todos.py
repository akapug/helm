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
import unicodedata

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


def claude_home():
    """The harness config home this seat runs under. CLAUDE_CONFIG_DIR is the
    per-seat pin (helm/launch.py sets it); ~/.claude is the unpinned default."""
    return os.environ.get("CLAUDE_CONFIG_DIR") or \
        os.path.expanduser(os.path.join("~", ".claude"))


def personal_dir(sid):
    return os.path.join(claude_home(), PERSONAL, str(sid or ""))


def personal_rows(sid, collect_unreadable=None):
    """[(path, row)] for one session's personal task files, id-sorted.

    A file that will not parse is SKIPPED, never repaired and never counted:
    the bridge's whole job is to remove rows safely, and a row it cannot read
    is a row it cannot prove anything about."""
    out = []
    d = personal_dir(sid)
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        full = os.path.join(d, name)
        row = pk.read_json(full, None)
        if isinstance(row, dict):
            out.append((full, row))
        elif collect_unreadable is not None:
            # A FILE THAT WILL NOT PARSE IS A THIRD DECLINED-TO-UNDERSTAND
            # CAUSE (@codex, T1), and the two stamp buckets do not cover it.
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
            collect_unreadable.append({"file": full})
    return out


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
    known, unavailable = tasks.snapshot(path)
    if unavailable:
        # CANNOT SEE is not "no twin" — filing on an unreadable ledger is how
        # a duplicate gets created with no way to notice.
        return None, "task ledger unreadable (%s) — refusing to file" % unavailable
    if not str(owner or "").strip():
        return None, "promote needs an owner — an unowned ledger row is a " \
                     "list, not a ledger (tasks.add refuses it too)"
    for full, row in personal_rows(sid):
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
        # DURABLE IDENTITY ON THE LEDGER SIDE TOO (@codex). The first cut
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
           "trash": None, "unavailable": None}
    # ONE ledger read for the whole sweep, and it REFUSES on an unreadable
    # one. Asking per-row would make every row answer "no twin" — a state
    # that is indistinguishable from an orphaned stamp and would report 200
    # orphans over a ledger that was simply unavailable for a moment. Nothing
    # is removed either way; the difference is whether the operator is told
    # the truth about why.
    known, unavailable = tasks.snapshot(path)
    if unavailable:
        rep["unavailable"] = str(unavailable)
        return rep
    for full, row in personal_rows(sid, collect_unreadable=rep["unreadable"]):
        stamped = str(row.get(STAMP) or "")
        if not stamped:
            rep["unstamped"] += 1
            continue
        # UNPARSEABLE IS NOT ABSENT, AND MY `or ""` COLLAPSED THEM
        # (peer review, mutation-proved: relax the accident this
        # depended on and a VALID ROW IS DELETED). normalize_id returns None
        # to mean "this string is not an id at all" — a different fact from
        # "no such row" — and coercing it to "" handed a falsy key to
        # known.get(). That happened to be safe ONLY because eventledger
        # rejects falsy ids two modules away: an accident, untested, and one
        # refactor from silently deleting rows in a store helm does not own.
        #
        # This is also @codex's "malformed replay can delete valid rows",
        # which I traced and could NOT construct. I was looking for a path
        # through my own code; the safety was living somewhere else.
        tid = tasks.normalize_id(stamped)
        if tid is None:
            # cannot even read the stamp — the strongest possible "cannot
            # tell", and cannot-tell has always meant KEEP on this leg.
            #
            # ITS OWN BUCKET, because it is its own FACT (peer review).
            # An UNPARSEABLE stamp means a WRITER BUG in a store helm does not
            # own; an ORPHANED one means the LEDGER MOVED ON. Different owners,
            # different fixes, and they shared a counter — so demote honoured
            # "UNPARSEABLE is not ABSENT" in the DECISION and collapsed it in
            # the REPORT, which is the half an operator reads.
            rep["unparseable"].append({"file": full, "id": row.get("id"),
                                       "stamp": stamped})
            continue
        twin = known.get(tid)
        if not twin:
            rep["orphaned"].append({"file": full, "id": row.get("id"),
                                    "row": tid})
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
        entry = {"file": full, "id": row.get("id"),
                 "row": twin["id"], "why": why}
        if not apply:
            # A DRY RUN REMOVES NOTHING, SO `removed` MUST BE EMPTY (@codex,
            # T1 — and it falsifies the contract I claimed one commit ago).
            # That commit said `removed` means removed BY CONSTRUCTION; it was
            # true only on the apply path. A dry run appended anyway, so
            # `--dry-run --json` published removed:[row] about a file still on
            # disk and the summary said "1 dropped" under a per-row line
            # reading "would drop". Naming a contract does not establish it —
            # the sentence has to be true on EVERY path the function has.
            rep["would_remove"].append(entry)
            continue
        if apply:
            err = _trash(sid, full, row, rep)
            if err:
                # THE REPORT MUST NOT SAY A THING THE FILESYSTEM DOES NOT
                # (peer review). This used to append to `removed`
                # FIRST and let _trash stamp a `failed` key onto the row it
                # had just added — so on the one path built never to lose a
                # row, the row STAYED in `removed` while its FILE STILL
                # EXISTED. _trash's own docstring says a failure CANCELS the
                # removal: true of the disk, false of the report. Any consumer
                # doing len(rep["removed"]) over-reported deletions. Now
                # `removed` means removed, by construction rather than by the
                # caller remembering to filter.
                entry["failed"] = err
                rep["failed"].append(entry)
                continue
        rep["removed"].append(entry)
    return rep


def _trash(sid, full, row, rep):
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
    try:
        # RE-READ AND CHECK IDENTITY BEFORE UNLINKING (@codex, T1, P0
        # DESTRUCTIVE, task/394). `personal_rows` reads every file up front and
        # the decision is taken against that CACHED row, but the unlink went by
        # PATHNAME with no identity check — a classic destructive TOCTOU. Their
        # probe replaced the file with a NEW LIVE UNSTAMPED row just before
        # os.remove: helm DELETED that row, reported it removed while citing
        # the OLD closed twin as the reason, and wrote the OLD bytes to trash.
        # A live row destroyed, a false reason recorded, and an undo holding
        # something that was never there — on the one leg built so that no row
        # is ever lost.
        current = pk.read_json(full, None)
        if not isinstance(current, dict):
            return ("the file changed under the sweep and no longer parses, "
                    "so it was NOT removed")
        if _identity(current) != _identity(row):
            got = _identity(current)
            # ONE-ELEMENT TUPLE: `_identity` RETURNS a tuple and `%` spreads
            # one across multiple conversions — the first run of this guard
            # raised TypeError instead of reporting the race it had correctly
            # detected. A refusal path that crashes is not a refusal.
            return ("the file changed under the sweep — it now holds %s, not "
                    "the row this decision was made about, so it was NOT "
                    "removed" % (("%s stamped %s" % got) if got
                                 else "an unidentifiable row",))
        dest = os.path.join(record.session_dir(sid), TRASH)
        os.makedirs(dest, exist_ok=True)
        keep = os.path.join(dest, os.path.basename(full))
        # CURRENT bytes, never the cached ones: an undo that restores a
        # version already replaced is worse than no undo, because it reads as
        # a recovery.
        with open(keep, "w", encoding="utf-8") as fh:
            json.dump(current, fh, ensure_ascii=False, indent=1)
    except FileNotFoundError:
        return "GONE — something else removed this file before the sweep did"
    except OSError as e:
        return ("the undo copy could not be written, so nothing was removed: "
                "%s" % e)
    # TWO PHASES, TWO MESSAGES (@codex residual). Both steps shared one `try`,
    # so an UNLINK failure was reported in the undo-copy's words — the operator
    # was told the backup failed when the backup had SUCCEEDED and the delete
    # was what did not happen. Opposite states, one sentence, and the SAFE one
    # (both copy and original present) read as the dangerous one.
    try:
        os.remove(full)
        rep["trash"] = dest
    except FileNotFoundError:
        # THE MIRROR CASE codex measured: an external unlink wins the race.
        # "removal CANCELLED" is false — the file is gone, helm just did not
        # do it, and nobody should go looking for a row that no longer exists.
        return "GONE — something else removed this file before the sweep did"
    except OSError as e:
        return ("the undo copy IS written but the file could not be removed, "
                "so both still exist: %s" % e)
    return None


def _identity(row):
    """The pair that says WHICH row a personal file holds: its own id and the
    ledger row it is stamped with. Both, because either alone can repeat — an
    id is unique only within a session dir, and one ledger row can be stamped
    on more than one personal row. -> None when the row carries no id at all,
    which never compares equal to anything, including another None."""
    if not isinstance(row, dict):
        return None
    rid = row.get("id")
    if rid in (None, ""):
        return None
    return (str(rid), str(row.get(STAMP) or ""))


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
    for i, a in enumerate(rest):
        if a == "--owner" and i + 1 < len(rest):
            seat = rest[i + 1]
    rest = [a for i, a in enumerate(rest)
            if a != "--owner" and (i == 0 or rest[i - 1] != "--owner")]
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
        return 0
    # THE TWO CANNOT-TELL POPULATIONS ARE NAMED APART AND CARRY THEIR ROWS.
    # An UNPARSEABLE stamp is a WRITER BUG in a store helm does not own; an
    # ORPHANED one means the LEDGER MOVED ON. They shared a counter and a
    # nameless one, so a sweep running unattended every turn let its operator
    # audit what it DELETED but not what it SILENTLY DECLINED TO UNDERSTAND.
    for r in rep["unparseable"]:
        print("  kept %s — stamp %r does not parse, so the ledger cannot be "
              "asked about it (a writer wrote it wrong)"
              % (r["id"], r["stamp"]))
    for r in rep["orphaned"]:
        print("  kept %s — stamp names %s, which the ledger does not hold "
              "(the ledger moved on)" % (r["id"], r["row"]))
    for r in rep["unreadable"]:
        # A VALUE COMPUTED AND NEVER RENDERED IS ITS OWN BUG CLASS.
        print("  kept %s — the file does not parse at all, so no row could be "
              "read from it (a writer wrote it wrong)" % r["file"])
    for r in rep["would_remove"]:
        print("  would drop %s — %s" % (r["id"], r["why"]))
    if not rep["removed"] and not rep["failed"] and not rep["would_remove"]:
        # THE POPULATION IS SAID EVEN WHEN NOTHING MOVED. "nothing to drop"
        # over a list of 200 unstamped rows reads as "your list is clean",
        # which is the opposite of true — they are unreachable, not absent.
        print("helm todos: nothing to drop — %d row(s) still yours and open"
              "%s%s%s%s" % (rep["kept"],
                          ", %d never promoted" % rep["unstamped"]
                          if rep["unstamped"] else "",
                          ", %d with an unreadable stamp"
                          % len(rep["unparseable"]) if rep["unparseable"] else "",
                          ", %d stamped but not in the ledger"
                          % len(rep["orphaned"]) if rep["orphaned"] else "",
                          ", %d file(s) that do not parse"
                          % len(rep["unreadable"]) if rep["unreadable"] else ""))
        return 0
    for r in rep["removed"]:
        print("  dropped %s — %s" % (r["id"], r["why"]))
    for r in rep["failed"]:
        # KEPT, not dropped: a trash failure CANCELS the removal, so the file
        # is still there and saying "dropped (FAILED)" described a state the
        # filesystem was not in.
        print("  KEPT %s — %s, but the undo copy failed so the removal was "
              "cancelled: %s" % (r["id"], r["why"], r["failed"]))
    print("helm todos: %d %s, %d kept%s%s"
          % (len(rep["would_remove"]) if dry else len(rep["removed"]),
             "would drop" if dry else "dropped", rep["kept"],
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
