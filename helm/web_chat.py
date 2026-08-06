"""Chat message projections for :mod:`helm.web`."""
import sys
import threading

_web = sys.modules.get(__package__ + ".web")
if _web is not None:
    globals().update({name: value for name, value in vars(_web).items()
                      if not (name.startswith("__") and name.endswith("__"))})



# ── chat window (lazy-load) ──────────────────────────────────────────────
# The initial/reset open returns only the last CHAT_WIN_DEFAULT rows (the
# settled recent-50 contract); deeper history comes from the
# older-page fetch (?before=<idx>) as the owner scrolls up. The live
# incremental poll (since=<total>) is UNTOUCHED — it stays rows[since:] byte
# for byte, the measured 22ms/54KB good path.
CHAT_WIN_DEFAULT = 50



def _chat_gen(rows):
    """A rotation fingerprint: the identity of the room's HEAD row. An append
    never touches row 0, so this is STABLE across the incremental poll; a rotate
    (chat._rotate keeps only the newest half — chat.py:_rotate) makes row 0 a
    DIFFERENT message, so the fingerprint CHANGES. The client's `since` is an
    ABSOLUTE row count and is meaningless across that reindex (a rotate-then-
    regrow past the old cursor slips a naive total<since check), so a changed
    gen is the client's one reliable signal to reset its cursor + repaint.
    Empty room => '0'. Cheap: hashes one row on a read the caller already did."""
    if not rows:
        return "0"
    h = rows[0]
    key = "\x00".join((str(h.get("id") or ""), str(h.get("ts") or ""),
                       str(h.get("from") or ""), str(h.get("tts") or ""),
                       str(h.get("tfrom") or ""), str(h.get("react") or ""),
                       str(h.get("text") or "")))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]



def _chat_win(qs):
    """Window size from ?win=. Default CHAT_WIN_DEFAULT. `win=0`/`win=all`
    is the escape hatch for a consumer that genuinely needs the whole room
    (None => no window). A garbled value falls back to the default rather
    than erroring — the window is a rendering hint, never a contract."""
    raw = (_q1(qs, "win", None) or "").strip().lower()
    if raw in ("all", "0"):
        return None
    if not raw:
        return CHAT_WIN_DEFAULT
    try:
        n = int(raw)
    except ValueError:
        return CHAT_WIN_DEFAULT
    return n if n > 0 else None



# The older-page body cache. The slice rows[start:before] is IMMUTABLE signed
# history (rows only ever append, and a signed row never mutates once written),
# so caching the BODY keyed (room, before, win) can never corrupt integrity —
# DREGGTEGRITY-legal precisely because this response carries NO transport/signal
# truth (that is recomputed live on the poll path only, never here). Single-
# flight TTL, keyed by the chat root like _ROOMS_SUM so isolated test worlds
# can never read each other's slice.
_CHAT_OLDER_CACHE = {}          # (chat-root, room, before, win) -> (at, body, stat)

_CHAT_OLDER_TTL = 30.0

_CHAT_OLDER_LOCK = threading.Lock()



def _room_stat(room):
    """(size, mtime_ns) of a room's file, or (0, 0). The older-page cache's
    premise 'signed rows only ever append' is FALSE under rotation (chat._rotate
    rewrites the file to its newest half), so keying the cache on a fingerprint
    of the file itself makes a rotation — or any append — a cache MISS instead
    of serving dropped rows / a stale total for up to the TTL. Cheap: one stat,
    no read; a hit still skips the read+serialize the cache exists to avoid."""
    from . import chat
    try:
        st = os.stat(chat.room_path(room))
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return (0, 0)



def _api_chat_older(qs):
    """Older-history page (a before-cursor fetch, n rows back): the
    immutable body of rows[max(0,before-win):before] for a room, plus the
    absolute `base` of the first row returned and the live `total`. BODY ONLY —
    no transport, no rooms, no roster, no signal, no presence. Signed history is
    immutable, so this slice is cache-safe; the signing STATUS is never in this
    response and is always recomputed live on the poll path."""
    from . import chat
    room = _q1(qs, "room", "main")
    try:
        before = int(_q1(qs, "before", "0"))
    except ValueError:
        return {"error": "before wants an integer"}, 400
    win = _chat_win(qs)
    if win is None:
        win = CHAT_WIN_DEFAULT
    try:
        root = str(chat.chat_dir())
    except Exception:
        root = "?"
    # The stat is VALIDATED, not keyed. Baking a pre-sampled stat into the key
    # was a TOCTOU: an append between that sample and the serve left the sampled
    # stat matching the OLD entry, so a stale total got served (reproduced in
    # review: cached total 10 while the file already held 11). It also NEVER
    # evicted — every
    # append minted a fresh stat -> a fresh key -> unbounded growth (12 appends,
    # 12 entries). Now the key is version-free (root, room, before, win) and the
    # room's live stat is compared to the stat STORED WITH the body: a mismatch is
    # a MISS (rotation/append can never serve a stale slice or total), and a fresh
    # read OVERWRITES the one entry per (room, page) — the cache stays bounded.
    key = (root, room, before, win)
    now = time.time()
    hit = _CHAT_OLDER_CACHE.get(key)
    if hit and now - hit[0] < _CHAT_OLDER_TTL and hit[2] == _room_stat(room):
        return dict(hit[1]), 200
    with _CHAT_OLDER_LOCK:
        hit = _CHAT_OLDER_CACHE.get(key)   # re-check under the single-flight lock
        if hit and now - hit[0] < _CHAT_OLDER_TTL and hit[2] == _room_stat(room):
            return dict(hit[1]), 200
        rows, total = chat.read(room)
        st = _room_stat(room)              # sampled AFTER the read: the stat the body reflects
        end = before if 0 <= before <= total else total
        start = max(0, end - win)
        body = {"room": room,
                "lines": chat.public_rows(rows[start:end]),
                "base": start, "total": total, "gen": _chat_gen(rows)}
        # evict every OTHER version of this room (any before/win at a different
        # stat) so a rotated/appended room cannot accumulate entries — the cache
        # holds only current-version pages, bounded per room.
        for k in [k for k, v in _CHAT_OLDER_CACHE.items()
                  if k[0] == root and k[1] == room and v[2] != st]:
            del _CHAT_OLDER_CACHE[k]
        _CHAT_OLDER_CACHE[key] = (now, body, st)
        return dict(body), 200



def _api_chat_ids(qs):
    """Batch id hydration (cap 100): the
    rows whose id is in ?ids=<csv>, BODY ONLY (no transport/rooms/roster/signal).
    The client uses it to resolve a reply's parent that sits ABOVE the loaded
    window WITHOUT paging the whole gap — and, crucially, to tell 'outside the
    window but STILL in the room' (the id comes back) from 'genuinely rotated
    out' (the id is absent), so only the latter renders the 'rotated out' chip.
    DREGGTEGRITY: like the older page this carries NO signing status; the
    transport truth recomputes live on the poll path, never from here."""
    from . import chat
    room = _q1(qs, "room", "main")
    want = [x for x in (_q1(qs, "ids", "") or "").split(",") if x][:100]
    if not want:
        return {"room": room, "lines": [], "gen": "0"}, 200
    wset = set(want)
    rows, _total = chat.read(room)
    hit = [m for m in rows if (m.get("id") or "") in wset]
    return {"room": room, "lines": chat.public_rows(hit),
            "gen": _chat_gen(rows)}, 200



def _api_chat(qs):
    """Poll read: rows after ?since= (count already seen) + the new total +
    the transport truth (signed/unsigned + chain head — the panel's tick and
    strip) + the owner-unread signal (_owner_signal — the nav badge on EVERY
    tab) + `rooms` (the channel sidebar list, cross-room unread for the summed
    badge) + `roster` (the live seat names — the composer's @mention completion
    source, so a mention the owner types is an EXACT token seats.deliverable
    will actually match). The panel polls this every ~2s; since past the end
    resets. Rows include reaction rows AND reply rows ({reply_to, rts, rfrom});
    the client aggregates both.

    Two read shapes share this route:
      • ?before=<idx>  -> the older-history page (body only; see _api_chat_older).
      • else           -> the live poll. The INCREMENTAL path (0<since<=total)
        is byte-identical to always — rows[since:], no window, no base — the
        measured 22ms/54KB good path stays exactly what it was. Only the
        INITIAL/RESET open (since==0, or a since past the end) is WINDOWED to
        the last `win` rows and stamped with `base` (the absolute index of
        lines[0]) so the client can lazy-load older pages from there."""
    _cockpit_beat()          # the owner is HERE — every open page runs this poll
    if _q1(qs, "before", None) is not None:
        return _api_chat_older(qs)
    if _q1(qs, "ids", None) is not None:
        return _api_chat_ids(qs)
    try:
        since = int(_q1(qs, "since", "0"))
    except ValueError:
        return {"error": "since wants an integer"}, 400
    try:
        from . import chat, seats as _s
        room = _q1(qs, "room", "main")
        try:
            roster = _s.roster()
        except Exception:
            roster = {}
        rows, total = chat.read(room)   # one read serves the slice AND the signal
        try:
            # the fleet-wide presence bar (dot + one status line per seat) —
            # rides the poll the panel already runs; light (no cursor scans)
            presence = _s.presence_report()
        except Exception:
            presence = []
        win = _chat_win(qs)
        incremental = 0 < since <= total   # the live cursor path — never windowed
        if incremental:
            start = since                   # BYTE-IDENTICAL to always: rows[since:]
        elif win is None:
            start = 0                       # win=0/all escape hatch: full history
        else:
            start = max(0, total - win)     # initial/reset: the last `win` rows
        # `roster` feeds the composer's @mention list — the seat KEYS ride
        # the JSON wire raw (ensure_ascii=False), so launder each label so a
        # hostile HELM_CHAT_NAME (ESC/bidi) cannot reach a non-browser consumer
        # or spoof the dropdown. The panel still matches on the exact stored
        # key when the owner sends; only this published copy is laundered.
        # the rows carry raw NAME fields (from/tfrom/rfrom/dm) — a hostile one
        # can only exist OUTSIDE the validated join seam (home.chat_name), but
        # launder the EMITTED copy so no such name reaches a non-browser reader
        # of the /api/chat JSON (chat.public_rows; the stored rows stay raw for
        # reaction/reply matching, mirroring the roster's _pub_row owner).
        # DREGGTEGRITY: transport is chat.transport_status() computed LIVE on
        # EVERY poll — the signed/unsigned + chain-head truth is cheap and is
        # NEVER served from a cache. Only the immutable row BODY may be windowed
        # (above) or cached (the older-page); the signing STATUS always recomputes.
        out = {"room": room,
               "lines": chat.public_rows(rows[start:]),
               "total": total, "gen": _chat_gen(rows),
               "transport": chat.transport_status(fleet=True),
               "rooms": _rooms_summary_cached(roster),
               "roster": sorted(_s._seat_label(s) for s in roster),
               "presence": presence}
        if not incremental:
            # only the initial/reset open carries `base` (absolute index of
            # lines[0]) so the client can lazy-load older pages from there; the
            # incremental poll stays exactly rows[since:] with no extra field.
            out["base"] = start
        out.update(_owner_signal(room, rows))
        return out, 200
    except Exception:
        return {"unavailable": True}, 200



def _api_chat_read_post(payload):
    """The owner's read-ack: the chat view is open and visible, everything
    rendered — advance the owner-read cursor to the room's end. The mirror of
    chat.mark_owner_unread's direction, owned HERE (chat.py stays the agents'
    module). Count + newest row id, so rotation cannot strand it."""
    from . import chat, pk
    room = str(payload.get("room") or "main")
    rows, total = chat.read(room)
    pk.write_json(_owner_read_path(room),
                  {"n": total, "rid": rows[-1].get("id") if rows else None})
    _rooms_summary_invalidate()   # the badge must zero on the NEXT poll
    return {"ok": True, "room": room, "owner_read": total}, 200



def _api_chat_post(payload):
    """The owner's post: append (signed server-side when the room node
    answers) + mark owner-unread. name defaults to the derived owner handle.

    Optional `reply_to` = the parent row's id (the panel's reply button holds
    it): the row threads under that parent and, when signed, its digest BINDS
    it. Delivery: the reply also WAKES the parent's author (mention-tier,
    casefold, via the stamped rfrom — replying replaces typing the
    @mention); otherwise it wakes what its text alone would."""
    from . import chat, seats
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return {"error": 'payload wants {"text": "..."} (non-empty)'}, 400
    room = str(payload.get("room") or "main")
    if room.startswith(chat.DM_PREFIX):
        # A post into an open DM channel IS a DM: route it through
        # the one owner send-seam so signing/beacon/delivery hold. The lane
        # stem is seats._seat_key output — it PASSES the recipient token
        # regex while addressing nothing — so resolve it back to the roster
        # seat and refuse an orphan lane by name, never address the stem.
        seat = _dm_seat_map().get(room[len(chat.DM_PREFIX):])
        if not seat:
            # _dsan the echo: `room` is client-supplied and this string rides
            # the owner-rendered JSON error
            return {"error": "DM lane %r has no roster seat — readable, not "
                             "postable" % chat._dsan(room)}, 400
        return _api_chat_dm({"text": text, "to": seat,
                             "name": payload.get("name"),
                             "reply_to": payload.get("reply_to")})
    try:
        msg = chat.post(text.strip(), room,
                        who=str(payload.get("name") or seats.owner_name()),
                        profile=_chat_profile(), origin="web",
                        reply_to=str(payload.get("reply_to") or "") or None)
    except ValueError as exc:
        # A REFUSED POST IS A 400, NOT A 500. shaguard refuses a padded short
        # sha, and that is a fact about the REQUEST — this handler already
        # answers bad payloads with 400 and the panel renders `error`. Letting
        # it escape would turn a precise, actionable refusal into a server
        # fault the owner cannot read.
        return {"error": str(exc)}, 400
    chat.mark_owner_unread(room)
    _rooms_summary_invalidate()   # the owner's own post shows on the NEXT poll
    return {"ok": True, "msg": chat.public_rows([msg])[0],
            "total": chat.read(room)[1]}, 200



def _api_chat_dm(payload):
    """The owner's TRUE 1:1 (the ledger 'message a seat' card routes single-
    seat sends here): one private recipient's lane, never a room post — the
    old path posted '@seat …' into #main and called it a DM.
    Exact-token addressee (seats.dm — premise exact-token-addressee-match);
    signed like a post; the recipient's beacon surfaces it."""
    from . import chat, seats
    text = str(payload.get("text") or "").strip()
    if not text:
        return {"error": "empty text"}, 400
    row, err = seats.dm(str(payload.get("to") or ""), text,
                        who=str(payload.get("name") or seats.owner_name()),
                        profile=_chat_profile(), origin="web",
                        # threads the DM (chat.post owns the resolve) — the
                        # panel's reply button holds the parent id; dropping
                        # it here silently flattened the reply (caught by an
                        # independent review)
                        reply_to=str(payload.get("reply_to") or "") or None)
    if err:
        return {"error": err}, 400
    _rooms_summary_invalidate()   # a DM lane row is summary'd state too
    return {"ok": True, "msg": chat.public_rows([row])[0]}, 200
del _web
