#!/usr/bin/env python3
"""helm seats — the meld-half's agent-facing lane, collapsed onto the chat
room (design: prd/2026-07-20-meldhalf-design.md). Meld carried a separate
tmpfs whisper channel because it had no room; helm HAS the room, so every
capability here is a READ PATTERN over /dev/shm/helm-chat plus one RAM roster
file — zero new transports, zero daemons, zero slot files.

The lane is called DELIVERY, never "whisper" (that word is taken twice:
inject's first-turn brief digest, and the v1 on-ledger attestation frames).

The four legs:
  * join    — SessionStart hook: roster row (RAM presence) + the seat's
              identity/protocol line as session context. Never posts to the
              room (9 homes x resumes = join spam; presence lives in the
              roster panel, not the transcript).
  * deliver — PostToolUse hook: the tool-boundary nudge. An agent 40 tool
              calls into an autonomous turn is unreachable by chat today;
              this closes exactly that (meld's whole live-nudge value). At
              most ONE row per boundary, 200-byte clip, control-char scrub,
              information-not-instruction label. Every fire refreshes the
              seat's roster last_seen — presence freshness IS "last tool
              boundary", so the roster needs no heartbeat daemon.
  * wait    — the beacon: block until a row addressed to the seat lands
              (Monitor arms it; --follow stays the firehose form).
  * claims/council kernels — the only non-redundant M2 subset: an advisory
              same-host TTL lease (the worktree-collision class) and the
              embargoed verdict (independent judgments, no anchoring). The
              dregg cap-gated forms stay upstream; this trust domain is one
              uid in a 0700 tmpfs dir.

Every hook-facing path is FAIL-OPEN TOTAL: any surprise prints nothing and
exits 0 — a wedged helm must never hold or shape a turn.

Cursor law: <room>.cursor.<seat> holds {"n": rows-processed, "size": room
bytes at that point, -1 when mid-backlog}. The hot path is one stat — size
unchanged => exit — so the fleet-wide per-tool-call cost is O(1). First
contact baselines to the current total (no backlog flood); a rotation resets
to 0 (a duplicate nudge is acceptable, silence is not — read() already
self-heals the same way).
"""
import getpass
import json
import os
import re
import sys
import time
import unicodedata

from . import chat, home, pk

MAX_BYTES = 200          # the delivery clip — meld's whisper frame budget
PREVIEW_CHARS = 80       # roster panel preview
FRESH_S, QUIET_S = 120, 900
DEFAULT_TTL = 900        # claims lease default
_BROADCAST = re.compile(r"(?<![A-Za-z0-9._-])@(all|fleet|everyone)(?![A-Za-z0-9._-])", re.I)


# ---------------------------------------------------------------------------
# identity + addressing
# ---------------------------------------------------------------------------

def derive_seat(session=None):
    """$HELM_CHAT_NAME first (the launch seam sets it), else the session-
    derived agent name — chat.whoname's law: a bare agent never gets the
    operator's identity."""
    name = home.env("CHAT_NAME")
    if name:
        return name
    if session:
        return "agent-" + str(session)[:8]
    return chat.whoname()


def owner_names():
    """Senders whose posts deliver WITHOUT an @mention (the owner steers
    mid-flight — meld's watcher use case). HELM_CHAT_OWNER_NAMES csv
    overrides; default = 'david' (the web surface's name) + the unix login."""
    raw = home.env("CHAT_OWNER_NAMES")
    if raw is not None:
        return {n.strip().lower() for n in raw.split(",") if n.strip()}
    names = {"david"}
    try:
        names.add(getpass.getuser().lower())
    except Exception:
        pass
    return names


def _mention_re(seat):
    return re.compile(r"(?<![A-Za-z0-9._-])@" + re.escape(seat)
                      + r"(?![A-Za-z0-9._-])", re.I)


def deliverable(m, seat):
    """Does this row reach `seat` at a tool boundary? @seat / @all mentions
    and owner posts do; agent-to-agent non-mention chatter does NOT (noise
    law); reactions and own posts never."""
    text = m.get("text")
    if not text or m.get("react"):
        return False
    frm = str(m.get("from") or "")
    if frm == seat:
        return False
    if _mention_re(seat).search(text) or _BROADCAST.search(text):
        return True
    return frm.lower() in owner_names()


def _scrub(s):
    """Meld's reader-side defense, ported verbatim in spirit: strip anything
    that could reshape the single-line label the content rides in — C0/C1
    controls, format chars, line/paragraph separators. Tab survives."""
    return "".join(ch for ch in s if ch == "\t"
                   or unicodedata.category(ch) not in ("Cc", "Cf", "Zl", "Zp"))


def _clip(s, cap=MAX_BYTES):
    """Byte-budget clip on a codepoint boundary (meld's re-clip law)."""
    enc = s.encode("utf-8")
    if len(enc) <= cap:
        return s
    end = cap
    while end > 0 and (enc[end] & 0xC0) == 0x80:
        end -= 1
    return enc[:end].decode("utf-8", errors="ignore") + "…"


# ---------------------------------------------------------------------------
# roster (RAM presence) + cursors
# ---------------------------------------------------------------------------

def roster_path():
    return os.path.join(chat.chat_dir(), ".roster.json")


def cursor_path(room, seat):
    return os.path.join(chat.chat_dir(),
                        "%s.cursor.%s" % (pk.slug(room), pk.slug(seat)))


def roster():
    return pk.read_json(roster_path(), {}) or {}


def touch_roster(seat, session=None, cwd=None, joined=False):
    """Refresh the seat's presence row. Keyed by seat, last-writer-wins (two
    sessions under one HELM_CHAT_NAME share a row AND a cursor — documented).
    Advisory presence, not a record: a lost concurrent update heals on the
    next tool boundary."""
    chat._ensure_dir()
    r = roster()
    row = r.get(seat) or {}
    if session:
        row["session"] = str(session)
    if cwd:
        row["cwd"] = cwd
        row["project"] = os.path.basename(cwd.rstrip(os.sep)) or cwd
    if joined and not row.get("joined"):
        row["joined"] = pk.now_ts()
    row["last_seen"] = time.time()
    r[seat] = row
    pk.write_json(roster_path(), r)
    return row


def seat_for_session(session):
    if not session:
        return None
    for seat, row in roster().items():
        if row.get("session") == str(session):
            return seat
    return None


def _cursor(room, seat):
    """(n, size) or None (no/corrupt cursor = first contact)."""
    d = pk.read_json(cursor_path(room, seat), None)
    if isinstance(d, dict) and isinstance(d.get("n"), int):
        return d["n"], d.get("size", -1)
    return None


def _write_cursor(room, seat, n, size):
    chat._ensure_dir()
    pk.write_json(cursor_path(room, seat), {"n": n, "size": size})


def _baseline(room, seat):
    """First contact: start delivery at NOW — never flood a fresh seat with
    the room's whole history one nudge per tool call."""
    try:
        size = os.path.getsize(chat.room_path(room))
    except OSError:
        size = 0
    _write_cursor(room, seat, chat.read(room)[1], size)


# ---------------------------------------------------------------------------
# join (SessionStart) + deliver (PostToolUse) + wait (the beacon)
# ---------------------------------------------------------------------------

def join(session=None, cwd=None, seat=None, room="main"):
    """The autojoin: roster row + cursor baseline + the identity line the
    hook injects as session context. Idempotent per seat."""
    seat = seat or seat_for_session(session) or derive_seat(session)
    touch_roster(seat, session=session, cwd=cwd, joined=True)
    if _cursor(room, seat) is None:
        _baseline(room, seat)
    line = ("[helm chat] you are seat '%s' in room %s — @%s and owner posts "
            "reach you between tool calls; speak: helm chat post; catch up: "
            "helm chat read; block on the next word: helm chat wait"
            % (seat, room, seat))
    return seat, line


def deliver(session=None, room="main", seat=None):
    """The tool-boundary nudge: at most ONE deliverable row, oldest first;
    the rest collapse to a count. Returns the label line or None. Every call
    refreshes presence (the free heartbeat)."""
    if (home.env("CHAT_DELIVER") or "").lower() in ("0", "off", "no"):
        return None
    seat = seat or seat_for_session(session) or derive_seat(session)
    touch_roster(seat, session=session)
    cur = _cursor(room, seat)
    if cur is None:
        _baseline(room, seat)   # self-heals a session older than the install
        return None
    n, csize = cur
    try:
        size = os.path.getsize(chat.room_path(room))
    except OSError:
        return None
    if csize == size:           # the overwhelmingly common case — one stat
        return None
    rows, total = chat.read(room, n)
    if n > total:               # rotation under the cursor — read() already
        n = 0                   # reset the slice; realign the index base
    hit = next((i for i, m in enumerate(rows) if deliverable(m, seat)), None)
    if hit is None:
        _write_cursor(room, seat, total, size)
        return None
    waiting = sum(1 for m in rows[hit + 1:] if deliverable(m, seat))
    new_n = n + hit + 1
    _write_cursor(room, seat, new_n, size if new_n == total else -1)
    m = rows[hit]
    line = "[helm chat → %s] %s: %s" % (
        seat, m.get("from") or "?", _clip(_scrub(m.get("text") or "")))
    if waiting:
        line += " (+%d waiting — helm chat read)" % waiting
    return line


def wait(seat=None, room="main", any_row=False, timeout=None, poll=None):
    """Block until the next word arrives; print-ready line or None on
    timeout. Seat mode is a DELIVERY surface (advances the seat cursor, so
    the PostToolUse leg never re-nudges what wait already surfaced); --any
    watches the room without touching any cursor. Monitor arms this."""
    poll = chat.POLL_S if poll is None else poll
    deadline = time.time() + timeout if timeout else None
    seat = seat or derive_seat(None)
    since = chat.read(room)[1] if any_row else None
    while True:
        if any_row:
            rows, total = chat.read(room, since)  # read() self-heals since>total
            if rows:
                return chat._fmt(rows[0])
            since = total
        else:
            line = deliver(room=room, seat=seat)
            if line:
                return line
        if deadline and time.time() >= deadline:
            return None
        time.sleep(poll)


# ---------------------------------------------------------------------------
# claims — the advisory TTL lease (M2's non-redundant kernel #1)
# ---------------------------------------------------------------------------

def claims_path():
    return os.path.join(chat.chat_dir(), ".claims.json")


def _claims_live():
    """Load + sweep expired. Advisory by design: read-modify-write between
    cooperating same-uid agents, not a security boundary (the cap-gated form
    stays dregg's)."""
    c = pk.read_json(claims_path(), {}) or {}
    now = time.time()
    live = {r: v for r, v in c.items()
            if isinstance(v, dict) and v.get("expiry", 0) > now}
    if len(live) != len(c):
        pk.write_json(claims_path(), live)
    return live


def claim(resource, seat, ttl=DEFAULT_TTL):
    """(ok, message). Refused while another holder's lease is live; the
    holder re-claiming extends."""
    chat._ensure_dir()
    c = _claims_live()
    row = c.get(resource)
    if row and row.get("holder") != seat:
        return False, "%s is held by %s for %ds more" % (
            resource, row.get("holder"), int(row["expiry"] - time.time()))
    c[resource] = {"holder": seat, "expiry": time.time() + ttl, "ts": pk.now_ts()}
    pk.write_json(claims_path(), c)
    return True, "%s claimed by %s for %ds" % (resource, seat, ttl)


def release(resource, seat):
    """(ok, message). Holder-bound — meld's law: a third party can neither
    steal a live lease nor drop someone else's."""
    c = _claims_live()
    row = c.get(resource)
    if not row:
        return False, "%s is not claimed" % resource
    if row.get("holder") != seat:
        return False, "%s is held by %s — only the holder releases" % (
            resource, row.get("holder"))
    del c[resource]
    pk.write_json(claims_path(), c)
    return True, "%s released" % resource


def claims_list():
    now = time.time()
    return [{"resource": r, "holder": v.get("holder"),
             "remaining": int(v.get("expiry", now) - now)}
            for r, v in sorted(_claims_live().items())]


# ---------------------------------------------------------------------------
# council — the embargoed verdict (M2's non-redundant kernel #2)
# ---------------------------------------------------------------------------

def council_path(topic):
    return os.path.join(chat.chat_dir(), ".council-%s.json" % pk.slug(topic))


def verdict(topic, seat, text, room="main"):
    """Seal (or replace) this seat's verdict; announce the SEAL, never the
    content — the embargo is the whole point (independent judgments, no
    anchoring; chat alone cannot express this)."""
    chat._ensure_dir()
    p = council_path(topic)
    seals = pk.read_json(p, {}) or {}
    seals[seat] = {"text": text, "ts": pk.now_ts()}
    pk.write_json(p, seals)
    chat.post("[council %s] %s sealed a verdict (%d sealed — "
              "helm chat reveal %s lifts the embargo)"
              % (topic, seat, len(seals), topic), room, who=seat)
    return len(seals)


def reveal(topic, room="main"):
    """Lift the embargo exactly once: post every sealed verdict as its
    author's row in one pass, then delete the topic. Returns count."""
    p = council_path(topic)
    seals = pk.read_json(p, {}) or {}
    for seat in sorted(seals):
        chat.post("[council %s] verdict: %s" % (topic, seals[seat].get("text", "")),
                  room, who=seat)
    try:
        os.remove(p)
    except OSError:
        pass
    return len(seals)


# ---------------------------------------------------------------------------
# the roster report (CLI table + GET /api/chat/roster + the seats panel)
# ---------------------------------------------------------------------------

def presence_of(last_seen):
    if not last_seen:
        return "absent"
    age = time.time() - last_seen
    return "fresh" if age < FRESH_S else "quiet" if age < QUIET_S else "absent"


def roster_report(room="main"):
    """{"seats": [...], "claims": [...]} — read-only, fail-open by caller."""
    seats = []
    for seat, row in sorted(roster().items()):
        cur = _cursor(room, seat)
        pending, preview = 0, None
        if cur is not None:
            rows = chat.read(room, min(cur[0], chat.read(room)[1]))[0]
            hits = [m for m in rows if deliverable(m, seat)]
            pending = len(hits)
            if hits:
                preview = _scrub(hits[-1].get("text") or "")[:PREVIEW_CHARS]
        seats.append({"seat": seat, "session": row.get("session"),
                      "project": row.get("project"), "cwd": row.get("cwd"),
                      "last_seen": row.get("last_seen"),
                      "presence": presence_of(row.get("last_seen")),
                      "pending": pending, "preview": preview})
    return {"room": room, "seats": seats, "claims": claims_list()}


# ---------------------------------------------------------------------------
# CLI (dispatched from chat.cmd_chat) + the hook legs
# ---------------------------------------------------------------------------

def _hook_stdin():
    """Bounded hook-JSON read (meld's law: a pathological stdin must not
    burn the boundary's latency budget)."""
    try:
        d = json.loads(sys.stdin.buffer.read(65536) or b"{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _flag(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def _hook_print(event, line):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": event, "additionalContext": line}}))


def cmd(verb, args, room="main"):
    """The seats subverbs, reached through `helm chat <verb>`."""
    args = list(args or [])
    if verb == "join":
        try:
            session = cwd = None
            if "--hook-json" in args:
                d = _hook_stdin()
                session, cwd = d.get("session_id"), d.get("cwd")
            seat, line = join(session=session, cwd=cwd or os.getcwd(),
                              seat=_flag(args, "--seat"), room=room)
            if "--hook-json" in args:
                _hook_print("SessionStart", line)
            else:
                print(line)
        except Exception:
            pass                    # fail-open: never shape a session start
        return 0
    if verb == "deliver":
        try:
            session = None
            if "--hook-json" in args:
                session = _hook_stdin().get("session_id")
            line = deliver(session=session, room=room, seat=_flag(args, "--seat"))
            if line:
                if "--hook-json" in args:
                    _hook_print("PostToolUse", line)
                else:
                    print(line)
        except Exception:
            pass                    # fail-open: never hold a tool boundary
        return 0
    if verb == "wait":
        timeout = _flag(args, "--timeout")
        line = wait(seat=_flag(args, "--seat"), room=room,
                    any_row="--any" in args,
                    timeout=float(timeout) if timeout else None)
        if line is None:
            return 1
        print(line)
        return 0
    if verb == "seats":
        rep = roster_report(room)
        if not rep["seats"]:
            print("helm chat: no seats yet — sessions join on their next start "
                  "(helm hooks install wires it)")
            return 0
        w = max(len(s["seat"]) for s in rep["seats"])
        for s in rep["seats"]:
            print("  %-*s  %-6s  pending %-3d %s" % (
                w, s["seat"], s["presence"], s["pending"],
                (s.get("project") or "")))
        for c in rep["claims"]:
            print("  claim: %s -> %s (%ds left)" % (
                c["resource"], c["holder"], c["remaining"]))
        return 0
    if verb == "claim":
        if not args:
            print("usage: helm chat claim <resource> [--ttl SECONDS] [--seat S]",
                  file=sys.stderr)
            return 2
        ttl = _flag(args, "--ttl")
        ok, msg = claim(args[0], _flag(args, "--seat") or derive_seat(None),
                        ttl=int(ttl) if ttl else DEFAULT_TTL)
        print("helm chat: " + msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if verb == "release":
        if not args:
            print("usage: helm chat release <resource> [--seat S]", file=sys.stderr)
            return 2
        ok, msg = release(args[0], _flag(args, "--seat") or derive_seat(None))
        print("helm chat: " + msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if verb == "claims":
        rows = claims_list()
        if not rows:
            print("helm chat: no live claims")
            return 0
        for c in rows:
            print("  %s -> %s (%ds left)" % (c["resource"], c["holder"],
                                             c["remaining"]))
        return 0
    if verb == "verdict":
        if len(args) < 2:
            print("usage: helm chat verdict <topic> <text...> [--seat S]",
                  file=sys.stderr)
            return 2
        seat = _flag(args, "--seat") or derive_seat(None)
        rest = args[1:]
        if "--seat" in rest:
            i = rest.index("--seat")
            del rest[i:i + 2]
        n = verdict(args[0], seat, " ".join(rest).strip(), room=room)
        print("helm chat: verdict sealed for '%s' (%d sealed)" % (args[0], n))
        return 0
    if verb == "reveal":
        if not args:
            print("usage: helm chat reveal <topic>", file=sys.stderr)
            return 2
        n = reveal(args[0], room=room)
        print("helm chat: revealed %d verdict%s on '%s'" % (n, "s"[:n != 1], args[0]))
        return 0
    print("helm chat: unknown subcommand '%s'" % verb, file=sys.stderr)
    return 2
