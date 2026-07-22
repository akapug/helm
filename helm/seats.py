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
  * join    — SessionStart hook: roster row (RAM presence, keyed on the seat's
              HELM_CHAT_NAME so a seat joins as its family name) + cursor
              INITIALIZED HERE (a message posted between session start and
              the first tool boundary must deliver — codex H5.5) + the seat's
              identity/protocol line as session context. That line DIRECTS the
              agent to arm its idle-wake beacon (a persistent Monitor on
              `helm chat wait --follow`) as a MANDATORY first action — the only
              thing that wakes an idle PTY agent (native-wake-only-agent-armed).
  * deliver — PostToolUse hook: the tool-boundary nudge. At most ONE row per
              boundary, 200-byte clip, control-char scrub, information-not-
              instruction label. AT-LEAST-ONCE, NEVER AT-MOST-ONCE (codex
              H7): the hook response is emitted in ONE unbuffered write and
              the cursor commits only AFTER — a kill in between produces a
              duplicate next boundary, which beats silence. Every fire
              touches the seat's own `.seen` file (presence for free); the
              unchanged-room fast path never rewrites shared state.
  * wait    — the beacon: block until a row addressed to the seat lands
              (Monitor arms it). --follow keeps the room open and streams EACH
              new matching row as one line (one line = one agent wake), never
              returning on a match. NOTE (codex M11): this is busy-turn parity
              plus an idle beacon the join context line makes MANDATORY to arm —
              nothing external can wake an idle PTY agent, so the self-armed
              Monitor is the only path.
  * claims  — advisory TTL lease with session+nonce+fence binding and
              monotonic expiry (the worktree-collision class).
  * stop-guard — Stop hook: the IDLE GATE (buildr/mc arbiter capability,
              helm-native). BLOCKS a stop while undelivered mentions/owner
              rows sit past the seat's cursor (once per pending-fingerprint —
              never an infinite loop) or while THIS session holds a live
              claim lease; WARNs (never blocks) on a clean stop to arm the
              beacon; silently runs `helm index cap --apply`. Fail-open
              total; HELM_STOP_GUARD=0 kills it.

Council (embargoed verdicts) is DEFERRED to 0.3 — the codex round showed a
correct embargo needs an expected-set freeze, a reveal state machine, salted
commitments and batch-row reveal; the 0.3 spec is recorded in the design
doc §11. No live consumer today, so: record, don't build.

Cursor law (codex H5): `<room>.cursor.<seat>[.<sid8>]` holds {dev, ino, off,
rid} — the room file's identity, the byte offset of the first unprocessed
row, and the last processed row's stable id. The cursor is PER (seat,
session): two live sessions sharing one HELM_CHAT_NAME each hold their own
cursor, so an @mention FANS OUT to all of them instead of being race-consumed
by whichever boundary fires first (the live @mention-loss class). A caller
with no session (bare CLI) rides the seat-level cursor; a fresh session
cursor seeds from the seat-level one when it exists (upgrade continuity —
rows tracked before the split are not skipped). Fast path = one stat (same inode, size
== off ⇒ nothing new; a same-size REPLACEMENT changes the inode and is
caught). Inode change or shrink ⇒ rotation/replacement: reset to 0 and use
rid to suppress the retained overlap (duplicates acceptable, loss is not).
All cursor transitions serialize on `<room>.cursor.<seat>.lock`; rows are
selected/committed by byte offset from ONE fstat'd fd, never by line count.
Initialized at JOIN. Every hook-facing path is FAIL-OPEN TOTAL.

MULTI-ROOM (slice 5 — the owner's live helm-dogfood '@opus-integrator' post
woke nothing, 2026-07-21): the lane is not main-scoped. deliver_any (the
PostToolUse hook) and the wait --follow beacon consider EVERY live room —
the seat's private DM lane first, then primary, then newest-activity rooms,
ROOM_SCAN_CAP-bounded — with the same per (seat, room, session) cursor
mechanics per room. A TRACKED seat meeting a cursor-less room BACKFILLS from
offset 0 (a room born after its join is all post-join news — the mention
that created the channel must deliver); an untracked seat keeps the EOF
self-heal everywhere (pre-join backlog never floods). join baselines every
existing room; stop-guard and the roster report read pending across the same
bounded scan.

BEACON SCOPE (premise beacon-scope-mentions-plus-home-room-owner-posts-not-
all — the live bug: codex-2, homed to #main, never saw an @codex-2 mention
posted in #helm-dogfood because homing ALLOWLISTED the scan): the scan
covers every live room; deliverable() applies the scope per row —
  (a) a @seat mention (or a {dm} row naming the seat) surfaces from ANY room,
      always — a direct address is never filtered;
  (b) ANYTHING in the seat's HOME room (roster home_room) surfaces — the
      team channel is full-surface for its own team;
  (c) @all broadcasts surface in {home, main}; owner-rail posts NO LONGER
      auto-wake (owner steer 2026-07-21: mentions + home room are enough) —
      never fleet-wide across every side room;
  (d) a MUTED room (helm chat seat mute <room> — roster row "mute") stops
      (b)/(c) noise at this seat; (a) still surfaces (mute tunes noise,
      never direct address).

DM (premise exact-token-addressee-match): seats.dm() writes ONE row into the
recipient's private lane (chat.dm_room — the `dm-` reserved namespace, a
dm/ subdir file no room list ever shows). The recipient is the EXACT seat
token (a casefold roster snap only — never a substring, never a slug fold:
team.a and team-a are different lanes by key). Delivery/beacon/stop-guard
pick the lane up first in the room scan; nobody else ever scans it.
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
SCAN_CAP = 512 * 1024    # deliver never reads more than this per room
ROOM_SCAN_CAP = 16       # rooms per boundary/beacon pass — the multi-room bound
OWNER_RAILS = ("web", "tui")  # server-side owner surfaces stamp these origins
_BROADCAST = re.compile(r"(?<![A-Za-z0-9._-])@(all|fleet|everyone)(?![A-Za-z0-9._-])", re.I)


# ---------------------------------------------------------------------------
# identity + addressing
# ---------------------------------------------------------------------------

_FAMILIES = ("fable", "opus", "sonnet", "haiku", "kimi", "glm", "gpt",
             "gemini", "deepseek", "qwen", "grok", "mistral", "llama")


def _family():
    """The ambient model family, best-effort: the model env first (seat
    launches export CLAUDE_CODE_SUBAGENT_MODEL), else the harness. Display
    material for the auto-name — never identity, never authorization."""
    model = (os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL")
             or os.environ.get("ANTHROPIC_MODEL") or "").lower()
    for fam in _FAMILIES:
        if fam in model:
            return fam
    if model:
        tok = pk.slug(model).split("-")[0]
        if tok:
            return tok
    if os.environ.get("CODEX_SESSION_ID"):
        return "codex"
    if (os.environ.get("CLAUDE_CODE_SESSION_ID")
            or os.environ.get("CLAUDE_SESSION_ID")
            or os.environ.get("CLAUDECODE")):
        return "claude"
    return "agent"


def auto_name(session, cwd=None):
    """G-stable-names: a MEANINGFUL stable auto-name for an un-named join —
    <project>-<family> ('helm-fable'), deduped with -2/-3… when a DIFFERENT
    session already holds the name. Stable: callers reach here only when the
    roster has no row for this session, and the result is immediately
    roster-bound (join / deliver self-heal), so the same session keeps
    resolving to the same seat. Opaque agent-<sid8> hex (12/15 of the live
    roster before this) is the last-resort floor only."""
    sid = str(session)
    proj = os.path.basename((cwd or "").rstrip(os.sep))
    base = pk.slug("%s-%s" % (proj, _family())) if proj else _family()
    r = roster()

    def taken(name):
        row = r.get(name)
        return bool(row) and row.get("session") != sid \
            and sid not in (row.get("sessions") or [])

    if not taken(base):
        return base
    for i in range(2, 100):
        cand = "%s-%d" % (base, i)
        if not taken(cand):
            return cand
    return "agent-" + sid[:8]


def derive_seat(session=None, cwd=None):
    """$HELM_CHAT_NAME first (the launch seam sets it), else a MEANINGFUL
    stable auto-name for the session (auto_name — project+family, deduped),
    else chat.whoname's law: a bare agent never gets the operator's
    identity."""
    name = home.env("CHAT_NAME")
    if name:
        return name
    if session:
        return auto_name(session, cwd)
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


def seat_scope(seat, r=None):
    """The seat's beacon tuning, one roster read: {"home": room-or-None,
    "mute": set}. Computed ONCE per scan pass and threaded down — the poll
    path stays ~one stat per quiet room."""
    row = ((r if r is not None else roster()).get(seat) or {}) if seat else {}
    return {"home": row.get("home_room"),
            "mute": {pk.slug(x) for x in row.get("mute") or []}}


def deliverable(m, seat, room="main", scope=None):
    """Does this row reach `seat` at a tool boundary, given the ROOM it sits
    in? The beacon-scope law (premise beacon-scope-mentions-plus-home-room-
    owner-posts-not-all), top to bottom:
      * reactions and the seat's own posts: never.
      * a {dm} row: the EXACT-token recipient only (casefold — never a
        substring, never a slug fold), whatever lane it sits in.
      * an @seat mention: ANY room, ALWAYS — checked before mute, because a
        direct address is never noise.
      * a muted room (the seat's roster "mute" list): nothing further.
      * the seat's HOME room (roster home_room): EVERY remaining row — the
        team channel is full-surface for its own team.
      * {home, main}: @all broadcasts only. Owner-rail posts (origin web/tui)
        do NOT wake here (owner steer 2026-07-21: mentions + home-room are
        enough — an owner post reaches a seat via an @mention or its own home
        room, never as a plain main broadcast). NOT fleet-wide: a side room's
        @all drafts nobody homed elsewhere.
      * anything else (foreign-room chatter, incl. non-mention owner posts
        outside home): never (noise law).
    scope=None computes seat_scope here — hot paths pass it precomputed."""
    text = m.get("text")
    if not text or m.get("react"):
        return False
    frm = str(m.get("from") or "")
    if frm == seat:
        return False
    if m.get("dm"):
        # exact-token recipient (casefold only), OR the row sits in the
        # seat's OWN lane — the lane is the routing truth, so a rename's
        # carried-over history (rows naming the old token) still delivers
        return (str(m["dm"]).casefold() == str(seat or "").casefold()
                or (bool(seat) and room == dm_lane(seat)))
    if _mention_re(seat).search(text):
        return True
    sc = scope if scope is not None else seat_scope(seat)
    if room in sc["mute"]:
        return False
    home_r = sc.get("home")
    if home_r and room == home_r:
        return True
    if room != "main" and room != home_r:
        return False
    # @all broadcasts still wake in {home, main}. Owner-rail posts NO LONGER
    # auto-wake (owner steer 2026-07-21: mentions + home-room are enough — an
    # owner post reaches a seat only via an @mention or its own home room, never
    # as a plain main-room broadcast). OWNER_RAILS/owner_names stay for owner
    # IDENTITY (forgery defense) elsewhere; owner-posts are simply not a wake
    # class. bug-class superseded: beacon-owner-post-wake-is-noise.
    return bool(_BROADCAST.search(text))


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


SESSIONS_KEPT = 8   # co-named sessions remembered per roster row (addressing)


def write_roster(seat, session=None, cwd=None, home_room=None):
    """The one-time (join) roster write — keyed by seat. `session` is the
    newest writer; every co-named session is ALSO kept in row["sessions"]
    (newest last, capped) so seat_for_session resolves ALL of them and each
    keeps its own delivery cursor (fan-out, never race-consume). Guarded by a
    lock anyway: joins are rare, losing a sibling seat's row at join time is
    avoidable for one flock. home_room (multi-team isolation, G1): the seat's
    team room from HELM_CHAT_ROOM — recorded once at join; a later re-join
    carrying a DIFFERENT room re-homes (the operator's deliberate move); a
    sessionless/auto roster write (home_room None) NEVER strips it."""
    chat._ensure_dir()
    with _flocked(roster_path() + ".lock"):
        r = roster()
        row = r.get(seat) or {}
        home_room = pk.slug(home_room) if home_room else None
        if home_room and home_room != row.get("home_room"):
            row["home_room"] = home_room
        if session:
            row["session"] = str(session)
            sess = [s for s in row.get("sessions") or [] if s != str(session)]
            sess.append(str(session))
            row["sessions"] = sess[-SESSIONS_KEPT:]
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
    sid = str(session)
    for seat, row in roster().items():
        if row.get("session") == sid or sid in (row.get("sessions") or []):
            return seat
    return None


def _resolve_seat(r, token):
    """A roster key, else the seat whose session (or 8+-char prefix of one)
    matches — how the owner names a live agent they only know by sid."""
    if token in r:
        return token
    t = str(token or "")
    if len(t) >= 8:
        for seat, row in r.items():
            sess = [row.get("session") or ""] + list(row.get("sessions") or [])
            if any(s == t or s.startswith(t) for s in sess if s):
                return seat
    return None


def _move_seat_state(old, new):
    """Carry every state file from the old seat key to the new one — cursors
    (+ per-session variants + locks), .seen, stop latches, every room. The
    tracked delivery ground survives a rename; an EOF re-baseline would be
    silent loss. Fail-open per file."""
    ok, nk = _seat_key(old), _seat_key(new)
    d = chat.chat_dir()
    try:
        os.replace(chat.room_path(chat.DM_PREFIX + ok),   # the private DM lane
                   chat.room_path(chat.DM_PREFIX + nk))   # rides the rename too
    except OSError:
        pass
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        if any(marker + ok in n for marker in (".cursor.", ".seen.", ".stopfp.")):
            try:
                # replace EVERY key occurrence: a dm-lane cursor carries the
                # key twice (dm-<key>.cursor.<key>…) and both must move —
                # the lane file kept its inode, so the cursor stays valid
                os.replace(os.path.join(d, n),
                           os.path.join(d, n.replace(ok, nk)))
            except OSError:
                pass


def rename_seat(old, new):
    """(ok, message). G-stable-names: bind a live agent to a memorable @name.
    `old` is a roster seat name or a session id (full, or an 8+-char prefix).
    Rebinds delivery: the roster row moves (so the hook's session_id resolves
    to the new name) and every keyed state file moves with it. The seat's
    HELM_CHAT_NAME env (if it launched with one) still names the OLD seat —
    the message says so; a beacon armed on the old name must be re-armed."""
    new = (new or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", new):
        return False, ("new name %r must be 1-64 chars of [A-Za-z0-9._-] "
                       "(what an @mention can address)" % new)
    if new.lower() in owner_names() or _BROADCAST.search("@" + new):
        return False, "%r is reserved (an owner/broadcast name)" % new
    with _flocked(roster_path() + ".lock"):
        r = roster()
        seat = _resolve_seat(r, old)
        if seat is None:
            return False, ("no roster row matches %r (a seat name or an "
                           "8+-char session prefix — helm chat seats --all)" % old)
        if seat == new:
            return True, "seat is already named %s" % new
        # case-INSENSITIVE taken-check: _seat_key casefolds, the reserved check
        # lowers, and _mention_re is re.I — a case-variant name (KIMI vs kimi)
        # is the SAME address + the SAME keyed state downstream, so two such
        # rows alias mentions, share presence, and cross-fire the reaper onto
        # the live seat's state (kimi cross-family review, live-probed 2026-07-21).
        # Exclude `seat` itself so a pure self-case-change isn't falsely blocked.
        if any(k != seat and k.casefold() == new.casefold() for k in r):
            return False, ("seat name %r is taken (case-insensitive — the "
                           "roster keys casefold; helm chat seats --all)" % new)
        r[new] = r.pop(seat)
        pk.write_json(roster_path(), r)
        _move_seat_state(seat, new)
    return True, ("seat %s -> %s: @%s now delivers to it. If it armed a "
                  "beacon on the old name, re-arm: Monitor(command: \"helm "
                  "chat wait --seat %s --follow\", persistent: true). A seat "
                  "launched with HELM_CHAT_NAME=%s re-registers the old name "
                  "on its next session — relaunch to make the rename stick "
                  "there." % (seat, new, new, new, seat))


def set_mute(seat, room, on=True):
    """(ok, message) — the seat's own beacon filter (the beacon-scope
    premise's tuning control). A muted room stops surfacing home-room
    chatter / @all at this seat; a direct @seat mention or a DM
    ALWAYS still surfaces — mute tunes noise, never direct address. Stored
    on the roster row so every lane (boundary, beacon, stop-guard, report)
    reads one truth."""
    room = pk.slug(room)
    with _flocked(roster_path() + ".lock"):
        r = roster()
        row = r.get(seat) or {}
        mute = [m for m in row.get("mute") or [] if m != room]
        if on:
            mute.append(room)
        row["mute"] = sorted(mute)
        r[seat] = row
        pk.write_json(roster_path(), r)
    if on:
        return True, ("%s muted for %s — @%s mentions and DMs still surface "
                      "(unmute: helm chat seat unmute %s)"
                      % (room, seat, seat, room))
    return True, "%s unmuted for %s" % (room, seat)


def mutes(seat):
    """The seat's muted rooms, sorted — one roster read."""
    return sorted((roster().get(seat) or {}).get("mute") or [])


# ---------------------------------------------------------------------------
# the cursor (codex H5) + the tail scan both deliver and the report use
# ---------------------------------------------------------------------------

def _sid8(session):
    """The session's cursor-key token — filename-safe, 8 chars, None-safe."""
    return pk.slug(str(session))[:8] if session else None


def cursor_path(room, seat, session=None):
    """PER (seat, session) when a session is known — co-named sessions each
    keep their own cursor (fan-out; the @mention-loss fix). Sessionless
    callers (bare CLI) ride the seat-level file."""
    p = os.path.join(chat.chat_dir(),
                     "%s.cursor.%s" % (pk.slug(room), _seat_key(seat)))
    s8 = _sid8(session)
    return "%s.%s" % (p, s8) if s8 else p


def _cursor(room, seat, session=None):
    d = pk.read_json(cursor_path(room, seat, session), None)
    if isinstance(d, dict) and isinstance(d.get("off"), int):
        return d
    return None


def _write_cursor(room, seat, dev, ino, off, rid, session=None):
    chat._ensure_dir()
    pk.write_json(cursor_path(room, seat, session),
                  {"dev": dev, "ino": ino, "off": off, "rid": rid})


def _init_cursor(room, seat, session=None, at_start=False):
    """Baseline at the CURRENT end of room — at JOIN time (codex H5.5), so
    everything posted after session start delivers at the first boundary.
    A fresh SESSION cursor inherits the seat-level baseline when one exists
    (pre-split installs tracked the seat file; those rows must not be
    skipped by an EOF re-baseline — loss is the one forbidden outcome).
    at_start=True baselines at OFFSET 0 instead (multi-room: a room born
    after the seat joined is all post-join news — the mention that created
    the channel must deliver, not vanish under an EOF baseline), still
    binding the room file's identity so rotation detection holds.
    -> True iff the baseline was inherited (already-tracked ground)."""
    if session:
        base = _cursor(room, seat)
        if base:
            _write_cursor(room, seat, base.get("dev"), base.get("ino"),
                          base["off"], base.get("rid"), session=session)
            return True
    try:
        st = os.stat(chat.room_path(room))
        _write_cursor(room, seat, st.st_dev, st.st_ino,
                      0 if at_start else st.st_size, None, session=session)
    except OSError:
        _write_cursor(room, seat, None, None, 0, None, session=session)
    return False


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


def dm_lane(seat):
    """The seat's own private DM lane, as a reserved-namespace room name."""
    return chat.DM_PREFIX + _seat_key(seat)


def _scan_rooms(primary="main", seat=None, scope=None):
    """Every room the delivery lane considers, bounded: the seat's private
    DM lane first when it exists (a 1:1 word outranks room traffic), then
    the primary room (whether or not its file exists yet), then the other
    live rooms (chat.list_rooms()) newest-activity-first up to ROOM_SCAN_CAP
    — under the cap the ACTIVE channels win, and each room's read is already
    SCAN_CAP-bounded. Fail-open: an unlistable dir is just the primary.

    The scan is deliberately scope-BLIND (premise beacon-scope-mentions-
    plus-home-room-owner-posts-not-all superseded the G1-G3 homing
    allowlist): an @mention anywhere must surface, so every room is scanned
    and deliverable() applies the per-row scope — a muted or foreign room's
    non-mention rows just advance that room's cursor quietly. DM lanes other
    than the seat's own are invisible here (list_rooms never shows them).
    The seat's HOME room and main are PINNED into the scan when they exist:
    foreign-room volume must never evict the seat's own channel or the
    owner's all-hands from the bounded window (the starvation class)."""
    rooms = []
    if seat:
        lane = dm_lane(seat)
        try:
            if os.path.exists(chat.room_path(lane)):
                rooms.append(lane)
        except OSError:
            pass
    rooms.append(primary)
    if seat:
        sc = scope if scope is not None else seat_scope(seat)
        pins = [sc.get("home"), "main"]
    else:
        pins = ["main"]
    seen = {pk.slug(r) for r in rooms}
    for p in pins:
        if p and p not in seen and os.path.exists(chat.room_path(p)):
            rooms.append(p)
            seen.add(p)
    try:
        names = chat.list_rooms()
    except OSError:
        names = []
    others = [n for n in names if n not in seen]
    if others:
        def mtime(n):
            try:
                return os.stat(chat.room_path(n)).st_mtime
            except OSError:
                return 0.0
        others.sort(key=mtime, reverse=True)
    return rooms + others[:ROOM_SCAN_CAP - 1]


def _room_dirty(room, seat, session=None):
    """Lock-free precheck: could `room` hold rows past the (seat, session)
    cursor? A missing cursor is dirty (a room this seat has never looked
    at). Otherwise ONE stat against the atomically-written cursor: same
    file identity and size == off ⇒ clean. False positives are fine
    (deliver re-checks under the lock); a false negative cannot happen —
    an append grows the size, a rotation/replacement changes the inode.
    This keeps the every-tool-call hot path at ~one stat per quiet room."""
    cur = _cursor(room, seat, session) or _cursor(room, seat)
    if cur is None:
        return True
    try:
        st = os.stat(chat.room_path(room))
    except OSError:
        return False                        # no room file: nothing to deliver
    return (st.st_dev, st.st_ino) != (cur.get("dev"), cur.get("ino")) \
        or st.st_size != cur.get("off")


def deliver(session=None, room="main", seat=None, emit=None, cwd=None,
            backfill=False, scope=None):
    """The tool-boundary nudge, ONE room: at most ONE deliverable row, oldest
    first; later matches stay PENDING (their count shows, their cursor ground
    is not consumed — codex H6). Returns the label line or None.

    At-least-once (codex H7): when `emit` is given it is called with the
    line BEFORE the cursor commits; emit must do its one unbuffered write.
    A kill between emit and commit re-delivers next boundary.

    Fan-out: the cursor is per (seat, session) — every co-named session sees
    the same @mention on its own boundary; consuming here never starves a
    sibling session (at-most-once BETWEEN co-named sessions was the bug).

    backfill=True (deliver_any's tracked-seat path) makes a MISSING cursor
    baseline at offset 0 and scan THIS boundary — the multi-room law for a
    room born after the seat joined; default keeps the EOF self-heal."""
    seat = seat or seat_for_session(session) or derive_seat(session, cwd)
    touch_seen(seat)                       # presence FIRST — a seat muted by the
    if (home.env("CHAT_DELIVER") or "").lower() in ("0", "off", "no"):
        return None                        # kill-switch below is still ALIVE:
                                           # keep its row fresh so it isn't reaped
    sc = scope if scope is not None else seat_scope(seat)
    with _flocked(cursor_path(room, seat, session) + ".lock"):
        cur = _cursor(room, seat, session)
        if cur is None:
            inherited = _init_cursor(room, seat, session, at_start=backfill)
            if session and seat_for_session(session) is None:
                write_roster(seat, session=session)
            if not inherited and not backfill:
                return None       # fresh EOF baseline: backlog never floods
            cur = _cursor(room, seat, session)  # already-tracked ground —
            if cur is None:                     # deliver from it THIS boundary
                return None
        got = _tail(room, cur)
        if got is None:
            return None
        dev, ino, base, entries = got
        hit = None
        last_end, last_rid = base, cur.get("rid")
        for i, (row, end) in enumerate(entries):
            if row is not None and deliverable(row, seat, room, sc):
                hit = (i, row, end)
                break
            last_end, last_rid = end, (row or {}).get("id") or last_rid
        if hit is None:
            _write_cursor(room, seat, dev, ino, last_end, last_rid,
                          session=session)
            return None
        i, row, end = hit
        waiting = sum(1 for r, _e in entries[i + 1:]
                      if r is not None and deliverable(r, seat, room, sc))
        is_dm = room.startswith(chat.DM_PREFIX)           # a DM is a DM on
        where = (" dm" if is_dm                           # every surface —
                 else "" if room == "main" else " #%s" % room)  # never a room
        line = "[helm chat%s → %s] %s: %s" % (            # the reply must land
            where, seat, row.get("from") or "?",          # where the word came
            _clip(_scrub(row.get("text") or "")))
        if waiting:
            line += " (+%d waiting — helm chat read%s)" % (
                waiting, " --dm" if is_dm
                else "" if room == "main" else " --room %s" % room)
        if emit is not None:
            emit(line)                  # output FIRST …
        _write_cursor(room, seat, dev, ino, end, row.get("id"),
                      session=session)  # … commit after
        return line


def deliver_any(session=None, seat=None, emit=None, cwd=None, room="main"):
    """The MULTI-ROOM boundary nudge (slice 5 — what the PostToolUse hook and
    the beacon actually call): one deliverable row per boundary from the
    first room that has one — the primary room first, then the rest of
    _scan_rooms' bounded, newest-activity-first list. An @mention in a
    channel the seat never joined must wake it (the owner's helm-dogfood
    '@opus-integrator' post, live 2026-07-21), so a TRACKED seat — it holds
    a primary-room cursor — meeting a cursor-less room BACKFILLS from offset
    0: a room born after its join is all post-join news. An UNtracked seat
    (never joined / reaped / pre-install) keeps the EOF self-heal everywhere:
    pre-join backlog never floods. Scanning a clean room advances only that
    room's cursor; a hit STOPS the scan, so later rooms keep their pending
    for the next boundary (one nudge per boundary — the budget stays flat).
    Exceptions propagate exactly like deliver's (H7: an emit that died must
    not commit); every caller already wraps fail-open."""
    seat = seat or seat_for_session(session) or derive_seat(session, cwd)
    touch_seen(seat)          # presence even when every room is quiet
    tracked = (_cursor(room, seat, session) or _cursor(room, seat)) is not None
    sc = seat_scope(seat)     # ONE roster read for the whole pass
    for r in _scan_rooms(room, seat=seat, scope=sc):
        if not _room_dirty(r, seat, session):
            continue
        # the DM lane ALWAYS backfills from 0 — every row in it is addressed
        # to this seat, so even an untracked (reaped/pre-install) seat must
        # get the DM that created its lane, never an EOF skip
        line = deliver(session=session, room=r, seat=seat, emit=emit, cwd=cwd,
                       backfill=(tracked and r != room)
                       or r.startswith(chat.DM_PREFIX), scope=sc)
        if line:
            return line
    return None


_SEAT_TOKEN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")   # what an @mention can say


def dm(to, text, who=None, session=None, profile=None, sign=None, origin=None):
    """One TRUE 1:1 message -> (row, None) or (None, reason). The recipient
    is the EXACT seat token (premise exact-token-addressee-match): the only
    resolution ever applied is a casefold snap onto a live roster key —
    never a substring, never a slug fold (team.a and team-a are different
    addressees with different lanes). The row lands in the recipient's
    private lane only (chat.post dm= — no room fanout by construction),
    signs like any post, and the recipient's beacon/boundary surfaces it
    first in the scan. A DM to a not-yet-joined seat waits in its lane; the
    join baselines that lane at 0, so it delivers."""
    to = (to or "").strip().lstrip("@")
    if not _SEAT_TOKEN.match(to):
        return None, ("recipient %r must be 1-64 chars of [A-Za-z0-9._-] — "
                      "the exact seat token" % to)
    r = roster()
    if to not in r:
        hits = [k for k in r if k.casefold() == to.casefold()]
        if len(hits) == 1:
            to = hits[0]        # case-snap to the live seat, nothing looser
    sender = who or seat_for_session(session) or derive_seat(session)
    if str(sender).casefold() == to.casefold():
        return None, "a DM to yourself would never deliver (own posts don't)"
    return chat.post(text, who=sender, profile=profile, sign=sign,
                     origin=origin, dm=to), None


# ---------------------------------------------------------------------------
# join (SessionStart) + wait (the beacon)
# ---------------------------------------------------------------------------

def join(session=None, cwd=None, seat=None, room="main"):
    """The autojoin: roster row + cursor initialized HERE + the identity line
    the hook injects as session context. The line DIRECTS the agent to arm its
    idle-wake beacon as a mandatory FIRST action — a self-armed Monitor is the
    only thing that can wake an idle PTY agent (native-wake-only-agent-armed),
    so a SessionStart directive is the strongest enforcement available.
    Idempotent per seat. Baselines a cursor in every room `_scan_rooms` admits
    (all live rooms for legacy un-homed seats; {home, main} for homed seats),
    so pre-join backlog never floods and later admitted rooms can backfill."""
    seat = seat or seat_for_session(session) or derive_seat(session, cwd)
    # Hooks carry HELM_CHAT_ROOM; direct `helm chat join --room team-x` and
    # `helm launch --room team-x` carry the explicit argument instead. The
    # implicit main default stays un-homed for backward-compatible all-room
    # delivery until an operator deliberately homes the seat.
    home_room = home.env("CHAT_ROOM") or (room if room != "main" else None)
    home_room = pk.slug(home_room) if home_room else None
    write_roster(seat, session=session, cwd=cwd, home_room=home_room)
    lane = dm_lane(seat)
    for r in _scan_rooms(room, seat=seat):
        # the seat's DM lane baselines at offset 0 — every row in it is
        # addressed to THIS seat by construction, so a DM sent before the
        # join must deliver, never vanish under an EOF baseline
        at0 = r == lane
        if _cursor(r, seat, session) is None:
            with _flocked(cursor_path(r, seat, session) + ".lock"):
                if _cursor(r, seat, session) is None:
                    _init_cursor(r, seat, session, at_start=at0)
        if session and _cursor(r, seat) is None:
            # the seat-level baseline too: sessionless callers (bare CLI wait/
            # deliver) must not start blind just because the join was hook-keyed
            with _flocked(cursor_path(r, seat) + ".lock"):
                if _cursor(r, seat) is None:
                    _init_cursor(r, seat, at_start=at0)
    scope = ("; everything in your home room %s also wakes you (mute/filter it "
             "if noisy)" % home_room if home_room and home_room != "main"
             else "")
    line = ("[helm chat] you are seat '%s' in room %s — @%s mentions and DMs "
            "(from ANY room) reach you between tool calls%s; owner posts do NOT "
            "wake you unless they @mention you or land in your home room; "
            "speak: helm chat post; catch up: helm chat read. MANDATORY FIRST "
            "ACTION: arm your inbox beacon so "
            "you wake on an @%s mention or a DM even while idle "
            "— Monitor(command: \"helm chat wait --seat %s --follow\", "
            "persistent: true). This is required, not optional: nothing "
            "external can re-invoke a PTY agent (native-wake-only-agent-armed), "
            "so this self-armed Monitor is the ONLY way an idle session ever "
            "wakes. Arm it before anything else."
            % (seat, room, seat, scope, seat, seat))
    return seat, line


def _emit_line(line):
    """The --follow beacon sink: print + FLUSH. The reader is a Monitor pipe,
    where bare print() block-buffers — an unflushed wake-line never reaches the
    agent. flush per line = one emitted row, one immediate agent wake."""
    print(line, flush=True)


def wait(seat=None, room="main", any_row=False, timeout=None, poll=None,
         emit=None, follow=False, session=None):
    """Block until the next word arrives; returns the line or None on
    timeout. Seat mode IS a delivery (advances the cursor via deliver's
    at-least-once path); --any watches the room without touching cursors.
    Busy-turn parity comes from the PostToolUse hook; an IDLE seat gets
    woken only if it armed a Monitor on this — opt-in by design (M11).

    --follow (the idle-wake beacon) NEVER returns on a match: it streams EACH
    new matching row as one emitted line — one Monitor line = one agent wake —
    reusing the delivery address filter (mentions of the seat + DMs + home
    room + @all; owner-rail posts no longer auto-wake),
    and returns only on timeout (a persistent Monitor passes no timeout, so it
    runs forever). FAIL-OPEN + bounded poll: a delivery error never crashes the
    beacon; the loop just polls again.

    MULTI-ROOM: seat mode rides deliver_any — `room` is the PRIMARY room, and
    a matching row in ANY live room (a channel the seat never joined included)
    wakes the seat, per-room cursor per (seat, room, session) so the boundary
    hook and the beacon never double-deliver. --any stays one room's tap."""
    poll = chat.POLL_S if poll is None else poll
    deadline = time.time() + timeout if timeout else None
    # the ambient session (CLI leg passes _env_session()) keys the SAME
    # per-session cursor the boundary hook advances — one session, one
    # cursor, whichever channel fires first; co-named siblings unaffected.
    seat = seat or seat_for_session(session) or derive_seat(session)
    # single-shot keeps its contract: emit stays as passed (None ⇒ deliver
    # returns the line without emitting). --follow always needs a sink to stream
    # through, so it defaults to a PER-LINE-FLUSHED print: the beacon's reader
    # is a Monitor (a PIPE), and bare print() is block-buffered to a pipe — the
    # wake-line would sit unflushed and the agent would never wake (the beacon
    # worked in a tty, dead through Monitor). flush=True = one line, one wake.
    stream = emit or (_emit_line if follow else emit)
    since = chat.read(room)[1] if any_row else None
    while True:
        if any_row:
            rows, total = chat.read(room, since)  # read() self-heals since>total
            if rows:
                if not follow:
                    return chat._fmt(rows[0])
                for m in rows:
                    stream(chat._fmt(m))
            since = total
        else:
            while True:                     # drain all currently-matching rows
                try:                        # across EVERY room (multi-room)
                    line = deliver_any(session=session, seat=seat,
                                       emit=stream, room=room)
                except Exception:
                    line = None             # fail-open: never crash the beacon
                if not line:
                    break
                if not follow:
                    return line             # single-shot: first match wins
        if deadline and time.time() >= deadline:
            return None
        time.sleep(poll)


# ---------------------------------------------------------------------------
# stop-guard (Stop hook) — the idle gate. buildr/mc capability, helm-native:
# an agent must not idle past its inbox or walk away holding a lease. Arbiter
# shape (buildr-stop-arbiter law): resolve posture ONCE, inline checks against
# it, surface ALL blocking messages in ONE exit-2 (fix everything in one
# shot); WARN lines ride along without changing the exit. Block-once-per-
# pending-fingerprint (guard-stop-inbox-beacon law): the FIRST stop on a given
# pending-row set blocks and points; a re-stop on the SAME rows passes —
# never an infinite block loop — and any new row re-arms the block. The hook
# JSON's stop_hook_active flag (the harness's own already-continuing signal)
# is honored the same way. FAIL-OPEN TOTAL: a broken guard must never wedge
# the fleet (cmd wraps everything; kill-switch HELM_STOP_GUARD=0, per-check
# HELM_STOP_GUARD_INBOX/CLAIMS/INDEX=0). Bounded reads (the cursor tail's
# SCAN_CAP), no network.
# ---------------------------------------------------------------------------

def _stop_fp_path(room, seat, session=None, kind="stopfp"):
    """The once-per-fingerprint latch — in the room dir, per (seat, session)
    like the cursor it gates (RAM-side, dies with the boot like the rest of
    the lane's state). kind names the latch lane: stopfp (the inbox block),
    stopwhisper (the contextual-continuation lane's fired-set)."""
    p = os.path.join(chat.chat_dir(),
                     "%s.%s.%s" % (pk.slug(room), kind, _seat_key(seat)))
    s8 = _sid8(session)
    return "%s.%s" % (p, s8) if s8 else p


def _rows_fp(pending):
    """The pending-set fingerprint — blake2b over (room, row-id), the ONE
    identity both the inbox block and the stop-whisper's unlanded leg latch
    on (they must agree on what 'the same rows' means)."""
    import hashlib
    return hashlib.blake2b(
        "|".join("%s:%s" % (rm, r.get("id") or chat.rkey(r))
                 for rm, r in pending).encode("utf-8"),
        digest_size=16).hexdigest()


def _pending_rows(room, seat, session=None, backfill=False, scope=None):
    """Deliverable rows past the (seat, session) cursor WITHOUT consuming
    them — roster_report's read pattern (the cursor never moves here; the
    stop-guard is a gate, not a delivery). Falls back to the seat-level
    cursor when the session has none yet (pre-install sessions). backfill
    mirrors deliver_any's tracked-seat law: a cursor-less room reads from
    offset 0 — the gate and the lane must agree on what is pending."""
    cur = _cursor(room, seat, session) or _cursor(room, seat)
    if cur is None:
        if not backfill:
            return []
        cur = {"off": 0}
    got = _tail(room, cur)
    if not got:
        return []
    sc = scope if scope is not None else seat_scope(seat)
    return [r for r, _e in got[3]
            if r is not None and deliverable(r, seat, room, sc)]


def _pending_all(room, seat, session=None):
    """[(room, row)] pending across the bounded room scan, cursors untouched
    — the stop-guard's and roster report's multi-room truth. Same tracked/
    backfill rule as deliver_any, same _room_dirty fast path per room, same
    one-roster-read scope."""
    tracked = (_cursor(room, seat, session) or _cursor(room, seat)) is not None
    sc = seat_scope(seat)
    out = []
    for r in _scan_rooms(room, seat=seat, scope=sc):
        if not _room_dirty(r, seat, session):
            continue
        out.extend((r, row) for row in _pending_rows(
            r, seat, session, scope=sc,
            backfill=(tracked and r != room) or r.startswith(chat.DM_PREFIX)))
    return out


def _off(name):
    return (home.env(name) or "").lower() in ("0", "off", "no")


# ── stop-whisper: the CONTEXTUAL continuation lane ─────────────────────────
# Lineage: per-toolcall-whispers-are-the-goal (contextual injection is the END
# GOAL; the cure for slop is BUDGETS — bytes caps, contextual gating,
# fail-closed-to-nothing — never removal) + the mc work-arbiter's hold-once-
# per-fingerprint-then-release + reflex.py's counter thresholds (field-tested
# 3/8) and salience law. A Stop hook's only agent-visible channel is the
# block reason (exit 2 stderr), so a whisper IS a soft hold: it fires ONCE
# per (signal, level) fingerprint with the right continuation, and the very
# next stop on the same state passes — never an infinite hold, never
# wallpaper. ONE budgeted line per stop (STOP_WHISPER_CAP), highest-salience
# unlatched signal wins, each line ends in a pull-depth pointer (tiny nudge,
# depth on demand — contextual-routing-preserves-lightness).

STOP_WHISPER_CAP = 240   # one line's byte budget (inject.py WHISPER_CAP kin)
_WHISPER_FIRED_CAP = 20  # fired-set entries kept per (seat, session) latch

STUCK_AT = 3    # reflex.py stuck-commonsense threshold (re-fires per bucket)
DIRTY_AT = 8    # reflex.py uncommitted-drift threshold
PENDING_STALE_S = 600  # unlanded rows must have AGED to whisper — a fresh set
                       # was just pointed at by the inbox block (echo ≠ context)
RUNNER_TAIL_ROWS = 40  # bounded command-log lookback (newest rows win)

# Code-ish edit targets only — a doc-only session must never arm the verify
# rungs (specificity law: a whisper that fires on prose edits is wallpaper).
_CODE_EDIT_RE = re.compile(
    r"\.(py|pyi|ts|tsx|js|jsx|mjs|cjs|rs|go|rb|sh|bash|zsh|c|h|cc|cpp|hpp"
    r"|java|kt|kts|swift|php|pl|lua|sql|proto|toml|yaml|yml|json)$", re.I)


def _ask_candidate():
    """The OWNER-ASK rung — TOP of the salience ladder. One cheap local read
    of the owner-ask ledger (ownerasks.py): any row not yet REPORTED to the
    owner (open OR done-but-unreported — `done` without `report` stays open,
    owner-surface-is-the-bar) whispers the OLDEST such ask, never the list
    (one ask per whisper — no wallpaper). The fp carries the row's status as
    its level, so an open→done transition re-fires exactly once. Fail-closed
    to None: ledger trouble = silence."""
    try:
        from . import ownerasks
        r = ownerasks.oldest_unreported()
        if not r:
            return None
        return ("ask:%s:%s" % (r.get("id"), r.get("status")),
                "owner ask %s is %s: '%s' — report it to the owner (then: "
                "helm asks report %s <chat-post-id>)"
                % (r.get("id"),
                   "done-UNREPORTED" if r.get("status") == "done" else "open",
                   _clip(_scrub(str(r.get("ask") or "")), 48), r.get("id")))
    except Exception:
        return None


def _runner_latest(session):
    """{token: row} — the LATEST recorded run per test-runner token from the
    session's command-log tail (record.py's verify-grounding log: REAL exit
    codes, token + digest, never raw command lines). One bounded read
    (RUNNER_TAIL_ROWS newest rows; the log itself rotates at 1MB). {} on any
    trouble or no session — which fails every gate rung CLOSED to silence."""
    if not session:
        return {}
    try:
        from . import record
        p = os.path.join(record.session_dir(session), "command-log.jsonl")
        with open(p, encoding="utf-8") as f:
            tail = f.readlines()[-RUNNER_TAIL_ROWS:]
    except Exception:
        return {}
    latest = {}
    for ln in tail:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("token"):
            latest[str(r["token"])] = r
    return latest


def _edited_code(session):
    """Basenames of CODE-ish files this session actually edited (record.py's
    edit-targets log — real landed edits, failed ones never appended). A
    doc-only session returns [] and never arms the verify rungs. [] on any
    trouble = fail-closed."""
    if not session:
        return []
    try:
        from . import record
        p = os.path.join(record.session_dir(session), "edit-targets.log")
        with open(p, encoding="utf-8") as f:
            names = [ln.strip() for ln in f]
    except Exception:
        return []
    return [n for n in names if n and _CODE_EDIT_RE.search(n)]


def _gate_candidate(latest):
    """The RED-GATE rung: a test/gate RAN this session and its LATEST run is
    NOT green (exit > 0; -1 = interrupted, no verdict, never red). Stopping
    on a known-red gate is exactly the premature stop this lane exists to
    catch. fp = digest:exit — the same red state whispers once; a NEW red
    run (new digest or exit) re-arms; a green rerun silences it for good."""
    red = [r for r in latest.values()
           if isinstance(r.get("exit"), int) and r["exit"] > 0]
    if not red:
        return None
    r = max(red, key=lambda x: x.get("ts") or 0)
    return ("redgate:%s:%s" % (r.get("digest"), r["exit"]),
            "gate ran RED — `%s` exited %s with no green rerun since; fix or "
            "surface it before stopping (pull: rerun that gate)"
            % (_clip(_scrub(str(r.get("token") or "?")), 40), r["exit"]))


def _unverified_candidate(dirty, edits, latest):
    """The UNVERIFIED rung: code edits landed, tree still dirty, and NO
    test/gate ran this session at all — `compiles` ≠ done; stopping here is
    stopping before the work was ever proven. Level bucket escalates per
    DIRTY_AT further edits (reflex escalate law), so one whisper per stretch,
    never wallpaper."""
    if not (dirty and edits) or latest:
        return None
    return ("unverified:%d" % (len(edits) // DIRTY_AT),
            "%d code edit(s) landed with NO test/gate run this session — run "
            "the gate before stopping (pull: git diff --stat, then the suite)"
            % len(edits))


def _unbanked_candidate(dirty, edits, latest):
    """The UNBANKED-GREEN rung: edits landed, EVERY latest gate run is green,
    tree still dirty — the next step is unambiguous: commit. The sharper,
    earlier cousin of the dirty-streak rung (no eight-op wait when the state
    already reads 'proven green, unbanked'). fp = the newest green digest:
    each newly-proven green state whispers once."""
    if not (dirty and edits and latest):
        return None
    if any(not (isinstance(r.get("exit"), int) and r["exit"] == 0)
           for r in latest.values()):
        return None   # a red/no-verdict gate stands — the red rung owns this stop
    g = max(latest.values(), key=lambda x: x.get("ts") or 0)
    return ("unbanked:%s" % g.get("digest"),
            "gate GREEN (`%s`) but the tree is dirty — bank the proven slice "
            "(pull: git add -A && git commit)"
            % _clip(_scrub(str(g.get("token") or "?")), 40))


def _whisper_candidates(session, pending, inbox_blocked):
    """[(fp, line)] of LIVE whisper signals, salience-ordered: owner-ask >
    stuck > red-gate > stale-pending > unverified > unbanked-green > dirty.
    Signals are cheap local reads only (reflex law): the session's record.py
    counters + verify-grounding logs (command-log/edit-targets) + the pending
    rows the guard already computed. Each fp carries a LEVEL bucket so a
    worsening streak re-fires (reflex escalate law) and a new pending set,
    red run, or green state re-arms."""
    out = []
    ask = _ask_candidate()   # owner-ask rung: unsurfaced owner debt outranks all
    if ask:
        out.append(ask)
    c = {}
    if session:
        try:
            from . import record
            got = record.counters(session)
            c = got if isinstance(got, dict) else {}
        except Exception:
            c = {}

    def n(k):
        try:
            return int(c.get(k) or 0)
        except (TypeError, ValueError):
            return 0

    stuck, dirty = n("stuck-streak"), n("dirty-streak")
    if stuck >= STUCK_AT:
        out.append(("stuck:%d" % (stuck // STUCK_AT),
                    "stopping while wedged — %d repeated infra/auth failures "
                    "this session; surface the blocker or check creds before "
                    "idling (pull: helm reflex smoke --session %s)"
                    % (stuck, session)))
    # the verify-grounding rungs (slice 2): one bounded read of record.py's
    # command-log + edit-targets — red gate > (…pending…) > unverified >
    # unbanked-green, each mutually exclusive by construction.
    latest = _runner_latest(session)
    edits = _edited_code(session)
    dirty_now = bool(c.get("last-dirty"))
    gate = _gate_candidate(latest)
    if gate:
        out.append(gate)
    if pending and not inbox_blocked:
        try:  # STALE rows only — reflex._fresh fails open to fresh, which
            from . import reflex  # fails the whisper CLOSED (silence) here
            stale = [(rm, r) for rm, r in pending
                     if not reflex._fresh(r.get("ts"), PENDING_STALE_S)]
        except Exception:
            stale = []
        if stale:
            out.append(("pending:" + _rows_fp(stale),
                        "%d owner/mention row(s) unlanded >%dm (pointed-at "
                        "once, no longer re-blocking) — land or explicitly "
                        "route them (pull: helm chat read)"
                        % (len(stale), PENDING_STALE_S // 60)))
    uv = _unverified_candidate(dirty_now, edits, latest)
    if uv:
        out.append(uv)
    ub = _unbanked_candidate(dirty_now, edits, latest)
    if ub:
        out.append(ub)
    if dirty >= DIRTY_AT:
        out.append(("dirty:%d" % (dirty // DIRTY_AT),
                    "%d dirtying ops with no commit at stop — bank the green "
                    "slice before idling; hot context is fuel (pull: git "
                    "status, then commit)" % dirty))
    return out


def _stop_whisper(session, room, seat, pending, inbox_blocked):
    """ONE budgeted contextual continuation for this stop, or None. The
    highest-salience signal whose (signal, level) fingerprint has NOT fired
    for this (seat, session) wins; firing latches it (fired-set JSON, capped)
    and appends one measurability row to the stop-whisper ledger (ids only,
    never text — the fire-ledger law). FAIL-CLOSED TO NOTHING: any state or
    ledger trouble yields silence, never a raise, never a louder lane."""
    cands = _whisper_candidates(session, pending, inbox_blocked)
    if not cands:
        return None
    path = _stop_fp_path(room, seat, session, kind="stopwhisper")
    d = pk.read_json(path, {}) or {}
    fired = [str(x) for x in d.get("fired") or []] if isinstance(d, dict) else []
    hit = next(((fp, line) for fp, line in cands if fp not in fired), None)
    if not hit:
        return None
    fp, line = hit
    try:
        chat._ensure_dir()
        pk.write_json(path, {"v": 1, "ts": pk.now_ts(),
                             "fired": (fired + [fp])[-_WHISPER_FIRED_CAP:]})
    except Exception:
        return None   # an unlatchable whisper would repeat forever — stay silent
    try:  # measurability rides the fire (fail-open; ids only)
        from . import inject
        inject._append_jsonl(
            os.path.join(home.global_dir(), ".state", "stop-whisper-ledger.jsonl"),
            {"v": 1, "ts": pk.now_ts(), "id": fp, "seat": seat,
             **({"session": str(session)} if session else {})},
            inject.LEDGER_MAX)
    except Exception:
        pass
    return _clip("[helm stop-whisper] " + line +
                 " This holds once per state — a re-stop passes.",
                 STOP_WHISPER_CAP)


def stop_guard(session=None, room="main", seat=None, stop_active=False):
    """-> (blocks, warns) for one Stop event. Posture resolved once (seat via
    the roster's session mapping, else the derived seat); checks are inline:
      (a) BLOCK — undelivered @mentions/owner rows past the seat's cursor,
          once per pending-fingerprint (blake2b of the pending row ids,
          latched in the room dir); a re-stop on the SAME rows passes.
      (b) BLOCK — live claim leases held by THIS session (session-bound: no
          session in the hook JSON ⇒ no claims check — a display name alone
          must never gate a stop).
      (c) WHISPER — the contextual continuation lane (_stop_whisper): ONE
          budgeted nudge from the live signals (stuck/dirty counters, the
          verify-grounding rungs — red gate, unverified edits, unbanked
          green — and the latched-but-unlanded pending set), once per (signal, level)
          fingerprint, riding an existing block or soft-holding alone;
          HELM_STOP_GUARD_WHISPER=0 disables; fail-closed to nothing.
      (d) WARN — clean stop: one line reminding to arm the idle-wake beacon.
      (e) silent mechanical — `helm index cap --apply` best-effort in-process
          (the documented Stop line, docs/VERBS.md): never blocks, never
          prints; HELM_STOP_GUARD_INDEX=0 disables.
    stop_active (the hook JSON's stop_hook_active) short-circuits everything:
    the harness is already continuing off a stop hook — blocking again is the
    infinite-loop shape both reference guards exist to prevent."""
    if _off("STOP_GUARD") or stop_active:
        return [], []
    seat = seat or seat_for_session(session) or derive_seat(session)
    blocks, warns, pending = [], [], []
    inbox_blocked = False

    if not _off("STOP_GUARD_INBOX"):
        pending = _pending_all(room, seat, session)   # EVERY room's inbox gates
        if pending:
            fp = _rows_fp(pending)
            fpp = _stop_fp_path(room, seat, session)
            try:
                with open(fpp) as f:
                    last = f.read().strip()
            except OSError:
                last = None
            if last != fp:
                try:
                    chat._ensure_dir()
                    pk.atomic_write(fpp, fp)
                except OSError:
                    pass  # latch write failing must not kill the guard
                lines = ["  %s%s: %s" % (
                    "[dm] " if rm.startswith(chat.DM_PREFIX)
                    else "" if rm == room else "[#%s] " % rm,
                    r.get("from") or "?",
                    _clip(_scrub(r.get("text") or ""), 120))
                         for rm, r in pending[:5]]
                if len(pending) > 5:
                    lines.append("  ... %d more" % (len(pending) - 5))
                blocks.append(
                    "[helm stop-guard] %d undelivered message(s) for seat "
                    "'%s':\n%s\naddress these before stopping (helm chat "
                    "read). This blocks once per pending set — a re-stop on "
                    "the same rows passes." % (len(pending), seat,
                                               "\n".join(lines)))
                inbox_blocked = True

    if session and not _off("STOP_GUARD_CLAIMS"):
        c = _sweep(pk.read_json(claims_path(), {}) or {})
        now = _now_mono()
        held = ["%s (%ds left)" % (r, int(v.get("exp_mono", now) - now))
                for r, v in sorted(c.items())
                if r != "_fence" and isinstance(v, dict)
                and v.get("session") == str(session)]
        if held:
            blocks.append(
                "[helm stop-guard] live claim lease(s) held by this session: "
                "%s — release them (helm chat release <resource> --lease "
                "<id>) or finish the work before stopping." % ", ".join(held))

    if not _off("STOP_GUARD_WHISPER"):
        try:  # fail-closed to NOTHING: whisper trouble = silence, never louder
            w = _stop_whisper(session, room, seat, pending, inbox_blocked)
        except Exception:
            w = None
        if w:
            blocks.append(w)   # rides an existing block, or IS the soft hold

    if not blocks and not pending:   # genuinely clean — a latched-pass (rows
        warns.append(                # still pending, already pointed at) stays
            "[helm stop-guard] inbox clean. If you intend to idle-wait, arm "
            "the beacon first: Monitor(command: \"helm chat wait --seat %s "
            "--follow\", persistent: true)" % seat)  # silent, never "clean"

    if not _off("STOP_GUARD_INDEX"):
        try:  # the documented Stop line — silent, best-effort, never a gate
            from . import store
            store.index_cap(apply=True)
        except Exception:
            pass
    return blocks, warns


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
    nonce (the capability) nor the bound session is ever published here.
    A poll is a TRUE read: no lock, no write — write_json is atomic
    (tmp + os.replace) so a lockless read never sees a torn file. Only
    when a row actually expired does the GC leg take the flock, re-read,
    and persist the sweep — a watched roster (web polls every 3s) must
    never churn .claims.json or contend with real claim/release traffic."""
    raw = pk.read_json(claims_path(), {}) or {}
    c = _sweep(raw)
    if len(c) != len(raw):  # sweep only ever drops rows
        with _flocked(claims_path() + ".lock"):
            raw = pk.read_json(claims_path(), {}) or {}
            c = _sweep(raw)
            if len(c) != len(raw):
                pk.write_json(claims_path(), c)
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


REAP_S = 3600   # a roster row unseen this long is a throwaway — reap it


def _unlink_seat_state(seat):
    """Remove every state file keyed on the seat (cursors + locks +
    per-session variants, .seen, stop latches) — the orphan tail a reaped
    row would otherwise leave in the room dir forever. Fail-open per file."""
    key = _seat_key(seat)
    d = chat.chat_dir()
    try:  # the private DM lane goes with the seat (RAM etiquette)
        os.remove(chat.room_path(chat.DM_PREFIX + key))
    except OSError:
        pass
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        if any((m + key) in n for m in (".cursor.", ".seen.", ".stopfp.")):
            try:
                os.remove(os.path.join(d, n))
            except OSError:
                pass


def reap_roster(max_age=REAP_S, now=None):
    """G-roster-reaper -> [reaped seats]. The roster only ever GREW — /tmp
    throwaway sessions piled up as permanently-absent rows with orphan
    cursor/seen/latch files. Drop rows unseen for max_age+ and unlink their
    state. Presence truth is the .seen mtime: deliver touches it at every
    boundary and an armed beacon's wait loop delivers, so a live-but-idle
    seat stays fresh; a reaped seat that returns self-heals at its next
    boundary (cursor re-baselines — acceptable for something absent an
    hour). Lock-free probe first: the web panel polls the report every 3s
    and must not churn the roster — only an actually-stale row takes the
    flock (claims_list's exact pattern). Fail-open total."""
    now = time.time() if now is None else now
    cut = now - max_age
    try:
        r = roster()
        if not any((last_seen(s, row) or 0) < cut for s, row in r.items()):
            return []
        victims = []
        with _flocked(roster_path() + ".lock"):
            r = roster()
            for s in list(r):
                if (last_seen(s, r[s]) or 0) < cut:
                    del r[s]
                    victims.append(s)
            if victims:
                pk.write_json(roster_path(), r)
        for s in victims:
            _unlink_seat_state(s)
        return victims
    except Exception:
        return []


def roster_report(room="main"):
    """{"seats": [...], "claims": [...]} — fail-open by caller. Pending is
    computed from each seat's cursor WITHOUT moving it. One GC leg rides the
    read (claims_list's precedent): rows absent past REAP_S are reaped here,
    so every live surface (CLI table, web panel) keeps the roster clean."""
    reap_roster()
    seats = []
    for seat, row in sorted(roster().items()):
        # pending is the MULTI-ROOM truth (the owner's panel must show a
        # helm-dogfood mention, not just main), read off the row's newest
        # session cursor (hook joins are session-keyed) with the seat-level
        # fallback — cursors never move here.
        hits = _pending_all(room, seat, session=row.get("session"))
        pending, preview = len(hits), None
        if hits:
            preview = _scrub(hits[-1][1].get("text") or "")[:PREVIEW_CHARS]
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
    return home.session_id()


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
            session = cwd = None
            if "--hook-json" in args:
                d = _hook_stdin()
                session, cwd = d.get("session_id"), d.get("cwd")
            emit = _hook_emit("PostToolUse") if "--hook-json" in args else print
            deliver_any(session=session, room=room,   # every room, one nudge
                        seat=_flag(args, "--seat"), emit=emit, cwd=cwd)
        except Exception:
            pass                    # fail-open: never hold a tool boundary
        return 0
    if verb == "dm":
        sender = chat._seat_flag(args)
        to = args[0] if args else None
        text = " ".join(args[1:]).strip()
        if to and not text and not sys.stdin.isatty():
            text = sys.stdin.read().strip()
        if not (to and text):
            print("usage: helm chat dm <seat> <text...> [--seat S]  "
                  "(one private recipient — never a room)", file=sys.stderr)
            return 2
        row, err = dm(to, text, who=sender, session=_env_session(),
                      profile=sender)
        if err:
            print("helm chat: " + err, file=sys.stderr)
            return 1
        print("helm chat [dm] %s" % chat._fmt(row))
        return 0
    if verb == "seat":
        if args[:1] == ["rename"] and len(args) >= 3:
            ok, msg = rename_seat(args[1], args[2])
            print("helm chat: " + msg, file=sys.stdout if ok else sys.stderr)
            return 0 if ok else 1
        if args[:1] in (["mute"], ["unmute"], ["mutes"]):
            sub = args.pop(0)
            who = chat._seat_flag(args) or derive_seat(_env_session())
            if sub == "mutes":
                got = mutes(who)
                print("helm chat: %s mutes %s" % (
                    who, ", ".join(got) if got else "nothing"))
                return 0
            if not args:
                print("usage: helm chat seat %s <room> [--seat S]" % sub,
                      file=sys.stderr)
                return 2
            ok, msg = set_mute(who, args[0], on=sub == "mute")
            print("helm chat: " + msg, file=sys.stdout if ok else sys.stderr)
            return 0 if ok else 1
        print("usage: helm chat seat rename <sid|oldname> <newname> | "
              "seat mute|unmute <room> [--seat S] | seat mutes [--seat S]",
              file=sys.stderr)
        return 2
    if verb == "stop-guard":
        try:
            session, stop_active = None, False
            if "--hook-json" in args:
                d = _hook_stdin()
                session = d.get("session_id")
                stop_active = bool(d.get("stop_hook_active"))
            blocks, warns = stop_guard(session=session, room=room,
                                       seat=_flag(args, "--seat"),
                                       stop_active=stop_active)
        except Exception:
            return 0                # FAIL-OPEN TOTAL: never wedge a stop
        for w in warns:             # WARN rides along, never changes the exit
            print(w, file=sys.stderr)
        if blocks:                  # ALL blockers in ONE exit-2 (one-shot fix)
            print("\n".join(blocks), file=sys.stderr)
            return 2
        return 0
    if verb == "wait":
        timeout = _flag(args, "--timeout")
        follow = "--follow" in args
        line = wait(seat=_flag(args, "--seat"), room=room,
                    any_row="--any" in args,
                    timeout=float(timeout) if timeout else None,
                    # --follow (beacon) + --any-watch must use wait()'s FLUSHED
                    # sink (_emit_line) — passing bare print here overrode it and
                    # block-buffered every wake-line into oblivion on a Monitor
                    # pipe (the beacon-never-wakes bug). Only single-shot seat
                    # mode keeps print (deliver emits the one line + returns it).
                    emit=None if (follow or "--any" in args) else print,
                    follow=follow, session=_env_session())
        if follow:               # --follow streams via emit; returns on timeout
            return 0
        if line is None:
            return 1
        if "--any" in args:
            print(line)
        return 0
    if verb == "seats":
        rep = roster_report(room)
        rows = rep["seats"]
        hidden = 0
        if "--all" not in args:      # absent rows hide by default (rows past
            shown = [s for s in rows if s["presence"] != "absent"]
            hidden = len(rows) - len(shown)          # REAP_S are already gone)
            rows = shown
        if not rows and not hidden:
            print("helm chat: no seats yet — sessions join on their next start "
                  "(helm hooks install wires it)")
            return 0
        w = max([len(s["seat"]) for s in rows] or [0])
        for s in rows:
            print("  %-*s  %-6s  pending %-3d %s" % (
                w, s["seat"], s["presence"], s["pending"],
                (s.get("project") or "")))
        if hidden:
            print("  (%d absent seat%s hidden — --all shows them; unseen "
                  ">%dm reaps them)" % (hidden, "s"[:hidden != 1], REAP_S // 60))
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
