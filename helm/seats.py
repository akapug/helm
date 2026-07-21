#!/usr/bin/env python3
"""helm seats — the meld-half's agent-facing lane, collapsed onto the chat
room (design: prd/2026-07-20-meldhalf-design.md, hardened per the codex
adversarial round §11 there). Meld carried a separate tmpfs whisper channel
because it had no room; helm HAS the room, so every capability here is a
READ PATTERN over /dev/shm/helm-chat plus small RAM state files — zero new
transports, zero daemons, zero slot files.

The lane is called DELIVERY, never "whisper" (that word is taken twice:
inject's first-turn brief digest, and the v1 on-ledger attestation frames).

TRUST DOMAIN — SAY IT LOUDLY: seat names are DISPLAY LABELS. Everything in
this module is advisory coordination between cooperating same-uid processes
in a 0700 tmpfs dir — not a security boundary, and never claimed as one
(principal cryptography stays dregg's; do not rebuild it here). What the
bindings below DO defend against is the realistic failure: an agent — or a
prompt-injected one — impersonating the owner or another holder through
legit tooling. Hence: owner-rule delivery trusts only rows the server-side
owner rails stamped (origin web/tui); claim leases bind to {session, lease
nonce, fence}, never to a matching display string.

The legs:
  * join    — SessionStart hook: roster row (RAM presence) + cursor
              INITIALIZED HERE (a message posted between session start and
              the first tool boundary must deliver — codex H5.5) + the seat's
              identity/protocol line as session context.
  * deliver — PostToolUse hook: the tool-boundary nudge. At most ONE row per
              boundary, 200-byte clip, control-char scrub, information-not-
              instruction label. AT-LEAST-ONCE, NEVER AT-MOST-ONCE (codex
              H7): the hook response is emitted in ONE unbuffered write and
              the cursor commits only AFTER — a kill in between produces a
              duplicate next boundary, which beats silence. Every fire
              touches the seat's own `.seen` file (presence for free); the
              unchanged-room fast path never rewrites shared state.
  * wait    — the beacon: block until a row addressed to the seat lands
              (Monitor arms it). NOTE (codex M11): this is busy-turn parity
              plus an OPT-IN idle beacon — an idle session that never armed
              a Monitor receives nothing; the join context line says to arm.
  * claims  — advisory TTL lease with session+nonce+fence binding and
              monotonic expiry (the worktree-collision class).

Council (embargoed verdicts) is DEFERRED to 0.3 — the codex round showed a
correct embargo needs an expected-set freeze, a reveal state machine, salted
commitments and batch-row reveal; the 0.3 spec is recorded in the design
doc §11. No live consumer today, so: record, don't build.

Cursor law (codex H5): `<room>.cursor.<seat>` holds {dev, ino, off, rid} —
the room file's identity, the byte offset of the first unprocessed row, and
the last processed row's stable id. Fast path = one stat (same inode, size
== off ⇒ nothing new; a same-size REPLACEMENT changes the inode and is
caught). Inode change or shrink ⇒ rotation/replacement: reset to 0 and use
rid to suppress the retained overlap (duplicates acceptable, loss is not).
All cursor transitions serialize on `<room>.cursor.<seat>.lock`; rows are
selected/committed by byte offset from ONE fstat'd fd, never by line count.
Initialized at JOIN. Every hook-facing path is FAIL-OPEN TOTAL.
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
SCAN_CAP = 512 * 1024    # deliver never reads more than this per boundary
OWNER_RAILS = ("web", "tui")  # server-side owner surfaces stamp these origins
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
    """Display names the owner rails post under. HELM_CHAT_OWNER_NAMES csv
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
    do. Owner posts do ONLY when the row was stamped by a server-side owner
    rail (origin web/tui — codex C1): a CLI post claiming an owner name is
    an ordinary message and delivers only via mention. Agent chatter without
    a mention never delivers (noise law); reactions and own posts never."""
    text = m.get("text")
    if not text or m.get("react"):
        return False
    frm = str(m.get("from") or "")
    if frm == seat:
        return False
    if _mention_re(seat).search(text) or _BROADCAST.search(text):
        return True
    return m.get("origin") in OWNER_RAILS and frm.lower() in owner_names()


def _scrub(s):
    """Meld's reader-side defense, ported: strip anything that could reshape
    the single-line label the content rides in — C0/C1 controls, format
    chars, line/paragraph separators. Tab survives."""
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
# small flock helper (premise.py's pattern; every shared-state RMW uses it)
# ---------------------------------------------------------------------------

class _flocked:
    """flock a STABLE sibling lock file (never a file that atomic-replace
    swaps out under the lock). Fail-open: no lock ⇒ proceed unlocked — the
    guarantee degrades, the operation never dies on the lock."""

    def __init__(self, path):
        self.path, self.f = path, None

    def __enter__(self):
        try:
            import fcntl
            self.f = open(self.path, "a")
            fcntl.flock(self.f.fileno(), fcntl.LOCK_EX)
        except OSError:
            self.f = None
        return self

    def __exit__(self, *exc):
        if self.f is not None:
            try:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self.f.close()
        return False


# ---------------------------------------------------------------------------
# roster (RAM presence) — full row at join; per-seat `.seen` touch on the
# hot path (codex H8 / freeze bar 6: deliver never rewrites shared state)
# ---------------------------------------------------------------------------

def roster_path():
    return os.path.join(chat.chat_dir(), ".roster.json")


def _seat_key(seat):
    """The seat's STATE-FILE key: readable slug + a short hash of the
    casefolded raw seat. pk.slug alone collides ('api.a' and 'api-a' both
    slug to 'api-a'), and a shared cursor lets one seat silently CONSUME the
    other's rows (codex B1 — loss, not a duplicate). Every per-seat state
    path (cursor, cursor lock, seen) derives from this one key; case-only
    variants fold together deliberately — case-insensitive @mentions cannot
    address them apart anyway."""
    import hashlib
    h = hashlib.blake2b(str(seat).casefold().encode("utf-8"),
                        digest_size=4).hexdigest()
    return "%s-%s" % (pk.slug(seat), h)


def seen_path(seat):
    return os.path.join(chat.chat_dir(), ".seen." + _seat_key(seat))


def roster():
    return pk.read_json(roster_path(), {}) or {}


def touch_seen(seat):
    """The hot-path presence beat: utime a per-seat empty file — no shared
    read-modify-write, no lock, no lost sibling rows."""
    p = seen_path(seat)
    try:
        os.utime(p)
    except OSError:
        try:
            with open(p, "w"):
                pass
        except OSError:
            pass


def last_seen(seat, row=None):
    try:
        return os.stat(seen_path(seat)).st_mtime
    except OSError:
        return (row or {}).get("last_seen")


def write_roster(seat, session=None, cwd=None):
    """The one-time (join) roster write — keyed by seat, last-writer-wins
    (two sessions under one HELM_CHAT_NAME share a row AND a cursor —
    documented). Guarded by a lock anyway: joins are rare, losing a sibling
    seat's row at join time is avoidable for one flock."""
    chat._ensure_dir()
    with _flocked(roster_path() + ".lock"):
        r = roster()
        row = r.get(seat) or {}
        if session:
            row["session"] = str(session)
        if cwd:
            row["cwd"] = cwd
            row["project"] = os.path.basename(cwd.rstrip(os.sep)) or cwd
        if not row.get("joined"):
            row["joined"] = pk.now_ts()
        row["last_seen"] = time.time()
        r[seat] = row
        pk.write_json(roster_path(), r)
    touch_seen(seat)
    return row


def seat_for_session(session):
    if not session:
        return None
    for seat, row in roster().items():
        if row.get("session") == str(session):
            return seat
    return None


# ---------------------------------------------------------------------------
# the cursor (codex H5) + the tail scan both deliver and the report use
# ---------------------------------------------------------------------------

def cursor_path(room, seat):
    return os.path.join(chat.chat_dir(),
                        "%s.cursor.%s" % (pk.slug(room), _seat_key(seat)))


def _cursor(room, seat):
    d = pk.read_json(cursor_path(room, seat), None)
    if isinstance(d, dict) and isinstance(d.get("off"), int):
        return d
    return None


def _write_cursor(room, seat, dev, ino, off, rid):
    chat._ensure_dir()
    pk.write_json(cursor_path(room, seat),
                  {"dev": dev, "ino": ino, "off": off, "rid": rid})


def _init_cursor(room, seat):
    """Baseline at the CURRENT end of room — at JOIN time (codex H5.5), so
    everything posted after session start delivers at the first boundary."""
    try:
        st = os.stat(chat.room_path(room))
        _write_cursor(room, seat, st.st_dev, st.st_ino, st.st_size, None)
    except OSError:
        _write_cursor(room, seat, None, None, 0, None)


def _tail(room, cur):
    """Read complete rows from the cursor position onward, byte-accurately,
    off ONE fstat'd fd. -> (dev, ino, base_off, entries) or None when there
    is nothing to read. entries = [(row_dict|None, end_off)] for every
    COMPLETE line (unparseable → row None); a trailing partial line (writer
    mid-append) is never consumed. On rotation/replacement (inode change or
    shrink) the scan restarts at 0 and everything up to AND INCLUDING the
    cursor's last row id — if still present — is trimmed here, with base_off
    moved past it, so the caller can never commit a cursor BEFORE rows it
    already processed (duplicate suppression; duplicates beyond that are
    accepted by law — loss is not)."""
    path = chat.room_path(room)
    try:
        f = open(path, "rb")
    except OSError:
        return None
    with f:
        st = os.fstat(f.fileno())
        off, suppress = cur.get("off", 0), None
        if (st.st_dev, st.st_ino) != (cur.get("dev"), cur.get("ino")) \
                or st.st_size < off:
            off, suppress = 0, cur.get("rid")   # rotation / replacement
        elif st.st_size == off:
            return None                          # genuinely nothing new
        f.seek(off)
        data = f.read(SCAN_CAP)
    entries, pos = [], off
    for chunk in data.split(b"\n"):
        end = pos + len(chunk) + 1
        if end > off + len(data):                # trailing partial line
            break
        row = None
        try:
            v = json.loads(chunk.decode("utf-8", errors="replace"))
            if isinstance(v, dict):
                row = v
        except ValueError:
            pass
        if chunk:
            entries.append((row, end))
        pos = end
    base = off
    if suppress is not None:
        for i, (row, end) in enumerate(entries):
            if row is not None and row.get("id") == suppress:
                base, entries = end, entries[i + 1:]
                break
    return st.st_dev, st.st_ino, base, entries


def deliver(session=None, room="main", seat=None, emit=None):
    """The tool-boundary nudge: at most ONE deliverable row, oldest first;
    later matches stay PENDING (their count shows, their cursor ground is
    not consumed — codex H6). Returns the label line or None.

    At-least-once (codex H7): when `emit` is given it is called with the
    line BEFORE the cursor commits; emit must do its one unbuffered write.
    A kill between emit and commit re-delivers next boundary."""
    if (home.env("CHAT_DELIVER") or "").lower() in ("0", "off", "no"):
        return None
    seat = seat or seat_for_session(session) or derive_seat(session)
    touch_seen(seat)
    with _flocked(cursor_path(room, seat) + ".lock"):
        cur = _cursor(room, seat)
        if cur is None:
            _init_cursor(room, seat)   # a pre-install session self-heals
            if session and seat_for_session(session) is None:
                write_roster(seat, session=session)
            return None
        got = _tail(room, cur)
        if got is None:
            return None
        dev, ino, base, entries = got
        hit = None
        last_end, last_rid = base, cur.get("rid")
        for i, (row, end) in enumerate(entries):
            if row is not None and deliverable(row, seat):
                hit = (i, row, end)
                break
            last_end, last_rid = end, (row or {}).get("id") or last_rid
        if hit is None:
            _write_cursor(room, seat, dev, ino, last_end, last_rid)
            return None
        i, row, end = hit
        waiting = sum(1 for r, _e in entries[i + 1:]
                      if r is not None and deliverable(r, seat))
        line = "[helm chat → %s] %s: %s" % (
            seat, row.get("from") or "?", _clip(_scrub(row.get("text") or "")))
        if waiting:
            line += " (+%d waiting — helm chat read)" % waiting
        if emit is not None:
            emit(line)                  # output FIRST …
        _write_cursor(room, seat, dev, ino, end, row.get("id"))  # … commit after
        return line


# ---------------------------------------------------------------------------
# join (SessionStart) + wait (the beacon)
# ---------------------------------------------------------------------------

def join(session=None, cwd=None, seat=None, room="main"):
    """The autojoin: roster row + cursor initialized HERE + the identity
    line the hook injects as session context. Idempotent per seat."""
    seat = seat or seat_for_session(session) or derive_seat(session)
    write_roster(seat, session=session, cwd=cwd)
    if _cursor(room, seat) is None:
        with _flocked(cursor_path(room, seat) + ".lock"):
            if _cursor(room, seat) is None:
                _init_cursor(room, seat)
    line = ("[helm chat] you are seat '%s' in room %s — @%s and owner posts "
            "reach you between tool calls; speak: helm chat post; catch up: "
            "helm chat read; idle? arm a Monitor on: helm chat wait --seat %s"
            % (seat, room, seat, seat))
    return seat, line


def wait(seat=None, room="main", any_row=False, timeout=None, poll=None,
         emit=None):
    """Block until the next word arrives; returns the line or None on
    timeout. Seat mode IS a delivery (advances the cursor via deliver's
    at-least-once path); --any watches the room without touching cursors.
    Busy-turn parity comes from the PostToolUse hook; an IDLE seat gets
    woken only if it armed a Monitor on this — opt-in by design (M11)."""
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
            line = deliver(room=room, seat=seat, emit=emit)
            if line:
                return line
        if deadline and time.time() >= deadline:
            return None
        time.sleep(poll)


# ---------------------------------------------------------------------------
# claims — the advisory TTL lease (codex C1-lite + H9 hardening)
# ---------------------------------------------------------------------------

def claims_path():
    return os.path.join(chat.chat_dir(), ".claims.json")


def _now_mono():
    return time.monotonic()


def _sweep(c):
    now = _now_mono()
    return {r: v for r, v in c.items()
            if r == "_fence" or (isinstance(v, dict)
                                 and v.get("exp_mono", 0) > now)}


def _binding_ok(row, seat, lease, session):
    """The C1-lite composite check, validated TOGETHER (codex B2): the lease
    nonce is THE capability (printed once, to the grantee, never listed),
    the supplied seat must be the recorded holder, and when both the grant
    and the caller carry a session they must agree. Never lease-OR-session:
    a roster-visible session id alone must open nothing."""
    if not lease or row.get("lease") != lease:
        return False, "the lease id (the grant's capability)"
    if seat != row.get("holder"):
        return False, "the holding seat (%s)" % row.get("holder")
    if row.get("session") and session and row["session"] != str(session):
        return False, "the granting session"
    return True, None


def claim(resource, seat, ttl=DEFAULT_TTL, lease=None, session=None):
    """(ok, message, lease_id). A fresh grant mints a random lease nonce +
    an increasing fence and records the caller's ambient session (display /
    extra binding — never an authorizer). EXTENDING a live lease requires
    the full binding {lease, seat, session-if-recorded}; a display name or
    a copied session id alone extends nothing (codex B2). Expiry is
    monotonic (tmpfs state dies with the boot; wall time only displays).
    Check+sweep+write hold one flock."""
    chat._ensure_dir()
    with _flocked(claims_path() + ".lock"):
        c = _sweep(pk.read_json(claims_path(), {}) or {})
        row = c.get(resource)
        if row:
            ok, needs = _binding_ok(row, seat, lease, session)
            if not ok:
                return False, "%s is held by %s for %ds more (extend needs %s)" % (
                    resource, row.get("holder"),
                    int(row["exp_mono"] - _now_mono()), needs), None
            lease_id, fence = row["lease"], row["fence"]
        else:
            lease_id = os.urandom(8).hex()
            fence = int(c.get("_fence", 0)) + 1
            c["_fence"] = fence
        c[resource] = {"holder": seat, "session": str(session) if session else None,
                       "lease": lease_id, "fence": fence,
                       "exp_mono": _now_mono() + ttl,
                       "exp_wall": time.time() + ttl, "ts": pk.now_ts()}
        pk.write_json(claims_path(), c)
        return True, "%s claimed by %s for %ds (lease %s, fence %d)" % (
            resource, seat, ttl, lease_id, fence), lease_id


def release(resource, seat, lease=None, session=None):
    """(ok, message). Release demands the SAME composite binding as extend —
    {lease capability, holding seat, session-if-recorded}. A stale holder
    whose lease expired-and-was-regranted fails on the fresh nonce (ABA),
    and a caller who copied a session id out of the roster fails on the
    lease (codex B2's exact reproduction)."""
    with _flocked(claims_path() + ".lock"):
        c = _sweep(pk.read_json(claims_path(), {}) or {})
        row = c.get(resource)
        if not row:
            pk.write_json(claims_path(), c)
            return False, "%s is not claimed" % resource
        ok, needs = _binding_ok(row, seat, lease, session)
        if not ok:
            return False, "%s stays held — release needs %s" % (resource, needs)
        del c[resource]
        pk.write_json(claims_path(), c)
        return True, "%s released" % resource


def claims_list():
    """The public table: holder/fence/remaining only — neither the lease
    nonce (the capability) nor the bound session is ever published here."""
    with _flocked(claims_path() + ".lock"):
        c = _sweep(pk.read_json(claims_path(), {}) or {})
        pk.write_json(claims_path(), c) if c else None
    now = _now_mono()
    return [{"resource": r, "holder": v.get("holder"), "fence": v.get("fence"),
             "remaining": int(v.get("exp_mono", now) - now)}
            for r, v in sorted(c.items()) if r != "_fence"]


# ---------------------------------------------------------------------------
# the roster report (CLI table + GET /api/chat/roster + the seats panel)
# ---------------------------------------------------------------------------

def presence_of(ls):
    if not ls:
        return "absent"
    age = time.time() - ls
    return "fresh" if age < FRESH_S else "quiet" if age < QUIET_S else "absent"


def roster_report(room="main"):
    """{"seats": [...], "claims": [...]} — read-only, fail-open by caller.
    Pending is computed from each seat's cursor WITHOUT moving it."""
    seats = []
    for seat, row in sorted(roster().items()):
        cur = _cursor(room, seat)
        pending, preview = 0, None
        got = _tail(room, cur) if cur else None
        if got:
            rows = [r for r, _e in got[3] if r is not None]
            hits = [r for r in rows if deliverable(r, seat)]
            pending = len(hits)
            if hits:
                preview = _scrub(hits[-1].get("text") or "")[:PREVIEW_CHARS]
        ls = last_seen(seat, row)
        seats.append({"seat": seat, "session": row.get("session"),
                      "project": row.get("project"), "cwd": row.get("cwd"),
                      "last_seen": ls, "presence": presence_of(ls),
                      "pending": pending, "preview": preview})
    return {"room": room, "seats": seats, "claims": claims_list()}


# ---------------------------------------------------------------------------
# CLI (dispatched from chat.cmd_chat) + the hook legs
# ---------------------------------------------------------------------------

def _hook_stdin():
    """Bounded hook-JSON read (a pathological stdin must not burn the
    boundary's latency budget)."""
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


def _env_session():
    return os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("CODEX_SESSION_ID")


def _hook_emit(event):
    """ONE unbuffered write of the whole hook response (codex H7): no
    partial stdout can reach the harness, and the cursor commit that follows
    emit() is provably after the output left the process."""
    def emit(line):
        payload = json.dumps({"hookSpecificOutput": {
            "hookEventName": event, "additionalContext": line}}) + "\n"
        os.write(1, payload.encode("utf-8"))
    return emit


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
                _hook_emit("SessionStart")(line)
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
            emit = _hook_emit("PostToolUse") if "--hook-json" in args else print
            deliver(session=session, room=room, seat=_flag(args, "--seat"),
                    emit=emit)
        except Exception:
            pass                    # fail-open: never hold a tool boundary
        return 0
    if verb == "wait":
        timeout = _flag(args, "--timeout")
        line = wait(seat=_flag(args, "--seat"), room=room,
                    any_row="--any" in args,
                    timeout=float(timeout) if timeout else None,
                    emit=None if "--any" in args else print)
        if line is None:
            return 1
        if "--any" in args:
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
            print("  claim: %s -> %s (%ds left, fence %s)" % (
                c["resource"], c["holder"], c["remaining"], c["fence"]))
        return 0
    if verb == "claim":
        if not args:
            print("usage: helm chat claim <resource> [--ttl SECONDS] [--seat S] "
                  "[--lease ID to extend]   (keep the printed lease id — it is "
                  "the release capability)", file=sys.stderr)
            return 2
        # session comes ONLY from the ambient harness env — never a flag: a
        # roster-visible SID must not be assertable through the CLI (codex B2)
        ttl = _flag(args, "--ttl")
        ok, msg, _lease = claim(
            args[0], _flag(args, "--seat") or derive_seat(None),
            ttl=int(ttl) if ttl else DEFAULT_TTL,
            lease=_flag(args, "--lease"), session=_env_session())
        print("helm chat: " + msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if verb == "release":
        if not args:
            print("usage: helm chat release <resource> --lease ID [--seat S]",
                  file=sys.stderr)
            return 2
        ok, msg = release(args[0], _flag(args, "--seat") or derive_seat(None),
                          lease=_flag(args, "--lease"), session=_env_session())
        print("helm chat: " + msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if verb == "claims":
        rows = claims_list()
        if not rows:
            print("helm chat: no live claims")
            return 0
        for c in rows:
            print("  %s -> %s (%ds left, fence %s)" % (
                c["resource"], c["holder"], c["remaining"], c["fence"]))
        return 0
    if verb in ("verdict", "reveal"):
        print("helm chat: council is deferred to 0.3 (codex review — see the "
              "design doc §11); use the room + /premise for now", file=sys.stderr)
        return 2
    print("helm chat: unknown subcommand '%s'" % verb, file=sys.stderr)
    return 2
