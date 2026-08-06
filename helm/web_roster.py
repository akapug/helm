"""Roster projections for :mod:`helm.web`."""
import math
import subprocess
import sys
import threading

_web = sys.modules.get(__package__ + ".web")
if _web is not None:
    globals().update({name: value for name, value in vars(_web).items()
                      if not (name.startswith("__") and name.endswith("__"))})



def _seat_ephemeral(s):
    """An EPHEMERAL review-subagent (auto-named agent-<hex>, no home room, /tmp
    cwd) — a transient fan-out SA, not a conversational seat you would message.
    Tagged so the live 'message a seat' picker can hide it while it stays
    QUERYABLE elsewhere (owner steer: declutter, do not delete).
    Delegates to seats._is_ephemeral_sa — ONE criterion for every surface that
    hides them (picker + the fleet-presence 'online' list)."""
    from . import seats
    return seats._is_ephemeral_sa(
        s.get("seat"), s.get("home_room"), s.get("cwd"))



# roster_report is the ONE heavy read on the poll path (~2.4s at 200 seats: it
# walks pending per seat). Uncached, N clients x 2s polls ran N CONCURRENT
# 2.4s computes on the threaded server -> CPU pegged -> 25s responses ->
# BrokenPipeError -> UI went blank (thread-pool starvation class).
# Single-flight TTL cache: ONE compute per freshness window; every other
# poller is served from memory (decision-spirit #22 — memory is the
# coordination read-path; the recompute is the write-behind). The TTL sits
# just past the 2s poll cadence so each window recomputes at most once, and
# the ephemeral tag is baked in so the cached rep is fully publish-ready.
_ROSTER_REP_CACHE = {}          # room -> (computed_at, rep)

_ROSTER_REP_TTL = 2.5

_ROSTER_REP_LOCK = threading.Lock()



# ── upstream starvation, joined onto the roster the owner actually reads ────
# proxywatch has classified this correctly for a long time and NOBODY COULD SEE
# IT. Measured live: ds4pro RATE-LIMITED, grok AUTH-UNAVAILABLE, gemini
# MALFORMED200, codex/kimi HEALTHY — all accurate, all confirmed against raw
# proxy log tails, and every seat row on the console carried state=None. The
# owner's read of a rate-limited seat was "quiet", which is indistinguishable
# from "idle" and is the wrong thing to conclude about a seat that is alive,
# trying, and being refused upstream.
#
# READ-MODEL, NOT A PROBE. This reads the record proxywatch's timer already
# persists; it never calls health() and never spends a canary token. One small
# file read per roster CACHE REBUILD (not per row, not per poll). Probing
# upstream on a 2s poll path would put the console's own traffic into the rate
# limits it is trying to report.
def _upstream_by_family():
    """({family: rec}, age_seconds_or_None, error_or_None) from the persisted
    proxywatch record.

    Three answers, never two. An unreadable or absent record is NOT "everyone
    is fine" — that is the could-not-look-recorded-as-a-fact class this repo
    has hit at the filesystem, argv, event-ledger and roster layers already,
    and a starvation badge is exactly the surface where silence reads as
    health."""
    try:
        from . import proxywatch
        st, err = proxywatch._read_delivery_state()
        if err:
            return {}, None, err
        age = None
        try:
            age = max(0, int(time.time() - os.path.getmtime(
                proxywatch._state_path())))
        except OSError:
            pass                      # the map is still good; only age is not
        upstream, shape_err = proxywatch.upstream_records(st)
        return (upstream or {}), age, shape_err
    except Exception as e:
        return {}, None, "proxywatch state unreadable — %s" % e.__class__.__name__



# A record this old stopped describing NOW, and the badge must say so rather
# than keep asserting the last thing the watcher happened to see.
#
# DERIVED FROM THE INSTALLED TIMER, NOT FROM proxywatch.INTERVAL_S. The unit
# on this box is OnUnitActiveSec=900s — 15 minutes — with observed starts
# exactly 15 minutes apart and a normal pass finishing in about 8 seconds.
# INTERVAL_S now agrees (installed is truth, the standing ruling), but when it
# lagged at 5 minutes, reading the code constant instead of the installed
# unit would have set this threshold three times too tight.
#
# I had 20 minutes here first and every badge on the fleet read UNKNOWN,
# because 15-minute cycles plus any pass overrun crosses 20 routinely. A
# staleness rule that fires in normal operation does not report staleness, it
# just deletes the feature. Two missed cycles plus margin is the honest line:
# one late pass is not news, a watcher that has missed two is.
# TWINNED WITH proxywatch.UPSTREAM_CACHE_FRESH_S, DELIBERATELY NOT IMPORTED.
# `helm seat where`/`list` grew the same staleness question and now carry the
# same number; the obvious move is for one to read the other. It was tried and
# reverted: this module imports proxywatch LAZILY, inside a try, so the console
# still serves when proxywatch is unavailable — and a module-scope constant
# reading through it destroys exactly that tolerance (measured: NameError at
# import, the whole web module dead).
#
# So the duplication is a CHOICE, not an oversight. If either moves, move both:
# they answer the same question about the same record, and the failure mode of
# them drifting is two surfaces disagreeing about whether one file is stale.
_UPSTREAM_STALE_S = 40 * 60



def _annotate_upstream(row, upstream, age, err):
    """Join one seat's upstream state onto its roster row, in place.

    A seat with no proxy family gets NOTHING — not "unknown". claude-direct
    seats have no upstream to be starved by, so a badge there would be noise
    on every row that can never carry the condition (the attention-budget law:
    relevance is decided by capability-to-act, and there is no act here).

    A FRESH record additionally carries the record's own wall verdict and, when
    recorded, its reset horizon:
      upstream_dark      — rec["dark"], the bit proxywatch composed from
                           _UPSTREAM_DARK at write time. Passed through, never
                           re-derived: the roster must not grow a second
                           opinion about which states are walls (one sheet).
                           EXACT booleans only — bool("false") is True, so a
                           corrupt persisted value maps to None ("verdict
                           unreadable"), which the client renders calm, never
                           as an asserted wall (a review FIX round on
                           this lane).
      upstream_resets_at_ms — rec["resets_at_ms"] (epoch ms, the repo-wide
                           reset-horizon vocabulary — providers/creds carry the
                           same key). Nothing records it yet; the
                           standing-config lane will. Present only when the
                           record carries a FINITE number, ABSENT otherwise —
                           a horizon the record does not hold is not rendered
                           as one that merely happens to be empty, and
                           int(float("nan")) RAISES: one malformed persisted
                           field must never take /api/chat/roster down to
                           seats=[] unavailable (measured in a review FIX
                           round; the persisting json allows NaN).
    A STALE record asserts NEITHER: it degrades to UNKNOWN with the last-seen
    state preserved, and claiming "dark" or "resets at" from a watcher that
    stopped writing would be the same could-not-look-read-as-fact bug one
    field over."""
    seat = row.get("seat")
    if not seat:
        return
    try:
        from . import proxywatch, seat as seatmod
        family, ferr = seatmod.family_for(
            seat, row.get("runtime"), row.get("runtime_verified") is True)
    except Exception:
        return                        # never take the roster down for a badge
    if ferr or not family:
        return                        # not a proxy seat: correctly silent
    if err:
        row["upstream"] = "UNKNOWN"
        row["upstream_family"] = family
        row["beacon_paused"] = True
        row["upstream_why"] = err
        return
    rec, rec_err = proxywatch.upstream_record({"upstream": upstream}, family)
    if rec_err:
        row["upstream"] = "UNKNOWN"
        row["upstream_family"] = family
        row["beacon_paused"] = True
        row["upstream_why"] = rec_err
        return
    stale = age is not None and age > _UPSTREAM_STALE_S
    # Same classifier the delivery actuator uses. The owner sees PAUSED even
    # when the watcher later goes stale: stale is not a measured recovery, and
    # pending rows remain held until proxywatch records HEALTHY.
    row["beacon_paused"] = proxywatch.beacon_paused(rec)
    row["upstream"] = "UNKNOWN" if stale else rec.get("state")
    row["upstream_family"] = family
    row["upstream_since"] = rec.get("since")
    row["upstream_last_dark"] = rec.get("last_dark_state")
    row["upstream_age_s"] = age
    if rec.get("dark_invalid"):
        row["upstream_why"] = "proxywatch dark latch is not boolean"
    if stale:
        row["upstream_why"] = ("proxywatch last wrote %dm ago (>%dm): this is "
                               "the last thing it saw, not the state now"
                               % (age // 60, _UPSTREAM_STALE_S // 60))
        row["upstream_stale_state"] = rec.get("state")
        return                        # THEN is all a stale record may assert
    dark = rec.get("dark")
    row["upstream_dark"] = None if rec.get("dark_invalid") else \
        dark if isinstance(dark, bool) else None
    resets = rec.get("resets_at_ms")
    if isinstance(resets, (int, float)) and not isinstance(resets, bool) \
            and math.isfinite(resets):
        row["upstream_resets_at_ms"] = int(resets)
        # THE TWO CLOCKS NEVER RENDER AS ONE (the provenance
        # law): an owner-entered vendor reset and a measured proxy bench
        # horizon carry the same key, so the provenance rides beside it and
        # the tooltip can say whose number this is.
        if rec.get("reset_kind"):
            row["upstream_reset_kind"] = str(rec.get("reset_kind"))
        if rec.get("reset_source"):
            row["upstream_reset_source"] = str(rec.get("reset_source"))



def _roster_cached(room):
    hit = _ROSTER_REP_CACHE.get(room)
    if hit and time.time() - hit[0] < _ROSTER_REP_TTL:
        return hit[1]
    with _ROSTER_REP_LOCK:
        hit = _ROSTER_REP_CACHE.get(room)   # re-check under the lock: the
        if hit and time.time() - hit[0] < _ROSTER_REP_TTL:  # single-flight gate
            return hit[1]
        from . import seats
        rep = seats.roster_report(room)
        upstream, up_age, up_err = _upstream_by_family()
        for s in rep.get("seats", []):
            s["ephemeral"] = _seat_ephemeral(s)
            # THE BADGE MUST NEVER TAKE THE ROSTER DOWN. A raise here rode up
            # to _api_chat_roster's catch-all and the WHOLE payload answered
            # seats=[] unavailable — one malformed persisted field blanked
            # every seat (measured: int(float("nan")) on the horizon, a
            # review FIX round). The parse boundary now rejects that input;
            # this seam holds the general law: a join failure degrades THIS
            # seat's field to an honest UNKNOWN-with-reason, never the payload.
            try:
                _annotate_upstream(s, upstream, up_age, up_err)
            except Exception as e:    # noqa: BLE001 — the fail-open boundary
                s["upstream"] = "UNKNOWN"
                s["upstream_why"] = ("roster upstream join failed — %s"
                                     % e.__class__.__name__)
        _ROSTER_REP_CACHE[room] = (time.time(), rep)
        return rep



# ── the OWNER's presence row ────────────────────────────────────────────────
# The retired cave tab's presence pane knew exactly ONE thing the roster did
# not: that a HUMAN is at a cockpit right now. Everything else it showed was
# protocol (opaque envelope ids and byte counts) or a duplicate of the roster.
# So the human keeps his row and the tab goes.
#
# The signal is the cockpit's OWN poll, not a self-POST heartbeat: every open
# page runs GET /api/chat every ~2s regardless of which tab is showing (the
# cave's heartbeat only beat while ITS tab was open, which is why the owner
# vanished from his own presence pane whenever he looked at anything else).
# One in-process stamp, one TTL, no tmpfs peer file, nothing to reap.
OWNER_PRESENCE_TTL = 30           # matches the old multiplayer heartbeat TTL



def _owner_row():
    """The owner's roster row while a cockpit is open, else None.

    Deliberately NOT shaped like a seat: `owner: True` and no session/cwd/
    pending, so the panel can render it as the human it is and every existing
    seat consumer (the message picker, the git side-channel, the live count)
    can skip it on one field. presence is derived from the SAME beat the row
    is gated on, so the dot can never claim a freshness the stamp does not
    have."""
    seen = _COCKPIT_BEAT[0]
    age = time.time() - seen
    if not seen or age > OWNER_PRESENCE_TTL:
        return None
    from . import chat
    return {"seat": chat._dsan(_chat_profile()), "owner": True,
            "presence": "fresh" if age < 10 else "quiet",
            "last_seen": seen, "connection": MP_OWNER_CONNECTION,
            "line": "watching the cockpit", "source": "cockpit",
            "session": None, "runtime": None, "project": None, "cwd": None,
            "home_room": None, "pending": 0, "preview": None,
            "todo": None, "ephemeral": False}



def _api_chat_roster(qs):
    """The seats panel's read: roster presence + per-seat pending deliveries
    + live claims (seats.py — the meld parity surface), PLUS the
    owner's own row while a cockpit is open. Read-only, fail-open: any
    surprise answers empty, never a 500. Each seat is tagged `ephemeral` so
    the live picker can hide done review-SAs (kept queryable). Served from the
    single-flight roster cache — poll fan-in never stacks concurrent heavy
    computes again.

    The owner row is grafted on a COPY of the cached report, never into it: a
    cache mutated per request would accumulate one owner row per poll for the
    whole TTL window."""
    try:
        rep = _roster_cached(_q1(qs, "room", "main"))
    except Exception:
        return {"seats": [], "claims": [], "unavailable": True}, 200
    try:
        owner = _owner_row()
    except Exception:
        owner = None            # the human's row never takes the fleet's down
    if owner:
        rep = dict(rep)
        rep["seats"] = [owner] + list(rep.get("seats") or [])
    return rep, 200



def _api_notes():
    """FLEET NOTES — what the fleet left the OWNER, rendered on the home tab.
    Read-only, fail-open (the roster read's law): any surprise answers an empty
    list at 200, never a 500 and never a blank home page. These rows used to
    live on the retired cave tab's tmpfs demo board, where a reboot ate them;
    fleetnotes.py is the durable home (helm note set|list|rm)."""
    try:
        from . import fleetnotes
        return {"notes": fleetnotes.rows(), "cap": fleetnotes.MAX_KEYS}
    except Exception:
        return {"notes": [], "unavailable": True}



def _api_storage_matrix():
    """The owner-facing storage benchmark snapshot. Read-only: the browser
    never measures or crosses SSH. Missing, stale, partial and unreadable are
    data states, not server errors, so every one answers at HTTP 200."""
    try:
        from . import storage_matrix
        return storage_matrix.view()
    except Exception:
        return {"state": "unavailable", "message":
                "measurement unavailable — the snapshot could not be read"}



def _api_todos(qs):
    """The owner's fleet-todo read: every seat and the task it is on right
    now (todos.py — the pull half of the TodoWrite/Task* mirror). Read-only,
    fail-open: any surprise answers empty, never a 500."""
    try:
        from . import todos
        return todos.fleet(), 200
    except Exception:
        return {"seats": [], "orphans": [], "orphans_hidden": 0, "now": 0,
                "unavailable": True}, 200



# The roster DOING column shows a lane-less (source=home) agent's repo LAST
# COMMIT instead of just its home room. git log is a subprocess, so it is kept
# OFF the 2s presence poll (roster_report/presence_report stay subprocess-free):
# it lives here, on the roster tab's OWN 60s timer, behind a module-level 60s
# TTL cache. Fail-open per cwd (a bad cwd caches None, never 500s the panel).
_ROSTER_GIT_CACHE = {}   # cwd -> (fetched_at, {"ts", "subject"} | None)

_ROSTER_GIT_TTL = 60



def _roster_git_one(cwd, now):
    hit = _ROSTER_GIT_CACHE.get(cwd)
    if hit and now - hit[0] < _ROSTER_GIT_TTL:
        return hit[1]
    info = None
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "log", "-1", "--format=%ct%x09%s"],
            capture_output=True, text=True, timeout=2)
        line = (r.stdout or "").strip()
        if r.returncode == 0 and line:
            ts_s, _, subj = line.partition("\t")
            info = {"ts": int(ts_s), "subject": subj}
    except (OSError, ValueError, subprocess.SubprocessError):
        info = None   # fail-open: cache the miss so a bad cwd is not re-run hot
    _ROSTER_GIT_CACHE[cwd] = (now, info)
    return info



def _api_roster_git(qs):
    """Last commit (epoch ts + subject) per distinct non-/tmp cwd of the
    NON-ABSENT, lane-less (source=home) seats — the only rows whose DOING cell
    the roster tab decorates with 'last commit N ago'. Read-only, fail-open;
    the 60s cache + the tab's 60s poll keep git off the 2s presence hot path."""
    now = time.time()
    try:
        # rides the same single-flight roster cache as the panel read — this
        # endpoint no longer triggers its own heavy roster_report walk.
        rows = _roster_cached(_q1(qs, "room", "main")).get("seats", [])
    except Exception:
        rows = []
    seen, out = set(), {}
    for s in rows:
        if not isinstance(s, dict):
            continue
        if s.get("presence") == "absent" or s.get("source") != "home":
            continue
        cwd = s.get("cwd")
        if not cwd or cwd.startswith("/tmp") or cwd in seen:
            continue
        seen.add(cwd)
        info = _roster_git_one(cwd, now)
        if info:
            out[cwd] = info
    return {"commits": out}, 200



def _api_chat_seat(payload):
    """The seats panel's one mutation: bind a live agent to a memorable @name
    (seats.rename_seat — roster row + delivery state move together). Bearer-
    gated like every POST; refusals answer 400 with the CLI's own message."""
    from . import seats
    if payload.get("action") != "rename":
        return {"error": "unknown action %r (rename)" % payload.get("action")}, 400
    ok, msg = seats.rename_seat(str(payload.get("seat") or ""),
                                str(payload.get("new") or ""))
    return ({"ok": True, "note": msg} if ok else {"error": msg}), (200 if ok else 400)



def _api_chat_react(payload):
    """The owner's click-to-react: target by ts+from (the row the panel
    holds). Signed like a post; rides the same transport."""
    from . import chat, seats
    e = payload.get("emoji")
    tts, tfrom = payload.get("tts"), payload.get("tfrom")
    if not (isinstance(e, str) and e and isinstance(tts, str) and isinstance(tfrom, str)):
        return {"error": 'payload wants {"emoji", "tts", "tfrom"}'}, 400
    room = str(payload.get("room") or "main")
    row, err = chat.react((tts, tfrom), e, room,
                          who=str(payload.get("name") or seats.owner_name()),
                          profile=_chat_profile())
    if err:
        return {"error": err}, 400
    return {"ok": True, "msg": chat.public_rows([row])[0],
            "total": chat.read(room)[1]}, 200
del _web
