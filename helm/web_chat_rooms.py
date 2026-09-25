"""Chat-room summaries for :mod:`helm.web`."""
# os/re/time are imported HERE, not inherited. The globals() copy below only
# fires when helm.web is ALREADY in sys.modules; on the other import order
# these names were unbound, every os./re./time. use raised NameError, and
# _owner_signal's `except Exception` answered zeros — so the owner's unread
# and @-mention badges read 0 forever with nothing logged anywhere. web.py
# binds these before it reaches web_compat, so the live path was safe; a
# direct import of web_compat or this module was not.
import os
import re
import sys
import threading
import time

_web = sys.modules.get(__package__ + ".web")
if _web is not None:
    globals().update({name: value for name, value in vars(_web).items()
                      if not (name.startswith("__") and name.endswith("__"))})



# ── chat: the human-included groupchat (chat.py owns rooms + markers) ──
# The web panel is the OWNER's surface (gui-first-owner law); agents live on
# `helm chat`. GET is an open read (loopback + same-origin only, like every
# GET); POST rides the mutation bearer and drops the owner-unread marker so
# the shipped reflex surfaces the message to every local agent next turn.
#
# The AGENT→OWNER direction (chat.mark_owner_unread is owner→agent only):
# every poll also carries the owner-facing unread signal — rows past the
# owner's last-read cursor, and the subset that @-mention an owner name
# (seats.owner_names(), the delivery filter's owner rule). The cursor is a
# web.py-owned marker in the room dir (<room>.owner-read); the chat view
# advances it via POST /api/chat/read when the owner has actually SEEN the
# room. Fail-open total: any surprise answers zeros — no badge, never an
# error (a GUI-first owner on another tab must never lose the page to this).

def _owner_read_path(room):
    from . import chat, pk
    return os.path.join(chat.chat_dir(), pk.slug(room) + ".owner-read")



def _owner_cursor(room, rows):
    """The owner's last-read position against the CURRENT rows. The stored
    row id wins (rotation-proof — ids are chat._append's stable per-row law);
    else the stored count while it still fits; else 0 (over-notify briefly,
    self-heals on the next read-ack)."""
    from . import pk
    st = pk.read_json(_owner_read_path(room), None)
    if not isinstance(st, dict):
        return 0
    rid = st.get("rid")
    if rid:
        for i in range(len(rows) - 1, -1, -1):
            if rows[i].get("id") == rid:
                return i + 1
    n = st.get("n")
    return n if isinstance(n, int) and 0 <= n <= len(rows) else 0



# THE ANSWERS CARD'S BUDGET (task/2364). One small card, so a handful of rows
# and a phone-readable head per row — the card is the glance, the room is the
# record. Nothing here caps what is COUNTED: `owner_mentions` walks every row,
# and only the rendered list is bounded.
_OWNER_ANSWER_CAP = 4
_OWNER_ANSWER_HEAD = 180


def _owner_signal(room, rows, names=None):
    """{owner_read, owner_unread, owner_mentions [, owner_mention_last,
    owner_mention_preview, owner_answers]} — what landed past the owner's
    cursor and how much of it addresses HIM.

    `owner_answers` is the NEWEST-FIRST list behind `owner_mention_preview`,
    for the "answers for you" card (task/2364): the same rows, the same owner
    rule, the same walk, just more than one of them. It is added HERE rather
    than in a reader of its own precisely because the ruling says the card must
    read the ledger the push path reads — a second extraction with its own
    matching rule would be free to disagree with the notification about what
    reached him, and the disagreement would be invisible.

    Mention matching mirrors seats.deliverable's owner rule:
    seats.owner_names() as the name set, the same @-boundary regex. A caller projecting every room may pass that ONE resolved set —
    owner identity is request truth, not per-room truth. The owner rails' own
    posts (origin web/tui, owner name) never badge the owner; reactions never
    badge (noise law). owner_mention_last is the newest unseen mention's
    identity — the client's notify-dedup key (once per NEW mention decision,
    not per poll)."""
    try:
        from . import seats
        if names is None:
            names = seats.owner_names()
        cur = _owner_cursor(room, rows)
        out = {"owner_read": cur, "owner_unread": 0, "owner_mentions": 0}
        rx = re.compile(r"(?<![A-Za-z0-9._-])@(?:%s)(?![A-Za-z0-9._-])"
                        % "|".join(sorted(map(re.escape, names))),
                        re.I) if names else None
        last = None
        addressed = []
        for m in rows[cur:]:
            text = m.get("text")
            if not isinstance(text, str) or not text or m.get("react"):
                continue
            if (m.get("origin") in seats.OWNER_RAILS
                    and str(m.get("from") or "").lower() in names):
                continue
            out["owner_unread"] += 1
            if rx and rx.search(text):
                out["owner_mentions"] += 1
                last = m
                addressed.append(m)
        if addressed:
            from . import chat as _chat
            # NEWEST FIRST, and the SLICE IS THE LAST STEP. Taking the newest
            # four out of the whole addressed list means the count above and
            # the list below are the same walk: `owner_mentions` is always the
            # true population, and the card can say "4 of 9" honestly because
            # it was never handed a truncated count.
            out["owner_answers"] = [
                {"from": _chat._dsan(m.get("from") or "?"),
                 "text": (m.get("text") or "")[:_OWNER_ANSWER_HEAD],
                 "ts": m.get("ts") or "", "room": room}
                for m in reversed(addressed[-_OWNER_ANSWER_CAP:])]
        else:
            # PRESENT AND EMPTY, never absent. An absent key means "this server
            # predates the reading" to the card, which is a fact about the
            # SERVER; a measured zero is a fact about the ROOM, and the card
            # says two different sentences for them.
            out["owner_answers"] = []
        if last is not None:
            # _dsan the identity: a foreign/pre-fix row's from-field is not
            # covered by the join seam and rides this owner-polled JSON raw.
            from . import chat
            out["owner_mention_last"] = "%s|%s" % (last.get("ts") or "",
                                                   chat._dsan(last.get("from") or ""))
            out["owner_mention_preview"] = "%s: %s" % (
                chat._dsan(last.get("from") or "?"), (last.get("text") or "")[:120])
        return out
    except Exception:
        # FAIL-OPEN STAYS FAIL-OPEN, and `owner_answers` is deliberately ABSENT
        # from this shape rather than empty: this branch means the read FAILED,
        # and an empty list here would tell the card the room is quiet. Absent
        # renders NOT SENT, which is the only honest thing to say.
        return {"owner_read": 0, "owner_unread": 0, "owner_mentions": 0}



def _room_seats(room, rows, roster, cursor_paths=None):
    """The sidebar's per-channel roster: which seats are present in THIS
    room, newest-activity first — [{seat, presence}]. 'Present' = the seat
    POSTED here recently (the rows are already read for the owner signal —
    reuse, no extra I/O) OR actually CONSUMED rows here (its room cursor has
    `active=true`; a bare EOF join baseline is NOT presence, or every seat
    would otherwise look present everywhere). Presence is the shared .seen
    beat (seats.last_seen /
    presence_of), so a fresh poster shows 'fresh', an idle one 'quiet';
    owner-rail rows are skipped (the owner is not a seat). Fail-open per
    row; capped so a busy channel can't flood the sidebar.

    Each row also carries `last_seen` (the raw beat, epoch seconds): the
    sidebar renders it as an AGE ('3m'), because a coloured dot with no legend
    and no clock cannot answer the owner's only question — is this seat working
    right now, or has it been quiet for an hour? (owner UX pass 2026-07-21)"""
    from . import seats as _s
    seen, out = set(), []
    # ONE presence projection everywhere (seats.presence_with_identity over
    # seats.last_seen): the sidebar cannot paint a dot the CLI table would
    # disagree with, and an UNVERIFIED row (its beat may be another process's
    # — seats.unverified_seats) shows 🟠 here too. Computed once per call from
    # the roster the caller already read: no extra I/O on the 2s poll.
    unver = _s.unverified_seats(roster)

    def _row(seat, ls=None):
        # the seat KEY rides the sidebar JSON raw — launder the emitted label
        # (the raw key still indexes roster[]/last_seen above) so a hostile
        # HELM_CHAT_NAME cannot spoof the channel roster or a non-browser reader.
        if ls is None:
            ls = _s.last_seen(seat, roster[seat])
        return _s._pub_row({
            "seat": seat, "runtime": roster[seat].get("runtime"),
            "presence": _s.presence_with_identity(ls, unver.get(seat)),
            "last_seen": ls})

    for m in reversed(rows[-64:]):           # recent activity, newest first
        frm = str(m.get("from") or "")
        if not frm or m.get("react") or frm in seen or frm not in roster \
                or not _s.room_in_scope(room, roster[frm]):
            continue
        seen.add(frm)
        out.append(_row(frm))
        if len(out) >= 6:
            return out
    for seat in sorted(roster):              # consumers who never posted
        if len(out) >= 6:
            break
        if seat in seen or not _s.room_in_scope(room, roster[seat]):
            continue
        # Freshness pre-filter — the O(all)->O(fresh) turn. Only a seat with a
        # recent presence beat can legitimately hold a live-sidebar slot, so
        # gate the expensive room_active cursor walk (several pk.read_json disk
        # reads per seat) behind it: a dead ephemeral review-SA that consumed
        # this room hours ago is noise, not presence. last_seen is one cheap
        # .seen stat (or the O(1) roster-carried beat when the file is gone) vs
        # room_active's per-cursor reads, and reusing ls in _row means each
        # survivor pays it once. Same FRESH_S/QUIET_S window presence_of paints
        # (reuse, no new magic number) and the exact _live_seats() idiom — so a
        # genuinely fresh never-posted consumer still reaches room_active and
        # appears, while a stale one is skipped before any disk cursor read.
        ls = _s.last_seen(seat, roster[seat])
        if _s.presence_of(ls) == "absent":   # the cheap gate stays presence_of:
            continue                         # an unverified row is NOT absent,
                                             # so it still reaches _row's dot
        active = (_s.room_active(room, seat) if cursor_paths is None
                  else _s.room_active(room, seat, cursor_paths))
        if not active:                         # bare EOF baseline is not presence
            continue
        out.append(_row(seat, ls))
    return out



# Per-room PARSE memo — the guard the TTL cache below does not give. That
# cache bounds how OFTEN the summary recomputes; it does nothing for what a
# recompute COSTS: every miss re-read + re-json-parsed EVERY room, even when
# one append moved one room (and the SSE watcher busts the cache on every
# such append, so a busy fleet recomputes near-continuously). Room files are
# tmpfs (/dev/shm) so the read is cheap-ish — the json parse is the cost, so
# what is memoized is the PARSED (rows, total) per room, keyed by the file's
# (st_mtime_ns, st_size, st_ino) stat identity (the same (mtime_ns, size)
# change key _chat_fingerprint already trusts, strengthened by the inode —
# free, it rides the same stat): a recompute re-parses only rooms that moved.
#   Key strength (the false-fresh window): every chat.py writer moves the
# key. _append grows st_size on every row (whole-row single write under the
# room flock — an append inside the same mtime tick still misses via size);
# _rotate and the restore sweep build a NEW file and os.replace it (fresh
# st_ino — a same-size replace still misses via inode); st_mtime_ns (ns
# resolution, not seconds) guards whatever remains. chat.py has NO in-place
# same-size writer, so the false-fresh window is practically empty.
#   The memo deliberately does NOT hold the derived summary row: owner_unread
# rides the <room>.owner-read cursor and seats presence rides .seen beats —
# both move WITHOUT the room file moving, so caching them on its identity
# would freeze a quiet room's badge and ages forever. Signal + seats stay
# recomputed per summary (cheap, in-memory over memoized rows); only the
# parse is memoized. Stale-proof beats fast (correctness over speed).
#   Fail-open: stat failure -> drop the entry, serve the direct chat.read —
# never a cached parse for a file we can no longer see; a raising read is
# never stored (_rooms_summary's per-room except stays the handler). Keyed
# by the room file's ABSOLUTE path (chat_dir()-derived), so isolated test
# worlds can never share an entry — the brick #1 pollution class, closed
# the same structural way as the root-keyed cache below.
_ROOM_ROWS_MEMO = {}            # room-file path -> (stat_key, rows, total)

_ROOM_ROWS_LOCK = threading.Lock()



def _room_rows_memo(room):
    """chat.read(room) memoized on the room file's stat identity — (rows,
    total), re-parsed ONLY when the file changed. Stat BEFORE read: a write
    landing between the two leaves an OLD key on NEW content, which the next
    stat mismatches and re-parses — the safe direction (never a new key over
    old content). The lock guards dict ops only (_roster_cached's idiom, one
    lock two-step; single-flight already lives in _rooms_summary_cached, a
    layer up, so a duplicate compute here is impossible on the poll path)."""
    from . import chat
    path = chat.room_path(room)
    try:
        st = os.stat(path)
    except OSError:
        with _ROOM_ROWS_LOCK:
            _ROOM_ROWS_MEMO.pop(path, None)   # never serve a cached parse
        return chat.read(room)                # for a file we cannot stat
    key = (st.st_mtime_ns, st.st_size, st.st_ino)
    with _ROOM_ROWS_LOCK:
        hit = _ROOM_ROWS_MEMO.get(path)
        if hit and hit[0] == key:
            return hit[1], hit[2]
    rows, total = chat.read(room)
    with _ROOM_ROWS_LOCK:
        _ROOM_ROWS_MEMO[path] = (key, rows, total)
    return rows, total



def _dm_seat_map(roster=None):
    """{lane-stem: display seat} for every roster seat — the REVERSE of
    seats._seat_key. A DM lane's filename is the recipient's state-file key
    (slug + 4-byte hash), which PASSES the recipient token regex while
    addressing nothing — so both the channel label and the post handler must
    map the stem back to the real seat, never trust the stem as an address
    (task/62). Roster-scale compute; callers hold no cache."""
    from . import seats
    out = {}
    try:
        for seat in (roster if roster is not None else seats.roster()):
            out[seats._seat_key(seat)] = str(seat)
    except Exception:
        pass
    return out


def _dm_lanes():
    """(lanes, fault): every DM lane with a RAM file, as room names
    (`dm-<stem>`), sorted. Enumerated HERE from the dm/ dir rather than
    widening chat.list_rooms — the lanes' invisibility to room fanout /
    log-flush defaults is that namespace's deliberate property
    (chat.room_path); only the OWNER's web sidebar gets to see them (task/62:
    droppable-in DM channels).

    A MISSING NAMESPACE IS PROVEN EMPTY; AN UNREADABLE ONE IS NOT — the same
    split seats_ack._dm_lanes draws, and this is a SEPARATE function that
    happened to carry the identical fail-open. `except OSError: return []`
    made a denied dm/ dir render as an owner with no DMs, on the OWNER'S OWN
    surface, which is the worst place for it. Two functions, one name, one
    defect: I cured the other one first and swept for consumers with a grep
    that filtered out `def` lines, so this definition was invisible to the
    sweep and the call site below broke on the tuple instead."""
    from . import chat
    d = os.path.join(chat.chat_dir(), "dm")
    try:
        names = os.listdir(d)
    except FileNotFoundError:
        return [], None                    # no DM namespace yet — PROVEN empty
    except OSError as exc:
        reason = {PermissionError: "denied",
                  NotADirectoryError: "not-a-file"}.get(type(exc), "unreadable")
        return [], ("dm-namespace", d, reason)
    return sorted(chat.DM_PREFIX + f[:-6] for f in names
                  if f.endswith(".jsonl")), None


def _rooms_summary(roster=None):
    """The channel list for the web sidebar: one light row per room —
    {room, total, last (ts), owner_unread, owner_mentions, seats}. Folding the
    per-room owner signal here is what lets the nav badge SUM every channel,
    so a post in a NON-main room is never invisible to the owner (the real
    single-room hole). seats = the per-channel roster (_room_seats) so the
    sidebar shows who is present/active in each room, not just the room name.
    A handful of small tmpfs stats + memoized parses (_room_rows_memo);
    fail-open per room. `roster` is passed in when the caller already read it
    (the poll needs the same names for the composer's @mention completion —
    one read serves both)."""
    from . import chat, seats as _s
    if roster is None:
        try:
            roster = _s.roster()
        except Exception:
            roster = {}
    # Two request-wide facts used by EVERY room. Re-resolving owner_names spawned
    # git 240 times, while room_active globbed the 25k-file chat root once per
    # fresh (room, seat) pair. One snapshot keeps every sidebar row on the same
    # input and turns the directory fanout O(pairs × files) into O(files + pairs).
    try:
        names = _s.owner_names()
    except Exception:
        names = ()
    try:
        cursor_paths = _s._cursor_path_index()
    except Exception:
        cursor_paths = None
    out, live = [], set()
    for room in chat.list_rooms():
        try:
            live.add(chat.room_path(room))
            rows, total = _room_rows_memo(room)
            sig = _owner_signal(room, rows, names)
            out.append({"room": room, "total": total,
                        "last": (rows[-1].get("ts") if rows else None),
                        "owner_unread": sig.get("owner_unread", 0),
                        "owner_mentions": sig.get("owner_mentions", 0),
                        "seats": _room_seats(room, rows, roster, cursor_paths)})
        except Exception:
            continue
    # DM lanes join the summary as first-class channel rows (task/62): same
    # row shape + owner signal (the nav badge SUMS d.rooms, so a DM to ANY
    # seat now reaches the owner), flagged dm:true with the resolved seat
    # label. seats stays [] — presence in an inbox lane is not membership.
    dm_seats = _dm_seat_map(roster)   # roster already defaulted above
    # _dm_lanes reports whether it could READ the namespace. The row list
    # cannot carry that — a namespace is not a room, and inventing a row for
    # it is the fake-sentinel mistake the ack ladder just removed — so the
    # signal rides the API ENVELOPE (web_chat's dm_incomplete) instead.
    dm_rooms, _dm_fault = _dm_lanes()
    for room in dm_rooms:
        try:
            live.add(chat.room_path(room))
            rows, total = _room_rows_memo(room)
            sig = _owner_signal(room, rows, names)
            stem = room[len(chat.DM_PREFIX):]
            # the label is a roster KEY reaching an owner-polled JSON sink —
            # launder the EMITTED copy (seats._seat_label, the display law);
            # the raw name never leaves: the POST path re-resolves it
            # internally as seats.dm's addressee.
            lbl = dm_seats.get(stem)
            out.append({"room": room, "total": total,
                        "last": (rows[-1].get("ts") if rows else None),
                        "owner_unread": sig.get("owner_unread", 0),
                        "owner_mentions": sig.get("owner_mentions", 0),
                        "seats": [], "dm": True,
                        "seat": (_s._seat_label(lbl) if lbl else None)})
        except Exception:
            continue
    # a vanished room's parsed rows are real memory — evict what this pass
    # did not see (other-root entries go too; eviction only ever costs a
    # re-parse, never staleness, so over-pruning is safe by construction)
    with _ROOM_ROWS_LOCK:
        for p in [p for p in _ROOM_ROWS_MEMO if p not in live]:
            del _ROOM_ROWS_MEMO[p]
    return out



# _rooms_summary WAS the ONE heavy read left on the chat poll path (~14s at
# 224 seats x 13 rooms: _room_seats' consumers-fallback walked the whole sorted
# roster calling room_active until 6 slots filled; quiet rooms scanned deepest).
# The freshness pre-filter above first made that O(fresh), not O(all ~224), but
# production later grew to 25k cursor siblings: room_active's glob still rescanned
# that WHOLE directory once per fresh pair (209 scans / 5.3m entries / 8.2s).
# The request-wide cursor index now makes it one listdir, and owner identity is
# likewise resolved once for every room. The compute is measured sub-second
# again. The single-flight TTL cache below then caches that
# bounded compute, not a multi-second one. History:
# it ran UNCACHED on EVERY /api/chat
# poll (incremental included), so N clients x 2s stacked N ~14s computes ->
# the ~60s /api/chat requests that starved the thread pool (the second half
# of the 2026-07-23 UI-blank; the roster cache was brick #1). Same
# single-flight TTL treatment — brick #2 of the poll->push read-model — with
# one upgrade: the cache is KEYED BY THE CHAT ROOT (chat_dir()), so isolated
# test worlds (fresh tmp roots) can never read each other's cached summary —
# the cross-test-pollution class caught on brick #1, closed structurally
# instead of by per-test clears. Prod cardinality: one root, one entry.
_ROOMS_SUM_CACHE = {}           # chat-root -> (computed_at, summary)

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_ROOMS_SUM_CACHE": "keyed by chat root and bounded by a TTL",
    "_ROOM_ROWS_MEMO": "keyed by room file and checked against its stat",
}

# HOW LONG THE SUMMARY MAY BE SERVED WITHOUT RECOMPUTING.
#
# MEASURED, `_rooms_summary` called straight through on the owner's own chat
# root, six consecutive builds: 0.93s fastest, 1.21s median, 1.56s slowest.
# THE OLD VALUE WAS 3.0, WHICH IS NOT A CACHE. /api/chat is polled every 2s
# (60-chat.js.part: `setInterval(esStretch(pollChat), 2000)`), so a 3s window
# covers exactly one poll and expires before the next: the cache served warm
# once and then made every SECOND poll block ~1.2s rebuilding it. A TTL that
# is barely longer than its own fill and barely longer than its own poll is
# the shape this tree has already been bitten by twice — a 46s /api/ready
# under a 30s TTL and a 6s roster build under a 2.5s TTL. A TTL must be a
# MULTIPLE of its measured fill, not a neighbour of it.
#
# 15 IS TEN TIMES THE SLOWEST MEASURED BUILD. At the 2s poll one poll in
# ~8 pays the rebuild instead of one in 2; at the 10s SSE-stretched cadence,
# one in ~2 instead of every single one.
#
# WHAT THE WINDOW COSTS IS BOUNDED AND IT IS NOT THE OWNER'S OWN WRITES.
# Every owner action that changes this summary — a read-ack zeroing a badge,
# a post or DM landing a row — busts the entry synchronously through
# `_rooms_summary_invalidate`, so read-your-own-writes holds at ANY ttl and
# does not depend on this number. The only thing this window delays is
# another agent's post appearing in the CROSS-ROOM unread badge, and the
# room the owner is actually reading is never summarised — its rows come
# live off the same poll. A badge for another room up to 15s late is the
# whole price.
_ROOMS_SUM_TTL = 15.0

_ROOMS_SUM_LOCK = threading.Lock()



def _rooms_summary_invalidate():
    """Read-your-own-writes for the owner: every OWNER action that changes the
    summary'd state (read-ack zeroing an unread badge; a post/DM landing a row)
    rides web.py, so it busts the cache synchronously and the very next poll
    reflects it. Agent posts arrive via the CLI outside this process and stay
    TTL-bounded (<=3s — the old 2s poll already tolerated that lag).

    LOCK-COUPLED (a re-clear, deterministic repro): a bare pop raced an
    in-flight compute — a _rooms_summary_cached call that entered its locked
    compute BEFORE the pop published its now-stale summary AFTER it. Taking
    the same lock serializes: the pop waits out any in-flight publish, so
    nothing computed pre-invalidation can survive it. Worst-case stall for
    the caller = one compute (~200ms post brick #3) — fine for the watcher's
    250ms tick and trivial for the owner-write handlers."""
    from . import chat
    with _ROOMS_SUM_LOCK:
        try:
            _ROOMS_SUM_CACHE.pop(str(chat.chat_dir()), None)
        except Exception:
            _ROOMS_SUM_CACHE.clear()



def _rooms_summary_cached(roster=None):
    from . import chat
    try:
        key = str(chat.chat_dir())
    except Exception:
        key = "?"
    hit = _ROOMS_SUM_CACHE.get(key)
    if hit and time.time() - hit[0] < _ROOMS_SUM_TTL:
        return hit[1]
    with _ROOMS_SUM_LOCK:
        hit = _ROOMS_SUM_CACHE.get(key)   # re-check under the lock: the
        if hit and time.time() - hit[0] < _ROOMS_SUM_TTL:  # single-flight gate
            return hit[1]
        summary = _rooms_summary(roster)
        _ROOMS_SUM_CACHE[key] = (time.time(), summary)
        return summary
del _web
